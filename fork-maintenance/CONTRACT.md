# Xpra Fork Maintenance Contract

## Purpose

This directory makes the user's Xpra fork maintainable as an ordered,
testable downstream patch queue. It keeps canonical Xpra source, fork-only
automation, patch development, isolated testing, and physical-GPU validation
in one repository without allowing fork changes onto `master`. It also keeps
fork GitHub CI as a minimal caller of this tracked automation.

## Repository identities

The enclosing Git repository is the only source checkout. It has exactly these
remote roles:

- `origin`: `https://github.com/kogeler/xpra.git`;
- `upstream`: `https://github.com/Xpra-org/xpra.git`;
- operational patch base: the unique source merge base already embedded in the
  current `develop` history.

`upstream/master` is the canonical source, but its current tip is not an input
to ordinary work on an existing `develop`. The operator owns the decision to
start a new upstream-refresh cycle. Until then, workspaces, tests, CI, and live
acceptance use the embedded source commit without a network query and without
requiring fork/canonical master equality or a new rebase.

There is no second linked Git worktree and no replaceable canonical checkout.
The isolated workflow may create a private detached copy of that embedded
source commit below `.artifacts/fork-maintenance/upstream-tests/workspaces/`.
That generated copy has no fork remote, carries no working-tree overlay, and is
never a source of branch history. Automation resolves the repository root as
the parent of `fork-maintenance/` and fails if that path is not the Git top
level.

## Branch contract

### `master`

Live remote fork `master`, cached `origin/master`, and local `master` are
operator-maintained source refs. They may lag canonical upstream between
explicit refresh cycles, and that condition never blocks work on current
`develop`.

`repo-sync` fetches `origin/master` and `upstream/master`, verifies both cached
refs against their live GitHub refs, and requires exact equality. It never
updates a remote branch, switches a branch, merges, rebases, resets, commits,
or pushes.

Remote fork `master` is a periodically synchronized upstream reference. The
dedicated hosted master-sync workflow checks its relationship with upstream
every 12 hours. When fork master is stale and can fast-forward, its service
identity runs:

```bash
gh repo sync kogeler/xpra --source Xpra-org/xpra --branch master
```

The command is never run with `--force`. The workflow verifies both live refs
afterward, changes no local ref, and fails when fork master cannot fast-forward.
Agents never invoke or dispatch this remote-mutating path. Only after the
operator explicitly chooses to refresh the embedded base does `repo-sync`
become a gate. If it reports a stale fork, the operator may run that exact
command without `--force`, then repeat `repo-sync`. `master-update` may
fast-forward local `master` only after equality is freshly proven. An ahead or
divergent fork or local branch stops that refresh for owner review; it does not
invalidate testing of the current queue.

Fork-only files, production fixes, tests not intended for upstream, merge
commits, and automation commits are forbidden on `master`. The hosted sync is
the only automation allowed to update remote `master`, and only by the
non-forced fast-forward above.

### `develop`

`develop` is the persistent fork-maintenance branch and intended default fork
branch. Its committed difference from its embedded source boundary is limited
to:

- root `AGENTS.md`;
- the root `.gitignore` runtime boundary;
- `.github/workflows/develop.yml`, `.github/workflows/master-sync.yml`,
  `.github/workflows/deb-packages.yml`, and the disabled upstream-workflow
  rename boundary below `.github/upstream-workflows/`;
- `fork-maintenance/`.

Production source changes are stored in `cases/*/fix.patch`; their applied
copies are not committed on clean `develop`. The current branch owns one unique
embedded source boundary and contains no merge commits above it. A moving
master ref does not alter that boundary.

Only an operator-selected upstream-refresh cycle rebases the fork-only
`develop` commits onto the freshly verified local `master`, followed by patch
resolution and validation against the new source. Merging `master`,
`upstream/master`, or an equivalent upstream ref into `develop` is forbidden.
If Git stops during that explicit rebase, the refresh remains incomplete until
every conflict is resolved and the rebase completes; existing pre-refresh
evidence remains historical rather than evidence for the new base.

A published `develop` is intentionally rewritten by later refreshes. Agents
and automation never publish that rewrite. The operator may do so only with an
exact expected remote SHA and `--force-with-lease`; plain `--force` is
forbidden.

`develop-check` rejects a dirty checkout, ambiguous embedded boundary,
downstream merge, committed source copies of queue patches, an unresolvable
active stack, or a missing ignore boundary. It does not query master freshness.
It remains the publication boundary, not the pre-commit isolated investigation
boundary.

`isolated-start-check` is the pre-commit investigation boundary. It requires
the checked-out branch to remain `develop`, locates the unique embedded source
commit without fetch or `ls-remote`, rejects dirty or committed Xpra source
changes, and permits local changes only in `AGENTS.md`, `.gitignore`,
`.github/workflows/`, `.github/upstream-workflows/`, and `fork-maintenance/`. It
records the branch, HEAD, cached `origin/master`, worktree status, and embedded
source commit and must not change any of them.

### Temporary branches

A clean non-master branch may be used only for exceptional host-worktree
integration diagnosis and patch operations. It must be descended from current
`develop` and have no committed changes on the selected case paths. Host patch
commands work on `develop` and such a temporary branch. The default isolated
workspace lifecycle requires
`develop` and does not support temporary branches. Parallel worktrees are
outside the supported model.

## Patch case contract

Each completed `cases/<slug>/` is either a production case or the single
`kind = "test-quarantine"` duty case. It contains:

- `case.toml`: stable identity, title, commit subject, patch digest,
  dependencies, owned paths, focused tests, and required gates;
- `fix.patch`: one atomic binary-capable Git patch containing production code
  and its regression tests, or only quarantined upstream test modules;
- `README.md`: current failure boundary, patch ownership, and required
  validation;
- optional `tests/`: case-owned functional probes.

The manifest schema retains the `[evidence]` table name for runner
compatibility, but `required_gates` is only a declarative validation list. It
does not authorize tracking reports or results.

Every patch must satisfy all of these conditions:

- SHA-256 equals `patch_sha256`;
- changed paths equal the manifest `paths` exactly;
- no path is absolute, traverses a parent, or enters `fork-maintenance/`;
- it passes `git apply --whitespace=error-all`;
- after application it passes an exact reverse check;
- every new downstream-authored source or test file carries
  `Copyright (C) <current-year> kogeler` in its native comment syntax; copied or
  derived content retains required existing notices and adds the `kogeler` line;
- a production case owns one behavior and includes at least one focused
  `unit.*` test;
- the test-quarantine case has no dependencies, changes only
  `tests/unittests/<unit-module>.py`, and binds every changed path to the exact
  module named in `[quarantine].modules`;
- the test-quarantine case requires all three clean `quarantine`,
  `quarantine-cython`, and `quarantine-no-compat` gates.

