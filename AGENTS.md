# Kogeler Xpra Fork Agent Guide

This repository is the user's Xpra fork. Upstream source and downstream fork
maintenance share one Git history; the tracked patch queue and its automation
live under `fork-maintenance/`.

## Sources of authority

Before changing Xpra source, read the current `CLAUDE.md`, `CONTRIBUTING.md`,
the canonical test workflow at `.github/upstream-workflows/test.yml` (verified
byte-for-byte against the workflow at the source boundary embedded in current
`develop`), and `pyproject.toml`. Before changing the fork workflow, also read
`fork-maintenance/CONTRACT.md`, the relevant runbook, and every selected
`cases/<id>/case.toml`.

Current source and maintainer feedback outrank old notes, logs, patch context,
or earlier conversations. Historical output is diagnostic context only; it is
never current acceptance evidence.

## Branch roles

- `upstream/master` is the canonical Xpra source.
- `origin/master` and local `master` are operator-maintained source refs. They
  may intentionally lag canonical master between explicit refresh cycles.
  Never commit fork-only changes on `master`, never push a patch to it, and
  never force, reset, or rewrite it.
- `develop` is the rebase-maintained fork integration branch and intended
  default branch. It carries `AGENTS.md`, the ignore and CI boundaries, and
  `fork-maintenance/`. Production changes remain stored as patches rather than
  committed copies of those patches in the Xpra source tree.
- Temporary non-master branches are supported only for exceptional clean
  host-worktree integration diagnosis and patch operations. They must descend
  from the current `develop`; the default isolated workflow remains on
  `develop` and does not support temporary branches. Do not create parallel
  worktrees.

Ordinary investigation, patch work, and testing use the source commit already
embedded in current `develop`. They never fetch, compare live master refs, or
require a rebase. Stay on `develop`; do not switch branches. Run:

```bash
make -C fork-maintenance isolated-start-check
make -C fork-maintenance workspace-create \
  CASE=<id> WORKSPACE=<unique-name> PATCH_MODE=patched
```

The isolated gate permits dirty files only at `AGENTS.md`, `.gitignore`, the
controlled `.github/` CI paths, and `fork-maintenance/`. It rejects any host
Xpra source change and copies the unique merge base already embedded in
`develop` below
ignored `.artifacts/fork-maintenance/upstream-tests/workspaces/`. Patch
application, source editing, and candidate staging occur only in that copy.
`workspace-update` atomically exports the complete candidate back to the
selected `cases/<id>/fix.patch` and derives its digest and paths. Under the
retained workspace lifecycle lock, it publishes one exact transaction below
ignored `case-updates/` before replacing the case patch/manifest and workspace
resolution/metadata together. Apply/reverse verification uses only that
transaction's temporary `candidate-lab/source`, never scratch in the finalized
workspace. The refreshed workspace remains bound to the newly published patch
and may be edited and exported again. The command must leave the host branch,
HEAD, index, and inherited Xpra source unchanged. An interrupted update is
resolved only by `case-recover CASE=<id>`: a complete transaction is finished
to its recorded new state, an incomplete preparation is removed without
changing the case, and an owner-only boundary is cleared only after validating
the currently published case and bound workspace. Never delete or hand-edit
this recovery state. Before recursively deleting a prepared or completed case
update, recovery publishes schema-1
`case-updates/<id>.update.remove.json` (`kind=case-update-rmtree-started`),
atomically renames the transaction to `case-updates/.<id>.update.remove`, and
deletes the update owner only after the tree, then the removal marker last. A
retry trusts only the bound device/inode after the rename; it does not re-hash a
partially deleted tree. The marker's canonical target array still revalidates
the published case files and any bound workspace resolution/metadata even when
the tree and update owner are already gone.

Only when the operator explicitly starts a new upstream-refresh and patch-
adaptation cycle does the clean host workflow run:

```bash
make -C fork-maintenance repo-sync
make -C fork-maintenance master-update
git switch develop
make -C fork-maintenance develop-rebase
make -C fork-maintenance patch-start-check
```

That explicit gate fetches both master refs, compares each cached ref with live
GitHub state, and requires live fork/canonical equality. If it reports a stale
fork, the operator may run:

```bash
gh repo sync kogeler/xpra --source Xpra-org/xpra --branch master
```

