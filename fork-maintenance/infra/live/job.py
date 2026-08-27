#!/usr/bin/env python3
# Copyright (C) 2026 kogeler
"""Supervise durable Xpra live runs with owned local processes and Podman labels."""

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

from profiles import (
    ALPHA_SCENARIOS,
    APPLICATIONS,
    H264_CLIENT_POLICIES,
    LIFECYCLES,
    ProfileError,
    validate_profile,
)

INFRA_ROOT = Path(__file__).resolve().parent
LAB_ROOT = INFRA_ROOT.parent.parent
PROJECT_ROOT = LAB_ROOT.parent
TOOLS_ROOT = LAB_ROOT / "tools"
sys.path.insert(0, str(TOOLS_ROOT))

import background_job

RUNNER = INFRA_ROOT / "run.py"
SUPERVISOR = Path(__file__).resolve()
BACKGROUND_SUPERVISOR = TOOLS_ROOT / "background_job.py"
SELECTION_TOOL = LAB_ROOT / "infra" / "upstream-tests" / "selection.py"
ARTIFACT_ROOT = PROJECT_ROOT / ".artifacts"
STATE_ROOT = ARTIFACT_ROOT / "fork-maintenance"
JOB_ROOT = STATE_ROOT / "jobs" / "live"
RESULT_ROOT = STATE_ROOT / "live-results"
VENV_ROOT = STATE_ROOT / "venvs"
REQUIREMENTS = INFRA_ROOT / "requirements.txt"
PILLOW_VERSION = "12.1.1"
OWNER = "xpra-lab-live-job"
RETIRED_RECORD_POLICIES = {"mixed-hardware"}
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
SELECTOR_RE = re.compile(r"(?:cases|stacks)/[a-z0-9]+(?:-[a-z0-9]+)*")


class JobError(RuntimeError):
    """Raised when a durable live-job invariant is not satisfied."""


def command(
    argv: list[str],
    *,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        argv,
        check=False,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True,
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


def result_path(run: str) -> Path:
    return RESULT_ROOT / run / "report.json"


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
    for path in (STATE_ROOT, STATE_ROOT / "jobs", JOB_ROOT, RESULT_ROOT, VENV_ROOT):
        ensure_private_directory(path, create=True)


def environment_path() -> Path:
    if REQUIREMENTS.is_symlink() or not REQUIREMENTS.is_file():
        raise JobError(f"live requirements lock is unavailable: {REQUIREMENTS}")
    identity = hashlib.sha256()
    identity.update(REQUIREMENTS.read_bytes())
    identity.update(b"\0")
    identity.update(sys.version.encode())
    return VENV_ROOT / f"live-{identity.hexdigest()[:16]}"


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
    if destination.exists() or destination.is_symlink():
        validate_environment(destination)
        print(destination)
        return 0
    with tempfile.TemporaryDirectory(prefix=".live-venv-", dir=VENV_ROOT) as name:
        temporary = Path(name)
        command([sys.executable, "-m", "venv", str(temporary)])
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
        )
        try:
            temporary.rename(destination)
        except FileExistsError:
            validate_environment(destination)
        validate_environment(destination)
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
    ensure_private_directory(path.parent)
    if path.exists() or path.is_symlink():
        raise JobError(f"refusing to overwrite existing artifact: {path}")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    except FileExistsError as error:
        raise JobError(f"artifact publication raced: {path}") from error
    finally:
        temporary.unlink(missing_ok=True)


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


def validate_selector(selector: str | None) -> None:
    if selector is None:
        return
    if not SELECTOR_RE.fullmatch(selector):
        raise JobError(f"invalid case or stack selector: {selector!r}")
    command(
        [
            sys.executable,
            str(SELECTION_TOOL),
            "--lab-root",
            str(LAB_ROOT),
            "--selection",
            selector,
            "validate",
        ]
    )


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
    if record.get("schema") != 2:
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
    expected_hashes = {
        "runner_sha256": sha256_file(RUNNER),
        "supervisor_sha256": sha256_file(SUPERVISOR),
        "background_supervisor_sha256": sha256_file(BACKGROUND_SUPERVISOR),
    }
    for key, value in expected_hashes.items():
        recorded = str(record.get(key, ""))
        if not SHA256_RE.fullmatch(recorded) or (
            require_current and recorded != value
        ):
            raise JobError(f"live-job ownership record has an invalid {key}")
    job_id = record.get("job_id")
    try:
        parsed_job_id = uuid.UUID(str(job_id))
    except ValueError as error:
        raise JobError("live-job ownership record has an invalid job ID") from error
    if parsed_job_id.version != 4 or str(parsed_job_id) != job_id:
        raise JobError("live-job ownership record has a non-canonical job ID")
    try:
        background_job.process_state(record, require_current=require_current)
    except background_job.BackgroundJobError as error:
        raise JobError(str(error)) from error
    return record


