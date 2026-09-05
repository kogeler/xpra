# Video pipeline lifecycle ownership

## Boundary

Each `WindowVideoSource` owns a native video pipeline and the asynchronous
resources which can create, feed, flush, replace, or retire that pipeline. The
lifetime starts once, before the generic `WindowSource` constructor invokes
repeatable initialization hooks, and becomes terminal when video-source
cleanup begins. Rebuilding encoder callables or resetting negotiated policy
does not recreate that lifetime.

The protected resource set is larger than the current CSC/video pair. It also
includes captured images waiting for A/V delay, the timeout and idle sources
which drain that queue, delayed B-frame flush state, the inactive-encoder
watchdog, scroll state released on the encode worker, the optional saved
bitstream file, the UI picture-refresh request after an x264 reset, and both
refresh timers owned by `VideoSubregion`.

The case gives those resources explicit publication, cancellation, and
terminal transitions:

- a CSC/video pair is constructed locally and published together under the
  video-state lock only while the source is live;
- a cleanup requested outside the encode worker detaches the visible pair,
  queues mandatory cleanup, and performs a worker-side late sweep for a pair
  published by work already ahead of that callback;
- a real encoder-registry refresh invokes that transaction before rebuilding
  callables, while one-time resource fields remain intact;
- each video-only GLib source has a generation and a retained `GLib.Source`
  object so dispatch, publication, replacement, and terminal cleanup have one
  exact identity;
- damage cancellation publishes generic sequence cancellation before it
  transfers queued images, timers, scroll data, and codecs to their cleanup
  owners;
- every sibling cleanup step is attempted even when another step raises, and
  the first error remains visible after later errors are logged; and
- the dynamic connection muxer attempts every selected mixin cleanup even when
  one raises, after which the base client connection owns shared encode-tail
  callbacks and the sole worker sentinel. Accepted per-window cleanup, CUDA
  release, and connection mmap closes precede worker termination. Traversal of
  every individual window source is a separate owner in
  `wayland-subsurface-stream-ownership`; exhaustive mixin traversal alone does
  not make an arbitrary mixin's internal loop exception-complete.

The guarantee is ownership and ordering at the Xpra layer. Codec backends may
release native resources synchronously or asynchronously behind their
`clean()` method. Empty public fields prove that Xpra detached the instances;
they are not a portable assertion that every driver operation completed at
that instruction.

## Embedded-source context

The case resolves against source commit
`212038243d0067b6860ebe7d6953692179ef353f`, embedded in the current `develop`
history. That source already supplies the following surrounding behavior:

- every client connection has one FIFO encode worker shared by its window
  sources;
- native pipeline construction and ordinary pipeline use occur on that encode
  worker;
- UI, protocol, calculation, and GLib callback paths may request cancellation
  or reconfiguration without joining the worker;
- `WindowVideoSource` inherits the generic `WindowSource` suspend, resume, and
  cleanup flow, which reaches video cleanup through polymorphic
  `cancel_damage()`;
- `setup_pipeline_option()` constructs the optional CSC and the video encoder
  lazily from scored codec specs;
- a delayed image in `encode_queue` describes damage and requested coding, not
  a captured native codec instance;
- a closing video flush may yield a final packet which must be preserved before
  the closed encoder is retired; and
- client-connection classes are dynamically composed from source mixins and
  cleaned in the muxer's established reverse ordering; each selected mixin and
  the final base connection retain distinct cleanup responsibilities.

This patch does not replace those policies. It supplies the ownership state
around them. On an upstream refresh, retention is behavioral: the resolved
source must still provide one-time video ownership, atomic pair publication,
exception-complete retirement, exact timer cancellation, and the same
encode-tail order. Patch applicability alone is not proof that the case is
still needed or correct.

## Surrounding code and ownership map

The lifecycle crosses connection, generic-window, video-window, GLib, and
backend boundaries:

| Layer | Responsibility |
| --- | --- |
| `xpra/server/subsystem/encoding.py` | Discovers server codec capability and triggers per-connection encoder reinitialization. It does not own per-window native instances. |
| `xpra/server/source/window.py` | Creates and removes window sources, fans configuration to them, and invokes their terminal cleanup. |
| `xpra/server/source/factory.py` | Constructs the dynamic client connection and exhaustively invokes every selected base cleanup in reverse order before reporting the first failure. |
| `xpra/server/source/client_connection.py` | Owns the encode queue, worker, end-callback registry, connection close event, and sole final sentinel. |
| `xpra/server/source/encoding.py` | Owns connection encoding state, background recalculation admission/completion, and publication of the shared CUDA device-context owner. Cleanup stops calculation before window teardown and registers CUDA release in the base-owned encode tail. |
| `xpra/server/source/mmap.py` | Detaches the shared read/write areas from public connection state, then registers their physical close after mandatory window encodes and before the sole worker sentinel. |
| `xpra/server/window/compress.py` | Owns callable encoder registries, generic damage sequence and timers, inherited cancellation, and the final `encode_ended()` barrier. |
| `xpra/server/window/video_compress.py` | Owns video lifetime state, pair construction/publication, A/V-delayed images, video-only timers, scroll transfer, saved stream, EOS intent, and codec cleanup. |
| `xpra/server/window/video_subregion.py` | Owns video-region detection state, video/non-video refresh regions, two GLib sources, their generations, and terminal callback containment. |
| `xpra/codecs/constants.py` and codec specs | Describe factories and capability limits. Weak instance counts are selection/diagnostic data, not deterministic ownership. |
| CSC and video backends | Own the CPU, GPU, driver, surface, bitstream, and backend-worker resources released by their `clean()` implementations. |
| `tests/unittests/unit/server/window/video_compress_test.py` | Drives exact interleavings with real Python threads, events, a FIFO worker, controlled GLib publication, and throwing resource doubles. |
| `tests/unittests/unit/server/source/encoding_lifecycle_test.py` | Exercises the real connection muxer, encode worker, background-calculation completion fence, local CUDA publication, and native GLib calculation handoff. |

Three encoder-related collections must remain distinct:

| State | Contents | Lifetime authority |
| --- | --- | --- |
| `_all_encoders` / `_encoders` | Python callables available for future damage. | Rebuilt by `init_encoders()`; contains no live native context ownership. |
| `WindowVideoSource.encode_queue` | Frozen `ImageWrapper` instances delayed for A/V synchronization. | Video source plus its timeout/idle generations; items later resolve the current callable and pipeline. |
| Connection encode queue | Compression work, scroll release, codec cleanup, `encode_ended`, connection-wide end callbacks, and `None`. | `ClientConnection`; one worker consumes it in FIFO order and only the base connection publishes `None`. |

Clearing one domain cannot stand in for cleaning another. Replacing callable
mappings does not release a codec, detaching a codec does not free delayed
images, and cancelling an A/V timeout does not remove mandatory work already
accepted by the connection worker.

An ordinary toplevel has one `WindowVideoSource`. The independently selectable
`wayland-subsurface-stream-ownership` case constructs its internal
`SubsurfaceWindowSource` directly from `WindowSource` and uses those sources
only as raw RGB32 layer producers for a connection-owned parent-backing
transaction. A child therefore never enters the video lifecycle described
here, never owns a decoder stream, and never needs a child-specific EOS rule.
This case remains authoritative for every actual `WindowVideoSource`, including
ordinary Wayland toplevels used by the complete-stack hardware gates.

## One-time lifetime and repeatable policy

`WindowVideoSource.__init__()` calls `_init_video_lifecycle()` before
`super().__init__()`. This matters because the base constructor invokes the
virtual `init_vars()` method. Lifetime fields must already exist when that
override runs, but they must not be reset by later policy reinitialization.

The one-time fields are:

