#!/usr/bin/env python3
"""Maintain the downstream Xpra patch queue without publishing Git state."""

from __future__ import annotations

import argparse
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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

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
REMOTE_URLS = {
    "origin": FORK_URL,
    "upstream": UPSTREAM_URL,
}
FORK_OWNER = "kogeler"
BASE_BRANCH = "master"
INTEGRATION_BRANCH = "develop"
ACTIVE_STACK = "develop"
WORKSPACE_OWNER = "xpra-fork-isolated-workspace"
UPSTREAM_TEST_OWNER = "xpra-lab-upstream-tests"
LIVE_JOB_OWNER = "xpra-lab-live-job"
CYCLE_CLEAN_OWNER = "xpra-fork-cycle-cleanup"

SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
GIT_SHA_RE = re.compile(r"[0-9a-f]{40}")
TEST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*")
UNIT_TEST_RE = re.compile(r"unit(?:\.[a-z0-9_]+)+")
SELECTION_RE = re.compile(r"(?:cases|stacks)/[a-z0-9]+(?:-[a-z0-9]+)*")
WORKSPACE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
CYCLE_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
WORKSPACE_PATCH_MODES = frozenset({"clean", "tests-only", "patched"})
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
        "live-wayland-h264-hardware",
    }
)
CASE_KINDS = frozenset({"production", "test-quarantine"})
QUARANTINE_GATES = frozenset(
    {"quarantine", "quarantine-cython", "quarantine-no-compat"}
)
ACTIVE_FORK_WORKFLOW = ".github/workflows/develop.yml"
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


def fail(message: str) -> NoReturn:
    raise ContribError(message)


def run(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    text: bool = True,
) -> subprocess.CompletedProcess[Any]:
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


def case_is_draft(directory: Path) -> bool:
    manifest = directory / "case.toml"
    return manifest.is_file() and read_toml(manifest).get("draft") is True


def load_case(directory: Path) -> Case:
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
        quarantined_tests = require_strings(quarantine_data, "modules", manifest)
        if (
            not quarantined_tests
            or len(quarantined_tests) != len(set(quarantined_tests))
            or not all(UNIT_TEST_RE.fullmatch(item) for item in quarantined_tests)
        ):
            fail(f"{manifest}: quarantine.modules must contain unique unit.* modules")
        if not set(quarantined_tests).issubset(tests):
            fail(f"{manifest}: every quarantined module must be retained in tests.list")
        if set(required_gates) != QUARANTINE_GATES:
            fail(f"{manifest}: a test-quarantine case must require all quarantine gates")
        expected_paths = tuple(
            sorted(f"tests/unittests/{item.replace('.', '/')}.py" for item in quarantined_tests)
        )
        if tuple(sorted(paths)) != expected_paths:
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


