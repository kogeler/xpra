#!/usr/bin/env python3
# Copyright (C) 2026 kogeler
"""Own patched DEB container builds and manual GitHub Release publication."""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import io
import json
import lzma
import os
import posixpath
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from pathlib import Path, PurePosixPath
from typing import Any

RUNNER_ROOT = Path(__file__).resolve().parent
LAB_ROOT = RUNNER_ROOT.parent.parent
PROJECT_ROOT = LAB_ROOT.parent
TOOLS_ROOT = LAB_ROOT / "tools"
UPSTREAM_RUNNER = LAB_ROOT / "infra" / "upstream-tests"
sys.path.insert(0, str(TOOLS_ROOT))

import background_job
import container_payload
import contrib

STATE_ROOT = PROJECT_ROOT / ".artifacts" / "fork-maintenance" / "deb-packages"
RUN_ROOT = STATE_ROOT / "runs"
OUTPUT_ROOT = STATE_ROOT / "outputs"
RESULT_ROOT = STATE_ROOT / "results"
RELEASE_ROOT = STATE_ROOT / "releases"
SOURCE_ROOT = STATE_ROOT / "sources"
SELECTION_ROOT = STATE_ROOT / "selections"
LOCK_ROOT = STATE_ROOT / "locks"
OWNER = "xpra-deb-packages"
SOURCE_OWNER = "xpra-deb-checkout-source"
SELECTION_OWNER = "xpra-deb-selection-cache"
SELECTION_TOOL = UPSTREAM_RUNNER / "selection.py"
BUILDER_INPUTS = {
    ".containerignore": RUNNER_ROOT / ".containerignore",
    "Containerfile": RUNNER_ROOT / "Containerfile",
    "builder.py": RUNNER_ROOT / "builder.py",
    "container_payload.py": TOOLS_ROOT / "container_payload.py",
    "selection.py": SELECTION_TOOL,
}
RUNNER_INPUTS = (
    RUNNER_ROOT / "Makefile",
    RUNNER_ROOT / "job.py",
    *BUILDER_INPUTS.values(),
    TOOLS_ROOT / "background_job.py",
    TOOLS_ROOT / "contrib.py",
)
DISTROS = {
    "ubuntu-26.04": "docker.io/library/ubuntu:26.04",
    "debian-13": "docker.io/library/debian:13",
}
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
SELECTOR_RE = re.compile(r"(?:cases|stacks)/[a-z0-9]+(?:-[a-z0-9]+)*")
VERSION_RE = re.compile(r"[0-9]+\.[0-9]+")
ACTIVE_SELECTION = "stacks/develop"
MAX_DEB_TAR_BYTES = 2 * 1024 * 1024 * 1024 - 1
MAX_DEB_CONTROL_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_DEB_CONTROL_FILE_BYTES = 1024 * 1024
MAX_DEB_CONTROL_MEMBERS = 4096
MAX_DEB_CONTROL_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_DEB_CONTROL_TAR_BYTES = 128 * 1024 * 1024
MAX_XZ_MEMORY_BYTES = 256 * 1024 * 1024
MAX_DEB_DATA_MEMBERS = 250_000
MAX_DEB_DATA_MEMBER_BYTES = 4 * 1024 * 1024 * 1024
MAX_DEB_DATA_EXPANDED_BYTES = 16 * 1024 * 1024 * 1024
MAX_DEB_DATA_TAR_BYTES = (
    MAX_DEB_DATA_EXPANDED_BYTES + MAX_DEB_DATA_MEMBERS * 1024 + 1024 * 1024
)
RELEASE_ASSET_NAMES = {
    "xpra-debian-13-amd64-debs.tar",
    "xpra-ubuntu-26.04-amd64-debs.tar",
}
RELEASE_REPOSITORY = "kogeler/xpra"
RELEASE_WORKFLOW = ".github/workflows/deb-packages.yml"
RELEASE_TRANSACTION_PREFIX = "<!-- xpra-deb-transaction:"
RELEASE_LIST_PAGE_SIZE = 100
MAX_RELEASE_LIST_PAGES = 100


class JobError(RuntimeError):
    """Raised when a package runtime or result cannot be trusted."""


class _ArMemberReader(io.RawIOBase):
    """Seekable bounded view of one ar member without buffering it in memory."""

    def __init__(self, path: Path, offset: int, size: int) -> None:
        self._stream = path.open("rb")
        self._offset = offset
        self._size = size
        self._position = 0
        self._stream.seek(offset)

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._position

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if whence == os.SEEK_SET:
            position = offset
        elif whence == os.SEEK_CUR:
            position = self._position + offset
        elif whence == os.SEEK_END:
            position = self._size + offset
        else:
            raise ValueError(f"invalid seek mode: {whence}")
        if position < 0:
            raise OSError("negative seek in ar member")
        self._position = min(position, self._size)
        self._stream.seek(self._offset + self._position)
        return self._position

    def readinto(self, buffer: bytearray) -> int:
        remaining = self._size - self._position
        if remaining <= 0:
            return 0
        payload = self._stream.read(min(len(buffer), remaining))
        length = len(payload)
        buffer[:length] = payload
        self._position += length
        return length

    def close(self) -> None:
        self._stream.close()
        super().close()


class _BoundedReader:
    """Count every decompressed byte read from a stream, including tar padding."""

    def __init__(self, stream: Any, maximum: int, label: str) -> None:
        self._stream = stream
        self._maximum = maximum
        self._label = label
        self.total = 0

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = self._maximum - self.total + 1
        payload = self._stream.read(min(size, self._maximum - self.total + 1))
        self.total += len(payload)
        if self.total > self._maximum:
            raise JobError(f"{self._label} expands past {self._maximum} bytes")
        return payload


class _SingleXZReader:
    """Incrementally expose one exact xz stream and reject trailing bytes."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._decoder = lzma.LZMADecompressor(
            format=lzma.FORMAT_XZ,
            memlimit=MAX_XZ_MEMORY_BYTES,
        )
        self._buffer = bytearray()
        self._finished = False

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = 1024 * 1024
        while len(self._buffer) < size and not self._finished:
            compressed = (
                self._stream.read(64 * 1024) if self._decoder.needs_input else b""
            )
            if self._decoder.needs_input and not compressed:
                raise JobError("xz stream is truncated")
            expanded = self._decoder.decompress(
                compressed,
                max_length=max(size - len(self._buffer), 1),
            )
            self._buffer.extend(expanded)
            if self._decoder.eof:
                if self._decoder.unused_data or self._stream.read(1):
                    raise JobError("xz stream contains trailing or concatenated data")
                self._finished = True
        payload = bytes(self._buffer[:size])
        del self._buffer[:size]
        return payload


def command(
    argv: list[str],
    *,
    check: bool = True,
    capture: bool = True,
    cwd: Path | None = None,
    pass_fds: tuple[int, ...] = (),
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        argv,
        check=False,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        pass_fds=pass_fds,
    )
    if check and result.returncode:
        details = f"\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}" if capture else ""
        raise JobError(f"command failed ({result.returncode}): {argv!r}{details}")
    return result


def command_bytes(argv: list[str], *, cwd: Path | None = None) -> bytes:
    result = subprocess.run(
        argv,
        check=False,
        cwd=cwd,
        capture_output=True,
    )
    if result.returncode:
        raise JobError(
            f"command failed ({result.returncode}): {argv!r}\n"
            f"stdout:\n{result.stdout.decode(errors='replace')}\n"
            f"stderr:\n{result.stderr.decode(errors='replace')}"
        )
    return result.stdout


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def runner_sha256() -> str:
    digest = hashlib.sha256()
    for path in RUNNER_INPUTS:
        if path.is_symlink() or not path.is_file():
            raise JobError(f"runner input is unavailable: {path}")
        digest.update(path.relative_to(LAB_ROOT).as_posix().encode())
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256_file(path)))
        digest.update(b"\0")
    return digest.hexdigest()


def prepare_state() -> None:
    if PROJECT_ROOT.is_symlink() or not PROJECT_ROOT.is_dir():
        raise JobError(f"project root is not a real directory: {PROJECT_ROOT}")
    if PROJECT_ROOT.stat().st_uid != os.getuid():
        raise JobError(f"project root is not owned by this user: {PROJECT_ROOT}")
    artifact_root = PROJECT_ROOT / ".artifacts"
    if artifact_root.is_symlink():
        raise JobError(f"artifact root is a symlink: {artifact_root}")
    artifact_root.mkdir(mode=0o700, exist_ok=True)
    if artifact_root.stat().st_uid != os.getuid() or artifact_root.stat().st_mode & 0o022:
        raise JobError(f"artifact root is not safely owned: {artifact_root}")
    for path in (
        STATE_ROOT.parent,
        STATE_ROOT,
        RUN_ROOT,
        OUTPUT_ROOT,
        RESULT_ROOT,
        RELEASE_ROOT,
        SOURCE_ROOT,
        SELECTION_ROOT,
        LOCK_ROOT,
    ):
        background_job.ensure_private_directory(path, create=True)


def replace_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomically replace one generated private cache record."""
    background_job.ensure_private_directory(path.parent)
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


def publish_bytes(path: Path, payload: bytes) -> None:
    """Publish a private immutable byte payload without sharing an inode."""
    try:
        background_job.publish_bytes(path, payload)
    except background_job.BackgroundJobError as error:
        raise JobError(str(error)) from error


def source_snapshot_sha256(values: dict[str, str]) -> str:
    identity = {
        key: values[key]
        for key in (
            "checkout_commit",
            "source_commit",
            "source_ref",
            "source_ref_commit",
            "workflow_sha256",
        )
    }
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def source_snapshot_root(checkout_commit: str, snapshot_sha256: str) -> Path:
    if not COMMIT_RE.fullmatch(checkout_commit) or not SHA256_RE.fullmatch(snapshot_sha256):
        raise JobError("invalid DEB source snapshot identity")
    return SOURCE_ROOT / f"{checkout_commit}-{snapshot_sha256}"


def source_bundle_path(checkout_commit: str, snapshot_sha256: str) -> Path:
    return source_snapshot_root(checkout_commit, snapshot_sha256) / "source.bundle"


def source_state_path(checkout_commit: str, snapshot_sha256: str) -> Path:
    return source_snapshot_root(checkout_commit, snapshot_sha256) / "source.json"


def source_partial_path() -> Path:
    return SOURCE_ROOT / ".source-snapshot.partial"


def source_partial_marker_path() -> Path:
    return SOURCE_ROOT / ".source-snapshot.partial.owner.json"


def source_lock_path() -> Path:
    return SOURCE_ROOT / ".source-snapshot.lock"


@contextmanager
def source_snapshot_lock() -> Iterator[int]:
    """Serialize publication and make a dead publisher's partial reclaimable."""
    path = source_lock_path()
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != 0o600
            or details.st_nlink != 1
        ):
            raise JobError(f"unsafe DEB source snapshot lock: {path}")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield descriptor
    finally:
        # Do not issue LOCK_UN: an exact publisher child may still hold the
        # inherited open-file description after this parent is terminated.
        os.close(descriptor)


def source_partial_record(
    identity: dict[str, str],
    snapshot_sha256: str,
) -> dict[str, Any]:
    target = source_snapshot_root(identity["checkout_commit"], snapshot_sha256)
    return {
        **identity,
        "kind": "source-snapshot-partial",
        "owner": SOURCE_OWNER,
        "partial": str(source_partial_path()),
        "schema": 1,
        "snapshot_sha256": snapshot_sha256,
        "target": str(target),
    }


def validate_source_partial_marker() -> tuple[dict[str, str], Path]:
    marker = source_partial_marker_path()
    payload = background_job.load_json(marker)
    expected_keys = {
        "checkout_commit",
        "kind",
        "owner",
        "partial",
        "schema",
        "snapshot_sha256",
        "source_commit",
        "source_ref",
        "source_ref_commit",
        "target",
        "workflow_sha256",
    }
    if set(payload) != expected_keys or any(
        payload.get(key) != value
        for key, value in {
            "kind": "source-snapshot-partial",
            "owner": SOURCE_OWNER,
            "partial": str(source_partial_path()),
            "schema": 1,
        }.items()
    ):
        raise JobError("DEB source partial marker does not match its owner")
    identity: dict[str, str] = {}
    for key, pattern in (
        ("checkout_commit", COMMIT_RE),
        ("source_commit", COMMIT_RE),
        ("source_ref_commit", COMMIT_RE),
        ("workflow_sha256", SHA256_RE),
    ):
        value = str(payload.get(key, ""))
        if not pattern.fullmatch(value):
            raise JobError(f"DEB source partial marker has invalid {key}")
        identity[key] = value
    source_ref = str(payload.get("source_ref", ""))
    ref_check = command(["git", "check-ref-format", source_ref], check=False)
    if (
        ref_check.returncode
        or not source_ref.startswith(("refs/heads/", "refs/remotes/"))
        or source_ref.rsplit("/", 1)[-1] != "master"
    ):
        raise JobError("DEB source partial marker does not name a master ref")
    identity["source_ref"] = source_ref
    snapshot_sha256 = str(payload.get("snapshot_sha256", ""))
    if (
        not SHA256_RE.fullmatch(snapshot_sha256)
        or source_snapshot_sha256(identity) != snapshot_sha256
    ):
        raise JobError("DEB source partial marker provenance is inconsistent")
    target = source_snapshot_root(identity["checkout_commit"], snapshot_sha256)
    if payload.get("target") != str(target):
        raise JobError("DEB source partial marker has an unexpected target")
    return {**identity, "snapshot_sha256": snapshot_sha256}, target


def remove_source_partial_directory() -> None:
    partial = source_partial_path()
    background_job.ensure_private_directory(partial)
    for entry in partial.iterdir():
        details = entry.lstat()
        if (
            entry.is_symlink()
            or not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or details.st_nlink != 1
        ):
            raise JobError(f"unsafe file in owned DEB source partial: {entry}")
    shutil.rmtree(partial)


def reject_unknown_source_partials() -> None:
    allowed = {
        source_lock_path(),
        source_partial_marker_path(),
        source_partial_path(),
    }
    unknown = sorted(
        str(path)
        for path in SOURCE_ROOT.iterdir()
        if path.name.startswith(".") and path not in allowed
    )
    if unknown:
        raise JobError(
            "unowned DEB source snapshot partials require operator review: "
            + ", ".join(unknown)
        )


def recover_source_partial() -> None:
    """Reclaim only the deterministic partial proven by its external marker."""
    marker = source_partial_marker_path()
    partial = source_partial_path()
    marker_present = marker.exists() or marker.is_symlink()
    partial_present = partial.exists() or partial.is_symlink()
    if partial_present and not marker_present:
        raise JobError(f"unowned DEB source snapshot partial requires review: {partial}")
    if marker_present:
        identity, target = validate_source_partial_marker()
        if partial_present:
            remove_source_partial_directory()
        if target.exists() or target.is_symlink():
            background_job.ensure_private_directory(target)
            observed = validate_source_state(target / "source.json")
            if any(observed.get(key) != value for key, value in identity.items()):
                raise JobError("recovered DEB source snapshot target has wrong provenance")
        background_job.ensure_private_regular(marker)
        marker.unlink()
    reject_unknown_source_partials()


