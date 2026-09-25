#!/usr/bin/env python3
# Copyright (C) 2026 kogeler
"""Strict, opt-in downstream type scopes in the develop checkout."""

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
    owned = {path for case in contrib.develop_map(contrib.REPOSITORY_ROOT).cases for path in case.paths}
    for path in paths:
        if path not in owned or not path.endswith(".py"):
            raise ValueError(f"typecheck scope is not owned by a case commit: {path}")
    return paths


def check() -> int:
    if importlib.metadata.version("mypy") != VERSION:
        raise ValueError(f"typecheck requires mypy {VERSION}")
    repo = contrib.REPOSITORY_ROOT
    # committed HEAD is the checked product: product paths must be clean
    state = contrib.isolated_start_check(repo)
    paths = scoped_files()
    for path in paths:
        target = repo / path
        if target.is_symlink() or not target.is_file():
            raise ValueError(f"typecheck source is missing or unsafe: {path}")
    config_bytes = CONFIG.read_bytes()
    result = subprocess.run(
        [sys.executable, "-m", "mypy", "--config-file", str(CONFIG)],
        cwd=repo, check=False,
    )
    if CONFIG.read_bytes() != config_bytes or contrib.rev_parse(repo, "HEAD") != state.head:
        raise ValueError("typecheck inputs changed while running")
    contrib.isolated_start_check(repo)
    print(json.dumps({
        "mypy": VERSION, "source_commit": state.source_commit, "head": state.head,
        "files": paths, "result": "passed" if result.returncode == 0 else "failed",
    }, sort_keys=True))
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        return check()
    except (contrib.ContribError, OSError, ValueError, importlib.metadata.PackageNotFoundError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
