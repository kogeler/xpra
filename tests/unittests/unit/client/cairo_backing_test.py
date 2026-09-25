#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2024 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import unittest


SUBSURFACE_COMPOSITE_MODE = "premultiplied-source-over-v1"
SUBSURFACE_TRANSACTION_ID = "subsurface-transaction-id"
SUBSURFACE_STAGE_INDEX = "subsurface-stage-index"
SUBSURFACE_STAGE_COUNT = "subsurface-stage-count"
SUBSURFACE_TOPOLOGY_EPOCH = "subsurface-topology-epoch"
SUBSURFACE_BACKING_EPOCH = "subsurface-backing-epoch"
SUBSURFACE_CLIENT_BACKING_STATE = "_client-subsurface-backing-state"
SUBSURFACE_TRANSACTION_OPTIONS = (
    SUBSURFACE_TRANSACTION_ID,
    SUBSURFACE_STAGE_INDEX,
    SUBSURFACE_STAGE_COUNT,
    SUBSURFACE_TOPOLOGY_EPOCH,
    SUBSURFACE_BACKING_EPOCH,
)


class TestParsePaddingColors(unittest.TestCase):

    def test_empty_string(self):
        from xpra.cairo.backing_base import parse_padding_colors
        r = parse_padding_colors("")
        assert r == (0.0, 0.0, 0.0), f"expected black, got {r}"

    def test_valid_colors(self):
        from xpra.cairo.backing_base import parse_padding_colors
        r = parse_padding_colors("1.0,0.5,0.0")
        assert len(r) == 3
        assert abs(r[0] - 1.0) < 0.001
        assert abs(r[1] - 0.5) < 0.001
        assert abs(r[2] - 0.0) < 0.001

    def test_spaces_trimmed(self):
        from xpra.cairo.backing_base import parse_padding_colors
        r = parse_padding_colors(" 0.2 , 0.4 , 0.6 ")
        assert abs(r[0] - 0.2) < 0.001
        assert abs(r[1] - 0.4) < 0.001
        assert abs(r[2] - 0.6) < 0.001

    def test_too_few_components_falls_back_to_black(self):
        from xpra.cairo.backing_base import parse_padding_colors
        r = parse_padding_colors("0.5,0.5")
        assert r == (0.0, 0.0, 0.0)

    def test_non_numeric_falls_back_to_black(self):
        from xpra.cairo.backing_base import parse_padding_colors
        r = parse_padding_colors("red,green,blue")
        assert r == (0.0, 0.0, 0.0)


class TestClamp(unittest.TestCase):

    def test_below_zero(self):
        from xpra.cairo.backing_base import clamp
        assert clamp(-1.0) == 0.0
        assert clamp(-0.001) == 0.0

    def test_above_one(self):
        from xpra.cairo.backing_base import clamp
        assert clamp(1.001) == 1.0
        assert clamp(100.0) == 1.0

    def test_boundary_values(self):
        from xpra.cairo.backing_base import clamp
        assert clamp(0.0) == 0.0
        assert clamp(1.0) == 1.0

    def test_midrange(self):
        from xpra.cairo.backing_base import clamp
        assert clamp(0.5) == 0.5
        assert clamp(0.9999) == 0.9999


class TestGetScalingFilter(unittest.TestCase):

    def test_nearest_env_override(self):
        from xpra.cairo.backing_base import get_scaling_filter
        from xpra.util.env import OSEnvContext
        from cairo import FILTER_NEAREST
        with OSEnvContext(XPRA_SCALING_FILTER="nearest"):
            f = get_scaling_filter(("text",), 2.0, 2.0)
            assert f == FILTER_NEAREST

    def test_bilinear_env_override(self):
        from xpra.cairo.backing_base import get_scaling_filter
        from xpra.util.env import OSEnvContext
        from cairo import FILTER_GOOD
        with OSEnvContext(XPRA_SCALING_FILTER="bilinear"):
            f = get_scaling_filter(("text",), 2.0, 2.0)
            assert f == FILTER_GOOD

    def test_text_integer_upscale_uses_nearest(self):
        from xpra.cairo.backing_base import get_scaling_filter
        from xpra.util.env import OSEnvContext
        from cairo import FILTER_NEAREST
        with OSEnvContext(XPRA_SCALING_FILTER=""):
            f = get_scaling_filter(("text",), 2.0, 2.0)
            assert f == FILTER_NEAREST

    def test_text_non_integer_scale_uses_best(self):
        from xpra.cairo.backing_base import get_scaling_filter
        from xpra.util.env import OSEnvContext
        from cairo import FILTER_BEST
        with OSEnvContext(XPRA_SCALING_FILTER=""):
            f = get_scaling_filter(("text",), 1.5, 1.5)
            assert f == FILTER_BEST

    def test_non_text_uses_good(self):
        from xpra.cairo.backing_base import get_scaling_filter
        from xpra.util.env import OSEnvContext
        from cairo import FILTER_GOOD
        with OSEnvContext(XPRA_SCALING_FILTER=""):
            f = get_scaling_filter(("video",), 1.5, 1.5)
            assert f == FILTER_GOOD
            f = get_scaling_filter((), 2.0, 2.0)
            assert f == FILTER_GOOD


class _TestBacking:
    """Concrete CairoBackingBase for testing — mixed in below after import."""
    RGB_MODES = ("BGRA", "BGRX", "BGR", "RGB", "RGBA", "RGBX")
    _do_paint_rgb_calls: list

    def _do_paint_rgb(self, fmt, alpha, img_data, x, y, w, h, rw, rh, rowstride, options,
                      source_over=False, reset_region=()) -> None:
        self._do_paint_rgb_calls.append(
            (fmt, alpha, img_data, x, y, w, h, rw, rh, rowstride, options, source_over, reset_region)
        )

    def repaint(self, x, y, w, h) -> None:
        pass

    def update_fps_buffer(self, width, height, pixels) -> None:
        pass


def _make_backing(wid=1, alpha=True, w=100, h=100):
    """Create a ready-to-use _TestBacking instance."""
    from xpra.cairo.backing_base import CairoBackingBase

    class TBacking(_TestBacking, CairoBackingBase):
        pass

    b = TBacking(wid, alpha)
    b._do_paint_rgb_calls = []
    b.border = None
    b.init(w, h, w, h)
    return b


def _composite_options(transaction_id=1, stage_index=0, stage_count=1,
                       topology_epoch=1, backing_epoch=1, reset=(0, 0, 1, 1)):
    from xpra.util.objects import typedict
    options = typedict({
        "subsurface-composite": SUBSURFACE_COMPOSITE_MODE,
        SUBSURFACE_TRANSACTION_ID: transaction_id,
        SUBSURFACE_STAGE_INDEX: stage_index,
        SUBSURFACE_STAGE_COUNT: stage_count,
        SUBSURFACE_TOPOLOGY_EPOCH: topology_epoch,
        SUBSURFACE_BACKING_EPOCH: backing_epoch,
        "flush": stage_count - stage_index - 1,
    })
    if reset is not None:
        options["subsurface-reset"] = reset
    return options


# ---------------------------------------------------------------------------
# cairo_paint_pointer_overlay
# ---------------------------------------------------------------------------

