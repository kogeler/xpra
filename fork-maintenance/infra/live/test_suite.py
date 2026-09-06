# Copyright (C) 2026 kogeler
"""Full-suite coverage cannot be satisfied by a single passing scenario."""

import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock, patch

import suite


class LiveSuiteTest(unittest.TestCase):
    def records(self):
        records = {}
        for run, profile in suite.members("suite-control"):
            values = dict(zip((
                "application", "lifecycle", "encoding", "h264_client_policy", "alpha_scenarios",
            ), profile, strict=True))
            values.update({
                "render_node": "/dev/dri/renderD128",
                "network_profile": suite.DEFAULT_NETWORK_PROFILE,
                "input_provenance": {
                    "source_commit": "a" * 40, "server_selection_sha256": "b" * 64,
                    "harness_sha256": "c" * 64, "source_archive_sha256": "d" * 64,
                    "source_workflow_sha256": "e" * 64, "server_context_sha256": "f" * 64,
                    "server_context_archive_sha256": "1" * 64,
                    "server_selection_resolution_sha256": "2" * 64,
                    "zed_archive_sha256": "3" * 64, "zed_binary_sha256": "4" * 64,
                },
            })
            records[run] = values
        return records

    def check(self, records, *, status="success", validation=None):
        def record(run):
            if run not in records:
                raise suite.job.JobError("missing suite member")
            return records[run]
        with (
            patch.object(suite, "current_inputs", return_value=("a" * 40, "b" * 64, "c" * 64)),
            patch.object(suite, "retained_record", side_effect=record),
            patch.object(suite.job, "verify_collected"),
            patch.object(suite.job, "load_private_json", return_value={"result": status}),
            patch.object(suite.job, "report_validation", return_value=(
                validation if validation is not None else ("passed", "5" * 64, {"evidence": True})
            )),
        ):
            return suite.check("suite-control")

    def test_exact_nine_profiles_and_both_former_case_gates(self):
        members = suite.members("suite-control")
        self.assertEqual(len(members), 9)
        self.assertEqual(len({run for run, _profile in members}), 9)
        self.assertEqual(members[0][1][0], "clipboard")
        self.assertEqual(members[1][1][0], "subsurface")
        report = self.check(self.records())
        self.assertEqual(report["result"], "passed")
        self.assertEqual(set(report["reports"]), {run for run, _ in members})

    def test_every_missing_profile_fails(self):
        for run in self.records():
            records = self.records()
            del records[run]
            with self.subTest(run=run), self.assertRaises(suite.job.JobError):
                self.check(records)

    def test_failed_status_or_failed_reparsed_evidence_cannot_accept_suite(self):
        for status in ("failed", "running", None):
            with self.subTest(status=status), self.assertRaisesRegex(suite.job.JobError, "did not pass"):
                self.check(self.records(), status=status)
        for validation in (("failed", "5" * 64, {"evidence": True}),
                           ("passed", "5" * 64, {"evidence": False}),
                           ("passed", "5" * 64, {})):
            with self.subTest(validation=validation), self.assertRaisesRegex(suite.job.JobError, "no longer validates"):
                self.check(self.records(), validation=validation)

    def test_mixed_candidate_context_hardware_and_application_payload_fail(self):
        for field in (
            "source_commit", "server_selection_sha256", "harness_sha256",
            "server_context_sha256", "server_context_archive_sha256",
            "server_selection_resolution_sha256", "source_archive_sha256",
            "source_workflow_sha256", "zed_archive_sha256", "zed_binary_sha256",
        ):
            records = self.records()
            last = next(reversed(records))
            records[last]["input_provenance"][field] = "changed"
            with self.subTest(field=field), self.assertRaises(suite.job.JobError):
                self.check(records)
        for field in ("render_node", "network_profile", "application"):
            records = self.records()
            records[next(reversed(records))][field] = "changed"
            with self.subTest(field=field), self.assertRaises(suite.job.JobError):
                self.check(records)

    def test_dispatch_uses_named_start_wait_remove_for_every_profile(self):
        with tempfile.TemporaryDirectory() as raw:
            commands = []
            with (
                patch.object(suite, "current_inputs", return_value=("source", "queue", "harness")),
                patch.object(suite, "check", return_value={"result": "passed"}),
                patch.object(suite.job, "JOB_ROOT", Path(raw) / "jobs"),
                patch.object(suite.job, "RESULT_ROOT", Path(raw) / "results"),
                patch.object(suite.subprocess, "run", side_effect=lambda argv, **kw: commands.append(argv) or Mock(returncode=0)),
            ):
                args = Namespace(run="fresh-suite", network_profile=suite.DEFAULT_NETWORK_PROFILE, render_node=None, zed_directory=None)
                self.assertEqual(suite.run_all(args), 0)
            self.assertEqual(len(commands), 27)
            for start, wait, remove in zip(commands[::3], commands[1::3], commands[2::3], strict=True):
                self.assertIn("STACK=develop", start)
                self.assertIn("CASE=", start)
                self.assertIn("PATCH_MODE=patched", start)
                self.assertEqual(wait[-2], "wait")
                self.assertEqual(remove[-2:], ["remove", wait[-1]])

    def test_dispatch_stops_at_first_failure_without_claiming_suite_success(self):
        with (
            tempfile.TemporaryDirectory() as raw,
            patch.object(suite, "current_inputs", return_value=("source", "queue", "harness")),
            patch.object(suite, "check") as check,
            patch.object(suite.job, "JOB_ROOT", Path(raw) / "jobs"),
            patch.object(suite.job, "RESULT_ROOT", Path(raw) / "results"),
            patch.object(suite.subprocess, "run", return_value=Mock(returncode=23)) as run,
        ):
            args = Namespace(run="failed-suite", network_profile=suite.DEFAULT_NETWORK_PROFILE, render_node=None, zed_directory=None)
            self.assertEqual(suite.run_all(args), 23)
            self.assertEqual(run.call_count, 1)
            check.assert_not_called()


if __name__ == "__main__":
    unittest.main()