Never add `--force`, and agents never run this remote-mutating command. Repeat
`repo-sync` after the operator action before continuing that refresh. A
divergent or ahead fork master stops only the explicit refresh for owner
review. `master-update` may only fast-forward local `master` after that gate.
None of these commands is a prerequisite for testing or editing the current
`develop` queue.

When the operator explicitly chooses to move the embedded source base, master
history is transferred to `develop` only by rebasing `develop` onto the fetched
local `master`. Merging `master`, `upstream/master`, or an equivalent upstream
ref into `develop` is forbidden. If that rebase stops, resolve every conflict,
stage the resolutions, and continue it before accepting the refreshed queue.

Rebasing an already published `develop` rewrites its fork-only commits. Agents
still never push or force-push. The operator may publish the reviewed rewrite
only with an exact-SHA `--force-with-lease`; plain `--force` is forbidden.

## Patch queue contract

`fork-maintenance/cases/<id>/fix.patch` is the source of truth for one atomic
production behavior plus its focused tests, except for the single explicitly
typed test-quarantine duty case. `case.toml` binds the exact patch digest,
paths, dependencies, tests, and required gates. The complete active queue is
`fork-maintenance/stacks/develop.toml`.

The currently retained active cases are:

- `wayland-initial-window-state`;
- `wayland-client-keymap-sync`;
- `wayland-empty-damage-throttle`;
- `video-pipeline-cleanup-race`;
- `debian-libva-codecs-package`;
- `upstream-test-quarantine` (the single test-only duty case).

The quarantine case is not a production fix. It may change only the exact
upstream unit-test modules listed in its `[quarantine]` manifest table. Before
applying it after every fork-master rebase, run all three clean `quarantine*`
gates. If any listed module is green on the clean embedded source, remove or narrow
that entry and refresh the one quarantine patch; never carry it forward merely
because it still applies.

Do not resurrect deleted historical cases, verifications, evidence, or stacks
without an explicit new request and a current-source reassessment.

Host `patch-apply`, `stack-apply`, `patch-update`, and unapply operations are
retained for the exceptional explicit upstream-refresh/integration cycle. The
default pre-commit cycle is
`workspace-create`, `workspace-stage`, `workspace-update`, and
`workspace-remove`; it never stages or edits inherited Xpra source in
`develop`.

Never edit `patch_sha256` or `paths` manually. Never leave the applied source
copy committed on `develop`; commit the maintained patch file and automation
metadata only. A patch that is neither forward-applicable nor exactly
reverse-applicable to the embedded source is divergent and must be reworked,
not forced.

## Implementation discipline

Search current source, adjacent tests, and recent maintainer-authored history
before editing. Preserve client/server subsystem boundaries, feature toggles,
codec discovery, platform gates, and pkg-config authority. Do not add preload
tricks, import-order dependencies, polling, application-specific workarounds,
or build-only logic to the installed package. Avoid unrelated refactors and
formatting churn.

Work on one atomic behavior at a time. Preserve unrelated user changes and
remotes. Never reset, clean, or switch a non-clean checkout automatically.
Run `git diff --check` on every candidate and use the lint configuration from
the source embedded in current `develop`.

Every new source or test file introduced by a downstream patch must carry
`Copyright (C) <current-year> kogeler` using that file's native comment syntax.
Do not attribute a downstream-authored new file to an upstream maintainer.
When copied or derived content requires an existing notice to be retained, keep
that notice and add the `kogeler` line.

## CI boundary

Every canonical upstream workflow is kept as a byte-identical, non-executable
rename below `.github/upstream-workflows/`. The only executable workflows are
`.github/workflows/develop.yml`, `.github/workflows/master-sync.yml`, and
`.github/workflows/deb-packages.yml`. During every explicit upstream refresh,
preserve upstream
workflow edits through those renames, relocate any newly added upstream
workflow, and run `make -C fork-maintenance ci-layout-check`.

The executable `develop.yml` test workflow is a deliberately thin GitHub
wrapper: it triggers only for pushes to `develop`, grants read-only contents
permission, pins every action to a reviewed full commit SHA with its release
version in a comment, selects `ubuntu-26.04`, uses a six-hour job timeout, and
declares only the fixed `full`, `full-cython`, and `full-no-compat` matrix.
Checkout fetches full history without persisting credentials. Every matrix job
invokes only `make -C fork-maintenance ci-upstream-tests`, passing its fixed leg
through `XPRA_CI_TARGET`. The hosted preflight requires the checkout to remain
clean at the exact `GITHUB_SHA`. Package installation, exact frozen-source
verification, image ownership, patch application, and test implementation
belong in `fork-maintenance/`, never in YAML.