class TestCairoPaintPointerOverlay(unittest.TestCase):

    def _ctx(self, w=64, h=64):
        from cairo import ImageSurface, Context, Format
        return Context(ImageSurface(Format.ARGB32, w, h))

    def test_no_cursor_data_returns_early(self):
        from xpra.cairo.backing_base import cairo_paint_pointer_overlay
        from time import monotonic
        ctx = self._ctx()
        cairo_paint_pointer_overlay(ctx, None, 10, 10, monotonic())
        cairo_paint_pointer_overlay(ctx, (), 10, 10, monotonic())

    def test_make_image_surface_noop_returns_early(self):
        from xpra.common import noop
        from unittest.mock import patch
        from xpra.cairo.backing_base import cairo_paint_pointer_overlay
        from time import monotonic
        cursor_data = [None, None, None, 16, 16, 0, 0, None, b"\x00" * (16 * 16 * 4)]
        ctx = self._ctx()
        with patch("xpra.cairo.backing_base.make_image_surface", noop):
            cairo_paint_pointer_overlay(ctx, cursor_data, 10, 10, monotonic())

    def test_elapsed_too_large_returns_early(self):
        from xpra.cairo.backing_base import cairo_paint_pointer_overlay
        from time import monotonic
        cursor_data = [None, None, None, 16, 16, 0, 0, None, b"\x00" * (16 * 16 * 4)]
        ctx = self._ctx()
        old_start = monotonic() - 10
        cairo_paint_pointer_overlay(ctx, cursor_data, 10, 10, old_start)

    def test_normal_case_paints(self):
        from unittest.mock import patch
        from xpra.cairo.backing_base import cairo_paint_pointer_overlay
        from time import monotonic
        from cairo import ImageSurface, Format
        cw, ch = 16, 16
        pixels = b"\x80\x40\x20\xFF" * (cw * ch)
        cursor_data = [None, None, None, cw, ch, 2, 3, None, pixels]
        ctx = self._ctx(200, 200)
        fake_surface = ImageSurface(Format.ARGB32, cw, ch)
        with patch("xpra.cairo.backing_base.make_image_surface", return_value=fake_surface):
            cairo_paint_pointer_overlay(ctx, cursor_data, 20, 30, monotonic())


# ---------------------------------------------------------------------------
# cairo_draw_backing
# ---------------------------------------------------------------------------

class TestCairoDrawBacking(unittest.TestCase):

    def _make(self, w=64, h=64):
        from cairo import ImageSurface, Context, Format
        surface = ImageSurface(Format.ARGB32, w, h)
        ctx = Context(surface)
        return ctx, surface

    def test_sets_operator_source(self):
        from cairo import Operator
        from xpra.cairo.backing_base import cairo_draw_backing
        ctx, backing = self._make()
        cairo_draw_backing(ctx, backing)
        # smoke test: no exception, operator is SOURCE after the call
        assert ctx.get_operator() == Operator.SOURCE

    def test_with_scaling_filter(self):
        from cairo import FILTER_NEAREST
        from xpra.cairo.backing_base import cairo_draw_backing
        ctx, backing = self._make()
        cairo_draw_backing(ctx, backing, scaling_filter=FILTER_NEAREST)

    def test_without_filter(self):
        from xpra.cairo.backing_base import cairo_draw_backing
        ctx, backing = self._make()
        cairo_draw_backing(ctx, backing, scaling_filter=None)


# ---------------------------------------------------------------------------
# CairoBackingBase.__init__ and init
# ---------------------------------------------------------------------------

class TestCairoBackingBaseInit(unittest.TestCase):

    def test_initial_attributes(self):
        b = _make_backing()
        assert b.size == (100, 100)
        assert b.render_size == (100, 100)
        assert b.fps_image is None
        assert b.content_types == ()

    def test_init_creates_surface(self):
        b = _make_backing()
        assert b._backing is not None

    def test_init_skips_create_when_unchanged(self):
        from unittest.mock import patch
        b = _make_backing()
        with patch.object(b, "create_surface") as mock_cs:
            b.init(100, 100, 100, 100)
            mock_cs.assert_not_called()

    def test_init_recreates_on_size_change(self):
        from unittest.mock import patch
        b = _make_backing()
        with patch.object(b, "create_surface", return_value=None) as mock_cs:
            b.init(200, 200, 200, 200)
            mock_cs.assert_called_once()

    def test_wid_zero_alpha_false(self):
        b = _make_backing(wid=42, alpha=False)
        assert b.wid == 42
        assert not b._alpha_enabled


# ---------------------------------------------------------------------------
# create_surface and close
# ---------------------------------------------------------------------------

class TestCairoBackingBaseCreateSurface(unittest.TestCase):

    def test_creates_image_surface(self):
        from cairo import ImageSurface
        b = _make_backing()
        assert isinstance(b._backing, ImageSurface)
        assert b._backing.get_width() == 100
        assert b._backing.get_height() == 100

    def test_zero_size_returns_none(self):
        b = _make_backing()
        b.size = (0, 0)
        result = b.create_surface()
        assert result is None
        assert b._backing is None

    def test_close_finishes_backing(self):
        b = _make_backing()
        assert b._backing is not None
        b.close()
        assert b._backing is None

    def test_close_idempotent(self):
        b = _make_backing()
        b.close()
        b.close()  # should not raise

    def test_copy_old_backing_on_resize(self):
        b = _make_backing(w=50, h=50)
        b.size = (100, 100)
        b.render_size = (100, 100)
        cr = b.create_surface()
        assert cr is not None
        assert b._backing.get_width() == 100

    def test_create_surface_alpha_enabled(self):
        b = _make_backing(alpha=True)
        b.size = (32, 32)
        b.render_size = (32, 32)
        b.create_surface()
        assert b._backing is not None

    def test_create_surface_no_alpha(self):
        b = _make_backing(alpha=False)
        b.size = (32, 32)
        b.render_size = (32, 32)
        b.create_surface()
        assert b._backing is not None


# ---------------------------------------------------------------------------
# get_info
# ---------------------------------------------------------------------------

class TestCairoBackingBaseGetInfo(unittest.TestCase):

    def test_has_type_cairo(self):
        b = _make_backing()
        info = b.get_info()
        assert info.get("type") == "Cairo"

    def test_has_rgb_formats(self):
        b = _make_backing()
        info = b.get_info()
        assert "rgb-formats" in info

    def test_size_in_info(self):
        b = _make_backing(w=80, h=60)
        info = b.get_info()
        assert info.get("size") == (80, 60)


# ---------------------------------------------------------------------------
# cairo_paint_box and cairo_paint_from_source
# ---------------------------------------------------------------------------

class TestCairoPaintBox(unittest.TestCase):

    def test_strokes_rectangle(self):
        from cairo import ImageSurface, Context, Format
        b = _make_backing()
        surface = ImageSurface(Format.ARGB32, 200, 200)
        gc = Context(surface)
        b.cairo_paint_box(gc, "h264", 10, 10, 50, 50)

    def test_unknown_encoding(self):
        from cairo import ImageSurface, Context, Format
        b = _make_backing()
        surface = ImageSurface(Format.ARGB32, 100, 100)
        gc = Context(surface)
        b.cairo_paint_box(gc, "unknown_codec", 0, 0, 100, 100)


class TestCairoPaintFromSource(unittest.TestCase):

    def test_no_backing_returns_early(self):
        from cairo import ImageSurface, Format
        b = _make_backing()
        b._backing = None
        src = ImageSurface(Format.ARGB32, 20, 20)
        called = []
        b.cairo_paint_from_source(lambda gc, s, x, y: called.append(1),
                                  src, 0, 0, 20, 20, 20, 20, {})
        assert not called

    def test_basic_paint(self):
        from cairo import ImageSurface, Format
        b = _make_backing()
        src = ImageSurface(Format.ARGB32, 20, 20)
        b.cairo_paint_from_source(
            lambda gc, s, sx, sy: gc.set_source_surface(s, sx, sy),
            src, 0, 0, 20, 20, 20, 20, {}
        )

    def test_paint_with_scale(self):
        from cairo import ImageSurface, Format
        b = _make_backing()
        src = ImageSurface(Format.ARGB32, 20, 20)
        b.cairo_paint_from_source(
            lambda gc, s, sx, sy: gc.set_source_surface(s, sx, sy),
            src, 0, 0, 20, 20, 40, 40, {}
        )

    def test_paint_with_paint_box(self):
        from cairo import ImageSurface, Format
        from xpra.util.objects import typedict
        b = _make_backing()
        b.paint_box_line_width = 2
        src = ImageSurface(Format.ARGB32, 20, 20)
        opts = typedict({"encoding": "h264"})
        b.cairo_paint_from_source(
            lambda gc, s, sx, sy: gc.set_source_surface(s, sx, sy),
            src, 5, 5, 20, 20, 20, 20, opts
        )


