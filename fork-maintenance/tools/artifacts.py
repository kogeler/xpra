# Copyright (C) 2026 kogeler
"""Permanent-policy filesystem garbage collection, separate from acceptance.

The policy selects storage classes; runtime ownership and workspace export
checks protect unfinished work. Neither historical result schemas nor dates
decide whether disposable output can be discarded. Deletion reuses the locked,
digest-bound, crash-resumable cycle-cleanup transaction engine.

Two scopes share that engine. ``clean`` is mid-session housekeeping and keeps
session work and caches. ``close`` ends a session: it refuses while any runtime,
recovery state, invalid knowledge record or foreign ``.artifacts`` entry
remains, then leaves only the distilled knowledge base and idle lock files.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import contrib
import knowledge
import tomllib

POLICY_PATH = contrib.AUTOMATION_ROOT / "artifacts.toml"
WORKSPACES = Path("upstream-tests/workspaces")
WORK = Path("work")
CLEAN = "clean"
CLOSE = "close"
GROUPS = ("permanent", "infrastructure", "task", "containers")
IDLE_PROTECTIONS = {"retained lifecycle lock", "cleanup transaction infrastructure"}
ACTIONS = {
    "plan": (CLEAN, "plan"),
    "clean": (CLEAN, "clean"),
    "check": (CLEAN, "check"),
    "close-plan": (CLOSE, "plan"),
    "close": (CLOSE, "clean"),
    "close-check": (CLOSE, "check"),
}
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
    permanent: tuple[Path, ...]
    infrastructure: tuple[Path, ...]
    task: tuple[Path, ...]
    containers: tuple[Path, ...]
    digest: str

    def keep(self, scope: str) -> tuple[Path, ...]:
        """Session close discards task state; knowledge and infrastructure stay."""
        kept = (*self.permanent, *self.infrastructure)
        return kept if scope == CLOSE else (*kept, *self.task)

    def cycle(self, scope: str) -> str:
        return f"artifacts-{self.digest}" if scope == CLEAN else f"artifacts-close-{self.digest}"


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
    if set(data) != {"schema", *GROUPS} or data["schema"] != 2:
        contrib.fail("unsupported artifact policy schema")
    groups = []
    for key in GROUPS:
        values = data[key]
        if not isinstance(values, list) or not values:
            contrib.fail(f"artifact policy {key} must be a nonempty array")
        paths = tuple(sorted(relative_path(value) for value in values))
        if len(set(paths)) != len(paths):
            contrib.fail(f"artifact policy repeats a {key} path")
        groups.append(paths)
    permanent, infrastructure, task, containers = groups
    everything = (*permanent, *infrastructure, *task, *containers)
    if len(set(everything)) != len(everything):
        contrib.fail("artifact policy assigns a path to more than one class")
    for path in everything:
        if any(parent not in containers for parent in path.parents if parent != Path(".")):
            contrib.fail(f"artifact policy lacks an explicit structural parent: {path}")
    if permanent != (knowledge.ROOT,):
        contrib.fail("only the distilled knowledge base may survive session close")
    if WORK not in task or WORKSPACES not in containers:
        contrib.fail("artifact policy must keep session work until close and classify workspaces")
    return Policy(permanent, infrastructure, task, containers, contrib.sha256_bytes(payload))


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


def busy_paths(path: Path) -> Iterator[Path]:
    """Yield everything except directories and empty lock files; never follow links."""
    if not path.exists() and not path.is_symlink():
        return
    info = path.lstat()
    if stat.S_ISDIR(info.st_mode):
        for child in sorted(path.iterdir()):
            yield from busy_paths(child)
    elif not (stat.S_ISREG(info.st_mode) and path.name.endswith(".lock") and info.st_size == 0):
        yield path


def close_blockers(root: Path, policy: Policy, protected: dict[Path, str]) -> dict[Path, str]:
    """Everything that would outlive the session besides knowledge and idle locks."""
    blockers = {
        path: f"session close: {reason}" for path, reason in protected.items() if reason not in IDLE_PROTECTIONS
    }
    for relative in policy.infrastructure:
        for path in busy_paths(root / relative):
            blockers.setdefault(
                path.relative_to(root),
                "session close: lifecycle state is not idle; finish it through its owning target",
            )
    for path, reason in knowledge.inspect(root)[1]:
        blockers[path] = f"session close: knowledge: {reason}"
    contrib.require_owned_directory(root.parent, "artifact root")
    for path in sorted(root.parent.iterdir()):
        if path != root:
            blockers[Path("..") / path.name] = (
                "session close: outside .artifacts/fork-maintenance; move it into work/ and re-plan"
            )
    return blockers


@contextmanager
def idle_cache_locks(targets: tuple[contrib.CleanupTarget, ...]) -> Iterator[None]:
    """Hold every lock file a target discards, so no cache publisher is active."""
    descriptors: list[int] = []
    try:
        for target in targets:
            path = target.path
            if path.is_symlink() or not path.exists():
                continue  # already staged by an interrupted transaction
            for candidate in sorted(path.iterdir()) if path.is_dir() else (path,):
                if candidate.is_symlink() or not candidate.is_file() or not candidate.name.endswith(".lock"):
                    continue
                descriptors.append(os.open(candidate, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW))
                try:
                    fcntl.flock(descriptors[-1], fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    contrib.fail(f"cache lock is held by an active process: {candidate}")
        yield
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def inventory_locked(repo: Path, policy: Policy, scope: str = CLEAN, *, inspect_runtime: bool = True) -> Inventory:
    root = contrib.cleanup_state_root(repo)
    keep = policy.keep(scope)
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
        if relative in keep:
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
    if scope == CLOSE:
        blocked.update(close_blockers(root, policy, protected))
    targets.sort(key=lambda target: (target.path.as_posix(), target.kind))
    provisional = contrib.CleanupPlan(policy.cycle(scope), tuple(targets), "")
    plan = contrib.CleanupPlan(policy.cycle(scope), tuple(targets), contrib.cleanup_plan_digest(repo, provisional))
    return Inventory(
        plan,
        tuple(
            sorted(
                (str(path), reason)
                for path, reason in protected.items()
                if not any(path == kept or kept in path.parents for kept in keep)
            )
        ),
        size,
        tuple(sorted((str(path), reason) for path, reason in blocked.items())),
    )


def validate_pending_policy(
    repo: Path, pending: contrib.CleanupTransaction, policy: Policy, scope: str = CLEAN, *, inspect_runtime: bool = True
) -> None:
    if pending.plan.cycle != policy.cycle(scope):
        contrib.fail(
            "pending cleanup belongs to another cycle, policy or scope; restore its policy and resume it"
            " with its original command first"
        )
    root = contrib.cleanup_state_root(repo)
    protected = protections(root, inspect_runtime=inspect_runtime)
    protect_workspace_recovery(root, protected)
    for target in pending.plan.targets:
        relative = target.path.relative_to(root)
        if relative in policy.containers or any(overlaps(relative, kept) for kept in (*policy.keep(scope), *protected)):
            contrib.fail(f"pending cleanup target is now retained or runtime-owned: {relative}")
        if target.kind == "workspace" and relative.parent != WORKSPACES:
            contrib.fail("pending artifacts cleanup has an invalid workspace path")
        if target.kind not in {"workspace", "artifact-file", "artifact-tree"}:
            contrib.fail("pending artifacts cleanup has an invalid target kind")
    contrib.validate_cleanup_plan_state(repo, pending.plan)


def operate(
    repo: Path,
    action: str,
    confirmation: str = "",
    *,
    scope: str = CLEAN,
    policy_path: Path = POLICY_PATH,
    inspect_runtime: bool = True,
) -> Inventory:
    for directory in (repo / ".artifacts", contrib.cleanup_state_root(repo)):
        if not directory.exists() and not directory.is_symlink():
            directory.mkdir(mode=0o700)  # a fresh or fully closed checkout
    contrib.validate_cleanup_host(repo)
    policy = load_policy(policy_path)
    with contrib.cleanup_lifecycle_locks(repo):
        pending = contrib.load_pending_cleanup_transaction(repo)
        if pending is not None:
            validate_pending_policy(repo, pending, policy, scope, inspect_runtime=inspect_runtime)
            report = Inventory(pending.plan, (), 0)
        else:
            report = inventory_locked(repo, policy, scope, inspect_runtime=inspect_runtime)
        if action == "clean" and confirmation:
            if confirmation != report.plan.digest:
                contrib.fail(f"CONFIRM does not match artifacts-{scope}-plan; review the new plan")
            if scope == CLOSE and report.blocked:
                # Never discard caches or session work underneath live state.
                contrib.fail("artifacts-close deletes nothing while a blocked path remains; resolve it and re-plan")
            if report.plan.targets:
                with idle_cache_locks(report.plan.targets):
                    if pending is None:
                        contrib.publish_cleanup_transaction(repo, report.plan)
                        pending = contrib.load_pending_cleanup_transaction(repo)
                    if pending is None:
                        contrib.fail("artifact cleanup transaction was not published")
                    contrib.finish_cleanup_transaction(repo, pending)
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=tuple(ACTIONS))
    args = parser.parse_args()
    scope, action = ACTIONS[args.action]
    try:
        confirmation = os.environ.get("XPRA_ARTIFACT_CONFIRM", "")
        report = operate(contrib.REPOSITORY_ROOT, action, confirmation, scope=scope)
        print(
            json.dumps(
                {
                    "policy_sha256": load_policy().digest,
                    "scope": scope,
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
        print(f"artifacts_{scope}_confirm={report.plan.digest}")
        if action == "clean" and confirmation:
            print(f"removed_targets={len(report.plan.targets)}")
        else:
            print(f"disposable_targets={len(report.plan.targets)}")
        incomplete = bool(report.blocked or (action == "check" and report.plan.targets))
        if scope == CLOSE and action == "check":
            print(f"session_closed={'no' if incomplete else 'yes'}")
        return 1 if incomplete else 0
    except (contrib.ContribError, OSError, ValueError, KeyError, TypeError) as error:
        print(f"artifacts: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
