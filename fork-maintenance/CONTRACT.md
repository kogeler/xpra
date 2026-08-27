# Xpra Fork Maintenance Contract

## Purpose

This directory makes the user's Xpra fork maintainable as an ordered,
testable downstream patch queue. It keeps canonical Xpra source, fork-only
automation, patch development, isolated testing, and physical-GPU validation
in one repository without allowing fork changes onto `master`. It also keeps
fork GitHub CI as a minimal caller of this tracked automation.

## Repository identities

The enclosing Git repository is the only source checkout. It has exactly these
remote roles:

- `origin`: `https://github.com/kogeler/xpra.git`;
- `upstream`: `https://github.com/Xpra-org/xpra.git`;
- canonical base: live, verified `upstream/master`.

There is no second linked Git worktree and no replaceable canonical checkout.
The isolated workflow may create a private detached copy of one verified
master commit below `.artifacts/fork-maintenance/upstream-tests/workspaces/`.
That generated copy has no fork remote, carries no working-tree overlay, and is
never a source of branch history. Automation resolves the repository root as
the parent of `fork-maintenance/` and fails if that path is not the Git top
level.

## Branch contract

### `master`

Remote fork `origin/master`, cached `origin/master`, cached
`upstream/master`, live `upstream/master`, and local `master` must converge on
the same commit before an upstream refresh is complete.

`repo-sync` fetches only the two master remote-tracking refs, verifies each
against live `ls-remote`, and compares the live commits. It never updates a
remote branch, switches a branch, merges, rebases, resets, commits, or pushes.

When live fork master is stale, only the operator runs:

```bash
gh repo sync kogeler/xpra --source Xpra-org/xpra --branch master
```

The command is requested only after a fresh mismatch and never with
`--force`. A fork master that cannot fast-forward is an owner-review boundary.
After successful remote equality, `master-update` may fast-forward local
`master`; it rejects an ahead or divergent local branch.

Fork-only files, production fixes, tests not intended for upstream, merge
commits, and automation commits are forbidden on `master`. Nothing in this
automation pushes `master`.

### `develop`

`develop` is the persistent fork-maintenance branch and intended default fork
branch. Its committed difference from current master is limited to:

- root `AGENTS.md`;
- the root `.gitignore` runtime boundary;
- `.github/workflows/develop.yml` and the disabled upstream-workflow
  rename boundary below `.github/upstream-workflows/`;
- `fork-maintenance/`.

Production source changes are stored in `cases/*/fix.patch`; their applied
copies are not committed on clean `develop`. Every upstream refresh rebases
the fork-only `develop` commits onto the verified local `master`, followed by
an explicit patch-resolution cycle. Merging `master`, `upstream/master`, or an
equivalent upstream ref into `develop` is forbidden.

Current master must be the linear base of `develop`, and the fork-only
`master..develop` range must contain no merge commits. `develop-rebase`
requires clean `develop`, freshly verifies fork/canonical master equality,
requires local `master` at that exact commit, and runs a local rebase. If Git
stops on conflicts, patch work remains blocked until every conflict is resolved
and the rebase completes.

A published `develop` is intentionally rewritten by later refreshes. Agents
and automation never publish that rewrite. The operator may do so only with an
exact expected remote SHA and `--force-with-lease`; plain `--force` is
forbidden.

`develop-check` rejects a dirty checkout, stale local master, committed source
copies of queue patches, an unresolvable active stack, or a missing ignore
boundary. It remains the publication boundary, not the pre-commit isolated
investigation boundary.

`isolated-start-check` is the pre-commit investigation boundary. It requires
the checked-out branch to remain `develop`, fetches and verifies live fork and
canonical master equality, rejects dirty or committed Xpra source changes, and
permits local changes only in `AGENTS.md`, `.gitignore`, the controlled
`.github/` CI paths, and `fork-maintenance/`. It records the branch, HEAD,
worktree status, and exact master commit and must not change any of them except
the two cached remote tracking refs it fetches.

### Temporary branches

