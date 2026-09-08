# Coalesced native Wayland clipboard owner notifications

## Boundary

Native Wayland owner changes at the embedded source emit a clipboard token
immediately for every selection update. A text widget can replace PRIMARY on
each selection adjustment, even when the user is working with one short line.
Each advertised owner can trigger target and content requests on the other
endpoint. The number of characters copied is therefore not a bound on the
number of clipboard protocol messages.

The server's existing clipboard flood limiter counts outgoing tokens and
replies together. A burst can exhaust that budget and discard a TARGETS reply,
leaving the X11 consumer to time out. This is a producer-rate defect in the
upstream Wayland adapter, independent of the event-delivery and transfer
lifecycle repair in
[`x11-client-clipboard-events`](../x11-client-clipboard-events/README.md).

This case coalesces ready owner notifications independently for CLIPBOARD and
PRIMARY. The first notification starts emission immediately; subsequent
notifications share one exponentially delayed callback which uses the latest
ready native source. New changes update that source identity without moving
the callback's deadline. Continuous selection changes must make progress,
and the final ready selection must not be lost when the burst stops.

The patch also makes token cancellation part of shared selection, direction
and peer revocation. It does not raise the server flood limit, extend request
timeouts, cache arbitrary clipboard contents or suppress diagnostic warnings.
Clipboard ownership, native transfer completion and loop prevention remain
separate responsibilities.

## Embedded-source context

The case resolves against source commit
`212038243d0067b6860ebe7d6953692179ef353f`, embedded in current `develop`.
Upstream commit `19a70cc7216bfa38dc788982a0fd9dd01dfeb237` already introduced
exponential token backoff in the X11 selection proxy. The Wayland adapter
overrides the generic token scheduler and emits its GObject token signal
directly, so inheriting the shared proxy does not give it that X11 behavior.

The new `xpra/wayland/server/clipboard_token.py` helper uses the same named
backoff settings without importing an X11 binding or making Wayland startup
depend on an X11 display. The native `clipboard.pyx` change is a narrow hook
in `WaylandPrimaryClipboardProxy.do_owner_changed()`. The ordinary clipboard
proxy shares that implementation while retaining its own selection API and
state.

On an upstream refresh, compare behavior rather than patch applicability.
An equivalent upstream replacement must bound continuous owner notification
bursts, deliver the latest ready source, preserve origin resolution, and
cancel old-peer work. A larger packet budget, a longer timeout or a
successful single paste does not establish those properties.

## Surrounding code and ownership map

| Component | Responsibility |
| --- | --- |
| `xpra/wayland/server/compositor.pyx` | Owns the seat and dispatches ordinary and primary selection changes from native clients. |
| `xpra/wayland/server/clipboard.pyx` | Owns the selection adapters, native source pointers, origin resolution, target discovery, GObject token signals and content-transfer callbacks. |
| `xpra/wayland/server/clipboard_token.py` | Admits a ready owner notification immediately or assigns it to one per-proxy backoff timer. |
| `xpra/clipboard/proxy.py` | Owns shared enablement/direction policy, token timer cancellation, token counters and eager-content collection. |
| `xpra/clipboard/core.py` | Maps selections, processes tokens and requests, tracks clipboard origins and resets state associated with the peer. |
| `xpra/clipboard/timeout.py` | Owns wire request IDs, completion callbacks and remote-request deadlines. |
| `xpra/server/source/clipboard.py` | Applies the outgoing clipboard packet budget and queues accepted messages for compression and transport. |
| `xpra/x11/selection/proxy.py` | Owns X11 selection conversion and its existing producer backoff; it is not the implementation of the new Wayland scheduler. |
| `fork-maintenance/infra/live/` | Owns the real X11-to-Wayland paste, PRIMARY burst, consumer-drain and complete-peer-log acceptance checks. |

The relevant path is:

```text
native client replaces a selection
  -> compositor selection signal
  -> Wayland proxy installs local source and targets
  -> resolve origin, if advertised by the source
  -> do_owner_changed()
       -> record latest ready source identity
       -> emit now, or retain one backoff timer
  -> schedule_emit_token()
       -> optional asynchronous eager-content reads
       -> send-clipboard-token GObject signal
  -> clipboard protocol helper
  -> connection's unchanged outgoing packet limiter
  -> peer token / TARGETS / content processing
```

Selection changes and the timer run through the existing GLib/native event
loop. The helper adds no worker, lock, polling source or second packet queue.
Its timer owns permission to begin token construction, not the lifetime of a
native source or an in-flight clipboard request.

