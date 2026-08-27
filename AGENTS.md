# Kogeler Xpra Fork Agent Guide

This repository is the user's Xpra fork. Upstream source and downstream fork
maintenance share one Git history; the tracked patch queue and its automation
live under `fork-maintenance/`.

## Sources of authority

Before changing Xpra source, read the current `CLAUDE.md`, `CONTRIBUTING.md`,
the canonical test workflow at `.github/upstream-workflows/test.yml` (verified
byte-for-byte against `upstream/master:.github/workflows/test.yml`), and
`pyproject.toml`. Before changing the fork workflow, also read
`fork-maintenance/CONTRACT.md`, the relevant runbook, and every selected
`cases/<id>/case.toml`.

Current source and maintainer feedback outrank old notes, logs, patch context,
or earlier conversations. Historical output is diagnostic context only; it is
never current acceptance evidence.

## Branch roles

- `upstream/master` is the canonical Xpra source.
- `origin/master` and local `master` are mirrors of canonical master. Never
  commit fork-only changes on `master`, never push a patch to it, and never
  force, reset, or rewrite it.
- `develop` is the rebase-maintained fork integration branch and intended
  default branch. It carries `AGENTS.md`, the ignore and CI boundaries, and
  `fork-maintenance/`. Production changes remain stored as patches rather than
  committed copies of those patches in the Xpra source tree.
- Temporary non-master branches may be used for isolated patch development,
  but the automation must also work directly on clean `develop`. Do not create
  parallel worktrees.

Host-worktree patch application or a publication refresh still starts from a
clean checkout and runs:

```bash
make -C fork-maintenance repo-sync
make -C fork-maintenance master-update
git switch develop
make -C fork-maintenance develop-rebase
make -C fork-maintenance patch-start-check
```

Pre-commit investigation, patch refresh, and testing use the isolated workflow
instead. Stay on `develop`; do not switch branches. Run:

```bash
make -C fork-maintenance isolated-start-check
make -C fork-maintenance workspace-create \
  CASE=<id> WORKSPACE=<unique-name> PATCH_MODE=patched
```

The isolated gate permits dirty files only at `AGENTS.md`, `.gitignore`, the
controlled `.github/` CI paths, and `fork-maintenance/`. It fetches and verifies
the live master refs, rejects any host Xpra source change, and copies the exact
verified master commit below
ignored `.artifacts/fork-maintenance/upstream-tests/workspaces/`. Patch
application, source editing, and candidate staging occur only in that copy.
`workspace-update` atomically exports the complete candidate back to the
selected `cases/<id>/fix.patch` and derives its digest and paths. The command
must leave the host branch, HEAD, index, and inherited Xpra source unchanged.

This fetches both master refs, compares each cached ref with live GitHub state,
and requires live fork/canonical equality. If and only if that fresh check
reports a mismatch, the operator may run:

```bash
gh repo sync kogeler/xpra --source Xpra-org/xpra --branch master
```

Never add `--force`, and agents never run this remote-mutating command. Repeat
`repo-sync` after the operator action before continuing. A divergent or ahead
fork master stops the workflow for owner review. `master-update` may only
fast-forward local `master` after that gate.

Upstream history is transferred to `develop` only by rebasing `develop` onto
the verified local `master`. Merging `master`, `upstream/master`, or an
equivalent upstream ref into `develop` is forbidden. If rebase stops, resolve
every conflict, stage the resolutions, and continue the rebase; do not begin
patch work until `patch-start-check` passes. Temporary patch branches are
created only from the fully rebased `develop`.

Rebasing an already published `develop` rewrites its fork-only commits. Agents
still never push or force-push. The operator may publish the reviewed rewrite
only with an exact-SHA `--force-with-lease`; plain `--force` is forbidden.

## Patch queue contract

`fork-maintenance/cases/<id>/fix.patch` is the source of truth for one atomic
production behavior plus its focused tests, except for the single explicitly
typed test-quarantine duty case. `case.toml` binds the exact patch digest,
paths, dependencies, tests, and required gates. The complete active queue is
`fork-maintenance/stacks/develop.toml`.

