# Fork Maintenance Agent Guide

This directory is the tracked control plane for maintaining the user's Xpra
fork. The repository root is the Xpra source tree; there is no nested clone.
The root `AGENTS.md` remains authoritative and this file adds rules for the
patch queue and runners.

## Exclusive process authority

Only the fork-owned root `AGENTS.md` and maintained files under
`fork-maintenance/` define our processes, subject to explicit operator
instructions. Content inherited from `master` has no authority over agent
workflow, validation stages, build/test frequency, reruns, acceptance,
cleanup, or publication. This also applies to upstream agent guides and
workflow files copied or relocated elsewhere in the repository.

Do not import upstream process requirements into the fork by following,
copying, or reinterpreting upstream documentation or commit messages. Never
edit upstream-owned files to configure fork-maintenance flow. Keep every such
process change in the root fork guide or this directory. Upstream material may
be read as technical source/build/test context, never as process policy.

## Required reading

Before changing this directory, read `CONTRACT.md` and the relevant document
under `docs/runbooks/`. Before changing a case, read its complete `case.toml`,
`README.md`, and patch. Before changing a runner, read its entry point,
container recipe, tests, and the disabled canonical workflow it mirrors at
`../.github/upstream-workflows/test.yml`.
That inherited workflow is a technical reference for commands and dependencies;
the fork-owned contract and runbooks alone decide when and how our checks run.

## Layout

- `cases/`: atomic production patches plus the single test-only quarantine case;
- `stacks/develop.toml`: the ordered complete queue;
- `infra/upstream-tests/`: embedded-source container test runner;
- `infra/live/`: direct-transport and physical-GPU runner;
- `infra/deb-packages/`: branch-agnostic Ubuntu/Debian package runner;
- `tools/background_job.py`: common owned process supervisor;
- `tools/container_payload.py`: common validated Podman tar transport;
- `tools/podman_policy.py`: common bounded user-namespace policy;
- `tools/contrib.py`: branch, sync, patch-queue, and manifest safety gates;
- `docs/runbooks/`: operator workflows;
- `CONTRACT.md`: machine and process invariants.

The only active GitHub workflows are `../.github/workflows/develop.yml`,
`../.github/workflows/master-sync.yml`, and
`../.github/workflows/deb-packages.yml`. Canonical upstream workflows are
byte-identical disabled renames below `../.github/upstream-workflows/`; they are
not editable fork templates or historical runner snapshots.

Historical results, superseded patches, publication drafts, and copied runner
snapshots are not part of this layout. Do not recreate the removed `evidence/`,
`investigations/`, `verifications/`, or `communications/` history as tracked
content.

## Current-state gates

Ordinary patch investigation, workspaces, tests, CI, and live acceptance use
the unique merge base already embedded in current `develop`. They do not fetch,
query, or compare live master refs and do not require local `master` to be
current. `isolated-start-check` remains on `develop`, allows dirt only in the
fork control plane, rejects host Xpra source changes, and records that embedded
source commit without changing a ref or the worktree.

Only an operator decision to begin a new upstream adaptation cycle activates
`repo-sync`, `master-update`, `develop-rebase`, and `patch-start-check`.
`repo-sync` then fetches both master refs, verifies their live equality, and
may instruct the operator to perform the documented non-forced remote sync;
agents never run that mutation. Those explicit refresh commands are never a
prerequisite for examining, editing, or testing the current `develop` queue.
The single agent entry point for the canonical **Autonomous Upstream Refresh
and Full Queue Adaptation** procedure is:

```text
Execute autonomous-upstream-refresh PRIMARY_CASE=<slug> against the current fork master.
```

This is an agent directive, not a Make target. Its complete queue-wide
procedure is
[`docs/runbooks/upstream-refresh.md`](docs/runbooks/upstream-refresh.md).
`PRIMARY_CASE` selects only the first and most detailed semantic review; every
active production case, the quarantine, control plane, both package builds,
and the complete test/live ladder remain in scope. The runbook derives its own
unique cycle identifier and self-corrects any in-scope procedural or harness
defect without restarting expensive evidence whose frozen semantic inputs are
unchanged.

