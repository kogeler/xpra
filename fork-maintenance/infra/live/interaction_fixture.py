#!/usr/bin/env python3
"""Provide deterministic pointer and keyboard evidence for the Xpra lab."""

from __future__ import annotations

from pathlib import Path

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gtk

READY_TITLE = "Xpra Hardware Interaction Ready"
CLICKED_TITLE = "Xpra Hardware Interaction Clicked"
CLICK_MARKER = Path("/tmp/xpra-hardware-pointer-clicked")
KEY_MARKER = Path("/tmp/xpra-hardware-keyboard-escape")


def main() -> int:
    for marker in (CLICK_MARKER, KEY_MARKER):
        marker.unlink(missing_ok=True)
    window = Gtk.Window(title=READY_TITLE)
    window.set_default_size(480, 320)
    window.connect("destroy", Gtk.main_quit)

    button = Gtk.Button(label="CLICK TO VERIFY POINTER INPUT")
    provider = Gtk.CssProvider()
    provider.load_from_data(
        b"button { font-size: 24px; background-image: none; "
        b"background-color: #274060; color: #ffffff; } "
        b"button.verified { background-color: #31d07b; color: #101820; }"
    )
    button.get_style_context().add_provider(
        provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )

    def clicked(_button: Gtk.Button) -> None:
        CLICK_MARKER.write_text("pointer click received\n", encoding="utf-8")
        button.set_label("POINTER INPUT VERIFIED")
        button.get_style_context().add_class("verified")
        window.set_title(CLICKED_TITLE)

    def key_pressed(_window: Gtk.Window, event: Gdk.EventKey) -> bool:
        if event.keyval != Gdk.KEY_Escape:
            return False
        KEY_MARKER.write_text("Escape received\n", encoding="utf-8")
        Gtk.main_quit()
        return True

    button.connect("clicked", clicked)
    window.connect("key-press-event", key_pressed)
    window.add(button)
    window.show_all()
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
