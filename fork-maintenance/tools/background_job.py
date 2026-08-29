#!/usr/bin/env python3
# Copyright (C) 2026 kogeler
"""Small, ownership-checked supervisor for durable local commands."""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import secrets
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = 1
SUPERVISOR = Path(__file__).resolve()
OWNER_TOKEN_ENV = "XPRA_FORK_BACKGROUND_JOB_OWNER_TOKEN"
_AT_EMPTY_PATH = 0x1000
_CHILDREN: dict[int, subprocess.Popen[bytes]] = {}


class BackgroundJobError(RuntimeError):
    """Raised when a background process cannot be trusted or controlled."""


class LaunchStateRetained(BackgroundJobError):
    """A failed launch still has runtime state that must remain owned."""


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


def _link_anonymous_file(descriptor: int, directory: int, name: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        linkat = libc.linkat
    except AttributeError as error:
        raise BackgroundJobError("linkat(AT_EMPTY_PATH) is unavailable") from error
    linkat.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
    )
    linkat.restype = ctypes.c_int
    if linkat(descriptor, b"", directory, os.fsencode(name), _AT_EMPTY_PATH) == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise FileExistsError(error_number, os.strerror(error_number), name)
    raise OSError(error_number, os.strerror(error_number), name)


def publish_bytes(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    """Durably publish one immutable private file without a named staging path."""
    ensure_private_directory(path.parent)
    if path.name in {"", ".", ".."} or path.parent / path.name != path:
        raise BackgroundJobError(f"invalid publication path: {path}")
    if mode & 0o077 or mode & ~0o777:
        raise BackgroundJobError(f"unsafe publication mode: {mode:o}")
    directory = os.open(
        path.parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    descriptor = -1
    try:
        descriptor = os.open(
            ".",
            os.O_WRONLY | os.O_CLOEXEC | os.O_TMPFILE,
            mode,
            dir_fd=directory,
        )
        os.fchmod(descriptor, mode)
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise BackgroundJobError("anonymous publication made no write progress")
            remaining = remaining[written:]
        os.fsync(descriptor)
        _link_anonymous_file(descriptor, directory, path.name)
        os.fsync(directory)
    except FileExistsError as error:
        raise BackgroundJobError(f"refusing to overwrite existing file: {path}") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    publish_bytes(
        path,
        (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode(),
    )


def publish_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_json(path, payload)


def load_json(path: Path) -> dict[str, Any]:
    ensure_private_regular(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BackgroundJobError(f"cannot read private JSON file: {path}") from error
    if not isinstance(payload, dict):
        raise BackgroundJobError(f"private JSON file is not an object: {path}")
    return payload


def _process_details(pid: int) -> tuple[str, str, str, str, int] | None:
    """Return state, process group, session, start ticks and UID for a process."""
    try:
        directory = os.open(
            f"/proc/{pid}",
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
        )
    except (FileNotFoundError, ProcessLookupError):
        return None
    except OSError as error:
        raise BackgroundJobError(f"cannot inspect process {pid}: {error}") from error
    try:
        info = os.fstat(directory)
        descriptor = os.open("stat", os.O_RDONLY | os.O_CLOEXEC, dir_fd=directory)
        try:
            chunks: list[bytes] = []
            while block := os.read(descriptor, 4096):
                chunks.append(block)
                if sum(map(len, chunks)) > 64 * 1024:
                    raise BackgroundJobError(f"oversized /proc status for process {pid}")
        finally:
            os.close(descriptor)
    except (FileNotFoundError, ProcessLookupError):
        return None
    except OSError as error:
        raise BackgroundJobError(f"cannot inspect process {pid}: {error}") from error
    finally:
        os.close(directory)
    try:
        value = b"".join(chunks).decode("ascii")
    except UnicodeDecodeError as error:
        raise BackgroundJobError(f"invalid /proc status for process {pid}") from error
    end = value.rfind(")")
    fields = value[end + 2 :].split() if end >= 0 else []
    if len(fields) < 20:
        raise BackgroundJobError(f"invalid /proc status for process {pid}")
    return fields[0], fields[2], fields[3], fields[19], info.st_uid


def process_identity(pid: int) -> tuple[str, str, str] | None:
    """Return state, process-group ID and start ticks for an extant process."""
    details = _process_details(pid)
    if details is None:
        return None
    state, process_group, _session, start_ticks, _uid = details
    return state, process_group, start_ticks


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
    owner_token = process.get("owner_token")
    if owner_token is not None and (
        not isinstance(owner_token, str)
        or len(owner_token) != 64
        or any(character not in "0123456789abcdef" for character in owner_token)
    ):
        raise BackgroundJobError("background owner record has an invalid process token")
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


def _stop_failed_launch(
    process: subprocess.Popen[bytes],
    owned: dict[str, Any] | None,
    *,
    timeout: float = 2.0,
) -> bool:
    """SIGKILL a failed launch and prove its exact new session disappeared."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + timeout
    while True:
        process.poll()
        try:
            live = (
                _owned_live_process_group(owned, require_current=False)
                if owned is not None
                else process_group_exists(process.pid)
            )
        except BackgroundJobError:
            return False
        if not live:
            try:
                process.wait(timeout=0)
            except subprocess.TimeoutExpired:
                return False
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


def _reap_local_child(pid: int) -> None:
    process = _CHILDREN.get(pid)
    if process is not None and process.poll() is not None:
        _CHILDREN.pop(pid, None)


def _process_owner_tokens(pid: int) -> tuple[str, ...] | None:
    """Read exact background owner-token entries from one stable proc directory."""
    content: bytearray | None = None
    permission_error: PermissionError | None = None
    for attempt in range(5):
        try:
            directory = os.open(
                f"/proc/{pid}",
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
            )
        except (FileNotFoundError, ProcessLookupError):
            return None
        except OSError as error:
            raise BackgroundJobError(f"cannot inspect process {pid}: {error}") from error
        try:
            descriptor = os.open(
                "environ",
                os.O_RDONLY | os.O_CLOEXEC,
                dir_fd=directory,
            )
            try:
                candidate = bytearray()
                while block := os.read(descriptor, 4096):
                    candidate.extend(block)
                    if len(candidate) > 1024 * 1024:
                        raise BackgroundJobError(
                            f"oversized /proc environment for process {pid}"
                        )
            finally:
                os.close(descriptor)
            content = candidate
        except (FileNotFoundError, ProcessLookupError):
            return None
        except PermissionError as error:
            permission_error = error
        except OSError as error:
            raise BackgroundJobError(f"cannot inspect process {pid}: {error}") from error
        finally:
            os.close(directory)
        if content is not None:
            break
        details = _process_details(pid)
        if details is None or details[0] == "Z":
            return None
        if attempt < 4:
            time.sleep(0.01)
    if content is None:
        assert permission_error is not None
        raise BackgroundJobError(
            f"cannot inspect process {pid}: {permission_error}"
        ) from permission_error
    prefix = f"{OWNER_TOKEN_ENV}=".encode()
    values: list[str] = []
    for entry in bytes(content).split(b"\0"):
        if not entry.startswith(prefix):
            continue
        try:
            values.append(entry[len(prefix) :].decode("ascii"))
        except UnicodeDecodeError as error:
            raise BackgroundJobError(
                f"process {pid} has a non-ASCII background owner token"
            ) from error
    return tuple(values)


def _owned_live_process_group(record: dict[str, Any], *, require_current: bool) -> bool:
    """Return whether the recorded session still has a live process.

    The supervisor is the session and process-group leader. Linux keeps that
    numeric process-group identity reserved while any member remains, even if
    the leader was killed. Inspecting every same-UID member prevents a lost
    supervisor from making its still-running payload look safe to discard.
    """
    process = _process_record(record, require_current=require_current)
    process_group = int(process["process_group"])
    recorded_start = int(process["start_ticks"])
    owner_token = process.get("owner_token")
    if not process_group_exists(process_group):
        return False
    found = False
    live = False
    try:
        entries = tuple(Path("/proc").iterdir())
    except OSError as error:
        raise BackgroundJobError(f"cannot enumerate /proc: {error}") from error
    for entry in entries:
        if not entry.name.isdigit():
            continue
        member_pid = int(entry.name)
        try:
            if entry.stat(follow_symlinks=False).st_uid != os.getuid():
                continue
            details = _process_details(member_pid)
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError as error:
            raise BackgroundJobError(
                f"cannot inspect candidate process-group member {member_pid}: {error}"
            ) from error
        if details is None:
            continue
        state, member_group, session, start_ticks, uid = details
        if member_group != str(process_group):
            continue
        found = True
        if (
            uid != os.getuid()
            or session != str(process_group)
            or int(start_ticks) < recorded_start
        ):
            raise BackgroundJobError(
                f"process group {process_group} no longer belongs to this background job"
            )
        if state != "Z":
            tokens = _process_owner_tokens(member_pid)
            if tokens is None:
                continue
            if tokens != (owner_token,):
                raise BackgroundJobError(
                    f"process {member_pid} does not carry the recorded background owner token"
                )
            live = True
    if live:
        return True
    # A matching member may have exited between stat and environ inspection,
    # or may be a zombie awaiting its parent. Do not declare the group gone
    # while the kernel still reserves it; a later retry can prove emptiness.
    if found and process_group_exists(process_group):
        return True
    if process_group_exists(process_group):
        raise BackgroundJobError(
            f"cannot account for extant background process group {process_group}"
        )
    return False


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
    if process.get("owner_token") is not None:
        expected["owner_token"] = process["owner_token"]
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
    process = _process_record(record, require_current=require_current)
    pid = int(process["pid"])
    _reap_local_child(pid)
    if finished is not None:
        if _owned_live_process_group(record, require_current=require_current):
            return {
                "state": "running",
                "pid": pid,
                "exit_code": finished["exit_code"],
            }
        return {"state": "completed", **finished}
    if verify_running_process(record, require_current=require_current):
        return {"state": "running", "pid": process["pid"], "exit_code": None}
    if _owned_live_process_group(record, require_current=require_current):
        return {"state": "running", "pid": process["pid"], "exit_code": None}
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
    pass_fds: tuple[int, ...] = (),
) -> dict[str, Any]:
    """Publish an owner record and release a new session only after publication."""
    if not argv:
        raise BackgroundJobError("background command is empty")
    if len(set(pass_fds)) != len(pass_fds):
        raise BackgroundJobError("background inherited descriptors are duplicated")
    for descriptor in pass_fds:
        if not isinstance(descriptor, int) or descriptor < 0:
            raise BackgroundJobError("background inherited descriptor is invalid")
        os.fstat(descriptor)
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
    ]
    for descriptor in pass_fds:
        wrapper.extend(("--pass-fd", str(descriptor)))
    wrapper.extend(("--", *argv))
    process: subprocess.Popen[bytes] | None = None
    owned: dict[str, Any] | None = None
    owner_published = False
    owner_token = secrets.token_hex(32)
    child_environment = dict(os.environ if environment is None else environment)
    child_environment[OWNER_TOKEN_ENV] = owner_token
    try:
        process = subprocess.Popen(
            wrapper,
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=subprocess.STDOUT,
            close_fds=True,
            pass_fds=(read_gate, *pass_fds),
            start_new_session=True,
            env=child_environment,
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
            "owner_token": owner_token,
            "runtime_log": str(runtime_log),
            "start_ticks": start_ticks,
            "supervisor_sha256": sha256_file(SUPERVISOR),
        }
        publish_json(owner_path, owned)
        owner_published = True
        if os.write(write_gate, b"1") != 1:
            raise BackgroundJobError("background release gate made no write progress")
        os.close(write_gate)
        write_gate = -1
        _CHILDREN[process.pid] = process
        return owned
    except BaseException as error:
        cleanup_proven = process is None
        if process is not None:
            cleanup_proven = _stop_failed_launch(process, owned)
        if cleanup_proven:
            runtime_log.unlink(missing_ok=True)
            completion_file.unlink(missing_ok=True)
            if owner_published:
                owner_path.unlink(missing_ok=True)
            elif owned is not None and owner_path.exists() and not owner_path.is_symlink():
                try:
                    owner_published = load_json(owner_path) == owned
                except BackgroundJobError:
                    owner_published = False
                if owner_published:
                    owner_path.unlink()
            raise
        raise LaunchStateRetained(
            "failed background launch still has an exact live process group; "
            "runtime ownership was retained"
        ) from error
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
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        _reap_local_child(pid)
        if not _owned_live_process_group(record, require_current=require_current):
            return
        time.sleep(0.05)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        _reap_local_child(pid)
        if not _owned_live_process_group(record, require_current=require_current):
            return
        time.sleep(0.05)
    raise BackgroundJobError(f"background process group {pid} did not stop")


def _run(args: argparse.Namespace) -> int:
    with os.fdopen(args.gate_fd, "rb", closefd=True) as gate:
        if gate.read(1) != b"1":
            print(
                "background owner publication was interrupted before release",
                file=sys.stderr,
                flush=True,
            )
            return 125
    started_at = utc_now()
    exit_code = 127
    try:
        result = subprocess.run(
            args.argv,
            cwd=args.cwd,
            check=False,
            pass_fds=tuple(args.pass_fd),
        )
        exit_code = result.returncode
    except OSError as error:
        print(f"background command could not start: {error}", file=sys.stderr, flush=True)
    identity = process_identity(os.getpid())
    if identity is None:
        return exit_code
    _state, _process_group, start_ticks = identity
    owner_token = os.environ.get(OWNER_TOKEN_ENV, "")
    if (
        len(owner_token) != 64
        or any(character not in "0123456789abcdef" for character in owner_token)
    ):
        raise BackgroundJobError("background supervisor has no valid owner token")
    _atomic_json(
        args.completion,
        {
            "exit_code": exit_code,
            "finished_at": utc_now(),
            "owner_token": owner_token,
            "pid": os.getpid(),
            "schema": SCHEMA,
            "started_at": started_at,
            "start_ticks": start_ticks,
            "supervisor_sha256": sha256_file(SUPERVISOR),
        },
    )
    return exit_code


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    commands = value.add_subparsers(dest="command", required=True)
    run = commands.add_parser("_run", help=argparse.SUPPRESS)
    run.add_argument("--gate-fd", type=int, required=True)
    run.add_argument("--completion", type=Path, required=True)
    run.add_argument("--cwd", type=Path, required=True)
    run.add_argument("--pass-fd", type=int, action="append", default=[])
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
