# Packet handler error boundary

## Boundary

A packet-handler error boundary must surround execution of the selected
handler, not only the call which schedules it. The GLib server and GUI client
defer main-thread handlers with `idle_add`; their bodies run after
`dispatch_packet()` has returned. An exception there escapes the upstream
dispatcher into PyGObject. A burst of malformed events can therefore produce
one unhandled traceback per queued event and make diagnostic output dominate
the main loop.

This case wraps the selected callable before passing it to the existing
thread/toolkit adapter. Ordinary `Exception` failures finish that invocation
and enter one dispatcher-wide, thread-safe reporting budget. The first report
retains the original traceback; further reports are admitted no more often
than once every five seconds. Successful packets are not delayed, dropped,
retried or redirected by the reporting policy.

This is a shared dispatch and diagnostic boundary, not a pointer-input fix,
connection recovery protocol or application-specific workaround. It preserves
the client/server calling conventions, routing and one-shot callback contract.
It does not undo partial handler work, prevent every hang, or turn an invalid
packet into a valid event.

## Embedded-source context

The case resolves against embedded source commit
`212038243d0067b6860ebe7d6953692179ef353f`. Upstream commit
`b0bd1265eb7f206c3583fabe63b5f8bd1aec019b` moved namespaced handler lookup into
the owning subsystems while retaining flat-registry precedence, legacy aliases
and default handlers. Commit `457e7598d3fb925e9e51cd94615b1abe61cbaf1c` also
made the clipboard helper use the shared subsystem registry. Those registration
boundaries are preserved; the patch does not replace them with a second router.

At this source boundary, the inner dispatch guard catches only
`AssertionError`, `TypeError`, `ValueError` and `RuntimeError` around
`call_packet_handler()`. The outer guard catches `RuntimeError` and
`AssertionError` around lookup and dispatch. Neither guard remains on the
stack when a deferred GLib callback runs. Merely adding exception classes to
those guards, or lowering the log level of a subsystem, cannot close that
asynchronous gap.

On an upstream refresh, equivalent replacement must protect actual deferred
execution on both endpoints, preserve the adapter and lifecycle semantics,
and bound reporting across packet types and peers. A nearby `try/except` or a
fix for one malformed pointer packet is not equivalent protection. Upstream
history is technical provenance; fork workflow remains governed by the
[validation runbook](../../docs/runbooks/validation.md).

## Surrounding code and ownership map

| Component | Responsibility |
| --- | --- |
| `xpra/net/protocol/socket_handler.py` | Receives, decodes and wraps wire data as `Packet`, invokes the endpoint callback, and owns transport errors and connection-loss delivery. |
| `xpra/net/dispatch.py` | Selects aliases, flat/default handlers and subsystem handlers; owns the guarded invocation and shared reporting state. |
| `SubsystemPacketHandlers` / `find_packet_handler()` | Keep registration with the owning subsystem and resolve namespace prefixes without changing handler signatures. |
| `xpra/server/glib_server.py` | Invokes server handlers as `(proto, packet)`, directly or through the server's main-context scheduler. |
| `xpra/client/base/client.py` | Invokes client handlers as `(packet)` and supplies the corresponding deferred/direct adapter. |
| `xpra/util/glib_scheduler.py` / GLib | Register and dispatch real idle sources; the patch does not replace the scheduler or own its source IDs. |
| `xpra/net/common.py` / `xpra/server/subsystem/pointer.py` | Validate packet fields and execute pointer policy; a rejected unsigned button is one concrete handler failure. |
| `xpra/log.py` | Formats and emits an admitted report, including the current exception traceback. |
| `xpra/server/subsystem/info.py` / client `get_info()` | Include dispatcher statistics in existing server/client diagnostic information. |

The patched main-thread path is:

```text
endpoint receives a Packet
  -> dispatcher resolves its name and chooses the existing handler
  -> dispatcher wraps that handler and passes it to the endpoint adapter
  -> adapter registers its one-shot call with GLib
  -> dispatch_packet returns

GLib invokes the adapter later
  -> adapter supplies the server or client argument list
  -> wrapper executes the selected handler
  -> on Exception: account for the failure and possibly report it
  -> wrapper returns None; the idle invocation completes
```

For non-main-thread handlers, the adapter invokes the same wrapper directly.
Errors raised during lookup or scheduling enter the outer dispatch guard and
use the same reporting budget; they need not reach a handler body first.

## Routing, admission and callback semantics