| State | Meaning |
| --- | --- |
| `_video_state_lock` | Re-entrant ownership lock for terminal state, pair publication/detachment, the delayed-image queue, and video-only timer identities. |
| `_video_source_closed` | Monotonic terminal bit. Once true, constructors and timer producers cannot publish new work. |
| `_video_cleanup_queued` | The terminal pair-cleanup/late-sweep handoff has been accepted by the encode FIFO. Later external cancellation must not enqueue another release behind the final barrier. |
| `_video_refresh_lock`, `video_fallback_refresh_idle`, `_video_fallback_refresh_generation` | Exact x264 fallback UI refresh and completion fence; the refresh body may enter generic damage/timer code without holding the video-state lock. |
| `video_subregion` | One terminal `VideoSubregion` owner, created after base construction and retained until UI cleanup. |
| `_csc_encoder`, `_video_encoder` | The currently published native pair; both are transferred through one locked boundary. |
| `encode_from_queue_timer`, `encode_from_queue_idle`, `encode_from_queue_due` and their generations | Timeout/idle ownership for the A/V-delayed image queue. |
| `b_frame_flush_timer`, `b_frame_flush_data` and `_b_frame_flush_generation` | Delayed video-frame flush ownership. |
| `video_encoder_timer` and `_video_encoder_timer_generation` | Inactive-encoder watchdog ownership. |
| `scroll_data` | Current scroll encoder state, released by ordered worker work. |
| `video_stream_file`, `_video_stream_encoder` | Optional saved-bitstream handle and the exact encoder allowed to close it. |

The base constructor also creates `encode_queue` once. This patch transfers and
drains that list under `_video_state_lock`; `init_vars()` does not replace it.

The repeatable `init_vars()` hook continues to reset policy and derived
selection state: dimension limits, masks, actual scaling, pipeline parameters
and scores, video/non-video encoding sets, fallback mappings, edge coding,
`start_video_frame`, `last_scroll_time`, and the last-pipeline-check marker.
`do_init_encoders()` rebuilds callable mappings and the derived encoding sets,
but it does not write codec fields or timer slots.

This separation permits ordinary reconfiguration without losing a resource
that an earlier encode item can still publish. Terminal UI cleanup may finally
set `video_subregion` to `None`, after the encode barrier has transferred all
work which can use it.

## Encoder registry reinitialization

The inherited encoder initializer remains authoritative:

```text
WindowVideoSource.init_encoders()
  -> if _encoders already exists: video_context_clean()
  -> WindowSource.init_encoders()
       -> WindowVideoSource.do_init_encoders()
       -> parse_csc_modes(...)
       -> update_encoding_selection(..., init=True)
```

Attribute presence distinguishes first construction from a real registry
refresh. During first construction `_encoders` does not yet exist, so the
source creates callables without queuing a meaningless cleanup. On every later
refresh, `video_context_clean()` first detaches the visible pair and queues a
mandatory worker transaction, even if that initial snapshot is empty.

The rebuild does not wait for native cleanup. This preserves the established
non-blocking UI/protocol behavior. Work already executing ahead of the cleanup
callback can finish constructing and publish a replacement pair while the
registry is rebuilt. Because neither `do_init_encoders()` nor `init_vars()`
erases the one-time fields, the queued worker-side late sweep observes and
retires that pair before later queue work proceeds.

The patch does not duplicate CSC parsing, encoding selection, codec discovery,
or callable insertion. A normal registry refresh may legitimately select a
different codec after the old resources have been transferred to cleanup.

## Lazy construction and atomic pair publication

The pipeline remains lazy:

```text
video_encode()
  -> check_pipeline()
       -> do_check_pipeline()
       -> get_video_pipeline_options()
       -> setup_pipeline()
            -> setup_pipeline_option()
```

`setup_pipeline_option()` owns each new object locally until the complete pair
is usable. The optional CSC is created and initialized first. If CSC
initialization or its immediate information query raises, that local converter
is cleaned before the exception leaves the constructor boundary.

The video encoder is then created and initialized. Until publication,
`clean_unpublished_pipeline()` owns both locals with nested `finally`
semantics: converter cleanup is attempted first and encoder cleanup is still
attempted if it raises. Direct `ve.clean()` is used for an unpublished encoder,
because no decoder stream, inactivity timer, saved stream, or EOS side effect
belongs to an instance which never became current.

After successful initialization, the method records the selected limits,
masks, scaling, and first-frame state. It then enters `_video_state_lock` and
performs one publication transition:

```text
if source is live:
    _csc_encoder = local_csc
    _video_encoder = local_encoder
    published = True
else:
    retain local ownership
```

Pair publication and terminal closure therefore cannot observe a half pair. A
constructor which loses to `_video_source_closed` cleans both locals exactly
once and returns `False`. A successfully published pair transfers ownership to
the source. Any later option failure is retired through
`video_context_clean()` rather than through the local guard, preventing double
cleanup.

Unsupported formats and scaling returns which occur before construction retain
their established selection behavior. This lifecycle does not manufacture a
codec candidate or alter scoring.

## Detach, worker cleanup, and late sweep

`video_context_clean()` is the single published-pipeline retirement entry
point. A request made outside the encode worker follows this sequence:

```text
caller
  -> invalidate B-frame flush state
  -> invalidate inactive-encoder watchdog
  -> under _video_state_lock:
       capture pair A
       clear both public fields
       queue mandatory clean(A) before releasing the lock
  -> return without waiting

encode worker
  -> finish work already ahead of clean(A)
  -> attempt cleanup of captured CSC A
  -> attempt cleanup of captured encoder A
  -> perform video_context_clean(encode_thread=True)
       cancel any newly published video timers
       detach pair B published by earlier queued work
       clean pair B directly
```

The callback is queued with `optional=False`. Optional encoding jobs may be
discarded once the connection close event is set; resource cleanup may not.
The closure retains strong references to pair A until the worker reaches it.
Detachment and FIFO insertion are one locked handoff: terminal cleanup cannot
observe the empty public pair and enqueue `encode_ended()` while an earlier
caller still holds detached resources outside the queue. After terminal
handoff succeeds, `_video_cleanup_queued` prevents later external pair or scroll
release requests from landing behind that final barrier. The worker-side late
sweep remains permitted; it is the release owner already accepted by the FIFO.

The late sweep is deterministic because pipeline construction and the sweep
share the same serial encode worker. Every encode item ahead of the cleanup
callback has completed its publication before the sweep runs; no later worker
item can interleave inside it. Polling, a repeated cleanup timer, or a caller
thread join would weaken this ordering rather than extend it.

`encode_thread=True` is an assertion made by code already executing in the
worker domain. It performs cleanup immediately and does not queue another
closure. It is used by direct pipeline invalidation and option-retry cleanup;
it is not a shortcut for UI or protocol callers.

## Cleanup completeness and exception visibility

The captured-pair closure uses nested `finally` blocks:

```text
try:
    try:
        clean captured CSC
    finally:
        clean captured video encoder
finally:
    if request came from outside the worker:
        run the late sweep
```

Consequently a CSC exception cannot skip its paired encoder, an encoder
exception cannot skip the late sweep, and a late-sweep CSC exception cannot
skip its paired encoder. Exceptions remain visible to the encode worker's
normal error boundary. When multiple native cleanup operations raise, ordinary
Python `finally` semantics determine the active exception; this patch does not
invent a backend exception protocol.

Sibling teardown actions which are not one nested resource pair use explicit
aggregation. `_raise_video_cleanup_errors()` logs every error after the first,
then re-raises the first with its original traceback. This policy is used for:

