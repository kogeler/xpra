# Xpra Fork Maintenance

This directory is the tracked patch queue and automation for
`kogeler/xpra`. It lives inside the Xpra repository: the parent directory is
the source tree, `master` is an operator-maintained upstream reference, and
`develop` carries this automation plus the source boundary against which its
current patch queue was adapted.

## Process authority

Our workflows are defined only by the fork-owned root [`AGENTS.md`](../AGENTS.md)
and this directory's [agent guide](AGENTS.md), [contract](CONTRACT.md), runbooks
and manifests, subject to explicit operator instructions. Follow those sources
strictly for process decisions.

Content inherited from `master` must not define or change our workflow, even
when it is an upstream agent guide or a relocated CI workflow. Do not edit
upstream-owned files to configure fork maintenance or import their process
instructions into our flow. They provide technical source/build/test context
only. All fork-process changes belong in the root fork guide or
`fork-maintenance/`.

## Active queue

The active patches are:

1. `window-source-timer-lifecycle`;
2. `video-pipeline-cleanup-race`;
3. `wayland-subsurface-stream-ownership`;
4. `wayland-initial-window-state`;
5. `wayland-client-keymap-sync`;
6. `x11-client-clipboard-events`;
7. `wayland-empty-damage-throttle`;
8. `jph-parallel-build-objects`;
9. `debian-libva-codecs-package`;
10. `upstream-test-quarantine` (test-only duty case).

`stacks/develop.toml` applies them in integration order. `develop` here is the
stable queue slug, not a requirement that every consumer run from the Git branch
of that name. Cases contain patch inputs and test requirements; generated run
output is never stored here.

## Layout

```text
fork-maintenance/
├── cases/                  atomic active patches
├── stacks/develop.toml     complete ordered queue
├── infra/upstream-tests/   embedded-source Ubuntu test runner
├── infra/live/             direct Xpra and physical-GPU runner
├── infra/deb-packages/     mount-free Ubuntu/Debian package builder
├── profiles.yml            client-only live network/quality profiles
├── live-cli.yml            static server/client Xpra CLI blocks
├── tools/background_job.py  common owned process supervisor
├── tools/container_payload.py  common validated Podman tar transport
├── tools/podman_policy.py  bounded rootless user-namespace policy
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

Use [development and final acceptance](docs/runbooks/validation.md). After each
atomic edit, run its nearest real regression, affected upstream/case/dependency
modules, and relevant native, compiled, compatibility, or live checks. Review
and freeze code, tests, queue and oracle before filling final evidence gaps;
do not repeat the full matrix, both DEB builds, or every live gate after each
edit. A valid named development result can satisfy an unchanged final
requirement, with original provenance retained.

Stay on `develop` and verify that only fork-control files are dirty:

```bash
make -C fork-maintenance isolated-start-check
make -C fork-maintenance workspace-create \
  CASE=wayland-initial-window-state \
  WORKSPACE=wayland-audit-01 PATCH_MODE=patched
