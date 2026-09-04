# Copyright (C) 2026 kogeler

"""Native-Wayland GTK clipboard fixture with digest-only evidence."""

from __future__ import annotations

import argparse
import json
import os
import signal
import stat
import time
from pathlib import Path
from typing import Final

from clipboard_fixture_common import marker_ids, marker_summary, marker_text

os.environ.setdefault("GDK_BACKEND", "wayland")

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk

TITLE: Final = "Xpra Wayland Clipboard Fixture"
MAX_COMMAND_BYTES: Final = 128
MAX_ENTRY_BYTES: Final = 4096


class JsonlEmitter:
    """Emit ordered evidence without clipboard text."""

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


def take_command(command_file: Path) -> tuple[str, str | None] | None:
    """Consume only a fixed-marker paste/own command or the quit command."""
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
    if operation not in ("own", "paste") or not separator or marker_id not in marker_ids():
        return ("invalid", None)
    return operation, marker_id


def run(command_file: Path) -> int:
    emitter = JsonlEmitter()
    display = Gdk.Display.get_default()
    if display is None or "wayland" not in display.get_name().casefold():
        raise RuntimeError("the clipboard fixture requires a native Wayland display")
    command_file.unlink(missing_ok=True)

    window = Gtk.Window(title=TITLE)
    window.set_default_size(720, 240)
    window.set_resizable(False)

    provider = Gtk.CssProvider()
    provider.load_from_data(
        b"""
window {
  background-image: linear-gradient(135deg, #243447, #4b6584, #9fb3c8);
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
    label = Gtk.Label(label="Native Wayland clipboard transfer")
    entry = Gtk.Entry()
    entry.set_hexpand(True)
    # Clipboard contents are updated programmatically.  Keeping the entry out
    # of the focus chain avoids an unrelated GTK focus animation while the
    # clipboard profile validates a static RGB transport boundary.
    entry.set_can_focus(False)
    entry.set_visibility(False)
    entry.set_invisible_char("\u2022")
    entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
    box.pack_start(label, False, False, 0)
    box.pack_start(entry, True, True, 0)
    window.add(box)

    clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
    state = {
        "closed": False,
        "command_id": 0,
        "confirming_owner": None,
        "owner_confirmation_pending": False,
        "pending_owner": None,
        "request_id": 0,
    }

    def finish() -> None:
        if state["closed"]:
            return
        state["closed"] = True
        emitter.emit("closed", pid=os.getpid())
        Gtk.main_quit()

    def paste_result(_clipboard: Gtk.Clipboard, text: str | None, request: tuple[int, int, str]) -> None:
        command_id, request_id, marker_id = request
        encoded = None if text is None else text.encode("utf-8")
        within_entry_bound = encoded is not None and len(encoded) <= MAX_ENTRY_BYTES
        entry.set_text(text if text is not None and within_entry_bound else "")
        emitter.emit(
            "paste-result",
            command_id=command_id,
            request_id=request_id,
            within_entry_bound=within_entry_bound,
            **marker_summary(marker_id, encoded),
        )

    def poll_command() -> bool:
        command = take_command(command_file)
        if command is None:
            return GLib.SOURCE_CONTINUE
        operation, marker_id = command
        state["command_id"] += 1
        command_id = state["command_id"]
        if operation == "invalid":
            emitter.emit("command-rejected", command_id=command_id, reason="invalid-command")
            return GLib.SOURCE_CONTINUE
        if operation == "quit":
            emitter.emit("command-accepted", command_id=command_id, operation="quit")
            finish()
            return GLib.SOURCE_REMOVE
        assert marker_id is not None
        if operation == "own":
            if state["pending_owner"] is not None or state["confirming_owner"] is not None:
                emitter.emit(
                    "command-rejected",
                    command_id=command_id,
                    reason="owner-change-pending",
                )
                return GLib.SOURCE_CONTINUE
            state["pending_owner"] = (command_id, marker_id)
            emitter.emit(
                "owner-armed",
                command_id=command_id,
                marker_id=marker_id,
            )
        elif operation == "paste":
            state["request_id"] += 1
            request_id = state["request_id"]
            entry.set_text("")
            emitter.emit(
                "paste-requested",
                command_id=command_id,
                marker_id=marker_id,
                request_id=request_id,
            )
            clipboard.request_text(paste_result, (command_id, request_id, marker_id))
        else:
            raise AssertionError(operation)
        return GLib.SOURCE_CONTINUE

    def publish_owner_confirmation() -> bool:
        state["owner_confirmation_pending"] = False
        request = state["confirming_owner"]
        if request is None:
            return GLib.SOURCE_REMOVE
        state["confirming_owner"] = None
        command_id, marker_id = request
        emitter.emit(
            "owner-confirmed",
            command_id=command_id,
            **marker_summary(marker_id, marker_text(marker_id)),
        )
        return GLib.SOURCE_REMOVE

    def clipboard_owner_changed(
        _clipboard: Gtk.Clipboard,
        event: Gdk.EventOwnerChange,
    ) -> None:
        if event.selection != Gdk.SELECTION_CLIPBOARD:
            return
        if (
            state["confirming_owner"] is not None
            and not state["owner_confirmation_pending"]
        ):
            state["owner_confirmation_pending"] = True
            # Keep the public record order deterministic even if a backend
            # happens to emit owner-change synchronously from set_text().
            GLib.idle_add(publish_owner_confirmation)

    def key_pressed(_window: Gtk.Window, event: Gdk.EventKey) -> bool:
        if event.keyval == Gdk.KEY_F8 and state["pending_owner"] is not None:
            request = state["pending_owner"]
            state["pending_owner"] = None
            state["confirming_owner"] = request
            command_id, marker_id = request
            clipboard.set_text(marker_text(marker_id), -1)
            display.flush()
            emitter.emit(
                "owner-set",
                command_id=command_id,
                **marker_summary(marker_id, marker_text(marker_id)),
            )
            return True
        if event.keyval != Gdk.KEY_Escape:
            return False
        emitter.emit("escape-received")
        finish()
        return True

    def delete(_window: Gtk.Window, _event: Gdk.Event) -> bool:
        finish()
        return True

    def handle_signal(*_args: object) -> None:
        finish()

    window.connect("key-press-event", key_pressed)
    window.connect("delete-event", delete)
    clipboard.connect("owner-change", clipboard_owner_changed)
    window.show_all()

    def ready() -> bool:
        emitter.emit("ready", backend="wayland", pid=os.getpid(), title=TITLE)
        return GLib.SOURCE_REMOVE

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    GLib.idle_add(ready)
    GLib.timeout_add(25, poll_command)
    Gtk.main()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--command-file",
        type=Path,
        default=Path("/tmp/xpra-wayland-clipboard-command"),
    )
    args = parser.parse_args()
    return run(args.command_file)


if __name__ == "__main__":
    raise SystemExit(main())
