# This file is part of Xpra.
# Copyright (C) 2026 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import unittest
import warnings
from queue import Queue
from threading import Event, Lock, RLock, Thread
from time import monotonic
from types import SimpleNamespace
from unittest.mock import Mock, patch

from xpra.codecs.image import ImageWrapper
from xpra.net.packet_type import WINDOW_EOS
from xpra.server.source import client_connection
from xpra.server.window import compress, video_compress, video_subregion
from xpra.server.window.compress import WindowSource
from xpra.server.window.video_compress import WindowVideoSource
from xpra.server.window.video_subregion import VideoSubregion
from xpra.util.objects import typedict
from xpra.util.rectangle import rectangle

from unit.server.source.encoding_lifecycle_test import make_encoding_connection, finish_encoding_connection


class PipelineElement:

    def __init__(self, clean_error: bool = False) -> None:
        self.clean_count = 0
        self.clean_error = clean_error

    def clean(self) -> None:
        self.clean_count += 1
        if self.clean_error:
            raise RuntimeError("cleanup failed")


class ScrollOwner:

    def __init__(self) -> None:
        self.free_count = 0

    def free(self) -> None:
        self.free_count += 1


class Converter(PipelineElement):

    def __init__(self, init_error: bool = False, clean_error: bool = False) -> None:
        super().__init__(clean_error)
        self.init_error = init_error

    def init_context(self, src_width, src_height, _src_format,
                     dst_width, dst_height, _dst_format, _options) -> None:
        self.src_size = src_width, src_height
        self.dst_size = dst_width, dst_height
        if self.init_error:
            raise RuntimeError("converter init failed")

    def get_info(self) -> dict:
        return {}


class Encoder(PipelineElement):

    def __init__(self, init_started=None, finish_init=None,
                 init_error: bool = False, clean_error: bool = False,
                 info_started=None, finish_info=None) -> None:
        super().__init__(clean_error)
        self.init_started = init_started
        self.finish_init = finish_init
        self.init_error = init_error
        self.info_started = info_started
        self.finish_info = finish_info

    def init_context(self, _encoding, _width, _height, _src_format, _options) -> None:
        if self.init_started:
            self.init_started.set()
        if self.finish_init and not self.finish_init.wait(2):
            raise RuntimeError("encoder setup was not released")
        if self.init_error:
            raise RuntimeError("encoder init failed")

    def get_info(self) -> dict:
        if self.info_started:
            self.info_started.set()
        if self.finish_info and not self.finish_info.wait(2):
            raise RuntimeError("encoder info was not released")
        return {}


class EarlyRealGLib:
    """Use real GLib sources, dispatching each before attachment returns."""

    def __init__(self, glib) -> None:
        self.glib = glib
        self.context = glib.MainContext.default()
        self.remove_warnings = []

    def dispatch(self) -> None:
        if not self.context.iteration(False):
            raise RuntimeError("the real GLib source was not ready")

    def timeout_add(self, _delay, callback, *args):
        source_id = self.glib.timeout_add(0, callback, *args)
        self.dispatch()
        return source_id

    def idle_add(self, callback, *args):
        source_id = self.glib.idle_add(callback, *args)
        self.dispatch()
        return source_id

    def source_remove(self, source_id):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = self.glib.source_remove(source_id)
        self.remove_warnings.extend(str(item.message) for item in caught)
        return result

    def timeout_source_new(self, _delay):
        return self.wrap(self.glib.timeout_source_new(0))

    def idle_source_new(self):
        return self.wrap(self.glib.idle_source_new())

    def wrap(self, source):
        owner = self

        class Source:
            def set_callback(self, callback):
                source.set_callback(callback)

            def attach(self, context):
                source_id = source.attach(context)
                owner.dispatch()
                return source_id

            def destroy(self):
                source.destroy()

        return Source()


class FakeSource:

    def __init__(self, registry, delay=None, source_id=0) -> None:
        self.registry = registry
        self.delay = delay
        self.source_id = source_id
        self.callback = None
        self.destroyed = False

    def set_callback(self, callback) -> None:
        self.callback = callback

    def attach(self, _context) -> int:
        if self.delay is None:
            self.source_id = self.registry.idle_add(self.callback)
        else:
            self.source_id = self.registry.timeout_add(self.delay, self.callback)
        return self.source_id

    def get_id(self) -> int:
        return self.source_id

    def destroy(self) -> None:
        if not self.destroyed:
            self.destroyed = True
            if self.source_id:
                self.registry.source_remove(self.source_id)


class BlockingSources:

    def __init__(self, block_timeout=(), block_idle=(), dispatch_timeout=(), dispatch_idle=()) -> None:
        self.block_timeout = set(block_timeout)
        self.block_idle = set(block_idle)
        self.dispatch_timeout = set(dispatch_timeout)
        self.dispatch_idle = set(dispatch_idle)
        self.timeout_started = Event()
        self.idle_started = Event()
        self.release_timeout = Event()
        self.release_idle = Event()
        self.next_id = 100
        self.timeout_calls: list[tuple[int, object, tuple]] = []
        self.idle_calls: list[tuple[int, object, tuple]] = []
        self.callback_results: list[object] = []
        self.removed: list[int] = []

    def timeout_add(self, _delay, callback, *args) -> int:
        self.next_id += 1
        source_id = self.next_id
        self.timeout_calls.append((source_id, callback, args))
        if len(self.timeout_calls) in self.dispatch_timeout:
            self.callback_results.append(callback(*args))
        if len(self.timeout_calls) in self.block_timeout:
            self.timeout_started.set()
            if not self.release_timeout.wait(2):
                raise RuntimeError("timeout publication was not released")
        return source_id

    def idle_add(self, callback, *args) -> int:
        self.next_id += 1
        source_id = self.next_id
        self.idle_calls.append((source_id, callback, args))
        if len(self.idle_calls) in self.dispatch_idle:
            self.callback_results.append(callback(*args))
        if len(self.idle_calls) in self.block_idle:
            self.idle_started.set()
            if not self.release_idle.wait(2):
                raise RuntimeError("idle publication was not released")
        return source_id

    def source_remove(self, source_id: int) -> None:
        self.removed.append(source_id)

    def timeout_source_new(self, delay):
        return FakeSource(self, delay)

    def idle_source_new(self):
        return FakeSource(self)


class EncodeWorker:

    def __init__(self) -> None:
        self.queue: Queue = Queue()
        self.callbacks: list = []
        self.errors: list[BaseException] = []
        self.thread = Thread(target=self.run, name="test-encode", daemon=True)
        self.thread.start()

    def call(self, callback, *args) -> None:
        self.callbacks.append(callback)
        self.queue.put((callback, args))

    def run(self) -> None:
        while True:
            item = self.queue.get()
            try:
                if item is None:
                    return
                callback, args = item
                callback(*args)
            except BaseException as e:
                self.errors.append(e)
            finally:
                self.queue.task_done()

    def drain(self) -> None:
        self.queue.join()

    def close(self) -> None:
        self.queue.put(None)
        self.queue.join()
        self.thread.join(2)
        if self.thread.is_alive():
            raise RuntimeError("encode worker did not stop")


