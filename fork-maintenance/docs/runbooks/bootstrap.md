# Bootstrap Fork Maintenance

## Host prerequisites

Provide Git, GNU Make, Python 3.11 or newer, Ruff, and Podman. Physical profiles
additionally need access to the selected `/dev/dri/renderD*` node and, for the
Zed scenario, a readable application directory. No host service manager is a
runner prerequisite. The lifecycle creates no systemd unit and never invokes
`systemctl`; the upstream-compatible image's `libsystemd-dev` package is only a
source-build dependency.

Rootless Podman needs one subordinate UID and GID range that can supply at
least the reviewed 2048-ID allocation. The runners bound every allocating
`keep-id`, `nomap`, or `auto` namespace with an explicit `size`; they never use
`--userns=host` and never edit `/etc/subuid` or `/etc/subgid`. On the reference
host, one standard 65536-ID range is sufficient for the bounded live and
independent-container coexistence gate.

The private artifacts filesystem must support Linux anonymous temporary files
and `linkat(AT_EMPTY_PATH)`. Immutable background owner, completion, and status
records are fsynced and linked into place without a named temporary file; the
automation fails closed rather than weakening this no-clobber publication
boundary on an unsupported filesystem.

The automation never installs into system Python. Create its hash-locked live
analysis environment explicitly:

```bash
make -C fork-maintenance live-venv
```

Creation is serialized by retained `venvs/.environment.lock`. It publishes
`venvs/.environment.partial.owner.json` before using the deterministic
`venvs/.environment.partial` directory; the venv and pip children inherit the
kernel lock. After an interruption, the next `live-venv` validates that marker
and removes only its exact partial before retrying. A markerless or ambiguous
partial fails closed.

Run offline source checks before host/GPU diagnostics:

```bash
make -C fork-maintenance check
make -C fork-maintenance doctor
```

`doctor` performs no package installation and no remote mutation.

The GitHub-hosted develop test workflow uses the explicit `ubuntu-26.04` image,
which already provides Git, GNU Make, Python, and Podman. The workflow does not
install host packages; each of its three matrix jobs calls
`make -C fork-maintenance ci-upstream-tests` with one fixed
`XPRA_CI_TARGET`, and the tracked runner builds the frozen Ubuntu 26.04 test
container when its verified image is absent. No display, render node, or
hardware encoder is assumed in CI.

## Repository and remotes

The parent of `fork-maintenance/` must be the Xpra Git top level. Required
remotes are:

```text
origin    https://github.com/kogeler/xpra.git
upstream  https://github.com/Xpra-org/xpra.git
```

Inspect local state without fetching:

```bash
make -C fork-maintenance repo-status
```

Do not initialize a nested checkout. If a remote is missing or has a different
identity, stop for owner review rather than rewriting it automatically.

## Optional fork-master refresh

The normal workspace, test, live, CI-reproduction, and publication paths use
the unique source merge base already embedded in current `develop`. They do not
fetch or compare live master refs, and an older fork/local master does not block
them. The operator alone decides when to move that embedded base and begin a
new patch-adaptation cycle; only that explicit decision activates the refresh
steps below.

The hosted `master-sync.yml` workflow normally fast-forwards
`kogeler/xpra:master` from `Xpra-org/xpra:master` at 00:37 and 12:37 UTC. It
does not update local refs or rebase `develop`. See
[`master-sync.md`](master-sync.md).

When the operator chooses such a refresh and wants an immediate upstream-sync
attempt rather than relying on the most recent scheduled run, they dispatch the
same workflow from `develop` and wait for it to complete:

```bash
gh workflow run master-sync.yml --repo kogeler/xpra --ref develop
```

Agents never dispatch it. The explicit local refresh then fetches and verifies
both master refs, but only after following the initial boundary in
[`upstream-refresh.md`](upstream-refresh.md). That boundary exhaustively reviews
the non-ignored worktree and, iff legitimate changes exist, creates the one
complete preservation commit authorized by invoking the runbook without a
second confirmation. A clean checkout gets no empty commit. Require clean
porcelain and record that commit SHA or `<none>` before the first `repo-sync`:

```bash
make -C fork-maintenance repo-sync
```

The command verifies cached `origin/master` and `upstream/master` against their
live GitHub refs and requires exact fork/canonical equality. It does not modify
either remote branch. If it reports a stale fork, only the operator may run:

```bash
gh repo sync kogeler/xpra --source Xpra-org/xpra --branch master
```

Never add `--force`. Repeat `repo-sync` after the operator action.

Update only the local mirror:

```bash
make -C fork-maintenance master-update
```

This target creates or fast-forwards local `master` to fetched
`origin/master`. It rejects local master that is ahead or divergent and never
pushes it.

## Develop branch

For the first local setup only, create `develop` from the fetched fork
commit while the checkout is clean:

```bash
git switch --no-track -c develop refs/remotes/origin/master
```

Only after an operator decision to adopt a newer upstream base and completion
of the canonical runbook's one-start-commit boundary, transfer those commits by
rebasing clean `develop` onto the verified local `master`:

```bash
git switch develop
make -C fork-maintenance develop-rebase
```

If Git stops for conflicts, inspect each one against current fork-master source
and the fork-maintenance intent, edit it, and continue only after staging the
exact resolution:

```bash
git status --short
git add -- <resolved-paths>
git rebase --continue
```

