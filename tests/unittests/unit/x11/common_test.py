#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2020 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import unittest
import builtins
import os
import subprocess
import sys
from unittest.mock import patch

# pylint: disable=import-outside-toplevel


class TestDisplayUtil(unittest.TestCase):

    def test_client_list_stacking_supported(self):
        from xpra.x11.common import DEFAULT_NET_SUPPORTED
        self.assertIn("_NET_CLIENT_LIST_STACKING", DEFAULT_NET_SUPPORTED)

    def test_repr(self):
        from xpra.x11.common import X11Event, REPR_FUNCTIONS

        class Custom():  # pylint: disable=too-few-public-methods
            def repr(self):
                return "Custom"

        def custom_repr(*_args):
            return "XXXXX"

        REPR_FUNCTIONS[Custom] = custom_repr
        name = "00name00"
        e = X11Event(0, name, True, 255, 0)
        e.custom = Custom()

        def f(s, find=True):
            if repr(e).find(s)>=0 == find:
                #print("repr=%s" % repr(e))
                raise ValueError(f"{s!r} in {e!r}: {not find}")
        f(name)
        f(f"{e.serial:x}")
        f("XXXXX", True)

    def test_native_selection_router_does_not_import_gtk(self):
        from xpra.x11.selection import common
        from xpra.x11.selection import clipboard

        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name == "xpra.x11.gtk" or name.startswith("xpra.x11.gtk."):
                raise AssertionError("native selection routing imported GTK")
            return original_import(name, *args, **kwargs)

        with patch.object(common, "x11_event_loop_running", return_value=True):
            with patch.object(clipboard, "x11_event_loop_running", return_value=True):
                with patch.object(builtins, "__import__", side_effect=guarded_import):
                    self.assertIsNone(common.gtk_event_window(0x1234))
                    # No fields or GTK window are needed on this early branch.
                    clipboard.X11Clipboard.init_gtk_filter(object())

    def test_x11_filter_lease_return_values_describe_installation(self):
        # Start with a fresh native counter, independent of other unit tests'
        # still-live GTK subsystems. The third release is an intentional
        # underflow control, not ownership borrowed from another subsystem.
        script = """
from unit.process_test_util import DisplayContext
with DisplayContext():
    from xpra.x11.gtk.bindings import init_x11_filter, cleanup_x11_filter
    results = (
        init_x11_filter(), init_x11_filter(), cleanup_x11_filter(),
        cleanup_x11_filter(), cleanup_x11_filter(),
        init_x11_filter(), cleanup_x11_filter(),
    )
    assert results == (True, False, False, True, False, True, True), results
"""
        result = subprocess.run(
            (sys.executable, "-c", script),
            env=dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path)),
            capture_output=True, text=True, timeout=30, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


def main():
    from xpra.os_util import POSIX, OSX
    #can only work with an X11 server
    if POSIX and not OSX:
        unittest.main()


if __name__ == '__main__':
    main()
