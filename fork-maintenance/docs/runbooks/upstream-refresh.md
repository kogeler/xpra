# Autonomous Upstream Refresh and Full Queue Adaptation

## Single agent entry point

Give an agent this exact directive; no priority case is required:

```text
Execute autonomous-upstream-refresh against the current fork master.
```

`autonomous-upstream-refresh` is an agent workflow name, not a shell command or
Make target. The directive is the complete invocation of this runbook. It
explicitly chooses the current fork `master` as the next source boundary,
authorizes the queue-wide keep/adapt/retire decisions and in-scope repairs
defined below, and requires the agent to continue through the complete
validation and uncommitted handoff. Do not ask the operator to repeat the base
choice, provide a `CYCLE`, expand scope for another active case, or separately
confirm the one preservation commit.

Every case receives equally high review priority, depth, and evidence
requirements. Use stack order for operational dependencies, not to rank the
importance of defects. The older `PRIMARY_CASE=<slug>` spelling remains an
optional explicit request to start with that active production case; it never
reduces another case's review or reporting depth. Do not invent a priority or
ask the operator to choose one.

The agent derives one never-reused lowercase `CYCLE` prefix from the current
UTC date/time and a recognizable queue-refresh fragment, verifies that no
runtime or workspace identity already uses it, and records it before the first
lifecycle operation. Use enough time precision or append a numeric suffix to
prove uniqueness; do not ask the operator to name one.

## Purpose

This refresh has a mandatory code-review phase before runtime validation:

```text
new embedded source → every patch's applicability → deep manual queue review
  → reasoned keep/adapt/retire decisions → implement and re-review all changes
  → manual-review exit gate → clean controls and affected runtime tests
  → candidate freeze → remaining final acceptance
```

Follow the development, candidate-freeze, and final-acceptance phases in
[`validation.md`](validation.md) only after the manual-review exit gate.
During the initial review, finish all review-driven adaptations, removals and
regression-ownership migrations before starting any new Xpra test, quarantine
run, native/compiled regression, live profile, or real package build. Initial
applicability checks, static inspection, whitespace/lint and offline
fork-control/safety checks are not runtime validation and remain allowed.

After that gate, use nearest regressions, affected upstream/case modules,
relevant native/compiled checks, and the early complete live suite. Only after
the candidate is stable fill the final evidence gaps; do not repeat the whole
matrix after each subsequent correction. Tests can refute or strengthen a
review conclusion, but never replace the agent's reasoning about correctness,
necessity and uncovered behavior. Exhaustive test coverage is not achievable,
and a green suite cannot establish that every patch is correct or still needed.

This is the canonical autonomous end-to-end runbook for an operator-selected
upstream refresh. Use it after the operator has synchronized
`kogeler/xpra:master` from `Xpra-org/xpra:master` and wants the agent to move
the complete maintained queue to that current fork-master boundary. The
invocation above is that explicit choice.

The agent may fetch and verify both master refs, fast-forward the local
`master`, and rebase local `develop` onto that verified fork `master`. The agent
must not dispatch the hosted sync workflow, run `gh repo sync`, push or
force-push a ref, or change the default branch. A remote mismatch returns the
workflow to the operator; it is not repaired locally.

Moving the embedded source invalidates every previous functional result. The agent therefore reads and semantically reassesses every active patch in
its current surrounding source, gives every production case an explicit
keep/adapt/retire conclusion, reviews the quarantine duty, implements the
conclusions, and resolves the complete resulting queue before runtime tests.
It then confirms or revisits those conclusions through every available
tests-only control, the clean quarantine reassessment, focused/native tests,
both real distribution package builds, all three full upstream legs, and all
nine complete-stack live profiles. Every case requires the same detailed
correctness and necessity analysis, including cases whose patch bytes do not
change.

Invoking this runbook authorizes one local preservation commit at the very
beginning when legitimate non-ignored changes already exist. The agent makes
that one start commit without another confirmation. It does not authorize any
later content commit: every adaptation, retirement, quarantine, CI,
documentation, runner, or runbook result produced by the refresh remains
uncommitted for operator review.

## Inputs and reading

The invocation needs no case selection. If the operator explicitly supplies
the optional `PRIMARY_CASE`, it must be one production slug in the pre-refresh
`stacks/develop.toml` and affects only starting order, never review depth.
Derive `CYCLE` as specified by the single entry point and use it for every
`RUN`, `IMAGE_RUN`, and `WORKSPACE` created by this refresh. The directive itself confirms that this refresh should move
`develop` to the current verified fork `master`; there are no additional
required inputs.