class VideoSubregionLifecycleTest(unittest.TestCase):

    def test_real_glib_early_refresh_dispatch_owns_exact_source(self) -> None:
        for family in ("video", "nonvideo"):
            with self.subTest(family=family):
                sources = EarlyRealGLib(video_subregion.GLib)
                callback = Mock(return_value=True)
                subregion = VideoSubregion(callback, 150, True)
                subregion.rectangle = rectangle(0, 0, 64, 64)
                region = rectangle(0, 0, 32, 32) if family == "video" else rectangle(80, 80, 16, 16)
                with patch.object(video_subregion, "GLib", sources):
                    try:
                        subregion.add_video_refresh(region)
                        callback.assert_called_once()
                        self.assertEqual(sources.remove_warnings, [])
                        self.assertEqual(subregion.refresh_timer, 0)
                        self.assertEqual(subregion.nonvideo_refresh_timer, 0)
                    finally:
                        subregion.cleanup()

    def test_cleanup_waits_for_claimed_refresh_callback(self) -> None:
        for family in ("video", "nonvideo"):
            with self.subTest(family=family):
                sources = BlockingSources()
                callback_started = Event()
                release_callback = Event()
                cleanup_started = Event()
                cleanup_done = Event()
                refresh_calls = []

                def refresh(regions) -> bool:
                    refresh_calls.append(tuple(regions))
                    callback_started.set()
                    if not release_callback.wait(2):
                        raise RuntimeError("refresh callback was not released")
                    return family == "nonvideo"

                subregion = VideoSubregion(refresh, 150, True)
                subregion.rectangle = rectangle(0, 0, 64, 64)
                region = rectangle(0, 0, 32, 32) if family == "video" else rectangle(80, 80, 16, 16)
                with patch.object(video_subregion, "GLib", sources):
                    subregion.add_video_refresh(region)
                    _source_id, callback, args = sources.timeout_calls[0]
                    dispatch = Thread(target=callback, args=args, daemon=True)
                    dispatch.start()
                    self.assertTrue(callback_started.wait(2))

                    def cleanup() -> None:
                        cleanup_started.set()
                        subregion.cleanup()
                        cleanup_done.set()

                    close = Thread(target=cleanup, daemon=True)
                    close.start()
                    try:
                        self.assertTrue(cleanup_started.wait(2))
                        self.assertFalse(cleanup_done.wait(0.05))
                        release_callback.set()
                        dispatch.join(2)
                        close.join(2)
                        self.assertFalse(dispatch.is_alive())
                        self.assertFalse(close.is_alive())
                        self.assertTrue(cleanup_done.is_set())
                        self.assertEqual(len(refresh_calls), 1)
                        self.assertEqual(subregion.refresh_timer, 0)
                        self.assertEqual(subregion.nonvideo_refresh_timer, 0)

                        for _timer, stale_callback, stale_args in tuple(sources.timeout_calls):
                            self.assertFalse(stale_callback(*stale_args))
                        self.assertEqual(len(refresh_calls), 1)
                    finally:
                        release_callback.set()
                        dispatch.join(2)
                        close.join(2)

    def test_callback_before_timer_publication_is_not_retained(self) -> None:
        for family in ("video", "nonvideo"):
            with self.subTest(family=family):
                sources = BlockingSources(dispatch_timeout=(1,))
                refresh_calls = []
                subregion = VideoSubregion(lambda regions: refresh_calls.append(tuple(regions)) or True,
                                           150, True)
                subregion.rectangle = rectangle(0, 0, 64, 64)
                if family == "video":
                    region = rectangle(0, 0, 32, 32)
                    timer_name = "refresh_timer"
                else:
                    region = rectangle(80, 80, 16, 16)
                    timer_name = "nonvideo_refresh_timer"
                with patch.object(video_subregion, "GLib", sources):
                    subregion.add_video_refresh(region)

                source_id, _callback, _args = sources.timeout_calls[0]
                self.assertEqual(sources.callback_results, [False])
                self.assertEqual(sources.removed, [source_id])
                self.assertEqual(getattr(subregion, timer_name), 0)
                self.assertEqual(len(refresh_calls), 1)

    def test_cleanup_reclaims_timer_published_after_cancellation(self) -> None:
        for family in ("video", "nonvideo"):
            with self.subTest(family=family):
                sources = BlockingSources(block_timeout=(1,))
                refresh_calls = []
                subregion = VideoSubregion(lambda regions: refresh_calls.append(regions), 150, True)
                subregion.rectangle = rectangle(0, 0, 64, 64)
                if family == "video":
                    region = rectangle(0, 0, 32, 32)
                    timer_name = "refresh_timer"
                else:
                    region = rectangle(80, 80, 16, 16)
                    timer_name = "nonvideo_refresh_timer"
                with patch.object(video_subregion, "GLib", sources):
                    schedule = Thread(target=subregion.add_video_refresh, args=(region,), daemon=True)
                    schedule.start()
                    try:
                        self.assertTrue(sources.timeout_started.wait(2))
                        subregion.cleanup()
                        sources.release_timeout.set()
                        schedule.join(2)
                        self.assertFalse(schedule.is_alive())

                        source_id, callback, args = sources.timeout_calls[0]
                        self.assertEqual(sources.removed, [source_id])
                        self.assertEqual(getattr(subregion, timer_name), 0)
                        self.assertFalse(callback(*args))
                        self.assertEqual(refresh_calls, [])

                        subregion.reset()
                        subregion.add_video_refresh(region)
                        self.assertEqual(len(sources.timeout_calls), 1)
                    finally:
                        sources.release_timeout.set()
                        schedule.join(2)

    def test_cleanup_reclaims_refresh_retry_publication(self) -> None:
        sources = BlockingSources(block_timeout=(2,))
        refresh_calls = []

        def refresh(regions) -> bool:
            refresh_calls.append(tuple(regions))
            return False

        subregion = VideoSubregion(refresh, 150, True)
        subregion.rectangle = rectangle(0, 0, 64, 64)
        with patch.object(video_subregion, "GLib", sources):
            subregion.add_video_refresh(rectangle(0, 0, 32, 32))
            _source_id, callback, args = sources.timeout_calls[0]
            dispatch = Thread(target=callback, args=args, daemon=True)
            dispatch.start()
            try:
                self.assertTrue(sources.timeout_started.wait(2))
                subregion.cleanup()
                sources.release_timeout.set()
                dispatch.join(2)
                self.assertFalse(dispatch.is_alive())

                retry_id, retry_callback, retry_args = sources.timeout_calls[1]
                self.assertIn(retry_id, sources.removed)
                self.assertEqual(subregion.refresh_timer, 0)
                self.assertEqual(len(refresh_calls), 1)
                self.assertFalse(retry_callback(*retry_args))
                self.assertEqual(len(refresh_calls), 1)
            finally:
                sources.release_timeout.set()
                dispatch.join(2)

    def test_cleanup_attempts_both_timer_removals(self) -> None:
        subregion = VideoSubregion(lambda _regions: True, 150, True)
        removed = []

        def source_remove(source_id: int) -> None:
            removed.append(source_id)
            if source_id == 101:
                raise RuntimeError("video refresh removal failed")

        registry = SimpleNamespace(source_remove=source_remove)
        subregion.refresh_timer = FakeSource(registry, source_id=101)
        subregion.nonvideo_refresh_timer = FakeSource(registry, source_id=102)
        with patch.object(video_subregion, "GLib", registry), \
                self.assertRaisesRegex(RuntimeError, "video refresh removal failed"):
            subregion.cleanup()

        self.assertEqual(removed, [101, 102])
        self.assertEqual(subregion.refresh_timer, 0)
        self.assertEqual(subregion.nonvideo_refresh_timer, 0)
        self.assertTrue(subregion._closed)


class ConnectionCleanupOrderTest(unittest.TestCase):

    def test_closed_fifo_rejects_retained_producer_and_tail_registration(self) -> None:
        events = []
        source = self.make_connection(events)
        source.window_sources = {}
        retained_producer = source.queue_encode
        source.call_in_encode_thread(events.append, "accepted")
        source.call_in_encode_thread_at_end(events.append, "tail")
        source.close()
        source.stop_encode_thread()
        with self.assertRaisesRegex(RuntimeError, "after encode termination"):
            retained_producer((events.append, ("late",)))
        with self.assertRaisesRegex(RuntimeError, "after encode termination"):
            source.call_in_encode_thread_at_end(events.append, "late-tail")
        with self.assertRaises(ValueError):
            retained_producer(None)
        source.encode_thread.join(2)
        self.assertFalse(source.encode_thread.is_alive())
        self.assertTrue(source.encode_work_queue.empty())
        self.assertEqual(events, ["accepted", "tail", "cuda-free"])

    def test_baseexceptions_do_not_abandon_accepted_work_or_resource_tail(self) -> None:
        events = []
        source = self.make_connection(events)
        source.window_sources = {}
        entered, release = Event(), Event()

        def hold() -> None:
            entered.set()
            if not release.wait(2):
                raise AssertionError("encode worker was not released")

        def fail() -> None:
            raise SystemExit("callback failed")

        # Logger.error can be a read-only extension method. Replace the module
        # logger reference, not an attribute of that native instance.
        with patch.object(client_connection, "log") as logger:
            source.call_in_encode_thread(hold)
            try:
                self.assertTrue(entered.wait(2))
                source.call_in_encode_thread(fail)
                source.call_in_encode_thread(events.append, "accepted-after-error")
                source.call_in_encode_thread_at_end(fail)
                source.close()
            finally:
                release.set()
                source.encode_thread.join(2)
        self.assertFalse(source.encode_thread.is_alive())
        self.assertTrue(source.encode_work_queue.empty())
        self.assertEqual(events, ["accepted-after-error", "cuda-free"])
        self.assertGreaterEqual(logger.error.call_count, 2)

    def test_failed_worker_start_does_not_accept_resource(self) -> None:
        source = self.make_connection([])
        source.window_sources = {}
        callback = Mock()
        with patch.object(client_connection, "start_thread", side_effect=RuntimeError("start failed")):
            with self.assertRaisesRegex(RuntimeError, "start failed"):
                source.call_in_encode_thread(callback)
        self.assertTrue(source.encode_work_queue.empty())
        self.assertIsNone(source.encode_thread)
        callback.assert_not_called()
        source.call_in_encode_thread(callback)
        source.close()
        source.encode_thread.join(2)
        self.assertFalse(source.encode_thread.is_alive())
        callback.assert_called_once_with()

    def test_repeated_close_leaves_no_work_behind_sentinel(self) -> None:
        events = []
        source = self.make_connection(events)
        source.window_sources = {}
        source.close()
        source.close()
        worker = source.encode_thread
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(source.encode_work_queue.empty())
        self.assertEqual(events, ["cuda-free"])

    def make_connection(self, events):
        source = make_encoding_connection()
        self.addCleanup(finish_encoding_connection, source)
        source.cancel_recalculate_timer = Mock()
        source.cuda_device_context = SimpleNamespace(free=lambda: events.append("cuda-free"))
        return source

    def test_queue_sentinel_follows_window_and_shared_resource_cleanup(self) -> None:
        events = []
        source = self.make_connection(events)

        def window_cleanup() -> None:
            source.call_in_encode_thread(events.append, "codec-clean")

        source.window_sources = {1: SimpleNamespace(cleanup=window_cleanup)}
        source.close()
        worker = source.encode_thread
        worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(events, ["codec-clean", "cuda-free"])
        self.assertEqual(source.encode_work_queue.qsize(), 0)
        self.assertIsNone(source.cuda_device_context)

    def test_mmap_area_outlives_queued_window_encodes(self) -> None:
        events = []
        source = self.make_connection(events)
        area = Mock()
        area.close.side_effect = lambda: events.append("mmap-close")
        source.mmap_write_area = area

        def window_cleanup() -> None:
            source.call_in_encode_thread(
                lambda: events.append("codec-before-mmap-close")
                if "mmap-close" not in events else events.append("codec-after-mmap-close"),
            )

        source.window_sources = {1: SimpleNamespace(cleanup=window_cleanup)}
        source.close()
        worker = source.encode_thread
        worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(events, ["codec-before-mmap-close", "cuda-free", "mmap-close"])
        area.close.assert_called_once_with()
        self.assertIsNone(source.mmap_write_area)

    def test_queue_sentinel_and_later_mixins_survive_cleanup_error(self) -> None:
        events = []
        source = self.make_connection(events)

        def window_cleanup() -> None:
            source.call_in_encode_thread(events.append, "codec-clean")
            raise RuntimeError("window cleanup failed")

        source.window_sources = {1: SimpleNamespace(cleanup=window_cleanup)}
        with self.assertRaisesRegex(RuntimeError, "failed to close"):
            source.close()
        worker = source.encode_thread
        worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(events, ["codec-clean", "cuda-free"])
        self.assertEqual(source.encode_work_queue.qsize(), 0)
        self.assertIsNone(source.cuda_device_context)


