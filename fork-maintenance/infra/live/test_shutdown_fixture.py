# Copyright (C) 2026 kogeler
"""Real group-signal ownership, exit receipts and teardown-oracle counterfactuals."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import runtime_audit
import shutdown_fixture as fixture


ABORT_REPORT = """Fatal Python error: Aborted

Current thread 0x00007f09d83ec200 [python3] (most recent call first):
  File "/usr/lib/python3/dist-packages/xpra/server/glib_server.py", line 42 in do_run
  File "/usr/bin/xpra", line 21 in <module>

Current thread's C stack trace (most recent call first):
  Binary file "python3", at _Py_DumpStack+0x4a [0x47728a]
  Binary file "/usr/lib/x86_64-linux-gnu/libc.so.6", at ppoll+0x46 [0x7f09d8515136]
"""


def group_evidence(application, server_pid, order="together", root=None):
    """Synthetic collector input, never a claim that live teardown ran."""
    pgid = 70
    members = [dict(pid=pid, parent_pid=server_pid if index == 4 else 67,
                    start_ticks=application["start_ticks"] if index == 4 else "123456",
                    comm="fixture", pgid=pgid)
               for index, pid in enumerate((pgid, server_pid, 71, 72, application["pid"]))]
    files = {"sessions": ["wayland-0"], "sockets": ["host-wayland-0"]}
    ready = {"schema": 2, "pgid": pgid, "order": order, "members": members,
             "names": sorted(fixture.REQUIRED_NAMES), "application": application,
             "bus_owners": {"org.freedesktop.Notifications": server_pid,
                            "org.freedesktop.impl.portal.desktop.gtk": 71,
                            "org.freedesktop.portal.Desktop": 72},
             "session_files": files}
    events = fixture.expected_events(order, "heartbeat-eof")
    aborted = order == fixture.ABORT_CONTROL
    native = ({"origin": "signal", "signal": 6, "status": 134} if aborted else {"origin": "exit", "status": 143})
    result = {"schema": 2, "pgid": pgid, "order": order, "members": members,
              "reason": "heartbeat-eof", "events": [{"event": name, "time_ns": index * 100}
                                                       for index, name in enumerate(events, 1)],
              "statuses": {"private-bus": 0, "portal": -15, "portal-gtk": -15},
              "server_exit": native,
              "remaining_members": [dict(members[4], state="Z", parent_pid=1)] if aborted else [],
              "session_files": {"ready": files, "after": files if aborted else {"sessions": [], "sockets": []}}}
    lease = {"before": {"schema": 1, "root": dict(pid=80, parent_pid=1, start_ticks="123456", comm="python3"),
                        "descendants": []}, "after": {"remaining": []}}
    evidence = {"ready": ready, "result": result, "native_exit": native,
                "lease_exit_status": 255, "lease_processes": lease}
    if root is not None:
        records = {"server.shutdown-ready.json": json.dumps(ready),
                   "server.shutdown-result.json": json.dumps(result), "server.native-exit.json": json.dumps(native),
                   "client.shutdown-lease.pid": "80\n", "client.shutdown-lease.exit": "255\n",
                   "server.shutdown-supervisor.stderr": "",
                   **{f"client.shutdown-lease.processes-{phase}.json": json.dumps(value) for phase, value in lease.items()}}
        for role in ("client.shutdown-lease", "server.private-bus", "server.portal", "server.portal-gtk"):
            records[f"{role}.stdout"] = ""
            records[f"{role}.stderr"] = ""
        for name, body in records.items():
            path = root / name
            path.write_text(body)
            path.chmod(0o600)
        if aborted:
            # The control's fault-handler report follows the server's own log.
            with (root / "server.stderr").open("a", encoding="utf-8") as stream:
                stream.write(ABORT_REPORT)
            (root / "server.stderr").chmod(0o600)
        (root / "server.exit").write_text(f"{native['status']}\n")
        (root / "server.exit").chmod(0o600)
    return evidence


class ShutdownFixtureTest(unittest.TestCase):
    def test_failure_diagnostic_is_bounded_and_keeps_helper_output_and_process_state(self):
        with tempfile.TemporaryDirectory() as raw, patch.object(fixture, "ROOT", Path(raw)):
            path = Path(raw) / "server.stderr"
            path.write_bytes(b"x" * 70000 + b"last diagnostic\n")
            (Path(raw) / "server.portal.stdout").write_text("portal verbose output\n")
            (Path(raw) / "server.shutdown-supervisor.stderr").write_text("Traceback: supervisor\n")
            (Path(raw) / "server.portal.stderr").symlink_to(path)
            # the table reads the container's /proc: never the busy host's
            proc = Path(raw) / "proc"
            for pid, pgid in ((os.getpid(), os.getpgrp()), (1, 1)):
                (proc / str(pid)).mkdir(parents=True)
                (proc / str(pid) / "stat").write_text(f"{pid} (a) b) S 1 {pgid} {pgid} " + "0 " * 15 + "4242\n")
            (proc / "self").mkdir()
            (proc / "7").mkdir()
            (proc / "7" / "stat").write_text("7 (short) S 1\n")
            with patch.object(fixture.peer_processes, "PROC", proc):
                observed = fixture.diagnose()
            files = observed["files"]
            self.assertEqual(set(files), {"server.stderr", "server.portal.stdout", "server.shutdown-supervisor.stderr"})
            self.assertEqual(files["server.stderr"]["size"], 70016)
            self.assertEqual(len(files["server.stderr"]["suffix"]), 65536)
            self.assertTrue(files["server.stderr"]["suffix"].endswith("last diagnostic\n"))
            self.assertEqual([value["pid"] for value in observed["processes"]], sorted((1, os.getpid()), key=str))
            own = [value for value in observed["processes"] if value["pid"] == os.getpid()]
            self.assertEqual(len(own), 1)
            self.assertEqual(own[0], {"pid": os.getpid(), "comm": "a) b", "state": "S", "parent_pid": 1,
                                      "pgid": os.getpgrp(), "start_ticks": "4242"})

    def group_child(self):
        child = subprocess.Popen([sys.executable, "-u", "-c",
                                  "import signal; print('ready'); signal.pause()"],
                                 stdout=subprocess.PIPE, text=True, process_group=0)
        self.addCleanup(child.stdout.close)
        self.addCleanup(child.wait, 5)
        self.addCleanup(lambda: child.kill() if child.poll() is None else None)
        self.assertEqual(child.stdout.readline(), "ready\n")
        return child

    def test_signals_exact_owned_group_not_supervisor(self):
        child = self.group_child()
        member = fixture.identity(child.pid)
        self.assertEqual(member["pgid"], child.pid)
        self.assertNotEqual(member["pgid"], os.getpgrp())
        fixture.verified_group_signal(child.pid, [member], signal.SIGTERM)
        self.assertEqual(child.wait(5), -signal.SIGTERM)

    def test_changed_birth_or_group_and_parent_group_are_never_signalled(self):
        child = self.group_child()
        member = fixture.identity(child.pid)
        with patch.object(fixture.os, "killpg") as kill:
            for pgid, members in ((child.pid, [{**member, "start_ticks": "1"}]),
                                  (os.getpgrp(), [member]), (1, [member]),
                                  (child.pid + 1, [member])):
                with self.subTest(pgid=pgid), self.assertRaises(ValueError):
                    fixture.verified_group_signal(pgid, members, signal.SIGTERM)
            kill.assert_not_called()
        self.assertIsNone(child.poll())

    def test_exited_members_are_not_signalled_and_zombies_are_not_live(self):
        child = self.group_child()
        member = fixture.identity(child.pid)
        os.kill(child.pid, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while fixture.peer_processes.read_process(child.pid)["state"] != "Z":
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        # Unreaped: the PID cannot have been reused, yet nothing may be signalled.
        self.assertEqual(fixture.live_members(child.pid, [member]), [])
        with patch.object(fixture.os, "killpg") as kill:
            fixture.verified_group_signal(child.pid, [member], signal.SIGKILL)
        kill.assert_not_called()
        # A zombie can no longer write evidence: it does not block quiescence.
        self.assertNotIn(child.pid, [value["pid"] for value in fixture.quiescent(os.getpid())["remaining"]])
        self.assertNotIn(os.getpid(), [value["pid"] for value in fixture.quiescent(os.getpid())["remaining"]])

    def test_only_term_and_kill_are_group_operations(self):
        with patch.object(fixture.os, "killpg") as kill:
            with self.assertRaises(ValueError):
                fixture.verified_group_signal(os.getpid() + 100, [], signal.SIGINT)
            kill.assert_not_called()

    def test_parent_wait_status_never_turns_an_abort_into_an_exit(self):
        for expression, expected in (("raise SystemExit(143)", {"origin": "exit", "status": 143}),
                                     ("import os; os.abort()", {"origin": "signal", "signal": 6, "status": 134}),
                                     ("import os, signal; os.kill(os.getpid(), signal.SIGSEGV)",
                                      {"origin": "signal", "signal": 11, "status": 139})):
            with self.subTest(expression=expression):
                child = subprocess.run([sys.executable, "-c", expression], capture_output=True, check=False)
                self.assertEqual(fixture.exit_receipt(child.returncode), expected)

    def test_private_bus_never_uses_inherited_activation_directories(self):
        configuration = fixture.private_bus_configuration()
        self.assertIn(str(fixture.STATE / "bus"), configuration)
        self.assertNotIn("servicedir", configuration)
        self.assertNotIn("include", configuration)

    def test_bus_inventory_and_owner_reject_malformed_replies(self):
        with patch.object(fixture, "bus_call", return_value=(["org.freedesktop.DBus", ":1.3"],)):
            self.assertEqual(fixture.bus_names("unix:path=/x"), {"org.freedesktop.DBus", ":1.3"})
        with patch.object(fixture, "bus_call", return_value=([b"bytes"],)), self.assertRaises(ValueError):
            fixture.bus_names("unix:path=/x")
        for value in ((0,), (1,), ("12",)):
            with self.subTest(value=value), patch.dict(sys.modules, {"gi": Mock(), "gi.repository": Mock()}), patch.object(
                    fixture, "bus_call", return_value=value), self.assertRaises(ValueError):
                fixture.bus_owner("unix:path=/x", "org.freedesktop.Notifications")

    def test_session_files_are_observed_never_removed(self):
        with tempfile.TemporaryDirectory() as raw:
            sessions, sockets = Path(raw, "sessions"), Path(raw, "sockets")
            (sessions / "wayland-0").mkdir(parents=True)
            sockets.mkdir()
            (sockets / "host-wayland-0").touch()
            command = ["xpra", "seamless", f"--sessions-dir={sessions}", f"--socket-dir={sockets}",
                       f"--socket-dirs={sockets}"]
            self.assertEqual(fixture.session_entries(command),
                             {"sessions": ["wayland-0"], "sockets": ["host-wayland-0"]})
            self.assertTrue((sessions / "wayland-0").is_dir())
            for invalid in (command[:2], [*command, f"--sessions-dir={sockets}"], ["--socket-dir=relative"]):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    fixture.session_entries(invalid)

    def test_session_cleanup_is_required_and_an_abort_cannot_be_concealed(self):
        ready = {"sessions": ["wayland-0"], "sockets": ["host-wayland-0"]}
        empty = {"sessions": [], "sockets": []}
        for order in fixture.ORDERS:
            with self.subTest(order=order):
                self.assertTrue(fixture.session_cleanup_valid(order, {"ready": ready, "after": empty}))
                self.assertFalse(fixture.session_cleanup_valid(order, {"ready": ready, "after": ready}))
                self.assertFalse(fixture.session_cleanup_valid(order, {"ready": empty, "after": empty}))
        control = fixture.ABORT_CONTROL
        self.assertTrue(fixture.session_cleanup_valid(control, {"ready": ready, "after": ready}))
        self.assertFalse(fixture.session_cleanup_valid(control, {"ready": ready, "after": empty}))

    def test_helper_statuses_allow_only_dependency_driven_exits(self):
        normal = {"private-bus": 0, "portal": -15, "portal-gtk": -15}
        for order in fixture.SCENARIO_ORDERS:
            with self.subTest(order=order):
                self.assertTrue(fixture.helper_statuses_valid(order, normal))
                for role in fixture.HELPER_ROLES:
                    for forbidden in (-signal.SIGKILL, -signal.SIGABRT, -signal.SIGSEGV, 2):
                        self.assertFalse(fixture.helper_statuses_valid(order, {**normal, role: forbidden}))
                self.assertFalse(fixture.helper_statuses_valid(order, {**normal, "server": 0}))
                self.assertFalse(fixture.helper_statuses_valid(order, {**normal, "portal": True}))
        self.assertFalse(fixture.helper_statuses_valid("together", {**normal, "portal-gtk": 1}))
        self.assertTrue(fixture.helper_statuses_valid("server-first", {**normal, "portal-gtk": 1}))
        self.assertFalse(fixture.helper_statuses_valid("server-first", {**normal, "portal": 1}))
        self.assertTrue(fixture.helper_statuses_valid("bus-first", {**normal, "portal": 0, "portal-gtk": 1}))

    def test_server_exit_receipt_is_exact_per_order(self):
        exit_143 = {"origin": "exit", "status": 143}
        aborted = {"origin": "signal", "signal": 6, "status": 134}
        for order in fixture.ORDERS:
            with self.subTest(order=order):
                self.assertTrue(fixture.server_exit_valid(order, exit_143))
                for invalid in (aborted, {"origin": "exit", "status": 1}, {"origin": "exit", "status": True},
                                {"origin": "signal", "signal": 15, "status": 143}, {"origin": "exit", "status": 134}):
                    self.assertFalse(fixture.server_exit_valid(order, invalid))
        self.assertTrue(fixture.server_exit_valid("bus-first", {"origin": "exit", "status": 0}))
        self.assertFalse(fixture.server_exit_valid("together", {"origin": "exit", "status": 0}))
        self.assertTrue(fixture.server_exit_valid(fixture.ABORT_CONTROL, aborted))
        self.assertFalse(fixture.server_exit_valid(fixture.ABORT_CONTROL, exit_143))

    def test_heartbeat_channel_is_waited_and_stdin_closed_after_mux_loss(self):
        child = Mock()
        child.__enter__ = Mock(return_value=child)
        child.__exit__ = Mock()
        child.poll.return_value = None
        child.stdin.write.side_effect = BrokenPipeError()
        child.wait.return_value = 255
        with patch.object(fixture.subprocess, "Popen", return_value=child):
            self.assertEqual(fixture.heartbeat(["ssh", "owned-channel"]), 255)
        child.stdin.close.assert_called_once()
        child.wait.assert_called_once_with(timeout=15)

    def test_every_order_and_the_control_have_independent_oracles(self):
        application = {"pid": 41, "start_ticks": "919"}
        for order in fixture.SCENARIO_ORDERS:
            with self.subTest(order=order):
                evidence = group_evidence(application, 8, order)
                checks = fixture.evidence_checks(evidence, application=application, server_pid=8)
                self.assertTrue(all(checks.values()), checks)
                broken = group_evidence(application, 8, order)
                if order == fixture.ABORT_CONTROL:
                    broken["native_exit"] = broken["result"]["server_exit"] = {"origin": "exit", "status": 143}
                else:
                    # The incident: an abort during an otherwise exact teardown.
                    broken["native_exit"] = broken["result"]["server_exit"] = {"origin": "signal", "signal": 6,
                                                                               "status": 134}
                self.assertFalse(fixture.evidence_checks(broken, application=application,
                                                         server_pid=8)["server_exit_receipt_exact"])
                broken = group_evidence(application, 8, order)
                broken["result"]["server_exit"] = {"origin": "exit", "status": 0}
                self.assertFalse(fixture.evidence_checks(broken, application=application,
                                                         server_pid=8)["server_exit_receipt_exact"])
                broken = group_evidence(application, 8, order)
                broken["result"]["remaining_members"] = [dict(broken["ready"]["members"][2], state="S")]
                self.assertFalse(fixture.evidence_checks(broken, application=application,
                                                         server_pid=8)["private_group_members_gone"])
                broken = group_evidence(application, 8, order)
                broken["result"]["events"].insert(-1, {"event": "group-SIGKILL", "time_ns": 250})
                self.assertFalse(fixture.evidence_checks(broken, application=application,
                                                         server_pid=8)["shutdown_order_exact"])
                broken = group_evidence(application, 8, order)
                broken["ready"]["members"][4]["pgid"] = 800
                self.assertFalse(fixture.evidence_checks(broken, application=application,
                                                         server_pid=8)["owned_private_group_topology"])
                broken = group_evidence(application, 8, order)
                broken["ready"]["schema"] = 1
                self.assertFalse(fixture.evidence_checks(broken, application=application,
                                                         server_pid=8)["private_shutdown_receipts"])

    def test_real_fault_handler_report_is_the_only_expected_abort_failure(self):
        with tempfile.TemporaryDirectory() as raw:
            script = Path(raw, "xpra", "server", "abort_probe.py")
            script.parent.mkdir(parents=True)
            script.write_text("import os\nos.abort()\n")
            child = subprocess.run([sys.executable, "-X", "faulthandler", str(script)],
                                   capture_output=True, check=False)
        self.assertEqual(child.returncode, -signal.SIGABRT)
        # CPython 3.14 adds the aborting thread's native stack to its report.
        self.assertTrue(fixture.abort_diagnostic_valid(child.stderr), child.stderr.decode(errors="replace"))
        control = f"ssh-group-{fixture.ABORT_CONTROL}-01"
        self.assertEqual(fixture.unexpected_failures(control, "server.stderr", child.stderr, runtime_audit.failures), ())
        for scenario, filename in ((control, "client.stderr"), ("ssh-group-together-01", "server.stderr"),
                                   ("transport-loss", "server.stderr")):
            with self.subTest(scenario=scenario, filename=filename):
                self.assertEqual(fixture.unexpected_failures(scenario, filename, child.stderr, runtime_audit.failures),
                                 ("fatal-python-error",))
        for payload in (child.stderr + b"corrupted double-linked list\n",
                        child.stderr + b"Fatal Python error: Segmentation fault\n",
                        child.stderr.replace(b"Current thread's C stack trace", b"C stack"),
                        b"Fatal Python error: Aborted\n"):
            with self.subTest(payload=payload[-40:]):
                self.assertNotEqual(fixture.unexpected_failures(control, "server.stderr", payload,
                                                                runtime_audit.failures), ())


if __name__ == "__main__":
    unittest.main()
