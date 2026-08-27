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
3. collect every upstream-test, image-build, and live result;
4. review failures through their first failed boundary;
5. complete all clean quarantine reassessment gates for a rebase cycle;
6. run the exact per-job cleanup target for every collected job;
7. finish the required validation ladder, or document the proven non-semantic
   exception, and run the offline automation checks.

The per-job cleanup commands are:

```bash
make -C fork-maintenance test-remove RUN=cycle-run-name
make -C fork-maintenance live-remove RUN=cycle-run-name
make -C fork-maintenance test-image-remove IMAGE_RUN=cycle-image-name
make -C fork-maintenance test-abort RUN=unfinished-cycle-run-name
make -C fork-maintenance live-abort RUN=unfinished-cycle-live-name
make -C fork-maintenance test-image-abort IMAGE_RUN=unfinished-cycle-image-name
```

They validate and remove owned process and Podman state while retaining the
collected result for final review. `cycle-clean-plan` refuses to proceed while
an owner record, runtime log or completion record, collection lock, container,
or network remains. It never stops an active job on the operator's behalf.

The abort targets are the exception for unfinished jobs: they validate and
remove only the exact owned runtime state and intentionally retain no result.
Invoke lifecycle operations through Make only, never through direct process
signals or destructive Podman commands.

## Review the exact plan

Remain on `develop`, with no host Xpra source changes, and run:

```bash
make -C fork-maintenance cycle-clean-plan \
  CYCLE=wayland-audit-20260827
```

The JSON plan lists every exact relative target, its kind, and a content or
workspace fingerprint. It also prints `cycle_clean_confirm=<sha256>`.

The planner accepts only:

- complete, owner-bound upstream logs, statuses, and resolution records whose
  hashes agree;
- complete, owner-bound live logs, statuses, and result trees whose report and
  log hashes agree;
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

The command rebuilds the plan, rechecks runtime ownership and fingerprints,
and refuses a stale or mistyped digest. It then removes only those exact
collected files, live result directories, and finalized workspace directories.
If an external failure interrupts removal, inspect what remains and safely
re-plan it with a new digest; never compensate with a broad `rm` glob.

## Retained reusable state

Ordinary cycle cleanup deliberately keeps:

- content-addressed source bundles and live source archives;
- content-addressed build contexts and Podman images;
- the upstream-test ccache volume;
- hash-locked live and local tooling virtual environments.

These are shared caches, not cycle-owned results. The current upstream test
image and ccache have their own explicit, label-verified removal targets where
documented. Removing live caches or virtual environments is an owner-reviewed
disk-maintenance action, not part of patch finalization.
