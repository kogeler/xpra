#!/usr/bin/env python3
# Copyright (C) 2026 kogeler
"""Ephemeral SSH keys and a mandatory owned mux, without host credentials."""

import argparse
import base64
import json
from pathlib import Path
import subprocess

ROOT = Path("/home/lab/.xpra-live-ssh")
HOST = "xpra-server"
PORT = 22222


def public_key(value: str) -> str:
    if len(value) > 4096 or "\n" in value.strip() or "\r" in value.strip():
        raise ValueError("invalid fixture SSH public key")
    parts = value.split()
    if len(parts) < 2 or parts[0] != "ssh-ed25519":
        raise ValueError("fixture requires an ed25519 public key")
    payload = base64.b64decode(parts[1], validate=True)
    if not 32 <= len(payload) <= 1024:
        raise ValueError("invalid fixture SSH public key size")
    return " ".join(parts[:2])


def write_private(path: Path, body: str) -> None:
    with path.open("x", encoding="ascii") as stream:
        stream.write(body)
    path.chmod(0o600)


def keypair(name: str) -> str:
    path = ROOT / name
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)],
                   check=True, capture_output=True, text=True)
    return public_key(path.with_suffix(".pub").read_text())


def client_init() -> str:
    ROOT.mkdir(mode=0o700)
    return keypair("identity")


def server_init(client_public_key: str) -> str:
    authorized = public_key(client_public_key)
    ROOT.mkdir(mode=0o700)
    host_key = keypair("host_key")
    write_private(ROOT / "authorized_keys", authorized + "\n")
    write_private(ROOT / "sshd_config", (
        f"Port {PORT}\nListenAddress 0.0.0.0\nHostKey {ROOT}/host_key\n"
        f"PidFile /artifacts/server.sshd.pid\nAuthorizedKeysFile {ROOT}/authorized_keys\n"
        "AllowUsers lab\nPermitRootLogin no\nUsePAM no\nStrictModes yes\n"
        "PasswordAuthentication no\nKbdInteractiveAuthentication no\nPubkeyAuthentication yes\n"
        "AllowTcpForwarding no\nX11Forwarding no\nAllowAgentForwarding no\nLogLevel VERBOSE\n"
    ))
    return host_key


def trust_server(server_public_key: str) -> None:
    trusted = public_key(server_public_key)
    if not (ROOT / "identity").is_file():
        raise ValueError("fixture client identity is missing")
    write_private(ROOT / "known_hosts", f"[{HOST}]:{PORT} {trusted}\n")
    # ProxyCommand=false prevents a channel silently falling back to a new
    # connection when the owned mux is absent. Only the master overrides it.
    write_private(ROOT / "ssh_config", client_configuration())


def client_configuration() -> str:
    return (
        f"Host {HOST}\n  HostName {HOST}\n  Port {PORT}\n  User lab\n"
        "  BatchMode yes\n  PasswordAuthentication no\n  KbdInteractiveAuthentication no\n"
        "  IdentitiesOnly yes\n  IdentityAgent none\n  ForwardAgent no\n  RequestTTY no\n"
        f"  IdentityFile {ROOT}/identity\n  UserKnownHostsFile {ROOT}/known_hosts\n"
        "  GlobalKnownHostsFile /dev/null\n  StrictHostKeyChecking yes\n"
        f"  ControlPath {ROOT}/control\n  ControlMaster no\n  ControlPersist no\n"
        "  ProxyCommand /bin/false\n  ConnectTimeout 10\n  ConnectionAttempts 1\n  LogLevel DEBUG1\n"
    )


def ssh_command() -> list[str]:
    return ["ssh", "-F", str(ROOT / "ssh_config")]


def master_command() -> list[str]:
    return [*ssh_command(), "-o", "ProxyCommand=none", "-M", "-N", HOST]


def exit_command() -> list[str]:
    return [*ssh_command(), "-O", "exit", HOST]


def daemon_command() -> list[str]:
    return ["/usr/sbin/sshd", "-D", "-e", "-f", str(ROOT / "sshd_config")]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("client-init", "server-init", "trust-server"))
    parser.add_argument("public_key", nargs="?")
    args = parser.parse_args()
    if args.action == "client-init":
        result = client_init()
    elif args.action == "server-init":
        result = server_init(args.public_key or "")
    else:
        trust_server(args.public_key or "")
        result = None
    print(json.dumps({"public_key": result}, sort_keys=True))
