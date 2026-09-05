# Run Upstream-Compatible Tests

Test scheduling is owned by [`validation.md`](validation.md). Development uses
nearest regressions, affected existing upstream and downstream case modules,
and relevant composed/native/compiled/compatibility boundaries. A full suite
is not a prerequisite for a relevant early live check. The full matrix below
is final coverage on a reviewed frozen candidate; fill only missing or
invalidated results.

## Frozen source model

Every local acceptance job archives the unique source merge base already
embedded in current `develop`, resolves the selected case or stack, and applies
only forward-applicable patches inside an isolated container source tree.
Hosted develop test CI uses the same model and derives that boundary from the
checkout's cached `origin/master`. Neither local nor hosted jobs fetch, compare
moving master refs, require master freshness/equality, switch branches, merge,
or rebase. Neither path packages `develop`, `.git`, ignored files, credentials,
or the host working-tree diff.

Cached `origin/master` may later advance to a descendant without changing the
source selected for a named job. The credential-free bundle records that cached
tip so it contains and authenticates the history, while the job's separate
`source` identity remains the unique merge base embedded in `develop`. Named
background jobs do not require those two commits to be equal.

The source bundle, minimal selection snapshot, and every Podman image build
context cross stdin through the common validated tar helper. The test source
and queue are never bind-mounted, and the runner never uses `podman cp` or an
artifact bind to retrieve results. The one named ccache volume is cache-only.
Test containers return only their normal log; the entrypoint prints the exact
selection-resolution digest into that log for collection and validation.
Detached and hosted test containers use
`--userns=keep-id:uid=1000,gid=1000,size=2048` with runtime UID/GID 1000. The
explicit bound is verified on the Ubuntu 26.04 test image and leaves the rest
of the rootless subordinate-ID range available to other bounded namespaces.
The common Podman policy rejects `keep-id`, `nomap`, or `auto` without a
positive `size` and always rejects `--userns=host` before container creation.
Each exact source bundle has a retained mode-`0600` `.bundle.lock`. Publication
holds its kernel lock across the bundle child, validates the bundle before an
atomic no-replace rename, and may recover only its deterministic
`.bundle.partial` on the next snapshot attempt. Cycle cleanup retains the lock
and refuses any remaining partial.

Before a detached test calls `podman create`, it publishes an inspectable
`runs/<RUN>.prelaunch.json` binding the starter PID/start ticks, run UUID,
expected full labels, immutable image ID, and payload path. The final
`<RUN>.owner` is published only after the returned container ID and labels have
been verified. The already frozen source bundle and a selection snapshot below
`<RUN>.payload/` are then streamed to that exact container. The prelaunch stays
published through container start, tar delivery, and the ready-byte handshake;
the retained lifecycle lock descriptor is inherited by selection freezing,
`podman create`, `podman start`, and the payload-streaming child. The image-cache
lock is held by the Python starter across the same immutable-ID use handoff but
is not inherited by Podman's long-lived networking helper. A crash therefore
leaves either the normal owner or an exact prelaunch owner that `test-status` can
inspect and `test-abort` can recover after the recorded starter is no longer
active.

For detached jobs, the entry process waits on the image's pre-created validated
payload-ready FIFO. The extraction helper writes one ready byte only after the
streamed source and selection snapshot are complete. The sender retries a
non-blocking FIFO open for a bounded interval while the reader attaches;
execution then replaces the waiter directly. Do not replace this handshake with
process signals.

Verify the input-keyed, label-verified Ubuntu 26.04 image first:

```bash
make -C fork-maintenance test-image
```

If it is absent, follow the durable image-build sequence in
[`bootstrap.md`](bootstrap.md). Do not let an ordinary test target silently
pull or rebuild its environment. Test jobs inspect that cache entry and create
their container from the returned immutable image ID, never from the mutable tag
alone. The preflight and test startup use the same exact verifier, including
the current source label, build-run UUID, and rejection of extra maintenance
labels. Retained `image-builds/.image-cache.lock` serializes cache creation,
immutable-ID inspection/use handoff, and explicit removal. Podman image-build
children inherit the open lock. A detached container's Python starter instead
holds the lock itself through validation, immutable-ID handoff, and payload
delivery, so a mutable tag cannot change between validation and use and no
long-lived networking helper retains the lease.

