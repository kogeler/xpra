#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 kogeler
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

"""Compile the real decoder C, replace only VA calls, enforce real file denial.

No GPU is required here. Full decode/pixels and the Cython wrapper belong to
the physical complete-stack live profiles, not this deterministic driver.
"""

import ctypes
import os
from pathlib import Path
import shlex
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


def exercise(library_path, scenario):
    library = ctypes.CDLL(library_path)
    library.libva_decoder_create.argtypes = (
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_char_p,
        ctypes.c_int, ctypes.c_int, ctypes.c_char_p,
    )
    library.libva_decoder_destroy.argtypes = (ctypes.c_void_p,)
    library.libva_decode_get_device.restype = ctypes.c_char_p
    library.libva_decode_get_vendor.restype = ctypes.c_char_p
    os.environ["XPRA_LIBVA_DEVICE"] = "/dev/null"
    assert library.libva_decode_startup() == 0
    assert library.libva_decode_supports(b"h264", b"YUV420P") == 1

    def create(expected=0):
        context = ctypes.c_void_p()
        status = library.libva_decoder_create(ctypes.byref(context), b"h264", 128, 128, b"YUV420P")
        assert status == expected, ("decoder create", status, expected)
        assert bool(context.value) == (expected == 0)
        return context

    if scenario in ("errno", "log"):
        from xpra.seccomp import install_filter
        from xpra.seccomp.draw import DECODE_SYSCALLS
        install_filter(DECODE_SYSCALLS, scenario)
        context = create()
        library.libva_decoder_destroy(context)
        try:
            fd = os.open("/dev/null", os.O_RDONLY)
        except PermissionError as exc:
            assert scenario == "errno" and exc.errno == 1
        else:
            os.close(fd)
            assert scenario == "log"
    elif scenario == "shared":
        first, second = create(), create()
        assert library.test_opened() == 1, "per-decoder display reopened after preload"
        library.libva_decoder_destroy(first)
        assert library.test_terminated() == 0, "first decoder terminated shared display"
        library.libva_decoder_destroy(second)
        assert library.test_terminated() == 0, "module lost its display lease"
    elif scenario == "retired":
        first, second = create(), create()
        library.libva_decode_shutdown()
        assert library.test_terminated() == 0, "shutdown invalidated live decoder resources"
        create(-2)
        assert library.test_opened() == 1, "decoder resurrected a retired module"
        assert library.libva_decode_supports(b"h264", b"YUV420P") == 0
        assert library.libva_decode_get_device() == b""
        assert library.libva_decode_get_vendor() == b""
        # Explicit unfiltered module init may publish a fresh generation; old
        # decoder clean must release only its own generation, in either order.
        assert library.libva_decode_startup() == 0
        third = create()
        library.libva_decoder_destroy(second)
        library.libva_decoder_destroy(first)
        assert library.test_terminated() == 1
        library.libva_decode_shutdown()
        library.libva_decoder_destroy(third)
        assert library.test_terminated() == 2
        return
    elif scenario == "partial":
        for stage in (1, 2, 3):
            library.test_failure(stage)
            create(-1)
            assert library.test_terminated() == 0, ("failed setup lost module display", stage)
            library.test_failure(0)
            library.libva_decoder_destroy(create())
        assert library.test_opened() == 1
    elif scenario == "reprobe":
        library.libva_decode_shutdown()
        library.test_failure(4)
        assert library.libva_decode_startup() == -2
        assert library.libva_decode_supports(b"h264", b"YUV420P") == 0
        assert library.libva_decode_get_vendor() == b""
        create(-2)
        library.test_failure(0)
        library.libva_decode_shutdown()
        assert library.libva_decode_startup() == 0
        library.libva_decoder_destroy(create())
    else:
        raise AssertionError(scenario)
    library.libva_decode_shutdown()
    assert library.test_terminated() == library.test_opened(), "VA display leak"
    library.libva_decode_shutdown()
    assert library.test_terminated() == library.test_opened(), "double shutdown"


class LibVADisplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = Path(__file__).resolve().parents[4]
        scratch = cls.source / "build"
        scratch.mkdir(exist_ok=True)
        cls.temporary = TemporaryDirectory(prefix="libva-display-test-", dir=scratch)
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.library = str(Path(cls.temporary.name) / "decoder-test.so")
        flags = subprocess.check_output(
            ("pkg-config", "--cflags", "--libs", "libva", "libva-drm"), text=True,
        )
        command = (
            "cc", "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-fPIC", "-shared",
            "-Wl,-Bsymbolic-functions", "-o", cls.library,
            str(cls.source / "xpra/codecs/libva/va_decode.c"),
            str(Path(__file__).with_suffix(".c")), *shlex.split(flags),
        )
        subprocess.run(command, check=True, timeout=60)

    def run_scenario(self, scenario):
        # A filter cannot be removed: isolate every scenario in its own process.
        result = subprocess.run(
            (sys.executable, str(Path(__file__).resolve()), "--exercise", self.library, scenario),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_real_errno_filter(self):
        self.run_scenario("errno")

    def test_log_filter_control(self):
        self.run_scenario("log")

    def test_multiple_decoders_share_module_display(self):
        self.run_scenario("shared")

    def test_retired_display_and_late_cleanup(self):
        self.run_scenario("retired")

    def test_partial_initialization_releases_only_decoder_lease(self):
        self.run_scenario("partial")

    def test_failed_reprobe_clears_capabilities(self):
        self.run_scenario("reprobe")


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--exercise":
        exercise(sys.argv[2], sys.argv[3])
    else:
        unittest.main()