def podman_ids(kind: str, run: str) -> list[str]:
    filters = [
        "--filter",
        "label=io.xpra.lab.owner=live",
        "--filter",
        f"label=io.xpra.lab.run-id={run}",
        "--quiet",
    ]
    if kind == "container":
        result = command(["podman", "ps", "--all", *filters])
    elif kind == "network":
        result = command(["podman", "network", "ls", *filters])
    else:
        raise JobError(f"unsupported Podman object kind: {kind}")
    return [line for line in result.stdout.splitlines() if line]


def podman_labels(kind: str, object_id: str) -> dict[str, str]:
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
        config = item.get("Config")
        labels = config.get("Labels") if isinstance(config, dict) else None
    else:
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
    return {str(key): str(value) for key, value in labels.items()}


def owned_objects(run: str) -> dict[str, list[str]]:
    return {
        "containers": podman_ids("container", run),
        "networks": podman_ids("network", run),
    }


def verify_owned_object(kind: str, object_id: str, run: str) -> None:
    labels = podman_labels(kind, object_id)
    expected = {
        "io.xpra.lab.owner": "live",
        "io.xpra.lab.run-id": run,
    }
    if any(labels.get(key) != value for key, value in expected.items()):
        raise JobError(f"refusing unowned {kind}: {object_id}")