`test-image-cache-remove` takes that same lock and refuses deletion while any
matching image-build or test prelaunch/owner still leases the image. Cleanup may
accept a source label only after proving that it is an existing Git commit and
an ancestor of, or equal to, the current embedded source; unknown, unrelated,
and future commits are rejected. Removal still does not make new acceptance
evidence, and the complete owner-label set plus image/input/workflow identity
must match exactly. After an upstream rebase, when `test-image`
rejects an otherwise exact cache solely for that old source label, use this
target, confirm that `test-image` now reports the image absent, and rebuild it
through the named lifecycle. Any broader label mismatch remains a hard stop.

A named standalone build publishes
`image-builds/.<IMAGE_RUN>.image-prelaunch.json` before creating and populating
`image-builds/<IMAGE_RUN>/`; its final background owner uses schema 3 and is
released only after durable publication. `test-image-status` and
`test-image-abort` understand the prelaunch boundary. The shared lifecycle lock
prevents abort racing an active start, while a marker left after the starter is
gone authorizes removal of only that exact context. Normal image remove/abort
deletes the marker. Hosted foreground image creation instead streams build
inputs directly and creates no `.ci-image.*` host context.

Hosted foreground tests freeze their selection into deterministic
`.foreground-payload` staging after publishing
`.foreground-payload.owner.json`, all under retained
`.foreground-payload.lock`. The same foreground operation validates and
recovers only that exact marker-backed partial before reuse. A remaining marker
or staging tree blocks cycle cleanup.

## Resolve before spending test time

```bash
make -C fork-maintenance patch-check CASE=wayland-initial-window-state
make -C fork-maintenance stack-check STACK=develop
```

Resolution must report only `apply` or exact `already-present`. A divergent or
ambiguous patch stops the ladder.

## Unchanged-base non-semantic refreshes

Before `test-start`, compare the exact old and new applied trees. Do not start
container tests when the embedded source is unchanged and the only differences
are comments, copyright notices, or documentation, with identical paths,
modes, executable data, configuration, test assertions, source
selection/application, build commands, and runner behavior. Refresh derived
digests, resolve the selection, run whitespace and fork-control checks, and
report the proof instead. Any uncertainty or semantic difference requires the
affected development checks and final coverage. This exception never spans an
upstream rebase. After adaptation and candidate freeze following
`develop-rebase`, complete the clean quarantine reassessment, tests-only controls
for cases which own retained tests, case-specific no-test semantic inspection
for those which do not, patched focused/native gates, every case-specific
durable package boundary against the complete resulting stack, and every full
leg even when the patch files did not change. The canonical complete sequence
is
[`upstream-refresh.md`](upstream-refresh.md).

## Focused tests

Choose the mode which exercises the changed boundary; all three use the same
ordered manifest `unit.*` inventory and native-module requirements:

| `TARGET` | `CYTHONIZE_MORE` | `XPRA_BACKWARDS_COMPATIBLE` |
| --- | --- | --- |
| `focused` | `without` | `1` |
| `focused-cython` | `with` | `1` |
| `focused-no-compat` | `without` | `0` |

These values are pinned for both `setup.py unittests` and the post-build native
import checks. The runner checks the installed `xpra.net.common` path, whether
it is a compiled extension in the compiled mode, and its actual compatibility
value. That sentinel is a technical build contract, not an optional check to
skip after an upstream package-layout change; reassess the build and installed
module mapping if it stops holding. Logs retain `focused_mode`,
`focused_cythonize_more`,
`focused_backwards_compatible`, the `focused_applied_tree` Git tree identity,
and each ordered `focused_unit_test`. They supplement the ordinary frozen
selection, source and image provenance; they do not replace it.

Every mode still creates fresh source and performs the Xpra install/build
required by `setup.py unittests`. Reusing the dependency image and ccache does
not make these build-free or incremental installed-Xpra tests. The focused
modes select narrow modules, not the complete final upstream matrix.

First run the retained regression against unmodified embedded-source production
code by applying only the selected test paths in the relevant mode:

```bash
make -C fork-maintenance test-start \
  CASE=wayland-initial-window-state PATCH_MODE=tests-only \
  TARGET=focused RUN=wayland-master-regression-01
make -C fork-maintenance test-wait RUN=wayland-master-regression-01
```

This is the non-vacuous control for deciding whether upstream replaced a case.
`PATCH_MODE=clean` applies nothing and therefore cannot run a newly introduced
focused module; `PATCH_MODE=patched` applies the complete selected patch.
When a production patch owns no test path, `PATCH_MODE=tests-only` fails closed
instead of pretending to provide a regression. The focused runner also rejects
`PATCH_MODE=clean`; do not classify either guard failure as a clean test. Perform
the semantic inspection required by that case's README, run its existing
focused module on the patched or resulting stack, and prove its durable real
boundary against the complete resulting stack before deciding that upstream
replaced it.

