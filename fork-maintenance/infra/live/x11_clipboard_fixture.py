# Copyright (C) 2026 kogeler

"""Fixed-marker X11 clipboard owner, converter, and XFixes monitor."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import signal
import stat
import time
from ctypes import POINTER, Structure, Union, byref, c_char_p, c_int, c_long, c_ubyte, c_ulong, c_void_p
from pathlib import Path
from typing import Final

from clipboard_fixture_common import marker_ids, marker_summary, marker_text

PROPERTY_CHANGE_MASK: Final = 1 << 22
PROPERTY_NOTIFY: Final = 28
SELECTION_NOTIFY: Final = 31
CURRENT_TIME: Final = 0
ANY_PROPERTY_TYPE: Final = 0
XFIXES_SET_SELECTION_OWNER_NOTIFY_MASK: Final = 1
XFIXES_SELECTION_WINDOW_DESTROY_NOTIFY_MASK: Final = 2
XFIXES_SELECTION_CLIENT_CLOSE_NOTIFY_MASK: Final = 4
MAX_COMMAND_BYTES: Final = 128
MAX_CONVERSION_EVENTS: Final = 1024
MAX_MONITOR_EVENTS: Final = 1024
MAX_PROPERTY_BYTES: Final = 4096
MAX_TARGET_ATOMS: Final = 256

Display = c_void_p
Window = c_ulong
Atom = c_ulong
Time = c_ulong
Bool = c_int


class XAnyEvent(Structure):
    _fields_ = [
        ("type", c_int),
        ("serial", c_ulong),
        ("send_event", Bool),
        ("display", Display),
        ("window", Window),
    ]


class XPropertyEvent(Structure):
    _fields_ = [
        ("type", c_int),
        ("serial", c_ulong),
        ("send_event", Bool),
        ("display", Display),
        ("window", Window),
        ("atom", Atom),
        ("time", Time),
        ("state", c_int),
    ]


class XSelectionEvent(Structure):
    _fields_ = [
        ("type", c_int),
        ("serial", c_ulong),
        ("send_event", Bool),
        ("display", Display),
        ("requestor", Window),
        ("selection", Atom),
        ("target", Atom),
        ("property", Atom),
        ("time", Time),
    ]


class XFixesSelectionNotifyEvent(Structure):
    _fields_ = [
        ("type", c_int),
        ("serial", c_ulong),
        ("send_event", Bool),
        ("display", Display),
        ("window", Window),
        ("subtype", c_int),
        ("owner", Window),
        ("selection", Atom),
        ("timestamp", Time),
        ("selection_timestamp", Time),
    ]


class XEvent(Union):
    _fields_ = [
        ("type", c_int),
        ("xany", XAnyEvent),
        ("xproperty", XPropertyEvent),
        ("xselection", XSelectionEvent),
        ("xfixes", XFixesSelectionNotifyEvent),
        ("pad", c_long * 24),
    ]


class JsonlEmitter:
    """Emit bounded structured events without clipboard text."""

    def __init__(self) -> None:
        self.sequence = 0

    def emit(self, event: str, **values: object) -> None:
        payload = {
            "event": event,
            "monotonic_ns": time.monotonic_ns(),
            "schema": 1,
            "sequence": self.sequence,
            **values,
        }
        self.sequence += 1
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")), flush=True)


class X11:
    """Small raw Xlib/XFixes binding used only by the fixture."""

    def __init__(self) -> None:
        self.lib = ctypes.CDLL("libX11.so.6")
        self.fixes = ctypes.CDLL("libXfixes.so.3")
        self._declare()
        self.display = self.lib.XOpenDisplay(None)
        if not self.display:
            raise RuntimeError("cannot open the configured X display")
        self.root = int(self.lib.XDefaultRootWindow(self.display))

    def _declare(self) -> None:
        lib = self.lib
        lib.XOpenDisplay.argtypes = [c_char_p]
        lib.XOpenDisplay.restype = Display
        lib.XCloseDisplay.argtypes = [Display]
        lib.XCloseDisplay.restype = c_int
        lib.XDefaultRootWindow.argtypes = [Display]
        lib.XDefaultRootWindow.restype = Window
        lib.XInternAtom.argtypes = [Display, c_char_p, Bool]
        lib.XInternAtom.restype = Atom
        lib.XGetSelectionOwner.argtypes = [Display, Atom]
        lib.XGetSelectionOwner.restype = Window
        lib.XCreateSimpleWindow.argtypes = [
            Display,
            Window,
            c_int,
            c_int,
            c_ulong,
            c_ulong,
            c_ulong,
            c_ulong,
            c_ulong,
        ]
        lib.XCreateSimpleWindow.restype = Window
        lib.XDestroyWindow.argtypes = [Display, Window]
        lib.XDestroyWindow.restype = c_int
        lib.XSelectInput.argtypes = [Display, Window, c_long]
        lib.XSelectInput.restype = c_int
        lib.XConvertSelection.argtypes = [Display, Atom, Atom, Atom, Window, Time]
        lib.XConvertSelection.restype = c_int
        lib.XDeleteProperty.argtypes = [Display, Window, Atom]
        lib.XDeleteProperty.restype = c_int
        lib.XGetWindowProperty.argtypes = [
            Display,
            Window,
            Atom,
            c_long,
            c_long,
            Bool,
            Atom,
            POINTER(Atom),
            POINTER(c_int),
            POINTER(c_ulong),
            POINTER(c_ulong),
            POINTER(POINTER(c_ubyte)),
        ]
        lib.XGetWindowProperty.restype = c_int
        lib.XFree.argtypes = [c_void_p]
        lib.XFree.restype = c_int
        lib.XPending.argtypes = [Display]
        lib.XPending.restype = c_int
        lib.XNextEvent.argtypes = [Display, POINTER(XEvent)]
        lib.XNextEvent.restype = c_int
        lib.XFlush.argtypes = [Display]
        lib.XFlush.restype = c_int
        lib.XSync.argtypes = [Display, Bool]
        lib.XSync.restype = c_int
        self.fixes.XFixesQueryExtension.argtypes = [Display, POINTER(c_int), POINTER(c_int)]
        self.fixes.XFixesQueryExtension.restype = Bool
        self.fixes.XFixesSelectSelectionInput.argtypes = [Display, Window, Atom, c_ulong]
        self.fixes.XFixesSelectSelectionInput.restype = None

    def close(self) -> None:
        if self.display:
            self.lib.XCloseDisplay(self.display)
            self.display = None

    def atom(self, name: str) -> int:
        return int(self.lib.XInternAtom(self.display, name.encode("ascii"), False))

    def owner(self, selection: int) -> int:
        self.lib.XSync(self.display, False)
        return int(self.lib.XGetSelectionOwner(self.display, selection))


def take_command(command_file: Path) -> tuple[str, str | None] | None:
    """Consume one bounded command without ever returning arbitrary text."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(command_file, flags)
    except FileNotFoundError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_COMMAND_BYTES:
            return ("invalid", None)
        raw = os.read(descriptor, MAX_COMMAND_BYTES + 1)
    finally:
        os.close(descriptor)
        command_file.unlink(missing_ok=True)
    try:
        command = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        return ("invalid", None)
    if command == "quit":
        return ("quit", None)
    operation, separator, marker_id = command.partition(":")
    if operation != "set" or not separator or marker_id not in marker_ids():
        return ("invalid", None)
    return (operation, marker_id)