class TestCairoPaintSurface(unittest.TestCase):

    def test_paints_surface(self):
        from cairo import ImageSurface, Format
        b = _make_backing()
        src = ImageSurface(Format.ARGB32, 30, 30)
        b.cairo_paint_surface(src, 0, 0, 30, 30, {})

    def test_paints_scaled(self):
        from cairo import ImageSurface, Format
        b = _make_backing()
        src = ImageSurface(Format.ARGB32, 30, 30)
        b.cairo_paint_surface(src, 0, 0, 60, 60, {})


# ---------------------------------------------------------------------------
# do_paint_rgb
# ---------------------------------------------------------------------------

class TestDoPaintRgb(unittest.TestCase):

    def _callbacks(self):
        results = []
        return results, [lambda s, m: results.append((s, m))]

    def test_skip_paint_false(self):
        from xpra.util.objects import typedict
        b = _make_backing()
        results, cbs = self._callbacks()
        b.do_paint_rgb(None, "", "BGRA", b"\x00" * 400, 0, 0, 10, 10, 10, 10, 40, typedict({"paint": False}), cbs)
        assert results and results[0][0] is True

    def test_no_backing_fires_error(self):
        from xpra.util.objects import typedict
        b = _make_backing()
        b._backing = None
        results, cbs = self._callbacks()
        b.do_paint_rgb(None, "", "BGRA", b"\x00" * 400, 0, 0, 10, 10, 10, 10, 40, typedict(), cbs)
        assert results and results[0][0] == -1

    def test_bgra_32bpp(self):
        from xpra.util.objects import typedict
        b = _make_backing()
        results, cbs = self._callbacks()
        b.do_paint_rgb(None, "", "BGRA", b"\x00" * 400, 0, 0, 10, 10, 10, 10, 40, typedict(), cbs)
        assert results and results[0][0] is True
        assert b._do_paint_rgb_calls

    def test_rgb_24bpp(self):
        from xpra.util.objects import typedict
        b = _make_backing()
        results, cbs = self._callbacks()
        b.do_paint_rgb(None, "", "RGB", b"\x00" * 300, 0, 0, 10, 10, 10, 10, 30, typedict(), cbs)
        assert results and results[0][0] is True

    def test_bgr565_16bpp(self):
        from xpra.util.objects import typedict
        b = _make_backing()
        results, cbs = self._callbacks()
        b.do_paint_rgb(None, "", "BGR565", b"\x00" * 200, 0, 0, 10, 10, 10, 10, 20, typedict(), cbs)
        assert results and results[0][0] is True

    def test_r210_30bpp(self):
        from xpra.util.objects import typedict
        b = _make_backing()
        results, cbs = self._callbacks()
        b.do_paint_rgb(None, "", "r210", b"\x00" * 400, 0, 0, 10, 10, 10, 10, 40, typedict(), cbs)
        assert results and results[0][0] is True

    def test_invalid_format_fires_error(self):
        from xpra.util.objects import typedict
        b = _make_backing()
        results, cbs = self._callbacks()
        # single char → bpp=8 → Format.INVALID
        b.do_paint_rgb(None, "", "X", b"\x00" * 100, 0, 0, 10, 10, 10, 10, 10, typedict(), cbs)
        assert results and results[0][0] is False

    def _paint_format(self, rgb_format: str, alpha=True):
        """the cairo format chosen for painting `rgb_format` pixels"""
        from xpra.util.objects import typedict
        b = _make_backing(alpha=alpha)
        results, cbs = self._callbacks()
        b.do_paint_rgb(None, "", rgb_format, b"\x00" * 400, 0, 0, 10, 10, 10, 10, 40, typedict(), cbs)
        assert results and results[0][0] is True
        assert b._do_paint_rgb_calls, f"{rgb_format} was not painted"
        return b._do_paint_rgb_calls[0][0]

    def test_padding_formats_never_use_argb32(self):
        # the `X` byte is undefined, it must not end up as transparency:
        from cairo import Format
        for rgb_format in ("BGRX", "RGBX"):
            fmt = self._paint_format(rgb_format)
            assert fmt == Format.RGB24, f"{rgb_format} should be painted as RGB24, not {fmt}"

    def test_alpha_formats_use_argb32(self):
        from cairo import Format
        for rgb_format in ("BGRA", "RGBA"):
            fmt = self._paint_format(rgb_format)
            assert fmt == Format.ARGB32, f"{rgb_format} should be painted as ARGB32, not {fmt}"

    def test_alpha_formats_without_window_alpha(self):
        from cairo import Format
        for rgb_format in ("BGRA", "RGBA"):
            fmt = self._paint_format(rgb_format, alpha=False)
            assert fmt == Format.RGB24, f"{rgb_format} should be painted as RGB24, not {fmt}"

    def test_exact_composite_mode_keeps_alpha_and_exact_reset(self):
        from cairo import Format
        b = _make_backing(alpha=False, w=5, h=4)
        results, callbacks = self._callbacks()
        b.do_paint_rgb(
            None, "rgb", "BGRA", bytes(4), 0, 0, 1, 1, 1, 1, 4,
            _composite_options(reset=(0, 1, 2, 3)), callbacks,
        )
        self.assertTrue(results[0][0])
        call = b._do_paint_rgb_calls[0]
        self.assertEqual(call[0], Format.ARGB32)
        self.assertTrue(call[1])
        self.assertTrue(call[-2])
        self.assertEqual(call[-1], (0, 1, 2, 3))

    def test_exact_composite_mode_accepts_x_as_strictly_opaque(self):
        from cairo import Format
        b = _make_backing(alpha=True, w=1, h=1)
        for transaction_id, rgb_format in enumerate(("BGRX", "RGBX"), 1):
            b._do_paint_rgb_calls.clear()
            results, callbacks = self._callbacks()
            b.do_paint_rgb(
                None, "rgb", rgb_format, bytes(4), 0, 0, 1, 1, 1, 1, 4,
                _composite_options(transaction_id), callbacks,
            )
            self.assertTrue(results[0][0])
            call = b._do_paint_rgb_calls[0]
            self.assertEqual(call[0], Format.RGB24)
            self.assertTrue(call[-2])

    def test_composite_mode_is_exact_and_representation_bound(self):
        from xpra.util.objects import typedict
        b = _make_backing(alpha=False, w=1, h=1)

        for invalid_mode in (
                SUBSURFACE_COMPOSITE_MODE + "-unknown", "",
                SUBSURFACE_COMPOSITE_MODE.encode(), b"", None, False, 0,
        ):
            b._do_paint_rgb_calls.clear()
            results, callbacks = self._callbacks()
            b.do_paint_rgb(
                None, "rgb", "BGRA", bytes(4), 0, 0, 1, 1, 1, 1, 4,
                typedict({"subsurface-composite": invalid_mode}), callbacks,
            )
            self.assertFalse(results[0][0], (invalid_mode, results))
            self.assertFalse(b._do_paint_rgb_calls)

        for encoding, rgb_format in (("rgb", "BGR"), ("png", "BGRA")):
            b._do_paint_rgb_calls.clear()
            results, callbacks = self._callbacks()
            b.do_paint_rgb(
                None, encoding, rgb_format, bytes(4), 0, 0, 1, 1, 1, 1, 4,
                typedict({"subsurface-composite": SUBSURFACE_COMPOSITE_MODE}), callbacks,
            )
            self.assertFalse(results[0][0])
            self.assertFalse(b._do_paint_rgb_calls)

    def test_composite_reset_requires_one_positive_integer_rectangle(self):
        from xpra.util.objects import typedict
        b = _make_backing(alpha=False, w=1, h=1)
        for reset in (
                (0, 0, 1), (0, 0, 0, 1), (False, 0, 1, 1), "0,0,1,1",
                (-1, 0, 1, 1), (1, 0, 1, 1), (2, 0, 1, 1),
        ):
            b._do_paint_rgb_calls.clear()
            results, callbacks = self._callbacks()
            b.do_paint_rgb(
                None, "rgb", "BGRA", bytes(4), 0, 0, 1, 1, 1, 1, 4,
                typedict({
                    "subsurface-composite": SUBSURFACE_COMPOSITE_MODE,
                    "subsurface-reset": reset,
                }), callbacks,
            )
            self.assertFalse(results[0][0], (reset, results))
            self.assertFalse(b._do_paint_rgb_calls)

    def test_draw_region_rejects_non_rgb32_composite_ingress(self):
        from xpra.util.objects import typedict
        b = _make_backing(alpha=False, w=1, h=1)
        for coding in ("rgb24", "png", "jpeg", "h264", "scroll"):
            queued = []
            b.with_gfx_context = lambda function, *args: queued.append((function, args))
            results = []
            b.draw_region(
                0, 0, 1, 1, coding, bytes(4), 4,
                typedict({"subsurface-composite": SUBSURFACE_COMPOSITE_MODE}),
                [lambda success, message="": results.append((success, message))],
            )
            self.assertEqual(results, [], coding)
            self.assertEqual(len(queued), 1, coding)
            function, args = queued.pop()
            function(None, *args)
            self.assertEqual(len(results), 1, (coding, results))
            self.assertFalse(results[0][0], (coding, results))
            self.assertIn(coding, results[0][1])
            self.assertFalse(b._do_paint_rgb_calls)

    def test_draw_region_rejects_unknown_or_compressed_composite_on_ui_boundary(self):
        from xpra.util.objects import typedict

        b = _make_backing(alpha=False, w=1, h=1)
        cases = (
            typedict({"subsurface-composite": SUBSURFACE_COMPOSITE_MODE + "-unknown"}),
            typedict({"subsurface-composite": ""}),
            typedict({"subsurface-composite": False}),
            typedict({"subsurface-composite": SUBSURFACE_COMPOSITE_MODE, "lz4": 1}),
            typedict({"subsurface-composite": SUBSURFACE_COMPOSITE_MODE, "zstd": True}),
        )
        for options in cases:
            queued = []
            b.with_gfx_context = lambda function, *args: queued.append((function, args))
            results = []
            b.draw_region(
                0, 0, 1, 1, "rgb32", bytes(4), 4, options,
                [lambda success, message="": results.append((success, message))],
            )
            self.assertEqual(results, [])
            self.assertEqual(len(queued), 1)
            function, args = queued.pop()
            function(None, *args)
            self.assertEqual(len(results), 1)
            self.assertFalse(results[0][0], (options, results))
            self.assertFalse(b._do_paint_rgb_calls)

    def test_composited_mmap_is_drained_then_rejected_on_ui_boundary(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch
        from xpra.util.objects import typedict
        b = _make_backing(alpha=False, w=1, h=1)
        b.mmap = SimpleNamespace(mmap=object())
        queued = []
        b.with_gfx_context = lambda function, *args: queued.append((function, args))
        free_cb = MagicMock()
        mmap_result = memoryview(bytes(4)), free_cb
        results = []
        with patch("xpra.net.mmap.io.mmap_read", return_value=mmap_result):
            options = typedict({
                "rgb_format": "BGRA",
                "subsurface-composite": SUBSURFACE_COMPOSITE_MODE,
                SUBSURFACE_TRANSACTION_ID: 1,
            })
            b.draw_region(
                0, 0, 1, 1, "mmap", (), 4, options,
                [lambda success, message="": results.append((success, message))],
            )

        self.assertEqual(results, [])
        self.assertEqual(len(queued), 1)
        function, args = queued.pop()
        function(None, *args)
        self.assertFalse(results[0][0])
        self.assertIn("mmap", results[0][1])
        free_cb.assert_called_once()
        self.assertFalse(b._do_paint_rgb_calls)

    def test_composited_rgb_uses_scaled_source_size(self):
        from xpra.util.objects import typedict
        b = _make_backing(alpha=False, w=8, h=6)
        calls = []
        b.ui_paint_rgb = lambda *args: calls.append(args)
        b.paint_rgb("BGRX", bytes(2 * 3 * 4), 1, 1, 8, 6, 2 * 4, typedict({
            "scaled_size": (2, 3),
            "subsurface-composite": SUBSURFACE_COMPOSITE_MODE,
        }), [])
        self.assertEqual(calls[-1][3:9], (1, 1, 2, 3, 8, 6))

        # Unknown/default packet modes retain the existing source dimensions.
        b.paint_rgb("BGRX", bytes(8 * 6 * 4), 1, 1, 8, 6, 8 * 4, typedict({
            "scaled_size": (2, 3),
        }), [])
        self.assertEqual(calls[-1][3:9], (1, 1, 8, 6, 8, 6))

        for scaled_size in (("2", 3), (True, 3), (0, 3), (2,), "2,3"):
            with self.subTest(scaled_size=scaled_size), \
                    self.assertRaisesRegex(ValueError, "invalid subsurface scaled size"):
                b.paint_rgb("BGRX", bytes(2 * 3 * 4), 1, 1, 8, 6, 2 * 4, typedict({
                    "scaled_size": scaled_size,
                    "subsurface-composite": SUBSURFACE_COMPOSITE_MODE,
                }), [])
        self.assertEqual(len(calls), 2)


class TestSubsurfaceCompositeTransactions(unittest.TestCase):

    @staticmethod
    def paint(backing, options, callback_observations=None):
        results = []

        def callback(success, message=""):
            results.append((success, message))
            if callback_observations is not None:
                visible = backing._backing
                visible.flush()
                callback_observations.append((visible, bytes(visible.get_data())))

        backing.do_paint_rgb(
            None, "rgb", "BGRA", bytes((0, 0, 0xff, 0xff)),
            0, 0, 1, 1, 1, 1, 4, options,
            [callback],
        )
        return results

    def test_all_transaction_options_are_mandatory_and_exact_integers(self):
        backing = _make_backing(w=1, h=1)
        self.addCleanup(backing.close)
        for name in SUBSURFACE_TRANSACTION_OPTIONS:
            options = _composite_options()
            del options[name]
            results = self.paint(backing, options)
            self.assertEqual(len(results), 1)
            self.assertFalse(results[0][0], (name, results))
        for name in SUBSURFACE_TRANSACTION_OPTIONS:
            options = _composite_options()
            options[name] = True
            results = self.paint(backing, options)
            self.assertEqual(len(results), 1)
            self.assertFalse(results[0][0], (name, results))

        malformed = _composite_options(2)
        del malformed[SUBSURFACE_TRANSACTION_OPTIONS[-1]]
        self.assertFalse(self.paint(backing, malformed)[0][0])
        self.assertFalse(self.paint(backing, _composite_options(2))[0][0])
        self.assertTrue(self.paint(backing, _composite_options(3))[0][0])

    def test_order_duplicate_epoch_and_metadata_fail_closed(self):
        backing = _make_backing(w=1, h=1)
        self.addCleanup(backing.close)

        # A transaction can only begin at stage zero.
        self.assertFalse(self.paint(
            backing, _composite_options(1, 1, 2, reset=None),
        )[0][0])

        # Repeating a completed or active stage invalidates the whole ID.
        self.assertTrue(self.paint(backing, _composite_options(2, 0, 2))[0][0])
        self.assertFalse(self.paint(backing, _composite_options(2, 0, 2))[0][0])
        self.assertIsNone(backing._subsurface_transaction)
        self.assertFalse(self.paint(backing, _composite_options(2))[0][0])

        # Immutable metadata and exact order are checked on every stage.
        self.assertTrue(self.paint(backing, _composite_options(3, 0, 2))[0][0])
        self.assertFalse(self.paint(
            backing, _composite_options(3, 1, 2, topology_epoch=2, reset=None),
        )[0][0])
        self.assertIsNone(backing._subsurface_transaction)

        self.assertTrue(self.paint(
            backing, _composite_options(4, topology_epoch=3, backing_epoch=3),
        )[0][0])
        self.assertFalse(self.paint(
            backing, _composite_options(5, topology_epoch=2, backing_epoch=3),
        )[0][0])
        self.assertFalse(self.paint(
            backing, _composite_options(6, topology_epoch=3, backing_epoch=2),
        )[0][0])

    def test_delivery_state_rejects_overtaken_draw_without_wire_epoch_floor(self):
        backing = _make_backing(w=1, h=1)
        self.addCleanup(backing.close)
        self.assertTrue(self.paint(
            backing, _composite_options(1, backing_epoch=1),
        )[0][0])

        def delivered(options, target=backing):
            options[SUBSURFACE_CLIENT_BACKING_STATE] = (
                target,
                target._subsurface_client_backing_generation,
                target._subsurface_local_backing_epoch,
            )
            return options

        # Client-side display scaling replaces private staging and rendering
        # resources. A packet captured before that transition is stale, while
        # a newly delivered packet may still use the current wire epoch.
        old_render = delivered(_composite_options(2, backing_epoch=1))
        backing.init(2, 1, 1, 1)
        self.assertFalse(self.paint(backing, old_render)[0][0])
        self.assertTrue(self.paint(
            backing, delivered(_composite_options(3, backing_epoch=1)),
        )[0][0])

        old_canvas = delivered(_composite_options(
            4, backing_epoch=1, reset=(0, 0, 1, 1),
        ))
        backing.init(2, 1, 2, 1)
        self.assertFalse(self.paint(backing, old_canvas)[0][0])
        self.assertTrue(self.paint(
            backing, delivered(_composite_options(
                5, backing_epoch=1, reset=(0, 0, 2, 1),
            )),
        )[0][0])

        # A replacement which happens after the decode-thread check is still
        # rejected by object identity inside the eventual UI paint.
        replacement = _make_backing(w=2, h=1)
        self.addCleanup(replacement.close)
        self.assertFalse(self.paint(
            replacement, delivered(_composite_options(
                6, backing_epoch=1, reset=(0, 0, 2, 1),
            )),
        )[0][0])

    def test_stage_shape_reset_and_flush_contract(self):
        from xpra.util.objects import typedict

        backing = _make_backing(w=1, h=1)
        self.addCleanup(backing.close)
        invalid = (
            _composite_options(1, reset=None),
            _composite_options(2, 1, 2, reset=(0, 0, 1, 1)),
            _composite_options(3, 2, 2, reset=None),
            _composite_options(4, stage_count=0),
            _composite_options(5, stage_index=-1),
        )
        for options in invalid:
            self.assertFalse(self.paint(backing, options)[0][0])
        wrong_flush = _composite_options(6, 0, 2)
        wrong_flush["flush"] = 0
        self.assertFalse(self.paint(backing, wrong_flush)[0][0])
        with self.assertRaisesRegex(ValueError, "invalid .* encoding"):
            backing.get_subsurface_composite_stage(
                "mmap", "BGRA", typedict(_composite_options(7)),
            )
        compressed = _composite_options(8)
        compressed["lz4"] = 1
        with self.assertRaisesRegex(ValueError, "requires uncompressed pixels"):
            backing.get_subsurface_composite_stage("rgb", "BGRA", typedict(compressed))

    def test_malformed_pixel_geometry_fails_before_staging(self):
        backing = _make_backing(w=2, h=1)
        self.addCleanup(backing.close)
        options = _composite_options(1, reset=(0, 0, 2, 1))
        results = []
        backing.do_paint_rgb(
            None, "rgb", "BGRA", bytes(4),
            0, 0, 2, 1, 2, 1, 4, options,
            [lambda success, message="": results.append((success, message))],
        )
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0][0])
        self.assertIsNone(backing._subsurface_transaction)
        self.assertIsNone(backing._subsurface_staging_surface)

    def test_new_transaction_resize_close_and_ordinary_paint_discard_staging(self):
        from xpra.util.objects import typedict

        backing = _make_backing(w=2, h=1)

        self.assertTrue(self.paint(backing, _composite_options(1, 0, 2, reset=(0, 0, 2, 1)))[0][0])
        first_staging = backing._subsurface_staging_surface
        self.assertIsNotNone(first_staging)
        # A newer complete transaction supersedes the incomplete one.
        self.assertTrue(self.paint(backing, _composite_options(2, reset=(0, 0, 2, 1)))[0][0])
        self.assertIsNone(backing._subsurface_staging_surface)
        self.assertIsNone(backing._subsurface_transaction)

        self.assertTrue(self.paint(backing, _composite_options(3, 0, 2, reset=(0, 0, 2, 1)))[0][0])
        backing.init(3, 1, 3, 1)
        self.assertIsNone(backing._subsurface_staging_surface)
        self.assertIsNone(backing._subsurface_transaction)
        self.assertFalse(self.paint(backing, _composite_options(3, 1, 2, reset=None))[0][0])

        self.assertTrue(self.paint(
            backing, _composite_options(4, 0, 2, reset=(0, 0, 3, 1)),
        )[0][0])
        ordinary_results = []
        backing.do_paint_rgb(
            None, "rgb", "BGRA", bytes(4), 0, 0, 1, 1, 1, 1, 4, typedict(),
            [lambda success, message="": ordinary_results.append((success, message))],
        )
        self.assertTrue(ordinary_results[0][0])
        self.assertIsNone(backing._subsurface_transaction)
        self.assertIsNone(backing._subsurface_staging_surface)

        self.assertTrue(self.paint(
            backing, _composite_options(5, 0, 2, reset=(0, 0, 3, 1)),
        )[0][0])
        backing.close()
        self.assertIsNone(backing._subsurface_transaction)
        self.assertIsNone(backing._subsurface_staging_surface)

    def test_void_packet_discards_staging_at_ui_boundary(self):
        backing = _make_backing(w=2, h=1)
        self.addCleanup(backing.close)
        self.assertTrue(self.paint(
            backing, _composite_options(1, 0, 2, reset=(0, 0, 2, 1)),
        )[0][0])
        self.assertIsNotNone(backing._subsurface_transaction)

        queued = []
        results = []
        backing.with_gfx_context = lambda function, *args: queued.append((function, args))
        backing.paint_void([lambda success, message="": results.append((success, message))])
        self.assertEqual(results, [])
        self.assertIsNotNone(backing._subsurface_transaction)
        function, args = queued.pop()
        function(None, *args)
        self.assertTrue(results[0][0])
        self.assertIsNone(backing._subsurface_transaction)
        self.assertIsNone(backing._subsurface_staging_surface)

    def test_first_middle_and_final_failure_keep_visible_backing(self):
        from unittest.mock import patch

        for failure_stage in range(3):
            with self.subTest(failure_stage=failure_stage):
                backing = _make_backing(w=1, h=1)
                visible = backing._backing
                visible.flush()
                before = bytes(visible.get_data())

                def paint_or_fail(*args):
                    options = args[-3]
                    if options.intget(SUBSURFACE_STAGE_INDEX) == failure_stage:
                        raise RuntimeError("injected transaction stage failure")

                all_results = []
                callback_observations = []
                with patch.object(backing, "_do_paint_rgb", side_effect=paint_or_fail):
                    for stage_index in range(failure_stage + 1):
                        results = self.paint(backing, _composite_options(
                            1, stage_index, 3,
                            reset=(0, 0, 1, 1) if stage_index == 0 else None,
                        ), callback_observations)
                        self.assertEqual(len(results), 1)
                        all_results += results

                self.assertEqual(len(all_results), failure_stage + 1)
                self.assertFalse(all_results[-1][0])
                self.assertTrue(all(result[0] for result in all_results[:-1]))
                self.assertIs(backing._backing, visible)
                visible.flush()
                self.assertEqual(bytes(visible.get_data()), before)
                self.assertTrue(all(
                    callback_visible is visible and callback_pixels == before
                    for callback_visible, callback_pixels in callback_observations
                ))
                self.assertIsNone(backing._subsurface_transaction)
                self.assertIsNone(backing._subsurface_staging_surface)
                backing.close()

    def test_successful_commit_swaps_surface_and_finishes_previous_backing(self):
        from unittest.mock import MagicMock

        backing = _make_backing(w=1, h=1)
        backing._backing.finish()
        old_backing = MagicMock()
        staging = MagicMock()
        backing._backing = old_backing
        backing._subsurface_staging_surface = staging
        backing._commit_subsurface_staging(None)
        self.assertIs(backing._backing, staging)
        self.assertIsNone(backing._subsurface_staging_surface)
        staging.flush.assert_called_once()
        old_backing.finish.assert_called_once()

    def test_commit_cleanup_error_does_not_reclassify_published_surface(self):
        from unittest.mock import MagicMock

        backing = _make_backing(w=1, h=1)
        backing._backing.finish()
        old_backing = MagicMock()
        old_backing.finish.side_effect = RuntimeError("injected old-surface cleanup failure")
        staging = MagicMock()
        backing._backing = old_backing
        backing._subsurface_staging_surface = staging

        # Once the private surface has been published there is no rollback.
        # Cleanup failure must leave that commit successful and usable.
        backing._commit_subsurface_staging(None)
        self.assertIs(backing._backing, staging)
        self.assertIsNone(backing._subsurface_staging_surface)
        staging.flush.assert_called_once()
        old_backing.finish.assert_called_once()

    def test_post_commit_accounting_error_keeps_successful_ack(self):
        from unittest.mock import patch

        backing = _make_backing(w=1, h=1)
        self.addCleanup(backing.close)
        old_backing = backing._backing
        with patch.object(
                backing, "record_fps_event",
                side_effect=RuntimeError("injected post-commit accounting failure"),
        ):
            results = self.paint(backing, _composite_options())

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0][0], results)
        self.assertIsNot(backing._backing, old_backing)
        self.assertIsNone(backing._subsurface_transaction)
        self.assertEqual(backing._subsurface_transaction_floor, 1)

    def test_close_discards_private_surface_even_if_visible_finish_fails(self):
        from unittest.mock import MagicMock

        backing = _make_backing(w=1, h=1)
        backing._backing.finish()
        visible = MagicMock()
        visible.finish.side_effect = RuntimeError("injected visible-surface cleanup failure")
        staging = MagicMock()
        backing._backing = visible
        backing._subsurface_staging_surface = staging

        with self.assertRaisesRegex(RuntimeError, "visible-surface cleanup failure"):
            backing.close()
        self.assertIsNone(backing._backing)
        self.assertIsNone(backing._subsurface_staging_surface)
        staging.finish.assert_called_once()

    def test_discard_cleanup_error_does_not_escape_transaction_invalidation(self):
        from unittest.mock import MagicMock

        backing = _make_backing(w=1, h=1)
        self.addCleanup(backing.close)
        staging = MagicMock()
        staging.finish.side_effect = RuntimeError("injected staging cleanup failure")
        backing._subsurface_staging_surface = staging

        backing.invalidate_subsurface_transaction()
        self.assertIsNone(backing._subsurface_staging_surface)
        staging.finish.assert_called_once()


