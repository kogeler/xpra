#!/usr/bin/env python3
# Copyright (C) 2026 kogeler
"""Exercise the audit against real child-process failures, not invented exit codes."""

import subprocess
import sys
import unittest
from pathlib import Path

import runtime_audit


class RuntimeAuditTest(unittest.TestCase):
    def command(self, code):
        return subprocess.run(
            [sys.executable, str(Path(runtime_audit.__file__)), sys.executable, "-c", code],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
        )

    def test_joined_thread_exception_cannot_pass_with_zero_process_exit(self):
        result = self.command("from threading import Thread; t=Thread(target=lambda: 1/0); t.start(); t.join()")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"thread-exception", result.stdout)

    def test_real_logging_format_error_cannot_pass(self):
        result = self.command("import logging; logging.basicConfig(); logging.warning('%s', 1, 2)")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"logging-error", result.stdout)

    def test_unraisable_finalizer_cannot_pass(self):
        result = self.command("class Broken:\n def __del__(self): 1/0\nBroken()")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"unraisable-exception", result.stdout)

    def test_success_and_child_failure_keep_their_meaning(self):
        self.assertEqual(self.command("print('healthy')").returncode, 0)
        self.assertEqual(self.command("raise SystemExit(7)").returncode, 7)

    def test_caught_traceback_is_not_an_unhandled_failure(self):
        payload = (b"encoder context initialization failed\nTraceback (most recent call last):\n"
                   b"xpra.codecs.constants.EncodingNotSupported: unsupported codec\n")
        self.assertEqual(runtime_audit.failures(payload), ())
        self.assertEqual(runtime_audit.failures(payload, positive=True), ())
        self.assertNotEqual(self.command("raise RuntimeError('uncaught main thread')").returncode, 0)

    def test_live_resource_or_native_corruption_diagnostics_cannot_pass(self):
        for payload in (b"ResourceWarning: subprocess 10 is still running", b"corrupted double-linked list",
                        b"malloc(): unaligned tcache chunk detected", b"ERROR: AddressSanitizer: use-after-free"):
            with self.subTest(payload=payload):
                self.assertTrue(runtime_audit.failures(payload, positive=True))
                self.assertEqual(runtime_audit.failures(payload), ())


if __name__ == "__main__":
    unittest.main()