def start(args: argparse.Namespace) -> int:
    prepare_private_state()
    run = validate_name(args.run)
    validate_selector(args.selection)
    try:
        validate_profile(
            application=args.application,
            lifecycle=args.lifecycle,
            encoding=args.encoding,
            h264_client_policy=args.h264_client_policy,
            alpha_scenarios=args.alpha_scenarios,
        )
    except ProfileError as error:
        raise JobError(str(error)) from error
    if not RUNNER.is_file() or RUNNER.is_symlink():
        raise JobError(f"live runner is unavailable: {RUNNER}")
    paths = (
        record_path(run),
        runtime_log_path(run),
        completion_path(run),
        log_path(run),
        status_path(run),
        RESULT_ROOT / run,
    )
    for path in paths:
        if path.exists() or path.is_symlink():
            raise JobError(f"run artifact already exists: {path}")
    job_id = str(uuid.uuid4())
    record = {
        "alpha_scenarios": args.alpha_scenarios,
        "application": args.application,
        "background_supervisor_sha256": sha256_file(BACKGROUND_SUPERVISOR),
        "created_at": datetime.now(UTC).isoformat(),
        "encoding": args.encoding,
        "h264_client_policy": args.h264_client_policy,
        "job_id": job_id,
        "lifecycle": args.lifecycle,
        "owner": OWNER,
        "result_report": str(result_path(run)),
        "run": run,
        "runner_sha256": sha256_file(RUNNER),
        "schema": 2,
        "selection": args.selection,
        "supervisor_sha256": sha256_file(SUPERVISOR),
    }
    argv = [
        sys.executable,
        str(RUNNER),
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
        "--run-id",
        run,
    ]
    if args.selection:
        argv.extend(("--selection", args.selection))
    if args.render_node:
        argv.extend(("--render-node", args.render_node))
    if args.zed_directory:
        argv.extend(("--zed-directory", args.zed_directory))
    environment = dict(os.environ)
    environment["XPRA_LAB_JOB_ID"] = job_id
    try:
        owned = background_job.launch(
            owner_path=record_path(run),
            runtime_log=runtime_log_path(run),
            completion_file=completion_path(run),
            record=record,
            argv=argv,
            cwd=PROJECT_ROOT,
            environment=environment,
        )
    except background_job.BackgroundJobError as error:
        raise JobError(str(error)) from error
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
    record = load_record(args.run, require_current=False)
    state = background_job.process_state(record, require_current=False)
    print(
        json.dumps(
            {
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
    record = load_record(args.run, require_current=False)
    path = background_job.runtime_log_path(record, require_current=False)
    ensure_private_regular(path)
    sys.stdout.buffer.write(path.read_bytes())
    return 0


def report_validation(run: str, record: dict[str, Any]) -> tuple[str, str, dict[str, bool]]:
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
        and source.get("harness_sha256") == record["runner_sha256"],
        "job_id": isinstance(invocation, dict)
        and invocation.get("job_id") == record["job_id"],
        "lifecycle": payload.get("lifecycle_profile") == record["lifecycle"]
        and isinstance(invocation, dict)
        and invocation.get("lifecycle") == record["lifecycle"],
        "result": report_result_value == "passed",
        "run_id": isinstance(invocation, dict) and invocation.get("run_id") == run,
        "selection": isinstance(invocation, dict)
        and invocation.get("selection") == (record["selection"] or "master"),
        "supervisor_sha256": isinstance(source, dict)
        and source.get("supervisor_sha256") == record["supervisor_sha256"],
        "background_supervisor_sha256": isinstance(source, dict)
        and source.get("background_supervisor_sha256")
        == record["background_supervisor_sha256"],
    }
    return report_result_value, report_sha256, checks


def collect(args: argparse.Namespace) -> int:
    prepare_private_state()
    run = validate_name(args.run)
    record = load_record(run)
    lock = JOB_ROOT / f".{run}.collect.lock"
    try:
        lock.mkdir()
    except FileExistsError as error:
        raise JobError(f"collection or abort is already active: {run}") from error
    try:
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
        report_value, report_sha256, report_checks = report_validation(run, record)
        objects = owned_objects(run)
        log_sha256 = hashlib.sha256(log_payload).hexdigest()
        status_payload: dict[str, Any] = {
            "background_supervisor_sha256": record["background_supervisor_sha256"],
            "collected_at": datetime.now(UTC).isoformat(),
            "exit_code": state["exit_code"],
            "finished_at": state.get("finished_at", ""),
            "job_id": record["job_id"],
            "log_sha256": log_sha256,
            "logs_ok": True,
            "owner": OWNER,
            "owned_objects_remaining": objects,
            "process_pid": state["pid"],
            "report": str(result_path(run)),
            "report_result": report_value,
            "report_checks": report_checks,
            "report_sha256": report_sha256,
            "result": "success" if state["exit_code"] == 0 else "failed",
            "run": run,
            "runner_sha256": record["runner_sha256"],
            "schema": 2,
            "supervisor_sha256": record["supervisor_sha256"],
        }
        publish_bytes(log_path(run), log_payload)
        try:
            publish_json(status_path(run), status_payload)
        except BaseException:
            log_path(run).unlink(missing_ok=True)
            raise
        passed = (
            state["exit_code"] == 0
            and bool(report_checks)
            and all(report_checks.values())
            and not objects["containers"]
            and not objects["networks"]
        )
        print(f"saved {log_path(run)} and {status_path(run)}")
        return 0 if passed else 1
    finally:
        lock.rmdir()


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
        "owner": OWNER,
        "process_pid": record["process"]["pid"],
        "run": run,
        "runner_sha256": record["runner_sha256"],
        "schema": 2,
        "supervisor_sha256": record["supervisor_sha256"],
    }
    for key, value in expected_status.items():
        if status_record.get(key) != value:
            raise JobError(
                f"collected live-job status does not match ownership field {key}"
            )
    if status_record.get("log_sha256") != sha256_file(log_path(run)):
        raise JobError("collected live-job log digest does not match its status")


def remove_owned_objects(run: str) -> None:
    for object_id in podman_ids("container", run):
        verify_owned_object("container", object_id, run)
        command(["podman", "rm", "--force", object_id])
    for object_id in podman_ids("network", run):
        verify_owned_object("network", object_id, run)
        command(["podman", "network", "rm", object_id])
    remaining = owned_objects(run)
    if remaining["containers"] or remaining["networks"]:
        raise JobError(f"owned Podman objects remain after cleanup: {remaining}")


def remove(args: argparse.Namespace) -> int:
    prepare_private_state()
    run = validate_name(args.run)
    record = load_record(run, require_current=False)
    verify_collected(run, record)
    state = background_job.process_state(record, require_current=False)
    if state["state"] == "running":
        raise JobError("collect and wait for the live job before removing it")
    remove_owned_objects(run)
    for path in (runtime_log_path(run), completion_path(run), record_path(run)):
        path.unlink(missing_ok=False)
    print(f"removed owned runtime state for {run}; evidence was retained")
    return 0


def abort(args: argparse.Namespace) -> int:
    prepare_private_state()
    run = validate_name(args.run)
    record = load_record(run, require_current=False)
    lock = JOB_ROOT / f".{run}.collect.lock"
    try:
        lock.mkdir()
    except FileExistsError as error:
        raise JobError(f"collection or abort is already active: {run}") from error
    try:
        for path in (log_path(run), status_path(run)):
            if path.exists() or path.is_symlink():
                raise JobError(f"run already has collected evidence; use live-remove: {run}")
        state = background_job.process_state(record, require_current=False)
        if state["state"] == "running":
            try:
                background_job.terminate(record, require_current=False)
            except background_job.BackgroundJobError as error:
                raise JobError(str(error)) from error
        remove_owned_objects(run)
        result_directory = RESULT_ROOT / run
        if result_directory.exists() or result_directory.is_symlink():
            if result_directory.is_symlink():
                raise JobError(f"refusing symlinked live result directory: {result_directory}")
            ensure_private_directory(result_directory)
            shutil.rmtree(result_directory)
        for path in (runtime_log_path(run), completion_path(run), record_path(run)):
            path.unlink(missing_ok=True)
        print(f"aborted and removed owned runtime state for {run}")
        return 0
    finally:
        lock.rmdir()


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
        choices=H264_CLIENT_POLICIES,
        default="strict",
    )
    start_parser.add_argument(
        "--lifecycle", choices=LIFECYCLES, default="application-exit"
    )
    start_parser.add_argument("--selection")
    start_parser.add_argument(
        "--alpha-scenarios", choices=ALPHA_SCENARIOS, default="default"
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
