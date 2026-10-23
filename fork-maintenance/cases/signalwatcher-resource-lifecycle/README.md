# Complete signal watcher resource cleanup

Code: the `Fork-Case: signalwatcher-resource-lifecycle` commit on `develop`
(`make -C fork-maintenance case-show CASE=signalwatcher-resource-lifecycle`).

## Boundary

The POSIX window signal watcher is a local child with stdin/stdout pipes. On
last-window destruction, HUP/ERR or client cleanup, Xpra must close every pipe
and collect that child's status even when its UI loop is ending. Previously an
already-exited child returned before stdin closure; a broken stdin write skipped
closure too. The forced-stop path sent SIGKILL but returned without waiting.
An unclosed buffered writer or uncollected child could therefore survive cleanup.

This case owns that resource boundary, not every detach error. A warning's
location in GTK/OpenGL cleanup is not proof of its allocation site, and a
signal-watcher leak does not establish the cause of an intermittent nonzero
detach command or the independent server heap-corruption abort. Disabling
signal forwarding is not the fix. Configurations without a watcher create no
new resource and retain their existing behavior.

## Embedded-source context

The source embedded in `develop` already has the maintainer's `81096f3320`
unconditional watcher-pipe cleanup and `fd6177b0ba` forced stop rather than a
future UI timer. The later subsystem split retains the same helper routes.
Neither change closes stdin on all paths or collects a forcibly killed child.
Retain this case until an upstream equivalent establishes both postconditions
through last-window, already-dead and reaper-callback cleanup.