A clean non-master branch may be used for isolated patch development. It must
be descended from the fully rebased local `develop`, contain current verified
master, and have no committed changes on the selected case paths. The same
patch commands work on `develop` and temporary branches. Parallel worktrees
are outside the supported model.

## Patch case contract

Each completed `cases/<slug>/` is either a production case or the single
`kind = "test-quarantine"` duty case. It contains:

- `case.toml`: stable identity, title, commit subject, patch digest,
  dependencies, owned paths, focused tests, and required gates;
- `fix.patch`: one atomic binary-capable Git patch containing production code
  and its regression tests, or only quarantined upstream test modules;
- `README.md`: current failure boundary, patch ownership, and required
  validation;
- optional `tests/`: case-owned functional probes.

The manifest schema retains the `[evidence]` table name for runner
compatibility, but `required_gates` is only a declarative validation list. It
does not authorize tracking reports or results.

Every patch must satisfy all of these conditions:

- SHA-256 equals `patch_sha256`;
- changed paths equal the manifest `paths` exactly;
- no path is absolute, traverses a parent, or enters `fork-maintenance/`;
- it passes `git apply --whitespace=error-all`;
- after application it passes an exact reverse check;
- every new downstream-authored source or test file carries
  `Copyright (C) <current-year> kogeler` in its native comment syntax; copied or
  derived content retains required existing notices and adds the `kogeler` line;
- a production case owns one behavior and includes at least one focused
  `unit.*` test;
- the test-quarantine case has no dependencies, changes only
  `tests/unittests/<unit-module>.py`, and binds every changed path to the exact
  module named in `[quarantine].modules`;
- the test-quarantine case requires all three clean `quarantine`,
  `quarantine-cython`, and `quarantine-no-compat` gates.

There is exactly one active test-quarantine case. It is an explicit temporary
exception, never a production fix or a place for unrelated test repair. Each
entry requires a current clean-master failure in the frozen matrix. After
every upstream rebase, the clean quarantine gates invert the usual result:
they pass only when every listed module remains an exact ignored failure. A
passing module makes the case stale and must be removed or narrowed before the
quarantine patch or full patched matrix is accepted.

`case-new` creates a deliberately incomplete `draft = true` case. Drafts are
not test-selectable. Complete the human-authored fields, create a clean draft
workspace, and stage the whole candidate there; `workspace-update` removes the
draft marker and derives the digest and paths atomically without changing host
source. The clean host `patch-update` fallback can also promote a draft after
its publication start gate. Derived fields are never edited by hand.

Only these active cases are retained:

1. `wayland-initial-window-state`;
2. `video-pipeline-cleanup-race`;
3. `upstream-test-quarantine`.

## Stack contract

`stacks/develop.toml` is the complete active queue in dependency/application
order. A stack contains unique known cases, places selected dependencies before
their consumers, and declares the union of integration gates.

The resolver evaluates each patch against an isolated snapshot in sequence:

- `apply`: forward check succeeds and reverse check fails;
- `already-present`: forward check fails and exact reverse check succeeds;
- `ambiguous` or `diverged`: both or neither succeeds, so the operation stops.

An already-present patch remains named in resolution provenance until its case
is deliberately retired. A divergent patch is refreshed; it is never applied
with rejects, fuzz workarounds, source rewriting, or whitespace relaxation.

## Isolated patch lifecycle

The default pre-commit patch cycle never applies a production patch to the host
worktree. `workspace-create` makes a no-clobber named directory under the
ignored private runtime root, copies the complete verified master commit into
a detached local checkout, removes its remote, resolves the selected case or
stack, and applies either the complete patch, only its retained tests, or
nothing. The copied tree and its private index are generated runtime state.

A new draft case uses `PATCH_MODE=clean`. Its initially empty patch is not
resolved or applied; edits are made only in the detached workspace. Promotion
validates the completed manifest, derives the patch and owned paths, and
updates workspace provenance to the new completed case.

An atomic case workspace may be edited and staged with `workspace-stage`.
`workspace-update` requires the same host branch and HEAD, a complete staged
candidate, no untracked or unstaged workspace output, exact path ownership
unless explicitly expanded, and deterministic forward/reverse application to
the recorded master commit. It then atomically replaces only that case's
`fix.patch`, `patch_sha256`, and `paths`. It never stages the host index or
edits inherited Xpra source in `develop`.

