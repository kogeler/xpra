#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 kogeler
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from xpra.codecs.image import ImageWrapper
from xpra.server.window import compress, video_compress
from xpra.server.window.batch_config import DamageBatchConfig
from xpra.server.window.compress import WindowSource
from xpra.server.window.perfstats import WindowPerformanceStatistics
from xpra.server.window.subsurface_source import SubsurfaceWindowSource
from xpra.server.window.video_compress import WindowVideoSource
from xpra.util.objects import typedict
from xpra.util.rectangle import rectangle
from xpra.wayland.server.models.subsurface_window import SubsurfaceWindow


class InitialDamageTest(unittest.TestCase):

    @staticmethod
    def make_source(source_class: type[WindowVideoSource] = WindowVideoSource) -> WindowVideoSource:
        source = object.__new__(source_class)
        source.wid = 1
        source.window = Mock()
        source.window.get.return_value = ""
        source.window.get_internal_property_names.return_value = ("frame-has-alpha", "pixel-format")
        source.window.get_property.return_value = None
        source.window.has_alpha.return_value = True
        source._current_frame_has_alpha = None
        source._alpha_capable = True
        source._client_csc_modes_resolved = False
        source.window.connect.return_value = 41
        source.window_signal_handlers = []
        source.window_dimensions = (800, 600)
        source._opaque_region = ()
        source._encoding_hint = ""
        source._encoders = {}
        source._mmap = None
        source.is_OR = False
        source.is_tray = False
        source.is_shadow = False
        source.has_alpha = True
        source.supports_transparency = True
        source.discard_alpha = False
        source._want_alpha = True
        source.image_depth = 32
        source.strict = False
        source.encoding = "h264"
        source.common_encodings = ("h264",)
        source.common_video_encodings = ("h264",)
        source.non_video_encodings = ()
        source.window_type = set()
        source.content_types = ()
        source.supports_scrolling = False
        source.full_csc_modes = typedict()
        source.encoding_options = typedict()
        source.global_statistics = SimpleNamespace(congestion_value=0)
        source.statistics = SimpleNamespace(packet_count=0)
        source._current_speed = 50
        source._current_quality = 100
        source.bandwidth_limit = 0
        source.client_render_size = (0, 0)
        source.has_shape = False
        source.client_bit_depth = 24
        source.update_encoding_video_subregion = Mock()
        source.update_pipeline_scores = Mock()
        source.verify_csc_and_encoder = Mock(return_value=True)
        return source

    @staticmethod
    def damage(source: WindowVideoSource) -> bool:
        with patch.object(WindowSource, "damage") as damage:
            source.damage(0, 0, 800, 600)
        return damage.called

    def set_properties(self, source: WindowVideoSource, properties: dict) -> None:
        with patch.object(WindowSource, "set_client_properties") as set_properties:
            source.set_client_properties(typedict(properties))
        set_properties.assert_called_once()

    @staticmethod
    def set_pixel_format(source: WindowVideoSource, pixel_format: str) -> None:
        source.window.get.return_value = pixel_format
        source.window.get_property.return_value = {
            "RGBA": True, "BGRA": True, "RGBX": False, "BGRX": False,
        }.get(pixel_format)
        # This upstream signal callback exists on the tests-only clean control.
        # Observe behavior, not the presence of a new private WIS helper.
        source.frame_has_alpha_changed(source.window)

    @staticmethod
    def configure_mixed_encodings(source: WindowVideoSource) -> None:
        source.common_encodings = ("h264", "webp", "rgb32")
        source.common_video_encodings = ("h264",)
        source.non_video_encodings = ("webp", "rgb32")
        source.get_best_encoding_video = Mock(return_value="h264")
        source.image_depth = 32
        source.has_shape = False
        source.client_bit_depth = 24
        source._current_quality = 100
        source._fixed_min_quality = 0
        source._fixed_max_quality = 100
        source._rgb_auto_threshold = 2048

    @staticmethod
    def make_constructed_source(window: SubsurfaceWindow) -> WindowVideoSource:
        return WindowVideoSource(
            64, 64, Mock(), Mock(return_value=0), Mock(), Mock(),
            SimpleNamespace(congestion_value=0), 1, window, DamageBatchConfig(),
            0, False, 0, None, None, ("h264", "rgb32"), ("h264", "rgb32"),
            "h264", ("h264", "rgb32"), ("h264", "rgb32"), (),
            typedict(), typedict(), ("RGBA", "RGB"), typedict(), None, 0, 0,
        )

    def test_constructor_owns_one_real_frame_signal_per_source(self):
        window = SubsurfaceWindow(64, 64)
        sources = [self.make_constructed_source(window) for _ in range(2)]
        image = ImageWrapper(0, 0, 64, 64, b"\\0" * (64 * 64 * 4), "BGRA", 32, 256, 4)
        self.addCleanup(image.free)
        for source in sources:
            self.addCleanup(WindowSource.ui_cleanup, source)
            self.configure_mixed_encodings(source)
            source.full_csc_modes = typedict({"h264": ("YUV420P",)})
            source.update_encoding_video_subregion = Mock()
            source.update_pipeline_scores = Mock()
            source.verify_csc_and_encoder = Mock(return_value=True)
            self.assertEqual(len(source.window_signal_handlers), 1)
            self.assertTrue(window.handler_is_connected(source.window_signal_handlers[0]))

        for pixel_format, alpha in (("BGRX", False), ("BGRA", True), ("RGBX", False)):
            image.set_pixel_format(pixel_format)
            window.set_image(image)  # real GObject notify, original constructor-owned callback
            for source in sources:
                self.assertEqual(source.has_alpha, alpha)
                self.assertEqual(source._want_alpha, alpha)
                self.assertEqual(source.get_best_encoding(800, 600, {}, "h264"), "webp" if alpha else "h264")
                self.assertTrue(self.damage(source))
                self.assertEqual(len(source.window_signal_handlers), 1)

        first, other = sources
        first_handler = first.window_signal_handlers[0]
        WindowSource.ui_cleanup(first)
        self.assertFalse(window.handler_is_connected(first_handler))
        other.update_encoding_options = Mock(wraps=other.update_encoding_options)
        image.set_pixel_format("BGRA")
        window.set_image(image)
        other.update_encoding_options.assert_called_once()
        self.assertTrue(other.has_alpha)
        self.assertIsNone(first.window)

    def test_encoder_reinit_does_not_read_window_model(self):
        source = self.make_source()
        with (
            patch.object(WindowSource, "init_encoders") as inherited_init,
            patch.object(source, "video_context_clean"),
            patch.object(source.window, "get_property") as get_property,
        ):
            source.init_encoders()
        inherited_init.assert_called_once_with()
        get_property.assert_not_called()

    def test_csc_wait_uses_selected_video_candidates(self):
        cases = (
            ("strict h264 with vp9 modes", "h264", True, {"vp9": ("YUV420P",)}, False),
            ("h264 with vp9 modes", "h264", False, {"vp9": ("YUV420P",)}, False),
            ("h264 with h264 modes", "h264", True, {"h264": ("YUV420P",)}, True),
            ("auto with vp9 modes", "auto", False, {"vp9": ("YUV420P",)}, True),
            ("stream with vp9 modes", "stream", False, {"vp9": ("YUV420P",)}, True),
            ("grayscale with vp9 modes", "grayscale", False, {"vp9": ("YUV420P",)}, True),
            ("auto without modes", "auto", False, {}, False),
            ("strict png without modes", "png", True, {}, True),
            ("png without modes", "png", False, {}, True),
        )
        for name, encoding, strict, csc_modes, forwarded in cases:
            with self.subTest(name=name):
                source = self.make_source()
                source.common_encodings = ("h264", "vp9", "png")
                source.common_video_encodings = ("h264", "vp9")
                source.non_video_encodings = ("png",)
                source.encoding = encoding
                source.strict = strict
                source.full_csc_modes = typedict(csc_modes)
                self.set_pixel_format(source, "BGRX")

                self.assertEqual(self.damage(source), forwarded)

    def test_csc_wait_honours_fixed_selector_precedence(self):
        cases = (
            ("h264 hint", "h264", False),
            ("png hint", "png", True),
        )
        for name, hint, forwarded in cases:
            with self.subTest(name=name):
                source = self.make_source()
                source.encoding = "auto"
                source.common_encodings = ("h264", "vp9", "png")
                source.common_video_encodings = ("h264", "vp9")
                source.non_video_encodings = ("png",)
                source.full_csc_modes = typedict({"vp9": ("YUV420P",)})
                source._encoders = {hint: Mock()}
                source._encoding_hint = hint
                self.set_pixel_format(source, "BGRX")

                self.assertEqual(self.damage(source), forwarded)

        source = self.make_source()
        source.encoding = "auto"
        source.common_encodings = ("h264", "vp9", "png")
        source.common_video_encodings = ("h264", "vp9")
        source.non_video_encodings = ("png",)
        source.full_csc_modes = typedict({"vp9": ("YUV420P",)})
        with patch.object(compress, "HARDCODED_ENCODING", "h264"):
            self.set_pixel_format(source, "BGRX")
            self.assertFalse(self.damage(source))

    def test_video_only_damage_waits_for_window_csc_modes(self):
        source = self.make_source()

        self.assertFalse(self.damage(source))

        self.set_properties(source, {"event": "map"})
        self.assertTrue(self.damage(source))

    def test_explicit_empty_window_csc_modes_resolve_state(self):
        source = self.make_source()

        self.set_properties(source, {"encoding.full_csc_modes": {}})

        self.assertTrue(source._client_csc_modes_resolved)
        self.assertTrue(self.damage(source))

    def test_frame_alpha_transition_uses_bounded_encoding_debug(self):
        source = self.make_source()

        with (
            patch.object(video_compress, "log") as encoding_log,
            patch.object(video_compress, "videolog") as video_log,
        ):
            self.set_pixel_format(source, "BGRA")
            self.damage(source)
            self.damage(source)  # unchanged state must not repeat the diagnostic

        encoding_log.assert_called_once_with(
            "window %#x frame pixel format=%s, want-alpha=%s",
            1,
            "BGRA",
            True,
        )
        video_log.assert_not_called()

    def test_subsurface_damage_does_not_wait_for_toplevel_map(self):
        source = self.make_source(SubsurfaceWindowSource)
        source.parent_wid = 9

        self.assertTrue(self.damage(source))

    def test_unknown_format_restores_capability_alpha_policy(self):
        source = self.make_source()
        source.common_encodings = ("h264", "webp", "rgb32")
        source.non_video_encodings = ("webp", "rgb32")
        source.image_depth = 32
        source.has_shape = False
        source.client_bit_depth = 24
        source._current_quality = 100
        source._rgb_auto_threshold = 2048

        self.set_pixel_format(source, "BGRX")
        self.assertFalse(source._want_alpha)
        self.set_pixel_format(source, "DMABUF")

        self.assertIsNone(source._current_frame_has_alpha)
        self.assertTrue(source._want_alpha)
        self.assertEqual(source.get_best_encoding(800, 600, {}, "h264"), "webp")

    def test_opaque_frame_selects_requested_video_after_csc_resolution(self):
        source = self.make_source()
        source.common_encodings = ("h264", "webp", "rgb32")
        source.non_video_encodings = ("webp", "rgb32")
        source.get_best_encoding_video = Mock(return_value="h264")

        self.set_pixel_format(source, "BGRX")

        self.assertFalse(source._current_frame_has_alpha)
        self.assertFalse(source._want_alpha)
        self.assertEqual(source.get_best_encoding(800, 600, {}, "h264"), "h264")
        source.get_best_encoding_video.assert_called_once_with(800, 600, {}, "h264")
        self.assertFalse(self.damage(source))

        self.set_properties(source, {"event": "map"})
        self.assertTrue(self.damage(source))

    def test_alpha_frame_selects_alpha_capable_encoding(self):
        source = self.make_source()
        source.common_encodings = ("h264", "webp", "rgb32")
        source.non_video_encodings = ("webp", "rgb32")
        source.image_depth = 32
        source.has_shape = False
        source.client_bit_depth = 24
        source._current_quality = 100
        source._rgb_auto_threshold = 2048
        source._encoding_hint = "h264"
        source._encoders = {"h264": Mock()}

        self.set_pixel_format(source, "BGRA")

        self.assertTrue(source._current_frame_has_alpha)
        self.assertTrue(source._want_alpha)
        self.assertEqual(source.get_best_encoding(800, 600, {}, "h264"), "webp")
        self.assertTrue(self.damage(source))

    def test_alpha_frame_without_alpha_encoding_fails_closed(self):
        source = self.make_source()

        self.set_pixel_format(source, "RGBA")

        with self.assertRaisesRegex(ValueError, "no transparency encoding"):
            source.get_best_encoding(800, 600, {}, "h264")

    def test_alpha_admission_outranks_opaque_selectors_but_keeps_mmap(self):
        for policy in ("strict", "hint", "hardcoded"):
            with self.subTest(policy=policy):
                source = self.make_source()
                self.configure_mixed_encodings(source)
                source.strict = policy == "strict"
                source._encoding_hint = "h264" if policy == "hint" else ""
                source._encoders = {"h264": Mock()}
                with patch.object(compress, "HARDCODED_ENCODING", "h264" if policy == "hardcoded" else ""):
                    self.set_pixel_format(source, "BGRA")
                    self.assertEqual(source.get_best_encoding(800, 600, {}, "h264"), "webp")
                    source._mmap = object()
                    source.assign_encoding_getter()
                    self.assertEqual(source.get_best_encoding(800, 600, {}, "h264"), "mmap")

    def test_malformed_csc_properties_do_not_release_initial_wait(self):
        for value in (None, [], "h264", 1):
            with self.subTest(value=value):
                source = self.make_source()
                self.set_properties(source, {"encoding.full_csc_modes": value})
                self.assertFalse(self.damage(source))

    def test_alpha_frame_with_unusable_alpha_encoding_fails_closed(self):
        cases = (
            ("webp dimensions", ("h264", "webp"), 1, 1, {"quality": 50}),
            ("jpega lossless quality", ("h264", "jpega"), 800, 600, {"quality": 100}),
        )
        for name, encodings, width, height, options in cases:
            with self.subTest(name=name):
                source = self.make_source()
                source.common_encodings = encodings
                source.non_video_encodings = encodings[1:]
                source.image_depth = 32
                source.has_shape = False
                source.client_bit_depth = 24
                source._current_quality = options["quality"]
                source._rgb_auto_threshold = 2048
                source.content_types = ()
                self.set_pixel_format(source, "BGRA")

                with self.assertRaisesRegex(ValueError, "no usable transparency encoding"):
                    source.get_best_encoding(width, height, options, "h264")

    def test_invalid_current_alpha_encoding_selects_usable_fallback(self):
        source = self.make_source()
        source.common_encodings = ("h264", "webp", "rgb32")
        source.non_video_encodings = ("webp", "rgb32")
        source.image_depth = 32
        source.has_shape = False
        source.client_bit_depth = 24
        source._current_quality = 50
        source._rgb_auto_threshold = 2048
        source.content_types = ()
        self.set_pixel_format(source, "BGRA")

        self.assertEqual(source.get_best_encoding(1, 1, {}, "webp"), "rgb32")

    def test_opaque_frame_preserves_adaptive_video_fallbacks(self):
        source = self.make_source()
        source.common_encodings = ("h264", "webp", "rgb32")
        source.non_video_encodings = ("webp", "rgb32")
        source.width_mask = 0xFFFF
        source.height_mask = 0xFFFF
        source._current_quality = 50
        source._fixed_min_quality = 0
        source._fixed_max_quality = 100
        self.set_pixel_format(source, "BGRX")

        with patch.object(WindowSource, "get_auto_encoding", return_value="webp") as auto:
            self.assertEqual(source.get_best_encoding(1, 1, {}, "h264"), "webp")
            source.content_types = ("text",)
            self.assertEqual(source.get_best_encoding(800, 600, {}, "h264"), "webp")

        self.assertEqual(auto.call_count, 2)

    def test_opaque_frame_preserves_explicit_encoding_priority(self):
        source = self.make_source()
        source.common_encodings = ("h264", "png")
        source._encoding_hint = "png"
        source._encoders = {"png": Mock()}
        source._mmap = object()  # opaque policy keeps upstream hint/hardcode precedence
        self.set_pixel_format(source, "BGRX")
        self.assertEqual(source.get_best_encoding(800, 600, {}, "h264"), "png")

        source._encoding_hint = ""
        with patch.object(compress, "HARDCODED_ENCODING", "png"):
            source.assign_encoding_getter()
            self.assertEqual(source.get_best_encoding(800, 600, {}, "h264"), "png")

    def test_opaque_frame_preserves_lossless_window_selection(self):
        source = self.make_source()
        source.window_type = {next(iter(compress.LOSSLESS_WINDOW_TYPES))}
        fallback = Mock(return_value="png")

        with patch.object(
            WindowSource, "get_best_encoding_impl_default", return_value=fallback,
        ) as base_selector:
            self.set_pixel_format(source, "BGRX")
            self.assertEqual(source.get_best_encoding(800, 600, {}, "h264"), "png")

        base_selector.assert_called_once_with()

    def test_alpha_frame_preserves_mmap_transport(self):
        source = self.make_source()
        source._mmap = object()
        source.common_encodings = ("mmap",)
        source.common_video_encodings = ()
        source.non_video_encodings = ()

        self.set_pixel_format(source, "BGRA")

        self.assertTrue(source._want_alpha)
        self.assertEqual(source.get_best_encoding(800, 600, {}, "mmap"), "mmap")

    def test_frame_format_change_reselects_encoding(self):
        source = self.make_source()
        source.common_encodings = ("h264", "webp", "rgb32")
        source.non_video_encodings = ("webp", "rgb32")
        source.get_best_encoding_video = Mock(return_value="h264")
        source.image_depth = 32
        source.has_shape = False
        source.client_bit_depth = 24
        source._current_quality = 100
        source._rgb_auto_threshold = 2048

        self.set_pixel_format(source, "RGBX")
        self.assertEqual(source.get_best_encoding(800, 600, {}, "h264"), "h264")

        self.set_pixel_format(source, "RGBA")
        self.assertEqual(source.get_best_encoding(800, 600, {}, "h264"), "webp")

        self.set_pixel_format(source, "RGBX")
        self.assertEqual(source.get_best_encoding(800, 600, {}, "h264"), "h264")
        self.assertEqual(source.get_best_encoding_video.call_count, 2)

    def test_reconfigure_preserves_opaque_frame_selection_without_model_reads(self):
        source = self.make_source()
        self.configure_mixed_encodings(source)
        self.set_pixel_format(source, "BGRX")
        source.encoding_options = typedict()
        source.global_statistics = SimpleNamespace(congestion_value=0)
        source.statistics = SimpleNamespace(packet_count=0)
        source._current_speed = 50
        source.bandwidth_limit = 0
        source.client_render_size = (0, 0)
        source.window.reset_mock()
        with (
            patch.object(source, "update_encoding_video_subregion"),
            patch.object(source, "update_pipeline_scores"),
            patch.object(source, "verify_csc_and_encoder", return_value=True),
        ):
            source.update_encoding_options()

        self.assertFalse(source._want_alpha)
        self.assertEqual(source.get_best_encoding(800, 600, {}, "h264"), "h264")
        source.window.get.assert_not_called()
        source.window.get_property.assert_not_called()
        source.window.has_alpha.assert_not_called()

    def test_opaque_region_reconfiguration_updates_frame_selector(self):
        source = self.make_source()
        self.configure_mixed_encodings(source)
        source.window_dimensions = (800, 600)
        self.set_pixel_format(source, "BGRA")
        source.window.reset_mock()
        opaque_window = Mock()
        discard_states = []

        def update_generic_options(_force_reload=False):
            discard_states.append(source.discard_alpha)
            source._want_alpha = bool(
                source.has_alpha and source.supports_transparency
                and not source.opaque_contains(0, 0, *source.window_dimensions)
            )
            source.assign_encoding_getter()

        with (
            patch.object(WindowSource, "update_encoding_options", side_effect=update_generic_options),
            patch.object(source, "update_encoding_video_subregion"),
            patch.object(source, "update_pipeline_scores"),
            patch.object(source, "verify_csc_and_encoder", return_value=True),
        ):
            opaque_window.get_property.return_value = ((0, 0, 800, 600),)
            source.window_opaque_region_changed(opaque_window)
            self.assertTrue(source.discard_alpha)
            self.assertFalse(source._want_alpha)
            self.assertEqual(source.get_best_encoding(800, 600, {}, "h264"), "h264")

            opaque_window.get_property.return_value = ()
            source.window_opaque_region_changed(opaque_window)
            self.assertFalse(source.discard_alpha)
            self.assertTrue(source._want_alpha)
            self.assertEqual(source.get_best_encoding(800, 600, {}, "h264"), "webp")

        self.assertEqual(discard_states, [True, False])
        source.window.get.assert_not_called()

    def test_damage_rebinds_frame_selector_after_resize(self):
        source = self.make_source()
        self.configure_mixed_encodings(source)
        source.window_dimensions = (800, 600)
        source._opaque_region = ((0, 0, 800, 600),)
        source.statistics = WindowPerformanceStatistics()
        source.window_dimensions_updated = Mock()
        self.set_pixel_format(source, "BGRA")
        source.update_discard_alpha()
        source.update_encoding_options()
        self.assertTrue(source.discard_alpha)
        self.assertFalse(source._want_alpha)
        source.window.reset_mock()

        source.update_window_dimensions(801, 600)
        self.assertTrue(self.damage(source))
        self.assertFalse(source.discard_alpha)
        self.assertTrue(source._want_alpha)
        self.assertEqual(source.get_best_encoding(801, 600, {}, "h264"), "webp")

        source.update_window_dimensions(800, 600)
        self.assertTrue(self.damage(source))
        self.assertTrue(source.discard_alpha)
        self.assertFalse(source._want_alpha)
        self.assertEqual(source.get_best_encoding(800, 600, {}, "h264"), "h264")
        self.assertEqual(source.window_dimensions_updated.call_count, 2)
        self.assertEqual(source.window.get.call_count, 4)

    def test_damage_discovered_resize_rebinds_before_batching(self):
        for old_width, new_width in ((800, 801), (801, 800)):
            with self.subTest(old_width=old_width, new_width=new_width):
                source = self.make_source()
                self.configure_mixed_encodings(source)
                source.window_dimensions = (old_width, 600)
                source._opaque_region = ((0, 0, 800, 600),)
                source.statistics = WindowPerformanceStatistics()
                source.window_dimensions_updated = Mock()
                source.suspended = False
                source.full_frames_only = False
                source._client_csc_modes_resolved = True
                source.update_discard_alpha()
                self.set_pixel_format(source, "BGRA")
                source.window.get_dimensions.return_value = (new_width, 600)
                observed = []
                source.do_damage = lambda *_args: observed.append((
                    source.discard_alpha,
                    source._want_alpha,
                    source.get_best_encoding(new_width, 600, {}, "h264"),
                ))

                # The generic damage path, not the fixture, discovers the
                # resized model dimensions after the video wrapper entered.
                source.damage(0, 0, new_width, 600)

                opaque = new_width == 800
                self.assertEqual(observed, [(opaque, not opaque, "h264" if opaque else "webp")])

    def make_image_source(self, width=64, height=64, pixel_format="BGRA"):
        source = self.make_source()
        self.configure_mixed_encodings(source)
        init_video_lifecycle = getattr(type(source), "_init_video_lifecycle", None)
        if init_video_lifecycle:
            init_video_lifecycle(source)
        source.common_encodings = ("h264", "rgb24", "rgb32")
        source.non_video_encodings = ("rgb24", "rgb32")
        source.video_encodings = ("h264",)
        source.window_dimensions = (width, height)
        source.full_frames_only = False
        source.max_small_regions = 40
        source.b_frame_flush_timer = 0
        source.assign_sq_options = lambda options, **_kwargs: dict(options)
        source.get_frame_encode_delay = Mock(return_value=0)
        # A previously active H.264 pipeline may require even sizes.
        # Picture fallback must retain every pixel of this new frame.
        source.width_mask = source.height_mask = 0xFFFE
        source.edge_encoding = "rgb24"
        source.is_cancelled = Mock(return_value=False)
        source.scaled_size = Mock(return_value=None)
        image = ImageWrapper(
            0, 0, width, height, b"\x20\x40\x60\x80" * width * height,
            pixel_format, 32, width * 4, 4,
        )
        source.make_data_packet_cb = Mock()
        source.call_in_encode_thread = lambda callback, *args: callback(*args)
        self.set_pixel_format(source, pixel_format)
        return source, image

    def test_actual_alpha_image_survives_video_region_and_novideo_routes(self):
        for video_region, novideo, width, height in ((True, False, 65, 63), (False, True, 1, 1)):
            with self.subTest(video_region=video_region, novideo=novideo):
                source, image = self.make_image_source(width, height)
                source.video_subregion = SimpleNamespace(
                    rectangle=rectangle(0, 0, width, height) if video_region else None,
                )
                source.process_damage_region = lambda when, _x, _y, _w, _h, coding, options, flush=0: (
                    source.process_damage_image(when, when, image, coding, 1, options, flush)
                )

                source.send_regions(0, (rectangle(0, 0, width, height),), "h264", {"novideo": novideo})

                source.make_data_packet_cb.assert_called_once()
                encoded = source.make_data_packet_cb.call_args.args
                self.assertEqual(encoded[:2], (width, height))
                self.assertIs(encoded[4], image)
                self.assertEqual(encoded[5], "rgb32")
                self.assertEqual(image.get_pixel_format(), "BGRA")
                image.free()

    def test_captured_alpha_snapshots_planner_options_and_retains_typed_encode_options(self):
        source, image = self.make_image_source()
        options = typedict({
            "quality": 100,
            "av-delay": 0,
            "window-size": (64, 64),
            "scaled-width": 32,
            "scaled-height": 32,
        })
        source.get_transparent_encoding = Mock(wraps=source.get_transparent_encoding)

        source.do_process_damage_image(0, 0, image, "h264", 1, options)

        source.get_transparent_encoding.assert_called_once()
        planner_options = source.get_transparent_encoding.call_args.args[2]
        self.assertIs(type(planner_options), dict)
        self.assertIsNot(planner_options, options)
        self.assertEqual(planner_options, options)
        source.make_data_packet_cb.assert_called_once()
        encode_options = source.make_data_packet_cb.call_args.args[7]
        self.assertIs(encode_options, options)
        self.assertEqual(encode_options.intget("scaled-width"), 32)
        self.assertEqual(source.make_data_packet_cb.call_args.args[5], "rgb32")
        image.free()

    def test_captured_image_not_newer_model_selects_alpha_coding(self):
        for image_format, model_format, expected in (("BGRA", "BGRX", "rgb32"), ("BGRX", "BGRA", "h264")):
            with self.subTest(image_format=image_format, model_format=model_format):
                source, image = self.make_image_source(pixel_format=image_format)
                self.set_pixel_format(source, model_format)
                self.assertEqual(source.has_alpha, model_format == "BGRA")
                source.window.reset_mock()

                source.do_process_damage_image(0, 0, image, "h264", 1, typedict())

                source.make_data_packet_cb.assert_called_once()
                self.assertEqual(source.make_data_packet_cb.call_args.args[5], expected)
                source.window.get.assert_not_called()
                source.window.get_property.assert_not_called()
                source.window.has_alpha.assert_not_called()
                image.free()

    def test_captured_alpha_retains_mmap_without_picture_candidates(self):
        source, image = self.make_image_source(1, 1)
        source._mmap = object()
        source.common_encodings = ("mmap",)
        source.non_video_encodings = ()

        source.do_process_damage_image(0, 0, image, "h264", 1, typedict())

        source.make_data_packet_cb.assert_called_once()
        self.assertEqual(source.make_data_packet_cb.call_args.args[:2], (1, 1))
        self.assertEqual(source.make_data_packet_cb.call_args.args[5], "mmap")
        image.free()

    def test_captured_alpha_respects_stable_feature_browser_and_client_policy(self):
        for policy in ("feature", "window", "browser", "client"):
            with self.subTest(policy=policy):
                source, image = self.make_image_source()
                self.addCleanup(image.free)
                source.window.has_alpha.return_value = policy != "window"
                source.content_types = ("browser",) if policy == "browser" else ()
                source.window_type = {"NORMAL"}
                source.window_dimensions = (800, 600)
                source.supports_transparency = policy != "client"
                with patch.object(compress, "HAS_ALPHA", policy != "feature"), \
                        patch.object(compress, "BROWSER_ALPHA_FIX", True):
                    self.set_pixel_format(source, "BGRX")
                source.window.reset_mock()
                source.do_process_damage_image(0, 0, image, "h264", 1, typedict())
                source.make_data_packet_cb.assert_called_once()
                self.assertEqual(source.make_data_packet_cb.call_args.args[5], "h264")
                source.window.get_property.assert_not_called()

    def test_grayscale_disables_mmap_and_keeps_transparent_picture_admission(self):
        source, image = self.make_image_source()
        self.addCleanup(image.free)
        source.encoding = "grayscale"
        source._mmap = object()
        source.do_process_damage_image(0, 0, image, "h264", 1, typedict({"grayscale": True}))
        source.make_data_packet_cb.assert_called_once()
        self.assertEqual(source.make_data_packet_cb.call_args.args[5], "rgb32")
        self.assertTrue(source.make_data_packet_cb.call_args.args[7]["grayscale"])

    def test_captured_alpha_without_usable_candidate_releases_image(self):
        for candidates in (("h264",), ("h264", "webp")):
            with self.subTest(candidates=candidates):
                source, image = self.make_image_source(1, 1)
                source.common_encodings = candidates

                with self.assertRaisesRegex(ValueError, "transparency encoding"):
                    source.do_process_damage_image(0, 0, image, "h264", 1, typedict())

                source.make_data_packet_cb.assert_not_called()
                self.assertTrue(image.freed)

    def test_generic_opaque_region_remains_authoritative_for_captured_image(self):
        source, image = self.make_image_source()
        source._opaque_region = ((0, 0, 64, 64),)
        source.update_discard_alpha()

        source.process_damage_image(0, 0, image, "h264", 1, {}, 0)

        self.assertEqual(image.get_pixel_format(), "BGRX")
        source.make_data_packet_cb.assert_called_once()
        self.assertEqual(source.make_data_packet_cb.call_args.args[5], "h264")
        image.free()

    def test_safe_initial_damage_paths_remain_immediate(self):
        cases = {
            "picture fallback": {"non_video_encodings": ("rgb24",)},
            "global csc modes": {"full_csc_modes": typedict({"h264": ("YUV420P",)})},
            "override redirect": {"is_OR": True},
            "tray": {"is_tray": True},
            "shadow": {"is_shadow": True},
            "no video encoding": {"common_video_encodings": ()},
        }
        for name, values in cases.items():
            with self.subTest(name=name):
                source = self.make_source()
                for key, value in values.items():
                    setattr(source, key, value)
                self.assertTrue(self.damage(source))


if __name__ == "__main__":
    unittest.main()
