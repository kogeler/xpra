# Copyright (C) 2026 kogeler

"""Shared fixed-marker authority for the live clipboard fixtures."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

MARKERS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "one": "xpra-x11-clipboard-marker-one",
        "two": "xpra-x11-clipboard-marker-two",
        "three": "xpra-wayland-clipboard-marker-three",
    }
)


def marker_ids() -> tuple[str, ...]:
    """Return the complete ordered set of accepted marker identifiers."""
    return tuple(MARKERS)


def marker_text(marker_id: str) -> str:
    """Resolve a marker identifier through the fixed non-sensitive authority."""
    try:
        return MARKERS[marker_id]
    except KeyError as exc:
        raise ValueError("unknown clipboard fixture marker identifier") from exc


def marker_bytes(marker_id: str) -> bytes:
    """Return the canonical UTF-8 bytes for one fixed marker."""
    return marker_text(marker_id).encode("utf-8")


def content_digest(value: str | bytes) -> tuple[int, str]:
    """Return safe content evidence without retaining or exposing the value."""
    data = value.encode("utf-8") if isinstance(value, str) else value
    return len(data), hashlib.sha256(data).hexdigest()


def marker_summary(
    marker_id: str,
    observed: str | bytes | None = None,
) -> dict[str, object]:
    """Describe expected and observed content using only lengths and digests."""
    expected = marker_bytes(marker_id)
    observed_bytes = observed.encode("utf-8") if isinstance(observed, str) else observed
    return {
        "expected_length": len(expected),
        "expected_sha256": hashlib.sha256(expected).hexdigest(),
        "marker_id": marker_id,
        "matches": observed_bytes == expected,
        "observed_length": None if observed_bytes is None else len(observed_bytes),
        "observed_sha256": None if observed_bytes is None else hashlib.sha256(observed_bytes).hexdigest(),
    }