The master-sync workflow runs at minute 37 every 12 hours and may also be
dispatched manually by the operator. Its sole job has job-scoped
`contents: write`, checks out the automation from `develop` at depth one
without persisting credentials, and invokes only
`make -C fork-maintenance ci-master-sync`. That target may fast-forward only
remote fork `master` from `Xpra-org/xpra:master` through `gh repo sync` without
`--force`; it verifies exact live equality afterward and must not change,
merge, rebase, or publish `develop`. Agents never invoke this remote-mutating
target or dispatch the workflow.

The package-release workflow is manual-only and branch-agnostic. Its six-hour
job checks out full history for the operator-selected revision without
persisting credentials and invokes only
`make -C fork-maintenance ci-deb-release` on Ubuntu 26.04 with job-scoped
`contents: write`. Package source discovery never fetches or names the current
branch or a remote: it uses `HEAD` plus local or remote-tracking refs whose
final component is exactly `master`, requires one uniquely latest clean merge
base, rejects downstream merge commits, and rejects downstream committed or
dirty Xpra source. `stacks/develop` is the fixed queue slug, not a requirement
that the selected revision be on a branch named `develop`. The amd64 builds
require an x86-64 Podman host, network access, and sufficient disk space. They
build only the frozen fork source and resolve build dependencies from the
target Ubuntu or Debian archives. They do not enable the Xpra APT repository,
trust its signing key, or consume prebuilt Xpra packages. They produce unsigned
packages with `dpkg-buildpackage -us -uc`, force xz Debian members, disable
automatic dbgsym generation with
`DEB_BUILD_OPTIONS=noautodbgsym`, reject any debug-symbol package at both sides
of the container boundary, and validate each xz stream with a 256 MiB decoder
memory limit. The patched Debian packaging uses `dh_missing --fail-missing`, so
every staged build result must be assigned to one binary package or to the
small exact reviewed `not-installed` set. Before emit, the builder inventories
every actual DEB, rejects duplicate package identities and overlapping regular
payload paths, extracts the real `xpra-common` and `xpra-codecs` packages, and
imports the required libva and libyuv native modules with the distribution
Python. It also runs `dpkg-shlibdeps` over those packaged ELF objects and proves
that the resulting library dependencies are present in `xpra-codecs`. The host
independently parses every returned DEB and repeats the package-set, module
ownership, ABI, and dependency checks. They build separate Ubuntu 26.04 and
Debian 13 tar assets, then
stage a draft with `prerelease=false`, upload and verify exactly those two
assets, and publish an ordinary release whose title is exactly the Debian
version, for example `6.6-r42479-1`. Its unique transaction tag points at the
dispatched checkout commit. Publication binds the immutable GitHub release ID.
On failure, the target may roll back only the release it just created and a tag
that still points at that exact commit, deleting and verifying the tag before
deleting the immutable release ID. The input-keyed builder cache is label-verified,
every created package container executes the actual immutable builder image ID,
and every accepted package result binds it. Source and complete-queue snapshots
are immutable retained caches; the latter is stored as
`selections/<selection-sha>-<metadata-sha>/{lab,selection.json}`, and every
package owner binds its exact selection-state path and both digests. Local
package start publishes a retained prelaunch owner before its run directory and
main owner; removal publishes a retained result-bound transaction before its
first destructive step. A rerun of one hosted Actions run may recover only an
exact orphan draft from its own earlier failed attempt. Draft creation uses the
authenticated releases REST endpoint and binds the immutable release ID from
that response; draft discovery never uses the published-only tag endpoint.
Current-tag absence and orphan recovery scan a bounded paginated release list
and require one unique exact transaction. After publication, that same listing
identifies only canonical ordinary DEB releases owned by this workflow, orders
them by publication time with immutable-ID tie-breaking, retains the three
newest, and deletes every older owned release in exact tag-first,
release-ID-last order. Drafts and unrelated or manual releases are never
retention targets; malformed or ambiguous owned state fails closed. A retry of
the same hosted run may resume retention from an exact published release left
by a failed or cancelled prior attempt without publishing a duplicate. Agents
never invoke this hosted remote-publication target.