There is exactly one active test-quarantine case. It is an explicit temporary
exception, never a production fix or a place for unrelated test repair. Each
entry requires a current embedded-clean-source failure in the frozen matrix.
After every explicitly selected upstream rebase, the clean quarantine gates
invert the usual result:
they pass only when every listed module remains an exact ignored failure. A
passing module makes the case stale and must be removed or narrowed before the
quarantine patch or full patched matrix is accepted.

`case-new` creates a deliberately incomplete `draft = true` case. Drafts are
not test-selectable. Complete the human-authored fields, create a clean draft
workspace, and stage the whole candidate there; `workspace-update` removes the
draft marker and derives the digest and paths atomically without changing host
source. The clean host `patch-update` fallback can also promote a draft after
its publication start gate. Derived fields are never edited by hand.

Only these active cases are retained:

1. `wayland-initial-window-state`;
2. `wayland-empty-damage-throttle`;
3. `video-pipeline-cleanup-race`;
4. `debian-libva-codecs-package`;
5. `upstream-test-quarantine`.

## Stack contract

`stacks/develop.toml` is the complete active queue in dependency/application
order. Here `develop` is a stable queue slug, not a Git-branch precondition for
branch-agnostic consumers such as DEB packaging. A stack contains unique known
cases, places selected dependencies before their consumers, and declares the
union of integration gates.

The resolver evaluates each patch against an isolated snapshot in sequence:

- `apply`: forward check succeeds and reverse check fails;
- `already-present`: forward check fails and exact reverse check succeeds;
- `ambiguous` or `diverged`: both or neither succeeds, so the operation stops.

An already-present patch remains named in resolution provenance until its case
is deliberately retired. A divergent patch is refreshed; it is never applied
with rejects, fuzz workarounds, source rewriting, or whitespace relaxation.

## Isolated patch lifecycle

The default pre-commit patch cycle never applies a production patch to the host
worktree. `workspace-create` makes a no-clobber named directory under the
ignored private runtime root, copies the complete embedded source commit
into a detached local checkout, removes its remote, resolves the selected case or
stack, and applies either the complete patch, only its retained tests, or
nothing. The copied tree and its private index are generated runtime state.

A new draft case uses `PATCH_MODE=clean`. Its initially empty patch is not
resolved or applied; edits are made only in the detached workspace. Promotion
validates the completed manifest, derives the patch and owned paths, and
updates workspace provenance to the new completed case.

An atomic case workspace may be edited and staged with `workspace-stage`.
`workspace-update` requires the same host branch and HEAD, a complete staged
candidate, no untracked or unstaged workspace output, exact path ownership
unless explicitly expanded, and deterministic forward/reverse application to
the recorded embedded source commit. It then atomically replaces that case's
`fix.patch`, `patch_sha256`, and `paths` together with the workspace selection
resolution and metadata. A successful workspace remains current and may be
edited, staged, and exported again. It never stages the host index or edits
inherited Xpra source in `develop`.

Every host `patch-update` and isolated `workspace-update` publishes an ignored
case-update owner and exact old/new payload transaction below
`case-updates/<slug>.update{,.owner.json}` before replacing a tracked target.
Forward-application and exact-reverse verification use the preparation-only
`case-updates/<slug>.update/candidate-lab/source` tree. They never create
verification scratch in the finalized workspace; the candidate lab is removed
before the transaction marker is published.

Every workspace export binds the case patch/manifest and that workspace's
resolution and metadata in the same all-new transaction. Draft promotion moves
the workspace from clean to patched mode; an existing case remains patched. A
complete `transaction.json` is replayed to the recorded new state after
interruption; preparation without that marker is discarded. An owner without a
transaction is cleared only after the currently published case and any bound
workspace validate. Only `case-recover CASE=<slug>` may perform these actions,
under the retained global `case-updates/.lifecycle.lock`.

Before recursively deleting either a completed transaction or an aborted
preparation, the case lifecycle publishes external schema-1
`case-updates/<slug>.update.remove.json` with
`kind=case-update-rmtree-started` and disposition `complete` or `abort`. It
binds the transaction's full fingerprint, device, inode, update owner, and
operation identity before a no-replace rename to
`case-updates/.<slug>.update.remove`. After that rename, a retry validates the
bound device/inode because recursive deletion necessarily changes the tree. The
canonical `targets` array continues to validate the exact published patch,
manifest, and any bound workspace resolution/metadata by path, mode, and
SHA-256. After the tree is gone, cleanup deletes the update owner and only then
the removal marker. A crash in that interval leaves a valid phase-only retry
that still validates its recorded operation, workspace, owner digest, and
published targets.

`PATCH_MODE=tests-only` applies only paths below `tests/` from the selected
patch. This is the durable non-vacuous clean-source regression mode: retained
tests execute against unmodified production code. A tests-only workspace can
never replace a complete case patch.

All workspace operations and fingerprint publication are serialized by retained
`upstream-tests/workspaces/.lifecycle.lock`; workspace update acquires it before
`case-updates/.lifecycle.lock`. Workspace cleanup validates the exact name and
ownership record. Before its first destructive step it publishes schema-1
`upstream-tests/workspaces/.<name>.remove.owner.json`, binding the complete tree
fingerprint, device, and inode, then atomically renames the target to
`.<name>.remove`. A retry of `workspace-remove` or `workspace-recover` validates
the staged device/inode, completes only that removal, and deletes the external
owner last. An identity is never reused.

Case creation first publishes
`.artifacts/fork-maintenance/case-staging/<slug>.create.owner.json` and may then
own the matching `.create.partial`. Workspace creation uses deterministic
`upstream-tests/workspaces/.<name>.create.{owner.json,partial}` paths, and its
finalization audit first publishes external schema-2
`workspace-fingerprints/<name>.fingerprint.owner.json`, then may own the matching
`<name>.fingerprint/` scratch. Scratch cleanup publishes schema-1
`<name>.fingerprint.remove.json` (`kind=workspace-fingerprint-rmtree-started`)
before a no-replace rename to `.<name>.fingerprint.remove`; a partial retry is
bound by device/inode. The phase also binds the fingerprint-owner operation ID
and digest. After the tree is gone, cleanup deletes that owner and then the
phase marker last, leaving a valid phase-only retry boundary. Interrupted
create, remove, fingerprint, or update state is recoverable only through
`case-recover CASE=<slug>` or
`workspace-recover WORKSPACE=<name>`. Each target validates the marker and
exact entry set. Case recovery preserves a valid completed create, aborts only
an incomplete update preparation, completes an already published update
transaction, or finishes its removal phase. Workspace recovery preserves a
valid completed create and finishes only its exact marker-backed create,
remove, or fingerprint transition. Unowned or ambiguous state fails closed.

