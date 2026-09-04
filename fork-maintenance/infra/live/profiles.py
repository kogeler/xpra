"""Define the supported Xpra live application and lifecycle profiles."""

from __future__ import annotations

import argparse
import sys

from live_config import CLIPBOARD_POLICIES as CONFIGURED_CLIPBOARD_POLICIES
from live_config import (
    LiveConfigError,
    load_network_profiles,
    network_profile,
    network_profile_names,
)

CLIPBOARD_POLICIES = CONFIGURED_CLIPBOARD_POLICIES
CLIPBOARD_CASE_SELECTION = "cases/x11-client-clipboard-events"
APPLICATIONS = (
    "zed",
    "hardware",
    "opengl",
    "vkcube",
    "gtk",
    "keyboard",
    "clipboard",
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
    {("clipboard", "application-exit", "rgb", "strict", "default")}
)
LIVE_ACCEPTANCE_PROFILES = (
    STACK_LIVE_ACCEPTANCE_PROFILES | CASE_ONLY_LIVE_ACCEPTANCE_PROFILES
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


def validate_profile_selection(*, application: str, selection: str) -> None:
    """Bind case-only live profiles to their exact reviewed selection."""
    if application == "clipboard" and selection != CLIPBOARD_CASE_SELECTION:
        raise ProfileError(
            "clipboard live acceptance requires selection "
            f"{CLIPBOARD_CASE_SELECTION}"
        )


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
        validate_profile_selection(
            application=args.application,
            selection=args.selection,
        )
    except ProfileError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
