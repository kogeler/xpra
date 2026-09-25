# Upstream test quarantine

Code: the `Fork-Case: upstream-test-quarantine` commit on `develop`
(`make -C fork-maintenance case-show CASE=upstream-test-quarantine`).

## Status and lifetime

The quarantine is currently inactive: there are no assigned upstream test
modules and no `Fork-Case: upstream-test-quarantine` commit exists on
`develop`. The manifest keeps its `[tests]`, `[quarantine]`,
`[quarantine.gates]` and `[evidence]` tables with an empty `tests.list`, empty
`[quarantine].modules`, three empty gate arrays and empty `required_gates`.
The inactive case is not selectable; its gates are enabled only when currently
broken upstream tests need quarantining.

This directory is permanent fork infrastructure and the one exception to the
rule that a case directory exists only while its commit exists (see
[case commits](../../docs/runbooks/case-commits.md#model)). Never delete it,
its manifest, README, or the supporting quarantine gates and runbook just
because all upstream tests are green. Retire obsolete disabling changes, not
the quarantine mechanism. Do not retain historical skips or old module
assignments in the inactive manifest. Git history, not this directory, owns
their history.

## Boundary and ownership

This is the single reserved `kind = "test-quarantine"` duty case, not a
production fix. It may disable only upstream unit-test modules whose failures
have been reproduced on the source embedded in current `develop`, in the
frozen Ubuntu 26.04 matrix. A failure seen only with the downstream case
commits applied is not quarantine authority. Setup, compiler, dependency,
runner and fork regression failures must be diagnosed at their actual
boundary.

The case must have no dependencies and its commit must never change `xpra/`,
packaging, fork regressions or unrelated upstream tests. No other case may
adopt the quarantine kind, and this reserved slug must never become a
production case. The maintained
[quarantine runbook](../../docs/runbooks/test-quarantine.md) defines
admission, reassessment, activation and deactivation.

## Active manifest contract

When active, `[quarantine].modules` is the ordered, unique union of affected
`unit.*` modules. Each entry owns exactly its corresponding
`tests/unittests/<module>.py` path, and every entry remains in `tests.list`.
The three `[quarantine.gates]` arrays assign the exact failing subset in each
mode:

| Gate | Build mode | Compatibility |
| --- | --- | --- |
| `quarantine` | Interpreted | Enabled |
| `quarantine-cython` | `cythonize_more` | Enabled |
| `quarantine-no-compat` | Interpreted | Disabled |

Each subset preserves union order; their union must equal `modules`. A subset
may be empty if every listed module passes in that leg. The active case still
requires all three gates. Disable a module only in its failing modes; a
compiled-only defect does not justify hiding a green interpreted test.

The active case is exactly one `Fork-Case: upstream-test-quarantine` commit
which changes exactly the test files of `[quarantine].modules` and nothing
else, and `make -C fork-maintenance case-check CASE=upstream-test-quarantine`
must pass: the commit applies alone on the upstream base and stays removable
from `HEAD`. An empty or placeholder commit and the inactive scaffold are not
a valid active case, a successful clean control, or a way to bypass these
checks.

## Activation and deactivation

Before activation, review current source and test ownership, reproduce the
upstream failure in every affected clean leg, and establish task authority to
quarantine it. Populate the human-authored module, test and gate lists and
document each current failure here. Then disable the admitted modules in the
`develop` checkout and commit exactly those test files as the one quarantine
case commit (see
[add a case](../../docs/runbooks/case-commits.md#add-a-case)):

```bash
git add -- <the quarantined tests/unittests files>
git -c commit.gpgsign=false commit -m "<subject>" -m "<body>" \
  --trailer "Fork-Case: upstream-test-quarantine"
make -C fork-maintenance case-check CASE=upstream-test-quarantine
```

Do not run `case-new`: this reserved directory already exists. While the duty
is active, add, narrow or remove an assignment with a fixup of its commit and
`develop-squash` (see
[change a case](../../docs/runbooks/case-commits.md#change-a-case)), and
update the manifest lists in the same change so that the commit always touches
exactly the listed modules.

When the last assignment becomes obsolete, review the exact clean/direct
confirmations and finish the lifecycle of any job bound to the old inputs
first. Then drop the commit and empty the lists again:

```bash
make -C fork-maintenance case-drop CASE=upstream-test-quarantine
```

Unlike ordinary case retirement, keep the directory: clear `tests.list`,
`[quarantine].modules`, the three gate arrays and `required_gates`, and update
the status above. Preserve this structure and description. The runbook gives
the complete sequence and checks.

## Validation

After every explicit upstream refresh, manually review the duty and its
assignments as part of the incremental whole-stack review before runtime
tests. With the duty inactive, record that there are no assignments, verify
the preserved scaffold with its empty lists and the absence of a quarantine
commit, and omit its case-only runtime commands. Never create an empty or
placeholder quarantine commit just to run quarantine gates.

With the duty active, each clean gate executes the entire module union and
uses `--skip-fail` only for its declared subset. Acceptance requires the exact
ordered ignored-failure set, a passing complement, and no skipped or unignored
failures. A now-green assigned module makes the gate stale; its same-job
direct repeat without `--skip-fail` supplies confirmation to remove that
assignment, not permission to report the old mapping as passing. Remove a
module's disabling change only when no failing-leg assignment remains.

After reassessment, run the case's `PATCH_MODE=patched` focused check and the
complete stack's three full upstream legs on the stable candidate. An inactive
duty does not weaken or replace any production, native, package or
nine-profile complete-stack live requirement. Tests challenge the manual
review; they do not replace it.

Offline fork-control tests protect the retained directory and its empty
inactive state, exclude it from selection while no quarantine commit exists,
reject direct test selection of the inactive case, and retain the active
quarantine manifest checks. Run `make -C fork-maintenance check` and
`make -C fork-maintenance develop-check` after changing this infrastructure.