## Ready-source identity and origin ordering

`local_source_ptr` identifies the currently selected native source.
`remote_source_ptr` identifies a source installed on behalf of the Xpra peer.
Neither `_have_token` nor a cached target list is a substitute for those
identities. An empty selection or the proxy's own remote source does not
become a new local owner advertisement.

Origin discovery can be asynchronous. `local_source_changed()` first obtains
the target list and then, when necessary, reads the private origin MIME type.
Only the accepted origin completion calls `do_owner_changed()`. The backoff
helper records `_emit_token_source` at that ready boundary, not at the first
native pointer notification.

`source_key()` combines the native pointer with `local_generation` when that
field exists. The standalone embedded adapter has no generation field and
uses `None` in that position. With the complete queue, the separate X11
clipboard case supplies native source generations and stale-transfer guards.
This case consumes that stronger identity without duplicating or taking over
its generation lifecycle.

A timer may therefore encounter several different situations:

| State when the timer fires | Result |
| --- | --- |
| The original ready source is still current. | Start emission from that current source. |
| A newer source has completed origin resolution and updated the ready key. | Start emission from the newer source and its current targets. |
| A replacement has changed the pointer or generation but is not ready. | Finish the timer without advertising the replacement. |
| The selection is empty or owned by the remote proxy source. | Finish without emitting a local token. |
| Enablement or send permission has been revoked. | Finish without emission; normal revocation also cancels the timer. |

An unresolved replacement is not permanently marked delivered. Its later
valid origin completion enters the normal owner-change path again. Conversely,
the helper does not retain or dereference an old native source just to finish
a previously scheduled notification.

## Backoff state and bounded progress

Each selection proxy owns independent scheduling state:

| Field | Meaning |
| --- | --- |
| `_emit_token_timer` | The one pending GLib source ID, or zero when no timer owns emission. |
| `_emit_token_source` | The latest source pointer/generation admitted after origin resolution. |
| `_last_emit_token` | Monotonic time of the last admitted emission attempt. |
| `_emit_token_backoff` | Delay to use for the next deferred owner notification. |
| `_sent_token_events` | Existing token accounting, updated when this helper admits an emission attempt. |

The helper follows this ordering:

1. Reject disabled or send-denied notifications.
2. Publish the current ready-source key.
3. If a timer already exists, return without replacing it or changing its
   deadline.
4. With no timer, reset backoff after the configured quiet interval since
   the last admitted emission.
5. If backoff is zero, record the emission attempt and let the native caller
   continue immediately to `schedule_emit_token()`.
6. Otherwise, register one timer for the current backoff delay. Its callback
   revalidates ownership and source state before recording another attempt
   and entering the same native scheduler.

The existing X11-named environment settings are milliseconds:

| Setting | Default | Meaning here |
| --- | --- | --- |
| `XPRA_CLIPBOARD_TOKEN_BACKOFF_DELAY` | 20 | Initial delay, multiplied by two for requested targets and again for a greedy peer. |
| `XPRA_CLIPBOARD_TOKEN_BACKOFF_RESET` | 1000 | Quiet interval after which a new notification starts with zero backoff. |
| `XPRA_CLIPBOARD_TOKEN_BACKOFF_MAX` | 1000 | Cap on the next backoff delay, including the initial scaled delay. |

After an admitted emission, a nonzero backoff doubles up to the cap. A zero
backoff becomes the scaled initial delay. The timer delay is the selected
backoff, not a deadline repeatedly extended from the newest owner event.
These values describe defaults and existing configuration knobs, not an
unconditional packets-per-second guarantee for every environment setting.

This is coalescing, not a trailing-edge debounce. A continuous producer does
not have to become quiet before the peer sees progress. There is also no
periodic refresh when no owner changes occur: each timer is one-shot and
returns `False`, and another notification is needed to schedule further work.

## Timer cancellation and peer lifetime

`cancel_emit_token()` clears the published source ID before asking GLib to
remove it, and resets both the last-emission time and the backoff. A later
eligible notification therefore does not inherit the previous peer's delay.
No new public cancellation API is introduced.

The common proxy now calls that method when `set_enabled(False)` or
`set_direction(False, ...)` revokes outgoing work. The protocol helper calls
it for every proxy in `client_reset()` before clearing its clipboard origin.
Existing received-token and cleanup paths continue to use the same method.