def take_monitor_stop(command_file: Path) -> bool:
    """Consume only the exact private stop command used by the monitor."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(command_file, flags)
    except FileNotFoundError:
        return False
    try:
        metadata = os.fstat(descriptor)
        raw = os.read(descriptor, MAX_COMMAND_BYTES + 1)
    finally:
        os.close(descriptor)
        command_file.unlink(missing_ok=True)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size > MAX_COMMAND_BYTES
        or raw != b"stop\n"
    ):
        raise RuntimeError("invalid XFixes monitor command")
    return True


def selection_owners() -> tuple[int, int]:
    x11 = X11()
    try:
        return x11.owner(x11.atom("CLIPBOARD")), x11.owner(x11.atom("PRIMARY"))
    finally:
        x11.close()


def run_owner(marker_id: str, command_file: Path) -> int:
    """Own CLIPBOARD and PRIMARY, updating both through the same GTK objects."""
    os.environ.setdefault("GDK_BACKEND", "x11")
    import gi

    gi.require_version("Gdk", "3.0")
    gi.require_version("GdkX11", "3.0")
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gdk, GdkX11, GLib, Gtk

    emitter = JsonlEmitter()
    display = Gdk.Display.get_default()
    if display is None or not isinstance(display, GdkX11.X11Display):
        raise RuntimeError("the X11 clipboard fixture requires a native X11 GDK display")
    command_file.unlink(missing_ok=True)
    clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
    primary = Gtk.Clipboard.get(Gdk.SELECTION_PRIMARY)
    state: dict[str, object] = {
        "active_marker_id": marker_id,
        "clipboard_owner_xid": 0,
        "primary_owner_xid": 0,
        "status": 0,
        "stopping": False,
    }

    def set_marker(requested_marker_id: str) -> tuple[int, int]:
        value = marker_text(requested_marker_id)
        clipboard.set_text(value, -1)
        primary.set_text(value, -1)
        display.sync()
        return selection_owners()

    def stop(status: int = 0) -> None:
        if state["stopping"]:
            return
        state["stopping"] = True
        state["status"] = max(int(state["status"]), status)
        emitter.emit(
            "owner-stopping",
            clipboard_owner_xid=int(state["clipboard_owner_xid"]),
            marker_id=str(state["active_marker_id"]),
            pid=os.getpid(),
            primary_owner_xid=int(state["primary_owner_xid"]),
        )
        Gtk.main_quit()

    def ready() -> bool:
        clipboard_owner, primary_owner = set_marker(marker_id)
        state["clipboard_owner_xid"] = clipboard_owner
        state["primary_owner_xid"] = primary_owner
        valid = clipboard_owner > 0 and primary_owner > 0
        emitter.emit(
            "owner-ready",
            backend="x11",
            clipboard_owner_xid=clipboard_owner,
            owner_valid=valid,
            pid=os.getpid(),
            primary_owner_xid=primary_owner,
            **marker_summary(marker_id, marker_text(marker_id)),
        )
        if not valid:
            stop(1)
        return GLib.SOURCE_REMOVE

    def poll_command() -> bool:
        command = take_command(command_file)
        if command is None:
            return GLib.SOURCE_CONTINUE
        operation, requested_marker_id = command
        if operation == "invalid":
            emitter.emit("owner-command-rejected", reason="invalid-command")
            return GLib.SOURCE_CONTINUE
        if operation == "quit":
            emitter.emit("owner-command-accepted", operation="quit")
            stop()
            return GLib.SOURCE_REMOVE
        assert operation == "set" and requested_marker_id is not None
        previous_clipboard_owner = int(state["clipboard_owner_xid"])
        previous_primary_owner = int(state["primary_owner_xid"])
        clipboard_owner, primary_owner = set_marker(requested_marker_id)
        same_clipboard_owner = clipboard_owner == previous_clipboard_owner
        same_primary_owner = primary_owner == previous_primary_owner
        state["active_marker_id"] = requested_marker_id
        state["clipboard_owner_xid"] = clipboard_owner
        state["primary_owner_xid"] = primary_owner
        emitter.emit(
            "owner-updated",
            clipboard_owner_xid=clipboard_owner,
            previous_clipboard_owner_xid=previous_clipboard_owner,
            previous_primary_owner_xid=previous_primary_owner,
            primary_owner_xid=primary_owner,
            same_clipboard_owner_xid=same_clipboard_owner,
            same_primary_owner_xid=same_primary_owner,
            **marker_summary(requested_marker_id, marker_text(requested_marker_id)),
        )
        if not same_clipboard_owner or not same_primary_owner:
            stop(1)
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def handle_signal(*_args: object) -> None:
        stop()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    GLib.idle_add(ready)
    GLib.timeout_add(25, poll_command)
    Gtk.main()
    return int(state["status"])


def read_property(x11: X11, window: int, prop: int) -> tuple[dict[str, object], bytes | None, tuple[int, ...]]:
    """Read a property while keeping any selection bytes out of JSON evidence."""
    actual_type = Atom()
    actual_format = c_int()
    nitems = c_ulong()
    bytes_after = c_ulong()
    data = POINTER(c_ubyte)()
    status = x11.lib.XGetWindowProperty(
        x11.display,
        window,
        prop,
        0,
        (MAX_PROPERTY_BYTES + 3) // 4,
        False,
        ANY_PROPERTY_TYPE,
        byref(actual_type),
        byref(actual_format),
        byref(nitems),
        byref(bytes_after),
        byref(data),
    )
    public: dict[str, object] = {
        "actual_format": int(actual_format.value),
        "actual_type_atom": int(actual_type.value),
        "bytes_after": int(bytes_after.value),
        "nitems": int(nitems.value),
        "status": int(status),
        "value_complete": status == 0 and bytes_after.value == 0,
    }
    raw: bytes | None = None
    atoms: tuple[int, ...] = ()
    try:
        if status == 0 and data and actual_format.value == 8:
            raw = ctypes.string_at(data, nitems.value)
        elif status == 0 and data and actual_format.value == 32:
            atom_values = ctypes.cast(data, POINTER(c_ulong))
            atom_count = min(int(nitems.value), MAX_TARGET_ATOMS)
            atoms = tuple(int(atom_values[index]) for index in range(atom_count))
            public["atom_ids"] = atoms
            public["atom_ids_complete"] = nitems.value <= MAX_TARGET_ATOMS
            if nitems.value > MAX_TARGET_ATOMS:
                public["value_complete"] = False
    finally:
        if data:
            x11.lib.XFree(data)
    return public, raw, atoms


def convert_target(
    x11: X11,
    *,
    requestor: int,
    selection: int,
    target_name: str,
    timeout: float,
) -> tuple[dict[str, object], bytes | None, tuple[int, ...]]:
    """Issue one raw XConvertSelection request and capture its event route."""
    target = x11.atom(target_name)
    prop = x11.atom(f"XPRA_LIVE_CLIPBOARD_{target_name.replace('/', '_')}")
    x11.lib.XDeleteProperty(x11.display, requestor, prop)
    x11.lib.XConvertSelection(x11.display, selection, target, prop, requestor, CURRENT_TIME)
    x11.lib.XFlush(x11.display)
    deadline = time.monotonic() + timeout
    events: list[dict[str, object]] = []
    selection_event: dict[str, object] | None = None
    while time.monotonic() < deadline:
        while x11.lib.XPending(x11.display):
            event = XEvent()
            x11.lib.XNextEvent(x11.display, byref(event))
            if event.type == PROPERTY_NOTIFY:
                observed = event.xproperty
                events.append(
                    {
                        "atom_id": int(observed.atom),
                        "send_event": bool(observed.send_event),
                        "state": int(observed.state),
                        "time": int(observed.time),
                        "type": "PropertyNotify",
                        "window_xid": int(observed.window),
                    }
                )
                if len(events) >= MAX_CONVERSION_EVENTS:
                    return {
                        "completed": False,
                        "events": events,
                        "overflow": True,
                        "selection_notify": selection_event,
                        "target": target_name,
                        "value": None,
                    }, None, ()
            elif event.type == SELECTION_NOTIFY:
                observed = event.xselection
                selection_event = {
                    "property_atom": int(observed.property),
                    "requestor_xid": int(observed.requestor),
                    "selection_atom": int(observed.selection),
                    "send_event": bool(observed.send_event),
                    "target_atom": int(observed.target),
                    "time": int(observed.time),
                    "type": "SelectionNotify",
                }
                events.append(selection_event)
                if len(events) >= MAX_CONVERSION_EVENTS:
                    return {
                        "completed": False,
                        "events": events,
                        "overflow": True,
                        "selection_notify": selection_event,
                        "target": target_name,
                        "value": None,
                    }, None, ()
                if int(observed.target) == target and int(observed.requestor) == requestor:
                    if not observed.property:
                        return {
                            "completed": False,
                            "events": events,
                            "overflow": False,
                            "selection_notify": selection_event,
                            "target": target_name,
                            "value": None,
                        }, None, ()
                    public, raw, atoms = read_property(x11, requestor, int(observed.property))
                    return {
                        "completed": bool(public["value_complete"]),
                        "events": events,
                        "overflow": False,
                        "selection_notify": selection_event,
                        "target": target_name,
                        "value": public,
                    }, raw, atoms
        time.sleep(0.005)
    return {
        "completed": False,
        "events": events,
        "overflow": False,
        "selection_notify": selection_event,
        "target": target_name,
        "value": None,
    }, None, ()


def run_convert(marker_id: str, timeout: float) -> int:
    emitter = JsonlEmitter()
    x11 = X11()
    selection = x11.atom("CLIPBOARD")
    requestor = int(x11.lib.XCreateSimpleWindow(x11.display, x11.root, -1, -1, 1, 1, 0, 0, 0))
    if requestor <= 0:
        x11.close()
        raise RuntimeError("cannot create the X11 selection requestor window")
    x11.lib.XSelectInput(x11.display, requestor, PROPERTY_CHANGE_MASK)
    x11.lib.XSync(x11.display, False)
    owner_before = x11.owner(selection)
    try:
        targets, _target_bytes, target_atoms = convert_target(
            x11,
            requestor=requestor,
            selection=selection,
            target_name="TARGETS",
            timeout=timeout,
        )
        utf8_atom = x11.atom("UTF8_STRING")
        string_atom = x11.atom("STRING")
        known_targets = {
            "STRING": string_atom in target_atoms,
            "UTF8_STRING": utf8_atom in target_atoms,
        }
        if known_targets["UTF8_STRING"]:
            text_target = "UTF8_STRING"
        elif known_targets["STRING"]:
            text_target = "STRING"
        else:
            text_target = None
        if text_target is None:
            text: dict[str, object] = {
                "completed": False,
                "events": [],
                "overflow": False,
                "selection_notify": None,
                "target": None,
                "value": None,
            }
            observed = None
        else:
            text, observed, _text_atoms = convert_target(
                x11,
                requestor=requestor,
                selection=selection,
                target_name=text_target,
                timeout=timeout,
            )
        owner_after = x11.owner(selection)
        marker = marker_summary(marker_id, observed)
        result = {
            "backend": "x11",
            "known_targets": known_targets,
            "marker": marker,
            "owner_after_xid": owner_after,
            "owner_before_xid": owner_before,
            "owner_stable": owner_after == owner_before and owner_before > 0,
            "requestor_xid": requestor,
            "targets": targets,
            "text": text,
        }
        emitter.emit("conversion-result", **result)
        return 0 if targets["completed"] and text["completed"] and marker["matches"] and result["owner_stable"] else 1
    finally:
        x11.lib.XDestroyWindow(x11.display, requestor)
        x11.close()


def run_monitor(
    *,
    event_window: int | None,
    root: bool,
    stop_file: Path | None,
    timeout: float,
) -> int:
    emitter = JsonlEmitter()
    x11 = X11()
    selection = x11.atom("CLIPBOARD")
    event_base = c_int()
    error_base = c_int()
    if not x11.fixes.XFixesQueryExtension(x11.display, byref(event_base), byref(error_base)):
        raise RuntimeError("XFixes is unavailable")
    windows: list[int] = []
    if root:
        windows.append(x11.root)
    if event_window is not None and event_window not in windows:
        windows.append(event_window)
    mask = (
        XFIXES_SET_SELECTION_OWNER_NOTIFY_MASK
        | XFIXES_SELECTION_WINDOW_DESTROY_NOTIFY_MASK
        | XFIXES_SELECTION_CLIENT_CLOSE_NOTIFY_MASK
    )
    for window in windows:
        x11.fixes.XFixesSelectSelectionInput(x11.display, window, selection, mask)
    x11.lib.XSync(x11.display, False)
    if stop_file is not None:
        stop_file.unlink(missing_ok=True)
    emitter.emit(
        "monitor-ready",
        event_base=int(event_base.value),
        owner_before_xid=x11.owner(selection),
        root_xid=x11.root,
        subscribed_window_xids=windows,
    )
    events: list[dict[str, object]] = []
    overflow = False
    stop_requested = False
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline and not overflow and not stop_requested:
            while x11.lib.XPending(x11.display):
                event = XEvent()
                x11.lib.XNextEvent(x11.display, byref(event))
                if event.type != event_base.value:
                    continue
                observed = event.xfixes
                item = {
                    "owner_xid": int(observed.owner),
                    "selection_is_clipboard": int(observed.selection) == selection,
                    "selection_timestamp": int(observed.selection_timestamp),
                    "send_event": bool(observed.send_event),
                    "subtype": int(observed.subtype),
                    "timestamp": int(observed.timestamp),
                    "window_xid": int(observed.window),
                }
                events.append(item)
                emitter.emit("xfixes-selection-notify", **item)
                if len(events) >= MAX_MONITOR_EVENTS:
                    overflow = True
                    break
            if stop_file is not None:
                stop_requested = take_monitor_stop(stop_file)
                if stop_requested:
                    break
            time.sleep(0.005)
        emitter.emit(
            "monitor-result",
            event_count=len(events),
            events=events,
            overflow=overflow,
            owner_after_xid=x11.owner(selection),
            stop_requested=stop_requested,
            subscribed_window_xids=windows,
        )
        stopped_cleanly = stop_file is None or stop_requested
        return 0 if events and not overflow and stopped_cleanly else 1
    finally:
        x11.close()


def positive_timeout(value: str) -> float:
    timeout = float(value)
    if timeout <= 0 or timeout > 120:
        raise argparse.ArgumentTypeError("timeout must be greater than zero and at most 120 seconds")
    return timeout


def positive_xid(value: str) -> int:
    xid = int(value, 0)
    if xid <= 0:
        raise argparse.ArgumentTypeError("XID must be positive")
    return xid


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    owner = subparsers.add_parser("owner", help="own both X11 text selections")
    owner.add_argument("marker", choices=marker_ids())
    owner.add_argument(
        "--command-file",
        type=Path,
        default=Path("/tmp/xpra-x11-clipboard-owner-command"),
    )

    convert = subparsers.add_parser("convert", help="convert TARGETS and one text target")
    convert.add_argument("marker", choices=marker_ids())
    convert.add_argument("--timeout", type=positive_timeout, default=5.0)

    monitor = subparsers.add_parser("monitor", help="monitor XFixes owner changes")
    monitor.add_argument("--timeout", type=positive_timeout, default=8.0)
    monitor.add_argument("--event-window", type=positive_xid)
    monitor.add_argument("--root", action="store_true")
    monitor.add_argument("--stop-file", type=Path)

    args = parser.parse_args()
    if args.command == "owner":
        return run_owner(args.marker, args.command_file)
    if args.command == "convert":
        return run_convert(args.marker, args.timeout)
    if args.command == "monitor":
        if not args.root and args.event_window is None:
            parser.error("monitor requires --root, --event-window, or both")
        return run_monitor(
            event_window=args.event_window,
            root=args.root,
            stop_file=args.stop_file,
            timeout=args.timeout,
        )
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
