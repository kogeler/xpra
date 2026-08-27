# Fork Maintenance Agent Guide

This directory is the tracked control plane for maintaining the user's Xpra
fork. The repository root is the Xpra source tree; there is no nested clone.
The root `AGENTS.md` remains authoritative and this file adds rules for the
patch queue and runners.

## Required reading

Before changing this directory, read `CONTRACT.md` and the relevant document
under `docs/runbooks/`. Before changing a case, read its complete `case.toml`,
`README.md`, and patch. Before changing a runner, read its entry point,
container recipe, tests, and the disabled canonical workflow it mirrors at
`../.github/upstream-workflows/test.yml`.

## Layout

- `cases/`: atomic production patches plus the single test-only quarantine case;
- `stacks/develop.toml`: the ordered complete queue;
- `infra/upstream-tests/`: frozen-master container test runner;
- `infra/live/`: direct-transport and physical-GPU runner;
- `tools/contrib.py`: branch, sync, patch-queue, and manifest safety gates;
- `docs/runbooks/`: operator workflows;
- `CONTRACT.md`: machine and process invariants.

The only active GitHub workflow is
`../.github/workflows/develop.yml`. Canonical upstream workflows are
byte-identical disabled renames below `../.github/upstream-workflows/`; they are
not editable fork templates or historical runner snapshots.

Historical results, superseded patches, publication drafts, and copied runner
snapshots are not part of this layout. Do not recreate the removed `evidence/`,
`investigations/`, `verifications/`, or `communications/` history as tracked
content.

## Current-state gates

`repo-sync` is read-only with respect to remote branches: it fetches cached
refs and verifies both against live state. Only the operator can repair a stale
fork master with the documented non-forced `gh repo sync`. `master-update`
may fast-forward only the local mirror and rejects ahead or divergent history.

Host-worktree patch application still requires clean `develop`, an updated
local `master`, `develop-rebase`, and `patch-start-check`. Pre-commit
investigation and development use `isolated-start-check` instead: remain on
`develop`, allow dirt only in the fork control plane, and copy the exact live
master commit into a named private workspace below `.artifacts/`. Never switch
branches or modify the host Xpra source for this cycle.

`develop-check` requires:

- clean `develop`;
- local, fork, and canonical master at the same verified commit;
- current master as the linear base of `develop`;
- no merge commits in the fork-only `master..develop` range;
- no committed Xpra source changes outside the patch queue representation;
- a resolvable `stacks/develop.toml`;
- an effective ignore boundary for `.artifacts/fork-maintenance/`.

Do not weaken these checks to accommodate a dirty or stale checkout.

## Developing patches

Run `isolated-start-check`, then create one atomic workspace with
`workspace-create CASE=<id> WORKSPACE=<unique-name>`. Make source and regression
changes only below that workspace, use `workspace-stage`, and export them with
`workspace-update`; it atomically derives `fix.patch`, `patch_sha256`, and
`paths` while leaving the host source and index untouched. Review both
representations, then remove only the exact owned workspace. The old
apply/update/unapply cycle remains available only after the clean host gate.

Before export, inspect every `new file mode` entry. Each new downstream-authored
source or test file must carry `Copyright (C) <current-year> kogeler` in its
native comment syntax; never substitute an upstream maintainer's authorship.
Retain required notices on copied or derived content and add the `kogeler` line.

For integration diagnosis and acceptance, create a stack workspace or use the
frozen-master container runner. Do not export a stack as one atomic case patch.
Host `stack-apply` and `stack-unapply` remain a clean-checkout fallback only.

All known upstream-only test failures belong in
`upstream-test-quarantine`, never in a production case. A quarantine addition
requires a clean-master reproduction in every affected matrix leg. After each
upstream rebase, run the three clean `quarantine*` gates before applying that
case; a newly green module makes the quarantine stale and must be removed or
narrowed before the patched full matrix is accepted.

When upstream absorbs a patch exactly, the resolver reports
`already-present`. Removing it from the active queue still requires a current
code review and the case's relevant tests on clean master. Since run output is
local-only, record the conclusion in the commit message or external discussion,
not a new tracked evidence archive.