The hosted `ci-upstream-tests` path does not run `ci-layout-check`: GitHub has
already selected the executable workflow, and this publication audit must not
block the actual test matrix. Run it explicitly after an upstream refresh and
before push.

Hosted develop test CI does not chase live refs. It uses the checkout's
cached `origin/master` only to locate the merge base already embedded in the
pushed `develop`, then freezes that commit. A later `origin/master` advance must
not change the tested source. The develop test automation never fetches, syncs,
switches, merges, or rebases after `actions/checkout`; choosing whether to
refresh and rebase the source base belongs solely to the operator.

Each matrix job in the `develop.yml` test workflow applies the complete
`stacks/develop` queue and runs one upstream unit-test leg. The three test legs
run on independent hosted runners with `max-parallel: 3` and matrix fail-fast
disabled, so one failure does not cancel the other results. The master-sync and
package-release workflows have no test matrix.
CI never starts live, display-hardware, render-node, or hardware-H.264 profiles.
Those remain local physical acceptance gates.

Every Podman source/build-context transfer uses the common validated streaming
tar helper. Container-produced artifacts use the reverse stdout tar boundary
only where a caller requires them; upstream unit tests return only their normal
logs and recorded resolution digest. Bind mounts, bind-style `--mount`, and
`podman cp` are forbidden for source, patch, application-input, or artifact
transfer. The upstream-test named ccache volume is a cache-only exception, and
render nodes passed with `--device` are hardware access rather than file
transfer. Upstream-test containers wait for their streamed payload through the
pre-created validated readiness FIFO; readiness uses no process signal. The
sender makes bounded non-blocking open retries only until the FIFO reader is
attached, then writes one ready byte. Tar extraction stages only at the exact
`.<destination>.partial` sibling, refuses a pre-existing partial, and publishes
with an atomic no-replace rename. The common reader accepts only plain,
uncompressed tar and enforces raw-archive, member, content, and extended-
metadata bounds before publication. Reverse process output without a
caller-owned deterministic partial uses an anonymous `O_TMPFILE`, fsync, and
no-replace link; the common helper has no named generic fallback.

## Validation ladder

Stop at the first unexplained failure:

1. resolve or reproduce against the unmodified source commit embedded in
   current `develop`;
2. run the focused case regression;
3. run the affected native or subsystem boundary;
4. reassess every quarantined upstream module on the clean embedded source;
5. run all three Ubuntu 26.04 unit-test legs;
6. run every positive live acceptance gate required by the selected case or
   stack.

The fixed `live-xpra-hardware` gate uses `APPLICATION=hardware`,
`ENCODING=h264`, `H264_CLIENT_POLICY=adaptive-alpha`,
`ALPHA_SCENARIOS=default`, and the application-exit lifecycle. It resolves the
primary `vkcube` and auxiliary GTK Xpra window IDs independently by their exact
titles. Its first saved `window.info` is only an initial snapshot and must be
`BGRX` or `RGBX`; exact per-window frame-state logs prove that later primary
frames remain opaque. Startup layout and picture packets are structurally
validated but cannot establish acceptance. After both title-bound windows are
stable, the runner binds the active primary IDR group to its exact saved source
geometry, records an exact input interval, and closes it before the auxiliary
window exits. Within it, only positive H.264 main regions and their exact
required one-pixel lossless RGB24/RGB32 codec edges are allowed; arbitrary,
interior, larger, or alpha-bearing RGB regions fail. H.264 must predominate for
at least ten frames and one second, cover at least 99% of each production
window and 90% of all encoded pixels, and satisfy the exact VA-API
encode/decode, packet-chain, hardware-presentation, and pixel checks. Safe
startup or post-exit resize packets never contribute to those thresholds.

The auxiliary native-Wayland GTK fixture requires an RGBA visual and a
deterministic transparent border around its opaque interactive button. Its
`BGRA`/`RGBA` window must expose both transparent and opaque pixels in every
collected source screenshot for that exact window and emit only positive WebP
or alpha-bearing RGB32 packets with exact contained geometry. Client captures
prove the visible composited result and input response; they need not retain a
source alpha channel after composition. H.264, RGB24, and non-alpha RGB32 are
failures.

