# Xpra Fork Maintenance

This directory is the tracked control plane for `kogeler/xpra`: case
documentation, automation and runbooks. It lives inside the Xpra repository:
the parent directory is the source tree, `master` is an operator-maintained
upstream reference, and `develop` is that upstream base plus control commits
(this directory, the root `AGENTS.md`, `.gitignore` and the CI boundary) and
exactly one case commit per fork case. The model is specified in
[`docs/runbooks/case-commits.md`](docs/runbooks/case-commits.md).

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

## Active cases

The active case commits, in stack order, are:

1. `window-source-timer-lifecycle`;
2. `video-pipeline-cleanup-race`;
3. `wayland-subsurface-stream-ownership`;
4. `wayland-initial-window-state`;
5. `wayland-client-keymap-sync`;
6. `x11-client-clipboard-events`;
7. `packet-handler-error-boundary`;
8. `gtk-client-scroll-deduplication`;
9. `server-shutdown-disconnect-flush`.

No quarantine duty case is currently active. The permanent
[`upstream-test-quarantine` scaffold](cases/upstream-test-quarantine/README.md)
retains its manifest and description without a commit: empty module, gate and
test lists, not selectable, ready for future clean-source-proven upstream
failures. Never delete this infrastructure when all tests pass or restore
obsolete skips merely to activate it.

Each case is exactly one commit with a `Fork-Case: <slug>` trailer, and the
order of those commits on `develop` is the stack order. `stacks/develop.toml`
keeps only the stack-level test list; `develop` in `STACK=develop` is the
stable stack slug, not a requirement that every consumer run from the Git
branch of that name. Case directories contain documentation and test
requirements; case code lives only in the case commits, and generated run
output is never stored here. Resolve a case's commit with
`make -C fork-maintenance case-show CASE=<slug>`; no SHA is stored.

The upstream-owned native pointer conversion retains its real protocol tests
under the runner's [neutral regression ownership](infra/upstream-tests/neutral/README.md),
independent of a production case. Clean and complete-stack native checks remain
required; retirement does not remove the shared live scroll oracles.

## Layout

```text
fork-maintenance/
├── cases/                  one documentation directory per case commit
├── stacks/develop.toml     stack-level test list
├── infra/upstream-tests/   embedded-source Ubuntu test runner
├── infra/live/             direct Xpra and physical-GPU runner
├── infra/deb-packages/     mount-free Ubuntu/Debian package builder
├── profiles.yml            client-only live network/quality profiles
├── live-cli.yml            static server/client Xpra CLI blocks
├── tools/background_job.py  common owned process supervisor
├── tools/container_payload.py  common validated Podman tar transport
├── tools/podman_policy.py  bounded rootless user-namespace policy
├── tools/contrib.py        sync, branch, case-commit, and manifest gates
├── docs/runbooks/          operator workflows
├── AGENTS.md               scoped agent rules
├── CONTRACT.md             invariants
└── Makefile                supported interface
```

All durable runtime, build, result, publication, and cache outputs—logs,
reports, screenshots, snapshots, status records, virtual environments, and
caches—live under ignored `.artifacts/fork-maintenance/` at the repository
root. Only transient interpreter caches may use another explicitly ignored
local path. Podman runtime objects are owned separately by immutable IDs and
labels. That tree is per-session state: a finished session is closed to the
distilled `knowledge/` base and its generated session registry
`knowledge/INDEX.md`, which every new session reads first (see
[the session runbook](docs/runbooks/session-close.md)).

GitHub CI is intentionally separate from upstream's active workflow set. The
canonical workflows are byte-identical disabled renames below
`.github/upstream-workflows/`; the only executable files are the thin
`.github/workflows/develop.yml` test caller,
`.github/workflows/master-sync.yml` fork-master sync caller, and
`.github/workflows/deb-packages.yml` manual package-release caller.

## Case workflow

Use [development and final acceptance](docs/runbooks/validation.md). After each
atomic edit, run its nearest real regression, affected upstream/case/dependency
modules, and relevant native, compiled, compatibility, or live checks. Review
and freeze code, tests, stack and oracle before filling final evidence gaps;
do not repeat the full upstream matrix or both DEB builds after each edit.
Every live validation uses the entire nine-profile suite. A valid named
development result can satisfy an unchanged final requirement, with original
provenance retained.

Work in the single `develop` checkout, which is the patched product; there is
no other branch and no Git worktree. Verify that product paths are clean (only
control paths may be dirty) and locate the case:

```bash
make -C fork-maintenance isolated-start-check
make -C fork-maintenance case-show CASE=wayland-initial-window-state
```

