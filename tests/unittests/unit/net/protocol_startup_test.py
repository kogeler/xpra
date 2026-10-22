#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 kogeler
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

"""Deterministic connection retirement before the parse worker enters."""

from threading import Thread
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from xpra.common import noop
from xpra.net.bytestreams import Connection
from xpra.net.protocol import socket_handler


class ProtocolStartupTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        socket_handler.packet_encoding.init_all()
        socket_handler.compression.init_all()

    def protocol(self, socktype="tcp"):
        connection = Connection("fixture", socktype, {})
        connection.read = Mock(side_effect=AssertionError("unexpected transport read"))
        connection.write = Mock(side_effect=AssertionError("unexpected transport write"))
        scheduler = SimpleNamespace(idle_add=Mock(return_value=1), timeout_add=Mock(return_value=1))
        protocol = socket_handler.SocketProtocol(connection, noop, scheduler=scheduler)

        def cleanup():
            protocol.close()
            protocol.clean()

        self.addCleanup(cleanup)
        return protocol, connection

    def enter_worker(self, protocol):
        errors = []

        def run():
            try:
                protocol._read_parse_thread_loop()
            except BaseException as exc:
                errors.append(exc)

        worker = Thread(target=run, name="fixture-parse-start", daemon=True)
        worker.start()
        worker.join(2)
        self.assertFalse(worker.is_alive(), "parse entry did not finish")
        return errors

    def check_retired(self, steal=False):
        protocol, connection = self.protocol()
        if steal:
            self.assertIs(protocol.steal_connection(), connection)
        else:
            protocol.close()
        self.assertTrue(protocol.is_closed())
        self.assertIsNone(protocol._conn)
        with patch.object(socket_handler, "install_parse_thread_seccomp") as install, \
                patch.object(protocol, "do_read_parse_thread_loop") as parse:
            errors = self.enter_worker(protocol)
        self.assertEqual(errors, [], "a retired connection escaped the worker entry")
        install.assert_not_called()
        parse.assert_not_called()

    def test_close_before_worker_entry(self):
        self.check_retired()

    def test_steal_before_worker_entry(self):
        self.check_retired(steal=True)

    def test_close_after_ui_start_admission_keeps_preparation_owner(self):
        protocol, _connection = self.protocol()
        worker = protocol._read_parser_thread
        protocol.start()
        start = protocol.idle_add.call_args.args[0]
        prepared = []
        errors = []

        def close_after_admission():
            protocol.close()
            return False

        with patch.object(worker, "is_alive", side_effect=close_after_admission), \
                patch.object(socket_handler, "prepare_parse_thread_seccomp",
                             side_effect=lambda conn: prepared.append(conn.socktype)), \
                patch.object(socket_handler, "install_parse_thread_seccomp") as install, \
                patch("threading.excepthook", side_effect=errors.append):
            try:
                start()
            finally:
                if worker.ident is not None:
                    worker.join(2)
        self.assertEqual(errors, [])
        self.assertEqual(prepared, ["tcp"])
        self.assertFalse(worker.is_alive())
        install.assert_not_called()

    def test_close_before_ui_start_does_not_start_worker(self):
        protocol, _connection = self.protocol()
        worker = protocol._read_parser_thread
        protocol.start()
        start = protocol.idle_add.call_args.args[0]
        protocol.close()
        with patch.object(socket_handler, "prepare_parse_thread_seccomp") as prepare:
            start()
        prepare.assert_not_called()
        self.assertIsNone(worker.ident)

    def test_live_connection_installs_policy_before_parsing(self):
        for socktype in ("tcp", "pipe"):
            with self.subTest(socktype=socktype):
                protocol, _connection = self.protocol(socktype)
                events = []
                with patch.object(socket_handler, "install_parse_thread_seccomp",
                                  side_effect=lambda value: events.append(("policy", value))), \
                        patch.object(protocol, "do_read_parse_thread_loop",
                                     side_effect=lambda: events.append(("parse", None))):
                    self.assertEqual(self.enter_worker(protocol), [])
                self.assertEqual(events, [("policy", socktype), ("parse", None)])

    def test_close_during_policy_installation_prevents_reads(self):
        protocol, connection = self.protocol()
        # Keep the actual parser loop: its existing closed check must stop I/O.
        with patch.object(socket_handler, "install_parse_thread_seccomp", side_effect=lambda _kind: protocol.close()), \
                patch.object(connection, "read", side_effect=AssertionError("read retired connection")) as read:
            self.assertEqual(self.enter_worker(protocol), [])
        self.assertTrue(protocol.is_closed())
        read.assert_not_called()

    def test_unexpected_installation_error_is_not_hidden(self):
        protocol, _connection = self.protocol()
        failure = RuntimeError("fixture installation error")
        with patch.object(socket_handler, "install_parse_thread_seccomp", side_effect=failure), \
                patch.object(protocol, "do_read_parse_thread_loop") as parse:
            self.assertEqual(self.enter_worker(protocol), [failure])
        parse.assert_not_called()


if __name__ == "__main__":
    unittest.main()