def validate_source_state(path: Path) -> dict[str, str]:
    payload = background_job.load_json(path)
    expected_fixed = {
        "owner": SOURCE_OWNER,
        "schema": 1,
    }
    if any(payload.get(key) != value for key, value in expected_fixed.items()):
        raise JobError("DEB source snapshot metadata does not match its owner")
    values: dict[str, str] = {}
    for key, pattern in (
        ("checkout_commit", COMMIT_RE),
        ("snapshot_sha256", SHA256_RE),
        ("source_commit", COMMIT_RE),
        ("source_ref_commit", COMMIT_RE),
        ("workflow_sha256", SHA256_RE),
    ):
        value = str(payload.get(key, ""))
        if not pattern.fullmatch(value):
            raise JobError(f"DEB source snapshot has invalid {key}")
        values[key] = value
    source_ref = str(payload.get("source_ref", ""))
    ref_check = command(["git", "check-ref-format", source_ref], check=False)
    if (
        ref_check.returncode
        or not source_ref.startswith(("refs/heads/", "refs/remotes/"))
        or source_ref.rsplit("/", 1)[-1] != "master"
    ):
        raise JobError("DEB source snapshot does not name a master ref")
    values["source_ref"] = source_ref
    calculated_snapshot = source_snapshot_sha256(values)
    if values["snapshot_sha256"] != calculated_snapshot:
        raise JobError("DEB source snapshot identity does not match its provenance")
    expected_state = source_state_path(values["checkout_commit"], calculated_snapshot)
    if path != expected_state:
        raise JobError(f"DEB source snapshot metadata has an unexpected path: {path}")
    bundle = Path(str(payload.get("source_bundle", "")))
    expected_bundle = source_bundle_path(values["checkout_commit"], calculated_snapshot)
    if not bundle.is_absolute() or bundle != expected_bundle:
        raise JobError("DEB source snapshot has an unexpected bundle path")
    root = expected_state.parent
    background_job.ensure_private_directory(root)
    if stat.S_IMODE(root.lstat().st_mode) != 0o700:
        raise JobError("DEB source snapshot root mode is not exactly 0700")
    entries = tuple(root.iterdir())
    if {entry.name for entry in entries} != {"source.bundle", "source.json"}:
        raise JobError("DEB source snapshot does not have its exact immutable file set")
    for entry in entries:
        background_job.ensure_private_regular(entry)
        if stat.S_IMODE(entry.lstat().st_mode) != 0o600:
            raise JobError(f"DEB source snapshot file mode is not exactly 0600: {entry}")
    listed = command(["git", "bundle", "list-heads", str(bundle)]).stdout.strip()
    if listed != f"{values['source_ref_commit']} {source_ref}":
        raise JobError("DEB source bundle identity does not match its master ref")
    command(["git", "bundle", "verify", str(bundle)], cwd=PROJECT_ROOT)
    merge_bases = command(
        [
            "git",
            "merge-base",
            "--all",
            values["checkout_commit"],
            values["source_ref_commit"],
        ],
        cwd=PROJECT_ROOT,
    ).stdout.splitlines()
    if merge_bases != [values["source_commit"]]:
        raise JobError("DEB source snapshot does not name the exact history boundary")
    command(
        [
            "git",
            "merge-base",
            "--is-ancestor",
            values["source_commit"],
            values["source_ref_commit"],
        ],
        cwd=PROJECT_ROOT,
    )
    workflow = command_bytes(
        ["git", "show", f"{values['source_commit']}:.github/workflows/test.yml"],
        cwd=PROJECT_ROOT,
    )
    if hashlib.sha256(workflow).hexdigest() != values["workflow_sha256"]:
        raise JobError("DEB source snapshot workflow digest does not match")
    values.update(
        {
            "source_bundle": str(bundle),
            "source_ref": source_ref,
        }
    )
    return values


def freeze_checkout_source() -> Path:
    """Freeze the clean boundary located from HEAD and refs named master."""
    prepare_state()
    with source_snapshot_lock() as lock_fd:
        recover_source_partial()
        return freeze_checkout_source_locked(lock_fd)


