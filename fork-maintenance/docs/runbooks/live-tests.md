# Run Direct Xpra And Physical-GPU Tests

## Preconditions

Create and verify the hash-locked analysis environment:

```bash
make -C fork-maintenance live-venv
make -C fork-maintenance live-venv-check
```

`live-venv` holds retained `venvs/.environment.lock`; its venv/pip children
inherit that lock. If creation was interrupted, the next `live-venv` validates
`.environment.partial.owner.json` and recovers only the deterministic
`.environment.partial`. A markerless or ambiguous partial fails closed.

Verify Podman, the private process-supervisor state, render-node access,
application inputs, repository identity, and current patch resolution:

```bash
make -C fork-maintenance doctor
make -C fork-maintenance isolated-start-check
make -C fork-maintenance workspace-create \
  STACK=develop WORKSPACE=live-preflight-01 PATCH_MODE=patched
make -C fork-maintenance workspace-remove WORKSPACE=live-preflight-01
```

The runner fetches and freezes the exact equal live fork/canonical master commit
and applies the selected case or stack in its build context; the examples use
the complete `stacks/develop.toml` queue. The host develop source need not be
patched.

Before publishing the main live owner, `live-start` first publishes inspectable
`jobs/live/<RUN>.freeze-prelaunch.json`, then launches and durably publishes
`jobs/live/<RUN>.freeze.json` for the separate input-freeze process. That
process creates one private staging tree containing the content-verified
fork-master source archive, a complete frozen harness, server and clean-client
selection snapshots with their resolutions, validated server/client build-
context tar archives and tree digests, and a Zed archive when selected. A
manifest and `SHA256SUMS` bind the complete input tree. Only after validation is
that tree atomically renamed to `live-results/<RUN>/inputs` and the main live
owner is launched from its frozen harness.

The retained `jobs/live/.lifecycle.lock` is acquired before this freeze starts
and held until the durable main owner is published. Status, abort, or removal
therefore cannot interleave with the freeze-to-main ownership handoff.

The main worker reads only those run-owned inputs, so later source, harness,
queue, or application-directory edits cannot change the run. Image cache tags
are keyed by each complete context digest and verified labels; containers are
created from the inspected immutable server/client image IDs, not from mutable
tag names. The final report and status bind the source, both selections and
resolution digests, context archives/trees, Zed and harness/input digests, and
the actual image IDs with their complete ownership labels.

Live image build contexts use the common validated stdin tar transport. The
optional Zed directory is first frozen into the run-owned input snapshot and
then streamed into the server container after start.
Server/client `/artifacts` directories are container-local. During a workload,
the runner probes active logs in place through bounded metadata or suffix reads
and pulls only immutable one-shot artifacts such as completed screenshots. The
complete artifact set crosses the validated stdout-tar boundary once, after the
application and Xpra client/server workloads have exited; a failed active
workload is not repackaged concurrently. No host artifact or application-input
path is mounted and `podman cp` is not used. Render nodes remain explicit
`--device` access and are not a file-transfer channel.

Live image builds are embedded in and owned by the live job's unique `RUN`;
they do not use a separate `IMAGE_RUN`. A retry therefore uses a new live
`RUN`, including any image-build work it triggers.

Do not rerun a live profile solely for a proven comment, copyright, or
documentation-only patch refresh on the same master. First verify by exact
old/new applied-tree comparison that paths, modes, executable data,
configuration, test assertions, runner behavior, and live assertions are
unchanged, then run only resolution, whitespace, and fork-control checks and
record the proof. Any semantic difference requires the declared live gates.

Every named live acceptance run requires one nonempty reviewed `CASE` or
`STACK` selection. A clean-master diagnostic is not live acceptance and must
use the isolated/unit diagnostic paths; it cannot publish a live `PASS`.

The complete public positive set is exactly `live-rgb`, `live-h264`,
`live-xpra-detach`, `live-xpra-transport-loss`, and `live-xpra-hardware`.
Fail-closed unit fixtures prove that invalid evidence is rejected; every named
live target itself must prove its intended Xpra behavior and finish positive.

## RGB control

Run RGB before H.264 when isolating transport and presentation:

```bash
make -C fork-maintenance live-rgb \
  STACK=develop RUN=develop-rgb-01
make -C fork-maintenance live-wait RUN=develop-rgb-01
make -C fork-maintenance live-remove RUN=develop-rgb-01
```

The profile requires direct RGB transport, visible nonuniform pixels, real
input response, ordered lifecycle, and complete owned-object cleanup. RGB must
not silently become video.

## Adaptive Wayland H.264

The active queue's publication-grade graphics boundary uses frame-aware alpha
policy:

```bash
make -C fork-maintenance live-h264 \
  STACK=develop \
  RUN=develop-h264-adaptive-01
make -C fork-maintenance live-wait RUN=develop-h264-adaptive-01
make -C fork-maintenance live-remove RUN=develop-h264-adaptive-01
```

