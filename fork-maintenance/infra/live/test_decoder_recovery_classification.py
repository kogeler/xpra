# Copyright (C) 2026 kogeler
"""A recovery result must never be aggregated as positive H.264 decoding."""

from argparse import Namespace
from pathlib import Path
import unittest
from unittest.mock import patch

import decoder_recovery
import run as runner
from test_decoder_recovery import recovery_inputs


class RecoveryClassificationTest(unittest.TestCase):
    def classify(self, proof):
        logs = {"nonempty_wayland_commits": 1, "h264_per_window_negotiation_applied": True,
                "server_initial_data_errors": [], "server_logging_errors": 0,
                "h264_draw_regions": 1, "opengl_rgb_paints": 3, "client_successful_paints": 3,
                "paint_errors": [], "opengl_renderer": "AMD", "opengl_presentations": 3}
        with (patch.object(runner, "application_boundary_checks", return_value={"fixture": True}),
              patch.object(runner, "interaction_alpha_content_checks", return_value={"fixture_alpha": True}),
              patch.object(runner, "interaction_scroll_checks", return_value={"fixture_scroll": True})):
            return runner.classify_boundaries(
                args=Namespace(application="hardware", encoding="h264", h264_client_policy="adaptive-alpha",
                               render_node=Path("/dev/dri/renderD128"), lifecycle="application-exit"),
                application_activity={}, application_gpu={}, log_evidence=logs,
                updates={"count": 1, "updates": [{"payload_bytes": 1}]},
                direct={"image": {"central_opaque_ratio": 1}, "xwd": {"unique_rgb_colors": 1000}},
                composited={"quantized_rgb_colors": 1000, "background_match_ratio": 0},
                source_image={"quantized_rgb_colors": 1000},
                codec_hardware={"client_desktop": {"hardware_renderer": True}},
                interaction={"pointer_marker_present": True, "pointer_changed_pixels": True, "keyboard_escape_received": True},
                pixel_evidence={"matching_server_frame": True, "red_blue_order_verified": True,
                                "source_viewport_placement_logged": True},
                lifecycle={"mode": "application-exit", "server_exit_status": 0, "client_exit_status": 0,
                           "client_exited_after_server": True, "server_exited_after_application": True},
                recovery=proof,
            )

    def test_recovery_aggregation_is_explicit_and_fail_closed(self):
        proof = decoder_recovery.evaluate(**recovery_inputs())
        result = self.classify(proof)
        self.assertEqual(result["first_failed_boundary"], "passed")
        self.assertTrue(result["decoder_recovery_passed"])
        self.assertNotIn("hardware_pipeline_passed", result)
        self.assertNotIn("h264_encoder", result["boundaries"])
        self.assertNotIn("h264_decoder", result["boundaries"])
        proof["checks"]["server_retired_h264"] = False
        result = self.classify(proof)
        self.assertEqual(result["first_failed_boundary"], "decoder_recovery")
        self.assertFalse(result["decoder_recovery_passed"])


if __name__ == "__main__":
    unittest.main()