The fixed `live-xpra-opengl-hardware` gate uses the same adaptive-alpha,
application-exit, H.264, auxiliary-window, input, VA-API, client-presentation,
pixel, lifecycle, and cleanup contract. Its separately title-bound opaque
primary is the native-Wayland `glmark2-wayland` synthetic OpenGL `jellyfish`
benchmark instead of `vkcube`. It requests an EGL visual with no alpha channel.
Its fixed source viewport may be smaller than the tiled client backing, so the
pixel gate requires the exact logged viewport placement before comparing the
source crop. The server process must report metadata from a live OpenGL context,
use the selected render node and AMD Mesa/Radeon hardware driver rather than a
software renderer, and produce changing nonuniform client frames. This
complements the Vulkan gate; neither is a substitute for the other.

The only named positive live profiles are Zed RGB, adaptive-alpha Zed H.264,
RGB detach, RGB transport-loss fault injection, native-Wayland client-keymap
input, multi-window Vulkan hardware, and multi-window OpenGL hardware. Their
Make wrappers fix every acceptance dimension and require a nonempty reviewed
case or stack selection. The
orthogonal client-only `NETWORK_PROFILE` is loaded from
`fork-maintenance/profiles.yml`; its YAML default is used for the normal seven
gates. Static Xpra arguments come only from `fork-maintenance/live-cli.yml`.
Neither YAML value set may be duplicated in Python, Make, or unit-test
assertions. A clean-source or picture-fallback diagnostic cannot publish live
acceptance. Negative unit cases only prevent a false pass; every public live
target must finish with positive rendering, input, lifecycle, and owned-cleanup
evidence.

Tests used to accept a patch belong in the tracked case or
`fork-maintenance/infra`. Ad hoc probes can diagnose but cannot establish
acceptance. Native tests must fail rather than skip when their module is the
subject of the patch. Compare clean and patched runs in the same frozen image
before assigning an environment failure to the patch.

Jobs expected to exceed two minutes use the named lifecycle interfaces in
`fork-maintenance/Makefile`. Test jobs are detached Podman containers; standalone
upstream-test image builds, live jobs, and DEB jobs use the owned Python process
supervisor. Every test, live, or DEB run and retry uses a new `RUN`. Only a
standalone upstream-test image build and retry uses a new `IMAGE_RUN`; image
builds embedded in a live or DEB job belong to that parent `RUN`. No lifecycle
target creates a systemd unit or invokes `systemctl`.
The dedicated `ci-upstream-tests` and `ci-deb-release` targets are the hosted
exceptions: GitHub Actions owns their foreground job lifecycle and logs, while
Make/Python still owns every Podman build and run. They are not substitutes for
named local acceptance evidence.

Do not restart the functional ladder for a proven non-semantic refresh. This
exception is limited to an unchanged embedded source and an exact old/new applied diff
containing only comments, copyright notices, or documentation, with no path,
mode, executable data, configuration, test assertion, or runner behavior
change. Resolve the refreshed queue, run whitespace and fork-control checks,
and state the proof in the handoff; do not launch focused, native, full, or live
jobs. This exception never applies after `develop-rebase`: every explicit
upstream rebase requires the clean quarantine reassessment, all fork-control,
focused, native, and three full upstream legs, plus all seven positive live
profiles, even when every retained patch applies without textual changes. Any
uncertainty or semantic change uses the normal ladder.

Do not start or repeat an expensive downstream test when the observed failure
occurred in a pre-test guard and the change only removes or narrows that guard.
Prove that the failing command is now reachable with its narrow unit test and a
direct preflight reproduction. If the exact frozen fork source commit, patch and
selection digests, image inputs, entrypoint, and downstream test commands are
unchanged, running the matrix cannot validate the guard fix and is forbidden as
wasteful. Rerun heavy tests only when one of those downstream inputs or
behaviors changed.

