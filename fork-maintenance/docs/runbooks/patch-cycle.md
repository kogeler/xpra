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
# repeat edit, stage, and update as needed; the exported workspace remains current
make -C fork-maintenance workspace-remove WORKSPACE=short-behavior-01
```

This is the required pre-commit path when fork-control files are uncommitted.
It freezes the source merge base already embedded in current `develop`, without
fetching or testing master freshness, and never switches the host branch or
applies the production patch to host source. See
[`isolated-workspaces.md`](isolated-workspaces.md) for clean, tests-only,
path-change, provenance, and cleanup details.

For a complete cycle containing multiple named workspaces and runs, give every
identity one common prefix and finish with the digest-confirmed cleanup flow in
[`cycle-cleanup.md`](cycle-cleanup.md).

A new draft starts with `workspace-create ... PATCH_MODE=clean`; its first
`workspace-update` derives the patch, digest, and owned paths. The quarantine
duty case follows the separate admission and rebase rules in
[`test-quarantine.md`](test-quarantine.md).

The remainder of this runbook is the clean host-worktree fallback used only
when the operator deliberately begins a new upstream adaptation cycle. The
complete rebase, selected-case decision tree, queue-wide tests, and seven live
profiles are owned by [`upstream-refresh.md`](upstream-refresh.md).
That canonical runbook first creates one reviewed preservation commit when
non-ignored work exists; its invocation authorizes that commit without another
confirmation. It creates no empty commit for a clean checkout and no later
intermediate or result commit.

## Host-worktree fallback preconditions

Only when the operator chooses to move the queue to a newer upstream base,
complete the canonical runbook's exhaustive initial-worktree review and its
one preservation commit when needed. Once that leaves `develop` clean, fetch
the fork base and prepare the branch in this order:

```bash
make -C fork-maintenance repo-sync
make -C fork-maintenance master-update
git switch develop
make -C fork-maintenance develop-rebase
make -C fork-maintenance patch-start-check
make -C fork-maintenance patch-check CASE=short-behavior-name
```

`repo-sync` fetches both master refs, verifies each against live GitHub state,
and requires exact fork/canonical equality. If it reports a stale fork, only
the operator may run the printed non-forced `gh repo sync` command and repeat
the gate. Resolve every rebase conflict and finish the rebase before
`patch-start-check`; do not merge an upstream ref into `develop`. Only after
this sequence may host-worktree source editing against that new base begin. The
default isolated cycle, all tests, and live acceptance of current `develop` do
not require these commands, a branch switch, live equality, or a rebase.

Patch operations refuse `master`, stale or merge-updated `develop`, and a
temporary branch that does not descend from the refreshed `develop`. A temporary
branch is supported only for this exceptional clean host-worktree flow; the
default isolated lifecycle remains on `develop`. Host patch operations also
refuse a branch that already has a committed source copy on any path owned by
the selected case.

## Apply one patch

```bash
make -C fork-maintenance patch-apply CASE=short-behavior-name
```

The command resolves the case against the explicitly refreshed source, skips an exact
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
reverses on that refreshed source, and atomically derives `fix.patch`,
`patch_sha256`, and `paths`. For an existing case, the staged path set must
match its current ownership. When upstream rework genuinely changes that
boundary, inspect the staged names first and opt in explicitly:

```bash
make -C fork-maintenance patch-update \
  CASE=short-behavior-name ALLOW_PATH_CHANGE=1
```

Do not use this switch to hide an unrelated staged file.

`patch-update` and `workspace-update` first publish ignored
`case-updates/<slug>.update.owner.json` plus an exact old/new transaction under
retained `.lifecycle.lock`. Their application/reverse proof uses only the
transaction's temporary `candidate-lab/source`; it does not add verification
scratch to a finalized workspace. A workspace update also replaces that
workspace's selection resolution and metadata in the transaction, so the
successful workspace remains reusable. If interrupted, use only:

```bash
make -C fork-maintenance case-recover CASE=short-behavior-name
```

The recovery target aborts only an incomplete preparation, completes a
published `transaction.json`, or clears an owner-only boundary after validating
the current case and any bound workspace. It never guesses from partially
replaced tracked files. Before recursively deleting a preparation or completed
transaction it publishes schema-1 `<slug>.update.remove.json`, stages the tree
at `.<slug>.update.remove`, removes the external update owner after the tree,
and removes the phase last. Its canonical target array revalidates the exact
published outputs by path, mode, and SHA-256 even during phase-only retry. An
unresolved update blocks all later case updates and cycle cleanup.

Before scheduling tests, compare the old and refreshed applied trees. When the
embedded source is unchanged and the complete difference is limited to comments,
copyright notices, or documentation, while paths, modes, executable data,
configuration, test assertions, and runner behavior are identical, classify it
as non-semantic. Run resolution, whitespace, and fork-control checks only; do
not launch focused, native, matrix, or live jobs. State the comparison in the
handoff. A rebase changes the embedded source and never qualifies for this
exception. Any other change follows the full validation ladder.

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
their runners freeze the embedded source and apply the selected queue in isolation.

## Operator-selected upstream refresh

An advance of any master ref does not itself invalidate or block current
`develop`. Use this procedure only when the operator intentionally chooses that
new commit as the next patch-adaptation base. This section is a summary;
[`upstream-refresh.md`](upstream-refresh.md) is the canonical executable
runbook.

After the canonical runbook's initial preservation boundary has left the
checkout clean:

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
all three clean `quarantine*` gates from
[`test-quarantine.md`](test-quarantine.md). Remove or narrow every entry that is
green on the new source; forward applicability alone is never evidence that a
quarantine remains necessary.

An upstream rebase always invalidates the previous functional acceptance,
including when every patch still applies byte-for-byte. Run the complete
offline fork-control suite, a tests-only clean control for every production
case which owns retained tests and the documented no-test semantic inspection
otherwise, every patched focused and native gate, all three complete upstream
workflow legs, every case-specific durable package boundary against the
resulting stack, and all seven fixed positive live profiles. A new author-test
failure may enter the single quarantine only after the same module fails on
this exact clean source; then rerun the quarantine gates and complete patched
matrix.

An `apply` case uses the ordinary isolated `PATCH_MODE=patched` flow. A
`diverged` case cannot: both `patch-apply` and ordinary patched workspace
creation intentionally stop at resolver failure. Reconstruct its complete
candidate in the provenance-bound isolated `PATCH_MODE=reconstruct` mode from
`upstream-refresh.md`; never edit or stage host Xpra source, use rejects or
fuzz, export only conflict hunks, or create an intermediate cleanliness commit.
Run focused checks after each case, then the stack gates. If no tracked content
changed, finish with:

```bash
make -C fork-maintenance develop-check
```

If refreshed queue or control-plane files remain uncommitted, `develop-check`
correctly refuses the dirty checkout. Leave that final gate explicitly
outstanding in the handoff for the operator; the agent does not create an
intermediate or final refresh-result commit to satisfy the gate.

## Commit boundary

No automation target commits. Invocation of the canonical upstream-refresh
runbook itself authorizes exactly one direct local preservation commit before
fetch/rebase when exhaustive review finds legitimate non-ignored changes. It
contains the complete reviewed tracked and untracked set, is omitted for a
clean checkout, and never contains an applied source copy. After that boundary
the agent leaves all case, stack, and automation refresh results uncommitted
for the operator; no intermediate or final result commit is allowed merely to
restore a clean gate. Never push.