Replace placeholders such as `<case>` and `<cycle>` in every example; never
pass the angle brackets literally.

Read completely before changing anything. Fork-owned guides, contracts,
runbooks, and manifests alone define this process; inherited source documents,
workflows, and history supply technical context, never workflow authority:

1. root `AGENTS.md` and `fork-maintenance/AGENTS.md`;
2. `fork-maintenance/CONTRACT.md` and this runbook;
3. `stacks/develop.toml` and every active `cases/<id>/case.toml` and
   `README.md`;
4. every active case's complete `fix.patch`, including the quarantine patch,
   plus the complete surrounding source, callers, tests, and overlapping
   patches for all owned paths;
5. `CLAUDE.md`, `CONTRIBUTING.md`, `.github/upstream-workflows/test.yml`, and
   `pyproject.toml`;
6. the current source, adjacent tests, and recent maintainer-authored history
   for every active production path and quarantine-owned test module;
7. [`bootstrap.md`](bootstrap.md),
   [`isolated-workspaces.md`](isolated-workspaces.md),
   [`patch-cycle.md`](patch-cycle.md),
   [`test-quarantine.md`](test-quarantine.md),
   [`upstream-tests.md`](upstream-tests.md),
   [`live-tests.md`](live-tests.md), and, when applicable,
   [`deb-packages.md`](deb-packages.md);
8. [`cycle-cleanup.md`](cycle-cleanup.md) and
   [`publish-develop.md`](publish-develop.md).

Also read [`validation.md`](validation.md) for scheduling, candidate freeze,
and the exact evidence-reuse rules.

Resolve one reviewed Ruff executable before the first control-plane check and
record its version. `<ruff>` below is its absolute path. A system `ruff` is
valid; when it is absent, an operator-provisioned executable such as
`.artifacts/fork-maintenance/tooling-venv/bin/ruff` is also valid after its
ownership and executable-file boundary are reviewed. The optional tooling venv
is not created by this runbook and is not acceptance evidence.

