# This file is part of Xpra.
# Copyright (C) 2010 Antoine Martin <antoine@xpra.org>
# Copyright (C) 2008 Nathaniel Smith <njs@pobox.com>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import math
from typing import Any
from collections.abc import Sequence

from xpra.os_util import gi_import, OSX, WIN32
from xpra.util.objects import typedict
from xpra.util.env import envint, envbool, IgnoreWarningsContext
from xpra.client.gtk3.window.stub_window import GtkStubWindow
from xpra.client.gtk3.window.common import mask_buttons
from xpra.log import Logger

Gdk = gi_import("Gdk")

log = Logger("window", "pointer")


SMOOTH_SCROLL = envbool("XPRA_SMOOTH_SCROLL", True)
SMOOTH_SCROLL_NORM = envint("XPRA_SMOOTH_SCROLL_NORM", 50 if OSX else 100)
SKIP_DUPLICATE_SCROLL_EVENTS = envbool("XPRA_SKIP_DUPLICATE_SCROLL_EVENTS", True)
SIMULATE_MOUSE_DOWN = envbool("XPRA_SIMULATE_MOUSE_DOWN", True)
SIMULATE_MOUSE_UP = envbool("XPRA_SIMULATE_MOUSE_UP", True)
BUTTON_POLLING_DELAY = envint("XPRA_BUTTON_POLLING_DELAY", 50)
CURSOR_IDLE_TIMEOUT = envint("XPRA_CURSOR_IDLE_TIMEOUT", 6)


GDK_SCROLL_MAP: dict[Gdk.ScrollDirection, int] = {
    Gdk.ScrollDirection.UP: 4,
    Gdk.ScrollDirection.DOWN: 5,
    Gdk.ScrollDirection.LEFT: 6,
    Gdk.ScrollDirection.RIGHT: 7,
}


def _button_resolve(button: int) -> int:
    if WIN32 and button in (4, 5):
        # On Windows "X" buttons (the extra buttons sometimes found on the
        # side of the mouse) are numbered 4 and 5, as there is a different
        # API for scroll events. Convert them into the X11 convention of 8
        # and 9.
        return button + 4
    return button


def _device_info(event) -> str:
    try:
        return event.device.get_name()
    except AttributeError:
        return ""


def _get_pointer(event) -> tuple[int, int]:
    return round(event.x_root), round(event.y_root)


def _get_relative_pointer(event) -> tuple[int, int]:
    return round(event.x), round(event.y)


def _x11_scroll_key(event) -> tuple:
    window = event.window
    device = event.get_device()
    source = event.get_source_device()
    if window is None or device is None or source is None:
        return ()
    if window.get_display().__gtype__.name != "GdkX11Display":
        return ()
    return (event.time, window, device, source, event.x, event.y,
            event.x_root, event.y_root, int(event.state))


def norm_scroll(value: float):
    if SMOOTH_SCROLL_NORM == 100:
        return value
    smoothed = math.pow(abs(value), SMOOTH_SCROLL_NORM / 100)
    return math.copysign(smoothed, value)


