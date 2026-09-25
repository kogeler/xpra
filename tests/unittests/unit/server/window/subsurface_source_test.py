#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 kogeler
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import threading
import unittest
from collections import deque
from contextlib import contextmanager
from types import MappingProxyType, SimpleNamespace
from unittest.mock import Mock, patch

from xpra.net.common import BACKWARDS_COMPATIBLE, Packet
from xpra.net.packet_type import WINDOW_CREATE, WINDOW_DESTROY, WINDOW_DRAW, WINDOW_EOS, WINDOW_ICON
from xpra.server.source.avsync import AVSyncConnection
from xpra.server.source.bandwidth import BandwidthConnection
from xpra.server.source.client_connection import ClientConnection
from xpra.server.source.clipboard import ClipboardConnection
from xpra.server.source.encoding import EncodingsConnection
from xpra.server.source.image_filter import NoFilter
from xpra.server.source.window import WindowsConnection
from xpra.server.subsystem.display import set_window_refresh_rate
from xpra.server.window.compress import WindowSource
from xpra.server.window.subsurface_source import SubsurfaceWindowSource
from xpra.server.window.video_compress import WindowVideoSource
from xpra.util.objects import typedict

MMAP_DRAIN_DATA = "_mmap-drain-data"
SUBSURFACE_COMPOSITE_MODE = "premultiplied-source-over-v1"
SUBSURFACE_COMPOSITE_FORMATS = ("BGRA", "RGBA", "BGRX", "RGBX")


@contextmanager
def source_scheduler(kind="idle", **kwargs):
    """Control Source attachment without replacing the ownership algorithm."""
    from xpra.server.source.window import GLib

    schedule = Mock(return_value=1, **{k: v for k, v in kwargs.items() if k != "return_value"})
    if "return_value" in kwargs:
        schedule.return_value = kwargs["return_value"]
    schedule.sources = []
    schedule.destroyed = Mock()

    class TestSource:
        def __init__(self, create_args):
            self.create_args = create_args
            self.callback = None
            self.destroy_count = 0

        def set_callback(self, callback):
            self.callback = callback

        def attach(self, context):
            assert context is None
            return schedule(*self.create_args, self.callback)

        def destroy(self):
            if not self.destroy_count:
                self.destroy_count = 1
                schedule.destroyed(self)

    def create(*args):
        source = TestSource(args)
        schedule.sources.append(source)
        return source

    with patch.object(GLib, kind + "_source_new", side_effect=create):
        yield schedule


class _ClipboardTransport(ClientConnection, ClipboardConnection):
    """Use the real clipboard producer and outgoing queue on one connection."""


class SubsurfaceEncodingTest(unittest.TestCase):

    @staticmethod
    def make_source(alpha=False, mmap=False, pictures=("jpeg", "png", "rgb32")):
        source = SubsurfaceWindowSource.__new__(SubsurfaceWindowSource)
        source.wid = 0x20
        source._mmap = object() if mmap else None
        source.encoding = "h264"
        source.strict = True
        source._encoding_hint = "h264"
        source.non_video_encodings = tuple(pictures)
        source.common_encodings = ("h264", "vp9") + tuple(pictures)
        source.core_encodings = ("h264", "vp9") + tuple(pictures)
        source._encoders = {encoding: object() for encoding in pictures}
        source.rgb_formats = ("BGRA", "RGBA")
        source.supports_transparency = True
        source.image_filter = NoFilter
        source._want_alpha = alpha
        source.has_alpha = alpha
        source.image_depth = 32
        source.client_bit_depth = 24
        source.content_types = ()
        source.is_tray = False
        source.has_shape = False
        source._rgb_auto_threshold = 0
        source.parent_wid = 0x10
        source.offset_x = 0
        source.offset_y = 0
        source.logical_width = 1024
        source.logical_height = 768
        source.native_width = 1024
        source.native_height = 768
        source.geometry_generation = 0
        return source

    def test_compatibility_selector_excludes_video_for_every_policy(self) -> None:
        policies = (
            ("auto opaque", False, False, "", ""),
            ("strict video", False, True, "", ""),
            ("video hint", False, False, "h264", ""),
            ("hardcoded video", False, False, "", "h264"),
            ("alpha preserving", True, False, "", ""),
            ("alpha strict video", True, True, "h264", "h264"),
        )
        for name, alpha, strict, hint, hardcoded in policies:
            with self.subTest(name=name), patch("xpra.server.window.compress.HARDCODED_ENCODING", hardcoded):
                source = self.make_source(alpha=alpha)
                source.strict = strict
                source._encoding_hint = hint
                selector = source.get_best_encoding_impl()
                expected = "rgb32" if alpha else "jpeg"
                self.assertEqual(selector(1024, 768, {"quality": 80}, "h264"), expected)

        source = self.make_source(mmap=True)
        self.assertEqual(source.get_best_encoding_impl()(1, 1, {}, "h264"), "mmap")

        for alpha, pictures in ((False, ()), (True, ("jpeg",))):
            with self.subTest(alpha=alpha, pictures=pictures):
                source = self.make_source(alpha=alpha, pictures=pictures)
                with self.assertRaises(ValueError):
                    source.get_best_encoding_impl()(1024, 768, {"quality": 80}, "h264")

    def test_raw_source_initialization_has_no_video_lifecycle(self) -> None:
        self.assertTrue(issubclass(SubsurfaceWindowSource, WindowSource))
        self.assertFalse(issubclass(SubsurfaceWindowSource, WindowVideoSource))
        source = self.make_source()
        source.core_encodings = ("h264", "png", "rgb32")

        def init_picture_encoders(instance) -> None:
            instance._encoders = {"png": object(), "rgb32": object()}
            instance.picture_encodings = ("png", "rgb32")

        with patch.object(WindowSource, "do_init_encoders", init_picture_encoders):
            SubsurfaceWindowSource.do_init_encoders(source)

        self.assertEqual(set(source.non_video_encodings), {"png", "rgb32"})
        for video_state in (
                "video_subregion", "video_encoder_timer", "b_frame_flush_timer",
                "_video_encoder", "_csc_encoder", "queue_video_eos",
                "video_context_clean"):
            self.assertFalse(hasattr(source, video_state), video_state)

    def test_direct_capture_and_region_paths_cannot_bypass_selector(self) -> None:
        source = self.make_source()
        with patch.object(WindowSource, "process_damage_region") as process:
            source.process_damage_region(1.0, 0, 0, 1024, 768, "h264", {"quality": 80})
        process.assert_called_once()
        self.assertEqual(process.call_args.args[5], "jpeg")
        self.assertFalse(source.may_use_scrolling(object(), {}))

        with patch.object(WindowSource, "process_damage_region") as process:
            source.process_damage_region(
                1.5, 0, 0, 1024, 768, "jpeg",
                {"subsurface-composite": SUBSURFACE_COMPOSITE_MODE},
            )
        process.assert_called_once()
        self.assertEqual(process.call_args.args[5], "rgb32")

        source.logical_width = 100
        source.logical_height = 80
        source.native_width = 200
        source.native_height = 160
        source.parent_wid = 0x10
        source.offset_x = 3
        source.offset_y = 4
        source.geometry_generation = 5
        with patch.object(WindowSource, "process_damage_region") as process:
            source.process_damage_region(2.0, 7, 9, 11, 13, "h264", {"quality": 100})
        packet_options = process.call_args.args[6]
        self.assertNotIn("scaled-damage-size", packet_options)
        self.assertEqual(packet_options["subsurface-geometry"], (0x10, 3, 4, 100, 80, 200, 160))

        source = self.make_source(mmap=True)
        self.assertEqual(source.get_best_encoding_impl()(1, 1, {}, "h264"), "mmap")

    def test_subsurface_preflight_error_releases_supplied_capture(self) -> None:
        source = self.make_source()
        source.image_filter = SimpleNamespace(enabled=False)
        image = Mock()
        image.is_thread_safe.return_value = True

        with self.assertRaisesRegex(ValueError, "configured image filter"):
            source.process_damage_region(
                1.0, 0, 0, 1, 1, "rgb32",
                {"subsurface-composite": SUBSURFACE_COMPOSITE_MODE},
                captured_image=image,
            )

        image.free.assert_called_once_with()

    def test_partial_origin_stage_keeps_its_exact_destination_geometry(self) -> None:
        source = self.make_source(alpha=True)
        source.parent_wid = 0x10
        source.offset_x = -3
        source.offset_y = 4
        source.logical_width = 100
        source.logical_height = 80
        source.native_width = 200
        source.native_height = 160
        source.geometry_generation = 5
        source._damage_packet_sequence = 1
        source.allocate_damage_packet_sequence = lambda: 17
        source.snapshot_damage_options = lambda _source, options: dict(options)
        client_options = {"rgb_format": "BGRA"}
        options = {
            "subsurface-geometry": source.geometry(),
            "subsurface-geometry-generation": source.geometry_generation,
        }

        packet = source.make_draw_packet(
            0, 0, 11, 13, "rgb32", b"x" * (11 * 13 * 4),
            11 * 4, client_options, options,
        )

        self.assertEqual(packet[1:6], (0x10, -3, 4, 11, 13))
        self.assertNotIn("scaled_size", packet.get_dict(10))
        self.assertEqual(packet.get_dict(10)["subsurface-geometry-generation"], 5)

    def test_mmap_source_still_uses_raw_rgb32_for_composite(self) -> None:
        source = self.make_source(alpha=True, mmap=True)
        source.common_encodings = ("mmap",)
        source.rgb_formats = ("BGRX", "RGBX")
        source.supports_transparency = False

        with patch.object(WindowSource, "process_damage_region") as process:
            source.process_damage_region(
                1.75, 0, 0, 1024, 768, "mmap",
                {"subsurface-composite": SUBSURFACE_COMPOSITE_MODE},
            )

        process.assert_called_once()
        self.assertEqual(process.call_args.args[5], "rgb32")
        self.assertEqual(
            source.subsurface_composite_eligibility(),
            (SUBSURFACE_COMPOSITE_FORMATS, ""),
        )
        source.core_encodings = ("mmap",)
        formats, error = source.subsurface_composite_eligibility()
        self.assertEqual(formats, ())
        self.assertIn("client does not support raw RGB32", error)

    def test_composite_capture_uses_basic_picture_processing(self) -> None:
        source = self.make_source(alpha=True)
        source.is_cancelled = Mock(return_value=False)
        source.discard_alpha = True
        source.opaque_contains = Mock(return_value=True)
        source.scaled_size = Mock(return_value=None)
        source.window_dimensions = (40, 30)
        image = Mock()
        image.get_width.return_value = 1
        image.get_height.return_value = 1
        image.get_depth.return_value = 32
        image.get_pixel_format.return_value = "BGRA"
        image.get_target_x.return_value = 0
        image.get_target_y.return_value = 0

        with patch.object(WindowSource, "do_process_damage_image", return_value=True) as picture:
            queued = WindowSource.process_damage_image(
                source, 1.0, 1.0, image, "rgb32", 7,
                {"preserve-premultiplied-alpha": True},
                force_basic_picture=True,
            )

        self.assertTrue(queued)
        picture.assert_called_once()
        image.set_pixel_format.assert_not_called()


class WindowSourceCompletionTest(unittest.TestCase):

    def test_standalone_snapshot_accepts_mapping_subclasses_without_mutation(self) -> None:
        source = WindowSource.__new__(WindowSource)
        for mapping_type in (dict, typedict, MappingProxyType):
            with self.subTest(mapping_type=mapping_type.__name__):
                original = {"window-size": (100, 80), "backing-epoch": 7}
                options = mapping_type(dict(original))
                snapshot = source.snapshot_damage_options(source, options)
                self.assertIs(type(snapshot), dict)
                self.assertIsNot(snapshot, options)
                self.assertEqual(snapshot, original)
                snapshot["backing-epoch"] = 8
                self.assertEqual(options, original)

    @staticmethod
    def make_image():
        image = Mock()
        image.is_thread_safe.return_value = True
        return image

    def test_zero_size_filtered_image_is_freed_and_completes_failure(self) -> None:
        source = WindowSource.__new__(WindowSource)
        source.schedule_auto_refresh = Mock()
        source.wid = 0x20
        source.window = Mock()
        source.window.is_managed.return_value = True
        image = Mock()
        image.get_width.return_value = 0
        image.get_height.return_value = 1
        source.window.get_image.return_value = image
        source.window_dimensions = (1, 1)
        source.may_update_window_dimensions = Mock(return_value=(1, 1))
        source.snapshot_damage_options = lambda _source, options: dict(options)
        source.defer_damage = Mock(return_value=False)
        source._sequence = 0
        source.is_cancelled = Mock(return_value=False)
        source.image_filter = NoFilter
        completed = Mock()

        queued = WindowSource.process_damage_region(
            source, 1.0, 0, 0, 1, 1, "rgb32", {}, packet_complete=completed,
        )

        self.assertTrue(queued)
        image.free.assert_called_once_with()
        completed.assert_called_once_with(False)

    def test_capture_callback_failure_frees_filtered_image_and_completes_once(self) -> None:
        source = WindowSource.__new__(WindowSource)
        source.schedule_auto_refresh = Mock()
        source.wid = 0x20
        source.window = Mock()
        source.window.is_managed.return_value = True
        image = self.make_image()
        image.get_width.return_value = 1
        image.get_height.return_value = 1
        source.window.get_image.return_value = image
        source.window_dimensions = (1, 1)
        source.may_update_window_dimensions = Mock(return_value=(1, 1))
        source.snapshot_damage_options = lambda _source, options: dict(options)
        source.defer_damage = Mock(return_value=False)
        source._sequence = 0
        source.is_cancelled = Mock(return_value=False)
        source.image_filter = NoFilter
        source.process_damage_image = Mock(side_effect=RuntimeError("encode queue rejected image"))
        completed = Mock()

        with (
            patch("xpra.server.window.compress.check_main_thread"),
            self.assertRaisesRegex(RuntimeError, "encode queue rejected image"),
        ):
            WindowSource.process_damage_region(
                source, 1.0, 0, 0, 1, 1, "rgb32", {}, packet_complete=completed,
            )

        image.free.assert_called_once_with()
        completed.assert_called_once_with(False)

    def test_supplied_capture_is_consumed_without_reading_the_live_model(self) -> None:
        source = WindowSource.__new__(WindowSource)
        source.schedule_auto_refresh = Mock()
        source.wid = 0x20
        source.window = Mock()
        source.window.is_managed.return_value = True
        source.window_dimensions = (1, 1)
        source.may_update_window_dimensions = Mock(return_value=(1, 1))
        source.snapshot_damage_options = lambda _source, options: dict(options)
        source.defer_damage = Mock(return_value=False)
        source._sequence = 0
        source.is_cancelled = Mock(return_value=False)
        source.image_filter = NoFilter
        source.process_damage_image = Mock(return_value=True)
        image = self.make_image()
        image.get_width.return_value = 1
        image.get_height.return_value = 1

        queued = WindowSource.process_damage_region(
            source, 1.0, 0, 0, 1, 1, "rgb32", {}, captured_image=image,
        )

        self.assertTrue(queued)
        source.window.get_image.assert_not_called()
        source.process_damage_image.assert_called_once()
        self.assertIs(source.process_damage_image.call_args.args[2], image)
        # A successful encode handoff, rather than this UI method, now owns the
        # wrapper.  The connection never retains a second reference to it.
        image.free.assert_not_called()

    def test_supplied_capture_is_freed_on_pre_encode_abort(self) -> None:
        source = WindowSource.__new__(WindowSource)
        source.schedule_auto_refresh = Mock()
        source.wid = 0x20
        source.window = Mock()
        source.window.is_managed.return_value = False
        source.snapshot_damage_options = lambda _source, options: dict(options)
        image = self.make_image()

        queued = WindowSource.process_damage_region(
            source, 1.0, 0, 0, 1, 1, "rgb32", {}, captured_image=image,
        )

        self.assertFalse(queued)
        source.window.get_image.assert_not_called()
        image.free.assert_called_once_with()


class _FakeCapturedImage:
    def __init__(self, width: int, height: int, generation: int):
        self.width = width
        self.height = height
        self.generation = generation
        self.free_count = 0

    def get_width(self) -> int:
        return self.width

    def get_height(self) -> int:
        return self.height

    def is_thread_safe(self) -> bool:
        return True

    def free(self) -> None:
        self.free_count += 1


class _FakePixelSource:
    def __init__(self, wid: int, parent_wid=0):
        self.wid = wid
        self.parent_wid = parent_wid
        self.offset_x = 0
        self.offset_y = 0
        self.logical_width = 0
        self.logical_height = 0
        self.native_width = 0
        self.native_height = 0
        self.geometry_generation = 0
        self._sequence = 0
        self.window_dimensions = (64, 48)
        self.full_frames_only = False
        self.encoding = "png"
        self._encoders = {"rgb32": object()}
        self.common_encodings = ("rgb32",)
        self.core_encodings = ("rgb32",)
        self.rgb_formats = ("BGRA", "RGBA")
        self.supports_transparency = True
        self.image_filter = NoFilter
        self.has_alpha = True
        self.window = object()
        self.calls = []
        self.captured_images = []
        self.auto_publish = True
        self.packet_completions = []
        self.damage_packet_acked = Mock(side_effect=lambda *args: self.calls.append(("ack", args)))
        self.cleanup_started = threading.Event()
        self.cleanup_finished = threading.Event()
        self.cleanup_observer = None
        self.unregister_damage_packets = lambda _source: None
        self.cuda_device_context = None
        self.statistics = SimpleNamespace(
            encoding_stats=[], damage_in_latency=[], damage_out_latency=[],
        )
        self.batch_config = SimpleNamespace(
            always=False, locked=False, min_delay=0, max_delay=0, timeout_delay=0, delay=0,
            match_vrefresh=lambda value: self._record("refresh-rate", value),
        )

    def _record(self, name, *args):
        self.calls.append((name, args))

    def subsurface_composite_eligibility(self):
        return WindowSource.subsurface_composite_eligibility(self)

    def cleanup(self):
        self.unregister_damage_packets(self)
        self.cleanup_started.set()
        if self.cleanup_observer:
            self.cleanup_observer()
        self._record("cleanup")
        self.cleanup_finished.set()

    def suspend(self):
        self._record("suspend")

    def resume(self):
        self._record("resume")

    def go_idle(self):
        self._record("go-idle")

    def no_idle(self):
        self._record("no-idle")

    def init_encoders(self):
        self._record("init-encoders")

    def video_context_clean(self):
        self._record("video-clean")

    def map(self, value):
        self._record("map", value)

    def unmap(self):
        self._record("unmap")

    def cancel_damage(self, limit=0):
        self._record("cancel", limit)

    def may_update_window_dimensions(self):
        return self.window_dimensions

    def capture_damage_image(self, _x: int, _y: int, width: int, height: int):
        getter = getattr(self.window, "get_snapshot_generation", None)
        generation = getter() if getter else 0
        if type(generation) is not int:
            generation = 0
        image = _FakeCapturedImage(width, height, generation)
        self.captured_images.append(image)
        self._record("capture", width, height, generation, image)
        return image

    def geometry(self):
        return (
            self.parent_wid, self.offset_x, self.offset_y,
            self.logical_width, self.logical_height,
            self.native_width, self.native_height,
        )

    def update_geometry(self, *geometry):
        if self.geometry() == geometry:
            return False
        (
            self.parent_wid, self.offset_x, self.offset_y,
            self.logical_width, self.logical_height,
            self.native_width, self.native_height,
        ) = geometry
        self.geometry_generation += 1
        return True

    @staticmethod
    def get_best_nonvideo_encoding(_width, _height, _options):
        return "png"

    @staticmethod
    def get_subsurface_encoding(_width, _height, _options, _encoding):
        return "png"

    def process_damage_region(self, damage_time, x, y, width, height, coding, options,
                              flush=0, packet_complete=None, force_basic_picture=False, *,
                              captured_image=None):
        self._sequence += 1
        image = captured_image or self.capture_damage_image(x, y, width, height)
        self._record(
            "region", damage_time, x, y, width, height, coding, dict(options), flush,
            image.generation,
        )

        def complete(published: bool) -> None:
            image.free()
            if packet_complete:
                packet_complete(published)

        if packet_complete:
            if self.auto_publish:
                complete(True)
            else:
                self.packet_completions.append(complete)
        else:
            image.free()
        return True

    def set_auto_refresh_delay(self, value):
        self._record("refresh-delay", value)

    def refresh(self, value):
        self._record("refresh", value)

    def set_client_properties(self, value):
        self._record("client-properties", value)

    def set_new_encoding(self, value, strict):
        self._record("encoding", value, strict)

    def set_min_quality(self, value):
        self._record("min-quality", value)

    def set_max_quality(self, value):
        self._record("max-quality", value)

    def set_quality(self, value):
        self._record("quality", value)

    def set_min_speed(self, value):
        self._record("min-speed", value)

    def set_max_speed(self, value):
        self._record("max-speed", value)

    def set_speed(self, value):
        self._record("speed", value)

    def set_av_sync(self, value):
        self._record("av-sync", value)

    def set_av_sync_delay(self, value):
        self._record("av-delay", value)

    def may_update_av_sync_delay(self):
        self._record("av-update")


