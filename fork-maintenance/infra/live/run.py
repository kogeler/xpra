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
from pathlib import Path, PurePosixPath
from typing import Any

TOOLS_ROOT = Path(__file__).resolve().parents[2] / "tools"
sys.path.insert(0, str(TOOLS_ROOT))

import container_payload
import live_config
import PIL
import podman_policy
from PIL import Image, ImageChops, ImageStat
from profiles import (
    ALPHA_SCENARIOS,
    APPLICATIONS,
    DEFAULT_NETWORK_PROFILE,
    H264_ACCEPTANCE_POLICIES,
    H264_CLIENT_POLICIES,
    H264_FALLBACK_POLICIES,
    LIFECYCLES,
    NETWORK_PROFILES,
    ProfileError,
    scenario_specs,
    validate_profile,
)
from xwd_to_png import decode_xwd, save_alpha_visualization

INFRA_ROOT = Path(__file__).resolve().parent
MAINTENANCE_ROOT = INFRA_ROOT.parent.parent
MAIN_REPOSITORY_ROOT = MAINTENANCE_ROOT.parent
SOURCE_REPOSITORY = MAIN_REPOSITORY_ROOT
SELECTION_TOOL = INFRA_ROOT.parent / "upstream-tests" / "selection.py"
BACKGROUND_SUPERVISOR = MAINTENANCE_ROOT / "tools" / "background_job.py"
PAYLOAD_HELPER = MAINTENANCE_ROOT / "tools" / "container_payload.py"
PODMAN_POLICY = MAINTENANCE_ROOT / "tools" / "podman_policy.py"
LIVE_CONFIG_MODULE = INFRA_ROOT / "live_config.py"
NETWORK_PROFILES_CONFIG = MAINTENANCE_ROOT / "profiles.yml"
LIVE_CLI_CONFIG = MAINTENANCE_ROOT / "live-cli.yml"
DEFAULT_STATE_ROOT = MAIN_REPOSITORY_ROOT / ".artifacts" / "fork-maintenance"
DEFAULT_ZED_DIRECTORY = Path.home() / ".local" / "zed.app"
DEFAULT_RENDER_NODE = Path("/dev/dri/renderD128")
FORK_REMOTE_URL = "https://github.com/kogeler/xpra.git"
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
INTERACTION_READY_MARKER = "/tmp/xpra-hardware-interaction-ready"
INTERACTION_IDENTITY_ARTIFACT = "interaction.identity.json"
INTERACTION_FIXTURE_SCRIPT = "/opt/xpra-fork-maintenance/interaction_fixture.py"
EMPTY_DAMAGE_PARENT_TITLE = "Xpra Empty Damage Parent"
EMPTY_DAMAGE_CHILD_TITLE = "Xpra Empty Damage Child"
EMPTY_DAMAGE_READY_MARKER = "/tmp/xpra-empty-damage-fixture-ready"
EMPTY_DAMAGE_START_MARKER = "/tmp/xpra-empty-damage-pressure-start"
EMPTY_DAMAGE_PRESSURE_MARKER = "/tmp/xpra-empty-damage-pressure-ready"
EMPTY_DAMAGE_CLICK_MARKER = "/tmp/xpra-empty-damage-child-clicked"
EMPTY_DAMAGE_INPUT_DEADLINE_SECONDS = 3.0
KEYBOARD_FIXTURE_TITLE = "Xpra Wayland Keyboard Fixture"
KEYBOARD_SCENARIO_BASENAME = "live-wayland-keyboard.json"
LEGACY_SOURCE_VARIANT_SELECTORS = {"master": ()}
HARNESS_INPUTS = (
    INFRA_ROOT / ".containerignore",
    INFRA_ROOT / "Containerfile",
    INFRA_ROOT / "empty_damage_fixture.c",
    INFRA_ROOT / "interaction_fixture.py",
    INFRA_ROOT / "job.py",
    LIVE_CONFIG_MODULE,
    INFRA_ROOT / "profiles.py",
    INFRA_ROOT / "requirements.txt",
    INFRA_ROOT / "run.py",
    INFRA_ROOT / "start_hardware_fixture.sh",
    INFRA_ROOT / "start_wayland_keyboard_fixture.sh",
    INFRA_ROOT / "start_zed.sh",
    INFRA_ROOT / "wayland_keyboard_fixture.py",
    INFRA_ROOT / "xkb_xtest_driver.c",
    INFRA_ROOT / "xwd_to_png.py",
    SELECTION_TOOL,
    BACKGROUND_SUPERVISOR,
    PAYLOAD_HELPER,
    PODMAN_POLICY,
    NETWORK_PROFILES_CONFIG,
    LIVE_CLI_CONFIG,
)
BUILD_CONTEXT_INPUTS = (
    INFRA_ROOT / ".containerignore",
    INFRA_ROOT / "Containerfile",
    INFRA_ROOT / "empty_damage_fixture.c",
    INFRA_ROOT / "interaction_fixture.py",
    INFRA_ROOT / "start_hardware_fixture.sh",
    INFRA_ROOT / "start_wayland_keyboard_fixture.sh",
    INFRA_ROOT / "start_zed.sh",
    INFRA_ROOT / "wayland_keyboard_fixture.py",
    INFRA_ROOT / "xkb_xtest_driver.c",
    PAYLOAD_HELPER,
)
CONTAINER_PAYLOAD = "/opt/xpra-fork-maintenance/container_payload.py"
LIVE_CONTAINER_UID = 1001
LIVE_CONTAINER_GID = 1001
FRAME_LOG_CHUNK_BYTES = 256 * 1024
FRAME_LOG_SCAN_BYTES = 64 * 1024 * 1024
FRAME_LOG_TOTAL_BYTES = 8 * 1024 * 1024
H264_MIN_AGGREGATE_PIXEL_PERCENT = 90
H264_MIN_DAMAGE_SPAN_MS = 1000
H264_MIN_FRAME_PIXEL_PERCENT = 99
H264_MIN_MAIN_FRAMES = 10
ZED_THEME_TOGGLE_CYCLES = 8
ZED_THEME_TOGGLE_DELAY = 0.125
MAINTENANCE_LABEL_PREFIX = "io.xpra.fork-maintenance."
CONTAINER_LOG_DELTA_PROBE = r"""
import errno
import json
import os
import pathlib
import stat
import sys

offsets = json.loads(sys.argv[1])
match_limit = int(sys.argv[2])
scan_limit = int(sys.argv[3])
root = pathlib.Path(sys.argv[4])
markers = json.loads(sys.argv[5])
result = {}
for name, offset in offsets.items():
    path = root / name
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            result[name] = {'error': 'unsafe'}
            continue
        if details.st_size < offset:
            result[name] = {'error': 'truncated', 'size': details.st_size}
            continue
        remaining = min(scan_limit, details.st_size - offset)
        os.lseek(descriptor, offset, os.SEEK_SET)
        chunks = []
        while remaining:
            block = os.read(descriptor, min(1024 * 1024, remaining))
            if not block:
                result[name] = {'error': 'truncated', 'size': os.fstat(descriptor).st_size}
                break
            chunks.append(block)
            remaining -= len(block)
        if name in result:
            continue
        current = os.fstat(descriptor)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_dev != details.st_dev
            or current.st_ino != details.st_ino
            or current.st_mode != details.st_mode
        ):
            result[name] = {'error': 'unsafe'}
            continue
        if current.st_size < details.st_size:
            result[name] = {'error': 'truncated', 'size': current.st_size}
            continue
        payload = b''.join(chunks)
        newline = payload.rfind(b'\n')
        if newline < 0:
            if len(payload) >= scan_limit:
                result[name] = {'error': 'line-too-long'}
                continue
            complete = b''
        else:
            complete = payload[:newline + 1]
        encoded_markers = tuple(value.encode() for value in markers[name])
        matched = b''.join(
            line for line in complete.splitlines(keepends=True)
            if any(marker in line for marker in encoded_markers)
        )
        if len(matched) > match_limit:
            result[name] = {'error': 'matched-overflow'}
            continue
        result[name] = {
            'data': matched.decode('utf-8', errors='replace'),
            'next': offset + len(complete),
            'scanned': len(complete),
            'size': details.st_size,
        }
    except OSError as error:
        result[name] = {
            'error': 'unsafe' if error.errno == errno.ELOOP else 'unavailable'
        }
    finally:
        if descriptor >= 0:
            os.close(descriptor)
print(json.dumps(result, sort_keys=True))
"""

FRAME_LOG_MARKERS = {
    "server.stderr": (
        "commit wid ",
        "no compatible rgb format for 'RGBX'!",
        "only: ('BGRX', 'BGRA')",
        "rgb_encode using",
        "do_set_client_properties(",
        "client does not support any csc modes with h264",
        "no common encodings found",
        "no video pipeline options found",
        "failed to create data packet",
        "failed to encode h264 frame",
        "h264 video compression failed",
        "applied Wayland keyboard configuration hash=",
        "get_keycode: pressed=",
        "fake_key(",
        "wlr_seat_keyboard_notify_key(",
    ),
    "client.stdout": (
        "register_window",
        "draw_region(",
        "choose_decoder(",
        "paint_with_video_decoder: new libva",
        "do_video_paint('h264'",
        "record_decode_time(",
        "do_present_fbo(",
        "cairo._do_paint_rgb",
        "draw_widget(",
        "cairo_draw: window size=",
        "sending updated mappings to the server",
    ),
    "client.stderr": (
        "register_window",
        "draw_region(",
        "choose_decoder(",
        "paint_with_video_decoder: new libva",
        "do_video_paint('h264'",
        "record_decode_time(",
        "do_present_fbo(",
        "cairo._do_paint_rgb",
        "draw_widget(",
        "cairo_draw: window size=",
        "sending updated mappings to the server",
    ),
}

SERVER_ARTIFACT_PATTERNS = (
    re.compile(r"server(?:\..+|-va.*)"),
    re.compile(r"screen-updates"),
    re.compile(r"zed\..+"),
    re.compile(r"vkcube\.(?:exit|pid|stderr|stdout)"),
    re.compile(r"opengl\.(?:exit|pid|stderr|stdout)"),
    re.compile(r"interaction\.(?:exit|identity\.json|stderr|stdout)"),
    re.compile(r"keyboard-fixture\.(?:exit|pid|stderr|stdout)"),
    re.compile(r"empty-damage\.(?:exit|pid|stderr|stdout)"),
)
CLIENT_ARTIFACT_PATTERNS = (
    re.compile(r"client(?:\..+|-va.*)"),
    re.compile(r"transport-proxy\..+"),
    re.compile(r"sway(?:\..+|-child\.env)"),
    re.compile(r"xwayland-xdpyinfo\.txt"),
    re.compile(r"(?:xvfb|openbox|picom)\..+"),
    re.compile(r"(?:root|window|interaction)-.+"),
    re.compile(r"empty-damage-.+"),
)


@dataclass(frozen=True)
class HardwareFixtureSpec:
    """Bind one primary graphics API to the shared multi-window fixture."""

    api: str
    command: str
    primary_name: str
    title_patterns: tuple[str, ...]

    @property
    def pid_file(self) -> str:
        return f"{self.primary_name}.pid"


HARDWARE_FIXTURES = {
    "hardware": HardwareFixtureSpec(
        api="vulkan",
        command="/opt/xpra-fork-maintenance/start_hardware_fixture.sh",
        primary_name="vkcube",
        title_patterns=("vkcube",),
    ),
    "opengl": HardwareFixtureSpec(
        api="opengl",
        command="/opt/xpra-fork-maintenance/start_hardware_fixture.sh opengl",
        primary_name="opengl",
        title_patterns=("glmark2",),
    ),
}
MULTIWINDOW_HARDWARE_APPLICATIONS = frozenset(HARDWARE_FIXTURES)


class LabFailure(RuntimeError):
    """Raised when a required diagnostic boundary is unavailable."""


def hardware_fixture_spec(application: str) -> HardwareFixtureSpec:
    try:
        return HARDWARE_FIXTURES[application]
    except KeyError as error:
        raise LabFailure(f"application is not a multi-window hardware fixture: {application}") from error


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


def privatize_regular_tree(root: Path) -> None:
    """Normalize one owned tree to private directories and owner-only files."""
    if root.is_symlink() or not root.is_dir() or root.lstat().st_uid != os.getuid():
        raise LabFailure(f"cannot privatize an unsafe tree: {root}")
    for directory_name, directory_names, file_names in os.walk(
        root,
        followlinks=False,
    ):
        directory = Path(directory_name)
        directory.chmod(0o700)
        for name in tuple(directory_names):
            path = directory / name
            if path.is_symlink():
                directory_names.remove(name)
                continue
            if not path.is_dir():
                raise LabFailure(f"private tree contains an unsafe directory: {path}")
        for name in file_names:
            path = directory / name
            if path.is_symlink():
                continue
            if not path.is_file():
                raise LabFailure(f"private tree contains an unsafe file: {path}")
            mode = path.stat().st_mode
            path.chmod(0o700 if mode & stat.S_IXUSR else 0o600)


def replace_private_json(path: Path, payload: dict[str, Any]) -> None:
    ensure_private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


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
    elif encoding != "h264":
        raise LabFailure(f"unsupported live encoding: {encoding}")
    role = "client" if client else "server"
    try:
        return list(live_config.transport_options(role, encoding, h264_client_policy))
    except live_config.LiveConfigError as error:
        raise LabFailure(str(error)) from error


def static_cli_options(role: str, block: str) -> list[str]:
    """Load one tracked static Xpra option block for command assembly."""
    try:
        return list(live_config.static_cli_options(role, block))
    except live_config.LiveConfigError as error:
        raise LabFailure(str(error)) from error


def command_cli_options(role: str, command: str) -> list[str]:
    """Load one tracked static Xpra subcommand option block."""
    try:
        return list(live_config.command_cli_options(role, command))
    except live_config.LiveConfigError as error:
        raise LabFailure(str(error)) from error


def client_network_options(profile_name: str) -> list[str]:
    """Render one tracked client-only network and quality profile."""
    try:
        return list(live_config.network_profile(profile_name).client_options())
    except live_config.LiveConfigError as error:
        raise LabFailure(str(error)) from error


def live_user_options() -> list[str]:
    return [
        "--userns",
        podman_policy.keep_id_userns(LIVE_CONTAINER_UID, LIVE_CONTAINER_GID),
        "--user",
        f"{LIVE_CONTAINER_UID}:{LIVE_CONTAINER_GID}",
    ]


def scenario_acceptance(report: dict[str, Any], cleanup: dict[str, Any]) -> bool:
    collection = report.get("container_artifact_collection")
    lifecycle = report.get("lifecycle")
    lifecycle_profile = report.get("lifecycle_profile")
    classification = report.get("classification")
    try:
        expected_lifecycle = lifecycle_boundary_checks(
            lifecycle_profile,
            lifecycle,
        )
    except (AttributeError, LabFailure):
        expected_lifecycle = {}
    boundaries = (
        classification.get("boundaries") if isinstance(classification, dict) else None
    )
    lifecycle_accepted = bool(
        expected_lifecycle
        and all(expected_lifecycle.values())
        and isinstance(lifecycle, dict)
        and isinstance(lifecycle_profile, str)
        and lifecycle.get("mode") == lifecycle_profile
        and isinstance(boundaries, dict)
        and boundaries.get("lifecycle") == expected_lifecycle
    )
    return bool(collection) and all(
        isinstance(item, dict) and item.get("status") == "collected"
        for item in collection
    ) and (
        cleanup.get("passed") is True
        and lifecycle_accepted
        and isinstance(classification, dict)
        and classification.get("diagnostic_only") is not True
        and classification.get("first_failed_boundary") == "passed"
    )


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
    archive_sha256: str | None = None


@dataclass(frozen=True)
class BoundInputs:
    client_context: BuildContext
    input_manifest_sha256: str
    input_tree_sha256: str
    server_context: BuildContext
    snapshot: SourceSnapshot
    keyboard_scenario: dict[str, Any] | None
    keyboard_scenario_path: Path | None
    keyboard_scenario_sha256: str | None
    zed_archive: Path | None
    zed_archive_sha256: str | None
    zed_binary_sha256: str | None


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
    try:
        podman_policy.validate_podman_argv(command)
    except podman_policy.PodmanPolicyError as error:
        raise LabFailure(str(error)) from error
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


def stream_build_context(command: list[str], context: Path) -> None:
    """Send one immutable Podman build context through stdin."""
    print(f"+ {format_command(command)} < validated-tar({context})", flush=True)
    entries = tuple(
        container_payload.PayloadEntry(path, PurePosixPath(path.name))
        for path in sorted(context.iterdir(), key=lambda item: os.fsencode(item.name))
    )
    try:
        container_payload.stream_to_process(command, entries)
    except container_payload.PayloadError as error:
        raise LabFailure(str(error)) from error


def stream_bound_build_context(command: list[str], context: BuildContext) -> None:
    """Send the exact context archive frozen and bound by live-start."""
    if context.archive_sha256 is None or sha256_file(context.path) != context.archive_sha256:
        raise LabFailure("frozen live build-context archive changed before use")
    print(f"+ {format_command(command)} < frozen-tar({context.path})", flush=True)
    try:
        container_payload.stream_archive_to_process(
            command,
            context.path,
            expected_sha256=context.archive_sha256,
        )
    except container_payload.PayloadError as error:
        raise LabFailure(str(error)) from error


def _artifact_relative(value: str) -> str:
    try:
        return str(container_payload.archive_path(value))
    except container_payload.PayloadError as error:
        raise LabFailure(str(error)) from error


def container_artifact_exists(container: str, relative: str) -> bool:
    relative = _artifact_relative(relative)
    return (
        podman_exec(
            container,
            ["test", "-e", f"/artifacts/{relative}"],
            check=False,
            announce=False,
        ).returncode
        == 0
    )


def container_artifact_contains(container: str, relative: str, marker: str) -> bool:
    relative = _artifact_relative(relative)
    return (
        podman_exec(
            container,
            ["grep", "--fixed-strings", "--quiet", "--", marker, f"/artifacts/{relative}"],
            check=False,
            announce=False,
        ).returncode
        == 0
    )


def container_artifact_size(container: str, relative: str) -> int:
    """Return an exact remote artifact size without copying its active content."""
    relative = _artifact_relative(relative)
    probe = r"""
import os
import stat
import sys

details = os.lstat(sys.argv[1])
if not stat.S_ISREG(details.st_mode):
    raise SystemExit(2)
print(details.st_size)
"""
    result = podman_exec(
        container,
        ["python3", "-c", probe, f"/artifacts/{relative}"],
        check=False,
        announce=False,
    )
    if result.returncode:
        raise LabFailure(f"container artifact is not a regular file: {relative}")
    value = result.stdout.strip()
    if not re.fullmatch(r"[0-9]+", value):
        raise LabFailure(f"invalid container artifact size for {relative}: {value!r}")
    return int(value)


def container_artifact_suffix_matches(
    container: str,
    relative: str,
    offset: int,
    patterns: tuple[str, ...],
) -> bool:
    relative = _artifact_relative(relative)
    if offset < 0 or not patterns:
        raise LabFailure("invalid container artifact suffix query")
    script = r"""
import os
import re
import stat
import sys

offset = int(sys.argv[2])
limit = int(sys.argv[3])
flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
try:
    descriptor = os.open(sys.argv[1], flags)
    with os.fdopen(descriptor, 'rb') as stream:
        details = os.fstat(stream.fileno())
        if not stat.S_ISREG(details.st_mode) or details.st_size < offset:
            raise SystemExit(3)
        stream.seek(offset)
        payload = stream.read(limit + 1)
except OSError:
    raise SystemExit(3)
if len(payload) > limit:
    raise SystemExit(2)
data = payload.decode('utf-8', errors='replace')
try:
    matched = all(re.search(pattern, data) for pattern in sys.argv[4:])
except re.error:
    raise SystemExit(3)
raise SystemExit(0 if matched else 1)
"""
    result = podman_exec(
        container,
        [
            "python3",
            "-c",
            script,
            f"/artifacts/{relative}",
            str(offset),
            str(FRAME_LOG_TOTAL_BYTES),
            *patterns,
        ],
        check=False,
        announce=False,
    )
    if result.returncode not in {0, 1}:
        raise LabFailure(f"container artifact suffix exceeds its limit: {relative}")
    return result.returncode == 0


def container_artifact_files(
    container: str,
    relative: str,
    name: str,
) -> tuple[str, ...]:
    relative = _artifact_relative(relative)
    if "/" in name or name in {"", ".", ".."}:
        raise LabFailure(f"invalid artifact basename query: {name!r}")
    listing = podman_exec(
        container,
        [
            "find",
            f"/artifacts/{relative}",
            "-type",
            "f",
            "-name",
            name,
            "-printf",
            "%P\\0",
        ],
        announce=False,
    )
    return tuple(
        _artifact_relative(f"{relative}/{value}")
        for value in listing.stdout.split("\0")
        if value
    )


def pull_container_artifacts(
    container: str,
    destination: Path,
    relatives: tuple[str, ...],
) -> None:
    """Receive exact live artifacts through the common validated tar stream."""
    if not relatives:
        return
    ensure_private_directory(destination)
    command = [
        "podman",
        "exec",
        container,
        "python3",
        CONTAINER_PAYLOAD,
        "create",
    ]
    seen: set[str] = set()
    for value in relatives:
        relative = _artifact_relative(value)
        if relative in seen:
            continue
        seen.add(relative)
        command.extend(
            ("--entry-json", json.dumps([f"/artifacts/{relative}", relative]))
        )
    try:
        container_payload.merge_from_process(command, destination)
    except container_payload.PayloadError as error:
        raise LabFailure(str(error)) from error


def read_container_log_deltas(
    container: str,
    offsets: dict[str, int],
    *,
    markers: dict[str, tuple[str, ...]] | None = None,
) -> dict[str, tuple[int, str]]:
    """Scan one stable log snapshot and return only bounded evidence lines."""
    if not offsets or any(
        _artifact_relative(name) != name
        or "/" in name
        or not isinstance(offset, int)
        or offset < 0
        for name, offset in offsets.items()
    ):
        raise LabFailure("invalid incremental container-log request")
    selected_markers = markers or {
        name: FRAME_LOG_MARKERS[name]
        for name in offsets
        if name in FRAME_LOG_MARKERS
    }
    if (
        set(selected_markers) != set(offsets)
        or any(
            not isinstance(values, tuple)
            or not values
            or any(
                not isinstance(value, str) or not value or "\n" in value
                for value in values
            )
            for values in selected_markers.values()
        )
    ):
        raise LabFailure("invalid incremental container-log markers")
    response = podman_exec(
        container,
        [
            "python3",
            "-c",
            CONTAINER_LOG_DELTA_PROBE,
            json.dumps(offsets, sort_keys=True),
            str(FRAME_LOG_CHUNK_BYTES),
            str(FRAME_LOG_SCAN_BYTES),
            "/artifacts",
            json.dumps(selected_markers, sort_keys=True),
        ],
        announce=False,
    )
    try:
        payload = json.loads(response.stdout)
    except json.JSONDecodeError as error:
        raise LabFailure("incremental container-log probe returned invalid JSON") from error
    if not isinstance(payload, dict) or set(payload) != set(offsets):
        raise LabFailure("incremental container-log probe returned invalid fields")
    result: dict[str, tuple[int, str]] = {}
    for name, old_offset in offsets.items():
        item = payload[name]
        if not isinstance(item, dict):
            raise LabFailure(f"incremental container-log probe is invalid: {name}")
        error = item.get("error")
        if error == "truncated":
            raise LabFailure(f"container log was truncated during frame polling: {name}")
        if error in {"unavailable", "unsafe"}:
            raise LabFailure(f"container log is {error} during frame polling: {name}")
        if error in {"line-too-long", "matched-overflow"}:
            raise LabFailure(f"container log evidence exceeds its limit: {name}")
        if error is not None:
            raise LabFailure(f"incremental container-log probe is invalid: {name}")
        data = item.get("data")
        next_offset = item.get("next")
        scanned = item.get("scanned")
        size = item.get("size")
        if (
            not isinstance(data, str)
            or not isinstance(next_offset, int)
            or isinstance(next_offset, bool)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or not isinstance(scanned, int)
            or isinstance(scanned, bool)
            or next_offset < old_offset
            or scanned != next_offset - old_offset
            or scanned > FRAME_LOG_SCAN_BYTES
            or len(data.encode()) > FRAME_LOG_CHUNK_BYTES
            or size < next_offset
        ):
            raise LabFailure(f"incremental container-log probe is inconsistent: {name}")
        result[name] = next_offset, data
    return result


def pull_all_container_artifacts(
    container: str,
    destination: Path,
    role: str,
) -> None:
    listing = podman_exec(
        container,
        [
            "find",
            "/artifacts",
            "-mindepth",
            "1",
            "-maxdepth",
            "1",
            "-printf",
            "%f\\0",
        ],
        announce=False,
    )
    relatives = tuple(name for name in listing.stdout.split("\0") if name)
    patterns = {
        "server": SERVER_ARTIFACT_PATTERNS,
        "client": CLIENT_ARTIFACT_PATTERNS,
    }.get(role)
    if patterns is None:
        raise LabFailure(f"invalid live artifact role: {role}")
    other_patterns = (
        CLIENT_ARTIFACT_PATTERNS if role == "server" else SERVER_ARTIFACT_PATTERNS
    )
    ambiguous = [
        relative
        for relative in relatives
        if any(pattern.fullmatch(relative) for pattern in patterns)
        and any(pattern.fullmatch(relative) for pattern in other_patterns)
    ]
    if ambiguous:
        raise LabFailure(
            f"ambiguous {role} artifact names: {', '.join(sorted(ambiguous))}"
        )
    unexpected = [
        relative
        for relative in relatives
        if not any(pattern.fullmatch(relative) for pattern in patterns)
    ]
    if unexpected:
        raise LabFailure(
            f"unexpected {role} artifact names: {', '.join(sorted(unexpected))}"
        )
    pull_container_artifacts(container, destination, relatives)


def wait_for_container_artifact(
    container: str,
    directory: Path,
    relative: str,
    description: str,
) -> Path:
    wait_for(description, lambda: container_artifact_exists(container, relative))
    pull_container_artifacts(container, directory, (relative,))
    return directory / _artifact_relative(relative)


def send_zed_payload(
    container: str,
    zed_archive: Path,
    expected_sha256: str,
) -> None:
    """Populate the optional application input from one frozen archive."""
    if sha256_file(zed_archive) != expected_sha256:
        raise LabFailure("frozen Zed payload digest changed before transfer")
    try:
        container_payload.stream_archive_to_process(
            [
                "podman",
                "exec",
                "--interactive",
                container,
                "python3",
                CONTAINER_PAYLOAD,
                "extract",
                "--destination",
                "/home/lab/live-input",
            ],
            zed_archive,
            expected_sha256=expected_sha256,
        )
    except container_payload.PayloadError as error:
        raise LabFailure(str(error)) from error


def inspect_maintenance_image(
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
        raise LabFailure(f"image has no fork-maintenance provenance labels: {image}")
    if not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in labels.items()
    ):
        raise LabFailure(f"image has invalid provenance labels: {image}")
    expected = {
        "io.xpra.fork-maintenance.context": context_digest,
        "io.xpra.fork-maintenance.owner": "live",
        "io.xpra.fork-maintenance.role": role,
        "io.xpra.fork-maintenance.source": source_commit,
    }
    maintenance_labels = {
        key: value for key, value in labels.items() if key.startswith(MAINTENANCE_LABEL_PREFIX)
    }
    if maintenance_labels != expected:
        raise LabFailure(
            f"image provenance labels do not match the frozen inputs for {image}: "
            f"expected {json.dumps(expected, sort_keys=True)}, "
            f"observed {json.dumps(labels, sort_keys=True)}"
        )
    image_id = inspection.get("Id")
    if not isinstance(image_id, str) or not re.fullmatch(
        r"(?:sha256:)?[0-9a-f]{64}", image_id
    ):
        raise LabFailure(f"image has no immutable identifier: {image}")
    return {"id": image_id, "labels": maintenance_labels}


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


def inspect_podman_object(kind: str, name: str) -> tuple[str, dict[str, str]]:
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
        object_id = str(inspection.get("Id", ""))
        config = inspection.get("Config")
        labels = config.get("Labels") if isinstance(config, dict) else None
    else:
        object_id = str(inspection.get("id", inspection.get("ID", "")))
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
    if not re.fullmatch(r"[0-9a-f]{64}", object_id):
        raise LabFailure(f"{kind} has no immutable ID: {name}")
    return object_id, labels


def inspect_podman_object_labels(kind: str, name: str) -> dict[str, str]:
    return inspect_podman_object(kind, name)[1]


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
    expected_id: str | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "expected_labels": expected_labels,
        "name": name,
    }
    try:
        object_id, labels = inspect_podman_object(kind, name)
    except LabFailure as error:
        entry.update({"error": str(error), "status": "inspect-failed"})
        return entry
    observed = {key: labels.get(key) for key in expected_labels}
    entry["observed_labels"] = observed
    entry["observed_id"] = object_id
    if expected_id is not None and object_id != expected_id:
        entry["status"] = "identity-mismatch"
        return entry
    if observed != expected_labels:
        entry["status"] = "ownership-mismatch"
        return entry
    command = ["podman", "rm", "--force", object_id]
    if kind == "network":
        command = ["podman", "network", "rm", object_id]
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


def valid_process_identity(value: Any) -> bool:
    """Validate one bounded procfs process identity."""
    if not isinstance(value, dict) or set(value) != {
        "argv",
        "cmdline_sha256",
        "pid",
        "schema",
        "start_ticks",
    }:
        return False
    pid = value.get("pid")
    argv = value.get("argv")
    cmdline_sha256 = value.get("cmdline_sha256")
    if not (
        isinstance(argv, list)
        and 1 <= len(argv) <= 256
        and all(isinstance(argument, str) and "\0" not in argument for argument in argv)
    ):
        return False
    try:
        cmdline = b"\0".join(os.fsencode(argument) for argument in argv) + b"\0"
    except UnicodeEncodeError:
        return False
    if len(cmdline) > 1024 * 1024:
        return False
    expected_cmdline_sha256 = hashlib.sha256(cmdline).hexdigest()
    return bool(
        type(value.get("schema")) is int
        and value["schema"] == 1
        and isinstance(pid, int)
        and not isinstance(pid, bool)
        and 0 < pid <= 2**31 - 1
        and isinstance(value.get("start_ticks"), str)
        and len(value["start_ticks"]) <= 32
        and re.fullmatch(r"[1-9][0-9]*", value["start_ticks"])
        and isinstance(cmdline_sha256, str)
        and re.fullmatch(r"[0-9a-f]{64}", cmdline_sha256)
        and cmdline_sha256 == expected_cmdline_sha256
    )


def valid_interaction_fixture_identity(value: Any) -> bool:
    """Validate the complete identity published by the GTK fixture itself."""
    argv = value.get("argv") if isinstance(value, dict) else None
    return bool(
        valid_process_identity(value)
        and len(argv) == 2
        and re.fullmatch(r"python3(?:\.[0-9]+)?", PurePosixPath(argv[0]).name)
        and argv[1] == INTERACTION_FIXTURE_SCRIPT
    )


def load_interaction_fixture_identity(path: Path) -> dict[str, Any]:
    """Load one bounded, private fixture-owned process identity artifact."""
    ensure_private_regular_file(path)
    raw = path.read_bytes()
    if not raw or len(raw) > 4096 or b"\0" in raw:
        raise LabFailure("interaction fixture identity has an invalid size")
    try:
        payload = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_json_object_without_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LabFailure("interaction fixture identity is not valid JSON") from error
    if not valid_interaction_fixture_identity(payload):
        raise LabFailure("interaction fixture identity has invalid fields")
    return payload


def container_process_identity(container: str, pid: int) -> dict[str, Any] | None:
    """Read one live process identity from procfs in its container namespace."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise LabFailure(f"invalid container process PID: {pid!r}")
    probe = r"""
import hashlib
import json
import os
import pathlib
import select
import sys
import time

pid = int(sys.argv[1])
root = pathlib.Path('/proc') / str(pid)
try:
    descriptor = os.pidfd_open(pid)
except ProcessLookupError:
    raise SystemExit(3)
try:
    poller = select.poll()
    poller.register(descriptor, select.POLLIN)
    for attempt in range(5):
        if poller.poll(0):
            raise SystemExit(3)
        try:
            stat_before = (root / 'stat').read_text(encoding='ascii')
            cmdline = (root / 'cmdline').read_bytes()
            stat_after = (root / 'stat').read_text(encoding='ascii')
        except FileNotFoundError:
            if poller.poll(0):
                raise SystemExit(3)
            if attempt < 4:
                time.sleep(0.01)
                continue
            raise SystemExit(4)
        end_before = stat_before.rfind(')')
        end_after = stat_after.rfind(')')
        before = stat_before[end_before + 2:].split() if end_before >= 0 else []
        after = stat_after[end_after + 2:].split() if end_after >= 0 else []
        if (
            poller.poll(0)
            or (before and before[0].casefold() in {'x', 'z'})
            or (after and after[0].casefold() in {'x', 'z'})
        ):
            raise SystemExit(3)
        if len(before) < 20 or len(after) < 20 or len(cmdline) > 1024 * 1024:
            raise SystemExit(4)
        if before[19] != after[19]:
            raise SystemExit(4)
        argv = [os.fsdecode(value) for value in cmdline.split(b'\0') if value]
        if not cmdline or not argv:
            if attempt < 4:
                time.sleep(0.01)
                continue
            if poller.poll(0):
                raise SystemExit(3)
            raise SystemExit(4)
        print(json.dumps({
            'argv': argv,
            'cmdline_sha256': hashlib.sha256(cmdline).hexdigest(),
            'pid': pid,
            'schema': 1,
            'start_ticks': before[19],
        }, sort_keys=True))
        raise SystemExit(0)
    raise SystemExit(4)
finally:
    os.close(descriptor)
"""
    result = podman_exec(
        container,
        ["python3", "-c", probe, str(pid)],
        check=False,
        announce=False,
    )
    if result.returncode == 3:
        return None
    if result.returncode:
        raise LabFailure("could not read the container process identity")
    try:
        payload = json.loads(
            result.stdout,
            object_pairs_hook=_json_object_without_duplicates,
        )
    except json.JSONDecodeError as error:
        raise LabFailure("container process identity probe returned invalid JSON") from error
    if not valid_process_identity(payload):
        raise LabFailure("container process identity has invalid fields")
    return payload


def require_interaction_fixture_identity(
    container: str,
    expected: dict[str, Any],
    *,
    server_pid: int,
) -> dict[str, Any]:
    """Require the same live fixture process and forbid the Xpra server PID."""
    if not valid_interaction_fixture_identity(expected):
        raise LabFailure("expected interaction fixture identity is invalid")
    if expected["pid"] == server_pid:
        raise LabFailure("interaction fixture identity aliases the Xpra server")
    observed = require_process_identity(
        container,
        expected,
        role="interaction fixture",
    )
    return observed


def require_process_identity(
    container: str,
    expected: dict[str, Any],
    *,
    role: str,
) -> dict[str, Any]:
    """Require one unchanged, non-zombie process identity."""
    if not valid_process_identity(expected):
        raise LabFailure(f"expected {role} process identity is invalid")
    observed = container_process_identity(container, expected["pid"])
    if observed is None:
        raise LabFailure(f"{role} process is not running")
    if observed != expected:
        raise LabFailure(f"{role} process identity changed")
    return observed


def interaction_fixture_identity_is_gone(
    container: str,
    expected: dict[str, Any],
) -> bool:
    """Return true only when the published process is gone, not when PID was reused."""
    return process_identity_is_gone(
        container,
        expected,
        role="interaction fixture",
    )


def process_identity_is_gone(
    container: str,
    expected: dict[str, Any],
    *,
    role: str,
) -> bool:
    """Observe exact process exit while failing closed on PID reuse."""
    if not valid_process_identity(expected):
        raise LabFailure(f"expected {role} process identity is invalid")
    observed = container_process_identity(container, expected["pid"])
    if observed is None:
        return True
    if observed != expected:
        raise LabFailure(f"{role} PID was reused before exit observation")
    return False


def terminate_interaction_fixture(
    container: str,
    expected: dict[str, Any],
    *,
    server_identity: dict[str, Any],
) -> dict[str, Any]:
    """Signal only the exact published fixture through a verified Linux pidfd."""
    if not valid_process_identity(server_identity):
        raise LabFailure("expected Xpra server identity is invalid")
    identity = require_interaction_fixture_identity(
        container,
        expected,
        server_pid=server_identity["pid"],
    )
    probe = r"""
