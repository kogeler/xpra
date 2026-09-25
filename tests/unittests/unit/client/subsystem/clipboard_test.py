#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2019 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import unittest
from collections.abc import Callable
from time import monotonic, sleep
from unittest.mock import patch

from unit.client.subsystem.clientmixintest_util import ClientMixinTest
from unit.process_test_util import DisplayContext
from xpra.util.objects import AdHocStruct


def spin_until(predicate: Callable[[], bool], timeout: float = 2) -> bool:
    from xpra.os_util import gi_import
    context = gi_import("GLib").MainContext.default()
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        if predicate():
            return True
        sleep(0.01)
    while context.pending():
        context.iteration(False)
    return predicate()


class X11ClipboardEventTest(unittest.TestCase):

    @staticmethod
    def make_helper(packets: list[tuple]):
        from xpra.x11.bindings.fixes import XFixesBindings

        from xpra.x11.error import xsync
        from xpra.x11.selection.clipboard import X11Clipboard
        with xsync:
            if not XFixesBindings().hasXFixes():
                raise RuntimeError("XFixes is required for the clipboard event regression")
        helper = X11Clipboard(
            lambda *packet: packets.append(packet),
            **{
                "can-send": True,
                "can-receive": False,
                "clipboards.local": ("CLIPBOARD",),
                "clipboards.remote": ("CLIPBOARD",),
            },
        )
        helper.enable_selections(("CLIPBOARD",))
        helper.set_want_targets_client(("CLIPBOARD",))
        return helper

    def assert_gdk_window(self, helper) -> None:
        from xpra.os_util import gi_import
        Gdk = gi_import("Gdk")
        GdkX11 = gi_import("GdkX11")
        window = GdkX11.X11Window.lookup_for_display(
            Gdk.Display.get_default(), helper.event_window_xid,
        )
        self.assertIsNotNone(window)
        self.assertEqual(window.get_xid(), helper.event_window_xid)
        self.assertEqual(helper.gtk_event_window.get_xid(), helper.event_window_xid)

    @staticmethod
    def set_text(marker: str):
        from xpra.os_util import gi_import
        Gdk = gi_import("Gdk")
        Gtk = gi_import("Gtk")
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clipboard.set_text(marker, -1)
        Gdk.flush()
        return clipboard

    @staticmethod
    def clipboard_packets(packets: list[tuple]) -> list[tuple]:
        return [
            packet for packet in packets
            if packet[:2] in (
                ("clipboard-token", "CLIPBOARD"),
                ("clipboard-data", "CLIPBOARD"),
            )
        ]

    def assert_marker_conversion(self, helper, marker: str) -> None:
        result = []
        proxy = helper._get_proxy("CLIPBOARD")
        proxy.get_contents(
            "UTF8_STRING",
            lambda dtype, dformat, data: result.append((dtype, dformat, data)),
        )
        self.assertTrue(spin_until(lambda: bool(result)), "clipboard text conversion timed out")
        dtype, dformat, data = result[0]
        self.assertEqual(dtype, "UTF8_STRING")
        self.assertEqual(dformat, 8)
        self.assertEqual(data.decode("utf-8"), marker)

    def test_x11_clipboard_owner_change(self):
        marker = "xpra-x11-clipboard-test-one"
        packets: list[tuple] = []
        helper = self.make_helper(packets)
        clipboard = None
        try:
            self.assert_gdk_window(helper)
            clipboard = self.set_text(marker)
            self.assertTrue(
                spin_until(lambda: bool(self.clipboard_packets(packets))),
                "XFixes owner change did not produce a clipboard packet",
            )
            proxy = helper._get_proxy("CLIPBOARD")
            self.assertIn("UTF8_STRING", proxy.targets)
            self.assert_marker_conversion(helper, marker)
            spin_until(lambda: False, 0.2)
            self.assertEqual(len(self.clipboard_packets(packets)), 1)
        finally:
            helper.cleanup()
            if clipboard:
                clipboard.clear()

    def test_event_window_property_failure_rolls_back(self):
        from xpra.x11.bindings.window import X11WindowBindings
        from xpra.x11.error import xsync
        from xpra.x11.selection import clipboard as clipboard_module

        X11Window = X11WindowBindings()
        root_xid = X11Window.get_root_xid()
        created = []

        def fail_property(xid, *_args):
            created.append(xid)
            raise RuntimeError("injected clipboard window property failure")

        with (
            patch.object(clipboard_module, "prop_set", side_effect=fail_property),
            self.assertRaisesRegex(RuntimeError, "injected clipboard window"),
        ):
            clipboard_module.init_event_window(True)
        self.assertEqual(len(created), 1)
        with xsync:
            self.assertNotIn(created[0], X11Window.get_children(root_xid))

    def test_partial_proxy_initialization_rolls_back(self):
        from xpra.x11.dispatch import event_receivers_map
        from xpra.x11.gtk import bindings
        from xpra.x11.selection.clipboard import X11Clipboard

        packets: list[tuple] = []
        created = []
        failed_xids = []
        helper_holder = []
        original_make_proxy = X11Clipboard.make_proxy

        def fail_second_proxy(helper, selection):
            helper_holder.append(helper)
            if selection == "PRIMARY":
                failed_xids.append(helper.event_window_xid)
                raise RuntimeError("injected second proxy failure")
            proxy = original_make_proxy(helper, selection)
            proxy.schedule_emit_token(1000)
            created.append(proxy)
            return proxy

        with (
            patch.object(bindings, "init_x11_filter", wraps=bindings.init_x11_filter) as acquire,
            patch.object(bindings, "cleanup_x11_filter", wraps=bindings.cleanup_x11_filter) as release,
            patch.object(X11Clipboard, "make_proxy", new=fail_second_proxy),
            self.assertRaisesRegex(RuntimeError, "injected second proxy"),
        ):
            X11Clipboard(
                lambda *packet: packets.append(packet),
                **{
                    "can-send": True,
                    "can-receive": False,
                    "clipboards.local": ("CLIPBOARD", "PRIMARY"),
                    "clipboards.remote": ("CLIPBOARD", "PRIMARY"),
                },
            )

        self.assertEqual(len(created), 1)
        self.assertTrue(helper_holder)
        helper = helper_holder[0]
        proxy = created[0]
        self.assertFalse(proxy.is_enabled())
        self.assertEqual(proxy._emit_token_timer, 0)
        self.assertEqual(helper._clipboard_proxies, {})
        self.assertEqual(helper._proxy_signal_ids, {})
        self.assertEqual(helper.event_window_xid, 0)
        self.assertEqual(len(failed_xids), 1)
        self.assertNotIn(failed_xids[0], event_receivers_map)
        proxy.emit("send-clipboard-token", {})
        self.assertEqual(packets, [])
        acquire.assert_called_once_with()
        release.assert_called_once_with()

    def test_proxy_cleanup_drains_requests_and_incremental_timer(self):
        from xpra.os_util import gi_import
        from xpra.x11.selection.proxy import ClipboardProxy, IncrTransfer

        GLib = gi_import("GLib")
        context = GLib.MainContext.default()
        fired = []
        local_results = []

        def record_timer(name):
            fired.append(name)
            return GLib.SOURCE_REMOVE

        local_timer = GLib.timeout_add(60_000, record_timer, "local")
        proxy = ClipboardProxy(0x100, "CLIPBOARD")
        proxy.local_requests = {
            "UTF8_STRING": {
                1: (local_timer, lambda *result: local_results.append(result), 11),
            },
        }
        proxy.remote_requests = {
            "UTF8_STRING": [(0x200, "UTF8_STRING", "XPRA_TEST", 11)],
        }
        atom = "CLIPBOARD-UTF8_STRING"
        incr = proxy.incr_transfers[atom] = IncrTransfer(4)
        incr.dtype = "UTF8_STRING"
        incr.chunks.append(b"test")
        proxy.reschedule_incr_timer(atom)
        incr_timer = incr.timer

        try:
            with patch.object(proxy, "set_selection_response") as response:
                proxy.cleanup()
                proxy.cleanup()

            self.assertEqual(local_results, [])
            response.assert_called_once_with(
                0x200, "UTF8_STRING", "XPRA_TEST", "", 0, None, 11,
            )
            self.assertEqual(proxy.local_requests, {})
            self.assertEqual(proxy.remote_requests, {})
            self.assertEqual(proxy.incr_transfers, {})
            self.assertEqual(incr.timer, 0)
            self.assertIsNone(context.find_source_by_id(local_timer))
            self.assertIsNone(context.find_source_by_id(incr_timer))
            self.assertEqual(fired, [])
        finally:
            for timer in (local_timer, incr_timer):
                if context.find_source_by_id(timer):
                    GLib.source_remove(timer)

    def test_nonclaiming_and_receive_denied_tokens_preserve_local_state(self):
        from xpra.x11.selection.proxy import ClipboardProxy

        for claim, receive in ((False, True), (True, False)):
            with self.subTest(claim=claim, receive=receive):
                proxy = ClipboardProxy(0x100, "CLIPBOARD")
                proxy.set_enabled(True)
                proxy.set_direction(True, receive)
                proxy.targets = ("UTF8_STRING",)
                proxy.target_data = {"UTF8_STRING": ("UTF8_STRING", 8, b"local")}
                before = (proxy._selection_generation, proxy.targets, proxy.target_data, proxy._have_token)
                with patch.object(proxy, "claim") as take, patch.object(proxy, "cancel_emit_token") as cancel:
                    proxy.got_token(("text/plain",), {"text/plain": ("text/plain", 8, b"remote")}, claim=claim)
                take.assert_not_called()
                cancel.assert_not_called()
                self.assertEqual((proxy._selection_generation, proxy.targets, proxy.target_data, proxy._have_token), before)
                proxy.cleanup()

    def test_data_only_claim_advertises_its_supplied_target(self):
        from xpra.x11.selection.proxy import ClipboardProxy

        proxy = ClipboardProxy(0x100, "CLIPBOARD")
        proxy.set_enabled(True)
        proxy.set_direction(True, True)
        try:
            with patch.object(proxy, "claim") as take, patch.object(proxy, "got_contents"):
                proxy.got_token(None, {"UTF8_STRING": ("UTF8_STRING", 8, b"remote")})
            take.assert_called_once_with()
            self.assertEqual(proxy.targets, ("UTF8_STRING",))
        finally:
            proxy.cleanup()

    def test_proxy_cleanup_attempts_every_request_and_timer_after_failure(self):
        from xpra.os_util import gi_import
        from xpra.x11.selection.proxy import ClipboardProxy

        GLib = gi_import("GLib")
        context = GLib.MainContext.default()
        timers = [GLib.timeout_add(60_000, lambda: False) for _ in range(2)]
        retained = [context.find_source_by_id(timer) for timer in timers]
        proxy = ClipboardProxy(0x100, "CLIPBOARD")
        proxy.local_requests = {"UTF8_STRING": {i: (timer, self.fail, 11) for i, timer in enumerate(timers)}}
        proxy.remote_requests = {"UTF8_STRING": [(xid, "UTF8_STRING", "XPRA_TEST", 11) for xid in (0x200, 0x201)]}
        try:
            with (
                patch.object(proxy, "set_selection_response", side_effect=[RuntimeError("requestor gone"), None]) as respond,
                patch.object(GLib, "source_remove", side_effect=[RuntimeError("remove failed"), True]) as remove,
            ):
                proxy.cleanup()
                proxy.cleanup()
            self.assertEqual(respond.call_count, 2)
            self.assertEqual([call.args[0] for call in remove.call_args_list], timers)
            self.assertEqual(proxy.local_requests, {})
            self.assertEqual(proxy.remote_requests, {})
            self.assertFalse(proxy.is_enabled())
        finally:
            # The injected remover did not remove either real source; retain
            # their objects so the test does not clean up a reused numeric ID.
            for source in retained:
                source.destroy()

    def test_owner_change_schedules_one_token_per_state(self):
        from xpra.x11.selection.proxy import ClipboardProxy

        states = {
            "plain": ({}, 1),
            "have-token": ({"_have_token": True}, 1),
            "want-targets": ({"_want_targets": True}, 1),
            "greedy": ({"_greedy_client": True}, 1),
            "blocked": ({"_block_owner_change": True}, 0),
        }
        for name, (attributes, expected) in states.items():
            with self.subTest(name=name):
                proxy = ClipboardProxy(0x100, "CLIPBOARD")
                proxy.set_enabled(True)
                proxy.set_direction(True, False)
                for attribute, value in attributes.items():
                    setattr(proxy, attribute, value)
                scheduled = []

                def schedule(_proxy, min_delay=0):
                    scheduled.append(min_delay)

                event = AdHocStruct()
                event.owner = 0x200
                with patch.object(ClipboardProxy, "schedule_emit_token", new=schedule):
                    proxy.do_selection_notify_event(event)
                self.assertEqual(len(scheduled), expected)

    def test_x11_clipboard_helper_first_cleanup_drains_queued_event(self):
        from xpra.os_util import gi_import
        from xpra.x11.bindings.core import X11CoreBindings

        Gdk = gi_import("Gdk")
        first_packets: list[tuple] = []
        second_packets: list[tuple] = []
        first = self.make_helper(first_packets)
        second = self.make_helper(second_packets)
        first_xid = first.event_window_xid
        clipboard = None
        try:
            self.assert_gdk_window(first)
            self.assert_gdk_window(second)
            clipboard = gi_import("Gtk").Clipboard.get(Gdk.SELECTION_CLIPBOARD)
            clipboard.clear()
            Gdk.flush()
            X11CoreBindings().XSync(False)
            spin_until(lambda: False, 0.1)
            first_packets.clear()
            second_packets.clear()

            marker = "xpra-x11-clipboard-test-helper-first"
            clipboard.set_text(marker, -1)
            Gdk.flush()
            # The notification is now queued on both helper windows, but the
            # GDK event source has not translated either one yet.
            X11CoreBindings().XSync(False)
            display = Gdk.Display.get_default()
            self.assertTrue(display.has_pending())
            first.cleanup()
            GdkX11 = gi_import("GdkX11")
            self.assertIsNotNone(
                GdkX11.X11Window.lookup_for_display(display, first_xid)
            )
            self.assertTrue(
                spin_until(
                    lambda: bool(self.clipboard_packets(second_packets))
                    and GdkX11.X11Window.lookup_for_display(
                        display, first_xid,
                    ) is None
                ),
                "helper-first cleanup broke the surviving clipboard route",
            )
            self.assert_marker_conversion(second, marker)
            spin_until(lambda: False, 0.2)
            self.assertEqual(len(self.clipboard_packets(second_packets)), 1)
        finally:
            first.cleanup()
            second.cleanup()
            if clipboard:
                clipboard.clear()

    def test_x11_clipboard_shared_filter_lease(self):
        from xpra.x11.gtk.bindings import cleanup_x11_filter, init_x11_filter
        packets: list[tuple] = []
        init_x11_filter()
        peer_lease = True
        helper = self.make_helper(packets)
        clipboard = None
        try:
            self.assert_gdk_window(helper)
            clipboard = self.set_text("xpra-x11-clipboard-test-peer-one")
            self.assertTrue(spin_until(lambda: bool(self.clipboard_packets(packets))))
            spin_until(lambda: False, 0.2)
            self.assertEqual(len(self.clipboard_packets(packets)), 1)
            # False means the shared filter remains installed for the helper;
            # the peer's reference has nevertheless been released.
            self.assertFalse(cleanup_x11_filter())
            peer_lease = False

            packets.clear()
            marker = "xpra-x11-clipboard-test-peer-two"
            clipboard = self.set_text(marker)
            self.assertTrue(
                spin_until(lambda: bool(self.clipboard_packets(packets))),
                "peer cleanup removed the clipboard helper's filter lease",
            )
            self.assert_marker_conversion(helper, marker)
            spin_until(lambda: False, 0.2)
            self.assertEqual(len(self.clipboard_packets(packets)), 1)
        finally:
            helper.cleanup()
            if peer_lease:
                cleanup_x11_filter()
            if clipboard:
                clipboard.clear()


