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

The patch makes token cancellation part of native selection/direction
revocation and the shared peer-reset boundary. Wayland alone owns its backoff
and reset history; the generic scheduler's ordinary cancellation is unchanged. It does not raise the server flood limit, extend request
timeouts, cache arbitrary clipboard contents or suppress diagnostic warnings.
Clipboard ownership, native transfer completion and loop prevention remain
separate responsibilities.

## Embedded-source context

The case resolves against source commit
`d95058b0916913fe6ae5296fb702f66d833898b0`, embedded in current `develop`.
Upstream commit `19a70cc7216bfa38dc788982a0fd9dd01dfeb237` already introduced
exponential token backoff in the X11 selection proxy. The Wayland adapter
overrides the generic token scheduler and emits its GObject token signal
directly, so inheriting the shared proxy does not give it that X11 behavior.

Current `41d88fd2355` and `1eb1ddaf5cb` also provide a common scheduler with
elapsed-time subtraction and earliest-deadline retention. The Wayland override
still bypasses it, so the native burst defect remains. The previous downstream
patch reset generic last-send history on every cancellation; that is no longer
valid when cancellation merely moves an existing deadline earlier.

The adapted `xpra/wayland/server/clipboard_token.py` owns a cooperative
`WaylandTokenMixin` and the ready-owner admission helper. It uses the same named
backoff settings without importing X11. Explicit native constructors initialize
its state before connecting compositor callbacks. Source notifications and
origin/eager completions carry a token-specific generation, even on an isolated
case without the separate transfer-lifecycle patch.

This is an ADAPT decision, not retirement: the common scheduler's new timing
rules are preserved, obsolete generic proxy hunks are removed, and native
admission keeps its additional readiness/reservation boundary. Reading callers,
cancellation order and asynchronous source transitions establishes the need;
tests challenge those conclusions rather than replacing the manual review.

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
| `xpra/wayland/server/clipboard_token.py` | Owns native-only backoff/reset, readiness epoch and exact delayed reservation; admits a ready owner now or through one per-proxy timer. |
| Unmodified `xpra/clipboard/proxy.py` | Owns the shared policy fields, ordinary scheduler, timer cancellation and eager collection. Wayland cooperatively specializes revocation without changing other backends' scheduling history. |
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
helper records `_token_ready_source` at that ready boundary, not at the first
native pointer notification.

Every ordinary/primary native selection callback first advances
`_token_source_generation` and invalidates ready state, without postponing an
already armed timer. `source_key()` combines the pointer and this token epoch.
Origin completion compares its captured epoch before changing origin or
announcing readiness. An old read cannot bless a replacement which happens to
reuse the same pointer. Cancellation advances the epoch as well.

This readiness epoch is independent of the local/remote transfer generations
owned by the X11 clipboard case. That case additionally retires reads, writes
and sequential collectors at their native source boundary. The token case
does not duplicate its FD registry or pretend a timer owns the source.

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
| `_emit_token_timer` / `_emit_token_due` | Pending GLib source ID and its monotonic millisecond deadline; zero when no timer owns admission. |
| `_token_reservation` | Unique object identifying that particular delayed callback, independent of numeric ID reuse. |
| `_token_source_generation` / `_token_ready_source` | Native notification/cancellation epoch and latest pointer/epoch admitted after origin resolution. |
| `_token_last_attempt` / `_token_backoff` | Native admission time and next spacing policy, independent of generic last-send history. |
| `_sent_token_events` | Actual GObject token-publication count, not the number of eager attempts started. |

The helper follows this ordering:

1. Reject disabled/send-denied, empty and remote-owned notifications.
2. Publish the current ready-source key.
3. Keep an existing timer and deadline unchanged. New native notifications
   invalidate readiness; only their accepted origin completion updates it.
4. With no timer, compute elapsed time from separately rounded monotonic
   millisecond readings and reset backoff after the configured quiet interval.
5. Subtract elapsed time from the backoff. If nothing remains, record an
   admission attempt and immediately enter the native token constructor.
6. Otherwise register one timer, then publish its unique reservation, ID and
   deadline. A registration failure publishes none of those owners.