Alias resolution precedes handler selection. An alias creates the same renamed
`Packet` as upstream; the wrapper neither changes its fields nor performs
another conversion. For authenticated, open protocols the existing priority
remains flat UI handlers, flat non-UI handlers, then the owning subsystem.
Default UI and default non-UI handlers remain the subsequent fallback.

The wrapper captures the already selected callable and packet type. It does
not re-route the packet when the idle callback eventually runs, add a new
authentication decision, or replace subsystem-specific stale-owner checks.
In particular, default lifecycle handlers remain eligible after the protocol
is closed. Moving the open-protocol check around all callbacks would suppress
the `connection-lost` cleanup path and is not part of this change.

Client handlers must still receive one argument and server handlers two.
That difference remains in `call_packet_handler()`, not in packet inspection
or a guessed endpoint type. The generic dispatcher still invokes its direct
server-style handler. No adapter implementation or registration API is patched.

Both successful and failing wrapped calls return `None`. A handler's truthy
return value must not escape into a repeating GLib idle source. The existing
adapters already discard handler return values; the new wrapper preserves
that one-shot contract rather than introducing retries. It does not serialize
network and UI handlers into one total order or change their established
scheduling order.

Unknown packet types retain `handle_invalid_packet()` and its existing logging
and connection close. The error wrapper does not make unknown packets
acceptable or keep a connection alive against an existing explicit close
decision. It adds no automatic disconnect for an ordinary handler exception.

## Concrete invalid-button path and partial work

The modern `pointer-button` layout stores the button at index 4. The server's
`PointerManager._process_button()` reads it with `Packet.get_u8(4)`, whose
unsigned range excludes `-1`. The resulting `ValueError` is correct validation;
loosening the field range would move invalid input further into device code.

One source of `-1` is the client's button-mapping path: an unmapped button above
the ordinary three buttons gets that sentinel. The legacy `button-action`
branch suppresses it, but the modern branch at the embedded source constructs
a packet with it. An empty wheel map can expose this path. Wheel configuration
and sender-side suppression are separate responsibilities, not reasons to
special-case the dispatcher by packet type or application.

The parser also illustrates why containment is not rollback. Before reading
the button, it can record user activity, update the last mouse user and select
the UI driver. An exception at `get_u8()` prevents the terminal button action,
but does not reverse those earlier updates. The regression proves that valid
press/release packets behind the malformed burst still reach the terminal
input hook; it does not claim that every failing handler leaves all state
unchanged.

## Reporting state and thread ownership

`PacketDispatcher.__init__()` creates one lock, a next-report deadline and three
counters. This state belongs to the dispatcher object, not to each protocol,
packet name, exception message or process-global logger. A server dispatcher
therefore shares the budget across its peers; a separate client or server
dispatcher has an independent budget.

`_log_packet_error()` performs one short accounting transaction under the lock:

1. Increment the total failure count and read the monotonic clock.
2. Before the current deadline, increment cumulative and pending suppression
   counts, then return without calling the logger.
3. At or after the deadline, take the pending count for this report, reset it
   to zero and advance the deadline by `PACKET_ERROR_LOG_INTERVAL` (five
   seconds).
4. Release the lock before formatting or emitting the report.

The initial deadline is zero, so the first error is reportable even when the
clock is exactly zero. The next deadline is based on the newly admitted error,
not on how many intervals elapsed. There is no catch-up burst after a quiet
period. A quiet period also produces no delayed summary: another failure must
arrive to trigger another report. Pending suppressions remain inspectable in
the meantime.

Network and UI callbacks may update this state concurrently. An unlocked
check-then-update could let several workers admit reports against the same
deadline. Conversely, holding the lock during logger I/O could deadlock a
log consumer which re-enters dispatch. The deadline is committed before the
lock is released, so such an immediate reentrant failure is counted and
suppressed without recursively emitting another report.

The state has a fixed number of fields, not a growing map of peer or error
identities. It retains no packets, exception objects, tracebacks, callbacks or
peer references between failures. Only the original scheduled invocation
retains the selected handler and packet for its normal lifetime. There are no
new GLib sources, timer cancellation duties, polling loops or shutdown hooks.

## Diagnostic visibility and limits

Reports use the existing `network` logger at ERROR with the selected packet
type and the number of reports suppressed since the previous admitted report.
`exc_info=True` is evaluated while the original exception is still active,
preserving its type, message and traceback. `backtrace=False` avoids the
logger's default additional caller-stack dump; explicit logger backtrace
configuration remains logger policy.

`PacketDispatcher.get_info()` retains `packet-handlers` and adds
`packet-errors` only after a failure. Its counters are read together under
the same lock:

| Field | Meaning |
| --- | --- |
| `count` | Total failures observed by this dispatch error boundary. |
| `suppressed` | Cumulative reports omitted because the deadline had not expired. |
| `pending` | Omitted reports since the last admitted report; resets only on a new report. |

The server's full information response includes these fields under
`network.packet-errors`; client information includes `packet-errors` through
the existing base-client aggregation. These are diagnostic counts, not peer
identity, a retransmission queue, successful-packet counts or proof of a
healthy application. A shared budget deliberately means a second error kind
can be suppressed behind the first; it does not retain one sample per kind.

The budget covers this boundary's reports only. Existing logs inside a
handler, debug packet logging, unknown-packet diagnostics and transport/parser
errors keep their own behavior. Packet-type extraction and alias rewriting
precede the guarded routing block. Arbitrary exceptions outside that block,
or in independent callbacks a handler schedules for later, are not covered.

The report does not deliberately append the complete packet or peer, but the
original exception text can contain packet-derived values. This is not a
redaction layer or a bound on bytes per traceback. It also cannot preempt a
handler or a blocked logging backend; bounded report frequency is not a
universal latency or denial-of-service guarantee.

## Patch-queue and integration ownership

`fix.patch` changes only `xpra/net/dispatch.py` and adds
`tests/unittests/unit/net/packet_handler_error_test.py`. It has no case
dependencies and resolves independently, but production and live validation
always use the complete `stacks/develop` queue.

The shared dispatch route serves the keymap, clipboard and subsurface cases
as well as upstream handlers. Their policies and resource ownership remain
with those cases: deferred clipboard admission still checks its current
owner, keymap handling still validates its native state, and subsurface
transactions still own draw/ACK completion and repair. Containing an escaped
exception cannot commit a transaction, release an omitted resource or restore
a previous native state on their behalf.

Likewise, the timer and video lifecycle cases protect their own callbacks,
worker queues and cleanup ordering. A callback scheduled inside a packet
handler is not automatically protected after that handler returns. Moving
those case-specific cleanup guarantees into this wrapper would conflate
independent asynchronous lifetimes.

Patch storage is atomic, not a live-product selection. Updating this case must
not absorb adjacent fixes, replace their assertions with error-count checks,
or introduce an isolated clipboard, input or rendering live endpoint.

## Patch ownership and non-goals

The case owns actual handler exception containment, the shared reporting
budget and counters, and focused proof of those boundaries. It does not:

- alter wire packet layouts, field validation, aliases, feature negotiation or
  subsystem registration;
- change mousewheel, cursor-shape, notification or DPI policy;
- catch `KeyboardInterrupt`, `SystemExit`, `GeneratorExit` or other
  `BaseException` control flow outside `Exception`;
- retry failed work, roll back side effects, force disconnects, or disable a
  feature after an error;
- bound the number of queued packets or their execution time, or replace
  transport backpressure and protocol error handling;
- catch every exception in Xpra, suppress reports globally, or guarantee
  recovery of a stalled application; or
- add an installed dependency, preload requirement, background reporter,
  environment workaround or application-specific branch.

## Focused regression design

The retained module is `unit.net.packet_handler_error_test`. It imports the
actual dispatcher, server/client adapters, pointer parser and GLib bindings;
missing subject dependencies fail rather than skip. `EventClient` combines
the real `GLibScheduler` and `XpraClientBase` adapter with a minimal dispatcher
initializer, avoiding unrelated subsystem startup without replacing dispatch.

Only the reporting sink and limiter clock are controlled for error-accounting
assertions. The real default GLib main context dispatches the deferred calls.
A captured `sys.excepthook` records exceptions which escape into PyGObject;
checking only the mock logger would miss precisely that original failure.
`ExitStack` restores the hooks, and cleanup drains scheduled callbacks before
restoration even after an expected clean-source failure. The drain has a
deadline; it is a fixture guard, not a production timeout or proof that Python
can preempt a blocked handler.

The tests cover these distinct boundaries:

- Direct dispatch contains the original validation errors plus `IndexError`,
  `KeyError`, `AttributeError` and `OSError`, preserving the next valid packet
  and its arguments without closing the protocol.
- Both real GLib adapters defer 256 failing calls, execute interleaved valid
  packets and a final valid tail on the main-context thread, report once, and
  leak no exceptions to PyGObject. A truthy successful return remains one-shot.
