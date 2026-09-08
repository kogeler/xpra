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
from gi.repository import Gdk, GLib, Gtk  # noqa: E402 - select GI versions before importing

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
    if command in ("quit", "select", "select-end"):
        return (command, None)
    operation, separator, marker_id = command.partition(":")
    if operation not in ("own", "paste", "read") or not separator or marker_id not in marker_ids():
        return ("invalid", None)
    return operation, marker_id


def run(command_file: Path, diagnostic_file: Path = Path("/artifacts/native-paste-input.jsonl")) -> int:
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
    # Exercise the toolkit's real paste and popup paths without retaining
    # plaintext in the profile's screenshots. CSS affects paint, not selection
    # contents or GTK's clipboard conversion targets.
    text_view = Gtk.TextView()
    text_view.set_no_show_all(True)
    text_view.set_cursor_visible(False)
    text_view.set_size_request(-1, 64)
    text_buffer = text_view.get_buffer()
    paste_provider = Gtk.CssProvider()
    paste_provider.load_from_data(
        b"textview text, textview text selection { color: transparent; caret-color: transparent; }"
    )
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), paste_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    box.pack_start(text_view, True, True, 0)
    window.add(box)

    clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
    state = {
        "closed": False,
        "command_id": 0,
        "confirming_owner": None,
        "owner_confirmation_idle": 0,
        "pending_owner": None,
        "request_id": 0,
        "pending_paste": None,
        "selecting": False,
        "selection_keys": 0,
        "last_selection_key_ns": 0,
    }
    diagnostic_stream = diagnostic_file.open("x", encoding="utf-8")
    diagnostic_sequence = 0

    def diagnostic(event: str, **values: object) -> None:
        nonlocal diagnostic_sequence
        if state["closed"]:
            return
        if diagnostic_sequence >= 96:
            raise RuntimeError("native paste diagnostic event bound exceeded")
        print(json.dumps({"schema": 1, "sequence": diagnostic_sequence,
                          "monotonic_ns": time.monotonic_ns(), "event": event,
                          **values}, sort_keys=True), file=diagnostic_stream, flush=True)
        diagnostic_sequence += 1

    def finish() -> None:
        if state["closed"]:
            return
        state["closed"] = True
        if state["owner_confirmation_idle"]:
            GLib.source_remove(state["owner_confirmation_idle"])
            state["owner_confirmation_idle"] = 0
        state["confirming_owner"] = None
        state["pending_owner"] = None
        emitter.emit("closed", pid=os.getpid())
        diagnostic_stream.close()
        Gtk.main_quit()

    def paste_result(text: str | None, *, native: bool) -> None:
        request = state["pending_paste"]
        if request is None:
            raise RuntimeError("unexpected native GTK paste completion")
        state["pending_paste"] = None
        command_id, request_id, marker_id = request
        encoded = None if text is None else text.encode("utf-8")
        within_entry_bound = encoded is not None and len(encoded) <= MAX_ENTRY_BYTES
        entry.set_text(text if text is not None and within_entry_bound else "")
        text_view.hide()
        entry.show()
        window.set_focus(None)
        diagnostic("gtk-paste-done" if native else "gtk-read-done", request_id=request_id,
                   **marker_summary(marker_id, encoded))
        emitter.emit(
            "paste-result",
            command_id=command_id,
            request_id=request_id,
            within_entry_bound=within_entry_bound,
            **marker_summary(marker_id, encoded),
        )

    def paste_done(buffer: Gtk.TextBuffer, _clipboard: Gtk.Clipboard) -> None:
        paste_result(buffer.get_text(*buffer.get_bounds(), True) or None, native=True)

    def poll_command() -> bool:
        command = take_command(command_file)
        if command is None:
            return GLib.SOURCE_CONTINUE
        operation, marker_id = command
        if operation == "select":
            state["selecting"] = True
            state["selection_keys"] = 0
            entry.hide()
            text_view.show()
            text_view.set_cursor_visible(True)
            text_view.grab_focus()
            diagnostic("selection-armed")
            return GLib.SOURCE_CONTINUE
        if operation == "select-end":
            diagnostic("selection-finished", key_events=state["selection_keys"],
                       last_key_ns=state["last_selection_key_ns"],
                       characters=text_buffer.get_char_count(),
                       selection=[iterator.get_offset() for iterator in text_buffer.get_selection_bounds()])
            state["selecting"] = False
            text_view.set_cursor_visible(False)
            return GLib.SOURCE_CONTINUE
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
            text_view.hide()
            entry.show()
            window.set_focus(None)
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
        elif operation in ("paste", "read"):
            if state["pending_paste"] is not None:
                raise RuntimeError("previous native paste is still pending")
            state["request_id"] += 1
            request_id = state["request_id"]
            entry.set_text("")
            text_buffer.set_text("")
            entry.hide()
            text_view.show()
            text_view.grab_focus()
            state["pending_paste"] = (command_id, request_id, marker_id)
            emitter.emit(
                "paste-requested",
                command_id=command_id,
                marker_id=marker_id,
                request_id=request_id,
            )
            if operation == "read":
                # A disabled clipboard has no native offer: GtkTextBuffer does
                # not start a paste or emit paste-done in that case. Keep the
                # original asynchronous negative conversion control for off.
                clipboard.request_text(lambda _clipboard, text: paste_result(text, native=False))
        else:
            raise AssertionError(operation)
        return GLib.SOURCE_CONTINUE

    def publish_owner_confirmation(request: tuple[int, str]) -> bool:
        if state["closed"] or state["confirming_owner"] != request:
            return GLib.SOURCE_REMOVE
        state["owner_confirmation_idle"] = 0
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
        if state["closed"] or event.selection != Gdk.SELECTION_CLIPBOARD:
            return
        diagnostic("clipboard-owner-change", selection="CLIPBOARD")
        if (
            state["confirming_owner"] is not None
            and not state["owner_confirmation_idle"]
        ):
            # Keep the public record order deterministic even if a backend
            # happens to emit owner-change synchronously from set_text().
            state["owner_confirmation_idle"] = GLib.idle_add(
                publish_owner_confirmation, state["confirming_owner"]
            )

    def key_pressed(_window: Gtk.Window, event: Gdk.EventKey) -> bool:
        if event.keyval == Gdk.KEY_F8 and state["pending_owner"] is not None:
            request = state["pending_owner"]
            state["pending_owner"] = None
            state["confirming_owner"] = request
            command_id, marker_id = request
            emitter.emit("owner-input", command_id=command_id, keyval=int(event.keyval))
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
    primary = Gtk.Clipboard.get(Gdk.SELECTION_PRIMARY)
    primary.connect("owner-change", lambda *_args: diagnostic("clipboard-owner-change", selection="PRIMARY"))
    text_buffer.connect("paste-done", paste_done)

    def paste_key(_widget: Gtk.TextView, event: Gdk.EventKey) -> bool:
        if state["selecting"]:
            state["selection_keys"] += 1
            state["last_selection_key_ns"] = time.monotonic_ns()
        else:
            diagnostic("key-input", keyval=int(event.keyval), modifiers=int(event.state))
        return False

    def popup(_widget: Gtk.TextView, menu: Gtk.Menu) -> None:
        # Mnemonics are delivered to the popup, not its originating text view.
        menu.connect("key-press-event", paste_key)
        diagnostic("menu-populated", sensitive_items=sum(child.get_sensitive() for child in menu.get_children()))

    text_view.connect("key-press-event", paste_key)
    text_view.connect("populate-popup", popup)
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
