#!/usr/bin/env python3
# Copyright (C) 2026 kogeler
"""Prove that the real mypy configuration rejects the original packet mistake."""

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[2] / "tools"
sys.path.insert(0, str(TOOLS))
import typecheck  # noqa: E402 - shared workspace/config authority


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace")
    args = parser.parse_args()
    workspace = typecheck.contrib.load_workspace(typecheck.contrib.REPOSITORY_ROOT, args.workspace)
    path = workspace.source / "xpra/server/source/queued_packet.py"
    source = path.read_text(encoding="utf-8")
    expression = "packet[0] != WINDOW_DRAW"
    if source.count(expression) != 1:
        raise RuntimeError("packet regression control no longer matches its subject")
    # Mypy shadows the actual module; no installed or workspace source is edited.
    with tempfile.TemporaryDirectory(prefix="mypy-negative-", dir=workspace.source.parent) as raw:
        shadow = Path(raw) / "queued_packet.py"
        shadow.write_text(source.replace(expression, "packet.get_type() != WINDOW_DRAW"), encoding="utf-8")
        result = subprocess.run([
            sys.executable, "-m", "mypy", "--config-file", str(typecheck.CONFIG),
            "--shadow-file", str(path), str(shadow),
        ], cwd=workspace.source, check=False, capture_output=True, text=True)
    diagnostic = result.stdout + result.stderr
    if result.returncode != 1 or 'has no attribute "get_type"' not in diagnostic or "[attr-defined]" not in diagnostic:
        raise RuntimeError(f"mypy did not reject the original tuple/Packet mistake:\n{diagnostic}")
    print("mypy negative control: original get_type-on-sequence mistake rejected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
