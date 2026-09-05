import hashlib
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

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
diff --git a/tests/unittests/unit/client/cython_only_test.py b/tests/unittests/unit/client/cython_only_test.py
--- a/tests/unittests/unit/client/cython_only_test.py
+++ b/tests/unittests/unit/client/cython_only_test.py
@@ -1 +1,2 @@
+# cython-only quarantine
 VALUE = 2
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

    def test_required_gate_projection_excludes_tests_list_gates(self) -> None:
        manifest = self.directory / "verification.toml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                "required_gates = []",
                'required_gates = ["live-rgb"]',
            ),
            encoding="utf-8",
        )
        selected = selection.load_selection(
            self.lab,
            "verifications/current-behavior",
        )
        self.assertEqual(tuple(selection.iter_required_gates(selected)), ("live-rgb",))
        self.assertEqual(tuple(selection.iter_gates(selected)), ("full", "live-rgb"))

        arguments = (
            "selection.py",
            "--lab-root",
            str(self.lab),
            "--selection",
            "verifications/current-behavior",
            "required-gates",
        )
        with patch("sys.argv", arguments), redirect_stdout(StringIO()) as stdout:
            self.assertEqual(selection.main(), 0)
        self.assertEqual(stdout.getvalue(), "live-rgb\n")

    def test_wayland_subsurface_live_gate_is_a_supported_evidence_gate(self) -> None:
        manifest = self.directory / "verification.toml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                "required_gates = []",
                'required_gates = ["live-wayland-subsurface"]',
            ),
            encoding="utf-8",
        )

        selected = selection.load_selection(
            self.lab,
            "verifications/current-behavior",
        )
        self.assertEqual(
            tuple(selection.iter_required_gates(selected)),
            ("live-wayland-subsurface",),
        )

        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                "live-wayland-subsurface",
                "live-wayland-subsurface-typo",
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(selection.SelectionError, "invalid verification.*gate"):
            selection.load_selection(
                self.lab,
                "verifications/current-behavior",
            )

    def write_resolution(self) -> tuple[Path, Path, str, str]:
        selected = selection.load_selection(
            self.lab,
            "verifications/current-behavior",
        )
        source = self.lab / "source"
        source.mkdir()
        source_commit = "a" * 40
        document = selection.resolve_selection(
            selected,
            self.lab,
            source,
            source_commit,
        )
        resolution = self.lab / "resolution.json"
        resolution.write_text(json.dumps(document), encoding="utf-8")
        digest = self.lab / "resolution.sha256"
        digest.write_text(f'{document["resolution_sha256"]}\n', encoding="ascii")
        return resolution, digest, source_commit, selection.selection_digest(selected, self.lab)

    def test_resolution_patch_projection_is_counted_ordered_and_verified(self) -> None:
        resolution, digest, source_commit, selection_sha = self.write_resolution()
        arguments = (
            "selection.py",
            "--lab-root",
            str(self.lab),
            "--selection",
            "verifications/current-behavior",
            "resolution-patches",
            "--resolution",
            str(resolution),
            "--digest-file",
            str(digest),
            "--source-commit",
            source_commit,
            "--selection-sha256",
            selection_sha,
        )

        with patch("sys.argv", arguments), redirect_stdout(StringIO()) as stdout:
            self.assertEqual(selection.main(), 0)

        self.assertEqual(
            stdout.getvalue(),
            "count\t1\n"
            "0\tcurrent-behavior\tapply\t"
            "verifications/current-behavior/tests.patch\t"
            f"{hashlib.sha256(TEST_PATCH).hexdigest()}\n",
        )

    def test_resolution_validation_rejects_extra_patch_fields(self) -> None:
        resolution, _digest, source_commit, selection_sha = self.write_resolution()
        selected = selection.load_selection(
            self.lab,
            "verifications/current-behavior",
        )
        document = json.loads(resolution.read_text(encoding="utf-8"))
        document["patches"][0]["unexpected"] = True
        payload = dict(document)
        payload.pop("resolution_sha256")
        document["resolution_sha256"] = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

        with self.assertRaisesRegex(selection.SelectionError, "patch identity"):
            selection.validate_resolution_document(
                selected,
                self.lab,
                document,
                source_commit,
                selection_sha,
            )


class EntrypointSelectionCaptureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.entrypoint = Path(selection.__file__).with_name("entrypoint.sh").read_text(
            encoding="utf-8"
        )

    @classmethod
    def function_source(cls, name: str, following: str) -> str:
        marker = f"{name}() {{\n"
        body = cls.entrypoint.split(marker, 1)[1].split(f"\n{following}() {{", 1)[0]
        return marker + body + "\n"

    def run_capture_fault(
        self,
        function: str,
        following: str,
        invocation: str,
        selector_commands: str = (
            "    printf 'count\\t2\\n'\n"
            "    printf '0\\tfirst-case\\tapply\\tcases/first-case/fix.patch\\t%s\\n' "
            '"$(printf \'c%.0s\' {1..64})"\n'
        ),
    ) -> subprocess.CompletedProcess[str]:
        harness = r"""
set -euo pipefail
INPUTS=/unused
SNAPSHOT_LAB=/unused/lab
RESOLUTION=/unused/resolution.json
RESOLUTION_DIGEST=/unused/resolution.sha256
EXPECTED_COMMIT=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
SELECTION_DIGEST=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
SELECTION=stacks/develop
selection_tool() {
""" + selector_commands + r"""
    return 23
}
"""
        return subprocess.run(
            ("bash",),
            input=(
                harness
                + self.function_source(function, following)
                + f"if {invocation}; then exit 91; else status=$?; fi\n"
                + "test \"$status\" -eq 2\n"
            ),
            capture_output=True,
            text=True,
            check=False,
        )

    def test_resolution_projection_rejects_partial_stdout_with_nonzero_status(self) -> None:
        result = self.run_capture_fault(
            "verified_resolution_patch_rows",
            "selected_focused_tests",
            "verified_resolution_patch_rows",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("cannot read verified patch rows", result.stderr)

    def test_focused_selector_rejects_partial_stdout_with_nonzero_status(self) -> None:
        result = self.run_capture_fault(
            "selected_focused_tests",
            "selected_gate_names",
            "selected_focused_tests",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("cannot read focused unit tests", result.stderr)

    def test_gate_selector_rejects_partial_stdout_with_nonzero_status(self) -> None:
        result = self.run_capture_fault(
            "selected_gate_names",
            "validate_inputs",
            "selected_gate_names",
            "    printf '%s\\n' wayland\n",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("cannot read gates", result.stderr)

    def test_local_test_selector_rejects_partial_stdout_with_nonzero_status(self) -> None:
        result = self.run_capture_fault(
            "libyuv_smoke_test",
            "check_focused_native_modules",
            "libyuv_smoke_test",
            "    printf '%s\\n' cases/libyuv/tests/libyuv_smoke.py\n",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("cannot read local tests", result.stderr)

    def test_exact_output_guard_rejects_correct_output_with_nonzero_status(self) -> None:
        function = self.function_source("require_exact_output", "file_sha256")
        for expected, producer in (
            ("a" * 64, "printf '%s' \"$EXPECTED\""),
            ("", ":"),
        ):
            with self.subTest(expected=expected):
                result = subprocess.run(
                    ("bash",),
                    input=(
                        "set -euo pipefail\n"
                        f"EXPECTED='{expected}'\n"
                        + function
                        + "fault_command() { "
                        + producer
                        + "; return 23; }\n"
                        + "if require_exact_output \"$EXPECTED\" authority "
                        + "fault_command; then exit 91; else status=$?; fi\n"
                        + "test \"$status\" -eq 2\n"
                    ),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertIn("cannot determine authority", result.stderr)

    def test_validate_inputs_rejects_matching_digest_from_failed_selector(self) -> None:
        require_exact = self.function_source("require_exact_output", "file_sha256")
        validate_inputs = self.function_source("validate_inputs", "prepare_source")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source.bundle"
            source.touch()
            lab = root / "lab"
            lab.mkdir()
            result = subprocess.run(
                ("bash",),
                input=(
                    "set -euo pipefail\n"
                    "PATCH_MODE=patched\n"
                    "EXPECTED_COMMIT=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
                    "EXPECTED_SOURCE_HEAD=cccccccccccccccccccccccccccccccccccccccc\n"
                    "EXPECTED_SOURCE_REF=refs/remotes/origin/master\n"
                    "EXPECTED_WORKFLOW_SHA="
                    + "d" * 64
                    + "\nSELECTION_DIGEST="
                    + "b" * 64
                    + f"\nSOURCE='{source}'\nSNAPSHOT_LAB='{lab}'\n"
                    + require_exact
                    + r"""
git() {
    printf '%s %s\n' "$EXPECTED_SOURCE_HEAD" "$EXPECTED_SOURCE_REF"
}
selection_tool() {
    if test "$1" = validate; then
        return 0
    fi
    printf '%s' "$SELECTION_DIGEST"
    return 23
}
"""
                    + validate_inputs
                    + "if validate_inputs; then exit 91; else status=$?; fi\n"
                    + "test \"$status\" -eq 2\n"
                ),
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("cannot determine selection digest", result.stderr)

    def test_callers_use_status_safe_capture_and_no_process_substitution(self) -> None:
        prepare = self.function_source("prepare_source", "installed_xpra_dir")
        focused = self.function_source("run_focused", "run_wayland")

        self.assertIn(
            "if ! patch_rows_output=$(verified_resolution_patch_rows); then",
            prepare,
        )
        self.assertIn(
            "if ! selected_output=$(selected_focused_tests); then",
            focused,
        )
        self.assertNotIn("< <(", self.entrypoint)
        self.assertNotRegex(self.entrypoint, r"\btest\b[^\n]*\$\((?!\()")
        self.assertNotIn("selection_tool gates |", self.entrypoint)
        self.assertNotIn("selection_tool local-tests |", self.entrypoint)

        validate_inputs = self.function_source("validate_inputs", "prepare_source")
        self.assertIn(
            "require_exact_output \"$SELECTION_DIGEST\" 'selection digest'",
            validate_inputs,
        )
        prepare = self.function_source("prepare_source", "installed_xpra_dir")
        for authority in (
            "'checked-out source HEAD'",
            "'source workflow digest'",
            "'checked-out source status'",
            '"selected patch digest for $case_slug"',
        ):
            with self.subTest(authority=authority):
                self.assertIn(authority, prepare)


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
                    "paths = [",
                    '  "tests/unittests/unit/client/broken_test.py",',
                    '  "tests/unittests/unit/client/cython_only_test.py",',
                    "]",
                    "",
                    "[tests]",
                    "list = [",
                    '  "unit.client.broken_test",',
                    '  "unit.client.cython_only_test",',
                    "]",
                    "",
                    "[quarantine]",
                    "modules = [",
                    '  "unit.client.broken_test",',
                    '  "unit.client.cython_only_test",',
                    "]",
                    "",
                    "[quarantine.gates]",
                    'quarantine = ["unit.client.broken_test"]',
                    "quarantine-cython = [",
                    '  "unit.client.broken_test",',
                    '  "unit.client.cython_only_test",',
                    "]",
                    'quarantine-no-compat = ["unit.client.broken_test"]',
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
            ("unit.client.broken_test", "unit.client.cython_only_test"),
        )
        self.assertEqual(
            tuple(selection.iter_quarantined_tests(selected, "quarantine")),
            ("unit.client.broken_test",),
        )
        self.assertEqual(
            tuple(selection.iter_quarantined_tests(selected, "quarantine-cython")),
            ("unit.client.broken_test", "unit.client.cython_only_test"),
        )
        self.assertEqual(
            tuple(selection.iter_quarantined_tests(selected, "quarantine-no-compat")),
            ("unit.client.broken_test",),
        )
        self.assertEqual(
            tuple(selection.iter_gates(selected)),
            ("quarantine", "quarantine-cython", "quarantine-no-compat"),
        )

    def test_only_the_reserved_slug_may_be_a_test_quarantine(self) -> None:
        foreign = self.directory.parent / "foreign-quarantine"
        self.directory.rename(foreign)
        manifest = foreign / "case.toml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                'slug = "upstream-test-quarantine"',
                'slug = "foreign-quarantine"',
                1,
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(selection.SelectionError, "only upstream-test-quarantine"):
            selection.load_selection(self.lab, "cases/foreign-quarantine")

    def test_the_reserved_quarantine_slug_cannot_become_production(self) -> None:
        manifest = self.directory / "case.toml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                'kind = "test-quarantine"',
                'kind = "production"',
                1,
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(selection.SelectionError, "must use kind=test-quarantine"):
            selection.load_selection(self.lab, "cases/upstream-test-quarantine")

    def test_cli_emits_union_without_gate_and_exact_gate_subset(self) -> None:
        base = (
            "selection.py",
            "--lab-root",
            str(self.lab),
            "--selection",
            "cases/upstream-test-quarantine",
            "quarantined-tests",
        )
        expected = (
            (
                base,
                "unit.client.broken_test\nunit.client.cython_only_test\n",
            ),
            (
                (*base, "--gate", "quarantine"),
                "unit.client.broken_test\n",
            ),
            (
                (*base, "--gate", "quarantine-cython"),
                "unit.client.broken_test\nunit.client.cython_only_test\n",
            ),
        )
        for arguments, output in expected:
            with self.subTest(arguments=arguments), patch(
                "sys.argv", arguments
            ), redirect_stdout(StringIO()) as stdout:
                self.assertEqual(selection.main(), 0)
                self.assertEqual(stdout.getvalue(), output)

    def test_cli_rejects_gate_for_an_unrelated_action(self) -> None:
        arguments = (
            "selection.py",
            "--lab-root",
            str(self.lab),
            "--selection",
            "cases/upstream-test-quarantine",
            "validate",
            "--gate",
            "quarantine",
        )
        with patch("sys.argv", arguments), redirect_stderr(StringIO()) as stderr:
            self.assertEqual(selection.main(), 2)
        self.assertIn("valid only with quarantined-tests", stderr.getvalue())

    def test_quarantine_runner_propagates_each_selector_failure(self) -> None:
        entrypoint = Path(selection.__file__).with_name("entrypoint.sh").read_text(
            encoding="utf-8"
        )
        function = entrypoint.split("run_quarantine() {", 1)[1].split(
            "\n}\n\ncase ",
            1,
        )[0]
        runner = "run_quarantine() {" + function + "\n}\n"
        harness = r"""
set -euo pipefail
PATCH_MODE=clean
SELECTION=cases/upstream-test-quarantine
WORK=/unused
require_gate() { :; }
prepare_source() {
    printf '%s\n' PREPARE_CALLED >&2
    return 97
}
selection_tool() {
    if test "$*" = "$FAILED_ACTION"; then
        printf '%s\n' unit.client.partial_test
        return 23
    fi
    case "$*" in
        quarantined-tests)
            printf '%s\n' unit.client.green_test
            ;;
        "quarantined-tests --gate quarantine")
            printf '%s\n' unit.client.green_test
            ;;
        *)
            return 99
            ;;
    esac
}
"""
        for failed_action, error in (
            ("quarantined-tests", "cannot read quarantine module union"),
            (
                "quarantined-tests --gate quarantine",
                "cannot read quarantine assignment for gate quarantine",
            ),
        ):
            with self.subTest(failed_action=failed_action):
                result = subprocess.run(
                    ("bash",),
                    input=(
                        harness
                        + runner
                        + f"FAILED_ACTION='{failed_action}'\n"
                        + "run_quarantine without 1 quarantine\n"
                    ),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(error, result.stderr)
                self.assertNotIn("PREPARE_CALLED", result.stderr)

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

    def test_quarantine_requires_every_exact_gate(self) -> None:
        manifest = self.directory / "case.toml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                'quarantine-no-compat = ["unit.client.broken_test"]\n',
                "",
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(selection.SelectionError, "must contain exactly"):
            selection.load_selection(self.lab, "cases/upstream-test-quarantine")

    def test_quarantine_rejects_the_old_modules_only_schema(self) -> None:
        manifest = self.directory / "case.toml"
        text = manifest.read_text(encoding="utf-8")
        prefix, remainder = text.split("\n[quarantine.gates]\n", 1)
        _gate_table, evidence = remainder.split("\n[evidence]\n", 1)
        manifest.write_text(
            prefix + "\n[evidence]\n" + evidence,
            encoding="utf-8",
        )
        with self.assertRaisesRegex(selection.SelectionError, "modules and gates"):
            selection.load_selection(self.lab, "cases/upstream-test-quarantine")

    def test_quarantine_gate_must_be_an_ordered_subset(self) -> None:
        manifest = self.directory / "case.toml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                '  "unit.client.broken_test",\n  "unit.client.cython_only_test",\n]\n'
                'quarantine-no-compat',
                '  "unit.client.cython_only_test",\n  "unit.client.broken_test",\n]\n'
                'quarantine-no-compat',
                1,
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(selection.SelectionError, "must preserve"):
            selection.load_selection(self.lab, "cases/upstream-test-quarantine")

    def test_quarantine_gate_rejects_duplicates_and_foreign_modules(self) -> None:
        original = (self.directory / "case.toml").read_text(encoding="utf-8")
        invalid = (
            original.replace(
                'quarantine = ["unit.client.broken_test"]',
                'quarantine = ["unit.client.broken_test", "unit.client.broken_test"]',
                1,
            ),
            original.replace(
                'quarantine = ["unit.client.broken_test"]',
                'quarantine = ["unit.client.foreign_test"]',
                1,
            ),
        )
        errors = ("invalid quarantine.gates", "is not a subset")
        for manifest_text, error in zip(invalid, errors, strict=True):
            with self.subTest(error=error):
                (self.directory / "case.toml").write_text(
                    manifest_text,
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(selection.SelectionError, error):
                    selection.load_selection(self.lab, "cases/upstream-test-quarantine")

    def test_every_quarantine_module_requires_a_gate(self) -> None:
        manifest = self.directory / "case.toml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                'quarantine-cython = [\n'
                '  "unit.client.broken_test",\n'
                '  "unit.client.cython_only_test",\n'
                "]\n",
                'quarantine-cython = ["unit.client.broken_test"]\n',
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(selection.SelectionError, "assigned to at least one"):
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
