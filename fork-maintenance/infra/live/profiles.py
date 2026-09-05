"""Define the supported Xpra live application and lifecycle profiles."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from live_config import CLIPBOARD_POLICIES as CONFIGURED_CLIPBOARD_POLICIES
from live_config import (
    LiveConfigError,
    load_network_profiles,
    network_profile,
    network_profile_names,
)

CLIPBOARD_POLICIES = CONFIGURED_CLIPBOARD_POLICIES
CLIPBOARD_CASE_SELECTION = "cases/x11-client-clipboard-events"
SUBSURFACE_CASE_SELECTION = "cases/wayland-subsurface-stream-ownership"
APPLICATIONS = (
    "zed",
    "hardware",
    "opengl",
    "vkcube",
    "gtk",
    "keyboard",
    "clipboard",
    "subsurface",
)
LIFECYCLES = ("application-exit", "detach", "transport-loss")
ENCODINGS = ("rgb", "h264")
H264_ACCEPTANCE_POLICIES = ("strict", "adaptive-alpha")
H264_FALLBACK_POLICIES = ("fallback-auto", "fallback-h264")
H264_CLIENT_POLICIES = H264_ACCEPTANCE_POLICIES + H264_FALLBACK_POLICIES
ALPHA_SCENARIOS = ("default", "disabled", "both")
STACK_LIVE_ACCEPTANCE_PROFILES = frozenset(
    {
        ("zed", "application-exit", "rgb", "strict", "default"),
        ("zed", "application-exit", "h264", "adaptive-alpha", "default"),
        ("gtk", "detach", "rgb", "strict", "default"),
        ("gtk", "transport-loss", "rgb", "strict", "default"),
        ("hardware", "application-exit", "h264", "adaptive-alpha", "default"),
        ("opengl", "application-exit", "h264", "adaptive-alpha", "default"),
        ("keyboard", "application-exit", "rgb", "strict", "default"),
    }
)
CASE_ONLY_LIVE_ACCEPTANCE_PROFILES = frozenset(
    {
        ("clipboard", "application-exit", "rgb", "strict", "default"),
        ("subsurface", "application-exit", "rgb", "strict", "default"),
    }
)
LIVE_ACCEPTANCE_PROFILES = (
    STACK_LIVE_ACCEPTANCE_PROFILES | CASE_ONLY_LIVE_ACCEPTANCE_PROFILES
)
LIVE_PROFILE_REQUIRED_GATES = {
    ("zed", "application-exit", "rgb", "strict", "default"): "live-rgb",
    ("zed", "application-exit", "h264", "adaptive-alpha", "default"): "live-h264",
    ("gtk", "detach", "rgb", "strict", "default"): "live-xpra-detach",
    (
        "gtk",
        "transport-loss",
        "rgb",
        "strict",
        "default",
    ): "live-xpra-transport-loss",
    (
        "hardware",
        "application-exit",
        "h264",
        "adaptive-alpha",
        "default",
    ): "live-wayland-h264-hardware",
    (
        "opengl",
        "application-exit",
        "h264",
        "adaptive-alpha",
        "default",
    ): "live-wayland-opengl-h264-hardware",
    (
        "keyboard",
        "application-exit",
        "rgb",
        "strict",
        "default",
    ): "live-wayland-keyboard",
    (
        "clipboard",
        "application-exit",
        "rgb",
        "strict",
        "default",
    ): "live-x11-clipboard",
    (
        "subsurface",
        "application-exit",
        "rgb",
        "strict",
        "default",
    ): "live-wayland-subsurface",
}
STACK_ONLY_LIVE_ACCEPTANCE_PROFILES = frozenset(
    {
        ("zed", "application-exit", "h264", "adaptive-alpha", "default"),
        ("gtk", "detach", "rgb", "strict", "default"),
        ("gtk", "transport-loss", "rgb", "strict", "default"),
    }
)
DEFAULT_NETWORK_PROFILE = load_network_profiles()[0]
NETWORK_PROFILES = network_profile_names()


class ProfileError(ValueError):
    """Raised when a live profile combines incompatible boundaries."""


def validate_profile(
    *,
    application: str,
    lifecycle: str,
    encoding: str,
    h264_client_policy: str,
    alpha_scenarios: str,
    network_profile_name: str = DEFAULT_NETWORK_PROFILE,
) -> None:
    """Fail closed on unsupported or semantically ambiguous combinations."""
    if application not in APPLICATIONS:
        raise ProfileError(f"unsupported live application: {application}")
    if lifecycle not in LIFECYCLES:
        raise ProfileError(f"unsupported live lifecycle: {lifecycle}")
    if encoding not in ENCODINGS:
        raise ProfileError(f"unsupported live encoding: {encoding}")
    if h264_client_policy not in H264_CLIENT_POLICIES:
        raise ProfileError(f"unsupported H.264 client policy: {h264_client_policy}")
    if alpha_scenarios not in ALPHA_SCENARIOS:
        raise ProfileError(f"unsupported alpha scenarios: {alpha_scenarios}")
    try:
        network_profile(network_profile_name)
    except LiveConfigError as error:
        raise ProfileError(str(error)) from error
    profile = (
        application,
        lifecycle,
        encoding,
        h264_client_policy,
        alpha_scenarios,
    )
    if profile not in LIVE_ACCEPTANCE_PROFILES:
        raise ProfileError(f"unsupported live acceptance profile: {profile!r}")


def validate_profile_selection(
    *,
    application: str,
    lifecycle: str,
    encoding: str,
    h264_client_policy: str,
    alpha_scenarios: str,
    selection: str,
    selection_kind: str,
    required_gates: tuple[str, ...],
) -> None:
    """Bind every live profile to its exact case gate or to a stack."""
    profile = (
        application,
        lifecycle,
        encoding,
        h264_client_policy,
        alpha_scenarios,
    )
    gate = LIVE_PROFILE_REQUIRED_GATES.get(profile)
    if gate is None:
        raise ProfileError(f"unsupported live acceptance profile: {profile!r}")
    if application == "clipboard" and (
        selection_kind != "case" or selection != CLIPBOARD_CASE_SELECTION
    ):
        raise ProfileError(
            "clipboard live acceptance requires selection "
            f"{CLIPBOARD_CASE_SELECTION}"
        )
    if application == "subsurface" and (
        selection_kind != "case" or selection != SUBSURFACE_CASE_SELECTION
    ):
        raise ProfileError(
            "subsurface live acceptance requires selection "
            f"{SUBSURFACE_CASE_SELECTION}"
        )
    if selection_kind == "stack":
        if profile not in STACK_LIVE_ACCEPTANCE_PROFILES:
            raise ProfileError(
                f"live profile {gate} does not accept a stack selection"
            )
        return
    if selection_kind != "case":
        raise ProfileError(f"live acceptance requires a case or stack selection: {selection}")
    if profile in STACK_ONLY_LIVE_ACCEPTANCE_PROFILES:
        raise ProfileError(f"live profile {gate} requires a stack selection")
    if gate not in required_gates:
        raise ProfileError(
            f"case selection {selection} does not declare required gate {gate}"
        )


def selection_admission(
    lab_root: Path,
    selection: str,
) -> tuple[str, tuple[str, ...]]:
    """Read validated live-admission metadata from the selection authority."""
    tool = lab_root / "infra" / "upstream-tests" / "selection.py"
    if tool.is_symlink() or not tool.is_file():
        raise ProfileError(f"selection validator is unavailable: {tool}")

    def output(action: str) -> str:
        result = subprocess.run(
            (
                sys.executable,
                str(tool),
                "--lab-root",
                str(lab_root),
                "--selection",
                selection,
                action,
            ),
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            detail = result.stderr.strip() or f"exit status {result.returncode}"
            raise ProfileError(f"cannot validate live selection {selection}: {detail}")
        return result.stdout.strip()

    kind = output("kind")
    required_gates = tuple(output("required-gates").splitlines())
    return kind, required_gates


def scenario_specs(
    *, alpha_scenarios: str, lifecycle: str
) -> tuple[tuple[str, bool], ...]:
    """Return stable scenario names and their alpha override."""
    if lifecycle != "application-exit":
        return ((lifecycle, False),)
    return {
        "default": (("default-alpha", False),),
        "disabled": (("alpha-disabled", True),),
        "both": (("default-alpha", False), ("alpha-disabled", True)),
    }[alpha_scenarios]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("application", choices=APPLICATIONS)
    parser.add_argument("lifecycle", choices=LIFECYCLES)
    parser.add_argument("encoding", choices=ENCODINGS)
    parser.add_argument("h264_client_policy", choices=H264_ACCEPTANCE_POLICIES)
    parser.add_argument("alpha_scenarios", choices=ALPHA_SCENARIOS)
    parser.add_argument(
        "network_profile",
        choices=NETWORK_PROFILES,
        nargs="?",
        default=DEFAULT_NETWORK_PROFILE,
    )
    parser.add_argument("--selection", required=True)
    parser.add_argument(
        "--lab-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
    )
    args = parser.parse_args()
    try:
        validate_profile(
            application=args.application,
            lifecycle=args.lifecycle,
            encoding=args.encoding,
            h264_client_policy=args.h264_client_policy,
            alpha_scenarios=args.alpha_scenarios,
            network_profile_name=args.network_profile,
        )
        selection_kind, required_gates = selection_admission(
            args.lab_root,
            args.selection,
        )
        validate_profile_selection(
            application=args.application,
            lifecycle=args.lifecycle,
            encoding=args.encoding,
            h264_client_policy=args.h264_client_policy,
            alpha_scenarios=args.alpha_scenarios,
            selection=args.selection,
            selection_kind=selection_kind,
            required_gates=required_gates,
        )
    except ProfileError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
