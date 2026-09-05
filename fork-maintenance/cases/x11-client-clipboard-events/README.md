# Restore X11 client clipboard synchronization

## Boundary

Cross-backend clipboard synchronization requires one complete ownership and
delivery transaction: detect a local X11 owner, obtain its current targets and
data, publish the corresponding native-Wayland offer, and service each current
consumer. An independent native-Wayland owner must remain valid even when
direction policy forbids forwarding it back to X11. Successful negotiation or
a clipboard packet proves only part of that transaction.

On the GTK3 X11 client, `X11Clipboard` owns its raw event window, dispatcher
receiver, XFixes subscriptions, and one lease on the global GDK event filter.
The filter delivers both ownership notifications and conversion property
events to Xpra. Its lifetime cannot depend on optional XSettings or XI2
subsystems: every successful acquisition has an independent count, and only
the final matching release removes the filter.

The raw `InputOnly` window also has a strongly retained GDK foreign wrapper.
Xpra's filter returns `GDK_FILTER_CONTINUE`, so GTK must resolve that event XID
to a valid window and screen when it processes the same XFixes notification.
Without that association, `_gdk_x11_screen_process_owner_change` can receive a
NULL screen; merely routing the event to the clipboard proxy does not make
the later GDK dispatch safe. `StructureNotifyMask` preserves the mapping until
the server-ordered `DestroyNotify`, after earlier queued events have drained.

The applicable client-only installation therefore includes the regular
`xpra.x11.gtk` Python package as well as its compiled extensions. Its initializer
installs the error bridge and stable `get_pywindow` lookup delegate before
`init_gdk_display_source()` binds Xpra to GDK's display. Importing an extension
through an implicit namespace package is not equivalent to executing that
initializer. Package composition, shared filter ownership, and the event-XID
handoff are distinct parts of the same client lifetime.

On the server, `WaylandSelection` and `WaylandPrimarySelection` publish every
successful `set_source`, `clear`, and `send_source` operation with an outbound
display flush. A wlroots call may mutate seat state or queue an offer/request
without delivering it to the native client. `WaylandCompositor.process_events()`
flushes its own event-source callback, but cannot order work queued later by an
Xpra packet or GLib pipe callback. The selection adapter owns that later
publication; unrelated input must never be its trigger.

Local standard `CLIPBOARD` service is base compositor behavior. Its standard
seat request/set-selection listeners have the same unconditional lifetime as
`wl_data_device_manager`, including with `--clipboard=no`. Xpra forwarding,
data-control, and the optional primary-selection protocol retain their feature
gates. A successful `Gtk.Clipboard.set_text` call is only an ownership request;
the compositor's accepted non-NULL selection and the native client's resulting
owner-change event establish ownership independently of forwarding policy.

Every asynchronous data transfer retains its exact source and request
identity. `ClipboardTimeoutHelper` binds an optional completion callback to
the existing wire request ID; legacy X11, GTK, Win32, and macOS proxies retain
target-based delivery when no callback is supplied. A Wayland completion
closes over a private monotonic key, whose live record owns the exact Python
source object, remote generation, target, and FD. Destruction removes that
record and closes the FD, so a late reply cannot reach a replacement source
through a repeated target or reused descriptor. Native-source reads use an
independent key, pointer and local generation; replacement empty-completes old
reads once and stops their eager multi-target collectors, including when the
allocator reuses a source pointer.

Helper reset drains outstanding wire requests while their records remain
resolvable and a re-entry guard prevents new network work. Proxy cleanup
marks the lifetime closing, invalidates both generations, retires pipes,
clears only its still-owned remote source, and disconnects the exact callback
from the compositor's custom event registry. These same boundaries govern
replacement, reset, disconnect and teardown, not only initial synchronization.

The case has no other downstream production dependency. Its positive live
gate installs the selected case at both endpoints because client event
delivery and server publication/request ownership are both required to prove
the complete transaction.

## Upstream provenance

The case is based on the source commit embedded in the current `develop`
history, `212038243d0067b6860ebe7d6953692179ef353f`.  It does not compare or
follow moving master refs.  The relevant implementation is the result of a
maintainer-authored 2025 refactor followed by the 2026 token-state work:

- `3cf525bab37` introduced the original clipboard-owned X11-filter lease and
  count, establishing that clipboard setup may be an independent filter owner;
- `38cee7098df` later moved GDK display and filter ownership into the GTK
  server subsystem and removed that lease from the clipboard helper;
- `53a90b7b95b` delayed GTK imports in the old GTK clipboard helper;
- `9382af1f309` replaced its GTK-created event window with a raw X11
  `InputOnly` window;
- `ba84dd344e5` moved the almost-pure-X11 helper out of `xpra.x11.gtk` and
  introduced the `xpra.x11.common.get_pywindow` injection seam;
- `99ebdfa7637` split the helper into `xpra.x11.selection.clipboard` and
  `xpra.x11.selection.proxy`;
- `19a70cc7216b`, `f9a0d602a318`, `c94ffedda416`, and `84c75612eba8` added
  token backoff, multi-target delivery, generation checks, and target-owner
  isolation which the downstream correction must preserve;
- `1f5f73ee619` introduced the current Wayland compositor/clipboard split,
  including the selection adapter methods and compositor event-loop flush
  boundary; adaptation on the frozen source must preserve the explicit
  selection-adapter publication ownership described below;
- `c732fbd4137` and `d9c1aea45fb` added packet-origin loop detection and
  origin preservation to the Wayland proxies; the publication correction must
  not bypass or weaken that newer ownership design.

The failure is not evidence that the older GTK-owned clipboard-window design
should be restored.  The current raw-X11 boundary is deliberate, and recent
target-generation and backoff behavior is part of the surrounding maintainer
design.  Adaptation after an upstream refresh must first re-read these current
paths and commits; old diagnostic logs prove only this frozen source boundary.

## Surrounding ownership map

The behavior spans several owners which must not be collapsed into one ad hoc
clipboard object:

| Layer | Responsibility |
| --- | --- |
| `xpra/client/subsystem/clipboard.py` | Selects the platform helper, intersects client/server direction policy, configures selections, greedy and target policy, starts initial token synchronization, and transports packets. |
| `xpra/platform/posix/clipboard.py` | Chooses `X11Clipboard` when X11 bindings are active and otherwise falls back to the generic GTK helper. |
| `xpra/x11/selection/clipboard.py` | Owns the raw event window, Xpra receiver registration, XFixes subscriptions, proxies, filter lease, and cleanup ordering. |
| `xpra/x11/selection/proxy.py` | Implements local owner-change state, X selection conversions, target/data caches, token scheduling, generation invalidation, and remote-selection ownership. |
| `setup.py` and `xpra/x11/gtk/__init__.py` | Make the GTK/X11 Python adapter present in every applicable client install and inject the GDK window lookup. |
| `xpra/x11/common.py` | Provides a toolkit-neutral, stable lookup function used by early importers. |
| `xpra/x11/gtk/display_source.pyx` | Binds Xpra's Xlib calls to GDK's X11 display connection. |
| `xpra/x11/gtk/bindings.pyx` | Creates GDK foreign-window wrappers and owns the process-global raw X11 event filter. |
| `xpra/x11/bindings/fixes.pyx` | Registers and parses the XFixes extension event and selects owner-change notifications. |
| `xpra/x11/bindings/events.pyx` | Registers core X11 parsers/signals and turns an `XEvent` into an `X11Event`. |
| `xpra/x11/dispatch.py` | Routes the parsed event by delivered XID to the registered `X11Clipboard` GObject signal. |
| `xpra/clipboard/proxy.py` and `xpra/clipboard/core.py` | Own shared loop prevention, direction checks, token/request semantics, and wire-facing selection mapping. |
| `xpra/clipboard/timeout.py` | Owns wire request IDs and timeout sources.  It preserves legacy target delivery but may bind one exact completion callback to a request; reset and cleanup drain each record once under a re-entry guard. |
| `xpra/wayland/server/clipboard.pyx` | Adapts ordinary and primary wlroots selection APIs, owns local/native and remote/Xpra source generations, source wrappers, pipe watches and FDs, and publishes every outbound source mutation or request. |
| `xpra/wayland/server/compositor.pyx` | Creates the standard data-device global and seat, accepts standard client selection requests, emits compositor selection changes through its custom connect/disconnect registry, and separately feature-gates optional forwarding/data-control/primary-selection facilities. |
| wlroots and `wl_display` | Mutate seat state synchronously but queue protocol events and file descriptors for clients; explicit display flushing is the outbound publication boundary, not an ownership or event-dispatch substitute. |

The global filter is shared process state. Every successful acquisition owns
one independent lease, and cleanup releases only that lease. The counter
increments on every acquisition; only the zero-to-one transition installs the
filter and only the one-to-zero transition removes it. Clipboard setup and
that shared counter contract are tested together with two real owners.

There are three different meanings of "window" at this boundary.  The X
server owns the integer event-window XID.  Xpra's dispatcher owns a receiver
entry keyed by that XID.  GDK owns a foreign-window wrapper which associates
the same XID with its display and screen.  Creating any two of these does not
implicitly create the third.

