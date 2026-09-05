# Window-source timer lifecycle

## Boundary

Every GLib timeout owned by a `WindowSource` is a leased asynchronous resource.
Scheduling begins before `GLib.Source.attach()` returns a source ID, a callback may
be dispatched before that return is published to Python, and terminal
connection cleanup may run from another thread at either point. The source
therefore needs one lifetime which starts before any timer producer is usable
and closes exactly once before the rest of window teardown.

The case gives the generic window source one named lease registry and routes
its damage, refresh, decode-recovery, A/V-delay, and icon timers through that
registry. A callback claims and clears only its own lease before doing work.
Cancellation and terminal cleanup invalidate the same lease identity, so a
late callback cannot clear a replacement and a source attached after close
is destroyed rather than published. Each published lease retains the actual
`GLib.Source`: cancellation destroys that object, never a numeric ID which
dispatch may already have retired. A claimed callback increments a persistent
active-callback count and executes its arbitrary body outside the timer lock;
terminal cleanup waits on the corresponding condition before releasing any
window resources. Once closed, `init_vars()` can reset reusable encoding policy
but cannot reopen timer ownership.

This is a base-window lifecycle case. Codec pairs, queued video images,
video-specific flush/watchdog timers, and `VideoSubregion` timers belong to the
independent `video-pipeline-cleanup-race` case. The one video-source call site
which schedules the inherited `expire_timer` remains part of this case because
leaving a direct assignment there would bypass the base lease contract.

## Embedded-source context

The case resolves against source commit
`212038243d0067b6860ebe7d6953692179ef353f`, embedded in the current `develop`
history. The resulting implementation keeps each public numeric timer slot for
compatibility with adjacent scheduling and diagnostic code, while its named
lease, retained source object, epoch, and terminal state are the ownership
authority. The surrounding
source provides the policies this case preserves:

- damage batching decides when expire, soft-expire, timeout, and may-send work
  should run;
- auto-refresh and decode recovery decide their own delays and repaint scope;
- A/V synchronization computes its target and step independently of timer
  ownership;
- the icon mixin may also be constructed outside a full `WindowSource`; and
- non-optional encode work drains before the established `encode_ended`
  barrier releases encoder callables.

The patch changes ownership and terminal ordering, not any of those policies.
On an upstream refresh, retirement requires equivalent protection for pending
ID publication, stale callbacks, active callback completion, registry/lock
ordering, and exception-safe cleanup. A textual conflict or a newly introduced
timer helper is not by itself behavioral replacement.

## Surrounding code and ownership map

The relevant code crosses a mixin, the generic compression source, one
subclass producer, GLib, and connection teardown:

| Layer | Responsibility |
| --- | --- |
| `xpra/server/window/compress.py` | Owns the timer lock and condition, active-callback count, terminal epoch, named leases, generic timer producers and callbacks, optional packet-registry handoff, and the ordered `WindowSource.cleanup()` tail. |
| `xpra/server/window/windowicon.py` | Owns icon discovery and encode-thread handoff; it borrows the containing `WindowSource` timer API when present and preserves a standalone-mixin fallback. |
| `xpra/server/window/video_compress.py` | Contains a non-video-region path which schedules the inherited generic `expire_timer`; it must use the base lease API even though the caller is a `WindowVideoSource`. |
| `xpra/server/source/window.py` | Removes per-window sources and calls their terminal cleanup. |
| `xpra/server/source/factory.py` | Constructs the dynamic connection type and invokes selected mixin cleanup in reverse order; VPC owns exhaustive error handling at that separate connection-composition boundary. |
| `xpra/server/source/client_connection.py` | Owns the base connection close event and encode-queue tail; VPC extends exact sentinel ownership there, and this file does not own dynamic mixin traversal. |
| GLib main context | Assigns numeric source IDs, invokes one-shot callbacks, and removes published sources. Its dispatch timing is not serialized with Python producer threads. |
| Xpra encode queue | Receives work after some timer callbacks. Timer closure prevents new handoffs; the existing non-optional encode barrier remains the final ordered tail. |

The steady-state path is:

