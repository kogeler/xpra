#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 kogeler
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

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
    """Use the same installed Wayland development ABI as the native server."""
    protocols = subprocess.check_output(
        ("pkg-config", "--variable=pkgdatadir", "wayland-protocols"), text=True,
    ).strip()
    codes = []
    for name in ("xdg-shell", "viewporter"):
        protocol = Path(protocols) / "stable" / name / (name + ".xml")
        header = directory / (name + "-client-protocol.h")
        code = directory / (name + "-protocol.c")
        for kind, target in (("client-header", header), ("private-code", code)):
            subprocess.run(("wayland-scanner", kind, str(protocol), str(target)), check=True, timeout=30)
        codes.append(str(code))
    flags = shlex.split(subprocess.check_output(
        ("pkg-config", "--cflags", "--libs", "wayland-client"), text=True,
    ))
    client = directory / "subsurface-discovery-client"
    subprocess.run((
        "cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-I", str(directory),
        str(Path(__file__).with_name("subsurface_discovery_client.c")), *codes,
        "-o", str(client), *flags,
    ), check=True, timeout=30)
    return client


def native_probe(client: Path, mode: str, runtime: Path) -> None:
    # Run in a fresh interpreter: adjacent pure-Python subsystem tests replace
    # native module imports, and a native lifetime assertion must never inherit
    # their stand-ins or another compositor's strong-reference registry.
    os.environ.update(XDG_RUNTIME_DIR=str(runtime), XPRA_WAYLAND_GPU="no", WLR_RENDERER="pixman")
    from xpra.wayland.server.compositor import WaylandCompositor
    from xpra.wayland.server.wayland_surface import surfaces
    from xpra.wayland.server.models.window import Window
    from xpra.wayland.server.models.subsurface_window import SubsurfaceWindow
    from xpra.wayland.server.subsystem.window import WaylandWindowServer

    compositor = WaylandCompositor()
    roots = []
    children = []
    commits = []
    models = {}
    child_models = {}
    root_commits = []

    class ProbeWindowServer(WaylandWindowServer):
        __slots__ = ()

        def get_window(self, wid):
            return models.get(wid)

    server = ProbeWindowServer()

    def child_snapshot(wid, _root_wid, _mapped, _has_buffer, _tree, _damage,
                       image, width, height, *_rest):
        model = child_models[wid]
        model.replace_dimensions(width, height)
        if image is None:
            model.clear_image()
        else:
            model.replace_image_snapshot(image)

    def child_created(parent_wid, child, *_args):
        children.append((parent_wid, child))
        child.connect("new-subsurface", child_created)
        child.connect("subsurface-commit", lambda wid, *_: commits.append(wid))
        if mode == "pixels":
            child_models[child.wid] = SubsurfaceWindow(0, 0)
            child.connect("subsurface-commit", child_snapshot)

    def root_created(root, *_args):
        roots.append(root)
        root.connect("new-subsurface", child_created)
        if mode == "pixels":
            models[root.wid] = Window({
                "geometry": (0, 0, 4, 4), "image": None, "has-alpha": True, "surface": root,
            })
            root.connect("surface-snapshot", server.surface_snapshot)
            root.connect("commit", lambda _wid, mapped, size, damage, _tree:
                         root_commits.append((mapped, size, damage)))

    compositor.connect("new-surface", root_created)
    process = None
    try:
        socket = compositor.initialize()
        process = subprocess.Popen(
            (str(client), mode), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=dict(os.environ, WAYLAND_DISPLAY=socket),
        )
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)

            def wait_for(expected: bytes) -> None:
                deadline = monotonic() + 5
                while monotonic() < deadline:
                    compositor.process_events()
                    if selector.select(0.005):
                        line = process.stdout.readline()
                        assert line == expected + b"\n", (mode, expected, line)
                        return
                raise AssertionError(f"{mode}: timed out waiting for {expected!r}")

            wait_for(b"ready")
            assert len(roots) == 1, roots
            assert len(children) == 2, (mode, "native child discovery", children)
            direct = next(child for parent, child in children if parent == roots[0].wid)
            leaf = next(child for parent, child in children if parent == direct.wid)
            wrappers = (roots[0], direct, leaf)
            pointers = tuple(surface.wl_surface_ptr for surface in wrappers)
            assert len(set(pointers)) == 3 and all(pointers), pointers
            assert all(surfaces.get(pointer) is surface for pointer, surface in zip(pointers, wrappers))

            def advance(expected):
                process.stdin.write(b"\n")
                process.stdin.flush()
                wait_for(expected)

            if mode == "pixels":
                root = roots[0]
                model = models[root.wid]
                child_model = child_models[direct.wid]

                def check_frame(owner, alpha):
                    image = owner.get_image(0, 0, 4, 4)
                    assert image is not None and (image.get_width(), image.get_height()) == (4, 4), image
                    try:
                        assert ("A" in image.get_pixel_format()) == alpha, image
                        assert owner.get_property("frame-has-alpha") == alpha
                        assert owner.get_property("has-alpha") is True
                        first = bytes(image.get_pixels())[:4]
                        expected = b"\x33\x22\x11" if image.get_pixel_format().startswith("BGR") else b"\x11\x22\x33"
                        assert first[:3] == expected, (image.get_pixel_format(), first)
                    finally:
                        image.free()

                advance(b"pixels")
                check_frame(model, False)
                check_frame(child_model, True)
                assert root.source_format == 0x34325258  # DRM_FORMAT_XRGB8888
                # Synchronized child commit precedes its parent's first map;
                # visibility must not erase its attached-buffer identity.
                assert direct.source_format == 0x34325241, direct.source_format  # DRM_FORMAT_ARGB8888
                generation = model.get_snapshot_generation()
                advance(b"format")
                check_frame(model, True)
                check_frame(child_model, False)
                assert root.source_format == 0x34325241
                assert direct.source_format == 0x34325258, direct.source_format
                assert model.get_snapshot_generation() > generation
                assert root_commits[-1][2] == ((0, 0, 4, 4),), root_commits[-1]

                advance(b"oversized")
                assert root.get_size() == (65536, 65536)
                assert not model.has_image()
                try:
                    image = root.capture_logical_pixels()
                except ValueError as error:
                    assert "exceeds maximum size" in str(error), error
                else:
                    if image is not None:
                        image.free()
                    raise AssertionError("oversized viewport was not rejected before readback")
                advance(b"recovered")
                check_frame(model, True)

                original_set_image = model.set_image
                failures = []

                def fail_retention(_image):
                    failures.append(True)
                    raise MemoryError("injected native root retention failure")

                model.set_image = fail_retention
                try:
                    advance(b"failed-retain")
                finally:
                    model.set_image = original_set_image
                assert failures == [True], failures
                assert not model.has_image()
                advance(b"empty-recovered")
                check_frame(model, True)
                assert root_commits[-1][2] == ((0, 0, 4, 4),), root_commits[-1]
                advance(b"unmapped")
                assert not model.has_image() and not child_model.has_image()
                assert root.source_format is None and direct.source_format is None

            commits.clear()
            process.stdin.write(b"\n")
            process.stdin.flush()
            wait_for(b"recommitted")
            assert len(children) == 2, ("duplicate native child discovery", children)
            assert commits.count(direct.wid) == commits.count(leaf.wid) == 1, commits
            assert tuple(surface.wl_surface_ptr for surface in wrappers) == pointers
            process.stdin.write(b"\n")
            process.stdin.flush()
            wait_for(b"destroyed")
            assert all(pointer not in surfaces for pointer in pointers), surfaces
            assert all(surface.wl_surface_ptr == 0 for surface in wrappers), wrappers
            assert all(surface.source_format is None for surface in wrappers)
        returncode = process.wait(timeout=5)
        stderr = process.stderr.read().decode("utf-8", "replace")
        assert returncode == 0, (returncode, stderr)
        assert not stderr, stderr
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        compositor.cleanup()


class WaylandSubsurfaceDiscoveryTest(unittest.TestCase):

    def test_native_prebuilt_and_synchronized_trees(self):
        with tempfile.TemporaryDirectory(prefix="xpra-subsurface-discovery-") as name:
            directory = Path(name)
            client = compile_client(directory)
            for mode in ("prebuilt-child", "prebuilt-root", "synchronized", "pixels"):
                with self.subTest(mode=mode):
                    runtime = directory / mode
                    runtime.mkdir(mode=0o700)
                    completed = subprocess.run(
                        (sys.executable, __file__, "--native-probe", str(client), mode, str(runtime)),
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, timeout=20, check=False,
                    )
                    self.assertEqual(completed.returncode, 0, completed.stdout)


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "--native-probe":
        native_probe(Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4]))
    else:
        unittest.main()
