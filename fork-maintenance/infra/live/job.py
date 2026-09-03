#!/usr/bin/env python3
# Copyright (C) 2026 kogeler
"""Supervise durable Xpra live runs with owned local processes and Podman labels."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import live_config
from profiles import (
    ALPHA_SCENARIOS,
    APPLICATIONS,
    DEFAULT_NETWORK_PROFILE,
    H264_ACCEPTANCE_POLICIES,
    H264_CLIENT_POLICIES,
    LIFECYCLES,
    NETWORK_PROFILES,
    ProfileError,
    validate_profile,
)

INFRA_ROOT = Path(__file__).resolve().parent
MAINTENANCE_ROOT = INFRA_ROOT.parent.parent
PROJECT_ROOT = MAINTENANCE_ROOT.parent
TOOLS_ROOT = MAINTENANCE_ROOT / "tools"
sys.path.insert(0, str(TOOLS_ROOT))

import background_job
import container_payload
import podman_policy

live_run: Any | None = None

RUNNER = INFRA_ROOT / "run.py"
SUPERVISOR = Path(__file__).resolve()
BACKGROUND_SUPERVISOR = TOOLS_ROOT / "background_job.py"
PAYLOAD_HELPER = TOOLS_ROOT / "container_payload.py"
PODMAN_POLICY = TOOLS_ROOT / "podman_policy.py"
LIVE_CONFIG_MODULE = INFRA_ROOT / "live_config.py"
NETWORK_PROFILES_CONFIG = MAINTENANCE_ROOT / "profiles.yml"
LIVE_CLI_CONFIG = MAINTENANCE_ROOT / "live-cli.yml"
SELECTION_TOOL = MAINTENANCE_ROOT / "infra" / "upstream-tests" / "selection.py"
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
ARTIFACT_ROOT = PROJECT_ROOT / ".artifacts"
STATE_ROOT = ARTIFACT_ROOT / "fork-maintenance"
JOB_ROOT = STATE_ROOT / "jobs" / "live"
RESULT_ROOT = STATE_ROOT / "live-results"
VENV_ROOT = STATE_ROOT / "venvs"
REQUIREMENTS = INFRA_ROOT / "requirements.txt"
PILLOW_VERSION = "12.1.1"
OWNER = "xpra-fork-maintenance-live-job"
RETIRED_RECORD_POLICIES = {"mixed-hardware"}
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
SELECTOR_RE = re.compile(r"(?:cases|stacks)/[a-z0-9]+(?:-[a-z0-9]+)*")
MAINTENANCE_LABEL_PREFIX = "io.xpra.fork-maintenance."


class JobError(RuntimeError):
    """Raised when a durable live-job invariant is not satisfied."""


def live_runner_module() -> Any:
    """Return the Pillow-dependent runner after the live environment exists."""
    global live_run
    if live_run is None:
        try:
            live_run = importlib.import_module("run")
        except ImportError as error:
            raise JobError(
                "the live analysis environment is unavailable; run live-venv first"
            ) from error
    return live_run


def command(
    argv: list[str],
    *,
    check: bool = True,
    capture: bool = True,
    pass_fds: tuple[int, ...] = (),
) -> subprocess.CompletedProcess[str]:
    try:
        podman_policy.validate_podman_argv(argv)
    except podman_policy.PodmanPolicyError as error:
        raise JobError(str(error)) from error
    result = subprocess.run(
        argv,
        check=False,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True,
        pass_fds=pass_fds,
    )
    if check and result.returncode:
        details = ""
        if capture:
            details = f"\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        raise JobError(f"command failed ({result.returncode}): {argv!r}{details}")
    return result


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def harness_sha256() -> str:
    digest = hashlib.sha256()
    for path in HARNESS_INPUTS:
        if path.is_symlink() or not path.is_file():
            raise JobError(f"live harness input is unavailable: {path}")
        digest.update(path.relative_to(MAINTENANCE_ROOT).as_posix().encode())
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256_file(path)))
        digest.update(b"\0")
    return digest.hexdigest()


def validate_name(value: str, label: str = "run name") -> str:
    if not NAME_RE.fullmatch(value):
        raise JobError(f"invalid {label}: {value!r}")
    return value


def record_path(run: str) -> Path:
    return JOB_ROOT / f"{run}.owner.json"


def runtime_log_path(run: str) -> Path:
    return JOB_ROOT / f"{run}.runtime"


def completion_path(run: str) -> Path:
    return JOB_ROOT / f"{run}.completion.json"


def log_path(run: str) -> Path:
    return JOB_ROOT / f"{run}.log"


def status_path(run: str) -> Path:
    return JOB_ROOT / f"{run}.status.json"


def remove_transaction_path(run: str) -> Path:
    return JOB_ROOT / f"{validate_name(run)}.remove.json"


def result_path(run: str) -> Path:
    return RESULT_ROOT / run / "report.json"


def freeze_record_path(run: str) -> Path:
    return JOB_ROOT / f"{run}.freeze.json"


def freeze_prelaunch_path(run: str) -> Path:
    return JOB_ROOT / f"{validate_name(run)}.freeze-prelaunch.json"


def freeze_staging_path(run: str, job_id: str) -> Path:
    return RESULT_ROOT / f".{run}.freeze-{job_id}"


def freeze_runtime_log_path(run: str) -> Path:
    return JOB_ROOT / f"{run}.freeze.runtime"


def freeze_completion_path(run: str) -> Path:
    return JOB_ROOT / f"{run}.freeze.completion.json"


def freeze_result_path(run: str) -> Path:
    return JOB_ROOT / f"{run}.freeze-result.json"


def freeze_abort_transaction_path(run: str) -> Path:
    return JOB_ROOT / f"{validate_name(run)}.freeze-abort.json"


def freeze_abort_staging_path(run: str, key: str) -> Path:
    if key not in {"staging", "result"}:
        raise JobError(f"invalid live input-freeze abort directory key: {key}")
    return RESULT_ROOT / f".{validate_name(run)}.freeze-abort-{key}"


def ensure_private_regular(path: Path) -> None:
    try:
        background_job.ensure_private_regular(path)
    except background_job.BackgroundJobError as error:
        raise JobError(str(error)) from error


def ensure_private_directory(path: Path, *, create: bool = False) -> None:
    try:
        background_job.ensure_private_directory(path, create=create)
    except background_job.BackgroundJobError as error:
        raise JobError(str(error)) from error


def ensure_owned_directory(path: Path, *, create: bool = False) -> None:
    if path.is_symlink():
        raise JobError(f"directory must not be a symlink: {path}")
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    try:
        info = path.lstat()
    except OSError as error:
        raise JobError(f"directory is unavailable: {path}") from error
    if not path.is_dir() or info.st_uid != os.getuid():
        raise JobError(f"directory is not owned by this user: {path}")
    if info.st_mode & 0o022:
        raise JobError(f"directory is writable by another user: {path}")


def prepare_private_state() -> None:
    project = PROJECT_ROOT.lstat()
    if PROJECT_ROOT.is_symlink() or not PROJECT_ROOT.is_dir():
        raise JobError(f"project root is not a real directory: {PROJECT_ROOT}")
    if project.st_uid != os.getuid():
        raise JobError(f"project root is not owned by this user: {PROJECT_ROOT}")
    ensure_owned_directory(ARTIFACT_ROOT, create=True)
    for path in (
        STATE_ROOT,
        STATE_ROOT / "jobs",
        JOB_ROOT,
        RESULT_ROOT,
        VENV_ROOT,
    ):
        ensure_private_directory(path, create=True)


@contextmanager
def lifecycle_lock(run: str) -> Any:
    """Serialize lifecycle transitions with a retained, crash-releasing lock."""
    validate_name(run)
    path = JOB_ROOT / ".lifecycle.lock"
    ensure_private_directory(path.parent, create=True)
    try:
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as error:
        raise JobError(f"cannot open live lifecycle lock {path}: {error}") from error
    try:
        os.fchmod(descriptor, 0o600)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or info.st_mode & 0o177
        ):
            raise JobError(f"unsafe live lifecycle lock: {path}")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise JobError(f"a live lifecycle transition is already active: {run}") from error
        yield descriptor
    finally:
        os.close(descriptor)


def environment_path() -> Path:
    if REQUIREMENTS.is_symlink() or not REQUIREMENTS.is_file():
        raise JobError(f"live requirements lock is unavailable: {REQUIREMENTS}")
    identity = hashlib.sha256()
    identity.update(REQUIREMENTS.read_bytes())
    identity.update(b"\0")
    identity.update(sys.version.encode())
    return VENV_ROOT / f"live-{identity.hexdigest()[:16]}"


def environment_partial_path() -> Path:
    return VENV_ROOT / ".environment.partial"


def environment_partial_marker_path() -> Path:
    return VENV_ROOT / ".environment.partial.owner.json"


@contextmanager
def environment_lock() -> Any:
    path = VENV_ROOT / ".environment.lock"
    ensure_private_directory(path.parent, create=True)
    try:
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as error:
        raise JobError(f"cannot open live environment lock {path}: {error}") from error
    try:
        os.fchmod(descriptor, 0o600)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or info.st_mode & 0o177
        ):
            raise JobError(f"unsafe live environment lock: {path}")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise JobError("live environment creation is already active") from error
        yield descriptor
    finally:
        os.close(descriptor)


def load_environment_partial_marker() -> dict[str, Any]:
    marker_path = environment_partial_marker_path()
    try:
        record = background_job.load_json(marker_path)
    except background_job.BackgroundJobError as error:
        raise JobError(str(error)) from error
    expected = {
        "schema": 1,
        "owner": OWNER,
        "kind": "live-environment-partial",
        "partial": str(environment_partial_path()),
    }
    for key, value in expected.items():
        if record.get(key) != value:
            raise JobError(f"live environment partial owner mismatch for {key}")
    destination = Path(str(record.get("destination", "")))
    if (
        not destination.is_absolute()
        or destination.parent != VENV_ROOT
        or re.fullmatch(r"live-[0-9a-f]{16}", destination.name) is None
    ):
        raise JobError("live environment partial owner has an invalid destination")
    if not SHA256_RE.fullmatch(str(record.get("requirements_sha256", ""))):
        raise JobError("live environment partial owner has an invalid requirements digest")
    python_version = record.get("python_version")
    if not isinstance(python_version, str) or not python_version:
        raise JobError("live environment partial owner has an invalid Python version")
    return record


def recover_environment_partial() -> None:
    temporary = environment_partial_path()
    marker_path = environment_partial_marker_path()
    temporary_present = temporary.exists() or temporary.is_symlink()
    marker_present = marker_path.exists() or marker_path.is_symlink()
    if not temporary_present and not marker_present:
        return
    if not marker_present:
        raise JobError("live environment partial has no ownership marker")
    load_environment_partial_marker()
    if temporary_present:
        if temporary.is_symlink():
            raise JobError(f"live environment partial is a symlink: {temporary}")
        ensure_private_directory(temporary)
        shutil.rmtree(temporary)
    marker_path.unlink()


def validate_environment(path: Path) -> None:
    ensure_private_directory(path)
    python = path / "bin" / "python"
    if not python.exists() or not python.resolve().is_file():
        raise JobError(f"live Python environment is incomplete: {path}")
    result = command(
        [
            str(python),
            "-c",
            "import json, PIL, sys; print(json.dumps([PIL.__version__, sys.version]))",
        ]
    )
    try:
        pillow_version, python_version = json.loads(result.stdout)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise JobError(
            f"live Python environment returned invalid metadata: {path}"
        ) from error
    if pillow_version != PILLOW_VERSION or python_version != sys.version:
        raise JobError(
            f"live Python environment does not match Pillow {PILLOW_VERSION} "
            "and the current base interpreter"
        )


def environment_create(_args: argparse.Namespace) -> int:
    prepare_private_state()
    destination = environment_path()
    temporary = environment_partial_path()
    marker_path = environment_partial_marker_path()
    with environment_lock() as lock_descriptor:
        recover_environment_partial()
        if destination.exists() or destination.is_symlink():
            validate_environment(destination)
            print(destination)
            return 0
        publish_json(
            marker_path,
            {
                "schema": 1,
                "owner": OWNER,
                "kind": "live-environment-partial",
                "partial": str(temporary),
                "destination": str(destination),
                "requirements_sha256": sha256_file(REQUIREMENTS),
                "python_version": sys.version,
            },
        )
        temporary.mkdir(mode=0o700)
        try:
            command(
                [sys.executable, "-m", "venv", str(temporary)],
                pass_fds=(lock_descriptor,),
            )
            python = temporary / "bin" / "python"
            command(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--require-hashes",
                    "--only-binary=:all:",
                    "--requirement",
                    str(REQUIREMENTS),
                ],
                capture=False,
                pass_fds=(lock_descriptor,),
            )
            validate_environment(temporary)
            try:
                container_payload.rename_no_replace(temporary, destination)
            except FileExistsError:
                validate_environment(destination)
            except container_payload.PayloadError as error:
                raise JobError(str(error)) from error
            validate_environment(destination)
        finally:
            if temporary.exists():
                ensure_private_directory(temporary)
                shutil.rmtree(temporary)
            marker_path.unlink(missing_ok=True)
    print(destination)
    return 0


def environment_check(_args: argparse.Namespace) -> int:
    prepare_private_state()
    destination = environment_path()
    validate_environment(destination)
    print(destination)
    return 0


def environment_show_path(_args: argparse.Namespace) -> int:
    print(environment_path())
    return 0


def publish_bytes(path: Path, payload: bytes, mode: int = 0o600) -> None:
    try:
        background_job.publish_bytes(path, payload, mode=mode)
    except (background_job.BackgroundJobError, OSError) as error:
        raise JobError(str(error)) from error


def publish_json(path: Path, payload: dict[str, Any]) -> None:
    publish_bytes(
        path,
        (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode(),
    )


def load_private_json(path: Path) -> dict[str, Any]:
    try:
        return background_job.load_json(path)
    except background_job.BackgroundJobError as error:
        raise JobError(str(error)) from error


def validate_background_paths(
    record: dict[str, Any],
    *,
    runtime_log: Path,
    completion: Path,
    label: str,
) -> None:
    """Bind generic background-process paths to this live RUN's namespace."""
    process = record.get("process")
    if not isinstance(process, dict):
        raise JobError(f"{label} has no owned process record")
    if process.get("runtime_log") != str(runtime_log):
        raise JobError(f"{label} runtime log is outside its RUN")
    if process.get("completion") != str(completion):
        raise JobError(f"{label} completion is outside its RUN")


