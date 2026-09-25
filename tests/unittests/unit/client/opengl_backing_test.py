#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

from OpenGL.GL import GL_RGBA8, GL_RGB8, GL_RGBA4, GL_RGB5_A1, GL_RGB565, GL_RGBA16, GL_RGB10_A2, GL_NEAREST, GL_LINEAR


SUBSURFACE_COMPOSITE_MODE = "premultiplied-source-over-v1"
SUBSURFACE_TRANSACTION_ID = "subsurface-transaction-id"
SUBSURFACE_STAGE_INDEX = "subsurface-stage-index"
SUBSURFACE_STAGE_COUNT = "subsurface-stage-count"
SUBSURFACE_TOPOLOGY_EPOCH = "subsurface-topology-epoch"
SUBSURFACE_BACKING_EPOCH = "subsurface-backing-epoch"


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


class TestModuleFunctions(unittest.TestCase):
    """Test pure Python helper functions at module level."""

    def test_clamp(self):
        from xpra.opengl.backing import clamp
        assert clamp(0.0) == 0.0
        assert clamp(1.0) == 1.0
        assert clamp(0.5) == 0.5
        assert clamp(-1.0) == 0.0
        assert clamp(2.0) == 1.0

    def test_charclamp(self):
        from xpra.opengl.backing import charclamp
        assert charclamp(0) == 0
        assert charclamp(255) == 255
        assert charclamp(128) == 128
        assert charclamp(-1) == 0
        assert charclamp(256) == 255
        assert charclamp(127.9) == 128

    def test_get_tex_name_yuv(self):
        from xpra.opengl.backing import get_tex_name
        # YUV420P: plane 0=Y, 1=U, 2=V, single byte
        assert get_tex_name("YUV420P", 0) == "Y"
        assert get_tex_name("YUV420P", 1) == "U"
        assert get_tex_name("YUV420P", 2) == "V"

    def test_get_tex_name_16bit(self):
        from xpra.opengl.backing import get_tex_name
        # P16 formats: doubled plane names
        n = get_tex_name("YUV420P16", 0)
        assert n == "YY"

    def test_get_tex_name_default(self):
        from xpra.opengl.backing import get_tex_name
        # default args: YUV420P, index 0 → "Y"
        assert get_tex_name() == "Y"

    def test_get_tex_name_10bit(self):
        from xpra.opengl.backing import get_tex_name
        # P10 formats are also uploaded as 16-bit samples:
        assert get_tex_name("YUV420P10", 0) == "YY"
        assert get_tex_name("YUV422P10", 1) == "UU"


class TestPlanarFormats(unittest.TestCase):
    """Every planar format must have a full set of GL constants and a shader."""

    def test_upload_and_internal_formats(self):
        from xpra.opengl.backing import PLANAR_FORMATS, PIXEL_UPLOAD_FORMAT, PIXEL_INTERNAL_FORMAT
        from xpra.codecs.constants import get_subsampling_divs
        for fmt in PLANAR_FORMATS:
            nplanes = len(get_subsampling_divs(fmt))
            self.assertIn(fmt, PIXEL_UPLOAD_FORMAT, f"no upload format for {fmt!r}")
            assert len(PIXEL_UPLOAD_FORMAT[fmt]) >= nplanes, f"not enough upload formats for {fmt!r}"
            # the internal format is optional, but must cover every plane if present:
            iformats = PIXEL_INTERNAL_FORMAT.get(fmt, ())
            if iformats:
                assert len(iformats) >= nplanes, f"not enough internal formats for {fmt!r}"

    def test_shaders(self):
        from xpra.opengl.backing import PLANAR_FORMATS
        from xpra.opengl.shaders import SOURCE
        for fmt in PLANAR_FORMATS:
            # "YUV420P16" is uploaded as 16-bit and rendered with the 8-bit shader:
            name = fmt.replace("P16", "P")
            if name.startswith("GBRP"):
                # there is no GBRP shader (yet)
                continue
            for suffix in ("", "_FULL"):
                self.assertIn(f"{name}_to_RGB{suffix}", SOURCE, f"no shader for {fmt!r}")


class TestShaders(unittest.TestCase):

    def test_10bit_scaling(self):
        """The 10-bit shaders must scale the samples back up from the 16-bit texture range."""
        from xpra.opengl.shaders import SOURCE
        scale = str((2 ** 16 - 1) / (2 ** 10 - 1))
        for fmt in ("YUV420P10", "YUV422P10", "YUV444P10"):
            for suffix in ("", "_FULL"):
                source = SOURCE[f"{fmt}_to_RGB{suffix}"]
                assert source.count(scale) == 3, f"{fmt}{suffix} does not scale all 3 planes"

    def test_8bit_not_scaled(self):
        from xpra.opengl.shaders import SOURCE
        for fmt in ("YUV420P", "YUV422P", "YUV444P"):
            for suffix in ("", "_FULL"):
                assert "64.0" not in SOURCE[f"{fmt}_to_RGB{suffix}"]

    def test_invalid_bit_depth(self):
        from xpra.opengl.shaders import gen_YUV_to_RGB
        self.assertRaises(ValueError, gen_YUV_to_RGB, "YUV420P", bits=12)

    def test_invalid_colorspace(self):
        from xpra.opengl.shaders import gen_YUV_to_RGB
        self.assertRaises(ValueError, gen_YUV_to_RGB, "YUV420P", "bt2100")


def _make_mock_backing(wid=1, window_alpha=False, pixel_depth=0):
    """Create a GLWindowBackingBase subclass with all abstract methods mocked out."""
    from xpra.opengl.backing import GLWindowBackingBase

    class _MockBacking(GLWindowBackingBase):
        def init_gl_config(self):
            pass

        def init_backing(self):
            mock = MagicMock()
            mock.show = lambda: None
            self._backing = mock

        def is_double_buffered(self):
            return True

        def with_gl_context(self, cb, *args):
            pass

        def do_gl_show(self, rect_count):
            pass

        def gl_context(self):
            return None

    with patch("xpra.opengl.backing.is_X11", return_value=False):
        b = _MockBacking(wid, window_alpha, pixel_depth)
    return b


class TestGLContextLifecycle(unittest.TestCase):

    def test_missing_context_on_live_backing_remains_a_paint_error(self):
        from xpra.codecs.image import ImageWrapper
        from xpra.util.objects import typedict

        for kind in ("rgb", "scroll", "planar"):
            with self.subTest(kind=kind):
                backing = _make_mock_backing()
                results = []
                callbacks = [lambda success, message="": results.append((success, message))]
                image = None
                try:
                    self.assertFalse(backing._gl_context_callbacks_closed)
                    if kind == "rgb":
                        backing.do_paint_rgb(None, "rgb", "BGRX", bytes(4),
                                             0, 0, 1, 1, 1, 1, 4, typedict(), callbacks)
                    elif kind == "scroll":
                        backing.do_scroll_paints(None, ((0, 0, 1, 1, 0, 0),), 0, callbacks)
                    else:
                        image = ImageWrapper(0, 0, 2, 2, (bytes(4), bytes(2)),
                                             "NV12", 24, (2, 2), planes=ImageWrapper.PLANAR_2)
                        backing.paint_planar(None, "NV12_to_RGB", "h264", image,
                                             0, 0, 2, 2, 2, 2, typedict(), callbacks)
                    self.assertEqual(len(results), 1)
                    self.assertIs(results[0][0], False)
                    self.assertIn("context", results[0][1])
                    if image is not None:
                        self.assertTrue(image.freed)
                finally:
                    backing.close()

    def test_idle_callback_closes_once_after_widget_replacement(self):
        backing = _make_mock_backing()
        scheduled = []
        contexts = []

        def idle_add(callback, *args):
            scheduled.append((callback, args))
            return 1

        with patch("xpra.opengl.backing.GLib.idle_add", side_effect=idle_add):
            backing.with_gfx_context(lambda context: contexts.append(context))
        self.assertEqual(len(scheduled), 1)
        backing._backing = MagicMock()
        backing.close()
        self.assertEqual(contexts, [])
        callback, args = scheduled.pop()
        self.assertIsNone(callback(*args))
        self.assertEqual(contexts, [None])
        backing.close()

    def test_decoder_close_error_still_cleans_gl_and_drains_callbacks(self):
        backing = _make_mock_backing()
        contexts = []
        backing.defer_gl_context_callback(lambda context: contexts.append(context))

        def with_gl_context(callback, *args):
            callback(None, *args)

        backing.with_gl_context = with_gl_context
        with (
            patch.object(
                backing, "close_decoder", side_effect=(RuntimeError("decoder close failed"), False),
            ),
            patch.object(backing, "close_gl", wraps=backing.close_gl) as close_gl,
            self.assertRaisesRegex(RuntimeError, "decoder close failed"),
        ):
            backing.close()
        close_gl.assert_called_once_with(None)
        self.assertIsNone(backing._backing)
        self.assertEqual(contexts, [None])
        backing.close()
        self.assertEqual(contexts, [None])

    def test_pre_realize_callback_refuses_replaced_widget(self):
        backing = _make_mock_backing()
        context = object()
        contexts = []
        backing.defer_gl_context_callback(lambda value: contexts.append(value))
        backing._backing = MagicMock()
        backing.run_gl_context_callbacks(context)
        self.assertEqual(contexts, [None])
        backing.close()


class TestInitFormats(unittest.TestCase):
    """Test init_formats() logic at various bit depths."""

    def _make(self, window_alpha=False, pixel_depth=0):
        return _make_mock_backing(1, window_alpha, pixel_depth)

    def test_default_no_alpha(self):
        b = self._make(window_alpha=False, pixel_depth=0)
        assert b.internal_format == GL_RGB8

    def test_default_with_alpha(self):
        b = self._make(window_alpha=True, pixel_depth=0)
        assert b.internal_format == GL_RGBA8

    def test_bit_depth_24(self):
        b = self._make(window_alpha=False, pixel_depth=24)
        assert b.internal_format == GL_RGB8

    def test_bit_depth_32(self):
        b = self._make(window_alpha=True, pixel_depth=32)
        assert b.internal_format == GL_RGBA8

    def test_bit_depth_16_no_alpha(self):
        b = self._make(window_alpha=False, pixel_depth=16)
        assert b.internal_format == GL_RGB565
        assert "BGR565" in b.RGB_MODES
        assert "RGB565" in b.RGB_MODES

    def test_bit_depth_16_with_alpha(self):
        b = self._make(window_alpha=True, pixel_depth=16)
        assert b.internal_format in (GL_RGBA4, GL_RGB5_A1)

    def test_bit_depth_30(self):
        b = self._make(window_alpha=False, pixel_depth=30)
        assert b.internal_format == GL_RGB10_A2
        assert "r210" in b.RGB_MODES

    def test_bit_depth_above_32(self):
        b = self._make(window_alpha=False, pixel_depth=48)
        assert b.internal_format == GL_RGBA16
        assert "r210" in b.RGB_MODES


