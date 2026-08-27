from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import contrib


def command(*arguments: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        arguments,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


class MasterSyncTest(unittest.TestCase):
    def test_default_repository_is_parent_of_automation(self) -> None:
        self.assertEqual(contrib.DEFAULT_REPO, contrib.AUTOMATION_ROOT.parent)

    def test_fetch_updates_only_the_named_remote_tracking_ref(self) -> None:
        repo = Path("/tmp/xpra-fork")
        with patch.object(contrib, "git") as git_command:
            contrib.fetch_master(repo, "origin")
        git_command.assert_called_once_with(
            repo,
            "fetch",
            "origin",
            "refs/heads/master:refs/remotes/origin/master",
        )

    def test_sync_fetches_both_remotes_before_live_verification(self) -> None:
        repo = Path("/tmp/xpra-fork")
        base = "1" * 40
        with (
            patch.object(contrib, "verify_repo"),
            patch.object(contrib, "fetch_master") as fetch,
            patch.object(contrib, "verify_live_masters", return_value=base) as verify,
        ):
            self.assertEqual(contrib.sync_repo(repo), base)
        self.assertEqual(fetch.call_args_list, [call(repo, "upstream"), call(repo, "origin")])
        verify.assert_called_once_with(repo)

    def test_live_verification_rejects_stale_fork_without_force_advice(self) -> None:
        repo = Path("/tmp/xpra-fork")
        upstream = "1" * 40
        fork = "2" * 40

        def cached(_repo: Path, remote: str) -> str:
            return upstream if remote == "upstream" else fork

        def live(_repo: Path, remote: str, branch: str) -> str:
            self.assertEqual(branch, "master")
            return upstream if remote == "upstream" else fork

        with (
            patch.object(contrib, "cached_master", side_effect=cached),
            patch.object(contrib, "live_remote_ref", side_effect=live),
            self.assertRaisesRegex(contrib.ContribError, "gh repo sync") as raised,
        ):
            contrib.verify_live_masters(repo)
        self.assertIn("without --force", str(raised.exception))

    def test_live_verification_requires_cached_and_live_equality(self) -> None:
        repo = Path("/tmp/xpra-fork")
        with (
            patch.object(contrib, "cached_master", return_value="1" * 40),
            patch.object(contrib, "live_remote_ref", return_value="2" * 40),
            self.assertRaisesRegex(contrib.ContribError, "run repo-sync"),
        ):
            contrib.verify_live_masters(repo)


class DevelopRebaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        command("git", "init", "-q", "-b", "master", str(self.repo))
        command("git", "config", "user.name", "Develop Rebase Test", cwd=self.repo)
        command("git", "config", "user.email", "rebase@example.invalid", cwd=self.repo)
        (self.repo / "upstream.txt").write_text("base\n", encoding="utf-8")
        command("git", "add", "upstream.txt", cwd=self.repo)
        command("git", "commit", "-q", "-m", "base", cwd=self.repo)
        command("git", "switch", "-q", "-c", "develop", cwd=self.repo)
        (self.repo / "fork.txt").write_text("fork\n", encoding="utf-8")
        command("git", "add", "fork.txt", cwd=self.repo)
        command("git", "commit", "-q", "-m", "fork automation", cwd=self.repo)
        self.old_develop = command("git", "rev-parse", "HEAD", cwd=self.repo)
        command("git", "switch", "-q", "master", cwd=self.repo)
        (self.repo / "upstream.txt").write_text("current\n", encoding="utf-8")
        command("git", "add", "upstream.txt", cwd=self.repo)
        command("git", "commit", "-q", "-m", "upstream advance", cwd=self.repo)
        self.base = command("git", "rev-parse", "HEAD", cwd=self.repo)
        command("git", "switch", "-q", "develop", cwd=self.repo)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def sync_mocks(self):
        return (
            patch.object(contrib, "verify_repo"),
            patch.object(contrib, "sync_repo", return_value=self.base),
        )

    def test_patch_start_rejects_develop_before_rebase(self) -> None:
        verify, sync = self.sync_mocks()
        with (
            verify,
            sync,
            self.assertRaisesRegex(contrib.ContribError, "not rebased"),
        ):
            contrib.patch_start_check(self.repo)

    def test_develop_rebase_replays_linear_history(self) -> None:
        verify, sync = self.sync_mocks()
        with verify, sync:
            self.assertEqual(contrib.develop_rebase(self.repo), self.base)
        rebased = command("git", "rev-parse", "develop", cwd=self.repo)
        self.assertNotEqual(rebased, self.old_develop)
        self.assertEqual(
            command("git", "rev-list", "--merges", f"{self.base}..develop", cwd=self.repo),
            "",
        )
        verify, sync = self.sync_mocks()
        with verify, sync:
            self.assertEqual(contrib.patch_start_check(self.repo), self.base)

    def test_patch_start_rejects_merge_transfer(self) -> None:
        command("git", "merge", "-q", "--no-edit", "master", cwd=self.repo)
        verify, sync = self.sync_mocks()
        with (
            verify,
            sync,
            self.assertRaisesRegex(contrib.ContribError, "merge commits"),
        ):
            contrib.patch_start_check(self.repo)


class IsolatedStartTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        command("git", "init", "-q", "-b", "develop", str(self.repo))
        (self.repo / ".gitignore").write_text("/.artifacts/\n", encoding="utf-8")
        (self.repo / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
        command("git", "add", ".", cwd=self.repo)
        command(
            "git",
            "-c",
            "user.name=Isolated Test",
            "-c",
            "user.email=isolated@example.invalid",
            "commit",
            "-q",
            "-m",
            "base",
            cwd=self.repo,
        )
        self.base = command("git", "rev-parse", "HEAD", cwd=self.repo)
        control = self.repo / "fork-maintenance"
        control.mkdir()
        (control / "draft.txt").write_text("control\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_allows_dirty_control_plane_without_touching_the_branch(self) -> None:
        status = command("git", "status", "--porcelain=v1", cwd=self.repo)
        with (
            patch.object(contrib, "verify_repo"),
            patch.object(contrib, "sync_repo", return_value=self.base),
        ):
            state = contrib.isolated_start_check(self.repo)
        self.assertEqual(state.branch, "develop")
        self.assertEqual(state.head, self.base)
        self.assertEqual(state.source_commit, self.base)
        self.assertTrue(state.source_in_head)
        self.assertEqual(command("git", "status", "--porcelain=v1", cwd=self.repo), status)

    def test_rejects_a_dirty_host_source_path(self) -> None:
        (self.repo / "source.py").write_text("VALUE = 2\n", encoding="utf-8")
        with (
            patch.object(contrib, "verify_repo"),
            patch.object(contrib, "sync_repo", return_value=self.base),
            self.assertRaisesRegex(contrib.ContribError, "refuses host Xpra source changes"),
        ):
            contrib.isolated_start_check(self.repo)


class CiCheckoutValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        command("git", "init", "-q", "-b", "develop", str(self.repo))
        (self.repo / "tracked.txt").write_text("develop\n", encoding="utf-8")
        command("git", "add", "tracked.txt", cwd=self.repo)
        command(
            "git",
            "-c",
            "user.name=CI Remote Test",
            "-c",
            "user.email=ci-remotes@example.invalid",
            "commit",
            "-q",
            "-m",
            "develop",
            cwd=self.repo,
        )
        command(
            "git",
            "remote",
            "add",
            "origin",
            contrib.FORK_URL.removesuffix(".git"),
            cwd=self.repo,
        )
        self.head = command("git", "rev-parse", "HEAD", cwd=self.repo)
        self.environment = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_EVENT_NAME": "push",
            "GITHUB_REF": "refs/heads/develop",
            "GITHUB_REPOSITORY": "kogeler/xpra",
            "GITHUB_SHA": self.head,
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_requires_only_checkout_origin_without_mutating_remotes(self) -> None:
        with patch.dict(contrib.os.environ, self.environment):
            contrib.validate_ci_checkout(self.repo)

        self.assertEqual(
            command("git", "remote", "get-url", "origin", cwd=self.repo),
            contrib.FORK_URL.removesuffix(".git"),
        )
        self.assertEqual(command("git", "remote", cwd=self.repo), "origin")
        self.assertEqual(command("git", "rev-parse", "HEAD", cwd=self.repo), self.head)
        self.assertEqual(command("git", "branch", "--show-current", cwd=self.repo), "develop")

    def test_rejects_a_non_develop_push(self) -> None:
        environment = {**self.environment, "GITHUB_REF": "refs/heads/master"}
        with (
            patch.dict(contrib.os.environ, environment),
            self.assertRaisesRegex(contrib.ContribError, "unexpected GITHUB_REF"),
        ):
            contrib.validate_ci_checkout(self.repo)


class CiPrepareTest(unittest.TestCase):
    def test_does_not_run_the_publication_layout_audit(self) -> None:
        repo = Path("/tmp/xpra-fork")
        state = contrib.IsolatedState(
            branch="develop",
            head="1" * 40,
            source_commit="2" * 40,
            fork_base="2" * 40,
            source_in_head=True,
            worktree_status="",
        )
        with (
            patch.object(contrib, "validate_ci_checkout") as validate,
            patch.object(contrib, "ci_start_check", return_value=state) as start,
            patch.object(contrib, "ci_layout_check") as layout,
            patch.object(contrib, "sync_repo") as sync,
        ):
            self.assertEqual(contrib.ci_prepare(repo), state)

        validate.assert_called_once_with(repo)
        start.assert_called_once_with(repo)
        layout.assert_not_called()
        sync.assert_not_called()

    def test_start_uses_cached_fork_master_without_live_sync(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            command("git", "init", "-q", "-b", "master", str(repo))
            (repo / ".gitignore").write_text("/.artifacts/\n", encoding="utf-8")
            command("git", "add", ".gitignore", cwd=repo)
            command(
                "git",
                "-c",
                "user.name=CI Start Test",
                "-c",
                "user.email=ci-start@example.invalid",
                "commit",
                "-q",
                "-m",
                "master",
                cwd=repo,
            )
            base = command("git", "rev-parse", "HEAD", cwd=repo)
            command("git", "update-ref", "refs/remotes/origin/master", base, cwd=repo)
            command("git", "switch", "-q", "-c", "develop", cwd=repo)
            control = repo / "fork-maintenance"
            control.mkdir()
            (control / "marker.txt").write_text("fork\n", encoding="utf-8")
            command("git", "add", "fork-maintenance/marker.txt", cwd=repo)
            command(
                "git",
                "-c",
                "user.name=CI Start Test",
                "-c",
                "user.email=ci-start@example.invalid",
                "commit",
                "-q",
                "-m",
                "fork",
                cwd=repo,
            )

            with (
                patch.object(contrib, "verify_repo"),
                patch.object(contrib, "artifact_boundary_check"),
                patch.object(contrib, "sync_repo") as sync,
            ):
                state = contrib.ci_start_check(repo)

            self.assertEqual(state.source_commit, base)
            self.assertTrue(state.source_in_head)
            sync.assert_not_called()

    def test_start_keeps_the_embedded_base_when_fork_master_advances(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            command("git", "init", "-q", "-b", "master", str(repo))
            (repo / ".gitignore").write_text("/.artifacts/\n", encoding="utf-8")
            command("git", "add", ".gitignore", cwd=repo)
            command(
                "git",
                "-c",
                "user.name=CI Start Test",
                "-c",
                "user.email=ci-start@example.invalid",
                "commit",
                "-q",
                "-m",
                "master",
                cwd=repo,
            )
            embedded_base = command("git", "rev-parse", "HEAD", cwd=repo)
            command("git", "switch", "-q", "-c", "develop", cwd=repo)
            control = repo / "fork-maintenance"
            control.mkdir()
            (control / "marker.txt").write_text("fork\n", encoding="utf-8")
            command("git", "add", "fork-maintenance/marker.txt", cwd=repo)
            command(
                "git",
                "-c",
                "user.name=CI Start Test",
                "-c",
                "user.email=ci-start@example.invalid",
                "commit",
                "-q",
                "-m",
                "fork",
                cwd=repo,
            )
            develop = command("git", "rev-parse", "HEAD", cwd=repo)
            command("git", "switch", "-q", "master", cwd=repo)
            (repo / "upstream.txt").write_text("later\n", encoding="utf-8")
            command("git", "add", "upstream.txt", cwd=repo)
            command(
                "git",
                "-c",
                "user.name=CI Start Test",
                "-c",
                "user.email=ci-start@example.invalid",
                "commit",
                "-q",
                "-m",
                "later master",
                cwd=repo,
            )
            fork_tip = command("git", "rev-parse", "HEAD", cwd=repo)
            command("git", "update-ref", "refs/remotes/origin/master", fork_tip, cwd=repo)
            command("git", "switch", "-q", "develop", cwd=repo)

            with (
                patch.object(contrib, "verify_repo"),
                patch.object(contrib, "artifact_boundary_check"),
                patch.object(contrib, "sync_repo") as sync,
            ):
                state = contrib.ci_start_check(repo)

            self.assertEqual(command("git", "rev-parse", "HEAD", cwd=repo), develop)
            self.assertEqual(state.source_commit, embedded_base)
            self.assertTrue(state.source_in_head)
            sync.assert_not_called()


class CiLayoutTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        command("git", "init", "-q", "-b", "master", str(self.repo))
        upstream = self.repo / ".github" / "workflows"
        upstream.mkdir(parents=True)
        (upstream / "build.yml").write_text("name: Build\n", encoding="utf-8")
        (upstream / "test.yaml").write_text("name: Test\n", encoding="utf-8")
        command("git", "add", ".", cwd=self.repo)
        command(
            "git",
            "-c",
            "user.name=CI Layout Test",
            "-c",
            "user.email=ci-layout@example.invalid",
            "commit",
            "-q",
            "-m",
            "upstream workflows",
            cwd=self.repo,
        )
        self.base = command("git", "rev-parse", "HEAD", cwd=self.repo)
        disabled = self.repo / ".github" / "upstream-workflows"
        disabled.mkdir()
        (upstream / "build.yml").rename(disabled / "build.yml")
        (upstream / "test.yaml").rename(disabled / "test.yaml")
        workflow = list(contrib.fork_workflow_semantics())
        uses = f"        uses: actions/checkout@{contrib.CHECKOUT_ACTION_SHA}"
        workflow[workflow.index(uses)] += f"  # {contrib.CHECKOUT_ACTION_VERSION}"
        (upstream / "develop.yml").write_text(
            "\n".join(workflow) + "\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_accepts_exact_disabled_renames_and_thin_workflow(self) -> None:
        result = contrib.ci_layout_check(self.repo, self.base)

        self.assertEqual(
            result["disabled_upstream_workflows"],
            ("build.yml", "test.yaml"),
        )
        self.assertEqual(result["checkout_action_version"], "v7.0.1")

    def test_rejects_a_canonical_workflow_left_active(self) -> None:
        (self.repo / ".github" / "workflows" / "build.yml").write_text(
            "name: Build\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(contrib.ContribError, "only the fork workflow"):
            contrib.ci_layout_check(self.repo, self.base)

    def test_rejects_a_modified_disabled_workflow(self) -> None:
        (self.repo / ".github" / "upstream-workflows" / "test.yaml").write_text(
            "name: Changed\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(contrib.ContribError, "differs from canonical"):
            contrib.ci_layout_check(self.repo, self.base)

    def test_rejects_tag_pinning_or_extra_ci_logic(self) -> None:
        workflow = self.repo / contrib.ACTIVE_FORK_WORKFLOW
        workflow.write_text(
            workflow.read_text(encoding="utf-8").replace(
                f"actions/checkout@{contrib.CHECKOUT_ACTION_SHA}",
                "actions/checkout@v7",
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(contrib.ContribError, "full SHA"):
            contrib.ci_layout_check(self.repo, self.base)

    def test_rejects_an_incomplete_ci_matrix(self) -> None:
        workflow = self.repo / contrib.ACTIVE_FORK_WORKFLOW
        workflow.write_text(
            workflow.read_text(encoding="utf-8").replace(
                "          - full-no-compat\n",
                "",
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(contrib.ContribError, "approved thin"):
            contrib.ci_layout_check(self.repo, self.base)


class IsolatedWorkspaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        command("git", "init", "-q", "-b", "master", str(self.repo))
        (self.repo / ".gitignore").write_text("/.artifacts/\n", encoding="utf-8")
        (self.repo / "target.txt").write_text("old\n", encoding="utf-8")
        command("git", "add", ".", cwd=self.repo)
        command(
            "git",
            "-c",
            "user.name=Workspace Test",
            "-c",
            "user.email=workspace@example.invalid",
            "commit",
            "-q",
            "-m",
            "base",
            cwd=self.repo,
        )
        self.base = command("git", "rev-parse", "HEAD", cwd=self.repo)
        command("git", "switch", "-q", "-c", "develop", cwd=self.repo)

        self.case_dir = self.repo / "fork-maintenance" / "cases" / "sample-case"
        self.case_dir.mkdir(parents=True)
        (self.case_dir / "README.md").write_text("# Sample\n", encoding="utf-8")
        patch_bytes = (
            b"diff --git a/target.txt b/target.txt\n"
            b"--- a/target.txt\n"
            b"+++ b/target.txt\n"
            b"@@ -1 +1 @@\n"
            b"-old\n"
            b"+new\n"
            b"diff --git a/tests/sample_test.py b/tests/sample_test.py\n"
            b"new file mode 100644\n"
            b"--- /dev/null\n"
            b"+++ b/tests/sample_test.py\n"
            b"@@ -0,0 +1 @@\n"
            b"+VALUE = 1\n"
        )
        (self.case_dir / "fix.patch").write_bytes(patch_bytes)
        digest = hashlib.sha256(patch_bytes).hexdigest()
        (self.case_dir / "case.toml").write_text(
            "\n".join(
                (
                    "schema = 1",
                    'slug = "sample-case"',
                    'title = "Sample"',
                    'commit_subject = "Sample"',
                    f'patch_sha256 = "{digest}"',
                    "dependencies = []",
                    'paths = ["target.txt", "tests/sample_test.py"]',
                    "",
                    "[tests]",
                    'list = ["unit.sample_test"]',
                    "",
                    "[evidence]",
                    "required_gates = []",
                    "",
                )
            ),
            encoding="utf-8",
        )
        command("git", "add", "fork-maintenance", cwd=self.repo)
        command(
            "git",
            "-c",
            "user.name=Workspace Test",
            "-c",
            "user.email=workspace@example.invalid",
            "commit",
            "-q",
            "-m",
            "control plane",
            cwd=self.repo,
        )
        self.head = command("git", "rev-parse", "HEAD", cwd=self.repo)
        self.case = contrib.load_case(self.case_dir)
        self.resolution = {
            "schema": 1,
            "source_commit": self.base,
            "selection": "cases/sample-case",
            "selection_sha256": "1" * 64,
            "declared_cases": ["sample-case"],
            "base_dependencies": [],
            "patches": [
                {
                    "case": "sample-case",
                    "patch": "cases/sample-case/fix.patch",
                    "patch_sha256": self.case.patch_sha256,
                    "status": "apply",
                }
            ],
            "applied_cases": ["sample-case"],
            "already_present_cases": [],
            "resolution_sha256": "2" * 64,
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def mocks(self):
        state = contrib.IsolatedState(
            branch="develop",
            head=self.head,
            source_commit=self.base,
            fork_base=self.base,
            source_in_head=True,
            worktree_status=contrib.porcelain(self.repo),
        )
        return (
            patch.object(contrib, "isolated_start_check", return_value=state),
            patch.object(contrib, "selected_cases", return_value=(self.case,)),
            patch.object(contrib, "selection_resolution", return_value=self.resolution),
            patch.object(contrib, "get_case", return_value=self.case),
        )

    def test_patched_workspace_never_changes_the_host_source_or_branch(self) -> None:
        start, selected, resolution, _case = self.mocks()
        with start, selected, resolution:
            workspace = contrib.create_workspace(
                self.repo,
                "patched-01",
                "cases/sample-case",
                "patched",
            )
        self.assertEqual((self.repo / "target.txt").read_text(), "old\n")
        self.assertEqual((workspace.source / "target.txt").read_text(), "new\n")
        self.assertTrue((workspace.source / "tests" / "sample_test.py").is_file())
        self.assertFalse((workspace.source / "fork-maintenance").exists())
        self.assertEqual(command("git", "branch", "--show-current", cwd=self.repo), "develop")
        self.assertEqual(command("git", "rev-parse", "HEAD", cwd=self.repo), self.head)

    def test_tests_only_workspace_keeps_master_production_code(self) -> None:
        start, selected, resolution, _case = self.mocks()
        with start, selected, resolution:
            workspace = contrib.create_workspace(
                self.repo,
                "tests-only-01",
                "cases/sample-case",
                "tests-only",
            )
        self.assertEqual((workspace.source / "target.txt").read_text(), "old\n")
        self.assertTrue((workspace.source / "tests" / "sample_test.py").is_file())
        self.assertEqual(
            contrib.workspace_candidate_names(workspace),
            ("tests/sample_test.py",),
        )

    def test_workspace_update_exports_only_the_isolated_candidate(self) -> None:
        start, selected, resolution, case_mock = self.mocks()
        with start, selected, resolution:
            workspace = contrib.create_workspace(
                self.repo,
                "update-01",
                "cases/sample-case",
                "patched",
            )
        (workspace.source / "target.txt").write_text("newer\n", encoding="utf-8")
        with case_mock:
            names = contrib.stage_workspace(self.repo, "update-01")
            updated = contrib.update_case_from_workspace(self.repo, "update-01")
        self.assertEqual(names, ("target.txt", "tests/sample_test.py"))
        self.assertIn("+newer", updated.patch.read_text(encoding="utf-8"))
        self.assertEqual((self.repo / "target.txt").read_text(), "old\n")
        removed = contrib.remove_workspace(self.repo, "update-01")
        self.assertFalse(removed.exists())

    def test_clean_draft_workspace_promotes_without_touching_host_source(self) -> None:
        automation_root = self.repo / "fork-maintenance"
        cases_root = automation_root / "cases"
        draft_dir = cases_root / "draft-case"
        draft_dir.mkdir(parents=True)
        (draft_dir / "README.md").write_text("# Draft\n", encoding="utf-8")
        (draft_dir / "fix.patch").write_bytes(b"")
        (draft_dir / "case.toml").write_text(
            """schema = 1
draft = true
slug = "draft-case"
kind = "production"
title = "Draft"
commit_subject = "Draft"
patch_sha256 = ""
dependencies = []
paths = []

[tests]
list = ["unit.sample_test"]

[evidence]
required_gates = []
""",
            encoding="utf-8",
        )
        state = contrib.IsolatedState(
            branch="develop",
            head=self.head,
            source_commit=self.base,
            fork_base=self.base,
            source_in_head=True,
            worktree_status=contrib.porcelain(self.repo),
        )
        final_resolution = {
            "selection_sha256": "3" * 64,
            "resolution_sha256": "4" * 64,
        }
        with (
            patch.object(contrib, "AUTOMATION_ROOT", automation_root),
            patch.object(contrib, "CASES_ROOT", cases_root),
            patch.object(contrib, "isolated_start_check", return_value=state),
        ):
            workspace = contrib.create_workspace(
                self.repo,
                "draft-01",
                "cases/draft-case",
                "clean",
            )
            (workspace.source / "target.txt").write_text("quarantined\n", encoding="utf-8")
            self.assertEqual(contrib.stage_workspace(self.repo, "draft-01"), ("target.txt",))
            with patch.object(contrib, "selection_resolution", return_value=final_resolution):
                updated = contrib.update_case_from_workspace(self.repo, "draft-01")

        self.assertEqual((self.repo / "target.txt").read_text(encoding="utf-8"), "old\n")
        self.assertEqual(updated.paths, ("target.txt",))
        self.assertNotIn("draft = true", updated.manifest.read_text(encoding="utf-8"))
        metadata = contrib.workspace_metadata_path(workspace.directory).read_text(encoding="utf-8")
        self.assertIn('"patch_mode": "patched"', metadata)

    def test_cycle_cleanup_rejects_an_unexported_workspace_candidate(self) -> None:
        start, selected, resolution, _case = self.mocks()
        with start, selected, resolution:
            workspace = contrib.create_workspace(
                self.repo,
                "audit-unexported-01",
                "cases/sample-case",
                "patched",
            )
        (workspace.source / "target.txt").write_text("unfinished\n", encoding="utf-8")
        with (
            selected,
            resolution,
            self.assertRaisesRegex(contrib.ContribError, "unexported candidate"),
        ):
            contrib.finalized_workspace_fingerprint(
                self.repo,
                "audit-unexported-01",
            )

    def test_finalized_workspace_matches_the_current_patch_queue(self) -> None:
        start, selected, resolution, _case = self.mocks()
        with start, selected, resolution:
            contrib.create_workspace(
                self.repo,
                "audit-finalized-01",
                "cases/sample-case",
                "patched",
            )
        with selected, resolution:
            fingerprint = contrib.finalized_workspace_fingerprint(
                self.repo,
                "audit-finalized-01",
            )
        self.assertRegex(fingerprint, r"^[0-9a-f]{64}$")

    def test_cleanup_refuses_a_workspace_changed_after_planning(self) -> None:
        start, selected, resolution, _case = self.mocks()
        with start, selected, resolution:
            workspace = contrib.create_workspace(
                self.repo,
                "audit-race-01",
                "cases/sample-case",
                "patched",
            )
        with patch.object(contrib, "verify_repo"), selected, resolution:
            plan = contrib.build_cleanup_plan(
                self.repo,
                "audit-race",
                inspect_runtime=False,
            )
        ignored = workspace.source / ".artifacts"
        ignored.mkdir()
        (ignored / "late-output").write_text("late\n", encoding="utf-8")
        with (
            patch.object(contrib, "verify_repo"),
            selected,
            resolution,
            self.assertRaisesRegex(contrib.ContribError, "changed after planning"),
        ):
            contrib.remove_cleanup_plan(self.repo, plan, plan.digest)


class CycleCleanupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        command("git", "init", "-q", "-b", "develop", str(self.repo))
        command("git", "remote", "add", "origin", contrib.FORK_URL, cwd=self.repo)
        command("git", "remote", "add", "upstream", contrib.UPSTREAM_URL, cwd=self.repo)
        (self.repo / ".gitignore").write_text("/.artifacts/\n", encoding="utf-8")
        command("git", "add", ".gitignore", cwd=self.repo)
        command(
            "git",
            "-c",
            "user.name=Cycle Cleanup Test",
            "-c",
            "user.email=cleanup@example.invalid",
            "commit",
            "-q",
            "-m",
            "fixture",
            cwd=self.repo,
        )
        self.root = self.repo / ".artifacts" / "fork-maintenance"
        self.logs = self.root / "upstream-tests" / "logs"
        self.runs = self.root / "upstream-tests" / "runs"
        self.image_builds = self.root / "upstream-tests" / "image-builds"
        for path in (
            self.repo / ".artifacts",
            self.root,
            self.root / "upstream-tests",
            self.logs,
            self.runs,
            self.image_builds,
        ):
            path.mkdir(mode=0o700, exist_ok=True)
            path.chmod(0o700)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def collected_result(self, name: str, payload: bytes = b"complete\n") -> None:
        log = self.logs / f"{name}.log"
        status = self.logs / f"{name}.status"
        log.write_bytes(payload)
        status.write_text(
            "\n".join(
                (
                    f"owner={contrib.UPSTREAM_TEST_OWNER}",
                    f"name={name}",
                    f"log_sha256={hashlib.sha256(payload).hexdigest()}",
                    "selection_resolution_ok=0",
                    "",
                )
            ),
            encoding="utf-8",
        )
        log.chmod(0o600)
        status.chmod(0o600)

    def test_digest_confirmed_cleanup_removes_only_the_named_cycle(self) -> None:
        self.collected_result("audit-focused-01")
        self.collected_result("auditor-keep-01", b"keep\n")
        plan = contrib.build_cleanup_plan(
            self.repo,
            "audit",
            inspect_runtime=False,
        )
        self.assertEqual(len(plan.targets), 2)
        with self.assertRaisesRegex(contrib.ContribError, "CONFIRM"):
            contrib.remove_cleanup_plan(self.repo, plan, "0" * 64)
        self.assertTrue((self.logs / "audit-focused-01.log").exists())
        self.assertEqual(
            contrib.remove_cleanup_plan(self.repo, plan, plan.digest),
            2,
        )
        self.assertFalse((self.logs / "audit-focused-01.log").exists())
        self.assertTrue((self.logs / "auditor-keep-01.log").exists())

    def test_cleanup_refuses_a_remaining_runtime_owner_record(self) -> None:
        self.collected_result("audit-focused-01")
        owner = self.runs / "audit-focused-01.owner"
        owner.write_text("owned\n", encoding="utf-8")
        owner.chmod(0o600)
        with self.assertRaisesRegex(contrib.ContribError, "runtime state"):
            contrib.build_cleanup_plan(
                self.repo,
                "audit",
                inspect_runtime=False,
            )

    def test_cleanup_refuses_a_target_changed_after_planning(self) -> None:
        self.collected_result("audit-focused-01")
        plan = contrib.build_cleanup_plan(
            self.repo,
            "audit",
            inspect_runtime=False,
        )
        log = self.logs / "audit-focused-01.log"
        log.write_bytes(b"changed after review\n")
        log.chmod(0o600)
        with self.assertRaisesRegex(contrib.ContribError, "changed after planning"):
            contrib.remove_cleanup_plan(self.repo, plan, plan.digest)

    def test_tree_fingerprint_accepts_safe_relative_symlinks(self) -> None:
        tree = self.root / "safe-tree"
        target = tree / "source" / "packaging" / "debian" / "xpra"
        target.mkdir(parents=True)
        for directory in (tree, target, *target.parents):
            if directory == self.root:
                break
            directory.chmod(0o700)
        link = tree / "source" / "debian"
        link.symlink_to("packaging/debian/xpra")
        payload = target / "group-writable-input"
        payload.write_text("input\n", encoding="utf-8")
        payload.chmod(0o664)

        fingerprint = contrib.secure_tree_fingerprint(tree)

        self.assertRegex(fingerprint, r"^[0-9a-f]{64}$")

    def test_tree_fingerprint_rejects_an_escaping_symlink(self) -> None:
        tree = self.root / "unsafe-tree"
        tree.mkdir()
        tree.chmod(0o700)
        (tree / "escape").symlink_to("../outside")

        with self.assertRaisesRegex(contrib.ContribError, "escapes its owned tree"):
            contrib.secure_tree_fingerprint(tree)

    def test_tree_fingerprint_rejects_an_other_writable_file(self) -> None:
        tree = self.root / "unsafe-file-tree"
        tree.mkdir(mode=0o700)
        payload = tree / "payload"
        payload.write_text("unsafe\n", encoding="utf-8")
        payload.chmod(0o666)

        with self.assertRaisesRegex(contrib.ContribError, "unsafe file"):
            contrib.secure_tree_fingerprint(tree)


class ManifestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo = contrib.DEFAULT_REPO
        contrib.verify_repo(cls.repo)
        cls.revision = contrib.rev_parse(cls.repo, "refs/remotes/upstream/master")

    def test_only_declared_active_cases_remain(self) -> None:
        cases = contrib.load_cases()
        self.assertEqual(
            set(cases),
            {
                "upstream-test-quarantine",
                "wayland-initial-window-state",
                "video-pipeline-cleanup-race",
            },
        )
        self.assertEqual(cases["upstream-test-quarantine"].kind, "test-quarantine")
        self.assertEqual(contrib.load_drafts(), {})

    def test_develop_stack_is_the_complete_active_queue(self) -> None:
        stack = contrib.load_stacks(contrib.load_cases())["develop"]
        self.assertEqual(
            stack.series,
            (
                "wayland-initial-window-state",
                "video-pipeline-cleanup-race",
                "upstream-test-quarantine",
            ),
        )

    def test_each_patch_resolves_against_cached_upstream_master(self) -> None:
        for slug in contrib.load_cases():
            with self.subTest(case=slug):
                resolution = contrib.selection_resolution(
                    self.repo,
                    self.revision,
                    f"cases/{slug}",
                )
                self.assertEqual(resolution["declared_cases"], [slug])
                self.assertIn(
                    resolution["patches"][0]["status"],
                    {"apply", "already-present"},
                )

    def test_develop_stack_resolves_in_manifest_order(self) -> None:
        stack = contrib.load_stacks(contrib.load_cases())["develop"]
        resolution = contrib.selection_resolution(
            self.repo,
            self.revision,
            "stacks/develop",
        )
        self.assertEqual(resolution["declared_cases"], list(stack.series))
        self.assertEqual(
            [entry["case"] for entry in resolution["patches"]],
            list(stack.series),
        )

    def test_runtime_roots_are_ignored_and_untracked(self) -> None:
        contrib.artifact_boundary_check(self.repo)


class PatchQueueFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        command("git", "init", "-q", "-b", "develop", str(self.repo))
        command("git", "config", "user.name", "Patch Queue Test", cwd=self.repo)
        command("git", "config", "user.email", "patch@example.invalid", cwd=self.repo)
        (self.repo / "target.txt").write_text("old\n", encoding="utf-8")
        self.case_dir = self.repo / "fork-maintenance" / "cases" / "sample-case"
        self.case_dir.mkdir(parents=True)
        (self.case_dir / "README.md").write_text(
            "# Sample case\n\nA deterministic fixture.\n",
            encoding="utf-8",
        )
        (self.case_dir / "tests").mkdir()
        (self.case_dir / "tests" / "README.md").write_text(
            "Fixture tests.\n",
            encoding="utf-8",
        )
        patch_bytes = (
            b"diff --git a/target.txt b/target.txt\n"
            b"--- a/target.txt\n"
            b"+++ b/target.txt\n"
            b"@@ -1 +1 @@\n"
            b"-old\n"
            b"+new\n"
        )
        (self.case_dir / "fix.patch").write_bytes(patch_bytes)
        digest = hashlib.sha256(patch_bytes).hexdigest()
        (self.case_dir / "case.toml").write_text(
            "\n".join(
                (
                    "schema = 1",
                    'slug = "sample-case"',
                    'title = "Change sample"',
                    'commit_subject = "Change sample"',
                    f'patch_sha256 = "{digest}"',
                    "dependencies = []",
                    'paths = ["target.txt"]',
                    "",
                    "[tests]",
                    'list = ["unit.sample.test"]',
                    "",
                    "[evidence]",
                    "required_gates = []",
                    "",
                )
            ),
            encoding="utf-8",
        )
        command("git", "add", ".", cwd=self.repo)
        command("git", "commit", "-q", "-m", "fixture", cwd=self.repo)
        self.base = command("git", "rev-parse", "HEAD", cwd=self.repo)
        command("git", "branch", "master", self.base, cwd=self.repo)
        self.case = contrib.load_case(self.case_dir)
        self.resolution = {
            "schema": 1,
            "source_commit": self.base,
            "selection": "cases/sample-case",
            "declared_cases": ["sample-case"],
            "applied_cases": ["sample-case"],
            "already_present_cases": [],
            "patches": [{"case": "sample-case", "status": "apply"}],
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def mocks(self, case: contrib.Case | None = None):
        selected = self.case if case is None else case
        return (
            patch.object(contrib, "verify_repo"),
            patch.object(contrib, "sync_repo", return_value=self.base),
            patch.object(contrib, "selected_cases", return_value=(selected,)),
            patch.object(contrib, "selection_resolution", return_value=self.resolution),
        )

    def test_apply_and_unapply_are_inverse_index_operations(self) -> None:
        verify, sync, selected, resolution = self.mocks()
        with verify, sync, selected, resolution:
            contrib.apply_selection(self.repo, "cases/sample-case")
            self.assertEqual((self.repo / "target.txt").read_text(), "new\n")
            self.assertEqual(contrib.staged_names(self.repo), ("target.txt",))
            contrib.unapply_selection(self.repo, "cases/sample-case")
        self.assertEqual((self.repo / "target.txt").read_text(), "old\n")
        self.assertEqual(command("git", "status", "--porcelain", cwd=self.repo), "")

    def test_master_is_write_protected(self) -> None:
        command("git", "switch", "-q", "master", cwd=self.repo)
        with (
            patch.object(contrib, "verify_repo"),
            self.assertRaisesRegex(contrib.ContribError, "refusing.*master"),
        ):
            contrib.apply_selection(self.repo, "cases/sample-case")

    def test_apply_rejects_a_committed_source_copy_of_the_patch(self) -> None:
        (self.repo / "target.txt").write_text("different\n", encoding="utf-8")
        command("git", "add", "target.txt", cwd=self.repo)
        command("git", "commit", "-q", "-m", "unexpected source edit", cwd=self.repo)
        with (
            patch.object(contrib, "verify_repo"),
            patch.object(contrib, "sync_repo", return_value=self.base),
            patch.object(contrib, "selected_cases", return_value=(self.case,)),
            self.assertRaisesRegex(contrib.ContribError, "committed source changes"),
        ):
            contrib.apply_selection(self.repo, "cases/sample-case")

    def test_patch_update_refreshes_full_staged_diff_then_unapplies_it(self) -> None:
        (self.repo / "target.txt").write_text("newer\n", encoding="utf-8")
        command("git", "add", "target.txt", cwd=self.repo)
        with (
            patch.object(contrib, "verify_repo"),
            patch.object(contrib, "sync_repo", return_value=self.base),
        ):
            updated = contrib.update_case_patch(self.repo, self.case)
        self.assertNotEqual(updated.patch_sha256, self.case.patch_sha256)
        self.assertIn("+newer", updated.patch.read_text(encoding="utf-8"))
        self.assertEqual(contrib.staged_names(self.repo), ("target.txt",))
        resolution = dict(self.resolution, source_commit=self.base)
        with (
            patch.object(contrib, "verify_repo"),
            patch.object(contrib, "selected_cases", return_value=(updated,)),
            patch.object(contrib, "selection_resolution", return_value=resolution),
        ):
            contrib.unapply_selection(self.repo, "cases/sample-case")
        self.assertEqual((self.repo / "target.txt").read_text(), "old\n")
        self.assertEqual(contrib.staged_names(self.repo), ())
        self.assertEqual(
            set(contrib.unstaged_names(self.repo)),
            {
                "fork-maintenance/cases/sample-case/case.toml",
                "fork-maintenance/cases/sample-case/fix.patch",
            },
        )


class ManifestRewriteTest(unittest.TestCase):
    def test_derived_fields_are_replaced_and_draft_is_promoted(self) -> None:
        original = (
            "schema = 1\n"
            "draft = true\n"
            'slug = "sample"\n'
            'patch_sha256 = ""\n'
            "paths = []\n"
        )
        digest = "a" * 64
        updated = contrib.updated_manifest_text(
            original,
            digest=digest,
            paths=("one.py", "two.py"),
            draft=True,
        )
        self.assertNotIn("draft = true", updated)
        self.assertIn(f'patch_sha256 = "{digest}"', updated)
        self.assertIn('  "one.py",', updated)
        self.assertIn('  "two.py",', updated)


if __name__ == "__main__":
    unittest.main(verbosity=2)
