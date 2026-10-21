# Keep one-shot printing commands free of printer-change watchers

Code: the `Fork-Case: printing-cli-timer-cleanup` commit on `develop`
(`make -C fork-maintenance case-show CASE=printing-cli-timer-cleanup`).

## Boundary

On a POSIX installation with PyCUPS, `xpra printing-info` can log a CUPS
connection error and then remain alive indefinitely. The smallest trigger is
an unavailable system D-Bus service followed by an unavailable CUPS server:
printer initialization starts its polling fallback, printer enumeration raises,
and Python waits for the non-daemon polling timer even after the CLI has
reported its error. Successful snapshots and early validation returns also
unnecessarily acquire the same long-lived watcher.

One-shot enumeration and job submission must finish without acquiring printer-
change monitoring. This case removes that allocation from the CLI only; it
does not disable printing, hide a connection error, shorten a test timeout,
start external services, or change monitoring for a connected GUI client.

## Embedded-source context

The current embedded Xpra 8.0 source dispatches `printing-info` from
`xpra/scripts/main.py:do_run_mode()` into `xpra.platform.printing.main()`.
That function calls `init_printing()` with the default no-op change callback,
then uses only synchronous enumeration, information and submission functions.
No part of its argument handling or output consumes a printer-change event.

Current `xpra/platform/posix/printing.py` binds initialization to PyCUPS.
`pycups_printing.init_printing()` installs a D-Bus listener when available,
otherwise `schedule_polling_timer()` starts a default non-daemon
`threading.Timer`. `check_printers()` re-arms that timer. The separate
`cleanup_printing()` hook exists, but the one-shot command never invokes it.
Upstream's recent printing edits do not replace this lifecycle: the current
CLI still initializes watching before every snapshot. The canonical test
environment includes PyCUPS and a private session bus, not system D-Bus/CUPS
daemons; the same defect is observable on an ordinary host without those
services. Removing the dependency or changing test expectations would mask it.

Retirement requires an upstream one-shot path that never acquires monitoring,
or an equivalent ownership-correct design that both terminates on every exit
and cannot cancel another caller's watcher. A return value alone is not that
proof; actual subprocess termination remains mandatory.

## Surrounding code and ownership map

| Entry or object | Actual owner and consumer |
| --- | --- |
| `scripts.main.do_run_mode()` printing dispatch | Calls the standalone command and owns reporting its return/exception. It does not own a connected printer subsystem. |
| `platform.printing.main()` | Takes synchronous snapshots or submits a job under `program_context`; it needs no printer-change callback. |
| `client/base/printer.py:PrinterClient` | Initializes printing after handshake, consumes changes through `send_printers`, and performs its own cleanup. That long-lived owner is unchanged. |
| `platform.pycups_printing.init_printing()` | Publishes the change callback and installs a D-Bus receiver or polling timer for that long-lived owner. |
| PyCUPS `Connection` and synchronous backend calls | Discover printers, query configuration and submit jobs independently of change-watcher initialization. |
| Windows and Darwin bindings | Current Windows initialization registers a spooler change listener; Darwin selects its platform/PyCUPS implementation. The command does not need those change notifications either. |

The generic CLI must not initialize and then indiscriminately clean up a
process-global watcher: an embedding caller may already own it. Avoiding an
unneeded acquisition also avoids a cancellation-versus-rearm race with an
already executing polling callback. No timer identity or new watcher state is
introduced in this case.

## Mechanism and lifecycle

Previously, every command invoked `init_printing()` before checking its
arguments. The default callback is `noop`, so a D-Bus listener or recurring
timer can never affect command output. If enumeration raises, returning from
the top-level error handler still leaves Python waiting for that timer. Early
returns such as no printers, missing files or an unknown printer also retain
it. Calling cleanup only on success would leave those paths broken; making
the timer a daemon globally would change the GUI subsystem's ownership.

The corrected command enters the existing platform program context and
performs its synchronous work directly. It never calls `init_printing()` or
`cleanup_printing()`. Consequently it cannot allocate, replace or cancel the
client's watcher, on success, early return, exception or repeated invocation.
Argument validation, output, printer selection, `print_files()` submission,
exit codes and exception propagation are otherwise unchanged. Generic platform
initialization and cleanup still belong to `program_context`.

There is no new concurrency protocol: the CLI does not need a timer, lock,
polling flag or join once it stops creating this resource. Existing GUI
polling/rearm and D-Bus listener behavior is outside this command's ownership
and is not claimed repaired by this case.

## Case-commit and integration ownership

