# Run Direct Xpra And Physical-GPU Tests

Use [`validation.md`](validation.md) to schedule these fixed positive gates.
During development, run the relevant profile after its nearest focused/native
checks; all three full upstream suites are not a prerequisite. Final acceptance
fills only missing or invalidated atomic/stack requirements after candidate
freeze. Development-stage named results may count when their exact final
inputs and assertions remain valid; foreground diagnostics cannot.

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
current selected patch resolution. These examples use the complete stack;
for an early atomic run use its admitted `CASE=<slug>` in workspace creation
instead of `STACK=develop`:

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
embedded-source archive, a complete frozen harness, server and client
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

Pass any name returned above to any of the seven complete-stack wrappers or to
either case-only wrapper:

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
diagnostics, subcommand, clipboard direction, or transport, with encoding and
policy below each transport. The runner adds only genuinely dynamic values
such as endpoint, session name, child command, display, and selected device. Do
not duplicate a tracked YAML value in Python, Make, or a unit-test assertion.

The final complete-queue coverage includes each complete-stack wrapper with the
YAML default and runs every case-owned wrapper declared by a retained
production manifest separately. Reuse input-verified completed results rather
than rerunning them at a phase transition. Selecting the other network profiles is
optional coverage, not a larger mandatory release matrix. It changes only
client tuning: all codec, hardware, pixel, input, lifecycle, and cleanup gates
remain identical and must still pass. The strict loader, both YAML files, and
the selected profile name are frozen and hash-bound before the worker starts.

The main worker reads only those run-owned inputs, so later source, harness,
queue, or application-directory edits cannot change the run. Image cache tags
are keyed by each complete context digest and verified labels; containers are
created from the inspected immutable server/client image IDs, not from mutable
tag names. The final report and status bind the source, both selections and
resolution digests, context archives/trees, Zed and harness/input digests, and
the actual image IDs with their complete ownership labels.

The server and client build stages install Xpra and run their existing native
checks before copying/compiling independent C fixtures. A fixture-only edit
still changes the complete context/image identity, but need not invalidate the
unchanged Xpra build layer. Do not move fixture inputs ahead of that layer or
drop them from context hashes. Native flags, output paths and runtime checks
remain mandatory; record an actual cache hit before claiming a measured saving.

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
rebase requires every production case's declared live gates with its atomic
case selection and all seven fixed positive live profiles with the complete
stack selection on the final adapted candidate. A semantic difference on an
unchanged base requires the affected profile checks during development and
corresponding final coverage, not every live profile after every edit.

Every named live acceptance run requires one nonempty reviewed `CASE` or
`STACK` selection. A clean-source diagnostic is not live acceptance and must
use the isolated/unit diagnostic paths; it cannot publish a live `PASS`.

The complete-stack positive set is exactly `live-rgb`, `live-h264`,
`live-xpra-detach`, `live-xpra-transport-loss`, `live-xpra-hardware`, and
`live-xpra-opengl-hardware`, plus `live-wayland-keyboard`. Fail-closed unit
fixtures prove that invalid evidence is rejected; every named live target
itself must prove its intended Xpra behavior and finish positive. Their
acceptance dimensions are fixed;
`NETWORK_PROFILE` is the orthogonal client-only tuning overlay described above.
`live-x11-clipboard` and `live-wayland-subsurface` are additional case-only
positive gates and do not change this seven-profile stack set.

## Client keymap synchronization with a native-Wayland server

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

The clean maintained client uses `setxkbmap` on its actual X11 display. This
live gate exercises legacy client keymap transport; negotiated exact-wire
transport and native-Wayland client discovery remain focused/native coverage.
The bound scenario first uses model `pc104` with four ordered groups
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

## X11 client to native-Wayland clipboard synchronization

Run the clipboard case through its dedicated wrapper; it accepts neither
another case nor a stack selection:

```bash
make -C fork-maintenance live-x11-clipboard \
  CASE=x11-client-clipboard-events RUN=x11-client-clipboard-events-live-01
make -C fork-maintenance live-wait \
  RUN=x11-client-clipboard-events-live-01
make -C fork-maintenance live-status \
  RUN=x11-client-clipboard-events-live-01
make -C fork-maintenance live-logs \
  RUN=x11-client-clipboard-events-live-01
make -C fork-maintenance live-remove \
  RUN=x11-client-clipboard-events-live-01
make -C fork-maintenance live-status \
  RUN=x11-client-clipboard-events-live-01
```

The wrapper fixes RGB, application-exit, strict H.264 policy, and the default
alpha scenario. Unlike the seven complete-stack profiles, this client-side
regression builds the exact selected case source for both the Debian 13 X11
client and Ubuntu 26.04 native-Wayland server. Input freeze and final
collection require identical selection, resolution, and source context at both
endpoints. The Ubuntu and Debian images have independently bound immutable
image IDs and verified role labels; their IDs are not required to match.
The client CLI is taken only from
`live-cli.yml` and fixes `xsettings=no` and `input-devices=noxi2`; this keeps
unrelated XSettings and XI2 filter owners from masking a missing clipboard
filter lease.

One named run owns three fresh Xpra sessions, in fixed `both`, `to-server`, and
`off` order. In each session the X11 fixture first owns `CLIPBOARD`, while an
independent raw X11 converter proves `TARGETS` and the exact first marker
without using Xpra. Updating the same owner object to the second marker must
retain its owner XID, advance the XFixes ownership timestamp, and remain
locally convertible. The owner then restores the first marker through the same
XID, proving that repeated updates do not return stale data. The
native-Wayland GTK fixture observes all three forward policy results. `both`
and `to-server` must deliver the initial, changed, and restored markers without
reconnecting; `off` must deliver none. A separate third marker owned by the
native-Wayland fixture must return to X11 only under `both`: `to-server` and
`off` must not enable the reverse direction. An exact procfs identity artifact
must prove that the same Xpra client process survives both owner changes in
every session.

For the reverse phase, the private command only arms the Wayland owner. The
runner sends F8 through the forwarded Xpra window, the fixture claims inside
that key callback with a current Wayland input serial, and a compositor-driven
`owner-change` record confirms the claim. Keep the root XFixes monitor running
through the raw reverse conversion and until the Xpra client has exited, then
drain a real X-server round trip before publishing the terminal record. Require
the third production owner transition and the same new owner XID in `both`,
followed only by its zero-owner release/destroy after the fixture closes.
`to-server` and `off` retain exactly the two original same-XID forward
transitions and the original raw owner through client termination. A delayed
forbidden takeover after the first reverse read fails the gate.

Before every allowed forward paste, the runner observes the compositor's new
non-NULL selection event without sending input or running a diagnostic command
through Xpra. The retained `clipboard-transitions.json` binds four contiguous
server-log intervals, their observation times, and the final client-exit
observation. Collection reparses the existing safe compositor logs: each
forward phase has exactly one source publication (`off` has none), and the
reverse phase has exactly one non-NULL source after its real F8 event. Adjacent
source identities must differ; later pointer reuse remains valid. This proves
each restored marker belongs to a fresh publication. Clipboard payload logging
remains disabled.

The fixture records the input callback before calling the ownership API. Its
confirmation callback is bound to that request, deduplicated, and cancelled at
close. Collection compares cross-peer monotonic event times, including every
local update, paste request/result, reverse conversion, and monitor closure.
GTK confirmation and the reverse XFixes event are independent deliveries of
the same compositor transition; both must follow the owned input and precede
the raw reverse read. Their relative process scheduling is not authority.
Do not replace these event and terminal-process boundaries with a settle delay.