def removal_runtime_paths(run: str) -> dict[str, Path]:
    return {
        "owner": record_path(run),
        "runtime": runtime_log_path(run),
        "completion": completion_path(run),
        "freeze_prelaunch": freeze_prelaunch_path(run),
        "freeze_owner": freeze_record_path(run),
        "freeze_runtime": freeze_runtime_log_path(run),
        "freeze_completion": freeze_completion_path(run),
        "freeze_result": freeze_result_path(run),
    }


def publish_remove_transaction(run: str, record: dict[str, Any]) -> dict[str, Any]:
    """Publish immutable authority before removing any collected runtime state."""
    for path in (record_path(run), log_path(run), status_path(run)):
        ensure_private_regular(path)
    runtime_sha256: dict[str, str] = {}
    for key, path in removal_runtime_paths(run).items():
        if not path.exists() and not path.is_symlink():
            continue
        ensure_private_regular(path)
        runtime_sha256[key] = sha256_file(path)
    transaction = {
        "schema": 1,
        "owner": OWNER,
        "kind": "live-remove",
        "run": run,
        "record": record,
        "log_sha256": sha256_file(log_path(run)),
        "status_sha256": sha256_file(status_path(run)),
        "runtime_sha256": runtime_sha256,
    }
    publish_json(remove_transaction_path(run), transaction)
    return transaction


def load_remove_transaction(run: str) -> dict[str, Any]:
    """Load retained removal authority and revalidate its immutable evidence."""
    transaction = load_private_json(remove_transaction_path(run))
    if set(transaction) != {
        "schema",
        "owner",
        "kind",
        "run",
        "record",
        "log_sha256",
        "status_sha256",
        "runtime_sha256",
    }:
        raise JobError("live removal transaction fields are inconsistent")
    if (
        transaction.get("schema") != 1
        or transaction.get("owner") != OWNER
        or transaction.get("kind") != "live-remove"
        or transaction.get("run") != run
    ):
        raise JobError("live removal transaction identity is inconsistent")
    record = transaction.get("record")
    if (
        not isinstance(record, dict)
        or record.get("schema") != 4
        or record.get("owner") != OWNER
        or record.get("run") != run
        or record.get("result_report") != str(result_path(run))
    ):
        raise JobError("live removal transaction has an invalid ownership record")
    validate_background_paths(
        record,
        runtime_log=runtime_log_path(run),
        completion=completion_path(run),
        label="live removal transaction",
    )
    for key, path in (
        ("log_sha256", log_path(run)),
        ("status_sha256", status_path(run)),
    ):
        ensure_private_regular(path)
        expected = str(transaction.get(key, ""))
        if not SHA256_RE.fullmatch(expected) or sha256_file(path) != expected:
            raise JobError(f"live removal transaction evidence changed: {path}")
    runtime_sha256 = transaction.get("runtime_sha256")
    candidates = removal_runtime_paths(run)
    if (
        not isinstance(runtime_sha256, dict)
        or not set(runtime_sha256).issubset(candidates)
        or "owner" not in runtime_sha256
        or any(
            not isinstance(key, str)
            or not isinstance(value, str)
            or not SHA256_RE.fullmatch(value)
            for key, value in runtime_sha256.items()
        )
    ):
        raise JobError("live removal transaction has invalid runtime digests")
    for key, path in candidates.items():
        if not path.exists() and not path.is_symlink():
            continue
        if key not in runtime_sha256:
            raise JobError(f"unexpected live runtime file appeared during removal: {path}")
        ensure_private_regular(path)
        if sha256_file(path) != runtime_sha256[key]:
            raise JobError(f"live runtime file changed during removal: {path}")
    return transaction


def cleanup_removal_runtime(run: str, transaction: dict[str, Any]) -> None:
    runtime_sha256 = transaction["runtime_sha256"]
    for key, path in removal_runtime_paths(run).items():
        if not path.exists() and not path.is_symlink():
            continue
        if key not in runtime_sha256:
            raise JobError(f"refusing unbound live runtime file: {path}")
        ensure_private_regular(path)
        if sha256_file(path) != runtime_sha256[key]:
            raise JobError(f"refusing changed live runtime file: {path}")
        path.unlink()


def validate_selector(selector: str | None) -> None:
    if selector is None:
        raise JobError("live acceptance requires one non-empty case or stack selection")
    if not SELECTOR_RE.fullmatch(selector):
        raise JobError(f"invalid case or stack selector: {selector!r}")
    command(
        [
            sys.executable,
            str(SELECTION_TOOL),
            "--lab-root",
            str(MAINTENANCE_ROOT),
            "--selection",
            selector,
            "validate",
        ]
    )


def validate_input_provenance(
    provenance: object,
    *,
    application: str,
    run: str,
    selection: str | None,
    harness_digest: str,
) -> dict[str, Any]:
    if selection is None:
        raise JobError("live acceptance provenance has no reviewed case or stack")
    if not isinstance(provenance, dict) or provenance.get("schema") != 2:
        raise JobError("live-job input provenance has an unsupported schema")
    hash_names = (
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
    )
    for key in hash_names:
        if not SHA256_RE.fullmatch(str(provenance.get(key, ""))):
            raise JobError(f"live-job input provenance has an invalid {key}")
    if not COMMIT_RE.fullmatch(str(provenance.get("source_commit", ""))):
        raise JobError("live-job input provenance has an invalid source commit")
    if (
        not isinstance(provenance.get("source_commit_marker"), str)
        or not isinstance(provenance.get("source_revision"), int)
        or provenance["source_revision"] < 0
    ):
        raise JobError("live-job input provenance has invalid source version metadata")
    zed_archive = provenance.get("zed_archive_sha256")
    zed_binary = provenance.get("zed_binary_sha256")
    if (zed_archive is None) != (zed_binary is None) or (
        zed_archive is not None
        and (
            not SHA256_RE.fullmatch(str(zed_archive))
            or not SHA256_RE.fullmatch(str(zed_binary))
        )
    ):
        raise JobError("live-job input provenance has invalid Zed digests")
    if (application == "zed") != (zed_archive is not None):
        raise JobError("live-job input provenance has the wrong application payload")
    keyboard_scenario = provenance.get("keyboard_scenario")
    if keyboard_scenario is not None and (
        not isinstance(keyboard_scenario, dict)
        or set(keyboard_scenario) != {"name", "path", "schema", "sha256"}
        or type(keyboard_scenario.get("schema")) is not int
        or keyboard_scenario.get("schema") != 1
        or not isinstance(keyboard_scenario.get("name"), str)
        or not isinstance(keyboard_scenario.get("path"), str)
        or re.fullmatch(
            r"cases/[a-z0-9]+(?:-[a-z0-9]+)*/tests/live-wayland-keyboard\.json",
            keyboard_scenario["path"],
        )
        is None
        or not SHA256_RE.fullmatch(str(keyboard_scenario.get("sha256", "")))
    ):
        raise JobError("live-job input provenance has an invalid keyboard scenario")
    if (application == "keyboard") != (keyboard_scenario is not None):
        raise JobError("live-job input provenance has the wrong keyboard scenario payload")
    if provenance.get("harness_sha256") != harness_digest:
        raise JobError("live-job input provenance has the wrong harness digest")
    if provenance.get("server_selection") != (selection or "master"):
        raise JobError("live-job input provenance has the wrong server selection")
    if provenance.get("client_selection") != "master":
        raise JobError("live-job input provenance has the wrong client selection")
    inputs_path = provenance.get("path")
    if inputs_path != str(RESULT_ROOT / run / "inputs"):
        raise JobError("live-job input provenance has the wrong owned path")
    return provenance