Likewise, selecting an event is distinct from consuming it.  The X server can
enqueue an XFixes event correctly even when no GDK filter calls Xpra's parser,
and Xpra can parse an event correctly while leaving GDK unable to translate it
after the filter returns.  Controls and tests must establish each boundary
independently.

The server has the same distinction in the opposite direction.  Dispatching
the Wayland event-loop file descriptor accepts inbound client requests; it
does not prove that a later wlroots operation performed from an Xpra packet or
GLib pipe callback has flushed its queued protocol effect.  Conversely,
flushing delivers already queued output but must never be used to dispatch
input recursively or to manufacture a source transition which the seat did not
accept.

## Client initialization and package composition

The GTK3 client calls `xpra.gtk.util.init_display_source(False)` during
`client_base` import.  On X11 this imports
`xpra.x11.gtk.display_source.init_gdk_display_source`.  A normal Python package
import executes `xpra/x11/gtk/__init__.py` first; that initializer installs the
GDK error bridge and the `get_pywindow` adapter before the display source binds
the raw Xlib display to GDK's display.

The clean client-only build violated that assumption.  Its compiled
`xpra.x11.gtk.*` extensions were present, but the `xpra.x11.gtk` Python package
was enabled only by `server_ENABLED and gtk_x11_ENABLED`.  Python could load
the extension from a namespace package without executing the omitted
`__init__.py`.  This is why source-tree tests or a combined client/server build
alone cannot prove the installed-client boundary.

The package condition must include a GTK X11 client as well as a server.  It
must remain conditional on the existing GTK/X11 build feature; this case does
not make GTK a dependency of a deliberately non-GTK X11 consumer.  A future
package split is acceptable only if the client still receives the initializer
and error bridge which its compiled GTK/X11 extensions require.

There is a packaging trap here which an import-only source test cannot expose.
The Cython extensions `xpra.x11.gtk.display_source` and
`xpra.x11.gtk.bindings` are enabled directly by `gtk_x11_ENABLED`.  Python can
therefore resolve those extension modules below an implicit namespace package
even when the regular package containing `xpra/x11/gtk/__init__.py` was omitted
from the installed file set.  A successful import of either extension is not
proof that the initializer ran.  The package predicate must remain
`(server_ENABLED or client_ENABLED) and gtk_x11_ENABLED`, and acceptance must
inspect or execute a real client-only installation where `server_ENABLED` is
false.

Display-bound tests have the inverse trap.  `DisplayContext` starts Xvfb, sets
`DISPLAY` and `GDK_BACKEND=x11`, opens or replaces GDK's default display, and
then calls `init_gdk_display_source`; on exit it closes that display source and
the GDK display before terminating Xvfb.  Imports which instantiate X11
binding singletons or cache a display pointer must therefore remain inside the
active context.  Toolkit-neutral `xpra.x11.common` may deliberately be
imported earlier to exercise the stable delegate, but
`xpra.x11.selection.proxy`, `xpra.x11.gtk.bindings`, and the XFixes singleton
must not be initialized against the operator display or a display which the
test later closes.

The compiled bindings also carry process-global C state: the filter lease
count and the one-time core and XFixes event registration do not become fresh
Python objects for every test method.  A module-level single `DisplayContext`,
lazy display-bound imports, and fully balanced leases are part of the test
isolation contract.  Repeatedly opening unrelated Xvfb displays in one
interpreter, or simulating an XFixes-missing display before the positive test,
can poison one-time extension state and produce a result unrelated to this
case.

## Stable toolkit lookup and import order

The original injection seam assigned a new function to
`xpra.x11.common.get_pywindow`.  That works only for code which looks up the
module attribute after injection.  Modules such as the selection manager use
`from xpra.x11.common import get_pywindow`; an early import keeps the old
function object forever, so later assignment silently fails to update that
consumer.

The correction keeps `get_pywindow` itself stable and changes a private
delegate through `set_pywindow_lookup`.  Both early and late importers then call
the active toolkit adapter.  `has_pywindow_lookup` distinguishes an installed
toolkit adapter from the intentionally toolkit-neutral fallback; checking the
return value of the fallback is insufficient because the historical fallback
returns an opaque object rather than `None`.

These properties are deliberate:

- do not replace the public function object after another module may have
  imported it;
- reject a non-callable delegate without discarding the last valid delegate;
- allow the pure-X11 path to exist without importing GTK bindings;
- require a real GDK wrapper when the GTK adapter is installed;
- retain `self.window` strongly for the event window's whole lifetime so the
  GDK XID/display association is not garbage-collected early.

The lookup is process-global because the GDK display source is process-global.
This case does not attempt to support switching the active toolkit or X display
inside one live client process.

## Display connection and event route

`init_gdk_display_source()` obtains the default `GdkDisplay`, extracts its
`Display *`, and publishes that pointer through Xpra's X11 display source.
`X11WindowBindings`, `XFixesBindings`, `get_pywindow`, and the global GDK filter
therefore operate on the same effective X display connection in a GTK X11
client.  A fix which opens a second private display would split the request,
subscription, and GDK event queues and is out of scope.

For a local owner change, the required route is:

1. the application calls `XSetSelectionOwner` for `CLIPBOARD`;
2. the X server sends `XFSelectionNotify` to the Xpra event-window XID selected
   by `XFixesSelectSelectionInput`;
3. GDK's process-global filter invokes `parse_xevent` on that same queue;
4. `init_xfixes_events` supplies the extension parser and signal mapping;
5. `route_event` uses `event.window` / `event.delivered_to` to find the
   `X11Clipboard` receiver registered for the event-window XID;
6. `do_x11_xfixes_selection_notify_event` selects the named proxy;
7. `ClipboardProxy.do_selection_notify_event` validates owner, enablement, and
   `can-send` before entering the token state machine.

The global filter returns `GDK_FILTER_CONTINUE` for all parsed events so GDK can
perform its own normal event handling.  Changing clipboard events to
`GDK_FILTER_REMOVE` would hide the absent GDK window registration and could
swallow unrelated toolkit behavior.  The event-window wrapper is the ownership
repair which makes `CONTINUE` safe.

## Event-window and XFixes subscription ownership

The clipboard event window is an X11 `InputOnly` child of the root with
`PropertyChangeMask | StructureNotifyMask`.  The property mask is needed
because local selection owners write converted data to properties on this
requestor.  The structure mask makes the X server send `DestroyNotify` for the
owned raw window, allowing GDK to retire its foreign-window XID mapping in
server event order.  Each enabled selection is subscribed on this dedicated XID through
`XFixesSelectSelectionInput`.  The exact mask is the bitwise union of
`XFixesSetSelectionOwnerNotifyMask` (numeric value `1`),
`XFixesSelectionWindowDestroyNotifyMask` (`2`), and
`XFixesSelectionClientCloseNotifyMask` (`4`).  In other words, the binding
passes mask `7`: explicit replacement, destruction of the owner's window, and
closure of the owner's client are all ownership transitions.  A controlled
same-owner update normally arrives as subtype `0`, but production code must
not narrow the subscription to that live-fixture subtype.

GDK's foreign-window table holds its own reference after the helper releases
the Python wrapper.  That is intentional: XFixes notifications already queued
before `XDestroyWindow` still resolve a valid screen, then the later
`DestroyNotify` marks the wrapper destroyed and removes the mapping.  Omitting
`StructureNotifyMask` leaves a stale GDK mapping; destroying the wrapper
manually or swallowing the XFixes event can instead remove it too early.

The clean code also called `selectSelectionInput`, which passed the numeric
core event type `SelectionNotify` to `XSelectInput` as if it were a mask.
`SelectionNotify` is a directed protocol event, not a selectable event-mask
bit.  The call neither establishes the conversion path nor substitutes for
`PropertyChangeMask`; it can select unrelated mask bits by accident and is
removed from event-window construction.

The second XFixes subscription on the root window was also unused.  Clipboard
dispatch is registered for the dedicated event-window XID, not the root, so
root-delivered notifications had no clipboard receiver.  Subscribing both
locations duplicated observations for raw probes and left a root subscription
alive after the dedicated window was destroyed.  The correction owns exactly
one subscription per proxy on its event window.  Destruction of that owned
window lets the X server remove those subscriptions; cleanup must not modify
unrelated root consumers.

Missing XFixes remains an existing degraded-configuration boundary rather
than a new hard startup failure.  `xfixes_selection_input` returns `False`
after either an extension import failure or a negative `hasXFixes()` query,
emits the bounded first-time warning, and does not subscribe.  The helper
currently does not reject construction on that return value.  This case must
not silently change that policy while repairing the GTK filter lifecycle.
Conversely, the real X11 regression names XFixes as the behavior under test,
so it must fail rather than skip when its controlled Xvfb image lacks the
extension.  `init_xfixes_events()` caches its first query in Cython static
state, which is another reason that the positive regression has to initialize
it only after `DisplayContext` owns the intended display.

## Selection conversion and `send_event`

After an owner change, a target-bearing token requires a real X11 selection
conversion:

1. `get_contents("TARGETS")` allocates a request generation and timeout;
2. `XConvertSelection` asks the current owner to write
   `CLIPBOARD-TARGETS` on the Xpra event window;
