#!/usr/bin/env python3
# Copyright (C) 2026 kogeler
"""Unit tests for DEB source-tree preparation."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path, PurePosixPath
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
