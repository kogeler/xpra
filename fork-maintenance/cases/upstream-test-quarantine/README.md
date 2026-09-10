# Upstream test quarantine

## Status and lifetime

The quarantine is currently inactive: there are no assigned upstream test
modules, `fix.patch` is a zero-byte file, and `case.toml` uses the existing
`draft = true` schema. Its reference in `stacks/develop.toml` and its test-gate
entries in this manifest remain commented, with instructions to enable them
only when currently broken upstream tests need quarantining.

This directory is permanent fork infrastructure. Never delete it, its manifest,
empty patch, README, or the supporting quarantine gates and runbook just because
all upstream tests are green. Retire obsolete disabling changes, not the
quarantine mechanism. Do not retain historical skips or old module assignments
in the inactive patch. Git history, not this directory, owns their history.

## Boundary and ownership

This is the single reserved `kind = "test-quarantine"` duty case, not a
production fix. It may disable only upstream unit-test modules whose failures
have been reproduced on the source embedded in current `develop`, in the
frozen Ubuntu 26.04 matrix. A failure seen only with downstream patches applied
is not quarantine authority. Setup, compiler, dependency, runner and fork
regression failures must be diagnosed at their actual boundary.

The case must have no dependencies and must never change `xpra/`, packaging,
fork regressions or unrelated upstream tests. No other case may adopt the
quarantine kind, and this reserved slug must never become a production case.
The maintained [quarantine runbook](../../docs/runbooks/test-quarantine.md)
defines admission, reassessment, activation and deactivation.

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

The active patch must be nonempty, apply and reverse exactly, and match the
automation-derived `patch_sha256` and `paths`. The empty inactive scaffold is
not a valid active case, a successful clean control, or a way to bypass these
checks.

## Activation and deactivation

Before activation, review current source and test ownership, reproduce the
upstream failure in every affected clean leg, and establish task authority to
quarantine it. Populate the human-authored module, test and gate fields and
document each current failure here. Start a `PATCH_MODE=clean` draft workspace;
stage and export the complete test-only candidate using `workspace-update`.
Promotion removes `draft = true` and derives the real patch, digest and paths
atomically. Only then uncomment the queue reference. Do not run `case-new`:
this reserved directory already exists.

When the last assignment becomes obsolete, review the exact clean/direct
confirmations and finish any bound job/workspace lifecycle first. Comment the
queue reference with its reactivation purpose, restore `draft = true`, empty
the patch and manifest inventories, and comment the test-gate entries again.
The narrow inactive reset leaves `patch_sha256 = ""` and `paths = []`; it is
not a manual rewrite of an active patch's derived metadata. Preserve this
structure and description. The runbook gives the complete sequence and checks.

## Validation

After every explicit upstream refresh, manually review the duty and its
assignments as part of the incremental whole-queue review before runtime
tests. With the duty inactive, record that there are no assignments, verify
the preserved scaffold and commented queue reference, and omit its case-only
runtime commands. Never activate an empty patch just to run quarantine gates.

With the duty active, each clean gate executes the entire module union and
uses `--skip-fail` only for its declared subset. Acceptance requires the exact
ordered ignored-failure set, a passing complement, and no skipped or unignored
failures. A now-green assigned module makes the gate stale; its same-job
direct repeat without `--skip-fail` supplies confirmation to remove that
assignment, not permission to report the old mapping as passing. Remove a
module's disabling change only when no failing-leg assignment remains.

After reassessment, run the patched case's focused check and the complete
stack's three full upstream legs on the stable candidate. An inactive duty
does not weaken or replace any production, native, package or nine-profile
complete-stack live requirement. Tests challenge the manual review; they do
not replace it.

Offline fork-control tests protect the retained directory and empty draft
state, exclude it from active selection and snapshots, reject an accidental
queue activation or direct test selection, and retain the active quarantine
manifest/path-transition checks. Run `make -C fork-maintenance check` after
changing this infrastructure.