7. Its callback first claims that exact reservation, retires ID/deadline and
   rechecks current policy/source/readiness before admitting construction.

The native hook receives ordinary owner-change requests, not generic
`min_delay` rescheduling requests. It retains the first deadline for that
burst. No generic reschedule calls into its cancellation/reset method, and
the unmodified common scheduler retains its earliest-deadline behavior.

The existing X11-named environment settings are milliseconds:

| Setting | Default | Meaning here |
| --- | --- | --- |
| `XPRA_CLIPBOARD_TOKEN_BACKOFF_DELAY` | 20 | Initial delay, multiplied by two for requested targets and again for a greedy peer. |
| `XPRA_CLIPBOARD_TOKEN_BACKOFF_RESET` | 1000 | Quiet interval after which a new notification starts with zero backoff. |
| `XPRA_CLIPBOARD_TOKEN_BACKOFF_MAX` | 1000 | Cap on the next backoff delay, including the initial scaled delay. |

After an admitted emission, a nonzero backoff doubles up to the cap. A zero
backoff becomes the scaled initial delay. The timer waits only the remaining spacing after elapsed time, not a full
new backoff from each owner event or a repeatedly extended deadline.
These values describe defaults and existing configuration knobs, not an
unconditional packets-per-second guarantee for every environment setting.

This is coalescing, not a trailing-edge debounce. A continuous producer does
not have to become quiet before the peer sees progress. There is also no
periodic refresh when no owner changes occur: each timer is one-shot and
returns `False`, and another notification is needed to schedule further work.

## Timer cancellation and peer lifetime

`WaylandTokenMixin.cancel_emit_token()` retires the reservation and ready epoch,
resets native admission history, and delegates to ordinary core cancellation.
Core cancellation retires the ID/deadline before GLib removal. A removal
exception is contained after retirement; it cannot authorize the old closure
or prevent remaining native teardown. This reset does not change generic or
X11 throttle history during an ordinary earlier-deadline reschedule.

The mixin cancels on disabled/send-denied policy. Its methods cooperate through
`super()` with the transfer-lifecycle overrides in the X11 clipboard case;
they do not replace those overrides' read/write/source cleanup. The protocol
helper calls the existing cancellation method for each proxy at peer reset
before clearing origin. No alternate network reset or cancellation API is added.

Receive permission is independent of send permission. A transition to
`can_send=True, can_receive=False` preserves an already scheduled local token.
Becoming receive-only cancels outgoing announcements but does not make this
scheduler responsible for clearing received contents.

Receipt of a nonclaiming or receive-denied token is not native ownership
revocation. The separate transfer-lifecycle case owns that receiving boundary,
including empty claims which cannot replace an independent native owner.
The complete queue must preserve its cache/origin and pending announcement.
This case does not reintroduce the old test which incorrectly labelled
`got_token((), claim=False)` as a real remote takeover. Its remote-cancellation
control installs an actual native source for an accepted receiving claim.

The delayed closure captures a unique reservation object. An obsolete callback
must match that object before it can clear the proxy's ID or emit; recycling
the numeric GLib ID is not sufficient. The regression deliberately gives old
and new callbacks the same ID and invokes the old closure after revocation and
after replacement. This is UI-loop lifetime protection, not a claim that
native clipboard state is safe for arbitrary cross-thread access.

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

Admission updates `_token_last_attempt` and backoff, so even attempts abandoned
during asynchronous collection are rate bounded. `_sent_token_events` advances
only at the actual GObject publication boundary after the captured token epoch
still matches. It is not a transport acknowledgement: helper loop filtering or
connection policy may still reject the packet. Request IDs, deadlines and
returned data must be checked at their own completion boundaries.

The warning about "clipboard requests per second" comes from
`send_clipboard()`, which counts outgoing clipboard packets before encoding.
It is not restricted to incoming application content requests. A dropped
TARGETS response can thus coexist with a very small copied string. Fixing the
producer reduces this amplification; it does not guarantee that an unrelated
peer flood, slow source or transport failure can never cause another timeout.

## Patch-queue and integration ownership

