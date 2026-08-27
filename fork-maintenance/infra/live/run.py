#!/usr/bin/env python3
"""Exercise tracked applications across isolated Xpra live sessions."""

from __future__ import annotations

import argparse
import ast
import hashlib
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
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

import PIL
from PIL import Image, ImageChops, ImageStat
from profiles import (
    ALPHA_SCENARIOS,
    APPLICATIONS,
    H264_ACCEPTANCE_POLICIES,
    H264_CLIENT_POLICIES,
    H264_FALLBACK_POLICIES,
    LIFECYCLES,
    ProfileError,
    scenario_specs,
    validate_profile,
)
from xwd_to_png import decode_xwd, save_alpha_visualization

INFRA_ROOT = Path(__file__).resolve().parent
LAB_ROOT = INFRA_ROOT.parent.parent
MAIN_REPOSITORY_ROOT = LAB_ROOT.parent
SOURCE_REPOSITORY = MAIN_REPOSITORY_ROOT
SELECTION_TOOL = INFRA_ROOT.parent / "upstream-tests" / "selection.py"
BACKGROUND_SUPERVISOR = LAB_ROOT / "tools" / "background_job.py"
DEFAULT_STATE_ROOT = MAIN_REPOSITORY_ROOT / ".artifacts" / "fork-maintenance"
DEFAULT_ZED_DIRECTORY = Path.home() / ".local" / "zed.app"
DEFAULT_RENDER_NODE = Path("/dev/dri/renderD128")
UPSTREAM_REMOTE_URL = "https://github.com/Xpra-org/xpra.git"
SERVER_DISPLAY = ":150"
SERVER_PORT = 14500
CLIENT_PROXY_PORT = 14501
CLIENT_DISPLAY = ":0"
WAIT_SECONDS = 60.0
EXPECTED_PILLOW_VERSION = "12.1.1"
INTERACTION_READY_TITLE = "Xpra Hardware Interaction Ready"
INTERACTION_CLICKED_TITLE = "Xpra Hardware Interaction Clicked"
INTERACTION_CLICK_MARKER = "/tmp/xpra-hardware-pointer-clicked"
INTERACTION_KEY_MARKER = "/tmp/xpra-hardware-keyboard-escape"
LEGACY_SOURCE_VARIANT_SELECTORS = {"master": ()}
HARNESS_INPUTS = (
    INFRA_ROOT / ".containerignore",
    INFRA_ROOT / "Containerfile",
    INFRA_ROOT / "interaction_fixture.py",
    INFRA_ROOT / "job.py",
    INFRA_ROOT / "profiles.py",
    INFRA_ROOT / "requirements.txt",
    INFRA_ROOT / "run.py",
    INFRA_ROOT / "start_hardware_fixture.sh",
    INFRA_ROOT / "start_zed.sh",
    INFRA_ROOT / "xwd_to_png.py",
    SELECTION_TOOL,
    BACKGROUND_SUPERVISOR,
)
BUILD_CONTEXT_INPUTS = (
    INFRA_ROOT / ".containerignore",
    INFRA_ROOT / "Containerfile",
    INFRA_ROOT / "interaction_fixture.py",
    INFRA_ROOT / "start_hardware_fixture.sh",
    INFRA_ROOT / "start_zed.sh",
)


class LabFailure(RuntimeError):
    """Raised when a required diagnostic boundary is unavailable."""


def ensure_private_directory(path: Path, *, create: bool = False) -> None:
    if path.is_symlink():
        raise LabFailure(f"private directory must not be a symlink: {path}")
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        info = path.lstat()
    except OSError as error:
        raise LabFailure(f"private directory is unavailable: {path}") from error
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise LabFailure(f"private directory is not owned by this user: {path}")
    if create and stat.S_IMODE(info.st_mode) != 0o700:
        path.chmod(0o700)
        info = path.lstat()
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise LabFailure(
            f"unsafe private directory mode for {path}: {stat.S_IMODE(info.st_mode):o}"
        )


def ensure_trusted_parent_directory(path: Path) -> None:
    if path.is_symlink():
        raise LabFailure(f"state parent must not be a symlink: {path}")
    try:
        info = path.lstat()
    except OSError as error:
        raise LabFailure(f"state parent is unavailable: {path}") from error
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise LabFailure(f"state parent is not owned by this user: {path}")
    if info.st_mode & 0o022:
        raise LabFailure(f"state parent is writable by another user: {path}")


def ensure_private_regular_file(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise LabFailure(f"private file is unavailable: {path}") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or path.is_symlink()
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise LabFailure(f"unsafe private file: {path}")


@dataclass(frozen=True)
class Scenario:
    name: str
    disable_alpha: bool


def transport_encoding_options(
    encoding: str,
    h264_client_policy: str,
    *,
    client: bool,
) -> list[str]:
    """Return the exact Xpra encoding arguments for one tracked live profile."""
    if h264_client_policy not in H264_CLIENT_POLICIES:
        raise LabFailure(f"unsupported H.264 client policy: {h264_client_policy}")
    if encoding == "rgb":
        if h264_client_policy != "strict":
            raise LabFailure("non-strict H.264 policies require the H.264 live profile")
        if client:
            return [
                "--encodings=rgb",
                "--opengl=no",
                "--video-decoders=none",
                "--csc-modules=none",
            ]
        return ["--encodings=rgb", "--video-encoders=none", "--csc-modules=none"]
    if encoding != "h264":
        raise LabFailure(f"unsupported live encoding: {encoding}")
    encodings = {
        "strict": "h264",
        "adaptive-alpha": "h264,webp,rgb",
        "fallback-auto": "h264,rgb",
        "fallback-h264": "h264,rgb",
    }[h264_client_policy]
    if client:
        options = [
            "--video=yes",
            f"--encodings={encodings}",
            "--opengl=force:native",
            "--video-decoders=libva",
            "--csc-modules=none",
        ]
        if h264_client_policy in {"adaptive-alpha", "fallback-h264"}:
            options.append("--encoding=h264")
        return options
    return [
        "--video=yes",
        f"--encodings={encodings}",
        "--video-encoders=libva",
        "--csc-modules=libyuv",
    ]


@dataclass(frozen=True)
class SourceSnapshot:
    archive_path: Path
    archive_sha256: str
    commit: str
    commit_marker: str
    revision: int
    workflow_sha256: str


@dataclass(frozen=True)
class PatchSelection:
    case_slugs: tuple[str, ...]
    digest: str
    name: str
    patches: tuple[Path, ...]
    selector_digests: tuple[tuple[str, str], ...]
    selectors: tuple[str, ...]


@dataclass(frozen=True)
class BuildContext:
    digest: str
    manifest: dict[str, Any]
    patches: tuple[Path, ...]
    path: Path
    resolution: dict[str, Any]
    selection: PatchSelection


def format_command(command: list[str]) -> str:
    return shlex.join(command)


def run(
    command: list[str],
    *,
    check: bool = True,
    capture: bool = True,
    timeout: float | None = None,
    announce: bool = True,
) -> subprocess.CompletedProcess[str]:
    if announce:
        print(f"+ {format_command(command)}", flush=True)
    result = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True,
        timeout=timeout,
    )
    if check and result.returncode:
        details = ""
        if capture:
            details = f"\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        raise LabFailure(
            f"command exited with status {result.returncode}: "
            f"{format_command(command)}{details}"
        )
    return result


