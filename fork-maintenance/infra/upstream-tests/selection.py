#!/usr/bin/env python3
"""Validate and freeze an atomic fork-maintenance case or integration stack."""

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
QUARANTINE_GATES = frozenset(
    {"quarantine", "quarantine-cython", "quarantine-no-compat"}
)
LOCAL_TEST_RE = re.compile(
    r"(?:cases|verifications)/[a-z0-9]+(?:-[a-z0-9]+)*/tests/[A-Za-z0-9_./-]+\.py"
)


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
    dependencies: tuple[str, ...]
    tests: tuple[str, ...]
    required_gates: tuple[str, ...]
    quarantined_tests: tuple[str, ...]


@dataclass(frozen=True)
class Selection:
    name: str
    kind: str
    manifest_path: Path
    manifest_bytes: bytes
    cases: tuple[Case, ...]
    subjects: tuple[str, ...]
    tests: tuple[str, ...]


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


def require_tests(value: object, description: str) -> tuple[str, ...]:
    if not isinstance(value, dict):
        fail(f"invalid {description}: missing [tests] table")
    entries = value.get("list")
    if not isinstance(entries, list) or not entries:
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


def require_paths(value: object, description: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        fail(f"invalid {description}: paths must be a non-empty array")
    result: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            fail(f"invalid {description} path: {entry!r}")
        path = Path(entry)
        if (
            not entry
            or path.is_absolute()
            or ".." in path.parts
            or path.as_posix() != entry
            or entry == "fork-maintenance"
            or entry.startswith("fork-maintenance/")
        ):
            fail(f"invalid {description} path: {entry!r}")
        if entry in result:
            fail(f"duplicate {description} path: {entry}")
        result.append(entry)
    return tuple(result)


def read_case(lab_root: Path, slug: str) -> Case:
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
    if manifest.get("schema") != 1:
        fail(f"unsupported case manifest schema: {slug}")
    if require_slug(manifest.get("slug"), "manifest case slug") != slug:
        fail(f"case manifest slug does not match its directory: {slug}")
    kind = manifest.get("kind", "production")
    if not isinstance(kind, str) or kind not in CASE_KINDS:
        fail(f"invalid case kind for {slug}: {kind!r}")
    tests = require_tests(manifest.get("tests"), f"case {slug}")
    required_gates = require_gates(manifest.get("evidence"), f"case {slug}")
    declared_paths = require_paths(manifest.get("paths"), f"case {slug}")
    for test in tests:
        if LOCAL_TEST_RE.fullmatch(test):
            if not test.startswith(f"cases/{slug}/tests/"):
                fail(f"case {slug} references another case's local test: {test}")
            require_regular_file(lab_root / test, "case local test")
    dependencies_value = manifest.get("dependencies")
    if not isinstance(dependencies_value, list):
        fail(f"invalid case dependencies: {slug}")
    dependencies = tuple(
        require_slug(item, f"dependency of case {slug}") for item in dependencies_value
    )
    if len(dependencies) != len(set(dependencies)) or slug in dependencies:
        fail(f"invalid case dependencies: {slug}")
    patch_digest = manifest.get("patch_sha256")
    if not isinstance(patch_digest, str) or not re.fullmatch(
        r"[0-9a-f]{64}", patch_digest
    ):
        fail(f"invalid patch_sha256: {slug}")
    patch_path = case_dir / "fix.patch"
    patch_bytes = require_regular_file(patch_path, "case patch")
    actual_digest = hashlib.sha256(patch_bytes).hexdigest()
    if actual_digest != patch_digest:
        fail(
            f"case patch digest mismatch for {slug}: "
            f"manifest={patch_digest} actual={actual_digest}"
        )
    quarantine = manifest.get("quarantine")
    quarantined_tests: tuple[str, ...] = ()
    if kind == "production":
        if quarantine is not None:
            fail(f"production case {slug} may not declare [quarantine]")
        if set(required_gates).intersection(QUARANTINE_GATES):
            fail(f"production case {slug} may not declare quarantine gates")
    else:
        if dependencies:
            fail(f"test-quarantine case {slug} may not have dependencies")
        if not isinstance(quarantine, dict):
            fail(f"test-quarantine case {slug} requires [quarantine]")
        modules = quarantine.get("modules")
        if not isinstance(modules, list) or not modules:
            fail(f"test-quarantine case {slug} requires quarantine.modules")
        quarantined_tests = tuple(
            entry
            for entry in modules
            if isinstance(entry, str) and UNIT_TEST_RE.fullmatch(entry)
        )
        if len(quarantined_tests) != len(modules) or len(quarantined_tests) != len(
            set(quarantined_tests)
        ):
            fail(f"invalid quarantine.modules for {slug}")
        if not set(quarantined_tests).issubset(tests):
            fail(f"quarantined modules are not retained tests for {slug}")
        if set(required_gates) != QUARANTINE_GATES:
            fail(f"test-quarantine case {slug} must require all quarantine gates")
    case = Case(
        slug=slug,
        kind=kind,
        manifest_path=manifest_path,
        manifest_bytes=manifest_bytes,
        patch_path=patch_path,
        patch_bytes=patch_bytes,
        dependencies=dependencies,
        tests=tests,
        required_gates=required_gates,
        quarantined_tests=quarantined_tests,
    )
    paths = tuple(path.as_posix() for path in patch_source_paths(case))
    if tuple(sorted(declared_paths)) != paths:
        fail(f"case manifest paths do not match patch for {slug}: {paths}")
    if kind == "test-quarantine":
        expected = tuple(
            sorted(f"tests/unittests/{item.replace('.', '/')}.py" for item in quarantined_tests)
        )
        if paths != expected:
            fail(f"test-quarantine paths do not match modules for {slug}: {expected}")
    return case


def read_verification(lab_root: Path, slug: str) -> tuple[Case, tuple[str, ...]]:
    slug = require_slug(slug, "verification slug")
    verification_dir = lab_root / "verifications" / slug
    if verification_dir.is_symlink() or not verification_dir.is_dir():
        fail(f"verification directory is missing or is a symlink: {slug}")
    manifest_path = verification_dir / "verification.toml"
    manifest_bytes = require_regular_file(manifest_path, "verification manifest")
    manifest = parse_toml(manifest_bytes, f"verification manifest {slug}")
    if manifest.get("schema") != 1:
        fail(f"unsupported verification manifest schema: {slug}")
    if require_slug(manifest.get("slug"), "manifest verification slug") != slug:
        fail(f"verification manifest slug does not match its directory: {slug}")
    subjects_value = manifest.get("subjects")
    if not isinstance(subjects_value, list) or not subjects_value:
        fail(f"invalid verification subjects: {slug}")
    subjects = tuple(
        require_slug(item, f"subject of verification {slug}") for item in subjects_value
    )
    if len(subjects) != len(set(subjects)):
        fail(f"duplicate verification subject: {slug}")
    tests = require_tests(manifest.get("tests"), f"verification {slug}")
    required_gates = require_gates(manifest.get("evidence"), f"verification {slug}")
    for test in tests:
        if LOCAL_TEST_RE.fullmatch(test):
            if not test.startswith(f"verifications/{slug}/tests/"):
                fail(f"verification {slug} references an external local test: {test}")
            require_regular_file(lab_root / test, "verification local test")
    patch_digest = manifest.get("patch_sha256")
    if not isinstance(patch_digest, str) or not re.fullmatch(
        r"[0-9a-f]{64}", patch_digest
    ):
        fail(f"invalid verification patch_sha256: {slug}")
    patch_path = verification_dir / "tests.patch"
    patch_bytes = require_regular_file(patch_path, "verification test patch")
    actual_digest = hashlib.sha256(patch_bytes).hexdigest()
    if actual_digest != patch_digest:
        fail(
            f"verification patch digest mismatch for {slug}: "
            f"manifest={patch_digest} actual={actual_digest}"
        )
    verification = Case(
        slug=slug,
        kind="verification",
        manifest_path=manifest_path,
        manifest_bytes=manifest_bytes,
        patch_path=patch_path,
        patch_bytes=patch_bytes,
        dependencies=(),
        tests=tests,
        required_gates=required_gates,
        quarantined_tests=(),
    )
    if any(
        not path.parts or path.parts[0] != "tests"
        for path in patch_source_paths(verification)
    ):
        fail(f"verification {slug} patch may only modify tests/")
    return verification, subjects


def load_selection(lab_root: Path, name: str) -> Selection:
    if lab_root.is_symlink() or not lab_root.is_dir():
        fail(f"lab root is missing or is a symlink: {lab_root}")
    match = re.fullmatch(
        r"(cases|stacks|verifications)/([a-z0-9]+(?:-[a-z0-9]+)*)", name
    )
    if not match or not SLUG_RE.fullmatch(match.group(2)):
        fail(f"invalid selection: {name}")
    kind, slug = match.groups()
    if kind == "cases":
        case = read_case(lab_root, slug)
        return Selection(
            name=name,
            kind="case",
            manifest_path=case.manifest_path,
            manifest_bytes=case.manifest_bytes,
            cases=(case,),
            subjects=(case.slug,),
            tests=case.tests,
        )

    if kind == "verifications":
        verification, subjects = read_verification(lab_root, slug)
        return Selection(
            name=name,
            kind="verification",
            manifest_path=verification.manifest_path,
            manifest_bytes=verification.manifest_bytes,
            cases=(verification,),
            subjects=subjects,
            tests=verification.tests,
        )

    stacks_dir = lab_root / "stacks"
    if stacks_dir.is_symlink() or not stacks_dir.is_dir():
        fail(f"stacks directory is missing or is a symlink: {stacks_dir}")
    manifest_path = stacks_dir / f"{slug}.toml"
    manifest_bytes = require_regular_file(manifest_path, "stack manifest")
    manifest = parse_toml(manifest_bytes, f"stack manifest {slug}")
    if manifest.get("schema") != 1:
        fail(f"unsupported stack manifest schema: {slug}")
    if require_slug(manifest.get("slug"), "manifest stack slug") != slug:
        fail(f"stack manifest slug does not match its filename: {slug}")
    series = manifest.get("series")
    if not isinstance(series, list) or not series:
        fail(f"invalid stack series: {slug}")
    case_slugs = tuple(require_slug(item, "stack case slug") for item in series)
    if len(case_slugs) != len(set(case_slugs)):
        fail(f"duplicate case in stack series: {slug}")
    cases = tuple(read_case(lab_root, case_slug) for case_slug in case_slugs)
    preceding: set[str] = set()
    selected = set(case_slugs)
    for case in cases:
        missing = tuple(
            dep for dep in case.dependencies if dep in selected and dep not in preceding
        )
        if missing:
            fail(
                f"stack {slug} must place dependencies before {case.slug}: "
                f"{', '.join(missing)}"
            )
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


def iter_quarantined_tests(selection: Selection) -> Iterator[str]:
    seen: set[str] = set()
    for case in selection.cases:
        for test in case.quarantined_tests:
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


def selection_digest(selection: Selection, lab_root: Path) -> str:
    digest = hashlib.sha256()
    entries: list[tuple[str, bytes]] = [
        (str(selection.manifest_path.relative_to(lab_root)), selection.manifest_bytes)
    ]
    for case in selection.cases:
        entries.extend(
            (
                (str(case.manifest_path.relative_to(lab_root)), case.manifest_bytes),
                (str(case.patch_path.relative_to(lab_root)), case.patch_bytes),
            )
        )
        entries.extend(
            (str(path), data) for path, data in iter_case_test_files(case, lab_root)
        )
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
            raise SelectionError(
                f"case {case.slug} has a non-UTF-8 patch path"
            ) from exc
        path = Path(name)
        if (
            path.is_absolute()
            or ".." in path.parts
            or path.as_posix() != name
            or not path.parts
        ):
            fail(f"case {case.slug} has an unsafe patch path: {name!r}")
        found.add(path)
    if not found:
        fail(f"case {case.slug} patch contains no source paths")
    return tuple(sorted(found))


def run_git_apply(tree: Path, patch: Path, *arguments: str) -> int:
    return subprocess.run(
        (
            "git",
            "apply",
            *arguments,
            "--whitespace=error-all",
            str(patch),
        ),
        cwd=tree,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode


def omitted_dependencies(
    selection: Selection,
    lab_root: Path,
) -> tuple[tuple[str, Case], ...]:
    selected = {case.slug for case in selection.cases}
    result: list[tuple[str, Case]] = []
    seen: set[tuple[str, str]] = set()
    for case in selection.cases:
        for dependency in case.dependencies:
            key = (case.slug, dependency)
            if dependency in selected or key in seen:
                continue
            seen.add(key)
            result.append((case.slug, read_case(lab_root, dependency)))
    return tuple(result)


def resolve_selection(
    selection: Selection,
    lab_root: Path,
    source_tree: Path,
    source_commit: str,
) -> dict[str, object]:
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        fail(f"invalid source commit: {source_commit!r}")
    if source_tree.is_symlink() or not source_tree.is_dir():
        fail(f"source tree is missing or is a symlink: {source_tree}")
    source_tree = source_tree.resolve(strict=True)
    base_dependencies = omitted_dependencies(selection, lab_root)
    source_cases = (*selection.cases, *(case for _consumer, case in base_dependencies))
    all_paths = sorted(
        {path for case in source_cases for path in patch_source_paths(case)}
    )
    with tempfile.TemporaryDirectory(prefix="xpra-selection-") as raw:
        scratch = Path(raw)
        for relative in all_paths:
            source = source_tree / relative
            if source.is_symlink():
                fail(f"source path is a symlink: {relative}")
            if not source.exists():
                continue
            if not source.is_file() or not source.resolve().is_relative_to(source_tree):
                fail(f"source path is not a safe regular file: {relative}")
            destination = scratch / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

        dependency_entries: list[dict[str, str]] = []
        for consumer, dependency in base_dependencies:
            forward = run_git_apply(scratch, dependency.patch_path, "--check") == 0
            reverse = (
                run_git_apply(
                    scratch,
                    dependency.patch_path,
                    "--reverse",
                    "--check",
                )
                == 0
            )
            if forward or not reverse:
                fail(
                    f"case {consumer} has unresolved base dependency "
                    f"{dependency.slug} at {source_commit}"
                )
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
            forward = run_git_apply(scratch, case.patch_path, "--check") == 0
            reverse = (
                run_git_apply(scratch, case.patch_path, "--reverse", "--check") == 0
            )
            if forward == reverse:
                state = "ambiguous" if forward else "diverged"
                fail(
                    f"case {case.slug} is {state} at base {source_commit}; "
                    "refresh the patch and case metadata"
                )
            status = "apply" if forward else "already-present"
            if forward and (
                run_git_apply(scratch, case.patch_path) != 0
                or run_git_apply(scratch, case.patch_path, "--reverse", "--check") != 0
            ):
                fail(f"case {case.slug} failed deterministic patch application")
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
        "applied_cases": [
            entry["case"] for entry in entries if entry["status"] == "apply"
        ],
        "already_present_cases": [
            entry["case"] for entry in entries if entry["status"] == "already-present"
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["resolution_sha256"] = hashlib.sha256(canonical).hexdigest()
    return payload


def validate_resolution_document(
    selection: Selection,
    lab_root: Path,
    document: object,
    source_commit: str,
    expected_selection_digest: str,
) -> str:
    if not isinstance(document, dict):
        fail("selection resolution must be a JSON object")
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
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
        for consumer, dependency in omitted_dependencies(selection, lab_root)
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
        if not isinstance(entry, dict) or any(
            entry.get(key) != value for key, value in expected.items()
        ):
            fail("selection resolution patch identity is inconsistent")
        status = entry.get("status")
        if status == "apply":
            applied.append(case.slug)
        elif status == "already-present":
            already_present.append(case.slug)
        else:
            fail("selection resolution has an invalid patch status")
    if (
        document.get("applied_cases") != applied
        or document.get("already_present_cases") != already_present
    ):
        fail("selection resolution effective series is inconsistent")
    recorded_digest = document.get("resolution_sha256")
    payload = dict(document)
    payload.pop("resolution_sha256", None)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    actual_digest = hashlib.sha256(canonical).hexdigest()
    if recorded_digest != actual_digest:
        fail("selection resolution digest is inconsistent")
    return actual_digest


def snapshot(selection: Selection, lab_root: Path, destination: Path) -> None:
    if destination.exists():
        fail(f"snapshot destination already exists: {destination}")
    destination.mkdir(parents=True, mode=0o700)
    paths: list[tuple[Path, bytes]] = [
        (selection.manifest_path.relative_to(lab_root), selection.manifest_bytes)
    ]
    for case in selection.cases:
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lab-root", type=Path, required=True)
    parser.add_argument("--selection", required=True)
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
            "digest",
            "resolve",
            "snapshot",
            "verify-resolution",
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
        if args.lab_root.is_symlink():
            fail(f"lab root is a symlink: {args.lab_root}")
        lab_root = args.lab_root.resolve(strict=True)
        selection = load_selection(lab_root, args.selection)
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
            tests = (
                *selection.tests,
                *(test for case in selection.cases for test in case.tests),
            )
            for test in tests:
                if LOCAL_TEST_RE.fullmatch(test) and test not in seen:
                    seen.add(test)
                    print(test)
        elif args.action == "unit-tests":
            for test in iter_unit_tests(selection):
                print(test)
        elif args.action == "quarantined-tests":
            for test in iter_quarantined_tests(selection):
                print(test)
        elif args.action == "gates":
            for gate in iter_gates(selection):
                print(gate)
        elif args.action == "digest":
            print(selection_digest(selection, lab_root))
        elif args.action == "resolve":
            if args.source_tree is None or args.source_commit is None:
                fail("--source-tree and --source-commit are required for resolve")
            resolution = resolve_selection(
                selection,
                lab_root,
                args.source_tree,
                args.source_commit,
            )
            print(json.dumps(resolution, indent=2, sort_keys=True))
        elif args.action == "verify-resolution":
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
            resolution_bytes = require_regular_file(
                args.resolution, "selection resolution"
            )
            digest_bytes = require_regular_file(
                args.digest_file, "selection resolution digest"
            )
            try:
                document = json.loads(resolution_bytes)
                recorded_digest = digest_bytes.decode("ascii")
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                fail(f"invalid selection resolution output: {exc}")
            digest = validate_resolution_document(
                selection,
                lab_root,
                document,
                args.source_commit,
                args.selection_sha256,
            )
            if recorded_digest != f"{digest}\n":
                fail("selection resolution digest file is inconsistent")
            print(digest)
        elif args.action == "snapshot":
            if args.destination is None:
                fail("--destination is required for snapshot")
            snapshot(selection, lab_root, args.destination)
    except (OSError, SelectionError) as exc:
        print(f"selection error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
