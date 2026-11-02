# This file is part of Xpra.
# Copyright (C) 2026 kogeler
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

"""Encode real RGB packets across a controlled registry reconfiguration."""

import unittest
from threading import Event, Thread
from time import monotonic
from types import SimpleNamespace
from unittest.mock import patch

from xpra.codecs.image import ImageWrapper
from xpra.codecs.loader import load_codec
from xpra.server.source.source_stats import GlobalPerformanceStatistics
from xpra.server.window import compress
from xpra.server.window.batch_config import DamageBatchConfig
from xpra.server.window.compress import WindowSource
from xpra.server.window.video_compress import WindowVideoSource
from xpra.util.objects import typedict


class EncoderRegistryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if load_codec("enc_rgb") is None:
            raise RuntimeError("the real RGB encoder is required")

    @staticmethod
    def make_source(source_type):
        values = {"depth": 32}
        window = SimpleNamespace(
            is_OR=lambda: False, is_tray=lambda: False, is_shadow=lambda: False,
            has_alpha=lambda: True, get=values.get, get_property=values.__getitem__,
            get_dynamic_property_names=lambda: (), get_internal_property_names=lambda: (),
        )
        source = source_type(
            16, 16, lambda *_args: None, lambda: 0, lambda *_args: None,
            lambda *_args: None, GlobalPerformanceStatistics(), 7, window,
            DamageBatchConfig(), 0, False, 0,
            SimpleNamespace(get_encodings=lambda: (), get_gpu_encodings=dict), None,
            ("rgb24", "rgb32"), ("rgb",), "rgb", ("rgb",), ("rgb24", "rgb32"), (),
            typedict(), typedict(), ("BGRA", "RGBA"), typedict({"quality": 100, "speed": 100}),
            None, 0, 0,
        )
        source.init_encoders()
        return source

    @staticmethod
    def image():
        pixels = bytes(range(256)) * 4
        return ImageWrapper(0, 0, 16, 16, pixels, "BGRA", 32, 64)

    @staticmethod
    def encode(source, image):
        now = monotonic()
        return source.make_data_packet(now, now, image, "rgb32", 1,
                                       typedict({"quality": 100, "speed": 100}), 0)

    @staticmethod
    def worker(callback, errors, finished):
        def run():
            try:
                callback()
            except BaseException as error:
                errors.append(error)
            finally:
                finished.set()
        thread = Thread(target=run, daemon=True)
        thread.start()
        return thread

    def test_queued_rgb_frame_survives_registry_rebuild(self):
        for source_type in (WindowSource, WindowVideoSource):
            with self.subTest(source_type=source_type.__name__):
                source = self.make_source(source_type)
                image = self.image()
                paused, release, rebuilt, encoded, started = (Event() for _ in range(5))
                errors, packets = [], []
                get_codec = compress.get_codec

                def paused_codec(name):
                    if name == "enc_rgb":
                        paused.set()
                        if not release.wait(5):
                            raise RuntimeError("registry rebuild was not released")
                    return get_codec(name)

                def encode():
                    started.set()
                    packets.append(self.encode(source, image))

                with patch.object(compress, "get_codec", side_effect=paused_codec):
                    rebuild = self.worker(source.init_encoders, errors, rebuilt)
                    encoder = None
                    try:
                        self.assertTrue(paused.wait(5))
                        encoder = self.worker(encode, errors, encoded)
                        self.assertTrue(started.wait(5))
                        # Give the worker the controlled lookup opportunity;
                        # either a consistent snapshot or waiting is valid.
                        encoded.wait(0.1)
                    finally:
                        release.set()
                        rebuild.join(5)
                        if encoder is not None:
                            encoder.join(5)
                        image.free()
                self.assertTrue(rebuilt.is_set())
                self.assertTrue(encoded.is_set())
                self.assertEqual(errors, [])
                self.assertEqual(len(packets), 1)
                self.assertIsNotNone(packets[0])
                self.assertEqual(packets[0][6], "rgb32")
                self.assertGreater(len(packets[0][7]), 0)

    def test_registry_rebuild_does_not_wait_for_encoder_execution(self):
        source = self.make_source(WindowVideoSource)
        entered, release, encoded, rebuilt = (Event() for _ in range(4))
        errors, packets = [], []
        real_encoder = source._encoders["rgb32"]
        image = self.image()

        def held_encoder(*args):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("encoder execution was not released")
            return real_encoder(*args)

        source._encoders["rgb32"] = held_encoder
        encoder = self.worker(lambda: packets.append(self.encode(source, image)), errors, encoded)
        rebuild = None
        try:
            self.assertTrue(entered.wait(5))
            rebuild = self.worker(source.init_encoders, errors, rebuilt)
            self.assertTrue(rebuilt.wait(2), "registry lock was held during encoder execution")
        finally:
            release.set()
            encoder.join(5)
            if rebuild is not None:
                rebuild.join(5)
            image.free()
        self.assertTrue(encoded.is_set())
        self.assertEqual(errors, [])
        self.assertEqual(len(packets), 1)
        self.assertIsNotNone(packets[0])


if __name__ == "__main__":
    unittest.main()
