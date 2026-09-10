# GTK client scroll event ownership

## Boundary

The GTK pointer handler accepts both discrete and smooth scroll events. GDK
can represent one physical action as a smooth delta plus an emulated discrete
direction. Forwarding both creates two independent remote wheel operations.
A correct adapter must preserve smooth fractions without replaying their
discrete emulation, while keeping genuine discrete-only input usable.

This case owns event admission in `PointerWindow._do_scroll_event()`. When
smooth handling and the upstream suppression setting are enabled, a GDK-marked discrete emulation is normally
discarded and the smooth event owns movement. Device names, a guessed event
arrival interval and disabling all discrete events are not equivalent ways
to establish ownership.

X11 requires a narrow exception. Its smooth scroll values are derived from
absolute valuators; the first sample after initialization or reset can expose
a zero delta before the corresponding emulated buttons arrive. Those buttons
retain whole-step movement missing from the smooth sample. The adapter admits
them only for a zero axis with the exact latest X11 event metadata.

Native Wayland has no such X11 valuator baseline and delivers the discrete
copy first. It never uses this fallback. A preceding Wayland zero/stop sample
cannot authorize replay of a later frame's emulated event.

The common client pointer subsystem still owns inversion, accumulation and
wire serialization. Native server conversion now belongs to current upstream;
its [neutral protocol regression](../../infra/upstream-tests/neutral/README.md)
retains independent version-5/version-8 coverage after the production case's retirement.
This patch does not reinterpret wire distances, change XI2 event selection or
deduplicate already transmitted packets on the server.

## Embedded-source context

The case resolves against source commit
`d95058b0916913fe6ae5296fb702f66d833898b0`, embedded in current `develop`.
Upstream commit `9d9d1d09d84` moved wheel handling into the common client
pointer subsystem. The GTK window remains its toolkit adapter: it translates
GDK smooth deltas through `wheel_event()` and discrete directions through a
button press/release pair.

Upstream `f74c91e7320671a91d1d5a7db85a804e2b59e3b5` now suppresses
`event.get_pointer_emulated()` under smooth handling and introduces
`XPRA_SKIP_DUPLICATE_SCROLL_EVENTS`, enabled by default. The current decision is
**adapt and narrow**: preserve that implementation and its opt-out, while
retaining only the missing X11 zero-baseline recovery and complete scroll
admission. The clean handler still drops the first/reset X11 emulated step and
its smooth route does not enforce server-readonly, disabled pointer or coarse
policy like the button route does.

`wheel_smooth` and `wheel_map` remain pointer-subsystem policy. This patch reads
them, and keeps upstream's process-level suppression setting, rather than
inventing another configuration or modifying common packet serialization.

On an upstream refresh, replacement is behavioral. Clean source must admit
one representation, preserve the first X11 whole step after a baseline reset,
keep fractional smooth input and standalone discrete input, and honor
disabled/readonly policy. Merely adding a GDK emulation check can eliminate
duplicates while losing that first X11 movement.

## Surrounding code and ownership map

| Component | Responsibility |
| --- | --- |
| GDK's X11 and Wayland backends | Supply native scroll events, public device/window metadata and the private pointer-emulation flag. |
| GTK widget event delivery | Delivers scroll callbacks to the window adapter; native event lifetime remains with GDK/GTK. |
| `xpra/client/gtk3/window/pointer.py` | Owns event admission, the bounded X11 zero-axis fallback key, smooth normalization and discrete button-action entry. |
| `xpra/client/subsystem/pointer.py` | Owns `wheel_smooth`, `wheel_map`, inversion, delta accumulation, button serialization and precise/compatibility wheel transport. |
| `xpra/server/subsystem/pointer.py` | Parses admitted operations and chooses the native device or discrete fallback after ordinary pointer policy. |
| `xpra/wayland/server/pointer.pyx` | Current upstream converts one admitted operation into native axis units; independent neutral protocol tests retain that boundary. |
| `tests/unittests/unit/wayland/gtk_scroll_test.py` | Exercises the real GDK flag, GTK callback route, adapter and outgoing pointer serializer with controlled event sequences in both focused and native Wayland gates. |
| `fork-maintenance/infra/live/run.py` | Owns actual Sway-to-Xwayland and XTEST input, packet/native-axis accounting, remote GTK response and final log-tail checks. |

