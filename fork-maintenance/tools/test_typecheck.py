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
    def test_only_explicit_patch_owned_python_files_are_admitted(self):
        owned = "xpra/server/source/queued_packet.py"
        with (
            tempfile.TemporaryDirectory() as raw,
            patch.object(typecheck.contrib, "selected_cases", return_value=[SimpleNamespace(paths=[owned])]),
        ):
            config = Path(raw) / "mypy.ini"
            config.write_text(f"[mypy]\nfiles = {owned}\n", encoding="utf-8")
            self.assertEqual(typecheck.scoped_files(config), (owned,))
            for files in ("", f"{owned},{owned}", "xpra", "xpra/**/*.py", "xpra/scripts/main.py", "../main.py"):
                config.write_text(f"[mypy]\nfiles = {files}\n", encoding="utf-8")
                with self.subTest(files=files), self.assertRaises(ValueError):
                    typecheck.scoped_files(config)


class TypecheckWorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        source = Path(self.context.enter_context(tempfile.TemporaryDirectory()))
        self.path = source / "queued_packet.py"
        self.path.write_text("# test-owned source\n", encoding="utf-8")
        self.workspace = SimpleNamespace(
            selection="stacks/develop", patch_mode="patched", source=source,
            source_commit="a" * 40, selection_sha256="b" * 64,
        )
        self.version = self.context.enter_context(patch.object(
            typecheck.importlib.metadata, "version", return_value=typecheck.VERSION,
        ))
        self.context.enter_context(patch.object(typecheck.contrib, "isolated_start_check"))
        self.context.enter_context(patch.object(typecheck.contrib, "load_workspace", return_value=self.workspace))
        self.digest = self.context.enter_context(patch.object(
            typecheck.contrib, "run", return_value=SimpleNamespace(stdout=self.workspace.selection_sha256),
        ))
        self.fingerprint = self.context.enter_context(patch.object(
            typecheck.contrib, "finalized_workspace_fingerprint", return_value="c" * 64,
        ))
        self.context.enter_context(patch.object(typecheck, "scoped_files", return_value=(self.path.name,)))
        self.run = self.context.enter_context(patch.object(
            typecheck.subprocess, "run", return_value=Mock(returncode=0),
        ))
        self.output = self.context.enter_context(redirect_stdout(io.StringIO()))

    def test_runs_real_configuration_in_finalized_stack_and_propagates_failure(self):
        for returncode in (0, 1, 2):
            self.run.return_value.returncode = returncode
            self.assertEqual(typecheck.check("scope-control"), returncode)
            self.assertEqual(self.run.call_args.args[0], [
                typecheck.sys.executable, "-m", "mypy", "--config-file", str(typecheck.CONFIG),
            ])
            self.assertEqual(self.run.call_args.kwargs["cwd"], self.workspace.source)
        self.assertEqual(self.fingerprint.call_count, 6)

    def test_rejects_wrong_version_selection_mode_and_stale_metadata_before_mypy(self):
        self.version.return_value = "wrong-version"
        with self.assertRaisesRegex(ValueError, "requires mypy"):
            typecheck.check("scope-control")
        self.version.return_value = typecheck.VERSION
        for selection, mode in (("cases/example", "patched"), ("stacks/partial", "patched"),
                                ("stacks/develop", "tests-only")):
            self.workspace.selection, self.workspace.patch_mode = selection, mode
            with self.subTest(selection=selection, mode=mode), self.assertRaisesRegex(ValueError, "full stacks/develop"):
                typecheck.check("scope-control")
        self.workspace.selection, self.workspace.patch_mode = "stacks/develop", "patched"
        self.digest.return_value.stdout = "changed"
        with self.assertRaisesRegex(ValueError, "metadata is stale"):
            typecheck.check("scope-control")
        self.run.assert_not_called()

    def test_rejects_missing_or_symlinked_source_without_a_skip(self):
        self.path.unlink()
        with self.assertRaisesRegex(ValueError, "missing or unsafe"):
            typecheck.check("scope-control")
        self.path.symlink_to(typecheck.CONFIG)
        with self.assertRaisesRegex(ValueError, "missing or unsafe"):
            typecheck.check("scope-control")
        self.run.assert_not_called()

    def test_changed_workspace_cannot_publish_success(self):
        self.fingerprint.side_effect = ("before", "after")
        with self.assertRaisesRegex(ValueError, "inputs changed"):
            typecheck.check("scope-control")
        self.assertNotIn('"result": "passed"', self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
