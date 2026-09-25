#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import unittest
import warnings
from threading import Condition, Event, Lock, Thread
from types import SimpleNamespace
from unittest.mock import Mock, patch

from xpra.net.common import Packet
from xpra.server.source import factory
from xpra.server.source.client_connection import ClientConnection
from xpra.server.source.window import WindowsConnection
from xpra.server.window import compress, video_compress, windowicon
from xpra.server.window.compress import DelayedRegions, WindowSource
from xpra.server.window.video_compress import WindowVideoSource
from xpra.util.rectangle import rectangle
from xpra.util.objects import typedict


class FakeSource:

    def __init__(self, glib, delay) -> None:
        self.glib = glib
        self.delay = delay
        self.callback = None
        self.source_id = 0
        self.destroyed = False

    def set_callback(self, callback) -> None:
        self.callback = callback

    def attach(self, _context) -> int:
        self.source_id = self.glib.timeout_add(self.delay, self.callback)
        return self.source_id

    def destroy(self) -> None:
        if not self.destroyed:
            self.destroyed = True
            if self.source_id:
                self.glib.source_remove(self.source_id)


class FakeGLib:

    def __init__(self) -> None:
        self.lock = Lock()
        self.next_id = 1
        self.callbacks = {}
        self.removed = []

    def register(self, callback, args) -> int:
        with self.lock:
            source_id = self.next_id
            self.next_id += 1
            self.callbacks[source_id] = lambda: callback(*args)
        return source_id

    def timeout_add(self, _delay, callback, *args) -> int:
        return self.register(callback, args)

    def timeout_source_new(self, delay):
        return FakeSource(self, delay)

    def source_remove(self, source_id: int) -> bool:
        if source_id <= 0:
            raise ValueError(f"invalid GLib source id {source_id}")
        with self.lock:
            self.removed.append(source_id)
            return self.callbacks.pop(source_id, None) is not None

    def fire(self, source_id: int):
        with self.lock:
            callback = self.callbacks[source_id]
        try:
            keep = callback()
        except BaseException:
            with self.lock:
                self.callbacks.pop(source_id, None)
            raise
        if not keep:
            with self.lock:
                self.callbacks.pop(source_id, None)
        return keep


class BlockingGLib(FakeGLib):

    def __init__(self) -> None:
        super().__init__()
        self.timeout_started = Event()
        self.release_timeout = Event()

    def timeout_add(self, _delay, callback, *args) -> int:
        source_id = self.register(callback, args)
        if source_id == 1:
            self.timeout_started.set()
            if not self.release_timeout.wait(2):
                raise RuntimeError("timer publication was not released")
        return source_id


class FailingRemoveGLib(FakeGLib):

    def source_remove(self, source_id: int) -> bool:
        removed = super().source_remove(source_id)
        if source_id == 1:
            raise RuntimeError("cannot remove first timer")
        return removed


class ObservedCondition(Condition):

    def __init__(self, lock, waiter_started: Event) -> None:
        super().__init__(lock)
        self.waiter_started = waiter_started

    def wait(self, timeout: float | None = None) -> bool:
        self.waiter_started.set()
        return super().wait(timeout)


class WaitInterrupted(BaseException):
    pass


class CallbackInterrupted(BaseException):
    pass


class DeferredCleanupInterrupted(BaseException):
    pass


class InterruptingCondition(Condition):

    def __init__(self, lock, interrupted: Event) -> None:
        super().__init__(lock)
        self.interrupted = interrupted
        self.interrupt_next_wait = True

    def wait(self, timeout: float | None = None) -> bool:
        if self.interrupt_next_wait:
            self.interrupt_next_wait = False
            self.interrupted.set()
            raise WaitInterrupted("timer callback wait interrupted")
        return super().wait(timeout)