class VideoContextCleanTest(unittest.TestCase):

    @staticmethod
    def make_source(csc=None, encoder=None) -> WindowVideoSource:
        source = WindowVideoSource.__new__(WindowVideoSource)
        source._video_state_lock = RLock()
        source._video_cleanup_queued = False
        source._b_frame_flush_generation = 0
        source._video_source_closed = False
        source._video_stream_encoder = None
        source._csc_encoder = csc
        source._video_encoder = encoder
        source.cancel_video_encoder_flush = Mock()
        source.cancel_video_encoder_timer = Mock()
        source.call_in_encode_thread = Mock()
        source.csc_clean = Mock()
        source.ve_clean = Mock()
        source.wid = 1
        return source

    def test_cancels_timers_without_a_context(self) -> None:
        source = self.make_source()

        source.video_context_clean()

        source.cancel_video_encoder_flush.assert_called_once_with()
        source.cancel_video_encoder_timer.assert_called_once_with()
        source.call_in_encode_thread.assert_called_once()
        clean, = source.call_in_encode_thread.call_args.args

        clean()

        self.assertEqual(source.cancel_video_encoder_flush.call_count, 2)
        self.assertEqual(source.cancel_video_encoder_timer.call_count, 2)
        source.csc_clean.assert_not_called()
        source.ve_clean.assert_not_called()

    def test_cleans_context_published_after_empty_snapshot(self) -> None:
        source = self.make_source()

        source.video_context_clean()
        clean, = source.call_in_encode_thread.call_args.args

        csc = Mock()
        encoder = Mock()
        source._csc_encoder = csc
        source._video_encoder = encoder
        clean()

        self.assertIsNone(source._csc_encoder)
        self.assertIsNone(source._video_encoder)
        source.csc_clean.assert_called_once_with(csc)
        source.ve_clean.assert_called_once_with(encoder)

    def test_detaches_and_cleans_context(self) -> None:
        csc = Mock()
        encoder = Mock()
        source = self.make_source(csc, encoder)

        source.video_context_clean()

        self.assertIsNone(source._csc_encoder)
        self.assertIsNone(source._video_encoder)
        source.call_in_encode_thread.assert_called_once()
        clean, = source.call_in_encode_thread.call_args.args

        clean()

        self.assertEqual(source.cancel_video_encoder_flush.call_count, 2)
        source.csc_clean.assert_called_once_with(csc)
        source.ve_clean.assert_called_once_with(encoder)

    def test_timer_cancel_error_does_not_bypass_context_cleanup(self) -> None:
        csc = Mock()
        encoder = Mock()
        source = self.make_source(csc, encoder)
        source.cancel_video_encoder_flush.side_effect = (RuntimeError("timer removal failed"), None)

        with self.assertRaisesRegex(RuntimeError, "timer removal failed"):
            source.video_context_clean()

        self.assertIsNone(source._csc_encoder)
        self.assertIsNone(source._video_encoder)
        source.cancel_video_encoder_timer.assert_called_once_with()
        source.call_in_encode_thread.assert_called_once()

        clean, = source.call_in_encode_thread.call_args.args
        clean()

        source.csc_clean.assert_called_once_with(csc)
        source.ve_clean.assert_called_once_with(encoder)

    def test_ve_clean_still_cancels_its_timer(self) -> None:
        encoder = Mock()
        source = self.make_source()

        WindowVideoSource.ve_clean(source, encoder)

        source.cancel_video_encoder_timer.assert_called_once_with()
        encoder.clean.assert_called_once_with()

    def test_closed_encoder_flush_saves_data_before_cleanup(self) -> None:
        source = self.make_source()
        encoder = Mock()
        encoder.is_closed.side_effect = (False, True)
        encoder.get_type.return_value = "test"
        encoder.get_width.return_value = 64
        encoder.get_height.return_value = 64
        encoder.get_encoding.return_value = "h264"
        encoder.flush.return_value = b"data", {}
        source._video_encoder = encoder
        source.b_frame_flush_data = encoder, None, 1, 0, 0, None
        source.b_frame_flush_timer = 0
        source.start_video_frame = 0
        events = []
        source.video_stream_file = Mock()
        source.video_stream_file.write.side_effect = lambda data: events.append(("write", data))
        source.video_context_clean = Mock(side_effect=lambda encode_thread: events.append(("clean", encode_thread)))
        source.make_draw_packet = Mock(return_value=("draw",))
        source.queue_damage_packet = Mock()
        source.schedule_video_encoder_flush = Mock()
        source.schedule_video_encoder_timer = Mock()

        source.do_flush_video_encoder()

        self.assertEqual(events, [("write", b"data"), ("clean", True)])
        source.video_stream_file.flush.assert_called_once_with()
        source.queue_damage_packet.assert_called_once()
        source.schedule_video_encoder_flush.assert_not_called()
        source.schedule_video_encoder_timer.assert_not_called()


class NonVideoEncodingsTest(unittest.TestCase):

    def test_client_properties_exclude_unregistered_encodings(
        self,
    ) -> None:
        source = WindowVideoSource.__new__(WindowVideoSource)
        source.common_encodings = ("jpeg",)
        source.core_encodings = ("jpeg",)
        source.picture_encodings = ("jpeg", "webp")
        source._encoders = {"jpeg": Mock()}
        source.scroll_min_percent = 0
        source.scroll_preference = 100
        source.video_subregion = SimpleNamespace(supported=True)
        source.scaling_control = 0
        source.edge_encoding = ""
        source.full_csc_modes = typedict()

        def set_core_encodings(window_source, properties) -> None:
            window_source.core_encodings = properties.strtupleget("encodings.core", ())

        properties = typedict({"encodings.core": ("jpeg", "webp")})
        with patch.object(
            WindowSource, "do_set_client_properties", set_core_encodings,
        ):
            source.do_set_client_properties(properties)

        self.assertEqual(source.non_video_encodings, ("jpeg",))


