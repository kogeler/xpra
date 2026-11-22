#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 kogeler
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

"""Close the real exec-SSH transport and check that it collects its own child."""

import os
import signal
from select import select
from subprocess import TimeoutExpired
import sys
import unittest

from xpra.net.ssh.exec_client import connect_to


@unittest.skipUnless(os.name == "posix", "POSIX child process boundary")
class SshTunnelCloseTest(unittest.TestCase):

    def connection(self, *, exit_ssh=True, exited=False, stubborn=False):
        # Python replaces only the SSH executable: connect_to still constructs
        # the command, owns a real Popen and pipes, and installs its real callback.
        # The sleeping child ignores stdin EOF, so closing pipes cannot fake
        # successful termination. No network, server, keys or credentials.
        command = "raise SystemExit(0)" if exited else "import time; time.sleep(30)"
        if stubborn:
            command = ("import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                       "print('ready', flush=True); time.sleep(30)")
        connection = connect_to({
            "full_ssh": [sys.executable, "-c", command],
            "remote_xpra": ["xpra"], "proxy_command": ["_proxy"], "display_as_args": [],
            "host": "fixture", "exit_ssh": exit_ssh, "env": os.environ.copy(),
        })
        child = connection.process[0]
        terminated = []
        terminate = child.terminate

        def observed_terminate():
            terminated.append(True)
            terminate()

        child.terminate = observed_terminate

        def cleanup():
            connection.close()
            if child.poll() is None:
                child.kill()
            try:
                child.wait(timeout=2)
            except TimeoutExpired:
                pass

        self.addCleanup(cleanup)
        if stubborn:
            self.assertEqual(select((child.stdout,), (), (), 2)[0], [child.stdout])
            self.assertEqual(child.stdout.readline(), b"ready\n")
        if exited:
            self.assertEqual(child.wait(timeout=2), 0)
        return connection, child, terminated

    def test_close_collects_the_owned_child(self):
        connection, child, terminated = self.connection()
        connection.close()
        connection.close()  # the process callback must still run once
        # The transport owner must collect its child before returning,
        # calling wait() here would repair an otherwise leaked zombie:
        self.assertEqual(child.returncode, -signal.SIGTERM)
        self.assertEqual(terminated, [True])

    def test_sigterm_resistant_owned_child_is_left_to_the_reaper(self):
        # Upstream reaps a slow owned tunnel by normal child reaper polling
        # and never kills it (`test_owned_tunnel_reaped_by_normal_polling`):
        connection, child, terminated = self.connection(stubborn=True)
        connection.close()
        self.assertIsNone(child.poll())
        self.assertEqual(terminated, [True])

    def test_shared_tunnel_is_not_terminated(self):
        connection, child, terminated = self.connection(exit_ssh=False)
        connection.close()
        self.assertIsNone(child.poll())
        self.assertEqual(terminated, [])

    def test_already_exited_tunnel_is_not_signalled(self):
        connection, child, terminated = self.connection(exited=True)
        connection.close()
        self.assertEqual(child.returncode, 0)
        self.assertEqual(terminated, [])


if __name__ == "__main__":
    unittest.main()