def load_record(run: str, *, require_current: bool = True) -> dict[str, Any]:
    run = validate_name(run)
    record = load_private_json(record_path(run))
    expected = {
        "owner": OWNER,
        "result_report": str(result_path(run)),
        "run": run,
    }
    for key, value in expected.items():
        if record.get(key) != value:
            raise JobError(f"live-job ownership record mismatch for {key}")
    if record.get("schema") != 4:
        raise JobError("live-job ownership record has an unsupported schema")
    if record.get("encoding") not in {"rgb", "h264"}:
        raise JobError("live-job ownership record has an invalid encoding")
    if record.get("application") not in APPLICATIONS:
        raise JobError("live-job ownership record has an invalid application")
    if record.get("lifecycle") not in LIFECYCLES:
        raise JobError("live-job ownership record has an invalid lifecycle")
    policy = record.get("h264_client_policy")
    if policy not in {*H264_CLIENT_POLICIES, *RETIRED_RECORD_POLICIES}:
        raise JobError("live-job ownership record has an invalid H.264 client policy")
    if record.get("alpha_scenarios") not in ALPHA_SCENARIOS:
        raise JobError("live-job ownership record has invalid alpha scenarios")
    if record.get("network_profile") not in NETWORK_PROFILES:
        raise JobError("live-job ownership record has an invalid network profile")
    selection = record.get("selection")
    if selection is not None and (
        not isinstance(selection, str) or not SELECTOR_RE.fullmatch(selection)
    ):
        raise JobError("live-job ownership record has an invalid selection")
    render_node = record.get("render_node")
    if not isinstance(render_node, str) or not Path(render_node).is_absolute():
        raise JobError("live-job ownership record has an invalid render node")
    hash_names = (
        "runner_sha256",
        "supervisor_sha256",
        "background_supervisor_sha256",
        "harness_sha256",
    )
    for key in hash_names:
        recorded = str(record.get(key, ""))
        if not SHA256_RE.fullmatch(recorded):
            raise JobError(f"live-job ownership record has an invalid {key}")
    validate_input_provenance(
        record.get("input_provenance"),
        application=str(record["application"]),
        run=run,
        selection=selection,
        harness_digest=str(record["harness_sha256"]),
    )
    if require_current and not record_is_current(record):
        raise JobError("live-job runner or harness changed while the job was owned")
    job_id = record.get("job_id")
    try:
        parsed_job_id = uuid.UUID(str(job_id))
    except ValueError as error:
        raise JobError("live-job ownership record has an invalid job ID") from error
    if parsed_job_id.version != 4 or str(parsed_job_id) != job_id:
        raise JobError("live-job ownership record has a non-canonical job ID")
    validate_background_paths(
        record,
        runtime_log=runtime_log_path(run),
        completion=completion_path(run),
        label="live-job ownership record",
    )
    try:
        background_job.process_state(record, require_current=require_current)
    except background_job.BackgroundJobError as error:
        raise JobError(str(error)) from error
    if freeze_prelaunch_path(run).exists() or freeze_prelaunch_path(run).is_symlink():
        matching_freeze_prelaunch_to_main(record)
    return record


def record_is_current(record: dict[str, Any]) -> bool:
    try:
        expected = {
            "runner_sha256": sha256_file(RUNNER),
            "supervisor_sha256": sha256_file(SUPERVISOR),
            "background_supervisor_sha256": sha256_file(BACKGROUND_SUPERVISOR),
            "harness_sha256": harness_sha256(),
        }
    except (JobError, OSError):
        return False
    return all(record.get(key) == value for key, value in expected.items())


def podman_ids(kind: str, run: str) -> list[str]:
    filters = [
        "--filter",
        "label=io.xpra.fork-maintenance.owner=live",
        "--filter",
        f"label=io.xpra.fork-maintenance.run-id={run}",
        "--quiet",
    ]
    if kind == "container":
        result = command(["podman", "ps", "--all", *filters])
    elif kind == "network":
        result = command(["podman", "network", "ls", *filters])
    else:
        raise JobError(f"unsupported Podman object kind: {kind}")
    return [line for line in result.stdout.splitlines() if line]


def podman_object(kind: str, object_id: str) -> tuple[str, str, dict[str, str]]:
    argv = ["podman", "container", "inspect", object_id]
    if kind == "network":
        argv = ["podman", "network", "inspect", object_id]
    payload = json.loads(command(argv).stdout)
    if not isinstance(payload, list) or len(payload) != 1:
        raise JobError(f"unexpected Podman inspection result for {kind} {object_id}")
    item = payload[0]
    if not isinstance(item, dict):
        raise JobError(f"invalid Podman inspection result for {kind} {object_id}")
    if kind == "container":
        immutable_id = str(item.get("Id", ""))
        name = str(item.get("Name", "")).lstrip("/")
        config = item.get("Config")
        labels = config.get("Labels") if isinstance(config, dict) else None
    else:
        immutable_id = str(item.get("id", item.get("ID", "")))
        name = str(item.get("name", item.get("Name", "")))
        label_fields = [
            (field, item[field]) for field in ("Labels", "labels") if field in item
        ]
        if not label_fields or any(
            not isinstance(value, dict) for _field, value in label_fields
        ):
            raise JobError(f"Podman object has invalid labels: {kind} {object_id}")
        labels = label_fields[0][1]
        if any(value != labels for _field, value in label_fields[1:]):
            raise JobError(f"Podman object has conflicting labels: {kind} {object_id}")
    if not isinstance(labels, dict):
        raise JobError(f"Podman object has no labels: {kind} {object_id}")
    if not SHA256_RE.fullmatch(immutable_id) or not NAME_RE.fullmatch(name):
        raise JobError(f"Podman object has invalid immutable identity: {kind} {object_id}")
    return immutable_id, name, {str(key): str(value) for key, value in labels.items()}


def podman_labels(kind: str, object_id: str) -> dict[str, str]:
    return podman_object(kind, object_id)[2]


def owned_objects(run: str) -> dict[str, list[str]]:
    return {
        "containers": podman_ids("container", run),
        "networks": podman_ids("network", run),
    }


def verify_owned_object(kind: str, object_id: str, run: str) -> None:
    labels = podman_labels(kind, object_id)
    expected = {
        "io.xpra.fork-maintenance.owner": "live",
        "io.xpra.fork-maintenance.run-id": run,
    }
    if any(labels.get(key) != value for key, value in expected.items()):
        raise JobError(f"refusing unowned {kind}: {object_id}")


def object_ledger_entries(run: str) -> list[dict[str, Any]]:
    result_root = RESULT_ROOT / run
    if not result_root.exists() and not result_root.is_symlink():
        return []
    ensure_private_directory(result_root)
    entries: list[dict[str, Any]] = []
    for ledger_path in sorted(result_root.glob("*/podman-objects.json")):
        ledger = load_private_json(ledger_path)
        scenario = ledger.get("scenario")
        objects = ledger.get("objects")
        if (
            ledger.get("schema") != 1
            or ledger.get("owner") != "live"
            or ledger.get("run_id") != run
            or not isinstance(scenario, str)
            or not NAME_RE.fullmatch(scenario)
            or not isinstance(objects, dict)
        ):
            raise JobError(f"live Podman object ledger is invalid: {ledger_path}")
        for role, item in objects.items():
            if role not in {"server", "client", "network"} or not isinstance(item, dict):
                raise JobError(f"live Podman ledger role is invalid: {ledger_path}")
            kind = item.get("kind")
            name = item.get("name")
            immutable_id = item.get("id")
            labels = item.get("labels")
            expected_role = role if role != "network" else "network"
            if (
                kind != ("network" if role == "network" else "container")
                or not isinstance(name, str)
                or not NAME_RE.fullmatch(name)
                or not isinstance(immutable_id, str)
                or (immutable_id and not SHA256_RE.fullmatch(immutable_id))
                or not isinstance(labels, dict)
                or labels.get("io.xpra.fork-maintenance.owner") != "live"
                or labels.get("io.xpra.fork-maintenance.run-id") != run
                or labels.get("io.xpra.fork-maintenance.scenario") != scenario
                or labels.get("io.xpra.fork-maintenance.role") != expected_role
                or not all(isinstance(key, str) and isinstance(value, str) for key, value in labels.items())
            ):
                raise JobError(f"live Podman ledger entry is invalid: {ledger_path}")
            entries.append(
                {
                    "id": immutable_id,
                    "kind": kind,
                    "labels": labels,
                    "name": name,
                    "role": role,
                }
            )
    return entries


def load_freeze_prelaunch(run: str) -> dict[str, Any]:
    record = load_private_json(freeze_prelaunch_path(run))
    required = {
        "application",
        "background_supervisor_sha256",
        "completion",
        "freeze_owner",
        "freeze_result",
        "harness_sha256",
        "job_id",
        "kind",
        "owner",
        "process",
        "result",
        "run",
        "runner_sha256",
        "schema",
        "selection",
        "staging",
        "supervisor_sha256",
        "zed_directory",
    }
    if set(record) != required:
        raise JobError("live input-freeze prelaunch fields are inconsistent")
    if (
        record.get("schema") != 1
        or record.get("owner") != OWNER
        or record.get("kind") != "input-freeze-prelaunch"
        or record.get("run") != run
    ):
        raise JobError("live input-freeze prelaunch identity is inconsistent")
    job_id = str(record.get("job_id", ""))
    try:
        parsed_job_id = uuid.UUID(job_id)
    except ValueError as error:
        raise JobError("live input-freeze prelaunch has an invalid job ID") from error
    if parsed_job_id.version != 4 or str(parsed_job_id) != job_id:
        raise JobError("live input-freeze prelaunch has a non-canonical job ID")
    expected_paths = {
        "completion": str(freeze_completion_path(run)),
        "freeze_owner": str(freeze_record_path(run)),
        "freeze_result": str(freeze_result_path(run)),
        "result": str(RESULT_ROOT / run),
        "staging": str(freeze_staging_path(run, job_id)),
    }
    if any(record.get(key) != value for key, value in expected_paths.items()):
        raise JobError("live input-freeze prelaunch paths are inconsistent")
    if record.get("application") not in APPLICATIONS:
        raise JobError("live input-freeze prelaunch application is invalid")
    selection = record.get("selection")
    if selection is not None and (
        not isinstance(selection, str) or not SELECTOR_RE.fullmatch(selection)
    ):
        raise JobError("live input-freeze prelaunch selection is invalid")
    zed_directory = record.get("zed_directory")
    if zed_directory is not None and not isinstance(zed_directory, str):
        raise JobError("live input-freeze prelaunch Zed directory is invalid")
    for key in (
        "background_supervisor_sha256",
        "harness_sha256",
        "runner_sha256",
        "supervisor_sha256",
    ):
        if not SHA256_RE.fullmatch(str(record.get(key, ""))):
            raise JobError(f"live input-freeze prelaunch has an invalid {key}")
    process = record.get("process")
    if not isinstance(process, dict) or set(process) != {"pid", "start_ticks"}:
        raise JobError("live input-freeze prelaunch has invalid starter identity")
    if (
        not isinstance(process.get("pid"), int)
        or process["pid"] <= 1
        or not isinstance(process.get("start_ticks"), str)
        or not process["start_ticks"].isdigit()
    ):
        raise JobError("live input-freeze prelaunch has invalid starter identity")
    return record


