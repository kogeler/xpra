# Xpra Fork Maintenance

This directory is the tracked patch queue and automation for
`kogeler/xpra`. It lives inside the Xpra repository: the parent directory is
the source tree, `master` is the periodically synchronized operational fork
base, and `develop` carries this automation.

## Active queue

The active patches are:

1. `wayland-initial-window-state`;
2. `video-pipeline-cleanup-race`;
3. `upstream-test-quarantine` (test-only duty case).

`stacks/develop.toml` applies them in integration order. `develop` here is the
stable queue slug, not a requirement that every consumer run from the Git branch
of that name. Cases contain patch inputs and test requirements; generated run
output is never stored here.

## Layout

```text
fork-maintenance/
├── cases/                  atomic active patches
├── stacks/develop.toml     complete ordered queue
├── infra/upstream-tests/   frozen-master Ubuntu test runner
├── infra/live/             direct Xpra and physical-GPU runner
├── infra/deb-packages/     mount-free Ubuntu/Debian package builder
├── tools/container_payload.py  common validated Podman tar transport
├── tools/contrib.py        sync, branch, patch, and manifest gates
├── docs/runbooks/          operator workflows
├── AGENTS.md               scoped agent rules
├── CONTRACT.md             invariants
└── Makefile                supported interface
```

All durable runtime, build, result, publication, and cache outputs—logs,
reports, screenshots, snapshots, status records, virtual environments, and
caches—live under ignored `.artifacts/fork-maintenance/` at the repository
root. Transient interpreter/tool caches may use another explicitly ignored
local path. Podman runtime objects are owned separately by immutable IDs and
labels.

Interrupted case creation/update/removal and workspace
create/remove/fingerprint publication are stored under ignored `case-staging/`,
`case-updates/`, `upstream-tests/workspaces/`, and
`workspace-fingerprints/` roots. Recover them only through the exact public
`case-recover CASE=<slug>` or `workspace-recover WORKSPACE=<name>` Make target;
cycle cleanup refuses these marker-backed partials and transactions.

GitHub CI is intentionally separate from upstream's active workflow set. The
canonical workflows are byte-identical disabled renames below
`.github/upstream-workflows/`; the only executable files are the thin
`.github/workflows/develop.yml` test caller,
`.github/workflows/master-sync.yml` fork-master sync caller, and
`.github/workflows/deb-packages.yml` manual package-release caller.

## Isolated pre-commit workflow

Stay on `develop` and verify that only fork-control files are dirty:

```bash
make -C fork-maintenance check
make -C fork-maintenance isolated-start-check
make -C fork-maintenance workspace-create \
  CASE=wayland-initial-window-state \
  WORKSPACE=wayland-audit-01 PATCH_MODE=patched
```

The generated source is an exact detached copy of the freshly verified equal
fork/canonical master commit below ignored `.artifacts/`. Edit and stage only
there, then use `workspace-update` to export the complete patch and refreshed
workspace provenance back through one transaction. The successful workspace
remains current and can be edited, staged, and exported again. No command in
this cycle switches the host branch or applies production changes to host
`develop`.

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

The scheduled workflow normally syncs remote fork `master` from upstream every
12 hours. `repo-sync` fetches both master refs, verifies each against live
GitHub state, and requires exact fork/canonical equality. If it reports a stale
fork, only the operator may run the printed non-forced `gh repo sync` command
and repeat `repo-sync`. After equality is proven, update the local mirror:

```bash
make -C fork-maintenance master-update
git switch develop
make -C fork-maintenance develop-rebase
make -C fork-maintenance patch-start-check
make -C fork-maintenance stack-check STACK=develop
```

Run the fetch/master/rebase sequence when preparing a clean host-worktree or
publication cycle. Isolated work can begin directly with
`isolated-start-check`, which performs the fork-master fetch without switching
branches. Both local paths require live fork/upstream equality. Resolve every
rebase conflict before `patch-start-check`. Never merge master or another
upstream ref into `develop`.

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

