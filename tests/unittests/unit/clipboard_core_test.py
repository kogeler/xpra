#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import unittest
from collections import deque
from time import monotonic, sleep
from unittest.mock import Mock, patch

from xpra.os_util import gi_import

from xpra.net.common import Packet
from xpra.clipboard import core
from xpra.clipboard.common import ALL_CLIPBOARDS, parse_greedy, parse_want_targets
from xpra.clipboard.core import ClipboardProtocolHelperCore
from xpra.clipboard.proxy import ClipboardProxyCore
from xpra.clipboard.timeout import ClipboardTimeoutHelper
from xpra.util.objects import typedict

GLib = gi_import("GLib")


def spin_until(predicate, timeout=0.2) -> bool:
    """ runs the main loop, so that a timer which is still armed gets to fire """
    context = GLib.MainContext.default()
    deadline = monotonic() + timeout
    while monotonic() < deadline and not predicate():
        while context.pending():
            context.iteration(False)
        sleep(0.005)
    return predicate()


class ClipboardProxy(ClipboardProxyCore):
    """ the real proxy, with the tokens counted instead of sent """

    def __init__(self, selection: str):
        super().__init__(selection)
        self._enabled = True
        self._can_send = True
        self._can_receive = True
        self.tokens = []
        self.emitted = 0

    def do_emit_token(self) -> bool:
        self.emitted += 1
        return True

    def got_token(self, targets, target_data, claim, synchronous_client) -> None:
        self.tokens.append((targets, target_data, claim, synchronous_client))


class ClipboardHelper(ClipboardProtocolHelperCore):
    def make_proxy(self, selection: str):
        return ClipboardProxy(selection)


class TimeoutProxy(ClipboardProxyCore):
    def __init__(self, selection: str):
        super().__init__(selection)
        self.results = []

    def got_contents(self, target: str, dtype="", dformat=0, data=None) -> None:
        self.results.append((target, dtype, dformat, data))


class TimeoutHelper(ClipboardTimeoutHelper):
    def make_proxy(self, selection: str):
        return TimeoutProxy(selection)


