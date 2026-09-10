# Copyright (C) 2026 kogeler
"""Offline ownership controls; these never run the native pointer fixture."""

import hashlib
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import job
import neutral_tests


class NeutralTestsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="neutral-tests-control-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.parent = self.source / neutral_tests.TARGET_ROOT
        self.parent.mkdir(parents=True)
        (self.parent / "existing_test.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.production = self.source / "xpra" / "subject.py"
        self.production.parent.mkdir()
        self.production.write_text("VALUE = 1\n", encoding="utf-8")
        neutral_tests.git(self.source, "init", "--quiet")
        neutral_tests.git(self.source, "add", ".")
        neutral_tests.git(
            self.source, "-c", "user.name=Neutral Test", "-c", "user.email=neutral@example.invalid",
            "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "test fixture",
        )
        self.commit = neutral_tests.git(self.source, "rev-parse", "HEAD")
        self.assets = self.root / "assets"
        shutil.copytree(neutral_tests.ASSET_ROOT, self.assets, ignore=shutil.ignore_patterns("__pycache__"))

    def assert_no_installed_files(self):
        self.assertEqual(tuple(self.parent.iterdir()), (self.parent / "existing_test.py",))
        self.assertEqual(neutral_tests.git(self.source, "status", "--porcelain"), "")

    def test_install_on_clean_and_patched_production_preserves_exact_ownership(self):
        for patched in (False, True):
            with self.subTest(patched=patched), tempfile.TemporaryDirectory(dir=self.root) as raw:
                clone = Path(raw) / "source"
                subprocess.run(
                    ("git", "clone", "--quiet", "--no-hardlinks", str(self.source), str(clone)),
                    check=True, timeout=30,
                )
                if patched:
                    (clone / "xpra/subject.py").write_text("VALUE = 2\n", encoding="utf-8")
                    neutral_tests.git(clone, "add", "xpra/subject.py")
                before = neutral_tests.git(clone, "diff", "--cached", "--", "xpra")
                rows = neutral_tests.install(clone, self.commit, self.assets)
                self.assertEqual(neutral_tests.git(clone, "rev-parse", "HEAD"), self.commit)
                self.assertEqual(neutral_tests.git(clone, "diff", "--cached", "--", "xpra"), before)
                self.assertEqual(neutral_tests.git(clone, "diff", "--name-only"), "")
                self.assertEqual(neutral_tests.git(clone, "ls-files", "--others", "--exclude-standard"), "")
                self.assertEqual(len(rows), 2)
                for path, digest in rows:
                    target = clone / path
                    self.assertEqual(target.read_bytes(), (self.assets / target.name).read_bytes())
                    self.assertEqual(digest, hashlib.sha256(target.read_bytes()).hexdigest())
                    self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
                changed = set(neutral_tests.git(clone, "diff", "--cached", "--name-only").splitlines())
                self.assertEqual(changed, {path for path, _digest in rows} | ({"xpra/subject.py"} if patched else set()))

    def test_wrong_frozen_head_is_rejected_before_writes(self):
        with self.assertRaisesRegex(neutral_tests.NeutralTestError, "HEAD"):
            neutral_tests.install(self.source, "0" * 40, self.assets)
        self.assert_no_installed_files()

    def test_missing_second_input_does_not_publish_the_first(self):
        (self.assets / neutral_tests.ASSETS[1]).unlink()
        with self.assertRaisesRegex(neutral_tests.NeutralTestError, "input"):
            neutral_tests.install(self.source, self.commit, self.assets)
        self.assert_no_installed_files()

    def test_existing_upstream_or_case_test_is_not_overwritten_even_if_identical(self):
        existing = self.parent / neutral_tests.ASSETS[1]
        data = (self.assets / existing.name).read_bytes()
        existing.write_bytes(data)
        with self.assertRaisesRegex(neutral_tests.NeutralTestError, "already exists"):
            neutral_tests.install(self.source, self.commit, self.assets)
        self.assertEqual(existing.read_bytes(), data)
        self.assertFalse((self.parent / neutral_tests.ASSETS[0]).exists())

    def test_symlinked_input_is_rejected(self):
        asset = self.assets / neutral_tests.ASSETS[1]
        asset.unlink()
        asset.symlink_to(neutral_tests.ASSET_ROOT / asset.name)
        with self.assertRaisesRegex(neutral_tests.NeutralTestError, "input"):
            neutral_tests.install(self.source, self.commit, self.assets)
        self.assert_no_installed_files()

    def test_symlinked_asset_directory_is_rejected(self):
        alias = self.root / "alias"
        alias.symlink_to(self.assets, target_is_directory=True)
        with self.assertRaisesRegex(neutral_tests.NeutralTestError, "asset directory"):
            neutral_tests.install(self.source, self.commit, alias)
        self.assert_no_installed_files()

    def test_symlinked_source_is_rejected(self):
        alias = self.root / "alias"
        alias.symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(neutral_tests.NeutralTestError, "canonical"):
            neutral_tests.install(alias, self.commit, self.assets)
        self.assert_no_installed_files()

    def test_symlinked_target_parent_is_rejected(self):
        outside = self.root / "outside"
        self.parent.rename(outside)
        self.parent.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(neutral_tests.NeutralTestError, "parent"):
            neutral_tests.install(self.source, self.commit, self.assets)
        self.assertEqual(tuple(outside.iterdir()), (outside / "existing_test.py",))

    def test_dangling_target_symlink_is_rejected(self):
        target = self.parent / neutral_tests.ASSETS[1]
        target.symlink_to(self.root / "missing")
        with self.assertRaisesRegex(neutral_tests.NeutralTestError, "already exists"):
            neutral_tests.install(self.source, self.commit, self.assets)
        self.assertTrue(target.is_symlink())
        self.assertFalse((self.parent / neutral_tests.ASSETS[0]).exists())

    def test_undeclared_neighbor_is_not_installed(self):
        (self.assets / "not-a-neutral-input.py").write_text("NO = 1\n", encoding="utf-8")
        neutral_tests.install(self.source, self.commit, self.assets)
        self.assertFalse((self.parent / "not-a-neutral-input.py").exists())


class NeutralImageInputsTest(unittest.TestCase):
    def test_prepare_source_installs_neutral_inputs_after_case_modes_and_before_diff_checks(self):
        entrypoint = (job.RUNNER_ROOT / "entrypoint.sh").read_text(encoding="utf-8")
        prepare = entrypoint.split("prepare_source() {", 1)[1].split("\ninstalled_xpra_dir()", 1)[0]
        command = 'python3 "$HOST_RUNNER/neutral_tests.py"'
        self.assertEqual(prepare.count(command), 1)
        self.assertGreater(prepare.index(command), prepare.rindex("    done"))
        self.assertLess(prepare.index(command), prepare.index("    git diff --check"))
        self.assertIn('--source-tree "$WORK" --source-commit "$EXPECTED_COMMIT"', prepare)

    def test_exact_inventory_is_in_streamed_image_context_and_runner_identity(self):
        expected = {
            "neutral_tests.py",
            *(f"neutral/{name}" for name in neutral_tests.ASSETS),
        }
        actual = {name for name in job.IMAGE_CONTEXT_INPUTS if name.startswith("neutral")}
        self.assertEqual(actual, expected)
        self.assertTrue({job.IMAGE_CONTEXT_INPUTS[name] for name in expected} <= set(job.RUNNER_INPUTS))
        with tempfile.TemporaryDirectory(prefix="neutral-context-control-") as raw:
            context = Path(raw)
            job.populate_image_context(context)
            self.assertEqual(
                {path.relative_to(context).as_posix() for path in context.rglob("*") if path.is_file()},
                set(job.IMAGE_CONTEXT_INPUTS),
            )
            self.assertEqual(
                {entry.archive_path.as_posix() for entry in job.image_context_entries(context)},
                set(job.IMAGE_CONTEXT_INPUTS),
            )
            for name in expected:
                self.assertEqual((context / name).read_bytes(), job.IMAGE_CONTEXT_INPUTS[name].read_bytes())

        recipe = (job.RUNNER_ROOT / "Containerfile").read_text(encoding="utf-8")
        ignore = (job.RUNNER_ROOT / ".containerignore").read_text(encoding="utf-8").splitlines()
        make = (job.RUNNER_ROOT / "Makefile").read_text(encoding="utf-8")
        neutral_line = next(line for line in make.splitlines() if line.startswith("override NEUTRAL_INPUTS :="))
        self.assertEqual(set(neutral_line.split(":=", 1)[1].split()), expected)
        key_line = next(line for line in make.splitlines() if line.startswith("override IMAGE_INPUT_SHA :="))
        self.assertIn("$(NEUTRAL_INPUTS)", key_line)
        for name in expected:
            self.assertIn(name, recipe)
            self.assertIn("!" + name, ignore)

    def test_neutral_input_mutation_changes_current_runner_identity(self):
        with tempfile.TemporaryDirectory(prefix="neutral-digest-control-") as raw:
            root = Path(raw)
            inputs = tuple(root / f"input-{i}" for i in range(3))
            for path in inputs:
                path.write_bytes(b"original")
            with patch.object(job, "MAINTENANCE_ROOT", root), patch.object(job, "RUNNER_INPUTS", inputs):
                original = job.runner_sha256()
                for path in inputs:
                    path.write_bytes(b"changed")
                    self.assertNotEqual(job.runner_sha256(), original)
                    path.write_bytes(b"original")
                    self.assertEqual(job.runner_sha256(), original)


if __name__ == "__main__":
    unittest.main()