The gate locates the upstream base already embedded in current `develop`; it
does not fetch or compare moving master refs. Edit the case's product files in
place and fold the change into its commit:

```bash
git add -- <paths>
git -c commit.gpgsign=false commit \
  --fixup="$(make -s -C fork-maintenance case-commit CASE=wayland-initial-window-state)"
make -C fork-maintenance develop-squash
make -C fork-maintenance case-check CASE=wayland-initial-window-state
```

A new case starts with `case-new CASE=<slug>`, which creates only its
directory with a schema-2 manifest template and README skeleton; its code and
regression tests become one commit with the `Fork-Case: <slug>` trailer.
Retire a case with `case-drop CASE=<slug>` and remove its directory in the same
change. Runners test committed `HEAD`, so fold every edit into its case commit
before starting one; uncommitted control work survives every rewrite through
`--autostash`. Every rewrite changes `develop` history, which the operator
publishes with `--force-with-lease`. See
[`docs/runbooks/case-commits.md`](docs/runbooks/case-commits.md#everyday-operations).

After the candidate is reviewed and frozen, run the complete offline
fork-control check as part of final acceptance:

```bash
make -C fork-maintenance check
```

Once the tree is clean, `make -C fork-maintenance develop-check` also proves
the committed model: valid control and case commits only, one-to-one case
directories and commits, and every `case-check`, `stack-check` and
`ci-layout-check`. During development, run the affected control tests; the
complete check is not an automatic prerequisite for each edit.

## Explicit upstream refresh

The commands in this section are not prerequisites for investigation,
case work, tests, live acceptance, CI reproduction, or publication of the
unchanged current `develop` base. Run them only when the operator deliberately
chooses to move the case commits to a newer upstream commit and begin a new
adaptation cycle. The single entry point for the complete **Autonomous Upstream Refresh
and Full Queue Adaptation** procedure is this agent directive:

```text
Execute autonomous-upstream-refresh against the current fork master.
```

It is not a shell command or Make target. Every case receives equally deep
manual correctness and necessity review. The older optional
`PRIMARY_CASE=<slug>` spelling affects only an explicitly requested starting
order, never depth or scope. After recording how every case commit replayed
on the new source, the runbook requires a complete manual review, reasoned
keep/adapt/retire decisions, implementation in the case commits and re-review
of all initial changes and regression migrations before any runtime tests,
quarantine, live profiles or real builds.
Tests challenge those conclusions, not replace analysis of uncovered paths.
The runbook derives a unique cycle name, repairs in-scope workflow defects,
then uses the post-review development loop before freezing the candidate for
complete final acceptance. The exhaustive procedure is
[`docs/runbooks/upstream-refresh.md`](docs/runbooks/upstream-refresh.md).

The runbook requires a clean local `develop` (`develop-rebase` refuses any
uncommitted change) and existing local `master`. It preserves other dirty work
pending operator disposition and creates no
preservation commit. Its autonomous Git mutations are the local rebase and the
case-commit rewrites of the adaptation; fetching, preparing master, switching
branches, changing remotes and publishing require separate explicit operator
instructions. Remote URLs are not local gates.

Run commands from the Xpra root:

```bash
make -C fork-maintenance check
make -C fork-maintenance repo-status
```

The operator prepares local `master` independently. Refresh records the case
map and old tip, then rebases onto that local commit in the checkout without
fetching or checking remote equality:

```bash
make -C fork-maintenance case-list          # record the case map and old tip
make -C fork-maintenance develop-rebase
make -C fork-maintenance patch-start-check
make -C fork-maintenance stack-check STACK=develop
```

This sequence intentionally changes the source boundary embedded in `develop`.
Resolve every rebase conflict inside the case commit being replayed and
continue with `git -c commit.gpgsign=false rebase --continue` before
`patch-start-check`. Mid-rebase the control files may be old or absent: read
runbooks with `git show ORIG_HEAD:<path>` and run tooling only after the rebase
ends. Git drops a case commit whose diff is already upstream byte for byte;
retire the other fully upstream cases with `case-drop` and rework the rest with
fixups and `develop-squash`. Review each case with
`git range-diff <old-sha>^! <new-sha>^!` against the recorded map, then run
`develop-check`. Roll back with `git reset --keep <recorded tip>` or the
reflog. Never merge master or another upstream ref into `develop`. Outside
such an operator-selected refresh, start directly with `isolated-start-check`;
it performs no fetch, requires no master freshness or equality, and locates the
existing embedded source boundary under the case commits.

## Durable tests

The examples below are named execution interfaces. Select the relevant focused
unit boundary early; the full upstream matrix is not a live prerequisite.
Every live validation runs the complete nine-profile suite with all case
commits on both endpoints. Final coverage and input-verified reuse follow
[validation](docs/runbooks/validation.md). Each runner freezes the diff of
every selected case commit in committed `HEAD` into its private payload (as
that case's generated `fix.patch` there only), so product paths must be clean
at start; see [runners](docs/runbooks/case-commits.md#runners).

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
  STACK=develop RUN=develop-wayland-keyboard-01
make -C fork-maintenance live-wait RUN=develop-wayland-keyboard-01

make -C fork-maintenance live-wayland-subsurface \
  STACK=develop RUN=wayland-subsurface-live-01
make -C fork-maintenance live-wait RUN=wayland-subsurface-live-01
```

Every live test runs the complete `stacks/develop` on BOTH endpoints.
Case-only, partial-stack and clean-endpoint live tests are forbidden.
For every case validation, finish with one complete pass:

```bash
make -C fork-maintenance live-all STACK=develop RUN=<fresh-prefix>
make -C fork-maintenance live-suite-check STACK=develop RUN=<fresh-prefix>
```

Reach it through the [live loop](docs/runbooks/live-tests.md#the-live-loop-fix-and-continue-then-one-complete-pass):
when a gate fails, fix it and continue from that gate with
`live-all ... RUN=<next-prefix> FROM=<gate>` to the last gate instead of
rerunning the whole set; then a complete pass, repeated the same way until one
succeeds without a fix.

The nine required profiles are Zed RGB, adaptive-alpha Zed H.264, RGB detach,
RGB transport loss, native-Wayland keymap, Vulkan hardware, OpenGL hardware,
X11 clipboard and Wayland subsurface composition. The preceding per-profile
commands are member lifecycle examples, not standalone acceptance of a case.
Every client build also runs the installed clipboard adapter and mapped GL
regressions. The subsurface profile retains its exact independent oracle.

Its two-parent, two-sibling
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

After every explicitly selected upstream rebase, first pass the runbook's
whole-queue manual-review exit gate. If a duty case is active, reassess it
against its new clean source before using the duty commit in runtime
validation:

```bash
make -C fork-maintenance test-start \
  CASE=upstream-test-quarantine PATCH_MODE=clean \
  TARGET=quarantine RUN=rebase-quarantine-01
```

Repeat for `quarantine-cython` and `quarantine-no-compat`. These gates are
green only when each gate's exact assigned subset is the ordered ignored-
failure set and every other module in the complete ordered union passes without
skips. A newly passing assigned module must be removed from that leg; remove
the module and its test-file change from the quarantine commit only when no
gate still assigns it. A newly failing complement module must first be
reproduced and then assigned to its exact affected leg before the patched
matrix is accepted.

When no duty is active, record the reassessment as not applicable and omit
these three case commands. If the last assignment is obsolete, deactivate the
duty with `case-drop`, empty its lists again and preserve the directory
through the [quarantine runbook](docs/runbooks/test-quarantine.md); do not
delete the directory. All production and final full-suite gates remain.

After adaptation and candidate freeze, every explicit upstream rebase requires
the complete current final coverage, even if every case commit replayed
without a textual change: offline fork-control tests, tests-only controls for
production cases which own retained tests plus the documented no-test
semantic inspection for those which do not, patched focused and native gates,
every case-specific durable package boundary against the complete resulting
stack including both real Ubuntu 26.04 and Debian 13 builds, all three
complete upstream workflow legs, and all nine fixed positive complete-stack
live profiles. A new upstream-suite failure enters the single
quarantine only after the exact module reproduces on the clean rebased source
in the same mode. Reassess changed quarantine inputs, stabilize the candidate,
then fill affected final gaps rather than restarting the whole matrix after
each intermediate edit.

After a whole prefixed work cycle is finalized and reviewed, delete its
collected results through an exact two-phase plan:

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

For all old output and unmanaged scratch during a session, use the permanent
storage policy instead of maintaining a list of cycle names:

```bash
make -C fork-maintenance artifacts-clean-plan
make -C fork-maintenance artifacts-clean CONFIRM=<artifacts_clean_confirm>
make -C fork-maintenance artifacts-check
```

[`artifacts.toml`](artifacts.toml) classifies `knowledge/` as permanent,
lifecycle/recovery authorities as infrastructure, and session work plus every
filesystem cache as task state. Mid-session cleanup keeps all three;
runtime-bound results are protected; other safe output is disposable
regardless of age or report schema. No Podman objects are removed. A repeat on
unchanged state reports zero disposable targets. Review the
[artifact runbook](docs/runbooks/artifacts.md#deterministic-whole-directory-housekeeping)
before discarding evidence: a deleted named result cannot be reused later.

Every finished session, before its handoff and before any control commit that
records it, writes its distilled record and closes (case commits made during
the session stay on `develop`):

```bash
make -C fork-maintenance knowledge-new SESSION=<session>   # then fill it in
make -C fork-maintenance knowledge-index
make -C fork-maintenance artifacts-close-plan
make -C fork-maintenance artifacts-close CONFIRM=<artifacts_close_confirm>
make -C fork-maintenance artifacts-close-check
```

The close refuses while runtime, recovery state, an invalid record or foreign
`.artifacts/` entry remains, then leaves only `knowledge/` and idle lock files.

## DEB packages

Use these real builds early when diagnosing their actual package boundary, or
to fill final package requirements after candidate freeze. Unrelated source
or live-harness iterations do not automatically require two DEB builds.

Package builds are branch-agnostic. They locate the clean source boundary
between `HEAD` and refs whose final component is `master`, reject downstream
merge commits, product changes outside valid case commits and dirty product
paths, apply the frozen case commit diffs of `HEAD` (the complete
`stacks/develop` series), and exchange source and package tars with Podman
through stdin/stdout without bind mounts. The name `develop` there is a stack
slug, not a current-branch requirement. Each build binds the retained
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
applies the case commit diffs of `stacks/develop` to the embedded base before
its one leg. The workflow contains no build or test implementation and never
starts live/GPU profiles. Run `ci-layout-check`
during every explicit upstream refresh so new or modified canonical workflows
remain disabled exact renames.

## Master sync

At 00:37 and 12:37 UTC, the separate hosted workflow invokes the guarded
`ci-master-sync` Make target. It fast-forwards only `kogeler/xpra:master` from
`Xpra-org/xpra:master`, never uses force, and never changes `develop`. It also
supports manual operator dispatch. When deliberately starting a new adaptation
cycle, the operator prepares local `master` and invokes the
`autonomous-upstream-refresh` agent directive; the agent then rebases
`develop` onto that local `master` in the checkout as part of the complete
queue-adaptation runbook, without fetching or updating `master`. See
[`docs/runbooks/master-sync.md`](docs/runbooks/master-sync.md).

## Documentation

- [`docs/runbooks/case-documentation.md`](docs/runbooks/case-documentation.md):
  mandatory case README structure, analytical depth and semantic review checklist.
- [`CONTRACT.md`](CONTRACT.md): branch, case-commit, validation, and storage
  invariants;
- [`docs/runbooks/case-commits.md`](docs/runbooks/case-commits.md): one commit
  per case on `develop`: commit classes and trailer, case identity, change,
  add, retire, checks, runners, refresh, publication and commit authority;
- [`docs/runbooks/validation.md`](docs/runbooks/validation.md): development,
  candidate freeze, final acceptance, and input-verified evidence reuse;
- [`docs/runbooks/bootstrap.md`](docs/runbooks/bootstrap.md): remotes and host
  setup;
- [`docs/runbooks/investigate.md`](docs/runbooks/investigate.md): establish a new
  case boundary;
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
  handoff and `--force-with-lease` publication of `develop`;
- [`docs/runbooks/artifacts.md`](docs/runbooks/artifacts.md): local output and
  cleanup;
- [`docs/runbooks/session-close.md`](docs/runbooks/session-close.md): session
  registry, distilled session records and the mandatory session close;
- [`docs/runbooks/cycle-cleanup.md`](docs/runbooks/cycle-cleanup.md): finalize and
  remove one exact work cycle.

No target creates a new commit, pushes `develop`, creates a pull request, or
changes the fork's default branch. The rewriting targets `develop-squash`,
`case-drop` and `develop-rebase` only fold, drop or replay existing local
commits of `develop` in the checkout, unsigned. The hosted-only
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
are preserved. Hosted workflow dispatch requires an explicit operator request.
Case commits are the storage of case work: the agent creates, fixes up,
squashes, drops and rebases them itself, always unsigned (see
[commit authority](docs/runbooks/case-commits.md#commit-authority)). Control
commits follow the operator's instructions; uncommitted control work is never
automatically committed and survives every rewrite through `--autostash`. All
other Git operations are performed by the operator or explicitly delegated to
the agent. The agent never pushes: after any case rewrite the operator
publishes `develop` with `--force-with-lease`
([`docs/runbooks/publish-develop.md`](docs/runbooks/publish-develop.md)).
