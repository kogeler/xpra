import hashlib
import tempfile
import unittest
from pathlib import Path

import selection

TEST_PATCH = b"""diff --git a/tests/probe_test.py b/tests/probe_test.py
new file mode 100644
index 0000000..a6db29e
--- /dev/null
+++ b/tests/probe_test.py
@@ -0,0 +1 @@
+VALUE = 1
"""

QUARANTINE_PATCH = b"""diff --git a/tests/unittests/unit/client/broken_test.py b/tests/unittests/unit/client/broken_test.py
--- a/tests/unittests/unit/client/broken_test.py
+++ b/tests/unittests/unit/client/broken_test.py
@@ -1 +1,2 @@
+# quarantined
 VALUE = 1
"""


class VerificationSelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.lab = Path(self.temporary.name)
        self.directory = self.lab / "verifications" / "current-behavior"
        tests = self.directory / "tests"
        tests.mkdir(parents=True)
        (tests / "probe.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.directory / "tests.patch").write_bytes(TEST_PATCH)
        self.write_manifest(TEST_PATCH)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_manifest(self, patch: bytes) -> None:
        digest = hashlib.sha256(patch).hexdigest()
        (self.directory / "verification.toml").write_text(
            "\n".join(
                (
                    "schema = 1",
                    'slug = "current-behavior"',
                    'subjects = ["first-fix", "second-fix"]',
                    f'patch_sha256 = "{digest}"',
                    "",
                    "[tests]",
                    "list = [",
                    '  "unit.server.probe_test",',
                    '  "verifications/current-behavior/tests/probe.py",',
                    '  "full",',
                    "]",
                    "",
                    "[evidence]",
                    "required_gates = []",
                    "",
                )
            ),
            encoding="utf-8",
        )

    def test_verification_is_test_only_and_exposes_subjects(self) -> None:
        selected = selection.load_selection(self.lab, "verifications/current-behavior")

        self.assertEqual(selected.kind, "verification")
        self.assertEqual(selected.subjects, ("first-fix", "second-fix"))
        self.assertEqual(
            tuple(selection.iter_unit_tests(selected)),
            ("unit.server.probe_test",),
        )
        self.assertEqual(
            tuple(
                path.as_posix()
                for path in selection.patch_source_paths(selected.cases[0])
            ),
            ("tests/probe_test.py",),
        )

    def test_verification_rejects_production_paths(self) -> None:
        production_patch = TEST_PATCH.replace(
            b"tests/probe_test.py", b"xpra/server/probe.py"
        )
        (self.directory / "tests.patch").write_bytes(production_patch)
        self.write_manifest(production_patch)

        with self.assertRaisesRegex(selection.SelectionError, "may only modify tests/"):
            selection.load_selection(self.lab, "verifications/current-behavior")


class QuarantineSelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.lab = Path(self.temporary.name)
        self.directory = self.lab / "cases" / "upstream-test-quarantine"
        self.directory.mkdir(parents=True)
        (self.directory / "fix.patch").write_bytes(QUARANTINE_PATCH)
        digest = hashlib.sha256(QUARANTINE_PATCH).hexdigest()
        (self.directory / "case.toml").write_text(
            "\n".join(
                (
                    "schema = 1",
                    'slug = "upstream-test-quarantine"',
                    'kind = "test-quarantine"',
                    f'patch_sha256 = "{digest}"',
                    "dependencies = []",
                    'paths = ["tests/unittests/unit/client/broken_test.py"]',
                    "",
                    "[tests]",
                    'list = ["unit.client.broken_test"]',
                    "",
                    "[quarantine]",
                    'modules = ["unit.client.broken_test"]',
                    "",
                    "[evidence]",
                    "required_gates = [",
                    '  "quarantine",',
                    '  "quarantine-cython",',
                    '  "quarantine-no-compat",',
                    "]",
                    "",
                )
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_quarantine_exposes_exact_modules_and_reassessment_gates(self) -> None:
        selected = selection.load_selection(self.lab, "cases/upstream-test-quarantine")

        self.assertEqual(
            tuple(selection.iter_quarantined_tests(selected)),
            ("unit.client.broken_test",),
        )
        self.assertEqual(
            tuple(selection.iter_gates(selected)),
            ("quarantine", "quarantine-cython", "quarantine-no-compat"),
        )

    def test_quarantine_rejects_a_path_not_bound_to_its_module(self) -> None:
        manifest = self.directory / "case.toml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                "unit.client.broken_test",
                "unit.client.other_test",
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(selection.SelectionError, "paths do not match"):
            selection.load_selection(self.lab, "cases/upstream-test-quarantine")

    def test_case_rejects_manifest_paths_that_differ_from_the_patch(self) -> None:
        manifest = self.directory / "case.toml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                "tests/unittests/unit/client/broken_test.py",
                "tests/unittests/unit/client/other_test.py",
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(selection.SelectionError, "manifest paths do not match"):
            selection.load_selection(self.lab, "cases/upstream-test-quarantine")


if __name__ == "__main__":
    unittest.main()