That explicit runbook begins by normalizing `develop`: when non-ignored
changes exist, the agent exhaustively reviews them and creates one local
preservation commit containing every legitimate tracked and untracked change
and nothing else, including reviewed legitimate work unrelated to the refresh.
The runbook invocation itself supplies authority for that one commit, so no
additional confirmation is requested. A clean checkout gets no empty commit.
No refresh result, intermediate prerequisite, or final queue
change is committed by the agent after this boundary; if later work needs a
clean host, order it before tracked result edits, use an isolated supported
path, or stop rather than manufacturing another commit.

A temporary non-master branch is supported only for exceptional clean
host-worktree integration diagnosis and patch operations after it descends from
current `develop`. The isolated workspace lifecycle requires `develop` and is
not a temporary-branch workflow.

`develop-check` validates the current embedded-base branch and requires:

- clean `develop`;
- one unique embedded source boundary against cached `origin/master`;
- no merge commits in the downstream range above that boundary;
- no committed Xpra source changes outside the patch queue representation;
- a resolvable `stacks/develop.toml`;
- an effective ignore boundary for `.artifacts/fork-maintenance/`.

Cached or live master freshness is deliberately not part of this check; the
operator owns the decision to start a separate upstream refresh.

## Developing patches

Run `isolated-start-check`, then create one atomic workspace with
`workspace-create CASE=<id> WORKSPACE=<unique-name>`. Make source and regression
changes only below that workspace, use `workspace-stage`, and export them with
`workspace-update`; it atomically derives `fix.patch`, `patch_sha256`, and
`paths` while leaving the host source and index untouched. The transaction
also replaces that workspace's selection resolution and metadata, so a
successful workspace remains current and may be edited, staged, and exported
again before removal. Forward/reverse verification runs only in the
transaction's temporary `candidate-lab/source`. Review both representations,
then remove only the exact owned workspace. The old
apply/update/unapply cycle remains available for explicit clean-host
integration work.
Both update paths publish an ignored `case-updates/<slug>.update.owner.json`
and an exact transaction before replacing tracked files. Recover an interrupted
preparation, finish a complete transaction, or clear a validated owner-only
boundary only through `case-recover CASE=<slug>`; never edit the transaction or
its retained `.lifecycle.lock` by hand. Recursive transaction cleanup first
publishes `<slug>.update.remove.json`, stages the tree at
`.<slug>.update.remove`, then deletes the update owner and finally the removal
phase; an interrupted cleanup is completed by the same recovery target.

Before export, inspect every `new file mode` entry. Each new downstream-authored
source or test file must carry `Copyright (C) <current-year> kogeler` in its
native comment syntax; never substitute an upstream maintainer's authorship.
Retain required notices on copied or derived content and add the `kogeler` line.

For integration diagnosis and acceptance, create a stack workspace or use the
embedded-source container runner. Do not export a stack as one atomic case patch.
Host `stack-apply` and `stack-unapply` remain a clean-checkout fallback only.

All known upstream-only test failures belong in
`upstream-test-quarantine`, never in a production case. A quarantine addition
requires a clean-source reproduction in every affected matrix leg. After each
explicitly selected upstream rebase, run the three clean `quarantine*` gates
before applying that case. Every gate runs the complete ordered module union:
its exact gate-specific assignment must be the ignored-failure set, while the
complement must pass without skips. A newly green assigned module makes that
leg assignment stale; remove the module and its patch path only after it has no
remaining failing-leg assignment. Resolve every stale or newly failing leg
before the patched full matrix is accepted.

