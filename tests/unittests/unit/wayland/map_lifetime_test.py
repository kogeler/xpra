# This file is part of Xpra.
# Copyright (C) 2026 kogeler
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

"""Map refresh must respect window removal during compositor dispatch."""

import unittest
from unittest.mock import Mock, patch

from xpra.net.common import Packet
from xpra.wayland.server.models.window import Window
from xpra.wayland.server.subsystem.window import WaylandWindowServer


class WaylandMapLifetimeTest(unittest.TestCase):
    @staticmethod
    def make_window(wid):
        surface = Mock(wid=wid)
        window = Window({"surface": surface, "geometry": (0, 0, 160, 120)})
        window.setup()
        return window, surface

    def test_map_refreshes_the_current_window(self):
        connection = Mock()
        server = Mock()
        server.get_server_source.return_value = connection
        manager = WaylandWindowServer(server)
        window, surface = self.make_window(7)
        try:
            with patch.object(WaylandWindowServer, "window_sources", return_value=(connection,)):
                manager.do_add_new_window_common(7, window)
                manager._process_map(object(), Packet("window-map", 7, 0, 0, 160, 120))
            surface.resize.assert_called_once_with(160, 120)
            server.compositor.flush.assert_called_once_with()
            connection.damage.assert_called_once_with(7, window, 0, 0, 160, 120, None)
        finally:
            window.unmanage()

    def test_map_does_not_refresh_a_window_removed_during_flush(self):
        for replace in (False, True):
            with self.subTest(replace=replace):
                connection = Mock()
                server = Mock()
                server.get_server_source.return_value = connection
                manager = WaylandWindowServer(server)
                window, surface = self.make_window(7)
                replacement, _ = self.make_window(7)

                def dispatch_destroy():
                    manager.do_remove_window(7, window)
                    window.unmanage()
                    if replace:
                        manager.do_add_new_window_common(7, replacement)

                server.compositor.flush.side_effect = dispatch_destroy
                try:
                    with patch.object(WaylandWindowServer, "window_sources", return_value=(connection,)):
                        manager.do_add_new_window_common(7, window)
                        manager._process_map(object(), Packet("window-map", 7, 0, 0, 160, 120))
                    surface.resize.assert_called_once_with(160, 120)
                    connection.lost_window.assert_called_once_with(7, window)
                    connection.remove_window.assert_called_once_with(7, window)
                    connection.damage.assert_not_called()
                    self.assertIs(manager.get_window(7), replacement if replace else None)
                finally:
                    window.unmanage()
                    replacement.unmanage()


if __name__ == "__main__":
    unittest.main()