class TestPremultipliedSubsurfaceComposite(unittest.TestCase):

    @staticmethod
    def options(transaction_id=1, stage_index=0, stage_count=1,
                reset=(0, 0, 1, 1), topology_epoch=1, backing_epoch=1):
        return _composite_options(
            transaction_id, stage_index, stage_count,
            topology_epoch, backing_epoch, reset,
        )

    @staticmethod
    def pixels(backing) -> bytes:
        backing._backing.flush()
        return bytes(backing._backing.get_data())

    @staticmethod
    def paint(backing, pixels: bytes, x: int, width: int, options,
              encoding="rgb", rgb_format="BGRA", render_width=0):
        results = []
        render_width = render_width or width
        backing.do_paint_rgb(
            None, encoding, rgb_format, pixels,
            x, 0, width, 1, render_width, 1, width * 4, options,
            [lambda success, message="": results.append((success, message))],
        )
        return results

    def test_reset_source_over_and_repeat_on_opaque_backing(self):
        from unittest.mock import patch
        from xpra.cairo.backing import CairoBacking
        from xpra.cairo import backing as backing_module

        backing = CairoBacking(1, False)
        backing.init(4, 1, 4, 1)
        self.addCleanup(backing.close)
        # X is deliberately zero: RGB24 must still expose this as opaque.
        opaque_blue = bytes((0xff, 0, 0, 0xff))
        expected = opaque_blue + bytes((0x7f, 0, 0x80, 0xff)) * 2 + opaque_blue

        # The negotiated mode must never fall through GdkPixbuf: that path
        # interprets RGBA as straight-alpha rather than Wayland premultiplied.
        with patch.object(backing_module, "CAIRO_USE_PIXBUF", True), \
                patch.object(backing, "cairo_paint_pixbuf", side_effect=AssertionError("pixbuf path used")):
            transaction_id = 1
            for opaque_format, blue in (
                    ("BGRX", bytes((0xff, 0, 0, 0))),
                    ("RGBX", bytes((0, 0, 0xff, 0))),
            ):
                for alpha_format, half_red in (
                        ("BGRA", bytes((0, 0, 0x80, 0x80))),
                        ("RGBA", bytes((0x80, 0, 0, 0x80))),
                    ):
                    for _ in range(2):
                        before = self.pixels(backing)
                        self.assertTrue(self.paint(
                            backing, blue * 4, 0, 4,
                            self.options(transaction_id, 0, 2, (0, 0, 4, 1)),
                            rgb_format=opaque_format,
                        )[0][0])
                        self.assertEqual(self.pixels(backing), before)
                        self.assertTrue(self.paint(
                            backing, half_red, 1, 1,
                            self.options(transaction_id, 1, 2, None),
                            rgb_format=alpha_format, render_width=2,
                        )[0][0])
                        self.assertEqual(self.pixels(backing)[:16], expected)
                        transaction_id += 1

        # An in-bounds reset clears only its exact region.
        transparent = bytes(4)
        self.assertTrue(self.paint(
            backing, transparent, 1, 1,
            self.options(transaction_id, reset=(1, 0, 2, 1)), render_width=2,
        )[0][0])
        self.assertEqual(self.pixels(backing)[:16], opaque_blue + transparent * 2 + opaque_blue)

    def test_composite_rejects_ambiguous_representation_before_paint(self):
        from xpra.cairo.backing import CairoBacking

        backing = CairoBacking(1, False)
        backing.init(1, 1, 1, 1)
        self.addCleanup(backing.close)
        before = self.pixels(backing)
        transaction_id = 1
        for encoding, rgb_format in (("rgb", "BGR"), ("png", "BGRA")):
            results = self.paint(
                backing, bytes(4), 0, 1,
                self.options(transaction_id), encoding, rgb_format,
            )
            self.assertFalse(results[0][0], (encoding, rgb_format, results))
            self.assertEqual(self.pixels(backing), before)
            transaction_id += 1

    def test_failed_final_stage_discards_modified_surface_before_callback(self):
        from unittest.mock import patch
        from xpra.cairo.backing import CairoBacking

        backing = CairoBacking(1, False)
        backing.init(2, 1, 2, 1)
        self.addCleanup(backing.close)
        visible = backing._backing
        before = self.pixels(backing)

        first = self.paint(
            backing, bytes((0xff, 0, 0, 0)) * 2, 0, 2,
            self.options(1, 0, 2, (0, 0, 2, 1)), rgb_format="BGRX",
        )
        self.assertEqual(len(first), 1)
        self.assertTrue(first[0][0])
        self.assertIs(backing._backing, visible)
        self.assertEqual(self.pixels(backing), before)

        observations = []

        def callback(success, message=""):
            observations.append((success, backing._backing, self.pixels(backing), message))

        with patch.object(backing, "cairo_paint_surface", side_effect=RuntimeError("final-stage failure")):
            backing.do_paint_rgb(
                None, "rgb", "BGRA", bytes((0, 0, 0x80, 0x80)),
                0, 0, 1, 1, 1, 1, 4, self.options(1, 1, 2, None), [callback],
            )
        self.assertEqual(len(observations), 1)
        self.assertFalse(observations[0][0])
        self.assertIs(observations[0][1], visible)
        self.assertEqual(observations[0][2], before)
        self.assertIsNone(backing._subsurface_transaction)
        self.assertIsNone(backing._subsurface_staging_surface)

    def test_unsupported_final_coding_discards_staging_without_publish(self):
        from xpra.cairo.backing import CairoBacking

        backing = CairoBacking(1, False)
        backing.init(2, 1, 2, 1)
        self.addCleanup(backing.close)
        visible = backing._backing
        before = self.pixels(backing)

        first = self.paint(
            backing, bytes((0xff, 0, 0, 0)) * 2, 0, 2,
            self.options(1, 0, 2, (0, 0, 2, 1)), rgb_format="BGRX",
        )
        self.assertTrue(first[0][0])
        self.assertIsNotNone(backing._subsurface_staging_surface)

        # Exercise the public dispatch route used after WindowDraw deliberately
        # defers composite coding validation to the transaction owner.
        backing.with_gfx_context = lambda function, *args: function(None, *args)
        results = []
        backing.draw_region(
            0, 0, 1, 1, "unsupported-picture", bytes(4), 4,
            self.options(1, 1, 2, None),
            [lambda success, message="": results.append((success, message))],
        )

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0][0])
        self.assertIn("does not accept", results[0][1])
        self.assertIs(backing._backing, visible)
        self.assertEqual(self.pixels(backing), before)
        self.assertIsNone(backing._subsurface_transaction)
        self.assertIsNone(backing._subsurface_staging_surface)


