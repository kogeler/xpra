# Log the server closing message outside the signal handler

Code: the `Fork-Case: server-signal-quit-logging` commit on `develop`
(`make -C fork-maintenance case-show CASE=server-signal-quit-logging`).

## Boundary

Every server (`ServerCore` and its subclasses) installs Python signal handlers
for `SIGINT` and `SIGTERM` with `ServerCore.install_signal_handlers()`. The
handler calls `ServerCore.signal_quit()` synchronously. That method called
`closing()`, which logs "<type> server is terminating" through `log.info()`,
from inside the signal handler. CPython runs the handler between bytecodes of
the main thread, so the signal can land while the main thread is itself
writing a log record. Then the handler's write re-enters the same
`io.BufferedWriter`, which raises `RuntimeError: reentrant call inside
<_io.BufferedWriter name='<stderr>'>`. The logging module catches it and
prints `--- Logging error ---` with two tracebacks instead of the closing
message.

Our E2E live transport-loss profile caught this in the private-group
`bus-first` scenario. The group `SIGTERM` reached Xpra while it was writing a
`-d keyboard` debug line for the disconnecting client. The server exited 0,
but its log contained the logging error, which the runtime audit rightly
rejects. The race needs a log write in progress when the signal arrives, so
it shows mostly with debug logging or busy shutdowns. Without the fix the
output is an unpredictable traceback in place of the closing message.

Unaffected: the second termination signal (`deadly_signal`, installed by the
first) and the shutdown itself; the server already performed its cleanup
from the main loop.

## Embedded-source context

