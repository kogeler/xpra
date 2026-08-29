from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import private_state

MAKEFILE = Path(__file__).with_name("Makefile")
LAB_MAKEFILE = Path(__file__).resolve().parents[2] / "Makefile"
ENTRYPOINT = Path(__file__).with_name("entrypoint.sh")
CONTRACT = Path(__file__).resolve().parents[2] / "CONTRACT.md"


class PrivateStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.project_root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @property
    def state_root(self) -> Path:
        return self.project_root / ".artifacts" / "fork-maintenance" / "upstream-tests"

    def required_directories(self) -> tuple[Path, ...]:
        artifact_root = self.project_root / ".artifacts"
        lab_root = artifact_root / "fork-maintenance"
        return (
            artifact_root,
            lab_root,
            self.state_root,
            *(self.state_root / name for name in private_state.STATE_CHILDREN),
        )

    def test_creates_the_exact_private_tree(self) -> None:
        private_state.prepare_private_state(self.project_root)

        for path in self.required_directories():
            self.assertTrue(path.is_dir())
            self.assertFalse(path.is_symlink())
            self.assertEqual(path.stat().st_uid, os.getuid())
            self.assertEqual(path.stat().st_mode & 0o7777, 0o700)

    def test_tightens_existing_owned_directories_including_mode_zero(self) -> None:
        for path in self.required_directories():
            path.mkdir()
            path.chmod(0o775)
        (self.project_root / ".artifacts").chmod(0o755)
        (self.state_root / "logs").chmod(0o000)

        private_state.prepare_private_state(self.project_root)

        artifact_root, *private_directories = self.required_directories()
        self.assertEqual(artifact_root.stat().st_mode & 0o7777, 0o755)
        for path in private_directories:
            self.assertEqual(path.stat().st_mode & 0o7777, 0o700)

    def test_rejects_writable_shared_parent_without_chmod(self) -> None:
        artifact_root = self.project_root / ".artifacts"
        artifact_root.mkdir(mode=0o775)

        with self.assertRaisesRegex(
            private_state.PrivateStateError,
            "shared artifact parent is group or other writable",
        ):
            private_state.prepare_private_state(self.project_root)

        self.assertEqual(artifact_root.stat().st_mode & 0o7777, 0o775)
        self.assertEqual(list(artifact_root.iterdir()), [])

    def test_accepts_group_writable_project_root_without_chmod(self) -> None:
        self.project_root.chmod(0o775)
        try:
            private_state.prepare_private_state(self.project_root)

            self.assertEqual(self.project_root.stat().st_mode & 0o7777, 0o775)
            for path in self.required_directories()[1:]:
                self.assertEqual(path.stat().st_mode & 0o7777, 0o700)
        finally:
            self.project_root.chmod(0o700)

    def test_rejects_other_writable_project_root_without_chmod(self) -> None:
        self.project_root.chmod(0o777)
        try:
            with self.assertRaisesRegex(
                private_state.PrivateStateError,
                "project root is other writable",
            ):
                private_state.prepare_private_state(self.project_root)

            self.assertEqual(self.project_root.stat().st_mode & 0o7777, 0o777)
            self.assertEqual(list(self.project_root.iterdir()), [])
        finally:
            self.project_root.chmod(0o700)

    def test_rejects_a_symlink_without_modifying_its_target(self) -> None:
        target = self.project_root / "outside"
        target.mkdir(mode=0o755)
        (self.project_root / ".artifacts").symlink_to(
            target,
            target_is_directory=True,
        )

        with self.assertRaisesRegex(
            private_state.PrivateStateError,
            "not a real directory",
        ):
            private_state.prepare_private_state(self.project_root)

        self.assertEqual(target.stat().st_mode & 0o7777, 0o755)
        self.assertEqual(list(target.iterdir()), [])

    def test_rejects_a_nested_symlink_without_modifying_its_target(self) -> None:
        self.state_root.mkdir(parents=True)
        (self.project_root / ".artifacts").chmod(0o755)
        target = self.project_root / "outside"
        target.mkdir(mode=0o755)
        (self.state_root / "logs").symlink_to(target, target_is_directory=True)

        with self.assertRaisesRegex(
            private_state.PrivateStateError,
            "not a real directory",
        ):
            private_state.prepare_private_state(self.project_root)

        self.assertEqual(target.stat().st_mode & 0o7777, 0o755)
        self.assertEqual(list(target.iterdir()), [])

    def test_rejects_wrong_owner_before_chmod(self) -> None:
        artifact_root = self.project_root / ".artifacts"
        artifact_root.mkdir(mode=0o755)

        with self.assertRaisesRegex(
            private_state.PrivateStateError,
            "not owned by this user",
        ):
            private_state.prepare_private_state(
                self.project_root,
                expected_uid=os.getuid() + 1,
            )

        self.assertEqual(artifact_root.stat().st_mode & 0o7777, 0o755)


