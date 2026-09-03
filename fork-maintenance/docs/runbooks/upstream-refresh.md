# Rebase Develop And Reassess One Case

## Purpose

This is the canonical end-to-end runbook for an operator-selected upstream
refresh. Use it when the operator has synchronized `kogeler/xpra:master` from
`Xpra-org/xpra:master`, has explicitly chosen that newer fork-master commit as
the next source boundary, and has named one production case for detailed
reassessment.

The agent may fetch and verify both master refs, fast-forward the local
`master`, and rebase local `develop` onto that verified fork `master`. The agent
must not dispatch the hosted sync workflow, run `gh repo sync`, push or
force-push a ref, or change the default branch. A remote mismatch returns the
workflow to the operator; it is not repaired locally.

Naming one primary case does not limit post-rebase acceptance to that case.
Moving the embedded source invalidates every previous functional result, so the
complete queue must resolve and pass every available tests-only control,
focused/native tests, three full upstream legs, case-specific package
boundaries, and all seven live profiles. The primary case receives the deeper
semantic keep/adapt/retire review described below.

This document describes the workflow but does not itself authorize a refresh.
A task must explicitly choose the new base. Once the operator tells an agent to
execute this runbook, that invocation authorizes one local preservation commit
at the very beginning when legitimate non-ignored changes already exist. The
agent makes that one start commit without another confirmation. It does not
authorize any later content commit: every adaptation, retirement, quarantine,
CI, documentation, or runner result produced by the refresh remains
uncommitted for operator review.

## Inputs and reading

Choose before starting:

- `CASE`: one production slug currently selected by `stacks/develop.toml`;
- `CYCLE`: one never-reused lowercase prefix for every `RUN`, `IMAGE_RUN`, and
  `WORKSPACE` created by this refresh;
- the operator's confirmation that remote fork `master` was synchronized and
  that this refresh should move `develop` now.

Replace placeholders such as `<case>` and `<cycle>` in every example; never
pass the angle brackets literally.

Read completely before changing anything:

1. root `AGENTS.md` and `fork-maintenance/AGENTS.md`;
2. `fork-maintenance/CONTRACT.md` and this runbook;
3. `stacks/develop.toml` and every active `cases/<id>/case.toml` and
   `README.md`;
4. the selected case's complete `fix.patch`, plus every patch and surrounding
   source boundary which overlaps its owned paths;
5. `CLAUDE.md`, `CONTRIBUTING.md`, `.github/upstream-workflows/test.yml`, and
   `pyproject.toml`;
6. the current source, adjacent tests, and recent maintainer-authored history
   for every selected production path;
7. [`bootstrap.md`](bootstrap.md),
   [`isolated-workspaces.md`](isolated-workspaces.md),
   [`patch-cycle.md`](patch-cycle.md),
   [`test-quarantine.md`](test-quarantine.md),
   [`upstream-tests.md`](upstream-tests.md),
   [`live-tests.md`](live-tests.md), and, when applicable,
   [`deb-packages.md`](deb-packages.md);
8. [`cycle-cleanup.md`](cycle-cleanup.md) and
   [`publish-develop.md`](publish-develop.md).

Resolve one reviewed Ruff executable before the first control-plane check and
record its version. `<ruff>` below is its absolute path. A system `ruff` is
valid; when it is absent, an operator-provisioned executable such as
`.artifacts/fork-maintenance/tooling-venv/bin/ruff` is also valid after its
ownership and executable-file boundary are reviewed. The optional tooling venv
is not created by this runbook and is not acceptance evidence.

If the retained artifact inventory contains an owner from the retired
`xpra-lab-*` namespace, also read the historical
[`../namespace-migration.md`](../namespace-migration.md) completely before
classifying it. Current lifecycle readers intentionally have no compatibility
mode for that namespace.

If `<case>` is `upstream-test-quarantine`, use `test-quarantine.md`; it is a
temporary test duty, not a production behavior eligible for the decision tree
below.

## One start commit and the clean boundary

Start on `develop` with no merge, rebase, cherry-pick, or revert in progress.
Before touching a ref, inspect every staged, unstaged, and untracked non-ignored
path. Do not stash, reset, clean, or discard existing work. Reject unresolved
conflicts, secrets, generated artifacts, an applied or hand-edited Xpra source
copy, and any file whose ownership or intent is uncertain. Ignored runtime
state is never staged. `isolated-start-check` must prove that every legitimate
change stays inside the allowed fork-control boundary.

If legitimate non-ignored changes exist, this runbook requires the agent to
preserve all of them in exactly one local start commit without asking again.
Review and validate the complete candidate first:

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

Inspect the contents of every listed untracked file separately; a filename is
not sufficient review. Fix any in-scope preflight defect before snapshotting.
Then, if and only if the porcelain is nonempty, stage the complete reviewed
non-ignored state, prove nothing was omitted, and create one commit with a
concise subject describing that preserved work:

```bash
git add --all -- .
(
set -eu
git diff --quiet
test -z "$(git ls-files --others --exclude-standard)"
git diff --cached --check
git diff --cached --stat
git diff --cached --name-status
git diff --cached
)
git commit -m '<reviewed start-snapshot subject>'
test -z "$(git status --porcelain=v1 --untracked-files=all)"
git rev-parse HEAD
```