Final cleanup of a complete work cycle uses a common lowercase prefix for all
of its `RUN`, `IMAGE_RUN`, and `WORKSPACE` identities. `cycle-clean-plan`
is branch-agnostic and does not require a named remote; it never reads or
changes a branch or ref. It rejects any dirty host Xpra source regardless of
where it is invoked, then
requires transient job ownership to have been collected and removed, verifies
collected log/report hashes, and proves every matching workspace has no
unexported candidate and is exactly represented by the current patch queue. It
prints an exact target set and its confirmation digest. `cycle-clean` rebuilds
that plan when no transaction is pending and removes it only when the supplied
digest still matches. Before the first deletion it publishes
`cycle-cleanups/<CYCLE>.remove.json`; an interrupted deletion validates and
resumes only that stored plan with the same digest. Its schema-2 record also
binds every workspace/live-result directory's device, inode, and fingerprint
before atomically staging it at
`cycle-cleanups/.<CYCLE>.<index>.remove`. Before recursive deletion of that
staging, cleanup publishes schema-1
`cycle-cleanups/.<CYCLE>.<index>.rmtree.json` with
`kind=cycle-clean-rmtree-started`, binding the outer transaction and staging
identity. A retry validates the phase and device/inode rather than trying to
match the original fingerprint after partial deletion; the phase is removed
only after that staging is gone.

## Host patch lifecycle

Host patch application is an exceptional integration tool for an explicitly
selected upstream-refresh cycle. `patch-start-check` then re-verifies equal
live fork and canonical master refs, exact local master, linear rebased
`develop`, and the ancestry of any temporary branch. Committed versions of all
patch-owned source paths must match that refreshed master. Ordinary patch work,
investigation, and acceptance use `isolated-start-check` and the embedded-base
isolated lifecycle above, with no sync or rebase prerequisite.

`patch-apply` or `stack-apply` applies only resolver entries in `apply` state
with `git apply --index --whitespace=error-all`. It stages source and test
files, verifies the exact staged path set, and rejects unstaged or untracked
output. Partial failure reverses every successfully applied case in reverse
order. A failed rollback is reported as a hard dirty-state boundary.

For development, edit the applied files and stage the complete candidate.
`patch-update` requires no unstaged or untracked files, checks the exact path
contract, proves the new full patch applies and reverses on refreshed master, and
atomically replaces `fix.patch`, `patch_sha256`, and `paths`. It leaves the
source candidate staged for review.

`patch-unapply` or `stack-unapply` requires that the staged paths exactly equal
the effective queue. It reverses in dependency-safe order and restores the
committed source. After `patch-update`, changes to only that case's
`case.toml` and `fix.patch` may remain unstaged while unapplication runs.

The normal committed result on `develop` is the maintained patch and its
metadata, not the applied source copy.

## Explicit upstream refresh contract

Only when the operator decides to move `develop` to a newer upstream source
commit, use this clean sequence:

1. require a clean checkout;
2. run `repo-sync`;
3. fast-forward local `master` with `master-update`;
4. switch to `develop` and run `develop-rebase`;
5. resolve and stage every conflict, then use `git rebase --continue` until the
   rebase completes; abort and stop if correct resolution is not possible;
6. run `patch-start-check` and resolve the complete active stack against new
   master;
7. run every clean quarantine gate and remove or narrow entries that no longer
   fail on this exact master;
8. run the complete offline fork-control suite and the tests-only control for
   every production case, then review whether upstream replaced or narrowed any
   patch behavior;
9. run every patched focused and native gate, all three complete upstream test
   legs (`full`, `full-cython`, and `full-no-compat`), and all six fixed positive
   live profiles, even if the patches applied without textual changes;
10. reproduce any newly failing author test on this exact clean master before
    adding it to the single duty quarantine, then rerun its clean quarantine
    gates and the complete patched matrix;
11. run `develop-check` before handoff.

This explicit sequence fetches both master refs and requires live
fork/canonical equality. If step 2 reports a stale fork, the operator may run
the exact non-forced `gh repo sync` command printed by the gate, then repeat
step 2. Agents never run that remote mutation. Publishing, testing, or editing
the unchanged current `develop` does not implicitly start this sequence.

No form of `git merge master`, `git merge upstream/master`, or an equivalent
merge is accepted as upstream transfer. `develop-check` rejects merge commits
above the embedded source boundary.

Ordinary pre-commit investigation starts with `isolated-start-check`; its
outputs are bound to the embedded source commit and patch digests. Old reports
and previous successful resolution are not proof after an explicit base move
or executable/test semantics change. A digest-only patch refresh on an
unchanged embedded source may reuse prior functional results solely under the
non-semantic validation exception below. Moving the embedded source by rebase
always invalidates prior functional acceptance. Do not delete an active patch
merely because nearby upstream code looks equivalent. Review the exact
production path and run the retained tests-only regression first. Once a patch
is proven exactly present or fully replaced, remove it and update the stack in
one reviewed change; do not create a tracked history archive.

## Source and runner provenance

Local upstream-test and live acceptance runners and hosted develop test CI
freeze the unique source merge base already embedded in their `develop`
checkout, never the mutable working tree or a moving master tip. Cached
`origin/master` is only the local history anchor used to locate that commit.
None of these paths fetches or queries live master refs. Each test or live run
binds:

- exact embedded source commit and workflow digest;
- selection manifest and patch digests;
- base-aware resolution and its digest;
- runner/build-input digests;
- image identity and toolchain versions;
- exact target or live acceptance profile, selected client network profile,
  and frozen live CLI configuration;
- final process/container state and complete log digest.

Build contexts contain a credential-free source archive plus the selected
patch inputs. They exclude `.git`, remotes, credentials, global configuration,
ignored paths, and unrelated working-tree state.

A detached upstream-test start publishes
`upstream-tests/runs/<RUN>.prelaunch.json` before `podman create`. That record
binds the starter PID/start ticks, run UUID, payload path, immutable image ID,
and complete expected maintenance labels. The final owner is published only after the
created container's immutable ID and labels match. An active starter cannot be
aborted concurrently; once it is gone, the prelaunch record is sufficient to
inspect and exact-reclaim only its orphaned container/payload state.

The prelaunch record remains until the container is started and the complete
source/selection payload is delivered through the readiness FIFO. The retained
subsystem lifecycle lock is inherited across selection freezing, container
creation and start, and payload streaming, so abort cannot reclaim an in-flight
publisher. The Python starter holds the matching image-cache lock through
immutable-ID handoff and payload delivery, but does not pass it to Podman's
long-lived networking helper.

Hosted foreground tests use deterministic
`upstream-tests/.foreground-payload{,.owner.json}` staging under retained
`.foreground-payload.lock`. The same foreground operation validates and
recovers only that marker-owned partial before reuse; cycle cleanup treats a
remaining payload or marker as active state. Upstream image creation,
inspection/use handoff, and explicit cache removal are serialized by retained
`upstream-tests/image-builds/.image-cache.lock`; Podman children inherit the
open lock only for image builds. A detached test starter holds the lock itself
through the immutable-ID handoff without passing it to the test container's
long-lived helpers.
Exact cache removal takes the same lock and refuses any matching unresolved
image-build or test prelaunch/owner. It may recognize an older valid source
label only for cleanup, while still requiring the complete exact maintenance-label set,
image input/workflow identity, and immutable image ID.

