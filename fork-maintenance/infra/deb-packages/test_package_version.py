# Copyright (C) 2026 kogeler

import unittest
from unittest.mock import patch

import package_version as versions


class PackageVersionTest(unittest.TestCase):
    def test_downstream_revision_does_not_change_upstream_identity(self):
        for revision in (1, 2, 3):
            with self.subTest(revision=revision), patch.object(versions, "DEBIAN_REVISION", revision):
                self.assertEqual(versions.package_version("8.0", 43272), f"8.0-r43272-{revision}")

    def test_invalid_versions_and_boolean_revisions_are_rejected(self):
        for base, revision in (("8.0\n", 43272), ("8.0-1", 43272), ("../8.0", 43272),
                               ("8.0", True), ("8.0", 0), ("8.0", -1)):
            with self.subTest(base=base, revision=revision), self.assertRaises(ValueError):
                versions.package_version(base, revision)
        for revision in (False, 0, -1):
            with patch.object(versions, "DEBIAN_REVISION", revision), self.assertRaises(ValueError):
                versions.package_version("8.0", 43272)


if __name__ == "__main__":
    unittest.main()