class TestGetRgbFormatsAlphaFilter(unittest.TestCase):
    """Verify get_rgb_formats() strips alpha formats from non-alpha windows."""

    def _make(self, window_alpha):
        return _make_mock_backing(1, window_alpha, 0)

    def test_alpha_window_keeps_bgra(self):
        b = self._make(window_alpha=True)
        self.assertIn("BGRA", b.get_rgb_formats())

    def test_alpha_window_keeps_ayuv(self):
        b = self._make(window_alpha=True)
        self.assertIn("AYUV", b.get_rgb_formats())

    def test_non_alpha_window_strips_bgra(self):
        b = self._make(window_alpha=False)
        self.assertNotIn("BGRA", b.get_rgb_formats())

    def test_non_alpha_window_strips_ayuv(self):
        b = self._make(window_alpha=False)
        self.assertNotIn("AYUV", b.get_rgb_formats())


class TestGetInfo(unittest.TestCase):

    def test_get_info_keys(self):
        b = _make_mock_backing()
        info = b.get_info()
        assert info.get("type") == "OpenGL"
        assert "bit-depth" in info
        assert "internal-format" in info

    def test_get_info_no_error(self):
        b = _make_mock_backing()
        b.last_present_fbo_error = ""
        info = b.get_info()
        assert "last-error" not in info

    def test_get_info_with_error(self):
        b = _make_mock_backing()
        b.last_present_fbo_error = "test error"
        info = b.get_info()
        assert info.get("last-error") == "test error"


class TestGetInitMagfilter(unittest.TestCase):

    def test_integer_scale_returns_nearest(self):
        b = _make_mock_backing()
        b.render_size = (800, 600)
        b.size = (800, 600)
        assert b.get_init_magfilter() == GL_NEAREST

    def test_double_scale_returns_nearest(self):
        b = _make_mock_backing()
        b.render_size = (1600, 1200)
        b.size = (800, 600)
        assert b.get_init_magfilter() == GL_NEAREST

    def test_non_integer_scale_returns_linear(self):
        b = _make_mock_backing()
        b.render_size = (1000, 750)
        b.size = (800, 600)
        assert b.get_init_magfilter() == GL_LINEAR


class TestGetBitDepth(unittest.TestCase):

    def test_zero_returns_24(self):
        b = _make_mock_backing(pixel_depth=0)
        assert b.get_bit_depth(0) == 24

    def test_passthrough(self):
        b = _make_mock_backing(pixel_depth=30)
        assert b.get_bit_depth(30) == 30

    def test_stored_bit_depth(self):
        b = _make_mock_backing(pixel_depth=24)
        assert b.bit_depth == 24


class TestRepr(unittest.TestCase):

    def test_repr_contains_wid(self):
        b = _make_mock_backing(wid=0x1234)
        s = repr(b)
        assert "GLWindowBacking" in s
        assert "0x1234" in s


class TestGetEncodingProperties(unittest.TestCase):

    def test_bit_depth_in_props(self):
        b = _make_mock_backing(pixel_depth=24)
        props = b.get_encoding_properties()
        assert props.get("encoding.bit-depth") == 24


class TestSwapFbos(unittest.TestCase):
    """swap_fbos() is pure Python reference-swapping — no GL context needed."""

    def _make_with_fbos(self):
        from xpra.opengl.backing import N_TEXTURES, TEX_FBO, TEX_TMP_FBO
        b = _make_mock_backing()
        b.offscreen_fbo = 1
        b.tmp_fbo = 2
        b.textures = list(range(N_TEXTURES))
        b.textures[TEX_FBO] = 10
        b.textures[TEX_TMP_FBO] = 20
        return b, TEX_FBO, TEX_TMP_FBO

    def test_swaps_fbo_handles(self):
        b, TEX_FBO, TEX_TMP_FBO = self._make_with_fbos()
        b.swap_fbos()
        assert b.offscreen_fbo == 2
        assert b.tmp_fbo == 1

    def test_swaps_texture_indices(self):
        b, TEX_FBO, TEX_TMP_FBO = self._make_with_fbos()
        b.swap_fbos()
        assert b.textures[TEX_FBO] == 20
        assert b.textures[TEX_TMP_FBO] == 10

    def test_double_swap_is_identity(self):
        b, TEX_FBO, TEX_TMP_FBO = self._make_with_fbos()
        b.swap_fbos()
        b.swap_fbos()
        assert b.offscreen_fbo == 1
        assert b.tmp_fbo == 2
        assert b.textures[TEX_FBO] == 10
        assert b.textures[TEX_TMP_FBO] == 20


class TestSubsurfaceTransactionFbos(unittest.TestCase):

    @staticmethod
    def make_backing():
        from xpra.opengl.backing import N_TEXTURES, TEX_FBO, TEX_TMP_FBO
        backing = _make_mock_backing()
        backing.size = backing.render_size = (4, 2)
        backing.offscreen_fbo = 1
        backing.tmp_fbo = 2
        backing.textures = list(range(N_TEXTURES))
        backing.textures[TEX_FBO] = 10
        backing.textures[TEX_TMP_FBO] = 20
        return backing

    def test_texture_container_truth_is_never_used(self):
        class TextureNames(list):
            def __bool__(self):
                raise ValueError("texture names have no scalar truth value")

        context = object()
        for names in ((), (11, 12, 13)):
            with self.subTest(names=names):
                backing = self.make_backing()
                textures = TextureNames(names)
                backing.textures = textures
                backing.vao, backing.spinner_vao = 21, 22
                with (
                    patch.object(backing, "draw_to_offscreen") as restore,
                    patch("xpra.opengl.backing.glBindVertexArray"),
                    patch("xpra.opengl.backing.glUseProgram"),
                    patch("xpra.opengl.backing.glDeleteVertexArrays") as delete_vaos,
                    patch("xpra.opengl.backing.glDeleteFramebuffers") as delete_fbos,
                    patch("xpra.opengl.backing.glDeleteTextures") as delete_textures,
                ):
                    backing.invalidate_subsurface_transaction(context)
                    backing.subsurface_backing_reconfigured(context)
                    self.assertEqual(restore.call_count, 2 if names else 0)
                    backing.close_gl(context)
                    backing.close_gl(context)
                    delete_vaos.assert_called_once_with(2, [21, 22])
                    delete_fbos.assert_called_once_with(2, (1, 2))
                    if names:
                        delete_textures.assert_called_once()
                        self.assertIs(delete_textures.call_args.args[0], textures)
                    else:
                        delete_textures.assert_not_called()
                self.assertEqual(backing.textures, [])
                self.assertIsNone(backing.vao)
                self.assertIsNone(backing.spinner_vao)

    def test_private_fbo_is_published_only_after_final_stage(self):
        from OpenGL.GL import GL_SCISSOR_TEST

        backing = self.make_backing()
        context = object()
        first = backing.get_subsurface_composite_stage(
            "rgb", "BGRA", _composite_options(1, 0, 2, reset=(1, 0, 2, 2)),
        )
        final = backing.get_subsurface_composite_stage(
            "rgb", "BGRA", _composite_options(1, 1, 2, reset=None),
        )
        with patch("OpenGL.GL.glGetFloatv", return_value=(0.1, 0.2, 0.3, 0.4)), \
                patch("OpenGL.GL.glIsEnabled", return_value=True), \
                patch("OpenGL.GL.glEnable") as enable, \
                patch("xpra.opengl.backing.glDisable") as disable, \
                patch("xpra.opengl.backing.glClearColor") as clear_color, \
                patch.object(backing, "copy_fbo") as copy_fbo, \
                patch.object(backing, "draw_to_tmp") as draw_to_tmp:
            backing.prepare_subsurface_composite_stage(first, context)
            copy_fbo.assert_called_once_with(4, 2)
            draw_to_tmp.assert_called_once()
            disable.assert_called_once_with(GL_SCISSOR_TEST)
            enable.assert_called_once_with(GL_SCISSOR_TEST)
            clear_color.assert_called_once_with(0.1, 0.2, 0.3, 0.4)
        self.assertIsNone(backing.complete_subsurface_composite_stage(
            first, context, (1, 0, 2, 2),
        ))
        self.assertEqual((backing.offscreen_fbo, backing.tmp_fbo), (1, 2))
        committed_region = backing.complete_subsurface_composite_stage(
            final, context, (3, 1, 1, 1),
        )
        self.assertEqual(committed_region, (1, 0, 3, 2))
        self.assertEqual((backing.offscreen_fbo, backing.tmp_fbo), (2, 1))
        backing.draw_needs_refresh = False
        backing.paint_screen = False
        backing.painted(context, *committed_region, 0)
        self.assertEqual(backing.pending_fbo_paint, [(1, 0, 3, 2)])

    def test_failure_and_reconfiguration_restore_visible_target(self):
        backing = self.make_backing()
        context = object()
        stage = backing.get_subsurface_composite_stage(
            "rgb", "BGRA", _composite_options(1, 0, 2, reset=(0, 0, 4, 2)),
        )
        with patch("OpenGL.GL.glGetFloatv", return_value=(0, 0, 0, 0)), \
                patch("OpenGL.GL.glIsEnabled", return_value=False), \
                patch("xpra.opengl.backing.glDisable"), \
                patch("xpra.opengl.backing.glClearColor"), \
                patch.object(backing, "copy_fbo"), patch.object(backing, "draw_to_tmp"):
            backing.prepare_subsurface_composite_stage(stage, context)
        with patch.object(backing, "draw_to_offscreen") as draw_to_offscreen:
            backing.fail_subsurface_composite_stage(stage, context)
            draw_to_offscreen.assert_called_once()
        self.assertIsNone(backing._subsurface_transaction)
        self.assertEqual((backing.offscreen_fbo, backing.tmp_fbo), (1, 2))

        newer = backing.get_subsurface_composite_stage(
            "rgb", "BGRA", _composite_options(2, 0, 2, reset=(0, 0, 4, 2)),
        )
        with patch("OpenGL.GL.glGetFloatv", return_value=(0, 0, 0, 0)), \
                patch("OpenGL.GL.glIsEnabled", return_value=False), \
                patch("xpra.opengl.backing.glDisable"), \
                patch("xpra.opengl.backing.glClearColor"), \
                patch.object(backing, "copy_fbo"), patch.object(backing, "draw_to_tmp"):
            backing.prepare_subsurface_composite_stage(newer, context)
        with patch.object(backing, "draw_to_offscreen") as draw_to_offscreen:
            backing.subsurface_backing_reconfigured(context)
            draw_to_offscreen.assert_called_once()
        self.assertIsNone(backing._subsurface_transaction)

        resized = backing.get_subsurface_composite_stage(
            "rgb", "BGRA", _composite_options(3, 0, 2, reset=(0, 0, 4, 2)),
        )
        with patch("OpenGL.GL.glGetFloatv", return_value=(0, 0, 0, 0)), \
                patch("OpenGL.GL.glIsEnabled", return_value=False), \
                patch("xpra.opengl.backing.glDisable"), \
                patch("xpra.opengl.backing.glClearColor"), \
                patch.object(backing, "copy_fbo"), patch.object(backing, "draw_to_tmp"):
            backing.prepare_subsurface_composite_stage(resized, context)
        backing.init(8, 4, 8, 4)
        self.assertIsNone(backing._subsurface_transaction)

        closing = backing.get_subsurface_composite_stage(
            "rgb", "BGRA", _composite_options(4, 0, 2, reset=(0, 0, 8, 4)),
        )
        with patch("OpenGL.GL.glGetFloatv", return_value=(0, 0, 0, 0)), \
                patch("OpenGL.GL.glIsEnabled", return_value=False), \
                patch("xpra.opengl.backing.glDisable"), \
                patch("xpra.opengl.backing.glClearColor"), \
                patch.object(backing, "copy_fbo"), patch.object(backing, "draw_to_tmp"):
            backing.prepare_subsurface_composite_stage(closing, context)
        backing.close()
        self.assertIsNone(backing._subsurface_transaction)


