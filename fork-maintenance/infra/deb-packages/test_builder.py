#!/usr/bin/env python3
# Copyright (C) 2026 kogeler
"""Unit tests for DEB source-tree preparation."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
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


class SigningKeyTests(unittest.TestCase):
    def test_verifies_the_full_fingerprint_and_makes_the_key_apt_readable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            keyring = Path(raw) / "xpra.asc"

            def command(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                if argv[0] == "curl":
                    keyring.write_text("key", encoding="ascii")
                    keyring.chmod(0o600)
                    return subprocess.CompletedProcess(argv, 0, "", "")
                fingerprint = builder.XPRA_SIGNING_KEY_FINGERPRINT
                return subprocess.CompletedProcess(argv, 0, f"fpr:::::::::{fingerprint}:\n", "")

            with patch.object(builder, "run", side_effect=command):
                builder.install_signing_key(keyring)
            self.assertEqual(keyring.stat().st_mode & 0o777, 0o644)

    def test_rejects_an_unexpected_repository_key(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            keyring = Path(raw) / "xpra.asc"

            def command(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                if argv[0] == "curl":
                    keyring.write_text("key", encoding="ascii")
                    return subprocess.CompletedProcess(argv, 0, "", "")
                return subprocess.CompletedProcess(argv, 0, f"fpr:::::::::{'0' * 40}:\n", "")

            with (
                patch.object(builder, "run", side_effect=command),
                self.assertRaisesRegex(builder.BuildFailure, "unexpected Xpra"),
            ):
                builder.install_signing_key(keyring)


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
if __name__ == "__main__":
    unittest.main()