If the checkout began clean, do not create an empty commit. In either branch,
record the start-commit SHA or `<none>`, then require clean porcelain. This is
the runbook's only autonomous content commit. From this point onward do not
commit or amend any refresh result, even if a later clean-host-only operation
would otherwise be convenient; use the isolated reconstruction flow or stop
with an exact handoff.

## Pre-refresh record

Record in the handoff notes, without creating a tracked evidence file:

- the pre-preservation `develop` commit and the one start-commit SHA, or
  `<none>`;
- old `develop` commit which will enter the rebase (the start-commit SHA when
  one was required, otherwise the pre-preservation commit);
- old embedded source merge base;
- local and cached fork-master commits;
- selected case patch SHA-256 and complete stack resolution digest;
- current branch/status;
- the chosen case and cycle identifiers.

Use commands which do not change refs or tracked source, and require exactly
one old embedded source commit. Fork-control checks may update only ignored
tool caches:

```bash
(
set -eu

test "$(git branch --show-current)" = develop
test -z "$(git status --porcelain=v1 --untracked-files=all)"
git status --short --branch
git branch --show-current
git rev-parse HEAD
git rev-parse refs/remotes/origin/master
set -- $(git merge-base --all refs/remotes/origin/master HEAD)
test "$#" -eq 1
printf 'old_source=%s\n' "$1"
if git show-ref --verify --quiet refs/heads/master; then
  printf 'local_master=%s\n' "$(git rev-parse refs/heads/master)"
else
  result=$?
  test "$result" -eq 1
  printf '%s\n' 'local_master=<missing>'
fi
sha256sum fork-maintenance/cases/<case>/fix.patch
make -C fork-maintenance list
make -C fork-maintenance repo-status
make -C fork-maintenance stack-check STACK=develop
make -C fork-maintenance RUFF=<ruff> check
)
```

Local `master` may legitimately be absent in a new checkout; `repo-status`
does not report this local ref, so the explicit conditional above records either
its exact commit or its absence. `master-update` creates the branch after remote
equality is proven. Do not turn its absence into a pre-refresh failure.

There is no single global runtime-status target. Before rebasing, perform a
bounded read-only inventory of the exact ownership roots. Skip a root only when
it is absent; a symlink, non-directory root, or unreadable root is a hard stop:

First establish exclusive maintenance coordination for this checkout and its
artifact tree: no other operator, agent, or automation may start a test, live,
DEB, image, live-environment, case-update, workspace, or cleanup lifecycle from
this point through the rebase and initial post-rebase resolution. The
per-subsystem locks serialize individual transitions but this cross-subsystem
scan cannot hold them as one atomic lease; without that external quiescence
guarantee, stop instead of treating an instantaneously empty scan as stable.

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
  "$state_root/case-staging" \
  "$state_root/case-updates" \
  "$state_root/cycle-cleanups" \
  "$state_root/namespace-migration" \
  "$state_root/workspace-fingerprints" \
  "$state_root/upstream-tests" \
  "$state_root/upstream-tests/runs" \
  "$state_root/upstream-tests/logs" \
  "$state_root/upstream-tests/image-builds" \
  "$state_root/upstream-tests/sources" \
  "$state_root/upstream-tests/workspaces" \
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

for state_path in \
  "$state_root/case-staging" \
  "$state_root/case-updates" \
  "$state_root/cycle-cleanups" \
  "$state_root/upstream-tests/workspaces" \
  "$state_root/workspace-fingerprints"; do
  if test -L "$state_path"; then
    printf 'unsafe state root: %s\n' "$state_path" >&2
    exit 1
  elif test -e "$state_path"; then
    test -d "$state_path"
    find "$state_path" -xdev -mindepth 1 -maxdepth 2 \
      ! -name '*.lock' -print
  fi
done

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
whose name/ID occurs in a current record, retained removal transaction, or the
reviewed namespace-migration plan. Inspect each selected image or volume too.
Every selected runtime object must map one-to-one to the exact identity and
complete maintenance label set in one canonical, validated current owner,
prelaunch, abort, or removal authority found above. Use the matching lifecycle
reader to validate that record and route it; labels or a familiar name alone
are never authority. An unmatched, duplicate, mislabeled, or multiply claimed
object is an orphan and stops the refresh—do not call `podman rm` or
`podman network rm` directly. After all authorized routing, require the
complete container and network listings to contain no maintenance runtime
object.

Maintenance-labelled images and the upstream ccache volume are reusable
caches rather than active runtime. Inspect and retain current-namespace cache
objects, and let the owning image/cache checks validate their exact complete
labels and immutable identity when they are next selected; a malformed or
unattributed maintenance cache stops the refresh. Do not delete a valid cache
merely because its source label predates the rebase. Apply the stricter exact
absence/foreign-image exception below to every legacy-labelled image or volume
selected from the unfiltered listings.

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
- `namespace-migration/` itself is a non-symlink current-uid `0700` directory
  containing exactly the two non-symlink, current-uid, single-link `0600`
  Strategy-A files below, with no partial or extra entry, and they have the
  tracked historical SHA-256 identities:

```bash
test "$(sha256sum \
  .artifacts/fork-maintenance/namespace-migration/strategy-a-remove.json \
  | cut -d' ' -f1)" = \
  8f182545ba206260feeaa407289cd16c21d59e90c5764dc2a44b1717f97ff2b1
test "$(sha256sum \
  .artifacts/fork-maintenance/namespace-migration/strategy-a-remove.complete.json \
  | cut -d' ' -f1)" = \
  4ac27239fd46d57fd2a103f27968913778745192a89e2d9e90d0440ef0596045
```