When upstream absorbs a patch exactly, the resolver reports
`already-present`. Removing it from the active queue still requires a current
code review and the case's relevant tests on the embedded clean source. Since run output is
local-only, record the conclusion in the external refresh handoff; a later
operator-created commit may summarize it, but no tracked evidence archive is
created.

## Development and final acceptance

Follow [the canonical validation flow](docs/runbooks/validation.md). Develop,
review and adapt cases with the nearest real regression after each atomic edit,
affected existing upstream and downstream modules, risk-directed native,
compiled and no-compat checks, and early relevant live acceptance. Do not put
the full upstream matrix before the live boundary under development.

Keep full-suite, package and complete-profile obligations separate from that
iteration loop. Review and freeze source, tests, fixtures/oracles and build
inputs before filling the final acceptance ledger's missing/invalidated gates.
Valid named development results can satisfy final requirements on the same
inputs. A rebase requires complete acceptance on the new base, not a complete
rerun after every case adaptation. A final failure returns its owning boundary
to development before affected expensive gates restart.

Flow improvements stay in fork-owned files and use narrow infrastructure tests
first. Preserve pending patch work and named evidence; finish collection/removal
before changing a shared runner or another bound input. Cache reuse follows
actual verified input keys, never renamed labels or an assumption that focused
tests avoid building Xpra.

## Runners and artifacts

Local acceptance runners and hosted develop test CI freeze the unique source
merge base already embedded in their `develop` checkout. Cached
`origin/master` is only a local history anchor for locating that commit; its
freshness and equality with upstream are irrelevant to the run. Neither path
fetches, syncs, switches, merges, or rebases. Both apply the selected queue in
an isolated context and never package the develop working tree, `.git`,
credentials, ignored files, or Git configuration.

The fixed multi-window hardware gate is `APPLICATION=hardware`,
`ENCODING=h264`, `H264_CLIENT_POLICY=adaptive-alpha`,
`ALPHA_SCENARIOS=default`, with application-exit lifecycle. Resolve the exact
title-bound `vkcube` and GTK Xpra window IDs separately. The primary's first
saved `window.info` is only an initial `BGRX`/`RGBX` snapshot; exact frame-state
logs prove its dynamic opaque state. Startup layout packets remain reviewed
input but cannot establish acceptance. With both title-bound windows stable,
the runner binds the active IDR group to its exact saved source geometry and an
exact input interval ending before the auxiliary exits. Only positive H.264
main regions plus exact one-pixel lossless RGB24/RGB32 codec edges are
production. Every observed crop signature must gain one complete required edge
set, but an unchanged edge need not be resent with every H.264 frame. H.264
must dominate that interval and prove stable VA-API encode/decode and hardware
presentation. Safe startup and post-exit resize packets remain validated but
do not dilute or satisfy the production gate. Every collected source screenshot
for the deterministic native-Wayland GTK auxiliary's exact window must expose
transparent and opaque pixels. The window must remain `BGRA`/`RGBA` and use
only positive WebP or alpha-bearing RGB32 packets with exact contained
geometry; H.264, RGB24, and non-alpha RGB32 fail acceptance. Its client
captures prove visible composition and input response rather than preservation
of source alpha.

The fixed `APPLICATION=opengl` multi-window gate reuses this complete H.264,
auxiliary, input, lifecycle, and cleanup contract. Its primary is instead the
native-Wayland `glmark2-wayland` synthetic OpenGL `jellyfish` benchmark with a
no-alpha EGL visual. Its fixed source viewport may be smaller than the tiled
client backing; exact logged placement binds the source crop used by the pixel
gate. The server application must expose a live OpenGL context, selected
render-node descriptor, AMD Mesa/Radeon mapping, non-software renderer metadata,
and changing nonuniform forwarded frames. The Vulkan and OpenGL primary gates
are independent positive proofs.

