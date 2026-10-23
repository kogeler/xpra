# Copyright (C) 2026 kogeler
"""A recovery claim requires the failed live boundary and a real peer delta."""

import copy
import json
import unittest

from decoder_loss_fixture import MARKER
import decoder_recovery


def recovery_inputs():
    event = {"schema": 1, "event": "injected-init-failure", "encoding": "h264", "thread": "decode",
             "pid": 42, "width": 640, "height": 480, "colorspace": "YUV420P", "monotonic_ns": 100}
    return {"window_id": 1, "client_pid": 42,
            "client_log": "seccomp installed=True\n" + MARKER + json.dumps(event) + "\n"
                          + "record_decode_time(True, ) wid=0x1, rgb24: 640x480, 0.1ms\n",
            "server_log": "do_set_client_properties(typedict({'encoding.full_csc_modes': {'h264': ('YUV420P',)}, 'event': 'map'}))\n"
                          + "do_set_client_properties(typedict({'encoding.full_csc_modes': {'h264': []}}))\n",
            "server_info": "client.0.window.windows.1.encodings.csc_modes.h264=()\n"
                           + "client.0.window.windows.1.encodings.video=()\nwindows.1.size=(640, 480)\n",
            "packets": [{"sequence": sequence, "encoding": encoding, "x": 0, "y": 0,
                         "w": 640, "h": 480, "options": {"window-size": [640, 480]}}
                        for sequence, encoding in ((1, "h264"), (2, "rgb24"), (3, "rgb24"), (4, "rgb24"))]}


class DecoderRecoveryTest(unittest.TestCase):
    baseline = staticmethod(recovery_inputs)

    def test_proof_is_json_canonical_with_unchanged_tuple_csc_modes(self):
        value = self.baseline()
        value["server_log"] = value["server_log"].replace("'h264': []", "'h264': [], 'vp9': ('YUV420P',)")
        proof = decoder_recovery.evaluate(**value)
        self.assertEqual(json.loads(json.dumps(proof)), proof)

    def test_viewport_is_capture_phase_not_terminal_resized_packet(self):
        value = self.baseline()
        value["packets"][-1]["options"]["window-size"] = [1596, 1173]
        proof = decoder_recovery.evaluate(**value)
        self.assertEqual(proof["viewport_size"], [640, 480])
        for info in ("", "windows.1.size=(True, 480)\n", "windows.1.size=(640, 480)\n" * 2):
            with self.subTest(info=info), self.assertRaises(ValueError):
                decoder_recovery.viewport_size(info, 1)

    def test_healthy_transition_and_independent_counterfactuals(self):
        baseline = self.baseline()
        self.assertTrue(all(decoder_recovery.evaluate(**baseline)["checks"].values()))
        for mutation in ("no-filter", "no-fault", "no-advertisement", "no-delta", "stale-server", "loop",
                         "too-few-pictures", "no-ack", "only-rgb-edges", "unrelated-delta", "invalid-encoding"):
            with self.subTest(mutation=mutation):
                value = copy.deepcopy(baseline)
                if mutation == "no-filter":
                    value["client_log"] = value["client_log"].replace("seccomp installed=True", "requested default")
                elif mutation == "no-fault":
                    value["client_log"] = "\n".join(line for line in value["client_log"].splitlines() if not line.startswith(MARKER))
                elif mutation == "no-advertisement":
                    value["server_log"] = value["server_log"].replace("('YUV420P',)", "[]")
                elif mutation == "no-delta":
                    value["server_log"] = value["server_log"].splitlines()[0]
                elif mutation == "stale-server":
                    value["server_info"] = value["server_info"].replace("csc_modes.h264=()", "csc_modes.h264=('YUV420P',)")
                elif mutation == "loop":
                    value["packets"].append({**value["packets"][0], "sequence": 5})
                elif mutation == "too-few-pictures":
                    value["packets"].pop()
                elif mutation == "no-ack":
                    value["client_log"] = value["client_log"].replace("record_decode_time(True", "record_decode_time(False")
                elif mutation == "only-rgb-edges":
                    for packet in value["packets"][1:]:
                        packet["h"] = 1
                elif mutation == "unrelated-delta":
                    value["server_log"] = value["server_log"].replace("{'h264': []}", "{'h264': []}, 'unrelated': 1")
                else:
                    value["client_log"] += "invalid encoding: 'h264'\n"
                self.assertFalse(all(decoder_recovery.evaluate(**value)["checks"].values()))

    def test_another_client_or_wrong_fault_boundary_is_rejected(self):
        for old, new in ((42, 43), ("decode", "main"), ("YUV420P", "RGBA")):
            value = self.baseline()
            value["client_log"] = value["client_log"].replace(json.dumps(old), json.dumps(new))
            with self.subTest(old=old), self.assertRaises(ValueError):
                decoder_recovery.evaluate(**value)

    def test_old_inflight_failure_before_picture_recovery_is_not_a_loop(self):
        value = self.baseline()
        value["client_log"] = value["client_log"].replace("record_decode_time(", "invalid encoding: 'h264'\nrecord_decode_time(")
        self.assertTrue(all(decoder_recovery.evaluate(**value)["checks"].values()))


if __name__ == "__main__":
    unittest.main()