The [closed resource-warning discussion][warning-location] cautions that GC
can report a leak at an unrelated source location. The maintainer also
[identifies the watcher's actual role][watcher-role] and emphasizes avoiding
unnecessary UI I/O. Accordingly, this patch observes the real process/pipe
owner, preserves existing notification/TERM behavior and adds completion
waiting only after forced kill. It does not alter OpenGL, suppress warnings,
disable the watcher or refactor client shutdown. The original maintainer
commits and actual discussion constrain the design; they are not approval
of this downstream patch.

## Surrounding code and ownership map

`WindowSignalWatcher.assign_signal_watcher_pid` creates one `Popen` per remote
PID, registers it with `ChildReaper` and installs a GLib stdout I/O source.
Several window IDs can share that process. `WindowManagerClient.destroy_window`
removes the closing ID and calls `kill_signalwatcher` only for the last window,
then removes the PID association. The window subsystem's composed cleanup
destroys windows and performs a remaining-watchers sweep.

HUP/ERR dispatch also calls `kill_signalwatcher`. The reaper's queued
`watcher_terminated` callback instead calls `clean_signalwatcher` directly.
Therefore closing stdin only in the kill path would leave the latter path
incomplete. These callbacks and normal window cleanup execute on the UI owner;
the change adds no cross-thread scheduling. `ChildReaper.sigchld` queues UI work,
and its registered-process polling preserves the original `Popen` status lock.

The helper process (`fs/libexec/xpra/xpra_signal_listener`) reads one stdin
line and forwards selected Unix signals through stdout. Its protocol, signal
handlers and remote-window behavior remain unchanged.

## Mechanism and lifecycle

For a running child, the existing exit line is written/flushed before all pipe
cleanup. A closed stdin is not written again. The cleanup helper runs in
`finally`, so a broken write cannot bypass it; already-dead children invoke it
before returning. `clean_signalwatcher` still removes the GLib source first,
then independently closes stdout/stderr through `noerr`, and now closes stdin
the same way. The reaper callback thus releases all three streams as well.

These operations are serialized on the UI owner: the short exit write does not
dispatch another GLib callback. The I/O source is removed before process-stop
operations and before returning to the loop. No live read source is left
watching closed streams. Repeated cleanup sees source0 and closed streams,
which remain safe to close again.

The inherited graceful/TERM path still polls and waits up to10ms. If that wait
expires, the same `Popen` is killed and waited for up to one second. The normal
path gains no longer grace period; the bound is a deadline, not a sleep.
`Popen.kill` retains Python's process/status safeguards instead of signalling
an unrelated recycled PID directly. An already-collected child is not killed.
Kill/wait failure remains an explicit diagnostic; it does not turn an
uncollectable child into a successful live teardown. No later UI callback is
required to perform the wait.

## Case-commit and integration ownership

The case changes only the watcher cleanup module and adds
`unit.client.signalwatcher_cleanup_test`. It has no case dependencies: the
helpers and callers exist on the embedded base, and its diff is independent.
The window manager's decoder-capability notification in
`video-decoder-capability-loss` is a separate behavior in an unchanged caller.

`ssh-tunnel-close-owner` handles an exec-SSH child which may be closed by a
filtered protocol worker; this watcher already has an unfiltered UI owner.
Reusing its worker/event mechanism here would add unnecessary threading.
`protocol-parse-start-lifecycle` and `server-shutdown-disconnect-flush` retain
their distinct startup and packet-flush ownership. None establishes these
watcher pipe/wait postconditions. Shared live/raw-output/process fixtures
remain fork control code, not part of this installed product commit.

## Commit scope and non-goals

No changes to window/PID maps, feature defaults, signal-listener discovery,
forwarded signals, subprocess command lines, wire protocol, seccomp, native
rendering or the generic child reaper/`stop_proc`. No process groups, reaper
framework, new timer, daemon or platform dependency. The POSIX watcher remains
enabled under its existing gate; other platforms keep their admission policy.
Cleanup uses portable `Popen` APIs but the regression does not claim native
Windows/macOS deployment coverage.

The bounded forced wait is necessary because interpreter/UI shutdown cannot
depend on a later SIGCHLD dispatch. Merely closing stdin can leave a child which
does not consume it; merely killing does not collect status. Merely calling
`wait` in a test would hide the ownership defect. Broader handling of combined
GLib conditions or spontaneous watcher replacement is not bundled here.

## Regression design and clean control

The new module uses actual local `Popen` children and pipes, joined readiness
through `select`/a line, the real cleanup functions and a real GLib I/O source.
It does not replace stream closure, process signalling or waiting with mocks.
The forced child closes its stdin reader, ignores SIGTERM and announces
readiness only after both operations. Missing GI/subject imports fail; the
POSIX gate identifies the actual platform subject rather than optional absence.

Independent assertions cover already-exited stdin, direct reaper-helper cleanup,
broken-write stdin, forced child status before return, a running child with
already-closed stdin, ordinary repeated cleanup and removal of the registered
GLib source. The status assertion never waits or polls after the owner returns.
Fixture cleanup, registered before readiness assertions, kills/waits only its
exact child and closes remaining streams after the assertions. No UI main loop
is needed to rescue the owner.

The tests-only clean control reaches existing functions: its failures concern
open stdin, uncollected status or writing a closed stream, not a missing API.
Adjacent window/window-draw tests cover the composed consumer, child-reaper
tests preserve callback/status handling, and command-client tests retain CLI
exit behavior. The test does not construct GTK windows, execute the installed
signal-listener script, forward a real Unix window signal or prove that this
resource defect explains the reported intermittent detach exit code.

## Durable live or package boundary

The complete-stack GTK detach/transport-loss fixtures create actual remote
windows and their client watchers under a real window manager. Their raw peer
stdout/stderr, exact detach/client/server exit codes and birth-time-bound direct
child reaping must remain clean. Repeated real SSH-mux sessions supplement TCP;
successful rendering or a zero command status cannot hide a leaked watcher.
All nine complete-stack profiles remain required on both packaged endpoints.

The broader Task1 persistent-systemd and notification/multiwindow teardown
flows still need their own actual reproduction and exit attribution. This
case does not claim to provide those flows or resolve the server's native
abort. The Python-only change adds no native dependency, package split or
Elsewindow configuration requirement; exact final DEBs must carry the module.

## Invariants not to simplify

- Send the exit request before closing stdin, but always close it on failure.
- Keep all pipe closure in the helper also used by the child-reaper callback.
- Remove the actual GLib source before returning to UI dispatch.
- Collect the exact `Popen` after forced kill, without a future UI timer.
- Keep a finite forced wait; do not extend normal graceful-stop latency.
- Observe child status before test cleanup; do not repair the defect in the oracle.

## Required validation

Follow [canonical validation](../../docs/runbooks/validation.md): non-vacuous
tests-only and patched focused checks with all manifest modules, composed
coverage and final interpreted/compiled/no-compat full upstream legs. Run
affected fork controls, normal Ruff/whitespace/case/stack checks, both final
DEB builds and the full nine-profile live loop with its uninterrupted final
pass. There is no new module in the separate scoped mypy inventory. Runtime
results and unresolved Task1 causes belong in the session ledger, not claims
that these designed checks have already passed.

[warning-location]: https://github.com/Xpra-org/xpra/issues/4139#issuecomment-2141601212
[watcher-role]: https://github.com/Xpra-org/xpra/issues/4139#issuecomment-1953694192