- cancellation of both video-context timer families before pair detachment;
- destruction of multiple retained video GLib sources;
- the complete `cancel_damage()` action list; and
- `WindowVideoSource.cleanup()`, where a `VideoSubregion` error cannot bypass
  generic source cleanup.

The dynamic connection muxer applies the same exhaustive principle at a wider
boundary. It invokes `cleanup()` on every selected base in reverse muxer order,
records each `BaseException`, and reports only after the final base has run. An
ordinary `Exception` is then exposed through the established contextual
`RuntimeError`; a process-control `BaseException` is re-raised with its original
traceback. In either case, a window cleanup failure cannot skip the later
encoding mixin or base connection cleanup which registers CUDA release and
publishes the sentinel.

State is detached before external destruction or backend cleanup is attempted.
A throwing source destroy therefore cannot leave its slot looking live, and a
throwing backend cannot leave its pair published as current.

`ve_clean()` always cancels the inactivity watchdog first. When saved video
streams are enabled, its `finally` closes the file only if
`_video_stream_encoder is ve`. A stale captured encoder cannot close the file
opened by a replacement. EOS is emitted only after encoder cleanup returns
successfully.

## Damage cancellation and terminal cleanup

Video cancellation begins by invoking `WindowSource.cancel_damage(limit)`.
This publishes the generic damage-sequence boundary before ownership of queued
video objects is transferred. A capture which reaches `call_encode()` after
that boundary observes cancellation or terminal state under
`_video_state_lock` and frees its image instead of appending it.

The remaining video-owned actions are attempted in this order:

1. invalidate and remove both the A/V timeout and A/V idle source;
2. invalidate the x264 fallback UI refresh;
3. atomically swap `encode_queue` to an empty list and free every captured
   image from the old list;
4. cancel both video and non-video `VideoSubregion` refresh timers;
5. queue mandatory `do_free_scroll_data()` on the encode worker;
6. clear `last_scroll_time`; and
7. cancel video flush/watchdog state, detach the codec pair, and queue its
   mandatory cleanup transaction.

Each step runs even if a previous one raises. The first failure is reported
only after every later owner has received its cleanup attempt.

`WindowVideoSource.cleanup()` first sets `_video_source_closed` under the
video-state lock. New pair, delayed-image, B-frame, and watchdog publication is
then rejected. It cancels and fences the fallback refresh, terminally closes
`VideoSubregion`, and enters inherited `WindowSource.cleanup()` even if either
earlier cleanup raises.

When composed with `window-source-timer-lifecycle`, the generic cleanup then
unregisters any optional connection packet-publication identity, closes all
generic timer leases, waits for already claimed generic callbacks, runs the
polymorphic video cancellation above, resets repeatable policy, cleans mmap and
batch state, and queues `encode_ended()`. The base timer case makes repeated
generic cleanup idempotent without reopening either lifetime.

For one connection worker the resulting tail is:

```text
work accepted before cancellation
  -> do_free_scroll_data
  -> captured codec cleanup and late sweep
  -> encode_ended
  -> connection-wide end callbacks
  -> None sentinel
```

`suspend()` and `resume()` retain the inherited polymorphic path. They cancel
current video work without setting `_video_source_closed`; a live source may
therefore create a new pipeline after resume. Only terminal `cleanup()` closes
publication permanently.

## A/V-delayed image ownership

`process_damage_region()` may freeze a captured image and delay encoding to
align video with audio. The delayed item carries geometry, capture and queue
times, the image, requested coding, damage sequence, options, and flush state.
It deliberately does not retain a CSC or video-encoder generation. When due,
the normal encode callback resolves and validates the current pipeline.

The final UI-side handoff is guarded by `_video_state_lock`:

- terminal or cancelled work frees its image;
- zero-delay work transfers its image to a mandatory `_encode_or_free_image`
  item while the terminal lease is held; and
- delayed work is appended atomically, then receives an exact timeout.

`free_encode_queue_images()` swaps the list under the lock and frees wrappers
outside it. A concurrent producer either appends before the swap and transfers
its image to the cleanup list, or observes cancellation/closure and frees the
image itself.

An immediate image is no longer owned by `encode_queue` after handoff. Its
mandatory worker item therefore must run even when connection closure skips
optional compression: it either frees the cancelled image or enters
`make_data_packet_cb()`, whose existing `finally` owns release. Marking that
item optional would discard the only remaining release owner. A delayed-queue
wakeup may remain optional because the source still owns its untransferred
images and frees them during cancellation.

The delayed queue owns two GLib identities:

| Source | Role | Identity state |
| --- | --- | --- |
| `encode_from_queue_timer` | Makes the first eligible item available to the encode worker at its A/V due time. | `_encode_from_queue_generation`, retained source object, and `encode_from_queue_due`. |
| `encode_from_queue_idle` | Returns a worker-computed next due time to the UI scheduling domain. | `_encode_from_queue_idle_generation` and retained source object. |

For either source, scheduling reserves a new generation under the video lock,
creates a timeout or idle `GLib.Source`, and attaches it outside the lock using
the small `attach_source()` helper shared with `VideoSubregion`. Publication
retains that object only if the source is live, the queue still exists, damage
is not cancelled, and the generation still matches. The helper owns the object
until attach completes and destroys it if registration raises.

A callback may dispatch before attach returns. It claims its generation and
performs its one allowed handoff; the publisher then destroys its exact local
object rather than publishing a stale owner. GLib may already have destroyed
that one-shot source when its callback returned `False`. Object destruction is
idempotent and remains tied to that instance; a recyclable numeric source ID
is not a safe cancellation identity after this boundary. Numeric IDs are used
only for diagnostics, not for reacquiring ownership from the global context.

Cancellation advances both generations, clears both slots, and attempts both
object destructions even when the first raises. A blocked idle publication
released after cleanup fails its recheck, destroys its own source, and cannot
schedule a timeout or enqueue work. Zero remains the absent-slot value, not a
second kind of live source.

`encode_from_queue()` performs an initial live/non-empty check, then calls the
inherited `update_av_sync_delay()` outside `_video_state_lock`. That inherited
method may schedule a generic timer, so keeping it outside prevents a
video-lock to timer-lock edge. The worker reacquires the video lock, revalidates
terminal state, removes cancelled entries, and transfers at most one due item.
Images are freed and encoding is invoked outside the lock. Remaining work is
rescheduled through the guarded idle path.

If cleanup wins the unlocked A/V-update interval, the worker cancels any
generic A/V timer which the inherited update just published and exits without
touching cleaned video state.

## B-frame flush and inactivity watchdog

The delayed B-frame path owns a tuple
`(encoder, csc, frame, x, y, scaled_size)`, a retained GLib source, and
`_b_frame_flush_generation`. Scheduling replaces the previous generation,
records the tuple under `_video_state_lock`, registers the timeout outside the
lock, and publishes its object only after a live/generation recheck.

The timeout callback must match the exact generation. It clears its slot and,
while holding the terminal-state lease, queues the established optional
`do_flush_video_encoder(worker_generation, captured_tuple)` work. Both the
exact tuple and its generation cross the UI-to-worker boundary; the worker
must never reinterpret an older request using a replacement tuple. A later
encode, replacement, or cancellation invalidates that queued request. This
short locked handoff ensures connection
cleanup cannot publish the worker tail between the callback's live check and
its queue insertion. If connection close has already made optional encoding
work skippable, the later mandatory codec cleanup remains authoritative. Native
flush work does not execute under the lock.

`flush_video_encoder_now()` invalidates only the timer identity, retains the
flush tuple, and performs the same guarded worker handoff. Full cancellation
invalidates the generation, clears both source and tuple, and destroys the old GLib
source.

`do_flush_video_encoder()` preserves the existing stream behavior:

- it ignores a tuple whose encoder is no longer current;
- it packetizes and queues valid data returned by `flush()`;
- a closed encoder's final data is written, flushed, and queued before context
  cleanup runs in `finally`;
- supported multiple delayed frames may re-arm the flush generation; and
- an open encoder with no more delayed frames receives the inactivity
  watchdog.

The special x264 frame-zero path retires the whole current CSC/video pair
through direct `video_context_clean(True, send_eos=True)`. Its subsequent
`novideo` refresh has its own cancellable UI source and generation.
`_video_refresh_lock` fences a claimed callback through the complete `refresh`
body, while `_video_state_lock` protects only the short identity claim. Thus
terminal cleanup waits for a claimed refresh without holding the video-state
lock across generic damage and timer scheduling.

The watchdog has its own `_video_encoder_timer_generation`. Its producer and
callback use the same reserve/register/revalidate pattern. A stale or closed
callback is inert; a current callback clears its identity and retires the
pipeline. The lock is re-entrant because timeout retirement reuses the normal
video-context transaction.

## VideoSubregion lifetime

`VideoSubregion` is an independent terminal owner inside the video source. Its
constructor creates `_lifecycle_lock`, `_closed`, separate video/non-video
generations, and both retained-source slots before initializing reusable region
statistics.

`reset()` and `cleanup()` have different meanings:

| Operation | Region/statistics state | Timer state | Future scheduling |
| --- | --- | --- | --- |
| `reset()` | Reinitialized for another detection interval. | Both generations invalidated and both retained sources destroyed. | Allowed while the owner remains live. |
| `cleanup()` | Reinitialized only to drop retained regions/statistics. | Both generations invalidated and both retained sources destroyed. | Permanently rejected by `_closed`. |

Neither operation replaces the lock or resets `_closed`. Calling `reset()`
after terminal cleanup cannot reopen the object.

Both refresh families use the same publication protocol:

```text
under lifecycle lock:
  reject closed owner
  advance family generation
  detach old source object
outside lock:
  destroy old source
  create timeout Source and attach_source(callback, generation)
under lifecycle lock:
  publish retained object only if owner and generation still match
outside lock:
  otherwise destroy that exact local object
```

This covers three independent races: cleanup while attach is blocked,
callback dispatch before attach returns, and cleanup while a failed
video refresh is publishing its one-second retry. Every stale callback returns
`False` without invoking `refresh_cb` or changing a replacement slot.

A callback which successfully claims its generation retains
`_lifecycle_lock` through `refresh_cb`. Terminal cleanup therefore cannot
return while that claimed callback can still reach the source. If video
refresh succeeds, the corresponding regions are cleared. If it returns false,
a retry is prepared only for the still-current generation and is published
after releasing the lock; cleanup in that gap makes the helper reject or
remove it. A non-video refresh which does not complete is left for later damage
to reschedule under the existing policy.

Region mutation, detection settings, exclusion zones, diagnostics, and the
identification calculation use the same lifecycle lock. `add_video_refresh()`
may run outside the UI thread, so its region split and scheduling decisions are
one locked state transition rather than assumptions about GLib-thread order.

Cleanup destroys both timer families even if the first destroy raises.
Slots are cleared and terminal state is published before either external
operation is attempted. `get_info()` continues to expose numeric timer IDs via
the retained objects' `get_id()`; installed diagnostics keep their existing
value types without making numeric IDs the release authority.

## Scroll state, saved streams, and backend completion

`scroll_data` is a one-time ownership slot, not repeatable policy. Video damage
cancellation queues `do_free_scroll_data()` as mandatory worker work and does
not let `init_vars()` erase the reference first. Work already ahead of that
callback may publish scroll state; the callback sees the final worker-ordered
owner, clears the field, and calls `free()` once.

The saved video stream follows the encoder which produced it. Opening the
stream publishes `_video_stream_encoder` with the file. `ve_clean()` clears
both fields and closes the file in `finally` only for that exact encoder,
including when its backend raises. Retiring captured A leaves replacement B's
file untouched until B's own retirement. Data returned by a closing B-frame
flush is written and flushed before the pipeline is retired.

Codec-spec weak-reference counts remain diagnostic inputs to scoring. They do
not replace explicit cleanup. Conversely, the common codec interface exposes
no portable completion handle beyond `clean()`. This layer therefore does not
poll backend counters or join backend-private threads. It transfers each owned
instance to the correct cleanup domain, invokes cleanup exactly through that
contract, and does not use the instance afterward.

## Connection encode tail and CUDA ownership

The dynamic client connection may combine window and encoding mixins in an
order where connection-wide encoding cleanup runs before per-window cleanup.
No mixin may therefore terminate the shared worker on its own.

`ClientConnectionMuxer.close()` holds `_cleanup_lock` across one reverse
traversal and publishes `_cleanup_started` and the close event before entering
the mixins. Concurrent or repeated close requests cannot repeat their resource
handoffs. This connection-level terminal bit is not a replacement for the
generic window timer case's deferred callback cleanup: those are distinct
owners with different completion conditions.

The muxer iterates every selected base in reverse order.
Each call has its own exception boundary: failure is logged and retained, then
the loop proceeds to the next base. This is resource traversal, not
best-effort success. Once all bases have had their cleanup opportunity, the
first retained failure is reported. Reaching the later bases is what makes the
tail below unconditional even when a window source reports a cleanup error.

`ClientConnection` initializes `encode_end_callbacks`, `_encode_lock`, and
`_encode_queue_closed` beside its encode queue. The class-level `queue_encode`
method serializes acceptance and lazy worker creation under this lock; it does
not replace itself with an unguarded queue method after the first item. Worker
creation succeeds before an item is accepted. After sealing, optional work is
discarded and mandatory work raises visibly. Callers cannot enqueue `None`.
`call_in_encode_thread_at_end()` records a mandatory callable and arguments
which require every subsystem's ordinary cleanup to have queued first.
`EncodingsConnection.cleanup()` cancels its recalculation timer and registers
`free_cuda_device_context()` there when a context exists; it does not queue
`None`.

The calculation thread is not the encode thread. `add_work_item()` schedules
`recalculate_delays()` on the process-wide background worker, which reads
connection/window statistics, updates bandwidth and batching, and may
reconfigure a window's codec choices. Draining only the encode FIFO cannot
establish that this producer has stopped accessing window state.

`EncodingsConnection` therefore has two distinct locks and one terminal bit:

| State | Authority |
| --- | --- |
| `_encoding_state_lock` | Short admission/publication transitions for the calculation scheduler, `_encoding_closed`, and `cuda_device_context`. |
| `_calculate_execution_lock` | The entire active `recalculate_delays()` body, including its statistics, bandwidth, batch, and reconfiguration callouts. |
| `_encoding_closed` | Monotonic producer closure, independent of whether CUDA has already been physically released on the encode tail. |

`may_recalculate()` uses only the short state lock. It preserves the existing
pixel threshold and one-second scheduling policy, but cannot admit work after
closure. A delayed handoff owns a `GLib.Source`; the callback clears that exact
slot before enqueueing background work, and cancellation destroys the retained
object. A previously queued background item rechecks terminal state before
entering the body. No delayed numeric-ID lookup is used for release.

Cleanup first closes admission under the state lock, cancels the owned GLib
source, and waits for the calculation execution lock without holding the state
lock. This order matters when a
draw ACK still owns a per-window operation claim and calls `may_recalculate()`:
the ACK must not wait for the active calculator that source removal is itself
waiting to drain. Once the active calculation completes, cleanup clears its
pending IDs/pixels and registers CUDA release even if timer destruction raised.
The error remains visible after later owners have received their cleanup.

