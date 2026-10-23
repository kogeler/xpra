#!/usr/bin/env python3
# Copyright (C) 2026 kogeler
"""Owned SSH heartbeat lease and private bus/portal process-group teardown."""

import argparse
import json
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import sys
import time

import peer_processes


ROOT = Path(os.environ.get("XPRA_LIVE_ARTIFACTS", "/artifacts"))
STATE = Path("/tmp/server-runtime/shutdown-fixture")
# Teardown orders under test; the control deliberately aborts the real server.
ORDERS = ("together", "bus-first", "server-first")
ABORT_CONTROL = "abort-control"
SCENARIO_ORDERS = ORDERS + (ABORT_CONTROL,)
LEASE_SECONDS = 3
GRACE_SECONDS = 10
REQUIRED_NAMES = ("org.freedesktop.Notifications", "org.freedesktop.impl.portal.desktop.gtk",
                  "org.freedesktop.portal.Desktop")
HELPER_ROLES = ("private-bus", "portal", "portal-gtk")
# `ServerCore.signal_quit` exits with 128 + SIGTERM after its clean shutdown;
# with the bus gone first, the application may exit first (exit-with-children).
NORMAL_EXITS = {"together": {143}, "bus-first": {0, 143}, "server-first": {143}}
DIAGNOSTIC_FILES = (
    "server.pid", "server.exit", "server.native-exit.json", "server.stdout", "server.stderr",
    "server.private-bus.stdout", "server.private-bus.stderr", "server.portal.stdout", "server.portal.stderr",
    "server.portal-gtk.stdout", "server.portal-gtk.stderr", "server.shutdown-owner.json",
    "server.shutdown-supervisor.stderr", "server.shutdown-ready.json", "server.shutdown-result.json",
)


def publish(name: str, record: dict) -> None:
    with (ROOT / name).open("x", encoding="utf-8") as stream:
        json.dump(record, stream, sort_keys=True)
        stream.write("\n")


def private_output(name: str):
    return os.fdopen(os.open(ROOT / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "w")


def identity(pid: int) -> dict:
    value = peer_processes.read_process(pid)
    if value is None or value["state"] in {"Z", "X"}:
        raise RuntimeError(f"fixture process {pid} exited before identity capture")
    return {**peer_processes.identity(value), "pgid": os.getpgid(pid)}


def live_members(pgid: int, members: list[dict]) -> list[dict]:
    """Recorded members still running in the private group; changed identity fails."""
    live = []
    for expected in members:
        observed = peer_processes.read_process(expected["pid"])
        if observed is None or observed["state"] in {"Z", "X"}:
            continue
        try:
            group = os.getpgid(expected["pid"])
        except ProcessLookupError:
            continue
        if observed["start_ticks"] != expected["start_ticks"] or group != pgid:
            raise ValueError("fixture group member identity changed")
        live.append(observed)
    return live


def verified_group_signal(pgid: int, members: list[dict], signum: int) -> None:
    """Only the private group we created, never the SSH or parent group."""
    if pgid <= 1 or pgid == os.getpgrp() or signum not in {signal.SIGTERM, signal.SIGKILL}:
        raise ValueError("unsafe fixture process group signal")
    if live_members(pgid, members):
        os.killpg(pgid, signum)


def exit_receipt(returncode: int) -> dict:
    """The parent's own wait status: an abort can never look like an exit."""
    if returncode < 0:
        return {"origin": "signal", "signal": -returncode, "status": 128 - returncode}
    return {"origin": "exit", "status": returncode}


def option_value(command: list[str], name: str) -> Path:
    values = [value.split("=", 1)[1] for value in command if value.startswith(f"--{name}=")]
    if len(values) != 1 or not values[0].startswith("/"):
        raise ValueError(f"the server command needs exactly one absolute --{name}")
    return Path(values[0])


def session_entries(command: list[str]) -> dict[str, list[str]]:
    """Observe, never remove: an abort must leave its session behind."""
    entries = {}
    for key, option in (("sessions", "sessions-dir"), ("sockets", "socket-dir")):
        directory = option_value(command, option)
        names = sorted(path.name for path in directory.iterdir()) if directory.is_dir() else []
        if len(names) > 64:
            raise ValueError("unexpected session directory inventory")
        entries[key] = names
    return entries


def heartbeat(command: list[str]) -> int:
    """A distinct mux channel: killing the master ends this lease too."""
    stopping = []
    previous = signal.signal(signal.SIGTERM, lambda signum, _frame: stopping.append(signum))
    try:
        with subprocess.Popen(command, stdin=subprocess.PIPE) as child:
            assert child.stdin is not None
            try:
                while not stopping and child.poll() is None:
                    child.stdin.write(b".\n")
                    child.stdin.flush()
                    time.sleep(0.2)
            except BrokenPipeError:
                pass
            finally:
                try:
                    child.stdin.close()
                except BrokenPipeError:
                    pass
            return child.wait(timeout=15)
    finally:
        signal.signal(signal.SIGTERM, previous)


def process_table() -> list[dict]:
    """Bounded read-only state of every container process, for diagnosis only."""
    table = []
    for entry in sorted(peer_processes.PROC.iterdir(), key=lambda path: path.name):
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "stat").read_text()
        except (FileNotFoundError, ProcessLookupError):
            continue
        closing = raw.rfind(")")
        fields = raw[closing + 2:].split()
        if closing < 0 or len(fields) < 20:
            continue
        table.append({"pid": int(entry.name), "comm": raw[len(entry.name) + 2:closing], "state": fields[0],
                      "parent_pid": int(fields[1]), "pgid": int(fields[2]), "start_ticks": fields[19]})
        if len(table) >= 512:
            break
    return table