Run one atomic case while developing it:

```bash
make -C fork-maintenance test-start \
  CASE=wayland-initial-window-state TARGET=focused \
  RUN=wayland-focused-01
make -C fork-maintenance test-wait RUN=wayland-focused-01
```

For compiled Python or compatibility-policy changes, respectively:

```bash
make -C fork-maintenance test-start \
  CASE=wayland-initial-window-state TARGET=focused-cython \
  RUN=wayland-focused-cython-01
make -C fork-maintenance test-wait RUN=wayland-focused-cython-01

make -C fork-maintenance test-start \
  CASE=x11-client-clipboard-events TARGET=focused-no-compat \
  RUN=clipboard-focused-no-compat-01
make -C fork-maintenance test-wait RUN=clipboard-focused-no-compat-01
```

Use the same typed target with `PATCH_MODE=tests-only` when the clean control
depends on that execution mode. Each run has its own lifecycle; inspect and
remove it as described below. The root aliases `test-focused`,
`test-focused-cython` and `test-focused-no-compat` forward to named `test-start`
and still require `RUN`. Only the lower-level `infra/upstream-tests/Makefile`
focused targets run in the foreground for diagnostics; those do not replace
named local acceptance runs.

Use composed focused checks during development when interfaces overlap, and
ensure a current complete-queue result before final handoff:

```bash
make -C fork-maintenance test-start \
  STACK=develop TARGET=focused RUN=develop-focused-01
make -C fork-maintenance test-wait RUN=develop-focused-01
```

Focused execution derives unit modules from the selected manifests. Each
`unit.*` entry must resolve to an existing executable module whose filename
ends in `test.py`; an invalid name or missing subject fails before the build,
not as a skipped test.
When the selection declares the `wayland` gate, focused setup enables the
native Wayland server extension for any owning case; this is gate-driven and
must not depend on the `wayland-initial-window-state` slug.

## Native boundaries

Use only gates declared by the case or stack:

```bash
make -C fork-maintenance test-start \
  STACK=develop TARGET=wayland RUN=develop-wayland-01
make -C fork-maintenance test-wait RUN=develop-wayland-01
```

The native gate must build and import/link the actual subject module in a fresh
process. Do not replace it with a copied test or a mock-only probe.

## Full Ubuntu matrix

After candidate freeze, use a new `RUN` for every missing or invalidated leg:

```bash
make -C fork-maintenance test-start \
  STACK=develop TARGET=full RUN=develop-full-01
make -C fork-maintenance test-start \
  STACK=develop TARGET=full-cython RUN=develop-full-cython-01
make -C fork-maintenance test-start \
  STACK=develop TARGET=full-no-compat RUN=develop-full-no-compat-01

make -C fork-maintenance test-wait RUN=develop-full-01
make -C fork-maintenance test-wait RUN=develop-full-cython-01
make -C fork-maintenance test-wait RUN=develop-full-no-compat-01
```

The fork's validation contract requires all three final legs. Upstream
workflow dependencies and commands are technical build/test inputs, not
authority over this scheduling. The container does not claim to be the
GitHub-hosted runner image.

After an explicit upstream rebase these three legs are mandatory as the full
repository-author test suite for the rebased source. A failure is not added to
the quarantine from the patched run alone: first reproduce its exact module on
the clean embedded source in the same leg, update only the single duty
quarantine when that control proves it is upstream-owned. Reassess the changed
quarantine inputs and stabilize the candidate before filling the affected final
matrix gaps; a quarantine edit does not automatically launch three full suites.

The fork's hosted `develop` workflow fans the same three patched legs out to
three independent matrix runners. Every runner uses the same thin entry point:

```bash
XPRA_CI_TARGET=full make -C fork-maintenance ci-upstream-tests
```

The other matrix values are `full-cython` and `full-no-compat`. That CI-specific
target validates its value and keeps its one container in the foreground because
GitHub Actions owns the outer timeout, cancellation, and log. Do not use it in
place of named local acceptance runs, and do not duplicate any runner command
in the workflow YAML. See [`ci.md`](ci.md).

## Reassess quarantined upstream modules

Before applying `upstream-test-quarantine` after an explicitly selected upstream
rebase, run its exact module set on the new clean source in all three modes:

```bash
make -C fork-maintenance test-start \
  CASE=upstream-test-quarantine PATCH_MODE=clean \
  TARGET=quarantine RUN=rebase-quarantine-01
make -C fork-maintenance test-start \
  CASE=upstream-test-quarantine PATCH_MODE=clean \
  TARGET=quarantine-cython RUN=rebase-quarantine-cython-01
make -C fork-maintenance test-start \
  CASE=upstream-test-quarantine PATCH_MODE=clean \
  TARGET=quarantine-no-compat RUN=rebase-quarantine-no-compat-01
```

These targets intentionally use mixed expectations. Every gate runs the full
ordered `[quarantine].modules` union, passes `--skip-fail` only for its exact
`[quarantine.gates].<gate>` subset, and returns success only when that subset is
the ordered ignored-failure set while every complement module passes. Any
unignored failure, skipped module, or count mismatch fails closed. Follow
[`test-quarantine.md`](test-quarantine.md) to remove stale assignments or admit
a newly affected leg before the patched full matrix.

Reuse current reassessment results while the actual source, image/environment,
module union, and gate expectations remain unchanged. Independent CASE
development need not wait for unrelated quarantine work; an unrelated
production-only edit does not invalidate its clean-source evidence.

## Failure triage

Stop escalation at the first unexplained failure. Inspect the affected module
and surrounding source; matching upstream Actions output for the exact base
and leg is technical diagnostic context, not a scheduling authority or a
substitute for the current clean reproduction required for quarantine.
Keep an unrelated failure out of the current production patch. Admission to
the single duty quarantine requires existing task authority (including the
autonomous refresh directive) and a non-vacuous same-mode clean control; ask
for scope only when that authority is absent.

Use the narrowest supported control which observes the failure. The focused
family rejects `PATCH_MODE=clean`, and the quarantine targets select only their
declared module union; there is no generic named clean-module selector. If the
needed narrow control is unavailable, record that tooling boundary and either
implement/test it under authorized workflow scope or justify a broader clean
control. Do not invent a command or edit shared runner inputs during active
jobs. An expensive clean full leg is appropriate only when a narrower control
cannot reproduce the disputed boundary; record that reason before launching it:

```bash
make -C fork-maintenance test-start \
  STACK=develop PATCH_MODE=clean TARGET=full RUN=clean-full-01
```

Clean and patched comparisons must share source, workflow, runner inputs,
image, and target.

## Job lifecycle

For each `RUN`:

```bash
make -C fork-maintenance test-status RUN=name
make -C fork-maintenance test-logs RUN=name
make -C fork-maintenance test-wait RUN=name
make -C fork-maintenance test-collect RUN=name
make -C fork-maintenance test-remove RUN=name
make -C fork-maintenance test-abort RUN=running-or-lost-name
make -C fork-maintenance test-image-abort IMAGE_RUN=running-or-lost-image-name
```

`wait` supervises completion and collects a no-clobber local result. Use
`collect` for an already-finished container or owned process. Review before
`remove`; removal targets only the exact owned container/process/context and
retains the ignored result files. Before its first destructive step, test or
image removal publishes `logs/<name>.remove.json`, binding the old owner plus
log/status digests. An interrupted remove is retried through the same target;
the transaction remains beside the log/status until cycle cleanup. Never reuse
a run name or copy its result into Git.

One retained `upstream-tests/logs/.lifecycle.lock` serializes terminal
collect/abort transitions for the subsystem; it is a crash-releasing kernel
lock, not a per-`RUN` artifact. An abort target accepts a running or lost job
with no collected output. `lost` means no valid completion and no remaining
exact owned runtime; for a process job, a dead leader with a live owned process
group still counts as running. In that orphaned-group path, each live member
must expose exactly the private 256-bit token recorded in the process owner and
completion; otherwise ownership fails closed and state is preserved. A legacy
tokenless orphan is not signalable. Abort may also exact-discard a completed
uncollected job only when the recorded runner digest is stale; a current
completed job must be collected, and collected evidence can only use its remove
target. An active test prelaunch starter is refused, while its inactive exact
prelaunch/container/payload state is recoverable. Abort checks the owner record
and either the PID/start-time/process-group identity or the immutable container
ID and full labels, then force-removes only that exact runtime state. Use Make
lifecycle targets exclusively; never signal a job or run destructive Podman
commands directly.

Prefix every `RUN` and `IMAGE_RUN` with the current cycle identity. Once the
complete cycle is finalized, reviewed, and individually removed, follow
[`cycle-cleanup.md`](cycle-cleanup.md) to plan and digest-confirm deletion of
retained results.

Hosted develop test CI is limited to the three upstream unit-test legs. It never
runs a `live-*` target; physical display, render-node, and hardware-encoder gates
stay in the local live runbook.