Source: `d2940c7ddc6edd65aadb00f55ac559045d0c94ed`. `install_signal_handlers()`
and `signal_quit()` are unchanged since before the previous embedded source.
The synchronous `closing()` call dates from `f06bc57e3d` (2020, "log the same
'closing' message from all exit paths").

The maintainer's own rule is not to log or clean up from a Python signal
handler, and to schedule that work on the main loop instead:

- `e03b81abac` (2015): "the signal handler can interrupt io and cause a
  'reentrant call inside' runtime error". It moved the child reaper's log
  and reap calls to `idle_add`.
- `67708f9009` (2016) made the same change for the subprocess wrapper, citing
  [bpo-24283](https://bugs.python.org/issue24283).
- `34472f085b` (2019, #2158): "only use loggers from the callback we schedule
  via idle_add / glib signal handler callback".
- The current base class `GLibServer.signal_quit()` still reads "don't clean
  up from the signal handler" and only schedules `clean_quit`.
- `xpra/util/glib.py` `register_os_signal()` runs callbacks through
  `GLib.idle_add`. Its fallback installs `deadly_signal` synchronously so that
  a second signal exits at once, the same split this case keeps.

`ServerCore.signal_quit()` overrides that base method and was the only
remaining exception. Reviewed: local upstream history of
`xpra/server/core.py`, `glib_server.py`, `util/glib.py` and `util/system.py`.
The #2158 discussion on GitHub was not reviewed. An upstream change which
stops logging from this handler retires the case.

## Surrounding code and ownership map

| Owner / symbol | Role |
| --- | --- |
| `GLibServer.run()` | `install_signal_handlers(self.signal_quit)` before the main loop. |
| `ServerCore.install_signal_handlers()` | `signal.signal()` for `SIGINT`/`SIGTERM`; `os_signal()` calls the callback inside the Python handler. |
| `ServerCore.signal_quit()` | Installs `deadly_signal` for a second signal, queues `clean_quit` and `sys.exit(128 + signum)` with `GLib.idle_add`. Before the fix it also called `closing()`. |
| `ServerCore.clean_quit()` | Main loop: `closing()` (sets `_closing`, logs the message once), `cleanup()`, `quit_worker()`. |
| `ServerCore.closing()` / `log_closing_message()` | Idempotent: logs only on the first call. |

## Mechanism and lifecycle

Before the fix: signal → `os_signal` → `signal_quit` → `closing()` →
`log.info` → `StreamHandler.emit` → `stderr.write`. If the interrupted frame
was itself inside that `BufferedWriter`, the write raises and
`Handler.handleError` prints the logging error. The later `clean_quit()` does
not log again, because `_closing` is already set. So the closing message is
lost whenever the race hits.

After the fix, `signal_quit()` only changes signal dispositions and queues
the two callbacks; none of these log. `clean_quit()` runs on the main loop,
outside any write, and its first `closing()` call sets `_closing` and logs
the message. `_closing` is set when the main loop reaches `clean_quit`, as in
the base `GLibServer.signal_quit()`. Until then the server simply continues
its current iteration; the 5-second `force_quit` timer is still armed by
`clean_quit()`, and a second signal still exits immediately through
`deadly_signal`. There is no new state, lock or thread interaction.

## Case-commit and integration ownership

The commit changes `ServerCore.signal_quit()` in `xpra/server/core.py` (one
removed call and a comment) and adds
`tests/unittests/unit/server/signal_quit_test.py`. No dependency.
`server-shutdown-disconnect-flush` also edits `core.py`, in the separate
`handle_invalid_packet()` hunk, and its tests in `core_test.py`. The two
behaviours are independent: that case owns closing detached protocols at
shutdown, this one owns the signal-handler context. The regression lives in
its own file so that the promotion-branch test hunk stays byte-identical.

## Commit scope and non-goals

Unchanged: signal dispositions, exit codes (`128 + signum`), the second-signal
behaviour, `clean_quit()`/`cleanup()` order, and every other `closing()` path
(`clean_quit`, `quit`). Not changed: making the log handler swallow
re-entrancy, which would hide every other interrupted write. Not changed:
moving the whole callback to the main loop, which would also delay the
`deadly_signal` installation that guarantees a second signal works while the
main loop is busy. Other signal handlers (`deadly_signal`, which prints and
force-quits) are outside this case.

## Regression design and clean control

`unit.server.signal_quit_test` runs a fresh interpreter. It installs the real
`ServerCore.install_signal_handlers(server.signal_quit)` on a minimal
`ServerCore` (`__new__` plus the three attributes `closing()` reads) and
routes the root logger through Xpra's `SIGPIPEStreamHandler` onto an
`io.TextIOWrapper` over a real `io.BufferedWriter`. The raw stream's first
`write()` sends `SIGTERM` to the process, so the Python handler runs while the
buffered writer holds its lock: exactly the live interleaving, deterministic
in CPython's main thread. `GLib.idle_add` is replaced by a recorder. The test
requires that stderr contains no logging error and no re-entrancy error, and
that the queued callbacks are `clean_quit` and `sys.exit` with `128 + SIGTERM`.

Clean control: the handler logs the closing message, the write re-enters the
`BufferedWriter`, and the test fails on the logging-error assertion with the
same traceback as the live failure. Substituted: the main loop (callbacks are
recorded, not run) and the output stream. Blind spot: the closing message
itself is logged later by `clean_quit()`, which this unit test does not run.
The live profiles' server logs show it.
`unit.server.core_test` is the adjacent upstream control for `ServerCore`.

## Durable live or package boundary

`live-xpra-transport-loss`: the private-group teardown orders send `SIGTERM`
to the real packaged server (with `-d keyboard,...` debug output) and the
runtime log audit rejects `logging-error`. Each order must still log its
normal teardown, exit 143 (or 0 for `bus-first`) and leave no members. The
race is timing-dependent there; the unit test is the deterministic proof.

## Invariants not to simplify

- Nothing reachable from `signal_quit()` may log, flush or clean up; only
  signal dispositions and `GLib.idle_add` calls belong there.
- `deadly_signal` must still be installed synchronously, so that a second
  signal works even when the main loop never reaches `clean_quit`.
- `clean_quit()` must call `closing()` itself; it is now the only path that
  logs the message after a signal.

## Required validation

Follow [canonical validation](../../docs/runbooks/validation.md):
`case-check`, the case's `focused` target patched and tests-only (the clean
control must fail in `signal_quit_test`), the stack `focused` run, new DEBs
for both distributions, the live walk from `live-xpra-transport-loss` and one
complete nine-profile pass with `live-suite-check`, then the `full`,
`full-cython` and `full-no-compat` legs.
