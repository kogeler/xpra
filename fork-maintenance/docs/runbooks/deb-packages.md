# Build And Publish Downstream DEB Packages

## Source boundary

The package automation is branch-agnostic. It uses the checked-out `HEAD`
without requiring a current branch name. The only significant ref basename is
`master`: the source gate enumerates local and remote-tracking refs whose
final component is exactly `master`, computes their merge bases with `HEAD`,
and requires one uniquely latest common commit. That commit is the clean source
boundary before downstream fork-control commits.

The gate never fetches, syncs, switches, merges, or rebases. It neither
requires nor hard-codes a remote name. The selected exact master ref and its
commit are recorded only as immutable snapshot provenance. The range after the
clean boundary must contain no merge commit, and every downstream commit may
touch only the allowed fork-control paths; a committed or dirty Xpra source copy
fails closed. Detached `HEAD` is supported.

The internal source-snapshot step used by `deb-start` and `ci-deb-release`
creates
`deb-packages/sources/<checkout-sha>-<snapshot-sha>/{source.bundle,source.json}`.
The private Git bundle contains the selected master ref; `source.json` binds the
checkout commit, clean boundary, selected ref and tip, workflow digest, bundle
path, and snapshot digest. The container checks out the recorded clean boundary,
verifies the exact frozen source test-workflow digest, and applies the complete
`stacks/develop` queue from a separately frozen selection snapshot. The host
branch, `HEAD`, index, source tree, and refs remain unchanged.

The immutable queue cache is
`deb-packages/selections/<selection-sha>-<metadata-sha>/{lab,selection.json}`.
`selection-sha` is the semantic complete-queue digest; `metadata-sha` is the
SHA-256 of `selection.json`, which also binds the exact private `lab/` tree
digest. Every build owner and successful manifest records both digests plus the
absolute `selection_state` (`selection.json`) and `selection_snapshot` (`lab/`)
paths. Reuse requires revalidating the exact entry set, modes, tree digest, and
semantic queue digest. Inventory and lifecycle recovery must also survive a
later manifest-parser change: every historical cache still has its complete
private path, metadata digest, and tree digest revalidated, but only a cache
whose recorded selection digest equals the current queue digest is replayed
through the current semantic parser and eligible for reuse. A historical cache
which uses older manifest vocabulary is retained and remains ineligible; never
delete or rewrite it to make a new package run start. Persisted prelaunch,
owner, abort, and removal records use the same structural validation so their
exact cleanup remains possible, while every new build, payload transfer, and
collection replays current semantics before acceptance.

`develop` in `stacks/develop` is the stable queue slug. It does not require the
checked-out revision to be on a branch of that name and does not weaken the
branch-agnostic source gate.

## Prerequisites

The builder produces amd64 packages and requires an x86-64 host capable of
running `linux/amd64` Podman containers. The host also needs Git, GNU Make,
Python 3.11 or newer with `lzma`, Podman, network access to Docker Hub, Ubuntu
or Debian APT mirrors, and PyPI, plus sufficient free disk space for
two builder images, dependency caches, build trees, and output tars. Debian
package tooling, including `dpkg-deb`, is installed and used only inside the
builder container. GitHub's Ubuntu 26.04 hosted runner is the supported
publication environment; verify the same host prerequisites before a local
build.

The builder deliberately does not enable the Xpra APT repository from the
source tree. Build dependencies come only from the configured archive for the
target Ubuntu or Debian release; the source payload is the frozen fork source,
not a prebuilt Xpra package. Cython is upgraded from PyPI because the source
build requires a newer compiler than the target distributions currently ship.

## Container transport

All package input crosses stdin as the common validated tar format. This
includes the Git bundle and selected queue. The Podman build context uses the
same transport. No host source, queue, or output path is exposed through a
bind mount, `--mount`, or `podman cp`.

The transport accepts only plain, uncompressed tar and separately bounds raw
archive bytes, member count, expanded content, and PAX/GNU extended metadata.
It rejects sparse entries, transparent compression, concatenated streams, and
trailing bytes. A returned package tar must be smaller than 2 GiB, and each
extended-metadata record is limited to 1 MiB before allocation.

The builder writes progress only to stderr and emits exactly one deterministic
tar on stdout. The host publishes that stream atomically only after the
container exits successfully, then validates its provenance, package set,
sizes, and checksums. Each tar contains:

- every generated non-debug Xpra `.deb` file (never dbgsym packages or
  container-only dependency shims);
