# Autonomous Upstream Refresh and Full Queue Adaptation

## Single agent entry point

Give an agent this exact directive; no priority case is required:

```text
Execute autonomous-upstream-refresh against the current fork master.
```

`autonomous-upstream-refresh` is an agent workflow name, not a shell command or
Make target. The directive is the complete invocation of this runbook. It
explicitly chooses existing local `master` as the next source boundary,
authorizes the queue-wide keep/adapt/retire decisions and in-scope repairs
defined below, and requires the agent to continue through the complete
validation and handoff. Do not ask the operator to repeat the base choice,
provide a `CYCLE`, or expand scope for another active case. The autonomous Git
mutations are the local rebase described below and the case-commit rewrites
which implement the review decisions (see
[commit authority](case-commits.md#commit-authority)).

Every case receives equally high review priority, depth, and evidence
requirements. Use stack order for operational dependencies, not to rank the
importance of defects. The older `PRIMARY_CASE=<slug>` spelling remains an
optional explicit request to start with that active production case; it never
reduces another case's review or reporting depth. Do not invent a priority or
ask the operator to choose one.

The agent derives one never-reused lowercase `CYCLE` prefix from the current
UTC date/time and a recognizable queue-refresh fragment, verifies that no
runtime identity already uses it, and records it before the first lifecycle
operation. Use enough time precision or append a numeric suffix to prove
uniqueness; do not ask the operator to name one.

## Purpose

This refresh has a mandatory code-review phase before runtime validation:

```text
record the case map → rebase develop onto local master in the checkout
  → per-case range-diff and case-check inventory
  → [review one case → decide → fixup/rebuild/drop → re-review → checkpoint]
  → next case (including necessary cross-case repairs in the same iteration)
  → composed review and manual-review exit → clean controls and runtime tests
  → candidate freeze → remaining final acceptance
```

Every case is exactly one `Fork-Case: <slug>` commit on `develop`, as specified
in [`case-commits.md`](case-commits.md). Follow the development,
candidate-freeze, and final-acceptance phases in [`validation.md`](validation.md)
only after the manual-review exit gate. During the initial review, finish all
review-driven adaptations, removals and regression-ownership migrations before
starting any new Xpra test, quarantine run, native/compiled regression, live
profile, or real package build. The per-case range-diff and `case-check`
inventory, static inspection, whitespace/lint and offline fork-control/safety
checks are not runtime validation and remain allowed. Review and implement
incrementally, one atomic case at a time. Do not accumulate a whole-queue
read-only review before making the already justified changes. Persist findings
while the relevant code is in context, then fold the complete atomic change
into its case commit and record its checkpoint before moving on.

After that gate, use nearest regressions, affected upstream/case modules,
relevant native/compiled checks, and the early live loop. Only after
the candidate is stable fill the final evidence gaps; do not repeat the whole
matrix after each subsequent correction. Tests can refute or strengthen a
review conclusion, but never replace the agent's reasoning about correctness,
necessity and uncovered behavior. Exhaustive test coverage is not achievable,
and a green suite cannot establish that every case is correct or still needed.

This is the canonical autonomous end-to-end runbook for an operator-selected
upstream refresh. The operator prepares local `master` and chooses when to
move the complete queue to that local boundary. Remote state and transport
are not prerequisites. The invocation above authorizes the rebase of local
`develop` onto existing local `master` in the `develop` checkout, including
conflict resolution inside the replayed commits, continuation or abort, and
the case-commit rewrites defined below.

All other Git operations are performed by the operator or delegated to the
agent by a separate explicit instruction. Do not fetch, update `master`,
switch or create branches, add a Git worktree, configure remotes, stash by
hand, create a control commit, or push as an implicit part of refresh.
Read-only local inspection is always allowed. Case commits are the storage of
case work: the agent creates, fixes up, squashes, drops and rebases them itself
as part of this runbook.

Two exceptions keep the refresh non-interactive. First, the checkout may be a
partial clone whose local `master` lacks blobs; `develop-rebase` then backfills
exactly the missing object IDs reachable from local `master` and `HEAD` from
the public canonical repository over anonymous HTTPS (`objects-backfill` runs
the same step alone). It names no remote, updates no ref, `FETCH_HEAD` or
configuration, and uses no credentials; objects are content-addressed, so the
bytes are identical to the promisor's. Every Make target exports
`GIT_NO_LAZY_FETCH=1`, so a missing object fails closed instead of lazily
fetching from the SSH promisor, which would open the operator's security-key
PIN prompt. Second, every commit the agent creates or replays is unsigned:
the rebase runs with `commit.gpgsign=false`, conflict continuation uses
`git -c commit.gpgsign=false rebase --continue`, and every case commit or
fixup is created with `git -c commit.gpgsign=false commit`. The operator's
signing key is interactive and never required.

Moving the embedded source invalidates every previous functional result. The
agent therefore reads and semantically reassesses each case commit in its
current surrounding source, gives it an explicit keep/adapt/retire conclusion,
implements that conclusion in the case commit before taking the next case, and
reviews the quarantine duty in the same incremental pass. It resolves and
reviews the complete resulting queue before runtime tests.
It then confirms or revisits those conclusions through every available
tests-only control, the clean quarantine reassessment, focused/native tests,
both real distribution package builds, all three full upstream legs, and all
nine complete-stack live profiles. Every case requires the same detailed
correctness and necessity analysis, including cases whose commit replayed
without conflict and whose diff did not change.

Invoking this runbook authorizes no control commit. Require clean `develop`
before rebasing; preserve dirty work pending an explicit operator disposition.
Case results are stored in the case commits themselves. Every control-plane
result produced by the refresh (case directories, manifests and READMEs,
quarantine lists, CI layout, documentation, runners, runbooks) remains
uncommitted for operator review; it survives later case rewrites through
`--autostash`. The rebase and every case rewrite change `develop` history, so
the operator publishes `develop` with `--force-with-lease` after the refresh
(see [`publish-develop.md`](publish-develop.md)).

## Inputs and reading

The invocation needs no case selection. If the operator explicitly supplies
the optional `PRIMARY_CASE`, it must be one production slug in the
pre-refresh case map (`case-list`) and affects only starting order, never
review depth. Derive `CYCLE` as specified by the single entry point and use it
for every `RUN` and `IMAGE_RUN` created by this refresh. The directive itself
confirms that this refresh should move `develop` to existing local `master`;
there are no additional required inputs.

Replace placeholders such as `<case>` and `<cycle>` in every example; never
pass the angle brackets literally.

Read the applicable material completely at its phase. Before cleanup or ref
changes, read items 1, 2, 3 (stack and manifests), 5, 7 and 8, plus the
validation flow. Read every case README, complete case commit, surrounding
code, callers, tests, overlaps and maintainer history in items 3, 4 and 6
against the new source after the rebase and before changing that case or
starting runtime validation. Do not perform the same full semantic review on
the old source merely to permit the rebase; read old case commits only as
needed to resolve a conflict or to preserve existing work. Fork-owned guides,
contracts, runbooks, and manifests alone define this process; inherited source
documents, workflows, and history supply technical context, never workflow
authority:

1. root `AGENTS.md` and `fork-maintenance/AGENTS.md`;
2. `fork-maintenance/CONTRACT.md` and this runbook;
3. `stacks/develop.toml` and every `cases/<id>/case.toml` and `README.md`;
4. every case commit (`make -C fork-maintenance case-show CASE=<id>`, then
   `git show` of that commit), including the quarantine commit while the duty
   is active, plus the complete surrounding source, callers, tests, and
   overlapping case commits for all touched paths;
5. `CLAUDE.md`, `CONTRIBUTING.md`, `.github/upstream-workflows/test.yml`, and
   `pyproject.toml`;
6. the current source, adjacent tests, and recent maintainer-authored history
   for every active production path and quarantine-owned test module;
7. [`bootstrap.md`](bootstrap.md),
   [`case-commits.md`](case-commits.md),
   [`test-quarantine.md`](test-quarantine.md),
   [`upstream-tests.md`](upstream-tests.md),
   [`live-tests.md`](live-tests.md), and, when applicable,
   [`deb-packages.md`](deb-packages.md);
8. [`cycle-cleanup.md`](cycle-cleanup.md),
   [`session-close.md`](session-close.md) and
   [`publish-develop.md`](publish-develop.md).

Also read [`validation.md`](validation.md) for scheduling, candidate freeze,
and the exact evidence-reuse rules.

While the rebase is stopped, the tracked control files are those of the
commits replayed so far and may be old or absent. Read a runbook, manifest or
case README from the old tip instead, for example
`git show ORIG_HEAD:fork-maintenance/docs/runbooks/upstream-refresh.md`, and
re-read the working tree once the rebase has completed.

Resolve one reviewed Ruff executable before the first control-plane check and
record its version. `<ruff>` below is its absolute path. A system `ruff` or an
operator-provisioned executable outside the repository is valid. An
operator-provisioned `.artifacts/fork-maintenance/tooling-venv/bin/ruff` is
also valid after its ownership and executable-file boundary are reviewed, but
it is session state that `artifacts-close` discards. The optional tooling venv
is not created by this runbook and is not acceptance evidence.

When none exists, the agent provisions the version pinned in
`.pre-commit-config.yaml` in a disposable container and uses a wrapper outside
the repository, for example `/tmp/<user>/ruff`, that mounts the repository
read-only at its own path:

```bash
printf 'FROM docker.io/library/python:3.13-slim\nRUN pip install --no-cache-dir ruff==<pinned>\n' |
  podman build -q -t localhost/ruff-tool:<pinned> -
cat > /tmp/<user>/ruff <<'SH'
#!/bin/sh
root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
exec podman run --rm --network=none -e RUFF_NO_CACHE=true \
  -v "$root":"$root":ro -w "$PWD" localhost/ruff-tool:<pinned> ruff "$@"
SH
chmod 755 /tmp/<user>/ruff
```

Record the pinned version and image ID in the ledger. The image is tooling,
not a maintenance cache, and is not acceptance evidence.

If the retained artifact inventory contains an owner from the retired
`xpra-lab-*` namespace, use only the exact retired-namespace classification in
[the pre-refresh record](#pre-refresh-record). The old migration document is
no longer maintained. Current lifecycle readers intentionally have no
compatibility mode for that namespace; unmatched state remains a stop, not
permission to restore old tools or weaken the ownership audit.

An optional starting slug cannot be `upstream-test-quarantine`: it is a
temporary test duty, not a production behavior. The duty is nevertheless
always in scope and is reassessed through `test-quarantine.md`.

## Clean local rebase boundary

Start on `develop` with no merge, rebase, cherry-pick, or revert in progress.
Before touching a ref, inspect every staged, unstaged, and untracked non-ignored
path. Do not stash, reset, clean, or discard existing work. Reject unresolved
conflicts, secrets, generated artifacts, an uncommitted product-path change,
and any file whose ownership or intent is uncertain. Ignored runtime state is
never staged. `isolated-start-check` must prove that every legitimate change
stays inside the allowed fork-control boundary; it refuses dirty product paths.

Review the current state without changing it:

```bash
(
set -eu

test "$(git branch --show-current)" = develop
for marker in MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD rebase-merge rebase-apply; do
  test ! -e "$(git rev-parse --git-path "$marker")"
done

git status --short --branch
git diff --check
git diff --stat
git diff
git diff --cached --check
git diff --cached --stat
git diff --cached
git ls-files --others --exclude-standard
make -C fork-maintenance isolated-start-check
make -C fork-maintenance stack-check STACK=develop
make -C fork-maintenance RUFF=<ruff> check
)
```

Inspect every listed untracked file separately. If non-ignored changes remain,
preserve them and return only that prerequisite to the operator, who may
resolve it directly or explicitly delegate the necessary Git operation. Do
not stage, commit, stash or discard them merely to pass this boundary.
Record existing local `develop` and `master` once the checkout is clean.
Any missing local branch must likewise be prepared through an explicit
operator action.

A pending `fixup!`, `amend!` or `squash!` commit left by an interrupted earlier
session is unfinished case work. Fold it with
`make -C fork-maintenance develop-squash` only when an earlier session ledger
records its target and intent, then repeat this boundary; otherwise report it
as a prerequisite. After the rebase, case results become case commits and
control-plane results stay uncommitted; control paths may then remain dirty
while case commits are rewritten.

## Pre-refresh record

Record in the cycle ledger (`.artifacts/fork-maintenance/work/<session>/`),
never in a tracked file:

- the old local `develop` tip which will enter the rebase; it is the rollback
  point;
- old embedded source merge base;
- local and cached fork-master commits;
- the case map printed by `case-list` (slug, commit, subject for every case in
  stack order). These SHAs are a point-in-time record for the per-case
  range-diff after the rebase; never copy them into a tracked file;
- the complete old downstream series, control commits included;
- current branch/status;
- the derived cycle identifier and any explicit operator-requested starting order.

Use commands which do not change refs or tracked source, and require exactly
one old embedded source commit. Fork-control checks may update only ignored
tool caches:

`check` runs the live-runner unit tests with the hash-locked live
interpreter, so create or validate it first with `make -C fork-maintenance
live-venv` (see the quiescence note below).

```bash
(
set -eu

test "$(git branch --show-current)" = develop
test -z "$(git status --porcelain=v1 --untracked-files=all)"
git status --short --branch
git branch --show-current
git rev-parse HEAD
git rev-parse refs/heads/master
set -- $(git merge-base --all refs/heads/master HEAD)
test "$#" -eq 1
printf 'old_source=%s\n' "$1"
if git show-ref --verify --quiet refs/heads/master; then
  printf 'local_master=%s\n' "$(git rev-parse refs/heads/master)"
else
  result=$?
  test "$result" -eq 1
  printf '%s\n' 'local_master=<missing>'
fi
git log --reverse --format='%H %s' "$1"..HEAD
make -C fork-maintenance case-list
make -C fork-maintenance repo-status
make -C fork-maintenance develop-check
make -C fork-maintenance RUFF=<ruff> check
)
```

`repo-status` does not report local `master`, so record it explicitly. A
missing local `master` blocks the rebase until the operator prepares it or
explicitly delegates that operation. Refresh never creates or updates it.
`develop-check` proves on the clean tree that every fork commit is a valid
control or case commit, that case directories and case commits correspond one
to one, and that every case passes `case-check` on the old base; an
unexplained failure blocks the rebase.

There is no single global runtime-status target. Before rebasing, perform a
bounded read-only inventory of the exact ownership roots. Skip a root only when
it is absent; a symlink, non-directory root, or unreadable root is a hard stop:

First establish exclusive maintenance coordination for this checkout and its
artifact tree: no other operator, agent, or automation may commit to or
rewrite `develop`, or start a test, live, DEB, image, live-environment, or
cleanup lifecycle from this point through the rebase and initial post-rebase
resolution. The per-subsystem locks serialize individual transitions but this
cross-subsystem scan cannot hold them as one atomic lease; without that
external quiescence guarantee, stop instead of treating an instantaneously
empty scan as stable.

The live lifecycle commands depend on the hash-locked analysis environment.
Under that quiescence guarantee, create or validate it before inspecting any
retained live owner or transaction; `live-venv` is also the sole recovery route
for its exact environment partial:

```bash
make -C fork-maintenance live-venv
make -C fork-maintenance live-venv-check
```

```bash
(
set -eu

state_root=.artifacts/fork-maintenance

if test -L .artifacts; then
  printf '%s\n' 'unsafe artifact root: .artifacts' >&2
  exit 1
elif test -e .artifacts; then
  test -d .artifacts
  test "$(stat -c %u .artifacts)" -eq "$(id -u)"
  artifact_mode=$(stat -c %a .artifacts)
  test $((8#$artifact_mode & 8#22)) -eq 0
fi

for state_path in \
  "$state_root" \
  "$state_root/cycle-cleanups" \
  "$state_root/namespace-migration" \
  "$state_root/upstream-tests" \
  "$state_root/upstream-tests/runs" \
  "$state_root/upstream-tests/logs" \
  "$state_root/upstream-tests/image-builds" \
  "$state_root/upstream-tests/sources" \
  "$state_root/jobs" \
  "$state_root/jobs/live" \
  "$state_root/live-results" \
  "$state_root/venvs" \
  "$state_root/build-contexts" \
  "$state_root/build-contexts/live" \
  "$state_root/source-archives" \
  "$state_root/deb-packages" \
  "$state_root/deb-packages/runs" \
  "$state_root/deb-packages/results" \
  "$state_root/deb-packages/sources" \
  "$state_root/deb-packages/selections" \
  "$state_root/deb-packages/outputs" \
  "$state_root/deb-packages/releases" \
  "$state_root/deb-packages/locks" \
  "$state_root/deb-packages/locks/images"; do
  if test -L "$state_path"; then
    printf 'unsafe private state root: %s\n' "$state_path" >&2
    exit 1
  elif test -e "$state_path"; then
    test -d "$state_path"
    test "$(stat -c %u "$state_path")" -eq "$(id -u)"
    test "$(stat -c %a "$state_path")" = 700
  fi
done

state_path="$state_root/cycle-cleanups"
if test -L "$state_path"; then
  printf 'unsafe state root: %s\n' "$state_path" >&2
  exit 1
elif test -e "$state_path"; then
  test -d "$state_path"
  find "$state_path" -xdev -mindepth 1 -maxdepth 2 \
    ! -name '*.lock' -print
fi

for state_path in \
  "$state_root/upstream-tests/runs" \
  "$state_root/upstream-tests/logs" \
  "$state_root/upstream-tests/image-builds" \
  "$state_root/upstream-tests/sources" \
  "$state_root/jobs/live" \
  "$state_root/live-results" \
  "$state_root/venvs" \
  "$state_root/deb-packages/runs" \
  "$state_root/deb-packages/results" \
  "$state_root/deb-packages/sources" \
  "$state_root/deb-packages/selections" \
  "$state_root/deb-packages/outputs" \
  "$state_root/deb-packages/releases"; do
  if test -L "$state_path"; then
    printf 'unsafe state root: %s\n' "$state_path" >&2
    exit 1
  elif test -e "$state_path"; then
    test -d "$state_path"
    find "$state_path" -xdev -mindepth 1 -maxdepth 2 \
      \( -name '*.owner' -o -name 'owner.json' \
      -o -name '*.owner.json' -o -name '*.prelaunch.json' \
      -o -name '*.image-prelaunch.json' \
      -o -name '*.freeze-prelaunch.json' \
      -o -name '*.freeze.json' -o -name '*.freeze-abort.json' \
      -o -name '*.abort.json' -o -name '*.remove.json' \
      -o -name '*.payload' \
      -o -name '*.runtime' -o -name '*.completion.json' \
      -o -name '*.freeze-result.json' \
      -o -name '*.bundle.partial' \
      -o -name '.environment.partial' \
      -o -name '.source-snapshot.partial' \
      -o -name '.selection-cache.partial' \
      -o -name '.*.freeze-*' -o -name '.*.validate' \
      -o -name '.*.validate.owner.json' \
      -o -name '..*.validate.partial' \) -print
  fi
done

for state_path in \
  "$state_root/upstream-tests/.foreground-payload" \
  "$state_root/upstream-tests/.foreground-payload.owner.json" \
  "$state_root/venvs/.environment.partial" \
  "$state_root/venvs/.environment.partial.owner.json" \
  "$state_root/deb-packages/sources/.source-snapshot.partial" \
  "$state_root/deb-packages/sources/.source-snapshot.partial.owner.json" \
  "$state_root/deb-packages/selections/.selection-cache.partial" \
  "$state_root/deb-packages/selections/.selection-cache.partial.owner.json"; do
  if test -L "$state_path"; then
    printf 'unsafe runtime state: %s\n' "$state_path" >&2
    exit 1
  elif test -e "$state_path"; then
    printf '%s\n' "$state_path"
  fi
done

for state_path in \
  "$state_root/upstream-tests/image-builds" \
  "$state_root/deb-packages/runs"; do
  if test -e "$state_path"; then
    find "$state_path" -xdev -mindepth 1 -maxdepth 1 -type d -print
  fi
done

release_root="$state_root/deb-packages/releases"
if test -e "$release_root"; then
  find "$release_root" -xdev -mindepth 1 -maxdepth 2 -print
fi
)
```

Treat every entry printed below `deb-packages/releases/` separately from local
DEB run state. It is hosted publication staging, has no local cleanup or resume
authority, and is not cycle-owned. Review it against the exact completed or
interrupted structure in [`deb-packages.md`](deb-packages.md). A nonempty
release tree stops this agent-run refresh for operator review; never invoke the
hosted publication target, mutate GitHub, or remove that tree locally to make
the inventory pass.

The filesystem inventory is not sufficient because an interrupted runtime can
leave an owned Podman object after losing its filesystem owner, or a damaged
owner label can evade an exact-owner filter. Take a fresh read-only inventory
of the complete Podman object sets before routing any state and repeat it after
routing:

```bash
podman ps --all --format json
podman network ls --format json
podman image ls --all --format json
podman volume ls --format json
```

Inspect every listed container and network by immutable ID because the network
listing need not expose labels. From those inspections and the complete
image/volume listings, select every object which has any
`io.xpra.fork-maintenance.*` or `io.xpra.lab.*` label; whose name has a known
maintenance prefix such as `xpra-fork-maintenance-live-` or `xpra-deb-`; or
whose name/ID occurs in a current record or retained removal transaction.
Inspect each selected image or volume too.
Every selected runtime object must map one-to-one to the exact identity and
complete maintenance label set in one canonical, validated current owner,
prelaunch, abort, or removal authority found above. Use the matching lifecycle
reader to validate that record and route it; labels or a familiar name alone
are never authority. An unmatched, duplicate, mislabeled, or multiply claimed
object is an orphan and stops the refresh—do not call `podman rm` or
`podman network rm` directly. After all authorized routing, require the
complete container and network listings to contain no maintenance runtime
object.

### Disposable image caches

Image caches are reproducible build output, not uncommitted source or runtime
owners. The refresh itself authorizes removal of obsolete, retired-namespace,
or unverifiable **maintenance/test image caches** without another operator
question. Do not turn uncertainty about an old cache's freshness into a
blocker: discard it and rebuild from the current frozen inputs if a later gate
needs it. A still-valid current-namespace cache may be retained; its owning
preflight must verify it again before use. Rebuilds start only after the
whole-queue manual-review exit gate.

Prefer the owning public cache-removal target when it supports that exact
image. For an unused historical/legacy test image outside those targets, the
agent may use this narrow direct removal boundary:

- identify the maintenance/test purpose from inspected labels, tags, a
  current record, or the operator's explicit target; a familiar substring
  alone does not authorize deleting another project's images;
- record its complete immutable image ID, tags and relevant labels; inspect
  all containers, including stopped ones, and all current owner/prelaunch/
  transaction records to prove none still needs it;
- under the already-established exclusive maintenance coordination, repeat
  the identity/reference check immediately before removal; run
  `podman image rm --no-prune <full-immutable-id>` for that one image only,
  without `--force`, `--all`, `--ignore`, tag-only deletion or broad pruning;
- require `podman image exists <full-immutable-id>` to return the documented
  absent status, then repeat the complete inventory and record the removal.
  If Podman reports a container or child-image dependency, inspect that exact
  dependency and route it through its owning lifecycle; never force-delete it.

This exception removes an unused image only, never a container, network,
volume, running job, owner record, source tree, or result. Stop for genuinely
unresolved active ownership or irreplaceable data, not for a missing old image
migration report. A retired image has no special permanent retention exemption
because an earlier migration called it foreign; an explicitly selected obsolete
test image is disposable by the same bounded rule. Do not invent replacement
owner/migration records or resurrect retired tooling. The ccache volume and
other persistent data remain outside automatic image-cache disposal.

Derive each candidate identifier only from its canonical path shape: for DEB
and image-build `owner.json`, the run name is the validated parent directory,
not the basename. Before invoking a lifecycle reader for a removal
transaction, inspect its safe bounded `owner`, `kind`, and schema. Then let the
matching current lifecycle command validate every current-namespace record.
Inspect a test, live, DEB, or image-build owner with the corresponding
`test-status`, `live-status`, `deb-status`, or `test-image-status` target.
Route a current-owner `*.remove.json` by subsystem instead of assuming one
common status protocol:

- below `upstream-tests/logs`, use its bounded `kind` only to choose
  `test-remove` or `test-image-remove`, then repeat that idempotent remove target
  to validate and finish the transaction; upstream test/image status does not
  load a retained removal transaction after its owner is gone;
- below `jobs/live`, repeat the idempotent `live-remove` first—even when the
  main owner has not yet been deleted—then require `live-status` to report
  validated `phase=removed`;
- below `deb-packages/results`, repeat `deb-remove` to validate and finish the
  transaction, then use `deb-status` for the retained result.

Run that validation route for every printed transaction owned by
`xpra-fork-maintenance-upstream-tests`, `xpra-fork-maintenance-live-job`, or
`xpra-deb-packages`, even if its name belongs to an older cycle. Historical
output is not current proof, and a current transaction can be the only
remaining authority for a bound runtime object. After the idempotent current
validation completes, the transaction remains as retained evidence; never
delete it directly.

There is one exact retired-namespace exception. A transaction owned by
`xpra-lab-upstream-tests` below `upstream-tests/logs`, or by
`xpra-lab-live-job` below `jobs/live`, must not be passed to a current lifecycle
reader: the namespace cutover deliberately removed that compatibility code.
Classify it as inert historical evidence only when all of these fresh checks
pass:

- it is a non-symlink, current-uid, single-link regular file with mode `0600`
  in that exact canonical root. A legacy upstream transaction has exactly
  `schema, owner, kind, name, record, owner_sha256, log_sha256, status_sha256`;
  its kind is `test-remove` or `image-build-remove`, its basename matches
  `name`, and its embedded record has the same owner/name plus test schema
  `"4"`, or image schema `3` and kind `image-build`. A legacy live transaction
  has exactly `schema, owner, kind, run, record, log_sha256, status_sha256,
  runtime_sha256`, kind `live-remove`, and matching basename/run plus embedded
  schema `4`, owner, run, result path, and bounded runtime identities. In both
  forms the retained private log/status siblings must be exactly
  `<name>.log`/`<name>.status` for upstream or
  `<run>.log`/`<run>.status.json` for live and match the recorded SHA-256.
  Reproduce the recorded owner digest (`owner_sha256`, or
  `runtime_sha256.owner` for live) from canonical pretty sorted JSON plus
  newline for image-build and live records. A legacy upstream test owner
  instead uses its historical `key=value` lines in this exact order:
  `schema`, `owner`, `run_id`, `name`, `container_id`, `target`, `selection`,
  `selection_sha256`, `patch_mode`, `payload_path`, `source`, `source_head`,
  `source_remote`, `workflow_sha256`, `runner_sha256`, `image`, `image_id`,
  `image_input_sha256`. Its missing runtime/completion bytes cannot be
  re-hashed; require the recorded digest keys and values to be bounded and
  require every record-bound candidate path to be absent;
- no retired owner, prelaunch, payload, runtime, completion, freeze, abort, or
  partial exists anywhere in the active ownership roots printed above;
- every runtime identity and path bound by the retained transaction is absent
  according to fresh filesystem and complete Podman inspection; verify each
  recorded immutable container/image identity by its read-only existence
  interface when that identity is present in the transaction;
- no retired maintenance container, network or volume remains. Route unused
  retired image caches through the disposable-image boundary above, then
  require no remaining `io.xpra.lab.*` image labels in the final inventory.

Compare current JSON structures and actual runtime absence, not
presentation-only text or a historical migration summary. Missing completed
migration plans are not a refresh dependency: they may legitimately have been
discarded by artifact housekeeping. Neither restore them nor require their
historical image-ID list when the current complete inventory and each extant
transaction provide the applicable identities. Retained legacy logs remain
diagnostic context, never current acceptance. If no legacy transaction exists,
there is no legacy-evidence validation gate to manufacture.

### Route the remaining state

A pending cycle-clean transaction must be resumed with its original reviewed
`CYCLE` and confirmation digest. `live-venv` owns exact recovery of its
environment partial. For an upstream foreground/bundle partial or a DEB
source, selection, or validation partial, use only the exact recovery route in
the owning upstream/live/DEB runbook; if no unambiguous public route applies,
stop. Use a matching collect, remove, or abort interface only after its owning
runbook authorizes that transition. The image-cache boundary above is the only
narrow direct cleanup exception; never delete a marker, process, or container
by hand. If an identifier is ambiguous or belongs to another unfinished work
cycle, stop for operator review.

After resolving any marker-backed state, repeat the inventory and require no
unresolved printed runtime, transaction, partial, owner, or prelaunch entry to
remain. Retained removal transactions count as resolved only after their
applicable current validation route above has passed.

Do not start the rebase with an unexplained offline failure, ambiguous merge
base, merge commit in the downstream range, uncommitted product change,
pending fixup commit, active transaction, or unreviewed runtime owner.

## Rebase develop onto local master

Stay on clean local `develop` in its checkout and use the recorded existing
local `master`. Neither remote URLs nor cached/live remote equality are
admission gates. The public target performs no master update or branch switch
and creates no branch or worktree: it rebases the checkout itself. In a partial
clone it first backfills missing objects credential-free as described in
[Purpose](#purpose), then rebases without signing:

```bash
make -C fork-maintenance develop-rebase
```

Never substitute a remote-tracking ref for local `master`, and never
merge either master ref into `develop`.

Git drops a replayed commit whose change is already upstream byte for byte;
that is expected and needs no action during the rebase. The case is accounted
for after the rebase.

### Resolve conflicts inside the replayed commit

If the rebase stops, the conflict belongs to exactly one replayed commit.
Identify it with `git status` and `git show REBASE_HEAD`: its `Fork-Case`
trailer names the case, and a commit without the trailer is a control commit.
Inspect both sides of every conflict, the old case commit from the recorded map
and the new upstream source. The resolution becomes part of that commit, so it
stays inside that commit's class and case:

- in a case commit, resolve only that case's product hunks and keep the case's
  intent on the new source; never move a hunk into another case, add an
  unrelated change, or touch a control path;
- in a control commit, preserve canonical upstream workflow changes as
  byte-identical disabled renames and keep fork-only executable workflows
  separate; never add a product path.

Stage the resolution and continue until the rebase completes:

```bash
git add -- <resolved-paths>
git -c commit.gpgsign=false rebase --continue
```

Port a case hunk whose target code moved or changed shape when the mapping is
certain. Where upstream redesigned the target so that no certain mapping
exists, resolve that hunk to the new upstream side, record the case as
`diverged` in the ledger together with the unported hunks, and rebuild it in
its review iteration ([Rebuild a diverged case](#rebuild-a-diverged-case)).
Never skip a commit (`git rebase --skip`) and never let a resolution silently
empty a case commit: retirement is a reviewed decision made after the rebase.
If the only consistent resolution leaves a case commit without any change, keep
it as a recorded empty placeholder with its original message and trailer, then
continue:

```bash
git -c commit.gpgsign=false commit --allow-empty -C REBASE_HEAD
git -c commit.gpgsign=false rebase --continue
```

A placeholder fails `case-check` until its review either rebuilds it or retires
it with `case-drop`. If no safe resolution can be established, for example a
control-commit conflict whose correct form is unclear, run
`git rebase --abort`, which restores the old tip, stop the refresh, and report
the exact conflict.

Mid-rebase, read runbooks and manifests with `git show ORIG_HEAD:<path>` as
described in [Inputs and reading](#inputs-and-reading), and do not run
`make -C fork-maintenance` targets: tooling runs only after the rebase has
completed. Write conflict notes to the ignored ledger as you go.

### Record the result

After a successful rebase, record the new `develop` and embedded source
commits, compare the replayed downstream series with the recorded old series,
record the new case map and compare every case with its recorded old commit:

```bash
git range-diff \
  <old-source>..<old-develop> \
  <new-source>..HEAD
make -C fork-maintenance case-list
git range-diff <old-sha>^! <new-sha>^!
```

Run the per-case `range-diff` for every slug present in both maps. Classify each
case in the ledger as replayed with an unchanged diff, changed by a conflict
resolution, `diverged`, placeholder, or dropped. A slug missing from the new
map had its commit dropped by Git because its change is already upstream; its
case directory remains and goes through the reviewed
[retirement](#retire-a-fully-replaced-case). The rebase changes commit
identities even when a case's diff is unchanged, and a classification is an
input to the review, never its result.

The recorded old tip is the rollback point. If the refresh has to be abandoned
after the rebase completed, restore it with `git reset --keep <old-develop>`
(or from the reflog) and report; never do this merely to retry.

After rebase, re-read the current fork-owned `AGENTS.md`,
`fork-maintenance/AGENTS.md`, and `fork-maintenance/CONTRACT.md` before resolving
a case or making any post-rebase edit. Separately re-read `CLAUDE.md`,
`CONTRIBUTING.md`, and `pyproject.toml` completely for the new technical
source/build/test context; none is fork-process authority. At this point the
disabled workflow copy has not yet passed its post-rebase byte-identity gate,
so read the canonical workflow directly from the recorded new source rather
than trusting that copy:

```bash
git show <new-source>:.github/workflows/test.yml
```

Re-read the active manifests and owning runbooks if the replay or a conflict
changed them, then inspect the new surrounding source, adjacent tests, and
current maintainer-authored history for every path being reassessed.
New source behavior supersedes old technical assumptions and handoff notes;
inherited instructions cannot alter the fork-owned process.

Before making any new tracked edit, prove the rebased branch and inspect each
case separately so a first failure does not hide later status:

```bash
make -C fork-maintenance patch-start-check
make -C fork-maintenance case-check CASE=<case>
```

Run `case-check` for every case in the new map in stack order, recording each
result even if an earlier case fails, then run `stack-check STACK=develop`
once. This is a textual/provenance inventory, not manual review or permission
to test. A placeholder is expected to fail; a `diverged` case may pass while
still incomplete, so its ledger status, not the check, decides. A case which is
no longer independent of the others or no longer removable requires
investigation before it can be tested. Every case, including one whose commit
replayed unchanged, must next pass the deep manual review below. Do not repair
only conflict hunks and proceed to tests.

Also run:

```bash
make -C fork-maintenance ci-layout-check
```

After this gate passes, read `.github/upstream-workflows/test.yml` completely
as the verified disabled representation. If the gate required a workflow
boundary repair, run it again after that repair and only then re-read the
verified file before production assessment or editing.

A new or modified canonical workflow must be moved to the byte-identical
`.github/upstream-workflows/` boundary, leaving only the three authorized fork
workflows executable. Complete this repair before building the upstream-test
image or starting any test. It is control-plane work: keep it uncommitted as a
refresh result. Case work continues with it dirty, because the rewriting
targets autostash control paths and runners accept dirty control paths.

## Autonomous queue-wide authority and self-correction

An upstream rebase is never a single-case operation. The invocation expressly
authorizes the agent to inspect, retain, adapt, narrow, or retire every active
production case; update the quarantine duty; and repair the fork control,
tests, runners, documentation, and runbook needed to complete this refresh.
There is no low-priority or applicability-only review path. Before any new
runtime validation:

- every pre-refresh case must have a complete manual review and a reasoned
  decision, with all resulting production and regression-ownership changes
  implemented and re-reviewed;
- every remaining case must pass `case-check` on the new source: one nonempty,
  product-only commit which applies to the base on its own after its declared
  dependencies and is removable from `HEAD`;
- all case commits must compose in stack order (`stack-check`);
- quarantine modules and per-leg assumptions must be manually reviewed; the
  subsequent clean gates must confirm or correct their empirical assignments;
- any changed upstream workflow boundary must be reconciled;
- no product change may remain uncommitted and no `fixup!`/`amend!` commit may
  remain unsquashed.

These resolution checks are necessary but cannot satisfy the manual-review
exit gate. That gate precedes even clean controls and focused diagnosis.
The later candidate-freeze review in [`validation.md`](validation.md),
including tests, live oracles, compiled risks, and build inputs, remains a
separate prerequisite for the complete-stack final workload.

Perform the complete semantic mapping and keep/adapt/retire analysis below for
every case without waiting for a failure. If later testing passes unexpectedly,
skips, or no longer observes the claimed defect, reopen the affected case's
review and its overlapping consumers in this same pass. Do not request scope
expansion, preserve a potentially redundant or vacuous case commit merely
because it still replays, or run the expensive final matrix on an unresolved
stack.

When execution exposes an in-scope error or omission in this runbook, a related
contract, control-plane implementation, test, live/package harness, or case
documentation, repair it immediately and add or update the narrow regression
which proves the correction. Runtime checks of review-driven changes wait for
the manual-review exit gate; narrow offline fork-control checks may run during
review. Keep control-plane changes with the other uncommitted refresh results;
a product or test correction owned by a case goes into that case commit.
Continue the same runbook pass; do not restart the process from its first step
merely because the written procedure changed.

Maintain a current external run ledger of the exact source, case, selection,
runner, image-input, command, and result identities. Reuse an already valid
expensive result only while every semantic input which can affect it remains
identical. Rerun the narrow preflight after a pre-test guard repair, and rerun
an expensive payload only when its frozen source, frozen case diff or
selection, image inputs, entrypoint, test command/assertions, runner, scenario,
or acceptance behavior changed. The frozen diff bytes decide, not the commit
SHA: a SHA which changed only because an earlier commit was rewritten keeps the
same diff unless that commit touched the same files. A comments-only or
documentation-only correction does not invalidate an otherwise exact result.
If uncertainty remains, treat the result as invalid and rerun its gate with a
new identity.

Stop and return to the operator only for a boundary the directive cannot safely
authorize: missing local master or a required Git mutation not separately authorized;
unsafe, secret, unexplained, or externally owned local state; an unresolved
semantic choice where a correct implementation cannot be established; or a
mandatory physical/resource boundary which is genuinely unavailable. Report
the exact blocker and all completed current evidence. Ordinary difficulty, a
change to any active case, or a repair to this runbook is not a scope stop.

Every case is inspected and updated one at a time in the `develop` checkout,
even while reviewed control-plane results are uncommitted: edit its product
files, fold them into its case commit, and check it before taking the next
case. There is no other branch, no worktree and no separate rebuild mode; a
`diverged` case is rebuilt the same way ([below](#rebuild-a-diverged-case)).
Never add a second commit for a case or leave a fixup unsquashed merely to
make the next case possible.

## Reassess every production case semantically

This is a mandatory manual code review by the agent, not a test-dispatch phase.
Begin after recording every range-diff classification and `case-check` result
and completing the new-source and CI-boundary reading above. Enumerate the
recorded pre-refresh case map and account for every case after any proposed
retirement or ownership migration. Review all cases to the same depth,
including cases whose diff replayed unchanged and cases whose commit Git
dropped. Never let a failed first case hide the rest.
Use the per-case loop below in operational stack order; equal depth does not
mean reviewing every case before implementing the first. Existing recorded
reviews from this cycle are inputs to the next implementation, not a reason to
finish an outstanding read-only sweep first.

Read each complete case commit and manifest against the actual new embedded
source, not just its range-diff, conflict hunks, case README, or test
assertions. Trace the clean upstream behavior of the new source
(`git show <new-source>:<path>`) and the candidate as committed in the
checkout; inspect the composed candidate where cases overlap. Read callers and
callees across the changed boundary, adjacent tests, feature/platform gates,
and relevant maintainer-authored history between the old and new source.
History explains intent; current executable code is the authority for
behavior.

### Required reasoning for every case

Build the following map in the ignored cycle ledger. Identify concrete source
commits, paths and symbols for each claim. A checkbox, commit SHA, test name,
or statement that the code "looks correct" is not a review.

| Question | Required manual analysis |
| --- | --- |
| Original defect and present trigger | Explain the actual input, state transition, race, protocol sequence or package result; trace whether clean new upstream can still reach it. |
| Entry and exit paths | Trace relevant callers, callbacks and consumers, including alternate entries, early returns and failures, not only the regression's route. |
| Ownership and lifetime | Identify the subsystem, thread, process, connection or package responsible for each state/resource; reason through publication, replacement, cancellation and cleanup. |
| Correctness of the candidate | Explain why the case commit establishes each required invariant and does not introduce a new defect in its current surroundings. Inspect ordering, reentrancy, stale callbacks, concurrent teardown, partial initialization, rollback and exception paths where relevant. |
| Compatibility and scope | Check feature toggles, disabled/readonly policies, protocol/platform/build variants, ABI and packaging ownership; justify each changed hunk within the atomic case boundary. |
| Current necessity | Compare the behavior with and without the case commit. Map upstream replacements, redesigns, removed consumers and narrower remaining gaps to exact code, not a similar symptom or commit subject. |
| Queue interaction | Review overlapping paths and shared interfaces in stack order, including semantically related cases which touch different files; identify duplicate fixes, inconsistent ownership and assumptions about another case. |
| Coverage and blind spots | Read what each test actually stimulates and asserts. List important paths/interleavings/configurations it does not cover and reason about them directly; name useful additional regressions without claiming exhaustive coverage. |
| Durable verification plan | State the expected clean-control behavior and the focused/native/package/live observations which will later challenge the conclusion. Distinguish planned checks from results already collected. |

For a genuinely inapplicable dimension, explain why; do not invent concurrency
requirements for a static packaging manifest. Conversely, do not omit a
relevant failure or lifecycle path merely because the existing tests omit it.
Passing tests, textual applicability and previous acceptance are never
substitutes for this analysis. An upstream commit message claiming the same
fix is only a lead.

Review the quarantine commit, while the duty is active, and every declared
upstream test module manually as well: check what is disabled, its isolation
from production fixes, changes in current assertions and their subjects, and
the rationale for each per-leg assignment. Record the candidate disposition
and the exact clean reassessment plan. Actual failing-leg assignments still
require the later three clean gates; neither a code-reading hypothesis nor old
logs can certify them.

### Decide and implement the current case

Conclude the review of every production case with one of:

- `keep`: a specific defect remains reachable without the case commit, and
  every retained hunk is necessary and correct in the new source and complete
  queue;
- `adapt`: a specific residual defect remains, but the old implementation,
  scope or regression is no longer correct/minimal; specify the replacement;
- `retire`: upstream now establishes all required invariants, or the original
  production path no longer exists with no equivalent affected consumer;
  specify the durable regression and gate ownership after removal.

The conclusion must explain both correctness and continued necessity.
Separately record findings, code references, uncovered risks, required
production/test/metadata changes, and planned positive and negative checks.
For `adapt`, also explain why the final revised delta still belongs in the
fork; that the commit still replays is not that explanation. For `retire`,
account for every old invariant, not just the one exercised by a passing test.
Do not keep a redundant case commit only because its tests or a live fixture
are stored with it. A case whose commit Git dropped during the rebase has its
whole change upstream; review it like every other case and retire it. A
residual defect found in that review becomes a new case
([add a case](case-commits.md#add-a-case)).

A test cannot be the reason to skip the decision until later. State a
code-supported conclusion now and label its runtime verification as pending.
If a fundamental ownership or correctness question cannot be established from
the available source, record the exact unresolved question; the review gate
does not pass by changing it to "let the tests decide".

After reaching a code-supported conclusion for this case, implement it now
through the adapt, rebuild, or retirement flow below. Do not defer known
production or regression corrections until other unrelated cases have been
reviewed. Update regressions in the case commit, and manifests, documentation,
dependencies and gate ownership as control work, with their owning atomic
candidate. A `keep` decision with no required changes records the case's
current commit in the ledger; it needs no artificial edit. A retirement
includes its durable coverage migration before the old owner is removed.

Complete the current case README to the mandatory
[documentation standard](case-documentation.md) before closing its checkpoint.
Preserve the depth of existing analysis, update affected explanations against
current callers and tests, and do not replace it with a short applicability
summary. Missing mechanism, ownership or oracle analysis is an unfinished
case review, even when its commit did not need an edit.

When correctness requires another case's interface or overlapping hunk to
change, inspect and repair those consumers in this iteration. Record a bounded
linked set of cases and the shared invariant; fold each case's part into its
own case commit with its own fixup. Never fold a composed change for several
cases into one commit, silently move a hunk between cases, or expand this
dependency review into a read-only sweep of the remaining queue. A linked case
still needs the same full reasoning before its own review is complete. Inspect
partial composition as soon as the relevant commits pass `case-check`;
unrelated `diverged` cases do not require postponing these edits or pretending
that the whole stack already passes.

Re-read the folded case commit in its current surrounding code and recheck
the affected consumers. Resolve each finding or record a code-supported reason
it requires no change. Run applicable static/offline checks, update the
checkpoint below, and only then take the next case. Do not start a runtime test
between these initial case iterations. Material changes reopen exactly the
affected completed reviews; an earlier review of different code does not close
them. All initial decisions, cross-case repairs and regression migrations must
be implemented and re-reviewed before the whole-queue exit gate.

### Persist progress and resume without a new review sweep

Keep the cycle index and per-case working notes under
`.artifacts/fork-maintenance/work/<session>/` (for example `ledger.md` and
`notes/<case>.md`), never in tracked evidence archives. Write down a material
finding, its code references and intended repair when discovered, before
switching to another subsystem or a large source read. Do not rely on
conversation history, an eventual summary, or memory at the end of a long
review. Save an in-progress checkpoint before an interruption or context
handoff, even when the atomic change is not ready to be folded.

The compact cycle index must identify:

- the embedded source and current `develop` tip, active case or bounded linked
  set, and the exact next action/path/symbol;
- each case's status: `pending`, `reviewing`, `implementing`,
  `reviewed-adapted`, `reviewed-unchanged`, or `retired-migrated`; a written
  adaptation plan alone is still `implementing`, never completed review;
- the per-case note path, the old commit from the recorded map and the current
  commit (point-in-time SHAs, ledger only), and whether uncommitted product
  edits or unsquashed fixup commits remain;
- code-supported decisions, addressed/open findings, cross-case consumers
  which must be revisited, and the exact planned controls still pending;
- completed static/offline checks and, after the exit gate, exact runtime
  evidence identities and invalidations. Keep plans distinct from results.

Before leaving a completed case, fold its complete atomic change into its case
commit, run `case-check`, and verify the result with `case-show` and the
per-case range-diff; uncommitted product edits are not a saved adaptation.
Record the resulting commit and metadata changes in the checkpoint. Do not fold
a known incomplete or broken fragment just to obtain a checkpoint: before an
interruption, commit the unfinished edit as an unsquashed `fixup!` commit for
its case, which `develop-check` still rejects, and record its `implementing`
status, pending findings and precise next action. Checkpoints do not authorize
a control commit or a push.

On resumption, read this compact index and the active case's notes first.
Verify the recorded source, `git status`, the current `case-list` and any
unsquashed fixup commits (`git log --oneline <new-source>..HEAD`). A rebase
left in progress is identified from the ledger: continue resolving the refresh
rebase as above; abort an interrupted `develop-squash` or `case-drop` with
`git rebase --abort` and repeat that command. Re-read the specific source
needed for the next edit and any changed consumers. Preserve completed reviews
whose inputs and assumptions still match; reopen only affected ones. Continue
implementing a previously recorded justified decision before reviewing
unrelated remaining cases. Do not fetch or repeat rebase, cleanup, a full
reading sweep, or valid expensive gates merely because context was compacted
or the operator said to continue.

### Close the incremental pass

Once every case has a completed checkpoint, review the resulting composition
and the recorded cross-case interface changes. This is an integration review
of the accumulated candidates and their still-open risks, not a second full
read-only review campaign. Repair and checkpoint any newly affected cases,
require `stack-check` to pass, and record the exit gate below. Only then start
general runtime validation; failures return their owning cases to the same
review/edit/checkpoint loop and the affected regression tests.

## Adapt a case

For a replayed case whose delta must change, edit that case's product files in
the checkout, review the edit, and fold it into the case commit
([change a case](case-commits.md#change-a-case)):

```bash
git diff -- <paths>
git add -- <paths>
git -c commit.gpgsign=false commit \
  --fixup="$(make -s -C fork-maintenance case-commit CASE=<case>)"
make -C fork-maintenance develop-squash
make -C fork-maintenance case-check CASE=<case>
make -C fork-maintenance case-show CASE=<case>
git range-diff <old-sha>^! <new-sha>^!
```

`<old-sha>` comes from the recorded pre-refresh map and `<new-sha>` from
`case-commit`. Use `--fixup=amend:<sha>` instead when the commit message must
change too, for example a subject which no longer describes the narrowed
change; it opens the editor on the old message, so supply the new message
non-interactively and keep the generated `amend!` first line. `develop-squash`
replays every commit above the target, so later case SHAs change; refresh the
current map with `case-list`. If `develop-squash` stops with a conflict, the
edit overlaps another case: run `git rebase --abort` and resolve the overlap in
the case design, never by merging the cases.

A changed set of product paths touched by the case needs the same explicit
review as any other hunk; no manifest field records those paths. Update the
case README, its manifest tests and dependencies as control work outside the
commit. If no change is needed, record the current commit; nothing is edited.
If the case needs no downstream delta at all, follow the retirement path
instead; never keep an empty case commit.

## Rebuild a diverged case

A case recorded as `diverged` or as a placeholder during the rebase, or one
whose review concludes that the replayed implementation no longer fits the new
source, is rebuilt in the checkout on top of its replayed commit. Do not
re-apply the old diff with `git apply --reject`, fuzz or a merge, and do not
use another branch or worktree.

Use the old commit (`git show <old-sha>` from the recorded map) only as a
behavior and regression reference. Implement the entire current candidate in
the product files, not only the formerly conflicting hunks, and fold it into
the case commit exactly as in [Adapt a case](#adapt-a-case):

```bash
git add -- <paths>
git -c commit.gpgsign=false commit \
  --fixup="$(make -s -C fork-maintenance case-commit CASE=<case>)"
make -C fork-maintenance develop-squash
make -C fork-maintenance case-check CASE=<case>
```

The result must be nonempty, carry the case's regressions and pass
`case-check`: independent of the other cases after its declared dependencies,
and removable. A case with declared dependencies is rebuilt in place like any
other; if a safe ownership model cannot be established, report that exact
semantic blocker rather than guessing. An upstream replacement which needs no
downstream delta follows the retirement path instead; drop a placeholder with
`case-drop` rather than keeping it. Re-review the rebuilt commit in full.
Nothing here needs a clean control plane or a control commit, so several
diverged cases can be rebuilt one after another while earlier control-plane
results remain uncommitted.

## Retire a fully replaced case

This deletion procedure applies to production cases only. The reserved
`upstream-test-quarantine` directory, manifest, README and supporting
gates/runbook are permanent infrastructure. When its last upstream failure is
gone, follow the quarantine deactivation in
[`test-quarantine.md`](test-quarantine.md): drop its commit with `case-drop`
and empty `quarantine.modules`, its gate lists and `tests.list` again; never
delete that directory or restore historical skips to keep it active.

Make the retirement decision from the complete current-code analysis above,
then implement the retirement candidate during manual review, before runtime
validation. Final acceptance remains pending: later controls and the resulting
stack must confirm the conclusion or reopen the affected review. This ordering
does not turn an untested retirement into a validated result.

Before deleting the case, preserve its verification boundary under durable,
current ownership. Map every case-owned regression and fixture to an equivalent
upstream test or a retained gate which actually observes the same behavior.
Reading that replacement is part of review; do not infer equivalence from its
name. Plan both clean new-source and resulting-stack checks for every available
focused/native boundary, and the required resulting-stack package/live proof.

The current `CASE=<retired-slug> PATCH_MODE=tests-only` interface cannot select
a retired case. Do not postpone the manual decision or keep a redundant
production delta just to make that command usable. If no equivalent durable
test is selectable after removal, first migrate its tests/fixtures to suitable
maintained ownership and provide a supported provenance-bound selection
through the public Make interface. Add or repair its admission, frozen-source,
inventory and ownership checks and narrow fork-control tests before deleting
the old owner. Do not invent an unsupported mode, retain an archive as a test
authority, or use an ad hoc source probe as acceptance. If the ownership
boundary cannot be established safely, report that exact unresolved review
issue rather than silently dropping coverage.

There is no automatic `case-retire` target. In one reviewed change
([retire a case](case-commits.md#retire-a-case)):

1. migrate all still-required regressions, fixtures and gate inputs as above;
2. remove its slug from every other case's `dependencies`, from every
   case-ownership reference and from any test or gate list which names it;
3. drop the case commit unless Git already dropped it during the rebase;
   `case-drop` refuses while another case still declares the dependency:

   ```bash
   make -C fork-maintenance case-drop CASE=<case>
   ```

4. remove its tracked case directory rather than keeping a historical copy:

   ```bash
   git rm -r -- fork-maintenance/cases/<case>
   ```

5. update active-case lists and documentation, then run `stack-check` and
   manually review the resulting complete stack;
6. record the old/new source, upstream replacement or eliminated path, and
   replacement test ownership in the ignored ledger; execute its planned
   controls and resulting-stack gates only after the manual-review exit gate.

Deletion is a material decision, but the autonomous invocation authorizes this
code-supported retirement and the necessary coverage migration: the agent
drops the case commit itself, and the directory removal and documentation
changes stay uncommitted with the other control-plane results. The retirement
is accepted only after the subsequent durable checks pass. No separate scope
expansion or control commit is authorized or needed.

The current `wayland-client-keymap-sync` case has an additional hard retirement
boundary. Its versioned `tests/live-wayland-keyboard.json` scenario is the sole
input for `live-wayland-keyboard`, and both the runner and job provenance
require that input to be owned by one exact case selection. Removing the case
as-is makes the mandatory stack-wide keyboard gate fail before Xpra starts.
During review, migrate the scenario to durable neutral ownership or an
equivalent generic manifest-declared mechanism, then update the runner,
provenance schema, immutable inventories, mutation tests, contract and live
runbook together. After the review gate, prove the migrated gate with
`STACK=develop`. A redundant production fix is not the long-term owner of a
still-required live input, and an upstream unit test cannot replace it.

## Manual-review exit gate before runtime validation

Record this gate explicitly in the ignored cycle ledger before the first new
Xpra test, quarantine run, native/compiled regression, live profile or real
DEB build. Preparing the test image below also waits for this gate. Offline
fork-control checks, static analysis and ownership/lifecycle preflight are
allowed during review, but prove neither case correctness nor necessity.

The gate passes only when:

- every pre-refresh production case has the full current-code map, equal-depth
  correctness/necessity analysis, and implemented `keep`, `adapt` or `retire`
  conclusion; the quarantine has its manual assessment and clean-gate plan;
- the incremental checkpoints bind the current case commits (or unchanged
  diffs and completed migrations); no review-driven adaptation exists only as
  notes, uncommitted product edits or unsquashed fixup commits;
- all review findings have dispositions, required changes/removals and durable
  regression migrations are complete, and no review is deferred to test output;
- every retained/adapted case commit has been re-read in its final surrounding
  code; every case passes `case-check` and the complete stack passes
  `stack-check`;
- cross-case consumers, compatibility/failure/lifecycle behavior, test blind
  spots and residual runtime risks are explicitly accounted for;
- the CI boundary, source identities, the case map at the gate, exact planned
  controls and replacement gates for retired cases are recorded.

This is the agent's documented reasoning checkpoint, not an existing Make
target or an automated correctness certificate. It closes the accumulated
per-case review-and-implementation records; it does not require a separate
whole-queue review before implementation. There must be a review record
for the whole queue before any runtime run identity is launched; a check
summary or green checks cannot stand in for it. Do not require impossible
exhaustive test coverage, and do not equate untested branches with safe code.

After the gate, execute clean controls and the development tests below, then
freeze the runtime-validated candidate for final acceptance. A contradicting
test, newly found code defect or material candidate change reopens its owner
and affected consumers for manual review and correction before starting
further runtime validation. Update the whole-queue gate for the changed inputs;
unchanged case analyses and exact independent results need not be repeated.
This keeps early live diagnosis and nearest-regression iteration after review,
without allowing them to bypass it.

## Prepare the test image and plan quarantine reassessment

Quarantine steps in this runbook are conditional on an active duty case in
the current queue. If none exists, record that there are no assignments to
reassess and omit the `CASE=upstream-test-quarantine` commands, including its
patched focused check. Still perform image verification and every production,
composed, full-suite, package and live gate. Do not activate the inactive
quarantine merely to run these commands. Preserve its permanent directory,
manifest and README in the inactive state: no commit, empty
`quarantine.modules`, empty gate lists and empty `tests.list`; it is not
selectable. Never delete this infrastructure because all tests pass. If this
cycle proves all assignments obsolete and deactivates the duty, its exact
completed clean/direct results remain the deactivation evidence while
retained; do not test-select the inactive case.

Enter this section only after the recorded whole-queue manual-review exit
gate. Verify the image before the first test which uses it. Reassess quarantine
for the new source and actual image/module/gate inputs before using the duty
case in runtime validation; reading its commit during code review is not a
validated quarantine assignment. Independent reviewed production-case tests
may proceed without waiting for unrelated quarantine results.
Reuse current collected reassessment results when those inputs are unchanged.
The quarantine commit must pass `case-check` before its named clean gates can
start. If it now fails, reopen manual review and rebuild it through the flow
above, keeping only the still-required declared test-module changes. Re-review
the candidate and update the review exit record before returning here. If new
findings make the correct candidate empty, deactivate the duty only through its
documented semantic and clean-test decision, retaining its permanent inactive
directory. Never keep an empty quarantine commit or use an ad hoc diagnostic as
acceptance.

Now verify the input-keyed upstream-test image:

```bash
make -C fork-maintenance test-image
```

The check uses the same exact Python ownership verifier as test startup,
including the current source label, build-run UUID, complete maintenance label
set, input digest, and workflow digest. It must not be a weaker shell-only
label probe. If the image is absent, build it through its named lifecycle. If
the tag instead names an otherwise exact owned image whose sole mismatch is an
older source label, the removal target must prove that label names an existing
Git commit which is an ancestor of the current embedded source. Only then remove
that cache through the locked
`test-image-cache-remove` target, require `test-image` to report absence, and
then use the same named build lifecycle. Any other provenance mismatch is a
hard stop; do not remove or overwrite it.

```bash
make -C fork-maintenance test-image-cache-remove
make -C fork-maintenance test-image
```

For the absent-image branch, inspect the collected status and log, verify the
resulting cache entry, and remove only the transient build ownership:

```bash
make -C fork-maintenance test-image-start \
  IMAGE_RUN=<cycle>-upstream-image-01
make -C fork-maintenance test-image-wait \
  IMAGE_RUN=<cycle>-upstream-image-01
make -C fork-maintenance test-image-status \
  IMAGE_RUN=<cycle>-upstream-image-01
make -C fork-maintenance test-image-logs \
  IMAGE_RUN=<cycle>-upstream-image-01
make -C fork-maintenance test-image
make -C fork-maintenance test-image-remove \
  IMAGE_RUN=<cycle>-upstream-image-01
```

Any failure other than absence or the exact stale-source classification above
requires diagnosis; do not overwrite or delete an unverified cache entry.
Follow [`bootstrap.md`](bootstrap.md) for recovery and abort paths.
Any later change to the image inputs or embedded upstream workflow changes the
image key; repeat this verification/build lifecycle before the next test.

Execute the duty case against clean new production and clean upstream tests in
all three modes, with unique names:

```bash
make -C fork-maintenance test-start \
  CASE=upstream-test-quarantine PATCH_MODE=clean \
  TARGET=quarantine RUN=<cycle>-quarantine-01
make -C fork-maintenance test-start \
  CASE=upstream-test-quarantine PATCH_MODE=clean \
  TARGET=quarantine-cython RUN=<cycle>-quarantine-cython-01
make -C fork-maintenance test-start \
  CASE=upstream-test-quarantine PATCH_MODE=clean \
  TARGET=quarantine-no-compat RUN=<cycle>-quarantine-no-compat-01
```

Wait for, inspect, and remove each job through the matching `test-*` lifecycle.
Each gate runs the complete quarantine module union. Success means its exact
gate-specific subset is the ordered ignored-failure set, every complement
module passes, and there are no unignored failures or skipped modules. An
assigned module which becomes green makes that assignment stale; a complement
failure requires current clean-source diagnosis and an exact new assignment.
The autonomous invocation already authorizes that queue-wide duty update.
Update the manifest lists and the `Fork-Case: upstream-test-quarantine` commit
together (a fixup of that commit and `develop-squash`) as specified in
[`test-quarantine.md`](test-quarantine.md), then complete the clean gates whose
source, environment, module union, or expected subset changed. Every one of the
three final assignments still requires current, exact proof; an unrelated
production-only edit does not require another reassessment.

If any duty module remains, prove that the current quarantine commit itself
applies and its focused module selection is valid:

```bash
make -C fork-maintenance test-start \
  CASE=upstream-test-quarantine PATCH_MODE=patched TARGET=focused \
  RUN=<cycle>-quarantine-patched-focused-01
make -C fork-maintenance test-wait \
  RUN=<cycle>-quarantine-patched-focused-01
make -C fork-maintenance test-status \
  RUN=<cycle>-quarantine-patched-focused-01
make -C fork-maintenance test-logs \
  RUN=<cycle>-quarantine-patched-focused-01
make -C fork-maintenance test-remove \
  RUN=<cycle>-quarantine-patched-focused-01
```

If every declared upstream module is now green, deactivate the duty case as
required by [`test-quarantine.md`](test-quarantine.md) (`case-drop`, then empty
its lists) and omit this case-only patched command. Keep its directory,
manifest and README; the resulting stack-focused and full legs below remain
mandatory.

The review gate and CI-layout repair precede source builds; clean quarantine
proof precedes runtime use of the quarantine commit. Start the live loop
early in this post-review development phase once its focused/native and
complete-stack prerequisites are satisfied, without waiting for the full
upstream matrix. Keep every control-plane repair uncommitted. Any newly
required source or test correction first reopens its affected manual review,
then is folded into its case commit through the adapt or rebuild flow; runners
test committed `HEAD`, so fold it before the next runner start. After the
candidate is stable and frozen, fill missing or invalidated final coverage; do
not repeat the complete offline suite after each adaptation.

## Confirm review decisions with clean controls

### Establish the clean control or documented substitute

This section executes the verification plan recorded before the manual-review
exit gate. It does not make the initial keep/adapt/retire decision.
`PATCH_MODE=tests-only` and `PATCH_MODE=clean` still freeze and validate the
complete diff of the case commit before starting a container. Do not invoke
either command while that case fails `case-check`. For a new finding that the
case no longer fits the source, reopen manual review, rebuild its commit
through the flow above, and update the review exit record before returning to
this control. If a new finding indicates full upstream replacement and an
empty delta, return to the manual retirement and regression migration flow; an
empty case commit is never kept. For an already retired case, use the
replacement owner and supported commands recorded at the review gate, never
the removed `CASE` slug. A `case-check` failure whose cause is not understood
remains a hard stop until source and commit identity are trustworthy.

If the case commit owns one or more `tests/` paths, apply only those tests to
clean new-source production:

```bash
make -C fork-maintenance test-start \
  CASE=<case> PATCH_MODE=tests-only TARGET=focused \
  RUN=<cycle>-<case>-clean-focused-01
make -C fork-maintenance test-wait \
  RUN=<cycle>-<case>-clean-focused-01
make -C fork-maintenance test-status \
  RUN=<cycle>-<case>-clean-focused-01
make -C fork-maintenance test-logs \
  RUN=<cycle>-<case>-clean-focused-01
make -C fork-maintenance test-remove \
  RUN=<cycle>-<case>-clean-focused-01
```

The expected result for a still-needed case is a nonzero test result whose
first failure is the exact retained regression. Inspect it with `test-status`
and `test-logs`; setup, build, import, unrelated, skipped, or differently
failing results are not proof. Run these lifecycle steps as separate
invocations: the expected nonzero `test-wait` must not prevent the subsequent
status, log, and exact remove checks. `test-remove` validates a consistently
recorded failed result. Never hide the expected nonzero result with a shell
fallback.

Also run every declared native/subsystem target with `PATCH_MODE=tests-only`
for each retained or adapted test-owning case, not only the Python-focused
subset. For a retired case, run the equivalent clean and resulting-stack
focused/native checks through the durable ownership and selection migrated
during review. A missing replacement gate reopens review; deletion never waives
a behavioral boundary.

A production case may name an existing upstream focused module but own no
test file. No active case currently does; the retired
`debian-libva-codecs-package` packaging case was the last one. `tests-only` correctly
refuses such a selection, and the focused runner also rejects
`PATCH_MODE=clean`; do not turn either guard failure into a control. Inspect the
clean new upstream packaging, dependency resolution, install ownership and
import paths by reading the code during manual review to justify the necessity
decision. A patched focused run still covers the existing codec helper, but
passing it is not proof that package manifests contain the compiled modules.
The durable proof is
always the two real package builds below against the complete resulting stack:
the retained/adapted stack for keep, or the whole reviewed retirement candidate
for remove. The current DEB runner has no `PATCH_MODE=clean`; if a
clean package comparison is needed, provide that mode and its
provenance/fork-control tests in the review phase rather than bypassing
`deb-policy-check`. If no durable control observes the disputed behavior,
reopen review and strengthen or migrate its test/runner boundary. Do not
present an ad hoc probe as acceptance.

### Reconcile runtime results with the manual conclusions

| Result | Required conclusion path |
| --- | --- |
| `case-check` passes, clean regression fails as intended | Supports the reviewed defect for this tested trigger only; it does not prove every hunk necessary or every path correct. Compare the actual failure with the recorded code reasoning. |
| `case-check` passes, clean regression passes | Contradicts the expected clean-control result. Reopen the manual map: the case commit may be redundant/stale, the environment may miss the trigger, or the regression may be vacuous. Do not retire on this result alone. |
| Commit dropped by Git during the rebase | Exact upstream presence is not a new decision. Confirm the reviewed retirement through its migrated controls and every durable real boundary; neither the drop nor green tests certify the complete behavior. |
| `case-check` fails: not independent or not removable | The reviewed input identity or queue changed; runtime admission must stop. Reopen review and rebuild or restructure the case on the current source; never force, fuzz or merge. |
| `case-check` fails for an unexplained reason | Applicability is not trustworthy. Stop and inspect the commit/source identity before any edit or test claim. |

The code-supported decisions, confirmed or revised after testing, remain:

- retain the case commit unchanged;
- adapt or narrow its production code and regression;
- retire it because upstream safely replaces the complete behavior or removes
  the affected production path, with durable verification preserved.

“It still replays” is not enough for retention, and “the clean test passes” is
not enough for retirement.

## Final post-rebase acceptance

After the manual-review exit gate, post-review development loop and reviewed
candidate freeze, reconcile the ledger against all requirements below. Run
only missing or invalidated checks; do not repeat a valid development result
merely because final acceptance has begun.
The evidence-reuse rules in [`validation.md`](validation.md) retain original
run identities and require exact input proof.

There is no old-base or unchanged-case waiver after rebase: every requirement
must be proved on the new embedded source and final candidate. Stop escalation
at the first unexplained failure, return its owner to development, and stabilize
the correction before scheduling affected final gates with new run identities.

### Offline, clean controls, focused, and native

Run:

```bash
make -C fork-maintenance RUFF=<ruff> check
make -C fork-maintenance ci-layout-check
make -C fork-maintenance stack-check STACK=develop
git diff --check
```

For every retained or adapted case, also run its individual
`case-check CASE=<slug>`. For every retired case, that command must fail
because the case no longer exists; instead, search all current manifests,
stack files, Make targets, and active-case documentation for stale references
to its slug. The expected read-only check, repeated for each retired slug, is:

```bash
rg -n --fixed-strings '<case>' \
  AGENTS.md CLAUDE.md CONTRIBUTING.md pyproject.toml .gitignore \
  .github fork-maintenance
```

Exit status 1 with no output is the expected no-match result; any other nonzero
status is an error. Review every match; no current active reference may remain.

Ensure a valid tests-only focused control for every production case in the
current stack which owns tests, and every native/subsystem target declared by
such a case in `PATCH_MODE=tests-only`;
these are available clean controls after every rebase, not only when retirement
is already expected. Give each case/target pair a distinct `RUN`, inspect its
exact expected regression, and remove it through the ordinary lifecycle. For a
case with no retained test path, record that the tests-only control is
unavailable, perform its documented semantic inspection of clean upstream, and
prove the durable real boundary against the complete resulting stack. Do not
invoke the unsupported `PATCH_MODE=clean TARGET=focused` combination or a
native target absent from that case's manifest. If any clean control passes,
skips, or ceases to reproduce the exact retained regression, return to that
case's already-authorized semantic keep/adapt/retire analysis and update the
manual-review exit gate before further runtime validation. Do the same when a
no-test case's semantic inspection indicates that upstream may now replace its
behavior. Do not freeze or accept the final complete queue until every such
decision is resolved. Independent manual
case analysis may continue; new runtime checks require the updated review gate.
Early live validation remains part of post-review development under
[`validation.md`](validation.md), after focused/native prerequisites.

For every retained or adapted production case, ensure its individual focused
selection passes with the complete case commit. Enumerate the current stack
and use a distinct `RUN` for each missing or invalidated result; do not infer
atomic self-sufficiency from the later stack result:

```bash
make -C fork-maintenance test-start \
  CASE=<slug> PATCH_MODE=patched TARGET=focused \
  RUN=<cycle>-<slug>-patched-focused-01
make -C fork-maintenance test-wait \
  RUN=<cycle>-<slug>-patched-focused-01
make -C fork-maintenance test-status \
  RUN=<cycle>-<slug>-patched-focused-01
make -C fork-maintenance test-logs \
  RUN=<cycle>-<slug>-patched-focused-01
make -C fork-maintenance test-remove \
  RUN=<cycle>-<slug>-patched-focused-01
```

Do not run this `CASE` command for a retired case. If a case declares a
downstream dependency which prevents an atomic case selection from resolving,
use a maintained smallest dependency-complete selection; if no such selection
mechanism exists, stop and close that tooling/test-ownership gap rather than
silently relying only on the full stack. After the keep/adapt/retire decisions,
also ensure one valid focused result for the resulting complete stack; launch
it only if that final requirement is missing or invalidated:

```bash
make -C fork-maintenance test-start \
  STACK=develop PATCH_MODE=patched TARGET=focused \
  RUN=<cycle>-stack-focused-01
make -C fork-maintenance test-wait RUN=<cycle>-stack-focused-01
make -C fork-maintenance test-status RUN=<cycle>-stack-focused-01
make -C fork-maintenance test-logs RUN=<cycle>-stack-focused-01
make -C fork-maintenance test-remove RUN=<cycle>-stack-focused-01
```

For every retained or adapted production case, ensure every declared
native/subsystem target has a valid result with its individual `CASE=<slug>`
selection. In every decision branch, also ensure coverage of the resulting
stack's declared native/subsystem targets. Launch missing or invalidated checks
with unique run identities. The current Wayland examples are:

```bash
make -C fork-maintenance test-start \
  CASE=<slug> PATCH_MODE=patched TARGET=wayland \
  RUN=<cycle>-<slug>-wayland-01
make -C fork-maintenance test-wait RUN=<cycle>-<slug>-wayland-01
make -C fork-maintenance test-status RUN=<cycle>-<slug>-wayland-01
make -C fork-maintenance test-logs RUN=<cycle>-<slug>-wayland-01
make -C fork-maintenance test-remove RUN=<cycle>-<slug>-wayland-01

make -C fork-maintenance test-start \
  STACK=develop PATCH_MODE=patched TARGET=wayland \
  RUN=<cycle>-stack-wayland-01
make -C fork-maintenance test-wait RUN=<cycle>-stack-wayland-01
make -C fork-maintenance test-status RUN=<cycle>-stack-wayland-01
make -C fork-maintenance test-logs RUN=<cycle>-stack-wayland-01
make -C fork-maintenance test-remove RUN=<cycle>-stack-wayland-01
```

Omit an individual `CASE` block only for a retired case, and never run a target
that case does not declare. Apply the same dependency-complete-selection stop
described for focused runs. A subject native module must build, import, and
link; a skip is a failure. Review and remove each collected job before cycle
cleanup.

### Three full upstream legs

After candidate freeze, complete each missing or invalidated full leg with a
distinct run identity. They may execute concurrently when resources allow;
do not start them automatically after each intermediate adaptation:

Only the detached test payloads run concurrently. Collection, abort, and
removal are terminal lifecycle transitions protected by one retained lock, so
run each `test-wait` / review / `test-remove` sequence serially in the order
shown. Do not launch concurrent `test-wait` commands: the extra collectors are
expected to fail closed with `collection or abort is already active`, which is
not test evidence and does not require rerunning an already unchanged payload.

```bash
make -C fork-maintenance test-start \
  STACK=develop PATCH_MODE=patched TARGET=full \
  RUN=<cycle>-full-01
make -C fork-maintenance test-start \
  STACK=develop PATCH_MODE=patched TARGET=full-cython \
  RUN=<cycle>-full-cython-01
make -C fork-maintenance test-start \
  STACK=develop PATCH_MODE=patched TARGET=full-no-compat \
  RUN=<cycle>-full-no-compat-01

make -C fork-maintenance test-wait RUN=<cycle>-full-01
make -C fork-maintenance test-wait RUN=<cycle>-full-cython-01
make -C fork-maintenance test-wait RUN=<cycle>-full-no-compat-01

make -C fork-maintenance test-status RUN=<cycle>-full-01
make -C fork-maintenance test-logs RUN=<cycle>-full-01
make -C fork-maintenance test-remove RUN=<cycle>-full-01

make -C fork-maintenance test-status RUN=<cycle>-full-cython-01
make -C fork-maintenance test-logs RUN=<cycle>-full-cython-01
make -C fork-maintenance test-remove RUN=<cycle>-full-cython-01

make -C fork-maintenance test-status RUN=<cycle>-full-no-compat-01
make -C fork-maintenance test-logs RUN=<cycle>-full-no-compat-01
make -C fork-maintenance test-remove RUN=<cycle>-full-no-compat-01
```

Review every status and complete log before its exact remove command. A foreign
failure is not fixed or skipped in whichever production case happened to be
under review. First reproduce its module on the exact clean source in the same
leg and follow the already-authorized queue-wide quarantine procedure in
`test-quarantine.md`.

### Live preflight

Before the full live suite, create and verify the hash-locked analysis
environment, inspect the host boundary and prove that the complete stack is
committed and composes. All nine scenarios apply that full queue, the diff of
every case commit in `HEAD`, to both endpoints; `CASE`, partial queues and
clean endpoints are forbidden.

```bash
make -C fork-maintenance live-venv
make -C fork-maintenance live-venv-check
make -C fork-maintenance doctor
make -C fork-maintenance isolated-start-check
make -C fork-maintenance stack-check STACK=develop
```

Do not start a live wrapper if this preflight fails. `doctor` reports optional
hardware and input-path availability, but a selected live gate which requires
one of those paths still fails closed when it is unavailable. Reuse this
verified environment for all nine complete-stack profiles;
do not recreate it between otherwise unchanged runs.

### Case-owned real boundaries, complete-stack execution

Case manifests identify behavioral owners of live assertions, not isolated
product selections. Every current profile executes with the complete queue on
both endpoints, including clipboard, subsurface, keyboard and hardware tests.
A new or retired case must update its regression ownership without creating a
single-case live path or dropping the profile from global coverage.

Use the live loop of
[`live-tests.md`](live-tests.md#the-live-loop-fix-and-continue-then-one-complete-pass)
for any case validation. A product fix is folded into its case commit before
the continuation starts, because the runner freezes committed `HEAD`:

```bash
make -C fork-maintenance live-all STACK=develop RUN=<cycle>-live-01
# gate G failed: diagnose, fold the fix into its case commit, prove offline,
# then continue from G
make -C fork-maintenance live-remove RUN=<cycle>-live-01-G
make -C fork-maintenance live-all STACK=develop RUN=<cycle>-live-02 FROM=G
# ... until the last gate passes; then one complete pass
make -C fork-maintenance live-all STACK=develop RUN=<cycle>-live-03
make -C fork-maintenance live-suite-check STACK=develop RUN=<cycle>-live-03
```

The suite orders clipboard/subsurface first and runs every profile through its
named start/wait/remove lifecycle. A failed gate stops the controller: fix it
and continue from that gate under the next prefix instead of restarting the
whole set, until the last gate passes. Then run one complete pass; if one of
its gates fails, fix and continue the same way and finish with another complete
pass. Only a complete pass without a fix is validated by `live-suite-check`;
never combine results from different source/queue/harness candidates. Record
every prefix, failure, classification and fix in the cycle ledger.

### All nine positive live profiles

The complete `live-all` pass above is the mandatory nine-profile matrix. Do not
repeat it as a separate case-selected ladder or count a subset as full coverage.
Every member must have positive application, transport, hardware, lifecycle and
owned-cleanup evidence. Missing hardware, application input or environment
leaves the refresh incomplete, never skipped. Use only the named `live-*`
lifecycle commands for recovery; no direct destructive Podman commands.

For detach and transport loss, review the three identical fixture-owned
application identity snapshots (capture, post-disconnect, and pre-termination),
their exact Python/script argv, PID, procfs start ticks, and command-line
digest. Require the same three snapshots for the server identity, with its PID
equal to `server_pid` and distinct from the fixture PID. The termination record
must bind both unchanged identities and both pidfds; its one in-container probe
must reject an exited or zombie server, double-snapshot both processes, poll
the server pidfd immediately before sending `SIGTERM` only through the fixture
pidfd, then observe the exact fixture gone and only afterward the exact server
gone. Plain survival booleans or a process-name match are invalid: the Xpra
server argv itself contains the `--start-child` command and therefore matches
a naive `pgrep --full interaction_fixture.py` search.

## Final audit and handoff

After every case decision is reflected in the queue and all jobs are reviewed:

```bash
make -C fork-maintenance case-list
make -C fork-maintenance stack-check STACK=develop
make -C fork-maintenance ci-layout-check
make -C fork-maintenance RUFF=<ruff> check
git log --oneline <new-source>..HEAD
git diff --check
git status --short --branch
```

Precede this block with `make -C fork-maintenance case-check CASE=<case>` for
every retained or adapted case. For every retired case, record the reviewed
`rg` no-stale-reference result and the successful resulting `stack-check`
instead. The log must show no unsquashed `fixup!`/`amend!` commit.

If no control-plane change is uncommitted, for example because every case was
retained unchanged and the rebase needed no new control-plane repair, run the
final branch gate now:

```bash
make -C fork-maintenance develop-check
```

If retirement, quarantine, CI-layout, manifest, README, or documentation
changes are uncommitted, `develop-check` must instead remain outstanding. This
is expected: do not create a control commit merely to make it pass. Case
changes are already stored in their case commits; leave the complete reviewed
control-plane diff for the operator, who decides whether and how to commit it
after the handoff.

The handoff must state:

- old and new fork-master/source/develop commits;
- the clean pre-rebase state and any separately instructed prerequisite Git work;
- rewritten commit range, every rebase conflict resolution (commit and hunks),
  every `diverged` or placeholder case, and every case commit Git dropped;
- the whole-queue manual-review exit record before the first runtime run,
  including any reopening for later findings or changed candidates;
- each case's old and new commit from the recorded and final case maps with its
  per-case range-diff, equally detailed correctness/necessity map, uncovered
  risks, code-supported keep/adapt/retire conclusion, implemented changes and
  durable test ownership after retirement;
- the subsequent clean-control or documented no-test evidence, and how runtime
  results confirmed or changed the earlier manual conclusions;
- quarantine reassessment and any assignment changes;
- focused/native, package, full-leg, all nine complete-stack live
  run identities/results;
- any incomplete gate or missing authority;
- the exact final staged, unstaged, and untracked status and why
  `develop-check` is outstanding when the control plane is dirty;
- that local `develop` history was rewritten and must be published by the
  operator with `--force-with-lease`, and that no remote ref was changed by the
  agent.

After all collected jobs have had their exact remove targets run, write the
refresh session record before the handoff: the old/new source commits, one
line per case with its decision, how its commit changed and the key reason,
cross-case findings, gate outcomes, rebase conflicts and the pitfalls the next
refresh should check first. Then close the session:

```bash
make -C fork-maintenance knowledge-index
make -C fork-maintenance artifacts-close-plan
make -C fork-maintenance artifacts-close CONFIRM=<artifacts_close_confirm>
make -C fork-maintenance artifacts-close-check
```

Results remain ignored local state until that reviewed close; never copy
them into Git. Report the session record path in the handoff; the run
identities it names are deleted and cannot be reused. Publication is a separate
operator operation: the rebase and every case rewrite changed `develop`
history, so the operator force-pushes it with the exact-SHA
`--force-with-lease` procedure in [`publish-develop.md`](publish-develop.md)
after the refresh, and again after any later case rewrite. The agent never
pushes.
