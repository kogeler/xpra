# Native Wayland pointer scroll normalization

## Boundary

Xpra wheel packets carry a mapped direction button and a fractional distance
in wheel clicks. Native Wayland must translate that operation into its axis
orientation, surface distance and value-120 units consistently with discrete
wheel buttons 4 through 7. The button already includes the client's inversion
policy; the sign of the accompanying distance must not undo that mapping.

At the embedded source, the precise path forwards the signed Xpra distance
directly to wlroots. That reverses normal vertical smooth scrolling relative
to the discrete path, uses a different surface-distance scale for the same
wheel click, and ignores mapped inversion when choosing the sign. Correct
packet delivery alone does not make that native conversion correct.

This case changes only `WaylandPointer.wheel_motion()`. It looks up the same
`WHEEL_BUTTONS` entry as discrete `click()`, applies its signed surface step
to the absolute click magnitude, and derives the matching value-120 amount.
An unsupported wheel button is rejected before any native axis event.

Each incoming wheel operation is translated independently. This is not a
server-side duplicate filter: two client representations of one physical
step still become two native operations. Event admission belongs to the
separate
[`gtk-client-scroll-deduplication`](../gtk-client-scroll-deduplication/README.md)
case, while packet-error reporting, keyboard state and rendering retain their
own boundaries.

## Embedded-source context

The case resolves against source commit
`212038243d0067b6860ebe7d6953692179ef353f`, embedded in current `develop`.
The native pointer implementation was separated into `pointer.pyx` by upstream
commit `1f5f73ee619`; the client wheel entry points live in the pointer
subsystem after `9d9d1d09d84`. Those existing subsystem boundaries remain
unchanged.

The native device already advertises precise-wheel support and already has a
working discrete-wheel table. Its ordinary button path translates Xpra button
numbers to Linux button codes; wheel presses instead use the axis table and
wheel releases do nothing. The fix reuses that established wheel mapping,
rather than adding a second direction table to the server packet parser.

On an upstream refresh, review both entry points and the caller's units.
An equivalent replacement must preserve agreement between a unit precise
wheel operation and one discrete wheel press, including inverted mappings,
both axes and fractional value-120 delivery. A patch that still applies, or
an application that happens to scroll, is not sufficient proof.

## Surrounding code and ownership map

| Component | Responsibility |
| --- | --- |
| `xpra/client/gtk3/window/pointer.py` | Converts admitted GTK smooth deltas or discrete directions into calls on the common client pointer subsystem. |
| `xpra/client/subsystem/pointer.py` | Owns wheel policy, axis/button mapping, delta accumulation, precise packet serialization and compatibility fallback. |
| `xpra/server/subsystem/pointer.py` | Parses wheel packets, applies readonly/source admission, updates pointer/modifier state and selects precise-device or discrete-emulation delivery. |
| `xpra/wayland/server/subsystem/pointer.py` | Establishes native surface focus and flushes compositor output after the common pointer operation. |
| `xpra/wayland/server/pointer.pyx` | Owns the native pointer device, button/axis mapping and wlroots seat notification calls; this case changes its precise wheel conversion. |
| `xpra/wayland/server/compositor.pyx` | Owns the seat, cursor and pointer device used by native clients. |
| wlroots and the native `wl_pointer` client | Convert the seat notification into the protocol events supported by the bound pointer version and deliver them to the focused surface. |
| `fork-maintenance/infra/live/run.py` | Binds real client scroll stimuli to outgoing packets, server axis records, remote GTK movement and visible feedback. |

The two production routes converge at the same native operation:

```text
admitted smooth input
  -> PointerClient.wheel_event()
  -> mapped button + integer thousandths of a click on the wire
  -> PointerManager._process_wheel(): divide distance by 1000
  -> precise device: WaylandPointer.wheel_motion(button, clicks)
       -> table direction * abs(clicks), in surface units
       -> corresponding signed value-120 amount
                                                    \
                                                     -> do_wheel_motion()
                                                    /     -> seat axis notification
admitted discrete wheel input                      /      -> pointer frame
  -> pointer-button press/release                  /
  -> WaylandPointer.click(): press uses wheel table
                             release emits nothing
```

