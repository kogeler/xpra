#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 Antoine Martin <antoine@xpra.org>
# Copyright (C) 2026 kogeler
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import os
import fcntl
import ctypes
import sys
import unittest
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import patch

from xpra.os_util import gi_import

GLib = gi_import("GLib")

SELECTIONS = ("CLIPBOARD", "PRIMARY")
ORIGIN = "application/x-xpra-clipboard-origin"
TARGET = "application/x-xpra-generation-test"
EAGER_TARGETS = ("UTF8_STRING", "text/html")


def spin_until(predicate, timeout=2) -> bool:
    context = GLib.MainContext.default()
    deadline = monotonic() + timeout
    while monotonic() < deadline and not predicate():
        while context.pending():
            context.iteration(False)
        sleep(0.005)
    return predicate()


class FakeCompositor:
    """The proxies only use the compositor to subscribe to selection changes"""

    def __init__(self):
        self.connections: list[tuple[str, object]] = []
        self.event_listeners: dict[str, list] = {}

    def connect(self, signal, callback):
        self.connections.append((signal, callback))
        self.event_listeners.setdefault(signal, []).append(callback)
        return callback

    def disconnect(self, signal, callback) -> None:
        callbacks = self.event_listeners.get(signal, [])
        for index, registered in enumerate(callbacks):
            if registered is callback:
                callbacks.pop(index)
                break
        if not callbacks:
            self.event_listeners.pop(signal, None)

    def get_display_ptr(self) -> int:
        return 0

    def get_seat_ptr(self) -> int:
        return 0


class FakeSelectionAPI:
    """Stand in for the native selection, keyed by a fake source pointer"""

    def __init__(self):
        self.source = None
        self.origin = False
        self.reads: list[tuple[int, str, int]] = []

    def source_targets(self, source_ptr: int) -> tuple[str, ...]:
        targets = (f"application/x-test-{source_ptr}", )
        return (ORIGIN, ) + targets if self.origin else targets

    def send_source(self, source_ptr: int, target: str, fd: int) -> None:
        # the proxy closes its own end as soon as this returns,
        # so hold a copy to answer whenever the test wants to:
        self.reads.append((source_ptr, target, os.dup(fd)))

    def complete(self, index: int, data: bytes) -> None:
        _, _, fd = self.reads[index]
        try:
            os.write(fd, data)
        except BrokenPipeError:
            # like a real source whose consumer has gone: the proxy may already
            # have retired a stale read and closed its end of the pipe
            pass
        finally:
            os.close(fd)

    def set_source(self, source) -> None:
        self.source = source

    def clear(self) -> None:
        self.source = None


class SourceOwningSelectionAPI:
    """Stand in for the native selection, destroying a replaced source as wlroots does"""

    def __init__(self, targets=(TARGET,)):
        self.targets = targets
        self.write_fds = []
        self.clear_calls = 0
        self.set_calls = 0
        self.source = None

    def source_targets(self, _source_ptr: int) -> tuple[str, ...]:
        return self.targets

    def send_source(self, _source_ptr: int, _target: str, fd: int) -> None:
        self.write_fds.append(os.dup(fd))

    def set_source(self, source) -> None:
        self.set_calls += 1
        previous, self.source = self.source, source
        if previous is not None and previous is not source:
            previous.destroy()

    def clear(self) -> None:
        self.clear_calls += 1
        previous, self.source = self.source, None
        if previous is not None:
            previous.destroy()


class WaylandClipboardTokenTest(unittest.TestCase):

    def make_helper(self, can_send=True):
        # a missing native extension is a failure, not a reason to skip:
        from xpra.wayland.server.clipboard import WaylandClipboard
        kwargs = {"can-send": can_send, "can-receive": True}
        helper = WaylandClipboard(lambda *_packet: None, compositor=FakeCompositor(), **kwargs)
        self.addCleanup(helper.cleanup)
        helper.enable_selections(SELECTIONS)
        tokens: dict[str, list] = {selection: [] for selection in SELECTIONS}
        for selection, proxy in helper._clipboard_proxies.items():
            proxy.selection_api = FakeSelectionAPI()
            proxy.set_want_targets(True)
            proxy.connect("send-clipboard-token", lambda _proxy, token, l=tokens[selection]: l.append(token))
        return helper, tokens

    def test_isolated_owner_change_is_not_delayed(self):
        helper, tokens = self.make_helper()
        for selection, proxy in helper._clipboard_proxies.items():
            proxy.selection_changed(1)
            self.assertEqual(len(tokens[selection]), 1)
            self.assertEqual(tokens[selection][0]["targets"], ("application/x-test-1", ))
            self.assertEqual(proxy._emit_token_timer, 0)
            self.assertEqual(proxy._sent_token_events, 1)

    def test_burst_is_coalesced_into_the_latest_owner(self):
        helper, tokens = self.make_helper()
        for selection, proxy in helper._clipboard_proxies.items():
            for source_ptr in range(1, 101):
                proxy.selection_changed(source_ptr)
            # an owner burst must not become one wire packet per change:
            self.assertEqual(len(tokens[selection]), 1)
            self.assertNotEqual(proxy._emit_token_timer, 0)
        for selection, proxy in helper._clipboard_proxies.items():
            self.assertTrue(spin_until(lambda: len(tokens[selection]) == 2))
            self.assertEqual(tokens[selection][-1]["targets"], ("application/x-test-100", ))
            self.assertEqual(proxy._emit_token_timer, 0)
            self.assertEqual(proxy._sent_token_events, 2)

    def test_remote_token_cancels_the_one_we_had_scheduled(self):
        helper, tokens = self.make_helper()
        for selection, proxy in helper._clipboard_proxies.items():
            proxy.selection_changed(1)
            proxy.selection_changed(2)
            self.assertNotEqual(proxy._emit_token_timer, 0)
            proxy.got_token(("text/plain", ), {"text/plain": ("text/plain", 8, b"remote")})
            self.assertEqual(proxy._emit_token_timer, 0)
            self.assertFalse(spin_until(lambda: len(tokens[selection]) > 1, timeout=0.5))

    def test_stale_origin_read_is_dropped_when_the_pointer_is_reused(self):
        helper, tokens = self.make_helper()
        for selection, proxy in helper._clipboard_proxies.items():
            api = proxy.selection_api
            api.origin = True
            proxy.selection_changed(0x100)
            # the owner goes away, and the next source lands at the same address:
            proxy.selection_changed(0)
            proxy.selection_changed(0x100)
            self.assertEqual([(ptr, target) for ptr, target, _ in api.reads], [(0x100, ORIGIN)] * 2)
            api.complete(0, b"stale-origin")
            self.assertFalse(spin_until(lambda: proxy._clipboard_origin, timeout=0.5))
            self.assertEqual(tokens[selection], [])
            api.complete(1, b"current-origin")
            self.assertTrue(spin_until(lambda: len(tokens[selection]) == 1))
            self.assertEqual(proxy._clipboard_origin, "current-origin")
            self.assertEqual(tokens[selection][0]["targets"], ("application/x-test-256", ))

    def test_each_selection_is_built_from_its_own_native_types(self):
        helper, _ = self.make_helper()
        proxies = helper._clipboard_proxies
        self.assertEqual(helper.compositor.connections, [
            (proxies[selection].SELECTION_SIGNAL, proxies[selection].selection_changed) for selection in SELECTIONS
        ])
        for attribute in ("SELECTION_SIGNAL", "SELECTION_API", "SOURCE_CLASS"):
            values = [getattr(proxies[selection], attribute) for selection in SELECTIONS]
            self.assertNotEqual(values[0], values[1], f"both selections share {attribute}")

    def test_no_token_when_we_cannot_send(self):
        helper, tokens = self.make_helper(can_send=False)
        for selection, proxy in helper._clipboard_proxies.items():
            proxy.selection_changed(1)
            self.assertEqual(tokens[selection], [])
            self.assertEqual(proxy._emit_token_timer, 0)


