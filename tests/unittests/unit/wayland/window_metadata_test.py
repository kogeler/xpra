#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 kogeler
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import unittest
from unittest.mock import patch

from xpra.server.subsystem.window import WindowServer
from xpra.wayland.server.models.window import Window
from xpra.wayland.server.models.subsurface_window import SubsurfaceWindow


class WaylandWindowMetadataTest(unittest.TestCase):

    def test_pixel_format_is_internal_but_available_in_window_info(self):
        server = WindowServer()
        windows = (
            Window({"geometry": (0, 0, 64, 32), "has-alpha": True}),
            SubsurfaceWindow(64, 32),
        )
        for window in windows:
            with self.subTest(model=type(window).__name__):
                self.assertIn("pixel-format", window.get_internal_property_names())
                self.assertNotIn("pixel-format", window.get_property_names())
                self.assertNotIn("pixel-format", window.get_dynamic_property_names())
                self.assertIn("frame-has-alpha", window.get_internal_property_names())
                self.assertNotIn("frame-has-alpha", window.get_property_names())
                self.assertNotIn("frame-has-alpha", window.get_dynamic_property_names())
                for pixel_format in ("", "BGRX", "BGRA", "RGBX", "RGBA", ""):
                    with self.subTest(pixel_format=pixel_format):
                        window._updateprop("pixel-format", pixel_format)
                        alpha = not pixel_format or "A" in pixel_format
                        window._updateprop("frame-has-alpha", alpha)
                        with patch("xpra.server.window.metadata.get_util_logger") as get_logger:
                            info = server.get_window_info(window)
                        self.assertEqual(info.get("pixel-format"), pixel_format)
                        self.assertEqual(info.get("frame-has-alpha"), alpha)
                        self.assertEqual(info["size"], (64, 32))
                        self.assertTrue(info["has-alpha"])
                        get_logger.assert_not_called()


if __name__ == "__main__":
    unittest.main()
