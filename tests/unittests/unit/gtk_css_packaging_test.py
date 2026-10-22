#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 kogeler
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

"""Shared GTK resources must not require installing an Xpra server."""

from fnmatch import fnmatchcase
from pathlib import Path
import re
import unittest


SOURCE = Path(__file__).resolve().parents[3]


class GtkCssPackagingTest(unittest.TestCase):
    def test_debian_upgrade_declares_the_file_transfer(self):
        control = (SOURCE / "packaging/debian/xpra/control").read_text()
        common = next(part.strip() for part in control.split("\n\n")
                      if part.strip().startswith("Package: xpra-common\n"))
        for field in ("Breaks", "Replaces"):
            with self.subTest(field=field):
                self.assertRegex(common, rf"(?m)^{field}:.*xpra-server \(<< \$\{{binary:Version\}}\)")

    def test_debian_shared_css_has_one_common_owner(self):
        resources = sorted((SOURCE / "fs/share/xpra/css").glob("*.css"))
        self.assertTrue(resources, "the CSS payload must exist")
        manifests = SOURCE / "packaging/debian/xpra"
        for resource in resources:
            installed = "usr/share/xpra/css/" + resource.name
            owners = []
            for manifest in manifests.glob("*.files"):
                for line in manifest.read_text().splitlines():
                    pattern = line.strip()
                    if pattern.endswith("/"):
                        pattern += "*"
                    if fnmatchcase(installed, pattern):
                        owners.append(manifest.stem)
            with self.subTest(resource=installed):
                self.assertEqual(owners, ["xpra-common"])

    def test_rpm_shared_css_has_one_common_owner(self):
        spec = (SOURCE / "packaging/rpm/xpra.spec").read_text()
        owners = []
        for section in re.split(r"(?m)^%files", spec)[1:]:
            header, _, body = section.partition("\n")
            if "%{_datadir}/xpra/css" in body.splitlines():
                owners.append(header.strip())
        self.assertEqual(owners, ["-n %{package_prefix}-common"])


if __name__ == "__main__":
    unittest.main()