import hashlib
import json
import os
import pathlib
import select
import signal
import sys

pid = int(sys.argv[1])
expected_start = sys.argv[2]
expected_hash = sys.argv[3]
expected_argv = json.loads(sys.argv[4])
server_pid = int(sys.argv[5])
server_start = sys.argv[6]
server_hash = sys.argv[7]
server_argv = json.loads(sys.argv[8])

def snapshot(process_pid):
    root = pathlib.Path('/proc') / str(process_pid)
    stat_before = (root / 'stat').read_text(encoding='ascii')
    cmdline = (root / 'cmdline').read_bytes()
    stat_after = (root / 'stat').read_text(encoding='ascii')
    end_before = stat_before.rfind(')')
    end_after = stat_after.rfind(')')
    before = stat_before[end_before + 2:].split() if end_before >= 0 else []
    after = stat_after[end_after + 2:].split() if end_after >= 0 else []
    argv = [os.fsdecode(value) for value in cmdline.split(b'\0') if value]
    if (
        len(before) < 20
        or len(after) < 20
        or before[0].casefold() in {'x', 'z'}
        or after[0].casefold() in {'x', 'z'}
        or before[19] != after[19]
        or not cmdline
        or not argv
        or len(cmdline) > 1024 * 1024
    ):
        raise SystemExit(4)
    return before[19], hashlib.sha256(cmdline).hexdigest(), argv

try:
    descriptor = os.pidfd_open(pid)
except ProcessLookupError:
    raise SystemExit(3)
try:
    try:
        server_descriptor = os.pidfd_open(server_pid)
    except ProcessLookupError:
        raise SystemExit(5)
    try:
        server_poller = select.poll()
        server_poller.register(server_descriptor, select.POLLIN)
        if server_poller.poll(0):
            raise SystemExit(5)
        try:
            observed_server = snapshot(server_pid)
        except (FileNotFoundError, ProcessLookupError):
            raise SystemExit(5)
        if observed_server != (server_start, server_hash, server_argv):
            raise SystemExit(5)
        try:
            observed_fixture = snapshot(pid)
        except (FileNotFoundError, ProcessLookupError):
            raise SystemExit(3)
        if observed_fixture != (expected_start, expected_hash, expected_argv):
            raise SystemExit(4)
        if server_poller.poll(0):
            raise SystemExit(5)
        signal.pidfd_send_signal(descriptor, signal.SIGTERM)
    finally:
        os.close(server_descriptor)
finally:
    os.close(descriptor)
"""
    result = podman_exec(
        container,
        [
            "python3",
            "-c",
            probe,
            str(identity["pid"]),
            identity["start_ticks"],
            identity["cmdline_sha256"],
            json.dumps(identity["argv"]),
            str(server_identity["pid"]),
            server_identity["start_ticks"],
            server_identity["cmdline_sha256"],
            json.dumps(server_identity["argv"]),
        ],
        check=False,
        announce=False,
    )
    if result.returncode:
        raise LabFailure(
            "could not terminate the exact interaction fixture while the "
            f"Xpra server was live (status {result.returncode})"
        )
    return {
        "identity": identity,
        "pidfd": True,
        "server_identity": server_identity,
        "server_pidfd": True,
        "signal": "SIGTERM",
        "returncode": result.returncode,
    }


def quiesce_failed_workloads(
    processes: tuple[tuple[str, str, int], ...],
    *,
    timeout: float = 15,
) -> dict[str, Any]:
    """Stop exact scenario processes before collecting failure evidence."""
    evidence: list[dict[str, Any]] = []
    active: list[tuple[str, str, int]] = []
    for role, container, pid in processes:
        item: dict[str, Any] = {"container": container, "pid": pid, "role": role}
        if pid <= 0:
            item["status"] = "not-started"
            evidence.append(item)
            continue
        try:
            alive = container_process_exists(container, pid)
        except BaseException as error:  # noqa: BLE001
            item.update({"error": str(error), "status": "probe-failed"})
            evidence.append(item)
            continue
        if not alive:
            item["status"] = "already-exited"
            evidence.append(item)
            continue
        termination = podman_exec(
            container,
            ["kill", "-TERM", str(pid)],
            check=False,
            announce=False,
        )
        item.update(
            {
                "termination_returncode": termination.returncode,
                "termination_stderr": termination.stderr,
                "termination_stdout": termination.stdout,
                "status": "termination-requested",
            }
        )
        evidence.append(item)
        active.append((role, container, pid))

    deadline = time.monotonic() + timeout
    remaining = list(active)
    while remaining and time.monotonic() < deadline:
        remaining = [
            value
            for value in remaining
            if container_process_exists(value[1], value[2])
        ]
        if remaining:
            time.sleep(0.1)
    remaining_keys = {(container, pid) for _role, container, pid in remaining}
    for item in evidence:
        key = item["container"], item["pid"]
        if item["status"] == "termination-requested":
            item["status"] = "still-running" if key in remaining_keys else "exited"
    passed = not remaining and all(
        item["status"] in {"already-exited", "exited", "not-started"}
        for item in evidence
    )
    return {"passed": passed, "processes": evidence}


def wait_for_log(
    container: str,
    pid: int,
    path: Path,
    marker: str,
    description: str,
) -> None:
    relative = path.name

    def ready() -> bool:
        if container_artifact_contains(container, relative, marker):
            return True
        if not container_process_exists(container, pid):
            tail = ""
            if container_artifact_exists(container, relative):
                pull_container_artifacts(container, path.parent, (relative,))
                tail = "\n".join(
                    path.read_text(encoding="utf-8", errors="replace").splitlines()[
                        -80:
                    ]
                )
            raise LabFailure(f"process exited before {description}:\n{tail}")
        return False

    wait_for(description, ready)


def fail_with_exited_server_artifacts(
    server: str,
    directory: Path,
    message: str,
) -> None:
    """Collect complete quiesced server evidence, then raise one diagnostic."""
    pull_all_container_artifacts(server, directory, "server")
    sections: list[str] = []
    for name in (
        "server.stderr",
        "interaction.stderr",
        "interaction.exit",
        "vkcube.stderr",
        "vkcube.exit",
        "opengl.stderr",
        "opengl.stdout",
        "opengl.exit",
    ):
        path = directory / name
        if not path.is_file():
            continue
        tail = "\n".join(
            path.read_text(encoding="utf-8", errors="replace").splitlines()[-80:]
        )
        sections.append(f"[{name}]\n{tail}")
    detail = "\n".join(sections)
    suffix = f"\n{detail}" if detail else ""
    raise LabFailure(f"{message}{suffix}")


def wait_for_server_tcp_endpoint(
    server: str,
    server_pid: int,
    client: str,
    host: str,
    port: int,
    server_log: Path,
) -> None:
    """Wait until the Xpra TCP endpoint is reachable from the client container."""
    if not host or port < 1 or port > 65535:
        raise LabFailure("invalid server TCP endpoint")
    probe = r"""
import socket
import sys

try:
    connection = socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=0.5)
except OSError:
    raise SystemExit(75)
connection.close()
"""

    def server_exited() -> None:
        fail_with_exited_server_artifacts(
            server,
            server_log.parent,
            "Xpra server exited before its TCP endpoint was ready",
        )

    def reachable() -> bool:
        if not container_process_exists(server, server_pid):
            server_exited()
        result = podman_exec(
            client,
            ["python3", "-c", probe, host, str(port)],
            check=False,
            announce=False,
        )
        if not container_process_exists(server, server_pid):
            server_exited()
        if result.returncode == 0:
            return True
        if result.returncode == 75:
            return False
        detail = result.stderr.strip()
        suffix = f": {detail[-2000:]}" if detail else ""
        raise LabFailure(f"client TCP readiness probe failed{suffix}")

    wait_for("Xpra server TCP endpoint from the client container", reachable)


def wait_for_hardware_fixture(
    server: str,
    server_pid: int,
    directory: Path,
    application: str,
) -> None:
    """Require the selected graphics child and shared GTK auxiliary readiness."""
    fixture = hardware_fixture_spec(application)
    probe = r"""
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

def child_state(name):
    path = Path('/artifacts') / f'{name}.pid'
    try:
        details = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(details.st_mode):
        return False
    payload = path.read_bytes()
    if len(payload) > 32 or not re.fullmatch(rb'[1-9][0-9]*\n?', payload):
        return False
    try:
        os.kill(int(payload), 0)
    except OSError:
        return False
    return True


def interaction_state(expected_script):
    path = Path('/artifacts/interaction.identity.json')
    try:
        details = path.lstat()
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.getuid()
        or details.st_nlink != 1
        or stat.S_IMODE(details.st_mode) & 0o077
    ):
        return False
    try:
        raw = path.read_bytes()
        identity = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if (
        not raw
        or len(raw) > 4096
        or not isinstance(identity, dict)
        or set(identity) != {'argv', 'cmdline_sha256', 'pid', 'schema', 'start_ticks'}
        or type(identity.get('schema')) is not int
        or identity.get('schema') != 1
        or not isinstance(identity.get('pid'), int)
        or isinstance(identity.get('pid'), bool)
        or not 0 < identity['pid'] <= 2**31 - 1
        or not isinstance(identity.get('start_ticks'), str)
        or re.fullmatch(r'[1-9][0-9]{0,31}', identity['start_ticks']) is None
        or not isinstance(identity.get('cmdline_sha256'), str)
        or re.fullmatch(r'[0-9a-f]{64}', identity['cmdline_sha256']) is None
        or identity.get('argv') != ['python3', expected_script]
    ):
        return False
    root = Path('/proc') / str(identity['pid'])
    try:
        stat_before = (root / 'stat').read_text(encoding='ascii')
        cmdline = (root / 'cmdline').read_bytes()
        stat_after = (root / 'stat').read_text(encoding='ascii')
    except (FileNotFoundError, ProcessLookupError):
        return False
    end_before = stat_before.rfind(')')
    end_after = stat_after.rfind(')')
    before = stat_before[end_before + 2:].split() if end_before >= 0 else []
    after = stat_after[end_after + 2:].split() if end_after >= 0 else []
    argv = [os.fsdecode(value) for value in cmdline.split(b'\0') if value]
    return bool(
        len(before) >= 20
        and len(after) >= 20
        and before[0].casefold() not in {'x', 'z'}
        and after[0].casefold() not in {'x', 'z'}
        and before[19] == after[19] == identity['start_ticks']
        and argv == identity['argv']
        and hashlib.sha256(cmdline).hexdigest() == identity['cmdline_sha256']
    )


primary_name = sys.argv[1]
expected_script = sys.argv[2]
states = (interaction_state(expected_script), child_state(primary_name))
if False in states:
    raise SystemExit(76)
markers = [Path('/tmp/xpra-hardware-interaction-ready')]
marker_states = []
for marker in markers:
    try:
        marker_details = marker.lstat()
    except FileNotFoundError:
        marker_states.append(False)
        continue
    if not stat.S_ISREG(marker_details.st_mode):
        raise SystemExit(76)
    marker_states.append(True)
if all(state is True for state in states) and all(marker_states):
    raise SystemExit(0)
raise SystemExit(75)
"""

    def server_exited(message: str) -> None:
        fail_with_exited_server_artifacts(server, directory, message)

    def stop_failed_fixture() -> None:
        podman_exec(
            server,
            ["kill", "-TERM", str(server_pid)],
            check=False,
            announce=False,
        )
        wait_for(
            f"Xpra server exit after {fixture.api} fixture readiness failure",
            lambda: not container_process_exists(server, server_pid),
            timeout=15,
        )
        server_exited(f"{fixture.api} fixture child exited before GTK readiness")

    def ready() -> bool:
        if not container_process_exists(server, server_pid):
            server_exited(f"Xpra server exited before {fixture.api} fixture readiness")
        result = podman_exec(
            server,
            [
                "python3",
                "-c",
                probe,
                fixture.primary_name,
                INTERACTION_FIXTURE_SCRIPT,
            ],
            check=False,
            announce=False,
        )
        if not container_process_exists(server, server_pid):
            server_exited(f"Xpra server exited before {fixture.api} fixture readiness")
        if result.returncode == 0:
            return True
        if result.returncode == 75:
            return False
        if result.returncode == 76:
            stop_failed_fixture()
        detail = result.stderr.strip()
        suffix = f": {detail[-2000:]}" if detail else ""
        raise LabFailure(f"{fixture.api} fixture readiness probe failed{suffix}")

    wait_for(f"hardware fixture GTK and {fixture.api} readiness", ready)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _json_object_without_duplicates(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise LabFailure(f"JSON object repeats field: {key}")
        value[key] = item
    return value


def load_keyboard_scenario(path: Path) -> dict[str, Any]:
    """Load one bounded, versioned, case-owned keyboard scenario."""
    if path.is_symlink() or not path.is_file():
        raise LabFailure(f"keyboard scenario is unavailable: {path}")
    raw = path.read_bytes()
    if not raw or len(raw) > 64 * 1024 or b"\0" in raw:
        raise LabFailure("keyboard scenario has an invalid size")
    try:
        text_value = raw.decode("utf-8", errors="strict")
        payload = json.loads(
            text_value,
            object_pairs_hook=_json_object_without_duplicates,
        )
    except UnicodeDecodeError as error:
        raise LabFailure("keyboard scenario is not UTF-8") from error
    except json.JSONDecodeError as error:
        raise LabFailure("keyboard scenario is not valid JSON") from error
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema", "name", "phases"}
        or _exact_int(payload.get("schema")) != 1
        or not isinstance(payload.get("name"), str)
        or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", payload["name"]) is None
    ):
        raise LabFailure("keyboard scenario header is invalid")
    phases = payload.get("phases")
    if not isinstance(phases, list) or len(phases) != 2:
        raise LabFailure("keyboard scenario must define two phases")
    phase_names: set[str] = set()
    keycodes: set[int] = set()
    configurations: list[str] = []
    models: list[str] = []
    output_vectors: list[tuple[str, ...]] = []
    for phase in phases:
        if not isinstance(phase, dict) or set(phase) != {
            "name",
            "rmlvo",
            "physical_keycode",
            "inputs",
        }:
            raise LabFailure("keyboard scenario phase is invalid")
        name = phase.get("name")
        if (
            not isinstance(name, str)
            or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) is None
            or name in phase_names
        ):
            raise LabFailure("keyboard scenario phase name is invalid")
        phase_names.add(name)
        keycode = phase.get("physical_keycode")
        if (
            not isinstance(keycode, int)
            or isinstance(keycode, bool)
            or keycode < 8
            or keycode > 255
        ):
            raise LabFailure("keyboard scenario physical keycode is invalid")
        keycodes.add(keycode)
        rmlvo = phase.get("rmlvo")
        if not isinstance(rmlvo, dict) or set(rmlvo) != {
            "rules",
            "model",
            "layouts",
            "variants",
            "options",
        }:
            raise LabFailure("keyboard scenario RMLVO fields are invalid")
        for field in ("rules", "model"):
            value = rmlvo.get(field)
            if (
                not isinstance(value, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value) is None
            ):
                raise LabFailure(f"keyboard scenario {field} is invalid")
        layouts = rmlvo.get("layouts")
        variants = rmlvo.get("variants")
        if (
            not isinstance(layouts, list)
            or not 3 <= len(layouts) <= 4
            or any(
                not isinstance(value, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value) is None
                for value in layouts
            )
            or not isinstance(variants, list)
            or len(variants) != len(layouts)
            or any(
                not isinstance(value, str)
                or value
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value) is None
                for value in variants
            )
        ):
            raise LabFailure("keyboard scenario layouts or variants are invalid")
        options = rmlvo.get("options")
        if (
            not isinstance(options, str)
            or len(options.encode("utf-8")) > 1024
            or options
            and any(
                re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_:-]{0,127}", value) is None
                for value in options.split(",")
            )
        ):
            raise LabFailure("keyboard scenario options are invalid")
        inputs = phase.get("inputs")
        if not isinstance(inputs, list) or len(inputs) != len(layouts):
            raise LabFailure("keyboard scenario inputs are not group-aligned")
        groups: list[int] = []
        for item in inputs:
            if not isinstance(item, dict) or set(item) != {"group", "expected_text"}:
                raise LabFailure("keyboard scenario input is invalid")
            group = item.get("group")
            expected = item.get("expected_text")
            if (
                not isinstance(group, int)
                or isinstance(group, bool)
                or not isinstance(expected, str)
                or len(expected) != 1
                or expected.isspace()
                or ord(expected) < 32
                or 0xD800 <= ord(expected) <= 0xDFFF
            ):
                raise LabFailure("keyboard scenario group or expected text is invalid")
            expected.encode("utf-8", errors="strict")
            groups.append(group)
        if groups != list(range(len(layouts))):
            raise LabFailure("keyboard scenario must exercise every group in order")
        output_vectors.append(tuple(item["expected_text"] for item in inputs))
        models.append(rmlvo["model"])
        configurations.append(
            json.dumps(rmlvo, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        )
    if len(keycodes) != 1:
        raise LabFailure("keyboard scenario must keep one physical keycode")
    if len(set(configurations)) != len(configurations):
        raise LabFailure("keyboard scenario replacement configuration is unchanged")
    if len(set(models)) != len(models):
        raise LabFailure("keyboard scenario phases must use distinct models")
    if len(set(output_vectors)) != len(output_vectors):
        raise LabFailure("keyboard scenario replacement output is unchanged")
    return payload


def keyboard_rmlvo_hash(rmlvo: dict[str, Any]) -> str:
    payload = {
        "layout_groups": True,
        "layouts": rmlvo["layouts"],
        "model": rmlvo["model"],
        "options": rmlvo["options"],
        "rules": rmlvo["rules"],
        "variants": rmlvo["variants"],
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def selected_keyboard_scenario(
    selection: PatchSelection,
) -> tuple[Path, dict[str, Any], str]:
    """Resolve one generic case-owned scenario from the selected case set."""
    candidates = tuple(
        MAINTENANCE_ROOT / "cases" / slug / "tests" / KEYBOARD_SCENARIO_BASENAME
        for slug in selection.case_slugs
        if (MAINTENANCE_ROOT / "cases" / slug / "tests" / KEYBOARD_SCENARIO_BASENAME).is_file()
    )
    if len(candidates) != 1:
        raise LabFailure(
            "the keyboard profile requires one selected case-owned "
            f"{KEYBOARD_SCENARIO_BASENAME} scenario"
        )
    path = candidates[0]
    scenario = load_keyboard_scenario(path)
    return path, scenario, sha256_file(path)


def harness_sha256() -> str:
    digest = hashlib.sha256()
    for path in HARNESS_INPUTS:
        if path.is_symlink() or not path.is_file():
            raise LabFailure(f"live harness input is unavailable: {path}")
        digest.update(path.relative_to(MAINTENANCE_ROOT).as_posix().encode())
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256_file(path)))
        digest.update(b"\0")
    return digest.hexdigest()


def harness_snapshot_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for source in HARNESS_INPUTS:
        relative = source.relative_to(MAINTENANCE_ROOT)
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise LabFailure(f"frozen live harness input is unavailable: {relative}")
        digest.update(relative.as_posix().encode())
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256_file(path)))
        digest.update(b"\0")
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
            str(MAINTENANCE_ROOT),
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
        path = MAINTENANCE_ROOT / relative
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
            digest.update(b"clean-fork-master\0")
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
            "patch": patch.relative_to(MAINTENANCE_ROOT).as_posix(),
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


def resolve_embedded_source() -> tuple[str, str, int]:
    if not (SOURCE_REPOSITORY / ".git").exists():
        raise LabFailure(f"Xpra fork checkout is missing: {SOURCE_REPOSITORY}")
    if git_output("rev-parse", "--is-inside-work-tree") != "true":
        raise LabFailure(f"Xpra source is not a working tree: {SOURCE_REPOSITORY}")
    if git_output("branch", "--show-current") != "develop":
        raise LabFailure("live acceptance must run from the current develop branch")
    remotes = set(git_output("remote").splitlines())
    if "origin" not in remotes:
        raise LabFailure("Xpra fork checkout has no 'origin' remote")
    origin_url = git_output("remote", "get-url", "origin").removesuffix("/")
    if origin_url.removesuffix(".git") != FORK_REMOTE_URL.removesuffix(".git"):
        raise LabFailure(f"Xpra 'origin' remote has an unexpected URL: {origin_url}")

    head = git_output("rev-parse", "HEAD")
    source_tip = git_output("rev-parse", "refs/remotes/origin/master")
    if not re.fullmatch(r"[0-9a-f]{40}", head) or not re.fullmatch(
        r"[0-9a-f]{40}", source_tip
    ):
        raise LabFailure("could not resolve develop or cached origin/master")
    bases = git_output("merge-base", "--all", source_tip, head).splitlines()
    if len(bases) != 1 or not re.fullmatch(r"[0-9a-f]{40}", bases[0]):
        raise LabFailure("current develop has no single embedded source boundary")
    commit = bases[0]
    describe = git_output("describe", "--long", "--always", "--tags", commit)
    parts = describe.split("-")
    commit_marker = parts[-1] if len(parts) >= 3 else f"g{commit[:9]}"
    revision = (
        int(git_output("rev-list", "--count", "--first-parent", commit)) + 5014
    )
    if (
        git_output("rev-parse", "HEAD") != head
        or git_output("rev-parse", "refs/remotes/origin/master") != source_tip
    ):
        raise LabFailure("develop or cached origin/master changed while freezing source")
    return commit, commit_marker, revision


def create_source_snapshot(
    state_root: Path,
    *,
    temporary_root: Path | None = None,
) -> SourceSnapshot:
    commit, commit_marker, revision = resolve_embedded_source()
    archive_root = state_root / "source-archives"
    ensure_private_directory(archive_root, create=True)
    temporary_directory = temporary_root or archive_root
    ensure_private_directory(temporary_directory, create=True)
    with tempfile.NamedTemporaryFile(
        dir=temporary_directory,
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
            raise LabFailure("fork-master source archive has no test workflow") from error
        if workflow is None:
            raise LabFailure("fork-master source archive has no test workflow")
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
    *,
    temporary_root: Path | None = None,
) -> BuildContext:
    patches = selection.patches
    ensure_patch_selection_current(selection)
    for patch in patches:
        if not patch.is_file():
            raise LabFailure(f"case patch is missing: {patch}")
    context_root = state_root / "build-contexts" / "live"
    ensure_private_directory(context_root, create=True)
    temporary_directory = temporary_root or context_root
    ensure_private_directory(temporary_directory, create=True)
    temporary = Path(tempfile.mkdtemp(prefix=".context.", dir=temporary_directory))
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
            patch_manifest[patch.relative_to(MAINTENANCE_ROOT).as_posix()] = sha256_file(patch)
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
            patch = MAINTENANCE_ROOT / relative
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
                    "path": patch.relative_to(MAINTENANCE_ROOT).as_posix(),
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
        privatize_regular_tree(temporary)
        context_digest = tree_sha256(temporary)
        context_path = context_root / context_digest
        if context_path.is_symlink():
            raise LabFailure(f"cached build context is a symlink: {context_path}")
        if not context_path.exists():
            try:
                container_payload.rename_no_replace(temporary, context_path)
            except FileExistsError:
                pass
            except container_payload.PayloadError as error:
                raise LabFailure(str(error)) from error
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
                    path.relative_to(MAINTENANCE_ROOT).as_posix() for path in selection.patches
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
    zed_directory: Path | None,
    *,
    keyboard_scenario: tuple[Path, dict[str, Any], str] | None = None,
    zed_binary_sha256: str | None = None,
) -> tuple[str, Path | None, str | None]:
    inputs = result_directory / "inputs"
    harness = inputs / "harness"
    harness.mkdir(parents=True)
    for source in HARNESS_INPUTS:
        relative = source.relative_to(MAINTENANCE_ROOT)
        destination = harness / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    shutil.copy2(snapshot.archive_path, inputs / "source.tar")
    contexts = inputs / "contexts"
    contexts.mkdir()
    context_archive_sha256: dict[str, str] = {}
    for role, context in (
        ("server", server_context),
        ("client", client_context),
    ):
        archive_path = contexts / f"{role}.tar"
        with archive_path.open("xb") as stream:
            container_payload.write_archive(
                stream,
                tuple(
                    container_payload.PayloadEntry(
                        path,
                        PurePosixPath(path.name),
                    )
                    for path in sorted(
                        context.path.iterdir(),
                        key=lambda item: os.fsencode(item.name),
                    )
                ),
            )
        (contexts / f"{role}.manifest.json").write_text(
            json.dumps(context.manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        context_archive_sha256[role] = sha256_file(archive_path)
        if tree_sha256(context.path) != context.digest:
            raise LabFailure(f"{role} build context changed while it was frozen")
    selections = inputs / "selections"
    selections.mkdir()
    snapshot_patch_selection(selections / "server", server_context)
    snapshot_patch_selection(selections / "client", client_context)
    keyboard_scenario_manifest: dict[str, Any] | None = None
    if keyboard_scenario is not None:
        scenario_path, scenario, scenario_sha256 = keyboard_scenario
        if sha256_file(scenario_path) != scenario_sha256:
            raise LabFailure("keyboard scenario changed while it was being frozen")
        scenario_destination = inputs / "keyboard-scenario.json"
        shutil.copy2(scenario_path, scenario_destination)
        if load_keyboard_scenario(scenario_destination) != scenario:
            raise LabFailure("frozen keyboard scenario content is inconsistent")
        keyboard_scenario_manifest = {
            "name": scenario["name"],
            "path": scenario_path.relative_to(MAINTENANCE_ROOT).as_posix(),
            "schema": scenario["schema"],
            "sha256": scenario_sha256,
        }
    zed_archive: Path | None = None
    zed_archive_sha256: str | None = None
    if zed_directory is not None:
        zed_archive = inputs / "zed.tar"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(zed_archive, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                container_payload.write_archive(
                    stream,
                    (
                        container_payload.PayloadEntry(
                            zed_directory,
                            PurePosixPath("zed.app"),
                        ),
                    ),
                )
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        zed_archive_sha256 = sha256_file(zed_archive)
    manifest = {
        "schema": 2,
        "client_context_archive_sha256": context_archive_sha256["client"],
        "client_context_sha256": client_context.digest,
        "client_selection": client_context.selection.name,
        "client_selection_resolution_sha256": client_context.resolution[
            "resolution_sha256"
        ],
        "client_selection_sha256": client_context.selection.digest,
        "harness": {
            path.relative_to(MAINTENANCE_ROOT).as_posix(): sha256_file(
                harness / path.relative_to(MAINTENANCE_ROOT)
            )
            for path in HARNESS_INPUTS
        },
        "harness_sha256": harness_snapshot_sha256(harness),
        "keyboard_scenario": keyboard_scenario_manifest,
        "server_context_archive_sha256": context_archive_sha256["server"],
        "server_context_sha256": server_context.digest,
        "server_selection": server_context.selection.name,
        "server_selection_sha256": server_context.selection.digest,
        "server_selection_resolution_sha256": server_context.resolution[
            "resolution_sha256"
        ],
        "source_archive_sha256": snapshot.archive_sha256,
        "source_commit": snapshot.commit,
        "source_commit_marker": snapshot.commit_marker,
        "source_revision": snapshot.revision,
        "source_workflow_sha256": snapshot.workflow_sha256,
        "zed_archive_sha256": zed_archive_sha256,
        "zed_binary_sha256": zed_binary_sha256,
    }
    manifest_path = inputs / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    privatize_regular_tree(inputs)
    checksum_lines: list[str] = []
    for path in sorted(inputs.rglob("*")):
        if path.is_file() and not path.is_symlink() and path.name != "SHA256SUMS":
            checksum_lines.append(
                f"{sha256_file(path)}  {path.relative_to(inputs).as_posix()}\n"
            )
    checksums = inputs / "SHA256SUMS"
    checksums.write_text("".join(checksum_lines), encoding="utf-8")
    checksums.chmod(0o600)
    return sha256_file(manifest_path), zed_archive, zed_archive_sha256


def freeze_owned_inputs(
    result_directory: Path,
    state_root: Path,
    *,
    application: str,
    selection_name: str | None,
    zed_directory: Path | None,
) -> dict[str, Any]:
    """Freeze every mutable named-run input before its owner is launched."""
    if selection_name is None:
        raise LabFailure("live acceptance requires one non-empty case or stack selection")
    ensure_private_directory(result_directory)
    if tuple(result_directory.iterdir()):
        raise LabFailure(f"live input staging directory is not empty: {result_directory}")
    initial_harness_sha256 = harness_sha256()
    if application == "zed":
        if zed_directory is None:
            raise LabFailure("Zed input directory is required for the Zed profile")
        zed_binary = zed_directory / "libexec" / "zed-editor"
        if not zed_binary.is_file() or not os.access(zed_binary, os.X_OK):
            raise LabFailure(f"Zed executable is unavailable: {zed_binary}")
        zed_binary_sha256 = sha256_file(zed_binary)
    else:
        zed_binary = None
        zed_binary_sha256 = None

    freeze_root = result_directory / ".freeze"
    freeze_root.mkdir(mode=0o700)
    try:
        snapshot = create_source_snapshot(state_root, temporary_root=freeze_root)
        server_selection = resolve_patch_selection(selection_name, None)
        client_selection = resolve_patch_selection(None, "master")
        keyboard_scenario = (
            selected_keyboard_scenario(server_selection)
            if application == "keyboard"
            else None
        )
        server_context = prepare_build_context(
            state_root,
            snapshot,
            server_selection,
            temporary_root=freeze_root,
        )
        client_context = prepare_build_context(
            state_root,
            snapshot,
            client_selection,
            temporary_root=freeze_root,
        )
        input_manifest_sha256, _zed_archive, _zed_archive_sha256 = (
            snapshot_build_inputs(
                result_directory,
                snapshot,
                server_context,
                client_context,
                zed_directory if application == "zed" else None,
                keyboard_scenario=keyboard_scenario,
                zed_binary_sha256=zed_binary_sha256,
            )
        )
        if zed_binary is not None and sha256_file(zed_binary) != zed_binary_sha256:
            raise LabFailure("Zed executable changed while its payload was frozen")
        if harness_sha256() != initial_harness_sha256:
            raise LabFailure("live harness changed while its inputs were being frozen")
    finally:
        if freeze_root.exists():
            shutil.rmtree(freeze_root)

    inputs = result_directory / "inputs"
    manifest = json.loads((inputs / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise LabFailure("frozen live input manifest is not an object")
    if manifest.get("harness_sha256") != initial_harness_sha256:
        raise LabFailure("frozen live harness digest does not match its source")
    return {
        **manifest,
        "input_manifest_sha256": input_manifest_sha256,
        "input_tree_sha256": tree_sha256(inputs),
    }


def _validated_input_checksums(inputs: Path) -> dict[str, str]:
    checksum_path = inputs / "SHA256SUMS"
    ensure_private_regular_file(checksum_path)
    expected: dict[str, str] = {}
    for line in checksum_path.read_text(encoding="utf-8").splitlines():
        digest, separator, relative_value = line.partition("  ")
        try:
            relative = container_payload.archive_path(relative_value)
        except container_payload.PayloadError as error:
            raise LabFailure("frozen live input checksum has an unsafe path") from error
        key = str(relative)
        if not separator or not re.fullmatch(r"[0-9a-f]{64}", digest) or key in expected:
            raise LabFailure("frozen live input checksum manifest is invalid")
        expected[key] = digest
    observed: dict[str, str] = {}
    for directory, directory_names, file_names in os.walk(inputs, followlinks=False):
        root = Path(directory)
        for name in directory_names:
            path = root / name
            if path.is_symlink() or not path.is_dir():
                raise LabFailure(f"frozen live inputs contain an unsafe directory: {path}")
        for name in file_names:
            path = root / name
            if path.is_symlink() or not path.is_file():
                raise LabFailure(f"frozen live inputs contain an unsafe file: {path}")
            if path != checksum_path:
                observed[path.relative_to(inputs).as_posix()] = sha256_file(path)
    if observed != expected:
        raise LabFailure("frozen live input checksums do not match their files")
    return observed


def _bound_context(inputs: Path, role: str, manifest: dict[str, Any]) -> BuildContext:
    context_manifest_path = inputs / "contexts" / f"{role}.manifest.json"
    ensure_private_regular_file(context_manifest_path)
    try:
        context_manifest = json.loads(context_manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise LabFailure(f"frozen {role} context manifest is invalid JSON") from error
    if not isinstance(context_manifest, dict):
        raise LabFailure(f"frozen {role} context manifest is not an object")
    selection_value = context_manifest.get("selection")
    if not isinstance(selection_value, dict):
        raise LabFailure(f"frozen {role} selection provenance is missing")
    case_slugs = selection_value.get("case_slugs")
    selectors = selection_value.get("selectors")
    selector_digests = selection_value.get("selector_digests")
    resolution = selection_value.get("resolution")
    selection_name = selection_value.get("name")
    selection_digest = selection_value.get("digest")
    if (
        not isinstance(case_slugs, list)
        or not all(isinstance(value, str) for value in case_slugs)
        or not isinstance(selectors, list)
        or not all(isinstance(value, str) for value in selectors)
        or not isinstance(selector_digests, dict)
        or set(selector_digests) != set(selectors)
        or not all(
            isinstance(key, str)
            and isinstance(value, str)
            and re.fullmatch(r"[0-9a-f]{64}", value)
            for key, value in selector_digests.items()
        )
        or not isinstance(resolution, dict)
        or not isinstance(selection_name, str)
        or not isinstance(selection_digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", selection_digest)
    ):
        raise LabFailure(f"frozen {role} selection provenance is invalid")
    context_digest = manifest.get(f"{role}_context_sha256")
    context_archive_sha256 = manifest.get(f"{role}_context_archive_sha256")
    if (
        not isinstance(context_digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", context_digest)
        or not isinstance(context_archive_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", context_archive_sha256)
        or manifest.get(f"{role}_selection") != selection_name
        or manifest.get(f"{role}_selection_sha256") != selection_digest
        or manifest.get(f"{role}_selection_resolution_sha256")
        != resolution.get("resolution_sha256")
    ):
        raise LabFailure(f"frozen {role} context provenance is inconsistent")
    archive = inputs / "contexts" / f"{role}.tar"
    ensure_private_regular_file(archive)
    if sha256_file(archive) != context_archive_sha256:
        raise LabFailure(f"frozen {role} context archive changed")
    with tempfile.TemporaryDirectory(
        prefix=f".{role}-context-validation-",
        dir=inputs.parent,
    ) as temporary_name:
        extracted = Path(temporary_name) / "context"
        try:
            with archive.open("rb") as stream:
                container_payload.extract_archive(stream, extracted)
        except container_payload.PayloadError as error:
            raise LabFailure(f"frozen {role} context archive is invalid") from error
        if tree_sha256(extracted) != context_digest:
            raise LabFailure(
                f"frozen {role} context archive does not match its context digest"
            )
    selection = PatchSelection(
        case_slugs=tuple(case_slugs),
        digest=selection_digest,
        name=selection_name,
        patches=(),
        selector_digests=tuple((key, selector_digests[key]) for key in selectors),
        selectors=tuple(selectors),
    )
    return BuildContext(
        digest=context_digest,
        manifest=context_manifest,
        patches=(),
        path=archive,
        resolution=resolution,
        selection=selection,
        archive_sha256=context_archive_sha256,
    )


def load_bound_inputs(
    inputs: Path,
    *,
    expected_manifest_sha256: str,
    expected_tree_sha256: str,
) -> BoundInputs:
    """Load and fully validate the immutable input tree bound by live-start."""
    ensure_private_directory(inputs)
    if tree_sha256(inputs) != expected_tree_sha256:
        raise LabFailure("frozen live input tree does not match its owner record")
    _validated_input_checksums(inputs)
    manifest_path = inputs / "manifest.json"
    ensure_private_regular_file(manifest_path)
    if sha256_file(manifest_path) != expected_manifest_sha256:
        raise LabFailure("frozen live input manifest does not match its owner record")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise LabFailure("frozen live input manifest is invalid JSON") from error
    if not isinstance(manifest, dict) or manifest.get("schema") != 2:
        raise LabFailure("frozen live input manifest has an unsupported schema")
    source_archive = inputs / "source.tar"
    ensure_private_regular_file(source_archive)
    source_commit = manifest.get("source_commit")
    source_archive_sha256 = manifest.get("source_archive_sha256")
    source_workflow_sha256 = manifest.get("source_workflow_sha256")
    source_commit_marker = manifest.get("source_commit_marker")
    source_revision = manifest.get("source_revision")
    if (
        not isinstance(source_commit, str)
        or not re.fullmatch(r"[0-9a-f]{40}", source_commit)
        or not isinstance(source_archive_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", source_archive_sha256)
        or sha256_file(source_archive) != source_archive_sha256
        or not isinstance(source_workflow_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", source_workflow_sha256)
        or not isinstance(source_commit_marker, str)
        or not isinstance(source_revision, int)
    ):
        raise LabFailure("frozen live source provenance is invalid")
    if manifest.get("harness_sha256") != harness_sha256():
        raise LabFailure("executing live harness does not match the frozen input manifest")
    zed_archive_sha256 = manifest.get("zed_archive_sha256")
    zed_binary_sha256 = manifest.get("zed_binary_sha256")
    zed_archive = inputs / "zed.tar"
    if zed_archive_sha256 is None:
        if zed_archive.exists() or zed_archive.is_symlink() or zed_binary_sha256 is not None:
            raise LabFailure("unexpected Zed data in frozen live inputs")
        zed_archive_path: Path | None = None
    else:
        if (
            not isinstance(zed_archive_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", zed_archive_sha256)
            or not isinstance(zed_binary_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", zed_binary_sha256)
        ):
            raise LabFailure("frozen Zed provenance is invalid")
        ensure_private_regular_file(zed_archive)
        if sha256_file(zed_archive) != zed_archive_sha256:
            raise LabFailure("frozen Zed archive changed")
        zed_archive_path = zed_archive
    keyboard_descriptor = manifest.get("keyboard_scenario")
    keyboard_path = inputs / "keyboard-scenario.json"
    if keyboard_descriptor is None:
        if keyboard_path.exists() or keyboard_path.is_symlink():
            raise LabFailure("unexpected keyboard scenario in frozen live inputs")
        keyboard_scenario = None
        keyboard_scenario_path = None
        keyboard_scenario_sha256 = None
    else:
        if (
            not isinstance(keyboard_descriptor, dict)
            or set(keyboard_descriptor) != {"name", "path", "schema", "sha256"}
            or keyboard_descriptor.get("schema") != 1
            or not isinstance(keyboard_descriptor.get("name"), str)
            or not isinstance(keyboard_descriptor.get("path"), str)
            or not isinstance(keyboard_descriptor.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", keyboard_descriptor["sha256"])
            is None
        ):
            raise LabFailure("frozen keyboard scenario provenance is invalid")
        relative = PurePosixPath(keyboard_descriptor["path"])
        if (
            relative.is_absolute()
            or relative.as_posix() != keyboard_descriptor["path"]
            or len(relative.parts) != 4
            or relative.parts[0] != "cases"
            or relative.parts[2:] != ("tests", KEYBOARD_SCENARIO_BASENAME)
        ):
            raise LabFailure("frozen keyboard scenario source path is unsafe")
        ensure_private_regular_file(keyboard_path)
        keyboard_scenario_sha256 = keyboard_descriptor["sha256"]
        if sha256_file(keyboard_path) != keyboard_scenario_sha256:
            raise LabFailure("frozen keyboard scenario changed")
        keyboard_scenario = load_keyboard_scenario(keyboard_path)
        if (
            keyboard_scenario.get("schema") != keyboard_descriptor["schema"]
            or keyboard_scenario.get("name") != keyboard_descriptor["name"]
        ):
            raise LabFailure("frozen keyboard scenario identity is inconsistent")
        keyboard_scenario_path = keyboard_path
    return BoundInputs(
        client_context=_bound_context(inputs, "client", manifest),
        input_manifest_sha256=expected_manifest_sha256,
        input_tree_sha256=expected_tree_sha256,
        server_context=_bound_context(inputs, "server", manifest),
        snapshot=SourceSnapshot(
            archive_path=source_archive,
            archive_sha256=source_archive_sha256,
            commit=source_commit,
            commit_marker=source_commit_marker,
            revision=source_revision,
            workflow_sha256=source_workflow_sha256,
        ),
        keyboard_scenario=keyboard_scenario,
        keyboard_scenario_path=keyboard_scenario_path,
        keyboard_scenario_sha256=keyboard_scenario_sha256,
        zed_archive=zed_archive_path,
        zed_archive_sha256=zed_archive_sha256,
        zed_binary_sha256=zed_binary_sha256,
    )


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
    directory: Path,
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
    pull_container_artifacts(container, directory, (destination,))


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
    pull_container_artifacts(container, directory, (f"{stem}.rgba.png",))
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
    alpha_histogram = alpha.histogram()
    alpha_pixels = sum(alpha_histogram)
    alpha_minimum, alpha_maximum = alpha.getextrema()
    rgb_bytes = rgb.tobytes()
    return {
        "alpha_maximum": alpha_maximum,
        "alpha_minimum": alpha_minimum,
        "alpha_nonopaque_ratio": (
            sum(alpha_histogram[:255]) / alpha_pixels if alpha_pixels else 0.0
        ),
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


def image_alpha_content_checks(
    image: Any,
    *,
    prefix: str,
) -> dict[str, bool]:
    """Require measured nonopaque and fully opaque pixels in one image."""
    alpha_minimum = image.get("alpha_minimum") if isinstance(image, dict) else None
    alpha_maximum = image.get("alpha_maximum") if isinstance(image, dict) else None
    nonopaque_ratio = (
        image.get("alpha_nonopaque_ratio") if isinstance(image, dict) else None
    )
    return {
        f"{prefix}_has_transparent_pixels": bool(
            isinstance(alpha_minimum, int)
            and not isinstance(alpha_minimum, bool)
            and alpha_minimum < 255
            and isinstance(nonopaque_ratio, (int, float))
            and not isinstance(nonopaque_ratio, bool)
            and 0 < nonopaque_ratio <= 1
        ),
        f"{prefix}_has_opaque_pixels": bool(
            isinstance(alpha_maximum, int)
            and not isinstance(alpha_maximum, bool)
            and alpha_maximum == 255
        ),
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
    if application in {"gtk", "keyboard"}:
        # GTK text rasterization can differ by one intensity level at glyph edges
        # between the server pixels and the X11 client capture.  Keep the static
        # Zed RGB proof byte-exact and scope this bounded tolerance to the tracked
        # GTK fixtures only.
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


def crop_client_source_viewport(
    directory: Path,
    input_stem: str,
    output_stem: str,
    source_size: tuple[int, int],
) -> dict[str, Any]:
    """Crop the exact north-west Xpra source viewport from a larger backing."""
    source_width, source_height = source_size
    with Image.open(directory / f"{input_stem}.rgba.png") as source:
        backing = source.convert("RGBA")
    if (
        source_width <= 0
        or source_height <= 0
        or source_width > backing.width
        or source_height > backing.height
    ):
        raise LabFailure(
            f"source viewport {source_size!r} does not fit backing {backing.size!r}"
        )
    crop = backing.crop((0, 0, source_width, source_height))
    crop.save(directory / f"{output_stem}.rgba.png", format="PNG")
    crop.convert("RGB").save(directory / f"{output_stem}.rgb.png", format="PNG")
    save_alpha_visualization(crop, directory / f"{output_stem}.alpha.png")
    return {
        "image": analyze_image(crop),
        "viewport": {
            "backing_size": [backing.width, backing.height],
            "origin": [0, 0],
            "source_size": [source_width, source_height],
        },
    }


def client_source_viewport_logged(
    directory: Path,
    source_size: tuple[int, int],
    backing_size: tuple[int, int],
) -> bool:
    """Require the GL log to bind the source crop to the controlled backing."""
    source_width, source_height = source_size
    backing_width, backing_height = backing_size
    if source_width > backing_width or source_height > backing_height:
        return False
    expected = (
        f"viewport: (0, {backing_height - source_height}, "
        f"{source_width}, {source_height}) for backing size="
        f"({backing_width}, {backing_height})"
    )
    path = directory / "client.stdout"
    return path.is_file() and expected in path.read_text(
        encoding="utf-8",
        errors="replace",
    )


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
grep -E 'libvulkan_radeon|libEGL_mesa|libGLX_mesa|radeonsi_dri|swrast_dri|libgallium|libva\.so' "/proc/$pid/maps" 2>/dev/null || true
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


def process_gpu_evidence_matches_identity(
    evidence: Any,
    identity: Any,
) -> bool:
    """Bind procfs GPU evidence to the exact process cmdline identity."""
    return bool(
        isinstance(evidence, dict)
        and valid_process_identity(identity)
        and evidence.get("pid") == identity["pid"]
        and evidence.get("argv") == " ".join(identity["argv"]) + " "
    )


def keyboard_process_connection_identity(
    container: str,
    pid: int,
    *,
    server_side: bool,
) -> dict[str, Any]:
    """Bind one Xpra process and its isolated namespace TCP identity.

    The server deliberately disables ptrace via ``PR_SET_DUMPABLE=0``, so an
    unprivileged peer cannot inspect its fd table.  The matching client/server
    tuples are cross-bound by :func:`keyboard_live_checks` instead.
    """
    probe = r"""
