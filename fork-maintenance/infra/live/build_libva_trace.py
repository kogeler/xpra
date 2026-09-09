#!/usr/bin/env python3
# Copyright (C) 2026 kogeler
"""Build a trace-only libva repair inside the two live diagnostic images.

Use the installed distribution package's exact source version, including its
Debian patches. Only va/va_trace.c is modified. No driver, codec, Xpra build or
release package is changed. Runtime verification rejects a different package
version, filename or library digest after the artifact crosses image stages.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
INSTALL = Path("/opt/libva-trace-install")
WORK = Path("/tmp/xpra-libva-trace-build")
RECORD = Path("/opt/xpra-fork-maintenance/libva-trace.json")
VERSION_RE = re.compile(r"[0-9A-Za-z.+:~\-]+")


def command(*args: str, cwd: Path | None = None) -> str:
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def package_identity() -> dict[str, str]:
    fields = command(
        "dpkg-query", "-W", "-f=${source:Package}\n${source:Version}\n${Version}", "libva2",
    ).splitlines()
    if len(fields) != 3 or fields[0] != "libva" or not all(
        VERSION_RE.fullmatch(value) for value in fields[1:]
    ):
        raise RuntimeError("unexpected libva package identity")
    return dict(zip(("source_package", "source_version", "binary_version"), fields, strict=True))


def enable_source_types(text: str) -> str:
    """Keep the distribution's signed archive configuration and add only deb-src."""
    return re.sub(
        r"(?m)^Types: deb$", "Types: deb deb-src", text,
    )


def check_control_result(result: subprocess.CompletedProcess[str], *, clean: bool) -> None:
    expected = (42, "TRACE-CONTROL collision-reproduced\n") if clean else (
        0, "TRACE-CONTROL preserved\n",
    )
    if (result.returncode, result.stdout) != expected or result.stderr:
        raise RuntimeError(f"libva trace {'clean' if clean else 'patched'} control failed: {result}")


def native_control(source: Path, build: Path, *, clean: bool) -> None:
    executable = WORK / ("trace-clean" if clean else "trace-patched")
    subprocess.run([
        "cc", "-std=gnu11", "-O2", "-ffunction-sections", "-fdata-sections",
        "-I", str(build), "-I", str(build / "va"),
        "-I", str(source), "-I", str(source / "va"),
        str(HERE / "libva_trace_test.c"), "-Wl,--gc-sections", "-pthread", "-ldl",
        "-o", str(executable),
    ], check=True)
    directory = WORK / ("clean-files" if clean else "patched-files")
    directory.mkdir()
    result = subprocess.run([str(executable)], cwd=directory, text=True, capture_output=True, check=False)
    check_control_result(result, clean=clean)
    if list(directory.iterdir()):
        raise RuntimeError("libva trace control left files behind")
    print(result.stdout, end="", flush=True)


def build_library() -> None:
    identity = package_identity()
    WORK.mkdir()
    INSTALL.mkdir()
    sources = list(Path("/etc/apt/sources.list.d").glob("*.sources"))
    if not sources:
        raise RuntimeError("distribution deb822 archive configuration is missing")
    for path in sources:
        path.write_text(enable_source_types(path.read_text(encoding="utf-8")), encoding="utf-8")
    subprocess.run(["apt-get", "update"], check=True)
    subprocess.run([
        "apt-get", "source", "--download-only", "--only-source",
        f"libva={identity['source_version']}",
    ], cwd=WORK, check=True)
    archives = {path.name: digest(path) for path in sorted(WORK.iterdir()) if path.is_file()}
    descriptions = list(WORK.glob("*.dsc"))
    if len(descriptions) != 1:
        raise RuntimeError("expected one authenticated libva source description")
    source = WORK / "source"
    subprocess.run(["dpkg-source", "-x", str(descriptions[0]), str(source)], check=True)
    build = WORK / "build"
    architecture = command("dpkg-architecture", "-qDEB_HOST_MULTIARCH")
    if not re.fullmatch(r"[a-z0-9_-]+", architecture):
        raise RuntimeError("invalid distribution multiarch directory")
    library_dir = Path("/usr/lib") / architecture
    original = (library_dir / "libva.so.2").resolve(strict=True)
    if original.parent != library_dir or not re.fullmatch(r"libva\.so\.2\.\d+\.\d+", original.name):
        raise RuntimeError("unexpected libva runtime library path")
    subprocess.run([
        "meson", "setup", str(build), str(source), "--buildtype=release", "--prefix=/usr",
        f"--libdir=lib/{architecture}", "-Dwith_x11=yes", "-Dwith_glx=yes",
        "-Dwith_wayland=yes", "-Dwith_win32=no", "-Denable_docs=false",
    ], check=True)
    native_control(source, build, clean=True)
    patch = HERE / "libva_trace.patch"
    subprocess.run(["git", "apply", "--check", "--whitespace=error-all", str(patch)], cwd=source, check=True)
    subprocess.run(["git", "apply", "--whitespace=error-all", str(patch)], cwd=source, check=True)
    native_control(source, build, clean=False)
    subprocess.run(["meson", "compile", "-C", str(build)], check=True)
    compiled = build / "va" / original.name
    if not compiled.is_file() or compiled.is_symlink():
        raise RuntimeError("rebuilt library does not match the installed ABI filename")
    destination = INSTALL / original.relative_to("/")
    destination.parent.mkdir(parents=True)
    shutil.copyfile(compiled, destination)
    destination.chmod(0o644)
    record = {
        "schema": 1, **identity, "source_archives": archives,
        "patch_sha256": digest(patch), "original_library_sha256": digest(original),
        "library_path": str(original), "library_sha256": digest(destination),
        "clean_control": "collision-reproduced", "patched_control": "preserved",
    }
    output = INSTALL / RECORD.relative_to("/")
    output.parent.mkdir(parents=True)
    output.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    shutil.copyfile(Path(__file__), output.parent / "build_libva_trace.py")
    print(json.dumps(record, sort_keys=True), flush=True)


def verify_library() -> None:
    record = json.loads(RECORD.read_text(encoding="utf-8"))
    if record.get("schema") != 1 or any(record.get(key) != value for key, value in package_identity().items()):
        raise RuntimeError("live libva package differs from its diagnostic build")
    path = Path(record["library_path"])
    if path.is_symlink() or digest(path) != record["library_sha256"]:
        raise RuntimeError("live libva diagnostic library digest mismatch")
    if (path.parent / "libva.so.2").resolve(strict=True) != path:
        raise RuntimeError("live libva loader path mismatch")
    print(json.dumps(record, sort_keys=True), flush=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["build"]:
        build_library()
    elif sys.argv[1:] == ["verify"]:
        verify_library()
    else:
        raise SystemExit("expected build or verify (live container only)")