- the plan and completion both bind confirmation
  `162f862f94028f63a36ff536d140e1bf0af919b485921902bba23807f86f984f`;
  the completion binds the actual transaction digest, its 66 unique removed
  image IDs exactly equal the plan's owned image IDs with counts 17 DEB, 40
  live, and 9 upstream-test, and its removed volume is exactly the plan's
  `xpra-lab-upstream-ccache`;
- a fresh read-only Podman inventory finds none of those 66 IDs, no retired
  maintenance-owned container, network, volume, name, or `io.xpra.lab.*`
  label, and no old ccache volume. The plan's foreign image may be absent; if
  present, it is the sole allowed old-label result and must have exact ID
  `463f2603e257a7dd29c5fb5c03295902a8189a673002a2ea38117756417569b7`,
  recorded tag, and filtered retired labels. In addition to the unfiltered
  listing, call read-only `podman image exists <immutable-id>` for each of the
  66 exact plan IDs and require its documented absent result; a display listing
  alone is not absence proof;

Compare the JSON structures and live Podman inspection, not display-formatted
text. Any other retired owner, location, kind, field set, digest, runtime
record, or Podman identity is unresolved and stops the refresh. This exception
does not treat old logs as current acceptance; the exact completed cutover plus
the fresh absence audit proves only that their retired runtime cannot still be
active.

Inspect a finalized workspace with `workspace-status`; resolve only
marker-backed workspace state with `workspace-recover`. Resolve case
creation/update state only with `case-recover CASE=<id>`. A pending cycle-clean
transaction must be resumed with its original reviewed `CYCLE` and confirmation
digest. `live-venv` owns exact recovery of its environment partial. For an
upstream foreground/bundle partial or a DEB source, selection, or validation
partial, use only the exact recovery route in the owning upstream/live/DEB
runbook; if no unambiguous public route applies, stop. Use a matching collect,
remove, or abort interface only after its owning runbook authorizes that
transition. Never delete a marker, workspace, process, or container by hand.
If an identifier is ambiguous or belongs to another unfinished work cycle,
stop for operator review.

Do not carry a finalized workspace across the rebase: its metadata is bound to
the old host `HEAD`. Inspect it with both `workspace-status` and the exact
staged candidate from `workspace-diff`; the read-only commands explicitly
support `host_identity=stale`. Prove that the candidate contains no unexported
work before using `workspace-remove`, or stop if it belongs to another
unfinished cycle. Mutating stage/update operations remain forbidden for stale
identity. After resolving any marker-backed state, repeat the inventory and
require no unresolved printed runtime, transaction, partial, owner, prelaunch,
staging, or workspace entry to remain. Retained removal transactions count as
resolved only after their applicable current validation route above has
passed.

Do not start the rebase with an unexplained offline failure, ambiguous merge
base, merge commit in the downstream range, host Xpra source change, active
case/workspace transaction, or unreviewed runtime owner.

## Verify fork master and rebase develop

The operator's hosted workflow changes remote fork `master`; it does not update
this checkout. Fetch and verify the result:

```bash
make -C fork-maintenance repo-sync
```

`repo-sync` must prove that cached and live `origin/master` and
`upstream/master` are all the same commit. If the fork is stale, ahead,
divergent, missing, or moves during verification, stop. Return the refresh to
the operator, who owns the hosted master-sync workflow, then repeat this gate
only after that workflow has completed successfully.

With equality proven, update only local `master`, switch to `develop`, and
rebase:

```bash
make -C fork-maintenance master-update
git switch develop
make -C fork-maintenance develop-rebase
```

Never substitute `upstream/master` for the verified fork `master`, and never
merge either master ref into `develop`.

If rebase stops, inspect the current commit, both sides of every conflict, and
the new upstream source. Resolve and stage only conclusions that are certain,
then run `git rebase --continue` until complete. In particular, preserve
canonical upstream workflow changes as byte-identical disabled renames and
keep fork-only executable workflows separate. Never skip a fork commit merely
to make the rebase finish. If the correct resolution is uncertain, run
`git rebase --abort`, stop the refresh, and report the exact conflict.

After a successful rebase, record the new `develop` and embedded source commits
and compare the replayed downstream series with the captured old series using
`git range-diff`:

```bash
git range-diff \
  <old-source>..<old-develop> \
  <new-source>..HEAD
```

The rebase changes commit identities even when patch content is unchanged.

The authority and source context read before the rebase belonged to the old
embedded source. Before resolving a patch or making any post-rebase edit,
re-read the current `AGENTS.md`, `fork-maintenance/AGENTS.md`,
`fork-maintenance/CONTRACT.md`, `CLAUDE.md`, `CONTRIBUTING.md`,
and `pyproject.toml` completely. At this point the disabled workflow copy has
not yet passed its post-rebase byte-identity gate, so read the canonical
workflow directly from the recorded new source rather than trusting that copy:

```bash
git show <new-source>:.github/workflows/test.yml
```

Re-read the active manifests and owning runbooks if the replay or a conflict
changed them, then inspect the new surrounding source, adjacent tests, and
current maintainer-authored history for every path being reassessed.
New-source authority supersedes the pre-refresh copy and old handoff notes.

Before making any new tracked edit, prove the clean rebased branch and inspect
each case separately so a first stack failure does not hide later status:

```bash
make -C fork-maintenance patch-start-check
make -C fork-maintenance patch-check CASE=<case>
```