def freeze_prelaunch_active(record: dict[str, Any]) -> bool:
    process = record["process"]
    try:
        identity = background_job.process_identity(int(process["pid"]))
    except background_job.BackgroundJobError as error:
        raise JobError(str(error)) from error
    if identity is None:
        return False
    state, _process_group, start_ticks = identity
    return state != "Z" and start_ticks == process["start_ticks"]


def matching_freeze_prelaunch(record: dict[str, Any]) -> dict[str, Any] | None:
    run = str(record["run"])
    path = freeze_prelaunch_path(run)
    if not path.exists() and not path.is_symlink():
        return None
    prelaunch = load_freeze_prelaunch(run)
    for key in (
        "application",
        "background_supervisor_sha256",
        "freeze_result",
        "harness_sha256",
        "job_id",
        "result",
        "run",
        "runner_sha256",
        "selection",
        "staging",
        "supervisor_sha256",
    ):
        if prelaunch.get(key) != record.get(key):
            raise JobError(f"live input-freeze owner and prelaunch differ for {key}")
    return prelaunch


def matching_freeze_prelaunch_to_main(record: dict[str, Any]) -> dict[str, Any]:
    run = str(record["run"])
    prelaunch = load_freeze_prelaunch(run)
    for key in (
        "application",
        "background_supervisor_sha256",
        "harness_sha256",
        "job_id",
        "run",
        "runner_sha256",
        "selection",
        "supervisor_sha256",
    ):
        if prelaunch.get(key) != record.get(key):
            raise JobError(f"live main owner and freeze prelaunch differ for {key}")
    return prelaunch


def remove_freeze_prelaunch(run: str) -> None:
    path = freeze_prelaunch_path(run)
    if not path.exists() and not path.is_symlink():
        return
    ensure_private_regular(path)
    path.unlink()


def load_freeze_record(run: str) -> dict[str, Any]:
    record = load_private_json(freeze_record_path(run))
    if (
        record.get("schema") != 2
        or record.get("owner") != OWNER
        or record.get("kind") != "input-freeze"
        or record.get("run") != run
    ):
        raise JobError("live input-freeze ownership record is invalid")
    job_id = str(record.get("job_id", ""))
    try:
        parsed_job_id = uuid.UUID(job_id)
    except ValueError as error:
        raise JobError("live input-freeze record has an invalid job ID") from error
    if parsed_job_id.version != 4 or str(parsed_job_id) != job_id:
        raise JobError("live input-freeze record has a non-canonical job ID")
    if record.get("staging") != str(freeze_staging_path(run, job_id)):
        raise JobError("live input-freeze staging path is inconsistent")
    if record.get("result") != str(RESULT_ROOT / run):
        raise JobError("live input-freeze result path is inconsistent")
    if record.get("freeze_result") != str(freeze_result_path(run)):
        raise JobError("live input-freeze result record path is inconsistent")
    if record.get("application") not in APPLICATIONS:
        raise JobError("live input-freeze application is invalid")
    selection = record.get("selection")
    if selection is not None and (
        not isinstance(selection, str) or not SELECTOR_RE.fullmatch(selection)
    ):
        raise JobError("live input-freeze selection is invalid")
    for key in (
        "background_supervisor_sha256",
        "harness_sha256",
        "runner_sha256",
        "supervisor_sha256",
    ):
        if not SHA256_RE.fullmatch(str(record.get(key, ""))):
            raise JobError(f"live input-freeze has an invalid {key}")
    validate_background_paths(
        record,
        runtime_log=freeze_runtime_log_path(run),
        completion=freeze_completion_path(run),
        label="live input-freeze ownership record",
    )
    try:
        background_job.process_state(record, require_current=False)
    except background_job.BackgroundJobError as error:
        raise JobError(str(error)) from error
    matching_freeze_prelaunch(record)
    return record


def freeze_process_state(record: dict[str, Any]) -> dict[str, Any]:
    try:
        return background_job.process_state(record, require_current=False)
    except background_job.BackgroundJobError as error:
        raise JobError(str(error)) from error


def publish_freeze_abort_transaction(record: dict[str, Any]) -> dict[str, Any]:
    """Bind the exact input directories before an abort starts deleting them."""
    run = str(record["run"])
    owner_path = freeze_record_path(run)
    ensure_private_regular(owner_path)
    directories: dict[str, dict[str, Any]] = {}
    for key in ("staging", "result"):
        source = Path(str(record[key]))
        removal = freeze_abort_staging_path(run, key)
        if removal.exists() or removal.is_symlink():
            raise JobError(f"unowned live input-freeze abort staging exists: {removal}")
        entry: dict[str, Any] = {
            "source": str(source),
            "removal": str(removal),
            "present": False,
        }
        if source.exists() or source.is_symlink():
            if source.is_symlink():
                raise JobError(f"refusing symlinked live input-freeze path: {source}")
            ensure_private_directory(source)
            if key == "result":
                provenance = load_freeze_result(record)
                try:
                    owned = input_checksum_validation(source / "inputs", provenance)
                except (JobError, OSError, TypeError, ValueError):
                    owned = False
                if not owned:
                    raise JobError(
                        "refusing live result not proven by input-freeze provenance: "
                        f"{source}"
                    )
            info = source.stat(follow_symlinks=False)
            entry.update(
                {
                    "present": True,
                    "device": info.st_dev,
                    "inode": info.st_ino,
                }
            )
        directories[key] = entry
    transaction = {
        "schema": 1,
        "owner": OWNER,
        "kind": "live-input-freeze-abort",
        "run": run,
        "freeze_owner_sha256": sha256_file(owner_path),
        "directories": directories,
    }
    publish_json(freeze_abort_transaction_path(run), transaction)
    return transaction


def load_freeze_abort_transaction(record: dict[str, Any]) -> dict[str, Any]:
    run = str(record["run"])
    transaction = load_private_json(freeze_abort_transaction_path(run))
    if set(transaction) != {
        "schema",
        "owner",
        "kind",
        "run",
        "freeze_owner_sha256",
        "directories",
    }:
        raise JobError("live input-freeze abort transaction fields are inconsistent")
    if (
        transaction.get("schema") != 1
        or transaction.get("owner") != OWNER
        or transaction.get("kind") != "live-input-freeze-abort"
        or transaction.get("run") != run
        or transaction.get("freeze_owner_sha256")
        != sha256_file(freeze_record_path(run))
    ):
        raise JobError("live input-freeze abort transaction identity is inconsistent")
    directories = transaction.get("directories")
    if not isinstance(directories, dict) or set(directories) != {"staging", "result"}:
        raise JobError("live input-freeze abort directories are inconsistent")
    for key, entry in directories.items():
        if not isinstance(entry, dict):
            raise JobError("live input-freeze abort directory entry is invalid")
        expected = {
            "source": str(record[key]),
            "removal": str(freeze_abort_staging_path(run, key)),
            "present": entry.get("present"),
        }
        if entry.get("present") is True:
            expected.update({"device": entry.get("device"), "inode": entry.get("inode")})
            if (
                not isinstance(entry.get("device"), int)
                or not isinstance(entry.get("inode"), int)
                or entry["device"] < 0
                or entry["inode"] <= 0
            ):
                raise JobError("live input-freeze abort directory identity is invalid")
        elif entry.get("present") is not False:
            raise JobError("live input-freeze abort directory presence is invalid")
        if entry != expected:
            raise JobError("live input-freeze abort directory entry is inconsistent")
    return transaction


def remove_freeze_abort_directories(transaction: dict[str, Any]) -> None:
    for entry in transaction["directories"].values():
        source = Path(str(entry["source"]))
        removal = Path(str(entry["removal"]))
        source_exists = source.exists() or source.is_symlink()
        removal_exists = removal.exists() or removal.is_symlink()
        if source_exists and removal_exists:
            raise JobError("live input-freeze abort has both source and staging")
        if not entry["present"]:
            if source_exists or removal_exists:
                raise JobError("unexpected live input-freeze abort directory appeared")
            continue
        path = source if source_exists else removal if removal_exists else None
        if path is None:
            continue
        if path.is_symlink():
            raise JobError(f"refusing symlinked live input-freeze abort path: {path}")
        ensure_private_directory(path)
        info = path.stat(follow_symlinks=False)
        if info.st_dev != entry["device"] or info.st_ino != entry["inode"]:
            raise JobError("live input-freeze abort directory identity changed")
        if source_exists:
            try:
                container_payload.rename_no_replace(source, removal)
            except (FileExistsError, container_payload.PayloadError) as error:
                raise JobError(f"cannot stage live input-freeze abort directory: {error}") from error
        shutil.rmtree(removal)
    for entry in transaction["directories"].values():
        for key in ("source", "removal"):
            path = Path(str(entry[key]))
            if path.exists() or path.is_symlink():
                raise JobError(f"live input-freeze abort directory remains: {path}")


def cleanup_freeze_state(
    record: dict[str, Any],
    *,
    remove_input_directories: bool,
) -> None:
    if remove_input_directories:
        transaction_path = freeze_abort_transaction_path(str(record["run"]))
        if transaction_path.exists() or transaction_path.is_symlink():
            transaction = load_freeze_abort_transaction(record)
        else:
            transaction = publish_freeze_abort_transaction(record)
        remove_freeze_abort_directories(transaction)
    else:
        transaction_path = None
    run = str(record["run"])
    for path in (
        freeze_result_path(run),
        freeze_runtime_log_path(run),
        freeze_completion_path(run),
    ):
        if not path.exists() and not path.is_symlink():
            continue
        ensure_private_regular(path)
        path.unlink()
    if transaction_path is not None:
        ensure_private_regular(transaction_path)
        transaction_path.unlink()
    owner_path = freeze_record_path(run)
    if owner_path.exists() or owner_path.is_symlink():
        ensure_private_regular(owner_path)
        owner_path.unlink()


