#!/usr/bin/env python3
# Copyright (C) 2026 kogeler
"""Tests for the system-manager-free background process supervisor."""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import background_job


class BackgroundJobTests(unittest.TestCase):
    def paths(self, root: Path) -> tuple[Path, Path, Path]:
        return root / "owner.json", root / "runtime.log", root / "completion.json"

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