class ClipboardCoreTest(unittest.TestCase):
    def make_helper(self):
        packets = []
        helper = ClipboardHelper.__new__(ClipboardHelper)
        proxy = ClipboardProxy("CLIPBOARD")
        helper.send = lambda *packet: packets.append(packet)
        helper._local_to_remote = {}
        helper._remote_to_local = {}
        helper._greedy = ()
        helper._clipboard_proxies = {"CLIPBOARD": proxy}
        helper._clipboard_origins = {}
        helper._peer_requests = {}
        helper.local_selections = ("CLIPBOARD",)
        helper.local_greedy = ()
        helper.filter_res = ()
        helper.max_clipboard_packet_size = core.MAX_CLIPBOARD_PACKET_SIZE
        helper.max_clipboard_receive_size = -1
        helper.max_clipboard_send_size = -1
        helper.init_packet_handlers()
        return helper, proxy, packets

    def test_clipboard_reply_size_limit(self):
        limit = 16
        for size in (0, limit - 1, limit, limit + 1):
            with self.subTest(size=size):
                helper, proxy, packets = self.make_helper()
                helper.max_clipboard_packet_size = limit
                helper._remote_to_local["PRIMARY"] = "CLIPBOARD"
                payload = b"x" * size

                def get_contents(_target, callback):
                    callback("STRING", 8, payload)

                with patch.object(proxy, "get_contents", side_effect=get_contents):
                    helper.process_clipboard_packet(Packet("clipboard-request", 42, "PRIMARY", "UTF8_STRING"))
                if size > limit:
                    self.assertEqual(packets, [("clipboard-contents-none", 42, "PRIMARY")])
                else:
                    self.assertEqual(packets, [("clipboard-contents", 42, "PRIMARY", "STRING", 8, "bytes", payload, 0)])

    def test_modern_clipboard_data_packet(self):
        helper, proxy, packets = self.make_helper()
        helper.local_greedy = ("CLIPBOARD",)
        payload = b"hello" * 200
        with patch.object(core, "get_hex_uuid", return_value="origin-1"):
            helper._send_clipboard_token_handler(
                proxy,
                {
                    "targets": ("UTF8_STRING",),
                    "data": {"UTF8_STRING": ("STRING", 8, payload)},
                },
            )
        self.assertEqual(len(packets), 1)
        packet_type, selection, options = packets[0]
        self.assertEqual(packet_type, "clipboard-data")
        self.assertEqual(selection, "CLIPBOARD")
        self.assertEqual(options["targets"], ("UTF8_STRING",))
        self.assertEqual(options["data"], {
            "UTF8_STRING": ("STRING", 8, "bytes", payload),
        })
        self.assertEqual(options["claim"], True)
        self.assertEqual(options["greedy"], True)
        self.assertEqual(options["origin"], "origin-1")
        self.assertNotIn("token", options)
        self.assertNotIn("clipboard-token", helper.get_packet_types())

    def test_modern_packet_defaults(self):
        helper, proxy, _packets = self.make_helper()
        helper.process_clipboard_packet(Packet("clipboard-data", "CLIPBOARD", {}))
        self.assertEqual(proxy.tokens, [(None, None, True, False)])

    def test_modern_packet_preserves_origin(self):
        helper, proxy, packets = self.make_helper()
        helper.process_clipboard_packet(Packet("clipboard-data", "CLIPBOARD", {"origin": "remote-origin"}))
        self.assertEqual(proxy._clipboard_origin, "remote-origin")
        helper._send_clipboard_token_handler(proxy, {"targets": (), "data": {}})
        self.assertEqual(packets[0][2]["origin"], "remote-origin")

    def test_modern_packet_drops_returned_origin(self):
        helper, proxy, _packets = self.make_helper()
        helper._clipboard_origins["CLIPBOARD"] = deque(("local-origin",), maxlen=16)
        helper.process_clipboard_packet(Packet("clipboard-data", "CLIPBOARD", {"origin": "local-origin"}))
        self.assertEqual(proxy.tokens, [])

    def test_modern_packet_origin_stops_a_clipboard_cycle(self):
        sender, sender_proxy, sender_packets = self.make_helper()
        receiver, receiver_proxy, receiver_packets = self.make_helper()
        with patch.object(core, "get_hex_uuid", return_value="cycle-origin"):
            sender._send_clipboard_token_handler(sender_proxy, {"targets": (), "data": {}})
        receiver.process_clipboard_packet(Packet(*sender_packets[0]))
        receiver._send_clipboard_token_handler(receiver_proxy, {"targets": (), "data": {}})
        sender.process_clipboard_packet(Packet(*receiver_packets[0]))
        self.assertEqual(sender_proxy.tokens, [])

    def test_clipboard_origins_are_bounded(self):
        helper, _proxy, _packets = self.make_helper()
        for i in range(17):
            helper.remember_clipboard_origin("CLIPBOARD", str(i))
        self.assertEqual(tuple(helper._clipboard_origins["CLIPBOARD"]), tuple(str(i) for i in range(1, 17)))

    def test_client_reset_clears_clipboard_origins(self):
        helper, proxy, _packets = self.make_helper()
        helper.remember_clipboard_origin("CLIPBOARD", "origin")
        proxy._clipboard_origin = "origin"
        helper.client_reset()
        self.assertEqual(helper._clipboard_origins, {})
        self.assertEqual(proxy._clipboard_origin, "")

    def test_client_reset_drops_the_token_scheduled_for_the_old_peer(self):
        helper, proxy, _packets = self.make_helper()
        self.addCleanup(proxy.cleanup)
        proxy.schedule_emit_token()
        proxy.schedule_emit_token()
        self.assertEqual(proxy.emitted, 1)
        self.assertNotEqual(proxy._emit_token_timer, 0)
        helper.client_reset()
        self.assertEqual(proxy._emit_token_timer, 0)
        # the timer was removed, not merely forgotten:
        self.assertFalse(spin_until(lambda: proxy.emitted > 1))

    def test_send_tokens_goes_through_the_scheduler(self):
        helper, proxy, _packets = self.make_helper()
        self.addCleanup(proxy.cleanup)
        proxy.schedule_emit_token()
        proxy.schedule_emit_token()
        self.assertNotEqual(proxy._emit_token_timer, 0)
        helper.send_tokens(("CLIPBOARD", ))
        # the token which was scheduled is superseded, not left to fire as well:
        self.assertEqual(proxy._emit_token_timer, 0)
        self.assertFalse(spin_until(lambda: proxy.emitted > 2))
        # and this one is counted and timed, so that the next change is spaced out from it:
        self.assertEqual(proxy.emitted, 2)
        self.assertEqual(proxy._sent_token_events, 2)
        self.assertNotEqual(proxy._last_emit_token, 0)
        self.assertFalse(proxy._have_token)

    def test_timeout_helper_routes_exact_request_callbacks(self):
        packets = []
        helper = TimeoutHelper(
            lambda *packet: packets.append(packet),
            **{
                "clipboards.local": ("CLIPBOARD",),
                "clipboards.remote": ("CLIPBOARD",),
            },
        )
        proxy = helper._get_proxy("CLIPBOARD")
        results1 = []
        results2 = []
        try:
            helper._send_clipboard_request_handler(
                proxy, "CLIPBOARD", "UTF8_STRING",
                lambda dtype, dformat, data: results1.append((dtype, dformat, data)),
            )
            helper._send_clipboard_request_handler(
                proxy, "CLIPBOARD", "UTF8_STRING",
                lambda dtype, dformat, data: results2.append((dtype, dformat, data)),
            )
            request1, request2 = packets
            helper._clipboard_got_contents(request2[1], "UTF8_STRING", 8, b"two")
            helper._clipboard_got_contents(request1[1], "UTF8_STRING", 8, b"one")
            self.assertEqual(results1, [("UTF8_STRING", 8, b"one")])
            self.assertEqual(results2, [("UTF8_STRING", 8, b"two")])
            self.assertEqual(proxy.results, [])
        finally:
            helper.cleanup()

    def test_nonclaiming_data_does_not_replace_local_origin(self):
        for claim, receive, token in ((False, True, True), (True, False, True), (True, True, False)):
            with self.subTest(claim=claim, receive=receive, token=token):
                helper, proxy, _packets = self.make_helper()
                proxy._can_receive = receive
                proxy._clipboard_origin = "local-origin"
                helper._process_data(Packet("clipboard-data", "CLIPBOARD", {
                    "origin": "remote-origin", "claim": claim, "token": token,
                }))
                self.assertEqual(proxy._clipboard_origin, "local-origin")

    def test_failed_claim_keeps_previous_origin(self):
        helper, proxy, _packets = self.make_helper()
        proxy._clipboard_origin = "local-origin"
        with patch.object(proxy, "got_token", side_effect=RuntimeError("claim refused")):
            with self.assertRaisesRegex(RuntimeError, "claim refused"):
                helper._process_data(Packet("clipboard-data", "CLIPBOARD", {"origin": "new-origin"}))
        self.assertEqual(proxy._clipboard_origin, "local-origin")

    def test_helper_cleanup_retires_all_proxies_before_reporting_failure(self):
        helper = TimeoutHelper(lambda *_args: None, **{"clipboards.local": ("CLIPBOARD", "PRIMARY")})
        first, second = tuple(helper._clipboard_proxies.values())
        first.cleanup = Mock(side_effect=RuntimeError("first proxy failed"))
        second.cleanup = Mock()
        with self.assertRaisesRegex(RuntimeError, "first proxy failed"):
            helper.cleanup()
        first.cleanup.assert_called_once_with()
        second.cleanup.assert_called_once_with()
        self.assertFalse(helper._clipboard_proxies)
        late = []
        with patch.object(gi_import("GLib"), "timeout_add", side_effect=AssertionError("late request timer")):
            helper._send_clipboard_request_handler(
                first, "CLIPBOARD", "late", lambda *result: late.append(result),
            )
        self.assertEqual(late, [("", 0, None)])
        helper.cleanup()

    def test_request_admission_failure_completes_the_native_consumer(self):
        for failure in ("timer", "send"):
            with self.subTest(failure=failure):
                helper = TimeoutHelper(lambda *_args: None, **{"clipboards.local": ("CLIPBOARD",)})
                proxy = helper._get_proxy("CLIPBOARD")
                results = []
                target = gi_import("GLib") if failure == "timer" else helper
                method = "timeout_add" if failure == "timer" else "send"
                try:
                    with patch.object(target, method, side_effect=RuntimeError("admission failed")):
                        with self.assertRaisesRegex(RuntimeError, "admission failed"):
                            helper._send_clipboard_request_handler(
                                proxy, "CLIPBOARD", "test", lambda *result: results.append(result),
                            )
                    self.assertEqual(results, [("", 0, None)])
                    self.assertFalse(helper._clipboard_outstanding_requests)
                finally:
                    helper.cleanup()

    def test_progress_and_timer_removal_failure_do_not_orphan_results(self):
        helper = TimeoutHelper(lambda *_args: None, **{"clipboards.local": ("CLIPBOARD",)})
        proxy = helper._get_proxy("CLIPBOARD")
        results = []
        try:
            helper._send_clipboard_request_handler(
                proxy, "CLIPBOARD", "test", lambda *result: results.append(result),
            )
            request_id = next(iter(helper._clipboard_outstanding_requests))
            timer = helper._clipboard_outstanding_requests[request_id][0]
            # Retain the real source and destroy it directly after injection.
            source = gi_import("GLib").MainContext.default().find_source_by_id(timer)
            try:
                with patch.object(helper, "progress_cb", side_effect=RuntimeError("progress failed")):
                    with patch.object(gi_import("GLib"), "source_remove", side_effect=RuntimeError("remove failed")):
                        helper._clipboard_got_contents(request_id, "test", 8, b"result")
                self.assertEqual(results, [("test", 8, b"result")])
                self.assertFalse(helper._clipboard_outstanding_requests)
            finally:
                source.destroy()
        finally:
            helper.cleanup()

    def test_timeout_helper_routes_buffer_views_without_boolean_coercion(self):
        import ctypes

        for callback_delivery in (False, True):
            for data, expected in (
                (memoryview(ctypes.c_ubyte(0)), b"\0"),
                (memoryview(b""), b""),
                (None, None),
            ):
                with self.subTest(callback=callback_delivery, expected=expected):
                    packets = []
                    helper = TimeoutHelper(lambda *packet: packets.append(packet), **{
                        "clipboards.local": ("CLIPBOARD",), "clipboards.remote": ("CLIPBOARD",),
                    })
                    proxy = helper._get_proxy("CLIPBOARD")
                    results = []
                    callback = (lambda *result: results.append(result)) if callback_delivery else None
                    try:
                        helper._send_clipboard_request_handler(proxy, "CLIPBOARD", "UTF8_STRING", callback)
                        helper._clipboard_got_contents(packets[-1][1], "UTF8_STRING", 8, data)
                        if callback_delivery:
                            self.assertEqual(results, [("UTF8_STRING", 8, expected)])
                            self.assertEqual(proxy.results, [])
                        else:
                            self.assertEqual(proxy.results, [("UTF8_STRING", "UTF8_STRING", 8, expected)])
                        self.assertEqual(helper._clipboard_outstanding_requests, {})
                    finally:
                        helper.cleanup()

    def test_timeout_helper_reset_drains_callbacks_without_reentry(self):
        packets = []
        helper = TimeoutHelper(
            lambda *packet: packets.append(packet),
            **{
                "clipboards.local": ("CLIPBOARD",),
                "clipboards.remote": ("CLIPBOARD",),
            },
        )
        proxy = helper._get_proxy("CLIPBOARD")
        results = []
        nested = []

        def first_result(dtype, dformat, data) -> None:
            results.append((dtype, dformat, data))
            helper._send_clipboard_request_handler(
                proxy, "CLIPBOARD", "nested",
                lambda *result: nested.append(result),
            )

        try:
            helper._send_clipboard_request_handler(
                proxy, "CLIPBOARD", "first", first_result,
            )
            helper._send_clipboard_request_handler(
                proxy, "CLIPBOARD", "second",
                lambda *result: results.append(result),
            )
            self.assertEqual(len(packets), 2)
            helper.client_reset()
            self.assertEqual(results, [("", 0, None), ("", 0, None)])
            self.assertEqual(nested, [("", 0, None)])
            self.assertEqual(len(packets), 2)
            self.assertEqual(helper._clipboard_outstanding_requests, {})
        finally:
            helper.cleanup()

    def test_timeout_helper_preserves_legacy_proxy_delivery(self):
        packets = []
        helper = TimeoutHelper(
            lambda *packet: packets.append(packet),
            **{
                "clipboards.local": ("CLIPBOARD",),
                "clipboards.remote": ("CLIPBOARD",),
            },
        )
        proxy = helper._get_proxy("CLIPBOARD")
        try:
            helper._send_clipboard_request_handler(proxy, "CLIPBOARD", "UTF8_STRING")
            helper._clipboard_got_contents(packets[0][1], "UTF8_STRING", 8, b"legacy")
            self.assertEqual(
                proxy.results,
                [("UTF8_STRING", "UTF8_STRING", 8, b"legacy")],
            )
        finally:
            helper.cleanup()

    def test_timeout_helper_callback_timeout_and_duplicate_are_terminal(self):
        packets = []
        helper = TimeoutHelper(
            lambda *packet: packets.append(packet),
            **{
                "clipboards.local": ("CLIPBOARD",),
                "clipboards.remote": ("CLIPBOARD",),
            },
        )
        proxy = helper._get_proxy("CLIPBOARD")
        results = []
        try:
            helper._send_clipboard_request_handler(
                proxy, "CLIPBOARD", "UTF8_STRING",
                lambda *result: results.append(result),
            )
            request_id = packets[0][1]
            timer = helper._clipboard_outstanding_requests[request_id][0]
            gi_import("GLib").source_remove(timer)
            helper.timeout_request(request_id)
            helper._clipboard_got_contents(request_id, "UTF8_STRING", 8, b"late")
            self.assertEqual(results, [("", 0, None)])
            self.assertEqual(helper._clipboard_outstanding_requests, {})
        finally:
            helper.cleanup()

    def test_modern_packet_omits_empty_targets(self):
        helper, proxy, packets = self.make_helper()
        with patch.object(core, "get_hex_uuid", return_value="origin-2"):
            helper._send_clipboard_token_handler(proxy, {"targets": (), "data": {}})
        self.assertEqual(packets, [(
            "clipboard-data",
            "CLIPBOARD",
            {"claim": True, "greedy": False, "origin": "origin-2"},
        )])

    def test_modern_packet_sends_multiple_data_items(self):
        helper, proxy, packets = self.make_helper()
        helper._send_clipboard_token_handler(proxy, {
            "targets": ("UTF8_STRING", "image/png"),
            "data": {
                "UTF8_STRING": ("STRING", 8, b"hello", "future-field"),
                "image/png": ("image/png", 8, b"PNG"),
            },
        })
        options = packets[0][2]
        self.assertEqual(options["data"], {
            "UTF8_STRING": ("STRING", 8, "bytes", b"hello"),
            "image/png": ("image/png", 8, "bytes", b"PNG"),
        })

    def test_modern_packet_multiple_targets(self):
        helper, proxy, _packets = self.make_helper()
        helper.process_clipboard_packet(Packet(
            "clipboard-data",
            "CLIPBOARD",
            {
                "targets": ("UTF8_STRING", "text/plain"),
                "data": {
                    "UTF8_STRING": ("STRING", 8, "bytes", b"hello"),
                    "text/plain": ("text/plain", 8, "bytes", b"world"),
                },
                "claim": False,
                "greedy": True,
                "synchronous": True,
            },
        ))
        self.assertEqual(proxy.tokens, [(
            ("UTF8_STRING", "text/plain"),
            {
                "UTF8_STRING": ("STRING", 8, b"hello"),
                "text/plain": ("text/plain", 8, b"world"),
            },
            False,
            True,
        )])
        self.assertTrue(proxy._greedy_client)

    def test_too_many_targets(self):
        helper, proxy, _packets = self.make_helper()
        count = core.MAX_CLIPBOARD_TARGETS * 4
        targets = tuple(f"text/x-{i}" for i in range(count))
        helper.process_clipboard_packet(Packet("clipboard-data", "CLIPBOARD", {
            "targets": targets,
            "data": {target: ("STRING", 8, "bytes", b"x") for target in targets[:core.MAX_CLIPBOARD_TARGETS]},
        }))
        local_targets, target_data, _claim, _sync = proxy.tokens[0]
        self.assertEqual(local_targets, targets[:core.MAX_CLIPBOARD_TARGETS])
        self.assertEqual(tuple(target_data.keys()), targets[:core.MAX_CLIPBOARD_TARGETS])
        # the targets can also be sent as clipboard contents:
        self.assertEqual(helper.local_targets(targets), targets[:core.MAX_CLIPBOARD_TARGETS])
        # but the data for that many targets is rejected:
        with self.assertRaises(ValueError):
            helper.process_clipboard_packet(Packet("clipboard-data", "CLIPBOARD", {
                "data": {target: ("STRING", 8, "bytes", b"x") for target in targets},
            }))
        self.assertEqual(len(proxy.tokens), 1)

    def test_malformed_packets_are_rejected(self):
        helper, proxy, packets = self.make_helper()
        item = ("STRING", 8, "bytes", b"x")
        for packet in (
            Packet("clipboard-data", "CLIPBOARD"),
            Packet("clipboard-data", "CLIPBOARD", {}, 1),
            Packet("clipboard-data", "CLIPBOARD", []),
            Packet("clipboard-data", "CLIPBOARD", {f"key{i}": i for i in range(17)}),
            Packet("clipboard-data", "CLIPBOARD", {"": 1}),
            Packet("clipboard-data", "CLIPBOARD", {"data": {"UTF8_STRING": item + ("extra", )}}),
            Packet("clipboard-data", "CLIPBOARD", {"data": {"UTF8_STRING": item[:3]}}),
            Packet("clipboard-data", "CLIPBOARD", {"origin": "x" * 65}),
            Packet("clipboard-data", "CLIPBOARD", {"origin": 1}),
            Packet("clipboard-data", "CLIPBOARD", {"targets": "UTF8_STRING"}),
            Packet("clipboard-data", "CLIP\nBOARD", {}),
            Packet("clipboard-request", 1, "CLIPBOARD"),
            Packet("clipboard-request", 1, "CLIPBOARD", "UTF8_STRING", 2),
            Packet("clipboard-request", -1, "CLIPBOARD", "UTF8_STRING"),
            Packet("clipboard-request", 1, "x" * 65, "UTF8_STRING"),
            Packet("clipboard-request", 1, "CLIPBOARD", ""),
            Packet("clipboard-request", 1, "CLIPBOARD", "x" * 257),
            Packet("clipboard-request", 1, "CLIPBOARD", "UTF8\x00STRING"),
            Packet("clipboard-contents", 1, "CLIPBOARD", "STRING", 8, "bytes"),
            Packet("clipboard-contents", 1, "CLIPBOARD", "STRING", 8, "bytes", b"x", 0, 0),
            Packet("clipboard-contents", 1, "CLIPBOARD", "", 8, "bytes", b"x"),
            Packet("clipboard-contents", 1, "CLIPBOARD", "STRING", 9, "bytes", b"x"),
            Packet("clipboard-contents", 1, "CLIPBOARD", "STRING", 8, "x" * 17, b"x"),
            Packet("clipboard-contents", 1, "CLIPBOARD", "STRING", 8, "bytes", b"x", -1),
            Packet("clipboard-contents-none", 1),
            Packet("clipboard-contents-none", 1, "CLIPBOARD", 2),
            Packet("clipboard-pending-requests", 1, 2),
            Packet("clipboard-pending-requests", 256),
            Packet("clipboard-enable-selections", ("CLIPBOARD", ), 1),
            Packet("clipboard-enable-selections", ["X"] * 9),
            Packet("clipboard-enable-selections", ["x" * 65]),
            Packet("clipboard-enable-selections", ["a\nb"]),
        ):
            with self.assertRaises((ValueError, TypeError), msg=f"{packet!r} accepted"):
                helper.process_clipboard_packet(packet)
        self.assertEqual(proxy.tokens, [])
        self.assertEqual(packets, [])

    def test_data_targets_and_options(self):
        helper, proxy, _packets = self.make_helper()
        helper.process_clipboard_packet(Packet("clipboard-data", "CLIPBOARD", {
            "origin": b"abc",
            # invalid target names are dropped:
            "targets": ("UTF8_STRING", "\x00", b"TEXT"),
            "claim": False,
            "synchronous": True,
        }))
        self.assertEqual(proxy.tokens, [(("UTF8_STRING", "TEXT"), None, False, True)])
        # a token which does not claim the selection keeps the local origin:
        self.assertEqual(proxy._clipboard_origin, "")
        self.assertFalse(proxy._greedy_client)
        helper.process_clipboard_packet(Packet("clipboard-data", "CLIPBOARD", {"greedy": True, "token": False}))
        self.assertTrue(proxy._greedy_client)
        self.assertEqual(len(proxy.tokens), 1)

    def test_enable_selections(self):
        helper, proxy, _packets = self.make_helper()
        primary = ClipboardProxy("PRIMARY")
        helper._clipboard_proxies["PRIMARY"] = primary
        helper.process_clipboard_packet(Packet("clipboard-enable-selections", ["CLIPBOARD"]))
        self.assertTrue(proxy.is_enabled())
        self.assertFalse(primary.is_enabled())
        helper.process_clipboard_packet(Packet("clipboard-enable-selections", [b"PRIMARY"]))
        self.assertFalse(proxy.is_enabled())
        self.assertTrue(primary.is_enabled())

    def test_pending_requests(self):
        helper, _proxy, _packets = self.make_helper()
        progress = []
        helper.progress_cb = lambda *args: progress.append(args)
        helper.process_clipboard_packet(Packet("clipboard-pending-requests", 3))
        self.assertEqual(progress, [(-1, 3)])

    def test_token_data_budget(self):
        helper, proxy, _packets = self.make_helper()
        helper.max_clipboard_packet_size = 1000
        data = {
            "UTF8_STRING": ("STRING", 8, "bytes", b"x" * 600),
            "text/plain": ("STRING", 8, "bytes", b"x" * 600),
        }
        with self.assertRaises(ValueError):
            helper.process_clipboard_packet(Packet("clipboard-data", "CLIPBOARD", {"data": data}))
        data["text/plain"] = ("STRING", 8, "bytes", b"x" * 400)
        helper.process_clipboard_packet(Packet("clipboard-data", "CLIPBOARD", {"data": data}))
        self.assertEqual(len(proxy.tokens), 1)

    def test_rejected_names_are_dropped(self):
        helper, proxy, packets = self.make_helper()
        refused = {"text/x-refused", "REFUSED_TYPE"}
        helper.accept_names = lambda names: set(names) - refused
        helper.process_clipboard_packet(Packet("clipboard-data", "CLIPBOARD", {
            "targets": ("UTF8_STRING", "text/x-refused"),
            "data": {
                "UTF8_STRING": ("STRING", 8, "bytes", b"hello"),
                "text/x-refused": ("STRING", 8, "bytes", b"x"),
                "TEXT": ("REFUSED_TYPE", 8, "bytes", b"x"),
            },
        }))
        targets, target_data, _claim, _sync = proxy.tokens[0]
        self.assertEqual(targets, ("UTF8_STRING", ))
        self.assertEqual(target_data, {"UTF8_STRING": ("STRING", 8, b"hello")})
        # a request for a refused target is answered without asking the proxy:
        with patch.object(proxy, "get_contents") as get_contents:
            helper.process_clipboard_packet(Packet("clipboard-request", 7, "CLIPBOARD", "text/x-refused"))
        get_contents.assert_not_called()
        self.assertEqual(packets, [("clipboard-contents-none", 7, "CLIPBOARD")])
        self.assertEqual(helper._peer_requests, {})

    def test_peer_request_limits(self):
        helper, proxy, packets = self.make_helper()
        callbacks = []
        with patch.object(proxy, "get_contents", side_effect=lambda _target, callback: callbacks.append(callback)):
            for request_id in range(core.MAX_CLIPBOARD_PEER_REQUESTS):
                helper.process_clipboard_packet(Packet("clipboard-request", request_id, "CLIPBOARD", "UTF8_STRING"))
            self.assertEqual(len(callbacks), core.MAX_CLIPBOARD_PEER_REQUESTS)
            self.assertEqual(packets, [])
            # a duplicate request id is ignored:
            helper.process_clipboard_packet(Packet("clipboard-request", 0, "CLIPBOARD", "UTF8_STRING"))
            self.assertEqual(packets, [])
            # one more is answered straight away:
            helper.process_clipboard_packet(Packet("clipboard-request", 1000, "CLIPBOARD", "UTF8_STRING"))
            self.assertEqual(packets, [("clipboard-contents-none", 1000, "CLIPBOARD")])
            self.assertEqual(len(callbacks), core.MAX_CLIPBOARD_PEER_REQUESTS)
            # answering a request makes room for another one:
            callbacks[0]("STRING", 8, b"hello")
            self.assertEqual(packets[-1], ("clipboard-contents", 0, "CLIPBOARD", "STRING", 8, "bytes", b"hello", 0))
            helper.process_clipboard_packet(Packet("clipboard-request", 1001, "CLIPBOARD", "UTF8_STRING"))
            self.assertEqual(len(callbacks), core.MAX_CLIPBOARD_PEER_REQUESTS + 1)
            # requests which are never answered expire:
            with patch.object(core, "monotonic", return_value=core.monotonic() + core.PEER_REQUEST_EXPIRY + 1):
                helper.process_clipboard_packet(Packet("clipboard-request", 1002, "CLIPBOARD", "UTF8_STRING"))
            self.assertEqual(len(callbacks), core.MAX_CLIPBOARD_PEER_REQUESTS + 2)
            self.assertEqual(tuple(helper._peer_requests), (1002, ))
        helper.client_reset()
        self.assertEqual(helper._peer_requests, {})

    def test_selection_capabilities_backwards_compatibility(self):
        caps = typedict({"greedy": ("CLIPBOARD",)})
        self.assertTrue(caps.boolget("greedy"))
        self.assertEqual(parse_greedy(caps), ("CLIPBOARD",))
        self.assertEqual(parse_greedy(typedict({"greedy": True})), tuple(ALL_CLIPBOARDS))
        self.assertEqual(parse_want_targets(typedict({"want_targets": True})), tuple(ALL_CLIPBOARDS))

        helper, _proxy, _packets = self.make_helper()
        helper.local_preferred_targets = ()
        helper.local_want_targets = ()
        helper.local_greedy = ("CLIPBOARD",)
        self.assertEqual(helper.get_caps()["greedy"], ("CLIPBOARD",))

    def test_per_selection_greedy(self):
        helper, clipboard, _packets = self.make_helper()
        primary = ClipboardProxy("PRIMARY")
        helper._clipboard_proxies["PRIMARY"] = primary
        helper.set_greedy_client(("PRIMARY",))
        self.assertFalse(clipboard._greedy_client)
        self.assertTrue(primary._greedy_client)

        helper._local_to_remote["CLIPBOARD"] = "PRIMARY"
        helper.set_greedy_client(("PRIMARY",))
        self.assertTrue(clipboard._greedy_client)

    def test_per_selection_want_targets(self):
        helper, clipboard, _packets = self.make_helper()
        primary = ClipboardProxy("PRIMARY")
        helper._clipboard_proxies["PRIMARY"] = primary
        helper.set_want_targets_client(("PRIMARY",))
        self.assertFalse(clipboard._want_targets)
        self.assertTrue(primary._want_targets)

        helper._local_to_remote["CLIPBOARD"] = "PRIMARY"
        helper.set_want_targets_client(("PRIMARY",))
        self.assertTrue(clipboard._want_targets)

    def test_outgoing_greedy_is_per_selection(self):
        helper, clipboard, packets = self.make_helper()
        primary = ClipboardProxy("PRIMARY")
        helper._clipboard_proxies["PRIMARY"] = primary
        helper.local_greedy = ("PRIMARY",)
        helper._send_clipboard_token_handler(clipboard, {})
        helper._send_clipboard_token_handler(primary, {})
        self.assertFalse(packets[0][2]["greedy"])
        self.assertTrue(packets[1][2]["greedy"])

    def test_remote_clipboard_translation(self):
        # the client does not pass the list of selections,
        # only the ones selected with `--local-clipboard` and `--remote-clipboard`:
        helper = ClipboardHelper(lambda *_packet: None, **{
            "clipboard.local": "CLIPBOARD",
            "clipboard.remote": "PRIMARY",
        })
        self.assertEqual(helper.local_to_remote("CLIPBOARD"), "PRIMARY")
        self.assertEqual(helper.remote_to_local("PRIMARY"), "CLIPBOARD")
        self.assertIn("PRIMARY", helper.get_remote_selections())

    def test_invalid_remote_clipboard(self):
        helper = ClipboardHelper(lambda *_packet: None, **{
            "clipboard.local": "CLIPBOARD",
            "clipboard.remote": "INVALID",
        })
        self.assertEqual(helper.local_to_remote("CLIPBOARD"), "CLIPBOARD")


def main():
    unittest.main()


if __name__ == "__main__":
    main()