```text
producer
  -> reserve named lease under _timer_lock
  -> mark the public timer slot pending
  -> construct GLib timeout Source and install the wrapped callback
  -> attach Source to the default main context
  -> publish Source and returned ID if the same epoch and lease are still active

wrapped callback
  -> require exact lease identity
  -> defer the body until source-ID publication when dispatch raced attach()
  -> reject a closed/stale epoch
  -> remove its lease and clear only its matching public slot
  -> increment the active-callback count under _timer_lock
  -> run the one-shot body without _timer_lock
  -> decrement the count and notify waiters in finally

cleanup
  -> deactivate optional connection packet ownership without _timer_lock
  -> close the epoch and detach every lease under _timer_lock
  -> destroy all already-published GLib Source objects
  -> wait on the condition until every claimed callback has returned
  -> execute every remaining cleanup step and queue the encode-ended barrier
```

The numeric attributes remain available because adjacent Xpra code and
diagnostics use zero/nonzero timer state. They are mirrors of the lease
registry, not the ownership authority.

## Construction and terminal state

`WindowSource.__init__()` establishes the non-reentrant `_timer_lock`, its
`_timer_condition`, `_active_timer_callbacks`, `_timer_epoch`,
`_timer_closed`, and the empty lease map before calling the icon mixin or the
repeatable `init_vars()` hook. This order matters for subclasses:

- Python dispatch can call an overridden `init_vars()` during construction;
- a subclass may add reusable policy fields there;
- neither an override nor later teardown-time `init_vars()` may replace the
  lifetime lock or clear the terminal bit; and
- all public timer slots exist before either the icon path or generic source
  can inspect them.

The initializer is deliberately idempotent only for construction support. It
does not interpret an existing closed lifecycle as a request to create a new
one. A cleaned `WindowSource` object is terminal and is never a reusable
session object.

`init_vars()` continues to reset negotiated encodings, batching state,
regions, counters, and other reusable fields used by the established cleanup
path. It invokes the one-time lifecycle initializer only to support the small
test and subclass construction patterns already used in the tree. It cannot
reset `_timer_closed`, replace `_timer_lock`, decrement the epoch, or recreate
leases.

## Lease publication state machine

A named timer has four externally meaningful states:

| State | Public slot | Lease map | Permitted transition |
| --- | --- | --- | --- |
| Absent | `0` | no entry | A live producer may reserve one lease. |
| Publishing | internal negative sentinel | exact lease with `source_id == 0` | The callback temporarily stays registered, publication commits the positive ID, or cancel/close invalidates the lease. |
| Published | positive GLib source ID | the same lease with its ID and retained `Source` | Exact callback claim, exact cancellation, or terminal close. |
| Terminal | `0` | no entries and `_timer_closed` true | No producer may schedule; `init_vars()` cannot change this state. |

The generic registry never calls `GLib.source_remove()`. Cancellation or close
of a publishing lease clears the public slot but leaves the not-yet-published
source with its producer. The producer constructs it with
`GLib.timeout_source_new()`, installs its callback, and calls `attach(None)`
outside the timer lock. It retains that exact object even while attachment is
in progress. When `attach()` returns, the producer commits the source and ID
only if the exact lease, epoch, and live state still match; otherwise it
destroys its local source outside the lock. A canceller never destroys an
unattached source under the attaching producer.

After publication, cancellation detaches the lease under the lock, then calls
`Source.destroy()` outside it. An already-dispatched stale callback can return
`False` in between those steps. Destroying the same retained source remains
valid after GLib has completed it; looking up its old numeric ID does not.
This also covers cancelled dispatch before attachment returns without keeping
a stale callback artificially alive. Source IDs remain diagnostic mirrors,
not a capability to remove some future source with the same ID.

An exception during source construction or attachment removes only the
reservation made by that call, restores its slot to zero, and destroys any
created source. It does not disturb a later lease. The exception remains
visible to the caller.

Only one lease may occupy a named slot. A producer which finds the slot leased,
or finds the source closed, returns without creating another GLib source. This
preserves the existing coalescing behavior of the individual timer families.

## Early dispatch and callback identity

Although most Xpra timeout producers execute on the UI thread, some delays are
calculated or requested from worker-owned paths. The real GLib main context can
dispatch a zero-delay callback before the attaching producer publishes its ID.
The implementation therefore handles this ordering:

1. the producer reserves lease `A` and calls GLib;
2. GLib invokes `A` before returning its numeric source ID;
3. the wrapper sees that `A.source_id` is not published and returns `True`,
   keeping that GLib source alive;
4. the producer publishes the returned positive ID, unless `A` was cancelled
   or the source closed in the meantime; and
5. a later dispatch either claims `A` once or observes it stale.

