#!/usr/bin/env python3
# Copyright (C) 2026 kogeler
"""Own durable upstream-test containers and image-build processes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RUNNER_ROOT = Path(__file__).resolve().parent
LAB_ROOT = RUNNER_ROOT.parent.parent
PROJECT_ROOT = LAB_ROOT.parent
TOOLS_ROOT = LAB_ROOT / "tools"
sys.path.insert(0, str(TOOLS_ROOT))

import background_job

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
)
IMAGE_CONTEXT_INPUTS = (
    ".containerignore",
    "Containerfile",
    "entrypoint.sh",
    "selection.py",
)


class JobError(RuntimeError):
    """Raised when a durable runner ownership invariant fails."""


def command(
    argv: list[str],
    *,
    check: bool = True,
    capture: bool = True,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        argv,
        check=False,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True,
        cwd=cwd,
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
        STATE_ROOT,
        LOG_ROOT,
        RUN_ROOT,
        IMAGE_BUILD_ROOT,
        SOURCE_ROOT,
    ):
        ensure_private_directory(path, create=True)


def publish_bytes(path: Path, payload: bytes) -> None:
    ensure_private_directory(path.parent)
    if path.exists() or path.is_symlink():
        raise JobError(f"refusing to overwrite existing artifact: {path}")
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    except FileExistsError as error:
        raise JobError(f"artifact publication raced: {path}") from error
    finally:
        temporary.unlink(missing_ok=True)


def publish_status(path: Path, values: dict[str, Any]) -> None:
    payload = "".join(f"{key}={value}\n" for key, value in values.items()).encode()
    publish_bytes(path, payload)


def publish_record(path: Path, values: dict[str, Any]) -> None:
    publish_status(path, values)


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


def log_path(name: str) -> Path:
    return LOG_ROOT / f"{name}.log"


def status_path(name: str) -> Path:
    return LOG_ROOT / f"{name}.status"


def resolution_path(name: str) -> Path:
    return LOG_ROOT / f"{name}.selection-resolution.json"


def resolution_digest_path(name: str) -> Path:
    return LOG_ROOT / f"{name}.selection-resolution.sha256"


def result_paths(name: str) -> tuple[Path, ...]:
    return (
        log_path(name),
        status_path(name),
        resolution_path(name),
        resolution_digest_path(name),
    )


def require_absent(paths: tuple[Path, ...], description: str) -> None:
    for path in paths:
        if path.exists() or path.is_symlink():
            raise JobError(f"{description} already exists: {path}")


def selection_digest(selection: str) -> str:
    result = command(
        [
            sys.executable,
            str(RUNNER_ROOT / "selection.py"),
            "--lab-root",
            str(LAB_ROOT),
            "--selection",
            selection,
            "digest",
        ]
    ).stdout.strip()
    if not SHA256_RE.fullmatch(result):
        raise JobError("selection resolver returned an invalid digest")
    return result


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
    source: str | None = None,
    build_run_id: str | None = None,
) -> str:
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
        "io.xpra.lab.workflow": workflow,
    }
    if source is not None:
        expected["io.xpra.lab.source"] = source
    if build_run_id is not None:
        expected["io.xpra.lab.image-build-run-id"] = build_run_id
    if any(str(labels.get(key, "")) != value for key, value in expected.items()):
        raise JobError(f"image ownership labels do not match: {image}")
    return image_id


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
        "run_id",
        "runner_sha256",
        "schema",
        "selection",
        "selection_sha256",
        "source",
        "target",
        "workflow_sha256",
    }
    if set(record) != required:
        raise JobError("background ownership record fields are inconsistent")
    if record["schema"] != "2" or record["owner"] != OWNER or record["name"] != name:
        raise JobError("background ownership record identity is inconsistent")
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
    if require_current and record["runner_sha256"] != runner_sha256():
        raise JobError("background runner changed while the job was owned")
    return record


def container_state(record: dict[str, str]) -> dict[str, Any]:
    item = inspect_json(["podman", "container", "inspect", record["container_id"]])
    config = item.get("Config")
    state = item.get("State")
    labels = config.get("Labels") if isinstance(config, dict) else None
    if not isinstance(labels, dict) or not isinstance(state, dict):
        raise JobError("owned container inspection is incomplete")
    expected = {
        "io.xpra.lab.upstream-test": "true",
        "io.xpra.lab.owner": record["owner"],
        "io.xpra.lab.run-id": record["run_id"],
        "io.xpra.lab.target": record["target"],
        "io.xpra.lab.selection": record["selection"],
        "io.xpra.lab.selection-sha256": record["selection_sha256"],
        "io.xpra.lab.patch-mode": record["patch_mode"],
        "io.xpra.lab.source": record["source"],
        "io.xpra.lab.workflow": record["workflow_sha256"],
        "io.xpra.lab.runner": record["runner_sha256"],
        "io.xpra.lab.image-id": record["image_id"],
        "io.xpra.lab.image-input": record["image_input_sha256"],
    }
    if any(str(labels.get(key, "")) != value for key, value in expected.items()):
        raise JobError("refusing container whose ownership labels do not match")
    actual_id = str(item.get("Id", ""))
    image_id = str(item.get("Image", "")).removeprefix("sha256:")
    actual_name = str(item.get("Name", "")).lstrip("/")
    if actual_id != record["container_id"] or image_id != record["image_id"]:
        raise JobError("owned container immutable identity does not match")
    if actual_name != record["name"]:
        raise JobError("owned container name does not match")
    return state


def test_start(args: argparse.Namespace) -> int:
    prepare_state()
    name = validate_name(args.name)
    if args.target not in TARGETS or args.patch_mode not in {"clean", "tests-only", "patched"}:
        raise JobError("invalid background test options")
    if not SELECTOR_RE.fullmatch(args.selection):
        raise JobError(f"invalid selection: {args.selection!r}")
    require_absent((*result_paths(name), test_record_path(name)), "job artifact")
    exists = command(["podman", "container", "exists", name], check=False)
    if exists.returncode == 0:
        raise JobError(f"container already exists: {name}")
    if exists.returncode != 1:
        raise JobError(f"cannot check container name: {name}")
    image_id = image_identity(args.image, args.image_input_sha256, args.workflow_sha256)
    selection_sha = selection_digest(args.selection)
    run_id = str(uuid.uuid4())
    source_bundle = source_bundle_path(args.source, args.source_remote)
    source_ref = f"refs/remotes/{args.source_remote}/master"
    ensure_private_regular(source_bundle)
    labels = {
        "io.xpra.lab.upstream-test": "true",
        "io.xpra.lab.owner": OWNER,
        "io.xpra.lab.run-id": run_id,
        "io.xpra.lab.target": args.target,
        "io.xpra.lab.selection": args.selection,
        "io.xpra.lab.selection-sha256": selection_sha,
        "io.xpra.lab.patch-mode": args.patch_mode,
        "io.xpra.lab.source": args.source,
        "io.xpra.lab.workflow": args.workflow_sha256,
        "io.xpra.lab.runner": runner_sha256(),
        "io.xpra.lab.image-id": image_id,
        "io.xpra.lab.image-input": args.image_input_sha256,
    }
    argv = ["podman", "create", "--name", name]
    for key, value in labels.items():
        argv.extend(("--label", f"{key}={value}"))
    argv.extend(
        (
            "--userns",
            "keep-id:uid=1000,gid=1000",
            "--user",
            "1000:1000",
            "--env",
            f"XPRA_EXPECTED_SOURCE_COMMIT={args.source}",
            "--env",
            f"XPRA_EXPECTED_SOURCE_HEAD={args.source}",
            "--env",
            f"XPRA_EXPECTED_SOURCE_REF={source_ref}",
            "--env",
            f"XPRA_EXPECTED_WORKFLOW_SHA={args.workflow_sha256}",
            "--env",
            f"XPRA_LAB_SELECTION={args.selection}",
            "--env",
            f"XPRA_PATCH_MODE={args.patch_mode}",
            "--env",
            f"XPRA_EXPECTED_SELECTION_SHA={selection_sha}",
            "--mount",
            f"type=bind,src={source_bundle},dst=/source.bundle,ro",
            "--mount",
            f"type=bind,src={LAB_ROOT / 'cases'},dst=/lab-host/cases,ro",
            "--mount",
            f"type=bind,src={LAB_ROOT / 'stacks'},dst=/lab-host/stacks,ro",
            "--volume",
            f"{CCACHE_VOLUME}:/home/ubuntu/.cache/ccache:U",
            image_id,
            "bash",
            "/opt/xpra-lab-upstream-tests/entrypoint.sh",
            args.target,
        )
    )
    created = command(argv).stdout.strip()
    if not SHA256_RE.fullmatch(created):
        command(["podman", "rm", "--force", name], check=False)
        raise JobError("podman create returned an invalid container ID")
    record = {
        "schema": 2,
        "owner": OWNER,
        "run_id": run_id,
        "name": name,
        "container_id": created,
        "target": args.target,
        "selection": args.selection,
        "selection_sha256": selection_sha,
        "patch_mode": args.patch_mode,
        "source": args.source,
        "workflow_sha256": args.workflow_sha256,
        "runner_sha256": runner_sha256(),
        "image": args.image,
        "image_id": image_id,
        "image_input_sha256": args.image_input_sha256,
    }
    try:
        container_state({key: str(value) for key, value in record.items()})
        publish_record(test_record_path(name), record)
        command(["podman", "start", created])
    except BaseException:
        test_record_path(name).unlink(missing_ok=True)
        command(["podman", "rm", "--force", created], check=False)
        raise
    print(f"started durable Podman test {name} ({created})")
    return 0


def test_status(args: argparse.Namespace) -> int:
    prepare_state()
    record = load_test_record(args.name, require_current=False)
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
    record = load_test_record(args.name, require_current=False)
    container_state(record)
    return command(
        ["podman", "logs", record["container_id"]], check=False, capture=False
    ).returncode


def copy_resolution(record: dict[str, str], name: str) -> tuple[bool, str]:
    resolution = resolution_path(name)
    digest_file = resolution_digest_path(name)
    temporary_resolution = resolution.with_name(f".{resolution.name}.{uuid.uuid4().hex}")
    temporary_digest = digest_file.with_name(f".{digest_file.name}.{uuid.uuid4().hex}")
    try:
        for remote, local in (
            ("/work/inputs/selection-resolution.json", temporary_resolution),
            ("/work/inputs/selection-resolution.sha256", temporary_digest),
        ):
            result = command(
                ["podman", "cp", f"{record['container_id']}:{remote}", str(local)],
                check=False,
            )
            if result.returncode:
                return False, ""
            local.chmod(0o600)
            ensure_private_regular(local)
        verified = command(
            [
                sys.executable,
                str(RUNNER_ROOT / "selection.py"),
                "--lab-root",
                str(LAB_ROOT),
                "--selection",
                record["selection"],
                "verify-resolution",
                "--resolution",
                str(temporary_resolution),
                "--digest-file",
                str(temporary_digest),
                "--source-commit",
                record["source"],
                "--selection-sha256",
                record["selection_sha256"],
            ],
            check=False,
        )
        value = verified.stdout.strip()
        if verified.returncode or not SHA256_RE.fullmatch(value):
            return False, ""
        os.link(temporary_resolution, resolution)
        os.link(temporary_digest, digest_file)
        return True, value
    finally:
        temporary_resolution.unlink(missing_ok=True)
        temporary_digest.unlink(missing_ok=True)


def test_collect(args: argparse.Namespace) -> int:
    prepare_state()
    name = validate_name(args.name)
    record = load_test_record(name)
    lock = LOG_ROOT / f".{name}.collect.lock"
    try:
        lock.mkdir()
    except FileExistsError as error:
        raise JobError(f"collection or abort is already active: {name}") from error
    try:
        require_absent(result_paths(name), "result artifact")
        state = container_state(record)
        container_status = str(state.get("Status", ""))
        if container_status in {"created", "configured", "running", "paused", "stopping"}:
            raise JobError(f"test container is still active: {name}")
        log_result = command(["podman", "logs", record["container_id"]], check=False)
        log_payload = (log_result.stdout + log_result.stderr).encode()
        publish_bytes(log_path(name), log_payload)
        resolution_ok, resolution_sha = copy_resolution(record, name)
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
            "schema": 2,
            "owner": OWNER,
            "run_id": record["run_id"],
            "name": name,
            "result": "success" if exit_code == 0 else "failed",
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
            "source": record["source"],
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
            resolution_path(name).unlink(missing_ok=True)
            resolution_digest_path(name).unlink(missing_ok=True)
            raise
        print(f"saved {log_path(name)} and {status_path(name)} (exit {exit_code})")
        return 0 if validation_ok else 1
    finally:
        lock.rmdir()


def test_wait(args: argparse.Namespace) -> int:
    prepare_state()
    record = load_test_record(args.name)
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
        "source",
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
    if status.get("selection_resolution_ok") == "1":
        ensure_private_regular(resolution_path(name))
        ensure_private_regular(resolution_digest_path(name))
        if status.get("selection_resolution_sha256") != resolution_digest_path(name).read_text(
            encoding="ascii"
        ).strip():
            raise JobError("collected selection resolution digest does not match")
    elif resolution_path(name).exists() or resolution_digest_path(name).exists():
        raise JobError("unexpected collected selection resolution")


def test_remove(args: argparse.Namespace) -> int:
    prepare_state()
    name = validate_name(args.name)
    record = load_test_record(name, require_current=False)
    verify_test_evidence(name, record)
    state = container_state(record)
    if str(state.get("Status")) in {"running", "paused", "stopping"}:
        raise JobError("test job is still running; wait or abort it first")
    command(["podman", "rm", record["container_id"]])
    test_record_path(name).unlink()
    print(f"removed owned runtime state for {name}; evidence was retained")
    return 0


def test_abort(args: argparse.Namespace) -> int:
    prepare_state()
    name = validate_name(args.name)
    record = load_test_record(name, require_current=False)
    lock = LOG_ROOT / f".{name}.collect.lock"
    try:
        lock.mkdir()
    except FileExistsError as error:
        raise JobError(f"collection or abort is already active: {name}") from error
    try:
        require_absent(result_paths(name), "collected result")
        container_state(record)
        command(["podman", "rm", "--force", record["container_id"]])
        test_record_path(name).unlink()
        print(f"aborted and removed owned runtime state for {name}")
        return 0
    finally:
        lock.rmdir()


def image_context(name: str) -> Path:
    return IMAGE_BUILD_ROOT / validate_name(name)


def image_owner_path(name: str) -> Path:
    return image_context(name) / "owner.json"


def load_image_record(name: str, *, require_current: bool = True) -> dict[str, Any]:
    context = image_context(name)
    ensure_private_directory(context)
    try:
        record = background_job.load_json(image_owner_path(name))
    except background_job.BackgroundJobError as error:
        raise JobError(str(error)) from error
    expected = {"schema": 2, "owner": IMAGE_OWNER, "kind": "image-build", "name": name}
    for key, value in expected.items():
        if record.get(key) != value:
            raise JobError(f"image-build ownership mismatch for {key}")
    for key in ("input_sha256", "workflow_sha256", "runner_sha256"):
        if not SHA256_RE.fullmatch(str(record.get(key, ""))):
            raise JobError(f"image-build ownership has invalid {key}")
    if not COMMIT_RE.fullmatch(str(record.get("source", ""))):
        raise JobError("image-build ownership has invalid source")
    if require_current and record.get("runner_sha256") != runner_sha256():
        raise JobError("image-build runner changed while the job was owned")
    try:
        background_job.process_state(record, require_current=require_current)
    except background_job.BackgroundJobError as error:
        raise JobError(str(error)) from error
    return record


def populate_image_context(destination: Path) -> None:
    for input_name in IMAGE_CONTEXT_INPUTS:
        source = RUNNER_ROOT / input_name
        if source.is_symlink() or not source.is_file():
            raise JobError(f"image input is unavailable: {source}")
        target = destination / input_name
        shutil.copyfile(source, target)
        target.chmod(0o755 if input_name in {"entrypoint.sh", "selection.py"} else 0o644)


def image_build_argv(args: argparse.Namespace, job_id: str) -> list[str]:
    return [
        "podman",
        "build",
        "--pull=always",
        "--iidfile",
        "image.iid",
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
        ".",
    ]


def image_ensure(args: argparse.Namespace) -> int:
    """Build the content-addressed image in the foreground for hosted CI."""
    prepare_state()
    exists = command(["podman", "image", "exists", args.image], check=False)
    if exists.returncode == 0:
        image_id = image_identity(
            args.image,
            args.image_input_sha256,
            args.workflow_sha256,
        )
        print(f"using verified cached image {args.image} ({image_id})")
        return 0
    if exists.returncode != 1:
        raise JobError(f"cannot inspect image name: {args.image}")

    context = Path(tempfile.mkdtemp(prefix=".ci-image.", dir=IMAGE_BUILD_ROOT))
    context.chmod(0o700)
    job_id = str(uuid.uuid4())
    try:
        populate_image_context(context)
        command(image_build_argv(args, job_id), capture=False, check=True, cwd=context)
        image_id = image_identity(
            args.image,
            args.image_input_sha256,
            args.workflow_sha256,
            source=args.source,
            build_run_id=job_id,
        )
    finally:
        shutil.rmtree(context, ignore_errors=True)
    print(f"built and verified CI image {args.image} ({image_id})")
    return 0


def image_start(args: argparse.Namespace) -> int:
    prepare_state()
    name = validate_name(args.name)
    exists = command(["podman", "image", "exists", args.image], check=False)
    if exists.returncode == 0:
        raise JobError(f"image already exists and will not be overwritten: {args.image}")
    if exists.returncode != 1:
        raise JobError(f"cannot inspect image name: {args.image}")
    require_absent((log_path(name), status_path(name), image_context(name)), "image job artifact")
    temporary = Path(tempfile.mkdtemp(prefix=f".{name}.", dir=IMAGE_BUILD_ROOT))
    temporary.chmod(0o700)
    try:
        populate_image_context(temporary)
        temporary.rename(image_context(name))
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    context = image_context(name)
    job_id = str(uuid.uuid4())
    record = {
        "schema": 2,
        "owner": IMAGE_OWNER,
        "kind": "image-build",
        "name": name,
        "job_id": job_id,
        "created_at": utc_now(),
        "image": args.image,
        "input_sha256": args.image_input_sha256,
        "source": args.source,
        "workflow_sha256": args.workflow_sha256,
        "runner_sha256": runner_sha256(),
    }
    build_argv = image_build_argv(args, job_id)
    argv = [
        "bash",
        "-c",
        'set -e; "$@"; chmod 0600 image.iid',
        "xpra-lab-image-build",
        *build_argv,
    ]
    try:
        owned = background_job.launch(
            owner_path=image_owner_path(name),
            runtime_log=context / "runtime.log",
            completion_file=context / "completion.json",
            record=record,
            argv=argv,
            cwd=context,
        )
    except BaseException:
        shutil.rmtree(context, ignore_errors=True)
        raise
    print(f"started image build {args.image} (pid {owned['process']['pid']})")
    return 0


def image_status(args: argparse.Namespace) -> int:
    prepare_state()
    record = load_image_record(args.name, require_current=False)
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
    record = load_image_record(name)
    lock = LOG_ROOT / f".{name}.collect.lock"
    try:
        lock.mkdir()
    except FileExistsError as error:
        raise JobError(f"collection or abort is already active: {name}") from error
    try:
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
        expected_labels = {
            "io.xpra.lab.image-builder": "true",
            "io.xpra.lab.image-build-run-id": str(record["job_id"]),
            "io.xpra.lab.image-input": str(record["input_sha256"]),
            "io.xpra.lab.source": str(record["source"]),
            "io.xpra.lab.workflow": str(record["workflow_sha256"]),
        }
        labels_ok = image_ok and all(labels.get(key) == value for key, value in expected_labels.items())
        exit_code = state["exit_code"]
        validation_ok = exit_code == 0 and labels_ok
        values = {
            "schema": 2,
            "owner": IMAGE_OWNER,
            "run_id": record["job_id"],
            "name": name,
            "result": "success" if exit_code == 0 else "failed",
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
            "finished": state.get("finished_at", ""),
        }
        publish_bytes(log_path(name), log_payload)
        try:
            publish_status(status_path(name), values)
        except BaseException:
            log_path(name).unlink(missing_ok=True)
            raise
        print(f"saved {log_path(name)} and {status_path(name)} (exit {exit_code})")
        return 0 if validation_ok else 1
    finally:
        lock.rmdir()


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
        "runner_sha256": str(record["runner_sha256"]),
    }
    for key, value in expected.items():
        if status.get(key) != value:
            raise JobError(f"collected image evidence mismatch for {key}")
    if status.get("log_sha256") != sha256_file(log_path(name)):
        raise JobError("collected image log digest does not match")


def image_remove(args: argparse.Namespace) -> int:
    prepare_state()
    name = validate_name(args.name)
    record = load_image_record(name, require_current=False)
    verify_image_evidence(name, record)
    state = background_job.process_state(record, require_current=False)
    if state["state"] == "running":
        raise JobError("image build is still running; wait or abort it first")
    shutil.rmtree(image_context(name))
    print(f"removed owned image-build runtime state for {name}; evidence was retained")
    return 0


def image_abort(args: argparse.Namespace) -> int:
    prepare_state()
    name = validate_name(args.name)
    record = load_image_record(name, require_current=False)
    lock = LOG_ROOT / f".{name}.collect.lock"
    try:
        lock.mkdir()
    except FileExistsError as error:
        raise JobError(f"collection or abort is already active: {name}") from error
    try:
        if status_path(name).exists() or status_path(name).is_symlink():
            raise JobError(f"image build already has collected evidence: {name}")
        partial_log = log_path(name)
        if partial_log.exists() or partial_log.is_symlink():
            ensure_private_regular(partial_log)
        try:
            background_job.terminate(record, require_current=False)
        except background_job.BackgroundJobError as error:
            raise JobError(str(error)) from error
        image_ok, image_id, labels = inspect_built_image(record, normalize_iid=True)
        if image_ok:
            if labels.get("io.xpra.lab.image-build-run-id") != record["job_id"]:
                raise JobError("refusing image whose build ownership label does not match")
            command(["podman", "image", "rm", image_id])
        partial_log.unlink(missing_ok=True)
        shutil.rmtree(image_context(name))
        print(f"aborted and removed owned image-build runtime state for {name}")
        return 0
    finally:
        lock.rmdir()


def runner_sha(_args: argparse.Namespace) -> int:
    print(runner_sha256())
    return 0


def add_common_image_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--image", required=True)
    parser.add_argument("--image-input-sha256", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--workflow-sha256", required=True)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    commands = value.add_subparsers(dest="scope", required=True)
    commands.add_parser("runner-sha").set_defaults(handler=runner_sha)
    test = commands.add_parser("test")
    test_commands = test.add_subparsers(dest="operation", required=True)
    start = test_commands.add_parser("start")
    start.add_argument("name")
    start.add_argument("--target", choices=sorted(TARGETS), required=True)
    start.add_argument("--selection", required=True)
    start.add_argument("--patch-mode", choices=("clean", "tests-only", "patched"), required=True)
    start.add_argument("--source-remote", choices=sorted(SOURCE_REMOTES), required=True)
    add_common_image_arguments(start)
    start.set_defaults(handler=test_start)
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
        OSError,
        json.JSONDecodeError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