def diagnose() -> dict:
    """Bounded read-only failure evidence, not a full/accepted log transfer."""
    records = {}
    for name in DIAGNOSTIC_FILES:
        path = ROOT / name
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as stream:
                size = os.fstat(stream.fileno()).st_size
                stream.seek(max(0, size - 65536))
                records[name] = {"size": size, "suffix": stream.read(65536).decode("utf-8", errors="replace")}
    return {"files": records, "processes": process_table()}


def quiescent(exclude: int) -> dict:
    """The server container is quiet when only init and exited zombies remain."""
    remaining = [value for value in process_table()
                 if value["pid"] not in {1, exclude} and value["state"] not in {"Z", "X"}]
    return {"quiescent": not remaining, "remaining": remaining}


def private_bus_configuration() -> str:
    # Explicit services only: no inherited user bus or D-Bus activation helpers.
    return ("<!DOCTYPE busconfig PUBLIC '-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN' "
            "'http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd'>\n"
            "<busconfig><type>session</type><listen>unix:path=" + str(STATE / "bus")
            + "</listen><auth>EXTERNAL</auth><policy context='default'>"
            "<allow send_destination='*'/><allow receive_sender='*'/><allow own='*'/>"
            "</policy></busconfig>\n")


def bus_call(address: str, method: str, parameters, reply_type: str) -> tuple:
    """The fixture's own short-lived bus client; never Xpra's connection."""
    from gi.repository import Gio, GLib  # pylint: disable=import-outside-toplevel
    flags = Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
    connection = Gio.DBusConnection.new_for_address_sync(address, flags, None, None)
    try:
        reply = connection.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                     method, parameters, GLib.VariantType.new(reply_type),
                                     Gio.DBusCallFlags.NO_AUTO_START, 5000, None)
        return reply.unpack()
    finally:
        connection.close_sync(None)


def bus_names(address: str) -> set[str]:
    (names,) = bus_call(address, "ListNames", None, "(as)")
    if not isinstance(names, list) or any(not isinstance(value, str) for value in names):
        raise ValueError("invalid private bus name inventory")
    return set(names)