3. the owner writes that property, generating `PropertyNotify` because the
   window selected `PropertyChangeMask`;
4. `do_property_notify` reads and deletes the property, decodes atoms, and
   resolves all callbacks for that target;
5. the proxy verifies that the owner and selection generation are still
   current before emitting a token.

Ordinary X11 owners also send a synthetic `SelectionNotify` to the requestor.
The current generic parser drops most events whose `send_event` flag is set,
including that `SelectionNotify`.  Protocol probes proved this fact, but also
proved that the associated property write and routed `PropertyNotify` complete
both `TARGETS` and `UTF8_STRING` conversions.  The parser policy is therefore
not the first failing boundary in this case and is deliberately unchanged.

Do not infer a conversion failure from the 100 ms timeout alone.  The clean
failure timed out because neither XFixes nor property events entered Xpra's
dispatcher; an independent consumer on the same display completed promptly.
Increasing `CONVERT_TIMEOUT` would only delay the same failure and weaken
diagnostics.

## Filter lease and cleanup lifecycle

The GDK raw-event filter is process-global shared state.  XSettings/display,
XI2, an X11 server subsystem, and the clipboard helper may all need it at the
same time.  `init_x11_filter` is therefore an acquire operation, not a
"first caller won" probe.  Every successful call owns one lease and must return
success to its caller; only the zero-to-one transition installs the actual GDK
filter.  Every owned cleanup releases one lease; only the one-to-zero transition
removes the filter.  An unmatched cleanup is rejected without underflowing the
counter or removing anything.

The old implementation incremented the counter only for the first caller and
returned true whenever the stored count happened to equal one.  Multiple
callers could all believe they owned the same single count, allowing the first
cleanup to remove the filter from every survivor.  The regression must cover a
peer lease, clipboard acquisition, peer release, and a later clipboard event.

The frozen source has these callers and lifetimes; an upstream adaptation must
repeat this inventory rather than assuming the clipboard is the only user:

| Caller | Acquisition boundary | Matching lifetime |
| --- | --- | --- |
| `GtkX11Server` in `xpra/x11/subsystem/gtk.py` | Unconditionally during GTK X11 server setup, after the GDK display source is initialized. | Releases in subsystem cleanup, before late cleanup closes the GDK display source. |
| `X11DisplayPropsWatcher` in `xpra/platform/posix/display.py` | After handshake, only when XSettings, workarea, desktop, or stacking properties actually require an X11 watcher. | Stores its own boolean lease and releases it in watcher cleanup.  This is the optional path whose presence masked the clipboard omission. |
| `XI2Client` in `xpra/client/subsystem/xi2.py` | After handshake and successful XI2 selection/injection; disabled by `input-devices=noxi2`. | Stores and releases its own lease in XI2 subsystem cleanup. |
| `X11Clipboard` in `xpra/x11/selection/clipboard.py` | During helper construction when the GTK `get_pywindow` adapter is installed. | Releases only its own lease on normal cleanup or constructor rollback, after its receiver and event window are retired. |
| `ManagerSelection.main` | The standalone selection-manager utility acquires after initializing its GDK display source. | Process-lifetime one-shot; it has no shared in-process teardown path in this entry point. |
| `gtk/examples/window_focus.py` | Diagnostic example setup on X11. | Process-lifetime example lease; not an owner on the normal client path. |

The first four are independently composable production owners.  A boolean on
one subsystem records whether that subsystem owns a lease; it is not a mirror
of the process-wide count.  The process-lifetime utilities still matter when
changing the API, but their absence of an explicit release is not permission
for another owner to perform a global reset.

`X11Clipboard` initializes its XID, wrapper, and lease fields before any
fallible setup.  Construction then follows this order:

1. detect whether a real toolkit lookup is installed;
2. acquire the clipboard's own filter lease when it is;
3. create the raw event window under `xsync`;
4. create and retain the GDK foreign wrapper;
5. register the Xpra receiver;
6. initialize the core helper and proxies, recording each helper-facing signal
   handler as soon as it is connected.

The window factory destroys a just-created XID if its diagnostic property
cannot be set.  A later constructor exception disconnects handlers and cleans
every completed or in-flight proxy before removing the receiver/window and
releasing the filter lease; rollback continues through all owned resources
without replacing the original exception.  Proxy cleanup delegates to the
shared core cleanup so token and unblock timers cannot survive a failed or
normal helper lifecycle.  The X11 proxy additionally owns an `INCR` transfer
timer and one `XConvertSelection` timeout for every entry in
`local_requests`; neither belongs to the helper's network-timeout table.

The similarly named request tables cross different boundaries and must not be
merged or drained with one generic callback loop:

| State | What it represents | Completion and teardown owner |
| --- | --- | --- |
| Proxy `_emit_token_timer` and `_block_owner_change` | Deferred local-owner announcement and the short loop-prevention embargo. | `ClipboardProxyCore.cleanup` removes both GLib sources. |
| Helper `_clipboard_outstanding_requests` | A `clipboard-request` already sent over the Xpra connection, keyed by wire request ID and guarded by `REMOTE_TIMEOUT`; a record may also own one request-specific completion callback. | `ClipboardTimeoutHelper` consumes `clipboard-contents` / `clipboard-contents-none`, removes the timer, and calls that exact callback when present or the legacy selected proxy otherwise.  Reset and cleanup drain each still-live ID once as empty while a guard makes re-entrant requests complete locally instead of recreating network work. |
| Wayland proxy `pending_writes` | One native consumer FD waiting for the response to one exact remote request.  Its unique local key records the Python source identity, remote generation, target, and FD; it is never a target-wide fan-out table. | The request-bound helper callback atomically pops only its key.  It writes only if source identity and generation are still current; source destruction or cleanup first pops and closes the FD, making a late response harmless even after numeric FD reuse. |
| Wayland proxy `pending_reads` | One asynchronous pipe read from a native source, keyed independently of its numeric FD and bound to the local source pointer and monotonic ownership generation. | EOF returns data only for the still-current pointer/generation.  Replacement removes the GLib watch, closes the read FD, and empty-completes once; cleanup retires it without starting replacement work. |
| X11 proxy `remote_requests` | One or more local X11 `SelectionRequest` consumers waiting for a target which Xpra must fetch from its remote peer.  Requests for the same target share one wire request. | `got_contents` writes each requestor property and sends `SelectionNotify`; proxy cleanup gives every still-attached requestor one empty response, then clears the table.  There is no independent timer in this table. |
| X11 proxy `local_requests` | Xpra's own `XConvertSelection` operations against the current local X11 owner, keyed by target and local request generation. | Routed `PropertyNotify` or `CONVERT_TIMEOUT` completes the callback.  Cleanup atomically detaches the table and removes its GLib sources without calling those callbacks. |
| X11 proxy `incr_data_*` and `incr_data_timer` | The currently accumulated incremental property transfer for a local X11 conversion. | Every chunk refreshes the one-second watchdog.  Completion or an error cancels the source before resetting the fields; cleanup does the same. |

Proxy teardown cancels the `INCR` source before clearing its numeric source ID,
then resets the accumulated size, type, and chunks.  It resolves every pending
local-X11 request for remote data with an empty selection response while the
live `remote_requests` table is still authoritative.  Local conversion
requests are different: cleanup first
detaches the whole table and removes every saved GLib source without invoking
the completion callbacks.  Calling those callbacks during teardown can run the
eager-target collector synchronously, issue another `XConvertSelection`, and
create a fresh timeout after cleanup has begun.  Repeated cleanup therefore
has no request to answer, callback to run, or source to remove.

Normal cleanup first disconnects proxy-to-helper signals and cancels proxy
requests and timers through `ClipboardTimeoutHelper`, then unregisters and
destroys the raw event window, drops the Python wrapper reference, and finally
releases the clipboard's filter lease.  GDK drops its table reference only on
the ordered `DestroyNotify`.  Cleanup is idempotent.  It never releases a
peer's lease and never performs process-wide receiver cleanup.

## Token state, generations, and loop prevention

`ClipboardProxyCore.do_owner_changed` already decides whether an owner change
needs a token for `_have_token`, greedy, and want-targets modes.  The X11
override historically called that method and then unconditionally scheduled a
second token.  When the first schedule executes immediately, no timer remains
to coalesce the second call, so one XFixes notification can produce duplicate
packets and unnecessary conversions.

The X11 handler records before calling the core whether only the plain
first-token path still needs explicit scheduling.  It lets the core own all
target-bearing, replacement, and remote-token cases, then schedules only that
remaining plain case.  This preserves clients which neither request targets nor
operate greedily while avoiding a second schedule for clients which do.

Owner changes also increment `_selection_generation`, clear target/data caches,
and reset `_targets_owner`.  Delayed target callbacks compare both generation
and current owner before sending.  Backoff intentionally collects state after
the delay so a burst publishes the newest owner.  Future adaptation must not
replace these guards with unconditional immediate emission, reuse a remote
target list for a local owner, or remove `_block_owner_change`; those are the
loop, stale-data, and ownership-storm boundaries around this fix.

Direction policy remains layered rather than inferred from event presence:

- `to-server` and `both` set `_can_send` on the X11 client proxy;
- `to-client` and `both` set `_can_receive` and permit claiming the local X11
  selection for remote data;
- `off` disables helper transport entirely;
- an XFixes event can still be observed while the proxy correctly declines to
  send because `_can_send` is false.

## Cross-peer token, request, and data flow

An ownership packet announces which peer should represent the selection; it
does not guarantee that every future paste byte was embedded in that packet.
The eager and on-demand paths share the same policy checks and must both remain
valid.

For X11-client to Wayland-server transfer, the complete route is:

1. the XFixes route above invalidates the X11 proxy generation and schedules
   one ownership announcement;
2. when the peer wants targets or is greedy, the X11 proxy performs real local
   `TARGETS` conversions and, for eager targets, further local conversions;
3. the client helper translates the selection name, filters and marshals the
   targets/data, and the client subsystem sends the resulting clipboard
   packet only while clipboard sharing remains enabled;
4. the server subsystem rejects readonly, non-owner, disabled, or stale-client
   packets before scheduling its helper on the main loop;
5. the Wayland proxy accepts the token only when its receive direction allows
   it, creates a wlroots data source for the advertised MIME types, and sets
   that source on the compositor seat, then flushes the display so the new
   offer is published without waiting for an unrelated compositor event;
6. a native-Wayland consumer request is satisfied immediately from eager data
   when present; otherwise the source's send callback emits
   `clipboard-request` back to the X11 client;
7. the client matches that request to its X11 proxy, performs
   `XConvertSelection` against the still-current local owner, and answers with
   `clipboard-contents` or `clipboard-contents-none`; the server matches the
   wire request ID to its request-bound completion, then writes only the FD
   owned by that still-current Wayland source object and generation.  A reply
   for a destroyed source has no live local key and cannot reach a later FD
   merely because its target or descriptor number is equal.

For Wayland-server to X11-client transfer, the ownership direction reverses
but the on-demand packet pair does not:

1. the compositor's seat selection signal advances a local generation and
   supplies the new native Wayland source pointer; the Wayland proxy cancels
   reads from the previous generation, reads the new MIME types, excludes the
   private origin MIME, and calls its owner-change path only after a
   generation-matched origin read is complete;
2. the server helper sends an ownership announcement only with its negotiated
   send authority; the client subsystem hands an accepted packet to its X11
   helper;
3. with receive authority and `claim=true`, the X11 proxy caches advertised
   targets/eager data and claims `CLIPBOARD` using its event-window XID;
4. a local X11 consumer then generates `SelectionRequest`.  The proxy writes
   cached data immediately, or records the requestor in `remote_requests` and
   sends one `clipboard-request` to the Wayland peer for that target;
5. the Wayland helper reads the native source through a unique generation-bound
   pipe record and flushes the native source request to its client before
   waiting for pipe data.  Sequential eager collection checks the same
   generation before requesting each next target.  Only a current completion
   returns `clipboard-contents`; the X11 proxy then writes the requestor
   property and sends the protocol `SelectionNotify`.

This is why the reverse live boundary must prove both the Wayland compositor
ownership transition and the later X11 owner/conversion.  Merely calling a
fixture's `Gtk.Clipboard.set_text`, seeing a token counter, or matching bytes
already held by the original X11 owner proves only a prefix of this route.

The ownership announcement has two wire representations selected when
`xpra.net.common` reads `XPRA_BACKWARDS_COMPATIBLE` at import time:

| Mode | Ownership announcement | Semantics which must remain equivalent |
| --- | --- | --- |
| Compatibility mode (`BACKWARDS_COMPATIBLE=true`) | `clipboard-token` with positional selection and optional targets.  The legacy form can embed at most one eager data item; older short forms default to claiming the selection.  Newer positional fields carry `claim`, `greedy`, and optional synchronous-client behavior. | Direction still controls the sender's `claim`; a missing eager item must fall back to the request/contents round trip.  This format has no modern origin field. |
| No-compat mode (`BACKWARDS_COMPATIBLE=false`) | `clipboard-data` with an options dictionary containing `claim`, `greedy`, a bounded origin, optional targets, and a map of all accepted eager data items.  `token` defaults true when the receiver processes this packet. | The bounded origin is remembered and rejected on a loop, target filtering and size limits still apply, and the same on-demand fallback remains available. |

`clipboard-request`, `clipboard-contents`, and `clipboard-contents-none` are
the request/data response pair in both modes.  Compatibility mode registers
both token representations, while no-compat mode deliberately does not
register `clipboard-token`.  Focused assertions therefore recognize the
representation selected by the test process rather than hard-coding the
legacy name; `focused-no-compat` exercises the modern `clipboard-data`
announcement and absence of the legacy handler during development, while
`full-no-compat` retains complete-queue final coverage. An upstream
adaptation must preserve behavior across both representations instead of
making the X11 event fix depend on one packet layout.

## Wayland publication, listener, and ownership lifecycle

### Outbound display queue

A successful wlroots function call and a delivered Wayland protocol event are
different boundaries.  The compositor's `wl_display` owns an outbound queue
per client.  `wlr_seat_set_selection`,
`wlr_seat_set_primary_selection`, `wlr_data_source_send`, and
`wlr_primary_selection_source_send` may update server-side state or enqueue a
request synchronously, but the affected GTK client cannot observe the queued
message until the display is flushed.

The compositor normally calls `process_events()` from its Wayland event-source
callback, which dispatches inbound work and then flushes clients.  Clipboard
tokens, empty-token cleanup, source pipe callbacks, and Xpra connection
callbacks can run later from GLib without another readable compositor FD.
Depending on the next input event for publication therefore creates an
unbounded and order-dependent clipboard state.  The selection adapters must
own the publication boundary for every operation they enqueue:

| Selection | Adapter operation | Protocol-facing effect | Required order |
| --- | --- | --- | --- |
| `CLIPBOARD` | `WaylandSelection.set_source` | Installs Xpra's wlroots data source and queues the new standard data-device offer/selection. | Allocate the replacement and publish its proxy identity/generation first.  The seat setter may synchronously destroy the old source; after it installs the replacement and queues its signal/offer, flush before returning. |
| `CLIPBOARD` | `WaylandSelection.clear` | Replaces the seat selection with NULL and queues cancellation of the old offer. | Invoke only while the seat still owns this proxy's source.  wlroots synchronously destroys that old source inside the setter, installs NULL and queues its signal/offer; flush after the setter returns.  An explicit wrapper destroy is then only an idempotent fallback for an unavailable seat/display. |
| `CLIPBOARD` | `WaylandSelection.send_source` | Requests the selected MIME from a native standard data source using the supplied pipe FD. | Call the source send API, flush the request while the source and FD are valid, then let the caller close its local write descriptor. |
| `PRIMARY` | `WaylandPrimarySelection.set_source` | Installs Xpra's primary-selection source and queues its offer. | Publish the replacement identity/generation, call the primary setter which may synchronously destroy the old source, then flush the installed offer. |
| `PRIMARY` | `WaylandPrimarySelection.clear` | Replaces the primary selection with NULL and queues cancellation of its offer. | Apply the same synchronous destroy → NULL/install-and-signal → post-call flush order, with the same idempotent explicit-destroy fallback. |
| `PRIMARY` | `WaylandPrimarySelection.send_source` | Requests a MIME from a native primary-selection source through its pipe FD. | Send, flush while the request resources are live, then close through the existing caller ownership path. |

This symmetry matters even though the live fixture asserts standard
`CLIPBOARD`.  Both proxy classes share the same lifecycle and either selection
can be negotiated; repairing only the visible standard path would retain the
same stale-offer and stalled-source defect for `PRIMARY`.

`wl_display_flush_clients()` is deliberately narrower than
`WaylandCompositor.flush()`: it publishes already queued output and does not
dispatch input, iterate GLib, or re-enter clipboard callbacks.  It is also not
a round trip.  Returning normally from this void call cannot prove that the
client processed an offer; the live sink result, compositor selection signal,
and eventual cross-peer conversion remain the behavioral authorities.

### Standard data-device ownership when forwarding is off

There are two independent feature decisions which the embedded source had
accidentally coupled:

1. the compositor advertises the standard `wl_data_device_manager` for local
   Wayland data-device behavior, including clipboard selection and drag and
   drop;
2. Xpra may create a clipboard forwarding helper, advertise the privileged
   data-control manager, and advertise/manage the optional primary-selection
   protocol.

The first decision is base compositor behavior and is still required under
`--clipboard=no`.  The seat's standard
`L_REQUEST_SET_SELECTION` listener must accept a valid client request through
`request_set_selection` and `wlr_seat_set_selection`; the standard
`L_SET_SELECTION` listener must then publish the resulting source pointer
through `set_selection` and the compositor signal.  Those two listeners belong
beside unconditional seat/data-device creation.  The data-control manager,
primary-selection manager and its `L_REQUEST_SET_PRIMARY_SELECTION` /
`L_SET_PRIMARY_SELECTION` seat listeners, and the Xpra clipboard helper remain
conditional.  Making the standard listeners unconditional does not enable
network clipboard forwarding and does not grant reverse authority under the
`off` policy.

