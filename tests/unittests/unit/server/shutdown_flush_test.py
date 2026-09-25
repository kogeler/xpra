#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 kogeler
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import unittest
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import Mock, patch

from xpra.common import noop
from xpra.net import compression, packet_encoding
from xpra.net.common import Packet
from xpra.net.packet_type import CONNECTION_CLOSE
from xpra.server import core
from xpra.server.base import ServerBase
from xpra.server.core import ServerCore

from unit.net.protocol_flush_test import TIMEOUT, HeldWriteConnection, ManualScheduler, WriteThreadHoldsLastPacket


class ServerShutdownFlushTest(unittest.TestCase):

    def test_disconnect_packets_get_the_flush_interval_before_sockets_close(self):
        # A clean server exit queues every client's disconnect packet, then
        # closes its sockets and exits. Without the flush interval in between,
        # the write threads may not write that packet before the process exits
        # and the client sees a lost connection instead of the reason.
        events = []
        server = ServerBase.__new__(ServerBase)
        server.subsystems = {"network": SimpleNamespace(
            cleanup=lambda: events.append("subsystems"),
            cancel_touch_timer=lambda: None,
            cleanup_all_protocols=lambda: events.append("disconnect"),
            cleanup_sockets=lambda: events.append("sockets"),
        )}
        with (
            patch.object(ServerBase, "server_event", lambda _self, *args: events.append(("event", *args))),
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
            network=SimpleNamespace(_potential_protocols=[]),
            get_server_source=Mock(return_value=None),
        )
        proto = Mock()
        proto.is_closed.return_value = False
        with patch("xpra.server.core.netlog") as netlog:
            ServerCore.handle_invalid_packet(server, proto, Packet("window-focus", 0, ()))
        netlog.error.assert_not_called()
        proto.close.assert_not_called()

    def test_late_client_packet_after_detach_keeps_the_disconnect(self):
        # `xpra detach` disconnects the attached client while the server keeps running:
        # `disconnect_protocol` queues the `connection-close` packet, then removes the
        # client's source, so the client's next packet (ie: pointer motion) arrives
        # from a detached protocol - while the disconnect packet is still being written.
        packet_encoding.init_all()
        compression.init_all()
        conn = HeldWriteConnection()
        self.addCleanup(conn.release.set)
        scheduler = ManualScheduler()
        protocol = WriteThreadHoldsLastPacket(conn, noop, scheduler=scheduler)
        protocol.enable_default_encoder()
        protocol.enable_default_compressor()
        server = SimpleNamespace(
            _closing=False,
            network=SimpleNamespace(_potential_protocols=[]),
            get_server_source=Mock(return_value=None),
        )

        protocol.send_disconnect(["detach request"])
        with patch("xpra.server.core.netlog") as netlog:
            ServerCore.handle_invalid_packet(server, protocol, Packet("pointer", 1, (100, 100)))

        netlog.error.assert_not_called()
        self.assertFalse(protocol.is_closed(), "the late packet closed the protocol under its disconnect")
        conn.release.set()
        wait_for_packet_sent = [fn for timeout, fn, _args in scheduler.timers if timeout == 100][0]
        deadline = monotonic() + TIMEOUT
        while wait_for_packet_sent():
            self.assertLess(monotonic(), deadline, "the disconnect packet was never reported as sent")
            sleep(0.01)
        self.assertEqual(conn.events, [("written", CONNECTION_CLOSE), "close"])

    def test_invalid_packet_from_connection_without_source_still_closes_it(self):
        # not detached: still being set up, and nothing else will close it
        proto = Mock()
        proto.is_closed.return_value = False
        server = SimpleNamespace(
            _closing=False,
            network=SimpleNamespace(_potential_protocols=[proto]),
            get_server_source=Mock(return_value=None),
        )
        with patch("xpra.server.core.netlog") as netlog:
            ServerCore.handle_invalid_packet(server, proto, Packet("window-focus", 0, ()))
        netlog.error.assert_called()
        proto.close.assert_called_once_with()


def main():
    unittest.main()


if __name__ == "__main__":
    main()
