#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2019 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import unittest
from unittest.mock import Mock, patch

from xpra.os_util import WIN32, OSX
from xpra.net.common import Packet
from xpra.net.packet_type import WINDOW_STACKING
from xpra.util.objects import AdHocStruct, typedict
from unit.process_test_util import DisplayContext
from unit.client.subsystem.clientmixintest_util import ClientMixinTest

# Tests-only clean controls must reach existing packet delivery, not fail while
# importing a production constant which only the candidate introduces.
SUBSURFACE_CLIENT_BACKING_STATE = "_client-subsurface-backing-state"


class WindowManagerTest(ClientMixinTest):

    def test_missing_resize_counter_defaults_to_zero(self):
        from xpra.client.subsystem.window.manager import WindowManagerClient

        resize_counters = []

        class Display:
            @staticmethod
            def sx(value):
                return value

            @staticmethod
            def sy(value):
                return value

        class Window:
            @staticmethod
            def move_resize(_x, _y, _w, _h, resize_counter):
                resize_counters.append(resize_counter)

            @staticmethod
            def resize(_w, _h, resize_counter):
                resize_counters.append(resize_counter)

        manager = AdHocStruct()
        manager.get_subsystem = lambda _name: Display()
        manager.get_window = lambda _wid: Window()
        WindowManagerClient._process_move_resize(
            manager, Packet("window-move-resize", 1, 2, 3, 4, 5),
        )
        WindowManagerClient._process_resized(
            manager, Packet("window-resized", 1, 4, 5),
        )
        self.assertEqual(resize_counters, [0, 0])

    @unittest.skipUnless(WIN32, "win32 only")
    def test_win32_window_stacking(self):
        from xpra.platform.win32 import constants as win32con
        from xpra.platform.win32.window_stacking import Win32WindowStackingWatcher

        def window(hwnd: int, tray=False):
            model = Mock()
            model.is_tray.return_value = tray
            model.get_window_handle.return_value = hwnd
            return model

        # a tray has no window handle at all: `get_window_handle` must not be called on it
        tray = Mock()
        tray.is_tray.return_value = True
        del tray.get_window_handle

        window_client = AdHocStruct()
        window_client._id_to_window = {
            1: window(0x101),
            2: window(0x102),
            3: window(0),
            4: tray,
        }
        window_client.server_window_stacking = True
        window_client.send_window_stacking = Mock()
        window_client.client = AdHocStruct()
        window_client.client.after_handshake = Mock()

        watcher = Win32WindowStackingWatcher(window_client)
        watcher.setup()
        window_client.client.after_handshake.assert_called_once_with(watcher.do_setup)

        prefix = "xpra.platform.win32.window_stacking."
        with patch(prefix + "get_hwnd_stacking", return_value=(0x999, 0x102, 0x101)) as get_hwnd_stacking:
            with patch(prefix + "SetWinEventHook", return_value=0x1234) as set_hook:
                watcher.do_setup()
        # a single hook covers `EVENT_OBJECT_CREATE` .. `EVENT_OBJECT_REORDER`:
        set_hook.assert_called_once()
        self.assertEqual(set_hook.call_args[0][:2], (win32con.EVENT_OBJECT_CREATE, win32con.EVENT_OBJECT_REORDER))
        # only the windows which do have a handle are looked up:
        self.assertEqual(get_hwnd_stacking.call_args[0][0], {0x101: 1, 0x102: 2})
        # `EnumWindows` returns the topmost window first, the packet is bottom-to-top:
        window_client.send_window_stacking.assert_called_once_with((1, 2))

        # events for the controls within a window must not trigger an update:
        watcher.win_event(0, win32con.EVENT_OBJECT_SHOW, 0x101, win32con.OBJID_CLIENT, 9, 0, 0)
        self.assertFalse(watcher.stacking_timer)
        watcher.win_event(0, win32con.EVENT_OBJECT_SHOW, 0x101, win32con.OBJID_CARET, win32con.CHILDID_SELF, 0, 0)
        self.assertFalse(watcher.stacking_timer)
        # whole windows appearing, disappearing or moving in the z-order do:
        watcher.win_event(0, win32con.EVENT_OBJECT_HIDE, 0x101, win32con.OBJID_WINDOW, win32con.CHILDID_SELF, 0, 0)
        self.assertTrue(watcher.stacking_timer)
        watcher.cancel_stacking_timer()
        # reorders are reported against the desktop window using `OBJID_CLIENT`:
        watcher.win_event(0, win32con.EVENT_OBJECT_REORDER, 0x10010, win32con.OBJID_CLIENT, win32con.CHILDID_SELF, 0, 0)
        self.assertTrue(watcher.stacking_timer)

        with patch(prefix + "UnhookWinEvent") as unhook:
            watcher.cleanup()
        unhook.assert_called_once_with(0x1234)
        self.assertFalse(watcher.stacking_timer)
        self.assertIsNone(watcher.hook)
        # the ctypes callback must outlive the hook: events queued before
        # `UnhookWinEvent` can still be delivered
        self.assertIsNotNone(watcher.callback)

    @unittest.skipUnless(OSX, "macOS only")
    def test_darwin_window_stacking(self):
        from xpra.platform.darwin.window_stacking import DarwinWindowStackingWatcher, get_notification_names

        def window(number: int, tray=False):
            model = Mock()
            model.is_tray.return_value = tray
            model.window_number = number
            return model

        # a tray is an `NSStatusItem`: `get_window_handle` must not be called on it
        tray = Mock()
        tray.is_tray.return_value = True
        del tray.get_window_handle

        window_client = AdHocStruct()
        window_client._id_to_window = {
            1: window(101),
            2: window(102),
            3: window(0),
            4: tray,
        }
        window_client.server_window_stacking = True
        window_client.send_window_stacking = Mock()
        window_client.client = AdHocStruct()
        window_client.client.after_handshake = Mock()

        watcher = DarwinWindowStackingWatcher(window_client)
        watcher.setup()
        window_client.client.after_handshake.assert_called_once_with(watcher.do_setup)

        def get_nswindow(win):
            number = win.window_number
            if not number:
                # not realized yet: no `NSWindow`
                return None
            nswindow = Mock()
            nswindow.windowNumber.return_value = number
            return nswindow

        prefix = "xpra.platform.darwin.window_stacking."
        # 999 is a window of ours which is not a client window (ie: the splash screen):
        with patch(prefix + "get_nswindow", side_effect=get_nswindow):
            with patch(prefix + "get_window_numbers", return_value=(999, 102, 101)):
                with patch(prefix + "NSNotificationCenter") as center:
                    watcher.do_setup()
        # a single observer for every notification, matched on the name only:
        add_observer = center.defaultCenter.return_value.addObserver_selector_name_object_
        self.assertEqual(add_observer.call_count, len(get_notification_names()))
        for call in add_observer.call_args_list:
            self.assertEqual(call[0][0], watcher.observer)
            self.assertEqual(call[0][1], b"orderMayHaveChanged:")
        # the window server returns the topmost window first, the packet is bottom-to-top:
        window_client.send_window_stacking.assert_called_once_with((1, 2))

        # the notifications are coalesced:
        self.assertFalse(watcher.stacking_timer)
        watcher.schedule_update()
        timer = watcher.stacking_timer
        self.assertTrue(timer)
        watcher.schedule_update()
        self.assertEqual(watcher.stacking_timer, timer)

        with patch(prefix + "NSNotificationCenter") as center:
            watcher.cleanup()
        center.defaultCenter.return_value.removeObserver_.assert_called_once()
        self.assertFalse(watcher.stacking_timer)
        self.assertIsNone(watcher.observer)

    def test_windowmanager(self):
        with DisplayContext():
            from xpra.client.subsystem.window import WindowClient
            # `get_mouse_position` delegates to the owning client
            # (`WindowPointer.get_mouse_position`), and the test harness
            # provides it as the client stand-in:
            opts = AdHocStruct()
            opts.system_tray = True
            opts.cursors = True
            opts.bell = True
            opts.input_devices = True
            opts.auto_refresh_delay = 0
            opts.min_size = "100x100"
            opts.max_size = "2000x2000"
            opts.pixel_depth = 24
            opts.windows = True
            opts.sharing = "no"
            opts.window_close = "forward"
            opts.modal_windows = True
            opts.border = "red"
            opts.tray_icon = "yes"
            self._test_mixin_class(WindowClient, opts, {"window": {"stacking": True}})
            self.assertNotIn("sync-stacking", self.mixin.get_window_caps())
            self.assertNotIn("subsurface-composite", self.mixin.get_window_caps())
            self.mixin.send_window_stacking((3, 1, 3, 2))
            self.verify_packet(-1, (WINDOW_STACKING, [3, 1, 2]))
            packet_count = len(self.packets)
            self.mixin.send_window_stacking((3, 1, 2))
            self.assertEqual(len(self.packets), packet_count)
            self.mixin.server_window_stacking = False
            self.mixin.send_window_stacking((2, 1))
            self.assertEqual(len(self.packets), packet_count)