For the controlled live owner, the authoritative sequence is:

1. the private command arms one fixed marker ID but does not touch the
   clipboard;
2. F8 crosses the real Xpra input path and supplies a current compositor
   serial to the native GTK key callback;
3. `Gtk.Clipboard.set_text` issues the standard data-device request and the
   fixture records `owner-set` only as an API-attempt boundary;
4. the compositor request listener installs the non-NULL source, its set
   listener observes that source, and the display publishes the corresponding
   standard selection event;
5. GTK receives that compositor event and the fixture's owner-change callback
   verifies the controlled length/digest before recording `owner-confirmed`.

GTK3's Wayland owner-change signal does not expose an X11-style owner XID and
is not sufficient by itself to distinguish an arbitrary replacement or NULL
offer.  It becomes authority here only because the runner binds it to the
single pending F8 command, the non-NULL compositor transition, the expected
fixed-marker metadata, and the bounded event order.  `owner-set`, a successful
self-read before the compositor response, or a sleep after `set_text` is never
equivalent evidence.

Under `both`, the confirmed native source must subsequently create the third
XFixes takeover and satisfy the raw reverse conversion.  Under `to-server` and
`off`, confirmation proves that the Wayland compositor accepted an independent
native owner even though policy forbids Xpra from taking the X11 selection;
the XFixes monitor must remain at exactly the two local same-XID updates.  This
makes the negative policy result non-vacuous: it cannot pass merely because
the Wayland owner operation itself was broken.

### Publication and policy completion boundaries

Each permitted ownership transition must complete its own offer and data
delivery before the fixture advances to the next controlled transition. The
following order distinguishes current protocol completion from a token,
source pointer, API attempt or stale cached value alone:

| Policy and phase | Required ordered transition | Independent authority |
| --- | --- | --- |
| `both` or `to-server`, forward | Same-XID X11 owner update and advancing timestamp, local target/data conversion, accepted token, replacement Wayland source, outbound flush, then native sink equality for that generation. | A new server pointer cannot substitute for delivery of its current offer; repeating the initial marker must establish a later generation rather than reuse the first read. |
| `both`, reverse | F8 input callback, standard seat source installation, native `owner-confirmed`, third XFixes takeover by the Xpra event window, then exact raw X11 reverse conversion. | The native owner's on-demand request must itself be flushed by `send_source`; successful ownership on both peers alone does not prove data delivery. |
| `to-server`, reverse control | F8 and native owner confirmation, followed by an unchanged original X11 owner and exactly the two local same-XID monitor events. | Local Wayland ownership completes while direction policy forbids a reverse Xpra claim. |
| `off`, both directions | All forward reads remain absent; F8 still produces a compositor-accepted native owner, while the original X11 owner and its two local transitions remain unchanged. | With no forwarding helper, the standard compositor listener path must independently complete before blocked transfer can count as policy proof. |

In this controlled sequence, a GTK `SelectionBuffer` cancellation while a
requested transfer is still pending does not satisfy that transfer. Likewise,
a broken pipe after an ownership timeout and forced teardown is not successful
`off` behavior. Every scenario retains the zero-length fixture-stderr contract;
neither warning is environmental noise to discard or allowlist.

### Source and cleanup ordering

The Wayland source lifecycle is represented by five authorities which must
stay distinct: the seat identifies the currently active source; the proxy
records whether that pointer is its own remote/Xpra source or a native local
source; the Python/Cython wrapper owns the object Xpra allocated; monotonically
increasing local and remote generations disambiguate allocator reuse; and the
pending read/write tables own their exact GLib source or FD.  Pointer equality
alone is not a generation and target equality is not request identity.

For remote-token replacement the exact order is:

1. allocate the replacement wrapper and MIME array;
2. increment `remote_generation` and publish the replacement object and
   pointer in the proxy;
3. call `wlr_seat_set_selection` or its primary equivalent;
4. wlroots synchronously destroys the old active source from inside that call;
   its destroy callback closes only writes owned by the old Python object and
   does not clear fields already pointing at the replacement;
5. wlroots installs the replacement, emits/queues the selection state, and the
   adapter flushes clients after the setter returns.

For an empty token, reset, or cleanup, `clear_remote_source` first retains the
old object/pointer and increments the remote generation.  It may call the seat
clear only if `local_source_ptr` still equals that owned remote pointer;
otherwise a newer native owner is authoritative and only the superseded
wrapper is destroyed.  On the active path wlroots normally destroys the old
source synchronously while replacing the seat value with NULL, then queues the
selection signal and the adapter flushes after return.  Calling the wrapper's
idempotent `destroy()` afterward is a no-op in that normal path and releases it
only when the adapter had no live seat/display and therefore could not perform
the clear.  This is not a fictional clear → flush → destroy sequence: the
wlroots setter owns synchronous destruction.

Each remote source-send request receives its own local `write_key` and helper
completion.  The pending record stores the exact source object, remote
generation, target, and FD.  Source destruction or cleanup atomically removes
and closes matching records; a response/timeout then finds no key and performs
no I/O.  The closure never captures a raw FD as its authority, so OS-level FD
reuse cannot redirect an old response into a new source.  Keeping the wire
request alive until response or the bounded `REMOTE_TIMEOUT` is safe because
its local callback has already become a no-op; immediate network cancellation
would require a separate packet/API contract and is not invented here.

Each native-source read similarly receives a unique `read_key` and records the
local generation, source pointer, read FD, GLib watch, and callback.  Every
seat selection signal increments the generation before retiring old reads.
Replacement removes each watch, closes its FD, and empty-completes the callback
once; EOF can return accumulated bytes only while both generation and pointer
remain current.  Origin reads and every step of eager sequential collection
use the same predicate, so cancellation cannot make an old collector request
its next target from a new owner which happens to reuse the pointer.

Cleanup first marks the proxy closing and advances both generations, preventing
new reads, writes, or selection callbacks.  It cancels core timers, pipe
watches, and pending FDs; clears only a still-owned remote seat source; then
unregisters the exact callback from `WaylandCompositor`'s custom event-list
registry while the compositor still exists.  That registry is not a GObject
signal API: `connect` returns the exact opaque handler registration consumed by
`disconnect(event_name, registration)`.  Emission iterates a snapshot so a
callback removed during lifecycle work cannot skip an unrelated peer.
Repeated cleanup is a no-op.

On a NULL native source or unavailable display, `send_source` closes the
supplied FD and returns; the caller's guarded close tolerates that handoff.
With a valid source it queues the source request and flushes before the caller
closes its write side; the read watch owns and closes its separate descriptor.
Ordinary and primary sources follow the same set/clear/send and generation
rules, but the standard listener lifetime must not be inferred from whether
the optional primary or data-control managers were created.  Compositor
cleanup detaches its wlroots listeners, then destroys the seat/display in its
existing order; the unconditional standard listeners gain no private cleanup
shortcut and optional managers gain no ownership over them.

Do not substitute an arbitrary `process_events()` call, nested GLib iteration,
timer, polling loop, longer clipboard deadline, fixture-side delay, or an
extra input event for the display flush.  Do not enable the Xpra clipboard
helper in the `off` scenario, change `off` into a direction-only mode, embed
all bytes eagerly to avoid `send_source`, or treat `owner-set` as confirmation.
None repairs both directions, both selection classes, and deterministic cleanup
at the actual queue/listener boundary.

## Diagnostic limits and independent controls

`XPRA_X11_DEBUG_EVENTS=XFSelectionNotify` is not a reliable negative control at
this source boundary.  Core `init_x11_events()` invokes `set_debug_events()`
before `init_xfixes_events()` later registers the extension name, so the name
can be reported as unknown even though runtime parsing is eventually active.
Use raw XFixes monitors and structured route evidence to check actual delivery.

That registration-order issue affects diagnostics, not production routing,
because `xfixes_selection_input` registers the parser before selecting the
notification. It remains outside this case's production correction unless a
new behavioral reproduction proves that delivery depends on it. The
same separation applies to the generic synthetic-event parser policy discussed
above.

The following observations are controls, not proposed fixes:

- enabling XSettings masks the missing clipboard lease but introduces an
  unrelated owner whose cleanup can expose the bad refcount;
- a root subscription proves XFixes generation but does not route events to the
  clipboard receiver;
- FeatherPad is one real owner, not an application-specific compatibility
  target;
- the original Wayland sink and a minimal native-Wayland GTK sink exercise the
  same Xpra packet boundary;
- keyboard/focus activity proves connection liveness but says nothing about
  the X11 event filter.

## Patch-queue and integration traps

This is one atomic end-to-end production case even though it touches packaging,
common lookup state, Cython filter ownership, the X11 helper, token scheduling,
and the Wayland compositor's selection adapters and listeners.  Applying only
the apparent one-line X11 filter acquisition leaves the client crash;
packaging only the initializer leaves the helper dependent on an unrelated
filter owner; registering the GDK window without fixing lease cleanup leaves
later events vulnerable to another subsystem's teardown.  Conversely, the
Wayland offer and source-request delivery require the current X11 owner event
to cross the wire, and the `off` listener control proves that a blocked transfer
is policy rather than a broken native owner.