- A real registered `PointerManager` receives 256 modern packets with button
  `-1`, followed by a valid press/release pair. The original unsigned-field
  validation must fail inside the wrapper, while the valid pair reaches the
  recording device hook. The peer source, UI-driver selection and terminal
  input hook are substituted; the parser and subsystem route are real.
- Flat/default and aliased routes, different peers and different error
  messages share one budget; alternating keys cannot multiply log output.
- Controlled clock values cover just before and exactly at the deadline,
  suppression-summary resumption, the first error at zero, independent
  dispatchers and the exact information counters.
- Eight real worker threads generate 512 failures against one dispatcher and
  must not each acquire a separate reporting allowance.
- A reentrant logging consumer dispatches another failure and completes
  without holding the accounting lock across logger I/O.
- A failing `idle_add` enters the scheduling-error boundary without invoking
  the handler body.
- Closed protocols reject ordinary packets while still delivering the
  default `connection-lost` lifecycle callback on both adapters.
- `BaseException` control-flow values propagate from the wrapper rather than
  being silently converted to routine packet-error reports.

The tests-only clean-source control must reach real handler execution and
expose escaping callback exceptions or unbounded reports. A missing new
`packet-errors` field alone is not a sufficient negative control; an import,
fixture-construction or scheduler-dependency failure is not the target defect.
The manifest also retains adjacent dispatcher, protocol, GLib server, server
core, pointer, client hello and client command modules. Those controls protect
existing routing and lifecycle behavior outside the new regression.

## Durable live boundary

There is no new case-only live fixture. Deterministic fault injection belongs
to the focused real-GLib module; it neither opens a production connection nor
claims to inject input into a real Wayland application. The complete live
suite provides the complementary end-to-end boundary with every active patch
on both server and client.

Its nine profiles cover Zed RGB and adaptive-alpha H.264, detach, transport
loss, native-Wayland keymap input, X11 clipboard, subsurface composition, and
Vulkan/OpenGL hardware H.264. They require their existing positive rendering,
input, lifecycle and owned-cleanup assertions through the shared dispatcher.
The live scenarios do not inject this malformed-button burst, so their success
cannot replace the focused exception-hook, counter and valid-tail assertions.
Conversely, a bounded error log in a unit test cannot establish live pixels,
clipboard ownership, hardware presentation or connection teardown.

## Invariants not to simplify

- Guard the selected callable before handing it to the adapter; a guard around
  `idle_add` alone cannot contain the deferred body.
- Preserve client/server signatures, routing precedence, aliases, closed-
  protocol defaults and the existing thread choice.
- Keep callback completion one-shot and leave retries to their actual owner.
- Catch `Exception`, not `BaseException`, without weakening packet validation.
- Use one budget per dispatcher across routes and peers, not an unbounded
  dictionary keyed by exception text or packet contents.
- Check and advance the monotonic deadline under the same counter lock.
- Commit reporting state before logging, but never hold that lock during
  handler execution or logger I/O.
- Preserve the original exception context and suppression counts without
  retaining traceback or packet objects in reporting state.
- Do not equate containment with rollback, recovery, successful input or
  coverage of later independently scheduled work.
- Keep real GLib and parser controls, the non-vacuous clean failure, and all
  nine complete-stack live profiles as separate acceptance responsibilities.

## Required validation

Follow [development and final acceptance](../../docs/runbooks/validation.md).
After an atomic behavior change, run the manifest's focused modules and a
non-vacuous tests-only clean control in the same frozen image. Exercise the
real GLib boundary in interpreted, Cythonized and no-compat modes; bytecode
compilation or a mock scheduler is not a native callback substitute. Check
the standalone case and its complete-stack composition without exporting the
stack into this atomic patch.

Start `live-all STACK=develop RUN=<fresh-prefix>` early once focused
prerequisites are satisfied. At candidate freeze, fill missing or invalidated
focused/native, quarantine, fork-control and all three full upstream legs,
then require `live-suite-check` to verify all nine current complete-stack
profiles. These are final coverage obligations, not a reason to restart
unchanged expensive jobs after each edit. Package builds follow the enclosing
validation contract; this case introduces no packaging or ABI change.

The [scoped mypy gate](../../docs/runbooks/typecheck.md) currently checks
`xpra/server/source/queued_packet.py`, not `xpra/net/dispatch.py`. A green
result there cannot be claimed as type-checking this wrapper or proving its
thread and callback semantics.

Keep exact source, selection, resolution, image and named-result identities
in the ignored cycle ledger. This README describes current behavior and its
durable controls, not a stored test result or an archive of a particular
incident.
