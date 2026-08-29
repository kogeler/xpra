#!/usr/bin/env python3
# Copyright (C) 2026 kogeler
"""Build one patched Xpra DEB set and emit exactly one tar on stdout."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

import container_payload

RUNNER_ROOT = Path(__file__).resolve().parent
PAYLOAD = Path("/work/payload")
LAB = PAYLOAD / "lab"
SOURCE_BUNDLE = PAYLOAD / "source.bundle"
SELECTION_STATE = PAYLOAD / "selection.json"
SOURCE_MIRROR = Path("/work/source.git")
SOURCE = Path("/work/xpra")
OUTPUT = Path("/work/output")
BUILD_DEPENDENCIES = Path("/work/build-dependencies")
DISTROS = {
    "ubuntu-26.04": {"id": "ubuntu", "version": "26.04", "codename": "resolute"},
    "debian-13": {"id": "debian", "version": "13", "codename": "trixie"},
}
SHA256_RE = re.compile(r"[0-9a-f]{64}")
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
ACTIVE_SELECTION = "stacks/develop"
XPRA_SIGNING_KEY_FINGERPRINT = "B4993B57323148E37977E5D873254CAD17978FAF"


class BuildFailure(RuntimeError):
    """Raised when package inputs or output fail a provenance boundary."""


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    capture: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    log(f"+ {' '.join(command)}")
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else sys.stderr,
        stderr=subprocess.PIPE if capture else sys.stderr,
    )
    if capture and result.stderr:
        sys.stderr.write(result.stderr)
        sys.stderr.flush()
    if result.returncode:
        details = f"\n{result.stdout}" if capture and result.stdout else ""
        raise BuildFailure(
            f"command failed ({result.returncode}): {' '.join(command)}{details}"
        )
    return result


def required_environment(name: str, pattern: re.Pattern[str] | None = None) -> str:
    value = os.environ.get(name, "")
    if not value or (pattern is not None and pattern.fullmatch(value) is None):
        raise BuildFailure(f"invalid or missing {name}")
    return value


def os_release() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
        key, separator, raw = line.partition("=")
        if separator:
            values[key] = raw.strip().strip('"')
    return values


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def selection_tree_sha256(root: Path) -> str:
    """Validate and digest the exact private selection tree received on stdin."""
    try:
        root_details = root.lstat()
    except OSError as error:
        raise BuildFailure(f"selection tree is unavailable: {root}") from error
    if (
        not stat.S_ISDIR(root_details.st_mode)
        or root_details.st_uid != os.getuid()
        or stat.S_IMODE(root_details.st_mode) != 0o700
    ):
        raise BuildFailure("selection tree root is not exactly private")
    entries: list[tuple[Path, os.stat_result]] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        children = sorted(directory.iterdir(), key=lambda item: os.fsencode(item.name))
        child_directories: list[Path] = []
        for child in children:
            details = child.lstat()
            if details.st_uid != os.getuid():
                raise BuildFailure(f"selection cache entry has the wrong owner: {child}")
            if stat.S_ISDIR(details.st_mode):
                if stat.S_IMODE(details.st_mode) != 0o700:
                    raise BuildFailure(f"selection cache directory mode is not 0700: {child}")
                child_directories.append(child)
            elif stat.S_ISREG(details.st_mode):
                if stat.S_IMODE(details.st_mode) != 0o600 or details.st_nlink != 1:
                    raise BuildFailure(f"selection cache file is not exactly private: {child}")
            else:
                raise BuildFailure(f"unsupported selection cache entry: {child}")
            entries.append((child, details))
        pending.extend(reversed(child_directories))
    digest = hashlib.sha256(b"xpra-deb-selection-tree-v1\0")
    for path, details in sorted(
        entries,
        key=lambda item: item[0].relative_to(root).as_posix(),
    ):
        relative = path.relative_to(root).as_posix()
        kind = b"directory" if stat.S_ISDIR(details.st_mode) else b"file"
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(kind)
        digest.update(b"\0")
        digest.update(f"{stat.S_IMODE(details.st_mode):04o}".encode("ascii"))
        digest.update(b"\0")
        if kind == b"file":
            digest.update(str(details.st_size).encode("ascii"))
            digest.update(b"\0")
            digest.update(bytes.fromhex(sha256_file(path)))
        digest.update(b"\0")
    return digest.hexdigest()


def validate_selection_cache(
    selection: str,
    selection_sha256: str,
    selection_cache_sha256: str,
) -> None:
    expected_entries = {"lab", "selection.json", "source.bundle"}
    if {entry.name for entry in PAYLOAD.iterdir()} != expected_entries:
        raise BuildFailure("input payload does not have its exact file set")
    for path in (SOURCE_BUNDLE, SELECTION_STATE):
        details = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != 0o600
            or details.st_nlink != 1
        ):
            raise BuildFailure(f"input payload file is not exactly private: {path}")
    if sha256_file(SELECTION_STATE) != selection_cache_sha256:
        raise BuildFailure("selection cache metadata digest does not match")
    payload = json.loads(SELECTION_STATE.read_text(encoding="utf-8"))
    expected_keys = {
        "owner",
        "schema",
        "selection",
        "selection_sha256",
        "snapshot_tree_sha256",
    }
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise BuildFailure("selection cache metadata schema is invalid")
    expected = {
        "owner": "xpra-deb-selection-cache",
        "schema": 1,
        "selection": selection,
        "selection_sha256": selection_sha256,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise BuildFailure("selection cache metadata provenance does not match")
    tree_sha256 = str(payload.get("snapshot_tree_sha256", ""))
    if SHA256_RE.fullmatch(tree_sha256) is None:
        raise BuildFailure("selection cache tree digest is invalid")
    if selection_tree_sha256(LAB) != tree_sha256:
        raise BuildFailure("selection cache tree digest does not match")


def selection_command(selection: str, *arguments: str) -> list[str]:
    return [
        "python3",
        str(RUNNER_ROOT / "selection.py"),
        "--lab-root",
        str(LAB),
        "--selection",
        selection,
        *arguments,
    ]


def prepare_source(
    *,
    selection: str,
    selection_sha256: str,
    source_commit: str,
    source_ref_commit: str,
    source_ref: str,
    workflow_sha256: str,
) -> dict[str, Any]:
    if PAYLOAD.is_symlink() or not PAYLOAD.is_dir():
        raise BuildFailure("input payload was not extracted safely")
    heads = run(["git", "bundle", "list-heads", str(SOURCE_BUNDLE)], capture=True)
    if heads.stdout.strip() != f"{source_ref_commit} {source_ref}":
        raise BuildFailure("source bundle identity does not match the selected master ref")
    run(["git", "clone", "--quiet", "--mirror", str(SOURCE_BUNDLE), str(SOURCE_MIRROR)])
    run(
        [
            "git",
            "clone",
            "--quiet",
            "--no-hardlinks",
            "--no-checkout",
            str(SOURCE_MIRROR),
            str(SOURCE),
        ]
    )
    run(["git", "merge-base", "--is-ancestor", source_commit, source_ref_commit], cwd=SOURCE)
    run(["git", "checkout", "--quiet", "--detach", source_commit], cwd=SOURCE)
    actual_workflow = run(
        ["git", "show", f"{source_commit}:.github/workflows/test.yml"],
        cwd=SOURCE,
        capture=True,
    ).stdout.encode()
    if hashlib.sha256(actual_workflow).hexdigest() != workflow_sha256:
        raise BuildFailure("source test workflow digest does not match")
    observed_selection = run(
        selection_command(selection, "digest"),
        capture=True,
    ).stdout.strip()
    if observed_selection != selection_sha256:
        raise BuildFailure("selection digest does not match its payload")
    resolution_payload = run(
        selection_command(
            selection,
            "resolve",
            "--source-tree",
            str(SOURCE),
            "--source-commit",
            source_commit,
        ),
        capture=True,
    ).stdout
    resolution = json.loads(resolution_payload)
    resolution_sha256 = str(resolution.get("resolution_sha256", ""))
    if not SHA256_RE.fullmatch(resolution_sha256):
        raise BuildFailure("selection resolver returned an invalid resolution digest")
    patches = resolution.get("patches")
    if not isinstance(patches, list):
        raise BuildFailure("selection resolver returned an invalid patch list")
    for entry in patches:
        if not isinstance(entry, dict):
            raise BuildFailure("selection patch entry is invalid")
        patch = LAB / str(entry.get("patch", ""))
        if (
            patch.is_symlink()
            or not patch.is_file()
            or sha256_file(patch) != entry.get("patch_sha256")
        ):
            raise BuildFailure(f"selection patch payload is stale: {patch}")
        status = entry.get("status")
        if status == "already-present":
            run(
                ["git", "apply", "--reverse", "--check", "--whitespace=error-all", str(patch)],
                cwd=SOURCE,
            )
        elif status == "apply":
            run(
                ["git", "apply", "--check", "--index", "--whitespace=error-all", str(patch)],
                cwd=SOURCE,
            )
            run(
                ["git", "apply", "--index", "--whitespace=error-all", str(patch)],
                cwd=SOURCE,
            )
        else:
            raise BuildFailure(f"invalid patch resolution status: {status!r}")
    run(["git", "diff", "--check"], cwd=SOURCE)
    run(["git", "diff", "--cached", "--check"], cwd=SOURCE)
    return resolution


def source_version(source_commit: str) -> tuple[str, int, int]:
    version_source = (SOURCE / "xpra" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([0-9]+\.[0-9]+)"$', version_source, re.MULTILINE)
    if match is None:
        raise BuildFailure("cannot resolve the Xpra package version")
    revision_count = int(
        run(
            ["git", "rev-list", "--count", source_commit, "--first-parent"],
            cwd=SOURCE,
            capture=True,
        ).stdout.strip()
    )
    revision = revision_count + 5014
    return match.group(1), revision, revision_count


def write_source_info(source_commit: str, revision: int) -> None:
    described = run(
        ["git", "describe", "--long", "--always", "--tags", source_commit],
        cwd=SOURCE,
        capture=True,
    ).stdout.strip()
    parts = described.split("-")
    commit_marker = parts[-1] if len(parts) >= 3 else f"g{source_commit[:9]}"
    metadata = (
        "BRANCH = 'HEAD'\n"
        f"COMMIT = {commit_marker!r}\n"
        "LOCAL_MODIFICATIONS = 1\n"
        f"REVISION = {revision}\n"
    )
    (SOURCE / "xpra" / "src_info.py").write_text(metadata, encoding="utf-8")


def prepare_debian_tree(codename: str, base_version: str, revision: int) -> str:
    debian = SOURCE / "debian"
    expected_target = "packaging/debian/xpra"
    if debian.is_symlink():
        if os.readlink(debian) != expected_target:
            raise BuildFailure("the Debian packaging symlink has an unexpected target")
    elif debian.exists():
        raise BuildFailure("the Debian packaging path is not the expected symlink")
    else:
        debian.symlink_to(expected_target, target_is_directory=True)
    control = debian / "control"
    text = control.read_text(encoding="utf-8")
    text = re.sub(rf"(?m)^#{re.escape(codename)}:", f"#{codename}:\n", text)
    control.write_text(text, encoding="utf-8")
    changelog = debian / "changelog"
    lines = changelog.read_text(encoding="utf-8").splitlines(keepends=True)
    if not re.fullmatch(r"[0-9]+\.[0-9]+", base_version):
        raise BuildFailure("invalid Xpra base version for the Debian changelog")
    header = (
        re.fullmatch(
            rf"xpra \({re.escape(base_version)}-1\)( .+\n)",
            lines[0],
        )
        if lines
        else None
    )
    if header is None:
        raise BuildFailure("unexpected Debian changelog header")
    lines[0] = f"xpra ({base_version}-r{revision}-1){header.group(1)}"
    changelog.write_text("".join(lines), encoding="utf-8")
    return f"{base_version}-r{revision}-1"


def install_signing_key(keyring: Path = Path("/usr/share/keyrings/xpra.asc")) -> None:
    run(
        [
            "curl",
            "--fail",
            "--location",
            "--silent",
            "--show-error",
            "https://xpra.org/xpra.asc",
            "--output",
            str(keyring),
        ]
    )
    key_data = run(
        ["gpg", "--batch", "--show-keys", "--with-colons", str(keyring)],
        capture=True,
    ).stdout
    fingerprints = tuple(
        fields[9]
        for line in key_data.splitlines()
        if (fields := line.split(":"))[0] == "fpr" and len(fields) > 9
    )
    if fingerprints != (XPRA_SIGNING_KEY_FINGERPRINT,):
        raise BuildFailure(f"unexpected Xpra repository signing key: {fingerprints}")
    keyring.chmod(0o644)


def install_packaging_shims() -> None:
    packages = BUILD_DEPENDENCIES
    packages.mkdir(mode=0o700)
    for name in ("libcuda1", "libnvidia-fbc1"):
        source = SOURCE / "packaging" / "debian" / name
        control = source / "DEBIAN" / "control"
        if source.is_symlink() or not source.is_dir() or control.is_symlink() or not control.is_file():
            raise BuildFailure(f"upstream Debian packaging shim is missing: {name}")
        source.chmod(0o755)
        control.parent.chmod(0o755)
        control.chmod(0o644)
        package = packages / f"{name}.deb"
        run(["dpkg-deb", "--build", str(source), str(package)])
        run(["dpkg", "--install", str(package)])


def install_build_dependencies(codename: str) -> None:
    repository = SOURCE / "packaging" / "repos" / codename / "xpra.sources"
    if repository.is_symlink() or not repository.is_file():
        raise BuildFailure(f"Xpra repository definition is missing for {codename}")
    shutil.copyfile(repository, "/etc/apt/sources.list.d/xpra.sources")
    install_signing_key()
    run(["apt-get", "update"])
    install_packaging_shims()
    dependency_script = r"""
