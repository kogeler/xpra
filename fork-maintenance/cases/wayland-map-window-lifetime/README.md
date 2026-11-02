# Revalidate Wayland map ownership after compositor dispatch

Code: the `Fork-Case: wayland-map-window-lifetime` commit on `develop`
(`make -C fork-maintenance case-show CASE=wayland-map-window-lifetime`).

## Boundary

A `window-map` packet resolves a live Wayland window and surface, applies the
client properties and resizes the surface. Its compositor flush is an event
boundary: `Compositor.flush()` invokes `dispatch_pending()`, which can deliver
the surface's destroy event before returning. The model can consequently be
removed from the server maps while the packet handler still holds its Python
reference. Calling `refresh_window()` afterward reaches the generic reverse
mapping lookup and raises `KeyError: WaylandWindow(0x0)`.

The map handler must refresh only the same model still registered under its
window ID after dispatch. A retained Python reference is not proof of current
registration. A removed or replaced model needs no map-time damage. A current
model still receives its ordinary refresh after properties, resize and flush.

## Embedded-source context

The embedded source remains `646b712d1bff46b08b8f26ddc7a83229d65ba5bc`.
Antoine Martin's `34484a2332` moved the Wayland map handler into its subsystem
with the resize, compositor flush and unconditional refresh already present.
The current compositor explicitly dispatches its event loop from `flush()`;
its `processing_events` guard prevents recursive native dispatch but does not
retain models after a destroy event. Generic `WindowServer.refresh_window_area()`
indexes the managed-window map, an older upstream behavior inherited from
`39b7fc7bc3`. Both actual maintainer diffs and current callers were read.

Repeated SSH detach churn exposed the missing registration check. The
packet-handler error boundary contained the exception, and visible rendering
and teardown still completed. The strengthened positive live peer-log check
correctly rejected those results. This is an older upstream event-reentrancy
bug, separate from the fork's FakeClient interface regressions. No upstream
approval is claimed. Retire this case when the map route retains or revalidates
model ownership across native event dispatch and preserves current-model damage.

## Surrounding code and ownership map

| Component | Ownership |
| --- | --- |
| `wayland/server/subsystem/window.py:WaylandWindowServer` | Resolves packet IDs and owns ordering of client properties, native resize, flush and initial refresh. |
| `wayland/server/compositor.pyx:Compositor` | Dispatches pending Wayland events and flushes clients; a flush may synchronously destroy a surface. |
| `wayland/server/models/window.py:Window` | Owns the Python model, geometry and managed lifetime; its object may remain referenced after removal. |
| `server/subsystem/window.py:WindowServer` | Owns both model/ID maps and real removal, refresh and damage fanout. |
| Window connection | Owns per-client source damage and terminal removal; receives no damage from an obsolete map continuation. |
| Packet dispatcher | Contains unexpected handler exceptions but does not extend model ownership. |

The event loop and map handler run on the UI thread. This requires validation
at a reentrant call boundary, not a new thread lock. Native dispatch must still
run; deferring it or retaining a destroyed model would change compositor policy.

## Mechanism and lifecycle

After `compositor.flush()` returns, the handler resolves `get_window(wid)` again
and compares model identity. It calls the existing `refresh_window(window)`
only when that lookup is the captured model. The existing maps are authoritative;
no second lifetime counter, weak-reference registry or optional API is added.

The active route retains properties-before-resize-before-flush-before-refresh.
A destroyed window simply finishes its now-obsolete map handling without
creating more damage. Identity comparison also prevents a model later occupying
the same ID from authorizing refresh of the old object. The check does not map,
resize or damage a replacement on behalf of the obsolete request.

Exceptions from live-model refresh, property negotiation, resizing or compositor
dispatch retain their existing paths. The fix catches no exception and does not
reinterpret a real rendering or transport failure as success. No application,
codec, driver or packet timing special case is introduced.

## Case-commit and integration ownership

This independent case owns the post-dispatch map continuation and its focused
regression module. The current upstream base contains the handler, both model
maps and the real model/removal APIs, so no dependency is required. Independence
and removability are checked with `case-check`.

`wayland-initial-window-state` separately relies on map-time properties and
refresh to release its CSC-readiness barrier. The live-model regression and
existing window module preserve that order. WSSO owns native surface topology
and snapshot lifetime; this case changes neither native destruction nor its
source cleanup. The timer and video-pipeline cases continue owning terminal
source resources. Notification detach has a similar ownership principle but
a different owner and is kept in its own case.

## Commit scope and non-goals

Only the Wayland map continuation is changed. A generic refresh guard would
also affect X11, shadow and desktop callers; some derived methods perform their
own mapping or overlay operations after `super()`. That broader behavior is not
needed for this demonstrated event boundary and is not silently changed here.
No global packet dropping, generic `KeyError` suppression, retry, timer,
registration resurrection or source reference retention is added.

## Regression design and clean control

`unit.wayland.map_lifetime_test` constructs the real Wayland window subsystem
and real `Window` model, registers it through `do_add_new_window_common()`, and
invokes the real map handler with a `Packet`. The compositor's flush callback
synchronously calls the actual `do_remove_window()` and unmanages the model.
The refresh and reverse-map lookup are not replaced. The request must finish
without damage for the removed model, including when a replacement is registered
under the same ID. Actual removal notifications remain observable.

A separate positive control leaves the model registered and requires exactly
one real refresh fanout to the window connection after resize and flush. The
connection is a side-effect spy; no pixels or successful outcome are fabricated.
The surface/compositor stand-ins control native event timing; model construction,
registration, removal, packet parsing, map handling and refresh are production
code. Direct imports require the real compiled Wayland modules rather than
skipping an unavailable subject. The named focused selection includes the native
Wayland build gate. The tests-only clean control reaches the same reverse-map
`KeyError`, without depending on any new method or field.

This deterministic control proves the Python lifetime boundary, not wlroots
scheduling, physical input, every surface role or a transported compositor.
Existing Wayland window and generic window subsystem modules remain selected,
and native, compiled, compatibility and package-backed live checks close the
corresponding execution gaps.

## Durable live or package boundary

`live-xpra-detach` creates and destroys real application windows while SSH
clients reconnect during churn. This produced explicit `window-map` packet
handler errors in the complete server stderr even though the scenario rendered
and its first boundary passed. Positive acceptance therefore requires the full
peer-log oracle as well as pixels and process cleanup. Both endpoints use the
complete current stack and exact validated Ubuntu server/Debian client DEBs.

Continue the live loop from this earliest failing detach gate after the fix,
finish the remaining profiles, then require one complete nine-profile pass
without a fix and `live-suite-check`. Package imports and source-level tests do
not substitute for that native event-dispatch route. Runtime identities and
results remain in the ignored session ledger.

## Invariants not to simplify

- Treat compositor flush as a native event-dispatch boundary.
- Refresh only the same model still registered after dispatch.
- Preserve live-model property/resize/flush/refresh ordering.
- Keep generic damage ownership and native surface destruction unchanged.
- Reject caught packet-handler errors in positive peer logs despite rendering.

## Required validation

Run the meaningful tests-only control and positive focused case/stack modules,
native Wayland checks, all three full upstream legs, offline fork controls and
both matching DEB builds. Complete the live fix-and-continue loop from detach,
then one full nine-profile pass with exact packages and complete peer-log checks.
