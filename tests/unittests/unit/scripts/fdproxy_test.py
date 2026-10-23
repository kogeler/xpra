#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 kogeler
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import os
import select
import signal
import socket
import threading
import time
import unittest
from unittest.mock import patch

from xpra.scripts import fdproxy
from xpra.net.bytestreams import SocketConnection, TwoFileConnection

# more than the client pipe and our read buffer hold, so the relay to the client is still busy
# when the server has already queued its last packet and closed its socket:
PAYLOAD = b"x" * (3 * fdproxy.PROXY_BUFFER_SIZE)
LAST_PACKET = b"connection-close: detach request"


class XpraProxyCloseTest(unittest.TestCase):
    """
    The real `xpra _proxy` relay between an ssh channel (pipes) and the server's socket.
    Only `force_quit` is replaced, so that the test process survives the proxy's exit.
    """

    def setUp(self) -> None:
        self.handlers = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM,
                                                          signal.SIGHUP, signal.SIGPIPE)}
        self.server_end, proxy_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        stdin_r, self.stdin_w = os.pipe()
        self.stdout_r, stdout_w = os.pipe()
        client_conn = TwoFileConnection(os.fdopen(stdout_w, "wb", 0), os.fdopen(stdin_r, "rb", 0),
                                        socktype="stdin/stdout")
        server_conn = SocketConnection(proxy_end, "local", "remote", "socket://test", "socket")
        self.exits: list[int] = []
        quit_patch = patch.object(fdproxy, "force_quit", side_effect=self.exits.append)
        quit_patch.start()
        self.addCleanup(quit_patch.stop)
        self.proxy = fdproxy.XpraProxy("test", client_conn, server_conn)
        self.runner = threading.Thread(target=self.proxy.run, daemon=True)
        self.runner.start()

    def tearDown(self) -> None:
        # end both relay threads before `force_quit` is restored:
        for fd in (self.stdin_w, self.stdout_r):
            try:
                os.close(fd)
            except OSError:
                pass
        self.server_end.close()
        self.runner.join(5)
        for signum, handler in self.handlers.items():
            signal.signal(signum, handler)
        self.assertFalse(self.runner.is_alive(), "the proxy did not end")

    def server_sends_last_packet_and_closes(self) -> None:
        def server() -> None:
            self.server_end.sendall(PAYLOAD + LAST_PACKET)
            self.server_end.close()
        sender = threading.Thread(target=server, daemon=True)
        sender.start()
        sender.join(10)
        self.assertFalse(sender.is_alive(), "the server could not queue its last packet")

    def wait_for_exit(self, timeout=5) -> None:
        # `quit()` calls `force_quit()` last, after closing both connections
        # (python signal handlers run in this main thread, so keep it running):
        deadline = time.monotonic() + timeout
        while not self.exits and time.monotonic() < deadline:
            time.sleep(0.01)

    def client_receives(self) -> bytes:
        data = b""
        while True:
            ready = select.select([self.stdout_r], [], [], 5)[0]
            self.assertTrue(ready, "the client stream did not end")
            chunk = os.read(self.stdout_r, 65536)
            if not chunk:
                return data
            data += chunk

    def test_last_server_packet_reaches_client_sending_after_close(self) -> None:
        # ie: `xpra detach` from another connection, while this client is still sending
        self.server_sends_last_packet_and_closes()
        os.write(self.stdin_w, b"pointer-position")
        # the failed write to the server must not end the relay of what it sent before:
        self.wait_for_exit(timeout=1)
        received = self.client_receives()
        self.assertEqual(len(received), len(PAYLOAD + LAST_PACKET))
        self.assertTrue(received.endswith(LAST_PACKET))
        self.wait_for_exit()
        self.assertEqual(self.proxy.exit_code, 0)

    def test_server_close_ends_proxy(self) -> None:
        self.server_sends_last_packet_and_closes()
        self.assertEqual(self.client_receives(), PAYLOAD + LAST_PACKET)
        self.wait_for_exit()
        self.assertEqual(self.proxy.exit_code, 0)

    def test_client_end_of_stream_ends_proxy(self) -> None:
        os.close(self.stdin_w)
        self.wait_for_exit()
        self.assertEqual(self.proxy.exit_code, 0)
        # (the real proxy process exits here, which releases the server socket)
        self.assertEqual(self.exits[:1], [0])

    def test_broken_client_pipe_still_ends_proxy(self) -> None:
        # (whether the loop or the SIGPIPE handler gets there first)
        os.close(self.stdout_r)
        self.server_end.sendall(b"server-data")
        self.wait_for_exit()
        self.assertIn(self.proxy.exit_code, (0, 128 + signal.SIGPIPE))
        self.assertTrue(self.exits)


def main():
    unittest.main()


if __name__ == '__main__':
    main()