The currently retained active cases are:

- `wayland-initial-window-state`;
- `video-pipeline-cleanup-race`;
- `upstream-test-quarantine` (the single test-only duty case).

The quarantine case is not a production fix. It may change only the exact
upstream unit-test modules listed in its `[quarantine]` manifest table. Before
applying it after every upstream rebase, run all three clean `quarantine*`
gates. If any listed module is green on clean current master, remove or narrow
that entry and refresh the one quarantine patch; never carry it forward merely
because it still applies.

Do not resurrect deleted historical cases, verifications, evidence, or stacks
without an explicit new request and a current-source reassessment.

Host `patch-apply`, `stack-apply`, `patch-update`, and unapply operations are
permitted only after the clean sync/rebase start gate. They are retained for
exceptional host integration diagnosis. The default pre-commit cycle is
`workspace-create`, `workspace-stage`, `workspace-update`, and
`workspace-remove`; it never stages or edits inherited Xpra source in
`develop`.

Never edit `patch_sha256` or `paths` manually. Never leave the applied source
copy committed on `develop`; commit the maintained patch file and automation
metadata only. A patch that is neither forward-applicable nor exactly
reverse-applicable to current master is divergent and must be reworked, not
forced.

## Implementation discipline

Search current source, adjacent tests, and recent maintainer-authored history
before editing. Preserve client/server subsystem boundaries, feature toggles,
codec discovery, platform gates, and pkg-config authority. Do not add preload
tricks, import-order dependencies, polling, application-specific workarounds,
or build-only logic to the installed package. Avoid unrelated refactors and
formatting churn.

Work on one atomic behavior at a time. Preserve unrelated user changes and
remotes. Never reset, clean, or switch a non-clean checkout automatically.
Run `git diff --check` on every candidate and use the current upstream lint
configuration.

Every new source or test file introduced by a downstream patch must carry
`Copyright (C) <current-year> kogeler` using that file's native comment syntax.
Do not attribute a downstream-authored new file to an upstream maintainer.
When copied or derived content requires an existing notice to be retained, keep
that notice and add the `kogeler` line.

## CI boundary

Every canonical upstream workflow is kept as a byte-identical, non-executable
rename below `.github/upstream-workflows/`. The only executable workflow is
`.github/workflows/develop.yml`. After every rebase, preserve upstream
workflow edits through those renames, relocate any newly added upstream
workflow, and run `make -C fork-maintenance ci-layout-check`.

The executable CI file is a deliberately thin GitHub wrapper: it triggers only
for pushes to `develop`, grants read-only contents permission, pins every action
to a reviewed full commit SHA with its release version in a comment, selects
`ubuntu-26.04`, and declares only the fixed `full`, `full-cython`, and
`full-no-compat` matrix. Every matrix job invokes only
`make -C fork-maintenance ci-upstream-tests`, passing its fixed leg through
`XPRA_CI_TARGET`. Package installation, canonical source verification, image
ownership, patch application, and test implementation belong in
`fork-maintenance/`, never in YAML.

The hosted `ci-upstream-tests` path does not run `ci-layout-check`: GitHub has
already selected the executable workflow, and this publication audit must not
block the actual test matrix. Run it explicitly after rebase and before push.

Hosted CI also does not chase live `upstream/master`. It uses the checkout's
cached `origin/master` only to locate the merge base already embedded in the
pushed `develop`, then freezes that commit. A later `origin/master` advance must
not change the tested source. The CI automation never fetches, syncs, switches,
merges, or rebases after `actions/checkout`; live fork/canonical equality and
the actual rebase belong to the operator's pre-publication cycle.

Each CI matrix job applies the complete `stacks/develop` queue and runs one
upstream unit-test leg. The three legs run on independent hosted runners with
`max-parallel: 3` and matrix fail-fast disabled, so one failure does not cancel
the other results.
CI never starts live, display-hardware, render-node, or hardware-H.264 profiles.
Those remain local physical acceptance gates.

