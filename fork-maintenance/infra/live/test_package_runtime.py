# Copyright (C) 2026 kogeler
"""Exact DEB bytes and observed runtime capabilities are separate requirements."""

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import package_runtime as runtime


def expected_packages(role):
    names = runtime.COMMON_PACKAGES + (runtime.SERVER_PACKAGES if role == "server" else ())
    return [{"package": name, "name": f"{name}_1_amd64.deb", "version": "1",
             "sha256": hashlib.sha256(name.encode()).hexdigest(), "size": len(name)} for name in names]


def evidence(role):
    entries = expected_packages(role)
    expected = {"version": "1", "distro": "distribution", "source_commit": "a" * 40,
                "selection_sha256": "b" * 64, "packages": entries}
    names = runtime.CORE_MODULES + (("xpra.wayland.server.compositor",) if role == "server" else ())
    record = {"schema": 1, "role": role, "payload_verified": True, **expected,
              "css_upgrade": {"metadata_package_sha256": entries[0]["sha256"],
                              "synthetic_old_version": "1~cssold", "negative_overwrite_rejected": True,
                              "positive_owner": "xpra-common"},
              "versions": {entry["package"]: "1" for entry in entries},
              "modules": {name: {"path": "/usr/lib/python3/dist-packages/" + name.replace(".", "/") + ".so",
                                 "owner": "xpra-common", "sha256": "c" * 64} for name in names}}
    expected["native_modules"] = {
        value["path"]: {"owner": value["owner"], "sha256": value["sha256"]}
        for value in record["modules"].values()
    }
    return expected, record


class PackageRuntimeTest(unittest.TestCase):
    def test_complete_inventory_is_required_on_both_roles(self):
        for role in ("server", "client"):
            expected, record = evidence(role)
            runtime.validate_evidence(record, expected, role)
            for name in record["modules"]:
                changed = copy.deepcopy(record)
                del changed["modules"][name]
                with self.subTest(role=role, missing=name), self.assertRaises(ValueError):
                    runtime.validate_evidence(changed, expected, role)

    def test_wrong_source_queue_role_bytes_or_import_cannot_pass(self):
        expected, original = evidence("client")
        changes = (
            lambda r: r.update(source_commit="other"),
            lambda r: r.update(selection_sha256="other"),
            lambda r: r.update(role="server"),
            lambda r: r["packages"][0].update(sha256="d" * 64),
            lambda r: r["versions"].update({"xpra-common": "other"}),
            lambda r: r["modules"][runtime.CORE_MODULES[0]].update(path="/usr/local/lib/xpra.so"),
            lambda r: r["modules"][runtime.CORE_MODULES[0]].update(owner="foreign-package"),
            lambda r: r["modules"][runtime.CORE_MODULES[0]].update(sha256="x" * 64),
            lambda r: r["modules"][runtime.CORE_MODULES[0]].update(sha256="d" * 64),
            lambda r: r.pop("css_upgrade"),
            lambda r: r["css_upgrade"].update(negative_overwrite_rejected=False),
            lambda r: r["css_upgrade"].update(metadata_package_sha256="d" * 64),
        )
        for change in changes:
            record = copy.deepcopy(original)
            change(record)
            with self.subTest(change=change), self.assertRaises(ValueError):
                runtime.validate_evidence(record, expected, "client")

    def test_installation_uses_only_explicit_checksum_verified_package_paths(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            entries = expected_packages("client")
            manifest = {"schema": 2, "architecture": "amd64", "debian_version": "1", "packages": entries}
            (root / "manifest.json").write_text(json.dumps(manifest))
            for entry in entries:
                (root / entry["name"]).write_bytes(entry["package"].encode())
            self.assertEqual(runtime.package_entries(root, "client"), (manifest, entries))
            (root / entries[0]["name"]).write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "differs"):
                runtime.package_entries(root, "client")

    def test_missing_duplicate_and_unsafe_packages_fail(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            entries = expected_packages("client")
            for bad in (entries[1:], [entries[0], *entries], [{**entries[0], "name": "../outside.deb"}, *entries[1:]]):
                (root / "manifest.json").write_text(json.dumps({
                    "schema": 2, "architecture": "amd64", "debian_version": "1", "packages": bad,
                }))
                with self.subTest(packages=bad), self.assertRaises(ValueError):
                    runtime.package_entries(root, "client")

    def test_an_existing_source_or_minimal_installation_is_rejected(self):
        with patch.object(runtime.importlib.util, "find_spec", return_value=object()), self.assertRaises(ValueError):
            runtime.install(Path("/never-read"), "client")

    def test_install_keeps_payload_verification_and_overrides_base_path_filters(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            entries = expected_packages("client")
            manifest = {"source_commit": "a" * 40, "selection_sha256": "b" * 64,
                        "debian_version": "1", "distro": "distribution"}
            with (patch.object(runtime.importlib.util, "find_spec", return_value=None),
                  patch.object(runtime, "package_entries", return_value=(manifest, entries)),
                  patch.object(runtime, "capabilities", return_value={"versions": {}, "modules": {}}) as verify,
                  patch.object(runtime, "css_upgrade_probe", return_value={}) as upgrade,
                  patch.object(runtime, "RECORD", root / "record.json"),
                  patch.object(runtime.subprocess, "run") as command):
                runtime.install(root, "client")
                argv = command.call_args.args[0]
                self.assertIn("Dpkg::Options::=--path-include=*", argv)
                self.assertEqual(argv[-len(entries):], [str(root / entry["name"]) for entry in entries])
                verify.assert_called_once_with(entries, "client", payload=True)
                upgrade.assert_called_once_with(root / entries[0]["name"], "1")

    def test_root_only_payloads_are_not_claimed_as_nonroot_verification(self):
        with patch.object(runtime.os, "geteuid", return_value=1001), self.assertRaises(ValueError):
            runtime.capabilities([], "client", payload=True)
        with patch.object(runtime.os, "geteuid", return_value=1001), patch.object(runtime.subprocess, "run") as dpkg:
            with patch.object(runtime, "CORE_MODULES", ()):
                self.assertEqual(runtime.capabilities([], "client"), {"versions": {}, "modules": {}})
            dpkg.assert_not_called()


if __name__ == "__main__":
    unittest.main()
