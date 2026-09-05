from __future__ import annotations

import copy
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from argparse import Namespace
from collections.abc import Callable
from contextlib import contextmanager, redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

LIVE_DIRECTORY = Path(__file__).resolve().parent

JOB_SPEC = importlib.util.spec_from_file_location(
    "xpra_fork_maintenance_live_job", LIVE_DIRECTORY / "job.py"
)
if JOB_SPEC is None or JOB_SPEC.loader is None:
    raise RuntimeError("could not load live-job module")
job = importlib.util.module_from_spec(JOB_SPEC)
sys.path.insert(0, str(LIVE_DIRECTORY))
try:
    JOB_SPEC.loader.exec_module(job)
finally:
    sys.path.pop(0)

RUN_SPEC = importlib.util.spec_from_file_location(
    "xpra_fork_maintenance_live_run", LIVE_DIRECTORY / "run.py"
)
if RUN_SPEC is None or RUN_SPEC.loader is None:
    raise RuntimeError("could not load live runner module")
live_run = importlib.util.module_from_spec(RUN_SPEC)
sys.modules[RUN_SPEC.name] = live_run
sys.path.insert(0, str(LIVE_DIRECTORY))
try:
    RUN_SPEC.loader.exec_module(live_run)
finally:
    sys.path.pop(0)
job.live_run = live_run


JOB_ID = "12345678-1234-4abc-8def-123456789abc"
OTHER_JOB_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def completed(
    argv: list[str],
    stdout: str = "",
    *,
    returncode: int = 0,
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def interaction_identity(pid: int = 41) -> dict[str, object]:
    argv = ["python3", live_run.INTERACTION_FIXTURE_SCRIPT]
    return {
        "argv": argv,
        "cmdline_sha256": hashlib.sha256(
            b"\0".join(os.fsencode(argument) for argument in argv) + b"\0"
        ).hexdigest(),
        "pid": pid,
        "schema": 1,
        "start_ticks": "123456",
    }


def server_identity(pid: int = 8) -> dict[str, object]:
    argv = [
        "/usr/bin/python3",
        "/usr/bin/xpra",
        "seamless",
        live_run.SERVER_DISPLAY,
        f"--start-child=python3 {live_run.INTERACTION_FIXTURE_SCRIPT}",
    ]
    return {
        "argv": argv,
        "cmdline_sha256": hashlib.sha256(
            b"\0".join(os.fsencode(argument) for argument in argv) + b"\0"
        ).hexdigest(),
        "pid": pid,
        "schema": 1,
        "start_ticks": "654321",
    }


def client_identity(pid: int = 404) -> dict[str, object]:
    argv = [
        "/usr/bin/python3",
        "/usr/bin/xpra",
        "attach",
        f"tcp://xpra-server:{live_run.SERVER_PORT}",
    ]
    return {
        "argv": argv,
        "cmdline_sha256": hashlib.sha256(
            b"\0".join(os.fsencode(argument) for argument in argv) + b"\0"
        ).hexdigest(),
        "pid": pid,
        "schema": 1,
        "start_ticks": "777777",
    }


def clipboard_interaction(policy: str) -> dict[str, object]:
    checks = {name: True for name in live_run.CLIPBOARD_LIVE_CHECK_NAMES}
    return {
        "attempted": True,
        "checks": checks,
        "client_alive_after_changes": True,
        "local": {
            "initial": {},
            "repeat": {},
            "reverse": {},
            "updated": {},
        },
        "owner": {"records": []},
        "policy": policy,
        "transitions": {},
        "wayland": {"records": []},
        "xfixes": {"records": []},
    }


def clipboard_event(
    sequence: int,
    event: str,
    **values: object,
) -> dict[str, object]:
    return {
        "event": event,
        "monotonic_ns": (sequence + 1) * 1_000,
        "schema": 1,
        "sequence": sequence,
        **values,
    }


def clipboard_conversion(marker_id: str, owner_xid: int) -> dict[str, object]:
    selection_notify = {
        "property_atom": 31,
        "requestor_xid": 41,
        "selection_atom": 51,
        "send_event": True,
        "target_atom": 61,
        "time": 71,
        "type": "SelectionNotify",
    }
    conversion = {
        "completed": True,
        "events": [
            {
                "atom_id": 31,
                "send_event": False,
                "state": 0,
                "time": 70,
                "type": "PropertyNotify",
                "window_xid": 41,
            },
            selection_notify,
        ],
        "overflow": False,
        "selection_notify": selection_notify,
        "target": "TARGETS",
        "value": {"value_complete": True},
    }
    text = copy.deepcopy(conversion)
    text["target"] = "UTF8_STRING"
    return clipboard_event(
        0,
        "conversion-result",
        backend="x11",
        known_targets={"STRING": True, "UTF8_STRING": True},
        marker=live_run.clipboard_fixture_common.marker_summary(
            marker_id,
            live_run.clipboard_fixture_common.marker_text(marker_id),
        ),
        owner_after_xid=owner_xid,
        owner_before_xid=owner_xid,
        owner_stable=True,
        requestor_xid=41,
        targets=conversion,
        text=text,
    )


def clipboard_jsonl(records: list[dict[str, object]]) -> str:
    return "".join(json.dumps(record, sort_keys=True) + "\n" for record in records)


def execute_container_log_probe(
    root: Path,
) -> object:
    def execute(
        _container: str,
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        if command[:2] != ["python3", "-c"]:
            raise AssertionError(f"unexpected probe command: {command}")
        output = StringIO()
        arguments = ["-c", *command[3:-2], str(root), command[-1]]
        with patch.object(sys, "argv", arguments), redirect_stdout(output):
            exec(command[2], {"__name__": "__main__"})  # noqa: S102
        return completed(command, output.getvalue())

    return execute


class LiveJobTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.artifact_root = self.root / "artifacts"
        self.state_root = self.artifact_root / "fork-maintenance"
        self.job_root = self.root / "jobs"
        self.result_root = self.root / "results"
        self.venv_root = self.root / "venvs"
        self.constant_patches = (
            patch.object(job, "ARTIFACT_ROOT", self.artifact_root),
            patch.object(job, "STATE_ROOT", self.state_root),
            patch.object(job, "JOB_ROOT", self.job_root),
            patch.object(job, "RESULT_ROOT", self.result_root),
            patch.object(job, "VENV_ROOT", self.venv_root),
        )
        for value in self.constant_patches:
            value.start()

    def tearDown(self) -> None:
        for value in reversed(self.constant_patches):
            value.stop()
        self.temporary.cleanup()

    def write_private_json(self, path: Path, value: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value) + "\n", encoding="utf-8")
        path.chmod(0o600)

    def retained_remove(
        self,
        run: str,
        *,
        complete: bool = True,
    ) -> tuple[dict[str, object], bytes]:
        job.prepare_private_state()
        record: dict[str, object] = {
            "job_id": JOB_ID,
            "owner": job.OWNER,
            "process": {
                "completion": str(job.completion_path(run)),
                "runtime_log": str(job.runtime_log_path(run)),
            },
            "result_report": str(job.result_path(run)),
            "run": run,
            "schema": 4,
        }
        log = b"retained collected log\n"
        self.write_private_json(job.record_path(run), record)
        job.runtime_log_path(run).write_bytes(log)
        job.runtime_log_path(run).chmod(0o600)
        self.write_private_json(job.completion_path(run), {"exit_code": 0})
        job.log_path(run).write_bytes(log)
        job.log_path(run).chmod(0o600)
        self.write_private_json(job.status_path(run), {"result": "success"})
        transaction = job.publish_remove_transaction(run, record)
        if complete:
            job.cleanup_removal_runtime(run, transaction)
        else:
            job.record_path(run).unlink()
        return record, log

    def record(self, run: str) -> dict[str, object]:
        provenance = {
            "client_context_archive_sha256": "1" * 64,
            "client_context_sha256": "2" * 64,
            "client_selection": "master",
            "client_selection_resolution_sha256": "3" * 64,
            "client_selection_sha256": "4" * 64,
            "harness_sha256": job.harness_sha256(),
            "input_manifest_sha256": "5" * 64,
            "input_tree_sha256": "6" * 64,
            "path": str(job.RESULT_ROOT / run / "inputs"),
            "schema": 2,
            "server_context_archive_sha256": "7" * 64,
            "server_context_sha256": "8" * 64,
            "server_selection": "stacks/develop",
            "server_selection_resolution_sha256": "9" * 64,
            "server_selection_sha256": "a" * 64,
            "source_archive_sha256": "b" * 64,
            "source_commit": "c" * 40,
            "source_commit_marker": "gc12345678",
            "source_revision": 5515,
            "source_workflow_sha256": "d" * 64,
            "zed_archive_sha256": "e" * 64,
            "zed_binary_sha256": "f" * 64,
        }
        return {
            "alpha_scenarios": "default",
            "application": "zed",
            "background_supervisor_sha256": job.sha256_file(
                job.BACKGROUND_SUPERVISOR
            ),
            "encoding": "rgb",
            "h264_client_policy": "strict",
            "harness_sha256": job.harness_sha256(),
            "input_provenance": provenance,
            "job_id": JOB_ID,
            "lifecycle": "application-exit",
            "network_profile": job.DEFAULT_NETWORK_PROFILE,
            "owner": job.OWNER,
            "process": {
                "completion": str(job.completion_path(run)),
                "pid": 12345,
                "process_group": 12345,
                "runtime_log": str(job.runtime_log_path(run)),
                "start_ticks": "42",
                "supervisor_sha256": job.sha256_file(job.BACKGROUND_SUPERVISOR),
            },
            "result_report": str(job.result_path(run)),
            "render_node": "/dev/dri/renderD128",
            "run": run,
            "runner_sha256": job.sha256_file(job.RUNNER),
            "schema": 4,
            "selection": "stacks/develop",
            "supervisor_sha256": job.sha256_file(job.SUPERVISOR),
        }

    def clipboard_record(self, run: str) -> dict[str, object]:
        record = self.record(run)
        record["application"] = "clipboard"
        provenance = record["input_provenance"]
        provenance["zed_archive_sha256"] = None
        provenance["zed_binary_sha256"] = None
        provenance["client_selection"] = provenance["server_selection"]
        for client_key, server_key in (
            ("client_context_archive_sha256", "server_context_archive_sha256"),
            ("client_context_sha256", "server_context_sha256"),
            (
                "client_selection_resolution_sha256",
                "server_selection_resolution_sha256",
            ),
            ("client_selection_sha256", "server_selection_sha256"),
        ):
            provenance[client_key] = provenance[server_key]
        return record

    def make_clipboard_evidence_tree(
        self,
        run: str,
        record: dict[str, object],
    ) -> tuple[dict[str, object], Path]:
        report = job.result_path(run)
        report.parent.mkdir(parents=True)
        report.parent.chmod(0o700)
        inputs = report.parent / "inputs"
        inputs.mkdir()
        inputs.chmod(0o700)
        report.write_text("{}\n", encoding="utf-8")
        report.chmod(0o600)
        scenarios: list[dict[str, object]] = []
        scenario_digests: dict[str, str] = {}
        lifecycle = {
            "client_exit_status": 0,
            "client_exited_after_server": True,
            "mode": "application-exit",
            "server_exited_after_application": True,
        }
        lifecycle_checks = live_run.lifecycle_boundary_checks(
            "application-exit",
            lifecycle,
        )
        for policy in live_run.CLIPBOARD_POLICIES:
            name = f"clipboard-{policy}"
            scenario_root = report.parent / name
            scenario_root.mkdir()
            scenario_root.chmod(0o700)
            artifact = scenario_root / "evidence.txt"
            artifact.write_text("digest-only clipboard evidence\n", encoding="utf-8")
            artifact.chmod(0o600)
            interaction = clipboard_interaction(policy)
            scenario: dict[str, object] = {
                "application": "clipboard",
                "artifact_collection_passed": True,
                "artifact_sha256": {
                    "evidence.txt": job.sha256_file(artifact),
                },
                "classification": {
                    "boundaries": {
                        "interaction": interaction["checks"],
                        "lifecycle": lifecycle_checks,
                    }
                },
                "cleanup": {"passed": True},
                "client": {
                    "clipboard_options": list(
                        live_run.live_config.clipboard_options("client", policy)
                    )
                },
                "clipboard_policy": policy,
                "encoding": "rgb",
                "h264_client_policy": "strict",
                "interaction": interaction,
                "lifecycle": lifecycle,
                "lifecycle_profile": "application-exit",
                "name": name,
                "result": "passed",
                "server": {
                    "clipboard_options": list(
                        live_run.live_config.clipboard_options("server", policy)
                    )
                },
            }
            scenario_report = scenario_root / "report.json"
            scenario_report.write_text(
                json.dumps(scenario, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            scenario_report.chmod(0o600)
            scenarios.append(scenario)
            scenario_digests[name] = job.sha256_file(scenario_report)
        payload: dict[str, object] = {
            "application": "clipboard",
            "encoding": "rgb",
            "h264_client_policy": "strict",
            "lifecycle_profile": "application-exit",
            "scenario_report_sha256": scenario_digests,
            "scenarios": scenarios,
            "source": record["input_provenance"],
        }
        return payload, report

    def make_clipboard_fixture_artifacts(
        self,
        root: Path,
        policy: str,
    ) -> dict[str, object]:
        root.mkdir(parents=True)
        root.chmod(0o700)
        owner_pid = 101
        fixture_pid = 202
        monitor_pid = 303
        xpra_client_identity = client_identity()
        owner_xid = 401
        primary_owner_xid = 402
        root_xid = 501

        def summary(marker_id: str, *, matches: bool = True) -> dict[str, object]:
            observed = (
                live_run.clipboard_fixture_common.marker_text(marker_id)
                if matches
                else None
            )
            return live_run.clipboard_fixture_common.marker_summary(
                marker_id,
                observed,
            )

        owner_records = [
            clipboard_event(
                0,
                "owner-ready",
                backend="x11",
                clipboard_owner_xid=owner_xid,
                owner_valid=True,
                pid=owner_pid,
                primary_owner_xid=primary_owner_xid,
                **summary("one"),
            ),
            clipboard_event(
                1,
                "owner-updated",
                clipboard_owner_xid=owner_xid,
                previous_clipboard_owner_xid=owner_xid,
                previous_primary_owner_xid=primary_owner_xid,
                primary_owner_xid=primary_owner_xid,
                same_clipboard_owner_xid=True,
                same_primary_owner_xid=True,
                **summary("two"),
            ),
            clipboard_event(
                2,
                "owner-updated",
                clipboard_owner_xid=owner_xid,
                previous_clipboard_owner_xid=owner_xid,
                previous_primary_owner_xid=primary_owner_xid,
                primary_owner_xid=primary_owner_xid,
                same_clipboard_owner_xid=True,
                same_primary_owner_xid=True,
                **summary("one"),
            ),
            clipboard_event(3, "owner-command-accepted", operation="quit"),
            clipboard_event(
                4,
                "owner-stopping",
                clipboard_owner_xid=owner_xid,
                marker_id="one",
                pid=owner_pid,
                primary_owner_xid=primary_owner_xid,
            ),
        ]
        forward = policy in {"both", "to-server"}
        wayland_records = [
            clipboard_event(
                0,
                "ready",
                backend="wayland",
                pid=fixture_pid,
                title=live_run.CLIPBOARD_FIXTURE_TITLE,
            )
        ]
        sequence = 1
        for request_id, marker_id in enumerate(("one", "two", "one"), 1):
            wayland_records.extend(
                (
                    clipboard_event(
                        sequence,
                        "paste-requested",
                        command_id=request_id,
                        marker_id=marker_id,
                        request_id=request_id,
                    ),
                    clipboard_event(
                        sequence + 1,
                        "paste-result",
                        command_id=request_id,
                        request_id=request_id,
                        within_entry_bound=forward,
                        **summary(marker_id, matches=forward),
                    ),
                )
            )
            sequence += 2
        wayland_records.extend(
            (
                clipboard_event(
                    7,
                    "owner-armed",
                    command_id=4,
                    marker_id="three",
                ),
                clipboard_event(
                    8,
                    "owner-input",
                    command_id=4,
                    keyval=65477,
                ),
                clipboard_event(
                    9,
                    "owner-set",
                    command_id=4,
                    **summary("three"),
                ),
                clipboard_event(
                    10,
                    "owner-confirmed",
                    command_id=4,
                    **summary("three"),
                ),
                clipboard_event(11, "escape-received"),
                clipboard_event(12, "closed", pid=fixture_pid),
            )
        )
        reverse_marker = "three" if policy == "both" else "one"
        reverse_owner_xid = 601 if policy == "both" else owner_xid
        notification_pairs = [(601, owner_xid), (701, owner_xid)]
        if policy == "both":
            notification_pairs.append((801, reverse_owner_xid))
            notification_pairs.append((801, 0))
        notification_values = [
            {
                "owner_xid": notification_owner,
                "selection_is_clipboard": True,
                "selection_timestamp": timestamp,
                "send_event": False,
                "subtype": 0,
                "timestamp": timestamp + 1,
                "window_xid": root_xid,
            }
            for timestamp, notification_owner in notification_pairs
        ]
        xfixes_records = [
            clipboard_event(
                0,
                "monitor-ready",
                pid=monitor_pid,
                event_base=87,
                owner_before_xid=owner_xid,
                root_xid=root_xid,
                subscribed_window_xids=[root_xid],
            ),
            *(
                clipboard_event(
                    index,
                    "xfixes-selection-notify",
                    **values,
                )
                for index, values in enumerate(notification_values, 1)
            ),
            clipboard_event(
                len(notification_values) + 1,
                "monitor-result",
                pid=monitor_pid,
                event_count=len(notification_values),
                events=notification_values,
                overflow=False,
                owner_after_xid=0 if policy == "both" else owner_xid,
                stop_requested=True,
                stop_requested_ns=1_046_000_000,
                drained_ns=1_046_500_000,
                subscribed_window_xids=[root_xid],
            ),
        ]
        owner_times = (1, 12, 22, 50, 51)
        wayland_times = (3, 6, 7, 16, 17, 26, 27, 30, 31, 32, 33, 40, 41)
        monitor_times = (10, 13, 23, 34, 42, 47) if policy == "both" else (10, 13, 23, 47)
        for records, times in ((owner_records, owner_times), (wayland_records, wayland_times),
                               (xfixes_records, monitor_times)):
            for record, value in zip(records, times, strict=True):
                record["monotonic_ns"] = 1_000_000_000 + value * 1_000_000
        stdout_records = {
            "clipboard-consumer-initial.stdout": [
                clipboard_conversion("one", owner_xid)
            ],
            "clipboard-consumer-repeat.stdout": [
                clipboard_conversion("one", owner_xid)
            ],
            "clipboard-consumer-reverse.stdout": [
                clipboard_conversion(reverse_marker, reverse_owner_xid)
            ],
            "clipboard-consumer-updated.stdout": [
                clipboard_conversion("two", owner_xid)
            ],
            "clipboard-fixture.stdout": wayland_records,
            "clipboard-monitor.stdout": xfixes_records,
            "clipboard-owner.stdout": owner_records,
        }
        for name, value in (("initial", 2), ("updated", 15), ("repeat", 25), ("reverse", 35)):
            stdout_records[f"clipboard-consumer-{name}.stdout"][0]["monotonic_ns"] = (
                1_000_000_000 + value * 1_000_000
            )
        phase_records = []
        log_parts = []
        end = 0
        for index, (name, value) in enumerate(zip(("initial", "updated", "repeat", "reverse"),
                                                 (5, 14, 24, 36), strict=True)):
            text = f"phase {name}\n"
            if name == "reverse":
                text += "keyboard event: wid=1, keyname='F8', True, typedict({})\n"
            if forward or name == "reverse":
                text += f"emit('selection', {100 + index}) callbacks=()\n"
            payload = text.encode("utf-8")
            phase_records.append({"name": name, "log_range": [end, end + len(payload)],
                                  "observed_ns": 1_000_000_000 + value * 1_000_000})
            end += len(payload)
            log_parts.append(payload)
        (root / "server.stderr").write_bytes(b"".join(log_parts))
        (root / "server.stderr").chmod(0o600)
        (root / "client.exit").write_text("0\n", encoding="ascii")
        (root / "client.exit").chmod(0o600)
        live_run.replace_private_json(root / live_run.CLIPBOARD_TRANSITIONS_ARTIFACT, {
            "schema": 1, "phases": phase_records, "client_exit_observed_ns": 1_044_000_000,
        })
        for name, records in stdout_records.items():
            path = root / name
            path.write_text(clipboard_jsonl(records), encoding="utf-8")
            path.chmod(0o600)
        for name, pid in {
            "client.pid": xpra_client_identity["pid"],
            "clipboard-fixture.pid": fixture_pid,
            "clipboard-monitor.pid": monitor_pid,
            "clipboard-owner.pid": owner_pid,
        }.items():
            path = root / name
            path.write_text(f"{pid}\n", encoding="ascii")
            path.chmod(0o600)
        for stem in (
            "clipboard-consumer-initial",
            "clipboard-consumer-repeat",
            "clipboard-consumer-reverse",
            "clipboard-consumer-updated",
            "clipboard-fixture",
            "clipboard-monitor",
            "clipboard-owner",
        ):
            for suffix, contents in (("exit", "0\n"), ("stderr", "")):
                path = root / f"{stem}.{suffix}"
                path.write_text(contents, encoding="ascii")
                path.chmod(0o600)
        live_run.replace_private_json(
            root / live_run.CLIPBOARD_CLIENT_SURVIVAL_ARTIFACT,
            {
                "after": xpra_client_identity,
                "before": xpra_client_identity,
                "schema": 1,
            },
        )
        return live_run._clipboard_evidence_from_artifacts(root, policy)

    def make_subsurface_fixture_artifacts(
        self, root: Path, *, coalesced: bool = False,
        startup_captures: tuple[int, int] = (1, 1),
    ) -> dict[str, object]:
        root.mkdir(parents=True)
        root.chmod(0o700)
        parent_wids = {"primary": 1, "secondary": 5}
        child_wids = {"lower": 2, "upper": 3, "reparented-upper": 3}
        role_ids = {**parent_wids, **child_wids}
        role_wires = live_run._subsurface_role_wires(parent_wids, child_wids)
        fixture_pid = 202
        primary_extra, secondary_extra = (count - 1 for count in startup_captures)
        sequence_offset = 2 * primary_extra + secondary_extra

        def private_text(path: Path, value: str) -> None:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_text(value, encoding="utf-8")
            path.chmod(0o600)

        def private_bytes(path: Path, value: bytes) -> None:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_bytes(value)
            path.chmod(0o600)

        # Actual fixture patterns are cross-checked against compiled C below;
        # synthetic protocol evidence reuses that independently bound oracle.
        primary = live_run._subsurface_fixture_image("primary")
        secondary = live_run._subsurface_fixture_image("secondary")
        lower_one = live_run._subsurface_fixture_image("lower-one")
        lower_two = live_run._subsurface_fixture_image("lower-two")
        lower_three = live_run._subsurface_fixture_image("lower-three")
        lower_four = live_run._subsurface_fixture_image("lower-four")
        continuous_one = live_run._subsurface_fixture_image("lower-continuous-one")
        upper = live_run._subsurface_fixture_image("upper")
        source_images = {
            "primary": primary,
            "secondary": secondary,
            "lower-one": lower_one,
            "lower-two": lower_two,
            "lower-three": lower_three,
            "lower-four": lower_four,
            "lower-continuous-one": continuous_one,
            "upper": upper,
        }
        update_groups: dict[int, int] = {
            wid: 100 * (index + 1)
            for index, wid in enumerate(
                (*parent_wids.values(), *dict.fromkeys(child_wids.values()))
            )
        }
        packet_records: dict[int, dict[str, object]] = {}

        def save_update(
            role: str,
            sequence: int,
            geometry: tuple[int, int, int, int],
            image_name: str,
            *,
            reset: tuple[int, int, int, int] | None = None,
            composite: bool,
            transaction: tuple[int, int, int, int, int] | None = None,
            source_origin: tuple[int, int] = (0, 0),
            startup: bool = False,
        ) -> dict[str, object]:
            if not startup:
                sequence += sequence_offset
                if transaction is not None:
                    transaction = (transaction[0] + primary_extra, *transaction[1:])
            source_wid = {
                **parent_wids,
                **child_wids,
            }[role]
            group = update_groups[source_wid]
            update_groups[source_wid] += 1
            group_root = root / "screen-updates" / str(source_wid) / str(group)
            group_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            for private_directory in (
                root / "screen-updates",
                root / "screen-updates" / str(source_wid),
                group_root,
            ):
                private_directory.chmod(0o700)
            child = role in live_run.SUBSURFACE_CHILD_ROLES
            encoding = "rgb32" if child or composite else "rgb24"
            child_formats = tuple(sorted(live_run.SUBSURFACE_CHILD_FORMATS))
            opaque_composite_formats = tuple(
                value
                for value in sorted(live_run.SUBSURFACE_COMPOSITE_FORMATS)
                if value.endswith("X")
            )
            rgb_format = (
                child_formats[sequence % len(child_formats)]
                if child
                else opaque_composite_formats[sequence % len(opaque_composite_formats)]
                if composite
                else min(live_run.SUBSURFACE_BASELINE_RGB24_FORMATS)
            )
            options: dict[str, object] = {"rgb_format": rgb_format}
            if not composite:
                options.update({"backing-epoch": 0, "flush": 0})
            if composite:
                if transaction is None:
                    raise AssertionError("composite update requires a transaction")
                (
                    transaction_id,
                    stage_index,
                    stage_count,
                    topology_epoch,
                    backing_epoch,
                ) = transaction
                options["subsurface-composite"] = live_run.SUBSURFACE_COMPOSITE_MODE
                options.update(
                    {
                        "flush": stage_count - stage_index - 1,
                        "subsurface-backing-epoch": backing_epoch,
                        "subsurface-stage-count": stage_count,
                        "subsurface-stage-index": stage_index,
                        "subsurface-topology-epoch": topology_epoch,
                        "subsurface-transaction-id": transaction_id,
                    }
                )
            if reset is not None:
                options["subsurface-reset"] = list(reset)
            width, height = geometry[2:]
            source_x, source_y = source_origin
            full_source = source_images[image_name].convert("RGBA")
            if (
                source_x < 0
                or source_y < 0
                or source_x + width > full_source.width
                or source_y + height > full_source.height
            ):
                raise AssertionError("synthetic saved packet crop is invalid")
            source_image = full_source.crop(
                (source_x, source_y, source_x + width, source_y + height)
            )
            bytes_per_pixel = 4 if encoding == "rgb32" else 3
            stride = width * bytes_per_pixel + (4 if sequence % 2 else 0)
            payload = bytearray()
            pixels = source_image.tobytes()
            for row in range(height):
                for column in range(width):
                    offset = (row * width + column) * 4
                    red, green, blue, alpha = pixels[offset : offset + 4]
                    channels = {
                        "BGR": (blue, green, red),
                        "BGRA": (blue, green, red, alpha),
                        "BGRX": (blue, green, red, 0x5A),
                        "RGBA": (red, green, blue, alpha),
                        "RGBX": (red, green, blue, 0xA5),
                    }[rgb_format]
                    payload.extend(channels)
                payload.extend(b"\xC3" * (stride - width * bytes_per_pixel))
            packet = {
                "encoding": encoding,
                "file": f"0.{encoding}",
                "h": geometry[3],
                "options": options,
                "sequence": sequence,
                "stride": stride,
                "w": geometry[2],
                "x": geometry[0],
                "y": geometry[1],
            }
            info_path = group_root / "0.info"
            private_text(info_path, json.dumps(packet, sort_keys=True))
            payload_path = group_root / f"0.{encoding}"
            private_bytes(payload_path, bytes(payload))
            packet_records[sequence] = {
                **packet,
                "role": role,
                "source_wid": source_wid,
                "wire_wid": role_wires[role],
            }
            return {
                "packet_info": info_path.relative_to(root).as_posix(),
                "packet_info_sha256": live_run.sha256_file(info_path),
                "packet_payload": payload_path.relative_to(root).as_posix(),
                "payload_bytes": len(payload),
                "payload_sha256": live_run.sha256_file(payload_path),
                "role": role,
                "sequences": [sequence],
            }

        for index in range(primary_extra):
            save_update(
                "primary", index * 2 + 1,
                (0, 0, *live_run.SUBSURFACE_PARENT_DIMENSIONS["primary"]),
                "primary", reset=live_run.SUBSURFACE_TRANSACTION_RESETS["initial"],
                composite=True, transaction=(index + 1, 0, 2, 1, 0), startup=True,
            )
            save_update(
                "lower", index * 2 + 2,
                live_run.SUBSURFACE_PHASE_GEOMETRIES[("initial", "lower")],
                "lower-one", composite=True,
                transaction=(index + 1, 1, 2, 1, 0), startup=True,
            )
        for index in range(secondary_extra):
            save_update(
                "secondary", 2 * primary_extra + index + 1,
                (0, 0, *live_run.SUBSURFACE_PARENT_DIMENSIONS["secondary"]),
                "secondary", composite=False, startup=True,
            )

        parent_sources = {
            "primary": {
                key: value
                for key, value in save_update(
                    "primary",
                    1,
                    (0, 0, *live_run.SUBSURFACE_PARENT_DIMENSIONS["primary"]),
                    "primary",
                    reset=live_run.SUBSURFACE_TRANSACTION_RESETS["initial"],
                    composite=True,
                    transaction=(1, 0, 2, 1, 0),
                ).items()
                if key != "role"
            },
            "secondary": {
                key: value
                for key, value in save_update(
                    "secondary",
                    2,
                    (0, 0, *live_run.SUBSURFACE_PARENT_DIMENSIONS["secondary"]),
                    "secondary",
                    composite=False,
                ).items()
                if key != "role"
            },
        }
        phase_payloads = {
            "initial": (
                ("lower", 3, "lower-one"),
            ),
            "changed": (
                ("primary", 4, "primary"),
                ("lower", 5, "lower-two"),
            ),
            "restored": (
                ("primary", 6, "primary"),
                ("lower", 7, "lower-one"),
            ),
            "moved": (
                ("primary", 8, "primary"),
                ("lower", 9, "lower-one"),
            ),
            "stacked": (
                ("primary", 10, "primary"),
                ("lower", 11, "lower-one"),
                ("upper", 12, "upper"),
            ),
            "lower-updated": (
                ("primary", 13, "primary"),
                ("lower", 14, "lower-two"),
                ("upper", 15, "upper"),
            ),
            "lower-frame-one": (
                ("primary", 16, "primary"),
                ("lower", 17, "lower-three"),
                ("upper", 18, "upper"),
            ),
            "lower-frame-two": (
                ("primary", 19, "primary"),
                ("lower", 20, "lower-four"),
                ("upper", 21, "upper"),
            ),
            "lower-destroyed": (
                ("primary", 31, "primary"),
                ("upper", 32, "upper"),
            ),
            "upper-detached": (
                ("primary", 33, "primary"),
            ),
            "reparented": (
                ("secondary", 34, "secondary"),
                ("reparented-upper", 35, "upper"),
            ),
        }
        phases: dict[str, dict[str, object]] = {}
        topology_epochs = {
            "initial": 1,
            "changed": 1,
            "restored": 1,
            "moved": 2,
            "stacked": 3,
            "lower-updated": 3,
            "lower-frame-one": 3,
            "lower-frame-two": 3,
            "lower-destroyed": 4,
            "upper-detached": 5,
            "reparented": 6,
        }
        continuous_capture_count = 3
        continuous_generation_count = 5 if coalesced else 3
        transaction_ids = {
            phase: index
            + (
                continuous_capture_count
                if phase
                in ("lower-destroyed", "upper-detached", "reparented")
                else 0
            )
            for index, phase in enumerate(live_run.SUBSURFACE_PHASES, start=1)
        }
        for phase in live_run.SUBSURFACE_PHASES:
            transaction_id = transaction_ids[phase]
            specs = phase_payloads[phase]
            stage_count = len(specs) + (1 if phase == "initial" else 0)
            first_stage = 1 if phase == "initial" else 0
            streams = []
            for stage_index, (role, sequence, image_name) in enumerate(
                specs,
                start=first_stage,
            ):
                streams.append(
                    save_update(
                        role,
                        sequence,
                        live_run.SUBSURFACE_PHASE_GEOMETRIES[(phase, role)],
                        image_name,
                        reset=(
                            live_run.SUBSURFACE_TRANSACTION_RESETS[phase]
                            if stage_index == 0
                            else None
                        ),
                        composite=True,
                        source_origin=live_run.SUBSURFACE_PHASE_SOURCE_ORIGINS[
                            (phase, role)
                        ],
                        transaction=(
                            transaction_id,
                            stage_index,
                            stage_count,
                            topology_epochs[phase],
                            0,
                        ),
                    )
                )
            phases[phase] = {"streams": streams}

        continuous_specs = (
            ("primary", "primary"),
            ("lower", "lower-three"),
            ("upper", "upper"),
        )
        sequence = 22
        for generation in range(1, continuous_capture_count + 1):
            lower_image = "lower-continuous-one" if generation % 2 else "lower-four"
            for stage_index, (role, default_image) in enumerate(continuous_specs):
                image_name = lower_image if role == "lower" else default_image
                save_update(
                    role,
                    sequence,
                    live_run.SUBSURFACE_CONTINUOUS_GEOMETRY,
                    image_name,
                    reset=(
                        live_run.SUBSURFACE_CONTINUOUS_GEOMETRY
                        if stage_index == 0
                        else None
                    ),
                    composite=True,
                    source_origin=live_run.SUBSURFACE_CONTINUOUS_SOURCE_ORIGINS[role],
                    transaction=(8 + generation, stage_index, 3, 3, 0),
                )
                sequence += 1

        events = [
            {
                "event": "ready",
                "lower_attach_count": 1,
                "lower_buffer_dimensions": list(
                    live_run.SUBSURFACE_LOWER_BUFFER_DIMENSIONS
                ),
                "lower_buffer_id": 201,
                "lower_buffer_scale": live_run.SUBSURFACE_LOWER_BUFFER_SCALE,
                "lower_commit_count": 1,
                "lower_dimensions": list(live_run.SUBSURFACE_LOWER_DIMENSIONS),
                "lower_offset": list(live_run.SUBSURFACE_INITIAL_OFFSET),
                "lower_state_id": 1,
                "lower_surface_id": 101,
                "monotonic_ns": 1_000,
                "parent_dimensions": list(
                    live_run.SUBSURFACE_PARENT_DIMENSIONS["primary"]
                ),
                "parents_alive": 2,
                "schema": live_run.SUBSURFACE_FIXTURE_SCHEMA,
                "secondary_parent_dimensions": list(
                    live_run.SUBSURFACE_PARENT_DIMENSIONS["secondary"]
                ),
                "sequence": 0,
            },
            {
                "event": "lower-state",
                "lower_attach_count": 2,
                "lower_buffer_id": 203,
                "lower_buffer_scale": live_run.SUBSURFACE_LOWER_BUFFER_SCALE,
                "lower_commit_count": 2,
                "lower_state_id": 2,
                "lower_surface_id": 101,
                "monotonic_ns": 2_000,
                "schema": live_run.SUBSURFACE_FIXTURE_SCHEMA,
                "sequence": 1,
                "update_index": 1,
                "upper_attach_count": 0,
                "upper_commit_count": 0,
            },
            {
                "event": "lower-state",
                "lower_attach_count": 3,
                "lower_buffer_id": 204,
                "lower_buffer_scale": live_run.SUBSURFACE_LOWER_BUFFER_SCALE,
                "lower_commit_count": 3,
                "lower_state_id": 1,
                "lower_surface_id": 101,
                "monotonic_ns": 3_000,
                "schema": live_run.SUBSURFACE_FIXTURE_SCHEMA,
                "sequence": 2,
                "update_index": 2,
                "upper_attach_count": 0,
                "upper_commit_count": 0,
            },
            {
                "event": "lower-moved",
                "from_offset": list(live_run.SUBSURFACE_INITIAL_OFFSET),
                "lower_attach_count": 3,
                "lower_buffer_scale": live_run.SUBSURFACE_LOWER_BUFFER_SCALE,
                "lower_commit_count": 3,
                "lower_surface_id": 101,
                "monotonic_ns": 4_000,
                "schema": live_run.SUBSURFACE_FIXTURE_SCHEMA,
                "sequence": 3,
                "to_offset": list(live_run.SUBSURFACE_MOVED_OFFSET),
            },
            {
                "event": "sibling-created",
                "lower_offset": list(live_run.SUBSURFACE_MOVED_OFFSET),
                "monotonic_ns": 5_000,
                "overlap": list(live_run.SUBSURFACE_OVERLAP_GEOMETRY),
                "schema": live_run.SUBSURFACE_FIXTURE_SCHEMA,
                "sequence": 4,
                "stacking": ["lower", "upper"],
                "upper_attach_count": 1,
                "upper_buffer_id": 202,
                "upper_buffer_transform": live_run.SUBSURFACE_UPPER_BUFFER_TRANSFORM,
                "upper_commit_count": 1,
                "upper_dimensions": list(live_run.SUBSURFACE_UPPER_DIMENSIONS),
                "upper_offset": list(live_run.SUBSURFACE_UPPER_OFFSET),
                "upper_precommitted_before_role": True,
                "upper_surface_id": 102,
            },
            {
                "event": "lower-updated-under-upper",
                "lower_attach_count": 4,
                "lower_buffer_id": 205,
                "lower_buffer_scale": live_run.SUBSURFACE_LOWER_BUFFER_SCALE,
                "lower_commit_count": 4,
                "lower_state_id": 2,
                "lower_surface_id": 101,
                "monotonic_ns": 6_000,
                "schema": live_run.SUBSURFACE_FIXTURE_SCHEMA,
                "sequence": 5,
                "update_index": 3,
                "upper_attach_count": 1,
                "upper_commit_count": 1,
            },
            {
                "event": "lower-frame-generation",
                "frame_callback_data": 41,
                "frame_callback_id": 301,
                "frame_done_count": 1,
                "generation_id": 1,
                "lower_attach_count": 5,
                "lower_buffer_id": 206,
                "lower_buffer_scale": live_run.SUBSURFACE_LOWER_BUFFER_SCALE,
                "lower_commit_count": 5,
                "lower_state_id": 3,
                "lower_surface_id": 101,
                "monotonic_ns": 7_000,
                "schema": live_run.SUBSURFACE_FIXTURE_SCHEMA,
                "sequence": 6,
                "update_index": 4,
                "upper_attach_count": 1,
                "upper_commit_count": 1,
            },
            {
                "event": "lower-frame-generation",
                "frame_callback_data": 42,
                "frame_callback_id": 302,
                "frame_done_count": 2,
                "generation_id": 2,
                "lower_attach_count": 6,
                "lower_buffer_id": 207,
                "lower_buffer_scale": live_run.SUBSURFACE_LOWER_BUFFER_SCALE,
                "lower_commit_count": 6,
                "lower_state_id": 4,
                "lower_surface_id": 101,
                "monotonic_ns": 8_000,
                "schema": live_run.SUBSURFACE_FIXTURE_SCHEMA,
                "sequence": 7,
                "update_index": 5,
                "upper_attach_count": 1,
                "upper_commit_count": 1,
            },
            {
                "continuous_buffer_ids": [208, 207],
                "continuous_generation_count": 0,
                "event": "continuous-start",
                "frame_callback_pending": True,
                "frame_callback_ready": False,
                "frame_done_count": 2,
                "lower_attach_count": 6,
                "lower_buffer_id": 207,
                "lower_commit_count": 6,
                "lower_state_id": 4,
                "lower_surface_id": 101,
                "lower_update_count": 5,
                "monotonic_ns": 9_000,
                "producer_active": True,
                "schema": live_run.SUBSURFACE_FIXTURE_SCHEMA,
                "sequence": 8,
                "upper_attach_count": 1,
                "upper_commit_count": 1,
            },
            *(
                {
                    "continuous_generation_id": generation,
                    "event": "continuous-generation",
                    "frame_callback_data": 42 + generation,
                    "frame_callback_id": 302 + generation,
                    "frame_done_count": 2 + generation,
                    "lower_attach_count": 6 + generation,
                    "lower_buffer_id": 208 if generation % 2 else 207,
                    "lower_buffer_scale": live_run.SUBSURFACE_LOWER_BUFFER_SCALE,
                    "lower_commit_count": 6 + generation,
                    "lower_state_id": 3 if generation % 2 else 4,
                    "lower_surface_id": 101,
                    "lower_update_count": 5 + generation,
                    "monotonic_ns": (9 + generation) * 1_000,
                    "producer_active": True,
                    "schema": live_run.SUBSURFACE_FIXTURE_SCHEMA,
                    "sequence": 8 + generation,
                    "upper_attach_count": 1,
                    "upper_commit_count": 1,
                }
                for generation in range(1, continuous_generation_count + 1)
            ),
            {
                "continuous_buffer_ids": [208, 207],
                "continuous_generation_count": continuous_generation_count,
                "event": "continuous-stop",
                "frame_done_count": 2 + continuous_generation_count,
                "lower_attach_count": 6 + continuous_generation_count,
                "lower_buffer_id": 208,
                "lower_commit_count": 6 + continuous_generation_count,
                "lower_state_id": 3,
                "lower_surface_id": 101,
                "lower_update_count": 5 + continuous_generation_count,
                "monotonic_ns": 13_000,
                "pending_callback_cancelled": True,
                "producer_active": False,
                "schema": live_run.SUBSURFACE_FIXTURE_SCHEMA,
                "sequence": 12,
                "terminal_callback_completed": False,
                "terminal_callback_data": 0,
                "terminal_callback_id": 999,
                "upper_attach_count": 1,
                "upper_commit_count": 1,
            },
            {
                "event": "sibling-click",
                "monotonic_ns": 14_000,
                "parent_coordinates": list(
                    live_run.SUBSURFACE_POINTER_PARENT_COORDINATES
                ),
                "schema": live_run.SUBSURFACE_FIXTURE_SCHEMA,
                "sequence": 13,
                "surface_coordinates": [
                    float(value)
                    for value in live_run.SUBSURFACE_POINTER_SURFACE_COORDINATES
                ],
                "target": "upper",
            },
            {
                "event": "lower-destroyed",
                "lower_update_count": 5 + continuous_generation_count,
                "monotonic_ns": 15_000,
                "parents_alive": 2,
                "schema": live_run.SUBSURFACE_FIXTURE_SCHEMA,
                "sequence": 14,
                "upper_alive": True,
            },
            {
                "event": "upper-detached",
                "lower_destroyed": True,
                "monotonic_ns": 16_000,
                "old_parent": "primary",
                "parents_alive": 2,
                "schema": live_run.SUBSURFACE_FIXTURE_SCHEMA,
                "sequence": 15,
                "upper_attach_count": 1,
                "upper_buffer_id": 202,
                "upper_buffer_transform": live_run.SUBSURFACE_UPPER_BUFFER_TRANSFORM,
                "upper_commit_count": 1,
                "upper_precommitted_before_role": True,
                "upper_surface_id": 102,
            },
            {
                "event": "upper-reparented",
                "monotonic_ns": 17_000,
                "new_offset": list(live_run.SUBSURFACE_REPARENT_OFFSET),
                "new_parent": "secondary",
                "parents_alive": 2,
                "schema": live_run.SUBSURFACE_FIXTURE_SCHEMA,
                "sequence": 16,
                "upper_attach_count": 1,
                "upper_buffer_id": 202,
                "upper_buffer_transform": live_run.SUBSURFACE_UPPER_BUFFER_TRANSFORM,
                "upper_commit_count": 1,
                "upper_precommitted_before_role": True,
                "upper_reattach_parent_committed": True,
                "upper_reattach_without_child_commit": True,
                "upper_surface_id": 102,
            },
            {
                "click_count": 1,
                "event": "exit",
                "lower_destroyed": True,
                "lower_update_count": 5 + continuous_generation_count,
                "monotonic_ns": 18_000,
                "parents_alive": 2,
                "schema": live_run.SUBSURFACE_FIXTURE_SCHEMA,
                "sequence": 17,
                "upper_reparented": True,
            },
        ]
        for index, event in enumerate(events):
            event["sequence"] = index
            event["monotonic_ns"] = (index + 1) * 50_000_000
        private_text(
            root / "subsurface-fixture.stdout",
            "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
        )
        private_text(root / "subsurface-fixture.stderr", "")
        private_text(root / "subsurface-fixture.pid", f"{fixture_pid}\n")
        private_text(root / "subsurface-fixture.exit", "0\n")
        click_time = next(event["monotonic_ns"] for event in events
                          if event["event"] == "sibling-click")
        live_run.replace_private_json(
            root / live_run.SUBSURFACE_POINTER_TIMING_ARTIFACT,
            {
                "completed_monotonic_ns": click_time + 500,
                "deadline_ns": live_run.SUBSURFACE_INPUT_DEADLINE_NS,
                "elapsed_ns": 1_000,
                "fixture_event_monotonic_ns": next(
                    event["monotonic_ns"]
                    for event in events
                    if event["event"] == "sibling-click"
                ),
                "schema": 1,
                "started_monotonic_ns": click_time - 500,
            },
        )

        child_packet_counts = {
            "initial": {"lower": 1},
            "changed": {"lower": 2},
            "restored": {"lower": 3},
            "moved": {"lower": 4},
            "stacked": {"lower": 5, "upper": 1},
            "lower-updated": {"lower": 6, "upper": 2},
            "lower-frame-one": {"lower": 7, "upper": 3},
            "lower-frame-two": {"lower": 8, "upper": 4},
            "lower-destroyed": {
                "upper": 5 + continuous_capture_count,
            },
            "upper-detached": {},
            "reparented": {"reparented-upper": 1},
        }
        next_sequences = {
            "initial": 4,
            "changed": 6,
            "restored": 8,
            "moved": 10,
            "stacked": 13,
            "lower-updated": 16,
            "lower-frame-one": 19,
            "lower-frame-two": 22,
            "lower-destroyed": 33,
            "upper-detached": 34,
            "reparented": 36,
        }

        def parent_info_lines(before_sequence: int) -> list[str]:
            values = []
            for role, wid in parent_wids.items():
                prefix = f"client.0.window.windows.{wid}.damage"
                count = sum(packet["role"] == role and sequence < before_sequence
                            for sequence, packet in packet_records.items())
                values.extend((f"{prefix}.ack-pending=0", f"{prefix}.encoding-pending=0",
                               f"{prefix}.packets_sent={count}"))
            return values

        for phase in live_run.SUBSURFACE_PHASES:
            expected_children = live_run._subsurface_expected_children(
                phase,
                parent_wids,
                child_wids,
            )
            children = {
                child_wids[role]: (
                    child_packet_counts[phase][role] + (primary_extra if role == "lower" else 0),
                    *expected_children[child_wids[role]],
                )
                for role, _parent_role, _offset in live_run.SUBSURFACE_PHASE_CHILD_LAYOUTS[
                    phase
                ]
            }
            active_sources = len(live_run.SUBSURFACE_PARENT_ROLES) + len(children)
            next_sequence = next_sequences[phase] + sequence_offset
            lines = [
                f"windows.{parent_wids['primary']}.title={live_run.SUBSURFACE_FIXTURE_TITLE}",
                (
                    f"windows.{parent_wids['secondary']}.title="
                    f"{live_run.SUBSURFACE_REPARENT_TARGET_TITLE}"
                ),
                f"client.0.window.damage.next-packet-sequence={next_sequence}",
                "client.0.window.damage.ack-owners=0",
                "client.0.window.damage.subsurface-pending=0",
                "client.0.window.damage.subsurface-inflight=0",
                f"client.0.window.damage.active-pixel-sources={active_sources}",
                *parent_info_lines(next_sequence),
            ]
            for child_wid, (packets_sent, parent_wid, offset) in children.items():
                prefix = (
                    f"client.0.window.windows.{parent_wid}."
                    f"subsurfaces.{child_wid}"
                )
                lines.extend(
                    (
                        f"{prefix}.offset={tuple(offset)!r}",
                        f"{prefix}.info.damage.ack-pending=0",
                        f"{prefix}.info.damage.encoding-pending=0",
                        f"{prefix}.info.damage.packets_sent={packets_sent}",
                    )
                )
            private_text(
                root / live_run.SUBSURFACE_INFO_ARTIFACTS[phase],
                "\n".join(lines) + "\n",
            )

        continuous_children = {
            child_wids["lower"]: (
                8 + primary_extra + continuous_capture_count,
                parent_wids["primary"],
                list(live_run.SUBSURFACE_MOVED_OFFSET),
            ),
            child_wids["upper"]: (
                4 + continuous_capture_count,
                parent_wids["primary"],
                list(live_run.SUBSURFACE_UPPER_OFFSET),
            ),
        }
        continuous_lines = [
            f"windows.{parent_wids['primary']}.title={live_run.SUBSURFACE_FIXTURE_TITLE}",
            (
                f"windows.{parent_wids['secondary']}.title="
                f"{live_run.SUBSURFACE_REPARENT_TARGET_TITLE}"
            ),
            f"client.0.window.damage.next-packet-sequence={31 + sequence_offset}",
            "client.0.window.damage.ack-owners=0",
            "client.0.window.damage.active-pixel-sources=4",
            "client.0.window.damage.subsurface-pending=0",
            "client.0.window.damage.subsurface-inflight=0",
            *parent_info_lines(31 + sequence_offset),
        ]
        for child_wid, (packets_sent, parent_wid, offset) in continuous_children.items():
            child_prefix = (
                f"client.0.window.windows.{parent_wid}.subsurfaces.{child_wid}"
            )
            continuous_lines.extend(
                (
                    f"{child_prefix}.offset={tuple(offset)!r}",
                    f"{child_prefix}.info.damage.ack-pending=0",
                    f"{child_prefix}.info.damage.encoding-pending=0",
                    f"{child_prefix}.info.damage.packets_sent={packets_sent}",
                )
            )
        private_text(
            root / live_run.SUBSURFACE_CONTINUOUS_INFO_ARTIFACT,
            "\n".join(continuous_lines) + "\n",
        )

        child_sequences = tuple(
            sequence
            for sequence, packet in sorted(packet_records.items())
            if packet["role"] in live_run.SUBSURFACE_CHILD_ROLES
        )
        server_lines: list[str] = []
        map_barriers = []
        for role, other in (("secondary", "primary"), ("primary", "secondary")):
            wid = parent_wids[role]
            server_lines.extend((
                f"_focus({wid}, ()) current focus={parent_wids[other]}",
                f"focus: wid={wid:#x}, state=True, window=WaylandWindow({wid:#x}), surface=Surface({wid} : fixture)",
            ))
            map_barriers.append({
                "role": role, "wire_wid": wid, "client_xid": str(4096 + wid),
                "server_log_start": 0,
            })
        for record in map_barriers:
            record["server_log_end"] = len(("\n".join(server_lines) + "\n").encode())
        live_run.replace_private_json(
            root / live_run.SUBSURFACE_STARTUP_BARRIERS_ARTIFACT,
            {"schema": 1, "activation_order": ["secondary", "primary"], "parents": map_barriers},
        )
        secondary_wid = parent_wids["secondary"]
        secondary_width, secondary_height = live_run.SUBSURFACE_PARENT_DIMENSIONS["secondary"]
        for request in range(2):
            event = (
                "using existing 1 delayed regions created 1ms ago"
                if request and secondary_extra == 0
                else f"scheduling batching expiry for sequence {request + 1} in 0 ms"
            )
            server_lines.append(
                f"do_damage(0, 0, {secondary_width}, {secondary_height}, {{}}) "
                f"wid={secondary_wid:#x}, {event}"
            )
            if request or secondary_extra:
                server_lines.append(
                    f"process_damage_region: wid={secondary_wid:#x}, sequence={request + 2}, "
                    f"adding pixel data to encode queue ( {secondary_width}x{secondary_height} - rgb32)"
                )
        live_run.replace_private_json(
            root / live_run.SUBSURFACE_STARTUP_DAMAGE_ARTIFACT,
            {"schema": 1, "server_log_end": len(("\n".join(server_lines) + "\n").encode())},
        )
        for sequence in child_sequences:
            packet = packet_records[sequence]
            server_lines.extend(
                (
                    (
                        f"subsurface draw packet sequence {sequence} from source "
                        f"window 0x{packet['source_wid']:x} published as wire window "
                        f"0x{packet['wire_wid']:x} using {packet['encoding']}"
                    ),
                    (
                        f"draw acknowledgement sequence {sequence} for wire window "
                        f"0x{packet['wire_wid']:x} routed to subsurface window "
                        f"0x{packet['source_wid']:x}"
                    ),
                )
            )
        server_lines.extend(
            (
                (
                    "Wayland pointer target "
                    f"root={parent_wids['primary']:#x} "
                    f"surface={child_wids['upper']:#x} "
                    "local="
                    f"{live_run.SUBSURFACE_POINTER_SURFACE_COORDINATES[0]:.3f},"
                    f"{live_run.SUBSURFACE_POINTER_SURFACE_COORDINATES[1]:.3f}"
                ),
                (
                    "click(1, True, "
                    f"{live_run.SUBSURFACE_POINTER_SURFACE_COORDINATES!r})"
                ),
                (
                    "click(1, False, "
                    f"{live_run.SUBSURFACE_POINTER_SURFACE_COORDINATES!r})"
                ),
            )
        )
        private_text(root / "server.stderr", "\n".join(server_lines) + "\n")
        client_lines: list[str] = []
        for sequence in sorted(packet_records):
            packet = packet_records[sequence]
            client_lines.append(
                f"process_draw: 14 bytes for window {packet['wire_wid']}, sequence "
                f"{sequence:8d}, {packet['w']}x{packet['h']} at "
                f"{packet['x']},{packet['y']} using {packet['encoding']} "
                "encoding with options=typedict({})"
            )
        client_lines.extend(
            (
                (
                    "_button_action(1, fixture, True) "
                    f"wid=0x{parent_wids['primary']:x} / focus=1 / "
                    f"window wid=0x{parent_wids['primary']:x}"
                ),
                (
                    "_button_action(1, fixture, False) "
                    f"wid=0x{parent_wids['primary']:x} / focus=1 / "
                    f"window wid=0x{parent_wids['primary']:x}"
                ),
            )
        )
        private_text(root / "client.stdout", "\n".join(client_lines) + "\n")
        private_text(root / "client.stderr", "")

        for phase in live_run.SUBSURFACE_PHASES:
            captures = {
                "primary": primary.convert("RGBA"),
                "secondary": secondary.convert("RGBA"),
            }
            phase_images = {
                role: source_images[image_name]
                for role, _sequence, image_name in phase_payloads[phase]
            }
            for child_role, parent_role, offset in (
                live_run.SUBSURFACE_PHASE_CHILD_LAYOUTS[phase]
            ):
                captures[parent_role] = live_run._subsurface_source_over(
                    captures[parent_role],
                    phase_images[child_role],
                    offset,
                )
            for role, image in captures.items():
                path = root / live_run.subsurface_client_rgb_artifact(role, phase)
                image.save(path, format="PNG")
                path.chmod(0o600)

        continuous_backings = {
            "primary": primary.convert("RGBA"),
            "secondary": secondary.convert("RGBA"),
        }
        continuous_backings["primary"] = live_run._subsurface_source_over(
            continuous_backings["primary"],
            lower_four,
            live_run.SUBSURFACE_MOVED_OFFSET,
        )
        continuous_backings["primary"] = live_run._subsurface_source_over(
            continuous_backings["primary"],
            upper,
            live_run.SUBSURFACE_UPPER_OFFSET,
        )
        continuous_x, continuous_y, continuous_width, continuous_height = (
            live_run.SUBSURFACE_CONTINUOUS_GEOMETRY
        )
        for generation in range(1, continuous_capture_count + 1):
            lower_image = continuous_one if generation % 2 else lower_four
            continuous_backings["primary"].paste(
                live_run.Image.new(
                    "RGBA",
                    (continuous_width, continuous_height),
                    (0, 0, 0, 0),
                ),
                (continuous_x, continuous_y),
            )
            for role, source in (
                ("primary", primary.convert("RGBA")),
                ("lower", lower_image),
                ("upper", upper),
            ):
                source_x, source_y = live_run.SUBSURFACE_CONTINUOUS_SOURCE_ORIGINS[
                    role
                ]
                crop = source.crop(
                    (
                        source_x,
                        source_y,
                        source_x + continuous_width,
                        source_y + continuous_height,
                    )
                )
                continuous_backings["primary"] = live_run._subsurface_source_over(
                    continuous_backings["primary"],
                    crop,
                    (continuous_x, continuous_y),
                )
        for role, image in continuous_backings.items():
            path = root / live_run.subsurface_client_rgb_artifact(
                role,
                live_run.SUBSURFACE_CONTINUOUS_FINAL_PHASE,
            )
            image.save(path, format="PNG")
            path.chmod(0o600)

        source_updates = {
            role: live_run._subsurface_saved_updates(root, role_ids[role])
            for role in ("primary", "secondary", "lower", "upper")
        }
        active_snapshot = live_run._subsurface_continuous_transaction_snapshot(
            root,
            source_updates,
            {**parent_wids, **child_wids},
            after_sequence=21 + sequence_offset,
            before_sequence=29 + sequence_offset,
        )
        drained_snapshot = live_run._subsurface_continuous_transaction_snapshot(
            root,
            source_updates,
            {**parent_wids, **child_wids},
            after_sequence=21 + sequence_offset,
            before_sequence=31 + sequence_offset,
        )
        stop_event = next(event for event in events if event["event"] == "continuous-stop")
        live_run.replace_private_json(
            root / live_run.SUBSURFACE_CONTINUOUS_LIVENESS_ARTIFACT,
            {
                "active": {
                    "fixture_event_monotonic_ns": events[11]["monotonic_ns"],
                    "fixture_event_sequence": 11,
                    "fixture_generation_count": continuous_capture_count,
                    "fixture_process_alive": True,
                    "initial_fixture_generation_count": 2,
                    "observation_started_monotonic_ns": events[10]["monotonic_ns"] + 1,
                    "observed_monotonic_ns": events[11]["monotonic_ns"] + 250,
                    "packet_cut_before_sequence": 29 + sequence_offset,
                    "producer_active": True,
                    "snapshot": active_snapshot,
                    "stop_marker_absent": True,
                },
                "drained": {
                    "fixture_event_monotonic_ns": stop_event["monotonic_ns"],
                    "fixture_event_sequence": stop_event["sequence"],
                    "fixture_generation_count": continuous_generation_count,
                    "observed_monotonic_ns": stop_event["monotonic_ns"] + 250,
                    "producer_active": False,
                    "snapshot": drained_snapshot,
                },
                "schema": 3,
                "stop_requested_monotonic_ns": events[11]["monotonic_ns"] + 500,
            },
        )

        interaction: dict[str, object] = {
            "attempted": True,
            "checks": dict.fromkeys(live_run.SUBSURFACE_LIVE_CHECK_NAMES, False),
            "child_wids": child_wids,
            "evidence": {},
            "fixture_pid": fixture_pid,
            "parent_sources": parent_sources,
            "parent_wids": parent_wids,
            "phases": phases,
        }
        interaction["evidence"] = live_run.subsurface_artifact_observations(
            root,
            parent_wids=parent_wids,
            child_wids=child_wids,
            fixture_pid=fixture_pid,
            parent_sources=parent_sources,
            phases=phases,
        )
        interaction["checks"] = live_run.subsurface_interaction_checks(interaction)
        return interaction

    def freeze_prelaunch(self, run: str, *, job_id: str = JOB_ID) -> dict[str, object]:
        return {
            "application": "hardware",
            "background_supervisor_sha256": job.sha256_file(
                job.BACKGROUND_SUPERVISOR
            ),
            "completion": str(job.freeze_completion_path(run)),
            "freeze_owner": str(job.freeze_record_path(run)),
            "freeze_result": str(job.freeze_result_path(run)),
            "harness_sha256": job.harness_sha256(),
            "job_id": job_id,
            "kind": "input-freeze-prelaunch",
            "owner": job.OWNER,
            "process": {"pid": 12345, "start_ticks": "42"},
            "result": str(job.RESULT_ROOT / run),
            "run": run,
            "runner_sha256": job.sha256_file(job.RUNNER),
            "schema": 1,
            "selection": None,
            "staging": str(job.freeze_staging_path(run, job_id)),
            "supervisor_sha256": job.sha256_file(job.SUPERVISOR),
            "zed_directory": None,
        }

    def make_report(self, run: str, record: dict[str, object]) -> None:
        application = str(record["application"])
        lifecycle_profile = str(record["lifecycle"])
        report = job.result_path(run)
        report.parent.mkdir(parents=True)
        report.parent.chmod(0o700)
        inputs = report.parent / "inputs"
        inputs.mkdir()
        inputs.chmod(0o700)
        manifest = inputs / "manifest.json"
        manifest.write_text('{"input": "unit"}\n', encoding="utf-8")
        manifest.chmod(0o600)
        manifest_digest = job.sha256_file(manifest)
        checksums = inputs / "SHA256SUMS"
        checksums.write_text(
            f"{manifest_digest}  manifest.json\n",
            encoding="utf-8",
        )
        checksums.chmod(0o600)
        provenance = record["input_provenance"]
        provenance["input_manifest_sha256"] = manifest_digest
        provenance["input_tree_sha256"] = live_run.tree_sha256(inputs)
        scenario_root = report.parent / "default-alpha"
        scenario_root.mkdir()
        scenario_root.chmod(0o700)
        artifact = scenario_root / "evidence.txt"
        artifact.write_text("evidence\n", encoding="utf-8")
        artifact.chmod(0o600)
        application_activity: dict[str, object] = {}
        hardware: dict[str, object] = {}
        if application == "gtk" and lifecycle_profile in {"detach", "transport-loss"}:
            identity = interaction_identity()
            server = server_identity()
            identity_artifact = scenario_root / live_run.INTERACTION_IDENTITY_ARTIFACT
            identity_artifact.write_text(
                json.dumps(identity, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            identity_artifact.chmod(0o600)
            server_pid_artifact = scenario_root / "server.pid"
            server_pid_artifact.write_text(f"{server['pid']}\n", encoding="ascii")
            server_pid_artifact.chmod(0o600)
            application_activity.update(
                {
                    "process_alive": True,
                    "process_identity": copy.deepcopy(identity),
                }
            )
            hardware["application"] = {
                "argv": " ".join(identity["argv"]) + " ",
                "pid": identity["pid"],
            }
            lifecycle = {
                "application_exited_after_termination": True,
                "application_identity_at_capture": copy.deepcopy(identity),
                "application_identity_before_termination": copy.deepcopy(identity),
                "application_termination": {
                    "identity": copy.deepcopy(identity),
                    "pidfd": True,
                    "returncode": 0,
                    "server_identity": copy.deepcopy(server),
                    "server_pidfd": True,
                    "signal": "SIGTERM",
                },
                "mode": lifecycle_profile,
                "server_alive_before_application_termination": True,
                "server_exited_after_application": True,
                "server_identity_at_capture": copy.deepcopy(server),
                "server_identity_before_application_termination": copy.deepcopy(
                    server
                ),
                "server_pid": server["pid"],
            }
            if lifecycle_profile == "detach":
                lifecycle.update(
                    {
                        "application_identity_after_detach": copy.deepcopy(identity),
                        "application_survived_detach": True,
                        "client_exit_status": 0,
                        "client_exited_after_detach": True,
                        "detach_returncode": 0,
                        "server_identity_after_detach": copy.deepcopy(server),
                        "server_survived_detach": True,
                    }
                )
            else:
                lifecycle.update(
                    {
                        "application_identity_after_transport_loss": copy.deepcopy(
                            identity
                        ),
                        "application_survived_transport_loss": True,
                        "client_exit_status": 1,
                        "client_exited_after_transport_loss": True,
                        "server_identity_after_transport_loss": copy.deepcopy(server),
                        "server_survived_transport_loss": True,
                        "transport_disconnect_returncode": 0,
                    }
                )
        else:
            lifecycle = {
                "client_exit_status": 0,
                "client_exited_after_server": True,
                "mode": "application-exit",
                "server_exited_after_application": True,
            }
        artifact_sha256 = {
            path.relative_to(scenario_root).as_posix(): job.sha256_file(path)
            for path in scenario_root.iterdir()
            if path.is_file()
        }
        scenario = {
            "application": application,
            "application_activity": application_activity,
            "artifact_collection_passed": True,
            "artifact_sha256": artifact_sha256,
            "client": {
                "alpha_disabled": False,
                "network_options": list(
                    live_run.live_config.network_profile(
                        str(record["network_profile"])
                    ).client_options()
                )
            },
            "classification": {
                "boundaries": {
                    "lifecycle": live_run.lifecycle_boundary_checks(
                        lifecycle_profile,
                        lifecycle,
                    )
                }
            },
            "cleanup": {"passed": True},
            "encoding": "rgb",
            "hardware": hardware,
            "h264_client_policy": "strict",
            "lifecycle": lifecycle,
            "lifecycle_profile": lifecycle_profile,
            "name": "default-alpha",
            "network_profile": record["network_profile"],
            "result": "passed",
        }
        scenario_report = scenario_root / "report.json"
        scenario_report.write_text(
            json.dumps(scenario, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        scenario_report.chmod(0o600)
        report.write_text(
            json.dumps(
                {
                    "application": application,
                    "encoding": "rgb",
                    "h264_client_policy": "strict",
                    "lifecycle_profile": lifecycle_profile,
                    "network_profile": record["network_profile"],
                    "invocation": {
                        "alpha_scenarios": "default",
                        "application": application,
                        "h264_client_policy": "strict",
                        "job_id": JOB_ID,
                        "lifecycle": lifecycle_profile,
                        "network_profile": record["network_profile"],
                        "render_node": record["render_node"],
                        "run_id": run,
                        "selection": record["selection"],
                    },
                    "images": {
                        "client": {
                            "build_context_sha256": provenance[
                                "client_context_sha256"
                            ],
                            "id": "sha256:" + "1" * 64,
                            "labels": {
                                "io.xpra.fork-maintenance.context": provenance[
                                    "client_context_sha256"
                                ],
                                "io.xpra.fork-maintenance.owner": "live",
                                "io.xpra.fork-maintenance.role": "client-image",
                                "io.xpra.fork-maintenance.source": provenance["source_commit"],
                            },
                            "selection": "master",
                            "tag": "localhost/client:test",
                        },
                        "server": {
                            "build_context_sha256": provenance[
                                "server_context_sha256"
                            ],
                            "id": "sha256:" + "2" * 64,
                            "labels": {
                                "io.xpra.fork-maintenance.context": provenance[
                                    "server_context_sha256"
                                ],
                                "io.xpra.fork-maintenance.owner": "live",
                                "io.xpra.fork-maintenance.role": "server-image",
                                "io.xpra.fork-maintenance.source": provenance["source_commit"],
                            },
                            "selection": provenance["server_selection"],
                            "tag": "localhost/server:test",
                        },
                    },
                    "result": "passed",
                    "scenario_report_sha256": {
                        "default-alpha": job.sha256_file(scenario_report)
                    },
                    "scenarios": [scenario],
                    "source": {
                        "background_supervisor_sha256": record[
                            "background_supervisor_sha256"
                        ],
                        "harness_sha256": record["harness_sha256"],
                        "input_manifest_sha256": manifest_digest,
                        "input_provenance": {
                            key: value
                            for key, value in provenance.items()
                            if key
                            not in {
                                "input_manifest_sha256",
                                "input_tree_sha256",
                                "path",
                            }
                        },
                        "input_tree_sha256": provenance["input_tree_sha256"],
                        "archive_sha256": provenance["source_archive_sha256"],
                        "commit": provenance["source_commit"],
                        "fork_master": provenance["source_commit"],
                        "selection": {
                            "digest": provenance["server_selection_sha256"],
                            "name": provenance["server_selection"],
                            "resolution": {
                                "resolution_sha256": provenance[
                                    "server_selection_resolution_sha256"
                                ]
                            },
                        },
                        "supervisor_sha256": record["supervisor_sha256"],
                        "workflow_sha256": provenance["source_workflow_sha256"],
                        "zed_archive_sha256": provenance["zed_archive_sha256"],
                        "zed_sha256": provenance["zed_binary_sha256"],
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        report.chmod(0o600)

    def test_start_binds_profile_to_owned_process(self) -> None:
        args = Namespace(
            alpha_scenarios="default",
            application="hardware",
            encoding="h264",
            h264_client_policy="adaptive-alpha",
            lifecycle="application-exit",
            network_profile=job.DEFAULT_NETWORK_PROFILE,
            render_node=None,
            run="hardware-profile",
            selection="stacks/develop",
            zed_directory=None,
        )
        captured: list[dict[str, object]] = []
        captured_prelaunch: list[dict[str, object]] = []
        provenance = self.record(args.run)["input_provenance"]
        provenance["path"] = str(job.RESULT_ROOT / args.run / "inputs")
        provenance["zed_archive_sha256"] = None
        provenance["zed_binary_sha256"] = None
        provenance["server_selection"] = args.selection
        selected = live_run.PatchSelection(
            case_slugs=(),
            digest="a" * 64,
            kind="stack",
            name=args.selection,
            patches=(),
            required_gates=(),
            selector_digests=((args.selection, "a" * 64),),
            selectors=(args.selection,),
        )
        bound = Mock(server_context=Mock(selection=selected))

        def launch(**kwargs: object) -> dict[str, object]:
            captured.append(dict(kwargs))
            record = dict(kwargs["record"])
            if record.get("kind") == "input-freeze":
                captured_prelaunch.append(
                    json.loads(
                        job.freeze_prelaunch_path(args.run).read_text(encoding="utf-8")
                    )
                )
                harness = Path(record["staging"]) / "inputs" / "harness"
                for source in job.HARNESS_INPUTS:
                    destination = harness / source.relative_to(job.MAINTENANCE_ROOT)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(source.read_bytes())
            record["process"] = {"pid": 12345 + len(captured)}
            return record

        with (
            patch.object(job, "validate_selector"),
            patch.object(
                job.live_run,
                "resolve_patch_selection",
                return_value=selected,
            ),
            patch.object(job.background_job, "launch", side_effect=launch),
            patch.object(
                job.background_job,
                "wait_process",
                return_value={"state": "completed", "exit_code": 0},
            ),
            patch.object(job, "load_freeze_result", return_value=provenance),
            patch.object(job.live_run, "load_bound_inputs", return_value=bound),
            patch.object(job, "cleanup_freeze_state"),
        ):
            self.assertEqual(job.start(args), 0)
        self.assertEqual(len(captured), 2)
        record = captured[1]["record"]
        self.assertEqual(len(captured_prelaunch), 1)
        self.assertEqual(captured_prelaunch[0]["job_id"], record["job_id"])
        self.assertEqual(
            captured_prelaunch[0]["freeze_owner"],
            str(job.freeze_record_path(args.run)),
        )
        self.assertFalse(job.freeze_prelaunch_path(args.run).exists())
        freeze_argv = captured[0]["argv"]
        self.assertIn("_freeze", freeze_argv)
        self.assertEqual(record["application"], "hardware")
        self.assertEqual(record["network_profile"], args.network_profile)
        argv = captured[1]["argv"]
        self.assertEqual(argv[:2], [sys.executable, "-B"])
        self.assertEqual(
            Path(argv[2]),
            Path(provenance["path"])
            / "harness"
            / job.RUNNER.relative_to(job.MAINTENANCE_ROOT),
        )
        self.assertEqual(argv[argv.index("--application") + 1], "hardware")
        self.assertEqual(argv[argv.index("--lifecycle") + 1], "application-exit")
        self.assertEqual(
            argv[argv.index("--network-profile") + 1],
            args.network_profile,
        )
        self.assertIn("--bound-inputs", argv)
        self.assertNotIn("--zed-directory", argv)
        self.assertEqual(
            captured[1]["environment"]["XPRA_FORK_JOB_ID"],
            record["job_id"],
        )

    def test_start_rejects_invalid_frozen_admission_before_main_launch(self) -> None:
        selection_name = "cases/video-pipeline-cleanup-race"
        host_selection = live_run.PatchSelection(
            case_slugs=("video-pipeline-cleanup-race",),
            digest="a" * 64,
            kind="case",
            name=selection_name,
            patches=(),
            required_gates=("live-wayland-h264-hardware",),
            selector_digests=((selection_name, "a" * 64),),
            selectors=(selection_name,),
        )
        frozen_selection = live_run.PatchSelection(
            case_slugs=host_selection.case_slugs,
            digest="b" * 64,
            kind="case",
            name=selection_name,
            patches=(),
            required_gates=(),
            selector_digests=((selection_name, "b" * 64),),
            selectors=(selection_name,),
        )
        bound = Mock(server_context=Mock(selection=frozen_selection))

        def launch_recorder(
            records: list[dict[str, object]],
        ) -> object:
            def launch(**kwargs: object) -> dict[str, object]:
                record = dict(kwargs["record"])
                harness = Path(record["staging"]) / "inputs" / "harness"
                for source in job.HARNESS_INPUTS:
                    destination = harness / source.relative_to(job.MAINTENANCE_ROOT)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(source.read_bytes())
                record["process"] = {"pid": 12345}
                records.append(record)
                return record

            return launch

        for suffix, loader, message in (
            (
                "changed-gate",
                Mock(return_value=bound),
                "does not declare required gate live-wayland-h264-hardware",
            ),
            (
                "invalid-snapshot",
                Mock(side_effect=live_run.LabFailure("frozen selection is invalid")),
                "frozen selection is invalid",
            ),
        ):
            args = Namespace(
                alpha_scenarios="default",
                application="hardware",
                encoding="h264",
                h264_client_policy="adaptive-alpha",
                lifecycle="application-exit",
                network_profile=job.DEFAULT_NETWORK_PROFILE,
                render_node=None,
                run=f"frozen-admission-{suffix}",
                selection=selection_name,
                zed_directory=None,
            )
            provenance = self.record(args.run)["input_provenance"]
            provenance["path"] = str(job.RESULT_ROOT / args.run / "inputs")
            provenance["zed_archive_sha256"] = None
            provenance["zed_binary_sha256"] = None
            provenance["server_selection"] = args.selection
            launches: list[dict[str, object]] = []

            with (
                self.subTest(frozen_failure=suffix),
                patch.object(job, "validate_selector"),
                patch.object(
                    job.live_run,
                    "resolve_patch_selection",
                    return_value=host_selection,
                ),
                patch.object(
                    job.background_job,
                    "launch",
                    side_effect=launch_recorder(launches),
                ),
                patch.object(
                    job.background_job,
                    "wait_process",
                    return_value={"state": "completed", "exit_code": 0},
                ),
                patch.object(job, "load_freeze_result", return_value=provenance),
                patch.object(job.live_run, "load_bound_inputs", loader),
                patch.object(
                    job,
                    "freeze_process_state",
                    return_value={"state": "completed", "exit_code": 0},
                ),
                patch.object(job, "cleanup_freeze_state") as cleanup,
                self.assertRaisesRegex(job.JobError, message),
            ):
                job.start(args)

            self.assertEqual(len(launches), 1)
            cleanup.assert_called_once_with(
                launches[0],
                remove_input_directories=True,
            )
            self.assertFalse(job.record_path(args.run).exists())
            self.assertFalse(job.freeze_prelaunch_path(args.run).exists())

    def test_main_launch_retention_preserves_frozen_inputs_and_prelaunch(self) -> None:
        args = Namespace(
            alpha_scenarios="default",
            application="hardware",
            encoding="h264",
            h264_client_policy="adaptive-alpha",
            lifecycle="application-exit",
            network_profile=job.DEFAULT_NETWORK_PROFILE,
            render_node=None,
            run="retained-main-launch",
            selection="stacks/develop",
            zed_directory=None,
        )
        provenance = self.record(args.run)["input_provenance"]
        provenance["path"] = str(job.RESULT_ROOT / args.run / "inputs")
        provenance["zed_archive_sha256"] = None
        provenance["zed_binary_sha256"] = None
        provenance["server_selection"] = args.selection
        selected = live_run.PatchSelection(
            case_slugs=(),
            digest="a" * 64,
            kind="stack",
            name=args.selection,
            patches=(),
            required_gates=(),
            selector_digests=((args.selection, "a" * 64),),
            selectors=(args.selection,),
        )
        bound = Mock(server_context=Mock(selection=selected))
        launches = 0

        def launch(**kwargs: object) -> dict[str, object]:
            nonlocal launches
            launches += 1
            record = dict(kwargs["record"])
            if launches == 2:
                raise job.background_job.LaunchStateRetained("retained")
            harness = Path(record["staging"]) / "inputs" / "harness"
            for source in job.HARNESS_INPUTS:
                destination = harness / source.relative_to(job.MAINTENANCE_ROOT)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(source.read_bytes())
            record["process"] = {"pid": 12345}
            return record

        with (
            patch.object(job, "validate_selector"),
            patch.object(
                job.live_run,
                "resolve_patch_selection",
                return_value=selected,
            ),
            patch.object(job.background_job, "launch", side_effect=launch),
            patch.object(
                job.background_job,
                "wait_process",
                return_value={"state": "completed", "exit_code": 0},
            ),
            patch.object(job, "load_freeze_result", return_value=provenance),
            patch.object(job.live_run, "load_bound_inputs", return_value=bound),
            patch.object(job, "cleanup_freeze_state") as cleanup,
            self.assertRaisesRegex(job.JobError, "retained"),
        ):
            job.start(args)

        self.assertEqual(launches, 2)
        self.assertTrue((job.RESULT_ROOT / args.run / "inputs").is_dir())
        self.assertTrue(job.freeze_prelaunch_path(args.run).is_file())
        cleanup.assert_not_called()

    def test_start_rejects_a_clean_server_selection(self) -> None:
        args = Namespace(
            alpha_scenarios="default",
            application="zed",
            encoding="rgb",
            h264_client_policy="strict",
            lifecycle="application-exit",
            network_profile=job.DEFAULT_NETWORK_PROFILE,
            render_node=None,
            run="clean-selection",
            selection=None,
            zed_directory=None,
        )
        with self.assertRaisesRegex(job.JobError, "requires one non-empty"):
            job.start(args)

    def test_start_rejects_the_wrong_clipboard_selection(self) -> None:
        args = Namespace(
            alpha_scenarios="default",
            application="clipboard",
            encoding="rgb",
            h264_client_policy="strict",
            lifecycle="application-exit",
            network_profile=job.DEFAULT_NETWORK_PROFILE,
            render_node=None,
            run="clipboard-selection",
            selection="",
            zed_directory=None,
        )
        for selection in (
            "stacks/develop",
            "cases/wayland-client-keymap-sync",
        ):
            args.selection = selection
            selected = live_run.PatchSelection(
                case_slugs=(),
                digest="a" * 64,
                kind="stack" if selection.startswith("stacks/") else "case",
                name=selection,
                patches=(),
                required_gates=("live-wayland-keyboard",),
                selector_digests=((selection, "a" * 64),),
                selectors=(selection,),
            )
            with (
                self.subTest(selection=selection),
                patch.object(job, "validate_selector"),
                patch.object(
                    job.live_run,
                    "resolve_patch_selection",
                    return_value=selected,
                ),
                patch.object(job.background_job, "launch") as launch,
                self.assertRaisesRegex(
                    job.JobError,
                    "clipboard live acceptance requires selection",
                ),
            ):
                job._start_locked(args, args.run)
            launch.assert_not_called()

    def test_start_rejects_undeclared_case_gates_before_input_freeze(self) -> None:
        selection = "cases/video-pipeline-cleanup-race"
        selected = live_run.PatchSelection(
            case_slugs=("video-pipeline-cleanup-race",),
            digest="a" * 64,
            kind="case",
            name=selection,
            patches=(),
            required_gates=(),
            selector_digests=((selection, "a" * 64),),
            selectors=(selection,),
        )
        for application, gate in (
            ("hardware", "live-wayland-h264-hardware"),
            ("opengl", "live-wayland-opengl-h264-hardware"),
        ):
            args = Namespace(
                alpha_scenarios="default",
                application=application,
                encoding="h264",
                h264_client_policy="adaptive-alpha",
                lifecycle="application-exit",
                network_profile=job.DEFAULT_NETWORK_PROFILE,
                render_node=None,
                run=f"undeclared-{application}-gate",
                selection=selection,
                zed_directory=None,
            )
            with (
                self.subTest(application=application),
                patch.object(job, "validate_selector"),
                patch.object(
                    job.live_run,
                    "resolve_patch_selection",
                    return_value=selected,
                ),
                patch.object(job.background_job, "launch") as launch,
                self.assertRaisesRegex(
                    job.JobError,
                    f"does not declare required gate {gate}",
                ),
            ):
                job._start_locked(args, args.run)
            launch.assert_not_called()
            self.assertFalse(job.freeze_prelaunch_path(args.run).exists())

    def test_start_holds_the_lifecycle_lock_through_owner_publication(self) -> None:
        args = Namespace(run="locked-start")
        events: list[str] = []

        @contextmanager
        def lifecycle(run: str):
            self.assertEqual(run, args.run)
            events.append("lock-enter")
            try:
                yield object()
            finally:
                events.append("lock-exit")

        def start_locked(received: Namespace, run: str) -> int:
            self.assertIs(received, args)
            self.assertEqual(run, args.run)
            events.append("start")
            return 0

        with (
            patch.object(job, "prepare_private_state"),
            patch.object(job, "lifecycle_lock", lifecycle),
            patch.object(job, "_start_locked", side_effect=start_locked),
        ):
            self.assertEqual(job.start(args), 0)
        self.assertEqual(events, ["lock-enter", "start", "lock-exit"])

    def test_current_image_validation_ignores_only_non_maintenance_labels(self) -> None:
        images: dict[str, dict[str, object]] = {}
        inspections: list[subprocess.CompletedProcess[str]] = []
        for index, (name, role) in enumerate(
            (("client", "client-image"), ("server", "server-image")), start=1
        ):
            image_id = str(index) * 64
            labels = {
                "io.xpra.fork-maintenance.context": str(index + 2) * 64,
                "io.xpra.fork-maintenance.owner": "live",
                "io.xpra.fork-maintenance.role": role,
                "io.xpra.fork-maintenance.source": "a" * 40,
            }
            images[name] = {"id": image_id, "labels": labels}
            inspections.append(
                completed(
                    ["podman", "image", "inspect", image_id],
                    json.dumps(
                        [
                            {
                                "Id": "sha256:" + image_id,
                                "Labels": {
                                    **labels,
                                    "org.opencontainers.image.title": "base",
                                },
                            }
                        ]
                    ),
                )
            )
        with patch.object(job, "command", side_effect=inspections):
            self.assertTrue(job.current_image_validation({"images": images}))

        tampered = json.loads(inspections[0].stdout)
        tampered[0]["Labels"]["io.xpra.fork-maintenance.unexpected"] = "value"
        with patch.object(
            job,
            "command",
            side_effect=[
                completed(
                    ["podman", "image", "inspect", "1" * 64],
                    json.dumps(tampered),
                ),
                inspections[1],
            ],
        ):
            self.assertFalse(job.current_image_validation({"images": images}))

    def test_status_routes_a_prelaunch_input_freeze(self) -> None:
        run = "freezing"
        job.prepare_private_state()
        job.freeze_record_path(run).write_text("{}\n", encoding="utf-8")
        job.freeze_record_path(run).chmod(0o600)
        freeze = {"job_id": JOB_ID, "run": run, "staging": "/tmp/staging"}
        output = StringIO()
        with (
            patch.object(job, "load_freeze_record", return_value=freeze),
            patch.object(
                job,
                "freeze_process_state",
                return_value={"state": "running", "pid": 12345},
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(job.status(Namespace(run=run)), 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["phase"], "input-freeze")
        self.assertEqual(payload["input_freeze"]["state"], "running")

    def test_main_and_freeze_records_bind_background_paths_to_the_run(self) -> None:
        job.prepare_private_state()
        for kind in ("main", "freeze"):
            with self.subTest(kind=kind):
                run = f"wrong-{kind}-process-path"
                record = self.record(run)
                if kind == "main":
                    path = job.record_path(run)
                    loader = lambda run=run: job.load_record(  # noqa: E731 - captures this subtest's run
                        run, require_current=False
                    )
                else:
                    record.update(
                        {
                            "application": "hardware",
                            "freeze_result": str(job.freeze_result_path(run)),
                            "kind": "input-freeze",
                            "result": str(job.RESULT_ROOT / run),
                            "staging": str(job.freeze_staging_path(run, JOB_ID)),
                        }
                    )
                    record.pop("alpha_scenarios")
                    record.pop("encoding")
                    record.pop("h264_client_policy")
                    record.pop("input_provenance")
                    record.pop("lifecycle")
                    record.pop("network_profile")
                    record.pop("render_node")
                    record.pop("result_report")
                    record["schema"] = 2
                    path = job.freeze_record_path(run)
                    loader = lambda run=run: job.load_freeze_record(run)  # noqa: E731 - captures this subtest's run
                record["process"]["runtime_log"] = str(self.root / "outside.log")
                self.write_private_json(path, record)
                with self.assertRaisesRegex(job.JobError, "runtime log is outside"):
                    loader()
                record["process"]["runtime_log"] = str(
                    job.runtime_log_path(run)
                    if kind == "main"
                    else job.freeze_runtime_log_path(run)
                )
                record["process"]["completion"] = str(
                    self.root / "outside-completion.json"
                )
                path.write_text(json.dumps(record) + "\n", encoding="utf-8")
                path.chmod(0o600)
                with self.assertRaisesRegex(job.JobError, "completion is outside"):
                    loader()

    def test_ownerless_freeze_prelaunch_is_visible_and_exactly_abortable(self) -> None:
        run = "freeze-ownerless"
        job.prepare_private_state()
        marker = self.freeze_prelaunch(run)
        job.publish_json(job.freeze_prelaunch_path(run), marker)
        job.freeze_runtime_log_path(run).write_text(
            "owner publication interrupted\n", encoding="utf-8"
        )
        job.freeze_runtime_log_path(run).chmod(0o600)
        output = StringIO()
        with (
            patch.object(job.background_job, "process_identity", return_value=None),
            redirect_stdout(output),
        ):
            self.assertEqual(job.status(Namespace(run=run)), 0)
        status = json.loads(output.getvalue())
        self.assertEqual(status["phase"], "input-freeze-prelaunch")
        self.assertFalse(status["active"])
        with patch.object(job.background_job, "process_identity", return_value=None):
            self.assertEqual(job.abort(Namespace(run=run)), 0)
        self.assertFalse(job.freeze_prelaunch_path(run).exists())
        self.assertFalse(job.freeze_runtime_log_path(run).exists())

    def test_ownerless_freeze_prelaunch_refuses_active_or_executed_state(self) -> None:
        for state_kind in ("active", "completion", "staging"):
            with self.subTest(state_kind=state_kind):
                run = f"freeze-{state_kind}"
                job.prepare_private_state()
                marker = self.freeze_prelaunch(run)
                job.publish_json(job.freeze_prelaunch_path(run), marker)
                identity = None
                if state_kind == "active":
                    identity = ("S", "12345", "42")
                elif state_kind == "completion":
                    self.write_private_json(job.freeze_completion_path(run), {})
                else:
                    Path(str(marker["staging"])).mkdir(mode=0o700)
                with (
                    patch.object(
                        job.background_job,
                        "process_identity",
                        return_value=identity,
                    ),
                    self.assertRaisesRegex(
                        job.JobError,
                        "still active|executed or ambiguous",
                    ),
                ):
                    job.abort(Namespace(run=run))
                self.assertTrue(job.freeze_prelaunch_path(run).is_file())

    def test_abort_routes_running_completed_and_lost_input_freezes(self) -> None:
        for state in ("running", "completed", "lost"):
            with self.subTest(state=state):
                run = f"freeze-{state}"
                job.prepare_private_state()
                job.freeze_record_path(run).write_text("{}\n", encoding="utf-8")
                job.freeze_record_path(run).chmod(0o600)
                freeze = {"job_id": JOB_ID, "run": run}
                with (
                    patch.object(job, "load_freeze_record", return_value=freeze),
                    patch.object(
                        job,
                        "freeze_process_state",
                        return_value={"state": state},
                    ),
                    patch.object(job.background_job, "terminate") as terminate,
                    patch.object(job, "cleanup_freeze_state") as cleanup,
                ):
                    self.assertEqual(job.abort(Namespace(run=run)), 0)
                if state == "running":
                    terminate.assert_called_once_with(freeze, require_current=False)
                else:
                    terminate.assert_not_called()
                cleanup.assert_called_once_with(
                    freeze,
                    remove_input_directories=True,
                )

    def test_abort_of_owned_freeze_removes_matching_external_prelaunch(self) -> None:
        run = "freeze-owned-prelaunch"
        job.prepare_private_state()
        prelaunch = self.freeze_prelaunch(run)
        job.publish_json(job.freeze_prelaunch_path(run), prelaunch)
        self.write_private_json(job.freeze_record_path(run), {})
        freeze = {
            key: prelaunch[key]
            for key in (
                "application",
                "background_supervisor_sha256",
                "freeze_result",
                "harness_sha256",
                "job_id",
                "result",
                "run",
                "runner_sha256",
                "selection",
                "staging",
                "supervisor_sha256",
            )
        }
        with (
            patch.object(job, "load_freeze_record", return_value=freeze),
            patch.object(
                job,
                "freeze_process_state",
                return_value={"state": "completed"},
            ),
            patch.object(job, "cleanup_freeze_state"),
        ):
            self.assertEqual(job.abort(Namespace(run=run)), 0)
        self.assertFalse(job.freeze_prelaunch_path(run).exists())

    def test_input_freeze_abort_retries_after_partial_directory_removal(self) -> None:
        run = "freeze-abort-retry"
        job.prepare_private_state()
        staging = job.freeze_staging_path(run, JOB_ID)
        result = job.RESULT_ROOT / run
        for path in (staging, result):
            path.mkdir(mode=0o700)
            (path / "first").write_text("first\n", encoding="utf-8")
            (path / "second").write_text("second\n", encoding="utf-8")
        record = {
            "run": run,
            "staging": str(staging),
            "result": str(result),
        }
        self.write_private_json(job.freeze_record_path(run), record)
        real_rmtree = job.shutil.rmtree
        interrupted = False

        def interrupt_once(path: Path) -> None:
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                (Path(path) / "first").unlink()
                raise OSError("simulated crash during rmtree")
            real_rmtree(path)

        with (
            patch.object(job, "load_freeze_result", return_value={}),
            patch.object(job, "input_checksum_validation", return_value=True),
            patch.object(job.shutil, "rmtree", side_effect=interrupt_once),
            self.assertRaisesRegex(OSError, "simulated crash"),
        ):
            job.cleanup_freeze_state(record, remove_input_directories=True)
        self.assertTrue(job.freeze_abort_transaction_path(run).is_file())
        self.assertTrue(job.freeze_record_path(run).is_file())

        with (
            patch.object(job, "load_freeze_result", return_value={}),
            patch.object(job, "input_checksum_validation", return_value=True),
        ):
            job.cleanup_freeze_state(record, remove_input_directories=True)
        for path in (
            staging,
            result,
            job.freeze_abort_staging_path(run, "staging"),
            job.freeze_abort_staging_path(run, "result"),
            job.freeze_abort_transaction_path(run),
            job.freeze_record_path(run),
        ):
            self.assertFalse(path.exists())

    def test_collect_accepts_completed_owned_process_and_report(self) -> None:
        run = "accepted"
        record = self.record(run)
        job.prepare_private_state()
        job.runtime_log_path(run).write_bytes(b"complete log\n")
        job.runtime_log_path(run).chmod(0o600)
        self.make_report(run, record)
        completed_state = {
            "exit_code": 0,
            "finished_at": "2026-08-27T00:00:00+00:00",
            "pid": 12345,
            "state": "completed",
        }
        with (
            patch.object(job, "load_record", return_value=record),
            patch.object(job.background_job, "process_state", return_value=completed_state),
            patch.object(
                job,
                "owned_objects",
                return_value={"containers": [], "networks": []},
            ),
            patch.object(job, "current_image_validation", return_value=True),
        ):
            self.assertEqual(job.collect(Namespace(run=run)), 0)
        self.assertEqual(job.log_path(run).read_bytes(), b"complete log\n")
        status = json.loads(job.status_path(run).read_text(encoding="utf-8"))
        self.assertEqual(status["schema"], 3)
        self.assertEqual(status["exit_code"], 0)
        self.assertTrue(status["report_checks"]["background_supervisor_sha256"])

    def test_evidence_tree_binds_each_embedded_scenario_profile_identity(self) -> None:
        mutations = {
            "application": "gtk",
            "encoding": "h264",
            "h264_client_policy": "adaptive-alpha",
        }
        for field, replacement in mutations.items():
            with self.subTest(field=field):
                run = f"scenario-identity-{field.replace('_', '-')}"
                record = self.record(run)
                job.prepare_private_state()
                self.make_report(run, record)
                report_path = job.result_path(run)
                payload = json.loads(report_path.read_text(encoding="utf-8"))
                scenario = payload["scenarios"][0]
                scenario[field] = replacement
                scenario_path = report_path.parent / scenario["name"] / "report.json"
                scenario_path.write_text(
                    json.dumps(scenario, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                payload["scenario_report_sha256"][scenario["name"]] = job.sha256_file(
                    scenario_path
                )
                report_path.write_text(
                    json.dumps(payload, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                _result, _digest, checks = job.report_validation(run, record)
                self.assertFalse(checks["evidence_tree"])

    def test_subsurface_evidence_binds_default_alpha_client_mode(self) -> None:
        run = "subsurface-default-alpha"
        record = self.record(run)
        record["application"] = "subsurface"
        job.prepare_private_state()
        self.make_report(run, record)
        report = job.result_path(run)
        payload = json.loads(report.read_text(encoding="utf-8"))
        scenario = payload["scenarios"][0]

        with (
            patch.object(
                job,
                "subsurface_fixture_artifact_evidence_matches",
                return_value=True,
            ),
            patch.object(job, "input_checksum_validation", return_value=True),
        ):
            self.assertTrue(job.evidence_tree_validation(payload, report))
            scenario["client"]["alpha_disabled"] = True
            scenario_path = report.parent / scenario["name"] / "report.json"
            scenario_path.write_text(
                json.dumps(scenario, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            payload["scenario_report_sha256"][scenario["name"]] = job.sha256_file(
                scenario_path
            )
            self.assertFalse(job.evidence_tree_validation(payload, report))

    def test_case_only_provenance_binds_selected_source_to_both_endpoints(
        self,
    ) -> None:
        for application in ("clipboard", "subsurface"):
            run = f"{application}-provenance"
            record = self.clipboard_record(run)
            record["application"] = application
            provenance = record["input_provenance"]
            self.assertIs(
                job.validate_input_provenance(
                    provenance,
                    application=application,
                    run=run,
                    selection=str(record["selection"]),
                    harness_digest=str(record["harness_sha256"]),
                ),
                provenance,
            )
            for field in (
                "client_context_archive_sha256",
                "client_context_sha256",
                "client_selection",
                "client_selection_resolution_sha256",
                "client_selection_sha256",
            ):
                with self.subTest(application=application, field=field):
                    changed = copy.deepcopy(provenance)
                    changed[field] = (
                        "master" if field == "client_selection" else "0" * 64
                    )
                    with self.assertRaisesRegex(
                        job.JobError,
                        "client selection|same selected source",
                    ):
                        job.validate_input_provenance(
                            changed,
                            application=application,
                            run=run,
                            selection=str(record["selection"]),
                            harness_digest=str(record["harness_sha256"]),
                        )

    def test_image_provenance_uses_each_frozen_endpoint_selection(self) -> None:
        record = self.clipboard_record("clipboard-images")
        provenance = record["input_provenance"]
        images = {
            role: {
                "build_context_sha256": provenance[f"{role}_context_sha256"],
                "id": "sha256:" + ("1" if role == "client" else "2") * 64,
                "labels": {
                    "io.xpra.fork-maintenance.context": provenance[
                        f"{role}_context_sha256"
                    ],
                    "io.xpra.fork-maintenance.owner": "live",
                    "io.xpra.fork-maintenance.role": f"{role}-image",
                    "io.xpra.fork-maintenance.source": provenance["source_commit"],
                },
                "selection": provenance[f"{role}_selection"],
                "tag": f"localhost/{role}:test",
            }
            for role in ("client", "server")
        }
        self.assertTrue(
            job.image_provenance_validation({"images": images}, provenance)
        )
        images["client"]["selection"] = "master"
        self.assertFalse(
            job.image_provenance_validation({"images": images}, provenance)
        )

    def test_collect_records_a_completed_failure(self) -> None:
        run = "failed"
        record = self.record(run)
        job.prepare_private_state()
        job.runtime_log_path(run).write_bytes(b"failed log\n")
        job.runtime_log_path(run).chmod(0o600)
        completed_state = {
            "exit_code": 1,
            "finished_at": "2026-08-27T00:00:00+00:00",
            "pid": 12345,
            "state": "completed",
        }
        with (
            patch.object(job, "load_record", return_value=record),
            patch.object(job.background_job, "process_state", return_value=completed_state),
            patch.object(
                job,
                "owned_objects",
                return_value={"containers": [], "networks": []},
            ),
            patch.object(job, "current_image_validation", return_value=True),
        ):
            self.assertEqual(job.collect(Namespace(run=run)), 1)
        status = json.loads(job.status_path(run).read_text(encoding="utf-8"))
        self.assertEqual(status["result"], "failed")
        self.assertEqual(status["schema"], 3)

    def test_report_validation_rejects_mutated_scenario_evidence(self) -> None:
        run = "mutated-evidence"
        record = self.record(run)
        job.prepare_private_state()
        self.make_report(run, record)
        evidence = job.result_path(run).parent / "default-alpha" / "evidence.txt"
        evidence.write_text("changed\n", encoding="utf-8")
        _result, _digest, checks = job.report_validation(run, record)
        self.assertFalse(checks["evidence_tree"])

    def test_report_validation_rejects_nonprivate_scenario_evidence(self) -> None:
        run = "public-evidence"
        record = self.record(run)
        job.prepare_private_state()
        self.make_report(run, record)
        evidence = job.result_path(run).parent / "default-alpha" / "evidence.txt"
        evidence.chmod(0o644)
        _result, _digest, checks = job.report_validation(run, record)
        self.assertFalse(checks["evidence_tree"])

    def test_report_validation_recomputes_lifecycle_evidence(self) -> None:
        run = "mutated-lifecycle"
        record = self.record(run)
        job.prepare_private_state()
        self.make_report(run, record)
        report_path = job.result_path(run)
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        scenario = payload["scenarios"][0]
        scenario["lifecycle"]["client_exited_after_server"] = "true"
        scenario["classification"]["boundaries"]["lifecycle"][
            "client_exited_after_server"
        ] = True
        scenario_path = report_path.parent / scenario["name"] / "report.json"
        scenario_path.write_text(
            json.dumps(scenario, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        payload["scenario_report_sha256"][scenario["name"]] = job.sha256_file(
            scenario_path
        )
        report_path.write_text(
            json.dumps(payload, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _result, _digest, checks = job.report_validation(run, record)
        self.assertFalse(checks["evidence_tree"])

    def test_clipboard_jsonl_parser_rejects_sequence_schema_and_time_mutations(
        self,
    ) -> None:
        records = [
            clipboard_event(0, "ready"),
            clipboard_event(1, "completed"),
        ]
        self.assertEqual(
            live_run.parse_clipboard_jsonl_text(
                clipboard_jsonl(records),
                "clipboard-test.stdout",
            ),
            records,
        )
        mutations = {
            "schema": lambda value: value[0].__setitem__("schema", 2),
            "sequence": lambda value: value[1].__setitem__("sequence", 0),
            "timestamp": lambda value: value[1].__setitem__("monotonic_ns", 1_000),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                changed = copy.deepcopy(records)
                mutate(changed)
                with self.assertRaises(live_run.LabFailure):
                    live_run.parse_clipboard_jsonl_text(
                        clipboard_jsonl(changed),
                        "clipboard-test.stdout",
                    )

    def test_clipboard_artifact_helpers_accept_exact_policy_matrix(self) -> None:
        for policy in live_run.CLIPBOARD_POLICIES:
            with self.subTest(policy=policy):
                root = self.root / f"clipboard-{policy}"
                interaction = self.make_clipboard_fixture_artifacts(root, policy)
                self.assertEqual(
                    tuple(interaction["checks"]),
                    live_run.CLIPBOARD_LIVE_CHECK_NAMES,
                )
                self.assertTrue(all(interaction["checks"].values()))
                self.assertTrue(
                    live_run.clipboard_artifact_evidence_matches(interaction, root)
                )

    def test_clipboard_source_and_cross_peer_authorities_reject_mutations(self) -> None:
        def source_change(root: Path, old: str, new: str) -> None:
            path = root / "server.stderr"
            path.write_text(path.read_text().replace(old, new), encoding="utf-8")

        def event_change(root: Path, name: str, index: int, key: str, value: object) -> None:
            path = root / name
            records = live_run.read_clipboard_records(path)
            records[index][key] = value
            path.write_text(clipboard_jsonl(records), encoding="utf-8")

        mutations = {
            "null-native-source": lambda root: source_change(root, "selection', 103", "selection', 000"),
            "stale-restored-source": lambda root: source_change(root, "selection', 102", "selection', 101"),
            "missing-forward-source": lambda root: source_change(root, "emit('selection', 101)", "xmit('selection', 101)"),
            "unobserved-forward-source": lambda root: source_change(root, "emit('selection', 101)", "emit('selection', xxx)"),
            "reverse-before-native-confirmation": lambda root: event_change(
                root, "clipboard-consumer-reverse.stdout", 0, "monotonic_ns", 1_029_000_000),
            "monitor-stopped-before-fixture-closed": lambda root: event_change(
                root, "clipboard-monitor.stdout", -1, "stop_requested_ns", 1_039_000_000),
            "synthetic-xfixes-event": lambda root: event_change(
                root, "clipboard-monitor.stdout", 1, "send_event", True),
            "foreign-xfixes-window": lambda root: event_change(
                root, "clipboard-monitor.stdout", 1, "window_xid", 999),
            "foreign-monitor-pid": lambda root: (root / "clipboard-monitor.pid").write_text("999\n"),
        }
        for name, mutate in mutations.items():
            with self.subTest(mutation=name):
                root = self.root / name
                self.make_clipboard_fixture_artifacts(root, "both")
                mutate(root)
                try:
                    changed = live_run._clipboard_evidence_from_artifacts(root, "both")
                except live_run.LabFailure:
                    continue
                self.assertFalse(all(changed["checks"].values()), changed["checks"])
                self.assertFalse(live_run.clipboard_artifact_evidence_matches(changed, root))

    def test_clipboard_monitor_rejects_takeover_after_reverse_conversion(self) -> None:
        for policy in live_run.CLIPBOARD_POLICIES:
            with self.subTest(policy=policy):
                root = self.root / f"late-{policy}"
                self.make_clipboard_fixture_artifacts(root, policy)
                path = root / "clipboard-monitor.stdout"
                records = live_run.read_clipboard_records(path)
                late = {**records[1], "monotonic_ns": 1_038_000_000, "owner_xid": 999,
                        "selection_timestamp": 850, "timestamp": 850}
                records.insert(4 if policy == "both" else 3, late)
                for index, record in enumerate(records):
                    record["sequence"] = index
                records[-1]["event_count"] += 1
                records[-1]["events"] = [
                    {key: value for key, value in record.items()
                     if key not in {"event", "monotonic_ns", "schema", "sequence"}}
                    for record in records[1:-1]
                ]
                path.write_text(clipboard_jsonl(records), encoding="utf-8")
                changed = live_run._clipboard_evidence_from_artifacts(root, policy)
                self.assertFalse(changed["checks"]["reverse_policy"])
                self.assertFalse(live_run.clipboard_artifact_evidence_matches(changed, root))

    def test_clipboard_artifact_helpers_bind_pids_and_exact_artifact_set(
        self,
    ) -> None:
        def mutate_client_pid(root: Path) -> None:
            (root / "client.pid").write_text("999\n", encoding="ascii")

        def mutate_owner_pid(root: Path) -> None:
            (root / "clipboard-owner.pid").write_text("999\n", encoding="ascii")

        def mutate_fixture_pid(root: Path) -> None:
            (root / "clipboard-fixture.pid").write_text("999\n", encoding="ascii")

        def remove_exit(root: Path) -> None:
            (root / "clipboard-monitor.exit").unlink()

        def add_artifact(root: Path) -> None:
            (root / "clipboard-unexpected.stdout").write_text("", encoding="ascii")

        def add_owner_stderr(root: Path) -> None:
            (root / "clipboard-owner.stderr").write_text(
                "synthetic fixture warning\n",
                encoding="ascii",
            )

        def mutate_survival_identity(root: Path) -> None:
            path = root / live_run.CLIPBOARD_CLIENT_SURVIVAL_ARTIFACT
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["after"]["start_ticks"] = "888888"
            live_run.replace_private_json(path, payload)

        mutations = {
            "client-pid": (mutate_client_pid, "client_survived_owner_changes"),
            "owner-pid": (mutate_owner_pid, "event_sequence_exact"),
            "fixture-pid": (mutate_fixture_pid, "event_sequence_exact"),
            "missing-exit": (remove_exit, "fixture_processes_clean"),
            "owner-stderr": (add_owner_stderr, "fixture_processes_clean"),
            "unexpected-artifact": (add_artifact, "fixture_processes_clean"),
            "survival-identity": (
                mutate_survival_identity,
                "client_survived_owner_changes",
            ),
        }
        for name, (mutate, failed_check) in mutations.items():
            with self.subTest(name=name):
                root = self.root / f"clipboard-artifact-{name}"
                interaction = self.make_clipboard_fixture_artifacts(root, "both")
                mutate(root)
                checks = live_run.clipboard_interaction_checks(interaction, root)
                self.assertFalse(checks[failed_check])
                self.assertFalse(
                    live_run.clipboard_artifact_evidence_matches(interaction, root)
                )

    def test_clipboard_artifact_helpers_reject_plaintext_marker(self) -> None:
        for name in ("client-debug.txt", "report.json"):
            with self.subTest(name=name):
                root = self.root / f"clipboard-plaintext-{name}"
                interaction = self.make_clipboard_fixture_artifacts(root, "both")
                marker_log = root / name
                marker_log.write_text(
                    live_run.clipboard_fixture_common.marker_text("two"),
                    encoding="utf-8",
                )
                marker_log.chmod(0o600)
                checks = live_run.clipboard_interaction_checks(interaction, root)
                self.assertFalse(checks["no_plaintext_marker_artifacts"])
                self.assertFalse(
                    live_run.clipboard_artifact_evidence_matches(interaction, root)
                )

    def test_clipboard_blocked_reverse_requires_original_owner(self) -> None:
        for policy in ("to-server", "off"):
            with self.subTest(policy=policy):
                root = self.root / f"clipboard-reverse-{policy}"
                interaction = self.make_clipboard_fixture_artifacts(root, policy)
                changed = copy.deepcopy(interaction)
                reverse = changed["local"]["reverse"]
                reverse["owner_before_xid"] = 999
                reverse["owner_after_xid"] = 999
                reverse_path = root / "clipboard-consumer-reverse.stdout"
                reverse_path.write_text(
                    clipboard_jsonl([reverse]),
                    encoding="utf-8",
                )
                checks = live_run.clipboard_interaction_checks(changed, root)
                self.assertFalse(checks["reverse_policy"])
                changed["checks"] = checks
                self.assertFalse(
                    live_run.clipboard_artifact_evidence_matches(changed, root)
                )

    def test_clipboard_reverse_requires_confirmation_and_exact_xfixes_route(
        self,
    ) -> None:
        for policy in live_run.CLIPBOARD_POLICIES:
            with self.subTest(policy=policy, mutation="confirmation"):
                root = self.root / f"clipboard-confirmation-{policy}"
                interaction = self.make_clipboard_fixture_artifacts(root, policy)
                changed = copy.deepcopy(interaction)
                changed["wayland"]["records"][10]["event"] = "owner-unconfirmed"
                checks = live_run.clipboard_interaction_checks(changed, root)
                self.assertFalse(checks["reverse_policy"])
                self.assertFalse(checks["event_sequence_exact"])

            with self.subTest(policy=policy, mutation="xfixes"):
                root = self.root / f"clipboard-xfixes-{policy}"
                interaction = self.make_clipboard_fixture_artifacts(root, policy)
                changed = copy.deepcopy(interaction)
                records = changed["xfixes"]["records"]
                if policy == "both":
                    del records[3]
                else:
                    records.insert(
                        -1,
                        clipboard_event(
                            3,
                            "xfixes-selection-notify",
                            owner_xid=999,
                            selection_is_clipboard=True,
                            selection_timestamp=801,
                            send_event=False,
                            subtype=0,
                            timestamp=802,
                            window_xid=501,
                        ),
                    )
                checks = live_run.clipboard_interaction_checks(changed, root)
                self.assertFalse(checks["reverse_policy"])
                self.assertFalse(checks["event_sequence_exact"])

    def test_clipboard_evidence_recomputes_exact_classified_checks(self) -> None:
        interaction = clipboard_interaction("both")
        embedded = {
            "client": {
                "clipboard_options": list(
                    live_run.live_config.clipboard_options("client", "both")
                )
            },
            "clipboard_policy": "both",
            "classification": {
                "boundaries": {"interaction": interaction["checks"]}
            },
            "interaction": interaction,
            "server": {
                "clipboard_options": list(
                    live_run.live_config.clipboard_options("server", "both")
                )
            },
        }
        scenario_root = self.root / "clipboard-both"
        with (
            patch.object(
                live_run,
                "clipboard_interaction_checks",
                return_value=interaction["checks"],
            ) as recompute,
            patch.object(
                live_run,
                "clipboard_artifact_evidence_matches",
                return_value=True,
            ) as artifacts,
        ):
            self.assertTrue(
                job.clipboard_fixture_artifact_evidence_matches(
                    embedded,
                    scenario_root,
                    live_run,
                    "both",
                )
            )
        recompute.assert_called_once_with(interaction, scenario_root)
        artifacts.assert_called_once_with(interaction, scenario_root)

        mutations = {
            "classified": lambda value: value["classification"]["boundaries"].__setitem__(
                "interaction", {}
            ),
            "extra-check": lambda value: value["interaction"]["checks"].__setitem__(
                "unreviewed", True
            ),
            "false-check": lambda value: value["interaction"]["checks"].__setitem__(
                live_run.CLIPBOARD_LIVE_CHECK_NAMES[0], False
            ),
            "policy": lambda value: value["interaction"].__setitem__(
                "policy", "off"
            ),
            "shape": lambda value: value["interaction"].__setitem__(
                "unreviewed", True
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                changed = copy.deepcopy(embedded)
                mutate(changed)
                with (
                    patch.object(
                        live_run,
                        "clipboard_interaction_checks",
                        return_value=changed["interaction"]["checks"],
                    ),
                    patch.object(
                        live_run,
                        "clipboard_artifact_evidence_matches",
                        return_value=True,
                    ),
                ):
                    self.assertFalse(
                        job.clipboard_fixture_artifact_evidence_matches(
                            changed,
                            scenario_root,
                            live_run,
                            "both",
                        )
                    )

    def test_clipboard_evidence_requires_exact_policy_scenario_order(self) -> None:
        run = "clipboard-scenario-order"
        record = self.clipboard_record(run)
        job.prepare_private_state()
        payload, report = self.make_clipboard_evidence_tree(run, record)

        def recompute(interaction: dict[str, object], _root: Path) -> object:
            return interaction["checks"]

        with (
            patch.object(
                live_run,
                "clipboard_interaction_checks",
                side_effect=recompute,
            ),
            patch.object(
                live_run,
                "clipboard_artifact_evidence_matches",
                return_value=True,
            ),
            patch.object(job, "input_checksum_validation", return_value=True),
        ):
            self.assertTrue(job.evidence_tree_validation(payload, report))
            payload["scenarios"] = list(reversed(payload["scenarios"]))
            self.assertFalse(job.evidence_tree_validation(payload, report))

    def test_subsurface_c_pixels_match_independent_oracle_and_damage(self) -> None:
        source = (LIVE_DIRECTORY / "subsurface_fixture.c").read_text(encoding="utf-8")
        enum = re.search(r"enum pixel_pattern \{.*?\};", source, re.DOTALL)
        self.assertIsNotNone(enum)
        assert enum is not None
        functions = source.split("static uint32_t premultiplied_argb(", 1)[1].split(
            "static void create_buffer(", 1,
        )[0]
        macros = "\n".join(
            line for line in source.splitlines()
            if line.startswith("#define CONTINUOUS_DAMAGE_")
        )
        program = (
            "#include <stdint.h>\n#include <stdio.h>\n#include <stdlib.h>\n"
            + macros + "\n" + enum.group(0)
            + '\nstatic void fail_message(const char *message) { '
            'fputs(message, stderr); exit(2); }\n'
            + "static uint32_t premultiplied_argb(" + functions
            + "\nint main(int argc, char **argv) {\n"
            " if (argc != 4) return 2;\n"
            " int pattern = atoi(argv[1]), width = atoi(argv[2]), height = atoi(argv[3]);\n"
            " for (int y = 0; y < height; ++y) for (int x = 0; x < width; ++x) {\n"
            "  uint32_t pixel = pattern_pixel(pattern, x, y);\n"
            "  unsigned char rgba[] = {pixel >> 16, pixel >> 8, pixel, pixel >> 24};\n"
            "  if (fwrite(rgba, 1, 4, stdout) != 4) return 3;\n"
            " }\n return 0;\n}\n"
        )
        executable = self.root / "subsurface-pixels"
        subprocess.run(
            ["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-x", "c", "-",
             "-o", str(executable)],
            input=program, text=True, capture_output=True, check=True,
        )
        enum_names = [name.strip() for name in enum.group(0).split("{", 1)[1]
                      .split("}", 1)[0].split(",") if name.strip()]
        observed = {}
        for pattern, c_name in (
            ("primary", "PRIMARY_PARENT"), ("secondary", "SECONDARY_PARENT"),
            ("lower-one", "LOWER_STATE_ONE"), ("lower-two", "LOWER_STATE_TWO"),
            ("lower-three", "LOWER_STATE_THREE"), ("lower-four", "LOWER_STATE_FOUR"),
            ("upper", "UPPER_STATE"), ("lower-continuous-one", "LOWER_CONTINUOUS_ONE"),
        ):
            with self.subTest(pattern=pattern):
                expected = live_run._subsurface_fixture_image(pattern)
                raw = subprocess.run(
                    [str(executable), str(enum_names.index(c_name)),
                     str(expected.width), str(expected.height)],
                    capture_output=True, check=True,
                ).stdout
                self.assertEqual(raw, expected.tobytes())
                observed[pattern] = live_run.Image.frombytes("RGBA", expected.size, raw)
        x, y = live_run.SUBSURFACE_CONTINUOUS_SOURCE_ORIGINS["lower"]
        width, height = live_run.SUBSURFACE_CONTINUOUS_GEOMETRY[2:]
        difference = live_run.ImageChops.difference(
            observed["lower-continuous-one"], observed["lower-four"],
        ).convert("RGB")
        self.assertEqual(difference.getbbox(), (x, y, x + width, y + height))
        self.assertEqual(
            observed["lower-continuous-one"].crop((x, y, x + width, y + height)).tobytes(),
            observed["lower-three"].crop((x, y, x + width, y + height)).tobytes(),
        )

    def test_subsurface_c_continuous_scheduler_requires_callback_and_cadence(self) -> None:
        source = (LIVE_DIRECTORY / "subsurface_fixture.c").read_text(encoding="utf-8")
        commit = "static void commit_lower_continuous_generation(" + source.split(
            "static void commit_lower_continuous_generation(", 1,
        )[1].split("static void stop_lower_continuous_generations(", 1)[0]
        macros = "\n".join(line for line in source.splitlines()
                           if line.startswith("#define CONTINUOUS_"))
        # Execute the real C scheduler, replacing only Wayland marshaling and
        # its clock. Immediate callbacks must not create a catch-up burst; a
        # missing callback must still block after the wall-clock deadline.
        program = r'''
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#define LOWER_BUFFER_SCALE 2
struct fixture {
    bool lower_continuous_active, lower_continuous_stopped, lower_frame_ready;
    unsigned int lower_continuous_generation_count, lower_frame_done_count;
    unsigned int lower_attach_count, lower_commit_count, lower_update_count;
    uint32_t lower_frame_callback_id, lower_frame_callback_data;
    uint64_t lower_continuous_commit_ns;
    int lower_state;
    void *lower_surface;
    struct { void *buffer; } lower_buffers[7];
};
static uint64_t clock_ns;
static uint64_t monotonic_ns(void) __attribute__((unused));
static uint64_t monotonic_ns(void) { return clock_ns; }
static void fail_message(const char *message) { fputs(message, stderr); exit(2); }
static void arm_lower_frame_callback(struct fixture *fixture) { (void) fixture; }
static void wl_surface_attach(void *surface, void *buffer, int x, int y)
{ (void) surface; (void) buffer; (void) x; (void) y; }
static void wl_surface_damage_buffer(void *surface, int x, int y, int w, int h)
{ (void) surface; (void) x; (void) y; (void) w; (void) h; }
static void wl_surface_commit(void *surface) { (void) surface; clock_ns += 7000000; }
static void emit_continuous_generation(struct fixture *f, unsigned int b,
                                      uint32_t id, uint32_t data, uint64_t committed_ns)
{
    (void) b; (void) id; (void) data;
    if (committed_ns != f->lower_continuous_commit_ns || committed_ns + 7000000 != clock_ns)
        fail_message("scheduler and event must share the pre-marshaling timestamp");
}
'''
        program += macros + "\n" + commit + r'''
int main(int argc, char **argv) {
    (void) argv;
    struct fixture fixture = {.lower_continuous_active = true};
    if (argc > 1) {
        clock_ns = UINT64_C(10000000000);
        commit_lower_continuous_generation(&fixture);
        return 99;
    }
    const uint64_t offsets[] = {0, 1, 49999999, 50000000, 500000000,
                               500000001, 549999999, 550000000};
    const unsigned int expected[] = {1, 1, 1, 2, 3, 3, 3, 4};
    for (unsigned int index = 0; index < sizeof(offsets) / sizeof(offsets[0]); ++index) {
        clock_ns = UINT64_C(1000000000) + offsets[index];
        fixture.lower_frame_ready = true;
        fixture.lower_frame_done_count = 3 + fixture.lower_continuous_generation_count;
        commit_lower_continuous_generation(&fixture);
        if (fixture.lower_continuous_generation_count != expected[index]) {
            fprintf(stderr, "sample %u: emitted %u generations, expected %u\n",
                    index, fixture.lower_continuous_generation_count, expected[index]);
            return 3;
        }
    }
    return 0;
}
'''
        executable = self.root / "subsurface-continuous-scheduler"
        subprocess.run(
            ["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-x", "c", "-",
             "-o", str(executable)], input=program, text=True, capture_output=True, check=True,
        )
        result = subprocess.run([str(executable)], text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        missing = subprocess.run([str(executable), "missing-callback"],
                                 text=True, capture_output=True, check=False)
        self.assertEqual(missing.returncode, 2)
        self.assertIn("not callback-ready", missing.stderr)

    def test_subsurface_continuous_timeline_rejects_unpaced_burst(self) -> None:
        interaction = self.make_subsurface_fixture_artifacts(self.root / "subsurface-unpaced")
        events = copy.deepcopy(interaction["evidence"]["events"])
        for sequence, event in enumerate(events):
            event["monotonic_ns"] = (sequence + 1) * 1_000
        with self.assertRaisesRegex(live_run.LabFailure, "cadence"):
            live_run.validate_subsurface_fixture_events(events)
        with self.assertRaisesRegex(live_run.LabFailure, "cadence"):
            live_run._subsurface_continuous_event_prefix(events[:12], stopped=False)

    def test_subsurface_coalesces_uncaptured_generations_with_exact_final_state(self) -> None:
        root = self.root / "subsurface-coalesced"
        interaction = self.make_subsurface_fixture_artifacts(root, coalesced=True)
        continuous = interaction["evidence"]["continuous"]
        self.assertEqual(continuous["liveness"]["drained"]["fixture_generation_count"], 5)
        self.assertEqual(len(continuous["transactions"]["complete_transactions"]), 3)
        self.assertTrue(all(interaction["checks"].values()), interaction["checks"])
        self.assertTrue(live_run.subsurface_artifact_evidence_matches(interaction, root))
        for field in ("subsurface_pending", "subsurface_inflight"):
            with self.subTest(field=field):
                changed = copy.deepcopy(interaction)
                changed["evidence"]["continuous"]["info"][field] = 1
                checks = live_run.subsurface_interaction_checks(changed)
                self.assertFalse(checks["continuous_transactions_complete"])

    def test_subsurface_initial_and_map_damage_keep_complete_startup_history(self) -> None:
        for primary, secondary in ((1, 1), (1, 2), (2, 1), (2, 2)):
            with self.subTest(primary=primary, secondary=secondary):
                root = self.root / f"subsurface-startup-{primary}-{secondary}"
                interaction = self.make_subsurface_fixture_artifacts(
                    root, startup_captures=(primary, secondary),
                )
                self.assertTrue(all(interaction["checks"].values()), interaction["checks"])
                self.assertTrue(live_run.subsurface_artifact_evidence_matches(interaction, root))

                startup = interaction["evidence"]["startup"]
                self.assertEqual(len(startup["transactions"]), primary)
                self.assertEqual(len(startup["secondary"]), secondary)
                self.assertEqual(startup["packet_count"], 2 * primary + secondary)

    def test_subsurface_startup_rejects_dropped_or_malformed_earlier_history(self) -> None:
        root = self.root / "subsurface-startup-history"
        interaction = self.make_subsurface_fixture_artifacts(root, startup_captures=(2, 2))
        mutations = (
            lambda value: value["evidence"]["startup"]["transactions"].pop(0),
            lambda value: value["evidence"]["source_updates"]["1"]["updates"].pop(0),
            lambda value: value["evidence"]["source_updates"]["5"]["updates"].pop(0),
            lambda value: value["evidence"]["source_updates"]["2"]["updates"][0]["options"].__setitem__(
                "subsurface-transaction-id", True,
            ),
            lambda value: value["evidence"]["source_updates"]["1"]["updates"][0]["options"].__setitem__(
                "subsurface-stage-index", 1,
            ),
            lambda value: value["evidence"]["source_updates"]["1"]["updates"][0]["options"].__setitem__(
                "subsurface-backing-epoch", -1,
            ),
            lambda value: value["evidence"]["source_updates"]["5"]["updates"][0]["options"].__setitem__(
                "backing-epoch", -1,
            ),
            lambda value: value["evidence"]["source_updates"]["5"]["updates"][0]["options"].__setitem__(
                "backing-epoch", 1,
            ),
            lambda value: value["evidence"]["source_updates"]["5"]["updates"][0]["options"].__setitem__(
                "flush", 1,
            ),
            lambda value: value["evidence"]["stream"]["client_draws"].pop(0),
            lambda value: value["evidence"]["stream"]["acknowledgements"].pop(0),
            lambda value: value["evidence"]["info"]["initial"]["children"]["2"].__setitem__(
                "packets_sent", 1,
            ),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                changed = copy.deepcopy(interaction)
                mutate(changed)
                self.assertFalse(all(live_run.subsurface_interaction_checks(changed).values()))
        # Recompute from changed on-disk bytes, not merely self-reported hashes.
        # The final secondary packet is still correct; the earlier one is not.
        earlier = root / interaction["evidence"]["startup"]["secondary"][0]["packet_payload"]
        payload = earlier.read_bytes()
        earlier.write_bytes(bytes((payload[0] ^ 1,)) + payload[1:])
        with self.assertRaisesRegex(live_run.LabFailure, "secondary packet differs"):
            live_run.subsurface_artifact_observations(
                root, parent_wids=interaction["parent_wids"], child_wids=interaction["child_wids"],
                fixture_pid=interaction["fixture_pid"], parent_sources=interaction["parent_sources"],
                phases=interaction["phases"],
            )

    def test_subsurface_startup_rejects_a_third_unexplained_capture(self) -> None:
        for primary, secondary in ((3, 1), (1, 3)):
            with (
                self.subTest(primary=primary, secondary=secondary),
                self.assertRaisesRegex(live_run.LabFailure, "initial/map capture bound"),
            ):
                self.make_subsurface_fixture_artifacts(
                    self.root / f"subsurface-third-{primary}-{secondary}",
                    startup_captures=(primary, secondary),
                )

    def test_subsurface_startup_requires_new_focus_handlers_for_both_parents(self) -> None:
        windows = {"primary": "4097", "secondary": "4098"}
        for anchor in ("primary", "secondary"):
            root = self.root / f"subsurface-map-barriers-{anchor}"
            root.mkdir(mode=0o700)
            first = "secondary" if anchor == "primary" else "primary"
            actions = []

            def wait_for_handler(description, ready, actions=actions, first=first):
                if description.endswith("initial parent focus"):
                    self.assertFalse(actions)
                    self.assertTrue(ready())
                    return
                self.assertEqual(len(actions), 1 if first in description else 2)
                # Visibility and queued map packets are insufficient. Only a
                # fresh server UI handler can advance to the next activation.
                self.assertFalse(ready())
                self.assertTrue(ready())
                return True

            initial_matches = (True,) if anchor == "primary" else (False, True)
            with (
                self.subTest(anchor=anchor),
                patch.object(live_run, "podman_exec",
                             side_effect=lambda *args, actions=actions: actions.append(args[1][-1])),
                patch.object(live_run, "container_process_exists", return_value=True),
                patch.object(live_run, "container_artifact_size", side_effect=(10, 200)),
                patch.object(live_run, "container_artifact_suffix_matches",
                             side_effect=(*initial_matches, False, True, False, True)) as matches,
                patch.object(live_run, "wait_for", side_effect=wait_for_handler),
            ):
                live_run._establish_subsurface_startup_barriers(
                    "server", 11, "client", 12, root, {"primary": 1, "secondary": 2}, windows,
                )
            self.assertEqual(actions, [windows[first], windows[anchor]])
            self.assertTrue(all(call.args[2] == 10 for call in matches.call_args_list[len(initial_matches):]))
            metadata = json.loads((root / live_run.SUBSURFACE_STARTUP_BARRIERS_ARTIFACT).read_text())
            self.assertEqual(metadata["activation_order"], [first, anchor])
            records = metadata["parents"]
            self.assertEqual([record["wire_wid"] for record in records], [2, 1])
            self.assertEqual([record["server_log_start"] for record in records], [10, 10])
            self.assertEqual([record["server_log_end"] for record in records], [200, 200])

    def test_subsurface_startup_same_focus_handler_is_a_valid_map_barrier(self) -> None:
        root = self.root / "subsurface-same-focus"
        interaction = self.make_subsurface_fixture_artifacts(root)
        path = root / "server.stderr"
        text = path.read_text()
        # Native Wayland returns after its _focus entry when the requested
        # target is already current. No second state=True line is required.
        text = re.sub(r"focus: wid=[^\n]+", lambda match: " " * len(match[0]), text)
        path.write_text(text)
        self.assertEqual(
            live_run._load_subsurface_startup_barriers(root, interaction["parent_wids"]),
            interaction["evidence"]["startup_barriers"],
        )

    def test_subsurface_startup_parent_queue_must_drain(self) -> None:
        root = self.root / "subsurface-parent-queue"
        interaction = self.make_subsurface_fixture_artifacts(root)
        for role in live_run.SUBSURFACE_PARENT_ROLES:
            for field in ("ack_pending", "encoding_pending", "packets_sent"):
                with self.subTest(role=role, field=field):
                    changed = copy.deepcopy(interaction)
                    changed["evidence"]["info"]["initial"]["parents"][role][field] += 1
                    self.assertFalse(all(live_run.subsurface_interaction_checks(changed).values()))
        initial = root / live_run.SUBSURFACE_INFO_ARTIFACTS["initial"]
        text = initial.read_text()
        key = f"client.0.window.windows.{interaction['parent_wids']['secondary']}.damage.ack-pending"
        if f"{key}=" in text:
            text = re.sub(rf"{re.escape(key)}=0", f"{key}=1", text)
        else:
            text += f"{key}=1\n"
        initial.write_text(text)
        changed = copy.deepcopy(interaction)
        changed["evidence"] = live_run.subsurface_artifact_observations(
            root, parent_wids=interaction["parent_wids"], child_wids=interaction["child_wids"],
            fixture_pid=interaction["fixture_pid"], parent_sources=interaction["parent_sources"],
            phases=interaction["phases"],
        )
        self.assertFalse(all(live_run.subsurface_interaction_checks(changed).values()))

    def test_subsurface_startup_requires_both_ordinary_requests_to_leave_batching(self) -> None:
        request0 = b"do_damage(0, 0, 360, 260, {}) wid=0x2, scheduling batching expiry for sequence 1 in 0 ms\n"
        request1 = b"do_damage(0, 0, 360, 260, {}) wid=0x2, scheduling batching expiry for sequence 2 in 31 ms\n"
        merged1 = b"do_damage(0, 0, 360, 260, {}) wid=0x2, using existing 1 delayed regions created 1ms ago\n"
        capture0 = b"process_damage_region: wid=0x2, sequence=2, adding pixel data to encode queue ( 360x260 - rgb32)\n"
        capture1 = b"process_damage_region: wid=0x2, sequence=3, adding pixel data to encode queue ( 360x260 - rgb32)\n"
        for captures, log in (
            (1, request0 + merged1 + capture0),
            (2, request0 + capture0 + request1 + capture1),
        ):
            with self.subTest(captures=captures):
                result = live_run._subsurface_secondary_startup_damage(log, 2, captures)
                self.assertTrue(live_run._subsurface_startup_damage_metadata(result, captures))
                self.assertEqual(len(result["captures"]), captures)
        for captures, log in (
            (1, request0 + capture0),  # map request has not run
            (1, request0 + capture0 + request1),  # map damage is still delayed
            (1, request0 + merged1),  # coalesced damage has not been captured
            (2, capture0 + request0 + request1 + capture1),
            (2, request0 + request1 + capture0 + capture1),
            (2, request0 + capture0 + request1 + capture1.replace(b"360x260", b"359x260")),
            (2, request0 + capture0 + request1 + capture1.replace(b"wid=0x2", b"wid=0x3")),
        ):
            with self.subTest(log=log), self.assertRaises(live_run.LabFailure):
                live_run._subsurface_secondary_startup_damage(log, 2, captures)

    def test_subsurface_startup_cannot_reuse_old_focus_or_a_missing_map_barrier(self) -> None:
        root = self.root / "subsurface-old-focus"
        interaction = self.make_subsurface_fixture_artifacts(root)
        path = root / live_run.SUBSURFACE_STARTUP_BARRIERS_ARTIFACT
        value = json.loads(path.read_text())
        # Move the first slice beyond its actual handler. The old log line is
        # still present, but it cannot confirm the new activation interval.
        for record in value["parents"]:
            record["server_log_start"] = record["server_log_end"] - 1
        live_run.replace_private_json(path, value)
        self.assertFalse(live_run.subsurface_artifact_evidence_matches(interaction, root))
        with self.assertRaisesRegex(live_run.LabFailure, "fresh server focus/map barrier"):
            live_run._load_subsurface_startup_barriers(root, interaction["parent_wids"])

    def test_subsurface_capture_timeline_preserves_source_order_and_terminal_state(self) -> None:
        generations = [{"lower_state_id": state} for state in (3, 4, 3, 4, 3)]
        for captured, final, expected in (
            ((3, 4, 3), True, True), ((4, 3), True, True),
            ((3, 3, 3), True, True), ((3, 4), False, True),
            ((3, 4), True, False), ((4, 4, 4, 3), True, False),
            ((3, 4, 3, 4, 3, 4), False, False), ((True,), False, False),
        ):
            with self.subTest(captured=captured, final=final):
                self.assertIs(
                    live_run._subsurface_capture_timeline_matches(
                        [{"lower_state_id": state} for state in captured],
                        generations, final=final,
                    ), expected,
                )

    def test_subsurface_active_proof_rechecks_producer_after_packet_collection(self) -> None:
        generations = [{"lower_state_id": state} for state in (3, 4)]
        snapshot = {
            "complete_transactions": [
                {"lower_state_id": state,
                 "packets": [{"role": "lower", "payload_sha256": str(state) * 64}]}
                for state in (3, 4)
            ],
            "inflight_transaction": None,
            "packet_count": 6,
        }
        with (
            patch.object(live_run, "container_process_exists", return_value=True),
            patch.object(live_run, "read_container_subsurface_events", return_value=[
                {"monotonic_ns": 1_000_000_000} for _ in range(9)
            ]),
            patch.object(live_run.time, "monotonic_ns", return_value=1_100_000_000),
            patch.object(live_run, "_subsurface_continuous_event_prefix", side_effect=[
                (generations, None),
                (generations * (live_run.SUBSURFACE_CONTINUOUS_MAX_GENERATIONS // 2), None),
            ]),
            patch.object(live_run, "synchronize_subsurface_saved_updates",
                         return_value={"updates": [{"sequence": 28}]}),
            patch.object(live_run, "_subsurface_continuous_transaction_snapshot", return_value=snapshot),
            patch.object(live_run, "podman_exec", return_value=completed([])),
            patch.object(live_run, "wait_for", side_effect=lambda _name, check, **_kwargs: check()),
            self.assertRaisesRegex(live_run.LabFailure, "active safety cap"),
        ):
            live_run._wait_subsurface_continuous_active(
                "server", 1, "client", 2, 3, self.root,
                {"primary": 1, "secondary": 2, "lower": 3, "upper": 4}, 21,
            )

    def test_subsurface_active_observation_requires_progress_within_its_budget(self) -> None:
        # These are actual schema-bound fixture events, not a replacement for
        # the disputed cadence/prefix parser. Only process/artifact I/O is fake.
        base = 1_000_000_000
        names = ("ready", "lower-state", "lower-state", "lower-moved", "sibling-created",
                 "lower-updated-under-upper", "lower-frame-generation",
                 "lower-frame-generation", "continuous-start")
        records = [{"event": name, "schema": live_run.SUBSURFACE_FIXTURE_SCHEMA,
                    "sequence": sequence, "monotonic_ns": base + sequence * 50_000_000}
                   for sequence, name in enumerate(names)]
        records.extend({"event": "continuous-generation", "schema": live_run.SUBSURFACE_FIXTURE_SCHEMA,
                        "sequence": 8 + generation, "monotonic_ns": base + (8 + generation) * 50_000_000,
                        "continuous_generation_id": generation, "producer_active": True,
                        "lower_state_id": 3 if generation % 2 else 4}
                       for generation in range(1, 4))
        snapshot = {
            "complete_transactions": [
                {"lower_state_id": state,
                 "packets": [{"role": "lower", "payload_sha256": str(state) * 64}]}
                for state in (3, 4)
            ], "inflight_transaction": None, "packet_count": 6,
        }
        for scenario in ("progress", "progress-during-capture", "progress-between-polls",
                         "stalled", "late-before", "late-after"):
            clock = [records[10]["monotonic_ns"] + 1]
            calls = 0

            def read_events(_server: str, current_scenario: str = scenario,
                            current_clock: list[int] = clock) -> list[dict[str, object]]:
                nonlocal calls
                calls += 1
                if current_scenario == "late-before" or (calls > 1 and current_scenario == "late-after"):
                    current_clock[0] = records[8]["monotonic_ns"] + 5_000_000_001
                advanced = calls > (2 if current_scenario == "progress-between-polls" else 1)
                if advanced and current_scenario.startswith("progress"):
                    current_clock[0] = records[11]["monotonic_ns"] + 1
                return records[:12 if advanced and current_scenario != "stalled" else 11]

            def one_observation(_name: str, check: Callable[[], bool], **_kwargs: object) -> None:
                if not check() and not check():
                    raise live_run.LabFailure("observation did not advance")

            captured = copy.deepcopy(snapshot)
            if scenario == "progress-during-capture":
                captured["complete_transactions"].append(copy.deepcopy(captured["complete_transactions"][0]))
                captured["packet_count"] = 9

            with (
                self.subTest(scenario=scenario),
                patch.object(live_run, "container_process_exists", return_value=True),
                patch.object(live_run, "read_container_subsurface_events", side_effect=read_events),
                patch.object(live_run.time, "monotonic_ns", side_effect=lambda current=clock: current[0]),
                patch.object(live_run, "synchronize_subsurface_saved_updates",
                             return_value={"updates": [{"sequence": 28}]}),
                patch.object(live_run, "_subsurface_continuous_transaction_snapshot", return_value=captured),
                patch.object(live_run, "podman_exec", return_value=completed([])),
                patch.object(live_run, "wait_for", side_effect=one_observation),
            ):
                if scenario.startswith("progress"):
                    result = live_run._wait_subsurface_continuous_active(
                        "server", 1, "client", 2, 3, self.root,
                        {"primary": 1, "secondary": 2, "lower": 3, "upper": 4}, 21,
                    )
                    self.assertEqual(result["fixture_generation_count"], 3)
                    self.assertEqual(result["initial_fixture_generation_count"], 2)
                else:
                    with self.assertRaisesRegex(live_run.LabFailure, "deadline|did not advance"):
                        live_run._wait_subsurface_continuous_active(
                            "server", 1, "client", 2, 3, self.root,
                            {"primary": 1, "secondary": 2, "lower": 3, "upper": 4}, 21,
                        )

    def test_subsurface_active_observer_uses_one_packet_frontier(self) -> None:
        directory = self.root / "subsurface-serial-cut"
        interaction = self.make_subsurface_fixture_artifacts(directory, coalesced=True)
        roles = {**interaction["parent_wids"], **interaction["child_wids"]}
        roles = {role: roles[role] for role in ("primary", "secondary", "lower", "upper")}
        updates = {role: live_run._subsurface_saved_updates(directory, wid)
                   for role, wid in roles.items()}
        # Reuse real canonical packet bytes for a fourth continuous capture.
        # The first primary inventory stops at the third stage0, while the
        # later child inventories legitimately include the fourth transaction.
        for role in ("lower", "upper"):
            template = next(packet for packet in updates[role]["updates"]
                            if packet["sequence"] == {"primary": 25, "lower": 26, "upper": 27}[role])
            info = json.loads((directory / template["relative_info"]).read_text())
            info["sequence"] += 6
            info["options"]["subsurface-transaction-id"] += 2
            target = directory / f"screen-updates/{roles[role]}/999"
            target.mkdir(mode=0o700)
            payload = directory / live_run._subsurface_saved_payload_relative(template)
            (target / "0.info").write_text(json.dumps(info))
            (target / "0.info").chmod(0o600)
            (target / info["file"]).write_bytes(payload.read_bytes())
            (target / info["file"]).chmod(0o600)
        for role, wid in roles.items():
            current = live_run._subsurface_saved_updates(directory, wid)
            current["updates"] = [packet for packet in current["updates"]
                                  if packet["sequence"] <= (28 if role == "primary" else 30)
                                  or (role != "primary" and "/999/" in packet["relative_info"])]
            updates[role] = current
        with self.assertRaisesRegex(live_run.LabFailure, "stage is invalid"):
            live_run._subsurface_continuous_transaction_snapshot(directory, updates, roles, after_sequence=21)
        expected = live_run._subsurface_continuous_transaction_snapshot(
            directory, updates, roles, after_sequence=21, before_sequence=29,
        )
        self.assertEqual(len(expected["complete_transactions"]), 2)
        self.assertEqual(len(expected["inflight_transaction"]["packets"]), 1)
        events = interaction["evidence"]["events"]
        samples = iter((events[:12], events[:14]))
        clock = [events[11]["monotonic_ns"] + 1]

        def read_events(_server: str) -> list[dict[str, object]]:
            value = next(samples)
            clock[0] = value[-1]["monotonic_ns"] + 1
            return value

        def observe_once(_name: str, check: Callable[[], bool], **_kwargs: object) -> None:
            if not check():
                raise live_run.LabFailure("serial inventories prevented active observation")

        with (
            patch.object(live_run, "container_process_exists", return_value=True),
            patch.object(live_run, "read_container_subsurface_events", side_effect=read_events),
            patch.object(live_run.time, "monotonic_ns", side_effect=lambda: clock[0]),
            patch.object(live_run, "synchronize_subsurface_saved_updates",
                         side_effect=lambda _server, _directory, wid: updates[next(role for role in roles if roles[role] == wid)]),
            patch.object(live_run, "podman_exec", return_value=completed([])),
            patch.object(live_run, "wait_for", side_effect=observe_once),
        ):
            active = live_run._wait_subsurface_continuous_active(
                "server", 1, "client", 2, 3, directory, roles, 21,
            )
        self.assertEqual(active["packet_cut_before_sequence"], 29)
        self.assertEqual(active["snapshot"], expected)
        broken = copy.deepcopy(updates)
        broken["lower"]["updates"] = [packet for packet in broken["lower"]["updates"]
                                       if packet["sequence"] != 23]
        with self.assertRaisesRegex(live_run.LabFailure, "stage is invalid"):
            live_run._subsurface_continuous_transaction_snapshot(
                directory, broken, roles, after_sequence=21, before_sequence=29,
            )

    def test_subsurface_active_diagnostics_retain_bounded_failure_context(self) -> None:
        output = StringIO()

        def exhaust_observations(_name: str, check: Callable[[], bool], **_kwargs: object) -> None:
            for _ in range(65):
                check()

        with (
            redirect_stdout(output),
            patch.object(live_run, "container_process_exists", return_value=True),
            patch.object(live_run, "read_container_subsurface_events", return_value=[]),
            patch.object(live_run, "wait_for", side_effect=exhaust_observations),
            self.assertRaisesRegex(live_run.LabFailure, "attempt bound"),
        ):
            live_run._wait_subsurface_continuous_active(
                "server", 1, "client", 2, 3, self.root,
                {"primary": 1, "secondary": 2, "lower": 3, "upper": 4}, 21,
            )
        prefix = "SUBSURFACE_CONTINUOUS_OBSERVATION "
        records = [json.loads(line.removeprefix(prefix)) for line in output.getvalue().splitlines()]
        self.assertEqual(len(records), 64)
        for attempt, record in enumerate(records, 1):
            self.assertEqual(record["attempt"], attempt)
            self.assertFalse(record["accepted"])
            self.assertEqual(record["stage"], "initial-source-prefix")
            self.assertEqual(record["reason"], "subsurface continuous fixture prefix is incomplete")
            self.assertEqual(record["roles"], {})
            self.assertLessEqual(record["started_monotonic_ns"], record["finished_monotonic_ns"])

    def test_subsurface_client_parent_identities_preserve_actual_decorated_titles(self) -> None:
        expected = {
            "primary": ("101", live_run.SUBSURFACE_FIXTURE_TITLE + " on owned-server"),
            "secondary": ("102", live_run.SUBSURFACE_REPARENT_TARGET_TITLE + " on owned-server"),
        }
        for change in (None, "xid", "title", "missing"):
            observed = dict(expected)
            if change == "xid":
                observed["primary"] = ("103", expected["primary"][1])
            elif change == "title":
                observed["primary"] = ("101", expected["primary"][1] + " changed")
            elif change == "missing":
                del observed["primary"]
            output = StringIO()
            listing = "".join(f"{xid}\t{title}\n" for xid, title in observed.values())
            with (self.subTest(change=change), redirect_stdout(output),
                  patch.object(live_run, "podman_exec", return_value=completed([], listing))):
                if change is None:
                    live_run._require_subsurface_client_parent_identities("client", expected)
                else:
                    with self.assertRaisesRegex(live_run.LabFailure, "client mapped XID or WM title"):
                        live_run._require_subsurface_client_parent_identities("client", expected)
            record = json.loads(output.getvalue().removeprefix("SUBSURFACE_CLIENT_PARENT_IDENTITIES "))
            self.assertEqual(record["phase"], "final")
            self.assertEqual(record["expected"], {role: list(value) for role, value in expected.items()})
            self.assertEqual(record["observed"],
                             {role: list(observed[role]) if role in observed else None for role in expected})
        long_expected = dict(expected)
        long_expected["primary"] = ("101", expected["primary"][1] + "x" * 400)
        listing = "101\t" + long_expected["primary"][1] + "changed\n"
        listing += "102\t" + expected["secondary"][1] + "\n"
        output = StringIO()
        with (redirect_stdout(output),
              patch.object(live_run, "podman_exec", return_value=completed([], listing)),
              self.assertRaisesRegex(live_run.LabFailure, "client mapped XID or WM title")):
            live_run._require_subsurface_client_parent_identities("client", long_expected)
        record = json.loads(output.getvalue().removeprefix("SUBSURFACE_CLIENT_PARENT_IDENTITIES "))
        self.assertEqual(len(record["observed"]["primary"][1]), 256)
        self.assertEqual(record["observed"]["primary"], record["expected"]["primary"])

    def test_subsurface_source_oracle_rejects_rebound_wrong_pixels(self) -> None:
        root = self.root / "subsurface-wrong-source"
        interaction = self.make_subsurface_fixture_artifacts(root)
        metadata = next(
            value for value in interaction["phases"]["stacked"]["streams"]
            if value["role"] == "upper"
        )
        payload_path = root / metadata["packet_payload"]
        payload = bytearray(payload_path.read_bytes())
        payload[0] ^= 1
        payload_path.write_bytes(payload)
        metadata["payload_sha256"] = live_run.sha256_file(payload_path)
        with self.assertRaisesRegex(live_run.LabFailure, "differs from fixture pixels"):
            live_run.subsurface_artifact_observations(
                root, parent_wids=interaction["parent_wids"],
                child_wids=interaction["child_wids"], fixture_pid=interaction["fixture_pid"],
                parent_sources=interaction["parent_sources"], phases=interaction["phases"],
            )

    def test_subsurface_topology_full_repairs_and_local_removals_are_distinct(self) -> None:
        interaction = self.make_subsurface_fixture_artifacts(self.root / "subsurface-topology-boundaries")
        # These explicit fixture canvas/footprint boundaries are independent of
        # the geometry table which generates the synthetic packet artifacts.
        expected = {
            "stacked": {"primary": (0, 0, 420, 300), "lower": (48, 110, 220, 140),
                        "upper": (150, 150, 160, 100)},
            "lower-destroyed": {"primary": (48, 110, 220, 140), "upper": (150, 150, 118, 100)},
            "upper-detached": {"primary": (150, 150, 160, 100)},
            "reparented": {"secondary": (0, 0, 360, 260), "reparented-upper": (80, 70, 160, 100)},
        }
        role_ids = {**interaction["parent_wids"], **interaction["child_wids"]}
        for phase, roles in expected.items():
            for role, geometry in roles.items():
                with self.subTest(phase=phase, role=role):
                    binding = next(item for item in interaction["phases"][phase]["streams"] if item["role"] == role)
                    packet = next(item for item in interaction["evidence"]["source_updates"][str(role_ids[role])]["updates"]
                                  if item["sequence"] == binding["sequences"][0])
                    self.assertEqual(tuple(packet[key] for key in ("x", "y", "w", "h")), geometry)
                    if role in interaction["parent_wids"]:
                        self.assertEqual(packet["options"]["subsurface-reset"], list(geometry))
        self.assertTrue(all(interaction["checks"].values()))

    def test_subsurface_topology_rejects_partial_new_role_backing_repairs(self) -> None:
        interaction = self.make_subsurface_fixture_artifacts(self.root / "subsurface-partial-topology")
        role_ids = {**interaction["parent_wids"], **interaction["child_wids"]}
        for phase, role, partial in (
            ("stacked", "primary", (150, 150, 160, 100)),
            ("stacked", "lower", (150, 150, 118, 100)),
            ("reparented", "secondary", (80, 70, 160, 100)),
        ):
            with self.subTest(phase=phase, role=role):
                changed = copy.deepcopy(interaction)
                binding = next(item for item in changed["phases"][phase]["streams"] if item["role"] == role)
                packet = next(item for item in changed["evidence"]["source_updates"][str(role_ids[role])]["updates"]
                              if item["sequence"] == binding["sequences"][0])
                packet.update(zip(("x", "y", "w", "h"), partial, strict=True))
                if role in interaction["parent_wids"]:
                    packet["options"]["subsurface-reset"] = list(partial)
                self.assertFalse(live_run.subsurface_interaction_checks(changed)["atomic_transaction_contract_exact"])

    def test_subsurface_parsers_accept_exact_fixture_and_stream_authorities(
        self,
    ) -> None:
        root = self.root / "subsurface-parsers"
        interaction = self.make_subsurface_fixture_artifacts(root)
        evidence = interaction["evidence"]
        self.assertEqual(
            interaction["child_wids"]["reparented-upper"],
            interaction["child_wids"]["upper"],
        )
        self.assertEqual(
            [event["event"] for event in evidence["events"]],
            [
                "ready",
                "lower-state",
                "lower-state",
                "lower-moved",
                "sibling-created",
                "lower-updated-under-upper",
                "lower-frame-generation",
                "lower-frame-generation",
                "continuous-start",
                "continuous-generation",
                "continuous-generation",
                "continuous-generation",
                "continuous-stop",
                "sibling-click",
                "lower-destroyed",
                "upper-detached",
                "upper-reparented",
                "exit",
            ],
        )
        self.assertEqual(
            evidence["events"][0]["lower_buffer_scale"],
            live_run.SUBSURFACE_LOWER_BUFFER_SCALE,
        )
        self.assertEqual(
            evidence["events"][0]["lower_buffer_dimensions"],
            list(live_run.SUBSURFACE_LOWER_BUFFER_DIMENSIONS),
        )
        self.assertEqual(
            evidence["events"][4]["upper_buffer_transform"],
            live_run.SUBSURFACE_UPPER_BUFFER_TRANSFORM,
        )
        self.assertIs(
            evidence["events"][4]["upper_precommitted_before_role"],
            True,
        )
        self.assertIs(
            next(
                event
                for event in evidence["events"]
                if event["event"] == "upper-reparented"
            )["upper_reattach_without_child_commit"],
            True,
        )
        detached = next(
            event for event in evidence["events"] if event["event"] == "upper-detached"
        )
        reparented = next(
            event for event in evidence["events"] if event["event"] == "upper-reparented"
        )
        self.assertEqual(reparented["upper_commit_count"], detached["upper_commit_count"])
        self.assertEqual(
            evidence["pointer_timing"],
            {
                "completed_monotonic_ns": 700_000_500,
                "deadline_ns": live_run.SUBSURFACE_INPUT_DEADLINE_NS,
                "elapsed_ns": 1_000,
                "fixture_event_monotonic_ns": 700_000_000,
                "schema": 1,
                "started_monotonic_ns": 699_999_500,
            },
        )
        self.assertEqual(
            evidence["stream"]["input"],
            {
                "client_ordered": True,
                "client_press": True,
                "client_release": True,
                "server_leaf_coordinates": True,
                "server_leaf_surface": True,
                "server_ordered": True,
                "server_press": True,
                "server_release": True,
                "server_root_coordinates": True,
                "server_root_wire": True,
            },
        )
        self.assertEqual(
            evidence["stream"]["publications"],
            [
                {
                    "encoding": "rgb32",
                    "sequence": sequence,
                    "source_wid": source_wid,
                    "wire_wid": wire_wid,
                }
                for sequence, source_wid, wire_wid in (
                    (3, 2, 1),
                    (5, 2, 1),
                    (7, 2, 1),
                    (9, 2, 1),
                    (11, 2, 1),
                    (12, 3, 1),
                    (14, 2, 1),
                    (15, 3, 1),
                    (17, 2, 1),
                    (18, 3, 1),
                    (20, 2, 1),
                    (21, 3, 1),
                    (23, 2, 1),
                    (24, 3, 1),
                    (26, 2, 1),
                    (27, 3, 1),
                    (29, 2, 1),
                    (30, 3, 1),
                    (32, 3, 1),
                    (35, 3, 5),
                )
            ],
        )
        self.assertEqual(
            [record["generation_id"] for record in evidence["frame_generations"]],
            [1, 2],
        )
        self.assertEqual(
            [record["frame_done_count"] for record in evidence["frame_generations"]],
            [1, 2],
        )
        self.assertEqual(
            {record["source_wid"] for record in evidence["frame_generations"]},
            {interaction["child_wids"]["lower"]},
        )
        self.assertEqual(
            len({record["payload_sha256"] for record in evidence["frame_generations"]}),
            len(live_run.SUBSURFACE_FRAME_PHASES),
        )
        self.assertEqual(
            evidence["info"]["initial"]["children"]["2"]["offset"],
            list(live_run.SUBSURFACE_INITIAL_OFFSET),
        )
        self.assertEqual(
            evidence["info"]["reparented"]["children"]["3"]["parent_wid"],
            5,
        )
        self.assertEqual(
            evidence["info"]["reparented"]["children"]["3"]["packets_sent"],
            1,
        )
        transactions = [
            (
                packet["options"]["subsurface-transaction-id"],
                packet["options"]["subsurface-stage-index"],
                packet["options"]["subsurface-stage-count"],
                packet["options"]["flush"],
            )
            for source in evidence["source_updates"].values()
            for packet in source["updates"]
            if "subsurface-transaction-id" in packet["options"]
        ]
        self.assertIn((1, 0, 2, 1), transactions)
        self.assertIn((1, 1, 2, 0), transactions)
        self.assertIn((12, 0, 2, 1), transactions)
        self.assertIn((12, 1, 2, 0), transactions)
        self.assertEqual(
            len(evidence["continuous"]["transactions"]["complete_transactions"]),
            3,
        )
        self.assertIsNone(
            evidence["continuous"]["transactions"]["inflight_transaction"]
        )
        self.assertEqual(
            len(
                evidence["continuous"]["liveness"]["active"]["snapshot"][
                    "complete_transactions"
                ]
            ),
            live_run.SUBSURFACE_CONTINUOUS_MIN_GENERATIONS,
        )
        self.assertIsNotNone(
            evidence["continuous"]["liveness"]["active"]["snapshot"][
                "inflight_transaction"
            ]
        )
        self.assertEqual(
            {
                packet["options"]["rgb_format"]
                for source in evidence["source_updates"].values()
                for packet in source["updates"]
                if "subsurface-transaction-id" in packet["options"]
            },
            set(live_run.SUBSURFACE_COMPOSITE_FORMATS),
        )
        self.assertEqual(
            evidence["source_updates"]["5"]["updates"][0]["encoding"],
            "rgb24",
        )
        self.assertTrue(
            all(
                "screenshots" not in source
                for source in evidence["source_updates"].values()
            )
        )
        self.assertTrue(all(interaction["checks"].values()))
        self.assertTrue(
            live_run.subsurface_artifact_evidence_matches(interaction, root)
        )

        pending_start = copy.deepcopy(evidence["events"])
        start_event = next(
            event for event in pending_start if event["event"] == "continuous-start"
        )
        start_event.update(
            {
                "frame_callback_pending": True,
                "frame_callback_ready": False,
                "frame_done_count": 2,
            }
        )
        live_run.validate_subsurface_fixture_events(pending_start)
        pending_interaction = copy.deepcopy(interaction)
        pending_interaction["evidence"]["events"] = pending_start
        self.assertTrue(
            all(live_run.subsurface_interaction_checks(pending_interaction).values())
        )

        completed_terminal = copy.deepcopy(evidence["events"])
        stop_event = next(
            event for event in completed_terminal if event["event"] == "continuous-stop"
        )
        stop_event.update(
            {
                "frame_done_count": stop_event["frame_done_count"] + 1,
                "pending_callback_cancelled": False,
                "terminal_callback_completed": True,
                "terminal_callback_data": 123,
                "terminal_callback_id": 456,
            }
        )
        live_run.validate_subsurface_fixture_events(completed_terminal)
        completed_interaction = copy.deepcopy(interaction)
        completed_interaction["evidence"]["events"] = completed_terminal
        self.assertTrue(
            all(live_run.subsurface_interaction_checks(completed_interaction).values())
        )
        stop_event["pending_callback_cancelled"] = True
        with self.assertRaisesRegex(live_run.LabFailure, "continuous stop"):
            live_run.validate_subsurface_fixture_events(completed_terminal)

        def fixture_events_with_generations(count: int) -> list[dict[str, object]]:
            source = evidence["events"]
            result = copy.deepcopy(source[:9])
            templates = source[9:11]
            continuous_ids = source[12]["continuous_buffer_ids"]
            for generation in range(1, count + 1):
                event = copy.deepcopy(templates[(generation - 1) % 2])
                event.update(
                    {
                        "continuous_generation_id": generation,
                        "frame_callback_data": 2_000 + generation,
                        "frame_callback_id": 1_000 + generation,
                        "frame_done_count": 2 + generation,
                        "lower_attach_count": 6 + generation,
                        "lower_buffer_id": continuous_ids[(generation - 1) % 2],
                        "lower_commit_count": 6 + generation,
                        "lower_state_id": 3 if generation % 2 else 4,
                        "lower_update_count": 5 + generation,
                    }
                )
                result.append(event)
            stop = copy.deepcopy(source[12])
            stop.update(
                {
                    "continuous_generation_count": count,
                    "frame_done_count": 2 + count,
                    "lower_attach_count": 6 + count,
                    "lower_buffer_id": continuous_ids[(count - 1) % 2],
                    "lower_commit_count": 6 + count,
                    "lower_state_id": 3 if count % 2 else 4,
                    "lower_update_count": 5 + count,
                }
            )
            result.append(stop)
            for event in copy.deepcopy(source[13:]):
                if "lower_update_count" in event:
                    event["lower_update_count"] = 5 + count
                result.append(event)
            for sequence, event in enumerate(result):
                event["sequence"] = sequence
                event["monotonic_ns"] = (sequence + 1) * 50_000_000
            return result

        for generation_count in (
            live_run.SUBSURFACE_CONTINUOUS_MIN_GENERATIONS,
            live_run.SUBSURFACE_CONTINUOUS_MAX_GENERATIONS,
        ):
            with self.subTest(continuous_generation_count=generation_count):
                live_run.validate_subsurface_fixture_events(
                    fixture_events_with_generations(generation_count)
                )
        with self.assertRaisesRegex(live_run.LabFailure, "generation count"):
            live_run.validate_subsurface_fixture_events(
                fixture_events_with_generations(
                    live_run.SUBSURFACE_CONTINUOUS_MIN_GENERATIONS - 1
                )
            )
        with self.assertRaisesRegex(live_run.LabFailure, "generation count"):
            live_run.validate_subsurface_fixture_events(
                fixture_events_with_generations(
                    live_run.SUBSURFACE_CONTINUOUS_MAX_GENERATIONS + 1
                )
            )

        active_at_cap = copy.deepcopy(evidence["continuous"]["liveness"])
        active_at_cap["active"]["fixture_generation_count"] = (
            live_run.SUBSURFACE_CONTINUOUS_MAX_GENERATIONS
        )
        with self.assertRaisesRegex(live_run.LabFailure, "liveness state"):
            live_run.validate_subsurface_continuous_liveness(
                active_at_cap,
                live_run.validate_subsurface_fixture_events(evidence["events"]),
                evidence["continuous"]["transactions"],
            )

        duplicate = root / "duplicate-events.stdout"
        duplicate.write_text(
            '{"event":"ready","event":"ready"}\n',
            encoding="utf-8",
        )
        duplicate.chmod(0o600)
        with self.assertRaises(live_run.LabFailure):
            live_run.load_subsurface_fixture_events(duplicate)

        bounded_line = '{"event":"bounded"}\n'
        maximum_events = live_run.SUBSURFACE_CONTINUOUS_MAX_GENERATIONS + 15
        self.assertEqual(
            len(
                live_run.parse_subsurface_fixture_jsonl_text(
                    bounded_line * maximum_events,
                    "bounded",
                )
            ),
            maximum_events,
        )
        with self.assertRaisesRegex(live_run.LabFailure, "event count"):
            live_run.parse_subsurface_fixture_jsonl_text(
                bounded_line * (maximum_events + 1),
                "over-bound",
            )

    def test_subsurface_source_over_uses_exact_premultiplied_wire_math(self) -> None:
        parent = live_run.Image.new("RGBA", (1, 1), (10, 20, 30, 255))
        child = live_run.Image.new("RGBA", (1, 1), (64, 32, 16, 128))
        observed = live_run._subsurface_source_over(parent, child, (0, 0))
        self.assertEqual(observed.getpixel((0, 0)), (69, 42, 31, 255))

        transparent = live_run.Image.new("RGBA", (1, 1), (0, 0, 0, 0))
        self.assertEqual(
            live_run._subsurface_source_over(parent, transparent, (0, 0)).getpixel(
                (0, 0)
            ),
            (10, 20, 30, 255),
        )
        with self.assertRaisesRegex(live_run.LabFailure, "not premultiplied"):
            live_run._subsurface_source_over(
                parent,
                live_run.Image.new("RGBA", (1, 1), (129, 0, 0, 128)),
                (0, 0),
            )

    def test_subsurface_server_info_accepts_signed_child_offsets(self) -> None:
        root = self.root / "subsurface-signed-offset"
        interaction = self.make_subsurface_fixture_artifacts(root)
        path = root / live_run.SUBSURFACE_INFO_ARTIFACTS["initial"]
        positive = repr(tuple(live_run.SUBSURFACE_INITIAL_OFFSET))
        signed = tuple(-value for value in live_run.SUBSURFACE_INITIAL_OFFSET)
        path.write_text(
            path.read_text(encoding="utf-8").replace(positive, repr(signed), 1),
            encoding="utf-8",
        )
        parsed = live_run.parse_subsurface_server_info(
            path,
            interaction["parent_wids"],
        )
        lower_wid = interaction["child_wids"]["lower"]
        self.assertEqual(parsed["children"][lower_wid]["offset"], list(signed))
        self.assertEqual(
            live_run._subsurface_exact_pair(list(signed), dimensions=False),
            list(signed),
        )
        self.assertIsNone(
            live_run._subsurface_exact_pair(list(signed), dimensions=True)
        )

    def test_subsurface_classifier_rejects_each_ownership_boundary_mutation(
        self,
    ) -> None:
        interaction = self.make_subsurface_fixture_artifacts(
            self.root / "subsurface-classifier"
        )

        def packet_for_sequence(value: dict[str, object], sequence: int) -> dict[str, object]:
            sources = value["evidence"]["source_updates"]
            for source in sources.values():
                for packet in source["updates"]:
                    if packet["sequence"] == sequence:
                        return packet
            raise AssertionError(f"missing synthetic packet sequence {sequence}")

        def set_phase_option(
            value: dict[str, object],
            sequences: tuple[int, ...],
            key: str,
            option: object,
        ) -> None:
            for sequence in sequences:
                packet_for_sequence(value, sequence)["options"][key] = option

        mutations = {
            "wire-wid": (
                lambda value: value["evidence"]["stream"]["publications"][0].__setitem__(
                    "wire_wid", 9
                ),
                "child_packets_target_current_parent",
            ),
            "ack-source": (
                lambda value: value["evidence"]["stream"]["acknowledgements"][0].__setitem__(
                    "source_wid", 9
                ),
                "child_ack_owner_exact",
            ),
            "route-order": (
                lambda value: value["evidence"]["stream"].__setitem__(
                    "route_order",
                    list(reversed(value["evidence"]["stream"]["route_order"])),
                ),
                "child_ack_owner_exact",
            ),
            "sequence-collision": (
                lambda value: value["phases"]["changed"]["streams"][0].__setitem__(
                    "sequences", [3]
                ),
                "global_damage_sequences_unique",
            ),
            "video-child": (
                lambda value: value["evidence"]["source_updates"]["2"]["updates"][0].__setitem__(
                    "encoding", "h264"
                ),
                "child_transactions_raw_rgb32_only",
            ),
            "rgb24-transaction-root": (
                lambda value: packet_for_sequence(value, 4).__setitem__(
                    "encoding", "rgb24"
                ),
                "atomic_transaction_contract_exact",
            ),
            "child-geometry": (
                lambda value: value["evidence"]["source_updates"]["2"]["updates"][0].__setitem__(
                    "x", 73
                ),
                "child_transactions_raw_rgb32_only",
            ),
            "negative-packet-destination": (
                lambda value: value["evidence"]["source_updates"]["2"]["updates"][0].__setitem__(
                    "x", -1
                ),
                "child_transactions_raw_rgb32_only",
            ),
            "pending-ack": (
                lambda value: value["evidence"]["info"]["changed"]["children"]["2"].__setitem__(
                    "ack_pending", 1
                ),
                "child_ack_drained",
            ),
            "pending-composite": (
                lambda value: value["evidence"]["info"]["changed"].__setitem__(
                    "subsurface_pending", 1,
                ),
                "child_ack_drained",
            ),
            "inflight-composite": (
                lambda value: value["evidence"]["info"]["changed"].__setitem__(
                    "subsurface_inflight", 1,
                ),
                "child_ack_drained",
            ),
            "packet-count-jump": (
                lambda value: value["evidence"]["info"]["changed"]["children"]["2"].__setitem__(
                    "packets_sent", 3
                ),
                "same_lower_updated_repeatedly",
            ),
            "stale-pixels": (
                lambda value: value["evidence"]["pixels"]["comparisons"]["restored"]["primary"]["comparison"].__setitem__(
                    "exact", False
                ),
                "restored_alpha_composite_exact",
            ),
            "child-retained": (
                lambda value: value["evidence"]["info"]["lower-destroyed"].__setitem__(
                    "children", {"2": {}}
                ),
                "lower_source_removed",
            ),
            "missing-wire-mode": (
                lambda value: value["evidence"]["source_updates"]["2"]["updates"][0]["options"].pop(
                    "subsurface-composite"
                ),
                "premultiplied_source_over_wire_contract",
            ),
            "duplicate-reset": (
                lambda value: value["evidence"]["source_updates"]["2"]["updates"][0]["options"].__setitem__(
                    "subsurface-reset",
                    list(live_run.SUBSURFACE_PHASE_GEOMETRIES[("initial", "lower")]),
                ),
                "atomic_transaction_contract_exact",
            ),
            "client-input-order": (
                lambda value: value["evidence"]["stream"]["input"].__setitem__(
                    "client_ordered", False
                ),
                "client_pointer_path",
            ),
            "server-input-coordinates": (
                lambda value: value["evidence"]["stream"]["input"].__setitem__(
                    "server_leaf_coordinates", False
                ),
                "server_pointer_path",
            ),
            "server-input-leaf": (
                lambda value: value["evidence"]["stream"]["input"].__setitem__(
                    "server_leaf_surface", False
                ),
                "server_pointer_path",
            ),
            "server-input-root": (
                lambda value: value["evidence"]["stream"]["input"].__setitem__(
                    "server_root_wire", False
                ),
                "server_pointer_path",
            ),
            "server-input-order": (
                lambda value: value["evidence"]["stream"]["input"].__setitem__(
                    "server_ordered", False
                ),
                "server_pointer_path",
            ),
            "pointer-deadline": (
                lambda value: value["evidence"]["pointer_timing"].update(
                    {
                        "completed_monotonic_ns": (
                            value["evidence"]["pointer_timing"][
                                "started_monotonic_ns"
                            ]
                            + live_run.SUBSURFACE_INPUT_DEADLINE_NS
                            + 1
                        ),
                        "elapsed_ns": live_run.SUBSURFACE_INPUT_DEADLINE_NS + 1,
                    }
                ),
                "fixture_pointer_path",
            ),
            "missing-transaction-id": (
                lambda value: packet_for_sequence(value, 3)["options"].pop(
                    "subsurface-transaction-id"
                ),
                "atomic_transaction_contract_exact",
            ),
            "duplicate-transaction-id": (
                lambda value: set_phase_option(
                    value,
                    (4, 5),
                    "subsurface-transaction-id",
                    1,
                ),
                "atomic_transaction_contract_exact",
            ),
            "stage-index-gap": (
                lambda value: packet_for_sequence(value, 3)["options"].__setitem__(
                    "subsurface-stage-index", 2
                ),
                "atomic_transaction_contract_exact",
            ),
            "stage-count-mismatch": (
                lambda value: packet_for_sequence(value, 3)["options"].__setitem__(
                    "subsurface-stage-count", 3
                ),
                "atomic_transaction_contract_exact",
            ),
            "topology-epoch-mismatch": (
                lambda value: packet_for_sequence(value, 5)["options"].__setitem__(
                    "subsurface-topology-epoch", 99
                ),
                "atomic_transaction_contract_exact",
            ),
            "backing-epoch-mismatch": (
                lambda value: packet_for_sequence(value, 5)["options"].__setitem__(
                    "subsurface-backing-epoch", 99
                ),
                "atomic_transaction_contract_exact",
            ),
            "flush-order-mismatch": (
                lambda value: packet_for_sequence(value, 3)["options"].__setitem__(
                    "flush", 1
                ),
                "atomic_transaction_contract_exact",
            ),
            "non-premultiplied-source": (
                lambda value: value["evidence"]["pixels"]["alpha"]["initial:lower"].__setitem__(
                    "premultiplied", False
                ),
                "child_sources_have_transparency",
            ),
            "frame-generation-payload-digest": (
                lambda value: value["evidence"]["frame_generations"][0].__setitem__(
                    "payload_sha256", "0" * 64
                ),
                "child_frame_generations_exact",
            ),
            "frame-generation-order": (
                lambda value: value["evidence"].__setitem__(
                    "frame_generations",
                    list(reversed(value["evidence"]["frame_generations"])),
                ),
                "child_frame_generations_exact",
            ),
            "continuous-active-after-stop": (
                lambda value: value["evidence"]["continuous"]["liveness"][
                    "active"
                ].__setitem__(
                    "observed_monotonic_ns",
                    value["evidence"]["continuous"]["liveness"][
                        "stop_requested_monotonic_ns"
                    ]
                    + 1,
                ),
                "continuous_child_active_liveness",
            ),
            "continuous-active-without-progress": (
                lambda value: value["evidence"]["continuous"]["liveness"]["active"].__setitem__(
                    "initial_fixture_generation_count",
                    value["evidence"]["continuous"]["liveness"]["active"]["fixture_generation_count"],
                ),
                "continuous_child_active_liveness",
            ),
            "continuous-stale-liveness-schema": (
                lambda value: value["evidence"]["continuous"]["liveness"].__setitem__("schema", 2),
                "continuous_child_active_liveness",
            ),
            "continuous-packet-frontier-shift": (
                lambda value: value["evidence"]["continuous"]["liveness"]["active"].__setitem__(
                    "packet_cut_before_sequence",
                    value["evidence"]["continuous"]["liveness"]["active"]["packet_cut_before_sequence"] + 1,
                ),
                "continuous_child_active_liveness",
            ),
            "continuous-active-only-before-observation": (
                lambda value: value["evidence"]["continuous"]["liveness"]["active"].__setitem__(
                    "observation_started_monotonic_ns",
                    value["evidence"]["continuous"]["liveness"]["active"]["fixture_event_monotonic_ns"],
                ),
                "continuous_child_active_liveness",
            ),
            "continuous-stage-flush": (
                lambda value: packet_for_sequence(value, 23)["options"].__setitem__(
                    "flush", True
                ),
                "continuous_transactions_complete",
            ),
            "continuous-callback-accounting": (
                lambda value: next(
                    event
                    for event in value["evidence"]["events"]
                    if event["event"] == "continuous-stop"
                ).__setitem__("lower_attach_count", 99),
                "continuous_callback_accounting_exact",
            ),
            "continuous-generation-bool-id": (
                lambda value: next(
                    event
                    for event in value["evidence"]["events"]
                    if event["event"] == "continuous-generation"
                ).__setitem__("continuous_generation_id", True),
                "fixture_event_stream_exact",
            ),
            "continuous-final-pixels": (
                lambda value: value["evidence"]["pixels"]["comparisons"][
                    live_run.SUBSURFACE_CONTINUOUS_FINAL_PHASE
                ]["primary"]["comparison"].__setitem__("exact", False),
                "continuous_final_composite_exact",
            ),
            "child-eos": (
                lambda value: value["evidence"]["stream"]["eos_window_ids"].append(2),
                "no_child_eos",
            ),
            "destroy-refresh-geometry": (
                lambda value: next(
                    packet
                    for packet in value["evidence"]["source_updates"]["1"]["updates"]
                    if packet["sequence"] == 31
                ).__setitem__(
                    "w", 219
                ),
                "atomic_transaction_contract_exact",
            ),
            "reparent-buffer-changed": (
                lambda value: next(
                    event
                    for event in value["evidence"]["events"]
                    if event["event"] == "upper-reparented"
                ).__setitem__("upper_buffer_id", 999),
                "reparent_preserves_surface_and_buffer",
            ),
            "reparent-wid-replaced": (
                lambda value: value["child_wids"].__setitem__(
                    "reparented-upper", 4
                ),
                "upper_wid_stable_and_role_rebound",
            ),
            "reparent-role-wrong-parent": (
                lambda value: value["evidence"]["info"]["reparented"][
                    "children"
                ]["3"].__setitem__("parent_wid", 1),
                "upper_wid_stable_and_role_rebound",
            ),
            "upper-source-retained-while-detached": (
                lambda value: value["evidence"]["info"]["upper-detached"].__setitem__(
                    "children", {"3": {}}
                ),
                "upper_wid_stable_and_role_rebound",
            ),
            "upper-packet-while-detached": (
                lambda value: value["evidence"]["source_updates"]["3"][
                    "updates"
                ].insert(
                    -1,
                    {
                        **copy.deepcopy(
                            value["evidence"]["source_updates"]["3"]["updates"][-1]
                        ),
                        "sequence": 34,
                    },
                ),
                "upper_wid_stable_and_role_rebound",
            ),
            "lower-buffer-not-scaled": (
                lambda value: value["evidence"]["events"][0].__setitem__(
                    "lower_buffer_scale", 1
                ),
                "fixture_event_stream_exact",
            ),
            "lower-buffer-not-physical-2x": (
                lambda value: value["evidence"]["events"][0].__setitem__(
                    "lower_buffer_dimensions",
                    list(live_run.SUBSURFACE_LOWER_DIMENSIONS),
                ),
                "fixture_event_stream_exact",
            ),
            "upper-transform-missing": (
                lambda value: value["evidence"]["events"][4].__setitem__(
                    "upper_buffer_transform", "normal"
                ),
                "fixture_event_stream_exact",
            ),
            "upper-not-precommitted": (
                lambda value: value["evidence"]["events"][4].__setitem__(
                    "upper_precommitted_before_role", False
                ),
                "fixture_event_stream_exact",
            ),
            "upper-reattach-child-commit": (
                lambda value: next(
                    event
                    for event in value["evidence"]["events"]
                    if event["event"] == "upper-reparented"
                ).__setitem__("upper_reattach_without_child_commit", False),
                "fixture_event_stream_exact",
            ),
            "upper-reattach-without-parent-commit": (
                lambda value: next(
                    event
                    for event in value["evidence"]["events"]
                    if event["event"] == "upper-reparented"
                ).__setitem__("upper_reattach_parent_committed", False),
                "fixture_event_stream_exact",
            ),
        }
        for name, (mutate, failed_check) in mutations.items():
            with self.subTest(name=name):
                changed = copy.deepcopy(interaction)
                mutate(changed)
                checks = live_run.subsurface_interaction_checks(changed)
                self.assertFalse(checks[failed_check])

        malformed = copy.deepcopy(interaction)
        malformed["child_wids"] = {}
        self.assertFalse(any(live_run.subsurface_interaction_checks(malformed).values()))

    def test_subsurface_artifact_recomputation_rejects_mutated_authorities(
        self,
    ) -> None:
        def mutate_log(root: Path) -> None:
            path = root / "server.stderr"
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "published as wire window 0x1",
                    "published as wire window 0x9",
                    1,
                ),
                encoding="utf-8",
            )

        def mutate_pointer_log(root: Path) -> None:
            path = root / "server.stderr"
            data = path.read_text(encoding="utf-8")
            match = re.search(
                r"Wayland pointer target root=0x[0-9a-f]+ "
                r"surface=0x[0-9a-f]+ local=(?P<x>-?[0-9]+\.[0-9]{3}),",
                data,
            )
            self.assertIsNotNone(match)
            assert match is not None
            coordinate = float(match.group("x")) + 1.0
            path.write_text(
                data[:match.start("x")]
                + f"{coordinate:.3f}"
                + data[match.end("x"):],
                encoding="utf-8",
            )

        def mutate_pointer_root(root: Path) -> None:
            path = root / "server.stderr"
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "Wayland pointer target root=0x1 ",
                    "Wayland pointer target root=0x5 ",
                    1,
                ),
                encoding="utf-8",
            )

        def mutate_pointer_surface(root: Path) -> None:
            path = root / "server.stderr"
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    " surface=0x3 local=",
                    " surface=0x2 local=",
                    1,
                ),
                encoding="utf-8",
            )

        def mutate_pointer_timing(root: Path) -> None:
            path = root / live_run.SUBSURFACE_POINTER_TIMING_ARTIFACT
            value = json.loads(path.read_text(encoding="utf-8"))
            value["deadline_ns"] -= 1
            path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")

        def mutate_info(root: Path) -> None:
            path = root / live_run.SUBSURFACE_INFO_ARTIFACTS["changed"]
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "damage.ack-pending=0",
                    "damage.ack-pending=1",
                ),
                encoding="utf-8",
            )

        def mutate_destroyed_info(root: Path) -> None:
            path = root / live_run.SUBSURFACE_INFO_ARTIFACTS["lower-destroyed"]
            with path.open("a", encoding="utf-8") as stream:
                stream.write(
                    "client.0.window.windows.1.subsurfaces.2."
                    "info.damage.ack-pending=0\n"
                )

        def mutate_reparented_wid(root: Path) -> None:
            path = root / live_run.SUBSURFACE_INFO_ARTIFACTS["reparented"]
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    ".subsurfaces.3.",
                    ".subsurfaces.4.",
                ),
                encoding="utf-8",
            )

        def mutate_pixels(root: Path) -> None:
            path = root / live_run.subsurface_client_rgb_artifact(
                "primary",
                "restored",
            )
            with live_run.Image.open(path) as source:
                image = source.convert("RGB")
            image.putpixel(live_run.SUBSURFACE_INITIAL_OFFSET, (0, 0, 0))
            image.save(path, format="PNG")

        def mutate_events(root: Path) -> None:
            path = root / "subsurface-fixture.stdout"
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    '"lower_state_id": 2',
                    '"lower_state_id": 1',
                    1,
                ),
                encoding="utf-8",
            )

        def mutate_continuous_liveness(root: Path) -> None:
            path = root / live_run.SUBSURFACE_CONTINUOUS_LIVENESS_ARTIFACT
            value = json.loads(path.read_text(encoding="utf-8"))
            value["active"]["observed_monotonic_ns"] = (
                value["stop_requested_monotonic_ns"] + 1
            )
            path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")

        def mutate_continuous_event_reference(root: Path) -> None:
            path = root / live_run.SUBSURFACE_CONTINUOUS_LIVENESS_ARTIFACT
            value = json.loads(path.read_text(encoding="utf-8"))
            value["active"]["fixture_event_sequence"] = float(
                value["active"]["fixture_event_sequence"]
            )
            path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")

        def mutate_continuous_active_prefix(root: Path) -> None:
            path = root / live_run.SUBSURFACE_CONTINUOUS_LIVENESS_ARTIFACT
            value = json.loads(path.read_text(encoding="utf-8"))
            value["active"]["snapshot"]["complete_transactions"].reverse()
            path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")

        def mutate_continuous_empty_inflight(root: Path) -> None:
            path = root / live_run.SUBSURFACE_CONTINUOUS_LIVENESS_ARTIFACT
            value = json.loads(path.read_text(encoding="utf-8"))
            value["active"]["snapshot"]["inflight_transaction"]["packets"] = []
            value["active"]["snapshot"]["packet_count"] = 6
            path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")

        def mutate_continuous_active_nested_integer(root: Path) -> None:
            path = root / live_run.SUBSURFACE_CONTINUOUS_LIVENESS_ARTIFACT
            value = json.loads(path.read_text(encoding="utf-8"))
            value["active"]["snapshot"]["complete_transactions"][0]["packets"][1][
                "stage_index"
            ] = True
            path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")

        def mutate_continuous_drained_nested_integer(root: Path) -> None:
            path = root / live_run.SUBSURFACE_CONTINUOUS_LIVENESS_ARTIFACT
            value = json.loads(path.read_text(encoding="utf-8"))
            value["drained"]["snapshot"]["complete_transactions"][0]["packets"][0][
                "source_wid"
            ] = True
            path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")

        def mutate_continuous_info(root: Path) -> None:
            path = root / live_run.SUBSURFACE_CONTINUOUS_INFO_ARTIFACT
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "damage.ack-pending=0",
                    "damage.ack-pending=1",
                    1,
                ),
                encoding="utf-8",
            )

        def mutate_continuous_pixels(root: Path) -> None:
            path = root / live_run.subsurface_client_rgb_artifact(
                "primary",
                live_run.SUBSURFACE_CONTINUOUS_FINAL_PHASE,
            )
            with live_run.Image.open(path) as source:
                image = source.convert("RGB")
            coordinate = live_run.SUBSURFACE_CONTINUOUS_GEOMETRY[:2]
            red, green, blue = image.getpixel(coordinate)
            image.putpixel(coordinate, ((red + 1) % 256, green, blue))
            image.save(path, format="PNG")

        def mutate_continuous_packet(root: Path) -> None:
            path = root / "screen-updates" / "2" / "308" / "0.info"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["options"]["flush"] = True
            path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

        def mutate_continuous_secondary_source(root: Path) -> None:
            source = root / "screen-updates" / "1" / "108"
            target = root / "screen-updates" / "5" / "202"
            shutil.copytree(source, target)
            shutil.rmtree(source)

        def mutate_continuous_fixture_fields(root: Path) -> None:
            path = root / "subsurface-fixture.stdout"
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            next(
                record
                for record in records
                if record["event"] == "continuous-generation"
            ).pop("producer_active")
            path.write_text(
                "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
                encoding="utf-8",
            )

        def mutate_wire_contract(root: Path) -> None:
            path = root / "screen-updates" / "2" / "300" / "0.info"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["options"].pop("subsurface-composite")
            path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

        def mutate_packet_payload(root: Path) -> None:
            path = root / "screen-updates" / "2" / "300" / "0.rgb32"
            payload = bytearray(path.read_bytes())
            payload[0] ^= 0xFF
            path.write_bytes(payload)

        def mutate_packet_info(
            root: Path,
            callback: Callable[[dict[str, object]], None],
        ) -> None:
            path = root / "screen-updates" / "2" / "300" / "0.info"
            payload = json.loads(path.read_text(encoding="utf-8"))
            callback(payload)
            path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

        def mark_packet_compressed(root: Path) -> None:
            mutate_packet_info(root, lambda payload: payload.__setitem__("compressed", "lz4"))

        def mutate_packet_stride(root: Path) -> None:
            mutate_packet_info(
                root,
                lambda payload: payload.__setitem__("stride", payload["stride"] + 4),
            )

        def mutate_packet_destination(root: Path) -> None:
            mutate_packet_info(
                root,
                lambda payload: payload.__setitem__("x", -1),
            )

        def mutate_packet_format(root: Path) -> None:
            mutate_packet_info(
                root,
                lambda payload: payload["options"].__setitem__("rgb_format", "ARGB"),
            )

        def mutate_packet_path(root: Path) -> None:
            mutate_packet_info(
                root,
                lambda payload: payload.__setitem__("file", "../0.rgb32"),
            )

        def duplicate_packet_field(root: Path) -> None:
            path = root / "screen-updates" / "2" / "300" / "0.info"
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "{",
                    '{"encoding":"rgb24",',
                    1,
                ),
                encoding="utf-8",
            )

        def add_unexpected_source(root: Path) -> None:
            path = root / "screen-updates" / "99"
            path.mkdir(mode=0o700)

        for name, mutate in {
            "packet-log": mutate_log,
            "pointer-log": mutate_pointer_log,
            "pointer-root": mutate_pointer_root,
            "pointer-surface": mutate_pointer_surface,
            "pointer-timing": mutate_pointer_timing,
            "server-info": mutate_info,
            "server-info-stale-child": mutate_destroyed_info,
            "server-info-replaced-reparent-wid": mutate_reparented_wid,
            "client-pixels": mutate_pixels,
            "continuous-active-nested-integer": mutate_continuous_active_nested_integer,
            "continuous-active-prefix": mutate_continuous_active_prefix,
            "continuous-drained-nested-integer": mutate_continuous_drained_nested_integer,
            "continuous-empty-inflight": mutate_continuous_empty_inflight,
            "continuous-event-reference": mutate_continuous_event_reference,
            "continuous-fixture-fields": mutate_continuous_fixture_fields,
            "continuous-client-pixels": mutate_continuous_pixels,
            "continuous-liveness": mutate_continuous_liveness,
            "continuous-packet": mutate_continuous_packet,
            "continuous-secondary-source": mutate_continuous_secondary_source,
            "continuous-server-info": mutate_continuous_info,
            "fixture-events": mutate_events,
            "packet-compression": mark_packet_compressed,
            "packet-duplicate-field": duplicate_packet_field,
            "packet-destination": mutate_packet_destination,
            "packet-format": mutate_packet_format,
            "packet-path": mutate_packet_path,
            "packet-payload": mutate_packet_payload,
            "packet-stride": mutate_packet_stride,
            "source-inventory": add_unexpected_source,
            "wire-contract": mutate_wire_contract,
        }.items():
            with self.subTest(name=name):
                root = self.root / f"subsurface-artifact-{name}"
                interaction = self.make_subsurface_fixture_artifacts(root)
                mutate(root)
                self.assertFalse(
                    live_run.subsurface_artifact_evidence_matches(interaction, root)
                )

    def test_subsurface_artifact_recomputation_ignores_async_source_screenshots(
        self,
    ) -> None:
        root = self.root / "subsurface-artifact-no-screenshot-authority"
        interaction = self.make_subsurface_fixture_artifacts(root)
        self.assertEqual(list(root.glob("screen-updates/*/*/screenshot.png")), [])
        for info_path in root.glob("screen-updates/*/*/0.info"):
            screenshot = info_path.parent / "screenshot.png"
            screenshot.write_bytes(b"not an authority")
            screenshot.chmod(0o600)
        self.assertTrue(
            live_run.subsurface_artifact_evidence_matches(interaction, root)
        )
        screenshots = ["screen-updates/1/1/screenshot.png"]
        self.assertEqual(
            live_run.pixel_pipeline_source_screenshots("subsurface", screenshots),
            [],
        )
        self.assertEqual(
            live_run.pixel_pipeline_source_screenshots("zed", screenshots),
            screenshots,
        )
        packet_info = tuple(
            path.relative_to(root).as_posix()
            for path in sorted((root / "screen-updates" / "1").glob("*/0.info"))
        )
        with (
            patch.object(
                live_run,
                "container_artifact_files",
                return_value=packet_info,
            ),
            patch.object(live_run, "pull_container_artifacts") as pull,
        ):
            updates = live_run.synchronize_subsurface_saved_updates(
                "server",
                root,
                1,
            )
            self.assertTrue(live_run.subsurface_startup_packet_ready("server", root, 1))
        self.assertNotIn("screenshots", updates)
        pull.assert_not_called()

    def test_subsurface_collection_excludes_async_source_screenshots(self) -> None:
        listing = "server.stderr\0screen-updates\0subsurface-fixture.stdout\0"
        with (
            patch.object(
                live_run,
                "podman_exec",
                return_value=completed(["find"], listing),
            ),
            patch.object(live_run, "pull_container_artifacts") as pull,
        ):
            live_run.pull_all_container_artifacts(
                "server",
                self.root,
                "server",
                include_screen_updates=False,
            )
        pull.assert_called_once_with(
            "server",
            self.root,
            ("server.stderr", "subsurface-fixture.stdout"),
        )

        with patch.object(
            live_run,
            "podman_exec",
            return_value=completed(["find"], "1\0" "5\0" "2\0" "3\0"),
        ) as execute:
            self.assertEqual(
                live_run.container_subsurface_source_wids("server"),
                {1, 2, 3, 5},
            )
        self.assertEqual(
            execute.call_args.args,
            (
                "server",
                [
                    "find",
                    "/artifacts/screen-updates",
                    "-mindepth",
                    "1",
                    "-maxdepth",
                    "1",
                    "-printf",
                    "%f\\0",
                ],
            ),
        )
        self.assertEqual(execute.call_args.kwargs, {"announce": False})
        for name, invalid in {
            "empty": "",
            "duplicate": "1\0" "1\0",
            "non-numeric": "screenshot.png\0",
            "non-positive": "0\0",
            "out-of-range": f"{2**31}\0",
        }.items():
            with (
                self.subTest(name=name),
                patch.object(
                    live_run,
                    "podman_exec",
                    return_value=completed(["find"], invalid),
                ),
                self.assertRaisesRegex(
                    live_run.LabFailure,
                    "subsurface saved source",
                ),
            ):
                live_run.container_subsurface_source_wids("server")

    def test_subsurface_packet_sync_refreshes_an_incomplete_info_sidecar(self) -> None:
        root = self.root / "subsurface-incomplete-info"
        self.make_subsurface_fixture_artifacts(root)
        relative = "screen-updates/2/300/0.info"
        path = root / relative
        complete = path.read_bytes()
        path.write_bytes(b"{\n")

        def refresh(
            _container: str,
            _directory: Path,
            relatives: tuple[str, ...],
        ) -> None:
            self.assertEqual(relatives, (relative,))
            path.write_bytes(complete)

        with (
            patch.object(
                live_run,
                "container_artifact_files",
                return_value=(relative,),
            ),
            patch.object(
                live_run,
                "pull_container_artifacts",
                side_effect=refresh,
            ) as pull,
        ):
            updates = live_run.synchronize_subsurface_saved_updates(
                "server",
                root,
                2,
            )
        self.assertEqual(updates["updates"][0]["sequence"], 3)
        pull.assert_called_once_with("server", root, (relative,))

    def test_subsurface_raw_packet_decoder_rejects_non_raw_authorities(self) -> None:
        mutations = {
            "compression": (
                lambda packet: packet.__setitem__("compressed", "lz4"),
                "payload is compressed",
            ),
            "format": (
                lambda packet: packet["options"].__setitem__("rgb_format", "ARGB"),
                "RGB format or stride is invalid",
            ),
            "stride": (
                lambda packet: packet.__setitem__(
                    "stride", packet["w"] * 4 - 1
                ),
                "RGB format or stride is invalid",
            ),
            "size": (
                lambda packet: packet.__setitem__("stride", packet["stride"] + 4),
                "payload size is invalid",
            ),
        }
        for name, (mutate, message) in mutations.items():
            with self.subTest(name=name):
                root = self.root / f"subsurface-raw-{name}"
                self.make_subsurface_fixture_artifacts(root)
                info_path = root / "screen-updates" / "2" / "300" / "0.info"
                info = json.loads(info_path.read_text(encoding="utf-8"))
                mutate(info)
                info_path.write_text(json.dumps(info, sort_keys=True), encoding="utf-8")
                packets = live_run._subsurface_saved_updates(root, 2)["updates"]
                packet = next(packet for packet in packets if packet["sequence"] == 3)
                with self.assertRaisesRegex(live_run.LabFailure, message):
                    live_run._subsurface_raw_packet_image(
                        root,
                        packet,
                        2,
                        composite=True,
                    )

        root = self.root / "subsurface-raw-unsafe-path"
        self.make_subsurface_fixture_artifacts(root)
        info_path = root / "screen-updates" / "2" / "300" / "0.info"
        info = json.loads(info_path.read_text(encoding="utf-8"))
        info["file"] = "../0.rgb32"
        info_path.write_text(json.dumps(info, sort_keys=True), encoding="utf-8")
        with self.assertRaisesRegex(live_run.LabFailure, "payload path is unsafe"):
            live_run._subsurface_saved_updates(root, 2)

    def test_subsurface_job_classifier_recomputes_exact_checks(self) -> None:
        root = self.root / "subsurface-job"
        interaction = self.make_subsurface_fixture_artifacts(root)
        embedded = {
            "classification": {
                "boundaries": {"interaction": interaction["checks"]}
            },
            "interaction": interaction,
        }
        embedded = json.loads(json.dumps(embedded, sort_keys=True))
        self.assertTrue(
            job.subsurface_fixture_artifact_evidence_matches(
                embedded,
                root,
                live_run,
            )
        )
        for name, mutate in {
            "classified": lambda value: value["classification"]["boundaries"].__setitem__(
                "interaction", {}
            ),
            "reported": lambda value: value["interaction"]["checks"].__setitem__(
                "no_child_eos", False
            ),
            "extra": lambda value: value["interaction"]["checks"].__setitem__(
                "unreviewed", True
            ),
        }.items():
            with self.subTest(name=name):
                changed = copy.deepcopy(embedded)
                mutate(changed)
                self.assertFalse(
                    job.subsurface_fixture_artifact_evidence_matches(
                        changed,
                        root,
                        live_run,
                    )
                )

    def test_report_validation_binds_gtk_lifecycle_authority_files(self) -> None:
        def rewrite_report(run: str, payload: dict[str, object]) -> None:
            report_path = job.result_path(run)
            scenario = payload["scenarios"][0]
            scenario_path = report_path.parent / scenario["name"] / "report.json"
            scenario_path.write_text(
                json.dumps(scenario, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            payload["scenario_report_sha256"][scenario["name"]] = job.sha256_file(
                scenario_path
            )
            report_path.write_text(
                json.dumps(payload, sort_keys=True) + "\n",
                encoding="utf-8",
            )

        mutations = (
            "missing-identity",
            "changed-identity",
            "activity",
            "process-alive",
            "hardware-pid",
            "hardware-argv",
            "server-identity",
            "server-pid",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                run = f"gtk-authority-{mutation}"
                record = self.record(run)
                record["application"] = "gtk"
                record["lifecycle"] = "detach"
                job.prepare_private_state()
                self.make_report(run, record)
                _result, _digest, initial = job.report_validation(run, record)
                self.assertTrue(initial["evidence_tree"])

                report_path = job.result_path(run)
                payload = json.loads(report_path.read_text(encoding="utf-8"))
                scenario = payload["scenarios"][0]
                scenario_root = report_path.parent / scenario["name"]
                if mutation == "missing-identity":
                    (scenario_root / live_run.INTERACTION_IDENTITY_ARTIFACT).unlink()
                    del scenario["artifact_sha256"][
                        live_run.INTERACTION_IDENTITY_ARTIFACT
                    ]
                elif mutation == "changed-identity":
                    identity_path = (
                        scenario_root / live_run.INTERACTION_IDENTITY_ARTIFACT
                    )
                    identity_path.write_text(
                        json.dumps(interaction_identity(pid=42), sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                    scenario["artifact_sha256"][
                        live_run.INTERACTION_IDENTITY_ARTIFACT
                    ] = job.sha256_file(identity_path)
                elif mutation == "activity":
                    scenario["application_activity"][
                        "process_identity"
                    ] = interaction_identity(pid=42)
                elif mutation == "process-alive":
                    scenario["application_activity"]["process_alive"] = False
                elif mutation == "hardware-pid":
                    scenario["hardware"]["application"]["pid"] = 42
                elif mutation == "hardware-argv":
                    scenario["hardware"]["application"]["argv"] = "python3 fixture.py"
                elif mutation == "server-identity":
                    scenario["lifecycle"]["server_identity_at_capture"] = (
                        server_identity(pid=9)
                    )
                else:
                    server_pid_path = scenario_root / "server.pid"
                    server_pid_path.write_text("9\n", encoding="ascii")
                    scenario["artifact_sha256"]["server.pid"] = job.sha256_file(
                        server_pid_path
                    )
                rewrite_report(run, payload)
                _result, _digest, checks = job.report_validation(run, record)
                self.assertFalse(checks["evidence_tree"])

    def test_report_validation_accepts_complete_gtk_transport_identities(self) -> None:
        run = "gtk-transport-identities"
        record = self.record(run)
        record["application"] = "gtk"
        record["lifecycle"] = "transport-loss"
        job.prepare_private_state()
        self.make_report(run, record)
        _result, _digest, checks = job.report_validation(run, record)
        self.assertTrue(checks["evidence_tree"])

    def test_collect_rejects_running_process(self) -> None:
        run = "running"
        record = self.record(run)
        job.prepare_private_state()
        with (
            patch.object(job, "load_record", return_value=record),
            patch.object(
                job.background_job,
                "process_state",
                return_value={"state": "running", "pid": 12345, "exit_code": None},
            ),
            self.assertRaisesRegex(job.JobError, "still running"),
        ):
            job.collect(Namespace(run=run))
        self.assertTrue((self.job_root / ".lifecycle.lock").is_file())

    def test_lifecycle_lock_reuses_an_unlocked_file_after_a_crash(self) -> None:
        job.prepare_private_state()
        path = self.job_root / ".lifecycle.lock"
        path.write_text("", encoding="utf-8")
        path.chmod(0o600)
        with job.lifecycle_lock("stale"):
            pass
        with job.lifecycle_lock("stale"):
            pass

    def test_lifecycle_lock_rejects_a_fifo(self) -> None:
        job.prepare_private_state()
        path = self.job_root / ".lifecycle.lock"
        os.mkfifo(path, 0o600)
        with (
            self.assertRaisesRegex(job.JobError, "unsafe live lifecycle lock"),
            job.lifecycle_lock("unsafe"),
        ):
            pass

    def test_missing_report_failed_collection_can_still_be_removed(self) -> None:
        run = "missing-report"
        record = self.record(run)
        job.prepare_private_state()
        job.runtime_log_path(run).write_bytes(b"runner failed before report\n")
        job.runtime_log_path(run).chmod(0o600)
        completed_state = {
            "exit_code": 1,
            "finished_at": "2026-08-27T00:00:00+00:00",
            "pid": 12345,
            "state": "completed",
        }
        with (
            patch.object(job, "load_record", return_value=record),
            patch.object(job.background_job, "process_state", return_value=completed_state),
            patch.object(
                job,
                "owned_objects",
                return_value={"containers": [], "networks": []},
            ),
        ):
            self.assertEqual(job.collect(Namespace(run=run)), 1)
        status = json.loads(job.status_path(run).read_text(encoding="utf-8"))
        self.assertEqual(status["report_checks"], {})
        self.assertEqual(status["result"], "failed")
        for path in (job.record_path(run), job.completion_path(run)):
            path.write_text("{}\n", encoding="utf-8")
            path.chmod(0o600)
        with (
            patch.object(job, "load_record", return_value=record),
            patch.object(job.background_job, "process_state", return_value=completed_state),
            patch.object(job, "remove_owned_objects"),
        ):
            self.assertEqual(job.remove(Namespace(run=run)), 0)
        self.assertFalse(job.record_path(run).exists())

    def test_verify_collected_uses_the_sealed_collection_rules(self) -> None:
        run = "sealed-collection"
        record = self.record(run)
        job.prepare_private_state()
        job.runtime_log_path(run).write_bytes(b"complete log\n")
        job.runtime_log_path(run).chmod(0o600)
        self.make_report(run, record)
        completed_state = {
            "exit_code": 0,
            "finished_at": "2026-08-27T00:00:00+00:00",
            "pid": 12345,
            "state": "completed",
        }
        with (
            patch.object(job, "load_record", return_value=record),
            patch.object(job.background_job, "process_state", return_value=completed_state),
            patch.object(
                job,
                "owned_objects",
                return_value={"containers": [], "networks": []},
            ),
            patch.object(job, "current_image_validation", return_value=True),
        ):
            self.assertEqual(job.collect(Namespace(run=run)), 0)

        with patch.object(
            job,
            "report_validation",
            side_effect=AssertionError(
                "removal must not reinterpret sealed evidence with current rules"
            ),
        ) as current_validation:
            job.verify_collected(run, record)
        current_validation.assert_not_called()

        report = job.result_path(run)
        report.write_bytes(report.read_bytes() + b"\n")
        with self.assertRaisesRegex(job.JobError, "report digest"):
            job.verify_collected(run, record)

    def test_remove_deletes_only_exactly_labelled_objects(self) -> None:
        run = "exact-labels"
        record = self.record(run)
        job.prepare_private_state()
        log = b"complete log\n"
        job.log_path(run).write_bytes(log)
        job.log_path(run).chmod(0o600)
        self.make_report(run, record)
        report_result, report_sha256, report_checks = job.report_validation(run, record)
        report_checks["current_images"] = True
        self.write_private_json(
            job.status_path(run),
            {
                "background_supervisor_sha256": record[
                    "background_supervisor_sha256"
                ],
                "exit_code": 0,
                "harness_sha256": record["harness_sha256"],
                "input_provenance": record["input_provenance"],
                "job_id": JOB_ID,
                "log_sha256": hashlib.sha256(log).hexdigest(),
                "logs_ok": True,
                "owner": job.OWNER,
                "owned_objects_remaining": {"containers": [], "networks": []},
                "process_pid": 12345,
                "report": str(job.result_path(run)),
                "report_checks": report_checks,
                "report_result": report_result,
                "report_sha256": report_sha256,
                "result": "success",
                "run": run,
                "runner_sha256": record["runner_sha256"],
                "schema": 3,
                "supervisor_sha256": record["supervisor_sha256"],
                "validation_ok": True,
            },
        )
        for path in (
            job.record_path(run),
            job.runtime_log_path(run),
            job.completion_path(run),
        ):
            path.write_text("{}\n", encoding="utf-8")
            path.chmod(0o600)
        labels = {
            "io.xpra.fork-maintenance.owner": "live",
            "io.xpra.fork-maintenance.run-id": run,
        }
        ids = {
            "container": ["a" * 64],
            "network": ["b" * 64],
        }
        removals: list[list[str]] = []

        def podman_ids(kind: str, requested_run: str) -> list[str]:
            self.assertEqual(requested_run, run)
            return list(ids[kind])

        def remove_command(
            argv: list[str], *, check: bool = True, capture: bool = True
        ) -> subprocess.CompletedProcess[str]:
            self.assertTrue(check)
            self.assertTrue(capture)
            removals.append(argv)
            if argv[:3] == ["podman", "rm", "--force"]:
                ids["container"].clear()
            elif argv[:3] == ["podman", "network", "rm"]:
                ids["network"].clear()
            return completed(argv)

        with (
            patch.object(job, "load_record", return_value=record),
            patch.object(
                job.background_job,
                "process_state",
                return_value={"state": "completed", "exit_code": 0},
            ),
            patch.object(job, "podman_ids", side_effect=podman_ids),
            patch.object(
                job,
                "object_ledger_entries",
                return_value=[
                    {
                        "id": "a" * 64,
                        "kind": "container",
                        "labels": labels,
                        "name": "container-exact",
                    },
                    {
                        "id": "b" * 64,
                        "kind": "network",
                        "labels": labels,
                        "name": "network-exact",
                    },
                ],
            ),
            patch.object(
                job,
                "podman_object",
                side_effect=lambda kind, object_id: (
                    object_id,
                    "container-exact" if kind == "container" else "network-exact",
                    labels,
                ),
            ),
            patch.object(job, "command", side_effect=remove_command),
            patch.object(
                job,
                "current_image_validation",
                side_effect=AssertionError("remove must not inspect mutable image cache"),
            ) as current_images,
        ):
            self.assertEqual(job.remove(Namespace(run=run)), 0)
        current_images.assert_not_called()
        self.assertEqual(
            removals,
            [
                ["podman", "rm", "--force", "a" * 64],
                ["podman", "network", "rm", "b" * 64],
            ],
        )
        self.assertFalse(job.record_path(run).exists())
        self.assertTrue(job.status_path(run).exists())

    def test_remove_retries_after_a_crash_with_its_retained_transaction(self) -> None:
        run = "remove-crash"
        record = {
            "job_id": JOB_ID,
            "owner": job.OWNER,
            "process": {
                "completion": str(job.completion_path(run)),
                "runtime_log": str(job.runtime_log_path(run)),
            },
            "result_report": str(job.result_path(run)),
            "run": run,
            "schema": 4,
        }
        job.prepare_private_state()
        for path, payload in (
            (job.log_path(run), b"collected log\n"),
            (job.status_path(run), b"{}\n"),
            (job.record_path(run), b"{}\n"),
            (job.runtime_log_path(run), b"runtime\n"),
            (job.completion_path(run), b"{}\n"),
        ):
            path.write_bytes(payload)
            path.chmod(0o600)
        remove_objects = Mock(side_effect=[RuntimeError("simulated crash"), None])
        with (
            patch.object(job, "load_record", return_value=record),
            patch.object(job, "verify_collected"),
            patch.object(
                job.background_job,
                "process_state",
                return_value={"state": "completed", "exit_code": 0},
            ),
            patch.object(job, "remove_owned_objects", remove_objects),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                job.remove(Namespace(run=run))
            self.assertTrue(job.remove_transaction_path(run).is_file())
            self.assertTrue(job.record_path(run).is_file())
            self.assertEqual(job.remove(Namespace(run=run)), 0)
        for path in (
            job.record_path(run),
            job.runtime_log_path(run),
            job.completion_path(run),
        ):
            self.assertFalse(path.exists())
        self.assertTrue(job.remove_transaction_path(run).is_file())

    def test_removed_status_and_logs_use_only_the_retained_transaction(self) -> None:
        run = "removed-read-only"
        _record, log = self.retained_remove(run)
        retained = (
            job.log_path(run),
            job.status_path(run),
            job.remove_transaction_path(run),
        )
        before = {path: path.read_bytes() for path in retained}

        status_output = StringIO()
        with (
            patch.object(
                job,
                "load_freeze_prelaunch",
                side_effect=AssertionError("removed status must not use freeze state"),
            ),
            patch.object(
                job,
                "owned_objects",
                side_effect=AssertionError("removed status must not inspect Podman"),
            ),
            patch.object(
                job,
                "evidence_tree_validation",
                side_effect=AssertionError(
                    "removed status must not replay current report semantics"
                ),
            ),
            redirect_stdout(status_output),
        ):
            self.assertEqual(job.status(Namespace(run=run)), 0)
        self.assertEqual(
            json.loads(status_output.getvalue()),
            {"phase": "removed", "run": run},
        )

        logs_output = Mock(buffer=BytesIO())
        with (
            patch.object(
                job,
                "load_freeze_prelaunch",
                side_effect=AssertionError("removed logs must not use freeze state"),
            ),
            patch.object(
                job,
                "evidence_tree_validation",
                side_effect=AssertionError(
                    "removed logs must not replay current report semantics"
                ),
            ),
            patch.object(job.sys, "stdout", logs_output),
        ):
            self.assertEqual(job.logs(Namespace(run=run)), 0)
        self.assertEqual(logs_output.buffer.getvalue(), log)
        self.assertEqual({path: path.read_bytes() for path in retained}, before)

    def test_removed_read_routes_reject_changed_schema_and_evidence(self) -> None:
        for corruption in ("schema", "log", "status"):
            with self.subTest(corruption=corruption):
                run = f"removed-corrupt-{corruption}"
                self.retained_remove(run)
                if corruption == "schema":
                    transaction = json.loads(
                        job.remove_transaction_path(run).read_text(encoding="utf-8")
                    )
                    transaction["schema"] = 2
                    job.remove_transaction_path(run).write_text(
                        json.dumps(transaction) + "\n",
                        encoding="utf-8",
                    )
                    job.remove_transaction_path(run).chmod(0o600)
                else:
                    path = (
                        job.log_path(run)
                        if corruption == "log"
                        else job.status_path(run)
                    )
                    path.write_bytes(path.read_bytes() + b"changed\n")
                    path.chmod(0o600)

                with self.assertRaises(job.JobError):
                    job.status(Namespace(run=run))
                logs_output = Mock(buffer=BytesIO())
                with (
                    patch.object(job.sys, "stdout", logs_output),
                    self.assertRaises(job.JobError),
                ):
                    job.logs(Namespace(run=run))
                self.assertEqual(logs_output.buffer.getvalue(), b"")

    def test_status_reports_an_interrupted_retained_removal(self) -> None:
        run = "remove-interrupted-status"
        self.retained_remove(run, complete=False)
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(job.status(Namespace(run=run)), 0)
        self.assertEqual(
            json.loads(output.getvalue()),
            {"phase": "removing", "run": run},
        )

    def test_remove_rejects_mismatched_object_labels(self) -> None:
        run = "mismatch"
        labels = {
            "io.xpra.fork-maintenance.owner": "someone-else",
            "io.xpra.fork-maintenance.run-id": run,
        }
        with (
            patch.object(
                job,
                "object_ledger_entries",
                return_value=[
                    {
                        "id": "a" * 64,
                        "kind": "container",
                        "labels": {
                            "io.xpra.fork-maintenance.owner": "live",
                            "io.xpra.fork-maintenance.run-id": run,
                        },
                        "name": "container",
                    }
                ],
            ),
            patch.object(
                job,
                "podman_ids",
                side_effect=lambda kind, _run: ["a" * 64] if kind == "container" else [],
            ),
            patch.object(
                job,
                "podman_object",
                return_value=("a" * 64, "container", labels),
            ),
            self.assertRaisesRegex(job.JobError, "no longer matches"),
        ):
            job.remove_owned_objects(run)

    def test_abort_terminates_process_and_removes_partial_result(self) -> None:
        run = "abort"
        record = self.record(run)
        job.prepare_private_state()
        for path in (
            job.record_path(run),
            job.runtime_log_path(run),
            job.completion_path(run),
        ):
            path.write_text("{}\n", encoding="utf-8")
            path.chmod(0o600)
        result_directory = job.result_path(run).parent
        result_directory.mkdir(parents=True)
        result_directory.chmod(0o700)
        (result_directory / "partial").write_text("partial", encoding="utf-8")
        with (
            patch.object(job, "load_record", return_value=record),
            patch.object(
                job.background_job,
                "process_state",
                return_value={"state": "running", "pid": 12345},
            ),
            patch.object(job.background_job, "terminate") as terminate,
            patch.object(job, "remove_owned_objects") as remove_objects,
        ):
            self.assertEqual(job.abort(Namespace(run=run)), 0)
        terminate.assert_called_once_with(record, require_current=False)
        remove_objects.assert_called_once_with(run)
        self.assertFalse(job.record_path(run).exists())
        self.assertFalse(result_directory.exists())

    def test_abort_refuses_current_completed_but_discards_stale_completed(self) -> None:
        run = "completed-abort"
        record = self.record(run)
        job.prepare_private_state()
        for path in (
            job.record_path(run),
            job.runtime_log_path(run),
            job.completion_path(run),
        ):
            path.write_text("{}\n", encoding="utf-8")
            path.chmod(0o600)
        with (
            patch.object(job, "load_record", return_value=record),
            patch.object(
                job.background_job,
                "process_state",
                return_value={"state": "completed", "pid": 12345},
            ),
            patch.object(job, "remove_owned_objects") as remove_objects,
            self.assertRaisesRegex(job.JobError, "must be collected"),
        ):
            job.abort(Namespace(run=run))
        remove_objects.assert_not_called()

        stale = dict(record)
        stale["harness_sha256"] = "0" * 64
        with (
            patch.object(job, "load_record", return_value=stale),
            patch.object(
                job.background_job,
                "process_state",
                return_value={"state": "completed", "pid": 12345},
            ),
            patch.object(job, "remove_owned_objects") as remove_objects,
        ):
            self.assertEqual(job.abort(Namespace(run=run)), 0)
        remove_objects.assert_called_once_with(run)
        self.assertFalse(job.record_path(run).exists())

    def test_private_state_tightens_owned_directories(self) -> None:
        self.artifact_root.mkdir(mode=0o755)
        self.state_root.mkdir(mode=0o775)
        job.prepare_private_state()
        for path in (
            self.state_root,
            self.state_root / "jobs",
            self.job_root,
            self.result_root,
            self.venv_root,
        ):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.artifact_root.stat().st_mode & 0o777, 0o755)

    def test_environment_path_bootstraps_without_site_packages(self) -> None:
        result = subprocess.run(
            [sys.executable, "-S", str(job.SUPERVISOR), "environment-path"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout.strip(), r"/venvs/live-[0-9a-f]{16}$")

    def test_environment_partial_recovery_requires_its_exact_marker(self) -> None:
        self.venv_root.mkdir(mode=0o700)
        partial = job.environment_partial_path()
        partial.mkdir(mode=0o700)
        (partial / "stale").write_text("stale\n", encoding="utf-8")
        self.write_private_json(
            job.environment_partial_marker_path(),
            {
                "schema": 1,
                "owner": job.OWNER,
                "kind": "live-environment-partial",
                "partial": str(partial),
                "destination": str(self.venv_root / ("live-" + "1" * 16)),
                "requirements_sha256": "2" * 64,
                "python_version": "old interpreter",
            },
        )
        job.recover_environment_partial()
        self.assertFalse(partial.exists())
        self.assertFalse(job.environment_partial_marker_path().exists())

        partial.mkdir(mode=0o700)
        self.write_private_json(job.environment_partial_marker_path(), {})
        with self.assertRaisesRegex(job.JobError, "owner mismatch"):
            job.recover_environment_partial()
        self.assertTrue(partial.is_dir())
        self.assertTrue(job.environment_partial_marker_path().is_file())

    def test_environment_child_inherits_the_recovery_lock(self) -> None:
        self.venv_root.mkdir(mode=0o700)
        read_gate, write_gate = os.pipe()
        child: subprocess.Popen[bytes] | None = None
        competitor = -1
        try:
            with job.environment_lock() as lock_descriptor:
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        "import os,sys; os.read(int(sys.argv[1]), 1)",
                        str(read_gate),
                    ],
                    pass_fds=(lock_descriptor, read_gate),
                )
                os.close(read_gate)
                read_gate = -1
            competitor = os.open(self.venv_root / ".environment.lock", os.O_RDWR)
            with self.assertRaises(BlockingIOError):
                fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.write(write_gate, b"x")
            os.close(write_gate)
            write_gate = -1
            child.wait(timeout=5)
            fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            if read_gate >= 0:
                os.close(read_gate)
            if write_gate >= 0:
                os.close(write_gate)
            if child is not None and child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            if competitor >= 0:
                os.close(competitor)

    def test_private_state_rejects_symlink(self) -> None:
        target = self.root / "untrusted-target"
        target.mkdir()
        self.artifact_root.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(job.JobError, "symlink"):
            job.prepare_private_state()
        self.assertEqual(list(target.iterdir()), [])

    def test_private_state_rejects_writable_parent(self) -> None:
        self.artifact_root.mkdir(mode=0o700)
        self.artifact_root.chmod(0o775)
        with self.assertRaisesRegex(job.JobError, "writable by another user"):
            job.prepare_private_state()
        self.assertFalse(self.state_root.exists())

    def test_network_labels_accept_lowercase_podman_field(self) -> None:
        labels = {
            "io.xpra.fork-maintenance.owner": "live",
            "io.xpra.fork-maintenance.run-id": "lowercase-labels",
        }
        argv = ["podman", "network", "inspect", "network-id"]
        with patch.object(
            job,
            "command",
            return_value=completed(
                argv,
                json.dumps([{"id": "a" * 64, "name": "network", "labels": labels}]),
            ),
        ):
            self.assertEqual(job.podman_labels("network", "network-id"), labels)

    def test_network_labels_reject_conflicting_podman_fields(self) -> None:
        argv = ["podman", "network", "inspect", "network-id"]
        payload = [
            {
                "Labels": {"io.xpra.fork-maintenance.owner": "live"},
                "labels": {"io.xpra.fork-maintenance.owner": "someone-else"},
            }
        ]
        with (
            patch.object(
                job,
                "command",
                return_value=completed(argv, json.dumps(payload)),
            ),
            self.assertRaisesRegex(job.JobError, "conflicting labels"),
        ):
            job.podman_labels("network", "network-id")

class LiveRunnerCleanupTest(unittest.TestCase):
    @staticmethod
    def primary_native_capture_rows(*, legacy: bool = False) -> list[str]:
        prefix = "2026-09-05 11:27:47,912 Surface(1 : empty project)"
        if legacy:
            return [
                prefix + ".capture_pixels: dmabuf 1536x1095 format=0x34325258 modifier=0x20000000056bb03 planes=2",
                prefix + ".capture_pixels: 0,0 1536x1095 (6727680 bytes)",
                prefix + "._emit(surface-image, (1, DMABufImageWrapper(0x34325258:(0, 0, 1536, 1095, 32):(6144, 1536):0))) callbacks=[]",
                "commit wid 1 mapped=True, size=(1536, 1095), rects=[(0, 0, 1536, 1095)], subsurfaces=[]",
            ]
        return [
            prefix + "._emit(surface-snapshot, (1, None)) callbacks=[]",
            prefix + ".capture_pixels: 0,0 1536x1095 (6727680 bytes)",
            prefix + "._emit(surface-snapshot, (1, ImageWrapper(RGBX:(0, 0, 1536, 1095, 32):PACKED))) callbacks=[]",
            "commit wid 1 mapped=True, size=(1536, 1095), rects=((0, 0, 1536, 1095),), subsurfaces=((1, 0, 0, 1536, 1095, 1536, 1095),)",
        ]

    @staticmethod
    def primary_native_capture_packets() -> dict[str, object]:
        return {
            "window_id": 1,
            "initial_pixel_format": "RGBX",
            "updates": [{
                "encoding": "rgb24", "x": 0, "y": 0, "w": 1536, "h": 1095,
                "payload_bytes": 6727680, "relative_info": "screen-updates/1/1/0.info",
                "options": {"window-size": [1536, 1095], "rgb_format": "RGBX"},
            }],
        }

    def test_primary_native_capture_accepts_published_legacy_and_normalized_routes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            for legacy in (False, True):
                with self.subTest(legacy=legacy):
                    (directory / "server.stderr").write_text(
                        "\n".join(self.primary_native_capture_rows(legacy=legacy)) + "\n",
                        encoding="utf-8",
                    )
                    checks = live_run.wayland_capture_checks(
                        live_run.inspect_logs(directory), self.primary_native_capture_packets(),
                    )
                    self.assertTrue(all(checks.values()), checks)

    def test_primary_native_capture_rejects_unowned_incomplete_or_invalid_route(self) -> None:
        base = self.primary_native_capture_rows(legacy=True)
        candidates = {
            "wrong-window": [line.replace("Surface(1 :", "Surface(9 :").replace("(1, DMABuf", "(9, DMABuf").replace("commit wid 1 ", "commit wid 9 ") for line in base],
            "irrelevant-probe-size": [line.replace("1536x1095", "64x64").replace("6727680", "16384").replace("1536, 1095", "64, 64").replace("(6144, 1536)", "(256, 64)") for line in base],
            "bad-bytecount": [line.replace("6727680 bytes", "6727679 bytes") for line in base],
            "unmapped": [line.replace("mapped=True", "mapped=False") for line in base],
            "wrong-depth": [line.replace("1095, 32)", "1095, 24)") for line in base],
            "nonzero-origin": [line.replace(":(0, 0, 1536", ":(1, 0, 1536") for line in base],
            "publication-before-read": [base[0], base[2], base[1], base[3]],
            "missing-read": [base[0], base[2], base[3]],
            "missing-native-dmabuf": base[1:],
            "missing-publication": [base[0], base[1], base[3]],
            "mismatched-published-id": [line.replace("(1, DMABuf", "(2, DMABuf") for line in base],
            "mismatched-published-fourcc": [base[0], base[1], base[2].replace("0x34325258", "0x34324258"), base[3]],
            "mismatched-native-planes": [base[0].replace("planes=2", "planes=1"), *base[1:]],
            "fds-not-released": [line.replace(":0))) callbacks=", ":2))) callbacks=") for line in base],
        }
        normalized = self.primary_native_capture_rows()
        candidates.update({
            "normalized-alpha-format": [line.replace("ImageWrapper(RGBX:", "ImageWrapper(RGBA:") for line in normalized],
            "normalized-bad-bytecount": [line.replace("6727680 bytes", "1 bytes") for line in normalized],
            "normalized-invalidation-after-read": [*normalized[:2], normalized[0], *normalized[2:]],
            "normalized-failed-read": [*normalized[:2], "Error: failed to read texture pixels for Surface(1 : empty project)", *normalized[2:]],
            "normalized-failed-publication": [*normalized[:3], "Error capturing logical root pixels for Surface(1 : empty project)", normalized[3]],
            "normalized-failed-model-copy": [*normalized[:3], "Error replacing Wayland root snapshot 0x1", normalized[3]],
            "normalized-unknown-model": [*normalized[:3], "surface-snapshot: unknown toplevel wid=0x1, dropping", normalized[3]],
            "legacy-unknown-model": [*base[:3], "Warning: cannot update window 1: not found!", base[3]],
            "normalized-role-unmapped": [*normalized[:2], "Surface(1 : empty project)._emit(unmap, (1,)) callbacks=[]", *normalized[2:]],
            "normalized-wrong-canvas": [*normalized[:3], normalized[3].replace("size=(1536, 1095)", "size=(1537, 1095)")],
            "normalized-publication-before-read": [normalized[0], normalized[2], normalized[1], normalized[3]],
            "normalized-missing-publication": [*normalized[:2], normalized[3]],
            "fake-legacy-without-native-metadata": [base[1], base[2], base[3]],
        })
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            for name, lines in candidates.items():
                with self.subTest(name=name):
                    (directory / "server.stderr").write_text("\n".join(lines) + "\n", encoding="utf-8")
                    checks = live_run.wayland_capture_checks(
                        live_run.inspect_logs(directory), self.primary_native_capture_packets(),
                    )
                    self.assertFalse(all(checks.values()), checks)

    def test_native_capture_keeps_raw_logical_and_h264_crop_geometry_separate(self) -> None:
        rows = self.primary_native_capture_rows()
        rows[1] = rows[1].replace("1536x1095", "3072x2190").replace("6727680", "26910720")
        packets = self.primary_native_capture_packets()
        packets["initial_pixel_format"] = "BGRA"  # Adaptive Zed may begin alpha-bearing.
        packet = packets["updates"][0]
        packet["encoding"] = "h264"
        packet["w"] = 1535  # The existing H.264 edge contract owns the missing pixel.
        del packet["options"]["rgb_format"]
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            (directory / "server.stderr").write_text("\n".join(rows) + "\n", encoding="utf-8")
            logs = live_run.inspect_logs(directory)
            checks = live_run.wayland_capture_checks(logs, packets)
            self.assertTrue(all(checks.values()), checks)
            record, = logs["native_wayland_captures"]
            self.assertEqual(record["kind"], "normalized-texture")
            self.assertEqual(record["read_size"], [3072, 2190])
            self.assertEqual(record["logical_size"], [1536, 1095])
            self.assertEqual(record["read_bytes"], 26910720)
            self.assertEqual(record["pixel_format"], "RGBX")
            self.assertNotIn("native_fourcc", record)
            packet["relative_info"] = "screen-updates/10/1/0.info"
            self.assertFalse(all(live_run.wayland_capture_checks(logs, packets).values()))

    def test_native_capture_failure_resets_only_owner_and_allows_fresh_capture(self) -> None:
        rows = self.primary_native_capture_rows()
        failures = (
            "Error capturing logical root pixels for Surface(1 : empty project)",
            "Error replacing Wayland root snapshot 0x1",
            "surface-snapshot: unknown toplevel wid=0x1, dropping",
            "Surface(1 : empty project)._emit(unmap, (1,)) callbacks=[]",
            "Surface(1 : empty project)._emit(destroy, (1,)) callbacks=[]",
        )
        for failure in failures:
            with self.subTest(failure=failure):
                failed = [*rows[:3], failure, rows[3]]
                self.assertEqual(live_run.parse_wayland_native_captures("\n".join(failed)), [])
                recovered = live_run.parse_wayland_native_captures("\n".join([*failed, *rows]))
                self.assertEqual(len(recovered), 1)
                self.assertGreater(recovered[0]["read_line"], len(failed))
                unrelated = failure.replace("Surface(1 :", "Surface(9 :").replace("0x1", "0x9")
                other_window = live_run.parse_wayland_native_captures("\n".join([*rows[:3], unrelated, rows[3]]))
                self.assertEqual(len(other_window), 1)

    def test_wayland_commit_counters_accept_both_real_sequence_representations(self) -> None:
        lines = (
            (
                "2026-09-05 10:57:01,799 commit wid 2 mapped=True, size=(360, 240), "
                "rects=(), subsurfaces=((2, 0, 0, 360, 240, 360, 240),)"
            ),
            (
                "2026-09-05 10:57:01,799 commit wid 3 mapped=True, size=(260, 160), "
                "rects=(), subsurfaces=((3, 0, 0, 260, 160, 260, 160),)"
            ),
            "commit wid 2 mapped=True, size=(360, 240), rects=[], subsurfaces=[]",
            "commit wid 2 mapped=True, size=(360, 240), rects=[(0, 0, 360, 240)]",
            "commit wid 3 mapped=True, size=(260, 160), rects=((0, 0, 260, 160),)",
        )
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            log = directory / "server.stderr"
            log.write_text("\n".join(lines), encoding="utf-8")
            observed = live_run.inspect_logs(directory)
            self.assertEqual(observed["empty_wayland_commits"], 3)
            self.assertEqual(observed["nonempty_wayland_commits"], 2)
            for value in ("unknown", "[]junk", "()junk", "[(0, 0, 0, 1)]", "[False]"):
                with self.subTest(value=value):
                    log.write_text(f"commit wid 2 mapped=True, rects={value}\n")
                    observed = live_run.inspect_logs(directory)
                    self.assertEqual(observed["empty_wayland_commits"], 0)
                    self.assertEqual(observed["nonempty_wayland_commits"], 0)

    def test_mapped_empty_commit_uses_actual_bounded_suffix_probe(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            log = directory / "server.stderr"

            def execute(_container: str, command: list[str], **_kwargs: object) -> object:
                self.assertEqual(command[:2], ["python3", "-c"])
                arguments = ["-c", str(log), *command[4:]]
                with patch.object(sys, "argv", arguments):
                    try:
                        exec(command[2], {"__name__": "__main__"})  # noqa: S102
                    except SystemExit as error:
                        return completed(command, returncode=error.code)
                self.fail("suffix probe did not publish its result")

            prefix = "old commit wid 2 mapped=True, rects=[]\n"
            rows = (
                ("commit wid 2 mapped=True, size=(360, 240), rects=[], subsurfaces=[]", True),
                ("commit wid 2 mapped=True, size=(360, 240), rects=(), subsurfaces=((2, 0, 0, 360, 240, 360, 240),)", True),
                ("commit wid 2 mapped=True, rects=()", True),
                ("commit wid 20 mapped=True, rects=[]", False),
                ("commit wid 3 mapped=True, rects=[]", False),
                ("commit wid 2 mapped=False, rects=[]", False),
                ("commit wid 2 mapped=True, rects=[(0, 0, 1, 1)]", False),
                ("commit wid 2 mapped=True, rects=((0, 0, 1, 1),)", False),
                ("commit wid 2 mapped=True, rects=unknown", False),
                ("commit wid 2 mapped=True, rects=[]junk", False),
                ("commit wid 2 mapped=True, rects=()junk", False),
                ("commit wid 2 mapped=True, other_rects=[]", False),
                ("commit wid 2 mapped=True, rects=\n[]", False),
                ("recommit wid 2 mapped=True, rects=[]", False),
                ("no new matching commit", False),
            )
            with patch.object(live_run, "podman_exec", side_effect=execute):
                for row, expected in rows:
                    with self.subTest(row=row):
                        log.write_text(prefix + row + "\n", encoding="utf-8")
                        observed = live_run.container_artifact_suffix_matches(
                            "server", "server.stderr", len(prefix.encode()),
                            (live_run.mapped_empty_wayland_commit_pattern(2),),
                        )
                        self.assertEqual(observed, expected)

    def test_frame_poll_requires_valid_nonempty_damage_for_exact_window(self) -> None:
        class ProbeFinished(Exception):
            pass

        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            screenshot = directory / "screen-updates" / "1" / "1" / "screenshot.png"
            screenshot.parent.mkdir(parents=True)
            screenshot.write_bytes(b"png")
            for wid, value, expected in (
                (1, "[(0, 0, 64, 64)]", True),
                (1, "((0, 0, 64, 64),)", True),
                (1, "[(0, 0, 32, 32), (32, 32, 32, 32)]", True),
                (1, "((0, 0, 32, 32), (32, 32, 32, 32))", True),
                (1, "[]", False),
                (1, "()", False),
                (1, "unknown", False),
                (1, "[(0, 0, 0, 64)]", False),
                (1, "((0, 0, 64, 64),)junk", False),
                (10, "[(0, 0, 64, 64)]", False),
            ):
                with self.subTest(wid=wid, value=value):
                    server_log = f"commit wid {wid} rects={value}\nrgb_encode using RGBX\n"
                    client_log = (
                        "cairo._do_paint_rgb\nrecord_decode_time(True,\n"
                        "draw_widget(\ncairo_draw: window size=\n"
                    )

                    def deltas(
                        container: str, offsets: dict[str, int],
                        server_log: str = server_log, client_log: str = client_log,
                    ) -> dict[str, tuple[int, str]]:
                        values = {
                            "server.stderr": server_log if container == "server" else "",
                            "client.stdout": client_log if container == "client" else "",
                            "client.stderr": "",
                        }
                        return {
                            name: (offset + len(values[name].encode()), values[name])
                            for name, offset in offsets.items()
                        }

                    def wait_once(
                        _description: str, predicate: object,
                        expected: bool = expected, **_kwargs: object,
                    ) -> None:
                        self.assertEqual(bool(predicate()), expected)  # type: ignore[operator]
                        raise ProbeFinished

                    with (
                        patch.object(live_run, "read_container_log_deltas", side_effect=deltas),
                        patch.object(live_run, "container_artifact_files", return_value=()),
                        patch.object(live_run, "analyze_png", return_value={"quantized_rgb_colors": 64}),
                        patch.object(live_run, "wait_for", side_effect=wait_once),
                        patch.object(live_run, "container_process_exists", return_value=True),
                        self.assertRaises(ProbeFinished),
                    ):
                        live_run.wait_for_frame_boundary(
                            "server", 101, "client", 202, directory,
                            "rgb", "strict-hardware", application="zed", expected_xpra_wid=1,
                        )

    def test_image_inspection_accepts_only_exact_maintenance_provenance(self) -> None:
        expected = {
            "io.xpra.fork-maintenance.context": "1" * 64,
            "io.xpra.fork-maintenance.owner": "live",
            "io.xpra.fork-maintenance.role": "server-image",
            "io.xpra.fork-maintenance.source": "2" * 40,
        }
        inspection = {
            "Id": "sha256:" + "3" * 64,
            "Labels": {
                **expected,
                "io.buildah.version": "1.42.1",
                "org.opencontainers.image.title": "ubuntu",
            },
        }
        with patch.object(
            live_run,
            "run",
            return_value=completed(
                ["podman", "image", "inspect"], json.dumps([inspection])
            ),
        ):
            observed = live_run.inspect_maintenance_image(
                "image",
                role="server-image",
                source_commit="2" * 40,
                context_digest="1" * 64,
            )
        self.assertEqual(observed["labels"], expected)

        inspection["Labels"]["io.xpra.fork-maintenance.unexpected"] = "value"
        with (
            patch.object(
                live_run,
                "run",
                return_value=completed(
                    ["podman", "image", "inspect"], json.dumps([inspection])
                ),
            ),
            self.assertRaisesRegex(live_run.LabFailure, "provenance labels"),
        ):
            live_run.inspect_maintenance_image(
                "image",
                role="server-image",
                source_commit="2" * 40,
                context_digest="1" * 64,
            )

    def test_frame_poll_reads_bounded_deltas_without_repulling_full_logs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            screenshot = directory / "screen-updates" / "1" / "1" / "screenshot.png"
            screenshot.parent.mkdir(parents=True)
            screenshot.write_bytes(b"png")
            server_log = "commit wid 1 rects=[(0, 0, 1, 1)]\nrgb_encode using RGBX\n"
            client_log = (
                "cairo._do_paint_rgb\nrecord_decode_time(True,\n"
                "draw_widget(\ncairo_draw: window size=\n"
            )

            def deltas(container: str, offsets: dict[str, int]) -> dict[str, tuple[int, str]]:
                values = {
                    "server.stderr": server_log if container == "server" else "",
                    "client.stdout": client_log if container == "client" else "",
                    "client.stderr": "",
                }
                return {
                    name: (offset + len(values[name].encode()), values[name])
                    for name, offset in offsets.items()
                }

            def wait_once(_description: str, predicate: object, **_kwargs: object) -> None:
                self.assertTrue(predicate())  # type: ignore[operator]

            with (
                patch.object(live_run, "read_container_log_deltas", side_effect=deltas) as read,
                patch.object(live_run, "container_artifact_files", return_value=()),
                patch.object(
                    live_run,
                    "analyze_png",
                    return_value={"quantized_rgb_colors": 64},
                ),
                patch.object(live_run, "wait_for", side_effect=wait_once),
                patch.object(live_run, "container_process_exists", return_value=True),
                patch.object(live_run, "pull_container_artifacts") as pull,
            ):
                outcome = live_run.wait_for_frame_boundary(
                    "server",
                    101,
                    "client",
                    202,
                    directory,
                    "rgb",
                    "strict-hardware",
                    application="zed",
                    expected_xpra_wid=1,
                )
            self.assertEqual(outcome, "success")
            self.assertEqual(read.call_count, 2)
            pull.assert_not_called()

    def test_subsurface_frame_poll_uses_raw_packet_not_async_screenshot(self) -> None:
        width, height = live_run.SUBSURFACE_PARENT_DIMENSIONS["primary"]
        server_log = (
            f"commit wid 1 rects=[(0, 0, {width}, {height})]\n"
            "rgb_encode using BGRX\n"
        )
        client_log = (
            "cairo._do_paint_rgb\nrecord_decode_time(True,\n"
            "draw_widget(\ncairo_draw: window size=\n"
        )

        def deltas(container: str, offsets: dict[str, int]) -> dict[str, tuple[int, str]]:
            values = {
                "server.stderr": server_log if container == "server" else "",
                "client.stdout": client_log if container == "client" else "",
                "client.stderr": "",
            }
            return {
                name: (offset + len(values[name].encode()), values[name])
                for name, offset in offsets.items()
            }

        def wait_once(_description: str, predicate: object, **_kwargs: object) -> None:
            self.assertTrue(predicate())  # type: ignore[operator]

        with (
            tempfile.TemporaryDirectory() as raw,
            patch.object(live_run, "read_container_log_deltas", side_effect=deltas),
            patch.object(
                live_run,
                "subsurface_startup_packet_ready",
                return_value=True,
            ) as packet_ready,
            patch.object(
                live_run,
                "container_artifact_files",
                side_effect=AssertionError("async screenshots are not WSSO authority"),
            ),
            patch.object(
                live_run,
                "analyze_png",
                side_effect=AssertionError("async screenshots are not WSSO authority"),
            ),
            patch.object(live_run, "wait_for", side_effect=wait_once),
            patch.object(live_run, "container_process_exists", return_value=True),
        ):
            outcome = live_run.wait_for_frame_boundary(
                "server",
                101,
                "client",
                202,
                Path(raw),
                "rgb",
                "strict",
                application="subsurface",
                expected_xpra_wid=1,
            )
        self.assertEqual(outcome, "success")
        packet_ready.assert_called_once_with("server", Path(raw), 1)

    def test_incremental_log_probe_caps_reads_at_its_fd_size_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "server.stderr"
            source.write_bytes(b"before\n")
            original_fstat = os.fstat
            first_fstat = True

            def grow_after_snapshot(descriptor: int) -> os.stat_result:
                nonlocal first_fstat
                details = original_fstat(descriptor)
                if first_fstat:
                    first_fstat = False
                    with source.open("ab") as stream:
                        stream.write(b"after\n")
                return details

            with (
                patch.object(live_run.os, "fstat", side_effect=grow_after_snapshot),
                patch.object(
                    live_run,
                    "podman_exec",
                    side_effect=execute_container_log_probe(root),
                ),
            ):
                result = live_run.read_container_log_deltas(
                    "server",
                    {"server.stderr": 0},
                    markers={"server.stderr": ("before",)},
                )
            grown = source.read_bytes()

        self.assertEqual(result, {"server.stderr": (7, "before\n")})
        self.assertEqual(grown, b"before\nafter\n")

    def test_incremental_log_probe_rejects_unsafe_or_truncated_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "target"
            target.write_text("value\n", encoding="utf-8")
            (root / "symlink.stderr").symlink_to(target.name)
            os.mkfifo(root / "fifo.stderr", mode=0o600)
            truncated = root / "truncated.stderr"
            truncated.write_bytes(b"abc")

            execute = execute_container_log_probe(root)
            for name, offset, error in (
                ("symlink.stderr", 0, "unsafe"),
                ("fifo.stderr", 0, "unsafe"),
                ("truncated.stderr", 4, "truncated"),
            ):
                with (
                    self.subTest(name=name),
                    patch.object(live_run, "podman_exec", side_effect=execute),
                    self.assertRaisesRegex(live_run.LabFailure, error),
                ):
                    live_run.read_container_log_deltas(
                        "server",
                        {name: offset},
                        markers={name: ("value",)},
                    )

    def test_incremental_log_probe_skips_large_unmatched_debug_output(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "server.stderr"
            noise = b"unmatched debug output\n" * 400_000
            marker = b"commit wid 1 rects=[(0, 0, 1, 1)]\n"
            source.write_bytes(noise + marker)
            with patch.object(
                live_run,
                "podman_exec",
                side_effect=execute_container_log_probe(root),
            ):
                result = live_run.read_container_log_deltas(
                    "server",
                    {"server.stderr": 0},
                )

        self.assertGreater(len(noise), live_run.FRAME_LOG_TOTAL_BYTES)
        self.assertEqual(
            result,
            {"server.stderr": (len(noise) + len(marker), marker.decode())},
        )

    def test_live_artifact_pull_keeps_strict_archive_validation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            destination = Path(raw)
            with patch.object(
                live_run.container_payload,
                "merge_from_process",
            ) as merge:
                live_run.pull_container_artifacts(
                    "server",
                    destination,
                    ("server.stderr",),
                )
        command = merge.call_args.args[0]
        self.assertEqual(
            command[:6],
            [
                "podman",
                "exec",
                "server",
                "python3",
                live_run.CONTAINER_PAYLOAD,
                "create",
            ],
        )

    def test_container_artifact_size_reads_only_exact_remote_metadata(self) -> None:
        with patch.object(
            live_run,
            "podman_exec",
            return_value=completed(["stat"], "123\n"),
        ) as execute:
            self.assertEqual(
                live_run.container_artifact_size("server", "server.stderr"),
                123,
            )
        execute.assert_called_once_with(
            "server",
            [
                "python3",
                "-c",
                ANY,
                "/artifacts/server.stderr",
            ],
            check=False,
            announce=False,
        )
        probe = execute.call_args.args[1][2]
        self.assertIn("os.lstat", probe)
        self.assertIn("stat.S_ISREG", probe)

        with (
            patch.object(
                live_run,
                "podman_exec",
                return_value=completed(["python3"], returncode=2),
            ),
            self.assertRaisesRegex(live_run.LabFailure, "not a regular file"),
        ):
            live_run.container_artifact_size("server", "server.stderr")

    def test_container_artifact_suffix_query_is_bounded(self) -> None:
        with patch.object(
            live_run,
            "podman_exec",
            return_value=completed(["python3"], returncode=2),
        ) as execute, self.assertRaisesRegex(live_run.LabFailure, "exceeds its limit"):
            live_run.container_artifact_suffix_matches(
                "server",
                "server.stderr",
                42,
                ("marker",),
            )
        command = execute.call_args.args[1]
        self.assertEqual(command[-3:], ["42", str(live_run.FRAME_LOG_TOTAL_BYTES), "marker"])

    def test_wait_for_log_does_not_pull_a_log_while_its_writer_is_active(self) -> None:
        def wait_once(_description: str, predicate: object, **_kwargs: object) -> None:
            self.assertTrue(predicate())  # type: ignore[operator]

        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "server.stderr"
            with (
                patch.object(live_run, "container_artifact_contains", return_value=True),
                patch.object(live_run, "container_process_exists") as process_exists,
                patch.object(live_run, "pull_container_artifacts") as pull,
                patch.object(live_run, "wait_for", side_effect=wait_once),
            ):
                live_run.wait_for_log(
                    "server",
                    101,
                    path,
                    "ready",
                    "server readiness",
                )
        process_exists.assert_not_called()
        pull.assert_not_called()

    def test_server_tcp_endpoint_retries_from_the_client_until_reachable(self) -> None:
        probes = [
            completed(["python3"], returncode=75),
            completed(["python3"]),
        ]

        def wait_twice(_description: str, predicate: object, **_kwargs: object) -> None:
            self.assertFalse(predicate())  # type: ignore[operator]
            self.assertTrue(predicate())  # type: ignore[operator]

        with tempfile.TemporaryDirectory() as raw:
            server_log = Path(raw) / "server.stderr"
            with (
                patch.object(live_run, "container_process_exists", return_value=True) as alive,
                patch.object(live_run, "podman_exec", side_effect=probes) as execute,
                patch.object(live_run, "pull_container_artifacts") as pull,
                patch.object(live_run, "wait_for", side_effect=wait_twice),
            ):
                live_run.wait_for_server_tcp_endpoint(
                    "server",
                    101,
                    "client",
                    "xpra-server",
                    14500,
                    server_log,
                )

        self.assertEqual(alive.call_count, 4)
        self.assertEqual(execute.call_count, 2)
        for call in execute.call_args_list:
            self.assertEqual(call.args[0], "client")
            self.assertEqual(call.args[1][-2:], ["xpra-server", "14500"])
            self.assertEqual(call.kwargs, {"announce": False, "check": False})
        pull.assert_not_called()

    def test_server_tcp_endpoint_collects_complete_server_evidence_after_exit(self) -> None:
        events: list[str] = []

        def stopped(_container: str, _pid: int) -> bool:
            events.append("server-exited")
            return False

        def pull_server(
            container: str,
            destination: Path,
            role: str,
        ) -> None:
            events.append("pull-server")
            self.assertEqual(container, "server")
            self.assertEqual(role, "server")
            (destination / "server.stderr").write_text(
                "server failed\n",
                encoding="utf-8",
            )
            (destination / "interaction.stderr").write_text(
                "interaction failed\n",
                encoding="utf-8",
            )

        def wait_once(_description: str, predicate: object, **_kwargs: object) -> None:
            predicate()  # type: ignore[operator]

        with tempfile.TemporaryDirectory() as raw:
            server_log = Path(raw) / "server.stderr"
            with (
                patch.object(live_run, "container_process_exists", side_effect=stopped),
                patch.object(live_run, "pull_all_container_artifacts", side_effect=pull_server),
                patch.object(live_run, "podman_exec") as execute,
                patch.object(live_run, "wait_for", side_effect=wait_once),
                self.assertRaisesRegex(
                    live_run.LabFailure,
                    "server failed",
                ),
            ):
                live_run.wait_for_server_tcp_endpoint(
                    "server",
                    101,
                    "client",
                    "xpra-server",
                    14500,
                    server_log,
                )

        self.assertEqual(events, ["server-exited", "pull-server"])
        execute.assert_not_called()

    def test_failure_quiescence_stops_only_exact_workload_pids(self) -> None:
        running = {("client", 202), ("server", 101)}
        terminations: list[tuple[str, int]] = []

        def process_exists(container: str, pid: int) -> bool:
            return (container, pid) in running

        def execute(
            container: str,
            command: list[str],
            **kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            self.assertEqual(kwargs, {"announce": False, "check": False})
            self.assertEqual(command[:2], ["kill", "-TERM"])
            pid = int(command[2])
            terminations.append((container, pid))
            running.remove((container, pid))
            return completed(command)

        with (
            patch.object(live_run, "container_process_exists", side_effect=process_exists),
            patch.object(live_run, "podman_exec", side_effect=execute),
        ):
            result = live_run.quiesce_failed_workloads(
                (
                    ("client", "client", 202),
                    ("server", "server", 101),
                    ("unused", "unused", 0),
                )
            )

        self.assertTrue(result["passed"])
        self.assertEqual(terminations, [("client", 202), ("server", 101)])
        self.assertEqual(
            [item["status"] for item in result["processes"]],
            ["exited", "exited", "not-started"],
        )

    def test_failure_quiescence_refuses_collection_while_a_pid_remains(self) -> None:
        with (
            patch.object(live_run, "container_process_exists", return_value=True),
            patch.object(
                live_run,
                "podman_exec",
                return_value=completed(["kill", "-TERM", "101"]),
            ),
        ):
            result = live_run.quiesce_failed_workloads(
                (("server", "server", 101),),
                timeout=0,
            )

        self.assertFalse(result["passed"])
        self.assertEqual(result["processes"][0]["status"], "still-running")

    def test_hardware_fixture_readiness_retries_until_both_children_are_ready(self) -> None:
        probes = [
            completed(["python3"], returncode=75),
            completed(["python3"]),
        ]

        def wait_twice(_description: str, predicate: object, **_kwargs: object) -> None:
            self.assertFalse(predicate())  # type: ignore[operator]
            self.assertTrue(predicate())  # type: ignore[operator]

        with (
            tempfile.TemporaryDirectory() as raw,
            patch.object(
                live_run,
                "container_process_exists",
                return_value=True,
            ) as alive,
            patch.object(live_run, "podman_exec", side_effect=probes) as execute,
            patch.object(live_run, "pull_all_container_artifacts") as pull,
            patch.object(live_run, "wait_for", side_effect=wait_twice),
        ):
            live_run.wait_for_hardware_fixture("server", 101, Path(raw), "hardware")

        self.assertEqual(alive.call_count, 4)
        self.assertEqual(execute.call_count, 2)
        for call in execute.call_args_list:
            self.assertEqual(call.args[0], "server")
            self.assertEqual(call.args[1][:2], ["python3", "-c"])
            self.assertEqual(call.args[1][-2:], ["vkcube", live_run.INTERACTION_FIXTURE_SCRIPT])
            self.assertEqual(call.kwargs, {"announce": False, "check": False})
        probe = execute.call_args.args[1][2]
        self.assertIn("interaction.identity.json", probe)
        self.assertIn("states = (interaction_state(expected_script)", probe)
        self.assertNotIn("child_state(name) for name in ('interaction'", probe)
        pull.assert_not_called()

    def test_opengl_fixture_readiness_binds_the_glmark2_primary(self) -> None:
        with (
            tempfile.TemporaryDirectory() as raw,
            patch.object(live_run, "container_process_exists", return_value=True),
            patch.object(
                live_run,
                "podman_exec",
                return_value=completed(["python3"]),
            ) as execute,
            patch.object(live_run, "wait_for", side_effect=lambda _name, ready: ready()),
        ):
            live_run.wait_for_hardware_fixture("server", 101, Path(raw), "opengl")

        command = execute.call_args.args[1]
        self.assertEqual(command[-2:], ["opengl", live_run.INTERACTION_FIXTURE_SCRIPT])

    def test_dead_hardware_child_stops_server_before_collecting_evidence(self) -> None:
        events: list[str] = []
        process_states = iter((True, True, False))

        def process_exists(_container: str, _pid: int) -> bool:
            state = next(process_states)
            events.append(f"server-alive={state}")
            return state

        def execute(
            _container: str,
            command: list[str],
            **_kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            if command[:2] == ["python3", "-c"]:
                events.append("child-dead")
                return completed(command, returncode=76)
            self.assertEqual(command, ["kill", "-TERM", "101"])
            events.append("stop-server")
            return completed(command)

        def collect(container: str, destination: Path, role: str) -> None:
            events.append("collect-server")
            self.assertEqual((container, role), ("server", "server"))
            (destination / "interaction.stderr").write_text(
                "GTK failed\n",
                encoding="utf-8",
            )
            (destination / "interaction.exit").write_text("1\n", encoding="ascii")

        def wait_control(
            description: str,
            predicate: object,
            **_kwargs: object,
        ) -> None:
            if description == "hardware fixture GTK and vulkan readiness":
                predicate()  # type: ignore[operator]
            else:
                self.assertTrue(predicate())  # type: ignore[operator]

        with (
            tempfile.TemporaryDirectory() as raw,
            patch.object(
                live_run,
                "container_process_exists",
                side_effect=process_exists,
            ),
            patch.object(live_run, "podman_exec", side_effect=execute),
            patch.object(live_run, "pull_all_container_artifacts", side_effect=collect),
            patch.object(live_run, "wait_for", side_effect=wait_control),
            self.assertRaisesRegex(live_run.LabFailure, "GTK failed"),
        ):
            live_run.wait_for_hardware_fixture("server", 101, Path(raw), "hardware")

        self.assertEqual(
            events,
            [
                "server-alive=True",
                "child-dead",
                "server-alive=True",
                "stop-server",
                "server-alive=False",
                "collect-server",
            ],
        )

    def test_zed_mouse_pulls_only_new_immutable_screenshots(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            live_run.Image.new("RGB", (8, 8), (240, 240, 240)).save(
                directory / "window-direct.rgb.png"
            )
            baseline = "screen-updates/1/1/screenshot.png"
            post_click = "screen-updates/1/2/screenshot.png"

            def wait_once(_description: str, predicate: object, **_kwargs: object) -> None:
                self.assertTrue(predicate())  # type: ignore[operator]

            def convert_after(_directory: Path, stem: str) -> dict[str, object]:
                live_run.Image.new("RGB", (8, 8), (10, 10, 10)).save(
                    directory / f"{stem}.rgb.png"
                )
                return {
                    "image": {
                        "dominant_rgb": [10, 10, 10],
                        "rgb_sha256": "after",
                    },
                    "xwd": {"unique_rgb_colors": 256},
                }

            with (
                patch.object(
                    live_run,
                    "container_artifact_size",
                    side_effect=lambda _container, relative: {
                        "client.stdout": 101,
                        "server.stderr": 202,
                        "zed.stderr": 303,
                    }[relative],
                ) as sizes,
                patch.object(
                    live_run,
                    "container_artifact_files",
                    side_effect=[(baseline,), (baseline, post_click)],
                ) as files,
                patch.object(
                    live_run,
                    "container_artifact_suffix_matches",
                    return_value=True,
                ),
                patch.object(
                    live_run,
                    "detect_zed_system_theme_control",
                    return_value={
                        "click_position": [4, 4],
                        "dark_bounds": [0, 0, 4, 8],
                        "system_bounds": [4, 0, 8, 8],
                    },
                ),
                patch.object(
                    live_run,
                    "theme_segment_contrast",
                    side_effect=[0.1, 0.9, 0.9, 0.1],
                ),
                patch.object(live_run, "capture_xwd"),
                patch.object(live_run, "convert_xwd", side_effect=convert_after),
                patch.object(
                    live_run,
                    "compare_rgb_images",
                    return_value={"same_size": True, "mean_absolute_error": 0.0},
                ),
                patch.object(live_run, "podman_exec", return_value=completed(["xdotool"])),
                patch.object(live_run, "pull_container_artifacts") as pull,
                patch.object(live_run, "wait_for", side_effect=wait_once),
            ):
                result = live_run.exercise_zed_mouse(
                    "server",
                    "client",
                    "0x1",
                    {"width": 8, "height": 8},
                    directory,
                    {
                        "image": {
                            "dominant_rgb": [240, 240, 240],
                            "rgb_sha256": "before",
                        }
                    },
                )

        self.assertTrue(all(result["input_path"].values()))
        self.assertEqual(sizes.call_count, 3)
        self.assertEqual(files.call_count, 2)
        pull.assert_called_once_with("server", directory, (post_click,))

    def test_adaptive_h264_frame_poll_unlocks_on_exact_first_frame(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            server_log = (
                "commit wid 1 rects=[(0, 0, 64, 64)]\n"
                "do_set_client_properties(encoding.full_csc_modes={'h264': ('YUV420P',)})\n"
            )
            client_log = (
                "register_window(..) window(0x1)=ClientWindow(0x1)\n"
                "draw_region(0, 0, 64, 63, h264\n"
                "choose_decoder('h264')=libva\n"
                "do_video_paint('h264', ImageWrapper(NV12\n"
                "record_decode_time(True, 1) wid=0x1, h264:\n"
                "do_present_fbo(\n"
                "register_window(..) window(0x2)=ClientWindow(0x2)\n"
            )
            remote_info = (
                "screen-updates/1/window.info",
                "screen-updates/1/1/0.info",
            )
            payload = "screen-updates/1/1/0.h264"
            pulls: list[tuple[str, ...]] = []

            def deltas(container: str, offsets: dict[str, int]) -> dict[str, tuple[int, str]]:
                values = {
                    "server.stderr": server_log if container == "server" else "",
                    "client.stdout": client_log if container == "client" else "",
                    "client.stderr": "",
                }
                return {
                    name: (offset + len(values[name].encode()), values[name])
                    for name, offset in offsets.items()
                }

            def pull(
                container: str,
                destination: Path,
                relatives: tuple[str, ...],
            ) -> None:
                self.assertEqual(container, "server")
                self.assertEqual(destination, directory)
                pulls.append(relatives)
                for relative in relatives:
                    target = directory / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if relative.endswith("/window.info"):
                        target.write_text(
                            json.dumps({"pixel-format": "BGRX"}),
                            encoding="utf-8",
                        )
                    elif relative.endswith(".info"):
                        target.write_text(
                            json.dumps(
                                {
                                    "encoding": "h264",
                                    "file": "0.h264",
                                    "h": 63,
                                    "options": {
                                        "flush": 0,
                                        "frame": 0,
                                        "type": "IDR",
                                        "window-size": [64, 64],
                                    },
                                    "sequence": 1,
                                    "w": 64,
                                    "x": 0,
                                    "y": 0,
                                }
                            ),
                            encoding="utf-8",
                        )
                    else:
                        target.write_bytes(b"h264")

            def wait_once(_description: str, predicate: object, **_kwargs: object) -> None:
                self.assertTrue(predicate())  # type: ignore[operator]

            with (
                patch.object(live_run, "read_container_log_deltas", side_effect=deltas),
                patch.object(
                    live_run,
                    "container_artifact_files",
                    return_value=remote_info,
                ),
                patch.object(live_run, "pull_container_artifacts", side_effect=pull),
                patch.object(live_run, "wait_for", side_effect=wait_once),
                patch.object(live_run, "container_process_exists", return_value=True),
            ):
                outcome = live_run.wait_for_frame_boundary(
                    "server",
                    101,
                    "client",
                    202,
                    directory,
                    "h264",
                    "adaptive-alpha",
                    application="hardware",
                    expected_xpra_wid=1,
                )
            self.assertEqual(outcome, "success")
            self.assertEqual(pulls, [remote_info, (payload,)])
            early_updates = live_run.parse_saved_updates(directory, 1)
            early_updates["initial_pixel_format"] = "BGRX"
            self.assertTrue(
                live_run.primary_h264_frame_ready(
                    "hardware", "adaptive-alpha", early_updates
                )
            )
            self.assertFalse(
                live_run.primary_h264_packets_valid(
                    "hardware", "adaptive-alpha", early_updates
                )
            )
            self.assertIsNone(
                live_run.hardware_h264_production_updates(early_updates)
            )

            def reject_wrong_window(
                _description: str, predicate: object, **_kwargs: object
            ) -> None:
                self.assertFalse(predicate())  # type: ignore[operator]
                raise live_run.LabFailure("exact target window did not produce a frame")

            with (
                patch.object(live_run, "read_container_log_deltas", side_effect=deltas),
                patch.object(
                    live_run,
                    "container_artifact_files",
                    return_value=remote_info,
                ),
                patch.object(live_run, "pull_container_artifacts", side_effect=pull),
                patch.object(live_run, "wait_for", side_effect=reject_wrong_window),
                patch.object(live_run, "container_process_exists", return_value=True),
                self.assertRaisesRegex(
                    live_run.LabFailure, "exact target window did not produce a frame"
                ),
            ):
                live_run.wait_for_frame_boundary(
                    "server",
                    101,
                    "client",
                    202,
                    directory,
                    "h264",
                    "adaptive-alpha",
                    application="hardware",
                    expected_xpra_wid=2,
                )
            self.assertEqual(pulls, [remote_info, (payload,)])

    def test_run04_singleton_cropped_h264_is_only_early_readiness(self) -> None:
        first_webp = {
            "encoding": "webp",
            "h": 1095,
            "options": {"flush": 0, "window-size": [1536, 1095]},
            "payload_bytes": 100,
            "relative_info": "screen-updates/1/280961706/0.info",
            "sequence": 1,
            "w": 1536,
            "x": 0,
            "y": 0,
        }
        h264 = {
            "encoding": "h264",
            "h": 1094,
            "options": {
                "flush": 0,
                "frame": 0,
                "profile": "main",
                "type": "IDR",
                "window-size": [1536, 1095],
            },
            "payload_bytes": 200,
            "relative_info": "screen-updates/1/280961714/0.info",
            "sequence": 2,
            "w": 1536,
            "x": 0,
            "y": 0,
        }
        later_webp = {
            "encoding": "webp",
            "h": 1173,
            "options": {"flush": 0, "window-size": [1596, 1173]},
            "payload_bytes": 300,
            "relative_info": "screen-updates/1/280961786/0.info",
            "sequence": 3,
            "w": 1596,
            "x": 0,
            "y": 0,
        }
        updates = {
            "count": 3,
            "encodings": ["h264", "webp"],
            "initial_pixel_format": "RGBX",
            "updates": [first_webp, h264, later_webp],
            "window_id": 1,
        }
        self.assertTrue(
            live_run.primary_h264_frame_ready(
                "zed",
                "adaptive-alpha",
                updates,
            )
        )
        self.assertIsNone(live_run.adaptive_h264_production_updates(updates))
        self.assertFalse(
            live_run.primary_h264_packets_valid(
                "zed",
                "adaptive-alpha",
                updates,
            )
        )

        stable_packets: list[dict[str, object]] = []
        for sequence, group, frame in ((4, 280967000, 0), (6, 280967051, 1)):
            stable_packets.extend(
                (
                    {
                        "encoding": "rgb24",
                        "h": 1,
                        "options": {
                            "flush": 1,
                            "rgb_format": "RGBX",
                            "window-size": [1596, 1173],
                        },
                        "payload_bytes": 40,
                        "relative_info": f"screen-updates/1/{group}/0.info",
                        "sequence": sequence,
                        "w": 1596,
                        "x": 0,
                        "y": 1172,
                    },
                    {
                        "encoding": "h264",
                        "h": 1172,
                        "options": {
                            "flush": 0,
                            "frame": frame,
                            "type": "IDR" if frame == 0 else "P",
                            "window-size": [1596, 1173],
                        },
                        "payload_bytes": 200,
                        "relative_info": f"screen-updates/1/{group}/1.info",
                        "sequence": sequence + 1,
                        "w": 1596,
                        "x": 0,
                        "y": 0,
                    },
                )
            )
        short_phase = {
            **updates,
            "count": 7,
            "encodings": ["h264", "rgb24", "webp"],
            "h264_stimulus": {
                "baseline_sequence": 3,
                "last_sequence": 7,
                "window_size": [1596, 1173],
            },
            "updates": [*updates["updates"], *stable_packets],
        }
        production = live_run.adaptive_h264_production_updates(short_phase)
        self.assertIsNotNone(production)
        assert production is not None
        self.assertEqual(
            [packet["sequence"] for packet in production["updates"]],
            [4, 5, 6, 7],
        )
        metrics = live_run.h264_production_metrics("zed", short_phase)
        self.assertEqual(metrics["h264_main_frame_count"], 2)
        self.assertEqual(metrics["h264_damage_span_ms"], 51)
        self.assertFalse(all(live_run.h264_dominance_checks(metrics).values()))

        invalid: dict[str, dict[str, object]] = {}
        zero_payload = json.loads(json.dumps(updates))
        zero_payload["updates"][1]["payload_bytes"] = 0
        invalid["zero payload"] = zero_payload
        offset_main = json.loads(json.dumps(updates))
        offset_main["updates"][1]["x"] = 1
        invalid["offset h264 geometry"] = offset_main
        oversized_crop = json.loads(json.dumps(updates))
        oversized_crop["updates"][1]["h"] = 1093
        invalid["more than one pixel crop"] = oversized_crop
        missing_window_size = json.loads(json.dumps(updates))
        missing_window_size["updates"][1]["options"].pop("window-size")
        invalid["missing h264 window size"] = missing_window_size
        missing_frame = json.loads(json.dumps(updates))
        missing_frame["updates"][1]["options"].pop("frame")
        invalid["missing h264 frame option"] = missing_frame
        wrong_window = json.loads(json.dumps(updates))
        wrong_window["updates"][1]["relative_info"] = (
            "screen-updates/2/280961714/0.info"
        )
        invalid["wrong saved window"] = wrong_window
        sequence_gap = json.loads(json.dumps(updates))
        sequence_gap["updates"][2]["sequence"] = 4
        invalid["sequence gap"] = sequence_gap
        rgb_fallback = json.loads(json.dumps(updates))
        rgb_fallback["updates"][1]["encoding"] = "rgb24"
        rgb_fallback["encodings"] = ["rgb24", "webp"]
        invalid["no h264 packet"] = rgb_fallback
        for name, candidate in invalid.items():
            with self.subTest(name=name):
                self.assertFalse(
                    live_run.primary_h264_frame_ready(
                        "zed",
                        "adaptive-alpha",
                        candidate,
                    )
                )

    def test_image_and_interaction_evidence_proves_real_alpha_content(self) -> None:
        image = live_run.Image.new("RGBA", (2, 1))
        image.putdata(((10, 20, 30, 0), (40, 50, 60, 255)))
        metrics = live_run.analyze_image(image)
        self.assertEqual(metrics["alpha_minimum"], 0)
        self.assertEqual(metrics["alpha_maximum"], 255)
        self.assertEqual(metrics["alpha_nonopaque_ratio"], 0.5)
        self.assertTrue(
            all(
                live_run.image_alpha_content_checks(
                    metrics,
                    prefix="window",
                ).values()
            )
        )
        alpha_evidence = {
            "image": metrics,
            "xwd": {"unique_rgb_colors": 101},
        }
        self.assertTrue(
            live_run.client_window_content_ready("gtk", alpha_evidence)
        )
        self.assertFalse(
            live_run.client_window_content_ready("zed", alpha_evidence)
        )
        opaque_evidence = json.loads(json.dumps(alpha_evidence))
        opaque_evidence["image"]["central_opaque_ratio"] = 1.0
        self.assertTrue(
            live_run.client_window_content_ready("zed", opaque_evidence)
        )

        capture = {
            "image": metrics,
            "xwd": {"unique_rgb_colors": 101},
        }
        source_alpha = {
            "all_have_opaque_pixels": True,
            "all_have_transparent_pixels": True,
            "count": 2,
        }
        checks = live_run.interaction_alpha_content_checks(
            {
                "before": capture,
                "after": capture,
                "source_alpha": source_alpha,
            }
        )
        self.assertTrue(all(checks.values()))

        missing_alpha = live_run.interaction_alpha_content_checks(
            {
                "before": capture,
                "after": capture,
                "source_alpha": {
                    **source_alpha,
                    "all_have_transparent_pixels": False,
                },
            }
        )
        self.assertFalse(
            missing_alpha["interaction_source_has_transparent_pixels"]
        )
        self.assertTrue(missing_alpha["interaction_source_has_opaque_pixels"])

        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            screenshot = (
                directory / "screen-updates" / "2" / "100" / "screenshot.png"
            )
            screenshot.parent.mkdir(parents=True)
            image.save(screenshot)
            source = live_run.saved_source_alpha_evidence(
                directory,
                {
                    "screenshots": [
                        "screen-updates/2/100/screenshot.png",
                    ],
                    "window_id": 2,
                },
            )
            self.assertEqual(source["count"], 1)
            self.assertTrue(source["all_have_transparent_pixels"])
            self.assertTrue(source["all_have_opaque_pixels"])
            with self.assertRaisesRegex(
                live_run.LabFailure,
                "screenshot path is unsafe",
            ):
                live_run.saved_source_alpha_evidence(
                    directory,
                    {
                        "screenshots": ["../screenshot.png"],
                        "window_id": 2,
                    },
                )

    def test_source_viewport_crop_binds_fixed_source_inside_larger_backing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            backing = live_run.Image.new("RGBA", (4, 3), (0, 0, 0, 255))
            backing.putpixel((0, 0), (255, 0, 0, 255))
            backing.putpixel((1, 0), (0, 255, 0, 255))
            backing.putpixel((0, 1), (0, 0, 255, 255))
            backing.putpixel((1, 1), (255, 255, 255, 255))
            backing.save(directory / "window.rgba.png")
            evidence = live_run.crop_client_source_viewport(
                directory,
                "window",
                "source",
                (2, 2),
            )
            (directory / "client.stdout").write_text(
                "viewport: (0, 1, 2, 2) for backing size=(4, 3)\n",
                encoding="utf-8",
            )

            self.assertEqual(evidence["image"]["width"], 2)
            self.assertEqual(evidence["image"]["height"], 2)
            self.assertEqual(
                evidence["viewport"],
                {
                    "backing_size": [4, 3],
                    "origin": [0, 0],
                    "source_size": [2, 2],
                },
            )
            self.assertTrue(
                live_run.client_source_viewport_logged(
                    directory,
                    (2, 2),
                    (4, 3),
                )
            )
            self.assertFalse(
                live_run.client_source_viewport_logged(
                    directory,
                    (3, 2),
                    (4, 3),
                )
            )

    def test_saved_update_payload_cannot_escape_its_update_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            info = directory / "screen-updates" / "1" / "1" / "0.info"
            info.parent.mkdir(parents=True)
            info.write_text(
                json.dumps({"encoding": "h264", "file": "/etc/passwd"}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(live_run.LabFailure, "payload name is unsafe"):
                live_run.parse_saved_updates(directory, 1)

    def test_live_user_mapping_is_independent_of_the_host_uid(self) -> None:
        self.assertEqual(
            live_run.live_user_options(),
            [
                "--userns",
                "keep-id:uid=1001,gid=1001,size=2048",
                "--user",
                "1001:1001",
            ],
        )

    def test_live_command_rejects_an_unbounded_user_namespace(self) -> None:
        with self.assertRaisesRegex(live_run.LabFailure, "explicit size"):
            live_run.run(
                ["podman", "run", "--userns=keep-id:uid=1001,gid=1001", "image"],
                announce=False,
            )

    def test_artifact_collection_failure_fails_scenario_acceptance(self) -> None:
        report = {
            "classification": {"first_failed_boundary": "passed"},
            "container_artifact_collection": [
                {"status": "collected"},
                {"status": "collection-failed"},
            ],
        }
        self.assertFalse(live_run.scenario_acceptance(report, {"passed": True}))

        report["container_artifact_collection"] = [{"status": "collected"}]
        report["classification"]["diagnostic_only"] = True
        self.assertFalse(live_run.scenario_acceptance(report, {"passed": True}))

    def test_network_labels_accept_lowercase_podman_field(self) -> None:
        labels = {
            "io.xpra.fork-maintenance.owner": "live",
            "io.xpra.fork-maintenance.run-id": "lowercase-labels",
        }
        argv = ["podman", "network", "inspect", "network-id"]
        with patch.object(
            live_run,
            "run",
            return_value=completed(
                argv,
                json.dumps([{"id": "a" * 64, "name": "network", "labels": labels}]),
            ),
        ):
            self.assertEqual(
                live_run.inspect_podman_object_labels("network", "network-id"),
                labels,
            )

    def test_network_labels_reject_conflicting_podman_fields(self) -> None:
        argv = ["podman", "network", "inspect", "network-id"]
        payload = [
            {
                "Labels": {"io.xpra.fork-maintenance.owner": "live"},
                "labels": {"io.xpra.fork-maintenance.owner": "someone-else"},
            }
        ]
        with (
            patch.object(
                live_run,
                "run",
                return_value=completed(argv, json.dumps(payload)),
            ),
            self.assertRaisesRegex(live_run.LabFailure, "conflicting provenance"),
        ):
            live_run.inspect_podman_object_labels("network", "network-id")

    def test_nonzero_remove_is_success_when_object_is_absent(self) -> None:
        name = "container-name"
        object_id = "a" * 64
        labels = {
            "io.xpra.fork-maintenance.owner": "live",
            "io.xpra.fork-maintenance.run-id": "cleanup-run",
        }

        def podman_command(
            argv: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            if argv == ["podman", "container", "inspect", name]:
                return completed(
                    argv,
                    json.dumps(
                        [{"Config": {"Labels": labels}, "Id": object_id, "Name": name}]
                    ),
                )
            if argv == ["podman", "rm", "--force", object_id]:
                return completed(
                    argv,
                    "remove output\n",
                    returncode=125,
                    stderr="reported failure after removal\n",
                )
            if argv == ["podman", "ps", "--all", "--format", "{{.Names}}"]:
                return completed(argv, "unrelated-container\n")
            raise AssertionError(f"unexpected command: {argv!r}")

        with patch.object(live_run, "run", side_effect=podman_command):
            result = live_run.remove_owned_podman_object("container", name, labels)
        self.assertEqual(result["status"], "removed")
        self.assertEqual(result["postcondition"], "absent")
        self.assertEqual(result["remove_returncode"], 125)
        self.assertEqual(result["remove_stdout"], "remove output\n")
        self.assertEqual(result["remove_stderr"], "reported failure after removal\n")

    def test_remove_fails_when_object_remains(self) -> None:
        name = "container-name"
        object_id = "a" * 64
        labels = {
            "io.xpra.fork-maintenance.owner": "live",
            "io.xpra.fork-maintenance.run-id": "cleanup-run",
        }

        def podman_command(
            argv: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            if argv == ["podman", "container", "inspect", name]:
                return completed(
                    argv,
                    json.dumps(
                        [{"Config": {"Labels": labels}, "Id": object_id, "Name": name}]
                    ),
                )
            if argv == ["podman", "rm", "--force", object_id]:
                return completed(argv, returncode=125, stderr="remove failed\n")
            if argv == ["podman", "ps", "--all", "--format", "{{.Names}}"]:
                return completed(argv, f"{name}\n")
            raise AssertionError(f"unexpected command: {argv!r}")

        with patch.object(live_run, "run", side_effect=podman_command):
            result = live_run.remove_owned_podman_object("container", name, labels)
        self.assertEqual(result["status"], "remove-failed")
        self.assertEqual(result["postcondition"], "present")


class LiveSourceTest(unittest.TestCase):
    def test_freezes_the_source_boundary_embedded_in_develop_without_network(self) -> None:
        commit = "1" * 40
        head = "2" * 40
        source_tip = "3" * 40
        responses = {
            ("rev-parse", "--is-inside-work-tree"): "true",
            ("branch", "--show-current"): "develop",
            ("remote",): "origin",
            ("remote", "get-url", "origin"): live_run.FORK_REMOTE_URL,
            ("rev-parse", "HEAD"): head,
            ("rev-parse", "refs/remotes/origin/master"): source_tip,
            ("merge-base", "--all", source_tip, head): commit,
            ("describe", "--long", "--always", "--tags", commit): "v6.4-1-g111111111",
            ("rev-list", "--count", "--first-parent", commit): "10",
        }
        with tempfile.TemporaryDirectory() as raw:
            repository = Path(raw)
            (repository / ".git").mkdir()
            with (
                patch.object(live_run, "SOURCE_REPOSITORY", repository),
                patch.object(
                    live_run,
                    "git_output",
                    side_effect=lambda *arguments: responses[arguments],
                ),
            ):
                resolved, marker, revision = live_run.resolve_embedded_source()

        self.assertEqual(resolved, commit)
        self.assertEqual(marker, "g111111111")
        self.assertEqual(revision, 5024)

    def test_input_evidence_archives_symlinked_build_contexts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result = root / "result"
            result.mkdir()
            source_archive = root / "source.tar"
            source_archive.write_bytes(b"source")
            selection = live_run.PatchSelection(
                case_slugs=(),
                digest="1" * 64,
                kind="legacy",
                name="master",
                patches=(),
                required_gates=(),
                selector_digests=(),
                selectors=(),
            )
            contexts: list[live_run.BuildContext] = []
            resolution = {"resolution_sha256": "2" * 64}
            context_manifest = {
                "selection": {
                    "case_slugs": [],
                    "digest": selection.digest,
                    "kind": selection.kind,
                    "name": selection.name,
                    "required_gates": [],
                    "resolution": resolution,
                    "selector_digests": {},
                    "selectors": [],
                }
            }
            for role in ("server", "client"):
                context_root = root / role
                context_root.mkdir()
                (context_root / "value").write_text(role, encoding="utf-8")
                (context_root / "link").symlink_to("value")
                contexts.append(
                    live_run.BuildContext(
                        digest=live_run.tree_sha256(context_root),
                        manifest=context_manifest,
                        patches=(),
                        path=context_root,
                        resolution=resolution,
                        selection=selection,
                    )
                )
            snapshot = live_run.SourceSnapshot(
                archive_path=source_archive,
                archive_sha256=hashlib.sha256(b"source").hexdigest(),
                commit="3" * 40,
                commit_marker="g333333333",
                revision=1,
                workflow_sha256="4" * 64,
            )

            manifest_sha256, _zed_archive, _zed_sha256 = (
                live_run.snapshot_build_inputs(
                    result,
                    snapshot,
                    contexts[0],
                    contexts[1],
                    None,
                )
            )

            inputs = result / "inputs"
            (inputs / "SHA256SUMS").chmod(0o600)
            self.assertTrue((inputs / "contexts/server.tar").is_file())
            self.assertFalse((inputs / "contexts/server").exists())
            self.assertTrue(
                job.input_checksum_validation(
                    inputs,
                    {
                        "input_manifest_sha256": manifest_sha256,
                        "input_tree_sha256": live_run.tree_sha256(inputs),
                    },
                )
            )
            extracted = root / "extracted"
            with (inputs / "contexts/server.tar").open("rb") as stream:
                live_run.container_payload.extract_archive(stream, extracted)
            self.assertEqual(os.readlink(extracted / "link"), "value")

            replacement = root / "replacement"
            replacement.mkdir()
            (replacement / "different").write_text("different\n", encoding="utf-8")
            server_archive = inputs / "contexts/server.tar"
            server_archive.unlink()
            with server_archive.open("xb") as stream:
                live_run.container_payload.write_archive(
                    stream,
                    (
                        live_run.container_payload.PayloadEntry(
                            replacement / "different",
                            live_run.PurePosixPath("different"),
                        ),
                    ),
                )
            server_archive.chmod(0o600)
            manifest_path = inputs / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["server_context_archive_sha256"] = live_run.sha256_file(
                server_archive
            )
            with self.assertRaisesRegex(
                live_run.LabFailure,
                "does not match its context digest",
            ):
                live_run._bound_context(inputs, "server", manifest)

    def test_frozen_selection_admission_uses_the_exact_snapshot_root(self) -> None:
        selected = live_run.resolve_patch_selection(
            "cases/wayland-empty-damage-throttle",
            None,
        )
        resolution = {"resolution_sha256": "2" * 64}
        context = live_run.BuildContext(
            digest="3" * 64,
            manifest={},
            patches=selected.patches,
            path=Path("/unused"),
            resolution=resolution,
            selection=selected,
        )
        with tempfile.TemporaryDirectory() as raw:
            inputs = Path(raw) / "inputs"
            root = inputs / "selections" / "server"
            root.parent.mkdir(parents=True)
            live_run.snapshot_patch_selection(root, context)
            live_run.privatize_regular_tree(inputs)
            snapshot_root = (
                root
                / "validated-manifests"
                / "0001-cases-wayland-empty-damage-throttle"
            )
            observed_roots: list[Path] = []
            output_at = live_run.selection_output_at

            def frozen_output(
                lab_root: Path,
                selector: str,
                action: str,
                *arguments: str,
            ) -> str:
                observed_roots.append(lab_root)
                self.assertEqual(lab_root, snapshot_root)
                return output_at(lab_root, selector, action, *arguments)

            with (
                patch.object(
                    live_run,
                    "MAINTENANCE_ROOT",
                    Path(raw) / "mutable-host-manifests-must-not-be-read",
                ),
                patch.object(
                    live_run,
                    "selection_output_at",
                    side_effect=frozen_output,
                ),
            ):
                live_run._validate_frozen_selection(
                    inputs,
                    "server",
                    selected,
                    resolution,
                )
            self.assertTrue(observed_roots)
            self.assertEqual(set(observed_roots), {snapshot_root})


class LiveTransportProfileTest(unittest.TestCase):
    def test_tracked_live_configuration_drives_all_runner_option_blocks(self) -> None:
        default, profiles = live_run.live_config.load_network_profiles()
        self.assertIn(default, profiles)
        self.assertEqual(tuple(profiles), live_run.NETWORK_PROFILES)
        self.assertEqual(default, live_run.DEFAULT_NETWORK_PROFILE)

        for profile_name, profile in profiles.items():
            with self.subTest(profile=profile_name):
                self.assertEqual(
                    live_run.client_network_options(profile_name),
                    list(profile.client_options()),
                )

        configured = live_run.live_config.load_live_cli()
        server_diagnostics = configured["server"]["diagnostics"]
        self.assertEqual(len(server_diagnostics), 2)
        self.assertEqual(server_diagnostics[0], "-d")
        server_debug_categories = server_diagnostics[1].split(",")
        self.assertIn("wayland", server_debug_categories)
        self.assertEqual(server_debug_categories.count("-clipboard"), 1)
        self.assertEqual(server_debug_categories[-1], "-clipboard")
        profiles_module = sys.modules["profiles"]
        for role, blocks in configured.items():
            for block, options in blocks.items():
                if block in {"clipboard", "commands", "transports"}:
                    continue
                with self.subTest(role=role, block=block):
                    self.assertEqual(
                        live_run.static_cli_options(role, block),
                        list(options),
                    )
            self.assertEqual(
                tuple(blocks["clipboard"]),
                profiles_module.CLIPBOARD_POLICIES,
            )
            for policy, options in blocks["clipboard"].items():
                with self.subTest(role=role, clipboard_policy=policy):
                    self.assertEqual(
                        live_run.live_config.clipboard_options(role, policy),
                        options,
                    )
            for command, options in blocks["commands"].items():
                with self.subTest(role=role, command=command):
                    self.assertEqual(
                        live_run.command_cli_options(role, command),
                        list(options),
                    )
            for encoding, transport in blocks["transports"].items():
                for policy in transport["policies"]:
                    with self.subTest(
                        role=role,
                        encoding=encoding,
                        policy=policy,
                    ):
                        self.assertEqual(
                            live_run.transport_encoding_options(
                                encoding,
                                policy,
                                client=role == "client",
                            ),
                            list(
                                live_run.live_config.transport_options(
                                    role,
                                    encoding,
                                    policy,
                                )
                            ),
                        )

        harness = set(live_run.HARNESS_INPUTS)
        self.assertEqual(job.HARNESS_INPUTS, live_run.HARNESS_INPUTS)
        self.assertIn(live_run.LIVE_CONFIG_MODULE, harness)
        self.assertIn(live_run.NETWORK_PROFILES_CONFIG, harness)
        self.assertIn(live_run.LIVE_CLI_CONFIG, harness)

    def test_clipboard_command_publication_is_atomic_and_no_replace(self) -> None:
        with patch.object(live_run, "podman_exec") as podman_exec:
            live_run.write_clipboard_command(
                "clipboard-client",
                live_run.CLIPBOARD_OWNER_COMMAND,
                "set:two",
            )
        podman_exec.assert_called_once()
        container, command = podman_exec.call_args.args
        self.assertEqual(container, "clipboard-client")
        self.assertEqual(command[:2], ["python3", "-c"])
        self.assertEqual(command[2], live_run.CLIPBOARD_COMMAND_PUBLISHER)
        self.assertEqual(
            command[3:],
            [live_run.CLIPBOARD_OWNER_COMMAND, "set:two"],
        )

        with patch.object(live_run, "podman_exec") as monitor_exec:
            live_run.write_clipboard_command(
                "clipboard-client",
                live_run.CLIPBOARD_MONITOR_COMMAND,
                "stop",
            )
        monitor_exec.assert_called_once_with(
            "clipboard-client",
            [
                "python3",
                "-c",
                live_run.CLIPBOARD_COMMAND_PUBLISHER,
                live_run.CLIPBOARD_MONITOR_COMMAND,
                "stop",
            ],
        )

        script = compile(command[2], "<clipboard-command-publisher>", "exec")

        def publish(path: Path) -> None:
            with patch.object(sys, "argv", ["-c", str(path), "set:two"]):
                exec(script, {"__name__": "__main__"})  # noqa: S102

        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            path = directory / "command"
            real_fsync = os.fsync
            real_link = os.link
            state = {"fsynced": False, "linked": False}

            def fsync(descriptor: int) -> None:
                self.assertEqual(os.fstat(descriptor).st_mode & 0o777, 0o600)
                state["fsynced"] = True
                real_fsync(descriptor)

            def link(source: Path, destination: Path, **kwargs: object) -> None:
                self.assertTrue(state["fsynced"])
                self.assertFalse(Path(destination).exists())
                self.assertEqual(Path(source).read_bytes(), b"set:two\n")
                self.assertEqual(Path(source).stat().st_mode & 0o777, 0o600)
                state["linked"] = True
                real_link(source, destination, **kwargs)

            with (
                patch.object(os, "fsync", side_effect=fsync),
                patch.object(os, "link", side_effect=link),
            ):
                publish(path)
            self.assertTrue(state["linked"])
            self.assertEqual(path.read_bytes(), b"set:two\n")
            self.assertEqual(list(directory.glob(".command.*.partial")), [])

            path.write_bytes(b"existing\n")
            with self.assertRaises(FileExistsError):
                publish(path)
            self.assertEqual(path.read_bytes(), b"existing\n")
            self.assertEqual(list(directory.glob(".command.*.partial")), [])

        with (
            patch.object(live_run, "podman_exec") as rejected_exec,
            self.assertRaisesRegex(
                live_run.LabFailure,
                "invalid clipboard fixture command",
            ),
        ):
            live_run.write_clipboard_command(
                "clipboard-client",
                "/tmp/xpra-unowned-command",
                "set:two",
            )
        rejected_exec.assert_not_called()

        with (
            patch.object(live_run, "podman_exec") as rejected_exec,
            self.assertRaisesRegex(
                live_run.LabFailure,
                "invalid clipboard fixture command",
            ),
        ):
            live_run.write_clipboard_command(
                "clipboard-client",
                live_run.CLIPBOARD_OWNER_COMMAND,
                "paste:one",
            )
        rejected_exec.assert_not_called()

    def test_x11_clipboard_owner_disables_unavailable_at_spi_bridge(self) -> None:
        with (
            patch.object(live_run, "podman_exec") as podman_exec,
            patch.object(
                live_run,
                "wait_for_clipboard_event_count",
            ) as wait_for_event,
        ):
            live_run.start_x11_clipboard_owner("clipboard-client")

        podman_exec.assert_called_once()
        container, command = podman_exec.call_args.args
        self.assertEqual(container, "clipboard-client")
        self.assertEqual(command[:2], ["bash", "-lc"])
        self.assertIn(
            "env DISPLAY=:0 GDK_BACKEND=x11 NO_AT_BRIDGE=1 python3 ",
            command[2],
        )
        wait_for_event.assert_called_once_with(
            "clipboard-client",
            "clipboard-owner.stdout",
            "owner-ready",
            1,
            "local X11 clipboard owner",
        )

    def test_clipboard_fixture_uses_input_serial_without_focus_animation(self) -> None:
        source = (LIVE_DIRECTORY / "wayland_clipboard_fixture.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("entry.set_can_focus(False)", source)
        self.assertNotIn("entry.grab_focus()", source)
        self.assertIn("event.keyval == Gdk.KEY_F8", source)
        self.assertIn('clipboard.connect("owner-change", clipboard_owner_changed)', source)
        self.assertIn("publish_owner_confirmation, state[\"confirming_owner\"]", source)
        self.assertIn('"owner-armed"', source)
        self.assertIn('"owner-set"', source)
        self.assertIn('"owner-confirmed"', source)

    def test_clipboard_fixture_confirmation_owns_one_pending_callback(self) -> None:
        """Execute the fixture callbacks: arming alone cannot confirm ownership."""
        gdk = Mock(SELECTION_CLIPBOARD=1, KEY_F8=65477, KEY_Escape=65307)
        gdk.Display.get_default.return_value.get_name.return_value = "wayland-0"
        gtk = Mock(STYLE_PROVIDER_PRIORITY_APPLICATION=1)
        glib = Mock(SOURCE_REMOVE=False, SOURCE_CONTINUE=True)
        window_signals: dict[str, Callable] = {}
        clipboard_signals: dict[str, Callable] = {}
        gtk.Window.return_value.connect.side_effect = window_signals.__setitem__
        clipboard = gtk.Clipboard.get.return_value
        clipboard.connect.side_effect = clipboard_signals.__setitem__
        pending: list[tuple[Callable, tuple]] = []

        def idle(callback: Callable, *args: object) -> int:
            pending.append((callback, args))
            return len(pending)

        glib.idle_add.side_effect = idle
        fake_modules = {
            "gi": Mock(),
            "gi.repository": SimpleNamespace(Gdk=gdk, GLib=glib, Gtk=gtk),
        }
        specification = importlib.util.spec_from_file_location(
            "clipboard_fixture_callbacks", LIVE_DIRECTORY / "wayland_clipboard_fixture.py"
        )
        assert specification is not None and specification.loader is not None
        fixture = importlib.util.module_from_spec(specification)
        with patch.dict(sys.modules, fake_modules):
            specification.loader.exec_module(fixture)

        def exercise() -> None:
            poll = glib.timeout_add.call_args.args[1]
            changed = clipboard_signals["owner-change"]
            pressed = window_signals["key-press-event"]
            changed(clipboard, SimpleNamespace(selection=1))
            self.assertEqual(len(pending), 1)  # readiness only
            poll()
            clipboard.set_text.assert_not_called()
            self.assertFalse(pressed(None, SimpleNamespace(keyval=1)))

            def backend_notification(*_args: object) -> None:
                changed(clipboard, SimpleNamespace(selection=2))
                self.assertEqual(len(pending), 1)
                changed(clipboard, SimpleNamespace(selection=1))
                changed(clipboard, SimpleNamespace(selection=1))

            clipboard.set_text.side_effect = backend_notification
            self.assertTrue(pressed(None, SimpleNamespace(keyval=65477)))
            self.assertEqual(len(pending), 2)
            callback, args = pending[-1]
            callback(*args)
            changed(clipboard, SimpleNamespace(selection=1))
            self.assertEqual(len(pending), 2)
            clipboard.set_text.side_effect = lambda *_args: changed(
                clipboard, SimpleNamespace(selection=1)
            )
            poll()
            pressed(None, SimpleNamespace(keyval=65477))
            callback, args = pending[-1]
            window_signals["delete-event"](None, None)
            glib.source_remove.assert_called_once_with(3)
            callback(*args)  # already queued callback must be harmless after close
            changed(clipboard, SimpleNamespace(selection=1))

        gtk.main.side_effect = exercise
        output = StringIO()
        with (
            patch.object(fixture, "take_command", side_effect=[("own", "three")] * 2),
            patch.object(fixture.signal, "signal"),
            redirect_stdout(output),
            tempfile.TemporaryDirectory() as temporary,
        ):
            self.assertEqual(fixture.run(Path(temporary) / "command"), 0)
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(
            [record["event"] for record in records],
            ["owner-armed", "owner-input", "owner-set", "owner-confirmed",
             "owner-armed", "owner-input", "owner-set", "closed"],
        )

    def test_clipboard_monitor_drains_late_events_after_stop(self) -> None:
        specification = importlib.util.spec_from_file_location(
            "clipboard_monitor_callbacks", LIVE_DIRECTORY / "x11_clipboard_fixture.py"
        )
        assert specification is not None and specification.loader is not None
        fixture = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(fixture)
        clock_ns = 1_000_000_000
        queue: list[int] = []
        sync_sent = False
        stop_requested = False
        x11 = Mock(root=501, display=1)
        x11.atom.return_value = 51
        x11.owner.return_value = 401

        def sleep(_duration: float) -> None:
            nonlocal clock_ns
            clock_ns += 10_000_000

        def sync(*_args: object) -> None:
            nonlocal sync_sent
            if not sync_sent and stop_requested:
                sync_sent = True
                queue.extend((601, 401))

        def stop(_path: Path) -> bool:
            nonlocal stop_requested
            stop_requested = True
            return True

        def event(_display: object, pointer: object) -> None:
            value = pointer._obj.xfixes
            value.type = 87
            value.window = 501
            value.owner = queue.pop(0)
            value.selection = 51
            value.selection_timestamp = clock_ns // 1_000_000

        def query(_display: object, event_base: object, _error_base: object) -> bool:
            event_base._obj.value = 87
            return True

        x11.fixes.XFixesQueryExtension.side_effect = query
        x11.lib.XPending.side_effect = lambda _display: len(queue)
        x11.lib.XNextEvent.side_effect = event
        x11.lib.XSync.side_effect = sync
        output = StringIO()
        with (
            patch.object(fixture, "X11", return_value=x11),
            patch.object(fixture, "take_monitor_stop", side_effect=stop),
            patch.object(fixture, "time", SimpleNamespace(
                monotonic=lambda: clock_ns / 1_000_000_000,
                monotonic_ns=lambda: clock_ns,
                sleep=sleep,
            )),
            redirect_stdout(output),
            tempfile.TemporaryDirectory() as temporary,
        ):
            self.assertEqual(fixture.run_monitor(
                event_window=None, root=True,
                stop_file=Path(temporary) / "stop", timeout=1,
            ), 0)
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([record["owner_xid"] for record in records[1:3]], [601, 401])
        self.assertEqual(records[-1]["event_count"], 2)
        self.assertGreaterEqual(records[-1]["drained_ns"], records[-1]["stop_requested_ns"])

    def test_tracked_live_configuration_parser_fails_closed_generically(self) -> None:
        for source in (
            live_run.NETWORK_PROFILES_CONFIG,
            live_run.LIVE_CLI_CONFIG,
        ):
            with self.subTest(source=source.name), tempfile.TemporaryDirectory() as raw:
                candidate = Path(raw) / source.name
                lines = source.read_text(encoding="utf-8").splitlines()
                first_content = next(
                    index
                    for index, line in enumerate(lines)
                    if line and not line.startswith("#")
                )
                lines[first_content] += " "
                candidate.write_text("\n".join(lines) + "\n", encoding="utf-8")
                with self.assertRaisesRegex(
                    live_run.live_config.LiveConfigError,
                    "unsafe whitespace",
                ):
                    live_run.live_config.load_strict_yaml(candidate)

    def test_every_tracked_network_profile_supports_every_acceptance_profile(
        self,
    ) -> None:
        for application in live_run.APPLICATIONS:
            for lifecycle in live_run.LIFECYCLES:
                for encoding in ("rgb", "h264"):
                    for policy in live_run.H264_CLIENT_POLICIES:
                        for alpha_scenarios in live_run.ALPHA_SCENARIOS:
                            values = (
                                application,
                                lifecycle,
                                encoding,
                                policy,
                                alpha_scenarios,
                            )
                            outcomes = []
                            for profile_name in live_run.NETWORK_PROFILES:
                                try:
                                    live_run.validate_profile(
                                        application=application,
                                        lifecycle=lifecycle,
                                        encoding=encoding,
                                        h264_client_policy=policy,
                                        alpha_scenarios=alpha_scenarios,
                                        network_profile_name=profile_name,
                                    )
                                except live_run.ProfileError:
                                    outcomes.append(False)
                                else:
                                    outcomes.append(True)
                            with self.subTest(values=values):
                                self.assertEqual(len(set(outcomes)), 1)

    def test_supported_xpra_only_profiles_are_exact(self) -> None:
        expected_stack = {
            ("keyboard", "application-exit", "rgb", "strict", "default"),
            ("zed", "application-exit", "rgb", "strict", "default"),
            ("zed", "application-exit", "h264", "adaptive-alpha", "default"),
            (
                "hardware",
                "application-exit",
                "h264",
                "adaptive-alpha",
                "default",
            ),
            (
                "opengl",
                "application-exit",
                "h264",
                "adaptive-alpha",
                "default",
            ),
            ("gtk", "detach", "rgb", "strict", "default"),
            ("gtk", "transport-loss", "rgb", "strict", "default"),
        }
        expected_case_only = {
            ("clipboard", "application-exit", "rgb", "strict", "default"),
            ("subsurface", "application-exit", "rgb", "strict", "default"),
        }
        profiles_module = sys.modules["profiles"]
        self.assertEqual(
            profiles_module.STACK_LIVE_ACCEPTANCE_PROFILES,
            expected_stack,
        )
        self.assertEqual(
            profiles_module.CASE_ONLY_LIVE_ACCEPTANCE_PROFILES,
            expected_case_only,
        )
        self.assertFalse(expected_stack & expected_case_only)
        expected = expected_stack | expected_case_only
        self.assertEqual(
            set(profiles_module.LIVE_PROFILE_REQUIRED_GATES),
            expected,
        )
        self.assertEqual(
            profiles_module.STACK_ONLY_LIVE_ACCEPTANCE_PROFILES,
            {
                ("zed", "application-exit", "h264", "adaptive-alpha", "default"),
                ("gtk", "detach", "rgb", "strict", "default"),
                ("gtk", "transport-loss", "rgb", "strict", "default"),
            },
        )
        observed = set()
        for application in live_run.APPLICATIONS:
            for lifecycle in live_run.LIFECYCLES:
                for encoding in ("rgb", "h264"):
                    for policy in live_run.H264_CLIENT_POLICIES:
                        for alpha_scenarios in live_run.ALPHA_SCENARIOS:
                            values = (
                                application,
                                lifecycle,
                                encoding,
                                policy,
                                alpha_scenarios,
                            )
                            with self.subTest(values=values):
                                try:
                                    live_run.validate_profile(
                                        application=application,
                                        lifecycle=lifecycle,
                                        encoding=encoding,
                                        h264_client_policy=policy,
                                        alpha_scenarios=alpha_scenarios,
                                    )
                                except live_run.ProfileError:
                                    continue
                                observed.add(values)
        self.assertEqual(observed, expected)

    def test_clipboard_profile_requires_its_exact_case_selection(self) -> None:
        profiles_module = sys.modules["profiles"]
        exact = "cases/x11-client-clipboard-events"
        self.assertEqual(profiles_module.CLIPBOARD_CASE_SELECTION, exact)
        profiles_module.validate_profile_selection(
            application="clipboard",
            lifecycle="application-exit",
            encoding="rgb",
            h264_client_policy="strict",
            alpha_scenarios="default",
            selection=exact,
            selection_kind="case",
            required_gates=("live-x11-clipboard",),
        )
        for selection in (
            "stacks/develop",
            "cases/wayland-client-keymap-sync",
        ):
            with (
                self.subTest(selection=selection),
                self.assertRaisesRegex(
                    live_run.ProfileError,
                    "clipboard live acceptance requires selection",
                ),
            ):
                profiles_module.validate_profile_selection(
                    application="clipboard",
                    lifecycle="application-exit",
                    encoding="rgb",
                    h264_client_policy="strict",
                    alpha_scenarios="default",
                    selection=selection,
                    selection_kind=(
                        "stack" if selection.startswith("stacks/") else "case"
                    ),
                    required_gates=("live-wayland-keyboard",),
                )

        profile_argv = [
            "profiles.py",
            "clipboard",
            "application-exit",
            "rgb",
            "strict",
            "default",
        ]
        with patch.object(
            sys,
            "argv",
            [*profile_argv, "--selection", exact],
        ):
            self.assertEqual(profiles_module.main(), 0)
        for selection in (
            "stacks/develop",
            "cases/wayland-client-keymap-sync",
        ):
            stderr = StringIO()
            with (
                self.subTest(cli_selection=selection),
                patch.object(
                    sys,
                    "argv",
                    [*profile_argv, "--selection", selection],
                ),
                patch.object(sys, "stderr", stderr),
            ):
                self.assertEqual(profiles_module.main(), 2)
            self.assertIn(
                "clipboard live acceptance requires selection",
                stderr.getvalue(),
            )

    def test_subsurface_profile_requires_its_exact_case_selection(self) -> None:
        profiles_module = sys.modules["profiles"]
        exact = "cases/wayland-subsurface-stream-ownership"
        self.assertEqual(profiles_module.SUBSURFACE_CASE_SELECTION, exact)
        profile = ("subsurface", "application-exit", "rgb", "strict", "default")
        profiles_module.validate_profile_selection(
            application=profile[0],
            lifecycle=profile[1],
            encoding=profile[2],
            h264_client_policy=profile[3],
            alpha_scenarios=profile[4],
            selection=exact,
            selection_kind="case",
            required_gates=("live-wayland-subsurface",),
        )
        for selection in ("stacks/develop", "cases/wayland-initial-window-state"):
            with (
                self.subTest(selection=selection),
                self.assertRaisesRegex(
                    live_run.ProfileError,
                    "subsurface live acceptance requires selection",
                ),
            ):
                profiles_module.validate_profile_selection(
                    application=profile[0],
                    lifecycle=profile[1],
                    encoding=profile[2],
                    h264_client_policy=profile[3],
                    alpha_scenarios=profile[4],
                    selection=selection,
                    selection_kind=(
                        "stack" if selection.startswith("stacks/") else "case"
                    ),
                    required_gates=("live-wayland-subsurface",),
                )

    def test_case_and_stack_profiles_require_the_exact_admission_gate(self) -> None:
        profiles_module = sys.modules["profiles"]

        def validate(
            profile: tuple[str, str, str, str, str],
            selection: str,
            kind: str,
            gates: tuple[str, ...],
        ) -> None:
            application, lifecycle, encoding, policy, alpha = profile
            profiles_module.validate_profile_selection(
                application=application,
                lifecycle=lifecycle,
                encoding=encoding,
                h264_client_policy=policy,
                alpha_scenarios=alpha,
                selection=selection,
                selection_kind=kind,
                required_gates=gates,
            )

        for profile in profiles_module.STACK_LIVE_ACCEPTANCE_PROFILES:
            with self.subTest(stack_profile=profile):
                validate(profile, "stacks/develop", "stack", ())
        with self.assertRaisesRegex(
            live_run.ProfileError,
            "clipboard live acceptance requires selection",
        ):
            validate(
                ("clipboard", "application-exit", "rgb", "strict", "default"),
                "stacks/develop",
                "stack",
                (),
            )

        accepted_cases = (
            (
                ("zed", "application-exit", "rgb", "strict", "default"),
                "cases/wayland-empty-damage-throttle",
                "live-rgb",
            ),
            (
                (
                    "hardware",
                    "application-exit",
                    "h264",
                    "adaptive-alpha",
                    "default",
                ),
                "cases/wayland-initial-window-state",
                "live-wayland-h264-hardware",
            ),
            (
                (
                    "opengl",
                    "application-exit",
                    "h264",
                    "adaptive-alpha",
                    "default",
                ),
                "cases/wayland-initial-window-state",
                "live-wayland-opengl-h264-hardware",
            ),
            (
                ("keyboard", "application-exit", "rgb", "strict", "default"),
                "cases/wayland-client-keymap-sync",
                "live-wayland-keyboard",
            ),
            (
                ("clipboard", "application-exit", "rgb", "strict", "default"),
                "cases/x11-client-clipboard-events",
                "live-x11-clipboard",
            ),
            (
                ("subsurface", "application-exit", "rgb", "strict", "default"),
                "cases/wayland-subsurface-stream-ownership",
                "live-wayland-subsurface",
            ),
        )
        for profile, case, gate in accepted_cases:
            with self.subTest(case=case, gate=gate):
                validate(profile, case, "case", (gate,))

        hardware = (
            "hardware",
            "application-exit",
            "h264",
            "adaptive-alpha",
            "default",
        )
        with self.assertRaisesRegex(
            live_run.ProfileError,
            "does not declare required gate live-wayland-h264-hardware",
        ):
            validate(
                hardware,
                "cases/video-pipeline-cleanup-race",
                "case",
                (),
            )
        for profile in profiles_module.STACK_ONLY_LIVE_ACCEPTANCE_PROFILES:
            gate = profiles_module.LIVE_PROFILE_REQUIRED_GATES[profile]
            with (
                self.subTest(stack_only_case_profile=profile),
                self.assertRaisesRegex(
                    live_run.ProfileError,
                    f"live profile {gate} requires a stack selection",
                ),
            ):
                validate(
                    profile,
                    "cases/wayland-initial-window-state",
                    "case",
                    (gate,),
                )

    def test_public_live_clis_do_not_advertise_fallback_diagnostics(self) -> None:
        with (
            patch.object(sys, "stderr", StringIO()),
            self.assertRaises(SystemExit) as job_exit,
        ):
            job.parser().parse_args(
                [
                    "start",
                    "fallback-run",
                    "--encoding",
                    "h264",
                    "--h264-client-policy",
                    "fallback-auto",
                    "--selection",
                    "stacks/develop",
                ]
            )
        self.assertEqual(job_exit.exception.code, 2)

        with (
            patch.object(
                sys,
                "argv",
                [
                    "run.py",
                    "--h264-client-policy",
                    "fallback-h264",
                    "--selection",
                    "stacks/develop",
                ],
            ),
            patch.object(sys, "stderr", StringIO()),
            patch.object(live_run.PIL, "__version__", live_run.EXPECTED_PILLOW_VERSION),
            self.assertRaises(SystemExit) as runner_exit,
        ):
            live_run.main()
        self.assertEqual(runner_exit.exception.code, 2)

        with (
            patch.object(
                sys,
                "argv",
                [
                    "run.py",
                    "--selection",
                    "stacks/develop",
                    "--source-variant",
                    "master",
                ],
            ),
            patch.object(sys, "stderr", StringIO()),
            patch.object(live_run.PIL, "__version__", live_run.EXPECTED_PILLOW_VERSION),
            self.assertRaises(SystemExit) as clean_alias_exit,
        ):
            live_run.main()
        self.assertEqual(clean_alias_exit.exception.code, 2)

        profiles_module = sys.modules["profiles"]
        with (
            patch.object(
                sys,
                "argv",
                [
                    "profiles.py",
                    "zed",
                    "application-exit",
                    "h264",
                    "fallback-auto",
                    "default",
                ],
            ),
            patch.object(sys, "stderr", StringIO()),
            self.assertRaises(SystemExit) as profiles_exit,
        ):
            profiles_module.main()
        self.assertEqual(profiles_exit.exception.code, 2)

    def test_named_make_profiles_fix_every_acceptance_dimension(self) -> None:
        makefile = (LIVE_DIRECTORY.parents[1] / "Makefile").read_text(encoding="utf-8")
        expected = {
            "live-rgb": (
                "APPLICATION=zed",
                "LIFECYCLE=application-exit",
                "ENCODING=rgb",
                "H264_CLIENT_POLICY=strict",
                "ALPHA_SCENARIOS=default",
            ),
            "live-h264": (
                "APPLICATION=zed",
                "LIFECYCLE=application-exit",
                "ENCODING=h264",
                "H264_CLIENT_POLICY=adaptive-alpha",
                "ALPHA_SCENARIOS=default",
            ),
            "live-wayland-keyboard": (
                "APPLICATION=keyboard",
                "LIFECYCLE=application-exit",
                "ENCODING=rgb",
                "H264_CLIENT_POLICY=strict",
                "ALPHA_SCENARIOS=default",
            ),
            "live-x11-clipboard": (
                "APPLICATION=clipboard",
                "LIFECYCLE=application-exit",
                "ENCODING=rgb",
                "H264_CLIENT_POLICY=strict",
                "ALPHA_SCENARIOS=default",
            ),
            "live-wayland-subsurface": (
                "APPLICATION=subsurface",
                "LIFECYCLE=application-exit",
                "ENCODING=rgb",
                "H264_CLIENT_POLICY=strict",
                "ALPHA_SCENARIOS=default",
            ),
            "live-xpra-detach": (
                "APPLICATION=gtk",
                "LIFECYCLE=detach",
                "ENCODING=rgb",
                "H264_CLIENT_POLICY=strict",
                "ALPHA_SCENARIOS=default",
            ),
            "live-xpra-transport-loss": (
                "APPLICATION=gtk",
                "LIFECYCLE=transport-loss",
                "ENCODING=rgb",
                "H264_CLIENT_POLICY=strict",
                "ALPHA_SCENARIOS=default",
            ),
            "live-xpra-hardware": (
                "APPLICATION=hardware",
                "LIFECYCLE=application-exit",
                "ENCODING=h264",
                "H264_CLIENT_POLICY=adaptive-alpha",
                "ALPHA_SCENARIOS=default",
            ),
            "live-xpra-opengl-hardware": (
                "APPLICATION=opengl",
                "LIFECYCLE=application-exit",
                "ENCODING=h264",
                "H264_CLIENT_POLICY=adaptive-alpha",
                "ALPHA_SCENARIOS=default",
            ),
        }
        for target, values in expected.items():
            with self.subTest(target=target):
                recipe = makefile.split(f"{target}:", 1)[1].split("\n\n", 1)[0]
                for value in values:
                    self.assertIn(value, recipe)

        self.assertIn(
            "live-start: isolated-start-check selector-check run-name-check live-options-check",
            makefile,
        )
        self.assertIn('--selection "$${XPRA_FORK_SELECTOR}"', makefile)
        options_check = makefile.split("live-options-check:\n", 1)[1].split(
            "\n\n", 1
        )[0]
        self.assertIn('--selection "$${XPRA_FORK_SELECTOR}"', options_check)
        self.assertNotIn("live-start: optional-selector-check", makefile)
        clipboard_policy = makefile.split(
            "live-x11-clipboard-policy-check:\n", 1
        )[1].split("\n\n", 1)[0]
        self.assertIn(
            '"$${XPRA_FORK_CASE}" = x11-client-clipboard-events',
            clipboard_policy,
        )
        self.assertIn('test -z "$${XPRA_FORK_STACK}"', clipboard_policy)
        subsurface_policy = makefile.split(
            "live-wayland-subsurface-policy-check:\n", 1
        )[1].split("\n\n", 1)[0]
        self.assertIn(
            '"$${XPRA_FORK_CASE}" = wayland-subsurface-stream-ownership',
            subsurface_policy,
        )
        self.assertIn('test -z "$${XPRA_FORK_STACK}"', subsurface_policy)

    def test_runner_and_input_freeze_reject_a_clean_server_selection(self) -> None:
        with self.assertRaisesRegex(live_run.LabFailure, "requires one non-empty"):
            live_run.freeze_owned_inputs(
                Path("/unreachable"),
                Path("/unreachable"),
                application="zed",
                selection_name=None,
                zed_directory=None,
            )
        with (
            patch.object(
                sys,
                "argv",
                ["run.py", "--encoding", "rgb", "--alpha-scenarios", "default"],
            ),
            patch.object(sys, "stderr", StringIO()),
            patch.object(live_run.PIL, "__version__", live_run.EXPECTED_PILLOW_VERSION),
            self.assertRaises(SystemExit) as clean_runner_exit,
        ):
            live_run.main()
        self.assertEqual(clean_runner_exit.exception.code, 2)

    def test_runner_rejects_the_wrong_clipboard_selection(self) -> None:
        for selection in (
            "stacks/develop",
            "cases/wayland-client-keymap-sync",
        ):
            with (
                self.subTest(selection=selection),
                patch.object(
                    sys,
                    "argv",
                    [
                        "run.py",
                        "--application",
                        "clipboard",
                        "--selection",
                        selection,
                    ],
                ),
                patch.object(
                    live_run.PIL,
                    "__version__",
                    live_run.EXPECTED_PILLOW_VERSION,
                ),
                self.assertRaisesRegex(
                    live_run.LabFailure,
                    "clipboard live acceptance requires selection",
                ),
            ):
                live_run.main()

    def test_runner_rejects_the_wrong_subsurface_selection(self) -> None:
        for selection in ("stacks/develop", "cases/wayland-initial-window-state"):
            with (
                self.subTest(selection=selection),
                patch.object(
                    sys,
                    "argv",
                    [
                        "run.py",
                        "--application",
                        "subsurface",
                        "--selection",
                        selection,
                    ],
                ),
                patch.object(
                    live_run.PIL,
                    "__version__",
                    live_run.EXPECTED_PILLOW_VERSION,
                ),
                self.assertRaisesRegex(
                    live_run.LabFailure,
                    "subsurface live acceptance requires selection",
                ),
            ):
                live_run.main()

    def test_bound_runner_rejects_an_undeclared_frozen_case_gate(self) -> None:
        selection_name = "cases/video-pipeline-cleanup-race"
        selected = live_run.PatchSelection(
            case_slugs=("video-pipeline-cleanup-race",),
            digest="a" * 64,
            kind="case",
            name=selection_name,
            patches=(),
            required_gates=(),
            selector_digests=((selection_name, "a" * 64),),
            selectors=(selection_name,),
        )
        bound = Mock(
            client_context=Mock(),
            client_selection=None,
            input_manifest_sha256="b" * 64,
            input_tree_sha256="c" * 64,
            keyboard_scenario=None,
            keyboard_scenario_sha256=None,
            server_context=Mock(selection=selected),
            snapshot=Mock(),
            zed_archive=None,
            zed_archive_sha256=None,
            zed_binary_sha256=None,
        )
        with tempfile.TemporaryDirectory() as raw:
            state_root = Path(raw) / "state"
            run_id = "bound-undeclared-gate"
            result = state_root / "live-results" / run_id
            inputs = result / "inputs"
            inputs.mkdir(parents=True)
            for path in (state_root, result.parent, result, inputs):
                path.chmod(0o700)
            argv = [
                "run.py",
                "--application",
                "hardware",
                "--encoding",
                "h264",
                "--h264-client-policy",
                "adaptive-alpha",
                "--selection",
                selection_name,
                "--state-root",
                str(state_root),
                "--run-id",
                run_id,
                "--render-node",
                "/dev/null",
                "--bound-inputs",
                str(inputs),
                "--bound-input-manifest-sha256",
                "b" * 64,
                "--bound-input-tree-sha256",
                "c" * 64,
            ]
            with (
                patch.object(sys, "argv", argv),
                patch.object(
                    live_run.PIL,
                    "__version__",
                    live_run.EXPECTED_PILLOW_VERSION,
                ),
                patch.object(live_run.shutil, "which", return_value="/usr/bin/podman"),
                patch.object(live_run, "load_bound_inputs", return_value=bound) as load,
                patch.object(
                    live_run,
                    "resolve_patch_selection",
                    side_effect=AssertionError("mutable host selection was consulted"),
                ) as resolve,
                patch.object(live_run, "run") as podman_run,
                self.assertRaisesRegex(
                    live_run.LabFailure,
                    "does not declare required gate live-wayland-h264-hardware",
                ),
            ):
                live_run.main()

            load.assert_called_once_with(
                inputs,
                expected_manifest_sha256="b" * 64,
                expected_tree_sha256="c" * 64,
            )
            resolve.assert_not_called()
            podman_run.assert_not_called()

    def test_zed_h264_stimulus_retries_until_sustained_positive_production(
        self,
    ) -> None:
        commands: list[list[str]] = []
        captures: list[str] = []

        def execute(_container: str, argv: list[str], **_kwargs: object):
            commands.append(argv)
            return completed(argv)

        def capture(
            _container: str,
            _directory: Path,
            destination: str,
            **_kwargs: object,
        ) -> None:
            captures.append(destination)

        def converted(_directory: Path, stem: str) -> dict[str, object]:
            return {"image": {"rgb_sha256": stem}}

        baseline = {"updates": [{"sequence": 9}]}
        first = {"updates": [{"sequence": value} for value in range(1, 16)]}
        second = {"updates": [{"sequence": value} for value in range(1, 30)]}
        insufficient = {
            "aggregate_encoded_pixels": 100,
            "h264_damage_span_ms": 500,
            "h264_main_frame_count": 5,
            "h264_main_pixels": 99,
            "minimum_frame_h264_pixels": 99,
            "minimum_frame_window_pixels": 100,
        }
        sufficient = {
            **insufficient,
            "h264_damage_span_ms": 1125,
            "h264_main_frame_count": 10,
        }
        metric_inputs: list[dict[str, object]] = []

        def metrics(_application: str, updates: dict[str, object]):
            metric_inputs.append(updates)
            return insufficient if len(metric_inputs) == 1 else sufficient

        with (
            tempfile.TemporaryDirectory() as raw,
            patch.object(live_run, "podman_exec", side_effect=execute),
            patch.object(live_run, "capture_xwd", side_effect=capture),
            patch.object(live_run, "convert_xwd", side_effect=converted),
            patch.object(live_run.time, "sleep"),
            patch.object(
                live_run,
                "synchronize_saved_updates",
                side_effect=(baseline, first, second),
            ),
            patch.object(
                live_run,
                "h264_production_metrics",
                side_effect=metrics,
            ),
        ):
            evidence = live_run.exercise_zed_h264_stability(
                "server",
                "client",
                "4194320",
                1,
                {"width": 1596, "height": 1173},
                Path(raw),
                {
                    "target": {
                        "system_bounds": [100, 100, 160, 130],
                        "dark_bounds": [40, 100, 100, 130],
                    }
                },
            )

        self.assertEqual(evidence["baseline_sequence"], 9)
        self.assertEqual(evidence["last_sequence"], 29)
        self.assertEqual(evidence["window_size"], [1596, 1173])
        self.assertEqual(len(evidence["attempts"]), 2)
        self.assertTrue(all(evidence["dominance_checks"].values()))
        self.assertEqual(
            [value["h264_stimulus"] for value in metric_inputs],
            [
                {
                    "baseline_sequence": 9,
                    "last_sequence": 15,
                    "window_size": [1596, 1173],
                },
                {
                    "baseline_sequence": 9,
                    "last_sequence": 29,
                    "window_size": [1596, 1173],
                },
            ],
        )
        toggle_commands = [command for command in commands if "mousemove" in command]
        self.assertEqual(
            len(toggle_commands),
            2 * 2 * live_run.ZED_THEME_TOGGLE_CYCLES,
        )
        self.assertIn("window-h264-theme-baseline.xwd", captures)

    def test_saved_update_sync_separates_window_metadata_from_packets(self) -> None:
        listed = (
            "screen-updates/1/window.info",
            "screen-updates/1/100/0.info",
        )
        pulls: list[tuple[str, ...]] = []

        def pull(
            _container: str,
            directory: Path,
            relatives: tuple[str, ...],
        ) -> None:
            pulls.append(relatives)
            for relative in relatives:
                path = directory / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                if relative.endswith("window.info"):
                    path.write_text(
                        json.dumps({"pixel-format": "RGBX"}),
                        encoding="utf-8",
                    )
                elif relative.endswith(".info"):
                    path.write_text(
                        json.dumps(
                            {
                                "encoding": "h264",
                                "file": "0.h264",
                                "h": 64,
                                "options": {
                                    "flush": 0,
                                    "frame": 0,
                                    "type": "IDR",
                                    "window-size": [64, 64],
                                },
                                "sequence": 1,
                                "w": 64,
                                "x": 0,
                                "y": 0,
                            }
                        ),
                        encoding="utf-8",
                    )
                else:
                    path.write_bytes(b"h264")

        with (
            tempfile.TemporaryDirectory() as raw,
            patch.object(live_run, "container_artifact_files", return_value=listed),
            patch.object(live_run, "pull_container_artifacts", side_effect=pull),
        ):
            updates = live_run.synchronize_saved_updates(
                "server",
                Path(raw),
                1,
            )
        self.assertEqual(updates["count"], 1)
        self.assertEqual(updates["initial_pixel_format"], "RGBX")
        self.assertEqual(
            pulls,
            [
                listed,
                ("screen-updates/1/100/0.h264",),
            ],
        )

    def test_lifecycle_scenarios_do_not_inherit_alpha_matrix(self) -> None:
        self.assertEqual(
            live_run.scenario_specs(alpha_scenarios="default", lifecycle="detach"),
            (("detach", False),),
        )
        self.assertEqual(
            live_run.scenario_specs(
                alpha_scenarios="default", lifecycle="transport-loss"
            ),
            (("transport-loss", False),),
        )

    def test_lifecycle_boundaries_fail_closed(self) -> None:
        identity = interaction_identity()
        server = server_identity()
        detach = {
            "application_exited_after_termination": True,
            "application_identity_after_detach": copy.deepcopy(identity),
            "application_identity_at_capture": copy.deepcopy(identity),
            "application_identity_before_termination": copy.deepcopy(identity),
            "application_survived_detach": True,
            "application_termination": {
                "identity": copy.deepcopy(identity),
                "pidfd": True,
                "returncode": 0,
                "server_identity": copy.deepcopy(server),
                "server_pidfd": True,
                "signal": "SIGTERM",
            },
            "client_exit_status": 0,
            "client_exited_after_detach": True,
            "detach_returncode": 0,
            "server_alive_before_application_termination": True,
            "server_exited_after_application": True,
            "server_identity_after_detach": copy.deepcopy(server),
            "server_identity_at_capture": copy.deepcopy(server),
            "server_identity_before_application_termination": copy.deepcopy(server),
            "server_pid": server["pid"],
            "server_survived_detach": True,
        }
        self.assertTrue(
            all(live_run.lifecycle_boundary_checks("detach", detach).values())
        )
        detach["application_survived_detach"] = False
        self.assertFalse(
            live_run.lifecycle_boundary_checks("detach", detach)[
                "application_survived_detach"
            ]
        )
        detach["application_survived_detach"] = True
        transport = {
            "application_exited_after_termination": True,
            "application_identity_after_transport_loss": copy.deepcopy(identity),
            "application_identity_at_capture": copy.deepcopy(identity),
            "application_identity_before_termination": copy.deepcopy(identity),
            "application_survived_transport_loss": True,
            "application_termination": {
                "identity": copy.deepcopy(identity),
                "pidfd": True,
                "returncode": 0,
                "server_identity": copy.deepcopy(server),
                "server_pidfd": True,
                "signal": "SIGTERM",
            },
            "client_exit_status": 1,
            "client_exited_after_transport_loss": True,
            "server_alive_before_application_termination": True,
            "server_exited_after_application": True,
            "server_identity_after_transport_loss": copy.deepcopy(server),
            "server_identity_at_capture": copy.deepcopy(server),
            "server_identity_before_application_termination": copy.deepcopy(server),
            "server_pid": server["pid"],
            "server_survived_transport_loss": True,
            "transport_disconnect_returncode": 0,
        }
        self.assertTrue(
            all(
                live_run.lifecycle_boundary_checks("transport-loss", transport).values()
            )
        )
        for mutation in ("missing", "changed"):
            with self.subTest(transport_server_identity=mutation):
                candidate = copy.deepcopy(transport)
                if mutation == "missing":
                    del candidate["server_identity_after_transport_loss"]
                else:
                    candidate["server_identity_after_transport_loss"][
                        "start_ticks"
                    ] = "765432"
                self.assertFalse(
                    all(
                        live_run.lifecycle_boundary_checks(
                            "transport-loss",
                            candidate,
                        ).values()
                    )
                )
        transport["client_exit_status"] = 0
        self.assertFalse(
            live_run.lifecycle_boundary_checks("transport-loss", transport)[
                "client_exit_nonzero"
            ]
        )
        transport["client_exit_status"] = 1
        transport["transport_disconnect_returncode"] = False
        self.assertFalse(
            live_run.lifecycle_boundary_checks("transport-loss", transport)[
                "transport_disconnect_succeeded"
            ]
        )
        self.assertFalse(
            live_run.lifecycle_boundary_checks(
                "application-exit",
                {
                    "client_exit_status": False,
                    "client_exited_after_server": True,
                    "server_exited_after_application": True,
                },
            )["client_exit_zero"]
        )

    def test_lifecycle_rejects_the_old_server_argv_false_positive(self) -> None:
        old_false_positive = {
            "application_survived_detach": True,
            "client_exit_status": 0,
            "client_exited_after_detach": True,
            "detach_returncode": 0,
            "server_exited_after_application": True,
            "server_survived_detach": True,
        }
        checks = live_run.lifecycle_boundary_checks("detach", old_false_positive)
        self.assertFalse(checks["fixture_identity_published"])
        self.assertFalse(checks["application_survived_detach"])
        self.assertFalse(all(checks.values()))
        forged_report = {
            "classification": {
                "boundaries": {
                    "lifecycle": dict.fromkeys(checks, True),
                },
                "first_failed_boundary": "passed",
            },
            "container_artifact_collection": [{"status": "collected"}],
            "lifecycle": old_false_positive,
            "lifecycle_profile": "detach",
        }
        self.assertFalse(live_run.scenario_acceptance(forged_report, {"passed": True}))

        server_argv = {
            **interaction_identity(pid=8),
            "argv": [
                "/usr/bin/python3",
                "/usr/local/bin/xpra",
                "seamless",
                "--start-child=python3 " + live_run.INTERACTION_FIXTURE_SCRIPT,
            ],
        }
        self.assertFalse(live_run.valid_interaction_fixture_identity(server_argv))

    def test_lifecycle_fixture_identity_fields_fail_closed_independently(self) -> None:
        identity = interaction_identity()
        server = server_identity()
        lifecycle = {
            "application_exited_after_termination": True,
            "application_identity_after_detach": copy.deepcopy(identity),
            "application_identity_at_capture": copy.deepcopy(identity),
            "application_identity_before_termination": copy.deepcopy(identity),
            "application_survived_detach": True,
            "application_termination": {
                "identity": copy.deepcopy(identity),
                "pidfd": True,
                "returncode": 0,
                "server_identity": copy.deepcopy(server),
                "server_pidfd": True,
                "signal": "SIGTERM",
            },
            "client_exit_status": 0,
            "client_exited_after_detach": True,
            "detach_returncode": 0,
            "server_alive_before_application_termination": True,
            "server_exited_after_application": True,
            "server_identity_after_detach": copy.deepcopy(server),
            "server_identity_at_capture": copy.deepcopy(server),
            "server_identity_before_application_termination": copy.deepcopy(server),
            "server_pid": server["pid"],
            "server_survived_detach": True,
        }
        self.assertTrue(all(live_run.lifecycle_boundary_checks("detach", lifecycle).values()))
        required = (
            "application_exited_after_termination",
            "application_identity_after_detach",
            "application_identity_at_capture",
            "application_identity_before_termination",
            "application_termination",
            "server_alive_before_application_termination",
            "server_identity_after_detach",
            "server_identity_at_capture",
            "server_identity_before_application_termination",
            "server_pid",
        )
        for field in required:
            with self.subTest(field=field):
                candidate = copy.deepcopy(lifecycle)
                del candidate[field]
                self.assertFalse(
                    all(live_run.lifecycle_boundary_checks("detach", candidate).values())
                )
        mutations = (
            ("application_identity_after_detach", "start_ticks", "654321"),
            ("application_identity_at_capture", "cmdline_sha256", "b" * 64),
            ("application_identity_before_termination", "pid", 42),
            ("server_identity_after_detach", "start_ticks", "765432"),
            ("server_identity_at_capture", "cmdline_sha256", "c" * 64),
            ("server_identity_before_application_termination", "pid", 9),
        )
        for field, identity_field, replacement in mutations:
            with self.subTest(field=field, identity_field=identity_field):
                candidate = copy.deepcopy(lifecycle)
                candidate[field][identity_field] = replacement
                self.assertFalse(
                    all(live_run.lifecycle_boundary_checks("detach", candidate).values())
                )
        aliases_server = copy.deepcopy(lifecycle)
        aliases_server["server_pid"] = identity["pid"]
        self.assertFalse(
            live_run.lifecycle_boundary_checks("detach", aliases_server)[
                "fixture_distinct_from_server"
            ]
        )
        for field, replacement in (
            ("identity", interaction_identity(pid=42)),
            ("pidfd", False),
            ("returncode", False),
            ("returncode", 1),
            ("server_identity", server_identity(pid=9)),
            ("server_pidfd", False),
            ("signal", "SIGKILL"),
        ):
            with self.subTest(termination_field=field):
                candidate = copy.deepcopy(lifecycle)
                candidate["application_termination"][field] = replacement
                self.assertFalse(
                    live_run.lifecycle_boundary_checks("detach", candidate)[
                        "exact_fixture_termination"
                    ]
                )
        for field in ("server_identity", "server_pidfd"):
            with self.subTest(missing_termination_field=field):
                candidate = copy.deepcopy(lifecycle)
                del candidate["application_termination"][field]
                self.assertFalse(
                    live_run.lifecycle_boundary_checks("detach", candidate)[
                        "exact_fixture_termination"
                    ]
                )

        for field in ("client_exit_status", "detach_returncode"):
            with self.subTest(boolean_integer_field=field):
                candidate = copy.deepcopy(lifecycle)
                candidate[field] = False
                self.assertFalse(
                    all(live_run.lifecycle_boundary_checks("detach", candidate).values())
                )

        boolean_schema = copy.deepcopy(lifecycle)
        boolean_schema["application_identity_at_capture"]["schema"] = True
        self.assertFalse(
            live_run.lifecycle_boundary_checks("detach", boolean_schema)[
                "fixture_identity_published"
            ]
        )

    def test_interaction_fixture_identity_loader_is_exact(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / live_run.INTERACTION_IDENTITY_ARTIFACT
            path.write_text(json.dumps(interaction_identity()) + "\n", encoding="utf-8")
            path.chmod(0o600)
            self.assertEqual(
                live_run.load_interaction_fixture_identity(path),
                interaction_identity(),
            )
            server_identity = interaction_identity(pid=8)
            server_identity["argv"] = [
                "/usr/bin/python3",
                "/usr/local/bin/xpra",
                "--start-child=python3 " + live_run.INTERACTION_FIXTURE_SCRIPT,
            ]
            path.write_text(json.dumps(server_identity) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(live_run.LabFailure, "invalid fields"):
                live_run.load_interaction_fixture_identity(path)

    def test_collector_binds_gtk_lifecycle_to_authority_artifacts(self) -> None:
        identity = interaction_identity()
        server = server_identity()
        with tempfile.TemporaryDirectory() as raw:
            scenario_root = Path(raw)
            identity_path = scenario_root / live_run.INTERACTION_IDENTITY_ARTIFACT
            identity_path.write_text(
                json.dumps(identity, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            identity_path.chmod(0o600)
            server_pid_path = scenario_root / "server.pid"
            server_pid_path.write_text("8\n", encoding="ascii")
            server_pid_path.chmod(0o600)
            embedded = {
                "application_activity": {
                    "process_alive": True,
                    "process_identity": copy.deepcopy(identity),
                },
                "hardware": {
                    "application": {
                        "argv": " ".join(identity["argv"]) + " ",
                        "pid": identity["pid"],
                    }
                },
                "lifecycle": {
                    "application_identity_at_capture": copy.deepcopy(identity),
                    "server_identity_at_capture": copy.deepcopy(server),
                    "server_pid": server["pid"],
                },
            }
            self.assertTrue(
                job.gtk_fixture_artifact_evidence_matches(
                    embedded,
                    scenario_root,
                    live_run,
                )
            )

            for field in ("application_activity", "hardware", "lifecycle"):
                with self.subTest(missing_report_field=field):
                    candidate = copy.deepcopy(embedded)
                    del candidate[field]
                    self.assertFalse(
                        job.gtk_fixture_artifact_evidence_matches(
                            candidate,
                            scenario_root,
                            live_run,
                        )
                    )

            for container_field, identity_field in (
                ("application_activity", "process_identity"),
                ("lifecycle", "application_identity_at_capture"),
                ("lifecycle", "server_identity_at_capture"),
            ):
                with self.subTest(mismatched_report_identity=container_field):
                    candidate = copy.deepcopy(embedded)
                    candidate[container_field][identity_field] = (
                        server_identity(pid=9)
                        if identity_field == "server_identity_at_capture"
                        else interaction_identity(pid=42)
                    )
                    self.assertFalse(
                        job.gtk_fixture_artifact_evidence_matches(
                            candidate,
                            scenario_root,
                            live_run,
                        )
                    )

            activity_not_alive = copy.deepcopy(embedded)
            activity_not_alive["application_activity"]["process_alive"] = False
            self.assertFalse(
                job.gtk_fixture_artifact_evidence_matches(
                    activity_not_alive,
                    scenario_root,
                    live_run,
                )
            )
            for field, replacement in (
                ("pid", 42),
                ("argv", " ".join(identity["argv"])),
            ):
                with self.subTest(hardware_application_field=field):
                    candidate = copy.deepcopy(embedded)
                    candidate["hardware"]["application"][field] = replacement
                    self.assertFalse(
                        job.gtk_fixture_artifact_evidence_matches(
                            candidate,
                            scenario_root,
                            live_run,
                        )
                    )

            different = interaction_identity(pid=42)
            identity_path.write_text(
                json.dumps(different, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            self.assertFalse(
                job.gtk_fixture_artifact_evidence_matches(
                    embedded,
                    scenario_root,
                    live_run,
                )
            )
            identity_path.unlink()
            self.assertFalse(
                job.gtk_fixture_artifact_evidence_matches(
                    embedded,
                    scenario_root,
                    live_run,
                )
            )

            identity_path.write_text(
                json.dumps(identity, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            identity_path.chmod(0o600)
            server_pid_path.write_text("9\n", encoding="ascii")
            self.assertFalse(
                job.gtk_fixture_artifact_evidence_matches(
                    embedded,
                    scenario_root,
                    live_run,
                )
            )

    def test_interaction_fixture_rejects_a_stale_or_reused_pid(self) -> None:
        expected = interaction_identity()
        reused = {**expected, "start_ticks": "654321"}
        with patch.object(
            live_run,
            "container_process_identity",
            return_value=reused,
        ):
            with self.assertRaisesRegex(live_run.LabFailure, "identity changed"):
                live_run.require_interaction_fixture_identity(
                    "server",
                    expected,
                    server_pid=8,
                )
            with self.assertRaisesRegex(live_run.LabFailure, "PID was reused"):
                live_run.interaction_fixture_identity_is_gone("server", expected)
        with self.assertRaisesRegex(live_run.LabFailure, "aliases the Xpra server"):
            live_run.require_interaction_fixture_identity(
                "server",
                expected,
                server_pid=expected["pid"],
            )

    def test_fixture_termination_binds_two_pidfds_in_one_probe(self) -> None:
        fixture = interaction_identity()
        server = server_identity()
        with (
            patch.object(
                live_run,
                "require_interaction_fixture_identity",
                return_value=fixture,
            ),
            patch.object(
                live_run,
                "podman_exec",
                return_value=completed(["python3", "-c", "probe"]),
            ) as execute,
        ):
            evidence = live_run.terminate_interaction_fixture(
                "server",
                fixture,
                server_identity=server,
            )

        command = execute.call_args.args[1]
        probe = command[2]
        self.assertEqual(
            command[3:],
            [
                str(fixture["pid"]),
                fixture["start_ticks"],
                fixture["cmdline_sha256"],
                json.dumps(fixture["argv"]),
                str(server["pid"]),
                server["start_ticks"],
                server["cmdline_sha256"],
                json.dumps(server["argv"]),
            ],
        )
        self.assertIn("descriptor = os.pidfd_open(pid)", probe)
        self.assertIn("server_descriptor = os.pidfd_open(server_pid)", probe)
        self.assertGreaterEqual(probe.count("server_poller.poll(0)"), 2)
        self.assertLess(
            probe.index("server_descriptor = os.pidfd_open(server_pid)"),
            probe.index("signal.pidfd_send_signal(descriptor, signal.SIGTERM)"),
        )
        self.assertEqual(
            evidence,
            {
                "identity": fixture,
                "pidfd": True,
                "returncode": 0,
                "server_identity": server,
                "server_pidfd": True,
                "signal": "SIGTERM",
            },
        )

    def test_zombie_fixture_identity_is_treated_as_exited(self) -> None:
        probe_result = completed(["python3", "-c", "probe"], returncode=3)
        with patch.object(live_run, "podman_exec", return_value=probe_result) as execute:
            self.assertIsNone(live_run.container_process_identity("server", 41))
        probe = execute.call_args.args[1][2]
        self.assertIn("descriptor = os.pidfd_open(pid)", probe)
        self.assertIn("poller.register(descriptor, select.POLLIN)", probe)
        self.assertIn("before[0].casefold() in {'x', 'z'}", probe)
        self.assertIn("after[0].casefold() in {'x', 'z'}", probe)
        self.assertIn("stat_before", probe)
        self.assertIn("stat_after", probe)
        self.assertIn("for attempt in range(5)", probe)
        with patch.object(
            live_run,
            "container_process_identity",
            return_value=None,
        ):
            self.assertTrue(
                live_run.interaction_fixture_identity_is_gone(
                    "server",
                    interaction_identity(),
                )
            )

    def test_server_window_id_is_title_bound(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            info = Path(raw) / "server-info.txt"
            info.write_text(
                "windows.1.title=vkcube\n"
                "windows.2.title=Xpra Hardware Interaction Ready on server\n",
                encoding="utf-8",
            )
            self.assertEqual(live_run.server_xpra_window_id(info, ("vkcube",)), 1)
            with self.assertRaisesRegex(live_run.LabFailure, "identified 0"):
                live_run.server_xpra_window_id(info, ("missing",))
            self.assertEqual(
                live_run.server_xpra_window_id(
                    info, ("xpra hardware interaction ready",)
                ),
                2,
            )
            self.assertEqual(
                live_run.server_xpra_window_inventory(info),
                {
                    1: "vkcube",
                    2: "Xpra Hardware Interaction Ready on server",
                },
            )

    def passing_keyboard_evidence(
        self,
    ) -> tuple[dict[str, object], dict[str, object], str]:
        scenario_path = (
            LIVE_DIRECTORY.parents[1]
            / "cases"
            / "wayland-client-keymap-sync"
            / "tests"
            / live_run.KEYBOARD_SCENARIO_BASENAME
        )
        scenario = live_run.load_keyboard_scenario(scenario_path)
        scenario_sha256 = live_run.sha256_file(scenario_path)
        window_id = 4194305
        physical_keycode = 24
        symbol_values = {
            "q": (113, "q", "q", "q"),
            "a": (97, "a", "a", "a"),
            "й": (1738, "Cyrillic_shorti", "Cyrillic_shorti", "Cyrillic_shorti"),
            "ض": (1494, "Arabic_dad", "Arabic_dad", "Arabic_dad"),
            "ქ": (0x10010E5, "Georgian_khar", "Q", "U+10E5"),
            "ճ": (0x1000573, "Armenian_tche", "Q", "U+0573"),
        }
        cumulative = ""
        phases = []
        events: list[dict[str, object]] = [
            {
                "backend": "wayland-0",
                "event": "ready",
                "monotonic_ns": 1000,
                "schema": 1,
                "sequence": 0,
                "text": "",
                "title": live_run.KEYBOARD_FIXTURE_TITLE,
            }
        ]
        sequence = 1
        offset = 100
        for phase in scenario["phases"]:
            phase_inputs = []
            phase_start = offset
            offset += 100
            phase_end = offset
            for item in phase["inputs"]:
                before = cumulative
                cumulative += item["expected_text"]
                keysym, driver_keyname, packet_keyname, fixture_keyname = symbol_values[
                    item["expected_text"]
                ]
                start = offset
                offset += 100
                trace = {
                    "device": [
                        {"keycode": physical_keycode, "pressed": True},
                        {"keycode": physical_keycode, "pressed": False},
                    ],
                    "resolutions": [
                        {
                            "client_group": item["group"],
                            "client_keycode": physical_keycode,
                            "keyname": packet_keyname,
                            "keyval": keysym,
                            "pressed": True,
                            "server_group": item["group"],
                            "server_keycode": physical_keycode,
                        },
                        {
                            "client_group": item["group"],
                            "client_keycode": physical_keycode,
                            "keyname": packet_keyname,
                            "keyval": keysym,
                            "pressed": False,
                            "server_group": item["group"],
                            "server_keycode": physical_keycode,
                        },
                    ],
                    "sha256": "a" * 64,
                }
                phase_inputs.append(
                    {
                        "application_text": cumulative,
                        "client": {
                            "display": live_run.CLIENT_DISPLAY,
                            "focus_before": window_id,
                            "group_after": item["group"],
                            "group_before": item["group"],
                            "group_requested": item["group"],
                            "keysym": keysym,
                            "keysym_name": driver_keyname,
                            "physical_keycode": physical_keycode,
                            "press": True,
                            "release": True,
                            "schema": 1,
                            "window": window_id,
                        },
                        "client_log_range": [start, offset],
                        "client_trace": {
                            "events": [
                                {
                                    "group": item["group"],
                                    "keycode": physical_keycode,
                                    "keyname": packet_keyname,
                                    "keysym": keysym,
                                    "modifiers": [],
                                    "pressed": pressed,
                                    "string": item["expected_text"],
                                    "window": 1,
                                }
                                for pressed in (True, False)
                            ],
                            "sha256": "b" * 64,
                        },
                        "expected_text": item["expected_text"],
                        "group": item["group"],
                        "server_log_range": [start, offset],
                        "server_trace": trace,
                    }
                )
                for event, text in (
                    ("key-press", before),
                    ("changed", cumulative),
                    ("key-release", cumulative),
                ):
                    record: dict[str, object] = {
                        "event": event,
                        "monotonic_ns": 1000 + sequence,
                        "schema": 1,
                        "sequence": sequence,
                        "text": text,
                    }
                    if event != "changed":
                        record.update(
                            {
                                "hardware_keycode": physical_keycode,
                                "keyname": fixture_keyname,
                                "keyval": keysym,
                            }
                        )
                    events.append(record)
                    sequence += 1
            rmlvo_hash = live_run.keyboard_rmlvo_hash(phase["rmlvo"])
            phases.append(
                {
                    "client_query": phase["rmlvo"],
                    "inputs": phase_inputs,
                    "name": phase["name"],
                    "rmlvo": phase["rmlvo"],
                    "rmlvo_hash": rmlvo_hash,
                    "server_application": {
                        "group_count": len(phase["rmlvo"]["layouts"]),
                        "hash": rmlvo_hash,
                        "log_range": [phase_start, phase_end],
                        "owner": "keyboard-client-uuid",
                        "sha256": "f" * 64,
                    },
                    "server_info_artifact": f"server-info-keyboard-{phase['name']}.txt",
                    "server_info": {
                        "compiled_groups": len(phase["rmlvo"]["layouts"]),
                        "current_group": phase["inputs"][-1]["group"],
                        "effective_rmlvo": phase["rmlvo"],
                        "layout_groups": True,
                        "owner": "keyboard-client-uuid",
                        "rejected_configuration": False,
                    },
                    "server_info_sha256": "c" * 64,
                    "structured_update": {
                        "group_count": len(phase["rmlvo"]["layouts"]),
                        "hash": rmlvo_hash,
                        "log_range": [phase_start, phase_end],
                        "owner": "keyboard-client-uuid",
                        "packet": "keymap-changed",
                        "representation": "legacy",
                        "result": "installed",
                        "sha256": "f" * 64,
                    },
                }
            )
        events.append(
            {
                "event": "closed",
                "monotonic_ns": 1000 + sequence,
                "schema": 1,
                "sequence": sequence,
                "text": cumulative,
            }
        )

        def identity(role: str) -> dict[str, object]:
            client = role == "client"
            process = {
                "cmdline_sha256": ("d" if client else "e") * 64,
                "pid": 300 if client else 200,
                "start_ticks": "123456" if client else "654321",
            }
            return {
                "connection": {
                    "family": "tcp4",
                    "inode": 101 if client else 202,
                    "local_address": "0300590A" if client else "0200590A",
                    "local_port": 38000 if client else live_run.SERVER_PORT,
                    "remote_address": "0200590A" if client else "0300590A",
                    "remote_port": live_run.SERVER_PORT if client else 38000,
                    "state": "established",
                },
                "process": process,
            }

        snapshots = [
            {"client": identity("client"), "server": identity("server")}
            for _phase in range(len(phases) + 1)
        ]
        evidence: dict[str, object] = {
            "application": {
                "events": events,
                "exit_status": 0,
                "final_text": cumulative,
                "observed_texts": live_run.keyboard_expected_texts(scenario),
            },
            "identity_snapshots": snapshots,
            "phases": phases,
            "physical_keycode": physical_keycode,
            "runtime_replacement": {
                "after_hash": phases[-1]["rmlvo_hash"],
                "application_observed_after_replacement": True,
                "before_hash": phases[0]["rmlvo_hash"],
                "configuration_changed": True,
                "connection_unchanged": True,
                "processes_unchanged": True,
            },
            "scenario": {"data": scenario, "sha256": scenario_sha256},
            "schema": 1,
            "window_id": window_id,
            "xpra_window_id": 1,
        }
        return evidence, scenario, scenario_sha256

    def test_keyboard_live_report_and_every_required_field_fail_closed(self) -> None:
        evidence, scenario, digest = self.passing_keyboard_evidence()
        checks = live_run.keyboard_live_checks(evidence, scenario, digest)
        self.assertEqual(tuple(checks), live_run.KEYBOARD_LIVE_CHECK_NAMES)
        self.assertTrue(all(checks.values()), checks)

        dictionary_paths: list[tuple[object, ...]] = []

        def collect(value: object, path: tuple[object, ...] = ()) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    dictionary_paths.append((*path, key))
                    collect(child, (*path, key))
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    collect(child, (*path, index))

        def parent_at(value: object, path: tuple[object, ...]) -> object:
            current = value
            for component in path:
                current = current[component]  # type: ignore[index]
            return current

        collect(evidence)
        for path in dictionary_paths:
            with self.subTest(missing="/".join(map(str, path))):
                candidate = copy.deepcopy(evidence)
                parent = parent_at(candidate, path[:-1])
                assert isinstance(parent, dict)
                parent.pop(path[-1])
                mutated = live_run.keyboard_live_checks(candidate, scenario, digest)
                self.assertFalse(all(mutated.values()), mutated)

        packet_only = copy.deepcopy(evidence)
        packet_only["application"] = {
            "events": [],
            "exit_status": 0,
            "final_text": "",
            "observed_texts": [],
        }
        packet_checks = live_run.keyboard_live_checks(packet_only, scenario, digest)
        self.assertTrue(packet_checks["server_group_translation_exact"])
        self.assertFalse(packet_checks["application_text_authoritative"])
        self.assertFalse(all(packet_checks.values()))

        rejected_after_layout = copy.deepcopy(evidence)
        rejected_after_layout["phases"][0]["structured_update"]["result"] = "rejected"
        rejected_after_layout["phases"][0]["server_info"][
            "rejected_configuration"
        ] = True
        rejected_checks = live_run.keyboard_live_checks(
            rejected_after_layout, scenario, digest
        )
        self.assertTrue(rejected_checks["server_keymaps_applied"])
        self.assertTrue(rejected_checks["application_text_authoritative"])
        self.assertFalse(rejected_checks["structured_keymap_packet_accepted"])
        self.assertFalse(rejected_checks["no_rejected_configuration"])
        self.assertFalse(all(rejected_checks.values()))

        identical_only = copy.deepcopy(evidence)
        identical_only["phases"][0]["structured_update"]["result"] = "identical"
        identical_checks = live_run.keyboard_live_checks(
            identical_only, scenario, digest
        )
        self.assertFalse(identical_checks["structured_keymap_packet_accepted"])
        self.assertFalse(all(identical_checks.values()))

        client_release_missing = copy.deepcopy(evidence)
        client_release_missing["phases"][0]["inputs"][0]["client_trace"][
            "events"
        ].pop()
        client_release_checks = live_run.keyboard_live_checks(
            client_release_missing, scenario, digest
        )
        self.assertFalse(client_release_checks["client_press_release_observed"])
        self.assertFalse(all(client_release_checks.values()))

        client_string_wrong = copy.deepcopy(evidence)
        client_string_wrong["phases"][0]["inputs"][0]["client_trace"]["events"][
            1
        ]["string"] = "a"
        client_string_checks = live_run.keyboard_live_checks(
            client_string_wrong, scenario, digest
        )
        self.assertFalse(client_string_checks["client_press_release_observed"])
        self.assertFalse(all(client_string_checks.values()))

        wrong_xpra_window = copy.deepcopy(evidence)
        wrong_xpra_window["phases"][0]["inputs"][0]["client_trace"]["events"][
            1
        ]["window"] = 2
        wrong_xpra_window_checks = live_run.keyboard_live_checks(
            wrong_xpra_window, scenario, digest
        )
        self.assertFalse(
            wrong_xpra_window_checks["client_press_release_observed"]
        )
        self.assertFalse(all(wrong_xpra_window_checks.values()))

        wrong_key = copy.deepcopy(evidence)
        wrong_key["phases"][0]["inputs"][0]["client_trace"]["events"][0][
            "keycode"
        ] = 25
        wrong_key_checks = live_run.keyboard_live_checks(wrong_key, scenario, digest)
        self.assertFalse(wrong_key_checks["client_press_release_observed"])
        self.assertFalse(all(wrong_key_checks.values()))

        wrong_group = copy.deepcopy(evidence)
        wrong_group["phases"][0]["inputs"][0]["server_trace"]["resolutions"][0][
            "server_group"
        ] = 1
        wrong_group_checks = live_run.keyboard_live_checks(
            wrong_group, scenario, digest
        )
        self.assertFalse(wrong_group_checks["server_group_translation_exact"])
        self.assertFalse(all(wrong_group_checks.values()))

        wrong_server_keyname = copy.deepcopy(evidence)
        for event in wrong_server_keyname["phases"][0]["inputs"][0][
            "server_trace"
        ]["resolutions"]:
            event["keyname"] = "foreign-name"
        wrong_server_keyname_checks = live_run.keyboard_live_checks(
            wrong_server_keyname, scenario, digest
        )
        self.assertFalse(
            wrong_server_keyname_checks["server_group_translation_exact"]
        )
        self.assertFalse(all(wrong_server_keyname_checks.values()))

        unstable_fixture_keyname = copy.deepcopy(evidence)
        unstable_fixture_keyname["application"]["events"][3]["keyname"] = "other"
        unstable_fixture_keyname_checks = live_run.keyboard_live_checks(
            unstable_fixture_keyname, scenario, digest
        )
        self.assertFalse(
            unstable_fixture_keyname_checks["fixture_event_sequence_exact"]
        )
        self.assertFalse(all(unstable_fixture_keyname_checks.values()))

        reconnected = copy.deepcopy(evidence)
        for role in ("client", "server"):
            connection = reconnected["identity_snapshots"][1][role]["connection"]
            connection["inode"] += 1000
        reconnected["identity_snapshots"][1]["client"]["connection"][
            "local_port"
        ] += 1
        reconnected["identity_snapshots"][1]["server"]["connection"][
            "remote_port"
        ] += 1
        reconnect_checks = live_run.keyboard_live_checks(
            reconnected, scenario, digest
        )
        self.assertFalse(reconnect_checks["connection_identity_unchanged"])
        self.assertFalse(reconnect_checks["runtime_replacement_proven"])
        self.assertFalse(all(reconnect_checks.values()))

        absent_replacement = copy.deepcopy(evidence)
        absent_replacement["runtime_replacement"]["configuration_changed"] = False
        replacement_checks = live_run.keyboard_live_checks(
            absent_replacement, scenario, digest
        )
        self.assertFalse(replacement_checks["runtime_replacement_proven"])
        self.assertFalse(all(replacement_checks.values()))

        duplicate_application_event = copy.deepcopy(evidence)
        duplicate_application_event["application"]["events"].insert(
            2,
            copy.deepcopy(duplicate_application_event["application"]["events"][1]),
        )
        duplicate_event_checks = live_run.keyboard_live_checks(
            duplicate_application_event, scenario, digest
        )
        self.assertFalse(duplicate_event_checks["fixture_event_sequence_exact"])
        self.assertFalse(duplicate_event_checks["application_text_authoritative"])
        self.assertFalse(all(duplicate_event_checks.values()))

        stale_client_range = copy.deepcopy(evidence)
        stale_client_range["phases"][1]["inputs"][0]["client_log_range"] = list(
            stale_client_range["phases"][0]["inputs"][0]["client_log_range"]
        )
        stale_client_checks = live_run.keyboard_live_checks(
            stale_client_range, scenario, digest
        )
        self.assertFalse(stale_client_checks["client_press_release_observed"])
        self.assertFalse(all(stale_client_checks.values()))

        stale_application_range = copy.deepcopy(evidence)
        for field in ("server_application", "structured_update"):
            stale_application_range["phases"][1][field]["log_range"] = [50, 75]
        stale_application_checks = live_run.keyboard_live_checks(
            stale_application_range, scenario, digest
        )
        self.assertFalse(stale_application_checks["phase_configurations_bound"])
        self.assertFalse(all(stale_application_checks.values()))

        wrong_effective_group = copy.deepcopy(evidence)
        wrong_effective_group["phases"][0]["server_info"]["current_group"] = 0
        effective_group_checks = live_run.keyboard_live_checks(
            wrong_effective_group, scenario, digest
        )
        self.assertFalse(
            effective_group_checks["server_info_effective_state_exact"]
        )
        self.assertFalse(all(effective_group_checks.values()))

        layout_only = copy.deepcopy(evidence)
        layout_only["phases"][0]["structured_update"]["packet"] = "layout-changed"
        layout_only_checks = live_run.keyboard_live_checks(layout_only, scenario, digest)
        self.assertTrue(layout_only_checks["server_keymaps_applied"])
        self.assertTrue(layout_only_checks["application_text_authoritative"])
        self.assertFalse(layout_only_checks["structured_keymap_packet_accepted"])
        self.assertFalse(all(layout_only_checks.values()))

        finalized = copy.deepcopy(evidence)
        finalized["checks"] = checks
        self.assertTrue(
            live_run.keyboard_embedded_checks_match(finalized, scenario, digest)
        )
        missing_check = copy.deepcopy(finalized)
        missing_check["checks"].pop("structured_keymap_packet_accepted")
        self.assertFalse(
            live_run.keyboard_embedded_checks_match(missing_check, scenario, digest)
        )
        extra_check = copy.deepcopy(finalized)
        extra_check["checks"]["layout_only_is_enough"] = True
        self.assertFalse(
            live_run.keyboard_embedded_checks_match(extra_check, scenario, digest)
        )
        false_check = copy.deepcopy(finalized)
        false_check["checks"]["structured_keymap_packet_accepted"] = False
        self.assertFalse(
            live_run.keyboard_embedded_checks_match(false_check, scenario, digest)
        )
        non_boolean_check = copy.deepcopy(finalized)
        non_boolean_check["checks"]["structured_keymap_packet_accepted"] = 1
        self.assertFalse(
            live_run.keyboard_embedded_checks_match(
                non_boolean_check, scenario, digest
            )
        )

    def test_keyboard_scenario_requires_changed_four_group_output(self) -> None:
        _evidence, scenario, _digest = self.passing_keyboard_evidence()
        self.assertEqual(
            [len(phase["rmlvo"]["layouts"]) for phase in scenario["phases"]],
            [4, 4],
        )
        self.assertEqual(
            [phase["rmlvo"]["model"] for phase in scenario["phases"]],
            ["pc104", "pc105"],
        )
        self.assertEqual(scenario["phases"][0]["inputs"][-1]["expected_text"], "ض")
        self.assertEqual(
            [item["expected_text"] for item in scenario["phases"][1]["inputs"]],
            ["ქ", "ճ", "q", "a"],
        )
        characters = [
            item["expected_text"]
            for phase in scenario["phases"]
            for item in phase["inputs"]
        ]
        self.assertEqual(characters, ["q", "a", "й", "ض", "ქ", "ճ", "q", "a"])
        self.assertEqual(
            {unicodedata.name(character).split()[0] for character in characters},
            {"LATIN", "CYRILLIC", "ARABIC", "GEORGIAN", "ARMENIAN"},
        )
        unchanged = copy.deepcopy(scenario)
        unchanged["phases"][1]["inputs"] = copy.deepcopy(
            unchanged["phases"][0]["inputs"]
        )
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "scenario.json"
            path.write_text(json.dumps(unchanged), encoding="utf-8")
            with self.assertRaisesRegex(live_run.LabFailure, "output is unchanged"):
                live_run.load_keyboard_scenario(path)
            same_model = copy.deepcopy(scenario)
            same_model["phases"][1]["rmlvo"]["model"] = same_model["phases"][0][
                "rmlvo"
            ]["model"]
            path.write_text(json.dumps(same_model), encoding="utf-8")
            with self.assertRaisesRegex(live_run.LabFailure, "distinct models"):
                live_run.load_keyboard_scenario(path)
            boolean_schema = copy.deepcopy(scenario)
            boolean_schema["schema"] = True
            path.write_text(json.dumps(boolean_schema), encoding="utf-8")
            with self.assertRaisesRegex(live_run.LabFailure, "header is invalid"):
                live_run.load_keyboard_scenario(path)

    def test_keyboard_structured_log_and_server_info_fail_closed(self) -> None:
        accepted_log = (
            "applied Wayland keyboard configuration hash="
            + "0" * 64
            + " groups=4 owner=keyboard-client-uuid\n"
            "received Wayland structured keymap packet=keymap-changed\n"
            "applied Wayland keyboard configuration hash="
            + "a" * 64
            + " groups=4 owner=keyboard-client-uuid\n"
            "accepted Wayland structured keymap packet=keymap-changed "
            "representation=legacy hash="
            + "a" * 64
            + " groups=4 owner=keyboard-client-uuid result=installed\n"
        )
        info_text = (
            "keyboard.compiled-groups=4\n"
            "keyboard.current-group=0\n"
            "keyboard.effective-rmlvo.layout-groups=True\n"
            "keyboard.effective-rmlvo.layouts='us', 'fr', 'ru', 'ara'\n"
            "keyboard.effective-rmlvo.model=pc105\n"
            "keyboard.effective-rmlvo.options=\n"
            "keyboard.effective-rmlvo.rules=evdev\n"
            "keyboard.effective-rmlvo.variants='', '', '', ''\n"
            "keyboard.owner=keyboard-client-uuid\n"
        )
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            log_path = directory / "server.stderr"
            log_path.write_text(accepted_log, encoding="utf-8")
            structured = live_run.parse_keyboard_structured_update(
                log_path, 0, log_path.stat().st_size
            )
            self.assertEqual(structured["packet"], "keymap-changed")
            self.assertEqual(structured["representation"], "legacy")
            self.assertEqual(structured["result"], "installed")
            application = live_run.parse_keyboard_server_application(
                log_path, 0, log_path.stat().st_size
            )
            self.assertEqual(application["hash"], "a" * 64)
            silent_log = "received Wayland structured keymap packet=keymap-changed\n"
            log_path.write_text(silent_log, encoding="utf-8")
            with self.assertRaisesRegex(live_run.LabFailure, "receipt and acceptance"):
                live_run.parse_keyboard_structured_update(
                    log_path, 0, log_path.stat().st_size
                )
            rejected_log = accepted_log.replace(
                "accepted Wayland",
                "Warning: rejected Wayland keyboard configuration: invalid layouts\n"
                "accepted Wayland",
            )
            log_path.write_text(rejected_log, encoding="utf-8")
            with self.assertRaisesRegex(live_run.LabFailure, "rejected"):
                live_run.parse_keyboard_structured_update(
                    log_path, 0, log_path.stat().st_size
                )
            log_path.write_text(
                "rejected Wayland keyboard configuration: legacy update\n"
                + accepted_log,
                encoding="utf-8",
            )
            with self.assertRaisesRegex(live_run.LabFailure, "rejected"):
                live_run.parse_keyboard_structured_update(
                    log_path, 0, log_path.stat().st_size
                )

            identical_log = accepted_log.replace(
                "result=installed", "result=identical"
            )
            log_path.write_text(identical_log, encoding="utf-8")
            with self.assertRaisesRegex(live_run.LabFailure, "did not install"):
                live_run.parse_keyboard_structured_update(
                    log_path, 0, log_path.stat().st_size
                )

            ordered_application = (
                "applied Wayland keyboard configuration hash="
                + "a" * 64
                + " groups=4 owner=keyboard-client-uuid\n"
            )
            application_before_receipt = accepted_log.replace(
                "received Wayland structured keymap packet=keymap-changed\n"
                + ordered_application,
                ordered_application
                + "received Wayland structured keymap packet=keymap-changed\n",
            )
            log_path.write_text(application_before_receipt, encoding="utf-8")
            with self.assertRaisesRegex(
                live_run.LabFailure, "ordered keymap application"
            ):
                live_run.parse_keyboard_structured_update(
                    log_path, 0, log_path.stat().st_size
                )

            wrong_application = accepted_log.replace(
                "applied Wayland keyboard configuration hash=" + "a" * 64,
                "applied Wayland keyboard configuration hash=" + "b" * 64,
            )
            log_path.write_text(wrong_application, encoding="utf-8")
            with self.assertRaisesRegex(live_run.LabFailure, "does not match"):
                live_run.parse_keyboard_structured_update(
                    log_path, 0, log_path.stat().st_size
                )

            log_path.write_bytes(accepted_log.encode() + b"\xff")
            with self.assertRaisesRegex(live_run.LabFailure, "not UTF-8"):
                live_run.parse_keyboard_structured_update(
                    log_path, 0, log_path.stat().st_size
                )

            info_path = directory / "server-info.txt"
            info_path.write_text(info_text, encoding="utf-8")
            info_path.chmod(0o600)
            info = live_run.parse_keyboard_server_info(info_path)
            self.assertFalse(info["rejected_configuration"])
            self.assertEqual(
                info["effective_rmlvo"]["layouts"], ["us", "fr", "ru", "ara"]
            )
            info_path.write_text(
                info_text + "keyboard.rejected-configuration.reason=invalid layouts\n",
                encoding="utf-8",
            )
            self.assertTrue(
                live_run.parse_keyboard_server_info(info_path)[
                    "rejected_configuration"
                ]
            )

    def test_keyboard_client_packet_log_is_exact_and_utf8(self) -> None:
        client_log = (
            "2026-09-02 14:20:01,549 "
            "do_send_keyboard('key-action', 1, 'Q', True, [], "
            "16781541, 'ქ', 24, 0)\n"
            "2026-09-02 14:20:01,599 "
            "do_send_keyboard('key-action', 1, 'Q', False, [], "
            "16781541, 'ქ', 24, 0)\n"
        )
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "client.stdout"
            path.write_text(client_log, encoding="utf-8")
            trace = live_run.parse_keyboard_client_trace(path, 0, path.stat().st_size)
            self.assertEqual(
                trace["events"],
                [
                    {
                        "group": 0,
                        "keycode": 24,
                        "keyname": "Q",
                        "keysym": 16781541,
                        "modifiers": [],
                        "pressed": pressed,
                        "string": "ქ",
                        "window": 1,
                    }
                    for pressed in (True, False)
                ],
            )
            self.assertRegex(trace["sha256"], r"^[0-9a-f]{64}$")

            path.write_bytes(client_log.encode("utf-8") + b"\xff")
            with self.assertRaisesRegex(live_run.LabFailure, "not UTF-8"):
                live_run.parse_keyboard_client_trace(path, 0, path.stat().st_size)

            wrong_packet = client_log.replace("'key-action'", "'key-repeat'", 1)
            path.write_text(wrong_packet, encoding="utf-8")
            with self.assertRaisesRegex(live_run.LabFailure, "invalid packet values"):
                live_run.parse_keyboard_client_trace(path, 0, path.stat().st_size)

    def test_keyboard_authority_artifacts_are_reparsed_fail_closed(self) -> None:
        evidence, scenario, digest = self.passing_keyboard_evidence()
        server_payload = bytearray()
        client_payload = bytearray()
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            for phase_index, phase in enumerate(evidence["phases"]):
                rmlvo_hash = phase["rmlvo_hash"]
                owner = phase["server_application"]["owner"]
                groups = phase["server_application"]["group_count"]
                configuration_start = len(server_payload)
                server_payload.extend(
                    (
                        "applied Wayland keyboard configuration hash="
                        + str(phase_index) * 64
                        + f" groups={groups} owner={owner}\n"
                        "received Wayland structured keymap packet=keymap-changed\n"
                        "applied Wayland keyboard configuration hash="
                        + rmlvo_hash
                        + f" groups={groups} owner={owner}\n"
                        "accepted Wayland structured keymap packet=keymap-changed "
                        "representation=legacy hash="
                        + rmlvo_hash
                        + f" groups={groups} owner={owner} result=installed\n"
                    ).encode("utf-8")
                )
                configuration_end = len(server_payload)
                phase["server_application"]["log_range"] = [
                    configuration_start,
                    configuration_end,
                ]
                phase["structured_update"]["log_range"] = [
                    configuration_start,
                    configuration_end,
                ]
                for item in phase["inputs"]:
                    client_start = len(client_payload)
                    for event in item["client_trace"]["events"]:
                        client_payload.extend(
                            (
                                "2026-09-02 14:20:01,000 do_send_keyboard("
                                f"'key-action', {event['window']}, {event['keyname']!r}, "
                                f"{event['pressed']!r}, {event['modifiers']!r}, "
                                f"{event['keysym']}, {event['string']!r}, "
                                f"{event['keycode']}, {event['group']})\n"
                            ).encode()
                        )
                    item["client_log_range"] = [client_start, len(client_payload)]

                    server_start = len(server_payload)
                    for event in item["server_trace"]["resolutions"]:
                        server_payload.extend(
                            (
                                f"get_keycode: pressed={event['pressed']!r} "
                                f"keyname={event['keyname']!r} keyval={event['keyval']} "
                                f"client-keycode={event['client_keycode']} "
                                f"client-group={event['client_group']} -> "
                                f"{event['server_keycode']}/{event['server_group']}\n"
                            ).encode()
                        )
                    for event in item["server_trace"]["device"]:
                        server_payload.extend(
                            (
                                "wlr_seat_keyboard_notify_key(0x1, 1, "
                                f"{event['keycode']}, {int(event['pressed'])})\n"
                            ).encode("ascii")
                        )
                    item["server_log_range"] = [server_start, len(server_payload)]

                rmlvo = phase["rmlvo"]
                info_path = directory / phase["server_info_artifact"]
                info_path.write_text(
                    f"keyboard.compiled-groups={groups}\n"
                    f"keyboard.current-group={phase['server_info']['current_group']}\n"
                    "keyboard.effective-rmlvo.layout-groups=True\n"
                    "keyboard.effective-rmlvo.layouts="
                    + repr(tuple(rmlvo["layouts"]))[1:-1]
                    + "\n"
                    f"keyboard.effective-rmlvo.model={rmlvo['model']}\n"
                    f"keyboard.effective-rmlvo.options={rmlvo['options']}\n"
                    f"keyboard.effective-rmlvo.rules={rmlvo['rules']}\n"
                    "keyboard.effective-rmlvo.variants="
                    + repr(tuple(rmlvo["variants"]))[1:-1]
                    + "\n"
                    f"keyboard.owner={owner}\n",
                    encoding="utf-8",
                )
                info_path.chmod(0o600)

            server_log = directory / "server.stderr"
            client_log = directory / "client.stdout"
            server_log.write_bytes(server_payload)
            client_log.write_bytes(client_payload)
            server_log.chmod(0o600)
            client_log.chmod(0o600)
            fixture_log = directory / "keyboard-fixture.stdout"
            fixture_log.write_text(
                "".join(
                    json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                    + "\n"
                    for event in evidence["application"]["events"]
                ),
                encoding="utf-8",
            )
            fixture_log.chmod(0o600)
            fixture_exit = directory / "keyboard-fixture.exit"
            fixture_exit.write_text("0\n", encoding="ascii")
            fixture_exit.chmod(0o600)

            for phase in evidence["phases"]:
                start, end = phase["server_application"]["log_range"]
                phase["server_application"] = live_run.parse_keyboard_server_application(
                    server_log, start, end
                )
                phase["structured_update"] = live_run.parse_keyboard_structured_update(
                    server_log, start, end
                )
                info_path = directory / phase["server_info_artifact"]
                phase["server_info"] = live_run.parse_keyboard_server_info(info_path)
                phase["server_info_sha256"] = live_run.sha256_file(info_path)
                for item in phase["inputs"]:
                    start, end = item["client_log_range"]
                    item["client_trace"] = live_run.parse_keyboard_client_trace(
                        client_log, start, end
                    )
                    start, end = item["server_log_range"]
                    item["server_trace"] = live_run.parse_keyboard_server_trace(
                        server_log, start, end
                    )

            evidence["checks"] = live_run.keyboard_live_checks(
                evidence, scenario, digest
            )
            self.assertTrue(all(evidence["checks"].values()), evidence["checks"])
            self.assertTrue(
                live_run.keyboard_embedded_checks_match(evidence, scenario, digest)
            )
            self.assertTrue(
                live_run.keyboard_artifact_evidence_matches(evidence, directory)
            )

            original_client = client_log.read_bytes()
            client_log.write_bytes(original_client.replace(b"True", b"False", 1))
            self.assertFalse(
                live_run.keyboard_artifact_evidence_matches(evidence, directory)
            )
            client_log.write_bytes(original_client)

            original_server = server_log.read_bytes()
            first_hash = evidence["phases"][0]["rmlvo_hash"].encode("ascii")
            server_log.write_bytes(original_server.replace(first_hash, b"f" * 64, 1))
            self.assertFalse(
                live_run.keyboard_artifact_evidence_matches(evidence, directory)
            )
            server_log.write_bytes(original_server)

            first_info = directory / evidence["phases"][0]["server_info_artifact"]
            original_info = first_info.read_bytes()
            first_info.write_bytes(
                original_info + b"keyboard.rejected.reason=mutation\n"
            )
            self.assertFalse(
                live_run.keyboard_artifact_evidence_matches(evidence, directory)
            )
            first_info.write_bytes(original_info)

            original_fixture = fixture_log.read_bytes()
            fixture_log.write_bytes(original_fixture.rsplit(b"\n", 2)[0] + b"\n")
            self.assertFalse(
                live_run.keyboard_artifact_evidence_matches(evidence, directory)
            )
            fixture_log.write_bytes(original_fixture)

            fixture_log.write_bytes(original_fixture + b"\xff")
            self.assertFalse(
                live_run.keyboard_artifact_evidence_matches(evidence, directory)
            )
            fixture_log.write_bytes(original_fixture)

            fixture_exit.write_text("1\n", encoding="ascii")
            self.assertFalse(
                live_run.keyboard_artifact_evidence_matches(evidence, directory)
            )

    def test_keyboard_scenario_and_container_inventory_are_bound(self) -> None:
        evidence, scenario, digest = self.passing_keyboard_evidence()
        self.assertEqual(evidence["scenario"], {"data": scenario, "sha256": digest})
        command, titles, pid_file = live_run.application_contract("keyboard")
        self.assertEqual(
            command,
            "/opt/xpra-fork-maintenance/start_wayland_keyboard_fixture.sh",
        )
        self.assertEqual(titles, (live_run.KEYBOARD_FIXTURE_TITLE,))
        self.assertEqual(pid_file, "keyboard-fixture.pid")
        harness_names = {path.name for path in live_run.HARNESS_INPUTS}
        context_names = {path.name for path in live_run.BUILD_CONTEXT_INPUTS}
        for name in (
            "start_wayland_keyboard_fixture.sh",
            "wayland_keyboard_fixture.py",
            "xkb_xtest_driver.c",
        ):
            self.assertIn(name, harness_names)
            self.assertIn(name, context_names)
        containerfile = (LIVE_DIRECTORY / "Containerfile").read_text(encoding="utf-8")
        self.assertIn("xpra-xkb-xtest-driver", containerfile)
        self.assertIn("start_wayland_keyboard_fixture.sh", containerfile)
        self.assertIn("wayland_keyboard_fixture.py", containerfile)

    def test_subsurface_fixture_and_container_inventory_are_bound(self) -> None:
        command, titles, pid_file = live_run.application_contract("subsurface")
        self.assertEqual(
            command,
            "/opt/xpra-fork-maintenance/start_wayland_subsurface_fixture.sh",
        )
        self.assertEqual(titles, (live_run.SUBSURFACE_FIXTURE_TITLE,))
        self.assertEqual(pid_file, "subsurface-fixture.pid")
        harness_names = {path.name for path in live_run.HARNESS_INPUTS}
        context_names = {path.name for path in live_run.BUILD_CONTEXT_INPUTS}
        for name in ("start_wayland_subsurface_fixture.sh", "subsurface_fixture.c"):
            self.assertIn(name, harness_names)
            self.assertIn(name, context_names)
            self.assertIn(
                f"!{name}",
                (LIVE_DIRECTORY / ".containerignore").read_text(encoding="utf-8"),
            )
        containerfile = (LIVE_DIRECTORY / "Containerfile").read_text(encoding="utf-8")
        self.assertIn("xpra-subsurface-fixture", containerfile)
        self.assertIn("start_wayland_subsurface_fixture.sh", containerfile)
        fixture = (LIVE_DIRECTORY / "subsurface_fixture.c").read_text(
            encoding="utf-8"
        )

        def integer_macro(name: str) -> int:
            match = re.search(rf"^#define {name} ([0-9]+)$", fixture, re.MULTILINE)
            self.assertIsNotNone(match, name)
            assert match is not None
            return int(match.group(1))

        def string_macro(name: str) -> str:
            match = re.search(rf'^#define {name} "([^"\\]+)"$', fixture, re.MULTILINE)
            self.assertIsNotNone(match, name)
            assert match is not None
            return match.group(1)

        self.assertEqual(
            integer_macro("SUBSURFACE_FIXTURE_SCHEMA"),
            live_run.SUBSURFACE_FIXTURE_SCHEMA,
        )
        self.assertEqual(integer_macro("CONTINUOUS_MIN_INTERVAL_NS"),
                         live_run.SUBSURFACE_CONTINUOUS_MIN_INTERVAL_NS)
        self.assertLess(live_run.SUBSURFACE_CONTINUOUS_ACTIVE_DEADLINE_NS,
                        (live_run.SUBSURFACE_CONTINUOUS_MAX_GENERATIONS - 1)
                        * live_run.SUBSURFACE_CONTINUOUS_MIN_INTERVAL_NS)
        self.assertNotIn(r'\"schema\":3', fixture)
        self.assertEqual(
            (integer_macro("PRIMARY_WIDTH"), integer_macro("PRIMARY_HEIGHT")),
            live_run.SUBSURFACE_PARENT_DIMENSIONS["primary"],
        )
        self.assertEqual(
            (integer_macro("SECONDARY_WIDTH"), integer_macro("SECONDARY_HEIGHT")),
            live_run.SUBSURFACE_PARENT_DIMENSIONS["secondary"],
        )
        self.assertEqual(
            (integer_macro("LOWER_WIDTH"), integer_macro("LOWER_HEIGHT")),
            live_run.SUBSURFACE_LOWER_DIMENSIONS,
        )
        self.assertEqual(
            integer_macro("LOWER_BUFFER_SCALE"),
            live_run.SUBSURFACE_LOWER_BUFFER_SCALE,
        )
        self.assertEqual(
            (integer_macro("LOWER_INITIAL_X"), integer_macro("LOWER_INITIAL_Y")),
            live_run.SUBSURFACE_INITIAL_OFFSET,
        )
        self.assertEqual(
            (integer_macro("LOWER_MOVED_X"), integer_macro("LOWER_MOVED_Y")),
            live_run.SUBSURFACE_MOVED_OFFSET,
        )
        self.assertEqual(
            (integer_macro("UPPER_WIDTH"), integer_macro("UPPER_HEIGHT")),
            live_run.SUBSURFACE_UPPER_DIMENSIONS,
        )
        self.assertEqual(
            (integer_macro("UPPER_X"), integer_macro("UPPER_Y")),
            live_run.SUBSURFACE_UPPER_OFFSET,
        )
        self.assertEqual(
            (
                integer_macro("UPPER_REPARENT_X"),
                integer_macro("UPPER_REPARENT_Y"),
            ),
            live_run.SUBSURFACE_REPARENT_OFFSET,
        )
        self.assertEqual(
            string_macro("UPPER_BUFFER_TRANSFORM_NAME"),
            live_run.SUBSURFACE_UPPER_BUFFER_TRANSFORM,
        )
        self.assertEqual(
            string_macro("PRIMARY_TITLE"),
            live_run.SUBSURFACE_FIXTURE_TITLE,
        )
        self.assertEqual(
            string_macro("SECONDARY_TITLE"),
            live_run.SUBSURFACE_REPARENT_TARGET_TITLE,
        )
        self.assertEqual(
            (
                string_macro("FRAME_GENERATION_ONE_MARKER"),
                string_macro("FRAME_GENERATION_TWO_MARKER"),
            ),
            live_run.SUBSURFACE_FRAME_GENERATION_MARKERS,
        )
        self.assertEqual(
            (
                string_macro("CONTINUOUS_START_MARKER"),
                string_macro("CONTINUOUS_STOP_MARKER"),
            ),
            (
                live_run.SUBSURFACE_CONTINUOUS_START_MARKER,
                live_run.SUBSURFACE_CONTINUOUS_STOP_MARKER,
            ),
        )
        self.assertEqual(
            integer_macro("CONTINUOUS_MIN_GENERATIONS"),
            live_run.SUBSURFACE_CONTINUOUS_MIN_GENERATIONS,
        )
        self.assertEqual(
            integer_macro("CONTINUOUS_MAX_GENERATIONS"),
            live_run.SUBSURFACE_CONTINUOUS_MAX_GENERATIONS,
        )
        self.assertEqual(
            (
                integer_macro("CONTINUOUS_DAMAGE_X")
                + live_run.SUBSURFACE_MOVED_OFFSET[0],
                integer_macro("CONTINUOUS_DAMAGE_Y")
                + live_run.SUBSURFACE_MOVED_OFFSET[1],
                integer_macro("CONTINUOUS_DAMAGE_WIDTH"),
                integer_macro("CONTINUOUS_DAMAGE_HEIGHT"),
            ),
            live_run.SUBSURFACE_CONTINUOUS_GEOMETRY,
        )
        wrapper = (LIVE_DIRECTORY / "start_wayland_subsurface_fixture.sh").read_text(
            encoding="utf-8"
        )
        for marker in (
            *live_run.SUBSURFACE_FRAME_GENERATION_MARKERS,
            live_run.SUBSURFACE_CONTINUOUS_START_MARKER,
            live_run.SUBSURFACE_CONTINUOUS_STOP_MARKER,
        ):
            self.assertIn(marker, wrapper)
        for authority in (
            "WL_SHM_FORMAT_ARGB8888",
            "wl_surface_set_buffer_scale",
            "logical_x = x / scale",
            "logical_x = width - logical_x - 1",
            "WL_OUTPUT_TRANSFORM_180",
            "wl_subsurface_place_above",
            "wl_subsurface_set_position",
            "wl_surface_frame",
            "wl_callback_add_listener",
            "lower_frame_done",
            "lower_frame_ready",
            "commit_lower_frame_generation",
            "emit_lower_frame_generation",
            "commit_lower_continuous_generation",
            "emit_continuous_generation",
            "stop_lower_continuous_generations",
            "Wayland roundtrip failed after role-less upper commit",
            "Wayland roundtrip failed after upper detach",
            "Wayland roundtrip failed after parent-only upper reattach",
            "upper_reattach_without_child_commit",
            live_run.SUBSURFACE_REPARENT_TARGET_TITLE,
        ):
            self.assertIn(authority, fixture)
        self.assertNotIn("wait_for_generation_marker", fixture)
        self.assertNotIn("usleep", fixture)

    @staticmethod
    def passing_empty_damage_evidence() -> dict[str, object]:
        evidence: dict[str, object] = {
            "click_failure": None,
            "click_observed_after_seconds": 0.25,
            "click_position": [130, 80],
            "clicked_within_deadline": True,
            "events": copy.deepcopy(
                LiveTransportProfileTest.passing_empty_damage_events()
            ),
            "fixture_exit_status": 0,
            "input_path": {
                "client_press_release": True,
                "server_child_focus": True,
                "server_coordinates": True,
                "server_press_release": True,
                "fixture_child_release": True,
                "fixture_coordinates": True,
            },
            "pressure": {
                "marker": True,
                "parent_mapped_empty_commit": True,
                "child_mapped_empty_commit": True,
                "parent_frames_at_marker": 60,
                "child_frames_at_marker": 61,
            },
            "teardown": {
                "client_destroy_logged": True,
                "client_windows_absent": True,
                "complete": True,
                "server_destroy_logged": True,
                "server_inventory_available": True,
                "server_windows_absent": True,
            },
            "windows": {
                "client_ids_distinct": True,
                "server_ids_distinct": True,
                "visible_content": True,
            },
        }
        return evidence

    @staticmethod
    def passing_empty_damage_events() -> list[dict[str, object]]:
        return [
            {
                "child_frames": 0,
                "event": "ready",
                "monotonic_seconds": 10.0,
                "parent_frames": 0,
            },
            {
                "child_frames": 61,
                "event": "pressure-ready",
                "monotonic_seconds": 20.0,
                "parent_frames": 60,
            },
            {
                "event": "child-click",
                "monotonic_seconds": 30.0,
                "x": 130.0,
                "y": 80.0,
            },
            {
                "child_frames": 63,
                "event": "exit",
                # The C fixture records microsecond-resolution floats. Adjacent
                # events may therefore have the same rounded timestamp.
                "monotonic_seconds": 30.0,
                "parent_frames": 62,
            },
        ]

    @staticmethod
    def mutate_nested(
        value: dict[str, object],
        path: tuple[str, ...],
        replacement: object,
    ) -> None:
        parent: dict[str, object] = value
        for key in path[:-1]:
            child = parent[key]
            assert isinstance(child, dict)
            parent = child
        parent[path[-1]] = replacement

    @staticmethod
    def remove_nested(value: dict[str, object], path: tuple[str, ...]) -> None:
        parent: dict[str, object] = value
        for key in path[:-1]:
            child = parent[key]
            assert isinstance(child, dict)
            parent = child
        del parent[path[-1]]

    def test_empty_damage_complete_evidence_and_event_stream_pass(self) -> None:
        evidence = self.passing_empty_damage_evidence()
        checks = live_run.empty_damage_fixture_checks(evidence)
        self.assertTrue(all(checks.values()), checks)
        self.assertTrue(checks["secondary_fixture_event_stream_exact"])

        events = self.passing_empty_damage_events()
        validated = live_run.validate_empty_damage_fixture_events(events)
        self.assertEqual(
            tuple(validated),
            ("ready", "pressure-ready", "child-click", "exit"),
        )
        self.assertEqual(list(validated.values()), events)

    def test_empty_damage_event_stream_is_bound_into_classifier_evidence(self) -> None:
        candidates: list[tuple[str, dict[str, object]]] = []

        evidence = self.passing_empty_damage_evidence()
        del evidence["events"]
        candidates.append(("missing-events", evidence))

        evidence = self.passing_empty_damage_evidence()
        evidence["events"] = []
        candidates.append(("empty-events", evidence))

        evidence = self.passing_empty_damage_evidence()
        evidence["events"] = [None, None, None, None]
        candidates.append(("malformed-event-records", evidence))

        evidence = self.passing_empty_damage_evidence()
        events = evidence["events"]
        assert isinstance(events, list)
        events[0], events[1] = events[1], events[0]
        candidates.append(("reordered-events", evidence))

        evidence = self.passing_empty_damage_evidence()
        pressure = evidence["pressure"]
        assert isinstance(pressure, dict)
        pressure["parent_frames_at_marker"] = 61
        candidates.append(("pressure-summary-mismatch", evidence))

        evidence = self.passing_empty_damage_evidence()
        events = evidence["events"]
        assert isinstance(events, list)
        click_event = events[2]
        assert isinstance(click_event, dict)
        click_event["x"] = 999.0
        candidates.append(("click-coordinate-mismatch", evidence))

        evidence = self.passing_empty_damage_evidence()
        del evidence["click_position"]
        candidates.append(("missing-click-position", evidence))

        for mutation, candidate in candidates:
            with self.subTest(mutation=mutation):
                checks = live_run.empty_damage_fixture_checks(candidate)
                self.assertFalse(
                    checks["secondary_fixture_event_stream_exact"],
                    checks,
                )
                self.assertFalse(all(checks.values()), checks)

    def test_empty_damage_boolean_evidence_requires_exact_true(self) -> None:
        boolean_fields = {
            ("clicked_within_deadline",): "secondary_pointer_response_bounded",
            ("input_path", "client_press_release"): "secondary_client_pointer_path",
            ("input_path", "server_child_focus"): "secondary_server_pointer_path",
            ("input_path", "server_coordinates"): "secondary_server_pointer_path",
            ("input_path", "server_press_release"): "secondary_server_pointer_path",
            ("input_path", "fixture_child_release"): "secondary_surface_pointer_path",
            ("input_path", "fixture_coordinates"): "secondary_surface_pointer_path",
            ("pressure", "marker"): "empty_damage_pressure_active",
            ("pressure", "parent_mapped_empty_commit"): "empty_damage_pressure_active",
            ("pressure", "child_mapped_empty_commit"): "empty_damage_pressure_active",
            ("teardown", "client_destroy_logged"): "secondary_toplevel_teardown",
            ("teardown", "client_windows_absent"): "secondary_toplevel_teardown",
            ("teardown", "complete"): "secondary_toplevel_teardown",
            ("teardown", "server_destroy_logged"): "secondary_toplevel_teardown",
            ("teardown", "server_inventory_available"): "secondary_toplevel_teardown",
            ("teardown", "server_windows_absent"): "secondary_toplevel_teardown",
            ("windows", "client_ids_distinct"): "secondary_toplevels_discovered",
            ("windows", "server_ids_distinct"): "secondary_toplevels_discovered",
            ("windows", "visible_content"): "secondary_toplevels_visible",
        }
        mutations = (("missing", None), ("false", False), ("truthy-string", "true"))
        for path, expected_check in boolean_fields.items():
            for mutation, replacement in mutations:
                with self.subTest(path="/".join(path), mutation=mutation):
                    evidence = self.passing_empty_damage_evidence()
                    if mutation == "missing":
                        self.remove_nested(evidence, path)
                    else:
                        self.mutate_nested(evidence, path, replacement)
                    checks = live_run.empty_damage_fixture_checks(evidence)
                    self.assertFalse(checks[expected_check], checks)
                    self.assertFalse(all(checks.values()), checks)

    def test_empty_damage_counts_and_exit_status_require_exact_integers(self) -> None:
        integer_fields = {
            ("fixture_exit_status",): (
                "secondary_fixture_clean_exit",
                (False, 0.0, "0"),
            ),
            ("pressure", "parent_frames_at_marker"): (
                "empty_damage_pressure_active",
                (True, 60.0, "60"),
            ),
            ("pressure", "child_frames_at_marker"): (
                "empty_damage_pressure_active",
                (True, 61.0, "61"),
            ),
        }
        for path, (expected_check, replacements) in integer_fields.items():
            for replacement in replacements:
                with self.subTest(path="/".join(path), replacement=repr(replacement)):
                    evidence = self.passing_empty_damage_evidence()
                    self.mutate_nested(evidence, path, replacement)
                    checks = live_run.empty_damage_fixture_checks(evidence)
                    self.assertFalse(checks[expected_check], checks)
                    self.assertFalse(all(checks.values()), checks)

        for field in ("parent_frames_at_marker", "child_frames_at_marker"):
            with self.subTest(field=field, boundary=59):
                evidence = self.passing_empty_damage_evidence()
                pressure = evidence["pressure"]
                assert isinstance(pressure, dict)
                pressure[field] = 59
                checks = live_run.empty_damage_fixture_checks(evidence)
                self.assertFalse(checks["empty_damage_pressure_active"], checks)

    def test_empty_damage_deadline_and_failure_evidence_fail_closed(self) -> None:
        exact_deadline = self.passing_empty_damage_evidence()
        exact_deadline["click_observed_after_seconds"] = (
            live_run.EMPTY_DAMAGE_INPUT_DEADLINE_SECONDS
        )
        self.assertTrue(
            live_run.empty_damage_fixture_checks(exact_deadline)[
                "secondary_pointer_response_bounded"
            ]
        )

        invalid_elapsed = (
            True,
            1,
            "0.25",
            -0.001,
            live_run.EMPTY_DAMAGE_INPUT_DEADLINE_SECONDS + 0.000001,
            float("nan"),
            float("inf"),
            -float("inf"),
        )
        for replacement in invalid_elapsed:
            with self.subTest(elapsed=repr(replacement)):
                evidence = self.passing_empty_damage_evidence()
                evidence["click_observed_after_seconds"] = replacement
                checks = live_run.empty_damage_fixture_checks(evidence)
                self.assertFalse(checks["secondary_pointer_response_bounded"], checks)
                self.assertFalse(all(checks.values()), checks)

        for field in ("click_observed_after_seconds", "click_failure"):
            with self.subTest(missing=field):
                evidence = self.passing_empty_damage_evidence()
                del evidence[field]
                checks = live_run.empty_damage_fixture_checks(evidence)
                self.assertFalse(checks["secondary_pointer_response_bounded"], checks)
                self.assertFalse(all(checks.values()), checks)

        for failure in ("", "timed out"):
            with self.subTest(click_failure=repr(failure)):
                evidence = self.passing_empty_damage_evidence()
                evidence["click_failure"] = failure
                checks = live_run.empty_damage_fixture_checks(evidence)
                self.assertFalse(checks["secondary_pointer_response_bounded"], checks)
                self.assertFalse(all(checks.values()), checks)

    def test_empty_damage_event_sequence_is_complete_and_ordered(self) -> None:
        for index in range(4):
            with self.subTest(mutation="missing", index=index):
                candidate = self.passing_empty_damage_events()
                candidate.pop(index)
                with self.assertRaises(live_run.LabFailure):
                    live_run.validate_empty_damage_fixture_events(candidate)
            with self.subTest(mutation="duplicate", index=index):
                candidate = self.passing_empty_damage_events()
                candidate.insert(index, copy.deepcopy(candidate[index]))
                with self.assertRaises(live_run.LabFailure):
                    live_run.validate_empty_damage_fixture_events(candidate)
            with self.subTest(mutation="wrong-name", index=index):
                candidate = self.passing_empty_damage_events()
                candidate[index]["event"] = "unexpected"
                with self.assertRaises(live_run.LabFailure):
                    live_run.validate_empty_damage_fixture_events(candidate)

        candidate = self.passing_empty_damage_events()
        candidate.append({"event": "unexpected"})
        with self.assertRaises(live_run.LabFailure):
            live_run.validate_empty_damage_fixture_events(candidate)
        for index in range(3):
            with self.subTest(mutation="adjacent-swap", index=index):
                candidate = self.passing_empty_damage_events()
                candidate[index], candidate[index + 1] = (
                    candidate[index + 1], candidate[index]
                )
                with self.assertRaises(live_run.LabFailure):
                    live_run.validate_empty_damage_fixture_events(candidate)

    def test_empty_damage_event_fields_are_exact(self) -> None:
        for index, event in enumerate(self.passing_empty_damage_events()):
            for field in tuple(event):
                with self.subTest(index=index, missing=field):
                    events = self.passing_empty_damage_events()
                    del events[index][field]
                    with self.assertRaises(live_run.LabFailure):
                        live_run.validate_empty_damage_fixture_events(events)
            with self.subTest(index=index, extra="unexpected"):
                events = self.passing_empty_damage_events()
                events[index]["unexpected"] = True
                with self.assertRaises(live_run.LabFailure):
                    live_run.validate_empty_damage_fixture_events(events)

    def test_empty_damage_event_timestamps_are_exact_finite_and_ordered(self) -> None:
        for index in range(4):
            original = self.passing_empty_damage_events()[index]["monotonic_seconds"]
            assert isinstance(original, float)
            for replacement in (
                True,
                int(original),
                str(original),
                -0.001,
                float("nan"),
                float("inf"),
                -float("inf"),
            ):
                with self.subTest(index=index, timestamp=repr(replacement)):
                    events = self.passing_empty_damage_events()
                    events[index]["monotonic_seconds"] = replacement
                    with self.assertRaises(live_run.LabFailure):
                        live_run.validate_empty_damage_fixture_events(events)

        events = self.passing_empty_damage_events()
        events[2]["monotonic_seconds"] = 19.5
        with self.assertRaises(live_run.LabFailure):
            live_run.validate_empty_damage_fixture_events(events)

    def test_empty_damage_event_frame_counts_are_exact_and_monotonic(self) -> None:
        for index in (0, 1, 3):
            for field in ("parent_frames", "child_frames"):
                original = self.passing_empty_damage_events()[index][field]
                assert isinstance(original, int) and not isinstance(original, bool)
                for replacement in (bool(original), float(original), str(original), -1):
                    with self.subTest(index=index, field=field, value=repr(replacement)):
                        events = self.passing_empty_damage_events()
                        events[index][field] = replacement
                        with self.assertRaises(live_run.LabFailure):
                            live_run.validate_empty_damage_fixture_events(events)

        semantic_mutations = (
            (0, "parent_frames", 1),
            (0, "child_frames", 1),
            (1, "parent_frames", 59),
            (1, "child_frames", 59),
            (3, "parent_frames", 59),
            (3, "child_frames", 60),
        )
        for index, field, replacement in semantic_mutations:
            with self.subTest(index=index, field=field, value=replacement):
                events = self.passing_empty_damage_events()
                events[index][field] = replacement
                with self.assertRaises(live_run.LabFailure):
                    live_run.validate_empty_damage_fixture_events(events)

    def test_empty_damage_click_coordinates_are_exact_finite_values(self) -> None:
        for field in ("x", "y"):
            original = self.passing_empty_damage_events()[2][field]
            assert isinstance(original, float)
            for replacement in (
                True,
                int(original),
                str(original),
                -0.001,
                float("nan"),
                float("inf"),
                -float("inf"),
            ):
                with self.subTest(field=field, value=repr(replacement)):
                    events = self.passing_empty_damage_events()
                    events[2][field] = replacement
                    with self.assertRaises(live_run.LabFailure):
                        live_run.validate_empty_damage_fixture_events(events)

    def test_empty_damage_event_loader_rejects_duplicate_json_fields(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "empty-damage.stdout"
            path.write_text(
                '{"event":"ready","event":"ready"}\n',
                encoding="utf-8",
            )
            path.chmod(0o600)
            with self.assertRaises(live_run.LabFailure):
                live_run.load_empty_damage_fixture_events(path)

    def test_primary_server_window_id_is_resolved_once_before_frame_poll(self) -> None:
        source = (LIVE_DIRECTORY / "run.py").read_text(encoding="utf-8")
        scenario = source.split("def run_scenario(\n", 1)[1]
        query = (
            'xpra_wid = server_xpra_window_id(directory / "server-info.txt", '
            "title_patterns)"
        )
        self.assertEqual(scenario.count(query), 1)
        self.assertLess(scenario.index(query), scenario.index("wait_for_frame_boundary("))

    def test_hardware_application_uses_owned_multiwindow_fixture(self) -> None:
        command, titles, pid_file = live_run.application_contract("hardware")
        self.assertEqual(command, "/opt/xpra-fork-maintenance/start_hardware_fixture.sh")
        self.assertEqual(titles, ("vkcube",))
        self.assertEqual(pid_file, "vkcube.pid")

        command, titles, pid_file = live_run.application_contract("opengl")
        self.assertEqual(command, "/opt/xpra-fork-maintenance/start_hardware_fixture.sh opengl")
        self.assertEqual(titles, ("glmark2",))
        self.assertEqual(pid_file, "opengl.pid")
        context_names = {path.name for path in live_run.BUILD_CONTEXT_INPUTS}
        self.assertIn("start_hardware_fixture.sh", context_names)

    def test_zed_fixture_keeps_the_reviewed_onboarding_window(self) -> None:
        source = (LIVE_DIRECTORY / "start_zed.sh").read_text(encoding="utf-8")
        self.assertNotIn("xpra-live-scroll", source)

        command, titles, pid_file = live_run.application_contract("zed")
        self.assertEqual(command, "/opt/xpra-fork-maintenance/start_zed.sh")
        self.assertEqual(titles, ("empty project", "zed"))
        self.assertEqual(pid_file, "zed.pid")
        context_names = {path.name for path in live_run.BUILD_CONTEXT_INPUTS}
        self.assertIn("empty_damage_fixture.c", context_names)

    def test_empty_damage_fixture_is_generic_and_recycles_frame_callbacks(self) -> None:
        source = (LIVE_DIRECTORY / "empty_damage_fixture.c").read_text(
            encoding="utf-8"
        )
        self.assertIn('PARENT_TITLE "Xpra Empty Damage Parent"', source)
        self.assertIn('CHILD_TITLE "Xpra Empty Damage Child"', source)
        self.assertIn("xdg_toplevel_set_parent(window->toplevel, parent)", source)
        self.assertIn("wl_surface_frame(window->surface)", source)
        self.assertIn("intentionally an empty commit", source)
        self.assertIn("wl_surface_commit(window->surface)", source)
        self.assertIn("PRESSURE_FRAMES 60", source)
        self.assertIn("WL_POINTER_BUTTON_STATE_RELEASED", source)
        self.assertNotIn("Zed", source)

    def test_server_image_builds_native_empty_damage_fixture(self) -> None:
        source = (LIVE_DIRECTORY / "Containerfile").read_text(encoding="utf-8")
        server_build = source.split(
            "FROM docker.io/library/ubuntu:26.04 AS server-build\n",
            1,
        )[1].split("FROM docker.io/library/ubuntu:26.04 AS server\n", 1)[0]
        server = source.split(
            "FROM docker.io/library/ubuntu:26.04 AS server\n",
            1,
        )[1].split("FROM docker.io/library/debian:13-slim AS client-build\n", 1)[0]
        self.assertIn("wayland-scanner client-header", server_build)
        self.assertIn("/tmp/xpra-empty-damage-fixture.c", server_build)
        self.assertIn(
            "-o /opt/xpra-install/usr/local/bin/xpra-empty-damage-fixture",
            server_build,
        )
        self.assertIn("test -x /usr/local/bin/xpra-empty-damage-fixture", server)

    def test_client_image_scopes_clipboard_adapter_preflight_to_its_case(self) -> None:
        source = (LIVE_DIRECTORY / "Containerfile").read_text(encoding="utf-8")
        client = source.split(
            "FROM docker.io/library/debian:13-slim AS client\n",
            1,
        )[1]
        case_selection = sys.modules["profiles"].CLIPBOARD_CASE_SELECTION
        case_guard = f'[ "$XPRA_SELECTION" = "{case_selection}" ]'
        conditional = client.split(f"if {case_guard}; then", 1)[1].split(
            "       fi \\\n",
            1,
        )[0]
        case_branch, clean_branch = conditional.split("       else \\\n", 1)
        client_import = "from xpra.client.gtk3 import client_base"
        adapter_check = "from xpra.x11.common import has_pywindow_lookup"
        helper_import = "from xpra.x11.selection.clipboard import X11Clipboard"
        self.assertIn("ARG XPRA_SELECTION", client)
        self.assertIn(case_guard, client)
        self.assertIn(client_import, case_branch)
        self.assertIn(adapter_check, case_branch)
        self.assertIn(helper_import, case_branch)
        self.assertIn(client_import, clean_branch)
        self.assertNotIn(adapter_check, clean_branch)
        self.assertNotIn(helper_import, clean_branch)

    def test_frame_alpha_states_are_parsed_and_bound_to_exact_windows(self) -> None:
        server_log = (
            "prefix window 0x1 frame pixel format=BGRX, want-alpha=False\n"
            "prefix window 0x2 frame pixel format=BGRA, want-alpha=True\n"
            "prefix window 0xa frame pixel format=RGBA, want-alpha=True\n"
            "window 0x1 frame pixel format=BGRX, want-alpha=false\n"
            "window 0x1 frame pixel format=BGRX, want-alpha=False trailing"
        )
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            (directory / "server.stderr").write_text(server_log, encoding="utf-8")
            states = live_run.inspect_logs(directory)["frame_alpha_states"]
        self.assertEqual(
            states,
            [
                {"pixel_format": "BGRX", "want_alpha": False, "window_id": 1},
                {"pixel_format": "BGRA", "want_alpha": True, "window_id": 2},
                {"pixel_format": "RGBA", "want_alpha": True, "window_id": 10},
            ],
        )
        checks = live_run.hardware_frame_alpha_state_checks(
            {"frame_alpha_states": states},
            {"window_id": 1},
            {"window_id": 2},
        )
        self.assertTrue(all(checks.values()))

        wrong_window = live_run.hardware_frame_alpha_state_checks(
            {"frame_alpha_states": states},
            {"window_id": 3},
            {"window_id": 2},
        )
        self.assertFalse(wrong_window["primary_window_opaque_frame_states"])
        self.assertTrue(wrong_window["interaction_window_alpha_frame_states"])

        later_transition = [
            *states,
            {"pixel_format": "RGBA", "want_alpha": True, "window_id": 1},
            {"pixel_format": "BGRX", "want_alpha": False, "window_id": 2},
        ]
        transitioned = live_run.hardware_frame_alpha_state_checks(
            {"frame_alpha_states": later_transition},
            {"window_id": 1},
            {"window_id": 2},
        )
        self.assertFalse(transitioned["primary_window_opaque_frame_states"])
        self.assertFalse(transitioned["interaction_window_alpha_frame_states"])

        adaptive_updates = {
            "updates": [
                {
                    "encoding": "webp",
                    "options": {},
                    "relative_info": "screen-updates/1/100/0.info",
                },
                {
                    "encoding": "h264",
                    "options": {},
                    "relative_info": "screen-updates/1/101/0.info",
                },
            ],
            "window_id": 1,
        }
        saved_states = [
            {
                "encoding": "webp",
                "pixel_format": "BGRA",
                "relative_info": "screen-updates/1/100/0.info",
                "want_alpha": True,
                "window_id": 1,
            },
            {
                "encoding": "h264",
                "pixel_format": "BGRX",
                "relative_info": "screen-updates/1/101/0.info",
                "want_alpha": False,
                "window_id": 1,
            },
        ]
        adaptive = live_run.adaptive_frame_alpha_state_checks(
            {
                "frame_alpha_states": [
                    {
                        "pixel_format": "BGRA",
                        "want_alpha": True,
                        "window_id": 1,
                    },
                    {
                        "pixel_format": "BGRX",
                        "want_alpha": False,
                        "window_id": 1,
                    },
                ],
                "saved_packet_frame_states": saved_states,
            },
            adaptive_updates,
        )
        self.assertTrue(all(adaptive.values()))
        wrong_adaptive_window = live_run.adaptive_frame_alpha_state_checks(
            {
                "frame_alpha_states": states,
                "saved_packet_frame_states": saved_states,
            },
            {**adaptive_updates, "window_id": 3},
        )
        self.assertFalse(all(wrong_adaptive_window.values()))
        inconsistent_adaptive = live_run.adaptive_frame_alpha_state_checks(
            {
                "frame_alpha_states": [
                    {
                        "pixel_format": "BGRX",
                        "want_alpha": False,
                        "window_id": 1,
                    },
                    {
                        "pixel_format": "BGRA",
                        "want_alpha": False,
                        "window_id": 1,
                    },
                ],
                "saved_packet_frame_states": saved_states,
            },
            adaptive_updates,
        )
        self.assertFalse(all(inconsistent_adaptive.values()))
        all_alpha_adaptive = live_run.adaptive_frame_alpha_state_checks(
            {
                "frame_alpha_states": [
                    {
                        "pixel_format": "BGRA",
                        "want_alpha": True,
                        "window_id": 1,
                    }
                ],
                "saved_packet_frame_states": [
                    {**record, "pixel_format": "BGRA", "want_alpha": True}
                    for record in saved_states
                ],
            },
            adaptive_updates,
        )
        self.assertFalse(
            all_alpha_adaptive["primary_h264_packets_have_opaque_frame_state"]
        )

        opaque_webp = live_run.adaptive_frame_alpha_state_checks(
            {
                "frame_alpha_states": [
                    {
                        "pixel_format": "RGBX",
                        "want_alpha": False,
                        "window_id": 1,
                    }
                ],
                "saved_packet_frame_states": [
                    {**record, "pixel_format": "RGBX", "want_alpha": False}
                    for record in saved_states
                ],
            },
            adaptive_updates,
        )
        self.assertTrue(all(opaque_webp.values()))

    def test_saved_packets_bind_to_the_latest_exact_window_frame_state(self) -> None:
        server_log = (
            "window 0x1 frame pixel format=RGBX, want-alpha=False\n"
            "saved webp : 10 bytes to "
            "'/artifacts/screen-updates/1/100/0.webp'\n"
            "window 0x2 frame pixel format=BGRA, want-alpha=True\n"
            "saved webp : 11 bytes to "
            "'/artifacts/screen-updates/2/101/0.webp'\n"
            "window 0x1 frame pixel format=RGBA, want-alpha=True\n"
            "saved rgb32: 12 bytes to "
            "'/artifacts/screen-updates/1/102/0.rgb32'\n"
        )
        self.assertEqual(
            live_run.parse_saved_packet_frame_states(server_log),
            [
                {
                    "encoding": "webp",
                    "pixel_format": "RGBX",
                    "relative_info": "screen-updates/1/100/0.info",
                    "want_alpha": False,
                    "window_id": 1,
                },
                {
                    "encoding": "webp",
                    "pixel_format": "BGRA",
                    "relative_info": "screen-updates/2/101/0.info",
                    "want_alpha": True,
                    "window_id": 2,
                },
                {
                    "encoding": "rgb32",
                    "pixel_format": "RGBA",
                    "relative_info": "screen-updates/1/102/0.info",
                    "want_alpha": True,
                    "window_id": 1,
                },
            ],
        )
        self.assertEqual(
            live_run.parse_saved_packet_frame_states(
                "saved h264 : 9 bytes to "
                "'/artifacts/screen-updates/1/100/0.h264'\n"
            )[0]["pixel_format"],
            None,
        )

    def test_hardware_artifact_allowlist_includes_only_exact_fixture_files(self) -> None:
        allowed = (
            "interaction.exit",
            "interaction.identity.json",
            "interaction.stderr",
            "interaction.stdout",
            "keyboard-fixture.exit",
            "keyboard-fixture.pid",
            "keyboard-fixture.stderr",
            "keyboard-fixture.stdout",
            "vkcube.exit",
            "vkcube.pid",
            "vkcube.stderr",
            "vkcube.stdout",
            "opengl.exit",
            "opengl.pid",
            "opengl.stderr",
            "opengl.stdout",
        )
        for name in allowed:
            with self.subTest(name=name):
                self.assertTrue(
                    any(pattern.fullmatch(name) for pattern in live_run.SERVER_ARTIFACT_PATTERNS)
                )
        for name in (
            "interaction.core",
            "interaction.pid",
            "keyboard-fixture.core",
            "keyboard-fixture.trace",
            "opengl.core",
            "opengl.trace",
            "vkcube.trace",
            "vkcube.exit.extra",
        ):
            with self.subTest(name=name):
                self.assertFalse(
                    any(pattern.fullmatch(name) for pattern in live_run.SERVER_ARTIFACT_PATTERNS)
                )

    def test_hardware_launcher_records_both_child_statuses_without_masking(self) -> None:
        source = (LIVE_DIRECTORY / "start_hardware_fixture.sh").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifacts = root / "artifacts"
            binaries = root / "bin"
            artifacts.mkdir()
            binaries.mkdir()
            script_text = source.replace("/artifacts", str(artifacts)).replace(
                "/tmp/xpra-hardware-",
                str(root / "marker-"),
            )
            script = root / "start-hardware.sh"
            script.write_text(script_text, encoding="utf-8")
            script.chmod(0o700)
            (binaries / "vkcube").write_text(
                "#!/bin/sh\nexit \"$FAKE_VULKAN_STATUS\"\n",
                encoding="utf-8",
            )
            (binaries / "glmark2-wayland").write_text(
                "#!/bin/sh\n"
                "test -z \"${DISPLAY+x}\" || exit 97\n"
                "exit \"$FAKE_OPENGL_STATUS\"\n",
                encoding="utf-8",
            )
            (binaries / "python3").write_text(
                "#!/bin/sh\n"
                "test -z \"${DISPLAY+x}\" || exit 98\n"
                "test \"$GDK_BACKEND\" = wayland || exit 99\n"
                "exit \"$FAKE_INTERACTION_STATUS\"\n",
                encoding="utf-8",
            )
            for executable in (
                binaries / "glmark2-wayland",
                binaries / "vkcube",
                binaries / "python3",
            ):
                executable.chmod(0o700)

            for interaction, vulkan, expected in ((0, 0, 0), (7, 0, 7), (0, 9, 9)):
                with self.subTest(
                    interaction=interaction,
                    vulkan=vulkan,
                ):
                    for name in ("interaction.exit", "vkcube.exit"):
                        (artifacts / name).unlink(missing_ok=True)
                    environment = os.environ.copy()
                    environment.update(
                        {
                            "DISPLAY": ":150",
                            "FAKE_INTERACTION_STATUS": str(interaction),
                            "FAKE_OPENGL_STATUS": "0",
                            "FAKE_VULKAN_STATUS": str(vulkan),
                            "PATH": f"{binaries}:{environment['PATH']}",
                        }
                    )
                    result = subprocess.run(
                        ["bash", str(script)],
                        capture_output=True,
                        check=False,
                        env=environment,
                        text=True,
                    )
                    self.assertEqual(result.returncode, expected, result.stderr)
                    self.assertEqual(
                        (artifacts / "interaction.exit").read_text(encoding="ascii"),
                        f"{interaction}\n",
                    )
                    self.assertEqual(
                        (artifacts / "vkcube.exit").read_text(encoding="ascii"),
                        f"{vulkan}\n",
                    )

            environment = os.environ.copy()
            environment.update(
                {
                    "DISPLAY": ":150",
                    "FAKE_INTERACTION_STATUS": "0",
                    "FAKE_OPENGL_STATUS": "11",
                    "FAKE_VULKAN_STATUS": "0",
                    "PATH": f"{binaries}:{environment['PATH']}",
                }
            )
            result = subprocess.run(
                ["bash", str(script), "opengl"],
                capture_output=True,
                check=False,
                env=environment,
                text=True,
            )
            self.assertEqual(result.returncode, 11, result.stderr)
            self.assertEqual(
                (artifacts / "opengl.exit").read_text(encoding="ascii"),
                "11\n",
            )

    def test_interaction_fixture_is_ready_and_has_a_transparent_border(self) -> None:
        source = (LIVE_DIRECTORY / "interaction_fixture.py").read_text(encoding="utf-8")
        self.assertIn('READY_MARKER = Path("/tmp/xpra-hardware-interaction-ready")', source)
        self.assertIn('IDENTITY_ARTIFACT = Path("/artifacts/interaction.identity.json")', source)
        self.assertIn("publish_process_identity()", source)
        self.assertIn("os.link(temporary, IDENTITY_ARTIFACT)", source)
        self.assertIn("get_rgba_visual()", source)
        self.assertIn("window.set_visual(visual)", source)
        self.assertIn("window.set_app_paintable(True)", source)
        self.assertIn("background-color: rgba(0, 0, 0, 0)", source)
        self.assertIn("button.set_halign(Gtk.Align.CENTER)", source)
        self.assertIn("button.set_valign(Gtk.Align.CENTER)", source)
        self.assertIn("button.set_size_request(360, 120)", source)
        self.assertIn("GLib.idle_add(publish_ready)", source)
        self.assertLess(
            source.index("window.show_all()"),
            source.index("GLib.idle_add"),
        )
        self.assertLess(
            source.index("GLib.idle_add"),
            source.index("Gtk.main()"),
        )

    def test_gtk_contract_uses_fixture_owned_identity_not_pgrep(self) -> None:
        command, _titles, identity_artifact = live_run.application_contract("gtk")
        self.assertEqual(command, f"python3 {live_run.INTERACTION_FIXTURE_SCRIPT}")
        self.assertEqual(identity_artifact, live_run.INTERACTION_IDENTITY_ARTIFACT)

    def test_hardware_launcher_uses_an_opaque_native_wayland_opengl_primary(
        self,
    ) -> None:
        source = (LIVE_DIRECTORY / "start_hardware_fixture.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("glmark2-wayland --run-forever", source)
        self.assertIn("--size 640x480", source)
        self.assertIn("--swap-mode fifo", source)
        self.assertIn("red=8:green=8:blue=8:alpha=0:buffer=24", source)
        self.assertIn("--benchmark jellyfish", source)

    def test_server_image_makes_interaction_fixture_readable_by_runtime_user(self) -> None:
        source = (LIVE_DIRECTORY / "Containerfile").read_text(encoding="utf-8")
        server_stage = source.split(
            "FROM docker.io/library/ubuntu:26.04 AS server\n",
            1,
        )[1].split("FROM docker.io/library/debian:13-slim AS client-build\n", 1)[0]
        chmod = "chmod 0644 /opt/xpra-fork-maintenance/interaction_fixture.py"
        runtime_check = "test -r /opt/xpra-fork-maintenance/interaction_fixture.py"
        self.assertIn(chmod, server_stage)
        self.assertIn(runtime_check, server_stage)
        self.assertIn("glmark2-wayland", server_stage)
        self.assertLess(server_stage.index(chmod), server_stage.index("USER lab"))
        self.assertGreater(server_stage.index(runtime_check), server_stage.index("USER lab"))

    def test_pixel_tolerance_is_scoped_to_the_owning_profile(self) -> None:
        self.assertEqual(live_run.pixel_error_limit("zed", "rgb"), 0.0)
        self.assertEqual(live_run.pixel_error_limit("clipboard", "rgb"), 1.0)
        self.assertEqual(live_run.pixel_error_limit("gtk", "rgb"), 1.0)
        self.assertEqual(live_run.pixel_error_limit("keyboard", "rgb"), 1.0)
        self.assertEqual(live_run.pixel_error_limit("hardware", "h264"), 15.0)
        self.assertEqual(live_run.pixel_error_limit("opengl", "h264"), 15.0)
        self.assertEqual(live_run.pixel_error_limit("vkcube", "rgb"), 0.0)

    def test_clipboard_pixel_tolerance_rejects_structural_stale_region(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            source = live_run.Image.new("RGB", (100, 100), (0, 0, 0))
            observed = source.copy()
            observed.paste((6, 6, 6), (25, 25, 75, 75))
            source_path = directory / "source.png"
            direct_path = directory / "direct.png"
            focused_path = directory / "focused.png"
            source.save(source_path)
            observed.save(direct_path)
            observed.save(focused_path)

            evidence, _source_image = live_run.pixel_pipeline_evidence(
                directory,
                [source_path.name],
                direct_path,
                focused_path,
                live_run.pixel_error_limit("clipboard", "rgb"),
            )

        self.assertFalse(evidence["matching_server_frame"])
        self.assertGreater(
            evidence["comparisons"][0]["direct"]["mean_absolute_error"],
            1.0,
        )

    def test_hardware_application_uses_observable_vulkan_boundaries(self) -> None:
        checks = live_run.application_boundary_checks(
            application="hardware",
            application_activity={
                "process_alive": True,
                "graphics_motion": {"changed": True},
            },
            application_gpu={
                "gpu_mappings": ["/usr/lib/libvulkan_radeon.so"],
                "render_nodes": ["/dev/dri/renderD128"],
            },
            log_evidence={
                "wayland_protocol": {
                    "ack_configure": 0,
                    "commits": 0,
                    "damage_buffer": 0,
                }
            },
            render_node=Path("/dev/dri/renderD128"),
        )
        self.assertTrue(all(checks.values()))
        self.assertNotIn("wayland_ack_configure", checks)
        checks["graphics_frames_changed"] = False
        self.assertFalse(all(checks.values()))

    def test_opengl_application_requires_live_hardware_context_and_driver(self) -> None:
        activity = {
            "process_alive": True,
            "graphics_motion": {"changed": True},
            "opengl": {
                "api": "OpenGL",
                "renderer": "AMD Radeon Graphics (radeonsi)",
                "source": "glmark2-wayland",
                "vendor": "AMD",
                "version": "4.6 (Core Profile) Mesa",
            },
        }
        gpu = {
            "gpu_mappings": ["/usr/lib/x86_64-linux-gnu/libgallium.so"],
            "render_nodes": ["/dev/dri/renderD128"],
        }
        checks = live_run.application_boundary_checks(
            application="opengl",
            application_activity=activity,
            application_gpu=gpu,
            log_evidence={"wayland_protocol": {}},
            render_node=Path("/dev/dri/renderD128"),
        )
        self.assertTrue(all(checks.values()))

        activity["opengl"] = {**activity["opengl"], "renderer": "llvmpipe"}
        software = live_run.application_boundary_checks(
            application="opengl",
            application_activity=activity,
            application_gpu=gpu,
            log_evidence={"wayland_protocol": {}},
            render_node=Path("/dev/dri/renderD128"),
        )
        self.assertFalse(software["opengl_hardware_renderer"])

    def test_opengl_renderer_evidence_is_exact_and_private(self) -> None:
        expected = {
            "api": "OpenGL",
            "renderer": "AMD Radeon Graphics (radeonsi)",
            "source": "glmark2-wayland",
            "vendor": "AMD",
            "version": "4.6 (Compatibility Profile) Mesa",
        }
        output = """
=======================================================
    glmark2 2023.01
=======================================================
    OpenGL Information
    GL_VENDOR:      AMD
    GL_RENDERER:    AMD Radeon Graphics (radeonsi)
    GL_VERSION:     4.6 (Compatibility Profile) Mesa
=======================================================
"""
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "opengl.stdout"
            path.write_text(output, encoding="utf-8")
            path.chmod(0o600)
            self.assertEqual(live_run.load_opengl_evidence(path), expected)

            path.write_text(
                output.replace("AMD\n", "AMD\n    GL_VENDOR: Intel\n"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(live_run.LabFailure, "invalid vendor"):
                live_run.load_opengl_evidence(path)

    def test_h264_only_packet_check_rejects_empty_and_rgb_windows(self) -> None:
        updates = {
            "count": 2,
            "encodings": ["h264"],
            "updates": [
                {"encoding": "h264", "payload_bytes": 10},
                {"encoding": "h264", "payload_bytes": 20},
            ],
        }
        self.assertTrue(live_run.only_positive_h264_packets(updates))
        updates["encodings"] = ["h264", "rgb32"]
        self.assertFalse(live_run.only_positive_h264_packets(updates))
        updates["encodings"] = ["h264"]
        updates["updates"][1]["payload_bytes"] = 0
        self.assertFalse(live_run.only_positive_h264_packets(updates))
        self.assertFalse(live_run.only_positive_h264_packets(None))

    def test_primary_hardware_accepts_only_exact_warmup_and_production(self) -> None:
        warmup = [
            {
                "encoding": "webp",
                "h": 1173,
                "options": {"flush": 0, "window-size": [796, 1173]},
                "payload_bytes": 40 + sequence,
                "relative_info": f"screen-updates/1/{100 + sequence}/0.info",
                "sequence": sequence,
                "w": 796,
                "x": 0,
                "y": 0,
            }
            for sequence in range(1, 9)
        ]
        production = [
            {
                "encoding": "h264",
                "h": 1172,
                "payload_bytes": 100,
                "relative_info": "screen-updates/1/109/0.info",
                "sequence": 9,
                "w": 796,
                "x": 0,
                "y": 0,
                "options": {
                    "frame": 0,
                    "flush": 0,
                    "type": "IDR",
                    "window-size": [796, 1173],
                },
            },
            {
                "encoding": "rgb24",
                "h": 1,
                "payload_bytes": 20,
                "relative_info": "screen-updates/1/110/0.info",
                "sequence": 10,
                "w": 796,
                "x": 0,
                "y": 1172,
                "options": {
                    "flush": 1,
                    "rgb_format": "RGBX",
                    "window-size": [796, 1173],
                },
            },
            {
                "encoding": "h264",
                "h": 1172,
                "payload_bytes": 90,
                "relative_info": "screen-updates/1/110/1.info",
                "sequence": 11,
                "w": 796,
                "x": 0,
                "y": 0,
                "options": {
                    "frame": 1,
                    "flush": 0,
                    "type": "P",
                    "window-size": [796, 1173],
                },
            },
            {
                "encoding": "rgb24",
                "h": 1,
                "payload_bytes": 20,
                "relative_info": "screen-updates/1/111/0.info",
                "sequence": 12,
                "w": 796,
                "x": 0,
                "y": 1172,
                "options": {
                    "flush": 1,
                    "rgb_format": "RGBX",
                    "window-size": [796, 1173],
                },
            },
            {
                "encoding": "h264",
                "h": 1172,
                "payload_bytes": 85,
                "relative_info": "screen-updates/1/111/1.info",
                "sequence": 13,
                "w": 796,
                "x": 0,
                "y": 0,
                "options": {
                    "frame": 2,
                    "flush": 0,
                    "type": "P",
                    "window-size": [796, 1173],
                },
            },
        ]
        updates = {
            "count": 13,
            "encodings": ["h264", "rgb24", "webp"],
            "h264_stimulus": {
                "baseline_sequence": 10,
                "first_sequence": 9,
                "last_sequence": 13,
                "window_size": [796, 1173],
            },
            "initial_pixel_format": "BGRX",
            "updates": [*warmup, *production],
            "window_id": 1,
        }
        self.assertEqual(
            live_run.primary_h264_packet_contract_name(
                "hardware", "adaptive-alpha"
            ),
            "alpha_safe_warmup_then_h264_with_only_lossless_rgb_edges",
        )
        for pixel_format in ("BGRX", "RGBX"):
            with self.subTest(pixel_format=pixel_format):
                candidate = {**updates, "initial_pixel_format": pixel_format}
                suffix = live_run.hardware_h264_production_updates(candidate)
                self.assertIsNotNone(suffix)
                assert suffix is not None
                self.assertEqual(
                    [packet["sequence"] for packet in suffix["updates"]],
                    [9, 10, 11, 12, 13],
                )
                self.assertFalse(
                    live_run.primary_h264_packets_valid(
                        "hardware", "adaptive-alpha", candidate
                    )
                )

        alpha_rgb32 = json.loads(json.dumps(updates))
        alpha_rgb32["updates"][0].update(
            {
                "encoding": "rgb32",
                "options": {
                    "flush": 0,
                    "rgb_format": "RGBA",
                    "window-size": [796, 1173],
                },
            }
        )
        alpha_rgb32["encodings"] = ["h264", "rgb24", "rgb32", "webp"]
        self.assertIsNotNone(
            live_run.hardware_h264_production_updates(alpha_rgb32)
        )

        invalid: dict[str, dict[str, object]] = {}
        for pixel_format in (None, "BGRA", "RGBA"):
            candidate = json.loads(json.dumps(updates))
            if pixel_format is None:
                candidate.pop("initial_pixel_format")
            else:
                candidate["initial_pixel_format"] = pixel_format
            invalid[f"initial pixel format {pixel_format!r}"] = candidate
        rgb24_warmup = json.loads(json.dumps(updates))
        rgb24_warmup["updates"][0]["encoding"] = "rgb24"
        invalid["rgb24 warmup"] = rgb24_warmup
        opaque_rgb32_warmup = json.loads(json.dumps(updates))
        opaque_rgb32_warmup["updates"][0].update(
            {"encoding": "rgb32", "options": {"rgb_format": "RGBX"}}
        )
        opaque_rgb32_warmup["encodings"] = [
            "h264",
            "rgb24",
            "rgb32",
            "webp",
        ]
        invalid["opaque rgb32 warmup"] = opaque_rgb32_warmup
        interior_rgb = json.loads(json.dumps(updates))
        interior_rgb["updates"][9]["y"] = 1171
        invalid["interior rgb suffix"] = interior_rgb
        webp_suffix = json.loads(json.dumps(updates))
        webp_suffix["updates"][9]["encoding"] = "webp"
        invalid["webp after h264"] = webp_suffix
        missing_production = json.loads(json.dumps(updates))
        missing_production["updates"] = missing_production["updates"][:8]
        missing_production["count"] = 8
        missing_production["encodings"] = ["webp"]
        invalid["missing h264 production"] = missing_production
        sequence_gap = json.loads(json.dumps(updates))
        sequence_gap["updates"].pop(3)
        sequence_gap["count"] = 12
        invalid["sequence gap"] = sequence_gap
        zero_payload = json.loads(json.dumps(updates))
        zero_payload["updates"][0]["payload_bytes"] = 0
        invalid["zero warmup payload"] = zero_payload
        for name, candidate in invalid.items():
            with self.subTest(name=name):
                self.assertIsNone(
                    live_run.hardware_h264_production_updates(candidate)
                )
                self.assertFalse(
                    live_run.primary_h264_packets_valid(
                        "hardware", "adaptive-alpha", candidate
                    )
                )

    def test_hardware_production_starts_at_first_h264_damage_group(self) -> None:
        edge = {
            "encoding": "rgb24",
            "h": 1,
            "payload_bytes": 20,
            "relative_info": "screen-updates/1/200/0.info",
            "sequence": 1,
            "w": 796,
            "x": 0,
            "y": 1172,
            "options": {
                "flush": 1,
                "rgb_format": "RGBX",
                "window-size": [796, 1173],
            },
        }
        h264 = {
            "encoding": "h264",
            "h": 1172,
            "payload_bytes": 100,
            "relative_info": "screen-updates/1/200/1.info",
            "sequence": 2,
            "w": 796,
            "x": 0,
            "y": 0,
            "options": {
                "flush": 0,
                "frame": 0,
                "type": "IDR",
                "window-size": [796, 1173],
            },
        }
        updates = {
            "count": 2,
            "encodings": ["h264", "rgb24"],
            "h264_stimulus": {
                "baseline_sequence": 1,
                "first_sequence": 1,
                "last_sequence": 2,
                "window_size": [796, 1173],
            },
            "initial_pixel_format": "BGRX",
            "updates": [edge, h264],
            "window_id": 1,
        }
        suffix = live_run.hardware_h264_production_updates(updates)
        self.assertIsNotNone(suffix)
        assert suffix is not None
        self.assertEqual(
            [packet["sequence"] for packet in suffix["updates"]],
            [1, 2],
        )

        dangling = json.loads(json.dumps(updates))
        dangling_edge = json.loads(json.dumps(edge))
        dangling_edge.update(
            {
                "relative_info": "screen-updates/1/199/0.info",
                "sequence": 1,
            }
        )
        dangling_edge["options"]["flush"] = 0
        for sequence, packet in enumerate(dangling["updates"], 2):
            packet["sequence"] = sequence
        dangling["updates"] = [dangling_edge, *dangling["updates"]]
        dangling["count"] = 3
        self.assertIsNone(live_run.hardware_h264_production_updates(dangling))

    def test_damage_flush_metadata_survives_millisecond_directory_collision(
        self,
    ) -> None:
        packets: list[dict[str, object]] = []
        for frame in range(2):
            sequence = frame * 2 + 1
            packets.extend(
                (
                    {
                        "encoding": "rgb24",
                        "h": 1,
                        "options": {
                            "flush": 1,
                            "rgb_format": "BGRX",
                            "window-size": [80, 101],
                        },
                        "payload_bytes": 10,
                        "relative_info": (
                            f"screen-updates/1/200/{frame * 2}.info"
                        ),
                        "sequence": sequence,
                        "w": 80,
                        "x": 0,
                        "y": 100,
                    },
                    {
                        "encoding": "h264",
                        "h": 100,
                        "options": {
                            "flush": 0,
                            "frame": frame,
                            "type": "IDR" if frame == 0 else "P",
                            "window-size": [80, 101],
                        },
                        "payload_bytes": 100,
                        "relative_info": (
                            f"screen-updates/1/200/{frame * 2 + 1}.info"
                        ),
                        "sequence": sequence + 1,
                        "w": 80,
                        "x": 0,
                        "y": 0,
                    },
                )
            )
        groups = live_run._ordered_saved_damage_groups(packets, 1)
        self.assertIsNotNone(groups)
        assert groups is not None
        self.assertEqual(
            [[packet["sequence"] for packet in group] for group in groups],
            [[1, 2], [3, 4]],
        )
        self.assertTrue(
            live_run.h264_with_lossless_rgb_edges(
                {
                    "count": 4,
                    "encodings": ["h264", "rgb24"],
                    "updates": packets,
                    "window_id": 1,
                }
            )
        )

    def test_hardware_phase_uses_saved_source_size_not_client_geometry(self) -> None:
        packets = [
            {
                "encoding": "h264",
                "h": 480,
                "options": {
                    "flush": 0,
                    "frame": 0,
                    "type": "IDR",
                    "window-size": [640, 480],
                },
                "payload_bytes": 100,
                "relative_info": "screen-updates/1/1000/0.info",
                "sequence": 1,
                "w": 640,
                "x": 0,
                "y": 0,
            },
            {
                "encoding": "h264",
                "h": 480,
                "options": {
                    "flush": 0,
                    "frame": 1,
                    "type": "P",
                    "window-size": [640, 480],
                },
                "payload_bytes": 90,
                "relative_info": "screen-updates/1/1125/0.info",
                "sequence": 2,
                "w": 640,
                "x": 0,
                "y": 0,
            },
        ]
        updates = {
            "count": 2,
            "encodings": ["h264"],
            "initial_pixel_format": "BGRX",
            "updates": packets,
            "window_id": 1,
        }

        def wait_for_once(
            description: str,
            predicate: object,
            *,
            timeout: int,
        ) -> None:
            self.assertEqual(description, "stable hardware H.264 phase baseline")
            self.assertEqual(timeout, 15)
            self.assertTrue(callable(predicate))
            assert callable(predicate)
            self.assertTrue(predicate())

        with (
            patch.object(
                live_run,
                "synchronize_saved_updates",
                return_value=updates,
            ),
            patch.object(live_run, "wait_for", side_effect=wait_for_once),
        ):
            interval = live_run.begin_hardware_h264_stimulus(
                "server",
                LIVE_DIRECTORY,
                1,
            )

        self.assertEqual(
            interval,
            {
                "baseline_sequence": 2,
                "first_sequence": 1,
                "window_size": [640, 480],
            },
        )

    def test_hardware_phase_excludes_safe_resize_epilogue_but_proves_va_stream(
        self,
    ) -> None:
        packets: list[dict[str, object]] = [
            {
                "encoding": "webp",
                "h": 64,
                "options": {"flush": 0, "window-size": [64, 64]},
                "payload_bytes": 20,
                "relative_info": "screen-updates/1/100/0.info",
                "sequence": 1,
                "w": 64,
                "x": 0,
                "y": 0,
            },
            {
                "encoding": "h264",
                "h": 64,
                "options": {
                    "flush": 0,
                    "frame": 0,
                    "type": "IDR",
                    "window-size": [64, 64],
                },
                "payload_bytes": 30,
                "relative_info": "screen-updates/1/101/0.info",
                "sequence": 2,
                "w": 64,
                "x": 0,
                "y": 0,
            },
            {
                "encoding": "webp",
                "h": 101,
                "options": {"flush": 0, "window-size": [80, 101]},
                "payload_bytes": 40,
                "relative_info": "screen-updates/1/102/0.info",
                "sequence": 3,
                "w": 80,
                "x": 0,
                "y": 0,
            },
        ]
        for frame in range(10):
            sequence = 4 + frame * 2
            group = 2000 + frame * 125
            packets.extend(
                (
                    {
                        "encoding": "rgb24",
                        "h": 1,
                        "options": {
                            "flush": 1,
                            "rgb_format": "BGRX",
                            "window-size": [80, 101],
                        },
                        "payload_bytes": 10,
                        "relative_info": (
                            f"screen-updates/1/{group}/0.info"
                        ),
                        "sequence": sequence,
                        "w": 80,
                        "x": 0,
                        "y": 100,
                    },
                    {
                        "encoding": "h264",
                        "h": 100,
                        "options": {
                            "flush": 0,
                            "frame": frame,
                            "type": "IDR" if frame == 0 else "P",
                            "window-size": [80, 101],
                        },
                        "payload_bytes": 100 + frame,
                        "relative_info": (
                            f"screen-updates/1/{group}/1.info"
                        ),
                        "sequence": sequence + 1,
                        "w": 80,
                        "x": 0,
                        "y": 0,
                    },
                )
            )
        packets.extend(
            (
                {
                    "encoding": "rgb24",
                    "h": 1,
                    "options": {
                        "flush": 1,
                        "rgb_format": "BGRX",
                        "window-size": [160, 101],
                    },
                    "payload_bytes": 10,
                    "relative_info": "screen-updates/1/4000/0.info",
                    "sequence": 24,
                    "w": 80,
                    "x": 0,
                    "y": 100,
                },
                {
                    "encoding": "h264",
                    "h": 100,
                    "options": {
                        "flush": 0,
                        "frame": 10,
                        "type": "P",
                        "window-size": [160, 101],
                    },
                    "payload_bytes": 80,
                    "relative_info": "screen-updates/1/4000/1.info",
                    "sequence": 25,
                    "w": 80,
                    "x": 0,
                    "y": 0,
                },
                {
                    "encoding": "webp",
                    "h": 101,
                    "options": {"flush": 0, "window-size": [160, 101]},
                    "payload_bytes": 50,
                    "relative_info": "screen-updates/1/4001/0.info",
                    "sequence": 26,
                    "w": 80,
                    "x": 80,
                    "y": 0,
                },
                {
                    "encoding": "rgb24",
                    "h": 1,
                    "options": {
                        "flush": 1,
                        "rgb_format": "BGRX",
                        "window-size": [160, 101],
                    },
                    "payload_bytes": 10,
                    "relative_info": "screen-updates/1/4002/0.info",
                    "sequence": 27,
                    "w": 80,
                    "x": 0,
                    "y": 100,
                },
                {
                    "encoding": "h264",
                    "h": 100,
                    "options": {
                        "flush": 0,
                        "frame": 11,
                        "type": "P",
                        "window-size": [160, 101],
                    },
                    "payload_bytes": 70,
                    "relative_info": "screen-updates/1/4002/1.info",
                    "sequence": 28,
                    "w": 80,
                    "x": 0,
                    "y": 0,
                },
                {
                    "encoding": "rgb24",
                    "h": 1,
                    "options": {
                        "flush": 1,
                        "rgb_format": "BGRX",
                        "window-size": [160, 101],
                    },
                    "payload_bytes": 10,
                    "relative_info": "screen-updates/1/4003/0.info",
                    "sequence": 29,
                    "w": 80,
                    "x": 0,
                    "y": 100,
                },
            )
        )
        updates = {
            "count": len(packets),
            "encodings": ["h264", "rgb24", "webp"],
            "h264_stimulus": {
                "baseline_sequence": 5,
                "first_sequence": 4,
                "last_sequence": 23,
                "window_size": [80, 101],
            },
            "initial_pixel_format": "BGRX",
            "updates": packets,
            "window_id": 1,
        }
        baseline = {
            **updates,
            "count": 23,
            "encodings": ["h264", "rgb24", "webp"],
            "updates": packets[:23],
        }
        self.assertEqual(
            live_run.hardware_h264_phase_start_sequence(baseline, (80, 101)),
            4,
        )
        self.assertTrue(live_run.hardware_h264_history_valid(updates))
        production = live_run.hardware_h264_production_updates(updates)
        self.assertIsNotNone(production)
        assert production is not None
        self.assertEqual(
            [production["updates"][0]["sequence"], production["updates"][-1]["sequence"]],
            [4, 23],
        )
        metrics = live_run.h264_production_metrics("hardware", updates)
        self.assertTrue(all(live_run.h264_dominance_checks(metrics).values()))
        self.assertEqual(live_run.h264_production_metrics("opengl", updates), metrics)
        context_updates = live_run.hardware_h264_context_updates(updates)
        self.assertIsNotNone(context_updates)
        assert context_updates is not None
        self.assertEqual(context_updates["updates"][-1]["sequence"], 28)

        def context(side: str, frames: int) -> dict[str, object]:
            return {
                "begin_successes": frames,
                "completed_frames": frames,
                "context": "0x5",
                "created": True,
                "end_successes": frames,
                "entrypoint": (
                    "VAEntrypointEncSlice" if side == "server" else "VAEntrypointVLD"
                ),
                "file": f"{side}-va.trace",
                "generation": 1,
                "height": 112,
                "incomplete_frames": 0,
                "profile": (
                    "VAProfileH264Main" if side == "server" else "VAProfileH264High"
                ),
                "render_successes": frames,
                "width": 80,
            }

        matched = live_run.match_h264_production_stream(
            context_updates,
            {"contexts": [context("server", 13)], "files": ["server-va.trace"]},
            {"contexts": [context("client", 12)], "files": ["client-va.trace"]},
            allow_alpha_gaps=True,
            allow_lossless_rgb_edges=True,
            allow_terminal_server_frame=True,
            allow_window_resize_gaps=True,
        )
        self.assertTrue(matched["all_streams_proven"])
        self.assertEqual(matched["matched_stream"]["packet_count"], 12)
        strict = live_run.match_h264_production_stream(
            context_updates,
            {"contexts": [context("server", 13)], "files": ["server-va.trace"]},
            {"contexts": [context("client", 12)], "files": ["client-va.trace"]},
            allow_alpha_gaps=True,
            allow_lossless_rgb_edges=True,
        )
        self.assertFalse(strict["all_streams_proven"])

        client_lagged = live_run.match_h264_production_stream(
            context_updates,
            {"contexts": [context("server", 12)], "files": ["server-va.trace"]},
            {"contexts": [context("client", 11)], "files": ["client-va.trace"]},
            allow_alpha_gaps=True,
            allow_lossless_rgb_edges=True,
            allow_terminal_client_frame=True,
            allow_window_resize_gaps=True,
        )
        self.assertTrue(client_lagged["all_streams_proven"])
        self.assertTrue(
            client_lagged["matched_stream"]["terminal_client_frame_inflight"]
        )
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            lines = []
            for packet in context_updates["updates"]:
                lines.append(
                    "process_draw: {payload_bytes} bytes for window 1, "
                    "sequence {sequence}, {w}x{h} at {x},{y} using {encoding} "
                    "encoding with options=typedict({options!r})".format(**packet)
                )
            (directory / "client.stdout").write_text(
                "\n".join(lines) + "\n",
                encoding="utf-8",
            )
            self.assertTrue(
                live_run.terminal_client_h264_frame_inflight(
                    directory,
                    context_updates,
                )
            )
            (directory / "client.stdout").write_text(
                "\n".join(lines[:-1]) + "\n",
                encoding="utf-8",
            )
            self.assertFalse(
                live_run.terminal_client_h264_frame_inflight(
                    directory,
                    context_updates,
                )
            )

    def test_adaptive_h264_requires_sustained_temporal_and_pixel_dominance(self) -> None:
        width, height = 800, 600
        warmup = {
            "encoding": "webp",
            "h": height,
            "options": {"flush": 0, "window-size": [width, height]},
            "payload_bytes": 50,
            "relative_info": "screen-updates/1/1000/0.info",
            "sequence": 1,
            "w": width,
            "x": 0,
            "y": 0,
        }
        h264_packets = []
        for frame in range(10):
            h264_packets.append(
                {
                    "encoding": "h264",
                    "h": height,
                    "options": {
                        "flush": 0,
                        "frame": frame,
                        "type": "IDR" if frame == 0 else "P",
                        "window-size": [width, height],
                    },
                    "payload_bytes": 100 + frame,
                    "relative_info": (
                        f"screen-updates/1/{2000 + frame * 125}/0.info"
                    ),
                    "sequence": frame + 2,
                    "w": width,
                    "x": 0,
                    "y": 0,
                }
            )
        updates = {
            "count": 11,
            "encodings": ["h264", "webp"],
            "initial_pixel_format": "BGRX",
            "updates": [warmup, *h264_packets],
            "window_id": 1,
        }
        for application in ("hardware", "zed"):
            with self.subTest(application=application):
                candidate = updates
                if application == "zed":
                    candidate = {
                        **updates,
                        "h264_stimulus": {
                            "baseline_sequence": 1,
                            "last_sequence": 11,
                            "window_size": [width, height],
                        },
                    }
                else:
                    candidate = {
                        **updates,
                        "h264_stimulus": {
                            "baseline_sequence": 2,
                            "first_sequence": 2,
                            "last_sequence": 11,
                            "window_size": [width, height],
                        },
                    }
                metrics = live_run.h264_production_metrics(application, candidate)
                self.assertEqual(metrics["h264_main_frame_count"], 10)
                self.assertEqual(metrics["h264_damage_span_ms"], 1125)
                self.assertEqual(metrics["h264_main_pixels"], 10 * width * height)
                self.assertEqual(
                    metrics["aggregate_encoded_pixels"], 10 * width * height
                )
                self.assertAlmostEqual(
                    metrics["aggregate_h264_pixel_ratio"], 1.0
                )
                self.assertTrue(all(live_run.h264_dominance_checks(metrics).values()))
                self.assertTrue(
                    live_run.primary_h264_packets_valid(
                        application,
                        "adaptive-alpha",
                        candidate,
                    )
                )

        hardware_updates = {
            **updates,
            "h264_stimulus": {
                "baseline_sequence": 2,
                "first_sequence": 2,
                "last_sequence": 11,
                "window_size": [width, height],
            },
        }
        metrics = live_run.h264_production_metrics("hardware", hardware_updates)
        matched = {
            "matched_stream": {
                "damage_span_ms": 1125,
                "packet_count": 10,
                "pixel_count": 10 * width * height,
            }
        }
        self.assertTrue(
            all(
                live_run.matched_h264_stream_stability_checks(
                    matched,
                    metrics,
                ).values()
            )
        )
        invalid_metrics = {
            **metrics,
            "aggregate_encoded_pixels": 2 * metrics["h264_main_pixels"],
            "h264_damage_span_ms": 999,
            "h264_main_frame_count": 9,
            "minimum_frame_h264_pixels": 98,
            "minimum_frame_window_pixels": 100,
        }
        self.assertFalse(all(live_run.h264_dominance_checks(invalid_metrics).values()))
        unmatched = {
            "matched_stream": {
                "damage_span_ms": 999,
                "packet_count": 9,
                "pixel_count": 8 * width * height,
            }
        }
        self.assertFalse(
            all(
                live_run.matched_h264_stream_stability_checks(
                    unmatched,
                    metrics,
                ).values()
            )
        )

    def test_zed_adaptive_h264_accepts_only_exact_alpha_groups_around_h264(self) -> None:
        def h264(sequence: int, group: int, frame: int) -> dict[str, object]:
            return {
                "encoding": "h264",
                "h": 600,
                "options": {
                    "flush": 0,
                    "frame": frame,
                    "type": "IDR" if frame == 0 else "P",
                    "window-size": [800, 600],
                },
                "payload_bytes": 100,
                "relative_info": f"screen-updates/1/{group}/0.info",
                "sequence": sequence,
                "w": 800,
                "x": 0,
                "y": 0,
            }

        alpha = {
            "encoding": "webp",
            "h": 600,
            "options": {"flush": 0, "window-size": [800, 600]},
            "payload_bytes": 80,
            "relative_info": "screen-updates/1/200/0.info",
            "sequence": 2,
            "w": 800,
            "x": 0,
            "y": 0,
        }
        updates = {
            "count": 3,
            "encodings": ["h264", "webp"],
            "initial_pixel_format": "BGRA",
            "updates": [h264(1, 100, 0), alpha, h264(3, 300, 1)],
            "window_id": 1,
        }
        production = live_run.adaptive_h264_production_updates(updates)
        self.assertIsNotNone(production)
        assert production is not None
        self.assertEqual(
            [packet["sequence"] for packet in production["updates"]],
            [1, 3],
        )
        self.assertEqual(len(live_run.h264_packet_streams(updates)), 2)
        streams = live_run.h264_packet_streams(
            updates,
            allow_alpha_gaps=True,
            allow_lossless_rgb_edges=True,
        )
        self.assertEqual(len(streams), 1)
        self.assertEqual(streams[0]["interleaved_alpha_sequences"], [2])

        alpha_rgb32 = json.loads(json.dumps(updates))
        alpha_rgb32["updates"][1].update(
            {
                "encoding": "rgb32",
                "options": {
                    "flush": 0,
                    "rgb_format": "RGBA",
                    "window-size": [800, 600],
                },
            }
        )
        alpha_rgb32["encodings"] = ["h264", "rgb32"]
        self.assertIsNotNone(
            live_run.adaptive_h264_production_updates(alpha_rgb32)
        )

        invalid = {
            "full rgb fallback": {
                **updates,
                "count": 1,
                "encodings": ["webp"],
                "updates": [alpha],
            },
            "rgb24 alpha group": {
                **updates,
                "encodings": ["h264", "rgb24"],
                "updates": [
                    updates["updates"][0],
                    {**alpha, "encoding": "rgb24"},
                    updates["updates"][2],
                ],
            },
            "opaque rgb32 alpha group": {
                **alpha_rgb32,
                "updates": [
                    alpha_rgb32["updates"][0],
                    {
                        **alpha_rgb32["updates"][1],
                        "options": {"flush": 0, "rgb_format": "RGBX"},
                    },
                    alpha_rgb32["updates"][2],
                ],
            },
            "alpha group missing window size": {
                **updates,
                "updates": [
                    updates["updates"][0],
                    {**alpha, "options": {"flush": 0}},
                    updates["updates"][2],
                ],
            },
            "alpha group outside window": {
                **updates,
                "updates": [
                    updates["updates"][0],
                    {**alpha, "w": 2, "x": 799},
                    updates["updates"][2],
                ],
            },
        }
        for name, candidate in invalid.items():
            with self.subTest(name=name):
                self.assertIsNone(
                    live_run.adaptive_h264_production_updates(candidate)
                )

    def test_auxiliary_alpha_packet_check_accepts_only_webp_or_alpha_rgb32(self) -> None:
        webp_packet = {
            "encoding": "webp",
            "h": 320,
            "options": {"flush": 0, "window-size": [480, 320]},
            "payload_bytes": 10,
            "relative_info": "screen-updates/2/100/0.info",
            "sequence": 1,
            "w": 480,
            "x": 0,
            "y": 0,
        }
        rgb32_packet = {
            "encoding": "rgb32",
            "h": 320,
            "options": {
                "flush": 0,
                "rgb_format": "BGRA",
                "window-size": [480, 320],
            },
            "payload_bytes": 20,
            "relative_info": "screen-updates/2/100/0.info",
            "sequence": 1,
            "w": 480,
            "x": 0,
            "y": 0,
        }
        webp = {
            "count": 1,
            "encodings": ["webp"],
            "initial_pixel_format": "RGBA",
            "updates": [webp_packet],
            "window_id": 2,
        }
        rgb32 = {
            "count": 1,
            "encodings": ["rgb32"],
            "initial_pixel_format": "BGRA",
            "updates": [rgb32_packet],
            "window_id": 2,
        }
        mixed_rgb32 = json.loads(json.dumps(rgb32_packet))
        mixed_rgb32.update(
            {
                "relative_info": "screen-updates/2/101/0.info",
                "sequence": 2,
            }
        )
        mixed = {
            "count": 2,
            "encodings": ["rgb32", "webp"],
            "initial_pixel_format": "RGBA",
            "updates": [webp_packet, mixed_rgb32],
            "window_id": 2,
        }
        for candidate in (webp, rgb32, mixed):
            with self.subTest(candidate=candidate["encodings"]):
                self.assertTrue(live_run.only_positive_alpha_capable_packets(candidate))

        invalid = {
            "empty": {
                "count": 0,
                "encodings": [],
                "initial_pixel_format": "RGBA",
                "updates": [],
            },
            "zero payload": {
                **webp,
                "updates": [{**webp_packet, "payload_bytes": 0}],
            },
            "h264": {
                **webp,
                "encodings": ["h264"],
                "updates": [{**webp_packet, "encoding": "h264"}],
            },
            "rgb24": {
                **webp,
                "encodings": ["rgb24"],
                "updates": [{**webp_packet, "encoding": "rgb24"}],
            },
            "opaque source": {**webp, "initial_pixel_format": "BGRX"},
            "opaque rgb32 packet": {
                **rgb32,
                "updates": [
                    {
                        **rgb32_packet,
                        "options": {
                            **rgb32_packet["options"],
                            "rgb_format": "RGBX",
                        },
                    }
                ],
            },
            "missing rgb32 format": {
                **rgb32,
                "updates": [
                    {
                        **rgb32_packet,
                        "options": {
                            "flush": 0,
                            "window-size": [480, 320],
                        },
                    }
                ],
            },
            "missing window size": {
                **webp,
                "updates": [
                    {**webp_packet, "options": {"flush": 0}}
                ],
            },
            "outside window": {
                **webp,
                "updates": [{**webp_packet, "w": 2, "x": 479}],
            },
            "sequence gap": {
                **mixed,
                "updates": [webp_packet, {**mixed_rgb32, "sequence": 3}],
            },
            "bad saved index": {
                **webp,
                "updates": [
                    {
                        **webp_packet,
                        "relative_info": "screen-updates/2/100/1.info",
                    }
                ],
            },
            "bad flush": {
                **webp,
                "updates": [
                    {
                        **webp_packet,
                        "options": {
                            **webp_packet["options"],
                            "flush": 1,
                        },
                    }
                ],
            },
        }
        for name, candidate in invalid.items():
            with self.subTest(name=name):
                self.assertFalse(
                    live_run.only_positive_alpha_capable_packets(candidate)
                )
        self.assertFalse(live_run.only_positive_alpha_capable_packets(None))

    def test_saved_window_initial_pixel_format_is_exact_and_required(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            window = Path(raw) / "screen-updates" / "2"
            window.mkdir(parents=True)
            info = window / "window.info"
            info.write_text(json.dumps({"pixel-format": "BGRA"}), encoding="utf-8")
            self.assertEqual(
                live_run.saved_window_initial_pixel_format(Path(raw), 2), "BGRA"
            )
            info.write_text(json.dumps({"pixel-format": ""}), encoding="utf-8")
            with self.assertRaisesRegex(live_run.LabFailure, "no pixel format"):
                live_run.saved_window_initial_pixel_format(Path(raw), 2)

    def test_transport_encoding_options(self) -> None:
        configured = live_run.live_config.load_live_cli()
        for role, role_config in configured.items():
            for encoding, transport in role_config["transports"].items():
                for policy in transport["policies"]:
                    with self.subTest(
                        role=role,
                        encoding=encoding,
                        policy=policy,
                    ):
                        self.assertEqual(
                            live_run.transport_encoding_options(
                                encoding,
                                policy,
                                client=role == "client",
                            ),
                            list(
                                live_run.live_config.transport_options(
                                    role,
                                    encoding,
                                    policy,
                                )
                            ),
                        )
        with self.assertRaisesRegex(live_run.LabFailure, "H.264 policies require"):
            live_run.transport_encoding_options("rgb", "fallback-auto", client=True)

    def test_picture_fallback_requires_rgb_only_production_evidence(self) -> None:
        logs = {
            "client_draw_regions": 4,
            "client_successful_paints": 4,
            "h264_draw_regions": 0,
            "h264_per_window_negotiation_applied": True,
            "h264_pipeline_errors": [],
            "h264_pre_negotiation_errors": [],
            "opengl_presentations": 4,
            "opengl_renderer": "AMD Radeon Graphics",
            "opengl_rgb_paints": 4,
            "paint_errors": [],
            "rgb_encodes": 4,
            "server_initial_data_errors": [],
            "server_logging_errors": 0,
        }
        updates = {
            "count": 2,
            "encodings": ["rgb32"],
            "updates": [
                {"encoding": "rgb32", "payload_bytes": 10},
                {"encoding": "rgb32", "payload_bytes": 20},
            ],
        }
        hardware = {
            "client_desktop": {"hardware_renderer": True},
            "client_graphics_process": {
                "gpu_mappings": ["/usr/lib/dri/radeonsi_dri.so"],
                "render_nodes": ["/dev/dri/renderD128"],
            },
        }
        boundaries = live_run.classify_h264_picture_fallback(
            policy="fallback-h264",
            render_node=Path("/dev/dri/renderD128"),
            log_evidence=logs,
            updates=updates,
            codec_hardware=hardware,
        )
        self.assertTrue(all(all(values.values()) for values in boundaries))

        updates["encodings"] = ["h264", "rgb32"]
        updates["updates"].append({"encoding": "h264", "payload_bytes": 30})
        boundaries = live_run.classify_h264_picture_fallback(
            policy="fallback-h264",
            render_node=Path("/dev/dri/renderD128"),
            log_evidence=logs,
            updates=updates,
            codec_hardware=hardware,
        )
        self.assertFalse(boundaries[1]["only_picture_packets"])
        self.assertFalse(boundaries[1]["h264_not_reached"])


class H264EvidenceTest(unittest.TestCase):
    @staticmethod
    def packet(sequence: int, frame: int, *, scaled: bool = True) -> dict[str, object]:
        options: dict[str, object] = {
            "frame": frame,
            "type": "IDR" if frame == 0 else "P",
        }
        if scaled:
            options["scaled_size"] = [1064, 780]
        return {
            "encoding": "h264",
            "sequence": sequence,
            "x": 0,
            "y": 0,
            "w": 1596,
            "h": 1172,
            "payload_bytes": 100,
            "options": options,
        }

    @staticmethod
    def context(entrypoint: str) -> dict[str, object]:
        return {
            "completed_frames": 2,
            "context": "0x1",
            "created": True,
            "entrypoint": entrypoint,
            "file": f"trace-{entrypoint}",
            "generation": 1,
            "height": 784,
            "incomplete_frames": 0,
            "profile": "VAProfileH264Main",
            "width": 1072,
        }

    def adaptive_edge_updates(self) -> dict[str, object]:
        first = self.packet(1, 0)
        second = self.packet(3, 1)
        first["options"].update({"flush": 0, "window-size": [1596, 1173]})
        first["relative_info"] = "screen-updates/1/100/0.info"
        second["options"].update({"flush": 0, "window-size": [1596, 1173]})
        second["relative_info"] = "screen-updates/1/101/1.info"
        edge = {
            "encoding": "rgb24",
            "relative_info": "screen-updates/1/101/0.info",
            "sequence": 2,
            "x": 0,
            "y": 1172,
            "w": 1596,
            "h": 1,
            "payload_bytes": 64,
            "options": {
                "flush": 1,
                "rgb_format": "RGBX",
                "window-size": [1596, 1173],
            },
        }
        return {
            "count": 3,
            "encodings": ["h264", "rgb24"],
            "updates": [first, edge, second],
            "window_id": 1,
        }

    def test_adaptive_h264_accepts_only_exact_lossless_codec_edges(self) -> None:
        updates = self.adaptive_edge_updates()
        self.assertTrue(live_run.h264_with_lossless_rgb_edges(updates))
        adaptive_updates = {**updates, "initial_pixel_format": "BGRX"}
        self.assertIsNotNone(
            live_run.adaptive_h264_production_updates(adaptive_updates)
        )
        self.assertFalse(
            live_run.primary_h264_packets_valid(
                "zed", "adaptive-alpha", adaptive_updates
            )
        )
        hardware_updates = {**updates, "initial_pixel_format": "BGRX"}
        self.assertFalse(
            live_run.primary_h264_packets_valid(
                "hardware", "adaptive-alpha", hardware_updates
            )
        )

        invalid_changes = {
            "alpha format": ("options", "rgb_format", "RGBA"),
            "full frame": (None, "h", 1173),
            "interior row": (None, "y", 1171),
            "missing flush": ("options", "flush", 0),
            "two pixel edge": (None, "h", 2),
            "zero payload": (None, "payload_bytes", 0),
        }
        for name, (parent, field, value) in invalid_changes.items():
            with self.subTest(name=name):
                candidate = json.loads(json.dumps(updates))
                edge = candidate["updates"][1]
                target = edge[parent] if parent else edge
                target[field] = value
                self.assertFalse(live_run.h264_with_lossless_rgb_edges(candidate))

        missing_edge = json.loads(json.dumps(updates))
        missing_edge["updates"].pop(1)
        missing_edge["count"] = 2
        missing_edge["encodings"] = ["h264"]
        self.assertFalse(live_run.h264_with_lossless_rgb_edges(missing_edge))

        picture_fallback = json.loads(json.dumps(updates))
        picture_fallback["updates"] = [picture_fallback["updates"][1]]
        picture_fallback["count"] = 1
        picture_fallback["encodings"] = ["rgb24"]
        self.assertFalse(live_run.h264_with_lossless_rgb_edges(picture_fallback))

    def test_h264_damage_groups_require_exact_indexes_flushes_and_edges(self) -> None:
        updates = self.adaptive_edge_updates()
        invalid: dict[str, dict[str, object]] = {}

        dangling = json.loads(json.dumps(updates))
        dangling["updates"].pop()
        dangling["count"] = 2
        invalid["dangling edge"] = dangling

        different_parent = json.loads(json.dumps(updates))
        different_parent["updates"][2]["relative_info"] = (
            "screen-updates/1/102/0.info"
        )
        invalid["edge and h264 in different groups"] = different_parent

        bad_index = json.loads(json.dumps(updates))
        bad_index["updates"][2]["relative_info"] = "screen-updates/1/101/2.info"
        invalid["noncontiguous group index"] = bad_index

        missing_index = json.loads(json.dumps(updates))
        missing_index["updates"][1].pop("relative_info")
        invalid["missing group index"] = missing_index

        bad_flush = json.loads(json.dumps(updates))
        bad_flush["updates"][1]["options"]["flush"] = 2
        invalid["non-descending flush"] = bad_flush

        edge_after_h264 = json.loads(json.dumps(updates))
        edge_packet = edge_after_h264["updates"][1]
        h264_packet = edge_after_h264["updates"][2]
        edge_packet["relative_info"] = "screen-updates/1/101/1.info"
        edge_packet["sequence"] = 3
        edge_packet["options"]["flush"] = 0
        h264_packet["relative_info"] = "screen-updates/1/101/0.info"
        h264_packet["sequence"] = 2
        h264_packet["options"]["flush"] = 1
        edge_after_h264["updates"][1:3] = [h264_packet, edge_packet]
        invalid["edge after terminal h264"] = edge_after_h264

        for name, candidate in invalid.items():
            with self.subTest(name=name):
                self.assertFalse(live_run.h264_with_lossless_rgb_edges(candidate))

        right = {
            "encoding": "rgb24",
            "sequence": 1,
            "x": 1596,
            "y": 0,
            "w": 1,
            "h": 1173,
            "payload_bytes": 64,
            "relative_info": "screen-updates/1/300/0.info",
            "options": {
                "flush": 2,
                "rgb_format": "RGBX",
                "window-size": [1597, 1173],
            },
        }
        bottom = {
            "encoding": "rgb24",
            "sequence": 2,
            "x": 0,
            "y": 1172,
            "w": 1597,
            "h": 1,
            "payload_bytes": 64,
            "relative_info": "screen-updates/1/300/1.info",
            "options": {
                "flush": 1,
                "rgb_format": "RGBX",
                "window-size": [1597, 1173],
            },
        }
        h264 = self.packet(3, 0, scaled=False)
        h264.update(
            {
                "relative_info": "screen-updates/1/300/2.info",
                "options": {
                    "flush": 0,
                    "frame": 0,
                    "type": "IDR",
                    "window-size": [1597, 1173],
                },
            }
        )
        both_edges = {
            "count": 3,
            "encodings": ["h264", "rgb24"],
            "updates": [right, bottom, h264],
            "window_id": 1,
        }
        self.assertTrue(live_run.h264_with_lossless_rgb_edges(both_edges))

        duplicate = json.loads(json.dumps(both_edges))
        duplicate["updates"][0].update(
            {"x": 0, "y": 1172, "w": 1597, "h": 1}
        )
        self.assertFalse(live_run.h264_with_lossless_rgb_edges(duplicate))

        mismatched_crop = json.loads(json.dumps(updates))
        mismatched_crop["updates"][0]["w"] = 1595
        mismatched_crop["updates"][0]["options"]["window-size"] = [1596, 1173]
        self.assertFalse(live_run.h264_with_lossless_rgb_edges(mismatched_crop))

    def test_adaptive_h264_stream_allows_only_verified_edge_interleaving(self) -> None:
        updates = self.adaptive_edge_updates()
        strict_streams = live_run.h264_packet_streams(updates)
        self.assertEqual(len(strict_streams), 2)

        streams = live_run.h264_packet_streams(
            updates,
            allow_lossless_rgb_edges=True,
        )
        self.assertEqual(len(streams), 1)
        self.assertTrue(streams[0]["contiguous_frames"])
        self.assertTrue(streams[0]["contiguous_sequences"])
        self.assertEqual(streams[0]["packet_sequences"], [1, 3])
        self.assertEqual(streams[0]["interleaved_edge_sequences"], [2])
        self.assertEqual(streams[0]["transport_sequences"], [1, 2, 3])

        result = live_run.match_h264_production_stream(
            updates,
            {"contexts": [self.context("VAEntrypointEncSlice")]},
            {"contexts": [self.context("VAEntrypointVLD")]},
            allow_lossless_rgb_edges=True,
        )
        self.assertTrue(result["production_proven"])

        invalid = json.loads(json.dumps(updates))
        invalid["updates"][1]["y"] = 1171
        streams = live_run.h264_packet_streams(
            invalid,
            allow_lossless_rgb_edges=True,
        )
        self.assertEqual(len(streams), 2)

    def test_adaptive_alpha_phases_require_all_h264_streams_to_be_va_proven(
        self,
    ) -> None:
        first = self.packet(1, 0)
        second = self.packet(3, 0)
        for group, packet in ((100, first), (102, second)):
            packet["relative_info"] = f"screen-updates/1/{group}/0.info"
            packet["options"].update(
                {"flush": 0, "window-size": [1596, 1173]}
            )
        alpha = {
            "encoding": "webp",
            "h": 1173,
            "options": {"flush": 0, "window-size": [1596, 1173]},
            "payload_bytes": 50,
            "relative_info": "screen-updates/1/101/0.info",
            "sequence": 2,
            "w": 1596,
            "x": 0,
            "y": 0,
        }
        updates = {
            "count": 3,
            "encodings": ["h264", "webp"],
            "initial_pixel_format": "BGRA",
            "updates": [first, alpha, second],
            "window_id": 1,
        }
        contexts = {
            "server": {"contexts": [self.context("VAEntrypointEncSlice")]},
            "client": {"contexts": [self.context("VAEntrypointVLD")]},
        }
        result = live_run.match_h264_production_stream(
            updates,
            contexts["server"],
            contexts["client"],
            allow_alpha_gaps=True,
            allow_lossless_rgb_edges=True,
        )
        self.assertEqual(len(result["candidates"]), 2)
        self.assertEqual(len(result["complete_streams"]), 2)
        self.assertTrue(result["all_streams_proven"])

        insufficient = json.loads(json.dumps(contexts))
        insufficient["server"]["contexts"][0]["completed_frames"] = 1
        rejected = live_run.match_h264_production_stream(
            updates,
            insufficient["server"],
            insufficient["client"],
            allow_alpha_gaps=True,
            allow_lossless_rgb_edges=True,
        )
        self.assertFalse(rejected["all_streams_proven"])

    def test_scaled_stream_matches_scaled_va_surfaces(self) -> None:
        updates = {"updates": [self.packet(3, 0), self.packet(4, 1)]}
        streams = live_run.h264_packet_streams(updates)
        self.assertEqual(streams[0]["coded_size"], [1596, 1172])
        self.assertEqual(streams[0]["encoded_size"], [1064, 780])
        self.assertEqual(streams[0]["surface_size"], [1072, 784])

        result = live_run.match_h264_production_stream(
            updates,
            {"contexts": [self.context("VAEntrypointEncSlice")]},
            {"contexts": [self.context("VAEntrypointVLD")]},
        )
        self.assertTrue(result["production_proven"])
        self.assertEqual(result["matched_stream"]["first_sequence"], 3)

    def test_unscaled_stream_keeps_coded_dimensions(self) -> None:
        updates = {"updates": [self.packet(1, 0, scaled=False)]}
        stream = live_run.h264_packet_streams(updates)[0]
        self.assertEqual(stream["encoded_size"], [1596, 1172])
        self.assertEqual(stream["surface_size"], [1600, 1184])

    def test_scaled_packet_chain_uses_encoded_decoder_dimensions(self) -> None:
        packet = self.packet(3, 0)
        packet["payload_sha256"] = "a" * 64
        updates = {"updates": [packet], "window_id": 1}
        callback = "0x7f8bf8bdb920"
        options = "{'frame': 0, 'type': 'IDR', 'scaled_size': (1064, 780)}"
        log = "\n".join(
            (
                (
                    "process_draw: 100 <class 'memoryview'> for window 1, "
                    "sequence 3, 1596x1172 at 0,0 using h264 encoding "
                    f"with options=typedict({options})"
                ),
                (
                    "draw_region(0, 0, 1596, 1172, h264, 100 bytes, 0, "
                    f"typedict({options}), [<function "
                    "WindowDraw._do_draw.<locals>.record_decode_time "
                    f"at {callback}>"
                ),
                "choose_decoder([libva(YUV420P - h264)])=libva(YUV420P - h264)",
                "paint_with_video_decoder: new libva('h264', 1064, 780, 'YUV420P')",
                "libva decoded h264 100 bytes into 1064x780 NV12",
                (
                    "do_video_paint('h264', ImageWrapper(NV12:"
                    "(0, 0, 1064, 780, 24):PLANAR_2), "
                    f"record_decode_time at {callback}>"
                ),
                "record_decode_time(True, ) wid=0x1, h264: 1596x1172, 8.4ms",
                "sending ack: ('window-ack', 1, 1596, 1172, 3, 8468, \"''\")",
                (
                    "process_draw: 50 bytes for window 2, sequence 4, "
                    "480x357 at 0,0 using webp encoding with options="
                    "typedict({'flush': 0})"
                ),
                "sending ack: ('window-ack', 2, 480, 357, 4, 100, \"''\")",
                (
                    "process_draw: 40 bytes for window 1, sequence 4, "
                    "1596x1172 at 0,0 using h264 encoding with options="
                    "typedict({'frame': 1, 'type': 'P'})"
                ),
                "do_present_fbo(GLXWindowContext(0x400015)) will blit [(0, 0, 1596, 1173)]",
                "1.do_gl_show(GLDrawingArea(1, (1596, 1173))) swapping buffers now",
                "GLDrawingArea(1, (1596, 1173)).do_present_fbo() done",
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "client.stdout").write_text(log + "\n", encoding="utf-8")
            result = live_run.h264_client_packet_chain(
                directory,
                updates,
                {"first_sequence": 3},
            )
        self.assertTrue(result["complete"])
        self.assertEqual(result["size"], [1596, 1172])
        self.assertEqual(result["encoded_size"], [1064, 780])


class LiveFixtureBuildOrderTest(unittest.TestCase):
    FIXTURES = (
        (
            "server",
            (
                ("empty_damage_fixture.c", "xpra-empty-damage-fixture"),
                ("subsurface_fixture.c", "xpra-subsurface-fixture"),
            ),
        ),
        ("client", (("xkb_xtest_driver.c", "xpra-xkb-xtest-driver"),)),
    )

    def build_instructions(self, role: str) -> list[str]:
        recipe = (LIVE_DIRECTORY / "Containerfile").read_text(encoding="utf-8")
        stage = re.search(
            rf"(?ms)^FROM [^\n]+ AS {role}-build\n(.*?)(?=^FROM |\Z)",
            recipe,
        )
        self.assertIsNotNone(stage, role)
        assert stage is not None
        return [
            line.strip()
            for line in stage[1].replace("\\\n", "").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

    def assert_fixture_boundary(self, role: str, instructions: list[str]) -> None:
        def position(prefix: str) -> int:
            matches = [
                index
                for index, instruction in enumerate(instructions)
                if instruction.startswith(prefix)
            ]
            self.assertEqual(len(matches), 1, (role, prefix))
            return matches[0]

        source = position("COPY --from=xpra-source /src/xpra /src/xpra")
        install = position("RUN python3 setup.py install ")
        native_tests = position("RUN package_dir=")
        self.assertLess(source, install)
        self.assertLess(install, native_tests)
        for filename, executable in dict(self.FIXTURES)[role]:
            copied = position(f"COPY {filename} /tmp/{executable}.c")
            compiled = [
                index
                for index, instruction in enumerate(instructions)
                if instruction.startswith("RUN ")
                and f"-o /opt/xpra-install/usr/local/bin/{executable}" in instruction
            ]
            self.assertEqual(len(compiled), 1)
            self.assertLess(
                native_tests,
                copied,
                "fixture COPY must not invalidate Xpra install or native checks",
            )
            self.assertLess(copied, compiled[0])
            self.assertIn("cc -std=c11 -O2 -Wall -Wextra -Werror", instructions[compiled[0]])
            self.assertIn(f"/tmp/{executable}.c", instructions[compiled[0]])

    def test_server_c_fixtures_follow_xpra_install_and_native_checks(self) -> None:
        self.assert_fixture_boundary("server", self.build_instructions("server"))

    def test_client_c_fixture_follows_xpra_install_and_native_checks(self) -> None:
        self.assert_fixture_boundary("client", self.build_instructions("client"))

    def test_rejects_fixture_input_or_compilation_before_native_checks(self) -> None:
        for role, fixtures in self.FIXTURES:
            for filename, executable in fixtures:
                for moved_kind in ("COPY", "RUN"):
                    with self.subTest(role=role, fixture=filename, moved=moved_kind):
                        instructions = self.build_instructions(role)
                        moved = next(
                            instruction
                            for instruction in instructions
                            if (
                                instruction.startswith(f"COPY {filename} ")
                                if moved_kind == "COPY"
                                else instruction.startswith("RUN ")
                                and f"-o /opt/xpra-install/usr/local/bin/{executable}" in instruction
                            )
                        )
                        instructions.remove(moved)
                        install = next(
                            index
                            for index, instruction in enumerate(instructions)
                            if instruction.startswith("RUN python3 setup.py install ")
                        )
                        instructions.insert(install, moved)
                        with self.assertRaises(AssertionError):
                            self.assert_fixture_boundary(role, instructions)


class PacketSequenceLedgerTest(unittest.TestCase):
    @staticmethod
    def global_windows() -> dict[int, dict[str, object]]:
        # Two ordinary roots share the WSSO connection allocator. Codec frame
        # numbers and saved damage-group indices remain per primary window.
        primary = H264EvidenceTest().adaptive_edge_updates()
        primary["initial_pixel_format"] = "BGRX"
        for packet, sequence in zip(primary["updates"], (2, 4, 5), strict=True):
            packet["sequence"] = sequence
            packet["payload_sha256"] = "a" * 64
        auxiliary = {
            "count": 2,
            "window_id": 2,
            "encodings": ["webp"],
            "initial_pixel_format": "BGRA",
            "updates": [
                {
                    "encoding": "webp", "sequence": sequence,
                    "relative_info": f"screen-updates/2/{100 + index}/0.info",
                    "x": 0, "y": 0, "w": 64, "h": 64,
                    "payload_bytes": 32, "payload_sha256": "b" * 64,
                    "options": {"flush": 0, "window-size": [64, 64]},
                }
                for index, sequence in enumerate((1, 3))
            ],
        }
        authority = {
            "schema": 1, "namespace": "connection-v1",
            "source_selection_sha256": "c" * 64,
            "connection": "client.0", "connection_uuid": "owned-connection",
            "run_id": "sequence-control", "server_uuid": "owned-server",
            "client_session_id": "owned-session", "connection_time": 100,
            "endpoint": "owned-endpoint",
            "server_info_sha256": "d" * 64,
            "window_ids": [1, 2], "next_packet_sequence": 6,
        }
        rows = []
        for window in (primary, auxiliary):
            for packet in window["updates"]:
                rows.append({
                    "sequence": packet["sequence"],
                    "window_id": window["window_id"],
                    "relative_info": packet["relative_info"],
                    "payload_bytes": packet["payload_bytes"],
                    "payload_sha256": packet["payload_sha256"],
                    "packet_sha256": hashlib.sha256(json.dumps(
                        packet, sort_keys=True, separators=(",", ":"),
                    ).encode()).hexdigest(),
                })
        ledger = {
            "schema": 1, "authority": authority, "frontier": 6,
            "packets": sorted(rows, key=lambda row: row["sequence"]),
        }
        for window in (primary, auxiliary):
            window["packet_sequence_ledger"] = ledger
            window["packet_sequence_span"] = [1, 5]
        return {1: primary, 2: auxiliary}

    @classmethod
    def global_updates(cls) -> dict[str, object]:
        return cls.global_windows()[1]

    @staticmethod
    def write_info(directory: Path, *, global_mode: bool = True) -> Path:
        lines = [
            "uuid=owned-server", "client.0.uuid=stable-client-uuid",
            "client.0.session-id=owned-session", "client.0.connection_time=100",
            "client.0.connection.endpoint=owned-endpoint",
            "client.0.connection.active=True", "client.0.connection.closed=False",
            "windows.1.title=primary", "windows.2.title=auxiliary",
        ]
        if global_mode:
            lines += ["client.0.window.damage.next-packet-sequence=6", "client.0.window.damage.ack-owners=0"]
        path = directory / "server-info.txt"
        path.write_text("\n".join(lines) + "\n")
        return path

    @staticmethod
    def authority(info: Path, *, global_mode: bool = True) -> dict[str, object]:
        return live_run.packet_sequence_authority(
            info, run_id="sequence-control",
            selected_case_slugs=("wayland-subsurface-stream-ownership",) if global_mode else (),
            selection_sha256="c" * 64, expected_window_ids=(1, 2),
        )

    @classmethod
    def write_packets(cls, directory: Path) -> dict[int, dict[str, object]]:
        windows = cls.global_windows()
        for wid, window in windows.items():
            for packet in window["updates"]:
                path = directory / packet["relative_info"]
                path.parent.mkdir(parents=True, exist_ok=True)
                payload_name = path.with_suffix(f'.{packet["encoding"]}').name
                payload = f"owned packet {wid}/{packet['sequence']}".encode()
                path.with_name(payload_name).write_bytes(payload)
                raw = {key: value for key, value in packet.items() if key not in {
                    "relative_info", "payload_bytes", "payload_sha256",
                }}
                raw["file"] = payload_name
                path.write_text(json.dumps(raw))
            (directory / "screen-updates" / str(wid) / "window.info").write_text(
                json.dumps({"pixel-format": window["initial_pixel_format"]})
            )
        result = {}
        for wid in windows:
            result[wid] = live_run.parse_saved_updates(directory, wid)
            result[wid]["initial_pixel_format"] = windows[wid]["initial_pixel_format"]
        return result

    def test_global_primary_gaps_are_exact_auxiliary_owners_not_packet_loss(self) -> None:
        updates = self.global_updates()
        self.assertEqual([p["sequence"] for p in updates["updates"]], [2, 4, 5])
        self.assertTrue(live_run.primary_h264_frame_ready(
            "hardware", "adaptive-alpha", updates,
        ))
        self.assertTrue(live_run.h264_with_lossless_rgb_edges(updates))

    def test_global_ledger_rejects_missing_duplicate_foreign_and_changed_packets(self) -> None:
        def delete_row(value):
            value["packet_sequence_ledger"]["packets"].pop(2)

        changes = {
            "missing global owner": delete_row,
            "duplicate ID": lambda value: value["packet_sequence_ledger"]["packets"][2].update(sequence=2),
            "foreign WID": lambda value: value["packet_sequence_ledger"]["packets"][2].update(window_id=3),
            "cross-window path": lambda value: value["packet_sequence_ledger"]["packets"][2].update(relative_info="screen-updates/1/101/0.info"),
            "wrong payload digest": lambda value: value["updates"][0].update(payload_sha256="f" * 64),
            "changed geometry": lambda value: value["updates"][0].update(w=1200),
            "lost leading primary": lambda value: (value["updates"].pop(0), value.update(count=2)),
            "lost trailing primary": lambda value: (value["updates"].pop(), value.update(count=2)),
            "incorrect namespace": lambda value: value["packet_sequence_ledger"]["authority"].update(namespace="window-v1"),
            "unbound span": lambda value: value.pop("packet_sequence_span"),
            "bad frontier": lambda value: value["packet_sequence_ledger"].update(frontier=7),
        }
        for name, change in changes.items():
            with self.subTest(name=name):
                updates = self.global_updates()
                change(updates)
                self.assertFalse(live_run.primary_h264_frame_ready("hardware", "adaptive-alpha", updates))
                self.assertFalse(live_run.h264_with_lossless_rgb_edges(updates))

    def test_legacy_namespace_does_not_infer_global_ownership_from_gaps(self) -> None:
        updates = self.global_updates()
        updates.pop("packet_sequence_ledger")
        updates.pop("packet_sequence_span")
        self.assertFalse(live_run.primary_h264_frame_ready("hardware", "adaptive-alpha", updates))
        legacy = H264EvidenceTest().adaptive_edge_updates()
        legacy["initial_pixel_format"] = "BGRX"
        self.assertTrue(live_run.primary_h264_frame_ready("hardware", "adaptive-alpha", legacy))

    def test_namespace_requires_source_and_owned_connection_runtime_agreement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            info = self.write_info(directory)
            authority = self.authority(info)
            self.assertEqual(authority["namespace"], "connection-v1")
            self.assertEqual(authority["server_info_sha256"], hashlib.sha256(info.read_bytes()).hexdigest())
            self.assertEqual(authority["client_session_id"], "owned-session")
            self.assertEqual(authority["server_uuid"], "owned-server")
            with self.assertRaises(live_run.LabFailure):
                self.authority(info, global_mode=False)
            info = self.write_info(directory, global_mode=False)
            self.assertEqual(self.authority(info, global_mode=False)["namespace"], "window-v1")
            with self.assertRaises(live_run.LabFailure):
                self.authority(info)
            original = self.write_info(directory).read_text()
            for label, text in (
                ("missing session", original.replace("client.0.session-id=owned-session\n", "")),
                ("closed", original.replace("connection.closed=False", "connection.closed=True")),
                ("duplicate counter", original + "client.0.window.damage.next-packet-sequence=6\n"),
                ("extra peer", original + "client.1.uuid=other-client\n"),
                ("extra window", original + "windows.3.title=unowned\n"),
            ):
                with self.subTest(label=label):
                    info.write_text(text)
                    with self.assertRaises(live_run.LabFailure):
                        self.authority(info)

    def test_raw_packet_bytes_bind_prefix_and_final_history_without_renumbering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            authority = self.authority(self.write_info(directory))
            windows = self.write_packets(directory)
            before = copy.deepcopy(windows)
            bound = live_run.bind_packet_sequence_ledger(windows, authority)
            self.assertEqual(windows, before)
            self.assertEqual([packet["sequence"] for packet in bound[1]["updates"]], [2, 4, 5])
            self.assertTrue(live_run.primary_h264_frame_ready("hardware", "adaptive-alpha", bound[1]))
            self.assertTrue(live_run.only_positive_alpha_capable_packets(bound[2]))
            with patch.object(live_run, "synchronize_saved_updates", return_value=windows[2]):
                snapshot = live_run.synchronize_packet_sequence_projection(
                    "server", directory, 1, authority, primary=windows[1],
                )
            live_run.retain_packet_sequence_observation(directory, "readiness", snapshot)
            live_run.validate_packet_sequence_observations(directory, bound)
            first = directory / bound[1]["updates"][0]["relative_info"]
            payload = first.parent / json.loads(first.read_text())["file"]
            payload.write_bytes(b"changed owned bytes")
            changed = live_run.parse_saved_updates(directory, 1)
            changed["initial_pixel_format"] = "BGRX"
            changed_bound = live_run.bind_packet_sequence_ledger({1: changed, 2: windows[2]}, authority)
            with self.assertRaises(live_run.LabFailure):
                live_run.validate_packet_sequence_observations(directory, changed_bound)

    def test_incremental_snapshot_seals_only_complete_prefix_and_preserves_tail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            authority = self.authority(self.write_info(directory))
            windows = self.write_packets(directory)
            partial = copy.deepcopy(windows[1])
            partial["updates"].pop()
            partial["count"] = 2
            with patch.object(live_run, "synchronize_saved_updates", return_value=windows[2]):
                snapshot = live_run.synchronize_packet_sequence_projection(
                    "server", directory, 1, authority, primary=partial,
                )
            self.assertEqual(snapshot["packet_sequence_ledger"]["frontier"], 3)
            self.assertEqual(snapshot["packet_sequence_observation"]["observed_frontier"], 5)
            self.assertEqual([p["sequence"] for p in snapshot["updates"]], [2])
            self.assertEqual(snapshot["packet_sequence_observation"]["unsealed_primary"][0]["sequence"], 4)
            live_run.retain_packet_sequence_observation(directory, "readiness", snapshot)
            final = live_run.bind_packet_sequence_ledger(windows, authority)
            live_run.validate_packet_sequence_observations(directory, final)
            malformed = copy.deepcopy(partial)
            malformed["updates"][1]["options"]["flush"] = 0
            # A completed but codec-invalid group is not an unpublished tail:
            # retain it, so the original codec-group validator rejects it.
            self.assertEqual(live_run._complete_packet_sequence_prefix(malformed["updates"], 1), 2)
            with patch.object(live_run, "synchronize_saved_updates", return_value=windows[2]):
                invalid = live_run.synchronize_packet_sequence_projection(
                    "server", directory, 1, authority, primary=malformed,
                )
            self.assertFalse(live_run.primary_h264_frame_ready("hardware", "adaptive-alpha", invalid))
            malformed["updates"][1]["relative_info"] = "screen-updates/1/101/1.info"
            with self.assertRaises(live_run.LabFailure):
                live_run._complete_packet_sequence_prefix(malformed["updates"], 1)

    def test_h264_stream_projects_other_window_ids_without_inventing_codec_edges(self) -> None:
        updates = self.global_updates()
        streams = live_run.h264_packet_streams(updates, allow_lossless_rgb_edges=True)
        self.assertEqual(len(streams), 1)
        self.assertEqual(streams[0]["packet_sequences"], [2, 5])
        self.assertEqual(streams[0]["other_window_sequences"], [3])
        self.assertEqual(streams[0]["interleaved_edge_sequences"], [4])
        self.assertEqual(streams[0]["packet_count"], 2)
        self.assertTrue(streams[0]["contiguous_frames"])

    def test_all_h264_history_interval_and_group_consumers_preserve_global_ids(self) -> None:
        updates = self.global_updates()
        updates["h264_stimulus"] = {
            "first_sequence": 2, "baseline_sequence": 2,
            "last_sequence": 5, "window_size": [1596, 1173],
        }
        self.assertTrue(live_run.hardware_h264_history_valid(updates))
        for name in (
            "adaptive_h264_production_updates", "hardware_h264_stimulus_updates",
            "hardware_h264_context_updates", "hardware_h264_production_updates",
        ):
            with self.subTest(consumer=name):
                selected = getattr(live_run, name)(updates)
                self.assertIsNotNone(selected)
                self.assertEqual([packet["sequence"] for packet in selected["updates"]], [2, 4, 5])
                missing = copy.deepcopy(updates)
                missing["packet_sequence_ledger"]["packets"].pop(2)
                self.assertIsNone(getattr(live_run, name)(missing))
        self.assertEqual(live_run.hardware_h264_phase_start_sequence(updates, (1596, 1173)), 2)
        zed = copy.deepcopy(updates)
        zed["h264_stimulus"].pop("first_sequence")
        selected = live_run.zed_h264_stimulus_updates(zed)
        self.assertEqual([packet["sequence"] for packet in selected["updates"]], [4, 5])

        windows = self.global_windows()
        authority = windows[1]["packet_sequence_ledger"]["authority"]
        windows[1]["updates"][-1]["sequence"] = 6
        extra = copy.deepcopy(windows[2]["updates"][-1])
        extra.update(sequence=5, relative_info="screen-updates/2/102/0.info")
        windows[2]["updates"].append(extra)
        windows[2]["count"] = 3
        bound = live_run.bind_packet_sequence_ledger(windows, authority)
        groups = live_run._ordered_saved_damage_groups(
            bound[1]["updates"], 1, bound[1]["packet_sequence_ledger"],
        )
        self.assertEqual([[packet["sequence"] for packet in group] for group in groups], [[2], [4, 6]])
        self.assertTrue(live_run.h264_with_lossless_rgb_edges(bound[1]))
        for field, value in (("h", 2), ("y", 1171)):
            changed = copy.deepcopy(windows)
            changed[1]["updates"][1][field] = value
            rebound = live_run.bind_packet_sequence_ledger(changed, authority)
            self.assertFalse(live_run.h264_with_lossless_rgb_edges(rebound[1]))

    def test_actual_frame_waiter_consumes_global_raw_prefix_and_retains_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            authority = self.authority(self.write_info(directory))
            self.write_packets(directory)
            relatives = tuple(path.relative_to(directory).as_posix() for path in directory.glob("screen-updates/**/*.info"))
            server_log = "commit wid 1 rects=((0, 0, 1596, 1173),)\n"
            client_log = (
                "register_window(..) window(0x1)=ClientWindow(0x1)\n"
                "draw_region(0, 0, 1596, 1172, h264\n"
                "choose_decoder('h264')=libva\n"
                "do_video_paint('h264', ImageWrapper(NV12\n"
                "record_decode_time(True, 1) wid=0x1, h264:\n"
                "do_present_fbo(\n"
            )

            def deltas(container, offsets):
                return {
                    name: (100, server_log if container == "server" else client_log if name == "client.stdout" else "")
                    for name in offsets
                }

            def wait_once(description, predicate, **_kwargs):
                self.assertEqual(description, "H264 frame outcome")
                self.assertTrue(predicate())

            with (
                patch.object(live_run, "read_container_log_deltas", side_effect=deltas),
                patch.object(live_run, "container_artifact_files", return_value=relatives),
                patch.object(live_run, "wait_for", side_effect=wait_once),
                patch.object(live_run, "pull_container_artifacts") as pull,
            ):
                self.assertEqual(live_run.wait_for_frame_boundary(
                    "server", 101, "client", 202, directory, "h264", "adaptive-alpha",
                    application="hardware", expected_xpra_wid=1, sequence_authority=authority,
                ), "success")
                pull.assert_not_called()
            observation = json.loads((directory / "h264-sequence-observations.json").read_text())["observations"]["readiness"]
            self.assertEqual(observation["ledger"]["authority"], authority)
            self.assertEqual(observation["ledger"]["frontier"], 6)
            self.assertEqual([row["sequence"] for row in observation["ledger"]["packets"]], [1, 2, 3, 4, 5])

    def test_snapshot_cut_cannot_move_when_later_owner_is_sampled(self) -> None:
        windows = self.global_windows()
        authority = windows[1]["packet_sequence_ledger"]["authority"]

        def later_owner(_server, _directory, wid):
            self.assertEqual(wid, 2)
            future = copy.deepcopy(windows[1]["updates"][0])
            future.update(sequence=6, relative_info="screen-updates/1/102/0.info")
            windows[1]["updates"].append(future)
            return windows[2]

        with patch.object(live_run, "synchronize_saved_updates", side_effect=later_owner):
            snapshot = live_run.synchronize_packet_sequence_projection(
                "server", LIVE_DIRECTORY, 1, authority, primary=windows[1],
            )
        self.assertEqual(snapshot["packet_sequence_ledger"]["frontier"], 6)
        self.assertEqual(snapshot["packet_sequence_observation"]["observed_frontier"], 6)
        self.assertEqual(snapshot["packet_sequence_observation"]["unsealed_primary"], [])
        self.assertEqual([p["sequence"] for p in snapshot["updates"]], [2, 4, 5])

    def test_complete_prefix_keeps_multiple_flush_groups_in_one_millisecond_bucket(self) -> None:
        windows = self.global_windows()
        authority = windows[1]["packet_sequence_ledger"]["authority"]
        primary = windows[1]
        for index, packet in enumerate(primary["updates"]):
            packet["relative_info"] = f"screen-updates/1/100/{index}.info"
        complete = live_run.bind_packet_sequence_ledger(windows, authority)[1]
        groups = live_run._ordered_saved_damage_groups(
            complete["updates"], 1, complete["packet_sequence_ledger"],
        )
        self.assertEqual([[p["sequence"] for p in group] for group in groups], [[2], [4, 5]])
        self.assertEqual(live_run._complete_packet_sequence_prefix(primary["updates"], 1), 3)
        edge = copy.deepcopy(primary["updates"][1])
        edge.update(sequence=6, relative_info="screen-updates/1/100/3.info")
        primary["updates"].append(edge)
        primary["count"] = 4
        self.assertEqual(live_run._complete_packet_sequence_prefix(primary["updates"], 1), 3)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with patch.object(live_run, "synchronize_saved_updates", return_value=windows[2]):
                snapshot = live_run.synchronize_packet_sequence_projection(
                    "server", directory, 1, authority, primary=primary,
                )
            self.assertEqual(snapshot["packet_sequence_ledger"]["frontier"], 6)
            self.assertEqual(snapshot["packet_sequence_observation"]["unsealed_primary"][0]["sequence"], 6)
            live_run.retain_packet_sequence_observation(directory, "readiness", snapshot)
            last = copy.deepcopy(primary["updates"][2])
            last.update(sequence=7, relative_info="screen-updates/1/100/4.info")
            last["options"].update(frame=2)
            primary["updates"].append(last)
            primary["count"] = 5
            final = live_run.bind_packet_sequence_ledger(windows, authority)
            live_run.validate_packet_sequence_observations(directory, final)
            self.assertTrue(live_run.h264_with_lossless_rgb_edges(final[1]))
        for paths in (
            ["100/0", "100/2", "100/1"],
            ["100/0", "100/2", "100/3"],
            ["100/0", "99/0", "99/1"],
        ):
            with self.subTest(paths=paths):
                malformed = copy.deepcopy(primary["updates"][:3])
                for packet, path in zip(malformed, paths, strict=True):
                    packet["relative_info"] = f"screen-updates/1/{path}.info"
                with self.assertRaises(live_run.LabFailure):
                    live_run._complete_packet_sequence_prefix(malformed, 1)


if __name__ == "__main__":
    unittest.main()
