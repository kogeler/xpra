# Copyright (C) 2026 kogeler
"""SSH configuration controls; real SSH execution belongs to the live cases."""

import base64
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import ssh_fixture as fixture


PUBLIC_KEY = "ssh-ed25519 " + base64.b64encode(b"public fixture key material" * 2).decode()


class SshFixtureTest(unittest.TestCase):
    def fake_keygen(self, argv, **_kwargs):
        self.assertEqual(argv[:7], ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f"])
        path = Path(argv[-1])
        path.write_text("PRIVATE TEST MATERIAL\n")
        path.with_suffix(".pub").write_text(PUBLIC_KEY + " generated-comment\n")

    def test_own_keys_strict_host_identity_and_no_mux_fallback(self):
        with tempfile.TemporaryDirectory() as raw, patch.object(fixture.subprocess, "run", side_effect=self.fake_keygen):
            root = Path(raw)
            client, server = root / "client", root / "server"
            with patch.object(fixture, "ROOT", client):
                client_key = fixture.client_init()
            with patch.object(fixture, "ROOT", server):
                server_key = fixture.server_init(client_key)
                daemon = (server / "sshd_config").read_text()
                self.assertIn("PasswordAuthentication no\n", daemon)
                self.assertIn("AllowTcpForwarding no\n", daemon)
                self.assertEqual((server / "authorized_keys").read_text(), PUBLIC_KEY + "\n")
            with patch.object(fixture, "ROOT", client):
                fixture.trust_server(server_key)
                config = (client / "ssh_config").read_text()
                for option in ("IdentityAgent none", "IdentitiesOnly yes", "StrictHostKeyChecking yes",
                               "GlobalKnownHostsFile /dev/null", "ProxyCommand /bin/false"):
                    self.assertIn(option, config)
                self.assertIn("ProxyCommand=none", fixture.master_command())
                self.assertNotIn("PRIVATE TEST MATERIAL", config)
                self.assertEqual((client / "ssh_config").stat().st_mode & 0o777, 0o600)
                self.assertEqual(client.stat().st_mode & 0o777, 0o700)
                self.assertEqual((client / "known_hosts").read_text(), f"[xpra-server]:22222 {PUBLIC_KEY}\n")
                with self.assertRaises(FileExistsError):
                    fixture.client_init()

    def test_bad_public_keys_do_not_create_owned_state(self):
        with tempfile.TemporaryDirectory() as raw, patch.object(fixture, "ROOT", Path(raw) / "ssh"):
            for key in ("", "ssh-rsa AAAA", "ssh-ed25519 !!!", PUBLIC_KEY + "\nHost other",
                        "ssh-ed25519 YQ=="):
                with self.subTest(key=key), self.assertRaises(ValueError):
                    fixture.server_init(key)
            self.assertFalse(fixture.ROOT.exists())


if __name__ == "__main__":
    unittest.main()