class CompressTest(unittest.TestCase):

    @staticmethod
    def make_source(content_types=()) -> WindowSource:
        source = object.__new__(WindowSource)
        source._fixed_speed = -1
        source._fixed_min_speed = 0
        source._fixed_max_speed = 100
        source._current_speed = 50
        source._fixed_quality = -1
        source._quality_hint = -1
        source._fixed_min_quality = 0
        source._fixed_max_quality = 100
        source._current_quality = 80
        source._lossless_threshold_base = 70
        source.statistics = SimpleNamespace(last_packet_time=100)
        source.get_packets_backlog = lambda: 0
        source.content_types = content_types
        source.rgb_formats = ("RGB",)
        source.rgb_lz4 = False
        source.rgb_zstd = False
        source.encoding = "auto"
        source.supports_transparency = True
        source.image_depth = 24
        source._want_alpha = False
        source.is_tray = False
        source._rgb_auto_threshold = 0
        source.has_shape = False
        source.client_bit_depth = 24
        return source

    @staticmethod
    def make_window(has_alpha=True, frame_has_alpha=True, frame_property=True):
        """ a window model exposing `frame-has-alpha` the way the wayland models do """
        return SimpleNamespace(
            has_alpha=lambda: has_alpha,
            get_internal_property_names=lambda: ["frame-has-alpha"] if frame_property else [],
            get_property=lambda name: {"frame-has-alpha": frame_has_alpha}[name],
        )

    def make_alpha_source(self, **window_kwargs) -> WindowSource:
        source = self.make_source()
        source.window = self.make_window(**window_kwargs)
        source.is_OR = False
        source.window_type = set()
        source.window_dimensions = 800, 600
        return source

    def test_an_opaque_buffer_gives_up_the_alpha_channel(self) -> None:
        for has_alpha, frame_has_alpha, expected in (
                (True, True, True),
                (True, False, False),
                # the frame can only narrow the capability, never widen it:
                (False, True, False),
                (False, False, False),
        ):
            with self.subTest(has_alpha=has_alpha, frame_has_alpha=frame_has_alpha):
                source = self.make_alpha_source(has_alpha=has_alpha, frame_has_alpha=frame_has_alpha)
                source.update_has_alpha()
                self.assertEqual(source.has_alpha, expected)

    def test_a_window_without_frame_alpha_keeps_its_capability(self) -> None:
        # ie: an x11 window, whose alpha is decided once by its depth
        source = self.make_alpha_source(has_alpha=True, frame_has_alpha=False, frame_property=False)
        source.update_has_alpha()
        self.assertTrue(source.has_alpha)

    def test_wanting_alpha_hides_the_video_selection(self) -> None:
        # `get_transparent_encoding` only ever returns a `TRANSPARENCY_ENCODINGS` value,
        # and it is returned before `get_best_encoding_impl_default` - the one method
        # `WindowVideoSource` overrides to offer video - can be reached at all:
        source = self.make_source()
        source._encoding_hint = ""
        source._encoders = {}
        source._mmap = None
        source.strict = False
        source.common_encodings = ("rgb24", "rgb32", "png", "webp", "h264")
        source._want_alpha = True
        self.assertEqual(source.get_best_encoding_impl(), source.get_transparent_encoding)
        source._want_alpha = False
        self.assertEqual(source.get_best_encoding_impl(), source.get_auto_encoding)

    @staticmethod
    def make_cancellable_source() -> WindowSource:
        source = object.__new__(WindowSource)
        source.wid = 1
        source._sequence = 5
        source._damage_cancelled = 0
        source._damage_delayed = None
        source.encode_queue = []
        source.refresh_regions = []
        source.refresh_event_time = 0
        for timer in ("expire_timer", "may_send_timer", "soft_timer", "refresh_timer",
                      "timeout_timer", "av_sync_timer", "decode_error_refresh_timer"):
            setattr(source, timer, 0)
        source.statistics = SimpleNamespace(encoding_pending={})
        source.window = Mock()
        return source

    def test_dropping_a_delayed_region_acknowledges_it(self) -> None:
        # nothing else will: `send_delayed_regions` is never going to run for it,
        # and a wayland client throttles its rendering on that acknowledgement
        source = self.make_cancellable_source()
        source._damage_delayed = "some delayed regions"

        source.cancel_damage()

        self.assertIsNone(source._damage_delayed)
        source.window.acknowledge_changes.assert_called_once_with()

    def test_cancelling_without_a_delayed_region_acknowledges_nothing(self) -> None:
        # anything already extracted was acknowledged before it was extracted,
        # so there is nothing outstanding to answer for here
        source = self.make_cancellable_source()

        source.cancel_damage()

        source.window.acknowledge_changes.assert_not_called()

    @patch.object(compress, "monotonic", return_value=100)
    def test_automatic_screen_quality_is_promoted(self, _monotonic) -> None:
        for content_types in ((), ("browser",), ("desktop",)):
            with self.subTest(content_types=content_types):
                source = self.make_source(content_types)
                options = {}
                assigned = source.assign_sq_options(options)
                self.assertEqual(assigned["quality"], 100)
                self.assertNotIn("quality", options)

    @patch.object(compress, "monotonic", return_value=100)
    def test_natural_content_quality_is_not_promoted(self, _monotonic) -> None:
        for content_types in (("video",), ("picture",)):
            with self.subTest(content_types=content_types):
                source = self.make_source(content_types)
                assigned = source.assign_sq_options({})
                self.assertEqual(assigned["quality"], 80)

    @patch.object(compress, "monotonic", return_value=100)
    def test_explicit_or_fixed_quality_is_not_promoted(self, _monotonic) -> None:
        source = self.make_source(("browser",))
        assigned = source.assign_sq_options({"quality": 80})
        self.assertEqual(assigned["quality"], 80)

        source._fixed_quality = 80
        assigned = source.assign_sq_options({})
        self.assertEqual(assigned["quality"], 80)

        source._fixed_quality = -1
        source._quality_hint = 80
        assigned = source.assign_sq_options({})
        self.assertEqual(assigned["quality"], 80)

    @patch.object(compress, "monotonic", return_value=100)
    def test_auto_encoding_uses_assigned_quality(self, _monotonic) -> None:
        encodings = ("jpeg", "webp")
        source = self.make_source(("browser",))
        options = source.assign_sq_options({})
        self.assertEqual(options["quality"], 100)
        self.assertEqual(source.do_get_auto_encoding(1024, 1024, options, "", encodings), "webp")

        options = source.assign_sq_options({"quality": 80})
        self.assertEqual(options["quality"], 80)
        self.assertEqual(source.do_get_auto_encoding(1024, 1024, options, "", encodings), "jpeg")

    @patch.object(compress, "TRUE_LOSSLESS", False)
    def test_lossless_quality_never_uses_jpeg(self) -> None:
        source = self.make_source()
        options = {"quality": 100, "speed": 50}
        self.assertEqual(source.do_get_auto_encoding(1024, 1024, options, "", ("jpeg", "png")), "png")

        source.supports_transparency = True
        source.common_encodings = ("jpega", "png")
        self.assertEqual(source.get_transparent_encoding(1024, 1024, options, "auto"), "png")

    def test_continuous_tone_encoding(self) -> None:
        encodings = ("jpeg", "webp", "jph")
        cases = (
            (("picture",), 30, 20, False, 640, 360, "jph"),
            (("picture",), 50, 20, False, 640, 360, "jph"),
            (("picture",), 51, 20, False, 640, 360, "webp"),
            (("picture",), 50, 50, False, 640, 360, "webp"),
            (("video",), 30, 20, False, 640, 360, "jph"),
            (("browser",), 30, 20, False, 640, 360, "webp"),
            ((), 30, 20, False, 640, 360, "webp"),
            (("picture",), 30, 20, True, 640, 360, "webp"),
            (("browser",), 30, 20, False, 1024, 1024, "jpeg"),
            (("picture",), 100, 20, False, 640, 360, "webp"),
        )
        for content_types, quality, speed, alpha, width, height, expected in cases:
            with self.subTest(
                content_types=content_types, quality=quality, speed=speed,
                alpha=alpha, size=(width, height),
            ):
                source = self.make_source(content_types)
                source._want_alpha = alpha
                options = {"quality": quality, "speed": speed}
                encoding = source.do_get_auto_encoding(width, height, options, "", encodings)
                self.assertEqual(encoding, expected)

    @patch.object(compress, "TRUE_LOSSLESS", False)
    def test_transparent_lossless_webp_ignores_size_cutoff(self) -> None:
        source = self.make_source(("browser",))
        source.common_encodings = ("webp", "jpega")
        size = (1024, 1024)
        self.assertEqual(source.get_transparent_encoding(*size, {"quality": 100}, "auto"), "webp")
        self.assertEqual(source.get_transparent_encoding(*size, {"quality": 80}, "auto"), "jpega")