The existing Wayland subsystem then flushes queued protocol output. The
conversion does not create a display, focus a different surface, dispatch
native input recursively or take ownership of compositor publication.

## Wire direction and distance ownership

`PointerClient.wheel_event()` chooses a direction button from the sign of each
accumulated axis delta, then applies `wheel_map`. The precise packet contains
that mapped button and `round(distance * 1000)`. Inversion changes the mapped
button; it does not require the client to rewrite the signed distance too.

For GTK's normal mapping, the adapter negates the vertical GDK delta before
calling the common client subsystem. A downward step therefore commonly
arrives as button 5 with a negative wire distance, whereas native Wayland
expects a positive vertical axis distance for down. Horizontal and inverted
examples make the same ownership distinction visible:

| Incoming mapped button | Click distance | Intended native direction |
| --- | --- | --- |
| 5 | -1 | Down, despite the negative Xpra vertical distance. |
| 4 | -1 | Up after vertical inversion; the button is authoritative. |
| 7 | +1 | Right. |
| 6 | +1 | Left after horizontal inversion. |

The server parser reads the signed wire integer and divides by 1000 before
calling the device. The native method must not divide a second time or treat
the wire integer as surface coordinates. Its distance argument is already a
fractional number of wheel clicks.

The packet's optional `scaled-distance` belongs to the common server's
discrete fallback for devices without precise-wheel support. Wayland's device
reports precise support, so this case uses the original click magnitude and
does not reapply the client's speed multiplier or square-root scaling. Those
policies remain where upstream owns them.

## Native axis and value-120 conversion

The existing table defines one wheel click as `WHEEL_AXIS_STEP = 15.0`
surface units and `WHEEL_DISCRETE_STEP = 120` value-120 units:

| Xpra button | Direction | Wayland axis | Surface distance for one click | Value-120 amount |
| --- | --- | --- | --- | --- |
| 4 | Up | Vertical, 0 | -15 | -120 |
| 5 | Down | Vertical, 0 | +15 | +120 |
| 6 | Left | Horizontal, 1 | -15 | -120 |
| 7 | Right | Horizontal, 1 | +15 | +120 |

For a supported button, the conversion is:

```text
wheel = WHEEL_BUTTONS[button]
surface_distance = abs(click_distance) * wheel.signed_surface_step
value120 = round(surface_distance / WHEEL_AXIS_STEP * WHEEL_DISCRETE_STEP)
orientation = wheel.axis
```

Thus a quarter click has magnitude 3.75 surface units and 30 value-120 units;
two clicks have magnitude 30 and 240. Direction comes from the table in both
cases. The surface-distance value remains fractional; it is not reconstructed
from a rounded count of whole wheel clicks.

`do_wheel_motion()` retains the existing monotonic millisecond timestamp,
`WL_POINTER_AXIS_SOURCE_WHEEL` source and
`WL_POINTER_AXIS_RELATIVE_DIRECTION_IDENTICAL` argument. It submits one axis
notification and then one frame notification. This patch does not reinterpret
the relative-direction protocol field as another inversion switch.

The `discrete` parameter name in the native helper must not be mistaken for a
number of whole clicks: the wlroots boundary receives the signed value-120
amount. The protocol version determines what the client observes. The
regression binds version 5 for whole-step `axis_discrete` delivery and version
8 for `axis_value120`, including quarter steps. It does not claim to create
fractional discrete events for an older client.

The existing `round()` conversion remains the integer quantization boundary.
The patch adds no fractional remainder accumulator, minimum nonzero step or
device-specific acceleration. Very small magnitudes and older protocol
versions retain the behavior of the underlying notification path beyond the
tested conversion.

## Button, focus and lifecycle boundaries

The precise route rejects an unrecognized wheel button with the existing
pointer logger and returns without an axis or frame notification. It must
not silently treat an ordinary button as vertical scrolling. This narrow
lookup is not a replacement for wire field validation or general pointer
admission.