The fixture accepts only fixed, non-sensitive marker identifiers. Retained
runtime evidence contains event sequence, advertised targets, owner XIDs and
timestamps, policy decisions, lengths, SHA-256 digests, and equality booleans;
it must not contain marker plaintext or sample the operator's clipboard. The
ordinary rendering/pixel, input, application-exit, container/network, and
owned-cleanup checks still apply. A local conversion alone, an empty initial
token, a reconnect, a longer timeout, or polling cannot satisfy the gate.

## Native-Wayland subsurface stream ownership

Run the subsurface case through its dedicated wrapper; it accepts neither
another case nor a stack selection:

```bash
make -C fork-maintenance live-wayland-subsurface \
  CASE=wayland-subsurface-stream-ownership \
  RUN=wayland-subsurface-stream-ownership-live-01
make -C fork-maintenance live-wait \
  RUN=wayland-subsurface-stream-ownership-live-01
make -C fork-maintenance live-status \
  RUN=wayland-subsurface-stream-ownership-live-01
make -C fork-maintenance live-logs \
  RUN=wayland-subsurface-stream-ownership-live-01
make -C fork-maintenance live-remove \
  RUN=wayland-subsurface-stream-ownership-live-01
```

The wrapper fixes RGB, application-exit, strict H.264 policy, and the default
alpha scenario. The exact selected case source and resolution are applied to
both the Ubuntu 26.04 native-Wayland server and Debian 13 GTK X11 client:
server composition transactions and client backing semantics are one atomic
wire contract. Input freeze, image contexts, final report, and collection must
all preserve and verify the matching endpoint identities. The profile's fixed
client arguments select Cairo; mapped GTK OpenGL replacement and close are
covered separately by the case's real-Xvfb focused regression, not inferred
from this Cairo run.

The dedicated C fixture creates a 420x300 primary and 360x260 secondary
`xdg_toplevel`. Its 220x140 lower and 160x100 upper children use real ARGB
`wl_subsurface` buffers with fixed partial alpha and premultiplied patterned
channels over heterogeneous parent pixels. The lower uses a 440x280 scale-2
buffer; the upper uses transform 180 and is committed before its first
subsurface role exists.

The fixture emits schema 6. For `2 <= N <= 256` continuous generations its
exact `15 + N` event sequence is:

```text
ready
lower-state
lower-state
lower-moved
sibling-created
lower-updated-under-upper
lower-frame-generation
lower-frame-generation
continuous-start
continuous-generation x N
continuous-stop
sibling-click
lower-destroyed
upper-detached
upper-reparented
exit
```

Every object has the exact declared field set, schema value, zero-based
sequence, and strictly increasing monotonic timestamp. The first fixed phases
bind initial, changed, restored, move-without-attach, overlap, lower update,
and two marker-gated frame generations. Each frame generation consumes one
real callback ID/data pair and advances the exact attach, commit, update, and
done counts.

Continuous commits also require a 50 ms monotonic cadence floor. The same
sampled commit timestamp is emitted in the event stream; an immediate callback
does not bypass the floor, and a late wake-up does not trigger a catch-up burst.
The loop remains responsive to Wayland input and the stop marker. The observer
has five seconds from continuous-start for its active proof, including packet
collection and the subsequent fresh producer sample. The separate schema-3
active/drain record fixes an exclusive packet frontier from the first primary
inventory before fetching other streams. Validate every packet below it as
complete transactions plus one root-stage tail, then independently bind that
exact prefix to the final immutable packet ledger. Later packets still require
complete drain, pixel and global packet/ACK validation; a frontier cannot hide
an interior gap or delete later evidence. Bounded per-attempt diagnostics retain
the stage, reason, role counts/frontiers and monotonic durations, not pixels.
The observer records its initial
generation count/time and requires a later generation, while the unchanged
256-generation cap remains a hard guard. This fixture timing does not impose
a production frame rate on Xpra or require a packet for every source commit.