Returning `True` is confined to this pre-publication handshake. The wrapped
application callback is always one-shot even if its legacy return value is
truthy. Before invoking it, the wrapper removes the lease and clears the
matching slot. This preserves callbacks which intentionally re-arm their timer
by calling the normal producer again.

Lease object identity, not merely a slot name or source ID, separates a stale
callback from its replacement. A cancelled callback retained by a racing
dispatcher therefore returns without clearing, invoking, or cancelling the
new lease.

Claim, slot clearing, and active-count increment are one locked transition.
The wrapper then releases `_timer_lock` for the callback body, so that body may
re-arm its own slot and may enter connection packet publication without
nesting the two ownership locks. Its `finally` block reacquires the condition,
decrements the count, and wakes terminal waiters when the last callback exits.
A cleanup running in another thread closes the generation and detaches any
replacement, then waits for that count to reach zero before it tears down the
window. Exceptions from the callback remain visible to GLib after the count is
balanced.

## Generic timer families

The lease registry covers the timer slots whose state is owned by
`WindowSource` plus its icon mixin:

| Slot | Producer and callback role | Rearm behavior |
| --- | --- | --- |
| `expire_timer` | Expires a delayed damage batch when its due time is reached. Both the generic producer and the video subclass's non-video-region delay use this inherited slot. | `expire_delayed_region()` may schedule a later expiry, a soft timeout, or the hard timeout. |
| `soft_timer` | Gives an ACK-constrained delayed region another bounded chance before hard expiry. | One-shot; later damage processing may create another lease. |
| `timeout_timer` | Forces resolution of an excessively delayed batch. | One-shot. |
| `may_send_timer` | Rechecks bandwidth/batch readiness for delayed regions. | The callback may calculate a new delay and re-arm the same slot. |
| `refresh_timer` | Schedules automatic lossless refresh work and may defer itself until its target time. | The callback re-arms through the lease API when it is still early. |
| `decode_error_refresh_timer` | Requests a bounded full-quality refresh after a client decode error. | One-shot and independently cancellable. |
| `av_sync_timer` | Moves the current A/V delay toward its target in bounded steps. | The callback may schedule the next step through the same slot. |
| `send_window_icon_timer` | Batches icon compression before transferring it to the encode queue. | The GLib lease ends before the encode work is queued; a separate queued flag prevents duplicate handoffs. |

Timer callback bodies no longer clear their integer slots directly. The lease
wrapper clears the exact slot before dispatch, which is what makes stale
callback and replacement ordering well-defined.

`refresh_timer` remains covered even though the frozen source has no ordinary
production caller of `schedule_auto_refresh()` at this boundary. The API and
its timer callback are live inherited behavior, and keeping it inside the same
registry keeps inherited and backend-specific callers within terminal
ownership.

## Window-icon handoff

`WindowIconSource` is also used as a mixin boundary, so it cannot require every
standalone construction to be a fully initialized `WindowSource`. Its schedule
and cancel paths detect the lease methods dynamically:

- inside `WindowSource`, the icon timeout is a normal named lease;
- in a standalone mixin user, the established numeric GLib fallback remains;
- the timeout callback clears the fallback slot and marks the icon work as
  queued before calling the encode-thread handoff; and
- the queued flag is cleared by the encode worker, including rollback when the
  handoff itself raises.

The queued flag closes the interval created by lease semantics: the timer slot
is correctly zero while the callback body is running, but another icon update
must not enqueue a second compression job before the first one reaches the
worker.

`WindowSource.cleanup()` now calls the icon mixin cleanup explicitly through
`super()`. That call releases only the icon timer lease; it does not own the
generic timers or the later encode barrier.

## Terminal cleanup ordering

The first call to `WindowSource.cleanup()` deactivates an optional
connection-level damage-packet registration before it enters timer ownership.
That callback is discovered structurally, so this base case remains usable
without the subsurface case. `_cleanup_owner` identifies the thread responsible
for the whole terminal pass, not merely its packet prerequisite. Concurrent
cleanup callers wait until that owner publishes `_cleanup_complete`, so a
caller returning into connection teardown cannot overtake the encode-ended
tail. The prerequisite is additionally marked by `_damage_unregistering`.
A successful call replaces the hook with `noop`; a throwing
call clears only the in-progress prerequisite and deliberately leaves the
terminal transition unclaimed so one waiter or a later explicit cleanup can
retry it. WSSO unregister uses the corresponding active-operation fence: it
prevents new packet/ACK claims, waits for already claimed work, and invokes
source callbacks only after releasing the connection condition.