def bus_owner(address: str, name: str) -> int:
    from gi.repository import GLib  # pylint: disable=import-outside-toplevel
    (pid,) = bus_call(address, "GetConnectionUnixProcessID", GLib.Variant("(s)", (name,)), "(u)")
    if type(pid) is not int or pid <= 1:
        raise ValueError("invalid private bus owner PID")
    return pid


def expected_events(order: str, reason: str) -> list[str]:
    middle = {"bus-first": ["private-bus-reaped"], "server-first": ["server-reaped"],
              ABORT_CONTROL: ["server-aborted"]}.get(order, [])
    return [reason, *middle, "group-SIGTERM"]


def helper_statuses_valid(order: str, statuses: object) -> bool:
    if not isinstance(statuses, dict) or set(statuses) != set(HELPER_ROLES):
        return False
    if any(type(value) is not int for value in statuses.values()):
        return False
    # A helper may end itself only after its own deliberately stopped
    # dependency (bus, or the Wayland display for the GTK backend); never
    # by a fatal signal, and never by the forced kill.
    allowed = {"private-bus": {0, -signal.SIGTERM}, "portal": {-signal.SIGTERM}, "portal-gtk": {-signal.SIGTERM}}
    if order == "bus-first":
        allowed["portal"] = allowed["portal-gtk"] = {0, 1, -signal.SIGTERM}
    elif order in {"server-first", ABORT_CONTROL}:
        allowed["portal-gtk"] = {0, 1, -signal.SIGTERM}
    return all(statuses[role] in allowed[role] for role in HELPER_ROLES)


def server_exit_valid(order: str, native: object) -> bool:
    if order == ABORT_CONTROL:
        return native == {"origin": "signal", "signal": signal.SIGABRT, "status": 128 + signal.SIGABRT}
    return (isinstance(native, dict) and set(native) == {"origin", "status"} and native["origin"] == "exit"
            and type(native["status"]) is int and native["status"] in NORMAL_EXITS.get(order, set()))


def session_cleanup_valid(order: str, files: object) -> bool:
    if not isinstance(files, dict) or set(files) != {"ready", "after"}:
        return False
    ready, after = files["ready"], files["after"]
    shape = all(isinstance(value, dict) and set(value) == {"sessions", "sockets"}
                and all(isinstance(names, list) and all(isinstance(name, str) for name in names)
                        for names in value.values()) for value in (ready, after))
    if not shape or not ready["sessions"] or not ready["sockets"]:
        return False
    # The control's abort must leave its session behind rather than be hidden.
    return after == ready if order == ABORT_CONTROL else after == {"sessions": [], "sockets": []}


