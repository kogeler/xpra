#!/usr/bin/env python3
# Copyright (C) 2026 kogeler

"""Native-Wayland editable GTK fixture for keyboard transport acceptance."""

from __future__ import annotations

import json
import os
import time

os.environ.setdefault("GDK_BACKEND", "wayland")

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402 - select GI versions before importing

TITLE = "Xpra Wayland Keyboard Fixture"
MAX_TEXT_BYTES = 4096
sequence = 0
closed = False


def emit(event: str, **values: object) -> None:
    global sequence
    payload = {
        "event": event,
        "monotonic_ns": time.monotonic_ns(),
        "schema": 1,
        "sequence": sequence,
        **values,
    }
    sequence += 1
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


display = Gdk.Display.get_default()
if display is None or "wayland" not in display.get_name().casefold():
    raise RuntimeError("the keyboard fixture requires a native Wayland display")

window = Gtk.Window(title=TITLE)
window.set_default_size(720, 240)
window.set_resizable(False)

provider = Gtk.CssProvider()
provider.load_from_data(
    b"""
window {
  background-image: linear-gradient(135deg, #18324f, #346a8f, #8db9d8);
}
label {
  color: #ffffff;
  font-size: 22px;
  font-weight: bold;
}
entry {
  font: 28px Sans;
  min-height: 64px;
  padding: 12px;
}
"""
)
Gtk.StyleContext.add_provider_for_screen(
    Gdk.Screen.get_default(),
    provider,
    Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
)

box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22)
box.set_border_width(28)
label = Gtk.Label(label="Native Wayland keyboard input")
entry = Gtk.Entry()
entry.set_hexpand(True)
box.pack_start(label, False, False, 0)
box.pack_start(entry, True, True, 0)
window.add(box)


def current_text() -> str:
    value = entry.get_text()
    if len(value.encode("utf-8")) > MAX_TEXT_BYTES:
        raise RuntimeError("keyboard fixture text exceeded its bound")
    return value


def key_event(kind: str, event: Gdk.EventKey) -> bool:
    emit(
        kind,
        hardware_keycode=int(event.hardware_keycode),
        keyname=Gdk.keyval_name(event.keyval) or "",
        keyval=int(event.keyval),
        text=current_text(),
    )
    return False


def key_press(event: Gdk.EventKey) -> bool:
    if event.keyval == Gdk.KEY_Escape:
        finish()
        return True
    return key_event("key-press", event)


def changed(_entry: Gtk.Entry) -> None:
    emit("changed", text=current_text())


def finish() -> None:
    global closed
    if closed:
        return
    closed = True
    emit("closed", text=current_text())
    Gtk.main_quit()


def close(_window: Gtk.Window, _event: Gdk.Event) -> bool:
    finish()
    return True


entry.connect("key-press-event", lambda _widget, event: key_press(event))
entry.connect("key-release-event", lambda _widget, event: key_event("key-release", event))
entry.connect("changed", changed)
window.connect("delete-event", close)
window.show_all()
entry.grab_focus()


def ready() -> bool:
    emit("ready", backend=display.get_name(), text=current_text(), title=TITLE)
    return False


GLib.idle_add(ready)
Gtk.main()