class UpstreamMakeContractTest(unittest.TestCase):
    def test_local_runner_defaults_to_fork_master(self) -> None:
        makefile = MAKEFILE.read_text(encoding="utf-8")
        self.assertIn("SOURCE_REMOTE ?= origin", makefile)
        self.assertNotIn("SOURCE_REMOTE ?= upstream", makefile)

    def test_tests_only_mode_keeps_production_unpatched(self) -> None:
        entrypoint = ENTRYPOINT.read_text(encoding="utf-8")
        self.assertIn("clean|tests-only|patched", entrypoint)
        self.assertIn("--include='tests/**'", entrypoint)
        self.assertIn("patched|tests-only", entrypoint)
        self.assertIn("tests-only workspace", CONTRACT.read_text(encoding="utf-8"))

    def test_source_bundle_is_verified_before_publication(self) -> None:
        source = Path(__file__).with_name("job.py").read_text(encoding="utf-8")
        snapshot = source.split("def source_snapshot", 1)[1].split(
            "def publish_bytes",
            1,
        )[0]
        create = snapshot.index("created = subprocess.run(")
        verify = snapshot.index("verify_source_bundle(partial", create)
        publish = snapshot.index("container_payload.rename_no_replace", verify)
        self.assertLess(create, verify)
        self.assertLess(verify, publish)
        self.assertIn("pass_fds=(lock_fd,)", snapshot)

    def test_source_identity_crosses_the_make_container_boundary(self) -> None:
        makefile = MAKEFILE.read_text(encoding="utf-8")
        entrypoint = ENTRYPOINT.read_text(encoding="utf-8")
        job = Path(__file__).with_name("job.py").read_text(encoding="utf-8")

        self.assertIn('--source-head "$(SOURCE_TIP_COMMIT)"', makefile)
        self.assertIn('f"XPRA_EXPECTED_SOURCE_HEAD={args.source_head}"', job)
        self.assertIn('f"XPRA_EXPECTED_SOURCE_REF={source_ref}"', job)
        self.assertIn("$EXPECTED_SOURCE_HEAD $EXPECTED_SOURCE_REF", entrypoint)
        self.assertIn("XPRA_EXPECTED_SOURCE_REF={source_ref}", job)
        self.assertNotIn(
            '"$EXPECTED_COMMIT refs/remotes/upstream/master"',
            entrypoint,
        )

    def test_ci_rejects_bad_inputs_before_building_the_image(self) -> None:
        makefile = MAKEFILE.read_text(encoding="utf-8")
        self.assertIn(
            "ci: ci-policy-check selection-check source-snapshot ci-image",
            makefile,
        )
        self.assertIn('case "$${XPRA_CI_TARGET:-}" in', makefile)
        self.assertNotIn(
            "for target in full full-cython full-no-compat",
            makefile,
        )
        self.assertIn('target="$${XPRA_CI_TARGET}"', makefile)
        root_makefile = LAB_MAKEFILE.read_text(encoding="utf-8")
        self.assertIn('SOURCE_COMMIT="$$source_commit"', root_makefile)

    def test_named_jobs_keep_the_embedded_base_after_cached_master_advances(self) -> None:
        makefile = MAKEFILE.read_text(encoding="utf-8")
        recipe = makefile.split("background-start:", 1)[1].split(
            "\nbackground-name-check:",
            1,
        )[0]
        self.assertNotIn('"$(BASE_COMMIT)" = "$(SOURCE_TIP_COMMIT)"', recipe)
        self.assertIn('--source "$(BASE_COMMIT)"', recipe)
        self.assertIn('--source-head "$(SOURCE_TIP_COMMIT)"', recipe)

    def test_jobs_use_podman_and_the_owned_process_supervisor(self) -> None:
        makefile = MAKEFILE.read_text(encoding="utf-8")
        job = Path(__file__).with_name("job.py").read_text(encoding="utf-8")
        self.assertIn('"podman", "create", "--name", name', job)
        self.assertIn('["podman", "start", created],', job)
        self.assertIn("int(args.lifecycle_lock_descriptor)", job)
        self.assertIn("int(args.image_cache_lock_descriptor)", job)
        self.assertIn('command(["podman", "wait", record["container_id"]]', job)
        self.assertIn("background_job.launch(", job)
        self.assertIn('"podman",\n        "build"', job)
        self.assertIn("image-background-abort", makefile)

    def test_collected_results_bind_the_verified_selection_resolution(self) -> None:
        job = Path(__file__).with_name("job.py").read_text(encoding="utf-8")
        self.assertIn("def resolution_from_log", job)
        self.assertNotIn('"podman", "cp"', job)
        self.assertIn('"selection_resolution_ok": int(resolution_ok)', job)
        self.assertIn("resolution_from_log(log_path(name).read_bytes())", job)

    def test_failed_jobs_keep_an_exact_cleanup_path(self) -> None:
        job = Path(__file__).with_name("job.py").read_text(encoding="utf-8")
        self.assertIn("and labels_ok", job)
        self.assertIn('and not finished.startswith("0001-")', job)
        self.assertIn("verify_image_evidence(name, record)", job)
        self.assertIn("verify_test_evidence(name, record)", job)
        self.assertIn('"validation_ok": int(validation_ok)', job)

    def test_abort_paths_preserve_current_completed_jobs(self) -> None:
        job = Path(__file__).with_name("job.py").read_text(encoding="utf-8")
        test_abort_source = job.split("def test_abort", 1)[1].split(
            "def image_context", 1
        )[0]
        owned_test_abort = test_abort_source.split(
            "record = load_test_record(name, require_current=False)",
            1,
        )[1]
        image_abort_source = job.split("def image_abort", 1)[1].split(
            "def runner_sha", 1
        )[0]
        self.assertLess(
            owned_test_abort.index("container_lifecycle_state(record)"),
            owned_test_abort.index('["podman", "rm", "--force"'),
        )
        self.assertIn("completed test jobs must be collected", test_abort_source)
        self.assertLess(
            image_abort_source.index("background_job.process_state("),
            image_abort_source.index("shutil.rmtree(image_context(name))"),
        )
        self.assertIn("completed image jobs must be collected", image_abort_source)
        root_makefile = LAB_MAKEFILE.read_text(encoding="utf-8")
        self.assertIn("test-abort", root_makefile)
        self.assertIn("test-image-abort", root_makefile)


if __name__ == "__main__":
    unittest.main()
