# Investigate A Downstream Case

Use the development loop in [`validation.md`](validation.md): nearest real
regression after each atomic edit, affected upstream/case/dependency modules,
relevant native or compiled modes, and early relevant live checks. Full matrix,
package, and complete live coverage belongs to final acceptance after review
and candidate freeze, not to every investigative edit.

## Establish current state without switching branches

Stay on `develop` and prove the start boundary:

```bash
make -C fork-maintenance isolated-start-check
```

Search the [session registry](session-close.md#start-of-a-session) at
`.artifacts/fork-maintenance/knowledge/INDEX.md` for the symptom, error text,
symbol or case, and read only the matching session records before exploring.

The gate allows dirty fork-control files but rejects every uncommitted product
change. It records the branch, HEAD, and unique source merge base already
embedded in current `develop`; it never fetches, queries moving master refs,
switches, merges, rebases, resets, stashes, stages, or commits. Old logs and
previous `case-check` results are leads, not evidence about that recorded
source commit.

The `develop` checkout is the patched product: the embedded base plus every
case commit (see [`case-commits.md`](case-commits.md#model)). Read the clean
upstream version of a file with `git show <base>:<path>`, using the base the
gate printed, and a case's own change with `case-show`.

Read the current affected source, adjacent tests, recent maintainer-authored
history, `CLAUDE.md`, `CONTRIBUTING.md`, the current test workflow, and lint
configuration as technical context only. Fork-owned instructions define the
investigation and validation process. Record the first directly observed
failing boundary, not a root-cause guess based on an older symptom.

## Create one case

Use a behavior-based lowercase slug:

```bash
make -C fork-maintenance case-new CASE=short-behavior-name
```

`case-new` only creates `cases/<slug>/` with a schema-2 manifest template and
the README skeleton. Complete its kind, title, dependencies, focused tests,
required gates, and README. Apply the mandatory
[case documentation standard](case-documentation.md) in this same pass,
including the reference line, the mechanism, ownership/failure paths and real
regression limits; the generated headings alone do not complete it. Then
implement the change and the smallest regression directly in the checkout and
record them as the case's single commit:

```bash
# edit only the product files of this case and add the smallest regression
git add -- <paths>
git -c commit.gpgsign=false commit -m "<subject>" -m "<body>" \
  --trailer "Fork-Case: short-behavior-name"
make -C fork-maintenance case-check CASE=short-behavior-name
```

Follow the [commit message](case-commits.md#commit-message) format. Runners
test committed `HEAD`, so commit before each named run. Every later edit of the
case is a fixup folded into that commit
([change a case](case-commits.md#change-a-case)); never add a second commit
with the same trailer. The case directory and its commit belong together:
`develop-check` fails while one exists without the other.

For every source or test file created by the candidate, use the file's native
comment syntax to add `Copyright (C) <current-year> kogeler` before committing.
Do not name an upstream maintainer as the author of a downstream-created file.
If the file copies or derives protected content, retain its required notices
and add the `kogeler` line.

The stack order is the order of the case commits in `develop`; a new commit
lands on top, after every case it declares in `dependencies`. Do not create a
tracked report or history directory for the investigation.

An upstream-only failing test is the exception to behavior-based case
creation: update the single `upstream-test-quarantine` duty case under the
rules in [`test-quarantine.md`](test-quarantine.md); do not create a production
case for it.

## Reassess an existing case

Its code is already applied in the checkout. Inspect and check it first:

```bash
make -C fork-maintenance case-show CASE=short-behavior-name
make -C fork-maintenance case-check CASE=short-behavior-name
```

`case-check` proves that exactly one commit carries the trailer, that it
touches only product paths, and that its diff applies to the embedded base on
its own (after its declared dependencies), is neither already present nor
ambiguous, and can be removed from `HEAD` without conflict. A failure stops
the reassessment. During an explicitly authorized upstream refresh, a diff
already upstream byte for byte disappears in the rebase and conflicts are
resolved inside the replayed case commit, as described in
[`upstream-refresh.md`](upstream-refresh.md).

For a claimed upstream replacement, map each original trigger, production
path, state transition, and postcondition to current code. Then run the retained
focused regression on the clean embedded source. Do not retire a case from
commit-message similarity alone; a proven replacement is retired with
`case-drop` ([retire a case](case-commits.md#retire-a-case)).

If existing tests do not observe the disputed path, improve the case-owned test
or durable runner first. A copied test, temporary source rewrite, or one-off
container command is diagnostic only.

## Controls and unrelated failures

For graphics work, keep clean/patched controls on the same commit, image,
endpoint distribution, render node, dimensions, compositor, application, and
profile. Prefer the real application when buffer format or damage cadence is
part of the failure.

When a test outside selected paths fails, follow the narrow same-mode control
procedure in [upstream tests](upstream-tests.md#failure-triage). Matching
canonical Actions output is technical diagnostic context, not a prerequisite
or substitute for current clean proof. Never skip, weaken, reconfigure, or
repair that foreign test inside the current production case. Admission to the
duty quarantine requires task authority and its clean reassessment gates;
request new scope only when the existing task does not authorize that repair.
