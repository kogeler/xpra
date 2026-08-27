# Xpra Fork Maintenance

This directory is the tracked patch queue and automation for
`kogeler/xpra`. It lives inside the Xpra repository: the parent directory is
the source tree, `master` mirrors `Xpra-org/xpra`, and `develop` carries this
automation.

## Active queue

The active patches are:

1. `wayland-initial-window-state`;
2. `video-pipeline-cleanup-race`;
3. `upstream-test-quarantine` (test-only duty case).

`stacks/develop.toml` applies them in integration order. Cases contain patch
inputs and test requirements; generated run output is never stored here.

## Layout

```text
fork-maintenance/
├── cases/                  atomic active patches
├── stacks/develop.toml     complete ordered queue
├── infra/upstream-tests/   frozen-master Ubuntu test runner
├── infra/live/             direct Xpra and physical-GPU runner
├── tools/contrib.py        sync, branch, patch, and manifest gates
├── docs/runbooks/          operator workflows
├── AGENTS.md               scoped agent rules
├── CONTRACT.md             invariants
└── Makefile                supported interface
```

All logs, reports, screenshots, snapshots, status records, virtual
environments, and other outputs live under ignored
`.artifacts/fork-maintenance/` at the repository root.

GitHub CI is intentionally separate from upstream's active workflow set. The
canonical workflows are byte-identical disabled renames below
`.github/upstream-workflows/`; the only executable file is
`.github/workflows/develop.yml`, which calls one public Make target.

## Isolated pre-commit workflow

Stay on `develop` and verify that only fork-control files are dirty:

```bash
make -C fork-maintenance check
make -C fork-maintenance isolated-start-check
make -C fork-maintenance workspace-create \
  CASE=wayland-initial-window-state \
  WORKSPACE=wayland-audit-01 PATCH_MODE=patched
```

The generated source is an exact detached copy of verified master below
ignored `.artifacts/`. Edit and stage only there, then use `workspace-update`
to export the complete patch back to its case. No command in this cycle
switches the host branch or applies production changes to host `develop`.

See [`docs/runbooks/isolated-workspaces.md`](docs/runbooks/isolated-workspaces.md).

New cases also stay off the host source tree: create the draft, complete its
human fields, then start its workspace with `PATCH_MODE=clean`. The workspace
export derives the patch digest and path ownership.

## Clean publication checks

Run commands from the Xpra root:

```bash
make -C fork-maintenance check
make -C fork-maintenance repo-status
make -C fork-maintenance repo-sync
```

If `repo-sync` reports a live fork-master mismatch, only the operator runs the
non-forced synchronization command it prints. Repeat `repo-sync`, then update
the local mirror:

```bash
make -C fork-maintenance master-update
git switch develop
make -C fork-maintenance develop-rebase
make -C fork-maintenance patch-start-check
make -C fork-maintenance stack-check STACK=develop
```

Run this sync/master/rebase sequence before beginning work on every patch,
even when the previous work session used the same base. Resolve every rebase
conflict before `patch-start-check`. Never merge master or another upstream ref
into `develop`.

## Host-worktree fallback

Only after the clean publication start gate, a patch may be applied to clean
`develop` for exceptional host integration diagnosis:

```bash
make -C fork-maintenance patch-apply CASE=wayland-initial-window-state
# edit source and focused tests, then stage every owned path
git diff --cached --check
make -C fork-maintenance patch-update CASE=wayland-initial-window-state
make -C fork-maintenance patch-unapply CASE=wayland-initial-window-state
```

After `patch-unapply`, the committed Xpra source is restored and the refreshed
case files remain for review. Do not commit an applied source copy on
`develop`.

Apply or remove the full queue for local integration diagnosis:

```bash
make -C fork-maintenance stack-apply STACK=develop
make -C fork-maintenance stack-unapply STACK=develop
```

## Durable tests

Every job name is unique, including retries:

```bash
make -C fork-maintenance test-start \
  STACK=develop TARGET=focused RUN=develop-focused-01
make -C fork-maintenance test-wait RUN=develop-focused-01

make -C fork-maintenance live-start \
  STACK=develop ENCODING=h264 H264_CLIENT_POLICY=adaptive-alpha \
  RUN=develop-h264-01
make -C fork-maintenance live-wait RUN=develop-h264-01
```

Use the separate status, logs, collect, and exact cleanup targets documented in
the runbooks. Abort an unfinished test only with
`make -C fork-maintenance test-abort RUN=name`; never bypass the Make lifecycle
with direct process signals or destructive Podman commands. Image and live jobs
have matching `test-image-abort` and `live-abort` targets. A result remains local
even when it is final.

After every upstream rebase, reassess the duty quarantine against clean master
before running the patched matrix:

```bash
make -C fork-maintenance test-start \
  CASE=upstream-test-quarantine PATCH_MODE=clean \
  TARGET=quarantine RUN=rebase-quarantine-01
```

Repeat for `quarantine-cython` and `quarantine-no-compat`. These gates are
green only while each listed module is still non-green; a newly passing module
must be removed or narrowed in the quarantine case.

After a whole prefixed work cycle is finalized and reviewed, delete its
collected results and finalized workspaces through an exact two-phase plan:

```bash
make -C fork-maintenance cycle-clean-plan CYCLE=cycle-prefix
make -C fork-maintenance cycle-clean \
  CYCLE=cycle-prefix CONFIRM=<sha256-from-plan>
```

Reusable source/build caches, images, ccache, and virtual environments are
retained by default.

## Develop CI

A push to `develop` runs the complete patched upstream unit-test matrix on three
parallel GitHub-hosted Ubuntu 26.04 runners. Every matrix job uses the same local
entry point with its fixed `XPRA_CI_TARGET`:

```bash
XPRA_CI_TARGET=full make -C fork-maintenance ci-upstream-tests
```

The other values are `full-cython` and `full-no-compat`. Each target invocation
applies `stacks/develop` before its one leg. The workflow contains no build or
test implementation and never starts live/GPU profiles. Run `ci-layout-check`
after every upstream rebase so new or modified canonical workflows remain
disabled exact renames.

## Documentation

- [`CONTRACT.md`](CONTRACT.md): branch, patch, validation, and storage invariants;
- [`docs/runbooks/bootstrap.md`](docs/runbooks/bootstrap.md): remotes and host setup;
- [`docs/runbooks/investigate.md`](docs/runbooks/investigate.md): establish a new patch boundary;
- [`docs/runbooks/isolated-workspaces.md`](docs/runbooks/isolated-workspaces.md): default pre-commit patch cycle;
- [`docs/runbooks/patch-cycle.md`](docs/runbooks/patch-cycle.md): apply, edit, refresh, and remove;
- [`docs/runbooks/upstream-tests.md`](docs/runbooks/upstream-tests.md): container test matrix;
- [`docs/runbooks/ci.md`](docs/runbooks/ci.md): thin develop workflow and disabled upstream CI;
- [`docs/runbooks/test-quarantine.md`](docs/runbooks/test-quarantine.md): temporary upstream test quarantine;
- [`docs/runbooks/live-tests.md`](docs/runbooks/live-tests.md): physical and lifecycle profiles;
- [`docs/runbooks/publish-develop.md`](docs/runbooks/publish-develop.md): operator handoff;
- [`docs/runbooks/artifacts.md`](docs/runbooks/artifacts.md): local output and cleanup.
- [`docs/runbooks/cycle-cleanup.md`](docs/runbooks/cycle-cleanup.md): finalize and remove one exact work cycle.

No target creates a new content commit, pushes, changes a remote branch,
creates a pull request, or changes the fork's default branch.
`develop-rebase` only replays existing local commits onto verified master.
