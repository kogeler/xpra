# Maintain The Upstream Test Quarantine

## Scope

`cases/upstream-test-quarantine/` is the only duty patch for upstream unit-test
modules that are reproducibly non-green in the fork's frozen Ubuntu 26.04
matrix. It may disable tests only. Never put a production workaround, a fork
regression, or an unrelated cleanup in this case, and never hide a foreign
failure inside a production case.

The quarantine unit is a complete `unit.*` module. This matches Xpra's
`--skip-fail` boundary and remains stable when the methods that fail differ
between matrix legs. `[quarantine].modules` is the authoritative set; every
entry maps exactly to one changed `tests/unittests/<module>.py` path.

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

Update `[quarantine].modules`, `tests.list`, and the case README before export.
`patch_sha256` and `paths` remain automation-derived. Review that each changed
file is the exact module named by the manifest.

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

Wait for and review every named job. Each gate passes only when its build
completes and the ignored-failure set is exactly `[quarantine].modules`: no
listed module may pass, and no unlisted module may fail. A gate failure saying
the quarantine is stale is a removal signal, not permission to weaken the
checker.

If all three clean gates are green, the quarantine is still required in the
frozen environment. If a module passes in any leg, run it directly to confirm,
then remove or narrow its disabling change and manifest entry. If all entries
are fixed, retire the case from `stacks/develop.toml` and remove the case in
the same reviewed change; do not keep an empty placeholder or history archive.

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