Receive permission is independent of send permission. A transition to
`can_send=True, can_receive=False` must preserve an already scheduled local
token. Becoming receive-only cancels outgoing notification work but does not
make this helper responsible for clearing received contents. Native source,
read and write revocation remain in the owning adapter and its composed
transfer-lifecycle patch.

The delayed closure captures its own timer ID. It first compares that ID with
the proxy's current `_emit_token_timer`; a removed callback cannot clear a
newer timer or use a later peer merely because the same proxy object remains.
Only the matching callback clears the ID and performs policy/source checks.
The regression explicitly invokes a captured old callback after revocation
and again after a new timer has been installed.

## Eager reads, packet accounting and diagnostic limits

The first notification starts token construction immediately; it does not
promise immediate wire delivery. A greedy peer may require asynchronous
content reads before the native proxy can emit the token signal. The existing
source-validity checks still decide whether their completion is publishable.

Deferred notifications do not start eager reads for every intermediate
selection. The callback calls the existing scheduler only after it has
validated the latest ready source. Target selection, per-target data formats,
content-size limits and origin filtering remain in the native/shared
clipboard code, not in the backoff helper.

Similarly, `_sent_token_events` counts the helper's admitted emission attempts;
it is not a transport acknowledgement or proof that asynchronous collection
eventually produced a packet. Request IDs, timeout sources and returned data
must be checked at their own completion boundaries.

The warning about "clipboard requests per second" comes from
`send_clipboard()`, which counts outgoing clipboard packets before encoding.
It is not restricted to incoming application content requests. A dropped
TARGETS response can thus coexist with a very small copied string. Fixing the
producer reduces this amplification; it does not guarantee that an unrelated
peer flood, slow source or transport failure can never cause another timeout.

## Patch-queue and integration ownership

`fix.patch` adds the Wayland scheduling helper and its native regression,
hooks `clipboard.pyx`, and extends cancellation in `xpra/clipboard/proxy.py`
and `xpra/clipboard/core.py`. The existing clipboard-core regression gains a
checked peer-reset cancellation assertion. These paths form one outgoing
notification lifecycle; they do not include the packet limiter or request
timeout implementation.

The manifest has no case dependencies. The patch applies on the embedded
source by itself and composes after `x11-client-clipboard-events` in the
complete stack. Both cases touch clipboard infrastructure, so review their
combined source and run the composed focused modules after a change. Do not
export the stack as this case's patch.

The X11 clipboard case still owns event-filter leases, native display
publication, source generations, pipe/FD cleanup, exact request completion
and loop-prevention integration. This case owns when a ready local owner may
start a token emission and when that deferred permission expires. Backoff
cannot replace a missing XFixes event route or a stale native transfer guard.

## Patch ownership and non-goals

The case does not:

- change clipboard packet formats, selection mapping or origin MIME data;
- merge CLIPBOARD and PRIMARY state, or compare arbitrary payload bytes to
  guess whether two selections mean the same thing;
- add polling, delayed application paste, an IDE-specific branch or a new
  clipboard package dependency;
- raise flood budgets, extend conversion deadlines or hide timeout warnings;
- retry a dropped wire reply or take ownership of an outstanding native pipe;
- replace X11's existing backoff implementation or make Wayland import X11;
- guarantee delivery after disablement, peer loss, source replacement or a
  content-read failure which the owning layer correctly rejects.

## Focused regression design

`unit.wayland.clipboard_token_test` imports the actual compiled
`WaylandClipboard` proxies and uses their GObject signals and GLib timers.
Missing native extensions fail the test; they are not a reason to skip its
subject. The compositor connection shell and selection API are controlled
fixtures, not a running compositor or an end-to-end wire connection.

The selection API supplies deterministic target names and writes eager text
to a real pipe. This keeps assertions independent of an application's
clipboard behavior while exercising the proxy's normal token construction and
asynchronous content callback. Both CLIPBOARD and PRIMARY follow their actual
native-adapter entry points.

The six tests cover:

- A synchronous burst of 100 source replacements per selection: one immediate
  token, one final deferred token, targets from source 100, independent proxy
  timers and exact token-attempt accounting.
- Continuous changes with GLib dispatch between them: bounded intermediate
  progress and eventual delivery of the final source, rather than indefinite
  postponement.
- Disablement, send revocation, peer reset, receipt of a remote token and
  cleanup: timer cancellation and a stale callback unable to clear or use a
  newly installed timer. Only this callback-identity control substitutes timer
  registration/removal so that the obsolete closure can be invoked directly.
- Receive-only revocation while sending remains allowed: the pending outbound
  token survives and completes.