def evidence_checks(evidence: dict, *, application: dict, server_pid: int) -> dict[str, bool]:
    """Recomputed by both the worker and independent artifact collector."""
    ready, result, native = (evidence.get(key, {}) for key in ("ready", "result", "native_exit"))
    if not all(isinstance(record, dict) for record in (ready, result, native)):
        return {"private_shutdown_receipts": False}
    pgid = ready.get("pgid")
    members = ready.get("members")
    valid_members = bool(isinstance(members, list) and len(members) == 5 and type(pgid) is int and pgid > 1)
    if valid_members:
        valid_members = all(
            isinstance(member, dict) and set(member) == {"pid", "parent_pid", "start_ticks", "comm", "pgid"}
            and type(member["pid"]) is int and member["pid"] > 1
            and type(member["parent_pid"]) is int and member["parent_pid"] > 0
            and isinstance(member["start_ticks"], str) and re.fullmatch(r"[1-9][0-9]{0,31}", member["start_ticks"])
            and isinstance(member["comm"], str) and 0 < len(member["comm"]) <= 256
            and member["pgid"] == pgid for member in members
        ) and len({member["pid"] for member in members}) == 5
    owners = ready.get("bus_owners")
    order = ready.get("order")
    events = result.get("events", [])
    event_shape = bool(isinstance(events, list) and all(
        isinstance(value, dict) and set(value) == {"event", "time_ns"}
        and type(value["time_ns"]) is int and value["time_ns"] > 0 for value in events))
    remaining = result.get("remaining_members")
    return {
        "private_shutdown_receipts": ready.get("schema") == result.get("schema") == 2,
        "owned_private_group_topology": bool(valid_members and members[0]["pid"] == pgid
            and members[1]["pid"] == server_pid and members[4]["pid"] == application.get("pid")
            and members[4]["start_ticks"] == application.get("start_ticks")
            and members[4]["parent_pid"] == server_pid and ready.get("application") == application
            and result.get("members") == members and result.get("pgid") == pgid),
        "private_services_owned": bool(valid_members and isinstance(owners, dict) and set(owners) == set(REQUIRED_NAMES)
            and owners[REQUIRED_NAMES[0]] == server_pid and owners[REQUIRED_NAMES[1]] == members[2]["pid"]
            and owners[REQUIRED_NAMES[2]] == members[3]["pid"]),
        "ssh_lease_caused_group_shutdown": bool(result.get("reason") in {
            "heartbeat-eof", "heartbeat-expired", "supervisor-signal-1", "supervisor-signal-15"}),
        "shutdown_order_exact": bool(order in SCENARIO_ORDERS and result.get("order") == order and event_shape
            and [value["event"] for value in events] == expected_events(order, result.get("reason"))
            and all(left["time_ns"] < right["time_ns"] for left, right in zip(events, events[1:]))),
        "private_helpers_reaped_without_forced_kill": helper_statuses_valid(order, result.get("statuses")),
        "server_exit_receipt_exact": bool(server_exit_valid(order, native) and result.get("server_exit") == native),
        "session_files_cleaned_or_retained": session_cleanup_valid(order, result.get("session_files")),
        "private_group_members_gone": bool(isinstance(remaining, list) and len(remaining) <= 5 and all(
            isinstance(value, dict) and value.get("state") in {"Z", "X"} for value in remaining)),
    }


def abort_diagnostic_valid(stderr: bytes) -> bool:
    """CPython's fault handler must expose the real server's native stack."""
    text = stderr.decode("utf-8", errors="replace")
    marker = text.find("Fatal Python error: Aborted")
    if marker < 0 or text.count("Fatal Python error:") != 1:
        return False
    tail = text[marker:]
    stack = tail.find("Current thread's C stack trace (most recent call first):")
    return bool(stack > 0 and re.search(r'(?m)^  Binary file "[^"]*libc\.so\.6"', tail[stack:])
                and "xpra/server/" in tail[:stack])


def unexpected_failures(scenario: str, filename: str, payload: bytes, failures) -> tuple[str, ...]:
    """Positive runtime audit; only the abort control's own fault report is expected."""
    found = tuple(failures(payload, positive=True))
    if (scenario.startswith(f"ssh-group-{ABORT_CONTROL}-") and filename == "server.stderr"
            and abort_diagnostic_valid(payload)):
        return tuple(value for value in found if value != "fatal-python-error")
    return found