Operators and agents manage named jobs only through `fork-maintenance/Makefile`
targets. Do not signal recorded process groups or invoke destructive Podman
commands directly for a job lifecycle. Use the exact owned abort/remove target;
if one is missing, implement and test that target before operating on runtime
state. Process ownership binds the PID, process-group ID, kernel start ticks,
supervisor digest, private 256-bit owner token, private log, and completion
record; the token is repeated in the completion and inherited by every payload
process. The supervisor cannot start that payload until the owner is durably
published and its private release pipe receives one exact byte; EOF before the
byte fails closed. Container ownership binds the immutable ID and exact labels.
Abort may discard running or lost uncollected state, or completed uncollected
state only when its recorded runner has become stale. `lost` requires no valid
completion and no remaining exact owned runtime; a dead process-group leader
with a live owned member remains running. Every such member must expose exactly
the recorded owner token; a missing, duplicate, or mismatched token fails closed
and preserves the state for review. A legacy tokenless orphan is not signaled.
A current completed job must be collected, and collected evidence uses its
exact remove target. Detached upstream tests publish an inspectable prelaunch
owner before container creation. Live start first publishes
`jobs/live/<RUN>.freeze-prelaunch.json`, then the background input-freeze owner;
local DEB start publishes `deb-packages/runs/<RUN>.prelaunch.json`. Their exact
abort paths handle inactive/orphaned staging without guessing from a name. DEB
abort publishes `deb-packages/runs/<RUN>.abort.json` before its first destructive
step, resumes that exact transaction after interruption, and deletes it last.
Before discarding a freeze-owned live input tree, `live-abort` publishes
`jobs/live/<RUN>.freeze-abort.json`, atomically stages each exact directory at
`live-results/.<RUN>.freeze-abort-{staging,result}`, and deletes the transaction
only after both are gone. A standalone upstream image build likewise publishes
`image-builds/.<IMAGE_RUN>.image-prelaunch.json`; status/abort route that exact
boundary and normal remove/abort deletes it. Upstream-test and live terminal
transitions each use one retained subsystem `.lifecycle.lock`; DEB terminal
transitions use retained `deb-packages/locks/terminal.lock`. Upstream image
creation/use/removal is serialized by retained
`upstream-tests/image-builds/.image-cache.lock`; each DEB builder-image key uses
its retained `deb-packages/locks/images/<distro>-<input-sha>.lock`. Hosted
foreground test selection uses marker-owned
`upstream-tests/.foreground-payload` under retained
`.foreground-payload.lock`. All workspace operations and fingerprint publication
use retained `upstream-tests/workspaces/.lifecycle.lock`; any workspace export
acquires it before the case-update lock. Named local DEB output
validation uses exact marker-owned `.<tar>.validate` /
`..<tar>.validate.partial` siblings; only validation, `deb-remove`, or
`deb-abort` may recover them.

Direct workspace removal publishes schema-1
`upstream-tests/workspaces/.<WORKSPACE>.remove.owner.json`, atomically renames
the target to `.<WORKSPACE>.remove`, and removes that external owner last.
Fingerprint scratch is owned externally by
`workspace-fingerprints/<WORKSPACE>.fingerprint.owner.json`; its recursive
cleanup uses `<WORKSPACE>.fingerprint.remove.json` plus no-replace staging at
`.<WORKSPACE>.fingerprint.remove`. That phase binds the owner operation ID and
digest; cleanup removes the owner after the tree and the phase marker last, so a
phase-only retry remains exact. `workspace-recover` is the only generic recovery
interface for these marker-backed states.

`live-start` holds the live lifecycle lock from before input freeze through
durable main-owner publication. Upstream `test-start` holds both lifecycle and
image-cache locks through create, start, and payload delivery. The create/start
children inherit only the lifecycle descriptor; the Python starter itself holds
the image-cache lock through immutable-ID handoff and payload delivery so
Podman's long-lived networking helper cannot retain that cache lease.

Upstream image-cache removal refuses any matching unresolved image-build or
test prelaunch/owner. Cleanup may accept an older valid source label only while
all other ownership labels, image inputs, workflow digest, and immutable image
ID still match exactly; that cleanup path cannot create acceptance evidence.

Every collected test, standalone upstream image build, live run, and DEB build
publishes an immutable removal transaction before deleting runtime ownership.
It binds the reviewed evidence and old ownership, makes interrupted removal an
exact idempotent retry, and remains with the result until digest-confirmed cycle
cleanup. Never delete such a transaction by hand.

After a live main owner is gone, its exact schema-1 removal transaction alone
authorizes read-only inspection. `live-status` validates it and reports
`phase=removing` while bound runtime remains or `phase=removed` otherwise;
`live-logs` emits only its validated digest-bound final log. This post-remove
route is separate from pre-main freeze routing, and any transaction or evidence
mismatch fails closed.

## Runtime and result boundary