Cleanup then closes timer ownership before any operation which can cancel
damage, reset fields, or queue final encode work. Calls after completion are
no-ops. The owning call attempts all of these steps in order:

1. unregister optional connection packet ownership outside `_timer_lock`;
2. close the timer epoch and detach every lease atomically;
3. destroy every published GLib source and wait for all claimed callbacks;
4. run the icon mixin cleanup;
5. cancel all damage through the most-derived `cancel_damage()` implementation;
6. log the final encoding totals;
7. reset reusable policy fields without reopening the timer lifecycle;
8. release mmap and batch configuration state; and
9. enqueue the existing non-optional `encode_ended` barrier.

After the packet-registry prerequisite succeeds, cleanup records the first
exception but continues through every later owned step. Additional exceptions
are logged with their tracebacks, and the first one is re-raised after the
encode barrier has been attempted. Retrying `cleanup()` cannot repeat that
committed terminal pass or resurrect timers. The deliberately retryable
packet-registry prerequisite is the only pre-terminal exception: it has not
yet transferred ownership, so treating it as completed would permit timers to
close while packet publication remained live.

Reentry on the cleanup owner's thread returns to that outer owner rather than
waiting on itself. A cleanup request originating inside an active timer body
has a separate rule: it sets `_cleanup_requested` to reject new timer work,
then returns so the body can unwind. Per-thread callback depth includes nested
dispatch; only the last active callback runs the deferred terminal pass after
leaving the active registry. A competing cleanup caller can own that pass and
wait for the callback instead. Interrupts while waiting are retained and
reported after the ownership boundary, not used to release window resources
under a still-running callback.

The request bit remains set until timer closure has committed. If the optional
packet-unregister prerequisite raises during deferred cleanup, its retryable
state does not admit fresh timers or callback bodies in between attempts.
Successful retry closes the same lifetime and clears the request; it does not
create a new timer epoch for use.

Callback failure and deferred-cleanup failure are separate outcomes. The
wrapper always balances the active callback and attempts any deferred terminal
pass. If both fail, it logs the cleanup failure with its traceback and
re-raises the original callback failure with its original traceback. If only
cleanup fails, that failure remains visible. Cleanup therefore neither masks
the callback's error nor turns a failed terminal prerequisite into success.

The encode-ended callback continues to clear encoder functions and schedules
the established UI cleanup. This case does not change queue FIFO policy or
claim that cancelling a GLib timeout cancels work already accepted by the
encode queue.

## Threading and lock order

Timer reservation, callback claim, cancellation, and close use only the
per-source `_timer_lock`. GLib source construction, attachment, destruction, the
optional connection unregister callback, and every application callback body
run outside that lock. The condition uses the same lock only to publish and
wait for `_active_timer_callbacks`. This avoids holding a Python ownership lock
across external source registration/removal or arbitrary packet and encoding
work while lease identity still makes publication exact.

A callback may reserve another generic timer or enter packet publication after
it has claimed its lease and released `_timer_lock`. WSSO claims an ACK owner
and active operation under the connection condition, releases it before
calling `damage_packet_acked()`, then reacquires it only to finish the active
operation. Neither direction carries one ownership lock into the other domain.
Terminal cleanup waits only after it has closed new publication under its own
lock and released external locks.

The timer lock is independent of the video resource lock introduced by
`video-pipeline-cleanup-race`. Inherited generic producers use this API;
video-only timers retain their separate generation registry. Callback bodies
may enter video code only after releasing `_timer_lock`, and video code calls
an inherited timer producer only after releasing its video lock. The locks
must not be aliased and neither case may publish the other case's timer by
writing a numeric slot directly.

## Patch-queue and integration ownership

This case applies to the frozen embedded source without another production
dependency. It owns `compress.py`, the icon mixin, the inherited
`expire_timer` producer in `video_compress.py`, and its focused regression in
`compress_test.py`.

`video-pipeline-cleanup-race` is independently selectable against the same
clean source and adds ownership for video-only timers and queued resources.
The stack orders and composes the two patches explicitly because both touch the
video source. The base case must not absorb codec-pair, B-frame, encode-queue,
or `VideoSubregion` lifecycle changes merely to avoid that overlap.