Run `patch-check` for every case in current stack order. Run `stack-check` only
after every individual case resolves. `apply` and exact `already-present` are
the only acceptable resolver states; `diverged` or `ambiguous` requires review
and adaptation before the queue can be accepted.

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
image or starting any test. Keep it uncommitted as a refresh result; subsequent
case work must use isolated workspaces and must not require a clean host or a
second content commit.

## Queue-wide consequence and scope stops

An upstream rebase is not a single-patch operation even when one case is the
primary question. Before complete-stack heavy tests:

- every active case must be `apply` or exact `already-present` against the new
  source;
- all overlapping cases must still compose in declared order;
- stale quarantine entries must be removed or narrowed;
- any changed upstream workflow boundary must be reconciled;
- no applied production source may remain in host `develop`.

If an unrelated case diverges and the task authorizes only the named case,
stop and request expanded queue-adaptation scope. Do not silently change an
unrelated production patch and do not run the expensive final matrix on an
unresolvable stack.

The same scope stop applies when an unrelated production case's clean control
now passes, skips, or no longer observes its claimed defect. That result makes
the case's necessity part of the refresh decision: repeat the semantic mapping
and keep/adapt/retire analysis for that case under explicit authority before
the full or live matrix. Do not preserve a potentially redundant or vacuous
patch merely because it still applies.

Every applicable or diverged case is inspected and updated one at a time in an
isolated workspace, even while reviewed fork-control results are uncommitted.
No case adaptation may consume the clean host source/index boundary. A
diverged case uses the provenance-bound `PATCH_MODE=reconstruct` flow below;
never fall back to an intermediate commit merely to make the next case
possible.

## Prepare the test image and reassess quarantine

Do this before adapting the selected production case. The quarantine must
resolve before its named clean gates can start. If its old patch is `diverged`,
use the isolated reconstruction flow below to preserve only the still-required
declared test-module changes, publish that complete candidate, and then return
here. If current upstream makes the correct candidate empty, retire the duty
case only through its documented semantic and clean-test decision; do not
manufacture an empty patch or use an ad hoc diagnostic as acceptance.

Now verify the input-keyed upstream-test image:

```bash
make -C fork-maintenance test-image
```

If and only if the expected image is missing, build it through its named
lifecycle, inspect the collected status and log, verify the resulting cache
entry, and remove only the transient build ownership:

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

Any failure other than absence of the expected digest-keyed image requires
diagnosis; do not overwrite or delete an unverified cache entry. Follow
[`bootstrap.md`](bootstrap.md) for recovery and abort paths.
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
These gates are inverted: success means the exact declared module set still
fails after a successful build. A listed module which becomes green makes the
quarantine stale and must be removed or narrowed, after which all three clean
gates run again.

If any duty module remains, prove that the current quarantine patch itself
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

If every declared upstream module is now green, retire the duty case as
required by [`test-quarantine.md`](test-quarantine.md) and omit this case-only
patched command. The resulting stack-focused and full legs below remain
mandatory.

Complete any quarantine or CI-layout repair before production adaptation. Run
its individual resolution and available case-only gates as soon as they are
meaningful, but keep every repair uncommitted. Later case work continues through
the isolated applicable/reconstruction flows without touching the host source
or index. The complete `check` and `stack-check` remain mandatory after every
case resolves; do not claim them early while the queue is still divergent.

## Reassess the selected case semantically

Applicability is not a necessity decision. For the old and new source commits,
inspect the selected manifest paths and relevant upstream history. Build an
explicit map of:

| Question | Required evidence |
| --- | --- |
| Trigger | The real input, state, race, packaging result, or protocol sequence which exposed the defect. |
| Entry points | Every production caller and callback that can reach the changed code. |
| Ownership | The subsystem, thread, process, connection, or package which owns the state and cleanup. |
| Invariants | Ordering, rollback, compatibility, bounds, lifecycle, and failure behavior required by the case README. |
| Upstream change | Exact new code and maintainer history that preserves, narrows, replaces, or conflicts with each invariant. |
| Regression authority | A retained clean control that fails for the intended reason, or the documented semantic inspection when no clean real-boundary mode exists, followed by a patched or upstream-replaced candidate which passes the durable real boundary. |
| Real boundary | Each declared native, package, and live observation required by the case. |

Current source and maintainer-authored history outrank old patch context,
commit subjects, prior logs, or similarity of function names. Review adjacent
callers and tests, not just changed lines. An upstream commit message saying it
fixed the same symptom is a lead, never retirement evidence.

### Establish the clean control or documented substitute

`PATCH_MODE=tests-only` and `PATCH_MODE=clean` still validate the complete
selected case patch before starting a container. Do not invoke either command
while the selected case is `diverged` or `ambiguous`. For `diverged`, first
reconstruct and publish a nonempty current candidate as described below, then
return to this control. If upstream appears to have replaced the behavior so
completely that the correct candidate is empty, `patch-update` cannot publish
it and the current resolver cannot select its retained tests. Before retirement,
add a provenance-preserving diagnostic mode or migrate the regression into an
equivalent durable retained gate. `ambiguous` remains a hard stop until source
and patch identity are trustworthy.

If the production patch owns one or more `tests/` paths, apply only those tests
to clean new-source production:

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

