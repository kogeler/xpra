#!/usr/bin/env python3
# Copyright (C) 2026 kogeler
"""Run the entire production live suite through the existing named job lifecycle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import job
from profiles import DEFAULT_NETWORK_PROFILE, LIVE_PROFILE_REQUIRED_GATES, LIVE_SELECTION


PEER_LOGS = ("server.stdout", "server.stderr", "client.stdout", "client.stderr")
PEER_WARNING_PATTERNS = {
    "wayland-display-name": re.compile(
        rb"['\"]?display-name['\"]? is not a declared signal of WaylandManager", re.IGNORECASE,
    ),
    "codec-startup-timeout": re.compile(
        rb"timed out waiting for the decode thread to load the codecs", re.IGNORECASE,
    ),
    "clipboard-rate-or-timeout": re.compile(
        rb"more than [0-9]+ clipboard requests per second|"
        rb"(?:clipboard|(?:PRIMARY|SECONDARY) selection request)[^\r\n]*timed out", re.IGNORECASE,
    ),
}


def check_peer_logs(run: str, report_digest: str) -> None:
    """Reject warning regressions in complete, report-bound logs of every peer."""
    report = job.result_path(run)
    job.ensure_private_directory(report.parent)
    job.ensure_private_regular(report)
    raw = report.read_bytes()
    if hashlib.sha256(raw).hexdigest() != report_digest:
        raise job.JobError(f"live suite report changed before peer-log validation: {run}")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise job.JobError(f"invalid peer-log report: {run}")
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise job.JobError(f"live suite peer logs have no scenarios: {run}")
    limit = job.live_runner_module().FRAME_LOG_SCAN_BYTES
    seen = set()
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            raise job.JobError(f"invalid peer-log scenario: {run}")
        name = scenario.get("name")
        if not isinstance(name, str) or not job.NAME_RE.fullmatch(name) or name in seen:
            raise job.JobError(f"invalid peer-log scenario name: {run}")
        seen.add(name)
        directory = report.parent / name
        job.ensure_private_directory(directory)
        digests = scenario.get("artifact_sha256")
        if not isinstance(digests, dict):
            raise job.JobError(f"live suite peer logs have no artifact digests: {run}/{name}")
        for filename in PEER_LOGS:
            path = directory / filename
            job.ensure_private_regular(path)
            with path.open("rb") as stream:
                content = stream.read(limit + 1)
            if len(content) > limit or hashlib.sha256(content).hexdigest() != digests.get(filename):
                raise job.JobError(f"live suite peer log is oversized or changed: {run}/{name}/{filename}")
            for label, pattern in PEER_WARNING_PATTERNS.items():
                if pattern.search(content):
                    # Never include a clipboard-bearing log line in diagnostics.
                    raise job.JobError(f"live suite warning {label}: {run}/{name}/{filename}")


def validate_member(run: str, record: dict[str, object]) -> str:
    job.verify_collected(run, record)
    status = job.load_private_json(job.status_path(run))
    if status.get("result") != "success":
        raise job.JobError(f"live suite member did not pass: {run}")
    result, digest, checks = job.report_validation(run, record, inspect_current_images=False)
    if result != "passed" or not checks or not all(checks.values()):
        raise job.JobError(f"live suite member evidence no longer validates: {run}")
    check_peer_logs(run, digest)
    return digest


def members(prefix: str) -> list[tuple[str, tuple[str, str, str, str, str]]]:
    job.validate_name(prefix)
    # Exercise the shared clipboard/draw queue first, then the other profiles.
    profiles = sorted(
        LIVE_PROFILE_REQUIRED_GATES,
        key=lambda profile: (profile[0] not in {"clipboard", "subsurface"}, profile),
    )
    return [(f"{prefix}-{LIVE_PROFILE_REQUIRED_GATES[profile]}", profile) for profile in profiles]


def current_inputs() -> tuple[str, str, str]:
    runner = job.live_runner_module()
    source, _marker, _revision = runner.resolve_embedded_source()
    selection = runner.resolve_patch_selection(LIVE_SELECTION, None)
    return source, selection.digest, job.harness_sha256()


def retained_record(run: str) -> dict[str, object]:
    owner = job.record_path(run)
    if owner.exists() or owner.is_symlink():
        return job.load_record(run)
    record = job.load_remove_transaction(run)["record"]
    if any(path.exists() or path.is_symlink() for path in job.removal_runtime_paths(run).values()):
        raise job.JobError(f"live suite member removal is incomplete: {run}")
    if not job.record_is_current(record):
        raise job.JobError(f"live suite member harness is stale: {run}")
    job.validate_input_provenance(
        record["input_provenance"], application=record["application"], run=run,
        selection=record["selection"], harness_digest=record["harness_sha256"],
    )
    return record


def check(prefix: str) -> dict[str, object]:
    """Revalidate all nine retained named results, not a caller-supplied subset."""
    expected_inputs = current_inputs()
    common: tuple[object, ...] | None = None
    zed: tuple[object, object] | None = None
    results: dict[str, str] = {}
    for run, profile in members(prefix):
        record = retained_record(run)
        observed = tuple(record[key] for key in (
            "application", "lifecycle", "encoding", "h264_client_policy", "alpha_scenarios",
        ))
        if observed != profile:
            raise job.JobError(f"wrong live suite profile: {run}")
        digest = validate_member(run, record)
        provenance = record["input_provenance"]
        identity = (
            provenance["source_commit"], provenance["server_selection_sha256"],
            provenance["harness_sha256"],
        )
        if identity != expected_inputs:
            raise job.JobError(f"live suite member does not bind the current candidate: {run}")
        # Application fixtures differ; source, queue, context and physical profile must not.
        shared = tuple(provenance[key] for key in (
            "source_archive_sha256", "source_workflow_sha256",
            "server_context_sha256", "server_context_archive_sha256",
            "server_selection_resolution_sha256",
        )) + (record["render_node"], record["network_profile"])
        if common is not None and shared != common:
            raise job.JobError(f"live suite members have different frozen inputs: {run}")
        common = shared
        if profile[0] == "zed":
            application = (provenance["zed_archive_sha256"], provenance["zed_binary_sha256"])
            if zed is not None and application != zed:
                raise job.JobError("live suite Zed payload changed between RGB and H.264")
            zed = application
        results[run] = digest
    if current_inputs() != expected_inputs:
        raise job.JobError("live suite inputs changed during verification")
    return {
        "schema": 1, "result": "passed", "selection": LIVE_SELECTION,
        "source_commit": expected_inputs[0], "selection_sha256": expected_inputs[1],
        "harness_sha256": expected_inputs[2], "reports": results,
    }


def run_all(args: argparse.Namespace) -> int:
    expected = current_inputs()
    runs = members(args.run)
    for run, _profile in runs:
        for path in (
            job.record_path(run), job.freeze_record_path(run), job.freeze_prelaunch_path(run),
            job.remove_transaction_path(run), job.status_path(run), job.log_path(run),
            job.result_path(run).parent,
        ):
            if path.exists() or path.is_symlink():
                raise job.JobError(f"live suite requires fresh RUN names: {path}")
    for run, profile in runs:
        if current_inputs() != expected:
            raise job.JobError("live suite candidate changed; do not combine different candidates")
        application, lifecycle, encoding, policy, alpha = profile
        command = [
            "make", "--no-print-directory", "-C", str(job.MAINTENANCE_ROOT), "live-start",
            "CASE=", "STACK=develop", "PATCH_MODE=patched", f"RUN={run}",
            f"APPLICATION={application}", f"LIFECYCLE={lifecycle}", f"ENCODING={encoding}",
            f"H264_CLIENT_POLICY={policy}", f"ALPHA_SCENARIOS={alpha}",
            f"NETWORK_PROFILE={args.network_profile}",
        ]
        if args.render_node:
            command.append(f"RENDER_NODE={args.render_node}")
        if args.zed_directory:
            command.append(f"ZED_DIRECTORY={args.zed_directory}")
        print(f"live suite: {run}", flush=True)
        for invocation in (
            command,
            [sys.executable, str(Path(job.__file__)), "wait", run],
            [sys.executable, str(Path(job.__file__)), "remove", run],
        ):
            result = subprocess.run(invocation, check=False)
            if result.returncode:
                print(f"live suite stopped at {run}; inspect its named logs before retrying", file=sys.stderr)
                return result.returncode
        # A rendering/paste pass cannot conceal a late startup or conversion
        # warning. Stop before launching another physical profile.
        validate_member(run, retained_record(run))
    print(json.dumps(check(args.run), indent=2, sort_keys=True))
    return 0


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "check"))
    parser.add_argument("run", help="fresh prefix for all nine named jobs")
    parser.add_argument("--network-profile", default=DEFAULT_NETWORK_PROFILE)
    parser.add_argument("--render-node")
    parser.add_argument("--zed-directory")
    args = parser.parse_args()
    try:
        if args.action == "run":
            return run_all(args)
        print(json.dumps(check(args.run), indent=2, sort_keys=True))
        return 0
    except (job.JobError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