import hashlib
import json
import pathlib
import sys

pid = int(sys.argv[1])
port = int(sys.argv[2])
server_side = sys.argv[3] == 'server'

root = pathlib.Path('/proc') / str(pid)
stat_value = (root / 'stat').read_text(encoding='ascii')
end = stat_value.rfind(')')
if end < 0:
    raise SystemExit(70)
fields = stat_value[end + 2:].split()
if len(fields) < 20:
    raise SystemExit(70)
cmdline = (root / 'cmdline').read_bytes()
if not cmdline or len(cmdline) > 1024 * 1024:
    raise SystemExit(70)
process = {
    'cmdline_sha256': hashlib.sha256(cmdline).hexdigest(),
    'pid': pid,
    'start_ticks': fields[19],
}
port_connections = []
for family, path in (('tcp4', pathlib.Path('/proc/net/tcp')), ('tcp6', pathlib.Path('/proc/net/tcp6'))):
    for line in path.read_text(encoding='ascii').splitlines()[1:]:
        values = line.split()
        if len(values) < 10 or values[3] != '01':
            continue
        local_address, local_port = values[1].split(':')
        remote_address, remote_port = values[2].split(':')
        local_port_value = int(local_port, 16)
        remote_port_value = int(remote_port, 16)
        if (server_side and local_port_value != port) or (not server_side and remote_port_value != port):
            continue
        connection = {
            'family': family,
            'inode': int(values[9]),
            'local_address': local_address,
            'local_port': local_port_value,
            'remote_address': remote_address,
            'remote_port': remote_port_value,
            'state': 'established',
        }
        port_connections.append(connection)
if len(port_connections) != 1:
    print(json.dumps({
        'port_connections': port_connections[:64],
    }, sort_keys=True), file=sys.stderr)
    raise SystemExit(71)
print(json.dumps({
    'connection': port_connections[0],
    'process': process,
}, sort_keys=True))
"""
    result = podman_exec(
        container,
        [
            "python3",
            "-c",
            probe,
            str(pid),
            str(SERVER_PORT),
            "server" if server_side else "client",
        ],
        check=False,
        announce=False,
    )
    if result.returncode:
        diagnostic = result.stderr.strip()[-4096:]
        suffix = f": {diagnostic}" if diagnostic else ""
        raise LabFailure(
            f"could not bind the {'server' if server_side else 'client'} Xpra TCP identity"
            f"{suffix}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise LabFailure("Xpra TCP identity probe returned invalid JSON") from error
    process = payload.get("process") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or set(payload) != {"connection", "process"}
        or not isinstance(process, dict)
        or set(process) != {"cmdline_sha256", "pid", "start_ticks"}
        or process.get("pid") != pid
        or re.fullmatch(r"[0-9a-f]{64}", str(process.get("cmdline_sha256", "")))
        is None
        or re.fullmatch(r"[1-9][0-9]*", str(process.get("start_ticks", "")))
        is None
        or not isinstance(payload.get("connection"), dict)
    ):
        raise LabFailure("Xpra TCP identity probe returned invalid evidence")
    return payload


def keyboard_identity_snapshot(
    server: str,
    server_pid: int,
    client: str,
    client_pid: int,
) -> dict[str, Any]:
    return {
        "client": keyboard_process_connection_identity(
            client,
            client_pid,
            server_side=False,
        ),
        "server": keyboard_process_connection_identity(
            server,
            server_pid,
            server_side=True,
        ),
    }


def server_xpra_window_inventory(info_path: Path) -> dict[int, str]:
    """Return the exact server window-title inventory from ``xpra info``."""
    inventory: dict[int, str] = {}
    for line in info_path.read_text(encoding="utf-8", errors="replace").splitlines():
        key, separator, value = line.partition("=")
        match = re.fullmatch(r"windows\.([1-9][0-9]*)\.title", key)
        if separator and match:
            window_id = int(match.group(1))
            if window_id in inventory:
                raise LabFailure(f"xpra info repeats window title for ID {window_id}")
            inventory[window_id] = value
    return inventory


def server_xpra_window_id(info_path: Path, title_patterns: tuple[str, ...]) -> int:
    """Resolve one server window ID from its title in ``xpra info``."""
    expected = {pattern.casefold() for pattern in title_patterns}
    matches = [
        window_id
        for window_id, title in server_xpra_window_inventory(info_path).items()
        if any(pattern in title.casefold() for pattern in expected)
    ]
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
            and container_artifact_exists(container, f"{name}.exit")
        ),
        timeout=timeout,
    )
    pull_container_artifacts(container, directory, (f"{name}.exit",))
    return process_exit_status(directory, name)


def parse_saved_updates(directory: Path, xpra_wid: int) -> dict[str, Any]:
    updates: list[dict[str, Any]] = []
    window_directory = directory / "screen-updates" / str(xpra_wid)
    for info_path in sorted(window_directory.glob("*/[0-9]*.info")):
        info = json.loads(info_path.read_text(encoding="utf-8"))
        if not isinstance(info, dict):
            raise LabFailure(f"saved update metadata is not an object: {info_path}")
        payload_name = info.get("file")
        if (
            not isinstance(payload_name, str)
            or payload_name in {"", ".", ".."}
            or PurePosixPath(payload_name).name != payload_name
        ):
            raise LabFailure(f"saved update payload name is unsafe: {info_path}")
        payload = info_path.parent / payload_name
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


def synchronize_saved_updates(
    container: str,
    directory: Path,
    xpra_wid: int,
) -> dict[str, Any]:
    """Pull only completed immutable packet metadata and payloads for one window."""
    prefix = f"screen-updates/{xpra_wid}/"
    listed_info = tuple(
        relative
        for relative in container_artifact_files(
            container,
            "screen-updates",
            "*.info",
        )
        if relative.startswith(prefix)
    )
    window_info = f"{prefix}window.info"
    if window_info not in listed_info:
        raise LabFailure(f"saved window metadata is unavailable: {window_info}")
    remote_info = tuple(
        relative
        for relative in listed_info
        if re.fullmatch(
            rf"screen-updates/{xpra_wid}/(?:0|[1-9][0-9]*)/"
            r"(?:0|[1-9][0-9]*)\.info",
            relative,
        )
    )
    missing_info = tuple(
        relative
        for relative in (window_info, *remote_info)
        if not (directory / relative).is_file()
    )
    if missing_info:
        pull_container_artifacts(container, directory, missing_info)
    payloads: list[str] = []
    for relative in remote_info:
        info_path = directory / relative
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise LabFailure(f"invalid saved update metadata: {relative}") from error
        if not isinstance(info, dict):
            raise LabFailure(f"saved update metadata is not an object: {relative}")
        payload_name = info.get("file")
        if (
            not isinstance(payload_name, str)
            or payload_name in {"", ".", ".."}
            or PurePosixPath(payload_name).name != payload_name
        ):
            raise LabFailure(f"saved update payload name is unsafe: {relative}")
        payload = (PurePosixPath(relative).parent / payload_name).as_posix()
        if not (directory / payload).is_file():
            payloads.append(payload)
    if payloads:
        pull_container_artifacts(
            container,
            directory,
            tuple(sorted(set(payloads))),
        )
    updates = parse_saved_updates(directory, xpra_wid)
    updates["initial_pixel_format"] = saved_window_initial_pixel_format(
        directory,
        xpra_wid,
    )
    return updates


def saved_window_initial_pixel_format(directory: Path, xpra_wid: int) -> str:
    """Read the source pixel format saved with one window's first update."""
    info_path = directory / "screen-updates" / str(xpra_wid) / "window.info"
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LabFailure(f"invalid saved window metadata: {info_path}") from error
    if not isinstance(info, dict):
        raise LabFailure(f"saved window metadata is not an object: {info_path}")
    pixel_format = info.get("pixel-format")
    if not isinstance(pixel_format, str) or not pixel_format:
        raise LabFailure(f"saved window metadata has no pixel format: {info_path}")
    return pixel_format


def only_positive_h264_packets(updates: dict[str, Any] | None) -> bool:
    """Return whether one exact window produced only non-empty H.264 updates."""
    if not isinstance(updates, dict):
        return False
    packets = updates.get("updates")
    count = _exact_int(updates.get("count"), positive=True)
    encodings = updates.get("encodings")
    return bool(
        isinstance(packets, list)
        and count == len(packets)
        and isinstance(encodings, list)
        and all(isinstance(encoding, str) for encoding in encodings)
        and set(encodings) == {"h264"}
        and all(
            isinstance(packet, dict)
            and packet.get("encoding") == "h264"
            and _exact_int(packet.get("payload_bytes"), positive=True) is not None
            for packet in packets
        )
    )


def only_positive_alpha_capable_packets(updates: dict[str, Any] | None) -> bool:
    """Validate one alpha window's exact non-empty WebP/RGB32 packet set."""
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
        or len(encodings) != len(set(encodings))
        or not all(isinstance(packet, dict) for packet in packets)
        or not all(isinstance(packet.get("encoding"), str) for packet in packets)
        or updates.get("initial_pixel_format") not in {"BGRA", "RGBA"}
    ):
        return False
    actual_encodings = {packet.get("encoding") for packet in packets}
    if (
        set(encodings) != actual_encodings
        or not actual_encodings
        or not actual_encodings <= {"webp", "rgb32"}
    ):
        return False
    window_id = _exact_int(updates.get("window_id"), positive=True)
    sequences = [
        _exact_int(packet.get("sequence"), positive=True) for packet in packets
    ]
    if window_id is None or any(sequence is None for sequence in sequences):
        return False
    exact_sequences = [int(sequence) for sequence in sequences if sequence is not None]
    if exact_sequences != list(
        range(exact_sequences[0], exact_sequences[0] + len(exact_sequences))
    ):
        return False
    return _alpha_safe_warmup_groups_valid(packets, window_id)


def _exact_int(value: Any, *, positive: bool = False) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    if positive and value <= 0:
        return None
    return value


def _bounded_utf8_string(
    value: Any,
    maximum_bytes: int,
    *,
    nonempty: bool = False,
) -> bool:
    if not isinstance(value, str) or (nonempty and not value):
        return False
    try:
        return len(value.encode("utf-8")) <= maximum_bytes
    except UnicodeEncodeError:
        return False


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


def _saved_update_group_location(
    update: dict[str, Any],
    window_id: int,
) -> tuple[str, int] | None:
    relative_info = update.get("relative_info")
    if not isinstance(relative_info, str) or not relative_info:
        return None
    path = PurePosixPath(relative_info)
    if path.is_absolute() or path.as_posix() != relative_info or len(path.parts) != 4:
        return None
    root, relative_window_id, group, filename = path.parts
    index_match = re.fullmatch(r"(0|[1-9][0-9]*)\.info", filename)
    if (
        root != "screen-updates"
        or relative_window_id != str(window_id)
        or re.fullmatch(r"(0|[1-9][0-9]*)", group) is None
        or index_match is None
    ):
        return None
    return group, int(index_match.group(1))


def _ordered_saved_damage_groups(
    packets: list[dict[str, Any]],
    window_id: int,
) -> list[list[dict[str, Any]]] | None:
    seen_groups: set[str] = set()
    previous_group = ""
    expected_index = 0
    for packet in packets:
        location = _saved_update_group_location(packet, window_id)
        sequence = _exact_int(packet.get("sequence"), positive=True)
        if location is None or sequence is None:
            return None
        group, index = location
        if group != previous_group:
            if group in seen_groups:
                return None
            if previous_group and int(group) <= int(previous_group):
                return None
            seen_groups.add(group)
            previous_group = group
            expected_index = 0
        if index != expected_index:
            return None
        expected_index += 1

    ordered_groups: list[list[dict[str, Any]]] = []
    offset = 0
    while offset < len(packets):
        first = packets[offset]
        first_options = first.get("options")
        first_flush = (
            _exact_int(first_options.get("flush"))
            if isinstance(first_options, dict)
            else None
        )
        if first_flush is None or first_flush < 0:
            return None
        group_length = first_flush + 1
        group_packets = packets[offset : offset + group_length]
        if len(group_packets) != group_length:
            return None
        first_location = _saved_update_group_location(first, window_id)
        if first_location is None:
            return None
        directory = first_location[0]
        if any(
            (location := _saved_update_group_location(packet, window_id)) is None
            or location[0] != directory
            for packet in group_packets
        ):
            return None
        sequences = [int(packet["sequence"]) for packet in group_packets]
        if sequences != list(range(sequences[0], sequences[0] + len(sequences))):
            return None
        flushes: list[int] = []
        for packet in group_packets:
            options = packet.get("options")
            flush = _exact_int(options.get("flush")) if isinstance(options, dict) else None
            if flush is None or flush < 0:
                return None
            flushes.append(flush)
        if flushes != list(range(len(group_packets) - 1, -1, -1)):
            return None
        ordered_groups.append(group_packets)
        offset += group_length
    return ordered_groups


def _alpha_safe_warmup_groups_valid(
    packets: list[dict[str, Any]],
    window_id: int,
) -> bool:
    groups = _ordered_saved_damage_groups(packets, window_id)
    if groups is None:
        return False
    for group in groups:
        for packet in group:
            if not _alpha_safe_packet(packet):
                return False
    return True


def _alpha_safe_packet(packet: dict[str, Any]) -> bool:
    if _exact_int(packet.get("payload_bytes"), positive=True) is None:
        return False
    geometry = _packet_geometry(packet)
    window_size = _packet_window_size(packet)
    if geometry is None or window_size is None:
        return False
    x, y, width, height = geometry
    window_width, window_height = window_size
    if x + width > window_width or y + height > window_height:
        return False
    if packet.get("encoding") == "webp":
        return True
    options = packet.get("options")
    return bool(
        packet.get("encoding") == "rgb32"
        and isinstance(options, dict)
        and options.get("rgb_format") in {"BGRA", "RGBA"}
    )


def _safe_h264_context_gap(packet: dict[str, Any]) -> bool:
    """Accept a positive contained non-video region between one codec stream."""
    if _exact_int(packet.get("payload_bytes"), positive=True) is None:
        return False
    geometry = _packet_geometry(packet)
    window_size = _packet_window_size(packet)
    options = packet.get("options")
    if geometry is None or window_size is None or not isinstance(options, dict):
        return False
    x, y, width, height = geometry
    if x + width > window_size[0] or y + height > window_size[1]:
        return False
    if packet.get("encoding") == "webp":
        return True
    return bool(
        packet.get("encoding") in {"rgb24", "rgb32"}
        and options.get("rgb_format")
        in {
            "BGR",
            "BGRA",
            "BGRX",
            "RGB",
            "RGBA",
            "RGBX",
            "XBGR",
            "XRGB",
        }
    )


def _h264_packet_groups_valid(groups: list[list[dict[str, Any]]]) -> bool:
    cropped_signatures: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    edge_complete_signatures: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    for group_packets in groups:
        terminal = group_packets[-1]
        main_size = _h264_main_size(terminal)
        window_size = _packet_window_size(terminal)
        if main_size is None or window_size is None:
            return False
        if any(packet.get("encoding") == "h264" for packet in group_packets[:-1]):
            return False
        if any(_packet_window_size(packet) != window_size for packet in group_packets):
            return False
        main_width, main_height = main_size
        window_width, window_height = window_size
        required_edges = set()
        if main_width == window_width - 1:
            required_edges.add("right")
        if main_height == window_height - 1:
            required_edges.add("bottom")
        crop_signature = (window_size, main_size)
        if required_edges:
            cropped_signatures.add(crop_signature)

        edge_packets = group_packets[:-1]
        if not edge_packets:
            continue
        edge_kinds = [_lossless_rgb_edge_kind(packet) for packet in edge_packets]
        if (
            any(edge is None for edge in edge_kinds)
            or len(edge_kinds) != len(set(edge_kinds))
            or set(edge_kinds) != required_edges
        ):
            return False
        edge_complete_signatures.add(crop_signature)
    return cropped_signatures <= edge_complete_signatures


def _h264_readiness_group_valid(group_packets: list[dict[str, Any]]) -> bool:
    """Validate one safe early H.264 group without requiring its final edges."""
    terminal = group_packets[-1]
    main_size = _h264_main_size(terminal)
    window_size = _packet_window_size(terminal)
    options = terminal.get("options")
    if (
        main_size is None
        or window_size is None
        or not isinstance(options, dict)
        or (_exact_int(options.get("frame")) is None)
        or int(options["frame"]) < 0
        or not isinstance(options.get("type"), str)
        or not options["type"]
        or any(packet.get("encoding") == "h264" for packet in group_packets[:-1])
        or any(_packet_window_size(packet) != window_size for packet in group_packets)
    ):
        return False
    main_width, main_height = main_size
    window_width, window_height = window_size
    required_edges = set()
    if main_width == window_width - 1:
        required_edges.add("right")
    if main_height == window_height - 1:
        required_edges.add("bottom")
    edge_kinds = [
        _lossless_rgb_edge_kind(packet) for packet in group_packets[:-1]
    ]
    return bool(
        not any(edge is None for edge in edge_kinds)
        and len(edge_kinds) == len(set(edge_kinds))
        and set(edge_kinds) <= required_edges
    )


def adaptive_h264_frame_readiness_valid(
    application: str,
    updates: dict[str, Any] | None,
) -> bool:
    """Validate safe saved packets for the first decoded adaptive H.264 frame."""
    if application not in MULTIWINDOW_HARDWARE_APPLICATIONS | {"zed"}:
        return False
    allowed_initial_formats = (
        {"BGRX", "RGBX"}
        if application in MULTIWINDOW_HARDWARE_APPLICATIONS
        else {"BGRA", "BGRX", "RGBA", "RGBX"}
    )
    if (
        not isinstance(updates, dict)
        or updates.get("initial_pixel_format") not in allowed_initial_formats
    ):
        return False
    packets = updates.get("updates")
    count = _exact_int(updates.get("count"), positive=True)
    encodings = updates.get("encodings")
    window_id = _exact_int(updates.get("window_id"), positive=True)
    if (
        not isinstance(packets, list)
        or count != len(packets)
        or not isinstance(encodings, list)
        or not all(isinstance(encoding, str) for encoding in encodings)
        or len(encodings) != len(set(encodings))
        or not all(isinstance(packet, dict) for packet in packets)
        or not all(isinstance(packet.get("encoding"), str) for packet in packets)
        or window_id is None
    ):
        return False
    actual_encodings = {packet.get("encoding") for packet in packets}
    if set(encodings) != actual_encodings:
        return False
    sequences = [
        _exact_int(packet.get("sequence"), positive=True) for packet in packets
    ]
    if any(sequence is None for sequence in sequences):
        return False
    exact_sequences = [int(sequence) for sequence in sequences if sequence is not None]
    if exact_sequences != list(
        range(exact_sequences[0], exact_sequences[0] + len(exact_sequences))
    ):
        return False
    groups = _ordered_saved_damage_groups(packets, window_id)
    if groups is None:
        return False
    h264_group_seen = False
    for group in groups:
        group_encodings = {packet.get("encoding") for packet in group}
        if group_encodings <= {"webp", "rgb32"}:
            if not all(_alpha_safe_packet(packet) for packet in group):
                return False
            continue
        if not _h264_readiness_group_valid(group):
            return False
        h264_group_seen = True
    return h264_group_seen


def _h264_damage_groups_valid(updates: dict[str, Any]) -> bool:
    """Validate exact packet grouping and flush order for one H.264 window."""
    window_id = _exact_int(updates.get("window_id"), positive=True)
    packets = updates.get("updates")
    if window_id is None or not isinstance(packets, list) or not packets:
        return False
    if not all(isinstance(packet, dict) for packet in packets):
        return False
    groups = _ordered_saved_damage_groups(packets, window_id)
    return groups is not None and _h264_packet_groups_valid(groups)


def h264_with_lossless_rgb_edges(updates: dict[str, Any] | None) -> bool:
    """Validate H.264 regions and exact per-damage one-pixel RGB codec edges."""
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

    for packet in packets:
        if packet.get("encoding") == "h264":
            if _h264_main_size(packet) is None:
                return False
        else:
            if _lossless_rgb_edge_kind(packet) is None:
                return False
    return _h264_damage_groups_valid(updates)