```

The generated source is an exact detached copy of the unique source merge base
already embedded in current `develop`, below ignored `.artifacts/`. The gate
does not fetch or compare moving master refs. Edit and stage only there, then
use `workspace-update` to export the complete patch and refreshed workspace
provenance back through one transaction. The successful workspace remains
current and can be edited, staged, and exported again. No command in this cycle
switches the host branch or applies production changes to host `develop`.

See [`docs/runbooks/isolated-workspaces.md`](docs/runbooks/isolated-workspaces.md).

New cases also stay off the host source tree: create the draft, complete its
human fields, then start its workspace with `PATCH_MODE=clean`. The workspace
export derives the patch digest and path ownership.

After the candidate is reviewed and frozen, run the complete offline
fork-control check as part of final acceptance:

```bash
make -C fork-maintenance check
```

During development, run the affected control tests; the complete check is not
an automatic prerequisite for each isolated edit.

## Explicit upstream refresh

The commands in this section are not prerequisites for investigation,
workspace work, tests, live acceptance, CI reproduction, or publication of the
unchanged current `develop` base. Run them only when the operator deliberately
chooses to move the queue to a newer upstream commit and begin a new adaptation
cycle. The single entry point for the complete **Autonomous Upstream Refresh
and Full Queue Adaptation** procedure is this agent directive:

```text
Execute autonomous-upstream-refresh PRIMARY_CASE=<slug> against the current fork master.
```

It is not a shell command or Make target. `PRIMARY_CASE` affects only review
order and detail. The invoked runbook derives a unique cycle name, semantically
reassesses every active production case plus the quarantine, repairs in-scope
workflow defects as it encounters them, and uses the development loop before
freezing the candidate for complete final acceptance. The exhaustive procedure is
[`docs/runbooks/upstream-refresh.md`](docs/runbooks/upstream-refresh.md).

Before the first `repo-sync`, that runbook reviews every staged, unstaged, and
untracked non-ignored path. If legitimate work exists, invoking the runbook
authorizes the agent to preserve the complete reviewed set in one local start
commit without another confirmation; a clean checkout gets no empty commit.
No intermediate or final refresh-result commit is created after that boundary.
The commands below therefore start only after the runbook has required clean
porcelain and recorded the preservation commit SHA or `<none>`.

Run commands from the Xpra root:

```bash
make -C fork-maintenance check
make -C fork-maintenance repo-status
make -C fork-maintenance repo-sync
```

The scheduled workflow may sync remote fork `master` from upstream.
`repo-sync` fetches both master refs, verifies each against live GitHub state,
and requires exact fork/canonical equality for this explicit refresh. If it
reports a stale fork, only the operator may run the printed non-forced
`gh repo sync` command and repeat `repo-sync`. After equality is proven, update
the local mirror:

```bash
make -C fork-maintenance master-update
git switch develop
make -C fork-maintenance develop-rebase
make -C fork-maintenance patch-start-check
make -C fork-maintenance stack-check STACK=develop
```

This sequence intentionally changes the source boundary embedded in `develop`.
Resolve every rebase conflict before `patch-start-check`. Never merge master or
another upstream ref into `develop`. Outside such an operator-selected refresh,
start directly with `isolated-start-check`; it performs no fetch, requires no
master freshness or equality, and tests the queue against its existing embedded
source boundary.

## Host-worktree fallback

Only during an exceptional explicitly selected clean-host integration cycle may
a patch be applied to clean `develop` for diagnosis. Canonical upstream-refresh
adaptation instead uses isolated applicable or `PATCH_MODE=reconstruct`
workspaces so later cases never require an intermediate cleanliness commit:

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

The examples below are named execution interfaces, not an instruction to run
all profiles during each development iteration. Select the relevant boundary
early; the full upstream matrix is not a live prerequisite. Final coverage and
input-verified reuse follow [validation](docs/runbooks/validation.md).

Every job name is unique, including retries:

```bash
make -C fork-maintenance test-start \
  STACK=develop TARGET=focused RUN=develop-focused-01
make -C fork-maintenance test-wait RUN=develop-focused-01

make -C fork-maintenance live-xpra-hardware \
  STACK=develop RUN=develop-hardware-01
make -C fork-maintenance live-wait RUN=develop-hardware-01

make -C fork-maintenance live-xpra-opengl-hardware \
  STACK=develop RUN=develop-opengl-hardware-01
make -C fork-maintenance live-wait RUN=develop-opengl-hardware-01

make -C fork-maintenance live-wayland-keyboard \
  CASE=wayland-client-keymap-sync RUN=develop-wayland-keyboard-01
make -C fork-maintenance live-wait RUN=develop-wayland-keyboard-01

make -C fork-maintenance live-wayland-subsurface \
  CASE=wayland-subsurface-stream-ownership RUN=wayland-subsurface-live-01