def freeze_checkout_source_locked(lock_fd: int) -> Path:
    state = contrib.checkout_source_check(PROJECT_ROOT)
    workflow = command_bytes(
        ["git", "show", f"{state.source_commit}:.github/workflows/test.yml"],
        cwd=PROJECT_ROOT,
    )
    identity = {
        "checkout_commit": state.head,
        "source_commit": state.source_commit,
        "source_ref": state.master_ref,
        "source_ref_commit": state.master_commit,
        "workflow_sha256": hashlib.sha256(workflow).hexdigest(),
    }
    snapshot_sha256 = source_snapshot_sha256(identity)
    root = source_snapshot_root(state.head, snapshot_sha256)
    bundle = source_bundle_path(state.head, snapshot_sha256)
    metadata = source_state_path(state.head, snapshot_sha256)
    payload = {
        **identity,
        "owner": SOURCE_OWNER,
        "schema": 1,
        "snapshot_sha256": snapshot_sha256,
        "source_bundle": str(bundle),
    }
    if root.exists() or root.is_symlink():
        background_job.ensure_private_directory(root)
        observed = validate_source_state(metadata)
        if any(observed.get(key) != payload[key] for key in identity):
            raise JobError("cached DEB source snapshot provenance does not match")
        if (
            command(["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT).stdout.strip()
            != state.head
            or command(["git", "rev-parse", state.master_ref], cwd=PROJECT_ROOT)
            .stdout.strip()
            != state.master_commit
            or contrib.porcelain(PROJECT_ROOT) != state.worktree_status
        ):
            raise JobError("checkout changed while reusing the DEB source snapshot")
        return metadata

    temporary = source_partial_path()
    marker = source_partial_marker_path()
    background_job.publish_json(marker, source_partial_record(identity, snapshot_sha256))
    try:
        temporary.mkdir(mode=0o700)
        command(
            ["git", "bundle", "create", str(temporary / "source.bundle"), state.master_ref],
            cwd=PROJECT_ROOT,
            pass_fds=(lock_fd,),
        )
        (temporary / "source.bundle").chmod(0o600)
        temporary_payload = {**payload, "source_bundle": str(bundle)}
        descriptor = os.open(
            temporary / "source.json",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(temporary_payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            temporary.rename(root)
        except OSError as error:
            if error.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                raise
    finally:
        if temporary.exists() or temporary.is_symlink():
            remove_source_partial_directory()
        if marker.exists() or marker.is_symlink():
            background_job.ensure_private_regular(marker)
            marker.unlink()
    background_job.ensure_private_directory(root)
    observed = validate_source_state(metadata)
    if any(
        observed.get(key) != payload[key]
        for key in (
            "checkout_commit",
            "snapshot_sha256",
            "source_bundle",
            "source_commit",
            "source_ref",
            "source_ref_commit",
            "workflow_sha256",
        )
    ):
        raise JobError("published DEB source boundary does not match checkout history")
    if (
        command(["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT).stdout.strip()
        != state.head
        or command(["git", "rev-parse", state.master_ref], cwd=PROJECT_ROOT).stdout.strip()
        != state.master_commit
        or contrib.porcelain(PROJECT_ROOT) != state.worktree_status
    ):
        raise JobError("checkout changed while freezing the DEB source snapshot")
    return metadata


def validate_name(value: str) -> str:
    if not NAME_RE.fullmatch(value):
        raise JobError(f"invalid package job name: {value!r}")
    return value


def require_amd64_host() -> None:
    """Fail before expensive work when the amd64-only package contract cannot hold."""
    machine = os.uname().machine.lower()
    if machine not in {"x86_64", "amd64"}:
        raise JobError(f"DEB package builds require an amd64 host, found {machine!r}")


def image_input_sha256(distro: str, base_image_id: str) -> str:
    if distro not in DISTROS:
        raise JobError(f"unsupported DEB distribution: {distro}")
    if not SHA256_RE.fullmatch(base_image_id):
        raise JobError("invalid DEB base image identity")
    digest = hashlib.sha256()
    digest.update(DISTROS[distro].encode())
    digest.update(b"\0")
    digest.update(base_image_id.encode())
    digest.update(b"\0")
    for name, path in BUILDER_INPUTS.items():
        if path.is_symlink() or not path.is_file():
            raise JobError(f"builder input is unavailable: {path}")
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256_file(path)))
        digest.update(b"\0")
    return digest.hexdigest()


def image_name(distro: str, input_sha256: str) -> str:
    if not SHA256_RE.fullmatch(input_sha256):
        raise JobError("invalid DEB image-input digest")
    return f"localhost/xpra-deb-builder:{distro}-{input_sha256[:16]}"


def image_lock_path(distro: str, input_sha256: str) -> Path:
    if distro not in DISTROS or not SHA256_RE.fullmatch(input_sha256):
        raise JobError("invalid DEB image lock identity")
    return LOCK_ROOT / "images" / f"{distro}-{input_sha256}.lock"


@contextmanager
def image_build_lock(distro: str, input_sha256: str) -> Iterator[int]:
    """Serialize one mutable image tag and survive a killed publisher parent."""
    path = image_lock_path(distro, input_sha256)
    background_job.ensure_private_directory(path.parent, create=True)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != 0o600
            or details.st_nlink != 1
        ):
            raise JobError(f"unsafe DEB image-build lock: {path}")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield descriptor
    finally:
        # The exact Podman build child inherits this open-file description.  A
        # killed worker must not let another worker race its mutable tag.
        os.close(descriptor)


def inspect_image(
    image: str,
    distro: str,
    input_sha256: str,
    base_image_id: str,
) -> str:
    try:
        payload = json.loads(command(["podman", "image", "inspect", image]).stdout)
    except json.JSONDecodeError as error:
        raise JobError(f"invalid image inspection output: {image}") from error
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise JobError(f"unexpected image inspection output: {image}")
    item = payload[0]
    image_id = str(item.get("Id", "")).removeprefix("sha256:")
    labels = item.get("Labels")
    if labels is None and isinstance(item.get("Config"), dict):
        labels = item["Config"].get("Labels")
    expected = {
        "io.xpra.lab.deb-builder": "true",
        "io.xpra.lab.base-image-id": base_image_id,
        "io.xpra.lab.distro": distro,
        "io.xpra.lab.image-input": input_sha256,
        "io.xpra.lab.owner": OWNER,
    }
    if not SHA256_RE.fullmatch(image_id) or not isinstance(labels, dict):
        raise JobError(f"invalid DEB builder image metadata: {image}")
    if any(labels.get(key) != value for key, value in expected.items()):
        raise JobError(f"DEB builder image labels do not match: {image}")
    return image_id


def pulled_base_image_id(distro: str) -> str:
    reference = DISTROS[distro]
    command(["podman", "pull", "--quiet", reference])
    try:
        payload = json.loads(command(["podman", "image", "inspect", reference]).stdout)
    except json.JSONDecodeError as error:
        raise JobError(f"invalid DEB base image inspection: {reference}") from error
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise JobError(f"unexpected DEB base image inspection: {reference}")
    base_image_id = str(payload[0].get("Id", "")).removeprefix("sha256:")
    if not SHA256_RE.fullmatch(base_image_id):
        raise JobError(f"invalid DEB base image identity: {reference}")
    return base_image_id


def ensure_image(distro: str) -> tuple[str, str, str, str]:
    base_image_id = pulled_base_image_id(distro)
    input_sha256 = image_input_sha256(distro, base_image_id)
    image = image_name(distro, input_sha256)
    with image_build_lock(distro, input_sha256) as lock_fd:
        exists = command(["podman", "image", "exists", image], check=False)
        if exists.returncode == 0:
            return (
                image,
                inspect_image(image, distro, input_sha256, base_image_id),
                input_sha256,
                base_image_id,
            )
        if exists.returncode != 1:
            raise JobError(f"cannot inspect DEB builder image: {image}")
        build_id = str(uuid.uuid4())
        argv = [
            "podman",
            "build",
            "--pull=never",
            "--build-arg",
            f"BASE_IMAGE=sha256:{base_image_id}",
            "--label",
            f"io.xpra.lab.base-image-id={base_image_id}",
            "--label",
            "io.xpra.lab.deb-builder=true",
            "--label",
            f"io.xpra.lab.distro={distro}",
            "--label",
            f"io.xpra.lab.image-build-id={build_id}",
            "--label",
            f"io.xpra.lab.image-input={input_sha256}",
            "--label",
            f"io.xpra.lab.owner={OWNER}",
            "--tag",
            image,
            "--file",
            "Containerfile",
            "-",
        ]
        entries = tuple(
            container_payload.PayloadEntry(path, PurePosixPath(name))
            for name, path in BUILDER_INPUTS.items()
        )
        container_payload.stream_to_process(argv, entries, pass_fds=(lock_fd,))
        image_id = inspect_image(image, distro, input_sha256, base_image_id)
        item = json.loads(command(["podman", "image", "inspect", image_id]).stdout)[0]
        labels = item.get("Labels") or item.get("Config", {}).get("Labels", {})
        if labels.get("io.xpra.lab.image-build-id") != build_id:
            raise JobError("DEB builder image build identity does not match")
        return image, image_id, input_sha256, base_image_id


def selection_digest(selection: str, lab_root: Path = LAB_ROOT) -> str:
    value = command(
        [
            sys.executable,
            str(SELECTION_TOOL),
            "--lab-root",
            str(lab_root),
            "--selection",
            selection,
            "digest",
        ]
    ).stdout.strip()
    if not SHA256_RE.fullmatch(value):
        raise JobError("selection resolver returned an invalid digest")
    return value


def hydrate_source_arguments(args: argparse.Namespace) -> None:
    source_state = Path(args.source_state)
    values = validate_source_state(source_state)
    args.checkout_commit = values["checkout_commit"]
    args.source = values["source_commit"]
    args.source_ref_commit = values["source_ref_commit"]
    args.source_bundle = Path(values["source_bundle"])
    args.source_ref = values["source_ref"]
    args.workflow_sha256 = values["workflow_sha256"]


def selection_cache_root(selection_sha256: str, cache_sha256: str) -> Path:
    if not SHA256_RE.fullmatch(selection_sha256) or not SHA256_RE.fullmatch(
        cache_sha256
    ):
        raise JobError("invalid DEB selection cache identity")
    return SELECTION_ROOT / f"{selection_sha256}-{cache_sha256}"


def selection_partial_path() -> Path:
    return SELECTION_ROOT / ".selection-cache.partial"


def selection_partial_marker_path() -> Path:
    return SELECTION_ROOT / ".selection-cache.partial.owner.json"


def selection_lock_path() -> Path:
    return SELECTION_ROOT / ".selection-cache.lock"


@contextmanager
def selection_cache_lock() -> Iterator[int]:
    """Serialize content-addressed selection-cache publication and recovery."""
    path = selection_lock_path()
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != 0o600
            or details.st_nlink != 1
        ):
            raise JobError(f"unsafe DEB selection cache lock: {path}")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield descriptor
    finally:
        # Closing preserves the lock in an inherited publisher FD until that
        # subprocess also exits; explicit LOCK_UN would release it globally.
        os.close(descriptor)


def _selection_tree_entries(root: Path) -> tuple[tuple[Path, os.stat_result], ...]:
    try:
        root_details = root.lstat()
    except OSError as error:
        raise JobError(f"DEB selection tree is unavailable: {root}") from error
    if (
        not stat.S_ISDIR(root_details.st_mode)
        or root_details.st_uid != os.getuid()
        or stat.S_IMODE(root_details.st_mode) != 0o700
    ):
        raise JobError(f"DEB selection tree root is not exactly private: {root}")
    entries: list[tuple[Path, os.stat_result]] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            children = sorted(directory.iterdir(), key=lambda item: os.fsencode(item.name))
        except OSError as error:
            raise JobError(f"cannot enumerate DEB selection tree: {directory}") from error
        child_directories: list[Path] = []
        for child in children:
            try:
                details = child.lstat()
            except OSError as error:
                raise JobError(f"cannot inspect DEB selection cache entry: {child}") from error
            if details.st_uid != os.getuid():
                raise JobError(f"DEB selection cache entry has the wrong owner: {child}")
            if stat.S_ISDIR(details.st_mode):
                if stat.S_IMODE(details.st_mode) != 0o700:
                    raise JobError(
                        f"DEB selection cache directory mode is not 0700: {child}"
                    )
                child_directories.append(child)
            elif stat.S_ISREG(details.st_mode):
                if stat.S_IMODE(details.st_mode) != 0o600 or details.st_nlink != 1:
                    raise JobError(
                        f"DEB selection cache file is not exactly private: {child}"
                    )
            else:
                raise JobError(f"unsupported DEB selection cache entry: {child}")
            entries.append((child, details))
        pending.extend(reversed(child_directories))
    return tuple(sorted(entries, key=lambda item: item[0].relative_to(root).as_posix()))


def selection_tree_sha256(root: Path) -> str:
    """Digest the exact private entry set, kinds, modes, and file contents."""
    digest = hashlib.sha256(b"xpra-deb-selection-tree-v1\0")
    for path, details in _selection_tree_entries(root):
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


def normalize_selection_tree(root: Path) -> None:
    """Make a newly generated snapshot exactly private before digesting it."""
    try:
        root_details = root.lstat()
    except OSError as error:
        raise JobError(f"DEB selection snapshot is unavailable: {root}") from error
    if not stat.S_ISDIR(root_details.st_mode) or root_details.st_uid != os.getuid():
        raise JobError(f"DEB selection snapshot root is unsafe: {root}")
    pending = [root]
    while pending:
        directory = pending.pop()
        directory.chmod(0o700)
        for child in directory.iterdir():
            details = child.lstat()
            if details.st_uid != os.getuid() or child.is_symlink():
                raise JobError(f"DEB selection snapshot entry is unsafe: {child}")
            if stat.S_ISDIR(details.st_mode):
                pending.append(child)
            elif stat.S_ISREG(details.st_mode) and details.st_nlink == 1:
                child.chmod(0o600)
            else:
                raise JobError(f"unsupported DEB selection snapshot entry: {child}")


def validate_selection_state(path: Path) -> dict[str, str]:
    payload = background_job.load_json(path)
    expected_keys = {
        "owner",
        "schema",
        "selection",
        "selection_sha256",
        "snapshot_tree_sha256",
    }
    if set(payload) != expected_keys or any(
        payload.get(key) != value
        for key, value in {
            "owner": SELECTION_OWNER,
            "schema": 1,
            "selection": ACTIVE_SELECTION,
        }.items()
    ):
        raise JobError("DEB selection cache metadata does not match its owner")
    selection = str(payload["selection"])
    selection_sha256 = str(payload.get("selection_sha256", ""))
    tree_sha256 = str(payload.get("snapshot_tree_sha256", ""))
    cache_sha256 = sha256_file(path)
    if any(
        SHA256_RE.fullmatch(value) is None
        for value in (selection_sha256, tree_sha256, cache_sha256)
    ):
        raise JobError("DEB selection cache metadata has an invalid digest")
    root = selection_cache_root(selection_sha256, cache_sha256)
    if path != root / "selection.json":
        raise JobError(f"DEB selection cache metadata has an unexpected path: {path}")
    background_job.ensure_private_directory(root)
    if stat.S_IMODE(root.lstat().st_mode) != 0o700:
        raise JobError("DEB selection cache root mode is not exactly 0700")
    entries = tuple(root.iterdir())
    if {entry.name for entry in entries} != {"lab", "selection.json"}:
        raise JobError("DEB selection cache does not have its exact immutable entry set")
    background_job.ensure_private_regular(path)
    if stat.S_IMODE(path.lstat().st_mode) != 0o600:
        raise JobError("DEB selection cache metadata mode is not exactly 0600")
    snapshot = root / "lab"
    observed_tree = selection_tree_sha256(snapshot)
    if observed_tree != tree_sha256:
        raise JobError("DEB selection cache tree digest does not match")
    if selection_digest(selection, snapshot) != selection_sha256:
        raise JobError("DEB selection cache semantic digest does not match")
    return {
        "selection": selection,
        "selection_cache_sha256": cache_sha256,
        "selection_sha256": selection_sha256,
        "selection_snapshot": str(snapshot),
        "selection_state": str(path),
        "snapshot_tree_sha256": tree_sha256,
    }


def selection_partial_record(selection: str, selection_sha256: str) -> dict[str, Any]:
    return {
        "kind": "selection-cache-partial",
        "owner": SELECTION_OWNER,
        "partial": str(selection_partial_path()),
        "schema": 1,
        "selection": selection,
        "selection_sha256": selection_sha256,
    }


def publish_selection_partial_marker(payload: dict[str, Any]) -> None:
    """Durably publish the complete marker before creating its partial."""
    background_job.publish_json(selection_partial_marker_path(), payload)


def validate_selection_partial_marker() -> dict[str, str]:
    payload = background_job.load_json(selection_partial_marker_path())
    expected_keys = {
        "kind",
        "owner",
        "partial",
        "schema",
        "selection",
        "selection_sha256",
    }
    if set(payload) != expected_keys or any(
        payload.get(key) != value
        for key, value in {
            "kind": "selection-cache-partial",
            "owner": SELECTION_OWNER,
            "partial": str(selection_partial_path()),
            "schema": 1,
            "selection": ACTIVE_SELECTION,
        }.items()
    ):
        raise JobError("DEB selection partial marker does not match its owner")
    selection_sha256 = str(payload.get("selection_sha256", ""))
    if SHA256_RE.fullmatch(selection_sha256) is None:
        raise JobError("DEB selection partial marker has an invalid digest")
    return {
        "selection": str(payload["selection"]),
        "selection_sha256": selection_sha256,
    }


def remove_selection_partial_directory() -> None:
    partial = selection_partial_path()
    try:
        details = partial.lstat()
    except OSError as error:
        raise JobError(f"DEB selection partial is unavailable: {partial}") from error
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid():
        raise JobError(f"untrusted DEB selection partial: {partial}")
    pending = [partial]
    while pending:
        directory = pending.pop()
        for child in directory.iterdir():
            child_details = child.lstat()
            if child_details.st_uid != os.getuid() or child.is_symlink():
                raise JobError(f"unsafe entry in owned DEB selection partial: {child}")
            if stat.S_ISDIR(child_details.st_mode):
                pending.append(child)
            elif not stat.S_ISREG(child_details.st_mode) or child_details.st_nlink != 1:
                raise JobError(f"unsafe entry in owned DEB selection partial: {child}")
    shutil.rmtree(partial)


def reject_unknown_selection_partials() -> None:
    allowed = {
        selection_lock_path(),
        selection_partial_marker_path(),
        selection_partial_path(),
    }
    unknown = sorted(
        str(path)
        for path in SELECTION_ROOT.iterdir()
        if path.name.startswith(".") and path not in allowed
    )
    if unknown:
        raise JobError(
            "unowned DEB selection cache partials require operator review: "
            + ", ".join(unknown)
        )


def recover_selection_partial() -> None:
    """Reclaim only the deterministic partial proven by its external marker."""
    marker = selection_partial_marker_path()
    partial = selection_partial_path()
    marker_present = marker.exists() or marker.is_symlink()
    partial_present = partial.exists() or partial.is_symlink()
    if partial_present and not marker_present:
        raise JobError(f"unowned DEB selection cache partial requires review: {partial}")
    if marker_present:
        validate_selection_partial_marker()
        if partial_present:
            remove_selection_partial_directory()
        background_job.ensure_private_regular(marker)
        marker.unlink()
    reject_unknown_selection_partials()


def existing_selection_caches(selection: str, selection_sha256: str) -> tuple[dict[str, str], ...]:
    caches: list[dict[str, str]] = []
    name_pattern = re.compile(r"([0-9a-f]{64})-([0-9a-f]{64})")
    for path in sorted(SELECTION_ROOT.iterdir(), key=lambda item: item.name):
        if path.name.startswith("."):
            continue
        if path.is_symlink() or not path.is_dir() or name_pattern.fullmatch(path.name) is None:
            raise JobError(f"unowned entry in the DEB selection cache: {path}")
        values = validate_selection_state(path / "selection.json")
        if (
            values["selection"] == selection
            and values["selection_sha256"] == selection_sha256
        ):
            caches.append(values)
    if len(caches) > 1:
        raise JobError("DEB selection cache has duplicate content-addressed entries")
    return tuple(caches)


def freeze_selection_cache_locked(
    selection: str,
    selection_sha256: str,
    lock_fd: int,
) -> dict[str, str]:
    cached = existing_selection_caches(selection, selection_sha256)
    if cached:
        return cached[0]
    partial = selection_partial_path()
    marker = selection_partial_marker_path()
    marker_published = False
    try:
        publish_selection_partial_marker(
            selection_partial_record(selection, selection_sha256)
        )
        marker_published = True
        partial.mkdir(mode=0o700)
        snapshot = partial / "lab"
        command(
            [
                sys.executable,
                str(SELECTION_TOOL),
                "--lab-root",
                str(LAB_ROOT),
                "--selection",
                selection,
                "snapshot",
                "--destination",
                str(snapshot),
            ],
            pass_fds=(lock_fd,),
        )
        normalize_selection_tree(snapshot)
        if selection_digest(selection, snapshot) != selection_sha256:
            raise JobError("selection changed while freezing the DEB payload")
        tree_sha256 = selection_tree_sha256(snapshot)
        state_payload = {
            "owner": SELECTION_OWNER,
            "schema": 1,
            "selection": selection,
            "selection_sha256": selection_sha256,
            "snapshot_tree_sha256": tree_sha256,
        }
        background_job.publish_json(partial / "selection.json", state_payload)
        cache_sha256 = sha256_file(partial / "selection.json")
        target = selection_cache_root(selection_sha256, cache_sha256)
        try:
            container_payload.rename_no_replace(partial, target)
        except FileExistsError:
            observed = validate_selection_state(target / "selection.json")
            if (
                observed["selection"] != selection
                or observed["selection_sha256"] != selection_sha256
                or observed["selection_cache_sha256"] != cache_sha256
            ):
                raise JobError("DEB selection cache publication race has wrong provenance")
    finally:
        if partial.exists() or partial.is_symlink():
            remove_selection_partial_directory()
        if marker_published and (marker.exists() or marker.is_symlink()):
            background_job.ensure_private_regular(marker)
            marker.unlink()
    return validate_selection_state(target / "selection.json")


def freeze_selection_cache(selection: str) -> dict[str, str]:
    """Freeze the complete queue before a RUN exists and retain it by content."""
    prepare_state()
    if selection != ACTIVE_SELECTION or SELECTOR_RE.fullmatch(selection) is None:
        raise JobError(f"DEB builds require the complete {ACTIVE_SELECTION} queue")
    expected = selection_digest(selection)
    with selection_cache_lock() as lock_fd:
        recover_selection_partial()
        if selection_digest(selection) != expected:
            raise JobError("selection changed before freezing the DEB payload")
        values = freeze_selection_cache_locked(selection, expected, lock_fd)
        if selection_digest(selection) != expected:
            raise JobError("selection changed while publishing the DEB payload")
        return values


@contextmanager
def build_payload(args: argparse.Namespace) -> Iterator[tuple[container_payload.PayloadEntry, ...]]:
    source_bundle = Path(args.source_bundle)
    background_job.ensure_private_regular(source_bundle)
    snapshot = Path(args.selection_snapshot)
    state = Path(args.selection_state)
    values = validate_selection_state(state)
    expected = {
        "selection": str(args.selection),
        "selection_cache_sha256": str(args.selection_cache_sha256),
        "selection_sha256": str(args.selection_sha256),
        "selection_snapshot": str(snapshot),
        "selection_state": str(state),
    }
    if any(values.get(key) != value for key, value in expected.items()):
        raise JobError("owned DEB selection cache provenance does not match")
    yield (
        container_payload.PayloadEntry(source_bundle, PurePosixPath("source.bundle")),
        container_payload.PayloadEntry(snapshot, PurePosixPath("lab")),
        container_payload.PayloadEntry(state, PurePosixPath("selection.json")),
    )


def validate_build_arguments(args: argparse.Namespace) -> None:
    if args.distro not in DISTROS:
        raise JobError(f"unsupported DEB distribution: {args.distro}")
    if args.selection != ACTIVE_SELECTION:
        raise JobError(f"DEB builds require the complete {ACTIVE_SELECTION} queue")
    ref_check = command(["git", "check-ref-format", args.source_ref], check=False)
    if (
        ref_check.returncode
        or not args.source_ref.startswith(("refs/heads/", "refs/remotes/"))
        or args.source_ref.rsplit("/", 1)[-1] != "master"
    ):
        raise JobError(f"invalid master source ref: {args.source_ref!r}")
    for name in ("checkout_commit", "source", "source_ref_commit"):
        if not COMMIT_RE.fullmatch(str(getattr(args, name))):
            raise JobError(f"invalid {name.replace('_', ' ')}")
    if not SHA256_RE.fullmatch(args.workflow_sha256):
        raise JobError("invalid source workflow digest")
    if not args.container_name.startswith("xpra-deb-") or not NAME_RE.fullmatch(
        args.container_name
    ):
        raise JobError("invalid DEB container name")
    if not SHA256_RE.fullmatch(str(args.selection_sha256)) or not SHA256_RE.fullmatch(
        str(args.selection_cache_sha256)
    ):
        raise JobError("invalid DEB selection cache digest")
    try:
        parsed_build_id = uuid.UUID(str(args.build_id))
    except (TypeError, ValueError) as error:
        raise JobError("invalid DEB build identity") from error
    if parsed_build_id.version != 4 or str(parsed_build_id) != args.build_id:
        raise JobError("DEB build identity is not a canonical UUID4")
    generated_paths = tuple(
        Path(getattr(args, name))
        for name in ("container_state", "output", "output_partial")
    )
    if any(
        not path.is_absolute()
        or ".." in path.parts
        or not path.is_relative_to(STATE_ROOT)
        for path in generated_paths
    ):
        raise JobError("DEB generated paths must stay below the private package state root")
    if len(set(generated_paths)) != len(generated_paths):
        raise JobError("DEB generated paths must be distinct")
    source_state = Path(args.source_state)
    source_bundle = Path(args.source_bundle)
    for label, path in (("source state", source_state), ("source bundle", source_bundle)):
        if (
            not path.is_absolute()
            or ".." in path.parts
            or not path.is_relative_to(SOURCE_ROOT)
        ):
            raise JobError(f"DEB {label} path is outside the immutable source cache")
    source_values = validate_source_state(source_state)
    expected_source = {
        "checkout_commit": str(args.checkout_commit),
        "source_bundle": str(source_bundle),
        "source_commit": str(args.source),
        "source_ref": str(args.source_ref),
        "source_ref_commit": str(args.source_ref_commit),
        "workflow_sha256": str(args.workflow_sha256),
    }
    if any(source_values.get(key) != value for key, value in expected_source.items()):
        raise JobError("DEB build arguments do not match their source snapshot")
    selection_snapshot = Path(args.selection_snapshot)
    selection_state = Path(args.selection_state)
    selection_values = validate_selection_state(selection_state)
    expected_selection = {
        "selection": str(args.selection),
        "selection_cache_sha256": str(args.selection_cache_sha256),
        "selection_sha256": str(args.selection_sha256),
        "selection_snapshot": str(selection_snapshot),
        "selection_state": str(selection_state),
    }
    if any(
        selection_values.get(key) != value
        for key, value in expected_selection.items()
    ):
        raise JobError("DEB build arguments do not match their selection cache")


def container_labels(container_name: str, args: argparse.Namespace) -> dict[str, str]:
    return {
        "io.xpra.lab.build-id": args.build_id,
        "io.xpra.lab.deb-build": "true",
        "io.xpra.lab.distro": args.distro,
        "io.xpra.lab.owner": OWNER,
        "io.xpra.lab.run-name": container_name.removeprefix("xpra-deb-"),
        "io.xpra.lab.selection": args.selection,
        "io.xpra.lab.selection-cache": args.selection_cache_sha256,
        "io.xpra.lab.source": args.source,
    }


def load_container_record(args: argparse.Namespace) -> dict[str, str] | None:
    path = Path(args.container_state)
    if not path.exists() and not path.is_symlink():
        return None
    payload = background_job.load_json(path)
    expected = {
        "build_id": args.build_id,
        "container_name": args.container_name,
        "owner": OWNER,
        "schema": 1,
        "selection_cache_sha256": args.selection_cache_sha256,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise JobError("DEB container ownership record does not match")
    values = {
        key: str(payload.get(key, ""))
        for key in (
            "base_image_id",
            "builder_image_input_sha256",
            "container_id",
            "image_id",
        )
    }
    if any(not SHA256_RE.fullmatch(value) for value in values.values()):
        raise JobError("DEB container ownership record has an invalid immutable ID")
    return values


def remove_owned_container(
    args: argparse.Namespace,
    *,
    tolerate_invalid_record: bool = False,
) -> None:
    recorded = load_container_record(args)
    if recorded is None and not tolerate_invalid_record:
        raise JobError("DEB container has no immutable ownership record")
    identifier = recorded["container_id"] if recorded else args.container_name
    exists = command(["podman", "container", "exists", identifier], check=False)
    if exists.returncode == 1:
        return
    if exists.returncode != 0:
        raise JobError(f"cannot inspect package container: {identifier}")
    payload = json.loads(
        command(["podman", "container", "inspect", identifier]).stdout
    )
    if not isinstance(payload, list) or len(payload) != 1:
        raise JobError(f"invalid package container inspection: {identifier}")
    item = payload[0]
    container_id = str(item.get("Id", ""))
    container_name = str(item.get("Name", "")).lstrip("/")
    image_id = str(item.get("Image", "")).removeprefix("sha256:")
    config = item.get("Config", {})
    labels = config.get("Labels") if isinstance(config, dict) else None
    expected = container_labels(args.container_name, args)
    if (
        not SHA256_RE.fullmatch(container_id)
        or container_name != args.container_name
        or not isinstance(labels, dict)
        or any(labels.get(key) != value for key, value in expected.items())
        or (recorded and container_id != recorded["container_id"])
        or (recorded and image_id != recorded["image_id"])
    ):
        raise JobError(f"refusing package container with mismatched ownership: {identifier}")
    command(["podman", "rm", "--force", container_id])


def validate_single_xz_stream(payload: bytes, *, max_bytes: int) -> None:
    """Fully consume exactly one xz stream without retaining its expansion."""
    decoder = lzma.LZMADecompressor(
        format=lzma.FORMAT_XZ,
        memlimit=MAX_XZ_MEMORY_BYTES,
    )
    total = 0
    offset = 0
    while offset < len(payload) or not decoder.needs_input:
        block = payload[offset : offset + 64 * 1024] if decoder.needs_input else b""
        offset += len(block)
        expanded = decoder.decompress(block, max_length=1024 * 1024)
        total += len(expanded)
        if total > max_bytes:
            raise JobError(f"xz stream expands past {max_bytes} bytes")
        if decoder.eof:
            if decoder.unused_data or offset != len(payload):
                raise JobError("xz stream contains trailing or concatenated data")
            return
        if decoder.needs_input and offset == len(payload):
            break
    raise JobError("xz stream is truncated")


def deb_archive_path(value: str) -> PurePosixPath:
    """Normalize dpkg's conventional leading ``./`` before strict validation."""
    while value.startswith("./"):
        value = value[2:]
    if not value:
        raise JobError("Debian tar archive contains an empty path")
    try:
        return container_payload.archive_path(value)
    except container_payload.PayloadError as error:
        raise JobError("Debian tar archive contains an unsafe path") from error


def deb_archive_member_path(member: tarfile.TarInfo) -> PurePosixPath | None:
    """Return a safe member path, or ``None`` for dpkg's canonical root entry."""
    if member.name not in {".", "./"}:
        return deb_archive_path(member.name)
    if (
        member.type != tarfile.DIRTYPE
        or member.size != 0
        or member.mode != 0o755
        or member.uid != 0
        or member.gid != 0
        or member.uname not in {"", "root"}
        or member.gname not in {"", "root"}
        or member.linkname
        or member.devmajor != 0
        or member.devminor != 0
        or member.pax_headers
        or member.sparse is not None
    ):
        raise JobError("Debian tar archive contains unsafe root directory metadata")
    return None


def deb_control_fields(path: Path) -> dict[str, str]:
    """Parse the required control fields from a Debian ar archive."""
    control_payload = b""
    debian_binary = b""
    data_member: tuple[int, int] | None = None
    with path.open("rb") as stream:
        if stream.read(8) != b"!<arch>\n":
            raise JobError(f"invalid Debian ar signature: {path.name}")
        seen: set[str] = set()
        ar_order: list[str] = []
        while header := stream.read(60):
            if len(header) != 60 or header[58:60] != b"`\n":
                raise JobError(f"invalid Debian ar member header: {path.name}")
            try:
                member_name = header[:16].decode("ascii").strip().removesuffix("/")
                member_size = int(header[48:58].decode("ascii").strip())
            except (UnicodeDecodeError, ValueError) as error:
                raise JobError(f"invalid Debian ar member metadata: {path.name}") from error
            if not member_name or member_name in seen or member_size < 0:
                raise JobError(f"unsafe Debian ar member: {member_name!r}")
            seen.add(member_name)
            ar_order.append(member_name)
            if member_name == "debian-binary":
                if member_size != 4:
                    raise JobError(f"invalid Debian format marker: {path.name}")
                debian_binary = stream.read(member_size)
            elif member_name == "control.tar.xz":
                if control_payload:
                    raise JobError(f"duplicate Debian control archive: {path.name}")
                if member_size > MAX_DEB_CONTROL_ARCHIVE_BYTES:
                    raise JobError(
                        "Debian control archive exceeds "
                        f"{MAX_DEB_CONTROL_ARCHIVE_BYTES} bytes: {path.name}"
                    )
                control_payload = stream.read(member_size)
            elif member_name == "data.tar.xz":
                if data_member is not None:
                    raise JobError(f"duplicate Debian data archive: {path.name}")
                data_member = (stream.tell(), member_size)
                stream.seek(member_size, os.SEEK_CUR)
            else:
                raise JobError(f"unexpected Debian ar member {member_name!r}: {path.name}")
            if member_size % 2 and stream.read(1) != b"\n":
                raise JobError(f"invalid Debian ar padding: {path.name}")
        if stream.tell() != path.stat().st_size:
            raise JobError(f"Debian ar archive was not consumed exactly: {path.name}")
    if ar_order != ["debian-binary", "control.tar.xz", "data.tar.xz"]:
        raise JobError(f"Debian ar members are not in canonical order: {path.name}")
    if debian_binary != b"2.0\n" or not control_payload or data_member is None:
        raise JobError(f"incomplete Debian package structure: {path.name}")
    try:
        validate_single_xz_stream(
            control_payload,
            max_bytes=MAX_DEB_CONTROL_TAR_BYTES,
        )
        control_stream = io.BytesIO(control_payload)
        with tarfile.open(
            fileobj=control_stream,
            mode="r|xz",
            tarinfo=container_payload.BoundedTarInfo,
        ) as archive:
            control_bytes: bytes | None = None
            expanded_bytes = 0
            names: set[PurePosixPath] = set()
            root_seen = False
            for member_count, member in enumerate(archive, start=1):
                if member_count > MAX_DEB_CONTROL_MEMBERS:
                    raise JobError(
                        "Debian control archive exceeds "
                        f"{MAX_DEB_CONTROL_MEMBERS} members: {path.name}"
                )
                try:
                    name = deb_archive_member_path(member)
                except JobError as error:
                    raise JobError(
                        f"Debian control archive has an unsafe path: {path.name}"
                    ) from error
                if name is None:
                    if root_seen:
                        raise JobError(
                            f"Debian control archive has a duplicate root: {path.name}"
                        )
                    root_seen = True
                    continue
                if name in names:
                    raise JobError(
                        f"Debian control archive has a duplicate path: {path.name}"
                    )
                names.add(name)
                if member.size < 0:
                    raise JobError(f"Debian control archive has a negative size: {path.name}")
                if member.isreg():
                    expanded_bytes += member.size
                    if expanded_bytes > MAX_DEB_CONTROL_EXPANDED_BYTES:
                        raise JobError(
                            "Debian control archive expands past "
                            f"{MAX_DEB_CONTROL_EXPANDED_BYTES} bytes: {path.name}"
                        )
                    is_control = str(name) == "control"
                    if is_control and member.size > MAX_DEB_CONTROL_FILE_BYTES:
                        raise JobError(
                            f"Debian package control file is too large: {path.name}"
                        )
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        raise JobError(
                            f"Debian control member is unreadable: {path.name}:{name}"
                        )
                    limit = (
                        MAX_DEB_CONTROL_FILE_BYTES + 1
                        if is_control
                        else member.size + 1
                    )
                    payload = extracted.read(limit)
                    if len(payload) != member.size:
                        raise JobError(
                            f"Debian control member is truncated: {path.name}:{name}"
                        )
                    if is_control:
                        if control_bytes is not None:
                            raise JobError(
                                f"Debian package has no unique control file: {path.name}"
                            )
                        control_bytes = payload
                elif member.isdir():
                    if member.size:
                        raise JobError(
                            f"Debian control directory has payload bytes: {path.name}:{name}"
                        )
                else:
                    raise JobError(
                        f"Debian control archive has a pathological entry: {path.name}:{name}"
                    )
            if control_bytes is None:
                raise JobError(f"Debian package has no unique control file: {path.name}")
            control_text = control_bytes.decode("utf-8")
        if control_stream.tell() != len(control_payload):
            raise JobError(f"Debian control archive has trailing data: {path.name}")
    except (EOFError, UnicodeDecodeError, lzma.LZMAError, tarfile.TarError) as error:
        raise JobError(f"invalid Debian control archive: {path.name}") from error
    try:
        offset, size = data_member
        with _ArMemberReader(path, offset, size) as member_stream:
            decompressed = _SingleXZReader(member_stream)
            bounded = _BoundedReader(
                decompressed,
                MAX_DEB_DATA_TAR_BYTES,
                "Debian data tar stream",
            )
            expanded_bytes = 0
            names: set[PurePosixPath] = set()
            root_seen = False
            with tarfile.open(
                fileobj=bounded,
                mode="r|",
                tarinfo=container_payload.BoundedTarInfo,
            ) as archive:
                for member_count, member in enumerate(archive, start=1):
                    if member_count > MAX_DEB_DATA_MEMBERS:
                        raise JobError(
                            "Debian data archive exceeds "
                            f"{MAX_DEB_DATA_MEMBERS} members: {path.name}"
                        )
                    try:
                        name = deb_archive_member_path(member)
                    except JobError as error:
                        raise JobError(
                            f"Debian data archive has an unsafe path: {path.name}"
                        ) from error
                    if name is None:
                        if root_seen:
                            raise JobError(
                                f"Debian data archive has a duplicate root: {path.name}"
                            )
                        root_seen = True
                        continue
                    if name in names:
                        raise JobError(
                            f"Debian data archive has a duplicate path: {path.name}"
                        )
                    names.add(name)
                    if member.size < 0 or member.size > MAX_DEB_DATA_MEMBER_BYTES:
                        raise JobError(
                            f"Debian data member is too large: {path.name}:{name}"
                        )
                    if member.isfile():
                        expanded_bytes += member.size
                        if expanded_bytes > MAX_DEB_DATA_EXPANDED_BYTES:
                            raise JobError(
                                "Debian data archive expands past "
                                f"{MAX_DEB_DATA_EXPANDED_BYTES} bytes: {path.name}"
                            )
                        content = archive.extractfile(member)
                        if content is None:
                            raise JobError(
                                f"Debian data member is unreadable: {path.name}"
                            )
                        consumed = 0
                        while block := content.read(1024 * 1024):
                            consumed += len(block)
                            if consumed > member.size:
                                raise JobError(
                                    "Debian data member exceeds its header: "
                                    f"{path.name}:{name}"
                                )
                        if consumed != member.size:
                            raise JobError(
                                f"Debian data member is truncated: {path.name}:{name}"
                            )
                    elif member.isdir():
                        if member.size:
                            raise JobError(
                                "Debian data directory has payload bytes: "
                                f"{path.name}:{name}"
                            )
                    elif member.issym() or member.islnk():
                        if member.size:
                            raise JobError(
                                f"Debian data link has payload bytes: {path.name}:{name}"
                            )
                        target = member.linkname
                        if not target or target.startswith("/"):
                            raise JobError(
                                f"Debian data archive has an unsafe link: {path.name}:{name}"
                            )
                        normalized = posixpath.normpath(
                            posixpath.join(str(name.parent), target)
                        )
                        if normalized == ".." or normalized.startswith(("../", "/")):
                            raise JobError(
                                f"Debian data link escapes its archive: {path.name}:{name}"
                            )
                    else:
                        raise JobError(
                            "Debian data archive has a pathological entry: "
                            f"{path.name}:{name}"
                        )
            while bounded.read(1024 * 1024):
                pass
            if member_stream.tell() != size:
                raise JobError(f"Debian data archive has trailing data: {path.name}")
    except (EOFError, lzma.LZMAError, tarfile.TarError) as error:
        raise JobError(f"invalid Debian data archive: {path.name}") from error
    fields: dict[str, str] = {}
    paragraph_ended = False
    for line in control_text.splitlines():
        if not line:
            paragraph_ended = True
            continue
        if paragraph_ended:
            raise JobError(f"Debian package control has multiple paragraphs: {path.name}")
        if line[:1].isspace():
            continue
        key, separator, value = line.partition(":")
        if separator and key in {"Package", "Version", "Architecture"}:
            normalized = key.lower()
            if normalized in fields:
                raise JobError(f"duplicate Debian control field {key}: {path.name}")
            fields[normalized] = value.strip()
    if set(fields) != {"package", "version", "architecture"} or any(
        not value or "\n" in value for value in fields.values()
    ):
        raise JobError(f"Debian package control fields are incomplete: {path.name}")
    return fields


def validation_paths(path: Path) -> tuple[Path, Path, Path]:
    directory = path.parent / f".{path.name}.validate"
    partial = path.parent / f".{directory.name}.partial"
    marker = path.parent / f".{path.name}.validate.owner.json"
    return directory, partial, marker


def remove_validation_directory(path: Path) -> None:
    if path.is_symlink():
        raise JobError(f"refusing symlinked DEB validation scratch: {path}")
    background_job.ensure_private_directory(path)
    if stat.S_IMODE(path.lstat().st_mode) != 0o700:
        raise JobError(f"unsafe DEB validation scratch mode: {path}")
    shutil.rmtree(path)


def recover_validation_scratch(path: Path) -> None:
    """Reclaim only deterministic extraction state bound to this exact output."""
    temporary, partial, marker = validation_paths(path)
    present = [
        candidate
        for candidate in (temporary, partial)
        if candidate.exists() or candidate.is_symlink()
    ]
    marker_present = marker.exists() or marker.is_symlink()
    if present and not marker_present:
        raise JobError(
            "unowned DEB validation scratch requires review: "
            + ", ".join(map(str, present))
        )
    if not marker_present:
        return
    record = background_job.load_json(marker)
    expected_keys = {
        "kind",
        "output",
        "output_device",
        "output_inode",
        "output_size",
        "owner",
        "partial",
        "schema",
        "temporary",
    }
    if set(record) != expected_keys or any(
        record.get(key) != value
        for key, value in {
            "kind": "deb-output-validation",
            "output": str(path),
            "owner": OWNER,
            "partial": str(partial),
            "schema": 1,
            "temporary": str(temporary),
        }.items()
    ):
        raise JobError("DEB validation scratch marker does not match its owner")
    background_job.ensure_private_regular(path)
    details = path.lstat()
    if any(
        record.get(key) != value
        for key, value in {
            "output_device": details.st_dev,
            "output_inode": details.st_ino,
            "output_size": details.st_size,
        }.items()
    ):
        raise JobError("DEB output changed while validation scratch was owned")
    for candidate in (temporary, partial):
        if candidate.exists() or candidate.is_symlink():
            remove_validation_directory(candidate)
    background_job.ensure_private_regular(marker)
    marker.unlink()


def validate_package_tar(path: Path, expected: argparse.Namespace) -> dict[str, Any]:
    background_job.ensure_private_regular(path)
    if path.stat().st_size > MAX_DEB_TAR_BYTES:
        raise JobError(f"package tar exceeds {MAX_DEB_TAR_BYTES} bytes")
    temporary, partial, marker = validation_paths(path)
    recover_validation_scratch(path)
    details = path.lstat()
    background_job.publish_json(
        marker,
        {
            "kind": "deb-output-validation",
            "output": str(path),
            "output_device": details.st_dev,
            "output_inode": details.st_ino,
            "output_size": details.st_size,
            "owner": OWNER,
            "partial": str(partial),
            "schema": 1,
            "temporary": str(temporary),
        },
    )
    try:
        with path.open("rb") as stream:
            container_payload.extract_archive(
                stream,
                temporary,
                max_bytes=MAX_DEB_TAR_BYTES,
            )
        manifest_path = temporary / "manifest.json"
        checksums_path = temporary / "SHA256SUMS"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise JobError("package tar has no manifest.json")
        if checksums_path.is_symlink() or not checksums_path.is_file():
            raise JobError("package tar has no SHA256SUMS")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("schema") != 2:
            raise JobError("package manifest schema is invalid")
        expected_values = {
            "base_image_id": expected.base_image_id,
            "builder_image_id": expected.builder_image_id,
            "builder_image_input_sha256": expected.builder_image_input_sha256,
            "checkout_commit": expected.checkout_commit,
            "distro": expected.distro,
            "selection": expected.selection,
            "selection_cache_sha256": expected.selection_cache_sha256,
            "selection_sha256": expected.selection_sha256,
            "source_commit": expected.source,
            "source_ref": expected.source_ref,
            "source_ref_commit": expected.source_ref_commit,
            "workflow_sha256": expected.workflow_sha256,
        }
        if any(manifest.get(key) != value for key, value in expected_values.items()):
            raise JobError("package manifest provenance does not match the build")
        if manifest.get("architecture") != "amd64":
            raise JobError("package manifest architecture is not amd64")
        base_version = str(manifest.get("base_version", ""))
        debian_version = str(manifest.get("debian_version", ""))
        revision = manifest.get("revision")
        revision_count = manifest.get("revision_first_parent_count")
        if (
            VERSION_RE.fullmatch(base_version) is None
            or not isinstance(revision, int)
            or isinstance(revision, bool)
            or not isinstance(revision_count, int)
            or isinstance(revision_count, bool)
            or revision_count < 1
            or revision != revision_count + 5014
            or not debian_version.startswith(f"{base_version}-r{revision}-")
        ):
            raise JobError("package manifest version and revision are inconsistent")
        for key in ("selection_resolution_sha256", "selection_sha256"):
            if SHA256_RE.fullmatch(str(manifest.get(key, ""))) is None:
                raise JobError(f"package manifest has an invalid {key}")
        packages = manifest.get("packages")
        if not isinstance(packages, list) or not packages:
            raise JobError("package manifest contains no DEB packages")
        names: set[str] = set()
        expected_checksums: list[str] = []
        for entry in packages:
            if not isinstance(entry, dict):
                raise JobError("package manifest entry is invalid")
            name = str(entry.get("name", ""))
            if (
                not name.startswith("xpra")
                or not name.endswith(".deb")
                or Path(name).name != name
                or name in names
            ):
                raise JobError(f"unsafe or duplicate DEB name: {name!r}")
            names.add(name)
            package = temporary / name
            if package.is_symlink() or not package.is_file():
                raise JobError(f"manifest DEB is missing: {name}")
            digest = sha256_file(package)
            if entry.get("sha256") != digest or entry.get("size") != package.stat().st_size:
                raise JobError(f"manifest DEB metadata does not match: {name}")
            fields = deb_control_fields(package)
            if (
                any(entry.get(key) != value for key, value in fields.items())
                or not fields["package"].startswith("xpra")
                or fields["version"] != debian_version
                or fields["architecture"] not in {"all", "amd64"}
                or name
                != (
                    f"{fields['package']}_{fields['version']}_"
                    f"{fields['architecture']}.deb"
                )
            ):
                raise JobError(f"DEB control metadata does not match: {name}")
            expected_checksums.append(f"{digest}  {name}\n")
        archive_debs = {path.name for path in temporary.glob("*.deb")}
        if archive_debs != names:
            raise JobError("package tar DEB set does not exactly match its manifest")
        archive_entries = tuple(temporary.iterdir())
        if any(entry.is_symlink() or not entry.is_file() for entry in archive_entries):
            raise JobError("package tar contains a non-file top-level entry")
        expected_files = {"manifest.json", "SHA256SUMS", *names}
        if {entry.name for entry in archive_entries} != expected_files:
            raise JobError("package tar contains files outside its exact manifest set")
        if checksums_path.read_text(encoding="ascii") != "".join(expected_checksums):
            raise JobError("package tar SHA256SUMS does not match its DEB set")
        return manifest
    finally:
        for candidate in (temporary, partial):
            if candidate.exists() or candidate.is_symlink():
                remove_validation_directory(candidate)
        if marker.exists() or marker.is_symlink():
            background_job.ensure_private_regular(marker)
            marker.unlink()


def build_distribution(args: argparse.Namespace) -> dict[str, Any]:
    prepare_state()
    require_amd64_host()
    validate_build_arguments(args)
    output = Path(args.output)
    if output.exists() or output.is_symlink():
        raise JobError(f"refusing to overwrite DEB output: {output}")
    background_job.ensure_private_directory(output.parent)
    _image, image_id, image_input, base_image_id = ensure_image(args.distro)
    args.builder_image_id = image_id
    args.builder_image_input_sha256 = image_input
    args.base_image_id = base_image_id
    selection_sha256 = args.selection_sha256
    container_name = args.container_name
    labels = container_labels(container_name, args)
    argv = ["podman", "create", "--interactive", "--name", container_name]
    for key, value in labels.items():
        argv.extend(("--label", f"{key}={value}"))
    environment = {
        "XPRA_DEB_DISTRO": args.distro,
        "XPRA_EXPECTED_BASE_IMAGE_ID": base_image_id,
        "XPRA_EXPECTED_BUILDER_IMAGE_ID": image_id,
        "XPRA_EXPECTED_BUILDER_IMAGE_INPUT_SHA": image_input,
        "XPRA_EXPECTED_CHECKOUT_COMMIT": args.checkout_commit,
        "XPRA_EXPECTED_SELECTION_CACHE_SHA": args.selection_cache_sha256,
        "XPRA_EXPECTED_SELECTION_SHA": selection_sha256,
        "XPRA_EXPECTED_SOURCE_COMMIT": args.source,
        "XPRA_EXPECTED_SOURCE_REF": args.source_ref,
        "XPRA_EXPECTED_SOURCE_REF_COMMIT": args.source_ref_commit,
        "XPRA_EXPECTED_WORKFLOW_SHA": args.workflow_sha256,
        "XPRA_LAB_SELECTION": args.selection,
    }
    for key, value in environment.items():
        argv.extend(("--env", f"{key}={value}"))
    argv.append(image_id)
    state_path = Path(args.container_state)
    if state_path.exists() or state_path.is_symlink():
        raise JobError(f"DEB container ownership record already exists: {state_path}")
    output_published = False
    try:
        container_id = command(argv).stdout.strip()
        if not SHA256_RE.fullmatch(container_id):
            raise JobError("podman create returned an invalid DEB container ID")
        background_job.publish_json(
            state_path,
            {
                "build_id": args.build_id,
                "base_image_id": base_image_id,
                "builder_image_input_sha256": image_input,
                "container_id": container_id,
                "container_name": container_name,
                "image_id": image_id,
                "owner": OWNER,
                "schema": 1,
                "selection_cache_sha256": args.selection_cache_sha256,
            },
        )
        with build_payload(args) as entries:
            container_payload.exchange_to_file(
                ["podman", "start", "--attach", "--interactive", container_id],
                entries,
                output,
                temporary_path=Path(args.output_partial),
                max_output_bytes=MAX_DEB_TAR_BYTES,
            )
        output_published = True
        return validate_package_tar(output, args)
    except BaseException:
        if output_published:
            background_job.ensure_private_regular(output)
            output.unlink()
        raise
    finally:
        remove_owned_container(args, tolerate_invalid_record=True)


def run_directory(name: str) -> Path:
    return RUN_ROOT / validate_name(name)


def owner_path(name: str) -> Path:
    return run_directory(name) / "owner.json"


def status_path(name: str) -> Path:
    return run_directory(name) / "status.json"


def result_path(name: str) -> Path:
    return RESULT_ROOT / f"{validate_name(name)}.status.json"


def result_log_path(name: str) -> Path:
    return RESULT_ROOT / f"{validate_name(name)}.log"


def package_prelaunch_path(name: str) -> Path:
    return RUN_ROOT / f"{validate_name(name)}.prelaunch.json"


def abort_transaction_path(name: str) -> Path:
    return RUN_ROOT / f"{validate_name(name)}.abort.json"


def remove_transaction_path(name: str) -> Path:
    return RESULT_ROOT / f"{validate_name(name)}.remove.json"


def terminal_lock_path() -> Path:
    return LOCK_ROOT / "terminal.lock"


@contextmanager
def package_terminal_lock() -> Iterator[None]:
    """Serialize destructive/finalizing operations; process death releases flock."""
    path = terminal_lock_path()
    background_job.ensure_private_directory(path.parent, create=True)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != 0o600
            or details.st_nlink != 1
        ):
            raise JobError(f"unsafe DEB terminal-operation lock: {path}")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise JobError("another DEB terminal operation is active") from error
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def terminal_operation(
    handler: Callable[[argparse.Namespace], int],
) -> Callable[[argparse.Namespace], int]:
    """Apply the crash-releasing terminal-operation lock to a CLI handler."""

    @wraps(handler)
    def locked(args: argparse.Namespace) -> int:
        prepare_state()
        with package_terminal_lock():
            return handler(args)

    return locked


def local_build_paths(name: str, distro: str) -> dict[str, Path | str]:
    directory = run_directory(name)
    output = OUTPUT_ROOT / f"{name}-{distro}-debs.tar"
    return {
        "container_name": f"xpra-deb-{name}",
        "container_state": directory / "container.json",
        "output": output,
        "output_partial": output.with_name(f".{output.name}.partial"),
    }


def validate_local_arguments(name: str, arguments: object) -> argparse.Namespace:
    if not isinstance(arguments, dict):
        raise JobError("package job has invalid owned arguments")
    args = argparse.Namespace(**arguments)
    validate_build_arguments(args)
    expected = local_build_paths(name, args.distro)
    for key, value in expected.items():
        if str(getattr(args, key)) != str(value):
            raise JobError(f"package job ownership path mismatch for {key}")
    return args


def validate_local_record(name: str, record: dict[str, Any]) -> argparse.Namespace:
    args = validate_local_arguments(name, record.get("arguments"))
    process = record.get("process")
    if not isinstance(process, dict):
        raise JobError("package job has no owned process record")
    directory = run_directory(name)
    if process.get("runtime_log") != str(directory / "runtime.log"):
        raise JobError("package job runtime log is outside its RUN")
    if process.get("completion") != str(directory / "completion.json"):
        raise JobError("package job completion is outside its RUN")
    return args


def load_package_prelaunch(name: str) -> dict[str, Any]:
    record = background_job.load_json(package_prelaunch_path(name))
    if set(record) != {
        "arguments",
        "kind",
        "name",
        "owner",
        "runner_sha256",
        "schema",
    }:
        raise JobError("package prelaunch ownership fields are inconsistent")
    expected = {
        "kind": "deb-build-prelaunch",
        "name": name,
        "owner": OWNER,
        "schema": 1,
    }
    if any(record.get(key) != value for key, value in expected.items()):
        raise JobError("package prelaunch ownership identity is inconsistent")
    if not SHA256_RE.fullmatch(str(record.get("runner_sha256", ""))):
        raise JobError("package prelaunch has an invalid runner digest")
    validate_local_arguments(name, record.get("arguments"))
    return record


def matching_package_prelaunch(record: dict[str, Any]) -> dict[str, Any]:
    name = str(record.get("name", ""))
    prelaunch = load_package_prelaunch(name)
    if (
        prelaunch.get("arguments") != record.get("arguments")
        or prelaunch.get("runner_sha256") != record.get("runner_sha256")
    ):
        raise JobError("package owner does not match its prelaunch ownership")
    return prelaunch


def publish_abort_transaction(
    name: str,
    prelaunch: dict[str, Any],
    record: dict[str, Any] | None,
) -> dict[str, Any]:
    """Publish retry authority before discarding any uncollected package state."""
    prelaunch_path = package_prelaunch_path(name)
    background_job.ensure_private_regular(prelaunch_path)
    if load_package_prelaunch(name) != prelaunch:
        raise JobError("package prelaunch changed before abort")
    directory = run_directory(name)
    run_device: int | None = None
    run_inode: int | None = None
    if directory.exists() or directory.is_symlink():
        if directory.is_symlink():
            raise JobError(f"refusing symlinked package runtime: {directory}")
        background_job.ensure_private_directory(directory)
        details = directory.lstat()
        run_device = details.st_dev
        run_inode = details.st_ino
    owner_digest: str | None = None
    if record is not None:
        owner = owner_path(name)
        background_job.ensure_private_regular(owner)
        if matching_package_prelaunch(record) != prelaunch:
            raise JobError("package abort owner and prelaunch differ")
        owner_digest = sha256_file(owner)
    transaction = {
        "kind": "deb-build-abort",
        "mode": "owned" if record is not None else "prelaunch",
        "name": name,
        "owner": OWNER,
        "owner_record": record,
        "owner_sha256": owner_digest,
        "prelaunch_record": prelaunch,
        "prelaunch_sha256": sha256_file(prelaunch_path),
        "run_device": run_device,
        "run_directory": str(directory),
        "run_inode": run_inode,
        "schema": 1,
    }
    background_job.publish_json(abort_transaction_path(name), transaction)
    return transaction


def load_abort_transaction(name: str) -> dict[str, Any]:
    transaction = background_job.load_json(abort_transaction_path(name))
    if set(transaction) != {
        "kind",
        "mode",
        "name",
        "owner",
        "owner_record",
        "owner_sha256",
        "prelaunch_record",
        "prelaunch_sha256",
        "run_device",
        "run_directory",
        "run_inode",
        "schema",
    } or any(
        transaction.get(key) != value
        for key, value in {
            "kind": "deb-build-abort",
            "name": name,
            "owner": OWNER,
            "run_directory": str(run_directory(name)),
            "schema": 1,
        }.items()
    ):
        raise JobError("package abort transaction fields are inconsistent")
    mode = transaction.get("mode")
    prelaunch = transaction.get("prelaunch_record")
    record = transaction.get("owner_record")
    if mode not in {"owned", "prelaunch"} or not isinstance(prelaunch, dict):
        raise JobError("package abort transaction identity is inconsistent")
    validate_local_arguments(name, prelaunch.get("arguments"))
    if any(
        prelaunch.get(key) != value
        for key, value in {
            "kind": "deb-build-prelaunch",
            "name": name,
            "owner": OWNER,
            "schema": 1,
        }.items()
    ) or not SHA256_RE.fullmatch(str(prelaunch.get("runner_sha256", ""))):
        raise JobError("package abort transaction has an invalid prelaunch record")
    if not SHA256_RE.fullmatch(str(transaction.get("prelaunch_sha256", ""))):
        raise JobError("package abort transaction has an invalid prelaunch digest")
    if mode == "owned":
        if not isinstance(record, dict):
            raise JobError("owned package abort has no owner record")
        validate_local_record(name, record)
        if (
            record.get("schema") != 2
            or record.get("owner") != OWNER
            or record.get("kind") != "deb-build"
            or record.get("name") != name
            or record.get("arguments") != prelaunch.get("arguments")
            or record.get("runner_sha256") != prelaunch.get("runner_sha256")
            or not SHA256_RE.fullmatch(str(transaction.get("owner_sha256", "")))
        ):
            raise JobError("package abort transaction has an invalid owner record")
    elif record is not None or transaction.get("owner_sha256") is not None:
        raise JobError("prelaunch package abort unexpectedly binds a main owner")
    directory = run_directory(name)
    run_device = transaction.get("run_device")
    run_inode = transaction.get("run_inode")
    if (run_device is None) != (run_inode is None) or any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in (run_device, run_inode)
        if value is not None
    ):
        raise JobError("package abort transaction has an invalid runtime identity")
    if directory.exists() or directory.is_symlink():
        if directory.is_symlink():
            raise JobError(f"refusing symlinked package runtime: {directory}")
        background_job.ensure_private_directory(directory)
        details = directory.lstat()
        if details.st_dev != run_device or details.st_ino != run_inode:
            raise JobError("package runtime identity changed during abort")
    prelaunch_path = package_prelaunch_path(name)
    if prelaunch_path.exists() or prelaunch_path.is_symlink():
        background_job.ensure_private_regular(prelaunch_path)
        if sha256_file(prelaunch_path) != transaction["prelaunch_sha256"]:
            raise JobError("package prelaunch changed during abort")
        if background_job.load_json(prelaunch_path) != prelaunch:
            raise JobError("package prelaunch content changed during abort")
    owner = owner_path(name)
    if owner.exists() or owner.is_symlink():
        if mode != "owned":
            raise JobError("package main owner appeared during prelaunch abort")
        background_job.ensure_private_regular(owner)
        if sha256_file(owner) != transaction["owner_sha256"]:
            raise JobError("package main owner changed during abort")
        if background_job.load_json(owner) != record:
            raise JobError("package main owner content changed during abort")
    return transaction


def load_record(name: str, *, require_current: bool = True) -> dict[str, Any]:
    record = background_job.load_json(owner_path(name))
    expected = {"schema": 2, "owner": OWNER, "kind": "deb-build", "name": name}
    if any(record.get(key) != value for key, value in expected.items()):
        raise JobError("package job ownership record does not match")
    validate_local_record(name, record)
    matching_package_prelaunch(record)
    if require_current and record.get("runner_sha256") != runner_sha256():
        raise JobError("package runner changed while the job was owned")
    background_job.process_state(record, require_current=require_current)
    return record


def record_args(record: dict[str, Any]) -> argparse.Namespace:
    return argparse.Namespace(**record["arguments"])


@terminal_operation
def package_start(args: argparse.Namespace) -> int:
    prepare_state()
    if args.selection != ACTIVE_SELECTION:
        raise JobError(f"DEB builds require the complete {ACTIVE_SELECTION} queue")
    require_amd64_host()
    name = validate_name(args.name)
    directory = run_directory(name)
    args.build_id = str(uuid.uuid4())
    expected_paths = local_build_paths(name, args.distro)
    if str(args.output) != str(expected_paths["output"]):
        raise JobError("package job output does not match its RUN")
    if args.container_name != expected_paths["container_name"]:
        raise JobError("package container name does not match its RUN")
    args.container_state = expected_paths["container_state"]
    args.output_partial = expected_paths["output_partial"]
    output = Path(args.output)
    finalized = result_path(name)
    if (
        directory.exists()
        or directory.is_symlink()
        or package_prelaunch_path(name).exists()
        or package_prelaunch_path(name).is_symlink()
        or abort_transaction_path(name).exists()
        or abort_transaction_path(name).is_symlink()
        or output.exists()
        or output.is_symlink()
        or finalized.exists()
        or finalized.is_symlink()
        or result_log_path(name).exists()
        or result_log_path(name).is_symlink()
        or remove_transaction_path(name).exists()
        or remove_transaction_path(name).is_symlink()
        or Path(args.output_partial).exists()
        or Path(args.output_partial).is_symlink()
    ):
        raise JobError(f"package job artifacts already exist: {name}")
    selection_cache = freeze_selection_cache(args.selection)
    args.selection_cache_sha256 = selection_cache["selection_cache_sha256"]
    args.selection_sha256 = selection_cache["selection_sha256"]
    args.selection_snapshot = Path(selection_cache["selection_snapshot"])
    args.selection_state = Path(selection_cache["selection_state"])
    hydrate_source_arguments(args)
    validate_build_arguments(args)
    arguments = {
        key: str(getattr(args, key))
        for key in (
            "container_name",
            "build_id",
            "checkout_commit",
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
        )
    }
    record = {
        "schema": 2,
        "owner": OWNER,
        "kind": "deb-build",
        "name": name,
        "arguments": arguments,
        "runner_sha256": runner_sha256(),
    }
    prelaunch = {
        "arguments": arguments,
        "kind": "deb-build-prelaunch",
        "name": name,
        "owner": OWNER,
        "runner_sha256": record["runner_sha256"],
        "schema": 1,
    }
    argv = [sys.executable, str(RUNNER_ROOT / "job.py"), "worker"]
    for key, value in arguments.items():
        argv.extend((f"--{key.replace('_', '-')}", value))
    owner_published = False
    background_job.publish_json(package_prelaunch_path(name), prelaunch)
    try:
        directory.mkdir(mode=0o700)
        owned = background_job.launch(
            owner_path=owner_path(name),
            runtime_log=directory / "runtime.log",
            completion_file=directory / "completion.json",
            record=record,
            argv=argv,
            cwd=PROJECT_ROOT,
        )
        owner_published = True
    except BaseException as error:
        retained = isinstance(error, background_job.LaunchStateRetained)
        if (
            not retained
            and not owner_published
            and not owner_path(name).exists()
            and not owner_path(name).is_symlink()
        ):
            if directory.exists() or directory.is_symlink():
                if directory.is_symlink():
                    raise JobError(f"refusing symlinked DEB prelaunch directory: {directory}")
                background_job.ensure_private_directory(directory)
                shutil.rmtree(directory)
            package_prelaunch_path(name).unlink(missing_ok=True)
        raise
    print(f"started DEB build {name} (pid {owned['process']['pid']})")
    return 0


def package_worker(args: argparse.Namespace) -> int:
    manifest = build_distribution(args)
    print(json.dumps(manifest, sort_keys=True))
    return 0


def package_status(args: argparse.Namespace) -> int:
    prepare_state()
    owner = owner_path(args.name)
    aborting = abort_transaction_path(args.name)
    if aborting.exists() or aborting.is_symlink():
        transaction = load_abort_transaction(args.name)
        print(
            json.dumps(
                {
                    "mode": transaction["mode"],
                    "name": args.name,
                    "phase": "aborting",
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if not owner.exists() and not owner.is_symlink():
        transaction = remove_transaction_path(args.name)
        if transaction.exists() or transaction.is_symlink():
            removed = load_remove_transaction(args.name)
            print(
                json.dumps(
                    {
                        "name": args.name,
                        "phase": "removed",
                        "validation_ok": removed["validation_ok"],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        prelaunch = load_package_prelaunch(args.name)
        print(
            json.dumps(
                {
                    "name": args.name,
                    "phase": "prelaunch",
                    "runner_sha256": prelaunch["runner_sha256"],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    record = load_record(args.name, require_current=False)
    state = background_job.process_state(record, require_current=False)
    print(json.dumps({"name": args.name, "process": state}, indent=2, sort_keys=True))
    return 0


def package_logs(args: argparse.Namespace) -> int:
    prepare_state()
    owner = owner_path(args.name)
    if not owner.exists() and not owner.is_symlink():
        aborting = abort_transaction_path(args.name)
        if aborting.exists() or aborting.is_symlink():
            load_abort_transaction(args.name)
            path = run_directory(args.name) / "runtime.log"
            if not path.exists() and not path.is_symlink():
                raise JobError(f"package abort has no retained runtime log: {args.name}")
            background_job.ensure_private_regular(path)
            sys.stdout.buffer.write(path.read_bytes())
            return 0
        transaction = remove_transaction_path(args.name)
        if transaction.exists() or transaction.is_symlink():
            load_remove_transaction(args.name)
            path = result_log_path(args.name)
            background_job.ensure_private_regular(path)
            sys.stdout.buffer.write(path.read_bytes())
            return 0
        load_package_prelaunch(args.name)
        path = run_directory(args.name) / "runtime.log"
        if not path.exists() and not path.is_symlink():
            raise JobError(f"package prelaunch has no runtime log yet: {args.name}")
        background_job.ensure_private_regular(path)
        sys.stdout.buffer.write(path.read_bytes())
        return 0
    record = load_record(args.name, require_current=False)
    path = background_job.runtime_log_path(record, require_current=False)
    background_job.ensure_private_regular(path)
    sys.stdout.buffer.write(path.read_bytes())
    return 0


@terminal_operation
def package_collect(args: argparse.Namespace) -> int:
    prepare_state()
    if abort_transaction_path(args.name).exists() or abort_transaction_path(args.name).is_symlink():
        raise JobError(f"package abort is incomplete; retry abort: {args.name}")
    if remove_transaction_path(args.name).exists() or remove_transaction_path(args.name).is_symlink():
        raise JobError(f"package build was already removed: {args.name}")
    record = load_record(args.name)
    state = background_job.process_state(record)
    if state["state"] == "running":
        raise JobError(f"package build is still running: {args.name}")
    if state["state"] != "completed":
        raise JobError(f"package build disappeared: {args.name}")
    if status_path(args.name).exists() or status_path(args.name).is_symlink():
        raise JobError(f"package result was already collected: {args.name}")
    build_args = record_args(record)
    validate_build_arguments(build_args)
    runtime_log = background_job.runtime_log_path(record)
    background_job.ensure_private_regular(runtime_log)
    log_sha256 = sha256_file(runtime_log)
    container: dict[str, str] | None = None
    validation_error = ""
    try:
        container = load_container_record(build_args)
    except (JobError, background_job.BackgroundJobError, OSError) as error:
        validation_error = str(error)
    if container:
        build_args.base_image_id = container["base_image_id"]
        build_args.builder_image_id = container["image_id"]
        build_args.builder_image_input_sha256 = container[
            "builder_image_input_sha256"
        ]
    valid = state["exit_code"] == 0 and container is not None
    manifest: dict[str, Any] = {}
    if container is None and not validation_error:
        validation_error = "worker published no container provenance"
    if valid:
        try:
            manifest = validate_package_tar(Path(build_args.output), build_args)
        except (
            JobError,
            UnicodeError,
            background_job.BackgroundJobError,
            OSError,
            container_payload.PayloadError,
            json.JSONDecodeError,
            tarfile.TarError,
        ) as error:
            valid = False
            validation_error = str(error)
    elif (
        not validation_error
        and (Path(build_args.output).exists() or Path(build_args.output).is_symlink())
    ):
        validation_error = "worker failed after publishing an untrusted output"
    payload = {
        "arguments": record["arguments"],
        "container": container or {},
        "exit_code": state["exit_code"],
        "finished_at": state.get("finished_at", ""),
        "log_sha256": log_sha256,
        "manifest": manifest,
        "name": args.name,
        "output": build_args.output,
        "output_sha256": sha256_file(Path(build_args.output)) if valid else "",
        "owner": OWNER,
        "process_pid": state["pid"],
        "runner_sha256": record["runner_sha256"],
        "schema": 2,
        "validation_error": validation_error,
        "validation_ok": valid,
    }
    background_job.publish_json(status_path(args.name), payload)
    print(f"collected DEB build {args.name}: validation_ok={int(valid)}")
    return 0 if valid else 1


def package_wait(args: argparse.Namespace) -> int:
    prepare_state()
    record = load_record(args.name)
    background_job.wait_process(record)
    return package_collect(args)


def json_payload(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()


def validate_collected_status(
    name: str,
    record: dict[str, Any],
) -> dict[str, Any]:
    status = background_job.load_json(status_path(name))
    if (
        status.get("schema") != 2
        or status.get("owner") != OWNER
        or status.get("name") != name
        or not isinstance(status.get("validation_ok"), bool)
    ):
        raise JobError("package result is not an owned collected build")
    build_args = validate_local_arguments(name, record.get("arguments"))
    if (
        status.get("output") != str(build_args.output)
        or status.get("arguments") != record.get("arguments")
    ):
        raise JobError("package result paths do not match its owner record")
    return status


def publish_remove_transaction(
    name: str,
    record: dict[str, Any],
    status: dict[str, Any],
) -> dict[str, Any]:
    """Publish immutable authority before changing any collected runtime state."""
    directory = run_directory(name)
    background_job.ensure_private_directory(directory)
    details = directory.lstat()
    owner = owner_path(name)
    prelaunch = package_prelaunch_path(name)
    collected = status_path(name)
    runtime_log = background_job.runtime_log_path(record, require_current=False)
    for path in (owner, prelaunch, collected, runtime_log):
        background_job.ensure_private_regular(path)
    if validate_collected_status(name, record) != status:
        raise JobError("package collected status changed before removal")
    transaction = {
        "final_log": str(result_log_path(name)),
        "final_status": str(result_path(name)),
        "kind": "deb-build-remove",
        "log_sha256": sha256_file(runtime_log),
        "name": name,
        "owner": OWNER,
        "owner_record": record,
        "owner_sha256": sha256_file(owner),
        "output": str(status["output"]),
        "output_sha256": str(status.get("output_sha256", "")),
        "prelaunch_sha256": sha256_file(prelaunch),
        "run_device": details.st_dev,
        "run_directory": str(directory),
        "run_inode": details.st_ino,
        "schema": 1,
        "status": status,
        "status_sha256": hashlib.sha256(json_payload(status)).hexdigest(),
        "validation_ok": status["validation_ok"],
    }
    if transaction["status_sha256"] != sha256_file(collected):
        raise JobError("package collected status is not canonically encoded")
    if transaction["log_sha256"] != status.get("log_sha256"):
        raise JobError("package runtime log changed before removal")
    background_job.publish_json(remove_transaction_path(name), transaction)
    return transaction


def load_remove_transaction(name: str) -> dict[str, Any]:
    transaction = background_job.load_json(remove_transaction_path(name))
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
    if set(transaction) != expected_keys or any(
        transaction.get(key) != value
        for key, value in {
            "final_log": str(result_log_path(name)),
            "final_status": str(result_path(name)),
            "kind": "deb-build-remove",
            "name": name,
            "owner": OWNER,
            "run_directory": str(run_directory(name)),
            "schema": 1,
        }.items()
    ):
        raise JobError("package removal transaction fields are inconsistent")
    record = transaction.get("owner_record")
    status = transaction.get("status")
    if not isinstance(record, dict) or not isinstance(status, dict):
        raise JobError("package removal transaction has invalid embedded records")
    if any(
        record.get(key) != value
        for key, value in {
            "kind": "deb-build",
            "name": name,
            "owner": OWNER,
            "schema": 2,
        }.items()
    ):
        raise JobError("package removal transaction has an invalid owner record")
    build_args = validate_local_arguments(name, record.get("arguments"))
    if (
        status.get("schema") != 2
        or status.get("owner") != OWNER
        or status.get("name") != name
        or status.get("arguments") != record.get("arguments")
        or status.get("output") != str(build_args.output)
        or status.get("validation_ok") is not transaction.get("validation_ok")
        or transaction.get("output") != str(build_args.output)
        or transaction.get("output_sha256") != str(status.get("output_sha256", ""))
        or status.get("log_sha256") != transaction.get("log_sha256")
        or not isinstance(transaction.get("validation_ok"), bool)
    ):
        raise JobError("package removal transaction has an invalid collected status")
    for key in (
        "log_sha256",
        "owner_sha256",
        "prelaunch_sha256",
        "status_sha256",
    ):
        if not SHA256_RE.fullmatch(str(transaction.get(key, ""))):
            raise JobError(f"package removal transaction has invalid {key}")
    if transaction["status_sha256"] != hashlib.sha256(json_payload(status)).hexdigest():
        raise JobError("package removal transaction status digest is inconsistent")
    output_sha256 = str(transaction["output_sha256"])
    if (
        transaction["validation_ok"]
        and not SHA256_RE.fullmatch(output_sha256)
    ) or (not transaction["validation_ok"] and output_sha256):
        raise JobError("package removal transaction has invalid output provenance")
    directory = run_directory(name)
    if directory.exists() or directory.is_symlink():
        if directory.is_symlink():
            raise JobError(f"refusing symlinked package runtime: {directory}")
        background_job.ensure_private_directory(directory)
        details = directory.lstat()
        if (
            stat.S_IMODE(details.st_mode) != 0o700
            or
            details.st_dev != transaction.get("run_device")
            or details.st_ino != transaction.get("run_inode")
        ):
            raise JobError("package runtime identity changed during removal")
        for path, key in (
            (owner_path(name), "owner_sha256"),
            (status_path(name), "status_sha256"),
            (directory / "runtime.log", "log_sha256"),
        ):
            if path.exists() or path.is_symlink():
                background_job.ensure_private_regular(path)
                if sha256_file(path) != transaction[key]:
                    raise JobError(f"package runtime changed during removal: {path}")
    prelaunch = package_prelaunch_path(name)
    if prelaunch.exists() or prelaunch.is_symlink():
        background_job.ensure_private_regular(prelaunch)
        if sha256_file(prelaunch) != transaction["prelaunch_sha256"]:
            raise JobError("package prelaunch ownership changed during removal")
    for path, key in (
        (result_path(name), "status_sha256"),
        (result_log_path(name), "log_sha256"),
    ):
        if path.exists() or path.is_symlink():
            background_job.ensure_private_regular(path)
            if sha256_file(path) != transaction[key]:
                raise JobError(f"finalized package evidence changed: {path}")
    if not directory.exists() and not directory.is_symlink() and not all(
        path.exists() and not path.is_symlink()
        for path in (result_path(name), result_log_path(name))
    ):
        raise JobError("removed package runtime has incomplete final evidence")
    return transaction


def publish_or_validate_final(path: Path, payload: bytes, digest: str) -> None:
    if hashlib.sha256(payload).hexdigest() != digest:
        raise JobError(f"finalized package payload has the wrong digest: {path}")
    if path.exists() or path.is_symlink():
        background_job.ensure_private_regular(path)
        if sha256_file(path) != digest:
            raise JobError(f"existing finalized package evidence does not match: {path}")
        return
    publish_bytes(path, payload)


def finish_package_remove(name: str, transaction: dict[str, Any]) -> None:
    record = transaction["owner_record"]
    status = transaction["status"]
    build_args = validate_local_arguments(name, record["arguments"])
    output = Path(build_args.output)
    remove_owned_container(
        build_args,
        tolerate_invalid_record=not transaction["validation_ok"],
    )
    validation_scratch = validation_paths(output)
    if any(path.exists() or path.is_symlink() for path in validation_scratch):
        recover_validation_scratch(output)
    partial = Path(build_args.output_partial)
    if partial.exists() or partial.is_symlink():
        background_job.ensure_private_regular(partial)
        partial.unlink()
    if transaction["validation_ok"]:
        digest = str(transaction["output_sha256"])
        background_job.ensure_private_regular(output)
        if not SHA256_RE.fullmatch(digest) or sha256_file(output) != digest:
            raise JobError("package output changed after collection")
    elif output.exists() or output.is_symlink():
        background_job.ensure_private_regular(output)
        output.unlink()
    directory = run_directory(name)
    runtime_log = directory / "runtime.log"
    collected = status_path(name)
    if result_log_path(name).exists() or result_log_path(name).is_symlink():
        background_job.ensure_private_regular(result_log_path(name))
        log_payload = result_log_path(name).read_bytes()
    else:
        background_job.ensure_private_regular(runtime_log)
        log_payload = runtime_log.read_bytes()
    publish_or_validate_final(
        result_log_path(name),
        log_payload,
        str(transaction["log_sha256"]),
    )
    if result_path(name).exists() or result_path(name).is_symlink():
        background_job.ensure_private_regular(result_path(name))
        status_payload = result_path(name).read_bytes()
    elif collected.exists() or collected.is_symlink():
        background_job.ensure_private_regular(collected)
        status_payload = collected.read_bytes()
    else:
        status_payload = json_payload(status)
    publish_or_validate_final(
        result_path(name),
        status_payload,
        str(transaction["status_sha256"]),
    )
    if directory.exists() or directory.is_symlink():
        if directory.is_symlink():
            raise JobError(f"refusing symlinked package runtime: {directory}")
        details = directory.lstat()
        if (
            details.st_dev != transaction["run_device"]
            or details.st_ino != transaction["run_inode"]
        ):
            raise JobError("package runtime identity changed during removal")
        shutil.rmtree(directory)
    prelaunch = package_prelaunch_path(name)
    if prelaunch.exists() or prelaunch.is_symlink():
        background_job.ensure_private_regular(prelaunch)
        if sha256_file(prelaunch) != transaction["prelaunch_sha256"]:
            raise JobError("package prelaunch ownership changed during removal")
        prelaunch.unlink()


@terminal_operation
def package_remove(args: argparse.Namespace) -> int:
    prepare_state()
    if abort_transaction_path(args.name).exists() or abort_transaction_path(args.name).is_symlink():
        raise JobError("package abort is incomplete; retry abort")
    transaction_path = remove_transaction_path(args.name)
    if transaction_path.exists() or transaction_path.is_symlink():
        transaction = load_remove_transaction(args.name)
    else:
        record = load_record(args.name, require_current=False)
        status = validate_collected_status(args.name, record)
        state = background_job.process_state(record, require_current=False)
        if state["state"] == "running":
            raise JobError("package job is still running")
        if state["state"] != "completed":
            raise JobError("package job has no completed process record")
        transaction = publish_remove_transaction(args.name, record, status)
    finish_package_remove(args.name, transaction)
    retained = (
        "finalized output was retained"
        if transaction["validation_ok"]
        else "no output was retained"
    )
    print(f"removed package runtime state for {args.name}; {retained}")
    return 0


@terminal_operation
def package_abort(args: argparse.Namespace) -> int:
    prepare_state()
    if remove_transaction_path(args.name).exists() or remove_transaction_path(args.name).is_symlink():
        raise JobError("package removal is already finalized; retry remove")
    aborting = abort_transaction_path(args.name)
    if aborting.exists() or aborting.is_symlink():
        transaction = load_abort_transaction(args.name)
        finish_package_abort(args.name, transaction)
        print(f"finished interrupted package abort for {args.name}")
        return 0
    owner = owner_path(args.name)
    if not owner.exists() and not owner.is_symlink():
        prelaunch = load_package_prelaunch(args.name)
        build_args = validate_local_arguments(args.name, prelaunch["arguments"])
        directory = run_directory(args.name)
        forbidden = (
            status_path(args.name),
            Path(build_args.container_state),
            Path(build_args.output),
            Path(build_args.output_partial),
            directory / "completion.json",
        )
        if any(path.exists() or path.is_symlink() for path in forbidden):
            raise JobError("ownerless package prelaunch contains executed worker state")
        if directory.exists() or directory.is_symlink():
            if directory.is_symlink():
                raise JobError(f"refusing symlinked package prelaunch: {directory}")
            background_job.ensure_private_directory(directory)
            entries = tuple(directory.iterdir())
            if any(path.name != "runtime.log" for path in entries):
                raise JobError("ownerless package prelaunch has unexpected runtime files")
            for path in entries:
                background_job.ensure_private_regular(path)
        transaction = publish_abort_transaction(args.name, prelaunch, None)
        finish_package_abort(args.name, transaction)
        print(f"discarded recoverable package prelaunch state for {args.name}")
        return 0
    record = load_record(args.name, require_current=False)
    if status_path(args.name).exists() or status_path(args.name).is_symlink():
        raise JobError("collected package jobs cannot be aborted")
    build_args = record_args(record)
    validate_build_arguments(build_args)
    state = background_job.process_state(record, require_current=False)
    if state["state"] == "completed":
        if record.get("runner_sha256") == runner_sha256():
            raise JobError("completed package jobs must be collected, not aborted")
    elif state["state"] not in {"running", "lost"}:
        raise JobError(f"package job has an unsupported process state: {state['state']}")
    prelaunch = matching_package_prelaunch(record)
    transaction = publish_abort_transaction(args.name, prelaunch, record)
    finish_package_abort(args.name, transaction)
    print(f"aborted and removed package runtime state for {args.name}")
    return 0


def finish_package_abort(name: str, transaction: dict[str, Any]) -> None:
    """Idempotently finish one exact abort transaction, deleting it last."""
    transaction = load_abort_transaction(name)
    prelaunch = transaction["prelaunch_record"]
    build_args = validate_local_arguments(name, prelaunch["arguments"])
    record = transaction["owner_record"]
    if record is not None:
        state = background_job.process_state(record, require_current=False)
        if state["state"] == "running":
            background_job.terminate(record, require_current=False)
        elif state["state"] not in {"completed", "lost"}:
            raise JobError(
                f"package job has an unsupported abort state: {state['state']}"
            )
    if record is not None:
        remove_owned_container(build_args, tolerate_invalid_record=True)
    output = Path(build_args.output)
    if any(
        path.exists() or path.is_symlink()
        for path in validation_paths(output)
    ):
        recover_validation_scratch(output)
    for path in (Path(build_args.output), Path(build_args.output_partial)):
        if path.exists() or path.is_symlink():
            background_job.ensure_private_regular(path)
            path.unlink()
    directory = run_directory(name)
    if directory.exists() or directory.is_symlink():
        if directory.is_symlink():
            raise JobError(f"refusing symlinked package runtime: {directory}")
        details = directory.lstat()
        if (
            details.st_dev != transaction["run_device"]
            or details.st_ino != transaction["run_inode"]
        ):
            raise JobError("package runtime identity changed during abort")
        shutil.rmtree(directory)
    prelaunch_path = package_prelaunch_path(name)
    if prelaunch_path.exists() or prelaunch_path.is_symlink():
        background_job.ensure_private_regular(prelaunch_path)
        if sha256_file(prelaunch_path) != transaction["prelaunch_sha256"]:
            raise JobError("package prelaunch changed during abort")
        prelaunch_path.unlink()
    abort_transaction_path(name).unlink()


def require_gh_release_cli() -> None:
    result = command(["gh", "version"])
    match = re.search(r"(?m)^gh version ([0-9]+)\.([0-9]+)\.([0-9]+)\b", result.stdout)
    if match is None or tuple(map(int, match.groups())) < (2, 97, 0):
        raise JobError("GitHub CLI 2.97.0 or newer is required for DEB publication")


def gh_json(arguments: list[str]) -> dict[str, Any]:
    try:
        payload = json.loads(command(["gh", *arguments]).stdout)
    except json.JSONDecodeError as error:
        raise JobError(f"GitHub CLI returned invalid JSON: {arguments!r}") from error
    if not isinstance(payload, dict):
        raise JobError(f"GitHub CLI returned a non-object: {arguments!r}")
    return payload


def gh_json_list(arguments: list[str]) -> list[dict[str, Any]]:
    try:
        payload = json.loads(command(["gh", *arguments]).stdout)
    except json.JSONDecodeError as error:
        raise JobError(f"GitHub CLI returned invalid JSON: {arguments!r}") from error
    if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
        raise JobError(f"GitHub CLI returned a non-object list: {arguments!r}")
    return payload


def require_gh_absent(endpoint: str, label: str) -> None:
    result = command(["gh", "api", endpoint], check=False)
    if result.returncode == 0:
        raise JobError(f"GitHub {label} already exists")
    if "HTTP 404" not in result.stderr:
        raise JobError(f"cannot prove GitHub {label} is absent: {result.stderr.strip()}")


def gh_optional_json(arguments: list[str], label: str) -> dict[str, Any] | None:
    result = command(["gh", *arguments], check=False)
    if result.returncode:
        if "HTTP 404" in result.stderr:
            return None
        raise JobError(f"cannot inspect GitHub {label}: {result.stderr.strip()}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise JobError(f"GitHub {label} inspection returned invalid JSON") from error
    if not isinstance(payload, dict):
        raise JobError(f"GitHub {label} inspection returned a non-object")
    return payload


def authenticated_releases() -> tuple[dict[str, Any], ...]:
    """List every release, including authenticated draft results, with a hard bound."""
    releases: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for page in range(1, MAX_RELEASE_LIST_PAGES + 1):
        batch = gh_json_list(
            [
                "api",
                (
                    f"repos/{RELEASE_REPOSITORY}/releases"
                    f"?per_page={RELEASE_LIST_PAGE_SIZE}&page={page}"
                ),
            ]
        )
        if len(batch) > RELEASE_LIST_PAGE_SIZE:
            raise JobError("GitHub release listing exceeds its requested page size")
        for release in batch:
            release_id = release.get("id")
            tag = release.get("tag_name")
            if (
                not isinstance(release_id, int)
                or isinstance(release_id, bool)
                or release_id <= 0
                or not isinstance(tag, str)
                or not tag
                or not isinstance(release.get("draft"), bool)
            ):
                raise JobError("GitHub release listing contains an invalid identity")
            if release_id in seen_ids:
                raise JobError("GitHub release listing contains a duplicate immutable ID")
            seen_ids.add(release_id)
            releases.append(release)
        if len(batch) < RELEASE_LIST_PAGE_SIZE:
            return tuple(releases)
    raise JobError(
        f"GitHub release listing exceeds {MAX_RELEASE_LIST_PAGES} pages"
    )


def listed_release_by_tag(
    tag: str,
    releases: tuple[dict[str, Any], ...] | None = None,
) -> dict[str, Any] | None:
    if releases is None:
        releases = authenticated_releases()
    matches = tuple(release for release in releases if release.get("tag_name") == tag)
    if len(matches) > 1:
        raise JobError(f"GitHub release tag is ambiguous in authenticated listing: {tag}")
    return matches[0] if matches else None


def release_asset_metadata(assets: list[Path]) -> dict[str, dict[str, Any]]:
    return {
        asset.name: {
            "digest": f"sha256:{sha256_file(asset)}",
            "size": asset.stat().st_size,
        }
        for asset in assets
    }


def validate_remote_release(
    release: dict[str, Any],
    *,
    tag: str,
    title: str,
    notes_body: str,
    github_sha: str,
    asset_metadata: dict[str, dict[str, Any]],
    draft: bool | None,
    exact_assets: bool = True,
) -> int:
    release_id = release.get("id")
    if (
        not isinstance(release_id, int)
        or isinstance(release_id, bool)
        or release_id <= 0
    ):
        raise JobError("GitHub release has an invalid immutable ID")
    if (
        release.get("tag_name") != tag
        or release.get("target_commitish") != github_sha
        or (draft is not None and release.get("draft") is not draft)
        or release.get("prerelease") is not True
        or release.get("name") != title
        or release.get("body") != notes_body
    ):
        raise JobError("GitHub release metadata does not match the publication request")
    observed_assets = release.get("assets")
    if not isinstance(observed_assets, list):
        raise JobError("GitHub release has invalid asset metadata")
    observed: dict[str, dict[str, Any]] = {}
    for asset in observed_assets:
        if not isinstance(asset, dict) or not isinstance(asset.get("name"), str):
            raise JobError("GitHub release contains an invalid asset")
        name = asset["name"]
        if name in observed:
            raise JobError("GitHub release contains duplicate asset names")
        observed[name] = {
            "digest": asset.get("digest"),
            "size": asset.get("size"),
        }
    if exact_assets and observed != asset_metadata:
        raise JobError("GitHub release asset set or digests do not match")
    if not exact_assets and any(
        name not in asset_metadata or asset_metadata[name] != metadata
        for name, metadata in observed.items()
    ):
        raise JobError("GitHub release contains an unowned asset")
    return release_id


def tag_commit(tag: str) -> str | None:
    result = command(
        ["gh", "api", f"repos/kogeler/xpra/git/ref/tags/{tag}"],
        check=False,
    )
    if result.returncode:
        if "HTTP 404" in result.stderr:
            return None
        raise JobError(f"cannot inspect GitHub release tag: {result.stderr.strip()}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise JobError("GitHub release tag inspection returned invalid JSON") from error
    target = payload.get("object") if isinstance(payload, dict) else None
    commit = target.get("sha") if isinstance(target, dict) else None
    if not isinstance(commit, str) or not COMMIT_RE.fullmatch(commit):
        raise JobError("GitHub release tag has an invalid target")
    return commit


def validate_release_inputs(
    *,
    directory: Path,
    tag: str,
    title: str,
    notes: Path,
    github_sha: str,
    assets: list[Path],
) -> tuple[str, dict[str, dict[str, Any]]]:
    background_job.ensure_private_directory(directory)
    if not NAME_RE.fullmatch(tag) or not title.strip() or "\n" in title:
        raise JobError("GitHub release tag or title is invalid")
    if not COMMIT_RE.fullmatch(github_sha):
        raise JobError("GitHub release commit is invalid")
    if notes.parent != directory:
        raise JobError("GitHub release notes are outside their staging directory")
    background_job.ensure_private_regular(notes)
    if len(assets) != len(RELEASE_ASSET_NAMES) or {
        asset.name for asset in assets
    } != RELEASE_ASSET_NAMES:
        raise JobError("GitHub release has an unexpected local asset set")
    for asset in assets:
        if asset.parent != directory:
            raise JobError("GitHub release asset is outside its staging directory")
        background_job.ensure_private_regular(asset)
        if asset.stat().st_size > MAX_DEB_TAR_BYTES:
            raise JobError(f"GitHub release asset exceeds {MAX_DEB_TAR_BYTES} bytes")
    return notes.read_text(encoding="utf-8"), release_asset_metadata(assets)


def release_transaction_record(
    *,
    run_id: str,
    attempt: str,
    github_sha: str,
    version: str,
    assets: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if (
        not run_id.isdigit()
        or not attempt.isdigit()
        or int(run_id) <= 0
        or int(attempt) <= 0
        or not COMMIT_RE.fullmatch(github_sha)
        or not version
        or "\n" in version
        or set(assets) != RELEASE_ASSET_NAMES
    ):
        raise JobError("invalid GitHub release transaction identity")
    for name, metadata in assets.items():
        if (
            Path(name).name != name
            or not isinstance(metadata, dict)
            or not isinstance(metadata.get("size"), int)
            or isinstance(metadata.get("size"), bool)
            or metadata["size"] < 0
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(metadata.get("digest", "")))
        ):
            raise JobError("invalid GitHub release transaction asset metadata")
    return {
        "assets": assets,
        "attempt": int(attempt),
        "commit": github_sha,
        "owner": OWNER,
        "repository": RELEASE_REPOSITORY,
        "run_id": int(run_id),
        "schema": 1,
        "version": version,
        "workflow": RELEASE_WORKFLOW,
    }


def release_transaction_marker(record: dict[str, Any]) -> str:
    return (
        RELEASE_TRANSACTION_PREFIX
        + json.dumps(record, sort_keys=True, separators=(",", ":"))
        + " -->"
    )


def parse_release_transaction(body: object) -> dict[str, Any]:
    if not isinstance(body, str):
        raise JobError("GitHub recovery draft has no transaction body")
    markers = [
        line
        for line in body.splitlines()
        if line.startswith(RELEASE_TRANSACTION_PREFIX) and line.endswith(" -->")
    ]
    if len(markers) != 1:
        raise JobError("GitHub recovery draft has no unique transaction marker")
    raw = markers[0][len(RELEASE_TRANSACTION_PREFIX) : -4]
    try:
        record = json.loads(raw)
    except json.JSONDecodeError as error:
        raise JobError("GitHub recovery draft transaction marker is invalid") from error
    if not isinstance(record, dict) or set(record) != {
        "assets",
        "attempt",
        "commit",
        "owner",
        "repository",
        "run_id",
        "schema",
        "version",
        "workflow",
    }:
        raise JobError("GitHub recovery draft transaction fields are inconsistent")
    assets = record.get("assets")
    if not isinstance(assets, dict):
        raise JobError("GitHub recovery draft transaction assets are invalid")
    validated = release_transaction_record(
        run_id=str(record.get("run_id", "")),
        attempt=str(record.get("attempt", "")),
        github_sha=str(record.get("commit", "")),
        version=str(record.get("version", "")),
        assets=assets,
    )
    if validated != record:
        raise JobError("GitHub recovery draft transaction is noncanonical")
    return record


def release_notes_body(
    *,
    github_sha: str,
    source: str,
    selection: str,
    revision: int,
    transaction: dict[str, Any],
) -> str:
    return "\n".join(
        (
            "Patched Xpra DEB packages for Ubuntu 26.04 and Debian 13.",
            "",
            f"- checkout commit: `{github_sha}`",
            f"- clean source boundary: `{source}`",
            f"- downstream selection: `{selection}`",
            f"- upstream revision: `r{revision}`",
            "",
            (
                "Each asset is a tar containing all generated `.deb` files, "
                "`manifest.json`, and `SHA256SUMS`."
            ),
            "",
            release_transaction_marker(transaction),
            "",
        )
    )


def validate_recovery_workflow_attempt(
    payload: dict[str, Any],
    *,
    run_id: int,
    attempt: int,
    github_sha: str,
) -> None:
    repository = payload.get("repository")
    if (
        payload.get("id") != run_id
        or payload.get("run_attempt") != attempt
        or payload.get("event") != "workflow_dispatch"
        or payload.get("head_sha") != github_sha
        or payload.get("path") != RELEASE_WORKFLOW
        or payload.get("status") != "completed"
        or payload.get("conclusion")
        not in {
            "action_required",
            "cancelled",
            "failure",
            "stale",
            "startup_failure",
            "timed_out",
        }
        or not isinstance(repository, dict)
        or repository.get("full_name") != RELEASE_REPOSITORY
    ):
        raise JobError("prior GitHub release attempt is not an exact failed workflow run")


def reconcile_prior_release_attempts(
    *,
    run_id: str,
    attempt: str,
    github_sha: str,
    version: str,
    source: str,
    selection: str,
    revision: int,
    asset_metadata: dict[str, dict[str, Any]],
) -> None:
    """Recover exact orphan drafts left by killed prior attempts of this run."""
    current_attempt = int(attempt)
    releases = authenticated_releases()
    for prior_attempt in range(1, current_attempt):
        tag = f"kogeler-deb-{version}-run{run_id}-attempt{prior_attempt}"
        release = listed_release_by_tag(tag, releases)
        observed_tag = tag_commit(tag)
        if release is None:
            if observed_tag is not None:
                raise JobError(
                    f"prior release attempt has an ambiguous tag without a release: {tag}"
                )
            continue
        if release.get("draft") is not True:
            raise JobError(
                f"prior GitHub release attempt is already published: {tag}"
            )
        if observed_tag is not None and observed_tag != github_sha:
            raise JobError("prior GitHub draft tag target changed; refusing recovery")
        transaction = parse_release_transaction(release.get("body"))
        if (
            transaction["run_id"] != int(run_id)
            or transaction["attempt"] != prior_attempt
            or transaction["commit"] != github_sha
            or transaction["version"] != version
            or transaction["assets"] != asset_metadata
        ):
            raise JobError("prior GitHub draft belongs to a different transaction")
        action_run = gh_json(
            [
                "api",
                (
                    f"repos/{RELEASE_REPOSITORY}/actions/runs/{run_id}/"
                    f"attempts/{prior_attempt}"
                ),
            ]
        )
        validate_recovery_workflow_attempt(
            action_run,
            run_id=int(run_id),
            attempt=prior_attempt,
            github_sha=github_sha,
        )
        notes_body = release_notes_body(
            github_sha=github_sha,
            source=source,
            selection=selection,
            revision=revision,
            transaction=transaction,
        )
        title = f"Kogeler Xpra DEB {version}"
        release_id = validate_remote_release(
            release,
            tag=tag,
            title=title,
            notes_body=notes_body,
            github_sha=github_sha,
            asset_metadata=transaction["assets"],
            draft=True,
            exact_assets=False,
        )
        errors, rollback_id = rollback_release(
            tag=tag,
            title=title,
            notes_body=notes_body,
            github_sha=github_sha,
            asset_metadata=transaction["assets"],
            release_id=release_id,
            create_attempted=True,
            publish_attempted=False,
        )
        if rollback_id != release_id:
            raise JobError("prior GitHub draft immutable ID changed during recovery")
        if errors:
            raise JobError(
                f"prior GitHub release-attempt recovery failed: {'; '.join(errors)}"
            )


@contextmanager
def publication_signal_guard() -> Iterator[None]:
    """Turn hosted cancellation signals into the publication rollback path."""
    watched = (signal.SIGINT, signal.SIGTERM)
    previous = {number: signal.getsignal(number) for number in watched}

    def interrupted(number: int, _frame: object) -> None:
        # Do not let the runner's second grace-period signal interrupt rollback.
        for watched_number in watched:
            signal.signal(watched_number, signal.SIG_IGN)
        raise JobError(f"GitHub release publication interrupted by signal {number}")

    try:
        for number in watched:
            signal.signal(number, interrupted)
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def publication_interruptible(
    handler: Callable[..., None],
) -> Callable[..., None]:
    @wraps(handler)
    def guarded(*args: object, **kwargs: object) -> None:
        with publication_signal_guard():
            handler(*args, **kwargs)

    return guarded


def rollback_release(
    *,
    tag: str,
    title: str,
    notes_body: str,
    github_sha: str,
    asset_metadata: dict[str, dict[str, Any]],
    release_id: int | None,
    create_attempted: bool,
    publish_attempted: bool,
) -> tuple[list[str], int | None]:
    """Remove an exact tag first, retaining its release as retry authority."""
    errors: list[str] = []
    owned_release: dict[str, Any] | None = None
    try:
        if release_id is not None:
            owned_release = gh_optional_json(
                ["api", f"repos/kogeler/xpra/releases/{release_id}"],
                "release rollback candidate",
            )
        elif create_attempted:
            owned_release = listed_release_by_tag(tag)
            if owned_release is None:
                raise JobError(
                    "cannot prove an ambiguously created GitHub draft is absent"
                )
        if owned_release is not None:
            validated_id = validate_remote_release(
                owned_release,
                tag=tag,
                title=title,
                notes_body=notes_body,
                github_sha=github_sha,
                asset_metadata=asset_metadata,
                draft=None if publish_attempted else True,
                exact_assets=False,
            )
            if release_id is not None and validated_id != release_id:
                raise JobError("GitHub release immutable ID changed during rollback")
            release_id = validated_id
    except (JobError, OSError) as error:
        errors.append(str(error))
    if errors:
        return errors, release_id

    try:
        observed_tag = tag_commit(tag)
    except (JobError, OSError) as error:
        errors.append(str(error))
        return errors, release_id

    if owned_release is None:
        if observed_tag is not None:
            errors.append(
                "release tag exists without its exact release; refusing cleanup"
            )
        return errors, release_id

    if observed_tag is not None and observed_tag != github_sha:
        errors.append("release tag target changed; refusing cleanup")
        return errors, release_id
    if observed_tag == github_sha:
        try:
            result = command(
                [
                    "gh",
                    "api",
                    "--method",
                    "DELETE",
                    f"repos/kogeler/xpra/git/refs/tags/{tag}",
                ],
                check=False,
            )
        except OSError as error:
            errors.append(f"release tag cleanup failed: {error}")
        else:
            if result.returncode:
                errors.append("release tag cleanup failed")
        if errors:
            return errors, release_id
        try:
            remaining_tag = tag_commit(tag)
        except (JobError, OSError) as error:
            errors.append(str(error))
        else:
            if remaining_tag is not None:
                errors.append("GitHub release tag still exists after cleanup")
        if errors:
            return errors, release_id

    assert release_id is not None
    try:
        result = command(
            [
                "gh",
                "api",
                "--method",
                "DELETE",
                f"repos/kogeler/xpra/releases/{release_id}",
            ],
            check=False,
            capture=False,
        )
    except OSError as error:
        errors.append(f"release cleanup failed: {error}")
    else:
        if result.returncode:
            errors.append("release cleanup failed")
    if errors:
        return errors, release_id
    try:
        remaining_release = gh_optional_json(
            ["api", f"repos/kogeler/xpra/releases/{release_id}"],
            "release cleanup verification",
        )
    except (JobError, OSError) as error:
        errors.append(str(error))
    else:
        if remaining_release is not None:
            errors.append("GitHub release still exists after exact cleanup")
    if not errors:
        try:
            remaining_tag = tag_commit(tag)
        except (JobError, OSError) as error:
            errors.append(str(error))
        else:
            if remaining_tag is not None:
                errors.append("GitHub release tag reappeared after cleanup")
    return errors, release_id


@publication_interruptible
def publish_release(
    *,
    directory: Path,
    tag: str,
    title: str,
    notes: Path,
    github_sha: str,
    assets: list[Path],
) -> None:
    notes_body, asset_metadata = validate_release_inputs(
        directory=directory,
        tag=tag,
        title=title,
        notes=notes,
        github_sha=github_sha,
        assets=assets,
    )
    require_gh_release_cli()
    if listed_release_by_tag(tag) is not None:
        raise JobError("GitHub release already exists")
    require_gh_absent(f"repos/kogeler/xpra/git/ref/tags/{tag}", "release tag")
    publication = directory / "publication.json"
    record: dict[str, Any] = {
        "assets": asset_metadata,
        "commit": github_sha,
        "owner": OWNER,
        "release_id": None,
        "schema": 1,
        "stage": "preflight",
        "tag": tag,
    }
    replace_json(publication, record)
    create_attempted = False
    publish_attempted = False
    release_id: int | None = None
    try:
        create_attempted = True
        created = gh_json(
            [
                "api",
                "--method",
                "POST",
                f"repos/{RELEASE_REPOSITORY}/releases",
                "-f",
                f"tag_name={tag}",
                "-f",
                f"target_commitish={github_sha}",
                "-f",
                f"name={title}",
                "-f",
                f"body={notes_body}",
                "-F",
                "draft=true",
                "-F",
                "prerelease=true",
            ]
        )
        candidate_id = created.get("id")
        if (
            isinstance(candidate_id, int)
            and not isinstance(candidate_id, bool)
            and candidate_id > 0
        ):
            release_id = candidate_id
        validated_id = validate_remote_release(
            created,
            tag=tag,
            title=title,
            notes_body=notes_body,
            github_sha=github_sha,
            asset_metadata={},
            draft=True,
        )
        if release_id is not None and validated_id != release_id:
            raise JobError("GitHub draft immutable ID changed in its create response")
        release_id = validated_id
        record.update({"release_id": release_id, "stage": "draft-created"})
        replace_json(publication, record)
        for asset in assets:
            background_job.ensure_private_regular(asset)
            expected_asset = asset_metadata[asset.name]
            if (
                asset.stat().st_size != expected_asset["size"]
                or f"sha256:{sha256_file(asset)}" != expected_asset["digest"]
            ):
                raise JobError(f"GitHub release asset changed before upload: {asset.name}")
            command(
                [
                    "gh",
                    "api",
                    (
                        "https://uploads.github.com/repos/kogeler/xpra/releases/"
                        f"{release_id}/assets?name={asset.name}"
                    ),
                    "--method",
                    "POST",
                    "--header",
                    "Content-Type: application/octet-stream",
                    "--input",
                    str(asset),
                ],
            )
        draft = gh_json(["api", f"repos/kogeler/xpra/releases/{release_id}"])
        if validate_remote_release(
            draft,
            tag=tag,
            title=title,
            notes_body=notes_body,
            github_sha=github_sha,
            asset_metadata=asset_metadata,
            draft=True,
        ) != release_id:
            raise JobError("GitHub release immutable ID changed after asset upload")
        record["stage"] = "assets-verified"
        replace_json(publication, record)
        publish_attempted = True
        published = gh_json(
            [
                "api",
                "--method",
                "PATCH",
                f"repos/kogeler/xpra/releases/{release_id}",
                "-F",
                "draft=false",
            ]
        )
        if validate_remote_release(
            published,
            tag=tag,
            title=title,
            notes_body=notes_body,
            github_sha=github_sha,
            asset_metadata=asset_metadata,
            draft=False,
        ) != release_id:
            raise JobError("GitHub release immutable ID changed while publishing")
        if tag_commit(tag) != github_sha:
            raise JobError("published GitHub release tag points at the wrong commit")
        record["stage"] = "published"
        replace_json(publication, record)
    except BaseException as error:
        cleanup_errors, rollback_id = rollback_release(
            tag=tag,
            title=title,
            notes_body=notes_body,
            github_sha=github_sha,
            asset_metadata=asset_metadata,
            release_id=release_id,
            create_attempted=create_attempted,
            publish_attempted=publish_attempted,
        )
        if rollback_id is not None:
            release_id = rollback_id
            record["release_id"] = rollback_id
        record["stage"] = "cleanup-failed" if cleanup_errors else "rolled-back"
        record["cleanup_errors"] = cleanup_errors
        replace_json(publication, record)
        if cleanup_errors:
            raise JobError(
                f"DEB publication failed ({error}); cleanup: {'; '.join(cleanup_errors)}"
            ) from error
        raise


def ci_release(args: argparse.Namespace) -> int:
    try:
        contrib.validate_deb_release_checkout(PROJECT_ROOT)
    except contrib.ContribError as error:
        raise JobError(str(error)) from error
    if args.selection != ACTIVE_SELECTION:
        raise JobError(f"DEB releases require the complete {ACTIVE_SELECTION} queue")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "")
    github_sha = os.environ.get("GITHUB_SHA", "")
    if not run_id.isdigit() or not attempt.isdigit() or not COMMIT_RE.fullmatch(github_sha):
        raise JobError("GitHub release run identity is invalid")
    require_amd64_host()
    require_gh_release_cli()
    prepare_state()
    release_name = f"run-{run_id}-attempt-{attempt}"
    directory = RELEASE_ROOT / release_name
    if directory.exists() or directory.is_symlink():
        raise JobError(f"DEB release staging directory already exists: {directory}")
    selection_cache = freeze_selection_cache(args.selection)
    args.source_state = freeze_checkout_source()
    hydrate_source_arguments(args)
    if args.checkout_commit != github_sha:
        raise JobError("DEB source snapshot does not match the dispatched checkout commit")
    directory.mkdir(mode=0o700)
    selection_snapshot = Path(selection_cache["selection_snapshot"])
    frozen_selection_sha256 = selection_cache["selection_sha256"]
    frozen_selection_cache_sha256 = selection_cache["selection_cache_sha256"]
    manifests: list[dict[str, Any]] = []
    assets: list[Path] = []
    for distro in DISTROS:
        output = directory / f"xpra-{distro}-amd64-debs.tar"
        build_args = argparse.Namespace(**vars(args))
        build_args.distro = distro
        build_args.output = str(output)
        build_args.container_name = f"xpra-deb-ci-{run_id}-{attempt}-{distro}"
        build_args.build_id = str(uuid.uuid4())
        build_args.container_state = directory / f".{distro}-container.json"
        build_args.output_partial = directory / f".{output.name}.partial"
        build_args.selection_cache_sha256 = frozen_selection_cache_sha256
        build_args.selection_sha256 = frozen_selection_sha256
        build_args.selection_snapshot = selection_snapshot
        build_args.selection_state = Path(selection_cache["selection_state"])
        manifests.append(build_distribution(build_args))
        assets.append(output)
    final_selection_cache = validate_selection_state(
        selection_snapshot.parent / "selection.json"
    )
    if (
        selection_digest(args.selection) != frozen_selection_sha256
        or final_selection_cache["selection_cache_sha256"]
        != frozen_selection_cache_sha256
    ):
        raise JobError("package selection changed across distribution builds")
    common_provenance = (
        "base_version",
        "checkout_commit",
        "debian_version",
        "revision",
        "revision_first_parent_count",
        "selection",
        "selection_cache_sha256",
        "selection_resolution_sha256",
        "selection_sha256",
        "source_commit",
        "source_ref",
        "source_ref_commit",
        "workflow_sha256",
    )
    if any(
        manifest.get(key) != manifests[0].get(key)
        for manifest in manifests[1:]
        for key in common_provenance
    ):
        raise JobError("distribution builds produced inconsistent source provenance")
    current_source = contrib.checkout_source_check(PROJECT_ROOT)
    if (
        current_source.head != args.checkout_commit
        or current_source.source_commit != args.source
        or current_source.master_ref != args.source_ref
        or current_source.master_commit != args.source_ref_commit
    ):
        raise JobError("checkout source changed across distribution builds")
    versions = {str(manifest["debian_version"]) for manifest in manifests}
    revisions = {int(manifest["revision"]) for manifest in manifests}
    if len(versions) != 1 or len(revisions) != 1:
        raise JobError("distribution builds produced inconsistent versions")
    version = versions.pop()
    revision = revisions.pop()
    tag = f"kogeler-deb-{version}-run{run_id}-attempt{attempt}"
    notes = directory / "release-notes.md"
    asset_metadata = release_asset_metadata(assets)
    transaction = release_transaction_record(
        run_id=run_id,
        attempt=attempt,
        github_sha=github_sha,
        version=version,
        assets=asset_metadata,
    )
    notes.write_text(
        release_notes_body(
            github_sha=github_sha,
            source=args.source,
            selection=args.selection,
            revision=revision,
            transaction=transaction,
        ),
        encoding="utf-8",
    )
    notes.chmod(0o600)
    reconcile_prior_release_attempts(
        run_id=run_id,
        attempt=attempt,
        github_sha=github_sha,
        version=version,
        source=args.source,
        selection=args.selection,
        revision=revision,
        asset_metadata=asset_metadata,
    )
    publish_release(
        directory=directory,
        tag=tag,
        title=f"Kogeler Xpra DEB {version}",
        notes=notes,
        github_sha=github_sha,
        assets=assets,
    )
    print(f"published GitHub Release {tag} with {len(assets)} assets")
    return 0


def add_build_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--build-id", required=True)
    parser.add_argument("--container-name", required=True)
    parser.add_argument("--container-state", type=Path, required=True)
    parser.add_argument("--distro", choices=sorted(DISTROS), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--output-partial", type=Path, required=True)
    parser.add_argument("--selection", required=True)
    parser.add_argument("--selection-cache-sha256", required=True)
    parser.add_argument("--selection-sha256", required=True)
    parser.add_argument("--selection-snapshot", type=Path, required=True)
    parser.add_argument("--selection-state", type=Path, required=True)
    parser.add_argument("--checkout-commit", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--source-bundle", type=Path, required=True)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--source-ref-commit", required=True)
    parser.add_argument("--source-state", type=Path, required=True)
    parser.add_argument("--workflow-sha256", required=True)


def add_public_build_arguments(parser: argparse.ArgumentParser, *, output: bool) -> None:
    if output:
        parser.add_argument("--container-name", required=True)
        parser.add_argument("--distro", choices=sorted(DISTROS), required=True)
        parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--selection", required=True)
    parser.add_argument("--source-state", type=Path, required=True)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    commands = value.add_subparsers(dest="command", required=True)
    commands.add_parser("host-check").set_defaults(
        handler=lambda _args: require_amd64_host() or 0
    )
    commands.add_parser("runner-sha").set_defaults(handler=lambda _args: print(runner_sha256()) or 0)
    commands.add_parser("source-snapshot").set_defaults(
        handler=lambda _args: print(freeze_checkout_source()) or 0
    )
    build = commands.add_parser("build")
    add_build_arguments(build)
    build.set_defaults(handler=lambda args: print(json.dumps(build_distribution(args), sort_keys=True)) or 0)
    worker = commands.add_parser("worker")
    add_build_arguments(worker)
    worker.set_defaults(handler=package_worker)
    start = commands.add_parser("start")
    start.add_argument("name")
    add_public_build_arguments(start, output=True)
    start.set_defaults(handler=package_start)
    for operation, handler in (
        ("status", package_status),
        ("logs", package_logs),
        ("wait", package_wait),
        ("collect", package_collect),
        ("remove", package_remove),
        ("abort", package_abort),
    ):
        operation_parser = commands.add_parser(operation)
        operation_parser.add_argument("name")
        operation_parser.set_defaults(handler=handler)
    release = commands.add_parser("ci-release")
    release.add_argument("--selection", required=True)
    release.set_defaults(handler=ci_release)
    return value


def main() -> int:
    os.umask(0o077)
    args = parser().parse_args()
    try:
        return int(args.handler(args))
    except (
        JobError,
        background_job.BackgroundJobError,
        container_payload.PayloadError,
        json.JSONDecodeError,
        OSError,
        tarfile.TarError,
        UnicodeError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