The owned behavior is one transaction: detect a new X11 client owner, publish
its current offer to a native-Wayland server application, service current data,
then accept an independent native-Wayland owner and either forward or reject it
according to negotiated policy. Splitting the Wayland delivery boundaries into an
independent case would make one case accept only a packet prefix while the
other depended on this case to reach its reproduction.  Keeping one case does
not collapse subsystem ownership: the X11 lease remains client-owned, outbound
Wayland publication remains selection-adapter-owned, and standard compositor
selection listeners remain independent of the optional Xpra clipboard helper.

Keep these maintenance constraints:

- edit Xpra source only in the case's isolated workspace and export with
  `workspace-stage` / `workspace-update`; never hand-edit `fix.patch`, its
  digest, or `paths`;
- preserve the package condition for both GTK X11 client-only and server
  builds, and verify an installed client rather than only a source checkout;
- retain the stable delegate if any current consumer can import
  `get_pywindow` before GTK injection;
- audit every current `init_x11_filter` and `cleanup_x11_filter` caller when
  adapting the lease contract;
- do not turn a shared global filter into clipboard-private installation or
  process-wide cleanup;
- do not restore the root XFixes subscription unless it gains an explicit,
  independently owned receiver and cleanup contract;
- do not make `SelectionNotify` an `XSelectInput` mask or assume it is the
  property-completion event;
- preserve generation, target-owner, backoff, block, direction, and selection
  translation semantics in adjacent upstream changes;
- audit all six Wayland adapter operations together: `set_source`, `clear`,
  and `send_source` for both standard and primary selection must flush after
  the wlroots operation rather than depend on later compositor input;
- retain the remote-source pointer equality guard, pre-set replacement
  publication, synchronous wlroots destruction semantics, and post-set flush;
  explicit wrapper destruction is only the idempotent fallback and cleanup
  must never erase a newer native owner;
- preserve exact request-bound Wayland completions and monotonic local/remote
  generations.  Never merge pending writes by target, key reads only by an FD
  or raw pointer, or let a cancelled eager collector cross into a new owner;
- keep helper reset/cleanup draining wire requests exactly once under its
  re-entry guard, and unregister each clipboard callback through the
  compositor's custom connect/disconnect contract rather than assuming a
  GObject signal ID;
- keep standard data-device seat listeners alive with the unconditional
  standard manager even when `features.clipboard` is false; do not move the
  optional data-control or primary-selection facilities outside their gate;
- keep extension debug-name ordering and generic `send_event` policy out of
  this patch unless new protocol evidence changes the first failing boundary;
- never retain arbitrary clipboard bytes in tracked or ignored acceptance
  evidence, and never allowlist a cancellation or teardown warning which was
  produced by an incomplete selection lifecycle.

The case has no source dependency on another active downstream patch.  It is
listed in `stacks/develop.toml` in deterministic queue order, but its case-owned
live gate deliberately selects only `CASE=x11-client-clipboard-events` so clean
and patched behavior stay attributable to this boundary.  Full-stack focused
and upstream legs separately prove compatibility with the rest of the queue.

Retirement after an upstream refresh requires behavior, not textual patch
conflict: the clean embedded source must package the client adapter, acquire and
balance the clipboard filter lease, retain a safe GDK mapping, deliver one
owner-change token, publish ordinary and primary set/clear/send operations
without incidental input, preserve local standard data-device ownership with
forwarding disabled, and pass the case's real X11 plus cross-backend live
controls.  A patch which merely applies in reverse is not sufficient evidence.

## Patch ownership and non-goals

The patch owns the smallest coherent cross-backend clipboard lifecycle exposed
by the X11-client failure:

- explicit acquisition and release of the global X11 filter by
  `X11Clipboard`;
- inclusion of the GTK X11 Python package for client-only builds and a stable
  lookup delegate which remains valid for early importers;
- true shared lease/refcount semantics, including balanced and idempotent
  cleanup;
- an event-window and GDK handoff which handles Xpra's subscribed XFixes events
  without crashing GTK or swallowing unrelated GDK selection notifications;
- removal of the unused root XFixes subscription and the invalid use of the
  numeric `SelectionNotify` event type as an `XSelectInput` mask;
- one token schedule per owner transition under the existing proxy state
  machine;
- immediate outbound Wayland display publication after ordinary and primary
  `set_source`, `clear`, and `send_source` operations;
- source- and generation-bound Wayland reads and writes, including exact
  per-wire-request completion and cancellation on replacement/reset/cleanup;
- correct wlroots replacement/clear lifecycle: publish the new proxy identity
  before the setter, tolerate synchronous destruction inside it, flush after
  it returns, and idempotently destroy only as a fallback, while never clearing
  a newer native source;
- balanced helper request draining and exact compositor callback
  disconnection, so teardown cannot retain timers, FDs, or post-cleanup
  selection entry;
- unconditional standard seat request/set-selection listeners paired with the
  compositor's unconditional standard data-device manager, while optional
  forwarding/data-control/primary facilities remain feature-gated;
- a real X11/XFixes regression in the existing clipboard client test module;
- deterministic compiled Wayland pipe/source lifecycle regressions for both
  ordinary and primary selections, plus the real compositor listener API.

It does not extend the clipboard conversion deadline, poll selection owners,
synthesize paste input, special-case FeatherPad or Zed, log clipboard contents,
change clipboard wire formats, change target filtering or translation, alter
negotiated direction, make the forwarding helper mandatory, or replace the
generic GTK/Wayland clipboard backend.  It does not turn a display flush into
recursive event dispatch, add timing-based publication, or broaden standard
`CLIPBOARD` listener ownership to the optional primary/data-control protocols.
It also does not broadly redesign X11 event parsing, extension debug-name
registration, GDK filter return policy, or all legacy selection bindings.

## Focused regression design

The focused test uses the existing `DisplayContext`, a real Xvfb display,
GDK's event loop, XFixes, the raw InputOnly requestor windows, and a real
`Gtk.Clipboard` selection owner.  It does not mock the disputed event route.

One test constructs `X11Clipboard` without another filter owner, verifies that
its event XID has a GDK wrapper, changes the real `CLIPBOARD` owner, and requires
the corresponding clipboard packet.  It then performs a real `UTF8_STRING`
conversion and requires exactly one packet after the event loop settles.  Its
tests-only clean-source failure proves that the standalone helper does not
receive the owner change; the exact-one assertion binds the adjacent scheduling
correction.

A second test acquires a direct peer filter lease, creates the clipboard
helper, proves one owner change, releases the peer lease, and proves a second
change still reaches the helper.  The opposite cleanup order uses two helpers:
it queues one XFixes owner change, destroys the first helper before GDK
dispatch, requires the second helper to route the event, and requires the
first XID's foreign mapping to disappear only after `DestroyNotify` is drained.

The two constructor fault tests make deliberately different claims.  Injecting
a failure while setting the event-window diagnostic property proves that the
window factory destroys the real XID it just created; it does not claim to
exercise helper or filter teardown.  Injecting failure while constructing the
second proxy passes through the full helper rollback and proves that the first
proxy's real GLib token source, helper signal handlers, dispatcher receiver,
event XID, and clipboard filter lease are all retired without emitting a late
packet.  Together they cover both sides of the ownership handoff without
pretending one injected exception represents every setup failure.

A state table requires one owner change to schedule exactly one token in
plain, have-token, want-targets, and greedy modes and none while blocked.  The
proxy-lifecycle test manually seeds `local_requests`, `remote_requests`, and
partial `INCR` state and installs real long-lived GLib sources for the local
conversion and incremental watchdog.  Two cleanup calls must cancel both
sources, clear all incremental state, answer the mocked X11 requestor exactly
once, and never invoke the local conversion callback.  This is an exact
teardown/idempotency test; it does not claim to perform an end-to-end INCR
transfer or network round trip.  A common unit test separately binds stable
early-import lookup delegation and the exact lease sequence, including
harmless rejection of an unmatched cleanup.

`unit.clipboard_core_test` binds the shared request-delivery seam independently
of either platform.  Two same-target requests complete out of order by their
wire IDs and must reach only their own callbacks; the legacy no-callback path
must still call `proxy.got_contents`.  Timeout followed by a duplicate reply is
terminal once, and `client_reset` must remove every timer, empty-complete every
detached callback once, and prevent a callback from creating re-entrant wire
work while the drain guard is active.

`unit.wayland.clipboard_test` imports the freshly compiled clipboard and
compositor extensions and uses their real source wrapper classes with real OS
pipes.  For both `CLIPBOARD` and `PRIMARY`, source S1 opens request R1, S2
replaces it and opens R2, and a delayed R1 response is forbidden from writing
or closing S2's pipe; only R2 may deliver S2 bytes.  The reverse test starts a
native pipe read, advances ownership while deliberately reusing the same raw
source pointer, requires one empty completion, and proves late bytes cannot
revive it.  A greedy two-target variant requires cancellation to stop the old
sequential collector before it requests target two from the replacement.
Separate assertions cover NULL-owner cache/origin reset, idempotent wrapper
destruction when the selection adapter cannot clear, and cleanup of both
custom compositor event registrations.  A real uninitialized
`WaylandCompositor` instance binds the exact connect/emit/disconnect API rather
than substituting GObject signal IDs.

