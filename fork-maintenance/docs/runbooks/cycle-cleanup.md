# Finalize And Clean One Work Cycle

## Purpose

Use this flow only after the patch queue for one work cycle is final, every
required result has been collected and reviewed, and no more retries will use
that cycle identity. It removes disposable results and generated workspaces
below `.artifacts/fork-maintenance/` without touching Xpra source, case files,
the host index, branches, or reusable caches.

Choose one lowercase cycle prefix before starting work and put it at the start
of every `RUN`, `IMAGE_RUN`, and `WORKSPACE`, followed by a dash:

```text
wayland-audit-20260827-focused-01
wayland-audit-20260827-image-01
wayland-audit-20260827-patched-01
```

Here the cleanup identity is `CYCLE=wayland-audit-20260827`. A cycle identity
and every run identity are never reused.

## Completion boundary

Before planning deletion:

1. export every accepted workspace candidate with `workspace-update`;
2. review the resulting `fix.patch`, derived manifest fields, and stack;
3. recover any inspected interrupted case creation/update/removal or workspace
   create/remove/fingerprint staging with the exact `case-recover` or
   `workspace-recover` target;
4. collect every upstream-test, image-build, live, and DEB result;
5. review failures through their first failed boundary;
6. complete all clean quarantine reassessment gates for a rebase cycle;
7. run the exact per-job cleanup target for every collected job;
8. finish the required validation ladder, or document the proven non-semantic
   unchanged-base exception, and run the offline automation checks. A rebase
   cycle never qualifies for that exception.

The per-job cleanup commands are:

```bash
make -C fork-maintenance test-remove RUN=cycle-run-name
make -C fork-maintenance live-remove RUN=cycle-run-name
make -C fork-maintenance test-image-remove IMAGE_RUN=cycle-image-name
make -C fork-maintenance deb-remove RUN=cycle-package-name
make -C fork-maintenance test-abort RUN=running-or-lost-cycle-run-name
make -C fork-maintenance live-abort RUN=running-or-lost-cycle-live-name
make -C fork-maintenance test-image-abort IMAGE_RUN=running-or-lost-cycle-image-name
make -C fork-maintenance deb-abort RUN=running-or-lost-cycle-package-name
```

They validate and remove owned process and Podman state while retaining the
collected result and its evidence-bound removal transaction for final review.
If removal is interrupted, rerun the same target: it validates that retained
transaction before finishing only the exact old runtime deletion.
`cycle-clean-plan` refuses to proceed while a run owner, runtime log,
completion record, recoverable prelaunch/partial, case
create/update/removal phase, workspace create/remove/fingerprint record,
container, or network remains. Retained subsystem lock files are validated but
are not themselves results. The planner never stops an active job on the
operator's behalf.

The abort targets accept running or lost uncollected jobs and completed
uncollected jobs only after their recorded runner becomes stale. `lost` means
that no valid completion exists and the exact owned process/container runtime
is gone; a dead process-group leader with a live owned member remains running.
For that orphaned group, every live member must carry the private 256-bit token
recorded by the owner and completion; a missing, duplicate, or mismatched token
preserves the state for review. A legacy tokenless orphan is never signaled.
The targets reject a current completed job, which must be collected,
and validate and remove only the exact owned runtime state without retaining a
result. An inactive upstream-test prelaunch marker may be reclaimed by
`test-abort`, while an active starter is refused. Invoke lifecycle operations
through Make only, never through direct process signals or destructive Podman
commands. A freeze-only `live-abort` first publishes
`jobs/live/<RUN>.freeze-abort.json`, binds and atomically stages its exact input
directories at `live-results/.<RUN>.freeze-abort-{staging,result}`, and removes
that transaction last; an interruption is recovered only by retrying the same
abort target.

## Review the exact plan

Remain on the current checkout, with no host Xpra source changes, and run:

```bash
make -C fork-maintenance cycle-clean-plan \
  CYCLE=wayland-audit-20260827
```

The JSON plan lists every exact relative target, its kind, and a content or
workspace fingerprint. It also prints `cycle_clean_confirm=<sha256>`.
The planner is branch-agnostic, supports a detached `HEAD`, and requires no
named remote. It does not read, switch, or update refs. Planning and confirmed
execution acquire all retained locks in one fixed order: upstream-test
lifecycle, upstream image cache, live lifecycle, DEB terminal, workspace
lifecycle, then case-update lifecycle. This prevents runtime transitions and
case exports from racing evidence or workspace fingerprinting.
If a cleanup transaction is already pending, only this same `CYCLE` can be
planned; the command validates and prints its stored plan/digest. Every other
cycle remains blocked until the pending transaction completes.

