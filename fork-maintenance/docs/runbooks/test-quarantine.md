# Maintain The Upstream Test Quarantine

## Scope

The duty is currently inactive, but its infrastructure is permanent. Preserve
`cases/upstream-test-quarantine/` with its manifest, README and zero-byte
`fix.patch`, and retain the supporting gates and this runbook. Never delete
them because no upstream tests are broken. With no active assignments, record
reassessment as not applicable and do not invoke its case gates. Do not restore
old skips just to activate the case; all production and final upstream-suite
gates remain mandatory.

`cases/upstream-test-quarantine/` is the only duty patch for upstream unit-test
modules that are reproducibly non-green in the fork's frozen Ubuntu 26.04
matrix. It may disable tests only. Never put a production workaround, a fork
regression, or an unrelated cleanup in this case, and never hide a foreign
failure inside a production case.

Its slug and `kind = "test-quarantine"` are one reserved identity. No other
case may adopt that kind, and `upstream-test-quarantine` may not be rewritten
as production, even while `ALLOW_PATH_CHANGE=1` admits a module-union update.

The quarantine unit is a complete `unit.*` module. This matches Xpra's
`--skip-fail` boundary and remains stable when the methods that fail differ
between matrix legs. `[quarantine].modules` is the authoritative ordered union;
every entry maps exactly to one changed `tests/unittests/<module>.py` path.
`[quarantine.gates]` has exactly `quarantine`, `quarantine-cython`, and
`quarantine-no-compat`. Each value is an ordered unique subset of the union,
preserves union order, and the three subsets together must name every module.
Use a narrow subset when a failure exists in only one build mode; never disable
that module in a leg where the clean test is green.

## Permanent inactive state

Use the existing draft mechanism, not an empty completed case or a new schema:

- keep `schema = 1`, `draft = true`, the reserved slug and kind, and meaningful
  title and commit subject;
- retain `fix.patch` as exactly zero bytes, `patch_sha256 = ""`, and empty
  `dependencies` and `paths` arrays;
- retain `[tests]`, `[quarantine]`, `[quarantine.gates]` and `[evidence]`, with
  empty test/module inventories, all three empty gate-assignment arrays and
  empty `required_gates`;
- keep the `"upstream-test-quarantine"` queue entry commented in
  `stacks/develop.toml` and the full/quarantine gate names commented inside the
  manifest arrays. Each comment must explain that activation is only for
  currently broken upstream tests with clean-source proof;
- keep a current README explaining ownership, admission, activation,
  deactivation and validation. Do not preserve obsolete modules or disabled
  test bodies in the empty patch as a history archive.

`make -C fork-maintenance list` reports this scaffold as a draft; completed-case
loading and stack snapshots exclude it. Direct case test selection and an uncommented stack
reference to the draft must fail, not accept a zero-test run. The directory
must survive both upstream refresh and ordinary cleanup when inactive. The
production-case retirement procedure never authorizes deleting it.

## Admission

Before adding a module:

1. freeze and record the source embedded in current `develop` with
   `isolated-start-check`;
2. reproduce the module failure on that clean source in every affected matrix
   leg;
3. confirm that build/setup completed and that the failure belongs to the
   named module;
4. compare the exact canonical Actions run when available;
5. obtain explicit scope to quarantine rather than repair the foreign test.

Update the existing duty case; do not create one quarantine case per module.
When it is inactive, do not call `case-new` or remove `draft = true` by hand.
Keep the queue reference commented. Populate `tests.list` with the complete
current module union and uncomment its three full legs, set the per-leg
assignments, uncomment all three `required_gates`, and document the current
failure boundaries in the case README. Leave the blank derived fields alone.
Then use the supported draft-workspace path:

```bash
make -C fork-maintenance workspace-create \
  CASE=upstream-test-quarantine \
  WORKSPACE=quarantine-activate-01 PATCH_MODE=clean
# edit only the admitted upstream test modules below the printed source path
make -C fork-maintenance workspace-stage WORKSPACE=quarantine-activate-01
make -C fork-maintenance workspace-update WORKSPACE=quarantine-activate-01
```