class _SnapshotWindow:

    def __init__(self, generation: int = 0):
        self.generation = generation

    def get_snapshot_generation(self) -> int:
        return self.generation


class SubsurfaceOwnershipTest(unittest.TestCase):

    def test_connection_snapshot_accepts_mapping_subclasses_and_preserves_epochs(self) -> None:
        connection = self.make_connection()
        parent = _FakePixelSource(0x10)
        child = _FakePixelSource(0x20, parent_wid=parent.wid)
        connection.subsurface_sources[child.wid] = child
        connection._backing_epochs[parent.wid] = 7
        for source in (parent, child):
            connection.configure_pixel_source(source)
            epoch_key = "subsurface-backing-epoch" if source is child else "backing-epoch"
            for mapping_type in (dict, typedict, MappingProxyType):
                for explicit_epoch in (None, 5):
                    with self.subTest(source=source.wid, mapping_type=mapping_type.__name__,
                                      explicit_epoch=explicit_epoch):
                        original = {"window-size": (100, 80)}
                        if source is child:
                            original["subsurface-composite"] = SUBSURFACE_COMPOSITE_MODE
                        if explicit_epoch is not None:
                            original[epoch_key] = explicit_epoch
                        options = mapping_type(dict(original))
                        snapshot = source.snapshot_damage_options(source, options)
                        self.assertIs(type(snapshot), dict)
                        self.assertIsNot(snapshot, options)
                        expected = dict(original)
                        expected.setdefault(epoch_key, 7)
                        self.assertEqual(snapshot, expected)
                        snapshot[epoch_key] = 8
                        self.assertEqual(options, original)

    @staticmethod
    def make_connection(connection_class=WindowsConnection):
        connection = connection_class()
        WindowsConnection.init_state(connection)
        connection.window_enabled = True
        connection.statistics = SimpleNamespace(client_decode_time=[])
        connection.may_recalculate = Mock()
        connection.calculate_window_pixels = {}
        connection.calculate_window_ids = set()
        connection.emit = Mock()
        connection.idle = False
        connection.subsurface_composite_modes = (SUBSURFACE_COMPOSITE_MODE,)
        return connection

    @staticmethod
    def make_transport(connection):
        transport = ClientConnection.__new__(ClientConnection)
        transport.close_event = threading.Event()
        transport.ordinary_packets = []
        transport.packet_queue = deque()
        transport.filter_queued_damage_packet = connection.filter_queued_damage_packet
        return transport

    @staticmethod
    def make_window_model():
        window = Mock()
        window.is_tray.return_value = False
        window.get_property_names.return_value = ()
        return window

    def test_clipboard_producer_survives_draw_queue_filter(self) -> None:
        connection = self.make_connection()
        transport = _ClipboardTransport.__new__(_ClipboardTransport)
        transport.close_event = threading.Event()
        transport.ordinary_packets = []
        transport.packet_queue = deque()
        transport.statistics = SimpleNamespace(packet_qsizes=[], damage_packet_qpixels=[])
        transport.protocol = Mock()
        transport.filter_queued_damage_packet = connection.filter_queued_damage_packet
        ClipboardConnection.init_state(transport)
        packets = []
        for selection in ("CLIPBOARD", "PRIMARY"):
            for packet in (
                Packet("clipboard-token", selection),
                Packet("clipboard-contents", 1, selection, "UTF8_STRING", 8, "bytes", b"queue regression"),
            ):
                # This production method deliberately queues a plain tuple.
                # Calling queue_packet with a fabricated Packet would miss the bug.
                ClipboardConnection.compress_clipboard(transport, packet)
                packets.append(transport.packet_queue[-1][0])
        transport.send("ping", 123)
        self.assertEqual(transport.next_packet()[0].get_type(), "ping")
        for index, expected in enumerate(packets):
            packet, synchronous, more = transport.next_packet()
            self.assertIs(packet, expected)
            self.assertTrue(synchronous)
            self.assertEqual(more, index < len(packets) - 1)
        self.assertEqual(transport.next_packet()[0].get_type(), "none")
        self.assertFalse(transport.is_closed())
        self.assertEqual(connection._damage_packet_owners, {})

    def test_non_draw_queue_entries_keep_identity_and_batch_flags(self) -> None:
        connection = self.make_connection()
        for parts in ((WINDOW_EOS, 0x10), (WINDOW_ICON, 0x10, 0, 0, "default", b"")):
            for packet in (parts, list(parts), Packet(*parts)):
                for more in (False, True):
                    queued = packet, 0, 0, more
                    with self.subTest(packet_type=parts[0], container=type(packet).__name__, more=more):
                        self.assertIs(connection.filter_queued_damage_packet(queued), queued)

    def test_sequence_draws_cannot_bypass_stale_validation_or_mmap_drain(self) -> None:
        connection = self.make_connection()
        for coding in ("rgb32", "mmap"):
            chunks = ((4096, 16),)
            data = chunks if coding == "mmap" and BACKWARDS_COMPATIBLE else b""
            parts = (WINDOW_DRAW, 0x10, 2, 3, 4, 5, coding, data, 1, 16, {"chunks": chunks})
            for packet in (Packet(*parts), parts, list(parts)):
                with self.subTest(coding=coding, container=type(packet).__name__):
                    result = connection.filter_queued_damage_packet((packet, 0x10, 20, True))
                    if coding != "mmap":
                        self.assertIsNone(result)
                        continue
                    drain, wid, pixels, more = result
                    self.assertEqual((drain.get_type(), drain.get_wid()), (WINDOW_DRAW, -1))
                    self.assertEqual(tuple(drain[2:]), parts[2:])
                    self.assertEqual((wid, pixels, more), (0, 0, False))

    def test_source_borrows_retain_statistics_and_batch_until_callout_finishes(self) -> None:
        for phase, child in (("statistics", False), ("statistics", True),
                             ("weighted-average", False), ("bandwidth", False),
                             ("bandwidth-assign", False)):
            with self.subTest(phase=phase, child=child):
                connection = self.make_connection()
                source = _FakePixelSource(0x20, 0x10 if child else 0)
                sources = connection.subsurface_sources if child else connection.window_sources
                sources[source.wid] = source
                source.maximized = source.fullscreen = source.suspended = False
                source.batch_config.last_updated = 0
                connection.configure_pixel_source(source)
                entered = threading.Event()
                release = threading.Event()
                unregister_entered = threading.Event()
                failures = []

                def blocked_value(value=None):
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError("calculator callout was not released")
                    return value

                def retire_statistics():
                    source.statistics = None
                    source.batch_config = None

                source.cleanup_observer = retire_statistics
                source.statistics.update_averages = lambda: blocked_value()
                source.statistics.get_damage_pixels = lambda: blocked_value(20)
                source.calculate_batch_delay = Mock()
                source.reconfigure = Mock()
                connection.protocol = SimpleNamespace(
                    is_closed=lambda: False,
                    _conn=SimpleNamespace(output_bytecount=0, socktype_wrapped=""),
                )
                connection.is_closed = lambda: False
                connection.get_focus = lambda: 0
                connection.statistics = None
                connection.global_batch_config = SimpleNamespace(last_delays=[])
                if phase == "weighted-average":
                    class DelayedBatch:
                        @property
                        def last_updated(self):
                            return blocked_value(1)
                        delay = 10
                    source.batch_config = DelayedBatch()
                if phase == "bandwidth-assign":
                    class BandwidthAssignmentSource(_FakePixelSource):
                        @property
                        def bandwidth_limit(self):
                            return self._limit

                        @bandwidth_limit.setter
                        def bandwidth_limit(self, value):
                            self._limit = blocked_value(value)

                    source.__class__ = BandwidthAssignmentSource
                    source.statistics.get_damage_pixels = lambda: 20
                if phase.startswith("bandwidth"):
                    connection.bandwidth_detection = True
                    connection.bandwidth_limit = 0
                    connection.statistics = SimpleNamespace(avg_congestion_send_speed=5_000_000)

                def calculate():
                    if phase.startswith("bandwidth"):
                        BandwidthConnection.update_bandwidth_limits(connection)
                    else:
                        with connection.pixel_source_operation(source) as owner:
                            self.assertIs(owner, source)
                            if phase == "weighted-average":
                                self.assertEqual(owner.batch_config.last_updated, 1)
                            else:
                                owner.statistics.update_averages()
                                owner.calculate_batch_delay()
                                owner.reconfigure()

                def run(callback):
                    try:
                        callback()
                    except Exception as error:
                        failures.append(error)

                original_unregister = connection.unregister_damage_packets

                def unregister(owner):
                    unregister_entered.set()
                    original_unregister(owner)

                connection.unregister_damage_packets = unregister
                remove = (lambda: connection.cleanup_subsurface_source(source.wid)) if child else (
                    lambda: connection.remove_window(source.wid, source.window)
                )
                calculator = threading.Thread(target=run, args=(calculate,))
                remover = threading.Thread(target=run, args=(remove,))
                with patch("xpra.server.source.encoding.may_update_bandwidth_limits"):
                    try:
                        calculator.start()
                        self.assertTrue(entered.wait(5), "calculator did not reach its source callout")
                        remover.start()
                        self.assertTrue(unregister_entered.wait(5), "remover did not reach source unregister")
                        cleanup_ran_while_borrowed = source.cleanup_finished.wait(0.05)
                    finally:
                        release.set()
                        calculator.join(5)
                        if remover.ident is not None:
                            remover.join(5)
                self.assertFalse(calculator.is_alive())
                self.assertFalse(remover.is_alive())
                self.assertFalse(cleanup_ran_while_borrowed, "source cleanup ran inside a live calculation")
                self.assertEqual(failures, [])
                self.assertTrue(source.cleanup_finished.is_set())
                self.assertEqual(connection._damage_packet_active_ops, {})

    def test_borrow_rejects_exact_source_replaced_after_lookup(self) -> None:
        connection = self.make_connection()
        old = _FakePixelSource(0x20)
        replacement = _FakePixelSource(old.wid)
        connection.window_sources[old.wid] = old
        connection.configure_pixel_source(old)
        borrowed = connection.get_pixel_source(old.wid)
        self.assertIs(borrowed, old)
        connection.window_sources[old.wid] = replacement
        connection.configure_pixel_source(replacement)
        connection.unregister_damage_packets(old)
        with connection.pixel_source_operation(borrowed) as owner:
            self.assertIsNone(owner)
        with connection.pixel_source_operation(replacement) as owner:
            self.assertIs(owner, replacement)
        self.assertEqual(connection._damage_packet_active_ops, {})

    def test_source_operation_rejects_detached_replaced_and_closing_sources(self) -> None:
        connection = self.make_connection()
        source = _FakePixelSource(0x20)
        connection.window_sources[source.wid] = source
        connection.configure_pixel_source(source)
        with self.assertRaisesRegex(RuntimeError, "callout failed"):
            with connection.pixel_source_operation(source) as owner:
                self.assertIs(owner, source)
                self.assertEqual(connection._damage_packet_active_ops, {id(source): 1})
                raise RuntimeError("callout failed")
        self.assertEqual(connection._damage_packet_active_ops, {})
        replacement = _FakePixelSource(source.wid)
        for current, closing in ((None, False), (replacement, False), (source, True)):
            with self.subTest(current=current, closing=closing):
                connection.window_sources[source.wid] = current
                connection._damage_packet_closing = closing
                # The former source remains registered until unregister runs;
                # map deactivation alone must already reject a fresh borrow.
                self.assertIs(connection._damage_packet_sources[id(source)], source)
                with connection.pixel_source_operation(source) as owner:
                    self.assertIsNone(owner)
                self.assertEqual(connection._damage_packet_active_ops, {})
        connection._damage_packet_closing = False
        connection.window_sources[source.wid] = source
        connection.unregister_damage_packets(source)
        with connection.pixel_source_operation(source) as owner:
            self.assertIsNone(owner)

    def test_window_damage_acceptance_uses_exact_live_announced_model(self) -> None:
        connection = self.make_connection()
        connection.hello_sent = 1.0
        connection.suspended = False
        connection.is_closed = Mock(return_value=False)
        wid = 0x10
        model = self.make_window_model()

        self.assertFalse(connection.can_consume_window_damage(wid, model))
        self.assertTrue(connection._announce_window(wid, model, Mock()))
        self.assertTrue(connection.can_consume_window_damage(wid, model))

        source = SimpleNamespace(window=model, suspended=False)
        connection.window_sources[wid] = source
        self.assertTrue(connection.can_consume_window_damage(wid, model))
        source.suspended = True
        self.assertFalse(connection.can_consume_window_damage(wid, model))
        source.suspended = False

        connection.hidden_windows.add(model)
        self.assertFalse(connection.can_consume_window_damage(wid, model))
        connection.hidden_windows.clear()
        connection.suspended = True
        self.assertFalse(connection.can_consume_window_damage(wid, model))
        connection.suspended = False
        connection.is_closed.return_value = True
        self.assertFalse(connection.can_consume_window_damage(wid, model))
        connection.is_closed.return_value = False

        other = self.make_window_model()
        self.assertFalse(connection.can_consume_window_damage(wid, other))
        connection._window_detaching[wid] = model
        self.assertFalse(connection.can_consume_window_damage(wid, model))

    def test_refusal_detaches_exact_parent_and_children_once(self) -> None:
        connection = self.make_connection()
        model = self.make_window_model()
        parent = _FakePixelSource(0x10)
        child = _FakePixelSource(0x20, parent.wid)
        parent.window = model
        for source in (parent, child):
            source.statistics.damage_ack_pending = {}
            connection.configure_pixel_source(source)
            connection.calculate_window_pixels[source.wid] = 10
        connection.window_sources[parent.wid] = parent
        connection.subsurface_sources[child.wid] = child
        connection.window_backing_properties[parent.wid] = {"bit-depth": 24}
        connection.subsurface_stacking[parent.wid] = (parent.wid, child.wid)
        connection._subsurface_composites[parent.wid] = {"token": object()}
        self.assertTrue(connection._announce_window(parent.wid, model, lambda: None))
        connection.send = Mock()
        for source in (parent, child):
            sequence = source.allocate_damage_packet_sequence()
            source.statistics.damage_ack_pending[sequence] = object()
            self.assertTrue(source.publish_damage_packet(parent.wid, sequence, source, lambda: None))

        self.assertTrue(connection.refuse_window(parent.wid, model, "unsupported composition"))

        connection.send.assert_called_once_with(WINDOW_DESTROY, parent.wid)
        self.assertTrue(connection.is_window_refused(parent.wid, model))
        self.assertFalse(connection.is_window_announced(parent.wid, model))
        self.assertEqual(connection.window_sources, {})
        self.assertEqual(connection.subsurface_sources, {})
        self.assertEqual(connection.window_backing_properties, {})
        self.assertEqual(connection.subsurface_stacking, {})
        self.assertEqual(connection._subsurface_composites, {})
        self.assertEqual(connection._damage_packet_sources, {})
        self.assertEqual(connection._damage_packet_owners, {})
        self.assertEqual(connection.calculate_window_pixels, {})
        for source in (parent, child):
            self.assertEqual(source.statistics.damage_ack_pending, {})
            self.assertTrue(source.cleanup_finished.is_set())

        backing_epoch = connection._backing_epochs[parent.wid]
        topology_epoch = connection._subsurface_topology_epochs[parent.wid]
        self.assertFalse(connection.refuse_window(parent.wid, model, "same refusal"))
        connection.send.assert_called_once_with(WINDOW_DESTROY, parent.wid)
        self.assertEqual(connection._backing_epochs[parent.wid], backing_epoch)
        self.assertEqual(connection._subsurface_topology_epochs[parent.wid], topology_epoch)

    def test_never_announced_refusal_and_exact_model_reuse(self) -> None:
        connection = self.make_connection()
        connection.send = Mock()
        old_model = self.make_window_model()
        new_model = self.make_window_model()
        wid = 0x10

        self.assertTrue(connection.refuse_window(wid, old_model, "late joiner unsupported"))
        connection.lost_window(wid, old_model)
        connection.send.assert_not_called()
        self.assertFalse(connection.allow_window(wid, new_model))
        connection.remove_window(wid, old_model)

        announced = Mock()
        self.assertTrue(connection._announce_window(wid, new_model, announced))
        announced.assert_called_once_with()
        source = _FakePixelSource(wid)
        source.window = new_model
        connection.window_sources[wid] = source
        connection.configure_pixel_source(source)

        self.assertFalse(connection.refuse_window(wid, old_model, "stale callback"))
        connection.lost_window(wid, old_model)
        connection.remove_window(wid, old_model)
        self.assertIs(connection.window_sources[wid], source)
        self.assertTrue(connection.is_window_announced(wid, new_model))
        connection.send.assert_not_called()

    def test_stale_remove_cannot_detach_unannounced_reused_source(self) -> None:
        connection = self.make_connection()
        old_model = self.make_window_model()
        new_model = self.make_window_model()
        source = _FakePixelSource(0x10)
        source.window = new_model
        connection.window_sources[source.wid] = source
        connection.configure_pixel_source(source)

        connection.remove_window(source.wid, old_model)

        self.assertIs(connection.window_sources[source.wid], source)
        self.assertFalse(source.cleanup_started.is_set())
        self.assertFalse(connection._window_model_matches(source.wid, old_model))
        self.assertTrue(connection._window_model_matches(source.wid, new_model))
        self.assertFalse(connection._announce_window(source.wid, old_model, Mock()))
        announced = Mock()
        self.assertTrue(connection._announce_window(source.wid, new_model, announced))
        announced.assert_called_once_with()

    def test_same_wid_restore_waits_for_exact_old_model_detach(self) -> None:
        connection = self.make_connection()
        old_model = self.make_window_model()
        new_model = self.make_window_model()
        old_source = _FakePixelSource(0x10)
        old_source.window = old_model
        cleanup_release = threading.Event()
        old_source.cleanup_observer = lambda: self.assertTrue(cleanup_release.wait(2))
        connection.window_sources[old_source.wid] = old_source
        connection.configure_pixel_source(old_source)
        self.assertTrue(connection._announce_window(old_source.wid, old_model, lambda: None))

        remover = threading.Thread(
            target=connection.remove_window,
            args=(old_source.wid, old_model),
            daemon=True,
        )
        remover.start()
        self.assertTrue(old_source.cleanup_started.wait(2))
        announced = Mock()
        self.assertFalse(connection._announce_window(old_source.wid, new_model, announced))
        announced.assert_not_called()
        new_source = _FakePixelSource(old_source.wid)
        new_source.window = new_model
        with self.assertRaisesRegex(RuntimeError, "being detached"):
            connection._activate_pixel_source(connection.window_sources, new_source.wid, new_source)
        self.assertNotIn(old_source.wid, connection.window_sources)

        cleanup_release.set()
        remover.join(2)
        self.assertFalse(remover.is_alive())
        self.assertTrue(connection._announce_window(old_source.wid, new_model, announced))
        announced.assert_called_once_with()

    def test_refused_root_can_be_prepared_but_cannot_publish(self) -> None:
        connection = self.make_connection()
        model = self.make_window_model()
        wid = 0x10
        connection.send = Mock()
        self.assertTrue(connection.refuse_window(wid, model, "unsupported tree"))
        source = _FakePixelSource(wid)
        source.window = model

        self.assertIs(
            connection._activate_pixel_source(connection.window_sources, wid, source),
            source,
        )
        refused_epoch = connection._backing_epochs[wid]
        refused_packet = Packet(
            WINDOW_DRAW, wid, 0, 0, 1, 1, "rgb32", b"x", 1, 4,
            {"backing-epoch": refused_epoch},
        )
        self.assertFalse(connection.validate_damage_packet(source, refused_packet))
        self.assertTrue(connection.allow_window(wid, model))
        self.assertFalse(connection.validate_damage_packet(source, refused_packet))
        restored_packet = Packet(
            WINDOW_DRAW, wid, 0, 0, 1, 1, "rgb32", b"x", 2, 4,
            {"backing-epoch": connection._backing_epochs[wid]},
        )
        self.assertTrue(connection.validate_damage_packet(source, restored_packet))
        connection.send.assert_not_called()

    def test_refusal_is_connection_local_and_allow_waits_for_detach(self) -> None:
        first = self.make_connection()
        second = self.make_connection()
        model = self.make_window_model()
        wid = 0x10
        release_cleanup = threading.Event()
        parent = _FakePixelSource(wid)
        parent.window = model
        parent.cleanup_observer = lambda: self.assertTrue(release_cleanup.wait(2))
        first.window_sources[wid] = parent
        first.configure_pixel_source(parent)
        for connection in (first, second):
            self.assertTrue(connection._announce_window(wid, model, lambda: None))
            connection.send = Mock()

        refused = []
        refusal_thread = threading.Thread(
            target=lambda: refused.append(first.refuse_window(wid, model, "client lacks composition")),
            daemon=True,
        )
        refusal_thread.start()
        self.assertTrue(parent.cleanup_started.wait(2))
        allowed = []
        allow_thread = threading.Thread(
            target=lambda: allowed.append(first.allow_window(wid, model)), daemon=True,
        )
        allow_thread.start()
        allow_thread.join(0.05)
        self.assertTrue(allow_thread.is_alive())
        self.assertTrue(second.is_window_announced(wid, model))
        self.assertFalse(second.is_window_refused(wid, model))
        second.send.assert_not_called()

        release_cleanup.set()
        refusal_thread.join(2)
        allow_thread.join(2)
        self.assertFalse(refusal_thread.is_alive())
        self.assertFalse(allow_thread.is_alive())
        self.assertEqual(refused, [True])
        self.assertEqual(allowed, [True])
        first.send.assert_called_once_with(WINDOW_DESTROY, wid)
        self.assertFalse(first.is_window_refused(wid, model))

    def test_recreate_packets_precede_and_filter_stale_rgb(self) -> None:
        connection = self.make_connection()
        transport = self.make_transport(connection)
        wid = 0x10
        model = self.make_window_model()

        def queue_ordinary(packet_type, *parts, **kwargs) -> None:
            synchronous = bool(kwargs.get("synchronous", True))
            more = bool(kwargs.get("will_have_more", not synchronous))
            transport.ordinary_packets.append((Packet(packet_type, *parts), synchronous, more))

        connection.send = queue_ordinary
        self.assertTrue(connection._announce_window(
            wid, model, lambda: queue_ordinary(WINDOW_CREATE, wid),
        ))
        self.assertEqual(ClientConnection.next_packet(transport)[0].get_type(), WINDOW_CREATE)

        old_source = _FakePixelSource(wid)
        old_source.window = model
        old_source.statistics.damage_ack_pending = {}
        connection.window_sources[wid] = old_source
        connection.configure_pixel_source(old_source)
        old_sequence = old_source.allocate_damage_packet_sequence()
        old_packet = Packet(
            WINDOW_DRAW, wid, 0, 0, 1, 1, "rgb32", b"old", old_sequence, 4,
            {"backing-epoch": connection._backing_epochs.get(wid, 0)},
        )
        old_source.statistics.damage_ack_pending[old_sequence] = object()
        self.assertTrue(old_source.publish_damage_packet(
            wid, old_sequence, old_source,
            lambda: transport.packet_queue.append((old_packet, wid, 1, False)),
        ))

        self.assertTrue(connection.refuse_window(wid, model, "dynamic child"))
        self.assertTrue(connection.allow_window(wid, model))
        self.assertTrue(connection._announce_window(
            wid, model, lambda: queue_ordinary(WINDOW_CREATE, wid),
        ))
        new_source = _FakePixelSource(wid)
        new_source.window = model
        new_source.statistics.damage_ack_pending = {}
        connection.window_sources[wid] = new_source
        connection.configure_pixel_source(new_source)
        new_sequence = new_source.allocate_damage_packet_sequence()
        new_packet = Packet(
            WINDOW_DRAW, wid, 0, 0, 1, 1, "rgb32", b"new", new_sequence, 4,
            {"backing-epoch": connection._backing_epochs[wid]},
        )
        new_source.statistics.damage_ack_pending[new_sequence] = object()
        self.assertTrue(new_source.publish_damage_packet(
            wid, new_sequence, new_source,
            lambda: transport.packet_queue.append((new_packet, wid, 1, False)),
        ))

        packet_types = [ClientConnection.next_packet(transport)[0].get_type() for _ in range(3)]
        self.assertEqual(packet_types, [WINDOW_DESTROY, WINDOW_CREATE, WINDOW_DRAW])
        self.assertEqual(ClientConnection.next_packet(transport)[0].get_type(), "none")
        self.assertEqual(old_source.statistics.damage_ack_pending, {})
        self.assertNotIn((wid, old_sequence), connection._damage_packet_owners)
        self.assertIs(connection._damage_packet_owners[(wid, new_sequence)], new_source)
        self.assertIn(new_sequence, new_source.statistics.damage_ack_pending)

    def test_stale_mmap_queue_entry_becomes_unknown_window_drain(self) -> None:
        connection = self.make_connection()
        transport = self.make_transport(connection)
        source = _FakePixelSource(0x10)
        source.statistics.damage_ack_pending = {}
        connection.window_sources[source.wid] = source
        connection.configure_pixel_source(source)
        sequence = source.allocate_damage_packet_sequence()
        chunks = ((4096, 16),)
        data = chunks if BACKWARDS_COMPATIBLE else b""
        packet = Packet(
            WINDOW_DRAW, source.wid, 2, 3, 4, 5, "mmap", data, sequence, 16,
            {"backing-epoch": 0, "chunks": chunks},
        )
        source.statistics.damage_ack_pending[sequence] = object()
        self.assertTrue(source.publish_damage_packet(
            source.wid, sequence, source,
            # The invalidated continuation will never be queued.  Its terminal
            # drain must therefore close this transport batch.
            lambda: transport.packet_queue.append((packet, source.wid, 20, True)),
        ))
        connection._backing_epochs[source.wid] = 1

        drain, synchronous, more = ClientConnection.next_packet(transport)

        self.assertEqual((drain.get_type(), drain.get_wid()), (WINDOW_DRAW, -1))
        self.assertEqual(drain[2:], packet[2:])
        self.assertEqual(drain[7], data)
        self.assertEqual(drain.get_dict(10)["chunks"], chunks)
        self.assertTrue(synchronous)
        self.assertFalse(more)
        self.assertEqual(source.statistics.damage_ack_pending, {})
        self.assertEqual(connection._damage_packet_owners, {})

    def test_new_transaction_does_not_drop_committed_queued_transaction(self) -> None:
        connection = self.make_connection()
        transport = self.make_transport(connection)
        parent = _FakePixelSource(0x10)
        child = _FakePixelSource(0x20, parent.wid)
        connection.window_sources[parent.wid] = parent
        connection.subsurface_sources[child.wid] = child
        connection.subsurface_stacking[parent.wid] = (parent.wid, child.wid)
        for source in (parent, child):
            source.statistics.damage_ack_pending = {}
            connection.configure_pixel_source(source)
        connection._subsurface_transaction_ids[parent.wid] = 1
        connection._subsurface_composites[parent.wid] = {
            "running": True,
            "transaction_id": 1,
            "content_generations": ((parent, 0), (child, 0)),
            "snapshots_ready": True,
        }

        def make_packet(source, stage_index: int):
            sequence = source.allocate_damage_packet_sequence()
            packet = Packet(
                WINDOW_DRAW, parent.wid, 0, 0, 1, 1, "rgb32", b"rgba", sequence, 4,
                {
                    "subsurface-backing-epoch": 0,
                    "subsurface-composite": SUBSURFACE_COMPOSITE_MODE,
                    "subsurface-stage-count": 2,
                    "subsurface-stage-index": stage_index,
                    "subsurface-topology-epoch": 0,
                    "subsurface-transaction-id": 1,
                    "rgb_format": "BGRA",
                },
            )
            source.statistics.damage_ack_pending[sequence] = object()
            self.assertTrue(source.publish_damage_packet(
                parent.wid, sequence, source,
                lambda: transport.packet_queue.append((packet, source.wid, 1, stage_index == 0)),
                validator=lambda: connection.validate_damage_packet(source, packet),
            ))
            return packet

        first = make_packet(parent, 0)
        second = make_packet(child, 1)
        # A new transaction and topology are installed before the network
        # thread reaches either packet, and the child has already moved to a
        # different root. Strict publication authority has moved on, but both
        # stages already crossed the authoritative queue boundary in order.
        # Dropping the child stage would strand the old client transaction and
        # can lose unrelated pixels outside the reparent repair footprint.
        connection._subsurface_transaction_ids[parent.wid] = 2
        connection._subsurface_topology_epochs[parent.wid] = 1
        child.parent_wid = 0x30
        self.assertFalse(connection.validate_damage_packet(parent, first))

        self.assertIs(ClientConnection.next_packet(transport)[0], first)
        self.assertIs(ClientConnection.next_packet(transport)[0], second)
        self.assertEqual(ClientConnection.next_packet(transport)[0].get_type(), "none")
        self.assertEqual(len(connection._damage_packet_owners), 2)

    def test_root_only_topology_does_not_activate_composition(self) -> None:
        connection = self.make_connection()
        parent = _FakePixelSource(0x10)
        connection.window_sources[parent.wid] = parent
        connection.configure_pixel_source(parent)

        with source_scheduler("idle") as idle_add:
            connection.update_subsurface_geometries(parent.wid, (), (parent.wid,))

        self.assertEqual(connection.subsurface_stacking[parent.wid], (parent.wid,))
        self.assertNotIn(parent.wid, connection._subsurface_composites)
        self.assertNotIn(parent.wid, connection._subsurface_topology_epochs)
        idle_add.assert_not_called()

    def test_zero_sized_last_child_does_not_poison_parent_publication(self) -> None:
        connection = self.make_connection()
        parent = _FakePixelSource(0x10)
        child = _FakePixelSource(0x20, parent.wid)
        connection.window_sources[parent.wid] = parent
        connection.subsurface_sources[child.wid] = child
        connection.subsurface_stacking[parent.wid] = (parent.wid, child.wid)
        connection.configure_pixel_source(parent)
        connection.configure_pixel_source(child)

        with source_scheduler("idle") as idle_add:
            connection.cleanup_subsurface_source(child.wid)

        self.assertEqual(connection.subsurface_stacking[parent.wid], (parent.wid,))
        self.assertEqual(connection._subsurface_topology_epochs[parent.wid], 1)
        self.assertNotIn(parent.wid, connection._subsurface_composites)
        idle_add.assert_not_called()
        packet = Packet(WINDOW_DRAW, parent.wid, 0, 0, 1, 1, "rgb32", b"x", 1, 4, {"backing-epoch": 0})
        self.assertTrue(connection.validate_damage_packet(parent, packet))

    def test_composite_packet_validation_rechecks_raw_formats_and_all_sources(self) -> None:
        connection = self.make_connection()
        parent = _FakePixelSource(0x10)
        child = _FakePixelSource(0x20, parent.wid)
        connection.window_sources[parent.wid] = parent
        connection.subsurface_sources[child.wid] = child
        connection.subsurface_stacking[parent.wid] = (parent.wid, child.wid)
        connection.configure_pixel_source(parent)
        connection.configure_pixel_source(child)
        connection._subsurface_transaction_ids[parent.wid] = 7
        connection._subsurface_composites[parent.wid] = {
            "running": True,
            "transaction_id": 7,
            "content_generations": ((parent, 0), (child, 0)),
            "snapshots_ready": True,
        }
        parent.supports_transparency = False
        parent.rgb_formats = ("BGRX", "RGBX")

        def make_packet(coding="rgb32", rgb_format="BGRA", **extra_options):
            options = {
                "subsurface-backing-epoch": 0,
                "subsurface-composite": SUBSURFACE_COMPOSITE_MODE,
                "subsurface-stage-count": 2,
                "subsurface-stage-index": 0,
                "subsurface-topology-epoch": 0,
                "subsurface-transaction-id": 7,
                "rgb_format": rgb_format,
            }
            options.update(extra_options)
            return Packet(
                WINDOW_DRAW, parent.wid, 0, 0, 1, 1, coding, b"rgba", 1, 4, options,
            )

        self.assertTrue(connection.validate_damage_packet(parent, make_packet(rgb_format="BGRX")))
        self.assertTrue(connection.validate_damage_packet(child, make_packet(rgb_format="BGRA")))
        self.assertFalse(connection.validate_damage_packet(parent, make_packet(coding="rgb24")))
        self.assertFalse(connection.validate_damage_packet(parent, make_packet(rgb_format="RGB")))
        self.assertFalse(connection.validate_damage_packet(parent, make_packet(lz4=1)))
        self.assertFalse(connection.validate_damage_packet(parent, make_packet(zstd=1)))

        connection.subsurface_composite_modes = ()
        self.assertFalse(connection.validate_damage_packet(parent, make_packet()))
        connection.subsurface_composite_modes = (SUBSURFACE_COMPOSITE_MODE,)

        child.image_filter = SimpleNamespace(enabled=False)
        self.assertFalse(connection.validate_damage_packet(parent, make_packet()))

        child.image_filter = NoFilter
        child.window = _SnapshotWindow()
        connection._subsurface_composites[parent.wid]["content_generations"] = (
            (parent, 0), (child, 0),
        )
        child.window.generation = 1
        # Every stage now owns pixels captured in the same UI iteration.  A
        # later native commit queues a successor instead of revoking the
        # immutable transaction which is still reaching the packet queue.
        self.assertTrue(connection.validate_damage_packet(parent, make_packet()))
        self.assertTrue(connection.validate_damage_packet(parent, make_packet(), committed=True))

    def test_idle_schedule_failure_parks_and_new_damage_recovers_transaction(self) -> None:
        connection = self.make_connection()
        parent = _FakePixelSource(0x10)
        child = _FakePixelSource(0x20, parent.wid)
        child.update_geometry(parent.wid, 0, 0, 20, 20, 20, 20)
        connection.window_sources[parent.wid] = parent
        connection.subsurface_sources[child.wid] = child
        connection.subsurface_stacking[parent.wid] = (parent.wid, child.wid)
        connection.configure_pixel_source(parent)
        connection.configure_pixel_source(child)

        with source_scheduler("idle", side_effect=RuntimeError("idle failed")):
            connection.request_subsurface_composite(parent.wid, parent_region=(2, 3, 10, 11))

        state = connection._subsurface_composites[parent.wid]
        self.assertFalse(state["running"])
        self.assertIsNone(state["token"])
        self.assertEqual(state["pending_region"], (2, 3, 10, 11))
        self.assertEqual(state["failures"], 1)

        def run_now(callback, *args):
            callback(*args)
            return 1

        with source_scheduler("idle", side_effect=run_now):
            connection.request_subsurface_composite(parent.wid, parent_region=(4, 5, 2, 2))

        self.assertNotIn(parent.wid, connection._subsurface_composites)

    def test_parent_removal_cancels_owned_idle_callback(self) -> None:
        connection = self.make_connection()
        parent = _FakePixelSource(0x10)
        child = _FakePixelSource(0x20, parent.wid)
        child.update_geometry(parent.wid, 2, 3, 10, 11, 10, 11)
        connection.window_sources[parent.wid] = parent
        connection.subsurface_sources[child.wid] = child
        connection.subsurface_stacking[parent.wid] = (parent.wid, child.wid)
        connection.configure_pixel_source(parent)
        connection.configure_pixel_source(child)
        scheduled = []

        def retain(callback, *args):
            scheduled.append((callback, args))
            return 41

        with (
            source_scheduler("idle", side_effect=retain) as idle_sources,
            patch("xpra.server.source.window.GLib.source_remove") as source_remove,
        ):
            connection.request_subsurface_composite(
                parent.wid, parent_region=(2, 3, 10, 11),
            )
            self.assertEqual(len(connection._subsurface_glib_sources), 1)

            connection.remove_window(parent.wid, parent.window)

            source_remove.assert_not_called()
            idle_sources.destroyed.assert_called_once_with(idle_sources.sources[0])
            self.assertEqual(connection._subsurface_glib_sources, {})
            callback, args = scheduled[0]
            self.assertFalse(callback(*args))
            self.assertNotIn(parent.wid, connection._subsurface_composites)

    def test_removal_cancels_idle_registration_which_finishes_late(self) -> None:
        connection = self.make_connection()
        parent = _FakePixelSource(0x10)
        child = _FakePixelSource(0x20, parent.wid)
        child.update_geometry(parent.wid, 2, 3, 10, 11, 10, 11)
        connection.window_sources[parent.wid] = parent
        connection.subsurface_sources[child.wid] = child
        connection.subsurface_stacking[parent.wid] = (parent.wid, child.wid)
        connection.configure_pixel_source(parent)
        connection.configure_pixel_source(child)
        scheduler_entered = threading.Event()
        scheduler_release = threading.Event()
        scheduled = []

        def delayed_registration(callback, *args):
            scheduled.append((callback, args))
            scheduler_entered.set()
            self.assertTrue(scheduler_release.wait(2))
            return 42

        request = threading.Thread(
            target=connection.request_subsurface_composite,
            kwargs={"parent_wid": parent.wid, "parent_region": (2, 3, 10, 11)},
            daemon=True,
        )
        with (
            source_scheduler("idle", side_effect=delayed_registration) as idle_sources,
            patch("xpra.server.source.window.GLib.source_remove") as source_remove,
        ):
            request.start()
            self.assertTrue(scheduler_entered.wait(2))
            connection.remove_window(parent.wid, parent.window)
            scheduler_release.set()
            request.join(2)

            self.assertFalse(request.is_alive())
            source_remove.assert_not_called()
            idle_sources.destroyed.assert_called_once_with(idle_sources.sources[0])
            self.assertEqual(connection._subsurface_glib_sources, {})
            callback, args = scheduled[0]
            self.assertFalse(callback(*args))

    def test_real_glib_sources_survive_early_dispatch_and_late_attachment(self) -> None:
        from xpra.server.source.window import GLib

        for kind in ("idle", "watchdog"):
            for cancel in (False, True):
                with self.subTest(kind=kind, cancel=cancel):
                    connection = self.make_connection()
                    token = object()
                    connection._subsurface_composites[0x10] = {"token": token}
                    context = GLib.MainContext.new()
                    callback = Mock()
                    real_source = (
                        GLib.idle_source_new() if kind == "idle" else GLib.timeout_source_new(0)
                    )
                    successor = GLib.idle_source_new()

                    class EarlySource:
                        def set_callback(self, dispatch):
                            real_source.set_callback(dispatch)

                        def attach(self, _context):
                            source_id = real_source.attach(context)
                            if cancel:
                                connection._cancel_subsurface_glib_sources(0x10)
                            else:
                                self_test.assertTrue(context.iteration(False))
                                self_test.assertTrue(real_source.is_destroyed())
                            successor.set_callback(lambda *_args: False)
                            successor.attach(context)
                            return source_id

                        def destroy(self):
                            real_source.destroy()

                    self_test = self
                    owner = EarlySource()
                    try:
                        with patch.object(GLib, "source_remove") as numeric_remove:
                            result = connection._register_subsurface_glib_source(
                                0x10, token, kind, lambda: owner, callback,
                            )
                            self.assertIs(result, None if cancel else owner)
                            self.assertEqual(callback.call_count, 0 if cancel else 1)
                            self.assertTrue(real_source.is_destroyed())
                            self.assertFalse(successor.is_destroyed())
                            self.assertEqual(connection._subsurface_glib_sources, {})
                            self.assertFalse(connection._remove_subsurface_glib_source(
                                owner, 0x10, token, kind,
                            ))
                            self.assertFalse(successor.is_destroyed())
                            numeric_remove.assert_not_called()
                            self.assertTrue(context.iteration(False))
                            self.assertTrue(successor.is_destroyed())
                    finally:
                        real_source.destroy()
                        successor.destroy()

    def test_source_registration_failure_releases_only_its_reservation(self) -> None:
        for phase in ("construct", "callback", "attach"):
            with self.subTest(phase=phase):
                connection = self.make_connection()
                token = object()
                connection._subsurface_composites[0x10] = {"token": token}
                source = Mock()
                source.attach.return_value = 1
                failure = RuntimeError("source " + phase + " failed")
                if phase == "callback":
                    source.set_callback.side_effect = failure
                elif phase == "attach":
                    source.attach.side_effect = failure
                create = Mock(return_value=source)
                if phase == "construct":
                    create.side_effect = failure
                with self.assertRaisesRegex(RuntimeError, "source .* failed"):
                    connection._register_subsurface_glib_source(
                        0x10, token, "idle", create, Mock(),
                    )
                self.assertEqual(connection._subsurface_glib_sources, {})
                self.assertEqual(source.destroy.call_count, 0 if phase == "construct" else 1)

    def test_refusal_cancels_watchdog_and_late_stage_completion(self) -> None:
        connection = self.make_connection()
        model = self.make_window_model()
        parent = _FakePixelSource(0x10)
        child = _FakePixelSource(0x20, parent.wid)
        parent.window = model
        parent.auto_publish = False
        child.update_geometry(parent.wid, 2, 3, 10, 11, 10, 11)
        connection.window_sources[parent.wid] = parent
        connection.subsurface_sources[child.wid] = child
        connection.subsurface_stacking[parent.wid] = (parent.wid, child.wid)
        connection.configure_pixel_source(parent)
        connection.configure_pixel_source(child)
        self.assertTrue(connection._announce_window(parent.wid, model, lambda: None))
        connection.send = Mock()
        watchdogs = []

        def run_now(callback, *args):
            callback(*args)
            return 1

        def retain_watchdog(_delay, callback):
            watchdogs.append(callback)
            return 77

        with (
            source_scheduler("idle", side_effect=run_now),
            source_scheduler("timeout", side_effect=retain_watchdog) as watchdog_sources,
            patch("xpra.server.source.window.GLib.source_remove") as source_remove,
        ):
            connection.request_subsurface_composite(
                parent.wid, parent_region=(2, 3, 10, 11),
            )
            self.assertEqual(len(watchdogs), 1)
            self.assertEqual(len(connection._subsurface_glib_sources), 1)
            parent_image = parent.captured_images[0]
            child_image = child.captured_images[0]
            self.assertEqual((parent_image.free_count, child_image.free_count), (0, 0))

            self.assertTrue(connection.refuse_window(parent.wid, model, "unsupported composition"))

            source_remove.assert_not_called()
            watchdog_sources.destroyed.assert_called_once_with(watchdog_sources.sources[0])
            self.assertEqual(connection._subsurface_glib_sources, {})
            # Teardown owns the untouched child snapshot, while the encode
            # completion retains the already-handed root snapshot.
            self.assertEqual((parent_image.free_count, child_image.free_count), (0, 1))
            self.assertFalse(watchdogs[0]())
            parent.packet_completions[0](True)
            self.assertEqual((parent_image.free_count, child_image.free_count), (1, 1))
            self.assertEqual(connection._subsurface_glib_sources, {})
            self.assertNotIn(parent.wid, connection._subsurface_composites)

    def test_connection_cleanup_releases_only_unclaimed_composite_snapshots(self) -> None:
        connection = self.make_connection()
        parent = _FakePixelSource(0x10)
        child = _FakePixelSource(0x20, parent.wid)
        parent.auto_publish = False
        child.update_geometry(parent.wid, 2, 3, 10, 11, 10, 11)
        connection.window_sources[parent.wid] = parent
        connection.subsurface_sources[child.wid] = child
        connection.subsurface_stacking[parent.wid] = (parent.wid, child.wid)
        connection.configure_pixel_source(parent)
        connection.configure_pixel_source(child)

        def run_now(callback, *args):
            callback(*args)
            return 1

        with (
            source_scheduler("idle", side_effect=run_now),
            source_scheduler("timeout", return_value=78) as watchdog_sources,
            patch("xpra.server.source.window.GLib.source_remove") as source_remove,
        ):
            connection.request_subsurface_composite(
                parent.wid, parent_region=(2, 3, 10, 11),
            )
            self.assertEqual(len(parent.packet_completions), 1)
            parent_image = parent.captured_images[0]
            child_image = child.captured_images[0]
            self.assertEqual((parent_image.free_count, child_image.free_count), (0, 0))

            late_completion = parent.packet_completions.pop(0)
            connection.cleanup()

            source_remove.assert_not_called()
            watchdog_sources.destroyed.assert_called_once_with(watchdog_sources.sources[0])
            self.assertEqual((parent_image.free_count, child_image.free_count), (0, 1))
            self.assertEqual(connection._subsurface_composites, {})
            self.assertEqual(connection._subsurface_glib_sources, {})
            self.assertEqual(connection.window_sources, {})
            self.assertEqual(connection.subsurface_sources, {})

            # The encode owner releases the already-claimed root wrapper.  Its
            # late stage completion cannot recreate state or a GLib callback.
            late_completion(True)
            self.assertEqual((parent_image.free_count, child_image.free_count), (1, 1))
            self.assertEqual(connection._subsurface_composites, {})
            self.assertEqual(connection._subsurface_glib_sources, {})
            source_remove.assert_not_called()
            watchdog_sources.destroyed.assert_called_once_with(watchdog_sources.sources[0])

    def test_connection_cleanup_cancels_owned_idle_callback(self) -> None:
        connection = self.make_connection()
        parent = _FakePixelSource(0x10)
        connection.window_sources[parent.wid] = parent
        connection.subsurface_stacking[parent.wid] = (parent.wid,)
        connection.configure_pixel_source(parent)
        scheduled = []

        def retain(callback, *args):
            scheduled.append((callback, args))
            return 43

        with (
            source_scheduler("idle", side_effect=retain) as idle_sources,
            patch("xpra.server.source.window.GLib.source_remove") as source_remove,
        ):
            connection.request_subsurface_composite(
                parent.wid, parent_region=(0, 0, 10, 11),
            )
            connection.cleanup()

            source_remove.assert_not_called()
            idle_sources.destroyed.assert_called_once_with(idle_sources.sources[0])
            self.assertEqual(connection._subsurface_glib_sources, {})
            callback, args = scheduled[0]
            self.assertFalse(callback(*args))

    def test_reparent_property_error_still_schedules_both_backing_repairs(self) -> None:
        connection = self.make_connection()
        old_parent = _FakePixelSource(0x10)
        new_parent = _FakePixelSource(0x11)
        child = _FakePixelSource(0x20, old_parent.wid)
        child.update_geometry(old_parent.wid, 4, 5, 20, 10, 20, 10)
        connection.window_sources = {
            old_parent.wid: old_parent,
            new_parent.wid: new_parent,
        }
        connection.subsurface_sources[child.wid] = child
        connection.subsurface_stacking = {
            old_parent.wid: (old_parent.wid, child.wid),
            new_parent.wid: (new_parent.wid,),
        }
        for source in (old_parent, new_parent, child):
            connection.configure_pixel_source(source)
        connection.apply_subsurface_client_properties = Mock(
            side_effect=RuntimeError("property replay failed"),
        )
        scheduled = []

        def retain(callback, *args):
            scheduled.append((callback, args))
            return len(scheduled)

        with (
            source_scheduler("idle", side_effect=retain),
            self.assertRaisesRegex(RuntimeError, "property replay failed"),
        ):
            connection.update_subsurface_geometry(
                child.wid, new_parent.wid, 7, 8, 20, 10, 20, 10,
            )

        self.assertEqual(
            {record["parent-wid"] for record in connection._subsurface_glib_sources.values()},
            {old_parent.wid, new_parent.wid},
        )
        self.assertTrue(connection._subsurface_composites[old_parent.wid]["running"])
        self.assertTrue(connection._subsurface_composites[new_parent.wid]["running"])

        # The already-published repair schedules remain usable even though
        # client-property replay reported its independent failure.
        with source_scheduler("idle", side_effect=retain):
            while scheduled:
                callback, args = scheduled.pop(0)
                callback(*args)
        old_transaction = next(
            args[6]["subsurface-transaction-id"]
            for name, args in old_parent.calls if name == "region"
        )
        new_transaction = next(
            args[6]["subsurface-transaction-id"]
            for name, args in new_parent.calls if name == "region"
        )
        child_transaction = next(
            args[6]["subsurface-transaction-id"]
            for name, args in child.calls if name == "region"
        )
        self.assertGreater(new_transaction, old_transaction)
        self.assertEqual(child_transaction, new_transaction)
        self.assertEqual(connection._subsurface_transaction_id, new_transaction)
        self.assertNotIn(old_parent.wid, connection._subsurface_composites)
        self.assertNotIn(new_parent.wid, connection._subsurface_composites)

    def test_direct_child_capture_always_joins_exact_composition(self) -> None:
        connection = self.make_connection()
        parent = _FakePixelSource(0x10)
        child = WindowSource.__new__(WindowSource)
        child.wid = 0x20
        child.parent_wid = parent.wid
        child.offset_x = 7
        child.offset_y = 9
        child.logical_width = 40
        child.logical_height = 30
        child.native_width = 40
        child.native_height = 30
        child.window_dimensions = (40, 30)
        child._sequence = 0
        child._encoders = {"rgb32": object()}
        child.core_encodings = ("rgb32",)
        child.image_filter = NoFilter
        child.window = Mock()
        child.window.is_managed.return_value = True
        child.window.get_image.return_value = None
        child.may_update_window_dimensions = Mock(return_value=(40, 30))
        child.is_cancelled = Mock(return_value=False)
        connection.window_sources[parent.wid] = parent
        connection.subsurface_sources[child.wid] = child
        connection.subsurface_stacking[parent.wid] = (parent.wid,)
        connection.configure_pixel_source(parent)
        connection.configure_pixel_source(child)
        completed = Mock()

        with source_scheduler("idle", return_value=1) as idle_add:
            queued = WindowSource.process_damage_region(
                child, 1.0, 2, 3, 11, 13, "rgb32", {}, packet_complete=completed,
            )

        self.assertFalse(queued)
        child.window.get_image.assert_not_called()
        completed.assert_called_once_with(False)
        self.assertEqual(
            connection._subsurface_composites[parent.wid]["pending_region"],
            (9, 12, 11, 13),
        )
        idle_add.assert_called_once()

        child.window.get_image.reset_mock()
        queued = WindowSource.process_damage_region(
            child, 2.0, 2, 3, 11, 13, "rgb32",
            {"subsurface-composite": SUBSURFACE_COMPOSITE_MODE},
        )
        self.assertFalse(queued)
        child.window.get_image.assert_called_once_with(2, 3, 11, 13)

    def test_decode_error_full_refresh_defers_after_consuming_refresh_state(self) -> None:
        from time import monotonic, sleep
        from xpra.os_util import gi_import

        source = WindowSource.__new__(WindowSource)
        source.schedule_auto_refresh = Mock()
        source.wid = 0x10
        source.window = Mock()
        source.window.is_managed.return_value = True
        source.auto_refresh_encodings = ("rgb32",)
        source.is_cancelled = Mock(return_value=False)
        source.refresh_regions = [object()]
        source.window_dimensions = (64, 48)
        source.defer_damage = Mock(return_value=True)
        source.send_delayed_regions = Mock()
        source.decode_error_refresh_timer = 0
        source.global_statistics = SimpleNamespace(decode_errors=0)

        # Exercise the actual scheduling boundary. Upstream consumes the slot
        # in the body; the generic lifecycle case consumes its owned lease
        # before entering that body. Neither composition permits bypassing
        # its scheduler by assigning a fake ID and calling the body directly.
        try:
            source.client_decode_error(-1, "fixed regression decode failure")
            self.assertGreater(source.decode_error_refresh_timer, 0)
            context = gi_import("GLib").main_context_default()
            deadline = monotonic() + 2
            while source.decode_error_refresh_timer and monotonic() < deadline:
                context.iteration(False)
                sleep(0.001)
        finally:
            pending_timer = source.decode_error_refresh_timer
            source.cancel_decode_error_refresh_timer()

        self.assertEqual(pending_timer, 0, "decode-error callback did not consume its timer")
        self.assertEqual(source.decode_error_refresh_timer, 0)
        self.assertEqual(source.refresh_regions, [])
        source.defer_damage.assert_called_once_with(source, 0, 0, 64, 48)
        source.send_delayed_regions.assert_not_called()

    def test_global_sequences_route_colliding_parent_wid_to_exact_source(self) -> None:
        connection = self.make_connection()
        parent = _FakePixelSource(0x10)
        child = _FakePixelSource(0x20, 0x10)
        connection.window_sources[parent.wid] = parent
        connection.subsurface_sources[child.wid] = child
        connection.configure_pixel_source(parent)
        connection.configure_pixel_source(child)

        parent_sequence = parent.allocate_damage_packet_sequence()
        child_sequence = child.allocate_damage_packet_sequence()
        self.assertEqual((parent_sequence, child_sequence), (1, 2))
        self.assertTrue(parent.publish_damage_packet(0x10, parent_sequence, parent, lambda: None))
        self.assertTrue(child.publish_damage_packet(0x10, child_sequence, child, lambda: None))

        connection.client_ack_damage(child_sequence, child.wid, 20, 10, 0, "wrong wire wid")
        child.damage_packet_acked.assert_not_called()
        parent.damage_packet_acked.assert_not_called()
        connection.client_ack_damage(child_sequence, 0x10, 20, 10, 1, "child")
        connection.client_ack_damage(parent_sequence, 0x10, 40, 30, 1, "parent")
        child.damage_packet_acked.assert_called_once_with(child_sequence, 20, 10, 1, "child")
        parent.damage_packet_acked.assert_called_once_with(parent_sequence, 40, 30, 1, "parent")
        self.assertEqual(
            [call.args[0] for call in connection.may_recalculate.call_args_list],
            [child.wid, parent.wid],
        )
        self.assertEqual(connection._damage_packet_owners, {})

    def test_info_exposes_bounded_registry_state_and_flattenable_children(self) -> None:
        from xpra.server.source.source_stats import GlobalPerformanceStatistics

        connection = self.make_connection()
        parent = _FakePixelSource(0x10)
        child = _FakePixelSource(0x20, parent.wid)
        parent.get_info = Mock(side_effect=lambda: {"damage": {"ack-pending": 0}})
        child.get_info = Mock(side_effect=lambda: {"damage": {"ack-pending": 1}})
        child.offset_x = 3
        child.offset_y = 5
        connection.window_sources[parent.wid] = parent
        connection.subsurface_sources[child.wid] = child
        connection.configure_pixel_source(parent)
        connection.configure_pixel_source(child)
        sequence = child.allocate_damage_packet_sequence()
        child.publish_damage_packet(parent.wid, sequence, child, lambda: None)
        connection.packet_queue = ()
        connection.encode_queue_size = Mock(return_value=0)
        connection.global_batch_config = None
        connection.statistics = GlobalPerformanceStatistics()
        connection.statistics.packet_count = 11
        connection.statistics.damage_events_count = 13
        connection.statistics.packet_qsizes.append((0, 7))
        connection.statistics.compression_work_qsizes.append((0, 9))

        info = connection.get_window_info()

        self.assertEqual(info["damage"]["next-packet-sequence"], sequence + 1)
        self.assertEqual(info["damage"]["ack-owners"], 1)
        self.assertEqual(info["damage"]["active-pixel-sources"], 2)
        self.assertEqual(info["damage"]["subsurface-pending"], 0)
        self.assertEqual(info["damage"]["subsurface-inflight"], 0)
        self.assertEqual(info["damage"]["packets_sent"], 11)
        self.assertEqual(info["damage"]["events"], 13)
        self.assertEqual(info["damage"]["packet_queue"]["size"]["current"], 0)
        self.assertEqual(info["damage"]["packet_queue"]["size"]["max"], 7)
        self.assertEqual(info["damage"]["compression_queue"]["size"]["current"], 0)
        self.assertEqual(info["damage"]["data_queue"]["size"]["max"], 9)
        self.assertIn("connection", info)
        self.assertEqual(info["encoding"]["decode_errors"], 0)
        subsurfaces = info["windows"][parent.wid]["subsurfaces"]
        self.assertEqual(subsurfaces, {
            child.wid: {
                "offset": (3, 5),
                "info": {"damage": {"ack-pending": 1}},
            },
        })

        connection.cleanup_subsurface_source(child.wid)
        info = connection.get_window_info()
        self.assertEqual(info["damage"]["ack-owners"], 0)
        self.assertEqual(info["damage"]["active-pixel-sources"], 1)
        self.assertNotIn("subsurfaces", info["windows"][parent.wid])

    def test_info_distinguishes_composite_successors_from_packet_drain(self) -> None:
        connection = self.make_connection()
        connection.packet_queue = ()
        connection.encode_queue_size = Mock(return_value=0)
        connection.global_batch_config = None
        connection.statistics = None
        for states, expected in (
                ({}, (0, 0)),
                ({1: {"pending_region": (0, 0, 4, 4), "running": False}}, (1, 0)),
                ({1: {"pending_region": None, "running": True}}, (0, 1)),
                ({1: {"pending_region": (0, 0, 4, 4), "running": True}}, (1, 1)),
                ({1: {"pending_region": None, "running": True},
                  2: {"pending_region": (0, 0, 4, 4), "running": False}}, (1, 1)),
                ({}, (0, 0)),
        ):
            with self.subTest(states=states):
                connection._subsurface_composites = states
                damage = connection.get_window_info()["damage"]
                self.assertEqual(damage["ack-owners"], 0)
                self.assertEqual(damage["compression_queue"]["size"]["current"], 0)
                self.assertEqual(
                    (damage["subsurface-pending"], damage["subsurface-inflight"]), expected,
                )

    def test_damage_logs_bind_child_publish_and_ack_to_wire_parent(self) -> None:
        connection = self.make_connection()
        source = WindowSource.__new__(WindowSource)
        source.schedule_auto_refresh = Mock()
        source.wid = 0x20
        source.statistics = SimpleNamespace(
            damage_ack_pending={}, damage_in_latency=[], last_packet_time=0,
            packet_count=0, encoding_totals={},
        )
        source.global_statistics = SimpleNamespace(packet_count=0)
        source.encoding_last_used = ""
        source.publish_damage_packet = connection.publish_damage_packet
        source.queue_packet = Mock()
        source.damage_packet_acked = Mock()
        connection.subsurface_sources[source.wid] = source
        connection.configure_pixel_source(source)
        sequence = source.allocate_damage_packet_sequence()
        packet = Packet(WINDOW_DRAW, 0x10, 0, 0, 1, 1, "rgb24", b"x", sequence, 4, {})

        with (
            patch("xpra.server.window.compress.SCREEN_UPDATES_DIRECTORY", ""),
            patch("xpra.server.window.compress.damagelog") as publish_log,
            patch("xpra.server.source.window.damagelog") as ack_log,
        ):
            WindowSource.queue_damage_packet(source, packet, 1.0, 1.0, typedict())
            connection.client_ack_damage(sequence, 0x10, 1, 1, 1, "ok")

        publish_log.assert_called_once_with(
            "subsurface draw packet sequence %s from source window %#x "
            "published as wire window %#x using %s",
            sequence, source.wid, 0x10, "rgb24",
        )
        ack_log.assert_called_once_with(
            "draw acknowledgement sequence %s for wire window %#x routed to subsurface window %#x",
            sequence, 0x10, source.wid,
        )
        source.damage_packet_acked.assert_called_once_with(sequence, 1, 1, 1, "ok")

    def test_connection_ack_diagnostics_cannot_bypass_source_cleanup(self) -> None:
        connection = self.make_connection()
        child = _FakePixelSource(0x20, 0x10)
        decode_times = Mock()
        decode_times.append.side_effect = RuntimeError("decode statistics failed")
        connection.statistics.client_decode_time = decode_times
        connection.subsurface_sources[child.wid] = child
        connection.configure_pixel_source(child)
        sequence = child.allocate_damage_packet_sequence()
        child.publish_damage_packet(child.parent_wid, sequence, child, lambda: None)

        with patch(
                "xpra.server.source.window.damagelog",
                side_effect=RuntimeError("ack logging failed"),
        ):
            connection.client_ack_damage(sequence, child.parent_wid, 1, 1, 1, "ok")

        decode_times.append.assert_called_once()
        child.damage_packet_acked.assert_called_once_with(sequence, 1, 1, 1, "ok")
        connection.may_recalculate.assert_called_once_with(child.wid, 1)
        self.assertEqual(connection._damage_packet_owners, {})
        self.assertEqual(connection._damage_packet_active_ops, {})

    def test_source_ack_clears_pending_before_optional_statistics(self) -> None:
        source = WindowSource.__new__(WindowSource)
        source.schedule_auto_refresh = Mock()
        source.wid = 0x20
        decode_times = Mock()
        decode_times.append.side_effect = RuntimeError("decode statistics failed")
        pending = {
            7: (1.0, "rgb32", 1, 4, {"frame": 0}, 1.0),
        }
        source.statistics = SimpleNamespace(
            damage_ack_pending=pending,
            client_decode_time=decode_times,
        )
        latency = Mock(side_effect=RuntimeError("latency statistics failed"))
        source.global_statistics = SimpleNamespace(record_latency=latency)
        source._damage_delayed = None
        source.soft_expired = 1

        WindowSource.damage_packet_acked(source, 7, 1, 1, 1, "ok")

        self.assertEqual(pending, {})
        decode_times.append.assert_called_once()
        latency.assert_called_once()
        self.assertEqual(source.soft_expired, 0)

    def test_composite_transaction_uses_full_order_and_one_reset(self) -> None:
        connection = self.make_connection()
        parent = _FakePixelSource(0x10)
        below = _FakePixelSource(0x20, parent.wid)
        above = _FakePixelSource(0x21, parent.wid)
        parent.window_dimensions = (100, 80)
        below.update_geometry(parent.wid, 0, 0, 60, 50, 60, 50)
        above.update_geometry(parent.wid, 20, 10, 30, 20, 30, 20)
        connection.window_sources[parent.wid] = parent
        connection.subsurface_sources = {below.wid: below, above.wid: above}
        connection.subsurface_stacking[parent.wid] = (below.wid, parent.wid, above.wid)
        for source in (parent, below, above):
            connection.configure_pixel_source(source)

        def run_now(callback, *args):
            callback(*args)
            return 1

        with source_scheduler("idle", side_effect=run_now):
            connection.request_subsurface_composite(
                parent.wid, parent_region=(10, 5, 50, 45),
            )

        regions = [
            next(args for name, args in source.calls if name == "region")
            for source in (below, parent, above)
        ]
        self.assertEqual(
            [(args[1], args[2], args[3], args[4]) for args in regions],
            [(10, 5, 50, 45), (10, 5, 50, 45), (0, 0, 30, 20)],
        )
        self.assertEqual([args[5] for args in regions], ["rgb32"] * 3)
        self.assertEqual([args[7] for args in regions], [2, 1, 0])
        options = [args[6] for args in regions]
        self.assertEqual(
            [value.get("subsurface-reset") for value in options],
            [(10, 5, 50, 45), None, None],
        )
        for index, value in enumerate(options):
            self.assertEqual(value["subsurface-composite"], SUBSURFACE_COMPOSITE_MODE)
            self.assertTrue(value["preserve-premultiplied-alpha"])
            self.assertIs(value["alpha"], True)
            self.assertIs(value["lz4"], False)
            self.assertEqual(value["rgb_formats"], SUBSURFACE_COMPOSITE_FORMATS)
            self.assertIs(value["zstd"], False)
            self.assertEqual(value["window-size"], (100, 80))
            self.assertEqual(value["subsurface-backing-epoch"], 0)
            self.assertEqual(value["subsurface-topology-epoch"], 0)
            self.assertEqual(value["subsurface-transaction-id"], 1)
            self.assertEqual(value["subsurface-stage-index"], index)
            self.assertEqual(value["subsurface-stage-count"], 3)
            self.assertNotIn("backing-epoch", value)
        for source in (below, parent, above):
            self.assertEqual(len(source.captured_images), 1)
            self.assertEqual(source.captured_images[0].free_count, 1)
        self.assertNotIn(parent.wid, connection._subsurface_composites)

    def test_composite_without_installed_rgb32_refuses_root_before_capture(self) -> None:
        connection = self.make_connection()
        parent = _FakePixelSource(0x10)
        child = _FakePixelSource(0x20, parent.wid)
        parent._encoders = {"png": object()}
        child.update_geometry(parent.wid, 0, 0, 20, 20, 20, 20)
        connection.window_sources[parent.wid] = parent
        connection.subsurface_sources[child.wid] = child
        connection.subsurface_stacking[parent.wid] = (parent.wid, child.wid)
        connection.configure_pixel_source(parent)
        connection.configure_pixel_source(child)

        def run_now(callback, *args):
            callback(*args)
            return 1

        with source_scheduler("idle", side_effect=run_now):
            connection.request_subsurface_composite(
                parent.wid, parent_region=(0, 0, 20, 20),
            )

        self.assertFalse(any(name == "region" for name, _args in parent.calls))
        self.assertFalse(any(name == "region" for name, _args in child.calls))
        self.assertTrue(connection.is_window_refused(parent.wid, parent.window))
        self.assertNotIn(parent.wid, connection.window_sources)
        self.assertNotIn(child.wid, connection.subsurface_sources)
        self.assertNotIn(parent.wid, connection._subsurface_composites)

    def test_configured_filter_refuses_the_whole_composite_before_capture(self) -> None:
        for filtered_role in ("parent", "child"):
            with self.subTest(filtered_role=filtered_role):
                connection = self.make_connection()
                parent = _FakePixelSource(0x10)
                child = _FakePixelSource(0x20, parent.wid)
                child.update_geometry(parent.wid, 0, 0, 20, 20, 20, 20)
                filtered = parent if filtered_role == "parent" else child
                filtered.image_filter = SimpleNamespace(enabled=False)
                connection.window_sources[parent.wid] = parent
                connection.subsurface_sources[child.wid] = child
                connection.subsurface_stacking[parent.wid] = (parent.wid, child.wid)
                connection.configure_pixel_source(parent)
                connection.configure_pixel_source(child)

                def run_now(callback, *args):
                    callback(*args)
                    return 1

                with source_scheduler("idle", side_effect=run_now):
                    connection.request_subsurface_composite(
                        parent.wid, parent_region=(0, 0, 20, 20),
                    )

                self.assertFalse(any(name == "region" for name, _args in parent.calls))
                self.assertFalse(any(name == "region" for name, _args in child.calls))
                self.assertTrue(connection.is_window_refused(parent.wid, parent.window))
                self.assertEqual(connection.window_sources, {})
                self.assertEqual(connection.subsurface_sources, {})

    def test_eligibility_change_after_preflight_refuses_before_capture(self) -> None:
        connection = self.make_connection()
        parent = _FakePixelSource(0x10)
        child = _FakePixelSource(0x20, parent.wid)
        child.update_geometry(parent.wid, 0, 0, 20, 20, 20, 20)
        connection.window_sources[parent.wid] = parent
        connection.subsurface_sources[child.wid] = child
        connection.subsurface_stacking[parent.wid] = (parent.wid, child.wid)
        connection.configure_pixel_source(parent)
        connection.configure_pixel_source(child)

        def change_eligibility():
            child.image_filter = SimpleNamespace(enabled=False)
            return parent.window_dimensions

        parent.may_update_window_dimensions = change_eligibility

        def run_now(callback, *args):
            callback(*args)
            return 1

        with source_scheduler("idle", side_effect=run_now):
            connection.request_subsurface_composite(
                parent.wid, parent_region=(0, 0, 20, 20),
            )

        self.assertFalse(any(name == "region" for name, _args in parent.calls))
        self.assertFalse(any(name == "region" for name, _args in child.calls))
        self.assertTrue(connection.is_window_refused(parent.wid, parent.window))
        self.assertEqual(connection.window_sources, {})
        self.assertEqual(connection.subsurface_sources, {})
        self.assertNotIn(parent.wid, connection._subsurface_composites)

    def test_second_layer_capture_failure_releases_snapshots_and_rearms(self) -> None:
        connection = self.make_connection()
        parent = _FakePixelSource(0x10)
        child = _FakePixelSource(0x20, parent.wid)
        child.update_geometry(parent.wid, 0, 0, 20, 20, 20, 20)
        connection.window_sources[parent.wid] = parent
        connection.subsurface_sources[child.wid] = child
        connection.subsurface_stacking[parent.wid] = (parent.wid, child.wid)
        connection.configure_pixel_source(parent)
        connection.configure_pixel_source(child)
        capture_child = child.capture_damage_image
        child.capture_damage_image = Mock(side_effect=RuntimeError("child snapshot failed"))

        def run_now(callback, *args):
            callback(*args)
            return 1

        with source_scheduler("idle", side_effect=run_now):
            connection.request_subsurface_composite(
                parent.wid, parent_region=(0, 0, 20, 20),
            )

            state = connection._subsurface_composites[parent.wid]
            self.assertFalse(state["running"])
            self.assertIsNone(state["token"])
            self.assertEqual(state["failures"], 3)
            self.assertEqual(state["pending_region"], (0, 0, 20, 20))
            self.assertEqual(child.capture_damage_image.call_count, 3)
            self.assertEqual(len(parent.captured_images), 3)
            self.assertTrue(all(image.free_count == 1 for image in parent.captured_images))
            self.assertFalse(child.captured_images)

            # Fresh damage rearms the bounded failure state.  The same atomic
            # capture now succeeds and both newly owned wrappers reach one
            # terminal owner without retaining the parked transaction.
            child.capture_damage_image = capture_child
            connection.request_subsurface_composite(
                parent.wid, parent_region=(5, 6, 2, 3),
            )

        self.assertNotIn(parent.wid, connection._subsurface_composites)
        self.assertEqual(len(parent.captured_images), 4)
        self.assertEqual(len(child.captured_images), 1)
        for source in (parent, child):
            self.assertTrue(all(image.free_count == 1 for image in source.captured_images))

    def test_first_composite_topology_rebuilds_the_complete_parent(self) -> None:
        connection = self.make_connection()
        parent = _FakePixelSource(0x10)
        child = _FakePixelSource(0x20, parent.wid)
        parent.window_dimensions = (100, 80)
        connection.window_sources[parent.wid] = parent
        connection.subsurface_sources[child.wid] = child
        connection.configure_pixel_source(parent)
        connection.configure_pixel_source(child)

        def run_now(callback, *args):
            callback(*args)
            return 1

        with source_scheduler("idle", side_effect=run_now):
            connection.update_subsurface_geometries(
                parent.wid,
                ((child.wid, 20, 10, 30, 20, 30, 20),),
                (parent.wid, child.wid),
            )

        parent_region = next(args for name, args in parent.calls if name == "region")
        child_region = next(args for name, args in child.calls if name == "region")
        self.assertEqual(
            (parent_region[1], parent_region[2], parent_region[3], parent_region[4]),
            (0, 0, 100, 80),
        )
        self.assertEqual(parent_region[6]["subsurface-reset"], (0, 0, 100, 80))
        self.assertEqual(
            (child_region[1], child_region[2], child_region[3], child_region[4]),
            (0, 0, 30, 20),
        )

    def test_topology_change_restarts_after_actual_publication(self) -> None:
        connection = self.make_connection()
        parent = _FakePixelSource(0x10)
        child = _FakePixelSource(0x20, parent.wid)
        parent.window_dimensions = (100, 80)
        parent.auto_publish = False
        child.update_geometry(parent.wid, 0, 0, 20, 20, 20, 20)
        connection.window_sources[parent.wid] = parent
        connection.subsurface_sources[child.wid] = child
        connection.subsurface_stacking[parent.wid] = (parent.wid, child.wid)
        connection.configure_pixel_source(parent)
        connection.configure_pixel_source(child)

        def run_now(callback, *args):
            callback(*args)
            return 1

        with source_scheduler("idle", side_effect=run_now):
            connection.request_subsurface_composite(
                parent.wid, parent_region=(0, 0, 20, 20),
            )
            self.assertEqual(len(parent.packet_completions), 1)
            self.assertFalse(any(name == "region" for name, _args in child.calls))
            first_parent_image = parent.captured_images[0]
            first_child_image = child.captured_images[0]
            self.assertEqual((first_parent_image.free_count, first_child_image.free_count), (0, 0))

            connection.update_subsurface_geometry(
                child.wid, parent.wid, 10, 0, 20, 20, 20, 20,
            )
            parent.packet_completions.pop(0)(True)

            # The completed encoder owns the first wrapper; aborting the old
            # topology releases only the later, still-unconsumed child wrapper.
            self.assertEqual((first_parent_image.free_count, first_child_image.free_count), (1, 1))

            # The packet from the old topology was published, but no later
            # layer from that transaction may follow it.  The replacement
            # transaction starts with a fresh reset over the bounding union.
            self.assertEqual(len(parent.packet_completions), 1)
            parent_regions = [args for name, args in parent.calls if name == "region"]
            self.assertEqual(len(parent_regions), 2)
            self.assertEqual(parent_regions[1][6]["subsurface-reset"], (0, 0, 30, 20))
            self.assertFalse(any(name == "region" for name, _args in child.calls))

            parent.packet_completions.pop(0)(True)

        child_region = next(args for name, args in child.calls if name == "region")
        self.assertEqual((child_region[1], child_region[2], child_region[3], child_region[4]), (0, 0, 20, 20))
        self.assertNotIn("subsurface-reset", child_region[6])
        for source in (parent, child):
            self.assertTrue(all(image.free_count == 1 for image in source.captured_images))
        self.assertNotIn(parent.wid, connection._subsurface_composites)

    def test_content_change_after_capture_finishes_then_repairs(self) -> None:
        connection = self.make_connection()
        parent = _FakePixelSource(0x10)
        child = _FakePixelSource(0x20, parent.wid)
        parent.auto_publish = False
        child.update_geometry(parent.wid, 0, 0, 20, 20, 20, 20)
        child.window = _SnapshotWindow()
        connection.window_sources[parent.wid] = parent
        connection.subsurface_sources[child.wid] = child
        connection.subsurface_stacking[parent.wid] = (parent.wid, child.wid)
        connection.configure_pixel_source(parent)
        connection.configure_pixel_source(child)

        def run_now(callback, *args):
            callback(*args)
            return 1

        with source_scheduler("idle", side_effect=run_now):
            connection.request_subsurface_composite(
                parent.wid, parent_region=(0, 0, 20, 20),
            )
            first_parent = parent.packet_completions.pop(0)
            self.assertFalse(any(name == "region" for name, _args in child.calls))
            self.assertEqual([image.generation for image in parent.captured_images], [0])
            self.assertEqual([image.generation for image in child.captured_images], [0])

            # A native child commit lands while the root stage is encoding.
            # The already-captured child raster remains transaction 1's stage;
            # the damage notification records generation 1 for a successor.
            child.window.generation = 1
            connection.request_subsurface_composite(
                parent.wid, parent_region=(0, 0, 20, 20),
            )
            first_parent(True)

            parent_regions = [args for name, args in parent.calls if name == "region"]
            child_regions = [args for name, args in child.calls if name == "region"]
            self.assertEqual(len(parent_regions), 2)
            self.assertEqual(
                [args[6]["subsurface-transaction-id"] for args in parent_regions],
                [1, 2],
            )
            self.assertEqual(parent_regions[1][6]["subsurface-reset"], (0, 0, 20, 20))
            self.assertEqual(len(child_regions), 1)
            self.assertEqual(child_regions[0][6]["subsurface-transaction-id"], 1)
            self.assertEqual(child_regions[0][8], 0)
            self.assertEqual([image.generation for image in child.captured_images], [0, 1])

            parent.packet_completions.pop(0)(True)

        child_regions = [args for name, args in child.calls if name == "region"]
        self.assertEqual(
            [args[6]["subsurface-transaction-id"] for args in child_regions],
            [1, 2],
        )
        self.assertEqual([args[6]["subsurface-stage-index"] for args in child_regions], [1, 1])
        self.assertEqual([args[8] for args in child_regions], [0, 1])
        for source in (parent, child):
            self.assertTrue(source.captured_images)
            self.assertTrue(all(image.free_count == 1 for image in source.captured_images))
        self.assertNotIn(parent.wid, connection._subsurface_composites)

    def test_continuous_content_changes_complete_each_captured_transaction(self) -> None:
        connection = self.make_connection()
        parent = _FakePixelSource(0x10)
        child = _FakePixelSource(0x20, parent.wid)
        parent.auto_publish = False
        child.update_geometry(parent.wid, 0, 0, 20, 20, 20, 20)
        child.window = _SnapshotWindow()
        connection.window_sources[parent.wid] = parent
        connection.subsurface_sources[child.wid] = child
        connection.subsurface_stacking[parent.wid] = (parent.wid, child.wid)
        connection.configure_pixel_source(parent)
        connection.configure_pixel_source(child)
        idles = []

        def retain(callback, *args):
            idles.append((callback, args))
            return len(idles) + 100

        def run_idle() -> None:
            self.assertTrue(idles)
            callback, args = idles.pop(0)
            self.assertFalse(callback(*args))

        with (
            source_scheduler("idle", side_effect=retain),
            source_scheduler("timeout", return_value=99),
            patch("xpra.server.source.window.GLib.source_remove"),
        ):
            connection.request_subsurface_composite(
                parent.wid, parent_region=(0, 0, 20, 20),
            )
            run_idle()

            # Keep replacing the native child raster before the current root
            # encode completes.  Each immutable two-stage snapshot must reach
            # its final stage; newer damage may only create one successor.
            for generation in range(1, 11):
                child.window.generation = generation
                connection.request_subsurface_composite(
                    parent.wid, parent_region=(0, 0, 20, 20),
                )
                self.assertEqual(len(parent.packet_completions), 1)
                parent.packet_completions.pop(0)(True)
                run_idle()       # root completion queues captured child pixels
                run_idle()       # child completion finishes this transaction
                run_idle()       # one coalesced successor captures the latest raster
                self.assertFalse(idles)

            parent_regions = [args for name, args in parent.calls if name == "region"]
            child_regions = [args for name, args in child.calls if name == "region"]
            self.assertEqual(
                [args[6]["subsurface-transaction-id"] for args in parent_regions],
                list(range(1, 12)),
            )
            self.assertEqual(
                [args[6]["subsurface-transaction-id"] for args in child_regions],
                list(range(1, 11)),
            )
            self.assertEqual([args[8] for args in child_regions], list(range(10)))
            self.assertEqual(len(connection._subsurface_glib_sources), 1)

            # Quiescence completes the last captured generation without an
            # extra root retry or an orphan GLib callback.
            parent.packet_completions.pop(0)(True)
            run_idle()
            run_idle()

        self.assertFalse(idles)
        child_regions = [args for name, args in child.calls if name == "region"]
        self.assertEqual(
            [args[6]["subsurface-transaction-id"] for args in child_regions],
            list(range(1, 12)),
        )
        self.assertEqual([args[8] for args in child_regions], list(range(11)))
        for source in (parent, child):
            self.assertEqual(len(source.captured_images), 11)
            self.assertTrue(all(image.free_count == 1 for image in source.captured_images))
        self.assertNotIn(parent.wid, connection._subsurface_composites)
        self.assertEqual(connection._subsurface_glib_sources, {})

    def test_failed_composite_is_bounded_and_new_damage_rearms_it(self) -> None:
        connection = self.make_connection()
        parent = _FakePixelSource(0x10)
        parent.auto_publish = False
        connection.window_sources[parent.wid] = parent
        connection.subsurface_stacking[parent.wid] = (parent.wid,)
        connection.configure_pixel_source(parent)

        def run_now(callback, *args):
            callback(*args)
            return 1

        with source_scheduler("idle", side_effect=run_now):
            connection.request_subsurface_composite(
                parent.wid, parent_region=(2, 3, 10, 11),
            )
            for attempt in range(3):
                self.assertEqual(len(parent.packet_completions), 1)
                self.assertEqual(
                    len([1 for name, _args in parent.calls if name == "region"]),
                    attempt + 1,
                )
                parent.packet_completions.pop(0)(False)

            state = connection._subsurface_composites[parent.wid]
            self.assertFalse(state["running"])
            self.assertEqual(state["failures"], 3)
            self.assertEqual(state["pending_region"], (2, 3, 10, 11))

            parent.auto_publish = True
            connection.request_subsurface_composite(
                parent.wid, parent_region=(6, 7, 2, 2),
            )

        attempts = [
            args[6]["subsurface-transaction-id"]
            for name, args in parent.calls if name == "region"
        ]
        self.assertEqual(attempts, [1, 2, 3, 4])
        self.assertNotIn(parent.wid, connection._subsurface_composites)

    def test_stage_watchdog_bounds_hang_and_late_completion_cannot_publish(self) -> None:
        connection = self.make_connection()
        parent = _FakePixelSource(0x10)
        parent.auto_publish = False
        connection.window_sources[parent.wid] = parent
        connection.subsurface_stacking[parent.wid] = (parent.wid,)
        connection.configure_pixel_source(parent)
        timers = []

        def run_now(callback, *args):
            callback(*args)
            return 1

        def add_timeout(delay, callback, *args):
            timers.append((delay, callback, args))
            return len(timers)

        with (
            source_scheduler("idle", side_effect=run_now),
            source_scheduler("timeout", side_effect=add_timeout),
            patch("xpra.server.source.window.GLib.source_remove"),
        ):
            connection.request_subsurface_composite(
                parent.wid, parent_region=(2, 3, 10, 11),
            )
            connection.request_subsurface_composite(
                parent.wid, parent_region=(20, 4, 2, 2),
            )

            for attempt in range(3):
                self.assertGreater(len(timers), attempt)
                _delay, callback, args = timers[attempt]
                callback(*args)
                state = connection._subsurface_composites[parent.wid]
                if attempt == 0:
                    self.assertEqual(state["failures"], 1)
                    # Damage accumulated while the retry is running must not
                    # reset the bounded consecutive-failure count.
                    connection.request_subsurface_composite(
                        parent.wid, parent_region=(1, 1, 1, 1),
                    )
                    self.assertEqual(state["failures"], 1)

            state = connection._subsurface_composites[parent.wid]
            self.assertFalse(state["running"])
            self.assertEqual(state["failures"], 3)
            old_completions = tuple(parent.packet_completions)
            self.assertEqual(len(old_completions), 3)

            for complete in old_completions:
                complete(True)
            self.assertIs(connection._subsurface_composites[parent.wid], state)
            self.assertFalse(state["running"])
            self.assertEqual(state["failures"], 3)

            parent.auto_publish = True
            connection.request_subsurface_composite(
                parent.wid, parent_region=(6, 7, 2, 2),
            )

        timed_out_sequences = [
            args[0] for name, args in parent.calls
            if name == "cancel" and args and args[0]
        ]
        self.assertEqual(timed_out_sequences, [1, 2, 3])
        self.assertNotIn(parent.wid, connection._subsurface_composites)

    def test_watchdog_schedule_failure_uses_bounded_transaction_retries(self) -> None:
        connection = self.make_connection()
        parent = _FakePixelSource(0x10)
        parent.auto_publish = False
        connection.window_sources[parent.wid] = parent
        connection.subsurface_stacking[parent.wid] = (parent.wid,)
        connection.configure_pixel_source(parent)

        def run_now(callback, *args):
            callback(*args)
            return 1

        with (
            source_scheduler("idle", side_effect=run_now),
            source_scheduler("timeout",
                side_effect=RuntimeError("watchdog schedule failed"),
            ) as timeout_add,
        ):
            connection.request_subsurface_composite(
                parent.wid, parent_region=(2, 3, 10, 11),
            )

        state = connection._subsurface_composites[parent.wid]
        self.assertFalse(state["running"])
        self.assertIsNone(state["token"])
        self.assertEqual(state["pending_region"], (2, 3, 10, 11))
        self.assertEqual(state["failures"], 3)
        self.assertEqual(timeout_add.call_count, 3)
        attempts = [
            args[6]["subsurface-transaction-id"]
            for name, args in parent.calls if name == "region"
        ]
        self.assertEqual(attempts, [1, 2, 3])
        for complete in parent.packet_completions:
            complete(True)
        self.assertIs(connection._subsurface_composites[parent.wid], state)

    def test_unmap_remap_epochs_restart_with_a_fresh_full_transaction(self) -> None:
        connection = self.make_connection()
        parent = _FakePixelSource(0x10)
        child = _FakePixelSource(0x20, parent.wid)
        parent.auto_publish = False
        child.update_geometry(parent.wid, 5, 6, 20, 10, 20, 10)
        connection.window_sources[parent.wid] = parent
        connection.subsurface_sources[child.wid] = child
        connection.subsurface_stacking[parent.wid] = (parent.wid, child.wid)
        connection.configure_pixel_source(parent)
        connection.configure_pixel_source(child)

        def run_now(callback, *args):
            callback(*args)
            return 1

        with (
            source_scheduler("idle", side_effect=run_now),
            source_scheduler("timeout", return_value=99),
            patch("xpra.server.source.window.GLib.source_remove"),
        ):
            connection.request_subsurface_composite(
                parent.wid, parent_region=(5, 6, 20, 10),
            )
            first = parent.packet_completions.pop(0)
            connection.unmap_window(parent.wid, parent.window)
            self.assertEqual(connection._backing_epochs[parent.wid], 1)
            first(True)

            second = parent.packet_completions.pop(0)
            connection.map_window(parent.wid, parent.window, (1, 2))
            self.assertEqual(connection._backing_epochs[parent.wid], 2)
            second(True)

            third = parent.packet_completions.pop(0)
            third(True)

        parent_regions = [args for name, args in parent.calls if name == "region"]
        self.assertEqual(
            [args[6]["subsurface-transaction-id"] for args in parent_regions],
            [1, 2, 3],
        )
        self.assertEqual(
            [args[6]["subsurface-backing-epoch"] for args in parent_regions],
            [0, 1, 2],
        )
        self.assertEqual(parent_regions[-1][6]["subsurface-reset"], (0, 0, 64, 48))
        self.assertNotIn(parent.wid, connection._subsurface_composites)

    def test_resize_fences_queued_pixels_before_control_and_recomposites(self) -> None:
        connection = self.make_connection()
        transport = self.make_transport(connection)
        parent = _FakePixelSource(0x10)
        child = _FakePixelSource(0x20, parent.wid)
        window = parent.window
        child.update_geometry(parent.wid, 5, 6, 20, 10, 20, 10)
        connection.window_sources[parent.wid] = parent
        connection.subsurface_sources[child.wid] = child
        connection.subsurface_stacking[parent.wid] = (parent.wid, child.wid)
        connection._client_backing_sizes[parent.wid] = parent.window_dimensions
        connection._subsurface_transaction_ids[parent.wid] = 1
        for source in (parent, child):
            source.statistics.damage_ack_pending = {}
            connection.configure_pixel_source(source)
        connection._subsurface_composites[parent.wid] = {
            "running": True,
            "transaction_id": 1,
            "content_generations": ((parent, 0), (child, 0)),
            "snapshots_ready": True,
        }

        sequence = parent.allocate_damage_packet_sequence()
        stale = Packet(
            WINDOW_DRAW, parent.wid, 0, 0, 1, 1, "rgb32", b"rgba", sequence, 4,
            {
                "subsurface-backing-epoch": 0,
                "subsurface-composite": SUBSURFACE_COMPOSITE_MODE,
                "subsurface-stage-count": 2,
                "subsurface-stage-index": 0,
                "subsurface-topology-epoch": 0,
                "subsurface-transaction-id": 1,
                "rgb_format": "BGRA",
            },
        )
        parent.statistics.damage_ack_pending[sequence] = object()
        self.assertTrue(parent.publish_damage_packet(
            parent.wid, sequence, parent,
            lambda: transport.packet_queue.append((stale, parent.wid, 1, False)),
            validator=lambda: connection.validate_damage_packet(parent, stale),
        ))
        # The packet is now a committed member of a complete queued
        # transaction; resize starts from idle composite state.
        connection._subsurface_composites.pop(parent.wid)

        controls = []
        connection.can_send_window_model = Mock(return_value=True)
        connection.update_window_visibility = Mock(return_value=False)
        connection.send = lambda *args: controls.append(
            (args, connection._backing_epochs[parent.wid]),
        )
        parent.window_dimensions = (80, 60)

        def run_now(callback, *args):
            callback(*args)
            return 1

        with (
            source_scheduler("idle", side_effect=run_now),
            source_scheduler("timeout", return_value=99),
            patch("xpra.server.source.window.GLib.source_remove"),
        ):
            connection.resize_window(parent.wid, window, 80, 60, 7)

        self.assertEqual(controls, [(("window-resized", parent.wid, 80, 60, 7), 1)])
        self.assertEqual(connection._backing_epochs[parent.wid], 1)
        self.assertEqual(ClientConnection.next_packet(transport)[0].get_type(), "none")
        parent_regions = [args for name, args in parent.calls if name == "region"]
        self.assertTrue(parent_regions)
        self.assertEqual(parent_regions[-1][6]["subsurface-backing-epoch"], 1)
        self.assertEqual(parent_regions[-1][6]["subsurface-reset"], (0, 0, 80, 60))

        # A move-only control retains the same wire canvas and does not fence
        # or schedule another composite transaction.
        parent.calls.clear()
        connection.move_resize_window(parent.wid, window, 4, 5, 80, 60, 8)
        self.assertEqual(connection._backing_epochs[parent.wid], 1)
        self.assertFalse([args for name, args in parent.calls if name == "region"])

    def test_packet_completion_reports_the_queue_publication_result_once(self) -> None:
        source = WindowSource.__new__(WindowSource)
        source.schedule_auto_refresh = Mock()
        source.statistics = SimpleNamespace(encoding_pending={})
        source.claim_damage_packet_publication = Mock()
        source.release_damage_packet_publication = Mock()
        packet = Packet(WINDOW_DRAW, 0x10, 0, 0, 1, 1, "rgb32", b"rgba", 1, 4, {})
        source.make_data_packet = Mock(return_value=packet)
        image = Mock()

        for published in (False, True):
            with self.subTest(published=published):
                completed = Mock()
                source.queue_damage_packet = Mock(return_value=published)
                WindowSource.make_data_packet_cb(
                    source, 1, 1, 1.0, 1.0, image, "rgb32", 7,
                    typedict({"_damage-packet-complete": completed}),
                )
                completed.assert_called_once_with(published)
                source.queue_damage_packet.assert_called_once()

    def test_encoding_pending_failure_releases_image_and_completes(self) -> None:
        class BrokenPending(dict):
            def __setitem__(self, _key, _value):
                raise RuntimeError("pending registration failed")

        source = WindowSource.__new__(WindowSource)
        source.schedule_auto_refresh = Mock()
        source.statistics = SimpleNamespace(encoding_pending=BrokenPending())
        source.is_cancelled = Mock(return_value=False)
        source.make_data_packet = Mock()
        image = Mock()
        image.is_thread_safe.return_value = True
        completed = Mock()

        WindowSource.make_data_packet_cb(
            source, 1, 1, 1.0, 1.0, image, "rgb32", 7,
            typedict({"_damage-packet-complete": completed}),
        )

        source.make_data_packet.assert_not_called()
        image.free.assert_called_once_with()
        completed.assert_called_once_with(False)

    def test_completion_runs_even_when_mmap_lease_release_fails(self) -> None:
        source = WindowSource.__new__(WindowSource)
        source.schedule_auto_refresh = Mock()
        source.statistics = SimpleNamespace(encoding_pending={})
        lease = object()
        source.claim_damage_packet_publication = Mock(return_value=lease)
        source.release_damage_packet_publication = Mock(side_effect=RuntimeError("lease release failed"))
        source.make_data_packet = Mock(
            return_value=Packet(
                WINDOW_DRAW, 0x10, 0, 0, 1, 1, "mmap", b"", 1, 4,
                {"chunks": ((4096, 4),)},
            ),
        )
        source.queue_damage_packet = Mock(return_value=True)
        image = Mock()
        image.is_thread_safe.return_value = True
        completed = Mock()

        with self.assertRaisesRegex(RuntimeError, "lease release failed"):
            WindowSource.make_data_packet_cb(
                source, 1, 1, 1.0, 1.0, image, "mmap", 7,
                typedict({"_damage-packet-complete": completed}),
            )

        source.release_damage_packet_publication.assert_called_once_with(source, lease)
        completed.assert_called_once_with(True)

    def test_draw_packet_uses_shared_sequence_and_retains_local_attempt_count(self) -> None:
        source = WindowSource.__new__(WindowSource)
        source.schedule_auto_refresh = Mock()
        source.wid = 0x20
        source._damage_packet_sequence = 7
        source.allocate_damage_packet_sequence = Mock(return_value=41)
        source._draw_packet_target = lambda x, y, _options=None: (0x10, x + 3, y + 4)
        source.snapshot_damage_options = WindowSource.snapshot_damage_options
        source.global_statistics = SimpleNamespace(packet_count=0)
        source.statistics = SimpleNamespace(packet_count=0, encoding_totals={})
        source.encoding_last_used = ""

        client_options = {}
        packet = WindowSource.make_draw_packet(
            source, 1, 2, 10, 20, "rgb24", b"pixels", 40, client_options, typedict({
                "window-size": (100, 80),
                "subsurface-composite": SUBSURFACE_COMPOSITE_MODE,
                "subsurface-backing-epoch": 3,
                "subsurface-topology-epoch": 4,
                "subsurface-transaction-id": 5,
                "subsurface-stage-index": 0,
                "subsurface-stage-count": 2,
            }),
        )
        self.assertEqual((packet.get_wid(), packet.get_u64(8)), (0x10, 41))
        self.assertEqual((packet.get_i16(2), packet.get_i16(3)), (4, 6))
        self.assertEqual(client_options["window-size"], (100, 80))
        self.assertEqual(client_options["subsurface-backing-epoch"], 3)
        self.assertEqual(client_options["subsurface-topology-epoch"], 4)
        self.assertEqual(client_options["subsurface-transaction-id"], 5)
        self.assertEqual(client_options["subsurface-stage-index"], 0)
        self.assertEqual(client_options["subsurface-stage-count"], 2)
        self.assertEqual(source._damage_packet_sequence, 8)
        # Packet construction consumes identities, but sent counters advance
        # only after the connection publication boundary succeeds.
        self.assertEqual(source.statistics.packet_count, 0)

        source.allocate_damage_packet_sequence = WindowSource.allocate_damage_packet_sequence.__get__(source)
        packet = WindowSource.make_draw_packet(
            source, 0, 0, 1, 1, "rgb24", b"x", 4, {}, {},
        )
        self.assertEqual(packet.get_u64(8), 8)
        self.assertEqual(source._damage_packet_sequence, 9)

    def test_detached_source_rejects_normal_packets_but_drains_mmap(self) -> None:
        connection = self.make_connection()
        pending = {}
        queued = []
        source = WindowSource.__new__(WindowSource)
        source.schedule_auto_refresh = Mock()
        source.wid = 0x20
        source.statistics = SimpleNamespace(
            damage_ack_pending=pending, damage_in_latency=[], last_packet_time=0,
        )
        source.queue_packet = lambda *args: queued.append(args)
        connection.subsurface_sources[source.wid] = source
        connection.configure_pixel_source(source)
        publication_lease = source.claim_damage_packet_publication(source)
        self.assertIsNotNone(publication_lease)

        detached = threading.Event()

        def detach() -> None:
            connection.unregister_damage_packets(source)
            detached.set()

        detach_thread = threading.Thread(target=detach, daemon=True)
        detach_thread.start()
        for _ in range(100):
            with connection._damage_packet_lock:
                if id(source) not in connection._damage_packet_sources:
                    break
            threading.Event().wait(0.01)
        self.assertFalse(detached.is_set())

        packet = Packet(WINDOW_DRAW, 0x10, 0, 0, 1, 1, "rgb24", b"x", 1, 4, {})
        WindowSource.queue_damage_packet(source, packet, 1.0, 1.0, typedict())
        self.assertEqual(pending, {})
        self.assertEqual(queued, [])
        self.assertEqual(connection._damage_packet_owners, {})

        mmap_packet = Packet(
            WINDOW_DRAW, 0x10, 0, 0, 1, 1, "mmap", b"", 2, 4,
            {"chunks": ((4096, 4),)},
        )
        WindowSource.queue_damage_packet(source, mmap_packet, 2.0, 2.0, typedict())
        self.assertEqual(pending, {})
        self.assertEqual(queued, [])

        WindowSource.queue_damage_packet(
            source, mmap_packet, 2.0, 2.0, typedict(), publication_lease=publication_lease,
        )
        detach_thread.join(2)
        self.assertFalse(detach_thread.is_alive())
        self.assertTrue(detached.is_set())
        self.assertEqual(len(queued), 1)
        drain_packet, queue_wid, pixels, flush = queued[0]
        self.assertEqual((drain_packet.get_wid(), queue_wid, pixels, flush), (-1, source.wid, 0, False))
        self.assertEqual(drain_packet[2:], mmap_packet[2:])
        self.assertEqual(connection._damage_packet_owners, {})
        self.assertEqual(connection._damage_packet_publication_leases, {})

        WindowSource.queue_damage_packet(
            source, mmap_packet, 2.0, 2.0, typedict(), publication_lease=publication_lease,
        )
        self.assertEqual(len(queued), 1)

        connection.configure_pixel_source(source)
        closing_lease = source.claim_damage_packet_publication(source)
        connection._damage_packet_closing = True
        closing_packet = Packet(
            WINDOW_DRAW, 0x10, 0, 0, 1, 1, "mmap", b"", 3, 4,
            {"chunks": ((4100, 4),)},
        )
        WindowSource.queue_damage_packet(
            source, closing_packet, 3.0, 3.0, typedict(), publication_lease=closing_lease,
        )
        self.assertEqual(len(queued), 1)
        self.assertEqual(connection._damage_packet_publication_leases, {})

    def test_publication_cannot_consume_another_sources_mmap_lease(self) -> None:
        connection = self.make_connection()
        first = _FakePixelSource(0x20, 0x10)
        second = _FakePixelSource(0x21, 0x10)
        connection.subsurface_sources = {first.wid: first, second.wid: second}
        connection.configure_pixel_source(first)
        connection.configure_pixel_source(second)
        lease = first.claim_damage_packet_publication(first)
        self.assertIsNotNone(lease)

        published = second.publish_damage_packet(
            0x10, second.allocate_damage_packet_sequence(), second,
            Mock(), publication_lease=lease,
        )

        self.assertFalse(published)
        self.assertIs(connection._damage_packet_publication_leases[lease], first)
        self.assertEqual(connection._damage_packet_active_ops, {id(first): 1})
        first.release_damage_packet_publication(first, lease)
        self.assertEqual(connection._damage_packet_publication_leases, {})
        self.assertEqual(connection._damage_packet_active_ops, {})

    def test_mmap_encode_keeps_its_claimed_area_during_detach(self) -> None:
        source = WindowSource.__new__(WindowSource)
        source.schedule_auto_refresh = Mock()
        source.rgb_formats = ("BGRX",)
        source.supports_transparency = True
        source.global_statistics = SimpleNamespace(mmap_bytes_sent=0, mmap_free_size=0)
        image = Mock()
        image.get_pixel_format.return_value = "BGRX"
        image.get_pixels.return_value = b"pixels"
        image.get_width.return_value = 1
        image.get_height.return_value = 1
        image.get_rowstride.return_value = 4
        area = Mock()

        def write_data(_data):
            source._mmap = None
            return ((4096, 6),)

        area.write_data.side_effect = write_data
        area.get_free_size.return_value = 8192
        source._mmap = area

        options = {}
        result = WindowSource.mmap_encode(source, "mmap", image, options)

        self.assertEqual(result[0], "mmap")
        self.assertEqual(result[2]["chunks"], ((4096, 6),))
        self.assertEqual(source.global_statistics.mmap_bytes_sent, 6)
        self.assertEqual(source.global_statistics.mmap_free_size, 8192)
        self.assertEqual(
            options[MMAP_DRAIN_DATA],
            (result[1], "BGRX", ((4096, 6),)),
        )
        area.get_free_size.assert_called_once_with()

    def test_post_mmap_write_failure_drains_exact_chunks_during_detach(self) -> None:
        connection = self.make_connection()
        queued = []
        source = WindowSource.__new__(WindowSource)
        source.schedule_auto_refresh = Mock()
        source.wid = 0x20
        source.rgb_formats = ("BGRX",)
        source.supports_transparency = True
        source.global_statistics = SimpleNamespace(mmap_bytes_sent=0, mmap_free_size=0)
        source.statistics = SimpleNamespace(encoding_pending={})
        source.is_cancelled = Mock(return_value=False)
        source.queue_packet = lambda *args: queued.append(args)
        area = Mock()
        area.write_data.return_value = ((4096, 6),)
        area.get_free_size.return_value = 8192
        source._mmap = area
        image = Mock()
        image.is_thread_safe.return_value = True
        image.get_pixel_format.return_value = "BGRX"
        image.get_pixels.return_value = b"pixels"
        image.get_width.return_value = 1
        image.get_height.return_value = 1
        image.get_rowstride.return_value = 4
        connection.subsurface_sources[source.wid] = source
        connection.configure_pixel_source(source)
        detached = threading.Event()
        detach_thread = None

        def fail_after_write(_damage_time, _process_damage_time, callback_image,
                             coding, _sequence, options, _flush):
            nonlocal detach_thread
            encoded = WindowSource.mmap_encode(source, coding, callback_image, options)
            self.assertEqual(encoded[2]["chunks"], ((4096, 6),))
            detach_thread = threading.Thread(
                target=lambda: (connection.unregister_damage_packets(source), detached.set()),
                daemon=True,
            )
            detach_thread.start()
            for _ in range(100):
                with connection._damage_packet_lock:
                    if id(source) not in connection._damage_packet_sources:
                        break
                threading.Event().wait(0.01)
            raise RuntimeError("post-mmap packet construction failed")

        source.make_data_packet = fail_after_write
        completed = Mock()
        WindowSource.make_data_packet_cb(
            source, 1, 1, 1.0, 1.0, image, "mmap", 7,
            typedict({"_damage-packet-complete": completed}),
        )

        self.assertIsNotNone(detach_thread)
        detach_thread.join(2)
        self.assertFalse(detach_thread.is_alive())
        self.assertTrue(detached.is_set())
        self.assertEqual(len(queued), 1)
        drain_packet, queue_wid, pixels, flush = queued[0]
        self.assertEqual((drain_packet.get_wid(), queue_wid, pixels, flush), (-1, 0, 0, False))
        self.assertEqual(drain_packet.get_str(6), "mmap")
        self.assertEqual(drain_packet.get_type(), WINDOW_DRAW)
        self.assertEqual(drain_packet[7], ((4096, 6),) if BACKWARDS_COMPATIBLE else b"")
        self.assertEqual(drain_packet.get_dict(10)["chunks"], ((4096, 6),))
        self.assertEqual(connection._damage_packet_owners, {})
        self.assertEqual(connection._damage_packet_publication_leases, {})
        self.assertEqual(connection._damage_packet_active_ops, {})
        completed.assert_called_once_with(False)
        image.free.assert_called_once_with()

    def test_post_mmap_validator_failure_restores_lease_for_exact_drain(self) -> None:
        connection = self.make_connection()
        queued = []
        source = WindowSource.__new__(WindowSource)
        source.schedule_auto_refresh = Mock()
        source.wid = 0x20
        source.statistics = SimpleNamespace(
            encoding_pending={}, damage_ack_pending={}, damage_in_latency=[], last_packet_time=0,
            packet_count=0, encoding_totals={},
        )
        source.global_statistics = SimpleNamespace(packet_count=0)
        source.encoding_last_used = ""
        source.is_cancelled = Mock(return_value=False)
        source.queue_packet = lambda *args: queued.append(args)
        source.save_update = Mock()
        connection.subsurface_sources[source.wid] = source
        connection.configure_pixel_source(source)
        source.validate_damage_packet = Mock(side_effect=RuntimeError("validator failed"))
        detach_started = threading.Event()
        detached = threading.Event()
        detach_thread = None
        chunks = ((8192, 12),)
        data = chunks if BACKWARDS_COMPATIBLE else b""

        def detach() -> None:
            detach_started.set()
            connection.unregister_damage_packets(source)
            detached.set()

        def make_packet(_damage_time, _process_damage_time, _image,
                        _coding, _sequence, options, _flush):
            nonlocal detach_thread
            options[MMAP_DRAIN_DATA] = (data, "BGRX", chunks)
            detach_thread = threading.Thread(target=detach, daemon=True)
            detach_thread.start()
            self.assertTrue(detach_started.wait(2))
            for _ in range(100):
                with connection._damage_packet_lock:
                    if id(source) not in connection._damage_packet_sources:
                        break
                threading.Event().wait(0.01)
            return Packet(WINDOW_DRAW, 0x10, 0, 0, 1, 1, "mmap", data, 7, 4, {
                "rgb_format": "BGRX", "chunks": chunks,
            })

        source.make_data_packet = make_packet
        completed = Mock()
        image = Mock()
        image.is_thread_safe.return_value = True
        with self.assertRaisesRegex(RuntimeError, "validator failed"):
            WindowSource.make_data_packet_cb(
                source, 1, 1, 1.0, 1.0, image, "mmap", 7,
                typedict({"_damage-packet-complete": completed}),
            )

        self.assertIsNotNone(detach_thread)
        detach_thread.join(2)
        self.assertFalse(detach_thread.is_alive())
        self.assertTrue(detached.is_set())
        self.assertEqual(len(queued), 1)
        drain_packet, queue_wid, pixels, flush = queued[0]
        self.assertEqual((drain_packet.get_wid(), queue_wid, pixels, flush), (-1, 0, 0, False))
        self.assertEqual(drain_packet.get_type(), WINDOW_DRAW)
        self.assertEqual(drain_packet[7], data)
        self.assertEqual(drain_packet.get_dict(10)["chunks"], chunks)
        self.assertEqual(connection._damage_packet_owners, {})
        self.assertEqual(connection._damage_packet_publication_leases, {})
        self.assertEqual(connection._damage_packet_active_ops, {})
        completed.assert_called_once_with(False)
        image.free.assert_called_once_with()

    def test_mmap_write_requires_and_releases_a_publication_lease(self) -> None:
        source = WindowSource.__new__(WindowSource)
        source.schedule_auto_refresh = Mock()
        source.statistics = SimpleNamespace(encoding_pending={})
        source.claim_damage_packet_publication = Mock(return_value=None)
        source.release_damage_packet_publication = Mock()
        source.make_data_packet = Mock()
        image = Mock()

        WindowSource.make_data_packet_cb(
            source, 1, 1, 1.0, 1.0, image, "mmap", 7, typedict(),
        )

        source.make_data_packet.assert_not_called()
        source.release_damage_packet_publication.assert_not_called()

        lease = object()
        source.claim_damage_packet_publication.return_value = lease
        source.make_data_packet.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            WindowSource.make_data_packet_cb(
                source, 1, 1, 2.0, 2.0, Mock(), "mmap", 8, typedict(),
            )
        source.release_damage_packet_publication.assert_called_once_with(source, lease)
        self.assertEqual(source.statistics.encoding_pending, {})

        source.release_damage_packet_publication.reset_mock()
        source.claim_damage_packet_publication.return_value = lease

        def mark_written_then_fail(_damage_time, _process_damage_time, _image,
                                   _coding, _sequence, options, _flush):
            chunks = ((4096, 4),)
            options[MMAP_DRAIN_DATA] = (chunks, "BGRX", chunks)
            return None

        source.make_data_packet.side_effect = mark_written_then_fail
        source.queue_mmap_drain = Mock(side_effect=RuntimeError("drain queue failed"))
        with self.assertRaisesRegex(RuntimeError, "drain queue failed"):
            WindowSource.make_data_packet_cb(
                source, 1, 1, 3.0, 3.0, Mock(), "mmap", 9, typedict(),
            )
        source.queue_mmap_drain.assert_called_once()
        source.release_damage_packet_publication.assert_called_once_with(source, lease)

    def test_standalone_mmap_drain_publishes_its_terminal_descriptor(self) -> None:
        source = WindowSource.__new__(WindowSource)
        source.schedule_auto_refresh = Mock()
        source.wid = 0x20
        source._damage_packet_sequence = 7
        source.allocate_damage_packet_sequence = WindowSource.allocate_damage_packet_sequence.__get__(source)
        source.publish_damage_packet = WindowSource.publish_damage_packet
        source.queue_packet = Mock()
        chunks = ((4096, 4),)
        lease = source.claim_damage_packet_publication(source)

        drained = WindowSource.queue_mmap_drain(
            source, (chunks, "BGRX", chunks), lease,
        )

        self.assertTrue(drained)
        source.queue_packet.assert_called_once()
        packet, queue_wid, pixels, flush = source.queue_packet.call_args.args
        self.assertEqual((packet.get_wid(), queue_wid, pixels, flush), (-1, 0, 0, False))
        self.assertEqual(packet.get_dict(10)["chunks"], chunks)

    def test_publish_exception_rolls_back_owner_and_pending_ack(self) -> None:
        connection = self.make_connection()
        source = WindowSource.__new__(WindowSource)
        source.schedule_auto_refresh = Mock()
        source.wid = 0x20
        source.statistics = SimpleNamespace(
            damage_ack_pending={}, damage_in_latency=[], last_packet_time=0,
            packet_count=3, encoding_totals={"rgb24": [2, 2]},
        )
        source.global_statistics = SimpleNamespace(packet_count=4)
        source.encoding_last_used = "png"
        source.publish_damage_packet = connection.publish_damage_packet
        source.queue_packet = Mock()
        source.save_update = Mock()
        connection.subsurface_sources[source.wid] = source
        connection.configure_pixel_source(source)
        packet = Packet(WINDOW_DRAW, 0x10, 0, 0, 1, 1, "rgb24", b"x", 1, 4, {})

        with patch("xpra.server.window.compress.SCREEN_UPDATES_DIRECTORY", "/enabled"):
            for error in (RuntimeError("queue failed"), KeyboardInterrupt()):
                with self.subTest(error=type(error).__name__):
                    source.queue_packet.side_effect = error
                    with self.assertRaises(type(error)):
                        WindowSource.queue_damage_packet(source, packet, 1.0, 1.0, typedict())
                    self.assertEqual(source.statistics.damage_ack_pending, {})
                    self.assertEqual(source.statistics.damage_in_latency, [])
                    self.assertEqual(source.statistics.last_packet_time, 0)
                    self.assertEqual(source.statistics.packet_count, 3)
                    self.assertEqual(source.statistics.encoding_totals, {"rgb24": [2, 2]})
                    self.assertEqual(source.global_statistics.packet_count, 4)
                    self.assertEqual(source.encoding_last_used, "png")
                    source.save_update.assert_not_called()
                    self.assertEqual(connection._damage_packet_owners, {})

    def test_published_packet_survives_diagnostic_artifact_failure(self) -> None:
        connection = self.make_connection()
        source = WindowSource.__new__(WindowSource)
        source.schedule_auto_refresh = Mock()
        source.wid = 0x20
        source.statistics = SimpleNamespace(
            damage_ack_pending={}, damage_in_latency=[], last_packet_time=0,
            packet_count=3, encoding_totals={"rgb24": [2, 2]},
        )
        source.global_statistics = SimpleNamespace(packet_count=4)
        source.encoding_last_used = "png"
        source.publish_damage_packet = connection.publish_damage_packet
        source.queue_packet = Mock()
        source.save_update = Mock(side_effect=RuntimeError("artifact failed"))
        connection.subsurface_sources[source.wid] = source
        connection.configure_pixel_source(source)
        packet = Packet(WINDOW_DRAW, 0x10, 0, 0, 1, 1, "rgb24", b"x", 1, 4, {})

        with patch("xpra.server.window.compress.SCREEN_UPDATES_DIRECTORY", "/enabled"):
            published = WindowSource.queue_damage_packet(source, packet, 1.0, 1.0, typedict())

        self.assertTrue(published)
        source.queue_packet.assert_called_once()
        source.save_update.assert_called_once_with(packet, 1.0)
        self.assertIn(1, source.statistics.damage_ack_pending)
        self.assertIs(connection._damage_packet_owners[(0x10, 1)], source)
        self.assertEqual(source.statistics.packet_count, 4)
        self.assertEqual(source.statistics.encoding_totals, {"rgb24": [3, 3]})
        self.assertEqual(source.global_statistics.packet_count, 5)
        self.assertEqual(source.encoding_last_used, "rgb24")

    def test_protocol_wakeup_failure_keeps_committed_packet_and_ack_owner(self) -> None:
        connection = self.make_connection()
        transport = ClientConnection.__new__(ClientConnection)
        packet_qsizes = Mock()
        packet_qsizes.append.side_effect = RuntimeError("statistics failed")
        transport.statistics = SimpleNamespace(
            packet_qsizes=packet_qsizes, damage_packet_qpixels=[],
        )
        transport.packet_queue = deque()
        transport.protocol = Mock()
        transport.protocol.source_has_more.side_effect = RuntimeError("wakeup failed")
        source = WindowSource.__new__(WindowSource)
        source.schedule_auto_refresh = Mock()
        source.wid = 0x20
        source.statistics = SimpleNamespace(
            damage_ack_pending={}, damage_in_latency=[], last_packet_time=0,
            packet_count=0, encoding_totals={},
        )
        source.global_statistics = SimpleNamespace(packet_count=0)
        source.encoding_last_used = ""
        source.publish_damage_packet = connection.publish_damage_packet
        source.queue_packet = transport.queue_packet
        connection.subsurface_sources[source.wid] = source
        connection.configure_pixel_source(source)
        packet = Packet(WINDOW_DRAW, 0x10, 0, 0, 1, 1, "rgb24", b"x", 1, 4, {})

        published = WindowSource.queue_damage_packet(source, packet, 1.0, 1.0, typedict())

        self.assertTrue(published)
        self.assertEqual(tuple(transport.packet_queue), ((packet, source.wid, 1, False),))
        packet_qsizes.append.assert_called_once()
        transport.protocol.source_has_more.assert_called_once_with()
        self.assertIn(1, source.statistics.damage_ack_pending)
        self.assertIs(connection._damage_packet_owners[(0x10, 1)], source)

    def test_publish_runs_unlocked_and_cleanup_waits_for_its_lease(self) -> None:
        connection = self.make_connection()
        child = _FakePixelSource(0x20, 0x10)
        connection.subsurface_sources[child.wid] = child
        connection.configure_pixel_source(child)
        sequence = child.allocate_damage_packet_sequence()
        publishing = threading.Event()
        release = threading.Event()
        published = []
        observations = []

        def publisher() -> None:
            publishing.set()
            lock_was_free = threading.Event()

            def probe_lock() -> None:
                with connection._damage_packet_lock:
                    lock_was_free.set()

            probe = threading.Thread(target=probe_lock, daemon=True)
            probe.start()
            probe.join(1)
            observations.append(lock_was_free.is_set())
            observations.append(release.wait(5))
            observations.append(not child.cleanup_started.is_set())
            published.append(sequence)

        publish_thread = threading.Thread(
            target=child.publish_damage_packet,
            args=(0x10, sequence, child, publisher),
        )
        publish_thread.start()
        self.assertTrue(publishing.wait(5))
        cleanup_thread = threading.Thread(target=connection.cleanup_subsurface_source, args=(child.wid,))
        cleanup_thread.start()
        for _ in range(100):
            with connection._damage_packet_lock:
                if id(child) not in connection._damage_packet_sources:
                    break
            threading.Event().wait(0.01)
        self.assertFalse(child.cleanup_started.is_set())
        release.set()
        publish_thread.join(5)
        cleanup_thread.join(5)
        self.assertFalse(publish_thread.is_alive())
        self.assertFalse(cleanup_thread.is_alive())
        self.assertEqual(published, [sequence])
        self.assertEqual(observations, [True, True, True])
        self.assertTrue(child.cleanup_finished.is_set())
        connection.client_ack_damage(sequence, 0x10, 20, 10, 0, "late")
        child.damage_packet_acked.assert_not_called()

    def test_connection_cleanup_attempts_every_pixel_source(self) -> None:
        connection = self.make_connection()
        parent = _FakePixelSource(0x10)
        child = _FakePixelSource(0x20, parent.wid)
        parent.cleanup = Mock(side_effect=RuntimeError("parent cleanup failed"))
        connection.window_sources[parent.wid] = parent
        connection.subsurface_sources[child.wid] = child
        connection.configure_pixel_source(parent)
        connection.configure_pixel_source(child)
        connection.hidden_windows.add(object())

        with self.assertRaisesRegex(RuntimeError, "parent cleanup failed"):
            connection.cleanup()

        parent.cleanup.assert_called_once_with()
        self.assertTrue(child.cleanup_finished.is_set())
        self.assertEqual(connection.window_sources, {})
        self.assertEqual(connection.subsurface_sources, {})
        self.assertEqual(connection._damage_packet_sources, {})
        self.assertEqual(connection.hidden_windows, set())

    def test_subsurface_cleanup_clears_accounting_after_error(self) -> None:
        connection = self.make_connection()
        parent = _FakePixelSource(0x10)
        child = _FakePixelSource(0x20, 0x10)
        child.offset_x = 3
        child.offset_y = 4
        child.logical_width = 5
        child.logical_height = 6
        child.cleanup = Mock(side_effect=RuntimeError("child cleanup failed"))
        connection.window_sources[parent.wid] = parent
        connection.subsurface_sources[child.wid] = child
        connection.configure_pixel_source(parent)
        connection.configure_pixel_source(child)
        connection.calculate_window_pixels[child.wid] = 100
        scheduled = []

        def schedule(callback, *args):
            scheduled.append((callback, args))
            return 1

        with (
            source_scheduler("idle", side_effect=schedule),
            patch.object(connection, "_run_subsurface_composite") as run_composite,
            self.assertRaisesRegex(RuntimeError, "child cleanup failed"),
        ):
            connection.cleanup_subsurface_source(child.wid)

        self.assertNotIn(child.wid, connection.subsurface_sources)
        self.assertNotIn(child.wid, connection.calculate_window_pixels)
        self.assertEqual(connection._damage_packet_sources, {id(parent): parent})
        self.assertEqual(len(scheduled), 1)
        self.assertTrue(callable(scheduled[0][0]))
        state = connection._subsurface_composites[parent.wid]
        self.assertEqual(state["pending_region"], (3, 4, 5, 6))
        callback, args = scheduled[0]
        self.assertFalse(callback(*args))
        run_composite.assert_called_once_with(parent.wid, state["token"])
        # False hands one-shot destruction to GLib; the connection must have
        # relinquished its reservation before the exact repair is dispatched.
        self.assertEqual(connection._subsurface_glib_sources, {})

    def test_parent_removal_attempts_every_child_cleanup_after_error(self) -> None:
        connection = self.make_connection()
        parent = _FakePixelSource(0x10)
        first_child = _FakePixelSource(0x20, parent.wid)
        second_child = _FakePixelSource(0x21, parent.wid)
        parent.cleanup = Mock(side_effect=RuntimeError("parent cleanup failed"))
        first_child.cleanup = Mock(side_effect=RuntimeError("first child cleanup failed"))
        connection.window_sources[parent.wid] = parent
        connection.subsurface_sources = {
            first_child.wid: first_child,
            second_child.wid: second_child,
        }
        for source in (parent, first_child, second_child):
            connection.configure_pixel_source(source)
            connection.calculate_window_pixels[source.wid] = 100

        with self.assertRaisesRegex(RuntimeError, "parent cleanup failed"):
            connection.remove_window(parent.wid, parent.window)

        parent.cleanup.assert_called_once_with()
        first_child.cleanup.assert_called_once_with()
        self.assertTrue(second_child.cleanup_finished.is_set())
        self.assertEqual(connection.window_sources, {})
        self.assertEqual(connection.subsurface_sources, {})
        self.assertEqual(connection.calculate_window_pixels, {})
        self.assertEqual(connection._damage_packet_sources, {})
        self.assertEqual(connection._damage_packet_owners, {})

    def test_ack_runs_unlocked_and_cleanup_waits_for_its_lease(self) -> None:
        connection = self.make_connection()
        child = _FakePixelSource(0x20, 0x10)
        connection.subsurface_sources[child.wid] = child
        connection.configure_pixel_source(child)
        sequence = child.allocate_damage_packet_sequence()
        child.publish_damage_packet(0x10, sequence, child, lambda: None)
        acknowledging = threading.Event()
        release = threading.Event()
        observations = []

        def acknowledge(*_args) -> None:
            acknowledging.set()
            lock_was_free = threading.Event()

            def probe_lock() -> None:
                with connection._damage_packet_lock:
                    lock_was_free.set()

            probe = threading.Thread(target=probe_lock, daemon=True)
            probe.start()
            probe.join(1)
            observations.append(lock_was_free.is_set())
            observations.append(release.wait(5))
            observations.append(not child.cleanup_started.is_set())

        child.damage_packet_acked.side_effect = acknowledge
        ack_thread = threading.Thread(
            target=connection.client_ack_damage,
            args=(sequence, 0x10, 20, 10, 0, "ok"),
        )
        ack_thread.start()
        self.assertTrue(acknowledging.wait(5))
        cleanup_thread = threading.Thread(target=connection.cleanup_subsurface_source, args=(child.wid,))
        cleanup_thread.start()
        for _ in range(100):
            with connection._damage_packet_lock:
                if id(child) not in connection._damage_packet_sources:
                    break
            threading.Event().wait(0.01)
        self.assertFalse(child.cleanup_started.is_set())
        release.set()
        ack_thread.join(5)
        cleanup_thread.join(5)
        self.assertFalse(ack_thread.is_alive())
        self.assertFalse(cleanup_thread.is_alive())
        self.assertEqual(observations, [True, True, True])
        self.assertTrue(child.cleanup_finished.is_set())

    def test_pixel_fanout_preserves_toplevel_reporting_boundary(self) -> None:
        connection = self.make_connection()
        parent = _FakePixelSource(0x10)
        child = _FakePixelSource(0x20, 0x10)
        connection.window_sources[parent.wid] = parent
        connection.subsurface_sources[child.wid] = child
        connection.configure_pixel_source(parent)
        connection.configure_pixel_source(child)

        self.assertEqual(connection.all_window_sources(), (parent,))
        self.assertEqual(connection.all_pixel_sources(), (parent, child))
        connection.suspend_window_sources()
        connection.resume_window_sources()
        connection.go_idle()
        connection.no_idle()
        connection.cleanup_video_encoders()
        connection.reinit_encoders()
        window = Mock()
        window.get_dimensions.return_value = (640, 480)
        parent.window = window
        connection.map_window(parent.wid, window, (5, 7))
        connection.unmap_window(parent.wid, window)
        connection.cancel_damage(parent.wid)
        connection.update_batch(parent.wid, window, typedict({"delay": 12}))
        connection.set_client_properties(parent.wid, window, typedict({"bit-depth": 24}))
        connection.can_send_window = Mock(return_value=True)
        connection.damage = Mock()
        connection.refresh(parent.wid, window, {"quality": 90})
        EncodingsConnection.set_auto_refresh_delay(connection, 250, (parent.wid,))
        connection.encodings = ("auto", "png")
        connection.server_encodings = ("auto", "png")
        connection.statistics.reset = Mock()
        connection.default_batch_config = Mock()
        connection.default_batch_config.clone.return_value = Mock()
        EncodingsConnection.set_encoding(connection, "png", (parent.wid,), True)
        EncodingsConnection.set_min_quality(connection, 10)
        EncodingsConnection.set_max_quality(connection, 90)
        EncodingsConnection.set_quality(connection, 75)
        EncodingsConnection.set_min_speed(connection, 20)
        EncodingsConnection.set_max_speed(connection, 80)
        EncodingsConnection.set_speed(connection, 60)
        set_window_refresh_rate(connection, 60000)

        connection.av_sync = True
        connection.audio_source = object()
        connection.get_audio_source_latency = lambda: 5
        connection.av_sync_delay = 10
        connection.av_sync_delta = 2
        connection.av_sync_delay_total = 0
        AVSyncConnection.update_av_sync_delay_total(connection)

        for source in (parent, child):
            names = [name for name, _args in source.calls]
            for expected in (
                "suspend", "resume", "go-idle", "no-idle", "init-encoders",
                "map", "unmap", "cancel", "client-properties",
                "refresh-delay", "encoding", "min-quality", "max-quality", "quality",
                "min-speed", "max-speed", "speed", "refresh-rate",
                "av-sync", "av-delay", "av-update",
            ):
                self.assertIn(expected, names)
            self.assertEqual(source.batch_config.delay, 12)
        self.assertIn("video-clean", [name for name, _args in parent.calls])
        self.assertNotIn("video-clean", [name for name, _args in child.calls])
        connection.statistics.reset.assert_called_once_with()
        connection.damage.assert_called_once_with(
            parent.wid, window, 0, 0, 640, 480, {"quality": 90},
        )
        self.assertNotIn(("refresh", ({"quality": 90},)), child.calls)
        self.assertNotIn(("refresh", ({"quality": 90},)), parent.calls)

        parent_sequence = parent.allocate_damage_packet_sequence()
        child_sequence = child.allocate_damage_packet_sequence()
        parent.publish_damage_packet(parent.wid, parent_sequence, parent, lambda: None)
        child.publish_damage_packet(parent.wid, child_sequence, child, lambda: None)

        def assert_detached() -> None:
            self.assertEqual(connection.window_sources, {})
            self.assertEqual(connection.subsurface_sources, {})
            self.assertEqual(connection._damage_packet_owners, {})
            self.assertEqual(connection._damage_packet_sources, {})

        parent.cleanup_observer = assert_detached
        child.cleanup_observer = assert_detached
        connection.emit.side_effect = lambda *_args: assert_detached()

        connection.remove_window(parent.wid, window)
        self.assertEqual(connection.window_sources, {})
        self.assertEqual(connection.subsurface_sources, {})
        self.assertEqual(connection.window_backing_properties, {})
        self.assertEqual(connection._damage_packet_owners, {})
        self.assertEqual(connection._damage_packet_sources, {})
        self.assertTrue(parent.cleanup_finished.is_set())
        self.assertTrue(child.cleanup_finished.is_set())

    def test_late_subsurface_inherits_only_parent_backing_capabilities(self) -> None:
        connection = self.make_connection()
        parent = _FakePixelSource(0x10)
        existing_child = _FakePixelSource(0x20, parent.wid)
        connection.window_sources[parent.wid] = parent
        connection.subsurface_sources[existing_child.wid] = existing_child
        properties = typedict({
            "bit-depth": 30,
            "encoding.full_csc_modes": {"h264": ("NV12",)},
            "encoding.transparency": False,
            "encodings.core": ("rgb24", "png"),
            "encoding.render-size": (800, 600),
            "maximized": True,
        })

        connection.set_client_properties(parent.wid, parent.window, properties)

        parent_properties = next(args[0] for name, args in parent.calls if name == "client-properties")
        child_properties = next(args[0] for name, args in existing_child.calls if name == "client-properties")
        self.assertEqual(parent_properties, properties)
        self.assertEqual(set(child_properties), {
            "bit-depth", "encoding.full_csc_modes", "encoding.transparency", "encodings.core",
        })
        self.assertNotIn("encoding.render-size", child_properties)
        self.assertNotIn("maximized", child_properties)

        late_child = _FakePixelSource(0x21, parent.wid)
        connection.apply_subsurface_client_properties(late_child, parent.wid)
        late_properties = next(args[0] for name, args in late_child.calls if name == "client-properties")
        self.assertEqual(late_properties, child_properties)

    def test_unhide_without_server_geometry_refreshes_but_does_not_map(self) -> None:
        connection = self.make_connection()
        parent = _FakePixelSource(0x10)
        child = _FakePixelSource(0x20, 0x10)
        connection.window_sources[parent.wid] = parent
        connection.subsurface_sources[child.wid] = child
        window = object()
        parent.window = window
        connection.hidden_windows = {window}
        connection.is_window_visible = Mock(return_value=True)
        connection.can_send_window = Mock(return_value=True)
        connection.get_server_geometry = None
        connection.refresh = Mock()

        self.assertFalse(connection.update_window_visibility(parent.wid, window, notify=False))

        for source in (parent, child):
            self.assertNotIn("map", [name for name, _args in source.calls])
        connection.refresh.assert_called_once_with(parent.wid, window, {})


if __name__ == "__main__":
    unittest.main()