- `manifest.json`;
- `SHA256SUMS`.

The builder sets `DEB_BUILD_OPTIONS=parallel=<n> noautodbgsym`, the supported
debhelper switch that prevents automatic debug-symbol packages. The container
also refuses any unexpected `*-dbgsym_*.deb`, and the independent host
validator rejects both a dbgsym filename and a control `Package:` ending in
`-dbgsym`. Therefore a debug-symbol package cannot enter `manifest.json`,
`SHA256SUMS`, either tar asset, or the release.

The build forces `DPKG_DEB_COMPRESSOR_TYPE=xz` at level 6. The host validator
parses each Debian ar archive, requires its exact `control.tar.xz` and
`data.tar.xz` members, streams the data archive through Python's xz reader, and
rejects an unexpected, duplicate, truncated, concatenated, trailing, or
unreadable member before the outer tar is accepted. Control/data archive size,
member count, member size, expanded content, and raw compressed bytes are
bounded; each xz decoder uses a 256 MiB memory limit.

## Complete package validation

The downstream Debian patch runs `dh_missing --fail-missing`. This turns the
whole `debian/tmp` install tree into a packaging boundary: every result must be
owned by one binary package or listed in the exact reviewed
`packaging/debian/xpra/not-installed` file. The exclusions are limited to the
generic systemd units replaced by Debian's package-specific units, the encoder
service units that have no Debian lifecycle integration, and the Wireshark
dissector that cannot be installed correctly without the optional Wireshark
build environment. Codec directories, Python package metadata, and server
helpers are assigned to packages rather than excluded.

The builder does not infer success from source manifest text. Before emit it
inspects the control and data archives of every generated DEB and requires:

- one unique control `Package` identity for every archive;
- no regular payload path owned by two packages;
- one ABI-compatible native module each for `xpra.codecs.libva.encoder`,
  `xpra.codecs.libva.decoder`, and `xpra.codecs.libyuv.converter`;
- all three modules in ordinary `xpra-codecs`, never an AMD, NVIDIA, or extras
  package;
- final `xpra-codecs` dependencies that include `libva-drm2`, `libva2`, and
  `libyuv0`, with no dependency on a vendor-specific Xpra codec package.

It then extracts the actual `xpra-common` and `xpra-codecs` DEBs into a private
root and imports all three modules with the distribution's `/usr/bin/python3`.
The loaded paths must be the files inventoried from those DEBs. Finally,
`dpkg-shlibdeps` runs on the packaged ELF objects and every resolved library
dependency must occur in the final control `Depends` field.

After the package tar crosses stdout, the host independently parses every
Debian ar/control/data archive and repeats the complete package inventory,
payload ownership, module ABI, and dependency checks. It does not trust either
the source `.files` manifests or the builder-generated `manifest.json` as proof
of installed capability.

## Versioning

The package source version comes from `xpra/__init__.py`. The sequential
revision remains compatible with upstream packaging: first-parent commit count
at the clean source boundary plus `5014`. The first Debian changelog entry is
therefore rewritten from `<base>-1` to `<base>-r<revision>-1`, where `<base>` is
the current source version. The generated `xpra/src_info.py` records
`BRANCH = 'HEAD'`, the clean commit marker, one local modification layer, and
that revision. This generated build metadata is container-local; no extra
downstream source patch is required.
The build also installs the two dependency shims supplied by upstream under
`packaging/debian/` before resolving `Build-Depends`, matching upstream's
`build.sh` sequence on amd64. It deliberately invokes
`dpkg-buildpackage -us -uc -b`, so the binary package build is unsigned.
`SHA256SUMS` records the exact package bytes within the release tar but is not a
Debian package signature; the Xpra repository key used during dependency
installation does not sign these downstream packages.

## Local lifecycle

