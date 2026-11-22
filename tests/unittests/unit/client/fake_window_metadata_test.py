#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 kogeler
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

"""
Windows created without a client receive their metadata as a `typedict`
(`xpra.opengl.window.test_gl_client_window`), which compiled builds must
accept: Cython enforces an exact builtin type for a `dict` annotation.
"""

import unittest

from xpra.util.objects import typedict
from xpra.client.gui.fake_client import FakeWindowHost


class FakeWindowMetadataTest(unittest.TestCase):

    def test_make_window_accepts_typedict_metadata(self):
        host = FakeWindowHost()
        created = []

        class Window:
            def __init__(self, *args):
                created.append(args)

        metadata = typedict({"title": "probe", "has-alpha": False})
        window = host.make_window(Window, 7, (0, 0, 4, 4), metadata)
        self.assertEqual(len(created), 1)
        # the window receives its own typedict copy of the metadata:
        received = created[0][6]
        self.assertIsInstance(received, typedict)
        self.assertEqual(dict(received), dict(metadata))
        self.assertIs(host.get_subsystem("window")._id_to_window[7], window)


def main():
    unittest.main()


if __name__ == "__main__":
    main()
