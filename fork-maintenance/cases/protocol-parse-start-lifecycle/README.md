# Preserve the parse worker's connection across startup and retirement

Code: the `Fork-Case: protocol-parse-start-lifecycle` commit on `develop`
(`make -C fork-maintenance case-show CASE=protocol-parse-start-lifecycle`).

## Boundary

An output/protocol owner closes or steals a connection after parse-thread
startup is scheduled but before that worker enters. `close()` and
`steal_connection()` set `_closed` and clear `_conn`. The worker dereferences
`self._conn.socktype` outside its parser exception guard, producing an unhandled
`AttributeError` rather than finishing quietly. The existing protocol format
test exposes this as a background traceback even when its assertions pass.
The queued UI startup callback also rereads `_conn` after its admission check,
so retirement in that interval can pass None to filter preparation.

The expected result is no new parsing on a retired connection, no startup
traceback, and unchanged filter-before-parser ordering for a live connection.
The defect does not require a real enabled filter: dereferencing the released
owner fails before policy installation. It affects shared socket protocol
startup, not only exec SSH or one GUI backend.

## Embedded-source context

Maintainer `af2a0946d2` added parse-thread seccomp installation before the
existing try/except. His subsequent [direct-attribute change][direct-attributes]
deliberately removes `getattr` for connection members that always exist. The
problem here is the connection's lifetime, not an optional `socktype` member;
restoring a fallback attribute lookup would conceal retirement instead of
preserving that contract. Earlier `2f59ec02b7` explicitly avoids reporting
parser errors once the protocol closes, but its guarded body does not include
this new startup dereference.

The [closed seccomp issue][seccomp] and current code distinguish main-thread
preparation from worker-local installation. Keep that division: a filter
installed on the UI thread would constrain the wrong owner. The maintainer's
[review requiring concrete race evidence][race-review] informs the deterministic
ordering tests rather than a broad shutdown rewrite. Keep this case until
upstream handles both queued-start and worker-entry retirement without hiding
errors on live connections. No matching closed issue specifically reporting
this exact traceback was located; the actual introducing/follow-up code and
related review are the design evidence, not a claim of upstream approval.

## Surrounding code and ownership map

`SocketProtocol.__init__()` owns the connection and creates an unstarted parse
thread. `start()` queues `start_network_parse_thread()` through the scheduler.
That UI callback checks closed/running state, prepares imports and peer
metadata, then starts the existing thread. Formatting/output paths can close
the protocol independently of this queued startup.

`close()` captures its connection, marks the protocol closed, clears `_conn`
and closes that captured transport. `steal_connection()` also closes admission
and clears `_conn`, but transfers the transport to a caller instead of closing
it; the caller owns waiting for old workers. `clean()` later drops references.
The parser's existing `while not self._closed` already prevents reads when a
close occurs after installation. The WebSocket subclass shares this worker
entry and normal parser dispatch. Internal trusted pipes retain their own
no-filter admission inside `install_parse_thread_seccomp`.

## Mechanism and lifecycle

The UI startup callback captures the connection before checking admission and
passes that local reference to preparation. If closure wins before admission,
the original closed guard still refuses startup. If closure wins afterwards,
preparation has its original object rather than a reread of the cleared slot;
the worker then sees retirement and exits. A local reference does not keep the
transport open, reverse `_closed`, or transfer ownership back from a thief.

The worker likewise captures `_conn` once. If absent, it returns before filter
setup or parsing. Otherwise it reads `socktype` on that retained object,
installs the existing policy on itself and enters the unchanged parser loop.
Closure after the snapshot is already handled by the loop's closed check.
Both references are local to startup; no cached transport or new state field
survives it, and no lock, retry, timer or polling loop is introduced.

Each hunk closes a demonstrated dereference interval at its actual owner.
Merely checking `_closed` before another `_conn` read would retain a race;
catching arbitrary AttributeError around setup would also hide malformed live
state and unexpected installation failures. Moving installation inside a broad
catch or onto the UI thread is not required. Existing installer feature/socket
gates, error reporting and syscall policy remain unchanged.