The corrected adapter has two legitimate output paths:

```text
GDK scroll event -> GTK widget callback -> PointerWindow._do_scroll_event()
  |
  +-- pointer policy denies input ----------------------> no output; invalidate key
  |
  +-- smooth event
  |     +-- coarse policy -----------------------------> no output; invalidate key
  |     +-- smooth enabled
  |           -> replace latest X11 key and zero-axis mask
  |           -> normalize deltas -> PointerClient.wheel_event()
  |
  +-- discrete event
        +-- genuine, coarse policy, or suppression off -> button press/release
        +-- emulated with smooth enabled
              +-- exact X11 zero-axis match -----------> button press/release
              +-- otherwise --------------------------> no output
```

Every branch completes the GTK handler with `True`. Suppression is local
event consumption, not postponement or a request for GTK to resend the event.
Unknown discrete directions still have no valid button mapping and produce
no button action.

## Admission and smooth/discrete policy

Before processing either representation, the handler resolves the pointer
subsystem and rejects input when it is missing, the client or server is
readonly, the server has disabled pointer input, or `wheel_map` is empty.
The rejected callback invalidates `_smooth_scroll_key`, so later allowed
input cannot borrow a previously admitted sample through that denied event.
It also clears the zero-axis mask.

With coarse policy, the handler also invalidates the key and ignores smooth
events. Discrete events, including native emulation, retain the existing
press/release path. Dropping every marked discrete event independently of
`wheel_smooth` would break this deliberate coarse-mode ownership.

With smooth policy, an unmarked discrete event remains usable. It can belong
to a genuine discrete-only device, and the adapter must not discard it just
because smooth events were seen earlier. Only a marked emulation enters the
fallback decision.

`XPRA_SKIP_DUPLICATE_SCROLL_EVENTS=0` deliberately restores forwarding of
marked discrete events even under smooth policy, preserving upstream's escape
hatch for backends which mark standalone events as emulated. No X11 fallback
state is retained or consulted in that mode. It may produce both representations
when the backend really supplies both; that is an explicit policy choice, not
permission to bypass readonly, disabled-pointer or mousewheel-off admission.

| Event/policy | Owner of movement |
| --- | --- |
| Smooth event, smooth enabled | The smooth delta through `PointerClient.wheel_event()`. |
| Smooth event, coarse policy | No output from this representation. |
| Genuine discrete event, admitted policy | The existing button press/release path. |
| Emulated discrete event, coarse policy | The discrete representation. |
| Emulated discrete event, suppression disabled | Forwarded by deliberate upstream opt-out; a smooth counterpart is also forwarded. |
| Emulated discrete event, suppression and smooth enabled, exact X11 zero-axis match | The discrete representation for movement absent from that axis's smooth sample. |
| Other emulated discrete event, suppression and smooth enabled | No output; smooth input owns the operation. |
| Missing/disabled pointer, empty map or readonly | Neither representation is forwarded. |

The patch does not add observers for every policy assignment. Key invalidation
happens when a denied/coarse callback is processed, when a later smooth event
replaces the key, or during window initialization/cleanup. It should not be
documented as a new global pointer-policy lifecycle API.

## X11 zero-valuator fallback

For an admitted smooth event with suppression enabled, `_x11_scroll_key()` requires a real event
window, logical device and source device, and checks that the event window's
display has GType name `GdkX11Display`. The event's display is authoritative;
the process may have both X11 and Wayland displays open.

The resulting key contains all of:

- the native event timestamp;
- the event window;
- the logical device and source device;
- local `x`, `y` and root `x_root`, `y_root` coordinates; and
- the integer modifier/button state.

Missing identities or a non-X11 display yield an empty key. There is no
device-name allowlist, time tolerance, host-backend assumption or map of
historical events. Comparison uses the actual public event values and GDK
objects, not only a timestamp or one device ID.

The smooth event also records a two-bit mask from its raw deltas: mask value
`1` is set when `delta_x == 0`, value `2` when `delta_y == 0`. The test is before
normalization and uses exact zero, not a "small enough" threshold. A real
fractional delta cannot grant permission to replay its whole-step emulation.

For an emulated discrete event, vertical buttons 4/5 select mask value `2` and
horizontal buttons 6/7 select value `1`. Admission requires a nonempty stored
key, that axis's zero bit, and equality with the new event's complete X11
key. A nonzero vertical sample can therefore suppress its vertical emulation
while a zero horizontal axis still admits matching horizontal movement.