`PATCH_MODE=tests-only` applies only paths below `tests/` from the selected
patch. This is the durable non-vacuous clean-master regression mode: retained
tests execute against unmodified production code. A tests-only workspace can
never replace a complete case patch.

Workspace cleanup validates the exact name and ownership record and removes
only that generated directory. An identity is never reused.

Final cleanup of a complete work cycle uses a common lowercase prefix for all
of its `RUN`, `IMAGE_RUN`, and `WORKSPACE` identities. `cycle-clean-plan`
requires transient job ownership to have been collected and removed, verifies
collected log/report hashes, and proves every matching workspace has no
unexported candidate and is exactly represented by the current patch queue. It
prints an exact target set and its confirmation digest. `cycle-clean` rebuilds
that plan and removes it only when the supplied digest still matches.

## Host patch lifecycle

Host patch application, refresh, or integration diagnosis starts with the
clean publication refresh contract below. `patch-start-check` then re-verifies
live fork/canonical equality, exact local master, linear rebased `develop`, and
the ancestry of any temporary branch. Committed versions of all patch-owned
source paths must match current master. Pre-commit investigation and acceptance
instead start with `isolated-start-check` and use the isolated lifecycle above.

`patch-apply` or `stack-apply` applies only resolver entries in `apply` state
with `git apply --index --whitespace=error-all`. It stages source and test
files, verifies the exact staged path set, and rejects unstaged or untracked
output. Partial failure reverses every successfully applied case in reverse
order. A failed rollback is reported as a hard dirty-state boundary.

For development, edit the applied files and stage the complete candidate.
`patch-update` requires no unstaged or untracked files, checks the exact path
contract, proves the new full patch applies and reverses on current master, and
atomically replaces `fix.patch`, `patch_sha256`, and `paths`. It leaves the
source candidate staged for review.

`patch-unapply` or `stack-unapply` requires that the staged paths exactly equal
the effective queue. It reverses in dependency-safe order and restores the
committed source. After `patch-update`, changes to only that case's
`case.toml` and `fix.patch` may remain unstaged while unapplication runs.

The normal committed result on `develop` is the maintained patch and its
metadata, not the applied source copy.

## Publication refresh contract

Before committing or publishing a refreshed `develop`, use this clean sequence:

1. require a clean checkout;
2. run `repo-sync`;
3. when required, wait for the operator's non-forced fork sync and rerun the
   gate;
4. fast-forward local `master` with `master-update`;
5. switch to `develop` and run `develop-rebase`;
6. resolve and stage every conflict, then use `git rebase --continue` until the
   rebase completes; abort and stop if correct resolution is not possible;
7. run `patch-start-check` and resolve the complete active stack against new
   master;
8. run every clean quarantine gate and remove or narrow entries that no longer
   fail on this exact master;
9. confirm that isolated run provenance and patch digests still bind this exact
   master, apply the non-semantic exception below, or rerun the affected gates;
10. run any remaining focused, native, full, and required live gates;
11. run `develop-check` before handoff.

No form of `git merge master`, `git merge upstream/master`, or an equivalent
merge is accepted as upstream transfer. `develop-check` rejects merge commits
above current master even when current master is technically an ancestor.

Pre-commit investigation may precede this clean publication refresh only via
`isolated-start-check`; its outputs are bound to the verified master commit and
patch digests. Old reports and previous successful resolution are not proof
after the master or executable/test semantics change. A digest-only change may
reuse the prior functional result solely under the non-semantic validation
exception below. Do not delete an active patch merely because nearby upstream
code looks equivalent. Review the exact production path and run the retained
tests-only regression first. Once a patch is proven exactly present or fully
replaced, remove it and update the stack in one reviewed change; do not create
a tracked history archive.

## Source and runner provenance