The exact complete-stack live acceptance set is Zed RGB, adaptive-alpha Zed
H.264, RGB detach, RGB transport-loss fault injection, native-Wayland
client-keymap input, multi-window Vulkan hardware, and multi-window OpenGL
hardware. The separate positive `live-x11-clipboard` gate is owned only by
`x11-client-clipboard-events`: it selects that case at both endpoints and is
not added to the seven-profile stack set. Its native-Wayland reverse source is
armed by a private command, claims only inside a real F8 callback delivered
through Xpra, and must receive a compositor owner-change confirmation. The
same root XFixes monitor covers both forward updates and that reverse boundary,
with three production owner events for `both` and exactly two for `to-server`
and `off`. It remains active through controlled Xpra client exit and an X11
queue drain; only an exact shutdown-only zero-owner event may be classified
separately. Retained compositor source intervals and cross-stream fixture
chronology must be reparsed at collection, and a late nonzero takeover fails.
Their fixed Make wrappers require the
exact nonempty reviewed selection allowed by each profile. `profiles.yml` alone
supplies the selectable client network/quality overlay and its default;
`live-cli.yml` alone supplies static server/client Xpra arguments. Do not
duplicate their concrete values in Python, Make, or unit-test assertions.
Clean-source and picture-fallback diagnostics cannot publish acceptance; every
public target is a positive Xpra behavior proof rather than an expected-failure
result.

DEB builds are a separate branch-agnostic source path. They use `HEAD` and
enumerate refs whose final component is `master`, require one uniquely latest
clean merge base, and never fetch, sync, or require a current branch name or
remote name. Downstream merge commits and committed or dirty Xpra source after
that boundary are forbidden. The selected master ref is recorded as frozen
provenance only. The clean boundary receives the complete queue inside the
package container; `stacks/develop` is the queue slug, not a checked-out branch
requirement. Builds are unsigned, force xz Debian members, and validate each xz
stream with a 256 MiB decoder memory limit. Builder images are cached by their
complete input digest and verified labels; each package container executes the
immutable image ID, and each accepted package result binds it. The immutable queue cache
is `selections/<selection-sha>-<metadata-sha>/{lab,selection.json}`; a package
owner binds the exact selection-state path, semantic queue digest, and metadata
digest before its worker starts. It also publishes a retained prelaunch owner
before creating the local `RUN`. `deb-abort` publishes
`deb-packages/runs/<RUN>.abort.json` before its first destructive step and
deletes that transaction only after an exact retry completes; `deb-remove`
publishes an immutable removal transaction before changing runtime or final
evidence. Per-image-key locks are retained below
`deb-packages/locks/images/`; Podman build children inherit them through
immutable-ID handoff. Deterministic output-validation scratch is marker-bound
to the exact tar device/inode/size. Named local scratch may be recovered only
by validation, `deb-remove`, or `deb-abort`; hosted scratch remains in its
release-attempt staging for review.

The Debian package sequence uses `dh_missing --fail-missing`: every staged
result must be assigned to one binary package or match the exact reviewed
`packaging/debian/xpra/not-installed` set. Validation is package-set based, not
a source-manifest text check. The builder inventories every actual DEB, rejects
duplicate package identities and overlapping regular payload paths, resolves
the five required native modules (libva encoder/decoder, libyuv converter and
JPH encoder/decoder) to exactly one matching amd64 CPython ABI in ordinary
`xpra-codecs`, then imports them from an extracted private package root. The
JPH pair must complete a deterministic 32x32 quality-100 lossless RGB roundtrip;
compare all RGB channels using the decoded stride, ignoring BGRX padding rather
than claiming alpha preservation. `dpkg-shlibdeps` over all five packaged ELF
objects must produce dependencies represented by the final `xpra-codecs`
control data; do not guess distribution-specific OpenJPH library names.
The host independently parses the returned ar, control, and data archives and
repeats payload ownership, filename ABI, and declared dependency-name checks.
Native imports, roundtrip execution and ELF dependency resolution remain
container-side checks for the supported Ubuntu 26.04 and Debian 13 builds.
Do not replace this with a test for selected `.files` lines or trust the emitted manifest as package
content authority.