If the retained artifact inventory contains an owner from the retired
`xpra-lab-*` namespace, use only the exact retired-namespace classification in
[the pre-refresh record](#pre-refresh-record). The old migration document is
no longer maintained. Current lifecycle readers intentionally have no
compatibility mode for that namespace; unmatched state remains a stop, not
permission to restore old tools or weaken the ownership audit.

An optional starting slug cannot be `upstream-test-quarantine`: it is a
temporary test duty, not a production behavior. The duty is nevertheless
always in scope and is reassessed through `test-quarantine.md`.

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
- every active case patch SHA-256 and the complete stack resolution digest;
- current branch/status;
- the derived cycle identifier and any explicit operator-requested starting order.

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
# Repeat for every slug printed by the active stack listing.
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
unattributed maintenance cache stops the refresh. Do not delete a cache merely
because its source label predates the rebase during this inventory phase: the
current-source image preflight below is the authority for whether it can be
used. That preflight may classify an otherwise exact old-source image for
explicit locked removal and rebuild. Apply the stricter exact absence/foreign-
image exception below to every legacy-labelled image or volume selected from
the unfiltered listings.

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

The public target rechecks clean porcelain and the `develop` branch before its
first fetch, so an omitted preservation step fails before either cached ref can
move. `repo-sync` must then prove that cached and live `origin/master` and
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

After rebase, re-read the current fork-owned `AGENTS.md`,
`fork-maintenance/AGENTS.md`, and `fork-maintenance/CONTRACT.md` before resolving
a patch or making any post-rebase edit. Separately re-read `CLAUDE.md`,
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

Before making any new tracked edit, prove the clean rebased branch and inspect
each case separately so a first stack failure does not hide later status:

```bash
make -C fork-maintenance patch-start-check
make -C fork-maintenance patch-check CASE=<case>
```

Run `patch-check` for every case in current stack order, recording each result
even if an earlier case fails. This is a textual/provenance inventory, not
manual review or permission to test. Run `stack-check` only after every
individual case resolves. `apply` and exact `already-present` are the only
acceptable resolver states for a selected runtime candidate; `diverged` or
`ambiguous` requires investigation before that candidate can be tested.
Every case, including `apply` and `already-present`, must next pass the deep
manual review below. Do not repair only conflict hunks and proceed to tests.

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

## Autonomous queue-wide authority and self-correction

An upstream rebase is never a single-patch operation. The invocation expressly
authorizes the agent to inspect, retain, adapt, narrow, or retire every active
production case; update the quarantine duty; and repair the fork control,
tests, runners, documentation, and runbook needed to complete this refresh.
There is no low-priority or applicability-only review path. Before any new
runtime validation:

- every pre-refresh case must have a complete manual review and a reasoned
  decision, with all resulting production and regression-ownership changes
  implemented and re-reviewed;
- every remaining active case must be `apply` or exact `already-present`
  against the new source;
- all overlapping cases must still compose in declared order;
- quarantine modules and per-leg assumptions must be manually reviewed; the
  subsequent clean gates must confirm or correct their empirical assignments;
- any changed upstream workflow boundary must be reconciled;
- no applied production source may remain in host `develop`.

These resolution checks are necessary but cannot satisfy the manual-review
exit gate. That gate precedes even clean controls and focused diagnosis.
The later candidate-freeze review in [`validation.md`](validation.md),
including tests, live oracles, compiled risks, and build inputs, remains a
separate prerequisite for the complete-stack final workload.

Perform the complete semantic mapping and keep/adapt/retire analysis below for
every case without waiting for a failure. If later testing passes unexpectedly,
skips, or no longer observes the claimed defect, reopen the affected case's
review and its overlapping consumers in this same pass. Do not request scope
expansion, preserve a potentially redundant or vacuous patch merely because it
still applies, or run the expensive final matrix on an unresolved stack.

When execution exposes an in-scope error or omission in this runbook, a related
contract, control-plane implementation, test, live/package harness, or case
documentation, repair it immediately and add or update the narrow regression
which proves the correction. Runtime checks of review-driven changes wait for
the manual-review exit gate; narrow offline fork-control checks may run during
review. Keep those changes with the other uncommitted refresh results and
continue the same runbook pass; do not restart the process from its first step
merely because the written procedure changed.

Maintain a current external run ledger of the exact source, case, selection,
runner, image-input, command, and result identities. Reuse an already valid
expensive result only while every semantic input which can affect it remains
identical. Rerun the narrow preflight after a pre-test guard repair, and rerun
an expensive payload only when its frozen source, applied patch or selection,
image inputs, entrypoint, test command/assertions, runner, scenario, or
acceptance behavior changed. A comments-only or documentation-only correction
does not invalidate an otherwise exact result. If uncertainty remains, treat
the result as invalid and rerun its gate with a new identity.

Stop and return to the operator only for a boundary the directive cannot safely
authorize: an unequal or divergent live fork master requiring remote mutation;
unsafe, secret, unexplained, or externally owned local state; an unresolved
semantic choice where a correct implementation cannot be established; or a
mandatory physical/resource boundary which is genuinely unavailable. Report
the exact blocker and all completed current evidence. Ordinary difficulty, a
change to any active case, or a repair to this runbook is not a scope stop.

Every applicable or diverged case is inspected and updated one at a time in an
isolated workspace, even while reviewed fork-control results are uncommitted.
No case adaptation may consume the clean host source/index boundary. A
diverged case uses the provenance-bound `PATCH_MODE=reconstruct` flow below;
never fall back to an intermediate commit merely to make the next case
possible.

## Reassess every production case semantically

This is a mandatory manual code review by the agent, not a test-dispatch phase.
Begin after recording every applicability result and completing the new-source
and CI-boundary reading above. Enumerate the recorded pre-refresh queue and
account for every case after any proposed retirement or ownership migration.
Review all cases to the same depth, including unchanged patches and exact
`already-present` cases. Never let a failed first case hide the rest.

Read each complete patch and manifest against the actual new embedded source,
not just the old/new diff, conflict hunks, case README, or test assertions.
Trace the clean upstream behavior and the candidate with the patch applied in
a supported isolated workspace; inspect the composed candidate where cases
overlap. Read callers and callees across the changed boundary, adjacent tests,
feature/platform gates, and relevant maintainer-authored history between the
old and new source. History explains intent; current executable code is the
authority for behavior.

### Required reasoning for every case

Build the following map in the ignored cycle ledger. Identify concrete source
commits, paths and symbols for each claim. A checkbox, patch digest, test name,
or statement that the code "looks correct" is not a review.

| Question | Required manual analysis |
| --- | --- |
| Original defect and present trigger | Explain the actual input, state transition, race, protocol sequence or package result; trace whether clean new upstream can still reach it. |
| Entry and exit paths | Trace relevant callers, callbacks and consumers, including alternate entries, early returns and failures, not only the regression's route. |
| Ownership and lifetime | Identify the subsystem, thread, process, connection or package responsible for each state/resource; reason through publication, replacement, cancellation and cleanup. |
| Correctness of the candidate | Explain why the applied patch establishes each required invariant and does not introduce a new defect in its current surroundings. Inspect ordering, reentrancy, stale callbacks, concurrent teardown, partial initialization, rollback and exception paths where relevant. |
| Compatibility and scope | Check feature toggles, disabled/readonly policies, protocol/platform/build variants, ABI and packaging ownership; justify each changed hunk within the atomic case boundary. |
| Current necessity | Compare the behavior with and without the patch. Map upstream replacements, redesigns, removed consumers and narrower remaining gaps to exact code, not a similar symptom or commit subject. |
| Queue interaction | Review overlapping paths and shared interfaces in declared order, including semantically related cases which touch different files; identify duplicate fixes, inconsistent ownership and assumptions about another patch. |
| Coverage and blind spots | Read what each test actually stimulates and asserts. List important paths/interleavings/configurations it does not cover and reason about them directly; name useful additional regressions without claiming exhaustive coverage. |
| Durable verification plan | State the expected clean-control behavior and the focused/native/package/live observations which will later challenge the conclusion. Distinguish planned checks from results already collected. |

For a genuinely inapplicable dimension, explain why; do not invent concurrency
requirements for a static packaging manifest. Conversely, do not omit a
relevant failure or lifecycle path merely because the existing tests omit it.
Passing tests, textual applicability and previous acceptance are never
substitutes for this analysis. An upstream commit message claiming the same
fix is only a lead.

Review the quarantine patch and every declared upstream test module manually
as well: check what is disabled, its isolation from production fixes, changes
in current assertions and their subjects, and the rationale for each per-leg
assignment. Record the candidate disposition and the exact clean reassessment
plan. Actual failing-leg assignments still require the later three clean
gates; neither a code-reading hypothesis nor old logs can certify them.

### Record decisions before executing tests

Conclude the review of every production case with one of:

- `keep`: a specific defect remains reachable without the patch, and every
  retained hunk is necessary and correct in the new source and complete queue;
- `adapt`: a specific residual defect remains, but the old implementation,
  scope or regression is no longer correct/minimal; specify the replacement;
- `retire`: upstream now establishes all required invariants, or the original
  production path no longer exists with no equivalent affected consumer;
  specify the durable regression and gate ownership after removal.

The conclusion must explain both correctness and continued necessity.
Separately record findings, code references, uncovered risks, required
production/test/metadata changes, and planned positive and negative checks.
For `adapt`, also explain why the final revised delta still belongs in the
fork; `apply` is not that explanation. For `retire`, account for every old
invariant, not just the one exercised by a passing test. Do not keep a
redundant patch only because its tests or a live fixture are stored inside it.

A test cannot be the reason to skip the decision until later. State a
code-supported conclusion now and label its runtime verification as pending.
If a fundamental ownership or correctness question cannot be established from
the available source, record the exact unresolved question; the review gate
does not pass by changing it to "let the tests decide".

### Implement and re-review the conclusions

Complete the whole queue's initial review before implementing its
review-driven production changes. Then apply the appropriate applicable,
reconstruction, or retirement flow below in operational stack order. Export
one atomic candidate at a time; update regressions, manifests, documentation,
dependencies and gate ownership together. Do not start a real test between
these initial case adaptations.

Re-read each final candidate in the new source and composed queue, including
the consumers affected by another case's changes. Resolve every finding or
record a supported reason it requires no change. Update the ledger with each
old/new digest, implemented decision, and remaining runtime risk. Material
changes reopen the affected reviews; an earlier review of different code does
not close them. All review-driven code changes and removals must be complete
before the manual-review exit gate.

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
that reconstruction attempt and implement a dependency-aware atomic boundary
with its fork-control tests before continuing the same pass; never bypass the
guard or reconstruct against an incomplete source. If a safe ownership model
cannot be established, report that exact semantic blocker rather than guessing:

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
a deleted case. Do not postpone the manual decision or keep a redundant
production delta just to make that command usable. If no equivalent durable
test is selectable after removal, first migrate its tests/fixtures to suitable
maintained ownership and provide a supported provenance-bound selection
through the public Make interface. Add or repair its admission, frozen-source,
inventory and ownership checks and narrow fork-control tests before deleting
the old owner. Do not invent an unsupported mode, retain an archive as a test
authority, or use an ad hoc source probe as acceptance. If the ownership
boundary cannot be established safely, report that exact unresolved review
issue rather than silently dropping coverage.

There is no automatic `case-retire` target. In one reviewed content change:

1. migrate all still-required regressions, fixtures and gate inputs as above;
2. remove the case from `stacks/develop.toml` and remove its slug from every
   dependency or case-ownership reference;
3. remove its tracked case directory rather than keeping a historical copy;
4. update active-case lists and documentation, then resolve and manually review
   the resulting complete stack;
5. record the old/new source, upstream replacement or eliminated path, and
   replacement test ownership in the ignored ledger; execute its planned
   controls and resulting-stack gates only after the manual-review exit gate.

Deletion is a material decision, but the autonomous invocation authorizes this
code-supported, uncommitted retirement candidate and the necessary coverage
migration. It is accepted only after the subsequent durable checks pass. No
separate scope expansion or result commit is authorized or needed.

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
allowed during review, but prove neither patch correctness nor necessity.

The gate passes only when:

- every pre-refresh production case has the full current-code map, equal-depth
  correctness/necessity analysis, and implemented `keep`, `adapt` or `retire`
  conclusion; the quarantine has its manual assessment and clean-gate plan;
- all review findings have dispositions, required changes/removals and durable
  regression migrations are complete, and no review is deferred to test output;
- every retained/adapted patch has been re-read in its final surrounding code;
  all individual selections resolve and the resulting complete stack composes;
- cross-case consumers, compatibility/failure/lifecycle behavior, test blind
  spots and residual runtime risks are explicitly accounted for;
- the CI boundary, source/queue identities, per-case digests, exact planned
  controls and replacement gates for retired cases are recorded.

This is the agent's documented reasoning checkpoint, not an existing Make
target or an automated correctness certificate. There must be a review record
for the whole queue before any runtime run identity is launched; a resolver
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

Enter this section only after the recorded whole-queue manual-review exit
gate. Verify the image before the first test which uses it. Reassess quarantine
for the new source and actual image/module/gate inputs before using the duty
case in runtime validation; isolated application for code review or export is
not a validated quarantine assignment. Independent reviewed production-case
tests may proceed without waiting for unrelated quarantine results.
Reuse current collected reassessment results when those inputs are unchanged.
The quarantine must resolve before its named clean gates can start. If an
identity check now reports `diverged`, reopen manual review and use the isolated
reconstruction flow above to preserve only the still-required declared
test-module changes. Publish and re-review the candidate and update the review
exit record before returning here. If new findings make the correct candidate
empty, retire the duty case only through its documented semantic and clean-test
decision; do not
manufacture an empty patch or use an ad hoc diagnostic as acceptance.

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
Update the case through the atomic admission sequence in
[`test-quarantine.md`](test-quarantine.md), then complete the clean gates whose
source, environment, module union, or expected subset changed. Every one of the
three final assignments still requires current, exact proof; an unrelated
production-only edit does not require another reassessment.

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

The review gate and CI-layout repair precede source builds; clean quarantine
proof precedes runtime use of the duty patch. Start the complete live suite
early in this post-review development phase once its focused/native and
complete-stack prerequisites are satisfied, without waiting for the full
upstream matrix. Keep every repair uncommitted. Any newly required source or
test correction first reopens its affected manual review, then uses the
isolated applicable/reconstruction flow without touching host source or index.
After the candidate is stable and frozen, fill missing or invalidated final
coverage; do not repeat the complete offline suite after each adaptation.

## Confirm review decisions with clean controls

### Establish the clean control or documented substitute

This section executes the verification plan recorded before the manual-review
exit gate. It does not make the initial keep/adapt/retire decision.
`PATCH_MODE=tests-only` and `PATCH_MODE=clean` still validate the complete
case patch before starting a container. Do not invoke either command while
that case is `diverged` or `ambiguous`. For a new `diverged` finding, reopen
manual review, reconstruct and publish the candidate through the flow above,
and update the review exit record before returning to this control. If a new
finding indicates full upstream replacement and an empty delta, return to the
manual retirement and regression
migration flow; `patch-update` cannot publish an empty patch. For an already
retired case, use the replacement owner and supported commands recorded at the
review gate, never the removed `CASE` slug. `ambiguous` remains a hard stop
until source and patch identity are trustworthy.

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

Also run every declared native/subsystem target with `PATCH_MODE=tests-only`
for each retained or adapted test-owning case, not only the Python-focused
subset. For a retired case, run the equivalent clean and resulting-stack
focused/native checks through the durable ownership and selection migrated
during review. A missing replacement gate reopens review; deletion never waives
a behavioral boundary.

Some production cases, currently `debian-libva-codecs-package`, name an
existing upstream focused module but own no test file. `tests-only` correctly
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
| `apply`, clean regression fails as intended | Supports the reviewed defect for this tested trigger only; it does not prove every hunk necessary or every path correct. Compare the actual failure with the recorded code reasoning. |
| `apply`, clean regression passes | Contradicts the expected clean-control result. Reopen the manual map: the patch may be redundant/stale, the environment may miss the trigger, or the regression may be vacuous. Do not retire on this result alone. |
| `already-present` | Exact source presence is not a new decision. Confirm the reviewed candidate through its retained or migrated controls and every durable real boundary; neither resolver status nor green tests certify the complete behavior. |
| `diverged` | The reviewed input identity or queue changed; runtime admission must stop. Reopen review and reconstruct the complete candidate on the current source; never force, fuzz or use rejects. |
| `ambiguous` | Applicability is not trustworthy. Stop and inspect the patch/source identity before any edit or test claim. |

The code-supported decisions, confirmed or revised after testing, remain:

- retain the patch unchanged;
- adapt or narrow its production code and regression;
- retire it because upstream safely replaces the complete behavior or removes
  the affected production path, with durable verification preserved.

“It still applies” is not enough for retention, and “the clean test passes” is
not enough for retirement.

## Final post-rebase acceptance

After the manual-review exit gate, post-review development loop and reviewed
candidate freeze, reconcile the ledger against all requirements below. Run
only missing or invalidated checks; do not repeat a valid development result
merely because final acceptance has begun.
The evidence-reuse rules in [`validation.md`](validation.md) retain original
run identities and require exact input proof.

There is no old-base or unchanged-patch waiver after rebase: every requirement
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
`patch-check CASE=<slug>`. For every retired case, that command must fail
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
selection passes with the complete patch. Enumerate the current stack and use
a distinct `RUN` for each missing or invalidated result; do not infer atomic
self-sufficiency from the later stack result:

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
environment, inspect the host boundary and materialize the complete stack in an
isolated workspace. All nine scenarios apply that full queue to both endpoints;
`CASE`, partial queues and clean endpoints are forbidden.

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
verified environment for all nine complete-stack profiles;
do not recreate it between otherwise unchanged runs.

### Case-owned real boundaries, complete-stack execution

Case manifests identify behavioral owners of live assertions, not isolated
product selections. Every current profile executes with the complete queue on
both endpoints, including clipboard, subsurface, keyboard and hardware tests.
A new or retired case must update its regression ownership without creating a
single-patch live path or dropping the profile from global coverage.

Use the full suite for any patch validation:

```bash
make -C fork-maintenance live-all STACK=develop RUN=<cycle>-live-01
make -C fork-maintenance live-suite-check STACK=develop RUN=<cycle>-live-01
```

The suite orders clipboard/subsurface first, runs every profile through its
named start/wait/remove lifecycle and validates all nine retained reports.
A failure stops escalation. Diagnose, correct and use new run names; do not
combine results from different source/queue/harness candidates.

### All nine positive live profiles

The `live-all` command above is the mandatory nine-profile matrix. Do not
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

## Final audit and uncommitted handoff

After every case decision is reflected in the queue and all jobs are reviewed:

```bash
make -C fork-maintenance stack-check STACK=develop
make -C fork-maintenance ci-layout-check
make -C fork-maintenance RUFF=<ruff> check
git diff --check
git status --short --branch
```

Precede this block with `make -C fork-maintenance patch-check CASE=<case>` for
every retained or adapted case. For every retired case, record the reviewed
`rg` no-stale-reference result and the successful resulting-stack resolution
instead.

If the resulting checkout is already clean—for example, every case was
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
- the whole-queue manual-review exit record before the first runtime run,
  including any reopening for later findings or changed candidates;
- each case's old/new patch digests, equally detailed correctness/necessity
  map, uncovered risks, code-supported keep/adapt/retire conclusion,
  implemented changes and durable test ownership after retirement;
- the subsequent clean-control or documented no-test evidence, and how runtime
  results confirmed or changed the earlier manual conclusions;
- quarantine reassessment and any assignment changes;
- focused/native, package, full-leg, all nine complete-stack live
  run identities/results;
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
