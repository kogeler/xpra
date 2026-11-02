# Ignore notification replies after client detach

Code: the `Fork-Case: notification-client-detach` commit on `develop`
(`make -C fork-maintenance case-show CASE=notification-client-detach`).

## Boundary

A notification close or action can be admitted while a client is connected,
then execute from the UI queue after detach has removed its server source.
`NotificationForwarder._process_close()` and `_process_action()` previously
asserted that the source still existed. Real SSH detach churn produced
`Error processing 'notification-close' packet` with `AssertionError` at
`assert ss`. The packet-error boundary contains the exception, so later frames
and process cleanup may pass despite this incorrect lifetime assumption.

A reply whose client has gone away has no remaining per-client callback owner.
It must be ignored, without forwarding it to the shared D-Bus notifier or
crediting activity to another client. Connected-client replies retain their
existing behavior and validation. This is source ownership at deferred UI
delivery, not a new notification protocol or permission policy.

## Embedded-source context

The source is `646b712d1bff46b08b8f26ddc7a83229d65ba5bc`, unchanged by this task.
The missing-source assertions are inherited upstream. Current
`init_packet_handlers()` explicitly registers close, action and status with
`main_thread=True`; `PacketDispatcher` schedules the bound callback and does
not retain a server source on its behalf. The neighboring `_process_status()`
already performs an optional source lookup before applying its update.

Antoine Martin's `a154b6a7bd` restricts notification actions to bounded,
printable keys actually offered by the application. Its implementation and
active-action regression were read. This case preserves that policy and its
callback/forwarder distinction; it changes only what happens when the source
lookup reports no client. Public discussion access was rate limited; no
upstream endorsement is claimed. Retirement requires the upstream callbacks
to handle detach between admission and UI execution without assertion,
activity attribution or notification forwarding.

## Surrounding code and ownership map

| Component | Ownership |
| --- | --- |
| Client notification subsystem | Sends close/action replies while its protocol is alive; detach can overtake deferred server delivery. |
| `net/dispatch.py:PacketDispatcher` | Routes authenticated packets and schedules UI callbacks; the error-boundary case contains unexpected exceptions but does not supply missing client context. |
| `server/subsystem/notification.py:NotificationForwarder` | Resolves the current protocol source at callback execution and owns notification reply semantics. |
| `server/source/notification.py:NotificationConnection` | Owns the connected client's callback map and activity attribution. |
| `dbus/notifications.py:DBUSNotificationsForwarder` | Owns shared forwarded notifications and offered action keys; receives no event from an absent client. |
| `server/core.py` and source cleanup | Remove the protocol/source association during detach; source lookup afterward returns no owner. |

The handler already runs on the UI thread, so this fix needs no new lock,
thread, timer, retained protocol or delayed retry. It respects the completed
source removal instead of extending that source's lifetime.

## Mechanism and lifecycle

Both reply handlers retain the feature-enabled assertion and immediately
resolve `get_server_source(proto)`. If it returns no source, they return before
parsing a reply whose per-client context no longer exists. Otherwise the same
local `ss` is used for callback removal, activity and forwarding.

For a connected client, close still removes a special client callback or
forwards an ordinary close. Action still checks key length and printability,
credits its user event and invokes either the client callback or a currently
offered forwarded action. The active callback remains popped exactly once.
No notification IDs or action keys are added to an allowlist.

The early lookup matches the neighboring status handler. It also avoids
warning about obsolete reply contents after the client has detached. Disabled
features, malformed active-client packets, callback failures and invalid
forwarder data keep their existing error paths. Returning for a missing source
does not suppress an exception from a live notification callback.

## Case-commit and integration ownership

This case owns only the two notification reply handlers and their focused
notification module. No manifest dependency is required: both paths and the
existing test module exist at the embedded base, and the case introduces no
API supplied by another fork case. Its independent application/removal is
checked through `case-check`.

`packet-handler-error-boundary` remains separately responsible for containment
and reporting; it must not turn this expected late reply into successful
notification work. `server-shutdown-disconnect-flush` preserves final protocol
writes and late-packet handling, without owning the semantics of a deferred
notification. The new guards do not remove those cases or expand their scope.

The positive live-suite checker additionally rejects explicit packet-handler
error reports in report-bound peer logs. That is fork-control code, not part of
this product commit. It was motivated by the observed trace rather than a
blanket traceback rule; caught debug codec probes remain permitted.

## Commit scope and non-goals

Ignore notification replies with no current source; preserve replies with a
source. No public options, wire formats, D-Bus signatures, global notification
state, source construction or cleanup scheduling change. Do not replace this
with a dispatcher-wide rule dropping every queued UI callback: terminal and
resource-drain callbacks have different ownership requirements. Do not retain
the detached source or forward its reply using a different current client.

## Regression design and clean control

`unit.server.subsystem.notification_test` obtains each actual registered UI
handler, then removes the server-source association before invoking it with a
real `Packet`. Both close and action must return without changing the old
callback map, calling the shared notifier or recording a user event. The same
test restores a connected source and proves that close removal and action
callback/activity still work. Existing real D-Bus and offered-action tests
remain selected; their active-client validation is unchanged.

The tests-only clean control reaches `assert ss` for both deferred replies;
it does not depend on a newly added method or mock the handler's outcome.
The source lookup models the detach transition and notifier/activity spies
observe side effects. This is a deterministic lifetime control, not proof of
every notification transport or desktop integration.

## Durable live or package boundary

`live-xpra-detach` repeatedly creates and removes SSH clients during window
churn. The retained server stream demonstrated a late notification-close
assertion even though the profile had rendered and cleaned up. Replaying the
strengthened positive peer-log oracle on that exact report must reject it.
The repaired source must complete that gate without packet-handler errors,
then the live loop continues and finishes with one complete nine-profile pass.
Both endpoints always use the complete current stack and exact validated
Ubuntu server and Debian client DEBs. Native package validation remains
complementary; no installer or build-only notification workaround is added.

## Invariants not to simplify

- Resolve source ownership at deferred execution, not only packet admission.
- A detached client may not mutate or forward notification state.
- Preserve key validation and offered-action checks for connected clients.
- Keep this lifetime rule at the notification owner, not a global callback drop.
- Reject explicit packet-handler errors in positive live peer streams without
  rejecting benign caught codec-debug tracebacks.

## Required validation

Run the meaningful tests-only control, focused notification regression and
composed focused stack; run all three full legs, offline fork controls and
both real DEB builds. Exercise the complete-stack live loop from the earliest
newly failing detach gate, then one full nine-profile pass and suite check.
Runtime identities and results remain in the ignored session ledger.
