# wayland-empty-damage-throttle

## Boundary

A mapped Wayland client may request another frame callback while committing no
new buffer damage. While at least one live, unsuspended Xpra consumer can see
that window, Xpra must eventually acknowledge the callback, but it must not do
so synchronously from the same compositor event dispatch. A client that commits
again from every callback can otherwise continuously re-arm both sides of that
dispatch and delay unrelated input packets.

The retained Zed reproducer opens a second parented toplevel. While both Zed
surfaces are present, the unmodified server processes about 1,861 empty-damage
commits per second and delays pointer or keyboard packets for multiple seconds.
Once an input packet is scheduled, surface routing, application activation,
and all window-destruction boundaries complete normally.

## Upstream provenance

This case is a corrective follow-up to
[Xpra-org/xpra#5002](https://github.com/Xpra-org/xpra/pull/5002), authored by
`kogeler` and squash-merged upstream as
[`a11b97fc02be`](https://github.com/Xpra-org/xpra/commit/a11b97fc02be0172ec6dc169f6bb0b936dba5663)
on 2026-08-19. That change correctly restored liveness for mapped empty-damage
commits: because they queue no damage rectangle, the normal delayed
damage/batching path never acknowledges the surface and a client can remain
blocked waiting for `frame_done`.

The upstream implementation acknowledged the surface directly inside the
compositor commit handler. `models.window.Window.acknowledge_changes()` sends
`frame_done` and flushes Wayland clients, so a client which empty-commits again
from every frame callback forms an unpaced event-loop feedback cycle. The
embedded source containing #5002 but without this downstream correction can
consequently spend its dispatch capacity servicing empty commits and delay
unrelated input. This patch preserves #5002's liveness guarantee while pacing
and coalescing acknowledgements outside the current commit dispatch. Damage,
unmap, or destruction cancels only a scheduled empty-ack entry; an already
queued wlroots callback remains pending until normal damage/visibility
acknowledgement or destruction settles it.

## Surrounding code and acknowledgement ownership

This behavior crosses the native Wayland surface, generic Xpra window, and
per-client compression layers. Treating only the obvious call in
`WaylandWindowServer.commit()` as the whole feature caused the original review
to miss the important ownership boundaries.

| Layer | Relevant responsibility |
| --- | --- |
| `xpra/wayland/server/surface.pyx` | Reads `wlr_surface.buffer_damage`, captures the mapped surface, emits `surface-image`, and then emits the generic `commit` with damage rectangles and subsurface geometry. |
| `xpra/wayland/server/wayland_surface.pyx` | Implements synchronous `Surface.frame_done()` by calling `wlr_surface_send_frame_done()`. It does not schedule a future callback. |
| `xpra/wayland/server/subsystem/window.py` | Updates the Python window model, classifies mapped commits as damaged or empty, fans real damage out to Xpra consumers, and owns the new shared empty-ack timer. |
| `xpra/server/subsystem/window.py` and `xpra/server/source/window.py` | Route a damage rectangle to every eligible client connection and its `WindowSource`; the generic source also suppresses pixels for windows hidden from a client's `sharing=combine` display area. |
| `xpra/server/window/compress.py` | Owns normal batching and backlog decisions. `WindowSource.send_delayed_regions()` acknowledges the Wayland surface on the UI thread before extracting and encoding delayed regions. |
| `xpra/wayland/server/models/window.py` | Is the common acknowledgement boundary reached both by normal `WindowSource` work and by the empty-damage timer. The damage guard therefore belongs here, not only in the Wayland server timer. |

The normal flow is:

```text
wlroots surface commit
  -> native capture and damage extraction
  -> WaylandWindowServer.commit()
       -> non-empty rects: refresh_window_area()
            -> each WindowSource batches the damage
            -> send_delayed_regions()
            -> Window.acknowledge_changes()
            -> surface.frame_done() + display.flush_clients()
       -> no rects: shared 16 ms timer
            -> Window.acknowledge_empty_changes()
            -> Window.acknowledge_changes()
```

An empty `buffer_damage` region means only that the client supplied no new
buffer damage. It does not mean that no Wayland frame callback is queued, that
no double-buffered surface state changed, or that Xpra may skip the rest of the
commit bookkeeping. The empty branch must remain after toplevel tracking,
colourspace and size updates, and subsurface geometry propagation. The focused
test deliberately verifies that these side effects finish before the delayed
acknowledgement.

The native path currently captures the mapped surface before it classifies the
damage list and emits `commit`. This patch therefore limits callback dispatch
but does not remove empty-commit texture capture/readback. Skipping that work is
a separate optimization with broader scale, viewport, colourspace, and
state-only commit implications.

## The wlroots semantic that requires a damage guard

Wayland frame callbacks are queued on a `wl_surface`; they are not owned
one-for-one by Xpra damage rectangles. `wlr_surface_send_frame_done()` drains
all callbacks currently queued on that surface, including callbacks added by
later commits.

This makes cancelling a timer entry insufficient. Consider this sequence:

1. A mapped commit supplies real damage and enters the normal Xpra batching
   path.
2. Before its `WindowSource` is ready to send the delayed regions, the same
   surface makes an empty commit and queues another frame callback.
3. An empty timer which calls `frame_done` now would drain callbacks for both
   commits and bypass the batching/backlog decision for the damaged frame.

For that reason a mapped non-empty commit cancels its window's empty-timer entry
and calls `mark_wayland_damage_frame_pending()` before the first
`refresh_window_area()`. A later empty commit neither acknowledges nor schedules
while this guard is set. The callback remains in wlroots and is drained by the
ordinary `WindowSource` acknowledgement.

`acknowledge_empty_changes()` performs the managed-state and damage-guard test
together in the model. Do not replace it with a server-side check followed by
an unconditional ordinary acknowledgement: the timer revalidates server state,
then the model must still be the final authority immediately before
`frame_done`.

The guard is intentionally a coarse boolean. It means that a mapped non-empty
compositor commit has entered Xpra's normal damage path; it is not a callback
count, damage generation, viewer count, or client draw acknowledgement. One
ordinary `WindowSource` acknowledgement clears it, matching the pre-existing
multi-consumer semantics. Exact per-generation or per-viewer ownership would
require a coordinated change to the generic `WindowSource` API and must not be
approximated with a counter local to the Wayland subsystem.

## State machine and ordering invariants

| Event | Required transition |
| --- | --- |
| Mapped commit with rectangles | Cancel only this WID's scheduled empty ack, mark its damage guard, then fan out the rectangles. Marking after `refresh_window_area()` is too late. |
| Mapped commit without rectangles | After all normal commit bookkeeping, schedule only when the model guard is clear and an eligible consumer exists. Repeated commits replace the same WID entry. |
| Unmapped commit | Cancel the WID's empty entry, do not acknowledge, and retain any existing damage guard. The native surface path reports no damage rectangles while unmapped. |
| Shared timer fires | Set the timer ID to zero, snapshot and clear the WID-to-window queue, then acknowledge. This ordering permits a callback triggered by `frame_done` to re-arm the next one-shot timer without corrupting the old batch. |
| Empty candidate is dispatched | Require `get_window(wid) is window`, recheck consumer eligibility, and call the model's atomic `acknowledge_empty_changes()`. The identity test rejects a stale strong reference after WID reuse. |
| Normal `WindowSource` acknowledgement | `Window.acknowledge_changes()` calls `queue_frame_done()`, which clears the guard and synchronously sends `frame_done`, then flushes the Wayland display. |
| Client geometry configure | Cancel the WID's empty entry, resize, call `queue_frame_done()`, then perform the existing single compositor flush. Configure completion remains outside the timer. |
| Native unmap | Cancel the scheduled empty entry but retain the damage guard. The current unmap path only marks the model iconic and does not cancel delayed `WindowSource` work or settle wlroots callbacks. |
| Server destroy | Cancel the entry, clear the guard, sever the surface reference, and finish the existing unmanage/removal path. |
| Model `do_unmanaged()` | Clear the guard and managed state so every later empty acknowledgement is rejected. Timer cancellation remains the server lifecycle's responsibility. |
| Server cleanup | Cancel the shared timer and release its strong window references before parent cleanup unmanages the models. |

Despite its name, `Window.queue_frame_done()` does not enqueue asynchronous
work. It synchronously clears the guard and calls `surface.frame_done()`, but
deliberately does not call `display.flush_clients()`. This split lets the
configure handler retain its established `resize -> frame_done -> one
compositor.flush` ordering. Moving a flush into `queue_frame_done()` introduces
a double or prematurely ordered flush; adding another timer there introduces a
second scheduling layer.

Do not clear the guard on unmap and do not substitute
`WindowsConnection.cancel_damage(wid)`. That generic operation drops delayed
regions and timers and invalidates queued or in-flight encodes for the affected
consumer. The corrective patch must not redefine Xpra's existing unmap or
damage-loss behavior merely to make the local boolean easier to manage.
`Window.do_unmanaged()` also clears the guard as terminal model cleanup, but it
does not take over the subsystem's timer ownership.

All commit handlers, GLib timeout callbacks, and normal
`send_delayed_regions()` acknowledgements currently run on the UI/main thread;
the latter asserts this explicitly. The dictionary and boolean consequently do
not use locks. If any of these operations moves to a worker thread, this state
machine must be redesigned rather than assumed thread-safe.

## Timer and consumer semantics

There is one GLib one-shot timer for the server and one dictionary entry per
candidate window. This distinction is important:

- repeated empty commits for one WID coalesce into one entry;
- empty commits from several windows share a wake-up but retain independent
  model guards and identity checks;
- damage, unmap, or destruction of one window removes only that entry;
- the shared GLib source is removed only when the candidate dictionary becomes
  empty;
- clearing the queue before dispatch makes re-arming from a `frame_done`
  callback safe and keeps the feedback cycle paced.

The 16 ms delay mirrors wlroots' current native headless `frame_delay`, whose
integer milliseconds are derived from the nominal 60,000 mHz refresh. This is
an independent GLib rate-limit timer, not a callback synchronized with output
frames; 16 ms is mathematically 62.5 Hz and scheduling may also fire later. It
is policy derived from the present backend, not a Wayland protocol constant or
a guaranteed minimum wait for every window joining an already armed shared
timer. Reassess it if the backend or output scheduling changes.

Consumer eligibility intentionally mirrors the ordinary Xpra window delivery
boundary. A candidate is eligible when at least one `WindowsConnection`:

- is neither closed nor connection-suspended;
- accepts the window through `can_send_window(window)`;
- does not currently hide the window from that connection's display area; and
- has no `WindowSource` yet, or has an existing `WindowSource` which is not
  suspended.

A missing `WindowSource` is eligible because absence is not suspension and
ordinary `WindowsConnection.damage()` would lazily create the source. This also
covers initial-window delivery. Do not add separate idle, readonly, recording,
iconic, ownership, or application-specific tests here unless the generic
damage path adopts the same rule. In particular, `is_idle` changes batching
policy but does not make ordinary window damage ineligible.

The hidden-window predicate was added upstream with `sharing=combine` after
the first version of this patch. Native Wayland currently rejects that sharing
layout, while the seamless X11 server is its supported producer, but the
generic `WindowsConnection.damage()` boundary is shared and now suppresses
damage for such a hidden window. Keeping the empty-callback test aligned avoids
a latent invisible-render loop if Wayland later gains that layout, and the
fire-time recheck covers a connection whose visibility changes after the timer
was armed.

With no eligible consumer the callback is intentionally retained instead of
driving an invisible application at roughly the timer cadence. A newly
connected consumer's
initial full damage and a suspended source's resume refresh use the normal
damage path and can settle the retained callback. A filter change by itself
does not necessarily generate a refresh, so a filtered surface can remain
withheld until an ordinary eligible damage or visibility transition. One
eligible consumer is sufficient because the wlroots callback list belongs to
the shared surface, not to each Xpra client.

A candidate which fails the fire-time identity/consumer check or whose model
rejects `acknowledge_empty_changes()` is not requeued automatically. A pending
damage guard leaves ownership with the normal `WindowSource` path; an unmanaged
model has no remaining liveness duty; a missing consumer waits for a later
ordinary delivery or visibility transition. Adding a retry timer here would
turn the rate limiter into polling and could again drive invisible clients.

## Separate surface paths

The timer applies only to the generic toplevel `Surface.commit()` path.

- Popup native commits use the popup-specific `popup_commit()` and
  `surface_image()` handling. This case does not route them through the generic
  empty timer; their completion semantics are owned elsewhere in the queue.
- A native `Subsurface.commit()` emits `subsurface-image`, not the generic
  toplevel `commit`. Its `SubsurfaceWindow` facade is retained in
  `subsurface_facades`, not `_id_to_window`, and it has no damage-guard or
  acknowledgement API.
- A toplevel commit still updates the geometry of its listed subsurfaces before
  taking the empty branch.

Do not spread the generic timer or model methods to popups or subsurfaces
without a separate lifecycle and acknowledgement analysis. Their superficially
similar commit events do not have the same model ownership.

## Patch-queue and test integration traps

This case has no semantic dependency on another production fix and must apply
and test standalone. The maintained `develop` series nevertheless applies
`wayland-initial-window-state`, then `wayland-client-keymap-sync`, then this
case. Both earlier cases touch nearby clean-base context:

- `wayland-initial-window-state` changes the same Wayland model, subsystem, and
  `window_test.py`, adding `WaylandWindowServerFrameStateTest`;
- `wayland-client-keymap-sync` changes `_focus` immediately after configure and
  adds `WaylandWindowServerFocusTest` to the same test module.

The final patch therefore intentionally uses one line of Git diff context.
When exporting a changed candidate, preserve that through the official
workspace transaction rather than hand-editing `fix.patch`, `patch_sha256`, or
`paths`:

```bash
GIT_CONFIG_COUNT=1 \
GIT_CONFIG_KEY_0=diff.context \
GIT_CONFIG_VALUE_0=1 \
make -C fork-maintenance workspace-update \
  CASE=wayland-empty-damage-throttle WORKSPACE=<owned-workspace>
```

Low-context application has its own hazard. An earlier version applied and
reversed cleanly but placed the new methods inside the preceding
`WaylandWindowServerFrameStateTest` when the complete stack was assembled.
`stack-check` proved only textual applicability and did not detect the changed
test ownership. Keep `WaylandWindowServerEmptyDamageTest` as a separate class
and run the focused/native test through the fully applied stack; inspect test
discovery or the applied class order whenever adjacent context changes.

The subsystem unit tests call unbound real `WaylandWindowServer` methods on a
mock server. New helper methods are otherwise created as permissive child mocks
and may silently test nothing. The test setup explicitly bridges each real
helper with `side_effect` and emulates model guard state with a closure; every
window mock, including secondary windows, needs that setup.

The model tests deliberately obtain the real `Window` class from
`WaylandWindowServer.commit.__globals__` after the module's native import stubs
have been installed. Re-importing the model inside another `patch.dict` block
can cross the PyGObject/native metaclass boundary and fail for a reason unrelated
to this behavior. The production-model test populates `_gproperties`, calls
`setup()`, and then exercises the real acknowledgement methods.

The case declares the native `wayland` gate directly in `case.toml`. Do not
rely on the adjacent initial-window-state case to enable
`--with-wayland_server`; standalone Cython compilation, import, and linkage are
part of this case's boundary.

## Patch ownership and non-goals

The production patch owns only the Wayland toplevel empty-damage
acknowledgement schedule, the model guard needed to protect normal damage, and
the focused regression. It must retain callback liveness for a visible
consumer, avoid synchronous compositor feedback, coalesce repeated work, and
leave the normal `WindowSource` acknowledgement path authoritative while the
guard is set.

It does not:

- remove the pre-classification texture capture/readback;
- change generic damage batching, encoding, or client draw acknowledgements;
- redefine popup, subsurface, multi-`WindowSource`, unmap, or configure
  semantics beyond the guard and flush-preserving calls described above;
- proactively wake a filtered surface without an ordinary visibility/damage
  transition;
- add modal protocol support; or
- special-case Zed, titles, coordinates, floating windows, dialogs, or any
  application identity.

## Regression design

The focused suite is intentionally broader than “empty commit uses a timer”.
It covers active and absent consumers, a consumer disappearing before fire,
pending damage at fire time, recursive re-arming, repeated coalescing,
empty-to-damage and damage-to-empty ordering, independent windows, unmap,
destroy, cleanup, WID reuse, configure ordering, the real production model,
the exact 16 ms policy, and the rule that `queue_frame_done()` does not flush.

Tests-only mode must fail non-vacuously on the embedded clean source by exposing
the synchronous feedback loop. Patched standalone tests establish case
ownership; the same focused test through `stacks/develop` establishes that the
low-context patch still owns the intended classes and composes with adjacent
Wayland changes. The native `wayland` gate then compiles and links the Cython
boundary which mocks cannot cover.

The permanent positive live proof belongs to `live-rgb`. Its generic native
Wayland fixture creates parent and child toplevels, re-arms empty-damage frame
callbacks on both, and requires at least 60 observed callbacks per surface. A
real client-side pointer press/release must reach the child surface within one
shared 3.0-second deadline covering both the blocking input command and marker
wait. The fixture must then exit and both client and server inventories must
prove child teardown.

The fixture's application stream is the authority: exactly four ordered JSON
objects named `ready`, `pressure-ready`, `child-click`, and `exit`, with exact
field sets and types, finite non-negative nondecreasing monotonic-clock
timestamps, frame counts, and click coordinates. Duplicate keys, partial
output, extra/reordered events, stale counts, malformed types, packet-only
observations, nonzero exit, or incomplete teardown fail closed. Raw fixture
output is retained for diagnosis but cannot make an invalid stream pass.
Fork-control mutation tests independently invalidate every acceptance-bearing
classifier/event field and the plausible packet-only case with no application
observation.

## Invariants not to simplify

- Do not restore a synchronous acknowledgement in `commit()`.
- Do not remove the model damage guard or mark it after damage fan-out.
- Do not let an empty timer drain callbacks while normal damage is pending.
- Do not clear the guard on native unmap or cancel generic `WindowSource`
  damage as a substitute.
- Do not flush from `queue_frame_done()` or make that method asynchronous.
- Do not acknowledge when no ordinary eligible Xpra consumer can see the
  window.
- Do not cancel the shared timer while another WID remains queued.
- Do not retain only a WID without checking the exact window identity at fire
  time.
- Do not extend the toplevel solution to popup or subsurface commits by name
  similarity alone.
- Do not trust patch apply/reverse checks without running the applied stack's
  actual focused and native tests.

## Required validation

Follow the current isolated-workspace and upstream/live runbooks rather than
using host source or ad hoc evidence. Run `unit.wayland.window_test` first in
tests-only mode to prove the synchronous clean-source failure, then with the
completed standalone case. Run the standalone native `wayland` boundary, then
repeat the focused and native boundaries through `stacks/develop` to cover the
complete queue. Reassess quarantine as required and run all three full upstream
unit-test legs.

The `live-rgb` regression must finish positive through the frozen-input,
bounded-payload harness with its exact application event stream, input deadline,
lifecycle, and owned cleanup evidence. Before publication, run all seven fixed
positive live profiles required by the fork contract so this scheduler is also
exercised alongside the other maintained rendering, keyboard, detach, fault,
Vulkan, and OpenGL paths.