# ---------------------------------------------------------------------------
# do_paint_scroll
# ---------------------------------------------------------------------------

class TestDoPaintScroll(unittest.TestCase):

    def _callbacks(self):
        results = []
        return results, [lambda s, m="": results.append((s, m))]

    def test_no_backing_fires_error(self):
        b = _make_backing()
        b._backing = None
        results, cbs = self._callbacks()
        b.do_paint_scroll([(0, 0, 10, 10, 5, 5)], cbs)
        assert results and results[0][0] is False

    def test_scroll_copies_region(self):
        results, cbs = self._callbacks()
        b = _make_backing()
        b.do_paint_scroll([(0, 0, 50, 50, 5, 5)], cbs)
        assert results and results[0][0] is True

    def test_scroll_with_paint_box(self):
        results, cbs = self._callbacks()
        b = _make_backing()
        b.paint_box_line_width = 2
        b.do_paint_scroll([(0, 0, 50, 50, 5, 5)], cbs)
        assert results and results[0][0] is True


# ---------------------------------------------------------------------------
# paint_backing_offset_border and clip_to_backing
# ---------------------------------------------------------------------------

class TestPaintBackingOffsetBorder(unittest.TestCase):

    def _ctx(self, w=200, h=200):
        from cairo import ImageSurface, Context, Format
        return Context(ImageSurface(Format.ARGB32, w, h))

    def test_zero_offsets_noop(self):
        b = _make_backing()
        b.offsets = (0, 0, 0, 0)
        ctx = self._ctx()
        b.paint_backing_offset_border(ctx, 200, 200)

    def test_with_offsets_paints_padding(self):
        b = _make_backing()
        b.offsets = (10, 5, 10, 5)
        ctx = self._ctx()
        b.paint_backing_offset_border(ctx, 200, 200)


