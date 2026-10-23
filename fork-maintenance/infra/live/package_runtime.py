#!/usr/bin/env python3
# Copyright (C) 2026 kogeler
"""Install and attest the exact collected DEBs, never a second Xpra build."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import json
import os
import subprocess
import tempfile
from pathlib import Path


COMMON_PACKAGES = ("xpra-common", "xpra-codecs", "xpra-client", "xpra-client-gtk3", "xpra-x11", "xpra-wayland")
SERVER_PACKAGES = ("xpra-server", "xpra-server-wayland")
CORE_MODULES = (
    "xpra.seccomp._native", "xpra.platform.posix._landlock",
    "xpra.codecs.libva.encoder", "xpra.codecs.libva.decoder",
    "xpra.codecs.libyuv.converter", "xpra.codecs.jph.encoder", "xpra.codecs.jph.decoder",
    "xpra.net.rencodeplus.rencodeplus", "xpra.x11.bindings.core",
)
RECORD = Path("/opt/xpra-fork-maintenance/package-capabilities.json")


def css_upgrade_probe(package: Path, version: str) -> dict:
    """Exercise real dpkg with the built common package's transfer metadata.

    Minimal no-script packages isolate file ownership from dependencies and
    service hooks. This proves the metadata, not a full old-release upgrade.
    This function runs only inside the package-owning container.
    """
    fields = {
        name: subprocess.check_output(["dpkg-deb", "-f", str(package), name], text=True).strip()
        for name in ("Breaks", "Replaces")
    }
    # A hyphen in this suffix would change Debian's upstream/revision split.
    old_version = version + "~cssold"
    subprocess.run(["dpkg", "--compare-versions", old_version, "lt", version], check=True)
    with tempfile.TemporaryDirectory(prefix="xpra-css-upgrade-") as raw:
        scratch = Path(raw)

        def fixture(name: str, revision: str, css: bool, *, replaces: bool = True) -> Path:
            label = "old" if revision == old_version else "current"
            directory = scratch / (name + "-" + label + ("-positive" if replaces else "-negative"))
            (directory / "DEBIAN").mkdir(parents=True)
            control = (f"Package: {name}\nVersion: {revision}\nArchitecture: all\n"
                       "Maintainer: Xpra test <nobody@example.invalid>\nDescription: CSS ownership probe\n")
            if name == "xpra-common":
                control += "Breaks: " + fields["Breaks"] + "\n"
                if replaces:
                    control += "Replaces: " + fields["Replaces"] + "\n"
            (directory / "DEBIAN/control").write_text(control)
            if css:
                data = directory / "usr/share/xpra/css/10_header_bar.css"
                data.parent.mkdir(parents=True)
                data.write_bytes(name.encode())
            if name == "xpra-server":
                sentinel = directory / "usr/share/xpra/server-sentinel"
                sentinel.parent.mkdir(parents=True, exist_ok=True)
                sentinel.write_bytes(revision.encode())
            result = directory.with_suffix(".deb")
            subprocess.run(["dpkg-deb", "--build", str(directory), str(result)],
                           check=True, capture_output=True, text=True)
            return result

        old = fixture("xpra-server", old_version, True)
        current_server = fixture("xpra-server", version, False)
        for positive in (False, True):
            common = fixture("xpra-common", version, True, replaces=positive)
            root = scratch / ("positive-root" if positive else "negative-root")
            root.mkdir()
            dpkg = ["env", "LC_ALL=C", "dpkg", "--root=" + str(root), "--auto-deconfigure"]
            subprocess.run([*dpkg, "--unpack", str(old)], check=True, capture_output=True, text=True)
            unpack = subprocess.run([*dpkg, "--unpack", str(common)], capture_output=True, text=True, check=False)
            if not positive:
                if not unpack.returncode or "trying to overwrite" not in unpack.stderr:
                    raise ValueError("CSS transfer negative control did not reach the overwrite rejection")
                continue
            if unpack.returncode:
                raise ValueError("CSS ownership transfer failed: " + unpack.stderr[:2048])
            subprocess.run([*dpkg, "--unpack", str(current_server)], check=True, capture_output=True, text=True)
            subprocess.run([*dpkg, "--configure", "xpra-common", "xpra-server"],
                           check=True, capture_output=True, text=True)
            owner = subprocess.check_output([
                "dpkg-query", "--admindir=" + str(root / "var/lib/dpkg"),
                "-S", "/usr/share/xpra/css/10_header_bar.css",
            ], text=True).strip()
            if owner != "xpra-common: /usr/share/xpra/css/10_header_bar.css":
                raise ValueError("CSS transfer did not leave the common package as sole owner")
            if (root / "usr/share/xpra/css/10_header_bar.css").read_bytes() != b"xpra-common":
                raise ValueError("CSS transfer did not preserve the new resource bytes")
    return {"metadata_package_sha256": sha256(package), "synthetic_old_version": old_version,
            "negative_overwrite_rejected": True, "positive_owner": "xpra-common"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def package_entries(directory: Path, role: str) -> tuple[dict, list[dict]]:
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest.get("schema") != 2 or manifest.get("architecture") != "amd64":
        raise ValueError("invalid runtime DEB manifest")
    entries = manifest["packages"]
    names = COMMON_PACKAGES + (SERVER_PACKAGES if role == "server" else ())
    selected = []
    for name in names:
        matches = [entry for entry in entries if entry.get("package") == name]
        if len(matches) != 1:
            raise ValueError(f"required runtime package is missing or ambiguous: {name}")
        entry = matches[0]
        filename = entry["name"]
        if Path(filename).name != filename or not filename.endswith(".deb"):
            raise ValueError("unsafe runtime package filename")
        path = directory / filename
        if path.is_symlink() or sha256(path) != entry["sha256"] or path.stat().st_size != entry["size"]:
            raise ValueError(f"runtime DEB differs from accepted bytes: {name}")
        if entry["version"] != manifest["debian_version"]:
            raise ValueError("mixed runtime package versions")
        selected.append(entry)
    return manifest, selected


def capabilities(packages: list[dict], role: str, *, payload: bool = False) -> dict:
    if payload and os.geteuid() != 0:
        raise ValueError("complete package verification requires the container package owner")
    versions = {}
    for entry in packages:
        name = entry["package"]
        observed = subprocess.check_output(
            ["dpkg-query", "-W", "-f=${db:Status-Status} ${Version}", name], text=True,
        ).strip()
        if observed != "installed " + entry["version"]:
            raise ValueError(f"runtime package is not configured at the accepted version: {name}")
        versions[name] = entry["version"]
        if payload:
            # CUPS backends legitimately include root-only files. Full payload
            # integrity belongs to the package owner; imports below must also
            # pass independently as the actual unprivileged runtime user.
            checked = subprocess.run(["dpkg", "--verify", name], capture_output=True, text=True, check=False)
            if checked.returncode or checked.stdout or checked.stderr:
                raise ValueError(f"installed runtime package verification failed: {name}: "
                                 f"status={checked.returncode}; {(checked.stdout + checked.stderr)[:2048]}")
    modules = {}
    names = CORE_MODULES + (("xpra.wayland.server.compositor",) if role == "server" else ())
    for name in names:
        module = importlib.import_module(name)
        path = Path(module.__file__).resolve(strict=True)
        if not path.is_relative_to("/usr/lib/python3/dist-packages/xpra"):
            raise ValueError(f"runtime module escaped the installed DEBs: {name}")
        owner = subprocess.check_output(["dpkg-query", "-S", str(path)], text=True).split(": ", 1)[0]
        if owner not in versions:
            raise ValueError(f"runtime module is not owned by a selected DEB: {name}")
        modules[name] = {"path": str(path), "owner": owner, "sha256": sha256(path)}
    return {"versions": versions, "modules": modules}


def install(directory: Path, role: str) -> None:
    if importlib.util.find_spec("xpra") is not None:
        raise ValueError("refusing an endpoint which already contains another Xpra installation")
    manifest, entries = package_entries(directory, role)
    subprocess.run(
        # Container base images may exclude documentation/manpages in dpkg.
        # Install the complete payload instead of teaching verification to
        # ignore missing package files. This option is a literal argv value.
        ["apt-get", "-o", "Dpkg::Options::=--path-include=*",
         "install", "--yes", "--no-install-recommends",
         *(str((directory / entry["name"]).resolve()) for entry in entries)], check=True,
    )
    record = {
        "schema": 1, "role": role, "distro": manifest["distro"],
        "source_commit": manifest["source_commit"], "selection_sha256": manifest["selection_sha256"],
        "version": manifest["debian_version"], "packages": entries,
        "payload_verified": True,
        "css_upgrade": css_upgrade_probe(directory / entries[0]["name"], manifest["debian_version"]),
        **capabilities(entries, role, payload=True),
    }
    RECORD.parent.mkdir(parents=True, exist_ok=True)
    RECORD.write_text(json.dumps(record, sort_keys=True) + "\n")
    RECORD.chmod(0o644)


def verify(*, payload: bool = False) -> dict:
    record = json.loads(RECORD.read_text())
    if record.get("payload_verified") is not True:
        raise ValueError("package installation lacks complete payload verification")
    observed = capabilities(record["packages"], record["role"], payload=payload)
    if any(observed[key] != record.get(key) for key in ("versions", "modules")):
        raise ValueError("runtime capabilities differ from the installed package attestation")
    return record


def validate_evidence(record: dict, expected: dict, role: str) -> None:
    """Bind observed installed modules to the exact frozen package inventory."""
    if not isinstance(record, dict) or record.get("schema") != 1:
        raise ValueError("missing runtime package attestation")
    if record.get("payload_verified") is not True:
        raise ValueError("runtime lacks complete package-owner verification")
    for key, value in {
        "role": role, "distro": expected["distro"], "version": expected["version"],
        "source_commit": expected["source_commit"], "selection_sha256": expected["selection_sha256"],
    }.items():
        if record.get(key) != value:
            raise ValueError(f"runtime package attestation has wrong {key}")
    names = COMMON_PACKAGES + (SERVER_PACKAGES if role == "server" else ())
    entries = {entry["package"]: entry for entry in expected["packages"]}
    if record.get("packages") != [entries[name] for name in names]:
        raise ValueError("runtime does not bind the exact accepted DEB bytes")
    if record.get("versions") != {name: expected["version"] for name in names}:
        raise ValueError("runtime package versions are incomplete or mixed")
    if record.get("css_upgrade") != {
        "metadata_package_sha256": entries["xpra-common"]["sha256"],
        "synthetic_old_version": expected["version"] + "~cssold",
        "negative_overwrite_rejected": True, "positive_owner": "xpra-common",
    }:
        raise ValueError("runtime lacks the package-bound CSS transfer controls")
    modules = record.get("modules", {})
    required = CORE_MODULES + (("xpra.wayland.server.compositor",) if role == "server" else ())
    if set(modules) != set(required):
        raise ValueError("runtime critical module inventory is incomplete")
    for value in modules.values():
        path = Path(value["path"])
        digest = value["sha256"]
        if (not path.is_relative_to("/usr/lib/python3/dist-packages/xpra")
                or ".." in path.parts or value["owner"] not in names
                or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)):
            raise ValueError("runtime module provenance is invalid")
        if expected.get("native_modules", {}).get(str(path)) != {
            "owner": value["owner"], "sha256": digest,
        }:
            raise ValueError("loaded runtime module bytes differ from the frozen DEB payload")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "verify", "verify-payload"))
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--role", choices=("server", "client"))
    args = parser.parse_args()
    if args.action == "install":
        if args.directory is None or args.role is None:
            parser.error("install requires --directory and --role")
        install(args.directory, args.role)
    else:
        print(json.dumps(verify(payload=args.action == "verify-payload"), sort_keys=True))