def podman_exec(
    container: str,
    command: list[str],
    *,
    check: bool = True,
    detach: bool = False,
    announce: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    invocation = ["podman", "exec"]
    if detach:
        invocation.append("--detach")
    invocation.extend((container, *command))
    return run(
        invocation,
        check=check,
        announce=announce,
        timeout=timeout,
    )


def inspect_lab_image(
    image: str,
    *,
    role: str,
    source_commit: str,
    context_digest: str,
) -> dict[str, Any]:
    payload = json.loads(run(["podman", "image", "inspect", image]).stdout)
    if not isinstance(payload, list) or len(payload) != 1:
        raise LabFailure(f"unexpected Podman image inspection result for {image}")
    inspection = payload[0]
    if not isinstance(inspection, dict):
        raise LabFailure(f"invalid Podman image inspection result for {image}")
    labels = inspection.get("Labels")
    if not isinstance(labels, dict):
        config = inspection.get("Config")
        labels = config.get("Labels") if isinstance(config, dict) else None
    if not isinstance(labels, dict):
        raise LabFailure(f"image has no Xpra lab provenance labels: {image}")
    expected = {
        "io.xpra.lab.context": context_digest,
        "io.xpra.lab.owner": "live",
        "io.xpra.lab.role": role,
        "io.xpra.lab.source": source_commit,
    }
    mismatches = {
        key: {"expected": value, "observed": labels.get(key)}
        for key, value in expected.items()
        if labels.get(key) != value
    }
    if mismatches:
        raise LabFailure(
            f"image provenance labels do not match the frozen inputs for {image}: "
            f"{json.dumps(mismatches, sort_keys=True)}"
        )
    image_id = inspection.get("Id")
    if not isinstance(image_id, str) or not image_id:
        raise LabFailure(f"image has no immutable identifier: {image}")
    return {"id": image_id, "labels": expected}


def verify_container_image(container: str, expected_image_id: str) -> None:
    payload = json.loads(run(["podman", "container", "inspect", container]).stdout)
    if not isinstance(payload, list) or len(payload) != 1:
        raise LabFailure(f"unexpected Podman inspection result for {container}")
    inspection = payload[0]
    actual = inspection.get("Image") if isinstance(inspection, dict) else None
    normalize = lambda value: str(value).removeprefix("sha256:")
    if not re.fullmatch(r"[0-9a-f]{64}", normalize(actual)):
        raise LabFailure(f"container has no immutable image ID: {container}")
    if normalize(actual) != normalize(expected_image_id):
        raise LabFailure(
            f"container {container} uses image {actual}, expected {expected_image_id}"
        )


def inspect_podman_object_labels(kind: str, name: str) -> dict[str, str]:
    if kind not in {"container", "network"}:
        raise LabFailure(f"unsupported Podman object kind: {kind}")
    result = run(["podman", kind, "inspect", name], check=False, announce=False)
    if result.returncode:
        raise LabFailure(f"could not inspect {kind} {name}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise LabFailure(
            f"invalid Podman inspection result for {kind} {name}"
        ) from error
    if not isinstance(payload, list) or len(payload) != 1:
        raise LabFailure(f"unexpected Podman inspection result for {kind} {name}")
    inspection = payload[0]
    if not isinstance(inspection, dict):
        raise LabFailure(f"invalid Podman inspection object for {kind} {name}")
    if kind == "container":
        config = inspection.get("Config")
        labels = config.get("Labels") if isinstance(config, dict) else None
    else:
        label_fields = [
            (field, inspection[field])
            for field in ("Labels", "labels")
            if field in inspection
        ]
        if not label_fields or any(
            not isinstance(value, dict) for _field, value in label_fields
        ):
            raise LabFailure(f"network has invalid provenance labels: {name}")
        labels = label_fields[0][1]
        if any(value != labels for _field, value in label_fields[1:]):
            raise LabFailure(f"network has conflicting provenance labels: {name}")
    if not isinstance(labels, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in labels.items()
    ):
        raise LabFailure(f"{kind} has invalid provenance labels: {name}")
    return labels


def list_podman_object_names(kind: str) -> subprocess.CompletedProcess[str]:
    if kind == "container":
        command = ["podman", "ps", "--all", "--format", "{{.Names}}"]
    elif kind == "network":
        command = ["podman", "network", "ls", "--format", "{{.Name}}"]
    else:
        raise LabFailure(f"unsupported Podman object kind: {kind}")
    return run(command, check=False, announce=False)


def remove_owned_podman_object(
    kind: str,
    name: str,
    expected_labels: dict[str, str],
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "expected_labels": expected_labels,
        "name": name,
    }
    try:
        labels = inspect_podman_object_labels(kind, name)
    except LabFailure as error:
        entry.update({"error": str(error), "status": "inspect-failed"})
        return entry
    observed = {key: labels.get(key) for key in expected_labels}
    entry["observed_labels"] = observed
    if observed != expected_labels:
        entry["status"] = "ownership-mismatch"
        return entry
    command = ["podman", "rm", "--force", name]
    if kind == "network":
        command = ["podman", "network", "rm", name]
    result = run(command, check=False)
    postcheck = list_podman_object_names(kind)
    remaining_names = (
        {line.strip() for line in postcheck.stdout.splitlines() if line.strip()}
        if postcheck.returncode == 0
        else set()
    )
    entry.update(
        {
            "remove_returncode": result.returncode,
            "remove_stderr": result.stderr,
            "remove_stdout": result.stdout,
            "postcheck_returncode": postcheck.returncode,
            "postcheck_stderr": postcheck.stderr,
            "postcheck_stdout": postcheck.stdout,
        }
    )
    if postcheck.returncode:
        entry.update(
            {
                "error": "could not verify that the Podman object is absent",
                "postcondition": "unverified",
                "status": "remove-failed",
            }
        )
    elif name in remaining_names:
        entry.update(
            {
                "error": "Podman object remains after removal",
                "postcondition": "present",
                "status": "remove-failed",
            }
        )
    else:
        entry.update({"postcondition": "absent", "status": "removed"})
    return entry


def wait_for(
    description: str,
    predicate: Callable[[], bool],
    *,
    timeout: float = WAIT_SECONDS,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            print(f"PASS: {description}", flush=True)
            return
        time.sleep(0.1)
    raise LabFailure(f"timed out waiting for {description}")


def container_process_exists(container: str, pid: int) -> bool:
    return (
        podman_exec(
            container,
            ["kill", "-0", str(pid)],
            check=False,
            announce=False,
        ).returncode
        == 0
    )


def wait_for_log(
    container: str,
    pid: int,
    path: Path,
    marker: str,
    description: str,
) -> None:
    def ready() -> bool:
        if path.is_file() and marker in path.read_text(
            encoding="utf-8", errors="replace"
        ):
            return True
        if not container_process_exists(container, pid):
            tail = ""
            if path.is_file():
                tail = "\n".join(
                    path.read_text(encoding="utf-8", errors="replace").splitlines()[
                        -80:
                    ]
                )
            raise LabFailure(f"process exited before {description}:\n{tail}")
        return False

    wait_for(description, ready)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def artifact_sha256(directory: Path) -> dict[str, str]:
    return {
        path.relative_to(directory).as_posix(): sha256_file(path)
        for path in sorted(directory.rglob("*"))
        if path.is_file() and not path.is_symlink() and path.name != "report.json"
    }


def selection_output(selector: str, action: str, *arguments: str) -> str:
    if not SELECTION_TOOL.is_file() or SELECTION_TOOL.is_symlink():
        raise LabFailure(f"selection validator is unavailable: {SELECTION_TOOL}")
    return run(
        [
            sys.executable,
            str(SELECTION_TOOL),
            "--lab-root",
            str(LAB_ROOT),
            "--selection",
            selector,
            action,
            *arguments,
        ],
        announce=False,
    ).stdout.strip()


def selected_patch_paths(selector: str) -> tuple[Path, ...]:
    paths: list[Path] = []
    for value in selection_output(selector, "patches").splitlines():
        relative = Path(value)
        if (
            len(relative.parts) != 3
            or relative.parts[0] != "cases"
            or relative.parts[2] != "fix.patch"
            or relative.is_absolute()
            or ".." in relative.parts
        ):
            raise LabFailure(f"selection returned an unsafe patch path: {value!r}")
        path = LAB_ROOT / relative
        if path.is_symlink() or not path.is_file():
            raise LabFailure(f"selection patch is not a regular file: {relative}")
        paths.append(path)
    if not paths:
        raise LabFailure(f"selection has no patches: {selector}")
    return tuple(paths)


def resolve_patch_selection(
    selection_name: str | None,
    legacy_variant: str | None,
) -> PatchSelection:
    if selection_name and legacy_variant:
        raise LabFailure("--selection and --source-variant cannot be used together")
    if selection_name:
        selectors = (selection_name,)
        name = selection_name
    else:
        name = legacy_variant or "master"
        selectors = LEGACY_SOURCE_VARIANT_SELECTORS[name]

    patches: list[Path] = []
    case_slugs: list[str] = []
    selector_digests: list[tuple[str, str]] = []
    for selector in selectors:
        digest = selection_output(selector, "digest")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise LabFailure(f"selection returned an invalid digest: {selector}")
        selector_digests.append((selector, digest))
        selector_patches = selected_patch_paths(selector)
        selector_cases = selection_output(selector, "cases").splitlines()
        if [path.parent.name for path in selector_patches] != selector_cases:
            raise LabFailure(f"selection case and patch order do not match: {selector}")
        patches.extend(selector_patches)
        case_slugs.extend(selector_cases)
    if len(patches) != len(set(patches)):
        raise LabFailure(f"selection resolves the same patch more than once: {name}")
    if len(case_slugs) != len(set(case_slugs)):
        raise LabFailure(f"selection resolves the same case more than once: {name}")

    if selection_name:
        selection_digest = selector_digests[0][1]
    else:
        digest = hashlib.sha256()
        digest.update(f"legacy-selection\0{name}\0".encode())
        for selector, selector_digest in selector_digests:
            digest.update(f"{selector}\0{selector_digest}\0".encode())
        if not selectors:
            digest.update(b"clean-upstream-master\0")
        selection_digest = digest.hexdigest()
    return PatchSelection(
        case_slugs=tuple(case_slugs),
        digest=selection_digest,
        name=name,
        patches=tuple(patches),
        selector_digests=tuple(selector_digests),
        selectors=selectors,
    )


def ensure_patch_selection_current(selection: PatchSelection) -> None:
    for selector, expected in selection.selector_digests:
        observed = selection_output(selector, "digest")
        if observed != expected:
            raise LabFailure(
                f"selection changed while its build inputs were being frozen: {selector}"
            )


def clean_selection_resolution(
    selection: PatchSelection,
    source_commit: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": 1,
        "source_commit": source_commit,
        "selection": selection.name,
        "selection_sha256": selection.digest,
        "declared_cases": [],
        "base_dependencies": [],
        "patches": [],
        "applied_cases": [],
        "already_present_cases": [],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["resolution_sha256"] = hashlib.sha256(canonical).hexdigest()
    return payload


def resolve_selection_at_source(
    selection: PatchSelection,
    snapshot: SourceSnapshot,
    source_directory: Path,
) -> dict[str, Any]:
    if not selection.selectors:
        return clean_selection_resolution(selection, snapshot.commit)
    if selection.selectors != (selection.name,):
        raise LabFailure(
            "deprecated cumulative source variants cannot resolve merged patches; "
            "use --selection with one case or stack"
        )
    raw = selection_output(
        selection.name,
        "resolve",
        "--source-tree",
        str(source_directory),
        "--source-commit",
        snapshot.commit,
    )
    try:
        resolution = json.loads(raw)
    except json.JSONDecodeError as error:
        raise LabFailure("selection resolver returned invalid JSON") from error
    if not isinstance(resolution, dict):
        raise LabFailure("selection resolver returned a non-object")
    expected_patches = [
        {
            "case": case_slug,
            "patch": patch.relative_to(LAB_ROOT).as_posix(),
            "patch_sha256": sha256_file(patch),
        }
        for case_slug, patch in zip(
            selection.case_slugs, selection.patches, strict=True
        )
    ]
    if (
        resolution.get("schema") != 1
        or resolution.get("source_commit") != snapshot.commit
        or resolution.get("selection") != selection.name
        or resolution.get("selection_sha256") != selection.digest
        or resolution.get("declared_cases") != list(selection.case_slugs)
    ):
        raise LabFailure("selection resolution provenance is inconsistent")
    entries = resolution.get("patches")
    if not isinstance(entries, list) or len(entries) != len(expected_patches):
        raise LabFailure("selection resolution patch series is inconsistent")
    applied: list[str] = []
    already_present: list[str] = []
    for entry, expected in zip(entries, expected_patches, strict=True):
        if not isinstance(entry, dict) or any(
            entry.get(key) != value for key, value in expected.items()
        ):
            raise LabFailure("selection resolution patch identity is inconsistent")
        status = entry.get("status")
        if status == "apply":
            applied.append(expected["case"])
        elif status == "already-present":
            already_present.append(expected["case"])
        else:
            raise LabFailure("selection resolution has an invalid patch status")
    if (
        resolution.get("applied_cases") != applied
        or resolution.get("already_present_cases") != already_present
    ):
        raise LabFailure("selection resolution effective series is inconsistent")
    recorded_digest = resolution.get("resolution_sha256")
    payload = dict(resolution)
    payload.pop("resolution_sha256", None)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    if (
        not isinstance(recorded_digest, str)
        or recorded_digest != hashlib.sha256(canonical).hexdigest()
    ):
        raise LabFailure("selection resolution digest is inconsistent")
    return resolution


def git_output(*arguments: str) -> str:
    return run(["git", "-C", str(SOURCE_REPOSITORY), *arguments]).stdout.strip()


def resolve_live_upstream_master() -> tuple[str, str, int]:
    if not (SOURCE_REPOSITORY / ".git").exists():
        raise LabFailure(f"Xpra fork checkout is missing: {SOURCE_REPOSITORY}")
    if git_output("rev-parse", "--is-inside-work-tree") != "true":
        raise LabFailure(f"Xpra source is not a working tree: {SOURCE_REPOSITORY}")
    remotes = set(git_output("remote").splitlines())
    if "upstream" not in remotes:
        raise LabFailure("Xpra fork checkout has no 'upstream' remote")
    upstream_url = git_output("remote", "get-url", "upstream").removesuffix("/")
    if upstream_url.removesuffix(".git") != UPSTREAM_REMOTE_URL.removesuffix(".git"):
        raise LabFailure(
            f"Xpra 'upstream' remote has an unexpected URL: {upstream_url}"
        )

    run(
        [
            "git",
            "-C",
            str(SOURCE_REPOSITORY),
            "fetch",
            "--no-tags",
            "upstream",
            "+refs/heads/master:refs/remotes/upstream/master",
        ],
        capture=False,
    )
    local_commit = git_output("rev-parse", "refs/remotes/upstream/master")
    remote_line = git_output("ls-remote", "--heads", "upstream", "refs/heads/master")
    remote_commit, separator, remote_ref = remote_line.partition("\t")
    if (
        not separator
        or remote_ref != "refs/heads/master"
        or not re.fullmatch(r"[0-9a-f]{40}", remote_commit)
    ):
        raise LabFailure("could not resolve the live upstream/master commit")
    if local_commit != remote_commit:
        raise LabFailure(
            "upstream/master moved while it was being frozen; run the command again"
        )

    describe = git_output("describe", "--long", "--always", "--tags", remote_commit)
    parts = describe.split("-")
    commit_marker = parts[-1] if len(parts) >= 3 else f"g{remote_commit[:9]}"
    revision = (
        int(git_output("rev-list", "--count", "--first-parent", remote_commit)) + 5014
    )
    return remote_commit, commit_marker, revision


def create_source_snapshot(state_root: Path) -> SourceSnapshot:
    commit, commit_marker, revision = resolve_live_upstream_master()
    archive_root = state_root / "source-archives"
    ensure_private_directory(archive_root, create=True)
    with tempfile.NamedTemporaryFile(
        dir=archive_root,
        prefix=f".{commit}.",
        suffix=".tar",
        delete=False,
    ) as stream:
        temporary_archive = Path(stream.name)
    try:
        run(
            [
                "git",
                "-C",
                str(SOURCE_REPOSITORY),
                "archive",
                "--format=tar",
                f"--output={temporary_archive}",
                commit,
            ]
        )
        archive_sha256 = sha256_file(temporary_archive)
        archive_path = archive_root / f"{commit}-{archive_sha256}.tar"
        try:
            os.link(temporary_archive, archive_path)
        except FileExistsError:
            ensure_private_regular_file(archive_path)
            if sha256_file(archive_path) != archive_sha256:
                raise LabFailure(f"cached source archive is corrupt: {archive_path}")
        temporary_archive.unlink()
    finally:
        if temporary_archive.exists():
            temporary_archive.unlink()
    ensure_private_regular_file(archive_path)
    with tarfile.open(archive_path, mode="r:") as archive:
        try:
            workflow = archive.extractfile(".github/workflows/test.yml")
        except KeyError as error:
            raise LabFailure("upstream source archive has no test workflow") from error
        if workflow is None:
            raise LabFailure("upstream source archive has no test workflow")
        with workflow:
            workflow_sha256 = hashlib.sha256(workflow.read()).hexdigest()
    return SourceSnapshot(
        archive_path=archive_path,
        archive_sha256=archive_sha256,
        commit=commit,
        commit_marker=commit_marker,
        revision=revision,
        workflow_sha256=workflow_sha256,
    )


def write_source_metadata(
    source: Path, snapshot: SourceSnapshot, patched: bool
) -> None:
    metadata = (
        "BRANCH = 'master'\n"
        f"COMMIT = {snapshot.commit_marker!r}\n"
        f"LOCAL_MODIFICATIONS = {int(patched)}\n"
        f"REVISION = {snapshot.revision}\n"
    )
    (source / "xpra" / "src_info.py").write_text(metadata, encoding="utf-8")


def extract_source_archive(snapshot: SourceSnapshot, destination: Path) -> None:
    destination.mkdir()
    with tarfile.open(snapshot.archive_path, mode="r:") as archive:
        for member in archive.getmembers():
            member_path = Path(member.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise LabFailure(f"unsafe path in Xpra source archive: {member.name}")
            if ".git" in member_path.parts:
                raise LabFailure("Xpra source archive unexpectedly contains .git data")
        archive.extractall(destination, filter="data")


def tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        directory_names.sort()
        file_names.sort()
        directory_path = Path(directory)
        relative_directory = directory_path.relative_to(root).as_posix() or "."
        digest.update(
            f"D\0{relative_directory}\0"
            f"{stat.S_IMODE(directory_path.lstat().st_mode):o}\0".encode()
        )
        for name in tuple(directory_names):
            path = directory_path / name
            if not path.is_symlink():
                continue
            directory_names.remove(name)
            relative = path.relative_to(root).as_posix()
            digest.update(f"L\0{relative}\0{os.readlink(path)}\0".encode())
        for name in file_names:
            path = directory_path / name
            relative = path.relative_to(root).as_posix()
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                digest.update(f"L\0{relative}\0{os.readlink(path)}\0".encode())
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise LabFailure(f"unsupported build-context entry: {path}")
            digest.update(
                f"F\0{relative}\0{stat.S_IMODE(metadata.st_mode):o}\0".encode()
            )
            with path.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    digest.update(block)
            digest.update(b"\0")
    return digest.hexdigest()


def prepare_build_context(
    state_root: Path,
    snapshot: SourceSnapshot,
    selection: PatchSelection,
) -> BuildContext:
    patches = selection.patches
    ensure_patch_selection_current(selection)
    for patch in patches:
        if not patch.is_file():
            raise LabFailure(f"case patch is missing: {patch}")
    context_root = state_root / "build-contexts" / "live"
    ensure_private_directory(context_root, create=True)
    temporary = Path(tempfile.mkdtemp(prefix=".context.", dir=context_root))
    ensure_private_directory(temporary)
    try:
        for source in BUILD_CONTEXT_INPUTS:
            shutil.copy2(source, temporary / source.name)
        source_directory = temporary / "source"
        extract_source_archive(snapshot, source_directory)
        resolution = resolve_selection_at_source(
            selection,
            snapshot,
            source_directory,
        )
        write_source_metadata(
            source_directory,
            snapshot,
            bool(resolution["applied_cases"]),
        )

        patches_directory = temporary / "patches"
        patches_directory.mkdir()
        series: list[str] = []
        already_present: list[str] = []
        patch_manifest: dict[str, str] = {}
        resolution_entries = resolution["patches"]
        if not isinstance(resolution_entries, list):
            raise LabFailure("selection resolution patch series is invalid")
        for index, (patch, resolution_entry) in enumerate(
            zip(patches, resolution_entries, strict=True),
            start=1,
        ):
            if not isinstance(resolution_entry, dict):
                raise LabFailure("selection resolution patch entry is invalid")
            destination_name = f"{index:04d}-{patch.parent.name}.patch"
            shutil.copy2(patch, patches_directory / destination_name)
            if resolution_entry["status"] == "apply":
                series.append(destination_name)
            else:
                already_present.append(destination_name)
            patch_manifest[patch.relative_to(LAB_ROOT).as_posix()] = sha256_file(patch)
        dependency_entries = resolution["base_dependencies"]
        if not isinstance(dependency_entries, list):
            raise LabFailure("selection resolution base dependencies are invalid")
        for index, dependency_entry in enumerate(dependency_entries, start=1):
            if not isinstance(dependency_entry, dict):
                raise LabFailure("selection resolution base dependency is invalid")
            relative = Path(str(dependency_entry.get("patch", "")))
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or relative.as_posix() != dependency_entry.get("patch")
            ):
                raise LabFailure("selection resolution dependency path is unsafe")
            patch = LAB_ROOT / relative
            if (
                patch.is_symlink()
                or not patch.is_file()
                or sha256_file(patch) != dependency_entry.get("patch_sha256")
            ):
                raise LabFailure("selection resolution dependency patch is stale")
            destination_name = (
                f"dependency-{index:04d}-{dependency_entry['dependency']}.patch"
            )
            shutil.copy2(patch, patches_directory / destination_name)
            already_present.append(destination_name)
        (patches_directory / "series").write_text(
            "".join(f"{name}\n" for name in series),
            encoding="utf-8",
        )
        (patches_directory / "already-present").write_text(
            "".join(f"{name}\n" for name in already_present),
            encoding="utf-8",
        )
        manifest: dict[str, Any] = {
            "format": 1,
            "patches": patch_manifest,
            "patch_series": [
                {
                    "path": patch.relative_to(LAB_ROOT).as_posix(),
                    "sha256": sha256_file(patch),
                }
                for patch in patches
            ],
            "selection": {
                "case_slugs": list(selection.case_slugs),
                "digest": selection.digest,
                "name": selection.name,
                "resolution": resolution,
                "selector_digests": dict(selection.selector_digests),
                "selectors": list(selection.selectors),
            },
            "source": {
                "archive_sha256": snapshot.archive_sha256,
                "commit": snapshot.commit,
                "commit_marker": snapshot.commit_marker,
                "revision": snapshot.revision,
                "workflow_sha256": snapshot.workflow_sha256,
            },
        }
        (temporary / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        ensure_patch_selection_current(selection)
        context_digest = tree_sha256(temporary)
        context_path = context_root / context_digest
        if context_path.is_symlink():
            raise LabFailure(f"cached build context is a symlink: {context_path}")
        if not context_path.exists():
            try:
                temporary.rename(context_path)
            except FileExistsError:
                pass
        ensure_private_directory(context_path)
        if temporary.exists():
            if tree_sha256(context_path) != context_digest:
                raise LabFailure(f"cached build context is corrupt: {context_path}")
            cached_manifest = json.loads(
                (context_path / "manifest.json").read_text(encoding="utf-8")
            )
            if cached_manifest != manifest:
                raise LabFailure(
                    f"cached build context has wrong provenance: {context_path}"
                )
            shutil.rmtree(temporary)
        return BuildContext(
            digest=context_digest,
            manifest=manifest,
            patches=patches,
            path=context_path,
            resolution=resolution,
            selection=selection,
        )
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


def snapshot_patch_selection(
    destination: Path,
    context: BuildContext,
) -> None:
    selection = context.selection
    ensure_patch_selection_current(selection)
    destination.mkdir()
    (destination / "selection.json").write_text(
        json.dumps(
            {
                "case_slugs": selection.case_slugs,
                "digest": selection.digest,
                "name": selection.name,
                "patches": [
                    path.relative_to(LAB_ROOT).as_posix() for path in selection.patches
                ],
                "resolution": context.resolution,
                "selector_digests": dict(selection.selector_digests),
                "selectors": selection.selectors,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    snapshots = destination / "validated-manifests"
    snapshots.mkdir()
    for index, selector in enumerate(selection.selectors, start=1):
        selector_snapshot = snapshots / f"{index:04d}-{selector.replace('/', '-')}"
        selection_output(
            selector,
            "snapshot",
            "--destination",
            str(selector_snapshot),
        )
    ensure_patch_selection_current(selection)


def snapshot_build_inputs(
    result_directory: Path,
    snapshot: SourceSnapshot,
    server_context: BuildContext,
    client_context: BuildContext,
) -> str:
    inputs = result_directory / "inputs"
    harness = inputs / "harness"
    harness.mkdir(parents=True)
    for source in HARNESS_INPUTS:
        shutil.copy2(source, harness / source.name)
    shutil.copy2(snapshot.archive_path, inputs / "source.tar")
    contexts = inputs / "contexts"
    contexts.mkdir()
    shutil.copytree(server_context.path, contexts / "server", symlinks=True)
    shutil.copytree(client_context.path, contexts / "client", symlinks=True)
    selections = inputs / "selections"
    selections.mkdir()
    snapshot_patch_selection(selections / "server", server_context)
    snapshot_patch_selection(selections / "client", client_context)
    manifest = {
        "client_context_sha256": client_context.digest,
        "client_selection_sha256": client_context.selection.digest,
        "harness": {path.name: sha256_file(path) for path in HARNESS_INPUTS},
        "server_context_sha256": server_context.digest,
        "server_selection_sha256": server_context.selection.digest,
        "server_selection_resolution_sha256": server_context.resolution[
            "resolution_sha256"
        ],
        "source_archive_sha256": snapshot.archive_sha256,
        "source_commit": snapshot.commit,
        "source_workflow_sha256": snapshot.workflow_sha256,
    }
    manifest_path = inputs / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    checksum_lines: list[str] = []
    for path in sorted(inputs.rglob("*")):
        if path.is_file() and not path.is_symlink() and path.name != "SHA256SUMS":
            checksum_lines.append(
                f"{sha256_file(path)}  {path.relative_to(inputs).as_posix()}\n"
            )
    checksums = inputs / "SHA256SUMS"
    checksums.write_text("".join(checksum_lines), encoding="utf-8")
    return sha256_file(manifest_path)


def write_command_output(
    container: str,
    command: list[str],
    destination: Path,
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = podman_exec(container, command, check=False)
    destination.write_text(result.stdout + result.stderr, encoding="utf-8")
    if check and result.returncode:
        raise LabFailure(
            f"diagnostic command failed in {container}: {format_command(command)}"
        )
    return result


def find_window(container: str, patterns: tuple[str, ...]) -> tuple[str, str] | None:
    script = r"""
set -u
export DISPLAY="$1"
for wid in $(xdotool search --onlyvisible --name '.*' 2>/dev/null || true); do
    name=$(xdotool getwindowname "$wid" 2>/dev/null || true)
    printf '%s\t%s\n' "$wid" "$name"
done
"""
    result = podman_exec(
        container,
        ["bash", "-c", script, "find-window", CLIENT_DISPLAY],
        check=False,
        announce=False,
    )
    lowered = tuple(pattern.casefold() for pattern in patterns)
    for line in result.stdout.splitlines():
        window_id, separator, title = line.partition("\t")
        if separator and any(pattern in title.casefold() for pattern in lowered):
            return window_id, title
    return None


def window_geometry(container: str, window_id: str) -> dict[str, int]:
    result = podman_exec(
        container,
        [
            "env",
            f"DISPLAY={CLIENT_DISPLAY}",
            "xdotool",
            "getwindowgeometry",
            "--shell",
            window_id,
        ],
    )
    values: dict[str, int] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"X", "Y", "WIDTH", "HEIGHT", "SCREEN"}:
            values[key.lower()] = int(value)
    if not {"x", "y", "width", "height"}.issubset(values):
        raise LabFailure(f"incomplete X11 window geometry: {result.stdout!r}")
    return values


def capture_xwd(
    container: str,
    destination: str,
    *,
    window_id: str | None = None,
    screen: bool = False,
    announce: bool = True,
) -> None:
    selector = ["-id", window_id] if window_id else ["-root"]
    if screen:
        if window_id is None:
            raise ValueError("a window ID is required for a focused screen capture")
        selector.insert(0, "-screen")
    podman_exec(
        container,
        [
            "env",
            f"DISPLAY={CLIENT_DISPLAY}",
            "xwd",
            "-silent",
            *selector,
            "-out",
            f"/artifacts/{destination}",
        ],
        announce=announce,
    )


def capture_grim(
    container: str,
    directory: Path,
    stem: str,
    wayland_display: str,
) -> dict[str, Any]:
    podman_exec(
        container,
        [
            "env",
            "XDG_RUNTIME_DIR=/tmp/client-runtime",
            f"WAYLAND_DISPLAY={wayland_display}",
            "grim",
            f"/artifacts/{stem}.rgba.png",
        ],
    )
    rgba_path = directory / f"{stem}.rgba.png"
    with Image.open(rgba_path) as source:
        rgba = source.convert("RGBA")
    rgba.save(rgba_path, format="PNG")
    rgba.convert("RGB").save(directory / f"{stem}.rgb.png", format="PNG")
    save_alpha_visualization(rgba, directory / f"{stem}.alpha.png")
    return {
        "image": analyze_image(rgba),
        "xwd": {"capture": "grim", "wayland_display": wayland_display},
    }


def analyze_image(image: Image.Image) -> dict[str, Any]:
    rgba = image.convert("RGBA")
    rgb = rgba.convert("RGB")
    sample = rgb.copy()
    sample.thumbnail((320, 240))
    colors = sample.getcolors(maxcolors=sample.width * sample.height) or []
    dominant_count, dominant = max(colors, default=(0, (0, 0, 0)))
    stat = ImageStat.Stat(sample)
    alpha = rgba.getchannel("A")
    center = alpha.crop(
        (
            rgba.width // 10,
            rgba.height // 10,
            rgba.width - rgba.width // 10,
            rgba.height - rgba.height // 10,
        )
    )
    alpha_sample = center.resize((min(320, center.width), min(240, center.height)))
    alpha_values = list(alpha_sample.get_flattened_data())
    rgb_bytes = rgb.tobytes()
    return {
        "central_opaque_ratio": (
            sum(value == 255 for value in alpha_values) / len(alpha_values)
            if alpha_values
            else 0.0
        ),
        "dominant_rgb": list(dominant),
        "dominant_rgb_ratio": dominant_count / (sample.width * sample.height),
        "height": rgba.height,
        "quantized_rgb_colors": len(colors),
        "rgb_sha256": hashlib.sha256(rgb_bytes).hexdigest(),
        "rgb_channel_stddev": [round(value, 3) for value in stat.stddev],
        "width": rgba.width,
    }


def convert_xwd(directory: Path, stem: str) -> dict[str, Any]:
    image, xwd = decode_xwd(directory / f"{stem}.xwd")
    image.save(directory / f"{stem}.rgba.png", format="PNG")
    image.convert("RGB").save(directory / f"{stem}.rgb.png", format="PNG")
    save_alpha_visualization(image, directory / f"{stem}.alpha.png")
    return {"image": analyze_image(image), "xwd": xwd}


def analyze_png(path: Path) -> dict[str, Any]:
    with Image.open(path) as image:
        image.load()
        return analyze_image(image)


def compare_rgb_images(reference_path: Path, observed_path: Path) -> dict[str, Any]:
    with Image.open(reference_path) as reference_source:
        reference = reference_source.convert("RGB")
    with Image.open(observed_path) as observed_source:
        observed = observed_source.convert("RGB")
    if reference.size != observed.size:
        return {
            "exact": False,
            "mean_absolute_error": None,
            "red_blue_swapped_mean_absolute_error": None,
            "same_size": False,
        }
    normal = ImageStat.Stat(ImageChops.difference(reference, observed)).mean
    red, green, blue = observed.split()
    swapped = Image.merge("RGB", (blue, green, red))
    swapped_error = ImageStat.Stat(ImageChops.difference(reference, swapped)).mean
    return {
        "exact": reference.tobytes() == observed.tobytes(),
        "mean_absolute_error": round(sum(normal) / 3, 6),
        "red_blue_swapped_mean_absolute_error": round(sum(swapped_error) / 3, 6),
        "same_size": True,
    }


def pixel_pipeline_evidence(
    directory: Path,
    screenshots: list[str],
    direct_path: Path,
    focused_path: Path,
    maximum_error: float,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    comparisons: list[dict[str, Any]] = []
    for relative in screenshots:
        source_path = directory / relative
        direct = compare_rgb_images(source_path, direct_path)
        focused = compare_rgb_images(source_path, focused_path)
        comparisons.append(
            {
                "direct": direct,
                "focused_screen": focused,
                "source": relative,
            }
        )
    comparable = [
        comparison
        for comparison in comparisons
        if comparison["direct"]["same_size"]
        and comparison["focused_screen"]["same_size"]
    ]
    matching = [
        comparison
        for comparison in comparable
        if comparison["direct"]["mean_absolute_error"] <= maximum_error
        and comparison["focused_screen"]["mean_absolute_error"] <= maximum_error
    ]
    selected = min(
        comparable,
        key=lambda comparison: (
            comparison["direct"]["mean_absolute_error"]
            + comparison["focused_screen"]["mean_absolute_error"]
        ),
        default=None,
    )
    direct_focused = compare_rgb_images(direct_path, focused_path)
    red_blue_order_verified = bool(
        selected
        and selected["direct"]["red_blue_swapped_mean_absolute_error"]
        > selected["direct"]["mean_absolute_error"] + 0.1
    )
    evidence = {
        "comparisons": comparisons,
        "direct_focused": direct_focused,
        "maximum_mean_absolute_error": maximum_error,
        "matching_server_frame": bool(matching),
        "red_blue_order_verified": red_blue_order_verified,
    }
    source_image = (
        analyze_png(directory / selected["source"]) if selected is not None else None
    )
    return evidence, source_image


def pixel_error_limit(application: str, encoding: str) -> float:
    """Return the exact per-profile client/server image tolerance."""
    if encoding == "h264":
        return 15.0
    if application == "gtk":
        # GTK text rasterization can differ by one intensity level at glyph edges
        # between the server pixels and the X11 client capture.  Keep the static
        # Zed RGB proof byte-exact and scope this bounded tolerance to the tracked
        # GTK lifecycle fixture only.
        return 1.0
    return 0.0


def crop_composited_window(
    directory: Path,
    root_stem: str,
    geometry: dict[str, int],
    background_rgb: tuple[int, int, int],
) -> dict[str, Any]:
    root = Image.open(directory / f"{root_stem}.rgba.png").convert("RGBA")
    x = max(0, geometry["x"])
    y = max(0, geometry["y"])
    right = min(root.width, geometry["x"] + geometry["width"])
    bottom = min(root.height, geometry["y"] + geometry["height"])
    if right <= x or bottom <= y:
        raise LabFailure(f"window lies outside the captured root: {geometry}")
    crop = root.crop((x, y, right, bottom))
    crop.save(directory / "window-composited.png", format="PNG")
    evidence = analyze_image(crop)
    sample = crop.convert("RGB")
    sample.thumbnail((320, 240))
    pixels = list(sample.get_flattened_data())
    evidence["background_match_ratio"] = sum(
        max(abs(pixel[index] - background_rgb[index]) for index in range(3)) <= 3
        for pixel in pixels
    ) / len(pixels)
    evidence["reference_background_rgb"] = list(background_rgb)
    return evidence


def add_background_comparison(
    evidence: dict[str, Any],
    image_path: Path,
    background_rgb: tuple[int, int, int],
) -> None:
    with Image.open(image_path) as source:
        sample = source.convert("RGB")
    sample.thumbnail((320, 240))
    pixels = list(sample.get_flattened_data())
    evidence["image"]["background_match_ratio"] = sum(
        max(abs(pixel[index] - background_rgb[index]) for index in range(3)) <= 3
        for pixel in pixels
    ) / len(pixels)
    evidence["image"]["reference_background_rgb"] = list(background_rgb)


def read_os_release(container: str) -> dict[str, str]:
    result = podman_exec(
        container,
        ["cat", "/etc/os-release"],
        announce=False,
    )
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value.strip().strip('"')
    return values


def compositor_probe(container: str) -> dict[str, Any] | None:
    probe = (
        "import json\n"
        "import gi\n"
        "gi.require_version('Gdk', '3.0')\n"
        "from gi.repository import Gdk\n"
        "screen = Gdk.Screen.get_default()\n"
        "visual = screen.get_rgba_visual() if screen else None\n"
        "print(json.dumps({'composited': bool(screen and screen.is_composited()), "
        "'rgba_visual': bool(visual), 'rgba_depth': visual.get_depth() if visual else None}))\n"
    )
    result = podman_exec(
        container,
        ["env", f"DISPLAY={CLIENT_DISPLAY}", "python3", "-c", probe],
        check=False,
        announce=False,
    )
    if result.returncode:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def process_gpu_evidence(container: str, pid: int) -> dict[str, Any]:
    script = r"""
set -u
pid="$1"
printf '%s\n' '[argv]'
tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true
printf '\n%s\n' '[render-nodes]'
for descriptor in "/proc/$pid/fd/"*; do
    target=$(readlink "$descriptor" 2>/dev/null || true)
    case "$target" in
        /dev/dri/*) printf '%s\n' "$target" ;;
    esac
done
printf '%s\n' '[gpu-mappings]'
grep -E 'libvulkan_radeon|radeonsi_dri|swrast_dri|libgallium|libva\.so' "/proc/$pid/maps" 2>/dev/null || true
"""
    result = podman_exec(
        container,
        ["bash", "-c", script, "gpu-evidence", str(pid)],
        check=False,
        announce=False,
    )
    section = ""
    argv = ""
    render_nodes: list[str] = []
    mappings: list[str] = []
    for line in result.stdout.splitlines():
        if line == "[argv]":
            section = "argv"
        elif line == "[render-nodes]":
            section = "nodes"
        elif line == "[gpu-mappings]":
            section = "maps"
        elif section == "argv" and line:
            argv = line
        elif section == "nodes" and line:
            render_nodes.append(line)
        elif section == "maps" and line:
            mappings.append(line)
    return {
        "argv": argv,
        "gpu_mappings": mappings,
        "pid": pid,
        "render_nodes": sorted(set(render_nodes)),
    }


def client_xpra_window_id(directory: Path) -> int:
    client_log = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in (directory / "client.stdout", directory / "client.stderr")
        if path.is_file()
    )
    matches = re.findall(
        r"register_window\(\.\.\) window\(0x([0-9a-fA-F]+)\)="
        r"[A-Za-z0-9_]*ClientWindow\(0x\1\b",
        client_log,
    )
    window_ids = [int(value, 16) for value in matches if int(value, 16) > 0]
    if not window_ids:
        raise LabFailure("the client log does not identify the forwarded Xpra window")
    return window_ids[-1]


def server_xpra_window_id(info_path: Path, title_patterns: tuple[str, ...]) -> int:
    """Resolve one server window ID from its title in ``xpra info``."""
    expected = {pattern.casefold() for pattern in title_patterns}
    matches: list[int] = []
    for line in info_path.read_text(encoding="utf-8", errors="replace").splitlines():
        key, separator, value = line.partition("=")
        match = re.fullmatch(r"windows\.([1-9][0-9]*)\.title", key)
        if (
            separator
            and match
            and any(pattern in value.casefold() for pattern in expected)
        ):
            matches.append(int(match.group(1)))
    if len(matches) != 1:
        raise LabFailure(
            f"xpra info identified {len(matches)} windows for titles "
            f"{sorted(title_patterns)!r}"
        )
    return matches[0]


def process_exit_status(directory: Path, name: str) -> int:
    path = directory / f"{name}.exit"
    try:
        value = path.read_text(encoding="ascii").strip()
        status = int(value)
    except (OSError, ValueError) as error:
        raise LabFailure(f"invalid {name} process exit status") from error
    if status < 0 or status > 255:
        raise LabFailure(f"invalid {name} process exit status: {status}")
    return status


def wait_for_process_exit(
    container: str,
    pid: int,
    directory: Path,
    name: str,
    *,
    timeout: float = 15,
) -> int:
    wait_for(
        f"{name} process exit",
        lambda: (
            not container_process_exists(container, pid)
            and (directory / f"{name}.exit").is_file()
        ),
        timeout=timeout,
    )
    return process_exit_status(directory, name)


def parse_saved_updates(directory: Path, xpra_wid: int) -> dict[str, Any]:
    updates: list[dict[str, Any]] = []
    window_directory = directory / "screen-updates" / str(xpra_wid)
    for info_path in sorted(window_directory.glob("*/[0-9]*.info")):
        info = json.loads(info_path.read_text(encoding="utf-8"))
        payload = info_path.parent / str(info["file"])
        info["payload_bytes"] = payload.stat().st_size if payload.is_file() else -1
        info["payload_sha256"] = sha256_file(payload) if payload.is_file() else ""
        info["relative_info"] = str(info_path.relative_to(directory))
        updates.append(info)
    updates.sort(key=lambda update: int(update.get("sequence", -1)))
    encodings = sorted({str(update.get("encoding")) for update in updates})
    formats = sorted(
        {
            str(update.get("options", {}).get("rgb_format"))
            for update in updates
            if update.get("options", {}).get("rgb_format")
        }
    )
    screenshots = sorted(window_directory.glob("*/screenshot.png"))
    return {
        "count": len(updates),
        "encodings": encodings,
        "rgb_formats": formats,
        "screenshots": [str(path.relative_to(directory)) for path in screenshots],
        "updates": updates,
        "window_id": xpra_wid,
    }


def only_positive_h264_packets(updates: dict[str, Any] | None) -> bool:
    """Return whether one exact window produced only non-empty H.264 updates."""
    return bool(
        updates
        and updates.get("count", 0) > 0
        and set(updates.get("encodings", ())) == {"h264"}
        and all(
            int(update.get("payload_bytes", -1)) > 0
            for update in updates.get("updates", ())
        )
    )


def _exact_int(value: Any, *, positive: bool = False) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    if positive and value <= 0:
        return None
    return value


def _packet_window_size(update: dict[str, Any]) -> tuple[int, int] | None:
    options = update.get("options")
    if not isinstance(options, dict):
        return None
    size = options.get("window-size")
    if not isinstance(size, (tuple, list)) or len(size) != 2:
        return None
    width = _exact_int(size[0], positive=True)
    height = _exact_int(size[1], positive=True)
    if width is None or height is None:
        return None
    return width, height


def _packet_geometry(
    update: dict[str, Any],
) -> tuple[int, int, int, int] | None:
    x = _exact_int(update.get("x"))
    y = _exact_int(update.get("y"))
    width = _exact_int(update.get("w"), positive=True)
    height = _exact_int(update.get("h"), positive=True)
    if x is None or y is None or width is None or height is None or x < 0 or y < 0:
        return None
    return x, y, width, height


def _lossless_rgb_edge_kind(update: dict[str, Any]) -> str | None:
    if update.get("encoding") not in {"rgb24", "rgb32"}:
        return None
    if _exact_int(update.get("payload_bytes"), positive=True) is None:
        return None
    geometry = _packet_geometry(update)
    window_size = _packet_window_size(update)
    options = update.get("options")
    if geometry is None or window_size is None or not isinstance(options, dict):
        return None
    if options.get("rgb_format") not in {
        "BGR",
        "BGRX",
        "RGB",
        "RGBX",
        "XBGR",
        "XRGB",
    }:
        return None
    if _exact_int(options.get("flush"), positive=True) is None:
        return None
    x, y, width, height = geometry
    window_width, window_height = window_size
    if (x, y, width, height) == (0, window_height - 1, window_width, 1):
        return "bottom"
    if (x, y, width, height) == (window_width - 1, 0, 1, window_height):
        return "right"
    return None


def _h264_main_size(update: dict[str, Any]) -> tuple[int, int] | None:
    if update.get("encoding") != "h264":
        return None
    if _exact_int(update.get("payload_bytes"), positive=True) is None:
        return None
    geometry = _packet_geometry(update)
    window_size = _packet_window_size(update)
    if geometry is None or window_size is None:
        return None
    x, y, width, height = geometry
    window_width, window_height = window_size
    if (
        x
        or y
        or window_width - width not in {0, 1}
        or window_height - height
        not in {
            0,
            1,
        }
    ):
        return None
    return width, height


def h264_with_lossless_rgb_edges(updates: dict[str, Any] | None) -> bool:
    """Validate H.264 main regions plus only exact one-pixel RGB codec edges."""
    if not isinstance(updates, dict):
        return False
    packets = updates.get("updates")
    count = _exact_int(updates.get("count"), positive=True)
    encodings = updates.get("encodings")
    if (
        not isinstance(packets, list)
        or count != len(packets)
        or not isinstance(encodings, list)
        or not all(isinstance(encoding, str) for encoding in encodings)
        or not all(isinstance(packet, dict) for packet in packets)
    ):
        return False
    actual_encodings = {packet.get("encoding") for packet in packets}
    if set(encodings) != actual_encodings or "h264" not in actual_encodings:
        return False
    sequences = [
        _exact_int(packet.get("sequence"), positive=True) for packet in packets
    ]
    if any(sequence is None for sequence in sequences):
        return False
    exact_sequences = [int(sequence) for sequence in sequences if sequence is not None]
    if sorted(exact_sequences) != list(
        range(min(exact_sequences), max(exact_sequences) + 1)
    ):
        return False

    groups: dict[tuple[int, int], dict[str, set[Any]]] = {}
    for packet in packets:
        window_size = _packet_window_size(packet)
        if window_size is None:
            return False
        group = groups.setdefault(window_size, {"edges": set(), "main_sizes": set()})
        if packet.get("encoding") == "h264":
            main_size = _h264_main_size(packet)
            if main_size is None:
                return False
            group["main_sizes"].add(main_size)
        else:
            edge = _lossless_rgb_edge_kind(packet)
            if edge is None:
                return False
            group["edges"].add(edge)

    for window_size, group in groups.items():
        main_sizes = group["main_sizes"]
        if len(main_sizes) != 1:
            return False
        main_width, main_height = next(iter(main_sizes))
        window_width, window_height = window_size
        required_edges = set()
        if main_width == window_width - 1:
            required_edges.add("right")
        if main_height == window_height - 1:
            required_edges.add("bottom")
        if group["edges"] != required_edges:
            return False
    return True


def inspect_logs(directory: Path) -> dict[str, Any]:
    server_log = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in (directory / "server.stdout", directory / "server.stderr")
        if path.is_file()
    )
    client_log = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in (directory / "client.stdout", directory / "client.stderr")
        if path.is_file()
    )
    zed_log = (
        (directory / "zed.stderr").read_text(encoding="utf-8", errors="replace")
        if (directory / "zed.stderr").is_file()
        else ""
    )
    commit_lines = [line for line in server_log.splitlines() if "commit wid " in line]
    rgb_error_lines = [
        line
        for line in server_log.splitlines()
        if "no compatible rgb format" in line
        or "failed to create data packet" in line
        or "rgb_reformat" in line
        and "Error" in line
    ]
    paint_error_lines = [
        line
        for line in client_log.splitlines()
        if "Error painting" in line or "decoding error" in line or "paint error" in line
    ]
    initial_data_error_lines = [
        line
        for line in server_log.splitlines()
        if "Error: in " in line and ".send_initial_data" in line
    ]
    h264_negotiation_match = re.search(
        r"^.*do_set_client_properties\(.*encoding\.full_csc_modes.*'h264'.*$",
        server_log,
        re.MULTILINE,
    )
    h264_steady_log = (
        server_log[h264_negotiation_match.start() :]
        if h264_negotiation_match
        else server_log
    )
    h264_pre_negotiation_log = (
        server_log[: h264_negotiation_match.start()] if h264_negotiation_match else ""
    )

    def h264_errors(log: str) -> list[str]:
        return [
            line
            for line in log.splitlines()
            if "client does not support any csc modes with h264" in line
            or "no common encodings found" in line
            or "no video pipeline options found" in line
            or "failed to create data packet" in line
            or "failed to encode h264 frame" in line
            or "h264 video compression failed" in line
        ]

    h264_error_lines = h264_errors(h264_steady_log)
    h264_pre_negotiation_error_lines = h264_errors(h264_pre_negotiation_log)
    renderer_match = re.search(r"OpenGL [^\n]+ enabled on '([^']+)'", client_log)
    property_lines = [
        line
        for line in server_log.splitlines()
        if "do_set_client_properties(" in line
        and "encodings.rgb_formats" in line
        and "RGBX" in line
    ]
    return {
        "cairo_paints": client_log.count("cairo._do_paint_rgb"),
        "client_draw_packets": client_log.count("draw_region("),
        "client_draw_regions": len(
            re.findall(r"draw_region\([^\n]+(?:rgb24|rgb32)", client_log)
        ),
        "client_successful_paints": len(
            re.findall(r"record_decode_time\((?:True|1),", client_log)
        ),
        "dmabuf_fourcc": sorted(
            set(
                re.findall(
                    r"capture_pixels: dmabuf[^\n]+format=(0x[0-9a-fA-F]+)", server_log
                )
            )
        ),
        "empty_wayland_commits": sum("rects=[]" in line for line in commit_lines),
        "gtk_cairo_draws": client_log.count("cairo_draw: window size="),
        "gtk_draw_widgets": client_log.count("draw_widget("),
        "h264_draw_regions": len(re.findall(r"draw_region\([^\n]+h264", client_log)),
        "h264_libva_decodes": client_log.count("libva decoded h264"),
        "h264_nv12_paints": client_log.count(
            "do_video_paint('h264', ImageWrapper(NV12"
        ),
        "h264_per_window_negotiation_applied": bool(h264_negotiation_match),
        "h264_pipeline_errors": h264_error_lines[-20:],
        "h264_pre_negotiation_errors": h264_pre_negotiation_error_lines[-20:],
        "opengl_renderer": renderer_match.group(1) if renderer_match else "",
        "opengl_presentations": client_log.count("do_present_fbo("),
        "opengl_rgb_paints": len(
            re.findall(r"\.do_paint_rgb\(rgb,\s*(?:RGB|BGR)", client_log)
        ),
        "nonempty_wayland_commits": sum(
            "rects=[]" not in line for line in commit_lines
        ),
        "paint_errors": paint_error_lines[-20:],
        "rgb_encode_errors": rgb_error_lines[-20:],
        "rgb_encodes": server_log.count("rgb_encode using"),
        "rgbx_to_bgrx_conversions": server_log.count("argb_swap: rgbx_to_bgrx"),
        "rgbx_window_properties": property_lines,
        "server_initial_data_errors": initial_data_error_lines[-20:],
        "server_logging_errors": server_log.count("--- Logging error ---"),
        "rgbx_images": server_log.count("DMABufImageWrapper(0x34325258"),
        "transparency_false_mentions": server_log.count(
            "encoding.transparency': False"
        ),
        "transparency_true_mentions": server_log.count("encoding.transparency': True"),
        "wayland_protocol": {
            "ack_configure": zed_log.count("ack_configure"),
            "commits": zed_log.count(".commit("),
            "damage_buffer": zed_log.count("damage_buffer"),
        },
    }


VA_TRACE_LINE_RE = re.compile(
    r"^\[(?P<timestamp>\d+\.\d+)\]"
    r"\[ctx\s+(?P<context>none|0x[0-9a-fA-F]+)\](?P<body>.*)$"
)
VA_CONTEXT_PROFILE_RE = re.compile(
    r"profile\s*=\s*\d+,\s*(VAProfile\w+)\s+"
    r"entrypoint\s*=\s*\d+,\s*(VAEntrypoint\w+)"
)
VA_PICTURE_CALL_RE = re.compile(r"=+va_Trace(Begin|Render|End)Picture\b")
VA_PICTURE_RESULT_RE = re.compile(
    r"=+va(Begin|Render|End)Picture ret = (VA_STATUS_[A-Z_]+)"
)
H264_PROCESS_DRAW_RE = re.compile(
    r"(?m)^.*?process_draw:\s+(?P<payload_bytes>\d+)\s+"
    r"(?:<class '[^']+'>|bytes)\s+for window\s+(?P<window_id>\d+),\s+"
    r"sequence\s+(?P<sequence>\d+),\s+"
    r"(?P<width>\d+)x(?P<height>\d+)\s+at\s+"
    r"(?P<x>-?\d+),(?P<y>-?\d+)\s+using\s+"
    r"(?P<encoding>[\w-]+)\s+encoding with options="
    r"typedict\((?P<options>\{.*\})\)$"
)
H264_DRAW_REGION_RE = re.compile(
    r"(?m)^.*?draw_region\("
    r"(?P<x>-?\d+),\s*(?P<y>-?\d+),\s*"
    r"(?P<width>\d+),\s*(?P<height>\d+),\s*"
    r"(?P<encoding>[\w-]+),\s*(?P<payload_bytes>\d+) bytes,\s*"
    r"(?P<stride>\d+),\s*typedict\((?P<options>\{.*\})\),\s*"
    r"\[<function WindowDraw\._do_draw\.<locals>\.record_decode_time at "
    r"(?P<callback>0x[0-9a-fA-F]+)>"
)
H264_ACK_RE = re.compile(
    r"(?m)^.*?sending ack: \('window-ack',\s*"
    r"(?P<window_id>\d+),\s*(?P<width>\d+),\s*(?P<height>\d+),\s*"
    r"(?P<sequence>\d+),"
)


def parse_va_contexts(directory: Path, prefix: str) -> dict[str, Any]:
    """Parse successful VA picture transactions without mixing trace files."""
    paths = sorted(directory.glob(f"{prefix}.*"))
    contexts: list[dict[str, Any]] = []
    for path in paths:
        active: dict[str, dict[str, Any]] = {}
        generations: dict[str, int] = {}
        transactions: dict[str, dict[str, Any]] = {}
        creating: dict[str, Any] | None = None
        pending_picture_call: tuple[str, str] | None = None
        trace = path.read_bytes().decode("utf-8", errors="ignore").replace("\0", "")
        for raw_line in trace.splitlines():
            line_match = VA_TRACE_LINE_RE.match(raw_line)
            if not line_match:
                continue
            context_id = line_match.group("context")
            body = line_match.group("body")
            if "va_TraceCreateContext" in body:
                generation = generations.get(context_id, 0) + 1
                generations[context_id] = generation
                creating = {
                    "begin_successes": 0,
                    "completed_frames": 0,
                    "context": context_id,
                    "created": False,
                    "end_successes": 0,
                    "entrypoint": "",
                    "file": path.name,
                    "generation": generation,
                    "height": 0,
                    "incomplete_frames": 0,
                    "profile": "",
                    "render_successes": 0,
                    "width": 0,
                }
                contexts.append(creating)
                active[context_id] = creating
            elif creating is not None:
                profile_match = VA_CONTEXT_PROFILE_RE.search(body)
                if profile_match:
                    creating["profile"], creating["entrypoint"] = profile_match.groups()
                width_match = re.match(r"\s*width\s*=\s*(\d+)", body)
                height_match = re.match(r"\s*height\s*=\s*(\d+)", body)
                if width_match:
                    creating["width"] = int(width_match.group(1))
                if height_match:
                    creating["height"] = int(height_match.group(1))
                if "vaCreateContext ret =" in body:
                    creating["created"] = "VA_STATUS_SUCCESS" in body
                    creating = None

            call_match = VA_PICTURE_CALL_RE.search(body)
            if call_match:
                operation = call_match.group(1).lower()
                pending_picture_call = (operation, context_id)
                if operation == "begin":
                    previous = transactions.get(context_id)
                    if previous and previous["begin"] and not previous["end"]:
                        active[context_id]["incomplete_frames"] += 1
                    transactions[context_id] = {
                        "begin": False,
                        "end": False,
                        "render_successes": 0,
                    }

            result_match = VA_PICTURE_RESULT_RE.search(body)
            if not result_match or pending_picture_call is None:
                continue
            operation = result_match.group(1).lower()
            status = result_match.group(2)
            pending_operation, pending_context = pending_picture_call
            pending_picture_call = None
            if operation != pending_operation:
                continue
            record = active.get(pending_context)
            transaction = transactions.get(pending_context)
            if record is None or transaction is None or status != "VA_STATUS_SUCCESS":
                continue
            record[f"{operation}_successes"] += 1
            if operation == "begin":
                transaction["begin"] = True
            elif operation == "render":
                transaction["render_successes"] += 1
            else:
                transaction["end"] = True
                if transaction["begin"] and transaction["render_successes"] > 0:
                    record["completed_frames"] += 1
                else:
                    record["incomplete_frames"] += 1
                transactions.pop(pending_context, None)

        for context_id, transaction in transactions.items():
            if transaction["begin"] and context_id in active:
                active[context_id]["incomplete_frames"] += 1
    return {
        "contexts": contexts,
        "files": [path.name for path in paths],
    }


def h264_packet_streams(
    updates: dict[str, Any],
    *,
    allow_lossless_rgb_edges: bool = False,
) -> list[dict[str, Any]]:
    edge_mode = allow_lossless_rgb_edges and h264_with_lossless_rgb_edges(updates)
    packets_by_sequence = {
        int(update["sequence"]): update
        for update in updates["updates"]
        if _exact_int(update.get("sequence"), positive=True) is not None
    }

    def sequence_gap_is_edges(previous: dict[str, Any], packet: dict[str, Any]) -> bool:
        previous_sequence = int(previous["sequence"])
        sequence = int(packet["sequence"])
        if sequence == previous_sequence + 1:
            return True
        if not edge_mode or sequence <= previous_sequence + 1:
            return False
        window_size = _packet_window_size(packet)
        if window_size is None or _packet_window_size(previous) != window_size:
            return False
        return all(
            intermediate in packets_by_sequence
            and _packet_window_size(packets_by_sequence[intermediate]) == window_size
            and _lossless_rgb_edge_kind(packets_by_sequence[intermediate]) is not None
            for intermediate in range(previous_sequence + 1, sequence)
        )

    packets = sorted(
        (update for update in updates["updates"] if update.get("encoding") == "h264"),
        key=lambda update: int(update["sequence"]),
    )
    grouped: list[list[dict[str, Any]]] = []
    for packet in packets:
        options = packet.get("options", {})
        start_stream = not grouped
        if grouped:
            previous = grouped[-1][-1]
            previous_frame = previous.get("options", {}).get("frame")
            frame = options.get("frame")
            start_stream = (
                not sequence_gap_is_edges(previous, packet)
                or (int(packet["w"]), int(packet["h"]))
                != (int(previous["w"]), int(previous["h"]))
                or h264_encoded_size(packet) != h264_encoded_size(previous)
                or frame == 0
                or (
                    isinstance(frame, int)
                    and isinstance(previous_frame, int)
                    and frame != previous_frame + 1
                )
            )
        if start_stream:
            grouped.append([packet])
        else:
            grouped[-1].append(packet)

    streams: list[dict[str, Any]] = []
    for packets_in_stream in grouped:
        first = packets_in_stream[0]
        sequences = [int(packet["sequence"]) for packet in packets_in_stream]
        frames = [
            packet.get("options", {}).get("frame") for packet in packets_in_stream
        ]
        width = int(first["w"])
        height = int(first["h"])
        encoded_width, encoded_height = h264_encoded_size(first)
        transport_sequences = list(range(sequences[0], sequences[-1] + 1))
        interleaved_edge_sequences = [
            sequence
            for sequence in transport_sequences
            if sequence not in sequences
            and sequence in packets_by_sequence
            and _lossless_rgb_edge_kind(packets_by_sequence[sequence]) is not None
        ]
        streams.append(
            {
                "coded_size": [width, height],
                "encoded_size": [encoded_width, encoded_height],
                "contiguous_frames": frames == list(range(len(frames))),
                "contiguous_sequences": all(
                    sequence_gap_is_edges(previous, packet)
                    for previous, packet in pairwise(packets_in_stream)
                ),
                "first_sequence": sequences[0],
                "interleaved_edge_sequences": interleaved_edge_sequences,
                "last_sequence": sequences[-1],
                "packet_count": len(packets_in_stream),
                "packet_sequences": sequences,
                "positive_payloads": all(
                    int(packet.get("payload_bytes", -1)) > 0
                    for packet in packets_in_stream
                ),
                "starts_with_idr": (
                    first.get("options", {}).get("frame") == 0
                    and first.get("options", {}).get("type") == "IDR"
                ),
                "surface_size": [
                    (encoded_width + 15) // 16 * 16,
                    (encoded_height + 15) // 16 * 16,
                ],
                "transport_sequences": transport_sequences,
            }
        )
    return streams


def h264_encoded_size(packet: dict[str, Any]) -> tuple[int, int]:
    """Return the dimensions actually submitted to the video codec."""
    coded_size = (int(packet["w"]), int(packet["h"]))
    scaled_size = packet.get("options", {}).get("scaled_size")
    if not isinstance(scaled_size, (tuple, list)) or len(scaled_size) != 2:
        return coded_size
    if not all(
        isinstance(value, int) and not isinstance(value, bool) and value > 0
        for value in scaled_size
    ):
        return coded_size
    return int(scaled_size[0]), int(scaled_size[1])


def _context_key(context: dict[str, Any]) -> tuple[str, str, int]:
    return (
        str(context["file"]),
        str(context["context"]),
        int(context["generation"]),
    )


def _matching_va_contexts(
    contexts: list[dict[str, Any]],
    stream: dict[str, Any],
    entrypoints: set[str],
) -> list[dict[str, Any]]:
    width, height = stream["surface_size"]
    return [
        context
        for context in contexts
        if context["created"]
        and context["profile"].startswith("VAProfileH264")
        and context["entrypoint"] in entrypoints
        and [context["width"], context["height"]] == [width, height]
        and context["completed_frames"] == stream["packet_count"]
        and context["incomplete_frames"] == 0
    ]


def match_h264_production_stream(
    updates: dict[str, Any],
    server_trace: dict[str, Any],
    client_trace: dict[str, Any],
    *,
    allow_lossless_rgb_edges: bool = False,
) -> dict[str, Any]:
    streams = h264_packet_streams(
        updates,
        allow_lossless_rgb_edges=allow_lossless_rgb_edges,
    )
    candidates: list[dict[str, Any]] = []
    for stream in streams:
        server_matches = _matching_va_contexts(
            server_trace["contexts"],
            stream,
            {"VAEntrypointEncSlice", "VAEntrypointEncSliceLP"},
        )
        client_matches = _matching_va_contexts(
            client_trace["contexts"], stream, {"VAEntrypointVLD"}
        )
        candidate = {
            **stream,
            "client_contexts": client_matches,
            "server_contexts": server_matches,
        }
        candidate["complete"] = bool(
            stream["contiguous_frames"]
            and stream["contiguous_sequences"]
            and stream["positive_payloads"]
            and stream["starts_with_idr"]
            and len(server_matches) == 1
            and len(client_matches) == 1
        )
        candidates.append(candidate)
    complete = [candidate for candidate in candidates if candidate["complete"]]
    selected = max(
        complete,
        key=lambda candidate: (
            int(candidate["packet_count"]),
            -int(candidate["first_sequence"]),
        ),
        default=None,
    )
    production_keys = set()
    if selected:
        production_keys.update(
            _context_key(context)
            for context in (*selected["server_contexts"], *selected["client_contexts"])
        )
    all_contexts = [*server_trace["contexts"], *client_trace["contexts"]]
    selftest_contexts = [
        context
        for context in all_contexts
        if _context_key(context) not in production_keys
        and context["profile"].startswith("VAProfileH264")
        and [context["width"], context["height"]] == [128, 128]
        and context["completed_frames"] == 5
    ]
    unmatched_contexts = [
        context
        for context in all_contexts
        if _context_key(context) not in production_keys
        and context not in selftest_contexts
    ]
    return {
        "candidates": candidates,
        "matched_stream": selected,
        "production_proven": selected is not None,
        "selftest_contexts": selftest_contexts,
        "unmatched_contexts": unmatched_contexts,
    }


def _typedict_literal(value: str) -> dict[str, Any]:
    try:
        parsed = ast.literal_eval(value)
    except (SyntaxError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _normalise_log_value(value: Any) -> Any:
    if isinstance(value, (tuple, list)):
        return [_normalise_log_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalise_log_value(item) for key, item in value.items()}
    return value


def h264_client_packet_chain(
    directory: Path,
    updates: dict[str, Any],
    matched_stream: dict[str, Any] | None,
) -> dict[str, Any]:
    if not matched_stream:
        return {"complete": False, "reason": "no matched production stream"}
    sequence = int(matched_stream["first_sequence"])
    window_id = int(updates["window_id"])
    saved = next(
        (
            update
            for update in updates["updates"]
            if int(update.get("sequence", -1)) == sequence
            and update.get("encoding") == "h264"
        ),
        None,
    )
    if saved is None:
        return {"complete": False, "reason": "production packet was not saved"}
    log_path = directory / "client.stdout"
    client_log = (
        log_path.read_text(encoding="utf-8", errors="replace")
        if log_path.is_file()
        else ""
    )
    process_matches = [
        match
        for match in H264_PROCESS_DRAW_RE.finditer(client_log)
        if int(match.group("window_id")) == window_id
        and int(match.group("sequence")) == sequence
        and match.group("encoding") == "h264"
    ]
    if len(process_matches) != 1:
        return {
            "complete": False,
            "process_draw_matches": len(process_matches),
            "reason": "production packet does not have one client process_draw event",
            "sequence": sequence,
            "window_id": window_id,
        }
    process_match = process_matches[0]
    next_process = H264_PROCESS_DRAW_RE.search(client_log, process_match.end())
    draw_matches = [
        match
        for match in H264_DRAW_REGION_RE.finditer(
            client_log,
            process_match.end(),
            next_process.start() if next_process else len(client_log),
        )
        if match.group("encoding") == "h264"
    ]
    draw_match = draw_matches[0] if len(draw_matches) == 1 else None
    saved_options = _normalise_log_value(saved.get("options", {}))
    process_options = _normalise_log_value(
        _typedict_literal(process_match.group("options"))
    )
    packet_fields_match = bool(
        int(process_match.group("payload_bytes")) == int(saved["payload_bytes"])
        and int(process_match.group("width")) == int(saved["w"])
        and int(process_match.group("height")) == int(saved["h"])
        and int(process_match.group("x")) == int(saved["x"])
        and int(process_match.group("y")) == int(saved["y"])
        and all(
            process_options.get(key) == value for key, value in saved_options.items()
        )
    )
    draw_fields_match = False
    callback = ""
    if draw_match:
        callback = draw_match.group("callback")
        draw_options = _normalise_log_value(
            _typedict_literal(draw_match.group("options"))
        )
        draw_fields_match = bool(
            int(draw_match.group("payload_bytes")) == int(saved["payload_bytes"])
            and int(draw_match.group("width")) == int(saved["w"])
            and int(draw_match.group("height")) == int(saved["h"])
            and int(draw_match.group("x")) == int(saved["x"])
            and int(draw_match.group("y")) == int(saved["y"])
            and all(
                draw_options.get(key) == value for key, value in saved_options.items()
            )
        )

    width = int(saved["w"])
    height = int(saved["h"])
    encoded_width, encoded_height = h264_encoded_size(saved)
    payload_bytes = int(saved["payload_bytes"])
    search_start = draw_match.end() if draw_match else process_match.end()
    paint_match = (
        re.search(
            rf"(?m)^.*?do_video_paint\('h264',\s*"
            rf"ImageWrapper\(NV12:\(0, 0, {encoded_width}, {encoded_height},[^\n]+"
            rf"record_decode_time at {re.escape(callback)}>",
            client_log[search_start:],
        )
        if callback
        else None
    )
    paint_position = search_start + paint_match.start() if paint_match else -1
    paint_end = search_start + paint_match.end() if paint_match else search_start
    decoder_choice_match = re.search(
        r"choose_decoder\([^\n]*\)=libva\(YUV420P - h264\)",
        client_log[search_start:paint_position] if paint_position >= 0 else "",
    )
    decoder_instance_match = re.search(
        rf"paint_with_video_decoder: new libva\('h264',\s*"
        rf"{encoded_width},\s*{encoded_height},\s*'YUV420P'\)",
        client_log[search_start:paint_position] if paint_position >= 0 else "",
    )
    libva_decode_match = re.search(
        rf"libva decoded h264\s+{payload_bytes} bytes into "
        rf"{encoded_width}x{encoded_height} NV12\b",
        client_log[search_start:paint_position] if paint_position >= 0 else "",
    )
    decode_success_match = (
        re.search(
            rf"(?m)^.*?record_decode_time\((?:True|1),[^\n]*\) "
            rf"wid=0x{window_id:x}, h264: {width}x{height},",
            client_log[paint_end:],
        )
        if paint_match
        else None
    )
    decode_success_end = (
        paint_end + decode_success_match.end() if decode_success_match else paint_end
    )
    ack_matches = [
        match
        for match in H264_ACK_RE.finditer(client_log, decode_success_end)
        if int(match.group("window_id")) == window_id
        and int(match.group("sequence")) == sequence
        and int(match.group("width")) == width
        and int(match.group("height")) == height
    ]
    ack_match = ack_matches[0] if ack_matches else None
    ack_position = ack_match.start() if ack_match else -1
    intervening_decode = (
        "record_decode_time(" in client_log[decode_success_end:ack_position]
        if ack_position >= 0
        else True
    )
    present_match = re.search(
        r"(?m)^.*?do_present_fbo\([^\n]+\) will blit",
        client_log[ack_match.end() :] if ack_match else "",
    )
    present_position = (
        ack_match.end() + present_match.start() if ack_match and present_match else -1
    )
    next_ack = H264_ACK_RE.search(client_log, ack_match.end()) if ack_match else None
    unambiguous_presentation = bool(
        present_position >= 0
        and (next_ack is None or present_position < next_ack.start())
        and (next_process is None or present_position < next_process.start())
    )
    presentation_end = (
        ack_match.end() + present_match.end() if ack_match and present_match else 0
    )
    swap_match = (
        re.search(
            rf"(?m)^.*?\b{window_id}\.do_gl_show\("
            rf"GLDrawingArea\({window_id},[^\n]+swapping buffers now",
            client_log[presentation_end:],
        )
        if unambiguous_presentation
        else None
    )
    swap_end = presentation_end + swap_match.end() if swap_match else 0
    present_done_match = (
        re.search(
            rf"(?m)^.*?GLDrawingArea\({window_id},[^\n]+"
            rf"\.do_present_fbo\(\) done",
            client_log[swap_end:],
        )
        if swap_match
        else None
    )
    presentation_complete = bool(
        unambiguous_presentation and swap_match and present_done_match
    )
    base_chain_complete = bool(
        packet_fields_match
        and draw_fields_match
        and decoder_choice_match
        and decoder_instance_match
        and paint_match
        and decode_success_match
        and ack_match
        and not intervening_decode
        and presentation_complete
    )
    return {
        "acknowledged": bool(ack_match and not intervening_decode),
        "base_chain_complete": base_chain_complete,
        "callback": callback,
        "complete": bool(base_chain_complete and libva_decode_match),
        "decoder_selected": bool(decoder_choice_match and decoder_instance_match),
        "draw_region_matches_saved_packet": draw_fields_match,
        "encoded_size": [encoded_width, encoded_height],
        "libva_decode_log_matches_saved_packet": bool(libva_decode_match),
        "nv12_painted": bool(paint_match),
        "payload_bytes": payload_bytes,
        "payload_sha256": saved.get("payload_sha256", ""),
        "presented_before_later_work": presentation_complete,
        "process_draw_matches_saved_packet": packet_fields_match,
        "sequence": sequence,
        "size": [width, height],
        "window_id": window_id,
    }


def h264_hardware_evidence(
    directory: Path,
    updates: dict[str, Any],
    *,
    allow_lossless_rgb_edges: bool = False,
) -> dict[str, Any]:
    server_trace = parse_va_contexts(directory, "server-va")
    client_trace = parse_va_contexts(directory, "client-va")
    production = match_h264_production_stream(
        updates,
        server_trace,
        client_trace,
        allow_lossless_rgb_edges=allow_lossless_rgb_edges,
    )
    matched = production["matched_stream"]
    packet_chain = h264_client_packet_chain(directory, updates, matched)

    def side_summary(
        trace: dict[str, Any],
        side: str,
    ) -> dict[str, Any]:
        contexts = matched[f"{side}_contexts"] if matched else []
        context = contexts[0] if len(contexts) == 1 else None
        return {
            "contexts": trace["contexts"],
            "entrypoint_present": bool(context),
            "files": trace["files"],
            "h264_profile_present": bool(
                context and context["profile"].startswith("VAProfileH264")
            ),
            "production_context": context,
            "production_dimensions": (
                [[context["width"], context["height"]]] if context else []
            ),
            "submitted_frames": int(context["completed_frames"]) if context else 0,
        }

    return {
        "client": side_summary(client_trace, "client"),
        "packet_chain": packet_chain,
        "production": production,
        "server": side_summary(server_trace, "server"),
    }


def application_contract(application: str) -> tuple[str, tuple[str, ...], str]:
    if application == "zed":
        return "/opt/xpra-lab/start_zed.sh", ("empty project", "zed"), "zed.pid"
    if application == "hardware":
        return "/opt/xpra-lab/start_hardware_fixture.sh", ("vkcube",), "vkcube.pid"
    if application == "vkcube":
        return (
            "vkcube --wsi wayland --width 640 --height 480 --suppress_popups",
            ("vkcube",),
            "",
        )
    return (
        "python3 /opt/xpra-lab/interaction_fixture.py",
        ("xpra hardware interaction ready",),
        "",
    )


def wait_for_frame_boundary(
    server: str,
    server_pid: int,
    client: str,
    client_pid: int,
    directory: Path,
    encoding: str,
    h264_client_policy: str,
) -> str:
    outcome = "pending"
    h264_failure_seen_at: float | None = None

    def read_log(name: str) -> str:
        path = directory / name
        return (
            path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
        )

    def reached() -> bool:
        nonlocal outcome, h264_failure_seen_at
        server_log = read_log("server.stderr")
        client_log = read_log("client.stdout") + read_log("client.stderr")
        nonempty_commit = any(
            "rects=[]" not in line
            for line in server_log.splitlines()
            if "commit wid " in line
        )
        if encoding == "rgb":
            failed = (
                nonempty_commit
                and "no compatible rgb format for 'RGBX'!" in server_log
                and "only: ('BGRX', 'BGRA')" in server_log
            )
            source_ready = False
            for screenshot in directory.glob("screen-updates/*/*/screenshot.png"):
                try:
                    if analyze_png(screenshot)["quantized_rgb_colors"] > 32:
                        source_ready = True
                        break
                except (OSError, ValueError):
                    continue
            painted = (
                nonempty_commit
                and source_ready
                and "rgb_encode using" in server_log
                and "cairo._do_paint_rgb" in client_log
                and re.search(r"record_decode_time\((?:True|1),", client_log)
                and "draw_widget(" in client_log
                and "cairo_draw: window size=" in client_log
            )
            if failed:
                outcome = "failure"
                return True
            if painted:
                outcome = "success"
                return True
        else:
            negotiation_match = re.search(
                r"^.*do_set_client_properties\(.*encoding\.full_csc_modes.*'h264'.*$",
                server_log,
                re.MULTILINE,
            )
            steady_log = (
                server_log[negotiation_match.start() :]
                if negotiation_match
                else server_log
            )
            failure_present = nonempty_commit and any(
                marker in server_log
                for marker in (
                    "client does not support any csc modes with h264",
                    "no common encodings found",
                    "no video pipeline options found",
                    "failed to create data packet",
                )
            )
            if negotiation_match:
                failed = any(
                    marker in steady_log
                    for marker in (
                        "no video pipeline options found",
                        "failed to create data packet",
                        "failed to encode h264 frame",
                        "h264 video compression failed",
                    )
                )
                if not failed:
                    h264_failure_seen_at = None
            elif failure_present:
                if h264_failure_seen_at is None:
                    h264_failure_seen_at = time.monotonic()
                failed = time.monotonic() - h264_failure_seen_at >= 1.0
            else:
                h264_failure_seen_at = None
                failed = False
            try:
                xpra_wid = client_xpra_window_id(directory)
                updates = parse_saved_updates(directory, xpra_wid)
            except (LabFailure, OSError, ValueError, json.JSONDecodeError):
                xpra_wid = 0
                updates = {"count": 0, "encodings": [], "updates": []}
            if h264_client_policy in H264_FALLBACK_POLICIES and updates["count"] > 0:
                actual_encodings = set(updates["encodings"])
                if actual_encodings and actual_encodings <= {"rgb24", "rgb32"}:
                    outcome = "picture-fallback"
                else:
                    outcome = "unexpected-h264"
                return True
            production_marker = (
                f"register_window(..) window(0x{xpra_wid:x})=" if xpra_wid else ""
            )
            production_log = (
                client_log[client_log.find(production_marker) :]
                if production_marker and production_marker in client_log
                else ""
            )
            draw_match = re.search(r"draw_region\([^\n]+h264", production_log)
            after_draw = production_log[draw_match.start() :] if draw_match else ""
            decoder_selected = bool(
                re.search(
                    r"(?:choose_decoder\([^\n]*\)=libva|"
                    r"paint_with_video_decoder: new libva\('h264')",
                    after_draw,
                )
            )
            decode_acknowledged = bool(
                re.search(
                    rf"record_decode_time\((?:True|1),[^\n]*\) "
                    rf"wid=0x{xpra_wid:x}, h264:",
                    after_draw,
                )
            )
            presented = bool(
                nonempty_commit
                and (
                    h264_with_lossless_rgb_edges(updates)
                    if h264_client_policy == "adaptive-alpha"
                    else only_positive_h264_packets(updates)
                )
                and draw_match
                and decoder_selected
                and "do_video_paint('h264', ImageWrapper(NV12" in after_draw
                and decode_acknowledged
                and "do_present_fbo(" in after_draw
            )
            if presented:
                outcome = "success"
                return True
            if failed:
                if h264_failure_seen_at is None:
                    h264_failure_seen_at = time.monotonic()
                if time.monotonic() - h264_failure_seen_at >= 2.0:
                    outcome = "failure"
                    return True
        if not container_process_exists(server, server_pid):
            raise LabFailure("Xpra server exited before the first frame boundary")
        if not container_process_exists(client, client_pid):
            raise LabFailure("Xpra client exited before the first frame boundary")
        return False

    profile = (
        encoding.upper()
        if encoding != "h264" or h264_client_policy in H264_ACCEPTANCE_POLICIES
        else f"H264 {h264_client_policy}"
    )
    wait_for(f"{profile} frame outcome", reached)
    return outcome


def capture_window_when_ready(
    container: str,
    window_id: str,
    directory: Path,
    *,
    expect_content: bool,
) -> dict[str, Any]:
    evidence: dict[str, Any] | None = None

    def capture() -> bool:
        nonlocal evidence
        capture_xwd(
            container,
            "window-direct.xwd",
            window_id=window_id,
            announce=False,
        )
        evidence = convert_xwd(directory, "window-direct")
        return bool(
            evidence["xwd"]["unique_rgb_colors"] > 100
            and evidence["image"]["central_opaque_ratio"] >= 0.99
        )

    if expect_content:
        wait_for("nonuniform opaque pixels in the client window", capture)
    else:
        capture()
    assert evidence is not None
    return evidence


def detect_zed_system_theme_control(image: Image.Image) -> dict[str, Any]:
    """Locate Zed's selected System theme pill without fixed coordinates."""
    rgb = image.convert("RGB")
    width, height = rgb.size
    search_bottom = min(400, height // 2)
    pixels = rgb.load()
    visited: set[tuple[int, int]] = set()
    components: list[dict[str, Any]] = []

    def is_selected_accent(x: int, y: int) -> bool:
        red, green, blue = pixels[x, y]
        return red >= 185 and green >= 185 and blue - red >= 7 and blue - green >= 7

    for y in range(search_bottom):
        for x in range(width):
            point = (x, y)
            if point in visited or not is_selected_accent(x, y):
                continue
            visited.add(point)
            stack = [point]
            xs: list[int] = []
            ys: list[int] = []
            while stack:
                current_x, current_y = stack.pop()
                xs.append(current_x)
                ys.append(current_y)
                for adjacent in (
                    (current_x - 1, current_y),
                    (current_x + 1, current_y),
                    (current_x, current_y - 1),
                    (current_x, current_y + 1),
                ):
                    adjacent_x, adjacent_y = adjacent
                    if (
                        adjacent_x < 0
                        or adjacent_x >= width
                        or adjacent_y < 0
                        or adjacent_y >= search_bottom
                        or adjacent in visited
                        or not is_selected_accent(adjacent_x, adjacent_y)
                    ):
                        continue
                    visited.add(adjacent)
                    stack.append(adjacent)
            if len(xs) < 400:
                continue
            bounds = [min(xs), min(ys), max(xs) + 1, max(ys) + 1]
            component_width = bounds[2] - bounds[0]
            component_height = bounds[3] - bounds[1]
            components.append(
                {
                    "bounds": bounds,
                    "fill_ratio": round(
                        len(xs) / (component_width * component_height), 6
                    ),
                    "pixels": len(xs),
                }
            )

    pill_candidates = [
        component
        for component in components
        if 45 <= component["bounds"][2] - component["bounds"][0] <= 90
        and 20 <= component["bounds"][3] - component["bounds"][1] <= 40
        and component["fill_ratio"] >= 0.65
    ]
    if len(pill_candidates) != 1:
        raise LabFailure(
            "Zed System theme pill detection was ambiguous: "
            f"found {len(pill_candidates)} candidates"
        )
    system = pill_candidates[0]
    system_bounds = system["bounds"]
    pill_width = system_bounds[2] - system_bounds[0]
    pill_height = system_bounds[3] - system_bounds[1]
    card_candidates = [
        component
        for component in components
        if 2.5 * pill_width
        <= component["bounds"][2] - component["bounds"][0]
        <= 5 * pill_width
        and 2.5 * pill_height
        <= component["bounds"][3] - component["bounds"][1]
        <= 5 * pill_height
        and 0 < component["bounds"][1] - system_bounds[3] <= 40
    ]
    if len(card_candidates) != 1:
        raise LabFailure(
            "Zed selected theme-card validation was ambiguous: "
            f"found {len(card_candidates)} candidates"
        )
    control_left = system_bounds[2] - 3 * pill_width
    dark_bounds = [
        system_bounds[2] - 2 * pill_width,
        system_bounds[1],
        system_bounds[2] - pill_width,
        system_bounds[3],
    ]
    if control_left < 0 or dark_bounds[0] >= dark_bounds[2]:
        raise LabFailure("detected Zed theme control lies outside the window")
    click_position = [
        (dark_bounds[0] + dark_bounds[2]) // 2,
        (dark_bounds[1] + dark_bounds[3]) // 2,
    ]
    return {
        "click_position": click_position,
        "control_bounds": [
            control_left,
            system_bounds[1],
            system_bounds[2],
            system_bounds[3],
        ],
        "dark_bounds": dark_bounds,
        "detector": "selected-light-accent-connected-component",
        "search_bounds": [0, 0, width, search_bottom],
        "selected_theme_card": card_candidates[0],
        "system_bounds": system_bounds,
        "system_component": system,
    }


def theme_segment_contrast(
    image: Image.Image,
    bounds: list[int],
    background_rgb: list[int],
) -> float:
    left, top, right, bottom = bounds
    if right - left <= 4 or bottom - top <= 4:
        raise LabFailure(f"invalid Zed theme segment bounds: {bounds}")
    segment = image.convert("RGB").crop((left + 2, top + 2, right - 2, bottom - 2))
    pixels = list(segment.get_flattened_data())
    contrasting = sum(
        max(abs(pixel[channel] - background_rgb[channel]) for channel in range(3)) >= 10
        for pixel in pixels
    )
    return round(contrasting / len(pixels), 6)


def rgb_luminance(rgb: list[int]) -> float:
    return round(0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2], 3)


def exercise_zed_mouse(
    container: str,
    window_id: str,
    geometry: dict[str, int],
    directory: Path,
    initial: dict[str, Any],
) -> dict[str, Any]:
    before_path = directory / "window-before-pointer.rgb.png"
    with Image.open(directory / "window-direct.rgb.png") as source:
        before = source.convert("RGB")
    before.save(before_path, format="PNG")
    target = detect_zed_system_theme_control(before)
    click_x, click_y = target["click_position"]
    if click_x >= geometry["width"] or click_y >= geometry["height"]:
        raise LabFailure("detected Zed Dark theme target lies outside the Xpra window")

    before_background = initial["image"]["dominant_rgb"]
    before_selection = {
        "dark_contrast_ratio": theme_segment_contrast(
            before, target["dark_bounds"], before_background
        ),
        "system_contrast_ratio": theme_segment_contrast(
            before, target["system_bounds"], before_background
        ),
    }
    if (
        before_selection["system_contrast_ratio"] < 0.8
        or before_selection["dark_contrast_ratio"] > 0.35
        or rgb_luminance(before_background) < 180
    ):
        raise LabFailure(
            "detected Zed theme control does not show the expected light System state"
        )

    log_paths = {
        "client": directory / "client.stdout",
        "server": directory / "server.stderr",
        "zed": directory / "zed.stderr",
    }
    log_offsets = {
        name: path.stat().st_size if path.is_file() else 0
        for name, path in log_paths.items()
    }
    before_screenshots = {
        path.relative_to(directory)
        for path in directory.glob("screen-updates/*/*/screenshot.png")
    }
    podman_exec(
        container,
        [
            "env",
            f"DISPLAY={CLIENT_DISPLAY}",
            "xdotool",
            "windowactivate",
            "--sync",
            window_id,
            "mousemove",
            "--sync",
            "--window",
            window_id,
            str(click_x),
            str(click_y),
            "click",
            "1",
        ],
    )

    input_path: dict[str, bool] = {}

    def read_log_suffix(name: str) -> str:
        path = log_paths[name]
        if not path.is_file():
            return ""
        with path.open("rb") as stream:
            stream.seek(log_offsets[name])
            return stream.read().decode("utf-8", errors="replace")

    def pointer_path_complete() -> bool:
        nonlocal input_path
        client_log = read_log_suffix("client")
        server_log = read_log_suffix("server")
        zed_log = read_log_suffix("zed")
        input_path = {
            "client_coordinates": (f", {click_x}, {click_y})" in client_log),
            "client_press_release": bool(
                re.search(r"_button_action\(1,[^\n]+, True\)", client_log)
                and re.search(r"_button_action\(1,[^\n]+, False\)", client_log)
            ),
            "server_coordinates": (f"move_pointer({click_x}, {click_y}," in server_log),
            "server_press_release": (
                "click(1, True" in server_log and "click(1, False" in server_log
            ),
            "zed_coordinates": bool(
                re.search(
                    rf"wl_pointer#\d+\.(?:enter|motion)\([^\n]*"
                    rf"{click_x}\.0+,\s*{click_y}\.0+",
                    zed_log,
                )
            ),
            "zed_press_release": bool(
                re.search(r"wl_pointer#\d+\.button\([^\n]+,\s*272,\s*1\)", zed_log)
                and re.search(
                    r"wl_pointer#\d+\.button\([^\n]+,\s*272,\s*0\)",
                    zed_log,
                )
            ),
        }
        return all(input_path.values())

    wait_for("pointer path from Xpra client to Zed", pointer_path_complete, timeout=10)
    after: dict[str, Any] | None = None

    def theme_changed() -> bool:
        nonlocal after
        capture_xwd(
            container,
            "window-after-pointer.xwd",
            window_id=window_id,
            announce=False,
        )
        after = convert_xwd(directory, "window-after-pointer")
        dominant = after["image"]["dominant_rgb"]
        return bool(
            after["image"]["rgb_sha256"] != initial["image"]["rgb_sha256"]
            and max(dominant) < 100
            and after["xwd"]["unique_rgb_colors"] > 100
        )

    wait_for("Zed response to the forwarded pointer click", theme_changed)
    assert after is not None
    after_path = directory / "window-after-pointer.rgb.png"
    with Image.open(after_path) as source:
        after_image = source.convert("RGB")
    if before.size != after_image.size:
        raise LabFailure("Zed window size changed during the pointer proof")

    after_background = after["image"]["dominant_rgb"]
    after_selection = {
        "dark_contrast_ratio": theme_segment_contrast(
            after_image, target["dark_bounds"], after_background
        ),
        "system_contrast_ratio": theme_segment_contrast(
            after_image, target["system_bounds"], after_background
        ),
    }
    difference = ImageChops.difference(before, after_image)
    difference_path = directory / "window-pointer-diff.rgb.png"
    difference.save(difference_path, format="PNG")
    difference_pixels = list(difference.get_flattened_data())
    changed_pixel_ratio = sum(max(pixel) >= 10 for pixel in difference_pixels) / len(
        difference_pixels
    )
    mean_absolute_difference = sum(ImageStat.Stat(difference).mean) / 3
    before_luminance = rgb_luminance(before_background)
    after_luminance = rgb_luminance(after_background)
    if (
        after_selection["dark_contrast_ratio"] < 0.8
        or after_selection["system_contrast_ratio"] > 0.35
        or after_luminance >= 100
        or before_luminance - after_luminance < 80
        or changed_pixel_ratio < 0.75
        or mean_absolute_difference < 50
    ):
        raise LabFailure("Zed did not visibly change from System light to Dark")

    matching_updates: list[dict[str, Any]] = []

    def post_click_server_frame() -> bool:
        nonlocal matching_updates
        current_screenshots = {
            path.relative_to(directory)
            for path in directory.glob("screen-updates/*/*/screenshot.png")
        }
        comparisons: list[dict[str, Any]] = []
        for relative_path in sorted(current_screenshots - before_screenshots):
            try:
                comparison = compare_rgb_images(
                    directory / relative_path,
                    after_path,
                )
            except OSError:
                continue
            comparison["source"] = str(relative_path)
            comparisons.append(comparison)
        matching_updates = [
            comparison
            for comparison in comparisons
            if comparison["same_size"]
            and comparison["mean_absolute_error"] is not None
            and comparison["mean_absolute_error"] <= 15
        ]
        return bool(matching_updates)

    wait_for(
        "post-click Zed frame saved by the Xpra server",
        post_click_server_frame,
    )
    return {
        "after": after,
        "artifacts": {
            "after": after_path.name,
            "before": before_path.name,
            "difference": difference_path.name,
        },
        "before_selection": before_selection,
        "changed_pixel_ratio": round(changed_pixel_ratio, 6),
        "clicked_relative_position": [click_x, click_y],
        "dark_theme_selected": True,
        "after_selection": after_selection,
        "dominant_luminance": {
            "after": after_luminance,
            "before": before_luminance,
        },
        "input_path": input_path,
        "mean_absolute_difference": round(mean_absolute_difference, 6),
        "pixels_changed": (
            after["image"]["rgb_sha256"] != initial["image"]["rgb_sha256"]
        ),
        "post_click_server_frames": matching_updates,
        "target": target,
    }


def capture_vulkan_motion(
    container: str,
    window_id: str,
    directory: Path,
    initial: dict[str, Any],
) -> dict[str, Any]:
    """Prove that the forwarded Vulkan window contains changing real frames."""
    later: dict[str, Any] | None = None

    def changed() -> bool:
        nonlocal later
        capture_xwd(
            container,
            "window-motion.xwd",
            window_id=window_id,
            announce=False,
        )
        later = convert_xwd(directory, "window-motion")
        return bool(
            initial["xwd"]["unique_rgb_colors"] > 100
            and later["xwd"]["unique_rgb_colors"] > 100
            and initial["image"]["rgb_sha256"] != later["image"]["rgb_sha256"]
        )

    time.sleep(0.5)
    wait_for("changing nonuniform Vulkan frames", changed, timeout=15)
    assert later is not None
    return {
        "changed": True,
        "first_rgb_sha256": initial["image"]["rgb_sha256"],
        "second": later,
        "second_rgb_sha256": later["image"]["rgb_sha256"],
    }


def exercise_interaction_fixture(
    server: str,
    client: str,
    window_id: str,
    directory: Path,
    *,
    close_with_keyboard: bool,
) -> dict[str, Any]:
    """Forward pointer and optional keyboard input through the real Xpra client."""
    geometry = window_geometry(client, window_id)
    click_x = geometry["width"] // 2
    click_y = geometry["height"] // 2
    capture_xwd(
        client,
        "interaction-before.xwd",
        window_id=window_id,
        announce=False,
    )
    before = convert_xwd(directory, "interaction-before")
    podman_exec(
        client,
        [
            "env",
            f"DISPLAY={CLIENT_DISPLAY}",
            "xdotool",
            "windowactivate",
            "--sync",
            window_id,
            "mousemove",
            "--sync",
            "--window",
            window_id,
            str(click_x),
            str(click_y),
            "click",
            "1",
        ],
    )
    wait_for(
        "GTK pointer marker",
        lambda: (
            podman_exec(
                server,
                ["test", "-f", INTERACTION_CLICK_MARKER],
                check=False,
                announce=False,
            ).returncode
            == 0
        ),
    )
    after: dict[str, Any] | None = None

    def pointer_changed_frame() -> bool:
        nonlocal after
        capture_xwd(
            client,
            "interaction-after.xwd",
            window_id=window_id,
            announce=False,
        )
        after = convert_xwd(directory, "interaction-after")
        return bool(
            after["xwd"]["unique_rgb_colors"] > 10
            and after["image"]["rgb_sha256"] != before["image"]["rgb_sha256"]
        )

    wait_for("visible GTK pointer response", pointer_changed_frame)
    assert after is not None
    keyboard = False
    if close_with_keyboard:
        podman_exec(
            client,
            [
                "env",
                f"DISPLAY={CLIENT_DISPLAY}",
                "xdotool",
                "windowactivate",
                "--sync",
                window_id,
                "key",
                "Escape",
            ],
        )
        wait_for(
            "GTK keyboard marker",
            lambda: (
                podman_exec(
                    server,
                    ["test", "-f", INTERACTION_KEY_MARKER],
                    check=False,
                    announce=False,
                ).returncode
                == 0
            ),
        )
        keyboard = True
    return {
        "attempted": True,
        "clicked_relative_position": [click_x, click_y],
        "keyboard_escape_received": keyboard,
        "pointer_changed_pixels": True,
        "pointer_marker_present": True,
        "before": before,
        "after": after,
    }


def start_sway_desktop(
    container: str,
    directory: Path,
    render_node: Path,
) -> dict[str, Any]:
    sway_script = (
        "umask 022; printf '%s\\n' \"$$\" > /artifacts/sway.pid; "
        "exec env XDG_RUNTIME_DIR=/tmp/client-runtime "
        "WLR_BACKENDS=headless WLR_RENDERER=gles2 "
        f"WLR_RENDER_DRM_DEVICE={shlex.quote(str(render_node))} "
        "WLR_LIBINPUT_NO_DEVICES=1 WLR_HEADLESS_OUTPUTS=1 "
        "sway -d -c /dev/null "
        ">/artifacts/sway.stdout 2>/artifacts/sway.stderr"
    )
    podman_exec(container, ["bash", "-lc", sway_script], detach=True)
    sway_socket = ""

    def sway_ready() -> bool:
        nonlocal sway_socket
        result = podman_exec(
            container,
            [
                "bash",
                "-lc",
                (
                    "find /tmp/client-runtime -maxdepth 1 -type s "
                    "-name 'sway-ipc.*.sock' -print -quit"
                ),
            ],
            check=False,
            announce=False,
        )
        sway_socket = result.stdout.strip()
        if not sway_socket:
            return False
        return (
            podman_exec(
                container,
                [
                    "env",
                    "XDG_RUNTIME_DIR=/tmp/client-runtime",
                    "swaymsg",
                    "-s",
                    sway_socket,
                    "-t",
                    "get_outputs",
                ],
                check=False,
                announce=False,
            ).returncode
            == 0
        )

    wait_for("headless Sway compositor", sway_ready)
    sway_base = [
        "env",
        "XDG_RUNTIME_DIR=/tmp/client-runtime",
        "swaymsg",
        "-s",
        sway_socket,
    ]
    podman_exec(
        container,
        [*sway_base, "output", "HEADLESS-1", "mode", "1600x1200"],
    )
    podman_exec(
        container,
        [*sway_base, "output", "HEADLESS-1", "bg", "#000000", "solid_color"],
    )
    podman_exec(
        container,
        [
            *sway_base,
            "exec",
            (
                "sh -c 'env | sort > /artifacts/sway-child.env; "
                "xdpyinfo > /artifacts/xwayland-xdpyinfo.txt 2>&1'"
            ),
        ],
    )

    def xwayland_ready() -> bool:
        environment_path = directory / "sway-child.env"
        display_info = directory / "xwayland-xdpyinfo.txt"
        return bool(
            environment_path.is_file()
            and f"DISPLAY={CLIENT_DISPLAY}"
            in environment_path.read_text(encoding="utf-8", errors="replace")
            and display_info.is_file()
            and "name of display:"
            in display_info.read_text(encoding="utf-8", errors="replace")
        )

    wait_for("Sway Xwayland display", xwayland_ready)
    child_environment = {}
    for line in (
        (directory / "sway-child.env")
        .read_text(encoding="utf-8", errors="replace")
        .splitlines()
    ):
        key, separator, value = line.partition("=")
        if separator:
            child_environment[key] = value
    wayland_display = child_environment.get("WAYLAND_DISPLAY", "")
    if not wayland_display:
        raise LabFailure("Sway did not publish WAYLAND_DISPLAY")

    sway_log_path = directory / "sway.stderr"
    renderer = ""

    def hardware_renderer_ready() -> bool:
        nonlocal renderer
        if not sway_log_path.is_file():
            return False
        log = sway_log_path.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"GL renderer: ([^\n]+)", log)
        renderer = match.group(1).strip() if match else ""
        return bool(
            str(render_node) in log
            and renderer
            and not any(
                name in renderer.lower()
                for name in ("llvmpipe", "softpipe", "swrast", "software rasterizer")
            )
        )

    wait_for("Sway AMD hardware renderer", hardware_renderer_ready)
    probe = compositor_probe(container) or {}
    evidence: dict[str, Any] = {
        "backend": "sway-xwayland",
        "display": CLIENT_DISPLAY,
        "hardware_renderer": True,
        "render_node": str(render_node),
        "renderer": renderer,
        "rgba_visual": bool(probe.get("rgba_visual")),
        "sway_socket": sway_socket,
        "wayland_display": wayland_display,
    }
    evidence["background_capture"] = capture_grim(
        container,
        directory,
        "root-before",
        wayland_display,
    )
    background = evidence["background_capture"]
    if background["image"]["quantized_rgb_colors"] != 1 or background["image"][
        "dominant_rgb"
    ] != [0, 0, 0]:
        raise LabFailure("the controlled black Sway background was not established")
    evidence["background_expected_rgb"] = [0, 0, 0]
    (directory / "compositor.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return evidence


def start_client_desktop(
    container: str,
    directory: Path,
    *,
    hardware: bool,
    render_node: Path,
) -> dict[str, Any]:
    if hardware:
        return start_sway_desktop(container, directory, render_node)
    xvfb_script = (
        "umask 022; printf '%s\\n' \"$$\" > /artifacts/xvfb.pid; "
        f"exec Xvfb {CLIENT_DISPLAY} -screen 0 1600x1200x24 "
        "+extension Composite -nolisten tcp -noreset "
        ">/artifacts/xvfb.stdout 2>/artifacts/xvfb.stderr"
    )
    podman_exec(container, ["bash", "-lc", xvfb_script], detach=True)
    wait_for(
        "client X11 display",
        lambda: (
            podman_exec(
                container,
                ["xdpyinfo", "-display", CLIENT_DISPLAY],
                check=False,
                announce=False,
            ).returncode
            == 0
        ),
    )
    openbox_script = (
        "umask 022; printf '%s\\n' \"$$\" > /artifacts/openbox.pid; "
        f"exec env DISPLAY={CLIENT_DISPLAY} openbox --sm-disable "
        ">/artifacts/openbox.stdout 2>/artifacts/openbox.stderr"
    )
    podman_exec(container, ["bash", "-lc", openbox_script], detach=True)
    picom_script = (
        "umask 022; printf '%s\\n' \"$$\" > /artifacts/picom.pid; "
        f"exec env DISPLAY={CLIENT_DISPLAY} picom --backend xrender "
        "--config /dev/null --log-level debug "
        ">/artifacts/picom.stdout 2>/artifacts/picom.stderr"
    )
    podman_exec(container, ["bash", "-lc", picom_script], detach=True)
    probe: dict[str, Any] | None = None

    def composited() -> bool:
        nonlocal probe
        probe = compositor_probe(container)
        return bool(probe and probe.get("composited") and probe.get("rgba_visual"))

    wait_for("composited X11 display with an RGBA visual", composited)
    assert probe is not None
    podman_exec(
        container,
        [
            "env",
            f"DISPLAY={CLIENT_DISPLAY}",
            "xsetroot",
            "-solid",
            "#000000",
        ],
    )
    (directory / "compositor.json").write_text(
        json.dumps(probe, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    capture_xwd(container, "root-before.xwd")
    probe["background_capture"] = convert_xwd(directory, "root-before")
    background = probe["background_capture"]
    if background["xwd"]["unique_rgb_colors"] != 1 or background["image"][
        "dominant_rgb"
    ] != [0, 0, 0]:
        raise LabFailure("the controlled black client background was not established")
    probe["background_expected_rgb"] = [0, 0, 0]
    return probe


def classify_h264_picture_fallback(
    *,
    policy: str,
    render_node: Path,
    log_evidence: dict[str, Any],
    updates: dict[str, Any],
    codec_hardware: dict[str, Any],
) -> tuple[
    dict[str, bool],
    dict[str, bool],
    dict[str, bool],
    dict[str, bool],
    dict[str, bool],
]:
    """Classify the maintainer-suggested picture-fallback diagnostic profile."""
    client_graphics = codec_hardware.get("client_graphics_process", {})
    client_desktop = codec_hardware.get("client_desktop", {})
    renderer = log_evidence["opengl_renderer"]
    software_renderer = any(
        name in renderer.lower()
        for name in ("llvmpipe", "softpipe", "swrast", "software rasterizer")
    )
    actual_encodings = set(updates["encodings"])
    positive_payloads = updates["count"] > 0 and all(
        update["payload_bytes"] > 0 for update in updates["updates"]
    )
    startup_checks = {
        "fallback_policy_selected": policy in {"fallback-auto", "fallback-h264"},
        "per_window_h264_capabilities_applied": bool(
            log_evidence["h264_per_window_negotiation_applied"]
        ),
        "no_pre_negotiation_h264_error": not log_evidence[
            "h264_pre_negotiation_errors"
        ],
        "no_server_initial_data_error": not log_evidence["server_initial_data_errors"],
        "no_server_logging_error": log_evidence["server_logging_errors"] == 0,
    }
    encoding_checks = {
        "only_picture_packets": bool(actual_encodings)
        and actual_encodings <= {"rgb24", "rgb32"},
        "h264_not_reached": "h264" not in actual_encodings,
        "positive_saved_payload": positive_payloads,
        "rgb_encoder_ran": log_evidence["rgb_encodes"] > 0,
        "no_h264_pipeline_error": not log_evidence["h264_pipeline_errors"],
    }
    transport_checks = {
        "client_received_picture_packets": log_evidence["client_draw_regions"] > 0,
        "client_received_no_h264_packet": log_evidence["h264_draw_regions"] == 0,
    }
    paint_checks = {
        "opengl_painted_rgb": log_evidence["opengl_rgb_paints"] > 0,
        "paint_acknowledged": log_evidence["client_successful_paints"] > 0,
        "no_paint_error": not log_evidence["paint_errors"],
    }
    presentation_checks = {
        "opengl_presented": log_evidence["opengl_presentations"] > 0,
        "hardware_opengl_renderer": bool(renderer) and not software_renderer,
        "hardware_desktop_renderer": bool(client_desktop.get("hardware_renderer")),
        "client_render_node_open": str(render_node)
        in client_graphics.get("render_nodes", []),
        "client_gpu_driver_mapped": any(
            "radeonsi_dri" in mapping or "libgallium" in mapping
            for mapping in client_graphics.get("gpu_mappings", [])
        ),
    }
    return (
        startup_checks,
        encoding_checks,
        transport_checks,
        paint_checks,
        presentation_checks,
    )


def lifecycle_boundary_checks(
    lifecycle_profile: str, lifecycle: dict[str, Any]
) -> dict[str, bool]:
    """Classify only observed process and command outcomes for one lifecycle."""
    if lifecycle_profile == "application-exit":
        return {
            "client_exit_zero": lifecycle.get("client_exit_status") == 0,
            "client_exited_after_server": bool(
                lifecycle.get("client_exited_after_server")
            ),
            "server_exited_after_application": bool(
                lifecycle.get("server_exited_after_application")
            ),
        }
    if lifecycle_profile == "detach":
        return {
            "detach_command_succeeded": lifecycle.get("detach_returncode") == 0,
            "client_exit_zero": lifecycle.get("client_exit_status") == 0,
            "client_exited_after_detach": bool(
                lifecycle.get("client_exited_after_detach")
            ),
            "server_survived_detach": bool(lifecycle.get("server_survived_detach")),
            "application_survived_detach": bool(
                lifecycle.get("application_survived_detach")
            ),
            "server_exited_after_application": bool(
                lifecycle.get("server_exited_after_application")
            ),
        }
    if lifecycle_profile != "transport-loss":
        raise LabFailure(f"unsupported lifecycle profile: {lifecycle_profile}")
    exit_status = lifecycle.get("client_exit_status")
    return {
        "transport_disconnect_succeeded": (
            lifecycle.get("transport_disconnect_returncode") == 0
        ),
        "client_exit_nonzero": isinstance(exit_status, int)
        and not isinstance(exit_status, bool)
        and exit_status != 0,
        "client_exited_after_transport_loss": bool(
            lifecycle.get("client_exited_after_transport_loss")
        ),
        "server_survived_transport_loss": bool(
            lifecycle.get("server_survived_transport_loss")
        ),
        "application_survived_transport_loss": bool(
            lifecycle.get("application_survived_transport_loss")
        ),
        "server_exited_after_application": bool(
            lifecycle.get("server_exited_after_application")
        ),
    }


def application_boundary_checks(
    *,
    application: str,
    application_activity: dict[str, Any],
    application_gpu: dict[str, Any],
    log_evidence: dict[str, Any],
    render_node: Path,
) -> dict[str, bool]:
    """Return checks observable for the selected tracked application."""
    render_node_open = str(render_node) in application_gpu["render_nodes"]
    radv_mapped = any(
        "libvulkan_radeon" in mapping for mapping in application_gpu["gpu_mappings"]
    )
    if application in {"hardware", "vkcube"}:
        return {
            "process_alive_at_capture": bool(application_activity.get("process_alive")),
            "render_node_open": render_node_open,
            "radv_mapped": radv_mapped,
            "vulkan_frames_changed": bool(
                application_activity.get("vulkan_motion", {}).get("changed")
            ),
        }
    if application != "zed":
        return {
            "process_alive_at_capture": bool(application_activity.get("process_alive"))
        }
    return {
        "render_node_open": render_node_open,
        "radv_mapped": radv_mapped,
        "wayland_ack_configure": log_evidence["wayland_protocol"]["ack_configure"] > 0,
        "wayland_damage": log_evidence["wayland_protocol"]["damage_buffer"] > 0,
        "wayland_commit": log_evidence["wayland_protocol"]["commits"] > 0,
    }


def classify_boundaries(
    *,
    args: argparse.Namespace,
    application_activity: dict[str, Any],
    application_gpu: dict[str, Any],
    log_evidence: dict[str, Any],
    updates: dict[str, Any],
    direct: dict[str, Any],
    composited: dict[str, Any],
    source_image: dict[str, Any] | None,
    codec_hardware: dict[str, Any],
    interaction: dict[str, Any],
    pixel_evidence: dict[str, Any],
    lifecycle: dict[str, bool],
    interaction_updates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    application_checks = application_boundary_checks(
        application=args.application,
        application_activity=application_activity,
        application_gpu=application_gpu,
        log_evidence=log_evidence,
        render_node=args.render_node,
    )
    wayland_checks = {
        "nonempty_commit": log_evidence["nonempty_wayland_commits"] > 0,
        "xrgb8888_dmabuf": "0x34325258" in log_evidence["dmabuf_fourcc"],
        "rgbx_image": log_evidence["rgbx_images"] > 0,
    }
    if args.application != "zed":
        wayland_checks.pop("xrgb8888_dmabuf")
        wayland_checks.pop("rgbx_image")
    positive_payloads = updates["count"] > 0 and all(
        update["payload_bytes"] > 0 for update in updates["updates"]
    )
    if args.encoding == "rgb":
        startup_checks: dict[str, bool] | None = None
        encoding_checks = {
            "no_rgb_encode_error": not log_evidence["rgb_encode_errors"],
            "only_rgb_packets": bool(updates["encodings"])
            and set(updates["encodings"]) <= {"rgb24", "rgb32"},
            "positive_saved_payload": positive_payloads,
            "rgb_encoder_ran": log_evidence["rgb_encodes"] > 0,
        }
        transport_checks = {
            "client_received_rgb": log_evidence["client_draw_regions"] > 0
        }
        paint_checks = {
            "cairo_painted_rgb": log_evidence["cairo_paints"] > 0,
            "paint_acknowledged": log_evidence["client_successful_paints"] > 0,
            "no_paint_error": not log_evidence["paint_errors"],
        }
        presentation_checks = {
            "draw_widget_ran": log_evidence["gtk_draw_widgets"] > 0,
            "cairo_draw_ran": log_evidence["gtk_cairo_draws"] > 0,
        }
        encoding_boundary = "rgb_encoder"
        paint_boundary = "cairo_paint"
        presentation_boundary = "gtk_presentation"
        client_presented = log_evidence["cairo_paints"] > 0
    elif args.h264_client_policy in H264_FALLBACK_POLICIES:
        (
            startup_checks,
            encoding_checks,
            transport_checks,
            paint_checks,
            presentation_checks,
        ) = classify_h264_picture_fallback(
            policy=args.h264_client_policy,
            render_node=args.render_node,
            log_evidence=log_evidence,
            updates=updates,
            codec_hardware=codec_hardware,
        )
        encoding_boundary = "picture_fallback"
        paint_boundary = "opengl_rgb_paint"
        presentation_boundary = "opengl_presentation"
        client_presented = log_evidence["opengl_presentations"] > 0
    else:
        encode = codec_hardware.get("h264_encode", {})
        decode = codec_hardware.get("h264_decode", {})
        packet_chain = codec_hardware.get("h264_packet_chain", {})
        production = codec_hardware.get("h264_production", {})
        client_graphics = codec_hardware.get("client_graphics_process", {})
        client_desktop = codec_hardware.get("client_desktop", {})
        renderer = log_evidence["opengl_renderer"]
        software_renderer = any(
            name in renderer.lower()
            for name in ("llvmpipe", "softpipe", "swrast", "software rasterizer")
        )
        startup_checks = {
            "per_window_h264_capabilities_applied": bool(
                log_evidence["h264_per_window_negotiation_applied"]
            ),
            "no_pre_negotiation_h264_error": not log_evidence[
                "h264_pre_negotiation_errors"
            ],
            "no_server_initial_data_error": not log_evidence[
                "server_initial_data_errors"
            ],
            "no_server_logging_error": log_evidence["server_logging_errors"] == 0,
        }
        packet_contract_name = (
            "h264_main_with_only_lossless_rgb_edges"
            if args.h264_client_policy == "adaptive-alpha"
            else "only_h264_packets"
        )
        packet_contract_passed = (
            h264_with_lossless_rgb_edges(updates)
            if args.h264_client_policy == "adaptive-alpha"
            else only_positive_h264_packets(updates)
        )
        encoding_checks = {
            "no_h264_pipeline_error": not log_evidence["h264_pipeline_errors"],
            packet_contract_name: packet_contract_passed,
            "positive_saved_payload": positive_payloads,
            "hardware_encode_entrypoint": bool(encode.get("entrypoint_present")),
            "hardware_encode_h264_profile": bool(encode.get("h264_profile_present")),
            "hardware_encode_production_size": bool(
                encode.get("production_dimensions")
            ),
            "hardware_encoded_frames": int(encode.get("submitted_frames", 0)) > 0,
            "production_stream_matches_encoder_context": bool(
                production.get("production_proven")
            ),
        }
        if args.application == "hardware":
            encoding_checks["interaction_window_h264_packets"] = (
                only_positive_h264_packets(interaction_updates)
            )
        transport_checks = {
            "client_received_h264": log_evidence["h264_draw_regions"] > 0,
            "saved_packet_matches_process_draw": bool(
                packet_chain.get("process_draw_matches_saved_packet")
            ),
            "saved_packet_matches_draw_region": bool(
                packet_chain.get("draw_region_matches_saved_packet")
            ),
        }
        paint_checks = {
            "hardware_decode_entrypoint": bool(decode.get("entrypoint_present")),
            "hardware_decode_h264_profile": bool(decode.get("h264_profile_present")),
            "hardware_decode_production_size": bool(
                decode.get("production_dimensions")
            ),
            "hardware_decoded_frames": int(decode.get("submitted_frames", 0)) > 0,
            "libva_decoded_saved_packet": bool(
                packet_chain.get("libva_decode_log_matches_saved_packet")
            ),
            "nv12_painted": bool(packet_chain.get("nv12_painted")),
            "decode_acknowledged": bool(packet_chain.get("acknowledged")),
            "no_paint_error": not log_evidence["paint_errors"],
        }
        presentation_checks = {
            "packet_chain_presented": bool(packet_chain.get("complete")),
            "opengl_presented": bool(packet_chain.get("presented_before_later_work")),
            "hardware_opengl_renderer": bool(renderer) and not software_renderer,
            "hardware_desktop_renderer": bool(client_desktop.get("hardware_renderer")),
            "client_render_node_open": str(args.render_node)
            in client_graphics.get("render_nodes", []),
            "client_gpu_driver_mapped": any(
                "radeonsi_dri" in mapping or "libgallium" in mapping
                for mapping in client_graphics.get("gpu_mappings", [])
            ),
        }
        encoding_boundary = "h264_encoder"
        paint_boundary = "h264_decoder"
        presentation_boundary = "opengl_presentation"
        client_presented = bool(packet_chain.get("complete"))
    direct_image = direct["image"]
    direct_xwd = direct["xwd"]
    final_checks = {
        "direct_rgb_nonuniform": direct_xwd["unique_rgb_colors"] > 100,
        "focused_screen_nonuniform": composited["quantized_rgb_colors"] > 100,
        "focused_screen_not_background": (composited["background_match_ratio"] < 0.95),
        "source_nonuniform": bool(
            source_image and source_image["quantized_rgb_colors"] > 32
        ),
        "server_frame_matches_client": bool(
            pixel_evidence.get("matching_server_frame")
        ),
        "red_blue_order_verified": bool(pixel_evidence.get("red_blue_order_verified")),
        "window_central_alpha_opaque": direct_image["central_opaque_ratio"] >= 0.99,
    }
    lifecycle_checks = lifecycle_boundary_checks(args.lifecycle, lifecycle)
    if args.application == "zed":
        interaction_checks = {
            "dark_theme_selected_by_pointer": bool(
                interaction.get("dark_theme_selected")
            ),
            "pointer_changed_pixels": bool(interaction.get("pixels_changed")),
        }
    elif args.application in {"hardware", "gtk"}:
        interaction_checks = {
            "pointer_marker_present": bool(interaction.get("pointer_marker_present")),
            "pointer_changed_pixels": bool(interaction.get("pointer_changed_pixels")),
        }
        if args.lifecycle == "application-exit":
            interaction_checks["keyboard_escape_received"] = bool(
                interaction.get("keyboard_escape_received")
            )
    else:
        interaction_checks = {"not_required_for_vulkan_control": True}
    ordered: dict[str, dict[str, bool]] = {
        "application": application_checks,
        "wayland_capture": wayland_checks,
    }
    if startup_checks is not None:
        ordered["h264_startup"] = startup_checks
    ordered.update(
        {
            encoding_boundary: encoding_checks,
            "transport": transport_checks,
            paint_boundary: paint_checks,
            presentation_boundary: presentation_checks,
            "final_pixels": final_checks,
            "interaction": interaction_checks,
            "lifecycle": lifecycle_checks,
        }
    )
    first_failed = "passed"
    for name, checks in ordered.items():
        if not all(checks.values()):
            first_failed = name
            break
    final_pixels_passed = all(final_checks.values())
    background_ratio = composited["background_match_ratio"]
    if final_pixels_passed and client_presented:
        appearance = "rendered"
    elif direct_xwd["unique_rgb_colors"] <= 2 and background_ratio >= 0.97:
        appearance = "transparent-empty"
    elif direct_xwd["unique_rgb_colors"] <= 2:
        appearance = "opaque-empty"
    else:
        appearance = "unrendered"
    result = {
        "appearance": appearance,
        "boundaries": ordered,
        "first_failed_boundary": first_failed,
    }
    if args.encoding == "h264" and args.h264_client_policy in H264_ACCEPTANCE_POLICIES:
        hardware_boundaries = (
            "h264_encoder",
            "transport",
            "h264_decoder",
            "opengl_presentation",
            "final_pixels",
            "interaction",
            "lifecycle",
        )
        result["hardware_pipeline_passed"] = all(
            all(ordered[name].values()) for name in hardware_boundaries
        )
        result["startup_clean"] = all(startup_checks.values())
    elif args.encoding == "h264":
        result["diagnostic_only"] = True
        result["h264_transport_reached"] = "h264" in set(updates["encodings"])
        result["picture_fallback_observed"] = all(
            all(ordered[name].values())
            for name in (
                "h264_startup",
                "picture_fallback",
                "transport",
                "opengl_rgb_paint",
                "opengl_presentation",
                "final_pixels",
                "interaction",
                "lifecycle",
            )
        )
    return result


def run_scenario(
    args: argparse.Namespace,
    scenario: Scenario,
    *,
    commit: str,
    run_id: str,
    server_context_digest: str,
    client_context_digest: str,
    server_image_id: str,
    client_image_id: str,
    result_directory: Path,
) -> dict[str, Any]:
    directory = result_directory / scenario.name
    directory.mkdir(mode=0o700)
    ensure_private_directory(directory)
    suffix = uuid.uuid4().hex[:10]
    network = f"xpra-lab-live-{suffix}"
    server = f"xpra-lab-live-server-{suffix}"
    client = f"xpra-lab-live-client-{suffix}"
    containers: list[str] = []
    container_labels = {
        server: {
            "io.xpra.lab.context": server_context_digest,
            "io.xpra.lab.image-id": server_image_id,
            "io.xpra.lab.owner": "live",
            "io.xpra.lab.role": "server",
            "io.xpra.lab.run-id": run_id,
            "io.xpra.lab.source": commit,
        },
        client: {
            "io.xpra.lab.context": client_context_digest,
            "io.xpra.lab.image-id": client_image_id,
            "io.xpra.lab.owner": "live",
            "io.xpra.lab.role": "client",
            "io.xpra.lab.run-id": run_id,
            "io.xpra.lab.source": commit,
        },
    }
    network_labels = {
        "io.xpra.lab.owner": "live",
        "io.xpra.lab.run-id": run_id,
    }
    network_created = False
    report: dict[str, Any] = {"name": scenario.name, "result": "failed"}
    try:
        run(
            [
                "podman",
                "network",
                "create",
                "--label",
                "io.xpra.lab.owner=live",
                "--label",
                f"io.xpra.lab.run-id={run_id}",
                network,
            ]
        )
        network_created = True
        server_run = [
            "podman",
            "run",
            "--detach",
            "--name",
            server,
            "--label",
            "io.xpra.lab.owner=live",
            "--label",
            f"io.xpra.lab.run-id={run_id}",
            "--label",
            "io.xpra.lab.role=server",
            "--label",
            f"io.xpra.lab.source={commit}",
            "--label",
            f"io.xpra.lab.context={server_context_digest}",
            "--label",
            f"io.xpra.lab.image-id={server_image_id}",
            "--network",
            network,
            "--network-alias",
            "xpra-server",
            "--userns",
            "keep-id",
            "--device",
            f"{args.render_node}:{args.render_node}",
            "--group-add",
            "keep-groups",
            "--shm-size",
            "1g",
            "--env",
            "XDG_RUNTIME_DIR=/tmp/server-runtime",
            "--volume",
            f"{directory.resolve()}:/artifacts",
        ]
        if args.application == "zed":
            server_run.extend(
                ["--volume", f"{args.zed_directory.resolve()}:/opt/zed.app:ro"]
            )
        server_run.append(server_image_id)
        containers.append(server)
        run(server_run)
        verify_container_image(server, server_image_id)

        client_run = [
            "podman",
            "run",
            "--detach",
            "--name",
            client,
            "--label",
            "io.xpra.lab.owner=live",
            "--label",
            f"io.xpra.lab.run-id={run_id}",
            "--label",
            "io.xpra.lab.role=client",
            "--label",
            f"io.xpra.lab.source={commit}",
            "--label",
            f"io.xpra.lab.context={client_context_digest}",
            "--label",
            f"io.xpra.lab.image-id={client_image_id}",
            "--network",
            network,
            "--userns",
            "keep-id",
            "--shm-size",
            "1g",
            "--env",
            "XDG_RUNTIME_DIR=/tmp/client-runtime",
            "--volume",
            f"{directory.resolve()}:/artifacts",
        ]
        if args.encoding == "h264":
            client_run.extend(
                [
                    "--device",
                    f"{args.render_node}:{args.render_node}",
                    "--group-add",
                    "keep-groups",
                ]
            )
        client_run.append(client_image_id)
        containers.append(client)
        run(client_run)
        verify_container_image(client, client_image_id)

        for container, runtime in (
            (server, "/tmp/server-runtime"),
            (client, "/tmp/client-runtime"),
        ):
            podman_exec(container, ["install", "-d", "-m", "0700", runtime])

        versions = {
            "server": podman_exec(server, ["xpra", "--version"]).stdout.strip(),
            "client": podman_exec(client, ["xpra", "--version"]).stdout.strip(),
        }
        operating_systems = {
            "server": read_os_release(server),
            "client": read_os_release(client),
        }
        write_command_output(
            server,
            ["vulkaninfo", "--summary"],
            directory / "server-vulkaninfo.txt",
        )
        server_vainfo: subprocess.CompletedProcess[str] | None = None
        if args.encoding == "h264":
            server_vainfo = write_command_output(
                server,
                [
                    "env",
                    f"LIBVA_DRIVER_NAME={args.libva_driver}",
                    "vainfo",
                    "--display",
                    "drm",
                    "--device",
                    str(args.render_node),
                ],
                directory / "server-vainfo.txt",
            )
            write_command_output(
                client,
                [
                    "env",
                    f"LIBVA_DRIVER_NAME={args.libva_driver}",
                    "vainfo",
                    "--display",
                    "drm",
                    "--device",
                    str(args.render_node),
                ],
                directory / "client-vainfo.txt",
            )

        compositor = start_client_desktop(
            client,
            directory,
            hardware=args.encoding == "h264",
            render_node=args.render_node,
        )
        child_command, title_patterns, pid_file = application_contract(args.application)
        encoding_options = transport_encoding_options(
            args.encoding,
            args.h264_client_policy,
            client=False,
        )
        server_command = [
            "xpra",
            "seamless",
            SERVER_DISPLAY,
            "--minimal",
            "--backend=wayland",
            "--daemon=no",
            "--displayfd=1",
            f"--bind-tcp=0.0.0.0:{SERVER_PORT},auth=none",
            "--socket-dir=/tmp/server-runtime/xpra-sockets",
            "--socket-dirs=/tmp/server-runtime/xpra-sockets",
            "--sessions-dir=/tmp/server-runtime/xpra-sessions",
            (
                f"--session-name={args.application}-{args.encoding}-"
                f"{args.h264_client_policy}-lab"
            ),
            "--use-display=no",
            "--exit-with-client=no",
            "--exit-with-children=yes",
            "--terminate-children=yes",
            f"--start-child={child_command}",
            "--video-scaling=0",
            "--auto-refresh-delay=0",
            "--html=off",
            *encoding_options,
            "-d",
            "wayland,damage,encoding,encoder,argb",
        ]
        server_environment = [
            "XPRA_SCREEN_UPDATES_DIRECTORY=/artifacts/screen-updates",
        ]
        if args.encoding == "h264":
            server_environment.extend(
                [
                    f"LIBVA_DRIVER_NAME={args.libva_driver}",
                    f"XPRA_LIBVA_DEVICE={args.render_node}",
                    "LIBVA_TRACE=/artifacts/server-va",
                ]
            )
        server_script = (
            "umask 077; install -d -m 0700 /tmp/server-runtime/xpra-sockets "
            "/tmp/server-runtime/xpra-sessions /artifacts/screen-updates; "
            "printf '%s\\n' \"$$\" > /artifacts/server.pid; "
            f"exec env {' '.join(shlex.quote(value) for value in server_environment)} "
            f"{format_command(server_command)} "
            ">/artifacts/server.stdout 2>/artifacts/server.stderr"
        )
        podman_exec(server, ["bash", "-lc", server_script], detach=True)
        wait_for("server PID publication", lambda: (directory / "server.pid").is_file())
        server_pid = int((directory / "server.pid").read_text().strip())
        wait_for_log(
            server,
            server_pid,
            directory / "server.stderr",
            "xpra is ready.",
            "Wayland Xpra server readiness",
        )

        client_encoding_options = transport_encoding_options(
            args.encoding,
            args.h264_client_policy,
            client=True,
        )
        client_endpoint = f"xpra-server:{SERVER_PORT}"
        transport_proxy_pid: int | None = None
        if args.lifecycle == "transport-loss":
            proxy_script = (
                "umask 077; printf '%s\\n' \"$$\" "
                "> /artifacts/transport-proxy.pid; "
                f"exec socat TCP-LISTEN:{CLIENT_PROXY_PORT},reuseaddr "
                f"TCP:xpra-server:{SERVER_PORT} "
                ">/artifacts/transport-proxy.stdout "
                "2>/artifacts/transport-proxy.stderr"
            )
            podman_exec(client, ["bash", "-lc", proxy_script], detach=True)
            wait_for(
                "transport proxy PID publication",
                lambda: (directory / "transport-proxy.pid").is_file(),
            )
            transport_proxy_pid = int(
                (directory / "transport-proxy.pid").read_text().strip()
            )

            def transport_proxy_ready() -> bool:
                if not container_process_exists(client, transport_proxy_pid):
                    return False
                listeners = podman_exec(
                    client,
                    ["ss", "--listening", "--tcp", "--numeric"],
                    check=False,
                )
                return f":{CLIENT_PROXY_PORT} " in listeners.stdout

            wait_for("transport proxy listener", transport_proxy_ready)
            client_endpoint = f"127.0.0.1:{CLIENT_PROXY_PORT}"
        client_command = [
            "xpra",
            "attach",
            f"tcp://{client_endpoint}",
            "--minimal",
            "--compressors=none",
            "--quality=100",
            "--speed=100",
            "--reconnect=no",
            *client_encoding_options,
            "-d",
            "draw,paint,cairo,window,gtk,alpha,libva",
        ]
        client_environment = [f"DISPLAY={CLIENT_DISPLAY}"]
        if scenario.disable_alpha:
            client_environment.append("XPRA_ALPHA=0")
        if args.encoding == "h264":
            client_environment.extend(
                [
                    "GDK_BACKEND=x11",
                    "PYOPENGL_PLATFORM=x11",
                    f"LIBVA_DRIVER_NAME={args.libva_driver}",
                    f"XPRA_LIBVA_DEVICE={args.render_node}",
                    "LIBVA_TRACE=/artifacts/client-va",
                ]
            )
        client_script = (
            "umask 077; "
            f"env {' '.join(shlex.quote(value) for value in client_environment)} "
            f"{format_command(client_command)} "
            ">/artifacts/client.stdout 2>/artifacts/client.stderr & "
            "child=$!; printf '%s\\n' \"$child\" > /artifacts/client.pid; "
            'wait "$child"; status=$?; '
            'printf \'%s\\n\' "$status" > /artifacts/client.exit; exit "$status"'
        )
        podman_exec(client, ["bash", "-lc", client_script], detach=True)
        wait_for("client PID publication", lambda: (directory / "client.pid").is_file())
        client_pid = int((directory / "client.pid").read_text().strip())

        found: tuple[str, str] | None = None

        def application_window_ready() -> bool:
            nonlocal found
            if not container_process_exists(client, client_pid):
                log = (
                    (directory / "client.stderr").read_text(
                        encoding="utf-8", errors="replace"
                    )
                    if (directory / "client.stderr").is_file()
                    else ""
                )
                raise LabFailure(
                    f"Xpra client exited before the application window:\n{log[-12000:]}"
                )
            found = find_window(client, title_patterns)
            return found is not None

        wait_for("forwarded application window", application_window_ready)
        assert found is not None
        window_id, window_title = found
        frame_outcome = wait_for_frame_boundary(
            server,
            server_pid,
            client,
            client_pid,
            directory,
            args.encoding,
            args.h264_client_policy,
        )
        geometry = window_geometry(client, window_id)
        write_command_output(
            client,
            [
                "env",
                f"DISPLAY={CLIENT_DISPLAY}",
                "xprop",
                "-id",
                window_id,
            ],
            directory / "window-xprop.txt",
        )
        write_command_output(
            client,
            [
                "env",
                f"DISPLAY={CLIENT_DISPLAY}",
                "xwininfo",
                "-id",
                window_id,
            ],
            directory / "window-xwininfo.txt",
        )

        direct = capture_window_when_ready(
            client,
            window_id,
            directory,
            expect_content=frame_outcome in {"success", "picture-fallback"},
        )
        background_rgb = tuple(
            compositor["background_capture"]["image"]["dominant_rgb"]
        )
        if len(background_rgb) != 3:
            raise LabFailure("invalid reference background colour")
        if args.encoding == "h264":
            root_after = capture_grim(
                client,
                directory,
                "root-after",
                compositor["wayland_display"],
            )
            root_crop = crop_composited_window(
                directory,
                "root-after",
                geometry,
                background_rgb,
            )
            with Image.open(directory / "window-composited.png") as source:
                composited_window = source.convert("RGBA")
            composited_window.save(
                directory / "window-focused-screen.rgba.png", format="PNG"
            )
            composited_window.convert("RGB").save(
                directory / "window-focused-screen.rgb.png", format="PNG"
            )
            save_alpha_visualization(
                composited_window,
                directory / "window-focused-screen.alpha.png",
            )
            focused_screen = {
                "image": analyze_image(composited_window),
                "xwd": {"capture": "grim-crop"},
            }
        else:
            capture_xwd(
                client,
                "window-focused-screen.xwd",
                window_id=window_id,
                screen=True,
            )
            capture_xwd(client, "root-after.xwd")
            focused_screen = convert_xwd(directory, "window-focused-screen")
            root_after = convert_xwd(directory, "root-after")
            root_crop = crop_composited_window(
                directory,
                "root-after",
                geometry,
                background_rgb,
            )
        add_background_comparison(
            focused_screen,
            directory / "window-focused-screen.rgba.png",
            background_rgb,
        )
        write_command_output(
            server,
            [
                "xpra",
                "info",
                "wayland-0",
                "--socket-dir=/tmp/server-runtime/xpra-sockets",
            ],
            directory / "server-info.txt",
            check=False,
        )
        xpra_wid = server_xpra_window_id(directory / "server-info.txt", title_patterns)

        if pid_file:
            wait_for(
                "application PID publication",
                lambda: (directory / pid_file).is_file(),
            )
            application_pid = int((directory / pid_file).read_text().strip())
        else:
            executable = (
                "vkcube" if args.application == "vkcube" else "interaction_fixture.py"
            )
            pgrep = podman_exec(
                server,
                ["pgrep", "--oldest", "--full", executable],
            )
            application_pid = int(pgrep.stdout.strip().splitlines()[0])
        application_gpu = process_gpu_evidence(server, application_pid)
        application_activity: dict[str, Any] = {
            "process_alive": container_process_exists(server, application_pid)
        }
        client_graphics: dict[str, Any] = {}
        if args.encoding == "h264":
            client_graphics = process_gpu_evidence(client, client_pid)

        if args.application == "zed":
            if str(args.render_node) not in application_gpu["render_nodes"]:
                raise LabFailure("Zed has no open DRM render-node descriptor")
            if not any(
                "libvulkan_radeon" in mapping
                for mapping in application_gpu["gpu_mappings"]
            ):
                raise LabFailure("Zed did not map the RADV Vulkan driver")

        interaction: dict[str, Any] = {"attempted": False}
        if args.application == "zed" and frame_outcome in {
            "success",
            "picture-fallback",
        }:
            interaction = exercise_zed_mouse(
                client,
                window_id,
                geometry,
                directory,
                direct,
            )
            interaction["attempted"] = True
        elif args.application in {"hardware", "vkcube"}:
            application_activity["vulkan_motion"] = capture_vulkan_motion(
                client, window_id, directory, direct
            )

        interaction_window: tuple[str, str] | None = None
        interaction_xpra_wid: int | None = None
        if args.application == "hardware":

            def interaction_window_ready() -> bool:
                nonlocal interaction_window
                interaction_window = find_window(client, (INTERACTION_READY_TITLE,))
                return interaction_window is not None

            wait_for(
                "forwarded GTK interaction window",
                interaction_window_ready,
            )
            assert interaction_window is not None
            write_command_output(
                server,
                [
                    "xpra",
                    "info",
                    "wayland-0",
                    "--socket-dir=/tmp/server-runtime/xpra-sockets",
                ],
                directory / "server-info-interaction.txt",
                check=False,
            )
            interaction_xpra_wid = server_xpra_window_id(
                directory / "server-info-interaction.txt",
                (INTERACTION_READY_TITLE,),
            )
            interaction = exercise_interaction_fixture(
                server,
                client,
                interaction_window[0],
                directory,
                close_with_keyboard=True,
            )
        elif args.application == "gtk":
            interaction = exercise_interaction_fixture(
                server,
                client,
                window_id,
                directory,
                close_with_keyboard=args.lifecycle == "application-exit",
            )

        lifecycle: dict[str, Any] = {"mode": args.lifecycle}
        if args.lifecycle == "application-exit":
            if args.application in {"hardware", "vkcube"}:
                podman_exec(
                    client,
                    [
                        "env",
                        f"DISPLAY={CLIENT_DISPLAY}",
                        "xdotool",
                        "windowactivate",
                        "--sync",
                        window_id,
                        "key",
                        "Escape",
                    ],
                )
            elif args.application == "zed":
                time.sleep(0.5)
                podman_exec(
                    server, ["kill", "-TERM", str(application_pid)], check=False
                )
            wait_for(
                "Xpra server exit after application termination",
                lambda: not container_process_exists(server, server_pid),
                timeout=15,
            )
            lifecycle.update(
                {
                    "client_exit_status": wait_for_process_exit(
                        client, client_pid, directory, "client"
                    ),
                    "client_exited_after_server": True,
                    "server_exited_after_application": True,
                }
            )
        elif args.lifecycle == "detach":
            detach = podman_exec(
                client,
                [
                    "xpra",
                    "detach",
                    f"tcp://xpra-server:{SERVER_PORT}",
                    "--compressors=none",
                ],
                check=False,
            )
            lifecycle.update(
                {
                    "detach_returncode": detach.returncode,
                    "detach_stderr": detach.stderr,
                    "detach_stdout": detach.stdout,
                    "client_exit_status": wait_for_process_exit(
                        client, client_pid, directory, "client"
                    ),
                    "client_exited_after_detach": True,
                }
            )
            time.sleep(0.5)
            lifecycle.update(
                {
                    "application_survived_detach": container_process_exists(
                        server, application_pid
                    ),
                    "server_survived_detach": container_process_exists(
                        server, server_pid
                    ),
                }
            )
            podman_exec(server, ["kill", "-TERM", str(application_pid)], check=False)
            wait_for(
                "Xpra server exit after detached application termination",
                lambda: not container_process_exists(server, server_pid),
                timeout=15,
            )
            lifecycle["server_exited_after_application"] = True
        else:
            if transport_proxy_pid is None:
                raise LabFailure("transport-loss profile has no owned TCP proxy")
            disconnected = podman_exec(
                client,
                ["kill", "-KILL", str(transport_proxy_pid)],
                check=False,
            )
            lifecycle.update(
                {
                    "transport_proxy_pid": transport_proxy_pid,
                    "transport_disconnect_returncode": disconnected.returncode,
                    "transport_disconnect_stderr": disconnected.stderr,
                    "transport_disconnect_stdout": disconnected.stdout,
                    "client_exit_status": wait_for_process_exit(
                        client, client_pid, directory, "client"
                    ),
                    "client_exited_after_transport_loss": True,
                }
            )
            time.sleep(0.5)
            lifecycle.update(
                {
                    "application_survived_transport_loss": container_process_exists(
                        server, application_pid
                    ),
                    "server_survived_transport_loss": container_process_exists(
                        server, server_pid
                    ),
                }
            )
            podman_exec(server, ["kill", "-TERM", str(application_pid)], check=False)
            wait_for(
                "Xpra server exit after transport-loss application termination",
                lambda: not container_process_exists(server, server_pid),
                timeout=15,
            )
            lifecycle["server_exited_after_application"] = True

        updates = parse_saved_updates(directory, xpra_wid)
        interaction_updates = (
            parse_saved_updates(directory, interaction_xpra_wid)
            if interaction_xpra_wid is not None
            else None
        )
        log_evidence = inspect_logs(directory)
        pixel_evidence, source_image = pixel_pipeline_evidence(
            directory,
            updates["screenshots"],
            directory / "window-direct.rgb.png",
            directory / "window-focused-screen.rgb.png",
            pixel_error_limit(args.application, args.encoding),
        )
        hardware: dict[str, Any] = {
            "application": application_gpu,
            "render_node": str(args.render_node),
        }
        if args.encoding == "h264":
            hardware["client_desktop"] = compositor
            hardware["client_graphics_process"] = client_graphics
            hardware["server_vaapi_h264"] = bool(
                server_vainfo and "VAProfileH264" in server_vainfo.stdout
            )
            h264_hardware = h264_hardware_evidence(
                directory,
                updates,
                allow_lossless_rgb_edges=(args.h264_client_policy == "adaptive-alpha"),
            )
            hardware["h264_encode"] = h264_hardware["server"]
            hardware["h264_decode"] = h264_hardware["client"]
            hardware["h264_packet_chain"] = h264_hardware["packet_chain"]
            hardware["h264_production"] = h264_hardware["production"]
        classification = classify_boundaries(
            args=args,
            application_activity=application_activity,
            application_gpu=application_gpu,
            log_evidence=log_evidence,
            updates=updates,
            direct=direct,
            composited=focused_screen["image"],
            source_image=source_image,
            codec_hardware=hardware,
            interaction=interaction,
            pixel_evidence=pixel_evidence,
            lifecycle=lifecycle,
            interaction_updates=interaction_updates,
        )
        report = {
            "application": args.application,
            "application_activity": application_activity,
            "artifact_sha256": artifact_sha256(directory),
            "classification": classification,
            "client": {
                "alpha_disabled": scenario.disable_alpha,
                "compositor": compositor,
                "os_release": operating_systems["client"],
                "version": versions["client"],
            },
            "encoding": args.encoding,
            "h264_client_policy": args.h264_client_policy,
            "hardware": hardware,
            "interaction": interaction,
            "images": {
                "focused_screen_window": focused_screen,
                "root_crop": root_crop,
                "direct_window": direct,
                "pixel_pipeline": pixel_evidence,
                "root_after": root_after,
                "source": source_image,
            },
            "lifecycle": lifecycle,
            "lifecycle_profile": args.lifecycle,
            "logs": log_evidence,
            "name": scenario.name,
            "result": "completed",
            "server": {
                "os_release": operating_systems["server"],
                "version": versions["server"],
            },
            "updates": updates,
            "window": {
                "frame_outcome": frame_outcome,
                "geometry": geometry,
                "id": window_id,
                "xpra_id": xpra_wid,
                "title": window_title,
            },
        }
        if interaction_window is not None:
            report["interaction_window"] = {
                "id": interaction_window[0],
                "title": interaction_window[1],
            }
            report["interaction_updates"] = interaction_updates
        (directory / "report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(
            f"RESULT {scenario.name}: {classification['appearance']}, "
            f"first boundary={classification['first_failed_boundary']}",
            flush=True,
        )
        return report
    except BaseException as error:
        report["failure"] = str(error)
        (directory / "report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        raise
    finally:
        cleanup: dict[str, Any] = {
            "containers": [],
            "kept": bool(args.keep_containers),
            "network": None,
            "passed": False,
        }
        if args.keep_containers:
            cleanup["containers"] = [
                {"name": container, "status": "kept"} for container in containers
            ]
            cleanup["network"] = {
                "name": network,
                "status": "kept" if network_created else "not-created",
            }
            print(
                f"Kept containers {server}, {client} and network {network}",
                flush=True,
            )
        else:
            for container in reversed(containers):
                cleanup["containers"].append(
                    remove_owned_podman_object(
                        "container", container, container_labels[container]
                    )
                )
            if network_created:
                cleanup["network"] = remove_owned_podman_object(
                    "network", network, network_labels
                )
            else:
                cleanup["network"] = {"name": network, "status": "not-created"}
            cleanup["passed"] = all(
                item["status"] == "removed" for item in cleanup["containers"]
            ) and cleanup["network"]["status"] in {"removed", "not-created"}
        report["artifact_sha256"] = artifact_sha256(directory)
        report["cleanup"] = cleanup
        report["result"] = (
            "passed"
            if cleanup["passed"]
            and report.get("classification", {}).get("first_failed_boundary")
            == "passed"
            else "failed"
        )
        (directory / "report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def main() -> int:
    if PIL.__version__ != EXPECTED_PILLOW_VERSION:
        raise LabFailure(
            f"Pillow {PIL.__version__} is installed; "
            f"the live harness requires {EXPECTED_PILLOW_VERSION}"
        )
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--encoding",
        choices=("rgb", "h264"),
        default="rgb",
        help="strict Xpra transport; RGB is the diagnostic default",
    )
    parser.add_argument(
        "--h264-client-policy",
        choices=H264_CLIENT_POLICIES,
        default="strict",
        help=(
            "strict and adaptive-alpha are H.264 acceptance profiles; "
            "fallback-auto and fallback-h264 are picture-fallback diagnostics"
        ),
    )
    parser.add_argument(
        "--selection",
        metavar="{cases,stacks}/SLUG",
        help="validated case or stack manifest to apply to the server image",
    )
    parser.add_argument(
        "--source-variant",
        choices=tuple(LEGACY_SOURCE_VARIANT_SELECTORS),
        help="deprecated compatibility alias for the original cumulative variants",
    )
    parser.add_argument(
        "--application",
        choices=APPLICATIONS,
        default="zed",
        help="remote application; actual Zed is the primary reproducer",
    )
    parser.add_argument(
        "--lifecycle",
        choices=LIFECYCLES,
        default="application-exit",
        help="Xpra lifecycle boundary exercised after presentation and input",
    )
    parser.add_argument(
        "--alpha-scenarios",
        choices=ALPHA_SCENARIOS,
        default="both",
        help="run the normal client, an XPRA_ALPHA=0 diagnostic control, or both",
    )
    parser.add_argument(
        "--zed-directory",
        type=Path,
        default=DEFAULT_ZED_DIRECTORY,
        help="host Zed application directory mounted read-only into the server",
    )
    parser.add_argument(
        "--render-node",
        type=Path,
        default=DEFAULT_RENDER_NODE,
        help="DRM render node passed to the server and, for H.264, the client",
    )
    parser.add_argument(
        "--libva-driver",
        default="radeonsi",
        help="VA-API driver used by the current AMD host",
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="reuse images tagged for the current source commit",
    )
    parser.add_argument(
        "--keep-containers",
        action="store_true",
        help="keep disposable containers and networks for post-failure inspection",
    )
    parser.add_argument(
        "--state-root",
        type=Path,
        default=DEFAULT_STATE_ROOT,
        help=(
            f"external build-context and artifact root (default: {DEFAULT_STATE_ROOT})"
        ),
    )
    parser.add_argument(
        "--run-id",
        help="validated no-clobber result directory name supplied by a supervisor",
    )
    args = parser.parse_args()
    try:
        validate_profile(
            application=args.application,
            lifecycle=args.lifecycle,
            encoding=args.encoding,
            h264_client_policy=args.h264_client_policy,
            alpha_scenarios=args.alpha_scenarios,
        )
    except ProfileError as error:
        raise LabFailure(str(error)) from error
    transport_encoding_options(args.encoding, args.h264_client_policy, client=True)
    if args.run_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.run_id):
        raise LabFailure(f"invalid run ID: {args.run_id!r}")
    server_selection = resolve_patch_selection(args.selection, args.source_variant)
    client_selection = resolve_patch_selection(None, "master")
    args.selected_case_slugs = server_selection.case_slugs

    if shutil.which("podman") is None:
        raise LabFailure("podman is not available")
    if not args.render_node.is_char_device():
        raise LabFailure(f"render node is unavailable: {args.render_node}")
    if not os.access(args.render_node, os.R_OK | os.W_OK):
        raise LabFailure(
            f"render node is not readable and writable: {args.render_node}"
        )
    if args.application == "zed":
        zed_binary = args.zed_directory / "libexec" / "zed-editor"
        if not zed_binary.is_file() or not os.access(zed_binary, os.X_OK):
            raise LabFailure(f"Zed executable is unavailable: {zed_binary}")
    else:
        zed_binary = None

    if args.state_root.is_symlink():
        raise LabFailure(f"state root must not be a symlink: {args.state_root}")
    state_root = args.state_root.absolute()
    ensure_trusted_parent_directory(state_root.parent)
    ensure_private_directory(state_root, create=True)
    snapshot = create_source_snapshot(state_root)
    commit = snapshot.commit
    server_context = prepare_build_context(
        state_root,
        snapshot,
        server_selection,
    )
    client_context = prepare_build_context(state_root, snapshot, client_selection)
    if args.run_id:
        result_name = args.run_id
    else:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        result_name = f"{timestamp}-{os.getpid()}"
    result_root = state_root / "live-results"
    ensure_private_directory(result_root, create=True)
    result_directory = result_root / result_name
    result_directory.mkdir(mode=0o700, exist_ok=False)
    ensure_private_directory(result_directory)
    input_manifest_sha256 = snapshot_build_inputs(
        result_directory,
        snapshot,
        server_context,
        client_context,
    )
    server_context_digest = server_context.digest
    client_context_digest = client_context.digest
    selection_tag = re.sub(r"[^a-z0-9_.-]+", "-", server_selection.name).strip("-")
    server_suffix = f"{commit[:12]}-{selection_tag}-{server_context_digest[:12]}"
    client_suffix = f"{commit[:12]}-master-{client_context_digest[:12]}"
    server_image = f"localhost/xpra-lab-live-server:{server_suffix}"
    client_image = f"localhost/xpra-lab-live-client:{client_suffix}"
    if not args.skip_build:
        for target, image, context in (
            ("server", server_image, server_context),
            ("client", client_image, client_context),
        ):
            run(
                [
                    "podman",
                    "build",
                    "--target",
                    target,
                    "--build-arg",
                    f"XPRA_COMMIT={commit}",
                    "--build-arg",
                    f"XPRA_SELECTION={context.selection.name}",
                    "--label",
                    "io.xpra.lab.owner=live",
                    "--label",
                    f"io.xpra.lab.role={target}-image",
                    "--label",
                    f"io.xpra.lab.source={commit}",
                    "--label",
                    f"io.xpra.lab.context={context.digest}",
                    "--tag",
                    image,
                    str(context.path),
                ],
                capture=False,
            )
    else:
        run(["podman", "image", "exists", server_image])
        run(["podman", "image", "exists", client_image])
    image_inspections = {
        "server": inspect_lab_image(
            server_image,
            role="server-image",
            source_commit=commit,
            context_digest=server_context_digest,
        ),
        "client": inspect_lab_image(
            client_image,
            role="client-image",
            source_commit=commit,
            context_digest=client_context_digest,
        ),
    }

    scenarios = [
        Scenario(name, disable_alpha)
        for name, disable_alpha in scenario_specs(
            alpha_scenarios=args.alpha_scenarios,
            lifecycle=args.lifecycle,
        )
    ]
    aggregate: dict[str, Any] = {
        "application": args.application,
        "encoding": args.encoding,
        "h264_client_policy": args.h264_client_policy,
        "lifecycle_profile": args.lifecycle,
        "invocation": {
            "alpha_scenarios": args.alpha_scenarios,
            "application": args.application,
            "encoding": args.encoding,
            "h264_client_policy": args.h264_client_policy,
            "job_id": os.environ.get("XPRA_LAB_JOB_ID"),
            "lifecycle": args.lifecycle,
            "libva_driver": args.libva_driver,
            "render_node": str(args.render_node),
            "run_id": result_name,
            "selection": server_selection.name,
            "source_variant_alias": args.source_variant,
            "state_root": str(state_root),
        },
        "images": {
            "client": {
                "build_context_sha256": client_context_digest,
                "id": image_inspections["client"]["id"],
                "labels": image_inspections["client"]["labels"],
                "selection": client_selection.name,
                "tag": client_image,
            },
            "server": {
                "build_context_sha256": server_context_digest,
                "id": image_inspections["server"]["id"],
                "labels": image_inspections["server"]["labels"],
                "selection": server_selection.name,
                "tag": server_image,
            },
        },
        "result": "failed",
        "scenarios": [],
        "source": {
            "analysis_pillow_version": PIL.__version__,
            "analysis_python_version": sys.version.split()[0],
            "archive_sha256": snapshot.archive_sha256,
            "commit": commit,
            "harness_sha256": sha256_file(Path(__file__)),
            "input_manifest_sha256": input_manifest_sha256,
            "patches": {
                path.relative_to(LAB_ROOT).as_posix(): sha256_file(path)
                for path in server_selection.patches
            },
            "patch_series": [
                {
                    "path": path.relative_to(LAB_ROOT).as_posix(),
                    "sha256": sha256_file(path),
                }
                for path in server_selection.patches
            ],
            "selection": {
                "case_slugs": server_selection.case_slugs,
                "digest": server_selection.digest,
                "name": server_selection.name,
                "resolution": server_context.resolution,
                "selector_digests": dict(server_selection.selector_digests),
                "selectors": server_selection.selectors,
            },
            "upstream_master": commit,
            "supervisor_sha256": sha256_file(INFRA_ROOT / "job.py"),
            "background_supervisor_sha256": sha256_file(BACKGROUND_SUPERVISOR),
            "workflow_sha256": snapshot.workflow_sha256,
            "zed_sha256": sha256_file(zed_binary) if zed_binary else None,
        },
    }
    try:
        for scenario in scenarios:
            aggregate["scenarios"].append(
                run_scenario(
                    args,
                    scenario,
                    commit=commit,
                    run_id=result_directory.name,
                    server_context_digest=server_context_digest,
                    client_context_digest=client_context_digest,
                    server_image_id=image_inspections["server"]["id"],
                    client_image_id=image_inspections["client"]["id"],
                    result_directory=result_directory,
                )
            )
        aggregate["scenario_report_sha256"] = {
            scenario.name: sha256_file(result_directory / scenario.name / "report.json")
            for scenario in scenarios
        }
        appearances = {
            report["name"]: report["classification"]["appearance"]
            for report in aggregate["scenarios"]
        }
        passed = all(
            report["classification"]["first_failed_boundary"] == "passed"
            and report["cleanup"]["passed"] is True
            for report in aggregate["scenarios"]
        )
        aggregate["comparison"] = {"appearances": appearances}
        if args.lifecycle == "application-exit":
            aggregate["comparison"]["alpha_changes_empty_window"] = (
                appearances.get("default-alpha") == "transparent-empty"
                and appearances.get("alpha-disabled") == "opaque-empty"
            )
        else:
            aggregate["comparison"]["lifecycle"] = args.lifecycle
        aggregate["result"] = "passed" if passed else "failed"
        (result_directory / "report.json").write_text(
            json.dumps(aggregate, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        outcome = "PASS" if passed else "FAIL"
        print(
            f"{outcome}: diagnostic evidence written to {result_directory}", flush=True
        )
        return 0 if passed else 1
    except BaseException as error:  # noqa: BLE001
        aggregate["failure"] = str(error)
        (result_directory / "report.json").write_text(
            json.dumps(aggregate, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"FAIL: {error}", file=sys.stderr, flush=True)
        print(f"Partial evidence: {result_directory}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