class TestClipToBacking(unittest.TestCase):

    def _ctx(self, w=200, h=200):
        from cairo import ImageSurface, Context, Format
        return Context(ImageSurface(Format.ARGB32, w, h))

    def test_zero_render_size_returns_false(self):
        b = _make_backing()
        b.render_size = (0, 0)
        ctx = self._ctx()
        result = b.clip_to_backing(ctx, 200, 200)
        assert result is False

    def test_matching_sizes_returns_true(self):
        b = _make_backing(w=100, h=100)
        b.offsets = (0, 0, 0, 0)
        ctx = self._ctx()
        result = b.clip_to_backing(ctx, 100, 100)
        assert result is True

    def test_different_sizes_scales(self):
        b = _make_backing(w=50, h=50)
        b.render_size = (100, 100)
        b.offsets = (0, 0, 0, 0)
        ctx = self._ctx(100, 100)
        result = b.clip_to_backing(ctx, 100, 100)
        assert result is True

    def test_with_offsets(self):
        b = _make_backing(w=100, h=100)
        b.offsets = (5, 5, 5, 5)
        ctx = self._ctx()
        result = b.clip_to_backing(ctx, 100, 100)
        assert result is True


# ---------------------------------------------------------------------------
# cairo_draw
# ---------------------------------------------------------------------------

