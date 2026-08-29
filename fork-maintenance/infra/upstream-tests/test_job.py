# Copyright (C) 2026 kogeler

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager, nullcontext, redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, call, patch

import job


def image_args() -> argparse.Namespace:
    return argparse.Namespace(
        image="localhost/xpra-ci:test",
        image_input_sha256="1" * 64,
        source="2" * 40,
        workflow_sha256="3" * 64,
    )


class SourceBundleTest(unittest.TestCase):
    def test_prepare_state_rejects_a_symlinked_intermediate_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            project = Path(raw) / "project"
            outside = Path(raw) / "outside"
            (project / ".artifacts").mkdir(parents=True, mode=0o700)
            outside.mkdir(mode=0o700)
            os.symlink(outside, project / ".artifacts" / "fork-maintenance")
            with (
                patch.object(job, "PROJECT_ROOT", project),
                patch.object(
                    job,
                    "STATE_ROOT",
                    project / ".artifacts/fork-maintenance/upstream-tests",
                ),
                self.assertRaises(job.JobError),
            ):
                job.prepare_state()

    def test_uses_the_selected_remote_in_the_bundle_name(self) -> None:
        source = "2" * 40
        with tempfile.TemporaryDirectory() as raw, patch.object(job, "SOURCE_ROOT", Path(raw)):
            self.assertEqual(
                job.source_bundle_path(source, "origin"),
                Path(raw) / f"{source}-origin.bundle",
            )
            self.assertEqual(
                job.source_bundle_path(source, "upstream"),
                Path(raw) / f"{source}-upstream.bundle",
            )

    def test_rejects_an_untrusted_source_remote(self) -> None:
        with self.assertRaisesRegex(job.JobError, "invalid source remote"):
            job.source_bundle_path("2" * 40, "other")

    def test_snapshot_reclaims_only_its_deterministic_partial(self) -> None:
        head = "2" * 40
        source_ref = "refs/remotes/origin/master"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            sources = root / "sources"
            sources.mkdir(mode=0o700)
            partial = sources / f"{head}-origin.bundle.partial"
            partial.write_bytes(b"interrupted")
            partial.chmod(0o600)
            unrelated = sources / "unrelated.bundle.partial"
            unrelated.write_bytes(b"keep")
            unrelated.chmod(0o600)
            args = argparse.Namespace(
                bundle=str(sources / f"{head}-origin.bundle"),
                source_head=head,
                source_host=str(root),
                source_ref=source_ref,
                source_remote="origin",
            )

            def run_bundle(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                self.assertIn("pass_fds", kwargs)
                Path(argv[-2]).write_bytes(b"bundle")
                return subprocess.CompletedProcess(argv, 0, "", "")

            with (
                patch.object(job, "PROJECT_ROOT", root),
                patch.object(job, "SOURCE_ROOT", sources),
                patch.object(job, "prepare_state"),
                patch.object(
                    job,
                    "command",
                    return_value=subprocess.CompletedProcess([], 0, head + "\n", ""),
                ),
                patch.object(job, "verify_source_bundle"),
                patch.object(job.subprocess, "run", side_effect=run_bundle),
            ):
                self.assertEqual(job.source_snapshot(args), 0)
            self.assertEqual((sources / f"{head}-origin.bundle").read_bytes(), b"bundle")
            self.assertFalse(partial.exists())
            self.assertEqual(unrelated.read_bytes(), b"keep")

    def test_make_delegates_snapshot_without_anonymous_staging(self) -> None:
        makefile = (job.RUNNER_ROOT / "Makefile").read_text(encoding="utf-8")
        source_recipe = makefile.split("source-snapshot:", 1)[1].split(
            "\nimage image-check:", 1
        )[0]
        self.assertIn('"$(JOB)" source snapshot', source_recipe)
        self.assertNotIn("mktemp", source_recipe)

    def test_snapshot_rejects_a_special_lock_file(self) -> None:
        head = "2" * 40
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            sources = root / "sources"
            sources.mkdir(mode=0o700)
            lock = sources / f"{head}-origin.bundle.lock"
            os.mkfifo(lock, 0o600)
            args = argparse.Namespace(
                bundle=str(sources / f"{head}-origin.bundle"),
                source_head=head,
                source_host=str(root),
                source_ref="refs/remotes/origin/master",
                source_remote="origin",
            )
            with (
                patch.object(job, "PROJECT_ROOT", root),
                patch.object(job, "SOURCE_ROOT", sources),
                patch.object(job, "prepare_state"),
                self.assertRaisesRegex(job.JobError, "unsafe source bundle lock"),
            ):
                job.source_snapshot(args)

    def test_inherited_bundle_lock_survives_the_parent_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            lock_path = Path(raw) / "source.bundle.lock"
            lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            read_gate, write_gate = os.pipe()
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import os,sys; os.read(int(sys.argv[1]), 1)",
                    str(read_gate),
                ],
                pass_fds=(lock_fd, read_gate),
            )
            os.close(read_gate)
            os.close(lock_fd)
            competitor = os.open(lock_path, os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.write(write_gate, b"x")
                os.close(write_gate)
                child.wait(timeout=5)
                fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
                os.close(competitor)


class CiImageTest(unittest.TestCase):
    def test_image_cache_lock_excludes_a_competing_cache_operation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            images = Path(raw)
            with patch.object(job, "IMAGE_BUILD_ROOT", images):
                with job.image_cache_lock():
                    competitor = os.open(images / ".image-cache.lock", os.O_RDWR)
                    try:
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    finally:
                        os.close(competitor)
                with job.image_cache_lock():
                    pass

    def test_make_cache_remove_delegates_to_the_locked_job_helper(self) -> None:
        makefile = (job.RUNNER_ROOT / "Makefile").read_text(encoding="utf-8")
        recipe = makefile.split("image-remove: image", 1)[1].split(
            "\nimage-background-name-check:", 1
        )[0]
        self.assertIn('"$(JOB)" image cache-remove', recipe)
        self.assertNotIn("$(PODMAN) image rm", recipe)

    def test_cache_remove_refuses_unresolved_image_and_test_owners(self) -> None:
        image = "localhost/xpra-ci:test"
        image_id = "4" * 64
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            images = root / "images"
            runs = root / "runs"
            images.mkdir()
            runs.mkdir()
            context = images / "pending-build"
            context.mkdir()
            (context / "owner.json").write_text("{}\n", encoding="utf-8")
            (context / "owner.json").chmod(0o600)
            with (
                patch.object(job, "IMAGE_BUILD_ROOT", images),
                patch.object(job, "RUN_ROOT", runs),
                patch.object(
                    job,
                    "load_image_record",
                    return_value={"image": image},
                ),
                self.assertRaisesRegex(job.JobError, "image-build job"),
            ):
                job.require_image_cache_unleased(image, image_id)

            (context / "owner.json").unlink()
            context.rmdir()
            owner = runs / "pending-test.owner"
            owner.write_text("{}\n", encoding="utf-8")
            owner.chmod(0o600)
            with (
                patch.object(job, "IMAGE_BUILD_ROOT", images),
                patch.object(job, "RUN_ROOT", runs),
                patch.object(
                    job,
                    "load_test_record",
                    return_value={"image_id": image_id},
                ),
                self.assertRaisesRegex(job.JobError, "test job"),
            ):
                job.require_image_cache_unleased(image, image_id)

    def test_ensure_reuses_only_a_verified_owned_image(self) -> None:
        args = image_args()
        exists = subprocess.CompletedProcess([], 0, "", "")
        with (
            patch.object(job, "prepare_state"),
            patch.object(job, "image_cache_lock", return_value=nullcontext()),
            patch.object(job, "command", return_value=exists) as command,
            patch.object(job, "image_identity", return_value="4" * 64) as identity,
        ):
            self.assertEqual(job.image_ensure(args), 0)

        command.assert_called_once_with(
            ["podman", "image", "exists", args.image],
            check=False,
        )
        identity.assert_called_once_with(
            args.image,
            args.image_input_sha256,
            args.workflow_sha256,
            source=args.source,
        )

    def test_image_identity_requires_the_complete_exact_lab_provenance(self) -> None:
        args = image_args()
        build_run = "12345678-1234-4abc-8def-123456789abc"
        labels = {
            "io.xpra.lab.image-builder": "true",
            "io.xpra.lab.image-build-run-id": build_run,
            "io.xpra.lab.image-input": args.image_input_sha256,
            "io.xpra.lab.source": args.source,
            "io.xpra.lab.workflow": args.workflow_sha256,
            "org.opencontainers.image.base.name": "ubuntu:26.04",
        }
        inspection = {
            "Id": "sha256:" + "4" * 64,
            "Labels": labels,
        }
        with patch.object(job, "inspect_json", return_value=inspection):
            self.assertEqual(
                job.image_identity(
                    args.image,
                    args.image_input_sha256,
                    args.workflow_sha256,
                    source=args.source,
                ),
                "4" * 64,
            )
            for key, value in (
                ("io.xpra.lab.source", "5" * 40),
                ("io.xpra.lab.unexpected", "value"),
            ):
                with self.subTest(key=key), self.assertRaises(job.JobError):
                    inspection["Labels"] = {**labels, key: value}
                    job.image_identity(
                        args.image,
                        args.image_input_sha256,
                        args.workflow_sha256,
                        source=args.source,
                    )
            inspection["Labels"] = {
                **labels,
                "io.xpra.lab.source": "5" * 40,
            }
            self.assertEqual(
                job.removable_image_identity(
                    args.image,
                    args.image_input_sha256,
                    args.workflow_sha256,
                ),
                "4" * 64,
            )

    def test_ensure_streams_inputs_without_a_crash_leaking_context(self) -> None:
        args = image_args()
        missing = subprocess.CompletedProcess([], 1, "", "")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            with (
                patch.object(job, "IMAGE_BUILD_ROOT", root),
                patch.object(job, "prepare_state"),
                patch.object(job, "image_cache_lock", return_value=nullcontext(43)),
                patch.object(job, "uuid") as uuid_module,
                patch.object(job, "command", return_value=missing) as command,
                patch.object(job.container_payload, "stream_to_process") as stream,
                patch.object(job, "image_identity", return_value="5" * 64) as identity,
            ):
                uuid_module.uuid4.return_value = "ci-build-id"
                self.assertEqual(job.image_ensure(args), 0)

            self.assertEqual(tuple(root.iterdir()), ())
            self.assertEqual(
                command.call_args_list,
                [call(["podman", "image", "exists", args.image], check=False)],
            )
            stream.assert_called_once_with(
                job.image_build_argv(args, "ci-build-id", iidfile=None),
                job.image_source_entries(),
                cwd=job.RUNNER_ROOT,
            )
            identity.assert_called_once_with(
                args.image,
                args.image_input_sha256,
                args.workflow_sha256,
                source=args.source,
                build_run_id="ci-build-id",
            )

    def test_background_start_keeps_the_owned_run_name_for_every_path(self) -> None:
        args = image_args()
        args.name = "stream-image-01"
        missing = subprocess.CompletedProcess([], 1, "", "")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            logs = root / "logs"
            logs.mkdir()
            with (
                patch.object(job, "IMAGE_BUILD_ROOT", root),
                patch.object(job, "LOG_ROOT", logs),
                patch.object(job, "prepare_state"),
                patch.object(job, "runner_sha256", return_value="4" * 64),
                patch.object(job, "command", return_value=missing),
                patch.object(job.uuid, "uuid4", return_value="build-id"),
                patch.object(
                    job.background_job,
                    "launch",
                    return_value={"process": {"pid": 12345}},
                ) as launch,
            ):
                self.assertEqual(job.image_start(args), 0)

            context = root / args.name
            self.assertEqual(launch.call_args.kwargs["owner_path"], context / "owner.json")
            self.assertEqual(launch.call_args.kwargs["cwd"], context)
            self.assertEqual(launch.call_args.kwargs["record"]["name"], args.name)
            self.assertEqual(launch.call_args.kwargs["record"]["schema"], 3)
            self.assertEqual(len(launch.call_args.kwargs["pass_fds"]), 1)
            marker = root / f".{args.name}.image-prelaunch.json"
            self.assertTrue(marker.is_file())
            self.assertEqual(json.loads(marker.read_text())["context"], str(context))
            argv = launch.call_args.kwargs["argv"]
            self.assertIn(str(context / "container_payload.py"), argv)
            self.assertEqual(
                [
                    argv[index + 1]
                    for index, value in enumerate(argv)
                    if value == "--entry-json"
                ],
                [f'["{name}", "{name}"]' for name in job.IMAGE_CONTEXT_INPUTS],
            )

    def test_background_launch_retention_preserves_image_prelaunch_and_context(
        self,
    ) -> None:
        args = image_args()
        args.name = "retained-image-launch"
        missing = subprocess.CompletedProcess([], 1, "", "")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            logs = root / "logs"
            logs.mkdir()
            with (
                patch.object(job, "IMAGE_BUILD_ROOT", root),
                patch.object(job, "LOG_ROOT", logs),
                patch.object(job, "prepare_state"),
                patch.object(job, "runner_sha256", return_value="4" * 64),
                patch.object(job, "command", return_value=missing),
                patch.object(job.uuid, "uuid4", return_value="build-id"),
                patch.object(
                    job.background_job,
                    "launch",
                    side_effect=job.background_job.LaunchStateRetained("retained"),
                ),
                self.assertRaises(job.background_job.LaunchStateRetained),
            ):
                job.image_start(args)

            self.assertTrue((root / args.name).is_dir())
            self.assertTrue(
                (root / f".{args.name}.image-prelaunch.json").is_file()
            )

    def test_abort_discards_an_exact_interrupted_image_prelaunch(self) -> None:
        name = "interrupted-image"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            root.chmod(0o700)
            logs = root / "logs"
            images = root / "images"
            logs.mkdir(mode=0o700)
            images.mkdir(mode=0o700)
            context = images / name
            context.mkdir(mode=0o700)
            (context / "partial").write_text("partial\n", encoding="utf-8")
            marker = images / f".{name}.image-prelaunch.json"
            marker.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "owner": job.IMAGE_OWNER,
                        "kind": "image-build-prelaunch",
                        "name": name,
                        "job_id": "12345678-1234-4abc-8def-123456789abc",
                        "context": str(context),
                        "image": "localhost/xpra:test",
                        "input_sha256": "1" * 64,
                        "source": "2" * 40,
                        "workflow_sha256": "3" * 64,
                        "runner_sha256": "4" * 64,
                    }
                ),
                encoding="utf-8",
            )
            marker.chmod(0o600)
            with (
                patch.object(job, "LOG_ROOT", logs),
                patch.object(job, "IMAGE_BUILD_ROOT", images),
                patch.object(job, "prepare_state"),
            ):
                self.assertEqual(job.image_abort(argparse.Namespace(name=name)), 0)
            self.assertFalse(context.exists())
            self.assertFalse(marker.exists())

    def test_completed_current_image_job_must_be_collected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            logs = root / "logs"
            images = root / "images"
            logs.mkdir()
            images.mkdir()
            context = images / "current-image"
            context.mkdir()
            current = "4" * 64
            record = {
                "job_id": "build-id",
                "name": "current-image",
                "runner_sha256": current,
            }
            with (
                patch.object(job, "LOG_ROOT", logs),
                patch.object(job, "IMAGE_BUILD_ROOT", images),
                patch.object(job, "prepare_state"),
                patch.object(job, "load_image_record", return_value=record),
                patch.object(job, "runner_sha256", return_value=current),
                patch.object(
                    job.background_job,
                    "process_state",
                    return_value={"state": "completed"},
                ),
                patch.object(job.background_job, "terminate") as terminate,
                patch.object(job, "inspect_built_image") as inspect,
                self.assertRaisesRegex(job.JobError, "must be collected"),
            ):
                job.image_abort(argparse.Namespace(name="current-image"))
            terminate.assert_not_called()
            inspect.assert_not_called()
            self.assertTrue(context.is_dir())

    def test_stale_completed_and_lost_image_jobs_have_an_exact_discard_path(self) -> None:
        for process_state in ("completed", "lost"):
            with self.subTest(process_state=process_state), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                logs = root / "logs"
                images = root / "images"
                logs.mkdir()
                images.mkdir()
                name = f"discard-{process_state}"
                context = images / name
                context.mkdir()
                record = {
                    "job_id": "build-id",
                    "name": name,
                    "runner_sha256": "4" * 64,
                }
                with (
                    patch.object(job, "LOG_ROOT", logs),
                    patch.object(job, "IMAGE_BUILD_ROOT", images),
                    patch.object(job, "prepare_state"),
                    patch.object(job, "load_image_record", return_value=record),
                    patch.object(job, "runner_sha256", return_value="5" * 64),
                    patch.object(
                        job.background_job,
                        "process_state",
                        return_value={"state": process_state},
                    ),
                    patch.object(job.background_job, "terminate") as terminate,
                    patch.object(
                        job,
                        "inspect_built_image",
                        return_value=(False, "", {}),
                    ),
                ):
                    self.assertEqual(job.image_abort(argparse.Namespace(name=name)), 0)
                terminate.assert_not_called()
                self.assertFalse(context.exists())

    def test_running_image_job_is_terminated_before_discard(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            logs = root / "logs"
            images = root / "images"
            logs.mkdir()
            images.mkdir()
            context = images / "running-image"
            context.mkdir()
            record = {
                "job_id": "build-id",
                "name": "running-image",
                "runner_sha256": "4" * 64,
            }

            events: list[str] = []

            @contextmanager
            def cache_lock() -> object:
                events.append("cache-lock")
                yield object()

            def terminate(*_args: object, **_kwargs: object) -> None:
                events.append("terminate")

            with (
                patch.object(job, "LOG_ROOT", logs),
                patch.object(job, "IMAGE_BUILD_ROOT", images),
                patch.object(job, "prepare_state"),
                patch.object(job, "load_image_record", return_value=record),
                patch.object(
                    job.background_job,
                    "process_state",
                    return_value={"state": "running"},
                ),
                patch.object(
                    job.background_job,
                    "terminate",
                    side_effect=terminate,
                ) as terminate_mock,
                patch.object(job, "image_cache_lock", cache_lock),
                patch.object(
                    job,
                    "inspect_built_image",
                    side_effect=lambda *_args, **_kwargs: (
                        events.append("inspect") or (False, "", {})
                    ),
                ),
            ):
                self.assertEqual(
                    job.image_abort(argparse.Namespace(name="running-image")),
                    0,
                )
            terminate_mock.assert_called_once_with(record, require_current=False)
            self.assertEqual(events, ["terminate", "cache-lock", "inspect"])
            self.assertFalse(context.exists())

    def test_image_collection_reports_failed_when_validation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            logs = root / "logs"
            logs.mkdir()
            runtime_log = root / "runtime.log"
            runtime_log.write_bytes(b"build output\n")
            record = {
                "input_sha256": "1" * 64,
                "job_id": "build-id",
                "name": "invalid-image",
                "image": "localhost/xpra:test",
                "runner_sha256": "2" * 64,
                "source": "3" * 40,
                "workflow_sha256": "4" * 64,
            }
            with (
                patch.object(job, "LOG_ROOT", logs),
                patch.object(job, "prepare_state"),
                patch.object(job, "image_cache_lock", return_value=nullcontext()),
                patch.object(job, "load_image_record", return_value=record),
                patch.object(
                    job.background_job,
                    "process_state",
                    return_value={
                        "state": "completed",
                        "exit_code": 0,
                        "finished_at": "2026-01-01T00:00:00+00:00",
                    },
                ),
                patch.object(
                    job.background_job,
                    "runtime_log_path",
                    return_value=runtime_log,
                ),
                patch.object(job, "ensure_private_regular"),
                patch.object(
                    job,
                    "inspect_built_image",
                    return_value=(
                        True,
                        "5" * 64,
                        {
                            **job.built_image_labels(record),
                            "io.xpra.lab.unexpected": "value",
                        },
                    ),
                ),
                patch.object(job, "publish_bytes"),
                patch.object(job, "publish_status") as publish,
            ):
                self.assertEqual(
                    job.image_collect(argparse.Namespace(name="invalid-image")),
                    1,
                )
            status = publish.call_args.args[1]
            self.assertEqual(status["validation_ok"], 0)
            self.assertEqual(status["result"], "failed")


class BackgroundContainerTest(unittest.TestCase):
    def test_lifecycle_lock_reuses_a_stale_unlocked_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            logs = Path(raw)
            lock = logs / ".lifecycle.lock"
            lock.write_bytes(b"")
            lock.chmod(0o600)
            with patch.object(job, "LOG_ROOT", logs), job.lifecycle_lock("stale"):
                pass
            with patch.object(job, "LOG_ROOT", logs), job.lifecycle_lock("stale"):
                pass

    def test_lifecycle_lock_rejects_a_fifo(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            logs = Path(raw)
            os.mkfifo(logs / ".lifecycle.lock", 0o600)
            with (
                patch.object(job, "LOG_ROOT", logs),
                self.assertRaisesRegex(job.JobError, "unsafe lifecycle lock"),
                job.lifecycle_lock("unsafe"),
            ):
                pass

    def test_container_lifecycle_classifies_running_completed_and_lost(self) -> None:
        record = {"container_id": "5" * 64, "name": "lifecycle"}
        present = subprocess.CompletedProcess([], 0, "", "")
        absent = subprocess.CompletedProcess([], 1, "", "")
        for status, expected in (("running", "running"), ("exited", "completed")):
            with (
                self.subTest(status=status),
                patch.object(job, "command", return_value=present),
                patch.object(job, "container_state", return_value={"Status": status}),
            ):
                self.assertEqual(
                    job.container_lifecycle_state(record)["state"],
                    expected,
                )
        with (
            patch.object(job, "command", return_value=absent),
            patch.object(job, "container_state") as inspect,
        ):
            self.assertEqual(job.container_lifecycle_state(record)["state"], "lost")
        inspect.assert_not_called()

    def test_start_notifies_the_precreated_fifo_after_payload_delivery(self) -> None:
        args = argparse.Namespace(
            image="localhost/xpra:test",
            image_input_sha256="1" * 64,
            name="fifo-start",
            patch_mode="patched",
            selection="stacks/develop",
            source="2" * 40,
            source_head="3" * 40,
            source_remote="origin",
            target="full",
            workflow_sha256="4" * 64,
        )
        created = "5" * 64
        absent = subprocess.CompletedProcess([], 1, "", "")
        create = subprocess.CompletedProcess([], 0, created + "\n", "")
        started = subprocess.CompletedProcess([], 0, "", "")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            calls: list[list[str]] = []
            inherited: list[tuple[int, ...]] = []
            prelaunch_seen_before_create = False

            @contextmanager
            def lifecycle(_name: str):
                yield 42

            def command(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                nonlocal prelaunch_seen_before_create
                calls.append(argv)
                if argv[:2] == ["podman", "create"] or argv[:2] == ["podman", "start"]:
                    inherited.append(tuple(_kwargs.get("pass_fds", ())))
                if argv[:3] == ["podman", "container", "exists"]:
                    return absent
                if argv[:2] == ["podman", "create"]:
                    prelaunch_seen_before_create = (root / "prelaunch.json").is_file()
                    return create
                return started

            with (
                patch.object(job, "prepare_state"),
                patch.object(job, "lifecycle_lock", side_effect=lifecycle),
                patch.object(job, "image_cache_lock", return_value=nullcontext(43)),
                patch.object(job, "result_paths", return_value=(root / "log", root / "status")),
                patch.object(job, "test_record_path", return_value=root / "owner"),
                patch.object(job, "test_payload_path", return_value=root / "payload"),
                patch.object(job, "test_prelaunch_path", return_value=root / "prelaunch.json"),
                patch.object(job, "command", side_effect=command),
                patch.object(job, "image_identity", return_value="6" * 64),
                patch.object(job, "selection_digest", return_value="7" * 64),
                patch.object(job, "runner_sha256", return_value="8" * 64),
                patch.object(
                    job.background_job,
                    "process_identity",
                    return_value=("R", str(os.getpgrp()), "42"),
                ),
                patch.object(job, "prelaunch_container_id", return_value=created),
                patch.object(job, "container_state", return_value={}),
                patch.object(job, "publish_record"),
                patch.object(job, "send_test_payload") as send,
            ):
                self.assertEqual(job.test_start(args), 0)

        create_argv = next(argv for argv in calls if argv[:2] == ["podman", "create"])
        self.assertIn(job.CONTAINER_NOTIFY_FIFO, create_argv)
        self.assertTrue(prelaunch_seen_before_create)
        self.assertEqual(calls[-1], ["podman", "start", created])
        self.assertEqual(inherited, [(42, 43), (42, 43)])
        self.assertFalse(any("kill" in argv for argv in calls))
        send.assert_called_once_with(
            created,
            args,
            "7" * 64,
        )

    def test_foreground_payload_recovery_requires_its_exact_marker(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            root.chmod(0o700)
            payload = root / ".foreground-payload"
            marker = root / ".foreground-payload.owner.json"
            payload.mkdir(mode=0o700)
            (payload / "stale").write_text("stale\n", encoding="utf-8")
            marker.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "owner": job.OWNER,
                        "kind": "foreground-payload",
                        "path": str(payload),
                    }
                ),
                encoding="utf-8",
            )
            marker.chmod(0o600)
            with patch.object(job, "STATE_ROOT", root):
                job.recover_foreground_payload()
                self.assertFalse(payload.exists())
                self.assertFalse(marker.exists())
                payload.mkdir(mode=0o700)
                marker.write_text("{}\n", encoding="utf-8")
                marker.chmod(0o600)
                with self.assertRaisesRegex(job.JobError, "ownership mismatch"):
                    job.recover_foreground_payload()
            self.assertTrue(payload.is_dir())
            self.assertTrue(marker.is_file())

    def test_foreground_child_inherits_the_recovery_lock(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            root.chmod(0o700)
            read_gate, write_gate = os.pipe()
            child: subprocess.Popen[bytes] | None = None
            competitor = -1
            try:
                with (
                    patch.object(job, "STATE_ROOT", root),
                    job.foreground_payload_lock() as lock_descriptor,
                ):
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
                competitor = os.open(root / ".foreground-payload.lock", os.O_RDWR)
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

    def test_abort_cannot_remove_payload_while_snapshot_child_holds_start_lock(self) -> None:
        name = "selection-freeze"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            root.chmod(0o700)
            payload = root / "payload"
            payload.mkdir(mode=0o700)
            owner = root / "owner"
            owner.write_text("owned\n", encoding="utf-8")
            read_gate, write_gate = os.pipe()
            child: subprocess.Popen[bytes] | None = None
            try:
                with (
                    patch.object(job, "LOG_ROOT", root),
                    job.lifecycle_lock(name) as lock_descriptor,
                ):
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
                with (
                    patch.object(job, "LOG_ROOT", root),
                    patch.object(job, "prepare_state"),
                    patch.object(job, "test_payload_path", return_value=payload),
                    patch.object(job, "test_record_path", return_value=owner),
                    self.assertRaisesRegex(job.JobError, "already active"),
                ):
                    job.test_abort(argparse.Namespace(name=name))
                self.assertTrue(payload.is_dir())
                self.assertTrue(owner.is_file())
                os.write(write_gate, b"x")
                os.close(write_gate)
                write_gate = -1
                child.wait(timeout=5)
            finally:
                if read_gate >= 0:
                    os.close(read_gate)
                if write_gate >= 0:
                    os.close(write_gate)
                if child is not None and child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)

    def test_payload_receiver_notifies_the_same_fifo(self) -> None:
        args = argparse.Namespace(
            lifecycle_lock_descriptor=42,
            name="payload-receiver",
            selection="stacks/develop",
            source_head="3" * 40,
            source_remote="origin",
        )

        @contextmanager
        def payload(**_kwargs: object):
            yield (), 42

        with (
            patch.object(job, "test_payload", side_effect=payload),
            patch.object(job.container_payload, "stream_to_process") as stream,
        ):
            job.send_test_payload(
                "a" * 64,
                args,
                "7" * 64,
            )
        argv = stream.call_args.args[0]
        self.assertEqual(argv[argv.index("--notify-fifo") + 1], job.CONTAINER_NOTIFY_FIFO)
        self.assertEqual(stream.call_args.kwargs["pass_fds"], (42,))

    def test_failed_payload_keeps_owner_record_when_container_removal_fails(self) -> None:
        args = argparse.Namespace(
            image="localhost/xpra:test",
            image_input_sha256="1" * 64,
            name="payload-failure",
            patch_mode="patched",
            selection="stacks/develop",
            source="2" * 40,
            source_head="3" * 40,
            source_remote="origin",
            target="full",
            workflow_sha256="4" * 64,
        )
        container_id = "5" * 64
        absent = subprocess.CompletedProcess([], 1, "", "")
        created = subprocess.CompletedProcess([], 0, container_id + "\n", "")
        succeeded = subprocess.CompletedProcess([], 0, "", "")
        remove_failed = subprocess.CompletedProcess([], 1, "", "remove failed")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            owner = root / "owner"
            prelaunch = root / "prelaunch.json"

            @contextmanager
            def lifecycle(_name: str):
                yield 42

            def command(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                if argv[:3] == ["podman", "container", "exists"]:
                    return absent
                if argv[:2] == ["podman", "create"]:
                    return created
                if argv[:3] == ["podman", "rm", "--force"]:
                    return remove_failed
                return succeeded

            def publish(path: Path, _record: dict[str, object]) -> None:
                path.write_text("owned\n", encoding="utf-8")

            with (
                patch.object(job, "prepare_state"),
                patch.object(job, "lifecycle_lock", side_effect=lifecycle),
                patch.object(job, "image_cache_lock", return_value=nullcontext(43)),
                patch.object(job, "result_paths", return_value=(root / "log", root / "status")),
                patch.object(job, "test_record_path", return_value=owner),
                patch.object(job, "test_payload_path", return_value=root / "payload"),
                patch.object(job, "test_prelaunch_path", return_value=prelaunch),
                patch.object(job, "command", side_effect=command),
                patch.object(job, "image_identity", return_value="6" * 64),
                patch.object(job, "selection_digest", return_value="7" * 64),
                patch.object(job, "runner_sha256", return_value="8" * 64),
                patch.object(
                    job.background_job,
                    "process_identity",
                    return_value=("R", str(os.getpgrp()), "42"),
                ),
                patch.object(job, "prelaunch_container_id", return_value=container_id),
                patch.object(job, "container_state", return_value={}),
                patch.object(job, "publish_record", side_effect=publish),
                patch.object(
                    job,
                    "send_test_payload",
                    side_effect=job.container_payload.PayloadError("payload failed"),
                ),
                self.assertRaisesRegex(job.container_payload.PayloadError, "payload failed"),
            ):
                job.test_start(args)

            self.assertTrue(owner.is_file())

    def test_abort_recovers_an_inactive_prelaunch_container_by_immutable_id(self) -> None:
        name = "interrupted-start"
        container_id = "5" * 64
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            owner = root / "owner"
            marker = root / "prelaunch.json"
            payload = root / "payload"
            payload.mkdir(mode=0o700)
            (payload / "partial").write_text("partial\n", encoding="utf-8")
            record = {
                "image": "localhost/xpra:test",
                "image_id": "6" * 64,
                "kind": "test-prelaunch",
                "labels": {
                    "io.xpra.lab.image-id": "6" * 64,
                    "io.xpra.lab.owner": job.OWNER,
                    "io.xpra.lab.run-id": "12345678-1234-4abc-8def-123456789abc",
                    "io.xpra.lab.upstream-test": "true",
                },
                "name": name,
                "owner": job.OWNER,
                "payload_path": str(payload),
                "process": {"pid": 12345, "start_ticks": "42"},
                "run_id": "12345678-1234-4abc-8def-123456789abc",
                "runner_sha256": "7" * 64,
                "schema": 1,
            }
            marker.write_text("{}\n", encoding="utf-8")
            marker.chmod(0o600)
            with (
                patch.object(job, "LOG_ROOT", root),
                patch.object(job, "prepare_state"),
                patch.object(job, "result_paths", return_value=(root / "log", root / "status")),
                patch.object(job, "test_record_path", return_value=owner),
                patch.object(job, "test_prelaunch_path", return_value=marker),
                patch.object(job, "test_payload_path", return_value=payload),
                patch.object(job, "load_test_prelaunch", return_value=record),
                patch.object(job, "test_prelaunch_active", return_value=False),
                patch.object(job, "prelaunch_container_id", return_value=container_id),
                patch.object(
                    job,
                    "command",
                    return_value=subprocess.CompletedProcess([], 0, "", ""),
                ) as command,
            ):
                self.assertEqual(job.test_abort(argparse.Namespace(name=name)), 0)
            command.assert_called_once_with(["podman", "rm", "--force", container_id])
            self.assertFalse(marker.exists())
            self.assertFalse(payload.exists())

    def test_prelaunch_recovery_rejects_a_replacement_container(self) -> None:
        name = "replacement"
        record = {
            "image_id": "6" * 64,
            "labels": {
                "io.xpra.lab.image-id": "6" * 64,
                "io.xpra.lab.owner": job.OWNER,
                "io.xpra.lab.run-id": "12345678-1234-4abc-8def-123456789abc",
                "io.xpra.lab.upstream-test": "true",
            },
            "name": name,
        }
        item = {
            "Config": {"Labels": {"io.xpra.lab.owner": "someone-else"}},
            "Id": "5" * 64,
            "Image": "sha256:" + "6" * 64,
            "Name": "/" + name,
        }
        with (
            patch.object(
                job,
                "command",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ),
            patch.object(job, "inspect_json", return_value=item),
            self.assertRaisesRegex(job.JobError, "does not match prelaunch ownership"),
        ):
            job.prelaunch_container_id(record)

    def test_prelaunch_accepts_only_the_owned_image_labels_inherited_by_podman(
        self,
    ) -> None:
        name = "inherited-image-labels"
        expected = {
            "io.xpra.lab.image-id": "6" * 64,
            "io.xpra.lab.image-input": "7" * 64,
            "io.xpra.lab.owner": job.OWNER,
            "io.xpra.lab.run-id": "12345678-1234-4abc-8def-123456789abc",
            "io.xpra.lab.source": "8" * 40,
            "io.xpra.lab.upstream-test": "true",
            "io.xpra.lab.workflow": "9" * 64,
        }
        record = {
            "image_id": "6" * 64,
            "labels": expected,
            "name": name,
        }
        inherited = {
            "io.xpra.lab.image-builder": "true",
            "io.xpra.lab.image-build-run-id": "87654321-4321-4abc-8def-123456789abc",
        }
        item = {
            "Config": {
                "Labels": {
                    **expected,
                    **inherited,
                    "org.opencontainers.image.title": "ubuntu",
                }
            },
            "Id": "5" * 64,
            "Image": "sha256:" + "6" * 64,
            "Name": "/" + name,
        }
        image = {
            "Id": "sha256:" + "6" * 64,
            "Labels": {
                **inherited,
                "io.xpra.lab.image-input": expected["io.xpra.lab.image-input"],
                "io.xpra.lab.source": expected["io.xpra.lab.source"],
                "io.xpra.lab.workflow": expected["io.xpra.lab.workflow"],
                "org.opencontainers.image.title": "ubuntu",
            },
        }
        present = subprocess.CompletedProcess([], 0, "", "")
        with (
            patch.object(job, "command", return_value=present),
            patch.object(job, "inspect_json", side_effect=(item, image, item)),
        ):
            self.assertEqual(job.prelaunch_container_id(record), "5" * 64)
            item["Config"]["Labels"]["io.xpra.lab.unexpected"] = "value"
            with self.assertRaisesRegex(
                job.JobError, "does not match prelaunch ownership"
            ):
                job.prelaunch_container_id(record)
        item["Config"]["Labels"].pop("io.xpra.lab.unexpected")
        image["Labels"]["io.xpra.lab.image-build-run-id"] = (
            "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        )
        with (
            patch.object(job, "command", return_value=present),
            patch.object(job, "inspect_json", side_effect=(item, image)),
            self.assertRaisesRegex(job.JobError, "does not match prelaunch ownership"),
        ):
            job.prelaunch_container_id(record)

    def test_completed_current_test_job_must_be_collected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            owner = root / "current.owner"
            owner.write_text("owned\n", encoding="utf-8")
            current = "4" * 64
            record = {
                "container_id": "5" * 64,
                "name": "current-test",
                "runner_sha256": current,
            }
            with (
                patch.object(job, "LOG_ROOT", root),
                patch.object(job, "prepare_state"),
                patch.object(job, "load_test_record", return_value=record),
                patch.object(job, "test_record_path", return_value=owner),
                patch.object(job, "runner_sha256", return_value=current),
                patch.object(
                    job,
                    "container_lifecycle_state",
                    return_value={"state": "completed", "container_status": "exited"},
                ),
                patch.object(job, "command") as command,
                self.assertRaisesRegex(job.JobError, "must be collected"),
            ):
                job.test_abort(argparse.Namespace(name="current-test"))
            command.assert_not_called()
            self.assertTrue(owner.is_file())

    def test_stale_completed_and_lost_test_jobs_have_an_exact_discard_path(self) -> None:
        for lifecycle_state in ("completed", "lost"):
            with self.subTest(lifecycle_state=lifecycle_state), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                name = f"discard-{lifecycle_state}"
                owner = root / f"{name}.owner"
                owner.write_text("owned\n", encoding="utf-8")
                record = {
                    "container_id": "5" * 64,
                    "name": name,
                    "runner_sha256": "4" * 64,
                }
                with (
                    patch.object(job, "LOG_ROOT", root),
                    patch.object(job, "prepare_state"),
                    patch.object(job, "load_test_record", return_value=record),
                    patch.object(job, "test_record_path", return_value=owner),
                    patch.object(job, "runner_sha256", return_value="6" * 64),
                    patch.object(
                        job,
                        "container_lifecycle_state",
                        return_value={
                            "state": lifecycle_state,
                            "container_status": "exited" if lifecycle_state == "completed" else "",
                        },
                    ),
                    patch.object(job, "command") as command,
                ):
                    self.assertEqual(job.test_abort(argparse.Namespace(name=name)), 0)
                if lifecycle_state == "completed":
                    command.assert_called_once_with(
                        ["podman", "rm", "--force", record["container_id"]]
                    )
                else:
                    command.assert_not_called()
                self.assertFalse(owner.exists())

    def test_running_test_job_is_force_removed_before_discard(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            owner = root / "running.owner"
            owner.write_text("owned\n", encoding="utf-8")
            record = {
                "container_id": "5" * 64,
                "name": "running-test",
                "runner_sha256": "4" * 64,
            }
            with (
                patch.object(job, "LOG_ROOT", root),
                patch.object(job, "prepare_state"),
                patch.object(job, "load_test_record", return_value=record),
                patch.object(job, "test_record_path", return_value=owner),
                patch.object(
                    job,
                    "container_lifecycle_state",
                    return_value={"state": "running", "container_status": "running"},
                ),
                patch.object(job, "command") as command,
            ):
                self.assertEqual(job.test_abort(argparse.Namespace(name="running-test")), 0)
            command.assert_called_once_with(
                ["podman", "rm", "--force", record["container_id"]]
            )
            self.assertFalse(owner.exists())

    def test_remove_accepts_an_exact_collected_container_that_was_pruned(self) -> None:
        name = "collected-pruned"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            owner = root / f"{name}.owner"
            owner.write_text("owned\n", encoding="utf-8")
            owner.chmod(0o600)
            record = {"container_id": "5" * 64, "name": name}
            with (
                patch.object(job, "prepare_state"),
                patch.object(job, "lifecycle_lock", return_value=nullcontext()),
                patch.object(job, "load_test_record", return_value=record),
                patch.object(job, "test_record_path", return_value=owner),
                patch.object(
                    job,
                    "publish_remove_transaction",
                    return_value={"owner_sha256": job.sha256_file(owner)},
                ),
                patch.object(job, "matching_test_prelaunch", return_value=None),
                patch.object(job, "verify_test_evidence"),
                patch.object(
                    job,
                    "container_lifecycle_state",
                    return_value={"state": "lost", "container_status": ""},
                ),
                patch.object(job, "remove_test_payload"),
                patch.object(job, "remove_test_prelaunch"),
                patch.object(job, "command") as command,
            ):
                self.assertEqual(job.test_remove(argparse.Namespace(name=name)), 0)
            command.assert_not_called()
            self.assertFalse(owner.exists())

    def test_test_remove_retries_after_a_crash_with_its_retained_transaction(self) -> None:
        name = "remove-crash-test"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            logs = root / "logs"
            runs = root / "runs"
            logs.mkdir(mode=0o700)
            runs.mkdir(mode=0o700)
            owner = runs / f"{name}.owner"
            owner.write_text("owned\n", encoding="utf-8")
            owner.chmod(0o600)
            for path in (logs / f"{name}.log", logs / f"{name}.status"):
                path.write_text("evidence\n", encoding="utf-8")
                path.chmod(0o600)
            record = {
                "container_id": "5" * 64,
                "name": name,
                "owner": job.OWNER,
                "schema": "4",
            }
            remove_payload = Mock(side_effect=[RuntimeError("simulated crash"), None])
            with (
                patch.object(job, "LOG_ROOT", logs),
                patch.object(job, "RUN_ROOT", runs),
                patch.object(job, "prepare_state"),
                patch.object(job, "load_test_record", return_value=record),
                patch.object(job, "matching_test_prelaunch", return_value=None),
                patch.object(job, "verify_test_evidence"),
                patch.object(
                    job,
                    "container_lifecycle_state",
                    return_value={"state": "lost", "container_status": ""},
                ),
                patch.object(job, "remove_test_payload", remove_payload),
                patch.object(job, "remove_test_prelaunch"),
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                    job.test_remove(argparse.Namespace(name=name))
                self.assertTrue(job.remove_transaction_path(name).is_file())
                self.assertTrue(owner.is_file())
                self.assertEqual(job.test_remove(argparse.Namespace(name=name)), 0)
            self.assertFalse(owner.exists())
            self.assertTrue((logs / f"{name}.remove.json").is_file())

    def test_image_remove_recovers_a_partially_deleted_context(self) -> None:
        name = "remove-crash-image"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            logs = root / "logs"
            images = root / "images"
            logs.mkdir(mode=0o700)
            images.mkdir(mode=0o700)
            context = images / name
            context.mkdir(mode=0o700)
            owner = context / "owner.json"
            owner.write_text("{}\n", encoding="utf-8")
            owner.chmod(0o600)
            for path in (logs / f"{name}.log", logs / f"{name}.status"):
                path.write_text("evidence\n", encoding="utf-8")
                path.chmod(0o600)
            record = {
                "kind": "image-build",
                "name": name,
                "owner": job.IMAGE_OWNER,
                "schema": 3,
            }

            def interrupt_rmtree(path: Path) -> None:
                self.assertEqual(Path(path), context)
                owner.unlink()
                raise RuntimeError("simulated crash")

            with (
                patch.object(job, "LOG_ROOT", logs),
                patch.object(job, "IMAGE_BUILD_ROOT", images),
                patch.object(job, "prepare_state"),
                patch.object(job, "load_image_record", return_value=record),
                patch.object(job, "verify_image_evidence"),
                patch.object(
                    job.background_job,
                    "process_state",
                    return_value={"state": "completed"},
                ),
                patch.object(job.shutil, "rmtree", side_effect=interrupt_rmtree),
                self.assertRaisesRegex(RuntimeError, "simulated crash"),
            ):
                job.image_remove(argparse.Namespace(name=name))
            self.assertTrue((logs / f"{name}.remove.json").is_file())
            self.assertTrue(context.is_dir())
            self.assertFalse(owner.exists())
            with (
                patch.object(job, "LOG_ROOT", logs),
                patch.object(job, "IMAGE_BUILD_ROOT", images),
                patch.object(job, "prepare_state"),
                patch.object(job, "verify_image_evidence"),
            ):
                self.assertEqual(job.image_remove(argparse.Namespace(name=name)), 0)
            self.assertFalse(context.exists())
            self.assertTrue((logs / f"{name}.remove.json").is_file())

    def test_test_collection_reports_failed_when_validation_fails(self) -> None:
        record = {
            "container_id": "5" * 64,
            "image": "localhost/xpra:test",
            "image_id": "6" * 64,
            "image_input_sha256": "7" * 64,
            "name": "invalid-test",
            "patch_mode": "patched",
            "payload_path": "/tmp/invalid-test.payload",
            "run_id": "run-id",
            "runner_sha256": "8" * 64,
            "selection": "stacks/develop",
            "selection_sha256": "9" * 64,
            "source": "a" * 40,
            "source_head": "b" * 40,
            "source_remote": "origin",
            "target": "full",
            "workflow_sha256": "c" * 64,
        }
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with (
                patch.object(job, "LOG_ROOT", root),
                patch.object(job, "prepare_state"),
                patch.object(job, "load_test_record", return_value=record),
                patch.object(
                    job,
                    "container_state",
                    return_value={
                        "Status": "exited",
                        "ExitCode": 0,
                        "FinishedAt": "2026-01-01T00:00:00+00:00",
                    },
                ),
                patch.object(
                    job,
                    "command",
                    return_value=subprocess.CompletedProcess([], 0, "ordinary log\n", ""),
                ),
                patch.object(job, "publish_bytes"),
                patch.object(job, "publish_status") as publish,
            ):
                self.assertEqual(
                    job.test_collect(argparse.Namespace(name="invalid-test")),
                    1,
                )
            status = publish.call_args.args[1]
            self.assertEqual(status["validation_ok"], 0)
            self.assertEqual(status["result"], "failed")


class MainTest(unittest.TestCase):
    def test_unowned_diagnostic_shell_interface_is_absent(self) -> None:
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            job.parser().parse_args(["test", "shell"])
        makefile = (job.RUNNER_ROOT / "Makefile").read_text(encoding="utf-8")
        entrypoint = (job.RUNNER_ROOT / "entrypoint.sh").read_text(encoding="utf-8")
        self.assertNotIn("\nshell:", makefile)
        self.assertNotIn("\n    shell)", entrypoint)

    def test_transport_error_is_reported_without_a_traceback(self) -> None:
        arguments = argparse.Namespace(
            handler=lambda _args: (_ for _ in ()).throw(
                job.container_payload.PayloadError("transport failed")
            )
        )
        parser = unittest.mock.Mock()
        parser.parse_args.return_value = arguments
        stderr = StringIO()
        with (
            patch.object(job, "parser", return_value=parser),
            patch.object(job.os, "umask"),
            redirect_stderr(stderr),
        ):
            self.assertEqual(job.main(), 2)
        self.assertEqual(stderr.getvalue(), "error: transport failed\n")

    def test_collected_result_must_match_full_validation(self) -> None:
        with self.assertRaisesRegex(job.JobError, "contradicts"):
            job.verify_result_status(
                {"validation_ok": "0", "result": "success"},
                "test",
            )


if __name__ == "__main__":
    unittest.main()
