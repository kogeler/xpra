#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 kogeler
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import json
import os
from pathlib import Path
import selectors
import shlex
import subprocess
import sys
import tempfile
from time import monotonic
import unittest


def compile_client(directory: Path) -> Path:
    protocols = subprocess.check_output(
        ("pkg-config", "--variable=pkgdatadir", "wayland-protocols"), text=True, timeout=30,
    ).strip()
    protocol = Path(protocols) / "stable" / "xdg-shell" / "xdg-shell.xml"
    code = directory / "xdg-shell-protocol.c"
    for kind, target in (("client-header", directory / "xdg-shell-client-protocol.h"), ("private-code", code)):
        subprocess.run(("wayland-scanner", kind, str(protocol), str(target)), check=True, timeout=30)
    flags = shlex.split(subprocess.check_output(
        ("pkg-config", "--cflags", "--libs", "wayland-client"), text=True, timeout=30,
    ))
    client = directory / "pointer-scroll-client"
    subprocess.run((
        "cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-I", str(directory),
        str(Path(__file__).with_name("pointer_scroll_client.c")), str(code), "-o", str(client), *flags,
    ), check=True, timeout=30)
    return client


def native_probe(client: Path, version: int, runtime: Path) -> dict:
    # A fresh interpreter prevents adjacent subsystem tests' mocked imports or
    # an earlier compositor's native registry from replacing this real boundary.
    os.environ.update(XDG_RUNTIME_DIR=str(runtime), XPRA_WAYLAND_GPU="no", WLR_RENDERER="pixman")
    from xpra.wayland.server.compositor import WaylandCompositor

    compositor = WaylandCompositor()
    roots = []
    compositor.connect("new-surface", lambda root, *_args: roots.append(root))
    process = device = None
    observations = {}
    try:
        socket = compositor.initialize()
        device = compositor.get_pointer_device()
        process = subprocess.Popen(
            (str(client), str(version)), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=dict(os.environ, WAYLAND_DISPLAY=socket), bufsize=0,
        )
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, "client")
            selector.register(compositor.get_event_loop_fd(), selectors.EVENT_READ, "server")
            pending = bytearray()

            def receive(marker: bytes) -> list:
                events = []
                deadline = monotonic() + 5
                while monotonic() < deadline:
                    compositor.process_events()
                    while b"\n" in pending:
                        line, _, tail = pending.partition(b"\n")
                        pending[:] = tail
                        if line == marker:
                            return events
                        events.append(json.loads(line))
                    for key, _mask in selector.select(max(0, deadline - monotonic())):
                        if key.data == "client":
                            chunk = os.read(process.stdout.fileno(), 65536)
                            if not chunk:
                                raise AssertionError(f"pointer consumer closed before {marker!r}")
                            pending.extend(chunk)
                raise AssertionError(f"timed out waiting for pointer consumer {marker!r}")

            def synchronize() -> list:
                compositor.flush()
                process.stdin.write(b"\n")
                return receive(b"sync")

            assert receive(b"ready") == []
            assert len(roots) == 1, roots
            assert device.enter_surface(roots[0].xdg_surface_ptr, 10, 10)
            observations["enter"] = synchronize()
            for button in (4, 5, 6, 7):
                device.click(button, True, {})
                observations[f"press-{button}"] = synchronize()
                device.click(button, False, {})
                observations[f"release-{button}"] = synchronize()
                distances = (-2, -1, 1, 2) if version == 5 else (-2, -1, -0.25, 0.25, 1, 2)
                for distance in distances:
                    device.wheel_motion(button, distance)
                    observations[f"motion-{button}-{distance}"] = synchronize()
            device.wheel_motion(1, 1)
            observations["unsupported-wheel"] = synchronize()
            for pressed in (True, False):
                device.click(1, pressed, {})
                observations[f"button-{pressed}"] = synchronize()
            process.stdin.write(b"q")
            deadline = monotonic() + 5
            while process.poll() is None and monotonic() < deadline:
                compositor.process_events()
                selector.select(0.01)
            assert process.wait(timeout=1) == 0
            stderr = process.stderr.read().decode("utf-8", "replace")
            assert not stderr, stderr
    finally:
        try:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)
        finally:
            try:
                if device is not None:
                    device.cleanup()
            finally:
                compositor.cleanup()
    return observations


class WaylandPointerScrollTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.observations = {}
        with tempfile.TemporaryDirectory(prefix="xpra-pointer-scroll-") as name:
            directory = Path(name)
            client = compile_client(directory)
            for version in (5, 8):
                runtime = directory / str(version)
                runtime.mkdir(mode=0o700)
                completed = subprocess.run(
                    (sys.executable, __file__, "--native-probe", str(client), str(version), str(runtime)),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=30, check=False,
                    env=dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path)),
                )
                if completed.returncode:
                    raise AssertionError(completed.stdout)
                lines = [line.removeprefix("observations=") for line in completed.stdout.splitlines()
                         if line.startswith("observations=")]
                if len(lines) != 1:
                    raise AssertionError(completed.stdout)
                cls.observations[version] = json.loads(lines[0])

    def assert_axis(self, events, version, button, distance=1):
        # Independent protocol oracle: 0 is vertical, 1 horizontal; Wayland
        # positive axis means down/right. One click is 15 surface units / 120.
        orientation = 0 if button in (4, 5) else 1
        direction = -1 if button in (4, 6) else 1
        self.assertEqual([event for event in events if event[0] == "axis"],
                         [["axis", orientation, direction * distance * 15]])
        wheel_event = "discrete" if version == 5 else "value120"
        scale = 1 if version == 5 else 120
        self.assertEqual([event for event in events if event[0] == wheel_event],
                         [[wheel_event, orientation, direction * distance * scale]])
        self.assertEqual(events.count(["source", 0]), 1, events)
        self.assertEqual(events[-1], ["frame"], events)
        self.assertEqual(len(events), 4, events)

    def test_discrete_press_and_release(self):
        for version, observations in self.observations.items():
            self.assertIn(["enter"], observations["enter"])
            for button in (4, 5, 6, 7):
                with self.subTest(version=version, button=button):
                    self.assert_axis(observations[f"press-{button}"], version, button)
                    self.assertEqual(observations[f"release-{button}"], [])

    def test_smooth_distance_and_mapped_direction(self):
        for version, observations in self.observations.items():
            for key, events in observations.items():
                if not key.startswith("motion-"):
                    continue
                _prefix, button, distance = key.split("-", 2)
                with self.subTest(version=version, button=button, distance=distance):
                    self.assert_axis(events, version, int(button), abs(float(distance)))

    def test_unsupported_wheel_and_valid_button_tail(self):
        for version, observations in self.observations.items():
            with self.subTest(version=version):
                self.assertEqual(observations["unsupported-wheel"], [])
                self.assertEqual(observations["button-True"], [["button", 272, 1], ["frame"]])
                self.assertEqual(observations["button-False"], [["button", 272, 0], ["frame"]])


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "--native-probe":
        result = native_probe(Path(sys.argv[2]), int(sys.argv[3]), Path(sys.argv[4]))
        print("observations=" + json.dumps(result))
    else:
        unittest.main()
