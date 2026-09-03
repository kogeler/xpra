# Develop And Audit Patches In An Isolated Workspace

## Purpose

Use this flow before the first fork-control commit or whenever `develop`
contains uncommitted case or automation work. It never switches the host
branch, stages the host index, or overlays production changes on the Xpra
source inherited by `develop`.

The generated source lives below:

```text
.artifacts/fork-maintenance/upstream-tests/workspaces/<name>/source/
```

It is a private detached copy of the unique source merge base already embedded
in current `develop`. Its local `origin` remote is removed immediately. The
workspace metadata binds the host branch and HEAD, embedded source commit and
tree, selection and patch digests, resolution, and patch mode.
All workspace operations and fingerprint publication are serialized by retained
`upstream-tests/workspaces/.lifecycle.lock`.

This lifecycle is supported only while the host remains on `develop`. Temporary
branches belong only to the exceptional clean host-worktree flow in
[`patch-cycle.md`](patch-cycle.md); they are not an isolated-workspace
alternative.

## Start boundary

Remain on `develop` and run:

```bash
make -C fork-maintenance isolated-start-check
```

The gate locates the unique source merge base of current `develop` and cached
`origin/master`. The cached ref is a local history anchor only: the command
does not fetch, query upstream, require master freshness/equality, or rebase.
It permits dirty files only below the fork control boundary:

- `AGENTS.md`;
- `.gitignore`;
- `.github/workflows/`;
- `.github/upstream-workflows/`;
- `fork-maintenance/`.

Any host Xpra source or test change fails the gate. The command records and
rechecks the current branch, HEAD, and complete worktree state; it does not
switch, merge, rebase, reset, stash, stage, or commit.

## Create a workspace

Use a never-reused name:

```bash
make -C fork-maintenance workspace-create \
  CASE=wayland-initial-window-state \
  WORKSPACE=wayland-audit-01 \
  PATCH_MODE=patched
```

Available modes are:

- `patched`: apply the complete forward-applicable case or stack;
- `tests-only`: apply only its `tests/` paths to unmodified embedded-source production
  code;
- `clean`: apply nothing while retaining ordinary selection and resolution
  provenance;
- `reconstruct`: start one completed case from clean embedded source only after
  proving that its current patch is exactly `diverged` (neither forward- nor
  reverse-applicable).

`reconstruct` is a workspace-only recovery mode, not a test-runner patch mode.
It rejects stacks, drafts, cases with dependencies, `apply`, `already-present`,
and `ambiguous` patches. A dependent case needs a separately designed
dependency-aware reconstruction boundary; the clean-source mode must not record
an empty or invented dependency resolution.
Its private resolution binds the old manifest, patch, paths, selection digest,
and source commit. This permits a complete replacement without applying a
broken patch to the host or making an intermediate control-plane commit:

```bash
make -C fork-maintenance workspace-create \
  CASE=wayland-empty-damage-throttle \
  WORKSPACE=refresh-empty-damage-01 \
  PATCH_MODE=reconstruct
```

Build the full replacement in the clean workspace, then use the ordinary
`workspace-stage` and `workspace-update` commands below. Export repeats the
old-patch divergence and provenance checks before the existing atomic case
update transaction verifies and publishes the new forward/reverse-applicable
patch. An empty replacement is refused; retire an upstream-complete case
through the reviewed queue-removal workflow instead.

Use `tests-only` for the non-vacuous upstream control. It cannot be exported as
a complete case patch.

For a new case, first run `case-new` and complete only its human-authored
manifest fields and README. A draft is not selectable by test runners, but it
can start one isolated workspace in clean mode:

```bash
make -C fork-maintenance workspace-create \
  CASE=short-behavior-name WORKSPACE=short-behavior-01 PATCH_MODE=clean
```

Edit and stage the first complete candidate there. `workspace-update` promotes
the draft and derives `fix.patch`, `patch_sha256`, and `paths`; never type those
derived fields by hand. This draft flow also leaves host Xpra source and its
index unchanged.

Inspect generated state with:

```bash
make -C fork-maintenance workspace-status WORKSPACE=wayland-audit-01
make -C fork-maintenance workspace-diff WORKSPACE=wayland-audit-01
```

`workspace-status` and `workspace-diff` are deliberately read-only and can
inspect a structurally valid workspace created at an older host commit. Status
prints both the recorded and current branch/HEAD and reports
`host_identity=current` or `host_identity=stale`; diff prints its exact staged
candidate against the recorded source tree. A stale result may be reviewed and
removed through the exact removal lifecycle, but it cannot be staged or
exported: every candidate-mutating stage/update command continues to require
the original host branch and HEAD. Exact stale removal remains the deliberate
exception after the read-only review.

If creation, direct removal, or a cleanup fingerprint audit was interrupted,
inspect its marker-backed state and recover only that workspace identity before
retrying:

```bash
make -C fork-maintenance workspace-recover WORKSPACE=wayland-audit-01
```

