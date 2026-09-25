# Store And Clean Local Artifacts

## Single ignored root

All durable runtime, build, result, publication, and cache state lives below the
Xpra repository root:

```text
.artifacts/fork-maintenance/
├── build-contexts/
├── cycle-cleanups/
├── deb-packages/
│   ├── locks/
│   ├── outputs/
│   ├── releases/
│   ├── results/
│   ├── runs/
│   ├── selections/
│   └── sources/
├── jobs/
├── knowledge/
│   ├── INDEX.md
│   └── sessions/
├── live-results/
├── source-archives/
├── upstream-tests/
│   ├── image-builds/
│   ├── logs/
│   ├── runs/
│   └── sources/
├── tooling-venv/
├── venvs/
└── work/
    └── <session>/
```

Everything except `knowledge/` is session state. A finished session is closed
with [`artifacts-close`](session-close.md), which leaves only `knowledge/`
and idle lock files; `work/<session>/` is the session's free-form ledger,
notes and scratch area until then.

The exact set may grow only with a corresponding reviewed change to
`fork-maintenance/artifacts.toml` and its runtime-protection tests. Durable run
output never moves into the tracked `fork-maintenance/` directory, and nothing
else is created below `.artifacts/` outside `fork-maintenance/`. Only transient
interpreter caches may exist at another explicitly ignored local path; the root
`clean` Make target removes the automation's `__pycache__` entries. The root
`.gitignore` ignores all of `.artifacts/`; the `artifact-boundary` Make target
verifies that rule and checks that known runtime/result roots are not tracked.

## Deterministic whole-directory housekeeping