The atomic `wayland` gate builds `clipboard.pyx` and `compositor.pyx` with the
explicit clipboard and DMA-BUF Python dependencies required by the latter's
`wayland_surface` import chain.  It checks their ELF dependencies with
`ldd -r`, imports them in isolated processes with the display/event extensions,
and runs the native Wayland unit set.  This is why those modules and the
`wayland` gate are explicit in `case.toml`; relying only on a later full-Cython
run could let the focused case pass against stale extensions, omit a minimal
package dependency, or never compile the disputed paths.

The focused test is intentionally narrower than package acceptance.  Xvfb from
the source test image proves the event route and real protocol conversion; the
live client image proves that client-only packaging actually contains and
executes the adapter.

The deterministic Wayland units prove source/request identity, pipe ownership,
generation invalidation, and callback cleanup, but they are intentionally
narrower than native protocol publication.  They do not claim that observing a
flush call proves a standard client received a current offer, a native source
processed its request, or local ownership works with the Xpra helper absent.
Those protocol-facing claims remain the authority of the real two-endpoint
live case below.

## Durable live regression design

The durable live gate is the separate RGB-based `live-x11-clipboard` profile.
Its wrapper accepts exactly `CASE=x11-client-clipboard-events`; unlike the
seven complete-stack profiles, it applies the selected case source and
resolution to both the Debian 13 X11 client and Ubuntu 26.04 native-Wayland
server.  Both minimal package builds explicitly include clipboard support.
The client runs with YAML-owned `xsettings=no` and
`input-devices=noxi2`, so unrelated XSettings or XI2 initialization cannot
mask the clipboard helper's own event-filter responsibility.

### Live harness ownership map

The live proof is split across tracked owners; future adaptations must update
the owner of the affected contract rather than duplicating it in the runner:

| Owner | Live responsibility |
| --- | --- |
| `infra/live/profiles.py` and the `live-x11-clipboard` Make wrapper | Admit only the RGB/application-exit clipboard profile and the exact `cases/x11-client-clipboard-events` selection. |
| `infra/live/job.py` | Owns durable start/wait/status/abort/remove state, freezes inputs, validates endpoint-selection provenance, and requires the case source on both clipboard endpoints while preserving the clean client for the seven complete-stack profiles. |
| `infra/live/run.py` | Resolves and freezes the two build contexts, constructs the three policy scenarios, drives the ordered cross-peer interaction, reconstructs evidence from collected artifacts, and publishes the aggregate oracle. |
| `profiles.yml`, `live-cli.yml`, and `infra/live/live_config.py` | Own network quality and the exact role-specific `both`, `to-server`, and `off` Xpra arguments; Python orchestration does not duplicate those values. |
| `infra/live/Containerfile` | Builds the Ubuntu native-Wayland server and Debian X11 client packages.  Every client selection receives the ordinary GTK import preflight; only the exact clipboard case runs the additional installed-package `has_pywindow_lookup`/X11 helper preflight, because the seven complete-stack profiles intentionally retain a clean embedded-source client. |
| `infra/live/clipboard_fixture_common.py` | Owns the fixed non-sensitive marker IDs, lengths, and digests shared by both fixtures and the oracle. |
| `infra/live/x11_clipboard_fixture.py` | Implements the persistent GTK X11 owner, independent raw converter, and independent root XFixes monitor. |
| `infra/live/wayland_clipboard_fixture.py` and `start_wayland_clipboard_fixture.sh` | Implement and launch the native-Wayland sink/source window, its input-serial-bound reverse claim, and its compositor-confirmed event stream. |
| `infra/live/test_job.py` | Binds configuration flow, endpoint selection, fixture protocols, artifact reconstruction, fail-closed lifecycle behavior, and the selection-scoped image preflights without substituting mocks for the live X11/Wayland route. |

The exact-case selection is part of the acceptance contract, not merely a
convenient current slug.  If upstream later absorbs the production behavior,
the case cannot simply be deleted while this gate still names it.  The live
boundary must first move to a durable neutral owner (or an explicitly reviewed
replacement case), with its Make/profile selection rule, endpoint provenance,
fixture inputs, privacy checks, lifecycle tests, and policy oracle migrated in
one change.  Only a fresh clean-source run of that migrated gate can support
retiring the case.

One named run creates fresh sessions for `both`, `to-server`, and `off`.  A
`Gtk.Clipboard` X11 owner is already active before the Xpra client attaches,
matching the original initial-sync failure.  An independent raw X11 converter
first proves that the owner advertises `TARGETS` and returns the fixed marker
locally.  A raw root-window XFixes monitor starts before the later forward
changes and remains authoritative through the reverse attempt and final raw
conversion.  It must not stop after observing only the two same-XID forward
updates.

The second forward marker is installed by the same owner process and XID: its
XID must remain stable, its XFixes selection timestamp must advance, and the
client must survive without reconnecting.  A third transfer restores the first
marker through that same owner and proves repeated updates do not return stale
data.

The reverse transition has a stricter authority chain.  The command file only
arms the native-Wayland fixture.  The runner then delivers a real fixed key
through the Xpra input path, and `Gtk.Clipboard.set_text` runs inside that
window's key-event callback so GDK has the compositor input serial required by
Wayland selection ownership.  The fixture's immediate `owner-set` record says
only that it called the API.  The compositor-driven clipboard owner-change
callback must publish a separate confirmation before the runner attempts the
X11 conversion; a fixed sleep after the API call is not ownership evidence.

That confirmation is required in all three policies, including `off`.  The
`off` server intentionally runs with `--clipboard=no`, so no Xpra clipboard
helper is available to emit or relay a token.  Its successful confirmation
therefore proves the compositor's standard data-device request/set listeners
work independently of forwarding.  The same run must still show no reverse X11
takeover and no forward sink data.  Replacing `--clipboard=no` with an enabled
helper whose direction is disabled would test a different ownership graph and
would make this control vacuous.

The still-running XFixes monitor closes the cross-backend proof.  Under `both`
it must observe the X11 owner move from the fixed local owner to Xpra's event
window before the reverse conversion returns the reverse marker.  Under
`to-server` and `off`, its bounded terminal record must show no forbidden
reverse takeover and the conversion must still come from the original X11
owner XID.  Matching bytes alone cannot prove that Xpra claimed or declined
ownership.  The policy matrix is exact:

| Policy | X11 client to Wayland server | Wayland server to X11 client |
| --- | --- | --- |
| `both` | the initial, changed, and repeated fixed markers arrive | the fixed reverse marker arrives |
| `to-server` | the initial, changed, and repeated fixed markers arrive | the pre-existing local marker and owner XID remain; reverse data is blocked |
| `off` | all three forward transfers are blocked | the pre-existing local marker and owner XID remain; reverse data is blocked |

The event-driven live oracle binds more than final equality.  For each allowed
forward update, the server must expose the new source transition before the
fixture publishes a current digest match; the updated `two` read may not return
the previous 29-byte value, and restoring `one` must prove a later generation
rather than reuse the initial result.  Under `both`, F8, non-NULL compositor
selection, GTK `owner-confirmed`, the third XFixes takeover, and the successful
raw reverse conversion must appear in that order.  Under `to-server` and
`off`, F8 and native confirmation still occur, while the monitor terminates at
exactly two local same-XID events and the original raw X11 data remains
available.  No timeout, packet count, sleep, or last-observed value can replace
this ordered chain.

The gate reuses the ordinary RGB profile's positive window discovery,
rendering, screenshot/pixel, input, application-exit, process ownership, and
container cleanup boundaries.  Clipboard success cannot replace those checks,
and a rendered window cannot replace clipboard evidence.

The clipboard window must also remain a static RGB fixture.  Programmatic
paste updates do not need keyboard focus, so the entry stays out of the focus
chain and the input-owned reverse action is handled by the stable toplevel.
A GTK focus animation adds independently changing pixels and a separate
buffer-damage/visible-origin boundary to that comparison. Keep it outside this
static clipboard fixture rather than accepting a transient source/client
frame pair. Do not raise the pixel-error tolerance or fold a renderer workaround
into this case; retain the ordinary RGB pixel contract.

Fixtures emit bounded JSONL records containing marker IDs, expected lengths,
SHA-256 digests, equality booleans, targets, XIDs, timestamps, event kinds, and
PIDs.  They never emit marker plaintext.  Collection reconstructs the evidence
from retained authority artifacts instead of trusting the in-memory result,
binds fixture PIDs to owner records, and binds the Xpra client PID to equal
procfs identities captured before and after the two owner changes.  It also
requires zero exit statuses and empty fixture stderr, rejects unexpected
clipboard artifact names, and scans every collected file, including the final
report, for all fixed marker values.

The X11 GTK owner is launched with `NO_AT_BRIDGE=1` so an absent host
accessibility bus cannot add an AT-SPI warning to its otherwise empty stderr.
This is fixture isolation, not a relaxation of evidence: every expected
fixture stderr remains a regular zero-length file, synthetic stderr must still
fail the gate, and no validator may special-case or discard warning text.  In
particular, a GTK Wayland `Operation was cancelled` warning during a controlled
pending selection read belongs to its offer lifecycle, not accessibility-bus
isolation. An `off` ownership timeout followed by forced-teardown broken pipes
is likewise a failed local-owner boundary, not blocked-transfer proof. Neither
is an allowlist candidate.

