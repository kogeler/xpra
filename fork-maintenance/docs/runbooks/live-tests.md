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

Verify Podman and the private process-supervisor state, inspect the default
render-node and Zed-path availability, and prove repository identity plus
current patch resolution:

```bash
make -C fork-maintenance doctor
make -C fork-maintenance isolated-start-check
make -C fork-maintenance workspace-create \
  STACK=develop WORKSPACE=live-preflight-01 PATCH_MODE=patched
make -C fork-maintenance workspace-remove WORKSPACE=live-preflight-01
```

`doctor` reports optional hardware/input-path availability; it does not turn
those paths into global prerequisites for unrelated profiles. The selected live
run itself fails closed unless its render node is a readable/writable character
device and, for Zed, its chosen application input can be frozen and executed.

Both live containers run as UID/GID 1001 in
`--userns=keep-id:uid=1001,gid=1001,size=2048`. The explicit bound was verified
against the real Ubuntu 26.04 server and Debian 13 client images, including
writes to `/artifacts`, `/home/lab`, and `/run/user/1001`. It prevents an idle
or running live container pair from reserving the user's complete
subordinate-ID range.
Never replace it with `--userns=host`, omit `size`, or enlarge the host
`/etc/subuid` or `/etc/subgid` allocation. A coexistence audit keeps both live
containers alive while a separately owned `--userns=auto:size=2048` container
is created and run, then removes only those explicitly labelled test objects.

The runner freezes the unique source merge base already embedded in current
`develop` and applies the selected case or stack in its build context; the
examples use the complete `stacks/develop.toml` queue. It performs no fetch or
live master comparison, and cached/upstream master freshness is not a live-test
precondition. The host develop source need not be patched.

Before publishing the main live owner, `live-start` first publishes inspectable
`jobs/live/<RUN>.freeze-prelaunch.json`, then launches and durably publishes
`jobs/live/<RUN>.freeze.json` for the separate input-freeze process. That
process creates one private staging tree containing the content-verified
embedded-source archive, a complete frozen harness, server and clean-client
selection snapshots with their resolutions, validated server/client build-
context tar archives and tree digests, and a Zed archive when selected. A
manifest and `SHA256SUMS` bind the complete input tree. Only after validation is
that tree atomically renamed to `live-results/<RUN>/inputs` and the main live
owner is launched from its frozen harness.

The retained `jobs/live/.lifecycle.lock` is acquired before this freeze starts
and held until the durable main owner is published. Status, abort, or removal
therefore cannot interleave with the freeze-to-main ownership handoff.

## Tracked client profiles and static CLI blocks

[`profiles.yml`](../../profiles.yml) is the sole source of client-side network
and quality profile names, values, and the default. Omitting `NETWORK_PROFILE`
uses its declared `default_profile`. List the names without parsing the file in
shell code:

```bash
make -C fork-maintenance live-profile-list
```

Pass any name returned above to any of the seven public live wrappers:

```bash
make -C fork-maintenance live-h264 \
  STACK=develop NETWORK_PROFILE=<listed-name> RUN=develop-h264-profile-01
```

The selected profile supplies only the Xpra client minimum quality, minimum
speed, auto-refresh delay, refresh rate, and bandwidth limit. These controls
are not server options and are never added to the server command. The common
client bandwidth-detection setting is intentionally static rather than
repeated in every profile.

[`live-cli.yml`](../../live-cli.yml) is the sole source of all other static
Xpra arguments. It groups them by server/client role, then by base, lifecycle,
diagnostics, subcommand, or transport, with encoding and policy below each
transport. The runner adds only genuinely dynamic values such as endpoint,
session name, child command, display, and selected device. Do not duplicate a
tracked YAML value in Python, Make, or a unit-test assertion.

The standard acceptance ladder runs each public wrapper once with the YAML
default. Selecting the other profiles is optional coverage, not a larger
mandatory release matrix. It changes only client tuning: all codec, hardware,
pixel, input, lifecycle, and cleanup gates remain identical and must still pass.
The strict loader, both YAML files, and the selected profile name are frozen and
hash-bound before the worker starts.

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
documentation-only patch refresh on the same embedded source. First verify by exact
old/new applied-tree comparison that paths, modes, executable data,
configuration, test assertions, runner behavior, and live assertions are
unchanged, then run only resolution, whitespace, and fork-control checks and
record the proof. This exception cannot cross `develop-rebase`; every upstream
rebase requires all seven fixed positive live profiles. Any semantic difference
on an unchanged base also requires the declared live gates.

Every named live acceptance run requires one nonempty reviewed `CASE` or
`STACK` selection. A clean-source diagnostic is not live acceptance and must
use the isolated/unit diagnostic paths; it cannot publish a live `PASS`.