A named standalone image start publishes
`upstream-tests/image-builds/.<IMAGE_RUN>.image-prelaunch.json` before creating
or populating its context. The final image-build owner uses schema 3 and is
released only after durable owner publication. Status and abort recognize the
prelaunch marker; normal remove/abort deletes it. Hosted foreground image
creation streams the tracked context inputs directly and creates no
`.ci-image.*` host staging directory.

A live start first publishes
`jobs/live/<RUN>.freeze-prelaunch.json`, then launches and durably publishes the
owned input-freeze process record. Before the main live owner exists, that
process freezes one source archive, the complete harness, server and
clean-client selection snapshots plus resolutions, both validated context
archives/tree digests, the optional Zed archive, and the manifest/checksum tree
below run-owned staging. The validated tree is atomically published as
`live-results/<RUN>/inputs`; the main worker launches from the frozen harness
and reads only those bound inputs. Final report/status validation binds the
source, both selections and context/resolution digests, Zed/harness/input
digests, and actual immutable image IDs with complete ownership labels. The
retained live subsystem lifecycle lock is acquired before the prelaunch marker
and held through durable main-owner publication, excluding terminal transitions
from that handoff. Status and abort route through the exact prelaunch/freeze
boundary before the main owner exists. Before deleting freeze-owned input
directories, abort publishes schema-1 `kind=live-input-freeze-abort` at
`jobs/live/<RUN>.freeze-abort.json`, binds each device/inode, atomically moves it to
`live-results/.<RUN>.freeze-abort-{staging,result}`, and deletes the transaction
only after both exact directories are gone. An interrupted transaction is
continued only by the same `live-abort` target.

The two tracked YAML files at the maintenance root are the sole value authority
for live Xpra command options. `profiles.yml` declares the named client-side
quality/network profiles and its `default_profile`; `live-cli.yml` declares
static blocks grouped by server/client role, command concern, transport,
encoding, and policy. The common client `bandwidth-detection=no` setting belongs
to the static client base, while minimum quality/speed, auto-refresh delay,
refresh rate, and bandwidth limit come only from the selected network profile.
Those profile values are never passed to the server.

`infra/live/live_config.py` parses a deliberately small deterministic YAML
subset with the standard library and rejects malformed, ambiguous, unsafe, or
out-of-range data. Python and Make may derive values from that loader but may
not maintain duplicate option tables or defaults. Unit tests validate the
schema, fail-closed behavior, and loader-to-runner data flow generically; they
must not copy concrete profile names, arguments, or values into assertions.
Both YAML files and the loader are part of the frozen harness digest. The main
owner and final report bind the selected network-profile name.

Every public live wrapper accepts `NETWORK_PROFILE=<name>`. Omitting it uses
the `default_profile` declared only in `profiles.yml`. The normal required
six-gate acceptance ladder runs once with that default. Other tracked
network profiles exercise the same positive gates on operator request; they do
not create additional mandatory gates or weaken any rendering, codec,
lifecycle, or cleanup assertion.

DEB source selection is branch-agnostic. It uses the current `HEAD` and all
local or remote-tracking refs whose final component is exactly `master`, then
requires one uniquely latest merge base. It never fetches, syncs, or requires a
current branch or remote name. The range from that clean boundary to `HEAD`
must contain no merge commit and may touch only fork-control paths; any dirty or
committed Xpra source copy is rejected. The selected exact master ref is frozen
in a private bundle only as provenance. Every package run freezes the complete
active queue into one private immutable selection snapshot before container
execution; publication reuses one snapshot for both distributions and rejects a
live selection change across the builds. Package builds apply that queue,
selected by the stable `stacks/develop` slug, to the clean boundary inside the
container. They require an x86-64 Podman host and produce amd64 packages, need
network access and sufficient disk space, and invoke `dpkg-buildpackage`
unsigned with `-us -uc`. The build adds `noautodbgsym` to `DEB_BUILD_OPTIONS`,
and both the container output boundary and host validator reject any package
whose filename or control package name identifies it as dbgsym. Such packages
are neither retained in the tar nor publishable. The build forces
xz-compressed Debian control and data members; the Python 3.11+ host validator
streams and validates those exact members with a 256 MiB XZ decoder memory
limit before accepting a tar.

The Debian packaging closes the complete staged-file ownership boundary with
`dh_missing --fail-missing`. Every regular build result must belong to one
binary package or match the exact reviewed `packaging/debian/xpra/not-installed`
set. That exclusion contains only Debian-replaced generic systemd units, the
unintegrated encoder service units, and the Wireshark dissector that cannot be
placed correctly without the optional Wireshark build environment. Compiled
codec trees, Python package metadata, and installed server helpers are never
exclusions.

Before emitting its tar, the builder reads the control and data archives of
every actual DEB and rejects duplicate package names or overlapping regular
payload ownership. It resolves the required libva encoder, libva decoder, and
libyuv converter from that complete inventory, requires one matching amd64
CPython ABI for each, and requires all three to belong to ordinary
`xpra-codecs`. It then extracts the actual `xpra-common` and `xpra-codecs`
packages into a private root, imports those modules with the distribution
Python, and runs `dpkg-shlibdeps` on the packaged ELF objects. Every dynamically
resolved library dependency must occur in the final `xpra-codecs` `Depends`,
including `libva-drm2`, `libva2`, and `libyuv0`; the package must not depend on
an Xpra vendor-specific or extras codec package. The host independently parses
the returned Debian archives and repeats the package-set, payload ownership,
module ABI, and dependency checks without trusting the builder manifest.

The builder uses only the configured Ubuntu or Debian distribution archives
for Debian build dependencies. It does not install the source tree's
`xpra.sources`, trust an Xpra repository key, or consume prebuilt Xpra
packages. Any dependency unavailable from the target distribution fails the
build rather than silently adding another package source.

The input-keyed builder image cache is verified by its full input labels. Each
created package container executes the actual immutable image ID, and each
accepted result records it. The result binds the checkout commit, selected
master ref and commit, source commit, workflow digest, selection and resolution
digests, base and builder image IDs, builder-image input digest, version,
sequential revision, architecture, and exact DEB checksums.

The retained DEB queue cache has the exact shape
`selections/<selection-sha>-<metadata-sha>/{lab,selection.json}`. The first
digest is the semantic complete-queue digest; the second is the SHA-256 of the
metadata file, which binds the exact private `lab/` tree digest. A local package
owner records both digests and the absolute selection-state/snapshot paths before
its worker starts. Local Make freezes the source snapshot before invoking
package start, then package start freezes the selection before creating its
`RUN`; hosted publication freezes one selection and one source snapshot before
either distribution build and reuses both across them.