class TestFailShader(unittest.TestCase):
    """fail_shader() raises RuntimeError; glDeleteShader is skipped if shader not registered."""

    def test_raises_runtime_error(self):
        b = _make_mock_backing()
        with self.assertRaises(RuntimeError) as cm:
            b.fail_shader("myshader", "compile failed")
        assert "myshader" in str(cm.exception)

    def test_bytes_error_decoded(self):
        b = _make_mock_backing()
        with self.assertRaises(RuntimeError) as cm:
            b.fail_shader("myshader", b"undefined variable")
        assert "undefined variable" in str(cm.exception)

    def test_error_text_in_exception(self):
        b = _make_mock_backing()
        with self.assertRaises(RuntimeError) as cm:
            b.fail_shader("s", "bad syntax on line 7")
        assert "bad syntax on line 7" in str(cm.exception)

    def test_strips_trailing_newlines(self):
        b = _make_mock_backing()
        with self.assertRaises(RuntimeError) as cm:
            b.fail_shader("s", "oops\n\r")
        # message should not end with literal \n\r
        assert str(cm.exception).rstrip()

    def test_no_gl_call_when_not_registered(self):
        """With no shader in self.shaders, glDeleteShader is never called (no GL context required)."""
        b = _make_mock_backing()
        b.shaders.clear()
        with self.assertRaises(RuntimeError):
            b.fail_shader("unregistered", "error")


class TestPresentFboLogic(unittest.TestCase):
    """present_fbo() context guard and pending_fbo_paint accumulation."""

    def test_no_context_raises(self):
        b = _make_mock_backing()
        with self.assertRaises(RuntimeError):
            b.present_fbo(None, 0, 0, 100, 100)

    def test_accumulates_pending_paint(self):
        b = _make_mock_backing()
        b.paint_screen = False     # prevent managed_present_fbo GL calls
        ctx = MagicMock()
        b.present_fbo(ctx, 10, 20, 100, 200)
        assert (10, 20, 100, 200) in b.pending_fbo_paint

    def test_multiple_rects_accumulated(self):
        b = _make_mock_backing()
        b.paint_screen = False
        ctx = MagicMock()
        b.present_fbo(ctx, 0, 0, 50, 50)
        b.present_fbo(ctx, 50, 0, 50, 50)
        assert len(b.pending_fbo_paint) == 2

    def test_single_buffer_present_uses_rectangle_extents(self):
        from OpenGL.GL import GL_COLOR_BUFFER_BIT
        from xpra.opengl.backing import N_TEXTURES

        b = _make_mock_backing()
        b.size = b.render_size = (100, 80)
        b.offscreen_fbo = 1
        b.textures = list(range(N_TEXTURES))
        b.pending_fbo_paint = [(10, 20, 30, 40)]
        ctx = MagicMock()
        ctx.get_scale_factor.return_value = 1
        with (
            patch.object(b, "is_double_buffered", return_value=False),
            patch.object(b, "is_show_fps", return_value=False),
            patch.object(b, "gl_show") as gl_show,
            patch("xpra.opengl.backing.glViewport"),
            patch("xpra.opengl.backing.glBindFramebuffer"),
            patch("xpra.opengl.backing.glFramebufferTexture2D"),
            patch("xpra.opengl.backing.glReadBuffer"),
            patch("xpra.opengl.backing.glBlitFramebuffer") as blit,
            patch("xpra.opengl.backing.glFlush"),
            patch("xpra.opengl.backing.gl_frame_terminator"),
        ):
            b.do_present_fbo(ctx)

        self.assertEqual(blit.call_args.args[:8], (10, 20, 40, 60, 10, 20, 40, 60))
        self.assertEqual(blit.call_args.args[8], GL_COLOR_BUFFER_BIT)
        gl_show.assert_called_once_with(1)
        self.assertEqual(b.pending_fbo_paint, [])

    def test_flush_nonzero_does_not_call_managed(self):
        """With flush>0 and PAINT_FLUSH enabled, managed_present_fbo is deferred."""
        from unittest.mock import patch
        b = _make_mock_backing()
        b.paint_screen = True
        ctx = MagicMock()
        with patch("xpra.opengl.backing.PAINT_FLUSH", True):
            with patch.object(b, "managed_present_fbo") as mock_mgr:
                b.present_fbo(ctx, 0, 0, 100, 100, flush=1)
                mock_mgr.assert_not_called()

    def test_flush_zero_calls_managed(self):
        """With flush=0 and paint_screen=True, managed_present_fbo is called."""
        b = _make_mock_backing()
        b.paint_screen = True
        ctx = MagicMock()
        with patch.object(b, "managed_present_fbo") as mock_mgr:
            b.present_fbo(ctx, 0, 0, 100, 100, flush=0)
            mock_mgr.assert_called_once_with(ctx)


class TestDrawPointerLogic(unittest.TestCase):
    """draw_pointer() timeout and early-return guards (no GL context needed)."""

    def test_expired_timeout_clears_overlay(self):
        from time import monotonic
        from xpra.opengl.backing import CURSOR_IDLE_TIMEOUT
        b = _make_mock_backing()
        start_time = monotonic() - CURSOR_IDLE_TIMEOUT - 1
        b.pointer_overlay = (100, 200, 0, 0, 0, start_time)
        b.draw_pointer()
        assert b.pointer_overlay == ()

    def test_no_cursor_data_returns_without_crash(self):
        from time import monotonic
        b = _make_mock_backing()
        b.pointer_overlay = (100, 200, 0, 0, 0, monotonic())
        b.cursor_data = ()     # no cursor — must not raise
        b.draw_pointer()
        assert b.pointer_overlay != ()    # not expired, so not cleared


