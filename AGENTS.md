# Kogeler Xpra Fork Agent Guide

This repository is the user's Xpra fork. Upstream source and downstream fork
maintenance share one Git history: `develop` is the upstream base plus control
commits (this guide, the ignore and CI boundaries, `fork-maintenance/`) and
exactly one case commit per fork case. Case documentation and all automation
live under `fork-maintenance/`; the model is specified in
[`fork-maintenance/docs/runbooks/case-commits.md`](fork-maintenance/docs/runbooks/case-commits.md).

## Sources of authority

Fork-maintenance process authority belongs exclusively to this fork-owned root
`AGENTS.md` and the maintained instructions, contract, runbooks, and manifests
under `fork-maintenance/`, subject to the operator's explicit instructions.
Follow only those documents for workflow, agent orchestration, validation
stages, test/build scheduling, reruns, acceptance, cleanup, and publication.

No content inherited from `master` is a source of fork-process instructions.
This prohibition includes upstream `AGENTS.md` or `CLAUDE.md` files,
`CONTRIBUTING.md`, READMEs, documentation, workflows (including their disabled
renames), commit messages, and maintainer process advice. Do not use that
content to introduce, replace, override, or expand a fork workflow requirement.
An upstream refresh changes technical inputs, not this authority boundary.
Never edit upstream-owned files to define or adjust fork-maintenance flow;
process changes belong only in this root guide and `fork-maintenance/`.

Before changing Xpra source, read the current `CLAUDE.md`, `CONTRIBUTING.md`,
the canonical test workflow at `.github/upstream-workflows/test.yml` (verified
byte-for-byte against the workflow at the source boundary embedded in current
`develop`), and `pyproject.toml`. Before changing the fork workflow, also read
`fork-maintenance/CONTRACT.md`, the relevant runbook, and every selected
`cases/<id>/case.toml`. Before changing a case, read
[`case-commits.md`](fork-maintenance/docs/runbooks/case-commits.md), its
`case.toml` and README, and its commit
(`make -C fork-maintenance case-show CASE=<id>`).

Reading upstream material supplies technical context about APIs, build/test
commands, dependencies, lint configuration, and source behavior only. It does
not make upstream development or CI procedures binding on this fork. For
technical correctness, current source and maintainer feedback outrank old
notes, logs, old diffs, or earlier conversations; they do not override the
fork's process rules. Unbound historical output is diagnostic context only, not
current acceptance evidence. A retained named result may satisfy a current gate
only under the exact-input/equivalence rules in the canonical validation flow.

## Session start and close

Every task is one session with a lowercase session ID, which is also the cycle
prefix of its runs. Follow the
[session runbook](fork-maintenance/docs/runbooks/session-close.md):

- start by reading `.artifacts/fork-maintenance/knowledge/INDEX.md`, the
  generated registry of earlier sessions, and open only matching records;
- during the session, keep any amount of output below
  `.artifacts/fork-maintenance/`; own notes, ledger and scratch go in
  `work/<session>/`;
- when the task is finished, before the final handoff and before any control
  commit that records it, distill the session into
  `knowledge/sessions/<session>.md`, run `knowledge-index`, then
  `artifacts-close-plan`, `artifacts-close CONFIRM=<digest>` and
  `artifacts-close-check`. Only the knowledge base survives; raw results,
  caches and scratch are discarded. Case commits made during the task are
  case storage, not session output, and stay on `develop`.

A paused, unfinished task keeps its session state and closes when it finishes.

## Autonomous upstream-refresh entry point

To make an agent rebase local `develop` onto existing local `master`,
reassess and adapt every active case commit, and execute the complete
acceptance cycle, give it this exact directive; no priority case is required:

```text
Execute autonomous-upstream-refresh against the current fork master.
```

This is an agent directive, not a shell command or Make target. It invokes the
complete **Autonomous Upstream Refresh and Full Queue Adaptation** runbook at
[`fork-maintenance/docs/runbooks/upstream-refresh.md`](fork-maintenance/docs/runbooks/upstream-refresh.md).
The directive is sufficient authorization for the whole local pass:

- inspect existing work and require a clean `develop` before rebasing
  (`develop-rebase` refuses any uncommitted change; later case rewrites carry
  uncommitted control work across with `--autostash`);
- record the case map (`case-list`) and the old `develop` tip, then rebase
  local `develop` onto recorded local `master` in the checkout without
  fetching, updating `master`, changing remotes, switching branches, or
  merging, resolving each conflict inside the case commit being replayed;
- after recording how every case commit replayed, deeply review one case at a
  time against the new source with equal priority and depth, including callers,
  ownership, failure paths, stack interactions and test blind spots;
- immediately implement each case's code-supported keep/adapt/retire decision
  and necessary cross-case repairs in the case commits (fixup plus
  `develop-squash`, or `case-drop`), re-review and checkpoint them before
  taking the next case; persist findings and exact next actions during work
  rather than accumulating a whole-queue read-only review;
- review the quarantine, finish regression migrations and review the resulting
  composition before any runtime test;
- repair any discovered case, control, test, package/live harness, contract,
  documentation, or runbook defect in the same uninterrupted pass, without
  restarting still-valid expensive gates unless their frozen semantic inputs
  changed;
- run all required clean controls, quarantine, focused/native and fork-control
  checks, both real DEB builds, all three full upstream legs, and all nine
  positive complete-stack live profiles;
- store every case adaptation in its unsigned case commit and leave
  control-plane repairs uncommitted for operator review unless the operator
  instructs a control commit; never push;
- distill the refresh into its session record and close the session before the
  final handoff.

The agent derives a unique cycle identifier, which is also its session ID; the
operator need not provide one or separately expand scope for another active
case. The older optional `PRIMARY_CASE=<slug>` spelling requests only a
starting order, never a deeper review for one case or a shallower review for
another. The Git mutations authorized by this directive are that local rebase
and the case-commit creation and rewrites the adaptation requires. Every other
Git or publication operation requires a separate explicit operator request;
the rewritten `develop` is published by the operator. Remote freshness and URL
spelling do not gate local case work or this refresh.

## Branch roles

- `upstream/master` is the canonical Xpra source.
- `origin/master` and local `master` are operator-maintained source refs. They
  may intentionally lag canonical master between explicit refresh cycles.
  Never commit fork-only changes on `master`, never push a fork commit to it,
  and never force, reset, or rewrite it.