The builder resolves build dependencies only from the configured target
Ubuntu or Debian archives. It does not enable the Xpra APT repository from the
source tree, trust an Xpra repository signing key, or install prebuilt Xpra
packages; the only Xpra program source is the frozen fork source payload.

Hosted package publication creates its draft through authenticated REST and
records the immutable release ID directly from that response. It never tries to
discover a draft through the published-only tag endpoint. Current-tag absence,
prior-attempt recovery, and malformed-create recovery use a bounded paginated
release listing and require one unique exact transaction; ambiguous state fails
closed. After publication, the same listing selects only canonical ordinary DEB
releases owned by this workflow, orders them by publication time and immutable
ID, keeps the three newest, and removes every older owned release in exact
tag-first and recorded-release-ID-last order. Drafts and unrelated or manual
releases are not retention targets. A retry may resume retention from an exact
published release left by a failed or cancelled earlier attempt of the same
hosted run without publishing a duplicate.

Durable runtime, build, result, publication, and cache state is rooted at
repository-level `.artifacts/fork-maintenance/`. It is private, no-clobber, and
ignored; transient interpreter/tool caches may use another explicitly ignored
local path. Long container tests use detached Podman containers; standalone
upstream-test image builds, live runs, and DEB runs use the owned Python process
supervisor. A test, live, or DEB attempt owns a unique `RUN`; only a standalone
upstream-test image build owns an `IMAGE_RUN`. Images built within live and DEB
jobs belong to their parent `RUN`. The lifecycle creates no systemd unit and
never invokes `systemctl`. Collection validates a real completion record,
complete log hash, runner provenance, and exact owned-object cleanup. Reports
and status files remain local even when they are final. Podman objects remain
outside the filesystem root but are bound by immutable IDs and exact ownership
labels.
DEB removal finalizes an immutable status and matching log for both successful
and failed builds; only a validated successful build also retains its output
tar. Test, standalone-image, live, and DEB remove transactions remain with
those collected results until cycle cleanup.

Every explicit allocating rootless namespace is bounded: `keep-id`, `nomap`,
and `auto` require a positive `size`, the reviewed live/upstream-test span is
2048 IDs, and `--userns=host` is forbidden. Do not alter host subordinate-ID
ranges to work around exhaustion. Common command validation must reject an
unbounded namespace before Podman runs.

Immutable runner-record publication requires filesystem `O_TMPFILE` plus
`linkat(AT_EMPTY_PATH)` and has no named temporary fallback. The live analysis
environment is separately serialized by retained `venvs/.environment.lock`;
only `live-venv` may validate and recover its exact marker-owned
`.environment.partial` state.

Give every run and workspace in one work cycle a common lowercase prefix.
After the patch and validation are final and reviewed, use `cycle-clean-plan`
and pass its exact digest to `cycle-clean`. The planner must reject active or
unremoved runtime state, incomplete evidence, and any workspace candidate not
represented by the current queue. Ordinary cycle cleanup retains shared
content-verified frozen source bundles and archives, immutable DEB selection
snapshots, input-keyed build contexts and images, ccache, and virtual
environments. Retained source/cache/lifecycle lock files are validated; any
source, selection, matching DEB validation, DEB abort transaction, or
live-freeze prelaunch/abort transaction/partial blocks cleanup. Case
creation/update and workspace create/remove/fingerprint staging also block it
until `case-recover` or `workspace-recover` validates and resolves only that
exact marker-backed state. Plan and execution acquire the retained
upstream-test lifecycle, upstream image-cache, live lifecycle, DEB terminal,
workspace lifecycle, and case-update locks in that fixed order. Before its
first deletion, execution publishes
the schema-2 `cycle-cleanups/<CYCLE>.remove.json`, binding each directory's
device, inode, and fingerprint. Directory targets are atomically staged at
`cycle-cleanups/.<CYCLE>.<index>.remove`; schema-1
`.<CYCLE>.<index>.rmtree.json` authorizes an exact device/inode-bound retry after
recursive deletion has changed that staged tree. An interruption is resumed
only with the same cycle and confirmation digest. Cleanup is branch-agnostic
and does not require or mutate a named remote or ref.

