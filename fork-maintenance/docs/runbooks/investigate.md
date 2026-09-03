# Investigate A Downstream Patch

## Establish current state without switching branches

Stay on `develop` and prove the isolated boundary:

```bash
make -C fork-maintenance isolated-start-check
```

The gate allows dirty fork-control files but rejects every host Xpra source or
test change. It records the host branch, HEAD, and unique source merge base
already embedded in current `develop`; it never fetches, queries moving master
refs, switches, merges, rebases, resets, stashes, stages, or commits. Old logs
and previous patch applicability are leads, not evidence about that recorded
source commit.

Read the current affected source, adjacent tests, recent maintainer-authored
history, `CLAUDE.md`, `CONTRIBUTING.md`, the current test workflow, and lint
configuration. Record the first directly observed failing boundary, not a
root-cause guess based on an older symptom.

## Create one case

Use a behavior-based lowercase slug:

```bash
make -C fork-maintenance case-new CASE=short-behavior-name
```

If an interrupted `case-new` left marker-backed staging, inspect it and recover
only that case identity before retrying:

```bash
make -C fork-maintenance case-recover CASE=short-behavior-name
```

Recovery removes the exact stale marker/partial, or validates an already
published draft and removes only its marker. Unowned or ambiguous staging is an
operator-review boundary.

The draft is intentionally unselectable by test jobs. Complete its
human-authored kind, title, commit subject, dependencies, focused tests,
required gates, and README. Leave `draft`, `patch_sha256`, and `paths`
unchanged. Create a clean isolated workspace for the first candidate:

```bash
make -C fork-maintenance workspace-create \
  CASE=short-behavior-name WORKSPACE=short-behavior-01 PATCH_MODE=clean
# edit only the printed workspace source and add the smallest regression
make -C fork-maintenance workspace-stage WORKSPACE=short-behavior-01
make -C fork-maintenance workspace-update WORKSPACE=short-behavior-01
```

Draft promotion is an exact case-update transaction: it binds the new patch and
manifest together with the promoted workspace metadata and resolution. If the
export is interrupted, run `case-recover CASE=short-behavior-name`; it discards
only an incomplete preparation, finishes a complete `transaction.json`, or
clears an owner-only boundary after validating the published case and workspace.
Before recursively deleting a preparation or completed transaction, recovery
publishes `<slug>.update.remove.json`, stages the exact tree at
`.<slug>.update.remove`, deletes the update owner after the tree, and deletes
the removal phase last. Its canonical target array continues to validate the
published case and bound workspace outputs during phase-only retry. Never
hand-edit `case-updates/`; unresolved update state blocks further updates and
cycle cleanup.

For every source or test file created by the candidate, use the file's native
comment syntax to add `Copyright (C) <current-year> kogeler` before staging.
Do not name an upstream maintainer as the author of a downstream-created file.
If the file copies or derives protected content, retain its required notices
and add the `kogeler` line.

Add the completed case to `stacks/develop.toml` in dependency order. Do not
create a tracked report or history directory for the investigation.

An upstream-only failing test is the exception to behavior-based case
creation: update the single `upstream-test-quarantine` duty case under the
rules in [`test-quarantine.md`](test-quarantine.md); do not create a production
case for it.

## Reassess an existing patch

Resolve it before creating a workspace:

```bash
make -C fork-maintenance patch-check CASE=short-behavior-name
```

If resolution is `apply` or exact `already-present`, create the audit
workspace:

```bash
make -C fork-maintenance workspace-create \
  CASE=short-behavior-name WORKSPACE=short-behavior-audit-01 PATCH_MODE=patched
```

- `apply` means the stored patch remains forward-applicable;
- `already-present` means the embedded source contains that exact diff;
- `ambiguous` stops before workspace creation. During an explicitly authorized
  upstream refresh, a proven `diverged` case uses the provenance-bound isolated
  `PATCH_MODE=reconstruct` flow in
  [`upstream-refresh.md`](upstream-refresh.md); ordinary patched workspace and
  host `patch-apply` modes deliberately cannot force that state. Reconstruction
  keeps host source/index untouched and does not create an intermediate commit.

For a claimed upstream replacement, map each original trigger, production
path, state transition, and postcondition to current code. Then run the retained
focused regression on the clean embedded source. Do not retire a patch from commit-message
similarity alone.

If existing tests do not observe the disputed path, improve the case-owned test
or durable runner first. A copied test, temporary source rewrite, or one-off
container command is diagnostic only.

## Controls and unrelated failures

For graphics work, keep clean/patched controls on the same commit, image,
endpoint distribution, render node, dimensions, compositor, application, and
profile. Prefer the real application when buffer format or damage cadence is
part of the failure.

When a full-suite test outside selected paths fails, inspect canonical Actions
for the exact base and leg before starting a local clean control. Never skip,
weaken, reconfigure, or repair that foreign test inside the current production
case. Explicitly authorized quarantine belongs only in the duty case and must
pass its clean reassessment gates.