class ScalingCacheTest(unittest.TestCase):

    def test_candidate_limits_override_generic_scaling(self) -> None:
        source = WindowVideoSource.__new__(WindowVideoSource)
        source.video_helper = Mock()
        source.video_helper.get_csc_specs.return_value = {}
        encoder_spec = SimpleNamespace(
            can_scale=True,
            codec_type="test",
            max_w=2048,
            max_h=2048,
            output_colorspaces=("RGB",),
        )
        source.video_helper.get_encoder_specs.return_value = {
            "RGB": (encoder_spec,),
        }
        source._current_quality = 50
        source._fixed_min_quality = 0
        source._current_speed = 50
        source._fixed_min_speed = 0
        source.content_types = ()
        source.is_shadow = False
        source.video_max_size = (4096, 4096)
        source.video_subregion = None
        source.full_csc_modes = typedict({"h264": ("RGB",)})
        source.encoding_options = typedict()
        source._csc_encoder = None
        source._video_encoder = None
        source.matches_video_subregion = Mock(return_value=None)
        source.get_video_fps = Mock(return_value=0)
        source.is_cancelled = Mock(return_value=False)
        source.calculate_scaling = Mock(side_effect=((1, 1), (2, 3)))

        with patch(
            "xpra.server.window.video_compress.get_pipeline_score",
            return_value=(1,),
        ) as get_pipeline_score:
            source.get_video_pipeline_options(("h264",), 3000, 2000, "RGB")

        scaling_calls = tuple(call.args for call in source.calculate_scaling.call_args_list)
        self.assertEqual(scaling_calls, (
            (3000, 2000, 4096, 4096),
            (3000, 2000, 2048, 2048),
        ))
        self.assertEqual(get_pipeline_score.call_args.args[5], (2, 3))