class TestCairoDraw(unittest.TestCase):

    def _ctx(self, w=200, h=200):
        from cairo import ImageSurface, Context, Format
        return Context(ImageSurface(Format.ARGB32, w, h))

    def test_no_backing_returns_early(self):
        b = _make_backing()
        b._backing = None
        ctx = self._ctx()
        b.cairo_draw(ctx, 100, 100)

    def test_basic_draw(self):
        b = _make_backing(w=100, h=100)
        ctx = self._ctx()
        b.cairo_draw(ctx, 100, 100)

    def test_draw_with_scaling(self):
        b = _make_backing(w=50, h=50)
        b.render_size = (100, 100)
        ctx = self._ctx()
        b.cairo_draw(ctx, 100, 100)


# ---------------------------------------------------------------------------
# cairo_draw_pointer
# ---------------------------------------------------------------------------

class TestCairoDrawPointer(unittest.TestCase):

    def _ctx(self, w=200, h=200):
        from cairo import ImageSurface, Context, Format
        return Context(ImageSurface(Format.ARGB32, w, h))

    def test_no_overlay_noop(self):
        b = _make_backing()
        b.pointer_overlay = ()
        ctx = self._ctx()
        b.cairo_draw_pointer(ctx)

    def test_no_cursor_data_noop(self):
        from time import monotonic
        b = _make_backing()
        b.pointer_overlay = (0, 0, 10, 10, 5, monotonic())
        b.cursor_data = ()
        b.default_cursor_data = ()
        ctx = self._ctx()
        b.cairo_draw_pointer(ctx)

    def test_with_cursor_data_and_overlay(self):
        from unittest.mock import patch
        from time import monotonic
        from cairo import ImageSurface, Format
        b = _make_backing()
        cw, ch = 8, 8
        pixels = b"\x80\x40\x20\xFF" * (cw * ch)
        b.cursor_data = [None, None, None, cw, ch, 1, 1, None, pixels]
        b.pointer_overlay = (0, 0, 20, 20, 5, monotonic())
        ctx = self._ctx()
        fake_surface = ImageSurface(Format.ARGB32, cw, ch)
        with patch("xpra.cairo.backing_base.make_image_surface", return_value=fake_surface):
            b.cairo_draw_pointer(ctx)


