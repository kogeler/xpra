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
WORKSPACE_OWNER = "xpra-fork-isolated-workspace"
WORKSPACE_CREATE_OWNER = "xpra-fork-workspace-create"
WORKSPACE_FINGERPRINT_OWNER = "xpra-fork-workspace-fingerprint"
WORKSPACE_REMOVE_OWNER = "xpra-fork-workspace-remove"
CASE_CREATE_OWNER = "xpra-fork-case-create"
CASE_UPDATE_OWNER = "xpra-fork-case-update"
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
UNIT_TEST_RE = re.compile(r"unit(?:\.[a-z0-9_]+)+")
SELECTION_RE = re.compile(r"(?:cases|stacks)/[a-z0-9]+(?:-[a-z0-9]+)*")
WORKSPACE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
CYCLE_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
RUNNER_PATCH_MODES = frozenset({"clean", "tests-only", "patched"})
WORKSPACE_PATCH_MODES = RUNNER_PATCH_MODES | {"reconstruct"}
SUPPORTED_GATES = frozenset(
    {
        "focused",
        "quarantine",
        "quarantine-cython",
        "quarantine-no-compat",
        "wayland",
        "libyuv",
        "full",
        "full-cython",
        "full-no-compat",
        "live-rgb",
        "live-wayland-keyboard",
        "live-wayland-h264-hardware",
        "live-wayland-opengl-h264-hardware",
    }
)
CASE_KINDS = frozenset({"production", "test-quarantine"})
TEST_QUARANTINE_SLUG = "upstream-test-quarantine"
QUARANTINE_GATE_NAMES = (
    "quarantine",
    "quarantine-cython",
    "quarantine-no-compat",
)
QUARANTINE_GATES = frozenset(QUARANTINE_GATE_NAMES)
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
class Case:
    slug: str
    directory: Path
    kind: str
    title: str
    commit_subject: str
    patch_sha256: str
    dependencies: tuple[str, ...]
    paths: tuple[str, ...]
    tests: tuple[str, ...]
    required_gates: tuple[str, ...]
    quarantined_tests: tuple[str, ...]
    quarantined_tests_by_gate: tuple[tuple[str, tuple[str, ...]], ...]

    @property
    def patch(self) -> Path:
        return self.directory / "fix.patch"

    @property
    def manifest(self) -> Path:
        return self.directory / "case.toml"


@dataclass(frozen=True)
class DraftCase:
    slug: str
    directory: Path
    kind: str
    dependencies: tuple[str, ...]

    @property
    def patch(self) -> Path:
        return self.directory / "fix.patch"

    @property
    def manifest(self) -> Path:
        return self.directory / "case.toml"


@dataclass(frozen=True)
class Stack:
    slug: str
    description: str
    series: tuple[str, ...]
    tests: tuple[str, ...]


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
class Workspace:
    name: str
    directory: Path
    source: Path
    branch: str
    head: str
    source_commit: str
    base_tree: str
    selection: str
    selection_sha256: str
    resolution_sha256: str
    patch_mode: str


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


@dataclass(frozen=True)
class CaseUpdateEntry:
    key: str
    target: Path
    old_payload: Path
    new_payload: Path
    old_sha256: str
    new_sha256: str
    old_mode: int
    new_mode: int


@dataclass(frozen=True)
class CaseUpdateTransaction:
    slug: str
    workspace: str
    operation_id: str
    directory: Path
    owner: Path
    entries: tuple[CaseUpdateEntry, ...]


def fail(message: str) -> NoReturn:
    raise ContribError(message)


def run(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    text: bool = True,
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


def require_string(data: dict[str, Any], key: str, source: Path) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        fail(f"{source}: {key} must be a non-empty string")
    return value


def require_strings(data: dict[str, Any], key: str, source: Path) -> tuple[str, ...]:
    value = data.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        fail(f"{source}: {key} must be an array of strings")
    return tuple(value)


def safe_relative_path(value: str, source: Path) -> str:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts or value != path.as_posix():
        fail(f"{source}: unsafe repository path: {value!r}")
    if value == "fork-maintenance" or value.startswith("fork-maintenance/"):
        fail(f"{source}: a production patch may not modify fork-maintenance")
    return value


def patch_paths(path: Path) -> tuple[str, ...]:
    if path.is_symlink() or not path.is_file():
        fail(f"patch is missing or unsafe: {path}")
    found: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="strict").splitlines():
        if line.startswith(("+++ b/", "--- a/")):
            value = line[6:].split("\t", 1)[0]
            found.add(safe_relative_path(value, path))
    if not found:
        fail(f"{path}: patch contains no repository paths")
    return tuple(sorted(found))


def quarantine_module_paths(modules: Iterable[str]) -> tuple[str, ...]:
    return tuple(
        sorted(f"tests/unittests/{module.replace('.', '/')}.py" for module in modules)
    )


def is_quarantine_path_transition(case: Case | DraftCase) -> bool:
    return (
        isinstance(case, Case)
        and case.slug == TEST_QUARANTINE_SLUG
        and case.kind == "test-quarantine"
        and tuple(sorted(case.paths)) != quarantine_module_paths(case.quarantined_tests)
    )


def validate_case_kind_identity(slug: str, kind: str, manifest: Path) -> None:
    if kind == "test-quarantine" and slug != TEST_QUARANTINE_SLUG:
        fail(
            f"{manifest}: only {TEST_QUARANTINE_SLUG!r} may use "
            "kind='test-quarantine'"
        )
    if slug == TEST_QUARANTINE_SLUG and kind != "test-quarantine":
        fail(f"{manifest}: {TEST_QUARANTINE_SLUG!r} must use kind='test-quarantine'")


def case_is_draft(directory: Path) -> bool:
    manifest = directory / "case.toml"
    return manifest.is_file() and read_toml(manifest).get("draft") is True


def load_case(
    directory: Path,
    *,
    allow_quarantine_path_transition: bool = False,
) -> Case:
    manifest = directory / "case.toml"
    data = read_toml(manifest)
    if data.get("schema") != 1 or data.get("draft") is True:
        fail(f"{manifest}: unsupported or draft schema")
    slug = require_string(data, "slug", manifest)
    if not SLUG_RE.fullmatch(slug) or slug != directory.name:
        fail(f"{manifest}: slug must match its directory")
    kind = data.get("kind", "production")
    if not isinstance(kind, str) or kind not in CASE_KINDS:
        fail(f"{manifest}: kind must be one of {sorted(CASE_KINDS)}")
    validate_case_kind_identity(slug, kind, manifest)
    title = require_string(data, "title", manifest)
    subject = require_string(data, "commit_subject", manifest)
    if "\n" in title or "\n" in subject:
        fail(f"{manifest}: title and commit_subject must be one line")
    patch_sha256 = require_string(data, "patch_sha256", manifest)
    if not SHA256_RE.fullmatch(patch_sha256):
        fail(f"{manifest}: patch_sha256 must be lowercase SHA-256")
    dependencies = require_strings(data, "dependencies", manifest)
    if (
        len(dependencies) != len(set(dependencies))
        or slug in dependencies
        or not all(SLUG_RE.fullmatch(value) for value in dependencies)
    ):
        fail(f"{manifest}: dependencies must be unique case slugs")
    paths = tuple(
        safe_relative_path(value, manifest)
        for value in require_strings(data, "paths", manifest)
    )
    if not paths or len(paths) != len(set(paths)):
        fail(f"{manifest}: paths must be non-empty and unique")
    tests_data = data.get("tests")
    if not isinstance(tests_data, dict):
        fail(f"{manifest}: missing [tests] table")
    tests = require_strings(tests_data, "list", manifest)
    if (
        not tests
        or len(tests) != len(set(tests))
        or not all(TEST_RE.fullmatch(item) for item in tests)
        or not any(UNIT_TEST_RE.fullmatch(item) for item in tests)
    ):
        fail(f"{manifest}: tests.list must include unique safe names and a unit.* test")
    evidence_data = data.get("evidence")
    if not isinstance(evidence_data, dict):
        fail(f"{manifest}: missing [evidence] table")
    required_gates = require_strings(evidence_data, "required_gates", manifest)
    if len(required_gates) != len(set(required_gates)) or set(required_gates).difference(
        SUPPORTED_GATES
    ):
        fail(f"{manifest}: evidence.required_gates contains an unsupported gate")
    quarantine_data = data.get("quarantine")
    quarantined_tests: tuple[str, ...] = ()
    quarantined_tests_by_gate: tuple[tuple[str, tuple[str, ...]], ...] = ()
    if kind == "production":
        if quarantine_data is not None:
            fail(f"{manifest}: production cases may not declare [quarantine]")
        if set(required_gates).intersection(QUARANTINE_GATES):
            fail(f"{manifest}: production cases may not declare quarantine gates")
    else:
        if dependencies:
            fail(f"{manifest}: test-quarantine cases may not have dependencies")
        if not isinstance(quarantine_data, dict):
            fail(f"{manifest}: test-quarantine cases require [quarantine]")
        if set(quarantine_data) != {"modules", "gates"}:
            fail(
                f"{manifest}: test-quarantine quarantine must contain exactly "
                "modules and gates"
            )
        quarantined_tests = require_strings(quarantine_data, "modules", manifest)
        if (
            not quarantined_tests
            or len(quarantined_tests) != len(set(quarantined_tests))
            or not all(UNIT_TEST_RE.fullmatch(item) for item in quarantined_tests)
        ):
            fail(f"{manifest}: quarantine.modules must contain unique unit.* modules")
        if not set(quarantined_tests).issubset(tests):
            fail(f"{manifest}: every quarantined module must be retained in tests.list")
        gates_data = quarantine_data.get("gates")
        if not isinstance(gates_data, dict) or set(gates_data) != QUARANTINE_GATES:
            fail(
                f"{manifest}: quarantine.gates must contain exactly "
                f"{QUARANTINE_GATE_NAMES}"
            )
        assigned: set[str] = set()
        for gate in QUARANTINE_GATE_NAMES:
            gate_modules = require_strings(gates_data, gate, manifest)
            if (
                len(gate_modules) != len(set(gate_modules))
                or not all(UNIT_TEST_RE.fullmatch(item) for item in gate_modules)
            ):
                fail(f"{manifest}: quarantine.gates.{gate} must contain unique unit.* modules")
            gate_set = set(gate_modules)
            if not gate_set.issubset(quarantined_tests):
                fail(f"{manifest}: quarantine.gates.{gate} is not a subset of modules")
            ordered = tuple(test for test in quarantined_tests if test in gate_set)
            if gate_modules != ordered:
                fail(
                    f"{manifest}: quarantine.gates.{gate} must preserve "
                    "quarantine.modules order"
                )
            assigned.update(gate_modules)
            quarantined_tests_by_gate += ((gate, gate_modules),)
        if assigned != set(quarantined_tests):
            fail(f"{manifest}: every quarantined module must be assigned to at least one gate")
        if set(required_gates) != QUARANTINE_GATES:
            fail(f"{manifest}: a test-quarantine case must require all quarantine gates")
        expected_paths = quarantine_module_paths(quarantined_tests)
        if (
            tuple(sorted(paths)) != expected_paths
            and not allow_quarantine_path_transition
        ):
            fail(
                f"{manifest}: test-quarantine paths must exactly match its modules: "
                f"{expected_paths}"
            )
    case = Case(
        slug,
        directory,
        kind,
        title,
        subject,
        patch_sha256,
        dependencies,
        paths,
        tests,
        required_gates,
        quarantined_tests,
        quarantined_tests_by_gate,
    )
    if sha256_file(case.patch) != case.patch_sha256:
        fail(f"{manifest}: fix.patch does not match patch_sha256")
    if tuple(sorted(case.paths)) != patch_paths(case.patch):
        fail(f"{manifest}: paths do not match fix.patch")
    readme = directory / "README.md"
    if readme.is_symlink() or not readme.is_file() or not readme.read_text(
        encoding="utf-8"
    ).strip():
        fail(f"{manifest}: README.md is missing or empty")
    return case


def load_draft_case(directory: Path) -> DraftCase:
    manifest = directory / "case.toml"
    data = read_toml(manifest)
    if data.get("schema") != 1 or data.get("draft") is not True:
        fail(f"{manifest}: not a draft case")
    slug = require_string(data, "slug", manifest)
    if not SLUG_RE.fullmatch(slug) or slug != directory.name:
        fail(f"{manifest}: slug must match its directory")
    kind = data.get("kind", "production")
    if not isinstance(kind, str) or kind not in CASE_KINDS:
        fail(f"{manifest}: kind must be one of {sorted(CASE_KINDS)}")
    validate_case_kind_identity(slug, kind, manifest)
    dependencies = require_strings(data, "dependencies", manifest)
    if (
        len(dependencies) != len(set(dependencies))
        or slug in dependencies
        or not all(SLUG_RE.fullmatch(value) for value in dependencies)
    ):
        fail(f"{manifest}: dependencies must be unique case slugs")
    for name in ("fix.patch", "README.md"):
        path = directory / name
        if path.is_symlink() or not path.is_file():
            fail(f"{manifest}: draft file is missing or unsafe: {name}")
    return DraftCase(slug, directory, kind, dependencies)


def load_cases(*, quarantine_path_transition: str = "") -> dict[str, Case]:
    if CASES_ROOT.is_symlink() or not CASES_ROOT.is_dir():
        fail(f"cases directory is missing or unsafe: {CASES_ROOT}")
    if quarantine_path_transition and not SLUG_RE.fullmatch(quarantine_path_transition):
        fail(f"invalid quarantine path transition slug: {quarantine_path_transition!r}")
    cases = {
        path.name: load_case(
            path,
            allow_quarantine_path_transition=(
                path.name == quarantine_path_transition
            ),
        )
        for path in sorted(CASES_ROOT.iterdir())
        if path.is_dir() and not path.name.startswith(".") and not case_is_draft(path)
    }
    if not cases:
        fail("no completed patch cases found")
    for case in cases.values():
        unknown = set(case.dependencies).difference(cases)
        if unknown:
            fail(f"{case.manifest}: unknown dependencies: {sorted(unknown)}")
    return cases


def load_drafts() -> dict[str, DraftCase]:
    if CASES_ROOT.is_symlink() or not CASES_ROOT.is_dir():
        fail(f"cases directory is missing or unsafe: {CASES_ROOT}")
    return {
        path.name: load_draft_case(path)
        for path in sorted(CASES_ROOT.iterdir())
        if path.is_dir() and not path.name.startswith(".") and case_is_draft(path)
    }


def load_stacks(cases: dict[str, Case]) -> dict[str, Stack]:
    if STACKS_ROOT.is_symlink() or not STACKS_ROOT.is_dir():
        fail(f"stacks directory is missing or unsafe: {STACKS_ROOT}")
    stacks: dict[str, Stack] = {}
    for path in sorted(STACKS_ROOT.glob("*.toml")):
        data = read_toml(path)
        if data.get("schema") != 1:
            fail(f"{path}: unsupported schema")
        slug = require_string(data, "slug", path)
        if not SLUG_RE.fullmatch(slug) or slug != path.stem:
            fail(f"{path}: slug must match its filename")
        series = require_strings(data, "series", path)
        if not series or len(series) != len(set(series)) or set(series).difference(cases):
            fail(f"{path}: series must contain unique known cases")
        seen: set[str] = set()
        selected = set(series)
        for case_slug in series:
            missing = set(cases[case_slug].dependencies).intersection(selected).difference(seen)
            if missing:
                fail(f"{path}: {case_slug} precedes dependencies {sorted(missing)}")
            seen.add(case_slug)
        tests_data = data.get("tests")
        if not isinstance(tests_data, dict):
            fail(f"{path}: missing [tests] table")
        tests = require_strings(tests_data, "list", path)
        if not tests or len(tests) != len(set(tests)) or not all(
            TEST_RE.fullmatch(item) for item in tests
        ):
            fail(f"{path}: tests.list must contain unique safe names")
        stacks[slug] = Stack(
            slug,
            require_string(data, "description", path),
            series,
            tests,
        )
    if not stacks:
        fail("no patch stacks found")
    return stacks


def get_case(
    slug: str,
    *,
    allow_draft: bool = False,
    allow_quarantine_path_transition: bool = False,
) -> Case | DraftCase:
    if not SLUG_RE.fullmatch(slug):
        fail(f"invalid case slug: {slug!r}")
    directory = CASES_ROOT / slug
    if allow_draft and directory.is_dir() and case_is_draft(directory):
        return load_draft_case(directory)
    try:
        return load_cases(
            quarantine_path_transition=(
                slug if allow_quarantine_path_transition else ""
            )
        )[slug]
    except KeyError as error:
        raise ContribError(f"unknown completed case: {slug}") from error


def normalize_url(value: str) -> str:
    return value.rstrip("/").removesuffix(".git")