The expected result for a still-needed patch is a nonzero test result whose
first failure is the exact retained regression. Inspect it with `test-status`
and `test-logs`; setup, build, import, unrelated, skipped, or differently
failing results are not proof. Run these lifecycle steps as separate
invocations: the expected nonzero `test-wait` must not prevent the subsequent
status, log, and exact remove checks. `test-remove` validates a consistently
recorded failed result. Never hide the expected nonzero result with a shell
fallback.

Before retiring a case which declares a native/subsystem target, repeat that
target with `PATCH_MODE=tests-only` while the retained tests still exist. This
checks clean new-source production through the real native build/import
boundary, not only its Python-focused subset.

Some production cases, currently `debian-libva-codecs-package`, name an
existing upstream focused module but own no test file. `tests-only` correctly
refuses such a selection, and the focused runner also rejects
`PATCH_MODE=clean`; do not turn either guard failure into a control. Inspect the
clean new upstream packaging, dependency resolution, install ownership, and
module imports directly before making a necessity hypothesis. A patched
focused run still covers the existing codec helper, but passing it is not proof
that package manifests contain the compiled modules. The durable proof is
always the two real package builds below against the complete resulting stack:
the retained/adapted stack for keep, or the whole reviewed retirement candidate
for remove. The current DEB runner has no `PATCH_MODE=clean`; if a
pre-retirement clean package experiment is needed, add that mode and its
provenance/fork-control tests rather than bypassing `deb-policy-check`. If no
durable control observes the disputed behavior, strengthen the retained case
test or runner before retirement. Do not present an ad hoc probe as acceptance.

### Interpret the resolver and control together

| Result | Required conclusion path |
| --- | --- |
| `apply`, clean regression fails as intended | The patch is still needed in some form. Review the applied code in its new surroundings, then retain unchanged only if every invariant and test remains correct. |
| `apply`, clean regression passes | The patch may be redundant, stale, or its regression may be vacuous. Map every invariant to upstream code and strengthen the test if necessary before choosing retirement or narrowing. |
| `already-present` | Upstream contains the exact patch diff, but retirement is still deliberate. Verify current callers, every available tests-only focused/native control or documented no-test substitute, and every real boundary before removing the case. |
| `diverged` | Upstream changed an owned boundary. Reconstruct the complete candidate on the new source; never force, fuzz, use rejects, or preserve old text mechanically. |
| `ambiguous` | Applicability is not trustworthy. Stop and inspect the patch/source identity before any edit or test claim. |

The legitimate final decisions are:

- retain the patch unchanged;
- adapt or narrow its production code and regression;
- retire it because current upstream fully and safely replaces the complete
  behavior.

“It still applies” is not enough for retention, and “the clean test passes” is
not enough for retirement.

## Refresh an applicable patch

For an `apply` case, use the default isolated flow so production changes never
touch the host source or index. The same flow supports a nonempty new downstream
delta for `already-present`; its workspace begins at the upstream tree which
already contains the old exact diff:

```bash
make -C fork-maintenance isolated-start-check
make -C fork-maintenance workspace-create \
  CASE=<case> WORKSPACE=<cycle>-<case>-adapt-01 PATCH_MODE=patched
make -C fork-maintenance workspace-status \
  WORKSPACE=<cycle>-<case>-adapt-01
make -C fork-maintenance workspace-diff \
  WORKSPACE=<cycle>-<case>-adapt-01
```

Inspect the candidate in its current surrounding code. If no change is needed
for an `apply` case, remove the workspace; the stored patch remains unchanged.
If an `already-present` case needs no downstream delta, follow the deliberate
retirement path instead of trying to export an empty patch.

When adaptation is required, edit only below the printed workspace `source`
path, then stage, review, export, and remove the complete atomic candidate:

```bash
make -C fork-maintenance workspace-stage \
  WORKSPACE=<cycle>-<case>-adapt-01
make -C fork-maintenance workspace-diff \
  WORKSPACE=<cycle>-<case>-adapt-01
make -C fork-maintenance workspace-update \
  WORKSPACE=<cycle>-<case>-adapt-01
make -C fork-maintenance workspace-status \
  WORKSPACE=<cycle>-<case>-adapt-01
make -C fork-maintenance workspace-remove \
  WORKSPACE=<cycle>-<case>-adapt-01
```

Use `ALLOW_PATH_CHANGE=1` on both `workspace-stage` and `workspace-update` only
after reviewing a genuinely changed ownership set. This is normally required
when an `already-present` patch becomes a smaller new delta. `workspace-update`
derives `fix.patch`, `patch_sha256`, and `paths`; never edit those fields
manually. Update the case README outside the workspace. Recover an interrupted
workspace operation only with `workspace-recover`, and an interrupted export
only with `case-recover CASE=<case>`, as specified in
[`isolated-workspaces.md`](isolated-workspaces.md).

## Reconstruct a diverged candidate

`patch-apply` and ordinary patched workspace creation intentionally reject a
completed case whose old patch is `diverged`. Do not retry them and do not use
`git apply --reject`, fuzz, or a host-worktree reconstruction.

Use the workspace-only reconstruction mode. It is valid only for exactly one
completed independent case with no dependencies whose current patch is
provably neither forward- nor reverse-applicable. It binds the old patch,
manifest, path set, selection, and source identities, copies clean embedded
source, and applies no old patch. If the diverged case has dependencies, stop
and first design a dependency-aware atomic boundary instead of bypassing the
guard or reconstructing it against an incomplete source:

```bash
make -C fork-maintenance isolated-start-check
make -C fork-maintenance workspace-create \
  CASE=<case> WORKSPACE=<cycle>-<case>-reconstruct-01 \
  PATCH_MODE=reconstruct
make -C fork-maintenance workspace-status \
  WORKSPACE=<cycle>-<case>-reconstruct-01
make -C fork-maintenance workspace-diff \
  WORKSPACE=<cycle>-<case>-reconstruct-01
```

Use the old patch only as a behavior and regression reference. Implement the
entire current candidate below that workspace's printed `source` path, not only
the conflict hunks. Stage, review, export, and remove it through the same atomic
workspace transaction:

```bash
make -C fork-maintenance workspace-stage \
  WORKSPACE=<cycle>-<case>-reconstruct-01
make -C fork-maintenance workspace-diff \
  WORKSPACE=<cycle>-<case>-reconstruct-01
make -C fork-maintenance workspace-update \
  WORKSPACE=<cycle>-<case>-reconstruct-01
make -C fork-maintenance workspace-status \
  WORKSPACE=<cycle>-<case>-reconstruct-01
make -C fork-maintenance workspace-remove \
  WORKSPACE=<cycle>-<case>-reconstruct-01
```

The export must be nonempty, must revalidate unchanged old case provenance and
host identity, and must produce a normal forward-applicable, reverse-rejecting
case. It atomically updates the patch/manifest and flips the retained workspace
to ordinary `patched` mode before removal. Use `ALLOW_PATH_CHANGE=1` on both
stage and update only when complete semantic review justifies a changed path
set. An upstream replacement which needs no downstream delta follows the
deliberate retirement path instead; never publish an empty patch. No step
stages host source or creates an intermediate commit, so multiple divergent
cases can be reconstructed sequentially while earlier results remain dirty.

## Retire a fully replaced case

Prepare a retirement candidate only after the semantic map and available clean
controls justify the hypothesis that upstream implements every required
invariant, including compatibility, failure, and lifecycle behavior. Accept
that retirement only after the complete resulting candidate passes every
durable boundary below. Before removing the case, run its focused and declared
native targets in `tests-only` mode when it owns retained tests. After removing
it, rerun those boundaries through the resulting stack. For a package or live
distinction, the positive replacement proof is likewise the resulting stack
after retirement; a clean-source diagnostic cannot publish acceptance.
Explicitly map the downstream regression to an equivalent upstream test or to
another durable retained gate. If removing the case would silently discard the
only non-vacuous regression, stop and resolve that test-ownership gap; do not
misclassify it as an upstream-failure quarantine.

There is no automatic `case-retire` target. In one reviewed content change:

1. remove the case from `stacks/develop.toml` and remove its slug from every
   dependency or case-ownership reference; preserve any independently required
   global gate after migrating its inputs and provenance;
2. remove its tracked case directory rather than keeping a historical copy;
3. update current active-case lists and documentation;
4. resolve and test the resulting complete stack;
5. record the upstream replacement and old/new source commits in the external
   handoff, never a tracked evidence archive or an automatic result commit.

Deletion is a material decision. If the refresh task did not explicitly
authorize keep/adapt/retire changes, stop after presenting the proof and ask
for that authority.

The current `wayland-client-keymap-sync` case has an additional hard retirement
boundary. Its versioned `tests/live-wayland-keyboard.json` scenario is the sole
input for `live-wayland-keyboard`, and both the runner and job provenance
currently require that input to be owned by one selected case. Removing the
case as-is makes the mandatory stack-wide keyboard gate fail before Xpra starts.
Before retiring it, migrate the scenario to durable neutral ownership or add an
equivalent generic manifest-declared mechanism, then update the runner,
provenance schema, immutable inventories, mutation tests, contract, and live
runbook together. Prove the migrated gate with `STACK=develop`. Otherwise keep
the case; an upstream unit test alone does not satisfy this live-input boundary.

## Complete post-rebase validation

There is no documentation-only or unchanged-patch exception after rebase.
Every command below uses the new embedded source and current patch/selection
digests. Stop at the first unexplained failure and use a new run identity for
every retry.

### Offline, clean controls, focused, and native

Run:

```bash
make -C fork-maintenance RUFF=<ruff> check
make -C fork-maintenance ci-layout-check
make -C fork-maintenance stack-check STACK=develop
git diff --check
```

For every retained or adapted case, also run its individual
`patch-check CASE=<slug>`. For a retired primary case, that command must fail
because the case no longer exists; instead, search all current manifests,
stack files, Make targets, and active-case documentation for stale references
to its slug. The expected read-only check is:

```bash
rg -n --fixed-strings '<case>' \
  AGENTS.md CLAUDE.md CONTRIBUTING.md pyproject.toml .gitignore \
  .github fork-maintenance
```

Exit status 1 with no output is the expected no-match result; any other nonzero
status is an error. Review every match; no current active reference may remain.

Repeat the tests-only focused control for every production case in the current
stack which owns tests, not only the primary case. For a case with no retained
test path, record that the tests-only control is unavailable, perform its
documented semantic inspection of clean upstream, and prove the durable real
boundary against the complete resulting stack. Do not invoke the unsupported
`PATCH_MODE=clean TARGET=focused` combination. If any non-primary clean control
passes, skips, or ceases to reproduce the exact retained regression, stop this
validation phase and apply the queue-wide scope rule above before continuing.
Apply the same stop when a no-test case's semantic inspection indicates that
upstream may now replace its behavior; it also requires an explicitly scoped
keep/adapt/retire reassessment before package, full, or live acceptance.