def load_freeze_result(record: dict[str, Any]) -> dict[str, Any]:
    payload = load_private_json(Path(str(record["freeze_result"])))
    if (
        payload.get("schema") != 1
        or payload.get("owner") != OWNER
        or payload.get("kind") != "input-freeze-result"
        or payload.get("run") != record["run"]
        or payload.get("job_id") != record["job_id"]
    ):
        raise JobError("live input-freeze result identity is inconsistent")
    return validate_input_provenance(
        payload.get("input_provenance"),
        application=str(record["application"]),
        run=str(record["run"]),
        selection=record.get("selection"),
        harness_digest=str(record["harness_sha256"]),
    )


def freeze_live_inputs(
    args: argparse.Namespace,
    staging: Path,
    *,
    expected_harness_sha256: str,
) -> dict[str, Any]:
    runner = live_runner_module()
    zed_directory = None
    if args.application == "zed":
        zed_directory = (
            Path(args.zed_directory)
            if args.zed_directory
            else runner.DEFAULT_ZED_DIRECTORY
        )
    try:
        provenance = runner.freeze_owned_inputs(
            staging,
            STATE_ROOT,
            application=args.application,
            selection_name=args.selection,
            zed_directory=zed_directory,
        )
    except runner.LabFailure as error:
        raise JobError(str(error)) from error
    provenance["path"] = str(RESULT_ROOT / args.run / "inputs")
    return validate_input_provenance(
        provenance,
        application=args.application,
        run=args.run,
        selection=args.selection,
        harness_digest=expected_harness_sha256,
    )


def freeze_worker(args: argparse.Namespace) -> int:
    """Create the immutable inputs under a separately owned process group."""
    prepare_private_state()
    run = validate_name(args.run)
    record = load_freeze_record(run)
    if record["job_id"] != args.job_id:
        raise JobError("live input-freeze worker has the wrong job ID")
    expected_hashes = {
        "background_supervisor_sha256": sha256_file(BACKGROUND_SUPERVISOR),
        "harness_sha256": harness_sha256(),
        "runner_sha256": sha256_file(RUNNER),
        "supervisor_sha256": sha256_file(SUPERVISOR),
    }
    if any(record.get(key) != value for key, value in expected_hashes.items()):
        raise JobError("live harness changed before input freeze execution")
    staging = Path(str(record["staging"]))
    staging.mkdir(mode=0o700)
    ensure_private_directory(staging)
    provenance = freeze_live_inputs(
        args,
        staging,
        expected_harness_sha256=str(record["harness_sha256"]),
    )
    publish_json(
        freeze_result_path(run),
        {
            "input_provenance": provenance,
            "job_id": record["job_id"],
            "kind": "input-freeze-result",
            "owner": OWNER,
            "run": run,
            "schema": 1,
        },
    )
    return 0


def start(args: argparse.Namespace) -> int:
    prepare_private_state()
    run = validate_name(args.run)
    with lifecycle_lock(run):
        return _start_locked(args, run)


def _start_locked(args: argparse.Namespace, run: str) -> int:
    runner = live_runner_module()
    validate_selector(args.selection)
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
        raise JobError(str(error)) from error
    if not RUNNER.is_file() or RUNNER.is_symlink():
        raise JobError(f"live runner is unavailable: {RUNNER}")
    paths = (
        freeze_prelaunch_path(run),
        freeze_record_path(run),
        freeze_runtime_log_path(run),
        freeze_completion_path(run),
        freeze_result_path(run),
        record_path(run),
        runtime_log_path(run),
        completion_path(run),
        log_path(run),
        status_path(run),
        remove_transaction_path(run),
        freeze_abort_transaction_path(run),
        freeze_abort_staging_path(run, "staging"),
        freeze_abort_staging_path(run, "result"),
        RESULT_ROOT / run,
    )
    for path in paths:
        if path.exists() or path.is_symlink():
            raise JobError(f"run artifact already exists: {path}")
    for path in RESULT_ROOT.iterdir():
        if path.name.startswith(f".{run}.freeze-"):
            raise JobError(f"unowned live input-freeze staging requires review: {path}")
    job_id = str(uuid.uuid4())
    staging = freeze_staging_path(run, job_id)
    final_result = RESULT_ROOT / run
    initial_hashes = {
        "background_supervisor_sha256": sha256_file(BACKGROUND_SUPERVISOR),
        "harness_sha256": harness_sha256(),
        "runner_sha256": sha256_file(RUNNER),
        "supervisor_sha256": sha256_file(SUPERVISOR),
    }
    freeze_record = {
        "application": args.application,
        **initial_hashes,
        "freeze_result": str(freeze_result_path(run)),
        "job_id": job_id,
        "kind": "input-freeze",
        "owner": OWNER,
        "result": str(final_result),
        "run": run,
        "schema": 2,
        "selection": args.selection,
        "staging": str(staging),
    }
    starter_identity = background_job.process_identity(os.getpid())
    if starter_identity is None:
        raise JobError("cannot bind the live input-freeze starter identity")
    _starter_state, _starter_group, starter_ticks = starter_identity
    freeze_prelaunch = {
        "application": args.application,
        **initial_hashes,
        "completion": str(freeze_completion_path(run)),
        "freeze_owner": str(freeze_record_path(run)),
        "freeze_result": str(freeze_result_path(run)),
        "job_id": job_id,
        "kind": "input-freeze-prelaunch",
        "owner": OWNER,
        "process": {"pid": os.getpid(), "start_ticks": starter_ticks},
        "result": str(final_result),
        "run": run,
        "schema": 1,
        "selection": args.selection,
        "staging": str(staging),
        "zed_directory": args.zed_directory,
    }
    freeze_argv = [
        sys.executable,
        str(SUPERVISOR),
        "_freeze",
        run,
        "--application",
        args.application,
        "--job-id",
        job_id,
    ]
    if args.selection:
        freeze_argv.extend(("--selection", args.selection))
    if args.zed_directory:
        freeze_argv.extend(("--zed-directory", args.zed_directory))
    freeze_owned: dict[str, Any] | None = None
    owned: dict[str, Any] | None = None
    main_launch_retained = False
    publish_json(freeze_prelaunch_path(run), freeze_prelaunch)
    try:
        freeze_owned = background_job.launch(
            owner_path=freeze_record_path(run),
            runtime_log=freeze_runtime_log_path(run),
            completion_file=freeze_completion_path(run),
            record=freeze_record,
            argv=freeze_argv,
            cwd=PROJECT_ROOT,
            environment=dict(os.environ),
        )
        freeze_state = background_job.wait_process(freeze_owned)
        if freeze_state["exit_code"] != 0:
            raise JobError(
                "live input freeze failed; inspect "
                f"{freeze_runtime_log_path(run)}"
            )
        input_provenance = load_freeze_result(freeze_owned)
        current_hashes = {
            "background_supervisor_sha256": sha256_file(BACKGROUND_SUPERVISOR),
            "harness_sha256": harness_sha256(),
            "runner_sha256": sha256_file(RUNNER),
            "supervisor_sha256": sha256_file(SUPERVISOR),
        }
        if current_hashes != initial_hashes:
            raise JobError("live harness changed before its owned run was launched")
        try:
            container_payload.rename_no_replace(staging, final_result)
        except (FileExistsError, container_payload.PayloadError, OSError) as error:
            raise JobError(f"live result publication raced: {final_result}") from error
        runner.load_bound_inputs(
            final_result / "inputs",
            expected_manifest_sha256=input_provenance["input_manifest_sha256"],
            expected_tree_sha256=input_provenance["input_tree_sha256"],
        )
        frozen_harness = final_result / "inputs" / "harness"
        frozen_runner = frozen_harness / "infra" / "live" / "run.py"
        frozen_supervisor = frozen_harness / "infra" / "live" / "job.py"
        frozen_background = frozen_harness / "tools" / "background_job.py"
        if (
            sha256_file(frozen_runner) != initial_hashes["runner_sha256"]
            or sha256_file(frozen_supervisor) != initial_hashes["supervisor_sha256"]
            or sha256_file(frozen_background)
            != initial_hashes["background_supervisor_sha256"]
            or runner.harness_snapshot_sha256(frozen_harness)
            != initial_hashes["harness_sha256"]
        ):
            raise JobError("frozen live harness does not match its owner record")
        render_node = str(args.render_node or runner.DEFAULT_RENDER_NODE)
        record = {
            "alpha_scenarios": args.alpha_scenarios,
            "application": args.application,
            "background_supervisor_sha256": initial_hashes[
                "background_supervisor_sha256"
            ],
            "created_at": datetime.now(UTC).isoformat(),
            "encoding": args.encoding,
            "h264_client_policy": args.h264_client_policy,
            "harness_sha256": initial_hashes["harness_sha256"],
            "input_provenance": input_provenance,
            "job_id": job_id,
            "lifecycle": args.lifecycle,
            "network_profile": args.network_profile,
            "owner": OWNER,
            "render_node": render_node,
            "result_report": str(result_path(run)),
            "run": run,
            "runner_sha256": initial_hashes["runner_sha256"],
            "schema": 4,
            "selection": args.selection,
            "supervisor_sha256": initial_hashes["supervisor_sha256"],
        }
        argv = [
            sys.executable,
            "-B",
            str(frozen_runner),
            "--encoding",
            args.encoding,
            "--h264-client-policy",
            args.h264_client_policy,
            "--application",
            args.application,
            "--lifecycle",
            args.lifecycle,
            "--alpha-scenarios",
            args.alpha_scenarios,
            "--network-profile",
            args.network_profile,
            "--run-id",
            run,
            "--state-root",
            str(STATE_ROOT),
            "--render-node",
            render_node,
            "--bound-inputs",
            input_provenance["path"],
            "--bound-input-manifest-sha256",
            input_provenance["input_manifest_sha256"],
            "--bound-input-tree-sha256",
            input_provenance["input_tree_sha256"],
        ]
        if args.selection:
            argv.extend(("--selection", args.selection))
        environment = dict(os.environ)
        environment["XPRA_FORK_JOB_ID"] = job_id
        try:
            owned = background_job.launch(
                owner_path=record_path(run),
                runtime_log=runtime_log_path(run),
                completion_file=completion_path(run),
                record=record,
                argv=argv,
                cwd=frozen_harness,
                environment=environment,
            )
        except background_job.LaunchStateRetained as error:
            main_launch_retained = True
            raise JobError(str(error)) from error
        except background_job.BackgroundJobError as error:
            raise JobError(str(error)) from error
        try:
            matching_freeze_prelaunch_to_main(owned)
            remove_freeze_prelaunch(run)
            cleanup_freeze_state(freeze_owned, remove_input_directories=False)
        except (JobError, OSError) as error:
            print(
                f"warning: live run started but stale freeze state remains: {error}",
                file=sys.stderr,
            )
    finally:
        if (
            owned is None
            and not record_path(run).exists()
            and freeze_owned is not None
            and not main_launch_retained
        ):
            try:
                freeze_state = freeze_process_state(freeze_owned)
                if freeze_state["state"] == "running":
                    background_job.terminate(freeze_owned, require_current=False)
                cleanup_freeze_state(
                    freeze_owned,
                    remove_input_directories=True,
                )
                remove_freeze_prelaunch(run)
            except (JobError, background_job.BackgroundJobError, OSError):
                # Preserve the exact owner record for a later live-abort recovery.
                pass
    assert owned is not None
    print(f"started durable live run {run} (pid {owned['process']['pid']})")
    return 0


