#!/usr/bin/env python3
# Copyright (C) 2026 kogeler
"""Observe owned descendants and signal only birth-time-bound fixture PIDs."""

import argparse
import json
import os
from pathlib import Path
import re
import signal


PROC = Path("/proc")


def read_process(pid: int) -> dict | None:
    try:
        raw = (PROC / str(pid) / "stat").read_text()
    except FileNotFoundError:
        return None
    closing = raw.rfind(")")
    fields = raw[closing + 2:].split()
    if closing < 0 or not raw.startswith(str(pid) + " (") or len(fields) < 20:
        raise ValueError("invalid peer procfs identity")
    return {"pid": pid, "parent_pid": int(fields[1]), "start_ticks": fields[19],
            "comm": raw[len(str(pid)) + 2:closing], "state": fields[0]}


def stop_owned(pid: int, start_ticks: str, signame: str) -> None:
    if signame not in {"TERM", "KILL"}:
        raise ValueError("unsupported fixture stop signal")
    descriptor = os.pidfd_open(pid)
    try:
        observed = read_process(pid)
        if observed is None or observed["start_ticks"] != start_ticks or observed["state"] == "Z":
            raise ValueError("fixture process identity changed before stop")
        signal.pidfd_send_signal(descriptor, getattr(signal, f"SIG{signame}"))
    finally:
        os.close(descriptor)


def identity(process: dict) -> dict:
    return {key: process[key] for key in ("pid", "parent_pid", "start_ticks", "comm")}


def validate_snapshot(snapshot: dict) -> None:
    if (not isinstance(snapshot, dict) or set(snapshot) != {"schema", "root", "descendants"}
            or type(snapshot["schema"]) is not int or snapshot["schema"] != 1
            or not isinstance(snapshot["descendants"], list) or len(snapshot["descendants"]) > 127):
        raise ValueError("invalid peer descendant snapshot")
    seen = set()
    for process in (snapshot["root"], *snapshot["descendants"]):
        if (not isinstance(process, dict) or set(process) != {"pid", "parent_pid", "start_ticks", "comm"}
                or type(process["pid"]) is not int or not 0 < process["pid"] <= 2**31 - 1
                or type(process["parent_pid"]) is not int or not 0 <= process["parent_pid"] <= 2**31 - 1
                or not isinstance(process["start_ticks"], str)
                or not re.fullmatch(r"[1-9][0-9]{0,31}", process["start_ticks"])
                or not isinstance(process["comm"], str) or not 1 <= len(process["comm"]) <= 256
                or "\0" in process["comm"]
                or process["pid"] in seen or (seen and process["parent_pid"] not in seen)):
            raise ValueError("invalid peer descendant ancestry")
        seen.add(process["pid"])


def capture(pid: int, start_ticks: str) -> dict:
    root = read_process(pid)
    if root is None or root["start_ticks"] != start_ticks or root["state"].upper() in {"Z", "X"}:
        raise ValueError("peer exited or changed before descendant capture")
    queue = [identity(root)]
    seen = {pid}
    for parent in queue:
        children = set()
        try:
            tasks = list((PROC / str(parent["pid"]) / "task").iterdir())
        except FileNotFoundError:
            continue
        if len(tasks) > 1024:
            raise ValueError("live peer thread inventory is unexpectedly large")
        for task in tasks:
            try:
                children.update(int(value) for value in (task / "children").read_text().split())
            except FileNotFoundError:
                continue
        for child_pid in sorted(children):
            child = read_process(child_pid)
            if child is None:
                continue
            if child["parent_pid"] != parent["pid"] or child_pid in seen:
                raise ValueError("peer descendant ancestry changed during capture")
            seen.add(child_pid)
            queue.append(identity(child))
            if len(queue) > 128:
                raise ValueError("live peer descendant inventory is unexpectedly large")
    observed = read_process(pid)
    if observed is None or any(observed[key] != root[key] for key in ("pid", "parent_pid", "start_ticks")):
        raise ValueError("peer identity changed during descendant capture")
    snapshot = {"schema": 1, "root": queue[0], "descendants": queue[1:]}
    validate_snapshot(snapshot)
    return snapshot


def remaining(snapshot: dict) -> list[dict]:
    validate_snapshot(snapshot)
    result = []
    for expected in (snapshot["root"], *snapshot["descendants"]):
        observed = read_process(expected["pid"])
        if observed is None:
            continue
        if observed["start_ticks"] != expected["start_ticks"]:
            raise ValueError("peer descendant PID was reused before reaping was observed")
        # PPID may now be 1. A zombie still occupying its procfs slot is not
        # reaped, even though a pidfd reports it as exited.
        result.append(observed)
    return result


def blocking_remaining(snapshot: dict, observations: list[dict]) -> list[dict]:
    """Require peer-owned waits without making Xpra a process subreaper.

    The peer must reap its direct children. An application's grandchildren
    are not waitable by Xpra; once exited and orphaned, container init owns
    their wait. Keep those zombies in raw observations but reject any live
    descendant, or any unreaped peer/direct child.
    """
    validate_snapshot(snapshot)
    if not isinstance(observations, list) or len(observations) > 128:
        raise ValueError("invalid peer process observations")
    expected = {value["pid"]: value for value in (snapshot["root"], *snapshot["descendants"])}
    root_pid = snapshot["root"]["pid"]
    seen = set()
    blocking = []
    for process in observations:
        if (not isinstance(process, dict) or set(process) != {"pid", "parent_pid", "start_ticks", "comm", "state"}
                or type(process["pid"]) is not int or process["pid"] not in expected or process["pid"] in seen
                or process["start_ticks"] != expected[process["pid"]]["start_ticks"]
                or type(process["parent_pid"]) is not int or not 0 <= process["parent_pid"] <= 2**31 - 1
                or not isinstance(process["comm"], str) or not 1 <= len(process["comm"]) <= 256
                or not isinstance(process["state"], str) or len(process["state"]) != 1):
            raise ValueError("invalid peer process observation identity")
        seen.add(process["pid"])
        requires_peer_wait = process["pid"] == root_pid or expected[process["pid"]]["parent_pid"] == root_pid
        exited_orphan = process["state"].upper() in {"Z", "X"} and process["parent_pid"] == 1
        if requires_peer_wait or not exited_orphan:
            blocking.append(process)
    return blocking


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("capture", "remaining", "stop"))
    parser.add_argument("subject")
    parser.add_argument("start_ticks", nargs="?")
    parser.add_argument("signal", nargs="?", choices=("TERM", "KILL"))
    args = parser.parse_args()
    if args.action == "stop":
        stop_owned(int(args.subject), args.start_ticks, args.signal)
        output = None
    else:
        output = (capture(int(args.subject), args.start_ticks) if args.action == "capture"
                  else remaining(json.loads(args.subject)))
    print(json.dumps(output, sort_keys=True))
