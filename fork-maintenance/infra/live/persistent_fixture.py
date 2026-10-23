#!/usr/bin/env python3
# Copyright (C) 2026 kogeler
"""Run the live Xpra server as a persistent systemd user service.

The server container boots systemd; `xpra-live-user-manager.service` runs the
real `systemd --user` manager for the lab user (without a login session, which
the user-namespaced container cannot open through PAM). The Xpra server is a
user unit of that manager, so neither the SSH session nor a container exec
shell owns it. `ExecStopPost` records the service's own exit receipt.
"""

import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

ROOT = Path(os.environ.get("XPRA_LIVE_ARTIFACTS", "/artifacts"))
UNIT = "xpra-live.service"
UNIT_DIR = Path.home() / ".config" / "systemd" / "user"
USER_RUNTIME_DIR = f"/run/user/{os.getuid()}"
HELPER = Path(__file__).resolve()
PROPERTIES = ("MainPID", "ActiveState", "SubState", "Result", "ControlGroup", "NRestarts")


def systemd_quote(value: str) -> str:
    """One unit-file word: double quoted, specifiers and variables escaped."""
    if "\n" in value or "\0" in value:
        raise ValueError("unit arguments must be single-line")
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{value}"'


def render_unit(environment: list[str], command: list[str]) -> str:
    if not command or not command[0].startswith("/"):
        raise ValueError("the service command needs an absolute executable")
    for value in environment:
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*=[^\n\0]*", value):
            raise ValueError(f"invalid service environment entry: {value!r}")
    lines = [
        "[Unit]",
        "Description=Xpra live persistent session",
        "[Service]",
        "Type=simple",
        *(f"Environment={systemd_quote(value)}" for value in environment),
        "ExecStart=" + " ".join(systemd_quote(value) for value in command),
        f"StandardOutput=file:{ROOT}/server.stdout",
        f"StandardError=file:{ROOT}/server.stderr",
        f"ExecStopPost={sys.executable} {HELPER} exit-receipt",
        "",
    ]
    return "\n".join(lines)


def user_systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], env={**os.environ, "XDG_RUNTIME_DIR": USER_RUNTIME_DIR},
                          capture_output=True, text=True, timeout=30, check=check)


def read_stat(pid: int) -> dict:
    raw = Path(f"/proc/{pid}/stat").read_text()
    closing = raw.rfind(")")
    fields = raw[closing + 2:].split()
    return {"pid": pid, "comm": raw[len(str(pid)) + 2:closing], "parent_pid": int(fields[1]),
            "start_ticks": fields[19]}


def state() -> dict:
    values = {}
    for line in user_systemctl("show", *(f"--property={name}" for name in PROPERTIES), UNIT).stdout.splitlines():
        key, _, value = line.partition("=")
        values[key] = value
    if set(values) != set(PROPERTIES):
        raise RuntimeError("incomplete service state")
    record = {"unit": UNIT, **values, "MainPID": int(values["MainPID"]), "NRestarts": int(values["NRestarts"])}
    if record["MainPID"] > 0:
        process = read_stat(record["MainPID"])
        manager = read_stat(process["parent_pid"])
        record["main_process"] = process
        record["manager_process"] = manager
        cmdline = Path(f"/proc/{manager['pid']}/cmdline").read_bytes().split(b"\0")
        record["manager_is_user_instance"] = cmdline[:2] == [b"/usr/lib/systemd/systemd", b"--user"]
    return record


def start(spec: dict) -> int:
    if set(spec) != {"environment", "command"}:
        raise ValueError("invalid persistent service specification")
    UNIT_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (UNIT_DIR / UNIT).open("x", encoding="utf-8") as stream:
        stream.write(render_unit(spec["environment"], spec["command"]))
    user_systemctl("daemon-reload")
    user_systemctl("start", UNIT)
    deadline = time.monotonic() + 30
    while (record := state())["MainPID"] <= 0:
        if time.monotonic() > deadline:
            raise RuntimeError("the persistent service has no main process")
        time.sleep(0.1)
    with (ROOT / "server.pid").open("x") as stream:
        stream.write(f"{record['MainPID']}\n")
    print(json.dumps(record, sort_keys=True))
    return 0


def exit_receipt() -> int:
    """`ExecStopPost`: systemd passes the main process's own result."""
    code, status = os.environ.get("EXIT_CODE", ""), os.environ.get("EXIT_STATUS", "")
    if code == "exited" and status.isdigit():
        receipt = {"origin": "exit", "status": int(status)}
    elif code in {"killed", "dumped"} and hasattr(signal, f"SIG{status}"):
        signum = int(getattr(signal, f"SIG{status}"))
        receipt = {"origin": "signal", "signal": signum, "status": 128 + signum}
    else:
        receipt = {"origin": "unknown", "status": 255}
    receipt["systemd"] = {"EXIT_CODE": code, "EXIT_STATUS": status, "SERVICE_RESULT": os.environ.get("SERVICE_RESULT", "")}
    with (ROOT / "server.systemd-exit.json").open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, sort_keys=True)
        stream.write("\n")
    with (ROOT / "server.exit").open("x") as stream:
        stream.write(f"{receipt['status']}\n")
    return 0


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "state", "exit-receipt", "manager-ready"))
    parser.add_argument("spec", nargs="?")
    args = parser.parse_args()
    if args.action == "start":
        return start(json.loads(args.spec))
    if args.action == "state":
        print(json.dumps(state(), sort_keys=True))
        return 0
    if args.action == "manager-ready":
        result = user_systemctl("is-system-running", check=False)
        print(result.stdout.strip())
        return 0 if result.stdout.strip() in {"running", "degraded"} else 1
    return exit_receipt()


if __name__ == "__main__":
    raise SystemExit(main())