The export validates the nonempty candidate against the completed quarantine
contract, derives the patch/digest/paths, removes the draft marker and updates
workspace provenance atomically. Only after successful promotion uncomment the
queue reference and update the active-case inventory checks and documentation.
Then follow the clean reassessment and patched acceptance below. An isolated
workspace is not a runtime or acceptance result.

For an already active duty, use its patched workspace so host source remains
untouched:

```bash
make -C fork-maintenance workspace-create \
  CASE=upstream-test-quarantine \
  WORKSPACE=rebase-quarantine-edit-01 PATCH_MODE=patched
# edit only the listed upstream test modules below the printed source path
make -C fork-maintenance workspace-stage WORKSPACE=rebase-quarantine-edit-01 \
  ALLOW_PATH_CHANGE=1
make -C fork-maintenance workspace-update WORKSPACE=rebase-quarantine-edit-01 \
  ALLOW_PATH_CHANGE=1
```

Update `tests.list`, `[quarantine].modules`, every `[quarantine.gates]` subset,
and the case README before export. During this short admission interval the new
human-authored union may not match the old patch's automation-owned `paths`.
That is why both stage and update above use `ALLOW_PATH_CHANGE=1`: the tool
still validates the old patch against its exact old `patch_sha256` and paths,
rejects this relaxation for a production case, and requires the staged
candidate to equal the complete new module-derived path union. It then derives
and publishes the new patch, digest, and paths atomically with workspace
provenance. Never edit active `patch_sha256` or `paths` manually. Without
`ALLOW_PATH_CHANGE=1`, the mixed manifest must fail closed. Recover an
interrupted export only with `case-recover CASE=upstream-test-quarantine`; its
transaction completes the exact recorded old/new pair rather than accepting a
partially published patch and manifest.

The case-update owner durably records whether that exact path transition was
admitted. Owner-only and pre-marker abort recovery may use the authority only
while the published old patch and manifest remain a structurally valid,
genuinely path-mismatched quarantine transition. The removal phase carries the
same authority across an interrupted abort; a completed transaction returns to
ordinary strict case validation.

Recovery also accepts the exact older schema-1 owner, transaction, and removal
field sets that predate this authority bit, interpreting its absence only as
`false`. Owner, transaction, and removal records may not mix old and current
forms; an extra field or any other missing field fails closed.

## Mandatory reassessment after an explicit upstream refresh

After every operator-selected upstream rebase, first complete the whole-queue
manual-review exit gate in [upstream refresh](upstream-refresh.md). This
includes reading the duty patch, each disabled test and its current production
subject, and recording the rationale and clean verification plan for every
per-leg assignment. Do not use quarantine runs to bypass review of other
patches or infer empirical failures from code reading alone.

Then, before using the quarantine patch in runtime validation on the new base,
run all three gates against clean production and clean tests. Isolated
application during manual review or export does not certify an assignment.
Merely observing that a master ref advanced does not trigger a rebase or block
testing the existing `develop` queue.

Follow [`validation.md`](validation.md): reassess once for the actual source,
image/environment, module union, and per-leg expectations, reusing current
collected proof while those inputs remain unchanged. After the review gate,
do not block independent reviewed production-case tests on unrelated quarantine
results or repeat clean quarantine for an unrelated production-only edit.
Any changed input requires its affected gate proof before
the quarantined stack can be accepted.

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

Wait for and review every named job. Each gate runs every module in the union,
but supplies `--skip-fail` only for that gate's declared subset. It passes only
when the successful count equals the complement, failed and skipped counts are
zero, and the exact ordered ignored-failure set equals that subset. An expected
failure which passes is stale; a complement failure means the manifest's leg
mapping is incomplete. Neither is permission to weaken the checker.

