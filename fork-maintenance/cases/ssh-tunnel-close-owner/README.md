# Terminate owned SSH tunnels outside the filtered parse thread

Code: the `Fork-Case: ssh-tunnel-close-owner` commit on `develop`
(`make -C fork-maintenance case-show CASE=ssh-tunnel-close-owner`).

## Boundary

On Linux, an exec-SSH connection with `exit_ssh=True` owns the SSH child and
must terminate and collect it when its transport closes. A protocol error or EOF can close
the transport on the network parse thread after that thread installs seccomp.
The existing synchronous callback calls `Popen.poll()` and `terminate()` there;
the parse policy excludes process waiting/signalling. With the default errno
action, termination fails with EPERM and leaves the owned tunnel running.
Even when signalling succeeds, returning immediately after `terminate()` does
not collect the child's exit status. Client cleanup can stop the UI loop and
clear its child-reaper registry before the child exits, leaving an SSH zombie.

This is distinct from a failed SSH handshake or an application refusing to
exit. Shared tunnels (`exit_ssh=False`), Paramiko's transport, and non-Linux
platforms retain their existing behavior. The regression closes a real local
child through the actual exec transport under the actual parse policy; it does
not infer the caller's thread from the operator's interleaved journal alone.

## Embedded-source context

The embedded source already moves privileged work out of parse handlers. In
particular, the maintainer's `a68b03baf5b` moves proxy shutdown dispatch to the UI
thread because cleanup cannot run under parse seccomp. The current
`FileTransferHandler.schedule_file_io()` starts its worker from the main
thread, explicitly to avoid inheriting the parse filter. The exec transport's
close callback still performs process operations synchronously on its caller.

`XpraClientBase.make_protocol()` already registers this `Popen` with the shared
child reaper. Its cleanup polls and clears registrations; it does not wait for
each still-running child, which would also affect deliberately shared tunnels.
The maintainer's `a5d8b04b73` reaps registered children through their own process
objects and preserves their wait locks/statuses; `1ab95c7d80` avoids stealing
unknown statuses when `waitid` is unavailable. Complete this transport's own
`Popen.wait()` instead of changing global reaper semantics or using `waitpid(-1)`.
The [closed SSH session discussion][ssh-session] and resulting `2d4665b759`
also require preserving the real `exit_ssh` boolean and its session/terminal
semantics, not globally changing `start_new_session` to solve cleanup.