Use this explicit mid-session discard operation when local output is no longer
needed for the current acceptance/reuse cycle. At the end of a session use
[`artifacts-close`](session-close.md#close-the-session) instead. It is independent of the cycle-specific
finalization flow below, and can remove obsolete report formats and unmanaged
diagnostic scratch without interpreting them as current acceptance evidence.
It does not run Xpra tests, rebuild images, stop jobs, or delete Podman objects.

Coordinate whole-root disposal with every agent/operator writing artifacts.
Lifecycle locks protect registered jobs, not an agent editing an unmanaged
probe. Do not run global disposal as a background janitor during concurrent
development. An unexpected diff or new artifact is not permission to discard
another writer's work: keep the reviewed plan, let a changed confirmation fail,
and finish the cleanup handoff without repeatedly deleting new output.
Plan/check remain non-destructive. Resume disposal only at a coordinated review
boundary.

The permanent allowlist is [`artifacts.toml`](../../artifacts.toml). Its entries
are storage classes, never current run/cycle/session names or dates:

- `permanent`: only `knowledge/`, the distilled session records and their
  generated registry. The policy loader rejects any other permanent class;
- `infrastructure`: lifecycle/recovery authorities and their lock files;
- `task`: session work (`work/`) and every reusable filesystem cache (build
  contexts, source archives and bundles, DEB sources/selections/image-key
  locks, virtual environments, the optional tooling venv).

Structural `containers` remain; their other safe children are disposable. The
same policy applies to yesterday's log and a newly generated log. No age
threshold, newest-success heuristic or agent-made list decides retention.
Mid-session housekeeping keeps all three classes. Session close keeps only
`permanent` and `infrastructure`, and requires the latter to be idle.

There is no archive of raw output. A handoff or conclusion that must outlive
the session is distilled into `knowledge/sessions/<session>.md`; the
[session runbook](session-close.md) defines its format, size limit and
registry. Do not copy reports, logs or screenshots into `knowledge/`.

Review and execute the exact mid-session plan:

```bash
make -C fork-maintenance artifacts-clean-plan
make -C fork-maintenance artifacts-clean CONFIRM=<artifacts_clean_confirm>
make -C fork-maintenance artifacts-check
```

`artifacts-clean` without `CONFIRM` is also plan-only. The plan prints exact
relative targets, content/mode fingerprints, total apparent file bytes,
protected paths and blocked unsafe paths with reasons. It binds the exact policy bytes as well as the
targets. A changed target, new disposable file, policy change or newly owned
result invalidates the old confirmation. Filesystem allocation, compression and
shared extents can make actual freed disk space differ from apparent bytes.

Both planning and removal hold the same four lifecycle locks as `cycle-clean`.
An existing runtime record protects its exact job family even when the owner
is old, exited or not collectable by today's schema. Read-only Podman inspection
also protects owned containers/networks without a local owner. Remaining job
state is finished through `test-*`, `test-image-*`, `live-*` or `deb-*`; the
housekeeping command neither signals processes nor unlinks their authority.
Runtime/recovery authorities and the four locks remain protected even if an
accidental policy edit omits their keep entry. Unknown runtime layouts fail
closed. Recovery state is retained. Symlinks inside a disposable tree,
including absolute ones such as a virtual environment's interpreter link, are
fingerprinted by their target text and never followed. Other unsafe
files/trees (other-writable, hard-linked or special files) are reported as
blocked, not followed, force-deleted or silently considered retained. A
blocked path makes the command return nonzero even when no other disposable
target remains.

Confirmed removal reuses the cycle transaction engine under the reserved
identity `artifacts-<policy-sha256>`. Directory staging is no-replace and binds
the original device/inode; the durable rmtree phase permits a retry after
partial recursive deletion. On interruption, run the same command with the
same policy and `CONFIRM`. Do not delete the marker/staging by hand and do not
use `cycle-clean` to resume this different policy. A pending cycle cleanup
must instead finish through its original cycle interface first.

After success, `artifacts-check` must report `disposable_targets=0` and an empty
`blocked` list; an immediate
repeat clean is a no-op. Protected runtime state remains listed separately,
not disguised as deleted garbage. Do not claim the folder is entirely empty or
that every job is removed merely from this result. Knowledge, session work,
caches and recovery infrastructure are the intended mid-session clean state.

## Session close

`artifacts-close-plan`, `artifacts-close CONFIRM=<artifacts_close_confirm>` and
`artifacts-close-check` apply the same policy with the `task` class no longer
kept. Planning additionally blocks on every protected runtime/recovery path,
any file below `infrastructure` other than an empty `*.lock`, any
`knowledge/` problem reported by `knowledge-check` (including a stale
`INDEX.md`), and any entry in `.artifacts/` other than `fork-maintenance/`.
Confirmed execution refuses while anything is blocked, holds every `*.lock`
file directly inside a discarded cache directory without waiting, and uses the reserved
identity `artifacts-close-<policy-sha256>`; resume an interruption with the same
`CONFIRM`. A pending `artifacts-clean` or `cycle-clean` transaction must finish
through its own command first, and vice versa. `artifacts-close-check` prints
`session_closed=yes` only when nothing but `knowledge/` and idle lock files
remains. The prerequisites, record format and blocker table are in the
[session runbook](session-close.md).

Deleting a named result ends its reuse window in [`validation.md`](validation.md).
The session ledger in `work/<session>/` must distinguish historical conclusions
from still available raw evidence. Do not rerun Xpra merely because logs were deleted;
if a later task needs unavailable evidence, it must produce a new named result.
The narrow implementation gate is `make -C fork-maintenance artifact-tests`;
run the full offline `check` on a stable cleanup-tooling candidate, not Xpra
full suites or package/live builds.

## Producer ownership boundaries

The durable and transient subtrees have these exact ownership boundaries:

- upstream-test source caches use
  `upstream-tests/sources/<commit>-<remote>.bundle`; the matching retained
  `.bundle.lock` serializes publication, while a `.bundle.partial` is
  recoverable staging and blocks cycle cleanup until the next exact snapshot
  operation has reclaimed it;
- a detached upstream test may temporarily own
  `upstream-tests/runs/<RUN>.prelaunch.json`, `<RUN>.owner`, and
  `<RUN>.payload/`; the prelaunch remains until container start and FIFO payload
  delivery finish, then collection/removal retains
  `upstream-tests/logs/<RUN>.{log,status,remove.json}`;
- a standalone image build owns `upstream-tests/image-builds/<IMAGE_RUN>/`
  plus `upstream-tests/image-builds/.<IMAGE_RUN>.image-prelaunch.json` until
  removal, and retains its final log/status/removal-transaction set in
  `upstream-tests/logs/` alongside test results; hosted foreground image
  creation leaves no `.ci-image.*` host context;
- hosted foreground tests use deterministic
  `upstream-tests/.foreground-payload{,.owner.json}` staging protected by
  retained `.foreground-payload.lock`; a remaining marker or payload blocks
  cycle cleanup;
- a live start first owns `jobs/live/<RUN>.freeze-prelaunch.json`, then
  `<RUN>.freeze.json` and its matching freeze runtime/completion/result files,
  before publishing the main owner and final `live-results/<RUN>/`
  input/evidence tree; freeze-only abort temporarily owns
  `jobs/live/<RUN>.freeze-abort.json` and exact
  `live-results/.<RUN>.freeze-abort-{staging,result}` directories; collection
  and removal retain the result tree plus
  `jobs/live/<RUN>.{log,status.json,remove.json}`;
- DEB source caches use
  `deb-packages/sources/<checkout-sha>-<snapshot-sha>/{source.bundle,source.json}`;
  immutable queue caches use
  `deb-packages/selections/<selection-sha>-<metadata-sha>/{lab,selection.json}`;
  a local start first owns `deb-packages/runs/<RUN>.prelaunch.json`, and an abort
  publishes `deb-packages/runs/<RUN>.abort.json` until its exact transaction
  completes; finalized local results are the status/log/removal-transaction set
  in `results/` plus the output tar in `outputs/` only for a validated success;
- DEB output validation owns only the siblings
  `.<tar>.validate`, `..<tar>.validate.partial`, and
  `.<tar>.validate.owner.json` in that output's parent; the marker binds the
  exact output inode and size. Local output scratch blocks cleanup of its
  matching cycle, while hosted scratch remains inside release staging for
  operator review;
- a completed hosted release tree is
  `deb-packages/releases/run-<run-id>-attempt-<n>/` and retains the two tar
  assets, `release-notes.md`, `publication.json`, and the two hidden
  distribution container ownership records; it is review state outside local
  cycle cleanup;
- cycle cleanup owns schema-2 `cycle-cleanups/<CYCLE>.remove.json` and may
  temporarily stage a live result tree at
  `cycle-cleanups/.<CYCLE>.<index>.remove`; the transaction binds its exact
  device, inode, and fingerprint, and a matching
  `.<CYCLE>.<index>.rmtree.json` phase authorizes partial recursive-deletion
  recovery by device/inode.

The live analysis environment uses retained `venvs/.environment.lock` plus
deterministic `.environment.partial` and `.environment.partial.owner.json`
paths. A later `live-venv` validates the marker and recovers only that partial
under the kernel lock; cycle cleanup retains this shared environment state.
The optional `tooling-venv/` is operator-provisioned host tooling state; no
Make lifecycle creates or removes it, and it is not acceptance evidence.

Upstream-test and live terminal transitions each use one retained subsystem
`.lifecycle.lock`; DEB start/collect/remove/abort transitions use the retained
`deb-packages/locks/terminal.lock`. These files are validated mode-`0600`
holders for crash-releasing kernel locks, not run identities and not disposable
cycle results. Selection/source cache lock files are retained for the same
reason. Upstream image handoff uses retained
`upstream-tests/image-builds/.image-cache.lock`, and each DEB image key uses
`deb-packages/locks/images/<distro>-<input-sha>.lock`; their Podman children
inherit the open locks. A partial directory or owner marker is runtime state,
not a cache.

Do not create tracked `evidence/`, `runs/`, `results/`, or `communications/`
directories. This includes compact reports, selected screenshots, final status
records, checksum manifests, and publication-ready text. A result being small,
sanitized, immutable, or final does not make it source code.

## Trust boundary

Private-state helpers require the repository root and `.artifacts` to be real,
owned directories. A shared checkout may make the owned repository root group
writable, but it must never be other-writable; `.artifacts` and mutable state
below it remain private. Helpers reject symlinks, wrong ownership, and unsafe
permissions. Private owner/status records are mode `0600`; build-context
payloads retain modes required by container builds inside private parents.

Immutable background owner/completion/status publication uses an anonymous
`O_TMPFILE`, file fsync, no-replace `linkat(AT_EMPTY_PATH)`, and parent-directory
fsync. There is no named publication temporary to guess at after a crash, and
an unsupported filesystem fails closed. A newly launched supervisor waits on a
private pipe; its payload starts only after the parent has durably published
the owner and written the exact release byte. Parent EOF or termination before
that byte cannot launch the payload.

The common reverse process-output path also uses an anonymous `O_TMPFILE` when
the caller has not supplied an exact owned deterministic partial. It fsyncs and
links the result without replacement; there is no named generic fallback.
The common extractor accepts only plain, uncompressed tar and separately bounds
raw archive bytes, member count, expanded content, and PAX/GNU extended
metadata. It rejects sparse entries, transparent compression, concatenated
streams, and trailing bytes.

Never weaken ownership or no-follow checks to reuse an unsafe old directory.
Stop and let the owner inspect it.

## Immutable run identities

Every upstream-test, live, or DEB job uses a unique validated `RUN`. Only a
standalone upstream-test image build uses a separate unique `IMAGE_RUN`. Image
builds embedded in live and DEB jobs are owned by their parent `RUN`. Names are
no-clobber identities and are never reused for a retry.

An acceptance-capable completed local result binds the exact source commit,
the selection digest (which covers every selected case commit's frozen diff),
runner/build inputs, actual immutable image ID, target/profile, final
timestamp, complete log hash, and owned-object status. An
input-keyed image tag is a cache lookup; it never replaces binding the image ID
actually executed. A failed result may stop before later provenance exists; its
status and complete log retain that exact failed boundary but never become
acceptance evidence. Do not edit a completed report or status file; start a new
run when inputs or classification change.

These local records support review but are not committed.

## Inspect before cleanup

Upstream-test job:

```bash
make -C fork-maintenance test-status RUN=name
make -C fork-maintenance test-logs RUN=name
make -C fork-maintenance test-collect RUN=name
```

Live job:

```bash
make -C fork-maintenance live-status RUN=name
make -C fork-maintenance live-logs RUN=name
make -C fork-maintenance live-collect RUN=name
```

Image build:

```bash
make -C fork-maintenance test-image-status IMAGE_RUN=name
make -C fork-maintenance test-image-logs IMAGE_RUN=name
make -C fork-maintenance test-image-collect IMAGE_RUN=name
```

DEB package job:

```bash
make -C fork-maintenance deb-status RUN=name
make -C fork-maintenance deb-logs RUN=name
make -C fork-maintenance deb-collect RUN=name
```

Keep failed runs until their first failed boundary and logs have been reviewed.
An interrupted job remains inspectable.

## Exact cleanup

After review, remove only the named owned transient state:

```bash
make -C fork-maintenance test-remove RUN=name
make -C fork-maintenance live-remove RUN=name
make -C fork-maintenance test-image-remove IMAGE_RUN=name
make -C fork-maintenance deb-remove RUN=name
make -C fork-maintenance test-abort RUN=running-or-lost-name
make -C fork-maintenance live-abort RUN=running-or-lost-live-name
make -C fork-maintenance test-image-abort IMAGE_RUN=running-or-lost-image-name
make -C fork-maintenance deb-abort RUN=running-or-lost-package-name
```

Cleanup verifies owner records, PID/start-time/process-group identity plus the
private 256-bit owner token for host jobs, and immutable container IDs plus
Podman labels. The token is bound into both owner and completion records and is
inherited by the supervised payload. Cleanup does not use broad globs and does
not remove retained local logs/reports, case commits or case directories,
unrelated containers, networks, images, or volumes.

Use an abort target for a running or lost job without collected output, or to
exact-discard a completed uncollected job only when its recorded runner digest
is stale. `lost` means that no valid completion exists and the exact owned
process/container runtime is gone; a dead supervisor whose owned process group
still has a live member remains `running`. In that orphaned-group path, every
live member must carry exactly the recorded owner token; a missing, duplicate,
or mismatched token fails closed and preserves the state. A legacy tokenless
orphan therefore remains owned but is not signalable. A current completed job
must be collected, and a collected job must use its remove target. All lifecycle
mutations go through these Make targets; do not signal processes or call
destructive Podman commands directly. If a lifecycle transition is missing,
add and test its exact-owned Make target before acting. The explicit
[upstream-refresh runbook](upstream-refresh.md#disposable-image-caches) has a
separate narrow procedure for unreferenced obsolete test images outside those
named-job lifecycles; it is not permission to remove a job or persistent data.

A detached upstream test also has an inspectable prelaunch owner before
`podman create`. `test-abort` refuses it while the recorded starter is active;
after that process is gone, the target may reclaim only the exactly labelled
container and payload named by the orphaned prelaunch record. Live input freeze
first has the inspectable `jobs/live/<RUN>.freeze-prelaunch.json` owner, then its
process owner, so `live-status`, `live-logs`, and `live-abort` remain usable
before the main live owner exists. Before deleting a freeze-owned staging or
result directory, `live-abort` publishes
`jobs/live/<RUN>.freeze-abort.json`, binds its device/inode, and atomically moves
it to `live-results/.<RUN>.freeze-abort-{staging,result}`. An interrupted abort
is completed only by retrying `live-abort`, which deletes the transaction last.

A standalone upstream image build likewise publishes its exact prelaunch
marker before populating the context. `test-image-status` can inspect it and
`test-image-abort` is its only recovery path after an interrupted starter;
collection is unavailable until the durable main owner exists.

A local DEB start publishes `deb-packages/runs/<RUN>.prelaunch.json` before its
run directory or main owner. `deb-status` and `deb-logs` expose it. Before
changing owned state, `deb-abort` publishes
`deb-packages/runs/<RUN>.abort.json`; status exposes the aborting phase and a
retry completes the exact transaction before deleting that marker last.
Output-validation scratch is recovered only by a later validation, `deb-remove`,
or `deb-abort` after its external marker proves the exact tar inode and scratch
paths.

Each collected test, standalone-image, live, or DEB remove target publishes a
retained transaction before its first destructive step. Reinvoke the same target
after interruption: the transaction revalidates the original owner and evidence
and completes only that exact deletion. Test and image transactions live in
`upstream-tests/logs/`, live transactions in `jobs/live/`, and DEB transactions
in `deb-packages/results/`; all remain with the collected result until cycle
cleanup.

When a live main owner is absent, its exact schema-1 removal transaction is the
only read-only authority for that removed run. `live-status` validates the
transaction and retained evidence, reporting `phase=removing` while a bound
runtime record remains and `phase=removed` after runtime removal completes.
`live-logs` validates the same transaction and returns only its digest-bound
final log. Neither command falls back to pre-main freeze state or inspects
unbound Podman state.

Collection requires current runner and supervisor digests. If automation was
updated while a job existed, its recorded digest remains usable only for
`status`, exact-owned `abort` of uncollected state, or removal of already
collected evidence; do not accept or newly collect that stale job.

Case work is not artifact state: each case is one commit on `develop` plus its
tracked directory (see [`case-commits.md`](case-commits.md)), so nothing below
`.artifacts/` records or recovers it. An interrupted rewrite is Git state in
the checkout, finished or rolled back with Git itself (`git rebase --continue`
or `--abort`, `git reset --keep <recorded tip>`, the reflog).

`test-image-cache-remove` is a separate explicit operation for the exact
label-verified current cache. Persistent ccache has no ordinary automatic
deletion target. During an explicitly authorized upstream refresh, obsolete or
unverifiable maintenance/test images are discarded under that runbook's exact
identity/reference checks without asking again, and rebuilt only if needed by
the reviewed current candidate. Missing historical migration reports do not
block this disposal or justify keeping an obsolete test image indefinitely.
This exception does not change the artifact planner's protection of owned or
nonempty runtime state.

After the complete work cycle is finalized, remove its retained results
through the digest-confirmed cycle flow:

```bash
make -C fork-maintenance cycle-clean-plan CYCLE=cycle-prefix
make -C fork-maintenance cycle-clean \
  CYCLE=cycle-prefix CONFIRM=<sha256-from-plan>
```

Every `RUN` and `IMAGE_RUN` in that cycle must begin with `cycle-prefix-`. The
planner blocks on transient owner/partial/abort records, unsafe retained lock
files, owned process records or Podman objects, incomplete or modified
evidence, and foreground payload or DEB validation scratch. It preserves
content-verified frozen source bundles and archives, immutable DEB selection
snapshots, input-keyed build contexts and label-verified images, ccache, and
virtual environments by default.
Plan and execution acquire the retained upstream-test lifecycle, upstream
image-cache, live lifecycle, and DEB terminal locks in that fixed order.
Before its first deletion, `cycle-clean` publishes
schema-2 `cycle-cleanups/<CYCLE>.remove.json`. It binds directory target
device/inode/fingerprint state and atomically stages those targets at
`cycle-cleanups/.<CYCLE>.<index>.remove`. Before recursive deletion it publishes
schema-1 `.<CYCLE>.<index>.rmtree.json`; a retry validates that phase and the
staging device/inode instead of re-hashing a partial tree. After interruption,
rerun the same cycle with the same confirmation digest to resume the exact
transaction.
Every `deb-remove` retains an immutable finalized status, matching hashed log,
and removal transaction; a validated success also retains its tar, while a
failed result must not. Cycle cleanup removes that exact set and rejects an
incomplete result set or orphaned/changed output.
Hosted `deb-packages/releases/` staging is not named by a local cycle and is
retained for operator review; cycle cleanup never treats it as a reusable DEB
result or deletes it implicitly.

See [`cycle-cleanup.md`](cycle-cleanup.md) for the completion boundary and full
review sequence. Caches preserved by cycle cleanup still belong to the session
and are discarded by [`artifacts-close`](session-close.md). Never add a Git
commit that archives results first.
