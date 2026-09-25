#!/usr/bin/env python3
"""Validate and freeze an atomic fork-maintenance case or the develop stack.

Every case is exactly one commit on ``develop`` carrying a ``Fork-Case: <slug>``
trailer. On the host (git mode) the lab root is the tracked ``fork-maintenance``
directory and each case diff is taken from its commit. A frozen payload
(snapshot mode) carries ``selection-source.json`` and one generated
``cases/<slug>/fix.patch`` per selected case instead.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

import tomllib

SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
UNIT_TEST_RE = re.compile(r"unit(?:\.[a-z0-9_]+)+")
GIT_SHA_RE = re.compile(r"[0-9a-f]{40}")
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
        "live-h264",
        "live-xpra-detach",
        "live-xpra-transport-loss",
        "live-x11-clipboard",
        "live-wayland-keyboard",
        "live-wayland-subsurface",
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
LOCAL_TEST_RE = re.compile(r"cases/[a-z0-9]+(?:-[a-z0-9]+)*/tests/[A-Za-z0-9_./-]+\.py")
CASE_FIELDS = frozenset(
    {"schema", "slug", "kind", "title", "dependencies", "tests", "evidence", "quarantine"}
)
STACK_FIELDS = frozenset({"schema", "slug", "description", "tests"})
# Paths owned by control commits; every other path is product code of a case commit.
CONTROL_PATHS = (
    "AGENTS.md",
    ".gitignore",
    ".github/workflows/",
    ".github/upstream-workflows/",
    "fork-maintenance/",
)
CASE_TRAILER = "Fork-Case"
FIXUP_PREFIXES = ("fixup! ", "squash! ", "amend! ")
SNAPSHOT_MARKER = "selection-source.json"
PATCH_NAME = "fix.patch"
BASE_REFS = ("refs/heads/master", "refs/remotes/origin/master")


class SelectionError(ValueError):
    pass


@dataclass(frozen=True)
class Case:
    slug: str
    kind: str
    manifest_path: Path
    manifest_bytes: bytes
    patch_path: Path
    patch_bytes: bytes
    commit: str
    dependencies: tuple[str, ...]
    tests: tuple[str, ...]
    required_gates: tuple[str, ...]
    quarantined_tests: tuple[str, ...]
    quarantined_tests_by_gate: tuple[tuple[str, tuple[str, ...]], ...]


@dataclass(frozen=True)
class Selection:
    name: str
    kind: str
    manifest_path: Path
    manifest_bytes: bytes
    cases: tuple[Case, ...]
    subjects: tuple[str, ...]
    tests: tuple[str, ...]


@dataclass(frozen=True)
class CaseCommit:
    slug: str
    commit: str
    subject: str
    paths: tuple[str, ...]


@dataclass(frozen=True)
class DevelopMap:
    """The classified fork history ``base..head`` of one checkout."""

    base: str
    head: str
    cases: tuple[CaseCommit, ...]
    control: tuple[str, ...]

    def commit_of(self, slug: str) -> CaseCommit | None:
        return next((case for case in self.cases if case.slug == slug), None)


@dataclass(frozen=True)
class Source:
    """Where case diffs come from: a checkout's commits or a frozen payload."""

    lab_root: Path
    snapshot: dict[str, object] | None
    develop: DevelopMap | None

    @property
    def series(self) -> tuple[str, ...]:
        if self.develop is not None:
            return tuple(case.slug for case in self.develop.cases)
        assert self.snapshot is not None
        return tuple(self.snapshot["series"])  # type: ignore[arg-type]

    def commit(self, slug: str) -> str:
        if self.develop is not None:
            found = self.develop.commit_of(slug)
            if found is None:
                fail(f"case {slug} has no Fork-Case commit in develop")
            return found.commit
        assert self.snapshot is not None
        commits = self.snapshot["commits"]
        assert isinstance(commits, dict)
        value = commits.get(slug)
        if not isinstance(value, str):
            fail(f"case {slug} is not part of the frozen selection")
        return value

    def patch_bytes(self, slug: str, *, alone: bool) -> bytes:
        """The case diff: in stack order, or cherry-picked alone onto the base."""
        if self.develop is not None:
            repo = self.lab_root.parent
            commit = self.commit(slug)
            if alone:
                return case_diff_alone(repo, self.develop.base, commit, slug)
            return case_diff(repo, commit)
        self.commit(slug)
        return require_regular_file(self.lab_root / "cases" / slug / PATCH_NAME, "frozen case patch")


def fail(message: str) -> NoReturn:
    raise SelectionError(message)