A local package start publishes
`deb-packages/runs/<RUN>.prelaunch.json` before creating the run directory or
main process owner. Status and logs expose this boundary. Before changing an
ownerless prelaunch or owned run, `deb-abort` publishes
`deb-packages/runs/<RUN>.abort.json`; status reports that exact aborting phase,
and a retry validates and completes the transaction before deleting its marker
last. Each mutable builder-image key is serialized by retained
`deb-packages/locks/images/<distro>-<input-sha>.lock`, and the Podman build child
inherits that lock until immutable-ID handoff.

Package-output validation owns only the deterministic sibling paths
`.<tar>.validate`, `..<tar>.validate.partial`, and
`.<tar>.validate.owner.json`. The marker binds the exact output path, device,
inode, size, and both scratch paths. For named local output, a later validation,
`deb-remove`, or `deb-abort` may recover only that exact marker-backed scratch;
unowned or changed state fails closed and blocks cleanup of its matching cycle.
Hosted scratch remains in its release-attempt staging for operator review.

Every Podman build context, source snapshot, patch selection, live application
input, and returned artifact crosses the process boundary through
`tools/container_payload.py` as a validated streaming tar. Extraction accepts
only plain, uncompressed tar and separately bounds raw archive bytes, member
count, expanded content, and PAX/GNU extended metadata; sparse entries,
transparent compression, concatenated streams, and trailing bytes are rejected.
Bind mounts, bind-style `--mount`, and `podman cp` are forbidden for these
transfers. The upstream-test ccache named volume is a cache-only exception;
render-node `--device` access is hardware access. Upstream unit tests return no
artifact tar: their normal logs contain the selected resolution digest and test
output.

Their entry process waits on a pre-created, validated payload-ready FIFO and
executes only after extraction writes its ready byte. Payload readiness uses no
process signal; the sender retries a non-blocking FIFO open only for a bounded
interval while the reader attaches. Each extraction uses the deterministic
`.<destination>.partial` sibling, refuses any pre-existing partial, and
publishes with an atomic no-replace rename rather than random staging.
When reverse process output has no caller-owned deterministic partial path, the
common exchange helper stages it in an anonymous `O_TMPFILE`, fsyncs it, and
links it into place without replacement; there is no named generic fallback.

Rootless container creation uses bounded subordinate-ID allocations. Every
explicit `keep-id`, `nomap`, or `auto` user namespace must declare a positive
`size`; `--userns=host` is forbidden. The reviewed allocation is `size=2048`:
it contains the live UID/GID 1001 and upstream-test UID/GID 1000 while retaining
1046 or 1047 mapped IDs above the runtime identity. The live Ubuntu 26.04
server, Debian 13 client, and Ubuntu 26.04 upstream-test images must create,
run, and write their owned paths with that span. No runner may compensate by
enlarging `/etc/subuid` or `/etc/subgid` or by consuming the host namespace.
This bound leaves the remaining subordinate-ID range available to an
independent bounded rootless container while a live server/client pair exists.

## GitHub CI contract

Canonical Xpra workflow files are never executed on `develop`. Every
`.yml`/`.yaml` file from the embedded
source commit's `.github/workflows/` is relocated without content changes to
the same relative name below `.github/upstream-workflows/`. The executable
directory contains exactly `.github/workflows/develop.yml`,
`.github/workflows/master-sync.yml`, and
`.github/workflows/deb-packages.yml`.
`ci-layout-check` compares the disabled files byte-for-byte with that embedded
source, rejects a missing, extra, edited, or still-active upstream
workflow, and checks all exact thin fork workflow interfaces. After a rebase,
upstream edits are resolved as rename/modify updates; any new canonical
workflow is relocated in the same cycle before publication.

`ci-layout-check` is an explicit publication audit. `ci-prepare`
does not invoke it: once GitHub has selected the workflow, a self-check of its
tracked filename or layout must not prevent the upstream test matrix from
starting.

The develop test workflow:

- triggers only on a push to `develop`;
- uses the GitHub-hosted `ubuntu-26.04` runner, a six-hour timeout, and read-only
  contents permission;
- pins each external action to a reviewed full commit SHA and records the
  corresponding current release version in an inline comment;
- requires a clean checkout at the exact hosted `GITHUB_SHA` before reading
  any queue or runner input;
- declares the exact `full`, `full-cython`, and `full-no-compat` matrix with
  fail-fast disabled and `max-parallel: 3`;
- fetches full checkout history without persisting credentials and, for each
  matrix value, invokes exactly
  `make -C fork-maintenance ci-upstream-tests` with that value in
  `XPRA_CI_TARGET`.

The master-sync workflow:

- runs at minute 37 every 12 hours and supports operator `workflow_dispatch`;
- uses the GitHub-hosted `ubuntu-26.04` runner with a ten-minute timeout;
- grants only its job `contents: write` and pins checkout to the reviewed full
  action SHA and `develop` at depth one without persisting credentials;
- invokes exactly `make -C fork-maintenance ci-master-sync` with the ephemeral
  job token;
- accepts only its expected hosted event, repository, workflow ref, develop
  ref, and exact checkout SHA;
- compares live fork and upstream master before and after the operation;
- calls only `gh repo sync kogeler/xpra --source Xpra-org/xpra --branch master`
  and never adds `--force`;
- leaves the develop checkout, local refs, commits, and worktree unchanged.

The scheduled sync updates only fork `master`. It never rebases, merges, signs,
commits, or publishes `develop`; the operator may later choose to start the
documented local `repo-sync` / `master-update` / `develop-rebase` cycle. Until
that explicit decision, a newer master has no effect on current `develop`
testing. An ahead, divergent, missing, concurrently moving, unauthorized, or
post-sync unequal ref fails the sync itself closed for owner review.

The package-release workflow:

- supports only operator `workflow_dispatch` on the selected branch or tag
  revision;
- does not hard-code the selected branch name and pins checkout to the reviewed
  full action SHA with `fetch-depth: 0` and no persisted credentials;
- runs on GitHub-hosted Ubuntu 26.04 with a six-hour timeout and job-scoped
  `contents: write`;
- invokes exactly `make -C fork-maintenance ci-deb-release` with the ephemeral
  job token;
- accepts only its exact hosted event, repository, workflow path/ref and
  checkout SHA, and requires a clean checkout, GitHub CLI 2.97.0 or newer, and
  Podman;
- runs no live, display, render-node, or hardware-codec profile;
- builds and validates one complete non-debug Xpra DEB tar for Ubuntu 26.04 and
  one for Debian 13 from one frozen selection snapshot and requires a common
  version/revision;
- updates no local checkout or source-selection ref; publication alone stages
  one draft with `prerelease=false` at the exact checkout SHA through
  authenticated REST, records its immutable release ID directly from the
  create response, uploads and validates exactly those two assets, publishes
  the draft as an ordinary release whose title is exactly the Debian version,
  and verifies its unique package tag target; it never looks up a draft through
  the published-only `/releases/tags/{tag}` endpoint, and every later release
  query, upload, publish, or deletion addresses the captured immutable ID;