The production change belongs only in `xpra/platform/printing.py` and removes
the one-shot command's unnecessary watcher initialization. The new
`unit.platformtests.printing_cli_test` belongs to this same behavior. The
existing `unit.scripts.main_test` remains selected unchanged: its actual
non-GUI subcommand subprocess boundary exposed the leak and must become green,
not skipped or quarantined.

No dependency on another fork case is required. This case applies alone to
the embedded source and does not alter packet handling, clipboard, native
Wayland state, encoding startup or shutdown timing. The complete stack still
exercises those shared application entrypoints together. The runner-owned
codec-startup tests remain enabled; no regression migration or skip replaces
their ownership. Fork-control files document the case but are not installed
production behavior.

## Commit scope and non-goals

The case does not edit PyCUPS, change GUI watcher startup/cleanup, make timer
threads daemon, add a polling loop, install a CUPS daemon, bind a host D-Bus
socket, change platform discovery or suppress a printer connection error.
There is no build-only or application-specific installed workaround. It also
does not redesign asynchronous job completion or printer export: this CLI
already returns after submission, and that contract is preserved.

Backend operations can still fail or take their existing bounded/native time;
the guarantee is that this command does not keep Python alive with a watcher
it never consumes. The fix does not claim arbitrary third-party backend calls
are timeout-free or that an unrelated embedding caller's threads terminate.

## Regression design and clean control

`unit.platformtests.printing_cli_test` has two complementary boundaries:

- Deterministic command tests run the actual CLI function with substituted
  synchronous backend results and watcher hooks. They cover information output,
  no-printer/unknown-printer/missing-file returns, default and explicit printer
  submission, and enumeration/configuration/submission exceptions. They require
  unchanged results and submission arguments, and no watcher acquisition or
  release. Clean source calls the existing initialization hook, so the first
  failure is a behavioral ownership assertion, not an absent API or fixture.
- The POSIX PyCUPS test requires the actual imported backend and launches the
  actual installed Xpra command as a subprocess. Private nonexistent CUPS and
  system-bus socket paths make external service absence deterministic. It
  requires the real connection error and a prompt nonzero process exit. A
  timeout kills/reaps that exact child; it is a failure, never accepted as an
  environment skip. Clean source starts the real non-daemon fallback and
  times out; the corrected command reports the same error and terminates.

Mocks in the first boundary do not prove thread/process exit; the second does.
The real subprocess test uses a platform gate only outside POSIX PyCUPS
platforms, not on the supported Ubuntu/Debian acceptance image. Windows and
Darwin native printing devices are not simulated as tested. Successful command
semantics are covered deterministically without inventing a CUPS protocol
server. The unchanged upstream `unit.scripts.main_test` additionally executes
its complete current subcommand inventory under ordinary runner configuration.

## Durable live or package boundary

All nine complete-stack live profiles remain required on both endpoints,
including Ubuntu Wayland server and Debian X11 client. Their printing feature
is deliberately disabled: rendering, paste and input success cannot prove this
CLI leak is fixed. The durable direct oracle is the installed-command
subprocess regression and unchanged upstream command module, in interpreted,
compiled and compatibility-disabled test modes.

Both distribution package builds remain part of refresh acceptance and package
the actual corrected platform module. Their codec imports, JPH roundtrip and
dependency checks prove package integrity, not a printing service integration.
No package-only shim or live fixture is substituted for the installed command.

## Invariants not to simplify

- Do not initialize a change watcher merely to enumerate printers or submit a
  synchronous one-shot command.
- Do not cancel a global watcher which this command did not acquire.
- Preserve the GUI printer subsystem's initialization, callback and teardown
  authority; do not change its thread policy to make CLI tests terminate.
- Keep the real subprocess-exit oracle and the original upstream subcommand
  test enabled. A mocked return, shortened timeout or removed PyCUPS dependency
  cannot replace them.
- Preserve connection failures and command return/submission semantics; no
  external daemon or host D-Bus exposure is an acceptance prerequisite.

## Required validation

Follow [canonical validation](../../docs/runbooks/validation.md): a tests-only
focused clean control must expose unnecessary watcher acquisition and actual
subprocess nontermination, followed by patched focused proof. The manifest
retains both durable modules and all three full upstream legs. Composed focused
interpreted/Cython/no-compat checks and the full nine-profile live loop remain
required; there is no declared case-only native target or isolated live escape.
The actual PyCUPS/installed-command boundary must execute on the supported test
image, not skip for a missing backend. Validate both real DEB builds on the
stable complete stack during this refresh. Scoped mypy remains confined to its
existing downstream-owned module list; this case adds no upstream-wide typing
obligation. Runtime results belong only in the ignored session ledger.
