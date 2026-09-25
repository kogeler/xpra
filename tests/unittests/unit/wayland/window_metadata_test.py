#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 kogeler
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import unittest
from unittest.mock import patch

from xpra.server.subsystem.window import WindowServer
from xpra.server.window.metadata import make_window_metadata
from xpra.wayland.server.models.window import Window
from xpra.wayland.server.models.subsurface_window import SubsurfaceWindow


class WaylandWindowMetadataTest(unittest.TestCase):

    def test_pixel_format_is_internal_but_available_in_window_info(self):
        server = WindowServer()
        window = Window({"geometry": (0, 0, 64, 32), "has-alpha": True})

        def get_info():
            info = server.get_window_info(window)
            self.assertEqual(info["size"], (64, 32))
            self.assertEqual(info["geometry"], (0, 0, 64, 32))
            self.assertTrue(info["has-alpha"])
            return info

        self._assert_frame_metadata(window, get_info)

    def test_internal_subsurface_frame_metadata_uses_shared_serializer(self):
        # The facade belongs to subsurface_facades, not the registered window
        # map. It has no public geometry property and is never passed to the
        # generic window-info enumerator. Exercise its actual metadata contract
        # without inventing a public window or substituting a geometry method.
        window = SubsurfaceWindow(64, 32)

        def get_info():
            info = {}
            for prop in window.get_property_names() + window.get_internal_property_names():
                info.update(make_window_metadata(window, prop, skip_defaults=False))
            self.assertEqual(window.get_dimensions(), (64, 32))
            self.assertTrue(info["has-alpha"])
            return info

        self._assert_frame_metadata(window, get_info)

    def _assert_frame_metadata(self, window, get_info):
        for prop in ("pixel-format", "frame-has-alpha"):
            self.assertIn(prop, window.get_internal_property_names())
            self.assertNotIn(prop, window.get_property_names())
            self.assertNotIn(prop, window.get_dynamic_property_names())
        for pixel_format in ("", "BGRX", "BGRA", "RGBX", "RGBA", ""):
            with self.subTest(pixel_format=pixel_format):
                window._updateprop("pixel-format", pixel_format)
                alpha = not pixel_format or "A" in pixel_format
                window._updateprop("frame-has-alpha", alpha)
                with patch("xpra.server.window.metadata.get_util_logger") as get_logger:
                    info = get_info()
                self.assertEqual(info.get("pixel-format"), pixel_format)
                self.assertEqual(info.get("frame-has-alpha"), alpha)
                get_logger.assert_not_called()


if __name__ == "__main__":
    unittest.main()