The complete public positive set is exactly `live-rgb`, `live-h264`,
`live-xpra-detach`, `live-xpra-transport-loss`, `live-xpra-hardware`, and
`live-xpra-opengl-hardware`, plus `live-wayland-keyboard`. Fail-closed unit
fixtures prove that invalid evidence is rejected; every named live target
itself must prove its intended Xpra behavior and finish positive. Their
acceptance dimensions are fixed;
`NETWORK_PROFILE` is the orthogonal client-only tuning overlay described above.

## Native-Wayland client keymap synchronization

Run the keyboard case independently; do not substitute `live-rgb`, whose Zed
scenario intentionally exercises the unrelated empty-damage case:

```bash
make -C fork-maintenance live-wayland-keyboard \
  CASE=wayland-client-keymap-sync RUN=wayland-keyboard-01
make -C fork-maintenance live-wait RUN=wayland-keyboard-01
make -C fork-maintenance live-remove RUN=wayland-keyboard-01
```

The selected case must provide exactly one
`tests/live-wayland-keyboard.json`. Input freeze validates and binds its exact
schema, digest, two distinct structured RMLVO configurations, aligned
layout/variant groups, options, one unchanged numeric physical keycode, and an
ordered expected character for every group. These values are scenario data;
the runner and production code contain no language or country choices.
Because the scenario path and provenance are currently case-owned, retiring
the keyboard case first requires a reviewed migration to durable neutral or
generic manifest-declared ownership. The migration must update the runner,
provenance checks, immutable inventories, and mutation tests before the
stack-selected keyboard gate can remain valid without that case.

The clean maintained client uses `setxkbmap` on its actual X11 display. The
bound scenario first uses model `pc104` with four ordered groups
(`us,fr,ru,ara`), then replaces it with model `pc105` and `ge,am,us,fr`
without reconnecting. Across both maximum-sized maps, one physical key
exercises Latin, Cyrillic, Arabic, Georgian, and Armenian Unicode text. For
each phase, the runner verifies the queried RMLVO values and
requires the clean client's nested `keymap-changed` packet to receive, install,
and explicitly accept the expected hash in that exact order. A preceding
legacy `layout-changed` application or an identical-only structured result
cannot satisfy this proof. The runner also records `xpra info`, which must expose
the exact effective RMLVO, group count, and final exercised group with no
rejected configuration; a generic application marker from `layout-changed`
cannot satisfy this boundary.
The native driver focuses the forwarded fixture window, locks each real XKB
group, and uses XTEST for one complete physical press/release pair. It never
types text, pastes, constructs an Xpra packet, or sends Unicode directly.
For every injection the report freezes and reparses a bounded `client.stdout`
interval with exactly one clean-Xpra-client `key-action` press and release,
matching the driver's keycode, group, keysym, name, and Unicode string, and the
fixture's exact internal Xpra window ID. The driver's XTEST success booleans
cannot substitute for these client observations.

The native-Wayland fixture is an ordinary focused `Gtk.Entry`. It receives no
scenario or expected string and emits only bounded ordered JSON events with its
actual UTF-8 buffer. Acceptance binds every client injection to the exact
group-aware server resolution and wlroots device press/release, then requires
the entry's complete cumulative text with no missing, extra, duplicate, or
reordered event. The replacement map is installed while the same client and server
processes and the same established TCP connection remain active. The fixture
must close through the forwarded window, exit zero, and drive the normal
application-exit lifecycle. A plausible packet-only report without the entry
observations fails closed.

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

After the established Zed main-window pointer control, this profile also runs
the native `xpra-empty-damage-fixture`. The fixture creates two independently
forwarded `xdg_toplevel` surfaces, makes the second a child of the first, and
waits for the runner to bind distinct GTK/X11 and Xpra server window IDs plus
their current geometries. It then drives both surfaces through at least 60
mapped frame callbacks followed by empty commits before the runner sends one
real click to the child. Acceptance requires the GTK client, Xpra server, and
the child's Wayland pointer listener to observe that input within three
seconds, followed by clean destruction of both client and server windows and a
zero fixture exit. The bounded JSON event stream, before/after server window
inventories, per-window screenshots, and process logs are retained as
`empty-damage.*`, `server-info-empty-damage-*.txt`, and
`empty-damage-{parent,child}.*` in the normal scenario artifacts.

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

The H.264 profiles intentionally use asymmetric CSC configuration:

- the server uses `--csc-modules=libyuv` to convert Wayland `BGRX`/`RGBX` to
  the `NV12` input accepted by its libva encoder;
- the client uses `--csc-modules=none`, because its libva decoder already
  returns `NV12` and the forced native OpenGL backing must render those planes
  directly with its GPU shader.

