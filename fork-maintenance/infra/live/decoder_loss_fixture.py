#!/usr/bin/env python3
# Copyright (C) 2026 kogeler
"""Inject permanent live H.264 init failure without changing discovery/probes.

Only a separately named recovery scenario may use this launcher. Ordinary
hardware-positive peers must execute the unmodified installed Xpra launcher.
The actual client backing, retirement notification, transport, server selection
and native picture presentation remain production code.
"""

from dataclasses import replace
import json
import os
import sys
from threading import current_thread
from time import monotonic_ns


MARKER = "XPRA_DECODER_LOSS_FIXTURE "


def inject(helper_type, unsupported_error) -> None:
    original = helper_type.get_decoder_specs

    class RefusingDecoder:
        def init_context(self, encoding, width, height, colorspace, _options):
            if encoding != "h264" or current_thread().name != "decode":
                raise AssertionError("fault injection escaped the live H.264 decode boundary")
            print(MARKER + json.dumps({
                "schema": 1, "event": "injected-init-failure", "encoding": encoding,
                "width": width, "height": height, "colorspace": colorspace,
                "thread": current_thread().name, "pid": os.getpid(), "monotonic_ns": monotonic_ns(),
            }, sort_keys=True), flush=True)
            raise unsupported_error("live fixture permanently lost H.264 decoder")

        def clean(self):
            pass

    def decoder_specs(helper, encoding):
        specifications = original(helper, encoding)
        if encoding != "h264":
            return specifications
        if not specifications or any(spec.codec_type != "libva" for specs in specifications.values() for spec in specs):
            raise ValueError("decoder-loss scenario requires the real libva H.264 discovery result")
        # Discovery obtains fresh native module specs through a different API.
        # Do not mutate those objects or replace the codec module's Decoder:
        # doing that would make the startup self-test remove H.264 from hello.
        return {colorspace: [replace(spec, codec_class=RefusingDecoder, codec_args=()) for spec in specs]
                for colorspace, specs in specifications.items()}

    helper_type.get_decoder_specs = decoder_specs


def main() -> int:
    from xpra.codecs.constants import EncodingNotSupported
    from xpra.codecs.video import VideoHelper
    from xpra.platform import init, set_default_name

    inject(VideoHelper, EncodingNotSupported)
    set_default_name("Xpra")
    init()
    from xpra.scripts.main import main as xpra_main

    return xpra_main(sys.argv[0], sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
