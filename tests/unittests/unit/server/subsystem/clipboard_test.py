#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2018 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import unittest
from threading import Thread, get_ident
from types import SimpleNamespace
from unittest.mock import Mock, patch

from xpra.util.objects import AdHocStruct
from xpra.os_util import POSIX, OSX
from unit.server.subsystem.servermixintest_util import ServerMixinTest
from unit.process_test_util import DisplayContext


class ClipboardOwnerTest(unittest.TestCase):

    @staticmethod
    def make_manager():
        from xpra.server.subsystem.clipboard import ClipboardManager

        def source(name):
            return SimpleNamespace(
                uuid=name, protocol=Mock(is_closed=lambda: False), clipboard_enabled=True, clipboard_record=False,
                clipboard_greedy=(), clipboard_want_targets=(), clipboard_selections=("CLIPBOARD",),
                clipboard_preferred_targets=(),
            )

        first, second = source("first"), source("second")
        sources = {first.protocol: first, second.protocol: second}
        server = SimpleNamespace(
            readonly=False, get_server_source=sources.get,
            get_sources_by_type=lambda *_args: (), setting_changed=Mock(),
        )
        manager = ClipboardManager(server)
        manager.enabled = True
        manager.direction = "both"
        manager.client = first
        manager.helper = Mock()
        scheduled = []
        manager.idle_add = lambda callback, *args: scheduled.append((callback, args))
        return manager, first, second, sources, scheduled

    def drain_callbacks(self, scheduled):
        while scheduled:
            callback, args = scheduled.pop(0)
            self.assertIsNone(callback(*args), "clipboard callbacks must be one-shot")

    def dispatch_from_thread(self, manager, protocol, packets):
        from xpra.net.dispatch import PacketDispatcher

        class Dispatcher(PacketDispatcher):
            def call_packet_handler(self, main, handler, proto, packet):
                # Match the server's scheduler boundary while retaining the
                # real subsystem lookup, aliases and authenticated dispatch.
                if main:
                    manager.idle_add(handler, proto, packet)
                else:
                    handler(proto, packet)

        dispatcher = Dispatcher()
        vars(dispatcher).update(vars(manager.server))
        dispatcher.subsystems[manager.PREFIX] = manager
        manager.server = dispatcher
        manager.init_packet_handlers()
        thread_ids = []
        errors = []

        def receive():
            thread_ids.append(get_ident())
            try:
                for packet in packets:
                    dispatcher.dispatch_packet(protocol, packet, authenticated=True)
            except Exception as error:
                errors.append(error)

        receiver = Thread(target=receive, name="clipboard-test-parser", daemon=True)
        receiver.start()
        receiver.join(5)
        self.assertFalse(receiver.is_alive(), "clipboard packet ingress did not finish")
        self.assertEqual(errors, [])
        self.assertEqual(len(thread_ids), 1)
        self.assertNotEqual(thread_ids[0], get_ident())

    def test_status_from_another_peer_cannot_change_owner_policy(self):
        from xpra.net.common import Packet

        manager, first, second, _sources, scheduled = self.make_manager()
        try:
            manager._process_packet(second.protocol, Packet("clipboard-status", False))
            self.drain_callbacks(scheduled)
            self.assertTrue(first.clipboard_enabled)
            first.clipboard_enabled = False
            manager._process_packet(second.protocol, Packet("clipboard-status", True))
            self.drain_callbacks(scheduled)
            self.assertFalse(first.clipboard_enabled)
            manager.helper.enable_selections.assert_not_called()
            manager._process_packet(first.protocol, Packet("clipboard-status", True))
            self.assertFalse(first.clipboard_enabled)
            self.drain_callbacks(scheduled)
            self.assertTrue(first.clipboard_enabled)
            manager._process_packet(first.protocol, Packet("clipboard-status", False))
            self.assertTrue(first.clipboard_enabled)
            self.drain_callbacks(scheduled)
            self.assertFalse(first.clipboard_enabled)
            manager.helper.enable_selections.assert_called_once_with()
        finally:
            manager.cleanup()

    def test_dispatched_policy_cycle_preserves_ui_thread_and_packet_order(self):
        from xpra.net.common import BACKWARDS_COMPATIBLE, Packet

        status_names = ["clipboard-status"]
        if BACKWARDS_COMPATIBLE:
            status_names.append("set-clipboard-enabled")
        for status_name in status_names:
            for initially_enabled in (True, False):
                with self.subTest(status=status_name, initially_enabled=initially_enabled):
                    manager, first, _second, _sources, scheduled = self.make_manager()
                    first.clipboard_enabled = initially_enabled
                    helper = manager.helper
                    calls = []
                    ui_thread = get_ident()

                    def record(name, *args):
                        calls.append((name, args, first.clipboard_enabled, get_ident()))

                    helper.enable_selections.side_effect = lambda *args: record("enable", *args)
                    helper.client_reset.side_effect = lambda: record("reset")
                    helper.process_clipboard_packet.side_effect = lambda packet: record("packet", packet)
                    enabled = Packet("clipboard-enable-selections", ("CLIPBOARD",))
                    fresh = Packet("clipboard-data", "CLIPBOARD", {"origin": "fresh-after-enable"})
                    packets = (
                        Packet(status_name, False),
                        Packet("clipboard-data", "CLIPBOARD", {"origin": "while-disabled"}),
                        Packet(status_name, True), enabled, fresh,
                    )
                    try:
                        self.dispatch_from_thread(manager, first.protocol, packets)
                        self.assertEqual(len(scheduled), len(packets))
                        self.assertEqual(first.clipboard_enabled, initially_enabled)
                        self.assertEqual(helper.mock_calls, [], "packet parser touched the native helper")
                        self.assertEqual(calls, [])
                        self.drain_callbacks(scheduled)
                        expected = [("enable", (), False, ui_thread)]
                        if initially_enabled:
                            expected.append(("reset", (), False, ui_thread))
                        expected.extend((
                            ("packet", (enabled,), True, ui_thread),
                            ("packet", (fresh,), True, ui_thread),
                        ))
                        self.assertEqual(calls, expected)
                        self.assertTrue(first.clipboard_enabled)
                    finally:
                        manager.cleanup()

    def test_dispatched_status_cannot_cross_peer_handoff(self):
        from xpra.net.common import Packet

        for return_to_first in (False, True):
            with self.subTest(return_to_first=return_to_first):
                manager, first, second, _sources, scheduled = self.make_manager()
                helper = manager.helper
                try:
                    self.dispatch_from_thread(manager, first.protocol, (Packet("clipboard-status", False),))
                    self.assertEqual(len(scheduled), 1)
                    self.assertTrue(first.clipboard_enabled)
                    helper.client_reset.assert_not_called()
                    manager.set_clipboard_source(second)
                    if return_to_first:
                        manager.set_clipboard_source(first)
                    helper.reset_mock()
                    self.drain_callbacks(scheduled)
                    self.assertTrue(first.clipboard_enabled)
                    self.assertTrue(second.clipboard_enabled)
                    self.assertEqual(helper.mock_calls, [], "stale status affected a replacement peer lifetime")
                finally:
                    manager.cleanup()

    def test_current_owner_packet_survives_repeated_same_owner_notification(self):
        from xpra.net.common import Packet

        manager, first, _second, _sources, scheduled = self.make_manager()
        packet = Packet("clipboard-data", "CLIPBOARD", {})
        helper = manager.helper
        try:
            manager._process_packet(first.protocol, packet)
            self.assertEqual(len(scheduled), 1)
            helper.process_clipboard_packet.assert_not_called()
            manager.set_clipboard_source(first)
            helper.client_reset.assert_not_called()
            callback, args = scheduled.pop()
            callback(*args)
            helper.process_clipboard_packet.assert_called_once_with(packet)
        finally:
            manager.cleanup()

    def test_deferred_packet_cannot_cross_owner_or_policy_lifetime(self):
        from xpra.net.common import Packet

        boundaries = (
            "reset", "disconnect", "replace", "return", "disable-enable",
            "direction-cycle", "helper", "protocol", "readonly", "peer-readonly",
        )
        for boundary in boundaries:
            with self.subTest(boundary=boundary):
                manager, first, second, sources, scheduled = self.make_manager()
                helper = manager.helper
                try:
                    manager._process_packet(first.protocol, Packet("clipboard-data", "CLIPBOARD", {}))
                    self.assertEqual(len(scheduled), 1)
                    if boundary == "reset":
                        manager.reset_clipboard()
                    elif boundary == "disconnect":
                        manager.cleanup_protocol(first.protocol)
                    elif boundary in ("replace", "return"):
                        manager.set_clipboard_source(second)
                        if boundary == "return":
                            manager.set_clipboard_source(first)
                    elif boundary == "disable-enable":
                        manager.set_clipboard_enabled_status(first, False)
                        manager.set_clipboard_enabled_status(first, True)
                    elif boundary == "direction-cycle":
                        manager.control_command_clipboard_direction("disabled")
                        manager.control_command_clipboard_direction("both")
                    elif boundary == "helper":
                        manager.helper = Mock()
                    elif boundary == "protocol":
                        sources.pop(first.protocol)
                    elif boundary == "peer-readonly":
                        first.effective_readonly = lambda: True
                    else:
                        manager.server.readonly = True
                    callback, args = scheduled.pop()
                    callback(*args)
                    helper.process_clipboard_packet.assert_not_called()
                finally:
                    manager.cleanup()

    def test_policy_changes_drain_requests_but_repeated_settings_preserve_them(self):
        manager, first, _second, _sources, _scheduled = self.make_manager()
        helper = manager.helper
        try:
            manager.set_clipboard_enabled_status(first, True)
            manager.control_command_clipboard_direction("both")
            helper.client_reset.assert_not_called()
            manager.set_clipboard_enabled_status(first, False)
            helper.client_reset.assert_called_once_with()
            manager.set_clipboard_enabled_status(first, False)
            manager.set_clipboard_enabled_status(first, True)
            helper.client_reset.assert_called_once_with()
            manager.control_command_clipboard_direction("to-client")
            helper.set_direction.assert_called_with(True, False)
            helper.client_reset.assert_called_once_with()
            manager.control_command_clipboard_direction("to-client")
            manager.control_command_clipboard_direction("both")
            helper.set_direction.assert_called_with(True, True)
            helper.client_reset.assert_called_once_with()
        finally:
            manager.cleanup()

    def test_peer_replacement_drains_before_new_peer_publication(self):
        manager, first, second, _sources, _scheduled = self.make_manager()
        helper = manager.helper
        owners_during_reset = []
        helper.client_reset.side_effect = lambda: owners_during_reset.append(manager.client)
        try:
            manager.set_clipboard_source(second)
            self.assertIs(manager.client, second)
            self.assertEqual(owners_during_reset, [None])
            helper.send_tokens.assert_called_once_with(second.clipboard_selections)
            manager.set_clipboard_source(second)
            self.assertEqual(owners_during_reset, [None])
            manager.set_clipboard_source(None)
            self.assertIsNone(manager.client)
            self.assertEqual(owners_during_reset, [None, None])
            self.assertTrue(first.clipboard_enabled)
            helper.enable_selections.assert_called_with()
        finally:
            manager.cleanup()

    def test_runtime_direction_uses_server_send_receive_orientation(self):
        from xpra.server.subsystem.clipboard import ClipboardManager

        manager, _first, _second, _sources, _scheduled = self.make_manager()
        directions = {
            "to-server": (False, True),
            "to-client": (True, False),
            "both": (True, True),
            "disabled": (False, False),
        }
        try:
            for direction, expected in directions.items():
                with self.subTest(direction=direction):
                    factory = Mock()
                    manager.direction = direction
                    with patch.object(ClipboardManager, "get_clipboard_class", return_value=factory):
                        manager.init_clipboard()
                    startup = factory.call_args.kwargs
                    self.assertEqual((startup["can-send"], startup["can-receive"]), expected)
                    manager.control_command_clipboard_direction(direction)
                    manager.helper.set_direction.assert_called_with(*expected)
                    manager.server.setting_changed.assert_called_with("clipboard-direction", direction)
        finally:
            manager.cleanup()


class ClipboardMixinTest(ServerMixinTest):

    # ClipboardManager subscribes to these ServerBase lifecycle signals.
    __signals__ = ("last-client-exited", "new-ui-driver")

    def test_clipboard(self):
        with DisplayContext():
            if POSIX and not OSX:
                from xpra.x11.gtk.display_source import init_gdk_display_source
                init_gdk_display_source()
            from xpra.server.subsystem.clipboard import ClipboardManager
            from xpra.server.source.clipboard import ClipboardConnection
            opts = AdHocStruct()
            opts.clipboard = "yes"
            opts.clipboard_direction = "both"
            opts.clipboard_filter_file = None
            self._test_mixin_class(ClipboardManager, opts, {}, ClipboardConnection)
            # Clipboard helpers own GTK objects tied to this display, so they
            # must be released before DisplayContext closes the connection.
            self.cleanup_test_objects()


def main():
    unittest.main()


if __name__ == '__main__':
    main()