- on publication failure, may delete only the release it created and its tag
  while the tag still points at that checkout SHA. It deletes and verifies tag
  absence first, then deletes and verifies the immutable release ID last; a
  missing release with an extant tag or any changed tag fails cleanup closed for
  owner review;
- before publishing a retried attempt, may remove only an exact draft/tag left
  by an earlier failed attempt of the same hosted workflow run, after validating
  that Actions attempt and the canonical embedded transaction, release ID,
  asset subset, checkout, version, and unchanged tag target, using the same
  tag-first/release-last order. Current-tag absence and exact orphan recovery
  use a bounded authenticated pagination of the release collection and require
  one unique matching transaction. If the create response is malformed, only
  that same listing may discover one exact draft, journal its immutable ID, and
  enter the ordered rollback;
- after publishing and verifying the current ordinary release, scans that
  complete bounded listing for canonical ordinary DEB releases owned by this
  workflow, orders them by `published_at` with immutable release ID as a stable
  tie-breaker, retains the three newest, and deletes every older owned release
  by verifying and deleting its exact tag first and its immutable release ID
  last. Drafts and unrelated or manual releases are excluded. Malformed,
  changed, duplicate, or ambiguous owned state fails closed before further
  deletion. A retry may resume this retention only from an exact published
  release belonging to a failed or cancelled earlier attempt of the same
  hosted run, without creating a duplicate release.

No dependency installation, Podman command, patch logic, test command, dynamic
test discovery, fallback, skip policy, or cleanup implementation belongs in
YAML. The fixed `develop.yml` matrix is only hosted-runner fan-out; Make
validates its value. Each of its three test jobs derives the
`develop`/checkout-`origin/master` merge base, freezes that already embedded
commit without a live remote query, applies the complete `stacks/develop` queue
inside the container, and runs exactly its selected leg. The jobs run
independently, and one failure does not cancel the remaining legs. The
master-sync and package-release workflows have no test matrix.

The develop test and package source/build paths never fetch, sync, switch,
merge, or rebase after `actions/checkout` has populated the checkout. A local
operator who explicitly elects to move the upstream base separately verifies
both live master refs and rebases onto that commit. Publication and testing of
the unchanged current base do not imply that refresh.

CI never invokes a `live-*` target and cannot satisfy RGB, render-node,
Wayland hardware-H.264, Vulkan, input, detach, or transport-loss acceptance.
Those profiles require the local physical environment. The hosted Actions job
is the outer foreground lifecycle and log owner for `ci-upstream-tests`; local
acceptance jobs continue to use unique named runner lifecycles and local
evidence.

## Validation contract

For one patch, validate in increasing scope:

1. embedded-clean-source failure or another non-vacuous focused regression;
2. selected focused tests;
3. affected native/subsystem checks;
4. all three clean quarantine reassessment gates when the active queue contains
   the duty quarantine case;
5. `full`, `full-cython`, and `full-no-compat`;
6. each live gate declared by the production cases and complete stack.

A proven non-semantic refresh does not restart this ladder. It requires the
same embedded source commit plus an exact old/new applied-tree diff limited to
comments, copyright notices, or documentation. Paths, modes, executable data,
configuration, test assertions, source selection/application, build commands,
runner behavior, and live assertions must remain unchanged. Refresh derived
digests, resolve the current queue, run whitespace checks and the affected
fork-control tests, and describe the proof at handoff. Do not spend container,
native, full-matrix, or live resources on that refresh. If any condition is
uncertain, the exception does not apply and the normal ladder is required. A
`develop-rebase` necessarily changes the embedded source and therefore never
qualifies: its acceptance always includes the complete fork-control suite,
clean quarantine reassessment, production tests-only controls, patched focused
and native gates, all three author-test legs, and all six fixed positive live
profiles.

Ordinary acceptance is green. A pre-existing failure outside the selected
paths is investigated against canonical CI before any costly local clean
control. It is never skipped, weakened, reconfigured, or fixed inside a
production case. With explicit user scope, an exact current clean-source
failure may be added only to the duty quarantine case and must then satisfy its
reassessment gates. No exception is implied by prior acceptance results.

A failure before a downstream test target starts is a control-plane failure,
not a failed Xpra test. When the change only removes or narrows that pre-test
guard, validate it with the focused automation unit test and the exact direct
preflight command. Do not run the expensive downstream matrix if the exact
frozen fork source commit, selection and patch digests, image inputs, container
entrypoint, and test commands are unchanged: those tests cannot validate
whether the removed guard still blocks them. Any change to a downstream input
or execution path restores the normal validation ladder.

The live runner keeps direct Xpra boundaries distinct from SSH orchestration.
Its exact positive set is Zed RGB, adaptive-alpha Zed H.264, RGB detach, RGB
direct-TCP transport-loss fault injection, multi-window Vulkan/input hardware
H.264, and multi-window native-Wayland OpenGL/input hardware H.264. Each fixed
Make wrapper binds every profile dimension and every named job requires a
nonempty reviewed case or stack selection. Foreground, clean-source, and
picture-fallback probes are diagnostic and cannot publish acceptance. A
positive fault-injection profile first proves rendering and input, then proves
the intended disconnect and survival behavior.

H.264 acceptance deliberately assigns different CSC roles to the endpoints.
The server enables `libyuv` to convert Wayland `BGRX`/`RGBX` source buffers to
the `NV12` input required by the libva encoder. The client disables software
CSC with `--csc-modules=none`: its libva decoder returns `NV12`, and the forced
native OpenGL backing must consume those planes through its GPU shader before
presentation. Enabling client-side `libyuv` is diagnostic only. It may broaden
advertised conversion modes or provide a CPU `NV12`-to-RGB fallback, but cannot
restore alpha already discarded by H.264, repair server codec cleanup, or
satisfy the direct hardware-presentation gate. CSC-module selection is also
independent of the codec allowlist; it must not be treated as codec discovery.

The two named multi-window hardware-H.264 gates are the fixed application-exit
profiles `APPLICATION=hardware` and `APPLICATION=opengl`, both with
`ENCODING=h264`, `H264_CLIENT_POLICY=adaptive-alpha`, and
`ALPHA_SCENARIOS=default`. Each resolves its primary and the common auxiliary
GTK Xpra window independently from their exact titles; registration order is
never authority. The primary's first saved
`window.info` is only an initial `BGRX`/`RGBX` snapshot. Every exact-window
frame-state record must remain opaque, and its complete saved packet history
must have positive contiguous sequence numbers in recorded order. The rounded
damage-time directory is storage only: one millisecond may contain multiple
damage groups, which are reconstructed by each exact descending `flush`
countdown. Startup layout/picture groups remain structurally validated but are
not production evidence. Once both title-bound windows are stable, the runner
records a baseline against the active exact IDR group and its saved source
geometry, then an end sequence before auxiliary exit. Each group in that interval has
contiguous sequences, one terminal positive H.264 main region, and only the
exact positive one-pixel right or bottom lossless RGB24/RGB32 edges allowed by
its crop. Every observed `(window-size, main-region-size)` crop signature must
gain one complete required edge set in the interval; an unchanged edge need not
be resent with every H.264 frame. Missing signature coverage, duplicate,
dangling, cross-group, arbitrary, interior, larger, or alpha-bearing RGB regions
fail.

