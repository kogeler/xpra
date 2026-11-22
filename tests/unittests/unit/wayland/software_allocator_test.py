#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 kogeler
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

"""
Software-rendered output resizes must not leave the pixman renderer with
render buffers whose shared memory has already been freed.

With the wlroots 0.19 shm allocator, every output buffer replaced by a mode
change is freed without its destroy signal; destroying the pixman renderer
then writes into that memory, which the interpreter has meanwhile reused.
glibc's checking allocator turns the corruption into an abort at the latest
when those objects are released.
"""

import glob
import os
import subprocess
import sys
import tempfile
import unittest

PROBE = r"""
import gc
from xpra.wayland.server.compositor import WaylandCompositor

compositor = WaylandCompositor()
outputs = []
compositor.connect("new-output", outputs.append)
compositor.add_socket()
compositor.start_backend()
compositor.flush()
assert outputs, "no output"
output = outputs[0]
held = []
for width, height in ((1280, 1024), (1600, 900), (1024, 768), (1920, 1200), (800, 600), (1366, 768)) * 2:
    assert output.resize(width, height) == (width, height)
    compositor.flush()
    held.append([bytearray(300) for _ in range(2000)])
held.append([{"k%i" % i: bytearray(i % 3000 + 1), "t": (i, str(i)), "l": [i] * (i % 50)} for i in range(20000)])
compositor.cleanup()
del held
gc.collect()
print("software allocator teardown complete", flush=True)
"""

ROUNDS = 3


def find_malloc_debug() -> str:
    for pattern in ("/usr/lib/*/libc_malloc_debug.so.0", "/lib/*/libc_malloc_debug.so.0",
                    "/usr/lib*/libc_malloc_debug.so.0", "/lib*/libc_malloc_debug.so.0"):
        found = sorted(glob.glob(pattern))
        if found:
            return found[0]
    return ""


class SoftwareAllocatorTest(unittest.TestCase):

    def test_resized_software_output_teardown(self):
        malloc_debug = find_malloc_debug()
        self.assertTrue(malloc_debug, "glibc libc_malloc_debug.so.0 is required for this native test")
        for attempt in range(ROUNDS):
            with tempfile.TemporaryDirectory(prefix="xpra-wayland-sw-") as runtime:
                os.chmod(runtime, 0o700)
                env = dict(os.environ)
                env.update({
                    "XDG_RUNTIME_DIR": runtime,
                    "XPRA_WAYLAND_GPU": "no",
                    "WLR_RENDERER": "pixman",
                    "LD_PRELOAD": malloc_debug,
                    "MALLOC_CHECK_": "3",
                    "PYTHONMALLOC": "malloc",
                    "PYTHONFAULTHANDLER": "1",
                })
                env.pop("WAYLAND_DISPLAY", None)
                proc = subprocess.run([sys.executable, "-c", PROBE], env=env, capture_output=True,
                                      text=True, timeout=120, check=False)
            output = proc.stdout + proc.stderr
            self.assertEqual(proc.returncode, 0,
                             f"attempt {attempt + 1}: probe exited with {proc.returncode}:\n{output[-4000:]}")
            self.assertIn("software allocator teardown complete", proc.stdout, output[-4000:])


def main():
    unittest.main()


if __name__ == "__main__":
    main()