The complete stack also contains `wayland-subsurface-stream-ownership`. Its
internal `SubsurfaceWindowSource` derives directly from `WindowSource`, so it
inherits this terminal timer lifetime, but active raw composition suppresses
the child's ordinary auto-refresh producer. WSSO separately owns its
connection-level composition idle callbacks and per-stage watchdogs, together
with damage deferral, capture, packet publication, exact client draw-ACK
routing, client staging, topology, native input, composite-root acknowledgement,
and child `frame_done()`/flush. The empty-damage case separately owns ordinary
non-composite toplevel empty-commit pacing. This case owns only the generic
named timer leases present on each source through base inheritance.

Both timer lifecycle and WSSO modify `compress.py`. The timer case owns leases,
callback claims, and terminal timer cleanup there; WSSO owns damage deferral,
raw capture, packet construction/publication hooks, sequence/ACK integration,
and composite metadata.

The pure-Python module is also compiled by the `CYTHONIZE_MORE` upstream leg.
Declare the callback's typed exception locals before its `try` / `finally`,
and keep the deferred-cleanup value reset inside `finally`. This gives Cython
one declaration when it lowers the multiple exits through that finalizer,
without changing Python error priority or cleanup ordering. Python import or
bytecode compilation does not cover this boundary. Use the real
`focused-cython` build and runtime regressions during development, and retain
the complete `full-cython` leg for final integration coverage.

Use the isolated workspace transaction for every patch refresh. Do not
hand-edit `fix.patch`, its digest, or manifest paths. After changing any
overlapping case, prove its standalone ownership and resolve and run the
focused modules through the complete stack.

## Patch ownership and non-goals

The production patch owns:

- one-time construction and terminal closure of generic window-source timer
  state;
- exact named lease reservation, source-object/ID publication, callback claim,
  cancellation, and close;
- active-callback accounting and terminal condition waiting with callback
  bodies outside the ownership lock;
- an optional, exactly-once packet-registry deactivation before timer close;
- migration of all generic timer producers and their inherited video expiry
  producer;
- icon timer integration, duplicate-handoff prevention, and explicit mixin
  cleanup;
- exception-preserving, all-steps-attempted generic cleanup; and
- focused deterministic interleaving coverage.

It does not:

- replace GLib or change timeout durations, batching formulas, refresh policy,
  A/V-delay calculations, or callback results visible to their callers;
- cancel work which has already crossed into the encode queue;
- own video codec pairs, B-frame data, scroll state, A/V image queues,
  video-specific watchdogs, or `VideoSubregion` timers;
- add polling, sleeps, process-global timer state, or application-specific
  branches; or
- change window protocol packets, encoding negotiation, Wayland capture, or
  client presentation.

## Regression design

The focused module retains the upstream encoding-selector tests and adds
case-owned timer-lifecycle tests. The lifecycle group uses deterministic
fake GLib registries and real Python threads. It exercises behavior rather
than relying on source-text inspection:

- a dynamically composed client connection closes while a worker is blocked
  between GLib source creation and Python ID publication;
- a callback dispatched before `Source.attach()` returns remains alive only for
  the publication handshake and is removed after a concurrent close;
- cancellation followed by replacement makes the retained old callback stale
  without clearing the replacement;
- a callback already claimed by the wrapper runs without the timer lock,
  blocks cleanup through the active count, may re-arm, and has that replacement
  rejected or removed by terminal close;
- a stronger synthetic external-lock interleaving can cancel a timer while a
  timer callback concurrently enters packet publication without a lock cycle;
  production WSSO draw-ACK handling releases its connection condition before
  invoking the source callback;
- optional damage-packet ownership is deactivated before timer closure and is
  not invoked twice after success;
- concurrent cleanup calls serialize that packet-registry handoff rather than
  executing it twice; a failing handoff leaves the pre-terminal prerequisite
  retryable and the successful retry owns the only terminal pass;
- competing cleanup callers wait for the entire owned tail, while same-thread
  cleanup reentry and nested callback-requested teardown do not wait on
  themselves;
- a callback-requested close keeps timer admission shut across a failed
  packet-unregister prerequisite and its explicit successful retry;
- callback failure remains the primary exception when deferred cleanup also
  fails, while later cleanup steps and the encode barrier are still attempted;
- interrupted waiters retain callback/resource ownership until the active
  callback or cleanup owner actually completes;