The H.264 main path must contain at least ten frames spanning at least one
second, cover at least 99% of each production window, and account for at least
90% of all encoded packet-region pixels. Its dominant stream must complete the
exact VA-API encode/decode, client-packet, hardware-presentation, and pixel
chain; safe warmup and post-auxiliary resize packets are not production
evidence. The complete H.264 context suffix remains VA-bound through final
quiescence. Normally the client decode count equals its transmitted H.264
packet count. Ordered shutdown may leave exactly one received post-stimulus
terminal packet incomplete on the client, or one completed terminal server
encode untransmitted; either exception requires an otherwise exact complete
packet sequence and is recorded. Larger or in-production differences fail.

The auxiliary fixture must require an RGBA visual and expose a deterministic
transparent border around its opaque input control. Its exact window must
report `BGRA` or `RGBA`, expose both transparent and fully opaque pixels in
every collected server-side source screenshot scoped to that window, and
produce a nonempty set containing only positive WebP or RGB32 packets with
contained geometry and exact group metadata. These GLib-idle screenshots are
window-level alpha samples, not packet-correlated evidence; ordered saved-packet
and frame-state log records provide the packet-to-state binding. Client
captures prove visible composition and input response; an X11 compositor is
not required to preserve the source alpha channel in those captures. An RGB32
packet is accepted only with `BGRA` or `RGBA` `rgb_format`. H.264, RGB24,
non-alpha RGB32, or an opaque auxiliary source format fails the auxiliary
contract. A software H.264 encoder or decoder or software presentation renderer
fails the complete gate. The Vulkan profile additionally requires live
`vkcube` RADV/render-node use. The OpenGL profile instead requires the
native-Wayland `glmark2-wayland` synthetic OpenGL `jellyfish` benchmark with a
no-alpha EGL visual, immutable
vendor/renderer/version metadata from its quiesced output, an AMD Mesa/Radeon
non-software renderer and driver mapping, the selected render node, and
changing nonuniform forwarded frames. When its fixed source viewport is smaller
than the tiled client backing, the exact logged OpenGL viewport binds the
north-west source crop used for source-to-client pixel and channel-order proof.
Both profiles then use the same VA-API encode/decode and client hardware-OpenGL
presentation boundary.

The adaptive-alpha Zed H.264 profile applies the same positive principle to one
dynamic window. Exact saved-packet records bind H.264 to opaque frame state and
alpha-bearing RGB32 to alpha state; WebP is alpha-capable but is not itself
proof of an alpha frame. Every H.264 stream is bound to complete VA contexts,
and the owned stable-geometry stimulus must meet the same temporal, per-frame,
and aggregate pixel-dominance thresholds.

## Runtime storage contract

All durable runtime, build, result, publication, and cache state lives below
repository-level:

```text
.artifacts/fork-maintenance/
```

This includes source archives and bundles, build contexts, image-build records,
test and live logs, DEB packages and release staging, reports, screenshots,
status files, checksums, local publication text, virtual environments, and
caches. Transient interpreter and tool caches may exist only at another
explicitly ignored local path and are never results. The root `.gitignore` must
ignore the entire `.artifacts/` tree. Owned Podman containers, images, networks,
and volumes remain engine runtime objects; their immutable identities and labels
are recorded in that private filesystem state.

No result is copied back under `fork-maintenance/`. In particular, tracked
`evidence/`, `runs/`, `results/`, and `communications/` directories are
forbidden. Git history records inputs and reproducible automation, never run
outputs.

Private state creation fails closed on symlinks, wrong ownership, or unsafe
permissions. Upstream-test, live, and DEB jobs use unique validated `RUN` names.
The private filesystem must support `O_TMPFILE` and
`linkat(AT_EMPTY_PATH)`: immutable runner records are written and fsynced as
anonymous files, linked into place without replacement, and followed by a
directory fsync. There is no named publication temporary or weaker fallback.
Only a standalone upstream-test image build uses a unique `IMAGE_RUN`; live and
DEB image builds are embedded in and owned by their parent `RUN`. An identity is
never reused for a retry. Cleanup requires exact ownership and removes only
reviewed run-owned transient objects. Runner lifecycle and supervision create no
systemd unit and never invoke `systemctl`; `libsystemd-dev` in the upstream test
image is only an inherited source-build dependency.

The public lifecycle interface is the root `fork-maintenance/Makefile`.
Operators and agents never signal recorded process groups or call destructive
Podman commands directly for a named job. Make targets validate the owner
record, PID plus kernel start ticks and process group for host jobs, immutable
container identity and labels for container jobs, and the result boundary
before aborting or removing anything. Every new host-process owner includes a
private 256-bit token in its owner and completion records; the supervisor passes
that token through the payload environment. Complex lifecycle logic belongs in
tracked Python helpers; Make remains the public orchestration interface.
The supervisor is also held behind a private release pipe: the payload starts
only after anonymous-file publication has fsynced and no-replace-linked the
owner and the launcher writes one exact release byte. EOF or launcher death
before that byte exits without starting the payload.
Collection and acceptance require the currently tracked runner and supervisor
digests. Status, abort, and removal may use an older recorded digest only to
inspect or clean an already-owned job after automation changed; that path can
never promote new acceptance evidence.
Abort may exact-discard a running or lost uncollected job, or a completed
uncollected job only after its runner digest becomes stale. `lost` requires no
valid completion and no remaining exact owned runtime. A dead supervisor whose
recorded process group still contains a live owned member remains `running` and
is terminated as a group; it is never cleaned as lost. In that orphaned-group
path, every live same-session member must expose exactly the recorded owner
token. A missing, duplicate, or mismatched token makes ownership unprovable, so
abort fails closed and preserves all state. A legacy tokenless orphan remains
owned and cannot be signaled. Abort rejects current
completed jobs, which must be collected, and every job that already has
collected evidence. An active upstream-test prelaunch starter is refused; its
inactive exact prelaunch owner is the only authority for orphan recovery. Live
status/logs/abort route first through
`jobs/live/<RUN>.freeze-prelaunch.json`, then through the owned input-freeze
process until the main owner is published. Local DEB status/logs/abort likewise
route through its retained prelaunch record before a main owner exists; abort
publishes `deb-packages/runs/<RUN>.abort.json` before the first destructive step
and deletes it only after the exact transaction completes.