After `continuous-start`, the same lower surface alternates two fixed buffers.
The odd-generation buffer is a dedicated image: only the advertised 32x32
lower-local damage rectangle differs from the final full-frame buffer. Every
pixel outside that rectangle is identical in both continuous buffers and in
the preceding frame. Partial damage therefore describes the complete buffer
change, including when Xpra retains and later replays the whole surface.
It may commit the next generation only after the previous
`wl_surface_frame` callback fires. Before requesting stop, capture a retained
active-liveness artifact while the process is alive, the stop marker is absent,
the producer is active, `2 <= active_generation_count < 256`, at least two
complete three-stage transactions exist, and their lower payloads include both
fixed digests. After `continuous-stop`, accept a terminal `N` up to the
inclusive 256 safety cap, require the last callback either completed or was
explicitly cancelled with no ambiguous state. Count `N` fixture commits and
their callbacks separately from the `M` immutable composition captures:
`2 <= M <= N`, because uncaptured pending damage may coalesce. The captured
pixel states must form an ordered subsequence of the generated states; the
last captured state must equal the final committed state. Every captured
transaction has exactly three stages and one complete set of packet/ACK
records. Require zero pending-region and in-flight composition counters in
addition to drained encoding and ACK queues. A transaction or frame completed
only after the producer stopped cannot satisfy the active proof.

The upper role is later destroyed, roundtripped, and recreated under the second
parent without another buffer attach or child commit. Because the underlying
`wl_surface` stays alive, its fixture proxy, retained buffer, Xpra wrapper, and
internal WID all remain identical. Only native surface destruction may retire
that identity.

Establish startup as a separate bounded history before issuing the first
controlled change. The initial server full damage and client-map refresh may
coalesce: accept one or two complete primary root/lower transactions and
independently one or two ordinary secondary full-canvas packets. Retain and
validate every packet, including the canonical raw pixels, exact transaction
stages and epochs, client draw and ACK, and global/source sequence inventory.
Bind fresh server focus handlers for both exact parent WIDs after their GTK
map packets on the same connection, then require a stable drained source
snapshot. Retain the bounded focus-log interval and revalidate it at collection;
an unawaited X11 activation or an old focus line is not a map barrier.
For the ordinary secondary, bind both initial/map damage requests and every
resulting full-window capture from the server log, with the final capture
after the final request. Check both parents' existing encoding/ACK queues and
exact packet counts as well as the composite and child counters.
The last entries bind the named
initial capture; they must not hide earlier packets. Derive cumulative source
counts from that exact startup history, retaining exact later phase deltas.

For each subsequent fixed phase, wait for its one complete transaction and
drained `xpra info` snapshot before taking both parent-window captures. Record
a separate continuous-final capture and drained info boundary after stopping
the producer. Role creation in `stacked` and role reattachment in `reparented`
require their exact full-primary/full-secondary reconciliation plans; role
removal repairs only the removed footprint and surviving intersections.
Stable-tree content damage remains local. Do not accept an arbitrary superset
or a partial substitute for any phase's exact production-owned repair plan.
The first published packet in every transaction must carry the
exact clipped dirty-union `subsurface-reset`; every packet must name
`premultiplied-source-over-v1`, and no later layer may repeat the reset. Bind
parent/root, lower, and upper packet order to wlroots bottom-to-top stacking.
Each child packet must be positive raw RGB32 with an alpha-bearing
`BGRA`/`RGBA` format. Match every child sequence across saved packet metadata,
the internal-source/current-parent publication line, the parent-wire client
draw, and its source-routed ACK. All parent and child sequences are globally
unique, source packet counts advance exactly, encoding queues drain, and the
connection ACK-owner count returns to zero at every fixed and continuous drain
boundary.