def verify_repo(
    repo: Path,
    remotes: Sequence[str] = ("origin", "upstream"),
) -> None:
    if repo.is_symlink() or not repo.is_dir():
        fail(f"Xpra repository is missing or unsafe: {repo}")
    top = Path(git(repo, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if top != repo.resolve():
        fail(f"repository path is not its top level: {repo}")
    for remote in remotes:
        expected = REMOTE_URLS.get(remote)
        if expected is None:
            fail(f"unsupported repository remote: {remote}")
        actual = git(repo, "remote", "get-url", remote).stdout.strip()
        if normalize_url(actual) != normalize_url(expected):
            fail(f"{remote} has unexpected URL: {actual}")


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


def require_current_master_in_history(repo: Path, base: str) -> str:
    head = rev_parse(repo, "HEAD")
    if not is_ancestor(repo, base, head):
        fail(
            f"current branch does not contain fork origin/{BASE_BRANCH} {base}; "
            "rebase develop onto the updated local master first"
        )
    return head


def require_local_master(repo: Path, base: str) -> str:
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
        fail("local master is missing; run master-update")
    local = rev_parse(repo, f"refs/heads/{BASE_BRANCH}")
    if local != base:
        fail(f"local master {local} is stale; run master-update before rebasing develop")
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
            f"develop is not rebased onto fork origin/{BASE_BRANCH} {base}; "
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


def require_patch_branch(repo: Path, base: str) -> str:
    branch = require_non_master(repo)
    develop = require_rebased_develop(repo, base)
    head = require_current_master_in_history(repo, base)
    if branch != INTEGRATION_BRANCH and not is_ancestor(repo, develop, head):
        fail("temporary patch branches must start from the fully rebased develop branch")
    return head


def patch_start_check(repo: Path) -> str:
    verify_repo(repo, ("origin",))
    require_clean(repo)
    require_non_master(repo)
    base = sync_repo(repo)
    require_patch_branch(repo, base)
    return base


def require_source_baseline(repo: Path, base: str, paths: Iterable[str]) -> None:
    selected = tuple(sorted(set(paths)))
    if not selected:
        return
    result = git(repo, "diff", "--quiet", f"{base}..HEAD", "--", *selected, check=False)
    if result.returncode not in (0, 1):
        fail("cannot compare current source paths with fork master")
    if result.returncode:
        changed = git(repo, "diff", "--name-only", f"{base}..HEAD", "--", *selected).stdout
        fail(
            "current branch contains committed source changes managed by the patch queue:\n"
            f"{changed.rstrip()}"
        )


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


def case_selection_digest(case: Case) -> str:
    """Return the canonical selection digest for one exact completed case."""
    lab_root = case.directory.parent.parent
    result = run(
        (
            sys.executable,
            str(SELECTION_TOOL),
            "--lab-root",
            str(lab_root),
            "--selection",
            f"cases/{case.slug}",
            "digest",
        )
    )
    lines = result.stdout.splitlines()
    if len(lines) != 1 or not SHA256_RE.fullmatch(lines[0]):
        fail("case selection digest output is invalid")
    return lines[0]


def case_patch_status(repo: Path, revision: str, case: Case) -> str:
    """Classify one validated patch against an immutable source revision."""
    if not GIT_SHA_RE.fullmatch(revision):
        fail("case patch classification has an invalid source commit")
    with tempfile.TemporaryDirectory(prefix="xpra-fork-reconstruct-") as raw:
        source = Path(raw)
        archive_tree(repo, revision, source)
        forward = (
            git(
                source,
                "apply",
                "--check",
                "--whitespace=error-all",
                str(case.patch),
                check=False,
            ).returncode
            == 0
        )
        reverse = (
            git(
                source,
                "apply",
                "--reverse",
                "--check",
                "--whitespace=error-all",
                str(case.patch),
                check=False,
            ).returncode
            == 0
        )
    if forward and reverse:
        return "ambiguous"
    if forward:
        return "apply"
    if reverse:
        return "already-present"
    return "diverged"


def reconstruction_selection_resolution(
    repo: Path,
    revision: str,
    selection: str,
    case: Case,
) -> dict[str, Any]:
    """Bind a completed diverged case before rebuilding it from clean source."""
    expected_selection = f"cases/{case.slug}"
    if selection != expected_selection:
        fail("reconstruction requires exactly one completed case selection")
    if case.dependencies:
        fail(
            f"case {case.slug} has dependencies; reconstruction requires an "
            "independent completed case"
        )
    status = case_patch_status(repo, revision, case)
    if status != "diverged":
        fail(
            f"case {case.slug} is {status} at base {revision}; "
            "reconstruction requires a diverged patch"
        )
    payload: dict[str, Any] = {
        "schema": 1,
        "source_commit": revision,
        "selection": selection,
        "selection_sha256": case_selection_digest(case),
        "declared_cases": [case.slug],
        "base_dependencies": [],
        "patches": [
            {
                "case": case.slug,
                "patch": f"cases/{case.slug}/fix.patch",
                "patch_sha256": case.patch_sha256,
                "status": "diverged",
            }
        ],
        "applied_cases": [],
        "already_present_cases": [],
        "reconstruction_case": case.slug,
        "reconstruction_manifest_sha256": sha256_file(case.manifest),
        "reconstruction_paths": list(case.paths),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["resolution_sha256"] = hashlib.sha256(canonical).hexdigest()
    return payload


def selected_cases(selection: str) -> tuple[Case, ...]:
    cases = load_cases()
    kind, slug = selection.split("/", 1)
    if kind == "cases":
        try:
            return (cases[slug],)
        except KeyError as error:
            raise ContribError(f"unknown case: {slug}") from error
    stacks = load_stacks(cases)
    try:
        return tuple(cases[item] for item in stacks[slug].series)
    except KeyError as error:
        raise ContribError(f"unknown stack: {slug}") from error


def effective_cases(selection: str, resolution: dict[str, Any]) -> tuple[Case, ...]:
    cases = {case.slug: case for case in selected_cases(selection)}
    applied = resolution.get("applied_cases")
    if not isinstance(applied, list) or not all(isinstance(item, str) for item in applied):
        fail("selection resolution has an invalid applied_cases list")
    try:
        return tuple(cases[slug] for slug in applied)
    except KeyError as error:
        raise ContribError(f"selection resolution names an unknown case: {error.args[0]}") from error


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


def apply_selection(repo: Path, selection: str) -> dict[str, Any]:
    base = patch_start_check(repo)
    cases = selected_cases(selection)
    require_source_baseline(repo, base, (path for case in cases for path in case.paths))
    resolution = selection_resolution(repo, base, selection)
    effective = effective_cases(selection, resolution)
    applied: list[Case] = []
    try:
        for case in effective:
            git(repo, "apply", "--index", "--check", "--whitespace=error-all", str(case.patch))
            git(repo, "apply", "--index", "--whitespace=error-all", str(case.patch))
            applied.append(case)
    except BaseException:
        rollback_failures: list[str] = []
        for case in reversed(applied):
            result = git(
                repo,
                "apply",
                "--reverse",
                "--index",
                "--whitespace=error-all",
                str(case.patch),
                check=False,
            )
            if result.returncode:
                rollback_failures.append(case.slug)
        if rollback_failures:
            fail(f"patch application failed and rollback failed for {rollback_failures}")
        raise
    expected = tuple(sorted({path for case in effective for path in case.paths}))
    actual = tuple(sorted(staged_names(repo)))
    if actual != expected:
        fail(f"staged paths {actual} do not match the resolved patch paths {expected}")
    if git(repo, "diff", "--cached", "--check", check=False).returncode:
        fail("applied patch queue fails git diff --cached --check")
    if unstaged_names(repo) or untracked_names(repo):
        fail("patch application produced unexpected unstaged or untracked files")
    return resolution


def allowed_case_metadata(repo: Path, cases: Iterable[Case]) -> set[str]:
    allowed: set[str] = set()
    root = repo.resolve()
    for case in cases:
        for path in (case.patch, case.manifest):
            allowed.add(path.resolve().relative_to(root).as_posix())
    return allowed


def unapply_selection(repo: Path, selection: str) -> dict[str, Any]:
    verify_repo(repo)
    require_non_master(repo)
    if untracked_names(repo):
        fail(f"repository contains untracked files: {untracked_names(repo)}")
    cases = selected_cases(selection)
    unexpected = set(unstaged_names(repo)).difference(allowed_case_metadata(repo, cases))
    if unexpected:
        fail(f"repository contains unrelated unstaged changes: {sorted(unexpected)}")
    head = rev_parse(repo, "HEAD")
    resolution = selection_resolution(repo, head, selection)
    effective = effective_cases(selection, resolution)
    expected = tuple(sorted({path for case in effective for path in case.paths}))
    actual = tuple(sorted(staged_names(repo)))
    if actual != expected:
        fail(f"staged paths {actual} do not match the removable patch paths {expected}")
    removed: list[Case] = []
    try:
        for case in reversed(effective):
            git(
                repo,
                "apply",
                "--reverse",
                "--index",
                "--check",
                "--whitespace=error-all",
                str(case.patch),
            )
            git(
                repo,
                "apply",
                "--reverse",
                "--index",
                "--whitespace=error-all",
                str(case.patch),
            )
            removed.append(case)
    except BaseException:
        restore_failures: list[str] = []
        for case in reversed(removed):
            result = git(
                repo,
                "apply",
                "--index",
                "--whitespace=error-all",
                str(case.patch),
                check=False,
            )
            if result.returncode:
                restore_failures.append(case.slug)
        if restore_failures:
            fail(f"patch removal failed and restoration failed for {restore_failures}")
        raise
    if staged_names(repo):
        fail("patch removal did not restore the committed source tree")
    return resolution


def render_paths(paths: Iterable[str]) -> str:
    entries = "\n".join(f"  {json.dumps(path)}," for path in paths)
    return f"paths = [\n{entries}\n]"


def updated_manifest_text(
    original: str,
    *,
    digest: str,
    paths: tuple[str, ...],
    draft: bool,
) -> str:
    result = original
    if draft:
        marker = re.compile(r"^draft = true\n", re.MULTILINE)
        if len(marker.findall(result)) != 1:
            fail("draft marker was edited unexpectedly")
        result = marker.sub("", result)
    digest_pattern = re.compile(r'^patch_sha256 = "(?:[0-9a-f]{64})?"$', re.MULTILINE)
    if len(digest_pattern.findall(result)) != 1:
        fail("manifest must contain exactly one patch_sha256 field")
    result = digest_pattern.sub(f'patch_sha256 = "{digest}"', result)
    paths_pattern = re.compile(r"^paths = \[[^]]*\]", re.MULTILINE)
    if len(paths_pattern.findall(result)) != 1:
        fail("manifest must contain exactly one paths array")
    return paths_pattern.sub(render_paths(paths), result)


def case_updates_root(repo: Path, *, create: bool = False) -> Path:
    relative = "case-updates"
    if create:
        return prepare_private_subdirectory(repo, relative, "case update root")
    return repo / ".artifacts" / "fork-maintenance" / relative


def case_update_paths(repo: Path, slug: str) -> tuple[Path, Path]:
    root = case_updates_root(repo, create=True)
    return root / f"{slug}.update", root / f"{slug}.update.owner.json"


def case_update_removal_paths(repo: Path, slug: str) -> tuple[Path, Path]:
    root = case_updates_root(repo, create=True)
    return root / f".{slug}.update.remove", root / f"{slug}.update.remove.json"


def fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def require_case_update_lock(path: Path) -> None:
    info = require_cleanup_file(path, "case update lifecycle lock")
    if stat.S_IMODE(info.st_mode) != 0o600:
        fail(f"invalid retained case update lifecycle lock: {path}")


@contextmanager
def case_update_lock(repo: Path) -> Iterator[None]:
    root = case_updates_root(repo, create=True)
    lock = root / ".lifecycle.lock"
    descriptor = open_retained_lifecycle_lock(lock, "case update lifecycle lock")
    try:
        yield
    finally:
        os.close(descriptor)


def case_update_owner_payload(
    repo: Path,
    slug: str,
    workspace: str,
    operation_id: str,
    quarantine_path_transition: bool = False,
) -> dict[str, Any]:
    transaction, owner = case_update_paths(repo, slug)
    return {
        "kind": "case-update",
        "operation_id": operation_id,
        "owner": CASE_UPDATE_OWNER,
        "policy": "complete",
        "quarantine_path_transition": quarantine_path_transition,
        "repository": str(repo.resolve()),
        "schema": 1,
        "slug": slug,
        "transaction": str(transaction),
        "workspace": workspace,
        "owner_record": str(owner),
    }


def case_update_quarantine_path_transition(payload: dict[str, Any]) -> bool:
    """Return the validated transition authority, including legacy false."""
    return bool(payload.get("quarantine_path_transition", False))


def validate_case_update_owner(repo: Path, slug: str) -> dict[str, Any]:
    transaction, owner = case_update_paths(repo, slug)
    payload = load_cleanup_json(owner, "case update owner")
    workspace = payload.get("workspace")
    operation_id = payload.get("operation_id")
    has_quarantine_path_transition = "quarantine_path_transition" in payload
    quarantine_path_transition = payload.get("quarantine_path_transition", False)
    if not isinstance(workspace, str) or (
        workspace and not WORKSPACE_RE.fullmatch(workspace)
    ):
        fail(f"case update owner has an invalid workspace: {owner}")
    if not isinstance(operation_id, str) or not UUID4_RE.fullmatch(operation_id):
        fail(f"case update owner has an invalid operation identity: {owner}")
    if has_quarantine_path_transition and not isinstance(quarantine_path_transition, bool):
        fail(f"case update owner has invalid quarantine transition authority: {owner}")
    expected = case_update_owner_payload(
        repo,
        slug,
        workspace,
        operation_id,
        quarantine_path_transition,
    )
    if not has_quarantine_path_transition:
        expected.pop("quarantine_path_transition")
    if payload != expected:
        fail(f"case update owner is inconsistent: {owner}")
    if Path(str(payload["transaction"])) != transaction:
        fail(f"case update owner escaped its exact transaction: {owner}")
    return payload


def require_case_update_target(path: Path, description: str) -> tuple[bytes, int]:
    try:
        info = path.lstat()
    except OSError as error:
        fail(f"{description} is unavailable: {path}: {error}")
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) & ~0o777
    ):
        fail(f"{description} is not a singly-linked owned regular file: {path}")
    try:
        return path.read_bytes(), stat.S_IMODE(info.st_mode)
    except OSError as error:
        fail(f"cannot read {description} {path}: {error}")


def case_update_expected_targets(
    repo: Path,
    slug: str,
    workspace: str,
) -> tuple[tuple[str, Path], ...]:
    case_directory = repo / "fork-maintenance" / "cases" / slug
    targets: list[tuple[str, Path]] = [
        ("case-patch", case_directory / "fix.patch"),
        ("case-manifest", case_directory / "case.toml"),
    ]
    if workspace:
        directory = workspace_root(repo) / workspace
        targets.extend(
            (
                ("workspace-resolution", workspace_resolution_path(directory)),
                ("workspace-metadata", workspace_metadata_path(directory)),
            )
        )
    return tuple(targets)


def require_case_update_filesystem(
    transaction_root: Path,
    targets: tuple[tuple[str, Path], ...],
) -> None:
    """Fail before publication when atomic target replacement is impossible."""
    try:
        transaction_device = transaction_root.stat().st_dev
        target_devices = {target.parent.stat().st_dev for _key, target in targets}
    except OSError as error:
        fail(f"cannot inspect case update filesystem boundary: {error}")
    if target_devices != {transaction_device}:
        fail("case update transaction and every target must share one filesystem")


def canonical_json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def validate_case_update_publication(path: Path, entry: CaseUpdateEntry) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        fail(f"case update publication is unavailable: {path}: {error}")
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) not in {0o600, entry.new_mode}
        or sha256_file(path) != entry.new_sha256
    ):
        fail(f"case update publication is inconsistent: {path}")


def case_update_target_state(entry: CaseUpdateEntry) -> str:
    payload, mode = require_case_update_target(entry.target, f"case update target {entry.key}")
    digest = sha256_bytes(payload)
    if digest == entry.new_sha256 and mode == entry.new_mode:
        return "new"
    if digest == entry.old_sha256 and mode == entry.old_mode:
        return "old"
    fail(f"case update target is neither its exact old nor new state: {entry.target}")


def validate_case_update_transaction(
    repo: Path,
    slug: str,
) -> CaseUpdateTransaction:
    transaction_directory, owner_path = case_update_paths(repo, slug)
    owner = validate_case_update_owner(repo, slug)
    require_private_directory(transaction_directory, "case update transaction")
    marker_path = transaction_directory / "transaction.json"
    marker = load_cleanup_json(marker_path, "case update transaction")
    expected_top = {
        "entries",
        "kind",
        "operation_id",
        "owner",
        "policy",
        "quarantine_path_transition",
        "repository",
        "schema",
        "slug",
        "transaction",
        "workspace",
        "owner_record",
    }
    if "quarantine_path_transition" not in owner:
        expected_top.remove("quarantine_path_transition")
    if set(marker) != expected_top:
        fail(f"case update transaction has an unexpected schema: {marker_path}")
    for key in expected_top.difference({"entries"}):
        if marker.get(key) != owner.get(key):
            fail(f"case update transaction does not match its owner: {marker_path}")
    expected_targets = case_update_expected_targets(repo, slug, str(owner["workspace"]))
    raw_entries = marker.get("entries")
    if not isinstance(raw_entries, list) or len(raw_entries) != len(expected_targets):
        fail(f"case update transaction has an invalid entry set: {marker_path}")
    entries: list[CaseUpdateEntry] = []
    allowed_names = {"transaction.json"}
    entry_keys = {
        "key",
        "new_mode",
        "new_payload",
        "new_sha256",
        "old_mode",
        "old_payload",
        "old_sha256",
        "target",
    }
    for raw, (expected_key, expected_target) in zip(
        raw_entries,
        expected_targets,
        strict=True,
    ):
        if not isinstance(raw, dict) or set(raw) != entry_keys:
            fail(f"case update transaction entry is invalid: {marker_path}")
        old_name = f"{expected_key}.old"
        new_name = f"{expected_key}.new"
        if (
            raw.get("key") != expected_key
            or raw.get("target") != str(expected_target)
            or raw.get("old_payload") != old_name
            or raw.get("new_payload") != new_name
            or not SHA256_RE.fullmatch(str(raw.get("old_sha256", "")))
            or not SHA256_RE.fullmatch(str(raw.get("new_sha256", "")))
        ):
            fail(f"case update transaction entry escaped its exact target: {marker_path}")
        old_mode = raw.get("old_mode")
        new_mode = raw.get("new_mode")
        if (
            not isinstance(old_mode, int)
            or isinstance(old_mode, bool)
            or not isinstance(new_mode, int)
            or isinstance(new_mode, bool)
            or old_mode < 0
            or old_mode > 0o777
            or new_mode != old_mode
        ):
            fail(f"case update transaction entry has invalid modes: {marker_path}")
        old_payload = transaction_directory / old_name
        new_payload = transaction_directory / new_name
        for path, description in (
            (old_payload, "old case update payload"),
            (new_payload, "new case update payload"),
        ):
            info = require_cleanup_file(path, description)
            if stat.S_IMODE(info.st_mode) != 0o600:
                fail(f"case update payload has an invalid mode: {path}")
        old_sha256 = str(raw["old_sha256"])
        new_sha256 = str(raw["new_sha256"])
        if (
            sha256_file(old_payload) != old_sha256
            or sha256_file(new_payload) != new_sha256
        ):
            fail(f"case update payload digest is inconsistent: {marker_path}")
        entry = CaseUpdateEntry(
            expected_key,
            expected_target,
            old_payload,
            new_payload,
            old_sha256,
            new_sha256,
            old_mode,
            new_mode,
        )
        publication = transaction_directory / f".{expected_key}.publish"
        if publication.exists() or publication.is_symlink():
            validate_case_update_publication(publication, entry)
            allowed_names.add(publication.name)
        allowed_names.update((old_name, new_name))
        case_update_target_state(entry)
        entries.append(entry)
    actual_names = {path.name for path in transaction_directory.iterdir()}
    if actual_names != allowed_names:
        fail(f"case update transaction contains unexpected staging: {transaction_directory}")
    if all(entry.old_sha256 == entry.new_sha256 for entry in entries):
        fail(f"case update transaction has no changed payload: {marker_path}")
    return CaseUpdateTransaction(
        slug,
        str(owner["workspace"]),
        str(owner["operation_id"]),
        transaction_directory,
        owner_path,
        tuple(entries),
    )


