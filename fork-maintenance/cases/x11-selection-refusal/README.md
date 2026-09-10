# X11 selection conversion refusal

## Boundary

An X11 selection owner can refuse a requested representation by returning
`SelectionNotify` with `property=None`. The X server also reports refusal when
there is no selection owner. This is an explicit conversion result, not a
silent owner or a timeout. The
[ICCCM selection protocol](https://www.x.org/releases/X11R7.7/doc/xorg-docs/icccm/icccm.html#responsibilities_of_the_selection_owner)
defines that negative reply separately from successful property delivery.

At the embedded source, Xpra already has a parser for core `SelectionNotify`,
but no dispatch signal for it. The generic synthetic-event filter also rejects
the form sent by an owner through `XSendEvent`. The local conversion therefore
waits for a property which will never be written, then warns:

```text
Warning: CLIPBOARD selection request for 'text/plain;charset=utf-8' timed out
 request 2
```

The same failure can occur for PRIMARY, another MIME spelling or TARGETS.
One copied line may cause several representation requests; an owner which
refuses a MIME alias may still serve `UTF8_STRING` successfully. Neither a
working alternative nor a successful paste repairs the missing negative path.

This case admits the real core reply and correlates it with one outstanding
local conversion. A refused request completes immediately with the existing
empty callback result. A genuinely silent owner still reaches the unchanged
deadline and warning. The patch changes neither clipboard rate limits nor
remote wire-request timeouts.

## Embedded-source context

The case resolves against source commit
`d95058b0916913fe6ae5296fb702f66d833898b0`, embedded in current `develop`.
The current `ClipboardProxy.get_contents()` asks `XConvertSelection` to place
results on the helper's shared event window using a selection/target-derived
property. Successful property changes can reach the proxy through the helper;
that route cannot observe a refusal because no result property is created.

An absent owner is a normal X11 selection state. The conversion still reaches
`XConvertSelection` and consumes the server's refusal, but its diagnostic
description must not call `get_wininfo(0)`: that helper queries window title
properties and causes `BadWindow` even when debug output is disabled. The
request boundary formats the absent owner without native window queries;
nonzero-owner diagnostics and the actual refusal protocol remain unchanged.

There are three missing pieces at the native reply boundary: a core-event
signal mapping, admission of owner-sent synthetic replies and an explicit
requestor window field for dispatch. Adding those alone would still leave
ambiguous completion when several conversions share the same target and
`CurrentTime` timestamp.

The fix keeps the existing conversion API and callback shape, but gives each
actual conversion its own requestor window. It leaves cached local contents,
selection ownership, target translation and remote-request handling with their
existing owners. The defect is present without any downstream clipboard case.

On an upstream refresh, inspect event parsing, dispatch and proxy completion
together. An equivalent replacement must distinguish refusal from silence,
correlate concurrent/retried conversions, preserve positive and incremental
delivery, and retire native requestor resources. A new signal name alone or
longer conversion timeout is not an equivalent repair.

Current-code reassessment retains the refusal repair and adapts its native
ownership. Upstream `af0d210f310` removed the injected
`xpra.x11.common.get_pywindow` API. The helper now explicitly passes its
GTK-filter route to the proxy; only that route uses the maintained
`gtk_event_window()` adapter. Raw X11 server conversions must neither require
a GDK wrapper nor import GTK. The review also separates private INCR streams
and their cancellation: independent requestor windows cannot safely share the
legacy helper-window accumulator.

## Surrounding code and ownership map

| Component | Responsibility |
| --- | --- |
| `xpra/x11/selection/clipboard.py` | Owns the shared helper event/selection-owner window and routes its events to the appropriate selection proxy. |
| `xpra/x11/selection/proxy.py` | Owns local conversion callbacks, deadlines, per-conversion requestors, reply admission and property reads. |
| `xpra/x11/bindings/window.pyx` | Performs native window creation/destruction, conversion requests and property I/O. |
| `xpra/x11/bindings/events.pyx` | Parses core X events, applies synthetic-event admission and supplies the signal/window metadata used by dispatch. |
| `xpra/x11/gtk/bindings.pyx` | Supplies the real GDK X11 filter which feeds native events into Xpra's parser/dispatcher. |
| `xpra/x11/selection/common.py` and the GTK lookup adapter | Supply the optional toolkit wrapper only when the helper's GTK filter routes events. |
| `xpra/x11/dispatch.py` | Maps XIDs to receivers and forwards only signals declared by those GObject receivers. |
| `xpra/clipboard/core.py` and `xpra/clipboard/timeout.py` | Own clipboard protocol messages, selection mapping and remote request completion/deadlines. |
| `xpra/server/source/clipboard.py` | Owns the separate outgoing clipboard packet budget. |

The repaired native conversion path is:

```text
ClipboardProxy.get_contents(target, callback)
  -> use an eligible owner-bound cache, or begin a native conversion
  -> create private InputOnly requestor and register its receiver
  -> record local request ID, deadline and callback
  -> XConvertSelection(..., requestor, CurrentTime)
       -> owner writes property and sends SelectionNotify(property)
       -> or owner/server sends SelectionNotify(None)
  -> native GDK filter -> core parser -> XID dispatcher -> proxy
       -> match requestor, selection, target and time
       -> refusal: retire this request and return an empty result
       -> success: accept acknowledgement, read this requestor's property
            -> ordinary result: retire and complete this request
            -> INCR: continue through property changes, then complete
```

These are local X11 conversion IDs and XIDs. They are not Xpra packet sequence
numbers, the protocol helper's remote request IDs or XFixes owner generations.
The same integer appearing in two layers' logs does not establish a shared
request identity.

## Core-event admission and routing

The parser adds `SelectionNotify -> ("x11-selection-notify", "")` to the core
event map and admits synthetic `SelectionNotify` alongside the already
permitted `ClientMessage` and `UnmapNotify`. No other synthetic event becomes
admissible. Owner-sent and X-server-generated refusals then share the same
native parse/dispatch route.

`parse_SelectionNotify()` sets `window` to the requestor XID in addition to
retaining `requestor`, `selection`, `target`, `property` and `time`. This lets
the dispatcher identify a direct event for that window rather than depending
on a fallback receiver or treating it as a parent-window notification.

The per-selection proxy declares the new GObject signal and registers itself
on each private requestor. The shared helper window is not made a catch-all
receiver for every conversion. Existing helper events and XFixes selection
notifications retain their separate routing.

Synthetic admission does not make a reply trustworthy by itself. The proxy
still requires an active owned requestor and matching conversion metadata.
Those checks prevent stale or unrelated completion; they are not an
authentication boundary against another client with access to the same X11
display. The patch does not redefine that display's security model.

## Request identity and native ownership

`get_contents()` retains the existing owner checks and eligible target/content
caches. If the helper itself owns the selection and no usable cached value is
available, the existing immediate-empty path remains. A requestor is allocated
only when an actual native conversion is needed.

Each conversion receives an unmapped InputOnly child of the root, with
property-change notifications selected. The GTK route also selects structure
notifications and requires a retained GDK wrapper; the raw event-loop route
needs neither GTK nor that extra event mask. It is not a visible
application window and does not claim the selection. Its native XID is
registered in the dispatcher, and the lookup wrapper is retained for the
whole conversion so GTK can continue to associate events with that window.

The proxy's per-request state is bounded by its active conversions:

| Field | Meaning |
| --- | --- |
| `local_requests[target][request_id]` | Existing GLib conversion deadline and completion callback. |
| `_requestor_windows[xid]` | Target, local request ID and retained window wrapper for one conversion. |
| `_requestor_xids[request_id]` | Reverse lookup used by completion, timeout and cancellation. |
| `_requestor_notified` | Requestors whose matching positive acknowledgement was accepted; not a completed-data set. |
| `_requestor_incr[xid]` | Independent chunks, actual byte count, type, format and exact timer reservation for one incremental conversion. |
| `_requestor_gtk` | Helper-owned event-route choice; it does not acquire another filter lease. |
| `_requestors_closed` | Terminal cleanup flag preventing later native conversions on this proxy. |

The property name remains `selection-target`. It need not encode a unique
request ID because the property lives on a different window for each request.
The implementation does not create a new atom for every retry or keep an
unbounded history of retired requests.

This distinction matters for refusals: `property=None` carries no property
identity. The ICCCM describes ambiguity between outstanding requests with the
same requestor, selection, target and timestamp. This implementation avoids
depending on that shared tuple by assigning a distinct requestor to each
conversion, including two requests for the same target.

`CurrentTime` remains the timestamp used by the existing conversion call;
the handler requires that same value in the reply. The patch does not add a
timestamp-allocation policy or treat target spelling as a request ID. Matching
CLIPBOARD data must not complete PRIMARY work, and a reply to a retired XID
must not cancel a newer conversion merely because its target matches.

The ICCCM recommends using the triggering event's timestamp instead of
`CurrentTime`. This case preserves the existing conversion API; distinct XIDs
make its unchanged timestamp unambiguous without claiming to implement that
separate timestamp policy.

## Refusal, acknowledgement and property ordering

`do_x11_selection_notify()` applies these checks before completing or reading
anything:

1. Find the requestor in `_requestor_windows`.
2. Require `event.window == event.requestor` and the proxy's selection.
3. Reject another acknowledgement for an already accepted requestor.
4. Require the recorded target and `CurrentTime` timestamp.
5. Interpret an empty property as refusal, or accept exactly this conversion's
   `selection-target` property as a positive acknowledgement.

An unexpected nonempty property is ignored rather than converted into a
refusal or used to read unrelated window data. Mismatched metadata leaves the
real request and its deadline intact so a subsequent valid reply can finish it.

Refusal removes only the identified callback and retires its requestor. The
existing callback representations are preserved:

| Requested target | Refusal result |
| --- | --- |
| `TARGETS` | `("ATOM", 32, b"")`, an empty target inventory. |
| Any other target | `("", 0, b"")`, no converted contents. |

There is no new wire error packet. Callers retain their own representation
fallback and remote-request behavior. The refusal itself is debug-level
diagnostic information, not a conversion-timeout warning.

Positive delivery has a separate ordering hazard. An owner writes the property
before it sends the success acknowledgement, so `PropertyNotify` may reach
Xpra first. The proxy must not consume a complete result and destroy the new
requestor before the owner has finished acknowledging it.

Private-requestor property notifications are therefore admitted only after a
matching positive `SelectionNotify`. On that acknowledgement the proxy marks
the requestor accepted and immediately reads its property, which also handles
the earlier property event. Later matching property changes continue through
the same reader. Unrelated property atoms do not enter conversion handling.

The accepted marker is especially important for INCR: the acknowledgement
starts the transfer but does not complete it. A duplicate refusal after that
positive acknowledgement must not replace an in-progress successful transfer
with an empty callback result. The regression sends exactly that sequence.

## Positive contents and incremental transfer

`read_requestor_property(xid, atom)` reads the acknowledged conversion's own
property. Type/format discovery, TARGETS extraction and content filtering still
use the native bindings and existing codecs. The unchanged legacy
`do_property_notify()` reader remains separate for the helper window; its
accumulator cannot intercept a private requestor's data.

An ordinary result completes through `got_requestor_contents()`. It resolves
the private XID back to one local request ID, removes that callback, destroys
the requestor and removes its deadline before returning filtered contents.
A successful reply no longer completes every pending request for the same
target just because they shared one property on the helper window.

The helper-window path still completes through `got_local_contents()`. That
existing entry point detaches all callbacks for its target and retires their
private requestors and deadlines before invoking any consumer. A raising
consumer cannot strand another callback; the batch is exhausted before the
first error is propagated. Re-entrant new reads are not part of that retired
batch.

Private INCR state belongs to the exact requestor. The initial one-integer,
format-32 INCR property starts a stream; its lower bound may be zero and is
not an exact final length. Deleting it admits the first chunk. Each later
nonempty chunk is appended only to that requestor's accumulator, with matching
type and format; deleting it admits the next chunk. An empty property joins
and completes that stream. A missing property is not an empty terminator.

Both a regular result property and the final empty INCR property are explicitly
deleted before native window destruction. Owners observing property deletion
therefore receive the protocol's completion acknowledgement. Positive
SelectionNotify still precedes reading, so destruction cannot overtake the
owner's success acknowledgement.

Each stream retains the existing one-second incremental deadline, with an
exact request/stream/reservation identity checked by its callback. Rescheduling
or retirement invalidates the previous reservation before timer removal; a
queued old callback cannot expire its replacement. The separate conversion
deadline still applies and is not extended by chunks. Expiry retires that
requestor's accumulator too, so an interrupted transfer cannot contaminate a
retry, a parallel INCR or an ordinary reply on another XID.

The private reader rejects malformed headers, changes of type or format and
data above `MAX_DATA_SIZE` (4 MiB). This bound now applies to the complete
accumulated result as well as individual native properties; it does not
silently truncate them or concatenate an unbounded sequence of chunks.
Invalid data retires the affected conversion with an empty result and reports
the error. This is not a new global clipboard memory quota or a limit on the
number of peer requests. The legacy helper-window reader retains its upstream
size and incremental policy, including the companion patch's timer ordering.

## Completion, cancellation and failure lifetime

`take_conversion_request()` removes the pending callback before retiring its
requestor and conversion deadline. `close_requestor()` detaches both XID
lookups, the accepted marker and private INCR reservation before unregistering
the receiver or destroying the native window. The GDK wrapper remains locally
retained through destruction. Native/timer cleanup attempts are independent;
a timer-removal exception cannot prevent another requestor from being retired.
Callbacks left queued by such an error see no active identity. Closing an
already retired ID is harmless.

The main terminal paths remain distinct:

| Terminal path | Callback and resource behavior |
| --- | --- |
| Matching refusal | Remove the request and deadline, destroy its requestor, complete once with the target-appropriate empty result. |
| Complete positive contents | Retire only that requestor and callback, then return filtered contents. |
| Silent-owner deadline | Retire the requestor, retain the existing two-line timeout warning and return the existing empty result. |
| Proxy cleanup | Set the closed flag, destroy active requestors and cancel their deadlines without replaying the abandoned conversion callbacks. |
| Temporary selection/send revocation | Detach all private conversions, then empty-complete their callbacks without a timeout; later reenable may allocate new requestors. |
| New read after terminal cleanup | Return an empty result without allocating another native window or deadline. |

`CONVERT_TIMEOUT` still comes from `XPRA_CLIPBOARD_CONVERT_TIMEOUT`, with the
existing 100 ms default. It is not the remote clipboard timeout or the separate
INCR timer. This case neither increases those values nor adds retries or
polling to conceal an owner which never responds.

Request setup must also be exception-safe. Failure to obtain/register a
required GTK wrapper destroys the new native window and removes any receiver.
If deadline allocation fails, the already created requestor is retired and
the exception propagates without leaving a pending callback. If conversion
setup fails after a callback was registered, that pending request is completed
and retired before the original exception propagates. Synchronizing X11 context
exit is inside the rollback boundary too. A failing consumer cannot replace
that original setup exception.

New conversions require selection enablement and send permission as well as
an open lifecycle. A temporary revocation cancels only these locally sourced
conversions, not the helper's remote-wire requests or selection ownership.
Changing receive permission alone does not cancel a still-permitted local
read. All old callbacks are detached before empty completion, so re-entrant
reenablement cannot expose an old request or accidentally cancel a new one.

After a timeout, an owner attempting to send a late reply to the destroyed
requestor encounters an X11 `BadWindow` rather than reaching a retry on a new
XID. The native test records that exact error on its independent owner
connection, then proves the new request still accepts its own positive reply.
An expected old-window error is confined to that control, not a production
warning suppression rule.

Broader helper, token, remote-request and legacy INCR teardown is still owned
by the surrounding clipboard lifecycle and its existing patch. This case
cancels its private conversion resources first; it does not replace helper
peer revocation or native Wayland pipe cancellation with the closed flag.

## Patch-queue and integration ownership

`fix.patch` changes `xpra/x11/bindings/events.pyx`,
`xpra/x11/selection/proxy.py` and the helper's proxy initialization in
`xpra/x11/selection/clipboard.py`, and adds
`tests/unittests/unit/x11/selection_refusal_test.py`. The manifest has no
dependencies: a balanced test-owned filter makes its refusal control reachable
on embedded clean source without borrowing another case's production repair.

The complete stack places it after
[`x11-client-clipboard-events`](../x11-client-clipboard-events/README.md).
That case owns filter leases, helper-window/XFixes subscriptions, broader
request teardown, native source/pipe lifetime and peer-policy integration.
This case owns the missing core conversion reply and the private native
identity needed to complete it correctly. Both touch the X11 proxy, so review
the composed cleanup and property reader, not only isolated patch application.

In the composed cleanup, private conversions are retired before the companion
case's shared/token/INCR and remote-request teardown. Its existing timer-before-
state-reset ordering remains intact in the shared INCR reader. Keep the two
patches independently applicable; do not export their combined source as one
case or duplicate filter-lease ownership here.

[`wayland-clipboard-token-coalescing`](../wayland-clipboard-token-coalescing/README.md)
owns the rate of ready native owner advertisements. It cannot interpret an
X11 owner's refusal, and this patch cannot bound a Wayland token burst.
The manager-signal and client-codec startup cases likewise remain independent;
their presence in the same full-log oracle is not a dependency.

## Patch ownership and non-goals

The case does not:

- alter XFixes ownership notification, helper filter leases or clipboard direction;
- claim a selection on the private requestor or expose a new application window;
- normalize MIME spelling, invent target contents or force every owner to
  support an alias it legitimately refuses;
- change clipboard packet formats, origin-loop prevention or remote request IDs;
- raise packet budgets, extend deadlines, retry silent owners or hide warnings;
- accept arbitrary synthetic X events or authenticate same-display X11 clients;
- replace the legacy helper-window INCR implementation or add a general cache;
- fix native Wayland I/O, transport loss, an application stall or every possible
  source of a clipboard timeout.

## Focused regression design

`unit.x11.selection_refusal_test` runs the native controls in two fresh
interpreters with the parent's selected import paths. The GTK child uses
`DisplayContext` and a private Xvfb, actual compiled bindings and the real GDK
filter. Its class-level filter lease is independently balanced even when the
filter was already installed. The raw child uses another owned Xvfb, the real
native X11 GLib event source and the server's GTK-import prohibition. It must
finish without importing `xpra.x11.gtk`. Missing native subjects fail, not skip;
process-global display pointers never leak into the surrounding unit suite.

An independent `ctypes` Xlib connection owns CLIPBOARD and PRIMARY. A GLib I/O
watch receives real `SelectionRequest` events. The owner can hold a request,
refuse it with `XSendEvent`, write normal TARGETS/text properties or drive an
INCR transfer by observing property deletion. It does not invoke the new
Xpra handler directly or use Xpra's reply serializer on the owner side.

The actual `X11Clipboard` helper supplies the legacy property route as well.
Without it a clean positive control would fail for missing helper routing,
which would obscure the refusal defect. Requests enter through the existing
proxy `get_contents()` API; the new handler need not exist for a tests-only
control to execute its stimulus.

The 21 native tests execute on both event routes and cover:

| Control | Required observation |
| --- | --- |
| Refused targets | Both UTF-8 MIME spellings and TARGETS complete with the exact empty callback shape on CLIPBOARD and PRIMARY, without timeout warnings. |
| No owner | The X server's refusal completes both selections without a request reaching the raw owner or diagnostic property queries against XID 0. |
| Positive contents | TARGETS, UTF8_STRING and sequential incremental TEXT return the actual owner data for both selections, with one callback and drained transfer state. |
| Identical concurrent conversions | Refusing the first request leaves the second pending; its later positive reply returns its own contents without a timeout. |
| Duplicate negative acknowledgement | A refusal following accepted INCR cannot replace the successful incremental result or cause duplicate completion. |
| Deadline-allocation failure | An injected `GLib.timeout_add()` exception leaves the exact original root children, receiver map and empty local-request state. |
| Mismatched reply metadata | Wrong selection, target, timestamp, property or requestor cannot cancel the request; its subsequent valid positive reply succeeds. |
| Silence and late reply | A genuinely silent owner reaches the real conversion deadline and warning; a late old-window refusal cannot cancel a fresh retry. |
| Pending cleanup | Repeated cleanup removes active conversion timers and receivers and does not later invoke the abandoned callback or emit a timeout. |
| Parallel INCR and ordinary replies | Same/different targets return their own exact bytes; an ordinary result cannot join a paused stream. |
| Partial-transfer expiry and retry | The real conversion deadline retires old partial data; a subsequent ordinary conversion remains intact. |
| Incremental deadline and header | A one-second INCR deadline completes only that request; a zero lower-bound header still transfers data. |
| Size and format errors | The complete-result byte limit and an in-stream format change retire the affected request without falling through to conversion timeout. |
| Timer failure and supersession | Raising removal still attempts all native cleanup; captured stale callbacks cannot complete a retired request or expire a rescheduled stream. |
| Temporary revocation | Disable/send denial empty-completes pending work, denies new reads and allows a successful later reenable. |
| Event-route ownership | A failed required GTK lookup restores root children and receiver state; actual raw conversions never invoke that lookup. |
| Synchronized setup failure | A fault after actual XConvertSelection and native synchronization retires resources and keeps the original error despite a raising consumer. |
| Legacy batch callback failure | All native requestors and deadlines are retired before the first callback; a raising consumer does not prevent the next completion. |

Sequential INCR also checks that the independent owner receives the final
property-deletion acknowledgement, not just that Xpra returned the bytes.
Only the parallel-ordering and isolated INCR-deadline controls lengthen their
test-local conversion deadline; ordinary positive, refusal and silence
controls retain the production default. No production timeout is changed.

The owner uses a fixed public marker, not operator clipboard contents. Bounded
GLib pumping observes asynchronous progress; it is test synchronization, not
production clipboard polling. Native owner errors are checked, with only the
explicit old-requestor `BadWindow` consumed in its dedicated assertion.

The regression's drain checks inspect pending conversions, private XID/INCR
state and native receiver registration, not just returned text. Independent
cleanup registrations balance helper, proxies, filter, raw owner connection,
I/O watch and display even after a failed assertion. The owner stops sending
fixture replies before helper retirement. Pending test work must not outlive
a failing clean-source control.

`PATCH_MODE=tests-only` must expose actual refusals falling through to timeout,
wrong shared-request completion or unretired state. An empty callback alone
would be vacuous because the old timeout produces one too: warning assertions,
request isolation and native lifetime distinguish the repaired result. Positive
text/TARGETS/INCR and unrelated-reply controls remain necessary on clean source.
An import failure, missing new API or disconnected event filter is not the
intended negative result. The additional raw route and property-deletion
controls are positive protocol obligations, not alternatives to observing
the original native refusal failure on clean source.

Both native child modes run the original refused-alias/TARGETS test first with
the same fail-fast ordering on clean and patched source. That makes a real
refusal falling through to timeout the first reported negative observation,
before checks of newly introduced requestor state. Every successful child
still executes all 21 methods exactly once. A wrapping owner-info observer
additionally rejects XID 0 queries in both GTK and raw modes; it does not
replace XConvertSelection, native events or the global X error recorder.

The manifest also retains `unit.x11.clipboard_filter_test` for upstream GTK
filter ownership and raw-server routing, and `unit.clipboard_core_test` and
`unit.client.subsystem.clipboard_test` for protocol/helper and client-adapter
controls. Composed focused checks additionally exercise the companion X11 and
Wayland clipboard cases; the standalone native owner test does not establish
their policy, backoff or transport behavior. SECONDARY is not exercised by
this native fixture even though the generic proxy has no new selection-name
restriction and the live warning oracle also checks that name.

## Durable live boundary

`live-x11-clipboard` is the topical gate declared in `case.toml`, and one
member of the mandatory nine-profile suite. Its X11 client and native-Wayland
server both contain the entire current queue. Isolated case-only or clean
live endpoints are not admitted.

The fixture creates fresh `both`, `to-server` and `off` sessions. It retains
the independent raw X11 local consumer, two same-XID forward owner changes,
real-F8 reverse takeover with compositor confirmation, root XFixes monitoring
and controlled client shutdown/event drain. Unrelated XSettings and XI2 paths
are disabled by the tracked client configuration so they cannot lend the
clipboard helper an accidental event-filter lease.

Allowed forward transfer must work through actual GTK Ctrl+V and context-menu
Paste. Real selection keys then generate a rapid native PRIMARY burst on the
same line. The event-driven X11 consumer must receive the final value under
`both`; reverse-denied policies must retain local contents. Every consumer
request must finish and the pending ledger must drain at shutdown.

Those application controls do not deterministically supply every refusal and
late-reply ordering. The dedicated native regression remains the exact oracle
for this patch, while the live fixture proves the full clipboard path and its
composition with filter, source, backoff and peer-policy repairs.

Collection and the suite controller inspect complete report-bound stdout and
stderr of both peers, including post-input/exit tails. Clipboard rate warnings
and selection timeouts, including PRIMARY and SECONDARY, fail even when a
paste succeeded. The warning checker reports categories and file identity,
not clipboard-bearing log lines. Positive application behavior, full-log
checks and owned cleanup are all required; none substitutes for another.

## Invariants not to simplify

- Admit core `SelectionNotify` specifically and route it to the real requestor.
- Keep selection-owner windows separate from private conversion requestors.
- Correlate replies by owned XID plus selection, target and the request time;
  target spelling or one shared property cannot identify a refused conversion.
- Register and retain each requestor until completion, timeout or cancellation;
  keep GTK lookup conditional on the helper's actual event route.
- Wait for its positive acknowledgement before consuming property events and
  keep that accepted state through INCR completion.
- Ignore duplicate or mismatched replies without completing another request.
- Keep private INCR bytes, type/format and exact timer reservation per request;
  delete the final property before destroying its window.
- Preserve the empty TARGETS versus ordinary-content callback formats.
- Retire native windows, receivers and deadlines before publishing completion;
  do not replay abandoned callbacks during terminal cleanup.
- Keep real silence timeouts, size/filter policy and companion cleanup intact.
- Separate temporary send/selection revocation from terminal cleanup; retire
  an old batch before re-entrant callbacks can admit new work.
- Require native negative/positive controls and complete live delivery/drain/log
  evidence; a shorter delay or one successful paste does not prove the fix.

## Required validation

Follow [development and final acceptance](../../docs/runbooks/validation.md).
During an upstream refresh, finish and record the incremental manual review
and resulting-stack exit gate before runtime validation. Then run the
manifest's tests-only clean control and
patched focused inventory against the same frozen source/image. Exercise
compiled and no-compat focused modes with actual native X11 bindings, plus the
composed X11/Wayland clipboard regressions. A constructed Python event or
direct call to the new handler cannot replace native parser/filter delivery.

Start the complete live suite after focused prerequisites:

```bash
make -C fork-maintenance live-all STACK=develop RUN=<fresh-cycle-prefix>
make -C fork-maintenance live-suite-check STACK=develop RUN=<fresh-cycle-prefix>
```

At candidate freeze, fill missing or invalidated controls, composed/native,
quarantine, fork-control and all three full upstream legs. Require all nine
current positive live profiles, not just the clipboard gate. Follow canonical
evidence-reuse and scheduling rules instead of repeating unchanged expensive
checks after every intermediate edit.

Package/build obligations follow the enclosing contract. The patch changes a
compiled event route but introduces no new package layout or external native
ABI. The [scoped mypy gate](../../docs/runbooks/typecheck.md) does not currently
own these X11 modules or their regression. Keep exact source, queue, image,
named-result identities and transient diagnosis in the ignored cycle ledger,
not this architectural README.
