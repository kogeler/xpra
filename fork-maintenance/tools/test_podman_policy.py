# Copyright (C) 2026 kogeler

from __future__ import annotations

import unittest

import podman_policy


class PodmanUserNamespacePolicyTest(unittest.TestCase):
    def test_builds_the_reviewed_bounded_keep_id_namespace(self) -> None:
        self.assertEqual(
            podman_policy.keep_id_userns(1001, 1001),
            "keep-id:uid=1001,gid=1001,size=2048",
        )

    def test_accepts_only_sized_allocating_modes(self) -> None:
        for value in (
            "auto:size=2048",
            "keep-id:uid=1001,gid=1001,size=2048",
            "nomap:size=2048",
        ):
            with self.subTest(value=value):
                self.assertEqual(podman_policy.validate_userns_spec(value), value)

    def test_rejects_unbounded_allocating_modes(self) -> None:
        for value in ("auto", "keep-id", "nomap", "auto:uidmapping=0:1:2"):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(
                    podman_policy.PodmanPolicyError,
                    "explicit size|positive size",
                ),
            ):
                podman_policy.validate_userns_spec(value)

    def test_rejects_host_duplicate_and_invalid_sizes(self) -> None:
        for value in (
            "host",
            "auto:size=0",
            "auto:size=-1",
            "auto:size=2048,size=4096",
            "keep-id:uid=1001,gid=1001,size=not-a-number",
        ):
            with self.subTest(value=value), self.assertRaises(podman_policy.PodmanPolicyError):
                podman_policy.validate_userns_spec(value)

    def test_keep_id_size_must_contain_uid_and_gid(self) -> None:
        with self.assertRaisesRegex(
            podman_policy.PodmanPolicyError,
            "does not contain",
        ):
            podman_policy.keep_id_userns(1001, 1001, size=1001)

    def test_argv_validator_handles_both_option_forms(self) -> None:
        for argv in (
            ["podman", "run", "--userns", "auto:size=2048", "image"],
            ["/usr/bin/podman", "create", "--userns=nomap:size=2048", "image"],
        ):
            with self.subTest(argv=argv):
                podman_policy.validate_podman_argv(argv)
        with self.assertRaisesRegex(podman_policy.PodmanPolicyError, "explicit size"):
            podman_policy.validate_podman_argv(
                ["podman", "run", "--userns=keep-id:uid=1001,gid=1001", "image"]
            )

    def test_argv_validator_rejects_missing_or_duplicate_options(self) -> None:
        for argv in (
            ["podman", "run", "--userns"],
            [
                "podman",
                "run",
                "--userns=auto:size=2048",
                "--userns",
                "nomap:size=2048",
                "image",
            ],
        ):
            with self.subTest(argv=argv), self.assertRaises(podman_policy.PodmanPolicyError):
                podman_policy.validate_podman_argv(argv)

    def test_non_podman_commands_are_unchanged(self) -> None:
        podman_policy.validate_podman_argv(
            ["python3", "helper.py", "--userns=keep-id"]
        )


if __name__ == "__main__":
    unittest.main()