Discrete wheel presses still use `click()` and the same table. Their releases
return without a second operation. Ordinary buttons still use `BUTTON_MAP`,
send their pressed/released state through `wlr_seat_pointer_notify_button()`
and terminate with a frame. The regression's valid button tail checks that
the wheel path has not broken that independent route.

Surface lookup, pointer focus, coordinates, modifiers, readonly policy and
UI-driver selection occur outside the conversion. The Wayland subsystem
flushes after wheel handling in its existing `finally` block. The unit
fixture separately establishes real mapped-surface focus and explicitly
flushes the compositor before waiting for each protocol observation.

No production state is added. The method borrows the device's existing seat
for the synchronous notification call; construction, listener detachment,
pointer constraints, relative motion and terminal device cleanup are
unchanged. There is no new timer, callback, queue or resource-release duty.

## Patch-queue and integration ownership

`fix.patch` changes only `xpra/wayland/server/pointer.pyx` in production. It
adds `tests/unittests/unit/wayland/pointer_scroll_test.py` and its native
`pointer_scroll_client.c` consumer. The manifest has no dependencies and
retains existing server pointer, client pointer and pointer-loopback modules
alongside the new native regression.

The GTK scroll case owns which event representation reaches the wire. This
case owns what one admitted operation means to the native server. They must
remain separate atomic patches: fixing a wrong sign cannot remove duplicated
input, and dropping an emulated event cannot correct a wrong native scale.
Complete-stack tests must prove their composition without weakening either
case's independent clean control.

The packet-handler error case owns exceptions escaping actual handler
execution. It does not turn an invalid wheel button into a supported one,
nor does its bounded logging prove that a native application received the
right movement. The keymap and subsurface cases likewise keep ownership of
keyboard policy and surface-tree input targeting.

## Patch ownership and non-goals

The case does not:

- change wire packet layouts, compatibility aliases or integer field bounds;
- change GTK event masks, XI2 selection or client event deduplication;
- add server-side timing heuristics to identify duplicate wheel operations;
- change client inversion, speed settings, delta accumulation or the common
  non-precise-device fallback;
- introduce touchpad gesture, finger-source, axis-stop or inertia semantics;
- change cursor motion, focus selection, ordinary button mapping or pointer
  constraints;
- equate 15 native surface units with a fixed number of application pixels,
  lines or scroll-widget increments.

The last distinction matters for validation: a correct native unit conversion
and a visible remote response are separate assertions. Toolkit policy may
translate the same axis distance into its own scroll delta; the server must
not tune production input to one fixture's widget scale.

## Focused regression design

`unit.wayland.pointer_scroll_test` compiles the retained C consumer using
`pkg-config`, `wayland-scanner` and the installed Wayland protocol descriptions.
The compiler uses `-Wall -Wextra -Werror`. Missing compiler, protocol files or
native Xpra modules fail the regression instead of skipping its subject.

Each pointer version runs in a fresh Python interpreter with a real
`WaylandCompositor`, Pixman rendering and a private runtime directory. This
prevents adjacent tests' mocked imports or an earlier native registry from
standing in for the compositor. The C client binds a version-5 or version-8
seat/pointer, creates an xdg toplevel, attaches a 64x64 shared-memory buffer
and waits until it is mapped. The fixture enters that exact surface before
injecting device operations.

The consumer records actual `wl_pointer` callbacks as JSON, not calls to a
mocked `do_wheel_motion()`. A compositor flush and client roundtrip delimit
each observation. Readiness and synchronization markers, bounded waits,
checked child exit and cleanup of the client/device/compositor prevent a
missing event stream from looking like an empty successful result.

The three test groups cover:

- **Discrete press/release:** all four wheel buttons, both protocol versions,
  exactly one signed axis amount per press, no event per release and an
  observed native pointer enter.
- **Precise magnitude and mapped direction:** every wheel button with both
  positive and negative distances of one and two clicks; version 8 also
  covers positive and negative quarter clicks. The oracle uses the absolute
  magnitude and independently states the expected direction and units.