Both endpoints advertise H.264 and alpha-capable picture encodings while
preferring H.264. Exact saved-packet records bind H.264 and non-alpha codec
edges to an opaque `RGBX`/`BGRX` state. Alpha-bearing RGB32 must instead bind to
an `RGBA`/`BGRA` alpha state. WebP is alpha-capable and may occur in either
state, so its presence alone is not alpha evidence. An odd dimension may
produce only the exact positive one-pixel, non-alpha lossless RGB edge tied to
its H.264 damage group. Full-window, interior, larger, or unbound RGB fallback
fails.

`RGBA`/`BGRA` frames must use an alpha-capable picture encoding and must never
pass silently through H.264. The runner waits for initial picture activity,
then records a stable-geometry interval owned by repeated real Zed input. Every
packet in that interval remains structurally and frame-state validated. Its
selected H.264 production groups must bind to complete VA-API encode/decode
contexts, contain at least ten H.264 frames over at least one second, cover at
least 99% of each production window, and use H.264 for at least 90% of their
encoded packet-region pixels; only their exact required one-pixel edges share
those groups. Every observed crop signature must gain one complete edge set,
but an unchanged edge need not repeat with every frame. Picture groups remain
valid adaptive behavior, are kept outside these H.264 production metrics, and
do not satisfy or replace the sustained dominant-H.264 proof. The profile also
proves the real Zed Vulkan path, selected GPU presentation, visible pixels,
input, lifecycle, and cleanup. A software H.264 encoder or decoder, software
presentation renderer, reconnect, or transport fallback is not acceptance.

Fallback classifiers remain unit-diagnostic helpers only. They are not
accepted live profiles, cannot be started as named live jobs, and cannot
produce acceptance evidence.

## Xpra-only lifecycle profiles

These profiles use direct TCP and deliberately exclude SSH ControlMaster or
parent-product orchestration.

Real detach:

```bash
make -C fork-maintenance live-xpra-detach \
  STACK=develop RUN=xpra-detach-01
make -C fork-maintenance live-wait RUN=xpra-detach-01
make -C fork-maintenance live-remove RUN=xpra-detach-01
```

Abrupt established transport loss through a run-owned single-connection TCP
proxy:

```bash
make -C fork-maintenance live-xpra-transport-loss \
  STACK=develop RUN=xpra-transport-loss-01
make -C fork-maintenance live-wait RUN=xpra-transport-loss-01
make -C fork-maintenance live-remove RUN=xpra-transport-loss-01
```

Run the fixed multi-window hardware acceptance profile with adaptive alpha:

```bash
make -C fork-maintenance live-xpra-hardware \
  STACK=develop RUN=xpra-hardware-01
make -C fork-maintenance live-wait RUN=xpra-hardware-01
make -C fork-maintenance live-remove RUN=xpra-hardware-01
```

This target fixes `APPLICATION=hardware`, `ENCODING=h264`,
`H264_CLIENT_POLICY=adaptive-alpha`, `ALPHA_SCENARIOS=default`, and the
application-exit lifecycle. Both endpoints advertise only the reviewed
H.264/WebP/RGB set while preferring H.264. The runner resolves the primary
`vkcube` and auxiliary GTK Xpra window IDs independently from their exact
titles; window registration order is not evidence.

The `vkcube` primary's first saved `window.info` is explicitly an initial
snapshot and must report `BGRX` or `RGBX`; it is not final/per-frame evidence.
Every exact-window `video` frame-state record must remain `BGRX`/`RGBX` with
`want-alpha=False`. The saved packet history must have positive contiguous
sequence numbers in recorded order. Rounded damage-time directories are
storage buckets, not group identity: several real damage groups may share one
millisecond bucket. The descending `flush` countdown reconstructs each exact
group, while bucket-local indexes remain contiguous. Startup layout and
picture groups are structurally validated but cannot satisfy the gate. After
both title-bound windows reach stable tiled geometry, the runner records a
baseline tied to the active exact IDR group. It closes the primary interval
only after the thresholds below pass and before sending Escape to the
auxiliary window. A production group contains exactly one terminal positive
H.264 main region, optionally preceded by the unique exact one-pixel right
and/or bottom lossless RGB24/RGB32 codec edges allowed by that region's crop.
Every observed `(window-size, main-region-size)` crop signature must gain one
complete required edge set in the interval; an unchanged edge need not be
resent with every H.264 frame. Missing signature coverage, duplicate, dangling,
cross-group, arbitrary, interior, larger, or alpha-bearing RGB fails.