Every smooth event replaces the key and mask, even when its timestamp and
all other public metadata equal the previous sample. Updating only when time
changes would leave a zero-axis allowance active after a later nonzero sample
in the same timestamp and reintroduce duplicate scrolling.

An accepted discrete fallback does not consume the zero-axis allowance.
One initial absolute sample may generate several whole-step emulated buttons;
dropping all but the first would lose movement. The allowance stays bounded
to the latest matching smooth sample and is superseded by the next one.

This is recovery of available information, not reconstruction of the raw
valuator. If the first fractional X11 sample has no discrete counterpart,
there is no movement value for the adapter to recover. The patch does not
invent one or claim to repair GDK's underlying valuator baseline.

## Native Wayland ordering

For the native Wayland wheel path exercised by the regression, GDK delivers
the marked discrete event before the smooth event. Waiting for a same-frame
smooth callback is unnecessary: the emulation flag already identifies the
discrete copy, so it is rejected immediately when smooth handling is enabled.

Wayland events cannot create an X11 fallback key. In particular, a previous
zero/stop event followed by a new frame's discrete-first event must still
produce only the new smooth operation, even if their public timestamps and
coordinates happen to coincide. A backend-neutral cache of the last zero
delta would get that sequence wrong.

The regression also replays the pair in reverse order and repeats it, proving
that ordinary emulation suppression is not an arrival-time heuristic. Those
controlled sequences do not change the documented X11 exception: recovery
there specifically uses its preceding matching smooth sample.

## Normalization, mapping and button state

An admitted smooth event retains the upstream coordinate transform,
`norm_scroll()` calls and vertical sign conversion. It passes `norm_x` and
`-norm_y` to the common pointer subsystem. This patch does not merge axes,
round them into whole clicks or modify the smooth-scroll normalization
setting.

`PointerClient.wheel_event()` continues to accumulate each axis, choose its
button through `wheel_map`, and serialize the precise distance or use its
compatibility fallback. Inversion is applied there, not in the GTK duplicate
decision. The X11 zero-axis test uses the original GDK direction before
inversion; the admitted discrete pair follows ordinary mapping afterward.

An admitted discrete event calls `_button_action()` once with pressed state
and once with released state. Existing modifier translation, pointer data,
window targeting and `button_pressed` bookkeeping remain in that method.
The regression requires an empty pressed-button map after every replay;
deduplication must not leave a phantom held wheel button.

The shared server and native device receive ordinary valid operations. This
case does not change the precise packet's thousandths-of-a-click units or
force one sign on both packet distance and mapped button. Server-side
normalization remains an independent necessary boundary.

## State and event lifetime

The only new per-window fields are `_smooth_scroll_key` and
`_smooth_scroll_zero_axes`. Initialization sets an empty key and zero mask;
cleanup clears both before the existing overlay cleanup calls. Denied, coarse
and suppression-disabled callbacks also clear both fields. A non-X11 smooth
event leaves an empty key and zero mask.

This is one latest-sample record, not an unbounded per-device history. The
key retains a bounded set of GDK window/device objects and scalar values
until replacement or invalidation. It does not retain the original
`Gdk.Event`, queue events for later delivery or install a timer.

The handler reads the native pointer-emulation flag during the event's
callback. That flag belongs to GDK's private event representation; public
fields such as direction, deltas and timestamp do not recreate it. A boxed
event copy can lose the flag and must not replace the live native event in
the regression's positive control.

No production scheduler, thread, lock, device subscription or polling loop
is added. Existing pointer-overlay and button-polling resources retain their
own lifecycle; the new fields neither schedule nor cancel those facilities
on behalf of their owners.

## Patch-queue and integration ownership

`fix.patch` changes only `xpra/client/gtk3/window/pointer.py` and adds
`tests/unittests/unit/wayland/gtk_scroll_test.py`. The manifest has no case
dependencies. It retains the existing client pointer and pointer-loopback
modules, the native `wayland` gate and all three full upstream legs.

Current upstream corrects native direction and scale after a single operation
is admitted. The [neutral pointer consumer](../../infra/upstream-tests/neutral/README.md)
must still reject incorrect native values without depending on this GTK filter,
while this case's default-policy outgoing assertions reject duplicates without
depending on a server that could hide them. The complete queue proves their
composition; no redundant native production patch is retained just to own tests.

