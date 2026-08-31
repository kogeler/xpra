# Fork-maintenance namespace migration

## Decision and source boundary

The operator selected Strategy A, the lifecycle-complete cutover, on
2026-08-31. The pre-change audit ran from current filesystem bytes on
`develop` at `7f9b67d23ef9f590dc91ae5fe8983a529cef518e`; the embedded upstream source
boundary was `a4835e209fc7fc9ee1e15c1a5250339c84dc9db3`.

This report intentionally records the historical mapping and cleanup audit.
Current operator documentation and commands use the current namespace. No
permanent retired-namespace scanner, environment rejection hook, compatibility
reader, or compatibility alias is installed.

## Approved mapping

| Identity class | Retired form | Current form |
| --- | --- | --- |
| Make and job environment | `XPRA_LAB_*` | `XPRA_FORK_*` |
| Resource owner values | `xpra-lab-*` | `xpra-fork-maintenance-*` |
| Image, volume, network, container, and job names | `xpra-lab-*` | `xpra-fork-maintenance-*` |
| Podman label keys | `io.xpra.lab.*` | `io.xpra.fork-maintenance.*` |
| Live container installation root | `/opt/xpra-lab` | `/opt/xpra-fork-maintenance` |
| Upstream-test runner root | `/opt/xpra-lab-upstream-tests` | `/opt/xpra-fork-maintenance/upstream-tests` |
| Live patch staging root | `/tmp/xpra-lab-patches` | `/tmp/xpra-fork-maintenance-patches` |
| Dynamic test module names | `xpra_lab_*` | `xpra_fork_maintenance_*` |

The generic workspace snapshot `lab/`, transaction staging
`candidate-lab/source`, selection option `--lab-root`, `LabFailure` exception,
and container user/home `/home/lab` are not project identities. They remain
unchanged as separately classified workspace, error-model, and container-user
contracts. Internal constants that actually denoted the maintenance source
root were renamed to `MAINTENANCE_ROOT`.

## Pre-change inventory

The maintained-tree inventory contained 414 strict retired-namespace
occurrences in 18 files:

| Classification | Occurrences |
| --- | ---: |
| Input/API | 91 |
| Runtime identity | 204 |
| Container filesystem contract | 32 |
| Internal code and test identity | 87 |

Retained runtime and result history contained 35,034 historical occurrences
across 1,437 files. Those immutable artifacts were not rewritten.

The pre-change Podman inventory had no containers or networks and contained:

- 40 exactly labelled live images;
- 17 exactly labelled DEB builder images;
- 9 exactly named and labelled upstream-test images;
- one old upstream-test ccache volume;
- one foreign lookalike image, `localhost/xpra-ci:test`, with immutable ID
  `463f2603e257a7dd29c5fb5c03295902a8189a673002a2ea38117756417569b7`.

The foreign image has a noncanonical build-run value and no owned maintenance
tag, so it was never a cleanup target. The 66 owned-image identity set was
bound to SHA-256
`39db899f504dca11737bbfaf96544490b8f006e5449da63a996e8b7e45fc4931`.

Filesystem runtime state also held 17 collected live owner records and 11
upstream-test owner records. There were no active DEB owners. The live owner
set digest was
`245080f5668b166ea8eefede9927b2b609d9710e830d568e155cbf135c29c332`;
the upstream owner set digest was
`8aee68d49f3b6a513b4fe21169cf95d9ace34c680b79b02d2750a84e1dd92c69`.

## Strategy A retained-state audit

The old code remained authoritative while cleanup ran. All 11 upstream owners
were removed through `test-remove`. A temporary, digest-bounded reader allowed
`live-remove` to process only the exact 17 pre-profile live records; its unit
tests proved that normal reads, changed records, and explicit invalid profiles
still failed. That reader and its finite digest mapping were removed before the
source cutover. Logs, statuses, reports, and immutable removal transactions
were retained.

The image and volume removal plan acquired the upstream-test lifecycle and
image-cache locks, live lifecycle lock, DEB terminal lock, and every retained
DEB image-key lock. It rejected active owners, image/volume users, changed
labels, changed immutable IDs, mixed identities, and the foreign lookalike.
The reviewed plan confirmation was
`162f862f94028f63a36ff536d140e1bf0af919b485921902bba23807f86f984f`.

The resumable transaction is retained below ignored runtime state at
`.artifacts/fork-maintenance/namespace-migration/strategy-a-remove.json`; its
SHA-256 is
`8f182545ba206260feeaa407289cd16c21d59e90c5764dc2a44b1717f97ff2b1`.
The completion record SHA-256 is
`4ac27239fd46d57fd2a103f27968913778745192a89e2d9e90d0440ef0596045`.
It records removal of all 66 owned images and the exact old ccache volume.

