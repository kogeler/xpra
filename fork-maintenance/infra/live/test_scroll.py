# Copyright (C) 2026 kogeler
"""Controls for the complete-stack live scroll oracle, not a live endpoint."""

import copy
from itertools import product
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import call, patch

from test_job import job, live_run


def events():
    position = [0.0, 0.0]
    result = []
    for seq, delta in enumerate(((0.0, 1.5), (0.0, 1.5), (0.0, -1.5),
                                 (0.0, -1.5), (1.5, 0.0), (-1.5, 0.0))):
        position = [a + b for a, b in zip(position, delta)]
        result.append({"schema": 1, "seq": seq, "monotonic_ns": 100 + seq,
                       "delta": list(delta), "position": position})
    return result


def stream(records):
    return "".join(json.dumps(record) + "\n" for record in records)


def write(root, name, text):
    path = root / name
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def artifacts(root: Path, driver: str):
    write(root, "interaction.scroll.jsonl", stream(events()))
    packets = []
    buttons = (5, 5, 4, 4, 7, 6)
    for button, distance in zip(buttons, (-1000, -1000, 1000, 1000, 1000, -1000)):
        if driver == "sway-axis":
            packet = ["pointer-wheel", 7, button, distance, (1, 1), [], [], {}]
            packets.append(f"send_wheel_delta(..) {packet!r}")
        else:
            for state in (True, False):
                packet = ["pointer-button", 0, len(packets), 7, button, state, (1, 1), {}]
                packets.append(f"button packet: {packet!r}")
    write(root, "client.stdout", "\n".join(packets) + "\n")
    write(root, "sway-pointer.stdout", "pointer-capability-ready\n" + "".join(
        f"wheel {index} {button}\n" for index, button in enumerate(buttons, 1)
    ))
    write(root, "sway-pointer.stderr", "")
    axes = ((0, 15.0, 120), (0, 15.0, 120), (0, -15.0, -120),
            (0, -15.0, -120), (1, 15.0, 120), (1, -15.0, -120))
    write(root, "server.stderr", "".join(f"do_wheel_motion(1, {a}, {b}, {c})\n" for a, b, c in axes))
    for name in ("server-info.txt", "server-info-interaction.txt"):
        write(root, name, "windows.7.title=Xpra Hardware Interaction Ready\n")
    for index, name in enumerate(("interaction-after", *(f"interaction-scroll-{i}" for i in range(1, 7)))):
        path = root / f"{name}.rgb.png"
        live_run.Image.new("RGB", (16, 16), (index * 20, 100, 100)).save(path)
        path.chmod(0o600)
    return {"driver": driver, "wid": 7, "client_log_start": 0, "server_log_start": 0}