The X11 clipboard case owns XFixes/filter leases and clipboard event routing,
not scroll admission or XI2 subscription. The packet-handler error case owns
exception containment, not choosing which wheel representation to send.
Do not absorb either repair or modify upstream XI2 handlers to make these
GTK adapter assertions pass.

## Patch ownership and non-goals

The case does not:

- disable all discrete devices after observing smooth input;
- infer physical device type from a device name or inter-event delay;
- use a timestamp-only pairing rule or an unbounded recent-event cache;
- consume the first X11 fallback allowance after just one whole step;
- apply the X11 baseline exception to native Wayland events;
- remove or bypass upstream's explicit suppression opt-out;
- copy native events to preserve them beyond their callback, or mutate GDK's
  private emulation flag;
- change XI2 masks, absolute valuator state, GTK internals or packet layouts;
- add gesture recognition, inertial scrolling, server-side deduplication or
  application-specific scroll scaling;
- recover an initial fractional movement for which GDK supplied neither a
  nonzero smooth delta nor a discrete event.

## Focused regression design

`unit.wayland.gtk_scroll_test` runs a fresh interpreter, a real
`WaylandCompositor` using Pixman, an owned Xvfb display and a GTK consumer
with both display backends available. Missing native/display dependencies
fail rather than skip. The native fixture maps a GTK drawing area, enters
its surface and injects a real wheel press/release through the compositor's
pointer device.

The regression lives in `unit/wayland` so the declared native gate actually
executes it, as well as the selected focused run. That gate builds both the
native compositor and GTK client modules. A green Wayland run which only ran
unrelated server modules is not a clean control for this case; its tests-only
run must reach the real GTK fixture and expose first/reset loss or forbidden
input admission. The relocation changes neither the six methods nor their
event/packet assertions, and the original client pointer/loopback modules stay
in the focused selection.

The consumer receives GDK's actual marked discrete event and verifies
`get_pointer_emulated()` before using it. All replays needing that private
flag run synchronously while its callback still owns the event. The test
does not fake the getter, write private flags or use a boxed copy. Publicly
constructed smooth and genuine discrete events provide explicit unmarked
controls; the subsequently received native smooth event must also be unmarked.

`Harness` subclasses the real `PointerWindow` and attaches its real scroll
handler to GTK widgets. `Gtk.main_do_event()` delivers each controlled event
through that widget path. A real `PointerClient` performs mapping and packet
serialization; only the endpoint packet queue, surrounding subsystem shells
and coordinate transform are fixtures. Assertions inspect outgoing wheel
and button packets, not a mocked call to the adapter's final send method.

The fixture pins the normal process setting to suppression enabled and checks
that value. Separate replay controls temporarily change only the public module
policy constant; they never mock the GDK flag, adapter or packet serializer.

The six test groups establish:

- **Single ownership:** all four directions, ordinary and reversed pair order,
  repeated pairs and per-axis inversion produce one precise operation per
  admitted smooth step, with the expected mapped button and signed distance.
- **Upstream opt-out:** all axes and both event orders deliberately forward
  both representations when suppression is disabled, including X11 nonzero
  samples. Disabled/readonly admission still rejects both.
- **Discrete and coarse controls:** genuine discrete events and coarse-mode
  pairs produce exactly one press/release pair. Mixed genuine and emulated
  streams preserve independent discrete movement without replaying emulation.
- **Fractions and admission:** simultaneous fractional axes retain their
  exact serialized magnitudes; a zero event produces no packet. Mousewheel
  off, client/server readonly, server pointer off and missing subsystem
  produce no output.
- **X11 baseline/reset:** first zero samples preserve one or several matching
  whole steps, subsequent nonzero samples suppress their emulation, reset
  restores fallback, and axis inversion still maps the admitted button pair.
- **X11 identity and invalidation:** per-axis recovery, fractional following
  samples and coincident timestamps do not reuse a stale zero allowance.
  Different times, every local/root coordinate, modifiers, windows, logical devices or
  source devices fail the match. Denied/coarse callbacks and cleanup invalidate
  the allowance. A Wayland stop followed by another frame cannot borrow it.

