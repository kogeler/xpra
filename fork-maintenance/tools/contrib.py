#!/usr/bin/env python3
"""Maintain the downstream Xpra patch queue without publishing Git state."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import uuid
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

import background_job
import container_payload
import podman_policy
import tomllib

AUTOMATION_ROOT = Path(__file__).resolve().parent.parent
REPOSITORY_ROOT = AUTOMATION_ROOT.parent
CASES_ROOT = AUTOMATION_ROOT / "cases"
STACKS_ROOT = AUTOMATION_ROOT / "stacks"
SELECTION_TOOL = AUTOMATION_ROOT / "infra" / "upstream-tests" / "selection.py"
PRIVATE_STATE_TOOL = AUTOMATION_ROOT / "infra" / "upstream-tests" / "private_state.py"
DEFAULT_REPO = REPOSITORY_ROOT

FORK_URL = "https://github.com/kogeler/xpra.git"
UPSTREAM_URL = "https://github.com/Xpra-org/xpra.git"
FORK_REPOSITORY = "kogeler/xpra"
UPSTREAM_REPOSITORY = "Xpra-org/xpra"
REMOTE_URLS = {
    "origin": FORK_URL,
    "upstream": UPSTREAM_URL,
}
FORK_OWNER = "kogeler"
BASE_BRANCH = "master"
INTEGRATION_BRANCH = "develop"
ACTIVE_STACK = "develop"
UPSTREAM_TEST_OWNER = "xpra-fork-maintenance-upstream-tests"
LIVE_JOB_OWNER = "xpra-fork-maintenance-live-job"
DEB_PACKAGE_OWNER = "xpra-deb-packages"
DEB_SELECTION_OWNER = "xpra-deb-selection-cache"
CYCLE_CLEAN_OWNER = "xpra-fork-cycle-cleanup"

SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
GIT_SHA_RE = re.compile(r"[0-9a-f]{40}")
UUID4_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
TEST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*")
SELECTION_RE = re.compile(r"(?:cases|stacks)/[a-z0-9]+(?:-[a-z0-9]+)*")
WORKSPACE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
CYCLE_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
RUNNER_PATCH_MODES = frozenset({"clean", "tests-only", "patched"})
TEST_QUARANTINE_SLUG = "upstream-test-quarantine"
ACTIVE_FORK_WORKFLOW = ".github/workflows/develop.yml"
MASTER_SYNC_WORKFLOW = ".github/workflows/master-sync.yml"
DEB_RELEASE_WORKFLOW = ".github/workflows/deb-packages.yml"
ACTIVE_FORK_WORKFLOWS = tuple(
    sorted((ACTIVE_FORK_WORKFLOW, DEB_RELEASE_WORKFLOW, MASTER_SYNC_WORKFLOW))
)
UPSTREAM_WORKFLOW_DIRECTORY = ".github/workflows"
DISABLED_UPSTREAM_WORKFLOW_DIRECTORY = ".github/upstream-workflows"
CHECKOUT_ACTION_VERSION = "v7.0.1"
CHECKOUT_ACTION_SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"
ALLOWED_DEVELOP_PATHS = (
    "AGENTS.md",
    ".gitignore",
    ".github/workflows/",
    ".github/upstream-workflows/",
    "fork-maintenance/",
)
LOCAL_ONLY_ROOTS = (
    ".artifacts",
    "fork-maintenance/communications",
    "fork-maintenance/evidence",
    "fork-maintenance/results",
    "fork-maintenance/runs",
)


class ContribError(RuntimeError):
    """A fail-closed patch-queue precondition was not met."""


@dataclass(frozen=True)
class IsolatedState:
    branch: str
    head: str
    source_commit: str
    fork_base: str
    source_in_head: bool
    worktree_status: str


@dataclass(frozen=True)
class MasterSyncState:
    fork_before: str
    upstream_before: str
    fork_after: str
    upstream_after: str
    updated: bool


@dataclass(frozen=True)
class CheckoutSourceState:
    head: str
    source_commit: str
    master_ref: str
    master_commit: str
    worktree_status: str


@dataclass(frozen=True)
class CleanupTarget:
    kind: str
    path: Path
    fingerprint: str


@dataclass(frozen=True)
class CleanupPlan:
    cycle: str
    targets: tuple[CleanupTarget, ...]
    digest: str


@dataclass(frozen=True)
class CleanupDirectoryState:
    index: int
    device: int
    inode: int
    fingerprint: str


@dataclass(frozen=True)
class CleanupTransaction:
    plan: CleanupPlan
    marker: Path
    directories: tuple[CleanupDirectoryState, ...]


def fail(message: str) -> NoReturn:
    raise ContribError(message)


def run(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    text: bool = True,
    env: dict[str, str] | None = None,
    input: str | None = None,
) -> subprocess.CompletedProcess[Any]:
    try:
        podman_policy.validate_podman_argv(command)
    except podman_policy.PodmanPolicyError as error:
        fail(str(error))
    result = subprocess.run(
        list(command),
        cwd=cwd,
        check=False,
        capture_output=True,
        text=text,
        env=None if env is None else {**os.environ, **env},
        input=input,
    )
    if check and result.returncode:
        stdout = result.stdout if text else result.stdout.decode(errors="replace")
        stderr = result.stderr if text else result.stderr.decode(errors="replace")
        detail = "\n".join(part for part in (stdout.strip(), stderr.strip()) if part)
        suffix = f"\n{detail}" if detail else ""
        fail(f"command exited with status {result.returncode}: {shlex.join(command)}{suffix}")
    return result


def git(
    repo: Path,
    *arguments: str,
    check: bool = True,
    text: bool = True,
) -> subprocess.CompletedProcess[Any]:
    return run(("git", "-C", str(repo), *arguments), check=check, text=text)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def read_toml(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        fail(f"manifest is missing or unsafe: {path}")
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        fail(f"cannot read {path}: {error}")


def verify_repo(
    repo: Path,
    remotes: Sequence[str] = ("origin", "upstream"),
) -> None:
    if repo.is_symlink() or not repo.is_dir():
        fail(f"Xpra repository is missing or unsafe: {repo}")
    top = Path(git(repo, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if top != repo.resolve():
        fail(f"repository path is not its top level: {repo}")
    available = set(git(repo, "remote").stdout.splitlines()) if remotes else set()
    for remote in remotes:
        if remote not in REMOTE_URLS:
            fail(f"unsupported repository remote: {remote}")
        if remote not in available:
            fail(f"repository has no {remote!r} remote")


def rev_parse(repo: Path, revision: str) -> str:
    value = git(repo, "rev-parse", revision).stdout.strip()
    if not GIT_SHA_RE.fullmatch(value):
        fail(f"cannot resolve a commit for {revision}: {value!r}")
    return value


def current_branch(repo: Path) -> str:
    branch = git(repo, "branch", "--show-current").stdout.strip()
    if not branch:
        fail("repository is in detached HEAD state")
    return branch


def porcelain(repo: Path) -> str:
    return git(repo, "status", "--porcelain=v1", "--untracked-files=all").stdout


def require_clean(repo: Path) -> None:
    status = porcelain(repo)
    if status:
        fail(f"repository has local changes:\n{status.rstrip()}")


def live_remote_ref(repo: Path, remote: str, branch: str) -> str:
    output = git(repo, "ls-remote", "--heads", remote, f"refs/heads/{branch}").stdout.strip()
    if not output:
        return ""
    rows = output.splitlines()
    if len(rows) != 1:
        fail(f"remote {remote} returned multiple refs for {branch}")
    commit = rows[0].split("\t", 1)[0]
    if not GIT_SHA_RE.fullmatch(commit):
        fail(f"remote {remote} returned an invalid commit for {branch}")
    return commit


def fetch_master(repo: Path, remote: str) -> None:
    git(
        repo,
        "fetch",
        remote,
        f"refs/heads/{BASE_BRANCH}:refs/remotes/{remote}/{BASE_BRANCH}",
    )


def cached_master(repo: Path, remote: str) -> str:
    return rev_parse(repo, f"refs/remotes/{remote}/{BASE_BRANCH}")


def verify_live_fork_master(repo: Path) -> str:
    observed: dict[str, str] = {}
    for remote, description in (
        ("origin", "fork"),
        ("upstream", "canonical upstream"),
    ):
        cached = cached_master(repo, remote)
        live = live_remote_ref(repo, remote, BASE_BRANCH)
        if not live or cached != live:
            fail(
                f"cached {remote}/{BASE_BRANCH} {cached} does not match live "
                f"{description} {live or '<missing>'}; run repo-sync"
            )
        observed[remote] = live
    if observed["origin"] != observed["upstream"]:
        fail(
            f"live fork {BASE_BRANCH} {observed['origin']} does not match live "
            f"canonical {BASE_BRANCH} {observed['upstream']}; the operator must run "
            f"gh repo sync kogeler/xpra --source Xpra-org/xpra --branch {BASE_BRANCH} "
            "without --force, then repeat repo-sync"
        )
    return observed["origin"]


def sync_repo(repo: Path) -> str:
    verify_repo(repo, ("origin", "upstream"))
    for remote in ("origin", "upstream"):
        fetch_master(repo, remote)
    return verify_live_fork_master(repo)


def repo_sync(repo: Path) -> str:
    """Enter the public refresh fetch boundary only from clean develop."""
    if current_branch(repo) != INTEGRATION_BRANCH:
        fail(f"repo-sync requires the {INTEGRATION_BRANCH} branch")
    require_clean(repo)
    return sync_repo(repo)


def is_ancestor(repo: Path, older: str, newer: str) -> bool:
    result = git(repo, "merge-base", "--is-ancestor", older, newer, check=False)
    if result.returncode not in (0, 1):
        fail(f"cannot compare commits {older} and {newer}")
    return result.returncode == 0


def require_non_master(repo: Path) -> str:
    branch = current_branch(repo)
    if branch == BASE_BRANCH:
        fail("refusing to apply or develop patches on master")
    return branch


def require_local_master(repo: Path, base: str = "") -> str:
    result = git(
        repo,
        "show-ref",
        "--verify",
        "--quiet",
        f"refs/heads/{BASE_BRANCH}",
        check=False,
    )
    if result.returncode not in (0, 1):
        fail("cannot inspect local master")
    if result.returncode == 1:
        fail("local master is missing; the operator must prepare it before upstream refresh")
    local = rev_parse(repo, f"refs/heads/{BASE_BRANCH}")
    if base and local != base:
        fail(f"local master changed from {base} to {local}")
    return local


def require_rebased_develop(repo: Path, base: str) -> str:
    require_local_master(repo, base)
    result = git(
        repo,
        "show-ref",
        "--verify",
        "--quiet",
        f"refs/heads/{INTEGRATION_BRANCH}",
        check=False,
    )
    if result.returncode not in (0, 1):
        fail("cannot inspect local develop")
    if result.returncode == 1:
        fail("local develop is missing")
    develop = rev_parse(repo, f"refs/heads/{INTEGRATION_BRANCH}")
    if not is_ancestor(repo, base, develop):
        fail(
            f"develop is not rebased onto local {BASE_BRANCH} {base}; "
            "run develop-rebase before patch work"
        )
    merges = tuple(
        git(
            repo,
            "rev-list",
            "--merges",
            f"{base}..refs/heads/{INTEGRATION_BRANCH}",
        ).stdout.splitlines()
    )
    if merges:
        fail(
            "develop contains merge commits above current master; upstream history "
            f"must be transferred only by rebase: {merges}"
        )
    return develop


def patch_start_check(repo: Path) -> str:
    """Verify that develop is completely rebased onto local master."""
    verify_repo(repo, ())
    require_clean(repo)
    if require_non_master(repo) != INTEGRATION_BRANCH:
        fail(f"case work stays on {INTEGRATION_BRANCH}; no other branch is supported")
    base = require_local_master(repo)
    require_rebased_develop(repo, base)
    develop_map(repo, base)
    return base


def archive_tree(repo: Path, revision: str, destination: Path) -> None:
    archive = git(repo, "archive", "--format=tar", revision, text=False).stdout
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as stream:
        stream.extractall(destination, filter="data")


def selection_resolution(
    repo: Path,
    revision: str,
    selection: str,
    *,
    lab_root: Path | None = None,
    scratch_directory: Path | None = None,
) -> dict[str, Any]:
    if not SELECTION_RE.fullmatch(selection):
        fail(f"invalid selection: {selection!r}")
    selected_lab_root = AUTOMATION_ROOT if lab_root is None else lab_root

    def resolve(source: Path) -> subprocess.CompletedProcess[str]:
        archive_tree(repo, revision, source)
        return run(
            (
                sys.executable,
                str(SELECTION_TOOL),
                "--lab-root",
                str(selected_lab_root),
                "--selection",
                selection,
                "resolve",
                "--source-tree",
                str(source),
                "--source-commit",
                revision,
            )
        )
    if scratch_directory is None:
        with tempfile.TemporaryDirectory(prefix="xpra-fork-selection-") as raw:
            result = resolve(Path(raw))
    else:
        if scratch_directory.exists() or scratch_directory.is_symlink():
            fail(f"selection-resolution scratch already exists: {scratch_directory}")
        scratch_directory.mkdir(mode=0o700)
        try:
            result = resolve(scratch_directory)
        finally:
            if scratch_directory.exists() and not scratch_directory.is_symlink():
                shutil.rmtree(scratch_directory)
                fsync_directory(scratch_directory.parent)
    data = json.loads(result.stdout)
    if not isinstance(data, dict):
        fail("selection resolver returned an invalid document")
    return data


def staged_names(repo: Path) -> tuple[str, ...]:
    return tuple(
        line
        for line in git(repo, "diff", "--cached", "--name-only").stdout.splitlines()
        if line
    )


def unstaged_names(repo: Path) -> tuple[str, ...]:
    return tuple(
        line for line in git(repo, "diff", "--name-only").stdout.splitlines() if line
    )


def untracked_names(repo: Path) -> tuple[str, ...]:
    return tuple(
        line
        for line in git(repo, "ls-files", "--others", "--exclude-standard").stdout.splitlines()
        if line
    )


def fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def canonical_json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def artifact_boundary_check(repo: Path) -> None:
    probe = ".artifacts/fork-maintenance/.ignore-probe"
    ignored = git(repo, "check-ignore", "--no-index", "--quiet", probe, check=False)
    if ignored.returncode != 0:
        fail("root .gitignore does not protect .artifacts/fork-maintenance")
    tracked = git(repo, "ls-files", "--", *LOCAL_ONLY_ROOTS).stdout.splitlines()
    if tracked:
        fail(f"runtime or result paths are tracked: {tracked}")


def repository_files(root: Path, description: str) -> tuple[str, ...]:
    if root.is_symlink() or not root.is_dir():
        fail(f"{description} is missing or unsafe: {root}")
    files: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            fail(f"{description} contains a symlink: {path}")
        if path.is_file():
            files.append(path.relative_to(root).as_posix())
        elif not path.is_dir():
            fail(f"{description} contains an unsupported object: {path}")
    return tuple(files)


def fork_workflow_semantics() -> tuple[str, ...]:
    return (
        "name: CI",
        "on:",
        "  push:",
        "    branches:",
        "      - develop",
        "permissions:",
        "  contents: read",
        "jobs:",
        "  upstream-tests:",
        "    name: Upstream tests (${{ matrix.target }})",
        "    runs-on: ubuntu-26.04",
        "    timeout-minutes: 360",
        "    strategy:",
        "      fail-fast: false",
        "      max-parallel: 3",
        "      matrix:",
        "        target:",
        "          - full",
        "          - full-cython",
        "          - full-no-compat",
        "    steps:",
        "      - name: Check out develop",
        f"        uses: actions/checkout@{CHECKOUT_ACTION_SHA}",
        "        with:",
        "          fetch-depth: 0",
        "          persist-credentials: false",
        "      - name: Run patched upstream test leg",
        "        env:",
        "          XPRA_CI_TARGET: ${{ matrix.target }}",
        "        run: make -C fork-maintenance ci-upstream-tests",
    )


def master_sync_workflow_semantics() -> tuple[str, ...]:
    return (
        "name: Sync master",
        "on:",
        "  schedule:",
        '    - cron: "37 */12 * * *"',
        "  workflow_dispatch:",
        "permissions: {}",
        "jobs:",
        "  sync-master:",
        "    runs-on: ubuntu-26.04",
        "    timeout-minutes: 10",
        "    permissions:",
        "      contents: write",
        "    steps:",
        "      - name: Check out develop automation",
        f"        uses: actions/checkout@{CHECKOUT_ACTION_SHA}",
        "        with:",
        "          ref: develop",
        "          fetch-depth: 1",
        "          persist-credentials: false",
        "      - name: Fast-forward fork master",
        "        env:",
        "          GH_TOKEN: ${{ github.token }}",
        "        run: make -C fork-maintenance ci-master-sync",
    )


def deb_release_workflow_semantics() -> tuple[str, ...]:
    return (
        "name: DEB packages",
        "on:",
        "  workflow_dispatch:",
        "permissions: {}",
        "jobs:",
        "  release:",
        "    runs-on: ubuntu-26.04",
        "    timeout-minutes: 360",
        "    permissions:",
        "      contents: write",
        "    steps:",
        "      - name: Check out dispatched revision",
        f"        uses: actions/checkout@{CHECKOUT_ACTION_SHA}",
        "        with:",
        "          fetch-depth: 0",
        "          persist-credentials: false",
        "      - name: Build patched packages and publish release",
        "        env:",
        "          GH_TOKEN: ${{ github.token }}",
        "        run: make -C fork-maintenance ci-deb-release",
    )


def validate_fork_workflow(
    path: Path,
    expected_semantics: tuple[str, ...],
    description: str,
) -> None:
    if path.is_symlink() or not path.is_file():
        fail(f"active fork workflow is missing or unsafe: {path}")
    text = path.read_text(encoding="utf-8")
    action_lines = re.findall(
        r"(?m)^\s*uses: actions/checkout@([0-9a-f]{40})\s+#\s+(v[0-9]+\.[0-9]+\.[0-9]+)\s*$",
        text,
    )
    if action_lines != [(CHECKOUT_ACTION_SHA, CHECKOUT_ACTION_VERSION)]:
        fail(
            "fork workflow must pin actions/checkout to the reviewed full SHA "
            f"for {CHECKOUT_ACTION_VERSION}"
        )
    semantic_lines = tuple(
        line.split("  #", 1)[0].rstrip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    if semantic_lines != expected_semantics:
        fail(
            f"{description} workflow is not its approved thin Make interface"
        )


def ci_layout_check(repo: Path, base: str) -> dict[str, Any]:
    """Require all canonical workflows to be byte-identical disabled renames."""
    if not GIT_SHA_RE.fullmatch(base):
        fail(f"invalid canonical workflow base: {base!r}")
    upstream_paths = tuple(
        path
        for path in git(
            repo,
            "ls-tree",
            "-r",
            "--name-only",
            base,
            "--",
            UPSTREAM_WORKFLOW_DIRECTORY,
        ).stdout.splitlines()
        if Path(path).suffix.lower() in {".yml", ".yaml"}
    )
    if not upstream_paths:
        fail(f"fork master has no workflows below {UPSTREAM_WORKFLOW_DIRECTORY}")

    active_root = repo / UPSTREAM_WORKFLOW_DIRECTORY
    active_files = repository_files(active_root, "active workflow directory")
    expected_active = tuple(Path(path).name for path in ACTIVE_FORK_WORKFLOWS)
    if active_files != expected_active:
        fail(
            "active workflow directory must contain only the approved fork workflows: "
            f"{active_files} != {expected_active}"
        )
    validate_fork_workflow(
        repo / ACTIVE_FORK_WORKFLOW,
        fork_workflow_semantics(),
        "develop CI",
    )
    validate_fork_workflow(
        repo / MASTER_SYNC_WORKFLOW,
        master_sync_workflow_semantics(),
        "master sync",
    )
    validate_fork_workflow(
        repo / DEB_RELEASE_WORKFLOW,
        deb_release_workflow_semantics(),
        "DEB release",
    )

    disabled_root = repo / DISABLED_UPSTREAM_WORKFLOW_DIRECTORY
    disabled_files = repository_files(disabled_root, "disabled upstream workflow directory")
    expected_disabled: list[str] = []
    prefix = f"{UPSTREAM_WORKFLOW_DIRECTORY}/"
    for upstream_path in upstream_paths:
        if not upstream_path.startswith(prefix):
            fail(f"canonical workflow escaped its directory: {upstream_path}")
        relative = upstream_path.removeprefix(prefix)
        expected_disabled.append(relative)
        original = repo / upstream_path
        if original.exists() or original.is_symlink():
            fail(f"canonical upstream workflow is still active: {upstream_path}")
        disabled = disabled_root / relative
        if disabled.is_symlink() or not disabled.is_file():
            fail(f"disabled upstream workflow is missing or unsafe: {disabled}")
        canonical = git(repo, "show", f"{base}:{upstream_path}", text=False).stdout
        if disabled.read_bytes() != canonical:
            fail(f"disabled workflow differs from fork master: {disabled}")
    if disabled_files != tuple(sorted(expected_disabled)):
        fail(
            "disabled workflow set does not exactly mirror fork master: "
            f"{disabled_files} != {tuple(sorted(expected_disabled))}"
        )
    return {
        "base": base,
        "active_workflows": ACTIVE_FORK_WORKFLOWS,
        "checkout_action_sha": CHECKOUT_ACTION_SHA,
        "checkout_action_version": CHECKOUT_ACTION_VERSION,
        "disabled_upstream_workflows": tuple(sorted(expected_disabled)),
    }


def validate_ci_checkout(repo: Path) -> None:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        verify_repo(repo, ("origin",))
        return
    expected_environment = {
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REF": f"refs/heads/{INTEGRATION_BRANCH}",
        "GITHUB_REPOSITORY": f"{FORK_OWNER}/xpra",
        "GITHUB_WORKFLOW_REF": (
            f"{FORK_REPOSITORY}/{ACTIVE_FORK_WORKFLOW}@"
            f"refs/heads/{INTEGRATION_BRANCH}"
        ),
    }
    for name, expected in expected_environment.items():
        actual = os.environ.get(name, "")
        if actual != expected:
            fail(f"CI environment has unexpected {name}: {actual!r}")
    expected_sha = os.environ.get("GITHUB_SHA", "")
    if not GIT_SHA_RE.fullmatch(expected_sha) or rev_parse(repo, "HEAD") != expected_sha:
        fail("CI checkout does not match GITHUB_SHA")
    if current_branch(repo) != INTEGRATION_BRANCH:
        fail(f"CI checkout must be on {INTEGRATION_BRANCH}")

    verify_repo(repo, ("origin",))
    require_clean(repo)


def ci_prepare(repo: Path) -> IsolatedState:
    validate_ci_checkout(repo)
    return ci_start_check(repo)


def validate_deb_release_checkout(repo: Path) -> None:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        fail("ci-deb-release may run only in GitHub Actions")
    expected_environment = {
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REPOSITORY": FORK_REPOSITORY,
    }
    for name, expected in expected_environment.items():
        actual = os.environ.get(name, "")
        if actual != expected:
            fail(f"DEB release has unexpected {name}: {actual!r}")
    github_ref = os.environ.get("GITHUB_REF", "")
    checked_ref = git(repo, "check-ref-format", github_ref, check=False)
    if (
        not github_ref.startswith(("refs/heads/", "refs/tags/"))
        or checked_ref.returncode
    ):
        fail(f"DEB release requires a valid branch or tag ref: {github_ref!r}")
    workflow_ref = os.environ.get("GITHUB_WORKFLOW_REF", "")
    expected_workflow_ref = f"{FORK_REPOSITORY}/{DEB_RELEASE_WORKFLOW}@{github_ref}"
    if workflow_ref != expected_workflow_ref:
        fail(f"DEB release has unexpected GITHUB_WORKFLOW_REF: {workflow_ref!r}")
    expected_sha = os.environ.get("GITHUB_SHA", "")
    if not GIT_SHA_RE.fullmatch(expected_sha) or rev_parse(repo, "HEAD") != expected_sha:
        fail("DEB release checkout does not match GITHUB_SHA")
    verify_repo(repo, ())
    require_clean(repo)
    if not os.environ.get("GH_TOKEN", "").strip():
        fail("DEB release requires the job-scoped GH_TOKEN")
    if shutil.which("gh") is None:
        fail("DEB release requires GitHub CLI")
    if shutil.which("podman") is None:
        fail("DEB release requires Podman")


def ci_deb_prepare(repo: Path) -> CheckoutSourceState:
    validate_deb_release_checkout(repo)
    return checkout_source_check(repo)


def validate_master_sync_checkout(repo: Path) -> None:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        fail("ci-master-sync may run only in GitHub Actions")
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    if event not in {"schedule", "workflow_dispatch"}:
        fail(f"master sync has unexpected GITHUB_EVENT_NAME: {event!r}")
    expected_environment = {
        "GITHUB_REF": f"refs/heads/{INTEGRATION_BRANCH}",
        "GITHUB_REPOSITORY": FORK_REPOSITORY,
        "GITHUB_WORKFLOW_REF": (
            f"{FORK_REPOSITORY}/{MASTER_SYNC_WORKFLOW}@"
            f"refs/heads/{INTEGRATION_BRANCH}"
        ),
    }
    for name, expected in expected_environment.items():
        actual = os.environ.get(name, "")
        if actual != expected:
            fail(f"master sync has unexpected {name}: {actual!r}")
    expected_sha = os.environ.get("GITHUB_SHA", "")
    if not GIT_SHA_RE.fullmatch(expected_sha) or rev_parse(repo, "HEAD") != expected_sha:
        fail("master sync checkout does not match GITHUB_SHA")
    if current_branch(repo) != INTEGRATION_BRANCH:
        fail(f"master sync checkout must be on {INTEGRATION_BRANCH}")
    verify_repo(repo, ("origin",))
    require_clean(repo)
    if not os.environ.get("GH_TOKEN", "").strip():
        fail("master sync requires the job-scoped GH_TOKEN")
    if shutil.which("gh") is None:
        fail("master sync requires GitHub CLI")


def fast_forward_fork_master(repo: Path) -> None:
    run(
        (
            "gh",
            "repo",
            "sync",
            FORK_REPOSITORY,
            "--source",
            UPSTREAM_REPOSITORY,
            "--branch",
            BASE_BRANCH,
        ),
        cwd=repo,
    )


def require_fork_master_fast_forward(
    repo: Path,
    fork_commit: str,
    upstream_commit: str,
) -> None:
    """Prove that a mismatched fork master can advance without rewriting it."""
    if not GIT_SHA_RE.fullmatch(fork_commit) or not GIT_SHA_RE.fullmatch(upstream_commit):
        fail("master sync received an invalid live commit identity")
    result = run(
        (
            "gh",
            "api",
            "--method",
            "GET",
            f"repos/{FORK_REPOSITORY}/compare/{fork_commit}...{upstream_commit}",
        ),
        cwd=repo,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        fail(f"master sync compare returned invalid JSON: {error}")
    if not isinstance(payload, dict):
        fail("master sync compare returned a non-object")
    base_commit = payload.get("base_commit")
    merge_base = payload.get("merge_base_commit")
    fast_forward = (
        payload.get("status") == "ahead"
        and isinstance(payload.get("ahead_by"), int)
        and not isinstance(payload.get("ahead_by"), bool)
        and payload["ahead_by"] > 0
        and payload.get("behind_by") == 0
        and isinstance(base_commit, dict)
        and base_commit.get("sha") == fork_commit
        and isinstance(merge_base, dict)
        and merge_base.get("sha") == fork_commit
    )
    if not fast_forward:
        fail(
            "fork master is ahead of or diverged from upstream master; "
            "owner review is required"
        )


def master_sync_local_state(repo: Path) -> tuple[str, str, str, str]:
    return (
        current_branch(repo),
        rev_parse(repo, "HEAD"),
        porcelain(repo),
        git(repo, "for-each-ref", "--format=%(refname) %(objectname)").stdout,
    )


def ci_master_sync(repo: Path) -> MasterSyncState:
    validate_master_sync_checkout(repo)
    local_before = master_sync_local_state(repo)

    fork_before = live_remote_ref(repo, FORK_URL, BASE_BRANCH)
    upstream_before = live_remote_ref(repo, UPSTREAM_URL, BASE_BRANCH)
    if not fork_before or not upstream_before:
        fail("master sync could not resolve both live master refs")

    if fork_before != upstream_before:
        require_fork_master_fast_forward(repo, fork_before, upstream_before)
        try:
            fast_forward_fork_master(repo)
        finally:
            if master_sync_local_state(repo) != local_before:
                fail("master sync changed the develop checkout")

    fork_after = live_remote_ref(repo, FORK_URL, BASE_BRANCH)
    upstream_after = live_remote_ref(repo, UPSTREAM_URL, BASE_BRANCH)
    if not fork_after or not upstream_after or fork_after != upstream_after:
        fail(
            f"fork master {fork_after or '<missing>'} does not match upstream master "
            f"{upstream_after or '<missing>'} after sync"
        )
    if master_sync_local_state(repo) != local_before:
        fail("master sync changed the develop checkout")
    return MasterSyncState(
        fork_before=fork_before,
        upstream_before=upstream_before,
        fork_after=fork_after,
        upstream_after=upstream_after,
        updated=fork_before != fork_after,
    )


def embedded_develop_state(
    repo: Path, purpose: str, *, source_ref: str = "", allow_pending_fixups: bool = False
) -> IsolatedState:
    """Locate the immutable source boundary already embedded in ``develop``."""
    verify_repo(repo, ())
    artifact_boundary_check(repo)
    branch = current_branch(repo)
    if branch != INTEGRATION_BRANCH:
        fail(f"{purpose} must stay on {INTEGRATION_BRANCH}")
    head = rev_parse(repo, "HEAD")
    status = porcelain(repo)
    unexpected_dirty = [
        path for path in isolated_dirty_names(repo) if not allowed_develop_path(path)
    ]
    if unexpected_dirty:
        fail(
            f"{purpose} uses committed HEAD; commit these product changes to their case "
            f"(git commit --fixup) first: {unexpected_dirty}"
        )

    if not source_ref:
        local_refs = git(repo, "for-each-ref", "--format=%(refname)", "refs/heads/master").stdout.splitlines()
        source_ref = "refs/heads/master" if "refs/heads/master" in local_refs else "refs/remotes/origin/master"
    source_tip = rev_parse(repo, source_ref)
    merge_base = git(repo, "merge-base", "--all", source_tip, head, check=False)
    source_commits = tuple(merge_base.stdout.splitlines())
    if (
        merge_base.returncode
        or len(source_commits) != 1
        or not GIT_SHA_RE.fullmatch(source_commits[0])
    ):
        fail(
            f"{INTEGRATION_BRANCH} and {source_ref} "
            "have no single usable history boundary"
        )
    source_commit = source_commits[0]
    # every product change must be a valid single Fork-Case commit:
    if not allow_pending_fixups:
        develop_map(repo, source_commit)
    merges = git(repo, "rev-list", "--merges", f"{source_commit}..{head}").stdout.splitlines()
    if merges:
        fail(f"develop contains fork-side merge commits: {merges}")
    if (
        current_branch(repo) != branch
        or rev_parse(repo, "HEAD") != head
        or rev_parse(repo, source_ref) != source_tip
        or porcelain(repo) != status
    ):
        fail(
            "repository branch, HEAD, source ref, or worktree changed "
            "while locating the embedded source"
        )
    return IsolatedState(
        branch=branch,
        head=head,
        source_commit=source_commit,
        fork_base=source_commit,
        source_in_head=True,
        worktree_status=status,
    )


def ci_start_check(repo: Path) -> IsolatedState:
    """Locate the source boundary already embedded in pushed ``develop``."""
    return embedded_develop_state(repo, "CI checkout", source_ref="refs/remotes/origin/master")


def checkout_source_check(repo: Path) -> CheckoutSourceState:
    """Locate the clean source boundary from HEAD and refs named ``master``."""
    verify_repo(repo, ())
    artifact_boundary_check(repo)
    head = rev_parse(repo, "HEAD")
    status = porcelain(repo)
    unexpected_dirty = [
        path for path in isolated_dirty_names(repo) if not allowed_develop_path(path)
    ]
    if unexpected_dirty:
        fail(
            "DEB source discovery uses committed HEAD; commit or discard these product "
            f"changes first: {unexpected_dirty}"
        )

    rows = git(
        repo,
        "for-each-ref",
        "--format=%(refname) %(objectname)",
        "refs/heads",
        "refs/remotes",
    ).stdout.splitlines()
    master_refs: list[tuple[str, str, tuple[str, ...]]] = []
    for row in rows:
        ref, separator, commit = row.partition(" ")
        if not separator or ref.rsplit("/", 1)[-1] != BASE_BRANCH:
            continue
        if not GIT_SHA_RE.fullmatch(commit):
            fail(f"invalid commit for master ref {ref}: {commit!r}")
        bases = tuple(git(repo, "merge-base", "--all", commit, head).stdout.splitlines())
        if not bases or any(not GIT_SHA_RE.fullmatch(base) for base in bases):
            fail(f"HEAD and {ref} have no trustworthy history boundary")
        master_refs.append((ref, commit, bases))
    if not master_refs:
        fail("repository has no local or remote-tracking ref named master")
    candidates = tuple(sorted({base for _ref, _commit, bases in master_refs for base in bases}))
    latest = tuple(
        candidate
        for candidate in candidates
        if all(is_ancestor(repo, other, candidate) for other in candidates)
    )
    if len(latest) != 1:
        fail(f"master refs do not identify one latest clean boundary: {candidates}")
    source_commit = latest[0]
    matching_refs = sorted(
        (ref, commit)
        for ref, commit, bases in master_refs
        if source_commit in bases
    )
    if not matching_refs:
        fail("no master ref owns the selected clean source boundary")
    master_ref, master_commit = matching_refs[0]
    sentinel_at_source = git(
        repo,
        "ls-tree",
        "--name-only",
        source_commit,
        "--",
        "fork-maintenance/CONTRACT.md",
    ).stdout.splitlines()
    if sentinel_at_source:
        fail("the clean source boundary already contains downstream maintenance files")
    sentinel_at_master = git(
        repo,
        "ls-tree",
        "--name-only",
        master_commit,
        "--",
        "fork-maintenance/CONTRACT.md",
    ).stdout.splitlines()
    if sentinel_at_master:
        fail(f"selected master ref contains downstream maintenance files: {master_ref}")

    merges = git(repo, "rev-list", "--merges", f"{source_commit}..{head}").stdout.splitlines()
    if merges:
        fail(f"checkout contains downstream merge commits: {merges}")
    # product changes above the boundary must be valid single Fork-Case commits:
    develop_map(repo, source_commit)
    if (
        rev_parse(repo, "HEAD") != head
        or rev_parse(repo, master_ref) != master_commit
        or porcelain(repo) != status
    ):
        fail("HEAD, selected master ref, or worktree changed while locating DEB source")
    return CheckoutSourceState(
        head=head,
        source_commit=source_commit,
        master_ref=master_ref,
        master_commit=master_commit,
        worktree_status=status,
    )


def allowed_develop_path(path: str) -> bool:
    return any(
        path.startswith(allowed) if allowed.endswith("/") else path == allowed
        for allowed in ALLOWED_DEVELOP_PATHS
    )


def isolated_dirty_names(repo: Path) -> tuple[str, ...]:
    return tuple(
        sorted(
            set(staged_names(repo))
            | set(unstaged_names(repo))
            | set(untracked_names(repo))
        )
    )


def isolated_start_check(repo: Path) -> IsolatedState:
    """Freeze the source boundary embedded in ``develop`` without any live query."""
    return embedded_develop_state(repo, "case work")


# --- case commits -----------------------------------------------------------
#
# Every case is exactly one commit on develop with a ``Fork-Case: <slug>``
# trailer (see docs/runbooks/case-commits.md). The classification of the fork
# history is shared with the runners through the selection module.

_SELECTION_MODULE: Any = None


def selection_module() -> Any:
    global _SELECTION_MODULE
    if _SELECTION_MODULE is None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("fork_selection", SELECTION_TOOL)
        if spec is None or spec.loader is None:
            fail(f"cannot load the selection module: {SELECTION_TOOL}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _SELECTION_MODULE = module
    return _SELECTION_MODULE


def develop_map(repo: Path, base: str | None = None) -> Any:
    """Classify base..HEAD: control commits and one commit per case."""
    selection = selection_module()
    try:
        return selection.develop_map(repo, base)
    except selection.SelectionError as error:
        fail(str(error))


def case_directories() -> tuple[str, ...]:
    return tuple(
        sorted(
            path.name
            for path in CASES_ROOT.iterdir()
            if path.is_dir() and not path.is_symlink() and (path / "case.toml").is_file()
        )
    )


def case_commit(repo: Path, slug: str) -> Any:
    found = develop_map(repo).commit_of(slug)
    if found is None:
        fail(f"case {slug} has no Fork-Case commit in {INTEGRATION_BRANCH}")
    return found


def require_clean_product(repo: Path, purpose: str) -> None:
    dirty = [path for path in isolated_dirty_names(repo) if not allowed_develop_path(path)]
    if dirty:
        fail(f"{purpose} needs committed product paths; commit or discard: {dirty}")


def manifest_dependencies(slug: str) -> tuple[str, ...]:
    data = read_toml(CASES_ROOT / slug / "case.toml")
    value = data.get("dependencies", [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        fail(f"invalid dependencies of case {slug}")
    return tuple(value)


def dependents(repo: Path, slug: str) -> tuple[str, ...]:
    return tuple(
        case.slug
        for case in develop_map(repo).cases
        if case.slug != slug
        and (CASES_ROOT / case.slug / "case.toml").is_file()
        and slug in manifest_dependencies(case.slug)
    )


def removable_from_head(repo: Path, commit: str) -> bool:
    """Reverting ``commit`` from HEAD merges without a conflict (in memory)."""
    result = git(
        repo, "merge-tree", "--write-tree", f"--merge-base={commit}", "HEAD", f"{commit}^",
        check=False,
    )
    return result.returncode == 0


def case_check(repo: Path, slug: str) -> dict[str, Any]:
    state = embedded_develop_state(repo, "case check")
    found = case_commit(repo, slug)
    resolution = selection_resolution(repo, state.source_commit, f"cases/{slug}")
    statuses = [entry.get("status") for entry in resolution.get("patches", [])]
    if statuses != ["apply"]:
        fail(f"case {slug} does not apply on the upstream base {state.source_commit}: {statuses}")
    if not removable_from_head(repo, found.commit):
        fail(f"case {slug} cannot be removed from {INTEGRATION_BRANCH} without a conflict")
    blocked_by = dependents(repo, slug)
    return {
        "case": slug,
        "commit": found.commit,
        "subject": found.subject,
        "paths": list(found.paths),
        "base": state.source_commit,
        "applies_on_base": True,
        "removable": True,
        "dependents": list(blocked_by),
        "resolution_sha256": resolution.get("resolution_sha256"),
    }


def stack_check(repo: Path) -> dict[str, Any]:
    """All case commits resolve on the base and reproduce HEAD's product tree."""
    state = embedded_develop_state(repo, "stack check")
    mapping = develop_map(repo, state.source_commit)
    resolution = selection_resolution(repo, state.source_commit, f"stacks/{ACTIVE_STACK}")
    statuses = {entry.get("case"): entry.get("status") for entry in resolution.get("patches", [])}
    stale = sorted(case for case, status in statuses.items() if status != "apply")
    if stale:
        fail(f"cases are already present on the upstream base: {stale}")
    selection = selection_module()
    with tempfile.TemporaryDirectory(prefix="xpra-fork-stack-") as raw:
        environment = {**os.environ, "GIT_INDEX_FILE": str(Path(raw) / "index"), **NONINTERACTIVE_GIT_ENV}
        run(("git", "-C", str(repo), "read-tree", state.source_commit), env=environment)
        for case in mapping.cases:
            result = subprocess.run(
                ("git", "-C", str(repo), "apply", "--cached", "--whitespace=nowarn", "-"),
                input=selection.case_diff(repo, case.commit),
                env=environment,
                capture_output=True,
                check=False,
            )
            if result.returncode:
                fail(f"case {case.slug} does not apply in stack order on the base")
        excluded = [f":(exclude){path.rstrip('/')}" for path in ALLOWED_DEVELOP_PATHS]
        difference = subprocess.run(
            ("git", "-C", str(repo), "diff", "--cached", "--name-only", "HEAD", "--", ".", *excluded),
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if difference.returncode or difference.stdout.strip():
            fail(f"base plus case commits differs from HEAD in: {difference.stdout.split()}")
    return {
        "base": state.source_commit,
        "head": mapping.head,
        "series": [case.slug for case in mapping.cases],
        "product_tree_matches_head": True,
        "resolution_sha256": resolution.get("resolution_sha256"),
    }


def case_rows(repo: Path) -> list[tuple[str, str, str]]:
    mapping = develop_map(repo)
    rows = [(case.slug, case.commit, case.subject) for case in mapping.cases]
    committed = {case.slug for case in mapping.cases}
    rows.extend((slug, "-", "(no commit)") for slug in case_directories() if slug not in committed)
    return rows


def develop_squash(repo: Path) -> str:
    """Fold every pending fixup!/amend!/squash! commit into its case commit."""
    verify_repo(repo, ())
    if current_branch(repo) != INTEGRATION_BRANCH:
        fail(f"current branch must be {INTEGRATION_BRANCH}")
    require_clean_product(repo, "develop-squash")
    state = embedded_develop_state(repo, "develop squash", allow_pending_fixups=True)
    result = run(
        (
            "git", "-C", str(repo), "-c", "commit.gpgsign=false", "rebase", "-i",
            "--autosquash", "--autostash", "--empty=drop", state.source_commit,
        ),
        check=False,
        env={**NONINTERACTIVE_GIT_ENV, "GIT_SEQUENCE_EDITOR": "true", "GIT_EDITOR": "true"},
    )
    if result.returncode:
        fail(
            "develop-squash stopped: the fixup overlaps another commit, so the cases are "
            "not independent; resolve it or run git rebase --abort\n"
            + "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
        )
    develop_map(repo, state.source_commit)
    return rev_parse(repo, "HEAD")


def case_drop(repo: Path, slug: str) -> str:
    """Remove one case commit from develop history (retirement)."""
    verify_repo(repo, ())
    if current_branch(repo) != INTEGRATION_BRANCH:
        fail(f"current branch must be {INTEGRATION_BRANCH}")
    require_clean_product(repo, "case-drop")
    found = case_commit(repo, slug)
    blocked_by = dependents(repo, slug)
    if blocked_by:
        fail(f"case {slug} is a dependency of {list(blocked_by)}; retire or rework them first")
    result = run(
        (
            "git", "-C", str(repo), "-c", "commit.gpgsign=false", "rebase", "--autostash",
            "--onto", f"{found.commit}^", found.commit, INTEGRATION_BRANCH,
        ),
        check=False,
        env=NONINTERACTIVE_GIT_ENV,
    )
    if result.returncode:
        fail(
            f"case-drop of {slug} stopped: a later commit depends on it; resolve or run "
            "git rebase --abort\n"
            + "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
        )
    return found.commit


CASE_MANIFEST_TEMPLATE = """schema = 2
slug = "{slug}"
kind = "production"
title = "TODO one-line description"
dependencies = []

[tests]
list = [
  # the case's focused modules, then the full legs:
  "full",
  "full-cython",
  "full-no-compat",
]

[evidence]
required_gates = [
  "live-rgb",
  "live-h264",
  "live-xpra-detach",
  "live-xpra-transport-loss",
  "live-wayland-keyboard",
  "live-x11-clipboard",
  "live-wayland-subsurface",
  "live-wayland-h264-hardware",
  "live-wayland-opengl-h264-hardware",
]
"""

CASE_README_TEMPLATE = """# TODO title

Code: the `Fork-Case: {slug}` commit on `develop`
(`make -C fork-maintenance case-show CASE={slug}`).

## Boundary

## Embedded-source context

## Surrounding code and ownership map

## Mechanism and lifecycle

## Case-commit and integration ownership

## Commit scope and non-goals

## Regression design and clean control

## Durable live or package boundary

## Invariants not to simplify

## Required validation
"""


def scaffold_case(repo: Path, slug: str) -> Path:
    verify_repo(repo, ())
    if not SLUG_RE.fullmatch(slug):
        fail(f"invalid case slug: {slug!r}")
    directory = CASES_ROOT / slug
    if directory.exists() or directory.is_symlink():
        fail(f"case already exists: {slug}")
    directory.mkdir(parents=True)
    (directory / "case.toml").write_text(CASE_MANIFEST_TEMPLATE.format(slug=slug), encoding="utf-8")
    (directory / "README.md").write_text(CASE_README_TEMPLATE.format(slug=slug), encoding="utf-8")
    return directory


def require_private_directory(path: Path, description: str) -> None:
    if path.is_symlink() or not path.is_dir():
        fail(f"{description} is missing or unsafe: {path}")
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o7777 != 0o700:
        fail(f"{description} must be owned by this user with mode 0700: {path}")


def publish_private_json(path: Path, value: object, description: str) -> None:
    """Durably publish one complete, immutable private owner record."""
    if not isinstance(value, dict):
        fail(f"{description} must be a JSON object")
    try:
        background_job.publish_json(path, value)
    except (background_job.BackgroundJobError, OSError) as error:
        fail(f"cannot publish {description} {path}: {error}")


def require_cycle_name(value: str) -> str:
    if not CYCLE_RE.fullmatch(value):
        fail("CYCLE must use lowercase words separated by single hyphens")
    return value


def cycle_matches(value: str, cycle: str) -> bool:
    return value.startswith(f"{cycle}-")


def cleanup_state_root(repo: Path) -> Path:
    return repo / ".artifacts" / "fork-maintenance"


def prepare_cleanup_directory(root: Path, path: Path, description: str) -> Path:
    """Create one private directory chain without following existing symlinks."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        fail(f"{description} escaped the cleanup state root: {path}")
    require_owned_directory(root, "fork-maintenance artifact root")
    cursor = root
    for part in relative.parts:
        cursor /= part
        if not cursor.exists() and not cursor.is_symlink():
            try:
                cursor.mkdir(mode=0o700)
                fsync_directory(cursor.parent)
            except OSError as error:
                fail(f"cannot create {description} {cursor}: {error}")
        require_owned_directory(cursor, description)
        if stat.S_IMODE(cursor.lstat().st_mode) != 0o700:
            fail(f"{description} mode is not exactly 0700: {cursor}")
    return path


def cleanup_lock_paths(repo: Path) -> tuple[Path, ...]:
    root = cleanup_state_root(repo)
    return (
        root / "upstream-tests" / "logs" / ".lifecycle.lock",
        root / "upstream-tests" / "image-builds" / ".image-cache.lock",
        root / "jobs" / "live" / ".lifecycle.lock",
        root / "deb-packages" / "locks" / "terminal.lock",
    )


def open_retained_lifecycle_lock(path: Path, description: str) -> int:
    """Open and exclusively acquire one exact crash-releasing retained lock."""
    expected: os.stat_result | None = None
    if path.exists() or path.is_symlink():
        expected = require_cleanup_file(path, description)
        if stat.S_IMODE(expected.st_mode) != 0o600:
            fail(f"{description} mode is not exactly 0600: {path}")
    try:
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
    except OSError as error:
        fail(f"cannot open {description} {path}: {error}")
    info = os.fstat(descriptor)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
        or (
            expected is not None
            and (info.st_dev, info.st_ino) != (expected.st_dev, expected.st_ino)
        )
    ):
        os.close(descriptor)
        fail(f"{description} is unsafe: {path}")
    try:
        os.fsync(descriptor)
        fsync_directory(path.parent)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            current = path.lstat()
        except OSError as error:
            fail(f"{description} disappeared while acquiring it: {path}: {error}")
        locked = os.fstat(descriptor)
        if (
            path.is_symlink()
            or (current.st_dev, current.st_ino) != (locked.st_dev, locked.st_ino)
        ):
            fail(f"{description} changed while acquiring it: {path}")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


@contextmanager
def cleanup_lifecycle_locks(repo: Path) -> Iterator[None]:
    """Exclude every subsystem pre-owner window in one fixed lock order."""
    root = cleanup_state_root(repo)
    descriptors: list[int] = []
    try:
        for path in cleanup_lock_paths(repo):
            prepare_cleanup_directory(root, path.parent, "cleanup lock directory")
            descriptor = open_retained_lifecycle_lock(
                path,
                "cleanup lifecycle lock",
            )
            descriptors.append(descriptor)
        yield
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def require_owned_directory(
    path: Path,
    description: str,
    *,
    private: bool = True,
) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        fail(f"{description} is unavailable: {path}: {error}")
    if path.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        fail(f"{description} is not a real owned directory: {path}")
    unsafe = 0o077 if private else 0o002
    if stat.S_IMODE(info.st_mode) & unsafe:
        label = "private" if private else "not other-writable"
        fail(f"{description} must be {label}: {path}")


def require_cleanup_file(path: Path, description: str) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as error:
        fail(f"{description} is unavailable: {path}: {error}")
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        fail(f"{description} is not a private singly-linked owned file: {path}")
    return info


def parse_status_file(path: Path) -> dict[str, str]:
    require_cleanup_file(path, "collected status")
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        fail(f"cannot read collected status {path}: {error}")
    for line in lines:
        if "=" not in line:
            fail(f"collected status contains a malformed line: {path}")
        key, value = line.split("=", 1)
        if not key or key in values:
            fail(f"collected status contains a duplicate or empty key: {path}")
        values[key] = value
    return values


def secure_tree_fingerprint(
    path: Path,
    *,
    private: bool = True,
    foreign_links: bool = False,
) -> str:
    """Fingerprint a tree without following links.

    Owner-bound results admit only contained relative links.  Disposable
    artifact trees (for example virtual environments) may also carry links to
    host paths; ``foreign_links`` records their exact target text instead of
    rejecting it.  Deletion never follows a link either way.
    """
    require_owned_directory(path, "cycle cleanup directory", private=private)
    entries: list[dict[str, Any]] = []
    for candidate in sorted(path.rglob("*")):
        relative = candidate.relative_to(path).as_posix()
        info = candidate.lstat()
        if info.st_uid != os.getuid():
            fail(f"cycle cleanup tree contains an unsafe path: {candidate}")
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(candidate)
            target_path = Path(target)
            depth = len(candidate.parent.relative_to(path).parts)
            if target_path.is_absolute() and not foreign_links:
                fail(f"cycle cleanup symlink has an absolute target: {candidate}")
            for part in () if foreign_links else target_path.parts:
                if part in ("", "."):
                    continue
                if part == "..":
                    depth -= 1
                    if depth < 0:
                        fail(f"cycle cleanup symlink escapes its owned tree: {candidate}")
                else:
                    depth += 1
            try:
                target.encode("utf-8")
            except UnicodeEncodeError as error:
                fail(f"cycle cleanup symlink target is not UTF-8: {candidate}: {error}")
            entries.append(
                {
                    "path": relative,
                    "type": "symlink",
                    "target": target,
                }
            )
        elif stat.S_ISDIR(info.st_mode):
            # The owner-bound tree root is private, so group-writable input
            # directories inside it are no more externally reachable than the
            # already accepted group-writable regular files.  Retain their
            # exact mode in the relocatable fingerprint, but reject paths that
            # another user could mutate.
            if mode & 0o002:
                fail(f"cycle cleanup tree contains an other-writable directory: {candidate}")
            entries.append({"path": relative, "type": "directory", "mode": mode})
        elif stat.S_ISREG(info.st_mode):
            if mode & 0o002 or info.st_nlink != 1:
                fail(f"cycle cleanup tree contains an unsafe file: {candidate}")
            entries.append(
                {
                    "path": relative,
                    "type": "file",
                    "mode": mode,
                    "sha256": sha256_file(candidate),
                }
            )
        else:
            fail(f"cycle cleanup tree contains a special file: {candidate}")
    return sha256_bytes(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode())


def artifact_fingerprint(path: Path) -> str:
    """Discard identity, not result acceptance; the ancestor must be private.

    Old probes need not have a runner's 0600/0700 modes. Bind their actual mode
    and content without relaxing any existing collected-result validation.
    """
    info = path.lstat()
    mode = stat.S_IMODE(info.st_mode)
    if info.st_uid != os.getuid() or mode & 0o002:
        fail(f"artifact is not safely owned: {path}")
    if stat.S_ISDIR(info.st_mode):
        content = secure_tree_fingerprint(path, private=False, foreign_links=True)
    elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
        content = sha256_file(path)
    else:
        fail(f"artifact is not a real directory or singly-linked file: {path}")
    return sha256_bytes(f"{mode}:{content}".encode())


CLEANUP_DIRECTORY_KINDS = {"live-result-tree", "artifact-tree"}


def require_cycle_plan(plan: CleanupPlan) -> None:
    if plan.cycle.startswith("artifacts-") or any(
        target.kind.startswith("artifact-") for target in plan.targets
    ):
        fail("this is a structural artifacts cleanup; use artifacts-clean")


def require_status_keys(
    values: dict[str, str],
    keys: Iterable[str],
    description: str,
) -> None:
    missing = sorted(set(keys).difference(values))
    if missing:
        fail(f"{description} is missing current-schema fields: {missing}")


def validate_upstream_status(values: dict[str, str], name: str, root: Path) -> str:
    """Validate one finalized upstream-test or standalone-image status record."""
    schema = values.get("schema")
    if schema not in {"2", "3"}:
        fail(f"collected upstream result has an unsupported schema: {name}")
    common = {
        "exit_code",
        "finished",
        "log_sha256",
        "logs_ok",
        "name",
        "owner",
        "result",
        "run_id",
        "runner_sha256",
        "schema",
        "selection_resolution_ok",
        "selection_resolution_sha256",
        "source",
        "validation_ok",
        "workflow_sha256",
    }
    require_status_keys(values, common, f"collected upstream result {name}")
    if values["owner"] != UPSTREAM_TEST_OWNER or values["name"] != name:
        fail(f"collected upstream result has an inconsistent identity: {name}")
    validation = values["validation_ok"]
    if validation not in {"0", "1"}:
        fail(f"collected upstream result has an invalid validation status: {name}")
    expected_result = "success" if validation == "1" else "failed"
    if values["result"] != expected_result:
        fail(f"collected upstream result contradicts its validation status: {name}")
    for key in ("log_sha256", "runner_sha256", "workflow_sha256"):
        if not SHA256_RE.fullmatch(values[key]):
            fail(f"collected upstream result has an invalid {key}: {name}")
    if not GIT_SHA_RE.fullmatch(values["source"]):
        fail(f"collected upstream result has an invalid source commit: {name}")
    if not UUID4_RE.fullmatch(values["run_id"]):
        fail(f"collected upstream result has an invalid run identity: {name}")
    if not re.fullmatch(r"-?[0-9]+", values["exit_code"]):
        fail(f"collected upstream result has an invalid exit code: {name}")
    if values["logs_ok"] not in {"0", "1"}:
        fail(f"collected upstream result has invalid log state: {name}")
    if values["selection_resolution_ok"] not in {"0", "1"}:
        fail(f"collected upstream result has invalid resolution state: {name}")
    if validation == "1" and not values["finished"]:
        fail(f"successful upstream result has no completion timestamp: {name}")

    if schema == "3":
        test_fields = {
            "container_exit",
            "container_id",
            "container_present",
            "container_status",
            "expected_image_id",
            "image",
            "image_id",
            "image_input_sha256",
            "patch_mode",
            "payload_path",
            "selection",
            "selection_sha256",
            "source_head",
            "source_remote",
            "target",
        }
        if set(values) != common | test_fields:
            fail(f"collected upstream test result is not the current owned schema: {name}")
        if (
            not SHA256_RE.fullmatch(values["container_id"])
            or values["container_present"] != "1"
            or not re.fullmatch(r"-?[0-9]+", values["container_exit"])
            or not values["container_status"]
            or not SHA256_RE.fullmatch(values["expected_image_id"])
            or values["image_id"] != values["expected_image_id"]
            or not SHA256_RE.fullmatch(values["image_input_sha256"])
            or not SHA256_RE.fullmatch(values["selection_sha256"])
            or not GIT_SHA_RE.fullmatch(values["source_head"])
            or not SELECTION_RE.fullmatch(values["selection"])
            or values["patch_mode"] not in RUNNER_PATCH_MODES
            or values["source_remote"] not in (*REMOTE_URLS, "local")
            or not TEST_RE.fullmatch(values["target"])
            or not values["image"]
            or values["payload_path"]
            != str(root / "upstream-tests" / "runs" / f"{name}.payload")
        ):
            fail(f"collected upstream test provenance is invalid: {name}")
        if validation == "1" and (
            values["exit_code"] != "0"
            or values["container_exit"] != "0"
            or values["container_present"] != "1"
            or values["container_status"] != "exited"
            or values["logs_ok"] != "1"
        ):
            fail(f"successful upstream test status is internally inconsistent: {name}")
        return "test"

    image_fields = {
        "iid_ok",
        "image",
        "image_builder",
        "image_exists",
        "image_id",
        "image_input_sha256",
    }
    if set(values) != common | image_fields:
        fail(f"collected upstream image result is not the current owned schema: {name}")
    if values["selection_resolution_ok"] != "0" or values["selection_resolution_sha256"]:
        fail(f"collected upstream image result has unexpected patch resolution: {name}")
    if validation == "1" and (
        values["exit_code"] != "0"
        or values["iid_ok"] != "1"
        or values["image_exists"] != "1"
        or values["image_builder"] != "true"
        or not SHA256_RE.fullmatch(values["image_id"])
        or not SHA256_RE.fullmatch(values["image_input_sha256"])
        or values["logs_ok"] != "1"
    ):
        fail(f"successful upstream image status is internally inconsistent: {name}")
    return "image"


def validate_upstream_remove_transaction(
    marker: Path,
    name: str,
    status: Path,
    log: Path,
    status_values: dict[str, str],
    result_kind: str,
) -> None:
    payload = load_cleanup_json(marker, "upstream removal transaction")
    if set(payload) != {
        "schema",
        "owner",
        "kind",
        "name",
        "record",
        "owner_sha256",
        "log_sha256",
        "status_sha256",
    }:
        fail(f"upstream removal transaction has unexpected fields: {name}")
    expected_kind = "test-remove" if result_kind == "test" else "image-build-remove"
    record = payload.get("record")
    if (
        payload.get("schema") != 1
        or payload.get("owner") != UPSTREAM_TEST_OWNER
        or payload.get("kind") != expected_kind
        or payload.get("name") != name
        or not isinstance(record, dict)
        or record.get("name") != name
        or record.get("owner") != UPSTREAM_TEST_OWNER
        or not SHA256_RE.fullmatch(str(payload.get("owner_sha256", "")))
        or payload.get("log_sha256") != sha256_file(log)
        or payload.get("status_sha256") != sha256_file(status)
    ):
        fail(f"upstream removal transaction identity is inconsistent: {name}")
    common = {
        "runner_sha256": status_values["runner_sha256"],
        "source": status_values["source"],
        "workflow_sha256": status_values["workflow_sha256"],
    }
    if any(str(record.get(key, "")) != value for key, value in common.items()):
        fail(f"upstream removal transaction provenance differs: {name}")
    if result_kind == "test":
        expected = {
            "run_id": status_values["run_id"],
            "container_id": status_values["container_id"],
            "image": status_values["image"],
            "image_id": status_values["image_id"],
            "image_input_sha256": status_values["image_input_sha256"],
            "patch_mode": status_values["patch_mode"],
            "payload_path": status_values["payload_path"],
            "selection": status_values["selection"],
            "selection_sha256": status_values["selection_sha256"],
            "source_head": status_values["source_head"],
            "source_remote": status_values["source_remote"],
            "target": status_values["target"],
        }
        if record.get("schema") != "4" or any(
            str(record.get(key, "")) != value for key, value in expected.items()
        ):
            fail(f"upstream test removal ownership differs: {name}")
    elif (
        record.get("schema") not in {2, 3}
        or record.get("kind") != "image-build"
        or str(record.get("job_id", "")) != status_values["run_id"]
        or str(record.get("image", "")) != status_values["image"]
        or str(record.get("input_sha256", ""))
        != status_values["image_input_sha256"]
    ):
        fail(f"upstream image removal ownership differs: {name}")


def upstream_result_targets(root: Path, cycle: str) -> tuple[list[CleanupTarget], set[str]]:
    logs = root / "upstream-tests" / "logs"
    if not logs.exists():
        return [], set()
    require_owned_directory(logs, "upstream-test log root")
    retained_lock = logs / ".lifecycle.lock"
    suffixes = (
        ".selection-resolution.sha256",
        ".selection-resolution.json",
        ".remove.json",
        ".status",
        ".log",
    )
    groups: dict[str, dict[str, Path]] = {}
    for path in logs.iterdir():
        if path == retained_lock:
            continue
        matched_suffix = next((suffix for suffix in suffixes if path.name.endswith(suffix)), None)
        if matched_suffix is None:
            if cycle_matches(path.name.lstrip("."), cycle):
                fail(f"unrecognized cycle artifact in upstream-test logs: {path}")
            continue
        name = path.name[: -len(matched_suffix)]
        if cycle_matches(name, cycle):
            groups.setdefault(name, {})[matched_suffix] = path

    targets: list[CleanupTarget] = []
    for name, paths in sorted(groups.items()):
        if (
            set(paths).difference(suffixes)
            or ".status" not in paths
            or ".log" not in paths
            or ".remove.json" not in paths
        ):
            fail(f"collected upstream result is incomplete: {name}")
        status_values = parse_status_file(paths[".status"])
        result_kind = validate_upstream_status(status_values, name, root)
        log = paths[".log"]
        require_cleanup_file(log, "collected upstream log")
        if status_values.get("log_sha256") != sha256_file(log):
            fail(f"collected upstream log digest does not match: {name}")
        try:
            log_resolution_digests = re.findall(
                rb"(?m)^selection_resolution_sha256=([0-9a-f]{64})\r?$",
                log.read_bytes(),
            )
        except OSError as error:
            fail(f"cannot read collected upstream log {log}: {error}")
        resolution_paths = {
            ".selection-resolution.json",
            ".selection-resolution.sha256",
        }.intersection(paths)
        resolution_ok = status_values["selection_resolution_ok"]
        if resolution_ok == "1":
            recorded_digest = status_values.get("selection_resolution_sha256", "")
            if not SHA256_RE.fullmatch(recorded_digest):
                fail(f"collected upstream resolution digest is invalid: {name}")
            if log_resolution_digests != [recorded_digest.encode("ascii")]:
                fail(f"collected upstream log resolution digest does not match: {name}")
            if resolution_paths:
                if len(resolution_paths) != 2:
                    fail(f"collected upstream resolution is incomplete: {name}")
                resolution = paths[".selection-resolution.json"]
                resolution_digest = paths[".selection-resolution.sha256"]
                require_cleanup_file(resolution, "collected selection resolution")
                require_cleanup_file(resolution_digest, "collected resolution digest")
                try:
                    legacy_digest = resolution_digest.read_text(encoding="ascii").strip()
                    resolution_payload = json.loads(resolution.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                    fail(f"cannot read collected resolution digest {resolution_digest}: {error}")
                if not isinstance(resolution_payload, dict):
                    fail(f"collected upstream resolution is not a JSON object: {name}")
                contract_digest = resolution_payload.get("resolution_sha256")
                if contract_digest != legacy_digest or contract_digest != recorded_digest:
                    fail(f"collected upstream resolution digest does not match: {name}")
        else:
            if status_values["selection_resolution_sha256"]:
                fail(f"collected upstream resolution status is inconsistent: {name}")
            if resolution_paths:
                fail(f"unexpected collected upstream resolution files: {name}")
            if result_kind == "test" and len(log_resolution_digests) == 1:
                fail(f"collected upstream resolution status does not match its log: {name}")
        validate_upstream_remove_transaction(
            paths[".remove.json"],
            name,
            paths[".status"],
            log,
            status_values,
            result_kind,
        )
        for path in sorted(paths.values()):
            require_cleanup_file(path, "collected upstream artifact")
            targets.append(CleanupTarget("upstream-result", path, sha256_file(path)))
    return targets, set(groups)


def load_cleanup_json(path: Path, description: str) -> dict[str, Any]:
    require_cleanup_file(path, description)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        fail(f"cannot read {description} {path}: {error}")
    if not isinstance(value, dict):
        fail(f"{description} must contain a JSON object: {path}")
    return value


def validate_live_status(
    status: dict[str, Any],
    name: str,
    result_directory: Path,
) -> tuple[Path, str]:
    """Validate one removed live job's immutable current-schema status."""
    require_owned_directory(result_directory / "inputs", "collected live input tree")
    required = {
        "background_supervisor_sha256",
        "collected_at",
        "exit_code",
        "finished_at",
        "harness_sha256",
        "input_provenance",
        "job_id",
        "log_sha256",
        "logs_ok",
        "owned_objects_remaining",
        "owner",
        "process_pid",
        "report",
        "report_checks",
        "report_result",
        "report_sha256",
        "result",
        "run",
        "runner_sha256",
        "schema",
        "supervisor_sha256",
        "validation_ok",
    }
    missing = sorted(required.difference(status))
    if missing:
        fail(f"collected live result {name} is missing current-schema fields: {missing}")
    if set(status) != required:
        fail(f"collected live result is not the current owned schema: {name}")
    if (
        status["schema"] != 3
        or status["owner"] != LIVE_JOB_OWNER
        or status["run"] != name
        or not UUID4_RE.fullmatch(str(status["job_id"]))
        or not isinstance(status["exit_code"], int)
        or isinstance(status["exit_code"], bool)
        or not isinstance(status["process_pid"], int)
        or isinstance(status["process_pid"], bool)
        or status["process_pid"] < 1
        or not isinstance(status["collected_at"], str)
        or not status["collected_at"]
        or not isinstance(status["finished_at"], str)
        or not status["finished_at"]
        or not isinstance(status["report_result"], str)
        or not isinstance(status["report_sha256"], str)
        or status["result"] not in {"success", "failed"}
        or status["logs_ok"] is not True
        or not isinstance(status["validation_ok"], bool)
    ):
        fail(f"collected live result identity is inconsistent: {name}")
    for key in (
        "background_supervisor_sha256",
        "harness_sha256",
        "log_sha256",
        "runner_sha256",
        "supervisor_sha256",
    ):
        if not isinstance(status[key], str) or not SHA256_RE.fullmatch(status[key]):
            fail(f"collected live result has an invalid {key}: {name}")

    provenance = status["input_provenance"]
    provenance_hashes = {
        "client_context_archive_sha256",
        "client_context_sha256",
        "client_selection_resolution_sha256",
        "client_selection_sha256",
        "harness_sha256",
        "input_manifest_sha256",
        "input_tree_sha256",
        "server_context_archive_sha256",
        "server_context_sha256",
        "server_selection_resolution_sha256",
        "server_selection_sha256",
        "source_archive_sha256",
        "source_workflow_sha256",
    }
    expected_provenance = provenance_hashes | {
        "client_selection",
        "harness",
        "keyboard_scenario",
        "path",
        "schema",
        "server_selection",
        "source_commit",
        "source_commit_marker",
        "source_revision",
        "zed_archive_sha256",
        "zed_binary_sha256",
    }
    harness = provenance.get("harness") if isinstance(provenance, dict) else None
    if (
        not isinstance(provenance, dict)
        or set(provenance) != expected_provenance
        or provenance.get("schema") != 2
        or provenance.get("path") != str(result_directory / "inputs")
        or provenance.get("harness_sha256") != status["harness_sha256"]
        or not GIT_SHA_RE.fullmatch(str(provenance.get("source_commit", "")))
        or not isinstance(harness, dict)
        or not harness
        or any(
            not isinstance(path, str)
            or not path
            or Path(path).is_absolute()
            or ".." in Path(path).parts
            or not SHA256_RE.fullmatch(str(digest))
            for path, digest in harness.items()
        )
        or (
            provenance.get("server_selection") != "master"
            and not SELECTION_RE.fullmatch(str(provenance.get("server_selection", "")))
        )
        or not isinstance(provenance.get("source_commit_marker"), str)
        or not isinstance(provenance.get("source_revision"), int)
        or isinstance(provenance.get("source_revision"), bool)
        or provenance["source_revision"] < 0
        or any(
            not SHA256_RE.fullmatch(str(provenance.get(key, "")))
            for key in provenance_hashes
        )
    ):
        fail(f"collected live input provenance is inconsistent: {name}")
    # All current live profiles bind the complete queue to both endpoints.
    # Retired case/clean-client records remain readable only for owned cleanup;
    # they cannot satisfy the current live suite admission or acceptance.
    if provenance["client_selection"] != "master" and (
        provenance["client_selection"] not in (
            "stacks/develop",
            "cases/x11-client-clipboard-events",
            "cases/wayland-subsurface-stream-ownership",
        )
        or provenance["client_selection"] != provenance["server_selection"]
        or any(
            provenance[f"client_{field}"] != provenance[f"server_{field}"]
            for field in (
                "selection_sha256",
                "selection_resolution_sha256",
                "context_sha256",
                "context_archive_sha256",
            )
        )
    ):
        fail(f"collected live endpoint provenance is inconsistent: {name}")
    zed_archive = provenance.get("zed_archive_sha256")
    zed_binary = provenance.get("zed_binary_sha256")
    if (zed_archive is None) != (zed_binary is None) or (
        zed_archive is not None
        and (
            not SHA256_RE.fullmatch(str(zed_archive))
            or not SHA256_RE.fullmatch(str(zed_binary))
        )
    ):
        fail(f"collected live Zed provenance is inconsistent: {name}")
    keyboard_scenario = provenance.get("keyboard_scenario")
    if keyboard_scenario is not None and (
        not isinstance(keyboard_scenario, dict)
        or set(keyboard_scenario) != {"name", "path", "schema", "sha256"}
        or type(keyboard_scenario.get("schema")) is not int
        or keyboard_scenario.get("schema") != 1
        or not isinstance(keyboard_scenario.get("name"), str)
        or not SLUG_RE.fullmatch(keyboard_scenario["name"])
        or not isinstance(keyboard_scenario.get("path"), str)
        or re.fullmatch(
            r"cases/[a-z0-9]+(?:-[a-z0-9]+)*/tests/live-wayland-keyboard\.json",
            keyboard_scenario["path"],
        )
        is None
        or not SHA256_RE.fullmatch(str(keyboard_scenario.get("sha256", "")))
    ):
        fail(f"collected live keyboard scenario provenance is inconsistent: {name}")
    keyboard_scenario_path = result_directory / "inputs" / "keyboard-scenario.json"
    if keyboard_scenario is None:
        if keyboard_scenario_path.exists() or keyboard_scenario_path.is_symlink():
            fail(f"collected live result has unexpected keyboard scenario data: {name}")
    else:
        require_cleanup_file(
            keyboard_scenario_path,
            "collected live keyboard scenario",
        )
        if sha256_file(keyboard_scenario_path) != keyboard_scenario["sha256"]:
            fail(f"collected live keyboard scenario digest does not match: {name}")

    checks = status["report_checks"]
    expected_report_checks = {
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
    }
    validation_ok = (
        isinstance(checks, dict)
        and set(checks) == expected_report_checks
        and bool(checks)
        and all(isinstance(value, bool) and value for value in checks.values())
    )
    objects = status["owned_objects_remaining"]
    if (
        not isinstance(checks, dict)
        or (checks and set(checks) != expected_report_checks)
        or any(not isinstance(value, bool) for value in checks.values())
        or not isinstance(objects, dict)
        or set(objects) != {"containers", "networks"}
        or any(
            not isinstance(values, list)
            or any(not isinstance(value, str) or not value for value in values)
            for values in objects.values()
        )
        or status["validation_ok"] is not validation_ok
    ):
        fail(f"collected live validation state is inconsistent: {name}")
    expected_result = (
        "success"
        if status["exit_code"] == 0
        and validation_ok
        and objects == {"containers": [], "networks": []}
        else "failed"
    )
    if status["result"] != expected_result:
        fail(f"collected live result contradicts its validation state: {name}")

    report = result_directory / "report.json"
    if status["report"] != str(report):
        fail(f"collected live report path is inconsistent: {name}")
    recorded_report_sha256 = status["report_sha256"]
    if report.exists() or report.is_symlink():
        if recorded_report_sha256:
            require_cleanup_file(report, "collected live report")
            if (
                not isinstance(recorded_report_sha256, str)
                or not SHA256_RE.fullmatch(recorded_report_sha256)
                or recorded_report_sha256 != sha256_file(report)
            ):
                fail(f"collected live report digest does not match: {name}")
            try:
                report_payload = json.loads(report.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                fail(f"cannot read collected live report {report}: {error}")
            if (
                not isinstance(report_payload, dict)
                or status["report_result"]
                != str(report_payload.get("result", "missing"))
            ):
                fail(f"collected live report result is inconsistent: {name}")
        elif (
            status["report_result"] != "missing"
            or checks
            or status["validation_ok"]
        ):
            fail(f"unbound collected live report contradicts its failed status: {name}")
    elif recorded_report_sha256 or status["report_result"] != "missing":
        fail(f"collected live report is missing: {name}")
    if validation_ok and status["report_result"] != "passed":
        fail(f"successful live validation has no passing report: {name}")
    return report, str(recorded_report_sha256)


def validate_live_remove_transaction(
    marker: Path,
    name: str,
    status_path: Path,
    log_path: Path,
    status: dict[str, Any],
) -> None:
    payload = load_cleanup_json(marker, "live removal transaction")
    if set(payload) != {
        "schema",
        "owner",
        "kind",
        "run",
        "record",
        "log_sha256",
        "status_sha256",
        "runtime_sha256",
    }:
        fail(f"live removal transaction has unexpected fields: {name}")
    record = payload.get("record")
    runtime_sha256 = payload.get("runtime_sha256")
    runtime_keys = {
        "owner",
        "runtime",
        "completion",
        "freeze_owner",
        "freeze_runtime",
        "freeze_completion",
        "freeze_result",
    }
    if (
        payload.get("schema") != 1
        or payload.get("owner") != LIVE_JOB_OWNER
        or payload.get("kind") != "live-remove"
        or payload.get("run") != name
        or payload.get("log_sha256") != sha256_file(log_path)
        or payload.get("status_sha256") != sha256_file(status_path)
        or not isinstance(record, dict)
        or record.get("schema") != 4
        or record.get("owner") != LIVE_JOB_OWNER
        or record.get("run") != name
        or record.get("job_id") != status["job_id"]
        or record.get("result_report") != status["report"]
        or record.get("input_provenance") != status["input_provenance"]
        or not isinstance(runtime_sha256, dict)
        or "owner" not in runtime_sha256
        or not set(runtime_sha256).issubset(runtime_keys)
        or any(
            not isinstance(value, str) or not SHA256_RE.fullmatch(value)
            for value in runtime_sha256.values()
        )
    ):
        fail(f"live removal transaction identity is inconsistent: {name}")
    for key in (
        "background_supervisor_sha256",
        "harness_sha256",
        "runner_sha256",
        "supervisor_sha256",
    ):
        if record.get(key) != status[key]:
            fail(f"live removal transaction provenance differs for {key}: {name}")


def validate_live_freeze_abort_transaction(root: Path, marker: Path, name: str) -> None:
    """Validate enough of a live freeze-only abort to keep it a blocker."""
    payload = load_cleanup_json(marker, "live input-freeze abort transaction")
    if set(payload) != {
        "directories",
        "freeze_owner_sha256",
        "kind",
        "owner",
        "run",
        "schema",
    }:
        fail(f"live input-freeze abort transaction has unexpected fields: {name}")
    freeze_owner = root / "jobs" / "live" / f"{name}.freeze.json"
    if (
        payload.get("schema") != 1
        or payload.get("owner") != LIVE_JOB_OWNER
        or payload.get("kind") != "live-input-freeze-abort"
        or payload.get("run") != name
        or not SHA256_RE.fullmatch(str(payload.get("freeze_owner_sha256", "")))
    ):
        fail(f"live input-freeze abort transaction identity is inconsistent: {name}")
    require_cleanup_file(freeze_owner, "live input-freeze owner")
    if sha256_file(freeze_owner) != payload["freeze_owner_sha256"]:
        fail(f"live input-freeze abort owner digest differs: {name}")
    directories = payload.get("directories")
    if not isinstance(directories, dict) or set(directories) != {"result", "staging"}:
        fail(f"live input-freeze abort directories are inconsistent: {name}")
    result_root = root / "live-results"
    for key in ("result", "staging"):
        entry = directories.get(key)
        if not isinstance(entry, dict):
            fail(f"live input-freeze abort directory entry is invalid: {name}")
        present = entry.get("present")
        expected_keys = {"present", "removal", "source"}
        if present is True:
            expected_keys.update({"device", "inode"})
            device = entry.get("device")
            inode = entry.get("inode")
            if (
                not isinstance(device, int)
                or isinstance(device, bool)
                or device < 0
                or not isinstance(inode, int)
                or isinstance(inode, bool)
                or inode <= 0
            ):
                fail(f"live input-freeze abort directory identity is invalid: {name}")
        elif present is not False:
            fail(f"live input-freeze abort directory presence is invalid: {name}")
        if set(entry) != expected_keys:
            fail(f"live input-freeze abort directory entry is inconsistent: {name}")
        removal = result_root / f".{name}.freeze-abort-{key}"
        source = Path(str(entry.get("source", "")))
        if entry.get("removal") != str(removal):
            fail(f"live input-freeze abort staging path is inconsistent: {name}")
        if key == "result":
            expected_source = result_root / name
            if source != expected_source:
                fail(f"live input-freeze abort result path is inconsistent: {name}")
        elif (
            source.parent != result_root
            or re.fullmatch(
                rf"\.{re.escape(name)}\.freeze-[0-9a-f]{{8}}-[0-9a-f]{{4}}-"
                r"4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                source.name,
            )
            is None
        ):
            fail(f"live input-freeze abort source staging is inconsistent: {name}")
        source_present = source.exists() or source.is_symlink()
        removal_present = removal.exists() or removal.is_symlink()
        if source_present and removal_present:
            fail(f"live input-freeze abort has both source and staging: {name}")
        if present is False:
            if source_present or removal_present:
                fail(f"live input-freeze abort has an unexpected directory: {name}")
            continue
        selected = source if source_present else removal if removal_present else None
        if selected is not None:
            require_private_directory(selected, "live input-freeze abort directory")
            details = selected.lstat()
            if details.st_dev != entry["device"] or details.st_ino != entry["inode"]:
                fail(f"live input-freeze abort directory identity changed: {name}")


def live_result_targets(root: Path, cycle: str) -> tuple[list[CleanupTarget], set[str]]:
    jobs = root / "jobs" / "live"
    results = root / "live-results"
    if not jobs.exists() and not results.exists():
        return [], set()
    if jobs.exists():
        require_owned_directory(jobs, "live-job record root")
        retained_lock = jobs / ".lifecycle.lock"
    else:
        retained_lock = jobs / ".lifecycle.lock"
    if results.exists():
        require_owned_directory(results, "live-result root")

    names: set[str] = set()
    if jobs.exists():
        for path in jobs.iterdir():
            if path == retained_lock:
                continue
            for suffix in (
                ".freeze-abort.json",
                ".status.json",
                ".remove.json",
                ".log",
                ".owner.json",
            ):
                if path.name.endswith(suffix):
                    name = path.name[: -len(suffix)]
                    if cycle_matches(name, cycle):
                        names.add(name)
                    break
            else:
                if cycle_matches(path.name.lstrip("."), cycle):
                    fail(f"unrecognized cycle artifact in live-job records: {path}")
    if results.exists():
        for path in results.iterdir():
            if path.name.startswith(".") and cycle_matches(
                path.name.lstrip("."), cycle
            ):
                fail(f"live run has incomplete input-freeze staging: {path}")
            if cycle_matches(path.name, cycle):
                names.add(path.name)

    targets: list[CleanupTarget] = []
    for name in sorted(names):
        status_path = jobs / f"{name}.status.json"
        log_path = jobs / f"{name}.log"
        remove_path = jobs / f"{name}.remove.json"
        owner_path = jobs / f"{name}.owner.json"
        if owner_path.exists() or owner_path.is_symlink():
            fail(
                f"live run {name} still has runtime ownership; collect it and run "
                f"live-remove first"
            )
        if (
            not status_path.exists()
            or not log_path.exists()
            or not remove_path.exists()
        ):
            fail(f"collected live result is incomplete: {name}")
        status_values = load_cleanup_json(status_path, "collected live status")
        require_cleanup_file(log_path, "collected live log")
        if status_values.get("log_sha256") != sha256_file(log_path):
            fail(f"collected live log digest does not match: {name}")
        result_directory = results / name
        if not result_directory.exists() or result_directory.is_symlink():
            fail(f"collected live result directory is missing or unsafe: {name}")
        validate_live_status(status_values, name, result_directory)
        validate_live_remove_transaction(
            remove_path,
            name,
            status_path,
            log_path,
            status_values,
        )
        for path in (status_path, log_path, remove_path):
            targets.append(CleanupTarget("live-result", path, sha256_file(path)))
        fingerprint = secure_tree_fingerprint(result_directory)
        targets.append(CleanupTarget("live-result-tree", result_directory, fingerprint))
    return targets, names


def deb_selection_tree_sha256(root: Path) -> str:
    """Reproduce the immutable DEB selection-cache tree identity."""
    require_owned_directory(root, "DEB selection cache tree")
    if stat.S_IMODE(root.lstat().st_mode) != 0o700:
        fail(f"DEB selection cache tree mode is not exactly 0700: {root}")
    entries: list[tuple[Path, os.stat_result]] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        child_directories: list[Path] = []
        for child in sorted(directory.iterdir(), key=lambda item: os.fsencode(item.name)):
            try:
                details = child.lstat()
            except OSError as error:
                fail(f"cannot inspect DEB selection cache entry {child}: {error}")
            if details.st_uid != os.getuid():
                fail(f"DEB selection cache entry has the wrong owner: {child}")
            if stat.S_ISDIR(details.st_mode):
                if stat.S_IMODE(details.st_mode) != 0o700:
                    fail(f"DEB selection cache directory mode is not 0700: {child}")
                child_directories.append(child)
            elif stat.S_ISREG(details.st_mode):
                if stat.S_IMODE(details.st_mode) != 0o600 or details.st_nlink != 1:
                    fail(f"DEB selection cache file is not exactly private: {child}")
            else:
                fail(f"unsupported DEB selection cache entry: {child}")
            entries.append((child, details))
        pending.extend(reversed(child_directories))

    digest = hashlib.sha256(b"xpra-deb-selection-tree-v1\0")
    for path, details in sorted(
        entries,
        key=lambda item: item[0].relative_to(root).as_posix(),
    ):
        relative = path.relative_to(root).as_posix()
        kind = b"directory" if stat.S_ISDIR(details.st_mode) else b"file"
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(kind)
        digest.update(b"\0")
        digest.update(f"{stat.S_IMODE(details.st_mode):04o}".encode("ascii"))
        digest.update(b"\0")
        if kind == b"file":
            digest.update(str(details.st_size).encode("ascii"))
            digest.update(b"\0")
            digest.update(bytes.fromhex(sha256_file(path)))
        digest.update(b"\0")
    return digest.hexdigest()


def deb_selection_semantic_digest(snapshot: Path) -> str:
    result = run(
        (
            sys.executable,
            str(SELECTION_TOOL),
            "--lab-root",
            str(snapshot),
            "--selection",
            f"stacks/{ACTIVE_STACK}",
            "digest",
        ),
        check=False,
    )
    digest = result.stdout.strip()
    if result.returncode or not SHA256_RE.fullmatch(digest):
        fail(f"cannot validate retained DEB selection cache: {snapshot}")
    return digest


def validate_deb_retained_state(package_root: Path) -> tuple[str, ...]:
    """Validate retained DEB locks/caches and report incomplete publications."""
    blockers: list[str] = []
    locks = package_root / "locks"
    if locks.exists() or locks.is_symlink():
        require_owned_directory(locks, "DEB lock root")
        for entry in locks.iterdir():
            if entry.name not in {"images", "terminal.lock"}:
                fail(f"DEB lock root contains an unrecognized entry: {entry}")
            if entry.name == "images":
                require_owned_directory(entry, "DEB image-build lock root")
                for image_lock in entry.iterdir():
                    if re.fullmatch(
                        r"(?:ubuntu-26\.04|debian-13)-[0-9a-f]{64}\.lock",
                        image_lock.name,
                    ) is None:
                        fail(
                            "DEB image-build lock root contains an unrecognized "
                            f"entry: {image_lock}"
                        )
                    info = require_cleanup_file(
                        image_lock,
                        "DEB image-build lock",
                    )
                    if stat.S_IMODE(info.st_mode) != 0o600:
                        fail(
                            "DEB image-build lock mode is not exactly 0600: "
                            f"{image_lock}"
                        )
                continue
            info = require_cleanup_file(entry, "DEB terminal-operation lock")
            if stat.S_IMODE(info.st_mode) != 0o600:
                fail(f"DEB terminal-operation lock mode is not exactly 0600: {entry}")

    sources = package_root / "sources"
    if sources.exists() or sources.is_symlink():
        require_owned_directory(sources, "DEB source-cache root")
        source_lock = sources / ".source-snapshot.lock"
        source_partial = sources / ".source-snapshot.partial"
        source_marker = sources / ".source-snapshot.partial.owner.json"
        allowed_hidden = {source_lock, source_partial, source_marker}
        for entry in sources.iterdir():
            if entry.name.startswith(".") and entry not in allowed_hidden:
                fail(f"DEB source-cache root contains an unrecognized entry: {entry}")
        if source_lock.exists() or source_lock.is_symlink():
            info = require_cleanup_file(source_lock, "DEB source-cache lock")
            if stat.S_IMODE(info.st_mode) != 0o600:
                fail(f"DEB source-cache lock mode is not exactly 0600: {source_lock}")
        if source_marker.exists() or source_marker.is_symlink():
            require_cleanup_file(source_marker, "DEB source-cache partial marker")
            blockers.append(f"deb-source-runtime:{source_marker}")
        if source_partial.exists() or source_partial.is_symlink():
            require_owned_directory(source_partial, "DEB source-cache partial")
            blockers.append(f"deb-source-runtime:{source_partial}")

        cache_name = re.compile(r"([0-9a-f]{40})-([0-9a-f]{64})")
        legacy_name = re.compile(
            r"[0-9a-f]{40}-(?:checkout|develop)\.(?:bundle|json)"
        )
        for cache in sorted(sources.iterdir(), key=lambda item: item.name):
            if cache in allowed_hidden:
                continue
            if legacy_name.fullmatch(cache.name):
                require_cleanup_file(cache, "retained legacy DEB source cache")
                continue
            match = cache_name.fullmatch(cache.name)
            if match is None:
                fail(f"DEB source-cache root contains an unowned entry: {cache}")
            require_owned_directory(cache, "DEB source cache")
            if stat.S_IMODE(cache.lstat().st_mode) != 0o700:
                fail(f"DEB source cache mode is not exactly 0700: {cache}")
            entries = {entry.name: entry for entry in cache.iterdir()}
            if set(entries) != {"source.bundle", "source.json"}:
                fail(f"DEB source cache has an unexpected entry set: {cache}")
            bundle = entries["source.bundle"]
            state_path = entries["source.json"]
            for entry in (bundle, state_path):
                info = require_cleanup_file(entry, "DEB source-cache file")
                if stat.S_IMODE(info.st_mode) != 0o600:
                    fail(f"DEB source-cache file mode is not exactly 0600: {entry}")
            state = load_cleanup_json(state_path, "DEB source-cache metadata")
            expected_keys = {
                "checkout_commit",
                "owner",
                "schema",
                "snapshot_sha256",
                "source_bundle",
                "source_commit",
                "source_ref",
                "source_ref_commit",
                "workflow_sha256",
            }
            checkout_commit, snapshot_sha256 = match.groups()
            identity = {
                key: state.get(key)
                for key in (
                    "checkout_commit",
                    "source_commit",
                    "source_ref",
                    "source_ref_commit",
                    "workflow_sha256",
                )
            }
            calculated_snapshot = sha256_bytes(
                json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
            )
            source_ref = state.get("source_ref")
            if (
                set(state) != expected_keys
                or state.get("owner") != "xpra-deb-checkout-source"
                or state.get("schema") != 1
                or state.get("checkout_commit") != checkout_commit
                or state.get("snapshot_sha256") != snapshot_sha256
                or calculated_snapshot != snapshot_sha256
                or state.get("source_bundle") != str(bundle)
                or not GIT_SHA_RE.fullmatch(str(state.get("source_commit", "")))
                or not GIT_SHA_RE.fullmatch(str(state.get("source_ref_commit", "")))
                or not SHA256_RE.fullmatch(str(state.get("workflow_sha256", "")))
                or not isinstance(source_ref, str)
                or not source_ref.startswith(("refs/heads/", "refs/remotes/"))
                or source_ref.rsplit("/", 1)[-1] != BASE_BRANCH
            ):
                fail(f"retained DEB source cache provenance is inconsistent: {cache}")

    selections = package_root / "selections"
    if not selections.exists() and not selections.is_symlink():
        return tuple(blockers)
    require_owned_directory(selections, "DEB selection-cache root")
    retained_lock = selections / ".selection-cache.lock"
    partial = selections / ".selection-cache.partial"
    marker = selections / ".selection-cache.partial.owner.json"
    allowed_hidden = {retained_lock, partial, marker}
    for entry in selections.iterdir():
        if entry.name.startswith(".") and entry not in allowed_hidden:
            fail(f"DEB selection-cache root contains an unrecognized entry: {entry}")
    if retained_lock.exists() or retained_lock.is_symlink():
        info = require_cleanup_file(retained_lock, "DEB selection-cache lock")
        if stat.S_IMODE(info.st_mode) != 0o600:
            fail(f"DEB selection-cache lock mode is not exactly 0600: {retained_lock}")
    if marker.exists() or marker.is_symlink():
        require_cleanup_file(marker, "DEB selection-cache partial marker")
        blockers.append(f"deb-selection-runtime:{marker}")
    if partial.exists() or partial.is_symlink():
        require_owned_directory(partial, "DEB selection-cache partial")
        blockers.append(f"deb-selection-runtime:{partial}")

    cache_name = re.compile(r"([0-9a-f]{64})-([0-9a-f]{64})")
    active_selection_sha256 = deb_selection_semantic_digest(AUTOMATION_ROOT)
    for cache in sorted(selections.iterdir(), key=lambda item: item.name):
        if cache in allowed_hidden:
            continue
        match = cache_name.fullmatch(cache.name)
        if match is None:
            fail(f"DEB selection-cache root contains an unowned entry: {cache}")
        require_owned_directory(cache, "DEB selection cache")
        if stat.S_IMODE(cache.lstat().st_mode) != 0o700:
            fail(f"DEB selection cache mode is not exactly 0700: {cache}")
        entries = {entry.name: entry for entry in cache.iterdir()}
        if set(entries) != {"lab", "selection.json"}:
            fail(f"DEB selection cache has an unexpected entry set: {cache}")
        state_path = entries["selection.json"]
        info = require_cleanup_file(state_path, "DEB selection-cache metadata")
        if stat.S_IMODE(info.st_mode) != 0o600:
            fail(f"DEB selection-cache metadata mode is not exactly 0600: {state_path}")
        state = load_cleanup_json(state_path, "DEB selection-cache metadata")
        expected_keys = {
            "owner",
            "schema",
            "selection",
            "selection_sha256",
            "snapshot_tree_sha256",
        }
        selection_sha256, cache_sha256 = match.groups()
        snapshot = entries["lab"]
        if (
            set(state) != expected_keys
            or state.get("owner") != DEB_SELECTION_OWNER
            or state.get("schema") != 1
            or state.get("selection") != f"stacks/{ACTIVE_STACK}"
            or state.get("selection_sha256") != selection_sha256
            or sha256_file(state_path) != cache_sha256
            or state.get("snapshot_tree_sha256")
            != deb_selection_tree_sha256(snapshot)
        ):
            fail(f"retained DEB selection cache provenance is inconsistent: {cache}")
        # Historical immutable snapshots may use manifest vocabulary which a
        # later resolver no longer accepts.  Their private tree and complete
        # content-addressed metadata remain mandatory above.  Replay semantic
        # validation only for a cache which could represent the active queue.
        if (
            selection_sha256 == active_selection_sha256
            and deb_selection_semantic_digest(snapshot) != selection_sha256
        ):
            fail(f"retained DEB selection cache provenance is inconsistent: {cache}")
    return tuple(blockers)


def validate_deb_status(
    status: dict[str, Any],
    package_root: Path,
    name: str,
    expected_output: Path,
) -> tuple[dict[str, str], str]:
    """Validate a finalized local DEB result against the current status schema."""
    required_status = {
        "arguments",
        "container",
        "exit_code",
        "finished_at",
        "log_sha256",
        "manifest",
        "name",
        "output",
        "output_sha256",
        "owner",
        "process_pid",
        "runner_sha256",
        "schema",
        "validation_error",
        "validation_ok",
    }
    missing_status = sorted(required_status.difference(status))
    if missing_status:
        fail(f"collected DEB result {name} is missing current-schema fields: {missing_status}")
    if set(status) != required_status:
        fail(f"collected DEB result is not the current owned schema: {name}")
    if (
        status["schema"] != 2
        or status["owner"] != DEB_PACKAGE_OWNER
        or status["name"] != name
        or not isinstance(status["validation_ok"], bool)
        or not isinstance(status["exit_code"], int)
        or isinstance(status["exit_code"], bool)
        or not isinstance(status["process_pid"], int)
        or isinstance(status["process_pid"], bool)
        or status["process_pid"] < 1
        or not isinstance(status["finished_at"], str)
        or not status["finished_at"]
        or not isinstance(status["validation_error"], str)
        or not isinstance(status["output"], str)
        or not isinstance(status["output_sha256"], str)
        or not isinstance(status["log_sha256"], str)
        or not SHA256_RE.fullmatch(status["log_sha256"])
        or not isinstance(status["runner_sha256"], str)
        or not SHA256_RE.fullmatch(status["runner_sha256"])
    ):
        fail(f"collected DEB result identity is inconsistent: {name}")

    arguments_value = status["arguments"]
    expected_argument_keys = {
        "build_id",
        "checkout_commit",
        "container_name",
        "container_state",
        "distro",
        "output",
        "output_partial",
        "selection",
        "selection_cache_sha256",
        "selection_sha256",
        "selection_snapshot",
        "selection_state",
        "source",
        "source_bundle",
        "source_ref",
        "source_ref_commit",
        "source_state",
        "workflow_sha256",
    }
    if (
        not isinstance(arguments_value, dict)
        or set(arguments_value) != expected_argument_keys
        or any(not isinstance(value, str) for value in arguments_value.values())
    ):
        fail(f"collected DEB arguments are not the current owned schema: {name}")
    arguments = {str(key): str(value) for key, value in arguments_value.items()}
    distro = arguments["distro"]
    run_root = package_root / "runs" / name
    source_root = package_root / "sources"
    source_bundle = Path(arguments["source_bundle"])
    source_state = Path(arguments["source_state"])
    selection_root = package_root / "selections"
    selection_cache = selection_root / (
        f"{arguments['selection_sha256']}-{arguments['selection_cache_sha256']}"
    )
    selection_snapshot = Path(arguments["selection_snapshot"])
    selection_state = Path(arguments["selection_state"])
    expected_partial = expected_output.with_name(f".{expected_output.name}.partial")
    if (
        distro not in {"ubuntu-26.04", "debian-13"}
        or arguments["selection"] != f"stacks/{ACTIVE_STACK}"
        or arguments["container_name"] != f"xpra-deb-{name}"
        or arguments["container_state"] != str(run_root / "container.json")
        or selection_snapshot != selection_cache / "lab"
        or selection_state != selection_cache / "selection.json"
        or not selection_snapshot.is_absolute()
        or not selection_state.is_absolute()
        or not selection_snapshot.is_relative_to(selection_root)
        or not selection_state.is_relative_to(selection_root)
        or ".." in selection_snapshot.parts
        or ".." in selection_state.parts
        or arguments["output"] != str(expected_output)
        or arguments["output_partial"] != str(expected_partial)
        or status["output"] != str(expected_output)
        or not UUID4_RE.fullmatch(arguments["build_id"])
        or not GIT_SHA_RE.fullmatch(arguments["checkout_commit"])
        or not GIT_SHA_RE.fullmatch(arguments["source"])
        or not GIT_SHA_RE.fullmatch(arguments["source_ref_commit"])
        or not SHA256_RE.fullmatch(arguments["selection_cache_sha256"])
        or not SHA256_RE.fullmatch(arguments["selection_sha256"])
        or not SHA256_RE.fullmatch(arguments["workflow_sha256"])
        or not arguments["source_ref"].startswith(("refs/heads/", "refs/remotes/"))
        or arguments["source_ref"].rsplit("/", 1)[-1] != BASE_BRANCH
        or not source_bundle.is_absolute()
        or not source_state.is_absolute()
        or not source_bundle.is_relative_to(source_root)
        or not source_state.is_relative_to(source_root)
        or source_bundle.parent != source_state.parent
        or re.fullmatch(
            rf"{arguments['checkout_commit']}-[0-9a-f]{{64}}",
            source_bundle.parent.name,
        )
        is None
        or ".." in source_bundle.parts
        or ".." in source_state.parts
        or source_bundle.name != "source.bundle"
        or source_state.name != "source.json"
    ):
        fail(f"collected DEB arguments are inconsistent: {name}")

    container = status["container"]
    manifest = status["manifest"]
    if not isinstance(container, dict) or not isinstance(manifest, dict):
        fail(f"collected DEB provenance is not an object: {name}")
    expected_container_keys = {
        "base_image_id",
        "builder_image_input_sha256",
        "container_id",
        "image_id",
    }
    immutable_container = {key: container.get(key) for key in expected_container_keys}
    if container and (
        set(container) != expected_container_keys
        or any(
            not isinstance(value, str) or not SHA256_RE.fullmatch(value)
            for value in immutable_container.values()
        )
    ):
        fail(f"DEB result has invalid container provenance: {name}")
    if status["validation_ok"]:
        if not container:
            fail(f"successful DEB result has invalid container provenance: {name}")
        expected_manifest = {
            "base_image_id": immutable_container["base_image_id"],
            "builder_image_id": immutable_container["image_id"],
            "builder_image_input_sha256": immutable_container[
                "builder_image_input_sha256"
            ],
            "checkout_commit": arguments["checkout_commit"],
            "distro": distro,
            "selection": arguments["selection"],
            "selection_cache_sha256": arguments["selection_cache_sha256"],
            "selection_sha256": arguments["selection_sha256"],
            "source_commit": arguments["source"],
            "source_ref": arguments["source_ref"],
            "source_ref_commit": arguments["source_ref_commit"],
            "workflow_sha256": arguments["workflow_sha256"],
        }
        expected_manifest_keys = {
            "architecture",
            "base_image_id",
            "base_version",
            "builder_image_id",
            "builder_image_input_sha256",
            "checkout_commit",
            "debian_version",
            "distro",
            "packages",
            "revision",
            "revision_first_parent_count",
            "schema",
            "selection",
            "selection_cache_sha256",
            "selection_resolution_sha256",
            "selection_sha256",
            "source_commit",
            "source_ref",
            "source_ref_commit",
            "workflow_sha256",
        }
        packages = manifest.get("packages")
        base_version = manifest.get("base_version")
        debian_version = manifest.get("debian_version")
        revision = manifest.get("revision")
        revision_count = manifest.get("revision_first_parent_count")
        if (
            status["exit_code"] != 0
            or status["validation_error"]
            or not SHA256_RE.fullmatch(status["output_sha256"])
            or set(manifest) != expected_manifest_keys
            or manifest.get("schema") != 2
            or manifest.get("architecture") != "amd64"
            or any(manifest.get(key) != value for key, value in expected_manifest.items())
            or not SHA256_RE.fullmatch(str(manifest.get("selection_resolution_sha256", "")))
            or not isinstance(base_version, str)
            or re.fullmatch(r"[0-9]+\.[0-9]+", base_version) is None
            or not isinstance(debian_version, str)
            or not debian_version.startswith(f"{base_version}-r{revision}-")
            or not isinstance(revision, int)
            or isinstance(revision, bool)
            or not isinstance(revision_count, int)
            or isinstance(revision_count, bool)
            or revision_count < 1
            or revision != revision_count + 5014
            or not isinstance(packages, list)
            or not packages
        ):
            fail(f"successful DEB result provenance is inconsistent: {name}")
        package_names: set[str] = set()
        for package in packages:
            if not isinstance(package, dict) or set(package) != {
                "architecture",
                "name",
                "package",
                "sha256",
                "size",
                "version",
            }:
                fail(f"successful DEB result has invalid package metadata: {name}")
            package_name = package.get("name")
            if (
                not isinstance(package_name, str)
                or not package_name.startswith("xpra")
                or not package_name.endswith(".deb")
                or Path(package_name).name != package_name
                or package_name in package_names
                or not isinstance(package.get("package"), str)
                or not package["package"].startswith("xpra")
                or package.get("version") != debian_version
                or package.get("architecture") not in {"all", "amd64"}
                or not SHA256_RE.fullmatch(str(package.get("sha256", "")))
                or not isinstance(package.get("size"), int)
                or isinstance(package.get("size"), bool)
                or package["size"] < 1
            ):
                fail(f"successful DEB result has invalid package metadata: {name}")
            package_names.add(package_name)
    elif status["output_sha256"] or manifest:
        fail(f"failed DEB result retained successful output provenance: {name}")
    return arguments, distro


def validate_deb_remove_transaction(
    marker: Path,
    package_root: Path,
    name: str,
    status_path: Path,
    log_path: Path,
    status: dict[str, Any],
    expected_output: Path,
) -> None:
    transaction = load_cleanup_json(marker, "DEB removal transaction")
    expected_keys = {
        "final_log",
        "final_status",
        "kind",
        "log_sha256",
        "name",
        "owner",
        "owner_record",
        "owner_sha256",
        "output",
        "output_sha256",
        "prelaunch_sha256",
        "run_device",
        "run_directory",
        "run_inode",
        "schema",
        "status",
        "status_sha256",
        "validation_ok",
    }
    run_directory = package_root / "runs" / name
    identity = {
        "final_log": str(log_path),
        "final_status": str(status_path),
        "kind": "deb-build-remove",
        "name": name,
        "output": str(expected_output),
        "owner": DEB_PACKAGE_OWNER,
        "run_directory": str(run_directory),
        "schema": 1,
    }
    owner_record = transaction.get("owner_record")
    embedded_status = transaction.get("status")
    if (
        set(transaction) != expected_keys
        or any(transaction.get(key) != value for key, value in identity.items())
        or embedded_status != status
        or transaction.get("validation_ok") is not status["validation_ok"]
        or transaction.get("output_sha256") != status["output_sha256"]
        or transaction.get("log_sha256") != sha256_file(log_path)
        or transaction.get("status_sha256") != sha256_file(status_path)
        or not isinstance(owner_record, dict)
    ):
        fail(f"DEB removal transaction identity is inconsistent: {name}")
    for key in (
        "log_sha256",
        "owner_sha256",
        "prelaunch_sha256",
        "status_sha256",
    ):
        if not SHA256_RE.fullmatch(str(transaction.get(key, ""))):
            fail(f"DEB removal transaction has an invalid {key}: {name}")
    if (
        transaction["status_sha256"] != sha256_bytes(canonical_json_bytes(status))
        or transaction["owner_sha256"]
        != sha256_bytes(canonical_json_bytes(owner_record))
    ):
        fail(f"DEB removal transaction digest is inconsistent: {name}")
    run_device = transaction.get("run_device")
    run_inode = transaction.get("run_inode")
    if (
        not isinstance(run_device, int)
        or isinstance(run_device, bool)
        or run_device < 0
        or not isinstance(run_inode, int)
        or isinstance(run_inode, bool)
        or run_inode < 1
    ):
        fail(f"DEB removal transaction runtime identity is invalid: {name}")

    expected_record_keys = {
        "arguments",
        "kind",
        "name",
        "owner",
        "process",
        "runner_sha256",
        "schema",
    }
    arguments = status["arguments"]
    process = owner_record.get("process")
    if (
        set(owner_record) != expected_record_keys
        or owner_record.get("arguments") != arguments
        or owner_record.get("kind") != "deb-build"
        or owner_record.get("name") != name
        or owner_record.get("owner") != DEB_PACKAGE_OWNER
        or owner_record.get("runner_sha256") != status["runner_sha256"]
        or owner_record.get("schema") != 2
        or not isinstance(process, dict)
    ):
        fail(f"DEB removal transaction owner provenance differs: {name}")
    expected_process_keys = {
        "completion",
        "owner_token",
        "pid",
        "process_group",
        "runtime_log",
        "start_ticks",
        "supervisor_sha256",
    }
    pid = process.get("pid")
    if (
        set(process) != expected_process_keys
        or process.get("completion") != str(run_directory / "completion.json")
        or process.get("runtime_log") != str(run_directory / "runtime.log")
        or pid != status["process_pid"]
        or process.get("process_group") != pid
        or not isinstance(process.get("start_ticks"), str)
        or not str(process["start_ticks"]).isdigit()
        or not SHA256_RE.fullmatch(str(process.get("owner_token", "")))
        or not SHA256_RE.fullmatch(str(process.get("supervisor_sha256", "")))
    ):
        fail(f"DEB removal transaction process provenance differs: {name}")
    prelaunch = {
        "arguments": arguments,
        "kind": "deb-build-prelaunch",
        "name": name,
        "owner": DEB_PACKAGE_OWNER,
        "runner_sha256": status["runner_sha256"],
        "schema": 1,
    }
    if transaction["prelaunch_sha256"] != sha256_bytes(
        canonical_json_bytes(prelaunch)
    ):
        fail(f"DEB removal transaction prelaunch provenance differs: {name}")


def deb_result_targets(root: Path, cycle: str) -> tuple[list[CleanupTarget], set[str]]:
    package_root = root / "deb-packages"
    results = package_root / "results"
    outputs = package_root / "outputs"
    if not results.exists() and not outputs.exists():
        return [], set()
    if results.exists():
        require_owned_directory(results, "DEB result root")
    if outputs.exists():
        require_owned_directory(outputs, "DEB output root")
    names: set[str] = set()
    if results.exists():
        for path in results.iterdir():
            name = path.name
            for suffix in (".status.json", ".remove.json", ".log"):
                if name.endswith(suffix):
                    name = name[: -len(suffix)]
                    if cycle_matches(name, cycle):
                        names.add(name)
                    break
            else:
                if not cycle_matches(path.name.lstrip("."), cycle):
                    continue
                fail(f"unrecognized cycle artifact in DEB results: {path}")
    targets: list[CleanupTarget] = []
    for name in sorted(names):
        status_path = results / f"{name}.status.json"
        remove_path = results / f"{name}.remove.json"
        log_path = results / f"{name}.log"
        status = load_cleanup_json(status_path, "collected DEB status")
        require_cleanup_file(log_path, "collected DEB log")
        if status.get("log_sha256") != sha256_file(log_path):
            fail(f"collected DEB log digest does not match: {name}")
        arguments_value = status.get("arguments")
        distro_value = arguments_value.get("distro") if isinstance(arguments_value, dict) else ""
        if distro_value not in {"ubuntu-26.04", "debian-13"}:
            fail(f"collected DEB distribution is invalid: {name}")
        distro = str(distro_value)
        expected_output = outputs / f"{name}-{distro}-debs.tar"
        _arguments, _distro = validate_deb_status(
            status,
            package_root,
            name,
            expected_output,
        )
        validate_deb_remove_transaction(
            remove_path,
            package_root,
            name,
            status_path,
            log_path,
            status,
            expected_output,
        )
        output = expected_output
        targets.extend(
            (
                CleanupTarget("deb-result", status_path, sha256_file(status_path)),
                CleanupTarget("deb-result", remove_path, sha256_file(remove_path)),
                CleanupTarget("deb-result", log_path, sha256_file(log_path)),
            )
        )
        if status["validation_ok"]:
            manifest = status.get("manifest")
            if not isinstance(manifest, dict) or manifest.get("distro") != distro:
                fail(f"collected DEB manifest distribution is inconsistent: {name}")
            require_cleanup_file(output, "collected DEB output")
            output_sha256 = status.get("output_sha256")
            if (
                not isinstance(output_sha256, str)
                or not SHA256_RE.fullmatch(output_sha256)
                or sha256_file(output) != output_sha256
            ):
                fail(f"collected DEB output digest does not match: {name}")
            targets.append(CleanupTarget("deb-result", output, output_sha256))
        elif output.exists() or output.is_symlink():
            fail(f"failed DEB result retained an untrusted output: {name}")
    output_entries = outputs.iterdir() if outputs.exists() else ()
    for output in output_entries:
        if cycle_matches(output.name.lstrip("."), cycle) and not any(
            target.path == output for target in targets
        ):
            fail(f"DEB output has no finalized result status: {output}")
    return targets, names


def runtime_cycle_blockers(cycle: str) -> tuple[str, ...]:
    blockers: list[str] = []
    for owner in (UPSTREAM_TEST_OWNER, "live", DEB_PACKAGE_OWNER):
        listed = run(
            (
                "podman",
                "ps",
                "--all",
                "--quiet",
                "--filter",
                f"label=io.xpra.fork-maintenance.owner={owner}",
            ),
            check=False,
        )
        if listed.returncode:
            fail("cannot inspect Podman containers before cycle cleanup")
        for object_id in listed.stdout.splitlines():
            inspected = run(("podman", "inspect", object_id), check=False)
            if inspected.returncode:
                fail(f"cannot inspect Podman container before cleanup: {object_id}")
            try:
                payload = json.loads(inspected.stdout)
                item = payload[0]
                labels = item["Config"]["Labels"] or {}
                container_name = str(item["Name"]).lstrip("/")
            except (IndexError, KeyError, TypeError, json.JSONDecodeError) as error:
                fail(f"invalid Podman container inspection for {object_id}: {error}")
            run_name = str(labels.get("io.xpra.fork-maintenance.run-id", ""))
            if owner == "live":
                identity = run_name
            elif owner == DEB_PACKAGE_OWNER:
                identity = str(labels.get("io.xpra.fork-maintenance.run-name", ""))
            else:
                identity = container_name
            if cycle_matches(identity, cycle):
                blockers.append(f"podman-container:{object_id}")

    networks = run(
        (
            "podman",
            "network",
            "ls",
            "--quiet",
            "--filter",
            "label=io.xpra.fork-maintenance.owner=live",
        ),
        check=False,
    )
    if networks.returncode:
        fail("cannot inspect Podman networks before cycle cleanup")
    for object_id in networks.stdout.splitlines():
        inspected = run(("podman", "network", "inspect", object_id), check=False)
        if inspected.returncode:
            fail(f"cannot inspect Podman network before cleanup: {object_id}")
        try:
            payload = json.loads(inspected.stdout)
            item = payload[0]
            labels = item.get("labels", item.get("Labels", {})) or {}
        except (IndexError, TypeError, json.JSONDecodeError) as error:
            fail(f"invalid Podman network inspection for {object_id}: {error}")
        if cycle_matches(str(labels.get("io.xpra.fork-maintenance.run-id", "")), cycle):
            blockers.append(f"podman-network:{object_id}")
    return tuple(sorted(blockers))


def cleanup_plan_payload(repo: Path, plan: CleanupPlan) -> dict[str, Any]:
    root = cleanup_state_root(repo)
    return {
        "schema": 1,
        "owner": CYCLE_CLEAN_OWNER,
        "cycle": plan.cycle,
        "targets": [
            {
                "kind": target.kind,
                "path": target.path.relative_to(root).as_posix(),
                "fingerprint": target.fingerprint,
            }
            for target in plan.targets
        ],
        "retained": [
            "build-contexts/",
            "cycle-cleanups/",
            "source-archives/",
            "upstream-tests/.foreground-payload.lock",
            "upstream-tests/image-builds/.image-cache.lock",
            "upstream-tests/logs/.lifecycle.lock",
            "upstream-tests/sources/",
            "deb-packages/locks/terminal.lock",
            "deb-packages/locks/images/",
            "deb-packages/selections/",
            "deb-packages/sources/",
            "jobs/live/.lifecycle.lock",
            "venvs/.environment.lock",
            "venvs/",
            "tooling-venv/",
            "Podman input-keyed, label-verified images",
            "Podman ccache volume",
        ],
    }


def cleanup_plan_digest(repo: Path, plan: CleanupPlan) -> str:
    payload = cleanup_plan_payload(repo, plan)
    return sha256_bytes(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    )


def cycle_cleanup_transaction_root(repo: Path, *, create: bool = False) -> Path:
    root = cleanup_state_root(repo)
    transactions = root / "cycle-cleanups"
    if create:
        return prepare_cleanup_directory(
            root,
            transactions,
            "cycle cleanup transaction root",
        )
    return transactions


def cleanup_plan_from_payload(repo: Path, payload: object) -> CleanupPlan:
    if not isinstance(payload, dict) or set(payload) != {
        "cycle",
        "owner",
        "retained",
        "schema",
        "targets",
    }:
        fail("cycle cleanup transaction has an invalid plan schema")
    cycle = payload.get("cycle")
    raw_targets = payload.get("targets")
    if not isinstance(cycle, str):
        fail("cycle cleanup transaction has an invalid cycle")
    require_cycle_name(cycle)
    if not isinstance(raw_targets, list) or not raw_targets:
        fail("cycle cleanup transaction has no exact targets")
    root = cleanup_state_root(repo)
    targets: list[CleanupTarget] = []
    seen: set[Path] = set()
    allowed_kinds = {
        "artifact-file",
        "artifact-tree",
        "deb-result",
        "live-result",
        "live-result-tree",
        "upstream-result",
    }
    for raw in raw_targets:
        if not isinstance(raw, dict) or set(raw) != {"fingerprint", "kind", "path"}:
            fail("cycle cleanup transaction has an invalid target entry")
        kind = raw.get("kind")
        relative_value = raw.get("path")
        fingerprint = raw.get("fingerprint")
        if (
            kind not in allowed_kinds
            or not isinstance(relative_value, str)
            or not SHA256_RE.fullmatch(str(fingerprint or ""))
        ):
            fail("cycle cleanup transaction target identity is invalid")
        relative = Path(relative_value)
        if (
            not relative_value
            or relative.is_absolute()
            or relative.as_posix() != relative_value
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            fail("cycle cleanup transaction target path is not normalized")
        target = root / relative
        if target in seen:
            fail(f"cycle cleanup transaction repeats a target: {target}")
        seen.add(target)
        targets.append(CleanupTarget(str(kind), target, str(fingerprint)))
    if targets != sorted(targets, key=lambda item: (item.path.as_posix(), item.kind)):
        fail("cycle cleanup transaction targets are not in canonical order")
    provisional = CleanupPlan(cycle, tuple(targets), "")
    if payload != cleanup_plan_payload(repo, provisional):
        fail("cycle cleanup transaction plan differs from the current schema")
    return CleanupPlan(cycle, tuple(targets), cleanup_plan_digest(repo, provisional))


def validate_cleanup_directory_state(
    path: Path,
    state: CleanupDirectoryState,
    *,
    artifact: bool = False,
) -> None:
    require_owned_directory(path, "cycle cleanup directory staging", private=not artifact)
    details = path.lstat()
    if not artifact and stat.S_IMODE(details.st_mode) != 0o700:
        fail(f"cycle cleanup directory mode is not exactly 0700: {path}")
    if (
        details.st_dev != state.device
        or details.st_ino != state.inode
        or (artifact_fingerprint(path) if artifact else secure_tree_fingerprint(path))
        != state.fingerprint
    ):
        fail(f"cycle cleanup directory changed after transaction publication: {path}")


def cleanup_directory_state(index: int, target: CleanupTarget) -> CleanupDirectoryState:
    artifact = target.kind == "artifact-tree"
    require_owned_directory(target.path, "cycle cleanup directory target", private=not artifact)
    details = target.path.lstat()
    if not artifact and stat.S_IMODE(details.st_mode) != 0o700:
        fail(f"cycle cleanup directory mode is not exactly 0700: {target.path}")
    return CleanupDirectoryState(
        index,
        details.st_dev,
        details.st_ino,
        artifact_fingerprint(target.path) if artifact else secure_tree_fingerprint(target.path),
    )


def cleanup_directory_phase_path(marker: Path, cycle: str, index: int) -> Path:
    return marker.parent / f".{cycle}.{index}.rmtree.json"


def cleanup_directory_phase_payload(
    transaction: CleanupTransaction,
    state: CleanupDirectoryState,
) -> dict[str, Any]:
    staging = transaction.marker.parent / (
        f".{transaction.plan.cycle}.{state.index}.remove"
    )
    return {
        "cycle": transaction.plan.cycle,
        "device": state.device,
        "fingerprint": state.fingerprint,
        "index": state.index,
        "inode": state.inode,
        "kind": "cycle-clean-rmtree-started",
        "owner": CYCLE_CLEAN_OWNER,
        "schema": 1,
        "staging": str(staging),
        "transaction": str(transaction.marker),
        "transaction_sha256": sha256_file(transaction.marker),
    }


def validate_cleanup_directory_phase(
    transaction: CleanupTransaction,
    state: CleanupDirectoryState,
) -> Path:
    phase = cleanup_directory_phase_path(
        transaction.marker,
        transaction.plan.cycle,
        state.index,
    )
    payload = load_cleanup_json(phase, "cycle cleanup rmtree phase")
    if payload != cleanup_directory_phase_payload(transaction, state):
        fail(f"cycle cleanup rmtree phase is inconsistent: {phase}")
    return phase


def publish_cleanup_directory_phase(
    transaction: CleanupTransaction,
    state: CleanupDirectoryState,
) -> Path:
    phase = cleanup_directory_phase_path(
        transaction.marker,
        transaction.plan.cycle,
        state.index,
    )
    if phase.exists() or phase.is_symlink():
        fail(f"cycle cleanup rmtree phase already exists: {phase}")
    publish_private_json(
        phase,
        cleanup_directory_phase_payload(transaction, state),
        "cycle cleanup rmtree phase",
    )
    return validate_cleanup_directory_phase(transaction, state)


def load_pending_cleanup_transaction(repo: Path) -> CleanupTransaction | None:
    transaction_root = cycle_cleanup_transaction_root(repo)
    if not transaction_root.exists() and not transaction_root.is_symlink():
        return None
    require_owned_directory(transaction_root, "cycle cleanup transaction root")
    if stat.S_IMODE(transaction_root.lstat().st_mode) != 0o700:
        fail("cycle cleanup transaction root mode is not exactly 0700")
    entries = tuple(sorted(transaction_root.iterdir()))
    if not entries:
        return None
    markers = tuple(
        path
        for path in entries
        if re.fullmatch(
            r"[a-z0-9]+(?:-[a-z0-9]+)*\.remove\.json",
            path.name,
        )
    )
    if len(markers) != 1:
        fail(f"cycle cleanup transaction root has unexpected state: {entries}")
    marker = markers[0]
    marker_info = require_cleanup_file(marker, "cycle cleanup transaction")
    if stat.S_IMODE(marker_info.st_mode) != 0o600:
        fail(f"cycle cleanup transaction mode is not exactly 0600: {marker}")
    record = load_cleanup_json(marker, "cycle cleanup transaction")
    if set(record) != {
        "directories",
        "kind",
        "operation_id",
        "owner",
        "plan",
        "plan_sha256",
        "policy",
        "repository",
        "schema",
    }:
        fail(f"cycle cleanup transaction has an unexpected schema: {marker}")
    operation_id = record.get("operation_id")
    if (
        record.get("kind") != "cycle-clean-remove"
        or not isinstance(operation_id, str)
        or not UUID4_RE.fullmatch(operation_id)
        or record.get("owner") != CYCLE_CLEAN_OWNER
        or record.get("policy") != "complete"
        or record.get("repository") != str(repo.resolve())
        or record.get("schema") != 2
    ):
        fail(f"cycle cleanup transaction identity is inconsistent: {marker}")
    plan = cleanup_plan_from_payload(repo, record.get("plan"))
    if (
        marker != transaction_root / f"{plan.cycle}.remove.json"
        or record.get("plan_sha256") != plan.digest
    ):
        fail(f"cycle cleanup transaction plan digest is inconsistent: {marker}")
    raw_directories = record.get("directories")
    expected_indices = tuple(
        index
        for index, target in enumerate(plan.targets)
        if target.kind in CLEANUP_DIRECTORY_KINDS
    )
    if not isinstance(raw_directories, list) or len(raw_directories) != len(
        expected_indices
    ):
        fail(f"cycle cleanup transaction has an invalid directory state: {marker}")
    directories: list[CleanupDirectoryState] = []
    for raw, expected_index in zip(raw_directories, expected_indices, strict=True):
        if not isinstance(raw, dict) or set(raw) != {
            "device",
            "fingerprint",
            "index",
            "inode",
        }:
            fail(f"cycle cleanup transaction directory state is invalid: {marker}")
        device = raw.get("device")
        inode = raw.get("inode")
        fingerprint = raw.get("fingerprint")
        if (
            raw.get("index") != expected_index
            or not isinstance(device, int)
            or isinstance(device, bool)
            or device < 0
            or not isinstance(inode, int)
            or isinstance(inode, bool)
            or inode <= 0
            or not SHA256_RE.fullmatch(str(fingerprint or ""))
        ):
            fail(f"cycle cleanup transaction directory identity is invalid: {marker}")
        directories.append(
            CleanupDirectoryState(
                expected_index,
                device,
                inode,
                str(fingerprint),
            )
        )
    by_index = {state.index: state for state in directories}
    staging = {
        transaction_root / f".{plan.cycle}.{index}.remove"
        for index, target in enumerate(plan.targets)
        if target.kind in CLEANUP_DIRECTORY_KINDS
    }
    phases = {
        cleanup_directory_phase_path(marker, plan.cycle, index)
        for index, target in enumerate(plan.targets)
        if target.kind in CLEANUP_DIRECTORY_KINDS
    }
    unexpected = set(entries).difference({marker}, staging, phases)
    if unexpected:
        fail(f"cycle cleanup transaction root has unexpected state: {sorted(unexpected)}")
    transaction = CleanupTransaction(plan, marker, tuple(directories))
    for index, target in enumerate(plan.targets):
        if target.kind not in CLEANUP_DIRECTORY_KINDS:
            continue
        partial = transaction_root / f".{plan.cycle}.{index}.remove"
        phase = cleanup_directory_phase_path(marker, plan.cycle, index)
        phase_present = phase.exists() or phase.is_symlink()
        if phase_present:
            validate_cleanup_directory_phase(transaction, by_index[index])
            if target.path.exists() or target.path.is_symlink():
                fail(f"cycle cleanup rmtree phase still has its target: {target.path}")
        if not partial.exists() and not partial.is_symlink():
            continue
        if target.path.exists() or target.path.is_symlink():
            fail(f"cycle cleanup has both target and removal staging: {target.path}")
        if phase_present:
            require_owned_directory(
                partial, "cycle cleanup directory staging", private=target.kind != "artifact-tree"
            )
            details = partial.lstat()
            state = by_index[index]
            if details.st_dev != state.device or details.st_ino != state.inode:
                fail(f"cycle cleanup directory identity changed: {partial}")
        else:
            validate_cleanup_directory_state(
                partial, by_index[index], artifact=target.kind == "artifact-tree"
            )
    for index, target in enumerate(plan.targets):
        if target.kind not in CLEANUP_DIRECTORY_KINDS:
            continue
        if target.path.exists() or target.path.is_symlink():
            validate_cleanup_directory_state(
                target.path, by_index[index], artifact=target.kind == "artifact-tree"
            )
    return transaction


def publish_cleanup_transaction(repo: Path, plan: CleanupPlan) -> Path:
    if load_pending_cleanup_transaction(repo) is not None:
        fail("a cycle cleanup transaction is already pending")
    transaction_root = cycle_cleanup_transaction_root(repo, create=True)
    marker = transaction_root / f"{plan.cycle}.remove.json"
    directories = tuple(
        cleanup_directory_state(index, target)
        for index, target in enumerate(plan.targets)
        if target.kind in CLEANUP_DIRECTORY_KINDS
    )
    publish_private_json(
        marker,
        {
            "directories": [
                {
                    "device": state.device,
                    "fingerprint": state.fingerprint,
                    "index": state.index,
                    "inode": state.inode,
                }
                for state in directories
            ],
            "kind": "cycle-clean-remove",
            "operation_id": str(uuid.uuid4()),
            "owner": CYCLE_CLEAN_OWNER,
            "plan": cleanup_plan_payload(repo, plan),
            "plan_sha256": plan.digest,
            "policy": "complete",
            "repository": str(repo.resolve()),
            "schema": 2,
        },
        "cycle cleanup transaction",
    )
    loaded = load_pending_cleanup_transaction(repo)
    if loaded != CleanupTransaction(plan, marker, directories):
        fail("published cycle cleanup transaction did not validate")
    return marker


def resumed_cleanup_runtime_blockers(
    repo: Path,
    cycle: str,
    *,
    inspect_runtime: bool,
) -> tuple[str, ...]:
    """Recheck runtime-only state when finalized evidence is partly absent."""
    root = cleanup_state_root(repo)
    blockers: list[str] = []
    upstream_root = root / "upstream-tests"
    for path in (
        upstream_root / ".foreground-payload",
        upstream_root / ".foreground-payload.owner.json",
    ):
        if path.exists() or path.is_symlink():
            blockers.append(f"upstream-foreground-runtime:{path}")
    runs = upstream_root / "runs"
    if runs.exists() or runs.is_symlink():
        require_owned_directory(runs, "upstream-test run root")
        for path in runs.iterdir():
            name = path.name.removesuffix(".owner")
            if cycle_matches(name.lstrip("."), cycle):
                blockers.append(f"upstream-runtime:{path}")
    sources = upstream_root / "sources"
    if sources.exists() or sources.is_symlink():
        require_owned_directory(sources, "upstream source-bundle root")
        blockers.extend(
            f"upstream-source-runtime:{path}"
            for path in sources.iterdir()
            if path.name.endswith(".bundle.partial")
        )
    image_builds = upstream_root / "image-builds"
    if image_builds.exists() or image_builds.is_symlink():
        require_owned_directory(image_builds, "upstream image-build root")
        image_lock = image_builds / ".image-cache.lock"
        for path in image_builds.iterdir():
            if path != image_lock and cycle_matches(path.name.lstrip("."), cycle):
                blockers.append(f"upstream-image-runtime:{path}")

    live_jobs = root / "jobs" / "live"
    if live_jobs.exists() or live_jobs.is_symlink():
        require_owned_directory(live_jobs, "live-job record root")
        live_lock = live_jobs / ".lifecycle.lock"
        runtime_suffixes = (
            ".freeze-abort.json",
            ".freeze-prelaunch.json",
            ".freeze.completion.json",
            ".freeze-result.json",
            ".freeze.runtime",
            ".freeze.json",
            ".owner.json",
            ".completion.json",
            ".runtime",
        )
        for path in live_jobs.iterdir():
            if path == live_lock:
                continue
            for suffix in runtime_suffixes:
                if path.name.endswith(suffix):
                    name = path.name[: -len(suffix)]
                    if cycle_matches(name.lstrip("."), cycle):
                        if suffix == ".freeze-abort.json":
                            validate_live_freeze_abort_transaction(root, path, name)
                        blockers.append(f"live-runtime:{path}")
                    break
            else:
                if path.name.startswith(".") and cycle_matches(
                    path.name.lstrip("."), cycle
                ):
                    blockers.append(f"live-runtime:{path}")
    live_results = root / "live-results"
    if live_results.exists() or live_results.is_symlink():
        require_owned_directory(live_results, "live-result root")
        blockers.extend(
            f"live-runtime:{path}"
            for path in live_results.iterdir()
            if path.name.startswith(".")
            and cycle_matches(path.name.lstrip("."), cycle)
        )
    live_venvs = root / "venvs"
    for path in (
        live_venvs / ".environment.partial",
        live_venvs / ".environment.partial.owner.json",
    ):
        if path.exists() or path.is_symlink():
            blockers.append(f"live-environment-runtime:{path}")

    package_root = root / "deb-packages"
    if package_root.exists() or package_root.is_symlink():
        require_owned_directory(package_root, "DEB state root")
        blockers.extend(validate_deb_retained_state(package_root))
    deb_runs = package_root / "runs"
    if deb_runs.exists() or deb_runs.is_symlink():
        require_owned_directory(deb_runs, "DEB runtime root")
        blockers.extend(
            f"deb-runtime:{path}"
            for path in deb_runs.iterdir()
            if cycle_matches(path.name.lstrip("."), cycle)
        )

    if inspect_runtime:
        blockers.extend(runtime_cycle_blockers(cycle))
    return tuple(sorted(set(blockers)))


def validate_cleanup_host(repo: Path) -> None:
    verify_repo(repo, ())
    artifact_boundary_check(repo)
    root = cleanup_state_root(repo)
    require_owned_directory(repo, "repository root", private=False)
    require_owned_directory(repo / ".artifacts", "artifact root")
    require_owned_directory(root, "fork-maintenance artifact root")


def _build_cleanup_plan_unlocked(
    repo: Path,
    cycle: str,
    *,
    inspect_runtime: bool = True,
) -> CleanupPlan:
    require_cycle_name(cycle)
    validate_cleanup_host(repo)
    root = cleanup_state_root(repo)

    blockers: list[str] = []
    upstream_root = root / "upstream-tests"
    if upstream_root.exists():
        require_owned_directory(upstream_root, "upstream-test state root")
        foreground_lock = upstream_root / ".foreground-payload.lock"
        if foreground_lock.exists() or foreground_lock.is_symlink():
            info = require_cleanup_file(
                foreground_lock,
                "upstream foreground-payload lock",
            )
            if stat.S_IMODE(info.st_mode) != 0o600:
                fail(f"invalid retained upstream foreground-payload lock: {foreground_lock}")
        foreground_payload = upstream_root / ".foreground-payload"
        foreground_marker = upstream_root / ".foreground-payload.owner.json"
        if foreground_payload.exists() or foreground_payload.is_symlink():
            require_owned_directory(
                foreground_payload,
                "upstream foreground-payload partial",
            )
            blockers.append(f"upstream-foreground-runtime:{foreground_payload}")
        if foreground_marker.exists() or foreground_marker.is_symlink():
            require_cleanup_file(
                foreground_marker,
                "upstream foreground-payload owner",
            )
            blockers.append(f"upstream-foreground-runtime:{foreground_marker}")
        upstream_lock = upstream_root / "logs" / ".lifecycle.lock"
        if upstream_lock.exists() or upstream_lock.is_symlink():
            info = require_cleanup_file(upstream_lock, "upstream lifecycle lock")
            if stat.S_IMODE(info.st_mode) != 0o600:
                fail(f"invalid retained upstream lifecycle lock: {upstream_lock}")
        runs = upstream_root / "runs"
        if runs.exists():
            require_owned_directory(runs, "upstream-test run root")
            for path in runs.iterdir():
                name = path.name.removesuffix(".owner")
                if cycle_matches(name.lstrip("."), cycle):
                    blockers.append(f"upstream-runtime:{path}")
        sources = upstream_root / "sources"
        if sources.exists():
            require_owned_directory(sources, "upstream source-bundle root")
            for path in sources.iterdir():
                if path.name.endswith(".bundle.partial"):
                    blockers.append(f"upstream-source-runtime:{path}")
                elif path.name.endswith(".bundle.lock"):
                    info = require_cleanup_file(path, "upstream source-bundle lock")
                    if (
                        re.fullmatch(
                            r"[0-9a-f]{40}-(?:local|origin|upstream)\.bundle\.lock",
                            path.name,
                        )
                        is None
                        or stat.S_IMODE(info.st_mode) != 0o600
                    ):
                        fail(f"invalid retained upstream source-bundle lock: {path}")
        image_builds = upstream_root / "image-builds"
        if image_builds.exists():
            require_owned_directory(image_builds, "upstream image-build root")
            image_cache_lock = image_builds / ".image-cache.lock"
            if image_cache_lock.exists() or image_cache_lock.is_symlink():
                info = require_cleanup_file(image_cache_lock, "upstream image-cache lock")
                if stat.S_IMODE(info.st_mode) != 0o600:
                    fail(f"invalid retained upstream image-cache lock: {image_cache_lock}")
            for path in image_builds.iterdir():
                if path == image_cache_lock:
                    continue
                if cycle_matches(path.name.lstrip("."), cycle):
                    blockers.append(f"upstream-image-runtime:{path}")

    live_jobs = root / "jobs" / "live"
    if live_jobs.exists():
        require_owned_directory(live_jobs, "live-job record root")
        live_lock = live_jobs / ".lifecycle.lock"
        if live_lock.exists() or live_lock.is_symlink():
            info = require_cleanup_file(live_lock, "live lifecycle lock")
            if stat.S_IMODE(info.st_mode) != 0o600:
                fail(f"invalid retained live lifecycle lock: {live_lock}")
        for path in live_jobs.iterdir():
            if path == live_lock:
                continue
            name = path.name
            runtime_suffix = ""
            for suffix in (
                ".freeze-abort.json",
                ".freeze-prelaunch.json",
                ".freeze.completion.json",
                ".freeze-result.json",
                ".freeze.runtime",
                ".freeze.json",
                ".owner.json",
                ".completion.json",
                ".status.json",
                ".runtime",
                ".log",
            ):
                if name.endswith(suffix):
                    name = name[: -len(suffix)]
                    runtime_suffix = suffix
                    break
            owned = runtime_suffix in {
                ".freeze-abort.json",
                ".freeze-prelaunch.json",
                ".freeze.completion.json",
                ".freeze-result.json",
                ".freeze.runtime",
                ".freeze.json",
                ".owner.json",
                ".completion.json",
                ".runtime",
            } and cycle_matches(name, cycle)
            temporary = path.name.startswith(".") and cycle_matches(
                name.lstrip("."), cycle
            )
            if owned or temporary:
                if runtime_suffix == ".freeze-abort.json" and cycle_matches(name, cycle):
                    validate_live_freeze_abort_transaction(root, path, name)
                blockers.append(f"live-runtime:{path}")

    live_venvs = root / "venvs"
    if live_venvs.exists():
        require_owned_directory(live_venvs, "live environment root")
        environment_lock = live_venvs / ".environment.lock"
        if environment_lock.exists() or environment_lock.is_symlink():
            info = require_cleanup_file(environment_lock, "live environment lock")
            if stat.S_IMODE(info.st_mode) != 0o600:
                fail(f"invalid retained live environment lock: {environment_lock}")
        environment_partial = live_venvs / ".environment.partial"
        environment_marker = live_venvs / ".environment.partial.owner.json"
        if environment_partial.exists() or environment_partial.is_symlink():
            require_owned_directory(environment_partial, "live environment partial")
            blockers.append(f"live-environment-runtime:{environment_partial}")
        if environment_marker.exists() or environment_marker.is_symlink():
            require_cleanup_file(environment_marker, "live environment partial owner")
            blockers.append(f"live-environment-runtime:{environment_marker}")

    live_results = root / "live-results"
    if live_results.exists():
        require_owned_directory(live_results, "live-result root")
        for path in live_results.iterdir():
            if path.name.startswith(".") and cycle_matches(
                path.name.lstrip("."), cycle
            ):
                blockers.append(f"live-runtime:{path}")

    package_root = root / "deb-packages"
    if package_root.exists() or package_root.is_symlink():
        require_owned_directory(package_root, "DEB state root")
        blockers.extend(validate_deb_retained_state(package_root))
    deb_runs = package_root / "runs"
    if deb_runs.exists():
        require_owned_directory(deb_runs, "DEB runtime root")
        for path in deb_runs.iterdir():
            if cycle_matches(path.name.lstrip("."), cycle):
                blockers.append(f"deb-runtime:{path}")

    if inspect_runtime:
        blockers.extend(runtime_cycle_blockers(cycle))
    if blockers:
        fail(
            "cycle still has active, uncollected, or unremoved runtime state:\n"
            + "\n".join(f"  {item}" for item in sorted(set(blockers)))
        )

    targets: list[CleanupTarget] = []
    upstream_targets, _upstream_names = upstream_result_targets(root, cycle)
    targets.extend(upstream_targets)
    live_targets, _live_names = live_result_targets(root, cycle)
    targets.extend(live_targets)
    deb_targets, _deb_names = deb_result_targets(root, cycle)
    targets.extend(deb_targets)

    if not targets:
        fail(f"no finalized artifacts match cycle {cycle!r}")
    targets.sort(key=lambda target: (target.path.as_posix(), target.kind))
    provisional = CleanupPlan(cycle, tuple(targets), "")
    return CleanupPlan(cycle, tuple(targets), cleanup_plan_digest(repo, provisional))


def build_cleanup_plan(
    repo: Path,
    cycle: str,
    *,
    inspect_runtime: bool = True,
) -> CleanupPlan:
    """Build or resume one plan while excluding every lifecycle publication."""
    require_cycle_name(cycle)
    require_cycle_plan(CleanupPlan(cycle, (), ""))
    validate_cleanup_host(repo)
    with cleanup_lifecycle_locks(repo):
        pending = load_pending_cleanup_transaction(repo)
        if pending is not None:
            plan = pending.plan
            require_cycle_plan(plan)
            if plan.cycle != cycle:
                fail(
                    f"cycle cleanup for {plan.cycle!r} must be completed before "
                    f"planning {cycle!r}"
                )
            blockers = resumed_cleanup_runtime_blockers(
                repo,
                cycle,
                inspect_runtime=inspect_runtime,
            )
            if blockers:
                fail(
                    "cycle still has active, uncollected, or unremoved runtime state:\n"
                    + "\n".join(f"  {item}" for item in blockers)
                )
            validate_cleanup_plan_state(repo, plan)
            return plan
        return _build_cleanup_plan_unlocked(
            repo,
            cycle,
            inspect_runtime=inspect_runtime,
        )


def require_cleanup_parent_chain(root: Path, target: Path) -> None:
    """Reject an exact cleanup target reached through any replaced parent."""
    try:
        relative = target.relative_to(root)
    except ValueError:
        fail(f"cycle cleanup target escaped its owned root: {target}")
    if not relative.parts:
        fail("cycle cleanup cannot remove its state root")
    cursor = root
    require_owned_directory(cursor, "cycle cleanup state root")
    for part in relative.parts[:-1]:
        cursor /= part
        require_owned_directory(cursor, "cycle cleanup target parent")


def validate_cleanup_target(repo: Path, root: Path, target: CleanupTarget) -> None:
    require_cleanup_parent_chain(root, target.path)
    if target.kind == "live-result-tree":
        fingerprint = secure_tree_fingerprint(target.path)
    elif target.kind in {"artifact-file", "artifact-tree"}:
        fingerprint = artifact_fingerprint(target.path)
    else:
        require_cleanup_file(target.path, "cycle cleanup target")
        fingerprint = sha256_file(target.path)
    if fingerprint != target.fingerprint:
        fail(f"cycle cleanup target changed after planning: {target.path}")


def validate_cleanup_plan_state(
    repo: Path,
    plan: CleanupPlan,
    *,
    allow_absent: bool = True,
) -> None:
    """Accept only each reviewed target's original state or completed absence."""
    root = cleanup_state_root(repo).resolve(strict=True)
    for target in plan.targets:
        if target.path.parent == target.path:
            fail("cycle cleanup target has no safe parent")
        require_cleanup_parent_chain(root, target.path)
        if not target.path.exists() and not target.path.is_symlink():
            if allow_absent:
                continue
            fail(f"cycle cleanup target changed after planning: {target.path}")
        validate_cleanup_target(repo, root, target)


def finish_cleanup_staged_directory(
    transaction: CleanupTransaction,
    state: CleanupDirectoryState,
    staging: Path,
) -> None:
    """Start or resume one directory rmtree behind its durable phase marker."""
    artifact = transaction.plan.targets[state.index].kind == "artifact-tree"
    phase = cleanup_directory_phase_path(
        transaction.marker,
        transaction.plan.cycle,
        state.index,
    )
    if phase.exists() or phase.is_symlink():
        validate_cleanup_directory_phase(transaction, state)
    else:
        validate_cleanup_directory_state(staging, state, artifact=artifact)
        publish_cleanup_directory_phase(transaction, state)
        validate_cleanup_directory_state(staging, state, artifact=artifact)
    if staging.exists() or staging.is_symlink():
        require_owned_directory(staging, "cycle cleanup directory staging", private=not artifact)
        details = staging.lstat()
        if details.st_dev != state.device or details.st_ino != state.inode:
            fail(f"cycle cleanup directory identity changed: {staging}")
        shutil.rmtree(staging)
        fsync_directory(staging.parent)
    if staging.exists() or staging.is_symlink():
        fail(f"cycle cleanup directory staging was not removed: {staging}")
    validate_cleanup_directory_phase(transaction, state)
    phase.unlink()
    fsync_directory(phase.parent)
    if phase.exists() or phase.is_symlink():
        fail(f"cycle cleanup rmtree phase was not removed: {phase}")


def finish_cleanup_transaction(repo: Path, transaction: CleanupTransaction) -> int:
    plan = transaction.plan
    marker = transaction.marker
    directory_states = {state.index: state for state in transaction.directories}
    root = cleanup_state_root(repo).resolve(strict=True)
    validate_cleanup_plan_state(repo, plan)
    removed = 0
    for index, target in enumerate(plan.targets):
        staging = marker.parent / f".{plan.cycle}.{index}.remove"
        if staging.exists() or staging.is_symlink():
            state = directory_states.get(index)
            if state is None:
                fail(f"cycle cleanup has unexpected file-target staging: {staging}")
            if target.path.exists() or target.path.is_symlink():
                fail(f"cycle cleanup has both target and removal staging: {target.path}")
            finish_cleanup_staged_directory(transaction, state, staging)
            removed += 1
        else:
            state = directory_states.get(index)
            phase = cleanup_directory_phase_path(marker, plan.cycle, index)
            if state is not None and (phase.exists() or phase.is_symlink()):
                if target.path.exists() or target.path.is_symlink():
                    fail(f"cycle cleanup rmtree phase still has its target: {target.path}")
                finish_cleanup_staged_directory(transaction, state, staging)
        require_cleanup_parent_chain(root, target.path)
        if not target.path.exists() and not target.path.is_symlink():
            continue
        validate_cleanup_target(repo, root, target)
        if target.kind in CLEANUP_DIRECTORY_KINDS:
            state = directory_states.get(index)
            if state is None:
                fail(f"cycle cleanup has no exact directory state: {target.path}")
            validate_cleanup_directory_state(target.path, state, artifact=target.kind == "artifact-tree")
            if staging.exists() or staging.is_symlink():
                fail(f"cycle cleanup directory staging already exists: {staging}")
            try:
                container_payload.rename_no_replace(target.path, staging)
            except FileExistsError as error:
                fail(f"cycle cleanup directory staging appeared: {staging}: {error}")
            except (container_payload.PayloadError, OSError) as error:
                fail(f"cannot stage cycle cleanup directory {target.path}: {error}")
            fsync_directory(target.path.parent)
            fsync_directory(staging.parent)
            validate_cleanup_directory_state(staging, state, artifact=target.kind == "artifact-tree")
            finish_cleanup_staged_directory(transaction, state, staging)
        else:
            target.path.unlink()
        if target.path.exists() or target.path.is_symlink():
            fail(f"cycle cleanup did not remove its exact target: {target.path}")
        if staging.exists() or staging.is_symlink():
            fail(f"cycle cleanup directory staging was not removed: {staging}")
        fsync_directory(target.path.parent)
        removed += 1
    pending = load_pending_cleanup_transaction(repo)
    if pending != transaction:
        fail("cycle cleanup transaction changed before finalization")
    marker.unlink()
    fsync_directory(marker.parent)
    if marker.exists() or marker.is_symlink():
        fail(f"cycle cleanup transaction removal did not complete: {marker}")
    return removed


def remove_cleanup_plan(
    repo: Path,
    plan: CleanupPlan,
    confirmation: str,
    *,
    inspect_runtime: bool = False,
) -> int:
    """Durably complete one reviewed plan under every subsystem lifecycle lock."""
    require_cycle_plan(plan)
    validate_cleanup_host(repo)
    if (
        confirmation != plan.digest
        or cleanup_plan_digest(repo, CleanupPlan(plan.cycle, plan.targets, ""))
        != plan.digest
    ):
        fail(
            "CONFIRM does not match the current cleanup plan; rerun cycle-clean-plan "
            "and review every target"
        )
    with cleanup_lifecycle_locks(repo):
        pending = load_pending_cleanup_transaction(repo)
        if pending is not None:
            pending_plan = pending.plan
            if pending_plan != plan:
                fail("CONFIRM does not identify the pending cycle cleanup transaction")
            blockers = resumed_cleanup_runtime_blockers(
                repo,
                plan.cycle,
                inspect_runtime=inspect_runtime,
            )
            if blockers:
                fail(
                    "cycle still has active, uncollected, or unremoved runtime state:\n"
                    + "\n".join(f"  {item}" for item in blockers)
                )
            return finish_cleanup_transaction(repo, pending)

        validate_cleanup_plan_state(repo, plan, allow_absent=False)
        current = _build_cleanup_plan_unlocked(
            repo,
            plan.cycle,
            inspect_runtime=inspect_runtime,
        )
        if current != plan:
            fail(
                "cleanup targets changed after planning; rerun cycle-clean-plan and "
                "review the new digest"
            )
        publish_cleanup_transaction(repo, plan)
        transaction = load_pending_cleanup_transaction(repo)
        if transaction is None or transaction.plan != plan:
            fail("published cycle cleanup transaction disappeared")
        return finish_cleanup_transaction(repo, transaction)


def master_update(repo: Path) -> str:
    require_clean(repo)
    base = sync_repo(repo)
    result = git(repo, "show-ref", "--verify", "--quiet", "refs/heads/master", check=False)
    if result.returncode not in (0, 1):
        fail("cannot inspect local master")
    if result.returncode == 1:
        git(repo, "branch", "--track", BASE_BRANCH, f"origin/{BASE_BRANCH}")
    else:
        local = rev_parse(repo, "refs/heads/master")
        if not is_ancestor(repo, local, base):
            fail("local master is ahead of or diverged from fork master; owner review is required")
        if current_branch(repo) == BASE_BRANCH:
            git(repo, "merge", "--ff-only", f"refs/remotes/origin/{BASE_BRANCH}")
        elif local != base:
            git(repo, "branch", "-f", BASE_BRANCH, f"refs/remotes/origin/{BASE_BRANCH}")
    if rev_parse(repo, "refs/heads/master") != base:
        fail("local master did not reach the fetched fork commit")
    return base


# Automation never reaches for the operator's credentials or signing key. A
# partial clone would otherwise lazily fetch a missing blob from its promisor
# remote, which can open an interactive SSH/security-key prompt.
NONINTERACTIVE_GIT_ENV = {
    "GIT_NO_LAZY_FETCH": "1",
    "GIT_TERMINAL_PROMPT": "0",
}


def missing_objects(repo: Path, *revisions: str) -> list[str]:
    result = run(
        ("git", "-C", str(repo), "rev-list", "--objects", "--missing=print", *revisions),
        env=NONINTERACTIVE_GIT_ENV,
    )
    return sorted({line[1:] for line in result.stdout.splitlines() if line.startswith("?")})


def backfill_missing_objects(repo: Path, *revisions: str, source: str = UPSTREAM_URL) -> int:
    """Complete a partial clone from the public canonical repository.

    Objects are content-addressed, so fetching exactly the missing IDs over
    anonymous HTTPS supplies identical bytes without credentials. Nothing
    names a remote, updates a ref or FETCH_HEAD, or changes configuration.
    """
    missing = missing_objects(repo, *revisions)
    if not missing:
        return 0
    run(
        (
            "git",
            "-C",
            str(repo),
            "-c",
            "credential.helper=",
            "-c",
            "fetch.negotiationAlgorithm=noop",
            "fetch",
            "--no-tags",
            "--no-write-fetch-head",
            "--recurse-submodules=no",
            "--stdin",
            source,
        ),
        env={**NONINTERACTIVE_GIT_ENV, "GIT_ASKPASS": "false", "SSH_ASKPASS": "false"},
        input="".join(f"{oid}\n" for oid in missing),
    )
    remaining = missing_objects(repo, *revisions)
    if remaining:
        fail(f"{len(remaining)} objects are still missing after backfill from {source}")
    return len(missing)


def develop_rebase(repo: Path) -> str:
    verify_repo(repo, ())
    require_clean(repo)
    if current_branch(repo) != INTEGRATION_BRANCH:
        fail(f"current branch must be {INTEGRATION_BRANCH}")
    base = require_local_master(repo)
    backfill_missing_objects(repo, f"refs/heads/{BASE_BRANCH}", "HEAD")
    # Replayed fork commits are never signed: the operator's signing key is
    # interactive, and publication does not require signatures.
    result = run(
        ("git", "-C", str(repo), "-c", "commit.gpgsign=false", "rebase", f"refs/heads/{BASE_BRANCH}"),
        check=False,
        env=NONINTERACTIVE_GIT_ENV,
    )
    if result.returncode:
        detail = "\n".join(
            part for part in (result.stdout.strip(), result.stderr.strip()) if part
        )
        suffix = f"\n{detail}" if detail else ""
        fail(
            "develop rebase stopped; resolve the conflict inside the case commit being "
            "replayed, stage it, and run git -c commit.gpgsign=false rebase --continue "
            "(read runbooks with git show ORIG_HEAD:<path>), or git rebase --abort"
            f"{suffix}"
        )
    require_rebased_develop(repo, base)
    return base


def develop_check(repo: Path) -> dict[str, Any]:
    """Every invariant of the commit model, each case and the whole stack."""
    require_clean(repo)
    state = embedded_develop_state(repo, "develop check")
    base = state.source_commit
    mapping = develop_map(repo, base)
    committed = [case.slug for case in mapping.cases]
    directories = set(case_directories())
    orphans = sorted(slug for slug in committed if slug not in directories)
    if orphans:
        fail(f"case commits without a case directory: {orphans}")
    missing = sorted(
        slug for slug in directories if slug not in committed and slug != TEST_QUARANTINE_SLUG
    )
    if missing:
        fail(f"case directories without a Fork-Case commit: {missing}")
    stray = sorted(str(path.relative_to(AUTOMATION_ROOT)) for path in CASES_ROOT.glob("*/fix.patch"))
    if stray:
        fail(f"stored case patches are not allowed: {stray}")
    for position, slug in enumerate(committed):
        late = [dep for dep in manifest_dependencies(slug) if dep not in committed[:position]]
        if late:
            fail(f"case {slug} must come after its dependencies: {late}")
    artifact_boundary_check(repo)
    ci_layout_check(repo, base)
    cases = [case_check(repo, slug) for slug in committed]
    return {
        "base": base,
        "head": mapping.head,
        "cases": cases,
        "control_commits": len(mapping.control),
        "stack": stack_check(repo),
    }


def print_resolution(resolution: dict[str, Any]) -> None:
    print(json.dumps(resolution, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="validate the in-repository automation boundary")
    commands.add_parser("repo-status", help="show local branch and cached master refs")
    commands.add_parser(
        "repo-sync",
        help="explicit upstream-refresh gate: fetch and verify equal master refs",
    )
    commands.add_parser("master-update", help="fast-forward local master to fetched fork master")
    commands.add_parser(
        "develop-rebase",
        help="explicitly rebase develop during an operator-selected upstream refresh",
    )
    commands.add_parser(
        "objects-backfill",
        help="complete partial-clone objects of local master and HEAD from public upstream",
    )
    commands.add_parser(
        "patch-start-check",
        help="verify an explicitly completed upstream sync and develop rebase",
    )
    commands.add_parser(
        "isolated-start-check",
        help="verify dirty-control-plane-safe isolated patch work without switching branches",
    )
    commands.add_parser(
        "checkout-source-check",
        help="locate the clean source boundary from HEAD and refs named master",
    )
    commands.add_parser(
        "ci-layout-check",
        help="verify disabled upstream workflows and the thin fork workflow",
    )
    commands.add_parser(
        "ci-prepare",
        help="locate the embedded fork-master boundary in a develop CI checkout",
    )
    commands.add_parser(
        "ci-master-sync",
        help="fast-forward fork master from upstream in the dedicated hosted workflow",
    )
    commands.add_parser(
        "ci-deb-prepare",
        help="validate the manual DEB release checkout and locate its source boundary",
    )
    commands.add_parser("develop-check", help="validate every invariant of the develop case commits")
    commands.add_parser("case-list", help="list every case with its develop commit")
    for name, text in (
        ("case-new", "create a case directory with a manifest template and README skeleton"),
        ("case-show", "show the develop commit of one case"),
        ("case-commit", "print only the develop commit SHA of one case"),
        ("case-check", "validate one case commit: applies alone, removable, product-only"),
        ("case-drop", "remove one case commit from develop history"),
    ):
        command = commands.add_parser(name, help=text)
        command.add_argument("case")
    stack = commands.add_parser("stack-check", help="resolve every case commit and reproduce HEAD")
    stack.add_argument("stack")
    commands.add_parser("develop-squash", help="fold pending fixup!/amend!/squash! commits")
    clean_plan = commands.add_parser(
        "cycle-clean-plan",
        help="audit and print the exact finalized artifacts owned by one cycle",
    )
    clean_plan.add_argument("cycle")
    clean = commands.add_parser(
        "cycle-clean",
        help="remove only an unchanged, digest-confirmed cycle cleanup plan",
    )
    clean.add_argument("cycle")
    clean.add_argument("--confirm", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo = args.repo.resolve()
    if args.command == "doctor":
        verify_repo(repo)
        artifact_boundary_check(repo)
        print(f"repository={repo}")
        print(f"case_directories={len(case_directories())}")
        print(f"case_commits={len(develop_map(repo).cases)}")
        print("doctor=passed")
    elif args.command == "repo-status":
        verify_repo(repo)
        print(f"repository={repo}")
        print(f"branch={current_branch(repo)}")
        print(f"head={rev_parse(repo, 'HEAD')}")
        for remote in ("upstream", "origin"):
            try:
                value = cached_master(repo, remote)
            except ContribError:
                value = "<missing>"
            print(f"cached_{remote}_master={value}")
        status = porcelain(repo)
        print(f"worktree={'clean' if not status else 'modified'}")
        if status:
            print(status, end="")
    elif args.command == "repo-sync":
        base = repo_sync(repo)
        print(f"fork_master={base}")
        print("sync=passed")
    elif args.command == "master-update":
        base = master_update(repo)
        print(f"master={base}")
        print("master_update=passed")
    elif args.command == "objects-backfill":
        verify_repo(repo, ())
        count = backfill_missing_objects(repo, f"refs/heads/{BASE_BRANCH}", "HEAD")
        print(f"objects_backfilled={count}")
    elif args.command == "develop-rebase":
        base = develop_rebase(repo)
        print(f"develop_base={base}")
        print("develop_rebase=passed")
    elif args.command == "patch-start-check":
        base = patch_start_check(repo)
        print(f"patch_base={base}")
        print("patch_start_check=passed")
    elif args.command == "isolated-start-check":
        state = isolated_start_check(repo)
        print(f"branch={state.branch}")
        print(f"head={state.head}")
        print(f"source_commit={state.source_commit}")
        print(f"fork_base={state.fork_base}")
        print(f"source_in_head={str(state.source_in_head).lower()}")
        print("isolated_start_check=passed")
    elif args.command == "checkout-source-check":
        state = checkout_source_check(repo)
        print(f"head={state.head}")
        print(f"source_commit={state.source_commit}")
        print(f"master_ref={state.master_ref}")
        print(f"master_commit={state.master_commit}")
        print("checkout_source_check=passed")
    elif args.command == "ci-layout-check":
        base = embedded_develop_state(repo, "CI layout check").source_commit
        print_resolution(ci_layout_check(repo, base))
        print("ci_layout_check=passed")
    elif args.command == "ci-prepare":
        state = ci_prepare(repo)
        print(f"branch={state.branch}")
        print(f"head={state.head}")
        print(f"source_commit={state.source_commit}")
        print("ci_prepare=passed")
    elif args.command == "ci-master-sync":
        state = ci_master_sync(repo)
        print(f"fork_before={state.fork_before}")
        print(f"upstream_before={state.upstream_before}")
        print(f"fork_after={state.fork_after}")
        print(f"upstream_after={state.upstream_after}")
        print(f"updated={str(state.updated).lower()}")
        print("ci_master_sync=passed")
    elif args.command == "ci-deb-prepare":
        state = ci_deb_prepare(repo)
        print(f"head={state.head}")
        print(f"source_commit={state.source_commit}")
        print(f"master_ref={state.master_ref}")
        print(f"master_commit={state.master_commit}")
        print("ci_deb_prepare=passed")
    elif args.command == "develop-check":
        resolution = develop_check(repo)
        print_resolution(resolution)
        print("develop_check=passed")
    elif args.command == "case-list":
        for slug, commit, subject in case_rows(repo):
            print(f"{slug}\t{commit}\t{subject}")
    elif args.command == "case-new":
        print(f"created={scaffold_case(repo, args.case)}")
    elif args.command == "case-show":
        found = case_commit(repo, args.case)
        sys.stdout.write(git(repo, "show", "--stat", "--format=fuller", found.commit).stdout)
    elif args.command == "case-commit":
        print(case_commit(repo, args.case).commit)
    elif args.command == "case-check":
        print_resolution(case_check(repo, args.case))
        print("case_check=passed")
    elif args.command == "case-drop":
        dropped = case_drop(repo, args.case)
        print(f"dropped={dropped}")
        print(f"head={rev_parse(repo, 'HEAD')}")
        print(f"next=remove fork-maintenance/cases/{args.case} and every reference to it")
        print("case_drop=passed")
    elif args.command == "stack-check":
        if args.stack != ACTIVE_STACK:
            fail(f"the only stack is {ACTIVE_STACK}")
        print_resolution(stack_check(repo))
        print("stack_check=passed")
    elif args.command == "develop-squash":
        print(f"head={develop_squash(repo)}")
        print("develop_squash=passed")
    elif args.command == "cycle-clean-plan":
        plan = build_cleanup_plan(repo, args.cycle)
        payload = cleanup_plan_payload(repo, plan)
        payload["confirm"] = plan.digest
        print(json.dumps(payload, indent=2, sort_keys=True))
        print(f"cycle_clean_confirm={plan.digest}")
    elif args.command == "cycle-clean":
        plan = build_cleanup_plan(repo, args.cycle)
        removed = remove_cleanup_plan(
            repo,
            plan,
            args.confirm,
            inspect_runtime=True,
        )
        print(f"cycle={plan.cycle}")
        print(f"removed_targets={removed}")
        print("cycle_clean=passed")
    else:
        raise AssertionError(args.command)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ContribError, OSError, json.JSONDecodeError) as error:
        print(f"fork-maintenance: {error}", file=sys.stderr)
        raise SystemExit(2) from error
