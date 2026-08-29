# Store And Clean Local Artifacts

## Single ignored root

All durable runtime, build, result, publication, and cache state lives below the
Xpra repository root:

```text
.artifacts/fork-maintenance/
├── build-contexts/
├── case-staging/
├── case-updates/
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
├── live-results/
├── source-archives/
├── upstream-tests/
│   ├── image-builds/
│   ├── logs/
│   ├── runs/
│   ├── sources/
│   └── workspaces/
├── tooling-venv/
├── venvs/
└── workspace-fingerprints/
```

The exact set may grow as runners add owned state, but durable run output never
moves into the tracked `fork-maintenance/` directory. Transient interpreter or
tool caches may exist only at another explicitly ignored local path; the root
`clean` Make target removes the automation's `__pycache__` entries. The root
`.gitignore` ignores all of `.artifacts/`; the `artifact-boundary` Make target
verifies that rule and checks that known runtime/result roots are not tracked.

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
- interrupted `case-new` publication is marker-owned below `case-staging/`;
  interrupted `patch-update` or `workspace-update` publication is owned below
  `case-updates/<slug>.update{,.owner.json}` under retained
  `case-updates/.lifecycle.lock`; its incomplete preparation may contain only
  the recoverable `candidate-lab/source` verification tree and exact old/new
  payloads, while a completed preparation contains `transaction.json` and no
  candidate lab; recursive cleanup owns `<slug>.update.remove.json` plus
  `.<slug>.update.remove` staging, then removes the update owner and finally the
  phase marker;
- all workspace operations and fingerprint publication are serialized by retained
  `upstream-tests/workspaces/.lifecycle.lock`; interrupted creation uses
  `upstream-tests/workspaces/.<name>.create.{owner.json,partial}`, while an
  interrupted direct removal uses `.<name>.remove.owner.json` plus
  `.<name>.remove` staging; fingerprint work uses
  `workspace-fingerprints/<name>.fingerprint{,.owner.json}`, and its recursive
  cleanup uses `<name>.fingerprint.remove.json` plus
  `.<name>.fingerprint.remove` staging;
- cycle cleanup owns schema-2 `cycle-cleanups/<CYCLE>.remove.json` and may
  temporarily stage a workspace or live result tree at
  `cycle-cleanups/.<CYCLE>.<index>.remove`; the transaction binds its exact
  device, inode, and fingerprint, and a matching
  `.<CYCLE>.<index>.rmtree.json` phase authorizes partial recursive-deletion
  recovery by device/inode.

The live analysis environment uses retained `venvs/.environment.lock` plus
deterministic `.environment.partial` and `.environment.partial.owner.json`
paths. A later `live-venv` validates the marker and recovers only that partial
under the kernel lock; cycle cleanup retains this shared environment state.

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
selection and patch digests, runner/build inputs, actual immutable image ID,
target/profile, final timestamp, complete log hash, and owned-object status. An
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
not remove retained local logs/reports, patches, cases, unrelated containers,
networks, images, or volumes.

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
add and test its exact-owned Make target before acting.

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

Interrupted case or workspace lifecycle state is not a cycle result. Inspect
its exact marker-backed state and use only the corresponding recovery target:

```bash
make -C fork-maintenance case-recover CASE=case-slug
make -C fork-maintenance workspace-recover WORKSPACE=workspace-name
```

Case recovery preserves an already published valid case, clears an owner-only
boundary only after validating that case and any bound workspace, aborts only
an incomplete update preparation, or finishes a complete `transaction.json` to
its recorded new state. Before deleting a preparation or completed transaction,
it publishes `<slug>.update.remove.json`, atomically stages the tree at
`.<slug>.update.remove`, and completes that exact removal before deleting the
external update owner and, last, the phase marker. Its canonical target array
keeps validating every published case and bound-workspace output by path, mode,
and SHA-256 even during phase-only retry. Workspace recovery preserves an
already published valid
workspace and resolves only its exact create, remove, or fingerprint
transaction. Direct removal uses `.<name>.remove.owner.json` and
`.<name>.remove`; fingerprint cleanup uses external
`<name>.fingerprint.owner.json`, `<name>.fingerprint.remove.json`, and
`.<name>.fingerprint.remove`. The fingerprint phase binds the owner operation ID
and digest, outlives that owner, and is deleted last. Unowned or ambiguous state
fails closed for operator review.

`test-image-cache-remove` is a separate explicit operation for the exact
label-verified current cache. Persistent ccache has no ordinary automatic
deletion target.

After the complete patch cycle is finalized, remove its retained results and
isolated workspaces through the digest-confirmed cycle flow:

```bash
make -C fork-maintenance cycle-clean-plan CYCLE=cycle-prefix
make -C fork-maintenance cycle-clean \
  CYCLE=cycle-prefix CONFIRM=<sha256-from-plan>
```

Every `RUN`, `IMAGE_RUN`, and `WORKSPACE` in that cycle must begin with
`cycle-prefix-`. The planner blocks on transient owner/partial/abort records,
unsafe retained lock files, owned process records or Podman objects, incomplete
or modified evidence, foreground payload or DEB validation scratch, case
creation/update/removal or workspace create/remove/fingerprint staging, and an
unexported workspace candidate. It preserves
content-verified frozen source bundles and archives, immutable DEB selection
snapshots, input-keyed build contexts and label-verified images, ccache, and
virtual environments by default.
Plan and execution acquire the retained upstream-test lifecycle, upstream
image-cache, live lifecycle, DEB terminal, workspace lifecycle, and case-update
locks in that fixed order. Before its first deletion, `cycle-clean` publishes
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

See `cycle-cleanup.md` for the completion boundary and full review sequence.

If local disk policy later requires removing retained results, use an explicit
owner-reviewed path below `.artifacts/fork-maintenance/`; never add a Git
commit that archives them first.