def load_cases() -> dict[str, Case]:
    if CASES_ROOT.is_symlink() or not CASES_ROOT.is_dir():
        fail(f"cases directory is missing or unsafe: {CASES_ROOT}")
    cases = {
        path.name: load_case(path)
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


def get_case(slug: str, *, allow_draft: bool = False) -> Case | DraftCase:
    if not SLUG_RE.fullmatch(slug):
        fail(f"invalid case slug: {slug!r}")
    directory = CASES_ROOT / slug
    if allow_draft and directory.is_dir() and case_is_draft(directory):
        return load_draft_case(directory)
    try:
        return load_cases()[slug]
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


def verify_live_masters(repo: Path) -> str:
    values: dict[str, str] = {}
    for remote in ("upstream", "origin"):
        cached = cached_master(repo, remote)
        live = live_remote_ref(repo, remote, BASE_BRANCH)
        if not live or cached != live:
            fail(
                f"cached {remote}/{BASE_BRANCH} {cached} does not match live "
                f"{live or '<missing>'}; run repo-sync"
            )
        values[remote] = live
    if values["origin"] != values["upstream"]:
        fail(
            f"fork origin/{BASE_BRANCH} {values['origin']} does not match "
            f"upstream/{BASE_BRANCH} {values['upstream']}; the operator must run "
            f"gh repo sync {FORK_OWNER}/xpra --source Xpra-org/xpra "
            f"--branch {BASE_BRANCH} without --force, then run repo-sync"
        )
    return values["upstream"]


def sync_repo(repo: Path) -> str:
    verify_repo(repo)
    fetch_master(repo, "upstream")
    fetch_master(repo, "origin")
    return verify_live_masters(repo)


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
            f"current branch does not contain live upstream/{BASE_BRANCH} {base}; "
            "rebase develop onto the synchronized local master first"
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
            f"develop is not rebased onto live upstream/{BASE_BRANCH} {base}; "
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
    verify_repo(repo)
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
        fail("cannot compare current source paths with upstream master")
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


def selection_resolution(repo: Path, revision: str, selection: str) -> dict[str, Any]:
    if not SELECTION_RE.fullmatch(selection):
        fail(f"invalid selection: {selection!r}")
    with tempfile.TemporaryDirectory(prefix="xpra-fork-selection-") as raw:
        source = Path(raw)
        archive_tree(repo, revision, source)
        result = run(
            (
                sys.executable,
                str(SELECTION_TOOL),
                "--lab-root",
                str(AUTOMATION_ROOT),
                "--selection",
                selection,
                "resolve",
                "--source-tree",
                str(source),
                "--source-commit",
                revision,
            )
        )
    data = json.loads(result.stdout)
    if not isinstance(data, dict):
        fail("selection resolver returned an invalid document")
    return data


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


def atomic_update_case_files(
    case: Case | DraftCase,
    patch_bytes: bytes,
    manifest_text: str,
) -> Case:
    patch_path = case.patch
    manifest_path = case.manifest
    patch_original = patch_path.read_bytes()
    manifest_original = manifest_path.read_bytes()
    patch_mode = patch_path.stat().st_mode & 0o7777
    manifest_mode = manifest_path.stat().st_mode & 0o7777
    nonce = f"{os.getpid()}-{os.urandom(4).hex()}"
    patch_new = case.directory / f".fix.patch.{nonce}.new"
    manifest_new = case.directory / f".case.toml.{nonce}.new"
    patch_restore = case.directory / f".fix.patch.{nonce}.restore"
    manifest_restore = case.directory / f".case.toml.{nonce}.restore"
    patch_replaced = False
    manifest_replaced = False
    try:
        patch_new.write_bytes(patch_bytes)
        manifest_new.write_text(manifest_text, encoding="utf-8", newline="\n")
        os.chmod(patch_new, patch_mode)
        os.chmod(manifest_new, manifest_mode)
        os.replace(patch_new, patch_path)
        patch_replaced = True
        os.replace(manifest_new, manifest_path)
        manifest_replaced = True
        return load_case(case.directory)
    except BaseException:
        if patch_replaced:
            patch_restore.write_bytes(patch_original)
            os.chmod(patch_restore, patch_mode)
            os.replace(patch_restore, patch_path)
        if manifest_replaced:
            manifest_restore.write_bytes(manifest_original)
            os.chmod(manifest_restore, manifest_mode)
            os.replace(manifest_restore, manifest_path)
        raise
    finally:
        for path in (patch_new, manifest_new, patch_restore, manifest_restore):
            path.unlink(missing_ok=True)


def update_case_patch(
    repo: Path,
    case: Case | DraftCase,
    *,
    allow_path_change: bool = False,
) -> Case:
    verify_repo(repo)
    require_non_master(repo)
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
    with tempfile.TemporaryDirectory(prefix="xpra-fork-patch-update-") as raw:
        tree = Path(raw)
        archive_tree(repo, base, tree)
        candidate_patch = tree / "candidate.patch"
        candidate_patch.write_bytes(patch_bytes)
        run(("git", "apply", "--check", "--whitespace=error-all", str(candidate_patch)), cwd=tree)
        run(("git", "apply", "--whitespace=error-all", str(candidate_patch)), cwd=tree)
        run(("git", "apply", "--reverse", "--check", str(candidate_patch)), cwd=tree)
    digest = sha256_bytes(patch_bytes)
    original = case.manifest.read_text(encoding="utf-8")
    manifest = updated_manifest_text(
        original,
        digest=digest,
        paths=names,
        draft=isinstance(case, DraftCase),
    )
    return atomic_update_case_files(case, patch_bytes, manifest)


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


def validate_fork_workflow(path: Path) -> None:
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
    if semantic_lines != fork_workflow_semantics():
        fail(
            "active fork workflow is not the approved thin checkout-plus-Make interface"
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
        fail(f"canonical master has no workflows below {UPSTREAM_WORKFLOW_DIRECTORY}")

    active_root = repo / UPSTREAM_WORKFLOW_DIRECTORY
    active_files = repository_files(active_root, "active workflow directory")
    expected_active = (Path(ACTIVE_FORK_WORKFLOW).name,)
    if active_files != expected_active:
        fail(
            "active workflow directory must contain only the fork workflow: "
            f"{active_files} != {expected_active}"
        )
    validate_fork_workflow(repo / ACTIVE_FORK_WORKFLOW)

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
            fail(f"disabled workflow differs from canonical master: {disabled}")
    if disabled_files != tuple(sorted(expected_disabled)):
        fail(
            "disabled workflow set does not exactly mirror canonical master: "
            f"{disabled_files} != {tuple(sorted(expected_disabled))}"
        )
    return {
        "base": base,
        "active_workflow": ACTIVE_FORK_WORKFLOW,
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


def ci_prepare(repo: Path) -> IsolatedState:
    validate_ci_checkout(repo)
    return ci_start_check(repo)


def ci_start_check(repo: Path) -> IsolatedState:
    """Locate the master boundary already embedded in pushed develop."""
    verify_repo(repo, ("origin",))
    artifact_boundary_check(repo)
    branch = current_branch(repo)
    if branch != INTEGRATION_BRANCH:
        fail(f"CI checkout must stay on {INTEGRATION_BRANCH}")
    head = rev_parse(repo, "HEAD")
    status = porcelain(repo)
    unexpected_dirty = [
        path for path in isolated_dirty_names(repo) if not allowed_develop_path(path)
    ]
    if unexpected_dirty:
        fail(
            "CI refuses host Xpra source changes; only fork control paths may "
            f"be dirty: {unexpected_dirty}"
        )

    source_tip = cached_master(repo, "origin")
    merge_base = git(repo, "merge-base", source_tip, head, check=False)
    source_commit = merge_base.stdout.strip()
    if merge_base.returncode or not GIT_SHA_RE.fullmatch(source_commit):
        fail(
            f"pushed {INTEGRATION_BRANCH} and checkout origin/{BASE_BRANCH} "
            "have no single usable history boundary"
        )
    committed = git(
        repo, "diff", "--name-only", f"{source_commit}..{head}"
    ).stdout.splitlines()
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
        or porcelain(repo) != status
    ):
        fail("repository branch, HEAD, or worktree changed while preparing CI source")
    return IsolatedState(
        branch=branch,
        head=head,
        source_commit=source_commit,
        fork_base=source_commit,
        source_in_head=True,
        worktree_status=status,
    )


def allowed_develop_path(path: str) -> bool:
    return any(path == allowed or path.startswith(allowed) for allowed in ALLOWED_DEVELOP_PATHS)


def isolated_dirty_names(repo: Path) -> tuple[str, ...]:
    return tuple(
        sorted(
            set(staged_names(repo))
            | set(unstaged_names(repo))
            | set(untracked_names(repo))
        )
    )


def isolated_start_check(repo: Path) -> IsolatedState:
    """Freeze current master without changing the checked-out branch or source tree."""
    verify_repo(repo)
    artifact_boundary_check(repo)
    branch = current_branch(repo)
    if branch != INTEGRATION_BRANCH:
        fail(f"isolated patch work must stay on {INTEGRATION_BRANCH}")
    head = rev_parse(repo, "HEAD")
    status = porcelain(repo)
    unexpected_dirty = [path for path in isolated_dirty_names(repo) if not allowed_develop_path(path)]
    if unexpected_dirty:
        fail(
            "isolated patch work refuses host Xpra source changes; only fork control "
            f"paths may be dirty: {unexpected_dirty}"
        )

    source_commit = sync_repo(repo)
    if current_branch(repo) != branch or rev_parse(repo, "HEAD") != head or porcelain(repo) != status:
        fail("repository branch, HEAD, or worktree changed while freezing isolated source")

    fork_base = git(repo, "merge-base", source_commit, head).stdout.strip()
    if not GIT_SHA_RE.fullmatch(fork_base):
        fail("cannot resolve the shared base of develop and current master")
    committed = git(repo, "diff", "--name-only", f"{fork_base}..{head}").stdout.splitlines()
    unexpected_committed = [path for path in committed if not allowed_develop_path(path)]
    if unexpected_committed:
        fail(
            "develop contains committed Xpra source changes outside the patch queue: "
            f"{unexpected_committed}"
        )
    merges = git(repo, "rev-list", "--merges", f"{fork_base}..{head}").stdout.splitlines()
    if merges:
        fail(f"develop contains fork-side merge commits: {merges}")
    return IsolatedState(
        branch=branch,
        head=head,
        source_commit=source_commit,
        fork_base=fork_base,
        source_in_head=is_ancestor(repo, source_commit, head),
        worktree_status=status,
    )


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


def write_private_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


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


def create_workspace(
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
            resolution = selection_resolution(repo, state.source_commit, selection)
    else:
        cases = selected_cases(selection)
        resolution = selection_resolution(repo, state.source_commit, selection)
    root = prepare_workspace_root(repo)
    target = root / name
    if target.exists() or target.is_symlink():
        fail(f"workspace already exists: {name}")
    temporary = Path(tempfile.mkdtemp(prefix=f".{name}.", dir=root))
    os.chmod(temporary, 0o700)
    try:
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
            if not isinstance(slug, str) or slug not in by_slug or status not in {
                "apply",
                "already-present",
            }:
                fail("selection resolution patch identity is invalid")
            if status != "apply" or patch_mode == "clean":
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
        if (
            current_branch(repo) != state.branch
            or rev_parse(repo, "HEAD") != state.head
            or porcelain(repo) != state.worktree_status
        ):
            fail("host branch, HEAD, or worktree changed while creating workspace")
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return load_workspace(repo, name)


def workspace_candidate_names(workspace: Workspace) -> tuple[str, ...]:
    return tuple(sorted(workspace_index_names(workspace.source, workspace.base_tree)))


def stage_workspace(
    repo: Path,
    name: str,
    *,
    allow_path_change: bool = False,
) -> tuple[str, ...]:
    workspace = load_workspace(repo, name)
    if not workspace.selection.startswith("cases/"):
        fail("only an atomic case workspace can be staged for patch update")
    slug = workspace.selection.split("/", 1)[1]
    case = get_case(slug, allow_draft=True)
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


def update_case_from_workspace(
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
    case = get_case(slug, allow_draft=True)
    resolution = json.loads(
        workspace_resolution_path(workspace.directory).read_text(encoding="utf-8")
    )
    entries = resolution.get("patches")
    if isinstance(case, DraftCase):
        if entries != [] or resolution.get("draft_case") != case.slug:
            fail("draft workspace resolution is inconsistent")
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
    with tempfile.TemporaryDirectory(prefix=".verify-", dir=workspace.directory) as raw:
        tree = Path(raw) / "source"
        tree.mkdir()
        archive_tree(repo, workspace.source_commit, tree)
        candidate_patch = Path(raw) / "candidate.patch"
        candidate_patch.write_bytes(patch_bytes)
        run(("git", "apply", "--check", "--whitespace=error-all", str(candidate_patch)), cwd=tree)
        run(("git", "apply", "--whitespace=error-all", str(candidate_patch)), cwd=tree)
        run(("git", "apply", "--reverse", "--check", str(candidate_patch)), cwd=tree)
    digest = sha256_bytes(patch_bytes)
    manifest = updated_manifest_text(
        case.manifest.read_text(encoding="utf-8"),
        digest=digest,
        paths=names,
        draft=isinstance(case, DraftCase),
    )
    updated = atomic_update_case_files(case, patch_bytes, manifest)
    if isinstance(case, DraftCase):
        current_resolution = selection_resolution(
            repo,
            workspace.source_commit,
            workspace.selection,
        )
        metadata_path = workspace_metadata_path(workspace.directory)
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["selection_sha256"] = current_resolution["selection_sha256"]
        metadata["resolution_sha256"] = current_resolution["resolution_sha256"]
        metadata["patch_mode"] = "patched"
        write_private_json(workspace_resolution_path(workspace.directory), current_resolution)
        write_private_json(metadata_path, metadata)
    return updated


def remove_workspace(repo: Path, name: str) -> Path:
    workspace = load_workspace(repo, name, require_host_identity=False)
    target = workspace.directory
    root = workspace_root(repo).resolve(strict=True)
    if target.parent.resolve(strict=True) != root or target.name != name:
        fail("workspace cleanup target escaped its owned root")
    shutil.rmtree(target)
    if target.exists() or target.is_symlink():
        fail(f"workspace removal did not complete: {target}")
    return target


def require_cycle_name(value: str) -> str:
    if not CYCLE_RE.fullmatch(value):
        fail("CYCLE must use lowercase words separated by single hyphens")
    return value


def cycle_matches(value: str, cycle: str) -> bool:
    return value == cycle or value.startswith(f"{cycle}-")


def cleanup_state_root(repo: Path) -> Path:
    return repo / ".artifacts" / "fork-maintenance"


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
            if mode & 0o022:
                fail(f"cycle cleanup tree contains a writable shared directory: {candidate}")
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


def finalized_workspace_fingerprint(repo: Path, name: str) -> str:
    workspace = load_workspace(repo, name, require_host_identity=False)
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

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".cycle-clean-index-",
        dir=workspace.directory,
    )
    os.close(descriptor)
    temporary_index = Path(temporary_name)
    try:
        shutil.copyfile(workspace.source / ".git" / "index", temporary_index)
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
        temporary_index.unlink(missing_ok=True)
        temporary_index.with_suffix(f"{temporary_index.suffix}.lock").unlink(missing_ok=True)

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


def upstream_result_targets(root: Path, cycle: str) -> tuple[list[CleanupTarget], set[str]]:
    logs = root / "upstream-tests" / "logs"
    if not logs.exists():
        return [], set()
    require_owned_directory(logs, "upstream-test log root")
    suffixes = (
        ".selection-resolution.sha256",
        ".selection-resolution.json",
        ".status",
        ".log",
    )
    groups: dict[str, dict[str, Path]] = {}
    for path in logs.iterdir():
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
        if set(paths).difference(suffixes) or ".status" not in paths or ".log" not in paths:
            fail(f"collected upstream result is incomplete: {name}")
        status_values = parse_status_file(paths[".status"])
        if status_values.get("owner") != UPSTREAM_TEST_OWNER:
            fail(f"collected upstream result has the wrong owner: {name}")
        if status_values.get("name") != name:
            fail(f"collected upstream result has the wrong name: {name}")
        log = paths[".log"]
        require_cleanup_file(log, "collected upstream log")
        if status_values.get("log_sha256") != sha256_file(log):
            fail(f"collected upstream log digest does not match: {name}")
        resolution_paths = {
            ".selection-resolution.json",
            ".selection-resolution.sha256",
        }.intersection(paths)
        resolution_ok = status_values.get("selection_resolution_ok")
        if resolution_ok == "1":
            if len(resolution_paths) != 2:
                fail(f"collected upstream resolution is incomplete: {name}")
            resolution = paths[".selection-resolution.json"]
            resolution_digest = paths[".selection-resolution.sha256"]
            require_cleanup_file(resolution, "collected selection resolution")
            require_cleanup_file(resolution_digest, "collected resolution digest")
            try:
                recorded_digest = resolution_digest.read_text(encoding="ascii").strip()
                resolution_payload = json.loads(resolution.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                fail(f"cannot read collected resolution digest {resolution_digest}: {error}")
            if not isinstance(resolution_payload, dict):
                fail(f"collected upstream resolution is not a JSON object: {name}")
            contract_digest = resolution_payload.get("resolution_sha256")
            if (
                contract_digest != recorded_digest
                or contract_digest != status_values.get("selection_resolution_sha256")
                or not isinstance(contract_digest, str)
                or not SHA256_RE.fullmatch(contract_digest)
            ):
                fail(f"collected upstream resolution digest does not match: {name}")
        elif resolution_paths:
            fail(f"unexpected collected upstream resolution files: {name}")
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


def live_result_targets(root: Path, cycle: str) -> tuple[list[CleanupTarget], set[str]]:
    jobs = root / "jobs" / "live"
    results = root / "live-results"
    if not jobs.exists() and not results.exists():
        return [], set()
    if jobs.exists():
        require_owned_directory(jobs, "live-job record root")
    if results.exists():
        require_owned_directory(results, "live-result root")

    names: set[str] = set()
    if jobs.exists():
        for path in jobs.iterdir():
            for suffix in (".status.json", ".log", ".owner.json"):
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
            if cycle_matches(path.name, cycle):
                names.add(path.name)

    targets: list[CleanupTarget] = []
    for name in sorted(names):
        status_path = jobs / f"{name}.status.json"
        log_path = jobs / f"{name}.log"
        owner_path = jobs / f"{name}.owner.json"
        if owner_path.exists() or owner_path.is_symlink():
            fail(
                f"live run {name} still has runtime ownership; collect it and run "
                f"live-remove first"
            )
        if not status_path.exists() or not log_path.exists():
            fail(f"collected live result is incomplete: {name}")
        status_values = load_cleanup_json(status_path, "collected live status")
        if (
            status_values.get("schema") not in {1, 2}
            or status_values.get("owner") != LIVE_JOB_OWNER
            or status_values.get("run") != name
        ):
            fail(f"collected live result identity is inconsistent: {name}")
        require_cleanup_file(log_path, "collected live log")
        if status_values.get("log_sha256") != sha256_file(log_path):
            fail(f"collected live log digest does not match: {name}")
        for path in (status_path, log_path):
            targets.append(CleanupTarget("live-result", path, sha256_file(path)))

        result_directory = results / name
        report = result_directory / "report.json"
        recorded_report = status_values.get("report")
        if recorded_report != str(report):
            fail(f"collected live report path is inconsistent: {name}")
        if result_directory.exists() or result_directory.is_symlink():
            fingerprint = secure_tree_fingerprint(result_directory)
            recorded_sha256 = status_values.get("report_sha256")
            if report.exists():
                if recorded_sha256 != sha256_file(report):
                    fail(f"collected live report digest does not match: {name}")
            elif recorded_sha256:
                fail(f"collected live report is missing: {name}")
            targets.append(CleanupTarget("live-result-tree", result_directory, fingerprint))
        elif status_values.get("report_sha256"):
            fail(f"collected live result directory is missing: {name}")
    return targets, names


def runtime_cycle_blockers(cycle: str) -> tuple[str, ...]:
    blockers: list[str] = []
    for owner in (UPSTREAM_TEST_OWNER, "live"):
        listed = run(
            (
                "podman",
                "ps",
                "--all",
                "--quiet",
                "--filter",
                f"label=io.xpra.lab.owner={owner}",
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
            run_name = str(labels.get("io.xpra.lab.run-id", ""))
            identity = run_name if owner == "live" else container_name
            if cycle_matches(identity, cycle):
                blockers.append(f"podman-container:{object_id}")

    networks = run(
        (
            "podman",
            "network",
            "ls",
            "--quiet",
            "--filter",
            "label=io.xpra.lab.owner=live",
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
        if cycle_matches(str(labels.get("io.xpra.lab.run-id", "")), cycle):
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
            "source-archives/",
            "upstream-tests/sources/",
            "venvs/",
            "tooling-venv/",
            "Podman content-addressed images",
            "Podman ccache volume",
        ],
    }


def build_cleanup_plan(
    repo: Path,
    cycle: str,
    *,
    inspect_runtime: bool = True,
) -> CleanupPlan:
    require_cycle_name(cycle)
    verify_repo(repo)
    artifact_boundary_check(repo)
    if current_branch(repo) != INTEGRATION_BRANCH:
        fail(f"cycle cleanup must remain on {INTEGRATION_BRANCH}")
    unexpected_dirty = [
        path for path in isolated_dirty_names(repo) if not allowed_develop_path(path)
    ]
    if unexpected_dirty:
        fail(f"cycle cleanup refuses host Xpra source changes: {unexpected_dirty}")
    root = cleanup_state_root(repo)
    require_owned_directory(repo, "repository root", private=False)
    require_owned_directory(repo / ".artifacts", "artifact root")
    require_owned_directory(root, "fork-maintenance artifact root")

    blockers: list[str] = []
    upstream_root = root / "upstream-tests"
    if upstream_root.exists():
        require_owned_directory(upstream_root, "upstream-test state root")
        runs = upstream_root / "runs"
        if runs.exists():
            require_owned_directory(runs, "upstream-test run root")
            for path in runs.iterdir():
                name = path.name.removesuffix(".owner")
                if cycle_matches(name.lstrip("."), cycle):
                    blockers.append(f"upstream-runtime:{path}")
        image_builds = upstream_root / "image-builds"
        if image_builds.exists():
            require_owned_directory(image_builds, "upstream image-build root")
            for path in image_builds.iterdir():
                if cycle_matches(path.name.lstrip("."), cycle):
                    blockers.append(f"upstream-image-runtime:{path}")

    live_jobs = root / "jobs" / "live"
    if live_jobs.exists():
        require_owned_directory(live_jobs, "live-job record root")
        for path in live_jobs.iterdir():
            name = path.name
            runtime_suffix = ""
            for suffix in (
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
                ".owner.json",
                ".completion.json",
                ".runtime",
            } and cycle_matches(name, cycle)
            temporary = path.name.startswith(".") and cycle_matches(
                name.lstrip("."), cycle
            )
            if owned or temporary:
                blockers.append(f"live-runtime:{path}")

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

    workspaces = workspace_root(repo)
    if workspaces.exists():
        require_owned_directory(workspaces, "workspace root")
        for path in sorted(workspaces.iterdir()):
            if path.name.startswith(".") and cycle_matches(path.name.lstrip("."), cycle):
                fail(f"cycle has an incomplete temporary workspace: {path}")
            if not cycle_matches(path.name, cycle):
                continue
            fingerprint = finalized_workspace_fingerprint(repo, path.name)
            targets.append(CleanupTarget("workspace", path, fingerprint))

    if not targets:
        fail(f"no finalized artifacts match cycle {cycle!r}")
    targets.sort(key=lambda target: (target.path.as_posix(), target.kind))
    provisional = CleanupPlan(cycle, tuple(targets), "")
    payload = cleanup_plan_payload(repo, provisional)
    digest = sha256_bytes(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    return CleanupPlan(cycle, tuple(targets), digest)


def remove_cleanup_plan(repo: Path, plan: CleanupPlan, confirmation: str) -> int:
    if confirmation != plan.digest:
        fail(
            "CONFIRM does not match the current cleanup plan; rerun cycle-clean-plan "
            "and review every target"
        )
    root = cleanup_state_root(repo).resolve(strict=True)
    removed = 0
    for target in plan.targets:
        if target.path.parent == target.path:
            fail("cycle cleanup target has no safe parent")
        try:
            target.path.relative_to(root)
        except ValueError:
            fail(f"cycle cleanup target escaped its owned root: {target.path}")
        if target.kind == "workspace":
            if finalized_workspace_fingerprint(repo, target.path.name) != target.fingerprint:
                fail(f"cycle cleanup target changed after planning: {target.path}")
            remove_workspace(repo, target.path.name)
        elif target.kind == "live-result-tree":
            if secure_tree_fingerprint(target.path) != target.fingerprint:
                fail(f"cycle cleanup target changed after planning: {target.path}")
            shutil.rmtree(target.path)
        else:
            require_cleanup_file(target.path, "cycle cleanup target")
            if sha256_file(target.path) != target.fingerprint:
                fail(f"cycle cleanup target changed after planning: {target.path}")
            target.path.unlink()
        if target.path.exists() or target.path.is_symlink():
            fail(f"cycle cleanup did not remove its exact target: {target.path}")
        removed += 1
    return removed


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
            fail("local master is ahead of or diverged from upstream; owner review is required")
        if current_branch(repo) == BASE_BRANCH:
            git(repo, "merge", "--ff-only", f"refs/remotes/upstream/{BASE_BRANCH}")
        elif local != base:
            git(repo, "branch", "-f", BASE_BRANCH, f"refs/remotes/upstream/{BASE_BRANCH}")
    if rev_parse(repo, "refs/heads/master") != base:
        fail("local master did not reach the verified upstream commit")
    return base


def develop_rebase(repo: Path) -> str:
    verify_repo(repo)
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
    base = sync_repo(repo)
    if current_branch(repo) != INTEGRATION_BRANCH:
        fail(f"current branch must be {INTEGRATION_BRANCH}")
    require_rebased_develop(repo, base)
    changed = git(repo, "diff", "--name-only", f"{base}..HEAD").stdout.splitlines()
    unexpected = [
        path
        for path in changed
        if not any(path == allowed or path.startswith(allowed) for allowed in ALLOWED_DEVELOP_PATHS)
    ]
    if unexpected:
        fail(f"develop contains source changes outside the patch queue: {unexpected}")
    artifact_boundary_check(repo)
    ci_layout_check(repo, base)
    return selection_resolution(repo, base, f"stacks/{ACTIVE_STACK}")


def scaffold_case(slug: str) -> Path:
    if not SLUG_RE.fullmatch(slug):
        fail("case slug must use lowercase words separated by single hyphens")
    target = CASES_ROOT / slug
    if target.exists() or target.is_symlink():
        fail(f"case already exists: {slug}")
    temporary = Path(tempfile.mkdtemp(prefix=f".{slug}.", dir=CASES_ROOT))
    try:
        (temporary / "tests").mkdir()
        (temporary / "case.toml").write_text(
            "\n".join(
                (
                    "schema = 1",
                    "draft = true",
                    f'slug = "{slug}"',
                    'kind = "production"',
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
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return target


def print_resolution(resolution: dict[str, Any]) -> None:
    print(json.dumps(resolution, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="validate the in-repository automation boundary")
    commands.add_parser("repo-status", help="show local branch and cached master refs")
    commands.add_parser("repo-sync", help="fetch and compare live fork and upstream master")
    commands.add_parser("master-update", help="fast-forward local master to verified upstream")
    commands.add_parser("develop-rebase", help="rebase clean develop onto verified master")
    commands.add_parser("patch-start-check", help="verify sync and rebase before patch work")
    commands.add_parser(
        "isolated-start-check",
        help="verify dirty-control-plane-safe isolated patch work without switching branches",
    )
    commands.add_parser(
        "ci-layout-check",
        help="verify disabled upstream workflows and the thin fork workflow",
    )
    commands.add_parser(
        "ci-prepare",
        help="freeze canonical master and validate a develop CI checkout",
    )
    commands.add_parser("develop-check", help="validate the clean develop patch-queue branch")
    commands.add_parser("case-list", help="list active patch cases and stacks")

    new = commands.add_parser("case-new", help="create a draft patch case")
    new.add_argument("case")
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
    for name in ("workspace-status", "workspace-diff", "workspace-remove"):
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
        base = sync_repo(repo)
        print(f"upstream={base}")
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
    elif args.command == "ci-layout-check":
        verify_repo(repo)
        base = rev_parse(repo, f"refs/remotes/upstream/{BASE_BRANCH}")
        print_resolution(ci_layout_check(repo, base))
        print("ci_layout_check=passed")
    elif args.command == "ci-prepare":
        state = ci_prepare(repo)
        print(f"branch={state.branch}")
        print(f"head={state.head}")
        print(f"source_commit={state.source_commit}")
        print("ci_prepare=passed")
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
        print(f"created={scaffold_case(args.case)}")
    elif args.command in {"patch-check", "stack-check"}:
        selection = (
            f"cases/{args.case}" if args.command == "patch-check" else f"stacks/{args.stack}"
        )
        base = patch_start_check(repo)
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
        workspace = load_workspace(repo, args.workspace)
        print(f"workspace={workspace.directory}")
        print(f"source={workspace.source}")
        print(f"source_commit={workspace.source_commit}")
        print(f"selection={workspace.selection}")
        print(f"patch_mode={workspace.patch_mode}")
        print(f"staged_paths={','.join(workspace_candidate_names(workspace))}")
        print(f"unstaged_paths={','.join(unstaged_names(workspace.source))}")
        print(f"untracked_paths={','.join(untracked_names(workspace.source))}")
    elif args.command == "workspace-diff":
        workspace = load_workspace(repo, args.workspace)
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
    elif args.command == "cycle-clean-plan":
        plan = build_cleanup_plan(repo, args.cycle)
        payload = cleanup_plan_payload(repo, plan)
        payload["confirm"] = plan.digest
        print(json.dumps(payload, indent=2, sort_keys=True))
        print(f"cycle_clean_confirm={plan.digest}")
    elif args.command == "cycle-clean":
        plan = build_cleanup_plan(repo, args.cycle)
        removed = remove_cleanup_plan(repo, plan, args.confirm)
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
