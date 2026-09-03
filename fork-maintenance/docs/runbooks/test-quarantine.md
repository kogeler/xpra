# Maintain The Upstream Test Quarantine

## Scope

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
Use its isolated workspace so host source remains untouched:

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
provenance. Never edit `patch_sha256` or `paths` manually. Without
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

After every operator-selected upstream rebase and before applying the
quarantine patch to the new base, run all three gates against clean production
and clean tests. Merely observing that a master ref advanced does not trigger a
rebase or block testing the existing `develop` queue.

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
also proved green. If a module passes in an assigned leg, run it directly to
confirm and remove that gate assignment. Remove its disabling change and union
entry only after it has no remaining gate assignments. If all entries are
fixed, retire the case from `stacks/develop.toml` and remove the case in the
same reviewed change; do not keep an empty placeholder or history archive.

Complete this reassessment after every upstream rebase even when the quarantine
patch and every production patch needed no textual refresh. A new failure from
the patched full matrix is not quarantine authority by itself: rerun the exact
module on the clean rebased source in the same mode and admit it only when that
control reproduces the same author-owned failure.

## Patched acceptance

After the clean reassessment, run the case focused gate with the patch applied,
then all three full stack legs:

```bash
make -C fork-maintenance test-start \
  CASE=upstream-test-quarantine PATCH_MODE=patched \
  TARGET=focused RUN=rebase-quarantine-patched-01

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