make -C fork-maintenance live-xpra-hardware \
  STACK=develop RUN=develop-hardware-01
make -C fork-maintenance live-wait RUN=develop-hardware-01
```

The five public live wrappers are positive acceptance gates: Zed RGB,
adaptive-alpha Zed H.264, RGB detach, RGB transport-loss fault injection, and
multi-window hardware H.264. They fix every profile dimension and require a
nonempty reviewed selection; clean-source and picture-fallback diagnostics
cannot publish `PASS`.

The hardware target resolves both windows by title. The primary's initial
`BGRX`/`RGBX` snapshot and dynamic opaque frame-state history lead to stable,
predominant H.264 main regions plus complete per-crop coverage by only exact
one-pixel lossless RGB codec edges, all through the VA-API and
hardware-presentation chain. Its deterministic transparent native-Wayland GTK
auxiliary must prove transparent and opaque pixels and emit only positive WebP
or alpha-bearing RGB32 packets. See the live runbook for the exact grouping,
thresholds, and evidence contract.

Use the separate status, logs, collect, and exact cleanup targets documented in
the runbooks. Abort a running or lost uncollected test only with
`make -C fork-maintenance test-abort RUN=name`; the same target may exact-
discard a completed uncollected job only after a runner change makes it stale.
An active detached-test starter is refused, while its inactive exact prelaunch
owner can recover an orphaned labelled container/payload. A current completed
job must be collected. Never bypass the Make lifecycle with direct process
signals or destructive Podman commands. Standalone image, live, and DEB jobs
have matching `test-image-abort`, `live-abort`, and `deb-abort` targets. Live
start first publishes `jobs/live/<RUN>.freeze-prelaunch.json`; local DEB abort
publishes `deb-packages/runs/<RUN>.abort.json` before changing owned state and
deletes it only after the exact abort transaction completes. Freeze-only live
abort similarly uses `jobs/live/<RUN>.freeze-abort.json` plus exact hidden
directory staging, and only a retry of `live-abort` completes it. A result
remains local even when it is final. Each collected remove operation first
publishes a retained evidence-bound transaction, so an interrupted removal is
retried through the same exact Make target and is never repaired by hand. Once
a live main owner is gone, `live-status` reports `phase=removing` or
`phase=removed` only after validating that exact transaction and its retained
evidence; `live-logs` likewise returns only the digest-bound final log.

After every fork-master rebase, reassess the duty quarantine against clean
master before running the patched matrix:

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

Reusable content-verified frozen source bundles and archives, immutable DEB
selection snapshots, input-keyed build contexts and images, ccache, and virtual
environments are retained by default. Before the first deletion, cleanup
publishes `cycle-cleanups/<CYCLE>.remove.json`; an interruption is resumed with
the same cycle and confirmation digest rather than replanned or repaired by
hand. The transaction binds directory device/inode/fingerprint state and uses
exact hidden staging below `cycle-cleanups/`. Before recursive deletion it
publishes a bound `.<CYCLE>.<index>.rmtree.json` phase for each directory, so an
interrupted partial deletion resumes by exact device/inode rather than requiring
the original tree hash.

## DEB packages

Package builds are branch-agnostic. They locate the clean source boundary
between `HEAD` and refs whose final component is `master`, reject downstream
merge commits and source overlays, apply the complete `stacks/develop` queue,
and exchange source and package tars with Podman through stdin/stdout without
bind mounts. The stack name is a queue slug, not a current-branch requirement.
Each build binds the retained
`selections/<selection-sha>-<metadata-sha>/{lab,selection.json}` snapshot and
both of its digests:

```bash
make -C fork-maintenance deb-start \
  DISTRO=ubuntu-26.04 RUN=packages-ubuntu-01