- `develop` is the rebase-maintained fork branch and intended default branch:
  the upstream base plus control commits (`AGENTS.md`, `.gitignore`,
  `.github/workflows/`, `.github/upstream-workflows/`, `fork-maintenance/`) and
  case commits (product paths only, one `Fork-Case: <slug>` trailer each). Its
  checkout is the patched product; the case commits are the only storage of
  case code.
- There is no other branch and no Git worktree. Never create a per-case,
  topic, or temporary branch and never run `git worktree`; every case
  operation, check, and rewrite runs in the single `develop` checkout.

Ordinary investigation, case work, and testing use the upstream base already
embedded in current `develop`. They never fetch, compare live master refs, or
require a rebase. Stay on `develop`; do not switch branches. Run:

```bash
make -C fork-maintenance isolated-start-check
make -C fork-maintenance case-show CASE=<id>
```

The start gate locates the embedded base and refuses dirty product paths; only
`AGENTS.md`, `.gitignore`, the controlled `.github/` CI paths, and
`fork-maintenance/` may be dirty. Edit product files directly in the checkout
and store every change in its case commit as
[case commits](fork-maintenance/docs/runbooks/case-commits.md#everyday-operations)
specifies: change a case with `git commit --fixup=<sha>` (or
`--fixup=amend:<sha>`) followed by `make -C fork-maintenance develop-squash`;
add one with `case-new CASE=<id>` and one commit carrying its trailer; retire
one with `case-drop CASE=<id>` and removal of its directory; split, merge, or
reword with an explicit non-interactive `git rebase -i` sequence from the
upstream base. Run `case-check CASE=<id>` after each change. Product paths must
be clean before any rewrite or runner start, because runners test committed
`HEAD`; uncommitted control work survives every rewrite through
`--autostash`. A conflict while squashing means the edit overlaps another
case: the cases are not independent, which is resolved in the design, not by
a merge.

Only when the operator explicitly starts a new upstream-refresh and
adaptation cycle does `develop` move to a new base. Follow the canonical
autonomous full-queue procedure in
[`fork-maintenance/docs/runbooks/upstream-refresh.md`](fork-maintenance/docs/runbooks/upstream-refresh.md).

The operator performs Git operations other than case-commit work directly or
explicitly delegates them to the agent, who is responsible for their correct
execution. Within this runbook the agent also performs the local `develop`
rebase onto existing local `master` itself, including conflict resolution and
rebase continuation or abort. No fetch, master update, remote/configuration
change, branch switch, manual stash, preservation commit or publication is
implied. Require clean product paths before the rebase; inspect and preserve
other dirty work pending an explicit disposition. Read-only Git inspection and
the runners' private test indexes are part of authorized local work and leave
the host index, refs and configuration unchanged. Case adaptations are stored
in their case commits; control-plane results remain uncommitted unless the
operator instructs a control commit.

```bash
make -C fork-maintenance case-list          # record the case map and old tip
make -C fork-maintenance develop-rebase
make -C fork-maintenance patch-start-check
```

The refresh uses local `master` exactly as prepared by the operator and never
checks live remote equality. `repo-sync` and `master-update` remain tools for
separately requested Git operations, not refresh or case-work prerequisites.
Local gates validate source provenance, not a remote's transport or URL.

When the operator explicitly chooses to move the embedded source base, master
history is transferred to `develop` only by rebasing `develop` onto the existing
local `master` in the checkout. Merging `master`, `upstream/master`, or an
equivalent upstream ref into `develop` is forbidden. If that rebase stops,
resolve the conflict inside the case commit being replayed, stage the
resolution, and continue with `git -c commit.gpgsign=false rebase --continue`.
Mid-rebase the control files may be old or absent: read runbooks with
`git show ORIG_HEAD:<path>` and run tooling only after the rebase ends. Git
drops a case commit whose diff is already upstream byte for byte; retire the
other fully upstream cases with `case-drop`. Review each case with
`git range-diff <old-sha>^! <new-sha>^!` against the recorded case map, then run
`develop-check`. Rollback is always `git reset --keep <recorded tip>` or the
reflog.

Any case rewrite (fixup and squash, drop, split, merge, reword, or rebase)
rewrites already published `develop` history. The operator publishes it with an
exact-SHA `--force-with-lease` whenever needed; plain `--force` is forbidden and
the agent never pushes.

## Case commit contract

Every fork case is exactly one case commit on `develop`, specified in
[`fork-maintenance/docs/runbooks/case-commits.md`](fork-maintenance/docs/runbooks/case-commits.md).
It holds one atomic production behavior plus any case-owned focused regression
tests, except for the optional single, explicitly typed test-quarantine duty
case. A case commit touches only product paths and carries exactly one
`Fork-Case: <slug>` trailer; a control commit touches only `AGENTS.md`,
`.gitignore`, `.github/workflows/`, `.github/upstream-workflows/`, or
`fork-maintenance/` and carries no trailer. The two classes may appear in any
order. A commit mixing both classes, a product change without a trailer, a
merge commit, and an unsquashed `fixup!`/`squash!`/`amend!` commit are
invalid. The case documentation lives in `fork-maintenance/cases/<slug>/`
(schema-2 `case.toml`, `README.md`, optional local `tests/`) and changes only
through control commits; a case directory exists if and only if its commit
exists, except for the permanent quarantine directory. A production case may
name an existing focused module only when its README binds the durable real
boundary which proves the behavior. `case.toml` binds the dependencies, tests,
and required gates; subject, diff digest, and touched paths derive from the
commit and are not stored. The order of the case commits in `develop` is the
stack order, and a case which declares dependencies follows each of them;
`fork-maintenance/stacks/develop.toml` keeps only the stack-level test list.

A case's stable identity is the slug in its trailer. Never store a case commit
SHA in a tracked file; resolve it with `case-list`, `case-show CASE=<slug>`, or
`make -s -C fork-maintenance case-commit CASE=<slug>`. Directly under its H1
title every case README carries this reference line:

```text
Code: the `Fork-Case: <slug>` commit on `develop`
(`make -C fork-maintenance case-show CASE=<slug>`).
```

Session records and ledgers also name cases by slug; a ledger may record the
SHAs of one moment, such as the case map before a rebase, as a point-in-time
record.

Every production case README must meet the mandatory
[case documentation standard](fork-maintenance/docs/runbooks/case-documentation.md)
in the same implementation/review pass as its case commit, before handoff.
Cover current-source necessity, callers and ownership, mechanism and
failure paths, stack interactions, regression oracles and blind spots, durable
live/package boundaries, non-goals and maintenance invariants. A short diff,
filled headings or passing tests do not excuse a summary-only README. The
agent completes this without a separate operator request; runtime results
remain in the ignored cycle ledger.

The currently retained active cases, in stack order, are:

- `window-source-timer-lifecycle`;
- `video-pipeline-cleanup-race`;
- `wayland-subsurface-stream-ownership`;
- `wayland-initial-window-state`;
- `wayland-client-keymap-sync`;
- `x11-client-clipboard-events`;
- `packet-handler-error-boundary`;
- `gtk-client-scroll-deduplication`;
- `server-shutdown-disconnect-flush`.

There is currently no active quarantine duty case, but
`fork-maintenance/cases/upstream-test-quarantine/` is permanent infrastructure.
Never delete its directory, manifest, README, or supporting gates and runbook
merely because no upstream tests are broken. Inactive means no
`Fork-Case: upstream-test-quarantine` commit, empty `quarantine.modules`, empty
gate lists, and an empty `tests.list`; the case is not selectable. This
reserved scaffold is not an active case or a historical archive. Record
reassessment as not applicable; do not test-select it or restore old skips.
All production and final full-suite gates remain required. Follow the
[quarantine runbook](fork-maintenance/docs/runbooks/test-quarantine.md) to
activate the existing scaffold (the manifest lists the modules, gates, and
tests, and one quarantine commit changes exactly those test files) or to
deactivate its last assignment (`case-drop`, then empty the lists again)
without deleting the infrastructure.

The quarantine case is not a production fix. Its commit may change only the
exact upstream unit-test modules listed in its `[quarantine]` manifest union.
After the whole-queue manual-review exit gate for every fork-master rebase, run
all three clean `quarantine*` gates before using the duty commit in runtime
validation. Inspecting or replaying the commit does not certify an assignment.
Each gate must confirm its exact assigned failure subset and that every other
listed module is green in that leg. Remove or narrow a stale gate assignment
and fix up the one quarantine commit; a module which is deliberately assigned
only to another failing leg is not stale merely because it is green here.
Never carry quarantine forward merely because its commit still rebases
cleanly.

Do not resurrect retired cases, deleted evidence, or stacks without an
explicit new request and a current-source reassessment.

The current runner-owned native pointer protocol tests are maintained under
`fork-maintenance/infra/upstream-tests/neutral/` after their production fix was
absorbed upstream. Their fixed image/runner-bound test inventory is installed
only in the runners' private test source copies, including production-clean
controls. Use the documented clean and complete-stack native gates; these are
not a production case, a restored historical archive or a live bypass.

Change case commits only through the documented operations: fixup plus
`develop-squash`, `case-new` plus one trailer commit, `case-drop`, an explicit
non-interactive `git rebase -i` sequence from the upstream base, and
`develop-rebase`. Never retire a case with a revert commit, never commit
product code outside its case commit, and never fold several cases into one
commit. Never store derived data (commit SHA, subject, diff digest, touched
paths) in a manifest.

`case-check CASE=<slug>` proves that the schema-2 manifest is valid, exactly one
commit carries the trailer, it touches only product paths, its diff applies to
the upstream base on its own after its declared dependencies and is neither
already present nor ambiguous, and reverting it from `HEAD` is conflict-free
with no other case depending on it. `stack-check STACK=develop` proves that
every case commit resolves in order on the base and that base plus all case
diffs reproduces exactly the product tree of `HEAD`. `develop-check` requires a
clean tree, valid control and case commits only, no merges or pending fixups,
one-to-one case directories and commits, dependencies before their consumers,
and every `case-check`, `stack-check`, and `ci-layout-check`. These proofs are
in-memory merges; they never check out another tree or create a branch. A case
commit which fails them is reworked, not forced.

## Implementation discipline

Use the scoped [mypy gate](fork-maintenance/docs/runbooks/typecheck.md) for its
explicit downstream-owned modules and their type contracts. Do not type-check
the entire upstream project, suppress a global baseline, or repair unrelated
upstream typing. Report the actual checked scope; static checks never replace
native regressions or the mandatory complete live suite.

Search current source, adjacent tests, and recent maintainer-authored history
before editing. Preserve client/server subsystem boundaries, feature toggles,
codec discovery, platform gates, and pkg-config authority. Do not add preload
tricks, import-order dependencies, polling, application-specific workarounds,
or build-only logic to the installed package. Avoid unrelated refactors and
formatting churn.

Work on one atomic behavior at a time. Preserve unrelated user changes and
remotes. Never reset, clean, or switch a non-clean checkout automatically.
Run `git diff --check` on every change before committing it to its case and
use the lint configuration from the source embedded in current `develop`.

Every new source or test file introduced by a case commit must carry
`Copyright (C) <current-year> kogeler` using that file's native comment syntax.
Do not attribute a downstream-authored new file to an upstream maintainer.
When copied or derived content requires an existing notice to be retained, keep
that notice and add the `kogeler` line.

## CI boundary

Every canonical upstream workflow is kept as a byte-identical, non-executable
rename below `.github/upstream-workflows/`. The only executable workflows are
`.github/workflows/develop.yml`, `.github/workflows/master-sync.yml`, and
`.github/workflows/deb-packages.yml`. During every explicit upstream refresh,
preserve upstream
workflow edits through those renames, relocate any newly added upstream
workflow, and run `make -C fork-maintenance ci-layout-check`.

The executable `develop.yml` test workflow is a deliberately thin GitHub
wrapper: it triggers only for pushes to `develop`, grants read-only contents
permission, pins every action to a reviewed full commit SHA with its release
version in a comment, selects `ubuntu-26.04`, uses a six-hour job timeout, and
declares only the fixed `full`, `full-cython`, and `full-no-compat` matrix.
Checkout fetches full history without persisting credentials. Every matrix job
invokes only `make -C fork-maintenance ci-upstream-tests`, passing its fixed leg
through `XPRA_CI_TARGET`. The hosted preflight requires the checkout to remain
clean at the exact `GITHUB_SHA`. Package installation, exact frozen-source
verification, image ownership, case-diff application, and test implementation
belong in `fork-maintenance/`, never in YAML.

The master-sync workflow runs at minute 37 every 12 hours and may also be
dispatched manually by the operator. Its sole job has job-scoped
`contents: write`, checks out the automation from `develop` at depth one
without persisting credentials, and invokes only
`make -C fork-maintenance ci-master-sync`. That target may fast-forward only
remote fork `master` from `Xpra-org/xpra:master` through `gh repo sync` without
`--force`; it verifies exact live equality afterward and must not change,
merge, rebase, or publish `develop`. Agent dispatch requires a separate explicit
operator request; ordinary case work and refresh do not authorize it.

The package-release workflow is manual-only and branch-agnostic. Its six-hour
job checks out full history for the operator-selected revision without
persisting credentials and invokes only
`make -C fork-maintenance ci-deb-release` on Ubuntu 26.04 with job-scoped
`contents: write`. Package source discovery never fetches or names the current
branch or a remote: it uses `HEAD` plus local or remote-tracking refs whose
final component is exactly `master`, requires one uniquely latest clean merge
base, rejects downstream merge commits, product changes outside valid case
commits, and dirty product paths, and freezes the case commit diffs of `HEAD`
as the complete stack. `stacks/develop` is the fixed stack slug, not a
requirement that the selected revision be on a branch named `develop`. The
amd64 builds require an x86-64 Podman host, network access, and sufficient
disk space. They
build only the frozen fork source and resolve build dependencies from the
target Ubuntu or Debian archives. They do not enable the Xpra APT repository,
trust its signing key, or consume prebuilt Xpra packages. They produce unsigned
packages with `dpkg-buildpackage -us -uc`, force xz Debian members, disable
automatic dbgsym generation with
`DEB_BUILD_OPTIONS=noautodbgsym`, reject any debug-symbol package at both sides
of the container boundary, and validate each xz stream with a 256 MiB decoder
memory limit. The patched Debian packaging uses `dh_missing --fail-missing`, so
every staged build result must be assigned to one binary package or to the
small exact reviewed `not-installed` set. Before emit, the builder inventories
every actual DEB, rejects duplicate package identities and overlapping regular
payload paths, extracts the real `xpra-common` and `xpra-codecs` packages, and
imports five required native modules with the distribution Python: the libva
encoder/decoder, libyuv converter, and JPH encoder/decoder, all owned by ordinary
`xpra-codecs` with one matching amd64 CPython ABI. The extracted JPH pair must
complete a deterministic 32x32 quality-100 lossless RGB roundtrip. The builder
also runs `dpkg-shlibdeps` over all five packaged ELF objects and proves that
the resulting library dependencies are present in `xpra-codecs`, without
guessing an OpenJPH SONAME. The host independently parses every returned
ar/control/data archive and repeats package-set, payload ownership, filename
ABI, and declared dependency-name checks. Native imports, pixel execution and
ELF dependency resolution remain container-side checks. They build separate
Ubuntu 26.04 and Debian 13 tar assets, then
stage a draft with `prerelease=false`, upload and verify exactly those two
assets, and publish an ordinary release whose title is exactly the Debian
version, for example `6.6-r42479-1`. Its unique transaction tag points at the
dispatched checkout commit. Publication binds the immutable GitHub release ID.
On failure, the target may roll back only the release it just created and a tag
that still points at that exact commit, deleting and verifying the tag before
deleting the immutable release ID. The input-keyed builder cache is label-verified,
every created package container executes the actual immutable builder image ID,
and every accepted package result binds it. Source and complete-queue snapshots
are immutable retained caches; the latter is stored as
`selections/<selection-sha>-<metadata-sha>/{lab,selection.json}`, and every
package owner binds its exact selection-state path and both digests. Local
package start publishes a retained prelaunch owner before its run directory and
main owner; removal publishes a retained result-bound transaction before its
first destructive step. A rerun of one hosted Actions run may recover only an
exact orphan draft from its own earlier failed attempt. Draft creation uses the
authenticated releases REST endpoint and binds the immutable release ID from
that response; draft discovery never uses the published-only tag endpoint.
Current-tag absence and orphan recovery scan a bounded paginated release list
and require one unique exact transaction. After publication, that same listing
identifies only canonical ordinary DEB releases owned by this workflow, orders
them by publication time with immutable-ID tie-breaking, retains the three
newest, and deletes every older owned release in exact tag-first,
release-ID-last order. Drafts and unrelated or manual releases are never
retention targets; malformed or ambiguous owned state fails closed. A retry of
the same hosted run may resume retention from an exact published release left
by a failed or cancelled prior attempt without publishing a duplicate. Agent
dispatch requires a separate explicit operator publication request.

The hosted `ci-upstream-tests` path does not run `ci-layout-check`: GitHub has
already selected the executable workflow, and this publication audit must not
block the actual test matrix. Run it explicitly after an upstream refresh and
before push.

Hosted develop test CI does not chase live refs. It uses the checkout's
cached `origin/master` only to locate the merge base already embedded in the
pushed `develop`, then freezes that commit and the case commit diffs above it.
A later `origin/master` advance must not change the tested source. The develop test automation never fetches, syncs,
switches, merges, or rebases after `actions/checkout`; choosing whether to
refresh and rebase the source base belongs solely to the operator.

Each matrix job in the `develop.yml` test workflow applies the complete
`stacks/develop` case series and runs one upstream unit-test leg. The three test legs
run on independent hosted runners with `max-parallel: 3` and matrix fail-fast
disabled, so one failure does not cancel the other results. The master-sync and
package-release workflows have no test matrix.
CI never starts live, display-hardware, render-node, or hardware-H.264 profiles.
Those remain local physical acceptance gates.

Every Podman source/build-context transfer uses the common validated streaming
tar helper. Container-produced artifacts use the reverse stdout tar boundary
only where a caller requires them; upstream unit tests return only their normal
logs and recorded resolution digest. Bind mounts, bind-style `--mount`, and
`podman cp` are forbidden for source, patch, application-input, or artifact
transfer. The upstream-test named ccache volume is a cache-only exception, and
render nodes passed with `--device` are hardware access rather than file
transfer. Upstream-test containers wait for their streamed payload through the
pre-created validated readiness FIFO; readiness uses no process signal. The
sender makes bounded non-blocking open retries only until the FIFO reader is
attached, then writes one ready byte. Tar extraction stages only at the exact
`.<destination>.partial` sibling, refuses a pre-existing partial, and publishes
with an atomic no-replace rename. The common reader accepts only plain,
uncompressed tar and enforces raw-archive, member, content, and extended-
metadata bounds before publication. Reverse process output without a
caller-owned deterministic partial uses an anonymous `O_TMPFILE`, fsync, and
no-replace link; the common helper has no named generic fallback.

## Validation phases

Use the canonical [development and final-acceptance flow](fork-maintenance/docs/runbooks/validation.md)
for new cases, existing-case review and upstream-rebase adaptation. Required
gates define final coverage, not a sequence to repeat after every edit.

An explicit upstream refresh first completes an incremental manual
review-and-implementation pass after the rebase has replayed the case commits.
Review one case, immediately implement its justified changes and necessary
cross-case repairs in the case commits, re-review, and save an input-bound
checkpoint before taking the next case. Persist findings and exact resume
actions while working; do not defer implementation until the whole queue has
been reviewed. Preserve regression
ownership, then review the resulting composition before any Xpra, quarantine,
native/compiled, live or real package run. Offline fork-control/safety and
static checks remain allowed.
Only after that recorded manual-review exit gate does the development testing
loop below begin. Tests challenge the conclusions; they never substitute for
the agent's analysis of paths and interleavings which tests do not cover.

Every live test MUST apply the complete current `stacks/develop` queue to BOTH
the server and client. Case-only, partial-stack and clean-endpoint live tests
are forbidden, including newly developed fixtures. A scenario may target one
behavior, but its running product must contain every active case commit. One
commit per case and case-only unit/negative controls do not authorize isolated
live runs. For validation of ANY case change, the entire nine-profile live
suite must pass in one
complete `make -C fork-maintenance live-all STACK=develop RUN=<fresh-prefix>`
pass. Reach it through the live loop in `fork-maintenance/docs/runbooks/live-tests.md`
and never rerun the whole set to test a fix: when a gate fails, find the
problem, fix it (code or test, from evidence), and continue from that same gate
with `live-all ... FROM=<gate>` under the next fresh prefix, until the last gate
passes. Then run one complete pass from the first gate; if a gate fails, fix
it, continue from it the same way, and finish with another complete pass,
until one complete pass succeeds without a fix.
A single-profile pass or a continuation cannot accept a case change.
`live-suite-check` verifies complete coverage and matching current source, queue, harness and endpoint provenance.
It also checks the complete report-bound stdout/stderr of both peers in every
scenario for undeclared Wayland display-name signals, codec startup waits,
clipboard rate warnings and selection timeouts, including PRIMARY/SECONDARY.
The suite checks each collected member before starting the next profile; a
successful paste or rendering result cannot hide one of these warnings.
This requirement also applies to unchanged-base repairs; no case manifest can
waive it. Never report retired case-only or clean-client results as current proof.

During development, freeze the embedded base, establish a non-vacuous clean
control, and run the nearest real regression immediately after each atomic
edit. Runners test committed `HEAD`, so fold the edit into its case commit
(fixup plus `develop-squash`) before starting one; control paths may stay
dirty. Include affected upstream modules, case regressions and relevant
dependent/composed tests; exercise native, compiled and compatibility modes
according to the changed boundary. Start the live loop early.
Full upstream unit suites are not a prerequisite
for live diagnosis or acceptance.
Stop escalation at the first unexplained failure and investigate its owner.

Do not automatically run the full upstream matrix or both DEB builds after an
intermediate correction. Freeze a reviewed candidate
only when source, tests, fixtures/oracles and build inputs are stable; then fill
missing or invalidated final gates. The full queue/rebase acceptance still
requires clean controls/quarantine, focused/native and full fork-control checks,
all three full upstream legs, both DEB builds and all nine complete-stack live
profiles. Reuse exact valid development evidence rather than
rerunning it merely because the phase changed. A newly found defect returns
its owner to the development loop before affected final jobs are rescheduled.

The fixed `live-xpra-hardware` gate uses `APPLICATION=hardware`,
`ENCODING=h264`, `H264_CLIENT_POLICY=adaptive-alpha`,
`ALPHA_SCENARIOS=default`, and the application-exit lifecycle. It resolves the
primary `vkcube` and auxiliary GTK Xpra window IDs independently by their exact
titles. Its first saved `window.info` is only an initial snapshot and must be
`BGRX` or `RGBX`; exact per-window frame-state logs prove that later primary
frames remain opaque. Startup layout and picture packets are structurally
validated but cannot establish acceptance. After both title-bound windows are
stable, the runner binds the active primary IDR group to its exact saved source
geometry, records an exact input interval, and closes it before the auxiliary
window exits. Within it, only positive H.264 main regions and their exact
required one-pixel lossless RGB24/RGB32 codec edges are allowed; arbitrary,
interior, larger, or alpha-bearing RGB regions fail. H.264 must predominate for
at least ten frames and one second, cover at least 99% of each production
window and 90% of all encoded pixels, and satisfy the exact VA-API
encode/decode, packet-chain, hardware-presentation, and pixel checks. Safe
startup or post-exit resize packets never contribute to those thresholds.

The auxiliary native-Wayland GTK fixture requires an RGBA visual and a
deterministic transparent border around its opaque interactive button. Its
`BGRA`/`RGBA` window must expose both transparent and opaque pixels in every
collected source screenshot for that exact window and emit only positive WebP
or alpha-bearing RGB32 packets with exact contained geometry. Client captures
prove the visible composited result and input response; they need not retain a
source alpha channel after composition. H.264, RGB24, and non-alpha RGB32 are
failures.

The fixed `live-xpra-opengl-hardware` gate uses the same adaptive-alpha,
application-exit, H.264, auxiliary-window, input, VA-API, client-presentation,
pixel, lifecycle, and cleanup contract. Its separately title-bound opaque
primary is the native-Wayland `glmark2-wayland` synthetic OpenGL `jellyfish`
benchmark instead of `vkcube`. It requests an EGL visual with no alpha channel.
Its fixed source viewport may be smaller than the tiled client backing, so the
pixel gate requires the exact logged viewport placement before comparing the
source crop. The server process must report metadata from a live OpenGL context,
use the selected render node and AMD Mesa/Radeon hardware driver rather than a
software renderer, and produce changing nonuniform client frames. This
complements the Vulkan gate; neither is a substitute for the other.

The fixed complete-stack positive live profiles remain Zed RGB, adaptive-alpha
Zed H.264, RGB detach, RGB transport-loss fault injection, native-Wayland
client-keymap input, multi-window Vulkan hardware, and multi-window OpenGL
hardware, clipboard synchronization, and subsurface composition.
The two hardware profiles and GTK detach/transport-loss profiles also require
real client scroll input, exact packet/native-axis accounting, remote GTK
displacement and visible feedback. Hardware uses Sway-to-Xwayland smooth input
without prescribing a client filtering implementation; GTK lifecycle profiles
use genuine XTEST discrete input. Each stimulus must yield exactly one remote
scroll step, including the first; forwarding both representations is a failure.
Collection reparses the complete post-input log tails and
fixture stream, so late duplicates cannot be hidden by an earlier screenshot.
The `live-x11-clipboard` gate uses the complete stack on the X11 client and
native-Wayland server endpoint, disables the unrelated client
XSettings and XI2 paths, and runs fresh `both`, `to-server`, and `off` sessions.
Its Wayland reverse owner is armed by a private command but claims inside a real
F8 event delivered through Xpra, then requires a compositor `owner-change`
confirmation. The root XFixes monitor remains active through that phase: it
must record a third takeover matching the raw reverse consumer under `both`
and exactly the two local same-XID updates under `to-server` and `off`.
It also covers controlled Xpra client shutdown and drains queued X11 events
after client exit. Only an exact shutdown-only zero-owner notification may be
separated from production takeovers; late nonzero takeovers remain failures.
Retained compositor source intervals and cross-stream fixture chronology are
reparsed during collection.
The same gate exercises real Ctrl+V/context-menu GTK paste and a rapid PRIMARY
selection burst on one line. Its event-driven X11 consumer must receive the
final selected value with `both` while reverse-denied policies retain local
contents. Complete peer stdout/stderr tails must contain no clipboard flood
warning or request timeout; all native consumer requests must be drained.

The `live-wayland-subsurface` gate likewise applies the complete stack to
both the native-Wayland server and GTK X11 client. Its schema-6 fixture keeps
two parent windows and stable child identities, exercises scale-2 and transform-180
buffers, stacking, move, detach, destroy, same-surface reparent, native leaf
pointer input, and a callback-gated continuous child producer. Retained raw RGB
packet payloads are checked against independent deterministic source pixels
and then replayed into the parent; asynchronous source screenshots cannot
replace either authority. Initial damage and client-map refresh have a bounded
one-or-two-capture startup ledger for each root; all initial transactions and
ordinary secondary packets retain exact pixel, draw, ACK, and sequence checks.
While the producer is active, at least two complete
distinct transactions must finish. Generated commits/callbacks and captured
transactions are counted separately: pending damage may coalesce, but each
captured transaction requires exact packet/ACK accounting. After stop, no
pending region, partial transaction, or ACK owner may remain, and the final
parent must match the last committed source state exactly. The live client uses the
profile's fixed Cairo renderer; real mapped GTK OpenGL replacement/close
semantics remain a focused Xvfb regression in the case.

Continuous fixture commits require both the real frame callback and a 50 ms
monotonic cadence floor, without catch-up bursts or blocking input/stop handling.
The active observation must finish within five seconds of continuous-start,
including packet collection, while the unchanged 256-generation cap has not
been reached. Retain the initial observation and prove a later source generation;
compare captured transactions with the fresh post-transfer generation prefix.
The schema-3 active/drain record fixes a packet frontier from the first primary
inventory before collecting the other streams. Its exact prefix and single
root-stage tail must match the final immutable packet ledger; later packets
remain mandatory drain and global-accounting evidence. Bounded observation
diagnostics record stages and timing, never pixel payloads.
This is fixture/observer timing, not an Xpra production throttle or one-packet-
per-commit requirement.

All nine gates belong to one mandatory complete-stack suite. The Make wrappers
fix every acceptance dimension and require exactly `STACK=develop`,
`PATCH_MODE=patched`, and no `CASE`. The
orthogonal client-only `NETWORK_PROFILE` is loaded from
`fork-maintenance/profiles.yml`; its YAML default is used for the nine gates.
Static Xpra arguments come only from `fork-maintenance/live-cli.yml`.
Neither YAML value set may be duplicated in Python, Make, or unit-test
assertions. A clean-source or picture-fallback diagnostic cannot publish live
acceptance. Negative unit cases only prevent a false pass; every public live
target must finish with positive rendering, input, lifecycle, and owned-cleanup
evidence.

Upstream-test, DEB, and live runners keep their selectors and modes:
`CASE=<slug>` or `STACK=develop`, with `PATCH_MODE=clean`, `tests-only`, or
`patched`. At start they freeze the selection into their private payload: the
manifests plus, for every selected case, the diff of its commit in `HEAD`
(`git diff --binary --full-index`), written as that case's generated
`fix.patch` inside the payload only. The selection digest binds those bytes,
so a changed commit is a new candidate; `tests-only` applies only the `tests/`
part of that diff to the clean upstream base. Product paths must be clean at
start; control paths may be dirty. See
[runners](fork-maintenance/docs/runbooks/case-commits.md#runners).

Tests used to accept a case belong in its case commit, its case directory, or
`fork-maintenance/infra`. Ad hoc probes can diagnose but cannot establish
acceptance. Native tests must fail rather than skip when their module is the
subject of the case. Compare clean and patched runs in the same frozen image
before assigning an environment failure to the case.

Jobs expected to exceed two minutes use the named lifecycle interfaces in
`fork-maintenance/Makefile`. Test jobs are detached Podman containers; standalone
upstream-test image builds, live jobs, and DEB jobs use the owned Python process
supervisor. Every test, live, or DEB run and retry uses a new `RUN`. Only a
standalone upstream-test image build and retry uses a new `IMAGE_RUN`; image
builds embedded in a live or DEB job belong to that parent `RUN`. No lifecycle
target creates a systemd unit or invokes `systemctl`.
The dedicated `ci-upstream-tests` and `ci-deb-release` targets are the hosted
exceptions: GitHub Actions owns their foreground job lifecycle and logs, while
Make/Python still owns every Podman build and run. They are not substitutes for
named local acceptance evidence.

Do not restart the functional ladder for a proven non-semantic refresh. This
exception is limited to an unchanged embedded source and an exact old/new applied diff
containing only comments, copyright notices, or documentation, with no path,
mode, executable data, configuration, test assertion, or runner behavior
change. Run `case-check` and `stack-check`, whitespace and fork-control checks,
and state the proof in the handoff; do not launch unchanged focused, native or
full jobs. Case validation still requires the complete live suite described
above. This exception never applies after `develop-rebase`: every explicit
upstream rebase requires the clean quarantine reassessment, all fork-control,
tests-only clean controls or documented no-test semantic substitutes, patched
focused/native gates, every durable package boundary on the resulting stack,
all three full upstream legs and all nine positive complete-stack live profiles,
even when every retained case commit replays without textual changes.
Complete this set on the stable new-base candidate, not after each
intermediate edit. Any
uncertainty or semantic change uses the development loop and affected final
gates described in the canonical flow.

Do not start or repeat an expensive downstream test when the observed failure
occurred in a pre-test guard and the change only removes or narrows that guard.
Prove that the failing command is now reachable with its narrow unit test and a
direct preflight reproduction. If the exact frozen fork source commit, case-diff
and selection digests, image inputs, entrypoint, and downstream test commands are
unchanged, running the matrix cannot validate the guard fix and is forbidden as
wasteful. Rerun heavy tests only when one of those downstream inputs or
behaviors changed.

Operators and agents manage named jobs only through `fork-maintenance/Makefile`
targets. Do not signal recorded process groups or invoke destructive Podman
commands directly for a job lifecycle. Use the exact owned abort/remove target;
if one is missing, implement and test that target before operating on runtime
state. Process ownership binds the PID, process-group ID, kernel start ticks,
supervisor digest, private 256-bit owner token, private log, and completion
record; the token is repeated in the completion and inherited by every payload
process. The supervisor cannot start that payload until the owner is durably
published and its private release pipe receives one exact byte; EOF before the
byte fails closed. Container ownership binds the immutable ID and exact labels.
Abort may discard running or lost uncollected state, or completed uncollected
state only when its recorded runner has become stale. `lost` requires no valid
completion and no remaining exact owned runtime; a dead process-group leader
with a live owned member remains running. Every such member must expose exactly
the recorded owner token; a missing, duplicate, or mismatched token fails closed
and preserves the state for review. A legacy tokenless orphan is not signaled.
A current completed job must be collected, and collected evidence uses its
exact remove target. Detached upstream tests publish an inspectable prelaunch
owner before container creation. Live start first publishes
`jobs/live/<RUN>.freeze-prelaunch.json`, then the background input-freeze owner;
local DEB start publishes `deb-packages/runs/<RUN>.prelaunch.json`. Their exact
abort paths handle inactive/orphaned staging without guessing from a name. DEB
abort publishes `deb-packages/runs/<RUN>.abort.json` before its first destructive
step, resumes that exact transaction after interruption, and deletes it last.
Before discarding a freeze-owned live input tree, `live-abort` publishes
`jobs/live/<RUN>.freeze-abort.json`, atomically stages each exact directory at
`live-results/.<RUN>.freeze-abort-{staging,result}`, and deletes the transaction
only after both are gone. A standalone upstream image build likewise publishes
`image-builds/.<IMAGE_RUN>.image-prelaunch.json`; status/abort route that exact
boundary and normal remove/abort deletes it. Upstream-test and live terminal
transitions each use one retained subsystem `.lifecycle.lock`; DEB terminal
transitions use retained `deb-packages/locks/terminal.lock`. Upstream image
creation/use/removal is serialized by retained
`upstream-tests/image-builds/.image-cache.lock`; each DEB builder-image key uses
its retained `deb-packages/locks/images/<distro>-<input-sha>.lock`. Hosted
foreground test selection uses marker-owned
`upstream-tests/.foreground-payload` under retained
`.foreground-payload.lock`. Named local DEB output
validation uses exact marker-owned `.<tar>.validate` /
`..<tar>.validate.partial` siblings; only validation, `deb-remove`, or
`deb-abort` may recover them.

`live-start` holds the live lifecycle lock from before input freeze through
durable main-owner publication. Upstream `test-start` holds both lifecycle and
image-cache locks through create, start, and payload delivery. The create/start
children inherit only the lifecycle descriptor; the Python starter itself holds
the image-cache lock through immutable-ID handoff and payload delivery so
Podman's long-lived networking helper cannot retain that cache lease.

Upstream image-cache removal refuses any matching unresolved image-build or
test prelaunch/owner. Cleanup may accept an older source label only after
proving that it names an existing commit which is an ancestor of, or equal to,
the current embedded source, while all other ownership labels, image inputs,
workflow digest, and immutable image ID still match exactly. An unknown,
unrelated, or future source is not removable, and this cleanup path cannot
create acceptance evidence.

Every collected test, standalone upstream image build, live run, and DEB build
publishes an immutable removal transaction before deleting runtime ownership.
It binds the reviewed evidence and old ownership, makes interrupted removal an
exact idempotent retry, and remains with the result until digest-confirmed cycle
cleanup. Never delete such a transaction by hand.

After a live main owner is gone, its exact schema-1 removal transaction alone
authorizes read-only inspection. `live-status` validates it and reports
`phase=removing` while bound runtime remains or `phase=removed` otherwise;
`live-logs` emits only its validated digest-bound final log. This post-remove
route is separate from pre-main freeze routing, and any transaction or evidence
mismatch fails closed.

## Runtime and result boundary

All generated filesystem output—logs, reports, screenshots, source bundles,
build contexts, status files, publication drafts, caches, and virtual
environments—lives below ignored `.artifacts/fork-maintenance/`; only
transient interpreter caches such as `__pycache__` may use another explicitly
ignored local path. It is never staged or committed. Owned Podman
containers, images, networks, and volumes remain engine runtime objects and are
controlled by the corresponding lifecycle and label checks.

Immutable runner records require an artifacts filesystem supporting
`O_TMPFILE` and `linkat(AT_EMPTY_PATH)`: they are fsynced as anonymous files,
linked without replacement, and followed by a directory fsync. There is no
named temporary-file fallback. The live environment uses retained
`venvs/.environment.lock` and exact marker-owned `.environment.partial` state;
only a later `live-venv` performs its locked recovery.

Use the session ID as the one common prefix for every named run in a work
cycle. To discard one reviewed cycle's results while the session continues,
run the two-phase `cycle-clean-plan` / digest-confirmed `cycle-clean`
workflow. It may remove only exact owned collected results, must refuse active
runtime state, and retains shared caches, including frozen source and DEB
selection snapshots, images, ccache, and virtual environments;
`artifacts-close` discards those filesystem caches when the session ends.
Retained lock files are validated; source, selection, matching DEB validation
scratch, a DEB abort transaction, or live-freeze prelaunch/abort
transaction/partials block cleanup. Planning and removal acquire the upstream
lifecycle, upstream image-cache, live lifecycle, and DEB terminal locks in
that fixed order. Before deleting the first reviewed target, `cycle-clean`
publishes the schema-2 `cycle-cleanups/<CYCLE>.remove.json`, including the
device, inode, and fingerprint of each directory target. It atomically stages
such directories at `cycle-cleanups/.<CYCLE>.<index>.remove`, then publishes
schema-1 `.<CYCLE>.<index>.rmtree.json` before recursive deletion. Once that
phase exists, a retry validates its transaction binding and the staging device/inode rather
than re-hashing the necessarily partial tree. Interruption is resumed with the
same `CYCLE` and confirmation digest, never a new plan or manual deletion.
Cleanup is branch-agnostic and neither requires nor changes a named remote,
branch, or ref.

The permanent structural allowlist is `fork-maintenance/artifacts.toml`, not
an agent-selected list of run names, dates, newest results, or cycle prefixes.
It has three classes: `permanent` (only the `knowledge/` base), lifecycle
`infrastructure`, and session `task` state (`work/` and every cache). For
mid-session housekeeping use `artifacts-clean-plan`, then
`artifacts-clean CONFIRM=<digest>`, and `artifacts-check`: it keeps knowledge,
infrastructure, session work and caches, protects runtime-bound results, and
discards all other safe output. Every finished session ends with `artifacts-close-plan`, `artifacts-close CONFIRM=<digest>`
and `artifacts-close-check`: it refuses while any runtime, recovery state,
invalid knowledge record or entry outside `.artifacts/fork-maintenance/`
remains, and then leaves only `knowledge/` plus idle lock files. Both are
deliberate evidence disposal, not acceptance or a way around job removal. They
do not stop processes, remove Podman objects, or promote old results. Complete
the intended evidence review before invoking them: deleting a named result
ends its reuse window. Durable experience goes into the distilled session
record, never into preserved logs, reports or archives. Follow the
[artifact runbook](fork-maintenance/docs/runbooks/artifacts.md) and the
[session runbook](fork-maintenance/docs/runbooks/session-close.md) for
confirmation, protection reports and interrupted-cleanup recovery.

Do not create tracked `evidence/`, `runs/`, `results/`, or `communications/`
trees, and never store a case as a patch file. Git history stores automation,
case commits, tests, and contracts—not the results of running them. Cleanup
acts only on exact owned runtime objects after review; it never touches case
commits, case directories, or unrelated Podman objects.

## Git and publication authority

- Case commits are the storage of case work: the agent creates, fixes up,
  squashes, drops, splits, and rebases them itself as part of the task, only
  in the `develop` checkout (see
  [commit authority](fork-maintenance/docs/runbooks/case-commits.md#commit-authority)).
  This includes the refresh runbook's local `develop` rebase onto existing
  local `master` with conflict continuation or abort.
- Control commits follow the operator's instructions. Without one, control
  work stays uncommitted for review; it survives every case rewrite through
  `--autostash`. Every other Git mutation requires an explicit operator
  request; the operator performs it directly or delegates it to the agent.
- Every commit the agent creates or replays is unsigned: pass
  `-c commit.gpgsign=false` to that one Git command (`commit`, `rebase`,
  `rebase --continue`, `cherry-pick`). The operator's global configuration signs
  with a hardware security token which an agent cannot operate; never change
  that configuration and never ask for the token.
- Agent commits and pull-request texts never name an agent as author or
  co-author: no `Co-Authored-By:` line for an AI agent and no "generated with"
  note ([agent commits](fork-maintenance/docs/runbooks/case-commits.md#agent-commits)).
  Automation never lazily fetches from the credentialed promisor of a partial
  clone; missing objects are backfilled credential-free by
  `make -C fork-maintenance objects-backfill` (run inside `develop-rebase`).
- Neither case work nor refresh authorizes a preservation commit, a revert
  commit for a case, another branch or a worktree, a fetch, branch switch,
  local-master update, remote/configuration change, or publication.
- The agent never pushes. Any case rewrite changes `develop` history, so the
  operator publishes `develop` with `--force-with-lease` whenever needed
  ([publication runbook](fork-maintenance/docs/runbooks/publish-develop.md)).
- The scheduled `master-sync.yml` service identity may fast-forward only the
  existing fork `master` ref; agent dispatch requires an explicit request.
- The manual `deb-packages.yml` service identity may create only its unique
  draft, ordinary release, package tag, and two validated tar assets. The
  release title is exactly its Debian version and `prerelease` is false. A
  failed attempt validates its just-created release, deletes the exact tag
  first only while it still targets the dispatched commit, verifies tag
  absence, and deletes that immutable release ID last. A retry may apply the
  same tag-first/release-last rollback to an exact draft left by an earlier
  failed attempt of the same workflow run, but only after validating that
  attempt, its transaction marker, assets, release ID, and unchanged tag
  target. After one ordinary release is verified, it may retain the three
  newest exact owned DEB releases and delete each older exact owned release in
  tag-first/release-ID-last order. An exact published release left by a failed
  or cancelled prior attempt may resume only that retention transaction;
  drafts, unrelated or manual releases, tag-only state, and ambiguous state
  are preserved.
  Agent dispatch requires an explicit publication request.
- Pull-request operations and default-branch changes require separate explicit
  operator instructions.
- Local read-only Git inspection is part of ordinary investigation. Network
  Git operations and ref changes other than the `develop` case rewrites above
  require an explicit operator request; remote URL spelling is not an
  acceptance gate.
- The operator reviews results and performs publication or explicitly
  delegates the exact operation to the agent.

When handing off, show exact status, embedded-source/master/develop commits,
the case map (`case-list`) and `develop-check` result, whether `develop` was
rewritten and needs a `--force-with-lease` publication, validation completed,
remaining validation, and resolved commands requiring separate operator
authorization. Do not claim results that exist only in an old log.
