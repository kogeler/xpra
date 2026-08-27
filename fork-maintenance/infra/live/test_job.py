from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

LIVE_DIRECTORY = Path(__file__).resolve().parent

JOB_SPEC = importlib.util.spec_from_file_location(
    "xpra_lab_live_job", LIVE_DIRECTORY / "job.py"
)
if JOB_SPEC is None or JOB_SPEC.loader is None:
    raise RuntimeError("could not load live-job module")
job = importlib.util.module_from_spec(JOB_SPEC)
sys.path.insert(0, str(LIVE_DIRECTORY))
try:
    JOB_SPEC.loader.exec_module(job)
finally:
    sys.path.pop(0)

RUN_SPEC = importlib.util.spec_from_file_location(
    "xpra_lab_live_run", LIVE_DIRECTORY / "run.py"
)
if RUN_SPEC is None or RUN_SPEC.loader is None:
    raise RuntimeError("could not load live runner module")
live_run = importlib.util.module_from_spec(RUN_SPEC)
sys.modules[RUN_SPEC.name] = live_run
sys.path.insert(0, str(LIVE_DIRECTORY))
try:
    RUN_SPEC.loader.exec_module(live_run)
finally:
    sys.path.pop(0)


JOB_ID = "12345678-1234-4abc-8def-123456789abc"
OTHER_JOB_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def completed(
    argv: list[str],
    stdout: str = "",
    *,
    returncode: int = 0,
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

class LiveJobTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.artifact_root = self.root / "artifacts"
        self.state_root = self.artifact_root / "fork-maintenance"
        self.job_root = self.root / "jobs"
        self.result_root = self.root / "results"
        self.venv_root = self.root / "venvs"
        self.constant_patches = (
            patch.object(job, "ARTIFACT_ROOT", self.artifact_root),
            patch.object(job, "STATE_ROOT", self.state_root),
            patch.object(job, "JOB_ROOT", self.job_root),
            patch.object(job, "RESULT_ROOT", self.result_root),
            patch.object(job, "VENV_ROOT", self.venv_root),
        )
        for value in self.constant_patches:
            value.start()

    def tearDown(self) -> None:
        for value in reversed(self.constant_patches):
            value.stop()
        self.temporary.cleanup()

    def write_private_json(self, path: Path, value: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value) + "\n", encoding="utf-8")
        path.chmod(0o600)

    def record(self, run: str) -> dict[str, object]:
        return {
            "alpha_scenarios": "default",
            "application": "zed",
            "background_supervisor_sha256": job.sha256_file(
                job.BACKGROUND_SUPERVISOR
            ),
            "encoding": "rgb",
            "h264_client_policy": "strict",
            "job_id": JOB_ID,
            "lifecycle": "application-exit",
            "owner": job.OWNER,
            "process": {
                "completion": str(job.completion_path(run)),
                "pid": 12345,
                "process_group": 12345,
                "runtime_log": str(job.runtime_log_path(run)),
                "start_ticks": "42",
                "supervisor_sha256": job.sha256_file(job.BACKGROUND_SUPERVISOR),
            },
            "result_report": str(job.result_path(run)),
            "run": run,
            "runner_sha256": job.sha256_file(job.RUNNER),
            "schema": 2,
            "selection": None,
            "supervisor_sha256": job.sha256_file(job.SUPERVISOR),
        }

    def make_report(self, run: str, record: dict[str, object]) -> None:
        report = job.result_path(run)
        report.parent.mkdir(parents=True)
        report.parent.chmod(0o700)
        report.write_text(
            json.dumps(
                {
                    "application": "zed",
                    "encoding": "rgb",
                    "h264_client_policy": "strict",
                    "lifecycle_profile": "application-exit",
                    "invocation": {
                        "alpha_scenarios": "default",
                        "application": "zed",
                        "h264_client_policy": "strict",
                        "job_id": JOB_ID,
                        "lifecycle": "application-exit",
                        "run_id": run,
                        "selection": "master",
                    },
                    "result": "passed",
                    "source": {
                        "background_supervisor_sha256": record[
                            "background_supervisor_sha256"
                        ],
                        "harness_sha256": record["runner_sha256"],
                        "supervisor_sha256": record["supervisor_sha256"],
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        report.chmod(0o600)

    def test_start_binds_profile_to_owned_process(self) -> None:
        args = Namespace(
            alpha_scenarios="default",
            application="hardware",
            encoding="h264",
            h264_client_policy="strict",
            lifecycle="application-exit",
            render_node=None,
            run="hardware-profile",
            selection=None,
            zed_directory=None,
        )
        captured: dict[str, object] = {}

        def launch(**kwargs: object) -> dict[str, object]:
            captured.update(kwargs)
            record = dict(kwargs["record"])
            record["process"] = {"pid": 12345}
            return record

        with patch.object(job.background_job, "launch", side_effect=launch):
            self.assertEqual(job.start(args), 0)
        record = captured["record"]
        self.assertEqual(record["application"], "hardware")
        argv = captured["argv"]
        self.assertEqual(argv[argv.index("--application") + 1], "hardware")
        self.assertEqual(argv[argv.index("--lifecycle") + 1], "application-exit")
        self.assertEqual(
            captured["environment"]["XPRA_LAB_JOB_ID"],
            record["job_id"],
        )

    def test_collect_accepts_completed_owned_process_and_report(self) -> None:
        run = "accepted"
        record = self.record(run)
        job.prepare_private_state()
        job.runtime_log_path(run).write_bytes(b"complete log\n")
        job.runtime_log_path(run).chmod(0o600)
        self.make_report(run, record)
        completed_state = {
            "exit_code": 0,
            "finished_at": "2026-08-27T00:00:00+00:00",
            "pid": 12345,
            "state": "completed",
        }
        with (
            patch.object(job, "load_record", return_value=record),
            patch.object(job.background_job, "process_state", return_value=completed_state),
            patch.object(
                job,
                "owned_objects",
                return_value={"containers": [], "networks": []},
            ),
        ):
            self.assertEqual(job.collect(Namespace(run=run)), 0)
        self.assertEqual(job.log_path(run).read_bytes(), b"complete log\n")
        status = json.loads(job.status_path(run).read_text(encoding="utf-8"))
        self.assertEqual(status["schema"], 2)
        self.assertEqual(status["exit_code"], 0)
        self.assertTrue(status["report_checks"]["background_supervisor_sha256"])

    def test_collect_records_a_completed_failure(self) -> None:
        run = "failed"
        record = self.record(run)
        job.prepare_private_state()
        job.runtime_log_path(run).write_bytes(b"failed log\n")
        job.runtime_log_path(run).chmod(0o600)
        completed_state = {
            "exit_code": 1,
            "finished_at": "2026-08-27T00:00:00+00:00",
            "pid": 12345,
            "state": "completed",
        }
        with (
            patch.object(job, "load_record", return_value=record),
            patch.object(job.background_job, "process_state", return_value=completed_state),
            patch.object(
                job,
                "owned_objects",
                return_value={"containers": [], "networks": []},
            ),
        ):
            self.assertEqual(job.collect(Namespace(run=run)), 1)
        status = json.loads(job.status_path(run).read_text(encoding="utf-8"))
        self.assertEqual(status["result"], "failed")
        self.assertEqual(status["schema"], 2)

    def test_collect_rejects_running_process(self) -> None:
        run = "running"
        record = self.record(run)
        job.prepare_private_state()
        with (
            patch.object(job, "load_record", return_value=record),
            patch.object(
                job.background_job,
                "process_state",
                return_value={"state": "running", "pid": 12345, "exit_code": None},
            ),
            self.assertRaisesRegex(job.JobError, "still running"),
        ):
            job.collect(Namespace(run=run))
        self.assertFalse((self.job_root / f".{run}.collect.lock").exists())

    def test_remove_deletes_only_exactly_labelled_objects(self) -> None:
        run = "exact-labels"
        record = self.record(run)
        job.prepare_private_state()
        log = b"complete log\n"
        job.log_path(run).write_bytes(log)
        job.log_path(run).chmod(0o600)
        self.write_private_json(
            job.status_path(run),
            {
                "background_supervisor_sha256": record[
                    "background_supervisor_sha256"
                ],
                "job_id": JOB_ID,
                "log_sha256": hashlib.sha256(log).hexdigest(),
                "owner": job.OWNER,
                "process_pid": 12345,
                "run": run,
                "runner_sha256": record["runner_sha256"],
                "schema": 2,
                "supervisor_sha256": record["supervisor_sha256"],
            },
        )
        for path in (
            job.record_path(run),
            job.runtime_log_path(run),
            job.completion_path(run),
        ):
            path.write_text("{}\n", encoding="utf-8")
            path.chmod(0o600)
        labels = {
            "io.xpra.lab.owner": "live",
            "io.xpra.lab.run-id": run,
        }
        ids = {
            "container": ["container-exact"],
            "network": ["network-exact"],
        }
        removals: list[list[str]] = []

        def podman_ids(kind: str, requested_run: str) -> list[str]:
            self.assertEqual(requested_run, run)
            return list(ids[kind])

        def remove_command(
            argv: list[str], *, check: bool = True, capture: bool = True
        ) -> subprocess.CompletedProcess[str]:
            self.assertTrue(check)
            self.assertTrue(capture)
            removals.append(argv)
            if argv[:3] == ["podman", "rm", "--force"]:
                ids["container"].clear()
            elif argv[:3] == ["podman", "network", "rm"]:
                ids["network"].clear()
            return completed(argv)

        with (
            patch.object(job, "load_record", return_value=record),
            patch.object(
                job.background_job,
                "process_state",
                return_value={"state": "completed", "exit_code": 0},
            ),
            patch.object(job, "podman_ids", side_effect=podman_ids),
            patch.object(job, "podman_labels", return_value=labels),
            patch.object(job, "command", side_effect=remove_command),
        ):
            self.assertEqual(job.remove(Namespace(run=run)), 0)
        self.assertEqual(
            removals,
            [
                ["podman", "rm", "--force", "container-exact"],
                ["podman", "network", "rm", "network-exact"],
            ],
        )
        self.assertFalse(job.record_path(run).exists())
        self.assertTrue(job.status_path(run).exists())

    def test_remove_rejects_mismatched_object_labels(self) -> None:
        run = "mismatch"
        labels = {
            "io.xpra.lab.owner": "someone-else",
            "io.xpra.lab.run-id": run,
        }
        with (
            patch.object(
                job,
                "podman_ids",
                side_effect=lambda kind, _run: ["id"] if kind == "container" else [],
            ),
            patch.object(job, "podman_labels", return_value=labels),
            self.assertRaisesRegex(job.JobError, "refusing unowned container"),
        ):
            job.remove_owned_objects(run)

    def test_abort_terminates_process_and_removes_partial_result(self) -> None:
        run = "abort"
        record = self.record(run)
        job.prepare_private_state()
        for path in (
            job.record_path(run),
            job.runtime_log_path(run),
            job.completion_path(run),
        ):
            path.write_text("{}\n", encoding="utf-8")
            path.chmod(0o600)
        result_directory = job.result_path(run).parent
        result_directory.mkdir(parents=True)
        result_directory.chmod(0o700)
        (result_directory / "partial").write_text("partial", encoding="utf-8")
        with (
            patch.object(job, "load_record", return_value=record),
            patch.object(
                job.background_job,
                "process_state",
                return_value={"state": "running", "pid": 12345},
            ),
            patch.object(job.background_job, "terminate") as terminate,
            patch.object(job, "remove_owned_objects") as remove_objects,
        ):
            self.assertEqual(job.abort(Namespace(run=run)), 0)
        terminate.assert_called_once_with(record, require_current=False)
        remove_objects.assert_called_once_with(run)
        self.assertFalse(job.record_path(run).exists())
        self.assertFalse(result_directory.exists())

    def test_private_state_tightens_owned_directories(self) -> None:
        self.artifact_root.mkdir(mode=0o755)
        self.state_root.mkdir(mode=0o775)
        job.prepare_private_state()
        for path in (
            self.state_root,
            self.state_root / "jobs",
            self.job_root,
            self.result_root,
            self.venv_root,
        ):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.artifact_root.stat().st_mode & 0o777, 0o755)

    def test_private_state_rejects_symlink(self) -> None:
        target = self.root / "untrusted-target"
        target.mkdir()
        self.artifact_root.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(job.JobError, "symlink"):
            job.prepare_private_state()
        self.assertEqual(list(target.iterdir()), [])

    def test_private_state_rejects_writable_parent(self) -> None:
        self.artifact_root.mkdir(mode=0o700)
        self.artifact_root.chmod(0o775)
        with self.assertRaisesRegex(job.JobError, "writable by another user"):
            job.prepare_private_state()
        self.assertFalse(self.state_root.exists())

    def test_network_labels_accept_lowercase_podman_field(self) -> None:
        labels = {
            "io.xpra.lab.owner": "live",
            "io.xpra.lab.run-id": "lowercase-labels",
        }
        argv = ["podman", "network", "inspect", "network-id"]
        with patch.object(
            job,
            "command",
            return_value=completed(argv, json.dumps([{"labels": labels}])),
        ):
            self.assertEqual(job.podman_labels("network", "network-id"), labels)

    def test_network_labels_reject_conflicting_podman_fields(self) -> None:
        argv = ["podman", "network", "inspect", "network-id"]
        payload = [
            {
                "Labels": {"io.xpra.lab.owner": "live"},
                "labels": {"io.xpra.lab.owner": "someone-else"},
            }
        ]
        with (
            patch.object(
                job,
                "command",
                return_value=completed(argv, json.dumps(payload)),
            ),
            self.assertRaisesRegex(job.JobError, "conflicting labels"),
        ):
            job.podman_labels("network", "network-id")

class LiveRunnerCleanupTest(unittest.TestCase):
    def test_network_labels_accept_lowercase_podman_field(self) -> None:
        labels = {
            "io.xpra.lab.owner": "live",
            "io.xpra.lab.run-id": "lowercase-labels",
        }
        argv = ["podman", "network", "inspect", "network-id"]
        with patch.object(
            live_run,
            "run",
            return_value=completed(argv, json.dumps([{"labels": labels}])),
        ):
            self.assertEqual(
                live_run.inspect_podman_object_labels("network", "network-id"),
                labels,
            )

    def test_network_labels_reject_conflicting_podman_fields(self) -> None:
        argv = ["podman", "network", "inspect", "network-id"]
        payload = [
            {
                "Labels": {"io.xpra.lab.owner": "live"},
                "labels": {"io.xpra.lab.owner": "someone-else"},
            }
        ]
        with (
            patch.object(
                live_run,
                "run",
                return_value=completed(argv, json.dumps(payload)),
            ),
            self.assertRaisesRegex(live_run.LabFailure, "conflicting provenance"),
        ):
            live_run.inspect_podman_object_labels("network", "network-id")

    def test_nonzero_remove_is_success_when_object_is_absent(self) -> None:
        name = "container-id"
        labels = {
            "io.xpra.lab.owner": "live",
            "io.xpra.lab.run-id": "cleanup-run",
        }

        def podman_command(
            argv: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            if argv == ["podman", "container", "inspect", name]:
                return completed(argv, json.dumps([{"Config": {"Labels": labels}}]))
            if argv == ["podman", "rm", "--force", name]:
                return completed(
                    argv,
                    "remove output\n",
                    returncode=125,
                    stderr="reported failure after removal\n",
                )
            if argv == ["podman", "ps", "--all", "--format", "{{.Names}}"]:
                return completed(argv, "unrelated-container\n")
            raise AssertionError(f"unexpected command: {argv!r}")

        with patch.object(live_run, "run", side_effect=podman_command):
            result = live_run.remove_owned_podman_object("container", name, labels)
        self.assertEqual(result["status"], "removed")
        self.assertEqual(result["postcondition"], "absent")
        self.assertEqual(result["remove_returncode"], 125)
        self.assertEqual(result["remove_stdout"], "remove output\n")
        self.assertEqual(result["remove_stderr"], "reported failure after removal\n")

    def test_remove_fails_when_object_remains(self) -> None:
        name = "container-id"
        labels = {
            "io.xpra.lab.owner": "live",
            "io.xpra.lab.run-id": "cleanup-run",
        }

        def podman_command(
            argv: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            if argv == ["podman", "container", "inspect", name]:
                return completed(argv, json.dumps([{"Config": {"Labels": labels}}]))
            if argv == ["podman", "rm", "--force", name]:
                return completed(argv, returncode=125, stderr="remove failed\n")
            if argv == ["podman", "ps", "--all", "--format", "{{.Names}}"]:
                return completed(argv, f"{name}\n")
            raise AssertionError(f"unexpected command: {argv!r}")

        with patch.object(live_run, "run", side_effect=podman_command):
            result = live_run.remove_owned_podman_object("container", name, labels)
        self.assertEqual(result["status"], "remove-failed")
        self.assertEqual(result["postcondition"], "present")


class LiveTransportProfileTest(unittest.TestCase):
    def test_supported_xpra_only_profiles_are_exact(self) -> None:
        for values in (
            ("zed", "application-exit", "rgb", "strict", "both"),
            ("zed", "application-exit", "h264", "adaptive-alpha", "default"),
            (
                "hardware",
                "application-exit",
                "h264",
                "strict",
                "default",
            ),
            ("gtk", "detach", "rgb", "strict", "default"),
            ("gtk", "transport-loss", "rgb", "strict", "default"),
        ):
            with self.subTest(values=values):
                live_run.validate_profile(
                    application=values[0],
                    lifecycle=values[1],
                    encoding=values[2],
                    h264_client_policy=values[3],
                    alpha_scenarios=values[4],
                )
        for values in (
            ("zed", "detach", "rgb", "strict", "default"),
            ("gtk", "transport-loss", "h264", "strict", "default"),
            ("hardware", "application-exit", "rgb", "strict", "default"),
            (
                "hardware",
                "application-exit",
                "h264",
                "fallback-h264",
                "default",
            ),
        ):
            with (
                self.subTest(values=values),
                self.assertRaises(live_run.ProfileError),
            ):
                live_run.validate_profile(
                    application=values[0],
                    lifecycle=values[1],
                    encoding=values[2],
                    h264_client_policy=values[3],
                    alpha_scenarios=values[4],
                )

    def test_lifecycle_scenarios_do_not_inherit_alpha_matrix(self) -> None:
        self.assertEqual(
            live_run.scenario_specs(alpha_scenarios="default", lifecycle="detach"),
            (("detach", False),),
        )
        self.assertEqual(
            live_run.scenario_specs(
                alpha_scenarios="default", lifecycle="transport-loss"
            ),
            (("transport-loss", False),),
        )

    def test_lifecycle_boundaries_fail_closed(self) -> None:
        detach = {
            "application_survived_detach": True,
            "client_exit_status": 0,
            "client_exited_after_detach": True,
            "detach_returncode": 0,
            "server_exited_after_application": True,
            "server_survived_detach": True,
        }
        self.assertTrue(
            all(live_run.lifecycle_boundary_checks("detach", detach).values())
        )
        detach["application_survived_detach"] = False
        self.assertFalse(
            live_run.lifecycle_boundary_checks("detach", detach)[
                "application_survived_detach"
            ]
        )
        transport = {
            "application_survived_transport_loss": True,
            "client_exit_status": 1,
            "client_exited_after_transport_loss": True,
            "server_exited_after_application": True,
            "server_survived_transport_loss": True,
            "transport_disconnect_returncode": 0,
        }
        self.assertTrue(
            all(
                live_run.lifecycle_boundary_checks("transport-loss", transport).values()
            )
        )
        transport["client_exit_status"] = 0
        self.assertFalse(
            live_run.lifecycle_boundary_checks("transport-loss", transport)[
                "client_exit_nonzero"
            ]
        )

    def test_server_window_id_is_title_bound(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            info = Path(raw) / "server-info.txt"
            info.write_text(
                "windows.1.title=vkcube\n"
                "windows.2.title=Xpra Hardware Interaction Ready on server\n",
                encoding="utf-8",
            )
            self.assertEqual(live_run.server_xpra_window_id(info, ("vkcube",)), 1)
            with self.assertRaisesRegex(live_run.LabFailure, "identified 0"):
                live_run.server_xpra_window_id(info, ("missing",))
            self.assertEqual(
                live_run.server_xpra_window_id(
                    info, ("xpra hardware interaction ready",)
                ),
                2,
            )

    def test_hardware_application_uses_owned_multiwindow_fixture(self) -> None:
        command, titles, pid_file = live_run.application_contract("hardware")
        self.assertEqual(command, "/opt/xpra-lab/start_hardware_fixture.sh")
        self.assertEqual(titles, ("vkcube",))
        self.assertEqual(pid_file, "vkcube.pid")
        context_names = {path.name for path in live_run.BUILD_CONTEXT_INPUTS}
        self.assertIn("start_hardware_fixture.sh", context_names)

    def test_pixel_tolerance_is_scoped_to_the_owning_profile(self) -> None:
        self.assertEqual(live_run.pixel_error_limit("zed", "rgb"), 0.0)
        self.assertEqual(live_run.pixel_error_limit("gtk", "rgb"), 1.0)
        self.assertEqual(live_run.pixel_error_limit("hardware", "h264"), 15.0)
        self.assertEqual(live_run.pixel_error_limit("vkcube", "rgb"), 0.0)

    def test_hardware_application_uses_observable_vulkan_boundaries(self) -> None:
        checks = live_run.application_boundary_checks(
            application="hardware",
            application_activity={
                "process_alive": True,
                "vulkan_motion": {"changed": True},
            },
            application_gpu={
                "gpu_mappings": ["/usr/lib/libvulkan_radeon.so"],
                "render_nodes": ["/dev/dri/renderD128"],
            },
            log_evidence={
                "wayland_protocol": {
                    "ack_configure": 0,
                    "commits": 0,
                    "damage_buffer": 0,
                }
            },
            render_node=Path("/dev/dri/renderD128"),
        )
        self.assertTrue(all(checks.values()))
        self.assertNotIn("wayland_ack_configure", checks)
        checks["vulkan_frames_changed"] = False
        self.assertFalse(all(checks.values()))

    def test_strict_hardware_packet_check_rejects_empty_and_rgb_windows(self) -> None:
        updates = {
            "count": 2,
            "encodings": ["h264"],
            "updates": [
                {"encoding": "h264", "payload_bytes": 10},
                {"encoding": "h264", "payload_bytes": 20},
            ],
        }
        self.assertTrue(live_run.only_positive_h264_packets(updates))
        updates["encodings"] = ["h264", "rgb32"]
        self.assertFalse(live_run.only_positive_h264_packets(updates))
        updates["encodings"] = ["h264"]
        updates["updates"][1]["payload_bytes"] = 0
        self.assertFalse(live_run.only_positive_h264_packets(updates))
        self.assertFalse(live_run.only_positive_h264_packets(None))

    def test_transport_encoding_options(self) -> None:
        self.assertEqual(
            live_run.transport_encoding_options("h264", "strict", client=False),
            [
                "--video=yes",
                "--encodings=h264",
                "--video-encoders=libva",
                "--csc-modules=libyuv",
            ],
        )
        self.assertEqual(
            live_run.transport_encoding_options("h264", "fallback-auto", client=True),
            [
                "--video=yes",
                "--encodings=h264,rgb",
                "--opengl=force:native",
                "--video-decoders=libva",
                "--csc-modules=none",
            ],
        )
        forced = live_run.transport_encoding_options(
            "h264", "fallback-h264", client=True
        )
        self.assertEqual(forced[-1], "--encoding=h264")
        self.assertIn("--encodings=h264,rgb", forced)
        adaptive = live_run.transport_encoding_options(
            "h264", "adaptive-alpha", client=True
        )
        self.assertIn("--encodings=h264,webp,rgb", adaptive)
        self.assertEqual(adaptive[-1], "--encoding=h264")
        self.assertIn(
            "--encodings=h264,webp,rgb",
            live_run.transport_encoding_options("h264", "adaptive-alpha", client=False),
        )
        with self.assertRaisesRegex(live_run.LabFailure, "H.264 policies require"):
            live_run.transport_encoding_options("rgb", "fallback-auto", client=True)

    def test_picture_fallback_requires_rgb_only_production_evidence(self) -> None:
        logs = {
            "client_draw_regions": 4,
            "client_successful_paints": 4,
            "h264_draw_regions": 0,
            "h264_per_window_negotiation_applied": True,
            "h264_pipeline_errors": [],
            "h264_pre_negotiation_errors": [],
            "opengl_presentations": 4,
            "opengl_renderer": "AMD Radeon Graphics",
            "opengl_rgb_paints": 4,
            "paint_errors": [],
            "rgb_encodes": 4,
            "server_initial_data_errors": [],
            "server_logging_errors": 0,
        }
        updates = {
            "count": 2,
            "encodings": ["rgb32"],
            "updates": [
                {"encoding": "rgb32", "payload_bytes": 10},
                {"encoding": "rgb32", "payload_bytes": 20},
            ],
        }
        hardware = {
            "client_desktop": {"hardware_renderer": True},
            "client_graphics_process": {
                "gpu_mappings": ["/usr/lib/dri/radeonsi_dri.so"],
                "render_nodes": ["/dev/dri/renderD128"],
            },
        }
        boundaries = live_run.classify_h264_picture_fallback(
            policy="fallback-h264",
            render_node=Path("/dev/dri/renderD128"),
            log_evidence=logs,
            updates=updates,
            codec_hardware=hardware,
        )
        self.assertTrue(all(all(values.values()) for values in boundaries))

        updates["encodings"] = ["h264", "rgb32"]
        updates["updates"].append({"encoding": "h264", "payload_bytes": 30})
        boundaries = live_run.classify_h264_picture_fallback(
            policy="fallback-h264",
            render_node=Path("/dev/dri/renderD128"),
            log_evidence=logs,
            updates=updates,
            codec_hardware=hardware,
        )
        self.assertFalse(boundaries[1]["only_picture_packets"])
        self.assertFalse(boundaries[1]["h264_not_reached"])


class H264EvidenceTest(unittest.TestCase):
    @staticmethod
    def packet(sequence: int, frame: int, *, scaled: bool = True) -> dict[str, object]:
        options: dict[str, object] = {
            "frame": frame,
            "type": "IDR" if frame == 0 else "P",
        }
        if scaled:
            options["scaled_size"] = [1064, 780]
        return {
            "encoding": "h264",
            "sequence": sequence,
            "x": 0,
            "y": 0,
            "w": 1596,
            "h": 1172,
            "payload_bytes": 100,
            "options": options,
        }

    @staticmethod
    def context(entrypoint: str) -> dict[str, object]:
        return {
            "completed_frames": 2,
            "context": "0x1",
            "created": True,
            "entrypoint": entrypoint,
            "file": f"trace-{entrypoint}",
            "generation": 1,
            "height": 784,
            "incomplete_frames": 0,
            "profile": "VAProfileH264Main",
            "width": 1072,
        }

    def adaptive_edge_updates(self) -> dict[str, object]:
        first = self.packet(1, 0)
        second = self.packet(3, 1)
        first["options"]["window-size"] = [1596, 1173]
        second["options"]["window-size"] = [1596, 1173]
        edge = {
            "encoding": "rgb24",
            "sequence": 2,
            "x": 0,
            "y": 1172,
            "w": 1596,
            "h": 1,
            "payload_bytes": 64,
            "options": {
                "flush": 1,
                "rgb_format": "RGBX",
                "window-size": [1596, 1173],
            },
        }
        return {
            "count": 3,
            "encodings": ["h264", "rgb24"],
            "updates": [first, edge, second],
        }

    def test_adaptive_h264_accepts_only_exact_lossless_codec_edges(self) -> None:
        updates = self.adaptive_edge_updates()
        self.assertTrue(live_run.h264_with_lossless_rgb_edges(updates))

        invalid_changes = {
            "alpha format": ("options", "rgb_format", "RGBA"),
            "full frame": (None, "h", 1173),
            "interior row": (None, "y", 1171),
            "missing flush": ("options", "flush", 0),
            "two pixel edge": (None, "h", 2),
            "zero payload": (None, "payload_bytes", 0),
        }
        for name, (parent, field, value) in invalid_changes.items():
            with self.subTest(name=name):
                candidate = json.loads(json.dumps(updates))
                edge = candidate["updates"][1]
                target = edge[parent] if parent else edge
                target[field] = value
                self.assertFalse(live_run.h264_with_lossless_rgb_edges(candidate))

        missing_edge = json.loads(json.dumps(updates))
        missing_edge["updates"].pop(1)
        missing_edge["count"] = 2
        missing_edge["encodings"] = ["h264"]
        self.assertFalse(live_run.h264_with_lossless_rgb_edges(missing_edge))

        picture_fallback = json.loads(json.dumps(updates))
        picture_fallback["updates"] = [picture_fallback["updates"][1]]
        picture_fallback["count"] = 1
        picture_fallback["encodings"] = ["rgb24"]
        self.assertFalse(live_run.h264_with_lossless_rgb_edges(picture_fallback))

    def test_adaptive_h264_stream_allows_only_verified_edge_interleaving(self) -> None:
        updates = self.adaptive_edge_updates()
        strict_streams = live_run.h264_packet_streams(updates)
        self.assertEqual(len(strict_streams), 2)

        streams = live_run.h264_packet_streams(
            updates,
            allow_lossless_rgb_edges=True,
        )
        self.assertEqual(len(streams), 1)
        self.assertTrue(streams[0]["contiguous_frames"])
        self.assertTrue(streams[0]["contiguous_sequences"])
        self.assertEqual(streams[0]["packet_sequences"], [1, 3])
        self.assertEqual(streams[0]["interleaved_edge_sequences"], [2])
        self.assertEqual(streams[0]["transport_sequences"], [1, 2, 3])

        result = live_run.match_h264_production_stream(
            updates,
            {"contexts": [self.context("VAEntrypointEncSlice")]},
            {"contexts": [self.context("VAEntrypointVLD")]},
            allow_lossless_rgb_edges=True,
        )
        self.assertTrue(result["production_proven"])

        invalid = json.loads(json.dumps(updates))
        invalid["updates"][1]["y"] = 1171
        streams = live_run.h264_packet_streams(
            invalid,
            allow_lossless_rgb_edges=True,
        )
        self.assertEqual(len(streams), 2)

    def test_scaled_stream_matches_scaled_va_surfaces(self) -> None:
        updates = {"updates": [self.packet(3, 0), self.packet(4, 1)]}
        streams = live_run.h264_packet_streams(updates)
        self.assertEqual(streams[0]["coded_size"], [1596, 1172])
        self.assertEqual(streams[0]["encoded_size"], [1064, 780])
        self.assertEqual(streams[0]["surface_size"], [1072, 784])

        result = live_run.match_h264_production_stream(
            updates,
            {"contexts": [self.context("VAEntrypointEncSlice")]},
            {"contexts": [self.context("VAEntrypointVLD")]},
        )
        self.assertTrue(result["production_proven"])
        self.assertEqual(result["matched_stream"]["first_sequence"], 3)

    def test_unscaled_stream_keeps_coded_dimensions(self) -> None:
        updates = {"updates": [self.packet(1, 0, scaled=False)]}
        stream = live_run.h264_packet_streams(updates)[0]
        self.assertEqual(stream["encoded_size"], [1596, 1172])
        self.assertEqual(stream["surface_size"], [1600, 1184])

    def test_scaled_packet_chain_uses_encoded_decoder_dimensions(self) -> None:
        packet = self.packet(3, 0)
        packet["payload_sha256"] = "a" * 64
        updates = {"updates": [packet], "window_id": 1}
        callback = "0x7f8bf8bdb920"
        options = "{'frame': 0, 'type': 'IDR', 'scaled_size': (1064, 780)}"
        log = "\n".join(
            (
                (
                    "process_draw: 100 <class 'memoryview'> for window 1, "
                    "sequence 3, 1596x1172 at 0,0 using h264 encoding "
                    f"with options=typedict({options})"
                ),
                (
                    "draw_region(0, 0, 1596, 1172, h264, 100 bytes, 0, "
                    f"typedict({options}), [<function "
                    "WindowDraw._do_draw.<locals>.record_decode_time "
                    f"at {callback}>"
                ),
                "choose_decoder([libva(YUV420P - h264)])=libva(YUV420P - h264)",
                "paint_with_video_decoder: new libva('h264', 1064, 780, 'YUV420P')",
                "libva decoded h264 100 bytes into 1064x780 NV12",
                (
                    "do_video_paint('h264', ImageWrapper(NV12:"
                    "(0, 0, 1064, 780, 24):PLANAR_2), "
                    f"record_decode_time at {callback}>"
                ),
                "record_decode_time(True, ) wid=0x1, h264: 1596x1172, 8.4ms",
                "sending ack: ('window-ack', 1, 1596, 1172, 3, 8468, \"''\")",
                "do_present_fbo(GLXWindowContext(0x400015)) will blit [(0, 0, 1596, 1173)]",
                "1.do_gl_show(GLDrawingArea(1, (1596, 1173))) swapping buffers now",
                "GLDrawingArea(1, (1596, 1173)).do_present_fbo() done",
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "client.stdout").write_text(log + "\n", encoding="utf-8")
            result = live_run.h264_client_packet_chain(
                directory,
                updates,
                {"first_sequence": 3},
            )
        self.assertTrue(result["complete"])
        self.assertEqual(result["size"], [1596, 1172])
        self.assertEqual(result["encoded_size"], [1064, 780])


if __name__ == "__main__":
    unittest.main()