Use a fresh `RUN` for every attempt. The build, dependency installation, and
Podman image creation are owned by the background process lifecycle. DEB builds
do not use `IMAGE_RUN`: an embedded image build belongs to this parent `RUN`.
The builder image cache is keyed by the distribution, resolved base-image ID,
and complete builder-input digest, and labels verify that key. Each exact mutable
tag is serialized by retained
`deb-packages/locks/images/<distro>-<input-sha>.lock`; the Podman build child
inherits the open lock through immutable-ID handoff. For a local
`deb-start`, Make completes and validates the reusable source snapshot first;
the package start then freezes or validates the immutable selection cache before
publishing `runs/<RUN>.prelaunch.json`. That record binds all frozen source,
selection, output, container, and runner arguments before `runs/<RUN>/` or the
main background owner exists. `deb-status` and `deb-logs` expose this boundary;
before changing an ownerless prelaunch or owned run, `deb-abort` publishes
`deb-packages/runs/<RUN>.abort.json`. Status reports that exact aborting phase;
a retry validates and completes only that transaction, then deletes the abort
marker last. The main owner repeats the same arguments. Its process
owner/completion and every payload process also bind the same private 256-bit
owner token; a live member without exactly that token makes orphaned-group
cleanup fail closed, and a legacy tokenless orphan is not signaled. For hosted
`ci-deb-release`, the GitHub preflight runs first, then the selection cache is
frozen before the source snapshot; both are complete before either distribution
build or release staging directory is created. Once a
package container is created, its container record and invocation bind the
actual immutable builder image ID; a validated manifest and accepted final
status repeat that ID. An earlier failure retains its exact status/log boundary
but has no accepted image provenance:

```bash
make -C fork-maintenance deb-start \
  DISTRO=ubuntu-26.04 RUN=packages-ubuntu-01
make -C fork-maintenance deb-status RUN=packages-ubuntu-01
make -C fork-maintenance deb-logs RUN=packages-ubuntu-01
make -C fork-maintenance deb-wait RUN=packages-ubuntu-01
make -C fork-maintenance deb-remove RUN=packages-ubuntu-01

make -C fork-maintenance deb-start \
  DISTRO=debian-13 RUN=packages-debian-01
make -C fork-maintenance deb-wait RUN=packages-debian-01
make -C fork-maintenance deb-remove RUN=packages-debian-01
```

`deb-wait` waits and collects the completed job. `deb-collect` is the explicit
alternative for a job already shown as completed. `deb-remove` publishes an
immutable `deb-packages/results/<RUN>.remove.json` before the first destructive
step. That transaction embeds the original owner and status and binds the
prelaunch, runtime directory identity, output, final status, and log digests.
The same target then publishes immutable final `.status.json` and matching
hashed `.log` beside it and removes only runtime ownership. All three files are
retained for both successful and failed collected builds. A validated success
also retains its tar below `deb-packages/outputs/`; a failed result retains no
package output. If removal is interrupted, reinvoke `deb-remove`; it validates
the retained transaction and finishes only that exact deletion. Use abort for a
running or lost uncollected job, or to exact-discard a completed uncollected job
only when its recorded runner is stale. A current completed job must be
collected. After removal, `deb-status` reports the validated transaction's
`removed` phase and `deb-logs` reads its retained final log:

```bash
make -C fork-maintenance deb-abort RUN=packages-ubuntu-01
```

After review, the normal digest-confirmed `cycle-clean-plan` / `cycle-clean`
flow removes the finalized status, log, and removal transaction, plus the tar
only for a validated success. Source bundles, immutable selection snapshots,
and input-keyed, label-verified builder images remain reusable caches.

Source and selection publication use retained mode-`0600`
`.source-snapshot.lock` and `.selection-cache.lock` files. Their deterministic
partial directories have external owner markers and are reclaimed only under
the matching kernel lock by the next exact snapshot operation. Package
start/collect/remove/abort operations are serialized by the retained
`deb-packages/locks/terminal.lock`. The lock files themselves remain; any
source/selection partial or marker blocks cycle cleanup rather than being
deleted speculatively. Package-tar validation owns deterministic siblings
`.<tar>.validate`, `..<tar>.validate.partial`, and
`.<tar>.validate.owner.json`; the marker binds the exact output device, inode,
size, and scratch paths. A later validation, `deb-remove`, or `deb-abort`
recovers only that exact state. Missing or changed ownership fails closed, and
any remaining local validation scratch blocks cleanup of its matching cycle.
Hosted validation scratch is part of its retained release-attempt staging and
is never deleted by local cycle cleanup.

## Manual GitHub Release workflow

`.github/workflows/deb-packages.yml` has only `workflow_dispatch`. It checks
out full history for the revision selected by the operator without persisting
credentials, so the workflow is not tied to a branch name. The job has a
six-hour timeout. Its sole implementation command is:

```bash
make -C fork-maintenance ci-deb-release
```

