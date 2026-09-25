#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 kogeler
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import unittest
from time import monotonic, sleep
from threading import Event

from xpra.common import noop
from xpra.net import compression, packet_encoding
from xpra.net.bytestreams import Connection
from xpra.net.packet_type import CONNECTION_CLOSE
from xpra.net.protocol.socket_handler import SocketProtocol

TIMEOUT = 10


class HeldWriteConnection(Connection):
    """ each write blocks until the test releases it, like a busy socket """

    def __init__(self):
        super().__init__("local", "tcp")
        self.writing = Event()
        self.release = Event()
        self.events = []

    def read(self, _n) -> bytes:
        return b""

    def write(self, buf, packet_type: str = "") -> int:
        self.writing.set()
        self.release.wait(TIMEOUT)
        if not self.active:
            self.events.append(("lost", packet_type))
            raise OSError("write on a closed connection")
        self.events.append(("written", packet_type))
        return len(buf)

    def close(self) -> None:
        self.events.append("close")
        super().close()


class ManualScheduler:
    """ the main loop does not run: timers only fire when the test calls them """

    def __init__(self):
        self.timers = []

    def idle_add(self, fn, *args, **kwargs) -> int:
        return 0

    def timeout_add(self, timeout: int, fn, *args, **kwargs) -> int:
        self.timers.append((timeout, fn, args))
        return len(self.timers)

    def source_remove(self, tid: int) -> None:
        """ not needed """


class WriteThreadHoldsLastPacket(SocketProtocol):

    def raw_write(self, items, packet_type="", synchronous=True, more=False) -> None:
        super().raw_write(items, packet_type, synchronous, more)
        # the write thread has taken the packet off the queue and is writing it:
        if not self._conn.writing.wait(TIMEOUT):
            raise RuntimeError("the write thread did not start writing")


class FlushThenCloseTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        packet_encoding.init_all()
        compression.init_all()

    def test_disconnect_is_written_before_the_connection_closes(self):
        # `send_disconnect` must only close once the write thread has written the
        # last packet, not as soon as the packet has left the write queue.
        conn = HeldWriteConnection()
        self.addCleanup(conn.release.set)
        scheduler = ManualScheduler()
        protocol = WriteThreadHoldsLastPacket(conn, noop, scheduler=scheduler)
        protocol.enable_default_encoder()
        protocol.enable_default_compressor()
        done = []

        protocol.send_disconnect(["server shutdown"], done_callback=lambda: done.append(True))

        self.assertFalse(protocol.is_closed(), "closed while the disconnect packet was still being written")
        self.assertEqual(conn.events, [])
        self.assertEqual(done, [])
        timers = [fn for timeout, fn, _args in scheduler.timers if timeout == 100]
        self.assertEqual(len(timers), 1, f"expected one packet-sent poll timer in {scheduler.timers}")
        wait_for_packet_sent = timers[0]
        self.assertTrue(wait_for_packet_sent())

        conn.release.set()
        deadline = monotonic() + TIMEOUT
        while wait_for_packet_sent():
            self.assertLess(monotonic(), deadline, "the written packet was never reported as sent")
            sleep(0.01)
        self.assertEqual(conn.events, [("written", CONNECTION_CLOSE), "close"])
        self.assertTrue(protocol.is_closed())
        self.assertEqual(done, [True])


def main():
    unittest.main()


if __name__ == "__main__":
    main()
