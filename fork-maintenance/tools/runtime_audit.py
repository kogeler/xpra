#!/usr/bin/env python3
# Copyright (C) 2026 kogeler
"""Reject asynchronous Python failures even when the owning command exits zero."""

from __future__ import annotations

import re
import subprocess
import sys


FAILURES = {
    "thread-exception": rb"(?m)^Exception in thread [^\r\n]*:",
    "unraisable-exception": rb"(?m)^Exception ignored (?:in|while calling)[^\r\n]*:",
    "logging-error": rb"(?m)^--- Logging error ---",
    "fatal-python-error": rb"(?m)^Fatal Python error:",
}
PATTERNS = {name: re.compile(pattern) for name, pattern in FAILURES.items()}


def failures(payload: bytes, *, positive: bool = False) -> tuple[str, ...]:
    found = [name for name, pattern in PATTERNS.items() if pattern.search(payload)]
    # A traceback alone is not an unhandled failure: codec capability probes
    # deliberately catch EncodingNotSupported and log exc_info at debug level.
    # Escaping main-thread failures must also fail the owning peer's exit gate.
    if positive and b"ResourceWarning:" in payload:
        found.append("resource-leak")
    if positive and re.search(
        rb"corrupted double-linked list|double free or corruption|"
        rb"(?:malloc|free|munmap_chunk)\(\):[^\r\n]*(?:corrupt|invalid|unaligned)|"
        rb"ERROR: (?:AddressSanitizer|LeakSanitizer)", payload,
    ):
        found.append("native-memory-error")
    return tuple(found)


def checked_command(argv: list[str]) -> int:
    if not argv:
        raise ValueError("runtime audit requires a command")
    found: set[str] = set()
    tail = b""
    with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as process:
        assert process.stdout is not None
        while block := process.stdout.read1(64 * 1024):
            sys.stdout.buffer.write(block)
            sys.stdout.buffer.flush()
            found.update(failures(tail + block))
            # Keep enough context for a marker split across pipe reads, without
            # buffering an unbounded build log or withholding its diagnostics.
            tail = (tail + block)[-4096:]
        status = process.wait()
    if found:
        print("runtime audit failed: " + ", ".join(sorted(found)), file=sys.stderr)
    return status if status else int(bool(found))


if __name__ == "__main__":
    raise SystemExit(checked_command(sys.argv[1:]))