For every retained or adapted production case, run its individual focused
selection with the complete patch. Enumerate the current stack and give every
case a distinct `RUN`; do not substitute the primary case alone or infer
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
silently relying only on the full stack. In every decision branch, also run the
resulting complete stack:

```bash
make -C fork-maintenance test-start \
  STACK=develop PATCH_MODE=patched TARGET=focused \
  RUN=<cycle>-stack-focused-01
make -C fork-maintenance test-wait RUN=<cycle>-stack-focused-01
make -C fork-maintenance test-status RUN=<cycle>-stack-focused-01
make -C fork-maintenance test-logs RUN=<cycle>-stack-focused-01
make -C fork-maintenance test-remove RUN=<cycle>-stack-focused-01
```

For every retained or adapted production case, run every native/subsystem
target that case declares with its individual `CASE=<slug>` selection and a
unique run identity. In every decision branch, also run every native/subsystem
target declared by the resulting stack. The current Wayland examples are:

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

Start each leg with a distinct run identity; they may execute concurrently when
resources allow:

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
failure is not fixed or skipped in the selected production case. First
reproduce its module on the exact clean source in the same leg and follow
`test-quarantine.md` when the user authorizes that scope.

### Live preflight

Before the first selected-case or stack live gate, create and verify the
hash-locked analysis environment, inspect the host boundary, and prove that the
complete resulting stack can be materialized in an isolated workspace:

```bash
make -C fork-maintenance live-venv
make -C fork-maintenance live-venv-check
make -C fork-maintenance doctor
make -C fork-maintenance isolated-start-check
make -C fork-maintenance workspace-create \
  STACK=develop WORKSPACE=<cycle>-live-preflight-01 PATCH_MODE=patched
make -C fork-maintenance workspace-remove \
  WORKSPACE=<cycle>-live-preflight-01
```

Do not start a live wrapper if this preflight fails. `doctor` reports optional
hardware and input-path availability, but a selected live gate which requires
one of those paths still fails closed when it is unavailable. Reuse this
verified environment for the selected-case gate and the seven stack profiles;
do not recreate it between otherwise unchanged runs.

### Selected-case real boundary

Only after all three full legs pass, run each live gate declared by a retained
or adapted case with `CASE=<case>`. After retirement, omit that patched-case
run: the corresponding gate in the resulting stack-wide seven-profile matrix
is the replacement proof, subject to the keyboard scenario-ownership boundary
above. Current manifest-to-Make mappings are:

| Manifest gate | Make wrapper |
| --- | --- |
| `live-rgb` | `live-rgb` |
| `live-wayland-keyboard` | `live-wayland-keyboard` |
| `live-wayland-h264-hardware` | `live-xpra-hardware` |
| `live-wayland-opengl-h264-hardware` | `live-xpra-opengl-hardware` |

Use `live-wait`, inspect the report/log, and `live-remove` for every run. After
each remove, repeat `live-status` and require its retained removal transaction
to report `phase=removed`; `live-logs` remains available through that validated
transaction. An empty `required_gates` list does not erase a package or
subsystem boundary stated by the case README.

If a Debian-packaging case was present in the recorded pre-refresh queue,
whether it is retained, adapted, or retired, run both real package builds and
their independent package/import validation. Also run them whenever any
selected/current case or case README owns a package boundary, even if its
manifest has no package gate:

```bash
make -C fork-maintenance deb-start \
  STACK=develop DISTRO=ubuntu-26.04 RUN=<cycle>-packages-ubuntu-01
make -C fork-maintenance deb-wait RUN=<cycle>-packages-ubuntu-01
make -C fork-maintenance deb-status RUN=<cycle>-packages-ubuntu-01
make -C fork-maintenance deb-logs RUN=<cycle>-packages-ubuntu-01
make -C fork-maintenance deb-remove RUN=<cycle>-packages-ubuntu-01
make -C fork-maintenance deb-status RUN=<cycle>-packages-ubuntu-01

make -C fork-maintenance deb-start \
  STACK=develop DISTRO=debian-13 RUN=<cycle>-packages-debian-01
make -C fork-maintenance deb-wait RUN=<cycle>-packages-debian-01
make -C fork-maintenance deb-status RUN=<cycle>-packages-debian-01
make -C fork-maintenance deb-logs RUN=<cycle>-packages-debian-01
make -C fork-maintenance deb-remove RUN=<cycle>-packages-debian-01
make -C fork-maintenance deb-status RUN=<cycle>-packages-debian-01
```

The package runner always applies the complete current stack. With the package
case retained, it proves the patched result. With that case removed from the
candidate stack, the same two builds are the durable package-boundary proof for
the proposed upstream replacement. The ordinary codec unit test is not a
substitute in either branch.

### All seven positive live profiles

Using the already verified live preflight above, run all seven wrappers
sequentially with `STACK=develop` and the YAML-declared default
`NETWORK_PROFILE`. Do not replace them with CI, clean diagnostics, or fallback
classifiers:

```bash
make -C fork-maintenance live-rgb \
  STACK=develop RUN=<cycle>-live-rgb-01
make -C fork-maintenance live-wait RUN=<cycle>-live-rgb-01
make -C fork-maintenance live-status RUN=<cycle>-live-rgb-01
make -C fork-maintenance live-logs RUN=<cycle>-live-rgb-01
make -C fork-maintenance live-remove RUN=<cycle>-live-rgb-01

make -C fork-maintenance live-h264 \
  STACK=develop RUN=<cycle>-live-h264-01
make -C fork-maintenance live-wait RUN=<cycle>-live-h264-01
make -C fork-maintenance live-status RUN=<cycle>-live-h264-01
make -C fork-maintenance live-logs RUN=<cycle>-live-h264-01
make -C fork-maintenance live-remove RUN=<cycle>-live-h264-01

make -C fork-maintenance live-xpra-detach \
  STACK=develop RUN=<cycle>-live-detach-01
make -C fork-maintenance live-wait RUN=<cycle>-live-detach-01
make -C fork-maintenance live-status RUN=<cycle>-live-detach-01
make -C fork-maintenance live-logs RUN=<cycle>-live-detach-01
make -C fork-maintenance live-remove RUN=<cycle>-live-detach-01

make -C fork-maintenance live-xpra-transport-loss \
  STACK=develop RUN=<cycle>-live-transport-loss-01
make -C fork-maintenance live-wait \
  RUN=<cycle>-live-transport-loss-01
make -C fork-maintenance live-status \
  RUN=<cycle>-live-transport-loss-01
make -C fork-maintenance live-logs \
  RUN=<cycle>-live-transport-loss-01
make -C fork-maintenance live-remove \
  RUN=<cycle>-live-transport-loss-01

make -C fork-maintenance live-wayland-keyboard \
  STACK=develop RUN=<cycle>-live-wayland-keyboard-01
make -C fork-maintenance live-wait \
  RUN=<cycle>-live-wayland-keyboard-01
make -C fork-maintenance live-status \
  RUN=<cycle>-live-wayland-keyboard-01
make -C fork-maintenance live-logs \
  RUN=<cycle>-live-wayland-keyboard-01
make -C fork-maintenance live-remove \
  RUN=<cycle>-live-wayland-keyboard-01

make -C fork-maintenance live-xpra-hardware \
  STACK=develop RUN=<cycle>-live-hardware-01
make -C fork-maintenance live-wait RUN=<cycle>-live-hardware-01
make -C fork-maintenance live-status RUN=<cycle>-live-hardware-01
make -C fork-maintenance live-logs RUN=<cycle>-live-hardware-01
make -C fork-maintenance live-remove RUN=<cycle>-live-hardware-01

make -C fork-maintenance live-xpra-opengl-hardware \
  STACK=develop RUN=<cycle>-live-opengl-hardware-01
make -C fork-maintenance live-wait \
  RUN=<cycle>-live-opengl-hardware-01
make -C fork-maintenance live-status \
  RUN=<cycle>-live-opengl-hardware-01
make -C fork-maintenance live-logs \
  RUN=<cycle>-live-opengl-hardware-01
make -C fork-maintenance live-remove \
  RUN=<cycle>-live-opengl-hardware-01
```

Every live result must be a positive application/transport/hardware result with
its exact lifecycle and cleanup evidence. Missing hardware, application input,
or a valid environment leaves the refresh incomplete; it is not converted to a
skip. Never signal an owned job or call destructive Podman commands directly;
use only `test-*`, `live-*`, `test-image-*`, and `deb-*` lifecycle targets.
After each `live-remove` shown above, repeat `live-status` for that exact `RUN`
and require `phase=removed` before proceeding to the next profile.

## Final audit and uncommitted handoff

After every case decision is reflected in the queue and all jobs are reviewed:

```bash
make -C fork-maintenance stack-check STACK=develop
make -C fork-maintenance ci-layout-check
make -C fork-maintenance RUFF=<ruff> check
git diff --check
git status --short --branch
```

For a retained or adapted primary case, precede this block with
`make -C fork-maintenance patch-check CASE=<case>`. For a retired case, record
the reviewed `rg` no-stale-reference result and the successful resulting-stack
resolution instead.

If the resulting checkout is already clean—for example, the selected case was
retained unchanged and the rebase needed no new control-plane repair—run the
final branch gate now:

```bash
make -C fork-maintenance develop-check
```

If adaptation, retirement, quarantine, CI-layout, or documentation changes are
uncommitted, `develop-check` must instead remain outstanding. This is expected:
do not stage, commit, or amend the refresh result merely to make it pass. Leave
the complete reviewed worktree diff for the operator, who decides whether and
how to commit it after the handoff.

The handoff must state:

- old and new fork-master/source/develop commits;
- the sole automatic start-commit SHA, or that the checkout began clean;
- rewritten commit range and any rebase conflict resolutions;
- selected case's old/new patch digests and keep/adapt/retire conclusion;
- semantic mapping and every available clean-control result, or the documented
  no-test semantic evidence;
- status of every other case and the quarantine;
- focused/native, package, full-leg, and seven live run identities/results;
- any incomplete gate or missing authority;
- the exact final staged, unstaged, and untracked status and why
  `develop-check` is outstanding when the result is dirty;
- that no remote ref was changed by the agent.

After all collected jobs and finalized workspaces have had their exact remove
targets run, review and execute the two-phase cleanup:

```bash
make -C fork-maintenance cycle-clean-plan CYCLE=<cycle>
make -C fork-maintenance cycle-clean \
  CYCLE=<cycle> CONFIRM=<sha256-from-reviewed-plan>
```

Results remain ignored local state until that reviewed cleanup; never copy
them into Git. The operator alone publishes rewritten `develop`, using the
exact-SHA `--force-with-lease` procedure in
[`publish-develop.md`](publish-develop.md). The agent never pushes.