def adaptive_h264_production_updates(
    updates: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Validate adaptive groups and return the final exact H.264 production phase."""
    if not isinstance(updates, dict) or updates.get("initial_pixel_format") not in {
        "BGRA",
        "BGRX",
        "RGBA",
        "RGBX",
    }:
        return None
    packets = updates.get("updates")
    count = _exact_int(updates.get("count"), positive=True)
    encodings = updates.get("encodings")
    window_id = _exact_int(updates.get("window_id"), positive=True)
    if (
        not isinstance(packets, list)
        or count != len(packets)
        or not isinstance(encodings, list)
        or not all(isinstance(encoding, str) for encoding in encodings)
        or len(encodings) != len(set(encodings))
        or not all(isinstance(packet, dict) for packet in packets)
        or not all(isinstance(packet.get("encoding"), str) for packet in packets)
        or window_id is None
    ):
        return None
    actual_encodings = {str(packet["encoding"]) for packet in packets}
    if set(encodings) != actual_encodings:
        return None
    sequences = [
        _exact_int(packet.get("sequence"), positive=True) for packet in packets
    ]
    if any(sequence is None for sequence in sequences):
        return None
    exact_sequences = [int(sequence) for sequence in sequences if sequence is not None]
    if exact_sequences != list(
        range(exact_sequences[0], exact_sequences[0] + len(exact_sequences))
    ):
        return None
    groups = _ordered_saved_damage_groups(packets, window_id)
    if groups is None:
        return None
    h264_groups: list[list[dict[str, Any]]] = []
    for group in groups:
        group_encodings = {str(packet["encoding"]) for packet in group}
        if group_encodings <= {"webp", "rgb32"}:
            if not _alpha_safe_warmup_groups_valid(group, window_id):
                return None
            continue
        if not _h264_readiness_group_valid(group):
            return None
        h264_groups.append(group)
    production_groups: list[list[dict[str, Any]]] | None = None
    for start in range(len(h264_groups)):
        candidate = h264_groups[start:]
        if _h264_packet_groups_valid(candidate):
            production_groups = candidate
            break
    if not production_groups:
        return None
    production_packets = [
        packet for group in production_groups for packet in group
    ]
    return {
        **updates,
        "count": len(production_packets),
        "encodings": sorted(
            {str(packet["encoding"]) for packet in production_packets}
        ),
        "updates": production_packets,
    }


def zed_h264_stimulus_updates(
    updates: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return the exact stable-geometry packet interval owned by Zed input."""
    if not isinstance(updates, dict):
        return None
    interval = updates.get("h264_stimulus")
    packets = updates.get("updates")
    window_id = _exact_int(updates.get("window_id"), positive=True)
    if (
        not isinstance(interval, dict)
        or not isinstance(packets, list)
        or not all(isinstance(packet, dict) for packet in packets)
        or window_id is None
    ):
        return None
    baseline = _exact_int(interval.get("baseline_sequence"))
    last_sequence = _exact_int(interval.get("last_sequence"), positive=True)
    window_size_value = interval.get("window_size")
    if (
        baseline is None
        or baseline < 0
        or last_sequence is None
        or last_sequence <= baseline
        or not isinstance(window_size_value, (tuple, list))
        or len(window_size_value) != 2
    ):
        return None
    window_width = _exact_int(window_size_value[0], positive=True)
    window_height = _exact_int(window_size_value[1], positive=True)
    if window_width is None or window_height is None:
        return None
    selected = [
        packet
        for packet in packets
        if (
            (sequence := _exact_int(packet.get("sequence"), positive=True))
            is not None
            and baseline < sequence <= last_sequence
        )
    ]
    sequences = [int(packet["sequence"]) for packet in selected]
    if (
        not selected
        or sequences
        != list(range(baseline + 1, last_sequence + 1))
        or any(
            _packet_window_size(packet) != (window_width, window_height)
            for packet in selected
        )
    ):
        return None
    return {
        **updates,
        "count": len(selected),
        "encodings": sorted({str(packet.get("encoding")) for packet in selected}),
        "updates": selected,
    }


def hardware_h264_stimulus_updates(
    updates: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return the exact stable primary interval recorded before auxiliary exit."""
    if not isinstance(updates, dict):
        return None
    interval = updates.get("h264_stimulus")
    packets = updates.get("updates")
    if (
        not isinstance(interval, dict)
        or not isinstance(packets, list)
        or not all(isinstance(packet, dict) for packet in packets)
    ):
        return None
    first_sequence = _exact_int(interval.get("first_sequence"), positive=True)
    baseline_sequence = _exact_int(
        interval.get("baseline_sequence"), positive=True
    )
    last_sequence = _exact_int(interval.get("last_sequence"), positive=True)
    window_size_value = interval.get("window_size")
    if (
        first_sequence is None
        or baseline_sequence is None
        or last_sequence is None
        or not first_sequence <= baseline_sequence < last_sequence
        or not isinstance(window_size_value, (tuple, list))
        or len(window_size_value) != 2
    ):
        return None
    window_width = _exact_int(window_size_value[0], positive=True)
    window_height = _exact_int(window_size_value[1], positive=True)
    if window_width is None or window_height is None:
        return None
    selected = [
        packet
        for packet in packets
        if (
            (sequence := _exact_int(packet.get("sequence"), positive=True))
            is not None
            and first_sequence <= sequence <= last_sequence
        )
    ]
    sequences = [int(packet["sequence"]) for packet in selected]
    if (
        not selected
        or sequences != list(range(first_sequence, last_sequence + 1))
        or any(
            _packet_window_size(packet) != (window_width, window_height)
            for packet in selected
        )
    ):
        return None
    return {
        **updates,
        "count": len(selected),
        "encodings": sorted({str(packet.get("encoding")) for packet in selected}),
        "updates": selected,
    }


def _hardware_complete_packet_prefix(
    updates: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[list[dict[str, Any]]]] | None:
    """Exclude only a safe shutdown-truncated epilogue after the bound phase."""
    packets = updates.get("updates")
    interval = updates.get("h264_stimulus")
    window_id = _exact_int(updates.get("window_id"), positive=True)
    last_sequence = (
        _exact_int(interval.get("last_sequence"), positive=True)
        if isinstance(interval, dict)
        else None
    )
    if not isinstance(packets, list) or window_id is None or last_sequence is None:
        return None
    for end in range(len(packets), 0, -1):
        prefix = packets[:end]
        groups = _ordered_saved_damage_groups(prefix, window_id)
        if groups is None:
            continue
        tail = packets[end:]
        if any(
            (_exact_int(packet.get("sequence"), positive=True) or 0) <= last_sequence
            or packet.get("encoding") == "h264"
            or _exact_int(packet.get("payload_bytes"), positive=True) is None
            or _packet_geometry(packet) is None
            or _packet_window_size(packet) is None
            or not isinstance(packet.get("options"), dict)
            or _exact_int(packet["options"].get("flush"), positive=True) is None
            or (
                packet.get("encoding") in {"rgb24", "rgb32"}
                and packet["options"].get("rgb_format")
                not in {"BGR", "BGRX", "RGB", "RGBX", "XBGR", "XRGB"}
            )
            or packet.get("encoding") not in {"rgb24", "rgb32", "webp"}
            for packet in tail
        ):
            continue
        if any(
            (geometry := _packet_geometry(packet)) is None
            or (window_size := _packet_window_size(packet)) is None
            or geometry[0] + geometry[2] > window_size[0]
            or geometry[1] + geometry[3] > window_size[1]
            for packet in tail
        ):
            continue
        return prefix, groups
    return None


def hardware_h264_history_valid(updates: dict[str, Any] | None) -> bool:
    """Validate every primary packet, including safe prelude and resize epilogue."""
    if not isinstance(updates, dict):
        return False
    packets = updates.get("updates")
    count = _exact_int(updates.get("count"), positive=True)
    encodings = updates.get("encodings")
    interval = updates.get("h264_stimulus")
    window_id = _exact_int(updates.get("window_id"), positive=True)
    if (
        not isinstance(packets, list)
        or count != len(packets)
        or not isinstance(encodings, list)
        or not all(isinstance(encoding, str) for encoding in encodings)
        or len(encodings) != len(set(encodings))
        or not isinstance(interval, dict)
        or window_id is None
        or not all(isinstance(packet, dict) for packet in packets)
    ):
        return False
    first_sequence = _exact_int(interval.get("first_sequence"), positive=True)
    if first_sequence is None:
        return False
    sequences = [
        _exact_int(packet.get("sequence"), positive=True) for packet in packets
    ]
    if any(sequence is None for sequence in sequences):
        return False
    exact_sequences = [int(sequence) for sequence in sequences if sequence is not None]
    if (
        exact_sequences
        != list(range(exact_sequences[0], exact_sequences[0] + len(exact_sequences)))
        or set(encodings) != {str(packet.get("encoding")) for packet in packets}
    ):
        return False
    complete = _hardware_complete_packet_prefix(updates)
    if complete is None:
        return False
    _complete_packets, groups = complete
    for group in groups:
        for packet in group:
            geometry = _packet_geometry(packet)
            window_size = _packet_window_size(packet)
            if (
                geometry is None
                or window_size is None
                or _exact_int(packet.get("payload_bytes"), positive=True) is None
                or packet.get("encoding") not in {"h264", "rgb24", "rgb32", "webp"}
            ):
                return False
            x, y, width, height = geometry
            if x + width > window_size[0] or y + height > window_size[1]:
                return False
            if packet.get("encoding") in {"rgb24", "rgb32"}:
                options = packet.get("options")
                if (
                    not isinstance(options, dict)
                    or options.get("rgb_format")
                    not in {
                        "BGR",
                        "BGRA",
                        "BGRX",
                        "RGB",
                        "RGBA",
                        "RGBX",
                        "XBGR",
                        "XRGB",
                    }
                ):
                    return False
        if int(group[-1]["sequence"]) < first_sequence and not (
            all(_alpha_safe_packet(packet) for packet in group)
            or _h264_readiness_group_valid(group)
        ):
            return False
    return True


def hardware_h264_context_updates(
    updates: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return all packets from the bound production IDR through final quiescence."""
    if not hardware_h264_history_valid(updates):
        return None
    assert isinstance(updates, dict)
    interval = updates.get("h264_stimulus")
    complete = _hardware_complete_packet_prefix(updates)
    packets = complete[0] if complete is not None else None
    if not isinstance(interval, dict) or not isinstance(packets, list):
        return None
    first_sequence = _exact_int(interval.get("first_sequence"), positive=True)
    if first_sequence is None or not all(isinstance(packet, dict) for packet in packets):
        return None
    selected = [
        packet
        for packet in packets
        if (
            (sequence := _exact_int(packet.get("sequence"), positive=True))
            is not None
            and sequence >= first_sequence
        )
    ]
    sequences = [int(packet["sequence"]) for packet in selected]
    if (
        not selected
        or sequences != list(range(first_sequence, sequences[-1] + 1))
        or any(
            _exact_int(packet.get("payload_bytes"), positive=True) is None
            or _packet_geometry(packet) is None
            or _packet_window_size(packet) is None
            for packet in selected
        )
        or any(
            packet.get("encoding") not in {"h264", "rgb24", "rgb32", "webp"}
            for packet in selected
        )
    ):
        return None
    return {
        **updates,
        "count": len(selected),
        "encodings": sorted({str(packet.get("encoding")) for packet in selected}),
        "updates": selected,
    }


def hardware_h264_production_updates(
    updates: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Validate and return the exact bound hardware H.264 production interval."""
    if not hardware_h264_history_valid(updates):
        return None
    exact_updates = hardware_h264_stimulus_updates(updates)
    if (
        not isinstance(exact_updates, dict)
        or exact_updates.get("initial_pixel_format") not in {
        "BGRX",
        "RGBX",
        }
    ):
        return None
    packets = exact_updates.get("updates")
    count = _exact_int(exact_updates.get("count"), positive=True)
    encodings = exact_updates.get("encodings")
    if (
        not isinstance(packets, list)
        or count != len(packets)
        or not isinstance(encodings, list)
        or not all(isinstance(encoding, str) for encoding in encodings)
        or len(encodings) != len(set(encodings))
        or not all(isinstance(packet, dict) for packet in packets)
        or not all(isinstance(packet.get("encoding"), str) for packet in packets)
    ):
        return None
    actual_encodings = {str(packet["encoding"]) for packet in packets}
    if set(encodings) != actual_encodings:
        return None
    sequences = [
        _exact_int(packet.get("sequence"), positive=True) for packet in packets
    ]
    if any(sequence is None for sequence in sequences):
        return None
    exact_sequences = [int(sequence) for sequence in sequences if sequence is not None]
    if exact_sequences != list(
        range(exact_sequences[0], exact_sequences[0] + len(exact_sequences))
    ):
        return None
    window_id = _exact_int(exact_updates.get("window_id"), positive=True)
    if window_id is None:
        return None
    groups = _ordered_saved_damage_groups(packets, window_id)
    if groups is None:
        return None
    first_h264_group = next(
        (
            index for index, group in enumerate(groups)
            if any(packet.get("encoding") == "h264" for packet in group)
        ),
        None,
    )
    if first_h264_group is None:
        return None
    warmup_packets = [
        packet for group in groups[:first_h264_group] for packet in group
    ]
    if not _alpha_safe_warmup_groups_valid(warmup_packets, window_id):
        return None
    production_packets = [
        packet for group in groups[first_h264_group:] for packet in group
    ]
    production = {
        **exact_updates,
        "count": len(production_packets),
        "encodings": sorted(
            {str(packet["encoding"]) for packet in production_packets}
        ),
        "updates": production_packets,
    }
    return production if h264_with_lossless_rgb_edges(production) else None


def hardware_h264_phase_start_sequence(
    updates: dict[str, Any] | None,
    window_size: tuple[int, int],
) -> int | None:
    """Find the active stable-geometry IDR group before recording a phase."""
    if not isinstance(updates, dict):
        return None
    packets = updates.get("updates")
    window_id = _exact_int(updates.get("window_id"), positive=True)
    if (
        not isinstance(packets, list)
        or not packets
        or not all(isinstance(packet, dict) for packet in packets)
        or window_id is None
    ):
        return None
    groups = _ordered_saved_damage_groups(packets, window_id)
    if groups is None:
        return None
    for index in range(len(groups) - 1, -1, -1):
        group = groups[index]
        terminal = group[-1]
        options = terminal.get("options")
        if (
            terminal.get("encoding") != "h264"
            or not isinstance(options, dict)
            or options.get("frame") != 0
            or options.get("type") != "IDR"
        ):
            continue
        production_groups = groups[index:]
        if (
            all(
                _packet_window_size(packet) == window_size
                for candidate in production_groups
                for packet in candidate
            )
            and _h264_packet_groups_valid(production_groups)
        ):
            return int(group[0]["sequence"])
    return None


def begin_hardware_h264_stimulus(
    server: str,
    directory: Path,
    xpra_wid: int,
) -> dict[str, Any]:
    """Bind the already-running stable primary stream before auxiliary input."""
    interval: dict[str, Any] | None = None

    def stable_phase_ready() -> bool:
        nonlocal interval
        updates = synchronize_saved_updates(server, directory, xpra_wid)
        packets = updates.get("updates")
        if (
            not isinstance(packets, list)
            or not packets
            or not isinstance(packets[-1], dict)
        ):
            return False
        window_size = _packet_window_size(packets[-1])
        if window_size is None:
            return False
        first_sequence = hardware_h264_phase_start_sequence(updates, window_size)
        sequences = [
            _exact_int(packet.get("sequence"), positive=True)
            for packet in packets
            if isinstance(packet, dict)
        ]
        if (
            first_sequence is None
            or not sequences
            or any(sequence is None for sequence in sequences)
        ):
            return False
        baseline_sequence = max(
            int(sequence) for sequence in sequences if sequence is not None
        )
        if baseline_sequence <= first_sequence:
            return False
        interval = {
            "baseline_sequence": baseline_sequence,
            "first_sequence": first_sequence,
            "window_size": list(window_size),
        }
        return True

    wait_for("stable hardware H.264 phase baseline", stable_phase_ready, timeout=15)
    assert interval is not None
    return interval


def finish_hardware_h264_stimulus(
    server: str,
    directory: Path,
    xpra_wid: int,
    interval: dict[str, Any],
) -> dict[str, Any]:
    """Close the exact primary interval while the auxiliary window is alive."""
    completed: dict[str, Any] | None = None

    def sustained_phase_ready() -> bool:
        nonlocal completed
        updates = synchronize_saved_updates(server, directory, xpra_wid)
        sequences = [
            _exact_int(packet.get("sequence"), positive=True)
            for packet in updates.get("updates", [])
            if isinstance(packet, dict)
        ]
        if not sequences or any(sequence is None for sequence in sequences):
            return False
        candidate = {
            **interval,
            "last_sequence": max(
                int(sequence) for sequence in sequences if sequence is not None
            ),
        }
        updates["h264_stimulus"] = candidate
        metrics = h264_production_metrics("hardware", updates)
        checks = h264_dominance_checks(metrics)
        if not all(checks.values()):
            return False
        completed = {
            **candidate,
            "dominance_checks": checks,
            "metrics": metrics,
        }
        return True

    wait_for("sustained dominant hardware H.264 phase", sustained_phase_ready, timeout=15)
    assert completed is not None
    return completed


def empty_h264_production_metrics() -> dict[str, Any]:
    return {
        "aggregate_encoded_pixels": 0,
        "aggregate_h264_pixel_ratio": 0.0,
        "h264_damage_span_ms": 0,
        "h264_main_frame_count": 0,
        "h264_main_pixels": 0,
        "minimum_frame_h264_pixels": 0,
        "minimum_frame_window_pixels": 0,
    }


def h264_production_metrics(
    application: str,
    updates: dict[str, Any] | None,
) -> dict[str, Any]:
    """Measure dominant H.264 regions over one validated adaptive sequence."""
    if application in MULTIWINDOW_HARDWARE_APPLICATIONS:
        exact_updates = hardware_h264_stimulus_updates(updates)
    elif application == "zed":
        exact_updates = zed_h264_stimulus_updates(updates)
    else:
        exact_updates = updates
    production = (
        hardware_h264_production_updates(exact_updates)
        if application in MULTIWINDOW_HARDWARE_APPLICATIONS
        else adaptive_h264_production_updates(exact_updates)
    )
    if production is None or not isinstance(exact_updates, dict):
        return empty_h264_production_metrics()
    coverage: list[tuple[int, int]] = []
    h264_damage_times: list[int] = []
    window_id = _exact_int(exact_updates.get("window_id"), positive=True)
    if window_id is None:
        return empty_h264_production_metrics()
    for packet in production["updates"]:
        if packet.get("encoding") != "h264":
            continue
        main_size = _h264_main_size(packet)
        window_size = _packet_window_size(packet)
        location = _saved_update_group_location(packet, window_id)
        if main_size is None or window_size is None or location is None:
            return empty_h264_production_metrics()
        coverage.append(
            (main_size[0] * main_size[1], window_size[0] * window_size[1])
        )
        h264_damage_times.append(int(location[0]))
    if not coverage:
        return empty_h264_production_metrics()
    aggregate_encoded_pixels = 0
    for packet in production["updates"]:
        geometry = _packet_geometry(packet)
        if geometry is None:
            return empty_h264_production_metrics()
        aggregate_encoded_pixels += geometry[2] * geometry[3]
    minimum_main, minimum_window = coverage[0]
    for main_pixels, window_pixels in coverage[1:]:
        if main_pixels * minimum_window < minimum_main * window_pixels:
            minimum_main, minimum_window = main_pixels, window_pixels
    h264_main_pixels = sum(main_pixels for main_pixels, _window_pixels in coverage)
    return {
        "aggregate_encoded_pixels": aggregate_encoded_pixels,
        "aggregate_h264_pixel_ratio": (
            h264_main_pixels / aggregate_encoded_pixels
            if aggregate_encoded_pixels
            else 0.0
        ),
        "h264_damage_span_ms": max(h264_damage_times) - min(h264_damage_times),
        "h264_main_frame_count": len(coverage),
        "h264_main_pixels": h264_main_pixels,
        "minimum_frame_h264_pixels": minimum_main,
        "minimum_frame_window_pixels": minimum_window,
    }


def h264_dominance_checks(metrics: dict[str, Any]) -> dict[str, bool]:
    """Require sustained, predominant H.264 coverage over the observed interval."""
    frames = _exact_int(metrics.get("h264_main_frame_count"), positive=True)
    main_pixels = _exact_int(metrics.get("minimum_frame_h264_pixels"), positive=True)
    window_pixels = _exact_int(
        metrics.get("minimum_frame_window_pixels"), positive=True
    )
    aggregate_h264_pixels = _exact_int(
        metrics.get("h264_main_pixels"), positive=True
    )
    aggregate_encoded_pixels = _exact_int(
        metrics.get("aggregate_encoded_pixels"), positive=True
    )
    damage_span_ms = _exact_int(metrics.get("h264_damage_span_ms"))
    return {
        "primary_h264_aggregate_pixels_dominant": bool(
            aggregate_h264_pixels is not None
            and aggregate_encoded_pixels is not None
            and aggregate_h264_pixels <= aggregate_encoded_pixels
            and aggregate_h264_pixels * 100
            >= aggregate_encoded_pixels * H264_MIN_AGGREGATE_PIXEL_PERCENT
        ),
        "primary_h264_damage_span_stable": bool(
            damage_span_ms is not None and damage_span_ms >= H264_MIN_DAMAGE_SPAN_MS
        ),
        "primary_h264_main_frames_stable": bool(
            frames is not None and frames >= H264_MIN_MAIN_FRAMES
        ),
        "primary_h264_per_frame_pixels_dominant": bool(
            main_pixels is not None
            and window_pixels is not None
            and main_pixels <= window_pixels
            and main_pixels * 100
            >= window_pixels * H264_MIN_FRAME_PIXEL_PERCENT
        ),
    }


def matched_h264_stream_stability_checks(
    production: dict[str, Any],
    metrics: dict[str, Any],
) -> dict[str, bool]:
    """Bind stability and dominance to the exact VA-matched H.264 stream."""
    matched = production.get("matched_stream")
    if not isinstance(matched, dict):
        matched = {}
    packet_count = _exact_int(matched.get("packet_count"), positive=True)
    damage_span_ms = _exact_int(matched.get("damage_span_ms"))
    matched_pixels = _exact_int(matched.get("pixel_count"), positive=True)
    total_h264_pixels = _exact_int(metrics.get("h264_main_pixels"), positive=True)
    return {
        "matched_h264_stream_damage_span_stable": bool(
            damage_span_ms is not None
            and (_exact_int(metrics.get("h264_damage_span_ms")) is not None)
            and damage_span_ms >= int(metrics["h264_damage_span_ms"])
        ),
        "matched_h264_stream_frames_stable": bool(
            packet_count is not None
            and (_exact_int(metrics.get("h264_main_frame_count"), positive=True)
                 is not None)
            and packet_count >= int(metrics["h264_main_frame_count"])
        ),
        "matched_h264_stream_pixels_dominant": bool(
            matched_pixels is not None
            and total_h264_pixels is not None
            and matched_pixels >= total_h264_pixels
        ),
    }


def primary_h264_packets_valid(
    application: str,
    h264_client_policy: str,
    updates: dict[str, Any] | None,
) -> bool:
    """Apply the exact primary-window codec contract for one live profile."""
    if h264_client_policy == "adaptive-alpha":
        metrics = h264_production_metrics(application, updates)
        return all(h264_dominance_checks(metrics).values())
    return only_positive_h264_packets(updates)


def primary_h264_frame_ready(
    application: str,
    h264_client_policy: str,
    updates: dict[str, Any] | None,
) -> bool:
    """Accept one structurally valid H.264 group for interaction readiness."""
    if h264_client_policy == "adaptive-alpha":
        return adaptive_h264_frame_readiness_valid(application, updates)
    return only_positive_h264_packets(updates)


def primary_h264_packet_contract_name(
    application: str,
    h264_client_policy: str,
) -> str:
    if application in MULTIWINDOW_HARDWARE_APPLICATIONS:
        return "alpha_safe_warmup_then_h264_with_only_lossless_rgb_edges"
    if h264_client_policy == "adaptive-alpha":
        return "adaptive_alpha_groups_with_dominant_h264_and_only_lossless_rgb_edges"
    return "only_h264_packets"


FRAME_ALPHA_STATE_RE = re.compile(
    r"(?:^|\s)window 0x(?P<window_id>[0-9a-fA-F]+) "
    r"frame pixel format=(?P<pixel_format>[A-Za-z0-9]+), "
    r"want-alpha=(?P<want_alpha>True|False)\s*$"
)
SAVED_PACKET_RE = re.compile(
    r"(?:^|\s)saved\s+(?P<encoding>[a-z0-9]+)\s*:\s*"
    r"[1-9][0-9]*\s+bytes to '/artifacts/"
    r"(?P<payload>screen-updates/(?P<window_id>[1-9][0-9]*)/"
    r"(?P<group>0|[1-9][0-9]*)/(?P<index>0|[1-9][0-9]*)\."
    r"(?P=encoding))'\s*$"
)


def parse_frame_alpha_states(server_log: str) -> list[dict[str, Any]]:
    """Parse exact per-window frame alpha transitions from the server log."""
    states: list[dict[str, Any]] = []
    for line in server_log.splitlines():
        match = FRAME_ALPHA_STATE_RE.search(line)
        if match is None:
            continue
        states.append(
            {
                "pixel_format": match.group("pixel_format"),
                "want_alpha": match.group("want_alpha") == "True",
                "window_id": int(match.group("window_id"), 16),
            }
        )
    return states


def parse_saved_packet_frame_states(server_log: str) -> list[dict[str, Any]]:
    """Bind each exact saved packet path to the latest state for its window."""
    current: dict[int, dict[str, Any]] = {}
    packets: list[dict[str, Any]] = []
    for line in server_log.splitlines():
        state_match = FRAME_ALPHA_STATE_RE.search(line)
        if state_match is not None:
            window_id = int(state_match.group("window_id"), 16)
            current[window_id] = {
                "pixel_format": state_match.group("pixel_format"),
                "want_alpha": state_match.group("want_alpha") == "True",
            }
            continue
        packet_match = SAVED_PACKET_RE.search(line)
        if packet_match is None:
            continue
        window_id = int(packet_match.group("window_id"))
        payload = PurePosixPath(packet_match.group("payload"))
        state = current.get(window_id, {})
        packets.append(
            {
                "encoding": packet_match.group("encoding"),
                "pixel_format": state.get("pixel_format"),
                "relative_info": payload.with_suffix(".info").as_posix(),
                "want_alpha": state.get("want_alpha"),
                "window_id": window_id,
            }
        )
    return packets


def exact_window_frame_alpha_states(
    states: Any,
    window_id: Any,
    *,
    pixel_formats: set[str],
    want_alpha: bool,
) -> bool:
    """Validate every recorded alpha transition for one exact Xpra window."""
    exact_window_id = _exact_int(window_id, positive=True)
    if not isinstance(states, list) or exact_window_id is None or not pixel_formats:
        return False
    matching: list[dict[str, Any]] = []
    for state in states:
        if not isinstance(state, dict):
            return False
        state_window_id = _exact_int(state.get("window_id"), positive=True)
        pixel_format = state.get("pixel_format")
        state_want_alpha = state.get("want_alpha")
        if (
            state_window_id is None
            or not isinstance(pixel_format, str)
            or not isinstance(state_want_alpha, bool)
        ):
            return False
        if state_window_id == exact_window_id:
            matching.append(state)
    return bool(matching) and all(
        state["pixel_format"] in pixel_formats
        and state["want_alpha"] is want_alpha
        for state in matching
    )


def hardware_frame_alpha_state_checks(
    log_evidence: dict[str, Any],
    updates: dict[str, Any],
    interaction_updates: dict[str, Any] | None,
) -> dict[str, bool]:
    """Bind opaque and alpha frame transitions to the two hardware windows."""
    interaction = interaction_updates if isinstance(interaction_updates, dict) else {}
    states = log_evidence.get("frame_alpha_states")
    return {
        "primary_window_opaque_frame_states": exact_window_frame_alpha_states(
            states,
            updates.get("window_id"),
            pixel_formats={"BGRX", "RGBX"},
            want_alpha=False,
        ),
        "interaction_window_alpha_frame_states": exact_window_frame_alpha_states(
            states,
            interaction.get("window_id"),
            pixel_formats={"BGRA", "RGBA"},
            want_alpha=True,
        ),
    }


def adaptive_frame_alpha_state_checks(
    log_evidence: dict[str, Any],
    updates: dict[str, Any],
) -> dict[str, bool]:
    """Bind adaptive alpha codecs to consistent transitions for one exact window."""
    states = log_evidence.get("frame_alpha_states")
    window_id = _exact_int(updates.get("window_id"), positive=True)
    if not isinstance(states, list) or window_id is None:
        matching: list[dict[str, Any]] = []
        structurally_valid = False
    else:
        matching = []
        structurally_valid = True
        for state in states:
            if not isinstance(state, dict):
                structurally_valid = False
                break
            state_window_id = _exact_int(state.get("window_id"), positive=True)
            if (
                state_window_id is None
                or not isinstance(state.get("pixel_format"), str)
                or not isinstance(state.get("want_alpha"), bool)
            ):
                structurally_valid = False
                break
            if state_window_id == window_id:
                matching.append(state)
    transitions_consistent = bool(matching) and structurally_valid and all(
        (
            state["pixel_format"] in {"BGRX", "RGBX"}
            and state["want_alpha"] is False
        )
        or (
            state["pixel_format"] in {"BGRA", "RGBA"}
            and state["want_alpha"] is True
        )
        for state in matching
    )
    packets = updates.get("updates")
    exact_packets = packets if isinstance(packets, list) else []
    state_records = log_evidence.get("saved_packet_frame_states")
    state_by_path: dict[str, dict[str, Any]] = {}
    records_valid = isinstance(state_records, list)
    if records_valid:
        for record in state_records:
            if not isinstance(record, dict):
                records_valid = False
                break
            relative = record.get("relative_info")
            record_window = _exact_int(record.get("window_id"), positive=True)
            if (
                not isinstance(relative, str)
                or not relative
                or record_window is None
                or relative in state_by_path
                or not isinstance(record.get("encoding"), str)
                or not isinstance(record.get("pixel_format"), str)
                or not isinstance(record.get("want_alpha"), bool)
            ):
                records_valid = False
                break
            state_by_path[relative] = record
    packets_bound = bool(exact_packets) and records_valid
    h264_states_valid = packets_bound
    alpha_rgb32_states_valid = packets_bound
    for packet in exact_packets:
        if not isinstance(packet, dict):
            packets_bound = False
            h264_states_valid = False
            alpha_rgb32_states_valid = False
            break
        relative = packet.get("relative_info")
        record = state_by_path.get(relative) if isinstance(relative, str) else None
        if (
            record is None
            or record.get("window_id") != window_id
            or record.get("encoding") != packet.get("encoding")
        ):
            packets_bound = False
            h264_states_valid = False
            alpha_rgb32_states_valid = False
            continue
        opaque = (
            record["pixel_format"] in {"BGRX", "RGBX"}
            and record["want_alpha"] is False
        )
        alpha = (
            record["pixel_format"] in {"BGRA", "RGBA"}
            and record["want_alpha"] is True
        )
        encoding = packet.get("encoding")
        if encoding == "h264" and not opaque:
            h264_states_valid = False
            packets_bound = False
        if encoding == "rgb24" and not opaque:
            packets_bound = False
        if encoding == "rgb32":
            options = packet.get("options")
            rgb_format = options.get("rgb_format") if isinstance(options, dict) else None
            if (
                rgb_format in {"BGRA", "RGBA"} and not alpha
                or rgb_format not in {"BGRA", "RGBA"} and not opaque
            ):
                alpha_rgb32_states_valid = False
                packets_bound = False
    return {
        "primary_alpha_rgb32_packets_have_alpha_frame_state": bool(
            transitions_consistent and alpha_rgb32_states_valid
        ),
        "primary_h264_packets_have_opaque_frame_state": bool(
            transitions_consistent and h264_states_valid
        ),
        "primary_packets_bound_to_frame_state": bool(
            transitions_consistent and packets_bound
        ),
        "primary_window_frame_alpha_states_consistent": transitions_consistent,
    }


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
        "frame_alpha_states": parse_frame_alpha_states(server_log),
        "saved_packet_frame_states": parse_saved_packet_frame_states(server_log),
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
    allow_alpha_gaps: bool = False,
    allow_lossless_rgb_edges: bool = False,
    allow_window_resize_gaps: bool = False,
) -> list[dict[str, Any]]:
    alpha_mode = allow_alpha_gaps
    edge_mode = allow_lossless_rgb_edges
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
        if window_size is None:
            return False
        if (
            _packet_window_size(previous) != window_size
            and not allow_window_resize_gaps
        ):
            return False
        return all(
            intermediate in packets_by_sequence
            and (
                _lossless_rgb_edge_kind(packets_by_sequence[intermediate]) is not None
                or alpha_mode
                and _alpha_safe_packet(packets_by_sequence[intermediate])
                or allow_window_resize_gaps
                and _safe_h264_context_gap(packets_by_sequence[intermediate])
            )
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
    window_id = _exact_int(updates.get("window_id"), positive=True)
    for packets_in_stream in grouped:
        first = packets_in_stream[0]
        sequences = [int(packet["sequence"]) for packet in packets_in_stream]
        frames = [
            packet.get("options", {}).get("frame") for packet in packets_in_stream
        ]
        width = int(first["w"])
        height = int(first["h"])
        encoded_width, encoded_height = h264_encoded_size(first)
        damage_times: list[int] = []
        if window_id is not None:
            for packet in packets_in_stream:
                location = _saved_update_group_location(packet, window_id)
                if location is None:
                    damage_times = []
                    break
                damage_times.append(int(location[0]))
        transport_sequences = list(range(sequences[0], sequences[-1] + 1))
        interleaved_edge_sequences = [
            sequence
            for sequence in transport_sequences
            if sequence not in sequences
            and sequence in packets_by_sequence
            and _lossless_rgb_edge_kind(packets_by_sequence[sequence]) is not None
        ]
        interleaved_alpha_sequences = [
            sequence
            for sequence in transport_sequences
            if sequence not in sequences
            and sequence in packets_by_sequence
            and _alpha_safe_packet(packets_by_sequence[sequence])
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
                "damage_span_ms": (
                    max(damage_times) - min(damage_times) if damage_times else 0
                ),
                "first_sequence": sequences[0],
                "interleaved_alpha_sequences": interleaved_alpha_sequences,
                "interleaved_edge_sequences": interleaved_edge_sequences,
                "last_sequence": sequences[-1],
                "packet_count": len(packets_in_stream),
                "pixel_count": len(packets_in_stream) * width * height,
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


def _eligible_va_contexts(
    contexts: list[dict[str, Any]],
    surface_size: tuple[int, int],
    entrypoints: set[str],
) -> list[dict[str, Any]]:
    width, height = surface_size
    return [
        context
        for context in contexts
        if context["created"]
        and context["profile"].startswith("VAProfileH264")
        and context["entrypoint"] in entrypoints
        and [context["width"], context["height"]] == [width, height]
        and context["incomplete_frames"] == 0
    ]


def match_h264_production_stream(
    updates: dict[str, Any],
    server_trace: dict[str, Any],
    client_trace: dict[str, Any],
    *,
    allow_alpha_gaps: bool = False,
    allow_lossless_rgb_edges: bool = False,
    allow_terminal_client_frame: bool = False,
    allow_terminal_server_frame: bool = False,
    allow_window_resize_gaps: bool = False,
) -> dict[str, Any]:
    streams = h264_packet_streams(
        updates,
        allow_alpha_gaps=allow_alpha_gaps,
        allow_lossless_rgb_edges=allow_lossless_rgb_edges,
        allow_window_resize_gaps=allow_window_resize_gaps,
    )
    candidates = [
        {
            **stream,
            "client_contexts": [],
            "complete": False,
            "server_contexts": [],
            "structurally_complete": bool(
                stream["contiguous_frames"]
                and stream["contiguous_sequences"]
                and stream["positive_payloads"]
                and stream["starts_with_idr"]
            ),
        }
        for stream in streams
    ]
    candidates_by_size: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for candidate in candidates:
        surface_size = tuple(int(value) for value in candidate["surface_size"])
        candidates_by_size.setdefault(surface_size, []).append(candidate)
    for surface_size, grouped_candidates in candidates_by_size.items():
        server_matches = _eligible_va_contexts(
            server_trace["contexts"],
            surface_size,
            {"VAEntrypointEncSlice", "VAEntrypointEncSliceLP"},
        )
        client_matches = _eligible_va_contexts(
            client_trace["contexts"],
            surface_size,
            {"VAEntrypointVLD"},
        )
        expected_frames = sum(
            int(candidate["packet_count"]) for candidate in grouped_candidates
        )
        server_frames = sum(
            int(context["completed_frames"]) for context in server_matches
        )
        client_frames = sum(
            int(context["completed_frames"]) for context in client_matches
        )
        server_frames_match = server_frames == expected_frames
        if allow_terminal_server_frame:
            server_frames_match = server_frames in {
                expected_frames,
                expected_frames + 1,
            }
        client_frames_match = client_frames == expected_frames
        if allow_terminal_client_frame:
            client_frames_match = client_frames in {
                expected_frames - 1,
                expected_frames,
            }
        group_complete = bool(
            all(candidate["structurally_complete"] for candidate in grouped_candidates)
            and server_matches
            and client_matches
            and server_frames_match
            and client_frames_match
        )
        for candidate in grouped_candidates:
            candidate["client_contexts"] = client_matches
            candidate["client_completed_frames"] = client_frames
            candidate["complete"] = group_complete
            candidate["expected_packet_frames"] = expected_frames
            candidate["server_contexts"] = server_matches
            candidate["server_completed_frames"] = server_frames
            candidate["terminal_server_frame_untransmitted"] = bool(
                allow_terminal_server_frame
                and server_frames == expected_frames + 1
            )
            candidate["terminal_client_frame_inflight"] = bool(
                allow_terminal_client_frame
                and client_frames == expected_frames - 1
            )
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
    for candidate in complete:
        production_keys.update(
            _context_key(context)
            for context in (
                *candidate["server_contexts"],
                *candidate["client_contexts"],
            )
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
        "all_streams_proven": bool(candidates)
        and len(complete) == len(candidates),
        "candidates": candidates,
        "complete_streams": complete,
        "matched_stream": selected,
        "production_proven": selected is not None,
        "selftest_contexts": selftest_contexts,
        "unmatched_contexts": unmatched_contexts,
    }


def terminal_client_h264_frame_inflight(
    directory: Path,
    updates: dict[str, Any],
) -> bool:
    """Prove one received post-phase terminal frame may die with the client."""
    packets = updates.get("updates")
    interval = updates.get("h264_stimulus")
    window_id = _exact_int(updates.get("window_id"), positive=True)
    last_phase_sequence = (
        _exact_int(interval.get("last_sequence"), positive=True)
        if isinstance(interval, dict)
        else None
    )
    if (
        not isinstance(packets, list)
        or not packets
        or not all(isinstance(packet, dict) for packet in packets)
        or window_id is None
        or last_phase_sequence is None
    ):
        return False
    saved = [
        (int(packet["sequence"]), str(packet.get("encoding")))
        for packet in packets
        if _exact_int(packet.get("sequence"), positive=True) is not None
    ]
    terminal = packets[-1]
    terminal_sequence = _exact_int(terminal.get("sequence"), positive=True)
    if (
        len(saved) != len(packets)
        or terminal.get("encoding") != "h264"
        or terminal_sequence is None
        or terminal_sequence <= last_phase_sequence
        or _exact_int(terminal.get("payload_bytes"), positive=True) is None
    ):
        return False
    log_path = directory / "client.stdout"
    if not log_path.is_file():
        return False
    client_log = log_path.read_text(encoding="utf-8", errors="replace")
    received = [
        (int(match.group("sequence")), match.group("encoding"))
        for match in H264_PROCESS_DRAW_RE.finditer(client_log)
        if int(match.group("window_id")) == window_id
    ]
    return received == saved


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
    next_process = next(
        (
            match
            for match in H264_PROCESS_DRAW_RE.finditer(
                client_log,
                process_match.end(),
            )
            if int(match.group("window_id")) == window_id
        ),
        None,
    )
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
    next_ack = (
        next(
            (
                match
                for match in H264_ACK_RE.finditer(client_log, ack_match.end())
                if int(match.group("window_id")) == window_id
            ),
            None,
        )
        if ack_match
        else None
    )
    unambiguous_presentation = bool(
        present_position >= 0
        and (next_ack is None or present_position < next_ack.start())
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
        "presented_before_later_ack": presentation_complete,
        "process_draw_matches_saved_packet": packet_fields_match,
        "sequence": sequence,
        "size": [width, height],
        "window_id": window_id,
    }


def h264_hardware_evidence(
    directory: Path,
    updates: dict[str, Any],
    *,
    allow_alpha_gaps: bool = False,
    allow_lossless_rgb_edges: bool = False,
    allow_terminal_server_frame: bool = False,
    allow_window_resize_gaps: bool = False,
) -> dict[str, Any]:
    server_trace = parse_va_contexts(directory, "server-va")
    client_trace = parse_va_contexts(directory, "client-va")
    production = match_h264_production_stream(
        updates,
        server_trace,
        client_trace,
        allow_alpha_gaps=allow_alpha_gaps,
        allow_lossless_rgb_edges=allow_lossless_rgb_edges,
        allow_terminal_client_frame=(
            allow_terminal_server_frame
            and terminal_client_h264_frame_inflight(directory, updates)
        ),
        allow_terminal_server_frame=allow_terminal_server_frame,
        allow_window_resize_gaps=allow_window_resize_gaps,
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
            "entrypoint_present": bool(contexts),
            "files": trace["files"],
            "h264_profile_present": bool(
                contexts
                and all(
                    item["profile"].startswith("VAProfileH264")
                    for item in contexts
                )
            ),
            "production_context": context,
            "production_contexts": contexts,
            "production_dimensions": [
                list(size)
                for size in sorted(
                    {
                        (int(item["width"]), int(item["height"]))
                        for item in contexts
                    }
                )
            ],
            "submitted_frames": sum(
                int(item["completed_frames"]) for item in contexts
            ),
        }

    return {
        "client": side_summary(client_trace, "client"),
        "packet_chain": packet_chain,
        "production": production,
        "server": side_summary(server_trace, "server"),
    }


def application_contract(application: str) -> tuple[str, tuple[str, ...], str]:
    if application == "zed":
        return "/opt/xpra-fork-maintenance/start_zed.sh", ("empty project", "zed"), "zed.pid"
    if application == "keyboard":
        return (
            "/opt/xpra-fork-maintenance/start_wayland_keyboard_fixture.sh",
            (KEYBOARD_FIXTURE_TITLE,),
            "keyboard-fixture.pid",
        )
    if application in MULTIWINDOW_HARDWARE_APPLICATIONS:
        fixture = hardware_fixture_spec(application)
        return fixture.command, fixture.title_patterns, fixture.pid_file
    if application == "vkcube":
        return (
            "vkcube --wsi wayland --width 640 --height 480 --suppress_popups",
            ("vkcube",),
            "",
        )
    return (
        f"python3 {INTERACTION_FIXTURE_SCRIPT}",
        ("xpra hardware interaction ready",),
        INTERACTION_IDENTITY_ARTIFACT,
    )


def wait_for_frame_boundary(
    server: str,
    server_pid: int,
    client: str,
    client_pid: int,
    directory: Path,
    encoding: str,
    h264_client_policy: str,
    *,
    application: str,
    expected_xpra_wid: int,
) -> str:
    if _exact_int(expected_xpra_wid, positive=True) is None:
        raise LabFailure(f"invalid expected Xpra window ID: {expected_xpra_wid!r}")
    outcome = "pending"
    h264_failure_seen_at: float | None = None
    server_offsets = {"server.stderr": 0}
    client_offsets = {"client.stdout": 0, "client.stderr": 0}
    frame_logs = {name: "" for name in (*server_offsets, *client_offsets)}
    frame_log_bytes = dict.fromkeys(frame_logs, 0)
    incomplete_update_info: set[str] = set()
    incomplete_screenshots: set[str] = set()

    def update_logs(container: str, offsets: dict[str, int]) -> None:
        for name, (next_offset, delta) in read_container_log_deltas(
            container,
            offsets,
        ).items():
            delta_bytes = len(delta.encode())
            if frame_log_bytes[name] + delta_bytes > FRAME_LOG_TOTAL_BYTES:
                raise LabFailure(f"container log exceeds frame-poll limit: {name}")
            offsets[name] = next_offset
            frame_log_bytes[name] += delta_bytes
            frame_logs[name] += delta

    def reached() -> bool:
        nonlocal outcome, h264_failure_seen_at
        update_logs(server, server_offsets)
        update_logs(client, client_offsets)
        server_log = frame_logs["server.stderr"]
        client_log = frame_logs["client.stdout"] + frame_logs["client.stderr"]
        nonempty_commit = any(
            "rects=[]" not in line
            for line in server_log.splitlines()
            if re.search(rf"\bcommit wid {expected_xpra_wid}\b", line)
        )
        if encoding == "rgb":
            window_prefix = f"screen-updates/{expected_xpra_wid}/"
            screenshots = tuple(
                relative
                for relative in container_artifact_files(
                    server, "screen-updates", "screenshot.png"
                )
                if relative.startswith(window_prefix)
            )
            screenshots_to_pull = tuple(
                relative
                for relative in screenshots
                if not (directory / relative).is_file()
                or relative in incomplete_screenshots
            )
            if screenshots_to_pull:
                pull_container_artifacts(server, directory, screenshots_to_pull)
            failed = (
                nonempty_commit
                and "no compatible rgb format for 'RGBX'!" in server_log
                and "only: ('BGRX', 'BGRA')" in server_log
            )
            source_ready = False
            incomplete_screenshots.clear()
            for screenshot in directory.glob(
                f"screen-updates/{expected_xpra_wid}/*/screenshot.png"
            ):
                try:
                    if analyze_png(screenshot)["quantized_rgb_colors"] > 32:
                        source_ready = True
                        break
                except (OSError, ValueError):
                    incomplete_screenshots.add(
                        screenshot.relative_to(directory).as_posix()
                    )
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
            window_prefix = f"screen-updates/{expected_xpra_wid}/"
            remote_update_info = tuple(
                relative
                for relative in container_artifact_files(
                    server, "screen-updates", "*.info"
                )
                if relative.startswith(window_prefix)
            )
            update_info_to_pull = tuple(
                relative
                for relative in remote_update_info
                if not (directory / relative).is_file()
                or relative in incomplete_update_info
            )
            if update_info_to_pull:
                pull_container_artifacts(server, directory, update_info_to_pull)
            incomplete_update_info.clear()
            update_payloads: list[str] = []
            for info_path in directory.glob(
                f"screen-updates/{expected_xpra_wid}/*/[0-9]*.info"
            ):
                try:
                    info = json.loads(info_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    incomplete_update_info.add(
                        info_path.relative_to(directory).as_posix()
                    )
                    continue
                if not isinstance(info, dict):
                    raise LabFailure(
                        f"saved update metadata is not an object: {info_path}"
                    )
                payload_name = info.get("file")
                if (
                    not isinstance(payload_name, str)
                    or payload_name in {"", ".", ".."}
                    or PurePosixPath(payload_name).name != payload_name
                ):
                    raise LabFailure(f"saved update payload name is unsafe: {info_path}")
                payload_relative = (
                    info_path.parent / payload_name
                ).relative_to(directory).as_posix()
                if not (directory / payload_relative).is_file():
                    update_payloads.append(payload_relative)
            if update_payloads:
                pull_container_artifacts(
                    server,
                    directory,
                    tuple(sorted(set(update_payloads))),
                )
            try:
                updates = parse_saved_updates(directory, expected_xpra_wid)
                updates["initial_pixel_format"] = saved_window_initial_pixel_format(
                    directory, expected_xpra_wid
                )
            except (LabFailure, OSError, ValueError, json.JSONDecodeError):
                incomplete_update_info.add(
                    f"screen-updates/{expected_xpra_wid}/window.info"
                )
                updates = {"count": 0, "encodings": [], "updates": []}
            if h264_client_policy in H264_FALLBACK_POLICIES and updates["count"] > 0:
                actual_encodings = set(updates["encodings"])
                if actual_encodings and actual_encodings <= {"rgb24", "rgb32"}:
                    outcome = "picture-fallback"
                else:
                    outcome = "unexpected-h264"
                return True
            production_marker = (
                f"register_window(..) window(0x{expected_xpra_wid:x})="
            )
            production_log = (
                client_log[client_log.find(production_marker) :]
                if production_marker in client_log
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
                    rf"wid=0x{expected_xpra_wid:x}, h264:",
                    after_draw,
                )
            )
            presented = bool(
                nonempty_commit
                and primary_h264_frame_ready(
                    application, h264_client_policy, updates
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
    application: str,
    expect_content: bool,
) -> dict[str, Any]:
    evidence: dict[str, Any] | None = None

    def capture() -> bool:
        nonlocal evidence
        capture_xwd(
            container,
            directory,
            "window-direct.xwd",
            window_id=window_id,
            announce=False,
        )
        evidence = convert_xwd(directory, "window-direct")
        return client_window_content_ready(application, evidence)

    if expect_content:
        wait_for("nonuniform source-appropriate client pixels", capture)
    else:
        capture()
    assert evidence is not None
    return evidence


def client_window_content_ready(
    application: str,
    evidence: dict[str, Any],
) -> bool:
    """Apply the reviewed opaque or real-alpha readiness rule."""
    xwd = evidence.get("xwd")
    image = evidence.get("image")
    if not isinstance(xwd, dict) or not isinstance(image, dict):
        return False
    if xwd.get("unique_rgb_colors", 0) <= 100:
        return False
    if application == "gtk":
        return all(image_alpha_content_checks(image, prefix="window").values())
    return image.get("central_opaque_ratio", 0) >= 0.99


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
    server: str,
    client: str,
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

    log_offsets = {
        "client": container_artifact_size(client, "client.stdout"),
        "server": container_artifact_size(server, "server.stderr"),
        "zed": container_artifact_size(server, "zed.stderr"),
    }
    before_screenshots = {
        Path(value)
        for value in container_artifact_files(
            server,
            "screen-updates",
            "screenshot.png",
        )
    }
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

    input_path: dict[str, bool] = {}

    def pointer_path_complete() -> bool:
        nonlocal input_path
        input_path = {
            "client_coordinates": container_artifact_suffix_matches(
                client,
                "client.stdout",
                log_offsets["client"],
                (re.escape(f", {click_x}, {click_y}"),),
            ),
            "client_press_release": container_artifact_suffix_matches(
                client,
                "client.stdout",
                log_offsets["client"],
                (
                    r"_button_action\(1,[^\n]+, True\)",
                    r"_button_action\(1,[^\n]+, False\)",
                ),
            ),
            "server_coordinates": container_artifact_suffix_matches(
                server,
                "server.stderr",
                log_offsets["server"],
                (re.escape(f"move_pointer({click_x}, {click_y},"),),
            ),
            "server_press_release": container_artifact_suffix_matches(
                server,
                "server.stderr",
                log_offsets["server"],
                (
                    re.escape("click(1, True"),
                    re.escape("click(1, False"),
                ),
            ),
            "zed_coordinates": container_artifact_suffix_matches(
                server,
                "zed.stderr",
                log_offsets["zed"],
                (
                    (
                        rf"wl_pointer#\d+\.(?:enter|motion)\([^\n]*"
                        rf"{click_x}\.0+,\s*{click_y}\.0+"
                    ),
                ),
            ),
            "zed_press_release": container_artifact_suffix_matches(
                server,
                "zed.stderr",
                log_offsets["zed"],
                (
                    r"wl_pointer#\d+\.button\([^\n]+,\s*272,\s*1\)",
                    r"wl_pointer#\d+\.button\([^\n]+,\s*272,\s*0\)",
                ),
            ),
        }
        return all(input_path.values())

    wait_for("pointer path from Xpra client to Zed", pointer_path_complete, timeout=10)
    after: dict[str, Any] | None = None

    def theme_changed() -> bool:
        nonlocal after
        capture_xwd(
            client,
            directory,
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
        listed = {
            Path(value) for value in container_artifact_files(
                server, "screen-updates", "screenshot.png"
            )
        }
        new_screenshots = listed - before_screenshots
        if new_screenshots:
            pull_container_artifacts(
                server,
                directory,
                tuple(path.as_posix() for path in sorted(new_screenshots)),
            )
        comparisons: list[dict[str, Any]] = []
        for relative_path in sorted(new_screenshots):
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


def exercise_zed_h264_stability(
    server: str,
    client: str,
    window_id: str,
    xpra_wid: int,
    geometry: dict[str, int],
    directory: Path,
    interaction: dict[str, Any],
) -> dict[str, Any]:
    """Alternate the real Zed theme controls and prove sustained H.264."""

    def target_center(name: str) -> tuple[int, int]:
        target = interaction.get("target")
        bounds = target.get(name) if isinstance(target, dict) else None
        if (
            not isinstance(bounds, list)
            or len(bounds) != 4
            or any(_exact_int(value) is None for value in bounds)
        ):
            raise LabFailure(f"Zed H.264 stimulus has invalid {name}")
        left, top, right, bottom = (int(value) for value in bounds)
        if not (0 <= left < right <= geometry["width"]):
            raise LabFailure(f"Zed H.264 stimulus {name} is outside the window")
        if not (0 <= top < bottom <= geometry["height"]):
            raise LabFailure(f"Zed H.264 stimulus {name} is outside the window")
        return (left + right) // 2, (top + bottom) // 2

    system_position = target_center("system_bounds")
    dark_position = target_center("dark_bounds")
    # Let the initial dark-theme transition and its lossless refresh finish before
    # recording the exact owned production interval.
    time.sleep(2.0)
    baseline_updates = synchronize_saved_updates(server, directory, xpra_wid)
    baseline_sequences = [
        _exact_int(packet.get("sequence"), positive=True)
        for packet in baseline_updates["updates"]
    ]
    if not baseline_sequences or any(value is None for value in baseline_sequences):
        raise LabFailure("Zed H.264 stimulus has no valid baseline sequence")
    baseline_sequence = max(int(value) for value in baseline_sequences if value)
    window_size = [geometry["width"], geometry["height"]]

    capture_xwd(
        client,
        directory,
        "window-h264-theme-baseline.xwd",
        window_id=window_id,
        announce=False,
    )
    baseline = convert_xwd(directory, "window-h264-theme-baseline")
    attempts: list[dict[str, Any]] = []
    final_updates: dict[str, Any] | None = None
    final_metrics: dict[str, Any] | None = None
    final_checks: dict[str, bool] | None = None
    for attempt in range(1, 4):
        light: dict[str, Any] | None = None
        for cycle in range(ZED_THEME_TOGGLE_CYCLES):
            for state, position in (
                ("system", system_position),
                ("dark", dark_position),
            ):
                result = podman_exec(
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
                        str(position[0]),
                        str(position[1]),
                        "click",
                        "1",
                    ],
                    check=False,
                )
                if result.returncode:
                    raise LabFailure(f"Zed {state} theme stimulus failed")
                time.sleep(ZED_THEME_TOGGLE_DELAY)
                if cycle == 0 and state == "system":
                    stem = f"window-h264-theme-{attempt}-light"
                    capture_xwd(
                        client,
                        directory,
                        f"{stem}.xwd",
                        window_id=window_id,
                        announce=False,
                    )
                    light = convert_xwd(directory, stem)
        if light is None:
            raise LabFailure("Zed H.264 stimulus did not capture its light phase")
        dark_stem = f"window-h264-theme-{attempt}-dark"
        capture_xwd(
            client,
            directory,
            f"{dark_stem}.xwd",
            window_id=window_id,
            announce=False,
        )
        dark = convert_xwd(directory, dark_stem)
        updates = synchronize_saved_updates(server, directory, xpra_wid)
        sequences = [
            _exact_int(packet.get("sequence"), positive=True)
            for packet in updates["updates"]
        ]
        if not sequences or any(value is None for value in sequences):
            raise LabFailure("Zed H.264 stimulus produced invalid packet sequences")
        last_sequence = max(int(value) for value in sequences if value)
        updates["h264_stimulus"] = {
            "baseline_sequence": baseline_sequence,
            "last_sequence": last_sequence,
            "window_size": window_size,
        }
        metrics = h264_production_metrics("zed", updates)
        checks = h264_dominance_checks(metrics)
        changed = (
            light["image"]["rgb_sha256"]
            != baseline["image"]["rgb_sha256"]
        )
        attempts.append(
            {
                "checks": checks,
                "client_frame_changed": changed,
                "last_sequence": last_sequence,
                "metrics": metrics,
            }
        )
        if changed and all(checks.values()):
            final_updates = updates
            final_metrics = metrics
            final_checks = checks
            break
    if final_updates is None or final_metrics is None or final_checks is None:
        raise LabFailure("Zed theme toggles did not produce sustained dominant H.264")
    stimulus = final_updates["h264_stimulus"]
    return {
        **stimulus,
        "attempts": attempts,
        "client_frames": {
            "baseline": baseline["image"]["rgb_sha256"],
            "dark": dark["image"]["rgb_sha256"],
            "light": light["image"]["rgb_sha256"],
        },
        "dominance_checks": final_checks,
        "metrics": final_metrics,
        "theme_toggle_cycles_per_attempt": ZED_THEME_TOGGLE_CYCLES,
    }


def load_opengl_evidence(path: Path) -> dict[str, Any]:
    """Parse immutable renderer metadata from quiesced ``glmark2-wayland`` output."""
    ensure_private_regular_file(path)
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise LabFailure("OpenGL fixture renderer evidence is invalid") from error
    if not raw or len(raw) > 8 * 1024 * 1024 or b"\0" in raw:
        raise LabFailure("OpenGL fixture renderer evidence has an invalid size")
    try:
        output = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise LabFailure("OpenGL fixture renderer evidence is not UTF-8") from error
    payload = {"api": "OpenGL", "source": "glmark2-wayland"}
    for key, label in (
        ("renderer", "GL_RENDERER"),
        ("vendor", "GL_VENDOR"),
        ("version", "GL_VERSION"),
    ):
        values = {
            match.strip()
            for match in re.findall(rf"^\s*{label}:\s*(.+?)\s*$", output, re.MULTILINE)
        }
        if len(values) != 1:
            raise LabFailure(f"OpenGL fixture renderer evidence has an invalid {key}")
        value = values.pop()
        if not value or len(value) > 512 or any(ord(character) < 32 for character in value):
            raise LabFailure(f"OpenGL fixture renderer evidence has an invalid {key}")
        payload[key] = value
    return payload


def capture_graphics_motion(
    container: str,
    window_id: str,
    directory: Path,
    initial: dict[str, Any],
) -> dict[str, Any]:
    """Prove that the forwarded primary graphics window contains changing frames."""
    later: dict[str, Any] | None = None

    def changed() -> bool:
        nonlocal later
        capture_xwd(
            container,
            directory,
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
    wait_for("changing nonuniform primary graphics frames", changed, timeout=15)
    assert later is not None
    return {
        "changed": True,
        "first_rgb_sha256": initial["image"]["rgb_sha256"],
        "second": later,
        "second_rgb_sha256": later["image"]["rgb_sha256"],
    }


def parse_setxkbmap_query(output: str) -> dict[str, Any]:
    values: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition(":")
        if not separator:
            continue
        name = key.strip()
        if name in values:
            raise LabFailure(f"setxkbmap query repeats {name}")
        values[name] = value.strip()
    if not {"rules", "model", "layout"}.issubset(values):
        raise LabFailure("setxkbmap query has incomplete RMLVO data")
    layouts = values["layout"].split(",") if values["layout"] else []
    variants = values.get("variant", "").split(",")
    if variants == [""] and len(layouts) > 1:
        variants = [""] * len(layouts)
    if len(variants) < len(layouts):
        variants.extend([""] * (len(layouts) - len(variants)))
    return {
        "rules": values["rules"],
        "model": values["model"],
        "layouts": layouts,
        "variants": variants,
        "options": values.get("options", ""),
    }


def configure_client_xkb(
    client: str,
    rmlvo: dict[str, Any],
) -> dict[str, Any]:
    command = [
        "setxkbmap",
        "-display",
        CLIENT_DISPLAY,
        "-rules",
        rmlvo["rules"],
        "-model",
        rmlvo["model"],
        "-layout",
        ",".join(rmlvo["layouts"]),
        "-variant",
        ",".join(rmlvo["variants"]),
        "-option",
        "",
    ]
    for option in filter(None, rmlvo["options"].split(",")):
        command.extend(("-option", option))
    podman_exec(client, command)
    query = podman_exec(
        client,
        ["setxkbmap", "-display", CLIENT_DISPLAY, "-query"],
    )
    observed = parse_setxkbmap_query(query.stdout)
    if observed != rmlvo:
        raise LabFailure(
            "client XKB query does not match the requested scenario configuration: "
            f"expected {json.dumps(rmlvo, ensure_ascii=False, sort_keys=True)}, "
            f"observed {json.dumps(observed, ensure_ascii=False, sort_keys=True)}"
        )
    return observed


def read_keyboard_fixture_events(container: str) -> list[dict[str, Any]]:
    probe = r"""
import json
import os
import stat
import sys

path = sys.argv[1]
limit = int(sys.argv[2])
try:
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
except FileNotFoundError:
    raise SystemExit(75)
try:
    details = os.fstat(descriptor)
    if not stat.S_ISREG(details.st_mode) or details.st_size > limit:
        raise SystemExit(70)
    payload = b''
    remaining = details.st_size
    while remaining:
        block = os.read(descriptor, remaining)
        if not block:
            raise SystemExit(71)
        payload += block
        remaining -= len(block)
    current = os.fstat(descriptor)
    if current.st_dev != details.st_dev or current.st_ino != details.st_ino:
        raise SystemExit(72)
finally:
    os.close(descriptor)
newline = payload.rfind(b'\n')
payload = payload[:newline + 1] if newline >= 0 else b''
text = payload.decode('utf-8', errors='strict')
lines = text.splitlines()
events = [json.loads(line) for line in lines if line]
print(json.dumps(events, ensure_ascii=True, separators=(',', ':')))
"""
    result = podman_exec(
        container,
        [
            "python3",
            "-c",
            probe,
            "/artifacts/keyboard-fixture.stdout",
            str(128 * 1024),
        ],
        check=False,
        announce=False,
    )
    if result.returncode:
        if result.returncode == 75:
            return []
        raise LabFailure("keyboard fixture event stream is unavailable or unsafe")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise LabFailure("keyboard fixture event probe returned invalid JSON") from error
    if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
        raise LabFailure("keyboard fixture event probe returned invalid events")
    return payload


def load_keyboard_fixture_events(path: Path) -> list[dict[str, Any]]:
    ensure_private_regular_file(path)
    raw = path.read_bytes()
    if not raw or len(raw) > 128 * 1024 or b"\0" in raw:
        raise LabFailure("keyboard fixture event stream has an invalid size")
    try:
        lines = raw.decode("utf-8", errors="strict").splitlines()
    except UnicodeDecodeError as error:
        raise LabFailure("keyboard fixture event stream is not UTF-8") from error
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            event = json.loads(line, object_pairs_hook=_json_object_without_duplicates)
        except json.JSONDecodeError as error:
            raise LabFailure("keyboard fixture event stream is not JSON") from error
        if not isinstance(event, dict):
            raise LabFailure("keyboard fixture event is not an object")
        events.append(event)
    return events


def keyboard_expected_texts(scenario: dict[str, Any]) -> list[str]:
    value = ""
    expected: list[str] = []
    for phase in scenario["phases"]:
        for item in phase["inputs"]:
            value += item["expected_text"]
            expected.append(value)
    return expected


KEYBOARD_LIVE_CHECK_NAMES = (
    "evidence_shape",
    "scenario_digest_bound",
    "scenario_content_bound",
    "physical_keycode_bound",
    "phase_configurations_bound",
    "client_xkb_queries_bound",
    "structured_keymap_packet_accepted",
    "server_keymaps_applied",
    "server_info_effective_state_exact",
    "no_rejected_configuration",
    "client_press_release_observed",
    "server_group_translation_exact",
    "server_press_release_stable",
    "fixture_event_sequence_exact",
    "application_text_authoritative",
    "fixture_clean_exit",
    "process_identity_unchanged",
    "connection_identity_unchanged",
    "runtime_replacement_proven",
)
KEYBOARD_OWNER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,127}")
KEYBOARD_RECEIVED_RE = re.compile(
    r"received Wayland structured keymap "
    r"packet=(?P<packet>keymap-changed|keyboard-config)"
)
KEYBOARD_ACCEPTED_RE = re.compile(
    r"accepted Wayland structured keymap "
    r"packet=(?P<packet>keymap-changed|keyboard-config) "
    r"representation=(?P<representation>[A-Za-z0-9_-]+) "
    r"hash=(?P<sha256>[0-9a-f]{64}) groups=(?P<groups>[1-4]) "
    r"owner=(?P<owner>[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,127}) "
    r"result=(?P<result>installed|identical)"
)
KEYBOARD_REJECTED_RE = re.compile(
    r"(?:rejected Wayland keyboard configuration|"
    r"structured Wayland keyboard configuration[^\n]*rejected)",
    re.IGNORECASE,
)
KEYBOARD_RESOLVE_RE = re.compile(
    r"get_keycode: pressed=(?P<pressed>True|False) "
    r"keyname=(?P<keyname>.+?) keyval=(?P<keyval>[0-9]+) "
    r"client-keycode=(?P<client_keycode>[0-9]+) "
    r"client-group=(?P<client_group>-?[0-9]+) -> "
    r"(?P<server_keycode>-?[0-9]+)/(?P<server_group>-?[0-9]+)"
)
KEYBOARD_DEVICE_RE = re.compile(
    r"wlr_seat_keyboard_notify_key\([^,]+, [0-9]+, "
    r"(?P<keycode>[0-9]+), (?P<state>[01])\)"
)
KEYBOARD_CLIENT_SEND_RE = re.compile(
    r"(?m)^[^\r\n]*\bdo_send_keyboard\((?P<arguments>[^\r\n]*)\)\s*$"
)
KEYBOARD_APPLIED_RE = re.compile(
    r"applied Wayland keyboard configuration "
    r"hash=(?P<sha256>[0-9a-f]{64}) groups=(?P<groups>[1-4]) "
    r"owner=(?P<owner>[^\s]+)"
)


def parse_keyboard_client_trace(
    path: Path,
    start: int,
    end: int,
) -> dict[str, Any]:
    """Parse actual clean-client key packets from one bounded log interval."""
    raw = path.read_bytes()
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or start < 0
        or end <= start
        or end > len(raw)
        or end - start > FRAME_LOG_TOTAL_BYTES
    ):
        raise LabFailure("keyboard client trace range is invalid")
    payload = raw[start:end]
    try:
        text_value = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise LabFailure("keyboard client trace is not UTF-8") from error
    events: list[dict[str, Any]] = []
    for match in KEYBOARD_CLIENT_SEND_RE.finditer(text_value):
        try:
            values = ast.literal_eval(f"({match.group('arguments')})")
        except (SyntaxError, ValueError) as error:
            raise LabFailure("keyboard client trace has invalid packet arguments") from error
        if not isinstance(values, tuple) or len(values) != 9:
            raise LabFailure("keyboard client trace has an invalid packet shape")
        packet, window, keyname, pressed, modifiers, keyval, keystr, keycode, group = values
        if (
            packet != "key-action"
            or _exact_int(window, positive=True) is None
            or not _bounded_utf8_string(keyname, 256, nonempty=True)
            or not isinstance(pressed, bool)
            or not isinstance(modifiers, (tuple, list))
            or len(modifiers) > 32
            or any(
                not _bounded_utf8_string(modifier, 256)
                for modifier in modifiers
            )
            or _exact_int(keyval) is None
            or not _bounded_utf8_string(keystr, 256)
            or _exact_int(keycode, positive=True) is None
            or keycode < 8
            or keycode > 255
            or _exact_int(group) is None
            or group < 0
            or group > 3
        ):
            raise LabFailure("keyboard client trace has invalid packet values")
        events.append(
            {
                "group": group,
                "keycode": keycode,
                "keyname": keyname,
                "keysym": keyval,
                "modifiers": list(modifiers),
                "pressed": pressed,
                "string": keystr,
                "window": window,
            }
        )
    return {
        "events": events,
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def parse_keyboard_server_trace(
    path: Path,
    start: int,
    end: int,
) -> dict[str, Any]:
    raw = path.read_bytes()
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or start < 0
        or end <= start
        or end > len(raw)
        or end - start > FRAME_LOG_TOTAL_BYTES
    ):
        raise LabFailure("keyboard server trace range is invalid")
    try:
        text_value = raw[start:end].decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise LabFailure("keyboard server trace is not UTF-8") from error
    resolutions: list[dict[str, Any]] = []
    for match in KEYBOARD_RESOLVE_RE.finditer(text_value):
        try:
            keyname = ast.literal_eval(match.group("keyname"))
        except (SyntaxError, ValueError):
            keyname = ""
        resolutions.append(
            {
                "client_group": int(match.group("client_group")),
                "client_keycode": int(match.group("client_keycode")),
                "keyname": keyname if isinstance(keyname, str) else "",
                "keyval": int(match.group("keyval")),
                "pressed": match.group("pressed") == "True",
                "server_group": int(match.group("server_group")),
                "server_keycode": int(match.group("server_keycode")),
            }
        )
    device = [
        {
            "keycode": int(match.group("keycode")),
            "pressed": match.group("state") == "1",
        }
        for match in KEYBOARD_DEVICE_RE.finditer(text_value)
    ]
    return {
        "device": device,
        "resolutions": resolutions,
        "sha256": hashlib.sha256(raw[start:end]).hexdigest(),
    }


def parse_keyboard_server_application(
    path: Path,
    start: int,
    end: int,
) -> dict[str, Any]:
    raw = path.read_bytes()
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or start < 0
        or end <= start
        or end > len(raw)
        or end - start > FRAME_LOG_TOTAL_BYTES
    ):
        raise LabFailure("keyboard server application range is invalid")
    payload = raw[start:end]
    try:
        text_value = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise LabFailure("keyboard server application trace is not UTF-8") from error
    _receipt, match, _accepted = _parse_keyboard_structured_triplet(text_value)
    return {
        "group_count": int(match.group("groups")),
        "hash": match.group("sha256"),
        "log_range": [start, end],
        "owner": match.group("owner"),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _parse_keyboard_structured_triplet(
    text_value: str,
) -> tuple[re.Match[str], re.Match[str], re.Match[str]]:
    """Require one received -> applied -> accepted structured update."""
    received = tuple(KEYBOARD_RECEIVED_RE.finditer(text_value))
    accepted = tuple(KEYBOARD_ACCEPTED_RE.finditer(text_value))
    if len(received) != 1 or len(accepted) != 1:
        raise LabFailure(
            "keyboard structured-update range does not contain one receipt and acceptance"
        )
    receipt = received[0]
    result = accepted[0]
    if receipt.start() >= result.start() or receipt.group("packet") != result.group("packet"):
        raise LabFailure("keyboard structured update was not accepted after its receipt")
    if result.group("result") != "installed":
        raise LabFailure("keyboard structured update did not install the keymap")
    applications = tuple(
        KEYBOARD_APPLIED_RE.finditer(text_value, receipt.end(), result.start())
    )
    if len(applications) != 1:
        raise LabFailure(
            "keyboard structured update does not contain one ordered keymap application"
        )
    application = applications[0]
    if (
        application.group("sha256") != result.group("sha256")
        or application.group("groups") != result.group("groups")
        or application.group("owner") != result.group("owner")
    ):
        raise LabFailure("keyboard structured application does not match its acceptance")
    if KEYBOARD_REJECTED_RE.search(text_value):
        raise LabFailure("keyboard configuration interval contains a rejected update")
    return receipt, application, result


def parse_keyboard_structured_update(
    path: Path,
    start: int,
    end: int,
) -> dict[str, Any]:
    """Bind one received and accepted structured packet in exact log order."""
    raw = path.read_bytes()
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or start < 0
        or end <= start
        or end > len(raw)
        or end - start > FRAME_LOG_TOTAL_BYTES
    ):
        raise LabFailure("keyboard structured-update range is invalid")
    payload = raw[start:end]
    try:
        text_value = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise LabFailure("keyboard structured-update trace is not UTF-8") from error
    _receipt, _application, result = _parse_keyboard_structured_triplet(text_value)
    return {
        "group_count": int(result.group("groups")),
        "hash": result.group("sha256"),
        "log_range": [start, end],
        "owner": result.group("owner"),
        "packet": result.group("packet"),
        "representation": result.group("representation"),
        "result": result.group("result"),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _xpra_info_sequence(value: str, field: str) -> list[str]:
    try:
        parsed = ast.literal_eval(value)
    except (SyntaxError, ValueError) as error:
        raise LabFailure(f"keyboard server info has invalid {field}") from error
    if isinstance(parsed, str):
        return [parsed]
    if not isinstance(parsed, tuple) or any(not isinstance(item, str) for item in parsed):
        raise LabFailure(f"keyboard server info has invalid {field}")
    return list(parsed)


def parse_keyboard_server_info(path: Path) -> dict[str, Any]:
    """Extract bounded effective keyboard state without retaining raw keymap text."""
    ensure_private_regular_file(path)
    raw = path.read_bytes()
    if not raw or len(raw) > 1024 * 1024 or b"\0" in raw:
        raise LabFailure("keyboard server info has an invalid size")
    try:
        lines = raw.decode("utf-8", errors="strict").splitlines()
    except UnicodeDecodeError as error:
        raise LabFailure("keyboard server info is not UTF-8") from error
    values: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition("=")
        if (
            not separator
            or re.fullmatch(r"[A-Za-z0-9_.-]+", key) is None
            or key in values
        ):
            raise LabFailure("keyboard server info has invalid or duplicate fields")
        values[key] = value
    required = {
        "keyboard.compiled-groups",
        "keyboard.current-group",
        "keyboard.effective-rmlvo.layout-groups",
        "keyboard.effective-rmlvo.layouts",
        "keyboard.effective-rmlvo.model",
        "keyboard.effective-rmlvo.options",
        "keyboard.effective-rmlvo.rules",
        "keyboard.effective-rmlvo.variants",
        "keyboard.owner",
    }
    if not required.issubset(values):
        raise LabFailure("keyboard server info is incomplete")
    try:
        compiled_groups = int(values["keyboard.compiled-groups"])
        current_group = int(values["keyboard.current-group"])
    except ValueError as error:
        raise LabFailure("keyboard server info has invalid group values") from error
    owner = values["keyboard.owner"]
    if (
        compiled_groups < 1
        or compiled_groups > 4
        or current_group < 0
        or current_group >= compiled_groups
        or KEYBOARD_OWNER_RE.fullmatch(owner) is None
    ):
        raise LabFailure("keyboard server info has unsafe effective state")
    rejected = any(
        re.search(r"(?:^|\.)keyboard\.rejected(?:[.-]|$)", key)
        for key in values
    )
    return {
        "compiled_groups": compiled_groups,
        "current_group": current_group,
        "effective_rmlvo": {
            "layouts": _xpra_info_sequence(
                values["keyboard.effective-rmlvo.layouts"], "layouts"
            ),
            "model": values["keyboard.effective-rmlvo.model"],
            "options": values["keyboard.effective-rmlvo.options"],
            "rules": values["keyboard.effective-rmlvo.rules"],
            "variants": _xpra_info_sequence(
                values["keyboard.effective-rmlvo.variants"], "variants"
            ),
        },
        "layout_groups": values["keyboard.effective-rmlvo.layout-groups"] == "True",
        "owner": owner,
        "rejected_configuration": rejected,
    }


def keyboard_live_checks(
    evidence: dict[str, Any],
    scenario: dict[str, Any],
    scenario_sha256: str,
) -> dict[str, bool]:
    """Validate every authority required by the keyboard live regression."""
    checks = dict.fromkeys(KEYBOARD_LIVE_CHECK_NAMES, False)
    try:
        if not isinstance(evidence, dict) or set(evidence) not in (
            {
                "application",
                "identity_snapshots",
                "phases",
                "physical_keycode",
                "runtime_replacement",
                "scenario",
                "schema",
                "window_id",
                "xpra_window_id",
            },
            {
                "application",
                "checks",
                "identity_snapshots",
                "phases",
                "physical_keycode",
                "runtime_replacement",
                "scenario",
                "schema",
                "window_id",
                "xpra_window_id",
            },
        ):
            return checks
        checks["evidence_shape"] = _exact_int(evidence.get("schema")) == 1
        scenario_binding = evidence.get("scenario")
        if not isinstance(scenario_binding, dict) or set(scenario_binding) != {
            "data",
            "sha256",
        }:
            return checks
        checks["scenario_digest_bound"] = bool(
            re.fullmatch(r"[0-9a-f]{64}", scenario_sha256)
            and scenario_binding.get("sha256") == scenario_sha256
        )
        checks["scenario_content_bound"] = scenario_binding.get("data") == scenario

        physical_keycode = _exact_int(evidence.get("physical_keycode"), positive=True)
        window_id = _exact_int(evidence.get("window_id"), positive=True)
        xpra_window_id = _exact_int(evidence.get("xpra_window_id"), positive=True)
        expected_keycodes = {
            phase.get("physical_keycode")
            for phase in scenario.get("phases", ())
            if isinstance(phase, dict)
        }
        checks["physical_keycode_bound"] = bool(
            physical_keycode is not None
            and 8 <= physical_keycode <= 255
            and expected_keycodes == {physical_keycode}
            and window_id is not None
            and xpra_window_id is not None
        )

        phases = evidence.get("phases")
        scenario_phases = scenario.get("phases")
        if not isinstance(phases, list) or not isinstance(scenario_phases, list):
            return checks
        if len(phases) != len(scenario_phases) or len(phases) < 2:
            return checks
        phase_configurations_bound = True
        client_queries_bound = True
        structured_updates_accepted = True
        server_keymaps_applied = True
        server_info_exact = True
        no_rejected_configuration = True
        client_input_observed = True
        server_translation_exact = True
        server_press_release_stable = True
        expected_texts: list[str] = []
        expected_event_inputs: list[dict[str, Any]] = []
        cumulative_text = ""
        owners: set[str] = set()
        structured_owners: set[str] = set()
        info_owners: set[str] = set()
        phase_hashes: list[str] = []
        last_client_log_end = 0
        last_server_log_end = 0
        for phase, expected_phase in zip(phases, scenario_phases, strict=True):
            if not isinstance(phase, dict) or set(phase) != {
                "client_query",
                "inputs",
                "name",
                "rmlvo",
                "rmlvo_hash",
                "server_application",
                "server_info_artifact",
                "server_info",
                "server_info_sha256",
                "structured_update",
            }:
                phase_configurations_bound = False
                continue
            expected_rmlvo = expected_phase.get("rmlvo")
            expected_inputs = expected_phase.get("inputs")
            expected_current_group = (
                _exact_int(expected_inputs[-1].get("group"))
                if isinstance(expected_inputs, list)
                and expected_inputs
                and isinstance(expected_inputs[-1], dict)
                else None
            )
            expected_hash = keyboard_rmlvo_hash(expected_rmlvo)
            phase_hashes.append(expected_hash)
            info_name = phase.get("server_info_artifact")
            phase_configurations_bound &= bool(
                phase.get("name") == expected_phase.get("name")
                and phase.get("rmlvo") == expected_rmlvo
                and phase.get("rmlvo_hash") == expected_hash
                and isinstance(info_name, str)
                and re.fullmatch(r"server-info-keyboard-[a-z0-9-]+\.txt", info_name)
                and re.fullmatch(
                    r"[0-9a-f]{64}", str(phase.get("server_info_sha256", ""))
                )
            )
            client_queries_bound &= phase.get("client_query") == expected_rmlvo
            structured = phase.get("structured_update")
            if not isinstance(structured, dict) or set(structured) != {
                "group_count",
                "hash",
                "log_range",
                "owner",
                "packet",
                "representation",
                "result",
                "sha256",
            }:
                structured_updates_accepted = False
            else:
                structured_owner = structured.get("owner")
                structured_owners.add(
                    structured_owner if isinstance(structured_owner, str) else ""
                )
                structured_range = structured.get("log_range")
                structured_updates_accepted &= bool(
                    structured.get("packet") == "keymap-changed"
                    and structured.get("representation") == "legacy"
                    and structured.get("result") == "installed"
                    and structured.get("hash") == expected_hash
                    and structured.get("group_count")
                    == len(expected_rmlvo["layouts"])
                    and isinstance(structured_owner, str)
                    and KEYBOARD_OWNER_RE.fullmatch(structured_owner) is not None
                    and isinstance(structured_range, list)
                    and len(structured_range) == 2
                    and _exact_int(structured_range[0]) is not None
                    and _exact_int(structured_range[1], positive=True) is not None
                    and structured_range[1] > structured_range[0]
                    and re.fullmatch(
                        r"[0-9a-f]{64}", str(structured.get("sha256", ""))
                    )
                )
            applied = phase.get("server_application")
            if not isinstance(applied, dict) or set(applied) != {
                "group_count",
                "hash",
                "log_range",
                "owner",
                "sha256",
            }:
                server_keymaps_applied = False
            else:
                owner = applied.get("owner")
                owners.add(owner if isinstance(owner, str) else "")
                server_keymaps_applied &= bool(
                    applied.get("hash") == expected_hash
                    and applied.get("group_count") == len(expected_rmlvo["layouts"])
                    and isinstance(owner, str)
                    and 0 < len(owner) <= 128
                    and not any(ord(character) < 33 for character in owner)
                    and isinstance(applied.get("log_range"), list)
                    and len(applied["log_range"]) == 2
                    and _exact_int(applied["log_range"][0]) is not None
                    and _exact_int(applied["log_range"][1], positive=True) is not None
                    and applied["log_range"][1] > applied["log_range"][0]
                    and re.fullmatch(r"[0-9a-f]{64}", str(applied.get("sha256", "")))
                    and isinstance(structured, dict)
                    and applied.get("log_range") == structured.get("log_range")
                    and applied.get("sha256") == structured.get("sha256")
                )
                applied_range = applied.get("log_range")
                if (
                    isinstance(applied_range, list)
                    and len(applied_range) == 2
                    and _exact_int(applied_range[0]) is not None
                    and _exact_int(applied_range[1], positive=True) is not None
                    and applied_range[1] > applied_range[0]
                    and applied_range[0] >= last_server_log_end
                ):
                    last_server_log_end = applied_range[1]
                else:
                    phase_configurations_bound = False
            server_info = phase.get("server_info")
            if not isinstance(server_info, dict) or set(server_info) != {
                "compiled_groups",
                "current_group",
                "effective_rmlvo",
                "layout_groups",
                "owner",
                "rejected_configuration",
            }:
                server_info_exact = False
                no_rejected_configuration = False
            else:
                info_owner = server_info.get("owner")
                info_owners.add(info_owner if isinstance(info_owner, str) else "")
                current_group = _exact_int(server_info.get("current_group"))
                server_info_exact &= bool(
                    server_info.get("compiled_groups") == len(expected_rmlvo["layouts"])
                    and current_group == expected_current_group
                    and server_info.get("effective_rmlvo") == expected_rmlvo
                    and server_info.get("layout_groups") is True
                    and isinstance(info_owner, str)
                    and KEYBOARD_OWNER_RE.fullmatch(info_owner) is not None
                )
                no_rejected_configuration &= (
                    server_info.get("rejected_configuration") is False
                )
            inputs = phase.get("inputs")
            if (
                not isinstance(inputs, list)
                or not isinstance(expected_inputs, list)
                or len(inputs) != len(expected_inputs)
            ):
                client_input_observed = False
                server_translation_exact = False
                server_press_release_stable = False
                continue
            for item, expected_item in zip(inputs, expected_inputs, strict=True):
                if not isinstance(item, dict) or set(item) != {
                    "application_text",
                    "client",
                    "client_log_range",
                    "client_trace",
                    "expected_text",
                    "group",
                    "server_log_range",
                    "server_trace",
                }:
                    client_input_observed = False
                    server_translation_exact = False
                    server_press_release_stable = False
                    continue
                expected_character = expected_item.get("expected_text")
                cumulative_text += expected_character
                expected_texts.append(cumulative_text)
                group = expected_item.get("group")
                client_record = item.get("client")
                client_trace = item.get("client_trace")
                client_log_range = item.get("client_log_range")
                trace = item.get("server_trace")
                log_range = item.get("server_log_range")
                if not isinstance(client_record, dict) or set(client_record) != {
                    "display",
                    "focus_before",
                    "group_after",
                    "group_before",
                    "group_requested",
                    "keysym",
                    "keysym_name",
                    "physical_keycode",
                    "press",
                    "release",
                    "schema",
                    "window",
                }:
                    client_input_observed = False
                    client_record = {}
                keysym = _exact_int(client_record.get("keysym"), positive=True)
                client_input_observed &= bool(
                    _exact_int(item.get("group")) == group
                    and item.get("expected_text") == expected_character
                    and item.get("application_text") == cumulative_text
                    and _exact_int(client_record.get("schema")) == 1
                    and client_record.get("display") == CLIENT_DISPLAY
                    and client_record.get("window") == window_id
                    and client_record.get("focus_before") == window_id
                    and _exact_int(client_record.get("group_requested")) == group
                    and _exact_int(client_record.get("group_before")) == group
                    and _exact_int(client_record.get("group_after")) == group
                    and _exact_int(
                        client_record.get("physical_keycode"), positive=True
                    )
                    == physical_keycode
                    and client_record.get("press") is True
                    and client_record.get("release") is True
                    and keysym is not None
                    and isinstance(client_record.get("keysym_name"), str)
                    and re.fullmatch(
                        r"[A-Za-z0-9_]+", client_record.get("keysym_name", "")
                    )
                    is not None
                    and isinstance(log_range, list)
                    and len(log_range) == 2
                    and _exact_int(log_range[0]) is not None
                    and _exact_int(log_range[1], positive=True) is not None
                    and log_range[1] > log_range[0]
                )
                if not isinstance(client_trace, dict) or set(client_trace) != {
                    "events",
                    "sha256",
                }:
                    client_input_observed = False
                    client_trace = {}
                client_events = client_trace.get("events")
                observed_client_keyname: str | None = None
                client_event_shape = bool(
                    isinstance(client_events, list)
                    and len(client_events) == 2
                    and all(
                        isinstance(value, dict)
                        and set(value)
                        == {
                            "group",
                            "keycode",
                            "keyname",
                            "keysym",
                            "modifiers",
                            "pressed",
                            "string",
                            "window",
                        }
                        for value in client_events
                    )
                )
                if client_event_shape:
                    client_press, client_release = client_events
                    observed_client_keyname = client_press.get("keyname")
                    client_range_ordered = bool(
                        isinstance(client_log_range, list)
                        and len(client_log_range) == 2
                        and _exact_int(client_log_range[0]) is not None
                        and _exact_int(client_log_range[1], positive=True) is not None
                        and client_log_range[1] > client_log_range[0]
                        and client_log_range[0] >= last_client_log_end
                    )
                    client_input_observed &= bool(
                        client_press.get("pressed") is True
                        and client_release.get("pressed") is False
                        and client_press.get("window") == xpra_window_id
                        and client_release.get("window") == xpra_window_id
                        and client_press.get("group") == group
                        and client_release.get("group") == group
                        and client_press.get("keycode") == physical_keycode
                        and client_release.get("keycode") == physical_keycode
                        and client_press.get("keysym") == keysym
                        and client_release.get("keysym") == keysym
                        and _bounded_utf8_string(
                            client_press.get("keyname"), 256, nonempty=True
                        )
                        and client_release.get("keyname")
                        == client_press.get("keyname")
                        and client_press.get("string") == expected_character
                        and client_release.get("string") == expected_character
                        and isinstance(client_press.get("modifiers"), list)
                        and client_release.get("modifiers")
                        == client_press.get("modifiers")
                        and all(
                            isinstance(modifier, str)
                            for modifier in client_press.get("modifiers", ())
                        )
                        and client_range_ordered
                        and re.fullmatch(
                            r"[0-9a-f]{64}", str(client_trace.get("sha256", ""))
                        )
                    )
                    if client_range_ordered:
                        last_client_log_end = client_log_range[1]
                else:
                    client_input_observed = False
                if not isinstance(trace, dict) or set(trace) != {
                    "device",
                    "resolutions",
                    "sha256",
                }:
                    server_translation_exact = False
                    server_press_release_stable = False
                    trace = {}
                resolutions = trace.get("resolutions")
                device = trace.get("device")
                resolution_shape = bool(
                    isinstance(resolutions, list)
                    and len(resolutions) == 2
                    and all(
                        isinstance(value, dict)
                        and set(value)
                        == {
                            "client_group",
                            "client_keycode",
                            "keyname",
                            "keyval",
                            "pressed",
                            "server_group",
                            "server_keycode",
                        }
                        for value in resolutions
                    )
                )
                device_shape = bool(
                    isinstance(device, list)
                    and len(device) == 2
                    and all(
                        isinstance(value, dict)
                        and set(value) == {"keycode", "pressed"}
                        for value in device
                    )
                    and _exact_int(device[0].get("keycode"), positive=True)
                    == physical_keycode
                    and device[0].get("pressed") is True
                    and _exact_int(device[1].get("keycode"), positive=True)
                    == physical_keycode
                    and device[1].get("pressed") is False
                )
                server_range_ordered = bool(
                    isinstance(log_range, list)
                    and len(log_range) == 2
                    and _exact_int(log_range[0]) is not None
                    and _exact_int(log_range[1], positive=True) is not None
                    and log_range[1] > log_range[0]
                    and log_range[0] >= last_server_log_end
                )
                if resolution_shape:
                    press, release = resolutions
                    server_translation_exact &= bool(
                        press.get("pressed") is True
                        and release.get("pressed") is False
                        and _exact_int(press.get("client_group")) == group
                        and _exact_int(release.get("client_group")) == group
                        and _exact_int(
                            press.get("client_keycode"), positive=True
                        )
                        == physical_keycode
                        and _exact_int(
                            release.get("client_keycode"), positive=True
                        )
                        == physical_keycode
                        and _exact_int(press.get("server_group")) == group
                        and _exact_int(release.get("server_group")) == group
                        and _exact_int(
                            press.get("server_keycode"), positive=True
                        )
                        == physical_keycode
                        and _exact_int(
                            release.get("server_keycode"), positive=True
                        )
                        == physical_keycode
                        and press.get("keyval") == keysym
                        and release.get("keyval") == keysym
                        and _bounded_utf8_string(
                            press.get("keyname"), 256, nonempty=True
                        )
                        and press.get("keyname") == observed_client_keyname
                        and release.get("keyname") == press.get("keyname")
                        and re.fullmatch(r"[0-9a-f]{64}", str(trace.get("sha256", "")))
                    )
                    server_press_release_stable &= bool(
                        device_shape
                        and server_range_ordered
                        and (press.get("server_keycode"), press.get("server_group"))
                        == (release.get("server_keycode"), release.get("server_group"))
                    )
                    server_translation_exact &= server_range_ordered
                    if server_range_ordered:
                        last_server_log_end = log_range[1]
                    expected_event_inputs.append(
                        {
                            "after": cumulative_text,
                            "before": cumulative_text[: -len(expected_character)],
                            "hardware_keycode": physical_keycode,
                            "keyval": keysym,
                        }
                    )
                else:
                    server_translation_exact = False
                    server_press_release_stable = False
        checks["phase_configurations_bound"] = bool(
            phase_configurations_bound and len(set(phase_hashes)) == len(phase_hashes)
        )
        checks["client_xkb_queries_bound"] = client_queries_bound
        checks["structured_keymap_packet_accepted"] = bool(
            structured_updates_accepted
            and len(structured_owners) == 1
            and "" not in structured_owners
        )
        checks["server_keymaps_applied"] = bool(
            server_keymaps_applied and len(owners) == 1 and "" not in owners
        )
        checks["server_info_effective_state_exact"] = bool(
            server_info_exact
            and len(info_owners) == 1
            and "" not in info_owners
            and info_owners == owners == structured_owners
        )
        checks["no_rejected_configuration"] = bool(no_rejected_configuration)
        checks["client_press_release_observed"] = client_input_observed
        checks["server_group_translation_exact"] = server_translation_exact
        checks["server_press_release_stable"] = server_press_release_stable

        application = evidence.get("application")
        if not isinstance(application, dict) or set(application) != {
            "events",
            "exit_status",
            "final_text",
            "observed_texts",
        }:
            return checks
        events = application.get("events")
        event_sequence_exact = isinstance(events, list) and len(events) == (
            2 + 3 * len(expected_event_inputs)
        )
        event_texts: list[str] = []
        if event_sequence_exact:
            ready = events[0]
            closed = events[-1]
            event_sequence_exact = bool(
                isinstance(ready, dict)
                and set(ready)
                == {
                    "backend",
                    "event",
                    "monotonic_ns",
                    "schema",
                    "sequence",
                    "text",
                    "title",
                }
                and ready.get("event") == "ready"
                and ready.get("backend")
                and "wayland" in str(ready.get("backend")).casefold()
                and ready.get("text") == ""
                and ready.get("title") == KEYBOARD_FIXTURE_TITLE
                and isinstance(closed, dict)
                and set(closed)
                == {"event", "monotonic_ns", "schema", "sequence", "text"}
                and closed.get("event") == "closed"
                and closed.get("text") == (expected_texts[-1] if expected_texts else "")
            )
            monotonic_values: list[int] = []
            for index, expected in enumerate(expected_event_inputs):
                press, changed, release = events[1 + index * 3 : 4 + index * 3]
                event_sequence_exact &= bool(
                    isinstance(press, dict)
                    and isinstance(changed, dict)
                    and isinstance(release, dict)
                    and set(press)
                    == {
                        "event",
                        "hardware_keycode",
                        "keyname",
                        "keyval",
                        "monotonic_ns",
                        "schema",
                        "sequence",
                        "text",
                    }
                    and set(release) == set(press)
                    and set(changed)
                    == {"event", "monotonic_ns", "schema", "sequence", "text"}
                    and press.get("event") == "key-press"
                    and changed.get("event") == "changed"
                    and release.get("event") == "key-release"
                    and press.get("hardware_keycode") == expected["hardware_keycode"]
                    and release.get("hardware_keycode") == expected["hardware_keycode"]
                    and press.get("keyval") == expected["keyval"]
                    and release.get("keyval") == expected["keyval"]
                    and _bounded_utf8_string(
                        press.get("keyname"), 256, nonempty=True
                    )
                    and release.get("keyname") == press.get("keyname")
                    and press.get("text") == expected["before"]
                    and changed.get("text") == expected["after"]
                    and release.get("text") == expected["after"]
                )
                event_texts.append(changed.get("text") if isinstance(changed, dict) else "")
            for sequence, event in enumerate(events):
                if not isinstance(event, dict):
                    event_sequence_exact = False
                    continue
                monotonic = _exact_int(event.get("monotonic_ns"), positive=True)
                event_sequence_exact &= bool(
                    _exact_int(event.get("schema")) == 1
                    and _exact_int(event.get("sequence")) == sequence
                    and monotonic is not None
                )
                if monotonic is not None:
                    monotonic_values.append(monotonic)
            event_sequence_exact &= bool(
                len(monotonic_values) == len(events)
                and all(left < right for left, right in pairwise(monotonic_values))
            )
        checks["fixture_event_sequence_exact"] = bool(event_sequence_exact)
        checks["application_text_authoritative"] = bool(
            event_sequence_exact
            and application.get("observed_texts") == expected_texts
            and event_texts == expected_texts
            and application.get("final_text")
            == (expected_texts[-1] if expected_texts else "")
        )
        checks["fixture_clean_exit"] = bool(
            _exact_int(application.get("exit_status")) == 0
            and isinstance(events, list)
            and events
            and isinstance(events[-1], dict)
            and events[-1].get("event") == "closed"
        )

        identities = evidence.get("identity_snapshots")
        identity_shape = isinstance(identities, list) and len(identities) == len(phases) + 1

        def valid_identity(value: Any) -> bool:
            if not isinstance(value, dict) or set(value) != {
                "connection",
                "process",
            }:
                return False
            connection = value.get("connection")
            process = value.get("process")

            def valid_process(candidate: Any) -> bool:
                return bool(
                    isinstance(candidate, dict)
                    and set(candidate) == {"cmdline_sha256", "pid", "start_ticks"}
                    and _exact_int(candidate.get("pid"), positive=True) is not None
                    and re.fullmatch(r"[1-9][0-9]*", str(candidate.get("start_ticks", "")))
                    and re.fullmatch(
                        r"[0-9a-f]{64}", str(candidate.get("cmdline_sha256", ""))
                    )
                )

            return bool(
                valid_process(process)
                and isinstance(connection, dict)
                and set(connection)
                == {
                    "family",
                    "inode",
                    "local_address",
                    "local_port",
                    "remote_address",
                    "remote_port",
                    "state",
                }
                and connection.get("family") in {"tcp4", "tcp6"}
                and _exact_int(connection.get("inode"), positive=True) is not None
                and _exact_int(connection.get("local_port"), positive=True) is not None
                and _exact_int(connection.get("remote_port"), positive=True) is not None
                and connection.get("state") == "established"
                and re.fullmatch(r"[0-9A-F]+", str(connection.get("local_address", "")))
                and re.fullmatch(r"[0-9A-F]+", str(connection.get("remote_address", "")))
            )

        if identity_shape:
            identity_shape = all(
                isinstance(snapshot, dict)
                and set(snapshot) == {"client", "server"}
                and valid_identity(snapshot.get("client"))
                and valid_identity(snapshot.get("server"))
                for snapshot in identities
            )
        if identity_shape:
            identity_shape = all(
                snapshot["client"]["connection"]["family"]
                == snapshot["server"]["connection"]["family"]
                and snapshot["client"]["connection"]["local_address"]
                == snapshot["server"]["connection"]["remote_address"]
                and snapshot["client"]["connection"]["remote_address"]
                == snapshot["server"]["connection"]["local_address"]
                and snapshot["client"]["connection"]["local_port"]
                == snapshot["server"]["connection"]["remote_port"]
                and snapshot["client"]["connection"]["remote_port"] == SERVER_PORT
                and snapshot["server"]["connection"]["local_port"] == SERVER_PORT
                for snapshot in identities
            )
        process_unchanged = False
        connection_unchanged = False
        if identity_shape:
            first = identities[0]
            process_unchanged = all(
                snapshot[role]["process"] == first[role]["process"]
                for snapshot in identities[1:]
                for role in ("client", "server")
            )
            connection_unchanged = all(
                snapshot[role]["connection"] == first[role]["connection"]
                for snapshot in identities[1:]
                for role in ("client", "server")
            )
        checks["process_identity_unchanged"] = process_unchanged
        checks["connection_identity_unchanged"] = connection_unchanged

        replacement = evidence.get("runtime_replacement")
        checks["runtime_replacement_proven"] = bool(
            isinstance(replacement, dict)
            and set(replacement)
            == {
                "after_hash",
                "application_observed_after_replacement",
                "before_hash",
                "configuration_changed",
                "connection_unchanged",
                "processes_unchanged",
            }
            and len(phase_hashes) >= 2
            and replacement.get("before_hash") == phase_hashes[0]
            and replacement.get("after_hash") == phase_hashes[-1]
            and replacement.get("configuration_changed") is True
            and phase_hashes[0] != phase_hashes[-1]
            and replacement.get("processes_unchanged") is True
            and replacement.get("connection_unchanged") is True
            and process_unchanged
            and connection_unchanged
            and replacement.get("application_observed_after_replacement") is True
            and bool(expected_texts)
            and application.get("final_text") == expected_texts[-1]
        )
    except (KeyError, TypeError, ValueError, UnicodeError):
        return checks
    return checks


def keyboard_embedded_checks_match(
    evidence: dict[str, Any],
    scenario: dict[str, Any],
    scenario_sha256: str,
) -> bool:
    """Recompute finalized checks and reject missing, extra, or forged entries."""
    recorded = evidence.get("checks") if isinstance(evidence, dict) else None
    if (
        not isinstance(recorded, dict)
        or set(recorded) != set(KEYBOARD_LIVE_CHECK_NAMES)
        or any(value is not True for value in recorded.values())
    ):
        return False
    candidate = dict(evidence)
    candidate.pop("checks", None)
    computed = keyboard_live_checks(candidate, scenario, scenario_sha256)
    return recorded == computed and all(computed.values())


def keyboard_artifact_evidence_matches(
    evidence: dict[str, Any],
    directory: Path,
) -> bool:
    """Reparse retained authority artifacts and cross-bind their report records."""
    try:
        server_log = directory / "server.stderr"
        client_log = directory / "client.stdout"
        ensure_private_regular_file(server_log)
        ensure_private_regular_file(client_log)
        phases = evidence.get("phases")
        if not isinstance(phases, list) or not phases:
            return False
        for phase in phases:
            if not isinstance(phase, dict):
                return False
            application = phase.get("server_application")
            structured = phase.get("structured_update")
            if not isinstance(application, dict) or not isinstance(structured, dict):
                return False
            application_range = application.get("log_range")
            structured_range = structured.get("log_range")
            if (
                not isinstance(application_range, list)
                or len(application_range) != 2
                or not isinstance(structured_range, list)
                or len(structured_range) != 2
                or parse_keyboard_server_application(
                    server_log, application_range[0], application_range[1]
                )
                != application
                or parse_keyboard_structured_update(
                    server_log, structured_range[0], structured_range[1]
                )
                != structured
            ):
                return False
            info_name = phase.get("server_info_artifact")
            info_digest = phase.get("server_info_sha256")
            if (
                not isinstance(info_name, str)
                or re.fullmatch(r"server-info-keyboard-[a-z0-9-]+\.txt", info_name)
                is None
            ):
                return False
            info_path = directory / info_name
            if (
                not isinstance(info_digest, str)
                or sha256_file(info_path) != info_digest
                or parse_keyboard_server_info(info_path) != phase.get("server_info")
            ):
                return False
            inputs = phase.get("inputs")
            if not isinstance(inputs, list):
                return False
            for item in inputs:
                if not isinstance(item, dict):
                    return False
                client_trace = item.get("client_trace")
                client_log_range = item.get("client_log_range")
                trace = item.get("server_trace")
                log_range = item.get("server_log_range")
                if (
                    not isinstance(client_trace, dict)
                    or not isinstance(client_log_range, list)
                    or len(client_log_range) != 2
                    or parse_keyboard_client_trace(
                        client_log, client_log_range[0], client_log_range[1]
                    )
                    != client_trace
                    or not isinstance(trace, dict)
                    or not isinstance(log_range, list)
                    or len(log_range) != 2
                    or parse_keyboard_server_trace(
                        server_log, log_range[0], log_range[1]
                    )
                    != trace
                ):
                    return False
        application = evidence.get("application")
        return bool(
            isinstance(application, dict)
            and load_keyboard_fixture_events(directory / "keyboard-fixture.stdout")
            == application.get("events")
            and process_exit_status(directory, "keyboard-fixture")
            == application.get("exit_status")
        )
    except (LabFailure, OSError, TypeError, ValueError):
        return False


def exercise_wayland_keyboard(
    server: str,
    server_pid: int,
    client: str,
    client_pid: int,
    window_id: str,
    xpra_window_id: int,
    directory: Path,
    scenario: dict[str, Any],
    scenario_sha256: str,
) -> dict[str, Any]:
    """Drive the real client XKB/XTEST path and retain bounded live evidence."""
    expected_window_id = int(window_id, 0)
    expected_texts = keyboard_expected_texts(scenario)

    def fixture_ready() -> bool:
        events = read_keyboard_fixture_events(server)
        if not events:
            return False
        first = events[0]
        if (
            first.get("event") != "ready"
            or first.get("title") != KEYBOARD_FIXTURE_TITLE
            or first.get("text") != ""
        ):
            raise LabFailure("keyboard fixture published invalid readiness evidence")
        return True

    wait_for("native Wayland keyboard fixture readiness", fixture_ready)
    podman_exec(
        client,
        [
            "env",
            f"DISPLAY={CLIENT_DISPLAY}",
            "xdotool",
            "windowactivate",
            "--sync",
            window_id,
        ],
    )
    identity_snapshots = [
        keyboard_identity_snapshot(server, server_pid, client, client_pid)
    ]
    baseline_rmlvo = scenario["phases"][-1]["rmlvo"]
    baseline_hash = keyboard_rmlvo_hash(baseline_rmlvo)
    baseline_patterns = (
        r"received Wayland structured keymap packet=keymap-changed",
        (
            rf"accepted Wayland structured keymap packet=keymap-changed "
            rf"representation=legacy hash={baseline_hash} "
            rf"groups={len(baseline_rmlvo['layouts'])} "
            rf"owner=[A-Za-z0-9][A-Za-z0-9_.:@+-]{{0,127}} "
            r"result=(?:installed|identical)"
        ),
    )
    wait_for(
        "initial structured client keymap acceptance",
        lambda: container_artifact_suffix_matches(
            server,
            "server.stderr",
            0,
            baseline_patterns,
        ),
    )
    phases: list[dict[str, Any]] = []
    observed_texts: list[str] = []
    input_index = 0
    for phase in scenario["phases"]:
        rmlvo = phase["rmlvo"]
        rmlvo_hash = keyboard_rmlvo_hash(rmlvo)
        phase_log_start = container_artifact_size(server, "server.stderr")
        query = configure_client_xkb(client, rmlvo)
        structured_patterns = (
            r"received Wayland structured keymap packet=keymap-changed",
            (
                rf"accepted Wayland structured keymap packet=keymap-changed "
                rf"representation=legacy hash={rmlvo_hash} "
                rf"groups={len(rmlvo['layouts'])} "
                rf"owner=[A-Za-z0-9][A-Za-z0-9_.:@+-]{{0,127}} "
                r"result=installed"
            ),
        )

        def server_accepted_structured_phase(
            offset: int = phase_log_start,
            patterns: tuple[str, ...] = structured_patterns,
        ) -> bool:
            return container_artifact_suffix_matches(
                server,
                "server.stderr",
                offset,
                patterns,
            )

        wait_for(
            f"structured server keymap acceptance for phase {phase['name']}",
            server_accepted_structured_phase,
        )
        structured_log_end = container_artifact_size(server, "server.stderr")
        info_name = f"server-info-keyboard-{phase['name']}.txt"
        phase_inputs: list[dict[str, Any]] = []
        for item in phase["inputs"]:
            group = item["group"]
            physical_keycode = phase["physical_keycode"]
            client_log_start = container_artifact_size(client, "client.stdout")
            log_start = container_artifact_size(server, "server.stderr")
            driver = podman_exec(
                client,
                [
                    "/usr/local/bin/xpra-xkb-xtest-driver",
                    CLIENT_DISPLAY,
                    window_id,
                    str(group),
                    str(physical_keycode),
                ],
            )
            try:
                client_record = json.loads(
                    driver.stdout,
                    object_pairs_hook=_json_object_without_duplicates,
                )
            except json.JSONDecodeError as error:
                raise LabFailure("keyboard XTEST driver returned invalid JSON") from error
            if not isinstance(client_record, dict):
                raise LabFailure("keyboard XTEST driver returned invalid evidence")
            client_keysym = _exact_int(client_record.get("keysym"), positive=True)
            client_keyname = client_record.get("keysym_name")
            if client_keysym is None or not isinstance(client_keyname, str):
                raise LabFailure("keyboard XTEST driver returned invalid symbol evidence")
            client_packet_keyname = (
                r"(?:'(?:\\.|[^'\\\r\n])*'|\"(?:\\.|[^\"\\\r\n])*\")"
            )
            client_packet_prefix = (
                rf"do_send_keyboard\('key-action', {xpra_window_id}, "
                + client_packet_keyname
            )
            client_packet_suffix = (
                rf", \[[^\]\r\n]*\], {client_keysym}, "
                + re.escape(repr(item["expected_text"]))
                + rf", {physical_keycode}, {group}\)"
            )
            client_patterns = (
                client_packet_prefix + r", True" + client_packet_suffix,
                client_packet_prefix + r", False" + client_packet_suffix,
            )
            wait_for(
                f"clean Xpra client press/release trace for input {input_index + 1}",
                lambda offset=client_log_start, patterns=client_patterns: (
                    container_artifact_suffix_matches(
                        client,
                        "client.stdout",
                        offset,
                        patterns,
                    )
                ),
            )
            client_log_end = container_artifact_size(client, "client.stdout")
            input_index += 1
            expected_count = input_index

            def application_observed_input(count: int = expected_count) -> bool:
                events = read_keyboard_fixture_events(server)
                changed = [
                    event.get("text")
                    for event in events
                    if event.get("event") == "changed"
                ]
                if changed != expected_texts[: len(changed)]:
                    raise LabFailure(
                        "keyboard fixture observed missing, extra, or reordered text"
                    )
                releases = sum(
                    event.get("event") == "key-release" for event in events
                )
                return changed == expected_texts[:count] and releases == count

            wait_for(
                f"application text after keyboard input {input_index}",
                application_observed_input,
            )
            changed_texts = [
                event.get("text")
                for event in read_keyboard_fixture_events(server)
                if event.get("event") == "changed"
            ]
            if changed_texts != expected_texts[:expected_count]:
                raise LabFailure("keyboard fixture observation changed after acceptance")
            observed_text = changed_texts[-1]
            trace_patterns = (
                (
                    rf"get_keycode: pressed=True .* client-keycode={physical_keycode} "
                    rf"client-group={group} -> {physical_keycode}/{group}"
                ),
                (
                    rf"get_keycode: pressed=False .* client-keycode={physical_keycode} "
                    rf"client-group={group} -> {physical_keycode}/{group}"
                ),
                rf"wlr_seat_keyboard_notify_key\([^\n]+, {physical_keycode}, 1\)",
                rf"wlr_seat_keyboard_notify_key\([^\n]+, {physical_keycode}, 0\)",
            )

            def server_observed_input(
                offset: int = log_start,
                patterns: tuple[str, ...] = trace_patterns,
            ) -> bool:
                return container_artifact_suffix_matches(
                    server,
                    "server.stderr",
                    offset,
                    patterns,
                )

            wait_for(
                f"server keyboard trace for input {input_index}",
                server_observed_input,
            )
            log_end = container_artifact_size(server, "server.stderr")
            observed_texts.append(observed_text)
            phase_inputs.append(
                {
                    "application_text": observed_text,
                    "client": client_record,
                    "client_log_range": [client_log_start, client_log_end],
                    "expected_text": item["expected_text"],
                    "group": group,
                    "server_log_range": [log_start, log_end],
                }
            )
        write_command_output(
            server,
            [
                "xpra",
                "info",
                *command_cli_options("server", "info"),
            ],
            directory / info_name,
        )
        phases.append(
            {
                "client_query": query,
                "inputs": phase_inputs,
                "name": phase["name"],
                "rmlvo": rmlvo,
                "rmlvo_hash": rmlvo_hash,
                "server_application_range": [phase_log_start, structured_log_end],
                "server_info_artifact": info_name,
                "structured_update_range": [phase_log_start, structured_log_end],
            }
        )
        identity_snapshots.append(
            keyboard_identity_snapshot(server, server_pid, client, client_pid)
        )
    return {
        "attempted": True,
        "evidence": {
            "identity_snapshots": identity_snapshots,
            "phases": phases,
            "physical_keycode": scenario["phases"][0]["physical_keycode"],
            "scenario": {"data": scenario, "sha256": scenario_sha256},
            "schema": 1,
            "window_id": expected_window_id,
            "xpra_window_id": xpra_window_id,
        },
        "observed_texts": observed_texts,
    }


def finalize_wayland_keyboard_evidence(
    interaction: dict[str, Any],
    directory: Path,
    scenario: dict[str, Any],
    scenario_sha256: str,
) -> dict[str, Any]:
    evidence = interaction.get("evidence")
    if not isinstance(evidence, dict):
        raise LabFailure("keyboard live evidence is unavailable")
    server_log = directory / "server.stderr"
    client_log = directory / "client.stdout"
    for phase in evidence.get("phases", ()):
        application_range = phase.pop("server_application_range", None)
        structured_range = phase.pop("structured_update_range", None)
        if not isinstance(application_range, list) or len(application_range) != 2:
            raise LabFailure("keyboard configuration trace range is unavailable")
        if not isinstance(structured_range, list) or len(structured_range) != 2:
            raise LabFailure("keyboard structured-update trace range is unavailable")
        phase["server_application"] = parse_keyboard_server_application(
            server_log,
            application_range[0],
            application_range[1],
        )
        phase["structured_update"] = parse_keyboard_structured_update(
            server_log,
            structured_range[0],
            structured_range[1],
        )
        info_path = directory / phase["server_info_artifact"]
        phase["server_info"] = parse_keyboard_server_info(info_path)
        phase["server_info_sha256"] = sha256_file(info_path)
        for item in phase.get("inputs", ()):
            client_log_range = item.get("client_log_range")
            if not isinstance(client_log_range, list) or len(client_log_range) != 2:
                raise LabFailure("keyboard client trace range is unavailable")
            item["client_trace"] = parse_keyboard_client_trace(
                client_log,
                client_log_range[0],
                client_log_range[1],
            )
            log_range = item.get("server_log_range")
            if not isinstance(log_range, list) or len(log_range) != 2:
                raise LabFailure("keyboard input trace range is unavailable")
            item["server_trace"] = parse_keyboard_server_trace(
                server_log,
                log_range[0],
                log_range[1],
            )
    events = load_keyboard_fixture_events(directory / "keyboard-fixture.stdout")
    expected_texts = keyboard_expected_texts(scenario)
    evidence["application"] = {
        "events": events,
        "exit_status": process_exit_status(directory, "keyboard-fixture"),
        "final_text": events[-1].get("text") if events else "",
        "observed_texts": [
            event.get("text") for event in events if event.get("event") == "changed"
        ],
    }
    identities = evidence.get("identity_snapshots", ())
    process_unchanged = bool(
        identities
        and all(
            snapshot[role]["process"] == identities[0][role]["process"]
            for snapshot in identities[1:]
            for role in ("client", "server")
        )
    )
    connection_unchanged = bool(
        identities
        and all(
            snapshot[role]["connection"] == identities[0][role]["connection"]
            for snapshot in identities[1:]
            for role in ("client", "server")
        )
    )
    phase_hashes = [phase["rmlvo_hash"] for phase in evidence.get("phases", ())]
    evidence["runtime_replacement"] = {
        "after_hash": phase_hashes[-1] if phase_hashes else "",
        "application_observed_after_replacement": bool(
            len(phase_hashes) >= 2
            and evidence["application"]["final_text"] == expected_texts[-1]
        ),
        "before_hash": phase_hashes[0] if phase_hashes else "",
        "configuration_changed": len(set(phase_hashes)) == len(phase_hashes) >= 2,
        "connection_unchanged": connection_unchanged,
        "processes_unchanged": process_unchanged,
    }
    evidence["checks"] = keyboard_live_checks(
        evidence,
        scenario,
        scenario_sha256,
    )
    if not keyboard_embedded_checks_match(evidence, scenario, scenario_sha256):
        failed = sorted(name for name, passed in evidence["checks"].items() if not passed)
        detail = ", ".join(failed) if failed else "embedded check schema mismatch"
        raise LabFailure(f"keyboard live evidence failed: {detail}")
    if not keyboard_artifact_evidence_matches(evidence, directory):
        raise LabFailure("keyboard live evidence does not match retained artifacts")
    interaction["observed_texts"] = evidence["application"]["observed_texts"]
    return interaction


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
        directory,
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
            directory,
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


def load_empty_damage_fixture_events(path: Path) -> list[dict[str, Any]]:
    """Parse the bounded JSON event stream emitted by the native fixture."""
    ensure_private_regular_file(path)
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise LabFailure("empty-damage fixture event stream is unavailable") from error
    if not raw or len(raw) > 64 * 1024 or b"\0" in raw:
        raise LabFailure("empty-damage fixture event stream has an invalid size")
    try:
        lines = raw.decode("utf-8", errors="strict").splitlines()
    except UnicodeDecodeError as error:
        raise LabFailure("empty-damage fixture event stream is not UTF-8") from error
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            event = json.loads(
                line,
                object_pairs_hook=_json_object_without_duplicates,
            )
        except json.JSONDecodeError as error:
            raise LabFailure("empty-damage fixture event stream is not JSON") from error
        if not isinstance(event, dict) or not isinstance(event.get("event"), str):
            raise LabFailure("empty-damage fixture event is invalid")
        events.append(event)
    return events


def validate_empty_damage_fixture_events(
    events: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Validate the fixture's complete ordered application observation stream."""
    if type(events) is not list or any(type(event) is not dict for event in events):
        raise LabFailure("empty-damage fixture events are not an exact object list")
    event_names = ("ready", "pressure-ready", "child-click", "exit")
    if len(events) != len(event_names) or tuple(
        event.get("event") for event in events
    ) != event_names:
        raise LabFailure("empty-damage fixture events are missing, extra, or reordered")
    frame_keys = {"child_frames", "event", "monotonic_seconds", "parent_frames"}
    click_keys = {"event", "monotonic_seconds", "x", "y"}
    if any(set(event) != frame_keys for event in (events[0], events[1], events[3])):
        raise LabFailure("empty-damage fixture frame event fields are invalid")
    if set(events[2]) != click_keys:
        raise LabFailure("empty-damage fixture click event fields are invalid")

    def exact_finite_float(value: Any, *, nonnegative: bool = True) -> float | None:
        if type(value) is not float or not -float("inf") < value < float("inf"):
            return None
        if nonnegative and value < 0:
            return None
        return value

    timestamps = tuple(
        exact_finite_float(event.get("monotonic_seconds"))
        for event in events
    )
    if any(value is None for value in timestamps) or tuple(sorted(timestamps)) != timestamps:
        raise LabFailure("empty-damage fixture event timestamps are invalid")
    ready_parent = _exact_int(events[0].get("parent_frames"))
    ready_child = _exact_int(events[0].get("child_frames"))
    pressure_parent = _exact_int(events[1].get("parent_frames"), positive=True)
    pressure_child = _exact_int(events[1].get("child_frames"), positive=True)
    exit_parent = _exact_int(events[3].get("parent_frames"), positive=True)
    exit_child = _exact_int(events[3].get("child_frames"), positive=True)
    if ready_parent != 0 or ready_child != 0:
        raise LabFailure("empty-damage fixture recycled callbacks before pressure start")
    if (
        pressure_parent is None
        or pressure_child is None
        or pressure_parent < 60
        or pressure_child < 60
    ):
        raise LabFailure("empty-damage fixture pressure counts are invalid")
    if (
        exit_parent is None
        or exit_child is None
        or exit_parent < pressure_parent
        or exit_child < pressure_child
    ):
        raise LabFailure("empty-damage fixture exit counts regressed")
    if (
        exact_finite_float(events[2].get("x")) is None
        or exact_finite_float(events[2].get("y")) is None
    ):
        raise LabFailure("empty-damage fixture click coordinates are invalid")
    return dict(zip(event_names, events, strict=True))


def empty_damage_fixture_checks(evidence: dict[str, Any]) -> dict[str, bool]:
    """Classify the generic second-toplevel live regression evidence."""
    windows = evidence.get("windows") if isinstance(evidence, dict) else None
    pressure = evidence.get("pressure") if isinstance(evidence, dict) else None
    input_path = evidence.get("input_path") if isinstance(evidence, dict) else None
    teardown = evidence.get("teardown") if isinstance(evidence, dict) else None
    windows = windows if isinstance(windows, dict) else {}
    pressure = pressure if isinstance(pressure, dict) else {}
    input_path = input_path if isinstance(input_path, dict) else {}
    teardown = teardown if isinstance(teardown, dict) else {}
    parent_frames = _exact_int(pressure.get("parent_frames_at_marker"), positive=True)
    child_frames = _exact_int(pressure.get("child_frames_at_marker"), positive=True)
    validated_events: dict[str, dict[str, Any]] = {}
    events = evidence.get("events")
    if type(events) is list:
        try:
            validated_events = validate_empty_damage_fixture_events(events)
        except LabFailure:
            pass
    pressure_event = validated_events.get("pressure-ready", {})
    click_event = validated_events.get("child-click", {})
    click_position = evidence.get("click_position")
    click_position_valid = bool(
        type(click_position) is list
        and len(click_position) == 2
        and all(_exact_int(value) is not None and value >= 0 for value in click_position)
    )
    event_stream_exact = bool(
        validated_events
        and parent_frames == _exact_int(pressure_event.get("parent_frames"), positive=True)
        and child_frames == _exact_int(pressure_event.get("child_frames"), positive=True)
        and click_position_valid
        and abs(click_event["x"] - click_position[0]) <= 1
        and abs(click_event["y"] - click_position[1]) <= 1
    )
    click_elapsed = evidence.get("click_observed_after_seconds")
    click_elapsed_valid = bool(
        type(click_elapsed) is float
        and 0 <= click_elapsed < float("inf")
        and click_elapsed <= EMPTY_DAMAGE_INPUT_DEADLINE_SECONDS
    )
    return {
        "secondary_toplevels_discovered": bool(
            windows.get("client_ids_distinct") is True
            and windows.get("server_ids_distinct") is True
        ),
        "secondary_toplevels_visible": windows.get("visible_content") is True,
        "empty_damage_pressure_active": bool(
            pressure.get("marker") is True
            and pressure.get("parent_mapped_empty_commit") is True
            and pressure.get("child_mapped_empty_commit") is True
            and parent_frames is not None
            and child_frames is not None
            and parent_frames >= 60
            and child_frames >= 60
        ),
        "secondary_client_pointer_path": bool(
            input_path.get("client_press_release") is True
        ),
        "secondary_server_pointer_path": bool(
            input_path.get("server_child_focus") is True
            and input_path.get("server_coordinates") is True
            and input_path.get("server_press_release") is True
        ),
        "secondary_surface_pointer_path": bool(
            input_path.get("fixture_child_release") is True
            and input_path.get("fixture_coordinates") is True
        ),
        "secondary_pointer_response_bounded": bool(
            evidence.get("clicked_within_deadline") is True
            and "click_failure" in evidence
            and evidence["click_failure"] is None
            and click_elapsed_valid
        ),
        "secondary_toplevel_teardown": all(
            teardown.get(name) is True
            for name in (
                "client_destroy_logged",
                "client_windows_absent",
                "complete",
                "server_destroy_logged",
                "server_inventory_available",
                "server_windows_absent",
            )
        ),
        "secondary_fixture_event_stream_exact": event_stream_exact,
        "secondary_fixture_clean_exit": _exact_int(evidence.get("fixture_exit_status")) == 0,
    }


def exercise_empty_damage_fixture(
    server: str,
    client: str,
    directory: Path,
) -> dict[str, Any]:
    """Exercise a child toplevel while both fixture surfaces recycle empty commits."""
    if find_window(client, (EMPTY_DAMAGE_PARENT_TITLE,)) is not None:
        raise LabFailure("stale empty-damage parent window is visible")
    if find_window(client, (EMPTY_DAMAGE_CHILD_TITLE,)) is not None:
        raise LabFailure("stale empty-damage child window is visible")
    server_log_start = container_artifact_size(server, "server.stderr")
    marker_paths = " ".join(
        shlex.quote(value)
        for value in (
            EMPTY_DAMAGE_READY_MARKER,
            EMPTY_DAMAGE_START_MARKER,
            EMPTY_DAMAGE_PRESSURE_MARKER,
            EMPTY_DAMAGE_CLICK_MARKER,
        )
    )
    launcher = (
        f"umask 077; rm -f {marker_paths}; "
        "/usr/local/bin/xpra-empty-damage-fixture "
        ">/artifacts/empty-damage.stdout "
        "2>/artifacts/empty-damage.stderr & "
        "child=$!; printf '%s\\n' \"$child\" >/artifacts/empty-damage.pid; "
        "wait \"$child\"; status=$?; "
        "printf '%s\\n' \"$status\" >/artifacts/empty-damage.exit; "
        "exit \"$status\""
    )
    podman_exec(server, ["bash", "-lc", launcher], detach=True)
    pid_path = wait_for_container_artifact(
        server,
        directory,
        "empty-damage.pid",
        "empty-damage fixture PID publication",
    )
    pid_text = pid_path.read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[1-9][0-9]*", pid_text):
        raise LabFailure("empty-damage fixture published an invalid PID")
    fixture_pid = int(pid_text)

    def fixture_ready() -> bool:
        if not container_process_exists(server, fixture_pid):
            raise LabFailure("empty-damage fixture exited before readiness")
        return (
            podman_exec(
                server,
                ["test", "-f", EMPTY_DAMAGE_READY_MARKER],
                check=False,
                announce=False,
            ).returncode
            == 0
        )

    wait_for("mapped empty-damage fixture toplevels", fixture_ready)
    parent_window: tuple[str, str] | None = None
    child_window: tuple[str, str] | None = None

    def client_windows_ready() -> bool:
        nonlocal parent_window, child_window
        parent_window = find_window(client, (EMPTY_DAMAGE_PARENT_TITLE,))
        child_window = find_window(client, (EMPTY_DAMAGE_CHILD_TITLE,))
        return bool(
            parent_window is not None
            and child_window is not None
            and parent_window[0] != child_window[0]
        )

    wait_for("forwarded empty-damage parent and child windows", client_windows_ready)
    assert parent_window is not None and child_window is not None
    parent_geometry = window_geometry(client, parent_window[0])
    child_geometry = window_geometry(client, child_window[0])
    capture_xwd(
        client,
        directory,
        "empty-damage-parent.xwd",
        window_id=parent_window[0],
        announce=False,
    )
    parent_image = convert_xwd(directory, "empty-damage-parent")
    capture_xwd(
        client,
        directory,
        "empty-damage-child.xwd",
        window_id=child_window[0],
        announce=False,
    )
    child_image = convert_xwd(directory, "empty-damage-child")
    server_info_before = directory / "server-info-empty-damage-before.txt"
    write_command_output(
        server,
        ["xpra", "info", *command_cli_options("server", "info")],
        server_info_before,
    )
    parent_wid = server_xpra_window_id(
        server_info_before,
        (EMPTY_DAMAGE_PARENT_TITLE,),
    )
    child_wid = server_xpra_window_id(
        server_info_before,
        (EMPTY_DAMAGE_CHILD_TITLE,),
    )
    if parent_wid == child_wid:
        raise LabFailure("empty-damage fixture toplevels share one server window ID")

    podman_exec(
        server,
        [
            "sh",
            "-c",
            'umask 077; : > "$1"',
            "empty-damage-pressure-start",
            EMPTY_DAMAGE_START_MARKER,
        ],
    )

    def pressure_ready() -> bool:
        if not container_process_exists(server, fixture_pid):
            raise LabFailure("empty-damage fixture exited before pressure readiness")
        return (
            podman_exec(
                server,
                ["test", "-f", EMPTY_DAMAGE_PRESSURE_MARKER],
                check=False,
                announce=False,
            ).returncode
            == 0
        )

    wait_for("sustained empty-damage frame callbacks", pressure_ready, timeout=5)
    empty_commit_patterns = {
        "parent": rf"commit wid {parent_wid} mapped=True,[^\n]*rects=\[\]",
        "child": rf"commit wid {child_wid} mapped=True,[^\n]*rects=\[\]",
    }
    mapped_empty_commits = {
        name: container_artifact_suffix_matches(
            server,
            "server.stderr",
            server_log_start,
            (pattern,),
        )
        for name, pattern in empty_commit_patterns.items()
    }
    if not all(mapped_empty_commits.values()):
        raise LabFailure("empty-damage fixture did not reach mapped empty commits")

    click_x = child_geometry["width"] // 2
    click_y = child_geometry["height"] // 2
    client_click_offset = container_artifact_size(client, "client.stdout")
    server_click_offset = container_artifact_size(server, "server.stderr")
    click_started = time.monotonic()
    click_deadline = click_started + EMPTY_DAMAGE_INPUT_DEADLINE_SECONDS
    click_observed = click_started
    clicked = False
    click_failure: str | None = None
    try:
        remaining = click_deadline - time.monotonic()
        if remaining <= 0:
            raise LabFailure("empty-damage input deadline expired before injection")
        podman_exec(
            client,
            [
                "env",
                f"DISPLAY={CLIENT_DISPLAY}",
                "xdotool",
                "windowactivate",
                "--sync",
                child_window[0],
                "mousemove",
                "--sync",
                "--window",
                child_window[0],
                str(click_x),
                str(click_y),
                "click",
                "1",
            ],
            timeout=remaining,
        )

        def child_pointer_release_observed() -> bool:
            probe_remaining = click_deadline - time.monotonic()
            if probe_remaining <= 0:
                return False
            return (
                podman_exec(
                    server,
                    ["test", "-f", EMPTY_DAMAGE_CLICK_MARKER],
                    check=False,
                    announce=False,
                    timeout=probe_remaining,
                ).returncode
                == 0
            )

        remaining = click_deadline - time.monotonic()
        if remaining <= 0:
            raise LabFailure("empty-damage input deadline expired after injection")
        wait_for(
            "empty-damage child pointer release",
            child_pointer_release_observed,
            timeout=remaining,
        )
        click_observed = time.monotonic()
        clicked = click_observed <= click_deadline
        if not clicked:
            click_failure = "empty-damage pointer release exceeded the input deadline"
    except subprocess.TimeoutExpired:
        click_observed = time.monotonic()
        click_failure = (
            "empty-damage input command exceeded the "
            f"{EMPTY_DAMAGE_INPUT_DEADLINE_SECONDS:g}s deadline"
        )
    except LabFailure as error:
        click_observed = time.monotonic()
        click_failure = str(error)
    click_observed_after = round(click_observed - click_started, 6)
    if not clicked:
        podman_exec(
            server,
            ["kill", "-TERM", str(fixture_pid)],
            check=False,
            announce=False,
        )
    fixture_exit_status = wait_for_process_exit(
        server,
        fixture_pid,
        directory,
        "empty-damage",
        timeout=15,
    )

    def client_windows_absent() -> bool:
        return (
            find_window(client, (EMPTY_DAMAGE_PARENT_TITLE,)) is None
            and find_window(client, (EMPTY_DAMAGE_CHILD_TITLE,)) is None
        )

    wait_for("empty-damage fixture window removal", client_windows_absent, timeout=15)
    server_info_after = directory / "server-info-empty-damage-after.txt"
    server_info_after_result = write_command_output(
        server,
        ["xpra", "info", *command_cli_options("server", "info")],
        server_info_after,
        check=False,
    )
    server_inventory_available = server_info_after_result.returncode == 0
    after_inventory = (
        server_xpra_window_inventory(server_info_after)
        if server_inventory_available
        else {}
    )
    input_path = {
        "client_press_release": container_artifact_suffix_matches(
            client,
            "client.stdout",
            client_click_offset,
            (
                rf"_button_action\(1,[^\n]+, True\) wid=0x{child_wid:x}",
                rf"_button_action\(1,[^\n]+, False\) wid=0x{child_wid:x}",
            ),
        ),
        "server_child_focus": container_artifact_suffix_matches(
            server,
            "server.stderr",
            server_click_offset,
            (re.escape(f"set_pointer_focus({child_wid},"),),
        ),
        "server_coordinates": container_artifact_suffix_matches(
            server,
            "server.stderr",
            server_click_offset,
            (re.escape(f"move_pointer({click_x}, {click_y},"),),
        ),
        "server_press_release": container_artifact_suffix_matches(
            server,
            "server.stderr",
            server_click_offset,
            (re.escape("click(1, True"), re.escape("click(1, False")),
        ),
    }
    teardown = {
        "client_windows_absent": client_windows_absent(),
        "server_inventory_available": server_inventory_available,
        "server_windows_absent": bool(
            server_inventory_available
            and parent_wid not in after_inventory
            and child_wid not in after_inventory
        ),
        "client_destroy_logged": container_artifact_suffix_matches(
            client,
            "client.stdout",
            client_click_offset,
            (
                re.escape(f"destroy_window({parent_wid}#x,"),
                re.escape(f"destroy_window({child_wid}#x,"),
            ),
        ),
        "server_destroy_logged": container_artifact_suffix_matches(
            server,
            "server.stderr",
            server_click_offset,
            (
                re.escape(f"_emit(destroy, ({parent_wid},))"),
                re.escape(f"_emit(destroy, ({child_wid},))"),
            ),
        ),
    }
    teardown["complete"] = all(teardown.values())
    pull_container_artifacts(
        server,
        directory,
        ("empty-damage.stdout", "empty-damage.stderr"),
    )
    event_list = load_empty_damage_fixture_events(directory / "empty-damage.stdout")
    validated_events: dict[str, dict[str, Any]] = {}
    try:
        validated_events = validate_empty_damage_fixture_events(event_list)
    except LabFailure:
        # Preserve the bounded raw observations in the failure report. The
        # classifier below requires an exact complete stream, so partial or
        # malformed semantics cannot become acceptance evidence.
        pass
    pressure_event = validated_events.get("pressure-ready", {})
    click_event = validated_events.get("child-click", {})
    click_event_x = click_event.get("x")
    click_event_y = click_event.get("y")
    input_path.update(
        {
            "fixture_child_release": bool(click_event),
            "fixture_coordinates": bool(
                type(click_event_x) is float
                and type(click_event_y) is float
                and abs(click_event_x - click_x) <= 1
                and abs(click_event_y - click_y) <= 1
            ),
        }
    )
    evidence = {
        "attempted": True,
        "artifacts": {
            "child_screenshot": "empty-damage-child.rgb.png",
            "events": "empty-damage.stdout",
            "parent_screenshot": "empty-damage-parent.rgb.png",
            "server_info_after": server_info_after.name,
            "server_info_after_returncode": server_info_after_result.returncode,
            "server_info_before": server_info_before.name,
            "stderr": "empty-damage.stderr",
        },
        "clicked_within_deadline": clicked,
        "click_failure": click_failure,
        "click_observed_after_seconds": click_observed_after,
        "click_position": [click_x, click_y],
        "events": event_list,
        "fixture_exit_status": fixture_exit_status,
        "input_path": input_path,
        "pid": fixture_pid,
        "pressure": {
            "marker": bool(pressure_event),
            "parent_frames_at_marker": pressure_event.get("parent_frames", 0),
            "child_frames_at_marker": pressure_event.get("child_frames", 0),
            "parent_mapped_empty_commit": mapped_empty_commits["parent"],
            "child_mapped_empty_commit": mapped_empty_commits["child"],
        },
        "teardown": teardown,
        "windows": {
            "child": {
                "client_geometry": child_geometry,
                "client_id": child_window[0],
                "client_title": child_window[1],
                "server_id": child_wid,
            },
            "client_ids_distinct": parent_window[0] != child_window[0],
            "parent": {
                "client_geometry": parent_geometry,
                "client_id": parent_window[0],
                "client_title": parent_window[1],
                "server_id": parent_wid,
            },
            "server_ids_distinct": parent_wid != child_wid,
            "visible_content": bool(
                parent_image["xwd"]["unique_rgb_colors"] > 1
                and child_image["xwd"]["unique_rgb_colors"] > 1
            ),
        },
    }
    evidence["checks"] = empty_damage_fixture_checks(evidence)
    return evidence


def interaction_alpha_content_checks(
    interaction: dict[str, Any],
) -> dict[str, bool]:
    """Prove source alpha and visible composited GTK frames before and after input."""
    source = interaction.get("source_alpha")
    checks = {
        "interaction_source_screenshots_present": bool(
            isinstance(source, dict)
            and _exact_int(source.get("count"), positive=True) is not None
        ),
        "interaction_source_has_transparent_pixels": bool(
            isinstance(source, dict) and source.get("all_have_transparent_pixels")
        ),
        "interaction_source_has_opaque_pixels": bool(
            isinstance(source, dict) and source.get("all_have_opaque_pixels")
        ),
    }
    for phase in ("before", "after"):
        capture = interaction.get(phase)
        xwd = capture.get("xwd") if isinstance(capture, dict) else None
        checks[f"interaction_{phase}_visible_content"] = bool(
            isinstance(xwd, dict)
            and _exact_int(xwd.get("unique_rgb_colors"), positive=True) is not None
            and int(xwd["unique_rgb_colors"]) > 10
        )
    return checks


def saved_source_alpha_evidence(
    directory: Path,
    updates: dict[str, Any] | None,
) -> dict[str, Any]:
    """Measure alpha in collected screenshots scoped to one exact source window."""
    if not isinstance(updates, dict):
        raise LabFailure("interaction source updates are unavailable")
    window_id = _exact_int(updates.get("window_id"), positive=True)
    screenshots = updates.get("screenshots")
    if window_id is None or not isinstance(screenshots, list) or not screenshots:
        raise LabFailure("interaction source screenshots are unavailable")
    metrics: list[dict[str, Any]] = []
    for relative in screenshots:
        if not isinstance(relative, str):
            raise LabFailure("interaction source screenshot path is invalid")
        path = PurePosixPath(relative)
        if (
            path.is_absolute()
            or path.as_posix() != relative
            or len(path.parts) != 4
            or path.parts[0] != "screen-updates"
            or path.parts[1] != str(window_id)
            or re.fullmatch(r"(?:0|[1-9][0-9]*)", path.parts[2]) is None
            or path.parts[3] != "screenshot.png"
        ):
            raise LabFailure(f"interaction source screenshot path is unsafe: {relative}")
        local = directory / relative
        try:
            with Image.open(local) as image:
                metrics.append(analyze_image(image.convert("RGBA")))
        except OSError as error:
            raise LabFailure(
                f"interaction source screenshot is invalid: {relative}"
            ) from error
    return {
        "all_have_opaque_pixels": all(
            metric["alpha_maximum"] == 255 for metric in metrics
        ),
        "all_have_transparent_pixels": all(
            metric["alpha_minimum"] < 255 for metric in metrics
        ),
        "count": len(metrics),
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
        return bool(
            container_artifact_contains(
                container,
                "sway-child.env",
                f"DISPLAY={CLIENT_DISPLAY}",
            )
            and container_artifact_contains(
                container,
                "xwayland-xdpyinfo.txt",
                "name of display:",
            )
        )

    wait_for("Sway Xwayland display", xwayland_ready)
    pull_container_artifacts(
        container,
        directory,
        ("sway-child.env", "xwayland-xdpyinfo.txt"),
    )
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

    renderer = ""

    def hardware_renderer_ready() -> bool:
        nonlocal renderer
        result = podman_exec(
            container,
            ["grep", "--fixed-strings", "GL renderer:", "/artifacts/sway.stderr"],
            check=False,
            announce=False,
        )
        if result.returncode:
            return False
        match = re.search(r"GL renderer: ([^\n]+)", result.stdout)
        renderer = match.group(1).strip() if match else ""
        return bool(
            container_artifact_contains(
                container,
                "sway.stderr",
                str(render_node),
            )
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
    capture_xwd(container, directory, "root-before.xwd")
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
        client_exit_status = lifecycle.get("client_exit_status")
        return {
            "client_exit_zero": (
                type(client_exit_status) is int and client_exit_status == 0
            ),
            "client_exited_after_server": lifecycle.get("client_exited_after_server")
            is True,
            "server_exited_after_application": lifecycle.get(
                "server_exited_after_application"
            )
            is True,
        }
    after_identity_field = (
        "application_identity_after_detach"
        if lifecycle_profile == "detach"
        else "application_identity_after_transport_loss"
    )
    initial_identity = lifecycle.get("application_identity_at_capture")
    survived_identity = lifecycle.get(after_identity_field)
    before_termination = lifecycle.get("application_identity_before_termination")
    server_after_identity_field = (
        "server_identity_after_detach"
        if lifecycle_profile == "detach"
        else "server_identity_after_transport_loss"
    )
    initial_server_identity = lifecycle.get("server_identity_at_capture")
    survived_server_identity = lifecycle.get(server_after_identity_field)
    server_before_termination = lifecycle.get(
        "server_identity_before_application_termination"
    )
    termination = lifecycle.get("application_termination")
    identity_shape_exact = bool(
        valid_interaction_fixture_identity(initial_identity)
        and valid_interaction_fixture_identity(survived_identity)
        and valid_interaction_fixture_identity(before_termination)
    )
    identity_unchanged = bool(
        identity_shape_exact
        and initial_identity == survived_identity == before_termination
    )
    server_identity_shape_exact = bool(
        valid_process_identity(initial_server_identity)
        and valid_process_identity(survived_server_identity)
        and valid_process_identity(server_before_termination)
    )
    server_identity_unchanged = bool(
        server_identity_shape_exact
        and initial_server_identity
        == survived_server_identity
        == server_before_termination
    )
    server_pid = lifecycle.get("server_pid")
    fixture_distinct_from_server = bool(
        identity_shape_exact
        and server_identity_shape_exact
        and server_pid == initial_server_identity["pid"]
        and initial_identity["pid"] != server_pid
    )
    exact_termination = bool(
        isinstance(termination, dict)
        and set(termination)
        == {
            "identity",
            "pidfd",
            "returncode",
            "server_identity",
            "server_pidfd",
            "signal",
        }
        and termination.get("identity") == before_termination
        and termination.get("pidfd") is True
        and termination.get("server_identity") == server_before_termination
        and termination.get("server_pidfd") is True
        and type(termination.get("returncode")) is int
        and termination["returncode"] == 0
        and termination.get("signal") == "SIGTERM"
    )
    identity_checks = {
        "fixture_identity_published": identity_shape_exact,
        "fixture_identity_unchanged": identity_unchanged,
        "server_identity_published": server_identity_shape_exact,
        "server_identity_unchanged": server_identity_unchanged,
        "fixture_distinct_from_server": fixture_distinct_from_server,
        "server_alive_before_application_termination": (
            lifecycle.get("server_alive_before_application_termination") is True
        ),
        "exact_fixture_termination": exact_termination,
        "application_exited_after_termination": (
            lifecycle.get("application_exited_after_termination") is True
        ),
    }
    if lifecycle_profile == "detach":
        detach_returncode = lifecycle.get("detach_returncode")
        client_exit_status = lifecycle.get("client_exit_status")
        return {
            "detach_command_succeeded": (
                type(detach_returncode) is int and detach_returncode == 0
            ),
            "client_exit_zero": (
                type(client_exit_status) is int and client_exit_status == 0
            ),
            "client_exited_after_detach": lifecycle.get("client_exited_after_detach")
            is True,
            "server_survived_detach": lifecycle.get("server_survived_detach") is True,
            "application_survived_detach": (
                lifecycle.get("application_survived_detach") is True
                and identity_unchanged
            ),
            **identity_checks,
            "server_exited_after_application": lifecycle.get(
                "server_exited_after_application"
            )
            is True,
        }
    if lifecycle_profile != "transport-loss":
        raise LabFailure(f"unsupported lifecycle profile: {lifecycle_profile}")
    exit_status = lifecycle.get("client_exit_status")
    disconnect_returncode = lifecycle.get("transport_disconnect_returncode")
    return {
        "transport_disconnect_succeeded": (
            type(disconnect_returncode) is int and disconnect_returncode == 0
        ),
        "client_exit_nonzero": isinstance(exit_status, int)
        and not isinstance(exit_status, bool)
        and exit_status != 0,
        "client_exited_after_transport_loss": lifecycle.get(
            "client_exited_after_transport_loss"
        )
        is True,
        "server_survived_transport_loss": lifecycle.get(
            "server_survived_transport_loss"
        )
        is True,
        "application_survived_transport_loss": (
            lifecycle.get("application_survived_transport_loss") is True
            and identity_unchanged
        ),
        **identity_checks,
        "server_exited_after_application": lifecycle.get(
            "server_exited_after_application"
        )
        is True,
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
    if application in MULTIWINDOW_HARDWARE_APPLICATIONS:
        fixture = hardware_fixture_spec(application)
        checks = {
            "process_alive_at_capture": (
                application_activity.get("process_alive") is True
            ),
            "render_node_open": render_node_open,
            "graphics_frames_changed": bool(
                application_activity.get("graphics_motion", {}).get("changed")
            ),
        }
        if fixture.api == "vulkan":
            checks["radv_mapped"] = any(
                "libvulkan_radeon" in mapping
                for mapping in application_gpu["gpu_mappings"]
            )
            return checks
        opengl = application_activity.get("opengl")
        evidence = opengl if isinstance(opengl, dict) else {}
        renderer = str(evidence.get("renderer", ""))
        vendor = str(evidence.get("vendor", ""))
        renderer_lower = renderer.casefold()
        software = any(
            token in renderer_lower
            for token in ("llvmpipe", "softpipe", "swrast", "software rasterizer")
        )
        checks.update(
            {
                "opengl_context_reported": bool(
                    renderer
                    and evidence.get("api") == "OpenGL"
                    and evidence.get("source") == "glmark2-wayland"
                    and evidence.get("version")
                ),
                "opengl_hardware_renderer": bool(
                    not software
                    and ("amd" in renderer_lower or "radeonsi" in renderer_lower)
                    and "amd" in vendor.casefold()
                ),
                "opengl_driver_mapped": any(
                    "radeonsi_dri" in mapping or "libgallium" in mapping
                    for mapping in application_gpu["gpu_mappings"]
                ),
            }
        )
        return checks
    radv_mapped = any(
        "libvulkan_radeon" in mapping for mapping in application_gpu["gpu_mappings"]
    )
    if application == "vkcube":
        return {
            "process_alive_at_capture": (
                application_activity.get("process_alive") is True
            ),
            "render_node_open": render_node_open,
            "radv_mapped": radv_mapped,
            "graphics_frames_changed": bool(
                application_activity.get("graphics_motion", {}).get("changed")
            ),
        }
    if application != "zed":
        return {
            "process_alive_at_capture": (
                application_activity.get("process_alive") is True
            )
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
        packet_contract_name = primary_h264_packet_contract_name(
            args.application, args.h264_client_policy
        )
        packet_contract_passed = primary_h264_packets_valid(
            args.application,
            args.h264_client_policy,
            updates,
        )
        all_production_streams_proven = bool(
            production.get("all_streams_proven")
            if args.h264_client_policy == "adaptive-alpha"
            else production.get("production_proven")
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
            "production_streams_match_encoder_context": (
                all_production_streams_proven
            ),
        }
        if args.h264_client_policy == "adaptive-alpha":
            primary_metrics = codec_hardware.get("h264_primary", {})
            encoding_checks.update(
                h264_dominance_checks(primary_metrics)
            )
            if args.application in MULTIWINDOW_HARDWARE_APPLICATIONS:
                encoding_checks.update(
                    matched_h264_stream_stability_checks(
                        production,
                        primary_metrics,
                    )
                )
        if args.application in MULTIWINDOW_HARDWARE_APPLICATIONS:
            encoding_checks["interaction_window_alpha_capable_packets"] = (
                only_positive_alpha_capable_packets(interaction_updates)
            )
            encoding_checks.update(
                hardware_frame_alpha_state_checks(
                    log_evidence,
                    updates,
                    interaction_updates,
                )
            )
        elif args.h264_client_policy == "adaptive-alpha":
            encoding_checks.update(
                adaptive_frame_alpha_state_checks(log_evidence, updates)
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
            "opengl_presented": bool(packet_chain.get("presented_before_later_ack")),
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
    }
    if args.application == "gtk":
        final_checks.update(
            image_alpha_content_checks(direct_image, prefix="window")
        )
    else:
        final_checks["window_central_alpha_opaque"] = (
            direct_image["central_opaque_ratio"] >= 0.99
        )
    if args.application in MULTIWINDOW_HARDWARE_APPLICATIONS:
        final_checks["source_viewport_placement_logged"] = bool(
            pixel_evidence.get("source_viewport_placement_logged")
        )
    lifecycle_checks = lifecycle_boundary_checks(args.lifecycle, lifecycle)
    if args.application == "zed":
        interaction_checks = {
            "dark_theme_selected_by_pointer": bool(
                interaction.get("dark_theme_selected")
            ),
            "pointer_changed_pixels": bool(interaction.get("pixels_changed")),
        }
        if args.encoding == "rgb" and args.lifecycle == "application-exit":
            secondary = interaction.get("empty_damage_fixture")
            interaction_checks.update(
                empty_damage_fixture_checks(
                    secondary if isinstance(secondary, dict) else {}
                )
            )
    elif args.application in MULTIWINDOW_HARDWARE_APPLICATIONS | {"gtk"}:
        interaction_checks = {
            "pointer_marker_present": bool(interaction.get("pointer_marker_present")),
            "pointer_changed_pixels": bool(interaction.get("pointer_changed_pixels")),
        }
        if args.application in MULTIWINDOW_HARDWARE_APPLICATIONS:
            interaction_checks.update(interaction_alpha_content_checks(interaction))
        if args.lifecycle == "application-exit":
            interaction_checks["keyboard_escape_received"] = bool(
                interaction.get("keyboard_escape_received")
            )
    elif args.application == "keyboard":
        keyboard_checks = interaction.get("evidence", {}).get("checks", {})
        check_schema_exact = bool(
            isinstance(keyboard_checks, dict)
            and set(keyboard_checks) == set(KEYBOARD_LIVE_CHECK_NAMES)
        )
        interaction_checks = {
            name: check_schema_exact and keyboard_checks.get(name) is True
            for name in KEYBOARD_LIVE_CHECK_NAMES
        }
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
    keyboard_scenario: dict[str, Any] | None,
    keyboard_scenario_sha256: str | None,
    zed_archive: Path | None,
    zed_archive_sha256: str | None,
) -> dict[str, Any]:
    if (args.application == "keyboard") != (
        isinstance(keyboard_scenario, dict)
        and isinstance(keyboard_scenario_sha256, str)
        and re.fullmatch(r"[0-9a-f]{64}", keyboard_scenario_sha256) is not None
    ):
        raise LabFailure("keyboard application and frozen scenario inputs do not match")
    directory = result_directory / scenario.name
    directory.mkdir(mode=0o700)
    ensure_private_directory(directory)
    suffix = uuid.uuid4().hex[:10]
    network = f"xpra-fork-maintenance-live-{suffix}"
    server = f"xpra-fork-maintenance-live-server-{suffix}"
    client = f"xpra-fork-maintenance-live-client-{suffix}"
    containers: list[str] = []
    container_labels = {
        server: {
            "io.xpra.fork-maintenance.context": server_context_digest,
            "io.xpra.fork-maintenance.image-id": server_image_id,
            "io.xpra.fork-maintenance.owner": "live",
            "io.xpra.fork-maintenance.role": "server",
            "io.xpra.fork-maintenance.run-id": run_id,
            "io.xpra.fork-maintenance.scenario": scenario.name,
            "io.xpra.fork-maintenance.source": commit,
        },
        client: {
            "io.xpra.fork-maintenance.context": client_context_digest,
            "io.xpra.fork-maintenance.image-id": client_image_id,
            "io.xpra.fork-maintenance.owner": "live",
            "io.xpra.fork-maintenance.role": "client",
            "io.xpra.fork-maintenance.run-id": run_id,
            "io.xpra.fork-maintenance.scenario": scenario.name,
            "io.xpra.fork-maintenance.source": commit,
        },
    }
    network_labels = {
        "io.xpra.fork-maintenance.owner": "live",
        "io.xpra.fork-maintenance.role": "network",
        "io.xpra.fork-maintenance.run-id": run_id,
        "io.xpra.fork-maintenance.scenario": scenario.name,
    }
    ledger_path = directory / "podman-objects.json"
    ledger: dict[str, Any] = {
        "objects": {
            "client": {
                "id": "",
                "kind": "container",
                "labels": container_labels[client],
                "name": client,
                "state": "planned",
            },
            "network": {
                "id": "",
                "kind": "network",
                "labels": network_labels,
                "name": network,
                "state": "planned",
            },
            "server": {
                "id": "",
                "kind": "container",
                "labels": container_labels[server],
                "name": server,
                "state": "planned",
            },
        },
        "owner": "live",
        "run_id": run_id,
        "scenario": scenario.name,
        "schema": 1,
    }
    replace_private_json(ledger_path, ledger)

    def bind_object(role: str) -> str:
        item = ledger["objects"][role]
        object_id, labels = inspect_podman_object(item["kind"], item["name"])
        if {key: labels.get(key) for key in item["labels"]} != item["labels"]:
            raise LabFailure(f"created live {role} labels do not match its ledger")
        item.update({"id": object_id, "labels": labels, "state": "created"})
        replace_private_json(ledger_path, ledger)
        return object_id

    network_created = False
    workloads_exited = False
    server_pid = 0
    client_pid = 0
    collected_containers: set[str] = set()
    report: dict[str, Any] = {"name": scenario.name, "result": "failed"}
    try:
        run(
            [
                "podman",
                "network",
                "create",
                "--label",
                "io.xpra.fork-maintenance.owner=live",
                "--label",
                f"io.xpra.fork-maintenance.run-id={run_id}",
                "--label",
                f"io.xpra.fork-maintenance.scenario={scenario.name}",
                "--label",
                "io.xpra.fork-maintenance.role=network",
                network,
            ]
        )
        network_created = True
        bind_object("network")
        server_run = [
            "podman",
            "run",
            "--detach",
            "--name",
            server,
            "--label",
            "io.xpra.fork-maintenance.owner=live",
            "--label",
            f"io.xpra.fork-maintenance.run-id={run_id}",
            "--label",
            f"io.xpra.fork-maintenance.scenario={scenario.name}",
            "--label",
            "io.xpra.fork-maintenance.role=server",
            "--label",
            f"io.xpra.fork-maintenance.source={commit}",
            "--label",
            f"io.xpra.fork-maintenance.context={server_context_digest}",
            "--label",
            f"io.xpra.fork-maintenance.image-id={server_image_id}",
            "--network",
            network,
            "--network-alias",
            "xpra-server",
            *live_user_options(),
            "--device",
            f"{args.render_node}:{args.render_node}",
            "--group-add",
            "keep-groups",
            "--shm-size",
            "1g",
            "--env",
            "XDG_RUNTIME_DIR=/tmp/server-runtime",
        ]
        server_run.append(server_image_id)
        containers.append(server)
        server_created = run(server_run).stdout.strip()
        server_id = bind_object("server")
        if server_created != server_id:
            raise LabFailure("server container create output does not match its immutable ID")
        verify_container_image(server, server_image_id)
        if args.application == "zed":
            if zed_archive is None or zed_archive_sha256 is None:
                raise LabFailure("frozen Zed payload is unavailable")
            send_zed_payload(server, zed_archive, zed_archive_sha256)

        client_run = [
            "podman",
            "run",
            "--detach",
            "--name",
            client,
            "--label",
            "io.xpra.fork-maintenance.owner=live",
            "--label",
            f"io.xpra.fork-maintenance.run-id={run_id}",
            "--label",
            f"io.xpra.fork-maintenance.scenario={scenario.name}",
            "--label",
            "io.xpra.fork-maintenance.role=client",
            "--label",
            f"io.xpra.fork-maintenance.source={commit}",
            "--label",
            f"io.xpra.fork-maintenance.context={client_context_digest}",
            "--label",
            f"io.xpra.fork-maintenance.image-id={client_image_id}",
            "--network",
            network,
            *live_user_options(),
            "--shm-size",
            "1g",
            "--env",
            "XDG_RUNTIME_DIR=/tmp/client-runtime",
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
        client_created = run(client_run).stdout.strip()
        client_id = bind_object("client")
        if client_created != client_id:
            raise LabFailure("client container create output does not match its immutable ID")
        verify_container_image(client, client_image_id)

        for container, runtime in (
            (server, "/tmp/server-runtime"),
            (client, "/tmp/client-runtime"),
        ):
            podman_exec(container, ["install", "-d", "-m", "0700", runtime])
            podman_exec(container, ["test", "-w", "/artifacts"])

        versions = {
            "server": podman_exec(
                server,
                ["xpra", *command_cli_options("server", "version")],
            ).stdout.strip(),
            "client": podman_exec(
                client,
                ["xpra", *command_cli_options("client", "version")],
            ).stdout.strip(),
        }
        operating_systems = {
            "server": read_os_release(server),
            "client": read_os_release(client),
        }
        if args.application in {"zed", "hardware", "vkcube"}:
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
        if args.application == "keyboard":
            assert keyboard_scenario is not None
            # Seed the clean client's real X11 display from scenario data
            # before attach.  Using the final phase as the baseline guarantees
            # that each later phase changes its model as well as its layouts,
            # so legacy layout-changed cannot pre-empt the structured update.
            configure_client_xkb(
                client,
                keyboard_scenario["phases"][-1]["rmlvo"],
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
            *static_cli_options("server", "base"),
            f"--bind-tcp=0.0.0.0:{SERVER_PORT},auth=none",
            (
                f"--session-name={args.application}-{args.encoding}-"
                f"{args.h264_client_policy}-fork-maintenance"
            ),
            *static_cli_options("server", "lifecycle"),
            f"--start-child={child_command}",
            *encoding_options,
            *static_cli_options("server", "diagnostics"),
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
        server_pid_path = wait_for_container_artifact(
            server,
            directory,
            "server.pid",
            "server PID publication",
        )
        server_pid = int(server_pid_path.read_text().strip())
        wait_for_log(
            server,
            server_pid,
            directory / "server.stderr",
            "xpra is ready.",
            "Wayland Xpra server readiness",
        )
        wait_for_server_tcp_endpoint(
            server,
            server_pid,
            client,
            "xpra-server",
            SERVER_PORT,
            directory / "server.stderr",
        )
        if args.application in MULTIWINDOW_HARDWARE_APPLICATIONS:
            wait_for_hardware_fixture(
                server,
                server_pid,
                directory,
                args.application,
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
            proxy_pid_path = wait_for_container_artifact(
                client,
                directory,
                "transport-proxy.pid",
                "transport proxy PID publication",
            )
            transport_proxy_pid = int(proxy_pid_path.read_text().strip())

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
            *static_cli_options("client", "base"),
            *client_network_options(args.network_profile),
            *client_encoding_options,
            *static_cli_options("client", "diagnostics"),
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
        client_pid_path = wait_for_container_artifact(
            client,
            directory,
            "client.pid",
            "client PID publication",
        )
        client_pid = int(client_pid_path.read_text().strip())

        found: tuple[str, str] | None = None

        def application_window_ready() -> bool:
            nonlocal found
            if not container_process_exists(client, client_pid):
                if container_artifact_exists(client, "client.stderr"):
                    pull_container_artifacts(client, directory, ("client.stderr",))
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
        write_command_output(
            server,
            [
                "xpra",
                "info",
                *command_cli_options("server", "info"),
            ],
            directory / "server-info.txt",
            check=False,
        )
        xpra_wid = server_xpra_window_id(directory / "server-info.txt", title_patterns)
        frame_outcome = wait_for_frame_boundary(
            server,
            server_pid,
            client,
            client_pid,
            directory,
            args.encoding,
            args.h264_client_policy,
            application=args.application,
            expected_xpra_wid=xpra_wid,
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
            application=args.application,
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
                directory,
                "window-focused-screen.xwd",
                window_id=window_id,
                screen=True,
            )
            capture_xwd(client, directory, "root-after.xwd")
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
        application_identity: dict[str, Any] | None = None
        server_process_identity: dict[str, Any] | None = None
        application_process_alive = False
        if args.application == "gtk":
            if pid_file != INTERACTION_IDENTITY_ARTIFACT:
                raise LabFailure("GTK application identity artifact is misconfigured")
            application_identity_path = wait_for_container_artifact(
                server,
                directory,
                pid_file,
                "interaction fixture identity publication",
            )
            application_identity = load_interaction_fixture_identity(
                application_identity_path
            )
            application_pid = application_identity["pid"]
            server_process_identity = container_process_identity(server, server_pid)
            if server_process_identity is None:
                raise LabFailure("Xpra server exited before application capture")
            require_process_identity(
                server,
                server_process_identity,
                role="Xpra server",
            )
            require_interaction_fixture_identity(
                server,
                application_identity,
                server_pid=server_process_identity["pid"],
            )
            application_process_alive = True
        elif pid_file:
            application_pid_path = wait_for_container_artifact(
                server,
                directory,
                pid_file,
                "application PID publication",
            )
            application_pid = int(application_pid_path.read_text().strip())
        elif args.application == "vkcube":
            pgrep = podman_exec(
                server,
                ["pgrep", "--oldest", "--exact", "vkcube"],
            )
            application_pid = int(pgrep.stdout.strip().splitlines()[0])
        else:
            raise LabFailure("application process identity contract is unavailable")
        application_gpu = process_gpu_evidence(server, application_pid)
        if application_identity is not None and not process_gpu_evidence_matches_identity(
            application_gpu,
            application_identity,
        ):
            raise LabFailure("GTK process evidence does not match its published identity")
        application_activity: dict[str, Any] = {
            "process_alive": (
                application_process_alive
                if application_identity is not None
                else container_process_exists(server, application_pid)
            )
        }
        if application_identity is not None:
            application_activity["process_identity"] = application_identity
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
        hardware_h264_interval: dict[str, Any] | None = None
        if args.application == "zed" and frame_outcome in {
            "success",
            "picture-fallback",
        }:
            interaction = exercise_zed_mouse(
                server,
                client,
                window_id,
                geometry,
                directory,
                direct,
            )
            interaction["attempted"] = True
            if args.encoding == "h264":
                interaction["h264_stimulus"] = exercise_zed_h264_stability(
                    server,
                    client,
                    window_id,
                    xpra_wid,
                    geometry,
                    directory,
                    interaction,
                )
            elif args.lifecycle == "application-exit":
                interaction["empty_damage_fixture"] = exercise_empty_damage_fixture(
                    server,
                    client,
                    directory,
                )
        elif args.application in MULTIWINDOW_HARDWARE_APPLICATIONS | {"vkcube"}:
            application_activity["graphics_motion"] = capture_graphics_motion(
                client, window_id, directory, direct
            )
            if (
                args.application in MULTIWINDOW_HARDWARE_APPLICATIONS
                and args.encoding == "h264"
            ):
                hardware_h264_interval = begin_hardware_h264_stimulus(
                    server,
                    directory,
                    xpra_wid,
                )

        interaction_window: tuple[str, str] | None = None
        interaction_xpra_wid: int | None = None
        if args.application in MULTIWINDOW_HARDWARE_APPLICATIONS:

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
                    *command_cli_options("server", "info"),
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
                close_with_keyboard=False,
            )
            if hardware_h264_interval is None:
                raise LabFailure("hardware H.264 phase baseline is unavailable")
            interaction["h264_stimulus"] = finish_hardware_h264_stimulus(
                server,
                directory,
                xpra_wid,
                hardware_h264_interval,
            )
            podman_exec(
                client,
                [
                    "env",
                    f"DISPLAY={CLIENT_DISPLAY}",
                    "xdotool",
                    "windowactivate",
                    "--sync",
                    interaction_window[0],
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
            interaction["keyboard_escape_received"] = True
        elif args.application == "gtk":
            interaction = exercise_interaction_fixture(
                server,
                client,
                window_id,
                directory,
                close_with_keyboard=args.lifecycle == "application-exit",
            )
        elif args.application == "keyboard":
            assert keyboard_scenario is not None
            assert keyboard_scenario_sha256 is not None
            interaction = exercise_wayland_keyboard(
                server,
                server_pid,
                client,
                client_pid,
                window_id,
                xpra_wid,
                directory,
                keyboard_scenario,
                keyboard_scenario_sha256,
            )

        lifecycle: dict[str, Any] = {"mode": args.lifecycle}
        if application_identity is not None:
            assert server_process_identity is not None
            lifecycle.update(
                {
                    "application_identity_at_capture": application_identity,
                    "server_identity_at_capture": server_process_identity,
                    "server_pid": server_pid,
                }
            )
        if args.lifecycle in {"detach", "transport-loss"} and application_identity is None:
            raise LabFailure("lifecycle profile requires fixture-owned application identity")
        if args.lifecycle == "application-exit":
            if args.application in MULTIWINDOW_HARDWARE_APPLICATIONS | {"vkcube"}:
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
            elif args.application == "keyboard":
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
                    *command_cli_options("client", "detach"),
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
            survived_identity = require_interaction_fixture_identity(
                server,
                application_identity,
                server_pid=server_pid,
            )
            assert server_process_identity is not None
            survived_server_identity = require_process_identity(
                server,
                server_process_identity,
                role="Xpra server",
            )
            lifecycle.update(
                {
                    "application_identity_after_detach": survived_identity,
                    "application_survived_detach": True,
                    "server_identity_after_detach": survived_server_identity,
                    "server_survived_detach": True,
                }
            )
            if not lifecycle["server_survived_detach"]:
                raise LabFailure("Xpra server did not survive detach")
            before_termination = require_interaction_fixture_identity(
                server,
                application_identity,
                server_pid=server_pid,
            )
            server_before_termination = require_process_identity(
                server,
                server_process_identity,
                role="Xpra server",
            )
            lifecycle.update(
                {
                    "application_identity_before_termination": before_termination,
                    "server_identity_before_application_termination": (
                        server_before_termination
                    ),
                    "server_alive_before_application_termination": True,
                    "application_termination": terminate_interaction_fixture(
                        server,
                        application_identity,
                        server_identity=server_process_identity,
                    ),
                }
            )
            wait_for(
                "interaction fixture exit after deliberate termination",
                lambda: interaction_fixture_identity_is_gone(
                    server,
                    application_identity,
                ),
            )
            lifecycle["application_exited_after_termination"] = True
            wait_for(
                "Xpra server exit after detached application termination",
                lambda: process_identity_is_gone(
                    server,
                    server_process_identity,
                    role="Xpra server",
                ),
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
            survived_identity = require_interaction_fixture_identity(
                server,
                application_identity,
                server_pid=server_pid,
            )
            assert server_process_identity is not None
            survived_server_identity = require_process_identity(
                server,
                server_process_identity,
                role="Xpra server",
            )
            lifecycle.update(
                {
                    "application_identity_after_transport_loss": survived_identity,
                    "application_survived_transport_loss": True,
                    "server_identity_after_transport_loss": (
                        survived_server_identity
                    ),
                    "server_survived_transport_loss": True,
                }
            )
            if not lifecycle["server_survived_transport_loss"]:
                raise LabFailure("Xpra server did not survive transport loss")
            before_termination = require_interaction_fixture_identity(
                server,
                application_identity,
                server_pid=server_pid,
            )
            server_before_termination = require_process_identity(
                server,
                server_process_identity,
                role="Xpra server",
            )
            lifecycle.update(
                {
                    "application_identity_before_termination": before_termination,
                    "server_identity_before_application_termination": (
                        server_before_termination
                    ),
                    "server_alive_before_application_termination": True,
                    "application_termination": terminate_interaction_fixture(
                        server,
                        application_identity,
                        server_identity=server_process_identity,
                    ),
                }
            )
            wait_for(
                "interaction fixture exit after deliberate termination",
                lambda: interaction_fixture_identity_is_gone(
                    server,
                    application_identity,
                ),
            )
            lifecycle["application_exited_after_termination"] = True
            wait_for(
                "Xpra server exit after transport-loss application termination",
                lambda: process_identity_is_gone(
                    server,
                    server_process_identity,
                    role="Xpra server",
                ),
                timeout=15,
            )
            lifecycle["server_exited_after_application"] = True

        workloads_exited = True
        pull_all_container_artifacts(server, directory, "server")
        collected_containers.add(server)
        pull_all_container_artifacts(client, directory, "client")
        collected_containers.add(client)
        if args.application == "keyboard":
            assert keyboard_scenario is not None
            assert keyboard_scenario_sha256 is not None
            interaction = finalize_wayland_keyboard_evidence(
                interaction,
                directory,
                keyboard_scenario,
                keyboard_scenario_sha256,
            )
        if args.application == "opengl":
            application_activity["opengl"] = load_opengl_evidence(
                directory / "opengl.stdout"
            )
        updates = parse_saved_updates(directory, xpra_wid)
        updates["initial_pixel_format"] = saved_window_initial_pixel_format(
            directory, xpra_wid
        )
        if args.application == "zed" and args.encoding == "h264":
            stimulus = interaction.get("h264_stimulus")
            if not isinstance(stimulus, dict):
                raise LabFailure("Zed H.264 stimulus evidence is unavailable")
            updates["h264_stimulus"] = {
                "baseline_sequence": stimulus.get("baseline_sequence"),
                "last_sequence": stimulus.get("last_sequence"),
                "window_size": stimulus.get("window_size"),
            }
        elif (
            args.application in MULTIWINDOW_HARDWARE_APPLICATIONS
            and args.encoding == "h264"
        ):
            stimulus = interaction.get("h264_stimulus")
            if not isinstance(stimulus, dict):
                raise LabFailure("hardware H.264 stimulus evidence is unavailable")
            updates["h264_stimulus"] = {
                "baseline_sequence": stimulus.get("baseline_sequence"),
                "first_sequence": stimulus.get("first_sequence"),
                "last_sequence": stimulus.get("last_sequence"),
                "window_size": stimulus.get("window_size"),
            }
        interaction_updates = (
            parse_saved_updates(directory, interaction_xpra_wid)
            if interaction_xpra_wid is not None
            else None
        )
        if interaction_updates is not None and interaction_xpra_wid is not None:
            interaction_updates["initial_pixel_format"] = (
                saved_window_initial_pixel_format(
                    directory, interaction_xpra_wid
                )
            )
            interaction["source_alpha"] = saved_source_alpha_evidence(
                directory,
                interaction_updates,
            )
        log_evidence = inspect_logs(directory)
        direct_evidence = direct
        focused_evidence = focused_screen
        direct_rgb_path = directory / "window-direct.rgb.png"
        focused_rgb_path = directory / "window-focused-screen.rgb.png"
        source_viewport: dict[str, Any] | None = None
        if args.application in MULTIWINDOW_HARDWARE_APPLICATIONS:
            stimulus = updates.get("h264_stimulus")
            window_size_value = (
                stimulus.get("window_size") if isinstance(stimulus, dict) else None
            )
            if (
                not isinstance(window_size_value, list)
                or len(window_size_value) != 2
            ):
                raise LabFailure("hardware source viewport size is unavailable")
            source_width = _exact_int(window_size_value[0], positive=True)
            source_height = _exact_int(window_size_value[1], positive=True)
            if source_width is None or source_height is None:
                raise LabFailure("hardware source viewport size is invalid")
            source_size = (source_width, source_height)
            direct_evidence = crop_client_source_viewport(
                directory,
                "window-direct",
                "window-direct-source-viewport",
                source_size,
            )
            direct_evidence["xwd"] = {
                **direct["xwd"],
                "source_viewport": direct_evidence["viewport"],
            }
            focused_evidence = crop_client_source_viewport(
                directory,
                "window-focused-screen",
                "window-focused-source-viewport",
                source_size,
            )
            focused_evidence["xwd"] = {
                **focused_screen["xwd"],
                "source_viewport": focused_evidence["viewport"],
            }
            add_background_comparison(
                focused_evidence,
                directory / "window-focused-source-viewport.rgba.png",
                background_rgb,
            )
            backing_size = tuple(
                int(value)
                for value in direct_evidence["viewport"]["backing_size"]
            )
            source_viewport = {
                "direct": direct_evidence,
                "focused_screen": focused_evidence,
                "placement_logged": bool(
                    backing_size == source_size
                    or client_source_viewport_logged(
                        directory,
                        source_size,
                        backing_size,
                    )
                ),
            }
            direct_rgb_path = directory / "window-direct-source-viewport.rgb.png"
            focused_rgb_path = directory / "window-focused-source-viewport.rgb.png"
        pixel_evidence, source_image = pixel_pipeline_evidence(
            directory,
            updates["screenshots"],
            direct_rgb_path,
            focused_rgb_path,
            pixel_error_limit(args.application, args.encoding),
        )
        if source_viewport is not None:
            pixel_evidence["source_viewport_placement_logged"] = source_viewport[
                "placement_logged"
            ]
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
            h264_production_updates = updates
            allow_alpha_gaps = False
            allow_lossless_rgb_edges = args.h264_client_policy == "adaptive-alpha"
            if args.h264_client_policy == "adaptive-alpha":
                if args.application in MULTIWINDOW_HARDWARE_APPLICATIONS:
                    h264_production_updates = hardware_h264_context_updates(
                        updates
                    ) or {
                        **updates,
                        "count": 0,
                        "encodings": [],
                        "updates": [],
                    }
                    allow_alpha_gaps = True
                else:
                    allow_alpha_gaps = True
                allow_lossless_rgb_edges = True
                hardware["h264_primary"] = h264_production_metrics(
                    args.application,
                    updates,
                )
            h264_hardware = h264_hardware_evidence(
                directory,
                h264_production_updates,
                allow_alpha_gaps=allow_alpha_gaps,
                allow_lossless_rgb_edges=allow_lossless_rgb_edges,
                allow_terminal_server_frame=(
                    args.application in MULTIWINDOW_HARDWARE_APPLICATIONS
                ),
                allow_window_resize_gaps=(
                    args.application in MULTIWINDOW_HARDWARE_APPLICATIONS
                ),
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
            direct=direct_evidence,
            composited=focused_evidence["image"],
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
                "network_options": client_network_options(args.network_profile),
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
                "source_viewport": source_viewport,
            },
            "lifecycle": lifecycle,
            "lifecycle_profile": args.lifecycle,
            "logs": log_evidence,
            "name": scenario.name,
            "network_profile": args.network_profile,
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
        if not workloads_exited:
            quiescence = quiesce_failed_workloads(
                (
                    ("client", client, client_pid),
                    ("server", server, server_pid),
                )
            )
            report["failure_quiescence"] = quiescence
            workloads_exited = bool(quiescence["passed"])
        (directory / "report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        raise
    finally:
        artifact_collection: list[dict[str, str]] = []
        roles = {server: "server", client: "client"}
        for container in containers:
            if container in collected_containers:
                artifact_collection.append(
                    {"container": container, "status": "collected"}
                )
                continue
            if not workloads_exited:
                artifact_collection.append(
                    {
                        "container": container,
                        "status": "skipped-active-workload",
                    }
                )
                continue
            try:
                pull_all_container_artifacts(container, directory, roles[container])
                artifact_collection.append(
                    {"container": container, "status": "collected"}
                )
            except BaseException as collection_error:  # noqa: BLE001
                artifact_collection.append(
                    {
                        "container": container,
                        "error": str(collection_error),
                        "status": "collection-failed",
                    }
                )
        report["container_artifact_collection"] = artifact_collection
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
                role = "server" if container == server else "client"
                item = ledger["objects"][role]
                removal = remove_owned_podman_object(
                    "container",
                    container,
                    item["labels"],
                    item["id"] or None,
                )
                cleanup["containers"].append(removal)
                item["state"] = removal["status"]
                replace_private_json(ledger_path, ledger)
            if network_created:
                network_item = ledger["objects"]["network"]
                cleanup["network"] = remove_owned_podman_object(
                    "network",
                    network,
                    network_item["labels"],
                    network_item["id"] or None,
                )
                network_item["state"] = cleanup["network"]["status"]
                replace_private_json(ledger_path, ledger)
            else:
                cleanup["network"] = {"name": network, "status": "not-created"}
            cleanup["passed"] = all(
                item["status"] == "removed" for item in cleanup["containers"]
            ) and cleanup["network"]["status"] in {"removed", "not-created"}
        report["artifact_sha256"] = artifact_sha256(directory)
        report["cleanup"] = cleanup
        report["artifact_collection_passed"] = bool(artifact_collection) and all(
            item["status"] == "collected" for item in artifact_collection
        )
        report["result"] = "passed" if scenario_acceptance(report, cleanup) else "failed"
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
        help="reviewed Xpra transport encoding; RGB is the strict default",
    )
    parser.add_argument(
        "--h264-client-policy",
        choices=H264_ACCEPTANCE_POLICIES,
        default="strict",
        help="reviewed H.264 acceptance policy",
    )
    parser.add_argument(
        "--selection",
        metavar="{cases,stacks}/SLUG",
        required=True,
        help="validated case or stack manifest to apply to the server image",
    )
    parser.set_defaults(source_variant=None)
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
        default="default",
        help="run the reviewed positive alpha scenario",
    )
    parser.add_argument(
        "--network-profile",
        choices=NETWORK_PROFILES,
        default=DEFAULT_NETWORK_PROFILE,
        help="tracked client-side network and quality profile",
    )
    parser.add_argument(
        "--zed-directory",
        type=Path,
        default=DEFAULT_ZED_DIRECTORY,
        help="host Zed application directory streamed read-only into the server",
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
    parser.add_argument("--bound-inputs", type=Path)
    parser.add_argument("--bound-input-manifest-sha256")
    parser.add_argument("--bound-input-tree-sha256")
    args = parser.parse_args()
    try:
        validate_profile(
            application=args.application,
            lifecycle=args.lifecycle,
            encoding=args.encoding,
            h264_client_policy=args.h264_client_policy,
            alpha_scenarios=args.alpha_scenarios,
            network_profile_name=args.network_profile,
        )
    except ProfileError as error:
        raise LabFailure(str(error)) from error
    if args.selection is None:
        raise LabFailure("live acceptance requires one non-empty case or stack selection")
    if args.source_variant is not None:
        raise LabFailure("live acceptance does not support clean source variants")
    transport_encoding_options(args.encoding, args.h264_client_policy, client=True)
    static_cli_options("server", "base")
    static_cli_options("server", "lifecycle")
    static_cli_options("server", "diagnostics")
    static_cli_options("client", "base")
    static_cli_options("client", "diagnostics")
    client_network_options(args.network_profile)
    if args.run_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.run_id):
        raise LabFailure(f"invalid run ID: {args.run_id!r}")
    bound_values = (
        args.bound_inputs,
        args.bound_input_manifest_sha256,
        args.bound_input_tree_sha256,
    )
    if any(value is not None for value in bound_values) and not all(
        value is not None for value in bound_values
    ):
        raise LabFailure("bound live inputs require path, manifest digest, and tree digest")

    if shutil.which("podman") is None:
        raise LabFailure("podman is not available")
    if not args.render_node.is_char_device():
        raise LabFailure(f"render node is unavailable: {args.render_node}")
    if not os.access(args.render_node, os.R_OK | os.W_OK):
        raise LabFailure(
            f"render node is not readable and writable: {args.render_node}"
        )
    if args.application == "zed" and args.bound_inputs is None:
        zed_binary = args.zed_directory / "libexec" / "zed-editor"
        if not zed_binary.is_file() or not os.access(zed_binary, os.X_OK):
            raise LabFailure(f"Zed executable is unavailable: {zed_binary}")
        zed_binary_sha256 = sha256_file(zed_binary)
    else:
        zed_binary = None
        zed_binary_sha256 = None

    if args.state_root.is_symlink():
        raise LabFailure(f"state root must not be a symlink: {args.state_root}")
    state_root = args.state_root.absolute()
    ensure_trusted_parent_directory(state_root.parent)
    ensure_private_directory(state_root, create=True)
    if args.run_id:
        result_name = args.run_id
    else:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        result_name = f"{timestamp}-{os.getpid()}"
    result_root = state_root / "live-results"
    ensure_private_directory(result_root, create=True)
    result_directory = result_root / result_name
    if args.bound_inputs is not None:
        if args.source_variant is not None:
            raise LabFailure("bound live inputs cannot use a source-variant alias")
        if args.bound_inputs != result_directory / "inputs":
            raise LabFailure("bound live input path does not match the supervised run")
        ensure_private_directory(result_directory)
        if {path.name for path in result_directory.iterdir()} != {"inputs"}:
            raise LabFailure("bound live result directory has unexpected pre-run content")
        bound = load_bound_inputs(
            args.bound_inputs,
            expected_manifest_sha256=args.bound_input_manifest_sha256,
            expected_tree_sha256=args.bound_input_tree_sha256,
        )
        snapshot = bound.snapshot
        server_context = bound.server_context
        client_context = bound.client_context
        server_selection = server_context.selection
        client_selection = client_context.selection
        input_manifest_sha256 = bound.input_manifest_sha256
        input_tree_sha256 = bound.input_tree_sha256
        zed_archive = bound.zed_archive
        zed_archive_sha256 = bound.zed_archive_sha256
        zed_binary_sha256 = bound.zed_binary_sha256
        keyboard_scenario = bound.keyboard_scenario
        keyboard_scenario_sha256 = bound.keyboard_scenario_sha256
        if args.selection != (None if server_selection.name == "master" else server_selection.name):
            raise LabFailure("bound live selection does not match the invocation")
    else:
        server_selection = resolve_patch_selection(args.selection, args.source_variant)
        client_selection = resolve_patch_selection(None, "master")
        keyboard_scenario_input = (
            selected_keyboard_scenario(server_selection)
            if args.application == "keyboard"
            else None
        )
        keyboard_scenario = (
            keyboard_scenario_input[1] if keyboard_scenario_input else None
        )
        keyboard_scenario_sha256 = (
            keyboard_scenario_input[2] if keyboard_scenario_input else None
        )
        snapshot = create_source_snapshot(state_root)
        server_context = prepare_build_context(
            state_root,
            snapshot,
            server_selection,
        )
        client_context = prepare_build_context(state_root, snapshot, client_selection)
        result_directory.mkdir(mode=0o700, exist_ok=False)
        ensure_private_directory(result_directory)
        input_manifest_sha256, zed_archive, zed_archive_sha256 = snapshot_build_inputs(
            result_directory,
            snapshot,
            server_context,
            client_context,
            args.zed_directory if args.application == "zed" else None,
            keyboard_scenario=keyboard_scenario_input,
            zed_binary_sha256=zed_binary_sha256,
        )
        if zed_binary is not None and sha256_file(zed_binary) != zed_binary_sha256:
            raise LabFailure("Zed executable changed while its payload was frozen")
        input_tree_sha256 = tree_sha256(result_directory / "inputs")
    if (args.application == "keyboard") != (keyboard_scenario is not None):
        raise LabFailure("frozen live inputs have the wrong keyboard scenario payload")
    if (keyboard_scenario is None) != (keyboard_scenario_sha256 is None):
        raise LabFailure("frozen keyboard scenario provenance is incomplete")
    input_manifest_path = result_directory / "inputs" / "manifest.json"
    ensure_private_regular_file(input_manifest_path)
    if sha256_file(input_manifest_path) != input_manifest_sha256:
        raise LabFailure("frozen live input manifest changed before use")
    try:
        input_provenance = json.loads(input_manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise LabFailure("frozen live input manifest is invalid JSON") from error
    if not isinstance(input_provenance, dict) or input_provenance.get("schema") != 2:
        raise LabFailure("frozen live input manifest has an unsupported schema")
    args.selected_case_slugs = server_selection.case_slugs
    commit = snapshot.commit
    server_context_digest = server_context.digest
    client_context_digest = client_context.digest
    selection_tag = re.sub(r"[^a-z0-9_.-]+", "-", server_selection.name).strip("-")
    server_suffix = f"{commit[:12]}-{selection_tag}-{server_context_digest[:12]}"
    client_suffix = f"{commit[:12]}-master-{client_context_digest[:12]}"
    server_image = f"localhost/xpra-fork-maintenance-live-server:{server_suffix}"
    client_image = f"localhost/xpra-fork-maintenance-live-client:{client_suffix}"
    if not args.skip_build:
        for target, image, context in (
            ("server", server_image, server_context),
            ("client", client_image, client_context),
        ):
            build_command = [
                "podman",
                "build",
                "--target",
                target,
                "--build-arg",
                f"XPRA_COMMIT={commit}",
                "--build-arg",
                f"XPRA_SELECTION={context.selection.name}",
                "--label",
                "io.xpra.fork-maintenance.owner=live",
                "--label",
                f"io.xpra.fork-maintenance.role={target}-image",
                "--label",
                f"io.xpra.fork-maintenance.source={commit}",
                "--label",
                f"io.xpra.fork-maintenance.context={context.digest}",
                "--tag",
                image,
                "-",
            ]
            if context.archive_sha256 is None:
                stream_build_context(build_command, context.path)
            else:
                stream_bound_build_context(build_command, context)
    else:
        run(["podman", "image", "exists", server_image])
        run(["podman", "image", "exists", client_image])
    image_inspections = {
        "server": inspect_maintenance_image(
            server_image,
            role="server-image",
            source_commit=commit,
            context_digest=server_context_digest,
        ),
        "client": inspect_maintenance_image(
            client_image,
            role="client-image",
            source_commit=commit,
            context_digest=client_context_digest,
        ),
    }
    patch_manifest = server_context.manifest.get("patches")
    patch_series = server_context.manifest.get("patch_series")
    if not isinstance(patch_manifest, dict) or not isinstance(patch_series, list):
        raise LabFailure("server build context has invalid patch provenance")
    if tree_sha256(result_directory / "inputs") != input_tree_sha256:
        raise LabFailure("frozen live inputs changed before scenario execution")

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
        "network_profile": args.network_profile,
        "invocation": {
            "alpha_scenarios": args.alpha_scenarios,
            "application": args.application,
            "encoding": args.encoding,
            "h264_client_policy": args.h264_client_policy,
            "job_id": os.environ.get("XPRA_FORK_JOB_ID"),
            "lifecycle": args.lifecycle,
            "libva_driver": args.libva_driver,
            "network_profile": args.network_profile,
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
            "harness_sha256": harness_sha256(),
            "input_manifest_sha256": input_manifest_sha256,
            "input_provenance": input_provenance,
            "input_tree_sha256": input_tree_sha256,
            "patches": patch_manifest,
            "patch_series": patch_series,
            "selection": {
                "case_slugs": server_selection.case_slugs,
                "digest": server_selection.digest,
                "name": server_selection.name,
                "resolution": server_context.resolution,
                "selector_digests": dict(server_selection.selector_digests),
                "selectors": server_selection.selectors,
            },
            "fork_master": commit,
            "supervisor_sha256": sha256_file(INFRA_ROOT / "job.py"),
            "background_supervisor_sha256": sha256_file(BACKGROUND_SUPERVISOR),
            "workflow_sha256": snapshot.workflow_sha256,
            "zed_sha256": zed_binary_sha256,
            "zed_archive_sha256": zed_archive_sha256,
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
                    keyboard_scenario=keyboard_scenario,
                    keyboard_scenario_sha256=keyboard_scenario_sha256,
                    zed_archive=zed_archive,
                    zed_archive_sha256=zed_archive_sha256,
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
        passed = all(report.get("result") == "passed" for report in aggregate["scenarios"])
        aggregate["comparison"] = {
            "all_scenarios_rendered": bool(appearances)
            and all(appearance == "rendered" for appearance in appearances.values()),
            "appearances": appearances,
            "lifecycle": args.lifecycle,
        }
        aggregate["result"] = "passed" if passed else "failed"
        if tree_sha256(result_directory / "inputs") != input_tree_sha256:
            raise LabFailure("frozen live inputs changed during scenario execution")
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
