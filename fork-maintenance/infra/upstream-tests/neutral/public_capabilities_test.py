#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 kogeler
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

"""Check public startup probes without substituting their window or renderer."""

import subprocess
import unittest

from unit.process_test_util import ProcessTestUtil


class PublicCapabilitiesTest(ProcessTestUtil):
    def command(self, *args, env=None):
        result = subprocess.run(
            [*self.get_xpra_cmd(), *args],
            env=env if env is not None else self.get_run_env(),
            capture_output=True, text=True, timeout=45, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_client_and_server_help_expose_core_startup_options(self):
        for mode, options in (
            ("attach", ("--ssh", "--opengl", "--video-decoders", "--clipboard-direction")),
            ("seamless", ("--start-child", "--start-child-after-connect",
                          "--exit-with-children", "--terminate-children")),
        ):
            with self.subTest(mode=mode):
                result = self.command(mode, "--help")
                help_text = (result.stdout + result.stderr).replace("_", "-")
                for option in options:
                    self.assertIn(option, help_text)

    def test_public_opengl_check_completes_a_real_test_window_draw(self):
        xvfb = self.start_Xvfb()
        try:
            environment = dict(self.get_run_env(), DISPLAY=xvfb.display,
                               GDK_BACKEND="x11", XDG_SESSION_TYPE="x11",
                               LIBGL_ALWAYS_SOFTWARE="1")
            environment.pop("WAYLAND_DISPLAY", None)
            # Only bypass the software-driver policy: the real GL context,
            # FakeClient, window/backing factory and draw must still succeed.
            result = self.command("opengl", "--opengl=force:native", env=environment)
            properties = {}
            for line in result.stdout.splitlines():
                key, separator, value = line.partition("=")
                if separator:
                    properties[key.strip()] = value.strip()
            diagnostic = result.stdout + result.stderr
            self.assertEqual(properties.get("success", "").lower(), "true", diagnostic)
            self.assertEqual(properties.get("safe", "").lower(), "true", diagnostic)
            self.assertIn("opengl", properties, diagnostic)
        finally:
            xvfb.terminate()
            xvfb.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