For the X11 adapter controls, the fixture creates real Xvfb-backed GTK windows
and devices, then combines their public metadata with the still-live native
GDK emulation flag. This deliberately tests the adapter's decision boundary;
it is not an XI2 generator and does not claim to reproduce GDK's absolute
valuator reset mechanism. Actual Xwayland input is the complementary live
boundary below.

The subprocess protocol checks mapping/readiness, one structured observation
result, bounded completion and exit status. Fresh child interpreters inherit
the installed module search path. Cleanup attempts client, device, compositor
and Xvfb even if an earlier cleanup fails. The tests-only clean control uses
existing public methods and must expose lost first/reset X11 movement or the
remaining admission/coarse defect. Ordinary default duplicate suppression is
already present upstream and is now a preservation control, not the negative
oracle. Missing new helpers, imports or test-only APIs are not valid failures.

## Durable live boundary

The complete-stack Vulkan and OpenGL hardware profiles generate actual Sway
axis input into the Xwayland/GTK Xpra client. This exercises the toolkit's
native valuator/emulation route in addition to the focused adapter controls.
The GTK detach and transport-loss profiles use genuine XTEST discrete input,
which must remain usable without a smooth counterpart.

The shared title-bound GTK interaction fixture checks six steps covering
both axes, repetition and reversal. Every stimulus, including the first,
must yield exactly one wire operation: either a wheel packet or a discrete
button press/release pair. The oracle permits the first X11 step's discrete
fallback but never accepts both representations of one action.

Each operation must also match the native server axis record, the remote
GTK displacement and visible feedback. The full post-input packet/native
log tails and final fixture stream are reparsed during collection. A late
duplicate cannot be hidden by taking a screenshot before it arrives.

The live oracle accepts behavior, not a particular GTK filtering strategy.
It neither requires every smooth-source step to use a precise packet nor
treats a bounded packet count without application movement as success. The
focused tests remain necessary for fractions, metadata mismatches and the
controlled admission/reset cases not generated by those six live stimuli.

Both endpoints always contain the complete current `stacks/develop` queue.
The manifest's `live-rgb` and `live-h264` gate metadata does not select an
isolated product or waive the other members of the nine-profile suite.

## Invariants not to simplify

- Use GDK's actual emulation flag to distinguish a duplicate representation
  from genuine discrete input.
- Preserve upstream's suppression toggle and its deliberate opt-out semantics.
- Check pointer admission before either representation can send packets.
- Respect coarse policy: it selects discrete ownership rather than disabling
  scrolling altogether.
- Restrict zero-valuator recovery to the event window's X11 display.
- Require the complete latest event key and the matching raw zero axis.
- Replace state on every smooth event, including identical timestamps.
- Preserve several emulated whole steps from one initial zero sample.
- Clear both bounded fields on denied/coarse/opt-out callbacks and at cleanup.
- Never borrow a Wayland stop or zero sample for discrete-first emulation.
- Keep mapping, fractions, packet serialization and native server conversion
  with their existing owners.
- Exercise the real native flag within its callback; do not turn a copied or
  publicly constructed event into a false positive control.
- Require the first real live step, complete packet accounting and remote
  application feedback, not only absence of a duplicate in a mocked stream.

## Required validation

Follow [development and final acceptance](../../docs/runbooks/validation.md).
For an explicit refresh, finish the whole-queue and composed manual-review
exit before runtime regression execution.
After an atomic change, run the manifest's focused modules and a tests-only
clean control against the same frozen image. Include actual native Wayland
and Xvfb/GDK execution, Cythonized and no-compat modes, and composed client/
server pointer regressions. Native subject modules must fail rather than skip
when their dependencies are missing.

Start `live-all STACK=develop RUN=<fresh-prefix>` after focused prerequisites.
At candidate freeze, fill missing or invalidated focused/native, quarantine,
fork-control and all three full upstream legs. Require `live-suite-check`
for all nine current complete-stack profiles. These are final coverage
obligations under the canonical scheduling/reuse rules, not a requirement
to restart unchanged full suites after each edit.

Package/build gates follow the enclosing contract; this case changes neither
packaging nor a native ABI. The
[scoped mypy gate](../../docs/runbooks/typecheck.md) does not currently cover
the GTK pointer adapter, and a green result there cannot prove this event
ownership boundary. Source, selection, image and named-result identities
belong in the ignored cycle ledger rather than this architectural README.
