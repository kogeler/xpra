# Paced Wayland empty-damage acknowledgement

## Boundary

A mapped Wayland client may request another frame callback while committing no
new buffer damage. While at least one live, unsuspended Xpra consumer can see
that window, Xpra must eventually acknowledge the callback, but it must not do
so synchronously from the same compositor event dispatch. A client that commits
again from every callback can otherwise continuously re-arm both sides of that
dispatch and delay unrelated input packets.

This case owns a shared, bounded, coalescing acknowledgement timer for ordinary
non-composite mapped toplevels. Empty commits join that timer only while a live
consumer can observe the window. Real damage, visibility changes, consumer
replacement, unmap, destruction, and terminal cleanup move the callback back
to the normal damage owner or remove only the timer entry that is no longer
valid. The resulting event-loop boundary preserves frame-callback liveness
without allowing an empty-commit producer to monopolize compositor dispatch or
delay unrelated pointer and keyboard work.

The server methods `schedule_empty_damage_ack()`,
`cancel_empty_damage_ack()`, and `cancel_empty_damage_acks()`, together with
the model's `mark_wayland_damage_frame_pending()`,
`clear_wayland_damage_frame_pending()`,
`has_wayland_damage_frame_pending()`, and
`acknowledge_empty_changes()`, form the ordinary-root composition seam. They
are intentionally named, externally callable methods rather than private
details of `commit()`: the authoritative surface-tree adapter supplied by
`wayland-subsurface-stream-ownership` uses this seam whenever its root remains
an ordinary non-composite toplevel. WEDT still owns all timer and guard state;
WSSO owns only the classification which decides whether ordinary ownership is
applicable.

## Embedded-source and upstream boundary

