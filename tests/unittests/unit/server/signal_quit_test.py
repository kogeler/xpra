#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 kogeler
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

"""
A termination signal must not log from the Python signal handler.

The handler runs between bytecodes of whatever the main thread was doing,
possibly inside a write to the very stream the server logs to; logging from
there re-enters that buffered writer ("reentrant call inside
<_io.BufferedWriter ...>") and the logging module reports a
"--- Logging error ---" instead of the message.
"""

import os
import subprocess
import sys
import unittest

PROBE = r"""
import io
import logging
import os
import signal
from types import SimpleNamespace

from xpra.log import SIGPIPEStreamHandler
import xpra.server.core as core
from xpra.server.core import ServerCore

queued = []
core.GLib = SimpleNamespace(idle_add=lambda *args: queued.append(args))


class SignallingRaw(io.RawIOBase):
    # deliver SIGTERM while the buffered writer above holds its lock
    fired = False

    def writable(self):
        return True

    def write(self, data):
        if not SignallingRaw.fired:
            SignallingRaw.fired = True
            os.kill(os.getpid(), signal.SIGTERM)
        return len(data)


stream = io.TextIOWrapper(io.BufferedWriter(SignallingRaw(), buffer_size=16), write_through=True)
root = logging.getLogger()
root.handlers[:] = [SIGPIPEStreamHandler(stream)]
root.setLevel(logging.INFO)

server = ServerCore.__new__(ServerCore)
server._closing = False
server._exit_mode = core.ServerExitMode.UNSET
server.session_type = "probe"
server.install_signal_handlers(server.signal_quit)
logging.getLogger("probe").info("x" * 256)
assert SignallingRaw.fired, "SIGTERM was not delivered during the write"
names = [getattr(args[0], "__name__", repr(args[0])) for args in queued]
assert names == ["clean_quit", "exit"], names
assert queued[1][1:] == (128 + signal.SIGTERM, ), queued[1]
print("signal quit deferred", flush=True)
"""


class SignalQuitTest(unittest.TestCase):

    def test_termination_signal_during_log_write(self):
        env = dict(os.environ)
        env.pop("XPRA_DEBUG_MODULES", None)
        proc = subprocess.run([sys.executable, "-c", PROBE], env=env, capture_output=True,
                              text=True, timeout=60, check=False)
        output = proc.stdout + proc.stderr
        self.assertNotIn("--- Logging error ---", proc.stderr, output[-4000:])
        self.assertNotIn("reentrant call", proc.stderr, output[-4000:])
        self.assertEqual(proc.returncode, 0, output[-4000:])
        self.assertIn("signal quit deferred", proc.stdout, output[-4000:])


def main():
    unittest.main()


if __name__ == "__main__":
    main()
