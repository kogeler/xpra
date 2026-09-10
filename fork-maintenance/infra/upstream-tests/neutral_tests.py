#!/usr/bin/env python3
# Copyright (C) 2026 kogeler
"""Install current runner-owned regressions without changing production source."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys

ASSET_ROOT = Path(__file__).with_name("neutral")
ASSETS = ("pointer_scroll_test.py", "pointer_scroll_client.c")
TARGET_ROOT = Path("tests/unittests/unit/wayland")


class NeutralTestError(ValueError):
    """A frozen-source, input or no-clobber boundary was violated."""


def git(source: Path, *args: str) -> str:
    return subprocess.run(
        ("git", "--literal-pathspecs", "-C", str(source), *args),
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        timeout=30,
    ).stdout.strip()


def install(source: Path, commit: str, asset_root: Path = ASSET_ROOT) -> tuple[tuple[str, str], ...]:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise NeutralTestError("invalid neutral-test source commit")
    if source.is_symlink() or not source.is_dir() or source.absolute() != source.resolve():
        raise NeutralTestError("neutral-test source must be a canonical real directory")
    if (source / ".git").is_symlink() or not (source / ".git").is_dir():
        raise NeutralTestError("neutral tests require the private cloned source checkout")
    if git(source, "rev-parse", "--show-toplevel") != str(source.resolve()):
        raise NeutralTestError("neutral-test source is not the Git checkout root")
    if git(source, "rev-parse", "HEAD") != commit:
        raise NeutralTestError("neutral-test source HEAD does not match the frozen commit")
    if asset_root.is_symlink() or not asset_root.is_dir():
        raise NeutralTestError("neutral-test asset directory is not a real directory")

    parent = source
    for part in TARGET_ROOT.parts:
        parent /= part
        if parent.is_symlink() or not parent.is_dir():
            raise NeutralTestError(f"neutral-test parent is not a real directory: {parent}")
    payloads = []
    for name in ASSETS:
        asset = asset_root / name
        if asset.is_symlink() or not asset.is_file():
            raise NeutralTestError(f"neutral-test input is not a regular file: {asset}")
        target = parent / name
        if target.exists() or target.is_symlink():
            raise NeutralTestError(f"neutral-test target already exists; reassess ownership: {target}")
        payloads.append((target, asset.read_bytes()))

    # Validate every input and destination before the first write. These are
    # generated files in one private container, never host source or a case patch.
    for target, data in payloads:
        with target.open("xb") as stream:
            os.fchmod(stream.fileno(), 0o644)
            stream.write(data)
    paths = tuple((TARGET_ROOT / name).as_posix() for name in ASSETS)
    git(source, "add", "--", *paths)
    return tuple(
        (target.relative_to(source).as_posix(), hashlib.sha256(data).hexdigest())
        for target, data in payloads
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-tree", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    try:
        rows = install(args.source_tree, args.source_commit)
    except (NeutralTestError, OSError, subprocess.SubprocessError) as error:
        print(f"neutral-test installation failed: {error}", file=sys.stderr)
        return 2
    for path, digest in rows:
        print(f"neutral_test={path} neutral_test_sha256={digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