Local upstream-test and live acceptance runners freeze verified live
`upstream/master`, never the mutable develop working tree. Hosted CI instead
uses cached checkout `origin/master` to locate the merge base already embedded
in pushed `develop` and freezes that commit; a later fork-master advance does
not change the source under test. It does not query moving live master refs or
create an `upstream` remote during the job. Each run binds:

- exact canonical commit and workflow digest;
- selection manifest and patch digests;
- base-aware resolution and its digest;
- runner/build-input digests;
- image identity and toolchain versions;
- exact target or live profile;
- final process/container state and complete log digest.

Build contexts contain a credential-free source archive plus the selected
patch inputs. They exclude `.git`, remotes, credentials, global configuration,
ignored paths, and unrelated working-tree state.

## GitHub CI contract

Canonical Xpra workflow files are never executed on `develop`. Every
`.yml`/`.yaml` file from current
`upstream/master:.github/workflows/` is relocated without content changes to
the same relative name below `.github/upstream-workflows/`. The executable
directory contains exactly `.github/workflows/develop.yml`.
`ci-layout-check` compares the disabled files byte-for-byte with current master,
rejects a missing, extra, edited, or still-active upstream workflow, and checks
the exact thin fork workflow interface. After a rebase, upstream edits are
resolved as rename/modify updates; any new canonical workflow is relocated in
the same cycle before publication.

`ci-layout-check` is an explicit rebase and publication audit. `ci-prepare`
does not invoke it: once GitHub has selected the workflow, a self-check of its
tracked filename or layout must not prevent the upstream test matrix from
starting.

The fork workflow:

- triggers only on a push to `develop`;
- uses the GitHub-hosted `ubuntu-26.04` runner and read-only contents permission;
- pins each external action to a reviewed full commit SHA and records the
  corresponding current release version in an inline comment;
- declares the exact `full`, `full-cython`, and `full-no-compat` matrix with
  fail-fast disabled and `max-parallel: 3`;
- performs checkout and, for each matrix value, invokes exactly
  `make -C fork-maintenance ci-upstream-tests` with that value in
  `XPRA_CI_TARGET`.

No dependency installation, Podman command, patch logic, test command, dynamic
test discovery, fallback, skip policy, or cleanup implementation belongs in
YAML. The fixed matrix is only hosted-runner fan-out; Make validates its value.
Each job derives the `develop`/checkout-`origin/master` merge base, freezes that
already embedded commit without a live remote query, applies the complete
`stacks/develop` queue inside the container, and runs exactly its selected leg.
The three jobs run independently, and one failure does not cancel the remaining
legs. The CI path never fetches, syncs, switches, merges, or rebases. Live
fork/canonical equality and the actual rebase remain mandatory pre-publication
operator steps, not moving hosted-job dependencies. Here "never fetches"
describes the tracked Make/Python path after `actions/checkout` has populated
the checkout.

CI never invokes a `live-*` target and cannot satisfy RGB, render-node,
Wayland hardware-H.264, Vulkan, input, detach, or transport-loss acceptance.
Those profiles require the local physical environment. The hosted Actions job
is the outer foreground lifecycle and log owner for `ci-upstream-tests`; local
acceptance jobs continue to use unique named runner lifecycles and local
evidence.

## Validation contract

For one patch, validate in increasing scope:

1. clean-master failure or another non-vacuous focused regression;
2. selected focused tests;
3. affected native/subsystem checks;
4. all three clean quarantine reassessment gates when the active queue contains
   the duty quarantine case;
5. `full`, `full-cython`, and `full-no-compat`;
6. each live gate declared by the production cases and complete stack.

A proven non-semantic refresh does not restart this ladder. It requires the
same verified master plus an exact old/new applied-tree diff limited to
comments, copyright notices, or documentation. Paths, modes, executable data,
configuration, test assertions, source selection/application, build commands,
runner behavior, and live assertions must remain unchanged. Refresh derived
digests, resolve the current queue, run whitespace checks and the affected
fork-control tests, and describe the proof at handoff. Do not spend container,
native, full-matrix, or live resources on that refresh. If any condition is
uncertain, the exception does not apply and the normal ladder is required.

