#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 kogeler
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

"""Exercise setup's real switch parser without modifying the source checkout."""

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


class SeccompBuildTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(__file__).resolve().parents[3]
        scratch = source / "build"
        scratch.mkdir(exist_ok=True)
        cls.temporary = TemporaryDirectory(prefix="seccomp-build-test-", dir=scratch)
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.copy = Path(cls.temporary.name) / "source"
        cls.copy.mkdir()
        shutil.copytree(source / "xpra", cls.copy / "xpra", ignore=shutil.ignore_patterns(
            "__pycache__", "*.pyc", "*.so", "*.pyd", "*.dll",
        ))
        for name in ("setup.py", "pyproject.toml", "README.md", "COPYING"):
            shutil.copy2(source / name, cls.copy / name)

    def check_switch(self, option, enabled):
        environment = os.environ.copy()
        environment.update({"PYTHONPATH": str(self.copy), "PYTHONDONTWRITEBYTECODE": "1"})
        result = subprocess.run(
            (sys.executable, "setup.py", "--minimal", option, "--help"),
            cwd=self.copy, env=environment, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIsNotNone(re.search(
            rf"(?m)^  --with-seccomp\s+or\s+--without-seccomp\s+\(default: {enabled}\)$", result.stdout,
        ), result.stdout)

    def test_explicit_enable_in_minimal_build(self):
        self.check_switch("--with-seccomp", True)

    def test_explicit_disable(self):
        self.check_switch("--without-seccomp", False)

    def test_standard_boolean_aliases(self):
        self.check_switch("--seccomp", True)
        self.check_switch("--no-seccomp", False)


if __name__ == "__main__":
    unittest.main()
