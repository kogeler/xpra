#!/usr/bin/env python3
# Copyright (C) 2026 kogeler
"""Physical installed-codec regression; no display/session or software fallback.

Run only inside a named complete-stack live endpoint. This complements, never
replaces, the transported application's VA/pixel/presentation evidence.
"""

import argparse
from errno import EPERM
import json
import os
from threading import Thread, get_native_id


def probe(action: str) -> dict:
    # Configure before importing the filter, just as the actual CLI does.
    from xpra.scripts.config import get_default_seccomp
    from xpra.scripts.main import configure_seccomp
    configure_seccomp(get_default_seccomp())
    from xpra.seccomp import draw, is_available
    from xpra.codecs.libva import decoder
    from xpra.codecs.checks import TEST_COMPRESSED_DATA
    from xpra.util.objects import typedict

    if not is_available() or draw.get_action() != "errno":
        raise RuntimeError("native decode filter/default errno policy is unavailable")
    frames = TEST_COMPRESSED_DATA["h264"]["YUV420P"][(128, 128)]
    if not frames:
        raise RuntimeError("physical H.264 regression has no compressed frames")
    outcome: dict = {"schema": 1, "action": action, "default_action": draw.get_action()}
    errors = []

    def worker():
        contexts = []
        try:
            decoder.init_module()
            outcome["driver"] = decoder.get_info()
            if "h264" not in decoder.get_encodings():
                raise RuntimeError("physical libva subject lacks H.264")
            # Match the real loader's prefilter self-test, then require fresh
            # contexts and the complete decode path *after* filter installation.
            warm = decoder.Decoder()
            contexts.append(warm)
            warm.init_context("h264", 128, 128, "YUV420P", typedict())
            image = warm.decompress_image(frames[0][0], typedict(frames[0][1]))
            image.free()
            warm.clean()
            contexts.clear()
            os.environ[draw.ACTION_ENV] = action
            if not draw.install_thread():
                raise RuntimeError("decode filter was not installed")
            outcome["tid"] = get_native_id()
            try:
                fd = os.open("/dev/null", os.O_RDONLY)
            except PermissionError as exc:
                if exc.errno != EPERM or action != "errno":
                    raise
                outcome["open_denied"] = True
            else:
                os.close(fd)
                if action != "log":
                    raise RuntimeError("installed decode filter allowed file access")
                outcome["open_denied"] = False
            decoded = 0
            for _ in range(2):
                context = decoder.Decoder()
                contexts.append(context)
                context.init_context("h264", 128, 128, "YUV420P", typedict())
                for data, options in frames:
                    image = context.decompress_image(data, typedict(options))
                    try:
                        if (image.get_width(), image.get_height(), image.get_pixel_format()) != (128, 128, "NV12"):
                            raise RuntimeError("libva produced invalid frame metadata")
                        if not all(image.get_pixels()):
                            raise RuntimeError("libva returned an empty frame plane")
                    finally:
                        image.free()
                    decoded += 1
            # UI module cleanup can precede queued/late window decoder clean.
            decoder.cleanup_module()
            for context in contexts:
                context.clean()
            contexts.clear()
            outcome["decoded_frames"] = decoded
            outcome["late_cleanup"] = True
        except BaseException as exc:
            errors.append(exc)
        finally:
            for context in contexts:
                context.clean()
            decoder.cleanup_module()

    thread = Thread(target=worker, name="libva-seccomp-probe", daemon=True)
    thread.start()
    thread.join(30)
    if thread.is_alive():
        raise RuntimeError("physical filtered decoder did not finish")
    if errors:
        raise errors[0]
    if outcome.get("decoded_frames") != 2 * len(frames):
        raise RuntimeError("incomplete physical decode regression")
    return outcome


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("errno", "log"))
    args = parser.parse_args()
    print("libva-seccomp-probe=" + json.dumps(probe(args.action), sort_keys=True))


if __name__ == "__main__":
    main()