The named clipboard checks bind local initial and updated `TARGETS`/marker
conversions, all three forward-policy outcomes, reverse policy and owner,
stable owner XID, advancing timestamp, exact event sequence, survival through
repeated changes, fixture cleanup, and absence of plaintext markers.  The three
scenario reports must appear in `both`, `to-server`, `off` order and the
aggregate report must bind each scenario name to its policy.  The case-only
gate does not alter the existing seven complete-stack profiles or their
clean-client semantics.

Acceptance requires a named `live-x11-clipboard` result in which every scenario
and the aggregate report are positive on the required inputs. Input callbacks,
monitor events, isolated stderr or static pixels alone cannot replace that
complete policy and ownership proof. Diagnostic runs remain in the ignored
cycle ledger, not this architecture description.

## Invariants not to simplify

- The GTK X11 adapter must be present in an installed client-only build, not
  merely importable from a source tree or combined server image.
- The event window, dispatcher receiver, GDK foreign wrapper, XFixes
  subscription, and global filter lease are separate owned resources.
- Xpra bindings, GDK wrapper, and filter must refer to the same effective X
  display and event queue.
- Every successful filter acquisition owns one count; only the final matching
  release removes the process-global filter.
- Client survival across owner changes must be proved by equal bounded procfs
  identities and the published client PID, not copied from an in-memory result.
- A failed or repeated cleanup must not remove a peer's filter, underflow the
  count, or clear unrelated dispatch receivers.
- Proxy cleanup must cancel core, local-conversion, and `INCR` timers, reset
  partial incremental data, answer remote X11 requestors at most once, and
  never let a local completion callback re-enter conversion during teardown.
- Every helper wire request retains its own ID, timer, selection, target, and
  optional completion.  Reset/cleanup empty-completes each record once under a
  re-entry guard; legacy proxies without a completion keep target-based
  delivery unchanged.
- GDK's foreign-wrapper table reference must outlive earlier queued events and
  be retired by server-ordered `DestroyNotify`; the Xpra filter continues to
  return `GDK_FILTER_CONTINUE`.
- `PropertyChangeMask` and routed `PropertyNotify` remain the conversion-data
  completion path; `StructureNotifyMask` owns GDK mapping cleanup, while
  `SelectionNotify` is not an X event mask.
- XFixes notifications are selected only on resources the helper owns and can
  clean up exactly.
- Direction, enablement, ownership-loop blocking, generation invalidation,
  target-owner isolation, and backoff remain authoritative before packet send.
- One owner transition must not create duplicate unbounded token or conversion
  work, and a burst must not publish stale owner data.
- Every successful ordinary or primary `set_source`, `clear`, and
  `send_source` wlroots operation is followed by its outbound display flush;
  later compositor input is never the publication trigger.
- A clipboard publication flush must not dispatch Wayland input, iterate GLib,
  or re-enter the proxy.  Client observation is proved separately by the
  compositor/fixture/conversion event chain.
- A replacement Wayland source object/generation is published before the
  wlroots setter synchronously destroys the old source; the resulting new or
  NULL seat state is flushed after the setter returns.  Explicit wrapper
  destruction is idempotent fallback only, and a remote source already
  superseded by a native source is destroyed without clearing the newer owner.
- Pending Wayland writes are keyed per request and owned by exact source object
  plus generation, never fanned out by target.  Pending reads use an
  independent key plus local generation and source pointer; neither raw pointer
  nor numeric FD reuse may cross an ownership transition.
- Origin reads and sequential eager collection stop at generation change.
  Source destruction, reset, and cleanup close owned FDs once, and late wire or
  GLib callbacks are no-ops rather than stale delivery.
- Clipboard cleanup unregisters the exact handler from the compositor's custom
  event registry.  It must not assume GObject signal IDs or leave callbacks
  which can re-enter a closing proxy.
- The standard data-device manager and standard seat selection listeners have
  one base-compositor lifetime, including under `--clipboard=no`.  Optional
  Xpra forwarding, data-control, primary-selection manager, and primary seat
  listeners remain feature-gated and do not borrow that ownership.
- No acceptance path may poll as the intended production mechanism or extend a
  timeout to conceal missing events.
- A native-Wayland ownership attempt is not authoritative until it occurs in a
  real input callback and the compositor reports the resulting non-NULL owner
  change; `off` must prove this even though it has no forwarding helper.
- The independent XFixes monitor spans forward and reverse ownership; a
  forward-only event count cannot accept reverse policy.
- The clipboard RGB fixture remains static.  A focus-animation or
  buffer-damage mismatch is diagnosed at its own boundary, never hidden by a
  larger pixel tolerance.
- Accessibility-bus isolation may prevent environmental stderr, but the
  zero-length stderr contract itself is never weakened.  Selection-buffer
  cancellation and timeout-teardown broken pipes are failures, not allowlisted
  diagnostics.
- Tests and probes use only fixed non-sensitive markers.  Runtime and retained
  evidence never sample arbitrary operator clipboard contents or retain fixed
  marker plaintext.
- A mock-only regression, raw XFixes observation alone, or a package import
  check alone cannot accept this cross-layer behavior.

## Required validation

Each required gate answers a different question; one green layer cannot stand
in for another. Schedule them with
[development and final acceptance](../../docs/runbooks/validation.md): nearest
regression after each atomic edit, affected upstream/case/composed modules,
relevant native/compiled/compatibility modes, and early clipboard live after
focused/native prerequisites. The table lists final obligations, not an
instruction to run full suites before every live iteration:

| Validation | Purpose |
| --- | --- |
| Clean tests-only case run | Applies the case-owned tests without the production correction to the frozen embedded source.  It must reach real Xvfb/XFixes assertions and fail for the missing event route or owned lifecycle, not because the image, import, or test discovery is broken. |
| Patched focused modules `unit.clipboard_core_test`, `unit.client.subsystem.clipboard_test`, `unit.x11.common_test`, and `unit.wayland.clipboard_test` | Prove exact wire-request callbacks and reset drain, the real X11 owner-change/conversion route, GDK XID lifetime, filter lease/refcount, exact token scheduling, lookup import order, Wayland source/read/write generation isolation for both selections, and bounded rollback/cleanup states on the atomic case. |
| Atomic `wayland` gate | Freshly compiles and linkage-checks the modified Wayland clipboard/compositor extensions and runs the native Wayland unit boundary, preventing a focused pass against absent or stale `.so` files. |
| The same focused modules and `wayland` gate through `STACK=develop` | Prove that earlier and later queue cases do not change those semantics, take accidental ownership of the filter, or break the Wayland lifecycle contract. |
| `patch-check`, `stack-check`, whitespace, lint, and fork-control units | Prove exact patch digest/path ownership, forward/reverse applicability, dependency order, and automation contracts; they do not prove runtime clipboard delivery. |
| Clean quarantine reassessment | Separates currently assigned upstream failures from this case before patched results are interpreted. |
| `full` | Runs the complete applied queue under the normal compatibility setting, including the legacy `clipboard-token` registration and default compiled-runtime behavior. |
| `full-cython` | Rebuilds the modified X11 filter lease and Wayland selection/compositor `.pyx` implementations rather than trusting stale generated binaries or cached extensions, then runs the complete Cython-enabled author suite. |
| `full-no-compat` | Sets the process-wide compatibility mode before imports and exercises the modern `clipboard-data` path without the legacy token handler. |
| Case-only `live-x11-clipboard` | Proves the installed Debian client-only package contains and executes `xpra.x11.gtk.__init__`, both endpoints use the same atomic case, every permitted new offer and native source request is delivered without incidental input, `off` retains standard native ownership without a forwarding helper, and real X11 owner events cross to a native-Wayland compositor and back under the exact `both` / `to-server` / `off` oracle while rendering, input, stderr, process, privacy, and cleanup remain positive. |
| Seven existing complete-stack live profiles | Guard the shared live builder/runner and the rest of the integrated queue after this case added a profile.  They are not substitutes for the case-only clipboard policy matrix. |

The client stage of the case live image is the essential package-composition
control because it installs with client, GTK/X11, and clipboard enabled but
without the server.  A source checkout, combined client/server installation,
or successful Cython import cannot replace it.  Broader release or upstream
refresh cycles may additionally require both real DEB builds under the
repository contract; those prove distribution packaging, but still do not
replace this deliberately client-only composition boundary.

Retain the non-vacuous clean result and stop escalation at the first unexplained
failure. After candidate freeze, fill only missing or invalidated requirements;
an input-verified positive named development run may already satisfy one.
Clean and patched comparisons keep the frozen
source, images, displays, fixtures, fixed marker set, and direction policy
identical.  Any semantic change to the live fixture, monitor, pixel capture, or
policy validator invalidates earlier live evidence and requires a fresh named
case run for the affected boundary. Foreground and ad hoc diagnostic output
never satisfies the table above.
