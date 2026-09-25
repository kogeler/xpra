# Copyright (C) 2026 kogeler
"""Control-plane guards for the explicitly scoped downstream type checker."""

import io
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import typecheck


class TypecheckScopeTest(unittest.TestCase):
    def test_only_explicit_case_commit_python_files_are_admitted(self):
        owned = "xpra/server/source/queued_packet.py"
        mapping = SimpleNamespace(cases=(SimpleNamespace(paths=(owned,)),))
        with (
            tempfile.TemporaryDirectory() as raw,
            patch.object(typecheck.contrib, "develop_map", return_value=mapping),
        ):
            config = Path(raw) / "mypy.ini"
            config.write_text(f"[mypy]\nfiles = {owned}\n", encoding="utf-8")
            self.assertEqual(typecheck.scoped_files(config), (owned,))
            for files in ("", f"{owned},{owned}", "xpra", "xpra/**/*.py", "xpra/scripts/main.py", "../main.py"):
                config.write_text(f"[mypy]\nfiles = {files}\n", encoding="utf-8")
                with self.subTest(files=files), self.assertRaises(ValueError):
                    typecheck.scoped_files(config)


class TypecheckCheckoutTest(unittest.TestCase):
    """mypy runs on the committed develop checkout, never on a copy."""

    def setUp(self):
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        self.repo = Path(self.context.enter_context(tempfile.TemporaryDirectory()))
        self.path = self.repo / "queued_packet.py"
        self.path.write_text("# test-owned source\n", encoding="utf-8")
        self.context.enter_context(patch.object(typecheck.contrib, "REPOSITORY_ROOT", self.repo))
        self.version = self.context.enter_context(patch.object(
            typecheck.importlib.metadata, "version", return_value=typecheck.VERSION,
        ))
        self.state = SimpleNamespace(source_commit="a" * 40, head="b" * 40)
        self.start = self.context.enter_context(patch.object(
            typecheck.contrib, "isolated_start_check", return_value=self.state,
        ))
        self.head = self.context.enter_context(patch.object(
            typecheck.contrib, "rev_parse", return_value=self.state.head,
        ))
        self.context.enter_context(patch.object(typecheck, "scoped_files", return_value=(self.path.name,)))
        self.run = self.context.enter_context(patch.object(
            typecheck.subprocess, "run", return_value=Mock(returncode=0),
        ))
        self.output = self.context.enter_context(redirect_stdout(io.StringIO()))

    def test_runs_real_configuration_in_the_checkout_and_propagates_failure(self):
        for returncode in (0, 1, 2):
            self.run.return_value.returncode = returncode
            self.assertEqual(typecheck.check(), returncode)
            self.assertEqual(self.run.call_args.args[0], [
                typecheck.sys.executable, "-m", "mypy", "--config-file", str(typecheck.CONFIG),
            ])
            self.assertEqual(self.run.call_args.kwargs["cwd"], self.repo)
        # committed product paths are checked before and after mypy
        self.assertEqual(self.start.call_count, 6)

    def test_rejects_a_wrong_version_before_mypy(self):
        self.version.return_value = "wrong-version"
        with self.assertRaisesRegex(ValueError, "requires mypy"):
            typecheck.check()
        self.run.assert_not_called()

    def test_rejects_missing_or_symlinked_source_without_a_skip(self):
        self.path.unlink()
        with self.assertRaisesRegex(ValueError, "missing or unsafe"):
            typecheck.check()
        self.path.symlink_to(typecheck.CONFIG)
        with self.assertRaisesRegex(ValueError, "missing or unsafe"):
            typecheck.check()
        self.run.assert_not_called()

    def test_a_moved_head_cannot_publish_success(self):
        self.head.return_value = "c" * 40
        with self.assertRaisesRegex(ValueError, "inputs changed"):
            typecheck.check()
        self.assertNotIn('"result": "passed"', self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