`fix.patch` adds the Wayland scheduling helper and its native regression,
hooks `clipboard.pyx`, and extends peer-reset cancellation in
`xpra/clipboard/core.py`. The generic proxy is no longer a changed path. The existing clipboard-core regression gains a
checked peer-reset cancellation assertion. These paths form one outgoing
notification lifecycle; they do not include the packet limiter or request
timeout implementation.

The manifest has no case dependencies. The patch applies on the embedded
source by itself and composes after `x11-client-clipboard-events` in the
complete stack. Both cases touch clipboard infrastructure, so review their
combined source and run the composed focused modules after a change. Do not
export the stack as this case's patch.

The X11 clipboard case owns the residual helper/filter lifetime,
source generations, pipe/FD cleanup, exact request completion and receiving-claim
integration. Current upstream already supplies filter counting, wrapper
retention, GTK packaging and native display flush; neither case should carry
duplicate implementations. This case owns when a ready local owner may
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

The native regressions cover:

- A synchronous burst of 100 source replacements per selection: one immediate
  token, one final deferred token, targets from source 100, independent proxy
  timers and exact token-attempt accounting.
- Continuous changes with GLib dispatch between them: bounded intermediate
  progress and eventual delivery of the final source, rather than indefinite
  postponement.
- Disablement, send revocation, peer reset, an actual claimed remote source
  and cleanup: cancellation and a stale closure unable to use a new reservation,
  even when both have the same numeric GLib ID.
- Deterministic elapsed/quiet-period policy: a 40 ms spacing with 25 ms elapsed
  schedules 15 ms, a later source does not postpone it, and a quiet interval
  permits immediate admission. Only the policy clock and timer registration
  are substituted; separate tests still dispatch real GLib sources.
- Failed timer registration and failed source removal: no orphan reservation,
  no stale publication and no counter increment for an unadmitted attempt.
- Receive-only revocation while sending remains allowed: the pending outbound
  token survives and completes.
- Empty replacement and two real origin-pipe reads with the same reused source
  pointer: the obsolete completion cannot make the new owner ready, the timer
  must not advertise unresolved state, and the current completion delivers the
  final source and origin.
- Cancelled eager acquisition: its late pipe completion cannot publish or
  increment the sent-token count.
- Greedy collection: no read starts for superseded intermediate owners, and
  the deferred read obtains source 3's bytes and target format.

`unit.clipboard_core_test` separately checks that peer reset invokes token
cancellation. The manifest retains existing client and server clipboard
subsystem modules, the native `wayland` gate and all three full upstream legs.
The composed queue additionally exercises the generation/transfer guards and
nonclaim/receive-denied/empty-decline continuity controls owned by the X11
clipboard case. The selection fixture destroys old native wrappers
synchronously, and holds real origin/eager write FDs where the test requires
in-flight work; it does not simulate unresolved origin by changing a field.

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
- Match the unique reservation before clearing the published timer ID; then
  recheck send policy and native source/readiness epoch.
- Reset only Wayland admission history on revocation, peer reset and cleanup;
  never reset the generic scheduler's last-send time when moving a deadline.
- Do not cancel outgoing work merely because receive permission is revoked.
- Preserve native transfer guards and the unchanged server packet limiter.
- Distinguish admitted emission attempts, emitted tokens and completed
  consumer requests in both documentation and assertions.
- Keep the non-vacuous native control and final-value/drain/full-log live
  checks; absence of a warning by itself is not proof of clipboard delivery.

## Required validation

Follow [development and final acceptance](../../docs/runbooks/validation.md).
During an upstream refresh, first complete the incremental manual review,
implemented case checkpoints and whole-queue composed-review exit. Only then
start runtime validation. After a behavior change, run the manifest's focused modules and the tests-only
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

The full refresh also requires both real DEB builds under the enclosing
contract. This case changes neither packaging rules nor a native ABI. The
[scoped mypy gate](../../docs/runbooks/typecheck.md) does not currently own
these clipboard modules, so its success cannot be reported as their type
coverage. Keep source, selection, image and named-result identities in the
ignored cycle ledger, not in this architectural README.