def report_result(run: str) -> str | None:
    report = result_path(run)
    if not report.is_file() or report.is_symlink():
        return None
    ensure_private_directory(report.parent)
    ensure_private_regular(report)
    try:
        value = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return str(value.get("result")) if isinstance(value, dict) else None


def status(args: argparse.Namespace) -> int:
    prepare_private_state()
    run = validate_name(args.run)
    owner = record_path(run)
    freeze_owner = freeze_record_path(run)
    if not owner.exists() and not owner.is_symlink():
        removal = remove_transaction_path(run)
        if removal.exists() or removal.is_symlink():
            load_remove_transaction(run)
            runtime_remaining = any(
                path.exists() or path.is_symlink()
                for path in removal_runtime_paths(run).values()
            )
            print(
                json.dumps(
                    {
                        "phase": "removing" if runtime_remaining else "removed",
                        "run": run,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if not freeze_owner.exists() and not freeze_owner.is_symlink():
            prelaunch = load_freeze_prelaunch(run)
            print(
                json.dumps(
                    {
                        "active": freeze_prelaunch_active(prelaunch),
                        "job_id": prelaunch["job_id"],
                        "phase": "input-freeze-prelaunch",
                        "run": run,
                        "staging": prelaunch["staging"],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        freeze_record = load_freeze_record(run)
        state = freeze_process_state(freeze_record)
        print(
            json.dumps(
                {
                    "input_freeze": state,
                    "job_id": freeze_record["job_id"],
                    "phase": "input-freeze",
                    "run": run,
                    "staging": freeze_record["staging"],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    record = load_record(run, require_current=False)
    state = background_job.process_state(record, require_current=False)
    freeze_state: dict[str, Any] | None = None
    if freeze_owner.exists() or freeze_owner.is_symlink():
        freeze_record = load_freeze_record(run)
        if freeze_record["job_id"] != record["job_id"]:
            raise JobError("live run and input-freeze owner job IDs differ")
        freeze_state = freeze_process_state(freeze_record)
    print(
        json.dumps(
            {
                "input_freeze": freeze_state,
                "owned_objects": owned_objects(args.run),
                "process": state,
                "report_result": report_result(args.run),
                "run": args.run,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def logs(args: argparse.Namespace) -> int:
    prepare_private_state()
    run = validate_name(args.run)
    if not record_path(run).exists() and not record_path(run).is_symlink():
        removal = remove_transaction_path(run)
        if removal.exists() or removal.is_symlink():
            load_remove_transaction(run)
            path = log_path(run)
            ensure_private_regular(path)
            sys.stdout.buffer.write(path.read_bytes())
            return 0
        if freeze_record_path(run).exists() or freeze_record_path(run).is_symlink():
            record = load_freeze_record(run)
        else:
            load_freeze_prelaunch(run)
            path = freeze_runtime_log_path(run)
            if not path.exists() and not path.is_symlink():
                raise JobError(f"live input-freeze prelaunch has no runtime log: {run}")
            ensure_private_regular(path)
            sys.stdout.buffer.write(path.read_bytes())
            return 0
    else:
        record = load_record(run, require_current=False)
    path = background_job.runtime_log_path(record, require_current=False)
    ensure_private_regular(path)
    sys.stdout.buffer.write(path.read_bytes())
    return 0


def regular_tree_files(root: Path) -> list[Path]:
    if root.is_symlink() or not root.is_dir():
        raise JobError(f"evidence tree is not a real directory: {root}")
    paths: list[Path] = []
    for current_name, directory_names, file_names in os.walk(root, followlinks=False):
        current = Path(current_name)
        for name in directory_names:
            path = current / name
            if path.is_symlink() or not path.is_dir():
                raise JobError(f"evidence tree contains an unsafe directory: {path}")
            ensure_private_directory(path)
        for name in file_names:
            path = current / name
            if path.is_symlink() or not path.is_file():
                raise JobError(f"evidence tree contains a non-regular file: {path}")
            ensure_private_regular(path)
            paths.append(path)
    return sorted(paths)


def input_checksum_validation(inputs: Path, source: dict[str, Any]) -> bool:
    runner = live_runner_module()
    checksum_path = inputs / "SHA256SUMS"
    ensure_private_regular(checksum_path)
    expected: dict[str, str] = {}
    for line in checksum_path.read_text(encoding="utf-8").splitlines():
        digest, separator, relative_value = line.partition("  ")
        try:
            relative = container_payload.archive_path(relative_value)
        except container_payload.PayloadError:
            return False
        key = str(relative)
        if not separator or not SHA256_RE.fullmatch(digest) or key in expected:
            return False
        expected[key] = digest
    observed = {
        path.relative_to(inputs).as_posix(): sha256_file(path)
        for path in regular_tree_files(inputs)
        if path != checksum_path
    }
    manifest = inputs / "manifest.json"
    return (
        observed == expected
        and source.get("input_manifest_sha256") == sha256_file(manifest)
        and source.get("input_tree_sha256") == runner.tree_sha256(inputs)
    )


def image_provenance_validation(
    payload: dict[str, Any],
    provenance: dict[str, Any],
) -> bool:
    images = payload.get("images")
    if not isinstance(images, dict) or set(images) != {"client", "server"}:
        return False
    source_commit = provenance["source_commit"]
    expected = {
        "client": {
            "context": provenance["client_context_sha256"],
            "role": "client-image",
            "selection": "master",
        },
        "server": {
            "context": provenance["server_context_sha256"],
            "role": "server-image",
            "selection": provenance["server_selection"],
        },
    }
    for role, values in expected.items():
        image = images.get(role)
        if not isinstance(image, dict):
            return False
        image_id = image.get("id")
        labels = image.get("labels")
        if (
            not isinstance(image_id, str)
            or not re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", image_id)
            or not isinstance(labels, dict)
            or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in labels.items()
            )
            or image.get("build_context_sha256") != values["context"]
            or image.get("selection") != values["selection"]
            or not isinstance(image.get("tag"), str)
            or not image["tag"]
        ):
            return False
        expected_labels = {
            "io.xpra.fork-maintenance.context": values["context"],
            "io.xpra.fork-maintenance.owner": "live",
            "io.xpra.fork-maintenance.role": values["role"],
            "io.xpra.fork-maintenance.source": source_commit,
        }
        if labels != expected_labels:
            return False
    return True


def current_image_validation(payload: dict[str, Any]) -> bool:
    images = payload.get("images")
    if not isinstance(images, dict):
        return False
    for role in ("client", "server"):
        reported = images.get(role)
        if not isinstance(reported, dict):
            return False
        image_id = reported.get("id")
        labels = reported.get("labels")
        if not isinstance(image_id, str) or not isinstance(labels, dict):
            return False
        try:
            inspected = json.loads(
                command(["podman", "image", "inspect", image_id]).stdout
            )
        except (JobError, json.JSONDecodeError):
            return False
        if not isinstance(inspected, list) or len(inspected) != 1:
            return False
        item = inspected[0]
        if not isinstance(item, dict):
            return False
        actual_id = item.get("Id")
        actual_labels = item.get("Labels")
        if not isinstance(actual_labels, dict):
            config = item.get("Config")
            actual_labels = config.get("Labels") if isinstance(config, dict) else None
        if not isinstance(actual_labels, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in actual_labels.items()
        ):
            return False
        actual_maintenance_labels = {
            key: value
            for key, value in actual_labels.items()
            if key.startswith(MAINTENANCE_LABEL_PREFIX)
        }
        normalize = lambda value: str(value).removeprefix("sha256:")
        if normalize(actual_id) != normalize(image_id) or actual_maintenance_labels != labels:
            return False
    return True


def evidence_tree_validation(payload: dict[str, Any], report: Path) -> bool:
    runner = live_runner_module()
    root = report.parent
    source = payload.get("source")
    scenarios = payload.get("scenarios")
    scenario_digests = payload.get("scenario_report_sha256")
    if (
        not isinstance(source, dict)
        or not isinstance(scenarios, list)
        or not scenarios
        or not isinstance(scenario_digests, dict)
    ):
        return False
    names: list[str] = []
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            return False
        name = scenario.get("name")
        if not isinstance(name, str) or not NAME_RE.fullmatch(name) or name in names:
            return False
        names.append(name)
    expected_top_level = {"inputs", "report.json", *names}
    if {path.name for path in root.iterdir()} != expected_top_level:
        return False
    if set(scenario_digests) != set(names):
        return False
    keyboard_scenario = None
    keyboard_scenario_sha256 = None
    if payload.get("application") == "keyboard":
        keyboard_path = root / "inputs" / "keyboard-scenario.json"
        try:
            keyboard_scenario = runner.load_keyboard_scenario(keyboard_path)
            keyboard_scenario_sha256 = sha256_file(keyboard_path)
        except (OSError, ValueError, runner.LabFailure):
            return False
    for embedded, name in zip(scenarios, names, strict=True):
        scenario_root = root / name
        scenario_report = scenario_root / "report.json"
        ensure_private_regular(scenario_report)
        try:
            recorded = json.loads(scenario_report.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if recorded != embedded or scenario_digests[name] != sha256_file(scenario_report):
            return False
        if (
            embedded.get("result") != "passed"
            or embedded.get("artifact_collection_passed") is not True
            or not isinstance(embedded.get("cleanup"), dict)
            or embedded["cleanup"].get("passed") is not True
        ):
            return False
        artifact_digests = embedded.get("artifact_sha256")
        if not isinstance(artifact_digests, dict):
            return False
        observed = {
            path.relative_to(scenario_root).as_posix(): sha256_file(path)
            for path in regular_tree_files(scenario_root)
            if path != scenario_report
        }
        if observed != artifact_digests:
            return False
        if keyboard_scenario is not None:
            interaction = embedded.get("interaction")
            evidence = (
                interaction.get("evidence") if isinstance(interaction, dict) else None
            )
            classification = embedded.get("classification")
            boundaries = (
                classification.get("boundaries")
                if isinstance(classification, dict)
                else None
            )
            classified_checks = (
                boundaries.get("interaction") if isinstance(boundaries, dict) else None
            )
            if (
                not isinstance(evidence, dict)
                or not isinstance(keyboard_scenario_sha256, str)
                or not runner.keyboard_embedded_checks_match(
                    evidence, keyboard_scenario, keyboard_scenario_sha256
                )
                or classified_checks != evidence.get("checks")
                or not runner.keyboard_artifact_evidence_matches(
                    evidence, scenario_root
                )
            ):
                return False
    return input_checksum_validation(root / "inputs", source)


def network_profile_validation(
    payload: dict[str, Any],
    record: dict[str, Any],
) -> bool:
    """Bind every scenario to the selected frozen client option profile."""
    profile_name = record.get("network_profile")
    try:
        expected_options = list(live_config.network_profile(str(profile_name)).client_options())
    except live_config.LiveConfigError:
        return False
    scenarios = payload.get("scenarios")
    return isinstance(scenarios, list) and bool(scenarios) and all(
        isinstance(scenario, dict)
        and scenario.get("network_profile") == profile_name
        and isinstance(scenario.get("client"), dict)
        and scenario["client"].get("network_options") == expected_options
        for scenario in scenarios
    )


def report_validation(
    run: str,
    record: dict[str, Any],
    *,
    inspect_current_images: bool = False,
) -> tuple[str, str, dict[str, bool]]:
    report = result_path(run)
    report_sha256 = ""
    report_result_value = "missing"
    checks: dict[str, bool] = {}
    if not report.exists() or report.is_symlink() or not report.is_file():
        return report_result_value, report_sha256, checks
    ensure_private_directory(report.parent)
    ensure_private_regular(report)
    try:
        payload = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return report_result_value, report_sha256, checks
    if not isinstance(payload, dict):
        return report_result_value, report_sha256, checks
    report_sha256 = sha256_file(report)
    report_result_value = str(payload.get("result", "missing"))
    invocation = payload.get("invocation")
    source = payload.get("source")
    selection = source.get("selection") if isinstance(source, dict) else None
    provenance = record["input_provenance"]
    expected_input_provenance = {
        key: value
        for key, value in provenance.items()
        if key not in {"input_manifest_sha256", "input_tree_sha256", "path"}
    }
    checks = {
        "alpha_scenarios": isinstance(invocation, dict)
        and invocation.get("alpha_scenarios") == record["alpha_scenarios"],
        "application": payload.get("application") == record["application"]
        and isinstance(invocation, dict)
        and invocation.get("application") == record["application"],
        "encoding": payload.get("encoding") == record["encoding"],
        "h264_client_policy": payload.get("h264_client_policy")
        == record["h264_client_policy"]
        and isinstance(invocation, dict)
        and invocation.get("h264_client_policy") == record["h264_client_policy"],
        "harness_sha256": isinstance(source, dict)
        and source.get("harness_sha256") == record["harness_sha256"],
        "job_id": isinstance(invocation, dict)
        and invocation.get("job_id") == record["job_id"],
        "lifecycle": payload.get("lifecycle_profile") == record["lifecycle"]
        and isinstance(invocation, dict)
        and invocation.get("lifecycle") == record["lifecycle"],
        "network_profile": payload.get("network_profile")
        == record["network_profile"]
        and isinstance(invocation, dict)
        and invocation.get("network_profile") == record["network_profile"]
        and network_profile_validation(payload, record),
        "result": report_result_value == "passed",
        "run_id": isinstance(invocation, dict) and invocation.get("run_id") == run,
        "render_node": isinstance(invocation, dict)
        and invocation.get("render_node") == record["render_node"],
        "reviewed_selection": isinstance(record.get("selection"), str)
        and bool(SELECTOR_RE.fullmatch(record["selection"])),
        "selection": isinstance(invocation, dict)
        and invocation.get("selection") == record["selection"],
        "selection_provenance": isinstance(selection, dict)
        and selection.get("name") == provenance["server_selection"]
        and selection.get("digest") == provenance["server_selection_sha256"]
        and isinstance(selection.get("resolution"), dict)
        and selection["resolution"].get("resolution_sha256")
        == provenance["server_selection_resolution_sha256"],
        "source_provenance": isinstance(source, dict)
        and source.get("archive_sha256") == provenance["source_archive_sha256"]
        and source.get("commit") == provenance["source_commit"]
        and source.get("fork_master") == provenance["source_commit"]
        and source.get("workflow_sha256") == provenance["source_workflow_sha256"]
        and source.get("input_manifest_sha256")
        == provenance["input_manifest_sha256"]
        and source.get("input_tree_sha256") == provenance["input_tree_sha256"]
        and source.get("zed_sha256") == provenance["zed_binary_sha256"]
        and source.get("zed_archive_sha256")
        == provenance["zed_archive_sha256"]
        and source.get("input_provenance") == expected_input_provenance,
        "image_provenance": image_provenance_validation(payload, provenance),
        "supervisor_sha256": isinstance(source, dict)
        and source.get("supervisor_sha256") == record["supervisor_sha256"],
        "background_supervisor_sha256": isinstance(source, dict)
        and source.get("background_supervisor_sha256")
        == record["background_supervisor_sha256"],
    }
    try:
        checks["evidence_tree"] = evidence_tree_validation(payload, report)
    except (JobError, OSError, TypeError, ValueError):
        checks["evidence_tree"] = False
    if inspect_current_images:
        checks["current_images"] = current_image_validation(payload)
    return report_result_value, report_sha256, checks


def collect(args: argparse.Namespace) -> int:
    prepare_private_state()
    run = validate_name(args.run)
    record = load_record(run)
    with lifecycle_lock(run):
        if log_path(run).exists() or status_path(run).exists():
            raise JobError(f"collected artifacts already exist: {run}")
        state = background_job.process_state(record)
        if state["state"] == "running":
            raise JobError(f"live job is still running: {run}")
        if state["state"] != "completed":
            raise JobError(f"live job disappeared without completion: {run}")
        runtime_log = background_job.runtime_log_path(record)
        ensure_private_regular(runtime_log)
        log_payload = runtime_log.read_bytes()
        report_value, report_sha256, report_checks = report_validation(
            run,
            record,
            inspect_current_images=True,
        )
        objects = owned_objects(run)
        log_sha256 = hashlib.sha256(log_payload).hexdigest()
        validation_ok = bool(report_checks) and all(report_checks.values())
        passed = (
            state["exit_code"] == 0
            and validation_ok
            and not objects["containers"]
            and not objects["networks"]
        )
        status_payload: dict[str, Any] = {
            "background_supervisor_sha256": record["background_supervisor_sha256"],
            "collected_at": datetime.now(UTC).isoformat(),
            "exit_code": state["exit_code"],
            "finished_at": state.get("finished_at", ""),
            "job_id": record["job_id"],
            "harness_sha256": record["harness_sha256"],
            "input_provenance": record["input_provenance"],
            "log_sha256": log_sha256,
            "logs_ok": True,
            "owner": OWNER,
            "owned_objects_remaining": objects,
            "process_pid": state["pid"],
            "report": str(result_path(run)),
            "report_result": report_value,
            "report_checks": report_checks,
            "report_sha256": report_sha256,
            "result": "success" if passed else "failed",
            "run": run,
            "runner_sha256": record["runner_sha256"],
            "schema": 3,
            "supervisor_sha256": record["supervisor_sha256"],
            "validation_ok": validation_ok,
        }
        publish_bytes(log_path(run), log_payload)
        try:
            publish_json(status_path(run), status_payload)
        except BaseException:
            log_path(run).unlink(missing_ok=True)
            raise
        print(f"saved {log_path(run)} and {status_path(run)}")
        return 0 if passed else 1


def wait(args: argparse.Namespace) -> int:
    prepare_private_state()
    record = load_record(args.run)
    try:
        background_job.wait_process(record, interval=1.0)
    except background_job.BackgroundJobError as error:
        raise JobError(str(error)) from error
    return collect(args)


def verify_collected(run: str, record: dict[str, Any]) -> None:
    status_record = load_private_json(status_path(run))
    ensure_private_regular(log_path(run))
    expected_status = {
        "background_supervisor_sha256": record["background_supervisor_sha256"],
        "job_id": record["job_id"],
        "harness_sha256": record["harness_sha256"],
        "input_provenance": record["input_provenance"],
        "owner": OWNER,
        "process_pid": record["process"]["pid"],
        "run": run,
        "runner_sha256": record["runner_sha256"],
        "schema": 3,
        "supervisor_sha256": record["supervisor_sha256"],
    }
    for key, value in expected_status.items():
        if status_record.get(key) != value:
            raise JobError(
                f"collected live-job status does not match ownership field {key}"
            )
    if status_record.get("log_sha256") != sha256_file(log_path(run)):
        raise JobError("collected live-job log digest does not match its status")
    objects = status_record.get("owned_objects_remaining")
    exit_code = status_record.get("exit_code")
    if (
        status_record.get("logs_ok") is not True
        or status_record.get("report") != str(result_path(run))
        or isinstance(exit_code, bool)
        or not isinstance(exit_code, int)
        or not isinstance(objects, dict)
        or set(objects) != {"containers", "networks"}
        or not all(
            isinstance(values, list)
            and all(isinstance(value, str) and SHA256_RE.fullmatch(value) for value in values)
            for values in objects.values()
        )
    ):
        raise JobError("collected live-job status has invalid lifecycle evidence")
    recorded_checks = status_record.get("report_checks")
    if not isinstance(recorded_checks, dict) or not all(
        isinstance(key, str) and isinstance(value, bool)
        for key, value in recorded_checks.items()
    ):
        raise JobError("collected live-job report checks are invalid")
    validation_ok = bool(recorded_checks) and all(recorded_checks.values())
    if (
        not isinstance(status_record.get("validation_ok"), bool)
        or status_record["validation_ok"] is not validation_ok
    ):
        raise JobError("collected live-job report validation is inconsistent")

    report = result_path(run)
    recorded_report_sha256 = status_record.get("report_sha256")
    recorded_report_result = status_record.get("report_result")
    if recorded_report_sha256:
        if (
            not isinstance(recorded_report_sha256, str)
            or not SHA256_RE.fullmatch(recorded_report_sha256)
            or not isinstance(recorded_report_result, str)
            or not recorded_checks
        ):
            raise JobError("collected live-job report identity is invalid")
        ensure_private_directory(report.parent)
        ensure_private_regular(report)
        if sha256_file(report) != recorded_report_sha256:
            raise JobError("collected live-job report digest does not match its status")
        try:
            report_payload = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise JobError("collected live-job report is no longer valid JSON") from error
        if (
            not isinstance(report_payload, dict)
            or str(report_payload.get("result", "missing"))
            != recorded_report_result
        ):
            raise JobError("collected live-job report result does not match its status")
    else:
        if (
            recorded_report_sha256 != ""
            or recorded_report_result != "missing"
            or recorded_checks
        ):
            raise JobError("collected live-job missing-report evidence is inconsistent")
        if report.exists() or report.is_symlink():
            ensure_private_regular(report)
            try:
                report_payload = json.loads(report.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                pass
            else:
                if isinstance(report_payload, dict):
                    raise JobError(
                        "collected live-job unvalidated report changed after collection"
                    )
    expected_result = (
        "success"
        if exit_code == 0
        and validation_ok
        and status_record.get("owned_objects_remaining")
        == {"containers": [], "networks": []}
        else "failed"
    )
    if status_record.get("result") != expected_result:
        raise JobError("collected live-job result is inconsistent with validation")


def remove_owned_objects(run: str) -> None:
    ledger = object_ledger_entries(run)
    recorded_by_kind = {
        kind: [entry for entry in ledger if entry["kind"] == kind]
        for kind in ("container", "network")
    }
    removals: list[tuple[str, str]] = []
    for kind in ("container", "network"):
        actual: dict[str, tuple[str, dict[str, str]]] = {}
        for object_id in podman_ids(kind, run):
            immutable_id, name, labels = podman_object(kind, object_id)
            actual[immutable_id] = (name, labels)
        matched: set[str] = set()
        for entry in recorded_by_kind[kind]:
            expected_id = entry["id"]
            candidates = [expected_id] if expected_id else [
                object_id
                for object_id, (name, _labels) in actual.items()
                if name == entry["name"]
            ]
            candidates = [candidate for candidate in candidates if candidate in actual]
            if not candidates:
                continue
            if len(candidates) != 1:
                raise JobError(f"live Podman ledger identity is ambiguous: {entry['name']}")
            object_id = candidates[0]
            name, labels = actual[object_id]
            labels_match = (
                labels == entry["labels"]
                if expected_id
                else {
                    key: value
                    for key, value in labels.items()
                    if key.startswith("io.xpra.fork-maintenance.")
                }
                == entry["labels"]
            )
            if (
                name != entry["name"]
                or not labels_match
                or (expected_id and object_id != expected_id)
            ):
                raise JobError(f"live Podman object no longer matches its ledger: {entry['name']}")
            matched.add(object_id)
            removals.append((kind, object_id))
        extras = set(actual) - matched
        if extras:
            raise JobError(f"unrecorded live Podman {kind} objects require owner review: {sorted(extras)}")
    for kind, object_id in removals:
        if kind == "container":
            command(["podman", "rm", "--force", object_id])
        else:
            command(["podman", "network", "rm", object_id])
    remaining = owned_objects(run)
    if remaining["containers"] or remaining["networks"]:
        raise JobError(f"owned Podman objects remain after cleanup: {remaining}")


def remove(args: argparse.Namespace) -> int:
    prepare_private_state()
    run = validate_name(args.run)
    with lifecycle_lock(run):
        transaction_path = remove_transaction_path(run)
        if transaction_path.exists() or transaction_path.is_symlink():
            transaction = load_remove_transaction(run)
            record = transaction["record"]
        else:
            record = load_record(run, require_current=False)
            verify_collected(run, record)
            state = background_job.process_state(record, require_current=False)
            if state["state"] == "running":
                raise JobError("collect and wait for the live job before removing it")
            if state["state"] not in {"completed", "lost"}:
                raise JobError(
                    f"live job has an unsupported process state: {state['state']}"
                )
            freeze_owner = freeze_record_path(run)
            if freeze_owner.exists() or freeze_owner.is_symlink():
                freeze_record = load_freeze_record(run)
                if freeze_record["job_id"] != record["job_id"]:
                    raise JobError("live run and input-freeze owner job IDs differ")
                if freeze_process_state(freeze_record)["state"] == "running":
                    raise JobError("live input-freeze transition is still running")
                staging = Path(str(freeze_record["staging"]))
                if staging.exists() or staging.is_symlink():
                    raise JobError(
                        "live input-freeze staging remains; use live-abort for review"
                    )
            transaction = publish_remove_transaction(run, record)
        verify_collected(run, record)
        remove_owned_objects(run)
        cleanup_removal_runtime(run, transaction)
    print(f"removed owned runtime state for {run}; evidence was retained")
    return 0


def abort(args: argparse.Namespace) -> int:
    prepare_private_state()
    run = validate_name(args.run)
    with lifecycle_lock(run):
        for path in (log_path(run), status_path(run)):
            if path.exists() or path.is_symlink():
                raise JobError(f"run already has collected evidence; use live-remove: {run}")
        owner = record_path(run)
        freeze_owner = freeze_record_path(run)
        if not owner.exists() and not owner.is_symlink():
            if not freeze_owner.exists() and not freeze_owner.is_symlink():
                prelaunch = load_freeze_prelaunch(run)
                if freeze_prelaunch_active(prelaunch):
                    raise JobError("live input-freeze starter is still active")
                forbidden = (
                    freeze_completion_path(run),
                    freeze_result_path(run),
                    record_path(run),
                    runtime_log_path(run),
                    completion_path(run),
                    log_path(run),
                    status_path(run),
                    Path(str(prelaunch["staging"])),
                    Path(str(prelaunch["result"])),
                )
                if any(path.exists() or path.is_symlink() for path in forbidden):
                    raise JobError(
                        "ownerless live input-freeze prelaunch has executed or ambiguous state"
                    )
                runtime = freeze_runtime_log_path(run)
                if runtime.exists() or runtime.is_symlink():
                    ensure_private_regular(runtime)
                    runtime.unlink()
                remove_freeze_prelaunch(run)
                print(f"discarded recoverable live input-freeze prelaunch for {run}")
                return 0
            freeze_record = load_freeze_record(run)
            freeze_state = freeze_process_state(freeze_record)
            if freeze_state["state"] == "running":
                try:
                    background_job.terminate(
                        freeze_record,
                        require_current=False,
                    )
                except background_job.BackgroundJobError as error:
                    raise JobError(str(error)) from error
            elif freeze_state["state"] not in {"completed", "lost"}:
                raise JobError(
                    "live input-freeze has an unsupported process state: "
                    f"{freeze_state['state']}"
                )
            cleanup_freeze_state(
                freeze_record,
                remove_input_directories=True,
            )
            if freeze_prelaunch_path(run).exists() or freeze_prelaunch_path(run).is_symlink():
                matching_freeze_prelaunch(freeze_record)
                remove_freeze_prelaunch(run)
            print(f"aborted and removed owned live input freeze for {run}")
            return 0
        record = load_record(run, require_current=False)
        freeze_record: dict[str, Any] | None = None
        if freeze_owner.exists() or freeze_owner.is_symlink():
            freeze_record = load_freeze_record(run)
            if freeze_record["job_id"] != record["job_id"]:
                raise JobError("live run and input-freeze owner job IDs differ")
            if freeze_process_state(freeze_record)["state"] == "running":
                raise JobError("live input-freeze transition is still running")
        state = background_job.process_state(record, require_current=False)
        if state["state"] == "completed" and record_is_current(record):
            raise JobError("completed live jobs must be collected, not aborted")
        if state["state"] == "running":
            try:
                background_job.terminate(record, require_current=False)
            except background_job.BackgroundJobError as error:
                raise JobError(str(error)) from error
        elif state["state"] not in {"completed", "lost"}:
            raise JobError(f"live job has an unsupported process state: {state['state']}")
        remove_owned_objects(run)
        result_directory = RESULT_ROOT / run
        if result_directory.exists() or result_directory.is_symlink():
            if result_directory.is_symlink():
                raise JobError(f"refusing symlinked live result directory: {result_directory}")
            ensure_private_directory(result_directory)
            shutil.rmtree(result_directory)
        for path in (runtime_log_path(run), completion_path(run), record_path(run)):
            path.unlink(missing_ok=True)
        if freeze_record is not None:
            cleanup_freeze_state(freeze_record, remove_input_directories=False)
        if freeze_prelaunch_path(run).exists() or freeze_prelaunch_path(run).is_symlink():
            matching_freeze_prelaunch_to_main(record)
            remove_freeze_prelaunch(run)
        print(f"aborted and removed owned runtime state for {run}")
        return 0


def doctor(_args: argparse.Namespace) -> int:
    prepare_private_state()
    for executable in ("git", "podman"):
        if command(["sh", "-c", f"command -v {executable}"], check=False).returncode:
            raise JobError(f"required command is unavailable: {executable}")
    command([sys.executable, "-c", "from PIL import Image"])
    print("durable live-job prerequisites: available")
    for label, path in (
        ("render node", Path("/dev/dri/renderD128")),
        ("Zed directory", Path.home() / ".local" / "zed.app"),
    ):
        state = "available" if path.exists() else "unavailable (optional)"
        print(f"{label}: {state} ({path})")
    return 0


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    commands = value.add_subparsers(dest="command", required=True)
    freeze_parser = commands.add_parser("_freeze", help=argparse.SUPPRESS)
    freeze_parser.add_argument("run")
    freeze_parser.add_argument("--application", choices=APPLICATIONS, required=True)
    freeze_parser.add_argument("--job-id", required=True)
    freeze_parser.add_argument("--selection", required=True)
    freeze_parser.add_argument("--zed-directory")
    freeze_parser.set_defaults(handler=freeze_worker)
    commands.add_parser("doctor").set_defaults(handler=doctor)
    commands.add_parser("environment-create").set_defaults(handler=environment_create)
    commands.add_parser("environment-check").set_defaults(handler=environment_check)
    commands.add_parser("environment-path").set_defaults(handler=environment_show_path)
    start_parser = commands.add_parser("start")
    start_parser.add_argument("run")
    start_parser.add_argument("--application", choices=APPLICATIONS, default="zed")
    start_parser.add_argument("--encoding", choices=("rgb", "h264"), required=True)
    start_parser.add_argument(
        "--h264-client-policy",
        choices=H264_ACCEPTANCE_POLICIES,
        default="strict",
    )
    start_parser.add_argument(
        "--lifecycle", choices=LIFECYCLES, default="application-exit"
    )
    start_parser.add_argument("--selection", required=True)
    start_parser.add_argument(
        "--alpha-scenarios", choices=ALPHA_SCENARIOS, default="default"
    )
    start_parser.add_argument(
        "--network-profile",
        choices=NETWORK_PROFILES,
        default=DEFAULT_NETWORK_PROFILE,
    )
    start_parser.add_argument("--render-node")
    start_parser.add_argument("--zed-directory")
    start_parser.set_defaults(handler=start)
    for name, handler in (
        ("status", status),
        ("logs", logs),
        ("wait", wait),
        ("collect", collect),
        ("remove", remove),
        ("abort", abort),
    ):
        subparser = commands.add_parser(name)
        subparser.add_argument("run")
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