def require_regular_file(path: Path, description: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        fail(f"{description} is missing or is not a regular file: {path}")
    return path.read_bytes()


def parse_toml(data: bytes, description: str) -> dict[str, object]:
    try:
        parsed = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        fail(f"invalid {description}: {exc}")
    if not isinstance(parsed, dict):
        fail(f"invalid {description}: expected a table")
    return parsed


def require_slug(value: object, description: str) -> str:
    if not isinstance(value, str) or not SLUG_RE.fullmatch(value):
        fail(f"invalid {description}: {value!r}")
    return value


def require_tests(value: object, description: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, dict):
        fail(f"invalid {description}: missing [tests] table")
    entries = value.get("list")
    if not isinstance(entries, list) or (not entries and not allow_empty):
        fail(f"invalid {description}: tests.list must be a non-empty array")
    result: list[str] = []
    for entry in entries:
        if not isinstance(entry, str) or not (
            UNIT_TEST_RE.fullmatch(entry)
            or entry in SUPPORTED_GATES
            or (
                LOCAL_TEST_RE.fullmatch(entry)
                and ".." not in Path(entry).parts
                and "//" not in entry
                and Path(entry).as_posix() == entry
            )
        ):
            fail(f"invalid {description} test entry: {entry!r}")
        if entry in result:
            fail(f"duplicate {description} test entry: {entry}")
        result.append(entry)
    return tuple(result)


def require_gates(value: object, description: str) -> tuple[str, ...]:
    if not isinstance(value, dict):
        fail(f"invalid {description}: missing [evidence] table")
    entries = value.get("required_gates")
    if not isinstance(entries, list):
        fail(f"invalid {description}: required_gates must be an array")
    result: list[str] = []
    for entry in entries:
        if not isinstance(entry, str) or entry not in SUPPORTED_GATES:
            fail(f"invalid {description} gate: {entry!r}")
        if entry in result:
            fail(f"duplicate {description} gate: {entry}")
        result.append(entry)
    return tuple(result)


def is_control_path(path: str) -> bool:
    return any(
        path.startswith(prefix) if prefix.endswith("/") else path == prefix
        for prefix in CONTROL_PATHS
    )


# --- git mode ---------------------------------------------------------------


def git(repo: Path, *arguments: str, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(
        ("git", "-C", str(repo), *arguments),
        input=input_bytes,
        capture_output=True,
        check=False,
        env={"GIT_NO_LAZY_FETCH": "1", "PATH": "/usr/bin:/bin:/usr/local/bin", "LC_ALL": "C"},
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        fail(f"git {' '.join(arguments[:2])} failed: {detail}")
    return result.stdout


def git_text(repo: Path, *arguments: str) -> str:
    return git(repo, *arguments).decode("utf-8")


DIFF_OPTIONS = (
    "--binary",
    "--full-index",
    "--no-renames",
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--src-prefix=a/",
    "--dst-prefix=b/",
)


def tree_diff(repo: Path, old: str, new: str) -> bytes:
    return git(repo, "-c", "core.quotePath=false", "diff", *DIFF_OPTIONS, old, new, "--")


def case_diff(repo: Path, commit: str) -> bytes:
    """The exact binary-safe diff of one case commit against its parent."""
    return tree_diff(repo, f"{commit}^", commit)


def case_diff_alone(repo: Path, base: str, commit: str, slug: str) -> bytes:
    """The case commit cherry-picked onto the bare base, merged in memory.

    Its own diff carries context from the cases before it; a three-way merge
    applies only its change, and a conflict proves it is not independent.
    """
    result = subprocess.run(
        ("git", "-C", str(repo), "merge-tree", "--write-tree", f"--merge-base={commit}^", base, commit),
        capture_output=True,
        check=False,
        env={"GIT_NO_LAZY_FETCH": "1", "PATH": "/usr/bin:/bin:/usr/local/bin", "LC_ALL": "C"},
    )
    if result.returncode == 1:
        fail(f"case {slug} does not apply alone on the upstream base (its commit conflicts with the base)")
    if result.returncode:
        fail(f"git merge-tree failed for case {slug}: {result.stderr.decode('utf-8', 'replace').strip()}")
    tree = result.stdout.decode().splitlines()[0].strip()
    if not GIT_SHA_RE.fullmatch(tree):
        fail(f"git merge-tree returned no tree for case {slug}")
    return tree_diff(repo, base, tree)


def resolve_base(repo: Path, head: str, base_commit: str | None) -> str:
    if base_commit is not None:
        if not GIT_SHA_RE.fullmatch(base_commit):
            fail(f"invalid base commit: {base_commit!r}")
        ancestor = subprocess.run(
            ("git", "-C", str(repo), "merge-base", "--is-ancestor", base_commit, head),
            check=False,
            capture_output=True,
        )
        if ancestor.returncode:
            fail(f"base {base_commit} is not an ancestor of HEAD")
        return base_commit
    refs = set(git_text(repo, "for-each-ref", "--format=%(refname)", *BASE_REFS).split())
    ref = next((candidate for candidate in BASE_REFS if candidate in refs), None)
    if ref is None:
        fail("no master ref (refs/heads/master or refs/remotes/origin/master) locates the upstream base")
    bases = git_text(repo, "merge-base", "--all", ref, head).split()
    if len(bases) != 1 or not GIT_SHA_RE.fullmatch(bases[0]):
        fail(f"HEAD and {ref} have no single upstream base")
    return bases[0]


def develop_map(repo: Path, base_commit: str | None = None) -> DevelopMap:
    """Classify every fork commit in ``base..HEAD`` and map cases to commits."""
    head = git_text(repo, "rev-parse", "--verify", "HEAD^{commit}").strip()
    base = resolve_base(repo, head, base_commit)
    merges = git_text(repo, "rev-list", "--merges", f"{base}..{head}").split()
    if merges:
        fail(f"develop contains merge commits: {merges}")
    commits = git_text(repo, "rev-list", "--reverse", "--topo-order", f"{base}..{head}").split()
    cases: list[CaseCommit] = []
    control: list[str] = []
    seen: dict[str, str] = {}
    for commit in commits:
        subject = git_text(repo, "log", "-1", "--format=%s", commit).rstrip("\n")
        if subject.startswith(FIXUP_PREFIXES):
            fail(f"pending {subject.split()[0]} commit {commit}: run develop-squash")
        trailers = [
            value.strip()
            for value in git_text(
                repo,
                "log",
                "-1",
                f"--format=%(trailers:key={CASE_TRAILER},valueonly,unfold,separator=%x00)",
                commit,
            ).rstrip("\n").split("\0")
            if value.strip()
        ]
        paths = tuple(
            sorted(
                path
                for path in git_text(
                    repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "--no-renames", f"{commit}^", commit
                ).splitlines()
                if path
            )
        )
        product = tuple(path for path in paths if not is_control_path(path))
        if not trailers:
            if product:
                fail(f"commit {commit} changes product paths without a {CASE_TRAILER} trailer: {list(product)}")
            control.append(commit)
            continue
        if len(trailers) != 1:
            fail(f"commit {commit} has more than one {CASE_TRAILER} trailer")
        slug = require_slug(trailers[0], f"{CASE_TRAILER} trailer of {commit}")
        if len(product) != len(paths):
            fail(f"case commit {commit} ({slug}) also changes control paths")
        # an empty case commit is a refresh placeholder: its case-check fails
        # until it is rebuilt with a fixup or retired with case-drop
        if slug in seen:
            fail(f"case {slug} has more than one commit: {seen[slug]} {commit}")
        seen[slug] = commit
        cases.append(CaseCommit(slug=slug, commit=commit, subject=subject, paths=paths))
    return DevelopMap(base=base, head=head, cases=tuple(cases), control=tuple(control))


def git_mode_repo(lab_root: Path) -> Path:
    repo = lab_root.parent
    toplevel = subprocess.run(
        ("git", "-C", str(lab_root), "rev-parse", "--show-toplevel"),
        capture_output=True,
        check=False,
        text=True,
    )
    if toplevel.returncode or Path(toplevel.stdout.strip()).resolve() != repo.resolve():
        fail(f"lab root is neither a frozen selection nor the fork-maintenance directory of a checkout: {lab_root}")
    if lab_root.name != "fork-maintenance":
        fail(f"lab root of a checkout must be its fork-maintenance directory: {lab_root}")
    return repo


def open_source(lab_root: Path, base_commit: str | None = None) -> Source:
    if lab_root.is_symlink() or not lab_root.is_dir():
        fail(f"lab root is missing or is a symlink: {lab_root}")
    marker = lab_root / SNAPSHOT_MARKER
    if marker.exists() or marker.is_symlink():
        try:
            payload = json.loads(require_regular_file(marker, "selection snapshot marker"))
        except json.JSONDecodeError as exc:
            fail(f"invalid selection snapshot marker: {exc}")
        if (
            not isinstance(payload, dict)
            or set(payload) != {"schema", "base", "head", "series", "commits"}
            or payload.get("schema") != 1
            or not isinstance(payload.get("series"), list)
            or not isinstance(payload.get("commits"), dict)
            or not all(isinstance(item, str) and SLUG_RE.fullmatch(item) for item in payload["series"])
            or not all(
                isinstance(key, str) and isinstance(value, str) and GIT_SHA_RE.fullmatch(value)
                for key, value in payload["commits"].items()
            )
            or not set(payload["commits"]).issubset(payload["series"])
        ):
            fail("selection snapshot marker is inconsistent")
        return Source(lab_root=lab_root, snapshot=payload, develop=None)
    repo = git_mode_repo(lab_root)
    stray = sorted(str(path.relative_to(lab_root)) for path in (lab_root / "cases").glob(f"*/{PATCH_NAME}"))
    if stray:
        fail(f"stored case patches are not allowed; every case is a commit: {stray}")
    return Source(lab_root=lab_root, snapshot=None, develop=develop_map(repo, base_commit))


# --- manifests ---------------------------------------------------------------


def parse_quarantine(
    slug: str, quarantine: object, tests: tuple[str, ...], required_gates: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[tuple[str, tuple[str, ...]], ...]]:
    if not isinstance(quarantine, dict):
        fail(f"test-quarantine case {slug} requires [quarantine]")
    if set(quarantine) != {"modules", "gates"}:
        fail(f"test-quarantine case {slug} quarantine must contain exactly modules and gates")
    modules = quarantine.get("modules")
    if not isinstance(modules, list):
        fail(f"test-quarantine case {slug} requires quarantine.modules")
    quarantined_tests = tuple(
        entry for entry in modules if isinstance(entry, str) and UNIT_TEST_RE.fullmatch(entry)
    )
    if len(quarantined_tests) != len(modules) or len(quarantined_tests) != len(set(quarantined_tests)):
        fail(f"invalid quarantine.modules for {slug}")
    if not set(quarantined_tests).issubset(tests):
        fail(f"quarantined modules are not retained tests for {slug}")
    gates = quarantine.get("gates")
    if not isinstance(gates, dict) or set(gates) != QUARANTINE_GATES:
        fail(f"test-quarantine case {slug} quarantine.gates must contain exactly {QUARANTINE_GATE_NAMES}")
    assigned: set[str] = set()
    by_gate: tuple[tuple[str, tuple[str, ...]], ...] = ()
    for gate in QUARANTINE_GATE_NAMES:
        gate_modules = gates.get(gate)
        if not isinstance(gate_modules, list):
            fail(f"invalid quarantine.gates.{gate} for {slug}")
        parsed = tuple(
            entry for entry in gate_modules if isinstance(entry, str) and UNIT_TEST_RE.fullmatch(entry)
        )
        if len(parsed) != len(gate_modules) or len(parsed) != len(set(parsed)):
            fail(f"invalid quarantine.gates.{gate} for {slug}")
        parsed_set = set(parsed)
        if not parsed_set.issubset(quarantined_tests):
            fail(f"quarantine.gates.{gate} is not a subset of modules for {slug}")
        if parsed != tuple(test for test in quarantined_tests if test in parsed_set):
            fail(f"quarantine.gates.{gate} must preserve quarantine.modules order for {slug}")
        assigned.update(parsed)
        by_gate += ((gate, parsed),)
    if assigned != set(quarantined_tests):
        fail(f"every quarantined module must be assigned to at least one gate for {slug}")
    if quarantined_tests and set(required_gates) != QUARANTINE_GATES:
        fail(f"test-quarantine case {slug} must require all quarantine gates")
    if not quarantined_tests and (tests or required_gates):
        fail(f"inactive test-quarantine case {slug} must keep empty tests and gates")
    return quarantined_tests, by_gate


def read_manifest(lab_root: Path, slug: str) -> tuple[Path, bytes, dict[str, object]]:
    slug = require_slug(slug, "case slug")
    cases_dir = lab_root / "cases"
    if cases_dir.is_symlink() or not cases_dir.is_dir():
        fail(f"cases directory is missing or is a symlink: {cases_dir}")
    case_dir = cases_dir / slug
    if case_dir.is_symlink() or not case_dir.is_dir():
        fail(f"case directory is missing or is a symlink: {slug}")
    manifest_path = case_dir / "case.toml"
    manifest_bytes = require_regular_file(manifest_path, "case manifest")
    manifest = parse_toml(manifest_bytes, f"case manifest {slug}")
    if manifest.get("schema") != 2:
        fail(f"unsupported case manifest schema (expected 2): {slug}")
    unknown = set(manifest) - CASE_FIELDS
    if unknown:
        fail(f"case manifest {slug} has unsupported fields: {sorted(unknown)}")
    if require_slug(manifest.get("slug"), "manifest case slug") != slug:
        fail(f"case manifest slug does not match its directory: {slug}")
    title = manifest.get("title")
    if not isinstance(title, str) or not title.strip():
        fail(f"case manifest {slug} requires a title")
    return manifest_path, manifest_bytes, manifest


def read_case(source: Source, slug: str, *, alone: bool) -> Case:
    lab_root = source.lab_root
    manifest_path, manifest_bytes, manifest = read_manifest(lab_root, slug)
    kind = manifest.get("kind", "production")
    if not isinstance(kind, str) or kind not in CASE_KINDS:
        fail(f"invalid case kind for {slug}: {kind!r}")
    if kind == "test-quarantine" and slug != TEST_QUARANTINE_SLUG:
        fail(f"only {TEST_QUARANTINE_SLUG} may use kind=test-quarantine")
    if slug == TEST_QUARANTINE_SLUG and kind != "test-quarantine":
        fail(f"{TEST_QUARANTINE_SLUG} must use kind=test-quarantine")
    tests = require_tests(manifest.get("tests"), f"case {slug}", allow_empty=kind == "test-quarantine")
    required_gates = require_gates(manifest.get("evidence"), f"case {slug}")
    for test in tests:
        if LOCAL_TEST_RE.fullmatch(test):
            if not test.startswith(f"cases/{slug}/tests/"):
                fail(f"case {slug} references another case's local test: {test}")
            require_regular_file(lab_root / test, "case local test")
    dependencies_value = manifest.get("dependencies")
    if not isinstance(dependencies_value, list):
        fail(f"invalid case dependencies: {slug}")
    dependencies = tuple(require_slug(item, f"dependency of case {slug}") for item in dependencies_value)
    if len(dependencies) != len(set(dependencies)) or slug in dependencies:
        fail(f"invalid case dependencies: {slug}")
    quarantined_tests: tuple[str, ...] = ()
    quarantined_tests_by_gate: tuple[tuple[str, tuple[str, ...]], ...] = ()
    if kind == "production":
        if manifest.get("quarantine") is not None:
            fail(f"production case {slug} may not declare [quarantine]")
        if set(required_gates).intersection(QUARANTINE_GATES):
            fail(f"production case {slug} may not declare quarantine gates")
    else:
        if dependencies:
            fail(f"test-quarantine case {slug} may not have dependencies")
        quarantined_tests, quarantined_tests_by_gate = parse_quarantine(
            slug, manifest.get("quarantine"), tests, required_gates
        )
        if not quarantined_tests:
            fail(f"inactive test-quarantine case {slug} is not selectable")
    commit = source.commit(slug)
    case = Case(
        slug=slug,
        kind=kind,
        manifest_path=manifest_path,
        manifest_bytes=manifest_bytes,
        patch_path=manifest_path.parent / PATCH_NAME,
        patch_bytes=source.patch_bytes(slug, alone=alone),
        commit=commit,
        dependencies=dependencies,
        tests=tests,
        required_gates=required_gates,
        quarantined_tests=quarantined_tests,
        quarantined_tests_by_gate=quarantined_tests_by_gate,
    )
    paths = tuple(path.as_posix() for path in patch_source_paths(case))
    if any(is_control_path(path) for path in paths):
        fail(f"case {slug} changes control paths: {paths}")
    if kind == "test-quarantine":
        expected = tuple(
            sorted(f"tests/unittests/{item.replace('.', '/')}.py" for item in quarantined_tests)
        )
        if paths != expected:
            fail(f"test-quarantine paths do not match modules for {slug}: {expected}")
    return case


def read_stack_manifest(lab_root: Path, slug: str) -> tuple[Path, bytes, dict[str, object]]:
    stacks_dir = lab_root / "stacks"
    if stacks_dir.is_symlink() or not stacks_dir.is_dir():
        fail(f"stacks directory is missing or is a symlink: {stacks_dir}")
    manifest_path = stacks_dir / f"{slug}.toml"
    manifest_bytes = require_regular_file(manifest_path, "stack manifest")
    manifest = parse_toml(manifest_bytes, f"stack manifest {slug}")
    if manifest.get("schema") != 2:
        fail(f"unsupported stack manifest schema (expected 2): {slug}")
    unknown = set(manifest) - STACK_FIELDS
    if unknown:
        fail(f"stack manifest {slug} has unsupported fields: {sorted(unknown)}")
    if require_slug(manifest.get("slug"), "manifest stack slug") != slug:
        fail(f"stack manifest slug does not match its filename: {slug}")
    return manifest_path, manifest_bytes, manifest


def load_selection(source: Source, name: str) -> Selection:
    lab_root = source.lab_root
    match = re.fullmatch(r"(cases|stacks)/([a-z0-9]+(?:-[a-z0-9]+)*)", name)
    if not match or not SLUG_RE.fullmatch(match.group(2)):
        fail(f"invalid selection: {name}")
    kind, slug = match.groups()
    if kind == "cases":
        case = read_case(source, slug, alone=True)
        return Selection(
            name=name,
            kind="case",
            manifest_path=case.manifest_path,
            manifest_bytes=case.manifest_bytes,
            cases=(case,),
            subjects=(case.slug,),
            tests=case.tests,
        )
    manifest_path, manifest_bytes, manifest = read_stack_manifest(lab_root, slug)
    case_slugs = source.series
    if not case_slugs:
        fail(f"stack {slug} has no case commits")
    cases = tuple(read_case(source, case_slug, alone=False) for case_slug in case_slugs)
    preceding: set[str] = set()
    for case in cases:
        missing = tuple(dep for dep in case.dependencies if dep not in preceding)
        if missing:
            fail(f"case {case.slug} must come after its dependencies: {', '.join(missing)}")
        preceding.add(case.slug)
    return Selection(
        name=name,
        kind="stack",
        manifest_path=manifest_path,
        manifest_bytes=manifest_bytes,
        cases=cases,
        subjects=case_slugs,
        tests=require_tests(manifest.get("tests"), f"stack {slug}"),
    )


def iter_unit_tests(selection: Selection) -> Iterator[str]:
    seen: set[str] = set()
    for case in selection.cases:
        for test in case.tests:
            if UNIT_TEST_RE.fullmatch(test) and test not in seen:
                seen.add(test)
                yield test
    for test in selection.tests:
        if UNIT_TEST_RE.fullmatch(test) and test not in seen:
            seen.add(test)
            yield test


def iter_gates(selection: Selection) -> Iterator[str]:
    seen: set[str] = set()
    for case in selection.cases:
        for test in case.tests:
            if test in SUPPORTED_GATES and test not in seen:
                seen.add(test)
                yield test
        for gate in case.required_gates:
            if gate not in seen:
                seen.add(gate)
                yield gate
    for test in selection.tests:
        if test in SUPPORTED_GATES and test not in seen:
            seen.add(test)
            yield test


def iter_required_gates(selection: Selection) -> Iterator[str]:
    """Yield only gates declared by case evidence, never tests.list entries."""
    seen: set[str] = set()
    for case in selection.cases:
        for gate in case.required_gates:
            if gate not in seen:
                seen.add(gate)
                yield gate


def iter_quarantined_tests(selection: Selection, gate: str | None = None) -> Iterator[str]:
    if gate is not None and gate not in QUARANTINE_GATES:
        fail(f"invalid quarantine gate: {gate}")
    seen: set[str] = set()
    for case in selection.cases:
        tests = case.quarantined_tests
        if gate is not None:
            tests = dict(case.quarantined_tests_by_gate).get(gate, ())
        for test in tests:
            if test not in seen:
                seen.add(test)
                yield test


def iter_case_test_files(case: Case, lab_root: Path) -> Iterator[tuple[Path, bytes]]:
    tests_dir = case.manifest_path.parent / "tests"
    if not tests_dir.exists():
        return
    if tests_dir.is_symlink() or not tests_dir.is_dir():
        fail(f"case tests path is not a regular directory: {tests_dir}")
    for path in sorted(tests_dir.rglob("*")):
        relative = path.relative_to(lab_root)
        if "__pycache__" in relative.parts or path.suffix in (".pyc", ".pyo"):
            continue
        if path.is_symlink():
            fail(f"case test path is a symlink: {relative}")
        if path.is_file():
            yield relative, path.read_bytes()
        elif not path.is_dir():
            fail(f"case test path is not regular: {relative}")


def series_entry(selection: Selection) -> tuple[str, bytes]:
    """Bind the stack order, which no manifest records any more."""
    relative = selection.manifest_path.with_suffix(".series").name
    return f"stacks/{relative}", "".join(f"{slug}\n" for slug in selection.subjects).encode()


def selection_digest(selection: Selection, lab_root: Path) -> str:
    digest = hashlib.sha256()
    entries: list[tuple[str, bytes]] = [
        (str(selection.manifest_path.relative_to(lab_root)), selection.manifest_bytes)
    ]
    if selection.kind == "stack":
        entries.append(series_entry(selection))
    for case in selection.cases:
        entries.extend(
            (
                (str(case.manifest_path.relative_to(lab_root)), case.manifest_bytes),
                (str(case.patch_path.relative_to(lab_root)), case.patch_bytes),
            )
        )
        entries.extend((str(path), data) for path, data in iter_case_test_files(case, lab_root))
    for relative, data in sorted(set(entries), key=lambda item: item[0]):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()


def patch_source_paths(case: Case) -> tuple[Path, ...]:
    found: set[Path] = set()
    for raw_line in case.patch_bytes.splitlines():
        if not raw_line.startswith((b"--- ", b"+++ ")):
            continue
        raw_name = raw_line[4:].split(b"\t", 1)[0]
        if raw_name == b"/dev/null":
            continue
        if not raw_name.startswith((b"a/", b"b/")):
            fail(f"case {case.slug} has an unsupported patch path")
        try:
            name = raw_name[2:].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SelectionError(f"case {case.slug} has a non-UTF-8 patch path") from exc
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or path.as_posix() != name or not path.parts:
            fail(f"case {case.slug} has an unsafe patch path: {name!r}")
        found.add(path)
    if not found:
        for raw_line in case.patch_bytes.splitlines():
            # binary-only or mode-only diffs name their paths in the header
            if raw_line.startswith(b"diff --git a/"):
                name = raw_line.split(b" b/", 1)[-1].decode("utf-8", "replace")
                found.add(Path(name))
    if not found:
        fail(f"case {case.slug} patch contains no source paths")
    return tuple(sorted(found))


def run_git_apply(tree: Path, patch_bytes: bytes, *arguments: str) -> int:
    return subprocess.run(
        ("git", "apply", *arguments, "--whitespace=error-all", "-"),
        cwd=tree,
        input=patch_bytes,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode


def omitted_dependencies(selection: Selection, source: Source) -> tuple[tuple[str, Case], ...]:
    selected = {case.slug for case in selection.cases}
    result: list[tuple[str, Case]] = []
    seen: set[tuple[str, str]] = set()
    for case in selection.cases:
        for dependency in case.dependencies:
            key = (case.slug, dependency)
            if dependency in selected or key in seen:
                continue
            seen.add(key)
            result.append((case.slug, read_case(source, dependency, alone=True)))
    return tuple(result)


def resolve_selection(
    selection: Selection,
    source: Source,
    source_tree: Path,
    source_commit: str,
) -> dict[str, object]:
    lab_root = source.lab_root
    if not GIT_SHA_RE.fullmatch(source_commit):
        fail(f"invalid source commit: {source_commit!r}")
    if source_tree.is_symlink() or not source_tree.is_dir():
        fail(f"source tree is missing or is a symlink: {source_tree}")
    source_tree = source_tree.resolve(strict=True)
    base_dependencies = omitted_dependencies(selection, source)
    source_cases = (*selection.cases, *(case for _consumer, case in base_dependencies))
    all_paths = sorted({path for case in source_cases for path in patch_source_paths(case)})
    with tempfile.TemporaryDirectory(prefix="xpra-selection-") as raw:
        scratch = Path(raw)
        for relative in all_paths:
            origin = source_tree / relative
            if origin.is_symlink():
                fail(f"source path is a symlink: {relative}")
            if not origin.exists():
                continue
            if not origin.is_file() or not origin.resolve().is_relative_to(source_tree):
                fail(f"source path is not a safe regular file: {relative}")
            destination = scratch / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(origin, destination)

        dependency_entries: list[dict[str, str]] = []
        for consumer, dependency in base_dependencies:
            forward = run_git_apply(scratch, dependency.patch_bytes, "--check") == 0
            reverse = run_git_apply(scratch, dependency.patch_bytes, "--reverse", "--check") == 0
            if forward or not reverse:
                fail(f"case {consumer} has unresolved base dependency {dependency.slug} at {source_commit}")
            dependency_entries.append(
                {
                    "case": consumer,
                    "dependency": dependency.slug,
                    "patch": dependency.patch_path.relative_to(lab_root).as_posix(),
                    "patch_sha256": hashlib.sha256(dependency.patch_bytes).hexdigest(),
                    "status": "already-present",
                }
            )

        entries: list[dict[str, str]] = []
        for case in selection.cases:
            forward = run_git_apply(scratch, case.patch_bytes, "--check") == 0
            reverse = run_git_apply(scratch, case.patch_bytes, "--reverse", "--check") == 0
            if forward == reverse:
                state = "ambiguous" if forward else "diverged"
                fail(f"case {case.slug} is {state} at base {source_commit}; rework its commit")
            status = "apply" if forward else "already-present"
            if forward and (
                run_git_apply(scratch, case.patch_bytes) != 0
                or run_git_apply(scratch, case.patch_bytes, "--reverse", "--check") != 0
            ):
                fail(f"case {case.slug} failed deterministic application")
            entries.append(
                {
                    "case": case.slug,
                    "patch": case.patch_path.relative_to(lab_root).as_posix(),
                    "patch_sha256": hashlib.sha256(case.patch_bytes).hexdigest(),
                    "status": status,
                }
            )

    payload: dict[str, object] = {
        "schema": 1,
        "source_commit": source_commit,
        "selection": selection.name,
        "selection_sha256": selection_digest(selection, lab_root),
        "declared_cases": [case.slug for case in selection.cases],
        "base_dependencies": dependency_entries,
        "patches": entries,
        "applied_cases": [entry["case"] for entry in entries if entry["status"] == "apply"],
        "already_present_cases": [
            entry["case"] for entry in entries if entry["status"] == "already-present"
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["resolution_sha256"] = hashlib.sha256(canonical).hexdigest()
    return payload


def validate_resolution_document(
    selection: Selection,
    source: Source,
    document: object,
    source_commit: str,
    expected_selection_digest: str,
) -> str:
    lab_root = source.lab_root
    if not isinstance(document, dict):
        fail("selection resolution must be a JSON object")
    expected_keys = {
        "already_present_cases",
        "applied_cases",
        "base_dependencies",
        "declared_cases",
        "patches",
        "resolution_sha256",
        "schema",
        "selection",
        "selection_sha256",
        "source_commit",
    }
    if set(document) != expected_keys:
        fail("selection resolution fields are inconsistent")
    if not GIT_SHA_RE.fullmatch(source_commit):
        fail(f"invalid source commit: {source_commit!r}")
    current_digest = selection_digest(selection, lab_root)
    if expected_selection_digest != current_digest:
        fail("expected selection digest is stale")
    if (
        document.get("schema") != 1
        or document.get("source_commit") != source_commit
        or document.get("selection") != selection.name
        or document.get("selection_sha256") != current_digest
        or document.get("declared_cases") != [case.slug for case in selection.cases]
    ):
        fail("selection resolution provenance is inconsistent")
    expected_dependencies = [
        {
            "case": consumer,
            "dependency": dependency.slug,
            "patch": dependency.patch_path.relative_to(lab_root).as_posix(),
            "patch_sha256": hashlib.sha256(dependency.patch_bytes).hexdigest(),
            "status": "already-present",
        }
        for consumer, dependency in omitted_dependencies(selection, source)
    ]
    if document.get("base_dependencies") != expected_dependencies:
        fail("selection resolution base dependencies are inconsistent")
    entries = document.get("patches")
    if not isinstance(entries, list) or len(entries) != len(selection.cases):
        fail("selection resolution patch series is inconsistent")
    applied: list[str] = []
    already_present: list[str] = []
    for entry, case in zip(entries, selection.cases, strict=True):
        expected = {
            "case": case.slug,
            "patch": case.patch_path.relative_to(lab_root).as_posix(),
            "patch_sha256": hashlib.sha256(case.patch_bytes).hexdigest(),
        }
        if (
            not isinstance(entry, dict)
            or set(entry) != {*expected, "status"}
            or any(entry.get(key) != value for key, value in expected.items())
        ):
            fail("selection resolution patch identity is inconsistent")
        status = entry.get("status")
        if status == "apply":
            applied.append(case.slug)
        elif status == "already-present":
            already_present.append(case.slug)
        else:
            fail("selection resolution has an invalid patch status")
    if document.get("applied_cases") != applied or document.get("already_present_cases") != already_present:
        fail("selection resolution effective series is inconsistent")
    recorded_digest = document.get("resolution_sha256")
    payload = dict(document)
    payload.pop("resolution_sha256", None)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    actual_digest = hashlib.sha256(canonical).hexdigest()
    if recorded_digest != actual_digest:
        fail("selection resolution digest is inconsistent")
    return actual_digest


def snapshot(selection: Selection, source: Source, destination: Path) -> None:
    """Freeze manifests, local tests and each selected commit's diff."""
    lab_root = source.lab_root
    if destination.exists():
        fail(f"snapshot destination already exists: {destination}")
    destination.mkdir(parents=True, mode=0o700)
    all_cases = (*selection.cases, *(case for _consumer, case in omitted_dependencies(selection, source)))
    paths: list[tuple[Path, bytes]] = [
        (selection.manifest_path.relative_to(lab_root), selection.manifest_bytes)
    ]
    for case in all_cases:
        paths.extend(
            (
                (case.manifest_path.relative_to(lab_root), case.manifest_bytes),
                (case.patch_path.relative_to(lab_root), case.patch_bytes),
            )
        )
        paths.extend(iter_case_test_files(case, lab_root))
    written: set[Path] = set()
    for relative, data in paths:
        if relative in written:
            continue
        written.add(relative)
        output = destination / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(data)
    if source.develop is not None:
        base, head = source.develop.base, source.develop.head
    else:
        assert source.snapshot is not None
        base, head = str(source.snapshot["base"]), str(source.snapshot["head"])
    marker = {
        "schema": 1,
        "base": base,
        "head": head,
        "series": list(source.series),
        "commits": {case.slug: case.commit for case in all_cases},
    }
    (destination / SNAPSHOT_MARKER).write_text(json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def develop_map_document(source: Source) -> dict[str, object]:
    if source.develop is None:
        fail("develop-map needs the fork-maintenance directory of a checkout")
    develop = source.develop
    return {
        "schema": 1,
        "base": develop.base,
        "head": develop.head,
        "cases": [
            {"slug": case.slug, "commit": case.commit, "subject": case.subject, "paths": list(case.paths)}
            for case in develop.cases
        ],
        "control": list(develop.control),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lab-root", type=Path, required=True)
    parser.add_argument("--selection")
    parser.add_argument("--base-commit")
    parser.add_argument("--gate", choices=QUARANTINE_GATE_NAMES)
    parser.add_argument(
        "action",
        choices=(
            "validate",
            "kind",
            "cases",
            "patches",
            "local-tests",
            "unit-tests",
            "quarantined-tests",
            "gates",
            "required-gates",
            "digest",
            "resolve",
            "resolution-patches",
            "snapshot",
            "verify-resolution",
            "develop-map",
        ),
    )
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--source-tree", type=Path)
    parser.add_argument("--source-commit")
    parser.add_argument("--resolution", type=Path)
    parser.add_argument("--digest-file", type=Path)
    parser.add_argument("--selection-sha256")
    args = parser.parse_args()

    try:
        if args.gate is not None and args.action != "quarantined-tests":
            fail("--gate is valid only with quarantined-tests")
        if args.lab_root.is_symlink():
            fail(f"lab root is a symlink: {args.lab_root}")
        lab_root = args.lab_root.resolve(strict=True)
        source = open_source(lab_root, args.base_commit)
        if args.action == "develop-map":
            print(json.dumps(develop_map_document(source), indent=2, sort_keys=True))
            return 0
        if args.selection is None:
            fail("--selection is required")
        selection = load_selection(source, args.selection)
        if args.action == "kind":
            print(selection.kind)
        elif args.action == "cases":
            for subject in selection.subjects:
                print(subject)
        elif args.action == "patches":
            for case in selection.cases:
                print(case.patch_path.relative_to(lab_root))
        elif args.action == "local-tests":
            seen: set[str] = set()
            tests = (*selection.tests, *(test for case in selection.cases for test in case.tests))
            for test in tests:
                if LOCAL_TEST_RE.fullmatch(test) and test not in seen:
                    seen.add(test)
                    print(test)
        elif args.action == "unit-tests":
            for test in iter_unit_tests(selection):
                print(test)
        elif args.action == "quarantined-tests":
            for test in iter_quarantined_tests(selection, args.gate):
                print(test)
        elif args.action == "gates":
            for gate in iter_gates(selection):
                print(gate)
        elif args.action == "required-gates":
            for gate in iter_required_gates(selection):
                print(gate)
        elif args.action == "digest":
            print(selection_digest(selection, lab_root))
        elif args.action == "resolve":
            if args.source_tree is None or args.source_commit is None:
                fail("--source-tree and --source-commit are required for resolve")
            resolution = resolve_selection(selection, source, args.source_tree, args.source_commit)
            print(json.dumps(resolution, indent=2, sort_keys=True))
        elif args.action in {"resolution-patches", "verify-resolution"}:
            if (
                args.resolution is None
                or args.digest_file is None
                or args.source_commit is None
                or args.selection_sha256 is None
            ):
                fail(
                    "--resolution, --digest-file, --source-commit, and "
                    "--selection-sha256 are required for verify-resolution"
                )
            resolution_bytes = require_regular_file(args.resolution, "selection resolution")
            digest_bytes = require_regular_file(args.digest_file, "selection resolution digest")
            try:
                document = json.loads(resolution_bytes)
                recorded_digest = digest_bytes.decode("ascii")
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                fail(f"invalid selection resolution output: {exc}")
            digest = validate_resolution_document(
                selection, source, document, args.source_commit, args.selection_sha256
            )
            if recorded_digest != f"{digest}\n":
                fail("selection resolution digest file is inconsistent")
            if args.action == "verify-resolution":
                print(digest)
            else:
                entries = document["patches"]
                print("count", len(entries), sep="\t")
                for index, entry in enumerate(entries):
                    print(
                        index,
                        entry["case"],
                        entry["status"],
                        entry["patch"],
                        entry["patch_sha256"],
                        sep="\t",
                    )
        elif args.action == "snapshot":
            if args.destination is None:
                fail("--destination is required for snapshot")
            snapshot(selection, source, args.destination)
    except (OSError, SelectionError) as exc:
        print(f"selection error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