Ordinary acceptance is green. A pre-existing failure outside the selected
paths is investigated against canonical CI before any costly local clean
control. It is never skipped, weakened, reconfigured, or fixed inside a
production case. With explicit user scope, an exact current clean-master
failure may be added only to the duty quarantine case and must then satisfy its
reassessment gates. No exception is implied by old lab results.

A failure before a downstream test target starts is a control-plane failure,
not a failed Xpra test. When the change only removes or narrows that pre-test
guard, validate it with the focused automation unit test and the exact direct
preflight command. Do not run the expensive downstream matrix if canonical
master, selection and patch digests, image inputs, container entrypoint, and
test commands are unchanged: those tests cannot validate whether the removed
guard still blocks them. Any change to a downstream input or execution path
restores the normal validation ladder.

The live runner keeps direct Xpra boundaries distinct from SSH orchestration.
It owns RGB, adaptive Wayland hardware H.264, real detach, direct TCP transport
loss, and multi-window Vulkan/input profiles. Foreground probes are diagnostic;
named supervised jobs are the acceptance path.

## Runtime storage contract

All generated state lives below repository-level:

```text
.artifacts/fork-maintenance/
```

This includes source archives, build contexts, image-build records, test and
live logs, reports, screenshots, status files, checksums, local publication
text, virtual environments, and caches. The root `.gitignore` must ignore the
entire `.artifacts/` tree.

No result is copied back under `fork-maintenance/`. In particular, tracked
`evidence/`, `runs/`, `results/`, and `communications/` directories are
forbidden. Git history records inputs and reproducible automation, never run
outputs.

Private state creation fails closed on symlinks, wrong ownership, or unsafe
permissions. Long jobs use unique validated `RUN` names; image builds use
unique `IMAGE_RUN` names. An identity is never reused for a retry. Cleanup
requires exact ownership and removes only reviewed run-owned transient objects.

The public lifecycle interface is the root `fork-maintenance/Makefile`.
Operators and agents never signal recorded process groups or call destructive
Podman commands directly for a named job. Make targets validate the owner
record, PID plus kernel start ticks and process group for host jobs, immutable
container identity and labels for container jobs, and the result boundary
before aborting or removing anything. Complex lifecycle logic belongs in
tracked Python helpers; Make remains the public orchestration interface.
Collection and acceptance require the currently tracked runner and supervisor
digests. Status, abort, and removal may use an older recorded digest only to
inspect or clean an already-owned job after automation changed; that path can
never promote new acceptance evidence.

GitHub workflow files also invoke only that public Makefile. The dedicated
foreground CI target is not a general local lifecycle shortcut: GitHub Actions
provides its timeout, cancellation state, and complete console log, while the
tracked Python helper still verifies and owns each image build and Make owns
each disposable `podman run --rm` invocation.

Collected results and finalized workspaces are removed only by the
digest-confirmed cycle cleanup flow. It never stops active work and rejects
remaining owner records, locks, owned processes, Podman containers or networks,
incomplete evidence, changed fingerprints, and unfinished workspace
candidates. Git-originated relative symlinks inside an owner-bound result tree
are fingerprinted without being followed; absolute or lexically escaping
targets are rejected. Because the result root itself is owned mode `0700`,
owned group-writable input files are fingerprinted; other-writable or
hard-linked files remain forbidden. Content-addressed source archives, build
contexts, images, ccache, and virtual environments are reusable state and
remain outside ordinary cycle cleanup.

## Authority boundary

The automation may verify/fetch refs read-only, fast-forward local `master`,
perform an explicitly invoked local `develop` rebase, create and remove exact
owned isolated workspaces, remove a digest-confirmed finalized artifact cycle,
update case files from a verified workspace, apply or remove patches in a clean
non-master host worktree, run tests, and print status.

No target creates a new content commit automatically. `develop-rebase` only
replays existing fork-only commits onto verified master and therefore changes
their local identities. The automation never pushes, force-updates or mutates
remote refs, runs `gh repo sync`, creates or edits a pull request, changes the
default branch, or changes global Git configuration. An agent creates a new
commit only after explicit current-conversation authorization. The operator
reviews and performs all remote publication and default-branch actions.