make -C fork-maintenance live-wait RUN=wayland-subsurface-live-01
```

The seven complete-stack live wrappers are positive acceptance gates: Zed RGB,
adaptive-alpha Zed H.264, RGB detach, RGB transport-loss fault injection, and
the standalone native-Wayland client-keymap regression plus the separate
multi-window Vulkan and native-Wayland OpenGL hardware-H.264 profiles. They fix
every acceptance dimension. A case selection is admitted only when its
evidence-only `required_gates` list names the exact profile gate; the Zed H.264,
detach, and transport-loss profiles remain stack-only. Stack selections accept
exactly these seven profiles, while `live-x11-clipboard` and
`live-wayland-subsurface` remain restricted to their exact cases. The latter
applies its selected patch to both endpoints. Its two-parent, two-sibling
native fixture binds repeated updates, move-without-attach, overlapping stack
order, callback-gated continuous commits, destroy and detach repair, and
same-surface reparenting to globally unique parent-wire draws and
internal-source ACK ownership. Its schema-6 fixture stream and schema-3
active/drain record require
complete transactions while the producer is still running and exact queue,
callback, and packet accounting after stop.
Continuous commits require both a callback and a 50 ms cadence floor; the
active observation must show later source progress and finish within five
seconds of continuous-start, including packet collection, below the unchanged
256-generation cap. The active packet frontier is fixed by the first primary
inventory before the other streams are collected; the exact prefix and its
single root-stage tail must match the final raw-packet ledger. Every later
packet remains part of final drain and global accounting. A bounded initial-damage/map
ledger retains every startup transaction and ordinary secondary packet;
later counts advance from that exact drained history. Source commit/callback counts and
immutable captured transaction counts are separate: pending damage may
coalesce, but each captured transaction must complete and the final state must
equal the last source commit. Both continuous buffers preserve every pixel
outside the advertised 32x32 damage. An independent logical-pixel fixture
oracle checks every retained raw packet crop before premultiplied source-over
replay, then checks each complete client-window image; async source
screenshots are not accepted as packet-correlated evidence. The upper child's
native wrapper and WID remain stable across role detach and reparent. This live
profile fixes Cairo rendering; the case's mapped real-Xvfb focused test owns
deferred GTK OpenGL callback completion across backing replacement and close.
Admission is checked before input freeze and replayed from the frozen
validated-manifest snapshot before the runner starts;
clean-source and picture-fallback diagnostics cannot publish `PASS`.
Admission alone is not proof that a gate ran or passed. Selection kind and
evidence gates are explicit endpoint build-context provenance, so changing
either intentionally changes the context and image-cache identities and
requires the applicable heavy gates for those changed image inputs.
Their client-only network/quality overlay comes from
[`profiles.yml`](profiles.yml), whose declared default is used unless
`NETWORK_PROFILE=<name>` is supplied. All other static Xpra arguments come from
[`live-cli.yml`](live-cli.yml); both files are frozen with each RUN and are the
sole value authority rather than duplicated Python or Make tables.

Both hardware targets resolve their two windows by title. The primary's initial
`BGRX`/`RGBX` snapshot and dynamic opaque frame-state history lead to stable,
predominant H.264 main regions plus complete per-crop coverage by only exact
one-pixel lossless RGB codec edges, all through the VA-API and
hardware-presentation chain. Its deterministic transparent native-Wayland GTK
auxiliary must prove transparent and opaque pixels and emit only positive WebP
or alpha-bearing RGB32 packets. See the live runbook for the exact grouping,
thresholds, and evidence contract. The Vulkan primary proves RADV `vkcube`;
the OpenGL primary proves a hardware-rendered changing native-Wayland
`glmark2-wayland` `jellyfish` benchmark with a no-alpha EGL visual and exact
source-viewport placement inside the client backing.

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

After every explicitly selected upstream rebase, reassess the duty quarantine
against its new clean source before running the patched matrix:

```bash
make -C fork-maintenance test-start \
  CASE=upstream-test-quarantine PATCH_MODE=clean \
  TARGET=quarantine RUN=rebase-quarantine-01