The [closed seccomp discussion][seccomp-issue] establishes that default-on
filtering must be considered together with legitimate resource operations,
not silently disabled. The [maintainer's scope feedback][scope-review] calls
for separate fixes for independent behaviors. Accordingly, this case repairs
only the exec-SSH process owner, independently of libva and decoder fallback.
Retire it when an upstream replacement demonstrates owned-child termination
from filtered transport closure, including closure without a running UI loop.

## Surrounding code and ownership map

`exec_client.connect_to()` constructs the command and creates the `Popen` and
its pipes before returning `TwoFileConnection`. This setup already requires
an unfiltered caller: creating the child is not a parse-thread operation.
The connection owns the one-shot close callback; `TwoFileConnection.close()`
clears that callback before invoking it and subsequently closes the pipes.
Its low-level reads/writes use `os.read`/`os.write`, not buffered-reader calls.

`SocketProtocol.close()` can run on a network worker. It marks the protocol
closed, schedules connection-lost delivery, and closes the connection directly.
Neither this transport nor all of its command-line consumers own a UI main
loop. `stop_tunnel()` owns closing the original child's pipes and, only when
requested, polling and terminating that same `Popen`. It does not manage other
SSH processes, a process group, remote children, or a reusable global worker.

## Mechanism and lifecycle

For Linux owned tunnels only, connection setup starts one daemon worker with
an `Event`. The worker waits without polling. The existing close callback is
replaced with a request that sets the event and joins this worker; the worker
runs `stop_tunnel()`, waits up to one second for that same `Popen`, then kills
and waits up to one further second if SIGTERM was insufficient. Waiting/signalling
errors are logged rather than escaping as an unhandled worker exception. Thus the callback retains
its synchronous completion boundary without using the UI loop, while process
operations execute on the original unfiltered owner. The connection's existing
one-shot callback prevents duplicate termination requests. An already-exited
child is still not signalled; `Popen` retains its PID, wait lock and reaping
safeguards. Escalation targets only this owned channel process, never an
external mux master, process group or remote application. The wait is bounded
so an unresponsive child cannot hang the synchronous close indefinitely; an
uncollectable child remains an explicit failure, not a successful live result.

Each added element has one job: the Linux/ownership gate avoids new workers
where signalling is unnecessary; the event transfers one close request; the
setup-time worker avoids filter inheritance; the join preserves completion
before the close callback returns. The daemon does not introduce a new process
exit hang if a consumer abandons an unclosed connection. As before, consumers
must close their connections, and abrupt whole-process exit is not a guaranteed
cleanup protocol. This patch does not reorder protocol connection-lost delivery
or promise to repair an independently demonstrated process-exit race.

Starting a thread inside the filtered close callback would inherit seccomp
and reproduce the failure. Merely posting `stop_tunnel` to `GLib.idle_add`
would depend on a main loop that may have stopped or never run. The generic
background worker is lazily started and separately stopped, so borrowing it
would introduce both inheritance and global-lifetime dependencies. A new
process-reaper framework or a change to every protocol's close ordering is
unnecessary for this transport-local defect. Allowing `kill`/`wait4` in the
parse policy would broaden privileges instead of using the resource owner.

## Case-commit and integration ownership

The product commit changes `xpra.net.ssh.exec_client` and adds
`unit.net.ssh_tunnel_seccomp_test`. It has no case dependencies and applies
independently to the embedded base. The packet error-boundary case determines
how a failing handler reaches protocol close, while this case owns the exec
transport's eventual process cleanup. The server disconnect-flush case owns
outgoing shutdown ordering, not this child or callback. No shared harness or
new protocol packet is required to exercise the regression.

## Commit scope and non-goals

No syscall-policy changes, SSH options, credentials, retry logic, authentication,
remote commands, process-group signalling, Paramiko changes, UI scheduling,
generic connection abstraction changes or client/server shutdown refactor.
Windows and macOS keep the original callback; shared Linux tunnels remain
alive. Completion waiting is confined to the Linux-owned worker, not the
generic `stop_proc` helper or child-reaper policy. Native stderr-reader behavior and existing pipe-close error handling
are unchanged. A worker is added even when Linux seccomp is disabled: setup
cannot assume which thread will later close the transport, and transport
ownership must not depend on a mutable per-thread filter configuration.

## Regression design and clean control

`unit.net.ssh_tunnel_seccomp_test` uses the real `connect_to`, `Popen`, pipes,
connection callback and native parse filter. Only the SSH executable is
substituted with a local Python child; no network, server, keys or credentials
are used. The child sleeps without reading stdin, so pipe EOF cannot fake
successful termination. A dedicated caller installs the real filter, closes
the connection twice, and is joined before assertions. A wrapper around the
real `terminate()` records its native thread ID and Xpra filter state.

The errno regression requires actual SIGTERM exit, collected `Popen.returncode`
before close returns, and one unfiltered owner
different from the filtered caller. Tests-only embedded production reaches
the actual blocked termination, leaving the child alive; patched production
terminates it. A log-action control preserves the real syscall without errno
denial. Separate controls require shared tunnels to stay alive and already
exited children not to be signalled. A resistant real child publishes readiness
only after installing SIGTERM ignore; close must then kill and collect it under
the same native parse policy. The ordinary oracle never calls `wait()` or
`poll()` afterward: doing so would repair the leaked zombie inside the test.
Fixture cleanup still reaps its exact child after assertions, including failed
readiness and regression paths. Linux without the native seccomp subject
fails setup rather than skipping the defect. Other platforms are explicitly
outside this Linux-only unit's subject.

All fixtures register cleanup: close the connection, terminate a surviving
owned test child from the unfiltered test owner, wait, and kill only that exact
child if necessary. No polling loop, arbitrary process cleanup or event loop
is substituted for the fix. The retained SSH, SSH-server, bytestream and
protocol modules cover adjacent callers; `unit.seccomp_test` covers native
policy behavior. These are local process tests, not an authenticated remote
SSH integration or a proof of abrupt interpreter-exit cleanup.

## Durable live or package boundary

All nine complete-stack profiles remain required. Detach and transport loss
keep their direct TCP controls and additionally exercise four real owned
external OpenSSH-mux sessions each, following the canonical
[SSH lifecycle boundary](../../docs/runbooks/live-tests.md#owned-ssh-mux-lifecycle-coverage).
Actual attach/detach channels cannot fall back to fresh SSH; raw statuses and
birth-time-bound descendants must prove both successful detach and collection
of the client's direct SSH child. Master loss independently requires the
documented connection-loss result, never an arbitrary nonzero crash.
This is not proof of a remote lease/private-bus group teardown, signal-watcher
pipe correctness or the separately reported intermittent detach exit-code
race. Those require their own current-source reproductions. This Python transport change adds no
native package, link dependency, external service or Elsewindow setting.

## Invariants not to simplify

- Establish the process owner before entering a filtered worker; a new thread
  created by that worker inherits its restriction.
- Signal only the original owned `Popen`; preserve shared-tunnel admission.
- Keep the connection's one-shot callback and synchronous close completion.
- Do not depend on UI-loop progress for transport cleanup.
- Do not permit process signalling or waiting in the parse syscall policy.
- A pipe-EOF-only fixture is insufficient: assert actual child termination.
- The owner must record the exit status; a test-side wait must not mask a leak.

## Required validation

Follow [canonical validation](../../docs/runbooks/validation.md): tests-only
negative and patched focused native-filter/process checks, the manifest's
adjacent transport tests, composed focused coverage, all three final upstream
legs, full fork-control checks and the full nine-profile live loop. There is
no new scoped mypy module or native package payload. Ruff uses the embedded
configuration. Keep run identities, actual results and missing remote-SSH
coverage in the ignored session ledger rather than turning this explanation
into a historical acceptance report.

[seccomp-issue]: https://github.com/Xpra-org/xpra/issues/622#issuecomment-5972499066
[scope-review]: https://github.com/Xpra-org/xpra/pull/5023#discussion_r3861406986
[ssh-session]: https://github.com/Xpra-org/xpra/issues/4733
