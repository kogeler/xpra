# Copyright (C) 2026 kogeler
"""The injector must preserve discovery and leave every other codec alone."""

from contextlib import redirect_stdout
from dataclasses import dataclass
from io import StringIO
import json
from threading import Thread
import unittest

import decoder_loss_fixture as fixture


@dataclass
class Spec:
    codec_type: str = "libva"
    codec_class: type = object
    codec_args: tuple = ()


class DecoderLossFixtureTest(unittest.TestCase):
    def test_only_live_factory_changes_not_startup_discovery(self):
        specs = {"h264": {"YUV420P": [Spec()]}, "vp9": {"YUV420P": [Spec()]}}

        class Helper:
            def get_decoder_specs(self, encoding):
                return specs[encoding]

        class Unsupported(Exception):
            pass

        fixture.inject(Helper, Unsupported)
        helper = Helper()
        self.assertIs(helper.get_decoder_specs("vp9"), specs["vp9"])
        injected = helper.get_decoder_specs("h264")["YUV420P"][0]
        self.assertIs(specs["h264"]["YUV420P"][0].codec_class, object)
        decoder = injected.codec_class()
        with self.assertRaises(AssertionError):
            decoder.init_context("h264", 640, 480, "YUV420P", {})
        events, failures = StringIO(), []

        def decode():
            with redirect_stdout(events):
                try:
                    decoder.init_context("h264", 640, 480, "YUV420P", {})
                except Unsupported as error:
                    failures.append(error)

        worker = Thread(target=decode, name="decode")
        worker.start()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(failures), 1)
        raw = events.getvalue()
        self.assertTrue(raw.startswith(fixture.MARKER))
        event = json.loads(raw.removeprefix(fixture.MARKER))
        self.assertEqual((event["event"], event["thread"], event["width"]), ("injected-init-failure", "decode", 640))
        decoder.clean()

    def test_missing_or_other_h264_decoder_cannot_fake_the_setup(self):
        for specifications in ({}, {"YUV420P": [Spec(codec_type="other")]}):
            class Helper:
                def get_decoder_specs(self, _encoding):
                    return specifications
            fixture.inject(Helper, RuntimeError)
            with self.assertRaises(ValueError):
                Helper().get_decoder_specs("h264")


if __name__ == "__main__":
    unittest.main()
