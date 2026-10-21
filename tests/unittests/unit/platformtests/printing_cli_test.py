#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 kogeler
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import os
import subprocess
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from xpra.os_util import POSIX, OSX
from xpra.platform import printing
from xpra.platform.paths import get_xpra_command


class PrintingCliTest(unittest.TestCase):

    def test_command_paths_do_not_acquire_or_release_a_client_watcher(self):
        # Enumeration/submission is synchronous. A pre-existing watcher belongs
        # to its long-lived caller; this CLI must neither restart nor cancel it.
        cases = (
            (("printing-info",), {}, 0, False),
            (("printing", __file__), {}, 1, False),
            (("printing", "/no-such-print-input"), {"office": {}}, 1, False),
            (("printing", "unknown", __file__), {"office": {}}, 1, False),
            (("printing", "office", "/no-such-print-input"), {"office": {}}, 1, False),
            (("printing", "office", __file__), {"office": {}}, 0, True),
            (("printing", __file__), {"office": {}}, 0, True),
        )
        for argv, printers, expected, submitted in cases:
            with self.subTest(argv=argv), ExitStack() as stack:
                init = stack.enter_context(patch.object(printing, "init_printing"))
                cleanup = stack.enter_context(patch.object(printing, "cleanup_printing"))
                stack.enter_context(patch.object(printing, "get_printers", return_value=printers))
                stack.enter_context(patch.object(printing, "get_info", return_value={}))
                stack.enter_context(patch.object(printing, "get_default_printer", return_value="office"))
                submit = stack.enter_context(patch.object(printing, "print_files"))
                stack.enter_context(patch("xpra.log.enable_color"))
                stack.enter_context(redirect_stdout(StringIO()))
                self.assertEqual(printing.main(list(argv)), expected)
                init.assert_not_called()
                cleanup.assert_not_called()
                if submitted:
                    submit.assert_called_once_with("office", [__file__], "Print Command", {})
                else:
                    submit.assert_not_called()

    def test_backend_exceptions_do_not_acquire_or_release_a_client_watcher(self):
        for operation in ("get_printers", "get_info", "print_files"):
            with self.subTest(operation=operation), ExitStack() as stack:
                init = stack.enter_context(patch.object(printing, "init_printing"))
                cleanup = stack.enter_context(patch.object(printing, "cleanup_printing"))
                stack.enter_context(patch.object(printing, "get_printers", return_value={"office": {}}))
                stack.enter_context(patch.object(printing, "get_info", return_value={}))
                stack.enter_context(patch.object(printing, "print_files"))
                stack.enter_context(patch.object(printing, "get_printer_attributes", return_value=[]))
                stack.enter_context(patch.object(printing, operation, side_effect=RuntimeError("backend failed")))
                stack.enter_context(patch("xpra.log.enable_color"))
                stack.enter_context(redirect_stdout(StringIO()))
                argv = ["printing", "office", __file__] if operation == "print_files" else ["printing-info"]
                with self.assertRaisesRegex(RuntimeError, "backend failed"):
                    printing.main(argv)
                init.assert_not_called()
                cleanup.assert_not_called()

    @unittest.skipUnless(POSIX and not OSX, "real PyCUPS CLI boundary")
    def test_real_printing_info_exits_without_cups_or_system_bus(self):
        # Require the actual supported backend, not a passing generic fallback.
        from xpra.platform import pycups_printing
        self.assertIs(printing.get_printers, pycups_printing.get_printers)
        with tempfile.TemporaryDirectory(prefix="xpra-printing-cli-") as directory:
            env = os.environ.copy()
            env.update({
                "CUPS_SERVER": str(Path(directory) / "absent-cups.sock"),
                "DBUS_SYSTEM_BUS_ADDRESS": f"unix:path={directory}/absent-bus.sock",
                "LC_ALL": "C",
            })
            try:
                result = subprocess.run(
                    [*get_xpra_command(), "printing-info"], env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    timeout=10, check=False,
                )
            except subprocess.TimeoutExpired as error:
                # subprocess.run kills/reaps its exact child on timeout, so the
                # expected clean negative cannot leak the real polling timer.
                self.fail(f"printing-info retained its non-daemon CUPS watcher: {error}")
            self.assertNotEqual(result.returncode, 0, "the deliberately absent CUPS server was accepted")
            self.assertIn("failed to connect to server", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
