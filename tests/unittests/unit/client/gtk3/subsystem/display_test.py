#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import unittest
from unittest.mock import patch

from unit.process_test_util import DisplayContext


class Gtk3DisplayClientTest(unittest.TestCase):
    """
    Real end-to-end check that a concrete `xpra.client.gtk3.client.XpraClient()`
    actually composes a `Gtk3DisplayClient` for its `display` subsystem (via
    `GTKXpraClient.get_subsystem_classes()`), and that the toolkit-specific
    implementation returns real values instead of the base class'
    `NotImplementedError` stubs.
    """

    def test_gtk3_display_client(self):
        with DisplayContext():
            from xpra.client.gtk3.client import XpraClient
            from xpra.client.gtk3.subsystem.display import Gtk3DisplayClient
            client = XpraClient()
            try:
                display = client.get_subsystem("display")
                self.assertIsNotNone(display, "no `display` subsystem composed")
                self.assertIsInstance(display, Gtk3DisplayClient)

                root_w, root_h = display.get_root_size()
                self.assertGreater(root_w, 0)
                self.assertGreater(root_h, 0)

                sizes = display.get_screen_sizes()
                self.assertTrue(sizes)

                monitors = display.get_monitors_info()
                self.assertIsInstance(monitors, dict)

                self.assertIsInstance(display.has_transparency(), bool)

                window_caps = client.get_subsystem("window").get_window_caps()
                self.assertEqual(
                    window_caps.get("subsurface-composite"),
                    ("premultiplied-source-over-v1",),
                )

                # The claim is global: disabling the concrete Cairo fallback
                # or selecting the test-only fake backing must remove it.
                from xpra.cairo.backing import CairoBacking
                with patch.object(CairoBacking, "SUBSURFACE_COMPOSITE_MODES", ()):
                    self.assertNotIn(
                        "subsurface-composite",
                        client.get_subsystem("window").get_window_caps(),
                    )
                gl = client.get_subsystem("opengl")
                from xpra.opengl.backing import GLWindowBackingBase
                with patch.object(gl, "GLClientWindowClass", object), \
                        patch.object(GLWindowBackingBase, "SUBSURFACE_COMPOSITE_MODES", ()):
                    self.assertNotIn(
                        "subsurface-composite",
                        client.get_subsystem("window").get_window_caps(),
                    )
                # Optional-backend introspection failures are diagnosed but
                # fail closed: hello neither aborts nor advertises a mode that
                # the selectable fallback cannot implement.
                from xpra.client.gtk3 import client_base
                with patch.object(CairoBacking, "SUBSURFACE_COMPOSITE_MODES", None), \
                        patch.object(client_base, "opengllog") as capability_log:
                    self.assertNotIn(
                        "subsurface-composite",
                        client.get_subsystem("window").get_window_caps(),
                    )
                    capability_log.error.assert_called_once_with(
                        "Error querying subsurface-composite backing support",
                        exc_info=True,
                    )
                with patch.object(CairoBacking, "SUBSURFACE_COMPOSITE_MODES", "premultiplied-source-over-v1"), \
                        patch.object(client_base, "opengllog") as capability_log:
                    self.assertNotIn(
                        "subsurface-composite",
                        client.get_subsystem("window").get_window_caps(),
                    )
                    capability_log.error.assert_called_once_with(
                        "Error querying subsurface-composite backing support",
                        exc_info=True,
                    )
                with patch.object(CairoBacking, "SUBSURFACE_COMPOSITE_MODES", ("future-mode",)):
                    self.assertNotIn(
                        "subsurface-composite",
                        client.get_subsystem("window").get_window_caps(),
                    )
                with patch.object(gl, "GLClientWindowClass", object), \
                        patch.object(GLWindowBackingBase, "SUBSURFACE_COMPOSITE_MODES", None), \
                        patch.object(client_base, "opengllog") as capability_log:
                    self.assertNotIn(
                        "subsurface-composite",
                        client.get_subsystem("window").get_window_caps(),
                    )
                    capability_log.error.assert_called_once_with(
                        "Error querying subsurface-composite backing support",
                        exc_info=True,
                    )
                from xpra.client.gui import widget_base
                with patch.object(widget_base, "USE_FAKE_BACKING", True):
                    self.assertNotIn(
                        "subsurface-composite",
                        client.get_subsystem("window").get_window_caps(),
                    )
            finally:
                client.cleanup()


def main():
    unittest.main()


if __name__ == '__main__':
    main()