class WaylandClipboardTest(unittest.TestCase):

    @staticmethod
    def make_helper():
        from xpra.wayland.server.clipboard import WaylandClipboard

        packets = []
        compositor = FakeCompositor()
        helper = WaylandClipboard(
            lambda *packet: packets.append(packet),
            compositor=compositor,
            **{"can-send": True, "can-receive": True},
        )
        helper.enable_selections(("CLIPBOARD", "PRIMARY"))
        for proxy in helper._clipboard_proxies.values():
            # These pipe/state controls do not install on a real seat. Do not
            # rely on a NULL native adapter silently accepting ownership.
            proxy.selection_api = SourceOwningSelectionAPI()
        return helper, compositor, packets

    @staticmethod
    def source_class(selection: str):
        from xpra.wayland.server.clipboard import WaylandPrimarySource, WaylandSelectionSource

        return WaylandSelectionSource if selection == "CLIPBOARD" else WaylandPrimarySource

    @staticmethod
    def make_manager(helper):
        from xpra.server.subsystem.clipboard import ClipboardManager

        owner = SimpleNamespace(uuid="clipboard-owner", protocol=object(), clipboard_enabled=True)
        server = SimpleNamespace(
            readonly=False, get_server_source=lambda protocol: owner if protocol is owner.protocol else None,
            get_sources_by_type=lambda *_args: (), setting_changed=lambda *_args: None,
        )
        manager = ClipboardManager(server)
        manager.enabled = True
        manager.direction = "both"
        manager.client = owner
        manager.helper = helper
        manager.idle_add = lambda callback, *args: callback(*args)
        return manager, owner

    @staticmethod
    def read_now(fd: int):
        try:
            return os.read(fd, 65536)
        except BlockingIOError:
            return None

    @staticmethod
    def large_pipe_payload(fd: int) -> bytes:
        capacity = fcntl.fcntl(fd, fcntl.F_GETPIPE_SZ)
        return bytes(range(256)) * (3 * capacity // 256 + 1)

    def test_unpublished_native_source_construction_releases_owner_reference(self):
        class BrokenTargets:
            def __iter__(self):
                yield TARGET
                raise RuntimeError("MIME iteration failed")

        for selection in ("CLIPBOARD", "PRIMARY"):
            with self.subTest(selection=selection):
                source_type = self.source_class(selection)
                source = source_type.__new__(source_type)
                references = sys.getrefcount(source)
                notified = []
                proxy = SimpleNamespace(source_destroyed=lambda value: notified.append(value))
                with self.assertRaisesRegex(RuntimeError, "MIME iteration failed"):
                    source.__init__(proxy, BrokenTargets())
                self.assertEqual(source.ptr(), 0)
                self.assertEqual(sys.getrefcount(source), references)
                self.assertEqual(notified, [])
                source.destroy()

    def test_native_destroy_releases_owner_even_if_proxy_notification_raises(self):
        def fail(_source):
            raise RuntimeError("destroy notification failed")

        for selection in ("CLIPBOARD", "PRIMARY"):
            with self.subTest(selection=selection):
                source = self.source_class(selection)(
                    SimpleNamespace(source_destroyed=fail), (TARGET,),
                )
                references = sys.getrefcount(source)
                source.destroy()
                self.assertEqual(source.ptr(), 0)
                self.assertEqual(sys.getrefcount(source), references - 1)
                source.destroy()

    def test_failed_native_publication_keeps_previous_source_and_metadata(self):
        for selection in ("CLIPBOARD", "PRIMARY"):
            with self.subTest(selection=selection):
                helper, _compositor, _packets = self.make_helper()
                proxy = helper._get_proxy(selection)
                try:
                    proxy.got_token((TARGET,), {TARGET: (TARGET, 8, b"old")})
                    previous = (
                        proxy.remote_source, proxy.remote_source_ptr, proxy.remote_generation,
                        proxy.targets, proxy.target_data, proxy._have_token,
                    )
                    with patch.object(proxy.selection_api, "set_source", side_effect=RuntimeError("no seat")):
                        with self.assertRaisesRegex(RuntimeError, "no seat"):
                            proxy.got_token(("text/plain",), {"text/plain": ("text/plain", 8, b"new")})
                    self.assertEqual((
                        proxy.remote_source, proxy.remote_source_ptr, proxy.remote_generation,
                        proxy.targets, proxy.target_data, proxy._have_token,
                    ), previous)
                    self.assertNotEqual(previous[0].ptr(), 0)
                finally:
                    helper.cleanup()

    def test_nonclaiming_and_receive_denied_tokens_preserve_local_state(self):
        for selection in ("CLIPBOARD", "PRIMARY"):
            for claim, receive in ((False, True), (True, False)):
                with self.subTest(selection=selection, claim=claim, receive=receive):
                    helper, _compositor, _packets = self.make_helper()
                    proxy = helper._get_proxy(selection)
                    try:
                        proxy.set_direction(True, receive)
                        proxy.local_source_ptr = 0x100
                        proxy.targets = (TARGET,)
                        proxy.target_data = {TARGET: (TARGET, 8, b"local")}
                        proxy._clipboard_origin = "local-origin"
                        before = (proxy.source_generation, proxy.targets, proxy.target_data)
                        with patch.object(proxy, "cancel_emit_token") as cancel:
                            proxy.got_token(("text/plain",), {"text/plain": ("text/plain", 8, b"remote")},
                                            claim=claim)
                        cancel.assert_not_called()
                        self.assertEqual((proxy.source_generation, proxy.targets, proxy.target_data), before)
                        self.assertEqual(proxy._clipboard_origin, "local-origin")
                        self.assertIsNone(proxy.remote_source)
                    finally:
                        helper.cleanup()

    def test_empty_claim_does_not_replace_a_native_owner_or_its_origin(self):
        from xpra.net.common import Packet

        for selection in ("CLIPBOARD", "PRIMARY"):
            with self.subTest(selection=selection):
                helper, _compositor, _packets = self.make_helper()
                proxy = helper._get_proxy(selection)
                proxy.local_source_ptr = 0x100
                proxy.targets = (TARGET,)
                proxy.target_data = {TARGET: (TARGET, 8, b"local")}
                proxy._clipboard_origin = "local-origin"
                try:
                    with patch.object(proxy, "cancel_emit_token") as cancel:
                        helper.process_clipboard_packet(Packet("clipboard-data", selection, {
                            "claim": True, "origin": "empty-remote",
                        }))
                    cancel.assert_not_called()
                    self.assertEqual(proxy.local_source_ptr, 0x100)
                    self.assertEqual(proxy.targets, (TARGET,))
                    self.assertEqual(proxy.target_data, {TARGET: (TARGET, 8, b"local")})
                    self.assertEqual(proxy._clipboard_origin, "local-origin")
                finally:
                    helper.cleanup()

    def test_empty_claim_retires_our_own_active_native_offer(self):
        for selection in ("CLIPBOARD", "PRIMARY"):
            with self.subTest(selection=selection):
                helper, _compositor, _packets = self.make_helper()
                proxy = helper._get_proxy(selection)
                try:
                    proxy.got_token((TARGET,), {TARGET: (TARGET, 8, b"remote")})
                    source = proxy.remote_source
                    proxy.local_source_ptr = source.ptr()
                    self.assertIs(proxy.got_token(()), True)
                    self.assertEqual(source.ptr(), 0)
                    self.assertIsNone(proxy.remote_source)
                    self.assertFalse(proxy._have_token)
                    self.assertEqual(proxy.targets, ())
                    self.assertEqual(proxy.target_data, {})
                finally:
                    helper.cleanup()

    def test_unavailable_native_adapter_refuses_before_taking_the_source(self):
        from xpra.wayland.server.clipboard import WaylandSelection, WaylandPrimarySelection

        for selection, adapter_type in (("CLIPBOARD", WaylandSelection), ("PRIMARY", WaylandPrimarySelection)):
            with self.subTest(selection=selection):
                source = self.source_class(selection)(None, (TARGET,))
                source_ptr = source.ptr()
                rfd, wfd = os.pipe()
                try:
                    adapter = adapter_type(0, 0)
                    with self.assertRaisesRegex(RuntimeError, "seat is not available"):
                        adapter.set_source(source)
                    self.assertEqual(source.ptr(), source_ptr)
                    # The unavailable send path still borrows the original FD.
                    adapter.send_source(source_ptr, TARGET, wfd)
                    os.write(wfd, b"still-owned")
                    self.assertEqual(os.read(rfd, 11), b"still-owned")
                finally:
                    source.destroy()
                    os.close(rfd)
                    os.close(wfd)

    def test_partial_proxy_setup_disconnects_every_constructed_proxy(self):
        from xpra.wayland.server.clipboard import WaylandClipboard, WaylandPrimaryClipboardProxy

        compositor = FakeCompositor()
        constructed = []
        original = WaylandPrimaryClipboardProxy.set_want_targets

        def configure(proxy, enabled):
            constructed.append(proxy)
            original(proxy, enabled)
            if proxy._selection == "PRIMARY":
                raise RuntimeError("second proxy setup failed")

        with patch.object(WaylandPrimaryClipboardProxy, "set_want_targets", new=configure):
            with self.assertRaisesRegex(RuntimeError, "second proxy setup failed"):
                WaylandClipboard(lambda *_packet: self.fail("partial helper sent a packet"), compositor=compositor)
        self.assertEqual([proxy._selection for proxy in constructed], ["CLIPBOARD", "PRIMARY"])
        self.assertEqual(compositor.event_listeners, {})
        for proxy in constructed:
            self.assertTrue(proxy.closing)
            self.assertIsNone(proxy.compositor)
            proxy.cleanup()

    def test_native_consumer_backpressure_preserves_complete_payload(self):
        GLib = gi_import("GLib")
        for selection in ("CLIPBOARD", "PRIMARY"):
            for cached in (False, True):
                with self.subTest(selection=selection, cached=cached):
                    helper, _compositor, packets = self.make_helper()
                    proxy = helper._get_proxy(selection)
                    rfd, wfd = os.pipe()
                    os.set_blocking(rfd, False)
                    # A nonblocking consumer FD makes the old single-write
                    # implementation fail with truncation, without hanging
                    # the tests-only clean control in its blocking write.
                    os.set_blocking(wfd, False)
                    payload = self.large_pipe_payload(wfd)
                    received = bytearray()
                    eof = []
                    dispatched = []

                    def drain():
                        chunk = self.read_now(rfd)
                        if chunk == b"":
                            eof.append(True)
                        elif chunk:
                            received.extend(chunk)
                        return bool(eof)

                    try:
                        target_data = {TARGET: (TARGET, 8, payload)} if cached else {}
                        proxy.got_token((TARGET,), target_data)
                        proxy.remote_source.send(TARGET, wfd)
                        requests = [packet for packet in packets if packet[0] == "clipboard-request"]
                        if cached:
                            self.assertEqual(requests, [])
                        else:
                            self.assertEqual(len(requests), 1)
                            helper._clipboard_got_contents(requests[0][1], TARGET, 8, payload)
                        # The consumer deliberately stays idle while another
                        # main-loop callback proves the compositor can run.
                        GLib.idle_add(lambda: dispatched.append(True))
                        self.assertTrue(spin_until(lambda: bool(dispatched)))
                        self.assertTrue(spin_until(drain), "native transfer did not reach EOF")
                        self.assertEqual(bytes(received), payload)
                        self.assertEqual(proxy.pending_writes, {})
                        self.assertEqual(proxy.pending_write_sources, {})
                    finally:
                        helper.cleanup()
                        os.close(rfd)

    def test_native_output_owns_byte_snapshot_of_buffer_views(self):
        for selection in ("CLIPBOARD", "PRIMARY"):
            for view_kind in ("typed", "strided", "mutable"):
                with self.subTest(selection=selection, view=view_kind):
                    helper, _compositor, _packets = self.make_helper()
                    proxy = helper._get_proxy(selection)
                    rfd, wfd = os.pipe()
                    os.set_blocking(rfd, False)
                    os.set_blocking(wfd, False)
                    storage = bytearray(self.large_pipe_payload(wfd))
                    data = memoryview(storage)
                    if view_kind == "typed":
                        data = data.cast("I")
                    elif view_kind == "strided":
                        data = data[::2]
                    expected = bytes(data)
                    received = bytearray()

                    def drain():
                        chunk = self.read_now(rfd)
                        if chunk:
                            received.extend(chunk)
                        return chunk == b""

                    try:
                        proxy.got_token((TARGET,), {TARGET: (TARGET, 8, data)})
                        proxy.remote_source.send(TARGET, wfd)
                        # The caller may reuse its mutable storage as soon as
                        # send returns; queued output must retain its bytes.
                        storage[:] = bytes(len(storage))
                        self.assertTrue(spin_until(drain), "buffer-view output did not reach EOF")
                        self.assertEqual(bytes(received), expected)
                        self.assertEqual(proxy.pending_writes, {})
                        self.assertEqual(proxy.pending_write_sources, {})
                    finally:
                        helper.cleanup()
                        os.close(rfd)

    def test_native_output_accepts_buffer_shapes_and_empty_replies(self):
        import numpy

        payloads = (
            ("scalar-view", memoryview(ctypes.c_ubyte(0)), b"\0"),
            ("scalar-buffer", ctypes.c_ubyte(0), b"\0"),
            ("numpy-multiple", numpy.array([0, 17, 255], dtype=numpy.uint8), b"\0\x11\xff"),
            ("numpy-zero", numpy.array([0], dtype=numpy.uint8), b"\0"),
            ("empty-array", numpy.array([], dtype=numpy.uint8), b""),
            ("empty-view", memoryview(b""), b""),
            ("empty-bytes", b"", b""),
            ("none", None, b""),
        )
        for selection in ("CLIPBOARD", "PRIMARY"):
            for cached in (False, True):
                for name, data, expected in payloads:
                    with self.subTest(selection=selection, cached=cached, payload=name):
                        helper, _compositor, packets = self.make_helper()
                        proxy = helper._get_proxy(selection)
                        rfd, wfd = os.pipe()
                        os.set_blocking(rfd, False)
                        received = bytearray()

                        def drain():
                            chunk = self.read_now(rfd)
                            if chunk:
                                received.extend(chunk)
                            return chunk == b""

                        try:
                            target_data = {TARGET: (TARGET, 8, data)} if cached else {}
                            proxy.got_token((TARGET,), target_data)
                            proxy.remote_source.send(TARGET, wfd)
                            requests = [packet for packet in packets if packet[0] == "clipboard-request"]
                            if cached:
                                self.assertEqual(requests, [])
                            else:
                                self.assertEqual(len(requests), 1)
                                self.assertIsNone(self.read_now(rfd), "request completed before its reply")
                                helper._clipboard_got_contents(requests[0][1], TARGET, 8, data)
                            self.assertTrue(spin_until(drain), "buffer output did not reach EOF")
                            self.assertEqual(bytes(received), expected)
                            self.assertEqual(proxy.pending_writes, {})
                            self.assertEqual(proxy.pending_write_sources, {})
                            self.assertEqual(helper._clipboard_outstanding_requests, {})
                        finally:
                            helper.cleanup()
                            os.close(rfd)

    def test_native_send_exception_cannot_close_reused_descriptor(self):
        from xpra.wayland.server import clipboard as clipboard_module

        GLib = gi_import("GLib")
        # Resolve wlroots through the actual extension's linked dependencies.
        # PyDLL keeps the GIL while its real C callback re-enters Python.
        native = ctypes.PyDLL(clipboard_module.__file__)
        for selection in ("CLIPBOARD", "PRIMARY"):
            symbol = (
                "wlr_data_source_send" if selection == "CLIPBOARD" else "wlr_primary_selection_source_send"
            )
            native_send = getattr(native, symbol)
            native_send.argtypes = (ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int)
            native_send.restype = None
            for boundary in ("nonblocking", "watch", "timer"):
                with self.subTest(selection=selection, boundary=boundary):
                    helper, _compositor, _packets = self.make_helper()
                    proxy = helper._get_proxy(selection)
                    rfd, wfd = os.pipe()
                    witness_rfd, witness_wfd = os.pipe()
                    os.set_blocking(rfd, False)
                    os.set_blocking(wfd, False)
                    payload = self.large_pipe_payload(wfd)
                    replacement_fds = []
                    finish = proxy.finish_pending_write

                    def retire_and_reuse(write_key):
                        finish(write_key)
                        replacement_fds.append(os.dup(witness_rfd))

                    owner, method = {
                        "nonblocking": (os, "set_blocking"),
                        "watch": (GLib, "io_add_watch"),
                        "timer": (GLib, "timeout_add"),
                    }[boundary]
                    try:
                        os.write(witness_wfd, b"independent descriptor")
                        proxy.got_token((TARGET,), {TARGET: (TARGET, 8, payload)})
                        with (
                            patch.object(proxy, "finish_pending_write", side_effect=retire_and_reuse),
                            patch.object(owner, method, side_effect=RuntimeError("injected output setup failure")),
                            patch.object(clipboard_module, "log") as mocked_log,
                        ):
                            native_send(proxy.remote_source.ptr(), TARGET.encode(), wfd)
                        mocked_log.error.assert_called_once()
                        self.assertEqual(replacement_fds, [wfd], "control did not reuse the retired descriptor")
                        os.fstat(wfd)
                        self.assertEqual(os.read(wfd, 64), b"independent descriptor")
                        self.assertEqual(proxy.pending_writes, {})
                        self.assertEqual(proxy.pending_write_sources, {})
                    finally:
                        helper.cleanup()
                        for fd in {rfd, wfd, witness_rfd, witness_wfd, *replacement_fds}:
                            try:
                                os.close(fd)
                            except OSError:
                                pass

    def test_native_consumer_pending_output_closes_on_lifetime_end(self):
        GLib = gi_import("GLib")
        context = GLib.MainContext.default()
        for selection in ("CLIPBOARD", "PRIMARY"):
            for ending in ("source", "reset", "cleanup", "timeout", "receive-disable", "selection-disable"):
                with self.subTest(selection=selection, ending=ending):
                    helper, _compositor, _packets = self.make_helper()
                    proxy = helper._get_proxy(selection)
                    rfd, wfd = os.pipe()
                    os.set_blocking(rfd, False)
                    os.set_blocking(wfd, False)
                    payload = self.large_pipe_payload(wfd)
                    eof = []

                    def drain():
                        if self.read_now(rfd) == b"":
                            eof.append(True)
                        return bool(eof)

                    try:
                        from xpra.wayland.server import clipboard as clipboard_module

                        proxy.got_token((TARGET,), {TARGET: (TARGET, 8, payload)})
                        with patch.object(clipboard_module, "REMOTE_TIMEOUT", 10 if ending == "timeout" else 2500):
                            proxy.remote_source.send(TARGET, wfd)
                        # Waiting for the remote reply and writing that reply
                        # into a full pipe are both source-owned lifetimes.
                        self.assertTrue(proxy.pending_writes)
                        sources = tuple(
                            source_id for ids in proxy.pending_write_sources.values() for source_id in ids
                        )
                        self.assertEqual(len(sources), 2)
                        if ending == "source":
                            proxy.remote_source.destroy()
                        elif ending == "reset":
                            helper.client_reset()
                        elif ending == "cleanup":
                            helper.cleanup()
                        elif ending == "receive-disable":
                            helper.set_direction(True, False)
                            helper.set_direction(True, True)
                        elif ending == "selection-disable":
                            helper.enable_selections()
                            helper.enable_selections(("CLIPBOARD", "PRIMARY"))
                        else:
                            self.assertTrue(spin_until(lambda: not proxy.pending_writes))
                        self.assertTrue(spin_until(drain), "retired transfer left its pipe open")
                        self.assertEqual(proxy.pending_writes, {})
                        self.assertEqual(proxy.pending_write_sources, {})
                        self.assertTrue(all(context.find_source_by_id(source_id) is None for source_id in sources))
                    finally:
                        helper.cleanup()
                        os.close(rfd)

    def test_direction_change_preserves_the_still_allowed_transfer(self):
        from xpra.net.common import Packet

        for selection in ("CLIPBOARD", "PRIMARY"):
            with self.subTest(selection=selection, allowed="receive"):
                helper, _compositor, packets = self.make_helper()
                proxy = helper._get_proxy(selection)
                proxy.selection_api = SourceOwningSelectionAPI()
                rfd, wfd = os.pipe()
                os.set_blocking(rfd, False)
                try:
                    proxy.got_token((TARGET,))
                    source = proxy.remote_source
                    proxy.local_source_ptr = source.ptr()
                    source.send(TARGET, wfd)
                    request = packets[-1][1]
                    helper.set_direction(False, True)
                    helper.set_direction(True, True)
                    self.assertIs(proxy.remote_source, source)
                    self.assertEqual(proxy.selection_api.clear_calls, 0)
                    self.assertIn(request, helper._clipboard_outstanding_requests)
                    self.assertIsNone(self.read_now(rfd))
                    helper.process_clipboard_packet(Packet(
                        "clipboard-contents", request, selection, TARGET, 8, "bytes", b"allowed-receive",
                    ))
                    self.assertEqual(self.read_now(rfd), b"allowed-receive")
                    self.assertEqual(self.read_now(rfd), b"")
                finally:
                    helper.cleanup()
                    os.close(rfd)
            with self.subTest(selection=selection, allowed="send"):
                helper, _compositor, _packets = self.make_helper()
                proxy = helper._get_proxy(selection)
                api = SourceOwningSelectionAPI()
                proxy.selection_api = api
                proxy.local_source_ptr = 0x1234
                results = []
                try:
                    proxy.get_contents(TARGET, lambda *result: results.append(result))
                    self.assertTrue(proxy.pending_reads)
                    helper.set_direction(True, False)
                    helper.set_direction(True, True)
                    self.assertTrue(proxy.pending_reads)
                    self.assertEqual(results, [])
                    os.write(api.write_fds[0], b"allowed-send")
                    os.close(api.write_fds.pop())
                    self.assertTrue(spin_until(lambda: bool(results)))
                    self.assertEqual(results, [(TARGET, 8, b"allowed-send")])
                    self.assertEqual(proxy.pending_reads, {})
                finally:
                    helper.cleanup()
                    for fd in api.write_fds:
                        os.close(fd)

    def test_policy_cycle_cancels_remote_request_without_clearing_new_native_owner(self):
        from xpra.net.common import Packet

        for selection in ("CLIPBOARD", "PRIMARY"):
            for boundary in ("status", "direction", "selection"):
                for native_owner in (False, True):
                    with self.subTest(selection=selection, boundary=boundary, native_owner=native_owner):
                        helper, _compositor, packets = self.make_helper()
                        manager, owner = self.make_manager(helper)
                        proxy = helper._get_proxy(selection)
                        api = SourceOwningSelectionAPI()
                        proxy.selection_api = api
                        rfd1 = rfd2 = -1
                        try:
                            proxy.got_token((TARGET,))
                            source = proxy.remote_source
                            proxy.local_source_ptr = 0x1234 if native_owner else source.ptr()
                            rfd1, wfd1 = os.pipe()
                            os.set_blocking(rfd1, False)
                            source.send(TARGET, wfd1)
                            request1 = packets[-1][1]
                            self.assertIn(request1, helper._clipboard_outstanding_requests)
                            manager.set_clipboard_enabled_status(owner, True)
                            manager.control_command_clipboard_direction("both")
                            helper.enable_selections(("CLIPBOARD", "PRIMARY"))
                            self.assertIs(proxy.remote_source, source)
                            self.assertIsNone(self.read_now(rfd1), "no-op policy change retired a live request")
                            if boundary == "status":
                                manager.set_clipboard_enabled_status(owner, False)
                                manager.set_clipboard_enabled_status(owner, True)
                            elif boundary == "direction":
                                manager.control_command_clipboard_direction("to-client")
                                manager.control_command_clipboard_direction("both")
                            else:
                                manager._process_packet(owner.protocol, Packet("clipboard-enable-selections", ()))
                            manager._process_packet(
                                owner.protocol, Packet("clipboard-enable-selections", ("CLIPBOARD", "PRIMARY")),
                            )
                            self.assertEqual(self.read_now(rfd1), b"", "policy loss left an old request FD open")
                            self.assertEqual(api.clear_calls, 0 if native_owner else 1)
                            if boundary != "selection":
                                self.assertNotIn(request1, helper._clipboard_outstanding_requests)
                            proxy.got_token((TARGET,))
                            rfd2, wfd2 = os.pipe()
                            os.set_blocking(rfd2, False)
                            proxy.remote_source.send(TARGET, wfd2)
                            request2 = packets[-1][1]
                            self.assertNotEqual(request1, request2)
                            manager._process_packet(owner.protocol, Packet(
                                "clipboard-contents", request1, selection, TARGET, 8, "bytes", b"stale-policy",
                            ))
                            self.assertIsNone(self.read_now(rfd2), "stale response completed a fresh consumer")
                            manager._process_packet(owner.protocol, Packet(
                                "clipboard-contents", request2, selection, TARGET, 8, "bytes", b"fresh-policy",
                            ))
                            self.assertEqual(self.read_now(rfd2), b"fresh-policy")
                            self.assertEqual(self.read_now(rfd2), b"")
                            self.assertEqual(helper._clipboard_outstanding_requests, {})
                        finally:
                            manager.cleanup()
                            for fd in (rfd1, rfd2):
                                if fd >= 0:
                                    os.close(fd)

    def test_policy_loss_cancels_native_reads_and_forbids_eager_data_until_restored(self):
        for selection in ("CLIPBOARD", "PRIMARY"):
            for boundary in ("status", "direction", "selection"):
                with self.subTest(selection=selection, boundary=boundary):
                    helper, _compositor, packets = self.make_helper()
                    manager, owner = self.make_manager(helper)
                    proxy = helper._get_proxy(selection)
                    api = SourceOwningSelectionAPI(EAGER_TARGETS)
                    proxy.selection_api = api
                    proxy._greedy_client = True
                    results = []
                    try:
                        proxy.selection_changed(0x1234)
                        self.assertEqual(len(api.write_fds), 1)
                        if boundary == "status":
                            manager.set_clipboard_enabled_status(owner, False)
                        elif boundary == "direction":
                            manager.control_command_clipboard_direction("to-server")
                        else:
                            helper.enable_selections()
                        self.assertEqual(proxy.pending_reads, {})
                        self.assertEqual(proxy.pending_read_timers, {})
                        with self.assertRaises(BrokenPipeError):
                            os.write(api.write_fds[0], b"stale-policy")
                        proxy.selection_changed(0x2345)
                        self.assertEqual(len(api.write_fds), 1, "revoked policy admitted another eager read")
                        self.assertEqual(proxy._emit_token_timer, 0, "revoked policy scheduled a local token")
                        proxy.get_contents(EAGER_TARGETS[0], lambda *result: results.append(result))
                        self.assertEqual(results, [("", 0, b"")])
                        self.assertEqual(packets, [], "revoked policy emitted local clipboard data")
                        manager.set_clipboard_enabled_status(owner, True)
                        manager.control_command_clipboard_direction("both")
                        helper.enable_selections(("CLIPBOARD", "PRIMARY"))
                        proxy.selection_changed(0x3456)
                        # the shared scheduler spaces this token from the first one:
                        self.assertTrue(spin_until(lambda: len(api.write_fds) == 2),
                                        "restored policy did not collect the eager data")
                        os.write(api.write_fds[1], b"fresh-policy")
                        os.close(api.write_fds.pop())
                        self.assertTrue(spin_until(lambda: len(api.write_fds) == 2))
                        os.close(api.write_fds.pop())
                        self.assertTrue(spin_until(lambda: bool(packets)))
                        self.assertEqual(len(packets), 1)
                        packet = packets[0]
                        if packet[0] == "clipboard-data":
                            data = packet[2]["data"][EAGER_TARGETS[0]][3]
                        else:
                            self.assertEqual(packet[0], "clipboard-token")
                            data = packet[7]
                        self.assertEqual(bytes(data), b"fresh-policy")
                    finally:
                        manager.cleanup()
                        for fd in api.write_fds:
                            os.close(fd)

    def test_remote_replacement_keeps_request_results_generation_bound(self):
        for selection in ("CLIPBOARD", "PRIMARY"):
            with self.subTest(selection=selection):
                helper, _compositor, packets = self.make_helper()
                proxy = helper._get_proxy(selection)
                source_type = self.source_class(selection)
                rfd1 = rfd2 = -1
                try:
                    proxy.got_token((TARGET,))
                    source1 = proxy.remote_source
                    self.assertIsInstance(source1, source_type)
                    rfd1, wfd1 = os.pipe()
                    os.set_blocking(rfd1, False)
                    source1.send(TARGET, wfd1)

                    proxy.got_token((TARGET,))
                    source2 = proxy.remote_source
                    self.assertIsInstance(source2, source_type)
                    self.assertIsNot(source2, source1)
                    # A real wlr_seat_set_*_selection call destroys source1
                    # synchronously while installing source2.
                    source1.destroy()

                    rfd2, wfd2 = os.pipe()
                    os.set_blocking(rfd2, False)
                    source2.send(TARGET, wfd2)

                    requests = [packet for packet in packets if packet[0] == "clipboard-request"]
                    self.assertEqual(len(requests), 2)
                    request1, request2 = requests
                    helper._clipboard_got_contents(request1[1], TARGET, 8, b"generation-one")
                    self.assertIsNone(
                        self.read_now(rfd2),
                        "the old request completed or corrupted the new generation pipe",
                    )
                    helper._clipboard_got_contents(request2[1], TARGET, 8, b"generation-two")
                    self.assertEqual(self.read_now(rfd2), b"generation-two")
                    self.assertEqual(self.read_now(rfd1), b"")
                finally:
                    helper.cleanup()
                    for fd in (rfd1, rfd2):
                        if fd >= 0:
                            try:
                                os.close(fd)
                            except OSError:
                                pass

    def test_native_read_timeout_and_size_limits_complete_once(self):
        from xpra.wayland.server import clipboard as clipboard_module

        for selection in ("CLIPBOARD", "PRIMARY"):
            for boundary in ("timeout", "origin-size", "packet-size", "eof"):
                with self.subTest(selection=selection, boundary=boundary):
                    helper, _compositor, _packets = self.make_helper()
                    proxy = helper._get_proxy(selection)
                    api = SourceOwningSelectionAPI()
                    proxy.selection_api = api
                    proxy.local_source_ptr = 0x1234
                    helper.max_clipboard_packet_size = 32
                    target = clipboard_module.ORIGIN_MIME_TYPE if boundary == "origin-size" else TARGET
                    results = []
                    payload = b"x" * (clipboard_module.MAX_ORIGIN_SIZE + 1)
                    try:
                        with patch.object(clipboard_module, "REMOTE_TIMEOUT", 10 if boundary == "timeout" else 60_000):
                            proxy.get_contents(target, lambda *result: results.append(result))
                        self.assertEqual(len(api.write_fds), 1)
                        fd = api.write_fds[0]
                        if boundary == "eof":
                            payload = b"final bytes queued before HUP"
                            os.write(fd, payload)
                            os.close(api.write_fds.pop())
                        elif boundary != "timeout":
                            os.write(fd, payload)
                            # Keep the native writer open: oversize must be
                            # rejected without waiting for its EOF.
                        self.assertTrue(spin_until(lambda: bool(results)))
                        expected = (target, 8, payload) if boundary == "eof" else ("", 0, b"")
                        self.assertEqual(results, [expected])
                        self.assertEqual(proxy.pending_reads, {})
                        self.assertEqual(proxy.pending_read_timers, {})
                        spin_until(lambda: len(results) > 1, 0.02)
                        self.assertEqual(results, [expected])
                    finally:
                        helper.cleanup()
                        for fd in api.write_fds:
                            os.close(fd)

    def test_native_read_preserves_explicit_send_truncation(self):
        from xpra.net.common import Packet

        helper, _compositor, packets = self.make_helper()
        proxy = helper._get_proxy("CLIPBOARD")
        api = SourceOwningSelectionAPI()
        proxy.selection_api = api
        proxy.local_source_ptr = 0x1234
        helper.max_clipboard_packet_size = 32
        helper.set_limits(16, None)
        payload = bytes(range(64))
        try:
            helper.process_clipboard_packet(Packet("clipboard-request", 7, "CLIPBOARD", TARGET))
            os.write(api.write_fds[0], payload)
            os.close(api.write_fds.pop())
            self.assertTrue(spin_until(lambda: bool(packets)))
            self.assertEqual(packets, [
                ("clipboard-contents", 7, "CLIPBOARD", TARGET, 8, "bytes", payload[:16], 48),
            ])
        finally:
            helper.cleanup()
            for fd in api.write_fds:
                os.close(fd)

    def test_native_read_setup_failure_retires_both_pipe_ends(self):
        GLib = gi_import("GLib")
        for boundary in ("nonblocking", "watch", "timer", "send"):
            with self.subTest(boundary=boundary):
                helper, _compositor, _packets = self.make_helper()
                proxy = helper._get_proxy("CLIPBOARD")
                api = SourceOwningSelectionAPI()
                proxy.selection_api = api
                proxy.local_source_ptr = 0x1234
                results = []
                pipes = []
                real_pipe = os.pipe

                def capture_pipe():
                    pair = real_pipe()
                    pipes.extend(pair)
                    return pair

                owner, method = {
                    "nonblocking": (os, "set_blocking"),
                    "watch": (GLib, "io_add_watch"),
                    "timer": (GLib, "timeout_add"),
                    "send": (api, "send_source"),
                }[boundary]
                try:
                    with (
                        patch.object(os, "pipe", new=capture_pipe),
                        patch.object(owner, method, side_effect=RuntimeError("injected read setup failure")),
                        self.assertRaisesRegex(RuntimeError, "injected read setup"),
                    ):
                        proxy.get_contents(TARGET, lambda *result: results.append(result))
                    self.assertEqual(results, [("", 0, b"")])
                    self.assertEqual(proxy.pending_reads, {})
                    self.assertEqual(proxy.pending_read_timers, {})
                    self.assertEqual(len(pipes), 2)
                    for fd in pipes:
                        with self.assertRaises(OSError):
                            os.fstat(fd)
                finally:
                    helper.cleanup()

    def test_closed_proxy_cannot_start_native_read(self):
        helper, _compositor, _packets = self.make_helper()
        proxy = helper._get_proxy("CLIPBOARD")
        proxy.local_source_ptr = 0x1234
        results = []
        helper.cleanup()
        with patch.object(os, "pipe", side_effect=AssertionError("post-cleanup pipe")):
            proxy.get_contents(TARGET, lambda *result: results.append(result))
        self.assertEqual(results, [("", 0, b"")])

    def test_selection_adapter_borrows_request_fd_without_display(self):
        from xpra.wayland.server.clipboard import WaylandSelection, WaylandPrimarySelection

        for adapter_class in (WaylandSelection, WaylandPrimarySelection):
            with self.subTest(adapter=adapter_class):
                rfd, wfd = os.pipe()
                try:
                    adapter_class(0, 0).send_source(0, TARGET, wfd)
                    # The read caller remains the only closer of this FD;
                    # wlroots may own only an adapter-created duplicate.
                    os.fstat(wfd)
                finally:
                    os.close(rfd)
                    try:
                        os.close(wfd)
                    except OSError:
                        pass

    def test_local_replacement_cancels_stale_read_generation(self):
        for selection in ("CLIPBOARD", "PRIMARY"):
            with self.subTest(selection=selection):
                helper, _compositor, _packets = self.make_helper()
                proxy = helper._get_proxy(selection)
                selection_api = SourceOwningSelectionAPI()
                proxy.selection_api = selection_api
                results = []
                try:
                    # Repeating the pointer deliberately covers allocator ABA:
                    # generation, not pointer identity, owns the pending read.
                    proxy.selection_changed(0x1234)
                    def got_contents(dtype, dformat, data, results=results) -> None:
                        results.append((dtype, dformat, data))

                    proxy.get_contents(TARGET, got_contents)
                    self.assertEqual(len(selection_api.write_fds), 1)
                    proxy.selection_changed(0x1234)
                    self.assertEqual(results, [("", 0, b"")])
                    old_wfd = selection_api.write_fds.pop()
                    try:
                        os.write(old_wfd, b"stale-generation")
                    except BrokenPipeError:
                        pass
                    finally:
                        os.close(old_wfd)
                    spin_until(lambda results=results: len(results) > 1, 0.2)
                    self.assertEqual(results, [("", 0, b"")])
                finally:
                    for fd in selection_api.write_fds:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                    helper.cleanup()

    def test_owner_replacement_stops_stale_eager_collection(self):
        for selection in ("CLIPBOARD", "PRIMARY"):
            with self.subTest(selection=selection):
                helper, _compositor, _packets = self.make_helper()
                proxy = helper._get_proxy(selection)
                selection_api = SourceOwningSelectionAPI(EAGER_TARGETS)
                proxy.selection_api = selection_api
                proxy._greedy_client = True
                try:
                    proxy.selection_changed(0x1234)
                    self.assertEqual(len(selection_api.write_fds), 1)

                    # Prevent the replacement itself from starting a new
                    # token.  Completing the cancelled old read must not make
                    # its sequential collector request the next target from
                    # the new generation, even when the allocator reuses the
                    # same source pointer.
                    proxy._enabled = False
                    proxy.selection_changed(0x1234)
                    self.assertEqual(len(selection_api.write_fds), 1)
                finally:
                    for fd in selection_api.write_fds:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                    helper.cleanup()

    def test_native_clear_resets_cached_state(self):
        for selection in ("CLIPBOARD", "PRIMARY"):
            with self.subTest(selection=selection):
                helper, _compositor, _packets = self.make_helper()
                proxy = helper._get_proxy(selection)
                try:
                    proxy.targets = (TARGET,)
                    proxy.target_data = {TARGET: (TARGET, 8, b"cached")}
                    proxy._clipboard_origin = "old-origin"
                    proxy.remote_source_ptr = 0
                    proxy.selection_changed(0)
                    self.assertEqual(proxy.targets, ())
                    self.assertEqual(proxy.target_data, {})
                    self.assertEqual(proxy._clipboard_origin, "")
                finally:
                    helper.cleanup()

    def test_active_remote_clear_destroys_source_when_adapter_cannot(self):
        for selection in ("CLIPBOARD", "PRIMARY"):
            with self.subTest(selection=selection):
                helper, _compositor, _packets = self.make_helper()
                proxy = helper._get_proxy(selection)
                selection_api = SourceOwningSelectionAPI()
                proxy.selection_api = selection_api
                try:
                    proxy.got_token((TARGET,))
                    source = proxy.remote_source
                    source_ptr = source.ptr()
                    self.assertNotEqual(source_ptr, 0)
                    proxy.local_source_ptr = source_ptr

                    # Explicitly model an adapter which cannot destroy. The
                    # normal fake now performs native-like synchronous destroy.
                    with patch.object(selection_api, "clear", return_value=None) as clear:
                        proxy.clear_remote_source()
                    clear.assert_called_once_with()
                    self.assertEqual(source.ptr(), 0)
                    self.assertIsNone(proxy.remote_source)
                    self.assertEqual(proxy.remote_source_ptr, 0)
                finally:
                    helper.cleanup()

    def test_cleanup_retires_source_and_disconnects_after_native_clear_failure(self):
        for selection in ("CLIPBOARD", "PRIMARY"):
            with self.subTest(selection=selection):
                helper, compositor, _packets = self.make_helper()
                proxy = helper._get_proxy(selection)
                proxy.got_token((TARGET,))
                source = proxy.remote_source
                proxy.local_source_ptr = source.ptr()
                with patch.object(proxy.selection_api, "clear", side_effect=RuntimeError("clear failed")):
                    with self.assertRaisesRegex(RuntimeError, "clear failed"):
                        helper.cleanup()
                self.assertEqual(source.ptr(), 0)
                self.assertIsNone(proxy.remote_source)
                self.assertEqual(compositor.event_listeners, {})
                self.assertEqual(helper._clipboard_proxies, {})
                helper.cleanup()

    def test_cleanup_disconnects_compositor_signals(self):
        helper, compositor, _packets = self.make_helper()
        self.assertEqual(tuple(compositor.event_listeners), ("selection", "primary-selection"))
        self.assertTrue(all(len(callbacks) == 1 for callbacks in compositor.event_listeners.values()))
        helper.cleanup()
        self.assertEqual(compositor.event_listeners, {})
        helper.cleanup()

    def test_real_compositor_listener_disconnect_contract(self):
        from xpra.wayland.server.compositor import WaylandCompositor

        compositor = WaylandCompositor()
        events = []

        def handler(value):
            events.append(value)

        registration = compositor.connect("selection", handler)
        self.assertIs(registration, handler)
        compositor.emit("selection", 1)
        compositor.disconnect("selection", registration)
        compositor.disconnect("selection", registration)
        compositor.emit("selection", 2)
        self.assertEqual(events, [1])

if __name__ == "__main__":
    unittest.main()