def supervise(order: str, command: list[str]) -> int:
    """Supervise the same SIGTERM-then-SIGKILL group topology as the service.

    The launcher is on a separate mux channel. EOF, lease expiry, or a signal
    always tears down the private group. Xpra is this supervisor's own child,
    so its wait status is exact without tracing the shipped process.
    """
    supervisor_log = private_output("server.shutdown-supervisor.stderr")
    # The SSH channel may be gone before teardown ends; keep tracebacks here.
    os.dup2(supervisor_log.fileno(), 2)
    STATE.mkdir(mode=0o700)
    (STATE / "bus.conf").write_text(private_bus_configuration())
    (STATE / "config" / "xdg-desktop-portal").mkdir(mode=0o700, parents=True)
    (STATE / "config" / "xdg-desktop-portal" / "portals.conf").write_text("[preferred]\ndefault=gtk\n")
    address = f"unix:path={STATE / 'bus'}"
    environment = {**os.environ, "DBUS_SESSION_BUS_ADDRESS": address, "XDG_RUNTIME_DIR": str(STATE.parent)}
    # Scoped only to the portal helpers, never to Xpra's own environment.
    portal_environment = {**environment, "WAYLAND_DISPLAY": "wayland-0", "XDG_CURRENT_DESKTOP": "GNOME",
                          "XDG_CONFIG_HOME": str(STATE / "config")}
    children: dict[str, subprocess.Popen] = {}
    streams = [supervisor_log]
    members: list[dict] = []
    pgid = 0
    interrupted: list[int] = []
    started = time.monotonic()
    last_heartbeat = started
    ready: dict | None = None
    reason = ""
    server_published = False

    def interrupted_by(signum, _frame):
        interrupted.append(signum)

    for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(signum, interrupted_by)
    publish("server.shutdown-owner.json", {"schema": 2, "supervisor": identity(os.getpid())})

    def launch(role, argv, *, group, env, stem=None):
        stdout = private_output(f"{stem or 'server.' + role}.stdout")
        stderr = private_output(f"{stem or 'server.' + role}.stderr")
        streams.extend((stdout, stderr))
        child = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                 process_group=group)
        children[role] = child
        return child

    def publish_server_exit() -> dict:
        nonlocal server_published
        receipt = exit_receipt(children["server"].returncode)
        if not server_published:
            server_published = True
            publish("server.native-exit.json", receipt)
            with private_output("server.exit") as stream:
                stream.write(f"{receipt['status']}\n")
        return receipt

    try:
        bus = launch("private-bus", ["dbus-daemon", "--nofork", "--nopidfile",
                                     f"--config-file={STATE / 'bus.conf'}"], group=0, env=environment)
        pgid = bus.pid
        members.append(identity(bus.pid))
        while not (STATE / "bus").exists():
            if bus.poll() is not None or time.monotonic() - started > 10:
                raise RuntimeError("private D-Bus startup failed")
            time.sleep(0.02)
        server = launch("server", command, group=pgid, env=environment, stem="server")
        members.append(identity(server.pid))
        with private_output("server.pid") as stream:
            stream.write(f"{server.pid}\n")
        while not interrupted:
            readable, _, _ = select.select([sys.stdin.buffer], [], [], 0.1)
            if readable:
                data = os.read(sys.stdin.fileno(), 4096)
                if not data:
                    reason = "heartbeat-eof"
                    break
                if data.strip(b".\n"):
                    raise ValueError("invalid SSH heartbeat")
                last_heartbeat = time.monotonic()
            if time.monotonic() - last_heartbeat > LEASE_SECONDS:
                reason = "heartbeat-expired"
                break
            if server.poll() is not None:
                publish_server_exit()
                raise RuntimeError("Xpra exited before deliberate teardown")
            server_log = ROOT / "server.stderr"
            application_path = ROOT / "interaction.identity.json"
            if (ready is None and application_path.exists()
                    and "xpra is ready." in server_log.read_text(errors="replace")):
                for role, executable in (("portal-gtk", "/usr/libexec/xdg-desktop-portal-gtk"),
                                         ("portal", "/usr/libexec/xdg-desktop-portal")):
                    child = launch(role, [executable, "--verbose"], group=pgid, env=portal_environment)
                    members.append(identity(child.pid))
                deadline = time.monotonic() + 15
                while not set(REQUIRED_NAMES) <= bus_names(address):
                    if any(child.poll() is not None for child in children.values()) or time.monotonic() > deadline:
                        raise RuntimeError("private portal/notification bus ownership missing")
                    time.sleep(0.05)
                application = json.loads(application_path.read_text())
                application_member = identity(application["pid"])
                if application_member["start_ticks"] != application["start_ticks"]:
                    raise RuntimeError("application identity changed before group capture")
                members.append(application_member)
                if any(member["pgid"] != pgid for member in members):
                    raise RuntimeError("private group does not contain every required peer")
                ready = {"schema": 2, "order": order, "pgid": pgid, "members": members,
                         "names": sorted(REQUIRED_NAMES), "application": application,
                         "bus_owners": {name: bus_owner(address, name) for name in REQUIRED_NAMES},
                         "session_files": session_entries(command)}
                publish("server.shutdown-ready.json", ready)
            if ready is None and time.monotonic() - started > 60:
                raise RuntimeError("shutdown fixture startup timed out")
        if interrupted:
            reason = f"supervisor-signal-{interrupted[0]}"
        if ready is None:
            raise RuntimeError("SSH lease ended before fixture readiness")
        events = [{"event": reason, "time_ns": time.monotonic_ns()}]
        if order == "bus-first":
            bus.terminate()
            bus.wait(timeout=5)
            events.append({"event": "private-bus-reaped", "time_ns": time.monotonic_ns()})
        elif order in {"server-first", ABORT_CONTROL}:
            # Still our unreaped child, so its PID cannot have been reused.
            server.send_signal(signal.SIGTERM if order == "server-first" else signal.SIGABRT)
            server.wait(timeout=GRACE_SECONDS)
            events.append({"event": "server-reaped" if order == "server-first" else "server-aborted",
                           "time_ns": time.monotonic_ns()})
        verified_group_signal(pgid, members, signal.SIGTERM)
        events.append({"event": "group-SIGTERM", "time_ns": time.monotonic_ns()})
        deadline = time.monotonic() + GRACE_SECONDS
        while time.monotonic() < deadline:
            if all(child.poll() is not None for child in children.values()):
                break
            time.sleep(0.05)
        else:
            verified_group_signal(pgid, members, signal.SIGKILL)
            events.append({"event": "group-SIGKILL", "time_ns": time.monotonic_ns()})
        statuses = {role: child.wait(timeout=15) for role, child in children.items() if role in HELPER_ROLES}
        server.wait(timeout=15)
        server_exit = publish_server_exit()
        remaining = [observed for member in members
                     if (observed := peer_processes.read_process(member["pid"])) is not None
                     and observed["start_ticks"] == member["start_ticks"]]
        publish("server.shutdown-result.json", {
            "schema": 2, "order": order, "pgid": pgid, "members": members, "reason": reason, "events": events,
            "statuses": statuses, "server_exit": server_exit, "remaining_members": remaining,
            "session_files": {"ready": ready["session_files"], "after": session_entries(command)},
        })
        return 0
    finally:
        # Never let one cleanup error leave the group or a log writer behind.
        if pgid:
            try:
                verified_group_signal(pgid, members, signal.SIGKILL)
            except (OSError, ValueError) as error:
                print(f"private group cleanup: {error}", file=sys.stderr, flush=True)
        for child in children.values():
            try:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=15)
            except (OSError, subprocess.TimeoutExpired) as error:
                print(f"fixture child cleanup: {error}", file=sys.stderr, flush=True)
        if "server" in children and children["server"].returncode is not None and not server_published:
            try:
                publish_server_exit()
            except OSError as error:
                print(f"server exit receipt: {error}", file=sys.stderr, flush=True)
        for stream in streams:
            stream.close()


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("supervise", "heartbeat", "diagnose", "quiescent"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.action == "diagnose":
        print(json.dumps(diagnose(), sort_keys=True))
        return 0
    if args.action == "quiescent":
        print(json.dumps(quiescent(os.getpid()), sort_keys=True))
        return 0
    if args.action == "heartbeat":
        return heartbeat(args.arguments)
    if not args.arguments or args.arguments[0] not in SCENARIO_ORDERS:
        parser.error("supervise requires an explicit shutdown order")
    return supervise(args.arguments[0], args.arguments[1:])


if __name__ == "__main__":
    raise SystemExit(main())