All generated filesystem output—logs, reports, screenshots, source bundles,
build contexts, status files, publication drafts, caches, and virtual
environments—lives below ignored `.artifacts/fork-maintenance/` or another
explicitly ignored local path. It is never staged or committed. Owned Podman
containers, images, networks, and volumes remain engine runtime objects and are
controlled by the corresponding lifecycle and label checks.

Immutable runner records require an artifacts filesystem supporting
`O_TMPFILE` and `linkat(AT_EMPTY_PATH)`: they are fsynced as anonymous files,
linked without replacement, and followed by a directory fsync. There is no
named temporary-file fallback. The live environment uses retained
`venvs/.environment.lock` and exact marker-owned `.environment.partial` state;
only a later `live-venv` performs its locked recovery.

Use one common prefix for every named run and isolated workspace in a work
cycle. After the patch queue and validation are final and reviewed, run the
two-phase `cycle-clean-plan` / digest-confirmed `cycle-clean` workflow. It may
remove only exact owned collected results and finalized workspaces, must refuse
active runtime state or an unexported candidate, and retains shared caches,
including frozen source and DEB selection snapshots, images, ccache, and virtual
environments by default. Retained lock files are validated; source, selection,
matching DEB validation scratch, a DEB abort transaction, or live-freeze
prelaunch/abort transaction/partials block cleanup. Planning and removal acquire
the upstream lifecycle, upstream image-cache, live
lifecycle, DEB terminal, workspace lifecycle, and case-update locks in that
fixed order. Before deleting the first reviewed target, `cycle-clean` publishes
the schema-2 `cycle-cleanups/<CYCLE>.remove.json`, including the device, inode,
and fingerprint of each directory target. It atomically stages such directories
at `cycle-cleanups/.<CYCLE>.<index>.remove`, then publishes schema-1
`.<CYCLE>.<index>.rmtree.json` before recursive deletion. Once that phase exists,
a retry validates its transaction binding and the staging device/inode rather
than re-hashing the necessarily partial tree. Interruption is resumed with the
same `CYCLE` and confirmation digest, never a new plan or manual deletion. Case
creation, case-update transactions, and workspace create/remove/fingerprint
staging also block cleanup until the exact public `case-recover` or
`workspace-recover` target validates and resolves only its marker-owned state.
Cleanup is branch-agnostic and neither requires nor changes a named remote,
branch, or ref.

Do not create tracked `evidence/`, `runs/`, `results/`, or `communications/`
trees. Git history stores automation, patch inputs, tests, and contracts—not
the results of running them. Cleanup acts only on exact owned runtime objects
after review; it never deletes patches, cases, or unrelated Podman objects.

## Git and publication authority

- Do not commit unless the user explicitly asks in the current conversation.
- Never push, force-push, mutate a remote ref, or change global Git
  configuration.
- The scheduled `master-sync.yml` service identity may fast-forward only the
  existing fork `master` ref; agents never invoke or dispatch it.
- The manual `deb-packages.yml` service identity may create only its unique
  draft, ordinary release, package tag, and two validated tar assets. The
  release title is exactly its Debian version and `prerelease` is false. A
  failed attempt validates its just-created release, deletes the exact tag
  first only while it still targets the dispatched commit, verifies tag
  absence, and deletes that immutable release ID last. A retry may apply the
  same tag-first/release-last rollback to an exact draft left by an earlier
  failed attempt of the same workflow run, but only after validating that
  attempt, its transaction marker, assets, release ID, and unchanged tag
  target. After one ordinary release is verified, it may retain the three
  newest exact owned DEB releases and delete each older exact owned release in
  tag-first/release-ID-last order. An exact published release left by a failed
  or cancelled prior attempt may resume only that retention transaction;
  drafts, unrelated or manual releases, tag-only state, and ambiguous state
  are preserved.
  Agents never invoke or dispatch it.
- Never create, update, or close a pull request or change the default branch on
  the user's behalf from this workspace.
- Read-only fetch, `ls-remote`, branch/PR audit, and local fast-forward of
  `master` are allowed within the documented gates.
- The operator reviews, signs if required, pushes `develop`, and later changes
  the fork's default branch.

When handing off, show exact status, embedded-source/master/develop commits,
patch resolution, validation completed, remaining validation, and resolved
operator-only commands. Do not claim results that exist only in an old log.
