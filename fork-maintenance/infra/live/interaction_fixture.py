#!/usr/bin/env python3
"""Provide deterministic pointer and keyboard evidence for Xpra live tests."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402 - select GI versions before importing

READY_TITLE = "Xpra Hardware Interaction Ready"
CLICKED_TITLE = "Xpra Hardware Interaction Clicked"
CLICK_MARKER = Path("/tmp/xpra-hardware-pointer-clicked")
KEY_MARKER = Path("/tmp/xpra-hardware-keyboard-escape")
READY_MARKER = Path("/tmp/xpra-hardware-interaction-ready")
IDENTITY_ARTIFACT = Path("/artifacts/interaction.identity.json")


def process_identity() -> dict[str, object]:
    """Return the kernel identity of this exact fixture process."""
    proc = Path("/proc/self")
    stat_value = (proc / "stat").read_text(encoding="ascii")
    end = stat_value.rfind(")")
    fields = stat_value[end + 2 :].split() if end >= 0 else []
    if len(fields) < 20:
        raise RuntimeError("cannot read the interaction fixture start time")
    cmdline = (proc / "cmdline").read_bytes()
    argv = [os.fsdecode(value) for value in cmdline.split(b"\0") if value]
    if not cmdline or not argv:
        raise RuntimeError("cannot read the interaction fixture command line")
    return {
        "argv": argv,
        "cmdline_sha256": hashlib.sha256(cmdline).hexdigest(),
        "pid": os.getpid(),
        "schema": 1,
        "start_ticks": fields[19],
    }


def publish_process_identity() -> None:
    """Publish identity without replacing any pre-existing artifact."""
    payload = (json.dumps(process_identity(), sort_keys=True) + "\n").encode()
    temporary = IDENTITY_ARTIFACT.with_name(
        f".{IDENTITY_ARTIFACT.name}.{os.getpid()}.partial"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, IDENTITY_ARTIFACT)
        directory = os.open(
            IDENTITY_ARTIFACT.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)


def main() -> int:
    publish_process_identity()
    for marker in (CLICK_MARKER, KEY_MARKER, READY_MARKER):
        marker.unlink(missing_ok=True)
    window = Gtk.Window(title=READY_TITLE)
    visual = window.get_screen().get_rgba_visual()
    if visual is None:
        raise RuntimeError("the hardware interaction fixture requires an RGBA visual")
    window.set_visual(visual)
    window.set_app_paintable(True)
    window.set_default_size(480, 320)
    window.connect("destroy", Gtk.main_quit)

    button = Gtk.Button(label="CLICK TO VERIFY POINTER INPUT")
    button.set_halign(Gtk.Align.CENTER)
    button.set_valign(Gtk.Align.CENTER)
    button.set_size_request(360, 120)
    provider = Gtk.CssProvider()
    provider.load_from_data(
        b"window { background-color: rgba(0, 0, 0, 0); } "
        b"button { font-size: 24px; background-image: none; "
        b"background-color: #274060; color: #ffffff; } "
        b"button.verified { background-color: #31d07b; color: #101820; }"
    )
    window.get_style_context().add_provider(
        provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
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

    def publish_ready() -> bool:
        READY_MARKER.write_text("GTK main loop ready\n", encoding="utf-8")
        return GLib.SOURCE_REMOVE

    GLib.idle_add(publish_ready)
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
