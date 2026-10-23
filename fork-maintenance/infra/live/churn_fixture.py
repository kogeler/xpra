#!/usr/bin/env python3
# Copyright (C) 2026 kogeler
"""Detach churn: transparent window churn and notifications, client pointer motion.

``server`` runs as a native-Wayland GTK client of the Xpra server. It keeps up
to four RGBA windows with a translucent background, replaces one every 50 ms,
and sends a desktop notification through the session bus every 250 ms, so an
attaching client keeps mapping windows and acknowledging updates while it is
being detached. ``pointer`` runs on the client display and keeps warping the
real pointer, which the attached Xpra client forwards as pointer motion.
Each mode prints bounded JSON progress records on stdout.
"""

import argparse
import json
import signal
import sys
import time

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402 - select GI versions before importing

WINDOW_PREFIX = "Xpra Detach Churn"
MAX_WINDOWS = 4
CHURN_MS = 50
NOTIFY_MS = 250
REPORT_MS = 500
CSS = b"window { background-color: rgba(32, 96, 160, 0.5); }"


def report(record: dict) -> None:
    print(json.dumps(record, sort_keys=True), flush=True)


def run_server() -> int:
    counts = {"created": 0, "destroyed": 0, "notified": 0, "notify_errors": 0}
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    windows: list[Gtk.Window] = []
    stopping: list[int] = []
    # python handlers run between callbacks: the 50 ms churn timer acts on it
    signal.signal(signal.SIGTERM, lambda signum, _frame: stopping.append(signum))

    def churn() -> bool:
        if stopping:
            Gtk.main_quit()
            return False
        if len(windows) >= MAX_WINDOWS:
            windows.pop(0).destroy()
            counts["destroyed"] += 1
        window = Gtk.Window(title=f"{WINDOW_PREFIX} {counts['created']}")
        visual = window.get_screen().get_rgba_visual()
        if visual is None:
            raise RuntimeError("the churn fixture requires an RGBA visual")
        window.set_visual(visual)
        window.set_app_paintable(True)
        window.get_style_context().add_provider(provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        window.set_default_size(160, 120)
        window.show_all()
        windows.append(window)
        counts["created"] += 1
        return True

    def notify() -> bool:
        body = GLib.Variant("(susssasa{sv}i)", ("xpra-detach-churn", 0, "", f"churn {counts['notified']}",
                                                "detach churn notification", [], {}, 2000))
        try:
            bus.call_sync("org.freedesktop.Notifications", "/org/freedesktop/Notifications",
                          "org.freedesktop.Notifications", "Notify", body, GLib.VariantType("(u)"),
                          Gio.DBusCallFlags.NO_AUTO_START, 2000, None)
            counts["notified"] += 1
        except GLib.Error:
            counts["notify_errors"] += 1
        return True

    def progress() -> bool:
        report({"mode": "server", **counts})
        return True

    churn()
    report({"mode": "server", "event": "ready", **counts})
    GLib.timeout_add(CHURN_MS, churn)
    GLib.timeout_add(NOTIFY_MS, notify)
    GLib.timeout_add(REPORT_MS, progress)
    Gtk.main()
    report({"mode": "server", "event": "stopped", **counts})
    return 0


def run_pointer(display_name: str) -> int:
    display = Gdk.Display.open(display_name)
    if display is None:
        raise RuntimeError(f"cannot open client display {display_name!r}")
    pointer = display.get_default_seat().get_pointer()
    screen = display.get_default_screen()
    positions = ((60, 60), (420, 300), (240, 180), (600, 120))
    stopping = []
    signal.signal(signal.SIGTERM, lambda *_args: stopping.append(True))
    moves = 0
    report({"mode": "pointer", "event": "ready", "moves": moves})
    last = time.monotonic()
    while not stopping:
        pointer.warp(screen, *positions[moves % len(positions)])
        display.flush()
        moves += 1
        if time.monotonic() - last >= REPORT_MS / 1000:
            report({"mode": "pointer", "moves": moves})
            last = time.monotonic()
        time.sleep(0.002)
    report({"mode": "pointer", "event": "stopped", "moves": moves})
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("server", "pointer"))
    parser.add_argument("--display", default=":1")
    args = parser.parse_args()
    if args.mode == "server":
        Gtk.init(sys.argv[:1])
        return run_server()
    return run_pointer(args.display)


if __name__ == "__main__":
    raise SystemExit(main())
