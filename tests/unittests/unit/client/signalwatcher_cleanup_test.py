#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 kogeler
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

"""Complete the real signal watcher owner's pipe and process cleanup."""

import os
from select import select
import signal
from subprocess import Popen, PIPE
import sys
import unittest

from xpra.client.subsystem.window.signalwatcher import GLib, clean_signalwatcher, kill_signalwatcher


@unittest.skipUnless(os.name == "posix", "POSIX signal watcher child lifetime")
class SignalWatcherCleanupTest(unittest.TestCase):
    def child(self, *, exited=False, closed_reader=False):
        command = "raise SystemExit(0)"
        if closed_reader:
            command = (
                "import os, signal, time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "os.close(0); print('ready', flush=True); time.sleep(30)"
            )
        elif not exited:
            command = "import sys; print('ready', flush=True); sys.stdin.readline()"
        proc = Popen([sys.executable, "-c", command], stdin=PIPE, stdout=PIPE, stderr=PIPE)
        proc.stdout_io_watch = 0

        def cleanup():
            if proc.stdout_io_watch:
                GLib.source_remove(proc.stdout_io_watch)
                proc.stdout_io_watch = 0
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=2)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream and not stream.closed:
                    try:
                        stream.close()
                    except BrokenPipeError:
                        pass

        self.addCleanup(cleanup)
        if exited:
            self.assertEqual(proc.wait(timeout=2), 0)
        else:
            self.assertEqual(select((proc.stdout,), (), (), 2)[0], [proc.stdout])
            self.assertEqual(proc.stdout.readline(), b"ready\n")
        return proc

    def assert_pipes_closed(self, proc):
        for name in ("stdin", "stdout", "stderr"):
            self.assertTrue(getattr(proc, name).closed, name)

    def test_already_exited_child_closes_all_pipes(self):
        proc = self.child(exited=True)
        kill_signalwatcher(proc)
        self.assert_pipes_closed(proc)

    def test_reaper_callback_cleanup_closes_all_pipes(self):
        proc = self.child(exited=True)
        clean_signalwatcher(proc)
        self.assert_pipes_closed(proc)

    def test_broken_reader_still_closes_stdin(self):
        proc = self.child(closed_reader=True)
        kill_signalwatcher(proc)
        self.assert_pipes_closed(proc)

    def test_forced_child_status_collected_before_return(self):
        proc = self.child(closed_reader=True)
        kill_signalwatcher(proc)
        # No test-side wait/poll may supply the owner's missing collection.
        self.assertEqual(proc.returncode, -signal.SIGKILL)

    def test_running_child_with_already_closed_stdin_is_still_collected(self):
        proc = self.child(closed_reader=True)
        proc.stdin.close()
        kill_signalwatcher(proc)
        self.assertEqual(proc.returncode, -signal.SIGKILL)
        self.assert_pipes_closed(proc)

    def test_normal_child_cleanup_is_complete_and_repeatable(self):
        proc = self.child()
        kill_signalwatcher(proc)
        self.assertIsNotNone(proc.returncode)
        self.assert_pipes_closed(proc)
        kill_signalwatcher(proc)
        self.assert_pipes_closed(proc)

    def test_cleanup_removes_the_real_glib_io_source(self):
        proc = self.child()
        source = GLib.io_add_watch(proc.stdout, GLib.PRIORITY_DEFAULT,
                                   GLib.IOCondition.IN | GLib.IOCondition.HUP,
                                   lambda *_args: False)
        proc.stdout_io_watch = source
        self.assertIsNotNone(GLib.MainContext.default().find_source_by_id(source))
        kill_signalwatcher(proc)
        self.assertEqual(proc.stdout_io_watch, 0)
        self.assertIsNone(GLib.MainContext.default().find_source_by_id(source))


if __name__ == "__main__":
    unittest.main()