Upstream-test and live collect/abort transitions each use one retained,
mode-`0600` subsystem `.lifecycle.lock`. DEB start/collect/remove/abort
transitions use the retained `deb-packages/locks/terminal.lock`. These files
hold crash-releasing kernel locks and are not per-run results. Upstream bundle
publication uses a retained exact `.bundle.lock`; DEB source and selection
publication use retained `.source-snapshot.lock` and `.selection-cache.lock`.
Deterministic partials are recoverable only by their matching locked snapshot
path. Retained upstream `.foreground-payload.lock` and
`image-builds/.image-cache.lock` protect foreground payload publication and
mutable image-cache handoff. Each DEB builder key has its own retained
`locks/images/<distro>-<input-sha>.lock`. All workspace operations and
fingerprint publication use retained
`upstream-tests/workspaces/.lifecycle.lock`; an update acquires it before the
case-update lock.

Every collected upstream test, standalone upstream image build, live run, and
local DEB build publishes a retained removal transaction before its first
destructive step. The exact paths are `upstream-tests/logs/<name>.remove.json`,
`jobs/live/<RUN>.remove.json`, and
`deb-packages/results/<RUN>.remove.json`. Each transaction binds the old owner
and collected evidence, authorizes an exact idempotent retry after a crash, and
remains mandatory evidence until cycle cleanup.

If a live main owner is absent and its exact schema-1 removal transaction
remains, that transaction is the sole read-only authority for the removed run.
`live-status` validates it, the final log/status digests, and all still-present
bound runtime records, then reports `phase=removing` while any such record
remains or `phase=removed` otherwise. `live-logs` performs the same validation
and emits only the digest-bound final log. Schema, evidence, or runtime mismatch
fails closed; neither route falls back to pre-main freeze state or unbound
Podman inspection.

Live environment creation uses retained `venvs/.environment.lock` and exact
`venvs/.environment.partial{,.owner.json}` staging. The venv and pip children
inherit the lock file descriptor, and the next explicit `live-venv` validates
the external marker before removing only that partial. Markerless or ambiguous
state fails closed; cycle cleanup retains this shared environment state.

GitHub workflow files also invoke only that public Makefile. The dedicated
foreground develop-test and package-release CI targets are not general local
lifecycle shortcuts: GitHub Actions provides their timeout, cancellation state,
and complete console log, while tracked Python verifies each image build and
Make owns each disposable `podman run --rm` invocation.

Collected results and finalized workspaces are removed only by the
digest-confirmed cycle cleanup flow. It never stops active work and rejects
remaining owner/partial records, unsafe retained lock files, owned processes,
Podman containers or networks, incomplete evidence, changed fingerprints, and
unfinished workspace candidates. Case-creation and workspace
create/remove/fingerprint staging and case-update preparation/removal
transactions are also blockers until their explicit recovery target succeeds.
Retained valid lock files are not cleanup targets. Plan and execution take the
retained upstream-test lifecycle, upstream image-cache, live lifecycle, DEB
terminal, workspace lifecycle, and case-update locks in that fixed order.
Before its first deletion,
execution publishes `cycle-cleanups/<CYCLE>.remove.json`, binding the exact plan
and confirmation digest plus each directory target's device, inode, and
fingerprint. Such targets are atomically staged at
`cycle-cleanups/.<CYCLE>.<index>.remove`, then gain a bound schema-1
`.<CYCLE>.<index>.rmtree.json` before recursive deletion. Once that phase is
published, partial-tree recovery relies on the original device/inode and phase
binding rather than an impossible original-fingerprint match. An interruption
is resumed only with that same cycle and digest; each phase is removed after its
staging, and the outer transaction only after all exact removals complete. The
transaction root permits only one pending cleanup, so a different cycle cannot
be planned until it is finished.
Git-originated relative symlinks inside an owner-bound result tree
are fingerprinted without being followed; absolute or lexically escaping
targets are rejected. Because the result root itself is owned mode `0700`,
owned group-writable input files are fingerprinted; other-writable or
hard-linked files remain forbidden. Content-verified frozen source bundles and
archives, immutable DEB selection snapshots, input-keyed build contexts and
images, ccache, and virtual environments are reusable state and remain outside
ordinary cycle cleanup. Any upstream bundle partial, live input-freeze staging,
freeze prelaunch/abort transaction, foreground upstream payload, matching local
DEB output-validation scratch, DEB abort transaction, or DEB source/selection
partial marker or directory blocks cleanup until its exact lifecycle or snapshot
recovery path resolves it.

Hosted DEB release staging below `deb-packages/releases/` is not a local
cycle-named result and remains for operator review; cycle cleanup does not
delete it implicitly. A successful attempt retains
`releases/run-<run-id>-attempt-<n>/` with exactly the two tar assets,
`release-notes.md`, `publication.json`, and the two hidden distribution
container ownership records; interrupted staging is preserved for explicit
review.

Local `deb-remove` retains an immutable final status, matching hashed log, and
removal transaction for both successful and failed collected builds, then
removes only runtime ownership. A validated success also retains its output tar;
a failed result must retain no output. Digest-confirmed cycle cleanup removes
the status, log, and removal transaction plus the tar only when validation
succeeded, and refuses an incomplete set, orphan output, active package runtime,
changed digest, or owned package container.
Package source bundles, immutable selection snapshots, and input-keyed,
label-verified builder images are reusable state; each package result remains
bound to the actual immutable image ID it executed.

## Authority boundary

The automation may verify/fetch refs read-only, fast-forward local `master`,
perform an explicitly invoked local `develop` rebase, create and remove exact
owned isolated workspaces, recover exact marker-backed case/workspace staging
and removal or case-update transactions, remove a digest-confirmed finalized
artifact cycle, update case files from a verified workspace, apply or remove
patches in a clean non-master host worktree, run tests, and print status.

No target creates a new content commit automatically. `develop-rebase` only
replays existing fork-only commits onto fetched fork master and therefore changes
their local identities. The hosted-only `ci-master-sync` target may only perform
the exact non-forced fork-master sync defined above. The hosted-only
`ci-deb-release` target may only create its unique draft, ordinary release with
the exact Debian-version title, package tag, and two validated tar assets. If
publication fails, it may delete only the release it just created and its tag
while the tag still targets the dispatched
checkout commit, deleting and verifying the tag first and the immutable release
ID last. A retry may additionally apply that ordered rollback to the exact
draft/tag of an earlier failed attempt of that same hosted workflow run after
validating its Actions record and embedded transaction. After a successful
publication it may retain the three newest exact owned DEB releases and delete
older owned releases in tag-first/release-ID-last order; a retry may resume
that retention from an exact published release of a failed or cancelled prior
attempt. Drafts and unrelated or manual releases, tag-only state, and ambiguous
state remain untouched outside the exact recovery rules above. All other
automation never pushes, force-updates or mutates remote refs or releases,
creates or edits a pull request, changes the default branch, or changes global
Git configuration. Agents invoke neither hosted mutation target. An agent
creates a new commit only after explicit current-conversation authorization.
The operator reviews and performs all branch publication and default-branch
actions.