Do not enable client-side libyuv to make an acceptance profile easier to pass.
That diagnostic variant can add a CPU `NV12`-to-RGB fallback or broaden the
per-window CSC modes, masking the direct decode-to-OpenGL boundary. It cannot
recover transparency lost through H.264 and cannot affect the server cleanup
race. The `--encodings` allowlist is a separate control: selecting a CSC module
does not discover or enable a codec.

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

Run both fixed multi-window hardware acceptance profiles with adaptive alpha:

```bash
make -C fork-maintenance live-xpra-hardware \
  STACK=develop RUN=xpra-hardware-01
make -C fork-maintenance live-wait RUN=xpra-hardware-01
make -C fork-maintenance live-remove RUN=xpra-hardware-01

make -C fork-maintenance live-xpra-opengl-hardware \
  STACK=develop RUN=xpra-opengl-hardware-01
make -C fork-maintenance live-wait RUN=xpra-opengl-hardware-01
make -C fork-maintenance live-remove RUN=xpra-opengl-hardware-01
```

The targets fix `APPLICATION=hardware` and `APPLICATION=opengl` respectively;
both also fix `ENCODING=h264`, `H264_CLIENT_POLICY=adaptive-alpha`,
`ALPHA_SCENARIOS=default`, and the application-exit lifecycle. Both endpoints
advertise only the reviewed H.264/WebP/RGB set while preferring H.264. Each run
resolves its primary and auxiliary GTK Xpra window IDs independently from their
exact titles; window registration order is not evidence.

The declarative manifest gates `live-wayland-h264-hardware` and
`live-wayland-opengl-h264-hardware` map to these two Make wrappers respectively.
The manifest strings are evidence requirements, not additional executable
targets.

Each opaque primary's first saved `window.info` is explicitly an initial
snapshot and must report `BGRX` or `RGBX`; it is not final/per-frame evidence.
Every exact-window `video` frame-state record must remain `BGRX`/`RGBX` with
`want-alpha=False`. The saved packet history must have positive contiguous
sequence numbers in recorded order. Rounded damage-time directories are
storage buckets, not group identity: several real damage groups may share one
millisecond bucket. The descending `flush` countdown reconstructs each exact
group, while bucket-local indexes remain contiguous. Startup layout and picture
groups are structurally validated but cannot satisfy the gate. After both
title-bound windows are stable, the runner records a baseline tied to the active
exact IDR group and its saved source geometry. It closes the primary interval
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
source-to-client pixel, and primary-motion checks. Normally client decoded
frames equal transmitted H.264 packets. Ordered shutdown may leave exactly one
received post-stimulus terminal packet incomplete on the client, or one
additional completed server encode untransmitted. Both exceptions require an
otherwise exact complete packet sequence; larger or in-production differences
fail.

The Vulkan profile runs `vkcube` through native Wayland and requires its live
process to hold the selected render node, map RADV, and produce changing
nonuniform forwarded frames. The OpenGL profile uses the same launcher and
evidence path but selects the native-Wayland `glmark2-wayland` synthetic OpenGL
`jellyfish` benchmark with an explicit no-alpha EGL visual. Its fixed 640x480
source may occupy a larger tiled client backing; the exact logged OpenGL
viewport binds the north-west source crop used by the pixel comparison. After
quiescence its stdout must contain one exact vendor, renderer, and version
identity from the live GL context; the process must hold the selected render node, map the AMD
Mesa/Radeon driver, reject `llvmpipe`, `softpipe`, `swrast`, and other software
renderers, and produce changing nonuniform forwarded frames. This is
server-application rendering evidence. Both profiles independently prove the
client's hardware OpenGL presentation of libva-decoded H.264, so neither
server-side graphics API substitutes for that client boundary.

The auxiliary fixture runs through native Wayland, requires an RGBA visual,
and paints a deterministic transparent border around its opaque interactive
button before publishing main-loop readiness. Its initial `window.info` and
every exact-window frame-state record must report `BGRA` or `RGBA` with
`want-alpha=True`. Every collected server-side source screenshot scoped to
that window must contain both nonopaque and fully opaque pixels. Xpra writes
these screenshots from a GLib idle callback against the then-current window;
they are window-level alpha samples and are not associated with an individual
saved packet. The ordered saved-packet and frame-state log records provide that
separate packet-to-state proof. Its before/after client captures instead prove
visible composition and input response; X11 composition may make their alpha
channel opaque. It must produce a nonempty set containing only positive WebP or
RGB32 packets, each with valid contained geometry and window size. Every RGB32
packet must identify `BGRA` or `RGBA`; H.264, RGB24, non-alpha RGB32, an opaque
source format, or an unreviewed codec fails. Its visible pointer response and
Escape handling remain mandatory, as do ordered application/server/client exit
and exact owned-object cleanup.

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
in [`cycle-cleanup.md`](cycle-cleanup.md) to delete the retained cycle results.