def publish_case_update_entry(
    transaction: CaseUpdateTransaction,
    entry: CaseUpdateEntry,
) -> None:
    publication = transaction.directory / f".{entry.key}.publish"
    state = case_update_target_state(entry)
    if publication.exists() or publication.is_symlink():
        validate_case_update_publication(publication, entry)
    elif state == "old":
        try:
            background_job.publish_bytes(publication, entry.new_payload.read_bytes())
        except (background_job.BackgroundJobError, OSError) as error:
            fail(f"cannot publish case update staging {publication}: {error}")
    if state == "new":
        if publication.exists() or publication.is_symlink():
            publication.unlink()
            fsync_directory(transaction.directory)
        return
    validate_case_update_publication(publication, entry)
    require_owned_directory(entry.target.parent, "case update target parent", private=False)
    transaction_fd = os.open(
        transaction.directory,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    target_fd = os.open(
        entry.target.parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    publication_fd = -1
    try:
        publication_fd = os.open(
            publication.name,
            os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=transaction_fd,
        )
        os.fchmod(publication_fd, entry.new_mode)
        os.fsync(publication_fd)
        validate_case_update_publication(publication, entry)
        if case_update_target_state(entry) != "old":
            fail(f"case update target changed before publication: {entry.target}")
        os.replace(
            publication.name,
            entry.target.name,
            src_dir_fd=transaction_fd,
            dst_dir_fd=target_fd,
        )
        os.fsync(target_fd)
        os.fsync(transaction_fd)
    except OSError as error:
        fail(f"cannot atomically publish case update target {entry.target}: {error}")
    finally:
        if publication_fd >= 0:
            os.close(publication_fd)
        os.close(target_fd)
        os.close(transaction_fd)
    if case_update_target_state(entry) != "new":
        fail(f"case update target publication did not complete: {entry.target}")


def case_update_remove_payload(
    repo: Path,
    slug: str,
    disposition: str,
    device: int,
    inode: int,
    fingerprint: str,
    targets: list[dict[str, Any]],
    operation_id: str,
    owner_sha256: str,
    workspace: str,
    quarantine_path_transition: bool,
    *,
    include_quarantine_path_transition: bool = True,
) -> dict[str, Any]:
    transaction, owner = case_update_paths(repo, slug)
    staging, _removal = case_update_removal_paths(repo, slug)
    payload = {
        "device": device,
        "disposition": disposition,
        "fingerprint": fingerprint,
        "inode": inode,
        "kind": "case-update-rmtree-started",
        "operation_id": operation_id,
        "owner": CASE_UPDATE_OWNER,
        "owner_record": str(owner),
        "owner_sha256": owner_sha256,
        "policy": "complete",
        "repository": str(repo.resolve()),
        "schema": 1,
        "slug": slug,
        "staging": str(staging),
        "targets": targets,
        "transaction": str(transaction),
        "workspace": workspace,
    }
    if include_quarantine_path_transition:
        payload["quarantine_path_transition"] = quarantine_path_transition
    return payload


def validate_case_update_remove_transaction(repo: Path, slug: str) -> dict[str, Any]:
    transaction, owner = case_update_paths(repo, slug)
    staging, removal = case_update_removal_paths(repo, slug)
    payload = load_cleanup_json(removal, "case update removal phase")
    disposition = payload.get("disposition")
    device = payload.get("device")
    inode = payload.get("inode")
    fingerprint = payload.get("fingerprint")
    operation_id = payload.get("operation_id")
    owner_sha256 = payload.get("owner_sha256")
    workspace_name = payload.get("workspace")
    has_quarantine_path_transition = "quarantine_path_transition" in payload
    quarantine_path_transition = payload.get("quarantine_path_transition", False)
    owner_present = owner.exists() or owner.is_symlink()
    if (
        not isinstance(operation_id, str)
        or not UUID4_RE.fullmatch(operation_id)
        or not SHA256_RE.fullmatch(str(owner_sha256 or ""))
        or not isinstance(workspace_name, str)
        or (workspace_name and not WORKSPACE_RE.fullmatch(workspace_name))
        or (
            has_quarantine_path_transition
            and not isinstance(quarantine_path_transition, bool)
        )
    ):
        fail(f"case update removal phase identity is invalid: {removal}")
    expected_owner = case_update_owner_payload(
        repo,
        slug,
        workspace_name,
        operation_id,
        quarantine_path_transition,
    )
    if not has_quarantine_path_transition:
        expected_owner.pop("quarantine_path_transition")
    if sha256_bytes(canonical_json_bytes(expected_owner)) != owner_sha256:
        fail(f"case update removal owner digest is inconsistent: {removal}")
    if owner_present:
        owner_payload = validate_case_update_owner(repo, slug)
        if (
            owner_payload["operation_id"] != operation_id
            or owner_payload["workspace"] != workspace_name
            or ("quarantine_path_transition" in owner_payload)
            != has_quarantine_path_transition
            or case_update_quarantine_path_transition(owner_payload)
            != quarantine_path_transition
            or sha256_file(owner) != owner_sha256
        ):
            fail(f"case update removal owner differs: {removal}")
    raw_targets = payload.get("targets")
    expected_targets = case_update_expected_targets(
        repo,
        slug,
        workspace_name,
    )
    normalized_targets: list[dict[str, Any]] = []
    if not isinstance(raw_targets, list) or len(raw_targets) != len(expected_targets):
        fail(f"case update removal phase has invalid targets: {removal}")
    for raw, (expected_key, expected_path) in zip(
        raw_targets,
        expected_targets,
        strict=True,
    ):
        if not isinstance(raw, dict) or set(raw) != {"key", "mode", "path", "sha256"}:
            fail(f"case update removal phase target is invalid: {removal}")
        mode = raw.get("mode")
        digest = raw.get("sha256")
        if (
            raw.get("key") != expected_key
            or raw.get("path") != str(expected_path)
            or not isinstance(mode, int)
            or isinstance(mode, bool)
            or mode < 0
            or mode > 0o777
            or not SHA256_RE.fullmatch(str(digest or ""))
        ):
            fail(f"case update removal phase target identity is invalid: {removal}")
        current, current_mode = require_case_update_target(
            expected_path,
            f"case update removal target {expected_key}",
        )
        if current_mode != mode or sha256_bytes(current) != digest:
            fail(f"case update removal target changed: {expected_path}")
        normalized_targets.append(
            {
                "key": expected_key,
                "mode": mode,
                "path": str(expected_path),
                "sha256": str(digest),
            }
        )
    if (
        disposition not in {"abort", "complete"}
        or not isinstance(device, int)
        or isinstance(device, bool)
        or device < 0
        or not isinstance(inode, int)
        or isinstance(inode, bool)
        or inode <= 0
        or not SHA256_RE.fullmatch(str(fingerprint or ""))
        or payload
        != case_update_remove_payload(
            repo,
            slug,
            str(disposition),
            device,
            inode,
            str(fingerprint),
            normalized_targets,
            operation_id,
            str(owner_sha256),
            workspace_name,
            quarantine_path_transition,
            include_quarantine_path_transition=has_quarantine_path_transition,
        )
    ):
        fail(f"case update removal phase is inconsistent: {removal}")
    transaction_present = transaction.exists() or transaction.is_symlink()
    staging_present = staging.exists() or staging.is_symlink()
    if transaction_present and staging_present:
        fail(f"case update removal has both transaction and staging: {slug}")
    if not owner_present and (transaction_present or staging_present):
        fail(f"case update removal lost its owner before directory removal: {slug}")
    selected = transaction if transaction_present else staging if staging_present else None
    if selected is not None:
        require_private_directory(selected, "case update removal directory")
        details = selected.lstat()
        if details.st_dev != device or details.st_ino != inode:
            fail(f"case update removal identity changed: {selected}")
        if transaction_present and secure_tree_fingerprint(transaction) != fingerprint:
            fail(f"case update transaction changed before removal: {transaction}")
    return payload


def publish_case_update_remove_transaction(
    repo: Path,
    slug: str,
    disposition: str,
) -> dict[str, Any]:
    transaction, _owner = case_update_paths(repo, slug)
    staging, removal = case_update_removal_paths(repo, slug)
    if disposition == "complete":
        current = validate_case_update_transaction(repo, slug)
        if any(case_update_target_state(entry) != "new" for entry in current.entries):
            fail(f"case update transaction is not complete: {slug}")
    elif disposition == "abort":
        validate_case_update_preparation(repo, slug)
    else:
        fail(f"invalid case update removal disposition: {disposition}")
    if staging.exists() or staging.is_symlink():
        fail(f"case update has unowned removal staging: {staging}")
    if removal.exists() or removal.is_symlink():
        fail(f"case update removal phase already exists: {removal}")
    require_private_directory(transaction, "case update removal directory")
    details = transaction.lstat()
    owner_payload = validate_case_update_owner(repo, slug)
    target_records: list[dict[str, Any]] = []
    for key, path in case_update_expected_targets(
        repo,
        slug,
        str(owner_payload["workspace"]),
    ):
        content, mode = require_case_update_target(
            path,
            f"case update removal target {key}",
        )
        target_records.append(
            {
                "key": key,
                "mode": mode,
                "path": str(path),
                "sha256": sha256_bytes(content),
            }
        )
    publish_private_json(
        removal,
        case_update_remove_payload(
            repo,
            slug,
            disposition,
            details.st_dev,
            details.st_ino,
            secure_tree_fingerprint(transaction),
            target_records,
            str(owner_payload["operation_id"]),
            sha256_file(case_update_paths(repo, slug)[1]),
            str(owner_payload["workspace"]),
            case_update_quarantine_path_transition(owner_payload),
            include_quarantine_path_transition=(
                "quarantine_path_transition" in owner_payload
            ),
        ),
        "case update removal phase",
    )
    return validate_case_update_remove_transaction(repo, slug)


def finish_case_update_remove_transaction(repo: Path, slug: str) -> tuple[Path, ...]:
    transaction, owner = case_update_paths(repo, slug)
    staging, removal = case_update_removal_paths(repo, slug)
    payload = validate_case_update_remove_transaction(repo, slug)
    validate_published_case(
        slug,
        repo / "fork-maintenance" / "cases" / slug,
        allow_quarantine_path_transition=(
            payload["disposition"] == "abort"
            and case_update_quarantine_path_transition(payload)
        ),
    )
    workspace_name = str(payload["workspace"])
    if workspace_name:
        workspace = load_workspace(
            repo,
            workspace_name,
            require_host_identity=False,
        )
        allowed_modes = (
            {"patched"}
            if payload["disposition"] == "complete"
            else {"clean", "patched", "reconstruct"}
        )
        if (
            workspace.selection != f"cases/{slug}"
            or workspace.patch_mode not in allowed_modes
        ):
            fail("case update removal workspace is inconsistent")
    if transaction.exists() or transaction.is_symlink():
        try:
            container_payload.rename_no_replace(transaction, staging)
        except FileExistsError as error:
            fail(f"case update removal staging appeared: {staging}: {error}")
        except (container_payload.PayloadError, OSError) as error:
            fail(f"cannot stage case update removal {transaction}: {error}")
        fsync_directory(transaction.parent)
        validate_case_update_remove_transaction(repo, slug)
    removed: list[Path] = []
    if staging.exists() or staging.is_symlink():
        validate_case_update_remove_transaction(repo, slug)
        shutil.rmtree(staging)
        fsync_directory(staging.parent)
        if staging.exists() or staging.is_symlink():
            fail(f"case update removal staging remains: {staging}")
        removed.append(staging)
    validate_case_update_remove_transaction(repo, slug)
    if owner.exists() or owner.is_symlink():
        validate_case_update_owner(repo, slug)
        owner.unlink()
        fsync_directory(owner.parent)
        removed.append(owner)
    validate_case_update_remove_transaction(repo, slug)
    removal.unlink()
    fsync_directory(removal.parent)
    removed.append(removal)
    if any(
        path.exists() or path.is_symlink()
        for path in (transaction, staging, removal, owner)
    ):
        fail(f"case update transaction removal did not complete: {slug}")
    if payload["disposition"] not in {"abort", "complete"}:
        fail(f"case update removal disposition changed: {slug}")
    return tuple(path for path in (staging, removal, owner) if path in removed)


def remove_case_update_transaction(
    repo: Path,
    transaction: CaseUpdateTransaction,
) -> tuple[Path, ...]:
    _staging, removal = case_update_removal_paths(repo, transaction.slug)
    if not removal.exists() and not removal.is_symlink():
        publish_case_update_remove_transaction(repo, transaction.slug, "complete")
    return finish_case_update_remove_transaction(repo, transaction.slug)


def complete_case_update_transaction(repo: Path, slug: str) -> Case:
    transaction = validate_case_update_transaction(repo, slug)
    for entry in transaction.entries:
        publish_case_update_entry(transaction, entry)
    updated = load_case(repo / "fork-maintenance" / "cases" / slug)
    if transaction.workspace:
        workspace = load_workspace(
            repo,
            transaction.workspace,
            require_host_identity=False,
        )
        if workspace.selection != f"cases/{slug}" or workspace.patch_mode != "patched":
            fail("completed case update workspace is inconsistent")
    transaction = validate_case_update_transaction(repo, slug)
    if any(case_update_target_state(entry) != "new" for entry in transaction.entries):
        fail(f"case update transaction did not reach its complete state: {slug}")
    remove_case_update_transaction(repo, transaction)
    return updated


def validate_case_update_preparation(repo: Path, slug: str) -> dict[str, Any]:
    transaction, _owner_path = case_update_paths(repo, slug)
    owner = validate_case_update_owner(repo, slug)
    if transaction.exists() or transaction.is_symlink():
        require_private_directory(transaction, "case update preparation")
        marker = transaction / "transaction.json"
        if marker.exists() or marker.is_symlink():
            fail(f"case update transaction is complete and must be finished: {marker}")
        expected_keys = {
            key for key, _target in case_update_expected_targets(
                repo,
                slug,
                str(owner["workspace"]),
            )
        }
        allowed = {"candidate-lab"}
        for key in expected_keys:
            allowed.update((f"{key}.old", f"{key}.new"))
        for path in transaction.iterdir():
            if path.name not in allowed:
                fail(f"case update preparation contains unexpected staging: {path}")
            if path.name == "candidate-lab":
                secure_tree_fingerprint(path)
            else:
                info = require_cleanup_file(path, "case update preparation payload")
                if stat.S_IMODE(info.st_mode) != 0o600:
                    fail(f"case update preparation payload has an invalid mode: {path}")
    else:
        validate_published_case(
            slug,
            repo / "fork-maintenance" / "cases" / slug,
            allow_quarantine_path_transition=case_update_quarantine_path_transition(
                owner
            ),
        )
        workspace_name = str(owner["workspace"])
        if workspace_name:
            workspace = load_workspace(
                repo,
                workspace_name,
                require_host_identity=False,
            )
            if workspace.selection != f"cases/{slug}":
                fail("case update owner names an inconsistent workspace")
    return owner


def abort_case_update_preparation(repo: Path, slug: str) -> tuple[Path, ...]:
    transaction, owner_path = case_update_paths(repo, slug)
    validate_case_update_preparation(repo, slug)
    if transaction.exists() or transaction.is_symlink():
        publish_case_update_remove_transaction(repo, slug, "abort")
        return finish_case_update_remove_transaction(repo, slug)
    recovered: list[Path] = []
    owner_path.unlink()
    fsync_directory(owner_path.parent)
    recovered.append(owner_path)
    return tuple(recovered)


@contextmanager
def case_update_candidate_lab(
    repo: Path,
    case: Case | DraftCase,
    patch_bytes: bytes,
    manifest_bytes: bytes,
    transaction_directory: Path,
    *,
    source_commit: str | None = None,
) -> Iterator[Path]:
    candidate_lab = transaction_directory / "candidate-lab"
    if candidate_lab.exists() or candidate_lab.is_symlink():
        fail(f"case update candidate lab already exists: {candidate_lab}")
    cases_root = case.directory.parent
    repository_files(cases_root, "case queue")
    candidate_lab.mkdir(mode=0o700)
    try:
        shutil.copytree(cases_root, candidate_lab / "cases", symlinks=True)
        candidate_case = candidate_lab / "cases" / case.slug
        candidate_patch = candidate_case / "fix.patch"
        candidate_manifest = candidate_case / "case.toml"
        candidate_patch.write_bytes(patch_bytes)
        candidate_manifest.write_bytes(manifest_bytes)
        load_case(candidate_case)
        if source_commit is not None:
            if not GIT_SHA_RE.fullmatch(source_commit):
                fail("case update candidate has an invalid source commit")
            source = candidate_lab / "source"
            source.mkdir()
            archive_tree(repo, source_commit, source)
            run(
                (
                    "git",
                    "apply",
                    "--check",
                    "--whitespace=error-all",
                    str(candidate_patch),
                ),
                cwd=source,
            )
            run(
                (
                    "git",
                    "apply",
                    "--whitespace=error-all",
                    str(candidate_patch),
                ),
                cwd=source,
            )
            run(
                ("git", "apply", "--reverse", "--check", str(candidate_patch)),
                cwd=source,
            )
        yield candidate_lab
    finally:
        if candidate_lab.exists() and not candidate_lab.is_symlink():
            shutil.rmtree(candidate_lab)
            fsync_directory(transaction_directory)


def workspace_update_payloads(
    repo: Path,
    case: Case | DraftCase,
    patch_bytes: bytes,
    manifest_bytes: bytes,
    workspace: Workspace,
    transaction_directory: Path,
) -> tuple[tuple[str, Path, bytes, bytes, int], ...]:
    expected_modes = (
        {"clean"} if isinstance(case, DraftCase) else {"patched", "reconstruct"}
    )
    if (
        workspace.selection != f"cases/{case.slug}"
        or workspace.patch_mode not in expected_modes
    ):
        fail("case update workspace is inconsistent")
    with case_update_candidate_lab(
        repo,
        case,
        patch_bytes,
        manifest_bytes,
        transaction_directory,
        source_commit=workspace.source_commit,
    ) as candidate_lab:
        current_resolution = selection_resolution(
            repo,
            workspace.source_commit,
            workspace.selection,
            lab_root=candidate_lab,
            scratch_directory=candidate_lab / "resolution-source",
        )
    if (
        current_resolution.get("source_commit") != workspace.source_commit
        or current_resolution.get("selection") != workspace.selection
        or not SHA256_RE.fullmatch(str(current_resolution.get("selection_sha256", "")))
        or not SHA256_RE.fullmatch(str(current_resolution.get("resolution_sha256", "")))
    ):
        fail("completed case selection resolution is inconsistent")
    resolution_path = workspace_resolution_path(workspace.directory)
    metadata_path = workspace_metadata_path(workspace.directory)
    resolution_original, resolution_mode = require_case_update_target(
        resolution_path,
        "workspace selection resolution",
    )
    metadata_original, metadata_mode = require_case_update_target(
        metadata_path,
        "workspace metadata",
    )
    try:
        metadata = json.loads(metadata_original.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        fail(f"workspace metadata is invalid: {error}")
    if not isinstance(metadata, dict):
        fail("workspace metadata must be a JSON object")
    metadata["selection_sha256"] = current_resolution["selection_sha256"]
    metadata["resolution_sha256"] = current_resolution["resolution_sha256"]
    metadata["patch_mode"] = "patched"
    return (
        (
            "workspace-resolution",
            resolution_path,
            resolution_original,
            canonical_json_bytes(current_resolution),
            resolution_mode,
        ),
        (
            "workspace-metadata",
            metadata_path,
            metadata_original,
            canonical_json_bytes(metadata),
            metadata_mode,
        ),
    )


def atomic_update_case_files(
    repo: Path,
    case: Case | DraftCase,
    patch_bytes: bytes,
    manifest_text: str,
    *,
    expected_patch_bytes: bytes,
    expected_manifest_bytes: bytes,
    workspace: Workspace | None = None,
    verify_source_commit: str | None = None,
) -> Case:
    manifest_bytes = manifest_text.encode("utf-8")
    quarantine_path_transition = is_quarantine_path_transition(case)
    expected_case_directory = repo / "fork-maintenance" / "cases" / case.slug
    if case.directory != expected_case_directory:
        fail(f"case update escaped its repository case directory: {case.directory}")
    artifact_boundary_check(repo)
    with case_update_lock(repo):
        root = case_updates_root(repo, create=True)
        unresolved = sorted(path for path in root.iterdir() if path.name != ".lifecycle.lock")
        if unresolved:
            fail(
                "case update state must be recovered before another update:\n"
                + "\n".join(f"  {path}" for path in unresolved)
            )
        patch_original, patch_mode = require_case_update_target(
            case.patch,
            "case patch",
        )
        manifest_original, manifest_mode = require_case_update_target(
            case.manifest,
            "case manifest",
        )
        if (
            patch_original != expected_patch_bytes
            or manifest_original != expected_manifest_bytes
        ):
            fail("case changed while its update transaction was being prepared")
        workspace_name = workspace.name if workspace is not None else ""
        require_case_update_filesystem(
            root,
            case_update_expected_targets(repo, case.slug, workspace_name),
        )
        operation_id = str(uuid.uuid4())
        transaction_directory, owner_path = case_update_paths(repo, case.slug)
        publish_private_json(
            owner_path,
            case_update_owner_payload(
                repo,
                case.slug,
                workspace_name,
                operation_id,
                quarantine_path_transition,
            ),
            "case update owner",
        )
        transaction_published = False
        try:
            transaction_directory.mkdir(mode=0o700)
            fsync_directory(root)
            values: list[tuple[str, Path, bytes, bytes, int]] = [
                ("case-patch", case.patch, patch_original, patch_bytes, patch_mode),
                (
                    "case-manifest",
                    case.manifest,
                    manifest_original,
                    manifest_bytes,
                    manifest_mode,
                ),
            ]
            if workspace is not None:
                values.extend(
                    workspace_update_payloads(
                        repo,
                        case,
                        patch_bytes,
                        manifest_bytes,
                        workspace,
                        transaction_directory,
                    )
                )
            else:
                with case_update_candidate_lab(
                    repo,
                    case,
                    patch_bytes,
                    manifest_bytes,
                    transaction_directory,
                    source_commit=verify_source_commit,
                ):
                    pass
            marker_entries: list[dict[str, Any]] = []
            for key, target, old_bytes, new_bytes, mode in values:
                old_name = f"{key}.old"
                new_name = f"{key}.new"
                try:
                    background_job.publish_bytes(transaction_directory / old_name, old_bytes)
                    background_job.publish_bytes(transaction_directory / new_name, new_bytes)
                except (background_job.BackgroundJobError, OSError) as error:
                    fail(f"cannot publish case update payload for {key}: {error}")
                marker_entries.append(
                    {
                        "key": key,
                        "new_mode": mode,
                        "new_payload": new_name,
                        "new_sha256": sha256_bytes(new_bytes),
                        "old_mode": mode,
                        "old_payload": old_name,
                        "old_sha256": sha256_bytes(old_bytes),
                        "target": str(target),
                    }
                )
            marker = case_update_owner_payload(
                repo,
                case.slug,
                workspace_name,
                operation_id,
                quarantine_path_transition,
            )
            marker["entries"] = marker_entries
            publish_private_json(
                transaction_directory / "transaction.json",
                marker,
                "case update transaction",
            )
            transaction_published = True
            return complete_case_update_transaction(repo, case.slug)
        except BaseException:
            if not transaction_published:
                abort_case_update_preparation(repo, case.slug)
            raise


def update_case_patch(
    repo: Path,
    case: Case | DraftCase,
    *,
    allow_path_change: bool = False,
) -> Case:
    verify_repo(repo)
    require_non_master(repo)
    expected_patch_bytes = case.patch.read_bytes()
    expected_manifest_bytes = case.manifest.read_bytes()
    if untracked_names(repo):
        fail(f"candidate contains untracked files: {untracked_names(repo)}")
    if unstaged_names(repo):
        fail(f"candidate contains unstaged changes: {unstaged_names(repo)}")
    names = tuple(sorted(staged_names(repo)))
    if not names:
        fail("candidate has no staged source changes")
    for name in names:
        safe_relative_path(name, case.manifest)
    if (
        isinstance(case, Case)
        and names != tuple(sorted(case.paths))
        and not allow_path_change
    ):
        fail(f"staged paths {names} do not match manifest paths {tuple(sorted(case.paths))}")
    base = sync_repo(repo)
    require_patch_branch(repo, base)
    require_source_baseline(repo, base, names)
    if git(repo, "diff", "--cached", "--check", check=False).returncode:
        fail("staged candidate fails git diff --cached --check")
    patch_bytes = git(
        repo,
        "diff",
        "--cached",
        "--binary",
        "--full-index",
        text=False,
    ).stdout
    if not patch_bytes:
        fail("candidate diff is empty")
    digest = sha256_bytes(patch_bytes)
    try:
        original = expected_manifest_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        fail(f"cannot decode case manifest {case.manifest}: {error}")
    manifest = updated_manifest_text(
        original,
        digest=digest,
        paths=names,
        draft=isinstance(case, DraftCase),
    )
    return atomic_update_case_files(
        repo,
        case,
        patch_bytes,
        manifest,
        expected_patch_bytes=expected_patch_bytes,
        expected_manifest_bytes=expected_manifest_bytes,
        verify_source_commit=base,
    )


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

    origin = git(repo, "remote", "get-url", "origin").stdout.strip()
    if normalize_url(origin) != normalize_url(FORK_URL):
        fail(f"origin has unexpected URL in CI: {origin}")
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


def embedded_develop_state(repo: Path, purpose: str) -> IsolatedState:
    """Locate the immutable source boundary already embedded in ``develop``."""
    verify_repo(repo, ("origin",))
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
            f"{purpose} refuses host Xpra source changes; only fork control paths may "
            f"be dirty: {unexpected_dirty}"
        )

    source_tip = cached_master(repo, "origin")
    merge_base = git(repo, "merge-base", "--all", source_tip, head, check=False)
    source_commits = tuple(merge_base.stdout.splitlines())
    if (
        merge_base.returncode
        or len(source_commits) != 1
        or not GIT_SHA_RE.fullmatch(source_commits[0])
    ):
        fail(
            f"{INTEGRATION_BRANCH} and cached origin/{BASE_BRANCH} "
            "have no single usable history boundary"
        )
    source_commit = source_commits[0]
    committed = downstream_committed_paths(repo, source_commit, head)
    unexpected_committed = [path for path in committed if not allowed_develop_path(path)]
    if unexpected_committed:
        fail(
            "develop contains committed Xpra source changes outside the patch queue: "
            f"{unexpected_committed}"
        )
    merges = git(repo, "rev-list", "--merges", f"{source_commit}..{head}").stdout.splitlines()
    if merges:
        fail(f"develop contains fork-side merge commits: {merges}")
    if (
        current_branch(repo) != branch
        or rev_parse(repo, "HEAD") != head
        or cached_master(repo, "origin") != source_tip
        or porcelain(repo) != status
    ):
        fail(
            "repository branch, HEAD, cached origin/master, or worktree changed "
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
    return embedded_develop_state(repo, "CI checkout")


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
            "DEB source discovery refuses host Xpra source changes; only fork "
            f"control paths may be dirty: {unexpected_dirty}"
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
    touched = downstream_committed_paths(repo, source_commit, head)
    unexpected_committed = [path for path in touched if not allowed_develop_path(path)]
    if unexpected_committed:
        fail(
            "checkout contains committed Xpra source changes outside the patch queue: "
            f"{unexpected_committed}"
        )
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


def downstream_committed_paths(repo: Path, base: str, head: str) -> tuple[str, ...]:
    """Return every path touched by first-parent downstream history."""
    return tuple(
        sorted(
            {
                path
                for path in git(
                    repo,
                    "log",
                    "--first-parent",
                    "--format=",
                    "--name-only",
                    f"{base}..{head}",
                ).stdout.splitlines()
                if path
            }
        )
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
    return embedded_develop_state(repo, "isolated patch work")


def require_workspace_name(name: str) -> str:
    if not WORKSPACE_RE.fullmatch(name):
        fail("WORKSPACE must contain only letters, digits, dot, underscore, and dash")
    return name


def workspace_root(repo: Path) -> Path:
    return repo / ".artifacts" / "fork-maintenance" / "upstream-tests" / "workspaces"


def require_private_directory(path: Path, description: str) -> None:
    if path.is_symlink() or not path.is_dir():
        fail(f"{description} is missing or unsafe: {path}")
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o7777 != 0o700:
        fail(f"{description} must be owned by this user with mode 0700: {path}")


def prepare_workspace_root(repo: Path) -> Path:
    run((sys.executable, str(PRIVATE_STATE_TOOL), "--project-root", str(repo)))
    root = workspace_root(repo)
    require_private_directory(root, "workspace root")
    return root


def workspace_lifecycle_lock_path(repo: Path) -> Path:
    return workspace_root(repo) / ".lifecycle.lock"


@contextmanager
def workspace_lifecycle_lock(repo: Path) -> Iterator[None]:
    prepare_workspace_root(repo)
    lock = workspace_lifecycle_lock_path(repo)
    descriptor = open_retained_lifecycle_lock(lock, "workspace lifecycle lock")
    try:
        yield
    finally:
        os.close(descriptor)


def workspace_create_paths(repo: Path, name: str) -> tuple[Path, Path, Path]:
    root = prepare_workspace_root(repo)
    return (
        root / name,
        root / f".{name}.create.partial",
        root / f".{name}.create.owner.json",
    )


def workspace_remove_paths(repo: Path, name: str) -> tuple[Path, Path, Path]:
    root = prepare_workspace_root(repo)
    return (
        root / name,
        root / f".{name}.remove",
        root / f".{name}.remove.owner.json",
    )


def workspace_fingerprint_root(repo: Path) -> Path:
    return prepare_private_subdirectory(
        repo,
        "workspace-fingerprints",
        "workspace fingerprint scratch root",
    )


def workspace_fingerprint_path(repo: Path, name: str) -> Path:
    return workspace_fingerprint_root(repo) / f"{name}.fingerprint"


def workspace_fingerprint_paths(repo: Path, name: str) -> tuple[Path, Path, Path, Path]:
    root = workspace_fingerprint_root(repo)
    return (
        root / f"{name}.fingerprint",
        root / f"{name}.fingerprint.owner.json",
        root / f".{name}.fingerprint.remove",
        root / f"{name}.fingerprint.remove.json",
    )


def workspace_fingerprint_owner_payload(
    repo: Path,
    name: str,
    operation_id: str,
) -> dict[str, Any]:
    scratch, owner, staging, removal = workspace_fingerprint_paths(repo, name)
    return {
        "kind": "workspace-fingerprint",
        "name": name,
        "operation_id": operation_id,
        "owner": WORKSPACE_FINGERPRINT_OWNER,
        "owner_record": str(owner),
        "removal": str(removal),
        "repository": str(repo.resolve()),
        "schema": 2,
        "scratch": str(scratch),
        "staging": str(staging),
        "workspace": str(workspace_root(repo) / name),
    }


def validate_workspace_fingerprint_owner(repo: Path, name: str) -> dict[str, Any]:
    _scratch, owner, _staging, _removal = workspace_fingerprint_paths(repo, name)
    payload = load_cleanup_json(owner, "workspace fingerprint owner")
    operation_id = payload.get("operation_id")
    if (
        not isinstance(operation_id, str)
        or not UUID4_RE.fullmatch(operation_id)
        or payload != workspace_fingerprint_owner_payload(repo, name, operation_id)
    ):
        fail(f"workspace fingerprint owner is inconsistent: {owner}")
    return payload


def validate_workspace_create_marker(repo: Path, name: str) -> dict[str, Any]:
    target, partial, marker = workspace_create_paths(repo, name)
    payload = load_cleanup_json(marker, "workspace creation owner")
    expected = {
        "kind": "workspace-create",
        "name": name,
        "owner": WORKSPACE_CREATE_OWNER,
        "partial": str(partial),
        "schema": 1,
        "target": str(target),
    }
    if (
        set(payload) != set(expected) | {"operation_id"}
        or any(payload.get(key) != value for key, value in expected.items())
        or not UUID4_RE.fullmatch(str(payload.get("operation_id", "")))
    ):
        fail(f"workspace creation owner is inconsistent: {marker}")
    return payload


def validate_workspace_fingerprint_scratch(
    repo: Path,
    name: str,
    scratch: Path,
) -> None:
    expected_scratch, _owner, _staging, _removal = workspace_fingerprint_paths(
        repo,
        name,
    )
    if scratch != expected_scratch:
        fail("workspace fingerprint scratch escaped its exact root")
    validate_workspace_fingerprint_owner(repo, name)
    require_private_directory(scratch, "workspace fingerprint scratch")
    secure_tree_fingerprint(scratch)
    entries = {entry.name: entry for entry in scratch.iterdir()}
    if set(entries).difference({"index", "index.lock"}):
        fail(f"workspace fingerprint scratch has an unexpected entry set: {scratch}")
    for filename in ("index", "index.lock"):
        path = entries.get(filename)
        if path is not None:
            info = path.lstat()
            mode = stat.S_IMODE(info.st_mode)
            if (
                path.is_symlink()
                or not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or mode & 0o600 != 0o600
                or mode & 0o111
                or mode & 0o002
            ):
                fail(f"workspace fingerprint index is unsafe: {path}")


def _recover_workspace_state_locked(repo: Path, name: str) -> tuple[Path, ...]:
    """Recover exact marker-backed creation, removal, or fingerprint state."""
    require_workspace_name(name)
    root = prepare_workspace_root(repo)
    target, partial, marker = workspace_create_paths(repo, name)
    remove_target, remove_staging, remove_marker = workspace_remove_paths(repo, name)
    if remove_target != target:
        fail("workspace lifecycle paths are inconsistent")
    allowed = {
        partial,
        marker,
        remove_staging,
        remove_marker,
        workspace_lifecycle_lock_path(repo),
    }
    unexpected = sorted(
        path
        for path in root.iterdir()
        if path.name.startswith(f".{name}.") and path not in allowed
    )
    if unexpected:
        fail(f"workspace has unrecognized partial state: {unexpected}")

    scratch, fingerprint_owner, fingerprint_staging, fingerprint_removal = (
        workspace_fingerprint_paths(repo, name)
    )
    create_state_present = any(
        path.exists() or path.is_symlink() for path in (partial, marker)
    )
    remove_marker_present = remove_marker.exists() or remove_marker.is_symlink()
    remove_staging_present = remove_staging.exists() or remove_staging.is_symlink()
    fingerprint_state = (
        scratch,
        fingerprint_owner,
        fingerprint_staging,
        fingerprint_removal,
    )
    fingerprint_present = any(
        path.exists() or path.is_symlink() for path in fingerprint_state
    )
    if remove_staging_present and not remove_marker_present:
        fail(f"workspace has an unowned removal staging: {remove_staging}")
    if remove_marker_present:
        if create_state_present or fingerprint_present:
            fail(f"workspace removal conflicts with another lifecycle state: {name}")
        return finish_workspace_remove_transaction(repo, name)
    if create_state_present and fingerprint_present:
        fail(f"workspace creation conflicts with fingerprint state: {name}")
    if (fingerprint_staging.exists() or fingerprint_staging.is_symlink()) and not (
        fingerprint_removal.exists() or fingerprint_removal.is_symlink()
    ):
        fail(f"workspace fingerprint has unowned removal staging: {fingerprint_staging}")
    fingerprint_owner_present = (
        fingerprint_owner.exists() or fingerprint_owner.is_symlink()
    )
    fingerprint_removal_only = (
        (fingerprint_removal.exists() or fingerprint_removal.is_symlink())
        and not (scratch.exists() or scratch.is_symlink())
        and not (fingerprint_staging.exists() or fingerprint_staging.is_symlink())
    )
    if fingerprint_present and not fingerprint_owner_present and not fingerprint_removal_only:
        fail(f"workspace has unowned fingerprint state: {name}")

    recovered: list[Path] = []
    marker_present = marker.exists() or marker.is_symlink()
    partial_present = partial.exists() or partial.is_symlink()
    if partial_present and not marker_present:
        fail(f"workspace has an unowned creation partial: {partial}")
    if marker_present:
        validate_workspace_create_marker(repo, name)
        if partial_present and (target.exists() or target.is_symlink()):
            fail(f"workspace creation has both partial and published state: {name}")
        if partial_present:
            secure_tree_fingerprint(partial)
            shutil.rmtree(partial)
            recovered.append(partial)
        elif target.exists() or target.is_symlink():
            load_workspace(repo, name, require_host_identity=False)
        marker.unlink()
        recovered.append(marker)

    if fingerprint_removal_only:
        recovered.extend(
            finish_workspace_fingerprint_remove_transaction(repo, name)
        )
    elif fingerprint_owner_present:
        validate_workspace_fingerprint_owner(repo, name)
        if fingerprint_removal.exists() or fingerprint_removal.is_symlink():
            recovered.extend(
                finish_workspace_fingerprint_remove_transaction(repo, name)
            )
        elif scratch.exists() or scratch.is_symlink():
            publish_workspace_fingerprint_remove_transaction(repo, name)
            recovered.extend(
                finish_workspace_fingerprint_remove_transaction(repo, name)
            )
        elif fingerprint_staging.exists() or fingerprint_staging.is_symlink():
            fail(
                "workspace fingerprint staging has no durable removal phase: "
                f"{fingerprint_staging}"
            )
        else:
            fingerprint_owner.unlink()
            fsync_directory(fingerprint_owner.parent)
            recovered.append(fingerprint_owner)
    if not recovered:
        fail(f"workspace has no recoverable partial state: {name}")
    return tuple(recovered)


def recover_workspace_state(repo: Path, name: str) -> tuple[Path, ...]:
    """Recover one workspace while excluding cleanup and other mutations."""
    with workspace_lifecycle_lock(repo), case_update_lock(repo):
        _target, _staging, removal = workspace_remove_paths(repo, name)
        if removal.exists() or removal.is_symlink():
            require_workspace_not_bound_to_case_update(repo, name)
        return _recover_workspace_state_locked(repo, name)


def write_private_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def publish_private_json(path: Path, value: object, description: str) -> None:
    """Durably publish one complete, immutable private owner record."""
    if not isinstance(value, dict):
        fail(f"{description} must be a JSON object")
    try:
        background_job.publish_json(path, value)
    except (background_job.BackgroundJobError, OSError) as error:
        fail(f"cannot publish {description} {path}: {error}")


def prepare_private_subdirectory(repo: Path, relative: str, description: str) -> Path:
    run((sys.executable, str(PRIVATE_STATE_TOOL), "--project-root", str(repo)))
    root = repo / ".artifacts" / "fork-maintenance" / relative
    if not root.exists() and not root.is_symlink():
        root.mkdir(mode=0o700)
    require_private_directory(root, description)
    return root


def draft_selection_resolution(
    case: DraftCase,
    source_commit: str,
    selection: str,
) -> dict[str, Any]:
    digest = hashlib.sha256()
    for path in (case.manifest, case.patch):
        digest.update(path.relative_to(AUTOMATION_ROOT).as_posix().encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    payload: dict[str, Any] = {
        "schema": 1,
        "source_commit": source_commit,
        "selection": selection,
        "selection_sha256": digest.hexdigest(),
        "declared_cases": [case.slug],
        "base_dependencies": [],
        "patches": [],
        "applied_cases": [],
        "already_present_cases": [],
        "draft_case": case.slug,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["resolution_sha256"] = hashlib.sha256(canonical).hexdigest()
    return payload


def workspace_index_names(source: Path, base_tree: str) -> tuple[str, ...]:
    return tuple(
        line
        for line in git(source, "diff", "--cached", "--name-only", base_tree, "--").stdout.splitlines()
        if line
    )


def workspace_metadata_path(directory: Path) -> Path:
    return directory / "workspace.json"


def workspace_resolution_path(directory: Path) -> Path:
    return directory / "selection-resolution.json"


def load_workspace(
    repo: Path,
    name: str,
    *,
    require_host_identity: bool = True,
) -> Workspace:
    require_workspace_name(name)
    root = prepare_workspace_root(repo)
    directory = root / name
    require_private_directory(directory, "workspace")
    metadata_path = workspace_metadata_path(directory)
    resolution_path = workspace_resolution_path(directory)
    for path, description in (
        (metadata_path, "workspace metadata"),
        (resolution_path, "workspace resolution"),
    ):
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o7777 != 0o600:
            fail(f"{description} is missing or unsafe: {path}")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        resolution = json.loads(resolution_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        fail(f"workspace metadata is invalid: {error}")
    if not isinstance(metadata, dict) or not isinstance(resolution, dict):
        fail("workspace metadata must be JSON objects")
    expected_strings = {
        "owner": WORKSPACE_OWNER,
        "name": name,
    }
    if metadata.get("schema") != 1 or any(
        metadata.get(key) != value for key, value in expected_strings.items()
    ):
        fail("workspace identity is inconsistent")
    fields = (
        "branch",
        "head",
        "source_commit",
        "base_tree",
        "selection",
        "selection_sha256",
        "resolution_sha256",
        "patch_mode",
    )
    if not all(isinstance(metadata.get(field), str) for field in fields):
        fail("workspace metadata fields are invalid")
    workspace = Workspace(
        name=name,
        directory=directory,
        source=directory / "source",
        branch=metadata["branch"],
        head=metadata["head"],
        source_commit=metadata["source_commit"],
        base_tree=metadata["base_tree"],
        selection=metadata["selection"],
        selection_sha256=metadata["selection_sha256"],
        resolution_sha256=metadata["resolution_sha256"],
        patch_mode=metadata["patch_mode"],
    )
    if (
        not GIT_SHA_RE.fullmatch(workspace.head)
        or not GIT_SHA_RE.fullmatch(workspace.source_commit)
        or not GIT_SHA_RE.fullmatch(workspace.base_tree)
        or not SELECTION_RE.fullmatch(workspace.selection)
        or not SHA256_RE.fullmatch(workspace.selection_sha256)
        or not SHA256_RE.fullmatch(workspace.resolution_sha256)
        or workspace.patch_mode not in WORKSPACE_PATCH_MODES
    ):
        fail("workspace metadata values are invalid")
    require_private_directory(workspace.source, "workspace source")
    if not (workspace.source / ".git").is_dir() or (workspace.source / ".git").is_symlink():
        fail("workspace source Git metadata is missing or unsafe")
    if git(workspace.source, "cat-file", "-e", f"{workspace.base_tree}^{{tree}}", check=False).returncode:
        fail("workspace base tree is missing")
    if (
        resolution.get("source_commit") != workspace.source_commit
        or resolution.get("selection") != workspace.selection
        or resolution.get("selection_sha256") != workspace.selection_sha256
        or resolution.get("resolution_sha256") != workspace.resolution_sha256
    ):
        fail("workspace selection resolution is inconsistent")
    if require_host_identity and (
        current_branch(repo) != workspace.branch or rev_parse(repo, "HEAD") != workspace.head
    ):
        fail("host branch or HEAD changed after workspace creation")
    return workspace


def _create_workspace_locked(
    repo: Path,
    name: str,
    selection: str,
    patch_mode: str,
) -> Workspace:
    require_workspace_name(name)
    if not SELECTION_RE.fullmatch(selection):
        fail(f"invalid workspace selection: {selection!r}")
    if patch_mode not in WORKSPACE_PATCH_MODES:
        fail(f"invalid workspace patch mode: {patch_mode!r}")
    state = isolated_start_check(repo)
    draft: DraftCase | None = None
    reconstruct = patch_mode == "reconstruct"
    if reconstruct and not selection.startswith("cases/"):
        fail("reconstruction requires exactly one completed case selection")
    if selection.startswith("cases/"):
        case_slug = selection.split("/", 1)[1]
        case_directory = CASES_ROOT / case_slug
        if case_directory.is_dir() and case_is_draft(case_directory):
            draft = get_case(case_slug, allow_draft=True)
            if not isinstance(draft, DraftCase):
                fail(f"case {case_slug} changed while creating its draft workspace")
            if patch_mode != "clean":
                fail("a draft case workspace must start with PATCH_MODE=clean")
            cases: tuple[Case, ...] = ()
            resolution = draft_selection_resolution(draft, state.source_commit, selection)
        else:
            cases = selected_cases(selection)
            if reconstruct:
                if len(cases) != 1 or cases[0].slug != case_slug:
                    fail("reconstruction requires exactly one completed case selection")
                resolution = reconstruction_selection_resolution(
                    repo,
                    state.source_commit,
                    selection,
                    cases[0],
                )
            else:
                resolution = selection_resolution(repo, state.source_commit, selection)
    else:
        cases = selected_cases(selection)
        resolution = selection_resolution(repo, state.source_commit, selection)
    target, temporary, marker = workspace_create_paths(repo, name)
    _remove_target, remove_staging, remove_marker = workspace_remove_paths(repo, name)
    fingerprint_state = workspace_fingerprint_paths(repo, name)
    if target.exists() or target.is_symlink():
        fail(f"workspace already exists: {name}")
    if (
        temporary.exists()
        or temporary.is_symlink()
        or marker.exists()
        or marker.is_symlink()
        or remove_staging.exists()
        or remove_staging.is_symlink()
        or remove_marker.exists()
        or remove_marker.is_symlink()
        or any(path.exists() or path.is_symlink() for path in fingerprint_state)
    ):
        fail(
            f"workspace {name} has incomplete lifecycle state; run workspace-recover"
        )
    operation_id = str(uuid.uuid4())
    publish_private_json(
        marker,
        {
            "kind": "workspace-create",
            "name": name,
            "operation_id": operation_id,
            "owner": WORKSPACE_CREATE_OWNER,
            "partial": str(temporary),
            "schema": 1,
            "target": str(target),
        },
        "workspace creation owner",
    )
    try:
        temporary.mkdir(mode=0o700)
        os.chmod(temporary, 0o700)
        source = temporary / "source"
        run(
            (
                "git",
                "clone",
                "--quiet",
                "--no-hardlinks",
                "--no-checkout",
                str(repo),
                str(source),
            )
        )
        os.chmod(source, 0o700)
        git(source, "checkout", "--quiet", "--detach", state.source_commit)
        git(source, "remote", "remove", "origin")
        if rev_parse(source, "HEAD") != state.source_commit or porcelain(source):
            fail("isolated checkout does not reproduce the clean master commit")
        base_tree = rev_parse(source, f"{state.source_commit}^{{tree}}")
        committed_tree = rev_parse(repo, f"{state.source_commit}^{{tree}}")
        if base_tree != committed_tree:
            fail("isolated archive does not reproduce the complete master tree")

        by_slug = {case.slug: case for case in cases}
        expected: set[str] = set()
        entries = resolution.get("patches")
        if not isinstance(entries, list):
            fail("selection resolution has no patch series")
        for entry in entries:
            if not isinstance(entry, dict):
                fail("selection resolution patch entry is invalid")
            slug = entry.get("case")
            status = entry.get("status")
            allowed_status = {"diverged"} if reconstruct else {"apply", "already-present"}
            if (
                not isinstance(slug, str)
                or slug not in by_slug
                or status not in allowed_status
            ):
                fail("selection resolution patch identity is invalid")
            if status != "apply" or patch_mode in {"clean", "reconstruct"}:
                continue
            case = by_slug[slug]
            arguments = ["apply", "--index", "--whitespace=error-all"]
            selected_paths = case.paths
            if patch_mode == "tests-only":
                arguments.append("--include=tests/**")
                selected_paths = tuple(path for path in case.paths if path.startswith("tests/"))
                if not selected_paths:
                    fail(f"case {slug} has no test paths for tests-only mode")
            git(source, *arguments, "--check", str(case.patch))
            git(source, *arguments, str(case.patch))
            reverse = ["apply", "--reverse", "--check", "--whitespace=error-all"]
            if patch_mode == "tests-only":
                reverse.append("--include=tests/**")
            git(source, *reverse, str(case.patch))
            expected.update(selected_paths)
        actual = set(workspace_index_names(source, base_tree))
        if actual != expected:
            fail(
                f"workspace paths {sorted(actual)} do not match selected paths "
                f"{sorted(expected)}"
            )
        if git(source, "diff", "--cached", "--check", base_tree, "--", check=False).returncode:
            fail("workspace candidate fails git diff --cached --check")

        resolution_path = workspace_resolution_path(temporary)
        write_private_json(resolution_path, resolution)
        metadata = {
            "schema": 1,
            "owner": WORKSPACE_OWNER,
            "name": name,
            "branch": state.branch,
            "head": state.head,
            "source_commit": state.source_commit,
            "fork_base": state.fork_base,
            "source_in_head": state.source_in_head,
            "base_tree": base_tree,
            "selection": selection,
            "selection_sha256": resolution["selection_sha256"],
            "resolution_sha256": resolution["resolution_sha256"],
            "patch_mode": patch_mode,
            "host_worktree_sha256": sha256_bytes(state.worktree_status.encode()),
        }
        write_private_json(workspace_metadata_path(temporary), metadata)
        os.replace(temporary, target)
        marker.unlink()
        if (
            current_branch(repo) != state.branch
            or rev_parse(repo, "HEAD") != state.head
            or porcelain(repo) != state.worktree_status
        ):
            fail("host branch, HEAD, or worktree changed while creating workspace")
    except BaseException:
        if temporary.exists() and not temporary.is_symlink():
            shutil.rmtree(temporary)
        marker.unlink(missing_ok=True)
        raise
    return load_workspace(repo, name)


def create_workspace(
    repo: Path,
    name: str,
    selection: str,
    patch_mode: str,
) -> Workspace:
    """Create one isolated workspace under its retained lifecycle lock."""
    with workspace_lifecycle_lock(repo):
        return _create_workspace_locked(repo, name, selection, patch_mode)


def workspace_candidate_names(workspace: Workspace) -> tuple[str, ...]:
    return tuple(sorted(workspace_index_names(workspace.source, workspace.base_tree)))


def _stage_workspace_locked(
    repo: Path,
    name: str,
    *,
    allow_path_change: bool = False,
) -> tuple[str, ...]:
    workspace = load_workspace(repo, name)
    if not workspace.selection.startswith("cases/"):
        fail("only an atomic case workspace can be staged for patch update")
    slug = workspace.selection.split("/", 1)[1]
    case = get_case(
        slug,
        allow_draft=True,
        allow_quarantine_path_transition=allow_path_change,
    )
    candidate = set(workspace_candidate_names(workspace))
    candidate.update(unstaged_names(workspace.source))
    candidate.update(untracked_names(workspace.source))
    if not candidate:
        fail("workspace candidate is empty")
    for path in candidate:
        safe_relative_path(path, case.manifest)
    expected_paths = set(case.paths) if isinstance(case, Case) else set()
    unexpected = candidate.difference(expected_paths)
    if isinstance(case, Case) and unexpected and not allow_path_change:
        fail(
            f"workspace paths {sorted(candidate)} do not match manifest paths "
            f"{tuple(sorted(case.paths))}"
        )
    git(workspace.source, "add", "-f", "-A", "--", *sorted(candidate))
    if unstaged_names(workspace.source) or untracked_names(workspace.source):
        fail("workspace still contains unstaged or untracked files")
    names = workspace_candidate_names(workspace)
    if not names:
        fail("workspace candidate is empty after staging")
    if isinstance(case, Case) and case.kind == "test-quarantine":
        expected = quarantine_module_paths(case.quarantined_tests)
        if names != expected:
            fail(f"workspace quarantine paths {names} do not match modules {expected}")
    if git(
        workspace.source,
        "diff",
        "--cached",
        "--check",
        workspace.base_tree,
        "--",
        check=False,
    ).returncode:
        fail("workspace candidate fails git diff --cached --check")
    return names


def stage_workspace(
    repo: Path,
    name: str,
    *,
    allow_path_change: bool = False,
) -> tuple[str, ...]:
    """Stage one candidate while excluding cleanup and other workspace operations."""
    with workspace_lifecycle_lock(repo):
        return _stage_workspace_locked(
            repo,
            name,
            allow_path_change=allow_path_change,
        )


def _update_case_from_workspace_locked(
    repo: Path,
    name: str,
    *,
    allow_path_change: bool = False,
) -> Case:
    workspace = load_workspace(repo, name)
    if workspace.patch_mode == "tests-only":
        fail("tests-only workspaces cannot replace a complete case patch")
    if not workspace.selection.startswith("cases/"):
        fail("only an atomic case workspace can update a case")
    slug = workspace.selection.split("/", 1)[1]
    case = get_case(
        slug,
        allow_draft=True,
        allow_quarantine_path_transition=allow_path_change,
    )
    expected_patch_bytes = case.patch.read_bytes()
    expected_manifest_bytes = case.manifest.read_bytes()
    resolution = json.loads(
        workspace_resolution_path(workspace.directory).read_text(encoding="utf-8")
    )
    entries = resolution.get("patches")
    if isinstance(case, DraftCase):
        if entries != [] or resolution.get("draft_case") != case.slug:
            fail("draft workspace resolution is inconsistent")
    elif workspace.patch_mode == "reconstruct":
        expected_resolution = reconstruction_selection_resolution(
            repo,
            workspace.source_commit,
            workspace.selection,
            case,
        )
        if resolution != expected_resolution:
            fail("reconstruction workspace provenance is inconsistent")
    else:
        if not isinstance(entries, list) or len(entries) != 1:
            fail("atomic workspace resolution is inconsistent")
        entry = entries[0]
        if not isinstance(entry, dict) or entry.get("patch_sha256") != case.patch_sha256:
            fail("case patch changed after workspace creation")
    if unstaged_names(workspace.source) or untracked_names(workspace.source):
        fail("workspace contains unstaged or untracked files; run workspace-stage")
    names = workspace_candidate_names(workspace)
    if not names:
        fail("workspace candidate is empty; retire an upstream-complete case instead")
    for path in names:
        safe_relative_path(path, case.manifest)
    if (
        isinstance(case, Case)
        and names != tuple(sorted(case.paths))
        and not allow_path_change
    ):
        fail(f"workspace paths {names} do not match manifest paths {tuple(sorted(case.paths))}")
    if isinstance(case, Case) and case.kind == "test-quarantine":
        expected = quarantine_module_paths(case.quarantined_tests)
        if names != expected:
            fail(f"workspace quarantine paths {names} do not match modules {expected}")
    patch_bytes = git(
        workspace.source,
        "diff",
        "--cached",
        "--binary",
        "--full-index",
        workspace.base_tree,
        "--",
        text=False,
    ).stdout
    if not patch_bytes:
        fail("workspace candidate diff is empty")
    if git(
        workspace.source,
        "diff",
        "--cached",
        "--check",
        workspace.base_tree,
        "--",
        check=False,
    ).returncode:
        fail("workspace candidate fails git diff --cached --check")
    digest = sha256_bytes(patch_bytes)
    try:
        original_manifest = expected_manifest_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        fail(f"cannot decode case manifest {case.manifest}: {error}")
    manifest = updated_manifest_text(
        original_manifest,
        digest=digest,
        paths=names,
        draft=isinstance(case, DraftCase),
    )
    return atomic_update_case_files(
        repo,
        case,
        patch_bytes,
        manifest,
        expected_patch_bytes=expected_patch_bytes,
        expected_manifest_bytes=expected_manifest_bytes,
        workspace=workspace,
        verify_source_commit=workspace.source_commit,
    )


def update_case_from_workspace(
    repo: Path,
    name: str,
    *,
    allow_path_change: bool = False,
) -> Case:
    """Export one candidate while serializing workspace-before-case publication."""
    with workspace_lifecycle_lock(repo):
        return _update_case_from_workspace_locked(
            repo,
            name,
            allow_path_change=allow_path_change,
        )


def workspace_remove_directory_state(path: Path) -> tuple[int, int, str]:
    require_private_directory(path, "workspace removal directory")
    details = path.lstat()
    return details.st_dev, details.st_ino, secure_tree_fingerprint(path)


def validate_workspace_remove_transaction(repo: Path, name: str) -> dict[str, Any]:
    """Validate one external removal marker and either exact directory location."""
    target, staging, marker = workspace_remove_paths(repo, name)
    marker_info = require_cleanup_file(marker, "workspace removal owner")
    if stat.S_IMODE(marker_info.st_mode) != 0o600:
        fail(f"workspace removal owner mode is not exactly 0600: {marker}")
    payload = load_cleanup_json(marker, "workspace removal owner")
    expected = {
        "kind": "workspace-remove",
        "name": name,
        "owner": WORKSPACE_REMOVE_OWNER,
        "policy": "complete",
        "schema": 1,
        "staging": str(staging),
        "target": str(target),
    }
    if (
        set(payload)
        != set(expected)
        | {"device", "fingerprint", "inode", "operation_id"}
        or any(payload.get(key) != value for key, value in expected.items())
        or not UUID4_RE.fullmatch(str(payload.get("operation_id", "")))
        or not SHA256_RE.fullmatch(str(payload.get("fingerprint", "")))
    ):
        fail(f"workspace removal owner is inconsistent: {marker}")
    for key in ("device", "inode"):
        value = payload.get(key)
        minimum = 0 if key == "device" else 1
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            fail(f"workspace removal owner has invalid {key}: {marker}")

    target_present = target.exists() or target.is_symlink()
    staging_present = staging.exists() or staging.is_symlink()
    if target_present and staging_present:
        fail(f"workspace removal has both target and staging: {name}")
    selected = target if target_present else staging if staging_present else None
    if selected is not None:
        require_private_directory(selected, "workspace removal directory")
        details = selected.lstat()
        if details.st_dev != payload["device"] or details.st_ino != payload["inode"]:
            fail(f"workspace removal directory identity changed: {selected}")
        # Before the no-replace rename, the complete tree must still be exactly
        # the one authorized by the external marker. Once renamed, its inode is
        # the durable deletion authority: an interrupted rmtree may have removed
        # an arbitrary prefix, so re-hashing the partial tree would dead-end the
        # only exact retry path.
        if target_present and secure_tree_fingerprint(target) != payload["fingerprint"]:
            fail(f"workspace removal directory changed after publication: {target}")
    return payload


def publish_workspace_remove_transaction(repo: Path, name: str) -> dict[str, Any]:
    target, staging, marker = workspace_remove_paths(repo, name)
    _create_target, create_partial, create_marker = workspace_create_paths(repo, name)
    fingerprint_state = workspace_fingerprint_paths(repo, name)
    if any(
        path.exists() or path.is_symlink()
        for path in (create_partial, create_marker, *fingerprint_state)
    ):
        fail(f"workspace {name} has incomplete state; run workspace-recover")
    if marker.exists() or marker.is_symlink():
        fail(f"workspace {name} already has a removal transaction")
    if staging.exists() or staging.is_symlink():
        fail(f"workspace has an unowned removal staging: {staging}")
    workspace = load_workspace(repo, name, require_host_identity=False)
    if workspace.directory != target:
        fail("workspace removal target escaped its owned root")
    device, inode, fingerprint = workspace_remove_directory_state(target)
    publish_private_json(
        marker,
        {
            "device": device,
            "fingerprint": fingerprint,
            "inode": inode,
            "kind": "workspace-remove",
            "name": name,
            "operation_id": str(uuid.uuid4()),
            "owner": WORKSPACE_REMOVE_OWNER,
            "policy": "complete",
            "schema": 1,
            "staging": str(staging),
            "target": str(target),
        },
        "workspace removal owner",
    )
    return validate_workspace_remove_transaction(repo, name)


def finish_workspace_remove_transaction(repo: Path, name: str) -> tuple[Path, ...]:
    """Idempotently finish the exact externally owned workspace removal."""
    target, staging, marker = workspace_remove_paths(repo, name)
    validate_workspace_remove_transaction(repo, name)
    removed: list[Path] = []
    if target.exists() or target.is_symlink():
        try:
            container_payload.rename_no_replace(target, staging)
        except FileExistsError as error:
            fail(f"workspace removal staging appeared during publication: {staging}: {error}")
        except (container_payload.PayloadError, OSError) as error:
            fail(f"cannot atomically stage workspace removal {target}: {error}")
        fsync_directory(target.parent)
        validate_workspace_remove_transaction(repo, name)
    if staging.exists() or staging.is_symlink():
        validate_workspace_remove_transaction(repo, name)
        shutil.rmtree(staging)
        fsync_directory(staging.parent)
        if staging.exists() or staging.is_symlink():
            fail(f"workspace removal staging was not removed: {staging}")
        removed.append(staging)
    validate_workspace_remove_transaction(repo, name)
    marker.unlink()
    fsync_directory(marker.parent)
    if marker.exists() or marker.is_symlink():
        fail(f"workspace removal owner was not removed: {marker}")
    removed.append(marker)
    return tuple(removed)


def _remove_workspace_locked(repo: Path, name: str) -> Path:
    target, staging, marker = workspace_remove_paths(repo, name)
    if marker.exists() or marker.is_symlink():
        finish_workspace_remove_transaction(repo, name)
        return target
    if staging.exists() or staging.is_symlink():
        fail(f"workspace has an unowned removal staging: {staging}")
    publish_workspace_remove_transaction(repo, name)
    finish_workspace_remove_transaction(repo, name)
    if target.exists() or target.is_symlink():
        fail(f"workspace removal did not complete: {target}")
    return target


def require_workspace_not_bound_to_case_update(repo: Path, name: str) -> None:
    """Preserve a workspace needed by any exact pending case-update owner."""
    case_update_cleanup_blockers(repo)
    root = case_updates_root(repo)
    if not root.exists() and not root.is_symlink():
        return
    for path in root.iterdir():
        match = re.fullmatch(
            r"([a-z0-9]+(?:-[a-z0-9]+)*)\.update\.owner\.json",
            path.name,
        )
        if match is None:
            continue
        payload = validate_case_update_owner(repo, match.group(1))
        if payload.get("workspace") == name:
            fail(
                f"workspace {name} is bound to pending case update {match.group(1)}; "
                "run case-recover first"
            )


def remove_workspace(repo: Path, name: str) -> Path:
    """Remove one exact workspace under its retained lifecycle lock."""
    with workspace_lifecycle_lock(repo), case_update_lock(repo):
        require_workspace_not_bound_to_case_update(repo, name)
        return _remove_workspace_locked(repo, name)


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
        root / "upstream-tests" / "workspaces" / ".lifecycle.lock",
        root / "case-updates" / ".lifecycle.lock",
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


def secure_tree_fingerprint(path: Path) -> str:
    require_owned_directory(path, "cycle cleanup directory")
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
            if target_path.is_absolute():
                fail(f"cycle cleanup symlink has an absolute target: {candidate}")
            for part in target_path.parts:
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


def run_with_index(source: Path, index: Path, *arguments: str) -> str:
    environment = os.environ.copy()
    environment["GIT_INDEX_FILE"] = str(index)
    result = subprocess.run(
        ("git", "-C", str(source), *arguments),
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    if result.returncode:
        detail = "\n".join(
            part for part in (result.stdout.strip(), result.stderr.strip()) if part
        )
        suffix = f"\n{detail}" if detail else ""
        fail(
            f"workspace finalization check failed ({result.returncode}): "
            f"{shlex.join(arguments)}{suffix}"
        )
    return result.stdout.strip()


def begin_workspace_fingerprint(repo: Path, name: str) -> Path:
    scratch, owner, staging, removal = workspace_fingerprint_paths(repo, name)
    _target, create_partial, create_owner = workspace_create_paths(repo, name)
    _remove_target, remove_staging, remove_owner = workspace_remove_paths(repo, name)
    if any(
        path.exists() or path.is_symlink()
        for path in (
            scratch,
            owner,
            staging,
            removal,
            create_partial,
            create_owner,
            remove_staging,
            remove_owner,
        )
    ):
        fail(
            f"workspace {name} has interrupted fingerprint state; "
            "run workspace-recover"
        )
    operation_id = str(uuid.uuid4())
    publish_private_json(
        owner,
        workspace_fingerprint_owner_payload(repo, name, operation_id),
        "workspace fingerprint owner",
    )
    try:
        scratch.mkdir(mode=0o700)
        os.chmod(scratch, 0o700)
        fsync_directory(scratch.parent)
    except OSError as error:
        fail(f"cannot create workspace fingerprint scratch {scratch}: {error}")
    validate_workspace_fingerprint_scratch(repo, name, scratch)
    return scratch


def workspace_fingerprint_remove_payload(
    repo: Path,
    name: str,
    device: int,
    inode: int,
    fingerprint: str,
    operation_id: str,
    owner_sha256: str,
) -> dict[str, Any]:
    scratch, owner, staging, removal = workspace_fingerprint_paths(repo, name)
    return {
        "device": device,
        "fingerprint": fingerprint,
        "inode": inode,
        "kind": "workspace-fingerprint-rmtree-started",
        "name": name,
        "operation_id": operation_id,
        "owner": WORKSPACE_FINGERPRINT_OWNER,
        "owner_record": str(owner),
        "owner_sha256": owner_sha256,
        "policy": "complete",
        "removal": str(removal),
        "repository": str(repo.resolve()),
        "schema": 1,
        "scratch": str(scratch),
        "staging": str(staging),
    }


def validate_workspace_fingerprint_remove_transaction(
    repo: Path,
    name: str,
) -> dict[str, Any]:
    scratch, owner, staging, removal = workspace_fingerprint_paths(repo, name)
    payload = load_cleanup_json(removal, "workspace fingerprint removal phase")
    device = payload.get("device")
    inode = payload.get("inode")
    fingerprint = payload.get("fingerprint")
    operation_id = payload.get("operation_id")
    owner_sha256 = payload.get("owner_sha256")
    owner_present = owner.exists() or owner.is_symlink()
    if (
        not isinstance(operation_id, str)
        or not UUID4_RE.fullmatch(operation_id)
        or not SHA256_RE.fullmatch(str(owner_sha256 or ""))
    ):
        fail(f"workspace fingerprint removal phase identity is invalid: {removal}")
    if owner_present:
        owner_payload = validate_workspace_fingerprint_owner(repo, name)
        if (
            owner_payload["operation_id"] != operation_id
            or sha256_file(owner) != owner_sha256
        ):
            fail(f"workspace fingerprint removal owner differs: {removal}")
    if (
        not isinstance(device, int)
        or isinstance(device, bool)
        or device < 0
        or not isinstance(inode, int)
        or isinstance(inode, bool)
        or inode <= 0
        or not SHA256_RE.fullmatch(str(fingerprint or ""))
        or payload
        != workspace_fingerprint_remove_payload(
            repo,
            name,
            device,
            inode,
            str(fingerprint),
            operation_id,
            str(owner_sha256),
        )
    ):
        fail(f"workspace fingerprint removal phase is inconsistent: {removal}")
    scratch_present = scratch.exists() or scratch.is_symlink()
    staging_present = staging.exists() or staging.is_symlink()
    if scratch_present and staging_present:
        fail(f"workspace fingerprint removal has both scratch and staging: {name}")
    if not owner_present and (scratch_present or staging_present):
        fail(
            "workspace fingerprint removal lost its owner before directory removal: "
            f"{name}"
        )
    selected = scratch if scratch_present else staging if staging_present else None
    if selected is not None:
        require_private_directory(selected, "workspace fingerprint removal directory")
        details = selected.lstat()
        if details.st_dev != device or details.st_ino != inode:
            fail(f"workspace fingerprint removal identity changed: {selected}")
        if scratch_present:
            validate_workspace_fingerprint_scratch(repo, name, scratch)
            if secure_tree_fingerprint(scratch) != fingerprint:
                fail(f"workspace fingerprint scratch changed before removal: {scratch}")
    return payload


def publish_workspace_fingerprint_remove_transaction(
    repo: Path,
    name: str,
) -> dict[str, Any]:
    scratch, owner, staging, removal = workspace_fingerprint_paths(repo, name)
    validate_workspace_fingerprint_scratch(repo, name, scratch)
    if staging.exists() or staging.is_symlink():
        fail(f"workspace fingerprint has unowned removal staging: {staging}")
    if removal.exists() or removal.is_symlink():
        fail(f"workspace fingerprint removal phase already exists: {removal}")
    details = scratch.lstat()
    owner_payload = validate_workspace_fingerprint_owner(repo, name)
    publish_private_json(
        removal,
        workspace_fingerprint_remove_payload(
            repo,
            name,
            details.st_dev,
            details.st_ino,
            secure_tree_fingerprint(scratch),
            str(owner_payload["operation_id"]),
            sha256_file(owner),
        ),
        "workspace fingerprint removal phase",
    )
    return validate_workspace_fingerprint_remove_transaction(repo, name)


def finish_workspace_fingerprint_remove_transaction(
    repo: Path,
    name: str,
) -> tuple[Path, ...]:
    scratch, owner, staging, removal = workspace_fingerprint_paths(repo, name)
    validate_workspace_fingerprint_remove_transaction(repo, name)
    removed: list[Path] = []
    if scratch.exists() or scratch.is_symlink():
        try:
            container_payload.rename_no_replace(scratch, staging)
        except FileExistsError as error:
            fail(f"workspace fingerprint removal staging appeared: {staging}: {error}")
        except (container_payload.PayloadError, OSError) as error:
            fail(f"cannot stage workspace fingerprint removal {scratch}: {error}")
        fsync_directory(scratch.parent)
        validate_workspace_fingerprint_remove_transaction(repo, name)
    if staging.exists() or staging.is_symlink():
        validate_workspace_fingerprint_remove_transaction(repo, name)
        shutil.rmtree(staging)
        fsync_directory(staging.parent)
        if staging.exists() or staging.is_symlink():
            fail(f"workspace fingerprint removal staging remains: {staging}")
        removed.append(staging)
    validate_workspace_fingerprint_remove_transaction(repo, name)
    if owner.exists() or owner.is_symlink():
        validate_workspace_fingerprint_owner(repo, name)
        owner.unlink()
        fsync_directory(owner.parent)
        removed.append(owner)
    validate_workspace_fingerprint_remove_transaction(repo, name)
    removal.unlink()
    fsync_directory(removal.parent)
    removed.append(removal)
    return tuple(path for path in (staging, removal, owner) if path in removed)


def remove_workspace_fingerprint(repo: Path, name: str, scratch: Path) -> None:
    expected, _owner, staging, removal = workspace_fingerprint_paths(repo, name)
    if scratch != expected:
        fail("workspace fingerprint cleanup escaped its owned root")
    if not removal.exists() and not removal.is_symlink():
        if staging.exists() or staging.is_symlink():
            fail(f"workspace fingerprint has unowned removal staging: {staging}")
        publish_workspace_fingerprint_remove_transaction(repo, name)
    finish_workspace_fingerprint_remove_transaction(repo, name)
    if any(path.exists() or path.is_symlink() for path in (scratch, staging, removal)):
        fail(f"workspace fingerprint cleanup did not complete: {scratch}")


def workspace_fingerprint_cleanup_blockers(repo: Path) -> tuple[str, ...]:
    root = (
        repo
        / ".artifacts"
        / "fork-maintenance"
        / "workspace-fingerprints"
    )
    if not root.exists() and not root.is_symlink():
        return ()
    require_private_directory(root, "workspace fingerprint scratch root")
    groups: dict[str, dict[str, Path]] = {}
    patterns = (
        (r"(.+)\.fingerprint\.owner\.json", "owner"),
        (r"(.+)\.fingerprint\.remove\.json", "removal"),
        (r"\.(.+)\.fingerprint\.remove", "staging"),
        (r"(.+)\.fingerprint", "scratch"),
    )
    for path in root.iterdir():
        for pattern, kind in patterns:
            match = re.fullmatch(pattern, path.name)
            if match is not None:
                name = require_workspace_name(match.group(1))
                group = groups.setdefault(name, {})
                if kind in group:
                    fail(f"workspace fingerprint root repeats {kind} state: {path}")
                group[kind] = path
                break
        else:
            fail(f"workspace fingerprint root contains an unrecognized entry: {path}")
    blockers: list[str] = []
    for name, kinds in sorted(groups.items()):
        if "staging" in kinds and "removal" not in kinds:
            fail(f"workspace fingerprint has unowned removal staging: {kinds['staging']}")
        if "owner" not in kinds and set(kinds) != {"removal"}:
            fail(f"workspace has unowned fingerprint state: {name}")
        if "owner" in kinds:
            validate_workspace_fingerprint_owner(repo, name)
        if "removal" in kinds:
            validate_workspace_fingerprint_remove_transaction(repo, name)
        elif "scratch" in kinds:
            validate_workspace_fingerprint_scratch(repo, name, kinds["scratch"])
        blockers.extend(
            f"workspace-fingerprint-runtime:{path}"
            for path in sorted(kinds.values())
        )
    return tuple(blockers)


def _finalized_workspace_fingerprint_locked(repo: Path, name: str) -> str:
    workspace = load_workspace(repo, name, require_host_identity=False)
    if workspace.patch_mode == "reconstruct":
        fail(f"workspace {name} is an unexported reconstruction workspace")
    if unstaged_names(workspace.source) or untracked_names(workspace.source):
        fail(f"workspace {name} contains an unexported candidate or untracked files")
    if rev_parse(workspace.source, "HEAD") != workspace.source_commit:
        fail(f"workspace {name} no longer points at its recorded source commit")
    if rev_parse(workspace.source, "HEAD^{tree}") != workspace.base_tree:
        fail(f"workspace {name} has an inconsistent base tree")

    cases = selected_cases(workspace.selection)
    by_slug = {case.slug: case for case in cases}
    resolution = selection_resolution(repo, workspace.source_commit, workspace.selection)
    entries = resolution.get("patches")
    if not isinstance(entries, list):
        fail(f"workspace {name} current selection has no patch series")
    applied: list[Case] = []
    expected_paths: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            fail(f"workspace {name} current selection entry is invalid")
        slug = entry.get("case")
        status_value = entry.get("status")
        if not isinstance(slug, str) or slug not in by_slug:
            fail(f"workspace {name} current selection identity is invalid")
        if status_value not in {"apply", "already-present"}:
            fail(f"workspace {name} current selection is divergent")
        if status_value != "apply" or workspace.patch_mode == "clean":
            continue
        case = by_slug[slug]
        paths = case.paths
        if workspace.patch_mode == "tests-only":
            paths = tuple(path for path in paths if path.startswith("tests/"))
            if not paths:
                fail(f"workspace {name} selection has no retained test paths")
        expected_paths.update(paths)
        applied.append(case)

    actual_paths = set(workspace_candidate_names(workspace))
    if actual_paths != expected_paths:
        fail(
            f"workspace {name} staged paths do not match the finalized queue: "
            f"{sorted(actual_paths)} != {sorted(expected_paths)}"
        )

    scratch = begin_workspace_fingerprint(repo, name)
    temporary_index = scratch / "index"
    try:
        source_index = workspace.source / ".git" / "index"
        source_index_info = source_index.lstat()
        if (
            source_index.is_symlink()
            or not stat.S_ISREG(source_index_info.st_mode)
            or source_index_info.st_uid != os.getuid()
            or source_index_info.st_nlink != 1
        ):
            fail(f"workspace {name} has an unsafe Git index")
        shutil.copyfile(source_index, temporary_index)
        os.chmod(temporary_index, 0o600)
        for case in reversed(applied):
            arguments = ["apply", "--reverse", "--cached", "--whitespace=error-all"]
            if workspace.patch_mode == "tests-only":
                arguments.append("--include=tests/**")
            arguments.extend(("--", str(case.patch)))
            run_with_index(workspace.source, temporary_index, *arguments)
        if run_with_index(workspace.source, temporary_index, "write-tree") != workspace.base_tree:
            fail(f"workspace {name} is not exactly represented by the finalized queue")
    finally:
        remove_workspace_fingerprint(repo, name, scratch)

    status = git(
        workspace.source,
        "status",
        "--porcelain=v1",
        "--ignored=matching",
        "--untracked-files=all",
    ).stdout
    identity = {
        "base_tree": workspace.base_tree,
        "index_tree": rev_parse(workspace.source, "HEAD^{tree}")
        if not actual_paths
        else git(workspace.source, "write-tree").stdout.strip(),
        "metadata_sha256": sha256_file(workspace_metadata_path(workspace.directory)),
        "mode": workspace.patch_mode,
        "name": name,
        "resolution_sha256": sha256_file(workspace_resolution_path(workspace.directory)),
        "selection": workspace.selection,
        "source_commit": workspace.source_commit,
        "status_sha256": sha256_bytes(status.encode()),
    }
    return sha256_bytes(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode())


def finalized_workspace_fingerprint(repo: Path, name: str) -> str:
    """Fingerprint one finalized workspace without racing a workspace mutation."""
    with workspace_lifecycle_lock(repo):
        return _finalized_workspace_fingerprint_locked(repo, name)


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
            or values["source_remote"] not in REMOTE_URLS
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
        or provenance.get("client_selection") != "master"
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
            "case-updates/.lifecycle.lock",
            "cycle-cleanups/",
            "source-archives/",
            "upstream-tests/.foreground-payload.lock",
            "upstream-tests/image-builds/.image-cache.lock",
            "upstream-tests/logs/.lifecycle.lock",
            "upstream-tests/sources/",
            "upstream-tests/workspaces/.lifecycle.lock",
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
        "deb-result",
        "live-result",
        "live-result-tree",
        "upstream-result",
        "workspace",
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
) -> None:
    require_owned_directory(path, "cycle cleanup directory staging")
    details = path.lstat()
    if stat.S_IMODE(details.st_mode) != 0o700:
        fail(f"cycle cleanup directory mode is not exactly 0700: {path}")
    if (
        details.st_dev != state.device
        or details.st_ino != state.inode
        or secure_tree_fingerprint(path) != state.fingerprint
    ):
        fail(f"cycle cleanup directory changed after transaction publication: {path}")


def cleanup_directory_state(index: int, target: CleanupTarget) -> CleanupDirectoryState:
    require_owned_directory(target.path, "cycle cleanup directory target")
    details = target.path.lstat()
    if stat.S_IMODE(details.st_mode) != 0o700:
        fail(f"cycle cleanup directory mode is not exactly 0700: {target.path}")
    return CleanupDirectoryState(
        index,
        details.st_dev,
        details.st_ino,
        secure_tree_fingerprint(target.path),
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
        if target.kind in {"live-result-tree", "workspace"}
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
        if target.kind in {"live-result-tree", "workspace"}
    }
    phases = {
        cleanup_directory_phase_path(marker, plan.cycle, index)
        for index, target in enumerate(plan.targets)
        if target.kind in {"live-result-tree", "workspace"}
    }
    unexpected = set(entries).difference({marker}, staging, phases)
    if unexpected:
        fail(f"cycle cleanup transaction root has unexpected state: {sorted(unexpected)}")
    transaction = CleanupTransaction(plan, marker, tuple(directories))
    for index, target in enumerate(plan.targets):
        if target.kind not in {"live-result-tree", "workspace"}:
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
            require_private_directory(partial, "cycle cleanup directory staging")
            details = partial.lstat()
            state = by_index[index]
            if details.st_dev != state.device or details.st_ino != state.inode:
                fail(f"cycle cleanup directory identity changed: {partial}")
        else:
            validate_cleanup_directory_state(partial, by_index[index])
    for index, target in enumerate(plan.targets):
        if target.kind not in {"live-result-tree", "workspace"}:
            continue
        if target.path.exists() or target.path.is_symlink():
            validate_cleanup_directory_state(target.path, by_index[index])
    return transaction


def publish_cleanup_transaction(repo: Path, plan: CleanupPlan) -> Path:
    if load_pending_cleanup_transaction(repo) is not None:
        fail("a cycle cleanup transaction is already pending")
    transaction_root = cycle_cleanup_transaction_root(repo, create=True)
    marker = transaction_root / f"{plan.cycle}.remove.json"
    directories = tuple(
        cleanup_directory_state(index, target)
        for index, target in enumerate(plan.targets)
        if target.kind in {"live-result-tree", "workspace"}
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


def case_update_cleanup_blockers(repo: Path) -> tuple[str, ...]:
    update_root = case_updates_root(repo)
    if not update_root.exists() and not update_root.is_symlink():
        return ()
    require_owned_directory(update_root, "case update root")
    update_lock = update_root / ".lifecycle.lock"
    if update_lock.exists() or update_lock.is_symlink():
        require_case_update_lock(update_lock)
    groups: dict[str, set[str]] = {}
    patterns = (
        (r"([a-z0-9]+(?:-[a-z0-9]+)*)\.update\.owner\.json", "owner"),
        (r"([a-z0-9]+(?:-[a-z0-9]+)*)\.update\.remove\.json", "removal"),
        (r"\.([a-z0-9]+(?:-[a-z0-9]+)*)\.update\.remove", "staging"),
        (r"([a-z0-9]+(?:-[a-z0-9]+)*)\.update", "transaction"),
    )
    for path in update_root.iterdir():
        if path == update_lock:
            continue
        for pattern, kind in patterns:
            match = re.fullmatch(pattern, path.name)
            if match is not None:
                groups.setdefault(match.group(1), set()).add(kind)
                break
        else:
            fail(f"case update root contains an unrecognized entry: {path}")
    blockers: list[str] = []
    for slug, kinds in sorted(groups.items()):
        transaction, owner = case_update_paths(repo, slug)
        staging, removal = case_update_removal_paths(repo, slug)
        if "staging" in kinds and "removal" not in kinds:
            fail(f"case update has unowned removal staging: {staging}")
        if (
            kinds.difference({"owner"})
            and "owner" not in kinds
            and kinds != {"removal"}
        ):
            fail(f"case has unowned update state: {slug}")
        if "owner" not in kinds and kinds != {"removal"}:
            fail(f"case update state is incomplete: {slug}")
        if "owner" in kinds:
            validate_case_update_owner(repo, slug)
        if "removal" in kinds:
            validate_case_update_remove_transaction(repo, slug)
        elif "transaction" in kinds:
            marker = transaction / "transaction.json"
            if marker.exists() or marker.is_symlink():
                validate_case_update_transaction(repo, slug)
            else:
                validate_case_update_preparation(repo, slug)
        else:
            validate_case_update_preparation(repo, slug)
        blockers.extend(
            f"case-update-runtime:{path}"
            for path in (transaction, staging, removal, owner)
            if path.exists() or path.is_symlink()
        )
    return tuple(blockers)


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

    blockers.extend(workspace_fingerprint_cleanup_blockers(repo))
    staging_root = case_staging_root(repo)
    if staging_root.exists() or staging_root.is_symlink():
        require_owned_directory(staging_root, "case staging root")
        blockers.extend(f"case-create-runtime:{path}" for path in staging_root.iterdir())
    workspaces = workspace_root(repo)
    if workspaces.exists() or workspaces.is_symlink():
        require_owned_directory(workspaces, "workspace root")
        workspace_lock = workspace_lifecycle_lock_path(repo)
        blockers.extend(
            f"workspace-runtime:{path}"
            for path in workspaces.iterdir()
            if path != workspace_lock
            and path.name.startswith(".")
            and cycle_matches(path.name.lstrip("."), cycle)
        )
    blockers.extend(case_update_cleanup_blockers(repo))
    if inspect_runtime:
        blockers.extend(runtime_cycle_blockers(cycle))
    return tuple(sorted(set(blockers)))


def validate_cleanup_host(repo: Path) -> None:
    verify_repo(repo, ())
    artifact_boundary_check(repo)
    unexpected_dirty = [
        path for path in isolated_dirty_names(repo) if not allowed_develop_path(path)
    ]
    if unexpected_dirty:
        fail(f"cycle cleanup refuses host Xpra source changes: {unexpected_dirty}")
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
                            r"[0-9a-f]{40}-(?:origin|upstream)\.bundle\.lock",
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

    blockers.extend(workspace_fingerprint_cleanup_blockers(repo))

    staging_root = case_staging_root(repo)
    if staging_root.exists() or staging_root.is_symlink():
        require_owned_directory(staging_root, "case staging root")
        groups: dict[str, set[str]] = {}
        for path in staging_root.iterdir():
            match = re.fullmatch(
                r"([a-z0-9]+(?:-[a-z0-9]+)*)\.create\.(partial|owner\.json)",
                path.name,
            )
            if match is None:
                fail(f"case staging root contains an unrecognized entry: {path}")
            slug, kind = match.groups()
            groups.setdefault(slug, set()).add(kind)
        for slug, kinds in sorted(groups.items()):
            target, partial, marker = case_create_paths(repo, slug)
            if "owner.json" not in kinds:
                fail(f"case has an unowned creation partial: {partial}")
            validate_case_create_marker(repo, slug)
            if "partial" in kinds:
                validate_case_create_partial(partial)
            if "partial" in kinds and (target.exists() or target.is_symlink()):
                fail(f"case creation has both partial and published state: {slug}")
            blockers.append(f"case-create-runtime:{marker}")

    blockers.extend(case_update_cleanup_blockers(repo))

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

    workspaces = workspace_root(repo)
    if workspaces.exists():
        require_owned_directory(workspaces, "workspace root")
        workspace_lock = workspace_lifecycle_lock_path(repo)
        if workspace_lock.exists() or workspace_lock.is_symlink():
            info = require_cleanup_file(workspace_lock, "workspace lifecycle lock")
            if stat.S_IMODE(info.st_mode) != 0o600:
                fail(f"invalid retained workspace lifecycle lock: {workspace_lock}")
        for path in sorted(workspaces.iterdir()):
            if path == workspace_lock:
                continue
            if path.name.startswith(".") and cycle_matches(path.name.lstrip("."), cycle):
                fail(f"cycle has an incomplete temporary workspace: {path}")
            if not cycle_matches(path.name, cycle):
                continue
            fingerprint = _finalized_workspace_fingerprint_locked(repo, path.name)
            targets.append(CleanupTarget("workspace", path, fingerprint))

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
    validate_cleanup_host(repo)
    with cleanup_lifecycle_locks(repo):
        pending = load_pending_cleanup_transaction(repo)
        if pending is not None:
            plan = pending.plan
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
    if target.kind == "workspace":
        fingerprint = _finalized_workspace_fingerprint_locked(repo, target.path.name)
    elif target.kind == "live-result-tree":
        fingerprint = secure_tree_fingerprint(target.path)
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
    phase = cleanup_directory_phase_path(
        transaction.marker,
        transaction.plan.cycle,
        state.index,
    )
    if phase.exists() or phase.is_symlink():
        validate_cleanup_directory_phase(transaction, state)
    else:
        validate_cleanup_directory_state(staging, state)
        publish_cleanup_directory_phase(transaction, state)
        validate_cleanup_directory_state(staging, state)
    if staging.exists() or staging.is_symlink():
        require_private_directory(staging, "cycle cleanup directory staging")
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
        if target.kind in {"live-result-tree", "workspace"}:
            state = directory_states.get(index)
            if state is None:
                fail(f"cycle cleanup has no exact directory state: {target.path}")
            validate_cleanup_directory_state(target.path, state)
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
            validate_cleanup_directory_state(staging, state)
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


def develop_rebase(repo: Path) -> str:
    verify_repo(repo, ("origin",))
    require_clean(repo)
    if current_branch(repo) != INTEGRATION_BRANCH:
        fail(f"current branch must be {INTEGRATION_BRANCH}")
    base = sync_repo(repo)
    require_local_master(repo, base)
    result = git(repo, "rebase", f"refs/heads/{BASE_BRANCH}", check=False)
    if result.returncode:
        detail = "\n".join(
            part for part in (result.stdout.strip(), result.stderr.strip()) if part
        )
        suffix = f"\n{detail}" if detail else ""
        fail(
            "develop rebase stopped; resolve every conflict, stage the resolutions, "
            "and run git rebase --continue, or abort and stop patch work"
            f"{suffix}"
        )
    require_rebased_develop(repo, base)
    return base


def develop_check(repo: Path) -> dict[str, Any]:
    require_clean(repo)
    state = embedded_develop_state(repo, "develop check")
    base = state.source_commit
    changed = git(repo, "diff", "--name-only", f"{base}..HEAD").stdout.splitlines()
    unexpected = [
        path
        for path in changed
        if not allowed_develop_path(path)
    ]
    if unexpected:
        fail(f"develop contains source changes outside the patch queue: {unexpected}")
    artifact_boundary_check(repo)
    ci_layout_check(repo, base)
    return selection_resolution(repo, base, f"stacks/{ACTIVE_STACK}")


def case_staging_root(repo: Path, *, create: bool = False) -> Path:
    relative = "case-staging"
    if create:
        return prepare_private_subdirectory(repo, relative, "case staging root")
    return repo / ".artifacts" / "fork-maintenance" / relative


def case_create_paths(repo: Path, slug: str) -> tuple[Path, Path, Path]:
    root = case_staging_root(repo, create=True)
    return (
        CASES_ROOT / slug,
        root / f"{slug}.create.partial",
        root / f"{slug}.create.owner.json",
    )


def validate_case_create_marker(repo: Path, slug: str) -> dict[str, Any]:
    target, partial, marker = case_create_paths(repo, slug)
    payload = load_cleanup_json(marker, "case creation owner")
    expected = {
        "kind": "case-create",
        "owner": CASE_CREATE_OWNER,
        "partial": str(partial),
        "schema": 1,
        "slug": slug,
        "target": str(target),
    }
    if (
        set(payload) != set(expected) | {"operation_id"}
        or any(payload.get(key) != value for key, value in expected.items())
        or not UUID4_RE.fullmatch(str(payload.get("operation_id", "")))
    ):
        fail(f"case creation owner is inconsistent: {marker}")
    return payload


def validate_case_create_partial(partial: Path) -> None:
    secure_tree_fingerprint(partial)
    allowed = {
        "README.md",
        "case.toml",
        "fix.patch",
        "tests",
        "tests/README.md",
    }
    entries = {path.relative_to(partial).as_posix(): path for path in partial.rglob("*")}
    if set(entries).difference(allowed):
        fail(f"case creation partial has unexpected entries: {partial}")
    if any(path.is_symlink() for path in entries.values()):
        fail(f"case creation partial contains a symlink: {partial}")


def validate_published_case(
    slug: str,
    directory: Path | None = None,
    *,
    allow_quarantine_path_transition: bool = False,
) -> None:
    if directory is None:
        directory = CASES_ROOT / slug
    if directory.is_symlink() or not directory.is_dir():
        fail(f"published case is missing or unsafe: {directory}")
    if case_is_draft(directory):
        if allow_quarantine_path_transition:
            fail("a draft case cannot carry quarantine path-transition authority")
        case = load_draft_case(directory)
    else:
        case = load_case(
            directory,
            allow_quarantine_path_transition=allow_quarantine_path_transition,
        )
        if allow_quarantine_path_transition and not is_quarantine_path_transition(case):
            fail("case update owner has stale quarantine path-transition authority")
    if case.slug != slug:
        fail(f"published case has an inconsistent identity: {directory}")


def _recover_case_update_locked(repo: Path, slug: str) -> tuple[Path, ...]:
    with case_update_lock(repo):
        root = case_updates_root(repo, create=True)
        transaction, owner = case_update_paths(repo, slug)
        staging, removal = case_update_removal_paths(repo, slug)
        expected = {transaction, owner, staging, removal, root / ".lifecycle.lock"}
        unexpected = sorted(
            path for path in root.iterdir() if path not in expected
        )
        if unexpected:
            fail(f"case update root has unrecognized or unrelated state: {unexpected}")
        transaction_present = transaction.exists() or transaction.is_symlink()
        owner_present = owner.exists() or owner.is_symlink()
        staging_present = staging.exists() or staging.is_symlink()
        removal_present = removal.exists() or removal.is_symlink()
        if staging_present and not removal_present:
            fail(f"case update has unowned removal staging: {staging}")
        if (
            (transaction_present or staging_present or removal_present)
            and not owner_present
            and not (removal_present and not transaction_present and not staging_present)
        ):
            fail(f"case has unowned update state: {slug}")
        if removal_present:
            return finish_case_update_remove_transaction(repo, slug)
        if not owner_present:
            return ()
        owner_payload = validate_case_update_owner(repo, slug)
        if not transaction_present:
            validate_published_case(
                slug,
                repo / "fork-maintenance" / "cases" / slug,
                allow_quarantine_path_transition=case_update_quarantine_path_transition(
                    owner_payload
                ),
            )
            workspace_name = str(owner_payload["workspace"])
            if workspace_name:
                workspace = load_workspace(
                    repo,
                    workspace_name,
                    require_host_identity=False,
                )
                if workspace.selection != f"cases/{slug}":
                    fail("case update owner names an inconsistent workspace")
            owner.unlink()
            fsync_directory(root)
            return (owner,)
        marker = transaction / "transaction.json"
        if marker.exists() or marker.is_symlink():
            complete_case_update_transaction(repo, slug)
            return (staging, removal, owner)
        return abort_case_update_preparation(repo, slug)


def _recover_case_creation_locked(repo: Path, slug: str) -> tuple[Path, ...]:
    if not SLUG_RE.fullmatch(slug):
        fail("case slug must use lowercase words separated by single hyphens")
    root = case_staging_root(repo, create=True)
    target, partial, marker = case_create_paths(repo, slug)
    allowed = {partial, marker}
    unexpected = sorted(
        path
        for path in root.iterdir()
        if path.name.startswith(f"{slug}.") and path not in allowed
    )
    if unexpected:
        fail(f"case has unrecognized partial state: {unexpected}")
    marker_present = marker.exists() or marker.is_symlink()
    partial_present = partial.exists() or partial.is_symlink()
    updates_root = case_updates_root(repo)
    update_present = False
    if updates_root.exists() or updates_root.is_symlink():
        require_private_directory(updates_root, "case update root")
        update_present = any(
            path.name.startswith(f"{slug}.update")
            or path.name == f".{slug}.update.remove"
            for path in updates_root.iterdir()
        )
    if update_present and (marker_present or partial_present):
        fail(f"case has both creation and update recovery state: {slug}")
    if update_present:
        update_state = _recover_case_update_locked(repo, slug)
        if not update_state:
            fail(f"case has unrecognized update state: {slug}")
        return update_state
    if partial_present and not marker_present:
        fail(f"case has an unowned creation partial: {partial}")
    if not marker_present:
        fail(f"case has no recoverable creation or update state: {slug}")
    validate_case_create_marker(repo, slug)
    if partial_present and (target.exists() or target.is_symlink()):
        fail(f"case creation has both partial and published state: {slug}")
    recovered: list[Path] = []
    if partial_present:
        validate_case_create_partial(partial)
        shutil.rmtree(partial)
        recovered.append(partial)
    elif target.exists() or target.is_symlink():
        validate_published_case(slug)
    marker.unlink()
    recovered.append(marker)
    return tuple(recovered)


def recover_case_creation(repo: Path, slug: str) -> tuple[Path, ...]:
    """Recover case state using the workspace-before-case lock order."""
    with workspace_lifecycle_lock(repo):
        return _recover_case_creation_locked(repo, slug)


def scaffold_case(repo: Path, slug: str) -> Path:
    if not SLUG_RE.fullmatch(slug):
        fail("case slug must use lowercase words separated by single hyphens")
    target, temporary, marker = case_create_paths(repo, slug)
    if target.exists() or target.is_symlink():
        fail(f"case already exists: {slug}")
    if (
        temporary.exists()
        or temporary.is_symlink()
        or marker.exists()
        or marker.is_symlink()
    ):
        fail(f"case {slug} has incomplete creation state; run case-recover")
    publish_private_json(
        marker,
        {
            "kind": "case-create",
            "operation_id": str(uuid.uuid4()),
            "owner": CASE_CREATE_OWNER,
            "partial": str(temporary),
            "schema": 1,
            "slug": slug,
            "target": str(target),
        },
        "case creation owner",
    )
    try:
        temporary.mkdir(mode=0o700)
        (temporary / "tests").mkdir()
        case_kind = (
            "test-quarantine" if slug == TEST_QUARANTINE_SLUG else "production"
        )
        (temporary / "case.toml").write_text(
            "\n".join(
                (
                    "schema = 1",
                    "draft = true",
                    f'slug = "{slug}"',
                    f'kind = "{case_kind}"',
                    'title = ""',
                    'commit_subject = ""',
                    'patch_sha256 = ""',
                    "dependencies = []",
                    "paths = []",
                    "",
                    "[tests]",
                    "list = []",
                    "",
                    "[evidence]",
                    "required_gates = []",
                    "",
                )
            ),
            encoding="utf-8",
        )
        (temporary / "fix.patch").write_bytes(b"")
        (temporary / "README.md").write_text(
            f"# {slug}\n\nDocument the failure, patch boundary, and required tests here.\n",
            encoding="utf-8",
        )
        (temporary / "tests" / "README.md").write_text(
            "Place case-owned functional probes in this directory.\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
        marker.unlink()
    finally:
        if temporary.exists() and not temporary.is_symlink():
            shutil.rmtree(temporary)
        marker.unlink(missing_ok=True)
    return target


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
    commands.add_parser("develop-check", help="validate the clean develop patch-queue branch")
    commands.add_parser("case-list", help="list active patch cases and stacks")

    new = commands.add_parser("case-new", help="create a draft patch case")
    new.add_argument("case")
    case_recover = commands.add_parser(
        "case-recover",
        help="recover exact marker-backed interrupted case creation or update state",
    )
    case_recover.add_argument("case")
    for name in ("patch-check", "patch-apply", "patch-unapply"):
        command = commands.add_parser(name)
        command.add_argument("case")
    update = commands.add_parser("patch-update")
    update.add_argument("case")
    update.add_argument("--allow-path-change", action="store_true")
    for name in ("stack-check", "stack-apply", "stack-unapply"):
        command = commands.add_parser(name)
        command.add_argument("stack")
    workspace_create = commands.add_parser("workspace-create")
    workspace_create.add_argument("workspace")
    workspace_create.add_argument("selection")
    workspace_create.add_argument(
        "--patch-mode",
        choices=tuple(sorted(WORKSPACE_PATCH_MODES)),
        default="patched",
    )
    for name in (
        "workspace-status",
        "workspace-diff",
        "workspace-remove",
        "workspace-recover",
    ):
        command = commands.add_parser(name)
        command.add_argument("workspace")
    for name in ("workspace-stage", "workspace-update"):
        command = commands.add_parser(name)
        command.add_argument("workspace")
        command.add_argument("--allow-path-change", action="store_true")
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
        cases = load_cases()
        stacks = load_stacks(cases)
        artifact_boundary_check(repo)
        print(f"repository={repo}")
        print(f"cases={len(cases)}")
        print(f"stacks={len(stacks)}")
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
        cases = load_cases()
        for case in cases.values():
            print(f"case\t{case.slug}\t{case.patch_sha256}")
        for draft in load_drafts().values():
            print(f"draft\t{draft.slug}")
        for stack in load_stacks(cases).values():
            print(f"stack\t{stack.slug}\t{','.join(stack.series)}")
    elif args.command == "case-new":
        isolated_start_check(repo)
        print(f"created={scaffold_case(repo, args.case)}")
    elif args.command == "case-recover":
        recovered = recover_case_creation(repo, args.case)
        print(f"recovered={','.join(str(path) for path in recovered)}")
        print("case_recover=passed")
    elif args.command in {"patch-check", "stack-check"}:
        selection = (
            f"cases/{args.case}" if args.command == "patch-check" else f"stacks/{args.stack}"
        )
        base = isolated_start_check(repo).source_commit
        print_resolution(selection_resolution(repo, base, selection))
        print("patch_check=passed")
    elif args.command in {"patch-apply", "stack-apply"}:
        selection = (
            f"cases/{args.case}" if args.command == "patch-apply" else f"stacks/{args.stack}"
        )
        print_resolution(apply_selection(repo, selection))
        print("patch_apply=passed")
    elif args.command in {"patch-unapply", "stack-unapply"}:
        selection = (
            f"cases/{args.case}" if args.command == "patch-unapply" else f"stacks/{args.stack}"
        )
        print_resolution(unapply_selection(repo, selection))
        print("patch_unapply=passed")
    elif args.command == "patch-update":
        case = get_case(args.case, allow_draft=True)
        updated = update_case_patch(
            repo,
            case,
            allow_path_change=args.allow_path_change,
        )
        print(f"patch={updated.patch}")
        print(f"patch_sha256={updated.patch_sha256}")
        print("patch_update=passed")
    elif args.command == "workspace-create":
        workspace = create_workspace(
            repo,
            args.workspace,
            args.selection,
            args.patch_mode,
        )
        print(f"workspace={workspace.directory}")
        print(f"source={workspace.source}")
        print(f"source_commit={workspace.source_commit}")
        print(f"base_tree={workspace.base_tree}")
        print(f"selection={workspace.selection}")
        print(f"patch_mode={workspace.patch_mode}")
        print("workspace_create=passed")
    elif args.command == "workspace-status":
        with workspace_lifecycle_lock(repo):
            workspace = load_workspace(
                repo,
                args.workspace,
                require_host_identity=False,
            )
            host_branch = current_branch(repo)
            host_head = rev_parse(repo, "HEAD")
            print(f"workspace={workspace.directory}")
            print(f"source={workspace.source}")
            print(f"source_commit={workspace.source_commit}")
            print(f"selection={workspace.selection}")
            print(f"patch_mode={workspace.patch_mode}")
            print(f"recorded_branch={workspace.branch}")
            print(f"recorded_head={workspace.head}")
            print(f"current_branch={host_branch}")
            print(f"current_head={host_head}")
            host_identity = (
                "current"
                if (host_branch, host_head) == (workspace.branch, workspace.head)
                else "stale"
            )
            print(f"host_identity={host_identity}")
            print(f"staged_paths={','.join(workspace_candidate_names(workspace))}")
            print(f"unstaged_paths={','.join(unstaged_names(workspace.source))}")
            print(f"untracked_paths={','.join(untracked_names(workspace.source))}")
    elif args.command == "workspace-diff":
        with workspace_lifecycle_lock(repo):
            workspace = load_workspace(
                repo,
                args.workspace,
                require_host_identity=False,
            )
            output = git(
                workspace.source,
                "diff",
                "--cached",
                "--binary",
                "--full-index",
                workspace.base_tree,
                "--",
                text=False,
            ).stdout
            sys.stdout.buffer.write(output)
    elif args.command == "workspace-stage":
        names = stage_workspace(
            repo,
            args.workspace,
            allow_path_change=args.allow_path_change,
        )
        print(f"staged_paths={','.join(names)}")
        print("workspace_stage=passed")
    elif args.command == "workspace-update":
        updated = update_case_from_workspace(
            repo,
            args.workspace,
            allow_path_change=args.allow_path_change,
        )
        print(f"patch={updated.patch}")
        print(f"patch_sha256={updated.patch_sha256}")
        print("workspace_update=passed")
    elif args.command == "workspace-remove":
        removed = remove_workspace(repo, args.workspace)
        print(f"removed={removed}")
        print("workspace_remove=passed")
    elif args.command == "workspace-recover":
        recovered = recover_workspace_state(repo, args.workspace)
        print(f"recovered={','.join(str(path) for path in recovered)}")
        print("workspace_recover=passed")
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