## Case-commit and integration ownership

This case owns the two startup-reference changes in
`xpra.net.protocol.socket_handler` and `unit.net.protocol_startup_test`. It has
no dependencies and applies independently to the embedded source. The
packet-handler-error-boundary case owns handler invocation/reporting in
`xpra.net.dispatch`, not transport startup. The SSH close-owner case owns
termination of its original child once close reaches that transport. The
server shutdown case owns disconnect flushing, not parse-worker admission.
Sharing a lifecycle route does not justify merging these independent behaviors.

## Commit scope and non-goals

No packet, encoding, encryption, transport ownership, EOF policy, thread count,
scheduler contract or platform default changes. No filter bypass for an open
connection, new seccomp permission, fallback `socktype`, swallowing of live
exceptions, process cleanup mechanism or global shutdown lock. This repair does
not certify all possible errors during connection stealing or concurrent I/O;
it preserves the existing wait-for-worker requirement for a transferred
transport. It does not alter repeated `start()` behavior or restart an exited
thread.

## Regression design and clean control

`unit.net.protocol_startup_test` constructs actual `SocketProtocol` and
`Connection` objects with assert-no-I/O read/write doubles and the ordinary
packet/compression registry initialization. A real joined Python thread enters the actual worker
method after actual close or steal; exceptions are captured and asserted rather
than left to stderr. Both tests fail on tests-only production at the original
None dereference. A scheduler double retains actual queued callbacks without
running a GLib loop. A separate deterministic test closes during the real UI
callback's `is_alive()` admission step: clean code passes None to preparation,
while patched code supplies the original connection and the started worker
exits without installing a filter. A closed-before-UI control starts nothing.

Open TCP and trusted-pipe controls record installation-hook-before-parser
ordering. A close inside the installation hook then enters the real parser
loop and must perform no read. An unexpected hook exception must still escape
without parsing. The hooks are substituted to control these interleavings,
not to claim native syscall enforcement. The existing native `seccomp_test`
and the SSH case's real filtered process test supply that separate boundary.
There are no feature skips in this case's regression.

Every started thread is joined with a bound; no timing sleeps select the race.
Cleanup closes/cleans each protocol, and queued scheduler doubles create no
native source. Adjacent protocol and bytestream modules retain real dispatch,
framing and teardown coverage. The unit is not a transported application, GPU
test or proof of every possible close interleaving.

## Durable live or package boundary

All nine profiles use the complete stack on both endpoints. Detach and abrupt
transport loss cover real protocol retirement and retained peer diagnostics;
the deterministic unit remains the direct oracle for this exact startup
ordering because ordinary live scheduling need not select it. No isolated live
endpoint is authorized. This Python reference-lifetime fix introduces no native
payload, package dependency or Elsewindow configuration requirement.

## Invariants not to simplify

- Read the connection slot once per startup phase; a separate closed check is
  not a reference-lifetime guarantee.
- Keep UI preparation and worker-local installation on their original owners.
- Return when worker entry observes an already-retired connection slot;
  later retirement is handled by the parser's existing closed check.
- Preserve actual socket-type dispatch for open sockets and trusted pipes.
- Keep unexpected live setup errors observable; do not add a broad catch.
- Assert worker exceptions and joined completion, not only passing peer tests.

## Required validation

Use [canonical validation](../../docs/runbooks/validation.md): tests-only
negative, patched focused startup/protocol/bytestream/native-filter modules,
composed focused and compiled checks, the final three upstream legs, full
fork-control checks and the complete nine-profile live loop. The new test has
no native subject and no optional platform skip. This module is outside the
separate scoped mypy inventory; Ruff uses the embedded configuration. Record
actual runs and remaining gaps only in the ignored session ledger.

[direct-attributes]: https://github.com/Xpra-org/xpra/commit/5a808721ac70ba451360e789cdd7ffea74366db9
[seccomp]: https://github.com/Xpra-org/xpra/issues/622
[race-review]: https://github.com/Xpra-org/xpra/pull/5019#pullrequestreview-5021429295