class TestOverlayTextureLogic(unittest.TestCase):

    def test_blend_state_is_restored(self):
        from OpenGL.GL import (
            GL_FUNC_ADD, GL_FUNC_SUBTRACT, GL_FUNC_REVERSE_SUBTRACT,
            GL_ONE, GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA,
            GL_DST_ALPHA, GL_ONE_MINUS_DST_ALPHA, GL_DST_COLOR, GL_ONE_MINUS_DST_COLOR,
        )
        b = _make_mock_backing()
        old_equation = GL_FUNC_SUBTRACT, GL_FUNC_REVERSE_SUBTRACT
        old_function = GL_DST_ALPHA, GL_ONE_MINUS_DST_ALPHA, GL_DST_COLOR, GL_ONE_MINUS_DST_COLOR
        old_state = old_equation + old_function
        with (
            patch("xpra.opengl.backing.glGetIntegerv", side_effect=old_state),
            patch("OpenGL.GL.glIsEnabled", return_value=False),
            patch("OpenGL.GL.glEnable") as enable,
            patch("xpra.opengl.backing.glDisable") as disable,
            patch("OpenGL.GL.glBlendEquationSeparate") as blend_equation,
            patch("OpenGL.GL.glBlendFuncSeparate") as blend_function,
            patch.object(b, "combine_texture") as combine_texture,
        ):
            b.overlay_texture(1, 2, 3, 4, 5)

        enable.assert_called_once()
        disable.assert_called_once()
        assert blend_equation.call_args_list[0].args == (GL_FUNC_ADD, GL_FUNC_ADD)
        assert blend_equation.call_args_list[-1].args == old_equation
        assert blend_function.call_args_list[0].args == (
            GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA, GL_ONE, GL_ONE_MINUS_SRC_ALPHA,
        )
        assert blend_function.call_args_list[-1].args == old_function
        combine_texture.assert_called_once_with("overlay", 2, 3, 4, 5, {"rgba": 1}, {})

    def test_premultiplied_blend_state_is_restored(self):
        from OpenGL.GL import (
            GL_FUNC_ADD, GL_FUNC_SUBTRACT, GL_FUNC_REVERSE_SUBTRACT,
            GL_ONE, GL_ONE_MINUS_SRC_ALPHA,
            GL_BLEND, GL_SCISSOR_TEST,
            GL_DST_ALPHA, GL_ONE_MINUS_DST_ALPHA, GL_DST_COLOR, GL_ONE_MINUS_DST_COLOR,
        )
        b = _make_mock_backing()
        b.size = (100, 100)
        b.render_size = (100, 100)
        old_equation = GL_FUNC_SUBTRACT, GL_FUNC_REVERSE_SUBTRACT
        old_function = GL_DST_ALPHA, GL_ONE_MINUS_DST_ALPHA, GL_DST_COLOR, GL_ONE_MINUS_DST_COLOR
        old_state = old_equation + old_function
        with (
            patch("xpra.opengl.backing.glGetIntegerv", side_effect=old_state),
            patch("OpenGL.GL.glIsEnabled", return_value=False),
            patch("OpenGL.GL.glEnable") as enable,
            patch("xpra.opengl.backing.glDisable") as disable,
            patch("OpenGL.GL.glBlendEquationSeparate") as blend_equation,
            patch("OpenGL.GL.glBlendFuncSeparate") as blend_function,
            patch.object(b, "combine_texture") as combine_texture,
        ):
            b.composite_premultiplied_texture(1, 2, 3, 4, 5, 8, 10)

        enable.assert_called_once()
        self.assertEqual(disable.call_args_list[0].args, (GL_SCISSOR_TEST,))
        self.assertEqual(disable.call_args_list[-1].args, (GL_BLEND,))
        self.assertEqual(blend_equation.call_args_list[0].args, (GL_FUNC_ADD, GL_FUNC_ADD))
        self.assertEqual(blend_equation.call_args_list[-1].args, old_equation)
        self.assertEqual(
            blend_function.call_args_list[0].args,
            (GL_ONE, GL_ONE_MINUS_SRC_ALPHA, GL_ONE, GL_ONE_MINUS_SRC_ALPHA),
        )
        self.assertEqual(blend_function.call_args_list[-1].args, old_function)
        combine_texture.assert_called_once_with(
            "premultiplied-overlay", 2, 3, 8, 10, {"rgba": 1}, {"scaling": (2.0, 2.0)},
            viewport_height=100,
        )

    def test_premultiplied_blend_preserves_enabled_scissor_and_blend(self):
        from OpenGL.GL import GL_BLEND, GL_SCISSOR_TEST

        b = _make_mock_backing()
        b.size = b.render_size = (100, 100)
        with (
            patch("xpra.opengl.backing.glGetIntegerv", side_effect=(0x8006, 0x8006, 1, 0, 1, 0)),
            patch("OpenGL.GL.glIsEnabled", side_effect=(True, True)),
            patch("OpenGL.GL.glEnable") as enable,
            patch("xpra.opengl.backing.glDisable") as disable,
            patch("OpenGL.GL.glBlendEquationSeparate"),
            patch("OpenGL.GL.glBlendFuncSeparate"),
            patch.object(b, "combine_texture"),
        ):
            b.composite_premultiplied_texture(1, 2, 3, 4, 5, 8, 10)

        disable.assert_called_once_with(GL_SCISSOR_TEST)
        self.assertEqual(enable.call_args_list[0].args, (GL_BLEND,))
        self.assertEqual(enable.call_args_list[-1].args, (GL_SCISSOR_TEST,))

    def test_combine_texture_cleans_transient_gl_objects_on_draw_failure(self):
        from OpenGL.GL import GL_TEXTURE0, GL_TEXTURE_RECTANGLE

        b = _make_mock_backing()
        b.programs["premultiplied-overlay"] = 17
        with (
            patch("xpra.opengl.backing.glGetIntegerv", return_value=(1, 2, 3, 4)),
            patch("xpra.opengl.backing.glViewport"),
            patch("xpra.opengl.backing.glUseProgram") as use_program,
            patch("xpra.opengl.backing.glActiveTexture") as active_texture,
            patch("xpra.opengl.backing.glBindTexture") as bind_texture,
            patch("xpra.opengl.backing.glGetUniformLocation", return_value=1),
            patch("xpra.opengl.backing.glUniform1i"),
            patch("xpra.opengl.backing.glUniform2f"),
            patch.object(b, "set_vao", return_value=23),
            patch("xpra.opengl.backing.glDrawArrays", side_effect=RuntimeError("draw failed")),
            patch("xpra.opengl.backing.glBindVertexArray") as bind_vao,
            patch("xpra.opengl.backing.glDeleteBuffers") as delete_buffers,
        ):
            with self.assertRaisesRegex(RuntimeError, "draw failed"):
                b.combine_texture(
                    "premultiplied-overlay", 2, 3, 8, 10,
                    {"rgba": 9}, {"scaling": (2.0, 2.0)}, viewport_height=100,
                )

        self.assertEqual(use_program.call_args_list[-1].args, (0,))
        self.assertEqual(bind_vao.call_args_list[-1].args, (0,))
        delete_buffers.assert_called_once_with(1, [23])
        self.assertEqual(bind_texture.call_args_list[-1].args, (GL_TEXTURE_RECTANGLE, 0))
        self.assertEqual(active_texture.call_args_list[-1].args, (GL_TEXTURE0,))

    def test_upload_and_stale_stage_failures_unbind_upload_texture(self):
        from OpenGL.GL import GL_TEXTURE_RECTANGLE

        b = _make_mock_backing()
        b.size = b.render_size = (1, 1)
        b.textures = list(range(16))
        results = []
        with (
            patch.object(b, "gl_init"),
            patch("xpra.opengl.backing.pixels_for_upload", return_value=("copy", bytes(4))),
            patch("xpra.opengl.backing.set_alignment"),
            patch("xpra.opengl.backing.glBindTexture") as bind_texture,
            patch("xpra.opengl.backing.glTexParameteri"),
            patch("xpra.opengl.backing.glTexImage2D"),
            patch.object(
                b, "prepare_subsurface_composite_stage",
                side_effect=ValueError("stale injected stage"),
            ),
            patch.object(b, "fail_subsurface_composite_stage"),
        ):
            b.do_paint_rgb(
                object(), "rgb", "BGRA", bytes(4),
                0, 0, 1, 1, 1, 1, 4, _composite_options(),
                [lambda success, message="": results.append((success, message))],
            )

        self.assertFalse(results[0][0])
        self.assertIn("stale injected stage", results[0][1])
        self.assertEqual(bind_texture.call_args_list[-1].args, (GL_TEXTURE_RECTANGLE, 0))

        results.clear()
        bind_texture.reset_mock()
        with (
            patch.object(b, "gl_init"),
            patch("xpra.opengl.backing.pixels_for_upload", return_value=("copy", bytes(4))),
            patch("xpra.opengl.backing.set_alignment", side_effect=ValueError("invalid upload alignment")),
            patch("xpra.opengl.backing.glBindTexture") as bind_texture,
            patch.object(b, "fail_subsurface_composite_stage"),
        ):
            b.do_paint_rgb(
                object(), "rgb", "BGRA", bytes(4),
                0, 0, 1, 1, 1, 1, 4, _composite_options(2),
                [lambda success, message="": results.append((success, message))],
            )

        self.assertFalse(results[0][0])
        self.assertIn("invalid upload alignment", results[0][1])
        self.assertEqual(bind_texture.call_args_list[-1].args, (GL_TEXTURE_RECTANGLE, 0))

    def test_vertex_upload_failure_releases_transient_buffer(self):
        from OpenGL.GL import GL_ARRAY_BUFFER

        b = _make_mock_backing()
        with (
            patch("xpra.opengl.backing.glBindVertexArray"),
            patch("xpra.opengl.backing.glGenBuffers", return_value=23),
            patch("xpra.opengl.backing.glBindBuffer") as bind_buffer,
            patch("xpra.opengl.backing.glBufferData", side_effect=RuntimeError("upload failed")),
            patch("xpra.opengl.backing.glDeleteBuffers") as delete_buffers,
        ):
            with self.assertRaisesRegex(RuntimeError, "upload failed"):
                b.set_vao()

        delete_buffers.assert_called_once_with(1, [23])
        self.assertEqual(bind_buffer.call_args_list[-1].args, (GL_ARRAY_BUFFER, 0))

    def test_subsurface_clear_restores_scissor_and_clear_state(self):
        from OpenGL.GL import GL_SCISSOR_TEST
        b = _make_mock_backing()
        b.size = (100, 80)
        old_box = (7, 8, 9, 10)
        old_color = (0.1, 0.2, 0.3, 0.4)
        with (
            patch("OpenGL.GL.glIsEnabled", return_value=False),
            patch("OpenGL.GL.glEnable") as enable,
            patch("OpenGL.GL.glScissor") as scissor,
            patch("OpenGL.GL.glGetFloatv", return_value=old_color),
            patch("xpra.opengl.backing.glGetIntegerv", return_value=old_box),
            patch("xpra.opengl.backing.glClearColor") as clear_color,
            patch("xpra.opengl.backing.glClear") as clear,
            patch("xpra.opengl.backing.glDisable") as disable,
        ):
            b.clear_subsurface_region((11, 13, 17, 19))

        enable.assert_called_once_with(GL_SCISSOR_TEST)
        self.assertEqual(scissor.call_args_list[0].args, (11, 48, 17, 19))
        self.assertEqual(scissor.call_args_list[-1].args, old_box)
        self.assertEqual(clear_color.call_args_list[0].args, (0, 0, 0, 0))
        self.assertEqual(clear_color.call_args_list[-1].args, old_color)
        clear.assert_called_once()
        disable.assert_called_once_with(GL_SCISSOR_TEST)

    def test_planar_subsurface_composition_is_rejected(self):
        from xpra.util.objects import typedict
        b = _make_mock_backing()
        for mode in (
                SUBSURFACE_COMPOSITE_MODE, "unknown-mode", "",
                SUBSURFACE_COMPOSITE_MODE.encode(), False, None,
        ):
            with self.subTest(mode=mode):
                image = MagicMock()
                image.get_pixel_format.return_value = "YUV420P"
                results = []
                b.paint_planar(
                    None, "YUV420P_to_RGB", "h264", image,
                    0, 0, 16, 16, 16, 16,
                    typedict({"subsurface-composite": mode}),
                    [lambda success, message="": results.append((success, message))],
                )
                image.free.assert_called_once()
                self.assertFalse(results[0][0])


class TestPaintBoxEarlyReturn(unittest.TestCase):
    """paint_box() with line_width=0 returns immediately with no GL calls."""

    def test_zero_line_width_is_noop(self):
        b = _make_mock_backing()
        b.paint_box_line_width = 0
        # would raise if it reached any GL code without a context
        b.paint_box("rgb24", 0, 0, 100, 100)

    def test_negative_line_width_is_noop(self):
        b = _make_mock_backing()
        b.paint_box_line_width = -1
        b.paint_box("h264", 10, 20, 80, 60)


class TestScrollPaints(unittest.TestCase):

    def test_negative_origin_is_clipped(self):
        from xpra.opengl.backing import N_TEXTURES
        b = _make_mock_backing()
        b.size = 100, 80
        b.offscreen_fbo = 10
        b.tmp_fbo = 11
        b.textures = list(range(N_TEXTURES))
        with (
            patch.object(b, "copy_fbo"),
            patch.object(b, "paint_box"),
            patch.object(b, "painted"),
            patch("xpra.opengl.backing.glBindFramebuffer"),
            patch("xpra.opengl.backing.glFramebufferTexture2D"),
            patch("xpra.opengl.backing.glReadBuffer"),
            patch("xpra.opengl.backing.glBindTexture"),
            patch("xpra.opengl.backing.glFlush"),
            patch("xpra.opengl.backing.glBlitFramebuffer") as blit,
        ):
            b.do_scroll_paints(object(), [(-10, -5, 30, 20, 5, 3)], 0, [])
        blit.assert_called_once_with(
            0, 80, 20, 65,
            5, 77, 25, 62,
            unittest.mock.ANY, unittest.mock.ANY,
        )

    def test_scroll_snapshot_is_restored_for_each_rectangle(self):
        from OpenGL.GL import GL_COLOR_ATTACHMENT0, GL_READ_FRAMEBUFFER, GL_TEXTURE_RECTANGLE
        from xpra.opengl.backing import N_TEXTURES, TEX_TMP_FBO
        b = _make_mock_backing()
        b.size = 100, 80
        b.offscreen_fbo = 10
        b.tmp_fbo = 11
        b.textures = list(range(N_TEXTURES))
        events = []

        def attached(framebuffer, attachment, target, texture, level):
            if framebuffer == GL_READ_FRAMEBUFFER and texture == b.textures[TEX_TMP_FBO]:
                assert attachment == GL_COLOR_ATTACHMENT0
                assert target == GL_TEXTURE_RECTANGLE
                assert level == 0
                events.append("snapshot")

        with (
            patch.object(b, "copy_fbo"),
            patch.object(b, "paint_box", side_effect=lambda *args: events.append("paint")),
            patch.object(b, "painted"),
            patch("xpra.opengl.backing.glBindFramebuffer"),
            patch("xpra.opengl.backing.glFramebufferTexture2D", side_effect=attached),
            patch("xpra.opengl.backing.glReadBuffer"),
            patch("xpra.opengl.backing.glBindTexture"),
            patch("xpra.opengl.backing.glFlush"),
            patch("xpra.opengl.backing.glBlitFramebuffer", side_effect=lambda *args: events.append("blit")),
        ):
            b.do_scroll_paints(object(), [(0, 0, 20, 20, 5, 0), (30, 20, 10, 10, 0, 5)], 0, [])

        assert events == ["snapshot", "blit", "paint", "snapshot", "blit", "paint"]


# ---------------------------------------------------------------------------
# GL context tests: require an actual OpenGL context.
# On Linux uses Xvfb + Mesa software rendering (LIBGL_ALWAYS_SOFTWARE=1).
# On macOS / Windows a display is assumed to already be present.
# ---------------------------------------------------------------------------