## Validation ladder

Stop at the first unexplained failure:

1. resolve or reproduce against unmodified current upstream master;
2. run the focused case regression;
3. run the affected native or subsystem boundary;
4. reassess every quarantined upstream module on clean master;
5. run all three Ubuntu 26.04 unit-test legs;
6. run required RGB or hardware-H.264 live acceptance.

Tests used to accept a patch belong in the tracked case or
`fork-maintenance/infra`. Ad hoc probes can diagnose but cannot establish
acceptance. Native tests must fail rather than skip when their module is the
subject of the patch. Compare clean and patched runs in the same frozen image
before assigning an environment failure to the patch.

Jobs expected to exceed two minutes use the named lifecycle interfaces in
`fork-maintenance/Makefile`. Test jobs are detached Podman containers; image
builds and live jobs use the owned Python process supervisor. Every run and
retry uses a new `RUN`; every image build and retry uses a new `IMAGE_RUN`.
The dedicated `ci-upstream-tests` target is the sole exception: GitHub Actions
owns its foreground job lifecycle and logs, while Make/Python still owns every
Podman build and run. It is CI coverage, not a substitute for named local
acceptance evidence.

Do not restart the functional ladder for a proven non-semantic refresh. This
exception is limited to an unchanged master and an exact old/new applied diff
containing only comments, copyright notices, or documentation, with no path,
mode, executable data, configuration, test assertion, or runner behavior
change. Resolve the refreshed queue, run whitespace and fork-control checks,
and state the proof in the handoff; do not launch focused, native, full, or live
jobs. Any uncertainty or semantic change uses the normal ladder.

Do not start or repeat an expensive downstream test when the observed failure
occurred in a pre-test guard and the change only removes or narrows that guard.
Prove that the failing command is now reachable with its narrow unit test and a
direct preflight reproduction. If master, patch and selection digests, image
inputs, entrypoint, and downstream test commands are unchanged, running the
matrix cannot validate the guard fix and is forbidden as wasteful. Rerun heavy
tests only when one of those downstream inputs or behaviors changed.

Operators and agents manage named jobs only through `fork-maintenance/Makefile`
targets. Do not signal recorded process groups or invoke destructive Podman
commands directly for a job lifecycle. Use the exact owned abort/remove target;
if one is missing, implement and test that target before operating on runtime
state. Process ownership binds the PID, process-group ID, kernel start ticks,
supervisor digest, private log, and completion record; container ownership binds
the immutable ID and exact labels.

## Runtime and result boundary

All logs, reports, screenshots, source bundles, build contexts, status files,
publication drafts, caches, virtual environments, and other generated output
live below ignored `.artifacts/fork-maintenance/` or another explicitly ignored
local path. They are never staged or committed.

Use one common prefix for every named run and isolated workspace in a work
cycle. After the patch queue and validation are final and reviewed, run the
two-phase `cycle-clean-plan` / digest-confirmed `cycle-clean` workflow. It may
remove only exact owned collected results and finalized workspaces, must refuse
active runtime state or an unexported candidate, and retains shared caches,
images, ccache, and virtual environments by default.

Do not create tracked `evidence/`, `runs/`, `results/`, or `communications/`
trees. Git history stores automation, patch inputs, tests, and contracts—not
the results of running them. Cleanup acts only on exact owned runtime objects
after review; it never deletes patches, cases, or unrelated Podman objects.

## Git and publication authority

- Do not commit unless the user explicitly asks in the current conversation.
- Never push, force-push, mutate a remote ref, or change global Git
  configuration.
- Never create, update, or close a pull request or change the default branch on
  the user's behalf from this workspace.
- Read-only fetch, `ls-remote`, branch/PR audit, and local fast-forward of
  `master` are allowed within the documented gates.
- The operator reviews, signs if required, pushes `develop`, and later changes
  the fork's default branch.

When handing off, show exact status, current master/develop commits, patch
resolution, validation completed, remaining validation, and resolved
operator-only commands. Do not claim results that exist only in an old log.
