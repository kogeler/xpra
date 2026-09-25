#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 kogeler
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import unittest
from collections import deque
from types import SimpleNamespace

from xpra.wayland.server.subsystem.pointer import WaylandPointerManager


class FakePointerDevice:

    def __init__(self):
        self.enter_results = deque()
        self.calls = []

    def enter_surface(self, xdg_surface_ptr, x, y):
        self.calls.append(("enter", xdg_surface_ptr, x, y))
        return self.enter_results.popleft()

    def leave_surface(self):
        self.calls.append(("leave",))

    def move_pointer(self, x, y, props):
        self.calls.append(("move", x, y, dict(props)))

    def click(self, button, pressed, props):
        self.calls.append(("click", button, pressed, dict(props)))

    def get_position(self):
        return 0, 0


class FakeWindowSubsystem:

    def __init__(self):
        self.pointer_focus = 0
        self.root = SimpleNamespace(xdg_surface_ptr=0x1000, wl_surface_ptr=0x1100)
        self.surface_wids = {0x1100: 7, 0x2000: 2, 0x3000: 3}

    def get_surface(self, wid):
        return self.root if wid == 7 else None

    def get_surface_wid(self, surface_ptr, _root_wid=0):
        return self.surface_wids.get(surface_ptr, 0)


class TestWaylandPointerManager(WaylandPointerManager):

    def is_readonly(self, _proto=None):
        return False

    def may_record_pointer_event(self, *_args, **_kwargs):
        return None


def make_manager():
    window = FakeWindowSubsystem()
    compositor = SimpleNamespace(flush_calls=0)

    def flush():
        compositor.flush_calls += 1

    compositor.flush = flush
    server = SimpleNamespace(
        compositor=compositor,
        subsystems={"window": window},
        readonly=False,
        ui_driver=None,
        idle_add=lambda *_args: 0,
        timeout_add=lambda *_args: 0,
        source_remove=lambda *_args: None,
        get_server_source=lambda _proto: None,
    )
    manager = TestWaylandPointerManager(server)
    manager.pointer_device = FakePointerDevice()
    return manager, window, compositor


class WaylandPointerFocusTest(unittest.TestCase):

    def test_same_wire_root_rehit_tests_root_and_upper_child(self):
        manager, window, _compositor = make_manager()
        device = manager.pointer_device
        device.enter_results.extend((
            (0x1100, 230.0, 200.0),
            (0x2000, 30.0, 20.0),
        ))

        first = manager.process_mouse_common(object(), 0, 7, (230, 200, 230, 200), {})
        self.assertEqual(first, (230, 200, 230, 200))
        self.assertEqual(window.pointer_focus, 7)
        second = manager.process_mouse_common(object(), 0, 7, (230, 200, 230, 200), {})
        self.assertEqual(second, (230, 200, 230, 200))
        self.assertEqual(window.pointer_focus, 2)
        self.assertEqual(
            [call for call in device.calls if call[0] == "enter"],
            [("enter", 0x1000, 230, 200), ("enter", 0x1000, 230, 200)],
        )
        # The native device owns translation to the resolved leaf; the stable
        # protocol stream and motion hook retain parent-local coordinates.
        self.assertEqual(
            [call[1:3] for call in device.calls if call[0] == "move"],
            [(230, 200), (230, 200)],
        )

    def test_child_move_recomputes_signed_leaf_translation(self):
        manager, window, _compositor = make_manager()
        device = manager.pointer_device
        device.enter_results.extend((
            (0x2000, 5.5, 7.25),
            (0x2000, -14.5, 7.25),
        ))

        manager.process_mouse_common(object(), 0, 7, (30, 40, 30, 40), {})
        manager.process_mouse_common(object(), 0, 7, (30, 40, 30, 40), {})

        self.assertEqual(window.pointer_focus, 2)
        self.assertEqual(len([call for call in device.calls if call[0] == "enter"]), 2)
        self.assertEqual(
            [call[1:3] for call in device.calls if call[0] == "move"],
            [(30, 40), (30, 40)],
        )

    def test_null_input_region_target_clears_focus_and_drops_click(self):
        manager, window, compositor = make_manager()
        device = manager.pointer_device
        window.pointer_focus = 2
        device.enter_results.append(())

        manager.process_pointer_button(
            object(), 0, 7, 1, True, (230, 200, 230, 200), {},
        )

        self.assertEqual(window.pointer_focus, 0)
        self.assertIn(("leave",), device.calls)
        self.assertFalse(any(call[0] == "move" for call in device.calls))
        self.assertFalse(any(call[0] == "click" for call in device.calls))
        self.assertGreater(compositor.flush_calls, 0)

    def test_exact_lifecycle_clear_does_not_drop_another_leaf(self):
        manager, window, compositor = make_manager()
        window.pointer_focus = 2

        self.assertFalse(manager.clear_pointer_focus(3))
        self.assertEqual(window.pointer_focus, 2)
        self.assertEqual(manager.pointer_device.calls, [])
        self.assertTrue(manager.clear_pointer_focus(2))
        self.assertEqual(window.pointer_focus, 0)
        self.assertEqual(manager.pointer_device.calls, [("leave",)])
        self.assertEqual(compositor.flush_calls, 1)

    def test_focus_clear_failures_cannot_block_role_teardown(self):
        manager, window, compositor = make_manager()
        window.pointer_focus = 2
        device = manager.pointer_device

        def failing_leave():
            device.calls.append(("leave",))
            raise RuntimeError("seat failed")

        def failing_flush():
            compositor.flush_calls += 1
            raise RuntimeError("display failed")

        device.leave_surface = failing_leave
        compositor.flush = failing_flush

        self.assertTrue(manager.clear_pointer_focus(2))
        self.assertEqual(window.pointer_focus, 0)
        self.assertEqual(device.calls, [("leave",)])
        self.assertEqual(compositor.flush_calls, 1)


def main():
    unittest.main()


if __name__ == "__main__":
    main()