The post-clean audit found zero old-namespace owned owners, containers,
networks, images, or volumes. Only the documented foreign lookalike remained.
No source compatibility reader for old runtime state remains.

## Implementation and validation

The migration pairs each writer with every reader and cleanup selector at its
contract boundary. Existing exact owner, immutable-ID, and current-label
checks remain the lifecycle authority; no separate legacy-namespace policy is
part of normal execution.

The final current-tree validation completed from uncommitted filesystem bytes:

- `make check` passed, including 172 contrib tests, 113 live tests, 105 DEB
  tests, 67 upstream-test lifecycle tests, and the auxiliary checks. Ruff,
  Python compilation, shell syntax, documentation checks, and
  `git diff --check` passed.
- `isolated-start-check`, `deb-policy-check`, `live-venv-check`, `doctor`,
  `stack-check STACK=develop`, `artifact-boundary`, and `ci-layout-check`
  passed. `develop-check` reached and correctly enforced its dirty-tree
  publication boundary; no commit was created because this migration did not
  grant commit authority.
- A one-time byte scan found no strict retired technical identifier in a
  maintained current file outside this historical report. The scan was not
  installed as a policy target, module, environment guard, or reusable
  successful result.
- The rebuilt upstream-test image has immutable ID
  `d355a401da09213453cc8ba9a37d6c23b2dcbc810c91d6f64c36abbe7f4de463`.
  Its history and filesystem manifest contain no retired path, and its FIFO and
  runner are under `/opt/xpra-fork-maintenance/upstream-tests/`.
- Clean and patched full-stack payload routes passed. The valid tests-only
  route for `wayland-initial-window-state` passed. The final complete patched
  upstream matrix passed in detached runs
  `namespace-cleanup-a-20260831-full-03`,
  `namespace-cleanup-a-20260831-full-cython-03`, and
  `namespace-cleanup-a-20260831-full-no-compat-03`; all three exited zero and
  their owned runtime was removed after collection.
- All six public positive live profiles passed: Zed RGB, adaptive-alpha Zed
  H.264, RGB detach, RGB transport loss, multi-window Vulkan hardware, and
  multi-window OpenGL hardware. Each proved rendering, input, its selected
  lifecycle, exact current ownership, and empty owned containers/networks after
  cleanup.
- The live server and client images have immutable IDs
  `0e01b9ff42c98c2fc16bff2020a3f1a2df0e944f1de1149d096b66b5621d8c4c`
  and
  `11ec7bdea32f8d18f4e8047744307e3dc2a4650bc0136fde325be462db57f37c`.
  Their histories and filesystem manifests contain no retired path; their
  installed helpers are under `/opt/xpra-fork-maintenance/`.
- Ubuntu 26.04 and Debian 13 DEB builds each produced and host-validated the
  complete 17-package set with `validation_ok=1`. Their builder-image immutable
  IDs are
  `fb775c4e98d0bcede1dfde5781a6c33aac293eb45a1fd52e41396ca991e9d158`
  and
  `a75b839401b36844326f663bcde6073b2e66403d3c03b87ee0954cc0d87b8a18`.
  Both history/filesystem scans passed, both package runtimes were removed,
  and the validated output tar files were retained.

The final runtime inventory has no maintenance-owned container, network, or
active owner. It retains only current caches: the five validated images and
`xpra-fork-maintenance-upstream-ccache`. No old-namespace owned image or volume
reappeared. The one pre-existing foreign lookalike recorded above remains
untouched.

## Rollback

Rollback must never make current resources invisible to their cleanup
authority.

1. Stop new starts and inventory every current owner through the current
   `live-status`, `test-status`, `test-image-status`, and `deb-status` targets.
2. Collect completed jobs, then use the matching current `*-remove` target.
   Use the exact current `*-abort` target only for state that its contract
   permits aborting. Do not signal processes or remove containers directly.
3. Re-run the current-label Podman inventory. Review immutable IDs, exact names,
   complete labels, retained owner records, and all lifecycle locks. Remove
   current cache images or volumes only through an operator-reviewed,
   digest-confirmed transaction using those exact identities; never widen a
   selector to `xpra-*`.
4. Prove that no current-namespace owned runtime remains. Preserve collected
   evidence and removal transactions.
5. Only then revert source changes. If source rollback is needed before step 4
   can be completed, keep the current cleanup code available in an isolated
   reviewed checkout until its owned inventory reaches zero.

Reverting ordinary implementation behavior while retaining the current
namespace is preferred when possible; it avoids a second lifecycle migration.
