# Run Upstream-Compatible Tests

## Frozen source model

Every local acceptance job archives the verified live `upstream/master`
commit, resolves the selected case or stack, and applies only
forward-applicable patches inside an isolated container source tree. Hosted CI
uses the same runner, derives the canonical base already embedded in pushed
`develop` as its merge base with checkout `origin/master`, and does not follow a
later fork or upstream update. It performs no fetch, sync, branch switch, merge,
or rebase after `actions/checkout`. Neither path packages `develop`, `.git`,
ignored files, credentials, or the host working-tree diff.

Verify the content-addressed Ubuntu 26.04 image first:

```bash
make -C fork-maintenance test-image
```

If it is absent, follow the durable image-build sequence in
[`bootstrap.md`](bootstrap.md). Do not let an ordinary test target silently
pull or rebuild its environment.

## Resolve before spending test time

```bash
make -C fork-maintenance patch-check CASE=wayland-initial-window-state
make -C fork-maintenance stack-check STACK=develop
```

Resolution must report only `apply` or exact `already-present`. A divergent or
ambiguous patch stops the ladder.

## Do not spend test resources on non-semantic refreshes

Before `test-start`, compare the exact old and new applied trees. Do not start
container tests when the verified master is unchanged and the only differences
are comments, copyright notices, or documentation, with identical paths,
modes, executable data, configuration, test assertions, source
selection/application, build commands, and runner behavior. Refresh derived
digests, resolve the selection, run whitespace and fork-control checks, and
report the proof instead. Any uncertainty or semantic difference resumes the
normal ladder.

## Focused tests

First run the retained regression against unmodified master production code by
applying only the selected test paths:

```bash
make -C fork-maintenance test-start \
  CASE=wayland-initial-window-state PATCH_MODE=tests-only \
  TARGET=focused RUN=wayland-master-regression-01
make -C fork-maintenance test-wait RUN=wayland-master-regression-01
```

This is the non-vacuous control for deciding whether upstream replaced a case.
`PATCH_MODE=clean` applies nothing and therefore cannot run a newly introduced
focused module; `PATCH_MODE=patched` applies the complete selected patch.

Run one atomic case while developing it:

```bash
make -C fork-maintenance test-start \
  CASE=wayland-initial-window-state TARGET=focused \
  RUN=wayland-focused-01
make -C fork-maintenance test-wait RUN=wayland-focused-01
```

Run the complete queue before handoff:

```bash
make -C fork-maintenance test-start \
  STACK=develop TARGET=focused RUN=develop-focused-01
make -C fork-maintenance test-wait RUN=develop-focused-01
```

Focused execution derives unit modules from the selected manifests. A missing
subject module is a failure, not a skip.

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

Use a new `RUN` for every leg:

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

All three legs are required locally even if upstream marks Cython-heavy CI
non-blocking. The container mirrors workflow dependencies and commands, but it
does not claim to be the GitHub-hosted runner image.

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

Before applying `upstream-test-quarantine` after every rebase, run its exact
module set on clean master in all three modes:

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

These targets intentionally invert the result. They return success only after
the build succeeds and every declared module is still an ignored failure; a
passing module or a different failure set makes the gate fail as stale. Follow
`test-quarantine.md` to remove or narrow stale entries before the patched full
matrix.

## Failure triage

Stop at the first unexplained failure. If it is outside selected paths, inspect
canonical Actions for the exact base and leg before running an expensive clean
control. An identical canonical failure is recorded in local notes and reported
as non-green; it is never skipped or fixed in the current production patch.
Only explicit user scope may admit it to the single duty quarantine, after a
current clean reproduction.

Run a clean control only when exact upstream CI proof is unavailable and the
user explicitly requests that additional run:

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
make -C fork-maintenance test-abort RUN=unfinished-name
make -C fork-maintenance test-image-abort IMAGE_RUN=unfinished-image-name
```

`wait` supervises completion and collects a no-clobber local result. Use
`collect` for an already-finished container or owned process. Review before
`remove`; removal targets only the exact owned container/process/context and
retains the ignored result files. Never reuse a run name or copy its result
into Git.

An abort target is only for an unfinished job with no collected output. It
checks the owner record and either the PID/start-time/process-group identity or
the immutable container ID and labels, then force-removes only that exact
runtime state. Use Make lifecycle targets exclusively; never signal a job or
run destructive Podman commands directly.

Prefix every `RUN` and `IMAGE_RUN` with the current cycle identity. Once the
complete cycle is finalized, reviewed, and individually removed, follow
`cycle-cleanup.md` to plan and digest-confirm deletion of retained results.

Hosted CI is limited to the three upstream unit-test legs. It never runs a
`live-*` target; physical display, render-node, and hardware-encoder gates stay
in the local live runbook.