class WindowSourceTimerLifecycleTest(unittest.TestCase):

    @staticmethod
    def make_source() -> WindowSource:
        source = object.__new__(WindowSource)
        source.init_vars()
        # The real constructor clears this only after the source is ready.
        # init_vars() alone deliberately leaves all damage cancelled.
        source._damage_cancelled = 0
        source.wid = 7
        source.encoding = "rgb"
        source.statistics = SimpleNamespace(
            encoding_totals={},
            encoding_pending={},
            damage_events_count=0,
        )
        source.global_statistics = None
        source.encode_queue = []
        source._mmap = None
        source.batch_config = SimpleNamespace(
            cleanup=Mock(),
            min_delay=1,
            expire_delay=100,
            timeout_delay=1000,
            max_delay=1000,
        )
        source.call_in_encode_thread = Mock(return_value=None)
        # These slots are initialized by the real constructors, outside
        # init_vars(), in the clean embedded source. Cleanup acknowledges a
        # dropped delayed region through `window` (upstream 9eb00d12ca).
        source.window = Mock()
        source.send_window_icon_timer = 0
        source.window_icon_queued = False
        source.av_sync = True
        source.av_sync_timer = 0
        source.av_sync_delay = 0
        source.av_sync_delay_target = 100
        return source

    @staticmethod
    def run_thread(callback, name: str):
        errors = []

        def run() -> None:
            try:
                callback()
            except BaseException as e:
                errors.append(e)

        thread = Thread(target=run, name=name, daemon=True)
        thread.start()
        return thread, errors

    def test_real_glib_cancelled_dispatch_preserves_exact_source_ownership(self) -> None:
        real_glib = compress.GLib
        context = real_glib.MainContext.default()
        for phase in ("pending", "published"):
            with self.subTest(phase=phase):
                source = self.make_source()
                paused, release = Event(), Event()
                remove_warnings = []
                callback = Mock()

                def pause() -> None:
                    paused.set()
                    if not release.wait(2):
                        raise RuntimeError("real GLib ownership handoff was not released")

                class OwnedSource:
                    def __init__(self, wrapped):
                        self.wrapped = wrapped

                    def set_callback(self, function):
                        self.wrapped.set_callback(function)

                    def attach(self, main_context):
                        source_id = self.wrapped.attach(main_context)
                        if phase == "pending":
                            pause()
                        return source_id

                    def destroy(self):
                        if phase == "published":
                            pause()
                        self.wrapped.destroy()

                class ObservedGLib:
                    @staticmethod
                    def timeout_source_new(delay):
                        return OwnedSource(real_glib.timeout_source_new(delay))

                    @staticmethod
                    def timeout_add(delay, function, *args):
                        source_id = real_glib.timeout_add(delay, function, *args)
                        if phase == "pending":
                            pause()
                        return source_id

                    @staticmethod
                    def source_remove(source_id):
                        if phase == "published":
                            pause()
                        with warnings.catch_warnings(record=True) as caught:
                            warnings.simplefilter("always")
                            removed = real_glib.source_remove(source_id)
                        remove_warnings.extend(str(item.message) for item in caught)
                        return removed

                with patch.object(compress, "GLib", ObservedGLib):
                    if phase == "pending":
                        worker, errors = self.run_thread(
                            lambda: source._schedule_timer("av_sync_timer", 0, callback), "timer-publish",
                        )
                    else:
                        source._schedule_timer("av_sync_timer", 0, callback)
                        worker, errors = self.run_thread(
                            source.cancel_av_sync_timer, "timer-cancel",
                        )
                    try:
                        self.assertTrue(paused.wait(2))
                        if phase == "pending":
                            source.cancel_av_sync_timer()
                        # Dispatch the real source after cancellation detached
                        # its lease but before the producer/destroyer resumes.
                        self.assertTrue(context.iteration(False))
                        release.set()
                        worker.join(2)
                        self.assertFalse(worker.is_alive())
                        self.assertEqual(errors, [])
                        callback.assert_not_called()
                        self.assertEqual(remove_warnings, [])
                        self.assertEqual(source.av_sync_timer, 0)
                    finally:
                        release.set()
                        worker.join(2)
                        source.cleanup()

    def test_real_glib_source_dispatch_is_one_shot_and_reusable(self) -> None:
        source = self.make_source()
        context = compress.GLib.MainContext.default()
        callback = Mock(return_value=True)
        try:
            for count in (1, 2):
                source_id = source._schedule_timer("av_sync_timer", 0, callback)
                self.assertGreater(source_id, 0)
                self.assertTrue(context.iteration(False))
                self.assertEqual(callback.call_count, count)
                self.assertEqual(source.av_sync_timer, 0)
                self.assertEqual(source._timer_leases, {})
                self.assertIsNone(context.find_source_by_id(source_id))
        finally:
            source.cleanup()

    def test_dynamic_connection_close_owns_pending_timer_publication(self) -> None:
        glib = BlockingGLib()
        window_source = self.make_source()
        bases = (ClientConnection, WindowsConnection)
        with patch.object(factory, "get_needed_based_classes", return_value=bases):
            connection_class = factory.get_client_connection_class(typedict())
        connection = object.__new__(connection_class)
        ClientConnection.__init__(connection, Mock(), Mock(), Mock())
        WindowsConnection.init_state(connection)
        connection.statistics = SimpleNamespace(reset=Mock())
        connection.start_queue_encode = Mock()
        connection.queue_encode = Mock()
        connection.window_sources = {window_source.wid: window_source}

        with patch.object(compress, "GLib", glib):
            worker, errors = self.run_thread(window_source.schedule_av_sync_update, "delay-calculation")
            try:
                self.assertTrue(glib.timeout_started.wait(2))
                with glib.lock:
                    pending_callback = glib.callbacks[1]
                early_result = pending_callback()
                connection.close()
                glib.release_timeout.set()
                worker.join(2)

                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
                self.assertTrue(early_result)
                self.assertEqual(glib.removed, [1])
                self.assertEqual(glib.callbacks, {})
                self.assertEqual(window_source.av_sync_timer, 0)
                self.assertTrue(connection.close_event.is_set())
            finally:
                glib.release_timeout.set()
                worker.join(2)

    @staticmethod
    def make_refresh_source() -> WindowSource:
        source = WindowSourceTimerLifecycleTest.make_source()
        source.window = SimpleNamespace(is_managed=lambda: True)
        source.window_dimensions = 64, 64
        source.auto_refresh_delay = 100
        source.base_auto_refresh_delay = 100
        source.auto_refresh_encodings = ("rgb24",)
        source.batch_config.delay = 1
        source.global_statistics = SimpleNamespace(congestion_value=0)
        source.statistics.damage_ack_pending = {}
        source.statistics.damage_in_latency = []
        source.queue_packet = Mock()
        return source

    def test_packet_publication_keeps_current_refresh_and_lossless_cancellation(self) -> None:
        source = self.make_refresh_source()
        glib = FakeGLib()
        lossy = Packet("draw", source.wid, 0, 0, 64, 64, "jpeg", b"pixels", 1, 192, {"quality": 50})
        lossless = Packet("draw", source.wid, 0, 0, 64, 64, "rgb24", b"pixels", 2, 192, {})
        with patch.object(compress, "GLib", glib), patch.object(compress, "AUTO_REFRESH", True), \
                patch.object(compress, "SCREEN_UPDATES_DIRECTORY", ""):
            source.queue_damage_packet(lossy, 1, 2, typedict())
            self.assertEqual(source.refresh_timer, 1)
            self.assertEqual(source.refresh_regions, [rectangle(0, 0, 64, 64)])
            source.queue_damage_packet(lossless, 1, 2, typedict())
            self.assertEqual(source.refresh_regions, [])
            self.assertEqual(source.refresh_timer, 0)
            self.assertEqual(glib.callbacks, {})
            self.assertEqual(glib.removed, [1])
            self.assertEqual(source.queue_packet.call_count, 2)
            source.cleanup()

    def test_packet_refresh_publication_cannot_cross_terminal_cleanup(self) -> None:
        # Exercise the current public encode-thread producer, not a new helper
        # absent from clean source. Clean upstream publishes a dead timer ID
        # after cleanup has cleared the window and its refresh state.
        source = self.make_refresh_source()
        glib = BlockingGLib()
        packet = Packet("draw", source.wid, 0, 0, 64, 64, "jpeg", b"pixels", 1, 192, {"quality": 50})
        with patch.object(compress, "GLib", glib), patch.object(compress, "AUTO_REFRESH", True), \
                patch.object(compress, "SCREEN_UPDATES_DIRECTORY", ""):
            worker, errors = self.run_thread(
                lambda: source.queue_damage_packet(packet, 1, 2, typedict()), "packet-refresh-publication")
            try:
                self.assertTrue(glib.timeout_started.wait(2))
                source.cleanup()
                self.assertFalse(glib.fire(1))
            finally:
                glib.release_timeout.set()
                worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(source.refresh_timer, 0)
            self.assertEqual(glib.callbacks, {})
            self.assertEqual(source.refresh_regions, [])
            source.queue_packet.assert_called_once_with(packet, source.wid, 4096, False)

    def test_stale_callback_cannot_clear_replacement(self) -> None:
        source = self.make_source()
        glib = FakeGLib()
        with patch.object(compress, "GLib", glib):
            source.schedule_av_sync_update()
            with glib.lock:
                stale_callback = glib.callbacks[1]
            source.cancel_av_sync_timer()
            source.schedule_av_sync_update()
            self.assertEqual(source.av_sync_timer, 2)

            self.assertFalse(stale_callback())

            self.assertEqual(source.av_sync_timer, 2)
            self.assertEqual(source.av_sync_delay, 0)
            self.assertEqual(tuple(glib.callbacks), (2,))
            source.cleanup()

    def test_cancelled_pending_publication_cannot_replace_new_lease(self) -> None:
        source = self.make_source()
        glib = BlockingGLib()
        with patch.object(compress, "GLib", glib):
            worker, errors = self.run_thread(source.schedule_av_sync_update, "delay-calculation")
            try:
                self.assertTrue(glib.timeout_started.wait(2))
                with glib.lock:
                    stale_callback = glib.callbacks[1]
                self.assertLess(source.av_sync_timer, 0)

                source.cancel_av_sync_timer()
                source.schedule_av_sync_update()
                self.assertEqual(source.av_sync_timer, 2)
                glib.release_timeout.set()
                worker.join(2)

                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(source.av_sync_timer, 2)
                self.assertEqual(glib.removed, [1])
                self.assertFalse(stale_callback())
                source.cleanup()
                self.assertEqual(glib.removed, [1, 2])
            finally:
                glib.release_timeout.set()
                worker.join(2)

    def test_cleanup_waits_for_claimed_callback_and_cancels_its_rearm(self) -> None:
        source = self.make_source()
        glib = FakeGLib()
        callback_at_rearm = Event()
        release_callback = Event()
        cleanup_started = Event()
        cleanup_done = Event()
        original_schedule = source.schedule_av_sync_update

        def paused_rearm(delay=0) -> None:
            callback_at_rearm.set()
            if not release_callback.wait(2):
                raise RuntimeError("A/V callback was not released")
            original_schedule(delay)

        def cleanup() -> None:
            cleanup_started.set()
            source.cleanup()
            cleanup_done.set()

        with patch.object(compress, "GLib", glib):
            source.schedule_av_sync_update()
            source.schedule_av_sync_update = paused_rearm
            callback_thread, callback_errors = self.run_thread(lambda: glib.fire(1), "timer-callback")
            cleanup_thread = None
            try:
                self.assertTrue(callback_at_rearm.wait(2))
                self.assertTrue(source._timer_lock.acquire(timeout=0.2))
                source._timer_lock.release()
                cleanup_thread, cleanup_errors = self.run_thread(cleanup, "connection-close")
                self.assertTrue(cleanup_started.wait(2))
                self.assertFalse(cleanup_done.wait(0.1))
                release_callback.set()
                callback_thread.join(2)
                cleanup_thread.join(2)

                self.assertFalse(callback_thread.is_alive())
                self.assertFalse(cleanup_thread.is_alive())
                self.assertEqual(callback_errors, [])
                self.assertEqual(cleanup_errors, [])
                self.assertTrue(cleanup_done.is_set())
                self.assertEqual(glib.removed, [])
                self.assertEqual(glib.callbacks, {})
                self.assertEqual(source.av_sync_timer, 0)
            finally:
                release_callback.set()
                callback_thread.join(2)
                if cleanup_thread:
                    cleanup_thread.join(2)

    def test_connection_and_timer_locks_have_one_direction(self) -> None:
        source = self.make_source()
        glib = FakeGLib()
        connection_lock = Lock()
        ack_holds_connection_lock = Event()
        allow_timer_cancel = Event()
        callback_started = Event()
        ack_done = Event()

        def publish_packet() -> None:
            callback_started.set()
            if not connection_lock.acquire(timeout=1):
                raise RuntimeError("timer callback could not acquire connection lock")
            connection_lock.release()

        def acknowledge_packet() -> None:
            with connection_lock:
                ack_holds_connection_lock.set()
                if not allow_timer_cancel.wait(2):
                    raise RuntimeError("damage ACK was not released")
                source.cancel_may_send_timer()
                ack_done.set()

        with patch.object(compress, "GLib", glib):
            source._schedule_timer("may_send_timer", 10, publish_packet)
            ack_thread, ack_errors = self.run_thread(acknowledge_packet, "damage-ack")
            callback_thread = None
            try:
                self.assertTrue(ack_holds_connection_lock.wait(2))
                callback_thread, callback_errors = self.run_thread(lambda: glib.fire(1), "timer-callback")
                self.assertTrue(callback_started.wait(2))
                allow_timer_cancel.set()
                self.assertTrue(ack_done.wait(0.5))
                ack_thread.join(2)
                callback_thread.join(2)

                self.assertFalse(ack_thread.is_alive())
                self.assertFalse(callback_thread.is_alive())
                self.assertEqual(ack_errors, [])
                self.assertEqual(callback_errors, [])
            finally:
                allow_timer_cancel.set()
                ack_thread.join(2)
                if callback_thread:
                    callback_thread.join(2)
            source.cleanup()

    def test_expiry_nested_timer_publication_cannot_cross_cleanup(self) -> None:
        for soft_expired, timer_slot in ((0, "soft_timer"), (1, "timeout_timer")):
            with self.subTest(timer_slot=timer_slot):
                source = self.make_source()
                source.max_soft_expired = 10
                source.soft_expired = soft_expired
                source._damage_delayed = DelayedRegions(1, "rgb", {}, [])
                source.may_send_delayed = Mock()
                glib = BlockingGLib()
                with patch.object(compress, "GLib", glib):
                    worker, errors = self.run_thread(
                        lambda: source.expire_delayed_region(0, 10), f"{timer_slot}-callback",
                    )
                    try:
                        self.assertTrue(glib.timeout_started.wait(2))
                        source.cleanup()
                        glib.release_timeout.set()
                        worker.join(2)

                        self.assertFalse(worker.is_alive())
                        self.assertEqual(errors, [])
                        self.assertEqual(glib.removed, [1])
                        self.assertEqual(glib.callbacks, {})
                        self.assertEqual(getattr(source, timer_slot), 0)
                    finally:
                        glib.release_timeout.set()
                        worker.join(2)

    def test_video_nonvideo_expiry_uses_base_timer_lease(self) -> None:
        source = object.__new__(WindowVideoSource)
        WindowSource.init_vars(source)
        video_region = rectangle(0, 0, 100, 100)
        source.video_subregion = SimpleNamespace(
            rectangle=video_region,
            set_at=0,
            non_max_wait=500,
        )
        source.b_frame_flush_timer = 0
        source.is_tray = False
        source.video_encodings = ("h264",)
        source.full_frames_only = False
        source.window_dimensions = 200, 100
        source.statistics = SimpleNamespace(damage_events_count=100)
        source.batch_config = SimpleNamespace(delay=20, expire_delay=100)
        source.assign_sq_options = Mock(return_value={})
        source.get_frame_encode_delay = Mock(return_value=50)
        source.process_damage_region = Mock()
        source.do_send_regions = Mock()
        source.expire_delayed_region = Mock(return_value=False)
        source._damage_delayed = None
        glib = FakeGLib()

        with patch.object(compress, "GLib", glib), patch.object(video_compress, "GLib", glib):
            source.send_regions(
                compress.monotonic(),
                (video_region, rectangle(100, 0, 100, 100)),
                "auto",
                {},
            )
            with glib.lock:
                stale_callback = glib.callbacks[1]
            self.assertEqual(source.expire_timer, 1)
            source.cancel_expire_timer()
            self.assertEqual(source.expire_timer, 0)
            self.assertEqual(glib.removed, [1])
            self.assertFalse(stale_callback())
            source.expire_delayed_region.assert_not_called()

    def test_terminal_cleanup_is_idempotent_and_init_vars_cannot_reopen_it(self) -> None:
        source = self.make_source()
        glib = FakeGLib()
        with patch.object(compress, "GLib", glib):
            source.cleanup()
            source.cleanup()

            source.batch_config.cleanup.assert_called_once_with()
            source.call_in_encode_thread.assert_called_once()
            source.init_vars()
            source.schedule_av_sync_update()
            self.assertEqual(source.av_sync_timer, 0)
            self.assertEqual(glib.callbacks, {})

    def test_connection_packet_registry_is_deactivated_before_timer_close(self) -> None:
        source = self.make_source()
        unregister_events = []

        def unregister(registered_source) -> None:
            unregister_events.append((registered_source, source._timer_closed))

        source.unregister_damage_packets = unregister
        glib = FakeGLib()
        with patch.object(compress, "GLib", glib):
            source.schedule_av_sync_update()
            source.cleanup()
            source.unregister_damage_packets(source)

        self.assertEqual(unregister_events, [(source, False)])
        self.assertEqual(glib.removed, [1])

    def test_concurrent_cleanup_serializes_packet_registry_deactivation(self) -> None:
        source = self.make_source()
        unregister_started = Event()
        release_unregister = Event()
        waiter_started = Event()
        unregister_events = []

        def unregister(registered_source) -> None:
            unregister_events.append((registered_source, source._timer_closed))
            unregister_started.set()
            if not release_unregister.wait(2):
                raise RuntimeError("packet deactivation was not released")

        source.unregister_damage_packets = unregister
        source._timer_condition = ObservedCondition(source._timer_lock, waiter_started)
        glib = FakeGLib()
        with patch.object(compress, "GLib", glib):
            source.schedule_av_sync_update()
            first, first_errors = self.run_thread(source.cleanup, "first-cleanup")
            second = None
            try:
                self.assertTrue(unregister_started.wait(2))
                second, second_errors = self.run_thread(source.cleanup, "second-cleanup")
                self.assertTrue(waiter_started.wait(2))
                self.assertEqual(unregister_events, [(source, False)])

                release_unregister.set()
                first.join(2)
                second.join(2)

                self.assertFalse(first.is_alive())
                self.assertFalse(second.is_alive())
                self.assertEqual(first_errors, [])
                self.assertEqual(second_errors, [])
            finally:
                release_unregister.set()
                first.join(2)
                if second:
                    second.join(2)

        self.assertEqual(unregister_events, [(source, False)])
        self.assertEqual(glib.removed, [1])
        source.batch_config.cleanup.assert_called_once_with()
        source.call_in_encode_thread.assert_called_once()

    def test_concurrent_cleanup_waits_for_terminal_barrier(self) -> None:
        source = self.make_source()
        batch_cleanup_started = Event()
        release_batch_cleanup = Event()
        second_cleanup_started = Event()
        second_cleanup_waiting = Event()
        second_cleanup_done = Event()
        events = []

        def batch_cleanup() -> None:
            batch_cleanup_started.set()
            if not release_batch_cleanup.wait(2):
                raise RuntimeError("batch cleanup was not released")
            raise RuntimeError("batch cleanup failed")

        def queue_encode(callback) -> None:
            events.append(("barrier", callback))

        def second_cleanup() -> None:
            second_cleanup_started.set()
            source.cleanup()
            events.append(("second-return",))
            second_cleanup_done.set()

        source.batch_config.cleanup = batch_cleanup
        source.call_in_encode_thread = queue_encode
        source._timer_condition = ObservedCondition(source._timer_lock, second_cleanup_waiting)
        first, first_errors = self.run_thread(source.cleanup, "first-cleanup")
        second = None
        try:
            self.assertTrue(batch_cleanup_started.wait(2))
            second, second_errors = self.run_thread(second_cleanup, "second-cleanup")
            self.assertTrue(second_cleanup_started.wait(2))
            self.assertTrue(second_cleanup_waiting.wait(2))
            self.assertFalse(second_cleanup_done.is_set())

            release_batch_cleanup.set()
            first.join(2)
            second.join(2)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(len(first_errors), 1)
            self.assertRegex(str(first_errors[0]), "batch cleanup failed")
            self.assertEqual(second_errors, [])
            self.assertEqual(events, [
                ("barrier", source.encode_ended),
                ("second-return",),
            ])
            self.assertTrue(source._cleanup_complete)
            self.assertEqual(source._cleanup_owner, 0)
        finally:
            release_batch_cleanup.set()
            first.join(2)
            if second:
                second.join(2)

    def test_interrupted_cleanup_waiter_stays_behind_terminal_owner(self) -> None:
        source = self.make_source()
        batch_cleanup_started = Event()
        release_batch_cleanup = Event()
        waiter_interrupted = Event()
        waiter_done = Event()
        events = []

        def batch_cleanup() -> None:
            batch_cleanup_started.set()
            if not release_batch_cleanup.wait(2):
                raise RuntimeError("batch cleanup was not released")

        def queue_encode(callback) -> None:
            events.append(("barrier", callback))

        def waiting_cleanup() -> None:
            try:
                source.cleanup()
            finally:
                events.append(("waiter-return",))
                waiter_done.set()

        source.batch_config.cleanup = batch_cleanup
        source.call_in_encode_thread = queue_encode
        source._timer_condition = InterruptingCondition(source._timer_lock, waiter_interrupted)
        owner, owner_errors = self.run_thread(source.cleanup, "cleanup-owner")
        waiter = None
        try:
            self.assertTrue(batch_cleanup_started.wait(2))
            waiter, waiter_errors = self.run_thread(waiting_cleanup, "cleanup-waiter")
            self.assertTrue(waiter_interrupted.wait(2))
            self.assertFalse(waiter_done.is_set())

            release_batch_cleanup.set()
            owner.join(2)
            waiter.join(2)

            self.assertFalse(owner.is_alive())
            self.assertFalse(waiter.is_alive())
            self.assertEqual(owner_errors, [])
            self.assertEqual(len(waiter_errors), 1)
            self.assertIsInstance(waiter_errors[0], WaitInterrupted)
            self.assertEqual(events, [
                ("barrier", source.encode_ended),
                ("waiter-return",),
            ])
        finally:
            release_batch_cleanup.set()
            owner.join(2)
            if waiter:
                waiter.join(2)

        self.assertTrue(source._cleanup_complete)
        self.assertEqual(source._cleanup_owner, 0)

    def test_cleanup_owner_can_reenter_during_packet_deactivation(self) -> None:
        source = self.make_source()
        events = []

        def unregister(registered_source) -> None:
            events.append("unregister")
            registered_source.cleanup()
            events.append("reentrant-return")

        source.unregister_damage_packets = unregister
        cleanup, cleanup_errors = self.run_thread(source.cleanup, "reentrant-cleanup")
        cleanup.join(2)

        self.assertFalse(cleanup.is_alive())
        self.assertEqual(cleanup_errors, [])
        self.assertEqual(events, ["unregister", "reentrant-return"])
        source.batch_config.cleanup.assert_called_once_with()
        source.call_in_encode_thread.assert_called_once_with(source.encode_ended)
        self.assertTrue(source._cleanup_complete)
        self.assertEqual(source._cleanup_owner, 0)

    def test_timer_callback_can_start_terminal_cleanup(self) -> None:
        source = self.make_source()
        glib = FakeGLib()
        events = []

        def callback_cleanup() -> None:
            events.append("callback-start")
            source.cleanup()
            events.append(("callback-after-cleanup", source._cleanup_complete))

        with patch.object(compress, "GLib", glib):
            source._schedule_timer("may_send_timer", 10, callback_cleanup)
            callback, callback_errors = self.run_thread(lambda: glib.fire(1), "timer-cleanup")
            callback.join(2)

        self.assertFalse(callback.is_alive())
        self.assertEqual(callback_errors, [])
        self.assertEqual(events, ["callback-start", ("callback-after-cleanup", False)])
        self.assertTrue(source._cleanup_complete)
        self.assertFalse(source._cleanup_requested)
        self.assertEqual(source._cleanup_owner, 0)
        source.batch_config.cleanup.assert_called_once_with()
        source.call_in_encode_thread.assert_called_once_with(source.encode_ended)

    def test_nested_callback_cleanup_defers_until_outer_callback_unwinds(self) -> None:
        source = self.make_source()
        glib = FakeGLib()
        events = []

        def inner_callback() -> None:
            events.append(("inner-depth", source._active_timer_callback_threads.copy()))
            source.cleanup()
            events.append(("inner-return", source._cleanup_complete))

        def outer_callback() -> None:
            events.append(("outer-depth", source._active_timer_callback_threads.copy()))
            inner_source = source._schedule_timer("av_sync_timer", 10, inner_callback)
            glib.fire(inner_source)
            events.append(("outer-return", source._cleanup_complete))

        with patch.object(compress, "GLib", glib):
            outer_source = source._schedule_timer("may_send_timer", 10, outer_callback)
            glib.fire(outer_source)

        depths = [next(iter(event[1].values())) for event in events[:2]]
        self.assertEqual(depths, [1, 2])
        self.assertEqual(events[2:], [
            ("inner-return", False),
            ("outer-return", False),
        ])
        self.assertEqual(source._active_timer_callback_threads, {})
        self.assertTrue(source._cleanup_complete)
        self.assertFalse(source._cleanup_requested)
        source.batch_config.cleanup.assert_called_once_with()
        source.call_in_encode_thread.assert_called_once_with(source.encode_ended)

    def test_callback_error_remains_primary_over_deferred_cleanup_error(self) -> None:
        source = self.make_source()
        glib = FakeGLib()
        events = []

        def callback_cleanup() -> None:
            source.cleanup()
            raise CallbackInterrupted("callback failed")

        def batch_cleanup() -> None:
            events.append("batch-cleanup")
            raise DeferredCleanupInterrupted("deferred cleanup failed")

        def queue_encode(callback) -> None:
            events.append(("barrier", callback))

        source.batch_config.cleanup = batch_cleanup
        source.call_in_encode_thread = queue_encode
        with patch.object(compress, "GLib", glib), patch.object(compress, "log") as test_log:
            with self.assertRaisesRegex(CallbackInterrupted, "callback failed"):
                glib_source = source._schedule_timer("may_send_timer", 10, callback_cleanup)
                glib.fire(glib_source)

        self.assertEqual(events, [
            "batch-cleanup",
            ("barrier", source.encode_ended),
        ])
        self.assertTrue(source._cleanup_complete)
        self.assertEqual(source._cleanup_owner, 0)
        self.assertTrue(any(
            "Additional error during timer-requested window source cleanup" in str(call.args[0])
            and isinstance(call.kwargs.get("exc_info", (None, None))[1], DeferredCleanupInterrupted)
            for call in test_log.error.call_args_list
        ))

    def test_callback_cleanup_request_stays_closed_across_unregister_retry(self) -> None:
        source = self.make_source()
        glib = FakeGLib()
        unregister_started = Event()
        release_unregister = Event()
        unregister_calls = []
        unexpected_callback = Mock()

        def unregister(_registered_source) -> None:
            unregister_calls.append("unregister")
            if len(unregister_calls) == 1:
                unregister_started.set()
                if not release_unregister.wait(2):
                    raise RuntimeError("packet deactivation was not released")
                raise DeferredCleanupInterrupted("packet deactivation failed")

        def request_cleanup() -> None:
            source.cleanup()

        source.unregister_damage_packets = unregister
        with patch.object(compress, "GLib", glib):
            source._schedule_timer("may_send_timer", 10, request_cleanup)
            callback, callback_errors = self.run_thread(lambda: glib.fire(1), "timer-cleanup")
            try:
                self.assertTrue(unregister_started.wait(2))
                self.assertTrue(source._cleanup_requested)
                self.assertEqual(
                    source._schedule_timer("av_sync_timer", 10, unexpected_callback),
                    0,
                )
                self.assertEqual(tuple(glib.callbacks), (1,))
                release_unregister.set()
                callback.join(2)

                self.assertFalse(callback.is_alive())
                self.assertEqual(len(callback_errors), 1)
                self.assertIsInstance(callback_errors[0], DeferredCleanupInterrupted)
                self.assertTrue(source._cleanup_requested)
                self.assertFalse(source._timer_closed)
                self.assertFalse(source._cleanup_complete)
                self.assertEqual(source._cleanup_owner, 0)

                source.cleanup()
            finally:
                release_unregister.set()
                callback.join(2)

        self.assertEqual(unregister_calls, ["unregister", "unregister"])
        unexpected_callback.assert_not_called()
        self.assertFalse(source._cleanup_requested)
        self.assertTrue(source._timer_closed)
        self.assertTrue(source._cleanup_complete)
        source.batch_config.cleanup.assert_called_once_with()
        source.call_in_encode_thread.assert_called_once_with(source.encode_ended)

    def test_active_callback_does_not_wait_for_external_cleanup_owner(self) -> None:
        source = self.make_source()
        glib = FakeGLib()
        callback_started = Event()
        enter_callback_cleanup = Event()
        callback_cleanup_returned = Event()
        external_cleanup_waiting = Event()
        cleanup_done = Event()

        def callback_cleanup() -> None:
            callback_started.set()
            if not enter_callback_cleanup.wait(2):
                raise RuntimeError("callback cleanup was not released")
            source.cleanup()
            callback_cleanup_returned.set()

        def external_cleanup() -> None:
            source.cleanup()
            cleanup_done.set()

        with patch.object(compress, "GLib", glib):
            source._schedule_timer("may_send_timer", 10, callback_cleanup)
            callback, callback_errors = self.run_thread(lambda: glib.fire(1), "timer-callback")
            cleanup = None
            try:
                self.assertTrue(callback_started.wait(2))
                source._timer_condition = ObservedCondition(source._timer_lock, external_cleanup_waiting)
                cleanup, cleanup_errors = self.run_thread(external_cleanup, "external-cleanup")
                self.assertTrue(external_cleanup_waiting.wait(2))
                self.assertFalse(cleanup_done.is_set())
                enter_callback_cleanup.set()
                self.assertTrue(callback_cleanup_returned.wait(1))
                callback.join(2)
                cleanup.join(2)

                self.assertFalse(callback.is_alive())
                self.assertFalse(cleanup.is_alive())
                self.assertEqual(callback_errors, [])
                self.assertEqual(cleanup_errors, [])
                self.assertTrue(cleanup_done.is_set())
            finally:
                enter_callback_cleanup.set()
                callback.join(2)
                if cleanup:
                    cleanup.join(2)

        self.assertTrue(source._cleanup_complete)
        self.assertFalse(source._cleanup_requested)
        self.assertEqual(source._cleanup_owner, 0)
        source.batch_config.cleanup.assert_called_once_with()
        source.call_in_encode_thread.assert_called_once_with(source.encode_ended)

    def test_interrupted_callback_wait_still_completes_terminal_tail(self) -> None:
        source = self.make_source()
        glib = FakeGLib()
        callback_started = Event()
        release_callback = Event()
        wait_interrupted = Event()
        cleanup_done = Event()
        events = []

        def blocking_callback() -> None:
            events.append("callback-start")
            callback_started.set()
            if not release_callback.wait(2):
                raise RuntimeError("timer callback was not released")
            events.append("callback-end")

        def queue_encode(callback) -> None:
            events.append(("barrier", callback))

        def terminal_cleanup() -> None:
            try:
                source.cleanup()
            finally:
                cleanup_done.set()

        source.call_in_encode_thread = queue_encode
        with patch.object(compress, "GLib", glib):
            source._schedule_timer("may_send_timer", 10, blocking_callback)
            callback, callback_errors = self.run_thread(lambda: glib.fire(1), "timer-callback")
            cleanup = None
            try:
                self.assertTrue(callback_started.wait(2))
                source._timer_condition = InterruptingCondition(source._timer_lock, wait_interrupted)
                cleanup, cleanup_errors = self.run_thread(terminal_cleanup, "interrupted-cleanup")
                self.assertTrue(wait_interrupted.wait(2))
                self.assertFalse(cleanup_done.wait(0.1))
                self.assertNotIn(("barrier", source.encode_ended), events)

                release_callback.set()
                callback.join(2)
                cleanup.join(2)

                self.assertFalse(callback.is_alive())
                self.assertFalse(cleanup.is_alive())
                self.assertEqual(callback_errors, [])
                self.assertEqual(len(cleanup_errors), 1)
                self.assertIsInstance(cleanup_errors[0], WaitInterrupted)
                self.assertEqual(events, [
                    "callback-start",
                    "callback-end",
                    ("barrier", source.encode_ended),
                ])
            finally:
                release_callback.set()
                callback.join(2)
                if cleanup:
                    cleanup.join(2)

        self.assertTrue(source._cleanup_complete)
        self.assertFalse(source._cleanup_requested)
        self.assertEqual(source._cleanup_owner, 0)
        source.batch_config.cleanup.assert_called_once_with()

    def test_packet_registry_deactivation_failure_is_retryable(self) -> None:
        source = self.make_source()
        unregister_events = []

        def unregister(registered_source) -> None:
            unregister_events.append((registered_source, source._timer_closed))
            if len(unregister_events) == 1:
                raise RuntimeError("packet deactivation failed")

        source.unregister_damage_packets = unregister
        glib = FakeGLib()
        with patch.object(compress, "GLib", glib):
            source.schedule_av_sync_update()
            with self.assertRaisesRegex(RuntimeError, "packet deactivation failed"):
                source.cleanup()

            self.assertFalse(source._cleanup_started)
            self.assertFalse(source._timer_closed)
            self.assertEqual(glib.removed, [])
            self.assertEqual(tuple(glib.callbacks), (1,))
            source.batch_config.cleanup.assert_not_called()
            source.call_in_encode_thread.assert_not_called()

            source.cleanup()
            source.cleanup()

        self.assertEqual(unregister_events, [(source, False), (source, False)])
        self.assertEqual(glib.removed, [1])
        self.assertEqual(glib.callbacks, {})
        source.batch_config.cleanup.assert_called_once_with()
        source.call_in_encode_thread.assert_called_once()
        self.assertTrue(source._timer_closed)

    def test_cleanup_exception_does_not_skip_later_owned_steps(self) -> None:
        source = self.make_source()
        source.cancel_window_icon_timer = Mock(side_effect=RuntimeError("icon cleanup failed"))
        glib = FakeGLib()
        with patch.object(compress, "GLib", glib):
            with self.assertRaisesRegex(RuntimeError, "icon cleanup failed"):
                source.cleanup()
            source.cleanup()

        source.cancel_window_icon_timer.assert_called_once_with()
        source.batch_config.cleanup.assert_called_once_with()
        source.call_in_encode_thread.assert_called_once()
        self.assertTrue(source._timer_closed)

    def test_timer_removal_exception_does_not_skip_other_leases_or_cleanup(self) -> None:
        source = self.make_source()
        glib = FailingRemoveGLib()
        with patch.object(compress, "GLib", glib):
            source._schedule_timer("av_sync_timer", 10, Mock())
            source._schedule_timer("may_send_timer", 10, Mock())
            with self.assertRaisesRegex(RuntimeError, "cannot remove first timer"):
                source.cleanup()
            source.cleanup()

        self.assertEqual(glib.removed, [1, 2])
        self.assertEqual(glib.callbacks, {})
        source.batch_config.cleanup.assert_called_once_with()
        source.call_in_encode_thread.assert_called_once()
        self.assertTrue(source._timer_closed)

    def test_icon_timer_is_part_of_terminal_window_lifecycle(self) -> None:
        source = self.make_source()
        source.has_png = True
        source.suspended = False
        source.window_icon_data = None
        source.window_icon_greedy = False
        source.has_default = False
        source.window_icon_size = 64, 64
        source.window_icon_max_size = 64, 64
        source.window = SimpleNamespace(get_property=Mock(return_value=[object()]))
        source.choose_icon = Mock(return_value=(1, 1, "png", b"x"))
        source.batch_config.delay_per_megapixel = 0
        glib = FakeGLib()
        with patch.object(compress, "GLib", glib), patch.object(windowicon, "GLib", glib):
            source.send_window_icon()
            with glib.lock:
                stale_callback = glib.callbacks[1]
            self.assertEqual(source.send_window_icon_timer, 1)

            source.cleanup()
            self.assertEqual(source.send_window_icon_timer, 0)
            self.assertEqual(glib.removed, [1])
            self.assertFalse(stale_callback())
            self.assertEqual(source.call_in_encode_thread.call_count, 1)
            (callback,) = source.call_in_encode_thread.call_args.args
            self.assertEqual(callback, source.encode_ended)

    def test_icon_encode_queue_remains_a_single_batch(self) -> None:
        source = self.make_source()
        source.has_png = True
        source.suspended = False
        source.window_icon_data = None
        source.window_icon_greedy = False
        source.has_default = False
        source.window_icon_size = 64, 64
        source.window_icon_max_size = 64, 64
        source.window = SimpleNamespace(get_property=Mock(return_value=[object()]))
        source.choose_icon = Mock(return_value=(1, 1, "png", b"x"))
        source.batch_config.delay_per_megapixel = 0
        glib = FakeGLib()
        with patch.object(compress, "GLib", glib), patch.object(windowicon, "GLib", glib):
            source.send_window_icon()
            self.assertFalse(glib.fire(1))
            self.assertEqual(source.send_window_icon_timer, 0)
            self.assertTrue(source.window_icon_queued)
            self.assertEqual(source.call_in_encode_thread.call_count, 1)

            source.send_window_icon()
            self.assertEqual(glib.callbacks, {})
            (encode_callback,) = source.call_in_encode_thread.call_args.args
            # Preserve upstream's cleanup-time payload cancellation for work
            # that has already left the GLib timer and entered the encode queue.
            windowicon.WindowIconSource.cleanup(source)
            self.assertIsNone(source.window_icon_data)
            encode_callback()
            self.assertFalse(source.window_icon_queued)

            source.send_window_icon()
            self.assertEqual(source.send_window_icon_timer, 2)
            source.cleanup()
            self.assertEqual(glib.removed, [2])

    def test_callback_result_and_exception_remain_one_shot(self) -> None:
        source = self.make_source()
        glib = FakeGLib()
        callback = Mock(return_value=True)
        with patch.object(compress, "GLib", glib):
            source._schedule_timer("may_send_timer", 10, callback)
            self.assertFalse(glib.fire(1))
            callback.assert_called_once_with()
            self.assertEqual(source.may_send_timer, 0)

            def fail() -> None:
                raise RuntimeError("timer failed")

            source._schedule_timer("may_send_timer", 10, fail)
            with self.assertRaisesRegex(RuntimeError, "timer failed"):
                glib.fire(2)
            self.assertEqual(source.may_send_timer, 0)
            source._schedule_timer("may_send_timer", 10, callback)
            self.assertEqual(source.may_send_timer, 3)
            source.cleanup()

    def test_timeout_add_exception_releases_pending_lease(self) -> None:
        source = self.make_source()
        glib = FakeGLib()
        with patch.object(compress, "GLib", glib), patch.object(
            glib, "timeout_add", side_effect=RuntimeError("cannot create timer"),
        ):
            with self.assertRaisesRegex(RuntimeError, "cannot create timer"):
                source._schedule_timer("may_send_timer", 10, Mock())
            self.assertEqual(source.may_send_timer, 0)
            self.assertEqual(source._timer_leases, {})

        with patch.object(compress, "GLib", glib):
            source._schedule_timer("may_send_timer", 10, Mock())
            self.assertEqual(source.may_send_timer, 1)
            source.cleanup()


def load_tests(_loader, tests, _pattern):
    # Establish behavior through an existing encode-thread entry point before
    # exercising the patch's internal lease representation. On clean source,
    # fail-fast must report the dead refresh timer, not a missing new helper.
    control = WindowSourceTimerLifecycleTest("test_packet_refresh_publication_cannot_cross_terminal_cleanup")
    ordered = unittest.TestSuite([control])
    for group in tests:
        ordered.addTests(test for test in group if test.id() != control.id())
    return ordered


if __name__ == "__main__":
    unittest.main(failfast=True)
