#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 kogeler
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from xpra.net.common import Packet
from xpra.server import core
from xpra.server.base import ServerBase
from xpra.server.core import ServerCore


class ServerShutdownFlushTest(unittest.TestCase):

    def test_disconnect_packets_get_the_flush_interval_before_sockets_close(self):
        # A clean server exit queues every client's disconnect packet, then
        # closes its sockets and exits. Without the flush interval in between,
        # the write threads may not write that packet before the process exits
        # and the client sees a lost connection instead of the reason.
        events = []
        server = ServerBase.__new__(ServerBase)
        with (
            patch.object(ServerBase, "_dispatch_fire", lambda _self, *_args, **_kwargs: events.append("subsystems")),
            patch.object(ServerBase, "cancel_touch_timer", lambda _self: None),
            patch.object(ServerBase, "cleanup_all_protocols", lambda _self, *_args, **_kwargs: events.append("disconnect")),
            patch.object(ServerBase, "server_event", lambda _self, *args: events.append(("event", *args))),
            patch.object(ServerBase, "cleanup_sockets", lambda _self: events.append("sockets")),
            patch.object(core, "sleep", lambda seconds: events.append(("flush", seconds))),
            patch.object(core, "stop_asyncio_loop", lambda: None),
        ):
            server.cleanup()
        self.assertEqual(
            events,
            ["subsystems", "disconnect", ("event", "exit"), ("flush", 0.1), "sockets"],
        )

    def test_late_client_packet_does_not_close_the_disconnecting_protocol(self):
        # Shutdown detaches each client right after queuing its disconnect
        # packet. A packet which the client sent before it received that
        # disconnect must not close the connection under the last write.
        server = SimpleNamespace(
            _closing=True,
            _potential_protocols=[],
            get_server_source=Mock(return_value=None),
        )
        proto = Mock()
        proto.is_closed.return_value = False
        with patch("xpra.server.core.netlog") as netlog:
            ServerCore.handle_invalid_packet(server, proto, Packet("focus", 0))
        netlog.error.assert_not_called()
        proto.close.assert_not_called()


def main():
    unittest.main()


if __name__ == "__main__":
    main()
