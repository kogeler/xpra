#!/usr/bin/env python3
# Copyright (C) 2026 kogeler
"""Run the entire production live suite through the existing named job lifecycle."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import job
from profiles import DEFAULT_NETWORK_PROFILE, LIVE_PROFILE_REQUIRED_GATES, LIVE_SELECTION


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
        job.verify_collected(run, record)
        status = job.load_private_json(job.status_path(run))
        if status.get("result") != "success":
            raise job.JobError(f"live suite member did not pass: {run}")
        result, digest, checks = job.report_validation(run, record, inspect_current_images=False)
        if result != "passed" or not checks or not all(checks.values()):
            raise job.JobError(f"live suite member evidence no longer validates: {run}")
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
