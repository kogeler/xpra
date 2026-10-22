#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 kogeler
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

"""Close the real exec-SSH transport under the real Linux parse filter."""

import os
import signal
from select import select
from subprocess import TimeoutExpired
import sys
from threading import Thread, get_native_id
import unittest

from xpra.net.ssh.exec_client import connect_to
from xpra.seccomp import install_filter, is_available, thread_state
from xpra.seccomp.parse import PARSE_SYSCALLS


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux parse seccomp boundary")
class SshTunnelSeccompTest(unittest.TestCase):
    def setUp(self):
        self.assertTrue(is_available(), "the native seccomp subject is unavailable")

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
        attempts = []
        terminate = child.terminate

        def observed_terminate():
            attempts.append((get_native_id(), thread_state.filtered))
            terminate()

        child.terminate = observed_terminate

        def cleanup():
            connection.close()
            if child.poll() is None:
                child.terminate()
            try:
                child.wait(timeout=2)
            except TimeoutExpired:
                child.kill()
                child.wait(timeout=2)

        self.addCleanup(cleanup)
        if stubborn:
            self.assertEqual(select((child.stdout,), (), (), 2)[0], [child.stdout])
            self.assertEqual(child.stdout.readline(), b"ready\n")
        if exited:
            self.assertEqual(child.wait(timeout=2), 0)
        return connection, child, attempts

    def filtered_close(self, connection, action):
        errors = []
        owner = []

        def close():
            try:
                install_filter(PARSE_SYSCALLS, action)
                owner.append(get_native_id())
                connection.close()
                connection.close()  # the process callback must still run once
            except BaseException as exc:
                errors.append(exc)

        thread = Thread(target=close, name="fixture-parse-close", daemon=True)
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive(), "filtered transport close did not finish")
        if errors:
            raise errors[0]
        self.assertEqual(len(owner), 1)
        return owner[0]

    def check_termination(self, action):
        connection, child, attempts = self.connection()
        caller = self.filtered_close(connection, action)
        # The transport owner must collect its child before returning. Calling
        # wait() here would repair an otherwise leaked zombie in the test.
        status = child.returncode
        self.assertIsNotNone(status, "owned tunnel close did not collect its child")
        self.assertEqual(status, -signal.SIGTERM)
        self.assertEqual(len(attempts), 1)
        if action == "errno":
            self.assertNotEqual(attempts[0][0], caller)
            self.assertFalse(attempts[0][1], "process cleanup ran on a filtered owner")

    def test_errno_close_terminates_on_unfiltered_owner_without_ui_loop(self):
        self.check_termination("errno")

    def test_log_action_control(self):
        self.check_termination("log")

    def test_sigterm_resistant_owned_child_is_killed_and_collected(self):
        connection, child, attempts = self.connection(stubborn=True)
        caller = self.filtered_close(connection, "errno")
        self.assertEqual(child.returncode, -signal.SIGKILL)
        self.assertEqual(len(attempts), 1)
        self.assertNotEqual(attempts[0][0], caller)
        self.assertFalse(attempts[0][1])

    def test_shared_tunnel_is_not_terminated(self):
        connection, child, attempts = self.connection(exit_ssh=False)
        self.filtered_close(connection, "errno")
        self.assertIsNone(child.poll())
        self.assertEqual(attempts, [])

    def test_already_exited_tunnel_is_not_signalled(self):
        connection, child, attempts = self.connection(exited=True)
        self.filtered_close(connection, "errno")
        self.assertEqual(child.returncode, 0)
        self.assertEqual(attempts, [])


if __name__ == "__main__":
    unittest.main()
