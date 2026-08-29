from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

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

    def test_sync_fetches_both_master_refs_before_live_verification(self) -> None:
        repo = Path("/tmp/xpra-fork")
        base = "1" * 40
        with (
            patch.object(contrib, "verify_repo") as verify_repo,
            patch.object(contrib, "fetch_master") as fetch,
            patch.object(
                contrib, "verify_live_fork_master", return_value=base
            ) as verify,
        ):
            self.assertEqual(contrib.sync_repo(repo), base)
        verify_repo.assert_called_once_with(repo, ("origin", "upstream"))
        self.assertEqual(
            fetch.call_args_list,
            [
                call(repo, "origin"),
                call(repo, "upstream"),
            ],
        )
        verify.assert_called_once_with(repo)

    def test_live_fork_verification_accepts_the_fetched_commit(self) -> None:
        repo = Path("/tmp/xpra-fork")
        fork = "1" * 40
        with (
            patch.object(contrib, "cached_master", return_value=fork) as cached,
            patch.object(contrib, "live_remote_ref", return_value=fork) as live,
        ):
            self.assertEqual(contrib.verify_live_fork_master(repo), fork)
        self.assertEqual(
            cached.call_args_list,
            [
                call(repo, "origin"),
                call(repo, "upstream"),
            ],
        )
        self.assertEqual(
            live.call_args_list,
            [
                call(repo, "origin", "master"),
                call(repo, "upstream", "master"),
            ],
        )

    def test_live_fork_verification_requires_cached_and_live_equality(self) -> None:
        repo = Path("/tmp/xpra-fork")
        with (
            patch.object(contrib, "cached_master", return_value="1" * 40),
            patch.object(contrib, "live_remote_ref", return_value="2" * 40),
            self.assertRaisesRegex(contrib.ContribError, "run repo-sync"),
        ):
            contrib.verify_live_fork_master(repo)

    def test_live_fork_verification_requires_canonical_equality(self) -> None:
        repo = Path("/tmp/xpra-fork")
        fork = "1" * 40
        upstream = "2" * 40
        with (
            patch.object(
                contrib,
                "cached_master",
                side_effect=[fork, upstream],
            ),
            patch.object(
                contrib,
                "live_remote_ref",
                side_effect=[fork, upstream],
            ),
            self.assertRaisesRegex(
                contrib.ContribError,
                "gh repo sync kogeler/xpra.*without --force",
            ),
        ):
            contrib.verify_live_fork_master(repo)


class CiMasterSyncTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        command("git", "init", "-q", "-b", "develop", str(self.repo))
        (self.repo / "tracked.txt").write_text("develop\n", encoding="utf-8")
        command("git", "add", "tracked.txt", cwd=self.repo)
        command(
            "git",
            "-c",
            "user.name=Master Sync Test",
            "-c",
            "user.email=master-sync@example.invalid",
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
            "GITHUB_EVENT_NAME": "schedule",
            "GITHUB_REF": "refs/heads/develop",
            "GITHUB_REPOSITORY": contrib.FORK_REPOSITORY,
            "GITHUB_SHA": self.head,
            "GITHUB_WORKFLOW_REF": (
                f"{contrib.FORK_REPOSITORY}/{contrib.MASTER_SYNC_WORKFLOW}"
                "@refs/heads/develop"
            ),
            "GH_TOKEN": "test-token",
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_accepts_only_the_dedicated_scheduled_checkout(self) -> None:
        with (
            patch.dict(contrib.os.environ, self.environment),
            patch.object(contrib.shutil, "which", return_value="/usr/bin/gh"),
        ):
            contrib.validate_master_sync_checkout(self.repo)

    def test_accepts_manual_dispatch_from_develop(self) -> None:
        environment = {**self.environment, "GITHUB_EVENT_NAME": "workflow_dispatch"}
        with (
            patch.dict(contrib.os.environ, environment),
            patch.object(contrib.shutil, "which", return_value="/usr/bin/gh"),
        ):
            contrib.validate_master_sync_checkout(self.repo)

    def test_rejects_a_push_event(self) -> None:
        environment = {**self.environment, "GITHUB_EVENT_NAME": "push"}
        with (
            patch.dict(contrib.os.environ, environment),
            self.assertRaisesRegex(contrib.ContribError, "GITHUB_EVENT_NAME"),
        ):
            contrib.validate_master_sync_checkout(self.repo)

    def test_non_forced_sync_command_is_exact(self) -> None:
        with patch.object(contrib, "run") as run_command:
            contrib.fast_forward_fork_master(self.repo)
        run_command.assert_called_once_with(
            (
                "gh",
                "repo",
                "sync",
                "kogeler/xpra",
                "--source",
                "Xpra-org/xpra",
                "--branch",
                "master",
            ),
            cwd=self.repo,
        )
        self.assertNotIn("--force", run_command.call_args.args[0])

    def sync_mocks(self, refs: list[str]):
        return (
            patch.object(contrib, "validate_master_sync_checkout"),
            patch.object(contrib, "current_branch", return_value="develop"),
            patch.object(contrib, "rev_parse", return_value="1" * 40),
            patch.object(contrib, "porcelain", return_value=""),
            patch.object(contrib, "live_remote_ref", side_effect=refs),
            patch.object(contrib, "require_fork_master_fast_forward"),
            patch.object(contrib, "fast_forward_fork_master"),
        )

    def test_fast_forward_relation_requires_fork_as_the_exact_merge_base(self) -> None:
        fork = "2" * 40
        upstream = "3" * 40
        response = {
            "ahead_by": 4,
            "base_commit": {"sha": fork},
            "behind_by": 0,
            "merge_base_commit": {"sha": fork},
            "status": "ahead",
        }
        completed = subprocess.CompletedProcess([], 0, json.dumps(response), "")
        with patch.object(contrib, "run", return_value=completed) as run_command:
            contrib.require_fork_master_fast_forward(self.repo, fork, upstream)
        run_command.assert_called_once_with(
            (
                "gh",
                "api",
                "--method",
                "GET",
                f"repos/{contrib.FORK_REPOSITORY}/compare/{fork}...{upstream}",
            ),
            cwd=self.repo,
        )

    def test_fast_forward_relation_rejects_an_ahead_fork(self) -> None:
        fork = "2" * 40
        upstream = "3" * 40
        response = {
            "ahead_by": 0,
            "base_commit": {"sha": upstream},
            "behind_by": 2,
            "merge_base_commit": {"sha": upstream},
            "status": "behind",
        }
        completed = subprocess.CompletedProcess([], 0, json.dumps(response), "")
        with (
            patch.object(contrib, "run", return_value=completed),
            self.assertRaisesRegex(contrib.ContribError, "owner review"),
        ):
            contrib.require_fork_master_fast_forward(self.repo, fork, upstream)

    def test_equal_master_is_a_noop(self) -> None:
        commit = "2" * 40
        validate, branch, head, status, live, relation, sync = self.sync_mocks(
            [commit, commit, commit, commit]
        )
        with (
            validate,
            branch,
            head,
            status,
            live,
            relation as relation_check,
            sync as sync_command,
        ):
            state = contrib.ci_master_sync(self.repo)
        self.assertFalse(state.updated)
        relation_check.assert_not_called()
        sync_command.assert_not_called()

    def test_stale_fork_is_synced_and_reverified(self) -> None:
        old = "2" * 40
        current = "3" * 40
        validate, branch, head, status, live, relation, sync = self.sync_mocks(
            [old, current, current, current]
        )
        with (
            validate,
            branch,
            head,
            status,
            live,
            relation as relation_check,
            sync as sync_command,
        ):
            state = contrib.ci_master_sync(self.repo)
        self.assertTrue(state.updated)
        self.assertEqual(state.fork_after, current)
        relation_check.assert_called_once_with(self.repo, old, current)
        sync_command.assert_called_once_with(self.repo)

    def test_post_sync_mismatch_fails(self) -> None:
        old = "2" * 40
        current = "3" * 40
        validate, branch, head, status, live, relation, sync = self.sync_mocks(
            [old, current, old, current]
        )
        with (
            validate,
            branch,
            head,
            status,
            live,
            relation,
            sync,
            self.assertRaisesRegex(contrib.ContribError, "does not match"),
        ):
            contrib.ci_master_sync(self.repo)

    def test_local_ref_change_fails(self) -> None:
        old = "2" * 40
        current = "3" * 40
        validate, branch, head, status, live, relation, sync = self.sync_mocks(
            [old, current, current, current]
        )
        with (
            validate,
            branch,
            head,
            status,
            live,
            relation,
            sync as sync_command,
            self.assertRaisesRegex(contrib.ContribError, "changed the develop checkout"),
        ):
            sync_command.side_effect = lambda _repo: command(
                "git", "branch", "unexpected", "HEAD", cwd=self.repo
            )
            contrib.ci_master_sync(self.repo)


class CiDebReleaseCheckoutTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        command("git", "init", "-q", "-b", "develop", str(self.repo))
        (self.repo / "tracked.txt").write_text("develop\n", encoding="utf-8")
        command("git", "add", "tracked.txt", cwd=self.repo)
        command(
            "git",
            "-c",
            "user.name=DEB Release Test",
            "-c",
            "user.email=deb-release@example.invalid",
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
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF": "refs/heads/package-candidate",
            "GITHUB_REPOSITORY": contrib.FORK_REPOSITORY,
            "GITHUB_SHA": self.head,
            "GITHUB_WORKFLOW_REF": (
                f"{contrib.FORK_REPOSITORY}/{contrib.DEB_RELEASE_WORKFLOW}"
                "@refs/heads/package-candidate"
            ),
            "GH_TOKEN": "test-token",
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_accepts_manual_branch_agnostic_release_checkout(self) -> None:
        with (
            patch.dict(contrib.os.environ, self.environment),
            patch.object(contrib.shutil, "which", return_value="/usr/bin/tool"),
        ):
            contrib.validate_deb_release_checkout(self.repo)

    def test_accepts_a_manually_selected_tag_checkout(self) -> None:
        github_ref = "refs/tags/package-candidate"
        environment = {
            **self.environment,
            "GITHUB_REF": github_ref,
            "GITHUB_WORKFLOW_REF": (
                f"{contrib.FORK_REPOSITORY}/{contrib.DEB_RELEASE_WORKFLOW}@{github_ref}"
            ),
        }
        with (
            patch.dict(contrib.os.environ, environment),
            patch.object(contrib.shutil, "which", return_value="/usr/bin/tool"),
        ):
            contrib.validate_deb_release_checkout(self.repo)

    def test_does_not_require_a_named_remote(self) -> None:
        command("git", "remote", "remove", "origin", cwd=self.repo)
        with (
            patch.dict(contrib.os.environ, self.environment),
            patch.object(contrib.shutil, "which", return_value="/usr/bin/tool"),
        ):
            contrib.validate_deb_release_checkout(self.repo)

    def test_rejects_push_event(self) -> None:
        environment = {**self.environment, "GITHUB_EVENT_NAME": "push"}
        with (
            patch.dict(contrib.os.environ, environment),
            patch.object(contrib.shutil, "which", return_value="/usr/bin/tool"),
            self.assertRaisesRegex(contrib.ContribError, "GITHUB_EVENT_NAME"),
        ):
            contrib.validate_deb_release_checkout(self.repo)


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

    def test_rejects_a_dirty_root_file_that_only_looks_allowed(self) -> None:
        (self.repo / "AGENTS.md.backup").write_text("not controlled\n", encoding="utf-8")
        with (
            patch.object(contrib, "verify_repo"),
            patch.object(contrib, "sync_repo", return_value=self.base),
            self.assertRaisesRegex(contrib.ContribError, "refuses host Xpra source changes"),
        ):
            contrib.isolated_start_check(self.repo)

    def test_rejects_multiple_fork_master_merge_bases(self) -> None:
        tree = command("git", "rev-parse", "HEAD^{tree}", cwd=self.repo)
        left = command(
            "git", "commit-tree", tree, "-p", self.base, "-m", "left", cwd=self.repo
        )
        right = command(
            "git", "commit-tree", tree, "-p", self.base, "-m", "right", cwd=self.repo
        )
        develop = command(
            "git",
            "commit-tree",
            tree,
            "-p",
            left,
            "-p",
            right,
            "-m",
            "develop",
            cwd=self.repo,
        )
        master = command(
            "git",
            "commit-tree",
            tree,
            "-p",
            right,
            "-p",
            left,
            "-m",
            "master",
            cwd=self.repo,
        )
        command("git", "update-ref", "refs/heads/develop", develop, cwd=self.repo)

        self.assertEqual(
            set(
                command(
                    "git", "merge-base", "--all", master, develop, cwd=self.repo
                ).splitlines()
            ),
            {left, right},
        )
        with (
            patch.object(contrib, "verify_repo"),
            patch.object(contrib, "sync_repo", return_value=master),
            self.assertRaisesRegex(contrib.ContribError, "single usable history boundary"),
        ):
            contrib.isolated_start_check(self.repo)

    def test_rejects_source_paths_changed_then_reverted_in_downstream_history(self) -> None:
        for value, message in (("VALUE = 2\n", "change source"), ("VALUE = 1\n", "revert source")):
            (self.repo / "source.py").write_text(value, encoding="utf-8")
            command("git", "add", "source.py", cwd=self.repo)
            command(
                "git",
                "-c",
                "user.name=Isolated Test",
                "-c",
                "user.email=isolated@example.invalid",
                "commit",
                "-q",
                "-m",
                message,
                cwd=self.repo,
            )
        self.assertEqual(
            command("git", "diff", "--name-only", f"{self.base}..HEAD", cwd=self.repo),
            "",
        )
        with (
            patch.object(contrib, "verify_repo"),
            patch.object(contrib, "sync_repo", return_value=self.base),
            self.assertRaisesRegex(contrib.ContribError, "outside the patch queue"),
        ):
            contrib.isolated_start_check(self.repo)


class CheckoutSourceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        command("git", "init", "-q", "-b", "master", str(self.repo))
        command("git", "config", "user.name", "Checkout Source Test", cwd=self.repo)
        command("git", "config", "user.email", "checkout@example.invalid", cwd=self.repo)
        (self.repo / ".gitignore").write_text("/.artifacts/\n", encoding="utf-8")
        workflow = self.repo / ".github" / "workflows" / "test.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text("name: Test\n", encoding="utf-8")
        (self.repo / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
        command("git", "add", ".", cwd=self.repo)
        command("git", "commit", "-q", "-m", "clean source", cwd=self.repo)
        self.base = command("git", "rev-parse", "HEAD", cwd=self.repo)
        command("git", "switch", "-q", "-c", "package-candidate", cwd=self.repo)
        contract = self.repo / "fork-maintenance" / "CONTRACT.md"
        contract.parent.mkdir()
        contract.write_text("downstream\n", encoding="utf-8")
        command("git", "add", "fork-maintenance/CONTRACT.md", cwd=self.repo)
        command("git", "commit", "-q", "-m", "downstream control", cwd=self.repo)
        self.checkout = command("git", "rev-parse", "HEAD", cwd=self.repo)
        command("git", "switch", "-q", "master", cwd=self.repo)
        (self.repo / "source.py").write_text("VALUE = 2\n", encoding="utf-8")
        command("git", "add", "source.py", cwd=self.repo)
        command("git", "commit", "-q", "-m", "later clean source", cwd=self.repo)
        self.master = command("git", "rev-parse", "HEAD", cwd=self.repo)
        command("git", "switch", "-q", "package-candidate", cwd=self.repo)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_finds_boundary_without_current_branch_name_or_remote(self) -> None:
        state = contrib.checkout_source_check(self.repo)
        self.assertEqual(state.head, self.checkout)
        self.assertEqual(state.source_commit, self.base)
        self.assertEqual(state.master_ref, "refs/heads/master")
        self.assertEqual(state.master_commit, self.master)

    def test_accepts_detached_head(self) -> None:
        command("git", "checkout", "-q", "--detach", self.checkout, cwd=self.repo)
        state = contrib.checkout_source_check(self.repo)
        self.assertEqual(state.head, self.checkout)
        self.assertEqual(state.source_commit, self.base)

    def test_rejects_committed_source_changes_after_the_boundary(self) -> None:
        (self.repo / "source.py").write_text("DOWNSTREAM = 1\n", encoding="utf-8")
        command("git", "add", "source.py", cwd=self.repo)
        command("git", "commit", "-q", "-m", "bad downstream source", cwd=self.repo)
        with self.assertRaisesRegex(contrib.ContribError, "outside the patch queue"):
            contrib.checkout_source_check(self.repo)

    def test_rejects_a_committed_root_file_that_only_looks_allowed(self) -> None:
        (self.repo / ".gitignore.evil").write_text("not controlled\n", encoding="utf-8")
        command("git", "add", ".gitignore.evil", cwd=self.repo)
        command("git", "commit", "-q", "-m", "bad lookalike", cwd=self.repo)
        with self.assertRaisesRegex(contrib.ContribError, "outside the patch queue"):
            contrib.checkout_source_check(self.repo)


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
            "GITHUB_WORKFLOW_REF": (
                f"{contrib.FORK_REPOSITORY}/{contrib.ACTIVE_FORK_WORKFLOW}"
                "@refs/heads/develop"
            ),
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

    def test_rejects_a_different_workflow(self) -> None:
        environment = {
            **self.environment,
            "GITHUB_WORKFLOW_REF": (
                f"{contrib.FORK_REPOSITORY}/{contrib.DEB_RELEASE_WORKFLOW}"
                "@refs/heads/develop"
            ),
        }
        with (
            patch.dict(contrib.os.environ, environment),
            self.assertRaisesRegex(contrib.ContribError, "GITHUB_WORKFLOW_REF"),
        ):
            contrib.validate_ci_checkout(self.repo)

    def test_rejects_a_dirty_hosted_checkout(self) -> None:
        control = self.repo / "fork-maintenance"
        control.mkdir()
        (control / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")
        with (
            patch.dict(contrib.os.environ, self.environment),
            self.assertRaisesRegex(contrib.ContribError, "repository has local changes"),
        ):
            contrib.validate_ci_checkout(self.repo)


class CiPrepareTest(unittest.TestCase):
    def test_root_make_does_not_recompute_the_validated_source_boundary(self) -> None:
        makefile = (contrib.AUTOMATION_ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertNotIn("merge-base", makefile)
        recipe = makefile.split("ci-upstream-tests:\n", 1)[1].split(
            "\nci-deb-release:", 1
        )[0]
        self.assertEqual(recipe.count("$(CONTRIB) ci-prepare"), 1)
        self.assertIn("sed -n 's/^source_commit=//p'", recipe)
        self.assertIn('SOURCE_COMMIT="$$source_commit"', recipe)

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

    def test_deb_prepare_uses_the_branch_agnostic_source_boundary(self) -> None:
        repo = Path("/tmp/xpra-fork")
        state = contrib.CheckoutSourceState(
            head="1" * 40,
            source_commit="2" * 40,
            master_ref="refs/remotes/example/master",
            master_commit="3" * 40,
            worktree_status="",
        )
        with (
            patch.object(contrib, "validate_deb_release_checkout") as validate,
            patch.object(contrib, "checkout_source_check", return_value=state) as source,
            patch.object(contrib, "ci_start_check") as old_start,
        ):
            self.assertEqual(contrib.ci_deb_prepare(repo), state)

        validate.assert_called_once_with(repo)
        source.assert_called_once_with(repo)
        old_start.assert_not_called()

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

    def test_start_rejects_multiple_embedded_merge_bases(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            command("git", "init", "-q", "-b", "develop", str(repo))
            command("git", "config", "user.name", "CI Start Test", cwd=repo)
            command("git", "config", "user.email", "ci-start@example.invalid", cwd=repo)
            (repo / ".gitignore").write_text("/.artifacts/\n", encoding="utf-8")
            command("git", "add", ".gitignore", cwd=repo)
            command("git", "commit", "-q", "-m", "base", cwd=repo)
            base = command("git", "rev-parse", "HEAD", cwd=repo)
            tree = command("git", "rev-parse", "HEAD^{tree}", cwd=repo)

            left = command("git", "commit-tree", tree, "-p", base, "-m", "left", cwd=repo)
            right = command(
                "git", "commit-tree", tree, "-p", base, "-m", "right", cwd=repo
            )
            develop = command(
                "git",
                "commit-tree",
                tree,
                "-p",
                left,
                "-p",
                right,
                "-m",
                "develop",
                cwd=repo,
            )
            master = command(
                "git",
                "commit-tree",
                tree,
                "-p",
                right,
                "-p",
                left,
                "-m",
                "master",
                cwd=repo,
            )
            command("git", "update-ref", "refs/heads/develop", develop, cwd=repo)
            command(
                "git", "update-ref", "refs/remotes/origin/master", master, cwd=repo
            )

            self.assertEqual(
                set(command("git", "merge-base", "--all", master, develop, cwd=repo).splitlines()),
                {left, right},
            )
            with (
                patch.object(contrib, "verify_repo"),
                patch.object(contrib, "artifact_boundary_check"),
                self.assertRaisesRegex(contrib.ContribError, "single usable history boundary"),
            ):
                contrib.ci_start_check(repo)


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
        for path, semantics in (
            (contrib.ACTIVE_FORK_WORKFLOW, contrib.fork_workflow_semantics()),
            (contrib.DEB_RELEASE_WORKFLOW, contrib.deb_release_workflow_semantics()),
            (contrib.MASTER_SYNC_WORKFLOW, contrib.master_sync_workflow_semantics()),
        ):
            workflow = list(semantics)
            uses = f"        uses: actions/checkout@{contrib.CHECKOUT_ACTION_SHA}"
            workflow[workflow.index(uses)] += f"  # {contrib.CHECKOUT_ACTION_VERSION}"
            (self.repo / path).write_text(
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
        self.assertEqual(
            result["active_workflows"],
            contrib.ACTIVE_FORK_WORKFLOWS,
        )
        self.assertEqual(result["checkout_action_version"], "v7.0.1")

    def test_rejects_a_canonical_workflow_left_active(self) -> None:
        (self.repo / ".github" / "workflows" / "build.yml").write_text(
            "name: Build\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(contrib.ContribError, "approved fork workflows"):
            contrib.ci_layout_check(self.repo, self.base)

    def test_rejects_a_modified_disabled_workflow(self) -> None:
        (self.repo / ".github" / "upstream-workflows" / "test.yaml").write_text(
            "name: Changed\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(contrib.ContribError, "differs from fork master"):
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

    def test_rejects_a_different_master_sync_schedule(self) -> None:
        workflow = self.repo / contrib.MASTER_SYNC_WORKFLOW
        workflow.write_text(
            workflow.read_text(encoding="utf-8").replace(
                'cron: "37 */12 * * *"',
                'cron: "37 */6 * * *"',
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(contrib.ContribError, "master sync workflow"):
            contrib.ci_layout_check(self.repo, self.base)

    def test_rejects_release_logic_outside_the_make_target(self) -> None:
        workflow = self.repo / contrib.DEB_RELEASE_WORKFLOW
        workflow.write_text(
            workflow.read_text(encoding="utf-8").replace(
                "        run: make -C fork-maintenance ci-deb-release\n",
                "        run: gh release create unreviewed\n",
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(contrib.ContribError, "DEB release workflow"):
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

    def case_update_candidate(self) -> tuple[bytes, str]:
        patch_bytes = self.case.patch.read_bytes().replace(b"+new\n", b"+newer\n")
        manifest = contrib.updated_manifest_text(
            self.case.manifest.read_text(encoding="utf-8"),
            digest=hashlib.sha256(patch_bytes).hexdigest(),
            paths=self.case.paths,
            draft=False,
        )
        return patch_bytes, manifest

    def leave_case_update_transaction(self) -> contrib.CaseUpdateTransaction:
        patch_bytes, manifest = self.case_update_candidate()
        with (
            patch.object(
                contrib,
                "complete_case_update_transaction",
                side_effect=RuntimeError("simulated crash"),
            ),
            self.assertRaisesRegex(RuntimeError, "simulated crash"),
        ):
            contrib.atomic_update_case_files(
                self.repo,
                self.case,
                patch_bytes,
                manifest,
                expected_patch_bytes=self.case.patch.read_bytes(),
                expected_manifest_bytes=self.case.manifest.read_bytes(),
            )
        return contrib.validate_case_update_transaction(self.repo, self.case.slug)

    def assert_no_tracked_case_staging(self, directory: Path | None = None) -> None:
        selected = self.case_dir if directory is None else directory
        self.assertFalse(any(path.name.startswith(".") for path in selected.iterdir()))

    def test_recovery_interfaces_are_visible_in_make_help(self) -> None:
        output = command(
            "make",
            "-s",
            "-C",
            str(contrib.AUTOMATION_ROOT),
            "help",
        )
        self.assertIn("case-recover CASE=short-behavior-name", output)
        self.assertIn(
            "recover exact interrupted create, update, or update-removal state",
            output,
        )
        self.assertIn("workspace-recover WORKSPACE=name", output)
        self.assertIn(
            "interrupted create, direct removal, or fingerprint cleanup state",
            output,
        )

    def test_workspace_entrypoints_wait_for_the_retained_lifecycle_lock(self) -> None:
        sentinel = object()
        entrypoints = (
            (
                "_create_workspace_locked",
                lambda: contrib.create_workspace(
                    self.repo,
                    "locked-create-01",
                    "cases/sample-case",
                    "patched",
                ),
            ),
            (
                "_stage_workspace_locked",
                lambda: contrib.stage_workspace(self.repo, "locked-stage-01"),
            ),
            (
                "_update_case_from_workspace_locked",
                lambda: contrib.update_case_from_workspace(self.repo, "locked-update-01"),
            ),
            (
                "_remove_workspace_locked",
                lambda: contrib.remove_workspace(self.repo, "locked-remove-01"),
            ),
            (
                "_recover_workspace_state_locked",
                lambda: contrib.recover_workspace_state(self.repo, "locked-recover-01"),
            ),
            (
                "_finalized_workspace_fingerprint_locked",
                lambda: contrib.finalized_workspace_fingerprint(
                    self.repo,
                    "locked-finalize-01",
                ),
            ),
            (
                "_recover_case_creation_locked",
                lambda: contrib.recover_case_creation(self.repo, "locked-case"),
            ),
        )
        root = contrib.prepare_workspace_root(self.repo)
        lock = contrib.workspace_lifecycle_lock_path(self.repo)

        def invoke_action(
            action: Callable[[], object],
            values: list[object],
            errors: list[BaseException],
            completed: threading.Event,
        ) -> None:
            try:
                values.append(action())
            except BaseException as error:  # noqa: BLE001 - thread handoff.
                errors.append(error)
            finally:
                completed.set()

        for implementation, action in entrypoints:
            with self.subTest(implementation=implementation):
                descriptor = os.open(
                    lock,
                    os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                )
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                completed = threading.Event()
                values: list[object] = []
                errors: list[BaseException] = []

                with patch.object(contrib, implementation, return_value=sentinel):
                    worker = threading.Thread(
                        target=invoke_action,
                        args=(action, values, errors, completed),
                    )
                    worker.start()
                    try:
                        self.assertFalse(completed.wait(0.1))
                    finally:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                        os.close(descriptor)
                    worker.join(5)
                self.assertFalse(worker.is_alive())
                if errors:
                    raise errors[0]
                self.assertEqual(values, [sentinel])
        self.assertEqual(root, lock.parent)

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
        verification_destinations: list[Path] = []
        archive_tree = contrib.archive_tree

        def observe_verification(repo: Path, revision: str, destination: Path) -> None:
            verification_destinations.append(destination)
            archive_tree(repo, revision, destination)

        with case_mock, patch.object(
            contrib,
            "archive_tree",
            side_effect=observe_verification,
        ):
            names = contrib.stage_workspace(self.repo, "update-01")
            updated = contrib.update_case_from_workspace(self.repo, "update-01")
        self.assertEqual(names, ("target.txt", "tests/sample_test.py"))
        self.assertIn("+newer", updated.patch.read_text(encoding="utf-8"))
        self.assertEqual((self.repo / "target.txt").read_text(), "old\n")
        self.assertEqual(len(verification_destinations), 2)
        self.assertTrue(
            all(
                destination.is_relative_to(contrib.case_updates_root(self.repo))
                for destination in verification_destinations
            )
        )
        self.assertFalse(
            any(path.name.startswith(".verify-") for path in workspace.directory.iterdir())
        )
        first_resolution = json.loads(
            contrib.workspace_resolution_path(workspace.directory).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(first_resolution["patches"][0]["patch_sha256"], updated.patch_sha256)

        (workspace.source / "target.txt").write_text("newest\n", encoding="utf-8")
        with patch.object(contrib, "get_case", return_value=updated):
            self.assertEqual(
                contrib.stage_workspace(self.repo, "update-01"),
                ("target.txt", "tests/sample_test.py"),
            )
            updated_again = contrib.update_case_from_workspace(self.repo, "update-01")

        self.assertIn("+newest", updated_again.patch.read_text(encoding="utf-8"))
        repeated_workspace = contrib.load_workspace(self.repo, "update-01")
        repeated_resolution = json.loads(
            contrib.workspace_resolution_path(repeated_workspace.directory).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            repeated_resolution["patches"][0]["patch_sha256"],
            updated_again.patch_sha256,
        )
        removed = contrib.remove_workspace(self.repo, "update-01")
        self.assertFalse(removed.exists())

    def test_workspace_remove_retries_exact_staging_after_interruption(self) -> None:
        start, selected, resolution, _case = self.mocks()
        name = "remove-retry-01"
        with start, selected, resolution:
            contrib.create_workspace(
                self.repo,
                name,
                "cases/sample-case",
                "patched",
            )
        target, staging, marker = contrib.workspace_remove_paths(self.repo, name)

        with (
            patch.object(contrib.shutil, "rmtree", side_effect=RuntimeError("crash")),
            self.assertRaisesRegex(RuntimeError, "crash"),
        ):
            contrib.remove_workspace(self.repo, name)

        self.assertFalse(target.exists())
        self.assertTrue(staging.is_dir())
        self.assertTrue(marker.is_file())
        self.assertEqual(contrib.remove_workspace(self.repo, name), target)
        self.assertFalse(staging.exists())
        self.assertFalse(marker.exists())

    def test_workspace_create_refuses_an_interrupted_direct_removal(self) -> None:
        start, selected, resolution, _case = self.mocks()
        name = "remove-before-recreate-01"
        with start, selected, resolution:
            contrib.create_workspace(
                self.repo,
                name,
                "cases/sample-case",
                "patched",
            )
        target, staging, marker = contrib.workspace_remove_paths(self.repo, name)
        contrib.publish_workspace_remove_transaction(self.repo, name)
        contrib.container_payload.rename_no_replace(target, staging)

        with (
            start,
            selected,
            resolution,
            self.assertRaisesRegex(contrib.ContribError, "workspace-recover"),
        ):
            contrib.create_workspace(
                self.repo,
                name,
                "cases/sample-case",
                "patched",
            )

        self.assertTrue(staging.is_dir())
        self.assertTrue(marker.is_file())

    def test_workspace_recover_resumes_a_partially_deleted_staging_inode(self) -> None:
        start, selected, resolution, _case = self.mocks()
        name = "remove-partial-01"
        with start, selected, resolution:
            contrib.create_workspace(
                self.repo,
                name,
                "cases/sample-case",
                "patched",
            )
        target, staging, marker = contrib.workspace_remove_paths(self.repo, name)
        contrib.publish_workspace_remove_transaction(self.repo, name)
        contrib.container_payload.rename_no_replace(target, staging)
        (staging / "workspace.json").unlink()

        recovered = contrib.recover_workspace_state(self.repo, name)

        self.assertEqual(recovered, (staging, marker))
        self.assertFalse(target.exists())
        self.assertFalse(staging.exists())
        self.assertFalse(marker.exists())

    def test_workspace_remove_never_overwrites_racing_staging(self) -> None:
        start, selected, resolution, _case = self.mocks()
        name = "remove-race-01"
        with start, selected, resolution:
            contrib.create_workspace(
                self.repo,
                name,
                "cases/sample-case",
                "patched",
            )
        target, staging, marker = contrib.workspace_remove_paths(self.repo, name)
        contrib.publish_workspace_remove_transaction(self.repo, name)
        staging.mkdir(mode=0o700)
        sentinel = staging / "sentinel"
        sentinel.write_text("do not overwrite\n", encoding="utf-8")

        with self.assertRaisesRegex(contrib.ContribError, "both target and staging"):
            contrib.remove_workspace(self.repo, name)

        self.assertTrue(target.is_dir())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "do not overwrite\n")
        self.assertTrue(marker.is_file())

    def test_workspace_remove_rejects_target_tampering_after_publication(self) -> None:
        start, selected, resolution, _case = self.mocks()
        name = "remove-tamper-01"
        with start, selected, resolution:
            contrib.create_workspace(
                self.repo,
                name,
                "cases/sample-case",
                "patched",
            )
        target, _staging, marker = contrib.workspace_remove_paths(self.repo, name)
        contrib.publish_workspace_remove_transaction(self.repo, name)
        tampered = target / "tampered-after-publication"
        tampered.write_text("preserve for review\n", encoding="utf-8")

        with self.assertRaisesRegex(contrib.ContribError, "changed after publication"):
            contrib.remove_workspace(self.repo, name)

        self.assertTrue(target.is_dir())
        self.assertTrue(tampered.is_file())
        self.assertTrue(marker.is_file())

    def test_workspace_remove_owns_legacy_verification_remnants(self) -> None:
        start, selected, resolution, _case = self.mocks()
        name = "remove-verify-01"
        with start, selected, resolution:
            workspace = contrib.create_workspace(
                self.repo,
                name,
                "cases/sample-case",
                "patched",
            )
        verification = workspace.directory / ".verify-crash"
        verification.mkdir(mode=0o700)
        (verification / "partial").write_text("stale\n", encoding="utf-8")

        target = contrib.remove_workspace(self.repo, name)

        self.assertFalse(target.exists())

    def test_workspace_remove_preserves_a_pending_case_update_workspace(self) -> None:
        start, selected, resolution, _case = self.mocks()
        name = "remove-case-bound-01"
        with start, selected, resolution:
            workspace = contrib.create_workspace(
                self.repo,
                name,
                "cases/sample-case",
                "patched",
            )
        _transaction, owner = contrib.case_update_paths(self.repo, self.case.slug)
        contrib.publish_private_json(
            owner,
            contrib.case_update_owner_payload(
                self.repo,
                self.case.slug,
                name,
                "12345678-1234-4abc-8def-123456789abc",
            ),
            "test case update owner",
        )

        with self.assertRaisesRegex(contrib.ContribError, "case-recover"):
            contrib.remove_workspace(self.repo, name)

        self.assertTrue(workspace.directory.is_dir())
        self.assertTrue(owner.is_file())
        self.assertEqual(
            contrib.recover_case_creation(self.repo, self.case.slug),
            (owner,),
        )
        self.assertEqual(
            contrib.remove_workspace(self.repo, name),
            workspace.directory,
        )
        self.assertFalse(workspace.directory.exists())

    def test_case_update_recovery_completes_a_transaction_before_first_publish(self) -> None:
        transaction = self.leave_case_update_transaction()
        self.assertTrue(all(contrib.case_update_target_state(entry) == "old" for entry in transaction.entries))
        self.assert_no_tracked_case_staging()

        recovered = contrib.recover_case_creation(self.repo, self.case.slug)

        staging, removal = contrib.case_update_removal_paths(
            self.repo,
            self.case.slug,
        )
        self.assertEqual(recovered, (staging, removal, transaction.owner))
        self.assertIn("+newer", self.case.patch.read_text(encoding="utf-8"))
        self.assertFalse(transaction.directory.exists())
        self.assertFalse(transaction.owner.exists())
        self.assert_no_tracked_case_staging()

    def test_case_update_recovery_resumes_partial_transaction_rmtree(self) -> None:
        transaction = self.leave_case_update_transaction()
        staging, removal = contrib.case_update_removal_paths(
            self.repo,
            self.case.slug,
        )
        original_rmtree = contrib.shutil.rmtree
        interrupted = False

        def partially_remove(path: Path, *args: object, **kwargs: object) -> None:
            nonlocal interrupted
            candidate = Path(path)
            if candidate == staging and not interrupted:
                interrupted = True
                (staging / "transaction.json").unlink()
                raise RuntimeError("simulated case-update rmtree crash")
            original_rmtree(path, *args, **kwargs)

        with (
            patch.object(contrib.shutil, "rmtree", side_effect=partially_remove),
            self.assertRaisesRegex(RuntimeError, "case-update rmtree crash"),
        ):
            contrib.recover_case_creation(self.repo, self.case.slug)

        self.assertFalse(transaction.directory.exists())
        self.assertTrue(staging.is_dir())
        self.assertTrue(removal.is_file())
        self.assertTrue(transaction.owner.is_file())
        self.assertEqual(
            contrib.recover_case_creation(self.repo, self.case.slug),
            (staging, removal, transaction.owner),
        )
        self.assertIn("+newer", self.case.patch.read_text(encoding="utf-8"))
        self.assertFalse(staging.exists())
        self.assertFalse(removal.exists())
        self.assertFalse(transaction.owner.exists())
        self.assert_no_tracked_case_staging()

    def test_case_update_remove_never_overwrites_racing_staging(self) -> None:
        transaction = self.leave_case_update_transaction()
        for entry in transaction.entries:
            contrib.publish_case_update_entry(transaction, entry)
        contrib.publish_case_update_remove_transaction(
            self.repo,
            self.case.slug,
            "complete",
        )
        staging, removal = contrib.case_update_removal_paths(
            self.repo,
            self.case.slug,
        )
        staging.mkdir(mode=0o700)
        sentinel = staging / "sentinel"
        sentinel.write_text("do not overwrite\n", encoding="utf-8")

        with self.assertRaisesRegex(
            contrib.ContribError,
            "both transaction and staging",
        ):
            contrib.recover_case_creation(self.repo, self.case.slug)

        self.assertTrue(transaction.directory.is_dir())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "do not overwrite\n")
        self.assertTrue(removal.is_file())
        self.assertTrue(transaction.owner.is_file())

    def test_case_update_remove_preserves_changed_published_target(self) -> None:
        transaction = self.leave_case_update_transaction()
        for entry in transaction.entries:
            contrib.publish_case_update_entry(transaction, entry)
        contrib.publish_case_update_remove_transaction(
            self.repo,
            self.case.slug,
            "complete",
        )
        _staging, removal = contrib.case_update_removal_paths(
            self.repo,
            self.case.slug,
        )
        self.case.patch.write_bytes(self.case.patch.read_bytes() + b"tampered\n")

        with self.assertRaisesRegex(contrib.ContribError, "removal target changed"):
            contrib.recover_case_creation(self.repo, self.case.slug)

        self.assertTrue(transaction.directory.is_dir())
        self.assertTrue(removal.is_file())
        self.assertTrue(transaction.owner.is_file())

    def test_case_update_remove_finishes_a_phase_left_after_owner_unlink(self) -> None:
        transaction = self.leave_case_update_transaction()
        for entry in transaction.entries:
            contrib.publish_case_update_entry(transaction, entry)
        contrib.publish_case_update_remove_transaction(
            self.repo,
            self.case.slug,
            "complete",
        )
        staging, removal = contrib.case_update_removal_paths(
            self.repo,
            self.case.slug,
        )
        contrib.container_payload.rename_no_replace(transaction.directory, staging)
        contrib.shutil.rmtree(staging)
        transaction.owner.unlink()

        self.assertEqual(
            contrib.recover_case_creation(self.repo, self.case.slug),
            (removal,),
        )
        self.assertFalse(removal.exists())
        self.assertIn("+newer", self.case.patch.read_text(encoding="utf-8"))

    def test_workspace_update_recovery_refreshes_regular_workspace_metadata(self) -> None:
        start, selected, resolution, case_mock = self.mocks()
        with start, selected, resolution:
            workspace = contrib.create_workspace(
                self.repo,
                "repeat-crash-01",
                "cases/sample-case",
                "patched",
            )
        (workspace.source / "target.txt").write_text("newer\n", encoding="utf-8")
        with case_mock:
            contrib.stage_workspace(self.repo, workspace.name)
            with (
                patch.object(
                    contrib,
                    "complete_case_update_transaction",
                    side_effect=RuntimeError("simulated workspace export crash"),
                ),
                self.assertRaisesRegex(RuntimeError, "simulated workspace export crash"),
            ):
                contrib.update_case_from_workspace(self.repo, workspace.name)

        transaction = contrib.validate_case_update_transaction(
            self.repo,
            self.case.slug,
        )
        self.assertEqual(
            tuple(entry.key for entry in transaction.entries),
            (
                "case-patch",
                "case-manifest",
                "workspace-resolution",
                "workspace-metadata",
            ),
        )

        contrib.recover_case_creation(self.repo, self.case.slug)

        updated = contrib.load_case(self.case.directory)
        recovered_workspace = contrib.load_workspace(self.repo, workspace.name)
        recovered_resolution = json.loads(
            contrib.workspace_resolution_path(recovered_workspace.directory).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            recovered_resolution["patches"][0]["patch_sha256"],
            updated.patch_sha256,
        )
        self.assertFalse(transaction.directory.exists())
        self.assertFalse(transaction.owner.exists())

    def test_case_update_recovery_reuses_a_valid_publication_remnant(self) -> None:
        transaction = self.leave_case_update_transaction()
        entry = transaction.entries[0]
        publication = transaction.directory / f".{entry.key}.publish"
        contrib.background_job.publish_bytes(publication, entry.new_payload.read_bytes())
        publication.chmod(entry.new_mode)

        contrib.recover_case_creation(self.repo, self.case.slug)

        self.assertIn("+newer", self.case.patch.read_text(encoding="utf-8"))
        self.assertFalse(publication.exists())
        self.assert_no_tracked_case_staging()

    def test_case_update_treats_an_unchanged_entry_as_complete(self) -> None:
        transaction = self.leave_case_update_transaction()
        original = transaction.entries[0]
        unchanged = contrib.CaseUpdateEntry(
            original.key,
            original.target,
            original.old_payload,
            original.old_payload,
            original.old_sha256,
            original.old_sha256,
            original.old_mode,
            original.old_mode,
        )

        self.assertEqual(contrib.case_update_target_state(unchanged), "new")
        contrib.publish_case_update_entry(transaction, unchanged)
        self.assertFalse(
            (transaction.directory / f".{unchanged.key}.publish").exists()
        )
        self.assert_no_tracked_case_staging()

    def test_case_update_recovery_completes_a_mixed_patch_manifest_pair(self) -> None:
        transaction = self.leave_case_update_transaction()
        contrib.publish_case_update_entry(transaction, transaction.entries[0])
        self.assertEqual(contrib.case_update_target_state(transaction.entries[0]), "new")
        self.assertEqual(contrib.case_update_target_state(transaction.entries[1]), "old")
        self.assert_no_tracked_case_staging()

        contrib.recover_case_creation(self.repo, self.case.slug)

        updated = contrib.load_case(self.case_dir)
        self.assertIn("+newer", updated.patch.read_text(encoding="utf-8"))
        self.assert_no_tracked_case_staging()

    def test_case_update_recovery_finishes_cleanup_after_all_targets_publish(self) -> None:
        transaction = self.leave_case_update_transaction()
        for entry in transaction.entries:
            contrib.publish_case_update_entry(transaction, entry)
        self.assertTrue(all(contrib.case_update_target_state(entry) == "new" for entry in transaction.entries))

        contrib.recover_case_creation(self.repo, self.case.slug)

        self.assertFalse(transaction.directory.exists())
        self.assertFalse(transaction.owner.exists())
        self.assert_no_tracked_case_staging()

    def test_case_update_recovery_aborts_owner_only_and_partial_preparation(self) -> None:
        transaction, owner = contrib.case_update_paths(self.repo, self.case.slug)
        contrib.publish_private_json(
            owner,
            contrib.case_update_owner_payload(
                self.repo,
                self.case.slug,
                "",
                "12345678-1234-4abc-8def-123456789abc",
            ),
            "test case update owner",
        )
        self.assertEqual(contrib.recover_case_creation(self.repo, self.case.slug), (owner,))

        contrib.publish_private_json(
            owner,
            contrib.case_update_owner_payload(
                self.repo,
                self.case.slug,
                "",
                "12345678-1234-4abc-8def-123456789abc",
            ),
            "test case update owner",
        )
        transaction.mkdir(mode=0o700)
        contrib.background_job.publish_bytes(transaction / "case-patch.old", b"partial")
        staging, removal = contrib.case_update_removal_paths(
            self.repo,
            self.case.slug,
        )

        self.assertEqual(
            contrib.recover_case_creation(self.repo, self.case.slug),
            (staging, removal, owner),
        )
        self.assertFalse(transaction.exists())
        self.assertFalse(owner.exists())
        self.assert_no_tracked_case_staging()

    def test_case_update_recovery_refuses_tampered_marker_payload_and_target(self) -> None:
        transaction = self.leave_case_update_transaction()
        marker = transaction.directory / "transaction.json"
        marker_payload = json.loads(marker.read_text(encoding="utf-8"))
        marker_payload["entries"][0]["target"] = str(self.repo / "outside")
        marker.write_text(json.dumps(marker_payload) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(contrib.ContribError, "escaped its exact target"):
            contrib.recover_case_creation(self.repo, self.case.slug)
        self.assertTrue(transaction.directory.exists())
        self.assert_no_tracked_case_staging()

        marker_payload["entries"][0]["target"] = str(self.case.patch)
        marker.write_text(json.dumps(marker_payload) + "\n", encoding="utf-8")
        transaction.entries[0].new_payload.write_bytes(b"tampered")
        with self.assertRaisesRegex(contrib.ContribError, "payload digest"):
            contrib.recover_case_creation(self.repo, self.case.slug)
        self.assertTrue(transaction.directory.exists())

        transaction.entries[0].new_payload.write_bytes(
            self.case_update_candidate()[0]
        )
        self.case.patch.write_bytes(b"neither old nor new\n")
        with self.assertRaisesRegex(contrib.ContribError, "neither its exact old nor new"):
            contrib.recover_case_creation(self.repo, self.case.slug)
        self.assertTrue(transaction.directory.exists())
        self.assert_no_tracked_case_staging()

    def test_case_update_recovery_refuses_unknown_markerless_staging(self) -> None:
        transaction, owner = contrib.case_update_paths(self.repo, self.case.slug)
        contrib.publish_private_json(
            owner,
            contrib.case_update_owner_payload(
                self.repo,
                self.case.slug,
                "",
                "12345678-1234-4abc-8def-123456789abc",
            ),
            "test case update owner",
        )
        transaction.mkdir(mode=0o700)
        (transaction / "unknown").write_text("do not delete\n", encoding="utf-8")
        (transaction / "unknown").chmod(0o600)

        with self.assertRaisesRegex(contrib.ContribError, "unexpected staging"):
            contrib.recover_case_creation(self.repo, self.case.slug)

        self.assertTrue(transaction.exists())
        self.assertTrue(owner.exists())
        self.assert_no_tracked_case_staging()

    def test_case_update_refuses_to_overwrite_a_changed_original_pair(self) -> None:
        patch_bytes, manifest = self.case_update_candidate()
        expected_patch = self.case.patch.read_bytes()
        expected_manifest = self.case.manifest.read_bytes()
        self.case.manifest.write_bytes(expected_manifest + b"\n")

        with self.assertRaisesRegex(contrib.ContribError, "case changed"):
            contrib.atomic_update_case_files(
                self.repo,
                self.case,
                patch_bytes,
                manifest,
                expected_patch_bytes=expected_patch,
                expected_manifest_bytes=expected_manifest,
            )

        transaction, owner = contrib.case_update_paths(self.repo, self.case.slug)
        self.assertFalse(transaction.exists())
        self.assertFalse(owner.exists())
        self.assertEqual(self.case.manifest.read_bytes(), expected_manifest + b"\n")
        self.assert_no_tracked_case_staging()

    def test_case_update_requires_one_filesystem_before_publication(self) -> None:
        transaction_root = Mock()
        transaction_root.stat.return_value = SimpleNamespace(st_dev=1)
        target = Mock()
        target.parent.stat.return_value = SimpleNamespace(st_dev=2)

        with self.assertRaisesRegex(contrib.ContribError, "share one filesystem"):
            contrib.require_case_update_filesystem(
                transaction_root,
                (("case-patch", target),),
            )

    def test_case_update_validates_the_complete_new_pair_before_mutation(self) -> None:
        patch_bytes, manifest = self.case_update_candidate()
        invalid_manifest = manifest.replace(
            'list = ["unit.sample_test"]',
            "list = []",
        )
        expected_patch = self.case.patch.read_bytes()
        expected_manifest = self.case.manifest.read_bytes()

        with self.assertRaisesRegex(contrib.ContribError, "tests.list"):
            contrib.atomic_update_case_files(
                self.repo,
                self.case,
                patch_bytes,
                invalid_manifest,
                expected_patch_bytes=expected_patch,
                expected_manifest_bytes=expected_manifest,
            )

        transaction, owner = contrib.case_update_paths(self.repo, self.case.slug)
        self.assertFalse(transaction.exists())
        self.assertFalse(owner.exists())
        self.assertEqual(self.case.patch.read_bytes(), expected_patch)
        self.assertEqual(self.case.manifest.read_bytes(), expected_manifest)
        self.assert_no_tracked_case_staging()

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
            "source_commit": self.base,
            "selection": "cases/draft-case",
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

    def test_draft_recovery_completes_case_and_workspace_as_one_transaction(self) -> None:
        automation_root = self.repo / "fork-maintenance"
        cases_root = automation_root / "cases"
        draft_dir = cases_root / "draft-recovery"
        draft_dir.mkdir(parents=True)
        (draft_dir / "README.md").write_text("# Draft recovery\n", encoding="utf-8")
        (draft_dir / "fix.patch").write_bytes(b"")
        (draft_dir / "case.toml").write_text(
            """schema = 1
draft = true
slug = "draft-recovery"
kind = "production"
title = "Draft recovery"
commit_subject = "Draft recovery"
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
            "source_commit": self.base,
            "selection": "cases/draft-recovery",
            "selection_sha256": "5" * 64,
            "resolution_sha256": "6" * 64,
        }
        with (
            patch.object(contrib, "AUTOMATION_ROOT", automation_root),
            patch.object(contrib, "CASES_ROOT", cases_root),
            patch.object(contrib, "isolated_start_check", return_value=state),
        ):
            workspace = contrib.create_workspace(
                self.repo,
                "draft-recovery-01",
                "cases/draft-recovery",
                "clean",
            )
            (workspace.source / "target.txt").write_text("recovered\n", encoding="utf-8")
            contrib.stage_workspace(self.repo, workspace.name)
            with (
                patch.object(
                    contrib,
                    "selection_resolution",
                    return_value=final_resolution,
                ),
                patch.object(
                    contrib,
                    "complete_case_update_transaction",
                    side_effect=RuntimeError("simulated draft crash"),
                ),
                self.assertRaisesRegex(RuntimeError, "simulated draft crash"),
            ):
                contrib.update_case_from_workspace(self.repo, workspace.name)

        transaction = contrib.validate_case_update_transaction(
            self.repo,
            "draft-recovery",
        )
        self.assertEqual(
            tuple(entry.key for entry in transaction.entries),
            (
                "case-patch",
                "case-manifest",
                "workspace-resolution",
                "workspace-metadata",
            ),
        )
        for entry in transaction.entries[:3]:
            contrib.publish_case_update_entry(transaction, entry)
        self.assertEqual(contrib.case_update_target_state(transaction.entries[2]), "new")
        self.assertEqual(contrib.case_update_target_state(transaction.entries[3]), "old")
        self.assert_no_tracked_case_staging(draft_dir)

        contrib.recover_case_creation(self.repo, "draft-recovery")

        updated = contrib.load_case(draft_dir)
        recovered_workspace = contrib.load_workspace(
            self.repo,
            workspace.name,
            require_host_identity=False,
        )
        self.assertEqual(updated.paths, ("target.txt",))
        self.assertEqual(recovered_workspace.patch_mode, "patched")
        self.assertEqual(recovered_workspace.selection_sha256, "5" * 64)
        self.assertFalse(transaction.directory.exists())
        self.assertFalse(transaction.owner.exists())
        self.assert_no_tracked_case_staging(draft_dir)

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
        workspace = contrib.load_workspace(self.repo, "audit-finalized-01")
        self.assertFalse(
            any(
                path.name.startswith(".cycle-clean-index-")
                for path in workspace.directory.iterdir()
            )
        )

    def test_workspace_recovery_removes_exact_marker_backed_creation_partial(self) -> None:
        name = "lifecycle"
        target, partial, marker = contrib.workspace_create_paths(self.repo, name)
        contrib.publish_private_json(
            marker,
            {
                "kind": "workspace-create",
                "name": name,
                "operation_id": "12345678-1234-4abc-8def-123456789abc",
                "owner": contrib.WORKSPACE_CREATE_OWNER,
                "partial": str(partial),
                "schema": 1,
                "target": str(target),
            },
            "test workspace creation owner",
        )
        partial.mkdir(mode=0o700)
        (partial / "incomplete").write_text("partial\n", encoding="utf-8")

        recovered = contrib.recover_workspace_state(self.repo, name)

        self.assertEqual(recovered, (partial, marker))
        self.assertFalse(partial.exists())
        self.assertFalse(marker.exists())
        self.assertFalse(target.exists())

    def test_workspace_recovery_refuses_an_unowned_creation_partial(self) -> None:
        name = "audit-unknown-create-01"
        _target, partial, _marker = contrib.workspace_create_paths(self.repo, name)
        partial.mkdir(mode=0o700)
        with self.assertRaisesRegex(contrib.ContribError, "unowned creation partial"):
            contrib.recover_workspace_state(self.repo, name)
        self.assertTrue(partial.exists())

    def test_workspace_recovery_clears_interrupted_fingerprint_scratch(self) -> None:
        start, selected, resolution, _case = self.mocks()
        name = "audit-fingerprint-01"
        with start, selected, resolution:
            contrib.create_workspace(
                self.repo,
                name,
                "cases/sample-case",
                "patched",
            )
        scratch = contrib.begin_workspace_fingerprint(self.repo, name)
        (scratch / "index").write_bytes(b"incomplete index")
        (scratch / "index").chmod(0o600)
        with (
            selected,
            resolution,
            self.assertRaisesRegex(contrib.ContribError, "workspace-recover"),
        ):
            contrib.finalized_workspace_fingerprint(self.repo, name)

        _scratch, owner, staging, removal = contrib.workspace_fingerprint_paths(
            self.repo,
            name,
        )
        self.assertEqual(
            contrib.recover_workspace_state(self.repo, name),
            (staging, removal, owner),
        )
        self.assertFalse(scratch.exists())
        with selected, resolution:
            self.assertRegex(
                contrib.finalized_workspace_fingerprint(self.repo, name),
                r"^[0-9a-f]{64}$",
            )

    def test_workspace_recovery_refuses_unknown_fingerprint_scratch(self) -> None:
        name = "audit-unknown-fingerprint-01"
        scratch = contrib.workspace_fingerprint_path(self.repo, name)
        scratch.mkdir(mode=0o700)
        with self.assertRaisesRegex(contrib.ContribError, "unowned fingerprint state"):
            contrib.recover_workspace_state(self.repo, name)
        self.assertTrue(scratch.exists())

    def test_workspace_fingerprint_recovery_resumes_partial_rmtree(self) -> None:
        name = "audit-fingerprint-rmtree-01"
        scratch = contrib.begin_workspace_fingerprint(self.repo, name)
        index = scratch / "index"
        index.write_bytes(b"partial index\n")
        index.chmod(0o600)
        _scratch, owner, staging, removal = contrib.workspace_fingerprint_paths(
            self.repo,
            name,
        )
        original_rmtree = contrib.shutil.rmtree
        interrupted = False

        def partially_remove(path: Path, *args: object, **kwargs: object) -> None:
            nonlocal interrupted
            candidate = Path(path)
            if candidate == staging and not interrupted:
                interrupted = True
                index_at_staging = staging / "index"
                index_at_staging.unlink()
                raise RuntimeError("simulated fingerprint rmtree crash")
            original_rmtree(path, *args, **kwargs)

        with (
            patch.object(contrib.shutil, "rmtree", side_effect=partially_remove),
            self.assertRaisesRegex(RuntimeError, "fingerprint rmtree crash"),
        ):
            contrib.remove_workspace_fingerprint(self.repo, name, scratch)

        self.assertFalse(scratch.exists())
        self.assertTrue(staging.is_dir())
        self.assertTrue(removal.is_file())
        self.assertTrue(owner.is_file())
        self.assertEqual(
            contrib.recover_workspace_state(self.repo, name),
            (staging, removal, owner),
        )
        self.assertFalse(staging.exists())
        self.assertFalse(removal.exists())
        self.assertFalse(owner.exists())

    def test_workspace_fingerprint_remove_never_overwrites_racing_staging(self) -> None:
        name = "audit-fingerprint-race-01"
        scratch = contrib.begin_workspace_fingerprint(self.repo, name)
        contrib.publish_workspace_fingerprint_remove_transaction(self.repo, name)
        _scratch, owner, staging, removal = contrib.workspace_fingerprint_paths(
            self.repo,
            name,
        )
        staging.mkdir(mode=0o700)
        sentinel = staging / "sentinel"
        sentinel.write_text("do not overwrite\n", encoding="utf-8")

        with self.assertRaisesRegex(contrib.ContribError, "both scratch and staging"):
            contrib.remove_workspace_fingerprint(self.repo, name, scratch)

        self.assertTrue(scratch.is_dir())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "do not overwrite\n")
        self.assertTrue(removal.is_file())
        self.assertTrue(owner.is_file())

    def test_workspace_fingerprint_remove_finishes_phase_after_owner_unlink(self) -> None:
        name = "audit-fingerprint-owner-unlink-01"
        scratch = contrib.begin_workspace_fingerprint(self.repo, name)
        contrib.publish_workspace_fingerprint_remove_transaction(self.repo, name)
        _scratch, owner, staging, removal = contrib.workspace_fingerprint_paths(
            self.repo,
            name,
        )
        contrib.container_payload.rename_no_replace(scratch, staging)
        contrib.shutil.rmtree(staging)
        owner.unlink()

        self.assertEqual(
            contrib.recover_workspace_state(self.repo, name),
            (removal,),
        )
        self.assertFalse(removal.exists())

    def test_case_creation_uses_ignored_staging_and_recovers_exact_partial(self) -> None:
        cases_root = self.repo / "fork-maintenance" / "cases"
        slug = "new-crash-case"
        with patch.object(contrib, "CASES_ROOT", cases_root):
            target, partial, marker = contrib.case_create_paths(self.repo, slug)
            contrib.publish_private_json(
                marker,
                {
                    "kind": "case-create",
                    "operation_id": "12345678-1234-4abc-8def-123456789abc",
                    "owner": contrib.CASE_CREATE_OWNER,
                    "partial": str(partial),
                    "schema": 1,
                    "slug": slug,
                    "target": str(target),
                },
                "test case creation owner",
            )
            partial.mkdir(mode=0o700)
            (partial / "case.toml").write_text("incomplete\n", encoding="utf-8")
            recovered = contrib.recover_case_creation(self.repo, slug)

        self.assertEqual(recovered, (partial, marker))
        self.assertFalse(partial.exists())
        self.assertFalse(marker.exists())
        self.assertFalse(target.exists())
        self.assertFalse(any(path.name.startswith(f".{slug}.") for path in cases_root.iterdir()))

    def test_case_creation_recovery_refuses_an_unowned_partial(self) -> None:
        cases_root = self.repo / "fork-maintenance" / "cases"
        slug = "unknown-crash-case"
        with patch.object(contrib, "CASES_ROOT", cases_root):
            _target, partial, _marker = contrib.case_create_paths(self.repo, slug)
            partial.mkdir(mode=0o700)
            with self.assertRaisesRegex(contrib.ContribError, "unowned creation partial"):
                contrib.recover_case_creation(self.repo, slug)
        self.assertTrue(partial.exists())

    def test_scaffold_case_never_stages_below_tracked_cases(self) -> None:
        cases_root = self.repo / "fork-maintenance" / "cases"
        slug = "new-draft-case"
        with patch.object(contrib, "CASES_ROOT", cases_root):
            target = contrib.scaffold_case(self.repo, slug)
        self.assertEqual(target, cases_root / slug)
        self.assertTrue(target.is_dir())
        self.assertFalse(any(path.name.startswith(f".{slug}.") for path in cases_root.iterdir()))
        staging = contrib.case_staging_root(self.repo)
        self.assertEqual(tuple(staging.iterdir()), ())

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
        self.sources = self.root / "upstream-tests" / "sources"
        self.deb_root = self.root / "deb-packages"
        self.deb_runs = self.deb_root / "runs"
        self.deb_results = self.deb_root / "results"
        self.deb_outputs = self.deb_root / "outputs"
        self.deb_sources = self.deb_root / "sources"
        self.deb_selections = self.deb_root / "selections"
        self.deb_locks = self.deb_root / "locks"
        self.live_jobs = self.root / "jobs" / "live"
        self.live_results = self.root / "live-results"
        for path in (
            self.repo / ".artifacts",
            self.root,
            self.root / "upstream-tests",
            self.logs,
            self.runs,
            self.image_builds,
            self.sources,
            self.deb_root,
            self.deb_runs,
            self.deb_results,
            self.deb_outputs,
            self.root / "jobs",
            self.live_jobs,
            self.live_results,
        ):
            path.mkdir(mode=0o700, exist_ok=True)
            path.chmod(0o700)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_after_releasing_case_update_lock(
        self,
        action: Callable[[], object],
    ) -> object:
        acquired = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        values: list[object] = []
        errors: list[BaseException] = []

        def hold_lock() -> None:
            try:
                with contrib.case_update_lock(self.repo):
                    acquired.set()
                    if not release.wait(5):
                        raise AssertionError("test did not release case update lock")
            except Exception as error:  # noqa: BLE001 - propagate across the test thread.
                errors.append(error)

        def invoke() -> None:
            try:
                values.append(action())
            except Exception as error:  # noqa: BLE001 - propagate across the test thread.
                errors.append(error)
            finally:
                completed.set()

        holder = threading.Thread(target=hold_lock)
        holder.start()
        self.assertTrue(acquired.wait(5))
        worker = threading.Thread(target=invoke)
        worker.start()
        self.assertFalse(completed.wait(0.1))
        release.set()
        holder.join(5)
        worker.join(5)
        self.assertFalse(holder.is_alive())
        self.assertFalse(worker.is_alive())
        if errors:
            raise errors[0]
        self.assertEqual(len(values), 1)
        return values[0]

    def run_after_releasing_cleanup_lock(
        self,
        lock: Path,
        action: Callable[[], object],
    ) -> object:
        lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock.parent.chmod(0o700)
        descriptor = os.open(
            lock,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        completed = threading.Event()
        values: list[object] = []
        errors: list[BaseException] = []

        def invoke() -> None:
            try:
                values.append(action())
            except Exception as error:  # noqa: BLE001 - propagate across the test thread.
                errors.append(error)
            finally:
                completed.set()

        worker = threading.Thread(target=invoke)
        worker.start()
        try:
            self.assertFalse(completed.wait(0.1))
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        worker.join(5)
        self.assertFalse(worker.is_alive())
        if errors:
            raise errors[0]
        self.assertEqual(len(values), 1)
        return values[0]

    def leave_cleanup_transaction(
        self,
        plan: contrib.CleanupPlan,
        removed_count: int,
    ) -> Path:
        with contrib.cleanup_lifecycle_locks(self.repo):
            marker = contrib.publish_cleanup_transaction(self.repo, plan)
        for target in plan.targets[:removed_count]:
            if target.kind in {"live-result-tree", "workspace"}:
                shutil.rmtree(target.path)
            else:
                target.path.unlink()
        return marker

    def assert_cleanup_transaction_resumes(
        self,
        plan: contrib.CleanupPlan,
        marker: Path,
        removed_count: int,
    ) -> None:
        resumed = contrib.build_cleanup_plan(
            self.repo,
            plan.cycle,
            inspect_runtime=False,
        )
        self.assertEqual(resumed, plan)
        self.assertEqual(
            contrib.remove_cleanup_plan(self.repo, resumed, resumed.digest),
            len(plan.targets) - removed_count,
        )
        self.assertFalse(marker.exists())
        self.assertTrue(
            all(
                not target.path.exists() and not target.path.is_symlink()
                for target in plan.targets
            )
        )

    def create_case_fixture(self, slug: str) -> contrib.Case:
        case_dir = self.repo / "fork-maintenance" / "cases" / slug
        case_dir.mkdir(parents=True)
        (case_dir / "README.md").write_text(f"# {slug}\n", encoding="utf-8")
        patch_bytes = (
            b"diff --git a/target.txt b/target.txt\n"
            b"--- a/target.txt\n"
            b"+++ b/target.txt\n"
            b"@@ -1 +1 @@\n"
            b"-old\n"
            b"+new\n"
        )
        (case_dir / "fix.patch").write_bytes(patch_bytes)
        (case_dir / "case.toml").write_text(
            "\n".join(
                (
                    "schema = 1",
                    f'slug = "{slug}"',
                    'title = "Audit"',
                    'commit_subject = "Audit"',
                    f'patch_sha256 = "{hashlib.sha256(patch_bytes).hexdigest()}"',
                    "dependencies = []",
                    'paths = ["target.txt"]',
                    "",
                    "[tests]",
                    'list = ["unit.audit_test"]',
                    "",
                    "[evidence]",
                    "required_gates = []",
                    "",
                )
            ),
            encoding="utf-8",
        )
        return contrib.load_case(case_dir)

    def write_upstream_remove_transaction(
        self,
        name: str,
        kind: str,
        record: dict[str, object],
    ) -> Path:
        log = self.logs / f"{name}.log"
        status = self.logs / f"{name}.status"
        marker = self.logs / f"{name}.remove.json"
        marker.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "owner": contrib.UPSTREAM_TEST_OWNER,
                    "kind": kind,
                    "name": name,
                    "record": record,
                    "owner_sha256": "0" * 64,
                    "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
                    "status_sha256": hashlib.sha256(status.read_bytes()).hexdigest(),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        marker.chmod(0o600)
        return marker

    def refresh_upstream_remove_transaction(self, name: str) -> None:
        marker = self.logs / f"{name}.remove.json"
        payload = json.loads(marker.read_text(encoding="utf-8"))
        payload["log_sha256"] = hashlib.sha256(
            (self.logs / f"{name}.log").read_bytes()
        ).hexdigest()
        payload["status_sha256"] = hashlib.sha256(
            (self.logs / f"{name}.status").read_bytes()
        ).hexdigest()
        marker.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    def write_upstream_result(
        self,
        name: str,
        payload: bytes,
        resolution_digest: str = "",
    ) -> None:
        log = self.logs / f"{name}.log"
        status = self.logs / f"{name}.status"
        validation_ok = bool(resolution_digest)
        log.write_bytes(payload)
        values = {
            "schema": "3",
            "owner": contrib.UPSTREAM_TEST_OWNER,
            "run_id": "12345678-1234-4abc-8def-123456789abc",
            "name": name,
            "result": "success" if validation_ok else "failed",
            "exit_code": "0" if validation_ok else "1",
            "validation_ok": str(int(validation_ok)),
            "container_present": "1",
            "container_id": "a" * 64,
            "container_status": "exited",
            "container_exit": "0" if validation_ok else "1",
            "finished": "2026-08-28T12:00:00Z",
            "target": "full",
            "selection": "stacks/develop",
            "selection_sha256": "b" * 64,
            "selection_resolution_ok": str(int(validation_ok)),
            "selection_resolution_sha256": resolution_digest,
            "patch_mode": "patched",
            "payload_path": str(self.runs / f"{name}.payload"),
            "source": "1" * 40,
            "source_head": "2" * 40,
            "source_remote": "origin",
            "workflow_sha256": "c" * 64,
            "runner_sha256": "d" * 64,
            "image_input_sha256": "e" * 64,
            "image": "localhost/xpra-test:current",
            "expected_image_id": "f" * 64,
            "image_id": "f" * 64,
            "logs_ok": "1",
            "log_sha256": hashlib.sha256(payload).hexdigest(),
        }
        status.write_text(
            "".join(f"{key}={value}\n" for key, value in values.items()),
            encoding="utf-8",
        )
        log.chmod(0o600)
        status.chmod(0o600)
        self.write_upstream_remove_transaction(
            name,
            "test-remove",
            {
                "schema": "4",
                "owner": contrib.UPSTREAM_TEST_OWNER,
                "run_id": values["run_id"],
                "name": name,
                "container_id": values["container_id"],
                "target": values["target"],
                "selection": values["selection"],
                "selection_sha256": values["selection_sha256"],
                "patch_mode": values["patch_mode"],
                "payload_path": values["payload_path"],
                "source": values["source"],
                "source_head": values["source_head"],
                "source_remote": values["source_remote"],
                "workflow_sha256": values["workflow_sha256"],
                "runner_sha256": values["runner_sha256"],
                "image": values["image"],
                "image_id": values["image_id"],
                "image_input_sha256": values["image_input_sha256"],
            },
        )

    def collected_result(self, name: str, payload: bytes = b"complete\n") -> None:
        self.write_upstream_result(name, payload)

    def collected_resolution_result(self, name: str, digest: str) -> None:
        payload = f"selection_resolution_sha256={digest}\ncomplete\n".encode()
        self.write_upstream_result(name, payload, digest)

    def collected_image_result(self, name: str) -> tuple[Path, Path]:
        payload = b"image build complete\n"
        log = self.logs / f"{name}.log"
        status = self.logs / f"{name}.status"
        log.write_bytes(payload)
        values = {
            "schema": "2",
            "owner": contrib.UPSTREAM_TEST_OWNER,
            "run_id": "12345678-1234-4abc-8def-123456789abc",
            "name": name,
            "result": "success",
            "exit_code": "0",
            "validation_ok": "1",
            "image": "localhost/xpra-test:current",
            "iid_ok": "1",
            "image_exists": "1",
            "image_id": "a" * 64,
            "image_builder": "true",
            "image_input_sha256": "b" * 64,
            "source": "1" * 40,
            "workflow_sha256": "c" * 64,
            "runner_sha256": "d" * 64,
            "selection_resolution_ok": "0",
            "selection_resolution_sha256": "",
            "logs_ok": "1",
            "log_sha256": hashlib.sha256(payload).hexdigest(),
            "finished": "2026-08-28T12:00:00Z",
        }
        status.write_text(
            "".join(f"{key}={value}\n" for key, value in values.items()),
            encoding="utf-8",
        )
        log.chmod(0o600)
        status.chmod(0o600)
        self.write_upstream_remove_transaction(
            name,
            "image-build-remove",
            {
                "schema": 3,
                "owner": contrib.UPSTREAM_TEST_OWNER,
                "kind": "image-build",
                "name": name,
                "job_id": values["run_id"],
                "image": values["image"],
                "input_sha256": values["image_input_sha256"],
                "source": values["source"],
                "workflow_sha256": values["workflow_sha256"],
                "runner_sha256": values["runner_sha256"],
            },
        )
        return status, log

    def collected_deb_result(self, name: str) -> tuple[Path, Path]:
        distro = "ubuntu-26.04"
        output = self.deb_outputs / f"{name}-ubuntu-26.04-debs.tar"
        output.write_bytes(b"packages\n")
        output.chmod(0o600)
        log = self.deb_results / f"{name}.log"
        log.write_bytes(b"package log\n")
        log.chmod(0o600)
        run_root = self.deb_runs / name
        source_root = self.deb_root / "sources" / f"{'1' * 40}-{'a' * 64}"
        selection_cache_sha256 = "6" * 64
        selection_sha256 = "b" * 64
        selection_root = (
            self.deb_root
            / "selections"
            / f"{selection_sha256}-{selection_cache_sha256}"
        )
        arguments = {
            "build_id": "12345678-1234-4abc-8def-123456789abc",
            "checkout_commit": "1" * 40,
            "container_name": f"xpra-deb-{name}",
            "container_state": str(run_root / "container.json"),
            "distro": distro,
            "output": str(output),
            "output_partial": str(output.with_name(f".{output.name}.partial")),
            "selection": "stacks/develop",
            "selection_cache_sha256": selection_cache_sha256,
            "selection_sha256": selection_sha256,
            "selection_snapshot": str(selection_root / "lab"),
            "selection_state": str(selection_root / "selection.json"),
            "source": "2" * 40,
            "source_bundle": str(source_root / "source.bundle"),
            "source_ref": "refs/remotes/origin/master",
            "source_ref_commit": "3" * 40,
            "source_state": str(source_root / "source.json"),
            "workflow_sha256": "c" * 64,
        }
        container = {
            "base_image_id": "d" * 64,
            "builder_image_input_sha256": "e" * 64,
            "container_id": "f" * 64,
            "image_id": "0" * 64,
        }
        manifest = {
            "architecture": "amd64",
            "base_version": "6.4",
            "base_image_id": container["base_image_id"],
            "builder_image_id": container["image_id"],
            "builder_image_input_sha256": container["builder_image_input_sha256"],
            "checkout_commit": arguments["checkout_commit"],
            "debian_version": "6.4-r5115-1",
            "distro": distro,
            "packages": [
                {
                    "architecture": "amd64",
                    "name": "xpra-test.deb",
                    "package": "xpra-test",
                    "sha256": "7" * 64,
                    "size": 1024,
                    "version": "6.4-r5115-1",
                }
            ],
            "revision": 5115,
            "revision_first_parent_count": 101,
            "schema": 2,
            "selection": arguments["selection"],
            "selection_cache_sha256": arguments["selection_cache_sha256"],
            "selection_resolution_sha256": "4" * 64,
            "selection_sha256": arguments["selection_sha256"],
            "source_commit": arguments["source"],
            "source_ref": arguments["source_ref"],
            "source_ref_commit": arguments["source_ref_commit"],
            "workflow_sha256": arguments["workflow_sha256"],
        }
        status = self.deb_results / f"{name}.status.json"
        status_payload = {
            "arguments": arguments,
            "container": container,
            "exit_code": 0,
            "finished_at": "2026-08-28T12:00:00Z",
            "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
            "manifest": manifest,
            "name": name,
            "output": str(output),
            "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            "owner": contrib.DEB_PACKAGE_OWNER,
            "process_pid": 12345,
            "runner_sha256": "5" * 64,
            "schema": 2,
            "validation_error": "",
            "validation_ok": True,
        }
        status.write_bytes(contrib.canonical_json_bytes(status_payload))
        status.chmod(0o600)
        self.write_deb_remove_transaction(name, status_payload)
        return status, output

    def write_deb_remove_transaction(
        self,
        name: str,
        status: dict[str, object],
    ) -> Path:
        run_directory = self.deb_runs / name
        log = self.deb_results / f"{name}.log"
        status_path = self.deb_results / f"{name}.status.json"
        arguments = status["arguments"]
        self.assertIsInstance(arguments, dict)
        record = {
            "arguments": arguments,
            "kind": "deb-build",
            "name": name,
            "owner": contrib.DEB_PACKAGE_OWNER,
            "process": {
                "completion": str(run_directory / "completion.json"),
                "owner_token": "8" * 64,
                "pid": status["process_pid"],
                "process_group": status["process_pid"],
                "runtime_log": str(run_directory / "runtime.log"),
                "start_ticks": "12345",
                "supervisor_sha256": "9" * 64,
            },
            "runner_sha256": status["runner_sha256"],
            "schema": 2,
        }
        prelaunch = {
            "arguments": arguments,
            "kind": "deb-build-prelaunch",
            "name": name,
            "owner": contrib.DEB_PACKAGE_OWNER,
            "runner_sha256": status["runner_sha256"],
            "schema": 1,
        }
        marker = self.deb_results / f"{name}.remove.json"
        marker.write_bytes(
            contrib.canonical_json_bytes(
                {
                    "final_log": str(log),
                    "final_status": str(status_path),
                    "kind": "deb-build-remove",
                    "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
                    "name": name,
                    "owner": contrib.DEB_PACKAGE_OWNER,
                    "owner_record": record,
                    "owner_sha256": hashlib.sha256(
                        contrib.canonical_json_bytes(record)
                    ).hexdigest(),
                    "output": status["output"],
                    "output_sha256": status["output_sha256"],
                    "prelaunch_sha256": hashlib.sha256(
                        contrib.canonical_json_bytes(prelaunch)
                    ).hexdigest(),
                    "run_device": 1,
                    "run_directory": str(run_directory),
                    "run_inode": 2,
                    "schema": 1,
                    "status": status,
                    "status_sha256": hashlib.sha256(
                        contrib.canonical_json_bytes(status)
                    ).hexdigest(),
                    "validation_ok": status["validation_ok"],
                }
            )
        )
        marker.chmod(0o600)
        return marker

    def collected_live_result(self, name: str) -> tuple[Path, Path, Path]:
        result = self.live_results / name
        result.mkdir(mode=0o700)
        inputs = result / "inputs"
        inputs.mkdir(mode=0o700)
        report = result / "report.json"
        report.write_text('{"result": "passed"}\n', encoding="utf-8")
        report.chmod(0o600)
        log = self.live_jobs / f"{name}.log"
        log.write_bytes(b"live log\n")
        log.chmod(0o600)
        status = self.live_jobs / f"{name}.status.json"
        status.write_text(
            json.dumps(
                {
                    "background_supervisor_sha256": "1" * 64,
                    "collected_at": "2026-08-28T12:01:00Z",
                    "exit_code": 0,
                    "finished_at": "2026-08-28T12:00:00Z",
                    "harness_sha256": "2" * 64,
                    "input_provenance": {
                        "client_context_archive_sha256": "3" * 64,
                        "client_context_sha256": "4" * 64,
                        "client_selection": "master",
                        "client_selection_resolution_sha256": "5" * 64,
                        "client_selection_sha256": "6" * 64,
                        "harness_sha256": "2" * 64,
                        "harness": {"infra/live/job.py": "f" * 64},
                        "input_manifest_sha256": "7" * 64,
                        "input_tree_sha256": "8" * 64,
                        "path": str(inputs),
                        "schema": 2,
                        "server_context_archive_sha256": "9" * 64,
                        "server_context_sha256": "a" * 64,
                        "server_selection": "stacks/develop",
                        "server_selection_resolution_sha256": "b" * 64,
                        "server_selection_sha256": "c" * 64,
                        "source_archive_sha256": "d" * 64,
                        "source_commit": "1" * 40,
                        "source_commit_marker": "v6.4",
                        "source_revision": 100,
                        "source_workflow_sha256": "e" * 64,
                        "zed_archive_sha256": None,
                        "zed_binary_sha256": None,
                    },
                    "job_id": "12345678-1234-4abc-8def-123456789abc",
                    "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
                    "logs_ok": True,
                    "owner": contrib.LIVE_JOB_OWNER,
                    "owned_objects_remaining": {"containers": [], "networks": []},
                    "process_pid": 12345,
                    "report": str(report),
                    "report_checks": {
                        key: True
                        for key in (
                            "alpha_scenarios",
                            "application",
                            "background_supervisor_sha256",
                            "current_images",
                            "encoding",
                            "evidence_tree",
                            "h264_client_policy",
                            "harness_sha256",
                            "image_provenance",
                            "job_id",
                            "lifecycle",
                            "render_node",
                            "result",
                            "run_id",
                            "selection",
                            "selection_provenance",
                            "source_provenance",
                            "supervisor_sha256",
                        )
                    },
                    "report_result": "passed",
                    "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
                    "result": "success",
                    "run": name,
                    "runner_sha256": "f" * 64,
                    "schema": 3,
                    "supervisor_sha256": "0" * 64,
                    "validation_ok": True,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        status.chmod(0o600)
        status_payload = json.loads(status.read_text(encoding="utf-8"))
        remove = self.live_jobs / f"{name}.remove.json"
        remove.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "owner": contrib.LIVE_JOB_OWNER,
                    "kind": "live-remove",
                    "run": name,
                    "record": {
                        "schema": 4,
                        "owner": contrib.LIVE_JOB_OWNER,
                        "run": name,
                        "job_id": status_payload["job_id"],
                        "result_report": status_payload["report"],
                        "input_provenance": status_payload["input_provenance"],
                        "background_supervisor_sha256": status_payload[
                            "background_supervisor_sha256"
                        ],
                        "harness_sha256": status_payload["harness_sha256"],
                        "runner_sha256": status_payload["runner_sha256"],
                        "supervisor_sha256": status_payload["supervisor_sha256"],
                    },
                    "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
                    "status_sha256": hashlib.sha256(status.read_bytes()).hexdigest(),
                    "runtime_sha256": {"owner": "0" * 64},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        remove.chmod(0o600)
        return status, log, result

    def refresh_live_remove_transaction(self, name: str) -> None:
        marker = self.live_jobs / f"{name}.remove.json"
        payload = json.loads(marker.read_text(encoding="utf-8"))
        payload["log_sha256"] = hashlib.sha256(
            (self.live_jobs / f"{name}.log").read_bytes()
        ).hexdigest()
        payload["status_sha256"] = hashlib.sha256(
            (self.live_jobs / f"{name}.status.json").read_bytes()
        ).hexdigest()
        marker.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    def retained_deb_selection_cache(self) -> tuple[Path, str]:
        self.deb_selections.mkdir(mode=0o700)
        temporary = self.deb_selections / "cache-staging"
        lab = temporary / "lab"
        lab.mkdir(parents=True, mode=0o700)
        temporary.chmod(0o700)
        control = lab / "control.txt"
        control.write_text("selection\n", encoding="utf-8")
        control.chmod(0o600)
        selection_sha256 = "b" * 64
        state = temporary / "selection.json"
        state.write_text(
            json.dumps(
                {
                    "owner": contrib.DEB_SELECTION_OWNER,
                    "schema": 1,
                    "selection": "stacks/develop",
                    "selection_sha256": selection_sha256,
                    "snapshot_tree_sha256": contrib.deb_selection_tree_sha256(lab),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        state.chmod(0o600)
        cache_sha256 = hashlib.sha256(state.read_bytes()).hexdigest()
        cache = self.deb_selections / f"{selection_sha256}-{cache_sha256}"
        temporary.rename(cache)
        return cache, selection_sha256

    def test_digest_confirmed_cleanup_removes_only_the_named_cycle(self) -> None:
        self.collected_result("audit-focused-01")
        self.collected_result("auditor-keep-01", b"keep\n")
        plan = contrib.build_cleanup_plan(
            self.repo,
            "audit",
            inspect_runtime=False,
        )
        self.assertEqual(len(plan.targets), 3)
        with self.assertRaisesRegex(contrib.ContribError, "CONFIRM"):
            contrib.remove_cleanup_plan(self.repo, plan, "0" * 64)
        self.assertTrue((self.logs / "audit-focused-01.log").exists())
        self.assertEqual(
            contrib.remove_cleanup_plan(self.repo, plan, plan.digest),
            3,
        )
        self.assertFalse((self.logs / "audit-focused-01.log").exists())
        self.assertTrue((self.logs / "auditor-keep-01.log").exists())

    def test_cleanup_requires_the_cycle_prefix_separator(self) -> None:
        self.collected_result("audit")
        with self.assertRaisesRegex(contrib.ContribError, "no finalized artifacts"):
            contrib.build_cleanup_plan(
                self.repo,
                "audit",
                inspect_runtime=False,
            )
        self.assertTrue((self.logs / "audit.status").exists())
        self.assertTrue((self.logs / "audit.log").exists())

    def test_cleanup_accepts_a_current_standalone_image_result(self) -> None:
        status, log = self.collected_image_result("audit-image-01")
        plan = contrib.build_cleanup_plan(
            self.repo,
            "audit",
            inspect_runtime=False,
        )
        remove = self.logs / "audit-image-01.remove.json"
        self.assertEqual({target.path for target in plan.targets}, {status, log, remove})

    def test_cleanup_rejects_an_incomplete_upstream_status(self) -> None:
        self.collected_result("audit-focused-01")
        status = self.logs / "audit-focused-01.status"
        status.write_text(
            status.read_text(encoding="utf-8").replace("schema=3\n", ""),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(contrib.ContribError, "unsupported schema"):
            contrib.build_cleanup_plan(
                self.repo,
                "audit",
                inspect_runtime=False,
            )

    def test_cleanup_requires_a_retained_upstream_removal_transaction(self) -> None:
        self.collected_result("audit-focused-01")
        (self.logs / "audit-focused-01.remove.json").unlink()
        with self.assertRaisesRegex(contrib.ContribError, "incomplete"):
            contrib.build_cleanup_plan(
                self.repo,
                "audit",
                inspect_runtime=False,
            )

    def test_cleanup_rejects_a_noncurrent_upstream_status_schema(self) -> None:
        self.collected_result("audit-focused-01")
        status = self.logs / "audit-focused-01.status"
        status.write_text(
            status.read_text(encoding="utf-8") + "unexpected=field\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(contrib.ContribError, "current owned schema"):
            contrib.build_cleanup_plan(
                self.repo,
                "audit",
                inspect_runtime=False,
            )

    def test_cleanup_accepts_failed_upstream_result_without_finished_timestamp(self) -> None:
        self.collected_result("audit-focused-01")
        status = self.logs / "audit-focused-01.status"
        status.write_text(
            status.read_text(encoding="utf-8").replace(
                "finished=2026-08-28T12:00:00Z\n", "finished=\n"
            ),
            encoding="utf-8",
        )
        self.refresh_upstream_remove_transaction("audit-focused-01")
        plan = contrib.build_cleanup_plan(
            self.repo,
            "audit",
            inspect_runtime=False,
        )
        self.assertEqual(len(plan.targets), 3)

    def test_digest_confirmed_cleanup_accepts_live_schema_three(self) -> None:
        status, log, result = self.collected_live_result("audit-live-01")
        plan = contrib.build_cleanup_plan(
            self.repo,
            "audit",
            inspect_runtime=False,
        )
        remove = self.live_jobs / "audit-live-01.remove.json"
        self.assertEqual(
            {target.path for target in plan.targets},
            {status, log, remove, result},
        )
        self.assertEqual(
            contrib.remove_cleanup_plan(self.repo, plan, plan.digest),
            4,
        )
        self.assertFalse(status.exists())
        self.assertFalse(log.exists())
        self.assertFalse(result.exists())

    def test_cleanup_accepts_a_failed_live_result_with_an_invalid_report(self) -> None:
        status_path, log, result = self.collected_live_result("audit-live-01")
        report = result / "report.json"
        report.write_text("not JSON\n", encoding="utf-8")
        status = json.loads(status_path.read_text(encoding="utf-8"))
        status.update(
            {
                "report_checks": {},
                "report_result": "missing",
                "report_sha256": "",
                "result": "failed",
                "validation_ok": False,
            }
        )
        status_path.write_text(json.dumps(status) + "\n", encoding="utf-8")
        self.refresh_live_remove_transaction("audit-live-01")

        plan = contrib.build_cleanup_plan(
            self.repo,
            "audit",
            inspect_runtime=False,
        )
        remove = self.live_jobs / "audit-live-01.remove.json"
        self.assertEqual(
            {target.path for target in plan.targets},
            {status_path, log, remove, result},
        )

    def test_cleanup_rejects_a_noncurrent_live_status_schema(self) -> None:
        status_path, _log, _result = self.collected_live_result("audit-live-01")
        status = json.loads(status_path.read_text(encoding="utf-8"))
        status["unexpected"] = True
        status_path.write_text(json.dumps(status) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(contrib.ContribError, "current owned schema"):
            contrib.build_cleanup_plan(
                self.repo,
                "audit",
                inspect_runtime=False,
            )

    def test_cleanup_rejects_a_tampered_live_removal_transaction(self) -> None:
        self.collected_live_result("audit-live-01")
        marker = self.live_jobs / "audit-live-01.remove.json"
        payload = json.loads(marker.read_text(encoding="utf-8"))
        payload["status_sha256"] = "9" * 64
        marker.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(contrib.ContribError, "transaction identity"):
            contrib.build_cleanup_plan(
                self.repo,
                "audit",
                inspect_runtime=False,
            )

    def test_cleanup_accepts_a_resolution_digest_bound_only_to_the_log(self) -> None:
        digest = "a" * 64
        self.collected_resolution_result("audit-focused-01", digest)
        plan = contrib.build_cleanup_plan(
            self.repo,
            "audit",
            inspect_runtime=False,
        )
        self.assertEqual(len(plan.targets), 3)

    def test_cleanup_does_not_require_a_branch_name_or_remotes(self) -> None:
        command("git", "switch", "-q", "-c", "arbitrary-package-branch", cwd=self.repo)
        command("git", "remote", "remove", "origin", cwd=self.repo)
        command("git", "remote", "remove", "upstream", cwd=self.repo)
        command("git", "checkout", "-q", "--detach", "HEAD", cwd=self.repo)
        self.collected_result("package-focused-01")
        plan = contrib.build_cleanup_plan(
            self.repo,
            "package",
            inspect_runtime=False,
        )
        self.assertEqual(len(plan.targets), 3)

    def test_cleanup_rejects_a_log_resolution_digest_mismatch(self) -> None:
        self.collected_resolution_result("audit-focused-01", "a" * 64)
        log = self.logs / "audit-focused-01.log"
        original_payload = log.read_bytes()
        payload = original_payload.replace(b"a" * 64, b"b" * 64)
        log.write_bytes(payload)
        status = self.logs / "audit-focused-01.status"
        values = status.read_text(encoding="utf-8").replace(
            f"log_sha256={hashlib.sha256(original_payload).hexdigest()}",
            f"log_sha256={hashlib.sha256(payload).hexdigest()}",
        )
        status.write_text(values, encoding="utf-8")
        with self.assertRaisesRegex(contrib.ContribError, "log resolution digest"):
            contrib.build_cleanup_plan(
                self.repo,
                "audit",
                inspect_runtime=False,
            )

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

    def test_cleanup_refuses_live_input_freeze_state_and_staging(self) -> None:
        self.collected_result("audit-focused-01")
        freeze = self.live_jobs / "audit-live-01.freeze.json"
        freeze.write_text("{}\n", encoding="utf-8")
        freeze.chmod(0o600)
        staging = self.live_results / (
            ".audit-live-01.freeze-12345678-1234-4abc-8def-123456789abc"
        )
        staging.mkdir(mode=0o700)
        with self.assertRaisesRegex(contrib.ContribError, "runtime state"):
            contrib.build_cleanup_plan(
                self.repo,
                "audit",
                inspect_runtime=False,
            )

    def test_cleanup_validates_and_blocks_a_live_input_freeze_abort(self) -> None:
        self.collected_result("freeze-abort-focused-01")
        run = "freeze-abort-live-01"
        freeze = self.live_jobs / f"{run}.freeze.json"
        freeze.write_text("{}\n", encoding="utf-8")
        freeze.chmod(0o600)
        marker = self.live_jobs / f"{run}.freeze-abort.json"
        marker.write_bytes(
            contrib.canonical_json_bytes(
                {
                    "directories": {
                        "result": {
                            "present": False,
                            "removal": str(
                                self.live_results / f".{run}.freeze-abort-result"
                            ),
                            "source": str(self.live_results / run),
                        },
                        "staging": {
                            "present": False,
                            "removal": str(
                                self.live_results / f".{run}.freeze-abort-staging"
                            ),
                            "source": str(
                                self.live_results
                                / (
                                    f".{run}.freeze-"
                                    "12345678-1234-4abc-8def-123456789abc"
                                )
                            ),
                        },
                    },
                    "freeze_owner_sha256": contrib.sha256_file(freeze),
                    "kind": "live-input-freeze-abort",
                    "owner": contrib.LIVE_JOB_OWNER,
                    "run": run,
                    "schema": 1,
                }
            )
        )
        marker.chmod(0o600)

        with self.assertRaisesRegex(contrib.ContribError, "runtime state"):
            contrib.build_cleanup_plan(
                self.repo,
                "freeze-abort",
                inspect_runtime=False,
            )

        payload = json.loads(marker.read_text(encoding="utf-8"))
        payload["directories"]["result"].update(
            {"present": True, "device": 1, "inode": 0}
        )
        marker.write_bytes(contrib.canonical_json_bytes(payload))
        with self.assertRaisesRegex(contrib.ContribError, "identity is invalid"):
            contrib.build_cleanup_plan(
                self.repo,
                "freeze-abort",
                inspect_runtime=False,
            )
        self.assertTrue(marker.exists())

    def test_cleanup_never_targets_live_freeze_abort_staging(self) -> None:
        self.collected_result("freeze-staging-focused-01")
        staging = self.live_results / ".freeze-staging-live-01.freeze-abort-result"
        staging.mkdir(mode=0o700)

        with self.assertRaisesRegex(contrib.ContribError, "runtime state"):
            contrib.build_cleanup_plan(
                self.repo,
                "freeze-staging",
                inspect_runtime=False,
            )
        self.assertTrue(staging.exists())

    def test_cleanup_refuses_live_input_freeze_prelaunch_state(self) -> None:
        self.collected_result("audit-focused-01")
        prelaunch = self.live_jobs / "audit-live-01.freeze-prelaunch.json"
        prelaunch.write_bytes(
            contrib.canonical_json_bytes(
                {
                    "kind": "input-freeze-prelaunch",
                    "owner": "xpra-lab-live-job",
                    "run": "audit-live-01",
                    "schema": 1,
                }
            )
        )
        prelaunch.chmod(0o600)

        with self.assertRaisesRegex(contrib.ContribError, "runtime state"):
            contrib.build_cleanup_plan(
                self.repo,
                "audit",
                inspect_runtime=False,
            )

    def test_pending_cleanup_refuses_new_live_prelaunch_and_deb_abort_state(self) -> None:
        blockers = (
            (
                "live-race",
                self.live_jobs / "live-race-live-01.freeze-prelaunch.json",
                {
                    "kind": "input-freeze-prelaunch",
                    "owner": "xpra-lab-live-job",
                    "run": "live-race-live-01",
                    "schema": 1,
                },
            ),
            (
                "deb-race",
                self.deb_runs / "deb-race-ubuntu-01.abort.json",
                {
                    "kind": "deb-build-abort",
                    "mode": "prelaunch",
                    "name": "deb-race-ubuntu-01",
                    "owner": contrib.DEB_PACKAGE_OWNER,
                    "schema": 1,
                },
            ),
            (
                "workspace-race",
                contrib.workspace_root(self.repo)
                / ".workspace-race-01.create.owner.json",
                {
                    "kind": "workspace-create",
                    "name": "workspace-race-01",
                    "owner": contrib.WORKSPACE_CREATE_OWNER,
                    "schema": 1,
                },
            ),
        )
        for cycle, blocker, payload in blockers:
            with self.subTest(cycle=cycle):
                self.collected_result(f"{cycle}-focused-01")
                plan = contrib.build_cleanup_plan(
                    self.repo,
                    cycle,
                    inspect_runtime=False,
                )
                marker = self.leave_cleanup_transaction(plan, 0)
                blocker.write_bytes(contrib.canonical_json_bytes(payload))
                blocker.chmod(0o600)

                with self.assertRaisesRegex(contrib.ContribError, "runtime state"):
                    contrib.build_cleanup_plan(
                        self.repo,
                        cycle,
                        inspect_runtime=False,
                    )

                self.assertTrue(marker.exists())
                self.assertTrue(blocker.exists())
                blocker.unlink()
                self.assert_cleanup_transaction_resumes(plan, marker, 0)

    def test_cleanup_refuses_owned_foreground_and_environment_partials(self) -> None:
        self.collected_result("audit-focused-01")
        upstream_root = self.root / "upstream-tests"
        foreground = upstream_root / ".foreground-payload"
        foreground.mkdir(mode=0o700)
        foreground_marker = upstream_root / ".foreground-payload.owner.json"
        foreground_marker.write_text("{}\n", encoding="utf-8")
        foreground_marker.chmod(0o600)
        venvs = self.root / "venvs"
        venvs.mkdir(mode=0o700)
        environment = venvs / ".environment.partial"
        environment.mkdir(mode=0o700)
        environment_marker = venvs / ".environment.partial.owner.json"
        environment_marker.write_text("{}\n", encoding="utf-8")
        environment_marker.chmod(0o600)
        with self.assertRaisesRegex(contrib.ContribError, "runtime state"):
            contrib.build_cleanup_plan(
                self.repo,
                "audit",
                inspect_runtime=False,
            )

    def test_cleanup_refuses_an_interrupted_source_bundle_partial(self) -> None:
        self.collected_result("audit-focused-01")
        partial = self.sources / f"{'1' * 40}-origin.bundle.partial"
        partial.write_bytes(b"interrupted")
        partial.chmod(0o600)
        with self.assertRaisesRegex(contrib.ContribError, "runtime state"):
            contrib.build_cleanup_plan(
                self.repo,
                "audit",
                inspect_runtime=False,
            )

    def test_cleanup_refuses_interrupted_workspace_fingerprint_state(self) -> None:
        self.collected_result("audit-focused-01")
        scratch = contrib.begin_workspace_fingerprint(self.repo, "audit-workspace-01")
        with self.assertRaisesRegex(contrib.ContribError, "runtime state"):
            contrib.build_cleanup_plan(
                self.repo,
                "audit",
                inspect_runtime=False,
            )
        self.assertTrue(scratch.exists())

    def test_cleanup_refuses_interrupted_case_creation_state(self) -> None:
        self.collected_result("audit-focused-01")
        cases_root = self.repo / "fork-maintenance" / "cases"
        cases_root.mkdir(parents=True)
        slug = "audit-new-case"
        with patch.object(contrib, "CASES_ROOT", cases_root):
            target, partial, marker = contrib.case_create_paths(self.repo, slug)
            contrib.publish_private_json(
                marker,
                {
                    "kind": "case-create",
                    "operation_id": "12345678-1234-4abc-8def-123456789abc",
                    "owner": contrib.CASE_CREATE_OWNER,
                    "partial": str(partial),
                    "schema": 1,
                    "slug": slug,
                    "target": str(target),
                },
                "test case creation owner",
            )
            partial.mkdir(mode=0o700)
            with self.assertRaisesRegex(contrib.ContribError, "runtime state"):
                contrib.build_cleanup_plan(
                    self.repo,
                    "audit",
                    inspect_runtime=False,
                )
        self.assertTrue(partial.exists())
        self.assertTrue(marker.exists())

    def test_cleanup_refuses_an_interrupted_case_update_transaction(self) -> None:
        self.collected_result("audit-focused-01")
        self.create_case_fixture("audit-case")
        transaction, owner = contrib.case_update_paths(self.repo, "audit-case")
        contrib.publish_private_json(
            owner,
            contrib.case_update_owner_payload(
                self.repo,
                "audit-case",
                "",
                "12345678-1234-4abc-8def-123456789abc",
            ),
            "test case update owner",
        )
        transaction.mkdir(mode=0o700)

        with self.assertRaisesRegex(contrib.ContribError, "runtime state"):
            contrib.build_cleanup_plan(
                self.repo,
                "audit",
                inspect_runtime=False,
            )
        self.assertTrue(transaction.exists())
        self.assertTrue(owner.exists())

    def test_cleanup_refuses_unknown_case_update_state(self) -> None:
        self.collected_result("audit-focused-01")
        update_root = contrib.case_updates_root(self.repo, create=True)
        unknown = update_root / "audit-case.unknown"
        unknown.write_text("do not delete\n", encoding="utf-8")
        unknown.chmod(0o600)

        with self.assertRaisesRegex(contrib.ContribError, "unrecognized entry"):
            contrib.build_cleanup_plan(
                self.repo,
                "audit",
                inspect_runtime=False,
            )
        self.assertTrue(unknown.exists())

    def test_cleanup_retains_and_validates_crash_releasing_lifecycle_locks(self) -> None:
        self.collected_result("lifecycle-focused-01")
        upstream_lock = self.logs / ".lifecycle.lock"
        image_cache_lock = self.image_builds / ".image-cache.lock"
        live_lock = self.live_jobs / ".lifecycle.lock"
        workspace_lock = contrib.workspace_lifecycle_lock_path(self.repo)
        workspace_lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        for lock in (upstream_lock, image_cache_lock, live_lock, workspace_lock):
            lock.touch(mode=0o600)
        source_lock = self.sources / f"{'1' * 40}-origin.bundle.lock"
        source_lock.touch(mode=0o600)
        with contrib.case_update_lock(self.repo):
            pass
        case_update_lock = contrib.case_updates_root(self.repo) / ".lifecycle.lock"

        plan = contrib.build_cleanup_plan(
            self.repo,
            "lifecycle",
            inspect_runtime=False,
        )
        retained = contrib.cleanup_plan_payload(self.repo, plan)["retained"]
        self.assertIn("upstream-tests/image-builds/.image-cache.lock", retained)
        self.assertIn("upstream-tests/logs/.lifecycle.lock", retained)
        self.assertIn("jobs/live/.lifecycle.lock", retained)
        self.assertIn("upstream-tests/workspaces/.lifecycle.lock", retained)
        self.assertIn("case-updates/.lifecycle.lock", retained)
        self.assertEqual(contrib.remove_cleanup_plan(self.repo, plan, plan.digest), 3)
        self.assertTrue(upstream_lock.exists())
        self.assertTrue(image_cache_lock.exists())
        self.assertTrue(live_lock.exists())
        self.assertTrue(workspace_lock.exists())
        self.assertTrue(source_lock.exists())
        self.assertTrue(case_update_lock.exists())

    def test_cleanup_rejects_an_unsafe_retained_lifecycle_lock(self) -> None:
        self.collected_result("audit-focused-01")
        lock = self.logs / ".lifecycle.lock"
        lock.symlink_to(self.logs / "outside")
        with self.assertRaisesRegex(contrib.ContribError, "lifecycle lock"):
            contrib.build_cleanup_plan(
                self.repo,
                "audit",
                inspect_runtime=False,
            )

    def test_cleanup_rejects_a_tampered_workspace_lifecycle_lock(self) -> None:
        self.collected_result("audit-focused-01")
        lock = contrib.workspace_lifecycle_lock_path(self.repo)
        lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock.symlink_to(lock.parent / "replacement")

        with self.assertRaisesRegex(contrib.ContribError, "lifecycle lock"):
            contrib.build_cleanup_plan(
                self.repo,
                "audit",
                inspect_runtime=False,
            )

    def test_cleanup_rejects_a_lifecycle_lock_replaced_during_acquisition(self) -> None:
        self.collected_result("audit-focused-01")
        lock = contrib.cleanup_lock_paths(self.repo)[0]
        real_flock = fcntl.flock
        replaced = False

        def replace_lock(descriptor: int, operation: int) -> None:
            nonlocal replaced
            real_flock(descriptor, operation)
            if replaced or operation != fcntl.LOCK_EX:
                return
            replaced = True
            lock.unlink()
            lock.touch(mode=0o600)

        with (
            patch.object(contrib.fcntl, "flock", side_effect=replace_lock),
            self.assertRaisesRegex(contrib.ContribError, "changed while acquiring"),
        ):
            contrib.build_cleanup_plan(
                self.repo,
                "audit",
                inspect_runtime=False,
            )

        self.assertTrue(lock.exists())

    def test_cleanup_treats_locks_cycle_as_results_not_the_case_lock(self) -> None:
        self.collected_result("locks-focused-01")
        with contrib.case_update_lock(self.repo):
            pass
        lock = contrib.case_updates_root(self.repo) / ".lifecycle.lock"

        plan = contrib.build_cleanup_plan(
            self.repo,
            "locks",
            inspect_runtime=False,
        )

        self.assertEqual(len(plan.targets), 3)
        self.assertTrue(lock.exists())

    def test_cleanup_plan_waits_for_an_active_case_update(self) -> None:
        self.collected_result("serialize-plan-focused-01")

        value = self.run_after_releasing_case_update_lock(
            lambda: contrib.build_cleanup_plan(
                self.repo,
                "serialize-plan",
                inspect_runtime=False,
            )
        )

        self.assertIsInstance(value, contrib.CleanupPlan)

    def test_cleanup_removal_waits_for_an_active_case_update(self) -> None:
        self.collected_result("serialize-remove-focused-01")
        plan = contrib.build_cleanup_plan(
            self.repo,
            "serialize-remove",
            inspect_runtime=False,
        )

        removed = self.run_after_releasing_case_update_lock(
            lambda: contrib.remove_cleanup_plan(self.repo, plan, plan.digest)
        )

        self.assertEqual(removed, 3)

    def test_cleanup_plan_waits_for_every_subsystem_preowner_lock(self) -> None:
        self.collected_result("serialize-all-focused-01")
        self.assertEqual(
            contrib.cleanup_lock_paths(self.repo),
            (
                self.logs / ".lifecycle.lock",
                self.image_builds / ".image-cache.lock",
                self.live_jobs / ".lifecycle.lock",
                self.deb_locks / "terminal.lock",
                contrib.workspace_lifecycle_lock_path(self.repo),
                contrib.case_updates_root(self.repo) / ".lifecycle.lock",
            ),
        )
        for lock in contrib.cleanup_lock_paths(self.repo):
            with self.subTest(lock=lock.relative_to(self.root)):
                value = self.run_after_releasing_cleanup_lock(
                    lock,
                    lambda: contrib.build_cleanup_plan(
                        self.repo,
                        "serialize-all",
                        inspect_runtime=False,
                    ),
                )
                self.assertIsInstance(value, contrib.CleanupPlan)

    def test_cleanup_execution_waits_for_every_subsystem_preowner_lock(self) -> None:
        for index, lock in enumerate(contrib.cleanup_lock_paths(self.repo)):
            cycle = f"serialize-execute-{index}"
            self.collected_result(f"{cycle}-focused-01")
            plan = contrib.build_cleanup_plan(
                self.repo,
                cycle,
                inspect_runtime=False,
            )
            with self.subTest(lock=lock.relative_to(self.root)):
                removed = self.run_after_releasing_cleanup_lock(
                    lock,
                    lambda plan=plan: contrib.remove_cleanup_plan(
                        self.repo,
                        plan,
                        plan.digest,
                    ),
                )
                self.assertEqual(removed, len(plan.targets))

    def test_cleanup_removal_rechecks_a_case_update_started_after_planning(self) -> None:
        self.collected_result("serialize-crash-focused-01")
        self.create_case_fixture("serialize-crash-case")
        plan = contrib.build_cleanup_plan(
            self.repo,
            "serialize-crash",
            inspect_runtime=False,
        )
        _transaction, owner = contrib.case_update_paths(
            self.repo,
            "serialize-crash-case",
        )
        contrib.publish_private_json(
            owner,
            contrib.case_update_owner_payload(
                self.repo,
                "serialize-crash-case",
                "",
                "12345678-1234-4abc-8def-123456789abc",
            ),
            "test case update owner",
        )

        with self.assertRaisesRegex(contrib.ContribError, "runtime state"):
            contrib.remove_cleanup_plan(self.repo, plan, plan.digest)

        self.assertTrue(owner.exists())
        self.assertTrue((self.logs / "serialize-crash-focused-01.status").exists())

    def test_digest_confirmed_cleanup_removes_a_finalized_deb_result(self) -> None:
        status, output = self.collected_deb_result("package-ubuntu-01")
        plan = contrib.build_cleanup_plan(
            self.repo,
            "package",
            inspect_runtime=False,
        )
        log = self.deb_results / "package-ubuntu-01.log"
        remove = self.deb_results / "package-ubuntu-01.remove.json"
        self.assertEqual(
            {target.path for target in plan.targets},
            {status, remove, log, output},
        )
        self.assertEqual(contrib.remove_cleanup_plan(self.repo, plan, plan.digest), 4)
        self.assertFalse(status.exists())
        self.assertFalse(remove.exists())
        self.assertFalse(log.exists())
        self.assertFalse(output.exists())

    def test_cleanup_transaction_recovers_every_upstream_file_kill_point(self) -> None:
        for removed_count in range(4):
            cycle = f"crash-upstream-{removed_count}"
            self.collected_result(f"{cycle}-focused-01")
            plan = contrib.build_cleanup_plan(
                self.repo,
                cycle,
                inspect_runtime=False,
            )
            self.assertEqual(len(plan.targets), 3)
            marker = self.leave_cleanup_transaction(plan, removed_count)
            self.assert_cleanup_transaction_resumes(plan, marker, removed_count)

    def test_cleanup_transaction_recovers_every_live_tree_and_file_kill_point(self) -> None:
        for removed_count in range(5):
            cycle = f"crash-live-{removed_count}"
            self.collected_live_result(f"{cycle}-01")
            plan = contrib.build_cleanup_plan(
                self.repo,
                cycle,
                inspect_runtime=False,
            )
            self.assertEqual(len(plan.targets), 4)
            self.assertIn("live-result-tree", {target.kind for target in plan.targets})
            marker = self.leave_cleanup_transaction(plan, removed_count)
            self.assert_cleanup_transaction_resumes(plan, marker, removed_count)

    def test_cleanup_transaction_recovers_exact_directory_staging(self) -> None:
        self.collected_live_result("crash-tree-partial-01")
        plan = contrib.build_cleanup_plan(
            self.repo,
            "crash-tree-partial",
            inspect_runtime=False,
        )
        marker = self.leave_cleanup_transaction(plan, 0)
        index, target = next(
            (index, target)
            for index, target in enumerate(plan.targets)
            if target.kind == "live-result-tree"
        )
        staging = marker.parent / f".{plan.cycle}.{index}.remove"
        contrib.container_payload.rename_no_replace(target.path, staging)

        resumed = contrib.build_cleanup_plan(
            self.repo,
            plan.cycle,
            inspect_runtime=False,
        )
        self.assertEqual(resumed, plan)
        self.assertEqual(
            contrib.remove_cleanup_plan(self.repo, resumed, resumed.digest),
            len(plan.targets),
        )
        self.assertFalse(marker.exists())
        self.assertFalse(staging.exists())

    def test_cleanup_transaction_resumes_a_partially_deleted_rmtree_phase(self) -> None:
        self.collected_live_result("crash-tree-rmtree-01")
        plan = contrib.build_cleanup_plan(
            self.repo,
            "crash-tree-rmtree",
            inspect_runtime=False,
        )
        marker = self.leave_cleanup_transaction(plan, 0)
        index, target = next(
            (index, target)
            for index, target in enumerate(plan.targets)
            if target.kind == "live-result-tree"
        )
        staging = marker.parent / f".{plan.cycle}.{index}.remove"
        phase = contrib.cleanup_directory_phase_path(marker, plan.cycle, index)
        original_rmtree = contrib.shutil.rmtree
        interrupted = False

        def partially_remove(path: Path, *args: object, **kwargs: object) -> None:
            nonlocal interrupted
            candidate = Path(path)
            if candidate == staging and not interrupted:
                interrupted = True
                staged_file = next(item for item in staging.rglob("*") if item.is_file())
                staged_file.unlink()
                raise RuntimeError("simulated partial rmtree")
            original_rmtree(path, *args, **kwargs)

        with (
            patch.object(contrib.shutil, "rmtree", side_effect=partially_remove),
            self.assertRaisesRegex(RuntimeError, "partial rmtree"),
        ):
            contrib.remove_cleanup_plan(self.repo, plan, plan.digest)

        self.assertFalse(target.path.exists())
        self.assertTrue(staging.is_dir())
        self.assertTrue(phase.is_file())
        resumed = contrib.build_cleanup_plan(
            self.repo,
            plan.cycle,
            inspect_runtime=False,
        )
        self.assertEqual(resumed, plan)
        contrib.remove_cleanup_plan(self.repo, resumed, resumed.digest)
        self.assertFalse(staging.exists())
        self.assertFalse(phase.exists())
        self.assertFalse(marker.exists())

    def test_cleanup_transaction_rejects_mutated_directory_staging(self) -> None:
        self.collected_live_result("crash-tree-mutated-01")
        plan = contrib.build_cleanup_plan(
            self.repo,
            "crash-tree-mutated",
            inspect_runtime=False,
        )
        marker = self.leave_cleanup_transaction(plan, 0)
        index, target = next(
            (index, target)
            for index, target in enumerate(plan.targets)
            if target.kind == "live-result-tree"
        )
        staging = marker.parent / f".{plan.cycle}.{index}.remove"
        contrib.container_payload.rename_no_replace(target.path, staging)
        staged_file = next(path for path in staging.rglob("*") if path.is_file())
        staged_file.unlink()

        with self.assertRaisesRegex(contrib.ContribError, "changed after transaction"):
            contrib.build_cleanup_plan(
                self.repo,
                plan.cycle,
                inspect_runtime=False,
            )

        self.assertTrue(marker.exists())
        self.assertTrue(staging.exists())

    def test_cleanup_transaction_never_overwrites_racing_directory_staging(self) -> None:
        self.collected_live_result("crash-tree-race-01")
        plan = contrib.build_cleanup_plan(
            self.repo,
            "crash-tree-race",
            inspect_runtime=False,
        )
        marker = self.leave_cleanup_transaction(plan, 0)
        index, target = next(
            (index, target)
            for index, target in enumerate(plan.targets)
            if target.kind == "live-result-tree"
        )
        staging = marker.parent / f".{plan.cycle}.{index}.remove"
        staging.mkdir(mode=0o700)
        sentinel = staging / "sentinel"
        sentinel.write_text("do not overwrite\n", encoding="utf-8")

        with self.assertRaisesRegex(contrib.ContribError, "both target and removal staging"):
            contrib.remove_cleanup_plan(self.repo, plan, plan.digest)

        self.assertTrue(target.path.is_dir())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "do not overwrite\n")
        self.assertTrue(marker.exists())

    def test_cleanup_transaction_rejects_symlinked_directory_staging(self) -> None:
        self.collected_live_result("crash-tree-symlink-01")
        plan = contrib.build_cleanup_plan(
            self.repo,
            "crash-tree-symlink",
            inspect_runtime=False,
        )
        marker = self.leave_cleanup_transaction(plan, 0)
        index, target = next(
            (index, target)
            for index, target in enumerate(plan.targets)
            if target.kind == "live-result-tree"
        )
        staging = marker.parent / f".{plan.cycle}.{index}.remove"
        shutil.rmtree(target.path)
        staging.symlink_to(self.root)

        with self.assertRaisesRegex(contrib.ContribError, "not a real owned directory"):
            contrib.build_cleanup_plan(
                self.repo,
                plan.cycle,
                inspect_runtime=False,
            )

        self.assertTrue(marker.exists())
        self.assertTrue(staging.is_symlink())

    def test_cleanup_transaction_recovers_every_deb_file_kill_point(self) -> None:
        for removed_count in range(5):
            cycle = f"crash-deb-{removed_count}"
            self.collected_deb_result(f"{cycle}-ubuntu-01")
            plan = contrib.build_cleanup_plan(
                self.repo,
                cycle,
                inspect_runtime=False,
            )
            self.assertEqual(len(plan.targets), 4)
            self.assertEqual(
                {
                    suffix
                    for target in plan.targets
                    for suffix in (".status.json", ".remove.json", ".log", ".tar")
                    if target.path.name.endswith(suffix)
                },
                {".status.json", ".remove.json", ".log", ".tar"},
            )
            marker = self.leave_cleanup_transaction(plan, removed_count)
            self.assert_cleanup_transaction_resumes(plan, marker, removed_count)

    def test_cleanup_transaction_rejects_tampering_and_unknown_state(self) -> None:
        self.collected_result("tampered-cycle-focused-01")
        plan = contrib.build_cleanup_plan(
            self.repo,
            "tampered-cycle",
            inspect_runtime=False,
        )
        marker = self.leave_cleanup_transaction(plan, 0)
        record = json.loads(marker.read_text(encoding="utf-8"))
        record["plan"]["targets"][0]["fingerprint"] = "0" * 64
        marker.write_bytes(contrib.canonical_json_bytes(record))

        with self.assertRaisesRegex(contrib.ContribError, "digest|differs"):
            contrib.build_cleanup_plan(
                self.repo,
                "tampered-cycle",
                inspect_runtime=False,
            )
        self.assertTrue(marker.exists())

    def test_cleanup_transaction_rejects_a_changed_remaining_target(self) -> None:
        self.collected_result("tampered-target-focused-01")
        plan = contrib.build_cleanup_plan(
            self.repo,
            "tampered-target",
            inspect_runtime=False,
        )
        marker = self.leave_cleanup_transaction(plan, 1)
        remaining = plan.targets[1]
        remaining.path.write_bytes(b"changed after cleanup began\n")

        with self.assertRaisesRegex(contrib.ContribError, "changed after planning"):
            contrib.build_cleanup_plan(
                self.repo,
                "tampered-target",
                inspect_runtime=False,
            )
        self.assertTrue(marker.exists())

    def test_cleanup_transaction_root_rejects_an_unknown_entry(self) -> None:
        self.collected_result("unknown-transaction-focused-01")
        root = contrib.cycle_cleanup_transaction_root(self.repo, create=True)
        unknown = root / "unknown"
        unknown.write_text("do not delete\n", encoding="utf-8")
        unknown.chmod(0o600)

        with self.assertRaisesRegex(
            contrib.ContribError,
            "unexpected state|unrecognized entry",
        ):
            contrib.build_cleanup_plan(
                self.repo,
                "unknown-transaction",
                inspect_runtime=False,
            )
        self.assertTrue(unknown.exists())

    def test_cleanup_refuses_a_missing_target_before_transaction_publication(self) -> None:
        self.collected_result("missing-before-marker-focused-01")
        plan = contrib.build_cleanup_plan(
            self.repo,
            "missing-before-marker",
            inspect_runtime=False,
        )
        plan.targets[0].path.unlink()

        with self.assertRaisesRegex(contrib.ContribError, "changed after planning"):
            contrib.remove_cleanup_plan(self.repo, plan, plan.digest)
        self.assertIsNone(contrib.load_pending_cleanup_transaction(self.repo))

    def test_cleanup_rejects_deb_manifest_provenance_tampering(self) -> None:
        status_path, _output = self.collected_deb_result("package-ubuntu-01")
        status = json.loads(status_path.read_text(encoding="utf-8"))
        status["manifest"]["source_commit"] = "9" * 40
        status_path.write_text(json.dumps(status) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(contrib.ContribError, "provenance is inconsistent"):
            contrib.build_cleanup_plan(
                self.repo,
                "package",
                inspect_runtime=False,
            )

    def test_cleanup_accepts_a_current_failed_deb_result(self) -> None:
        status_path, output = self.collected_deb_result("package-ubuntu-01")
        output.unlink()
        status = json.loads(status_path.read_text(encoding="utf-8"))
        status.update(
            {
                "container": {},
                "exit_code": 1,
                "manifest": {},
                "output_sha256": "",
                "validation_error": "worker failed",
                "validation_ok": False,
            }
        )
        status_path.write_bytes(contrib.canonical_json_bytes(status))
        self.write_deb_remove_transaction("package-ubuntu-01", status)
        plan = contrib.build_cleanup_plan(
            self.repo,
            "package",
            inspect_runtime=False,
        )
        self.assertEqual(
            {target.path for target in plan.targets},
            {
                status_path,
                self.deb_results / "package-ubuntu-01.log",
                self.deb_results / "package-ubuntu-01.remove.json",
            },
        )

    def test_cleanup_rejects_a_noncurrent_deb_status_schema(self) -> None:
        status_path, _output = self.collected_deb_result("package-ubuntu-01")
        status = json.loads(status_path.read_text(encoding="utf-8"))
        status["unexpected"] = True
        status_path.write_text(json.dumps(status) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(contrib.ContribError, "current owned schema"):
            contrib.build_cleanup_plan(
                self.repo,
                "package",
                inspect_runtime=False,
            )

    def test_cleanup_requires_the_deb_removal_transaction(self) -> None:
        self.collected_deb_result("package-ubuntu-01")
        marker = self.deb_results / "package-ubuntu-01.remove.json"
        marker.unlink()

        with self.assertRaisesRegex(contrib.ContribError, "DEB removal transaction"):
            contrib.build_cleanup_plan(
                self.repo,
                "package",
                inspect_runtime=False,
            )

    def test_cleanup_rejects_a_tampered_deb_removal_transaction(self) -> None:
        self.collected_deb_result("package-ubuntu-01")
        marker = self.deb_results / "package-ubuntu-01.remove.json"
        transaction = json.loads(marker.read_text(encoding="utf-8"))
        transaction["owner_record"]["runner_sha256"] = "0" * 64
        marker.write_bytes(contrib.canonical_json_bytes(transaction))

        with self.assertRaisesRegex(contrib.ContribError, "digest is inconsistent"):
            contrib.build_cleanup_plan(
                self.repo,
                "package",
                inspect_runtime=False,
            )

    def test_cleanup_refuses_deb_runtime_state(self) -> None:
        self.collected_result("package-focused-01")
        runtime = self.deb_runs / "package-ubuntu-01"
        runtime.mkdir(mode=0o700)
        with self.assertRaisesRegex(contrib.ContribError, "runtime state"):
            contrib.build_cleanup_plan(
                self.repo,
                "package",
                inspect_runtime=False,
            )

    def test_cleanup_retains_validated_deb_cache_and_terminal_lock(self) -> None:
        self.collected_result("audit-focused-01")
        cache, selection_sha256 = self.retained_deb_selection_cache()
        self.deb_sources.mkdir(mode=0o700)
        legacy = self.deb_sources / f"{'1' * 40}-checkout.bundle"
        legacy.write_bytes(b"legacy cache\n")
        legacy.chmod(0o600)
        self.deb_locks.mkdir(mode=0o700)
        terminal_lock = self.deb_locks / "terminal.lock"
        terminal_lock.touch(mode=0o600)
        image_locks = self.deb_locks / "images"
        image_locks.mkdir(mode=0o700)
        image_lock = image_locks / f"ubuntu-26.04-{'8' * 64}.lock"
        image_lock.touch(mode=0o600)
        with patch.object(
            contrib,
            "deb_selection_semantic_digest",
            return_value=selection_sha256,
        ):
            plan = contrib.build_cleanup_plan(
                self.repo,
                "audit",
                inspect_runtime=False,
            )
            retained = contrib.cleanup_plan_payload(self.repo, plan)["retained"]
            self.assertIn("deb-packages/selections/", retained)
            self.assertIn("deb-packages/locks/terminal.lock", retained)
            self.assertIn("deb-packages/locks/images/", retained)
            self.assertEqual(
                contrib.remove_cleanup_plan(self.repo, plan, plan.digest),
                3,
            )
        self.assertTrue(cache.exists())
        self.assertTrue(terminal_lock.exists())
        self.assertTrue(image_lock.exists())
        self.assertTrue(legacy.exists())

    def test_cleanup_rejects_tampered_retained_deb_selection_cache(self) -> None:
        self.collected_result("audit-focused-01")
        cache, selection_sha256 = self.retained_deb_selection_cache()
        (cache / "lab" / "control.txt").write_text("tampered\n", encoding="utf-8")
        with (
            patch.object(
                contrib,
                "deb_selection_semantic_digest",
                return_value=selection_sha256,
            ),
            self.assertRaisesRegex(contrib.ContribError, "provenance is inconsistent"),
        ):
            contrib.build_cleanup_plan(
                self.repo,
                "audit",
                inspect_runtime=False,
            )

    def test_cleanup_refuses_incomplete_deb_selection_publication(self) -> None:
        self.collected_result("audit-focused-01")
        self.deb_selections.mkdir(mode=0o700)
        marker = self.deb_selections / ".selection-cache.partial.owner.json"
        marker.write_text("{}\n", encoding="utf-8")
        marker.chmod(0o600)
        with self.assertRaisesRegex(contrib.ContribError, "runtime state"):
            contrib.build_cleanup_plan(
                self.repo,
                "audit",
                inspect_runtime=False,
            )

    def test_cleanup_refuses_incomplete_deb_source_publication(self) -> None:
        self.collected_result("audit-focused-01")
        self.deb_sources.mkdir(mode=0o700)
        marker = self.deb_sources / ".source-snapshot.partial.owner.json"
        marker.write_text("{}\n", encoding="utf-8")
        marker.chmod(0o600)
        with self.assertRaisesRegex(contrib.ContribError, "runtime state"):
            contrib.build_cleanup_plan(
                self.repo,
                "audit",
                inspect_runtime=False,
            )

    def test_cleanup_rejects_unrecognized_deb_terminal_lock(self) -> None:
        self.collected_result("audit-focused-01")
        self.deb_locks.mkdir(mode=0o700)
        unexpected = self.deb_locks / "other.lock"
        unexpected.touch(mode=0o600)
        with self.assertRaisesRegex(contrib.ContribError, "unrecognized entry"):
            contrib.build_cleanup_plan(
                self.repo,
                "audit",
                inspect_runtime=False,
            )

    def test_cleanup_rejects_unrecognized_deb_image_lock(self) -> None:
        self.collected_result("audit-focused-01")
        self.deb_locks.mkdir(mode=0o700)
        image_locks = self.deb_locks / "images"
        image_locks.mkdir(mode=0o700)
        unexpected = image_locks / "ubuntu-26.04-current.lock"
        unexpected.touch(mode=0o600)

        with self.assertRaisesRegex(contrib.ContribError, "unrecognized entry"):
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

    def test_cleanup_preflights_every_target_before_removing_any(self) -> None:
        self.collected_result("audit-focused-01")
        plan = contrib.build_cleanup_plan(
            self.repo,
            "audit",
            inspect_runtime=False,
        )
        log = self.logs / "audit-focused-01.log"
        status = self.logs / "audit-focused-01.status"
        status.write_text(
            status.read_text(encoding="utf-8") + "changed=after-plan\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(contrib.ContribError, "changed after planning"):
            contrib.remove_cleanup_plan(self.repo, plan, plan.digest)
        self.assertTrue(log.exists())
        self.assertTrue(status.exists())

    def test_cleanup_refuses_a_parent_replaced_by_a_symlink_after_planning(self) -> None:
        self.collected_result("audit-focused-01")
        plan = contrib.build_cleanup_plan(
            self.repo,
            "audit",
            inspect_runtime=False,
        )
        original_logs = self.logs.with_name("logs-original")
        self.logs.rename(original_logs)
        outside = self.root / "outside-logs"
        outside.mkdir(mode=0o700)
        for source in original_logs.iterdir():
            destination = outside / source.name
            destination.write_bytes(source.read_bytes())
            destination.chmod(0o600)
        self.logs.symlink_to(outside)
        with self.assertRaisesRegex(
            contrib.ContribError,
            "cleanup lock directory|private path is not a real directory|target parent",
        ):
            contrib.remove_cleanup_plan(self.repo, plan, plan.digest)
        self.assertTrue((outside / "audit-focused-01.log").exists())

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

    def test_tree_fingerprint_rejects_an_other_writable_directory(self) -> None:
        tree = self.root / "unsafe-directory-tree"
        tree.mkdir(mode=0o700)
        payload = tree / "payload"
        payload.mkdir(mode=0o700)
        payload.chmod(0o707)

        with self.assertRaisesRegex(contrib.ContribError, "other-writable directory"):
            contrib.secure_tree_fingerprint(tree)


class ManifestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo = contrib.DEFAULT_REPO
        contrib.verify_repo(cls.repo)
        cls.revision = contrib.rev_parse(cls.repo, "refs/remotes/origin/master")

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

    def test_each_patch_resolves_against_cached_fork_master(self) -> None:
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
        (self.repo / ".gitignore").write_text("/.artifacts/\n", encoding="utf-8")
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