The hosted preflight requires a `workflow_dispatch` event on a valid branch or
tag ref, the exact workflow path and checkout SHA, the job-scoped token, GitHub
CLI 2.97.0 or newer, Podman, a clean checkout, and the branch-agnostic source
boundary above. It updates no local checkout or source-selection ref; only the
publication phase creates its unique remote package tag. The Make/Python
implementation freezes one selection snapshot and one source snapshot for both
distributions, builds Ubuntu 26.04 and Debian 13 independently, rejects a live
selection or source change across the builds, validates both tars, and requires
consistent package versions and revisions.

Before publishing a retried attempt, the target checks every earlier attempt of
the same `GITHUB_RUN_ID`. It may delete only a draft whose earlier Actions
attempt is completed with an exact failure conclusion and whose canonical
embedded transaction matches the run, attempt, checkout, version, expected
asset metadata, release identity, and unchanged tag target. Exact draft recovery
deletes and verifies the tag first, then deletes and verifies the immutable
release ID last. If such a failed or cancelled attempt already published its
exact ordinary release before it stopped, the retry does not publish a
duplicate: it resumes the retention step described below. Other published,
tag-only, changed, or ambiguous state fails closed.

Publication then proves that the current unique release and tag are absent,
using bounded authenticated pagination of
`GET /repos/kogeler/xpra/releases?per_page=100&page=N` for releases. A draft is
never queried through the published-only `/releases/tags/{tag}` endpoint. The
listing stops after at most 100 pages and rejects duplicate immutable IDs or
tag identities. The target writes a local preflight record and creates a draft
with `prerelease=false` through authenticated
`POST /repos/kogeler/xpra/releases`, targeting the exact checkout SHA. It
records and validates the immutable GitHub release ID directly from that
response. Every later draft query, asset upload, publish `PATCH`, or release
deletion addresses that recorded ID; the tag is inspected and removed
separately as a Git ref. The target uploads exactly the two validated assets
(`xpra-ubuntu-26.04-amd64-debs.tar` and
`xpra-debian-13-amd64-debs.tar`), and verifies their remote sizes and SHA-256
digests against that draft. It then publishes the draft as an ordinary release,
requires the displayed release title to be exactly the Debian version (for
example `6.6-r42479-1`), verifies `prerelease=false`, and verifies the resulting
tag still targets the checkout SHA. The tag format is
`kogeler-deb-<debian-version>-run<run-id>-attempt<attempt>`; this unique package
tag is the only remote ref the workflow may create.

After the new release and tag are verified, the target scans the same complete,
bounded authenticated release listing. It recognizes as retention candidates
only ordinary releases with one canonical DEB transaction marker, the exact
version title, expected repository/workflow identity, checkout target, package
tag, and two asset sizes and SHA-256 digests. Candidates are ordered newest
first by `published_at`, with the immutable release ID as a deterministic
tie-breaker. The newest three are retained. Every older exact owned release is
revalidated, then its exact unchanged tag is deleted and verified absent before
its immutable release ID is deleted and verified absent. Drafts and unrelated
or manual releases neither count toward the three nor become deletion targets.
A malformed, changed, duplicate, or ambiguous marker-bearing release stops the
transaction before further deletion.

Retention is part of publication, not a best-effort follow-up. A normal error,
`SIGINT`, or `SIGTERM` before it completes enters rollback for the newly created
release. A hard kill after publication may leave the exact release and a partly
completed retention pass; a later failed/cancelled-attempt retry revalidates
that published release and resumes retention without creating another release.
Deleting each expired release is itself retry-safe when its tag is already
absent or the process stopped between tag and immutable-ID deletion.

If another publication step fails, rollback validates the exact current
release, deletes and verifies its tag first only while it still targets the
expected commit, then deletes and verifies the immutable release ID last. A
missing release with an extant tag or a changed tag fails closed. If the create
response is malformed, bounded release listing may recover only one exact draft
with the expected repository, target, transaction, and assets; its discovered
ID is journaled before the same ordered rollback. Duplicate or ambiguous
matches fail closed. Agents do not invoke this hosted target locally.

Publication drafts and release staging are generated filesystem artifacts below
`.artifacts/fork-maintenance/deb-packages/releases/run-<run-id>-attempt-<n>/`;
after a successful publication the directory retains its two assets,
`release-notes.md`, `publication.json`, and the two hidden distribution
container ownership records for operator review. The publication record moves
from `preflight` through the exact remote lifecycle to `retention-complete` and
records the expired transaction tags removed by that pass. This tree is never
tracked or removed by cycle cleanup; interrupted build staging remains for
explicit operator review as well.

The workflow never runs display, render-node, live RGB, or hardware-H.264
profiles. Those remain local physical acceptance gates.