class WindowDrawDeliveryStateTest(unittest.TestCase):

    class QueuedWindowDraw:
        """Build the real mixin lazily so its slot layout remains authoritative."""

        @staticmethod
        def make(client):
            from xpra.client.base.stub import StubClientSubsystem
            from xpra.client.subsystem.window.draw import WindowDraw

            class Harness(WindowDraw):
                __slots__ = ("_draw_counter", "pixel_counter", "decode_work")

                def add_decode_work(self, fn, *args) -> None:
                    self.decode_work.append((fn, args))

            draw = Harness()
            StubClientSubsystem.__init__(draw, client)
            WindowDraw.__init__(draw)
            draw.decode_work = []
            return draw

    @staticmethod
    def packet(sequence=1, coding="rgb32") -> Packet:
        return Packet(
            "window-draw", 1, 0, 0, 1, 1, coding, bytes(4), sequence, 4,
            {
                "rgb_format": "BGRA",
                "subsurface-composite": "premultiplied-source-over-v1",
                "subsurface-transaction-id": sequence,
                "subsurface-stage-index": 0,
                "subsurface-stage-count": 1,
                "subsurface-topology-epoch": 1,
                "subsurface-backing-epoch": 1,
                "subsurface-reset": (0, 0, 1, 1),
                "flush": 0,
            },
        )

    def make_draw(self):
        calls = []
        sent = []
        backing = AdHocStruct()
        backing._subsurface_local_backing_epoch = 1
        backing.composite_active = True
        backing.rejects = []

        def reject(transaction_id, callbacks, message):
            backing.composite_active = False
            backing.rejects.append((transaction_id, message))
            for callback in callbacks:
                callback(False, message)

        backing.reject_subsurface_composite = reject
        window = AdHocStruct()
        window._backing = backing
        window._subsurface_backing_generation = 1
        window.draw_region = lambda *args: calls.append(args)
        registry = AdHocStruct()
        registry.get_window = lambda wid: window if wid == 1 else None
        client = AdHocStruct()
        client.subsystems = {"window": registry}
        client.idle_add = lambda fn, *args: fn(*args)
        client.timeout_add = lambda _delay, fn, *args: fn(*args)
        client.source_remove = lambda _source: None
        client.send_now = lambda *packet: sent.append(packet)
        return self.QueuedWindowDraw.make(client), window, backing, calls, sent

    def test_dequeued_composite_rejects_replaced_backing(self):
        draw, window, _backing, calls, sent = self.make_draw()
        draw._process_draw(self.packet())
        replacement = AdHocStruct()
        replacement._subsurface_local_backing_epoch = 1
        window._backing = replacement

        fn, args = draw.decode_work.pop()
        fn(*args)
        self.assertEqual(calls, [])
        self.assertIn("stale subsurface draw target", sent[-1][-1])

    def test_stale_mmap_composite_drains_exact_descriptor_once_before_ack(self):
        import mmap
        from xpra.client.subsystem import mmap as mmap_module
        from xpra.client.subsystem.window import draw as draw_module
        from xpra.net.mmap.io import int_from_buffer

        class CountedMmap(mmap_module.MmapClient):
            __slots__ = ("release_count",)

            def free_packet_chunks(self, data, options):
                self.release_count += 1
                super().free_packet_chunks(data, options)

        for compatible, legacy in ((True, True), (True, False), (False, False)):
            for transition in ("replace", "window-generation", "backing-epoch"):
                with self.subTest(compatible=compatible, legacy=legacy, transition=transition):
                    draw, window, backing, calls, sent = self.make_draw()
                    pending_ui = []
                    draw.idle_add = lambda callback, *args: pending_ui.append((callback, args))
                    mmap_client = CountedMmap(draw.client)
                    mmap_client.release_count = 0
                    area = AdHocStruct()
                    mmap_client.mmap_read_area = area
                    draw.client.subsystems["mmap"] = mmap_client
                    with mmap.mmap(-1, 4096) as memory, \
                            patch.object(draw_module, "BACKWARDS_COMPATIBLE", compatible), \
                            patch.object(mmap_module, "BACKWARDS_COMPATIBLE", compatible):
                        area.mmap = memory
                        int_from_buffer(memory, 0).value = 8
                        chunks = ((16, 8),)
                        fields = list(self.packet(coding="mmap"))
                        fields[7] = chunks if legacy else b""
                        if not legacy:
                            fields[10]["chunks"] = chunks
                        draw._process_draw(Packet(*fields))
                        if transition == "replace":
                            replacement = AdHocStruct()
                            replacement._subsurface_local_backing_epoch = 1
                            window._backing = replacement
                        elif transition == "window-generation":
                            window._subsurface_backing_generation += 1
                        else:
                            backing._subsurface_local_backing_epoch += 1
                        fn, args = draw.decode_work.pop()
                        fn(*args)
                        self.assertEqual(calls, [])
                        self.assertEqual(sent, [])
                        self.assertEqual(len(pending_ui), 1)
                        self.assertEqual(int_from_buffer(memory, 0).value, 8)
                        fn, args = pending_ui.pop()
                        fn(*args)
                        self.assertEqual(mmap_client.release_count, 1)
                        self.assertEqual(int_from_buffer(memory, 0).value, 24)
                        self.assertEqual(len(sent), 1)
                        self.assertIn("stale subsurface draw target", sent[0][-1])
                        self.assertEqual(backing.rejects, [])
                        self.assertTrue(backing.composite_active)
                    mmap_client.mmap_read_area = None

    def test_dequeued_composite_rejects_reconfigured_same_backing(self):
        draw, _window, backing, calls, sent = self.make_draw()
        draw._process_draw(self.packet())
        backing._subsurface_local_backing_epoch += 1

        fn, args = draw.decode_work.pop()
        fn(*args)
        self.assertEqual(calls, [])
        self.assertIn("stale subsurface draw target", sent[-1][-1])

        # A packet delivered after that local transition is current even when
        # its server-owned wire epoch has not changed.
        draw._process_draw(self.packet(2))
        fn, args = draw.decode_work.pop()
        fn(*args)
        self.assertEqual(len(calls), 1)
        options = calls[0][-2]
        target, generation, local_epoch = options[SUBSURFACE_CLIENT_BACKING_STATE]
        self.assertIs(target, backing)
        self.assertEqual((generation, local_epoch), (1, 2))

    def test_unsupported_composite_coding_reaches_transaction_owner(self):
        draw, _window, _backing, calls, sent = self.make_draw()
        draw._process_draw(self.packet(coding="unsupported-picture"))
        fn, args = draw.decode_work.pop()
        fn(*args)

        # The generic allowed-encoding gate cannot safely reject a composite
        # stage: only the backing can discard its private transaction/FBO.
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][4], "unsupported-picture")
        self.assertEqual(sent, [])

    def test_reported_fault_invalidates_composite_before_ack(self):
        from xpra.client.subsystem.window import draw as draw_module

        draw, window, backing, _calls, sent = self.make_draw()

        def reject_draw(_x, _y, _width, _height, coding, _data,
                        _rowstride, _options, callbacks):
            self.assertEqual(coding, "void")
            backing.reject_subsurface_composite(1, callbacks, "injected failure")

        window.draw_region = reject_draw
        with patch.object(draw_module, "PAINT_FAULT_RATE", 1), \
                patch.object(draw_module, "PAINT_FAULT_TELL", True):
            draw._process_draw(self.packet())
            fn, args = draw.decode_work.pop()
            fn(*args)

        self.assertFalse(backing.composite_active)
        self.assertEqual(len(backing.rejects), 1)
        self.assertEqual(len(sent), 1)
        self.assertIn("injected failure", sent[0][-1])

    def test_synchronous_draw_exception_invalidates_composite_before_ack(self):
        draw, window, backing, _calls, sent = self.make_draw()
        window.draw_region = Mock(side_effect=RuntimeError("injected synchronous failure"))

        draw._process_draw(self.packet())
        fn, args = draw.decode_work.pop()
        fn(*args)

        self.assertFalse(backing.composite_active)
        self.assertEqual(backing.rejects, [(1, "injected synchronous failure")])
        self.assertEqual(len(sent), 1)
        self.assertIn("injected synchronous failure", sent[0][-1])

    def test_silent_fault_preserves_loss_simulation_without_ack(self):
        from xpra.client.subsystem.window import draw as draw_module

        draw, _window, backing, calls, sent = self.make_draw()
        with patch.object(draw_module, "PAINT_FAULT_RATE", 1), \
                patch.object(draw_module, "PAINT_FAULT_TELL", False):
            draw._process_draw(self.packet())
            fn, args = draw.decode_work.pop()
            fn(*args)

        self.assertTrue(backing.composite_active)
        self.assertEqual(backing.rejects, [])
        self.assertEqual(calls, [])
        self.assertEqual(sent, [])