## Runners and artifacts

Local acceptance runners freeze the verified live `upstream/master` commit.
Hosted CI uses checkout `origin/master` only to find the merge base already in
pushed `develop`, freezes that exact commit, and never follows a moving live
upstream ref during the job. It neither creates an `upstream` remote nor fetches,
syncs, switches, merges, or rebases after `actions/checkout`. Both paths apply
the selected queue in an isolated context and never package the develop working
tree, `.git`, credentials, ignored files, or Git configuration.

Runtime state is rooted at repository-level `.artifacts/fork-maintenance/`.
It is private, no-clobber, and ignored. Long container tests use detached
Podman containers; image builds and live runs use the owned Python process
supervisor. Collection validates a real completion record, complete log hash,
runner provenance, and exact owned-object cleanup. Reports and status files
remain local even when they are final.

Give every run and workspace in one work cycle a common lowercase prefix.
After the patch and validation are final and reviewed, use `cycle-clean-plan`
and pass its exact digest to `cycle-clean`. The planner must reject active or
unremoved runtime state, incomplete evidence, and any workspace candidate not
represented by the current queue. Ordinary cycle cleanup retains shared
content-addressed caches, images, ccache, and virtual environments.

Keep direct Xpra behavior separate from SSH or parent-product orchestration.
The live runner owns direct-TCP detach, abrupt transport loss, RGB, adaptive
Wayland H.264, and multi-window hardware profiles. Do not replace these with
foreground one-off commands when deciding whether a patch is ready.

Invoke job lifecycle operations only through the root Makefile targets. Never
signal recorded process groups or call destructive Podman commands directly for
a named job; `test-abort`, `test-remove`, and the corresponding live/image
targets must own the exact process, container, and record transition. If a
required transition has no target, add and test it before acting.

GitHub CI is a thin caller, not a second automation implementation. Its YAML may
select the `develop` push trigger, Ubuntu 26.04 runner, minimal permissions,
timeout, a full-SHA-pinned checkout action, and the exact fixed matrix of
`full`, `full-cython`, and `full-no-compat`. Each matrix job passes its value as
`XPRA_CI_TARGET`; its only command is
`make -C fork-maintenance ci-upstream-tests`. All source freezing, image
building, patch application, target validation, and test implementation stay
behind that Make target. Do not put apt, Podman, Python, retries, skips, dynamic
test discovery, or test commands into the workflow. Matrix fail-fast remains
disabled and `max-parallel` remains three so all three independent results are
collected concurrently when hosted runners are available. CI never invokes
`live-*` targets.

Do not invoke `ci-layout-check` from the hosted CI entry point. It is an
explicit rebase/publication audit; GitHub workflow selection must not become a
self-referential prerequisite for running the upstream tests.

The CI target may use foreground containers because the GitHub job owns the
outer lifecycle and log. Local patch acceptance continues to use the named
Make lifecycle; a CI run does not replace physical live gates or their local
evidence.

## Change boundary

Use `apply_patch` for edits. Preserve upstream style in Python, shell,
Containerfiles, TOML, and Make. Update tests whenever path, manifest, lifecycle,
or safety behavior changes. Run the narrow unit tests before the full offline
`make -C fork-maintenance check`.

A refresh proven by exact applied-tree comparison to change only comments,
copyright notices, or documentation does not rerun Xpra focused, native, full,
or live jobs. The master, paths, modes, executable data, configuration, test
assertions, and runner behavior must all be unchanged. Run resolution,
whitespace, and fork-control checks and report the non-semantic proof. Any
uncertainty falls back to the normal validation ladder.

When a run fails before entering an expensive test target, validate a fix to
that pre-test guard with the narrow control-plane unit test and direct preflight
command. Do not launch the downstream matrix merely to test removal of the
guard when its master, selection and patch digests, image inputs, entrypoint,
and test commands are unchanged. This is a proportional-validation rule, not
permission to skip tests whose inputs or execution behavior changed.

No target in this directory commits, pushes, synchronizes a remote branch,
creates a pull request, or changes the default branch. Do not add one.
