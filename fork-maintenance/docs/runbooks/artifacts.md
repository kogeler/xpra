# Store And Clean Local Artifacts

## Single ignored root

All generated work state lives below the Xpra repository root:

```text
.artifacts/fork-maintenance/
├── build-contexts/
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
└── venvs/
```

The exact set may grow as runners add owned state, but it never moves into the
tracked `fork-maintenance/` directory. The root `.gitignore` ignores all of
`.artifacts/`, and `make -C fork-maintenance artifact-boundary` verifies that
rule and checks that known runtime/result roots are not tracked.

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

Never weaken ownership or no-follow checks to reuse an unsafe old directory.
Stop and let the owner inspect it.

## Immutable run identities

Every upstream-test or live job uses a unique validated `RUN`. Every image
build uses a separate unique `IMAGE_RUN`. Names are no-clobber identities and
are never reused for a retry.

A completed local result binds the exact source commit, selection and patch
digests, runner/build inputs, image identity, target/profile, final timestamp,
complete log hash, and owned-object status. Do not edit a completed report or
status file; start a new run when inputs or classification change.

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

Keep failed runs until their first failed boundary and logs have been reviewed.
An interrupted job remains inspectable.

## Exact cleanup

After review, remove only the named owned transient state:

```bash
make -C fork-maintenance test-remove RUN=name
make -C fork-maintenance live-remove RUN=name
make -C fork-maintenance test-image-remove IMAGE_RUN=name
make -C fork-maintenance test-abort RUN=unfinished-name
make -C fork-maintenance live-abort RUN=unfinished-live-name
make -C fork-maintenance test-image-abort IMAGE_RUN=unfinished-image-name
```

Cleanup verifies owner records, PID/start-time/process-group identity for host
jobs, and immutable container IDs plus Podman labels. It does not use broad
globs and does not remove retained local logs/reports, patches, cases, unrelated
containers, networks, images, or volumes.

Use an abort target only for an unfinished job without collected output. All
job lifecycle mutations go through these Make targets; do not signal processes
or call destructive Podman commands directly. If a lifecycle transition is
missing, add and test its exact-owned Make target before acting.

Collection requires current runner and supervisor digests. If automation was
updated while a job existed, its recorded digest remains usable only for
`status`, exact-owned `abort`, or removal of already-collected evidence; do not
accept or newly collect that stale job.

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
`cycle-prefix-`. The planner blocks on transient owner records, collection
locks, owned process records or Podman objects, incomplete or modified evidence,
and an unexported workspace candidate. It preserves content-addressed source and
build caches, images, ccache, and virtual environments by default.

See `cycle-cleanup.md` for the completion boundary and full review sequence.

If local disk policy later requires removing retained results, use an explicit
owner-reviewed path below `.artifacts/fork-maintenance/`; never add a Git
commit that archives them first.
