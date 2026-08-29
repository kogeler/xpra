from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import contextmanager, redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import ANY, Mock, patch

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
job.live_run = live_run


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


def execute_container_log_probe(
    root: Path,
) -> object:
    def execute(
        _container: str,
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        if command[:2] != ["python3", "-c"]:
            raise AssertionError(f"unexpected probe command: {command}")
        output = StringIO()
        arguments = ["-c", *command[3:-2], str(root), command[-1]]
        with patch.object(sys, "argv", arguments), redirect_stdout(output):
            exec(command[2], {"__name__": "__main__"})  # noqa: S102
        return completed(command, output.getvalue())

    return execute


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

    def retained_remove(
        self,
        run: str,
        *,
        complete: bool = True,
    ) -> tuple[dict[str, object], bytes]:
        job.prepare_private_state()
        record: dict[str, object] = {
            "job_id": JOB_ID,
            "owner": job.OWNER,
            "process": {
                "completion": str(job.completion_path(run)),
                "runtime_log": str(job.runtime_log_path(run)),
            },
            "result_report": str(job.result_path(run)),
            "run": run,
            "schema": 4,
        }
        log = b"retained collected log\n"
        self.write_private_json(job.record_path(run), record)
        job.runtime_log_path(run).write_bytes(log)
        job.runtime_log_path(run).chmod(0o600)
        self.write_private_json(job.completion_path(run), {"exit_code": 0})
        job.log_path(run).write_bytes(log)
        job.log_path(run).chmod(0o600)
        self.write_private_json(job.status_path(run), {"result": "success"})
        transaction = job.publish_remove_transaction(run, record)
        if complete:
            job.cleanup_removal_runtime(run, transaction)
        else:
            job.record_path(run).unlink()
        return record, log

    def record(self, run: str) -> dict[str, object]:
        provenance = {
            "client_context_archive_sha256": "1" * 64,
            "client_context_sha256": "2" * 64,
            "client_selection": "master",
            "client_selection_resolution_sha256": "3" * 64,
            "client_selection_sha256": "4" * 64,
            "harness_sha256": job.harness_sha256(),
            "input_manifest_sha256": "5" * 64,
            "input_tree_sha256": "6" * 64,
            "path": str(job.RESULT_ROOT / run / "inputs"),
            "schema": 2,
            "server_context_archive_sha256": "7" * 64,
            "server_context_sha256": "8" * 64,
            "server_selection": "stacks/develop",
            "server_selection_resolution_sha256": "9" * 64,
            "server_selection_sha256": "a" * 64,
            "source_archive_sha256": "b" * 64,
            "source_commit": "c" * 40,
            "source_commit_marker": "gc12345678",
            "source_revision": 5515,
            "source_workflow_sha256": "d" * 64,
            "zed_archive_sha256": "e" * 64,
            "zed_binary_sha256": "f" * 64,
        }
        return {
            "alpha_scenarios": "default",
            "application": "zed",
            "background_supervisor_sha256": job.sha256_file(
                job.BACKGROUND_SUPERVISOR
            ),
            "encoding": "rgb",
            "h264_client_policy": "strict",
            "harness_sha256": job.harness_sha256(),
            "input_provenance": provenance,
            "job_id": JOB_ID,
            "lifecycle": "application-exit",
            "network_profile": job.DEFAULT_NETWORK_PROFILE,
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
            "render_node": "/dev/dri/renderD128",
            "run": run,
            "runner_sha256": job.sha256_file(job.RUNNER),
            "schema": 4,
            "selection": "stacks/develop",
            "supervisor_sha256": job.sha256_file(job.SUPERVISOR),
        }

    def freeze_prelaunch(self, run: str, *, job_id: str = JOB_ID) -> dict[str, object]:
        return {
            "application": "hardware",
            "background_supervisor_sha256": job.sha256_file(
                job.BACKGROUND_SUPERVISOR
            ),
            "completion": str(job.freeze_completion_path(run)),
            "freeze_owner": str(job.freeze_record_path(run)),
            "freeze_result": str(job.freeze_result_path(run)),
            "harness_sha256": job.harness_sha256(),
            "job_id": job_id,
            "kind": "input-freeze-prelaunch",
            "owner": job.OWNER,
            "process": {"pid": 12345, "start_ticks": "42"},
            "result": str(job.RESULT_ROOT / run),
            "run": run,
            "runner_sha256": job.sha256_file(job.RUNNER),
            "schema": 1,
            "selection": None,
            "staging": str(job.freeze_staging_path(run, job_id)),
            "supervisor_sha256": job.sha256_file(job.SUPERVISOR),
            "zed_directory": None,
        }

    def make_report(self, run: str, record: dict[str, object]) -> None:
        report = job.result_path(run)
        report.parent.mkdir(parents=True)
        report.parent.chmod(0o700)
        inputs = report.parent / "inputs"
        inputs.mkdir()
        inputs.chmod(0o700)
        manifest = inputs / "manifest.json"
        manifest.write_text('{"input": "unit"}\n', encoding="utf-8")
        manifest.chmod(0o600)
        manifest_digest = job.sha256_file(manifest)
        checksums = inputs / "SHA256SUMS"
        checksums.write_text(
            f"{manifest_digest}  manifest.json\n",
            encoding="utf-8",
        )
        checksums.chmod(0o600)
        provenance = record["input_provenance"]
        provenance["input_manifest_sha256"] = manifest_digest
        provenance["input_tree_sha256"] = live_run.tree_sha256(inputs)
        scenario_root = report.parent / "default-alpha"
        scenario_root.mkdir()
        scenario_root.chmod(0o700)
        artifact = scenario_root / "evidence.txt"
        artifact.write_text("evidence\n", encoding="utf-8")
        artifact.chmod(0o600)
        scenario = {
            "artifact_collection_passed": True,
            "artifact_sha256": {"evidence.txt": job.sha256_file(artifact)},
            "client": {
                "network_options": list(
                    live_run.live_config.network_profile(
                        str(record["network_profile"])
                    ).client_options()
                )
            },
            "cleanup": {"passed": True},
            "name": "default-alpha",
            "network_profile": record["network_profile"],
            "result": "passed",
        }
        scenario_report = scenario_root / "report.json"
        scenario_report.write_text(
            json.dumps(scenario, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        scenario_report.chmod(0o600)
        report.write_text(
            json.dumps(
                {
                    "application": "zed",
                    "encoding": "rgb",
                    "h264_client_policy": "strict",
                    "lifecycle_profile": "application-exit",
                    "network_profile": record["network_profile"],
                    "invocation": {
                        "alpha_scenarios": "default",
                        "application": "zed",
                        "h264_client_policy": "strict",
                        "job_id": JOB_ID,
                        "lifecycle": "application-exit",
                        "network_profile": record["network_profile"],
                        "render_node": record["render_node"],
                        "run_id": run,
                        "selection": record["selection"],
                    },
                    "images": {
                        "client": {
                            "build_context_sha256": provenance[
                                "client_context_sha256"
                            ],
                            "id": "sha256:" + "1" * 64,
                            "labels": {
                                "io.xpra.lab.context": provenance[
                                    "client_context_sha256"
                                ],
                                "io.xpra.lab.owner": "live",
                                "io.xpra.lab.role": "client-image",
                                "io.xpra.lab.source": provenance["source_commit"],
                            },
                            "selection": "master",
                            "tag": "localhost/client:test",
                        },
                        "server": {
                            "build_context_sha256": provenance[
                                "server_context_sha256"
                            ],
                            "id": "sha256:" + "2" * 64,
                            "labels": {
                                "io.xpra.lab.context": provenance[
                                    "server_context_sha256"
                                ],
                                "io.xpra.lab.owner": "live",
                                "io.xpra.lab.role": "server-image",
                                "io.xpra.lab.source": provenance["source_commit"],
                            },
                            "selection": provenance["server_selection"],
                            "tag": "localhost/server:test",
                        },
                    },
                    "result": "passed",
                    "scenario_report_sha256": {
                        "default-alpha": job.sha256_file(scenario_report)
                    },
                    "scenarios": [scenario],
                    "source": {
                        "background_supervisor_sha256": record[
                            "background_supervisor_sha256"
                        ],
                        "harness_sha256": record["harness_sha256"],
                        "input_manifest_sha256": manifest_digest,
                        "input_provenance": {
                            key: value
                            for key, value in provenance.items()
                            if key
                            not in {
                                "input_manifest_sha256",
                                "input_tree_sha256",
                                "path",
                            }
                        },
                        "input_tree_sha256": provenance["input_tree_sha256"],
                        "archive_sha256": provenance["source_archive_sha256"],
                        "commit": provenance["source_commit"],
                        "fork_master": provenance["source_commit"],
                        "selection": {
                            "digest": provenance["server_selection_sha256"],
                            "name": provenance["server_selection"],
                            "resolution": {
                                "resolution_sha256": provenance[
                                    "server_selection_resolution_sha256"
                                ]
                            },
                        },
                        "supervisor_sha256": record["supervisor_sha256"],
                        "workflow_sha256": provenance["source_workflow_sha256"],
                        "zed_archive_sha256": provenance["zed_archive_sha256"],
                        "zed_sha256": provenance["zed_binary_sha256"],
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
            h264_client_policy="adaptive-alpha",
            lifecycle="application-exit",
            network_profile=job.DEFAULT_NETWORK_PROFILE,
            render_node=None,
            run="hardware-profile",
            selection="stacks/develop",
            zed_directory=None,
        )
        captured: list[dict[str, object]] = []
        captured_prelaunch: list[dict[str, object]] = []
        provenance = self.record(args.run)["input_provenance"]
        provenance["path"] = str(job.RESULT_ROOT / args.run / "inputs")
        provenance["zed_archive_sha256"] = None
        provenance["zed_binary_sha256"] = None
        provenance["server_selection"] = args.selection

        def launch(**kwargs: object) -> dict[str, object]:
            captured.append(dict(kwargs))
            record = dict(kwargs["record"])
            if record.get("kind") == "input-freeze":
                captured_prelaunch.append(
                    json.loads(
                        job.freeze_prelaunch_path(args.run).read_text(encoding="utf-8")
                    )
                )
                harness = Path(record["staging"]) / "inputs" / "harness"
                for source in job.HARNESS_INPUTS:
                    destination = harness / source.relative_to(job.LAB_ROOT)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(source.read_bytes())
            record["process"] = {"pid": 12345 + len(captured)}
            return record

        with (
            patch.object(job, "validate_selector"),
            patch.object(job.background_job, "launch", side_effect=launch),
            patch.object(
                job.background_job,
                "wait_process",
                return_value={"state": "completed", "exit_code": 0},
            ),
            patch.object(job, "load_freeze_result", return_value=provenance),
            patch.object(job.live_run, "load_bound_inputs"),
            patch.object(job, "cleanup_freeze_state"),
        ):
            self.assertEqual(job.start(args), 0)
        self.assertEqual(len(captured), 2)
        record = captured[1]["record"]
        self.assertEqual(len(captured_prelaunch), 1)
        self.assertEqual(captured_prelaunch[0]["job_id"], record["job_id"])
        self.assertEqual(
            captured_prelaunch[0]["freeze_owner"],
            str(job.freeze_record_path(args.run)),
        )
        self.assertFalse(job.freeze_prelaunch_path(args.run).exists())
        freeze_argv = captured[0]["argv"]
        self.assertIn("_freeze", freeze_argv)
        self.assertEqual(record["application"], "hardware")
        self.assertEqual(record["network_profile"], args.network_profile)
        argv = captured[1]["argv"]
        self.assertEqual(argv[:2], [sys.executable, "-B"])
        self.assertEqual(
            Path(argv[2]),
            Path(provenance["path"])
            / "harness"
            / job.RUNNER.relative_to(job.LAB_ROOT),
        )
        self.assertEqual(argv[argv.index("--application") + 1], "hardware")
        self.assertEqual(argv[argv.index("--lifecycle") + 1], "application-exit")
        self.assertEqual(
            argv[argv.index("--network-profile") + 1],
            args.network_profile,
        )
        self.assertIn("--bound-inputs", argv)
        self.assertNotIn("--zed-directory", argv)
        self.assertEqual(
            captured[1]["environment"]["XPRA_LAB_JOB_ID"],
            record["job_id"],
        )

    def test_main_launch_retention_preserves_frozen_inputs_and_prelaunch(self) -> None:
        args = Namespace(
            alpha_scenarios="default",
            application="hardware",
            encoding="h264",
            h264_client_policy="adaptive-alpha",
            lifecycle="application-exit",
            network_profile=job.DEFAULT_NETWORK_PROFILE,
            render_node=None,
            run="retained-main-launch",
            selection="stacks/develop",
            zed_directory=None,
        )
        provenance = self.record(args.run)["input_provenance"]
        provenance["path"] = str(job.RESULT_ROOT / args.run / "inputs")
        provenance["zed_archive_sha256"] = None
        provenance["zed_binary_sha256"] = None
        provenance["server_selection"] = args.selection
        launches = 0

        def launch(**kwargs: object) -> dict[str, object]:
            nonlocal launches
            launches += 1
            record = dict(kwargs["record"])
            if launches == 2:
                raise job.background_job.LaunchStateRetained("retained")
            harness = Path(record["staging"]) / "inputs" / "harness"
            for source in job.HARNESS_INPUTS:
                destination = harness / source.relative_to(job.LAB_ROOT)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(source.read_bytes())
            record["process"] = {"pid": 12345}
            return record

        with (
            patch.object(job, "validate_selector"),
            patch.object(job.background_job, "launch", side_effect=launch),
            patch.object(
                job.background_job,
                "wait_process",
                return_value={"state": "completed", "exit_code": 0},
            ),
            patch.object(job, "load_freeze_result", return_value=provenance),
            patch.object(job.live_run, "load_bound_inputs"),
            patch.object(job, "cleanup_freeze_state") as cleanup,
            self.assertRaisesRegex(job.JobError, "retained"),
        ):
            job.start(args)

        self.assertEqual(launches, 2)
        self.assertTrue((job.RESULT_ROOT / args.run / "inputs").is_dir())
        self.assertTrue(job.freeze_prelaunch_path(args.run).is_file())
        cleanup.assert_not_called()

    def test_start_rejects_a_clean_server_selection(self) -> None:
        args = Namespace(
            alpha_scenarios="default",
            application="zed",
            encoding="rgb",
            h264_client_policy="strict",
            lifecycle="application-exit",
            network_profile=job.DEFAULT_NETWORK_PROFILE,
            render_node=None,
            run="clean-selection",
            selection=None,
            zed_directory=None,
        )
        with self.assertRaisesRegex(job.JobError, "requires one non-empty"):
            job.start(args)

    def test_start_holds_the_lifecycle_lock_through_owner_publication(self) -> None:
        args = Namespace(run="locked-start")
        events: list[str] = []

        @contextmanager
        def lifecycle(run: str):
            self.assertEqual(run, args.run)
            events.append("lock-enter")
            try:
                yield object()
            finally:
                events.append("lock-exit")

        def start_locked(received: Namespace, run: str) -> int:
            self.assertIs(received, args)
            self.assertEqual(run, args.run)
            events.append("start")
            return 0

        with (
            patch.object(job, "prepare_private_state"),
            patch.object(job, "lifecycle_lock", lifecycle),
            patch.object(job, "_start_locked", side_effect=start_locked),
        ):
            self.assertEqual(job.start(args), 0)
        self.assertEqual(events, ["lock-enter", "start", "lock-exit"])

    def test_current_image_validation_ignores_only_non_lab_labels(self) -> None:
        images: dict[str, dict[str, object]] = {}
        inspections: list[subprocess.CompletedProcess[str]] = []
        for index, (name, role) in enumerate(
            (("client", "client-image"), ("server", "server-image")), start=1
        ):
            image_id = str(index) * 64
            labels = {
                "io.xpra.lab.context": str(index + 2) * 64,
                "io.xpra.lab.owner": "live",
                "io.xpra.lab.role": role,
                "io.xpra.lab.source": "a" * 40,
            }
            images[name] = {"id": image_id, "labels": labels}
            inspections.append(
                completed(
                    ["podman", "image", "inspect", image_id],
                    json.dumps(
                        [
                            {
                                "Id": "sha256:" + image_id,
                                "Labels": {
                                    **labels,
                                    "org.opencontainers.image.title": "base",
                                },
                            }
                        ]
                    ),
                )
            )
        with patch.object(job, "command", side_effect=inspections):
            self.assertTrue(job.current_image_validation({"images": images}))

        tampered = json.loads(inspections[0].stdout)
        tampered[0]["Labels"]["io.xpra.lab.unexpected"] = "value"
        with patch.object(
            job,
            "command",
            side_effect=[
                completed(
                    ["podman", "image", "inspect", "1" * 64],
                    json.dumps(tampered),
                ),
                inspections[1],
            ],
        ):
            self.assertFalse(job.current_image_validation({"images": images}))

    def test_status_routes_a_prelaunch_input_freeze(self) -> None:
        run = "freezing"
        job.prepare_private_state()
        job.freeze_record_path(run).write_text("{}\n", encoding="utf-8")
        job.freeze_record_path(run).chmod(0o600)
        freeze = {"job_id": JOB_ID, "run": run, "staging": "/tmp/staging"}
        output = StringIO()
        with (
            patch.object(job, "load_freeze_record", return_value=freeze),
            patch.object(
                job,
                "freeze_process_state",
                return_value={"state": "running", "pid": 12345},
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(job.status(Namespace(run=run)), 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["phase"], "input-freeze")
        self.assertEqual(payload["input_freeze"]["state"], "running")

    def test_main_and_freeze_records_bind_background_paths_to_the_run(self) -> None:
        job.prepare_private_state()
        for kind in ("main", "freeze"):
            with self.subTest(kind=kind):
                run = f"wrong-{kind}-process-path"
                record = self.record(run)
                if kind == "main":
                    path = job.record_path(run)
                    loader = lambda run=run: job.load_record(
                        run, require_current=False
                    )
                else:
                    record.update(
                        {
                            "application": "hardware",
                            "freeze_result": str(job.freeze_result_path(run)),
                            "kind": "input-freeze",
                            "result": str(job.RESULT_ROOT / run),
                            "staging": str(job.freeze_staging_path(run, JOB_ID)),
                        }
                    )
                    record.pop("alpha_scenarios")
                    record.pop("encoding")
                    record.pop("h264_client_policy")
                    record.pop("input_provenance")
                    record.pop("lifecycle")
                    record.pop("network_profile")
                    record.pop("render_node")
                    record.pop("result_report")
                    record["schema"] = 2
                    path = job.freeze_record_path(run)
                    loader = lambda run=run: job.load_freeze_record(run)
                record["process"]["runtime_log"] = str(self.root / "outside.log")
                self.write_private_json(path, record)
                with self.assertRaisesRegex(job.JobError, "runtime log is outside"):
                    loader()
                record["process"]["runtime_log"] = str(
                    job.runtime_log_path(run)
                    if kind == "main"
                    else job.freeze_runtime_log_path(run)
                )
                record["process"]["completion"] = str(
                    self.root / "outside-completion.json"
                )
                path.write_text(json.dumps(record) + "\n", encoding="utf-8")
                path.chmod(0o600)
                with self.assertRaisesRegex(job.JobError, "completion is outside"):
                    loader()

    def test_ownerless_freeze_prelaunch_is_visible_and_exactly_abortable(self) -> None:
        run = "freeze-ownerless"
        job.prepare_private_state()
        marker = self.freeze_prelaunch(run)
        job.publish_json(job.freeze_prelaunch_path(run), marker)
        job.freeze_runtime_log_path(run).write_text(
            "owner publication interrupted\n", encoding="utf-8"
        )
        job.freeze_runtime_log_path(run).chmod(0o600)
        output = StringIO()
        with (
            patch.object(job.background_job, "process_identity", return_value=None),
            redirect_stdout(output),
        ):
            self.assertEqual(job.status(Namespace(run=run)), 0)
        status = json.loads(output.getvalue())
        self.assertEqual(status["phase"], "input-freeze-prelaunch")
        self.assertFalse(status["active"])
        with patch.object(job.background_job, "process_identity", return_value=None):
            self.assertEqual(job.abort(Namespace(run=run)), 0)
        self.assertFalse(job.freeze_prelaunch_path(run).exists())
        self.assertFalse(job.freeze_runtime_log_path(run).exists())

    def test_ownerless_freeze_prelaunch_refuses_active_or_executed_state(self) -> None:
        for state_kind in ("active", "completion", "staging"):
            with self.subTest(state_kind=state_kind):
                run = f"freeze-{state_kind}"
                job.prepare_private_state()
                marker = self.freeze_prelaunch(run)
                job.publish_json(job.freeze_prelaunch_path(run), marker)
                identity = None
                if state_kind == "active":
                    identity = ("S", "12345", "42")
                elif state_kind == "completion":
                    self.write_private_json(job.freeze_completion_path(run), {})
                else:
                    Path(str(marker["staging"])).mkdir(mode=0o700)
                with (
                    patch.object(
                        job.background_job,
                        "process_identity",
                        return_value=identity,
                    ),
                    self.assertRaisesRegex(
                        job.JobError,
                        "still active|executed or ambiguous",
                    ),
                ):
                    job.abort(Namespace(run=run))
                self.assertTrue(job.freeze_prelaunch_path(run).is_file())

    def test_abort_routes_running_completed_and_lost_input_freezes(self) -> None:
        for state in ("running", "completed", "lost"):
            with self.subTest(state=state):
                run = f"freeze-{state}"
                job.prepare_private_state()
                job.freeze_record_path(run).write_text("{}\n", encoding="utf-8")
                job.freeze_record_path(run).chmod(0o600)
                freeze = {"job_id": JOB_ID, "run": run}
                with (
                    patch.object(job, "load_freeze_record", return_value=freeze),
                    patch.object(
                        job,
                        "freeze_process_state",
                        return_value={"state": state},
                    ),
                    patch.object(job.background_job, "terminate") as terminate,
                    patch.object(job, "cleanup_freeze_state") as cleanup,
                ):
                    self.assertEqual(job.abort(Namespace(run=run)), 0)
                if state == "running":
                    terminate.assert_called_once_with(freeze, require_current=False)
                else:
                    terminate.assert_not_called()
                cleanup.assert_called_once_with(
                    freeze,
                    remove_input_directories=True,
                )

    def test_abort_of_owned_freeze_removes_matching_external_prelaunch(self) -> None:
        run = "freeze-owned-prelaunch"
        job.prepare_private_state()
        prelaunch = self.freeze_prelaunch(run)
        job.publish_json(job.freeze_prelaunch_path(run), prelaunch)
        self.write_private_json(job.freeze_record_path(run), {})
        freeze = {
            key: prelaunch[key]
            for key in (
                "application",
                "background_supervisor_sha256",
                "freeze_result",
                "harness_sha256",
                "job_id",
                "result",
                "run",
                "runner_sha256",
                "selection",
                "staging",
                "supervisor_sha256",
            )
        }
        with (
            patch.object(job, "load_freeze_record", return_value=freeze),
            patch.object(
                job,
                "freeze_process_state",
                return_value={"state": "completed"},
            ),
            patch.object(job, "cleanup_freeze_state"),
        ):
            self.assertEqual(job.abort(Namespace(run=run)), 0)
        self.assertFalse(job.freeze_prelaunch_path(run).exists())

    def test_input_freeze_abort_retries_after_partial_directory_removal(self) -> None:
        run = "freeze-abort-retry"
        job.prepare_private_state()
        staging = job.freeze_staging_path(run, JOB_ID)
        result = job.RESULT_ROOT / run
        for path in (staging, result):
            path.mkdir(mode=0o700)
            (path / "first").write_text("first\n", encoding="utf-8")
            (path / "second").write_text("second\n", encoding="utf-8")
        record = {
            "run": run,
            "staging": str(staging),
            "result": str(result),
        }
        self.write_private_json(job.freeze_record_path(run), record)
        real_rmtree = job.shutil.rmtree
        interrupted = False

        def interrupt_once(path: Path) -> None:
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                (Path(path) / "first").unlink()
                raise OSError("simulated crash during rmtree")
            real_rmtree(path)

        with (
            patch.object(job, "load_freeze_result", return_value={}),
            patch.object(job, "input_checksum_validation", return_value=True),
            patch.object(job.shutil, "rmtree", side_effect=interrupt_once),
            self.assertRaisesRegex(OSError, "simulated crash"),
        ):
            job.cleanup_freeze_state(record, remove_input_directories=True)
        self.assertTrue(job.freeze_abort_transaction_path(run).is_file())
        self.assertTrue(job.freeze_record_path(run).is_file())

        with (
            patch.object(job, "load_freeze_result", return_value={}),
            patch.object(job, "input_checksum_validation", return_value=True),
        ):
            job.cleanup_freeze_state(record, remove_input_directories=True)
        for path in (
            staging,
            result,
            job.freeze_abort_staging_path(run, "staging"),
            job.freeze_abort_staging_path(run, "result"),
            job.freeze_abort_transaction_path(run),
            job.freeze_record_path(run),
        ):
            self.assertFalse(path.exists())

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
            patch.object(job, "current_image_validation", return_value=True),
        ):
            self.assertEqual(job.collect(Namespace(run=run)), 0)
        self.assertEqual(job.log_path(run).read_bytes(), b"complete log\n")
        status = json.loads(job.status_path(run).read_text(encoding="utf-8"))
        self.assertEqual(status["schema"], 3)
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
            patch.object(job, "current_image_validation", return_value=True),
        ):
            self.assertEqual(job.collect(Namespace(run=run)), 1)
        status = json.loads(job.status_path(run).read_text(encoding="utf-8"))
        self.assertEqual(status["result"], "failed")
        self.assertEqual(status["schema"], 3)

    def test_report_validation_rejects_mutated_scenario_evidence(self) -> None:
        run = "mutated-evidence"
        record = self.record(run)
        job.prepare_private_state()
        self.make_report(run, record)
        evidence = job.result_path(run).parent / "default-alpha" / "evidence.txt"
        evidence.write_text("changed\n", encoding="utf-8")
        _result, _digest, checks = job.report_validation(run, record)
        self.assertFalse(checks["evidence_tree"])

    def test_report_validation_rejects_nonprivate_scenario_evidence(self) -> None:
        run = "public-evidence"
        record = self.record(run)
        job.prepare_private_state()
        self.make_report(run, record)
        evidence = job.result_path(run).parent / "default-alpha" / "evidence.txt"
        evidence.chmod(0o644)
        _result, _digest, checks = job.report_validation(run, record)
        self.assertFalse(checks["evidence_tree"])

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
        self.assertTrue((self.job_root / ".lifecycle.lock").is_file())

    def test_lifecycle_lock_reuses_an_unlocked_file_after_a_crash(self) -> None:
        job.prepare_private_state()
        path = self.job_root / ".lifecycle.lock"
        path.write_text("", encoding="utf-8")
        path.chmod(0o600)
        with job.lifecycle_lock("stale"):
            pass
        with job.lifecycle_lock("stale"):
            pass

    def test_lifecycle_lock_rejects_a_fifo(self) -> None:
        job.prepare_private_state()
        path = self.job_root / ".lifecycle.lock"
        os.mkfifo(path, 0o600)
        with (
            self.assertRaisesRegex(job.JobError, "unsafe live lifecycle lock"),
            job.lifecycle_lock("unsafe"),
        ):
            pass

    def test_missing_report_failed_collection_can_still_be_removed(self) -> None:
        run = "missing-report"
        record = self.record(run)
        job.prepare_private_state()
        job.runtime_log_path(run).write_bytes(b"runner failed before report\n")
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
        self.assertEqual(status["report_checks"], {})
        self.assertEqual(status["result"], "failed")
        for path in (job.record_path(run), job.completion_path(run)):
            path.write_text("{}\n", encoding="utf-8")
            path.chmod(0o600)
        with (
            patch.object(job, "load_record", return_value=record),
            patch.object(job.background_job, "process_state", return_value=completed_state),
            patch.object(job, "remove_owned_objects"),
        ):
            self.assertEqual(job.remove(Namespace(run=run)), 0)
        self.assertFalse(job.record_path(run).exists())

    def test_verify_collected_uses_the_sealed_collection_rules(self) -> None:
        run = "sealed-collection"
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
            patch.object(job, "current_image_validation", return_value=True),
        ):
            self.assertEqual(job.collect(Namespace(run=run)), 0)

        with patch.object(
            job,
            "report_validation",
            side_effect=AssertionError(
                "removal must not reinterpret sealed evidence with current rules"
            ),
        ) as current_validation:
            job.verify_collected(run, record)
        current_validation.assert_not_called()

        report = job.result_path(run)
        report.write_bytes(report.read_bytes() + b"\n")
        with self.assertRaisesRegex(job.JobError, "report digest"):
            job.verify_collected(run, record)

    def test_remove_deletes_only_exactly_labelled_objects(self) -> None:
        run = "exact-labels"
        record = self.record(run)
        job.prepare_private_state()
        log = b"complete log\n"
        job.log_path(run).write_bytes(log)
        job.log_path(run).chmod(0o600)
        self.make_report(run, record)
        report_result, report_sha256, report_checks = job.report_validation(run, record)
        report_checks["current_images"] = True
        self.write_private_json(
            job.status_path(run),
            {
                "background_supervisor_sha256": record[
                    "background_supervisor_sha256"
                ],
                "exit_code": 0,
                "harness_sha256": record["harness_sha256"],
                "input_provenance": record["input_provenance"],
                "job_id": JOB_ID,
                "log_sha256": hashlib.sha256(log).hexdigest(),
                "logs_ok": True,
                "owner": job.OWNER,
                "owned_objects_remaining": {"containers": [], "networks": []},
                "process_pid": 12345,
                "report": str(job.result_path(run)),
                "report_checks": report_checks,
                "report_result": report_result,
                "report_sha256": report_sha256,
                "result": "success",
                "run": run,
                "runner_sha256": record["runner_sha256"],
                "schema": 3,
                "supervisor_sha256": record["supervisor_sha256"],
                "validation_ok": True,
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
            "container": ["a" * 64],
            "network": ["b" * 64],
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
            patch.object(
                job,
                "object_ledger_entries",
                return_value=[
                    {
                        "id": "a" * 64,
                        "kind": "container",
                        "labels": labels,
                        "name": "container-exact",
                    },
                    {
                        "id": "b" * 64,
                        "kind": "network",
                        "labels": labels,
                        "name": "network-exact",
                    },
                ],
            ),
            patch.object(
                job,
                "podman_object",
                side_effect=lambda kind, object_id: (
                    object_id,
                    "container-exact" if kind == "container" else "network-exact",
                    labels,
                ),
            ),
            patch.object(job, "command", side_effect=remove_command),
            patch.object(
                job,
                "current_image_validation",
                side_effect=AssertionError("remove must not inspect mutable image cache"),
            ) as current_images,
        ):
            self.assertEqual(job.remove(Namespace(run=run)), 0)
        current_images.assert_not_called()
        self.assertEqual(
            removals,
            [
                ["podman", "rm", "--force", "a" * 64],
                ["podman", "network", "rm", "b" * 64],
            ],
        )
        self.assertFalse(job.record_path(run).exists())
        self.assertTrue(job.status_path(run).exists())

    def test_remove_retries_after_a_crash_with_its_retained_transaction(self) -> None:
        run = "remove-crash"
        record = {
            "job_id": JOB_ID,
            "owner": job.OWNER,
            "process": {
                "completion": str(job.completion_path(run)),
                "runtime_log": str(job.runtime_log_path(run)),
            },
            "result_report": str(job.result_path(run)),
            "run": run,
            "schema": 4,
        }
        job.prepare_private_state()
        for path, payload in (
            (job.log_path(run), b"collected log\n"),
            (job.status_path(run), b"{}\n"),
            (job.record_path(run), b"{}\n"),
            (job.runtime_log_path(run), b"runtime\n"),
            (job.completion_path(run), b"{}\n"),
        ):
            path.write_bytes(payload)
            path.chmod(0o600)
        remove_objects = Mock(side_effect=[RuntimeError("simulated crash"), None])
        with (
            patch.object(job, "load_record", return_value=record),
            patch.object(job, "verify_collected"),
            patch.object(
                job.background_job,
                "process_state",
                return_value={"state": "completed", "exit_code": 0},
            ),
            patch.object(job, "remove_owned_objects", remove_objects),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                job.remove(Namespace(run=run))
            self.assertTrue(job.remove_transaction_path(run).is_file())
            self.assertTrue(job.record_path(run).is_file())
            self.assertEqual(job.remove(Namespace(run=run)), 0)
        for path in (
            job.record_path(run),
            job.runtime_log_path(run),
            job.completion_path(run),
        ):
            self.assertFalse(path.exists())
        self.assertTrue(job.remove_transaction_path(run).is_file())

    def test_removed_status_and_logs_use_only_the_retained_transaction(self) -> None:
        run = "removed-read-only"
        _record, log = self.retained_remove(run)
        retained = (
            job.log_path(run),
            job.status_path(run),
            job.remove_transaction_path(run),
        )
        before = {path: path.read_bytes() for path in retained}

        status_output = StringIO()
        with (
            patch.object(
                job,
                "load_freeze_prelaunch",
                side_effect=AssertionError("removed status must not use freeze state"),
            ),
            patch.object(
                job,
                "owned_objects",
                side_effect=AssertionError("removed status must not inspect Podman"),
            ),
            redirect_stdout(status_output),
        ):
            self.assertEqual(job.status(Namespace(run=run)), 0)
        self.assertEqual(
            json.loads(status_output.getvalue()),
            {"phase": "removed", "run": run},
        )

        logs_output = Mock(buffer=BytesIO())
        with (
            patch.object(
                job,
                "load_freeze_prelaunch",
                side_effect=AssertionError("removed logs must not use freeze state"),
            ),
            patch.object(job.sys, "stdout", logs_output),
        ):
            self.assertEqual(job.logs(Namespace(run=run)), 0)
        self.assertEqual(logs_output.buffer.getvalue(), log)
        self.assertEqual({path: path.read_bytes() for path in retained}, before)

    def test_removed_read_routes_reject_changed_schema_and_evidence(self) -> None:
        for corruption in ("schema", "log", "status"):
            with self.subTest(corruption=corruption):
                run = f"removed-corrupt-{corruption}"
                self.retained_remove(run)
                if corruption == "schema":
                    transaction = json.loads(
                        job.remove_transaction_path(run).read_text(encoding="utf-8")
                    )
                    transaction["schema"] = 2
                    job.remove_transaction_path(run).write_text(
                        json.dumps(transaction) + "\n",
                        encoding="utf-8",
                    )
                    job.remove_transaction_path(run).chmod(0o600)
                else:
                    path = (
                        job.log_path(run)
                        if corruption == "log"
                        else job.status_path(run)
                    )
                    path.write_bytes(path.read_bytes() + b"changed\n")
                    path.chmod(0o600)

                with self.assertRaises(job.JobError):
                    job.status(Namespace(run=run))
                logs_output = Mock(buffer=BytesIO())
                with (
                    patch.object(job.sys, "stdout", logs_output),
                    self.assertRaises(job.JobError),
                ):
                    job.logs(Namespace(run=run))
                self.assertEqual(logs_output.buffer.getvalue(), b"")

    def test_status_reports_an_interrupted_retained_removal(self) -> None:
        run = "remove-interrupted-status"
        self.retained_remove(run, complete=False)
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(job.status(Namespace(run=run)), 0)
        self.assertEqual(
            json.loads(output.getvalue()),
            {"phase": "removing", "run": run},
        )

    def test_remove_rejects_mismatched_object_labels(self) -> None:
        run = "mismatch"
        labels = {
            "io.xpra.lab.owner": "someone-else",
            "io.xpra.lab.run-id": run,
        }
        with (
            patch.object(
                job,
                "object_ledger_entries",
                return_value=[
                    {
                        "id": "a" * 64,
                        "kind": "container",
                        "labels": {
                            "io.xpra.lab.owner": "live",
                            "io.xpra.lab.run-id": run,
                        },
                        "name": "container",
                    }
                ],
            ),
            patch.object(
                job,
                "podman_ids",
                side_effect=lambda kind, _run: ["a" * 64] if kind == "container" else [],
            ),
            patch.object(
                job,
                "podman_object",
                return_value=("a" * 64, "container", labels),
            ),
            self.assertRaisesRegex(job.JobError, "no longer matches"),
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

    def test_abort_refuses_current_completed_but_discards_stale_completed(self) -> None:
        run = "completed-abort"
        record = self.record(run)
        job.prepare_private_state()
        for path in (
            job.record_path(run),
            job.runtime_log_path(run),
            job.completion_path(run),
        ):
            path.write_text("{}\n", encoding="utf-8")
            path.chmod(0o600)
        with (
            patch.object(job, "load_record", return_value=record),
            patch.object(
                job.background_job,
                "process_state",
                return_value={"state": "completed", "pid": 12345},
            ),
            patch.object(job, "remove_owned_objects") as remove_objects,
            self.assertRaisesRegex(job.JobError, "must be collected"),
        ):
            job.abort(Namespace(run=run))
        remove_objects.assert_not_called()

        stale = dict(record)
        stale["harness_sha256"] = "0" * 64
        with (
            patch.object(job, "load_record", return_value=stale),
            patch.object(
                job.background_job,
                "process_state",
                return_value={"state": "completed", "pid": 12345},
            ),
            patch.object(job, "remove_owned_objects") as remove_objects,
        ):
            self.assertEqual(job.abort(Namespace(run=run)), 0)
        remove_objects.assert_called_once_with(run)
        self.assertFalse(job.record_path(run).exists())

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

    def test_environment_path_bootstraps_without_site_packages(self) -> None:
        result = subprocess.run(
            [sys.executable, "-S", str(job.SUPERVISOR), "environment-path"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout.strip(), r"/venvs/live-[0-9a-f]{16}$")

    def test_environment_partial_recovery_requires_its_exact_marker(self) -> None:
        self.venv_root.mkdir(mode=0o700)
        partial = job.environment_partial_path()
        partial.mkdir(mode=0o700)
        (partial / "stale").write_text("stale\n", encoding="utf-8")
        self.write_private_json(
            job.environment_partial_marker_path(),
            {
                "schema": 1,
                "owner": job.OWNER,
                "kind": "live-environment-partial",
                "partial": str(partial),
                "destination": str(self.venv_root / ("live-" + "1" * 16)),
                "requirements_sha256": "2" * 64,
                "python_version": "old interpreter",
            },
        )
        job.recover_environment_partial()
        self.assertFalse(partial.exists())
        self.assertFalse(job.environment_partial_marker_path().exists())

        partial.mkdir(mode=0o700)
        self.write_private_json(job.environment_partial_marker_path(), {})
        with self.assertRaisesRegex(job.JobError, "owner mismatch"):
            job.recover_environment_partial()
        self.assertTrue(partial.is_dir())
        self.assertTrue(job.environment_partial_marker_path().is_file())

    def test_environment_child_inherits_the_recovery_lock(self) -> None:
        self.venv_root.mkdir(mode=0o700)
        read_gate, write_gate = os.pipe()
        child: subprocess.Popen[bytes] | None = None
        competitor = -1
        try:
            with job.environment_lock() as lock_descriptor:
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        "import os,sys; os.read(int(sys.argv[1]), 1)",
                        str(read_gate),
                    ],
                    pass_fds=(lock_descriptor, read_gate),
                )
                os.close(read_gate)
                read_gate = -1
            competitor = os.open(self.venv_root / ".environment.lock", os.O_RDWR)
            with self.assertRaises(BlockingIOError):
                fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.write(write_gate, b"x")
            os.close(write_gate)
            write_gate = -1
            child.wait(timeout=5)
            fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            if read_gate >= 0:
                os.close(read_gate)
            if write_gate >= 0:
                os.close(write_gate)
            if child is not None and child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            if competitor >= 0:
                os.close(competitor)

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
            return_value=completed(
                argv,
                json.dumps([{"id": "a" * 64, "name": "network", "labels": labels}]),
            ),
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
    def test_image_inspection_accepts_only_exact_lab_provenance(self) -> None:
        expected = {
            "io.xpra.lab.context": "1" * 64,
            "io.xpra.lab.owner": "live",
            "io.xpra.lab.role": "server-image",
            "io.xpra.lab.source": "2" * 40,
        }
        inspection = {
            "Id": "sha256:" + "3" * 64,
            "Labels": {
                **expected,
                "io.buildah.version": "1.42.1",
                "org.opencontainers.image.title": "ubuntu",
            },
        }
        with patch.object(
            live_run,
            "run",
            return_value=completed(
                ["podman", "image", "inspect"], json.dumps([inspection])
            ),
        ):
            observed = live_run.inspect_lab_image(
                "image",
                role="server-image",
                source_commit="2" * 40,
                context_digest="1" * 64,
            )
        self.assertEqual(observed["labels"], expected)

        inspection["Labels"]["io.xpra.lab.unexpected"] = "value"
        with (
            patch.object(
                live_run,
                "run",
                return_value=completed(
                    ["podman", "image", "inspect"], json.dumps([inspection])
                ),
            ),
            self.assertRaisesRegex(live_run.LabFailure, "provenance labels"),
        ):
            live_run.inspect_lab_image(
                "image",
                role="server-image",
                source_commit="2" * 40,
                context_digest="1" * 64,
            )

    def test_frame_poll_reads_bounded_deltas_without_repulling_full_logs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            screenshot = directory / "screen-updates" / "1" / "1" / "screenshot.png"
            screenshot.parent.mkdir(parents=True)
            screenshot.write_bytes(b"png")
            server_log = "commit wid 1 rects=[(0, 0, 1, 1)]\nrgb_encode using RGBX\n"
            client_log = (
                "cairo._do_paint_rgb\nrecord_decode_time(True,\n"
                "draw_widget(\ncairo_draw: window size=\n"
            )

            def deltas(container: str, offsets: dict[str, int]) -> dict[str, tuple[int, str]]:
                values = {
                    "server.stderr": server_log if container == "server" else "",
                    "client.stdout": client_log if container == "client" else "",
                    "client.stderr": "",
                }
                return {
                    name: (offset + len(values[name].encode()), values[name])
                    for name, offset in offsets.items()
                }

            def wait_once(_description: str, predicate: object, **_kwargs: object) -> None:
                self.assertTrue(predicate())  # type: ignore[operator]

            with (
                patch.object(live_run, "read_container_log_deltas", side_effect=deltas) as read,
                patch.object(live_run, "container_artifact_files", return_value=()),
                patch.object(
                    live_run,
                    "analyze_png",
                    return_value={"quantized_rgb_colors": 64},
                ),
                patch.object(live_run, "wait_for", side_effect=wait_once),
                patch.object(live_run, "container_process_exists", return_value=True),
                patch.object(live_run, "pull_container_artifacts") as pull,
            ):
                outcome = live_run.wait_for_frame_boundary(
                    "server",
                    101,
                    "client",
                    202,
                    directory,
                    "rgb",
                    "strict-hardware",
                    application="zed",
                    expected_xpra_wid=1,
                )
            self.assertEqual(outcome, "success")
            self.assertEqual(read.call_count, 2)
            pull.assert_not_called()

    def test_incremental_log_probe_caps_reads_at_its_fd_size_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "server.stderr"
            source.write_bytes(b"before\n")
            original_fstat = os.fstat
            first_fstat = True

            def grow_after_snapshot(descriptor: int) -> os.stat_result:
                nonlocal first_fstat
                details = original_fstat(descriptor)
                if first_fstat:
                    first_fstat = False
                    with source.open("ab") as stream:
                        stream.write(b"after\n")
                return details

            with (
                patch.object(live_run.os, "fstat", side_effect=grow_after_snapshot),
                patch.object(
                    live_run,
                    "podman_exec",
                    side_effect=execute_container_log_probe(root),
                ),
            ):
                result = live_run.read_container_log_deltas(
                    "server",
                    {"server.stderr": 0},
                    markers={"server.stderr": ("before",)},
                )
            grown = source.read_bytes()

        self.assertEqual(result, {"server.stderr": (7, "before\n")})
        self.assertEqual(grown, b"before\nafter\n")

    def test_incremental_log_probe_rejects_unsafe_or_truncated_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "target"
            target.write_text("value\n", encoding="utf-8")
            (root / "symlink.stderr").symlink_to(target.name)
            os.mkfifo(root / "fifo.stderr", mode=0o600)
            truncated = root / "truncated.stderr"
            truncated.write_bytes(b"abc")

            execute = execute_container_log_probe(root)
            for name, offset, error in (
                ("symlink.stderr", 0, "unsafe"),
                ("fifo.stderr", 0, "unsafe"),
                ("truncated.stderr", 4, "truncated"),
            ):
                with (
                    self.subTest(name=name),
                    patch.object(live_run, "podman_exec", side_effect=execute),
                    self.assertRaisesRegex(live_run.LabFailure, error),
                ):
                    live_run.read_container_log_deltas(
                        "server",
                        {name: offset},
                        markers={name: ("value",)},
                    )

    def test_incremental_log_probe_skips_large_unmatched_debug_output(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "server.stderr"
            noise = b"unmatched debug output\n" * 400_000
            marker = b"commit wid 1 rects=[(0, 0, 1, 1)]\n"
            source.write_bytes(noise + marker)
            with patch.object(
                live_run,
                "podman_exec",
                side_effect=execute_container_log_probe(root),
            ):
                result = live_run.read_container_log_deltas(
                    "server",
                    {"server.stderr": 0},
                )

        self.assertGreater(len(noise), live_run.FRAME_LOG_TOTAL_BYTES)
        self.assertEqual(
            result,
            {"server.stderr": (len(noise) + len(marker), marker.decode())},
        )

    def test_live_artifact_pull_keeps_strict_archive_validation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            destination = Path(raw)
            with patch.object(
                live_run.container_payload,
                "merge_from_process",
            ) as merge:
                live_run.pull_container_artifacts(
                    "server",
                    destination,
                    ("server.stderr",),
                )
        command = merge.call_args.args[0]
        self.assertEqual(
            command[:6],
            [
                "podman",
                "exec",
                "server",
                "python3",
                live_run.CONTAINER_PAYLOAD,
                "create",
            ],
        )

    def test_container_artifact_size_reads_only_exact_remote_metadata(self) -> None:
        with patch.object(
            live_run,
            "podman_exec",
            return_value=completed(["stat"], "123\n"),
        ) as execute:
            self.assertEqual(
                live_run.container_artifact_size("server", "server.stderr"),
                123,
            )
        execute.assert_called_once_with(
            "server",
            [
                "python3",
                "-c",
                ANY,
                "/artifacts/server.stderr",
            ],
            check=False,
            announce=False,
        )
        probe = execute.call_args.args[1][2]
        self.assertIn("os.lstat", probe)
        self.assertIn("stat.S_ISREG", probe)

        with (
            patch.object(
                live_run,
                "podman_exec",
                return_value=completed(["python3"], returncode=2),
            ),
            self.assertRaisesRegex(live_run.LabFailure, "not a regular file"),
        ):
            live_run.container_artifact_size("server", "server.stderr")

    def test_container_artifact_suffix_query_is_bounded(self) -> None:
        with patch.object(
            live_run,
            "podman_exec",
            return_value=completed(["python3"], returncode=2),
        ) as execute, self.assertRaisesRegex(live_run.LabFailure, "exceeds its limit"):
            live_run.container_artifact_suffix_matches(
                "server",
                "server.stderr",
                42,
                ("marker",),
            )
        command = execute.call_args.args[1]
        self.assertEqual(command[-3:], ["42", str(live_run.FRAME_LOG_TOTAL_BYTES), "marker"])

    def test_wait_for_log_does_not_pull_a_log_while_its_writer_is_active(self) -> None:
        def wait_once(_description: str, predicate: object, **_kwargs: object) -> None:
            self.assertTrue(predicate())  # type: ignore[operator]

        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "server.stderr"
            with (
                patch.object(live_run, "container_artifact_contains", return_value=True),
                patch.object(live_run, "container_process_exists") as process_exists,
                patch.object(live_run, "pull_container_artifacts") as pull,
                patch.object(live_run, "wait_for", side_effect=wait_once),
            ):
                live_run.wait_for_log(
                    "server",
                    101,
                    path,
                    "ready",
                    "server readiness",
                )
        process_exists.assert_not_called()
        pull.assert_not_called()

    def test_server_tcp_endpoint_retries_from_the_client_until_reachable(self) -> None:
        probes = [
            completed(["python3"], returncode=75),
            completed(["python3"]),
        ]

        def wait_twice(_description: str, predicate: object, **_kwargs: object) -> None:
            self.assertFalse(predicate())  # type: ignore[operator]
            self.assertTrue(predicate())  # type: ignore[operator]

        with tempfile.TemporaryDirectory() as raw:
            server_log = Path(raw) / "server.stderr"
            with (
                patch.object(live_run, "container_process_exists", return_value=True) as alive,
                patch.object(live_run, "podman_exec", side_effect=probes) as execute,
                patch.object(live_run, "pull_container_artifacts") as pull,
                patch.object(live_run, "wait_for", side_effect=wait_twice),
            ):
                live_run.wait_for_server_tcp_endpoint(
                    "server",
                    101,
                    "client",
                    "xpra-server",
                    14500,
                    server_log,
                )

        self.assertEqual(alive.call_count, 4)
        self.assertEqual(execute.call_count, 2)
        for call in execute.call_args_list:
            self.assertEqual(call.args[0], "client")
            self.assertEqual(call.args[1][-2:], ["xpra-server", "14500"])
            self.assertEqual(call.kwargs, {"announce": False, "check": False})
        pull.assert_not_called()

    def test_server_tcp_endpoint_collects_complete_server_evidence_after_exit(self) -> None:
        events: list[str] = []

        def stopped(_container: str, _pid: int) -> bool:
            events.append("server-exited")
            return False

        def pull_server(
            container: str,
            destination: Path,
            role: str,
        ) -> None:
            events.append("pull-server")
            self.assertEqual(container, "server")
            self.assertEqual(role, "server")
            (destination / "server.stderr").write_text(
                "server failed\n",
                encoding="utf-8",
            )
            (destination / "interaction.stderr").write_text(
                "interaction failed\n",
                encoding="utf-8",
            )

        def wait_once(_description: str, predicate: object, **_kwargs: object) -> None:
            predicate()  # type: ignore[operator]

        with tempfile.TemporaryDirectory() as raw:
            server_log = Path(raw) / "server.stderr"
            with (
                patch.object(live_run, "container_process_exists", side_effect=stopped),
                patch.object(live_run, "pull_all_container_artifacts", side_effect=pull_server),
                patch.object(live_run, "podman_exec") as execute,
                patch.object(live_run, "wait_for", side_effect=wait_once),
                self.assertRaisesRegex(
                    live_run.LabFailure,
                    "server failed",
                ),
            ):
                live_run.wait_for_server_tcp_endpoint(
                    "server",
                    101,
                    "client",
                    "xpra-server",
                    14500,
                    server_log,
                )

        self.assertEqual(events, ["server-exited", "pull-server"])
        execute.assert_not_called()

    def test_failure_quiescence_stops_only_exact_workload_pids(self) -> None:
        running = {("client", 202), ("server", 101)}
        terminations: list[tuple[str, int]] = []

        def process_exists(container: str, pid: int) -> bool:
            return (container, pid) in running

        def execute(
            container: str,
            command: list[str],
            **kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            self.assertEqual(kwargs, {"announce": False, "check": False})
            self.assertEqual(command[:2], ["kill", "-TERM"])
            pid = int(command[2])
            terminations.append((container, pid))
            running.remove((container, pid))
            return completed(command)

        with (
            patch.object(live_run, "container_process_exists", side_effect=process_exists),
            patch.object(live_run, "podman_exec", side_effect=execute),
        ):
            result = live_run.quiesce_failed_workloads(
                (
                    ("client", "client", 202),
                    ("server", "server", 101),
                    ("unused", "unused", 0),
                )
            )

        self.assertTrue(result["passed"])
        self.assertEqual(terminations, [("client", 202), ("server", 101)])
        self.assertEqual(
            [item["status"] for item in result["processes"]],
            ["exited", "exited", "not-started"],
        )

    def test_failure_quiescence_refuses_collection_while_a_pid_remains(self) -> None:
        with (
            patch.object(live_run, "container_process_exists", return_value=True),
            patch.object(
                live_run,
                "podman_exec",
                return_value=completed(["kill", "-TERM", "101"]),
            ),
        ):
            result = live_run.quiesce_failed_workloads(
                (("server", "server", 101),),
                timeout=0,
            )

        self.assertFalse(result["passed"])
        self.assertEqual(result["processes"][0]["status"], "still-running")

    def test_hardware_fixture_readiness_retries_until_both_children_are_ready(self) -> None:
        probes = [
            completed(["python3"], returncode=75),
            completed(["python3"]),
        ]

        def wait_twice(_description: str, predicate: object, **_kwargs: object) -> None:
            self.assertFalse(predicate())  # type: ignore[operator]
            self.assertTrue(predicate())  # type: ignore[operator]

        with (
            tempfile.TemporaryDirectory() as raw,
            patch.object(
                live_run,
                "container_process_exists",
                return_value=True,
            ) as alive,
            patch.object(live_run, "podman_exec", side_effect=probes) as execute,
            patch.object(live_run, "pull_all_container_artifacts") as pull,
            patch.object(live_run, "wait_for", side_effect=wait_twice),
        ):
            live_run.wait_for_hardware_fixture("server", 101, Path(raw), "hardware")

        self.assertEqual(alive.call_count, 4)
        self.assertEqual(execute.call_count, 2)
        for call in execute.call_args_list:
            self.assertEqual(call.args[0], "server")
            self.assertEqual(call.args[1][:2], ["python3", "-c"])
            self.assertEqual(call.args[1][-1], "vkcube")
            self.assertEqual(call.kwargs, {"announce": False, "check": False})
        pull.assert_not_called()

    def test_opengl_fixture_readiness_binds_the_glmark2_primary(self) -> None:
        with (
            tempfile.TemporaryDirectory() as raw,
            patch.object(live_run, "container_process_exists", return_value=True),
            patch.object(
                live_run,
                "podman_exec",
                return_value=completed(["python3"]),
            ) as execute,
            patch.object(live_run, "wait_for", side_effect=lambda _name, ready: ready()),
        ):
            live_run.wait_for_hardware_fixture("server", 101, Path(raw), "opengl")

        command = execute.call_args.args[1]
        self.assertEqual(command[-1], "opengl")

    def test_dead_hardware_child_stops_server_before_collecting_evidence(self) -> None:
        events: list[str] = []
        process_states = iter((True, True, False))

        def process_exists(_container: str, _pid: int) -> bool:
            state = next(process_states)
            events.append(f"server-alive={state}")
            return state

        def execute(
            _container: str,
            command: list[str],
            **_kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            if command[:2] == ["python3", "-c"]:
                events.append("child-dead")
                return completed(command, returncode=76)
            self.assertEqual(command, ["kill", "-TERM", "101"])
            events.append("stop-server")
            return completed(command)

        def collect(container: str, destination: Path, role: str) -> None:
            events.append("collect-server")
            self.assertEqual((container, role), ("server", "server"))
            (destination / "interaction.stderr").write_text(
                "GTK failed\n",
                encoding="utf-8",
            )
            (destination / "interaction.exit").write_text("1\n", encoding="ascii")

        def wait_control(
            description: str,
            predicate: object,
            **_kwargs: object,
        ) -> None:
            if description == "hardware fixture GTK and vulkan readiness":
                predicate()  # type: ignore[operator]
            else:
                self.assertTrue(predicate())  # type: ignore[operator]

        with (
            tempfile.TemporaryDirectory() as raw,
            patch.object(
                live_run,
                "container_process_exists",
                side_effect=process_exists,
            ),
            patch.object(live_run, "podman_exec", side_effect=execute),
            patch.object(live_run, "pull_all_container_artifacts", side_effect=collect),
            patch.object(live_run, "wait_for", side_effect=wait_control),
            self.assertRaisesRegex(live_run.LabFailure, "GTK failed"),
        ):
            live_run.wait_for_hardware_fixture("server", 101, Path(raw), "hardware")

        self.assertEqual(
            events,
            [
                "server-alive=True",
                "child-dead",
                "server-alive=True",
                "stop-server",
                "server-alive=False",
                "collect-server",
            ],
        )

    def test_zed_mouse_pulls_only_new_immutable_screenshots(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            live_run.Image.new("RGB", (8, 8), (240, 240, 240)).save(
                directory / "window-direct.rgb.png"
            )
            baseline = "screen-updates/1/1/screenshot.png"
            post_click = "screen-updates/1/2/screenshot.png"

            def wait_once(_description: str, predicate: object, **_kwargs: object) -> None:
                self.assertTrue(predicate())  # type: ignore[operator]

            def convert_after(_directory: Path, stem: str) -> dict[str, object]:
                live_run.Image.new("RGB", (8, 8), (10, 10, 10)).save(
                    directory / f"{stem}.rgb.png"
                )
                return {
                    "image": {
                        "dominant_rgb": [10, 10, 10],
                        "rgb_sha256": "after",
                    },
                    "xwd": {"unique_rgb_colors": 256},
                }

            with (
                patch.object(
                    live_run,
                    "container_artifact_size",
                    side_effect=lambda _container, relative: {
                        "client.stdout": 101,
                        "server.stderr": 202,
                        "zed.stderr": 303,
                    }[relative],
                ) as sizes,
                patch.object(
                    live_run,
                    "container_artifact_files",
                    side_effect=[(baseline,), (baseline, post_click)],
                ) as files,
                patch.object(
                    live_run,
                    "container_artifact_suffix_matches",
                    return_value=True,
                ),
                patch.object(
                    live_run,
                    "detect_zed_system_theme_control",
                    return_value={
                        "click_position": [4, 4],
                        "dark_bounds": [0, 0, 4, 8],
                        "system_bounds": [4, 0, 8, 8],
                    },
                ),
                patch.object(
                    live_run,
                    "theme_segment_contrast",
                    side_effect=[0.1, 0.9, 0.9, 0.1],
                ),
                patch.object(live_run, "capture_xwd"),
                patch.object(live_run, "convert_xwd", side_effect=convert_after),
                patch.object(
                    live_run,
                    "compare_rgb_images",
                    return_value={"same_size": True, "mean_absolute_error": 0.0},
                ),
                patch.object(live_run, "podman_exec", return_value=completed(["xdotool"])),
                patch.object(live_run, "pull_container_artifacts") as pull,
                patch.object(live_run, "wait_for", side_effect=wait_once),
            ):
                result = live_run.exercise_zed_mouse(
                    "server",
                    "client",
                    "0x1",
                    {"width": 8, "height": 8},
                    directory,
                    {
                        "image": {
                            "dominant_rgb": [240, 240, 240],
                            "rgb_sha256": "before",
                        }
                    },
                )

        self.assertTrue(all(result["input_path"].values()))
        self.assertEqual(sizes.call_count, 3)
        self.assertEqual(files.call_count, 2)
        pull.assert_called_once_with("server", directory, (post_click,))

    def test_adaptive_h264_frame_poll_unlocks_on_exact_first_frame(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            server_log = (
                "commit wid 1 rects=[(0, 0, 64, 64)]\n"
                "do_set_client_properties(encoding.full_csc_modes={'h264': ('YUV420P',)})\n"
            )
            client_log = (
                "register_window(..) window(0x1)=ClientWindow(0x1)\n"
                "draw_region(0, 0, 64, 63, h264\n"
                "choose_decoder('h264')=libva\n"
                "do_video_paint('h264', ImageWrapper(NV12\n"
                "record_decode_time(True, 1) wid=0x1, h264:\n"
                "do_present_fbo(\n"
                "register_window(..) window(0x2)=ClientWindow(0x2)\n"
            )
            remote_info = (
                "screen-updates/1/window.info",
                "screen-updates/1/1/0.info",
            )
            payload = "screen-updates/1/1/0.h264"
            pulls: list[tuple[str, ...]] = []

            def deltas(container: str, offsets: dict[str, int]) -> dict[str, tuple[int, str]]:
                values = {
                    "server.stderr": server_log if container == "server" else "",
                    "client.stdout": client_log if container == "client" else "",
                    "client.stderr": "",
                }
                return {
                    name: (offset + len(values[name].encode()), values[name])
                    for name, offset in offsets.items()
                }

            def pull(
                container: str,
                destination: Path,
                relatives: tuple[str, ...],
            ) -> None:
                self.assertEqual(container, "server")
                self.assertEqual(destination, directory)
                pulls.append(relatives)
                for relative in relatives:
                    target = directory / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if relative.endswith("/window.info"):
                        target.write_text(
                            json.dumps({"pixel-format": "BGRX"}),
                            encoding="utf-8",
                        )
                    elif relative.endswith(".info"):
                        target.write_text(
                            json.dumps(
                                {
                                    "encoding": "h264",
                                    "file": "0.h264",
                                    "h": 63,
                                    "options": {
                                        "flush": 0,
                                        "frame": 0,
                                        "type": "IDR",
                                        "window-size": [64, 64],
                                    },
                                    "sequence": 1,
                                    "w": 64,
                                    "x": 0,
                                    "y": 0,
                                }
                            ),
                            encoding="utf-8",
                        )
                    else:
                        target.write_bytes(b"h264")

            def wait_once(_description: str, predicate: object, **_kwargs: object) -> None:
                self.assertTrue(predicate())  # type: ignore[operator]

            with (
                patch.object(live_run, "read_container_log_deltas", side_effect=deltas),
                patch.object(
                    live_run,
                    "container_artifact_files",
                    return_value=remote_info,
                ),
                patch.object(live_run, "pull_container_artifacts", side_effect=pull),
                patch.object(live_run, "wait_for", side_effect=wait_once),
                patch.object(live_run, "container_process_exists", return_value=True),
            ):
                outcome = live_run.wait_for_frame_boundary(
                    "server",
                    101,
                    "client",
                    202,
                    directory,
                    "h264",
                    "adaptive-alpha",
                    application="hardware",
                    expected_xpra_wid=1,
                )
            self.assertEqual(outcome, "success")
            self.assertEqual(pulls, [remote_info, (payload,)])
            early_updates = live_run.parse_saved_updates(directory, 1)
            early_updates["initial_pixel_format"] = "BGRX"
            self.assertTrue(
                live_run.primary_h264_frame_ready(
                    "hardware", "adaptive-alpha", early_updates
                )
            )
            self.assertFalse(
                live_run.primary_h264_packets_valid(
                    "hardware", "adaptive-alpha", early_updates
                )
            )
            self.assertIsNone(
                live_run.hardware_h264_production_updates(early_updates)
            )

            def reject_wrong_window(
                _description: str, predicate: object, **_kwargs: object
            ) -> None:
                self.assertFalse(predicate())  # type: ignore[operator]
                raise live_run.LabFailure("exact target window did not produce a frame")

            with (
                patch.object(live_run, "read_container_log_deltas", side_effect=deltas),
                patch.object(
                    live_run,
                    "container_artifact_files",
                    return_value=remote_info,
                ),
                patch.object(live_run, "pull_container_artifacts", side_effect=pull),
                patch.object(live_run, "wait_for", side_effect=reject_wrong_window),
                patch.object(live_run, "container_process_exists", return_value=True),
                self.assertRaisesRegex(
                    live_run.LabFailure, "exact target window did not produce a frame"
                ),
            ):
                live_run.wait_for_frame_boundary(
                    "server",
                    101,
                    "client",
                    202,
                    directory,
                    "h264",
                    "adaptive-alpha",
                    application="hardware",
                    expected_xpra_wid=2,
                )
            self.assertEqual(pulls, [remote_info, (payload,)])

    def test_run04_singleton_cropped_h264_is_only_early_readiness(self) -> None:
        first_webp = {
            "encoding": "webp",
            "h": 1095,
            "options": {"flush": 0, "window-size": [1536, 1095]},
            "payload_bytes": 100,
            "relative_info": "screen-updates/1/280961706/0.info",
            "sequence": 1,
            "w": 1536,
            "x": 0,
            "y": 0,
        }
        h264 = {
            "encoding": "h264",
            "h": 1094,
            "options": {
                "flush": 0,
                "frame": 0,
                "profile": "main",
                "type": "IDR",
                "window-size": [1536, 1095],
            },
            "payload_bytes": 200,
            "relative_info": "screen-updates/1/280961714/0.info",
            "sequence": 2,
            "w": 1536,
            "x": 0,
            "y": 0,
        }
        later_webp = {
            "encoding": "webp",
            "h": 1173,
            "options": {"flush": 0, "window-size": [1596, 1173]},
            "payload_bytes": 300,
            "relative_info": "screen-updates/1/280961786/0.info",
            "sequence": 3,
            "w": 1596,
            "x": 0,
            "y": 0,
        }
        updates = {
            "count": 3,
            "encodings": ["h264", "webp"],
            "initial_pixel_format": "RGBX",
            "updates": [first_webp, h264, later_webp],
            "window_id": 1,
        }
        self.assertTrue(
            live_run.primary_h264_frame_ready(
                "zed",
                "adaptive-alpha",
                updates,
            )
        )
        self.assertIsNone(live_run.adaptive_h264_production_updates(updates))
        self.assertFalse(
            live_run.primary_h264_packets_valid(
                "zed",
                "adaptive-alpha",
                updates,
            )
        )

        stable_packets: list[dict[str, object]] = []
        for sequence, group, frame in ((4, 280967000, 0), (6, 280967051, 1)):
            stable_packets.extend(
                (
                    {
                        "encoding": "rgb24",
                        "h": 1,
                        "options": {
                            "flush": 1,
                            "rgb_format": "RGBX",
                            "window-size": [1596, 1173],
                        },
                        "payload_bytes": 40,
                        "relative_info": f"screen-updates/1/{group}/0.info",
                        "sequence": sequence,
                        "w": 1596,
                        "x": 0,
                        "y": 1172,
                    },
                    {
                        "encoding": "h264",
                        "h": 1172,
                        "options": {
                            "flush": 0,
                            "frame": frame,
                            "type": "IDR" if frame == 0 else "P",
                            "window-size": [1596, 1173],
                        },
                        "payload_bytes": 200,
                        "relative_info": f"screen-updates/1/{group}/1.info",
                        "sequence": sequence + 1,
                        "w": 1596,
                        "x": 0,
                        "y": 0,
                    },
                )
            )
        short_phase = {
            **updates,
            "count": 7,
            "encodings": ["h264", "rgb24", "webp"],
            "h264_stimulus": {
                "baseline_sequence": 3,
                "last_sequence": 7,
                "window_size": [1596, 1173],
            },
            "updates": [*updates["updates"], *stable_packets],
        }
        production = live_run.adaptive_h264_production_updates(short_phase)
        self.assertIsNotNone(production)
        assert production is not None
        self.assertEqual(
            [packet["sequence"] for packet in production["updates"]],
            [4, 5, 6, 7],
        )
        metrics = live_run.h264_production_metrics("zed", short_phase)
        self.assertEqual(metrics["h264_main_frame_count"], 2)
        self.assertEqual(metrics["h264_damage_span_ms"], 51)
        self.assertFalse(all(live_run.h264_dominance_checks(metrics).values()))

        invalid: dict[str, dict[str, object]] = {}
        zero_payload = json.loads(json.dumps(updates))
        zero_payload["updates"][1]["payload_bytes"] = 0
        invalid["zero payload"] = zero_payload
        offset_main = json.loads(json.dumps(updates))
        offset_main["updates"][1]["x"] = 1
        invalid["offset h264 geometry"] = offset_main
        oversized_crop = json.loads(json.dumps(updates))
        oversized_crop["updates"][1]["h"] = 1093
        invalid["more than one pixel crop"] = oversized_crop
        missing_window_size = json.loads(json.dumps(updates))
        missing_window_size["updates"][1]["options"].pop("window-size")
        invalid["missing h264 window size"] = missing_window_size
        missing_frame = json.loads(json.dumps(updates))
        missing_frame["updates"][1]["options"].pop("frame")
        invalid["missing h264 frame option"] = missing_frame
        wrong_window = json.loads(json.dumps(updates))
        wrong_window["updates"][1]["relative_info"] = (
            "screen-updates/2/280961714/0.info"
        )
        invalid["wrong saved window"] = wrong_window
        sequence_gap = json.loads(json.dumps(updates))
        sequence_gap["updates"][2]["sequence"] = 4
        invalid["sequence gap"] = sequence_gap
        rgb_fallback = json.loads(json.dumps(updates))
        rgb_fallback["updates"][1]["encoding"] = "rgb24"
        rgb_fallback["encodings"] = ["rgb24", "webp"]
        invalid["no h264 packet"] = rgb_fallback
        for name, candidate in invalid.items():
            with self.subTest(name=name):
                self.assertFalse(
                    live_run.primary_h264_frame_ready(
                        "zed",
                        "adaptive-alpha",
                        candidate,
                    )
                )

    def test_image_and_interaction_evidence_proves_real_alpha_content(self) -> None:
        image = live_run.Image.new("RGBA", (2, 1))
        image.putdata(((10, 20, 30, 0), (40, 50, 60, 255)))
        metrics = live_run.analyze_image(image)
        self.assertEqual(metrics["alpha_minimum"], 0)
        self.assertEqual(metrics["alpha_maximum"], 255)
        self.assertEqual(metrics["alpha_nonopaque_ratio"], 0.5)
        self.assertTrue(
            all(
                live_run.image_alpha_content_checks(
                    metrics,
                    prefix="window",
                ).values()
            )
        )
        alpha_evidence = {
            "image": metrics,
            "xwd": {"unique_rgb_colors": 101},
        }
        self.assertTrue(
            live_run.client_window_content_ready("gtk", alpha_evidence)
        )
        self.assertFalse(
            live_run.client_window_content_ready("zed", alpha_evidence)
        )
        opaque_evidence = json.loads(json.dumps(alpha_evidence))
        opaque_evidence["image"]["central_opaque_ratio"] = 1.0
        self.assertTrue(
            live_run.client_window_content_ready("zed", opaque_evidence)
        )

        capture = {
            "image": metrics,
            "xwd": {"unique_rgb_colors": 101},
        }
        source_alpha = {
            "all_have_opaque_pixels": True,
            "all_have_transparent_pixels": True,
            "count": 2,
        }
        checks = live_run.interaction_alpha_content_checks(
            {
                "before": capture,
                "after": capture,
                "source_alpha": source_alpha,
            }
        )
        self.assertTrue(all(checks.values()))

        missing_alpha = live_run.interaction_alpha_content_checks(
            {
                "before": capture,
                "after": capture,
                "source_alpha": {
                    **source_alpha,
                    "all_have_transparent_pixels": False,
                },
            }
        )
        self.assertFalse(
            missing_alpha["interaction_source_has_transparent_pixels"]
        )
        self.assertTrue(missing_alpha["interaction_source_has_opaque_pixels"])

        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            screenshot = (
                directory / "screen-updates" / "2" / "100" / "screenshot.png"
            )
            screenshot.parent.mkdir(parents=True)
            image.save(screenshot)
            source = live_run.saved_source_alpha_evidence(
                directory,
                {
                    "screenshots": [
                        "screen-updates/2/100/screenshot.png",
                    ],
                    "window_id": 2,
                },
            )
            self.assertEqual(source["count"], 1)
            self.assertTrue(source["all_have_transparent_pixels"])
            self.assertTrue(source["all_have_opaque_pixels"])
            with self.assertRaisesRegex(
                live_run.LabFailure,
                "screenshot path is unsafe",
            ):
                live_run.saved_source_alpha_evidence(
                    directory,
                    {
                        "screenshots": ["../screenshot.png"],
                        "window_id": 2,
                    },
                )

    def test_source_viewport_crop_binds_fixed_source_inside_larger_backing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            backing = live_run.Image.new("RGBA", (4, 3), (0, 0, 0, 255))
            backing.putpixel((0, 0), (255, 0, 0, 255))
            backing.putpixel((1, 0), (0, 255, 0, 255))
            backing.putpixel((0, 1), (0, 0, 255, 255))
            backing.putpixel((1, 1), (255, 255, 255, 255))
            backing.save(directory / "window.rgba.png")
            evidence = live_run.crop_client_source_viewport(
                directory,
                "window",
                "source",
                (2, 2),
            )
            (directory / "client.stdout").write_text(
                "viewport: (0, 1, 2, 2) for backing size=(4, 3)\n",
                encoding="utf-8",
            )

            self.assertEqual(evidence["image"]["width"], 2)
            self.assertEqual(evidence["image"]["height"], 2)
            self.assertEqual(
                evidence["viewport"],
                {
                    "backing_size": [4, 3],
                    "origin": [0, 0],
                    "source_size": [2, 2],
                },
            )
            self.assertTrue(
                live_run.client_source_viewport_logged(
                    directory,
                    (2, 2),
                    (4, 3),
                )
            )
            self.assertFalse(
                live_run.client_source_viewport_logged(
                    directory,
                    (3, 2),
                    (4, 3),
                )
            )

    def test_saved_update_payload_cannot_escape_its_update_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            info = directory / "screen-updates" / "1" / "1" / "0.info"
            info.parent.mkdir(parents=True)
            info.write_text(
                json.dumps({"encoding": "h264", "file": "/etc/passwd"}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(live_run.LabFailure, "payload name is unsafe"):
                live_run.parse_saved_updates(directory, 1)

    def test_live_user_mapping_is_independent_of_the_host_uid(self) -> None:
        self.assertEqual(
            live_run.live_user_options(),
            [
                "--userns",
                "keep-id:uid=1001,gid=1001,size=2048",
                "--user",
                "1001:1001",
            ],
        )

    def test_live_command_rejects_an_unbounded_user_namespace(self) -> None:
        with self.assertRaisesRegex(live_run.LabFailure, "explicit size"):
            live_run.run(
                ["podman", "run", "--userns=keep-id:uid=1001,gid=1001", "image"],
                announce=False,
            )

    def test_artifact_collection_failure_fails_scenario_acceptance(self) -> None:
        report = {
            "classification": {"first_failed_boundary": "passed"},
            "container_artifact_collection": [
                {"status": "collected"},
                {"status": "collection-failed"},
            ],
        }
        self.assertFalse(live_run.scenario_acceptance(report, {"passed": True}))

        report["container_artifact_collection"] = [{"status": "collected"}]
        report["classification"]["diagnostic_only"] = True
        self.assertFalse(live_run.scenario_acceptance(report, {"passed": True}))

    def test_network_labels_accept_lowercase_podman_field(self) -> None:
        labels = {
            "io.xpra.lab.owner": "live",
            "io.xpra.lab.run-id": "lowercase-labels",
        }
        argv = ["podman", "network", "inspect", "network-id"]
        with patch.object(
            live_run,
            "run",
            return_value=completed(
                argv,
                json.dumps([{"id": "a" * 64, "name": "network", "labels": labels}]),
            ),
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
        name = "container-name"
        object_id = "a" * 64
        labels = {
            "io.xpra.lab.owner": "live",
            "io.xpra.lab.run-id": "cleanup-run",
        }

        def podman_command(
            argv: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            if argv == ["podman", "container", "inspect", name]:
                return completed(
                    argv,
                    json.dumps(
                        [{"Config": {"Labels": labels}, "Id": object_id, "Name": name}]
                    ),
                )
            if argv == ["podman", "rm", "--force", object_id]:
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
        name = "container-name"
        object_id = "a" * 64
        labels = {
            "io.xpra.lab.owner": "live",
            "io.xpra.lab.run-id": "cleanup-run",
        }

        def podman_command(
            argv: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            if argv == ["podman", "container", "inspect", name]:
                return completed(
                    argv,
                    json.dumps(
                        [{"Config": {"Labels": labels}, "Id": object_id, "Name": name}]
                    ),
                )
            if argv == ["podman", "rm", "--force", object_id]:
                return completed(argv, returncode=125, stderr="remove failed\n")
            if argv == ["podman", "ps", "--all", "--format", "{{.Names}}"]:
                return completed(argv, f"{name}\n")
            raise AssertionError(f"unexpected command: {argv!r}")

        with patch.object(live_run, "run", side_effect=podman_command):
            result = live_run.remove_owned_podman_object("container", name, labels)
        self.assertEqual(result["status"], "remove-failed")
        self.assertEqual(result["postcondition"], "present")


class LiveSourceTest(unittest.TestCase):
    def test_freezes_the_source_boundary_embedded_in_develop_without_network(self) -> None:
        commit = "1" * 40
        head = "2" * 40
        source_tip = "3" * 40
        responses = {
            ("rev-parse", "--is-inside-work-tree"): "true",
            ("branch", "--show-current"): "develop",
            ("remote",): "origin",
            ("remote", "get-url", "origin"): live_run.FORK_REMOTE_URL,
            ("rev-parse", "HEAD"): head,
            ("rev-parse", "refs/remotes/origin/master"): source_tip,
            ("merge-base", "--all", source_tip, head): commit,
            ("describe", "--long", "--always", "--tags", commit): "v6.4-1-g111111111",
            ("rev-list", "--count", "--first-parent", commit): "10",
        }
        with tempfile.TemporaryDirectory() as raw:
            repository = Path(raw)
            (repository / ".git").mkdir()
            with (
                patch.object(live_run, "SOURCE_REPOSITORY", repository),
                patch.object(
                    live_run,
                    "git_output",
                    side_effect=lambda *arguments: responses[arguments],
                ),
            ):
                resolved, marker, revision = live_run.resolve_embedded_source()

        self.assertEqual(resolved, commit)
        self.assertEqual(marker, "g111111111")
        self.assertEqual(revision, 5024)

    def test_input_evidence_archives_symlinked_build_contexts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result = root / "result"
            result.mkdir()
            source_archive = root / "source.tar"
            source_archive.write_bytes(b"source")
            selection = live_run.PatchSelection(
                case_slugs=(),
                digest="1" * 64,
                name="master",
                patches=(),
                selector_digests=(),
                selectors=(),
            )
            contexts: list[live_run.BuildContext] = []
            resolution = {"resolution_sha256": "2" * 64}
            context_manifest = {
                "selection": {
                    "case_slugs": [],
                    "digest": selection.digest,
                    "name": selection.name,
                    "resolution": resolution,
                    "selector_digests": {},
                    "selectors": [],
                }
            }
            for role in ("server", "client"):
                context_root = root / role
                context_root.mkdir()
                (context_root / "value").write_text(role, encoding="utf-8")
                (context_root / "link").symlink_to("value")
                contexts.append(
                    live_run.BuildContext(
                        digest=live_run.tree_sha256(context_root),
                        manifest=context_manifest,
                        patches=(),
                        path=context_root,
                        resolution=resolution,
                        selection=selection,
                    )
                )
            snapshot = live_run.SourceSnapshot(
                archive_path=source_archive,
                archive_sha256=hashlib.sha256(b"source").hexdigest(),
                commit="3" * 40,
                commit_marker="g333333333",
                revision=1,
                workflow_sha256="4" * 64,
            )

            manifest_sha256, _zed_archive, _zed_sha256 = (
                live_run.snapshot_build_inputs(
                    result,
                    snapshot,
                    contexts[0],
                    contexts[1],
                    None,
                )
            )

            inputs = result / "inputs"
            (inputs / "SHA256SUMS").chmod(0o600)
            self.assertTrue((inputs / "contexts/server.tar").is_file())
            self.assertFalse((inputs / "contexts/server").exists())
            self.assertTrue(
                job.input_checksum_validation(
                    inputs,
                    {
                        "input_manifest_sha256": manifest_sha256,
                        "input_tree_sha256": live_run.tree_sha256(inputs),
                    },
                )
            )
            extracted = root / "extracted"
            with (inputs / "contexts/server.tar").open("rb") as stream:
                live_run.container_payload.extract_archive(stream, extracted)
            self.assertEqual(os.readlink(extracted / "link"), "value")

            replacement = root / "replacement"
            replacement.mkdir()
            (replacement / "different").write_text("different\n", encoding="utf-8")
            server_archive = inputs / "contexts/server.tar"
            server_archive.unlink()
            with server_archive.open("xb") as stream:
                live_run.container_payload.write_archive(
                    stream,
                    (
                        live_run.container_payload.PayloadEntry(
                            replacement / "different",
                            live_run.PurePosixPath("different"),
                        ),
                    ),
                )
            server_archive.chmod(0o600)
            manifest_path = inputs / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["server_context_archive_sha256"] = live_run.sha256_file(
                server_archive
            )
            with self.assertRaisesRegex(
                live_run.LabFailure,
                "does not match its context digest",
            ):
                live_run._bound_context(inputs, "server", manifest)


class LiveTransportProfileTest(unittest.TestCase):
    def test_tracked_live_configuration_drives_all_runner_option_blocks(self) -> None:
        default, profiles = live_run.live_config.load_network_profiles()
        self.assertIn(default, profiles)
        self.assertEqual(tuple(profiles), live_run.NETWORK_PROFILES)
        self.assertEqual(default, live_run.DEFAULT_NETWORK_PROFILE)

        for profile_name, profile in profiles.items():
            with self.subTest(profile=profile_name):
                self.assertEqual(
                    live_run.client_network_options(profile_name),
                    list(profile.client_options()),
                )

        configured = live_run.live_config.load_live_cli()
        for role, blocks in configured.items():
            for block, options in blocks.items():
                if block in {"commands", "transports"}:
                    continue
                with self.subTest(role=role, block=block):
                    self.assertEqual(
                        live_run.static_cli_options(role, block),
                        list(options),
                    )
            for command, options in blocks["commands"].items():
                with self.subTest(role=role, command=command):
                    self.assertEqual(
                        live_run.command_cli_options(role, command),
                        list(options),
                    )
            for encoding, transport in blocks["transports"].items():
                for policy in transport["policies"]:
                    with self.subTest(
                        role=role,
                        encoding=encoding,
                        policy=policy,
                    ):
                        self.assertEqual(
                            live_run.transport_encoding_options(
                                encoding,
                                policy,
                                client=role == "client",
                            ),
                            list(
                                live_run.live_config.transport_options(
                                    role,
                                    encoding,
                                    policy,
                                )
                            ),
                        )

        harness = set(live_run.HARNESS_INPUTS)
        self.assertEqual(job.HARNESS_INPUTS, live_run.HARNESS_INPUTS)
        self.assertIn(live_run.LIVE_CONFIG_MODULE, harness)
        self.assertIn(live_run.NETWORK_PROFILES_CONFIG, harness)
        self.assertIn(live_run.LIVE_CLI_CONFIG, harness)

    def test_tracked_live_configuration_parser_fails_closed_generically(self) -> None:
        for source in (
            live_run.NETWORK_PROFILES_CONFIG,
            live_run.LIVE_CLI_CONFIG,
        ):
            with self.subTest(source=source.name), tempfile.TemporaryDirectory() as raw:
                candidate = Path(raw) / source.name
                lines = source.read_text(encoding="utf-8").splitlines()
                first_content = next(
                    index
                    for index, line in enumerate(lines)
                    if line and not line.startswith("#")
                )
                lines[first_content] += " "
                candidate.write_text("\n".join(lines) + "\n", encoding="utf-8")
                with self.assertRaisesRegex(
                    live_run.live_config.LiveConfigError,
                    "unsafe whitespace",
                ):
                    live_run.live_config.load_strict_yaml(candidate)

    def test_every_tracked_network_profile_supports_every_acceptance_profile(
        self,
    ) -> None:
        for application in live_run.APPLICATIONS:
            for lifecycle in live_run.LIFECYCLES:
                for encoding in ("rgb", "h264"):
                    for policy in live_run.H264_CLIENT_POLICIES:
                        for alpha_scenarios in live_run.ALPHA_SCENARIOS:
                            values = (
                                application,
                                lifecycle,
                                encoding,
                                policy,
                                alpha_scenarios,
                            )
                            outcomes = []
                            for profile_name in live_run.NETWORK_PROFILES:
                                try:
                                    live_run.validate_profile(
                                        application=application,
                                        lifecycle=lifecycle,
                                        encoding=encoding,
                                        h264_client_policy=policy,
                                        alpha_scenarios=alpha_scenarios,
                                        network_profile_name=profile_name,
                                    )
                                except live_run.ProfileError:
                                    outcomes.append(False)
                                else:
                                    outcomes.append(True)
                            with self.subTest(values=values):
                                self.assertEqual(len(set(outcomes)), 1)

    def test_supported_xpra_only_profiles_are_exact(self) -> None:
        expected = {
            ("zed", "application-exit", "rgb", "strict", "default"),
            ("zed", "application-exit", "h264", "adaptive-alpha", "default"),
            (
                "hardware",
                "application-exit",
                "h264",
                "adaptive-alpha",
                "default",
            ),
            (
                "opengl",
                "application-exit",
                "h264",
                "adaptive-alpha",
                "default",
            ),
            ("gtk", "detach", "rgb", "strict", "default"),
            ("gtk", "transport-loss", "rgb", "strict", "default"),
        }
        observed = set()
        for application in live_run.APPLICATIONS:
            for lifecycle in live_run.LIFECYCLES:
                for encoding in ("rgb", "h264"):
                    for policy in live_run.H264_CLIENT_POLICIES:
                        for alpha_scenarios in live_run.ALPHA_SCENARIOS:
                            values = (
                                application,
                                lifecycle,
                                encoding,
                                policy,
                                alpha_scenarios,
                            )
                            with self.subTest(values=values):
                                try:
                                    live_run.validate_profile(
                                        application=application,
                                        lifecycle=lifecycle,
                                        encoding=encoding,
                                        h264_client_policy=policy,
                                        alpha_scenarios=alpha_scenarios,
                                    )
                                except live_run.ProfileError:
                                    continue
                                observed.add(values)
        self.assertEqual(observed, expected)

    def test_public_live_clis_do_not_advertise_fallback_diagnostics(self) -> None:
        with (
            patch.object(sys, "stderr", StringIO()),
            self.assertRaises(SystemExit) as job_exit,
        ):
            job.parser().parse_args(
                [
                    "start",
                    "fallback-run",
                    "--encoding",
                    "h264",
                    "--h264-client-policy",
                    "fallback-auto",
                    "--selection",
                    "stacks/develop",
                ]
            )
        self.assertEqual(job_exit.exception.code, 2)

        with (
            patch.object(
                sys,
                "argv",
                [
                    "run.py",
                    "--h264-client-policy",
                    "fallback-h264",
                    "--selection",
                    "stacks/develop",
                ],
            ),
            patch.object(sys, "stderr", StringIO()),
            patch.object(live_run.PIL, "__version__", live_run.EXPECTED_PILLOW_VERSION),
            self.assertRaises(SystemExit) as runner_exit,
        ):
            live_run.main()
        self.assertEqual(runner_exit.exception.code, 2)

        with (
            patch.object(
                sys,
                "argv",
                [
                    "run.py",
                    "--selection",
                    "stacks/develop",
                    "--source-variant",
                    "master",
                ],
            ),
            patch.object(sys, "stderr", StringIO()),
            patch.object(live_run.PIL, "__version__", live_run.EXPECTED_PILLOW_VERSION),
            self.assertRaises(SystemExit) as clean_alias_exit,
        ):
            live_run.main()
        self.assertEqual(clean_alias_exit.exception.code, 2)

        profiles_module = sys.modules["profiles"]
        with (
            patch.object(
                sys,
                "argv",
                [
                    "profiles.py",
                    "zed",
                    "application-exit",
                    "h264",
                    "fallback-auto",
                    "default",
                ],
            ),
            patch.object(sys, "stderr", StringIO()),
            self.assertRaises(SystemExit) as profiles_exit,
        ):
            profiles_module.main()
        self.assertEqual(profiles_exit.exception.code, 2)

    def test_named_make_profiles_fix_every_acceptance_dimension(self) -> None:
        makefile = (LIVE_DIRECTORY.parents[1] / "Makefile").read_text(encoding="utf-8")
        expected = {
            "live-rgb": (
                "APPLICATION=zed",
                "LIFECYCLE=application-exit",
                "ENCODING=rgb",
                "H264_CLIENT_POLICY=strict",
                "ALPHA_SCENARIOS=default",
            ),
            "live-h264": (
                "APPLICATION=zed",
                "LIFECYCLE=application-exit",
                "ENCODING=h264",
                "H264_CLIENT_POLICY=adaptive-alpha",
                "ALPHA_SCENARIOS=default",
            ),
            "live-xpra-detach": (
                "APPLICATION=gtk",
                "LIFECYCLE=detach",
                "ENCODING=rgb",
                "H264_CLIENT_POLICY=strict",
                "ALPHA_SCENARIOS=default",
            ),
            "live-xpra-transport-loss": (
                "APPLICATION=gtk",
                "LIFECYCLE=transport-loss",
                "ENCODING=rgb",
                "H264_CLIENT_POLICY=strict",
                "ALPHA_SCENARIOS=default",
            ),
            "live-xpra-hardware": (
                "APPLICATION=hardware",
                "LIFECYCLE=application-exit",
                "ENCODING=h264",
                "H264_CLIENT_POLICY=adaptive-alpha",
                "ALPHA_SCENARIOS=default",
            ),
            "live-xpra-opengl-hardware": (
                "APPLICATION=opengl",
                "LIFECYCLE=application-exit",
                "ENCODING=h264",
                "H264_CLIENT_POLICY=adaptive-alpha",
                "ALPHA_SCENARIOS=default",
            ),
        }
        for target, values in expected.items():
            with self.subTest(target=target):
                recipe = makefile.split(f"{target}:\n", 1)[1].split("\n\n", 1)[0]
                for value in values:
                    self.assertIn(value, recipe)

        self.assertIn(
            "live-start: isolated-start-check selector-check run-name-check live-options-check",
            makefile,
        )
        self.assertIn('--selection "$${XPRA_LAB_SELECTOR}"', makefile)
        self.assertNotIn("live-start: optional-selector-check", makefile)

    def test_runner_and_input_freeze_reject_a_clean_server_selection(self) -> None:
        with self.assertRaisesRegex(live_run.LabFailure, "requires one non-empty"):
            live_run.freeze_owned_inputs(
                Path("/unreachable"),
                Path("/unreachable"),
                application="zed",
                selection_name=None,
                zed_directory=None,
            )
        with (
            patch.object(
                sys,
                "argv",
                ["run.py", "--encoding", "rgb", "--alpha-scenarios", "default"],
            ),
            patch.object(sys, "stderr", StringIO()),
            patch.object(live_run.PIL, "__version__", live_run.EXPECTED_PILLOW_VERSION),
            self.assertRaises(SystemExit) as clean_runner_exit,
        ):
            live_run.main()
        self.assertEqual(clean_runner_exit.exception.code, 2)

    def test_zed_h264_stimulus_retries_until_sustained_positive_production(
        self,
    ) -> None:
        commands: list[list[str]] = []
        captures: list[str] = []

        def execute(_container: str, argv: list[str], **_kwargs: object):
            commands.append(argv)
            return completed(argv)

        def capture(
            _container: str,
            _directory: Path,
            destination: str,
            **_kwargs: object,
        ) -> None:
            captures.append(destination)

        def converted(_directory: Path, stem: str) -> dict[str, object]:
            return {"image": {"rgb_sha256": stem}}

        baseline = {"updates": [{"sequence": 9}]}
        first = {"updates": [{"sequence": value} for value in range(1, 16)]}
        second = {"updates": [{"sequence": value} for value in range(1, 30)]}
        insufficient = {
            "aggregate_encoded_pixels": 100,
            "h264_damage_span_ms": 500,
            "h264_main_frame_count": 5,
            "h264_main_pixels": 99,
            "minimum_frame_h264_pixels": 99,
            "minimum_frame_window_pixels": 100,
        }
        sufficient = {
            **insufficient,
            "h264_damage_span_ms": 1125,
            "h264_main_frame_count": 10,
        }
        metric_inputs: list[dict[str, object]] = []

        def metrics(_application: str, updates: dict[str, object]):
            metric_inputs.append(updates)
            return insufficient if len(metric_inputs) == 1 else sufficient

        with (
            tempfile.TemporaryDirectory() as raw,
            patch.object(live_run, "podman_exec", side_effect=execute),
            patch.object(live_run, "capture_xwd", side_effect=capture),
            patch.object(live_run, "convert_xwd", side_effect=converted),
            patch.object(live_run.time, "sleep"),
            patch.object(
                live_run,
                "synchronize_saved_updates",
                side_effect=(baseline, first, second),
            ),
            patch.object(
                live_run,
                "h264_production_metrics",
                side_effect=metrics,
            ),
        ):
            evidence = live_run.exercise_zed_h264_stability(
                "server",
                "client",
                "4194320",
                1,
                {"width": 1596, "height": 1173},
                Path(raw),
                {
                    "target": {
                        "system_bounds": [100, 100, 160, 130],
                        "dark_bounds": [40, 100, 100, 130],
                    }
                },
            )

        self.assertEqual(evidence["baseline_sequence"], 9)
        self.assertEqual(evidence["last_sequence"], 29)
        self.assertEqual(evidence["window_size"], [1596, 1173])
        self.assertEqual(len(evidence["attempts"]), 2)
        self.assertTrue(all(evidence["dominance_checks"].values()))
        self.assertEqual(
            [value["h264_stimulus"] for value in metric_inputs],
            [
                {
                    "baseline_sequence": 9,
                    "last_sequence": 15,
                    "window_size": [1596, 1173],
                },
                {
                    "baseline_sequence": 9,
                    "last_sequence": 29,
                    "window_size": [1596, 1173],
                },
            ],
        )
        toggle_commands = [command for command in commands if "mousemove" in command]
        self.assertEqual(
            len(toggle_commands),
            2 * 2 * live_run.ZED_THEME_TOGGLE_CYCLES,
        )
        self.assertIn("window-h264-theme-baseline.xwd", captures)

    def test_saved_update_sync_separates_window_metadata_from_packets(self) -> None:
        listed = (
            "screen-updates/1/window.info",
            "screen-updates/1/100/0.info",
        )
        pulls: list[tuple[str, ...]] = []

        def pull(
            _container: str,
            directory: Path,
            relatives: tuple[str, ...],
        ) -> None:
            pulls.append(relatives)
            for relative in relatives:
                path = directory / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                if relative.endswith("window.info"):
                    path.write_text(
                        json.dumps({"pixel-format": "RGBX"}),
                        encoding="utf-8",
                    )
                elif relative.endswith(".info"):
                    path.write_text(
                        json.dumps(
                            {
                                "encoding": "h264",
                                "file": "0.h264",
                                "h": 64,
                                "options": {
                                    "flush": 0,
                                    "frame": 0,
                                    "type": "IDR",
                                    "window-size": [64, 64],
                                },
                                "sequence": 1,
                                "w": 64,
                                "x": 0,
                                "y": 0,
                            }
                        ),
                        encoding="utf-8",
                    )
                else:
                    path.write_bytes(b"h264")

        with (
            tempfile.TemporaryDirectory() as raw,
            patch.object(live_run, "container_artifact_files", return_value=listed),
            patch.object(live_run, "pull_container_artifacts", side_effect=pull),
        ):
            updates = live_run.synchronize_saved_updates(
                "server",
                Path(raw),
                1,
            )
        self.assertEqual(updates["count"], 1)
        self.assertEqual(updates["initial_pixel_format"], "RGBX")
        self.assertEqual(
            pulls,
            [
                listed,
                ("screen-updates/1/100/0.h264",),
            ],
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

    def test_primary_server_window_id_is_resolved_once_before_frame_poll(self) -> None:
        source = (LIVE_DIRECTORY / "run.py").read_text(encoding="utf-8")
        scenario = source.split("def run_scenario(\n", 1)[1]
        query = (
            'xpra_wid = server_xpra_window_id(directory / "server-info.txt", '
            "title_patterns)"
        )
        self.assertEqual(scenario.count(query), 1)
        self.assertLess(scenario.index(query), scenario.index("wait_for_frame_boundary("))

    def test_hardware_application_uses_owned_multiwindow_fixture(self) -> None:
        command, titles, pid_file = live_run.application_contract("hardware")
        self.assertEqual(command, "/opt/xpra-lab/start_hardware_fixture.sh")
        self.assertEqual(titles, ("vkcube",))
        self.assertEqual(pid_file, "vkcube.pid")

        command, titles, pid_file = live_run.application_contract("opengl")
        self.assertEqual(command, "/opt/xpra-lab/start_hardware_fixture.sh opengl")
        self.assertEqual(titles, ("glmark2",))
        self.assertEqual(pid_file, "opengl.pid")
        context_names = {path.name for path in live_run.BUILD_CONTEXT_INPUTS}
        self.assertIn("start_hardware_fixture.sh", context_names)

    def test_zed_fixture_keeps_the_reviewed_onboarding_window(self) -> None:
        source = (LIVE_DIRECTORY / "start_zed.sh").read_text(encoding="utf-8")
        self.assertNotIn("xpra-live-scroll", source)

        command, titles, pid_file = live_run.application_contract("zed")
        self.assertEqual(command, "/opt/xpra-lab/start_zed.sh")
        self.assertEqual(titles, ("empty project", "zed"))
        self.assertEqual(pid_file, "zed.pid")

    def test_frame_alpha_states_are_parsed_and_bound_to_exact_windows(self) -> None:
        server_log = (
            "prefix window 0x1 frame pixel format=BGRX, want-alpha=False\n"
            "prefix window 0x2 frame pixel format=BGRA, want-alpha=True\n"
            "prefix window 0xa frame pixel format=RGBA, want-alpha=True\n"
            "window 0x1 frame pixel format=BGRX, want-alpha=false\n"
            "window 0x1 frame pixel format=BGRX, want-alpha=False trailing"
        )
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            (directory / "server.stderr").write_text(server_log, encoding="utf-8")
            states = live_run.inspect_logs(directory)["frame_alpha_states"]
        self.assertEqual(
            states,
            [
                {"pixel_format": "BGRX", "want_alpha": False, "window_id": 1},
                {"pixel_format": "BGRA", "want_alpha": True, "window_id": 2},
                {"pixel_format": "RGBA", "want_alpha": True, "window_id": 10},
            ],
        )
        checks = live_run.hardware_frame_alpha_state_checks(
            {"frame_alpha_states": states},
            {"window_id": 1},
            {"window_id": 2},
        )
        self.assertTrue(all(checks.values()))

        wrong_window = live_run.hardware_frame_alpha_state_checks(
            {"frame_alpha_states": states},
            {"window_id": 3},
            {"window_id": 2},
        )
        self.assertFalse(wrong_window["primary_window_opaque_frame_states"])
        self.assertTrue(wrong_window["interaction_window_alpha_frame_states"])

        later_transition = [
            *states,
            {"pixel_format": "RGBA", "want_alpha": True, "window_id": 1},
            {"pixel_format": "BGRX", "want_alpha": False, "window_id": 2},
        ]
        transitioned = live_run.hardware_frame_alpha_state_checks(
            {"frame_alpha_states": later_transition},
            {"window_id": 1},
            {"window_id": 2},
        )
        self.assertFalse(transitioned["primary_window_opaque_frame_states"])
        self.assertFalse(transitioned["interaction_window_alpha_frame_states"])

        adaptive_updates = {
            "updates": [
                {
                    "encoding": "webp",
                    "options": {},
                    "relative_info": "screen-updates/1/100/0.info",
                },
                {
                    "encoding": "h264",
                    "options": {},
                    "relative_info": "screen-updates/1/101/0.info",
                },
            ],
            "window_id": 1,
        }
        saved_states = [
            {
                "encoding": "webp",
                "pixel_format": "BGRA",
                "relative_info": "screen-updates/1/100/0.info",
                "want_alpha": True,
                "window_id": 1,
            },
            {
                "encoding": "h264",
                "pixel_format": "BGRX",
                "relative_info": "screen-updates/1/101/0.info",
                "want_alpha": False,
                "window_id": 1,
            },
        ]
        adaptive = live_run.adaptive_frame_alpha_state_checks(
            {
                "frame_alpha_states": [
                    {
                        "pixel_format": "BGRA",
                        "want_alpha": True,
                        "window_id": 1,
                    },
                    {
                        "pixel_format": "BGRX",
                        "want_alpha": False,
                        "window_id": 1,
                    },
                ],
                "saved_packet_frame_states": saved_states,
            },
            adaptive_updates,
        )
        self.assertTrue(all(adaptive.values()))
        wrong_adaptive_window = live_run.adaptive_frame_alpha_state_checks(
            {
                "frame_alpha_states": states,
                "saved_packet_frame_states": saved_states,
            },
            {**adaptive_updates, "window_id": 3},
        )
        self.assertFalse(all(wrong_adaptive_window.values()))
        inconsistent_adaptive = live_run.adaptive_frame_alpha_state_checks(
            {
                "frame_alpha_states": [
                    {
                        "pixel_format": "BGRX",
                        "want_alpha": False,
                        "window_id": 1,
                    },
                    {
                        "pixel_format": "BGRA",
                        "want_alpha": False,
                        "window_id": 1,
                    },
                ],
                "saved_packet_frame_states": saved_states,
            },
            adaptive_updates,
        )
        self.assertFalse(all(inconsistent_adaptive.values()))
        all_alpha_adaptive = live_run.adaptive_frame_alpha_state_checks(
            {
                "frame_alpha_states": [
                    {
                        "pixel_format": "BGRA",
                        "want_alpha": True,
                        "window_id": 1,
                    }
                ],
                "saved_packet_frame_states": [
                    {**record, "pixel_format": "BGRA", "want_alpha": True}
                    for record in saved_states
                ],
            },
            adaptive_updates,
        )
        self.assertFalse(
            all_alpha_adaptive["primary_h264_packets_have_opaque_frame_state"]
        )

        opaque_webp = live_run.adaptive_frame_alpha_state_checks(
            {
                "frame_alpha_states": [
                    {
                        "pixel_format": "RGBX",
                        "want_alpha": False,
                        "window_id": 1,
                    }
                ],
                "saved_packet_frame_states": [
                    {**record, "pixel_format": "RGBX", "want_alpha": False}
                    for record in saved_states
                ],
            },
            adaptive_updates,
        )
        self.assertTrue(all(opaque_webp.values()))

    def test_saved_packets_bind_to_the_latest_exact_window_frame_state(self) -> None:
        server_log = (
            "window 0x1 frame pixel format=RGBX, want-alpha=False\n"
            "saved webp : 10 bytes to "
            "'/artifacts/screen-updates/1/100/0.webp'\n"
            "window 0x2 frame pixel format=BGRA, want-alpha=True\n"
            "saved webp : 11 bytes to "
            "'/artifacts/screen-updates/2/101/0.webp'\n"
            "window 0x1 frame pixel format=RGBA, want-alpha=True\n"
            "saved rgb32: 12 bytes to "
            "'/artifacts/screen-updates/1/102/0.rgb32'\n"
        )
        self.assertEqual(
            live_run.parse_saved_packet_frame_states(server_log),
            [
                {
                    "encoding": "webp",
                    "pixel_format": "RGBX",
                    "relative_info": "screen-updates/1/100/0.info",
                    "want_alpha": False,
                    "window_id": 1,
                },
                {
                    "encoding": "webp",
                    "pixel_format": "BGRA",
                    "relative_info": "screen-updates/2/101/0.info",
                    "want_alpha": True,
                    "window_id": 2,
                },
                {
                    "encoding": "rgb32",
                    "pixel_format": "RGBA",
                    "relative_info": "screen-updates/1/102/0.info",
                    "want_alpha": True,
                    "window_id": 1,
                },
            ],
        )
        self.assertEqual(
            live_run.parse_saved_packet_frame_states(
                "saved h264 : 9 bytes to "
                "'/artifacts/screen-updates/1/100/0.h264'\n"
            )[0]["pixel_format"],
            None,
        )

    def test_hardware_artifact_allowlist_includes_only_exact_fixture_files(self) -> None:
        allowed = (
            "interaction.exit",
            "interaction.pid",
            "interaction.stderr",
            "interaction.stdout",
            "vkcube.exit",
            "vkcube.pid",
            "vkcube.stderr",
            "vkcube.stdout",
            "opengl.exit",
            "opengl.pid",
            "opengl.stderr",
            "opengl.stdout",
        )
        for name in allowed:
            with self.subTest(name=name):
                self.assertTrue(
                    any(pattern.fullmatch(name) for pattern in live_run.SERVER_ARTIFACT_PATTERNS)
                )
        for name in (
            "interaction.core",
            "opengl.core",
            "opengl.trace",
            "vkcube.trace",
            "vkcube.exit.extra",
        ):
            with self.subTest(name=name):
                self.assertFalse(
                    any(pattern.fullmatch(name) for pattern in live_run.SERVER_ARTIFACT_PATTERNS)
                )

    def test_hardware_launcher_records_both_child_statuses_without_masking(self) -> None:
        source = (LIVE_DIRECTORY / "start_hardware_fixture.sh").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifacts = root / "artifacts"
            binaries = root / "bin"
            artifacts.mkdir()
            binaries.mkdir()
            script_text = source.replace("/artifacts", str(artifacts)).replace(
                "/tmp/xpra-hardware-",
                str(root / "marker-"),
            )
            script = root / "start-hardware.sh"
            script.write_text(script_text, encoding="utf-8")
            script.chmod(0o700)
            (binaries / "vkcube").write_text(
                "#!/bin/sh\nexit \"$FAKE_VULKAN_STATUS\"\n",
                encoding="utf-8",
            )
            (binaries / "glmark2-wayland").write_text(
                "#!/bin/sh\n"
                "test -z \"${DISPLAY+x}\" || exit 97\n"
                "exit \"$FAKE_OPENGL_STATUS\"\n",
                encoding="utf-8",
            )
            (binaries / "python3").write_text(
                "#!/bin/sh\n"
                "test -z \"${DISPLAY+x}\" || exit 98\n"
                "test \"$GDK_BACKEND\" = wayland || exit 99\n"
                "exit \"$FAKE_INTERACTION_STATUS\"\n",
                encoding="utf-8",
            )
            for executable in (
                binaries / "glmark2-wayland",
                binaries / "vkcube",
                binaries / "python3",
            ):
                executable.chmod(0o700)

            for interaction, vulkan, expected in ((0, 0, 0), (7, 0, 7), (0, 9, 9)):
                with self.subTest(
                    interaction=interaction,
                    vulkan=vulkan,
                ):
                    for name in ("interaction.exit", "vkcube.exit"):
                        (artifacts / name).unlink(missing_ok=True)
                    environment = os.environ.copy()
                    environment.update(
                        {
                            "DISPLAY": ":150",
                            "FAKE_INTERACTION_STATUS": str(interaction),
                            "FAKE_OPENGL_STATUS": "0",
                            "FAKE_VULKAN_STATUS": str(vulkan),
                            "PATH": f"{binaries}:{environment['PATH']}",
                        }
                    )
                    result = subprocess.run(
                        ["bash", str(script)],
                        capture_output=True,
                        check=False,
                        env=environment,
                        text=True,
                    )
                    self.assertEqual(result.returncode, expected, result.stderr)
                    self.assertEqual(
                        (artifacts / "interaction.exit").read_text(encoding="ascii"),
                        f"{interaction}\n",
                    )
                    self.assertEqual(
                        (artifacts / "vkcube.exit").read_text(encoding="ascii"),
                        f"{vulkan}\n",
                    )

            environment = os.environ.copy()
            environment.update(
                {
                    "DISPLAY": ":150",
                    "FAKE_INTERACTION_STATUS": "0",
                    "FAKE_OPENGL_STATUS": "11",
                    "FAKE_VULKAN_STATUS": "0",
                    "PATH": f"{binaries}:{environment['PATH']}",
                }
            )
            result = subprocess.run(
                ["bash", str(script), "opengl"],
                capture_output=True,
                check=False,
                env=environment,
                text=True,
            )
            self.assertEqual(result.returncode, 11, result.stderr)
            self.assertEqual(
                (artifacts / "opengl.exit").read_text(encoding="ascii"),
                "11\n",
            )

    def test_interaction_fixture_is_ready_and_has_a_transparent_border(self) -> None:
        source = (LIVE_DIRECTORY / "interaction_fixture.py").read_text(encoding="utf-8")
        self.assertIn('READY_MARKER = Path("/tmp/xpra-hardware-interaction-ready")', source)
        self.assertIn("get_rgba_visual()", source)
        self.assertIn("window.set_visual(visual)", source)
        self.assertIn("window.set_app_paintable(True)", source)
        self.assertIn("background-color: rgba(0, 0, 0, 0)", source)
        self.assertIn("button.set_halign(Gtk.Align.CENTER)", source)
        self.assertIn("button.set_valign(Gtk.Align.CENTER)", source)
        self.assertIn("button.set_size_request(360, 120)", source)
        self.assertIn("GLib.idle_add(publish_ready)", source)
        self.assertLess(
            source.index("window.show_all()"),
            source.index("GLib.idle_add"),
        )
        self.assertLess(
            source.index("GLib.idle_add"),
            source.index("Gtk.main()"),
        )

    def test_hardware_launcher_uses_an_opaque_native_wayland_opengl_primary(
        self,
    ) -> None:
        source = (LIVE_DIRECTORY / "start_hardware_fixture.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("glmark2-wayland --run-forever", source)
        self.assertIn("--size 640x480", source)
        self.assertIn("--swap-mode fifo", source)
        self.assertIn("red=8:green=8:blue=8:alpha=0:buffer=24", source)
        self.assertIn("--benchmark jellyfish", source)

    def test_server_image_makes_interaction_fixture_readable_by_runtime_user(self) -> None:
        source = (LIVE_DIRECTORY / "Containerfile").read_text(encoding="utf-8")
        server_stage = source.split(
            "FROM docker.io/library/ubuntu:26.04 AS server\n",
            1,
        )[1].split("FROM docker.io/library/debian:13-slim AS client-build\n", 1)[0]
        chmod = "chmod 0644 /opt/xpra-lab/interaction_fixture.py"
        runtime_check = "test -r /opt/xpra-lab/interaction_fixture.py"
        self.assertIn(chmod, server_stage)
        self.assertIn(runtime_check, server_stage)
        self.assertIn("glmark2-wayland", server_stage)
        self.assertLess(server_stage.index(chmod), server_stage.index("USER lab"))
        self.assertGreater(server_stage.index(runtime_check), server_stage.index("USER lab"))

    def test_pixel_tolerance_is_scoped_to_the_owning_profile(self) -> None:
        self.assertEqual(live_run.pixel_error_limit("zed", "rgb"), 0.0)
        self.assertEqual(live_run.pixel_error_limit("gtk", "rgb"), 1.0)
        self.assertEqual(live_run.pixel_error_limit("hardware", "h264"), 15.0)
        self.assertEqual(live_run.pixel_error_limit("opengl", "h264"), 15.0)
        self.assertEqual(live_run.pixel_error_limit("vkcube", "rgb"), 0.0)

    def test_hardware_application_uses_observable_vulkan_boundaries(self) -> None:
        checks = live_run.application_boundary_checks(
            application="hardware",
            application_activity={
                "process_alive": True,
                "graphics_motion": {"changed": True},
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
        checks["graphics_frames_changed"] = False
        self.assertFalse(all(checks.values()))

    def test_opengl_application_requires_live_hardware_context_and_driver(self) -> None:
        activity = {
            "process_alive": True,
            "graphics_motion": {"changed": True},
            "opengl": {
                "api": "OpenGL",
                "renderer": "AMD Radeon Graphics (radeonsi)",
                "source": "glmark2-wayland",
                "vendor": "AMD",
                "version": "4.6 (Core Profile) Mesa",
            },
        }
        gpu = {
            "gpu_mappings": ["/usr/lib/x86_64-linux-gnu/libgallium.so"],
            "render_nodes": ["/dev/dri/renderD128"],
        }
        checks = live_run.application_boundary_checks(
            application="opengl",
            application_activity=activity,
            application_gpu=gpu,
            log_evidence={"wayland_protocol": {}},
            render_node=Path("/dev/dri/renderD128"),
        )
        self.assertTrue(all(checks.values()))

        activity["opengl"] = {**activity["opengl"], "renderer": "llvmpipe"}
        software = live_run.application_boundary_checks(
            application="opengl",
            application_activity=activity,
            application_gpu=gpu,
            log_evidence={"wayland_protocol": {}},
            render_node=Path("/dev/dri/renderD128"),
        )
        self.assertFalse(software["opengl_hardware_renderer"])

    def test_opengl_renderer_evidence_is_exact_and_private(self) -> None:
        expected = {
            "api": "OpenGL",
            "renderer": "AMD Radeon Graphics (radeonsi)",
            "source": "glmark2-wayland",
            "vendor": "AMD",
            "version": "4.6 (Compatibility Profile) Mesa",
        }
        output = """
=======================================================
    glmark2 2023.01
=======================================================
    OpenGL Information
    GL_VENDOR:      AMD
    GL_RENDERER:    AMD Radeon Graphics (radeonsi)
    GL_VERSION:     4.6 (Compatibility Profile) Mesa
=======================================================
"""
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "opengl.stdout"
            path.write_text(output, encoding="utf-8")
            path.chmod(0o600)
            self.assertEqual(live_run.load_opengl_evidence(path), expected)

            path.write_text(
                output.replace("AMD\n", "AMD\n    GL_VENDOR: Intel\n"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(live_run.LabFailure, "invalid vendor"):
                live_run.load_opengl_evidence(path)

    def test_h264_only_packet_check_rejects_empty_and_rgb_windows(self) -> None:
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

    def test_primary_hardware_accepts_only_exact_warmup_and_production(self) -> None:
        warmup = [
            {
                "encoding": "webp",
                "h": 1173,
                "options": {"flush": 0, "window-size": [796, 1173]},
                "payload_bytes": 40 + sequence,
                "relative_info": f"screen-updates/1/{100 + sequence}/0.info",
                "sequence": sequence,
                "w": 796,
                "x": 0,
                "y": 0,
            }
            for sequence in range(1, 9)
        ]
        production = [
            {
                "encoding": "h264",
                "h": 1172,
                "payload_bytes": 100,
                "relative_info": "screen-updates/1/109/0.info",
                "sequence": 9,
                "w": 796,
                "x": 0,
                "y": 0,
                "options": {
                    "frame": 0,
                    "flush": 0,
                    "type": "IDR",
                    "window-size": [796, 1173],
                },
            },
            {
                "encoding": "rgb24",
                "h": 1,
                "payload_bytes": 20,
                "relative_info": "screen-updates/1/110/0.info",
                "sequence": 10,
                "w": 796,
                "x": 0,
                "y": 1172,
                "options": {
                    "flush": 1,
                    "rgb_format": "RGBX",
                    "window-size": [796, 1173],
                },
            },
            {
                "encoding": "h264",
                "h": 1172,
                "payload_bytes": 90,
                "relative_info": "screen-updates/1/110/1.info",
                "sequence": 11,
                "w": 796,
                "x": 0,
                "y": 0,
                "options": {
                    "frame": 1,
                    "flush": 0,
                    "type": "P",
                    "window-size": [796, 1173],
                },
            },
            {
                "encoding": "rgb24",
                "h": 1,
                "payload_bytes": 20,
                "relative_info": "screen-updates/1/111/0.info",
                "sequence": 12,
                "w": 796,
                "x": 0,
                "y": 1172,
                "options": {
                    "flush": 1,
                    "rgb_format": "RGBX",
                    "window-size": [796, 1173],
                },
            },
            {
                "encoding": "h264",
                "h": 1172,
                "payload_bytes": 85,
                "relative_info": "screen-updates/1/111/1.info",
                "sequence": 13,
                "w": 796,
                "x": 0,
                "y": 0,
                "options": {
                    "frame": 2,
                    "flush": 0,
                    "type": "P",
                    "window-size": [796, 1173],
                },
            },
        ]
        updates = {
            "count": 13,
            "encodings": ["h264", "rgb24", "webp"],
            "h264_stimulus": {
                "baseline_sequence": 10,
                "first_sequence": 9,
                "last_sequence": 13,
                "window_size": [796, 1173],
            },
            "initial_pixel_format": "BGRX",
            "updates": [*warmup, *production],
            "window_id": 1,
        }
        self.assertEqual(
            live_run.primary_h264_packet_contract_name(
                "hardware", "adaptive-alpha"
            ),
            "alpha_safe_warmup_then_h264_with_only_lossless_rgb_edges",
        )
        for pixel_format in ("BGRX", "RGBX"):
            with self.subTest(pixel_format=pixel_format):
                candidate = {**updates, "initial_pixel_format": pixel_format}
                suffix = live_run.hardware_h264_production_updates(candidate)
                self.assertIsNotNone(suffix)
                assert suffix is not None
                self.assertEqual(
                    [packet["sequence"] for packet in suffix["updates"]],
                    [9, 10, 11, 12, 13],
                )
                self.assertFalse(
                    live_run.primary_h264_packets_valid(
                        "hardware", "adaptive-alpha", candidate
                    )
                )

        alpha_rgb32 = json.loads(json.dumps(updates))
        alpha_rgb32["updates"][0].update(
            {
                "encoding": "rgb32",
                "options": {
                    "flush": 0,
                    "rgb_format": "RGBA",
                    "window-size": [796, 1173],
                },
            }
        )
        alpha_rgb32["encodings"] = ["h264", "rgb24", "rgb32", "webp"]
        self.assertIsNotNone(
            live_run.hardware_h264_production_updates(alpha_rgb32)
        )

        invalid: dict[str, dict[str, object]] = {}
        for pixel_format in (None, "BGRA", "RGBA"):
            candidate = json.loads(json.dumps(updates))
            if pixel_format is None:
                candidate.pop("initial_pixel_format")
            else:
                candidate["initial_pixel_format"] = pixel_format
            invalid[f"initial pixel format {pixel_format!r}"] = candidate
        rgb24_warmup = json.loads(json.dumps(updates))
        rgb24_warmup["updates"][0]["encoding"] = "rgb24"
        invalid["rgb24 warmup"] = rgb24_warmup
        opaque_rgb32_warmup = json.loads(json.dumps(updates))
        opaque_rgb32_warmup["updates"][0].update(
            {"encoding": "rgb32", "options": {"rgb_format": "RGBX"}}
        )
        opaque_rgb32_warmup["encodings"] = [
            "h264",
            "rgb24",
            "rgb32",
            "webp",
        ]
        invalid["opaque rgb32 warmup"] = opaque_rgb32_warmup
        interior_rgb = json.loads(json.dumps(updates))
        interior_rgb["updates"][9]["y"] = 1171
        invalid["interior rgb suffix"] = interior_rgb
        webp_suffix = json.loads(json.dumps(updates))
        webp_suffix["updates"][9]["encoding"] = "webp"
        invalid["webp after h264"] = webp_suffix
        missing_production = json.loads(json.dumps(updates))
        missing_production["updates"] = missing_production["updates"][:8]
        missing_production["count"] = 8
        missing_production["encodings"] = ["webp"]
        invalid["missing h264 production"] = missing_production
        sequence_gap = json.loads(json.dumps(updates))
        sequence_gap["updates"].pop(3)
        sequence_gap["count"] = 12
        invalid["sequence gap"] = sequence_gap
        zero_payload = json.loads(json.dumps(updates))
        zero_payload["updates"][0]["payload_bytes"] = 0
        invalid["zero warmup payload"] = zero_payload
        for name, candidate in invalid.items():
            with self.subTest(name=name):
                self.assertIsNone(
                    live_run.hardware_h264_production_updates(candidate)
                )
                self.assertFalse(
                    live_run.primary_h264_packets_valid(
                        "hardware", "adaptive-alpha", candidate
                    )
                )

    def test_hardware_production_starts_at_first_h264_damage_group(self) -> None:
        edge = {
            "encoding": "rgb24",
            "h": 1,
            "payload_bytes": 20,
            "relative_info": "screen-updates/1/200/0.info",
            "sequence": 1,
            "w": 796,
            "x": 0,
            "y": 1172,
            "options": {
                "flush": 1,
                "rgb_format": "RGBX",
                "window-size": [796, 1173],
            },
        }
        h264 = {
            "encoding": "h264",
            "h": 1172,
            "payload_bytes": 100,
            "relative_info": "screen-updates/1/200/1.info",
            "sequence": 2,
            "w": 796,
            "x": 0,
            "y": 0,
            "options": {
                "flush": 0,
                "frame": 0,
                "type": "IDR",
                "window-size": [796, 1173],
            },
        }
        updates = {
            "count": 2,
            "encodings": ["h264", "rgb24"],
            "h264_stimulus": {
                "baseline_sequence": 1,
                "first_sequence": 1,
                "last_sequence": 2,
                "window_size": [796, 1173],
            },
            "initial_pixel_format": "BGRX",
            "updates": [edge, h264],
            "window_id": 1,
        }
        suffix = live_run.hardware_h264_production_updates(updates)
        self.assertIsNotNone(suffix)
        assert suffix is not None
        self.assertEqual(
            [packet["sequence"] for packet in suffix["updates"]],
            [1, 2],
        )

        dangling = json.loads(json.dumps(updates))
        dangling_edge = json.loads(json.dumps(edge))
        dangling_edge.update(
            {
                "relative_info": "screen-updates/1/199/0.info",
                "sequence": 1,
            }
        )
        dangling_edge["options"]["flush"] = 0
        for sequence, packet in enumerate(dangling["updates"], 2):
            packet["sequence"] = sequence
        dangling["updates"] = [dangling_edge, *dangling["updates"]]
        dangling["count"] = 3
        self.assertIsNone(live_run.hardware_h264_production_updates(dangling))

    def test_damage_flush_metadata_survives_millisecond_directory_collision(
        self,
    ) -> None:
        packets: list[dict[str, object]] = []
        for frame in range(2):
            sequence = frame * 2 + 1
            packets.extend(
                (
                    {
                        "encoding": "rgb24",
                        "h": 1,
                        "options": {
                            "flush": 1,
                            "rgb_format": "BGRX",
                            "window-size": [80, 101],
                        },
                        "payload_bytes": 10,
                        "relative_info": (
                            f"screen-updates/1/200/{frame * 2}.info"
                        ),
                        "sequence": sequence,
                        "w": 80,
                        "x": 0,
                        "y": 100,
                    },
                    {
                        "encoding": "h264",
                        "h": 100,
                        "options": {
                            "flush": 0,
                            "frame": frame,
                            "type": "IDR" if frame == 0 else "P",
                            "window-size": [80, 101],
                        },
                        "payload_bytes": 100,
                        "relative_info": (
                            f"screen-updates/1/200/{frame * 2 + 1}.info"
                        ),
                        "sequence": sequence + 1,
                        "w": 80,
                        "x": 0,
                        "y": 0,
                    },
                )
            )
        groups = live_run._ordered_saved_damage_groups(packets, 1)
        self.assertIsNotNone(groups)
        assert groups is not None
        self.assertEqual(
            [[packet["sequence"] for packet in group] for group in groups],
            [[1, 2], [3, 4]],
        )
        self.assertTrue(
            live_run.h264_with_lossless_rgb_edges(
                {
                    "count": 4,
                    "encodings": ["h264", "rgb24"],
                    "updates": packets,
                    "window_id": 1,
                }
            )
        )

    def test_hardware_phase_uses_saved_source_size_not_client_geometry(self) -> None:
        packets = [
            {
                "encoding": "h264",
                "h": 480,
                "options": {
                    "flush": 0,
                    "frame": 0,
                    "type": "IDR",
                    "window-size": [640, 480],
                },
                "payload_bytes": 100,
                "relative_info": "screen-updates/1/1000/0.info",
                "sequence": 1,
                "w": 640,
                "x": 0,
                "y": 0,
            },
            {
                "encoding": "h264",
                "h": 480,
                "options": {
                    "flush": 0,
                    "frame": 1,
                    "type": "P",
                    "window-size": [640, 480],
                },
                "payload_bytes": 90,
                "relative_info": "screen-updates/1/1125/0.info",
                "sequence": 2,
                "w": 640,
                "x": 0,
                "y": 0,
            },
        ]
        updates = {
            "count": 2,
            "encodings": ["h264"],
            "initial_pixel_format": "BGRX",
            "updates": packets,
            "window_id": 1,
        }

        def wait_for_once(
            description: str,
            predicate: object,
            *,
            timeout: int,
        ) -> None:
            self.assertEqual(description, "stable hardware H.264 phase baseline")
            self.assertEqual(timeout, 15)
            self.assertTrue(callable(predicate))
            assert callable(predicate)
            self.assertTrue(predicate())

        with (
            patch.object(
                live_run,
                "synchronize_saved_updates",
                return_value=updates,
            ),
            patch.object(live_run, "wait_for", side_effect=wait_for_once),
        ):
            interval = live_run.begin_hardware_h264_stimulus(
                "server",
                LIVE_DIRECTORY,
                1,
            )

        self.assertEqual(
            interval,
            {
                "baseline_sequence": 2,
                "first_sequence": 1,
                "window_size": [640, 480],
            },
        )

    def test_hardware_phase_excludes_safe_resize_epilogue_but_proves_va_stream(
        self,
    ) -> None:
        packets: list[dict[str, object]] = [
            {
                "encoding": "webp",
                "h": 64,
                "options": {"flush": 0, "window-size": [64, 64]},
                "payload_bytes": 20,
                "relative_info": "screen-updates/1/100/0.info",
                "sequence": 1,
                "w": 64,
                "x": 0,
                "y": 0,
            },
            {
                "encoding": "h264",
                "h": 64,
                "options": {
                    "flush": 0,
                    "frame": 0,
                    "type": "IDR",
                    "window-size": [64, 64],
                },
                "payload_bytes": 30,
                "relative_info": "screen-updates/1/101/0.info",
                "sequence": 2,
                "w": 64,
                "x": 0,
                "y": 0,
            },
            {
                "encoding": "webp",
                "h": 101,
                "options": {"flush": 0, "window-size": [80, 101]},
                "payload_bytes": 40,
                "relative_info": "screen-updates/1/102/0.info",
                "sequence": 3,
                "w": 80,
                "x": 0,
                "y": 0,
            },
        ]
        for frame in range(10):
            sequence = 4 + frame * 2
            group = 2000 + frame * 125
            packets.extend(
                (
                    {
                        "encoding": "rgb24",
                        "h": 1,
                        "options": {
                            "flush": 1,
                            "rgb_format": "BGRX",
                            "window-size": [80, 101],
                        },
                        "payload_bytes": 10,
                        "relative_info": (
                            f"screen-updates/1/{group}/0.info"
                        ),
                        "sequence": sequence,
                        "w": 80,
                        "x": 0,
                        "y": 100,
                    },
                    {
                        "encoding": "h264",
                        "h": 100,
                        "options": {
                            "flush": 0,
                            "frame": frame,
                            "type": "IDR" if frame == 0 else "P",
                            "window-size": [80, 101],
                        },
                        "payload_bytes": 100 + frame,
                        "relative_info": (
                            f"screen-updates/1/{group}/1.info"
                        ),
                        "sequence": sequence + 1,
                        "w": 80,
                        "x": 0,
                        "y": 0,
                    },
                )
            )
        packets.extend(
            (
                {
                    "encoding": "rgb24",
                    "h": 1,
                    "options": {
                        "flush": 1,
                        "rgb_format": "BGRX",
                        "window-size": [160, 101],
                    },
                    "payload_bytes": 10,
                    "relative_info": "screen-updates/1/4000/0.info",
                    "sequence": 24,
                    "w": 80,
                    "x": 0,
                    "y": 100,
                },
                {
                    "encoding": "h264",
                    "h": 100,
                    "options": {
                        "flush": 0,
                        "frame": 10,
                        "type": "P",
                        "window-size": [160, 101],
                    },
                    "payload_bytes": 80,
                    "relative_info": "screen-updates/1/4000/1.info",
                    "sequence": 25,
                    "w": 80,
                    "x": 0,
                    "y": 0,
                },
                {
                    "encoding": "webp",
                    "h": 101,
                    "options": {"flush": 0, "window-size": [160, 101]},
                    "payload_bytes": 50,
                    "relative_info": "screen-updates/1/4001/0.info",
                    "sequence": 26,
                    "w": 80,
                    "x": 80,
                    "y": 0,
                },
                {
                    "encoding": "rgb24",
                    "h": 1,
                    "options": {
                        "flush": 1,
                        "rgb_format": "BGRX",
                        "window-size": [160, 101],
                    },
                    "payload_bytes": 10,
                    "relative_info": "screen-updates/1/4002/0.info",
                    "sequence": 27,
                    "w": 80,
                    "x": 0,
                    "y": 100,
                },
                {
                    "encoding": "h264",
                    "h": 100,
                    "options": {
                        "flush": 0,
                        "frame": 11,
                        "type": "P",
                        "window-size": [160, 101],
                    },
                    "payload_bytes": 70,
                    "relative_info": "screen-updates/1/4002/1.info",
                    "sequence": 28,
                    "w": 80,
                    "x": 0,
                    "y": 0,
                },
                {
                    "encoding": "rgb24",
                    "h": 1,
                    "options": {
                        "flush": 1,
                        "rgb_format": "BGRX",
                        "window-size": [160, 101],
                    },
                    "payload_bytes": 10,
                    "relative_info": "screen-updates/1/4003/0.info",
                    "sequence": 29,
                    "w": 80,
                    "x": 0,
                    "y": 100,
                },
            )
        )
        updates = {
            "count": len(packets),
            "encodings": ["h264", "rgb24", "webp"],
            "h264_stimulus": {
                "baseline_sequence": 5,
                "first_sequence": 4,
                "last_sequence": 23,
                "window_size": [80, 101],
            },
            "initial_pixel_format": "BGRX",
            "updates": packets,
            "window_id": 1,
        }
        baseline = {
            **updates,
            "count": 23,
            "encodings": ["h264", "rgb24", "webp"],
            "updates": packets[:23],
        }
        self.assertEqual(
            live_run.hardware_h264_phase_start_sequence(baseline, (80, 101)),
            4,
        )
        self.assertTrue(live_run.hardware_h264_history_valid(updates))
        production = live_run.hardware_h264_production_updates(updates)
        self.assertIsNotNone(production)
        assert production is not None
        self.assertEqual(
            [production["updates"][0]["sequence"], production["updates"][-1]["sequence"]],
            [4, 23],
        )
        metrics = live_run.h264_production_metrics("hardware", updates)
        self.assertTrue(all(live_run.h264_dominance_checks(metrics).values()))
        self.assertEqual(live_run.h264_production_metrics("opengl", updates), metrics)
        context_updates = live_run.hardware_h264_context_updates(updates)
        self.assertIsNotNone(context_updates)
        assert context_updates is not None
        self.assertEqual(context_updates["updates"][-1]["sequence"], 28)

        def context(side: str, frames: int) -> dict[str, object]:
            return {
                "begin_successes": frames,
                "completed_frames": frames,
                "context": "0x5",
                "created": True,
                "end_successes": frames,
                "entrypoint": (
                    "VAEntrypointEncSlice" if side == "server" else "VAEntrypointVLD"
                ),
                "file": f"{side}-va.trace",
                "generation": 1,
                "height": 112,
                "incomplete_frames": 0,
                "profile": (
                    "VAProfileH264Main" if side == "server" else "VAProfileH264High"
                ),
                "render_successes": frames,
                "width": 80,
            }

        matched = live_run.match_h264_production_stream(
            context_updates,
            {"contexts": [context("server", 13)], "files": ["server-va.trace"]},
            {"contexts": [context("client", 12)], "files": ["client-va.trace"]},
            allow_alpha_gaps=True,
            allow_lossless_rgb_edges=True,
            allow_terminal_server_frame=True,
            allow_window_resize_gaps=True,
        )
        self.assertTrue(matched["all_streams_proven"])
        self.assertEqual(matched["matched_stream"]["packet_count"], 12)
        strict = live_run.match_h264_production_stream(
            context_updates,
            {"contexts": [context("server", 13)], "files": ["server-va.trace"]},
            {"contexts": [context("client", 12)], "files": ["client-va.trace"]},
            allow_alpha_gaps=True,
            allow_lossless_rgb_edges=True,
        )
        self.assertFalse(strict["all_streams_proven"])

        client_lagged = live_run.match_h264_production_stream(
            context_updates,
            {"contexts": [context("server", 12)], "files": ["server-va.trace"]},
            {"contexts": [context("client", 11)], "files": ["client-va.trace"]},
            allow_alpha_gaps=True,
            allow_lossless_rgb_edges=True,
            allow_terminal_client_frame=True,
            allow_window_resize_gaps=True,
        )
        self.assertTrue(client_lagged["all_streams_proven"])
        self.assertTrue(
            client_lagged["matched_stream"]["terminal_client_frame_inflight"]
        )
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            lines = []
            for packet in context_updates["updates"]:
                lines.append(
                    "process_draw: {payload_bytes} bytes for window 1, "
                    "sequence {sequence}, {w}x{h} at {x},{y} using {encoding} "
                    "encoding with options=typedict({options!r})".format(**packet)
                )
            (directory / "client.stdout").write_text(
                "\n".join(lines) + "\n",
                encoding="utf-8",
            )
            self.assertTrue(
                live_run.terminal_client_h264_frame_inflight(
                    directory,
                    context_updates,
                )
            )
            (directory / "client.stdout").write_text(
                "\n".join(lines[:-1]) + "\n",
                encoding="utf-8",
            )
            self.assertFalse(
                live_run.terminal_client_h264_frame_inflight(
                    directory,
                    context_updates,
                )
            )

    def test_adaptive_h264_requires_sustained_temporal_and_pixel_dominance(self) -> None:
        width, height = 800, 600
        warmup = {
            "encoding": "webp",
            "h": height,
            "options": {"flush": 0, "window-size": [width, height]},
            "payload_bytes": 50,
            "relative_info": "screen-updates/1/1000/0.info",
            "sequence": 1,
            "w": width,
            "x": 0,
            "y": 0,
        }
        h264_packets = []
        for frame in range(10):
            h264_packets.append(
                {
                    "encoding": "h264",
                    "h": height,
                    "options": {
                        "flush": 0,
                        "frame": frame,
                        "type": "IDR" if frame == 0 else "P",
                        "window-size": [width, height],
                    },
                    "payload_bytes": 100 + frame,
                    "relative_info": (
                        f"screen-updates/1/{2000 + frame * 125}/0.info"
                    ),
                    "sequence": frame + 2,
                    "w": width,
                    "x": 0,
                    "y": 0,
                }
            )
        updates = {
            "count": 11,
            "encodings": ["h264", "webp"],
            "initial_pixel_format": "BGRX",
            "updates": [warmup, *h264_packets],
            "window_id": 1,
        }
        for application in ("hardware", "zed"):
            with self.subTest(application=application):
                candidate = updates
                if application == "zed":
                    candidate = {
                        **updates,
                        "h264_stimulus": {
                            "baseline_sequence": 1,
                            "last_sequence": 11,
                            "window_size": [width, height],
                        },
                    }
                else:
                    candidate = {
                        **updates,
                        "h264_stimulus": {
                            "baseline_sequence": 2,
                            "first_sequence": 2,
                            "last_sequence": 11,
                            "window_size": [width, height],
                        },
                    }
                metrics = live_run.h264_production_metrics(application, candidate)
                self.assertEqual(metrics["h264_main_frame_count"], 10)
                self.assertEqual(metrics["h264_damage_span_ms"], 1125)
                self.assertEqual(metrics["h264_main_pixels"], 10 * width * height)
                self.assertEqual(
                    metrics["aggregate_encoded_pixels"], 10 * width * height
                )
                self.assertAlmostEqual(
                    metrics["aggregate_h264_pixel_ratio"], 1.0
                )
                self.assertTrue(all(live_run.h264_dominance_checks(metrics).values()))
                self.assertTrue(
                    live_run.primary_h264_packets_valid(
                        application,
                        "adaptive-alpha",
                        candidate,
                    )
                )

        hardware_updates = {
            **updates,
            "h264_stimulus": {
                "baseline_sequence": 2,
                "first_sequence": 2,
                "last_sequence": 11,
                "window_size": [width, height],
            },
        }
        metrics = live_run.h264_production_metrics("hardware", hardware_updates)
        matched = {
            "matched_stream": {
                "damage_span_ms": 1125,
                "packet_count": 10,
                "pixel_count": 10 * width * height,
            }
        }
        self.assertTrue(
            all(
                live_run.matched_h264_stream_stability_checks(
                    matched,
                    metrics,
                ).values()
            )
        )
        invalid_metrics = {
            **metrics,
            "aggregate_encoded_pixels": 2 * metrics["h264_main_pixels"],
            "h264_damage_span_ms": 999,
            "h264_main_frame_count": 9,
            "minimum_frame_h264_pixels": 98,
            "minimum_frame_window_pixels": 100,
        }
        self.assertFalse(all(live_run.h264_dominance_checks(invalid_metrics).values()))
        unmatched = {
            "matched_stream": {
                "damage_span_ms": 999,
                "packet_count": 9,
                "pixel_count": 8 * width * height,
            }
        }
        self.assertFalse(
            all(
                live_run.matched_h264_stream_stability_checks(
                    unmatched,
                    metrics,
                ).values()
            )
        )

    def test_zed_adaptive_h264_accepts_only_exact_alpha_groups_around_h264(self) -> None:
        def h264(sequence: int, group: int, frame: int) -> dict[str, object]:
            return {
                "encoding": "h264",
                "h": 600,
                "options": {
                    "flush": 0,
                    "frame": frame,
                    "type": "IDR" if frame == 0 else "P",
                    "window-size": [800, 600],
                },
                "payload_bytes": 100,
                "relative_info": f"screen-updates/1/{group}/0.info",
                "sequence": sequence,
                "w": 800,
                "x": 0,
                "y": 0,
            }

        alpha = {
            "encoding": "webp",
            "h": 600,
            "options": {"flush": 0, "window-size": [800, 600]},
            "payload_bytes": 80,
            "relative_info": "screen-updates/1/200/0.info",
            "sequence": 2,
            "w": 800,
            "x": 0,
            "y": 0,
        }
        updates = {
            "count": 3,
            "encodings": ["h264", "webp"],
            "initial_pixel_format": "BGRA",
            "updates": [h264(1, 100, 0), alpha, h264(3, 300, 1)],
            "window_id": 1,
        }
        production = live_run.adaptive_h264_production_updates(updates)
        self.assertIsNotNone(production)
        assert production is not None
        self.assertEqual(
            [packet["sequence"] for packet in production["updates"]],
            [1, 3],
        )
        self.assertEqual(len(live_run.h264_packet_streams(updates)), 2)
        streams = live_run.h264_packet_streams(
            updates,
            allow_alpha_gaps=True,
            allow_lossless_rgb_edges=True,
        )
        self.assertEqual(len(streams), 1)
        self.assertEqual(streams[0]["interleaved_alpha_sequences"], [2])

        alpha_rgb32 = json.loads(json.dumps(updates))
        alpha_rgb32["updates"][1].update(
            {
                "encoding": "rgb32",
                "options": {
                    "flush": 0,
                    "rgb_format": "RGBA",
                    "window-size": [800, 600],
                },
            }
        )
        alpha_rgb32["encodings"] = ["h264", "rgb32"]
        self.assertIsNotNone(
            live_run.adaptive_h264_production_updates(alpha_rgb32)
        )

        invalid = {
            "full rgb fallback": {
                **updates,
                "count": 1,
                "encodings": ["webp"],
                "updates": [alpha],
            },
            "rgb24 alpha group": {
                **updates,
                "encodings": ["h264", "rgb24"],
                "updates": [
                    updates["updates"][0],
                    {**alpha, "encoding": "rgb24"},
                    updates["updates"][2],
                ],
            },
            "opaque rgb32 alpha group": {
                **alpha_rgb32,
                "updates": [
                    alpha_rgb32["updates"][0],
                    {
                        **alpha_rgb32["updates"][1],
                        "options": {"flush": 0, "rgb_format": "RGBX"},
                    },
                    alpha_rgb32["updates"][2],
                ],
            },
            "alpha group missing window size": {
                **updates,
                "updates": [
                    updates["updates"][0],
                    {**alpha, "options": {"flush": 0}},
                    updates["updates"][2],
                ],
            },
            "alpha group outside window": {
                **updates,
                "updates": [
                    updates["updates"][0],
                    {**alpha, "w": 2, "x": 799},
                    updates["updates"][2],
                ],
            },
        }
        for name, candidate in invalid.items():
            with self.subTest(name=name):
                self.assertIsNone(
                    live_run.adaptive_h264_production_updates(candidate)
                )

    def test_auxiliary_alpha_packet_check_accepts_only_webp_or_alpha_rgb32(self) -> None:
        webp_packet = {
            "encoding": "webp",
            "h": 320,
            "options": {"flush": 0, "window-size": [480, 320]},
            "payload_bytes": 10,
            "relative_info": "screen-updates/2/100/0.info",
            "sequence": 1,
            "w": 480,
            "x": 0,
            "y": 0,
        }
        rgb32_packet = {
            "encoding": "rgb32",
            "h": 320,
            "options": {
                "flush": 0,
                "rgb_format": "BGRA",
                "window-size": [480, 320],
            },
            "payload_bytes": 20,
            "relative_info": "screen-updates/2/100/0.info",
            "sequence": 1,
            "w": 480,
            "x": 0,
            "y": 0,
        }
        webp = {
            "count": 1,
            "encodings": ["webp"],
            "initial_pixel_format": "RGBA",
            "updates": [webp_packet],
            "window_id": 2,
        }
        rgb32 = {
            "count": 1,
            "encodings": ["rgb32"],
            "initial_pixel_format": "BGRA",
            "updates": [rgb32_packet],
            "window_id": 2,
        }
        mixed_rgb32 = json.loads(json.dumps(rgb32_packet))
        mixed_rgb32.update(
            {
                "relative_info": "screen-updates/2/101/0.info",
                "sequence": 2,
            }
        )
        mixed = {
            "count": 2,
            "encodings": ["rgb32", "webp"],
            "initial_pixel_format": "RGBA",
            "updates": [webp_packet, mixed_rgb32],
            "window_id": 2,
        }
        for candidate in (webp, rgb32, mixed):
            with self.subTest(candidate=candidate["encodings"]):
                self.assertTrue(live_run.only_positive_alpha_capable_packets(candidate))

        invalid = {
            "empty": {
                "count": 0,
                "encodings": [],
                "initial_pixel_format": "RGBA",
                "updates": [],
            },
            "zero payload": {
                **webp,
                "updates": [{**webp_packet, "payload_bytes": 0}],
            },
            "h264": {
                **webp,
                "encodings": ["h264"],
                "updates": [{**webp_packet, "encoding": "h264"}],
            },
            "rgb24": {
                **webp,
                "encodings": ["rgb24"],
                "updates": [{**webp_packet, "encoding": "rgb24"}],
            },
            "opaque source": {**webp, "initial_pixel_format": "BGRX"},
            "opaque rgb32 packet": {
                **rgb32,
                "updates": [
                    {
                        **rgb32_packet,
                        "options": {
                            **rgb32_packet["options"],
                            "rgb_format": "RGBX",
                        },
                    }
                ],
            },
            "missing rgb32 format": {
                **rgb32,
                "updates": [
                    {
                        **rgb32_packet,
                        "options": {
                            "flush": 0,
                            "window-size": [480, 320],
                        },
                    }
                ],
            },
            "missing window size": {
                **webp,
                "updates": [
                    {**webp_packet, "options": {"flush": 0}}
                ],
            },
            "outside window": {
                **webp,
                "updates": [{**webp_packet, "w": 2, "x": 479}],
            },
            "sequence gap": {
                **mixed,
                "updates": [webp_packet, {**mixed_rgb32, "sequence": 3}],
            },
            "bad saved index": {
                **webp,
                "updates": [
                    {
                        **webp_packet,
                        "relative_info": "screen-updates/2/100/1.info",
                    }
                ],
            },
            "bad flush": {
                **webp,
                "updates": [
                    {
                        **webp_packet,
                        "options": {
                            **webp_packet["options"],
                            "flush": 1,
                        },
                    }
                ],
            },
        }
        for name, candidate in invalid.items():
            with self.subTest(name=name):
                self.assertFalse(
                    live_run.only_positive_alpha_capable_packets(candidate)
                )
        self.assertFalse(live_run.only_positive_alpha_capable_packets(None))

    def test_saved_window_initial_pixel_format_is_exact_and_required(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            window = Path(raw) / "screen-updates" / "2"
            window.mkdir(parents=True)
            info = window / "window.info"
            info.write_text(json.dumps({"pixel-format": "BGRA"}), encoding="utf-8")
            self.assertEqual(
                live_run.saved_window_initial_pixel_format(Path(raw), 2), "BGRA"
            )
            info.write_text(json.dumps({"pixel-format": ""}), encoding="utf-8")
            with self.assertRaisesRegex(live_run.LabFailure, "no pixel format"):
                live_run.saved_window_initial_pixel_format(Path(raw), 2)

    def test_transport_encoding_options(self) -> None:
        configured = live_run.live_config.load_live_cli()
        for role, role_config in configured.items():
            for encoding, transport in role_config["transports"].items():
                for policy in transport["policies"]:
                    with self.subTest(
                        role=role,
                        encoding=encoding,
                        policy=policy,
                    ):
                        self.assertEqual(
                            live_run.transport_encoding_options(
                                encoding,
                                policy,
                                client=role == "client",
                            ),
                            list(
                                live_run.live_config.transport_options(
                                    role,
                                    encoding,
                                    policy,
                                )
                            ),
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
        first["options"].update({"flush": 0, "window-size": [1596, 1173]})
        first["relative_info"] = "screen-updates/1/100/0.info"
        second["options"].update({"flush": 0, "window-size": [1596, 1173]})
        second["relative_info"] = "screen-updates/1/101/1.info"
        edge = {
            "encoding": "rgb24",
            "relative_info": "screen-updates/1/101/0.info",
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
            "window_id": 1,
        }

    def test_adaptive_h264_accepts_only_exact_lossless_codec_edges(self) -> None:
        updates = self.adaptive_edge_updates()
        self.assertTrue(live_run.h264_with_lossless_rgb_edges(updates))
        adaptive_updates = {**updates, "initial_pixel_format": "BGRX"}
        self.assertIsNotNone(
            live_run.adaptive_h264_production_updates(adaptive_updates)
        )
        self.assertFalse(
            live_run.primary_h264_packets_valid(
                "zed", "adaptive-alpha", adaptive_updates
            )
        )
        hardware_updates = {**updates, "initial_pixel_format": "BGRX"}
        self.assertFalse(
            live_run.primary_h264_packets_valid(
                "hardware", "adaptive-alpha", hardware_updates
            )
        )

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

    def test_h264_damage_groups_require_exact_indexes_flushes_and_edges(self) -> None:
        updates = self.adaptive_edge_updates()
        invalid: dict[str, dict[str, object]] = {}

        dangling = json.loads(json.dumps(updates))
        dangling["updates"].pop()
        dangling["count"] = 2
        invalid["dangling edge"] = dangling

        different_parent = json.loads(json.dumps(updates))
        different_parent["updates"][2]["relative_info"] = (
            "screen-updates/1/102/0.info"
        )
        invalid["edge and h264 in different groups"] = different_parent

        bad_index = json.loads(json.dumps(updates))
        bad_index["updates"][2]["relative_info"] = "screen-updates/1/101/2.info"
        invalid["noncontiguous group index"] = bad_index

        missing_index = json.loads(json.dumps(updates))
        missing_index["updates"][1].pop("relative_info")
        invalid["missing group index"] = missing_index

        bad_flush = json.loads(json.dumps(updates))
        bad_flush["updates"][1]["options"]["flush"] = 2
        invalid["non-descending flush"] = bad_flush

        edge_after_h264 = json.loads(json.dumps(updates))
        edge_packet = edge_after_h264["updates"][1]
        h264_packet = edge_after_h264["updates"][2]
        edge_packet["relative_info"] = "screen-updates/1/101/1.info"
        edge_packet["sequence"] = 3
        edge_packet["options"]["flush"] = 0
        h264_packet["relative_info"] = "screen-updates/1/101/0.info"
        h264_packet["sequence"] = 2
        h264_packet["options"]["flush"] = 1
        edge_after_h264["updates"][1:3] = [h264_packet, edge_packet]
        invalid["edge after terminal h264"] = edge_after_h264

        for name, candidate in invalid.items():
            with self.subTest(name=name):
                self.assertFalse(live_run.h264_with_lossless_rgb_edges(candidate))

        right = {
            "encoding": "rgb24",
            "sequence": 1,
            "x": 1596,
            "y": 0,
            "w": 1,
            "h": 1173,
            "payload_bytes": 64,
            "relative_info": "screen-updates/1/300/0.info",
            "options": {
                "flush": 2,
                "rgb_format": "RGBX",
                "window-size": [1597, 1173],
            },
        }
        bottom = {
            "encoding": "rgb24",
            "sequence": 2,
            "x": 0,
            "y": 1172,
            "w": 1597,
            "h": 1,
            "payload_bytes": 64,
            "relative_info": "screen-updates/1/300/1.info",
            "options": {
                "flush": 1,
                "rgb_format": "RGBX",
                "window-size": [1597, 1173],
            },
        }
        h264 = self.packet(3, 0, scaled=False)
        h264.update(
            {
                "relative_info": "screen-updates/1/300/2.info",
                "options": {
                    "flush": 0,
                    "frame": 0,
                    "type": "IDR",
                    "window-size": [1597, 1173],
                },
            }
        )
        both_edges = {
            "count": 3,
            "encodings": ["h264", "rgb24"],
            "updates": [right, bottom, h264],
            "window_id": 1,
        }
        self.assertTrue(live_run.h264_with_lossless_rgb_edges(both_edges))

        duplicate = json.loads(json.dumps(both_edges))
        duplicate["updates"][0].update(
            {"x": 0, "y": 1172, "w": 1597, "h": 1}
        )
        self.assertFalse(live_run.h264_with_lossless_rgb_edges(duplicate))

        mismatched_crop = json.loads(json.dumps(updates))
        mismatched_crop["updates"][0]["w"] = 1595
        mismatched_crop["updates"][0]["options"]["window-size"] = [1596, 1173]
        self.assertFalse(live_run.h264_with_lossless_rgb_edges(mismatched_crop))

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

    def test_adaptive_alpha_phases_require_all_h264_streams_to_be_va_proven(
        self,
    ) -> None:
        first = self.packet(1, 0)
        second = self.packet(3, 0)
        for group, packet in ((100, first), (102, second)):
            packet["relative_info"] = f"screen-updates/1/{group}/0.info"
            packet["options"].update(
                {"flush": 0, "window-size": [1596, 1173]}
            )
        alpha = {
            "encoding": "webp",
            "h": 1173,
            "options": {"flush": 0, "window-size": [1596, 1173]},
            "payload_bytes": 50,
            "relative_info": "screen-updates/1/101/0.info",
            "sequence": 2,
            "w": 1596,
            "x": 0,
            "y": 0,
        }
        updates = {
            "count": 3,
            "encodings": ["h264", "webp"],
            "initial_pixel_format": "BGRA",
            "updates": [first, alpha, second],
            "window_id": 1,
        }
        contexts = {
            "server": {"contexts": [self.context("VAEntrypointEncSlice")]},
            "client": {"contexts": [self.context("VAEntrypointVLD")]},
        }
        result = live_run.match_h264_production_stream(
            updates,
            contexts["server"],
            contexts["client"],
            allow_alpha_gaps=True,
            allow_lossless_rgb_edges=True,
        )
        self.assertEqual(len(result["candidates"]), 2)
        self.assertEqual(len(result["complete_streams"]), 2)
        self.assertTrue(result["all_streams_proven"])

        insufficient = json.loads(json.dumps(contexts))
        insufficient["server"]["contexts"][0]["completed_frames"] = 1
        rejected = live_run.match_h264_production_stream(
            updates,
            insufficient["server"],
            insufficient["client"],
            allow_alpha_gaps=True,
            allow_lossless_rgb_edges=True,
        )
        self.assertFalse(rejected["all_streams_proven"])

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
                (
                    "process_draw: 50 bytes for window 2, sequence 4, "
                    "480x357 at 0,0 using webp encoding with options="
                    "typedict({'flush': 0})"
                ),
                "sending ack: ('window-ack', 2, 480, 357, 4, 100, \"''\")",
                (
                    "process_draw: 40 bytes for window 1, sequence 4, "
                    "1596x1172 at 0,0 using h264 encoding with options="
                    "typedict({'frame': 1, 'type': 'P'})"
                ),
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
