#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 kogeler
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import logging
from types import SimpleNamespace
import unittest

from xpra.net.common import Packet
from xpra.server.subsystem import window as window_module
from xpra.server.subsystem.window import WindowServer


class WindowLoggingTest(unittest.TestCase):
    def setUp(self):
        self.messages = []
        messages = self.messages

        class Handler(logging.Handler):
            def emit(self, record):
                # Actually format the record: mocking Logger.info would miss
                # a placeholder/argument mismatch that the real sink rejects.
                messages.append(record.getMessage())

        logger = window_module.log._logger
        old_level, old_propagate = logger.level, logger.propagate
        handler = Handler()
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        self.addCleanup(logger.removeHandler, handler)
        self.addCleanup(logger.setLevel, old_level)
        self.addCleanup(setattr, logger, "propagate", old_propagate)

    def test_unmap_formats_protocol_and_packet(self):
        server = SimpleNamespace(is_readonly=lambda _proto: False)
        packet = Packet("window-unmap", 7, False, {})
        WindowServer._process_unmap(server, "fixture-protocol", packet)
        self.assertEqual(self.messages, [f"_process_unmap(fixture-protocol, {packet})"])

    def test_readonly_unmap_does_not_log(self):
        server = SimpleNamespace(is_readonly=lambda _proto: True)
        WindowServer._process_unmap(server, "fixture-protocol", Packet("window-unmap", 7))
        self.assertEqual(self.messages, [])

    def test_adjacent_handlers_keep_the_same_diagnostic_convention(self):
        server = SimpleNamespace(is_readonly=lambda _proto: False)
        for name in ("map", "close"):
            packet = Packet(f"window-{name}", 7)
            getattr(WindowServer, f"_process_{name}")(server, "fixture-protocol", packet)
            self.assertEqual(self.messages[-1], f"_process_{name}(fixture-protocol, {packet})")


if __name__ == "__main__":
    unittest.main()
