# This file is part of Xpra.
# Copyright (C) 2026 kogeler
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

"""Deferred debug captures tolerate absent buffers and still write real PNGs."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from PIL import Image

from xpra.codecs.image import ImageWrapper
from xpra.server.window.compress import WindowSource


class DebugSnapshotTest(unittest.TestCase):
    @staticmethod
    def source(window, directory):
        # This callback only consumes these existing source fields; no worker,
        # timer or new private lifecycle API is substituted by this fixture.
        source = WindowSource.__new__(WindowSource)
        source.window = window
        source.window_dimensions = (64, 64)
        source.screen_updates_directory = str(directory)
        return source

    @staticmethod
    def image(size):
        return ImageWrapper(0, 0, size, size, bytes((10, 20, 30)) * size * size,
                            "RGB", 24, size * 3)

    def test_unavailable_or_terminal_window_has_no_debug_capture(self):
        for window in (
                None,
                SimpleNamespace(get_dimensions=lambda: (2, 2), get_image=lambda *_args: None),
        ):
            with self.subTest(window=window), tempfile.TemporaryDirectory() as directory:
                self.source(window, directory).save_screenshot()
                self.assertFalse((Path(directory) / "screenshot.png").exists())

    def test_empty_current_geometry_does_not_request_an_image(self):
        window = SimpleNamespace(get_dimensions=lambda: (0, 0), get_image=Mock())
        with tempfile.TemporaryDirectory() as directory:
            self.source(window, directory).save_screenshot()
        window.get_image.assert_not_called()

    def test_current_geometry_writes_real_pixels_without_freeing_the_model_image(self):
        image = self.image(2)
        window = SimpleNamespace(get_dimensions=lambda: (2, 2), get_image=image.get_sub_image)
        try:
            with tempfile.TemporaryDirectory() as directory:
                self.source(window, directory).save_screenshot()
                with Image.open(Path(directory) / "screenshot.png") as captured:
                    self.assertEqual(captured.size, (2, 2))
                    self.assertEqual(captured.getpixel((0, 0)), (10, 20, 30))
                self.assertFalse(image.freed)
        finally:
            image.free()

    def test_temporarily_smaller_buffer_recovers_on_a_later_capture(self):
        small, current = self.image(2), self.image(3)
        retained = [small]
        window = SimpleNamespace(
            get_dimensions=lambda: (3, 3),
            get_image=lambda *args: retained[0].get_sub_image(*args),
        )
        try:
            with tempfile.TemporaryDirectory() as directory:
                source = self.source(window, directory)
                source.save_screenshot()
                self.assertFalse((Path(directory) / "screenshot.png").exists())
                retained[0] = current
                source.save_screenshot()
                with Image.open(Path(directory) / "screenshot.png") as captured:
                    self.assertEqual(captured.size, (3, 3))
                    self.assertEqual(captured.getpixel((2, 2)), (10, 20, 30))
        finally:
            small.free()
            current.free()


if __name__ == "__main__":
    unittest.main()
