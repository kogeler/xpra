# Copyright (C) 2026 kogeler
"""An expected connection loss must not admit a signal crash as successful teardown."""

import unittest

import run as live_run
from test_job import interaction_identity, server_identity


class TransportExitStatusTest(unittest.TestCase):
    def lifecycle(self, status):
        application, server = interaction_identity(), server_identity()
        return {
            "mode": "transport-loss", "client_exit_status": status,
            "server_exit_status": 0,
            "client_exited_after_transport_loss": True, "transport_disconnect_returncode": 0,
            "application_survived_transport_loss": True, "server_survived_transport_loss": True,
            "server_alive_before_application_termination": True,
            "application_exited_after_termination": True, "server_exited_after_application": True,
            "server_pid": server["pid"],
            "application_identity_at_capture": application,
            "application_identity_after_transport_loss": application,
            "application_identity_before_termination": application,
            "server_identity_at_capture": server,
            "server_identity_after_transport_loss": server,
            "server_identity_before_application_termination": server,
            "application_termination": {"identity": application, "server_identity": server,
                                        "pidfd": True, "server_pidfd": True,
                                        "signal": "SIGTERM", "returncode": 0},
        }

    def test_only_the_expected_protocol_connection_loss_can_pass(self):
        self.assertTrue(all(live_run.lifecycle_boundary_checks("transport-loss", self.lifecycle(1)).values()))
        for status in (0, -6, -9, -11, 134, 137, 139, 14, 18, 26, True, None):
            with self.subTest(status=status):
                self.assertFalse(all(live_run.lifecycle_boundary_checks("transport-loss", self.lifecycle(status)).values()))

    def test_server_crash_cannot_pass_any_lifecycle(self):
        for mode in ("application-exit", "detach", "transport-loss"):
            baseline = self.lifecycle(1 if mode == "transport-loss" else 0)
            if mode == "detach":
                baseline = {key.replace("_transport_loss", "_detach"): value for key, value in baseline.items()}
                baseline["detach_returncode"] = 0
            baseline.update(mode=mode, client_exited_after_server=True)
            self.assertTrue(all(live_run.lifecycle_boundary_checks(mode, baseline).values()), mode)
            for status in (-6, -9, -11, 134, 137, 139, 1, 14, False, True, None):
                changed = {**baseline, "server_exit_status": status}
                with self.subTest(mode=mode, status=status):
                    self.assertFalse(all(live_run.lifecycle_boundary_checks(mode, changed).values()))
            del baseline["server_exit_status"]
            with self.subTest(mode=mode, status="missing"):
                self.assertFalse(all(live_run.lifecycle_boundary_checks(mode, baseline).values()))


if __name__ == "__main__":
    unittest.main()
