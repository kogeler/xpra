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
from unittest.mock import call, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import contrib


def command(*arguments: str, cwd: Path | None = None) -> str:
    if arguments[:1] == ("git",):
        # Fixture commits must never reach for the operator's signing key.
        arguments = ("git", "-c", "commit.gpgsign=false", *arguments[1:])
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

    def test_public_repo_sync_rejects_dirty_develop_before_fetch(self) -> None:
        repo = Path("/tmp/xpra-fork")
        with (
            patch.object(contrib, "current_branch", return_value="develop"),
            patch.object(
                contrib,
                "require_clean",
                side_effect=contrib.ContribError("repository has local changes"),
            ),
            patch.object(contrib, "sync_repo") as sync,
            self.assertRaisesRegex(contrib.ContribError, "local changes"),
        ):
            contrib.repo_sync(repo)
        sync.assert_not_called()

    def test_public_repo_sync_rejects_wrong_branch_before_fetch(self) -> None:
        repo = Path("/tmp/xpra-fork")
        with (
            patch.object(contrib, "current_branch", return_value="master"),
            patch.object(contrib, "require_clean") as require_clean,
            patch.object(contrib, "sync_repo") as sync,
            self.assertRaisesRegex(contrib.ContribError, "requires the develop branch"),
        ):
            contrib.repo_sync(repo)
        require_clean.assert_not_called()
        sync.assert_not_called()

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
        (self.repo / "fork-maintenance").mkdir()
        (self.repo / "fork-maintenance" / "fork.txt").write_text("fork\n", encoding="utf-8")
        command("git", "add", "fork-maintenance", cwd=self.repo)
        command("git", "commit", "-q", "-m", "fork automation", cwd=self.repo)
        (self.repo / "product.txt").write_text("case\n", encoding="utf-8")
        command("git", "add", "product.txt", cwd=self.repo)
        command("git", "commit", "-q", "-m", "fork case", "--trailer", "Fork-Case: one", cwd=self.repo)
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
            patch.object(contrib, "fetch_master", side_effect=AssertionError("unexpected fetch")),
            patch.object(contrib, "sync_repo", side_effect=AssertionError("unexpected sync")),
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
        self.assertEqual(command("git", "rev-parse", "master", cwd=self.repo), self.base)
        self.assertEqual(command("git", "remote", cwd=self.repo), "")
        verify, sync = self.sync_mocks()
        with verify, sync:
            self.assertEqual(contrib.patch_start_check(self.repo), self.base)

    def test_develop_rebase_never_signs_replayed_commits(self) -> None:
        # A configured but unusable signer fails any signing attempt.
        command("git", "config", "commit.gpgsign", "true", cwd=self.repo)
        command("git", "config", "gpg.program", "false", cwd=self.repo)
        verify, sync = self.sync_mocks()
        with verify, sync:
            self.assertEqual(contrib.develop_rebase(self.repo), self.base)
        self.assertEqual(
            subprocess.run(
                ("git", "log", "-1", "--format=%G?", "develop"),
                cwd=self.repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
            "N",
        )

    def test_patch_start_rejects_merge_transfer(self) -> None:
        command("git", "merge", "-q", "--no-edit", "master", cwd=self.repo)
        verify, sync = self.sync_mocks()
        with (
            verify,
            sync,
            self.assertRaisesRegex(contrib.ContribError, "merge commits"),
        ):
            contrib.patch_start_check(self.repo)

    def test_rebase_rejects_dirty_work_without_preserving_or_discarding_it(self) -> None:
        (self.repo / "fork-maintenance" / "fork.txt").write_text("pending operator work\n", encoding="utf-8")
        before = command("git", "status", "--porcelain=v1", cwd=self.repo)
        with self.assertRaises(contrib.ContribError):
            contrib.develop_rebase(self.repo)
        self.assertEqual(command("git", "rev-parse", "HEAD", cwd=self.repo), self.old_develop)
        self.assertEqual(command("git", "rev-parse", "master", cwd=self.repo), self.base)
        self.assertEqual(command("git", "status", "--porcelain=v1", cwd=self.repo), before)


class ObjectBackfillTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.source = root / "source"
        command("git", "init", "-q", "-b", "master", str(self.source))
        for key, value in (("uploadpack.allowFilter", "true"), ("uploadpack.allowAnySHA1InWant", "true")):
            command("git", "config", key, value, cwd=self.source)
        (self.source / "payload.txt").write_text("content-addressed payload\n", encoding="utf-8")
        command("git", "add", "payload.txt", cwd=self.source)
        command("git", "-c", "user.name=Fixture", "-c", "user.email=f@example.invalid", "commit", "-qm", "base",
                cwd=self.source)
        self.repo = root / "partial"
        command("git", "clone", "-q", "--filter=blob:none", "--no-checkout", self.source.as_uri(), str(self.repo))
        # The promisor itself must never be contacted by automation.
        command("git", "remote", "set-url", "origin", "ssh://promisor.invalid/unreachable.git", cwd=self.repo)
        self.blob = command("git", "rev-parse", "master:payload.txt", cwd=self.source)

    def refs(self) -> str:
        return command("git", "for-each-ref", "--format=%(refname) %(objectname)", cwd=self.repo)

    def test_missing_objects_do_not_trigger_a_lazy_fetch(self) -> None:
        self.assertEqual(contrib.missing_objects(self.repo, "refs/heads/master"), [self.blob])

    def test_backfill_fetches_exact_objects_without_touching_refs(self) -> None:
        refs = self.refs()
        count = contrib.backfill_missing_objects(self.repo, "refs/heads/master", source=self.source.as_uri())
        self.assertEqual(count, 1)
        self.assertEqual(contrib.missing_objects(self.repo, "refs/heads/master"), [])
        self.assertEqual(self.refs(), refs)
        self.assertFalse((self.repo / ".git" / "FETCH_HEAD").exists())
        self.assertEqual(contrib.backfill_missing_objects(self.repo, "refs/heads/master"), 0)

    def test_backfill_fails_closed_when_the_source_lacks_objects(self) -> None:
        empty = Path(self.temporary.name) / "empty"
        command("git", "init", "-q", "--bare", str(empty))
        with self.assertRaises(contrib.ContribError):
            contrib.backfill_missing_objects(self.repo, "refs/heads/master", source=empty.as_uri())
        self.assertEqual(contrib.missing_objects(self.repo, "refs/heads/master"), [self.blob])


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
        command(
            "git",
            "update-ref",
            "refs/remotes/origin/master",
            self.base,
            cwd=self.repo,
        )
        control = self.repo / "fork-maintenance"
        control.mkdir()
        (control / "draft.txt").write_text("control\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_remote_transport_and_location_do_not_change_local_admission(self) -> None:
        command("git", "remote", "add", "origin", "git@github.com:kogeler/xpra.git", cwd=self.repo)
        for url in ("git@github.com:kogeler/xpra.git", "ssh://git@example.invalid/fork", "/local/mirror"):
            with self.subTest(url=url):
                command("git", "remote", "set-url", "origin", url, cwd=self.repo)
                before = command("git", "status", "--porcelain=v1", cwd=self.repo)
                with patch.object(contrib, "fetch_master", side_effect=AssertionError("unexpected fetch")):
                    state = contrib.isolated_start_check(self.repo)
                self.assertEqual(state.source_commit, self.base)
                self.assertEqual(command("git", "remote", "get-url", "origin", cwd=self.repo), url)
                self.assertEqual(command("git", "status", "--porcelain=v1", cwd=self.repo), before)

    def test_allows_dirty_control_plane_without_touching_the_branch(self) -> None:
        status = command("git", "status", "--porcelain=v1", cwd=self.repo)
        with patch.object(contrib, "verify_repo"):
            state = contrib.isolated_start_check(self.repo)
        self.assertEqual(state.branch, "develop")
        self.assertEqual(state.head, self.base)
        self.assertEqual(state.source_commit, self.base)
        self.assertTrue(state.source_in_head)
        self.assertEqual(command("git", "status", "--porcelain=v1", cwd=self.repo), status)

    def test_local_master_can_advance_without_fetching_remote_tracking_refs(self) -> None:
        (self.repo / "source.py").write_text("VALUE = 2\n", encoding="utf-8")
        command("git", "add", "source.py", cwd=self.repo)
        command("git", "commit", "-q", "-m", "new local source", cwd=self.repo)
        local_master = command("git", "rev-parse", "HEAD", cwd=self.repo)
        command("git", "update-ref", "refs/heads/master", local_master, cwd=self.repo)
        before = command("git", "status", "--porcelain=v1", cwd=self.repo)
        with patch.object(contrib, "fetch_master", side_effect=AssertionError("unexpected fetch")):
            state = contrib.isolated_start_check(self.repo)
        self.assertEqual(state.source_commit, local_master)
        self.assertEqual(command("git", "rev-parse", "refs/remotes/origin/master", cwd=self.repo), self.base)
        self.assertEqual(command("git", "status", "--porcelain=v1", cwd=self.repo), before)
        with self.assertRaisesRegex(contrib.ContribError, "without a Fork-Case trailer"):
            contrib.ci_start_check(self.repo)

    def test_accepts_current_develop_when_cached_origin_master_is_newer(self) -> None:
        tree = command("git", "rev-parse", "HEAD^{tree}", cwd=self.repo)
        newer_master = command(
            "git",
            "commit-tree",
            tree,
            "-p",
            self.base,
            "-m",
            "later upstream source",
            cwd=self.repo,
        )
        command(
            "git",
            "update-ref",
            "refs/remotes/origin/master",
            newer_master,
            cwd=self.repo,
        )

        with patch.object(contrib, "verify_repo"):
            state = contrib.isolated_start_check(self.repo)

        self.assertEqual(state.head, self.base)
        self.assertEqual(state.source_commit, self.base)
        self.assertEqual(
            command("git", "rev-parse", "refs/remotes/origin/master", cwd=self.repo),
            newer_master,
        )

    def test_rejects_a_dirty_host_source_path(self) -> None:
        (self.repo / "source.py").write_text("VALUE = 2\n", encoding="utf-8")
        with (
            patch.object(contrib, "verify_repo"),
            self.assertRaisesRegex(contrib.ContribError, "commit these product changes"),
        ):
            contrib.isolated_start_check(self.repo)

    def test_rejects_a_dirty_root_file_that_only_looks_allowed(self) -> None:
        (self.repo / "AGENTS.md.backup").write_text("not controlled\n", encoding="utf-8")
        with (
            patch.object(contrib, "verify_repo"),
            self.assertRaisesRegex(contrib.ContribError, "commit these product changes"),
        ):
            contrib.isolated_start_check(self.repo)

    def test_rejects_multiple_embedded_source_merge_bases(self) -> None:
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
        command(
            "git",
            "update-ref",
            "refs/remotes/origin/master",
            master,
            cwd=self.repo,
        )

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
            self.assertRaisesRegex(contrib.ContribError, "without a Fork-Case trailer"),
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
        with self.assertRaisesRegex(contrib.ContribError, "without a Fork-Case trailer"):
            contrib.checkout_source_check(self.repo)

    def test_rejects_a_committed_root_file_that_only_looks_allowed(self) -> None:
        (self.repo / ".gitignore.evil").write_text("not controlled\n", encoding="utf-8")
        command("git", "add", ".gitignore.evil", cwd=self.repo)
        command("git", "commit", "-q", "-m", "bad lookalike", cwd=self.repo)
        with self.assertRaisesRegex(contrib.ContribError, "without a Fork-Case trailer"):
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
            if target.kind == "live-result-tree":
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
                        "keyboard_scenario": None,
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
                            "network_profile",
                            "render_node",
                            "result",
                            "reviewed_selection",
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

    def test_cleanup_rejects_reconstruct_as_an_upstream_runner_mode(self) -> None:
        self.collected_result("audit-focused-01")
        status = self.logs / "audit-focused-01.status"
        status.write_text(
            status.read_text(encoding="utf-8").replace(
                "patch_mode=patched\n",
                "patch_mode=reconstruct\n",
            ),
            encoding="utf-8",
        )
        self.refresh_upstream_remove_transaction("audit-focused-01")
        with self.assertRaisesRegex(
            contrib.ContribError,
            "upstream test provenance is invalid",
        ):
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

    def bind_live_endpoint_provenance(
        self, status_path: Path, changes: dict[str, str]
    ) -> None:
        status = json.loads(status_path.read_text(encoding="utf-8"))
        status["input_provenance"].update(changes)
        status_path.write_text(json.dumps(status) + "\n", encoding="utf-8")
        marker = self.live_jobs / f"{status['run']}.remove.json"
        payload = json.loads(marker.read_text(encoding="utf-8"))
        payload["record"]["input_provenance"] = status["input_provenance"]
        marker.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        self.refresh_live_remove_transaction(status["run"])

    def test_digest_confirmed_cleanup_accepts_current_stack_and_retired_case_endpoints(self) -> None:
        for index, selection in enumerate(
            ("stacks/develop", "cases/x11-client-clipboard-events", "cases/wayland-subsurface-stream-ownership")
        ):
            with self.subTest(selection=selection):
                cycle = f"shared-endpoints-{index}"
                name = f"{cycle}-live-01"
                status, log, result = self.collected_live_result(name)
                provenance = json.loads(status.read_text(encoding="utf-8"))[
                    "input_provenance"
                ]
                changes = {
                    "client_selection": selection,
                    "server_selection": selection,
                }
                for field in (
                    "selection_sha256",
                    "selection_resolution_sha256",
                    "context_sha256",
                    "context_archive_sha256",
                ):
                    changes[f"client_{field}"] = provenance[f"server_{field}"]
                self.bind_live_endpoint_provenance(status, changes)

                plan = contrib.build_cleanup_plan(
                    self.repo, cycle, inspect_runtime=False
                )
                marker = self.live_jobs / f"{name}.remove.json"
                expected = {status, log, marker, result}
                self.assertEqual({target.path for target in plan.targets}, expected)
                self.assertEqual(
                    contrib.remove_cleanup_plan(self.repo, plan, plan.digest), 4
                )
                self.assertTrue(all(not path.exists() for path in expected))

    def test_cleanup_rejects_unbound_shared_case_endpoints(self) -> None:
        clipboard = "cases/x11-client-clipboard-events"
        subsurface = "cases/wayland-subsurface-stream-ownership"
        mismatches = [
            (clipboard, "master", None),
            (clipboard, "stacks/develop", None),
            (clipboard, subsurface, None),
            ("cases/unreviewed-case", "cases/unreviewed-case", None),
            ("cases/unreviewed-case", clipboard, None),
            ("stacks/partial", "stacks/partial", None),
        ]
        for selection in (clipboard, subsurface, "stacks/develop"):
            for field in (
                "selection_sha256",
                "selection_resolution_sha256",
                "context_sha256",
                "context_archive_sha256",
            ):
                mismatches.append((selection, selection, field))
        for index, (client, server, mismatch) in enumerate(mismatches):
            with self.subTest(client=client, server=server, mismatch=mismatch):
                cycle = f"unbound-endpoints-{index}"
                name = f"{cycle}-live-01"
                status, log, result = self.collected_live_result(name)
                provenance = json.loads(status.read_text(encoding="utf-8"))[
                    "input_provenance"
                ]
                changes = {"client_selection": client, "server_selection": server}
                for field in (
                    "selection_sha256",
                    "selection_resolution_sha256",
                    "context_sha256",
                    "context_archive_sha256",
                ):
                    changes[f"client_{field}"] = provenance[f"server_{field}"]
                if mismatch:
                    changes[f"client_{mismatch}"] = "0" * 64
                # Rebind the removal record and status hash as well: an
                # unrelated transaction mismatch must not mask this boundary.
                self.bind_live_endpoint_provenance(status, changes)
                with self.assertRaisesRegex(
                    contrib.ContribError, "input provenance|endpoint"
                ):
                    contrib.build_cleanup_plan(self.repo, cycle, inspect_runtime=False)
                marker = self.live_jobs / f"{name}.remove.json"
                self.assertTrue(all(path.exists() for path in (status, log, marker, result)))

    def test_cleanup_accepts_live_keyboard_scenario_provenance(self) -> None:
        status_path, _log, result = self.collected_live_result("audit-live-01")
        scenario = result / "inputs" / "keyboard-scenario.json"
        scenario.write_bytes(b'{"schema":1}\n')
        scenario.chmod(0o600)
        status = json.loads(status_path.read_text(encoding="utf-8"))
        status["input_provenance"]["keyboard_scenario"] = {
            "name": "wayland-keyboard-groups",
            "path": (
                "cases/wayland-client-keymap-sync/tests/"
                "live-wayland-keyboard.json"
            ),
            "schema": 1,
            "sha256": hashlib.sha256(scenario.read_bytes()).hexdigest(),
        }
        status_path.write_text(json.dumps(status) + "\n", encoding="utf-8")
        marker = self.live_jobs / "audit-live-01.remove.json"
        payload = json.loads(marker.read_text(encoding="utf-8"))
        payload["record"]["input_provenance"] = status["input_provenance"]
        payload["status_sha256"] = hashlib.sha256(status_path.read_bytes()).hexdigest()
        marker.write_text(json.dumps(payload) + "\n", encoding="utf-8")

        plan = contrib.build_cleanup_plan(
            self.repo,
            "audit",
            inspect_runtime=False,
        )
        self.assertEqual(len(plan.targets), 4)

    def test_cleanup_rejects_missing_live_keyboard_scenario_provenance(self) -> None:
        status_path, _log, _result = self.collected_live_result("audit-live-01")
        status = json.loads(status_path.read_text(encoding="utf-8"))
        status["input_provenance"].pop("keyboard_scenario")
        status_path.write_text(json.dumps(status) + "\n", encoding="utf-8")

        with self.assertRaisesRegex(contrib.ContribError, "input provenance"):
            contrib.build_cleanup_plan(
                self.repo,
                "audit",
                inspect_runtime=False,
            )

    def test_cleanup_rejects_invalid_live_keyboard_scenario_provenance(self) -> None:
        invalid_values = (
            {},
            {
                "name": "wayland-keyboard-groups",
                "path": (
                    "cases/wayland-client-keymap-sync/tests/"
                    "live-wayland-keyboard.json"
                ),
                "schema": True,
                "sha256": "9" * 64,
            },
            {
                "name": "../keyboard",
                "path": (
                    "cases/wayland-client-keymap-sync/tests/"
                    "live-wayland-keyboard.json"
                ),
                "schema": 1,
                "sha256": "9" * 64,
            },
            {
                "name": "wayland-keyboard-groups",
                "path": "../live-wayland-keyboard.json",
                "schema": 1,
                "sha256": "9" * 64,
            },
            {
                "name": "wayland-keyboard-groups",
                "path": (
                    "cases/wayland-client-keymap-sync/tests/"
                    "live-wayland-keyboard.json"
                ),
                "schema": 1,
                "sha256": "invalid",
            },
        )
        for index, value in enumerate(invalid_values):
            with self.subTest(index=index):
                name = f"invalid-live-{index}-01"
                status_path, _log, _result = self.collected_live_result(name)
                status = json.loads(status_path.read_text(encoding="utf-8"))
                status["input_provenance"]["keyboard_scenario"] = value
                status_path.write_text(json.dumps(status) + "\n", encoding="utf-8")
                with self.assertRaisesRegex(
                    contrib.ContribError,
                    "keyboard scenario provenance",
                ):
                    contrib.build_cleanup_plan(
                        self.repo,
                        f"invalid-live-{index}",
                        inspect_runtime=False,
                    )

    def test_cleanup_rejects_unbound_live_keyboard_scenario_data(self) -> None:
        _status, _log, result = self.collected_live_result("unexpected-live-01")
        scenario = result / "inputs" / "keyboard-scenario.json"
        scenario.write_bytes(b"unexpected\n")
        scenario.chmod(0o600)

        with self.assertRaisesRegex(contrib.ContribError, "unexpected keyboard"):
            contrib.build_cleanup_plan(
                self.repo,
                "unexpected-live",
                inspect_runtime=False,
            )

    def test_cleanup_rejects_missing_or_changed_live_keyboard_scenario_data(
        self,
    ) -> None:
        for index, payload in enumerate((None, b"changed\n")):
            with self.subTest(index=index):
                name = f"scenario-data-{index}-01"
                status_path, _log, result = self.collected_live_result(name)
                scenario = result / "inputs" / "keyboard-scenario.json"
                expected = b'{"schema":1}\n'
                if payload is not None:
                    scenario.write_bytes(payload)
                    scenario.chmod(0o600)
                status = json.loads(status_path.read_text(encoding="utf-8"))
                status["input_provenance"]["keyboard_scenario"] = {
                    "name": "wayland-keyboard-groups",
                    "path": (
                        "cases/wayland-client-keymap-sync/tests/"
                        "live-wayland-keyboard.json"
                    ),
                    "schema": 1,
                    "sha256": hashlib.sha256(expected).hexdigest(),
                }
                status_path.write_text(json.dumps(status) + "\n", encoding="utf-8")
                expected_error = "unavailable" if payload is None else "digest"
                with self.assertRaisesRegex(contrib.ContribError, expected_error):
                    contrib.build_cleanup_plan(
                        self.repo,
                        f"scenario-data-{index}",
                        inspect_runtime=False,
                    )

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

    def test_cleanup_requires_the_reviewed_live_selection_check(self) -> None:
        status_path, _log, _result = self.collected_live_result("audit-live-01")
        status = json.loads(status_path.read_text(encoding="utf-8"))
        status["report_checks"].pop("reviewed_selection")
        status_path.write_text(json.dumps(status) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(contrib.ContribError, "validation state"):
            contrib.build_cleanup_plan(
                self.repo,
                "audit",
                inspect_runtime=False,
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
                    "owner": "xpra-fork-maintenance-live-job",
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
                    "owner": "xpra-fork-maintenance-live-job",
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


    def test_cleanup_retains_and_validates_crash_releasing_lifecycle_locks(self) -> None:
        self.collected_result("lifecycle-focused-01")
        upstream_lock = self.logs / ".lifecycle.lock"
        image_cache_lock = self.image_builds / ".image-cache.lock"
        live_lock = self.live_jobs / ".lifecycle.lock"
        for lock in (upstream_lock, image_cache_lock, live_lock):
            lock.touch(mode=0o600)
        source_locks = [
            self.sources / f"{'1' * 40}-{source}.bundle.lock"
            for source in ("local", "origin", "upstream")
        ]
        for source_lock in source_locks:
            source_lock.touch(mode=0o600)
        plan = contrib.build_cleanup_plan(
            self.repo,
            "lifecycle",
            inspect_runtime=False,
        )
        retained = contrib.cleanup_plan_payload(self.repo, plan)["retained"]
        self.assertIn("upstream-tests/image-builds/.image-cache.lock", retained)
        self.assertIn("upstream-tests/logs/.lifecycle.lock", retained)
        self.assertIn("jobs/live/.lifecycle.lock", retained)
        self.assertEqual(contrib.remove_cleanup_plan(self.repo, plan, plan.digest), 3)
        self.assertTrue(upstream_lock.exists())
        self.assertTrue(image_cache_lock.exists())
        self.assertTrue(live_lock.exists())
        self.assertTrue(all(source_lock.exists() for source_lock in source_locks))

    def test_cleanup_rejects_invalid_source_bundle_locks(self) -> None:
        self.collected_result("audit-focused-01")
        for name, mode in (
            (f"{'1' * 40}-unknown.bundle.lock", 0o600),
            (f"{'1' * 39}-local.bundle.lock", 0o600),
            (f"{'1' * 40}-local.bundle.lock", 0o644),
        ):
            with self.subTest(name=name, mode=mode):
                lock = self.sources / name
                lock.touch(mode=mode)
                lock.chmod(mode)
                try:
                    with self.assertRaisesRegex(contrib.ContribError, "upstream source-bundle lock"):
                        contrib.build_cleanup_plan(self.repo, "audit", inspect_runtime=False)
                    self.assertTrue(lock.exists())
                finally:
                    lock.unlink()

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


    def test_cleanup_plan_waits_for_every_subsystem_preowner_lock(self) -> None:
        self.collected_result("serialize-all-focused-01")
        self.assertEqual(
            contrib.cleanup_lock_paths(self.repo),
            (
                self.logs / ".lifecycle.lock",
                self.image_builds / ".image-cache.lock",
                self.live_jobs / ".lifecycle.lock",
                self.deb_locks / "terminal.lock",
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

    def test_cleanup_does_not_reparse_an_incompatible_historical_deb_cache(
        self,
    ) -> None:
        self.collected_result("audit-focused-01")
        cache, historical_sha256 = self.retained_deb_selection_cache()
        active_sha256 = "c" * 64
        self.assertNotEqual(historical_sha256, active_sha256)
        with patch.object(
            contrib,
            "deb_selection_semantic_digest",
            return_value=active_sha256,
        ) as semantic_digest:
            plan = contrib.build_cleanup_plan(
                self.repo,
                "audit",
                inspect_runtime=False,
            )
        semantic_digest.assert_called_once_with(contrib.AUTOMATION_ROOT)
        self.assertTrue(cache.exists())
        self.assertEqual(
            contrib.remove_cleanup_plan(self.repo, plan, plan.digest),
            3,
        )

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


CASE_MANIFEST = """schema = 2
slug = "{slug}"
kind = "production"
title = "Case {slug}"
dependencies = {dependencies}

[tests]
list = ["full"]

[evidence]
required_gates = []
"""
STACK_MANIFEST = """schema = 2
slug = "develop"
description = "test stack"

[tests]
list = ["full"]
"""
LINES = "".join(f"line {number}\n" for number in range(1, 21))


class CaseCommitTest(unittest.TestCase):
    """Every case is exactly one Fork-Case commit on develop."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        command("git", "init", "-q", "-b", "develop", str(self.repo))
        command("git", "config", "user.name", "Case Test", cwd=self.repo)
        command("git", "config", "user.email", "case@example.invalid", cwd=self.repo)
        self.write({".gitignore": "/.artifacts/\n", "xpra/a.py": LINES, "xpra/b.py": "b\n"})
        self.base = self.commit("upstream base")
        command("git", "update-ref", "refs/heads/master", self.base, cwd=self.repo)
        self.control = self.repo / "fork-maintenance"
        self.write({"fork-maintenance/stacks/develop.toml": STACK_MANIFEST})
        self.commit("control plane")
        for name, value in (
            ("AUTOMATION_ROOT", self.control),
            ("CASES_ROOT", self.control / "cases"),
            ("STACKS_ROOT", self.control / "stacks"),
        ):
            patcher = patch.object(contrib, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("verify_repo", "ci_layout_check"):
            patcher = patch.object(contrib, name)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, files: dict[str, str]) -> None:
        for relative, text in files.items():
            path = self.repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def commit(self, message: str, *, case: str | None = None) -> str:
        command("git", "add", "-A", cwd=self.repo)
        arguments = ["git", "commit", "-q", "-m", message]
        if case:
            arguments += ["--trailer", f"Fork-Case: {case}"]
        command(*arguments, cwd=self.repo)
        return command("git", "rev-parse", "HEAD", cwd=self.repo)

    def add_case(self, slug: str, files: dict[str, str], *, dependencies: str = "[]") -> str:
        self.write({
            f"fork-maintenance/cases/{slug}/case.toml": CASE_MANIFEST.format(slug=slug, dependencies=dependencies),
            f"fork-maintenance/cases/{slug}/README.md": f"# {slug}\n",
        })
        self.commit(f"document {slug}")
        self.write(files)
        return self.commit(f"implement {slug}", case=slug)

    def edit(self, relative: str, old: str, new: str) -> dict[str, str]:
        text = (self.repo / relative).read_text(encoding="utf-8")
        self.assertIn(old, text)
        return {relative: text.replace(old, new, 1)}

    def test_history_is_classified_into_control_and_one_commit_per_case(self) -> None:
        one = self.add_case("one", self.edit("xpra/a.py", "line 5\n", "line five\n"))
        mapping = contrib.develop_map(self.repo)
        self.assertEqual(mapping.base, self.base)
        self.assertEqual([(case.slug, case.commit) for case in mapping.cases], [("one", one)])
        self.assertEqual(mapping.cases[0].paths, ("xpra/a.py",))
        self.assertEqual(len(mapping.control), 2)
        self.assertEqual([row[0] for row in contrib.case_rows(self.repo)], ["one"])

    def test_invalid_commits_are_rejected(self) -> None:
        self.add_case("one", self.edit("xpra/a.py", "line 5\n", "line five\n"))
        for label, files, case, message in (
            ("untrailed product", {"xpra/b.py": "changed\n"}, None, "without a Fork-Case trailer"),
            ("mixed", {"xpra/b.py": "mixed\n", "fork-maintenance/x.txt": "x\n"}, "two", "also changes control paths"),
            ("duplicate", {"xpra/b.py": "again\n"}, "one", "more than one commit"),
        ):
            with self.subTest(label=label):
                head = command("git", "rev-parse", "HEAD", cwd=self.repo)
                self.write(files)
                self.commit(label, case=case)
                with self.assertRaisesRegex(contrib.ContribError, message):
                    contrib.develop_map(self.repo)
                command("git", "reset", "-q", "--hard", head, cwd=self.repo)

    def test_pending_fixups_are_squashed_into_their_case_commit(self) -> None:
        one = self.add_case("one", self.edit("xpra/a.py", "line 5\n", "line five\n"))
        self.add_case("two", {"xpra/b.py": "two\n"})
        self.write(self.edit("xpra/a.py", "line 6\n", "line six\n"))
        command("git", "add", "-A", cwd=self.repo)
        command("git", "commit", "-q", f"--fixup={one}", cwd=self.repo)
        with self.assertRaisesRegex(contrib.ContribError, "run develop-squash"):
            contrib.develop_map(self.repo)
        # uncommitted control work survives the rewrite
        (self.control / "notes.txt").write_text("keep\n", encoding="utf-8")
        contrib.develop_squash(self.repo)
        self.assertEqual((self.control / "notes.txt").read_text(encoding="utf-8"), "keep\n")
        mapping = contrib.develop_map(self.repo)
        self.assertEqual([case.slug for case in mapping.cases], ["one", "two"])
        folded = command("git", "show", mapping.cases[0].commit, "--", "xpra/a.py", cwd=self.repo)
        self.assertIn("+line six", folded)
        self.assertIn("+line five", folded)

    def test_adjacent_cases_are_each_independent_and_removable(self) -> None:
        # two cases change neighbouring lines: each commit's own diff carries the
        # other's context, but the in-memory cherry-pick onto the base does not
        self.add_case("one", self.edit("xpra/a.py", "line 5\n", "line five\n"))
        self.add_case("two", self.edit("xpra/a.py", "line 7\n", "line seven\n"))
        for slug in ("one", "two"):
            with self.subTest(slug=slug):
                report = contrib.case_check(self.repo, slug)
                self.assertTrue(report["applies_on_base"])
                self.assertTrue(report["removable"])
        report = contrib.stack_check(self.repo)
        self.assertEqual(report["series"], ["one", "two"])
        self.assertTrue(report["product_tree_matches_head"])

    def test_a_case_built_on_another_case_is_not_independent(self) -> None:
        self.add_case("one", self.edit("xpra/a.py", "line 5\n", "line five\n"))
        self.add_case("two", self.edit("xpra/a.py", "line five\n", "line FIVE\n"))
        # both sides of the entanglement are caught: one cannot be removed under
        # two, and two cannot be applied without one
        with self.assertRaisesRegex(contrib.ContribError, "cannot be removed"):
            contrib.case_check(self.repo, "one")
        with self.assertRaisesRegex(contrib.ContribError, "does not apply alone"):
            contrib.case_check(self.repo, "two")

    def test_case_drop_removes_only_that_commit(self) -> None:
        self.add_case("one", self.edit("xpra/a.py", "line 5\n", "line five\n"))
        self.add_case("two", {"xpra/b.py": "two\n"})
        contrib.case_drop(self.repo, "one")
        self.assertEqual([case.slug for case in contrib.develop_map(self.repo).cases], ["two"])
        self.assertNotIn("line five", (self.repo / "xpra/a.py").read_text(encoding="utf-8"))
        self.assertEqual((self.repo / "xpra/b.py").read_text(encoding="utf-8"), "two\n")

    def test_case_drop_refuses_a_dependency(self) -> None:
        self.add_case("one", self.edit("xpra/a.py", "line 5\n", "line five\n"))
        self.add_case("two", {"xpra/b.py": "two\n"}, dependencies='["one"]')
        with self.assertRaisesRegex(contrib.ContribError, "is a dependency of"):
            contrib.case_drop(self.repo, "one")
        self.assertEqual(contrib.case_check(self.repo, "one")["dependents"], ["two"])

    def test_develop_check_binds_case_directories_to_commits(self) -> None:
        self.add_case("one", self.edit("xpra/a.py", "line 5\n", "line five\n"))
        result = contrib.develop_check(self.repo)
        self.assertEqual([case["case"] for case in result["cases"]], ["one"])
        self.write({
            "fork-maintenance/cases/upstream-test-quarantine/case.toml": "schema = 2\n",
            "fork-maintenance/cases/upstream-test-quarantine/README.md": "# q\n",
        })
        self.commit("quarantine scaffold")
        contrib.develop_check(self.repo)
        self.write({
            "fork-maintenance/cases/orphan/case.toml": CASE_MANIFEST.format(slug="orphan", dependencies="[]"),
        })
        self.commit("case directory without its commit")
        with self.assertRaisesRegex(contrib.ContribError, "without a Fork-Case commit"):
            contrib.develop_check(self.repo)

    def test_stored_patches_are_refused(self) -> None:
        self.add_case("one", self.edit("xpra/a.py", "line 5\n", "line five\n"))
        self.write({"fork-maintenance/cases/one/fix.patch": "stale\n"})
        self.commit("stale stored patch")
        with self.assertRaisesRegex(contrib.ContribError, "stored case patches"):
            contrib.develop_check(self.repo)

    def test_runs_refuse_uncommitted_product_changes(self) -> None:
        self.add_case("one", self.edit("xpra/a.py", "line 5\n", "line five\n"))
        (self.control / "draft.txt").write_text("control work\n", encoding="utf-8")
        contrib.isolated_start_check(self.repo)
        (self.repo / "xpra/b.py").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(contrib.ContribError, "commit these product changes"):
            contrib.isolated_start_check(self.repo)

    def test_case_new_creates_a_schema_two_directory(self) -> None:
        created = contrib.scaffold_case(self.repo, "brand-new")
        manifest = (created / "case.toml").read_text(encoding="utf-8")
        self.assertIn("schema = 2", manifest)
        self.assertNotIn("patch_sha256", manifest)
        readme = (created / "README.md").read_text(encoding="utf-8")
        self.assertIn("Code: the `Fork-Case: brand-new` commit on `develop`", readme)
        with self.assertRaisesRegex(contrib.ContribError, "already exists"):
            contrib.scaffold_case(self.repo, "brand-new")


class RepositoryStateTest(unittest.TestCase):
    """The tracked control plane of this repository follows the commit model."""

    def test_no_case_stores_a_patch_and_manifests_use_schema_two(self) -> None:
        self.assertEqual(sorted(contrib.CASES_ROOT.glob("*/fix.patch")), [])
        for manifest in sorted(contrib.CASES_ROOT.glob("*/case.toml")):
            with self.subTest(case=manifest.parent.name):
                data = contrib.read_toml(manifest)
                self.assertEqual(data["schema"], 2)
                self.assertFalse({"patch_sha256", "paths", "commit_subject", "draft"} & set(data))
        stack = contrib.read_toml(contrib.STACKS_ROOT / "develop.toml")
        self.assertEqual(stack["schema"], 2)
        self.assertNotIn("series", stack)

    def test_quarantine_scaffold_is_permanent(self) -> None:
        directory = contrib.CASES_ROOT / contrib.TEST_QUARANTINE_SLUG
        self.assertTrue((directory / "case.toml").is_file(), "never delete the quarantine scaffold")
        self.assertTrue((directory / "README.md").read_text(encoding="utf-8").strip())
        data = contrib.read_toml(directory / "case.toml")
        self.assertEqual(data["kind"], "test-quarantine")


if __name__ == "__main__":
    unittest.main(verbosity=2)