class ClipboardClientTest(ClientMixinTest):

    def test_clipboard_types(self):
        from xpra.client.subsystem.clipboard import get_clipboard_helper_classes
        default = get_clipboard_helper_classes("auto")
        self.assertTrue(default)
        # `all` only changes the selections used, not the backend:
        self.assertEqual(get_clipboard_helper_classes("all"), default)
        self.assertEqual(get_clipboard_helper_classes("yes"), default)
        self.assertEqual(get_clipboard_helper_classes("no"), [])
        self.assertEqual(get_clipboard_helper_classes("nosuchbackend"), [])

    def test_clipboard(self):
        from xpra.client.subsystem.clipboard import ClipboardClient
        opts = AdHocStruct()
        opts.clipboard = "yes"
        opts.clipboard_direction = "both"
        opts.local_clipboard = "CLIPBOARD"
        opts.remote_clipboard = "CLIPBOARD"

        def after_handshake(fn: Callable, *args):
            self.glib.timeout_add(1000, fn, *args)
        # `ClipboardClient.parse_server_capabilities` calls `self.client.after_handshake(...)`,
        # so this must be set on the owning-client stand-in (`self`), not on the subsystem class:
        self.after_handshake = after_handshake
        self._test_mixin_class(ClipboardClient, opts, {
            "clipboard": {
                "enable-selections": True,
            },
        })
        self.glib.timeout_add(5000, self.stop)
        self.main_loop.run()
        assert len(self.packets)>=1
        assert self.packets[0][0]=="clipboard-enable-selections"


def main():
    with DisplayContext():
        unittest.main()


if __name__ == '__main__':
    main()
