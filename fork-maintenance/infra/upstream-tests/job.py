#!/usr/bin/env python3
# Copyright (C) 2026 kogeler
"""Own durable upstream-test containers and image-build processes."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import uuid
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

RUNNER_ROOT = Path(__file__).resolve().parent
LAB_ROOT = RUNNER_ROOT.parent.parent
PROJECT_ROOT = LAB_ROOT.parent
TOOLS_ROOT = LAB_ROOT / "tools"
sys.path.insert(0, str(TOOLS_ROOT))

import background_job
import container_payload

STATE_ROOT = PROJECT_ROOT / ".artifacts" / "fork-maintenance" / "upstream-tests"
LOG_ROOT = STATE_ROOT / "logs"
RUN_ROOT = STATE_ROOT / "runs"
IMAGE_BUILD_ROOT = STATE_ROOT / "image-builds"
SOURCE_ROOT = STATE_ROOT / "sources"
OWNER = "xpra-lab-upstream-tests"
IMAGE_OWNER = OWNER
CCACHE_VOLUME = "xpra-lab-upstream-ccache"
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
UUID4_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
SELECTOR_RE = re.compile(
    r"(?:cases|stacks|verifications)/[a-z0-9]+(?:-[a-z0-9]+)*"
)
TARGETS = {
    "versions",
    "patch-check",
    "focused",
    "quarantine",
    "quarantine-cython",
    "quarantine-no-compat",
    "wayland",
    "libyuv",
    "full",
    "full-cython",
    "full-no-compat",
}
SOURCE_REMOTES = {"origin", "upstream"}
RUNNER_INPUTS = (
    RUNNER_ROOT / "Makefile",
    RUNNER_ROOT / "private_state.py",
    RUNNER_ROOT / "job.py",
    TOOLS_ROOT / "background_job.py",
    TOOLS_ROOT / "container_payload.py",
)
IMAGE_CONTEXT_INPUTS = {
    ".containerignore": RUNNER_ROOT / ".containerignore",
    "Containerfile": RUNNER_ROOT / "Containerfile",
    "container_payload.py": TOOLS_ROOT / "container_payload.py",
    "entrypoint.sh": RUNNER_ROOT / "entrypoint.sh",
    "selection.py": RUNNER_ROOT / "selection.py",
}
CONTAINER_RUNNER = "/opt/xpra-lab-upstream-tests"
CONTAINER_PAYLOAD = f"{CONTAINER_RUNNER}/container_payload.py"
CONTAINER_INPUTS = "/work/payload"
CONTAINER_NOTIFY_FIFO = f"{CONTAINER_RUNNER}/payload-ready.fifo"


class JobError(RuntimeError):
    """Raised when a durable runner ownership invariant fails."""


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
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True,
        cwd=cwd,
        pass_fds=pass_fds,
    )
    if check and result.returncode:
        details = f"\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}" if capture else ""
        raise JobError(f"command failed ({result.returncode}): {argv!r}{details}")
    return result


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


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


def validate_name(value: str) -> str:
    if not NAME_RE.fullmatch(value):
        raise JobError(f"invalid job name: {value!r}")
    return value


def source_bundle_path(source: str, remote: str) -> Path:
    if not COMMIT_RE.fullmatch(source):
        raise JobError("invalid source commit")
    if remote not in SOURCE_REMOTES:
        raise JobError(f"invalid source remote: {remote!r}")
    return SOURCE_ROOT / f"{source}-{remote}.bundle"


def ensure_private_directory(path: Path, *, create: bool = False) -> None:
    try:
        background_job.ensure_private_directory(path, create=create)
    except background_job.BackgroundJobError as error:
        raise JobError(str(error)) from error


def ensure_private_regular(path: Path) -> None:
    try:
        background_job.ensure_private_regular(path)
    except background_job.BackgroundJobError as error:
        raise JobError(str(error)) from error


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
        LOG_ROOT,
        RUN_ROOT,
        IMAGE_BUILD_ROOT,
        SOURCE_ROOT,
    ):
        ensure_private_directory(path, create=True)


@contextmanager
def lifecycle_lock(name: str) -> Any:
    """Serialize a named collect/abort operation with a crash-releasing lock."""
    validate_name(name)
    path = LOG_ROOT / ".lifecycle.lock"
    ensure_private_directory(path.parent, create=True)
    try:
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as error:
        raise JobError(f"cannot open lifecycle lock {path}: {error}") from error
    try:
        os.fchmod(descriptor, 0o600)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or info.st_mode & 0o177
        ):
            raise JobError(f"unsafe lifecycle lock: {path}")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise JobError(f"collection or abort is already active: {name}") from error
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def image_cache_lock() -> Any:
    """Serialize image creation, inspection, use handoff, and cache removal."""
    path = IMAGE_BUILD_ROOT / ".image-cache.lock"
    ensure_private_directory(path.parent, create=True)
    try:
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as error:
        raise JobError(f"cannot open image-cache lock {path}: {error}") from error
    try:
        os.fchmod(descriptor, 0o600)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or info.st_mode & 0o177
        ):
            raise JobError(f"unsafe image-cache lock: {path}")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise JobError("image cache is in active use; retry later") from error
        yield descriptor
    finally:
        os.close(descriptor)


def source_bundle_heads(path: Path) -> str:
    return command(["git", "bundle", "list-heads", str(path)]).stdout.strip()


def verify_source_bundle(path: Path, source_host: Path, source_ref: str, head: str) -> None:
    ensure_private_regular(path)
    command(["git", "-C", str(source_host), "bundle", "verify", str(path)])
    if source_bundle_heads(path) != f"{head} {source_ref}":
        raise JobError(f"source bundle has unexpected heads: {path}")


def source_snapshot(args: argparse.Namespace) -> int:
    """Publish one exact source bundle with recoverable deterministic staging."""
    prepare_state()
    source_host = Path(args.source_host).resolve()
    if source_host != PROJECT_ROOT.resolve():
        raise JobError("source snapshot host is not the current repository")
    if not COMMIT_RE.fullmatch(args.source_head):
        raise JobError("invalid source snapshot head")
    if args.source_remote not in SOURCE_REMOTES:
        raise JobError(f"invalid source remote: {args.source_remote!r}")
    source_ref = f"refs/remotes/{args.source_remote}/master"
    if args.source_ref != source_ref:
        raise JobError("source snapshot ref does not match its remote")
    bundle = source_bundle_path(args.source_head, args.source_remote)
    if Path(args.bundle) != bundle:
        raise JobError("source snapshot bundle path is inconsistent")
    partial = source_bundle_partial_path(args.source_head, args.source_remote)
    lock = source_bundle_lock_path(args.source_head, args.source_remote)
    lock_fd = os.open(
        lock,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(lock_fd, 0o600)
        lock_stat = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(lock_stat.st_mode)
            or lock_stat.st_uid != os.getuid()
            or lock_stat.st_nlink != 1
            or lock_stat.st_mode & 0o177 != 0
        ):
            raise JobError(f"unsafe source bundle lock: {lock}")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if bundle.exists() or bundle.is_symlink():
            verify_source_bundle(bundle, source_host, source_ref, args.source_head)
            print(f"using verified source bundle {bundle}")
            return 0
        if partial.exists() or partial.is_symlink():
            if partial.is_symlink():
                raise JobError(f"refusing symlinked source bundle partial: {partial}")
            ensure_private_regular(partial)
            partial.unlink()
        if command(
            ["git", "-C", str(source_host), "rev-parse", source_ref]
        ).stdout.strip() != args.source_head:
            raise JobError("source ref changed before bundle creation")
        # The child inherits the lock, so a killed parent cannot expose a writable
        # partial to a concurrent recovery while `git bundle create` is still active.
        created = subprocess.run(
            [
                "git",
                "-C",
                str(source_host),
                "bundle",
                "create",
                str(partial),
                source_ref,
            ],
            check=False,
            capture_output=True,
            text=True,
            pass_fds=(lock_fd,),
        )
        if created.returncode:
            raise JobError(
                "source bundle creation failed "
                f"({created.returncode}):\nstdout:\n{created.stdout}\n"
                f"stderr:\n{created.stderr}"
            )
        partial.chmod(0o600)
        verify_source_bundle(partial, source_host, source_ref, args.source_head)
        if command(
            ["git", "-C", str(source_host), "rev-parse", source_ref]
        ).stdout.strip() != args.source_head:
            raise JobError("source ref changed during bundle creation")
        try:
            container_payload.rename_no_replace(partial, bundle)
        except container_payload.PayloadError:
            if not bundle.exists() or bundle.is_symlink():
                raise
            verify_source_bundle(bundle, source_host, source_ref, args.source_head)
        verify_source_bundle(bundle, source_host, source_ref, args.source_head)
        print(f"published source bundle {bundle}")
        return 0
    finally:
        if partial.exists() and not partial.is_symlink():
            try:
                ensure_private_regular(partial)
            except JobError:
                pass
            else:
                partial.unlink()
        os.close(lock_fd)


def publish_bytes(path: Path, payload: bytes) -> None:
    try:
        background_job.publish_bytes(path, payload)
    except (background_job.BackgroundJobError, OSError) as error:
        raise JobError(str(error)) from error


def publish_status(path: Path, values: dict[str, Any]) -> None:
    payload = "".join(f"{key}={value}\n" for key, value in values.items()).encode()
    publish_bytes(path, payload)


def publish_record(path: Path, values: dict[str, Any]) -> None:
    publish_status(path, values)


def publish_json(path: Path, values: dict[str, Any]) -> None:
    publish_bytes(
        path,
        (json.dumps(values, indent=2, sort_keys=True) + "\n").encode(),
    )


def parse_record(path: Path) -> dict[str, str]:
    ensure_private_regular(path)
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise JobError(f"cannot read ownership record: {path}") from error
    for line in lines:
        key, separator, value = line.partition("=")
        if not separator or not key or key in values or "\n" in value:
            raise JobError(f"invalid ownership record: {path}")
        values[key] = value
    return values


def test_record_path(name: str) -> Path:
    return RUN_ROOT / f"{name}.owner"


def test_payload_path(name: str) -> Path:
    return RUN_ROOT / f"{validate_name(name)}.payload"


def test_prelaunch_path(name: str) -> Path:
    return RUN_ROOT / f"{validate_name(name)}.prelaunch.json"


def foreground_payload_path() -> Path:
    return STATE_ROOT / ".foreground-payload"


def foreground_payload_marker_path() -> Path:
    return STATE_ROOT / ".foreground-payload.owner.json"


@contextmanager
def foreground_payload_lock() -> Any:
    path = STATE_ROOT / ".foreground-payload.lock"
    ensure_private_directory(path.parent, create=True)
    descriptor = os.open(
        path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or info.st_mode & 0o177
        ):
            raise JobError(f"unsafe foreground payload lock: {path}")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise JobError("an upstream foreground payload is already active") from error
        yield descriptor
    finally:
        os.close(descriptor)


def recover_foreground_payload() -> None:
    root = foreground_payload_path()
    marker = foreground_payload_marker_path()
    root_present = root.exists() or root.is_symlink()
    marker_present = marker.exists() or marker.is_symlink()
    if not root_present and not marker_present:
        return
    if not marker_present:
        raise JobError("foreground payload partial has no ownership marker")
    try:
        record = background_job.load_json(marker)
    except background_job.BackgroundJobError as error:
        raise JobError(str(error)) from error
    expected = {
        "schema": 1,
        "owner": OWNER,
        "kind": "foreground-payload",
        "path": str(root),
    }
    for key, value in expected.items():
        if record.get(key) != value:
            raise JobError(f"foreground payload ownership mismatch for {key}")
    if root_present:
        if root.is_symlink():
            raise JobError(f"refusing symlinked foreground payload: {root}")
        ensure_private_directory(root)
        shutil.rmtree(root)
    marker.unlink()


def source_bundle_partial_path(source: str, remote: str) -> Path:
    return source_bundle_path(source, remote).with_suffix(".bundle.partial")


def source_bundle_lock_path(source: str, remote: str) -> Path:
    return source_bundle_path(source, remote).with_suffix(".bundle.lock")


def log_path(name: str) -> Path:
    return LOG_ROOT / f"{name}.log"


def status_path(name: str) -> Path:
    return LOG_ROOT / f"{name}.status"


def remove_transaction_path(name: str) -> Path:
    return LOG_ROOT / f"{validate_name(name)}.remove.json"


def result_paths(name: str) -> tuple[Path, ...]:
    return (log_path(name), status_path(name))


def require_absent(paths: tuple[Path, ...], description: str) -> None:
    for path in paths:
        if path.exists() or path.is_symlink():
            raise JobError(f"{description} already exists: {path}")


def publish_remove_transaction(
    name: str,
    kind: str,
    record: dict[str, Any],
    owner_path: Path,
) -> dict[str, Any]:
    """Publish immutable authority before removing any collected runtime state."""
    if kind not in {"test-remove", "image-build-remove"}:
        raise JobError(f"unsupported removal transaction kind: {kind}")
    for path in (owner_path, log_path(name), status_path(name)):
        ensure_private_regular(path)
    transaction = {
        "schema": 1,
        "owner": OWNER,
        "kind": kind,
        "name": name,
        "record": record,
        "owner_sha256": sha256_file(owner_path),
        "log_sha256": sha256_file(log_path(name)),
        "status_sha256": sha256_file(status_path(name)),
    }
    publish_json(remove_transaction_path(name), transaction)
    return transaction


def load_remove_transaction(name: str, kind: str, owner_path: Path) -> dict[str, Any]:
    """Load a retained removal authority and revalidate its immutable evidence."""
    try:
        transaction = background_job.load_json(remove_transaction_path(name))
    except background_job.BackgroundJobError as error:
        raise JobError(str(error)) from error
    if set(transaction) != {
        "schema",
        "owner",
        "kind",
        "name",
        "record",
        "owner_sha256",
        "log_sha256",
        "status_sha256",
    }:
        raise JobError("removal transaction fields are inconsistent")
    if (
        transaction.get("schema") != 1
        or transaction.get("owner") != OWNER
        or transaction.get("kind") != kind
        or transaction.get("name") != name
    ):
        raise JobError("removal transaction identity is inconsistent")
    record = transaction.get("record")
    if not isinstance(record, dict) or record.get("name") != name:
        raise JobError("removal transaction has an invalid ownership record")
    if kind == "test-remove":
        if record.get("schema") != "4" or record.get("owner") != OWNER:
            raise JobError("test removal transaction has an invalid ownership record")
        if not SHA256_RE.fullmatch(str(record.get("container_id", ""))):
            raise JobError("test removal transaction has an invalid container ID")
    elif (
        record.get("schema") not in {2, 3}
        or record.get("owner") != IMAGE_OWNER
        or record.get("kind") != "image-build"
    ):
        raise JobError("image removal transaction has an invalid ownership record")
    for key, path in (
        ("log_sha256", log_path(name)),
        ("status_sha256", status_path(name)),
    ):
        ensure_private_regular(path)
        expected = str(transaction.get(key, ""))
        if not SHA256_RE.fullmatch(expected) or sha256_file(path) != expected:
            raise JobError(f"removal transaction evidence changed: {path}")
    expected_owner_sha = str(transaction.get("owner_sha256", ""))
    if not SHA256_RE.fullmatch(expected_owner_sha):
        raise JobError("removal transaction has an invalid owner digest")
    if owner_path.exists() or owner_path.is_symlink():
        ensure_private_regular(owner_path)
        if sha256_file(owner_path) != expected_owner_sha:
            raise JobError("removal transaction owner record changed")
    return transaction


def selection_digest(
    selection: str,
    *,
    lab_root: Path = LAB_ROOT,
    pass_fds: tuple[int, ...] = (),
) -> str:
    result = command(
        [
            sys.executable,
            str(RUNNER_ROOT / "selection.py"),
            "--lab-root",
            str(lab_root),
            "--selection",
            selection,
            "digest",
        ],
        pass_fds=pass_fds,
    ).stdout.strip()
    if not SHA256_RE.fullmatch(result):
        raise JobError("selection resolver returned an invalid digest")
    return result


@contextmanager
def test_payload(
    *,
    selection: str,
    expected_selection_sha256: str,
    source_head: str,
    source_remote: str,
    owner_name: str | None = None,
    lock_descriptor: int | None = None,
) -> Any:
    """Freeze one minimal selection and expose it with the source bundle."""
    if not SHA256_RE.fullmatch(expected_selection_sha256):
        raise JobError("invalid expected selection digest")
    source_bundle = source_bundle_path(source_head, source_remote)
    ensure_private_regular(source_bundle)
    if owner_name is not None and lock_descriptor is None:
        raise JobError("named test payload has no inherited start lock")
    lock_context = (
        foreground_payload_lock()
        if owner_name is None
        else nullcontext(lock_descriptor)
    )
    with lock_context as active_lock:
        if owner_name is None:
            recover_foreground_payload()
            root = foreground_payload_path()
            publish_json(
                foreground_payload_marker_path(),
                {
                    "schema": 1,
                    "owner": OWNER,
                    "kind": "foreground-payload",
                    "path": str(root),
                    "selection": selection,
                    "selection_sha256": expected_selection_sha256,
                    "source_head": source_head,
                    "source_remote": source_remote,
                },
            )
        else:
            root = test_payload_path(owner_name)
        root.mkdir(mode=0o700)
        ensure_private_directory(root)
        snapshot = root / "lab"
        inherited = () if active_lock is None else (active_lock,)
        try:
            command(
                [
                    sys.executable,
                    str(RUNNER_ROOT / "selection.py"),
                    "--lab-root",
                    str(LAB_ROOT),
                    "--selection",
                    selection,
                    "snapshot",
                    "--destination",
                    str(snapshot),
                ],
                pass_fds=inherited,
            )
            if (
                selection_digest(
                    selection,
                    lab_root=snapshot,
                    pass_fds=inherited,
                )
                != expected_selection_sha256
            ):
                raise JobError("selection changed while its container payload was frozen")
            yield (
                (
                    container_payload.PayloadEntry(
                        source_bundle,
                        PurePosixPath("source.bundle"),
                    ),
                    container_payload.PayloadEntry(snapshot, PurePosixPath("lab")),
                ),
                active_lock,
            )
        finally:
            if root.exists() or root.is_symlink():
                if root.is_symlink():
                    raise JobError(f"refusing symlinked test payload: {root}")
                ensure_private_directory(root)
                shutil.rmtree(root)
            if owner_name is None:
                foreground_payload_marker_path().unlink(missing_ok=True)


def payload_environment(args: argparse.Namespace, selection_sha256: str) -> list[str]:
    source_ref = f"refs/remotes/{args.source_remote}/master"
    return [
        "--env",
        f"XPRA_EXPECTED_SOURCE_COMMIT={args.source}",
        "--env",
        f"XPRA_EXPECTED_SOURCE_HEAD={args.source_head}",
        "--env",
        f"XPRA_EXPECTED_SOURCE_REF={source_ref}",
        "--env",
        f"XPRA_EXPECTED_WORKFLOW_SHA={args.workflow_sha256}",
        "--env",
        f"XPRA_LAB_SELECTION={args.selection}",
        "--env",
        f"XPRA_PATCH_MODE={args.patch_mode}",
        "--env",
        f"XPRA_EXPECTED_SELECTION_SHA={selection_sha256}",
    ]


def test_runtime_options(args: argparse.Namespace, selection_sha256: str) -> list[str]:
    return [
        "--userns",
        "keep-id:uid=1000,gid=1000",
        "--user",
        "1000:1000",
        *payload_environment(args, selection_sha256),
        "--volume",
        f"{CCACHE_VOLUME}:/home/ubuntu/.cache/ccache:U",
    ]


def send_test_payload(
    container_id: str,
    args: argparse.Namespace,
    selection_sha256: str,
) -> None:
    with test_payload(
        selection=args.selection,
        expected_selection_sha256=selection_sha256,
        source_head=args.source_head,
        source_remote=args.source_remote,
        owner_name=args.name,
        lock_descriptor=int(args.lifecycle_lock_descriptor),
    ) as payload:
        entries, lock_descriptor = payload
        if lock_descriptor is None:
            raise JobError("named test payload has no recovery lock")
        argv = [
            "podman",
            "exec",
            "--interactive",
            container_id,
            "python3",
            CONTAINER_PAYLOAD,
            "extract",
            "--destination",
            CONTAINER_INPUTS,
        ]
        argv.extend(("--notify-fifo", CONTAINER_NOTIFY_FIFO))
        container_payload.stream_to_process(
            argv,
            entries,
            pass_fds=(lock_descriptor,),
        )


def inspect_json(argv: list[str]) -> dict[str, Any]:
    try:
        payload = json.loads(command(argv).stdout)
    except json.JSONDecodeError as error:
        raise JobError(f"invalid Podman inspection output: {argv!r}") from error
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise JobError(f"unexpected Podman inspection output: {argv!r}")
    return payload[0]


def image_identity(
    image: str,
    image_input: str,
    workflow: str,
    *,
    source: str,
    build_run_id: str | None = None,
) -> str:
    if (
        not SHA256_RE.fullmatch(image_input)
        or not SHA256_RE.fullmatch(workflow)
        or not COMMIT_RE.fullmatch(source)
        or (build_run_id is not None and not UUID4_RE.fullmatch(build_run_id))
    ):
        raise JobError("invalid expected image provenance")
    item = inspect_json(["podman", "image", "inspect", image])
    image_id = str(item.get("Id", "")).removeprefix("sha256:")
    labels = item.get("Labels")
    if labels is None and isinstance(item.get("Config"), dict):
        labels = item["Config"].get("Labels")
    if not SHA256_RE.fullmatch(image_id) or not isinstance(labels, dict):
        raise JobError(f"invalid owned image metadata: {image}")
    expected = {
        "io.xpra.lab.image-builder": "true",
        "io.xpra.lab.image-input": image_input,
        "io.xpra.lab.source": source,
        "io.xpra.lab.workflow": workflow,
    }
    actual_build_run = str(labels.get("io.xpra.lab.image-build-run-id", ""))
    if build_run_id is None:
        if not UUID4_RE.fullmatch(actual_build_run):
            raise JobError(f"image has an invalid build-run label: {image}")
        expected["io.xpra.lab.image-build-run-id"] = actual_build_run
    else:
        expected["io.xpra.lab.image-build-run-id"] = build_run_id
    actual_labels = {str(key): str(value) for key, value in labels.items()}
    if not exact_lab_labels(actual_labels, expected):
        raise JobError(f"image ownership labels do not match: {image}")
    return image_id


def removable_image_identity(image: str, image_input: str, workflow: str) -> str:
    """Resolve an exactly owned cache image even when its source label is stale."""
    if not SHA256_RE.fullmatch(image_input) or not SHA256_RE.fullmatch(workflow):
        raise JobError("invalid expected image provenance")
    item = inspect_json(["podman", "image", "inspect", image])
    image_id = str(item.get("Id", "")).removeprefix("sha256:")
    labels = item.get("Labels")
    if labels is None and isinstance(item.get("Config"), dict):
        labels = item["Config"].get("Labels")
    if not SHA256_RE.fullmatch(image_id) or not isinstance(labels, dict):
        raise JobError(f"invalid owned image metadata: {image}")
    actual_labels = {str(key): str(value) for key, value in labels.items()}
    source = actual_labels.get("io.xpra.lab.source", "")
    build_run = actual_labels.get("io.xpra.lab.image-build-run-id", "")
    if not COMMIT_RE.fullmatch(source) or not UUID4_RE.fullmatch(build_run):
        raise JobError(f"image has invalid removal provenance: {image}")
    expected = {
        "io.xpra.lab.image-builder": "true",
        "io.xpra.lab.image-build-run-id": build_run,
        "io.xpra.lab.image-input": image_input,
        "io.xpra.lab.source": source,
        "io.xpra.lab.workflow": workflow,
    }
    if not exact_lab_labels(actual_labels, expected):
        raise JobError(f"image ownership labels do not match: {image}")
    return image_id


def test_labels(
    args: argparse.Namespace,
    *,
    image_id: str,
    run_id: str,
    runner_digest: str,
    selection_sha256: str,
) -> dict[str, str]:
    return {
        "io.xpra.lab.upstream-test": "true",
        "io.xpra.lab.owner": OWNER,
        "io.xpra.lab.run-id": run_id,
        "io.xpra.lab.target": args.target,
        "io.xpra.lab.selection": args.selection,
        "io.xpra.lab.selection-sha256": selection_sha256,
        "io.xpra.lab.patch-mode": args.patch_mode,
        "io.xpra.lab.source": args.source,
        "io.xpra.lab.source-head": args.source_head,
        "io.xpra.lab.source-remote": args.source_remote,
        "io.xpra.lab.workflow": args.workflow_sha256,
        "io.xpra.lab.runner": runner_digest,
        "io.xpra.lab.image-id": image_id,
        "io.xpra.lab.image-input": args.image_input_sha256,
    }


def exact_lab_labels(actual: dict[str, str], expected: dict[str, str]) -> bool:
    return {
        key: value for key, value in actual.items() if key.startswith("io.xpra.lab.")
    } == expected


def exact_test_container_labels(
    actual: dict[str, str], expected: dict[str, str], image_id: str
) -> bool:
    """Include only the two labels that Podman inherits from the owned image."""
    image_build_run = actual.get("io.xpra.lab.image-build-run-id", "")
    if (
        actual.get("io.xpra.lab.image-builder") != "true"
        or not UUID4_RE.fullmatch(image_build_run)
    ):
        return False
    complete = {
        **expected,
        "io.xpra.lab.image-builder": "true",
        "io.xpra.lab.image-build-run-id": image_build_run,
    }
    if not exact_lab_labels(actual, complete):
        return False
    required = {
        "io.xpra.lab.image-input",
        "io.xpra.lab.source",
        "io.xpra.lab.workflow",
    }
    if not required.issubset(expected):
        return False
    image = inspect_json(["podman", "image", "inspect", image_id])
    inspected_id = str(image.get("Id", "")).removeprefix("sha256:")
    labels = image.get("Labels")
    if labels is None and isinstance(image.get("Config"), dict):
        labels = image["Config"].get("Labels")
    if inspected_id != image_id or not isinstance(labels, dict):
        return False
    image_labels = {str(key): str(value) for key, value in labels.items()}
    image_expected = {
        "io.xpra.lab.image-builder": "true",
        "io.xpra.lab.image-build-run-id": image_build_run,
        "io.xpra.lab.image-input": expected["io.xpra.lab.image-input"],
        "io.xpra.lab.source": expected["io.xpra.lab.source"],
        "io.xpra.lab.workflow": expected["io.xpra.lab.workflow"],
    }
    return exact_lab_labels(image_labels, image_expected)


def load_test_prelaunch(name: str) -> dict[str, Any]:
    name = validate_name(name)
    try:
        record = background_job.load_json(test_prelaunch_path(name))
    except background_job.BackgroundJobError as error:
        raise JobError(str(error)) from error
    required = {
        "image",
        "image_id",
        "kind",
        "labels",
        "name",
        "owner",
        "payload_path",
        "process",
        "run_id",
        "runner_sha256",
        "schema",
    }
    if set(record) != required:
        raise JobError("test prelaunch ownership fields are inconsistent")
    if (
        record.get("schema") != 1
        or record.get("owner") != OWNER
        or record.get("kind") != "test-prelaunch"
        or record.get("name") != name
        or record.get("payload_path") != str(test_payload_path(name))
    ):
        raise JobError("test prelaunch ownership identity is inconsistent")
    if not UUID4_RE.fullmatch(str(record.get("run_id", ""))):
        raise JobError("test prelaunch ownership has an invalid run ID")
    if not SHA256_RE.fullmatch(str(record.get("image_id", ""))) or not SHA256_RE.fullmatch(
        str(record.get("runner_sha256", ""))
    ):
        raise JobError("test prelaunch ownership has invalid immutable digests")
    if not isinstance(record.get("image"), str) or not record["image"]:
        raise JobError("test prelaunch ownership has an invalid image")
    labels = record.get("labels")
    expected_label_keys = {
        "io.xpra.lab.image-id",
        "io.xpra.lab.image-input",
        "io.xpra.lab.owner",
        "io.xpra.lab.patch-mode",
        "io.xpra.lab.run-id",
        "io.xpra.lab.runner",
        "io.xpra.lab.selection",
        "io.xpra.lab.selection-sha256",
        "io.xpra.lab.source",
        "io.xpra.lab.source-head",
        "io.xpra.lab.source-remote",
        "io.xpra.lab.target",
        "io.xpra.lab.upstream-test",
        "io.xpra.lab.workflow",
    }
    if (
        not isinstance(labels, dict)
        or set(labels) != expected_label_keys
        or labels.get("io.xpra.lab.owner") != OWNER
        or labels.get("io.xpra.lab.upstream-test") != "true"
        or labels.get("io.xpra.lab.run-id") != record["run_id"]
        or labels.get("io.xpra.lab.image-id") != record["image_id"]
        or not all(isinstance(key, str) and isinstance(value, str) for key, value in labels.items())
    ):
        raise JobError("test prelaunch ownership has invalid labels")
    if (
        labels["io.xpra.lab.target"] not in TARGETS
        or labels["io.xpra.lab.patch-mode"] not in {"clean", "tests-only", "patched"}
        or not SELECTOR_RE.fullmatch(labels["io.xpra.lab.selection"])
        or labels["io.xpra.lab.source-remote"] not in SOURCE_REMOTES
        or not COMMIT_RE.fullmatch(labels["io.xpra.lab.source"])
        or not COMMIT_RE.fullmatch(labels["io.xpra.lab.source-head"])
        or any(
            not SHA256_RE.fullmatch(labels[key])
            for key in (
                "io.xpra.lab.image-id",
                "io.xpra.lab.image-input",
                "io.xpra.lab.runner",
                "io.xpra.lab.selection-sha256",
                "io.xpra.lab.workflow",
            )
        )
        or labels["io.xpra.lab.runner"] != record["runner_sha256"]
    ):
        raise JobError("test prelaunch ownership has invalid label values")
    process = record.get("process")
    if not isinstance(process, dict) or set(process) != {"pid", "start_ticks"}:
        raise JobError("test prelaunch ownership has invalid process identity")
    if (
        not isinstance(process.get("pid"), int)
        or process["pid"] <= 1
        or not isinstance(process.get("start_ticks"), str)
        or not process["start_ticks"].isdigit()
    ):
        raise JobError("test prelaunch ownership has invalid process identity")
    return record


def test_prelaunch_active(record: dict[str, Any]) -> bool:
    process = record["process"]
    try:
        identity = background_job.process_identity(int(process["pid"]))
    except background_job.BackgroundJobError as error:
        raise JobError(str(error)) from error
    if identity is None:
        return False
    state, _process_group, start_ticks = identity
    return state != "Z" and start_ticks == process["start_ticks"]


def prelaunch_container_id(record: dict[str, Any]) -> str | None:
    name = str(record["name"])
    exists = command(["podman", "container", "exists", name], check=False)
    if exists.returncode == 1:
        return None
    if exists.returncode != 0:
        raise JobError(f"cannot inspect prelaunch test container: {name}")
    item = inspect_json(["podman", "container", "inspect", name])
    config = item.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    actual_labels = (
        {str(key): str(value) for key, value in labels.items()}
        if isinstance(labels, dict)
        else None
    )
    container_id = str(item.get("Id", ""))
    image_id = str(item.get("Image", "")).removeprefix("sha256:")
    actual_name = str(item.get("Name", "")).lstrip("/")
    if (
        not SHA256_RE.fullmatch(container_id)
        or image_id != record["image_id"]
        or actual_name != name
        or actual_labels is None
        or not exact_test_container_labels(
            actual_labels,
            record["labels"],
            record["image_id"],
        )
    ):
        raise JobError("refusing container that does not match prelaunch ownership")
    return container_id


def remove_test_prelaunch(name: str) -> None:
    path = test_prelaunch_path(name)
    if not path.exists() and not path.is_symlink():
        return
    ensure_private_regular(path)
    path.unlink()


def load_test_record(name: str, *, require_current: bool = True) -> dict[str, str]:
    name = validate_name(name)
    record = parse_record(test_record_path(name))
    required = {
        "container_id",
        "image",
        "image_id",
        "image_input_sha256",
        "name",
        "owner",
        "patch_mode",
        "payload_path",
        "run_id",
        "runner_sha256",
        "schema",
        "selection",
        "selection_sha256",
        "source",
        "source_head",
        "source_remote",
        "target",
        "workflow_sha256",
    }
    if set(record) != required:
        raise JobError("background ownership record fields are inconsistent")
    if record["schema"] != "4" or record["owner"] != OWNER or record["name"] != name:
        raise JobError("background ownership record identity is inconsistent")
    if record["payload_path"] != str(test_payload_path(name)):
        raise JobError("background ownership record has an invalid payload path")
    if not UUID4_RE.fullmatch(record["run_id"]):
        raise JobError("background ownership record has an invalid run ID")
    if record["target"] not in TARGETS or record["patch_mode"] not in {
        "clean",
        "tests-only",
        "patched",
    }:
        raise JobError("background ownership record has invalid test options")
    if not SELECTOR_RE.fullmatch(record["selection"]):
        raise JobError("background ownership record has an invalid selection")
    for key in (
        "container_id",
        "image_id",
        "image_input_sha256",
        "runner_sha256",
        "selection_sha256",
        "workflow_sha256",
    ):
        if not SHA256_RE.fullmatch(record[key]):
            raise JobError(f"background ownership record has invalid {key}")
    if not COMMIT_RE.fullmatch(record["source"]):
        raise JobError("background ownership record has an invalid source commit")
    if not COMMIT_RE.fullmatch(record["source_head"]):
        raise JobError("background ownership record has an invalid source head")
    if record["source_remote"] not in SOURCE_REMOTES:
        raise JobError("background ownership record has an invalid source remote")
    if require_current and record["runner_sha256"] != runner_sha256():
        raise JobError("background runner changed while the job was owned")
    return record


def test_record_labels(record: dict[str, str]) -> dict[str, str]:
    return {
        "io.xpra.lab.upstream-test": "true",
        "io.xpra.lab.owner": record["owner"],
        "io.xpra.lab.run-id": record["run_id"],
        "io.xpra.lab.target": record["target"],
        "io.xpra.lab.selection": record["selection"],
        "io.xpra.lab.selection-sha256": record["selection_sha256"],
        "io.xpra.lab.patch-mode": record["patch_mode"],
        "io.xpra.lab.source": record["source"],
        "io.xpra.lab.source-head": record["source_head"],
        "io.xpra.lab.source-remote": record["source_remote"],
        "io.xpra.lab.workflow": record["workflow_sha256"],
        "io.xpra.lab.runner": record["runner_sha256"],
        "io.xpra.lab.image-id": record["image_id"],
        "io.xpra.lab.image-input": record["image_input_sha256"],
    }


def matching_test_prelaunch(record: dict[str, str]) -> dict[str, Any] | None:
    path = test_prelaunch_path(record["name"])
    if not path.exists() and not path.is_symlink():
        return None
    prelaunch = load_test_prelaunch(record["name"])
    if (
        prelaunch["run_id"] != record["run_id"]
        or prelaunch["image"] != record["image"]
        or prelaunch["image_id"] != record["image_id"]
        or prelaunch["runner_sha256"] != record["runner_sha256"]
        or prelaunch["labels"] != test_record_labels(record)
    ):
        raise JobError("stale test prelaunch ownership does not match its owner record")
    return prelaunch


def container_state(record: dict[str, str]) -> dict[str, Any]:
    item = inspect_json(["podman", "container", "inspect", record["container_id"]])
    config = item.get("Config")
    state = item.get("State")
    labels = config.get("Labels") if isinstance(config, dict) else None
    if not isinstance(labels, dict) or not isinstance(state, dict):
        raise JobError("owned container inspection is incomplete")
    expected = test_record_labels(record)
    actual_labels = {str(key): str(value) for key, value in labels.items()}
    if not exact_test_container_labels(actual_labels, expected, record["image_id"]):
        raise JobError("refusing container whose ownership labels do not match")
    actual_id = str(item.get("Id", ""))
    image_id = str(item.get("Image", "")).removeprefix("sha256:")
    actual_name = str(item.get("Name", "")).lstrip("/")
    if actual_id != record["container_id"] or image_id != record["image_id"]:
        raise JobError("owned container immutable identity does not match")
    if actual_name != record["name"]:
        raise JobError("owned container name does not match")
    return state


def container_lifecycle_state(record: dict[str, str]) -> dict[str, Any]:
    """Classify one exactly owned container for abort/collect decisions."""
    exists = command(
        ["podman", "container", "exists", record["container_id"]],
        check=False,
    )
    if exists.returncode == 1:
        return {"state": "lost", "container_status": ""}
    if exists.returncode != 0:
        raise JobError(f"cannot inspect owned test container: {record['name']}")
    details = container_state(record)
    container_status = str(details.get("Status", ""))
    if container_status in {"created", "configured", "running", "paused", "stopping"}:
        state = "running"
    elif container_status in {"exited", "stopped"}:
        state = "completed"
    else:
        raise JobError(f"test container has an unsupported state: {container_status!r}")
    return {"state": state, "container_status": container_status}


def remove_test_payload(name: str) -> None:
    path = test_payload_path(name)
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink():
        raise JobError(f"refusing symlinked test payload: {path}")
    ensure_private_directory(path)
    shutil.rmtree(path)


def test_start(args: argparse.Namespace) -> int:
    prepare_state()
    name = validate_name(args.name)
    with lifecycle_lock(name) as lock_descriptor, image_cache_lock() as cache_descriptor:
        args.lifecycle_lock_descriptor = lock_descriptor
        args.image_cache_lock_descriptor = cache_descriptor
        try:
            return _test_start_locked(args)
        finally:
            del args.lifecycle_lock_descriptor
            del args.image_cache_lock_descriptor


def _test_start_locked(args: argparse.Namespace) -> int:
    name = validate_name(args.name)
    if args.target not in TARGETS or args.patch_mode not in {"clean", "tests-only", "patched"}:
        raise JobError("invalid background test options")
    if not SELECTOR_RE.fullmatch(args.selection):
        raise JobError(f"invalid selection: {args.selection!r}")
    require_absent(
        (
            *result_paths(name),
            test_record_path(name),
            test_payload_path(name),
            test_prelaunch_path(name),
            remove_transaction_path(name),
        ),
        "job artifact",
    )
    exists = command(["podman", "container", "exists", name], check=False)
    if exists.returncode == 0:
        raise JobError(f"container already exists: {name}")
    if exists.returncode != 1:
        raise JobError(f"cannot check container name: {name}")
    if not COMMIT_RE.fullmatch(args.source) or not COMMIT_RE.fullmatch(args.source_head):
        raise JobError("invalid source identity")
    if args.source_remote not in SOURCE_REMOTES:
        raise JobError("invalid source remote")
    image_id = image_identity(
        args.image,
        args.image_input_sha256,
        args.workflow_sha256,
        source=args.source,
    )
    selection_sha = selection_digest(args.selection)
    run_id = str(uuid.uuid4())
    runner_digest = runner_sha256()
    labels = test_labels(
        args,
        image_id=image_id,
        run_id=run_id,
        runner_digest=runner_digest,
        selection_sha256=selection_sha,
    )
    process_identity = background_job.process_identity(os.getpid())
    if process_identity is None:
        raise JobError("cannot bind the test prelaunch process identity")
    _state, _process_group, start_ticks = process_identity
    prelaunch = {
        "schema": 1,
        "owner": OWNER,
        "kind": "test-prelaunch",
        "run_id": run_id,
        "name": name,
        "image": args.image,
        "image_id": image_id,
        "runner_sha256": runner_digest,
        "payload_path": str(test_payload_path(name)),
        "labels": labels,
        "process": {"pid": os.getpid(), "start_ticks": start_ticks},
    }
    publish_json(test_prelaunch_path(name), prelaunch)
    argv = ["podman", "create", "--name", name]
    for key, value in labels.items():
        argv.extend(("--label", f"{key}={value}"))
    argv.extend(
        (
            *test_runtime_options(args, selection_sha),
            image_id,
            "python3",
            CONTAINER_PAYLOAD,
            "wait-exec",
            "--ready-path",
            CONTAINER_INPUTS,
            "--notify-fifo",
            CONTAINER_NOTIFY_FIFO,
            "--",
            "bash",
            f"{CONTAINER_RUNNER}/entrypoint.sh",
            args.target,
        )
    )
    created = ""
    owner_published = False
    try:
        created = command(
            argv,
            pass_fds=(int(args.lifecycle_lock_descriptor),),
        ).stdout.strip()
        if not SHA256_RE.fullmatch(created):
            raise JobError("podman create returned an invalid container ID")
        recovered_id = prelaunch_container_id(prelaunch)
        if recovered_id != created:
            raise JobError("created container ID does not match prelaunch ownership")
        record = {
            "schema": 4,
            "owner": OWNER,
            "run_id": run_id,
            "name": name,
            "container_id": created,
            "target": args.target,
            "selection": args.selection,
            "selection_sha256": selection_sha,
            "patch_mode": args.patch_mode,
            "payload_path": str(test_payload_path(name)),
            "source": args.source,
            "source_head": args.source_head,
            "source_remote": args.source_remote,
            "workflow_sha256": args.workflow_sha256,
            "runner_sha256": runner_digest,
            "image": args.image,
            "image_id": image_id,
            "image_input_sha256": args.image_input_sha256,
        }
        container_state({key: str(value) for key, value in record.items()})
        publish_record(test_record_path(name), record)
        owner_published = True
        command(
            ["podman", "start", created],
            pass_fds=(int(args.lifecycle_lock_descriptor),),
        )
        send_test_payload(created, args, selection_sha)
        remove_test_prelaunch(name)
    except BaseException:
        owned_id = created if owner_published else prelaunch_container_id(prelaunch)
        removed_ok = owned_id is None
        if owned_id is not None:
            removed_ok = (
                command(["podman", "rm", "--force", owned_id], check=False).returncode
                == 0
            )
        if removed_ok:
            remove_test_payload(name)
            if owner_published:
                test_record_path(name).unlink()
            remove_test_prelaunch(name)
        raise
    print(f"started durable Podman test {name} ({created})")
    return 0


def validate_foreground_args(args: argparse.Namespace) -> tuple[str, str]:
    prepare_state()
    if args.target not in TARGETS | {"run"}:
        raise JobError("invalid foreground test target")
    if args.patch_mode not in {"clean", "tests-only", "patched"}:
        raise JobError("invalid foreground patch mode")
    if not SELECTOR_RE.fullmatch(args.selection):
        raise JobError(f"invalid selection: {args.selection!r}")
    if not COMMIT_RE.fullmatch(args.source) or not COMMIT_RE.fullmatch(args.source_head):
        raise JobError("invalid source identity")
    image_id = image_identity(
        args.image,
        args.image_input_sha256,
        args.workflow_sha256,
        source=args.source,
    )
    selection_sha = selection_digest(args.selection)
    return image_id, selection_sha


def test_foreground(args: argparse.Namespace) -> int:
    """Run one disposable test with its immutable payload on stdin."""
    prepare_state()
    with image_cache_lock():
        return _test_foreground_locked(args)


def _test_foreground_locked(args: argparse.Namespace) -> int:
    image_id, selection_sha = validate_foreground_args(args)
    argv = [
        "podman",
        "run",
        "--rm",
        "--interactive",
        *test_runtime_options(args, selection_sha),
    ]
    if args.target == "run":
        if not os.environ.get("XPRA_TEST_COMMAND"):
            raise JobError("XPRA_TEST_COMMAND is required for the run target")
        argv.extend(("--env", "XPRA_TEST_COMMAND"))
    argv.extend(
        (
            image_id,
            "bash",
            f"{CONTAINER_RUNNER}/entrypoint.sh",
            args.target,
        )
    )
    with test_payload(
        selection=args.selection,
        expected_selection_sha256=selection_sha,
        source_head=args.source_head,
        source_remote=args.source_remote,
    ) as payload:
        entries, lock_descriptor = payload
        if lock_descriptor is None:
            raise JobError("foreground payload has no recovery lock")
        container_payload.stream_to_process(
            argv,
            entries,
            pass_fds=(lock_descriptor,),
        )
    return 0


def test_status(args: argparse.Namespace) -> int:
    prepare_state()
    name = validate_name(args.name)
    owner = test_record_path(name)
    if not owner.exists() and not owner.is_symlink():
        prelaunch = load_test_prelaunch(name)
        container_id = prelaunch_container_id(prelaunch)
        print(
            json.dumps(
                {
                    "active": test_prelaunch_active(prelaunch),
                    "container_id": container_id,
                    "name": name,
                    "phase": "prelaunch",
                    "run_id": prelaunch["run_id"],
                    "state": "container-created" if container_id else "preparing",
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    record = load_test_record(args.name, require_current=False)
    matching_test_prelaunch(record)
    state = container_state(record)
    print(
        json.dumps(
            {
                "container_id": record["container_id"],
                "exit_code": state.get("ExitCode"),
                "finished_at": state.get("FinishedAt"),
                "name": record["name"],
                "state": state.get("Status"),
                "target": record["target"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def test_logs(args: argparse.Namespace) -> int:
    prepare_state()
    name = validate_name(args.name)
    owner = test_record_path(name)
    if not owner.exists() and not owner.is_symlink():
        prelaunch = load_test_prelaunch(name)
        container_id = prelaunch_container_id(prelaunch)
        if container_id is None:
            raise JobError(f"test prelaunch has no container logs yet: {name}")
        return command(
            ["podman", "logs", container_id], check=False, capture=False
        ).returncode
    record = load_test_record(args.name, require_current=False)
    matching_test_prelaunch(record)
    container_state(record)
    return command(
        ["podman", "logs", record["container_id"]], check=False, capture=False
    ).returncode


def resolution_from_log(payload: bytes) -> tuple[bool, str]:
    matches = re.findall(
        rb"(?m)^selection_resolution_sha256=([0-9a-f]{64})$",
        payload,
    )
    if len(matches) != 1:
        return False, ""
    return True, matches[0].decode("ascii")


def test_collect(args: argparse.Namespace) -> int:
    prepare_state()
    name = validate_name(args.name)
    record = load_test_record(name)
    matching_test_prelaunch(record)
    with lifecycle_lock(name):
        require_absent(result_paths(name), "result artifact")
        state = container_state(record)
        container_status = str(state.get("Status", ""))
        if container_status in {"created", "configured", "running", "paused", "stopping"}:
            raise JobError(f"test container is still active: {name}")
        log_result = command(["podman", "logs", record["container_id"]], check=False)
        log_payload = (log_result.stdout + log_result.stderr).encode()
        publish_bytes(log_path(name), log_payload)
        resolution_ok, resolution_sha = resolution_from_log(log_payload)
        exit_code = state.get("ExitCode")
        finished = str(state.get("FinishedAt", ""))
        valid_finished = bool(finished and not finished.startswith("0001-"))
        validation_ok = (
            log_result.returncode == 0
            and container_status == "exited"
            and exit_code == 0
            and valid_finished
            and resolution_ok
        )
        values = {
            "schema": 3,
            "owner": OWNER,
            "run_id": record["run_id"],
            "name": name,
            "result": "success" if validation_ok else "failed",
            "exit_code": exit_code,
            "validation_ok": int(validation_ok),
            "container_present": 1,
            "container_id": record["container_id"],
            "container_status": container_status,
            "container_exit": exit_code,
            "finished": finished,
            "target": record["target"],
            "selection": record["selection"],
            "selection_sha256": record["selection_sha256"],
            "selection_resolution_ok": int(resolution_ok),
            "selection_resolution_sha256": resolution_sha,
            "patch_mode": record["patch_mode"],
            "payload_path": record["payload_path"],
            "source": record["source"],
            "source_head": record["source_head"],
            "source_remote": record["source_remote"],
            "workflow_sha256": record["workflow_sha256"],
            "runner_sha256": record["runner_sha256"],
            "image_input_sha256": record["image_input_sha256"],
            "image": record["image"],
            "expected_image_id": record["image_id"],
            "image_id": record["image_id"],
            "logs_ok": int(log_result.returncode == 0),
            "log_sha256": hashlib.sha256(log_payload).hexdigest(),
        }
        try:
            publish_status(status_path(name), values)
        except BaseException:
            log_path(name).unlink(missing_ok=True)
            raise
        print(f"saved {log_path(name)} and {status_path(name)} (exit {exit_code})")
        return 0 if validation_ok else 1


def test_wait(args: argparse.Namespace) -> int:
    prepare_state()
    record = load_test_record(args.name)
    matching_test_prelaunch(record)
    state = container_state(record)
    if str(state.get("Status", "")) in {
        "created",
        "configured",
        "running",
        "paused",
        "stopping",
    }:
        command(["podman", "wait", record["container_id"]], capture=False)
    return test_collect(args)


def verify_test_evidence(name: str, record: dict[str, str]) -> None:
    status = parse_record(status_path(name))
    ensure_private_regular(log_path(name))
    for key in (
        "owner",
        "run_id",
        "name",
        "target",
        "selection",
        "selection_sha256",
        "patch_mode",
        "payload_path",
        "source",
        "source_head",
        "source_remote",
        "workflow_sha256",
        "runner_sha256",
        "image",
        "image_input_sha256",
        "container_id",
    ):
        if status.get(key) != record.get(key):
            raise JobError(f"collected test evidence mismatch for {key}")
    if status.get("log_sha256") != sha256_file(log_path(name)):
        raise JobError("collected test log digest does not match")
    resolution_ok, resolution_sha = resolution_from_log(log_path(name).read_bytes())
    if status.get("selection_resolution_ok") != str(int(resolution_ok)):
        raise JobError("collected selection resolution status does not match its log")
    if status.get("selection_resolution_sha256") != resolution_sha:
        raise JobError("collected selection resolution digest does not match its log")
    validation_ok = (
        status.get("logs_ok") == "1"
        and status.get("container_present") == "1"
        and status.get("container_id") == record["container_id"]
        and status.get("container_status") == "exited"
        and status.get("container_exit") == "0"
        and status.get("exit_code") == "0"
        and bool(status.get("finished"))
        and not status["finished"].startswith("0001-")
        and resolution_ok
        and status.get("expected_image_id") == record["image_id"]
        and status.get("image_id") == record["image_id"]
    )
    if status.get("validation_ok") != str(int(validation_ok)):
        raise JobError("collected test validation does not match its evidence")
    verify_result_status(status, "test")


def test_remove(args: argparse.Namespace) -> int:
    prepare_state()
    name = validate_name(args.name)
    with lifecycle_lock(name):
        transaction_path = remove_transaction_path(name)
        if transaction_path.exists() or transaction_path.is_symlink():
            transaction = load_remove_transaction(
                name,
                "test-remove",
                test_record_path(name),
            )
            record = transaction["record"]
        else:
            record = load_test_record(name, require_current=False)
            matching_test_prelaunch(record)
            verify_test_evidence(name, record)
            initial_state = container_lifecycle_state(record)
            if initial_state["state"] == "running":
                raise JobError("test job is still running; wait or abort it first")
            if initial_state["state"] not in {"completed", "lost"}:
                raise JobError(
                    f"test job has an unsupported state: {initial_state['state']}"
                )
            transaction = publish_remove_transaction(
                name,
                "test-remove",
                record,
                test_record_path(name),
            )
        verify_test_evidence(name, record)
        state = container_lifecycle_state(record)
        if state["state"] == "running":
            raise JobError("test job is still running; wait or abort it first")
        if state["state"] == "completed":
            command(["podman", "rm", record["container_id"]])
        elif state["state"] != "lost":
            raise JobError(f"test job has an unsupported state: {state['state']}")
        remove_test_payload(name)
        remove_test_prelaunch(name)
        owner_path = test_record_path(name)
        if owner_path.exists() or owner_path.is_symlink():
            ensure_private_regular(owner_path)
            if sha256_file(owner_path) != transaction["owner_sha256"]:
                raise JobError("test removal owner record changed")
            owner_path.unlink()
    print(f"removed owned runtime state for {name}; evidence was retained")
    return 0


def test_abort(args: argparse.Namespace) -> int:
    prepare_state()
    name = validate_name(args.name)
    with lifecycle_lock(name):
        require_absent(result_paths(name), "collected result")
        owner = test_record_path(name)
        if not owner.exists() and not owner.is_symlink():
            prelaunch = load_test_prelaunch(name)
            if test_prelaunch_active(prelaunch):
                raise JobError("test start is still active; retry abort after it exits")
            container_id = prelaunch_container_id(prelaunch)
            if container_id is not None:
                command(["podman", "rm", "--force", container_id])
            remove_test_payload(name)
            remove_test_prelaunch(name)
            print(f"discarded recoverable prelaunch state for {name}")
            return 0
        record = load_test_record(name, require_current=False)
        matching_test_prelaunch(record)
        state = container_lifecycle_state(record)
        if state["state"] == "completed":
            if record["runner_sha256"] == runner_sha256():
                raise JobError("completed test jobs must be collected, not aborted")
        elif state["state"] not in {"running", "lost"}:
            raise JobError(f"test job has an unsupported state: {state['state']}")
        if state["state"] != "lost":
            command(["podman", "rm", "--force", record["container_id"]])
        remove_test_payload(name)
        remove_test_prelaunch(name)
        test_record_path(name).unlink()
        print(f"aborted and removed owned runtime state for {name}")
        return 0


def image_context(name: str) -> Path:
    return IMAGE_BUILD_ROOT / validate_name(name)


def image_owner_path(name: str) -> Path:
    return image_context(name) / "owner.json"


def image_prelaunch_path(name: str) -> Path:
    return IMAGE_BUILD_ROOT / f".{validate_name(name)}.image-prelaunch.json"


def load_image_prelaunch(name: str) -> dict[str, Any]:
    try:
        record = background_job.load_json(image_prelaunch_path(name))
    except background_job.BackgroundJobError as error:
        raise JobError(str(error)) from error
    expected = {
        "schema": 1,
        "owner": IMAGE_OWNER,
        "kind": "image-build-prelaunch",
        "name": name,
    }
    for key, value in expected.items():
        if record.get(key) != value:
            raise JobError(f"image-build prelaunch mismatch for {key}")
    if not UUID4_RE.fullmatch(str(record.get("job_id", ""))):
        raise JobError("image-build prelaunch has an invalid job ID")
    for key in ("input_sha256", "workflow_sha256", "runner_sha256"):
        if not SHA256_RE.fullmatch(str(record.get(key, ""))):
            raise JobError(f"image-build prelaunch has invalid {key}")
    if not COMMIT_RE.fullmatch(str(record.get("source", ""))):
        raise JobError("image-build prelaunch has invalid source")
    if record.get("context") != str(image_context(name)):
        raise JobError("image-build prelaunch has an invalid context path")
    return record


def matching_image_prelaunch(record: dict[str, Any]) -> dict[str, Any] | None:
    name = str(record["name"])
    path = image_prelaunch_path(name)
    if not path.exists() and not path.is_symlink():
        if record.get("schema") == 2:
            return None
        raise JobError("image-build prelaunch record is missing")
    prelaunch = load_image_prelaunch(name)
    for key in (
        "job_id",
        "image",
        "input_sha256",
        "source",
        "workflow_sha256",
        "runner_sha256",
    ):
        if prelaunch.get(key) != record.get(key):
            raise JobError(f"image-build owner and prelaunch differ for {key}")
    return prelaunch


def load_image_record(name: str, *, require_current: bool = True) -> dict[str, Any]:
    context = image_context(name)
    ensure_private_directory(context)
    try:
        record = background_job.load_json(image_owner_path(name))
    except background_job.BackgroundJobError as error:
        raise JobError(str(error)) from error
    expected = {"owner": IMAGE_OWNER, "kind": "image-build", "name": name}
    for key, value in expected.items():
        if record.get(key) != value:
            raise JobError(f"image-build ownership mismatch for {key}")
    if record.get("schema") not in {2, 3}:
        raise JobError("image-build ownership has an unsupported schema")
    for key in ("input_sha256", "workflow_sha256", "runner_sha256"):
        if not SHA256_RE.fullmatch(str(record.get(key, ""))):
            raise JobError(f"image-build ownership has invalid {key}")
    if not COMMIT_RE.fullmatch(str(record.get("source", ""))):
        raise JobError("image-build ownership has invalid source")
    if require_current and record.get("runner_sha256") != runner_sha256():
        raise JobError("image-build runner changed while the job was owned")
    matching_image_prelaunch(record)
    try:
        background_job.process_state(record, require_current=require_current)
    except background_job.BackgroundJobError as error:
        raise JobError(str(error)) from error
    return record


def populate_image_context(destination: Path) -> None:
    for input_name, source in IMAGE_CONTEXT_INPUTS.items():
        if source.is_symlink() or not source.is_file():
            raise JobError(f"image input is unavailable: {source}")
        target = destination / input_name
        shutil.copyfile(source, target)
        target.chmod(
            0o755
            if input_name in {"container_payload.py", "entrypoint.sh", "selection.py"}
            else 0o644
        )


def image_context_entries(context: Path) -> tuple[container_payload.PayloadEntry, ...]:
    return tuple(
        container_payload.PayloadEntry(context / name, PurePosixPath(name))
        for name in IMAGE_CONTEXT_INPUTS
    )


def image_source_entries() -> tuple[container_payload.PayloadEntry, ...]:
    return tuple(
        container_payload.PayloadEntry(source, PurePosixPath(name))
        for name, source in IMAGE_CONTEXT_INPUTS.items()
    )


def image_build_argv(
    args: argparse.Namespace,
    job_id: str,
    *,
    iidfile: str | None = "image.iid",
) -> list[str]:
    command_line = [
        "podman",
        "build",
        "--pull=always",
        "--label",
        "io.xpra.lab.image-builder=true",
        "--label",
        f"io.xpra.lab.image-build-run-id={job_id}",
        "--label",
        f"io.xpra.lab.image-input={args.image_input_sha256}",
        "--label",
        f"io.xpra.lab.source={args.source}",
        "--label",
        f"io.xpra.lab.workflow={args.workflow_sha256}",
        "-t",
        args.image,
        "-f",
        "Containerfile",
        "-",
    ]
    if iidfile is not None:
        command_line[3:3] = ["--iidfile", iidfile]
    return command_line


def built_image_labels(record: dict[str, Any]) -> dict[str, str]:
    """Return the complete downstream label set for one owned build."""
    return {
        "io.xpra.lab.image-builder": "true",
        "io.xpra.lab.image-build-run-id": str(record["job_id"]),
        "io.xpra.lab.image-input": str(record["input_sha256"]),
        "io.xpra.lab.source": str(record["source"]),
        "io.xpra.lab.workflow": str(record["workflow_sha256"]),
    }


def image_ensure(args: argparse.Namespace) -> int:
    """Build the input-keyed, label-verified image for hosted CI."""
    prepare_state()
    with image_cache_lock():
        return _image_ensure_locked(args)


def _image_ensure_locked(args: argparse.Namespace) -> int:
    exists = command(["podman", "image", "exists", args.image], check=False)
    if exists.returncode == 0:
        image_id = image_identity(
            args.image,
            args.image_input_sha256,
            args.workflow_sha256,
            source=args.source,
        )
        print(f"using verified cached image {args.image} ({image_id})")
        return 0
    if exists.returncode != 1:
        raise JobError(f"cannot inspect image name: {args.image}")

    job_id = str(uuid.uuid4())
    container_payload.stream_to_process(
        image_build_argv(args, job_id, iidfile=None),
        image_source_entries(),
        cwd=RUNNER_ROOT,
    )
    image_id = image_identity(
        args.image,
        args.image_input_sha256,
        args.workflow_sha256,
        source=args.source,
        build_run_id=job_id,
    )
    print(f"built and verified CI image {args.image} ({image_id})")
    return 0


def image_cache_remove(args: argparse.Namespace) -> int:
    """Remove only the exact current cache image while no build or use can race."""
    prepare_state()
    with image_cache_lock():
        image_id = removable_image_identity(
            args.image,
            args.image_input_sha256,
            args.workflow_sha256,
        )
        require_image_cache_unleased(args.image, image_id)
        command(["podman", "image", "rm", image_id])
    print(f"removed verified cached image {args.image} ({image_id})")
    return 0


def require_image_cache_unleased(image: str, image_id: str) -> None:
    """Refuse cache deletion while an exact named job still owns the image."""
    for path in sorted(IMAGE_BUILD_ROOT.iterdir()):
        if path.name == ".image-cache.lock":
            continue
        if path.name.startswith(".") and path.name.endswith(".image-prelaunch.json"):
            name = path.name[1 : -len(".image-prelaunch.json")]
            prelaunch = load_image_prelaunch(name)
            if prelaunch["image"] == image:
                raise JobError(
                    f"image cache is still owned by image-build prelaunch {name}"
                )
            continue
        if path.is_symlink() or not path.is_dir():
            raise JobError(f"unexpected image-build cache entry requires review: {path}")
        record = load_image_record(path.name, require_current=False)
        if record["image"] == image:
            raise JobError(f"image cache is still owned by image-build job {path.name}")

    for path in sorted(RUN_ROOT.iterdir()):
        if path.name.endswith(".prelaunch.json"):
            name = path.name[: -len(".prelaunch.json")]
            prelaunch = load_test_prelaunch(name)
            if prelaunch["image_id"] == image_id:
                raise JobError(f"image cache is still owned by test prelaunch {name}")
            continue
        if not path.name.endswith(".owner"):
            continue
        name = path.name[: -len(".owner")]
        record = load_test_record(name, require_current=False)
        if record["image_id"] == image_id:
            raise JobError(f"image cache is still owned by test job {name}")


def image_start(args: argparse.Namespace) -> int:
    prepare_state()
    name = validate_name(args.name)
    with lifecycle_lock(name), image_cache_lock() as cache_descriptor:
        args.image_cache_lock_descriptor = cache_descriptor
        try:
            return _image_start_locked(args)
        finally:
            del args.image_cache_lock_descriptor


def _image_start_locked(args: argparse.Namespace) -> int:
    name = validate_name(args.name)
    exists = command(["podman", "image", "exists", args.image], check=False)
    if exists.returncode == 0:
        raise JobError(f"image already exists and will not be overwritten: {args.image}")
    if exists.returncode != 1:
        raise JobError(f"cannot inspect image name: {args.image}")
    require_absent(
        (
            log_path(name),
            status_path(name),
            image_context(name),
            image_prelaunch_path(name),
            remove_transaction_path(name),
        ),
        "image job artifact",
    )
    context = image_context(name)
    job_id = str(uuid.uuid4())
    prelaunch = {
        "schema": 1,
        "owner": IMAGE_OWNER,
        "kind": "image-build-prelaunch",
        "name": name,
        "job_id": job_id,
        "context": str(context),
        "image": args.image,
        "input_sha256": args.image_input_sha256,
        "source": args.source,
        "workflow_sha256": args.workflow_sha256,
        "runner_sha256": runner_sha256(),
    }
    publish_json(image_prelaunch_path(name), prelaunch)
    context.mkdir(mode=0o700)
    ensure_private_directory(context)
    try:
        populate_image_context(context)
    except BaseException as error:
        if not isinstance(error, background_job.LaunchStateRetained):
            shutil.rmtree(context, ignore_errors=True)
            image_prelaunch_path(name).unlink(missing_ok=True)
        raise
    record = {
        "schema": 3,
        "owner": IMAGE_OWNER,
        "kind": "image-build",
        "name": name,
        "job_id": job_id,
        "created_at": utc_now(),
        "image": args.image,
        "input_sha256": args.image_input_sha256,
        "source": args.source,
        "workflow_sha256": args.workflow_sha256,
        "runner_sha256": prelaunch["runner_sha256"],
    }
    build_argv = image_build_argv(args, job_id)
    stream_argv = [
        sys.executable,
        str(context / "container_payload.py"),
        "send",
    ]
    for input_name in IMAGE_CONTEXT_INPUTS:
        stream_argv.extend(("--entry-json", json.dumps([input_name, input_name])))
    stream_argv.extend(("--", *build_argv))
    argv = [
        "bash",
        "-c",
        'set -e; "$@"; chmod 0600 image.iid',
        "xpra-lab-image-build",
        *stream_argv,
    ]
    try:
        owned = background_job.launch(
            owner_path=image_owner_path(name),
            runtime_log=context / "runtime.log",
            completion_file=context / "completion.json",
            record=record,
            argv=argv,
            cwd=context,
            pass_fds=(int(args.image_cache_lock_descriptor),),
        )
    except BaseException as error:
        if not isinstance(error, background_job.LaunchStateRetained):
            shutil.rmtree(context, ignore_errors=True)
            image_prelaunch_path(name).unlink(missing_ok=True)
        raise
    print(f"started image build {args.image} (pid {owned['process']['pid']})")
    return 0


def image_status(args: argparse.Namespace) -> int:
    prepare_state()
    name = validate_name(args.name)
    owner = image_owner_path(name)
    if not owner.exists() and not owner.is_symlink():
        prelaunch = load_image_prelaunch(name)
        print(
            json.dumps(
                {
                    "image": prelaunch["image"],
                    "name": name,
                    "prelaunch": "owned-input-freeze",
                    "process": {"state": "prelaunch"},
                }
            )
        )
        return 0
    record = load_image_record(name, require_current=False)
    state = background_job.process_state(record, require_current=False)
    exists = command(["podman", "image", "exists", str(record["image"])], check=False)
    print(
        json.dumps(
            {
                "image": record["image"],
                "image_exists": exists.returncode == 0,
                "name": record["name"],
                "process": state,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def image_logs(args: argparse.Namespace) -> int:
    prepare_state()
    record = load_image_record(args.name, require_current=False)
    path = background_job.runtime_log_path(record, require_current=False)
    ensure_private_regular(path)
    sys.stdout.buffer.write(path.read_bytes())
    return 0


def inspect_built_image(
    record: dict[str, Any], *, normalize_iid: bool = False
) -> tuple[bool, str, dict[str, str]]:
    iidfile = image_context(str(record["name"])) / "image.iid"
    if not iidfile.exists() or iidfile.is_symlink():
        return False, "", {}
    if normalize_iid:
        info = iidfile.lstat()
        if not iidfile.is_file() or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise JobError(f"refusing untrusted image ID file: {iidfile}")
        iidfile.chmod(0o600)
    ensure_private_regular(iidfile)
    image_id = iidfile.read_text(encoding="ascii").strip().removeprefix("sha256:")
    if not SHA256_RE.fullmatch(image_id):
        return False, "", {}
    exists = command(["podman", "image", "exists", image_id], check=False)
    if exists.returncode != 0:
        return False, image_id, {}
    item = inspect_json(["podman", "image", "inspect", image_id])
    labels = item.get("Labels")
    if labels is None and isinstance(item.get("Config"), dict):
        labels = item["Config"].get("Labels")
    if not isinstance(labels, dict):
        return False, image_id, {}
    return True, image_id, {str(key): str(value) for key, value in labels.items()}


def image_collect(args: argparse.Namespace) -> int:
    prepare_state()
    name = validate_name(args.name)
    with lifecycle_lock(name), image_cache_lock():
        return _image_collect_locked(args, name)


def _image_collect_locked(args: argparse.Namespace, name: str) -> int:
    record = load_image_record(name)
    require_absent((log_path(name), status_path(name)), "result artifact")
    state = background_job.process_state(record)
    if state["state"] == "running":
        raise JobError(f"image build is still running: {name}")
    if state["state"] != "completed":
        raise JobError(f"image build disappeared without completion: {name}")
    runtime_log = background_job.runtime_log_path(record)
    ensure_private_regular(runtime_log)
    log_payload = runtime_log.read_bytes()
    image_ok, image_id, labels = inspect_built_image(record, normalize_iid=True)
    labels_ok = image_ok and exact_lab_labels(labels, built_image_labels(record))
    exit_code = state["exit_code"]
    finished = str(state.get("finished_at", ""))
    validation_ok = (
        exit_code == 0
        and labels_ok
        and bool(finished)
        and not finished.startswith("0001-")
    )
    values = {
        "schema": 2,
        "owner": IMAGE_OWNER,
        "run_id": record["job_id"],
        "name": name,
        "result": "success" if validation_ok else "failed",
        "exit_code": exit_code,
        "validation_ok": int(validation_ok),
        "image": record["image"],
        "iid_ok": int(bool(image_id)),
        "image_exists": int(image_ok),
        "image_id": image_id,
        "image_builder": labels.get("io.xpra.lab.image-builder", ""),
        "image_input_sha256": labels.get("io.xpra.lab.image-input", ""),
        "source": record["source"],
        "workflow_sha256": record["workflow_sha256"],
        "runner_sha256": record["runner_sha256"],
        "selection_resolution_ok": 0,
        "selection_resolution_sha256": "",
        "logs_ok": 1,
        "log_sha256": hashlib.sha256(log_payload).hexdigest(),
        "finished": finished,
    }
    publish_bytes(log_path(name), log_payload)
    try:
        publish_status(status_path(name), values)
    except BaseException:
        log_path(name).unlink(missing_ok=True)
        raise
    print(f"saved {log_path(name)} and {status_path(name)} (exit {exit_code})")
    return 0 if validation_ok else 1


def image_wait(args: argparse.Namespace) -> int:
    prepare_state()
    record = load_image_record(args.name)
    try:
        background_job.wait_process(record)
    except background_job.BackgroundJobError as error:
        raise JobError(str(error)) from error
    return image_collect(args)


def verify_image_evidence(name: str, record: dict[str, Any]) -> None:
    status = parse_record(status_path(name))
    ensure_private_regular(log_path(name))
    expected = {
        "owner": IMAGE_OWNER,
        "run_id": str(record["job_id"]),
        "name": name,
        "image": str(record["image"]),
        "image_input_sha256": str(record["input_sha256"]),
        "runner_sha256": str(record["runner_sha256"]),
        "source": str(record["source"]),
        "workflow_sha256": str(record["workflow_sha256"]),
    }
    for key, value in expected.items():
        if status.get(key) != value:
            raise JobError(f"collected image evidence mismatch for {key}")
    if status.get("log_sha256") != sha256_file(log_path(name)):
        raise JobError("collected image log digest does not match")
    validation_ok = (
        status.get("exit_code") == "0"
        and status.get("iid_ok") == "1"
        and status.get("image_exists") == "1"
        and SHA256_RE.fullmatch(status.get("image_id", "")) is not None
        and status.get("image_builder") == "true"
        and status.get("logs_ok") == "1"
        and bool(status.get("finished"))
        and not status["finished"].startswith("0001-")
    )
    if status.get("validation_ok") != str(int(validation_ok)):
        raise JobError("collected image validation does not match its evidence")
    verify_result_status(status, "image")


def verify_result_status(status: dict[str, str], kind: str) -> None:
    """Require the published result word to match full validation."""
    validation = status.get("validation_ok")
    if validation not in {"0", "1"}:
        raise JobError(f"collected {kind} evidence has invalid validation status")
    expected = "success" if validation == "1" else "failed"
    if status.get("result") != expected:
        raise JobError(f"collected {kind} result contradicts its validation status")


def image_remove(args: argparse.Namespace) -> int:
    prepare_state()
    name = validate_name(args.name)
    with lifecycle_lock(name):
        transaction_path = remove_transaction_path(name)
        if transaction_path.exists() or transaction_path.is_symlink():
            transaction = load_remove_transaction(
                name,
                "image-build-remove",
                image_owner_path(name),
            )
            record = transaction["record"]
        else:
            record = load_image_record(name, require_current=False)
            verify_image_evidence(name, record)
            state = background_job.process_state(record, require_current=False)
            if state["state"] == "running":
                raise JobError("image build is still running; wait or abort it first")
            if state["state"] not in {"completed", "lost"}:
                raise JobError(
                    f"image build has an unsupported state: {state['state']}"
                )
            transaction = publish_remove_transaction(
                name,
                "image-build-remove",
                record,
                image_owner_path(name),
            )
        verify_image_evidence(name, record)
        context = image_context(name)
        if context.exists() or context.is_symlink():
            if context.is_symlink():
                raise JobError(f"refusing symlinked image context: {context}")
            ensure_private_directory(context)
            owner_path = image_owner_path(name)
            if owner_path.exists() or owner_path.is_symlink():
                ensure_private_regular(owner_path)
                if sha256_file(owner_path) != transaction["owner_sha256"]:
                    raise JobError("image removal owner record changed")
            shutil.rmtree(context)
        prelaunch_path = image_prelaunch_path(name)
        if prelaunch_path.exists() or prelaunch_path.is_symlink():
            prelaunch = load_image_prelaunch(name)
            for key in (
                "job_id",
                "image",
                "input_sha256",
                "source",
                "workflow_sha256",
                "runner_sha256",
            ):
                if prelaunch.get(key) != record.get(key):
                    raise JobError(f"image removal prelaunch differs for {key}")
            prelaunch_path.unlink()
    print(f"removed owned image-build runtime state for {name}; evidence was retained")
    return 0


def image_abort(args: argparse.Namespace) -> int:
    prepare_state()
    name = validate_name(args.name)
    with lifecycle_lock(name):
        if status_path(name).exists() or status_path(name).is_symlink():
            raise JobError(f"image build already has collected evidence: {name}")
        owner = image_owner_path(name)
        prelaunch_path = image_prelaunch_path(name)
        if (
            not owner.exists()
            and not owner.is_symlink()
            and (prelaunch_path.exists() or prelaunch_path.is_symlink())
        ):
            load_image_prelaunch(name)
            context = image_context(name)
            if context.exists() or context.is_symlink():
                if context.is_symlink():
                    raise JobError(f"refusing symlinked image context: {context}")
                ensure_private_directory(context)
                shutil.rmtree(context)
            prelaunch_path.unlink()
            print(f"discarded recoverable image-build prelaunch state for {name}")
            return 0
        record = load_image_record(name, require_current=False)
        partial_log = log_path(name)
        if partial_log.exists() or partial_log.is_symlink():
            ensure_private_regular(partial_log)
        state = background_job.process_state(record, require_current=False)
        if state["state"] == "completed":
            if record.get("runner_sha256") == runner_sha256():
                raise JobError("completed image jobs must be collected, not aborted")
        elif state["state"] == "running":
            try:
                background_job.terminate(record, require_current=False)
            except background_job.BackgroundJobError as error:
                raise JobError(str(error)) from error
        elif state["state"] != "lost":
            raise JobError(f"image job has an unsupported process state: {state['state']}")
        # A running image worker inherits this lock from image-start. Stop it
        # first, then reacquire the same lock before inspecting or deleting the
        # cache entry so image-ensure/cache-remove cannot replace it between
        # validation and removal.
        with image_cache_lock():
            image_ok, image_id, labels = inspect_built_image(record, normalize_iid=True)
            if image_ok:
                if not exact_lab_labels(labels, built_image_labels(record)):
                    raise JobError("refusing image whose build ownership labels do not match")
                command(["podman", "image", "rm", image_id])
            partial_log.unlink(missing_ok=True)
            shutil.rmtree(image_context(name))
            image_prelaunch_path(name).unlink(missing_ok=True)
        print(f"aborted and removed owned image-build runtime state for {name}")
        return 0


def runner_sha(_args: argparse.Namespace) -> int:
    print(runner_sha256())
    return 0


def add_common_image_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--image", required=True)
    parser.add_argument("--image-input-sha256", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--workflow-sha256", required=True)


def add_payload_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--selection", required=True)
    parser.add_argument("--patch-mode", choices=("clean", "tests-only", "patched"), required=True)
    parser.add_argument("--source-head", required=True)
    parser.add_argument("--source-remote", choices=sorted(SOURCE_REMOTES), required=True)
    add_common_image_arguments(parser)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    commands = value.add_subparsers(dest="scope", required=True)
    commands.add_parser("runner-sha").set_defaults(handler=runner_sha)
    source = commands.add_parser("source")
    source_commands = source.add_subparsers(dest="operation", required=True)
    snapshot = source_commands.add_parser("snapshot")
    snapshot.add_argument("--source-host", required=True)
    snapshot.add_argument("--source-ref", required=True)
    snapshot.add_argument("--source-head", required=True)
    snapshot.add_argument("--source-remote", choices=sorted(SOURCE_REMOTES), required=True)
    snapshot.add_argument("--bundle", required=True)
    snapshot.set_defaults(handler=source_snapshot)
    test = commands.add_parser("test")
    test_commands = test.add_subparsers(dest="operation", required=True)
    start = test_commands.add_parser("start")
    start.add_argument("name")
    start.add_argument("--target", choices=sorted(TARGETS), required=True)
    add_payload_arguments(start)
    start.set_defaults(handler=test_start)
    foreground = test_commands.add_parser("foreground")
    foreground.add_argument("--target", choices=sorted(TARGETS | {"run"}), required=True)
    add_payload_arguments(foreground)
    foreground.set_defaults(handler=test_foreground)
    for operation, handler in (
        ("status", test_status),
        ("logs", test_logs),
        ("wait", test_wait),
        ("collect", test_collect),
        ("remove", test_remove),
        ("abort", test_abort),
    ):
        subparser = test_commands.add_parser(operation)
        subparser.add_argument("name")
        subparser.set_defaults(handler=handler)
    image = commands.add_parser("image")
    image_commands = image.add_subparsers(dest="operation", required=True)
    image_ensure_parser = image_commands.add_parser("ensure")
    add_common_image_arguments(image_ensure_parser)
    image_ensure_parser.set_defaults(handler=image_ensure)
    image_cache_remove_parser = image_commands.add_parser("cache-remove")
    add_common_image_arguments(image_cache_remove_parser)
    image_cache_remove_parser.set_defaults(handler=image_cache_remove)
    image_start_parser = image_commands.add_parser("start")
    image_start_parser.add_argument("name")
    add_common_image_arguments(image_start_parser)
    image_start_parser.set_defaults(handler=image_start)
    for operation, handler in (
        ("status", image_status),
        ("logs", image_logs),
        ("wait", image_wait),
        ("collect", image_collect),
        ("remove", image_remove),
        ("abort", image_abort),
    ):
        subparser = image_commands.add_parser(operation)
        subparser.add_argument("name")
        subparser.set_defaults(handler=handler)
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
        OSError,
        json.JSONDecodeError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