```

Repeat for `quarantine-cython` and `quarantine-no-compat`. These gates are
green only when each gate's exact assigned subset is the ordered ignored-
failure set and every other module in the complete ordered union passes without
skips. A newly passing assigned module must be removed from that leg; remove
the module and its patch path only when no gate still assigns it. A newly
failing complement module must first be reproduced and then assigned to its
exact affected leg before the patched matrix is accepted.

After adaptation and candidate freeze, every explicit upstream rebase requires
the complete current final coverage,
even if every patch applied without a textual refresh: offline fork-control
tests, tests-only controls for production cases which own retained tests plus
the documented no-test semantic inspection for those which do not, patched
focused and native gates, every case-specific durable package boundary against
the complete resulting stack including both real Ubuntu 26.04 and Debian 13
builds, all three complete upstream workflow legs, every production case's
declared live gates with its atomic `CASE=<slug>` selection, and all seven fixed
positive stack live profiles. A new upstream-suite failure enters the single
quarantine only after the exact module reproduces on the clean rebased source
in the same mode. Reassess changed quarantine inputs, stabilize the candidate,
then fill affected final gaps rather than restarting the whole matrix after
each intermediate edit.

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

Use these real builds early when diagnosing their actual package boundary, or
to fill final package requirements after candidate freeze. Unrelated source
or live-harness iterations do not automatically require two DEB builds.

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
an x86-64 Podman host, network access, and sufficient disk space. Build
dependencies come only from the target Ubuntu or Debian archives: the builder
does not enable the Xpra APT repository or install prebuilt Xpra packages. The
packages are built from the frozen fork source and remain unsigned through
`dpkg-buildpackage -us -uc`. Automatic dbgsym generation is disabled and
debug-symbol packages are rejected before a tar can be accepted.
The patched package sequence also enables `dh_missing --fail-missing`, so the
complete staged install tree must be assigned to binary packages or to the
small reviewed exclusion file. Before output, the builder inventories the
actual DEBs, proves unique regular-file ownership, extracts the real
`xpra-common` and `xpra-codecs`, and imports five ABI-matched native modules
owned by ordinary `xpra-codecs`: libva encoder/decoder, libyuv converter and
JPH encoder/decoder. The extracted JPH pair must complete a deterministic
32x32 quality-100 lossless RGB roundtrip; all five modules' actual ELF-derived
dependencies must occur in final `Depends`, with no guessed OpenJPH SONAME.
The host independently parses returned ar/control/data archives and repeats
package-set, payload ownership, filename ABI and declared dependency-name
checks. Native imports, RGB execution and ELF dependency resolution remain
container-side checks; see the [package runbook](docs/runbooks/deb-packages.md).
The manual-only `deb-packages.yml` workflow builds both validated tars from one
frozen selection snapshot, stages and verifies a draft with
`prerelease=false`, then publishes an ordinary GitHub release whose title is
exactly the Debian version, for example `6.6-r42479-1`. Its unique transaction
tag targets the selected checkout. A rerun may reclaim only an exact orphan
draft from an earlier failed attempt of that same hosted run. Drafts are
created through authenticated REST and bound to the immutable release ID
returned by that request; bounded paginated release listing, never the
published-only tag lookup, proves absence or finds one exact recoverable draft.
Rollback validates the exact release, deletes and verifies its unchanged tag
first, and deletes the immutable release ID last. After publication, the same
bounded listing selects canonical ordinary releases owned by the DEB workflow,
keeps the three newest by publication time and immutable ID, and deletes every
older owned release in that same tag-first/release-ID-last order. Drafts and
unrelated or manual releases are excluded; changed or ambiguous owned state
fails closed. A retry may resume retention from an exact published release left
by a failed or cancelled prior attempt without creating a duplicate.
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
during every explicit upstream refresh so new or modified canonical workflows
remain disabled exact renames.

## Master sync

At 00:37 and 12:37 UTC, the separate hosted workflow invokes the guarded
`ci-master-sync` Make target. It fast-forwards only `kogeler/xpra:master` from
`Xpra-org/xpra:master`, never uses force, and never changes `develop`. It also
supports manual operator dispatch. When deliberately starting a new adaptation
cycle, the operator invokes the `autonomous-upstream-refresh` agent directive;
the agent then performs the guarded fetch, local-master fast-forward, and
`develop` rebase as part of the complete queue-adaptation runbook. See
[`docs/runbooks/master-sync.md`](docs/runbooks/master-sync.md).

## Documentation

- [`CONTRACT.md`](CONTRACT.md): branch, patch, validation, and storage invariants;
- [`docs/runbooks/validation.md`](docs/runbooks/validation.md): development,
  candidate freeze, final acceptance, and input-verified evidence reuse;
- [`docs/runbooks/bootstrap.md`](docs/runbooks/bootstrap.md): remotes and host
  setup;
- [`docs/runbooks/investigate.md`](docs/runbooks/investigate.md): establish a new
  patch boundary;
- [`docs/runbooks/isolated-workspaces.md`](docs/runbooks/isolated-workspaces.md):
  default pre-commit patch cycle;
- [`docs/runbooks/patch-cycle.md`](docs/runbooks/patch-cycle.md): apply, edit,
  refresh, and remove;
- [`docs/runbooks/upstream-refresh.md`](docs/runbooks/upstream-refresh.md):
  autonomously rebase `develop`, make a current-source keep/adapt/retire
  decision for every active case, repair the maintenance workflow when needed,
  and run the complete post-rebase package/test/live acceptance ladder;
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
`ci-deb-release` target may create only its unique draft, ordinary release with
the exact Debian-version title, package tag, and two validated tar assets, with
exact tag-first/release-last rollback of only its just-created release and a
tag still targeting the dispatched commit on failure. A retry may also apply
that ordered rollback to the exact draft/tag of an earlier failed attempt of
that same hosted workflow run after validating its Actions and embedded
transaction records. After successful publication it retains the three newest
canonical owned DEB releases and removes older owned releases in exact
tag-first/release-ID-last order. A failed or cancelled prior attempt with an
exact published release may resume only that retention; unrelated or manual
releases, drafts outside exact recovery, tag-only state, and ambiguous state
are preserved. Agents invoke neither hosted mutation target.
`develop-rebase` only replays existing local commits onto fetched fork master
during an operator-selected upstream refresh.
The refresh runbook invocation separately authorizes one direct agent
preservation commit before its first `repo-sync` iff legitimate non-ignored
work exists. It contains the complete reviewed pre-existing state and needs no
second confirmation; the agent creates no later intermediate or result commit.