If all three clean gates are green, every declared per-leg failure remains
required in the frozen environment and every deliberately unassigned leg has
also proved green. If a module passes in an assigned leg, the same named gate
confirms it directly without `--skip-fail`, reusing the clean source and
upstream incremental build/install in the identical build mode. This repeat
selects only now-green assigned modules after the first union summary passes
structural, contamination and accounting checks. Its log binds the exact
executed inventory and requires those modules to pass without ignored or
skipped modules. A failed repeat is unresolved, not permission to remove a
skip. Even a successful repeat leaves the named gate **failed as stale**:
review both outputs and remove the obsolete assignment. Do not invoke the
unsupported clean focused mode or weaken the expected-failure checker.
Remove its disabling change and union entry only after it has no remaining
gate assignments. If all entries are fixed, deactivate the duty through the
following section. Remove the obsolete disabling changes, never the permanent
case infrastructure.

Complete this reassessment after every upstream rebase even when the quarantine
patch and every production patch needed no textual refresh. A new failure from
the patched full matrix is not quarantine authority by itself: rerun the exact
module on the clean rebased source in the same mode and admit it only when that
control reproduces the same author-owned failure.

## Deactivate the last assignment without deleting infrastructure

First review every obsolete assignment's current clean/direct confirmation and
record the conclusion in the ignored cycle ledger. No still-failing assignment
may be cleared merely to obtain a green matrix. Finish collection/removal of
jobs bound to the old inputs, resolve any interrupted case update through
`case-recover`, and remove finalized workspaces through their normal lifecycle
before resetting their case. Preserve any unexported work; do not hand-delete
recovery state or runtime objects.

In one reviewed maintenance change:

1. Comment the duty's `series` entry in `stacks/develop.toml` and any other
   active TOML selection reference, retaining an explanation to enable it when
   current broken upstream tests need quarantine. Keep the commented reference,
   not an active dependency on a draft.
2. Restore `draft = true`, empty `fix.patch` to exactly zero bytes, reset
   `patch_sha256 = ""` and `paths = []`, clear the module/test inventories and
   all three gate assignments, and comment the gate names as in the permanent
   inactive state above. Preserve the manifest tables and README. This is the
   only manual blank-derived-field reset: it publishes no active patch. Do not
   pass an empty candidate to `workspace-update`, which correctly rejects it.
3. Update active-case lists/checks and the README's current status without
   removing the permanent scaffold check. Resolve the remaining stack and run
   the offline fork-control checks. No case-specific runtime command may select
   the draft; the resulting full three-leg matrix must run without the removed
   disabling changes. All production acceptance requirements remain unchanged.

Do not run selection or build commands midway through this edit. There is no
automatic deactivation target and no new commit authority. The tracked files
must end in the complete inactive state together. Historical clean results
remain evidence only while their exact retained records remain available; the
empty patch itself proves nothing about upstream test health.

## Patched acceptance

After the clean reassessment, run the case focused gate with the patch applied:

```bash
make -C fork-maintenance test-start \
  CASE=upstream-test-quarantine PATCH_MODE=patched \
  TARGET=focused RUN=rebase-quarantine-patched-01
```

The complete stack still requires all three full legs for final acceptance.
Wait for the reviewed candidate freeze and fill only missing or invalidated
results; do not start this matrix after each intermediate duty-case edit:

```bash
make -C fork-maintenance test-start \
  STACK=develop PATCH_MODE=patched TARGET=full RUN=rebase-full-01
make -C fork-maintenance test-start \
  STACK=develop PATCH_MODE=patched TARGET=full-cython RUN=rebase-full-cython-01
make -C fork-maintenance test-start \
  STACK=develop PATCH_MODE=patched TARGET=full-no-compat RUN=rebase-full-no-compat-01
```

The quarantine case owns no production or live behavior, so adding or removing
only its test paths does not replace the production cases' focused, native, or
live gates. The final full matrix must nevertheless use the current complete
stack and be green.