- Empty or not-yet-resolved replacement: the obsolete ready key cannot
  advertise the new pointer, and a later ready source remains deliverable.
- Greedy collection: no read starts for superseded intermediate owners, and
  the deferred read obtains source 3's bytes and target format.

`unit.clipboard_core_test` separately checks that peer reset invokes token
cancellation. The manifest retains existing client and server clipboard
subsystem modules, the native `wayland` gate and all three full upstream legs.
The composed queue additionally exercises the generation and transfer guards
owned by the X11 clipboard case.

The tests-only clean control must reach the existing native owner-change
implementation and expose its unbounded burst. Failure to import the new
helper is not the oracle: the regression imports the existing proxy API.
Likewise, a green mocked signal counter cannot replace live target/content
completion on a real X11 consumer.

## Durable live boundary

The shared `live-x11-clipboard` fixture runs an X11 client and native-Wayland
server with the complete queue on both endpoints. Fresh `both`, `to-server`
and `off` sessions preserve the original same-XID local owner updates,
independent raw X11 consumer, real-F8 reverse takeover, compositor ownership
confirmation, root XFixes monitoring and controlled client shutdown/drain.

For allowed forward transfer, a native GTK text view receives Ctrl+V,
context-menu Paste and Ctrl+V through real client input. The menu's own key
callback observes its Paste mnemonic; text-view callbacks alone cannot prove
that input path. The `off` control uses an asynchronous native read to prove
absence of an offer, without waiting for a paste-completion signal that GTK
does not emit in that situation.

In forward-enabled sessions, the same one-line fixture then exercises rapid
PRIMARY changes using actual selection keys. An event-driven X11 GTK consumer
requests TARGETS and then text on owner notifications, without claiming the
selection or polling.
With `both`, it must receive the final one-character selection after the last
native key. With reverse transfer denied, the local X11 contents must remain
unchanged by that remote selection burst.

Collection reparses `native-paste-input.jsonl`,
`native-primary-consumer.jsonl` and all four complete peer stdout/stderr logs.
It requires real paste input, the final PRIMARY value, completion of every
consumer request, an empty pending-request ledger at shutdown and no clipboard
rate warning or request timeout. Fixture records contain lengths, hashes and
input metadata rather than copied plaintext; successful pixels or a single
paste cannot substitute for those checks.

The live control tests also reject missing stimulus, missing request drain,
incorrect final data and late warnings in either stream of either peer.
This fixture is one member of the mandatory nine-profile suite, never a
case-only live-product selection.

## Invariants not to simplify

- Schedule only after native origin resolution has admitted the source.
- Keep CLIPBOARD and PRIMARY timers and ready identities independent.
- Update the ready key without restarting an already scheduled callback.
- Read current targets and eager contents when emission starts, not when an
  intermediate owner first requested a delay.
- Check timer identity before clearing its published ID; then recheck send
  policy and native source/generation.
- Reset backoff on cancellation, including shared peer reset and disablement.
- Do not cancel outgoing work merely because receive permission is revoked.
- Preserve native transfer guards and the unchanged server packet limiter.
- Distinguish admitted emission attempts, emitted tokens and completed
  consumer requests in both documentation and assertions.
- Keep the non-vacuous native control and final-value/drain/full-log live
  checks; absence of a warning by itself is not proof of clipboard delivery.

## Required validation

Follow [development and final acceptance](../../docs/runbooks/validation.md).
After a behavior change, run the manifest's focused modules and the tests-only
clean control against the same frozen source/image. Include the composed
clipboard modules, actual native Wayland linkage and the compiled/no-compat
dimensions; bytecode compilation cannot prove the native signal and timer
boundary.

Start `live-all STACK=develop RUN=<fresh-prefix>` once focused prerequisites
are satisfied. At candidate freeze, fill missing or invalidated focused,
native, quarantine, fork-control and all three full upstream legs, and require
`live-suite-check` to confirm all nine current complete-stack profiles.
The manifest's `live-x11-clipboard` ownership does not waive the other eight.
Use the canonical scheduling and evidence-reuse rules rather than repeating
unchanged expensive checks after each edit.

Package/build validation follows the enclosing contract; this case does not
change packaging rules or a native ABI. The
[scoped mypy gate](../../docs/runbooks/typecheck.md) does not currently own
these clipboard modules, so its success cannot be reported as their type
coverage. Keep source, selection, image and named-result identities in the
ignored cycle ledger, not in this architectural README.