class WindowSubsurfaceRefreshTest(unittest.TestCase):

    class Backing:
        draw_needs_refresh = True
        repaint_all = False

        def __init__(self):
            self.callback_groups = []
            self.invalidations = 0
            self._subsurface_local_backing_epoch = 0
            self._backing = object()

        def draw_region(self, _x, _y, _width, _height, _coding,
                        _img_data, _rowstride, _options, callbacks) -> None:
            self.callback_groups.append(tuple(callbacks))

        def complete(self, success: bool, index: int = 0) -> None:
            callbacks = self.callback_groups.pop(index)
            for callback in callbacks:
                callback(success, "injected result")

        def invalidate_subsurface_transaction(self) -> None:
            self.invalidations += 1

    @staticmethod
    def options(transaction_id: int, stage_index: int, stage_count: int) -> typedict:
        return typedict({
            "subsurface-composite": "premultiplied-source-over-v1",
            "subsurface-transaction-id": transaction_id,
            "subsurface-stage-index": stage_index,
            "subsurface-stage-count": stage_count,
            "subsurface-topology-epoch": 1,
            "subsurface-backing-epoch": 1,
            "flush": stage_count - stage_index - 1,
        })

    @staticmethod
    def make_window(backing):
        from xpra.client.gui.window_base import ClientWindowBase
        window = object.__new__(ClientWindowBase)
        window.wid = 1
        window._backing = backing
        window.pending_refresh = []
        window._subsurface_pending_refresh = None
        window._subsurface_refresh_floor = 0
        window._subsurface_backing_generation = 1
        window._xscale = window._yscale = 1
        window.window_offset = None
        window._size = (64, 32)
        window.get_size = lambda: window._size
        window.repainted = []
        window.repaint = lambda *rect: window.repainted.append(rect)
        window.idle_add = lambda fn, *args: fn(*args)
        window._client = AdHocStruct()
        window._client.get_subsystem = lambda _name: None
        return window

    @staticmethod
    def draw(window, rect, options) -> None:
        window.draw_region(*rect, "rgb32", bytes(rect[2] * rect[3] * 4), rect[2] * 4, options, [])

    def test_failed_final_stage_never_publishes_or_leaves_damage(self):
        backing = self.Backing()
        window = self.make_window(backing)
        self.draw(window, (0, 0, 8, 8), self.options(1, 0, 2))
        backing.complete(True)
        self.assertEqual(window.repainted, [])

        self.draw(window, (8, 8, 4, 4), self.options(1, 1, 2))
        backing.complete(False)
        self.assertEqual(window.repainted, [])
        self.assertEqual(window.pending_refresh, [])
        self.assertIsNone(window._subsurface_pending_refresh)

        # A later ordinary frame must not expose rectangles from the failed
        # transaction which preceded it.
        self.draw(window, (20, 10, 2, 3), typedict({"flush": 0}))
        backing.complete(True)
        self.assertEqual(window.repainted, [(20, 10, 2, 3)])

    def test_unknown_composite_mode_failure_never_enters_ordinary_damage(self):
        for invalid_mode in ("unknown-composite-mode", "", False):
            with self.subTest(invalid_mode=invalid_mode):
                backing = self.Backing()
                window = self.make_window(backing)
                options = self.options(1, 0, 1)
                options["subsurface-composite"] = invalid_mode

                self.draw(window, (3, 4, 5, 6), options)
                self.assertEqual(window.pending_refresh, [])
                backing.complete(False)
                self.assertEqual(window.repainted, [])
                self.assertEqual(window.pending_refresh, [])
                self.assertIsNone(window._subsurface_pending_refresh)

    def test_success_publishes_all_stages_only_after_atomic_commit(self):
        backing = self.Backing()
        window = self.make_window(backing)
        with patch("xpra.client.gui.window_base.FORCE_FLUSH", True):
            self.draw(window, (0, 0, 8, 8), self.options(1, 0, 2))
            backing.complete(True)
        self.assertEqual(window.repainted, [])
        self.draw(window, (8, 8, 4, 4), self.options(1, 1, 2))
        backing.complete(True)
        self.assertEqual(window.repainted, [(0, 0, 8, 8), (8, 8, 4, 4)])
        self.assertEqual(window.pending_refresh, [])

    def test_delayed_old_callback_cannot_discard_new_transaction(self):
        old_backing = self.Backing()
        window = self.make_window(old_backing)
        self.draw(window, (0, 0, 8, 8), self.options(1, 0, 2))
        self.draw(window, (1, 1, 8, 8), self.options(2, 0, 2))

        # Callback completion, not decode enqueue, owns refresh ordering.
        old_backing.complete(False)
        old_backing.complete(True)
        pending = window._subsurface_pending_refresh
        self.assertIsNotNone(pending)
        self.assertEqual(pending[0], 2)
        self.assertEqual(window.repainted, [])

        replacement = self.Backing()
        window._backing = replacement
        self.draw(window, (2, 2, 4, 4), self.options(2, 1, 2))
        replacement.complete(False)
        self.assertIsNone(window._subsurface_pending_refresh)
        self.assertEqual(window.repainted, [])

    def test_queued_newer_stage_does_not_hide_an_earlier_atomic_commit(self):
        backing = self.Backing()
        window = self.make_window(backing)
        self.draw(window, (0, 0, 8, 8), self.options(1, 0, 2))
        self.draw(window, (8, 8, 4, 4), self.options(1, 1, 2))
        self.draw(window, (1, 1, 2, 2), self.options(2, 0, 2))

        self.assertIsNone(window._subsurface_pending_refresh)
        backing.complete(True)
        backing.complete(True)
        self.assertEqual(window.repainted, [(0, 0, 8, 8), (8, 8, 4, 4)])
        backing.complete(True)
        self.assertEqual(window._subsurface_pending_refresh[:2], (2, backing))
        self.assertEqual(window._subsurface_pending_refresh[3:5], (1, 2))

    def test_transaction_refresh_does_not_consume_queued_ordinary_damage(self):
        backing = self.Backing()
        window = self.make_window(backing)
        self.draw(window, (0, 0, 8, 8), self.options(1, 0, 2))
        self.draw(window, (8, 8, 4, 4), self.options(1, 1, 2))
        self.draw(window, (20, 10, 2, 3), typedict({"flush": 0}))

        backing.complete(True)
        backing.complete(True)
        self.assertEqual(window.repainted, [(0, 0, 8, 8), (8, 8, 4, 4)])
        self.assertEqual(window.pending_refresh, [(20, 10, 2, 3)])
        backing.complete(True)
        self.assertEqual(
            window.repainted,
            [(0, 0, 8, 8), (8, 8, 4, 4), (20, 10, 2, 3)],
        )
        self.assertEqual(window.pending_refresh, [])

    def test_delayed_ordinary_callback_from_replaced_backing_preserves_new_transaction(self):
        old_backing = self.Backing()
        window = self.make_window(old_backing)
        self.draw(window, (20, 10, 2, 3), typedict({"flush": 0}))

        replacement = self.Backing()
        window._backing = replacement
        self.draw(window, (0, 0, 8, 8), self.options(2, 0, 2))
        replacement.complete(True)
        self.assertEqual(window._subsurface_pending_refresh[:2], (2, replacement))
        self.assertEqual(window._subsurface_pending_refresh[3:5], (1, 2))

        old_backing.complete(True)
        self.assertEqual(window._subsurface_pending_refresh[:2], (2, replacement))
        self.assertEqual(window._subsurface_pending_refresh[3:5], (1, 2))

    def test_delayed_success_after_newer_commit_cannot_resurrect_old_damage(self):
        backing = self.Backing()
        window = self.make_window(backing)
        self.draw(window, (0, 0, 8, 8), self.options(1, 0, 1))
        self.draw(window, (8, 8, 4, 4), self.options(2, 0, 1))

        # Complete the newer transaction first. Its commit becomes the refresh
        # floor, so the delayed success callback from transaction 1 is stale.
        backing.complete(True, 1)
        self.assertEqual(window.repainted, [(8, 8, 4, 4)])
        backing.complete(True)
        self.assertEqual(window.repainted, [(8, 8, 4, 4)])
        self.assertIsNone(window._subsurface_pending_refresh)

    def test_full_repaint_consumes_rectangle_queue(self):
        backing = self.Backing()
        window = self.make_window(backing)
        with patch("xpra.client.gui.window_base.is_Wayland", return_value=True):
            self.draw(window, (3, 4, 5, 6), typedict({"flush": 0}))
            backing.complete(True)
        self.assertEqual(window.repainted, [(0, 0, 64, 32)])
        self.assertEqual(window.pending_refresh, [])

    def test_new_backing_invalidates_staging_even_when_instance_is_reused(self):
        backing = self.Backing()
        window = self.make_window(backing)
        key = window._subsurface_refresh_backing_key(backing)
        window._subsurface_pending_refresh = (1, backing, key, 1, 2, [(0, 0, 8, 8)])
        window.pending_refresh = [(1, 1, 2, 2)]

        # Keep a callback from the old logical backing generation alive while
        # make_new_backing deliberately reuses the same Python object.
        self.draw(window, (9, 9, 2, 2), self.options(2, 0, 2))

        window.get_backing_class = lambda: type(backing)
        window.make_new_backing = lambda *_args: backing
        window.border = None
        window.content_types = ()
        window.window_gravity = 0
        window.new_backing(64, 32)

        self.assertEqual(backing.invalidations, 1)
        self.assertIs(window._backing, backing)
        self.assertIsNone(window._subsurface_pending_refresh)
        self.assertEqual(window.pending_refresh, [])

        self.draw(window, (3, 3, 4, 4), self.options(3, 0, 2))
        backing.complete(True, 1)
        self.assertEqual(window._subsurface_pending_refresh[:2], (3, backing))
        backing.complete(True)
        self.assertEqual(window._subsurface_pending_refresh[:2], (3, backing))

    def test_resize_generation_rejects_delayed_same_instance_callback(self):
        backing = self.Backing()
        window = self.make_window(backing)
        self.draw(window, (0, 0, 8, 8), self.options(1, 0, 2))

        backing._subsurface_local_backing_epoch += 1
        self.draw(window, (4, 4, 8, 8), self.options(2, 0, 2))
        backing.complete(True, 1)
        self.assertEqual(window._subsurface_pending_refresh[:2], (2, backing))
        backing.complete(True)
        self.assertEqual(window._subsurface_pending_refresh[:2], (2, backing))


def main():
    unittest.main()


if __name__ == '__main__':
    main()