set -euo pipefail
deps=$(awk '
  /^Build-Depends:[[:space:]]*/ { active=1; sub(/^Build-Depends:[[:space:]]*/, ""); print; next }
  active && /^[[:space:]]*#/ { next }
  active && /^[[:space:]]/ { sub(/^[[:space:]]*/, ""); print; next }
  active { exit }
' debian/control | tr '\n' ' ')
test -n "$deps"
apt-get -o Debug::pkgProblemResolver=yes --no-install-recommends --yes satisfy "$deps"
"""
    run(["bash", "-c", dependency_script], cwd=SOURCE)
    run(["apt-get", "install", "--yes", "python3-pip"])
    run(["apt-get", "remove", "--yes", "cython3"])
    run(
        [
            "python3",
            "-m",
            "pip",
            "install",
            "--break-system-packages",
            "--upgrade",
            "Cython",
        ]
    )


def build_packages(source_commit: str) -> tuple[Path, ...]:
    epoch = run(
        ["git", "show", "-s", "--format=%ct", source_commit],
        cwd=SOURCE,
        capture=True,
    ).stdout.strip()
    environment = os.environ.copy()
    environment.update(
        {
            "BUILD_TYPE": "DEB",
            "DEB_BUILD_OPTIONS": f"parallel={os.cpu_count() or 1}",
            "DPKG_DEB_COMPRESSOR_LEVEL": "6",
            "DPKG_DEB_COMPRESSOR_TYPE": "xz",
            "SOURCE_DATE_EPOCH": epoch,
        }
    )
    run(
        ["dpkg-buildpackage", "-us", "-uc", "-b"],
        cwd=SOURCE,
        env=environment,
    )
    packages = tuple(sorted(Path("/work").glob("*.deb")))
    if not packages:
        raise BuildFailure("dpkg-buildpackage produced no DEB packages")
    return packages


def emit_output(
    *,
    distro: str,
    checkout_commit: str,
    source_commit: str,
    source_ref: str,
    source_ref_commit: str,
    workflow_sha256: str,
    base_image_id: str,
    builder_image_id: str,
    builder_image_input_sha256: str,
    selection: str,
    selection_cache_sha256: str,
    selection_sha256: str,
    resolution: dict[str, Any],
    base_version: str,
    debian_version: str,
    revision: int,
    revision_count: int,
    packages: tuple[Path, ...],
) -> None:
    OUTPUT.mkdir(mode=0o700)
    copied: list[Path] = []
    package_manifest: list[dict[str, Any]] = []
    for package in packages:
        destination = OUTPUT / package.name
        shutil.copyfile(package, destination)
        copied.append(destination)
        fields = {
            field.lower(): run(
                ["dpkg-deb", "--field", str(destination), field],
                capture=True,
            ).stdout.strip()
            for field in ("Package", "Version", "Architecture")
        }
        if (
            not fields["package"].startswith("xpra")
            or fields["version"] != debian_version
            or fields["architecture"] not in {"all", "amd64"}
        ):
            raise BuildFailure(f"unexpected DEB control metadata: {destination.name}")
        package_manifest.append(
            {
                **fields,
                "name": destination.name,
                "sha256": sha256_file(destination),
                "size": destination.stat().st_size,
            }
        )
    architecture = run(["dpkg", "--print-architecture"], capture=True).stdout.strip()
    manifest = {
        "architecture": architecture,
        "base_version": base_version,
        "base_image_id": base_image_id,
        "builder_image_id": builder_image_id,
        "builder_image_input_sha256": builder_image_input_sha256,
        "debian_version": debian_version,
        "checkout_commit": checkout_commit,
        "distro": distro,
        "packages": package_manifest,
        "revision": revision,
        "revision_first_parent_count": revision_count,
        "schema": 2,
        "selection": selection,
        "selection_cache_sha256": selection_cache_sha256,
        "selection_resolution_sha256": resolution["resolution_sha256"],
        "selection_sha256": selection_sha256,
        "source_commit": source_commit,
        "source_ref": source_ref,
        "source_ref_commit": source_ref_commit,
        "workflow_sha256": workflow_sha256,
    }
    manifest_path = OUTPUT / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    checksums = OUTPUT / "SHA256SUMS"
    checksums.write_text(
        "".join(f"{sha256_file(path)}  {path.name}\n" for path in copied),
        encoding="ascii",
    )
    output_files = (checksums, manifest_path, *copied)
    log(
        "package_output="
        + json.dumps(
            {
                "debian_version": debian_version,
                "distro": distro,
                "packages": len(copied),
                "revision": revision,
            },
            sort_keys=True,
        )
    )
    container_payload.write_archive(
        sys.stdout.buffer,
        (
            container_payload.PayloadEntry(path, PurePosixPath(path.name))
            for path in output_files
        ),
    )


def main() -> int:
    os.umask(0o077)
    os.environ["DEBIAN_FRONTEND"] = "noninteractive"
    distro = required_environment("XPRA_DEB_DISTRO")
    if distro not in DISTROS:
        raise BuildFailure(f"unsupported DEB distribution: {distro}")
    source_commit = required_environment("XPRA_EXPECTED_SOURCE_COMMIT", COMMIT_RE)
    checkout_commit = required_environment("XPRA_EXPECTED_CHECKOUT_COMMIT", COMMIT_RE)
    source_ref = required_environment("XPRA_EXPECTED_SOURCE_REF")
    source_ref_commit = required_environment("XPRA_EXPECTED_SOURCE_REF_COMMIT", COMMIT_RE)
    workflow_sha256 = required_environment("XPRA_EXPECTED_WORKFLOW_SHA", SHA256_RE)
    base_image_id = required_environment("XPRA_EXPECTED_BASE_IMAGE_ID", SHA256_RE)
    builder_image_id = required_environment("XPRA_EXPECTED_BUILDER_IMAGE_ID", SHA256_RE)
    builder_image_input_sha256 = required_environment(
        "XPRA_EXPECTED_BUILDER_IMAGE_INPUT_SHA", SHA256_RE
    )
    selection = required_environment("XPRA_LAB_SELECTION")
    if selection != ACTIVE_SELECTION:
        raise BuildFailure(f"DEB builds require the complete {ACTIVE_SELECTION} queue")
    selection_cache_sha256 = required_environment(
        "XPRA_EXPECTED_SELECTION_CACHE_SHA",
        SHA256_RE,
    )
    selection_sha256 = required_environment("XPRA_EXPECTED_SELECTION_SHA", SHA256_RE)
    expected = DISTROS[distro]
    release = os_release()
    if (
        release.get("ID") != expected["id"]
        or release.get("VERSION_ID") != expected["version"]
        or release.get("VERSION_CODENAME") != expected["codename"]
    ):
        raise BuildFailure(f"container OS does not match {distro}: {release}")
    container_payload.extract_archive(sys.stdin.buffer, PAYLOAD)
    validate_selection_cache(selection, selection_sha256, selection_cache_sha256)
    resolution = prepare_source(
        selection=selection,
        selection_sha256=selection_sha256,
        source_commit=source_commit,
        source_ref_commit=source_ref_commit,
        source_ref=source_ref,
        workflow_sha256=workflow_sha256,
    )
    base_version, revision, revision_count = source_version(source_commit)
    write_source_info(source_commit, revision)
    debian_version = prepare_debian_tree(expected["codename"], base_version, revision)
    if not debian_version.startswith(f"{base_version}-r{revision}-"):
        raise BuildFailure("Debian version does not preserve the upstream revision scheme")
    install_build_dependencies(expected["codename"])
    packages = build_packages(source_commit)
    emit_output(
        distro=distro,
        checkout_commit=checkout_commit,
        source_commit=source_commit,
        source_ref=source_ref,
        source_ref_commit=source_ref_commit,
        workflow_sha256=workflow_sha256,
        base_image_id=base_image_id,
        builder_image_id=builder_image_id,
        builder_image_input_sha256=builder_image_input_sha256,
        selection=selection,
        selection_cache_sha256=selection_cache_sha256,
        selection_sha256=selection_sha256,
        resolution=resolution,
        base_version=base_version,
        debian_version=debian_version,
        revision=revision,
        revision_count=revision_count,
        packages=packages,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BuildFailure, container_payload.PayloadError, json.JSONDecodeError, OSError) as error:
        log(f"DEB build failed: {error}")
        raise SystemExit(2) from error
