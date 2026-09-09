# Copyright (C) 2026 kogeler
"""Offline guards for the live-only libva diagnostic build and artifact handoff."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import build_libva_trace as trace


class TraceBuildTest(unittest.TestCase):
    def test_enabling_sources_preserves_archive_and_signature_authority(self) -> None:
        source = "Types: deb\nURIs: https://archive.example/\nSuites: stable\nSigned-By: /key\n"
        expected = source.replace("Types: deb\n", "Types: deb deb-src\n")
        self.assertEqual(trace.enable_source_types(source), expected)
        self.assertEqual(trace.enable_source_types(expected), expected)
        self.assertEqual(trace.enable_source_types("# Types: deb\n"), "# Types: deb\n")

    def test_exact_distribution_source_and_binary_versions(self) -> None:
        with patch.object(trace, "command", return_value="libva\n2.22.0-3\n2.22.0-3+b1") as command:
            self.assertEqual(trace.package_identity(), {
                "source_package": "libva", "source_version": "2.22.0-3", "binary_version": "2.22.0-3+b1",
            })
        self.assertEqual(command.call_args.args[-1], "libva2")
        self.assertIn("${source:Version}", command.call_args.args[2])

    def test_ambiguous_or_unexpected_source_identity_fails(self) -> None:
        for value in ("", "libva\n1", "other\n1\n1", "libva\n1\n1\n1", "libva\n1 --flag\n1"):
            with self.subTest(value=value), patch.object(trace, "command", return_value=value):
                with self.assertRaises(RuntimeError):
                    trace.package_identity()

    def test_native_control_requires_exact_behavior_and_no_unrelated_failure(self) -> None:
        clean = subprocess.CompletedProcess([], 42, "TRACE-CONTROL collision-reproduced\n", "")
        fixed = subprocess.CompletedProcess([], 0, "TRACE-CONTROL preserved\n", "")
        trace.check_control_result(clean, clean=True)
        trace.check_control_result(fixed, clean=False)
        for result in (
            subprocess.CompletedProcess([], 1, "", "compile failure"),
            subprocess.CompletedProcess([], 42, "wrong failure\n", ""),
            subprocess.CompletedProcess([], 42, clean.stdout, "unexpected error"),
            subprocess.CompletedProcess([], 0, fixed.stdout, "unexpected error"),
        ):
            for is_clean in (True, False):
                with self.subTest(result=result, clean=is_clean), self.assertRaises(RuntimeError):
                    trace.check_control_result(result, clean=is_clean)
        with self.assertRaises(RuntimeError):
            trace.check_control_result(fixed, clean=True)
        with self.assertRaises(RuntimeError):
            trace.check_control_result(clean, clean=False)

    def test_runtime_rejects_package_library_and_loader_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            library = directory / "libva.so.2.2200.0"
            library.write_bytes(b"diagnostic library")
            loader = directory / "libva.so.2"
            loader.symlink_to(library.name)
            record = directory / "record.json"
            identity = {"source_package": "libva", "source_version": "2.22.0-3", "binary_version": "2.22.0-3+b1"}
            document = {
                "schema": 1, **identity, "library_path": str(library), "library_sha256": trace.digest(library),
            }
            record.write_text(json.dumps(document), encoding="utf-8")
            with patch.object(trace, "RECORD", record), patch.object(trace, "package_identity", return_value=identity):
                with redirect_stdout(StringIO()):
                    trace.verify_library()
                for key, wrong in (("schema", 2), ("source_version", "2.23.0-1"), ("binary_version", "2.22.0-4")):
                    record.write_text(json.dumps({**document, key: wrong}), encoding="utf-8")
                    with self.subTest(key=key), self.assertRaisesRegex(RuntimeError, "package differs"):
                        trace.verify_library()
                record.write_text(json.dumps(document), encoding="utf-8")
                library.write_bytes(b"changed")
                with self.assertRaisesRegex(RuntimeError, "digest mismatch"):
                    trace.verify_library()
                library.write_bytes(b"diagnostic library")
                loader.unlink()
                loader.write_bytes(b"another library")
                with self.assertRaisesRegex(RuntimeError, "loader path mismatch"):
                    trace.verify_library()


if __name__ == "__main__":
    unittest.main()