Keep direct Xpra behavior separate from SSH or parent-product orchestration.
The live runner owns direct-TCP detach, abrupt transport loss, RGB, adaptive
Wayland H.264, multi-window hardware, and the case-only X11-to-native-Wayland
clipboard gate. Do not replace these with foreground one-off commands when
deciding whether a patch is ready.

Invoke job lifecycle operations only through the root Makefile targets. Never
signal recorded process groups or call destructive Podman commands directly for
a named job; `test-abort`, `test-remove`, and the corresponding live, image, and
package targets must own the exact process, container, and record transition.
If a required transition has no target, add and test it before acting.
Abort is limited to running or lost uncollected state or completed uncollected
state whose recorded runner has become stale. `lost` requires no valid
completion and no remaining exact owned runtime; a dead process-group leader
with a live owned member remains running. Host process owners also bind a
private 256-bit token in the owner/completion records and every inherited
payload environment. If the leader is gone, an extant member with a missing,
duplicate, or mismatched token fails closed and preserves state; a legacy
tokenless orphan is not signaled. A current completed job must be collected;
collected evidence uses the matching remove target. Detached tests publish an
exact prelaunch owner before `podman create`; active starters are refused and
inactive orphaned prelaunch state is reclaimed only through `test-abort`. Live
start first publishes `jobs/live/<RUN>.freeze-prelaunch.json`, then its owned
input-freeze process. Before discarding its input directories, `live-abort`
publishes `jobs/live/<RUN>.freeze-abort.json` and atomically moves them to exact
`live-results/.<RUN>.freeze-abort-{staging,result}` siblings; retry completes
that transaction. Local DEB start publishes
`deb-packages/runs/<RUN>.prelaunch.json`; `deb-abort` publishes
`deb-packages/runs/<RUN>.abort.json` before discarding owned state and removes
that marker only after an exact retry completes.

A standalone image start first publishes
`image-builds/.<IMAGE_RUN>.image-prelaunch.json`; image status/abort handle that
boundary and normal remove/abort deletes it. Upstream-test and live terminal
transitions each use one retained subsystem `.lifecycle.lock`; DEB
start/collect/remove/abort uses retained `deb-packages/locks/terminal.lock`.
All workspace operations and fingerprint publication use retained
`upstream-tests/workspaces/.lifecycle.lock`; workspace update acquires it before
`case-updates/.lifecycle.lock`. Retained upstream foreground payload
and image-cache locks serialize deterministic staging and mutable-tag handoff;
retained DEB per-key image locks provide the same handoff guarantee. Their
children inherit the open kernel lock. Live start holds its lifecycle lock from
before freeze through main-owner publication; upstream test start holds both
lifecycle and image-cache locks through create, start, and payload delivery.
Create/start inherit only the lifecycle descriptor; the Python starter keeps
the image-cache descriptor through payload delivery without leaking it to
Podman's long-lived helpers. Selection and payload streaming inherit the
lifecycle descriptor. Each collected test, standalone-image, live,
or DEB remove target publishes its evidence-bound transaction before the first
destructive step and reuses it for an idempotent retry after interruption.
After a live main owner is gone, its exact schema-1 removal transaction alone
authorizes read-only inspection: `live-status` validates it and reports
`phase=removing` while bound runtime remains or `phase=removed` otherwise, and
`live-logs` returns only its validated digest-bound final log. Pre-main freeze
routing remains separate, and any transaction or evidence mismatch fails closed.

