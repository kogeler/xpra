# Sessions: Start From The Registry, Close To Knowledge

## Rule

A session is one agent task, from its first command to its handoff. While it
runs, the agent may keep any amount of output below
`.artifacts/fork-maintenance/`: job results, caches, probes, copies of logs,
notes. Case work is not output: it lives in the case commits on `develop`
(see [`case-commits.md`](case-commits.md)), which the agent creates during the
task. When the task is finished, and **before any control commit and before
the final handoff**, the agent closes the session. Afterwards the whole
`.artifacts/` tree holds only:

```text
.artifacts/fork-maintenance/
├── knowledge/
│   ├── INDEX.md                 generated session registry
│   └── sessions/<session>.md    one distilled record per session
└── (empty lifecycle directories and 0-byte lock files)
```

Raw output never outlives its session. Anything a future agent needs must be
distilled into the session record; everything else is discarded, including
reusable caches. Podman images and volumes are engine objects outside this
tree and are unaffected.

## Session identity and working area

Choose one lowercase session ID before starting, for example
`popup-modal-20260924`. It is also the cycle prefix of every `RUN` and
`IMAGE_RUN` in the session (see [`cycle-cleanup.md`](cycle-cleanup.md)) and
the name of its record.

Keep the session's own notes under `work/<session>/`:

```text
.artifacts/fork-maintenance/work/<session>/
├── ledger.md    cycle index and resume point (validation.md, upstream-refresh.md)
├── notes/       per-case working notes
└── scratch/     ad hoc probes: mktemp -d -p .artifacts/fork-maintenance/work/<session>/scratch
```

The layout inside `work/<session>/` is free. Do not create scratch anywhere else
below `.artifacts/`, and never inside `knowledge/`. Mid-session
`artifacts-clean` keeps `work/` and all caches.

## Start of a session

Read `.artifacts/fork-maintenance/knowledge/INDEX.md` first. Search it for the
symptom, error text, symbol, case slug or subsystem, open only the matching
records, and follow their `Next time` hints. Records are leads, not acceptance
evidence: current source, current case files and new named results outrank
them, and a deleted run named in a record cannot be reused.

## Write the session record

Create the skeleton and fill every placeholder:

```bash
make -C fork-maintenance knowledge-new SESSION=<session>
```

The record is concise Markdown, at most 16 KiB. Its fixed header feeds the
registry:

```markdown
# <one-line problem statement>

- Date: 2026-09-24
- Kind: refresh | patch | investigation | tooling | ci | package
- Cases: <case slugs, comma-separated, or none>
- Keywords: <symptoms, error strings, subsystems, symbols>
- Summary: <one sentence: the conclusion a future agent needs>

## Problem
## Findings
## Changes
## Verification
## Next time
```

Header values must not contain `|`, `<` or `>`. Every section needs real
content; HTML comments do not count. Write what saves the next agent time:

- **Problem**: trigger, observed symptom and short exact signatures (an error
  line, failing test ID, profile), plus the embedded source and `develop`
  commits;
- **Findings**: root cause or review conclusion with `path:symbol`
  references, the ownership/failure paths that mattered, and dead ends with the
  reason they failed;
- **Changes**: cases (by slug) and tooling changed and why, keep/adapt/retire
  decisions, and what was deliberately left alone;
- **Verification**: one line per gate with its outcome; remaining or failed
  gates. Named runs are deleted by the close, so record conclusions only;
- **Next time**: where to start, useful commands and oracles, pitfalls, open
  risks and follow-ups.

Never paste logs, reports, JSON results, screenshots or diff bodies. Quote at
most a few lines of a signature. Keep durable architecture in the case README,
not here; the record captures the session's experience.

One session normally writes one record. When it revisits an older problem,
edit that record instead of repeating it, or put `Superseded by <session>` in
the old record's Summary. Delete a record that no longer has value. Regenerate
and validate the registry after any change; never edit `INDEX.md` by hand:

```bash
make -C fork-maintenance knowledge-index
make -C fork-maintenance knowledge-check
```

A session that produced nothing reusable, such as pure housekeeping, may skip
the record and says so in its handoff. It still closes.

## Close the session

Prerequisites, all through their owning targets:

1. collect every upstream-test, image-build, live and DEB job, review it, and
   run its exact `*-remove` (or `*-abort`) target;
2. fold every accepted product change into its case commit (no dirty product
   path, no pending `fixup!` commit), and finish any pending `cycle-clean` or
   `artifacts-clean` transaction with its original command;
3. move any scratch created outside `.artifacts/fork-maintenance/` into
   `work/<session>/`;
4. write the session record and regenerate the registry.

Then review and execute the exact plan:

```bash
make -C fork-maintenance artifacts-close-plan
make -C fork-maintenance artifacts-close CONFIRM=<artifacts_close_confirm>
make -C fork-maintenance artifacts-close-check
```

`artifacts-close-check` must print `session_closed=yes`. The plan lists every
target and a `blocked` list. The close deletes nothing while a path is blocked:

| Blocked reason | Resolution |
| --- | --- |
| runtime not removed, owner or transaction present | finish the job through its `collect`/`remove`/`abort` target |
| lifecycle state is not idle | finish the owning job or transaction; only empty lock files may remain |
| knowledge: ... | fix the record, then `knowledge-index` |
| outside `.artifacts/fork-maintenance` | move it into `work/<session>/` and re-plan |
| unsafe path (other-writable, hard link, special file) | inspect it; it is not yours to discard blindly |

Execution holds the four lifecycle locks and, without waiting, every `*.lock`
file directly inside a discarded cache directory, so a concurrent cache
publisher makes it fail instead of racing. It reuses the
digest-bound, resumable removal engine under the identity
`artifacts-close-<policy-sha256>`: after an interruption, rerun
`artifacts-close` with the same `CONFIRM`. Do not delete its markers by hand.

Deleting named results ends their reuse window in [`validation.md`](validation.md).
The next session rebuilds caches on demand: `live-venv` recreates the live
environment, the [typecheck venv](typecheck.md) is provisioned again, source
archives, bundles, selections and build contexts are regenerated, and
label-verified Podman images are reused where their input keys still match. A
Ruff executable should live outside the repository; a `tooling-venv/` below
`.artifacts/fork-maintenance/` is session state and does not survive.

## When not to close yet

Close only a finished task. A task that pauses mid-way (waiting for the
operator, an interrupted long run, a context handoff) keeps its `work/` ledger
and results so it can resume; it closes when it finishes. An explicit operator
request to keep raw evidence for review also defers the close; state that in
the handoff and close in the follow-up session.