Recovery preserves an already published valid workspace and resolves only its
exact create marker/partial, removal transaction, or fingerprint transaction.
Direct removal uses external
`upstream-tests/workspaces/.<name>.remove.owner.json` and atomically staged
`.<name>.remove`. Fingerprinting publishes external schema-2
`workspace-fingerprints/<name>.fingerprint.owner.json` before creating
`<name>.fingerprint`; recursive cleanup then publishes
`<name>.fingerprint.remove.json` and stages at
`.<name>.fingerprint.remove`. After either rename, recovery validates the bound
device/inode rather than re-hashing a partially deleted tree. The fingerprint
phase binds the owner operation ID and digest; it remains after owner removal
and is itself deleted last. An unowned or ambiguous partial is refused.

## Refresh one case

Edit only files below the printed workspace `source` path. Stage the complete
candidate in that private index:

```bash
make -C fork-maintenance workspace-stage WORKSPACE=wayland-audit-01
```

Review every staged addition (`new file mode`) before export. A new
downstream-authored source or test file must contain
`Copyright (C) <current-year> kogeler` in its native comment syntax. Preserve
required notices in copied or derived content and add the `kogeler` line; do
not assign downstream authorship to an upstream maintainer.

When a current-source reassessment genuinely changes path ownership, inspect
every changed path first and opt in explicitly:

```bash
make -C fork-maintenance workspace-stage \
  WORKSPACE=wayland-audit-01 ALLOW_PATH_CHANGE=1
```

Export the staged atomic candidate:

```bash
make -C fork-maintenance workspace-update WORKSPACE=wayland-audit-01
```

`workspace-update` proves whitespace, full forward application, and exact
reverse application on the recorded embedded source commit. It atomically derives the
case patch, digest, and path list. The only host files it may change are that
case's `fix.patch` and `case.toml`; host source and index stay untouched.
Apply/reverse verification runs in the recoverable
`case-updates/<slug>.update/candidate-lab/source` preparation, not in the
finalized workspace. That candidate lab is removed before publication of the
old/new transaction marker. The transaction also replaces the workspace's
`selection-resolution.json` and `workspace.json`, leaving a successful
workspace current and reusable for another edit/stage/export iteration.

Before replacing either tracked case file, the command publishes
`.artifacts/fork-maintenance/case-updates/<slug>.update.owner.json` and prepares
the matching `<slug>.update/` old/new payload transaction under retained
`case-updates/.lifecycle.lock`. Workspace update holds its lifecycle lock first,
then the case-update lock. Every export binds the case patch/manifest and
workspace provenance in that same all-new transaction; draft promotion also
moves the workspace from clean to patched mode. If interrupted, do not rerun
export or edit this state:

```bash
make -C fork-maintenance case-recover CASE=wayland-initial-window-state
```

Recovery discards only a preparation with no published `transaction.json`,
replays a complete transaction to its recorded new state, or clears an
owner-only boundary after validating the published case and bound workspace.
Before deleting a preparation or completed transaction, it publishes schema-1
`case-updates/<slug>.update.remove.json` with disposition `abort` or `complete`,
renames the transaction to `case-updates/.<slug>.update.remove`, removes the
update owner after the tree, and removes the phase marker last. Its canonical
target array revalidates the published patch, manifest, workspace resolution,
and workspace metadata by path, mode, and SHA-256 even after partial staged-tree
deletion or during a phase-only retry. Any unresolved update blocks another
case update and the cycle-clean planner.

## Build and test

Classify the exported diff before starting a job. If the embedded source is
unchanged and an exact old/new applied-tree comparison contains only comments,
copyright notices, or documentation—with identical paths, modes, executable
data, configuration, test assertions, and runner behavior—this is a
non-semantic refresh. Resolve the queue and run whitespace plus fork-control
checks, but do not rerun focused, native, full, or live jobs. Record that proof
in the handoff. A changed embedded source never qualifies: after an upstream
rebase use the complete post-rebase ladder. Any uncertainty uses the normal
ladder below.

The Ubuntu runner independently freezes the same embedded source and selection.
To prove the retained regression against clean production:

```bash
make -C fork-maintenance test-start \
  CASE=wayland-initial-window-state PATCH_MODE=tests-only \
  TARGET=focused RUN=wayland-master-regression-01
make -C fork-maintenance test-wait RUN=wayland-master-regression-01
```

Then run the case or stack with `PATCH_MODE=patched`. Every live acceptance run
also names a nonempty reviewed case or stack; clean-source comparison remains
an isolated/unit diagnostic and cannot publish a live `PASS`. All runners use
generated source copies and never package the host worktree.

## Exact cleanup

After reviewing and exporting the workspace, remove only its owned generated
directory:

```bash
make -C fork-maintenance workspace-remove WORKSPACE=wayland-audit-01
```

Removal is not reversible, but it affects runtime output only. Before touching
the directory, `workspace-remove` publishes its exact external owner and binds
the complete fingerprint, device, and inode. It then uses a no-replace rename to
the hidden removal staging and removes that owner last. Reinvoke
`workspace-remove`, or use `workspace-recover`, to complete an interrupted
transaction. The maintained case patch and every host source file remain
untouched.

When several workspaces and named runs belong to one completed cycle, prefer
the two-phase `cycle-clean-plan` / `cycle-clean` flow in
[`cycle-cleanup.md`](cycle-cleanup.md). Unlike direct workspace removal, its
planner proves that each staged workspace tree is exactly represented by the
current patch queue and binds every target to one reviewed confirmation digest.
The planner never performs staging recovery; any case/workspace marker or
partial must first go through its public exact recovery target.
