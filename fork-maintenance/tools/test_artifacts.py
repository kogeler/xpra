# Copyright (C) 2026 kogeler
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import artifacts
import contrib


class ArtifactsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        subprocess.run(("git", "init", "-q", str(self.repo)), check=True)
        (self.repo / ".gitignore").write_text("/.artifacts/\n", encoding="utf-8")
        subprocess.run(("git", "-C", str(self.repo), "add", ".gitignore"), check=True)
        subprocess.run(
            (
                "git",
                "-C",
                str(self.repo),
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-qm",
                "fixture",
            ),
            check=True,
        )
        self.root = contrib.cleanup_state_root(self.repo)
        (self.repo / ".artifacts").mkdir(mode=0o700)
        self.root.mkdir(mode=0o700)

    def directory(self, name: str, mode: int = 0o700) -> Path:
        path = self.root / name
        contrib.prepare_cleanup_directory(self.root, path, "test artifact directory")
        path.chmod(mode)
        return path

    def file(self, name: str, data: str = "fixture\n", mode: int = 0o600) -> Path:
        relative = Path(name)
        self.directory(str(relative.parent))
        path = self.root / relative
        path.write_text(data, encoding="utf-8")
        path.chmod(mode)
        return path

    def operate(self, action: str = "plan", confirm: str = "") -> artifacts.Inventory:
        return artifacts.operate(self.repo, action, confirm, inspect_runtime=False)

    def clean(self) -> artifacts.Inventory:
        report = self.operate()
        self.operate("clean", report.plan.digest)
        return report

    def test_static_policy_removes_old_and_future_scratch_without_name_lists(self) -> None:
        for name in (
            "audit-20010101.log",
            "review-20990101.log",
            "tmp/owner.py",
            "live-results/no-current-schema/data",
            "upstream-tests/logs/legacy.status",
            "deb-packages/results/unbound.json",
            "deb-packages/outputs/old.tar",
        ):
            self.file(name)
        kept = [
            self.file(name)
            for name in (
                "retained/handoff.md",
                "build-contexts/live/cache",
                "source-archives/cache",
                "upstream-tests/sources/cache",
                "deb-packages/selections/cache",
                "deb-packages/sources/cache",
                "venvs/cache",
                "tooling-venv/cache",
            )
        ]
        report = self.clean()
        self.assertEqual(len(report.plan.targets), 7)
        self.assertTrue(all(path.is_file() for path in kept))
        self.assertEqual(self.operate().plan.targets, ())
        self.assertEqual(self.clean().plan.targets, ())
        self.assertEqual(list((self.root / "cycle-cleanups").iterdir()), [])

    def test_no_confirmation_is_read_only(self) -> None:
        path = self.file("scratch")
        self.operate("clean")
        self.assertTrue(path.exists())

    def test_changed_target_invalidates_plan_before_any_deletion(self) -> None:
        first = self.file("a")
        last = self.file("z")
        report = self.operate()
        last.write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(contrib.ContribError, "CONFIRM"):
            self.operate("clean", report.plan.digest)
        self.assertTrue(first.exists())
        self.assertFalse((self.root / "cycle-cleanups").exists())

    def test_new_garbage_invalidates_reviewed_plan(self) -> None:
        self.file("a")
        report = self.operate()
        self.file("b")
        with self.assertRaisesRegex(contrib.ContribError, "CONFIRM"):
            self.operate("clean", report.plan.digest)

    def test_old_runtime_owners_protect_exact_result_families(self) -> None:
        self.file("upstream-tests/runs/a.b.owner", "old-format-owner")
        self.file("upstream-tests/image-builds/img/owner.json", "old-format-owner")
        self.file("jobs/live/live-a.owner.json", "old-format-owner")
        self.file("deb-packages/runs/deb-a/owner.json", "old-format-owner")
        protected = [
            self.file(name)
            for name in (
                "upstream-tests/logs/a.b.log",
                "upstream-tests/logs/img.status",
                "jobs/live/live-a.log",
                "live-results/live-a/data",
                "deb-packages/results/deb-a.status.json",
                "deb-packages/outputs/deb-a-ubuntu-debs.tar",
            )
        ]
        removed = self.file("live-results/live-ab/data")
        self.clean()
        self.assertTrue(all(path.exists() for path in protected))
        self.assertFalse(removed.exists())

    def test_runtime_appearing_after_plan_invalidates_confirmation(self) -> None:
        result = self.file("live-results/new/data")
        report = self.operate()
        self.file("jobs/live/new.freeze-prelaunch.json")
        with self.assertRaisesRegex(contrib.ContribError, "CONFIRM"):
            self.operate("clean", report.plan.digest)
        self.assertTrue(result.exists())

    def test_podman_runtime_without_owner_protects_its_result(self) -> None:
        result = self.file("live-results/runtime/data")
        self.file("scratch")
        with patch.object(
            artifacts,
            "podman_identities",
            return_value={
                "live": {"runtime"},
                "upstream": set(),
                "deb": set(),
            },
        ):
            report = artifacts.operate(self.repo, "plan")
        self.assertNotIn(result.parent, {target.path for target in report.plan.targets})

    def test_unknown_runtime_fails_closed(self) -> None:
        self.file("upstream-tests/runs/unrecognized")
        self.file("scratch")
        with self.assertRaisesRegex(contrib.ContribError, "unrecognized upstream runtime"):
            self.operate()

    def test_every_live_lifecycle_record_protects_its_base_run(self) -> None:
        for suffix in artifacts.LIVE_RUNTIME_SUFFIXES:
            with self.subTest(suffix=suffix):
                marker = self.file(f"jobs/live/run.with.dots{suffix}")
                result = self.file("live-results/run.with.dots/input")
                protected = artifacts.protections(self.root, inspect_runtime=False)
                self.assertIn(result.parent.relative_to(self.root), protected)
                self.assertIn(marker.relative_to(self.root), protected)
                marker.unlink()

    def test_runtime_inspection_failure_never_deletes(self) -> None:
        path = self.file("scratch")
        with (
            patch.object(artifacts, "podman_identities", side_effect=OSError("Podman unavailable")),
            self.assertRaises(OSError),
        ):
            artifacts.operate(self.repo, "clean", "0" * 64)
        self.assertTrue(path.exists())

    def test_dirty_workspace_is_protected_without_blocking_unrelated_cleanup(self) -> None:
        workspace = self.file("upstream-tests/workspaces/candidate/unique.patch")
        disposable = self.file("scratch")
        with patch.object(
            contrib, "_finalized_workspace_fingerprint_locked", side_effect=contrib.ContribError("unexported candidate")
        ):
            report = self.clean()
        self.assertTrue(workspace.exists())
        self.assertFalse(disposable.exists())
        self.assertIn("unexported candidate", dict(report.protected)["upstream-tests/workspaces/candidate"])

    def test_finalized_workspace_uses_existing_finalization_boundary(self) -> None:
        workspace = self.file("upstream-tests/workspaces/final/source/test")
        with patch.object(contrib, "_finalized_workspace_fingerprint_locked", return_value="a" * 64) as check:
            self.clean()
        check.assert_called_with(self.repo, "final")
        self.assertFalse(workspace.exists())

    def test_recovery_state_protects_workspaces_and_staging(self) -> None:
        workspace = self.file("upstream-tests/workspaces/candidate/unique")
        recovery = self.file("case-updates/case.update.owner.json")
        live = self.file("live-results/.run.freeze-abort-result/input")
        deb = self.file("deb-packages/outputs/.out.tar.validate.owner.json")
        output = self.file("deb-packages/outputs/out.tar")
        self.file("scratch")
        with patch.object(contrib, "_finalized_workspace_fingerprint_locked") as check:
            self.clean()
        check.assert_not_called()
        self.assertTrue(all(path.exists() for path in (workspace, recovery, live, deb, output)))

    def test_nonprivate_legacy_modes_are_bound_not_normalized(self) -> None:
        self.file("directory/file", mode=0o664)
        self.directory("directory", mode=0o755)
        loose = self.file("report.md", mode=0o664)
        self.clean()
        self.assertFalse(loose.exists())
        self.assertFalse((self.root / "directory").exists())

    def test_symlinks_hardlinks_and_other_writable_paths_are_protected(self) -> None:
        outside = self.repo / ".artifacts/outside"
        outside.write_text("must survive", encoding="utf-8")
        outside.chmod(0o600)
        (self.root / "link").symlink_to(outside)
        os.link(outside, self.root / "hardlink")
        self.directory("tree")
        (self.root / "tree/link").symlink_to(outside)
        unsafe = self.file("unsafe", mode=0o666)
        self.file("scratch")
        report = self.clean()
        self.assertEqual(len(report.blocked), 4)
        self.assertEqual(outside.read_text(encoding="utf-8"), "must survive")
        self.assertTrue(unsafe.exists())

    def test_runtime_and_locks_survive_an_accidental_policy_omission(self) -> None:
        owner = self.file("upstream-tests/runs/run.owner")
        recovery = self.file("case-updates/case.update.owner.json")
        lock = self.file("upstream-tests/logs/.lifecycle.lock", "")
        disposable = self.file("scratch")
        policy = self.repo / ".artifacts/policy.toml"
        payload = artifacts.POLICY_PATH.read_bytes()
        for entry in ("upstream-tests/runs", "upstream-tests/logs/.lifecycle.lock", "case-updates", "cycle-cleanups"):
            payload = payload.replace(f'  "{entry}",\n'.encode(), b"")
        policy.write_bytes(payload)
        report = artifacts.operate(self.repo, "plan", policy_path=policy, inspect_runtime=False)
        artifacts.operate(self.repo, "clean", report.plan.digest, policy_path=policy, inspect_runtime=False)
        self.assertTrue(all(path.exists() for path in (owner, recovery, lock)))
        self.assertFalse(disposable.exists())

    def test_check_never_calls_unsafe_leftovers_clean(self) -> None:
        blocked = self.file("unsafe", mode=0o666)
        report = self.operate()
        with (
            patch("sys.argv", ["artifacts.py", "check"]),
            patch.object(artifacts, "operate", return_value=report),
            patch("builtins.print"),
        ):
            self.assertEqual(artifacts.main(), 1)
        self.assertEqual(report.plan.targets, ())
        self.assertEqual(report.blocked[0][0], blocked.name)

    def test_parent_symlink_is_never_traversed(self) -> None:
        (self.root / "jobs").symlink_to(self.repo, target_is_directory=True)
        with self.assertRaisesRegex(contrib.ContribError, "not a real owned directory"):
            self.operate()

    def test_changed_directory_mode_invalidates_plan(self) -> None:
        self.file("scratch/file")
        report = self.operate()
        (self.root / "scratch").chmod(0o755)
        with self.assertRaisesRegex(contrib.ContribError, "CONFIRM"):
            self.operate("clean", report.plan.digest)

    def test_interrupted_partial_rmtree_resumes_exact_transaction(self) -> None:
        self.file("scratch/a")
        self.file("scratch/b")
        self.directory("scratch", mode=0o755)
        report = self.operate()
        real_rmtree = shutil.rmtree

        def interrupt(path: Path, *args: object, **kwargs: object) -> None:
            if Path(path).parent == self.root / "cycle-cleanups":
                (Path(path) / "a").unlink()
                raise OSError("simulated interruption")
            real_rmtree(path, *args, **kwargs)

        with (
            patch.object(contrib.shutil, "rmtree", side_effect=interrupt),
            self.assertRaisesRegex(OSError, "interruption"),
        ):
            self.operate("clean", report.plan.digest)
        pending = contrib.load_pending_cleanup_transaction(self.repo)
        self.assertIsNotNone(pending)
        self.assertEqual(self.operate().plan, report.plan)
        self.operate("clean", report.plan.digest)
        self.assertFalse((self.root / "scratch").exists())
        self.assertIsNone(contrib.load_pending_cleanup_transaction(self.repo))

    def test_cycle_api_cannot_bypass_artifact_policy(self) -> None:
        self.file("scratch")
        report = self.operate()
        with self.assertRaisesRegex(contrib.ContribError, "use artifacts-clean"):
            contrib.remove_cleanup_plan(self.repo, report.plan, report.plan.digest)
        with self.assertRaisesRegex(contrib.ContribError, "use artifacts-clean"):
            contrib.build_cleanup_plan(self.repo, report.plan.cycle)

    def test_policy_change_invalidates_old_confirmation(self) -> None:
        self.file("scratch")
        report = self.operate()
        policy = self.repo / ".artifacts/policy.toml"
        policy.write_bytes(artifacts.POLICY_PATH.read_bytes() + b"\n# reviewed policy change\n")
        with self.assertRaisesRegex(contrib.ContribError, "CONFIRM"):
            artifacts.operate(self.repo, "clean", report.plan.digest, policy_path=policy, inspect_runtime=False)

    def test_policy_cannot_keep_the_whole_root_or_use_globs_or_traversal(self) -> None:
        for value in (".", "..", "/", "../elsewhere", "foo/../bar", "foo/", "", "a\nb", "*.log"):
            with self.subTest(value=value), self.assertRaises(contrib.ContribError):
                artifacts.relative_path(value)

    def test_save_moves_only_unmanaged_records_without_clobber(self) -> None:
        source = self.file("handoff.md", mode=0o664)
        target = artifacts.save(self.repo, "handoff.md", "notes/handoff.md")
        self.assertFalse(source.exists())
        self.assertTrue(target.exists())
        self.file("handoff.md", "new")
        with self.assertRaises(FileExistsError):
            artifacts.save(self.repo, "handoff.md", "notes/handoff.md")
        with self.assertRaisesRegex(contrib.ContribError, "unmanaged"):
            artifacts.save(self.repo, "upstream-tests", "unsafe")
        self.clean()
        self.assertEqual(target.read_text(encoding="utf-8"), "fixture\n")

    def test_pending_transaction_preserves_newly_owned_target(self) -> None:
        self.file("live-results/run/input")
        report = self.operate()
        with contrib.cleanup_lifecycle_locks(self.repo):
            contrib.publish_cleanup_transaction(self.repo, report.plan)
        self.file("jobs/live/run.owner.json")
        with self.assertRaisesRegex(contrib.ContribError, "runtime-owned"):
            self.operate("clean", report.plan.digest)

    def test_parser_rejects_modified_transaction_plan(self) -> None:
        self.file("scratch")
        report = self.operate()
        with contrib.cleanup_lifecycle_locks(self.repo):
            marker = contrib.publish_cleanup_transaction(self.repo, report.plan)
        record = json.loads(marker.read_text(encoding="utf-8"))
        record["plan"]["targets"][0]["path"] = "../outside"
        marker.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(contrib.ContribError, "not normalized"):
            self.operate()


if __name__ == "__main__":
    unittest.main(verbosity=2)