The factory deliberately places `EncodingsConnection` after `WindowsConnection`
for initialization. Its reverse cleanup order consequently drains calculation
before window-source cleanup or base statistics reset. Keep that relationship
when changing mixin composition. It is a connection-close fence, not a promise
that merely copying a window-source dictionary pins each source. Individual
parent/child removal and exact-object claims during calculation belong to the
separately composable `wayland-subsurface-stream-ownership` case.

CUDA publication has its own local-construction boundary. Both capability
negotiation and later codec initialization/configuration may request a context.
The allocator checks current/terminal state, constructs a candidate outside the
state lock, then publishes it only if the connection is still live and no other
candidate won. A losing local candidate is freed outside the lock. The native
helper returns a lazy `cuda_device_context` wrapper: creating that wrapper is
not itself proof that a driver context has already been allocated. The wrapper
owns its eventual native context through the backend's existing enter/free
contract. The published owner remains available to accepted window encodes
until its tail callback detaches the field and invokes `free()`.

The independent `EncoderServer` service also calls the allocator, so its
construction receives the same publication guard. Its packet-thread encoder
map is not this window encode FIFO; tail ordering here does not claim to prove
every independent service encoder's lifetime. No service-specific encoder
protocol or backend-private completion mechanism is introduced by this case.

When the exhaustive muxer traversal reaches base
`ClientConnection.cleanup()`, it:

1. publishes the close event;
2. takes `_encode_lock`, ensures the worker exists, and seals queue admission
   exactly once;
3. moves the end-callback list to a local list and appends every callback as
   mandatory work under the same lock;
4. appends the sole `None` sentinel before releasing that lock; and
5. clears protocol/statistics state according to the existing base lifecycle.

The resulting connection order is:

```text
mandatory per-window release
  -> per-window encode_ended barriers
  -> shared CUDA release
  -> shared mmap-area close and future registered end owners
  -> sole sentinel
```

The registry is not a second queue or worker. It is a one-time ordering tail
for resources whose release must follow all window work. Future mixins with
the same requirement register a callback; they never publish another
sentinel. Likewise, a future muxer cleanup must remain one element of the
exhaustive reverse traversal rather than becoming an early-return boundary.
The encode loop catches an item's `BaseException` and continues draining; a
throwing codec must not abandon later mandatory resource releases. There is
still exactly one FIFO and one worker. The design does not retry arbitrary
mixin or native cleanup after exceptions: those APIs do not promise that a
failed call had no side effects.

`MMAP_Connection.cleanup()` first detaches both shared areas from connection
state. With a dynamically composed connection it registers one tail callback
which attempts both physical closes and preserves the first exception after
logging later ones. A standalone mmap mixin without the tail interface closes
them synchronously. The delayed close is required because a mandatory window
encode already accepted by the FIFO may still be writing its captured mmap
area; detaching the public fields prevents new use while the queued owner
finishes before physical release.

## EOS and stream identity

EOS belongs to the decoder stream being retired, not merely to an encoder
object which happened to be cleaned.

`ve_clean(ve, send_eos=None)` derives default intent by comparing `ve` with the
source's currently published encoder. A direct current-encoder cleanup can
therefore close its stream. An external context transaction detaches its
captured encoder first, so its default is no EOS: work ahead of the queued
cleanup may already have opened a replacement stream, and stale cleanup must
not close it.

Worker-owned invalidation in `check_pipeline()` and failed-option recovery in
`setup_pipeline()` call `video_context_clean(True, send_eos=True)`. Those paths
know they are retiring the active stream before constructing another candidate
and preserve explicit EOS intent even though pair detachment precedes backend
cleanup. EOS is queued only after `ve.clean()` succeeds.

After successful encoder cleanup, `ve_clean()` emits the established
`WINDOW_EOS, self.wid` packet directly when the captured intent says that the
current decoder stream is being retired. The separately owned
`wayland-subsurface-stream-ownership` case has no EOS specialization: its
internal child source is not a `WindowVideoSource`, cannot create a decoder
stream, and never enters this method. Stack composition must preserve that
type boundary instead of adding a virtual EOS hook solely for a non-video
producer.

## Threading and lock order

The final stack has several independent synchronization domains. They are kept
unnested at their crossing points rather than treated as one global lock:

| Domain | What may run while held | Crossing rule |
| --- | --- | --- |
| Connection damage-packet lock | Exact packet publication, ACK ownership, or source deactivation from the subsurface case. | Source maps and ACK identities are detached under the connection lock; source cleanup runs after releasing it. |
| Generic `WindowSource._timer_lock` | Lease-map, epoch, slot, and active-callback accounting from `window-source-timer-lifecycle`. | A callback claims under the lock, increments the active count, then runs its body outside the lock. Cleanup closes leases and waits on the condition. |
| Connection `_cleanup_lock` / `_encode_lock` | One terminal mixin traversal / exact encode admission and final tail publication. | Queue acceptance never calls back into a window owner; window handoffs may take the encode lock, not the reverse. No worker join is performed under either lock. |
| Encoding `_encoding_state_lock` / `_calculate_execution_lock` | Short terminal/scheduler/CUDA publication / one active background calculation. | Cleanup releases the state lock before waiting for execution; `may_recalculate` never takes the execution lock. Per-source operation claims belong to WSSO. |
| `WindowVideoSource._video_state_lock` | Video terminal bit, pair publication/detachment, queue transfer, and video timer generations. | Do not call generic timer scheduling while held; `update_av_sync_delay()` is deliberately outside it. Native cleanup and image freeing occur outside it. |
| `VideoSubregion._lifecycle_lock` | Region state, timer generation claim, and one claimed `refresh_cb`. | Video-source cleanup marks video state under its lock, releases it, then closes the subregion. Timer registration/removal occurs outside the subregion lock. |
| `_video_refresh_lock` | One claimed x264 fallback UI refresh and cancellation fence. | Claim video state briefly, then release its lock before calling generic `refresh`; video cleanup releases its state lock before waiting for this fence. |
| Connection encode FIFO | Native codec construction/use/cleanup and scroll release. | No caller-thread join. Mandatory cleanup and end callbacks precede the sentinel. |

The generic timer callback body running outside `_timer_lock` is essential for
composition. The subsurface ACK path claims its exact packet owner and active
operation under the connection condition, releases that condition before it
calls `damage_packet_acked()`, and reacquires it only to finish the claim. A
timer callback similarly claims its lease under `_timer_lock`, releases the
lock before packet publication, and reacquires it only to complete its active
callback. Those two completion fences let teardown wait for in-flight work
without nesting the connection and timer synchronization domains.

The corresponding video edge is removed in `encode_from_queue()`:
`update_av_sync_delay()` may acquire the generic timer lock, so it executes
without `_video_state_lock`; terminal state is revalidated after it returns.

The video cleanup override acquires `_video_state_lock` only long enough to set
the closed bit. It releases that lock before `VideoSubregion.cleanup()` and
generic `WindowSource.cleanup()`. The patch does not hold it while waiting for
a worker, and it introduces no worker join.

`VideoSubregion` deliberately keeps its own lock across a refresh callback so
terminal subregion cleanup waits for an already claimed callback. Video code
does not enter subregion mutation while holding `_video_state_lock`; the two
terminal transitions are ordered by releasing the video lock first.

## Patch-queue and responsibility boundaries

The case has `dependencies = []` and remains independently selectable against
the frozen embedded source. Other active cases touch
`xpra/server/window/video_compress.py`, so complete-stack resolution must prove
both textual application and the combined state machine.

Responsibility is divided as follows:

| Case | Owned boundary |
| --- | --- |
| `video-pipeline-cleanup-race` | Native pair publication/retirement, video images/sources, flush/fallback/watchdog generations, `VideoSubregion`, scroll/saved-stream cleanup, terminal connection calculation/CUDA publication, exhaustive muxer close, mmap close ordering, ordinary EOS intent, and the base connection encode tail. |
| `window-source-timer-lifecycle` | Generic `WindowSource` timer leases, callback completion accounting, terminal idempotence, icon timer, and exception-complete generic cleanup. |
| `wayland-initial-window-state` | Current Wayland buffer format, frame-alpha selector, CSC readiness, popup publication order, and opaque-region/dimension rebinding. |
| `wayland-subsurface-stream-ownership` | Retained normalized root/child rasters, stable surface identity, authoritative topology, ordered raw RGB32 parent-backing transactions, exact packet ownership and client draw-ACK routing, atomic Cairo/OpenGL staging, native pointer targeting, composite-root acknowledgement, child frame completion, and its live gate. |
| `wayland-empty-damage-throttle` | Ordinary non-composite toplevel frame-callback acknowledgement, empty-damage guard, and damage/no-damage pacing. |

The timer case and this case both modify video call sites, but neither is a
production dependency of the other. The timer case must retain the inherited
`expire_timer` producer; this case must retain its separate video generations
and the unlocked A/V-delay update. The subsurface case overlaps
`xpra/server/source/client_connection.py`: this case owns the connection-wide
end-callback registry and sole sentinel, while WSSO owns safe packet-enqueue
notification, composite packet filtering, ordinary-packet priority, and exact
ACK claims. It also overlaps `xpra/server/source/encoding.py`: WSSO fans generic
configuration across `all_pixel_sources`, but video encoder cleanup continues
to traverse only `all_window_sources` because a WSSO child is a direct
`WindowSource` and cannot own a video pipeline. That type boundary must remain
intact.

Refresh or conflict resolution must use an isolated workspace and
`workspace-stage` / `workspace-update`. Never hand-edit `fix.patch`, its
manifest digest, or its derived path list.

## Patch ownership and non-goals

`fix.patch` owns exactly the paths derived by `case.toml`:

- `xpra/server/source/client_connection.py`;
- `xpra/server/source/encoding.py`;
- `xpra/server/source/factory.py`;
- `xpra/server/source/mmap.py`;
- `xpra/server/window/video_compress.py`;
- `xpra/server/window/video_subregion.py`;
- `tests/unittests/unit/server/source/encoding_lifecycle_test.py`; and
- `tests/unittests/unit/server/window/video_compress_test.py`.

Its production responsibility is limited to:

- one-time video resource initialization and monotonic terminal state;
- registry reinitialization through the common context-clean transaction;
- atomic live-pair publication and terminal constructor-loser cleanup;
- worker-serialized captured cleanup plus a late sweep;
- nested pair cleanup and sibling-action exception aggregation;
- terminal ownership of delayed A/V images and their timeout/idle sources;
- B-frame flush and inactivity timer generations;
- terminal `VideoSubregion` refresh ownership;
- scroll and saved-stream release ordering;
- explicit ordinary-stream EOS intent and direct established packet emission;
- exhaustive reverse-order dynamic-mixin cleanup;
- terminal background-calculation completion and local CUDA-owner publication; and
- connection-wide end callbacks, deferred shared mmap close, and one
  base-owned encode sentinel.

It does not:

- change codec discovery, scoring, preference, dimensions, scaling, CSC mode,
  quality, speed, or format negotiation;
- force video or H.264 for a region which the existing selector treats as a
  picture;
- bind an A/V-delayed image to a stale native codec generation;
- poll codecs, GLib sources, or weak instance counts;
- join the encode worker or backend-private cleanup threads;
- suppress cleanup errors or turn backend failure into success;
- change mmap negotiation, ring/descriptors, or per-packet lease semantics;
  this case owns only connection-area close ordering after queued encodes;
- synthesize EOS for an unpublished encoder;
- create a child decoder identity or own subsurface transaction, packet, ACK,
  client-backing, input, or frame-callback behavior;
- own generic `WindowSource` timer leases;
- change Wayland frame-alpha or empty-damage policy;
- add application-specific production logic; or
- weaken a fixed hardware profile to manufacture an atomic live result.

## Focused regression design

`unit.server.window.video_compress_test` retains the upstream context tests and
adds behavioral lifecycle regressions. The module uses controlled resource
doubles but exercises production methods,
real Python threads, `Event` barriers, a FIFO `Queue`, the real dynamic
connection muxer, and the actual `ClientConnection.encode_loop()`.

`PipelineElement`, `Converter`, and `Encoder` count cleanup and can fail or
block initialization/information access. `ScrollOwner` counts release.
`BlockingSources` can dispatch a callback before attach returns, or block
timeout/idle publication across cleanup. Two native regressions additionally
dispatch the real GLib default context and observe automatic one-shot source
destruction across all seven video/subregion source families. They require
completion with no invalid-source warning and no retained stale timer; a
numeric-ID-only double cannot establish that native ownership contract.
`EncodeWorker` preserves FIFO
order, records mandatory/optional flags, and continues draining after an
injected exception.

### `VideoSubregionLifecycleTest`

| Test | Required behavior |
| --- | --- |
| `test_cleanup_waits_for_claimed_refresh_callback` | For both video and non-video families, a claimed refresh completes before terminal cleanup returns; later dispatch of every retained callback is inert. |
| `test_callback_before_timer_publication_is_not_retained` | Immediate callback dispatch claims the exact generation once; the publisher destroys its exact source and its public slot remains zero. |
| `test_cleanup_reclaims_timer_published_after_cancellation` | Cleanup during blocked attach destroys the source for both families, prevents callback work, and cannot be undone by `reset()`. |
| `test_cleanup_reclaims_refresh_retry_publication` | A failed video refresh may prepare a retry, but cleanup during retry publication destroys its source and prevents a second callback. |
| `test_cleanup_attempts_both_timer_removals` | Failure removing the video refresh source does not skip non-video removal; both slots clear, the owner closes, and the first error propagates. |

### `ConnectionCleanupOrderTest`

| Test | Required behavior |
| --- | --- |
| `test_queue_sentinel_follows_window_and_shared_resource_cleanup` | The real dynamic connection and encode loop execute per-window codec cleanup, then CUDA release, then stop at the sole sentinel with an empty queue. |
| `test_mmap_area_outlives_queued_window_encodes` | The connection drains mandatory per-window encode cleanup before closing the shared mmap write area, then releases CUDA and mmap exactly once ahead of the sole queue sentinel. |
| `test_queue_sentinel_and_later_mixins_survive_cleanup_error` | A window cleanup which queues codec release and then raises does not stop reverse mixin traversal: CUDA release and the base sentinel still run, the worker terminates with an empty queue, and close reports failure. |
| Repeated connection close | The actual `ClientConnection` constructor, lazy queue admission, dynamic muxer, and worker leave no resource callback or duplicate sentinel behind termination. A copied fake queue cannot establish this guarantee. |
| Immediate captured-image close | A real accepted image handoff reaches its mandatory release owner after the connection closes, even though optional compression is now skipped. |

### `VideoContextCleanTest`

| Test | Required behavior |
| --- | --- |
| `test_cancels_timers_without_a_context` | An empty external snapshot still queues a non-optional cleanup/late-sweep barrier and repeats timer cancellation on the worker. |
| `test_cleans_context_published_after_empty_snapshot` | A pair published after an empty caller snapshot is detached and cleaned by the worker-side late sweep. |
| `test_detaches_and_cleans_context` | The visible pair detaches synchronously, then both captured elements are cleaned through one mandatory callback. |
| `test_timer_cancel_error_does_not_bypass_context_cleanup` | A flush-timer cancellation error remains visible but does not skip watchdog cancellation, pair detachment, queueing, or later codec cleanup. |
| `test_ve_clean_still_cancels_its_timer` | Direct encoder cleanup retains ownership of the inactivity watchdog. |
| `test_closed_encoder_flush_saves_data_before_cleanup` | Data returned by a flush which closes the encoder is written, flushed, packetized, and queued before direct context cleanup, without timer rearm. |
| Captured flush request | A queued request for encoder/frame A cannot read a replacement frame tuple B at dispatch. |
| x264 frame-zero retirement | The special reset clears and cleans the whole published pair, including the CSC, before requesting a picture refresh. |