- **Unsupported wheel and valid tail:** `wheel_motion(1, 1)` produces no
  native events; subsequent ordinary button 1 press/release still produce
  button code 272 with the correct state and frame.

Every accepted wheel observation contains exactly one axis, one wheel-source
event, the appropriate discrete/value-120 event and a final frame. Checking
only an axis value could miss a duplicate or broken protocol grouping. The
oracle uses literal protocol expectations rather than importing the
production `WHEEL_BUTTONS` table it is supposed to verify.

The tests-only clean control invokes the existing public device methods and
must fail on incorrect delivered values. It must not fail because a newly
introduced API is missing. This control proves the native conversion, not
the client packet route: adjacent pointer modules and the live suite cover
the latter independently.

## Durable live boundary

There is no case-selected live endpoint. The complete current queue runs on
both the Xpra server and client in every profile. The Vulkan and OpenGL
hardware profiles exercise smooth Sway-to-Xwayland/GTK client input; the GTK
detach and transport-loss profiles exercise genuine XTEST discrete scrolling.

The shared interaction fixture resolves the remote GTK window independently
by title and checks a sequence of six steps in both axes and directions.
Each physical stimulus, including the first, must produce exactly one wire
operation: a precise wheel packet or an allowed discrete press/release pair,
never both. Server-native axis records must match the expected orientation,
surface distance and value-120 amount.

The remote widget must also report the exact displacement sequence and show
visible feedback after each step. Packet logs alone cannot prove application
delivery; pixels alone cannot distinguish a correct step from two duplicate
operations. Collection reparses complete post-input logs and the final
fixture stream, so late duplicates cannot disappear behind an early capture.

This shared live oracle deliberately does not prescribe the GTK filter's
implementation or require a precise packet for the first X11 valuator sample.
It does require one complete native operation per stimulus. Fractional and
older-protocol value checks remain in the focused native consumer; the six
live whole-step stimuli do not replace them.

The manifest retains `live-rgb` and `live-h264` as behavioral gate metadata.
Those entries neither restrict the topical shared interaction checks nor
replace the mandatory complete nine-profile suite.

## Invariants not to simplify

- Treat the mapped button as direction authority, including client inversion.
- Apply only the absolute click magnitude to the existing signed wheel table.
- Keep surface units and value-120 units consistent with discrete `click()`.
- Do not divide wire distance by 1000 again inside the native device.
- Do not reapply discrete-fallback speed scaling to a precise Wayland device.
- Preserve fractional surface distance and the existing integer rounding
  boundary without inventing whole-step accumulation here.
- Emit one native axis operation and frame per admitted wheel operation;
  wheel-button releases remain silent.
- Leave unsupported wheel buttons without native output and preserve the
  valid ordinary-button tail.
- Preserve focus, flush and device-lifetime ownership outside the conversion.
- Keep native protocol, client event admission and visible application
  behavior as separate regression boundaries.

## Required validation

Follow [development and final acceptance](../../docs/runbooks/validation.md).
After an atomic change, run the manifest's focused modules and the tests-only
clean control in the same frozen image. Exercise actual native Wayland
linkage, both pointer protocol versions, compiled and no-compat modes, and
the composed GTK/pointer tests. A Python mock of the native method or an
import-only check cannot establish the emitted protocol values.

Start `live-all STACK=develop RUN=<fresh-prefix>` after focused prerequisites.
At candidate freeze, fill missing or invalidated focused/native, quarantine,
fork-control and all three full upstream legs. Require `live-suite-check` for
all nine current complete-stack profiles, not a single scroll-bearing live
result. Use the canonical reuse rules rather than restarting unchanged
expensive jobs after every edit.

Package/build gates follow the enclosing validation contract. This case
changes native implementation behavior but introduces no new native ABI or
packaging rule. The [scoped mypy gate](../../docs/runbooks/typecheck.md) does
not type-check this Cython module or its native consumer. Keep exact source,
selection, image and result identities in the ignored cycle ledger; this
README describes the current contract, not a retained test run.
