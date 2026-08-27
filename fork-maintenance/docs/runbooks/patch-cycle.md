# Develop And Refresh A Patch

## Default isolated cycle

For an existing case, stay on `develop` and use the isolated workspace flow:

```bash
make -C fork-maintenance isolated-start-check
make -C fork-maintenance workspace-create \
  CASE=short-behavior-name WORKSPACE=short-behavior-01 PATCH_MODE=patched
# edit only the printed workspace source path
make -C fork-maintenance workspace-stage WORKSPACE=short-behavior-01
make -C fork-maintenance workspace-update WORKSPACE=short-behavior-01
make -C fork-maintenance workspace-remove WORKSPACE=short-behavior-01
```

This is the required pre-commit path when fork-control files are uncommitted.
It freezes verified master and never switches the host branch or applies the
production patch to host source. See `isolated-workspaces.md` for clean,
tests-only, path-change, provenance, and cleanup details.

For a complete cycle containing multiple named workspaces and runs, give every
identity one common prefix and finish with the digest-confirmed cleanup flow in
`cycle-cleanup.md`.

A new draft starts with `workspace-create ... PATCH_MODE=clean`; its first
`workspace-update` derives the patch, digest, and owned paths. The quarantine
duty case follows the separate admission and rebase rules in
`test-quarantine.md`.

The remainder of this runbook is the clean host-worktree fallback and the
publication rebase procedure.

## Host-worktree fallback preconditions

Before touching any existing or new patch, refresh the branch base from a clean
checkout in this exact order:

```bash
make -C fork-maintenance repo-sync
make -C fork-maintenance master-update
git switch develop
make -C fork-maintenance develop-rebase
make -C fork-maintenance patch-start-check
make -C fork-maintenance patch-check CASE=short-behavior-name
```

If the fork-master gate reports a mismatch, the operator performs the
documented non-forced sync and `repo-sync` is repeated before `master-update`.
Resolve every rebase conflict and finish the rebase before
`patch-start-check`; do not merge an upstream ref into `develop`. Only after
this sequence may investigation or source editing begin.

Patch operations refuse `master`, stale or merge-updated `develop`, and a
temporary branch that does not descend from fully rebased `develop`. They also
refuse a branch that already has a committed source copy on any path owned by
the selected case.

## Apply one patch

```bash
make -C fork-maintenance patch-apply CASE=short-behavior-name
```

The command resolves the case against current canonical master, skips an exact
`already-present` patch, and applies an `apply` patch with
`git apply --index --whitespace=error-all`. The resulting production and test
files are staged. No commit is created.

Inspect before editing:

```bash
git diff --cached --check
git diff --cached --stat
git diff --cached
```

## Edit and refresh

Edit only the atomic production boundary and its focused tests. Stage the
complete candidate; do not leave unstaged or untracked source:

```bash
git add -- <owned-source-and-test-paths>
git diff --cached --check
make -C fork-maintenance patch-update CASE=short-behavior-name
```

Before `patch-update`, inspect every staged addition (`new file mode`). Each new
downstream-authored source or test file must carry
`Copyright (C) <current-year> kogeler` in its native comment syntax. Keep any
required notice on copied or derived content and add the `kogeler` line; never
attribute a downstream-created file to an upstream maintainer.

`patch-update` exports the full staged binary diff, proves that it applies and
reverses on current master, and atomically derives `fix.patch`,
`patch_sha256`, and `paths`. For an existing case, the staged path set must
match its current ownership. When upstream rework genuinely changes that
boundary, inspect the staged names first and opt in explicitly:

```bash
make -C fork-maintenance patch-update \
  CASE=short-behavior-name ALLOW_PATH_CHANGE=1
```

Do not use this switch to hide an unrelated staged file.

Before scheduling tests, compare the old and refreshed applied trees. When the
master is unchanged and the complete difference is limited to comments,
copyright notices, or documentation, while paths, modes, executable data,
configuration, test assertions, and runner behavior are identical, classify it
as non-semantic. Run resolution, whitespace, and fork-control checks only; do
not launch focused, native, matrix, or live jobs. State the comparison in the
handoff. Any other change follows the full validation ladder.

Review the staged source representation and stored patch representation. Then
restore the committed source tree:

```bash
make -C fork-maintenance patch-unapply CASE=short-behavior-name
git status --short
git diff -- fork-maintenance/cases/short-behavior-name/
```

Only the refreshed case files should remain modified. The unapply gate permits
those exact metadata files and rejects unrelated worktree changes. It reverses
the current stored patch, so it also works immediately after `patch-update`.

Do not use `git reset`, checkout cleanup, or manual file deletion as the normal
patch lifecycle.

## Apply the complete queue

For local integration inspection:

```bash
make -C fork-maintenance stack-apply STACK=develop
# inspect or run a narrow foreground diagnostic
make -C fork-maintenance stack-unapply STACK=develop
```

The resolver applies cases in manifest order and removes them in reverse order.
Failure rolls back cases already applied during that operation. An integration
stack never becomes one atomic case patch.

Long or acceptance tests do not require applying patches to the host worktree;
their runners freeze master and apply the selected queue in isolated source.

## Refresh after upstream advances

With a clean checkout:

```bash
make -C fork-maintenance repo-sync
make -C fork-maintenance master-update
git switch develop
make -C fork-maintenance develop-rebase
```

If rebase stops, resolve and stage each conflict and run
`git rebase --continue` until it completes. Abort and stop if the resolution is
uncertain. Upstream transfer by merge is forbidden. Then prove the new base and
resolve the queue:

```bash
make -C fork-maintenance patch-start-check
make -C fork-maintenance stack-check STACK=develop
```

Before applying the duty quarantine or accepting any patched full run, execute
all three clean `quarantine*` gates from `test-quarantine.md`. Remove or narrow
every entry that is green on the new master; forward applicability alone is
never evidence that a quarantine remains necessary.

Refresh divergent cases one at a time using the apply/edit/update/unapply cycle.
Run focused checks after each case, then the stack gates. Finish with a clean:

```bash
make -C fork-maintenance develop-check
```

## Commit boundary

No automation target commits. An agent may create a local commit only after the
user explicitly authorizes it in the current conversation and the relevant
checks pass. Never commit an applied source copy to clean `develop`; commit the
case/stack/automation changes. Never push.