class VideoPipelineLifecycleTest(unittest.TestCase):

    def test_failed_codec_handoff_keeps_pair_for_terminal_worker_barrier(self) -> None:
        source = self.make_source()
        converter, encoder = Converter(), Encoder()
        source._csc_encoder = converter
        source._video_encoder = encoder
        source.scroll_data = scroll = ScrollOwner()
        source._video_source_closed = True
        source.call_in_encode_thread = Mock(side_effect=RuntimeError("handoff rejected"))
        with self.assertRaisesRegex(RuntimeError, "handoff rejected"):
            source.video_context_clean()
        self.assertIs(source._csc_encoder, converter)
        self.assertIs(source._video_encoder, encoder)
        self.assertEqual(converter.clean_count, 0)
        self.assertEqual(encoder.clean_count, 0)
        with patch.object(video_compress.WindowSource, "encode_ended") as base_barrier:
            source.encode_ended()
            source.encode_ended()
        self.assertEqual(converter.clean_count, 1)
        self.assertEqual(encoder.clean_count, 1)
        self.assertEqual(scroll.free_count, 1)
        self.assertIsNone(source._csc_encoder)
        self.assertIsNone(source._video_encoder)
        self.assertEqual(base_barrier.call_count, 2)

    def test_encoder_timer_failure_does_not_skip_native_cleanup_or_stream_close(self) -> None:
        source = self.make_source()
        encoder = Encoder()
        stream = Mock()
        source._video_stream_encoder = encoder
        source.video_stream_file = stream
        source.cancel_video_encoder_timer = Mock(side_effect=RuntimeError("timer removal failed"))
        with patch.object(video_compress, "SAVE_VIDEO_STREAMS", True), \
                self.assertRaisesRegex(RuntimeError, "timer removal failed"):
            source.ve_clean(encoder)
        self.assertEqual(encoder.clean_count, 1)
        stream.close.assert_called_once_with()
        self.assertIsNone(source.video_stream_file)

    def test_real_glib_early_video_dispatch_owns_exact_source(self) -> None:
        for family in ("flush", "watchdog", "av-timeout", "fallback"):
            with self.subTest(family=family):
                source = self.make_source()
                source.batch_config = SimpleNamespace(delay=20)
                source.call_in_encode_thread = Mock()
                source.video_context_clean = Mock()
                source.refresh = Mock()
                source.encode_from_queue = Mock()
                sources = EarlyRealGLib(video_compress.GLib)
                with patch.object(video_compress, "GLib", sources):
                    if family == "flush":
                        source.schedule_video_encoder_flush(object(), None, 1, 0, 0, None)
                        source.call_in_encode_thread.assert_called_once()
                    elif family == "watchdog":
                        source.schedule_video_encoder_timer()
                        source.video_context_clean.assert_called_once_with()
                    elif family == "av-timeout":
                        source.schedule_encode_from_queue(25)
                        source.encode_from_queue.assert_called_once_with()
                        source.call_in_encode_thread.assert_not_called()
                    else:
                        source.schedule_video_fallback_refresh()
                        source.refresh.assert_called_once_with({"novideo": True})
                    self.assertEqual(sources.remove_warnings, [])
                    self.assertEqual(source.b_frame_flush_timer, 0)
                    self.assertEqual(source.video_encoder_timer, 0)
                    self.assertEqual(source.encode_from_queue_timer, 0)
                    self.assertEqual(source.video_fallback_refresh_idle, 0)

    def test_cleanup_cannot_overtake_detached_context_handoff(self) -> None:
        source = self.make_source()
        source._csc_encoder = Converter()
        source._video_encoder = Encoder()
        entered = Event()
        release = Event()
        base_entered = Event()
        errors = []

        def submit(*_args) -> None:
            entered.set()
            if not release.wait(2):
                raise RuntimeError("context handoff was not released")

        def invoke(fn) -> None:
            try:
                fn()
            except BaseException as error:
                errors.append(error)

        source.call_in_encode_thread = submit
        retire = Thread(target=invoke, args=(source.video_context_clean,), daemon=True)
        cleanup = Thread(target=invoke, args=(source.cleanup,), daemon=True)
        with patch.object(video_compress.WindowSource, "cleanup", side_effect=base_entered.set):
            retire.start()
            self.assertTrue(entered.wait(2))
            cleanup.start()
            try:
                self.assertFalse(base_entered.wait(0.05))
            finally:
                release.set()
                retire.join(2)
                cleanup.join(2)
        self.assertFalse(retire.is_alive())
        self.assertFalse(cleanup.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(base_entered.is_set())

    def test_x264_fallback_refresh_is_cancelled_by_cleanup(self) -> None:
        source = self.make_source()
        source._video_encoder = encoder = Mock()
        encoder.is_closed.return_value = False
        encoder.get_type.return_value = "x264"
        source.b_frame_flush_data = (encoder, None, 0, 0, 0, None)
        source.non_video_encodings = ("rgb32",)
        source.refresh = Mock()
        sources = BlockingSources()
        with patch.object(video_compress, "GLib", sources), \
                patch.object(video_compress.WindowSource, "cleanup"):
            source.do_flush_video_encoder()
            source.cleanup()
            self.assertEqual(len(sources.idle_calls), 1)
            source_id, callback, args = sources.idle_calls[0]
            callback(*args)
            source.refresh.assert_not_called()
            self.assertIn(source_id, sources.removed)

    def test_retired_encoder_does_not_close_replacement_stream_file(self) -> None:
        source = self.make_source()
        retired = Encoder()
        current = Encoder()
        current_file = Mock()
        source._video_encoder = current
        source.video_stream_file = current_file
        source._video_stream_encoder = current
        with patch.object(video_compress, "SAVE_VIDEO_STREAMS", True):
            source.ve_clean(retired)
            current_file.close.assert_not_called()
            self.assertIs(source.video_stream_file, current_file)
            source.ve_clean(current)
        current_file.close.assert_called_once_with()
        self.assertEqual(retired.clean_count, 1)
        self.assertEqual(current.clean_count, 1)

    def test_x264_first_frame_flush_retires_whole_pipeline(self) -> None:
        source = self.make_source()
        converter = Converter()
        encoder = Mock()
        encoder.is_closed.return_value = False
        encoder.get_type.return_value = "x264"
        source._csc_encoder = converter
        source._video_encoder = encoder
        source.b_frame_flush_data = (encoder, converter, 0, 0, 0, None)
        source.non_video_encodings = ()
        source.do_flush_video_encoder()
        self.assertEqual(converter.clean_count, 1)
        encoder.clean.assert_called_once_with()
        self.assertIsNone(source._csc_encoder)
        self.assertIsNone(source._video_encoder)

    def test_queued_flush_cannot_consume_replacement_payload(self) -> None:
        source = self.make_source()
        source.batch_config = SimpleNamespace(delay=20)
        encoder = Mock()
        encoder.is_closed.return_value = False
        encoder.get_type.return_value = "test"
        encoder.get_width.return_value = 64
        encoder.get_height.return_value = 64
        encoder.flush.return_value = None
        source._video_encoder = encoder
        work = []
        source.call_in_encode_thread = lambda fn, *args: work.append((fn, args))
        sources = BlockingSources()
        with patch.object(video_compress, "GLib", sources):
            source.schedule_video_encoder_flush(encoder, None, 1, 0, 0, None)
            _timer, callback, args = sources.timeout_calls[0]
            callback(*args)
            source.schedule_video_encoder_flush(encoder, None, 2, 0, 0, None)
            for fn, args in work:
                fn(*args)
            encoder.flush.assert_not_called()
            source.cancel_video_encoder_flush()

    def test_immediate_image_is_freed_when_connection_closes_before_encode(self) -> None:
        source = self.make_source()
        source.init_vars()
        source._damage_cancelled = 0
        image = Mock()
        image.get_width.return_value = image.get_height.return_value = 64
        image.is_thread_safe.return_value = True
        source.statistics = SimpleNamespace(encoding_pending={})
        source.make_data_packet = Mock(return_value=None)
        connection = make_encoding_connection()
        self.addCleanup(finish_encoding_connection, connection)
        connection.window_sources = {}
        source.call_in_encode_thread = connection.call_in_encode_thread
        release = Event()
        connection.call_in_encode_thread(release.wait, 2)
        try:
            now = monotonic()
            with patch.object(video_compress, "ALWAYS_FREEZE", False):
                source.do_process_damage_image(now, now, image, "rgb32", 1, typedict())
            source._video_source_closed = True
            source._damage_cancelled = 1
            connection.close()
        finally:
            release.set()
        worker = connection.encode_thread
        worker.join(2)
        self.assertFalse(worker.is_alive())
        image.free.assert_called_once_with()
        source.make_data_packet.assert_called_once()
        self.assertEqual(source.statistics.encoding_pending, {})

    @staticmethod
    def make_source() -> WindowVideoSource:
        source = object.__new__(WindowVideoSource)
        # Initialize the resource fields explicitly so the tests-only clean
        # control reaches the disputed lifecycle instead of depending on the
        # candidate's constructor helper.
        source._video_state_lock = RLock()
        source._video_refresh_lock = RLock()
        source._video_cleanup_queued = False
        source.video_fallback_refresh_idle = 0
        source._video_fallback_refresh_generation = 0
        source._video_source_closed = False
        source._video_stream_encoder = None
        source.wid = 7
        source.video_subregion = VideoSubregion(lambda _regions: True, 150, True)
        source.video_encoder_timer = 0
        source._video_encoder_timer_generation = 0
        source.b_frame_flush_timer = 0
        source._b_frame_flush_generation = 0
        source.b_frame_flush_data = ()
        source.encode_from_queue_timer = 0
        source.encode_from_queue_due = 0.0
        source._encode_from_queue_generation = 0
        source.scroll_data = None
        source._csc_encoder = None
        source._video_encoder = None
        source.video_stream_file = None
        source._damage_cancelled = 0
        source._sequence = 1
        source.encode_queue = []
        source.encode_queue_max_size = 10
        source.av_sync_timer = 0
        source.send_window_icon_timer = 0
        source.window_icon_queued = False
        source.queue_packet = lambda *_args: None
        return source

    @staticmethod
    def setup_pipeline(source, converter, encoder) -> bool:
        source.full_csc_modes = typedict({"h264": ("YUV420P",)})
        source.encoding_options = typedict()
        source.assign_sq_options = lambda options: options
        source._current_speed = 50
        source._current_quality = 50
        source.encoding = "h264"
        source.datagram = 0
        source.get_video_encoder_options = lambda *_args: {}
        csc_spec = SimpleNamespace(
            width_mask=0xFFFF,
            height_mask=0xFFFF,
            min_w=8,
            min_h=8,
            max_w=16384,
            max_h=16384,
            make_instance=lambda: converter,
        )
        encoder_spec = SimpleNamespace(
            encoding="h264",
            output_colorspaces=("YUV420P",),
            width_mask=0xFFFF,
            height_mask=0xFFFF,
            min_w=8,
            min_h=8,
            max_w=16384,
            max_h=16384,
            can_scale=False,
            full_range=False,
            make_instance=lambda: encoder,
        )
        return source.setup_pipeline_option(
            64, 64, "BGRX", 100, (1, 1), (1, 1), 64, 64, csc_spec,
            "YUV420P", (1, 1), 64, 64, encoder_spec,
        )

    def test_cleanup_sweeps_pipeline_published_by_setup(self) -> None:
        for has_old_pipeline in (False, True):
            with self.subTest(has_old_pipeline=has_old_pipeline):
                source = self.make_source()
                worker = EncodeWorker()
                source.call_in_encode_thread = worker.call
                old_csc = Converter(clean_error=has_old_pipeline)
                old_encoder = Encoder()
                if has_old_pipeline:
                    source._csc_encoder = old_csc
                    source._video_encoder = old_encoder
                init_started = Event()
                finish_init = Event()
                new_csc = Converter()
                new_encoder = Encoder(init_started, finish_init)
                worker.call(self.setup_pipeline, source, new_csc, new_encoder)
                try:
                    self.assertTrue(init_started.wait(2))
                    source.video_context_clean()
                    self.assertIsNone(source._csc_encoder)
                    self.assertIsNone(source._video_encoder)
                    finish_init.set()
                    worker.drain()

                    self.assertIsNone(source._csc_encoder)
                    self.assertIsNone(source._video_encoder)
                    self.assertEqual(new_csc.clean_count, 1)
                    self.assertEqual(new_encoder.clean_count, 1)
                    self.assertEqual(old_csc.clean_count, int(has_old_pipeline))
                    self.assertEqual(old_encoder.clean_count, int(has_old_pipeline))
                    self.assertEqual(len(worker.errors), int(has_old_pipeline))
                finally:
                    finish_init.set()
                    worker.close()

    def test_subregion_cleanup_error_does_not_bypass_base_cleanup(self) -> None:
        source = self.make_source()
        source.video_subregion = Mock()
        source.video_subregion.cleanup.side_effect = RuntimeError("subregion cleanup failed")

        with patch.object(video_compress.WindowSource, "cleanup",
                          side_effect=RuntimeError("base cleanup failed")) as base_cleanup, \
                self.assertRaisesRegex(RuntimeError, "subregion cleanup failed"):
            source.cleanup()

        self.assertTrue(source._video_source_closed)
        base_cleanup.assert_called_once_with()

    def test_damage_cancel_error_does_not_bypass_owned_cleanup(self) -> None:
        source = self.make_source()
        source.init_vars()
        source._damage_cancelled = 0
        source.statistics = SimpleNamespace(encoding_pending={})
        source.call_in_encode_thread = Mock()
        queued_image = Mock()
        queued_image.is_thread_safe.return_value = True
        source.encode_queue = [(64, 64, 0, 0, queued_image)]
        source.b_frame_flush_data = (Mock(), None, 1, 0, 0, None)
        source.scroll_data = ScrollOwner()
        source._csc_encoder = Converter()
        source._video_encoder = Encoder()
        removed = []

        def source_remove(source_id: int) -> None:
            removed.append(source_id)
            if source_id == 101:
                raise RuntimeError("timer removal failed")

        fake_glib = SimpleNamespace(source_remove=source_remove)
        source.encode_from_queue_timer = FakeSource(fake_glib, source_id=101)
        source.b_frame_flush_timer = FakeSource(fake_glib, source_id=103)
        source.video_encoder_timer = FakeSource(fake_glib, source_id=104)
        source.video_subregion.refresh_timer = FakeSource(fake_glib, source_id=105)
        source.video_subregion.nonvideo_refresh_timer = FakeSource(fake_glib, source_id=106)
        with patch.object(video_compress, "GLib", fake_glib), \
                patch.object(video_subregion, "GLib", fake_glib), \
                self.assertRaisesRegex(RuntimeError, "timer removal failed"):
            source.cancel_damage(99)

        self.assertEqual(removed, [101, 105, 106, 103, 104])
        self.assertEqual(source.encode_queue, [])
        queued_image.free.assert_called_once_with()
        self.assertEqual(source.video_subregion.refresh_timer, 0)
        self.assertEqual(source.video_subregion.nonvideo_refresh_timer, 0)
        self.assertEqual(source.b_frame_flush_timer, 0)
        self.assertEqual(source.b_frame_flush_data, ())
        self.assertEqual(source.video_encoder_timer, 0)
        self.assertIsNone(source._csc_encoder)
        self.assertIsNone(source._video_encoder)
        self.assertEqual(
            [call.args[0].__name__ for call in source.call_in_encode_thread.call_args_list],
            ["do_free_scroll_data", "clean"],
        )

    def test_failed_av_timer_removal_still_clears_reservation(self) -> None:
        source = self.make_source()
        removed = []

        def source_remove(source_id: int) -> None:
            removed.append(source_id)
            if source_id == 101:
                raise RuntimeError("timeout removal failed")

        registry = SimpleNamespace(source_remove=source_remove)
        source.encode_from_queue_timer = FakeSource(registry, source_id=101)
        source.encode_from_queue_due = monotonic() + 10
        with patch.object(video_compress, "GLib", registry), \
                self.assertRaisesRegex(RuntimeError, "timeout removal failed"):
            source.cancel_encode_from_queue()

        self.assertEqual(removed, [101])
        self.assertEqual(source.encode_from_queue_timer, 0)
        self.assertEqual(source.encode_from_queue_due, 0)

    def test_reinitialization_cleans_existing_pipeline(self) -> None:
        source = self.make_source()
        worker = EncodeWorker()
        source.call_in_encode_thread = worker.call
        source._mmap = True
        old_csc = Converter()
        old_encoder = Encoder()
        worker.call(self.setup_pipeline, source, old_csc, old_encoder)
        worker.drain()
        self.assertIs(source._csc_encoder, old_csc)
        self.assertIs(source._video_encoder, old_encoder)
        source._encoders = {}
        source.parse_csc_modes = Mock()
        source.update_encoding_selection = Mock()

        def base_init() -> None:
            self.assertIsNone(source._csc_encoder)
            self.assertIsNone(source._video_encoder)

        try:
            with patch.object(video_compress.WindowSource, "do_init_encoders", side_effect=base_init), \
                    patch.object(video_compress, "has_codec", return_value=False):
                source.init_encoders()
            worker.drain()

            source.parse_csc_modes.assert_called_once_with(None)
            source.update_encoding_selection.assert_called_once_with("h264", init=True)
            self.assertEqual(old_csc.clean_count, 1)
            self.assertEqual(old_encoder.clean_count, 1)
            self.assertEqual(worker.errors, [])
        finally:
            worker.close()

    def test_reinitialization_sweeps_pipeline_published_during_rebuild(self) -> None:
        source = self.make_source()
        worker = EncodeWorker()
        source.call_in_encode_thread = worker.call
        source._mmap = True
        source._encoders = {}
        source.parse_csc_modes = Mock()
        source.update_encoding_selection = Mock()
        init_started = Event()
        finish_init = Event()
        info_started = Event()
        finish_info = Event()
        base_init_started = Event()
        finish_base_init = Event()
        converter = Converter()
        encoder = Encoder(init_started, finish_init, info_started=info_started, finish_info=finish_info)
        reinit_errors = []

        def base_init() -> None:
            base_init_started.set()
            if not finish_base_init.wait(2):
                raise RuntimeError("registry rebuild was not released")

        def reinitialize() -> None:
            try:
                with patch.object(video_compress.WindowSource, "do_init_encoders", side_effect=base_init), \
                        patch.object(video_compress, "has_codec", return_value=False):
                    source.init_encoders()
            except BaseException as e:
                reinit_errors.append(e)

        worker.call(self.setup_pipeline, source, converter, encoder)
        reinit = Thread(target=reinitialize, name="test-reinitialize", daemon=True)
        try:
            self.assertTrue(init_started.wait(2))
            reinit.start()
            self.assertTrue(base_init_started.wait(2))
            finish_init.set()
            self.assertTrue(info_started.wait(2))
            self.assertIs(source._csc_encoder, converter)
            self.assertIs(source._video_encoder, encoder)
            finish_base_init.set()
            reinit.join(2)
            self.assertFalse(reinit.is_alive())
            self.assertEqual(reinit_errors, [])
            finish_info.set()
            worker.drain()

            self.assertEqual(converter.clean_count, 1)
            self.assertEqual(encoder.clean_count, 1)
            self.assertIsNone(source._csc_encoder)
            self.assertIsNone(source._video_encoder)
            self.assertEqual(worker.errors, [])
        finally:
            finish_init.set()
            finish_base_init.set()
            finish_info.set()
            reinit.join(2)
            worker.close()

    def test_terminal_setup_releases_pair_without_publication(self) -> None:
        source = self.make_source()
        worker = EncodeWorker()
        source.call_in_encode_thread = worker.call
        init_started = Event()
        finish_init = Event()
        converter = Converter()
        encoder = Encoder(init_started, finish_init)
        worker.call(self.setup_pipeline, source, converter, encoder)
        try:
            self.assertTrue(init_started.wait(2))
            with source._video_state_lock:
                source._video_source_closed = True
            finish_init.set()
            worker.drain()

            self.assertEqual(converter.clean_count, 1)
            self.assertEqual(encoder.clean_count, 1)
            self.assertIsNone(source._csc_encoder)
            self.assertIsNone(source._video_encoder)
            self.assertEqual(worker.errors, [])
        finally:
            finish_init.set()
            worker.close()

    def test_video_timer_callbacks_claim_before_publication(self) -> None:
        source = self.make_source()
        source.batch_config = SimpleNamespace(delay=20)
        source.call_in_encode_thread = Mock()
        flush_sources = BlockingSources(dispatch_timeout=(1,))
        with patch.object(video_compress, "GLib", flush_sources):
            source.schedule_video_encoder_flush(object(), None, 1, 0, 0, None)

        flush_id, _callback, _args = flush_sources.timeout_calls[0]
        self.assertEqual(flush_sources.callback_results, [False])
        self.assertEqual(flush_sources.removed, [flush_id])
        self.assertEqual(source.b_frame_flush_timer, 0)
        source.call_in_encode_thread.assert_called_once()
        callback, generation, payload = source.call_in_encode_thread.call_args.args
        self.assertEqual(callback, source.do_flush_video_encoder)
        self.assertGreater(generation, 0)
        self.assertEqual(payload[2], 1)

        source = self.make_source()
        source.video_context_clean = Mock()
        timeout_sources = BlockingSources(dispatch_timeout=(1,))
        with patch.object(video_compress, "GLib", timeout_sources), \
                patch.object(video_compress, "VIDEO_TIMEOUT", 1):
            source.schedule_video_encoder_timer()

        timeout_id, _callback, _args = timeout_sources.timeout_calls[0]
        self.assertEqual(timeout_sources.callback_results, [False])
        self.assertEqual(timeout_sources.removed, [timeout_id])
        self.assertEqual(source.video_encoder_timer, 0)
        source.video_context_clean.assert_called_once_with()

    def test_invalid_pipeline_cleanup_attempts_both_elements(self) -> None:
        source = self.make_source()
        csc = Converter(clean_error=True)
        encoder = Encoder()
        source._csc_encoder = csc
        source._video_encoder = encoder
        source.do_check_pipeline = Mock(return_value=False)
        source.get_video_pipeline_options = Mock()
        packets = []
        source.queue_packet = packets.append

        with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
            source.check_pipeline(("h264",), 64, 64, "BGRX")

        self.assertEqual(csc.clean_count, 1)
        self.assertEqual(encoder.clean_count, 1)
        self.assertIsNone(source._csc_encoder)
        self.assertIsNone(source._video_encoder)
        self.assertEqual(packets, [(WINDOW_EOS, source.wid)])
        source.get_video_pipeline_options.assert_not_called()

    def test_failed_option_cleanup_attempts_both_elements(self) -> None:
        source = self.make_source()
        csc = Converter(clean_error=True)
        encoder = Encoder()
        source.is_cancelled = Mock(return_value=False)

        def fail_option(*_args) -> None:
            source._csc_encoder = csc
            source._video_encoder = encoder
            raise RuntimeError("option failed")

        source.setup_pipeline_option = fail_option
        packets = []
        source.queue_packet = packets.append

        with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
            source.setup_pipeline(((100,),), 64, 64, "BGRX")

        self.assertEqual(csc.clean_count, 1)
        self.assertEqual(encoder.clean_count, 1)
        self.assertIsNone(source._csc_encoder)
        self.assertIsNone(source._video_encoder)
        self.assertEqual(packets, [(WINDOW_EOS, source.wid)])

    def test_failed_initialization_cleans_unpublished_instances(self) -> None:
        for converter, encoder in (
            (Converter(init_error=True), Encoder()),
            (Converter(), Encoder(init_error=True)),
            (Converter(clean_error=True), Encoder(init_error=True)),
        ):
            with self.subTest(converter=converter, encoder=encoder):
                source = self.make_source()
                expected_error = "cleanup failed" if converter.clean_error else "init failed"
                with self.assertRaisesRegex(RuntimeError, expected_error):
                    self.setup_pipeline(source, converter, encoder)

                self.assertEqual(converter.clean_count, 1)
                self.assertEqual(encoder.clean_count, int(not converter.init_error))
                self.assertIsNone(source._csc_encoder)
                self.assertIsNone(source._video_encoder)

    def test_stream_file_closes_when_encoder_cleanup_fails(self) -> None:
        source = self.make_source()
        encoder = Encoder(clean_error=True)
        stream_file = Mock()
        source.video_stream_file = stream_file
        source._video_stream_encoder = encoder

        with patch.object(video_compress, "SAVE_VIDEO_STREAMS", True):
            with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                source.ve_clean(encoder)

        self.assertIsNone(source.video_stream_file)
        stream_file.close.assert_called_once_with()

    def test_full_cleanup_cancels_timer_and_queues_one_barrier(self) -> None:
        source = self.make_source()
        source.init_vars()
        source.av_sync_timer = 0
        queued_image = Mock()
        queued_image.is_thread_safe.return_value = True
        source.encode_queue = [(64, 64, 0, 0, queued_image)]
        source.encode_queue_max_size = 10
        source._mmap = None
        source.statistics = SimpleNamespace(encoding_totals={}, encoding_pending={})
        batch_cleaned = []
        source.batch_config = SimpleNamespace(cleanup=lambda: batch_cleaned.append(True))
        source.queue_packet = lambda *_args: None
        worker = EncodeWorker()
        source.call_in_encode_thread = worker.call
        old_csc = Converter()
        old_encoder = Encoder()
        worker.call(self.setup_pipeline, source, old_csc, old_encoder)
        worker.drain()
        self.assertIs(source._csc_encoder, old_csc)
        self.assertIs(source._video_encoder, old_encoder)
        worker_started = Event()
        finish_work = Event()

        def hold_worker() -> None:
            worker_started.set()
            if not finish_work.wait(2):
                raise RuntimeError("encode worker was not released")

        worker.call(hold_worker)
        self.assertTrue(worker_started.wait(2))
        removed_sources = []
        idle_callbacks = []
        fake_glib = SimpleNamespace(
            timeout_add=lambda _delay, _callback, *_args: 101,
            source_remove=removed_sources.append,
            idle_add=lambda callback: idle_callbacks.append(callback),
        )
        fake_glib.timeout_source_new = lambda delay: FakeSource(fake_glib, delay)
        try:
            with patch.object(video_compress, "GLib", fake_glib), \
                    patch.object(compress, "GLib", fake_glib):
                source.schedule_video_encoder_timer()
                self.assertEqual(source.video_encoder_timer.get_id(), 101)
                source.cleanup()
                self.assertEqual(removed_sources, [101])
                self.assertEqual(source.encode_queue, [])
                queued_image.free.assert_called_once_with()
                self.assertEqual(old_csc.clean_count, 0)
                self.assertEqual(old_encoder.clean_count, 0)
                finish_work.set()
                worker.drain()

            self.assertEqual(batch_cleaned, [True])
            self.assertEqual(old_csc.clean_count, 1)
            self.assertEqual(old_encoder.clean_count, 1)
            self.assertIsNone(source._csc_encoder)
            self.assertIsNone(source._video_encoder)
            self.assertEqual(worker.errors, [])
            self.assertEqual(len(idle_callbacks), 1)
            self.assertEqual(
                [callback.__name__ for callback in worker.callbacks],
                ["setup_pipeline", "hold_worker", "do_free_scroll_data", "clean", "encode_ended"],
            )
        finally:
            finish_work.set()
            worker.close()

    def test_full_cleanup_preserves_resources_for_worker_sweep(self) -> None:
        source = self.make_source()
        source.init_vars()
        source._mmap = None
        source.statistics = SimpleNamespace(encoding_totals={}, encoding_pending={})
        source.batch_config = SimpleNamespace(cleanup=lambda: None)
        source.queue_packet = lambda *_args: None
        source.scroll_data = scroll_data = ScrollOwner()
        worker = EncodeWorker()
        source.call_in_encode_thread = worker.call
        old_csc = Converter()
        old_encoder = Encoder()
        worker.call(self.setup_pipeline, source, old_csc, old_encoder)
        worker.drain()
        new_csc = Converter()
        new_encoder = Encoder()
        publish_started = Event()
        publish_resources = Event()

        def publish_after_cleanup() -> None:
            publish_started.set()
            if not publish_resources.wait(2):
                raise RuntimeError("resource publication was not released")
            source._csc_encoder = new_csc
            source._video_encoder = new_encoder
            source.b_frame_flush_data = (new_encoder, new_csc, 1, 0, 0, None)
            source.b_frame_flush_timer = FakeSource(fake_glib, source_id=202)
            source.video_encoder_timer = FakeSource(fake_glib, source_id=203)

        worker.call(publish_after_cleanup)
        self.assertTrue(publish_started.wait(2))
        removed_sources = []
        idle_callbacks = []
        fake_glib = SimpleNamespace(
            source_remove=removed_sources.append,
            idle_add=lambda callback: idle_callbacks.append(callback),
        )
        try:
            with patch.object(video_compress, "GLib", fake_glib), \
                    patch.object(compress, "GLib", fake_glib):
                source.cleanup()
                self.assertEqual(scroll_data.free_count, 0)
                self.assertEqual(old_csc.clean_count, 0)
                self.assertEqual(old_encoder.clean_count, 0)
                publish_resources.set()
                worker.drain()

            self.assertEqual(scroll_data.free_count, 1)
            self.assertEqual(old_csc.clean_count, 1)
            self.assertEqual(old_encoder.clean_count, 1)
            self.assertEqual(new_csc.clean_count, 1)
            self.assertEqual(new_encoder.clean_count, 1)
            self.assertEqual(set(removed_sources), {202, 203})
            self.assertEqual(source.b_frame_flush_timer, 0)
            self.assertEqual(source.b_frame_flush_data, ())
            self.assertEqual(source.video_encoder_timer, 0)
            self.assertIsNone(source.scroll_data)
            self.assertIsNone(source._csc_encoder)
            self.assertIsNone(source._video_encoder)
            self.assertEqual(worker.errors, [])
            self.assertEqual(len(idle_callbacks), 1)
            self.assertEqual(
                [callback.__name__ for callback in worker.callbacks],
                ["setup_pipeline", "publish_after_cleanup", "do_free_scroll_data", "clean", "encode_ended"],
            )
        finally:
            publish_resources.set()
            worker.close()

    def make_av_source(self):
        source = self.make_source()
        source.init_vars()
        source._damage_cancelled = 0
        source.av_sync_delay = 0
        source.update_av_sync_delay = Mock()
        source.call_in_encode_thread = Mock()
        return source

    @staticmethod
    def make_image(width=64, height=64):
        return ImageWrapper(0, 0, width, height, bytes(width * height * 4),
                            "RGBX", 24, width * 4)

    def test_av_sync_update_does_not_hold_video_state_lock(self) -> None:
        source = self.make_av_source()
        state_lock = Lock()
        source._video_state_lock = state_lock
        image = self.make_image()
        source.encode_queue = [
            (64, 64, 0.0, 0.0, image, "h264", 1, typedict(), 0),
        ]
        lock_was_available = []

        def update_av_sync_delay() -> None:
            acquired = state_lock.acquire(blocking=False)
            lock_was_available.append(acquired)
            if acquired:
                state_lock.release()

        source.update_av_sync_delay = update_av_sync_delay
        source.encode_from_queue()

        self.assertEqual(lock_was_available, [True])
        source.call_in_encode_thread.assert_called_once()
        self.assertIs(source.call_in_encode_thread.call_args.args[0].__func__,
                      source.make_data_packet_cb.__func__)
        self.assertEqual(source.encode_queue, [])
        self.assertFalse(image.freed)
        image.free()  # the recorded worker submission owns this image

    def test_real_av_timeout_drains_on_ui_and_hands_only_images_to_worker(self) -> None:
        source = self.make_av_source()
        image = self.make_image()
        item = (64, 64, 0.0, 0.0, image, "h264", 1, typedict(), 0)
        source.encode_queue = [item]
        sources = EarlyRealGLib(video_compress.GLib)
        with patch.object(video_compress, "GLib", sources):
            source.schedule_encode_from_queue(25)
        self.assertEqual(source.encode_from_queue_timer, 0)
        self.assertEqual(source.encode_from_queue_due, 0)
        self.assertEqual(source.encode_queue, [])
        source.call_in_encode_thread.assert_called_once_with(source.make_data_packet_cb, *item)
        self.assertEqual(sources.remove_warnings, [])
        self.assertFalse(image.freed)
        image.free()

    def test_cancelled_av_callback_cannot_revoke_replacement(self) -> None:
        source = self.make_av_source()
        source.encode_from_queue = Mock()
        sources = BlockingSources()
        with patch.object(video_compress, "GLib", sources):
            source.schedule_encode_from_queue(25)
            _old_id, callback, args = sources.timeout_calls[0]
            source.cancel_encode_from_queue()
            source.schedule_encode_from_queue(10)
            replacement = source.encode_from_queue_timer
            due = source.encode_from_queue_due
            self.assertFalse(callback(*args))
            self.assertIs(source.encode_from_queue_timer, replacement)
            self.assertEqual(source.encode_from_queue_due, due)
            source.encode_from_queue.assert_not_called()
            source._video_source_closed = True
            source.cancel_encode_from_queue()
            _new_id, callback, args = sources.timeout_calls[1]
            self.assertFalse(callback(*args))
            source.schedule_encode_from_queue(1)
        self.assertEqual(source.encode_from_queue_timer, 0)
        self.assertEqual(source.encode_from_queue_due, 0)
        self.assertEqual(len(sources.timeout_calls), 2)
        source.encode_from_queue.assert_not_called()

    def test_failed_av_publication_rolls_back_image_and_due_reservation(self) -> None:
        for phase in ("construct", "callback", "attach"):
            with self.subTest(phase=phase):
                source = self.make_av_source()
                image = self.make_image()
                sources = BlockingSources()
                timer = FakeSource(sources, 25)

                def fail(*_args):
                    raise RuntimeError("AV publication failed")

                if phase == "construct":
                    sources.timeout_source_new = fail
                else:
                    sources.timeout_source_new = lambda _delay: timer
                    if phase == "callback":
                        timer.set_callback = fail
                    else:
                        timer.attach = fail
                now = monotonic()
                with patch.object(video_compress, "GLib", sources), \
                        self.assertRaisesRegex(RuntimeError, "AV publication failed"):
                    source.do_process_damage_image(now, now, image, "rgb32", 1,
                                                   typedict({"av-delay": 1000}))
                self.assertEqual(source.encode_queue, [])
                self.assertEqual(source.encode_from_queue_timer, 0)
                self.assertEqual(source.encode_from_queue_due, 0)
                self.assertFalse(image.freed)  # rejection leaves the original with its caller
                source.call_in_encode_thread.assert_not_called()
                if phase != "construct":
                    self.assertTrue(timer.destroyed)
                sources = BlockingSources()
                with patch.object(video_compress, "GLib", sources):
                    source.do_process_damage_image(now, now, image, "rgb32", 1,
                                                   typedict({"av-delay": 1000}))
                    self.assertEqual(len(source.encode_queue), 1)
                    self.assertEqual(len(sources.timeout_calls), 1)
                    source.cancel_encode_from_queue()
                    source.free_encode_queue_images()
                self.assertTrue(image.freed)

    def test_accepted_av_image_is_removed_before_later_free_failure(self) -> None:
        source = self.make_av_source()
        source._damage_cancelled = 1
        accepted = self.make_image()
        rejected = Mock()
        rejected.is_thread_safe.return_value = True
        rejected.free.side_effect = RuntimeError("image free failed")
        remaining = self.make_image()
        source.encode_queue = [
            (64, 64, 0, 0, accepted, "h264", 2, typedict(), 0),
            (64, 64, 0, 0, rejected, "h264", 1, typedict(), 0),
            (64, 64, 0, 0, remaining, "h264", 3, typedict(), 0),
        ]
        with self.assertRaisesRegex(RuntimeError, "image free failed"):
            source.encode_from_queue()
        self.assertEqual([item[4] for item in source.encode_queue], [remaining])
        self.assertFalse(accepted.freed)
        rejected.free.assert_called_once_with()
        source.free_encode_queue_images()
        self.assertTrue(remaining.freed)
        rejected.free.assert_called_once_with()
        self.assertFalse(accepted.freed)
        accepted.free()

    def test_accepted_av_image_stays_worker_owned_when_reschedule_fails(self) -> None:
        source = self.make_av_source()
        first, second = self.make_image(), self.make_image()
        source.encode_queue = [
            (64, 64, 0, 0, first, "h264", 1, typedict(), 0),
            (64, 64, 0, 0, second, "h264", 2, typedict(), 0),
        ]
        source.schedule_encode_from_queue = Mock(side_effect=RuntimeError("reschedule failed"))
        with self.assertRaisesRegex(RuntimeError, "reschedule failed"):
            source.encode_from_queue()
        self.assertEqual([item[4] for item in source.encode_queue], [second])
        source.call_in_encode_thread.assert_called_once()
        source.free_encode_queue_images()
        self.assertTrue(second.freed)
        self.assertFalse(first.freed)
        first.free()

    def test_failed_region_handoff_releases_only_unaccepted_subimages(self) -> None:
        for accepted_count in (0, 1, 2):
            with self.subTest(accepted_count=accepted_count):
                source = self.make_av_source()
                source.common_video_encodings = ("h264",)
                source.edge_encoding = "rgb32"
                source.width_mask = source.height_mask = 0xFFFE
                image = self.make_image(65, 65)
                derived = []
                get_sub_image = image.get_sub_image

                def extract(*args):
                    sub = get_sub_image(*args)
                    derived.append(sub)
                    return sub

                image.get_sub_image = extract
                source.call_in_encode_thread.side_effect = (
                    [None] * accepted_count + [RuntimeError("handoff rejected")]
                )
                now = monotonic()
                with patch.object(video_compress, "ALWAYS_FREEZE", False), \
                        self.assertRaisesRegex(RuntimeError, "handoff rejected"):
                    source.do_process_damage_image(now, now, image, "h264", 1, typedict())
                self.assertEqual(len(derived), 2)
                self.assertFalse(image.freed)
                self.assertEqual([sub.freed for sub in derived],
                                 [index >= accepted_count for index in range(2)])
                for sub in derived[:accepted_count]:
                    sub.free()  # accepted worker owners, not frame-fanout cleanup
                image.free()

    def test_failed_subimage_extraction_releases_earlier_unaccepted_edge(self) -> None:
        source = self.make_av_source()
        source.common_video_encodings = ("h264",)
        source.edge_encoding = "rgb32"
        source.width_mask = source.height_mask = 0xFFFE
        image = self.make_image(65, 65)
        edge = image.get_sub_image(64, 0, 1, 65)
        image.get_sub_image = Mock(side_effect=(edge, RuntimeError("extract failed")))
        now = monotonic()
        with patch.object(video_compress, "ALWAYS_FREEZE", False), \
                self.assertRaisesRegex(RuntimeError, "extract failed"):
            source.do_process_damage_image(now, now, image, "h264", 1, typedict())
        self.assertTrue(edge.freed)
        self.assertFalse(image.freed)
        source.call_in_encode_thread.assert_not_called()
        image.free()

    def test_odd_video_fanout_preserves_every_pixel_once(self) -> None:
        for width, height in ((65, 64), (64, 65), (65, 65), (1, 65), (65, 1), (1, 1)):
            with self.subTest(size=(width, height)):
                source = self.make_av_source()
                source.common_video_encodings = ("h264",)
                source.edge_encoding = "rgb32"
                source.width_mask = source.height_mask = 0xFFFE
                pixels = bytes(v for y in range(height) for x in range(width) for v in (x, y, x ^ y, 255))
                image = ImageWrapper(17, 23, width, height, pixels, "RGBX", 24, width * 4)
                self.addCleanup(image.free)
                now = monotonic()
                with patch.object(video_compress, "ALWAYS_FREEZE", False):
                    source.do_process_damage_image(now, now, image, "h264", 1, typedict())
                calls = source.call_in_encode_thread.call_args_list
                self.assertTrue(calls)
                covered = set()
                for index, handoff in enumerate(calls):
                    rw, rh, _, _, region, coding, _, _, flush = handoff.args[1:]
                    self.addCleanup(region.free)
                    self.assertFalse(region.freed)
                    self.assertEqual(flush, len(calls) - index - 1)
                    rx, ry = region.get_target_x() - 17, region.get_target_y() - 23
                    if coding == "h264":
                        self.assertIs(region, image)
                        self.assertEqual((rw, rh), (width & 0xFFFE, height & 0xFFFE))
                        self.assertEqual(index, len(calls) - 1)
                    else:
                        self.assertEqual(coding, "rgb32")
                        self.assertEqual((rw, rh), (region.get_width(), region.get_height()))
                    data = region.get_pixels()
                    stride = region.get_rowstride()
                    for y in range(rh):
                        for x in range(rw):
                            point = rx + x, ry + y
                            self.assertNotIn(point, covered)
                            covered.add(point)
                            pos = y * stride + x * 4
                            sx, sy = point
                            self.assertEqual(bytes(data[pos:pos + 4]), bytes((sx, sy, sx ^ sy, 255)))
                self.assertEqual(covered, {(x, y) for y in range(height) for x in range(width)})


def load_tests(_loader, tests, _pattern):
    # A real GLib callback through add_video_refresh() establishes the clean
    # behavioral control before tests of the new private ownership machinery.
    control = VideoSubregionLifecycleTest("test_real_glib_early_refresh_dispatch_owns_exact_source")
    ordered = unittest.TestSuite([control])
    for group in tests:
        ordered.addTests(test for test in group if test.id() != control.id())
    return ordered


def main() -> None:
    unittest.main(failfast=True)


if __name__ == "__main__":
    main()