Retained packet payloads and their `save_update` metadata are the transport
pixel authority. Independently regenerate the fixture's logical surface
patterns and require each raw packet to match its exact source crop, including
alpha. The oracle describes logical pixels before Xpra; the C fixture stores
the inverse-transformed and scale-2 buffer representation. Their formulas are
cross-checked by compiling and executing the actual C pixel generator in the
focused infrastructure regression. This separates source normalization from
client composition and makes a consistently wrong transform or channel order
observable. Reject compression, bad rowstride or dimensions, unsafe paths,
digest mismatch, unknown packed formats, and any non-RGB32 composite stage.
Starting from the retained parent baseline, clear the exact reset, replay every
layer in protocol order, and compute each channel as
`Cpremul + round(P * (255 - alpha) / 255)` without premultiplying child bytes a
second time. Compare the complete reconstructed parent to the bound client
capture with zero mean absolute error. Also compare each reconstructed complete
parent against the independently composed fixture scene, including the area
outside continuous damage. Async source screenshots are neither
collected nor accepted as packet-correlated evidence.

The exact comparisons on both parents cover initial, changed, restored, moved,
stacked, lower-updated, both fixed frame generations, continuous-final,
lower-destroyed, upper-detached, and reparented state. They prove restoration
is not stale, movement clears the old footprint, every lower change preserves
the unchanged upper, the continuous producer makes bounded forward progress,
destruction and detach repair the primary root, and the unchanged upper buffer
composites over the heterogeneous secondary root.

Send one real pointer click into the known sibling overlap and require the
client parent-wire button records, exact upper-child server focus and
press/release records, and the fixture's upper-local event within three
seconds. At destroy/detach, require removal of the obsolete nested info entry,
the expected active-source decrement, no post-removal source packet, and no
child-WID EOS while both parent windows continue drawing. Finally signal the
ordered fixture exit and require status zero before normal Xpra
application-exit and owned cleanup.

Before those controlled phases, bind both actual client mapped XID/WM-title
tuples and require them unchanged before fixture exit. Preserve the complete
normal title decoration; diagnostics alone may be bounded. Client XIDs and WM
titles are not server wire IDs and fixture titles: the exact server inventory
continues to be checked independently at every phase.

Collection reparses the schema-6 dynamic fixture JSONL, the complete bounded
startup ledger, eleven fixed phase info snapshots, the separate continuous
active/drain artifacts, endpoint logs,
saved packet metadata and bounded raw payloads, and deterministic client
captures. It recomputes all 36 named classifier results; report booleans are
only cached summaries. A missing layer, wrong reset, mixed or partial
transaction, generation starvation, callback/capture accounting mismatch, straight-alpha
oracle, sequence collision, stale pixels, retained source, pending ACK, or
altered artifact must fail collection. The fixture pixels are fixed and never
sample an operator desktop or clipboard.

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

## H.264 packet-sequence authority

Choose the packet namespace from the frozen selected source and corroborating
runtime identity, never from the presence of a numerical gap. Without
`wayland-subsurface-stream-ownership`, the supported legacy source allocates
damage packet IDs per window and each saved window history must remain
positive, ordered, and numerically dense. With that case selected, every
ordinary parent and internal child uses the connection allocator, even when
the workload has no active subsurfaces. The shared H.264 observer admits this
connection-global namespace only with one confirmed active connection bound
to the owned run, server UUID, client UUID, session ID, connection time, and
endpoint. A client UUID may recur across runs; it and a client-info index are
not sufficient session identity. Actual initial
`damage.next-packet-sequence` and `damage.ack-owners` values corroborate the
namespace only. They neither limit a later observation frontier nor prove
terminal drain. Source selection and runtime state must agree; absent or
conflicting authority fails closed rather than triggering a compatibility
fallback.

For the connection-global namespace, retain an exact ledger across all
declared, title-bound ordinary windows of the profile. Keep each original
packet sequence and source WID, saved metadata path, metadata/payload digests,
and payload length. The ordered projection onto one window may have a gap
only when every intervening ID is accounted for by a packet of another
declared window in the same verified ledger. A merely increasing list, a
renumbered list, an unrelated window's counter, or an undeclared producer
cannot establish completeness. Duplicate global IDs, a missing primary
packet, or an unexplained connection-wide hole fails the gate.
This is an exact-accounting requirement for the controlled profile, not a
general protocol guarantee that every reserved ID is published. Production
may consume an ID for cancelled stale work; do not change that behavior or
reuse the ID merely to make a trace numerically dense.

