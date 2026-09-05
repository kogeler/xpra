# Copyright (C) 2026 kogeler

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager, nullcontext, redirect_stderr
from io import StringIO
from pathlib import Path
from typing import ClassVar
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
        recipe = makefile.split("image-remove: source-check inputs-check", 1)[1].split(
            "\nimage-background-name-check:", 1
        )[0]
        self.assertIn('"$(JOB)" image cache-remove', recipe)
        self.assertNotIn("$(PODMAN) image rm", recipe)

    def test_make_image_check_uses_the_exact_job_helper(self) -> None:
        makefile = (job.RUNNER_ROOT / "Makefile").read_text(encoding="utf-8")
        recipe = makefile.split("image image-check: source-check inputs-check", 1)[1].split(
            "\nimage-remove:", 1
        )[0]
        self.assertIn('"$(JOB)" image check', recipe)
        self.assertIn('--source "$(BASE_COMMIT)"', recipe)
        self.assertNotIn("$(PODMAN) image inspect", recipe)
        self.assertNotIn("RESOLVE_IMAGE", makefile)

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

    def test_check_uses_the_exact_current_source_under_the_cache_lock(self) -> None:
        args = image_args()
        cache_lock = Mock()
        cache_lock.__enter__ = Mock(return_value=43)
        cache_lock.__exit__ = Mock(return_value=False)
        with (
            patch.object(job, "prepare_state") as prepare,
            patch.object(job, "image_cache_lock", return_value=cache_lock),
            patch.object(
                job,
                "command",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ) as command,
            patch.object(job, "image_identity", return_value="4" * 64) as identity,
        ):
            self.assertEqual(job.image_check(args), 0)

        prepare.assert_called_once_with()
        cache_lock.__enter__.assert_called_once_with()
        cache_lock.__exit__.assert_called_once()
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

    def test_check_distinguishes_a_missing_image_from_an_inspection_error(self) -> None:
        args = image_args()
        for returncode, message in (
            (1, "required image is missing"),
            (2, "cannot inspect image name"),
        ):
            with (
                self.subTest(returncode=returncode),
                patch.object(job, "prepare_state"),
                patch.object(job, "image_cache_lock", return_value=nullcontext()),
                patch.object(
                    job,
                    "command",
                    return_value=subprocess.CompletedProcess([], returncode, "", ""),
                ),
                patch.object(job, "image_identity") as identity,
                self.assertRaisesRegex(job.JobError, message),
            ):
                job.image_check(args)
            identity.assert_not_called()

    def test_image_identity_requires_complete_exact_maintenance_provenance(self) -> None:
        args = image_args()
        build_run = "12345678-1234-4abc-8def-123456789abc"
        labels = {
            "io.xpra.fork-maintenance.image-builder": "true",
            "io.xpra.fork-maintenance.image-build-run-id": build_run,
            "io.xpra.fork-maintenance.image-input": args.image_input_sha256,
            "io.xpra.fork-maintenance.source": args.source,
            "io.xpra.fork-maintenance.workflow": args.workflow_sha256,
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
                ("io.xpra.fork-maintenance.source", "5" * 40),
                ("io.xpra.fork-maintenance.unexpected", "value"),
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
                "io.xpra.fork-maintenance.source": "5" * 40,
            }
            with patch.object(job, "require_removable_image_source") as source_check:
                self.assertEqual(
                    job.removable_image_identity(
                        args.image,
                        args.image_input_sha256,
                        args.workflow_sha256,
                        current_source=args.source,
                    ),
                    "4" * 64,
                )
            source_check.assert_called_once_with("5" * 40, args.source)

    def test_removable_source_must_exist_and_ancestor_or_equal_current(self) -> None:
        cached = "2" * 40
        current = "3" * 40

        def accepted(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.assertEqual(kwargs, {"cwd": job.PROJECT_ROOT, "check": False})
            self.assertIn(argv[:2], (["git", "cat-file"], ["git", "merge-base"]))
            return subprocess.CompletedProcess(argv, 0, "", "")

        with patch.object(job, "command", side_effect=accepted) as command:
            job.require_removable_image_source(cached, current)
        self.assertEqual(
            command.call_args_list,
            [
                call(
                    ["git", "cat-file", "-e", f"{cached}^{{commit}}"],
                    cwd=job.PROJECT_ROOT,
                    check=False,
                ),
                call(
                    ["git", "cat-file", "-e", f"{current}^{{commit}}"],
                    cwd=job.PROJECT_ROOT,
                    check=False,
                ),
                call(
                    ["git", "merge-base", "--is-ancestor", cached, current],
                    cwd=job.PROJECT_ROOT,
                    check=False,
                ),
            ],
        )

        with patch.object(job, "command", side_effect=accepted):
            job.require_removable_image_source(current, current)

    def test_removable_source_rejects_unknown_unrelated_and_future_commits(self) -> None:
        cached = "2" * 40
        current = "3" * 40
        success = subprocess.CompletedProcess([], 0, "", "")
        missing = subprocess.CompletedProcess([], 128, "", "")
        not_ancestor = subprocess.CompletedProcess([], 1, "", "")

        with (
            patch.object(job, "command", side_effect=(missing,)),
            self.assertRaisesRegex(job.JobError, "cached removal source.*existing commit"),
        ):
            job.require_removable_image_source(cached, current)

        for label in ("unrelated", "future"):
            with (
                self.subTest(label=label),
                patch.object(job, "command", side_effect=(success, success, not_ancestor)),
                self.assertRaisesRegex(job.JobError, "not an ancestor"),
            ):
                job.require_removable_image_source(cached, current)

    def test_cache_remove_uses_current_source_and_removes_the_exact_image_id(self) -> None:
        args = image_args()
        image_id = "4" * 64
        cache_lock = Mock()
        cache_lock.__enter__ = Mock(return_value=43)
        cache_lock.__exit__ = Mock(return_value=False)
        with (
            patch.object(job, "prepare_state") as prepare,
            patch.object(job, "image_cache_lock", return_value=cache_lock),
            patch.object(job, "removable_image_identity", return_value=image_id) as identity,
            patch.object(job, "require_image_cache_unleased") as unleased,
            patch.object(
                job,
                "command",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ) as command,
        ):
            self.assertEqual(job.image_cache_remove(args), 0)

        prepare.assert_called_once_with()
        identity.assert_called_once_with(
            args.image,
            args.image_input_sha256,
            args.workflow_sha256,
            current_source=args.source,
        )
        unleased.assert_called_once_with(args.image, image_id)
        command.assert_called_once_with(["podman", "image", "rm", image_id])
        cache_lock.__enter__.assert_called_once_with()
        cache_lock.__exit__.assert_called_once()

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
                            "io.xpra.fork-maintenance.unexpected": "value",
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
    def test_runtime_uses_the_bounded_upstream_user_namespace(self) -> None:
        args = argparse.Namespace(
            patch_mode="patched",
            selection="stacks/develop",
            source="2" * 40,
            source_head="3" * 40,
            source_remote="origin",
            workflow_sha256="4" * 64,
        )
        self.assertEqual(
            job.test_runtime_options(args, "5" * 64)[:4],
            [
                "--userns",
                "keep-id:uid=1000,gid=1000,size=2048",
                "--user",
                "1000:1000",
            ],
        )

    def test_command_rejects_an_unbounded_user_namespace(self) -> None:
        with self.assertRaisesRegex(job.JobError, "explicit size"):
            job.command(
                ["podman", "create", "--userns=keep-id:uid=1000,gid=1000", "image"]
            )

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
        # The starter keeps the cache lock through immutable-ID handoff, but
        # Podman's long-lived networking helper must not inherit and lease it.
        self.assertEqual(inherited, [(42,), (42,)])
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
                    "io.xpra.fork-maintenance.image-id": "6" * 64,
                    "io.xpra.fork-maintenance.owner": job.OWNER,
                    "io.xpra.fork-maintenance.run-id": "12345678-1234-4abc-8def-123456789abc",
                    "io.xpra.fork-maintenance.upstream-test": "true",
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
                "io.xpra.fork-maintenance.image-id": "6" * 64,
                "io.xpra.fork-maintenance.owner": job.OWNER,
                "io.xpra.fork-maintenance.run-id": "12345678-1234-4abc-8def-123456789abc",
                "io.xpra.fork-maintenance.upstream-test": "true",
            },
            "name": name,
        }
        item = {
            "Config": {"Labels": {"io.xpra.fork-maintenance.owner": "someone-else"}},
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
            "io.xpra.fork-maintenance.image-id": "6" * 64,
            "io.xpra.fork-maintenance.image-input": "7" * 64,
            "io.xpra.fork-maintenance.owner": job.OWNER,
            "io.xpra.fork-maintenance.run-id": "12345678-1234-4abc-8def-123456789abc",
            "io.xpra.fork-maintenance.source": "8" * 40,
            "io.xpra.fork-maintenance.upstream-test": "true",
            "io.xpra.fork-maintenance.workflow": "9" * 64,
        }
        record = {
            "image_id": "6" * 64,
            "labels": expected,
            "name": name,
        }
        inherited = {
            "io.xpra.fork-maintenance.image-builder": "true",
            "io.xpra.fork-maintenance.image-build-run-id": "87654321-4321-4abc-8def-123456789abc",
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
                "io.xpra.fork-maintenance.image-input": expected["io.xpra.fork-maintenance.image-input"],
                "io.xpra.fork-maintenance.source": expected["io.xpra.fork-maintenance.source"],
                "io.xpra.fork-maintenance.workflow": expected["io.xpra.fork-maintenance.workflow"],
                "org.opencontainers.image.title": "ubuntu",
            },
        }
        present = subprocess.CompletedProcess([], 0, "", "")
        with (
            patch.object(job, "command", return_value=present),
            patch.object(job, "inspect_json", side_effect=(item, image, item)),
        ):
            self.assertEqual(job.prelaunch_container_id(record), "5" * 64)
            item["Config"]["Labels"]["io.xpra.fork-maintenance.unexpected"] = "value"
            with self.assertRaisesRegex(
                job.JobError, "does not match prelaunch ownership"
            ):
                job.prelaunch_container_id(record)
        item["Config"]["Labels"].pop("io.xpra.fork-maintenance.unexpected")
        image["Labels"]["io.xpra.fork-maintenance.image-build-run-id"] = (
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


class FocusedEntrypointTest(unittest.TestCase):
    MODES: ClassVar[dict[str, tuple[str, str]]] = {
        "focused": ("without", "1"),
        "focused-cython": ("with", "1"),
        "focused-no-compat": ("without", "0"),
    }
    MODULES = ("unit.server.first_test", "unit.client.second_test")

    def run_focused(
        self,
        target: str,
        *,
        patch_mode: str = "patched",
        modules: tuple[str, ...] = MODULES,
        missing: str = "",
        selector_status: int = 0,
        gates: tuple[str, ...] = (),
        gate_status: int = 0,
        install_status: int = 0,
        native_status: int = 0,
        tree_output: str = "a" * 40,
        tree_status: int = 0,
    ) -> subprocess.CompletedProcess[str]:
        entrypoint = (job.RUNNER_ROOT / "entrypoint.sh").read_text(encoding="utf-8")

        def function(name: str, following: str) -> str:
            marker = f"{name}() {{\n"
            return marker + entrypoint.split(marker, 1)[1].split(
                f"\n{following}() {{", 1
            )[0] + "\n"

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for module in modules:
                if module == missing:
                    continue
                path = root / "tests/unittests" / (module.replace(".", "/") + ".py")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            (root / "setup.py").write_text(
                "import json, os, sys\n"
                "print('focused_invocation=' + json.dumps({\n"
                "    'arguments': sys.argv[1:],\n"
                "    'cythonize': os.environ.get('CYTHONIZE_MORE'),\n"
                "    'compat': os.environ.get('XPRA_BACKWARDS_COMPATIBLE'),\n"
                "    'cflags': os.environ.get('CFLAGS'),\n"
                "    'cxxflags': os.environ.get('CXXFLAGS'),\n"
                "    'extra_args': os.environ.get('EXTRA_ARGS'),\n"
                "}))\n"
                f"sys.exit({install_status})\n",
                encoding="utf-8",
            )
            harness = (
                "set -euo pipefail\n"
                f"WORK={shlex.quote(str(root))}\n"
                f"PATCH_MODE={shlex.quote(patch_mode)}\n"
                "SELECTION=cases/focused-subject\n"
                # Ambient values must never choose the named mode.
                "export CYTHONIZE_MORE=ambient XPRA_BACKWARDS_COMPATIBLE=ambient\n"
                "prepare_source() { printf 'prepared_source\\n'; }\n"
                "git() {\n"
                "    test \"$*\" = \"-C $WORK write-tree\" || return 91\n"
                f"    printf '%s' {shlex.quote(tree_output)}\n"
                f"    return {tree_status}\n"
                "}\n"
                "selection_tool() {\n"
                "    case \"$1\" in\n"
                f"        unit-tests) printf '%s' {shlex.quote(chr(10).join(modules))}; "
                f"return {selector_status} ;;\n"
                f"        gates) printf '%s' {shlex.quote(chr(10).join(gates))}; "
                f"return {gate_status} ;;\n"
                "        *) return 99 ;;\n"
                "    esac\n"
                "}\n"
                "check_focused_native_modules() { "
                "printf 'checked_native=%s:%s\\n' \"$CYTHONIZE_MORE\" \"$XPRA_BACKWARDS_COMPATIBLE\"; "
                f"return {native_status}; }}\n"
                + function("selected_focused_tests", "selected_gate_names")
                + function("selected_gate_names", "validate_inputs")
                + function("run_focused", "run_wayland")
                + '\ncase "${1:-help}" in\n'
                + entrypoint.rsplit('\ncase "${1:-help}" in\n', 1)[1]
            )
            return subprocess.run(
                ("bash", "-s", "--", target),
                input=harness,
                capture_output=True,
                text=True,
                check=False,
            )

    def test_named_modes_pin_environment_and_ordered_modules(self) -> None:
        for target, (cythonize, compat) in self.MODES.items():
            for patch_mode in ("patched", "tests-only"):
                with self.subTest(target=target, patch_mode=patch_mode):
                    result = self.run_focused(target, patch_mode=patch_mode)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    calls = [
                        json.loads(line.split("=", 1)[1])
                        for line in result.stdout.splitlines()
                        if line.startswith("focused_invocation=")
                    ]
                    self.assertEqual(calls, [{
                        "arguments": [
                            "unittests", "unit/server/first_test.py", "unit/client/second_test.py",
                        ],
                        "cythonize": cythonize,
                        "compat": compat,
                        "cflags": "-O0 -g0",
                        "cxxflags": "-O0 -g0",
                        "extra_args": "--with-terminal_client",
                    }])
                    for marker in (
                        f"focused_mode={target}",
                        f"focused_cythonize_more={cythonize}",
                        f"focused_backwards_compatible={compat}",
                        "focused_applied_tree=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    ):
                        self.assertEqual(result.stdout.splitlines().count(marker), 1)
                    self.assertEqual(
                        [line for line in result.stdout.splitlines() if line.startswith("focused_unit_test=")],
                        [f"focused_unit_test={module}" for module in self.MODULES],
                    )
                    self.assertEqual(
                        result.stdout.splitlines().count(f"checked_native={cythonize}:{compat}"), 1,
                    )

    def test_each_mode_rejects_empty_missing_or_failed_module_inventory(self) -> None:
        faults = (
            ({"modules": ()}, "selection has no focused unit tests"),
            ({"missing": self.MODULES[1]}, "selected unit test is missing after patching"),
            ({"selector_status": 23}, "cannot read focused unit tests"),
            ({"gate_status": 23}, "cannot read gates"),
            ({"modules": ("unit.test_util",)}, "selected unit module is not an executable test"),
        )
        for target in self.MODES:
            for options, message in faults:
                with self.subTest(target=target, options=options):
                    result = self.run_focused(target, **options)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn(message, result.stderr)
                    self.assertNotIn("focused_invocation=", result.stdout)
                    self.assertNotIn("checked_native", result.stdout)

    def test_each_mode_requires_tests_only_or_patched_source(self) -> None:
        for target in self.MODES:
            with self.subTest(target=target):
                result = self.run_focused(target, patch_mode="clean")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("focused regressions require", result.stderr)
                self.assertNotIn("prepared_source", result.stdout)

    def test_applied_tree_failure_cannot_publish_a_focused_plan_or_run_tests(self) -> None:
        for options, message in (
            ({"tree_status": 23}, "cannot determine focused applied source tree"),
            ({"tree_output": ""}, "invalid focused applied source tree"),
            ({"tree_output": "a" * 39}, "invalid focused applied source tree"),
        ):
            with self.subTest(options=options):
                result = self.run_focused("focused", **options)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(message, result.stderr)
                self.assertNotIn("focused_mode=", result.stdout)
                self.assertNotIn("focused_invocation=", result.stdout)

    def test_each_mode_preserves_native_build_requirements_and_failure(self) -> None:
        for target in self.MODES:
            with self.subTest(target=target):
                result = self.run_focused(target, gates=("wayland", "libyuv"), native_status=19)
                self.assertEqual(result.returncode, 19, result.stderr)
                invocation = next(
                    json.loads(line.split("=", 1)[1]) for line in result.stdout.splitlines()
                    if line.startswith("focused_invocation=")
                )
                self.assertEqual(
                    invocation["extra_args"],
                    "--with-terminal_client --with-csc_libyuv --with-argb "
                    "--with-keyboard --with-wayland_server --with-clipboard --with-dmabuf",
                )

    def test_failed_build_does_not_reach_native_success(self) -> None:
        for target in self.MODES:
            with self.subTest(target=target):
                result = self.run_focused(target, install_status=17)
                self.assertEqual(result.returncode, 17, result.stderr)
                self.assertNotIn("checked_native", result.stdout)

    def test_root_make_and_named_parser_admit_exact_focused_targets(self) -> None:
        for target in (*self.MODES, "focused-typo"):
            with self.subTest(target=target):
                result = subprocess.run(
                    ("make", "--no-print-directory", "-s", "test-target-check", f"TARGET={target}"),
                    cwd=job.MAINTENANCE_ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 2 if target == "focused-typo" else 0, result.stderr)
                arguments = [
                    "test", "start", "focused-mode", "--target", target,
                    "--selection", "cases/focused-subject", "--patch-mode", "patched",
                    "--source-head", "1" * 40, "--source-remote", "origin",
                    "--image", "localhost/xpra:test", "--image-input-sha256", "2" * 64,
                    "--source", "3" * 40, "--workflow-sha256", "4" * 64,
                ]
                if target == "focused-typo":
                    with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                        job.parser().parse_args(arguments)
                else:
                    args = job.parser().parse_args(arguments)
                    self.assertIs(args.handler, job.test_start)
                    labels = job.test_labels(
                        args, image_id="5" * 64, run_id="owned-run",
                        runner_digest="6" * 64, selection_sha256="7" * 64,
                    )
                    self.assertEqual(labels["io.xpra.fork-maintenance.target"], target)


class FocusedRuntimeModeTest(unittest.TestCase):
    def validate(
        self,
        *,
        cythonize: str,
        compat: str,
        compiled: bool,
        actual_compat: bool,
        outside: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        entrypoint = (job.RUNNER_ROOT / "entrypoint.sh").read_text(encoding="utf-8")
        validator = entrypoint.split("<<'FOCUSED_MODE_PY'\n", 1)[1].split(
            "\nFOCUSED_MODE_PY", 1
        )[0]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            installed = root / "installed"
            installed.mkdir()
            module = (root if outside else installed) / ("common.so" if compiled else "common.py")
            module.touch()
            # The validator's metadata controls do not claim to compile Xpra;
            # the named container smoke must import the real installed module.
            setup = (
                "import sys\nfrom types import ModuleType\n"
                "xpra = ModuleType('xpra')\n"
                "net = ModuleType('xpra.net')\n"
                "common = ModuleType('xpra.net.common')\n"
                f"common.__file__ = {str(module)!r}\n"
                f"common.BACKWARDS_COMPATIBLE = {actual_compat!r}\n"
                "xpra.net = net\nnet.common = common\n"
                "sys.modules.update({'xpra': xpra, 'xpra.net': net, 'xpra.net.common': common})\n"
            )
            return subprocess.run(
                (sys.executable, "-", str(installed), cythonize, compat),
                input=setup + validator,
                capture_output=True,
                text=True,
                check=False,
            )

    def test_installed_runtime_must_match_each_explicit_mode(self) -> None:
        for cythonize, compat in FocusedEntrypointTest.MODES.values():
            with self.subTest(cythonize=cythonize, compat=compat):
                result = self.validate(
                    cythonize=cythonize, compat=compat,
                    compiled=cythonize == "with", actual_compat=compat == "1",
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"focused_runtime_compiled={int(cythonize == 'with')}\n", result.stdout)
                self.assertIn(f"focused_runtime_backwards_compatible={compat}\n", result.stdout)

    def test_wrong_compiled_or_compatibility_mode_is_not_native_success(self) -> None:
        for cythonize, compat in FocusedEntrypointTest.MODES.values():
            for fault in ("compiled", "compatibility"):
                with self.subTest(cythonize=cythonize, compat=compat, fault=fault):
                    compiled = cythonize == "with"
                    actual_compat = compat == "1"
                    result = self.validate(
                        cythonize=cythonize, compat=compat,
                        compiled=not compiled if fault == "compiled" else compiled,
                        actual_compat=not actual_compat if fault == "compatibility" else actual_compat,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(fault, result.stderr)
                    self.assertNotIn("focused_runtime_module=", result.stdout)

    def test_host_or_source_import_cannot_stand_in_for_the_installed_module(self) -> None:
        result = self.validate(
            cythonize="with", compat="1", compiled=True, actual_compat=True, outside=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside the installed Xpra tree", result.stderr)


class QuarantineEntrypointTest(unittest.TestCase):
    @staticmethod
    def validator_source() -> str:
        entrypoint = (job.RUNNER_ROOT / "entrypoint.sh").read_text(encoding="utf-8")
        marker = "<<'QUARANTINE_SUMMARY_PY'\n"
        return entrypoint.split(marker, 1)[1].split("\nQUARANTINE_SUMMARY_PY", 1)[0]

    def validate(
        self,
        summary: str,
        *expected: str,
        module_count: int = 2,
        gate: str = "quarantine-cython",
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as raw:
            summary_path = Path(raw) / "summary.log"
            summary_path.write_text(summary, encoding="utf-8")
            return subprocess.run(
                [
                    sys.executable,
                    "-",
                    str(summary_path),
                    gate,
                    str(module_count),
                    *expected,
                ],
                input=self.validator_source(),
                capture_output=True,
                text=True,
                check=False,
            )

    def test_runner_uses_union_for_paths_and_gate_subset_only_for_skip_fail(self) -> None:
        entrypoint = (job.RUNNER_ROOT / "entrypoint.sh").read_text(encoding="utf-8")
        function = entrypoint.split("run_quarantine() {", 1)[1].split(
            "\n}\n\ncase ",
            1,
        )[0]
        self.assertIn(
            "if ! quarantined_output=$(selection_tool quarantined-tests); then",
            function,
        )
        self.assertIn(
            'if ! expected_output=$(selection_tool quarantined-tests --gate "$gate"); then',
            function,
        )
        self.assertIn('mapfile -t quarantined <<<"$quarantined_output"', function)
        self.assertIn('mapfile -t expected <<<"$expected_output"', function)
        self.assertIn('for module in "${expected[@]}"; do\n        skip_args+=', function)
        self.assertIn('for module in "${quarantined[@]}"; do\n        test_paths+=', function)

    def test_mixed_leg_requires_one_failure_and_one_success(self) -> None:
        summary = """test summary:
  successful tests: 0
  failed tests: 1
test summary:
  successful tests: 1
  failed tests: 0
  ignored failures: 1
    - unit.client.broken_test (exit code=1)
"""
        result = self.validate(summary, "unit.client.broken_test")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            "quarantine gate quarantine-cython confirmed failures: "
            "unit.client.broken_test\n",
        )

    def test_leg_with_no_expected_failure_requires_every_module_to_pass(self) -> None:
        summary = """test summary:
  successful tests: 2
  failed tests: 0
"""
        result = self.validate(summary)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("confirmed failures: <none>", result.stdout)

    def test_expected_failure_that_passes_is_stale(self) -> None:
        summary = """test summary:
  successful tests: 2
  failed tests: 0
"""
        result = self.validate(summary, "unit.client.broken_test")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("expected 1 successful modules, observed 2", result.stderr)

    def test_unignored_failure_or_skipped_module_is_contamination(self) -> None:
        summaries = (
            """test summary:
  successful tests: 0
  failed tests: 1
  ignored failures: 1
    - unit.client.broken_test (exit code=1)
""",
            """test summary:
  successful tests: 0
  failed tests: 0
  ignored failures: 1
    - unit.client.broken_test (exit code=1)
  skipped tests: 1
    - unit.client.green_test
""",
        )
        for summary in summaries:
            with self.subTest(summary=summary):
                result = self.validate(summary, "unit.client.broken_test")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("is contaminated", result.stderr)

    def test_ignored_failure_names_and_format_are_exact(self) -> None:
        summaries = (
            """test summary:
  successful tests: 1
  failed tests: 0
  ignored failures: 1
    - unit.client.other_test (exit code=1)
""",
            """test summary:
  successful tests: 1
  failed tests: 0
  ignored failures: 1
    - malformed ignored result
""",
        )
        for summary in summaries:
            with self.subTest(summary=summary):
                result = self.validate(summary, "unit.client.broken_test")
                self.assertNotEqual(result.returncode, 0)


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
