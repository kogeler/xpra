#!/usr/bin/env python3
# Copyright (C) 2026 kogeler
"""Tests for the system-manager-free background process supervisor."""

from __future__ import annotations

import argparse
import errno
import fcntl
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import background_job


class BackgroundJobTests(unittest.TestCase):
    def paths(self, root: Path) -> tuple[Path, Path, Path]:
        return root / "owner.json", root / "runtime.log", root / "completion.json"

    def test_anonymous_publication_has_no_named_crash_debris(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            root.chmod(0o700)
            destination = root / "published"
            script = "\n".join(
                (
                    "import sys, time",
                    "from pathlib import Path",
                    f"sys.path.insert(0, {str(Path(background_job.__file__).parent)!r})",
                    "import background_job",
                    "def pause(*_args):",
                    "    print('anonymous-ready', flush=True)",
                    "    time.sleep(60)",
                    "background_job._link_anonymous_file = pause",
                    "background_job.publish_bytes(Path(sys.argv[1]), b'private')",
                )
            )
            process = subprocess.Popen(
                [sys.executable, "-c", script, str(destination)],
                stdout=subprocess.PIPE,
                text=True,
            )
            try:
                assert process.stdout is not None
                self.assertEqual(process.stdout.readline().strip(), "anonymous-ready")
                process.kill()
                process.wait()
                self.assertEqual(tuple(root.iterdir()), ())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                if process.stdout is not None:
                    process.stdout.close()

    def test_anonymous_publication_is_private_and_no_replace(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            root.chmod(0o700)
            destination = root / "published"
            background_job.publish_bytes(destination, b"first")
            self.assertEqual(destination.read_bytes(), b"first")
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            with self.assertRaisesRegex(
                background_job.BackgroundJobError,
                "refusing to overwrite",
            ):
                background_job.publish_bytes(destination, b"second")
            self.assertEqual(destination.read_bytes(), b"first")
            self.assertEqual(tuple(root.iterdir()), (destination,))

    def test_anonymous_publication_exception_leaves_no_staging_name(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            root.chmod(0o700)
            with (
                patch.object(
                    background_job,
                    "_link_anonymous_file",
                    side_effect=OSError("injected publication failure"),
                ),
                self.assertRaisesRegex(OSError, "injected publication failure"),
            ):
                background_job.publish_bytes(root / "published", b"payload")
            self.assertEqual(tuple(root.iterdir()), ())

    def test_unsupported_anonymous_files_leave_no_staging_name(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            root.chmod(0o700)
            real_open = os.open

            def reject_tmpfile(
                path: object,
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                if flags & os.O_TMPFILE:
                    raise OSError(errno.EOPNOTSUPP, "O_TMPFILE unsupported")
                return real_open(path, flags, mode, dir_fd=dir_fd)

            with (
                patch.object(background_job.os, "open", side_effect=reject_tmpfile),
                self.assertRaises(OSError),
            ):
                background_job.publish_bytes(root / "published", b"payload")
            self.assertEqual(tuple(root.iterdir()), ())

    def test_release_gate_eof_never_starts_an_unowned_payload(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            root.chmod(0o700)
            read_gate, write_gate = os.pipe()
            os.close(write_gate)
            marker = root / "payload-ran"
            args = argparse.Namespace(
                argv=[sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"],
                completion=root / "completion.json",
                cwd=root,
                gate_fd=read_gate,
            )
            with patch.object(background_job.sys, "stderr", io.StringIO()):
                self.assertEqual(background_job._run(args), 125)
            self.assertFalse(marker.exists())
            self.assertFalse(args.completion.exists())

    def test_failed_launch_retains_published_owner_while_group_survives(self) -> None:
        class FakeProcess:
            pid = 4242

            def poll(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            root.chmod(0o700)
            owner, log, completion = self.paths(root)
            real_write = os.write

            def fail_release_gate(descriptor: int, payload: bytes) -> int:
                if payload == b"1":
                    raise OSError("synthetic gate failure")
                return real_write(descriptor, payload)

            with (
                patch.object(background_job.subprocess, "Popen", return_value=FakeProcess()),
                patch.object(
                    background_job,
                    "process_identity",
                    return_value=("S", "4242", "42"),
                ),
                patch.object(background_job, "_stop_failed_launch", return_value=False),
                patch.object(background_job.os, "write", side_effect=fail_release_gate),
                self.assertRaisesRegex(
                    background_job.BackgroundJobError,
                    "runtime ownership was retained",
                ),
            ):
                background_job.launch(
                    owner_path=owner,
                    runtime_log=log,
                    completion_file=completion,
                    record={"owner": "test"},
                    argv=[sys.executable, "-c", "pass"],
                    cwd=root,
                )
            self.assertTrue(owner.is_file())
            self.assertTrue(log.is_file())
            self.assertFalse(completion.exists())

    def test_owner_publication_failure_cleans_only_after_group_is_proven_gone(self) -> None:
        class FakeProcess:
            pid = 4343

            def poll(self) -> None:
                return None

        for group_gone in (False, True):
            with self.subTest(group_gone=group_gone), tempfile.TemporaryDirectory() as name:
                root = Path(name)
                root.chmod(0o700)
                owner, log, completion = self.paths(root)
                expected_error = (
                    background_job.BackgroundJobError if not group_gone else OSError
                )
                with (
                    patch.object(
                        background_job.subprocess,
                        "Popen",
                        return_value=FakeProcess(),
                    ),
                    patch.object(
                        background_job,
                        "process_identity",
                        return_value=("S", "4343", "43"),
                    ),
                    patch.object(
                        background_job,
                        "_stop_failed_launch",
                        return_value=group_gone,
                    ),
                    patch.object(
                        background_job,
                        "publish_json",
                        side_effect=OSError("synthetic publication failure"),
                    ),
                    self.assertRaises(expected_error),
                ):
                    background_job.launch(
                        owner_path=owner,
                        runtime_log=log,
                        completion_file=completion,
                        record={"owner": "test"},
                        argv=[sys.executable, "-c", "pass"],
                        cwd=root,
                    )
                self.assertFalse(owner.exists())
                self.assertEqual(log.exists(), not group_gone)
                self.assertFalse(completion.exists())

    def test_real_process_completes_and_persists_log(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            root.chmod(0o700)
            owner, log, completion = self.paths(root)
            record = background_job.launch(
                owner_path=owner,
                runtime_log=log,
                completion_file=completion,
                record={"owner": "test"},
                argv=[sys.executable, "-c", "print('durable output')"],
                cwd=root,
            )
            result = background_job.wait_process(record)
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(log.read_text(encoding="utf-8"), "durable output\n")
            self.assertEqual(json.loads(owner.read_text())["owner"], "test")

    def test_inherited_descriptor_remains_locked_until_payload_exit(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            root.chmod(0o700)
            owner, log, completion = self.paths(root)
            lock_path = root / "cache.lock"
            lock_descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
            record = background_job.launch(
                owner_path=owner,
                runtime_log=log,
                completion_file=completion,
                record={"owner": "test"},
                argv=[sys.executable, "-c", "import time; time.sleep(0.3)"],
                cwd=root,
                pass_fds=(lock_descriptor,),
            )
            os.close(lock_descriptor)
            competitor = os.open(lock_path, os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual(background_job.wait_process(record)["exit_code"], 0)
                fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(competitor)

    def test_real_process_group_can_be_terminated(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            root.chmod(0o700)
            owner, log, completion = self.paths(root)
            record = background_job.launch(
                owner_path=owner,
                runtime_log=log,
                completion_file=completion,
                record={"owner": "test"},
                argv=[
                    sys.executable,
                    "-c",
                    (
                        "import signal,time; "
                        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                        "print('ready', flush=True); time.sleep(60)"
                    ),
                ],
                cwd=root,
            )
            for _attempt in range(100):
                if "ready" in log.read_text(encoding="utf-8"):
                    break
                time.sleep(0.01)
            self.assertIn("ready", log.read_text(encoding="utf-8"))
            self.assertEqual(background_job.process_state(record)["state"], "running")
            background_job.terminate(record, grace=0.2)
            for _attempt in range(100):
                if not background_job.verify_running_process(record):
                    break
                time.sleep(0.01)
            self.assertFalse(background_job.verify_running_process(record))

    def test_killed_supervisor_does_not_hide_live_payload(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            root.chmod(0o700)
            owner, log, completion = self.paths(root)
            record = background_job.launch(
                owner_path=owner,
                runtime_log=log,
                completion_file=completion,
                record={"owner": "test"},
                argv=[
                    sys.executable,
                    "-c",
                    (
                        "import signal,time; "
                        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                        "print('payload-ready', flush=True); time.sleep(60)"
                    ),
                ],
                cwd=root,
            )
            for _attempt in range(100):
                if "payload-ready" in log.read_text(encoding="utf-8"):
                    break
                time.sleep(0.01)
            self.assertIn("payload-ready", log.read_text(encoding="utf-8"))
            supervisor = int(record["process"]["pid"])
            os.kill(supervisor, signal.SIGKILL)
            for _attempt in range(100):
                if not background_job.verify_running_process(record):
                    break
                time.sleep(0.01)
            self.assertFalse(background_job.verify_running_process(record))
            self.assertEqual(background_job.process_state(record)["state"], "running")
            self.assertTrue(owner.exists(), "ownership must remain while payload lives")
            background_job.terminate(record, grace=0.1)
            self.assertEqual(background_job.process_state(record)["state"], "lost")
            self.assertTrue(owner.exists(), "the caller owns explicit record cleanup")

    def test_completion_waits_for_remaining_process_group_members(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            root.chmod(0o700)
            owner, log, completion = self.paths(root)
            record = background_job.launch(
                owner_path=owner,
                runtime_log=log,
                completion_file=completion,
                record={"owner": "test"},
                argv=[
                    sys.executable,
                    "-c",
                    (
                        "import subprocess,sys; "
                        "subprocess.Popen([sys.executable, '-c', "
                        "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                        "time.sleep(60)']); print('spawned', flush=True)"
                    ),
                ],
                cwd=root,
            )
            for _attempt in range(200):
                if completion.exists():
                    break
                time.sleep(0.01)
            self.assertTrue(completion.exists())
            self.assertEqual(background_job.process_state(record)["state"], "running")
            background_job.terminate(record, grace=0.1)
            self.assertEqual(background_job.process_state(record)["state"], "completed")

    def test_orphan_recovery_rejects_a_same_session_without_owner_token(self) -> None:
        unrelated = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        try:
            identity = background_job.process_identity(unrelated.pid)
            self.assertIsNotNone(identity)
            assert identity is not None
            _state, process_group, start_ticks = identity
            self.assertEqual(process_group, str(unrelated.pid))
            record = {
                "process": {
                    "completion": "/tmp/background-owner-completion",
                    "owner_token": "a" * 64,
                    "pid": unrelated.pid,
                    "process_group": unrelated.pid,
                    "runtime_log": "/tmp/background-owner-log",
                    "start_ticks": start_ticks,
                    "supervisor_sha256": background_job.sha256_file(
                        background_job.SUPERVISOR
                    ),
                }
            }
            with self.assertRaisesRegex(
                background_job.BackgroundJobError,
                "does not carry the recorded background owner token",
            ):
                background_job._owned_live_process_group(
                    record,
                    require_current=True,
                )
            self.assertIsNone(unrelated.poll())
        finally:
            try:
                os.killpg(unrelated.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            unrelated.wait()

    def test_pid_identity_tampering_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            root.chmod(0o700)
            owner, log, completion = self.paths(root)
            record = background_job.launch(
                owner_path=owner,
                runtime_log=log,
                completion_file=completion,
                record={"owner": "test"},
                argv=[sys.executable, "-c", "import time; time.sleep(60)"],
                cwd=root,
            )
            tampered = json.loads(json.dumps(record))
            tampered["process"]["start_ticks"] = "1"
            with self.assertRaises(background_job.BackgroundJobError):
                background_job.verify_running_process(tampered)
            background_job.terminate(record, grace=0.2)

    def test_old_supervisor_digest_is_cleanup_only(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            root.chmod(0o700)
            owner, log, completion = self.paths(root)
            record = background_job.launch(
                owner_path=owner,
                runtime_log=log,
                completion_file=completion,
                record={"owner": "test"},
                argv=[sys.executable, "-c", "import time; time.sleep(60)"],
                cwd=root,
            )
            outdated = json.loads(json.dumps(record))
            outdated["process"]["supervisor_sha256"] = "0" * 64
            with self.assertRaises(background_job.BackgroundJobError):
                background_job.process_state(outdated)
            self.assertEqual(
                background_job.process_state(outdated, require_current=False)["state"],
                "running",
            )
            background_job.terminate(
                outdated,
                grace=0.2,
                require_current=False,
            )


if __name__ == "__main__":
    unittest.main()