### `VideoPipelineLifecycleTest`

| Test | Required behavior |
| --- | --- |
| `test_cleanup_sweeps_pipeline_published_by_setup` | Cleanup racing blocked construction retires the newly published pair; with an old throwing CSC, the old encoder and complete late pair are still attempted and the error remains visible. |
| `test_subregion_cleanup_error_does_not_bypass_base_cleanup` | Video terminal state is set and inherited cleanup is attempted even when subregion cleanup raises; the first error is retained. |
| `test_damage_cancel_error_does_not_bypass_owned_cleanup` | Failure removing the first A/V source does not skip the second source, queued-image free, both subregion timers, scroll release, B-frame/watchdog cancellation, pair detachment, or cleanup queueing. |
| `test_av_timer_cancel_attempts_timeout_and_idle_sources` | A/V cancellation clears and attempts both timeout and idle source destructions when the first raises. |
| `test_reinitialization_cleans_existing_pipeline` | A real registry refresh detaches and cleans the old pair while preserving inherited CSC parsing and selection update. |
| `test_reinitialization_sweeps_pipeline_published_during_rebuild` | A pair which finishes publication while the callable registry is blocked is retained in one-time fields and retired by the queued late sweep. |
| `test_terminal_setup_releases_pair_without_publication` | A constructor which finishes after terminal closure cleans both locals exactly once and publishes neither. |
| `test_video_timer_callbacks_claim_before_publication` | Immediate dispatch of B-frame and watchdog callbacks performs one valid action, clears the slot, and causes the publisher to destroy each exact source. |
| `test_invalid_pipeline_cleanup_attempts_both_elements` | Direct invalidation detaches both fields, attempts the encoder after a throwing CSC, emits one EOS for the retired current stream, and does not proceed to setup. |
| `test_failed_option_cleanup_attempts_both_elements` | Failed-option recovery uses the same direct transaction: both elements are attempted, fields detach, and explicit current-stream EOS is retained. |
| `test_failed_initialization_cleans_unpublished_instances` | CSC-init failure, encoder-init failure, and throwing CSC cleanup each release every constructed local owner exactly once without publication. |
| `test_stream_file_closes_when_encoder_cleanup_fails` | A saved bitstream file closes and its reference clears in `finally` while the native encoder error propagates. |
| Exact saved-stream owner | Retiring captured encoder A cannot close replacement B's stream; B closes it once during its own retirement. |
| `test_cleanup_cannot_overtake_detached_context_handoff` | A blocked real external context-clean handoff cannot be overtaken by terminal `encode_ended`; the detached pair remains ordered before the final source barrier. |
| `test_x264_fallback_refresh_is_cancelled_by_cleanup` | The real frame-zero path creates a tracked refresh which cleanup cancels; stale dispatch cannot reach generic refresh after terminal cleanup. |
| `test_full_cleanup_cancels_timer_and_queues_one_barrier` | Full inherited cleanup removes the watchdog, frees a delayed image before worker release, cleans the pair once, cleans batch state, and preserves `setup -> held work -> scroll free -> codec clean -> encode_ended`. |
| `test_full_cleanup_preserves_resources_for_worker_sweep` | Scroll state, a captured old pair, a pair and both video timers published by work ahead of cleanup all remain observable until the worker releases/frees them exactly once before `encode_ended`. |
| `test_cleanup_reclaims_post_cancel_encode_queue_idle` | A blocked idle source published after cancellation is removed, its delayed image is freed, and later callback dispatch cannot schedule a timeout or enqueue work. |
| `test_av_sync_update_does_not_hold_video_state_lock` | A non-reentrant lock probe proves inherited A/V-delay update runs without the video-state lock, then the due item is encoded. |
| `test_callback_before_av_source_publication_is_not_retained` | Immediate timeout and idle dispatch cannot strand their source objects; timeout work is handed off once and idle rescheduling can be cancelled without retained sources. |

The clean tests-only control must reach these production methods on the frozen
source and fail non-vacuously at the lifecycle assertions, while the retained
upstream context behavior remains meaningful. The entire atomic patched module
must pass, and the same module must pass after timer, frame-state, subsurface,
and other stack cases compose. Test inventories and the named result determine
the count; a README count must not become a reason to omit new regressions.

`unit.server.source.encoding_lifecycle_test` supplies the complementary
connection producer checks. Both focused modules share
`make_encoding_connection()`, which constructs the real dynamic muxer with
`ClientConnection`, `MMAP_Connection`, `WindowsConnection`, and
`EncodingsConnection`. Every selected subsystem runs its normal constructor,
`init_from()`, and `init_state()`, followed by an RGB hello. The calculation
test then obtains its source through the real `make_window_source()` and
retires it through the encode FIFO and `WindowSource.ui_cleanup()`.
This construction lets independently composed source and connection owners
initialize their own state rather than making the fixture copy a partial set
of private fields.
Event barriers hold the statistical calculation while close is requested;
window/final release must remain ordered after calculation completion. Local
CUDA constructors are held across close or concurrent construction to prove
terminal publication rejection and exact loser release. A throwing calculation
source destroy must not skip the CUDA tail, and post-close damage cannot admit
another calculation. Its native GLib test proves the real delayed handoff
clears its source before worker delivery and that already queued work remains
inert after closure.

Mock resources do not claim hardware-driver completion. They make publication,
queue order, callback identity, cleanup count, and exception propagation
deterministic; the complete-stack hardware profiles supply the real codec and
presentation boundary.

## Durable live boundary

`case.toml` deliberately declares `required_gates = []`. There is no honest
standalone `CASE=video-pipeline-cleanup-race` live profile: the fixed hardware
validators require per-window frame-alpha and packet correlation owned by
`wayland-initial-window-state`. Applying only this cleanup patch cannot produce
that oracle, and a weaker startup/exit check would not prove the lifecycle.

The complete `develop` stack nevertheless requires both hardware-H.264
profiles because this case owns the real pipeline resources they exercise:

- Make target `live-xpra-hardware`, whose gate identity is
  `live-wayland-h264-hardware`, uses a title-bound native-Wayland `vkcube`
  primary and requires RADV; and
- Make target `live-xpra-opengl-hardware`, whose gate identity is
  `live-wayland-opengl-h264-hardware`, uses title-bound native-Wayland
  `glmark2-wayland` `jellyfish`, the selected render node, a non-software AMD
  Mesa context, exact viewport placement, and changing frames.

Both use adaptive-alpha/default policy. Server-side libyuv supplies the NV12
accepted by libva, while client software CSC is disabled so libva-decoded NV12
reaches the forced native OpenGL presentation path. For the opaque primary,
acceptance includes predominant H.264, exact allowed one-pixel lossless edges,
VA-API encode/decode, packet-chain, presentation, pixel, motion, input,
application-exit, and owned-cleanup evidence.

The separately title-bound RGBA GTK auxiliary must retain transparent and
opaque source pixels and use only alpha-capable picture packets. It protects
the frame-state case while the same connection creates and destroys real video
resources. Startup layout or a single H.264 packet is not acceptance.