Seal a bounded startup prefix before using it as active-stream evidence.
Artifact reads from different window streams need not be simultaneous; the
explicit frontier determines which complete prefix is being checked. Final
acceptance requires the complete retained declared-window history, an exact
match for the sealed prefix, and all applicable client draw, ACK, and terminal
checks. Do not infer a final allocator value or zero ACK owners from the
initial namespace snapshot; either claim needs an actual fresh quiescent
observation.
Later packets cannot be discarded by choosing an earlier successful prefix.
The existing final encoder/decoder shutdown checks remain separate; their
narrow terminal-frame exception does not excuse a ledger omission.

All per-window H.264 consumers use this same namespace authority: startup and
IDR selection, damage groups, stimulus intervals, full stream suffixes, and
client packet-chain checks. They retain the actual IDs rather than generating
surrogate per-window sequences. H.264 context frame indexes remain contiguous;
storage bucket indexes and each descending `flush` countdown remain exact.
The codec, frame-alpha, crop/edge, hardware, and pixel requirements do not
change when another owned window interleaves a packet.

The frozen host runner computes this ordinary-root H.264 ledger from the saved
packet metadata and payloads. It records readiness and profile-owned baseline/
stimulus cuts in `h264-sequence-observations.json`, then rebuilds the ledger
from the final artifact inventory and checks the retained prefixes and tails.
Named-job collection independently
verifies the frozen source/input/harness provenance, artifact digests and report
embedding; it does not independently replay this H.264 ledger's semantics.
Keep that trust boundary distinct from the case-only subsurface RGB collector,
which has its own semantic reconstruction. Neither a report boolean nor a
reinterpretation of an old negative run replaces the required positive runtime
observation.

This shared observer covers ordinary-root H.264 and its picture auxiliary.
It does not replace or weaken the separate case-only WSSO RGB transaction
ledger, which owns internal-source/current-parent routing, transaction stages,
epochs, raw premultiplied payloads, and exact source ACKs.

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

For both lifecycle profiles, inspect the scenario report rather than accepting
only the high-level survival booleans. `application_identity_at_capture`, the
corresponding `application_identity_after_*`, and
`application_identity_before_termination` must be identical and must contain
the fixture's exact Python/script argv, PID, procfs start ticks, and command-line
digest. The corresponding `server_identity_at_capture`,
`server_identity_after_*`, and
`server_identity_before_application_termination` must likewise be the same
bounded PID/start-ticks/argv/digest identity, its PID must equal `server_pid`,
and it must differ from the fixture PID. `application_termination` must bind
both identities, record both successful pidfd opens, and record a successful
fixture-pidfd `SIGTERM`. The in-container termination probe must reject a dead
or zombie server, double-snapshot both identities, and poll the server pidfd
again immediately before the signal. The report must then show the exact
fixture gone and only afterward the server exit. This prevents
`pgrep --full` from mistaking the older Xpra server for the child merely because
the server argv contains `--start-child=...interaction_fixture.py`. Named-job
collection must independently reparse the retained private
`interaction.identity.json` and `server.pid` files and bind them to the raw
exact-live application activity, its hardware PID/argv, lifecycle capture, and
server identity/PID fields, even if all report and artifact digests are
internally refreshed.

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
`want-alpha=False`. The saved packet history must have positive sequence
numbers in recorded order and be complete in its
[verified namespace](#h264-packet-sequence-authority): dense per window for
the legacy allocator, or an exact window projection of the complete owned
connection ledger when WSSO is selected. Rounded damage-time directories are
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

Keep every harness input unchanged until the run has been collected and its
owned runtime removed. Input freezing isolates the worker from later host
edits; it does not authorize collection with a different current harness.
Parallel work may inspect the harness or edit unrelated documentation, but
must not change a shared harness input while any live job still owns it.

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
