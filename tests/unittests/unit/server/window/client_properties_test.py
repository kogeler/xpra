#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 kogeler
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

"""Client property dictionaries are deltas, not full window snapshots."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from xpra.common import noop
from xpra.server.window import video_compress
from xpra.server.window.compress import WindowSource
from xpra.server.window.video_compress import WindowVideoSource
from xpra.util.objects import typedict


class ClientPropertiesTest(unittest.TestCase):
    @staticmethod
    def make_source(source_class=WindowSource):
        # Exercise the real property parser, isolating unrelated encoder/quality
        # calculations and avoiding a native window or display dependency.
        source = source_class.__new__(source_class)
        source.maximized = True
        source.client_render_size = (640, 480)
        source.client_bit_depth = 24
        source.client_refresh_encodings = ("rgb24",)
        source.full_frames_only = False
        source.is_tray = False
        source.supports_transparency = False
        source.encodings = ("h264", "rgb24")
        source.core_encodings = source.encodings
        source.decoder_speed = typedict()
        source.rgb_formats = ("RGB", "BGRX")
        source.full_csc_modes = typedict({"h264": ("YUV420P",)})
        source._all_encoders = {"rgb24": [noop]}
        source._encoders = {}
        source.encoding = "h264"
        source.update_encoding_selection = Mock()
        if isinstance(source, WindowVideoSource):
            source.common_encodings = source.encodings
            source.scroll_min_percent = 50
            source.scroll_preference = 100
            source.video_subregion = SimpleNamespace(supported=False)
            source.scaling_control = None
            source.picture_encodings = ("rgb24",)
        return source

    def test_csc_delta_preserves_maximized(self):
        source = self.make_source()
        source.set_client_properties(typedict({"encoding.full_csc_modes": {}}))
        self.assertTrue(source.maximized)
        self.assertEqual(source.full_csc_modes, {})
        source.update_encoding_selection.assert_called_once_with("h264")

    def test_csc_delta_preserves_render_size(self):
        source = self.make_source()
        source.set_client_properties(typedict({"encoding.full_csc_modes": {}}))
        self.assertEqual(source.client_render_size, (640, 480))

    def test_csc_delta_preserves_disabled_video_subregion(self):
        source = self.make_source(WindowVideoSource)
        with patch.object(video_compress, "VIDEO_SUBREGION", True):
            source.set_client_properties(typedict({"encoding.full_csc_modes": {}}))
        self.assertFalse(source.video_subregion.supported)

    def test_explicit_values_still_replace_state(self):
        source = self.make_source(WindowVideoSource)
        with patch.object(video_compress, "VIDEO_SUBREGION", True):
            source.set_client_properties(typedict({
                "maximized": False,
                "encoding.render-size": (800, 600),
                "encoding.video_subregion": True,
                "encoding.full_csc_modes": {"vp9": ("YUV420P",)},
            }))
        self.assertFalse(source.maximized)
        self.assertEqual(source.client_render_size, (800, 600))
        self.assertTrue(source.video_subregion.supported)
        self.assertEqual(source.full_csc_modes, {"vp9": ("YUV420P",)})


if __name__ == "__main__":
    unittest.main()