- delayed-region expiry publishes both soft and hard nested timer families
  safely across cleanup;
- the `WindowVideoSource` non-video-region path uses the inherited expiry
  lease;
- icon timeout cancellation prevents a stale encode handoff, while the queued
  state prevents duplicate compression work;
- truthy legacy callback returns and callback exceptions remain one-shot and
  leave their slot reusable;
- source registration failure rolls back its pending lease;
- repeated cleanup and teardown-time `init_vars()` cannot reopen the source;
  and
- an early cleanup exception does not skip batch cleanup or the encode-ended
  barrier.

The focused real-GLib regression additionally uses two actual producer/main-
context interleavings. It cancels once before attachment returns and once
after publication but before destruction, dispatches the cancelled source,
and then releases the producer or destroyer. Both paths must suppress the
body and finish without a stale-ID removal warning. Only the scheduling
barriers are controlled: GLib owns the actual source, dispatch, automatic
retirement, and destruction. Fake registries cannot prove this native
ownership boundary by accepting removal of an already-completed ID.

The tests-only clean control must reach these interleavings and fail because
the lease API or terminal behavior is absent, not because imports, fixture
construction, or unrelated modules fail. The patched standalone module then
proves the atomic case, and the same module through `stacks/develop` proves its
composition with the adjacent video and subsurface cases. WSSO's separate
connection-owned idle/watchdog regressions belong to its focused module and do
not enlarge this lease registry.

## Live boundary

This case deliberately declares no atomic live gate. Its authoritative
boundaries are callback-before-publication, callback-versus-close, stale
callback identity, and exception ordering; the deterministic focused test can
schedule those interleavings exactly, while a live desktop cannot make them
repeatable or prove which side of attachment/publication won.

The complete stack's seven positive live profiles still exercise the resulting
lifecycle under real GLib, Xpra connections, rendering, input, application
exit, detach, transport loss, and hardware video. In particular, detach and
transport loss drive connection teardown, RGB drives delayed damage and icon
paths, and both hardware H.264 profiles exercise the adjacent video source.
Those gates complement the focused ownership proof; none substitutes for its
controlled races.

## Invariants not to simplify

- Do not initialize terminal timer state from repeatable `init_vars()`.
- Do not publish a numeric timer slot before reserving its lease.
- Destroy a retained `GLib.Source`; do not cancel the registry through numeric
  IDs, including positive IDs which may already have completed.
- Do not identify a callback solely by slot name or numeric source ID.
- Do not let a stale callback clear or cancel a replacement lease.
- Do not run the callback body before its source ID has a committed ownership
  outcome.
- Do not hold `_timer_lock` across a callback body, connection-registry hook,
  GLib source attachment, or source destruction.
- Increment and decrement the active-callback count in the same claimed
  lifecycle, and balance it in `finally` even when the callback raises.
- Do not propagate a truthy legacy callback result into GLib repetition; rearm
  explicitly through the producer.
- Do not release terminal cleanup while a claimed callback can still publish a
  replacement.
- Do not let teardown-time `init_vars()` reopen the epoch or recreate timer
  ownership.
- Do not stop cleanup at the first exception before the encode-ended barrier is
  attempted.
- Do not remove the standalone `WindowIconSource` fallback or conflate queued
  encode work with the GLib timer lease.
- Do not move video-only timers or codec resources into this base case.

## Required validation

Follow [development and final acceptance](../../docs/runbooks/validation.md)
with the isolated-workspace and upstream-test interfaces. During development,
retain a non-vacuous tests-only control and run the complete
`unit.server.window.compress_test` module immediately after an atomic timer
edit. Include affected upstream modules and the VPC/WSSO composed regressions
when their shared lifecycle changes. Verify the real compiled callback path
when Cythonization semantics are involved; bytecode compilation is insufficient.

Run the affected native/subsystem boundary if the final path set reaches one;
otherwise the focused module is the narrowest direct regression. Exercise a
relevant admitted stack lifecycle profile early when real shutdown behavior is
under review; this case declares no standalone live gate. At candidate freeze,
ensure standalone and composed focused coverage, then fill the final contract's
missing or invalidated quarantine, full-matrix and stack-live requirements.
Do not repeat the complete set after each timer edit. The final handoff must
bind exact case/stack resolution digests and retain every named
result below `.artifacts/fork-maintenance/`; ad hoc output and a plausible live
shutdown do not replace the deterministic lease interleavings.