make -C fork-maintenance deb-wait RUN=packages-ubuntu-01
make -C fork-maintenance deb-remove RUN=packages-ubuntu-01
```

Use `DISTRO=debian-13` and a different `RUN` for Debian. These amd64 builds need
an x86-64 Podman host, network access, and sufficient disk space; packages are
built unsigned with `dpkg-buildpackage -us -uc`. The manual-only
`deb-packages.yml` workflow builds both validated tars from one frozen selection
snapshot, stages and verifies a draft GitHub prerelease, then publishes its
unique tag at the selected checkout. A rerun may reclaim only an exact orphan
draft from an earlier failed attempt of that same hosted run. Drafts are created
through authenticated REST and bound to the immutable release ID returned by
that request; bounded paginated release listing, never the published-only tag
lookup, proves absence or finds one exact recoverable draft. Rollback validates
the exact release, deletes and verifies its unchanged tag first, and deletes the
immutable release ID last; published, tag-only, or ambiguous state is preserved.
See
[`docs/runbooks/deb-packages.md`](docs/runbooks/deb-packages.md).

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
after every fork-master rebase so new or modified canonical workflows remain
disabled exact renames.

## Master sync

At 00:37 and 12:37 UTC, the separate hosted workflow invokes the guarded
`ci-master-sync` Make target. It fast-forwards only `kogeler/xpra:master` from
`Xpra-org/xpra:master`, never uses force, and never changes `develop`. It also
supports manual operator dispatch. The operator later fetches fork master,
updates local master, and manually rebases develop. See
[`docs/runbooks/master-sync.md`](docs/runbooks/master-sync.md).

## Documentation

- [`CONTRACT.md`](CONTRACT.md): branch, patch, validation, and storage invariants;
- [`docs/runbooks/bootstrap.md`](docs/runbooks/bootstrap.md): remotes and host
  setup;
- [`docs/runbooks/investigate.md`](docs/runbooks/investigate.md): establish a new
  patch boundary;
- [`docs/runbooks/isolated-workspaces.md`](docs/runbooks/isolated-workspaces.md):
  default pre-commit patch cycle;
- [`docs/runbooks/patch-cycle.md`](docs/runbooks/patch-cycle.md): apply, edit,
  refresh, and remove;
- [`docs/runbooks/upstream-tests.md`](docs/runbooks/upstream-tests.md): container
  test matrix;
- [`docs/runbooks/ci.md`](docs/runbooks/ci.md): thin develop workflow and
  disabled upstream CI;
- [`docs/runbooks/master-sync.md`](docs/runbooks/master-sync.md): scheduled
  fork-master fast-forward;
- [`docs/runbooks/deb-packages.md`](docs/runbooks/deb-packages.md):
  branch-agnostic DEB builds and manual releases;
- [`docs/runbooks/test-quarantine.md`](docs/runbooks/test-quarantine.md):
  temporary upstream test quarantine;
- [`docs/runbooks/live-tests.md`](docs/runbooks/live-tests.md): physical and
  lifecycle profiles;
- [`docs/runbooks/publish-develop.md`](docs/runbooks/publish-develop.md): operator
  handoff;
- [`docs/runbooks/artifacts.md`](docs/runbooks/artifacts.md): local output and
  cleanup;
- [`docs/runbooks/cycle-cleanup.md`](docs/runbooks/cycle-cleanup.md): finalize and
  remove one exact work cycle.

No target creates a new content commit, pushes `develop`, creates a pull
request, or changes the fork's default branch. The hosted-only
`ci-master-sync` target may only fast-forward fork `master`. The hosted-only
`ci-deb-release` target may create only its unique draft/prerelease, package tag,
and two validated tar assets, with exact tag-first/release-last rollback of only
its just-created release and a tag still targeting the dispatched commit on
failure. A retry may also apply that ordered rollback to the exact draft/tag of
an earlier failed attempt of that same hosted workflow run after validating its
Actions and embedded transaction records. Published, tag-only, or ambiguous
state is preserved. Agents invoke neither hosted mutation target. `develop-rebase` only
replays existing local commits onto fetched fork master.
