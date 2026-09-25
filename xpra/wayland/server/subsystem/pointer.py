# This file is part of Xpra.
# Copyright (C) 2026 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

from collections.abc import Sequence

from xpra.net.common import Packet
from xpra.server.subsystem.pointer import PointerManager
from xpra.log import Logger

log = Logger("server", "wayland")


class WaylandPointerManager(PointerManager):
    __slots__ = ()
    # the compositor tracks its own cursor position,
    # and every event needs the `flush()` that comes with a move:
    SKIP_REDUNDANT_MOVES = False

    def make_pointer_device(self):
        return self.server.compositor.get_pointer_device()

    def cleanup(self) -> None:
        super().cleanup()
        if device := self.pointer_device:
            try:
                self.clear_pointer_focus()
            finally:
                device.cleanup()
                self.pointer_device = None
        self.pointer_device_map = {}

    def clear_pointer_focus(self, *surface_wids: int) -> bool:
        """Clear an exact native focus before its surface role is detached."""
        window = self.server.subsystems.get("window")
        focus_wid = getattr(window, "pointer_focus", 0)
        if surface_wids and focus_wid not in surface_wids:
            return False
        if not focus_wid and surface_wids:
            return False
        cleared = bool(focus_wid)
        if self.pointer_device:
            try:
                self.pointer_device.leave_surface()
            except BaseException:
                # Native role teardown must continue even when an already
                # failing seat cannot emit its final leave event.
                log.error("Error clearing native Wayland pointer focus", exc_info=True)
        if window is not None:
            window.pointer_focus = 0
        try:
            self.server.compositor.flush()
        except BaseException:
            # The focused identity is already invalidated.  A downstream
            # display flush failure cannot leave topology cleanup half-done.
            log.error("Error flushing cleared Wayland pointer focus", exc_info=True)
        return cleared

    def set_pointer_focus(self, wid: int, pointer: Sequence):
        server = self.server
        window = server.subsystems["window"]
        log("set_pointer_focus(%i, %s)", wid, pointer)
        log(" current focus=%i", window.pointer_focus)
        if wid == 0:
            self.clear_pointer_focus()
            return pointer
        surface = window.get_surface(wid)
        log("surface(%i)=%s", wid, surface)
        try:
            if not surface or len(pointer) < 4 or not (ptr := surface.xdg_surface_ptr):
                self.clear_pointer_focus()
                return None
            x, y = pointer[2:4]
            target = self.pointer_device.enter_surface(ptr, x, y)
            if not target:
                self.clear_pointer_focus()
                return None
            target_ptr = int(target[0])
            target_wid = window.get_surface_wid(target_ptr, wid)
            if not target_wid:
                log.error(
                    "Error: native pointer target %#x is not registered under root %#x",
                    target_ptr, wid,
                )
                self.clear_pointer_focus()
                return None
            # This is the internal native surface identity.  Wire pointer
            # packets and synchronized peer events continue to use `wid`.
            previous_wid = window.pointer_focus
            window.pointer_focus = target_wid
            if target_wid != previous_wid:
                log.info(
                    "Wayland pointer target root=%#x surface=%#x local=%.3f,%.3f",
                    wid, target_wid, float(target[1]), float(target[2]),
                )
        except BaseException:
            log.error("Error resolving native pointer target for root %#x", wid, exc_info=True)
            self.clear_pointer_focus()
            return None
        else:
            server.compositor.flush()
            return pointer

    def _adjust_pointer(self, proto, device_id: int, wid: int, pointer):
        pointer = super()._adjust_pointer(proto, device_id, wid, pointer)
        if not pointer:
            return None
        # Entering the wlroots-resolved leaf makes parent-local coordinates
        # meaningful for motion, button and wheel delivery.
        return self.set_pointer_focus(wid, pointer)

    def get_pointer_target(self, proto, wid: int, pos, props=None) -> tuple[int, int]:
        # wayland surfaces are fed surface-relative coordinates:
        if len(pos) >= 4:
            return pos[2], pos[3]
        return pos[0], pos[1]

    def _move_pointer(self, device_id: int, wid: int, pos, props=None) -> None:
        try:
            super()._move_pointer(device_id, wid, pos, props)
        finally:
            self.server.compositor.flush()

    def _update_modifiers(self, proto, wid: int, modifiers: Sequence[str]) -> None:
        # The generic path owns readonly and UI-driver arbitration.  Mirror
        # those guards before touching the shared wlroots keyboard state.
        if self.is_readonly(proto):
            return
        source = self.get_server_source(proto)
        if not source or (self.server.ui_driver and self.server.ui_driver != source.uuid):
            return
        super()._update_modifiers(proto, wid, modifiers)
        keyboard = self.server.subsystems.get("keyboard")
        if keyboard:
            keyboard.update_keyboard_modifiers(modifiers, source=source)

    def button_action(self, device_id: int, wid: int, button: int, pressed: bool, props: dict) -> None:
        try:
            super().button_action(device_id, wid, button, pressed, props)
        finally:
            self.server.compositor.flush()

    def _process_wheel(self, proto, packet: Packet) -> None:
        try:
            super()._process_wheel(proto, packet)
        finally:
            self.server.compositor.flush()