Workspace removal uses external
`upstream-tests/workspaces/.<WORKSPACE>.remove.owner.json` and no-replace staging
at `.<WORKSPACE>.remove`. Fingerprint scratch uses external schema-2
`workspace-fingerprints/<WORKSPACE>.fingerprint.owner.json`; its recursive
cleanup publishes `<WORKSPACE>.fingerprint.remove.json` and stages at
`.<WORKSPACE>.fingerprint.remove`. After staging, exact device/inode identity—not
a new hash of a partially deleted tree—authorizes retry. The fingerprint phase
binds its owner operation ID and digest, removes that owner after the tree, and
is itself removed last. Use only `workspace-remove` or `workspace-recover` to
finish these transitions.

GitHub CI is a thin caller, not a second automation implementation. Its YAML may
select the `develop` push trigger, Ubuntu 26.04 runner, minimal permissions,
six-hour timeout, a full-SHA-pinned full-history checkout without persisted
credentials, a clean worktree at the exact hosted `GITHUB_SHA`, and the exact fixed matrix of
`full`, `full-cython`, and `full-no-compat`. Each matrix job passes its value as
`XPRA_CI_TARGET`; its only command is
`make -C fork-maintenance ci-upstream-tests`. All source freezing, image
building, patch application, target validation, and test implementation stay
behind that Make target. Do not put apt, Podman, Python, retries, skips, dynamic
test discovery, or test commands into the workflow. Matrix fail-fast remains
disabled and `max-parallel` remains three so all three independent results are
collected concurrently when hosted runners are available. CI never invokes
`live-*` targets.

The separate thin `master-sync.yml` workflow may declare only its 12-hour
schedule, operator `workflow_dispatch`, Ubuntu 26.04 runner, ten-minute timeout,
full-SHA-pinned depth-one checkout of `develop` without persisted credentials,
and job-scoped `contents: write`. Its only command is
`make -C fork-maintenance ci-master-sync`. The target rejects local,
push-triggered, wrong-ref, wrong-repository, or wrong-workflow execution and may
only perform an exact non-forced fast-forward of fork `master`; it never updates
local refs or rebases, merges, commits, signs, or publishes `develop`.

The thin `deb-packages.yml` workflow is manual-only, checks out the dispatched
branch or tag revision without hard-coding its name, pins checkout to the
reviewed full SHA with full history and no persisted credentials, uses a
six-hour timeout, and invokes only
`make -C fork-maintenance ci-deb-release`. Its job-scoped `contents: write`
permission may stage one draft with `prerelease=false`, upload and verify the
validated Ubuntu 26.04 and Debian 13 tar assets, publish an ordinary release
whose title is exactly the Debian version, publish its unique transaction tag
at the checkout SHA, and bind the immutable release ID. Automatic dbgsym
generation is disabled and either side of the container boundary rejects a
debug-symbol package. Failed publication may delete only that just-created
release and its exact tag while the tag still points at the checkout SHA: it
deletes and verifies the tag first, then deletes and verifies the immutable
release ID last. All source selection, Podman builds, packaging, validation,
and publication preflight remain behind Make/Python. A rerun may apply the same
tag-first/release-last rollback only to an exact orphan draft from an earlier
failed attempt of the same hosted run. After successful publication it keeps
the three newest canonical owned DEB releases and removes older owned releases
in the same tag-first/release-ID-last order. A rerun may instead resume that
retention from an exact published release left by a failed or cancelled prior
attempt; unrelated or manual releases, drafts outside exact orphan recovery,
tag-only state, and ambiguous state remain untouched. Agents never invoke or
dispatch it.