The matched H.264 main stream—not its safe warmup—must contain at least ten
frames spanning at least one second, cover at least 99% of each window, and
account for at least 90% of all encoded pixels in that owned interval. Startup
or post-auxiliary resize WebP/RGB packets do not count for or against these
thresholds, but remain structurally validated. The complete H.264 context
suffix through final quiescence must satisfy the exact VA-API encoder,
saved-packet, client libva decoder, hardware OpenGL presentation,
source-to-client pixel, and Vulkan-motion checks. Client decoded frames equal
transmitted H.264 packets. At ordered shutdown the server may have exactly one
additional completed, untransmitted terminal encode; larger or client-side
count differences fail.

The auxiliary fixture runs through native Wayland, requires an RGBA visual,
and paints a deterministic transparent border around its opaque interactive
button before publishing main-loop readiness. Its initial `window.info` and
every exact-window frame-state record must report `BGRA` or `RGBA` with
`want-alpha=True`. Every saved server-side source screenshot must contain both
nonopaque and fully opaque pixels. Its before/after client captures instead
prove visible composition and input response; X11 composition may make their
alpha channel opaque. It must produce a nonempty set containing only positive
WebP or RGB32 packets, each with valid contained geometry and window size.
Every RGB32 packet must identify `BGRA` or `RGBA`; H.264, RGB24, non-alpha
RGB32, an opaque source format, or an unreviewed codec fails. Its visible
pointer response and Escape handling remain mandatory, as do ordered
application/server/client exit and exact owned-object cleanup.

Do not weaken either per-window contract, substitute a picture-fallback
diagnostic, or encode a known failure as expected success. Every retry uses a
new run identity.

## Evidence boundary

Only named-job execution is acceptance-capable. Foreground `run.py` output is
diagnostic. `live-wait` requires successful supervisor completion, hashed log,
exact report binding, current runner/supervisor digests, and no run-owned
Podman objects.

Every report, screenshot, trace, status, and log remains under ignored
`.artifacts/fork-maintenance/`. Never copy a final-looking report into the
tracked tree.

Use `live-status`, `live-logs`, `live-wait`, `live-collect`, and `live-remove`
for the normal lifecycle. Use `make -C fork-maintenance live-abort RUN=name` to
discard a running or lost uncollected run, or a completed uncollected run only
when its recorded runner is stale. A current completed run must be collected.
`lost` means that no valid completion exists and the exact owned process group
is gone; a dead leader whose owned group still has a live member remains
`running`. The owner and completion bind a private 256-bit token inherited by
the supervised payload. If the leader is gone, every live same-session member
must expose exactly that token or cleanup fails closed and preserves the state;
a legacy tokenless orphan is not signaled. Before the freeze owner exists,
status and abort route through the exact freeze-prelaunch marker; an active
starter is refused and only inactive, unambiguous owned staging can be
recovered. Before the main owner exists, status and logs route to the
input-freeze owner, and abort may terminate and remove that exact freeze plus
its staging/input tree. Before the directories are changed, abort publishes
the schema-1 `kind=live-input-freeze-abort` transaction at
`jobs/live/<RUN>.freeze-abort.json`, binds each device/inode, and atomically
moves it to `live-results/.<RUN>.freeze-abort-{staging,result}` before deletion.
An interrupted transaction is completed only by retrying `live-abort`, which
removes its marker last. Abort verifies the recorded process identity, stops
only a running process group, removes exact labelled Podman objects and partial
results, and publishes no acceptance evidence.

After collection, `live-remove` first publishes retained
`jobs/live/<RUN>.remove.json`. It binds the complete main owner, final log/status
hashes, and hashes of every still-present prelaunch/main/freeze runtime record
before any destructive step. Reinvoking `live-remove` validates that transaction
and finishes only the exact old cleanup; the result tree, log, status, and
transaction remain until cycle cleanup.

Once the main owner is gone, that exact schema-1 transaction is the sole
read-only authority for the removed run. `live-status` validates the transaction,
retained log/status, and every still-present bound runtime record, then reports
`phase=removing` while any such runtime remains or `phase=removed` otherwise.
`live-logs` performs the same validation before returning the digest-bound final
log. A changed schema, evidence digest, or unexpected runtime file fails closed.
This post-remove route does not fall back to input-freeze state; the pre-main
freeze routing above is unchanged.

One retained `jobs/live/.lifecycle.lock` serializes live terminal transitions
with a crash-releasing kernel lock. It is a subsystem lock rather than a
per-`RUN` artifact and remains after normal cleanup. Cycle cleanup blocks any
freeze prelaunch/owner/abort transaction, completion/runtime/result record, or
hidden freeze/abort staging tree; it never guesses that an interrupted input
snapshot is disposable. It requires the removal transaction with every
otherwise finalized live result.

Prefix every live `RUN` with the current cycle identity. After all profiles are
collected, reviewed, and individually removed, use the final two-phase cleanup
in `cycle-cleanup.md` to delete the retained cycle results.