These profiles prove that the composed lifetime operates with real libyuv,
libva, Mesa, and client presentation. The focused threaded module remains the
direct proof for forced constructor, timer, exception, and sentinel
interleavings. The case-only `live-wayland-subsurface` gate belongs to the
subsurface stream case and verifies ordered raw parent-backing composition by
non-video child sources; it is not a substitute for either hardware profile.

Before publication, the complete queue also retains all seven fixed positive
stack profiles. Their rendering, RGB/H.264, detach, transport-loss, input,
hardware, lifecycle, and cleanup boundaries ensure this connection-tail change
does not regress unrelated live ownership.

## Invariants not to simplify

- Initialize video resource state once before the virtual base initializer.
- Never let `init_vars()` or `do_init_encoders()` clear one-time codec, timer,
  queue, scroll, stream-file, lock, generation, or terminal fields.
- Use `_encoders` attribute presence only to distinguish first initialization
  from a real registry refresh; it is not native-resource state.
- Preserve the complete inherited registry rebuild, CSC parsing, and encoding
  selection sequence.
- Detach a published pair under `_video_state_lock`; publish both new fields
  under that same lock.
- A constructor which loses to terminal cleanup retains local ownership and
  cleans both instances without published-stream side effects.
- Clean a locally owned encoder directly; do not route an unpublished instance
  through EOS, timer, or saved-stream policy.
- Queue external pair cleanup as mandatory work and keep strong references to
  the captured pair; do not release the video-state lock between detachment
  and FIFO acceptance.
- Always perform the encode-thread late sweep after an external request,
  including an initially empty snapshot and a throwing captured cleanup.
- Keep direct worker cleanup direct; do not use `encode_thread=True` from an
  arbitrary caller.
- Attempt the paired encoder after a CSC exception and the late sweep after
  either captured cleanup exception.
- Preserve first-error visibility and log later sibling-action errors only
  after every cleanup attempt.
- Publish damage cancellation before swapping or freeing delayed images.
- Treat the A/V image queue separately from native codec generations.
- Swap the image queue under the video lock and free wrappers outside it.
- An immediate captured image requires a mandatory encode-or-free handoff;
  optional wakeups may own only work whose images remain in the source queue.
- Keep timeout and idle generations distinct; cancellation owns both.
- Revalidate live state, queue state, cancellation, and exact generation after
  each GLib registration returns.
- Destroy the exact locally retained Source when publication loses, including
  early callback and terminal cleanup races. Never reacquire it by a recycled
  numeric ID.
- Run `update_av_sync_delay()` outside the video lock and revalidate afterward.
- Queue at most one due A/V item per drain iteration and return later scheduling
  through the guarded idle path.
- Keep B-frame flush payload and its timer in one generation domain.
- Carry the exact captured payload and generation into the worker; never let a
  stale flush request consume a replacement frame.
- Queue a claimed B-frame flush before releasing its terminal-state lease; do
  not run native flush under the lock.
- Preserve final flush data and packet publication before closed-encoder
  cleanup.
- Retire the entire pair in the x264 frame-zero path and fence its subsequent
  picture refresh through terminal cleanup.
- Keep the inactivity watchdog in its own generation and cancel it from every
  encoder cleanup.
- Make `VideoSubregion.cleanup()` terminal and idempotent; `reset()` must never
  reopen it.
- Protect video and non-video subregion timers with independent generations.
- Keep a claimed subregion refresh callback inside the lifecycle boundary so
  cleanup cannot return ahead of it.
- Attach subregion sources outside the lock and publish their retained objects
  only after pre- and post-registration validation.
- Attempt removal of both subregion timers even when the first removal raises.
- Preserve `scroll_data` until encode-worker `do_free_scroll_data()` owns it.
- Close the saved stream in `finally` only for its exact encoder owner; do not
  treat file close as native codec completion.
- Send EOS only for the stream explicitly or identity-wise being retired, and
  only after successful encoder cleanup.
- Keep subsurface layer producers outside `WindowVideoSource`; do not add a
  video EOS hook or defensive video cleanup for a source that cannot own a
  decoder stream.
- Keep generic timer callback bodies outside `_timer_lock`; use active-callback
  accounting for cleanup completion.
- Never call generic timer scheduling while `_video_state_lock` is held.
- Detach connection packet ownership under its lock and perform source cleanup
  outside that lock.
- Attempt every dynamic connection base cleanup in reverse order even when an
  earlier base raises; report the first failure only after traversal finishes.
- Let base `ClientConnection.cleanup()` own every registered end callback and
  the sole sentinel.
- Seal encode admission and append the final tail under the same lock. Repeated
  close must not repeat mixins, and failed worker items must not stop tail drain.
- Stop the independent background calculation producer before window state or
  base statistics are cleared; the encode FIFO alone is not that fence.
- Never hold the encoding publication lock while waiting for active calculation,
  and never make an ACK-side `may_recalculate()` wait for the execution lock.
- Construct CUDA owners locally, publish only one live winner, and free losers
  outside the lock. Timer-cancellation failure must not bypass CUDA tail release.
- Order window cleanup and `encode_ended` before CUDA release, and CUDA release
  before worker termination.
- Do not replace exact ownership with polling, arbitrary timeouts, a second
  worker, or a caller-thread join.
- Do not weaken hardware evidence or relabel it as an atomic VPC gate.

## Required validation

Follow [development and final acceptance](../../docs/runbooks/validation.md),
stopping escalation at the first unexplained failure. During development,
run the nearest lifecycle regression immediately after each atomic edit,
including affected upstream modules and the timer/subsurface composed tests.
Exercise real compiled and compatibility modes when their behavior is involved.
Relevant hardware/lifecycle live runs use the admitted complete-stack selection
and may run before full suites; this case has no standalone live gate.
The table is final coverage, not a per-edit schedule. After candidate freeze,
fill only missing or invalidated requirements:

| Validation | Required proof |
| --- | --- |
| Clean tests-only focused control | Both final focused modules reach their frozen production boundaries and fail non-vacuously on absent lifecycle behavior rather than import or fixture setup. |
| Patched standalone focused run | Both complete declared focused modules pass with only this case selected. |
| Complete-stack focused run | Both focused modules pass after frame-state, generic timer, subsurface, and other active patches compose. |
| Composition-specific focused runs | Generic timer and subsurface modules pass with the unlocked timer callback body, exact cleanup order, direct ordinary video EOS behavior, and connection-tail behavior retained. |
| Patch and fork controls | Standalone/stack apply and reverse resolution, manifest-derived paths/digest, whitespace, lint, and repository controls pass. |
| Clean quarantine reassessment | All three assigned clean-source quarantine gates reproduce only their current exact subsets before patched results are interpreted. |
| `full`, `full-cython`, `full-no-compat` | The complete queue passes all maintained upstream unit-test legs. |
| Complete-stack `live-wayland-h264-hardware` | The Vulkan/RADV primary and alpha auxiliary complete the real codec, presentation, input, exit, and cleanup contract. |
| Complete-stack `live-wayland-opengl-h264-hardware` | The independent native OpenGL/render-node/viewport primary completes the same resource lifecycle. |
| Other case-owned atomic live gates | Each standalone case, including subsurface ownership, retains its own positive evidence. By design those `CASE=<slug>` runs do not prove VPC composition; VPC composition is established by the resolved-stack focused/full tests and the seven complete-stack live profiles. |
| Seven complete-stack positive live profiles | RGB, H.264, detach, transport loss, input, both hardware paths, application lifecycle, and owned cleanup remain green before publication. |

Retain the exact clean failure and every named patched result below
`.artifacts/fork-maintenance/`. A semantic change to pair publication, cleanup
nesting, queue handoff, timer generations, subregion callback ownership, EOS,
connection tail, or hardware evidence invalidates older results for that
boundary.