Repeat until the rebase completes. If a correct resolution cannot be proved,
run `git rebase --abort` and stop. Do not begin patch work in a conflicted or
partially rebased checkout.

Merging `master`, `upstream/master`, or any equivalent upstream ref into
`develop` is forbidden. After the rebase, prove the start boundary and resolve
the active patch queue against the new base:

```bash
make -C fork-maintenance patch-start-check
make -C fork-maintenance stack-check STACK=develop
```

When published fork-only commits were replayed, the operator later publishes
the reviewed branch with the exact-SHA `--force-with-lease` procedure in
[`publish-develop.md`](publish-develop.md). Neither this automation nor an
agent pushes the rewrite.

For ordinary work on the unchanged embedded base, when fork-control files are
uncommitted, do not switch or rebase the dirty checkout. Use
`isolated-start-check` and the named workspace flow. When the operator instead
invokes the upstream-refresh runbook, its one reviewed preservation commit
normalizes that pre-existing legitimate state before fetch/rebase. After that
boundary the agent creates no intermediate or final refresh-result commit; new
changes remain uncommitted for operator review. A clean rebase is required only
when the operator intentionally changes the source boundary, not before
testing, editing, committing, or publishing the unchanged current base.

After each explicitly selected rebase, reassess the single test-quarantine case
on the new clean source in all three matrix modes before applying it. Follow
[`test-quarantine.md`](test-quarantine.md); a module which becomes green in an
assigned gate must leave that gate in the same reviewed cycle. Remove the
module and its patch path only when no gate still assigns it.

## Runtime root

Durable runtime, build, result, publication, and cache state is rooted at:

```text
.artifacts/fork-maintenance/
```

The repository `.gitignore` must ignore `.artifacts/`. Private-state helpers
create owned directories and reject symlinks or unsafe permissions. Do not
copy results into the tracked automation tree. Interpreter and tool caches may
exist only at another explicitly ignored local path. The root `clean` Make
target removes the automation's transient `__pycache__` entries.

## Upstream-test image

`test-image` verifies the input-keyed, label-verified upstream-test image and
never builds it. It uses the same exact current-source ownership verifier as a
test start, including the build-run UUID and complete maintenance label set:

```bash
make -C fork-maintenance test-image
```

If missing, build it as a durable job with a never-reused identity:

```bash
IMAGE_RUN=upstream-image-01
make -C fork-maintenance test-image-start IMAGE_RUN="$IMAGE_RUN"
make -C fork-maintenance test-image-wait IMAGE_RUN="$IMAGE_RUN"
make -C fork-maintenance test-image
make -C fork-maintenance test-image-remove IMAGE_RUN="$IMAGE_RUN"
```

The final removal deletes only reviewed transient process/context state. It
first publishes retained `upstream-tests/logs/<IMAGE_RUN>.remove.json`, then
keeps the verified image and collected local log/status/transaction result. An
interrupted removal is retried through the same exact target. Use
`test-image-abort`
for a running or lost build without collected output, or to exact-discard a
completed uncollected build only after its recorded runner becomes stale. A
current completed build must be collected. Use `test-image-cache-remove` only
for an explicit cache refresh.

Before populating `upstream-tests/image-builds/<IMAGE_RUN>/`, image start
publishes
`upstream-tests/image-builds/.<IMAGE_RUN>.image-prelaunch.json`. The retained
upstream lifecycle lock prevents concurrent terminal action during start;
after an interrupted start, `test-image-status` reports the marker and
`test-image-abort` removes only that exact marker/context. Normal remove and
abort also delete the matching marker.

Retained `upstream-tests/image-builds/.image-cache.lock` serializes image
creation, immutable-ID inspection/use handoff, and explicit cache removal. The
Podman build child inherits the open lock. The lock remains after the named run
and is validated, not deleted, by cycle cleanup.
`test-image-cache-remove` also refuses a matching image-build or test
prelaunch/owner. It can identify an otherwise exact owned cache whose recorded
source is an existing Git commit and an ancestor of, or equal to, the current
embedded source. It still requires the full label set, input/workflow identity,
and immutable image ID before removal; an unknown, unrelated, or future source
is rejected. When
an upstream rebase leaves that exact stale-source condition, remove the cache
through this target, require `test-image` to report it absent, and then run the
named build sequence above. Never use this route for an unexplained or broader
provenance mismatch.

This standalone upstream-test image lifecycle is the only image build that uses
`IMAGE_RUN`. Live and DEB runners build or reuse their images inside the parent
job and own them through that job's `RUN`.

To exercise the image-build lifecycle without replacing the normal input-keyed
tag, use a lowercase, hyphenated suffix and a unique run, then remove both
through their exact targets:

```bash
make -C fork-maintenance test-image-start \
  IMAGE_RUN=cycle-image-smoke-01 IMAGE_TAG_SUFFIX=-cycle-smoke
make -C fork-maintenance test-image-wait \
  IMAGE_RUN=cycle-image-smoke-01 IMAGE_TAG_SUFFIX=-cycle-smoke
make -C fork-maintenance test-image-remove \
  IMAGE_RUN=cycle-image-smoke-01 IMAGE_TAG_SUFFIX=-cycle-smoke
make -C fork-maintenance test-image-cache-remove IMAGE_TAG_SUFFIX=-cycle-smoke
```

The suffix changes only the local tag. The same input, workflow, source, and
owner labels are still mandatory, so the isolated image cannot replace normal
acceptance implicitly.