Every container build context, source tree, patch selection, application input,
and returned artifact uses `tools/container_payload.py` over stdin/stdout. Do
not use bind mounts, bind-style `--mount`, or `podman cp` for transfer. The
upstream-test ccache named volume is cache-only; render-node `--device` access
is not a transfer channel. Upstream unit tests return no artifact archive: the
normal container log carries their resolution digest and test output.
The common extractor accepts only plain, uncompressed tar and separately bounds
raw archive bytes, members, expanded content, and extended metadata.
Reverse output without an exact caller-owned partial is staged with anonymous
`O_TMPFILE` publication and has no named generic fallback.
Their container entry process waits on the pre-created validated payload-ready
FIFO and begins only after extraction writes its ready byte. The sender makes
bounded non-blocking open retries until the reader is attached; do not replace
this handshake with process signals. Extraction uses only the deterministic
`.<destination>.partial` sibling, rejects a pre-existing partial, and publishes
by atomic no-replace rename.

Do not invoke `ci-layout-check` from the hosted develop test entry point. It is
an explicit publication audit; GitHub workflow selection must not become
a self-referential prerequisite for running the upstream tests.

The hosted test and package targets may use foreground containers because the
GitHub job owns the outer lifecycle and log. Local test, live, and package work
continues to use the named Make lifecycles; a CI run does not replace physical
live gates or their local evidence.

## Change boundary

Use `apply_patch` for edits. Preserve upstream style in Python, shell,
Containerfiles, TOML, and Make. Update tests whenever path, manifest, lifecycle,
or safety behavior changes. Run the narrow unit tests after each atomic change;
run the full offline `make -C fork-maintenance check` on the stable control-plane
candidate before final acceptance, not after each documentation or two-line edit.

A refresh proven by exact applied-tree comparison to change only comments,
copyright notices, or documentation does not rerun Xpra focused, native, full,
or live jobs. The embedded source, paths, modes, executable data, configuration, test
assertions, and runner behavior must all be unchanged. Run resolution,
whitespace, and fork-control checks and report the non-semantic proof. Any
uncertainty falls back to the affected development checks and final gates. This exception is only
for a patch/documentation refresh on an unchanged embedded source. It never
applies after `develop-rebase`: a changed base requires every clean quarantine,
fork-control, tests-only clean control or documented no-test semantic
substitute, patched focused/native gate, durable package boundary on the
resulting stack, full-matrix leg, every production case's declared live gate
with its atomic case selection, and all seven positive live gates with the
complete stack selection even if the patch bytes did not need modification.
That is the final new-base acceptance set, not a per-edit development sequence.

When a run fails before entering an expensive test target, validate a fix to
that pre-test guard with the narrow control-plane unit test and direct preflight
command. Do not launch the downstream matrix merely to test removal of the
guard when its exact frozen fork source commit, selection and patch digests,
image inputs, entrypoint, and test commands are unchanged. This is a
proportional-validation rule, not permission to skip tests whose inputs or
execution behavior changed.

No target in this directory commits, pushes the checked-out branch, creates a
pull request, or changes the default branch. The hosted-only
`ci-master-sync` target may fast-forward fork `master`; `ci-deb-release` may
create its unique draft, ordinary release with the exact version title, tag,
and two assets and may perform only its exact just-created
tag-first/release-last rollback while the tag still targets the dispatched
checkout. A retry may also apply that ordered rollback only to the exact
draft/tag left by an earlier failed attempt of the same hosted workflow run
after validating its Actions attempt and embedded transaction; published,
tag-only, or ambiguous state is otherwise preserved. On successful package
publication it may additionally keep the three newest canonical owned DEB
releases and delete older owned releases in exact tag-first/release-ID-last
order; an exact published release from a failed or cancelled earlier attempt
may resume only this retention. Agents invoke neither target.

The only direct agent commit implied by a runbook is the canonical
upstream-refresh preservation commit before fetch/rebase when the checkout is
dirty. It is exhaustively reviewed, contains all and only legitimate
non-ignored pre-existing changes, including legitimate unrelated user work,
and is created without a second confirmation.
After it—or immediately when the checkout began clean—the agent leaves all
refresh results uncommitted. Rebase replay is not a new direct content commit.