class TestNativeTextureArrays(unittest.TestCase):
    """Each PyOpenGL output handler owns a fresh Python/GTK/GL process."""

    def check_handler(self, handler):
        from subprocess import run
        import xpra
        env = os.environ.copy()
        env["PYOPENGL_USE_ACCELERATE"] = "0"
        env["XPRA_TEST_GL_ARRAY_HANDLER"] = handler
        bootstrap = """
import os
import runpy
import sys
import xpra

expected_xpra = sys.argv[2]
actual_xpra = os.path.realpath(xpra.__file__)
if actual_xpra != expected_xpra:
    raise AssertionError(f"child imported {actual_xpra}, expected installed Xpra {expected_xpra}")

handler = os.environ["XPRA_TEST_GL_ARRAY_HANDLER"]
if handler == "ctypesarrays":
    # Exercise real no-NumPy allocation, not converted or fabricated names.
    sys.modules["numpy"] = None
from OpenGL.arrays.arraydatatype import ArrayDatatype
ArrayDatatype.getRegistry().registerReturn(handler)
sys.argv = [sys.argv[1], "TestGLInit.check_native_texture_array_lifecycle", "-v"]
runpy.run_path(sys.argv[0], run_name="__main__")
"""
        result = run(
            [sys.executable, "-c", bootstrap, os.path.abspath(__file__), os.path.realpath(xpra.__file__)],
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            env=env, capture_output=True, text=True, timeout=90, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_numpy(self):
        self.check_handler("numpy")

    def test_ctypes(self):
        self.check_handler("ctypesarrays")


class TestGLInit(unittest.TestCase):

    xvfb = None
    env_context = None

    @classmethod
    def setUpClass(cls):
        import time
        if os.name == "posix" and sys.platform != "darwin":
            from xpra.util.env import OSEnvContext
            from unit.process_test_util import DisplayContext, ProcessTestUtil
            # the environment is restored in `tearDownClass`,
            # so that the tests running after this class
            # are not left with a `DISPLAY` pointing at the Xvfb we will have killed:
            cls.env_context = OSEnvContext(LIBGL_ALWAYS_SOFTWARE="1", GDK_BACKEND="x11")
            cls.env_context.__enter__()
            ProcessTestUtil.setUpClass()
            cls.ptu = ProcessTestUtil()
            cls.ptu.setUp()
            cls.xvfb = cls.ptu.start_Xvfb()
            os.environ["DISPLAY"] = cls.xvfb.display or ""
            try:
                from xpra.x11.bindings.wait_for_x_server import wait_for_x_server
                wait_for_x_server(cls.xvfb.display or "", 10)
            except ImportError:
                time.sleep(3)
            DisplayContext.open_display_source(cls.xvfb.display or "")

    @classmethod
    def tearDownClass(cls):
        try:
            if cls.xvfb:
                # GTK and PyGObject may defer destruction of GL widgets through
                # a reference cycle or the main context. Finish that work while
                # the process-global GDK display is still valid: closing it
                # first can make a later GtkStyleContext finalizer abort the
                # whole test process.
                import gc
                from xpra.os_util import gi_import
                from unit.process_test_util import DisplayContext, ProcessTestUtil

                Gtk = gi_import("Gtk")
                GLib = gi_import("GLib")
                try:
                    for window in Gtk.Window.list_toplevels():
                        window.destroy()
                    main_context = GLib.main_context_default()

                    def drain_main_context() -> None:
                        for _ in range(100):
                            if not main_context.pending():
                                break
                            main_context.iteration(False)

                    drain_main_context()
                    gc.collect()
                    drain_main_context()
                finally:
                    try:
                        # Close the display connection before killing the Xvfb,
                        # see `DisplayContext`.
                        DisplayContext.close_display_source()
                    finally:
                        try:
                            cls.xvfb.terminate()
                        finally:
                            cls.xvfb = None
                            try:
                                cls.ptu.tearDown()
                            finally:
                                ProcessTestUtil.tearDownClass()
        finally:
            if cls.env_context:
                cls.env_context.__exit__()
                cls.env_context = None

    def _make_gl_backing(self, window_alpha=False, pixel_depth=0):
        from xpra.os_util import gi_import
        from xpra.client.gtk3.opengl.drawing_area import GLDrawingArea
        Gtk = gi_import("Gtk")
        win = Gtk.Window()
        win.set_default_size(256, 256)
        backing = GLDrawingArea(1, window_alpha, pixel_depth)
        backing.size = (256, 256)
        backing.render_size = (256, 256)
        win.add(backing._backing)
        win.show_all()
        GLib = gi_import("GLib")
        ctx = GLib.main_context_default()
        for _ in range(20):
            ctx.iteration(False)
        return backing, win

    def test_gl_init_runs(self):
        """GLDrawingArea can initialize an OpenGL context."""
        backing, win = self._make_gl_backing()
        try:
            ctx = backing.gl_context()
            if ctx:
                with ctx:
                    backing.gl_init(ctx)
                assert backing.gl_setup
        finally:
            backing.close()
            win.destroy()

    def check_native_texture_array_lifecycle(self):
        """Actual generated texture arrays survive RGB, abort and exact cleanup."""
        from ctypes import Array
        from OpenGL.GL import (
            glBindFramebuffer, glBindTexture, glBindVertexArray,
            glDeleteTextures, glDeleteVertexArrays, glGenTextures, glGenVertexArrays,
            glGetIntegerv, glIsTexture, glIsVertexArray, glReadBuffer, glReadPixels,
            GL_READ_FRAMEBUFFER, GL_DRAW_FRAMEBUFFER_BINDING, GL_COLOR_ATTACHMENT0,
            GL_TEXTURE_RECTANGLE, GL_RGBA, GL_UNSIGNED_BYTE,
        )
        from xpra.opengl.backing import N_TEXTURES
        from xpra.util.objects import typedict

        generated = []

        def generate_textures(count):
            names = glGenTextures(count)
            generated.append((count, names))
            return names

        with patch("xpra.opengl.backing.glGenTextures", side_effect=generate_textures):
            backing, win = self._make_gl_backing(window_alpha=False, pixel_depth=24)
            backing.size = backing.render_size = (3, 1)
            backing.paint_screen = False
            try:
                ctx = backing.gl_context()
                self.assertIsNotNone(ctx, "no mapped OpenGL context for texture-array regression")
                with ctx:
                    backing.gl_init(ctx)
                self.assertEqual(len(generated), 1)
                self.assertEqual(generated[0][0], N_TEXTURES)
                self.assertIs(backing.textures, generated[0][1])
                handler = os.environ["XPRA_TEST_GL_ARRAY_HANDLER"]
                if handler == "numpy":
                    from numpy import ndarray
                    self.assertIsInstance(backing.textures, ndarray)
                else:
                    self.assertEqual(handler, "ctypesarrays")
                    self.assertIsNone(sys.modules.get("numpy"))
                    self.assertIsInstance(backing.textures, Array)
                self.assertEqual(len(backing.textures), N_TEXTURES)
                texture_names = tuple(int(name) for name in backing.textures)
                self.assertEqual(len(set(texture_names)), N_TEXTURES)

                def read_pixels():
                    glBindFramebuffer(GL_READ_FRAMEBUFFER, backing.offscreen_fbo)
                    glReadBuffer(GL_COLOR_ATTACHMENT0)
                    return bytes(glReadPixels(0, 0, 3, 1, GL_RGBA, GL_UNSIGNED_BYTE))

                def paint(pixels, options):
                    results = []
                    backing.do_paint_rgb(
                        ctx, "rgb", "RGBX", pixels,
                        0, 0, 3, 1, 3, 1, 12, options,
                        [lambda success, message="": results.append((success, message))],
                    )
                    self.assertEqual(len(results), 1)
                    self.assertIs(results[0][0], True, results)
                    self.assertEqual(results[0][1], "")

                pixels = bytes((30, 60, 90, 0, 120, 90, 60, 255, 90, 30, 120, 1))
                expected = bytes((30, 60, 90, 255, 120, 90, 60, 255, 90, 30, 120, 255))
                with ctx:
                    # Instantiate every generated name so a false glIsTexture
                    # after close proves deletion, not a never-created object.
                    for name in texture_names:
                        glBindTexture(GL_TEXTURE_RECTANGLE, name)
                    glBindTexture(GL_TEXTURE_RECTANGLE, 0)
                    self.assertTrue(all(glIsTexture(name) for name in texture_names))
                    paint(pixels, typedict())
                    self.assertEqual(read_pixels(), expected)
                    visible_fbo = backing.offscreen_fbo
                    for transaction_id, abort in (
                        (1, backing.invalidate_subsurface_transaction),
                        (2, backing.subsurface_backing_reconfigured),
                    ):
                        paint(bytes((200, 100, 50, 0)) * 3, _composite_options(
                            transaction_id, 0, 2, reset=(0, 0, 3, 1),
                        ))
                        self.assertIsNotNone(backing._subsurface_transaction)
                        self.assertEqual(read_pixels(), expected)
                        abort(ctx)
                        self.assertIsNone(backing._subsurface_transaction)
                        self.assertEqual(backing.offscreen_fbo, visible_fbo)
                        self.assertEqual(int(glGetIntegerv(GL_DRAW_FRAMEBUFFER_BINDING)), visible_fbo)
                        self.assertEqual(read_pixels(), expected)
                    paint(pixels, typedict())
                    self.assertEqual(read_pixels(), expected)

                    # Both independent VAOs must reach the native delete call;
                    # a count of one leaks the spinner even with valid arrays.
                    backing.spinner_vao = glGenVertexArrays(1)
                    glBindVertexArray(backing.spinner_vao)
                    glBindVertexArray(0)
                    vao_names = (int(backing.vao), int(backing.spinner_vao))
                    self.assertEqual(len(set(vao_names)), 2)
                    self.assertTrue(all(glIsVertexArray(name) for name in vao_names))

                deleted_textures = []
                deleted_vaos = []

                def delete_textures(names):
                    glDeleteTextures(names)
                    deleted_textures.append((
                        tuple(int(name) for name in names),
                        tuple(bool(glIsTexture(name)) for name in names),
                    ))

                def delete_vaos(count, names):
                    glDeleteVertexArrays(count, names)
                    deleted_vaos.append((
                        count, tuple(int(name) for name in names),
                        tuple(bool(glIsVertexArray(name)) for name in names),
                    ))

                with (
                    patch("xpra.opengl.backing.glDeleteTextures", side_effect=delete_textures),
                    patch("xpra.opengl.backing.glDeleteVertexArrays", side_effect=delete_vaos),
                ):
                    backing.close()
                    backing.close()
                self.assertEqual(deleted_textures, [(texture_names, (False,) * N_TEXTURES)])
                self.assertEqual(deleted_vaos, [(2, vao_names, (False, False))])
                self.assertEqual(backing.textures, [])
                self.assertIsNone(backing._backing)
                self.assertIsNone(backing.vao)
                self.assertIsNone(backing.spinner_vao)
            finally:
                try:
                    backing.close()
                finally:
                    win.destroy()

    def test_close_before_paint_idle_completes_callback(self):
        """Queued paints terminate as skips, not renderer failures, after close."""
        from xpra.codecs.image import ImageWrapper
        from xpra.os_util import gi_import
        from xpra.util.objects import typedict

        for kind in ("BGRX", "BGRA", "scroll", "planar"):
            with self.subTest(kind=kind):
                backing, win = self._make_gl_backing()
                results = []
                image = None
                callbacks = [lambda success, message="": results.append((success, message))]
                try:
                    if kind in ("BGRX", "BGRA"):
                        backing.paint_rgb(kind, bytes(4), 0, 0, 1, 1, 4, typedict(), callbacks)
                    elif kind == "scroll":
                        backing.paint_scroll(((0, 0, 1, 1, 0, 0),), typedict(), callbacks)
                    else:
                        image = ImageWrapper(0, 0, 2, 2, (bytes(4), bytes(2)),
                                             "NV12", 24, (2, 2), planes=ImageWrapper.PLANAR_2)
                        backing.paint_image_wrapper("h264", image, 0, 0, 2, 2, typedict(), callbacks)
                    self.assertEqual(results, [])
                    backing.close()
                    self.assertEqual(results, [])
                    main_context = gi_import("GLib").main_context_default()
                    for _ in range(20):
                        main_context.iteration(False)
                    self.assertEqual(len(results), 1)
                    self.assertIs(type(results[0][0]), int)
                    self.assertEqual(results[0][0], -1)
                    self.assertIn("backing is closed", results[0][1])
                    if image is not None:
                        self.assertTrue(image.freed)
                    backing.close()
                    self.assertEqual(len(results), 1)
                finally:
                    backing.close()
                    win.destroy()

    def test_rgb_paints_before_realize_and_after_close_are_skipped(self):
        from xpra.client.gtk3.opengl.drawing_area import GLDrawingArea
        from xpra.client.gtk3.opengl.glarea_backing import GLAreaBacking
        from xpra.os_util import gi_import
        from xpra.util.objects import typedict

        main_context = gi_import("GLib").main_context_default()
        for backing_class in (GLDrawingArea, GLAreaBacking):
            with self.subTest(backing_class=backing_class.__name__):
                backing = backing_class(1, False, 24)
                widget = backing._backing
                results = []

                def queue_paint():
                    backing.paint_rgb(
                        "BGRX", bytes(4), 0, 0, 1, 1, 4, typedict(),
                        [lambda success, message="": results.append((success, message))],
                    )
                    for _ in range(20):
                        main_context.iteration(False)

                try:
                    self.assertFalse(widget.get_mapped())
                    queue_paint()
                    self.assertEqual(results, [])
                    self.assertEqual(len(backing._pending_gl_context_callbacks), 1)
                    backing.close()
                    self.assertEqual(results, [(-1, "backing is closed")])
                    queue_paint()
                    self.assertEqual(results, [(-1, "backing is closed")] * 2)
                    self.assertEqual(backing._pending_gl_context_callbacks, [])
                    backing.close()
                    self.assertEqual(len(results), 2)
                finally:
                    backing.close()
                    widget.destroy()

    def test_pre_realize_callbacks_close_once_for_both_gtk_backings(self):
        """Both GTK GL widgets delegate unrealized work to the shared close owner."""
        from xpra.client.gtk3.opengl.drawing_area import GLDrawingArea
        from xpra.client.gtk3.opengl.glarea_backing import GLAreaBacking

        for backing_class in (GLDrawingArea, GLAreaBacking):
            with self.subTest(backing_class=backing_class.__name__):
                backing = backing_class(1, False, 24)
                widget = backing._backing
                calls = []
                try:
                    self.assertFalse(backing._backing.get_mapped())
                    backing.with_gl_context(
                        lambda context, marker, result=calls: result.append((context, marker)), "queued",
                    )
                    self.assertEqual(calls, [])
                    backing.close()
                    self.assertEqual(calls, [(None, "queued")])
                    backing.close()
                    self.assertEqual(calls, [(None, "queued")])
                    backing.with_gl_context(
                        lambda context, marker, result=calls: result.append((context, marker)), "closed",
                    )
                    self.assertEqual(calls, [(None, "queued"), (None, "closed")])
                finally:
                    backing.close()
                    widget.destroy()

    def test_init_textures(self):
        """init_textures() creates textures and fbos."""
        backing, win = self._make_gl_backing()
        try:
            ctx = backing.gl_context()
            if ctx:
                with ctx:
                    backing.init_textures()
                assert len(backing.textures) > 0
                assert backing.offscreen_fbo is not None
        finally:
            backing.close()
            win.destroy()

    def test_init_fbo(self):
        """init_fbo() initializes the framebuffer and clears it."""
        from xpra.opengl.backing import TEX_FBO
        backing, win = self._make_gl_backing()
        try:
            ctx = backing.gl_context()
            if ctx:
                with ctx:
                    backing.init_textures()
                    backing.init_fbo(TEX_FBO, backing.offscreen_fbo, 64, 64, GL_NEAREST)
        finally:
            backing.close()
            win.destroy()

    def test_gl_init_size_too_large(self):
        """gl_init() raises ValueError when texture size exceeds the GL maximum."""
        backing, win = self._make_gl_backing()
        try:
            ctx = backing.gl_context()
            if ctx:
                with ctx:
                    backing.size = (200000, 200000)
                    self.assertRaises(ValueError, backing.gl_init, ctx)
        finally:
            backing.close()
            win.destroy()

    def test_init_shaders(self):
        """init_shaders() compiles and links all fragment programs."""
        backing, win = self._make_gl_backing()
        try:
            ctx = backing.gl_context()
            if ctx:
                with ctx:
                    backing.init_textures()
                    backing.init_shaders()
                assert len(backing.programs) > 0
        finally:
            backing.close()
            win.destroy()

    def test_init_call(self):
        """init() updates render_size and resets gl_setup on size change."""
        backing, win = self._make_gl_backing()
        try:
            backing.gl_setup = True
            backing.size = (100, 100)
            backing.init(200, 150, 200, 150)
            assert backing.render_size == (200, 150)
            assert not backing.gl_setup
        finally:
            backing.close()
            win.destroy()

    def test_init_no_size_change(self):
        """init() does not reset gl_setup when size is unchanged."""
        backing, win = self._make_gl_backing()
        try:
            backing.gl_setup = True
            backing.size = (200, 150)
            backing.init(200, 150, 200, 150)
            assert backing.gl_setup
        finally:
            backing.close()
            win.destroy()

    def test_subsurface_intermediate_precision_matches_final_quantization(self):
        """Reduced output depth must not quantize every intermediate layer."""
        from OpenGL.GL import (
            glBindFramebuffer, glReadBuffer, glReadPixels,
            GL_READ_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_RGBA, GL_UNSIGNED_BYTE,
        )
        for pixel_depth, window_alpha, root, child in (
                (16, False, (5, 5, 5, 255), (1, 1, 1, 80)),
                (16, True, (20, 20, 20, 51), (0, 0, 0, 80)),
                (30, True, (20, 20, 20, 51), (0, 0, 0, 80)),
        ):
            with self.subTest(pixel_depth=pixel_depth, window_alpha=window_alpha):
                backing, win = self._make_gl_backing(window_alpha, pixel_depth)
                backing.size = backing.render_size = (3, 1)
                try:
                    ctx = self._full_gl_init(backing)
                    self.assertIsNotNone(ctx, "no mapped OpenGL context for precision regression")
                    configured_format = backing.internal_format
                    self.assertEqual(backing.offscreen_fbo_format, configured_format)

                    def paint(pixel, transaction_id, index=0, count=1, x=0, width=3):
                        results = []
                        backing.do_paint_rgb(
                            ctx, "rgb", "RGBA", bytes(pixel) * width, x, 0, width, 1, width, 1, width * 4,
                            _composite_options(
                                transaction_id, index, count,
                                reset=(x, 0, width, 1) if index == 0 else None,
                            ),
                            [lambda success, message="": results.append((success, message))],
                        )
                        self.assertEqual(len(results), 1)
                        self.assertIs(results[0][0], True, results)

                    def read_pixels():
                        glBindFramebuffer(GL_READ_FRAMEBUFFER, backing.offscreen_fbo)
                        glReadBuffer(GL_COLOR_ATTACHMENT0)
                        return bytes(glReadPixels(0, 0, *backing.size, GL_RGBA, GL_UNSIGNED_BYTE))

                    expected = tuple(s + (d * (255 - child[3]) + 127) // 255
                                     for s, d in zip(child, root))
                    with ctx:
                        # The output format may intentionally quantize the final
                        # result. Compare the same backing's one-shot result,
                        # not an unsupported demand for an eight-bit display.
                        paint(expected, 1)
                        reference = read_pixels()
                        paint(root, 2, 0, 2)
                        paint(child, 2, 1, 2)
                        self.assertEqual(read_pixels(), reference)
                        byte_format = GL_RGBA8 if window_alpha else GL_RGB8
                        self.assertEqual(backing.offscreen_fbo_format, byte_format)
                        self.assertEqual(reference, bytes(expected) * 3)

                        # A later partial reset retains the canonical byte
                        # canvas outside that region, including fractional alpha.
                        green = (0, 90, 0, 90)
                        paint(green, 3, x=1, width=1)
                        changed = bytes(expected) + bytes(green if window_alpha else (0, 90, 0, 255)) + bytes(expected)
                        self.assertEqual(read_pixels(), changed)

                        # Aborting a successor cannot quantize or replace the
                        # published byte canvas or its format identity.
                        paint(root, 4, 0, 2)
                        self.assertEqual(read_pixels(), changed)
                        backing.invalidate_subsurface_transaction(ctx)
                        self.assertEqual(read_pixels(), changed)
                        self.assertEqual(backing.offscreen_fbo_format, byte_format)

                        # Ordinary depth-owned pixels restore both FBO formats;
                        # configured depth/visual policy never changes globally.
                        from xpra.util.objects import typedict
                        ordinary = []
                        backing.do_paint_rgb(
                            ctx, "rgb", "RGBA", bytes(root) * 3,
                            0, 0, 3, 1, 3, 1, 12, typedict(),
                            [lambda success, message="": ordinary.append((success, message))],
                        )
                        self.assertEqual(ordinary, [(True, "")])
                        self.assertEqual(backing.offscreen_fbo_format, configured_format)
                        self.assertEqual(backing.tmp_fbo_format, configured_format)
                        ordinary_pixels = read_pixels()
                        paint(root, 5, 0, 2)
                        self.assertEqual(read_pixels(), ordinary_pixels)
                        self.assertEqual(backing.offscreen_fbo_format, configured_format)
                        backing.invalidate_subsurface_transaction(ctx)
                        self.assertEqual(read_pixels(), ordinary_pixels)
                        self.assertEqual(backing.offscreen_fbo_format, configured_format)
                        paint(expected, 6)
                        self.assertEqual(read_pixels(), reference)
                        self.assertEqual(backing.bit_depth, pixel_depth)
                        self.assertEqual(backing.internal_format, configured_format)

                        # Scrolling owns an ordinary scratch copy too. A
                        # byte-format scratch must not quantize later ordinary
                        # high-depth updates or survive under the wrong identity.
                        scroll = []
                        backing.do_scroll_paints(
                            ctx, ((0, 0, 2, 1, 1, 0),), 0,
                            [lambda success, message="": scroll.append((success, message))],
                        )
                        self.assertEqual(scroll, [(True, "")])
                        self.assertEqual(backing.offscreen_fbo_format, configured_format)
                        self.assertEqual(backing.tmp_fbo_format, configured_format)
                        paint(expected, 7)
                        self.assertEqual(read_pixels(), reference)

                        # Resize's copy path retains byte precision, even when
                        # an ordinary framebuffer would have two-bit alpha.
                        backing.size = backing.render_size = (4, 1)
                        backing.gl_setup = False
                        with patch("xpra.opengl.backing.FBO_RESIZE_DELAY", -1):
                            backing.resize_fbo(ctx, 3, 1, 4, 1)
                        self.assertEqual(read_pixels()[:12], reference)
                        self.assertEqual(backing.offscreen_fbo_format, byte_format)
                        self.assertEqual(backing.tmp_fbo_format, byte_format)
                finally:
                    backing.close()
                    win.destroy()
                    self.assertEqual(backing.offscreen_fbo_format, 0)
                    self.assertEqual(backing.tmp_fbo_format, 0)

    def test_premultiplied_source_over_reset_scaling_and_repeat(self):
        """The real FBO preserves source alpha even when the window backing is opaque."""
        from OpenGL.GL import (
            glBindFramebuffer, glReadBuffer, glReadPixels,
            GL_READ_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_RGBA, GL_UNSIGNED_BYTE,
        )
        width, height = 4, 2
        backing, win = self._make_gl_backing(window_alpha=False, pixel_depth=24)
        backing.size = backing.render_size = (width, height)
        try:
            ctx = self._full_gl_init(backing)
            self.assertIsNotNone(ctx, "no OpenGL context for subsurface composition regression")

            def paint(pixel_format, pixels, x, y, source_width, source_height,
                      render_width, render_height, transaction_id,
                      stage_index=0, stage_count=1, reset=(0, 0, width, height),
                      expected_success=True):
                options = _composite_options(
                    transaction_id, stage_index, stage_count, reset=reset,
                )
                results = []
                backing.do_paint_rgb(
                    ctx, "rgb", pixel_format, pixels,
                    x, y, source_width, source_height, render_width, render_height,
                    source_width * 4, options,
                    [lambda success, message="": results.append((success, message))],
                )
                self.assertEqual(len(results), 1)
                self.assertIs(results[0][0], expected_success, results)
                return results

            def read_pixels() -> bytes:
                glBindFramebuffer(GL_READ_FRAMEBUFFER, backing.offscreen_fbo)
                glReadBuffer(GL_COLOR_ATTACHMENT0)
                return bytes(glReadPixels(0, 0, width, height, GL_RGBA, GL_UNSIGNED_BYTE))

            # The zero X byte must still become an opaque root layer.
            blue_rgba = bytes((0, 0, 0xff, 0xff))
            blended_rgba = bytes((0x80, 0, 0x7f, 0xff))
            transaction_id = 1
            for opaque_format, blue in (
                    ("BGRX", bytes((0xff, 0, 0, 0))),
                    ("RGBX", bytes((0, 0, 0xff, 0))),
            ):
                for pixel_format, half_red in (
                        ("BGRA", bytes((0, 0, 0x80, 0x80))),
                        ("RGBA", bytes((0x80, 0, 0, 0x80))),
                ):
                    expected = (blue_rgba + blended_rgba * 2 + blue_rgba) * height
                    for _ in range(2):
                        with ctx:
                            before = read_pixels()
                            paint(opaque_format, blue * (width * height), 0, 0, width, height,
                                  width, height, transaction_id, 0, 2, (0, 0, width, height))
                            self.assertEqual(read_pixels(), before)
                            # Exercise the shader's non-identity coordinate scaling.
                            paint(pixel_format, half_red, 1, 0, 1, 1, 2, height,
                                  transaction_id, 1, 2, None)
                            actual = read_pixels()
                        self.assertEqual(
                            actual, expected, (opaque_format, pixel_format, actual, expected),
                        )
                        transaction_id += 1

            # Direct presentation on a single-buffered backend consumes only
            # `pending_fbo_paint`.  The final child layer must therefore queue
            # the complete transaction region, not just its own rectangle.
            previous_refresh = backing.draw_needs_refresh
            previous_paint_screen = backing.paint_screen
            backing.draw_needs_refresh = False
            backing.paint_screen = False
            backing.pending_fbo_paint.clear()
            try:
                with ctx:
                    paint(
                        "BGRX", bytes((0xff, 0, 0, 0)) * (width * height),
                        0, 0, width, height, width, height,
                        transaction_id, 0, 2, (0, 0, width, height),
                    )
                    self.assertEqual(backing.pending_fbo_paint, [])
                    paint(
                        "BGRA", bytes((0, 0, 0x80, 0x80)),
                        1, 0, 1, 1, 2, height,
                        transaction_id, 1, 2, None,
                    )
                self.assertEqual(backing.pending_fbo_paint, [(0, 0, width, height)])
            finally:
                backing.pending_fbo_paint.clear()
                backing.draw_needs_refresh = previous_refresh
                backing.paint_screen = previous_paint_screen
            transaction_id += 1

            # Any failed stage discards the private FBO and leaves the complete
            # visible FBO byte-for-byte unchanged. This includes the final
            # flush=0 stage, whose callback still reports exactly one failure.
            for failure_stage in range(3):
                with self.subTest(failure_stage=failure_stage), ctx:
                    before = read_pixels()
                    original_composite = backing.composite_premultiplied_texture
                    composite_call = 0

                    def composite_or_fail(*args, **kwargs):
                        nonlocal composite_call
                        stage_index = composite_call
                        composite_call += 1
                        if stage_index == failure_stage:
                            raise RuntimeError("injected OpenGL transaction stage failure")
                        return original_composite(*args, **kwargs)

                    with patch.object(backing, "painted") as painted, \
                            patch.object(
                                backing, "composite_premultiplied_texture", side_effect=composite_or_fail,
                            ):
                        for stage_index in range(failure_stage + 1):
                            paint(
                                "BGRX", bytes((0xff, 0, 0, 0)) * (width * height),
                                0, 0, width, height, width, height,
                                transaction_id, stage_index, 3,
                                (0, 0, width, height) if stage_index == 0 else None,
                                expected_success=stage_index != failure_stage,
                            )
                            self.assertEqual(read_pixels(), before)
                    painted.assert_not_called()
                    self.assertIsNone(backing._subsurface_transaction)
                    transaction_id += 1

            # Clearing one region must not alter either neighbour.
            with ctx:
                paint("BGRA", bytes(4), 1, 0, 1, 1, 2, height,
                      transaction_id, reset=(1, 0, 2, height))
                actual = read_pixels()
            self.assertEqual(actual, (blue_rgba + bytes((0, 0, 0, 0xff)) * 2 + blue_rgba) * height)
            transaction_id += 1

            # FBO identity swap is the publish point. A later presentation
            # bookkeeping error cannot truthfully NACK pixels which are
            # already visible in the backing and cannot be rolled back.
            opaque_green = bytes((0, 0xff, 0, 0))
            expected_green = bytes((0, 0xff, 0, 0xff)) * (width * height)
            with ctx, patch.object(
                    backing, "painted",
                    side_effect=RuntimeError("injected post-commit presentation failure"),
            ):
                paint(
                    "BGRX", opaque_green * (width * height),
                    0, 0, width, height, width, height,
                    transaction_id, reset=(0, 0, width, height),
                )
                actual = read_pixels()
            self.assertEqual(actual, expected_green)
            self.assertIsNone(backing._subsurface_transaction)
            self.assertEqual(backing._subsurface_transaction_floor, transaction_id)
        finally:
            backing.close()
            win.destroy()

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    def _full_gl_init(self, backing):
        """Run gl_init + init_shaders inside a context; return the context (or None)."""
        ctx = backing.gl_context()
        if ctx:
            with ctx:
                backing.gl_init(ctx)
                backing.init_shaders()
        return ctx

    # ------------------------------------------------------------------
    # packed RGB painting
    # ------------------------------------------------------------------

    def test_paint_rgbx_discards_padding_alpha(self):
        """The X component of BGRX and RGBX must not become backing alpha."""
        from OpenGL.GL import (
            glBindFramebuffer, glReadBuffer, glReadPixels,
            GL_READ_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_RGBA, GL_UNSIGNED_BYTE,
        )
        from xpra.util.objects import typedict

        w, h = 2, 1
        backing, win = self._make_gl_backing(window_alpha=True, pixel_depth=32)
        backing.size = backing.render_size = (w, h)
        try:
            ctx = self._full_gl_init(backing)
            if not ctx:
                self.skipTest("no OpenGL context")
            test_data = {
                "BGRX": bytes((0x30, 0x20, 0x10, 0x00,
                               0x60, 0x50, 0x40, 0x7f)),
                "RGBX": bytes((0x10, 0x20, 0x30, 0x00,
                               0x40, 0x50, 0x60, 0x7f)),
            }
            expected = bytes((0x10, 0x20, 0x30, 0xff,
                              0x40, 0x50, 0x60, 0xff))
            for pixel_format, pixels in test_data.items():
                with ctx:
                    backing.do_paint_rgb(ctx, "test", pixel_format, pixels,
                                         0, 0, w, h, w, h, w * 4, typedict(), [])
                    glBindFramebuffer(GL_READ_FRAMEBUFFER, backing.offscreen_fbo)
                    glReadBuffer(GL_COLOR_ATTACHMENT0)
                    data = glReadPixels(0, 0, w, h, GL_RGBA, GL_UNSIGNED_BYTE)
                actual = bytes(data) if not isinstance(data, bytes) else data
                assert actual == expected, f"{pixel_format}: expected {expected!r}, got {actual!r}"
        finally:
            backing.close()
            win.destroy()

    # ------------------------------------------------------------------
    # planar painting
    # ------------------------------------------------------------------

    def test_paint_planar_10bit(self):
        """Painting 10-bit planar images must give back the expected colours."""
        import struct
        from OpenGL.GL import (
            glBindFramebuffer, glReadBuffer, glReadPixels,
            GL_READ_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_RGBA, GL_UNSIGNED_BYTE,
        )
        from xpra.util.objects import typedict
        from xpra.codecs.image import ImageWrapper
        from xpra.codecs.constants import get_subsampling_divs
        # the pixel values are in BGRX byte order,
        # the colour names are just labels (as in `csc_colorspace_test`):
        test_data = {
            False: (
                ("black", "000000ff", 0x10, 0x80, 0x80),
                ("white", "ffffffff", 0xeb, 0x80, 0x80),
                ("green", "00ff00ff", 0x90, 0x36, 0x22),
                ("red", "ff0000ff", 0x29, 0xef, 0x6e),
                ("blue", "0000ffff", 0x52, 0x5a, 0xef),
                ("magenta", "ff00ffff", 0x6a, 0xc9, 0xdd),
            ),
            True: (
                ("black", "000000ff", 0x00, 0x80, 0x80),
                ("white", "ffffffff", 0xff, 0x80, 0x80),
                ("green", "00ff00ff", 0x95, 0x2c, 0x15),
                ("red", "ff0000ff", 0x1d, 0xff, 0x6b),
                ("blue", "0000ffff", 0x4d, 0x55, 0xff),
                ("magenta", "ff00ffff", 0x6a, 0xd4, 0xeb),
            ),
        }
        w = h = 64
        backing, win = self._make_gl_backing()
        backing.size = backing.render_size = (w, h)
        try:
            ctx = self._full_gl_init(backing)
            if not ctx:
                self.skipTest("no OpenGL context")
            for pixel_format in ("YUV420P10", "YUV422P10", "YUV444P10"):
                divs = get_subsampling_divs(pixel_format)
                for full_range, colors in test_data.items():
                    shader = f"{pixel_format}_to_RGB" + ("_FULL" if full_range else "")
                    for name, pixel, *yuv in colors:
                        planes = []
                        strides = []
                        for i, (xdiv, ydiv) in enumerate(divs):
                            pw = w // xdiv
                            # 8-bit sample scaled up to 10-bit, in 16-bit little-endian words:
                            planes.append(struct.pack("<H", yuv[i] << 2) * (pw * (h // ydiv)))
                            strides.append(pw * 2)
                        img = ImageWrapper(0, 0, w, h, planes, pixel_format, 24, strides,
                                           planes=ImageWrapper.PLANAR_3)
                        img.set_full_range(full_range)
                        with ctx:
                            backing.paint_planar(ctx, shader, "test", img, 0, 0, w, h, w, h,
                                                 typedict(), [])
                            glBindFramebuffer(GL_READ_FRAMEBUFFER, backing.offscreen_fbo)
                            glReadBuffer(GL_COLOR_ATTACHMENT0)
                            data = glReadPixels(0, 0, w, h, GL_RGBA, GL_UNSIGNED_BYTE)
                        bgrx = bytes.fromhex(pixel)
                        expected = (bgrx[2], bgrx[1], bgrx[0])
                        # `glReadPixels` gives us either bytes or a (h, w, 4) array,
                        # the image is a flat colour so any pixel will do:
                        first = data if isinstance(data, bytes) else data[0][0]
                        rgb = tuple(int(v) for v in first[:3])
                        delta = max(abs(a - b) for a, b in zip(rgb, expected))
                        info = f"{pixel_format} {name} full_range={full_range}"
                        assert delta <= 3, f"{info}: expected {expected} but got {rgb}"
        finally:
            backing.close()
            win.destroy()

    # ------------------------------------------------------------------
    # resize_fbo / copy_fbo / swap_fbos
    # ------------------------------------------------------------------

    def test_resize_fbo(self):
        """resize_fbo copies the existing FBO pixels onto a new-sized tmp FBO."""
        backing, win = self._make_gl_backing()
        try:
            ctx = backing.gl_context()
            if ctx:
                with ctx:
                    backing.gl_init(ctx)
                    # update size to simulate a window resize, then call resize_fbo
                    old_w, old_h = backing.size
                    new_w, new_h = old_w + 64, old_h + 32
                    backing.size = (new_w, new_h)
                    backing.render_size = (new_w, new_h)
                    backing.resize_fbo(ctx, old_w, old_h, new_w, new_h)
                assert backing.offscreen_fbo is not None
        finally:
            backing.close()
            win.destroy()

    def test_copy_fbo(self):
        """copy_fbo blits offscreen FBO into the tmp FBO without error."""
        backing, win = self._make_gl_backing()
        try:
            ctx = backing.gl_context()
            if ctx:
                with ctx:
                    backing.gl_init(ctx)
                    w, h = backing.size
                    backing.copy_fbo(w, h)
        finally:
            backing.close()
            win.destroy()

    def test_swap_fbos_gl(self):
        """swap_fbos() exchanges fbo handles and texture indices after full init."""
        from xpra.opengl.backing import TEX_FBO, TEX_TMP_FBO
        backing, win = self._make_gl_backing()
        try:
            ctx = backing.gl_context()
            if ctx:
                with ctx:
                    backing.gl_init(ctx)
                off_before = backing.offscreen_fbo
                tmp_before = backing.tmp_fbo
                tex_fbo_before = backing.textures[TEX_FBO]
                tex_tmp_before = backing.textures[TEX_TMP_FBO]
                backing.swap_fbos()
                assert backing.offscreen_fbo == tmp_before
                assert backing.tmp_fbo == off_before
                assert backing.textures[TEX_FBO] == tex_tmp_before
                assert backing.textures[TEX_TMP_FBO] == tex_fbo_before
        finally:
            backing.close()
            win.destroy()

    # ------------------------------------------------------------------
    # do_present_fbo / present_fbo / _present_fbo_catmull_rom
    # ------------------------------------------------------------------

    def test_do_present_fbo(self):
        """do_present_fbo renders the offscreen FBO to the display framebuffer."""
        backing, win = self._make_gl_backing()
        try:
            ctx = backing.gl_context()
            if ctx:
                with ctx:
                    backing.gl_init(ctx)
                    backing.pending_fbo_paint = [(0, 0, *backing.size)]
                    backing.do_present_fbo(ctx)
        finally:
            backing.close()
            win.destroy()

    def test_present_fbo(self):
        """present_fbo appends the rect and flushes through to do_present_fbo."""
        backing, win = self._make_gl_backing()
        try:
            ctx = backing.gl_context()
            if ctx:
                with ctx:
                    backing.gl_init(ctx)
                    backing.paint_screen = True
                    w, h = backing.size
                    backing.present_fbo(ctx, 0, 0, w, h, flush=0)
        finally:
            backing.close()
            win.destroy()

    def test_present_fbo_catmull_rom(self):
        """_present_fbo_catmull_rom runs when the upscale shader is available."""
        backing, win = self._make_gl_backing()
        try:
            ctx = self._full_gl_init(backing)
            if ctx and "upscale" in backing.programs:
                bw, bh = backing.size
                with ctx:
                    backing._present_fbo_catmull_rom(2.0, 2.0, 0, 0)
        finally:
            backing.close()
            win.destroy()

    # ------------------------------------------------------------------
    # save_fbo
    # ------------------------------------------------------------------

    def test_save_fbo(self):
        """save_fbo delegates to the utility function with correct arguments."""
        backing, win = self._make_gl_backing()
        try:
            ctx = backing.gl_context()
            if ctx:
                with ctx:
                    backing.gl_init(ctx)
                    with patch("xpra.opengl.backing.save_fbo") as mock_save:
                        backing.save_fbo()
                        mock_save.assert_called_once()
                        args = mock_save.call_args[0]
                        assert args[0] == backing.wid
                        assert args[3] == backing.size[0]
                        assert args[4] == backing.size[1]
        finally:
            backing.close()
            win.destroy()

    # ------------------------------------------------------------------
    # Alert overlays
    # ------------------------------------------------------------------

    def test_upload_alert_texture(self):
        """upload_alert_texture returns False gracefully when no icon is available."""
        backing, win = self._make_gl_backing()
        try:
            ctx = backing.gl_context()
            if ctx:
                with ctx:
                    backing.gl_init(ctx)
                    result = backing.upload_alert_texture()
                    assert isinstance(result, bool)
                    # second call should return the cached result
                    result2 = backing.upload_alert_texture()
                    assert result2 == result
        finally:
            backing.close()
            win.destroy()

    def test_draw_alert_spinner(self):
        """draw_alert_spinner uses the fixed-color shader to draw NLINES sectors."""
        backing, win = self._make_gl_backing()
        try:
            ctx = self._full_gl_init(backing)
            if ctx:
                with ctx:
                    backing.draw_alert_spinner()
                    backing.draw_alert_spinner(outer_pct=40)   # small-spinner variant
                    backing.draw_alert_spinner(outer_pct=90)   # big-spinner variant
        finally:
            backing.close()
            win.destroy()

    def test_draw_alert_shade(self):
        """draw_alert_shade blends a semi-transparent shade over the FBO."""
        backing, win = self._make_gl_backing()
        try:
            ctx = self._full_gl_init(backing)
            if ctx:
                with ctx:
                    backing.draw_alert_shade()              # default shade=0.5
                    backing.draw_alert_shade(shade=0.2)     # dark-shade
                    backing.draw_alert_shade(shade=0.8)     # light-shade
        finally:
            backing.close()
            win.destroy()

    def test_draw_alert_icon(self):
        """draw_alert_icon completes without error; alert_uploaded is set to ±1."""
        backing, win = self._make_gl_backing()
        try:
            ctx = self._full_gl_init(backing)
            if ctx:
                with ctx:
                    backing.draw_alert_icon()
                # alert_uploaded is either 1 (icon found & uploaded) or -1 (not found)
                assert backing.alert_uploaded != 0
        finally:
            backing.close()
            win.destroy()

    def test_draw_alert_icon_no_icon(self):
        """draw_alert_icon returns early and sets alert_uploaded=-1 when no icon exists."""
        backing, win = self._make_gl_backing()
        try:
            ctx = self._full_gl_init(backing)
            if ctx:
                from xpra.client.gui.window.backing import WindowBackingBase
                with patch.object(WindowBackingBase, "get_alert_icon", return_value=(0, 0, None)):
                    with ctx:
                        backing.draw_alert_icon()
                    assert backing.alert_uploaded == -1
        finally:
            backing.close()
            win.destroy()

    # ------------------------------------------------------------------
    # draw_pointer
    # ------------------------------------------------------------------

    def test_draw_pointer_gl(self):
        """draw_pointer renders the cursor overlay texture at the pointer position."""
        from time import monotonic
        from xpra.opengl.backing import TEX_CURSOR
        from xpra.opengl.util import upload_rgba_texture
        backing, win = self._make_gl_backing()
        try:
            ctx = self._full_gl_init(backing)
            if ctx:
                cw, ch = 16, 16
                pixels = b"\x80\x80\x80\xff" * (cw * ch)
                with ctx:
                    upload_rgba_texture(int(backing.textures[TEX_CURSOR]), cw, ch, pixels)
                # cursor_data: (name, serial, pixel_seq, width, height, xhot, yhot, cursor_serial, pixels)
                backing.cursor_data = (None, None, None, cw, ch, 0, 0, None, pixels)
                backing.pointer_overlay = (50, 50, 0, 0, 0, monotonic())
                with ctx:
                    backing.draw_pointer()
        finally:
            backing.close()
            win.destroy()

    # ------------------------------------------------------------------
    # draw_border / paint_box
    # ------------------------------------------------------------------

    def test_draw_border(self):
        """draw_border blends a coloured rectangle around the FBO edges."""
        from xpra.client.gui.window_border import WindowBorder
        backing, win = self._make_gl_backing()
        try:
            ctx = self._full_gl_init(backing)
            if ctx:
                border = WindowBorder(shown=True, red=1.0, green=0.0, blue=0.0, alpha=0.6, size=4)
                with ctx:
                    backing.draw_border(border)
                    # small window: border larger than window forces single-rect mode
                    border_big = WindowBorder(shown=True, size=512)
                    backing.draw_border(border_big)
        finally:
            backing.close()
            win.destroy()

    def test_paint_box(self):
        """paint_box draws a debug rectangle around the painted region."""
        backing, win = self._make_gl_backing()
        try:
            ctx = self._full_gl_init(backing)
            if ctx:
                backing.paint_box_line_width = 2
                with ctx:
                    backing.paint_box("rgb24", 10, 20, 80, 60)
                    backing.paint_box("h264", 0, 0, *backing.size)
        finally:
            backing.close()
            win.destroy()


def main():
    unittest.main()


if __name__ == "__main__":
    main()