class PointerWindow(GtkStubWindow):

    def init_window(self, client, metadata: typedict, client_props: typedict) -> None:
        self.cursor_data = ()
        self.remove_pointer_overlay_timer = 0
        self.show_pointer_overlay_timer = 0
        self.button_pressed: dict[int, int] = {}
        self.button_polling_timer = 0
        self.motion_cancels_pointer_overlay = True
        self._smooth_scroll_key = ()
        self._smooth_scroll_zero_axes = 0

    def cleanup(self) -> None:
        self._smooth_scroll_key = ()
        self._smooth_scroll_zero_axes = 0
        self.cancel_show_pointer_overlay_timer()
        self.cancel_remove_pointer_overlay_timer()

    def get_info(self) -> dict[str, Any]:
        return {
            "buttons-pressed": self.button_pressed,
            "cursor-data": bool(self.cursor_data),
        }

    def get_window_event_mask(self) -> Gdk.EventMask:
        mask: Gdk.EventMask = Gdk.EventMask.POINTER_MOTION_MASK
        mask |= Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
        mask |= Gdk.EventMask.SCROLL_MASK
        pointer = self.get_subsystem("pointer")
        if pointer and pointer.wheel_smooth:
            mask |= Gdk.EventMask.SMOOTH_SCROLL_MASK
        return mask

    def init_widget_events(self, widget) -> None:
        def motion(_w, event) -> bool:
            self._do_motion_notify_event(event)
            return True

        widget.connect("motion-notify-event", motion)

        def press(_w, event) -> bool:
            self._do_button_press_event(event)
            return True

        widget.connect("button-press-event", press)

        def release(_w, event) -> bool:
            self._do_button_release_event(event)
            return True

        widget.connect("button-release-event", release)

        def scroll(_w, event) -> bool:
            self._do_scroll_event(event)
            return True

        widget.connect("scroll-event", scroll)

    def get_mouse_event_wid(self, *_args) -> int:
        # used to be overridden in GTKClientWindowBase
        return self.wid

    ######################################################################
    # pointer overlay handling
    def set_cursor_data(self, cursor_data: Sequence) -> None:
        self.cursor_data = cursor_data
        if b := self._backing:
            self.when_realized("cursor", b.set_cursor_data, cursor_data)

    def cancel_remove_pointer_overlay_timer(self) -> None:
        if rpot := self.remove_pointer_overlay_timer:
            log(f"cancel_remove_pointer_overlay_timer() timer={rpot}")
            self.remove_pointer_overlay_timer = 0
            self.source_remove(rpot)

    def cancel_show_pointer_overlay_timer(self) -> None:
        if rsot := self.show_pointer_overlay_timer:
            log(f"cancel_show_pointer_overlay_timer() timer={rsot}")
            self.show_pointer_overlay_timer = 0
            self.source_remove(rsot)

    def show_pointer_overlay(self, pos: Sequence = ()) -> None:
        # schedule do_show_pointer_overlay if needed
        b = self._backing
        if not b:
            return
        prev = b.pointer_overlay
        if not pos:
            if not prev:
                return
            value = None
        else:
            if prev and prev[:2] == pos[:2]:
                return
            # store both scaled and unscaled value:
            # (the opengl client uses the raw value)
            value = pos[:2] + self.sp(*pos[:2]) + pos[2:]
        log("show_pointer_overlay(%s) previous value=%s, new value=%s", pos, prev, value)
        b.pointer_overlay = value
        if not self.show_pointer_overlay_timer:
            self.show_pointer_overlay_timer = self.timeout_add(10, self.do_show_pointer_overlay, prev)

    def do_show_pointer_overlay(self, prev: Sequence | None) -> None:
        # queue a draw event at the previous and current position of the pointer
        # (so the backend will repaint / overlay the cursor image there)
        self.show_pointer_overlay_timer = 0
        b = self._backing
        if not b:
            return
        value = b.pointer_overlay
        log("do_show_pointer_overlay: value=%s", value)
        if value:
            # repaint the scale value (in window coordinates):
            self.redraw()
            # clear it shortly after:
            self.schedule_remove_pointer_overlay()
        if prev:
            self.redraw()

    def schedule_remove_pointer_overlay(self, delay: int = CURSOR_IDLE_TIMEOUT * 1000) -> None:
        log(f"schedule_remove_pointer_overlay({delay})")
        self.cancel_remove_pointer_overlay_timer()
        self.remove_pointer_overlay_timer = self.timeout_add(delay, self.remove_pointer_overlay)

    def remove_pointer_overlay(self) -> None:
        log("remove_pointer_overlay()")
        self.remove_pointer_overlay_timer = 0
        self.show_pointer_overlay()

    def _do_button_press_event(self, event) -> None:
        # Gtk.Window.do_button_press_event(self, event)
        button = _button_resolve(event.button)
        self._button_action(button, event, True)

    def _do_button_release_event(self, event) -> None:
        # Gtk.Window.do_button_release_event(self, event)
        button = _button_resolve(event.button)
        self._button_action(button, event, False)

    ######################################################################
    # pointer motion

    def _do_motion_notify_event(self, event) -> None:
        # Gtk.Window.do_motion_notify_event(self, event)
        if self.motion_cancels_pointer_overlay:
            self.cancel_remove_pointer_overlay_timer()
            self.remove_pointer_overlay()
        # `server_pointer` is owned by the `pointer` subsystem (which may be disabled):
        pointer = self.get_subsystem("pointer")
        if self._client.readonly or self._client.server_readonly or (pointer and not pointer.server_pointer):
            return
        pointer_data, modifiers, buttons = self._pointer_modifiers(event)
        if self.button_polling_timer:
            self.cancel_button_polling()
            self.do_poll_buttons(pointer_data, modifiers, buttons)
            self.start_button_polling()
        wid = self.get_mouse_event_wid(*pointer_data)
        log("do_motion_notify_event(%s) wid=%#x / focus=%s / window wid=%#x",
            event, wid, self.get_subsystem("window")._focused, self.wid)
        log(" device=%s, pointer=%s, modifiers=%s, buttons=%s",
            _device_info(event), pointer_data, modifiers, buttons)
        device_id = 0
        if pointer:
            pointer.send_mouse_position(device_id, wid, pointer_data, modifiers, buttons)

    def get_mouse_position(self) -> tuple[int, int]:
        # this method is used on some platforms
        # to get the pointer position for events that don't include it
        # (ie: wheel events)
        x, y = self.get_subsystem("pointer").get_raw_mouse_position()
        return self._offset_pointer(x, y)

    def _offset_pointer(self, x: int, y: int) -> tuple[int, int]:
        if self.window_offset:
            x -= self.window_offset[0]
            y -= self.window_offset[1]
        return self.cp(x, y)

    def get_pointer_data(self, event) -> tuple[int, int, int, int]:
        x, y = _get_pointer(event)
        rx, ry = _get_relative_pointer(event)
        return self.adjusted_pointer_data(x, y, rx, ry)

    def adjusted_pointer_data(self, x: int, y: int, rx: int = 0, ry: int = 0) -> tuple[int, int, int, int]:
        # regular pointer coordinates are translated and scaled,
        # relative coordinates are scaled only:
        ox, oy = self._offset_pointer(x, y)
        cx, cy = self.cp(rx, ry)
        return ox, oy, cx, cy

    def _pointer_modifiers(self, event) -> tuple[tuple[int, int, int, int], list[str], list[int]]:
        pointer_data = self.get_pointer_data(event)
        # FIXME: state is used for both mods and buttons??
        # `mask_to_names` is owned by the `keyboard` subsystem (which may be absent):
        keyboard = self.get_subsystem("keyboard")
        modifiers = keyboard.mask_to_names(event.state) if keyboard else []
        buttons = mask_buttons(event.state)
        v = pointer_data, modifiers, buttons
        log("pointer_modifiers(%s)=%s (x_root=%s, y_root=%s, window_offset=%s)",
            event, v, event.x_root, event.y_root, self.window_offset)
        return v

    def _do_scroll_event(self, event) -> bool:
        pointer_sub = self.get_subsystem("pointer")
        if (not pointer_sub or self._client.readonly or self._client.server_readonly
                or not pointer_sub.server_pointer or not pointer_sub.wheel_map):
            self._smooth_scroll_key = ()
            self._smooth_scroll_zero_axes = 0
            return True
        if not pointer_sub.wheel_smooth or not SKIP_DUPLICATE_SCROLL_EVENTS:
            self._smooth_scroll_key = ()
            self._smooth_scroll_zero_axes = 0
        if event.direction == Gdk.ScrollDirection.SMOOTH:
            if not pointer_sub.wheel_smooth:
                return True
            # X11 delivers the valuator before its emulated buttons. GDK's
            # first/reset sample may have zero deltas; only those buttons
            # retain the missing whole steps. Replace even at the same time.
            self._smooth_scroll_key = _x11_scroll_key(event) if SKIP_DUPLICATE_SCROLL_EVENTS else ()
            self._smooth_scroll_zero_axes = (
                int(event.delta_x == 0) | (int(event.delta_y == 0) << 1)
            ) if self._smooth_scroll_key else 0
            log("smooth scroll event: %s, raw delta: %s,%s", event, event.delta_x, event.delta_y)
            pointer = self.get_pointer_data(event)
            device_id = -1
            norm_x = norm_scroll(event.delta_x)
            norm_y = norm_scroll(event.delta_y)
            pointer_sub.wheel_event(device_id, self.wid, norm_x, -norm_y, pointer)
            return True
        button_mapping = GDK_SCROLL_MAP.get(event.direction, -1)
        # Preserve upstream's opt-out. Wayland has no X11 valuator baseline:
        # its discrete copy comes first and never borrows an earlier zero.
        if SKIP_DUPLICATE_SCROLL_EVENTS and pointer_sub.wheel_smooth and event.get_pointer_emulated():
            axis = 2 if button_mapping in (4, 5) else 1 if button_mapping in (6, 7) else 0
            if not (self._smooth_scroll_key and self._smooth_scroll_zero_axes & axis
                    and self._smooth_scroll_key == _x11_scroll_key(event)):
                log("ignoring emulated scroll event: direction=%i", event.direction)
                return True
            # One initial sample may emulate several whole steps. The next
            # smooth sample replaces this bounded allowance, not this event.
            log("using discrete scroll for zero X11 valuator: direction=%i", event.direction)
        log("do_scroll_event device=%s, direction=%s, button_mapping=%s",
            _device_info(event), event.direction, button_mapping)
        if button_mapping >= 0:
            self._button_action(button_mapping, event, True)
            self._button_action(button_mapping, event, False)
        return True

    def translate_button(self, button: int, modifiers: list[str]) -> int:
        # `button_transform` is owned by the `pointer` subsystem (which may be disabled):
        pointer = self.get_subsystem("pointer")
        transform = pointer.button_transform if pointer else None
        if not transform:
            return button
        for modifier in modifiers:
            trans = transform.get((modifier, button), -1)
            if trans >= 0:
                log("translate_button(%i, %s) -> %s", button, modifiers, trans)
                # we could consume the modifier,
                # but pointer motion events would still include it
                return trans
        return button

    def _button_action(self, button: int, event, depressed: bool, props=None) -> None:
        # button events are sent by the `pointer` subsystem, which may be disabled:
        pointer = self.get_subsystem("pointer")
        if not pointer or self._client.readonly or self._client.server_readonly or not pointer.server_pointer:
            return
        if not pointer.middle_click and button == 2:
            log("_button_action: middle click suppressed (middle-click=no)")
            return
        pointer_data, modifiers, buttons = self._pointer_modifiers(event)
        wid = self.get_mouse_event_wid(*pointer_data)
        log("_button_action(%s, %s, %s) wid=%#x / focus=%s / window wid=%#x",
            button, event, depressed, wid, self.get_subsystem("window")._focused, self.wid)
        log(" device=%s, pointer=%s, modifiers=%s, buttons=%s",
            _device_info(event), pointer_data, modifiers, buttons)
        device_id = 0

        def send_button(server_button, pressed, **kwargs) -> None:
            sprops = props or {}
            sprops.update(kwargs)
            pointer.send_button(device_id, wid, server_button, pressed, pointer_data, modifiers, buttons, sprops)

        server_button = -1
        if not depressed:
            # we should have a record of which button press event was sent:
            server_button = self.button_pressed.get(button, -1)
            if SIMULATE_MOUSE_DOWN and server_button < 0:
                log("button action: simulating missing mouse-down event for window %s before mouse-up", wid)
                # (needed for some dialogs on win32):
                server_button = self.translate_button(button, modifiers)
                send_button(server_button, True, synthetic=True)

        if server_button < 0:
            server_button = self.translate_button(button, modifiers)

        if depressed:
            self.button_pressed[button] = server_button
        else:
            self.button_pressed.pop(button, None)
            if not self.button_pressed:
                self.cancel_button_polling()
                self.cancel_moveresize_cursor()
        send_button(server_button, depressed)

    def cancel_button_polling(self) -> None:
        log("cancel_button_polling()")
        if bpt := self.button_polling_timer:
            self.button_polling_timer = 0
            self.source_remove(bpt)

    def start_button_polling(self) -> None:
        log("start_button_polling()")
        if self.button_polling_timer:
            return
        self.button_polling_timer = self.timeout_add(BUTTON_POLLING_DELAY, self.poll_buttons)

    def poll_buttons(self) -> bool:
        with IgnoreWarningsContext():
            x, y, mask = self.get_root_window().get_pointer()[-3:]
        buttons = mask_buttons(mask)
        keyboard = self.get_subsystem("keyboard")
        modifiers = keyboard.mask_to_names(mask) if keyboard else []
        self.do_poll_buttons((x, y), modifiers, buttons)
        if not self.button_pressed:
            self.button_polling_timer = 0
            return False
        return True

    def do_poll_buttons(self, pointer_data: Sequence[int], modifiers: list[str], buttons: Sequence[int]) -> None:
        pointer = self.get_subsystem("pointer")
        pressed = tuple(self.button_pressed.keys())
        log("do_poll_buttons(%s, %s) pressed=%s", pointer_data, buttons, pressed)
        if pressed and not any(button in buttons for button in pressed):
            # the drag is over as far as the pointer device is concerned,
            # give the cursor back even if we don't simulate the release below:
            self.cancel_moveresize_cursor()
        for button in pressed:
            if button not in buttons:
                log(f"button {button=} unpressed")
                if SIMULATE_MOUSE_UP and pointer:
                    device_id = 0
                    wid = self.get_mouse_event_wid()
                    # use the button we sent the press with:
                    # the modifiers may have changed since, and `translate_button`
                    # would then release a different button from the one held down
                    server_button = self.button_pressed.get(button, -1)
                    if server_button < 0:
                        server_button = self.translate_button(button, modifiers)
                    sprops = {}
                    pointer.send_button(device_id, wid, server_button, False, pointer_data, modifiers, buttons, sprops)
                    self.button_pressed.pop(button, None)

    def do_button_press_event(self, event) -> None:
        self._button_action(event.button, event, True)

    def do_button_release_event(self, event) -> None:
        self._button_action(event.button, event, False)
