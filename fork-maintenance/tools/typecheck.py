#!/usr/bin/env python3
# Copyright (C) 2026 kogeler
"""Strict, opt-in downstream type scopes in an exact full-stack workspace."""

from __future__ import annotations

import argparse
import configparser
import importlib.metadata
import json
import subprocess
import sys
from pathlib import Path

import contrib

CONFIG = contrib.AUTOMATION_ROOT / "infra" / "typecheck" / "mypy.ini"
VERSION = "2.3.1"


def scoped_files(config: Path = CONFIG) -> tuple[str, ...]:
    parser = configparser.ConfigParser()
    parser.read_string(config.read_text(encoding="utf-8"))
    paths = tuple(value.strip() for value in parser["mypy"]["files"].split(","))
    if not paths or len(set(paths)) != len(paths):
        raise ValueError("typecheck requires nonempty unique explicit file scopes")
    owned = {path for case in contrib.selected_cases("stacks/develop") for path in case.paths}
    for path in paths:
        if path not in owned or not path.endswith(".py"):
            raise ValueError(f"typecheck scope is not owned by an active patch: {path}")
    return paths


def check(workspace_name: str) -> int:
    if importlib.metadata.version("mypy") != VERSION:
        raise ValueError(f"typecheck requires mypy {VERSION}")
    repo = contrib.REPOSITORY_ROOT
    contrib.isolated_start_check(repo)
    workspace = contrib.load_workspace(repo, workspace_name)
    if workspace.selection != "stacks/develop" or workspace.patch_mode != "patched":
        raise ValueError("typecheck requires a finalized full stacks/develop workspace")
    current_digest = contrib.run((
        sys.executable, str(contrib.SELECTION_TOOL), "--lab-root", str(contrib.AUTOMATION_ROOT),
        "--selection", workspace.selection, "digest",
    )).stdout.strip()
    if workspace.selection_sha256 != current_digest:
        raise ValueError("workspace selection metadata is stale; create a fresh full-stack workspace")
    before = contrib.finalized_workspace_fingerprint(repo, workspace_name)
    paths = scoped_files()
    for path in paths:
        target = workspace.source / path
        if target.is_symlink() or not target.is_file():
            raise ValueError(f"typecheck source is missing or unsafe: {path}")
    config_bytes = CONFIG.read_bytes()
    result = subprocess.run(
        [sys.executable, "-m", "mypy", "--config-file", str(CONFIG)],
        cwd=workspace.source, check=False,
    )
    if CONFIG.read_bytes() != config_bytes or contrib.finalized_workspace_fingerprint(repo, workspace_name) != before:
        raise ValueError("typecheck inputs changed while running")
    print(json.dumps({
        "mypy": VERSION, "source_commit": workspace.source_commit,
        "selection_sha256": workspace.selection_sha256, "workspace_sha256": before,
        "files": paths, "result": "passed" if result.returncode == 0 else "failed",
    }, sort_keys=True))
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace")
    args = parser.parse_args()
    try:
        return check(args.workspace)
    except (contrib.ContribError, OSError, ValueError, importlib.metadata.PackageNotFoundError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
