# Copyright (C) 2026 kogeler

from __future__ import annotations

import argparse
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

import job


def image_args() -> argparse.Namespace:
    return argparse.Namespace(
        image="localhost/xpra-ci:test",
        image_input_sha256="1" * 64,
        source="2" * 40,
        workflow_sha256="3" * 64,
    )


class SourceBundleTest(unittest.TestCase):
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


class CiImageTest(unittest.TestCase):
    def test_ensure_reuses_only_a_verified_owned_image(self) -> None:
        args = image_args()
        exists = subprocess.CompletedProcess([], 0, "", "")
        with (
            patch.object(job, "prepare_state"),
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
        )

    def test_ensure_builds_in_private_temporary_context_and_removes_it(self) -> None:
        args = image_args()
        missing = subprocess.CompletedProcess([], 1, "", "")
        built = subprocess.CompletedProcess([], 0, "", "")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            contexts: list[Path] = []

            def populate(path: Path) -> None:
                contexts.append(path)
                (path / "Containerfile").write_text("FROM scratch\n", encoding="utf-8")

            with (
                patch.object(job, "IMAGE_BUILD_ROOT", root),
                patch.object(job, "prepare_state"),
                patch.object(job, "populate_image_context", side_effect=populate),
                patch.object(job, "uuid") as uuid_module,
                patch.object(job, "command", side_effect=(missing, built)) as command,
                patch.object(job, "image_identity", return_value="5" * 64) as identity,
            ):
                uuid_module.uuid4.return_value = "ci-build-id"
                self.assertEqual(job.image_ensure(args), 0)

            self.assertEqual(len(contexts), 1)
            self.assertFalse(contexts[0].exists())
            self.assertEqual(
                command.call_args_list,
                [
                    call(["podman", "image", "exists", args.image], check=False),
                    call(
                        job.image_build_argv(args, "ci-build-id"),
                        capture=False,
                        check=True,
                        cwd=contexts[0],
                    ),
                ],
            )
            identity.assert_called_once_with(
                args.image,
                args.image_input_sha256,
                args.workflow_sha256,
                source=args.source,
                build_run_id="ci-build-id",
            )


if __name__ == "__main__":
    unittest.main()
