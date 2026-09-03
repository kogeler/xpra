# Synchronize Fork Master

## Purpose

Remote `kogeler/xpra:master` is a periodically synchronized, operator-maintained
upstream reference. The thin `.github/workflows/master-sync.yml` workflow attempts to
fast-forward it from `Xpra-org/xpra:master` at 00:37 and 12:37 UTC and supports
an operator-triggered `workflow_dispatch`. Equality with upstream is guaranteed
by a successful sync run, not continuously between runs. The workflow updates
only remote fork `master`; it never changes `develop` or any local branch. Its
freshness or equality with upstream is not a prerequisite for workspace work,
tests, live acceptance, CI reproduction, or publication of current `develop`.

GitHub evaluates scheduled workflows from the repository's default branch, so
this workflow and its Make implementation remain committed on default
`develop` even though the remote ref being synchronized is explicitly
`refs/heads/master`. A default-branch change requires an immediate audit of
this schedule. GitHub may delay scheduled jobs under load. The operator can use
the manual launch below when deliberately preparing a new upstream-adaptation
cycle.

## Hosted boundary

The workflow grants `contents: write` only to its one sync job, checks out the
automation from `develop` at depth one without persisting credentials, supplies
the ephemeral `GITHUB_TOKEN` as `GH_TOKEN`, and invokes only:

```bash
make -C fork-maintenance ci-master-sync
```

The target refuses local execution and accepts only `schedule` or
`workflow_dispatch` from the exact `master-sync.yml@develop` workflow in
`kogeler/xpra`. It requires a clean exact develop checkout and verifies that
the checkout remains unchanged.

When live master refs differ, the only mutation is equivalent to:

```bash
gh repo sync kogeler/xpra \
  --source Xpra-org/xpra \
  --branch master
```

Never add `--force`. GitHub CLI therefore permits only a fast-forward. The
target queries both live refs again and succeeds only when they are exactly
equal. An ahead or divergent fork, missing ref, authorization or branch-rule
failure, concurrent upstream movement, or post-sync mismatch fails closed for
owner review.

Agents never invoke `ci-master-sync`, trigger `workflow_dispatch`, or provide a
token for it. Unit tests mock the remote mutation and assert the exact command.

## Manual launch

The workflow is explicitly launchable through the GitHub Actions UI or by the
operator from the command line:

```bash
gh workflow run master-sync.yml --repo kogeler/xpra --ref develop
```

When this dispatch belongs to an explicit refresh cycle, wait for it to finish.
Before fetching the fork master it produced, enter the one-start-commit boundary
in [`upstream-refresh.md`](upstream-refresh.md): inspect every non-ignored
change and, iff legitimate work exists, preserve the complete reviewed set in
the one autonomous local commit authorized by invoking that runbook. A clean
checkout gets no empty commit, and no later refresh-result commit is allowed.
Only after the runbook records that commit SHA or `<none>` and requires clean
porcelain, fetch and verify:

```bash
make -C fork-maintenance repo-sync
```

Agents never execute this dispatch. This explicit-refresh command fetches and
verifies both master refs and requires exact fork/canonical equality. If it
still reports a stale fork, the operator may run the same non-forced
`gh repo sync` command directly, then repeat `repo-sync`; an ahead or divergent
fork remains an owner-review boundary.

## Manual develop refresh

Automatic master synchronization deliberately does not rebase or invalidate
`develop`. When the operator is ready to move the embedded source boundary and
adapt the complete queue to it, invoke the **Autonomous Upstream Refresh and
Full Queue Adaptation** procedure with:

```text
Execute autonomous-upstream-refresh PRIMARY_CASE=<slug> against the current fork master.
```

This is an agent directive, not a shell command. The exhaustive procedure is
[`upstream-refresh.md`](upstream-refresh.md); `PRIMARY_CASE` changes only
review order and detail, never scope. Its initial local
worktree review and optional one preservation commit must finish before the
first command below. That invocation needs no additional commit confirmation;
all later adaptation and validation results remain uncommitted. Its ref and
rebase sequence is:

```bash
make -C fork-maintenance repo-sync
make -C fork-maintenance master-update
git switch develop
make -C fork-maintenance develop-rebase
make -C fork-maintenance patch-start-check
```

Resolve every rebase conflict, reassess the quarantine case, resolve the active
queue, and run the complete post-rebase ladder before publishing the rewritten
`develop` with an exact-SHA force-with-lease. The ladder is mandatory even when
the queue applies unchanged: offline fork checks, clean quarantine reassessment,
tests-only controls for cases which own retained tests, case-specific no-test
semantic inspection, durable package boundaries against the resulting stack,
including both real Ubuntu 26.04 and Debian 13 builds, patched focused/native
gates, all three full author-test legs, every production case's declared live
gates with its atomic case selection, and all seven fixed positive live
profiles with the complete stack selection. If the operator does not choose
this refresh,
current `develop` continues to be tested and published against its existing
embedded source regardless of later master movement.
