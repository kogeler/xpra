# Investigate A Downstream Patch

## Establish current state without switching branches

Stay on `develop` and prove the isolated boundary:

```bash
make -C fork-maintenance isolated-start-check
```

The gate allows dirty fork-control files but rejects every host Xpra source or
test change. It fetches and verifies master, records the host branch and HEAD,
and never switches, merges, rebases, resets, stashes, stages, or commits. Old
logs and previous patch applicability are leads, not evidence about the newly
recorded master commit.

Read the current affected source, adjacent tests, recent maintainer-authored
history, `CLAUDE.md`, `CONTRIBUTING.md`, the current test workflow, and lint
configuration. Record the first directly observed failing boundary, not a
root-cause guess based on an older symptom.

## Create one case

Use a behavior-based lowercase slug:

```bash
make -C fork-maintenance case-new CASE=short-behavior-name
```

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

For every source or test file created by the candidate, use the file's native
comment syntax to add `Copyright (C) <current-year> kogeler` before staging.
Do not name an upstream maintainer as the author of a downstream-created file.
If the file copies or derives protected content, retain its required notices
and add the `kogeler` line.

Add the completed case to `stacks/develop.toml` in dependency order. Do not
create a tracked report or history directory for the investigation.

An upstream-only failing test is the exception to behavior-based case
creation: update the single `upstream-test-quarantine` duty case under the
rules in `test-quarantine.md`; do not create a production case for it.

## Reassess an existing patch

Resolve it before editing:

```bash
make -C fork-maintenance workspace-create \
  CASE=short-behavior-name WORKSPACE=short-behavior-audit-01 PATCH_MODE=patched
```

- `apply` means the stored patch remains forward-applicable;
- `already-present` means current master contains that exact diff;
- `diverged` means upstream changed the boundary and the full patch must be
  refreshed.

For a claimed upstream replacement, map each original trigger, production
path, state transition, and postcondition to current code. Then run the retained
focused regression on clean master. Do not retire a patch from commit-message
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
