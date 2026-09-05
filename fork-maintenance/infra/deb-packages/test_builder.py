#!/usr/bin/env python3
# Copyright (C) 2026 kogeler
"""Unit tests for DEB source-tree preparation."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import patch

import builder


class SelectionCacheTests(unittest.TestCase):
    def cache_fixture(self, root: Path) -> tuple[Path, Path, Path, Path, str]:
        payload = root / "payload"
        lab = payload / "lab"
        selected = lab / "stacks" / "develop.toml"
        selected.parent.mkdir(mode=0o700, parents=True)
        for directory in (payload, lab, selected.parent):
            directory.chmod(0o700)
        selected.write_text("schema = 1\n", encoding="utf-8")
        selected.chmod(0o600)
        bundle = payload / "source.bundle"
        bundle.write_bytes(b"bundle")
        bundle.chmod(0o600)
        with patch.object(builder, "LAB", lab):
            tree_sha256 = builder.selection_tree_sha256(lab)
        state = payload / "selection.json"
        state.write_text(
            json.dumps(
                {
                    "owner": "xpra-deb-selection-cache",
                    "schema": 1,
                    "selection": builder.ACTIVE_SELECTION,
                    "selection_sha256": "a" * 64,
                    "snapshot_tree_sha256": tree_sha256,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        state.chmod(0o600)
        return payload, lab, bundle, state, builder.sha256_file(state)

    def test_accepts_exact_selection_cache_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            payload, lab, bundle, state, cache_sha256 = self.cache_fixture(Path(raw))
            with (
                patch.object(builder, "PAYLOAD", payload),
                patch.object(builder, "LAB", lab),
                patch.object(builder, "SOURCE_BUNDLE", bundle),
                patch.object(builder, "SELECTION_STATE", state),
            ):
                builder.validate_selection_cache(
                    builder.ACTIVE_SELECTION,
                    "a" * 64,
                    cache_sha256,
                )

    def test_rejects_selection_tree_mutation_after_cache_freeze(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            payload, lab, bundle, state, cache_sha256 = self.cache_fixture(Path(raw))
            changed = lab / "stacks" / "develop.toml"
            changed.write_text("schema = 2\n", encoding="utf-8")
            changed.chmod(0o600)
            with (
                patch.object(builder, "PAYLOAD", payload),
                patch.object(builder, "LAB", lab),
                patch.object(builder, "SOURCE_BUNDLE", bundle),
                patch.object(builder, "SELECTION_STATE", state),
                self.assertRaisesRegex(builder.BuildFailure, "tree digest does not match"),
            ):
                builder.validate_selection_cache(
                    builder.ACTIVE_SELECTION,
                    "a" * 64,
                    cache_sha256,
                )


class DebianTreeTests(unittest.TestCase):
    def source_tree(
        self,
        root: Path,
        *,
        target: str = "packaging/debian/xpra",
        changelog_version: str = "6.6-1",
    ) -> Path:
        package_root = root / "packaging" / "debian" / "xpra"
        package_root.mkdir(parents=True)
        (package_root / "control").write_text(
            "Build-Depends: base\n#resolute:              ,extra\n\nPackage: xpra\n",
            encoding="utf-8",
        )
        (package_root / "changelog").write_text(
            f"xpra ({changelog_version}) UNRELEASED; urgency=low\n\n",
            encoding="utf-8",
        )
        os.symlink(target, root / "debian")
        return root

    def test_accepts_the_existing_upstream_packaging_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source = self.source_tree(Path(raw))
            with patch.object(builder, "SOURCE", source):
                version = builder.prepare_debian_tree("resolute", "6.6", 42479)
            self.assertEqual(version, "6.6-r42479-1")
            self.assertEqual(os.readlink(source / "debian"), "packaging/debian/xpra")
            control = (source / "debian" / "control").read_text(encoding="utf-8")
            self.assertIn("#resolute:\n              ,extra", control)
            self.assertEqual(
                (source / "debian" / "changelog").read_text(encoding="utf-8"),
                "xpra (6.6-r42479-1) UNRELEASED; urgency=low\n\n",
            )

    def test_rejects_a_different_packaging_link(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source = self.source_tree(Path(raw), target="elsewhere")
            with (
                patch.object(builder, "SOURCE", source),
                self.assertRaisesRegex(builder.BuildFailure, "unexpected target"),
            ):
                builder.prepare_debian_tree("resolute", "6.6", 42479)

    def test_rejects_a_mismatched_or_hyphenated_changelog_version(self) -> None:
        for changelog_version in ("6.7-1", "6.6-1-extra"):
            with self.subTest(version=changelog_version), tempfile.TemporaryDirectory() as raw:
                source = self.source_tree(
                    Path(raw),
                    changelog_version=changelog_version,
                )
                with (
                    patch.object(builder, "SOURCE", source),
                    self.assertRaisesRegex(
                        builder.BuildFailure,
                        "unexpected Debian changelog header",
                    ),
                ):
                    builder.prepare_debian_tree("resolute", "6.6", 42479)


class PackagingShimTests(unittest.TestCase):
    def test_builds_and_installs_the_two_upstream_dependency_shims(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw)
            for name in ("libcuda1", "libnvidia-fbc1"):
                control = source / "packaging" / "debian" / name / "DEBIAN" / "control"
                control.parent.mkdir(parents=True)
                control.write_text(f"Package: {name}\n", encoding="utf-8")
                control.parent.parent.chmod(0o700)
                control.parent.chmod(0o700)
                control.chmod(0o600)
            with (
                patch.object(builder, "SOURCE", source),
                patch.object(builder, "BUILD_DEPENDENCIES", source / "build-dependencies"),
                patch.object(builder, "run") as run_command,
            ):
                builder.install_packaging_shims()
            self.assertEqual(run_command.call_count, 4)
            build_commands = run_command.call_args_list[::2]
            self.assertTrue(
                all(
                    call.args[0][-1].startswith(str(source / "build-dependencies") + "/")
                    for call in build_commands
                )
            )
            for name in ("libcuda1", "libnvidia-fbc1"):
                root = source / "packaging" / "debian" / name
                self.assertEqual(root.stat().st_mode & 0o777, 0o755)
                self.assertEqual((root / "DEBIAN").stat().st_mode & 0o777, 0o755)
                self.assertEqual((root / "DEBIAN/control").stat().st_mode & 0o777, 0o644)
            self.assertEqual(
                [call.args[0][:2] for call in run_command.call_args_list],
                [
                    ["dpkg-deb", "--build"],
                    ["dpkg", "--install"],
                    ["dpkg-deb", "--build"],
                    ["dpkg", "--install"],
                ],
            )


class PackageSetValidationTests(unittest.TestCase):
    ABI = "cpython-314-x86_64-linux-gnu"

    def native_path(self, module: str, abi: str | None = None) -> PurePosixPath:
        module_path = builder.PYTHON_SITE_ROOT.joinpath(*module.split("."))
        return module_path.with_name(f"{module_path.name}.{abi or self.ABI}.so")

    def inspection(
        self,
        *,
        package: str = "xpra-codecs",
        files: tuple[PurePosixPath, ...] | None = None,
        depends: str = "xpra-common, libva2, libva-drm2, libyuv0",
    ) -> builder.DebInspection:
        if files is None:
            files = tuple(
                self.native_path(module)
                for module in builder.REQUIRED_NATIVE_CODEC_MODULES
            )
        return builder.DebInspection(
            architecture="amd64",
            depends=depends,
            files=files,
            package=package,
            version="7.0-r42485-1",
        )

    def complete_set(self) -> tuple[builder.DebInspection, ...]:
        return (
            self.inspection(
                package="xpra-common",
                files=(builder.PYTHON_SITE_ROOT / "xpra/__init__.py",),
                depends="python3",
            ),
            self.inspection(),
        )

    def test_accepts_complete_package_capability(self) -> None:
        bindings = builder.validate_package_set(self.complete_set())
        self.assertEqual(set(bindings), set(builder.REQUIRED_NATIVE_CODEC_MODULES))

    def test_requires_both_jph_extensions_in_the_core_package_with_one_abi(self) -> None:
        common, codecs = self.complete_set()
        jph = tuple(
            self.native_path(f"xpra.codecs.jph.{part}")
            for part in ("decoder", "encoder")
        )
        files = tuple(dict.fromkeys((*codecs.files, *jph)))
        builder.validate_package_set((common, self.inspection(files=files)))
        for target in jph:
            others = tuple(path for path in files if path != target)
            alternate = PurePosixPath(
                str(target).replace(self.ABI, "cpython-313-x86_64-linux-gnu")
            )
            python_only = target.with_name(target.name.split(".")[0] + ".py")
            cases = (
                ("missing", (common, self.inspection(files=others)), "missing or ambiguous"),
                (
                    "python-only",
                    (common, self.inspection(files=(*others, python_only))),
                    "missing or ambiguous",
                ),
                (
                    "wrong owner",
                    (
                        common,
                        self.inspection(files=others),
                        self.inspection(package="xpra-codecs-extras", files=(target,)),
                    ),
                    "belongs to xpra-codecs-extras",
                ),
                (
                    "different ABI",
                    (common, self.inspection(files=(*others, alternate))),
                    "different Python ABIs",
                ),
                (
                    "ambiguous ABI",
                    (common, self.inspection(files=(*files, alternate))),
                    "missing or ambiguous",
                ),
            )
            for label, inspections, message in cases:
                with self.subTest(module=target.name, label=label), self.assertRaisesRegex(
                    builder.BuildFailure, message,
                ):
                    builder.validate_package_set(inspections)

    def test_rejects_inconsistent_package_inventory(self) -> None:
        common, codecs = self.complete_set()
        encoder = self.native_path("xpra.codecs.libva.encoder")
        decoder = self.native_path("xpra.codecs.libva.decoder")
        without_libva = tuple(path for path in codecs.files if "/libva/" not in f"/{path}")
        cases = (
            ("missing common", (codecs,), "missing xpra-common"),
            (
                "duplicate package",
                (common, codecs, self.inspection()),
                "package name is duplicated",
            ),
            (
                "overlapping payload",
                (
                    common,
                    codecs,
                    self.inspection(
                        package="xpra-extra",
                        files=(common.files[0],),
                        depends="xpra-common",
                    ),
                ),
                "packages overlap",
            ),
            (
                "missing libva capability",
                (common, self.inspection(files=without_libva)),
                "xpra.codecs.libva.decoder is missing or ambiguous",
            ),
            (
                "missing encoder",
                (
                    common,
                    self.inspection(
                        files=tuple(path for path in codecs.files if path != encoder)
                    ),
                ),
                "xpra.codecs.libva.encoder is missing or ambiguous",
            ),
            (
                "missing decoder",
                (
                    common,
                    self.inspection(
                        files=tuple(path for path in codecs.files if path != decoder)
                    ),
                ),
                "xpra.codecs.libva.decoder is missing or ambiguous",
            ),
            (
                "ambiguous ABI",
                (
                    common,
                    self.inspection(
                        files=(
                            *codecs.files,
                            self.native_path(
                                "xpra.codecs.libva.encoder",
                                "cpython-313-x86_64-linux-gnu",
                            ),
                        )
                    ),
                ),
                "xpra.codecs.libva.encoder is missing or ambiguous",
            ),
            (
                "wrong package",
                (
                    common,
                    self.inspection(
                        files=tuple(path for path in codecs.files if path != decoder)
                    ),
                    self.inspection(
                        package="xpra-codecs-extras",
                        files=(decoder,),
                        depends="xpra-codecs",
                    ),
                ),
                "belongs to xpra-codecs-extras",
            ),
            (
                "missing linked dependency",
                (common, self.inspection(depends="xpra-common, libva2, libyuv0")),
                "libva-drm2",
            ),
            (
                "vendor dependency",
                (
                    common,
                    self.inspection(
                        depends=(
                            "xpra-common, libva2, libva-drm2, libyuv0, "
                            "xpra-codecs-amd"
                        )
                    ),
                ),
                "vendor or extras",
            ),
        )
        for label, inspections, message in cases:
            with self.subTest(label=label), self.assertRaisesRegex(
                builder.BuildFailure, message
            ):
                builder.validate_package_set(inspections)


class InstalledCodecCapabilityTests(unittest.TestCase):
    def exercise_probe(
        self,
        mode: str = "valid",
        *,
        declared_jph_dependency: bool = True,
        records: dict | None = None,
    ) -> dict:
        recorded = records if records is not None else {}
        recorded.update(encoded=0, decoded=0, freed=[], shlibdeps=[])

        class Image:
            PACKED = 0

            def __init__(self, _x, _y, width, height, pixels, pixel_format, depth, stride,
                         bytesperpixel=4, planes=0):
                self.width, self.height, self.pixels = width, height, pixels
                self.pixel_format, self.depth, self.stride = pixel_format, depth, stride
                self.bytesperpixel, self.planes = bytesperpixel, planes

            def get_width(self):
                return self.width

            def get_height(self):
                return self.height

            def get_pixels(self):
                return self.pixels

            def get_pixel_format(self):
                return self.pixel_format

            def get_depth(self):
                return self.depth

            def get_rowstride(self):
                return self.stride

            def get_planes(self):
                return self.planes

            def free(self):
                recorded["freed"].append(self)

        def encode(coding, image, options):
            self.assertEqual(coding, "jph")
            self.assertEqual((image.width, image.height, image.pixel_format), (32, 32, "RGB"))
            self.assertEqual(options["quality"], 100)
            self.assertGreater(len(set(image.pixels)), 32)
            recorded["encoded"] += 1
            recorded["source"] = image
            if mode == "encoder error":
                raise RuntimeError("injected native encoder failure")
            return ("jph", SimpleNamespace(data=b"" if mode == "empty" else b"codestream"),
                    {"quality": 100}, image.width, image.height, 0, 24)

        def decode(payload, _options):
            self.assertEqual(payload, b"codestream")
            recorded["decoded"] += 1
            source = recorded["source"]
            pixels = bytearray()
            for y in range(source.height):
                for x in range(source.width):
                    offset = (y * source.width + x) * 3
                    pixels.extend(reversed(source.pixels[offset:offset + 3]))
                    pixels.append(0xA5)
                pixels.extend(b"padding!")
            if mode == "changed pixel":
                pixels[0] ^= 1
            return Image(0, 0, source.width - (mode == "wrong size"), source.height,
                         bytes(pixels), "RGB" if mode == "wrong format" else "BGRX",
                         24, source.width * 4 + 8)

        fixture = PackageSetValidationTests()
        inspections = fixture.complete_set()
        if declared_jph_dependency:
            common, codecs = inspections
            inspections = (
                common,
                fixture.inspection(depends=f"{codecs.depends}, libopenjph-fixture"),
            )
        bindings = builder.validate_package_set(inspections)
        temporary_directory = tempfile.TemporaryDirectory
        with temporary_directory() as raw:
            root = Path(raw)

            def command(argv, **kwargs):
                if argv[:2] == ["dpkg-deb", "--extract"]:
                    return subprocess.CompletedProcess(argv, 0, "", "")
                if argv[:2] == ["/usr/bin/python3", "-c"]:
                    self.assertEqual(kwargs["cwd"], Path("/"))
                    self.assertNotIn("PYTHONPATH", kwargs["env"])
                    site = Path(argv[3])
                    modules = {}
                    names = (
                        *builder.REQUIRED_NATIVE_CODEC_MODULES,
                        "xpra.codecs.jph.decoder", "xpra.codecs.jph.encoder",
                    )
                    for name in names:
                        path = site.joinpath(*name.split("."))
                        modules[name] = SimpleNamespace(
                            __file__=str(path.with_suffix(f".{fixture.ABI}.so")),
                        )
                    modules["xpra.codecs.jph.encoder"].encode = encode
                    modules["xpra.codecs.jph.decoder"].decompress = decode
                    if mode == "escaped JPH import":
                        modules["xpra.codecs.jph.decoder"].__file__ = str(root / "foreign.so")
                    modules["xpra.codecs.image"] = SimpleNamespace(
                        __file__=str(site / "xpra/codecs/image.py"), ImageWrapper=Image,
                    )
                    modules["xpra.util.objects"] = SimpleNamespace(
                        __file__=str(site / "xpra/util/objects.py"), typedict=dict,
                    )
                    stdout, stderr = StringIO(), StringIO()
                    try:
                        with (
                            patch.object(sys, "argv", ["-c", *argv[3:]]),
                            patch.object(sys, "path", list(sys.path)),
                            patch.object(
                                importlib, "import_module", side_effect=modules.__getitem__,
                            ),
                            redirect_stdout(stdout), redirect_stderr(stderr),
                        ):
                            exec(argv[2], {"__name__": "__main__"})  # noqa: S102
                    except (Exception, SystemExit) as error:
                        raise builder.BuildFailure(str(error)) from error
                    recorded["probe_stderr"] = stderr.getvalue()
                    return subprocess.CompletedProcess(argv, 0, stdout.getvalue(), stderr.getvalue())
                if argv[0] == "dpkg-shlibdeps":
                    recorded["shlibdeps"] = argv
                    return subprocess.CompletedProcess(
                        argv, 0,
                        "shlibs:Depends=libva2, libva-drm2, libyuv0, libopenjph-fixture\n", "",
                    )
                raise AssertionError(argv)

            with (
                patch.object(builder.tempfile, "TemporaryDirectory",
                             side_effect=lambda **_kwargs: temporary_directory(dir=root)),
                patch.object(builder, "run", side_effect=command),
            ):
                builder.validate_installed_codec_capability(
                    (root / "common.deb", root / "codecs.deb"), inspections, bindings,
                )
        return recorded

    def test_actual_probe_requires_lossless_rgb_and_frees_images(self) -> None:
        observed = self.exercise_probe()
        self.assertEqual((observed["encoded"], observed["decoded"]), (1, 1))
        self.assertEqual(len(observed["freed"]), 2)
        self.assertIn("packaged_jph_roundtrip=", observed["probe_stderr"])
        for part in ("encoder", "decoder"):
            self.assertTrue(any(f"/jph/{part}." in arg for arg in observed["shlibdeps"]))

    def test_actual_probe_rejects_bad_jph_results_or_foreign_import(self) -> None:
        for mode in (
            "changed pixel", "wrong size", "wrong format", "empty",
            "encoder error", "escaped JPH import",
        ):
            with self.subTest(mode=mode):
                records = {}
                with self.assertRaises(builder.BuildFailure):
                    self.exercise_probe(mode, records=records)
                if records["encoded"]:
                    self.assertIn(records["source"], records["freed"])
                if records["decoded"]:
                    self.assertEqual(len(records["freed"]), 2)

    def test_rejects_missing_actual_jph_elf_dependency(self) -> None:
        with self.assertRaisesRegex(builder.BuildFailure, "libopenjph-fixture"):
            self.exercise_probe(declared_jph_dependency=False)


class PackageBuildTests(unittest.TestCase):
    def test_disables_automatic_dbgsym_packages(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            output.mkdir()
            package = output / "xpra_6.6-r42479-1_amd64.deb"
            package.write_bytes(b"deb")
            build_environment: dict[str, str] = {}

            def command(
                argv: list[str],
                **kwargs: object,
            ) -> subprocess.CompletedProcess[str]:
                if argv[:3] == ["git", "show", "-s"]:
                    return subprocess.CompletedProcess(argv, 0, "1234567890\n", "")
                if argv[0] == "dpkg-buildpackage":
                    build_environment.update(kwargs["env"])  # type: ignore[arg-type]
                    return subprocess.CompletedProcess(argv, 0, "", "")
                raise AssertionError(argv)

            with (
                patch.object(builder, "SOURCE", source),
                patch.object(builder, "PACKAGE_OUTPUT", output),
                patch.object(builder, "run", side_effect=command),
            ):
                self.assertEqual(builder.build_packages("a" * 40), (package,))

            self.assertIn(
                "noautodbgsym",
                build_environment["DEB_BUILD_OPTIONS"].split(),
            )

    def test_refuses_dbgsym_output_if_the_build_flag_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            output.mkdir()
            (output / "xpra_6.6-r42479-1_amd64.deb").write_bytes(b"deb")
            (output / "xpra-codecs-dbgsym_6.6-r42479-1_amd64.deb").write_bytes(
                b"debug"
            )

            def command(
                argv: list[str],
                **_kwargs: object,
            ) -> subprocess.CompletedProcess[str]:
                stdout = "1234567890\n" if argv[:3] == ["git", "show", "-s"] else ""
                return subprocess.CompletedProcess(argv, 0, stdout, "")

            with (
                patch.object(builder, "SOURCE", source),
                patch.object(builder, "PACKAGE_OUTPUT", output),
                patch.object(builder, "run", side_effect=command),
                self.assertRaisesRegex(builder.BuildFailure, "forbidden dbgsym"),
            ):
                builder.build_packages("a" * 40)


if __name__ == "__main__":
    unittest.main()
