# Copyright (C) 2026 kogeler
"""Reparenting and zombie state must not hide an owned child leak."""

import tempfile
import unittest
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import peer_processes as peers


class PeerProcessesTest(unittest.TestCase):
    def test_pid_reuse_refuses_stop_and_closes_descriptor(self):
        with (patch.object(peers.os, "pidfd_open", return_value=99),
              patch.object(peers.os, "close") as close,
              patch.object(peers, "read_process", return_value={"start_ticks": "43", "state": "S"}),
              patch.object(peers.signal, "pidfd_send_signal") as send):
            with self.assertRaisesRegex(ValueError, "identity changed"):
                peers.stop_owned(42, "41", "KILL")
            send.assert_not_called()
            close.assert_called_once_with(99)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.scope = patch.object(peers, "PROC", self.root)
        self.scope.start()
        self.addCleanup(self.scope.stop)

    def process(self, pid, parent=1, ticks="100", state="S", children=""):
        directory = self.root / str(pid)
        task = directory / "task" / str(pid)
        task.mkdir(parents=True, exist_ok=True)
        (directory / "stat").write_text(f"{pid} (comm with ) spaces) {state} {parent} "
                                       + "0 " * 17 + ticks + "\n")
        (task / "children").write_text(children)

    def snapshot(self):
        self.process(10, children="11")
        self.process(11, parent=10, ticks="110", children="12")
        self.process(12, parent=11, ticks="120")
        # A child forked by a non-main thread is part of the same ownership.
        thread = self.root / "10/task/15"
        thread.mkdir()
        (thread / "children").write_text("13")
        self.process(13, parent=10, ticks="130")
        return peers.capture(10, "100")

    def test_recursive_and_non_main_thread_children_are_owned(self):
        snapshot = self.snapshot()
        self.assertEqual([item["pid"] for item in snapshot["descendants"]], [11, 13, 12])
        self.assertEqual(len(peers.remaining(snapshot)), 4)

    def test_orphaned_zombie_is_not_reaped(self):
        snapshot = self.snapshot()
        for pid in (10, 11, 13):
            (self.root / str(pid) / "stat").unlink()
        self.process(12, parent=1, ticks="120", state="Z")
        self.assertEqual(peers.remaining(snapshot), [
            {"pid": 12, "parent_pid": 1, "start_ticks": "120", "comm": "comm with ) spaces", "state": "Z"},
        ])
        self.assertEqual(peers.blocking_remaining(snapshot, peers.remaining(snapshot)), [])
        (self.root / "12/stat").unlink()
        self.assertEqual(peers.remaining(snapshot), [])

    def test_orphaned_direct_child_still_requires_the_peers_wait(self):
        snapshot = self.snapshot()
        for pid in (10, 12, 13):
            (self.root / str(pid) / "stat").unlink()
        self.process(11, parent=1, ticks="110", state="Z")
        self.assertEqual([item["pid"] for item in peers.blocking_remaining(snapshot, peers.remaining(snapshot))], [11])

    def test_live_grandchild_is_not_an_init_zombie_exception(self):
        snapshot = self.snapshot()
        for pid in (10, 11, 13):
            (self.root / str(pid) / "stat").unlink()
        self.process(12, parent=1, ticks="120", state="S")
        self.assertEqual([item["pid"] for item in peers.blocking_remaining(snapshot, peers.remaining(snapshot))], [12])

    def test_pid_reuse_is_not_a_successful_reaping_observation(self):
        snapshot = self.snapshot()
        self.process(12, parent=1, ticks="999")
        with self.assertRaisesRegex(ValueError, "reused"):
            peers.remaining(snapshot)

    def test_wrong_root_or_changed_ancestry_fails_closed(self):
        self.process(10, children="11")
        self.process(11, parent=1)
        with self.assertRaisesRegex(ValueError, "ancestry"):
            peers.capture(10, "100")
        with self.assertRaisesRegex(ValueError, "exited or changed"):
            peers.capture(10, "999")

    def test_real_exited_child_is_not_reaped_until_wait(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            with patch.object(peers, "PROC", Path("/proc")):
                original = peers.read_process(child.pid)
                snapshot = peers.capture(child.pid, original["start_ticks"])
                child.terminate()
                os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
                self.assertEqual(peers.remaining(snapshot)[0]["state"], "Z")
                self.assertTrue(peers.blocking_remaining(snapshot, peers.remaining(snapshot)))
                child.wait(timeout=5)
                self.assertEqual(peers.remaining(snapshot), [])
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)

    def test_owned_stop_terminates_a_real_child_not_an_arbitrary_pid(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            with patch.object(peers, "PROC", Path("/proc")):
                identity = peers.read_process(child.pid)
                peers.stop_owned(child.pid, identity["start_ticks"], "TERM")
                self.assertEqual(child.wait(timeout=5), -15)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
