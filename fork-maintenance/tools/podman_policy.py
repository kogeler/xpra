# Copyright (C) 2026 kogeler
"""Fail-closed Podman user-namespace policy for fork-maintenance runners."""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

ROOTLESS_USERNS_SIZE = 2048
SIZED_USERNS_MODES = frozenset({"auto", "keep-id", "nomap"})
FORBIDDEN_USERNS_MODES = frozenset({"host"})
OPTION_RE = re.compile(r"[a-z][a-z0-9-]*")
POSITIVE_INTEGER_RE = re.compile(r"[1-9][0-9]*")


class PodmanPolicyError(ValueError):
    """Raised when a Podman command weakens the user-namespace boundary."""


def _userns_options(payload: str) -> dict[str, str]:
    options: dict[str, str] = {}
    for item in payload.split(","):
        key, separator, value = item.partition("=")
        if not separator or not OPTION_RE.fullmatch(key) or not value:
            raise PodmanPolicyError(f"invalid Podman user namespace option: {item!r}")
        if key in options:
            raise PodmanPolicyError(f"duplicate Podman user namespace option: {key}")
        options[key] = value
    return options


def validate_userns_spec(spec: str) -> str:
    """Require bounded subordinate-ID allocation and reject host namespaces."""
    if not isinstance(spec, str) or not spec:
        raise PodmanPolicyError("Podman user namespace specification is empty")
    mode, separator, payload = spec.partition(":")
    if mode in FORBIDDEN_USERNS_MODES:
        raise PodmanPolicyError("Podman host user namespace is forbidden")
    if mode not in SIZED_USERNS_MODES:
        return spec
    if not separator or not payload:
        raise PodmanPolicyError(f"Podman {mode} user namespace requires an explicit size")
    options = _userns_options(payload)
    if "size" not in options:
        raise PodmanPolicyError(f"Podman {mode} user namespace requires an explicit size")
    size = options["size"]
    if not POSITIVE_INTEGER_RE.fullmatch(size):
        raise PodmanPolicyError(f"Podman {mode} user namespace requires a positive size")
    return spec


def keep_id_userns(uid: int, gid: int, *, size: int = ROOTLESS_USERNS_SIZE) -> str:
    """Build one bounded keep-id namespace that contains the requested IDs."""
    values = {"uid": uid, "gid": gid, "size": size}
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values.values()):
        raise PodmanPolicyError("Podman keep-id uid, gid, and size must be integers")
    if uid < 0 or gid < 0 or size <= 0:
        raise PodmanPolicyError(
            "Podman keep-id uid and gid must be nonnegative and size must be positive"
        )
    if uid >= size or gid >= size:
        raise PodmanPolicyError("Podman keep-id size does not contain the requested uid/gid")
    return validate_userns_spec(f"keep-id:uid={uid},gid={gid},size={size}")


def validate_podman_argv(argv: Sequence[str]) -> None:
    """Validate every explicit user namespace in one Podman command."""
    if not argv or os.path.basename(argv[0]) != "podman":
        return
    specifications: list[str] = []
    index = 1
    while index < len(argv):
        value = argv[index]
        if value == "--userns":
            index += 1
            if index >= len(argv):
                raise PodmanPolicyError("Podman --userns is missing its value")
            specifications.append(argv[index])
        elif value.startswith("--userns="):
            specifications.append(value.removeprefix("--userns="))
        index += 1
    if len(specifications) > 1:
        raise PodmanPolicyError("Podman command specifies more than one user namespace")
    if specifications:
        validate_userns_spec(specifications[0])