The ordinary mapped-empty liveness branch entered upstream through
[Xpra-org/xpra#5002](https://github.com/Xpra-org/xpra/pull/5002), authored by
`kogeler` and squash-merged upstream as
[`a11b97fc02be`](https://github.com/Xpra-org/xpra/commit/a11b97fc02be0172ec6dc169f6bb0b936dba5663)
on 2026-08-19. That branch establishes that a mapped empty commit still needs
an acknowledgement because it does not enter the ordinary delayed
damage/batching path. `models.window.Window.acknowledge_changes()` performs the
actual `frame_done` and Wayland-client flush synchronously, so this case keeps
that mechanism but gives its empty-commit caller a separate scheduled
ownership boundary.

The embedded source therefore contributes the liveness decision and common
acknowledgement primitive; this patch contributes pacing, coalescing, consumer
selection, and terminal timer ownership. Damage, unmap, or destruction cancels
only a scheduled empty-ack entry. Any already queued native callback remains
pending until the normal damage/visibility owner or surface destruction
settles it. On an upstream refresh, equivalent behavior must preserve both
halves rather than merely retaining either the immediate branch or the timer.

## Surrounding code and acknowledgement ownership

This behavior crosses the native Wayland surface, generic Xpra window, and
per-client compression layers. `WaylandWindowServer.commit()` classifies the
commit, but scheduling, consumer eligibility, acknowledgement, and cancellation
belong to different owners.

| Layer | Relevant responsibility |
| --- | --- |
| `xpra/wayland/server/surface.pyx` | Reads `wlr_surface.buffer_damage`, captures the mapped surface, emits `surface-image`, and then emits the generic `commit` with damage rectangles and subsurface geometry. |
| `xpra/wayland/server/wayland_surface.pyx` | Implements synchronous `Surface.frame_done()` by calling `wlr_surface_send_frame_done()`. It does not schedule a future callback. |
| `xpra/wayland/server/subsystem/window.py` | Updates the Python window model, classifies ordinary mapped toplevel commits as damaged or empty, fans real damage out to Xpra consumers, and owns the shared empty-ack timer. A composed WSSO tree has separate root/child completion owners. |
| `xpra/server/subsystem/window.py` and `xpra/server/source/window.py` | Route a damage rectangle to every eligible client connection and its `WindowSource`; the generic source also suppresses pixels for windows hidden from a client's `sharing=combine` display area. |
| `xpra/server/window/compress.py` | Owns normal batching and backlog decisions. `WindowSource.send_delayed_regions()` acknowledges the Wayland surface on the UI thread before extracting and encoding delayed regions. |
| `xpra/wayland/server/models/window.py` | Is the common acknowledgement boundary reached both by normal `WindowSource` work and by the empty-damage timer. The damage guard therefore belongs here, not only in the Wayland server timer. |

The ordinary non-composite toplevel flow is:

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

With WSSO composed, `commit()` accepts two deliberately distinct shapes. The
legacy `list` remains the standalone ordinary Wayland route above. An
authoritative `tuple` contains the root marker and enters WSSO topology
reconciliation before acknowledgement ownership is selected:

```text
authoritative root-only commit
  -> empty and no successful repair: schedule_empty_damage_ack()
  -> explicit damage or successful full repair:
       cancel_empty_damage_ack()
       mark_wayland_damage_frame_pending()
       retain normal WindowSource acknowledgement ownership

authoritative root with active children
  -> cancel_empty_damage_ack()
  -> redirect connection damage into WSSO composite transactions
  -> WSSO acknowledges the native root commit exactly once
```

A source which is refused or whose reconciliation fails is handled for packet
routing, but that is not a successful repair and cannot manufacture an
ordinary damage acknowledgement owner. A root-only empty generation therefore
still delegates to the WEDT timer when no source received repair damage.
Conversely, an actual repair remains on the normal damage path and is protected
by the WEDT guard until `WindowSource` acknowledges it. Per-root reconciliation
sets are kept by WSSO; a repair or refusal for another affected root has no
bearing on this root's timer or guard.

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
consumer. This case must not redefine Xpra's existing unmap or
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

The hidden-window predicate is part of the current generic
`WindowsConnection.damage()` boundary for `sharing=combine`. Native Wayland
currently rejects that sharing layout, while the seamless X11 server is its
supported producer, but the shared consumer predicate must still remain
aligned. This avoids an invisible-render loop if Wayland gains that layout,
and the fire-time recheck covers a connection whose visibility changes after
the timer was armed.

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

## Separate surface and composite paths

The timer applies only to the generic toplevel `Surface.commit()` path.

- Popup native commits use the popup-specific `popup_commit()` and
  `surface_image()` handling. This case does not route them through the generic
  empty timer; their completion semantics are owned elsewhere in the queue.
- With this case selected alone, a native `Subsurface.commit()` emits its
  dedicated image event rather than the generic toplevel `commit`; its facade
  is not an ordinary toplevel model and has no empty-damage guard.
- With `wayland-subsurface-stream-ownership` composed, a child emits one atomic
  `subsurface-commit` and WSSO completes that native child callback exactly
  once after installing or invalidating the generation. A tuple-backed root
  with active children is likewise acknowledged once by the WSSO root owner
  after interception, independently of peer count. Neither composite route
  enters this shared empty-ack timer.
- A tuple-backed root with no active child is still an ordinary root. Its empty
  generation delegates to `schedule_empty_damage_ack()`; its real or repaired
  damage cancels that entry and joins the model guard. Tuple authority changes
  topology provenance, not WEDT's ordinary-root ownership.
- A toplevel commit still updates the geometry of its listed subsurfaces before
  taking the empty branch.

Do not spread the generic timer or model methods to popups, child surfaces, or
active composite roots. Their superficially similar commit events have
different callback owners and completion points.

## Patch-queue and test integration traps

This case has no semantic dependency on another production fix and must apply
and test standalone. The maintained `develop` series places it after
`wayland-subsurface-stream-ownership`, `wayland-initial-window-state`, and
`wayland-client-keymap-sync`. All four cases touch nearby clean-base
context:

- `wayland-initial-window-state` changes the same Wayland model, subsystem, and
  `window_test.py`, adding `WaylandWindowServerFrameStateTest`;
- `wayland-client-keymap-sync` changes `_focus` immediately after configure and
  adds `WaylandWindowServerFocusTest` to the same test module.
- WSSO changes native root/child commit routing, topology reconciliation,
  composite-root acknowledgement, and the same model/subsystem/test paths. It
  must preserve this case's ordinary non-composite timer and damage guard while
  keeping composite-root and child completion in its own state machine.

The maintained patch therefore intentionally uses one line of Git diff context.
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

Low-context application can remain textually valid while placing new methods
inside the preceding `WaylandWindowServerFrameStateTest`. Apply/reverse checks
therefore establish patch mechanics, not Python test ownership. Keep
`WaylandWindowServerEmptyDamageTest` as a separate class, run the
focused/native test through the fully applied stack, and inspect discovery or
the applied class order whenever adjacent context changes.

The subsystem unit tests call unbound real `WaylandWindowServer` methods on a
mock server. New helper methods are otherwise created as permissive child mocks
and may silently test nothing. The test setup explicitly bridges each real
helper with `side_effect` and emulates model guard state with a closure; every
window mock, including secondary windows, needs that setup.

The same fixture explicitly sets
`server._direct_subsurface_children.return_value = ()`. This is the topology
contract of a WEDT unit case: every tested window is an ordinary root with no
active WSSO child. Leaving that attribute as an unconstrained `Mock` would let
the composed `unmap()` / teardown methods observe invented child state or fail
while iterating it, so it would no longer test the timer boundary. The empty
tuple does not emulate WSSO reconciliation; WSSO's own focused module covers
authoritative tuples, root-only delegation, repair, refusal, and composite
acknowledgement.

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

The production patch owns only the ordinary non-composite Wayland toplevel empty-damage
acknowledgement schedule, the model guard needed to protect normal damage, and
the focused regression. It must retain callback liveness for a visible
consumer, avoid synchronous compositor feedback, coalesce repeated work, and
leave the normal `WindowSource` acknowledgement path authoritative while the
guard is set.

The case also owns the public ordinary-root scheduling, cancellation, pending,
and atomic empty-ack methods used by adjacent adapters. It does not own the
adapter's topology classification. WSSO may call this API for an authoritative
root-only tuple, but it may neither duplicate the timer nor transfer composite
root or child completion into it.

It does not:

- remove the pre-classification texture capture/readback;
- change generic damage batching, encoding, or client draw acknowledgements;
- redefine popup, WSSO composite-root, child-surface, multi-`WindowSource`,
  unmap, or configure semantics beyond the guard and flush-preserving calls
  described above;
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
- Do not remove the model damage guard or mark it after ordinary explicit
  damage fan-out. A WSSO full repair which is enqueued during reconciliation
  must still acquire the guard before its delayed `WindowSource`
  acknowledgement can run.
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
- Do not route a WSSO composite-root or child completion through the ordinary
  empty-damage timer; preserve its single compositor-owned acknowledgement.
- Do not treat an authoritative tuple as composite merely because of its type:
  a root-only tuple must retain the WEDT schedule/cancel/guard contract.
- Do not let a refusal or failed repair masquerade as normal damage ownership,
  or let another root's reconciliation state suppress this root's timer.
- Do not trust patch apply/reverse checks without running the applied stack's
  actual focused and native tests.

## Required validation

Follow [development and final acceptance](../../docs/runbooks/validation.md)
and the current isolated-workspace and upstream/live runbooks rather than
using host source or ad hoc evidence. Run `unit.wayland.window_test` first in
tests-only mode to prove the synchronous clean-source failure, then with the
standalone case after each atomic scheduler change. Include affected upstream
modules, the standalone native `wayland` boundary, and composed focused/native
checks when the WSSO commit adapter or another shared interface is involved.

The `live-rgb` regression must finish positive through the frozen-input,
bounded-payload harness with its exact application event stream, input deadline,
lifecycle, and owned cleanup evidence. Run it early after the relevant
focused/native checks, without a full-matrix prerequisite. After candidate
freeze, fill only missing or invalidated final requirements, including current
quarantine, all three full legs, and the seven fixed positive stack profiles
so this scheduler is also exercised alongside the other maintained rendering,
keyboard, detach, fault, Vulkan, and OpenGL paths.
