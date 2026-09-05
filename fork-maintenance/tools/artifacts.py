# Copyright (C) 2026 kogeler
"""Permanent-policy filesystem garbage collection, separate from acceptance.

The policy selects storage classes; runtime ownership and workspace export
checks protect unfinished work. Neither historical result schemas nor dates
decide whether disposable output can be discarded. Deletion reuses the locked,
digest-bound, crash-resumable cycle-cleanup transaction engine.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

import contrib
import tomllib

POLICY_PATH = contrib.AUTOMATION_ROOT / "artifacts.toml"
WORKSPACES = Path("upstream-tests/workspaces")
FINAL_SUFFIXES = (".remove.json", ".status.json", ".status", ".log", ".resolution.json")
LIVE_RUNTIME_SUFFIXES = (
    ".freeze-result.json",
    ".freeze.runtime",
    ".freeze-prelaunch.json",
    ".freeze-abort.json",
    ".freeze.completion.json",
    ".freeze.runtime.log",
    ".freeze.result.json",
    ".freeze.json",
    ".owner.json",
    ".completion.json",
    ".runtime.log",
    ".runtime",
)


@dataclass(frozen=True)
class Policy:
    keep: tuple[Path, ...]
    containers: tuple[Path, ...]
    digest: str

    @property
    def cycle(self) -> str:
        return f"artifacts-{self.digest}"


@dataclass(frozen=True)
class Inventory:
    plan: contrib.CleanupPlan
    protected: tuple[tuple[str, str], ...]
    bytes: int
    blocked: tuple[tuple[str, str], ...] = ()


def relative_path(value: object) -> Path:
    if (
        not isinstance(value, str)
        or not value
        or any(ord(character) < 32 or character in "*?[]" for character in value)
    ):
        contrib.fail("artifact policy requires a nonempty relative path")
    path = Path(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
        or not path.parts
    ):
        contrib.fail(f"artifact path is not normalized: {value!r}")
    return path


def load_policy(policy_path: Path = POLICY_PATH) -> Policy:
    payload = policy_path.read_bytes()
    data = tomllib.loads(payload.decode("utf-8"))
    if set(data) != {"schema", "keep", "containers"} or data["schema"] != 1:
        contrib.fail("unsupported artifact policy schema")
    groups = []
    for key in ("keep", "containers"):
        values = data[key]
        if not isinstance(values, list) or not values:
            contrib.fail(f"artifact policy {key} must be a nonempty array")
        paths = tuple(sorted(relative_path(value) for value in values))
        if len(set(paths)) != len(paths):
            contrib.fail(f"artifact policy repeats a {key} path")
        groups.append(paths)
    keep, containers = groups
    if set(keep).intersection(containers):
        contrib.fail("artifact policy cannot both keep and traverse a path")
    for path in (*keep, *containers):
        if any(parent not in containers for parent in path.parents if parent != Path(".")):
            contrib.fail(f"artifact policy lacks an explicit structural parent: {path}")
    if Path("retained") not in keep or WORKSPACES not in containers:
        contrib.fail("artifact policy must retain operator records and classify workspaces")
    return Policy(keep, containers, contrib.sha256_bytes(payload))


def entries(root: Path, relative: Path | str) -> tuple[Path, ...]:
    directory = root / relative
    if not directory.exists() and not directory.is_symlink():
        return ()
    contrib.require_owned_directory(directory, "artifact structural directory")
    return tuple(sorted(directory.iterdir()))


def identity(value: str) -> str:
    if not contrib.WORKSPACE_RE.fullmatch(value):
        contrib.fail(f"unrecognized artifact runtime identity: {value!r}")
    return value


def suffix_identity(name: str, suffixes: tuple[str, ...]) -> str | None:
    for suffix in suffixes:
        if name.endswith(suffix):
            return identity(name[: -len(suffix)])
    return None


def podman_identities() -> dict[str, set[str]]:
    """Read-only: even exited containers protect their records until removal."""
    result: dict[str, set[str]] = {"upstream": set(), "live": set(), "deb": set()}
    for owner, group in (
        (contrib.UPSTREAM_TEST_OWNER, "upstream"),
        ("live", "live"),
        (contrib.DEB_PACKAGE_OWNER, "deb"),
    ):
        listed = contrib.run(
            ("podman", "ps", "--all", "--quiet", "--filter", f"label=io.xpra.fork-maintenance.owner={owner}")
        )
        ids = listed.stdout.splitlines()
        if not ids:
            continue
        inspected = json.loads(contrib.run(("podman", "inspect", *ids)).stdout)
        if not isinstance(inspected, list) or len(inspected) != len(ids):
            contrib.fail("cannot bind every owned container before artifacts cleanup")
        for item in inspected:
            labels = item["Config"]["Labels"] or {}
            if labels.get("io.xpra.fork-maintenance.owner") != owner:
                contrib.fail("container ownership changed during artifacts planning")
            name = (
                str(item["Name"]).lstrip("/")
                if group == "upstream"
                else str(
                    labels.get(
                        "io.xpra.fork-maintenance.run-id" if group == "live" else "io.xpra.fork-maintenance.run-name",
                        "",
                    )
                )
            )
            result[group].add(identity(name))
    listed = contrib.run(
        ("podman", "network", "ls", "--quiet", "--filter", "label=io.xpra.fork-maintenance.owner=live")
    )
    ids = listed.stdout.splitlines()
    if ids:
        inspected = json.loads(contrib.run(("podman", "network", "inspect", *ids)).stdout)
        if not isinstance(inspected, list) or len(inspected) != len(ids):
            contrib.fail("cannot bind every owned network before artifacts cleanup")
        for item in inspected:
            labels = item.get("labels", item.get("Labels", {})) or {}
            result["live"].add(identity(str(labels.get("io.xpra.fork-maintenance.run-id", ""))))
    return result


def protections(root: Path, *, inspect_runtime: bool = True) -> dict[Path, str]:
    names = podman_identities() if inspect_runtime else {"upstream": set(), "live": set(), "deb": set()}
    # Ownership is an unconditional safety boundary, even if a future policy
    # edit accidentally omits a runtime/lock namespace from recursive keep.
    protected = {
        path.relative_to(root): "retained lifecycle lock" for path in contrib.cleanup_lock_paths(root.parent.parent)
    }
    protected[Path("cycle-cleanups")] = "cleanup transaction infrastructure"
    for path in entries(root, "upstream-tests/runs"):
        if path.name == ".lifecycle.lock":
            continue
        name = suffix_identity(path.name, (".prelaunch.json", ".owner", ".payload"))
        if name is None:
            contrib.fail(f"unrecognized upstream runtime state; use its lifecycle: {path}")
        names["upstream"].add(name)
        protected[path.relative_to(root)] = f"upstream runtime not removed: {name}"
    for path in entries(root, "upstream-tests/image-builds"):
        if path.name == ".image-cache.lock":
            continue
        if path.name.startswith(".") and path.name.endswith(".image-prelaunch.json"):
            name = identity(path.name[1 : -len(".image-prelaunch.json")])
        else:
            name = identity(path.name)
        names["upstream"].add(name)
        protected[path.relative_to(root)] = f"upstream image runtime not removed: {name}"
    for path in entries(root, "jobs/live"):
        if path.name == ".lifecycle.lock":
            continue
        name = suffix_identity(path.name, LIVE_RUNTIME_SUFFIXES)
        if name is not None:
            names["live"].add(name)
        elif suffix_identity(path.name, FINAL_SUFFIXES) is None:
            contrib.fail(f"unrecognized live runtime state; use its lifecycle: {path}")
    for path in entries(root, "deb-packages/runs"):
        name = suffix_identity(path.name, (".prelaunch.json", ".abort.json"))
        names["deb"].add(name or identity(path.name))
        protected[path.relative_to(root)] = f"DEB runtime not removed: {name or path.name}"

    # Exact RUN families: no cycle prefixes, dates, result schemas or success
    # inference. A runtime name containing dots still matches only its suffix.
    for group, directory, suffixes in (
        ("upstream", "upstream-tests/logs", FINAL_SUFFIXES),
        ("live", "jobs/live", LIVE_RUNTIME_SUFFIXES + FINAL_SUFFIXES),
        ("deb", "deb-packages/results", FINAL_SUFFIXES),
    ):
        for path in entries(root, directory):
            if path.name == ".lifecycle.lock":
                continue
            name = suffix_identity(path.name, suffixes)
            if name in names[group]:
                protected[path.relative_to(root)] = f"{group} runtime not removed: {name}"
    for path in entries(root, "live-results"):
        if path.name.startswith("."):
            protected[path.relative_to(root)] = "live staging: use live-abort/remove"
        elif path.name in names["live"]:
            protected[path.relative_to(root)] = f"live runtime not removed: {path.name}"
    for path in entries(root, "deb-packages/outputs"):
        if path.name.startswith("."):
            # Validation staging may refer to the neighbouring tar; preserve
            # the complete output boundary until its owner finishes recovery.
            protected[Path("deb-packages/outputs")] = "DEB validation staging: use deb-remove/abort"
        for name in names["deb"]:
            if path.name.startswith(f"{name}-"):
                protected[path.relative_to(root)] = f"DEB runtime not removed: {name}"
    return protected


def overlaps(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def protect_workspace_recovery(root: Path, protected: dict[Path, str]) -> None:
    authorities = tuple(
        path
        for directory in ("case-staging", "case-updates", "workspace-fingerprints")
        for path in entries(root, directory)
        if path.name != ".lifecycle.lock"
    ) + tuple(
        path for path in entries(root, WORKSPACES) if path.name.startswith(".") and path.name != ".lifecycle.lock"
    )
    for path in authorities:
        protected[path.relative_to(root)] = "case/workspace recovery authority"
    if authorities:
        protected[WORKSPACES] = "case/workspace recovery pending: use case-recover/workspace-recover"


def tree_bytes(path: Path) -> int:
    if not path.is_dir():
        return path.lstat().st_size
    return sum(p.lstat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink())


def inventory_locked(repo: Path, policy: Policy, *, inspect_runtime: bool = True) -> Inventory:
    root = contrib.cleanup_state_root(repo)
    protected = protections(root, inspect_runtime=inspect_runtime)
    targets: list[contrib.CleanupTarget] = []
    blocked: dict[Path, str] = {}
    size = 0
    # Recovery authorities can bind an entire workspace, not just their own
    # staging. Leave all workspaces intact until exact recovery is complete.
    protect_workspace_recovery(root, protected)

    def visit(path: Path) -> None:
        nonlocal size
        relative = path.relative_to(root)
        if relative in policy.keep:
            if path.is_symlink():
                contrib.fail(f"retained artifact path must not be a symlink: {path}")
            return
        if any(relative == keep or keep in relative.parents for keep in protected):
            return
        if relative in policy.containers or any(relative in keep.parents for keep in protected):
            for child in entries(root, relative):
                visit(child)
            return
        try:
            contrib.require_cleanup_parent_chain(root, path)
            if relative.parent == WORKSPACES:
                fingerprint = contrib._finalized_workspace_fingerprint_locked(repo, path.name)
                # Validate physical tree safety now, before publishing a plan.
                contrib.secure_tree_fingerprint(path)
                kind = "workspace"
            else:
                fingerprint = contrib.artifact_fingerprint(path)
                kind = "artifact-tree" if stat.S_ISDIR(path.lstat().st_mode) else "artifact-file"
            size += tree_bytes(path)
            targets.append(contrib.CleanupTarget(kind, path, fingerprint))
        except (contrib.ContribError, OSError) as error:
            # Do not silently downgrade an unfinished candidate or unsafe tree
            # to garbage. The report makes every exception visible.
            if relative.parent == WORKSPACES:
                protected[relative] = str(error)
            else:
                blocked[relative] = str(error)

    for path in entries(root, Path(".")):
        visit(path)
    targets.sort(key=lambda target: (target.path.as_posix(), target.kind))
    provisional = contrib.CleanupPlan(policy.cycle, tuple(targets), "")
    plan = contrib.CleanupPlan(policy.cycle, tuple(targets), contrib.cleanup_plan_digest(repo, provisional))
    return Inventory(
        plan,
        tuple(
            sorted(
                (str(path), reason)
                for path, reason in protected.items()
                if not any(path == keep or keep in path.parents for keep in policy.keep)
            )
        ),
        size,
        tuple(sorted((str(path), reason) for path, reason in blocked.items())),
    )


def validate_pending_policy(
    repo: Path, pending: contrib.CleanupTransaction, policy: Policy, *, inspect_runtime: bool = True
) -> None:
    if pending.plan.cycle != policy.cycle:
        contrib.fail("pending cleanup belongs to another cycle or policy; restore its policy and resume it first")
    root = contrib.cleanup_state_root(repo)
    protected = protections(root, inspect_runtime=inspect_runtime)
    protect_workspace_recovery(root, protected)
    for target in pending.plan.targets:
        relative = target.path.relative_to(root)
        if relative in policy.containers or any(overlaps(relative, keep) for keep in (*policy.keep, *protected)):
            contrib.fail(f"pending cleanup target is now retained or runtime-owned: {relative}")
        if target.kind == "workspace" and relative.parent != WORKSPACES:
            contrib.fail("pending artifacts cleanup has an invalid workspace path")
        if target.kind not in {"workspace", "artifact-file", "artifact-tree"}:
            contrib.fail("pending artifacts cleanup has an invalid target kind")
    contrib.validate_cleanup_plan_state(repo, pending.plan)


def operate(
    repo: Path, action: str, confirmation: str = "", *, policy_path: Path = POLICY_PATH, inspect_runtime: bool = True
) -> Inventory:
    contrib.validate_cleanup_host(repo)
    policy = load_policy(policy_path)
    with contrib.cleanup_lifecycle_locks(repo):
        pending = contrib.load_pending_cleanup_transaction(repo)
        if pending is not None:
            validate_pending_policy(repo, pending, policy, inspect_runtime=inspect_runtime)
            report = Inventory(pending.plan, (), 0)
        else:
            report = inventory_locked(repo, policy, inspect_runtime=inspect_runtime)
        if action == "clean" and confirmation:
            if confirmation != report.plan.digest:
                contrib.fail("CONFIRM does not match artifacts-clean-plan; review the new plan")
            if report.plan.targets:
                if pending is None:
                    contrib.publish_cleanup_transaction(repo, report.plan)
                    pending = contrib.load_pending_cleanup_transaction(repo)
                if pending is None:
                    contrib.fail("artifact cleanup transaction was not published")
                contrib.finish_cleanup_transaction(repo, pending)
        return report


def save(repo: Path, item: str, destination: str) -> Path:
    """Move one unmanaged operator record into retained/, never a runtime tree."""
    contrib.validate_cleanup_host(repo)
    policy = load_policy()
    relative, retained = relative_path(item), relative_path(destination)
    if len(relative.parts) != 1 or relative in (*policy.keep, *policy.containers):
        contrib.fail("artifacts-save accepts only an unmanaged top-level item, never managed runtime/results")
    root = contrib.cleanup_state_root(repo)
    source, target = root / relative, root / "retained" / retained
    with contrib.cleanup_lifecycle_locks(repo):
        if contrib.load_pending_cleanup_transaction(repo) is not None:
            contrib.fail("finish pending cleanup before retaining an item")
        contrib.artifact_fingerprint(source)
        contrib.prepare_cleanup_directory(root, target.parent, "retained operator records")
        contrib.container_payload.rename_no_replace(source, target)
        contrib.fsync_directory(source.parent)
        contrib.fsync_directory(target.parent)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "clean", "check", "save"))
    args = parser.parse_args()
    try:
        if args.action == "save":
            print(
                save(
                    contrib.REPOSITORY_ROOT,
                    os.environ.get("XPRA_ARTIFACT_ITEM", ""),
                    os.environ.get("XPRA_ARTIFACT_AS", ""),
                )
            )
            return 0
        confirmation = os.environ.get("XPRA_ARTIFACT_CONFIRM", "")
        report = operate(contrib.REPOSITORY_ROOT, args.action, confirmation)
        print(
            json.dumps(
                {
                    "policy_sha256": load_policy().digest,
                    "targets": [
                        {
                            "path": str(target.path.relative_to(contrib.cleanup_state_root(contrib.REPOSITORY_ROOT))),
                            "kind": target.kind,
                            "fingerprint": target.fingerprint,
                        }
                        for target in report.plan.targets
                    ],
                    "disposable_bytes": report.bytes,
                    "protected": [{"path": path, "reason": reason} for path, reason in report.protected],
                    "blocked": [{"path": path, "reason": reason} for path, reason in report.blocked],
                },
                indent=2,
            )
        )
        print(f"artifacts_clean_confirm={report.plan.digest}")
        if args.action == "clean" and confirmation:
            print(f"removed_targets={len(report.plan.targets)}")
        else:
            print(f"disposable_targets={len(report.plan.targets)}")
        return 1 if report.blocked or (args.action == "check" and report.plan.targets) else 0
    except (contrib.ContribError, OSError, ValueError, KeyError, TypeError) as error:
        print(f"artifacts: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