class ScrollEvidenceTest(unittest.TestCase):
    def test_black_background_waits_for_cursor_without_relaxing_pixels(self):
        black = {"image": {"quantized_rgb_colors": 1, "dominant_rgb": [0, 0, 0]}}
        cursor = {"image": {"quantized_rgb_colors": 2, "dominant_rgb": [0, 0, 0]}}
        wrong = {"image": {"quantized_rgb_colors": 1, "dominant_rgb": [1, 1, 1]}}

        def wait(description, ready, *, timeout):
            self.assertEqual(description, "controlled black Sway background")
            self.assertEqual(timeout, 5)
            self.assertFalse(ready())
            self.assertFalse(ready())
            self.assertTrue(ready())

        sway = ["swaymsg", "-s", "fixture.sock"]
        commands = [
            call("client", [*sway, "seat", "seat0", "hide_cursor", "100"]),
            call("client", [*sway, "seat", "seat0", "cursor", "set", "100", "100"]),
            call("client", [*sway, "seat", "seat0", "hide_cursor", "0"]),
        ]
        with patch.object(live_run, "capture_grim", side_effect=(cursor, wrong, black)) as capture, \
                patch.object(live_run, "podman_exec") as execute, \
                patch.object(live_run, "wait_for", side_effect=wait):
            self.assertEqual(live_run.capture_black_sway_background("client", Path("test"), "wayland-1", sway), black)
            self.assertEqual(capture.call_count, 3)
            capture.assert_called_with("client", Path("test"), "root-before", "wayland-1")
            self.assertEqual(execute.call_args_list, commands)
        # Neither a positive capture nor a timeout may leave focus-clearing
        # cursor hiding enabled for the later real input workload.
        with patch.object(live_run, "podman_exec") as execute, \
                patch.object(live_run, "wait_for", side_effect=live_run.LabFailure("not black")):
            with self.assertRaises(live_run.LabFailure):
                live_run.capture_black_sway_background("client", Path("test"), "wayland-1", sway)
            self.assertEqual(execute.call_args_list, commands)

    def test_virtual_pointer_inputs_are_frozen_and_built(self):
        self.assertEqual(live_run.HARNESS_INPUTS, job.HARNESS_INPUTS)
        directory = Path(live_run.__file__).parent
        ignored = (directory / ".containerignore").read_text().splitlines()
        recipe = (directory / "Containerfile").read_text()
        for name in ("virtual_pointer_device.c", "wlr-virtual-pointer-v1.xml"):
            self.assertIn(directory / name, live_run.HARNESS_INPUTS)
            self.assertIn(directory / name, live_run.BUILD_CONTEXT_INPUTS)
            self.assertIn("!" + name, ignored)
            self.assertIn("COPY " + name + " ", recipe)
        self.assertIn("wayland-scanner client-header", recipe)
        self.assertIn("wayland-scanner private-code", recipe)

    def test_parser_rejects_malformed_and_inconsistent_streams(self):
        self.assertEqual(live_run.parse_interaction_scroll_events(stream(events())), events())
        candidates = ("{}\n", "[]\n", stream(events())[:-1], "x" * 16385,
                      stream(events()).replace('"schema": 1', '"schema": 1, "schema": 1', 1))
        for candidate in candidates:
            with self.subTest(candidate=candidate[:80]), self.assertRaises(live_run.LabFailure):
                live_run.parse_interaction_scroll_events(candidate)
        for key, value in (("schema", True), ("seq", 2), ("monotonic_ns", 0),
                           ("delta", [0, 0]), ("delta", [False, 1.5]),
                           ("delta", [0, float("nan")]), ("delta", [0, 10 ** 400]),
                           ("position", [0, 50]), ("unexpected", 1)):
            candidate = events()
            candidate[0][key] = value
            with self.subTest(key=key), self.assertRaises(live_run.LabFailure):
                live_run.parse_interaction_scroll_events(stream(candidate))

    def test_raw_artifacts_and_collector(self):
        for application, driver in (("gtk", "x11-discrete"), ("hardware", "sway-axis"), ("opengl", "sway-axis")):
            with self.subTest(application=application), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                record = artifacts(root, driver)
                evidence = live_run.interaction_scroll_evidence(root, record)
                self.assertTrue(all(evidence["checks"].values()), evidence)
                embedded = {"interaction": {"scroll": evidence},
                            "classification": {"boundaries": {"interaction": evidence["checks"]}}}
                self.assertTrue(job.scroll_fixture_artifact_evidence_matches(embedded, root, live_run, application))
                for key in evidence:
                    altered = copy.deepcopy(embedded)
                    del altered["interaction"]["scroll"][key]
                    with self.subTest(missing=key):
                        self.assertFalse(job.scroll_fixture_artifact_evidence_matches(altered, root, live_run, application))
                for key in evidence["checks"]:
                    altered = copy.deepcopy(embedded)
                    altered["classification"]["boundaries"]["interaction"][key] = 1
                    self.assertFalse(job.scroll_fixture_artifact_evidence_matches(altered, root, live_run, application))
                # A late extra input remains visible after the final screenshot.
                log = (root / "client.stdout").read_text()
                write(root, "client.stdout", log + log)
                self.assertFalse(job.scroll_fixture_artifact_evidence_matches(embedded, root, live_run, application))

    def test_oracle_rejects_duplicate_direction_scale_and_forged_types(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            record = artifacts(root, "sway-axis")
            evidence = live_run.interaction_scroll_evidence(root, record)
            mutations = (
                ("driver", []), ("events", [None] * 6), ("events", events()[:-1]),
                ("packets", evidence["packets"] * 2), ("packets", [["wheel", 5, 1000]] * 6),
                ("packets", [["wheel", False, -1000]] + evidence["packets"][1:]),
                ("stimuli", []), ("stimuli", [[1, 5]] * 6),
                ("axes", [[0, -1.0, -120]] * 6), ("axes", [[False, 15.0, 120]] * 6),
                ("pixel_hashes", ["a" * 64] * 7), ("pixel_hashes", ["bad"] * 7),
            )
            for key, value in mutations:
                candidate = {**evidence, key: value}
                with self.subTest(key=key, value=value):
                    self.assertFalse(all(live_run.interaction_scroll_checks(candidate).values()))

    def test_axis_accepts_either_single_representation_without_losing_accounting(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            record = artifacts(root, "sway-axis")
            evidence = live_run.interaction_scroll_evidence(root, record)
            wheels = evidence["packets"]
            for discrete in product((False, True), repeat=6):
                packets = []
                for wheel, use_discrete in zip(wheels, discrete):
                    packets.extend([["button", wheel[1], state] for state in (True, False)]
                                   if use_discrete else [wheel])
                candidate = {**evidence, "packets": packets}
                with self.subTest(discrete=discrete):
                    self.assertTrue(all(live_run.interaction_scroll_checks(candidate).values()))
                    for invalid in (packets[:-1], packets + [wheels[-1]],
                                    [["button", 5, True], ["button", 5, False]] + packets):
                        self.assertFalse(live_run.interaction_scroll_checks(
                            {**candidate, "packets": invalid},
                        )["scroll_client_packets_exact"])
                    if not all(discrete):
                        self.assertFalse(live_run.interaction_scroll_checks(
                            {**candidate, "driver": "x11-discrete"},
                        )["scroll_client_packets_exact"])
            # Bind first-step fallback to actual raw-log reconstruction too.
            log = (root / "client.stdout").read_text().splitlines()
            first = [f"button packet: {['pointer-button', 0, index, 7, 5, state, (1, 1), {}]!r}"
                     for index, state in enumerate((True, False))]
            write(root, "client.stdout", "\n".join(first + log[1:]) + "\n")
            fallback = live_run.interaction_scroll_evidence(root, record)
            self.assertTrue(all(fallback["checks"].values()))
            # Forwarding both representations, including a late duplicate,
            # cannot pass merely because their directions now agree.
            write(root, "client.stdout", "\n".join(first + log) + "\n")
            duplicate = live_run.interaction_scroll_evidence(root, record)
            self.assertFalse(duplicate["checks"]["scroll_client_packets_exact"])
            for state in (1, False):
                bad = copy.deepcopy(fallback)
                bad["packets"][0][2] = state
                self.assertFalse(live_run.interaction_scroll_checks(bad)["scroll_client_packets_exact"])

    def test_collector_requires_exact_window_fields_offsets_and_raw_pixels(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            record = artifacts(root, "sway-axis")
            for key, value in (("driver", []), ("wid", 8), ("wid", True),
                               ("client_log_start", -1), ("server_log_start", 999999)):
                with self.subTest(key=key), self.assertRaises(live_run.LabFailure):
                    live_run.interaction_scroll_evidence(root, {**record, key: value})
            for packet in ([], ["pointer-wheel", 8, 5, -1000, (), (), (), {}],
                           ["pointer-wheel", 7, 5, True, (), (), (), {}], ["pointer-button"],
                           ["unknown", 7]):
                write(root, "client.stdout", f"send_wheel_delta(..) {packet!r}\n")
                with self.subTest(packet=packet), self.assertRaises(live_run.LabFailure):
                    live_run.interaction_scroll_evidence(root, record)
            record = artifacts(root, "sway-axis")
            first = root / "interaction-after.rgb.png"
            (root / "interaction-scroll-1.rgb.png").write_bytes(first.read_bytes())
            evidence = live_run.interaction_scroll_evidence(root, record)
            self.assertFalse(evidence["checks"]["scroll_visible_response"])

    def test_virtual_device_stream_must_be_complete_and_error_free(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            record = artifacts(root, "sway-axis")
            for text in ("pointer-capability-ready\nwheel 1 5", "bad\n", "x" * 1025):
                write(root, "sway-pointer.stdout", text)
                with self.assertRaises(live_run.LabFailure):
                    live_run.interaction_scroll_evidence(root, record)
            record = artifacts(root, "sway-axis")
            write(root, "sway-pointer.stderr", "device error\n")
            with self.assertRaises(live_run.LabFailure):
                live_run.interaction_scroll_evidence(root, record)


if __name__ == "__main__":
    unittest.main()