The planner accepts only:

- complete, owner-bound upstream logs, statuses, and removal transactions whose
  log hash and single embedded selection-resolution digest agree (legacy
  sidecars, when present, must agree too);
- complete, owner-bound live logs, statuses, removal transactions, and result
  trees whose report and log hashes agree;
- finalized DEB status/log/removal-transaction sets whose owner, paths, and log
  SHA-256 agree, plus an output tar with its exact digest only when
  `validation_ok` is true;
- isolated workspaces with no unstaged or untracked candidate and whose staged
  tree is exactly represented by the current selected patch queue.

Git-originated relative symlinks inside an owner-bound result are fingerprinted
as links and never followed. An absolute target or a relative target that
escapes the owned result tree blocks cleanup. Owned group-writable build inputs
are accepted only below the result's mode-`0700` root; other-writable and
hard-linked files are rejected.

An edited but unexported workspace is a hard stop. Export it or review and
remove it explicitly; do not use cycle cleanup to discard unfinished work.

## Execute the unchanged plan

After reviewing every target, pass back the exact printed digest:

```bash
make -C fork-maintenance cycle-clean \
  CYCLE=wayland-audit-20260827 \
  CONFIRM=<sha256-from-cycle-clean-plan>
```

When no transaction is pending, the command rebuilds the plan, rechecks runtime
ownership and fingerprints, and refuses a stale or mistyped digest. A retry
instead validates the stored transaction. Before its first deletion it publishes
schema-2 `cycle-cleanups/<CYCLE>.remove.json`, binding the reviewed plan and
digest plus every workspace/live-result directory's device, inode, and
fingerprint. Directory targets are atomically staged at
`cycle-cleanups/.<CYCLE>.<index>.remove`. After validating the complete staged
tree and before recursive deletion, cleanup publishes schema-1
`cycle-cleanups/.<CYCLE>.<index>.rmtree.json` with
`kind=cycle-clean-rmtree-started`. It binds the outer transaction plus the
staging device/inode. Once that phase exists, an exact retry deliberately does
not require the original fingerprint from a tree already changed by `rmtree`.
If an external failure interrupts removal, inspect the retained transaction and
rerun `cycle-clean` with the same `CYCLE` and `CONFIRM`; it resumes only that
exact transaction, removes each phase after its staging is gone, and deletes the
outer marker after every target is gone. Do not re-plan an interrupted
transaction or compensate with a broad `rm` glob.

## Retained reusable state

Ordinary cycle cleanup deliberately keeps:

- content-verified frozen upstream-test bundles, their validated publication
  lock files, and live source archives;
- branch-agnostic package source bundles, immutable selection snapshots, and
  their retained source/selection publication locks;
- the validated upstream/live subsystem lifecycle locks, upstream foreground
  payload and image-cache locks, the workspace and case-update lifecycle locks,
  and DEB terminal plus per-image-key locks;
- input-keyed build contexts and label-verified Podman images;
- the upstream-test ccache volume;
- the hash-locked live environment and any local tooling virtual environment.

These are shared caches, not cycle-owned results. The current upstream-test
image has its own explicit, label-verified removal target. Persistent ccache
has no ordinary automatic removal target. Removing ccache, live caches, or
virtual environments is an owner-reviewed disk-maintenance action, not part of
patch finalization.

The owned `cycle-cleanups/` directory is retained as transaction
infrastructure. A successful cleanup leaves it empty; a marker or exact hidden
directory staging/`rmtree` phase means the same transaction must be resumed,
not treated as a cache.

The live environment's retained `.environment.lock` and any marker-owned
`.environment.partial` are also outside cycle ownership. The next explicit
`live-venv` call performs exact locked recovery; cycle cleanup neither consumes
nor deletes that shared state.

Hosted `deb-packages/releases/` staging is also outside the local cycle-name
contract. It is retained for operator review and never removed implicitly by
cycle cleanup.

The planner fails on any upstream `.bundle.partial`, foreground payload, named
image-build prelaunch/context, live freeze prelaunch/abort transaction or
staging, DEB source/selection partial, abort transaction, output-validation
scratch, case creation/update/removal transaction, or workspace
create/remove/fingerprint staging. The matching exact snapshot, validation,
abort, remove, or public `case-recover` / `workspace-recover` operation performs
guarded recovery; cycle cleanup never guesses that a partial is safe to delete.
