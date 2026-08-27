#!/usr/bin/env python3
# Copyright (C) 2026 kogeler
"""Small, ownership-checked supervisor for durable local commands."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = 1
SUPERVISOR = Path(__file__).resolve()
_CHILDREN: dict[int, subprocess.Popen[bytes]] = {}


class BackgroundJobError(RuntimeError):
    """Raised when a background process cannot be trusted or controlled."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def ensure_private_directory(path: Path, *, create: bool = False) -> None:
    if path.is_symlink():
        raise BackgroundJobError(f"private directory is a symlink: {path}")
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        info = path.lstat()
    except OSError as error:
        raise BackgroundJobError(f"private directory is unavailable: {path}") from error
    if not path.is_dir() or info.st_uid != os.getuid():
        raise BackgroundJobError(f"private directory is not owned by this user: {path}")
    if create and info.st_mode & 0o077:
        path.chmod(0o700)
        info = path.lstat()
    if info.st_mode & 0o077:
        raise BackgroundJobError(
            f"unsafe private directory mode for {path}: {info.st_mode & 0o777:o}"
        )


def ensure_private_regular(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise BackgroundJobError(f"private file is unavailable: {path}") from error
    if path.is_symlink() or not path.is_file() or info.st_uid != os.getuid():
        raise BackgroundJobError(f"untrusted private file: {path}")
    if info.st_mode & 0o077 or info.st_nlink != 1:
        raise BackgroundJobError(f"unsafe private file metadata: {path}")


def _atomic_json(path: Path, payload: dict[str, Any], *, replace: bool) -> None:
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
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    except FileExistsError as error:
        raise BackgroundJobError(f"refusing to overwrite existing file: {path}") from error
    finally:
        temporary.unlink(missing_ok=True)


def publish_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_json(path, payload, replace=False)


def load_json(path: Path) -> dict[str, Any]:
    ensure_private_regular(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BackgroundJobError(f"cannot read private JSON file: {path}") from error
    if not isinstance(payload, dict):
        raise BackgroundJobError(f"private JSON file is not an object: {path}")
    return payload


def process_identity(pid: int) -> tuple[str, str, str] | None:
    """Return state, process-group ID and start ticks for an extant process."""
    try:
        value = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (FileNotFoundError, ProcessLookupError):
        return None
    except OSError as error:
        raise BackgroundJobError(f"cannot inspect process {pid}: {error}") from error
    end = value.rfind(")")
    fields = value[end + 2 :].split() if end >= 0 else []
    if len(fields) < 20:
        raise BackgroundJobError(f"invalid /proc status for process {pid}")
    return fields[0], fields[2], fields[19]


def _process_record(
    record: dict[str, Any], *, require_current: bool = True
) -> dict[str, Any]:
    process = record.get("process")
    if not isinstance(process, dict):
        raise BackgroundJobError("background owner record has no process identity")
    pid = process.get("pid")
    if not isinstance(pid, int) or pid <= 1:
        raise BackgroundJobError("background owner record has an invalid PID")
    if process.get("process_group") != pid:
        raise BackgroundJobError("background owner record has an invalid process group")
    start_ticks = process.get("start_ticks")
    if not isinstance(start_ticks, str) or not start_ticks.isdigit():
        raise BackgroundJobError("background owner record has invalid process start ticks")
    supervisor_sha256 = process.get("supervisor_sha256")
    if not isinstance(supervisor_sha256, str) or len(supervisor_sha256) != 64:
        raise BackgroundJobError("background owner record has an invalid supervisor digest")
    if require_current and supervisor_sha256 != sha256_file(SUPERVISOR):
        raise BackgroundJobError("background supervisor changed while the job was owned")
    for key in ("runtime_log", "completion"):
        value = process.get(key)
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise BackgroundJobError(f"background owner record has invalid {key}")
    return process


def verify_running_process(
    record: dict[str, Any], *, require_current: bool = True
) -> bool:
    process = _process_record(record, require_current=require_current)
    pid = int(process["pid"])
    identity = process_identity(pid)
    if identity is None:
        return False
    state, process_group, start_ticks = identity
    if state == "Z":
        return False
    if process_group != str(pid) or start_ticks != process["start_ticks"]:
        raise BackgroundJobError(f"PID {pid} no longer belongs to this background job")
    try:
        command_line = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError as error:
        raise BackgroundJobError(f"cannot inspect command line for PID {pid}") from error
    decoded = [item.decode(errors="replace") for item in command_line if item]
    if not decoded:
        return False
    if "_run" not in decoded or str(SUPERVISOR) not in decoded:
        raise BackgroundJobError(f"PID {pid} is not the recorded background supervisor")
    return True


def _reap_local_child(pid: int) -> None:
    process = _CHILDREN.get(pid)
    if process is not None and process.poll() is not None:
        _CHILDREN.pop(pid, None)


def completion_path(record: dict[str, Any], *, require_current: bool = True) -> Path:
    return Path(
        str(_process_record(record, require_current=require_current)["completion"])
    )


def runtime_log_path(record: dict[str, Any], *, require_current: bool = True) -> Path:
    return Path(
        str(_process_record(record, require_current=require_current)["runtime_log"])
    )


def completion(
    record: dict[str, Any], *, require_current: bool = True
) -> dict[str, Any] | None:
    path = completion_path(record, require_current=require_current)
    if not path.exists() and not path.is_symlink():
        return None
    payload = load_json(path)
    process = _process_record(record, require_current=require_current)
    expected = {
        "schema": SCHEMA,
        "pid": process["pid"],
        "start_ticks": process["start_ticks"],
        "supervisor_sha256": process["supervisor_sha256"],
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise BackgroundJobError(f"background completion mismatch for {key}")
    exit_code = payload.get("exit_code")
    if not isinstance(exit_code, int):
        raise BackgroundJobError("background completion has an invalid exit code")
    return payload


def process_state(
    record: dict[str, Any], *, require_current: bool = True
) -> dict[str, Any]:
    finished = completion(record, require_current=require_current)
    if finished is not None:
        _reap_local_child(
            int(_process_record(record, require_current=require_current)["pid"])
        )
        return {"state": "completed", **finished}
    if verify_running_process(record, require_current=require_current):
        process = _process_record(record, require_current=require_current)
        return {"state": "running", "pid": process["pid"], "exit_code": None}
    process = _process_record(record, require_current=require_current)
    return {"state": "lost", "pid": process["pid"], "exit_code": None}


def launch(
    *,
    owner_path: Path,
    runtime_log: Path,
    completion_file: Path,
    record: dict[str, Any],
    argv: list[str],
    cwd: Path,
    environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Publish an owner record and release a new session only after publication."""
    if not argv:
        raise BackgroundJobError("background command is empty")
    for path in (owner_path, runtime_log, completion_file):
        ensure_private_directory(path.parent)
        if path.exists() or path.is_symlink():
            raise BackgroundJobError(f"background artifact already exists: {path}")
    read_gate, write_gate = os.pipe()
    log_fd = os.open(runtime_log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    wrapper = [
        sys.executable,
        str(SUPERVISOR),
        "_run",
        "--gate-fd",
        str(read_gate),
        "--completion",
        str(completion_file),
        "--cwd",
        str(cwd),
        "--",
        *argv,
    ]
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            wrapper,
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=subprocess.STDOUT,
            close_fds=True,
            pass_fds=(read_gate,),
            start_new_session=True,
            env=environment,
        )
        os.close(read_gate)
        read_gate = -1
        identity = process_identity(process.pid)
        if identity is None:
            raise BackgroundJobError("background supervisor exited before publication")
        state, process_group, start_ticks = identity
        if state == "Z" or process_group != str(process.pid):
            raise BackgroundJobError("background supervisor has an invalid process group")
        owned = dict(record)
        owned["process"] = {
            "completion": str(completion_file),
            "pid": process.pid,
            "process_group": process.pid,
            "runtime_log": str(runtime_log),
            "start_ticks": start_ticks,
            "supervisor_sha256": sha256_file(SUPERVISOR),
        }
        publish_json(owner_path, owned)
        os.close(write_gate)
        write_gate = -1
        _CHILDREN[process.pid] = process
        return owned
    except BaseException:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        runtime_log.unlink(missing_ok=True)
        owner_path.unlink(missing_ok=True)
        completion_file.unlink(missing_ok=True)
        raise
    finally:
        os.close(log_fd)
        if read_gate >= 0:
            os.close(read_gate)
        if write_gate >= 0:
            os.close(write_gate)


def wait_process(record: dict[str, Any], *, interval: float = 0.2) -> dict[str, Any]:
    while True:
        state = process_state(record)
        if state["state"] == "completed":
            return state
        if state["state"] == "lost":
            raise BackgroundJobError("background process disappeared without completion")
        time.sleep(interval)


def process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate(
    record: dict[str, Any], *, grace: float = 5.0, require_current: bool = True
) -> None:
    state = process_state(record, require_current=require_current)
    if state["state"] == "completed":
        return
    if state["state"] != "running":
        raise BackgroundJobError("background process disappeared without completion")
    process = _process_record(record, require_current=require_current)
    pid = int(process["pid"])
    os.killpg(pid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        _reap_local_child(pid)
        if not process_group_exists(pid):
            return
        time.sleep(0.05)
    os.killpg(pid, signal.SIGKILL)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        _reap_local_child(pid)
        if not process_group_exists(pid):
            return
        time.sleep(0.05)
    raise BackgroundJobError(f"background process group {pid} did not stop")


def _run(args: argparse.Namespace) -> int:
    with os.fdopen(args.gate_fd, "rb", closefd=True) as gate:
        gate.read()
    started_at = utc_now()
    exit_code = 127
    try:
        result = subprocess.run(args.argv, cwd=args.cwd, check=False)
        exit_code = result.returncode
    except OSError as error:
        print(f"background command could not start: {error}", file=sys.stderr, flush=True)
    identity = process_identity(os.getpid())
    if identity is None:
        return exit_code
    _state, _process_group, start_ticks = identity
    _atomic_json(
        args.completion,
        {
            "exit_code": exit_code,
            "finished_at": utc_now(),
            "pid": os.getpid(),
            "schema": SCHEMA,
            "started_at": started_at,
            "start_ticks": start_ticks,
            "supervisor_sha256": sha256_file(SUPERVISOR),
        },
        replace=False,
    )
    return exit_code


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    commands = value.add_subparsers(dest="command", required=True)
    run = commands.add_parser("_run", help=argparse.SUPPRESS)
    run.add_argument("--gate-fd", type=int, required=True)
    run.add_argument("--completion", type=Path, required=True)
    run.add_argument("--cwd", type=Path, required=True)
    run.add_argument("argv", nargs=argparse.REMAINDER)
    run.set_defaults(handler=_run)
    return value


def main() -> int:
    os.umask(0o077)
    args = parser().parse_args()
    if getattr(args, "argv", None) and args.argv[0] == "--":
        args.argv = args.argv[1:]
    try:
        return int(args.handler(args))
    except (BackgroundJobError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