# ---------------------------------------------------------------------------
# cairo_draw_border
# ---------------------------------------------------------------------------

class TestCairoDrawBorder(unittest.TestCase):

    def _ctx(self, w=200, h=200):
        from cairo import ImageSurface, Context, Format
        return Context(ImageSurface(Format.ARGB32, w, h))

    def test_none_border_noop(self):
        b = _make_backing()
        ctx = self._ctx()
        b.cairo_draw_border(ctx, None)

    def test_hidden_border_noop(self):
        from xpra.client.gui.window_border import WindowBorder
        b = _make_backing()
        ctx = self._ctx()
        border = WindowBorder(shown=False)
        b.cairo_draw_border(ctx, border)

    def test_shown_border_paints(self):
        from xpra.client.gui.window_border import WindowBorder
        b = _make_backing(w=100, h=100)
        ctx = self._ctx()
        border = WindowBorder(shown=True, red=1.0, green=0.0, blue=0.0, alpha=0.5, size=4)
        b.cairo_draw_border(ctx, border)

    def test_border_larger_than_backing(self):
        from xpra.client.gui.window_border import WindowBorder
        b = _make_backing(w=10, h=10)
        ctx = self._ctx(10, 10)
        border = WindowBorder(shown=True, size=20)
        b.cairo_draw_border(ctx, border)


# ---------------------------------------------------------------------------
# Alert methods
# ---------------------------------------------------------------------------

class TestDrawAlertShade(unittest.TestCase):

    def _ctx(self, w=200, h=200):
        from cairo import ImageSurface, Context, Format
        return Context(ImageSurface(Format.ARGB32, w, h))

    def test_default_shade(self):
        b = _make_backing(w=100, h=100)
        ctx = self._ctx()
        b.draw_alert_shade(ctx)

    def test_custom_shade(self):
        b = _make_backing(w=100, h=100)
        ctx = self._ctx()
        b.draw_alert_shade(ctx, shade=0.8)


class TestDrawAlertSpinner(unittest.TestCase):

    def _ctx(self, w=200, h=200):
        from cairo import ImageSurface, Context, Format
        return Context(ImageSurface(Format.ARGB32, w, h))

    def test_default_spinner(self):
        b = _make_backing(w=200, h=200)
        ctx = self._ctx()
        b.draw_alert_spinner(ctx)

    def test_small_spinner(self):
        b = _make_backing(w=200, h=200)
        ctx = self._ctx()
        b.draw_alert_spinner(ctx, outer_pct=40)

    def test_big_spinner(self):
        b = _make_backing(w=200, h=200)
        ctx = self._ctx()
        b.draw_alert_spinner(ctx, outer_pct=90)


class TestGetAlertImage(unittest.TestCase):

    def test_returns_tuple(self):
        from xpra.cairo.backing_base import CairoBackingBase
        # reset cached value to force re-evaluation
        CairoBackingBase.alert_image = ()
        result = CairoBackingBase.get_alert_image()
        assert isinstance(result, tuple)

    def test_cached_on_second_call(self):
        from xpra.cairo.backing_base import CairoBackingBase
        r1 = CairoBackingBase.get_alert_image()
        r2 = CairoBackingBase.get_alert_image()
        assert r1 is r2


class TestDrawAlertIcon(unittest.TestCase):

    def _ctx(self, w=200, h=200):
        from cairo import ImageSurface, Context, Format
        return Context(ImageSurface(Format.ARGB32, w, h))

    def test_no_image_noop(self):
        from unittest.mock import patch
        from xpra.cairo.backing_base import CairoBackingBase
        b = _make_backing()
        ctx = self._ctx()
        with patch.object(CairoBackingBase, "get_alert_image", staticmethod(lambda: (0, 0, None))):
            b.draw_alert_icon(ctx)

    def test_with_image(self):
        from unittest.mock import patch
        from cairo import ImageSurface, Format
        from xpra.cairo.backing_base import CairoBackingBase
        b = _make_backing(w=100, h=100)
        ctx = self._ctx()
        fake_img = ImageSurface(Format.ARGB32, 32, 32)
        with patch.object(CairoBackingBase, "get_alert_image", staticmethod(lambda: (32, 32, fake_img))):
            b.draw_alert_icon(ctx)


class TestCairoDrawAlert(unittest.TestCase):

    def _ctx(self, w=200, h=200):
        from cairo import ImageSurface, Context, Format
        return Context(ImageSurface(Format.ARGB32, w, h))

    def test_no_alert_state_noop(self):
        b = _make_backing()
        b.alert_state = False
        ctx = self._ctx()
        b.cairo_draw_alert(ctx)

    def test_shade_mode(self):
        from xpra.util.env import OSEnvContext
        b = _make_backing(w=100, h=100)
        b.alert_state = True
        ctx = self._ctx()
        with OSEnvContext(XPRA_ALERT_MODE="shade"):
            from xpra.client.gui.window import backing as backing_mod
            orig = backing_mod.ALERT_MODE
            backing_mod.ALERT_MODE = ["shade"]
            try:
                b.cairo_draw_alert(ctx)
            finally:
                backing_mod.ALERT_MODE = orig

    def test_spinner_mode(self):
        b = _make_backing(w=200, h=200)
        b.alert_state = True
        ctx = self._ctx()
        from xpra.client.gui.window import backing as backing_mod
        orig = backing_mod.ALERT_MODE
        backing_mod.ALERT_MODE = ["spinner"]
        try:
            b.cairo_draw_alert(ctx)
        finally:
            backing_mod.ALERT_MODE = orig

    def test_icon_mode_no_image(self):
        from unittest.mock import patch
        from xpra.cairo.backing_base import CairoBackingBase
        from xpra.client.gui.window import backing as backing_mod
        b = _make_backing(w=100, h=100)
        b.alert_state = True
        ctx = self._ctx()
        orig = backing_mod.ALERT_MODE
        backing_mod.ALERT_MODE = ["icon"]
        try:
            with patch.object(CairoBackingBase, "get_alert_image", staticmethod(lambda: (0, 0, None))):
                b.cairo_draw_alert(ctx)
        finally:
            backing_mod.ALERT_MODE = orig


def load_tests(_loader, tests, _pattern):
    # Establish the actual visible-pixel failure on clean source before tests
    # of newly introduced transaction helpers. All remaining methods still run
    # on a successful candidate, with identical ordering in every patch mode.
    first = TestPremultipliedSubsurfaceComposite(
        "test_failed_final_stage_discards_modified_surface_before_callback",
    )

    def remaining(suite):
        for test in suite:
            if isinstance(test, unittest.TestSuite):
                yield from remaining(test)
            elif test.id() != first.id():
                yield test

    return unittest.TestSuite((first, *remaining(tests)))


def main():
    unittest.main(failfast=True)


if __name__ == '__main__':
    main()
