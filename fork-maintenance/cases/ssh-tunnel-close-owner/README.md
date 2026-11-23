# Collect the owned SSH tunnel process when its transport closes

Code: the `Fork-Case: ssh-tunnel-close-owner` commit on `develop`
(`make -C fork-maintenance case-show CASE=ssh-tunnel-close-owner`).

## Boundary

An exec-SSH client connection with `exit_ssh=True` owns its `ssh` child and
terminates it when the transport closes (`stop_tunnel()` in
`xpra/net/ssh/exec_client.py`, the `close_cb` of its `TwoFileConnection`).
`stop_tunnel()` returned right after `terminate()`. The child is registered
with the client's child reaper, but that only collects children from the main
loop: when the transport closes during client shutdown the loop stops first,
and the terminated `ssh` stays behind as a zombie until the client process
exits. Inside a container whose PID 1 does not reap orphans the zombie
survives the client too: our live SSH profiles showed a direct `ssh` child
left as a zombie with PPID 1, which this case removed.

Shared tunnels (`exit_ssh=False`) and an already-exited child keep their
behavior: they are not signalled.

## Embedded-source context

Source: `d2940c7ddc6edd65aadb00f55ac559045d0c94ed`. The case used to own two
things. Its primary defect, `terminate()` failing with `EPERM` when
`Protocol.close()` ran on the seccomp-filtered parse thread, is now fixed
upstream by `defd56fa8c` (#622, "Close filtered protocols on the main thread
before notifying connection loss"): a close requested on a filtered thread is
re-queued to the main thread, "to keep transport cleanup (SSH termination,
...) off filtered threads", and main-thread cleanup still closes the protocol
if it runs first. The case's dedicated close thread (created while `Popen`
setup was still unfiltered) duplicated that and was removed. What remains is
the collection of the owned child, which upstream does not do.

The child reaper's design is the maintainer's: `a5d8b04b73` reaps registered
children through their own `Popen` objects, preserving their wait locks, and
`1ab95c7d80` avoids stealing unknown statuses when `waitid` is unavailable.
This case calls the transport's own `Popen.wait()` rather than changing the
reaper or using `waitpid(-1)`. `2d4665b759` (the closed SSH session
discussion) requires keeping the real `exit_ssh` semantics, which this case
does. `55d197dd07` ("Test exec SSH tunnel cleanup with normal child reaper
polling") fixes the upstream contract for a slow owned tunnel: it is
terminated, then collected by normal reaper polling, and never killed
(`test_owned_tunnel_reaped_by_normal_polling`). The case therefore only waits
briefly and no longer escalates to `SIGKILL`.

## Surrounding code and ownership map

| Path / symbol | Role |
| --- | --- |
| `exec_client.connect_to()` | Creates the `ssh` `Popen` and its pipes and returns the `TwoFileConnection` with `close_cb=stop_tunnel`. |
| `TwoFileConnection.close()` | Clears the one-shot callback, calls it, closes the pipes. |
| `stop_tunnel()` | Closes the pipes; for `exit_ssh` terminates the child and, with this case, collects it within 1 s; a child still running after that is left to the child reaper. |
| `SocketProtocol.close()` | Since `defd56fa8c` runs the transport close on the main thread when called from a filtered thread. |
| `XpraClientBase.make_protocol()` / child reaper | Registers the `Popen`; polls registered children from the main loop. |

## Mechanism

`terminate()` → `wait(timeout=1)`; on `TimeoutExpired` the child is logged as
left to the child reaper, which polls the same `Popen` from the main loop. An
`OSError` is logged like the existing termination error. The wait blocks the
closing thread for at most one second, only for an owned tunnel which is still
running; `ssh` normally exits on `SIGTERM` at once, which is the live zombie
case. The reaper's later `poll()` of the same `Popen` finds the status already
collected.

## Case-commit and integration ownership

The commit changes `stop_tunnel()` and adds
`tests/unittests/unit/net/ssh_tunnel_close_test.py`. It has no dependency.
`ssh-proxy-final-delivery` owns the remote relay, not the client's child.

## Commit scope and non-goals

No change to the command line, `exit_ssh` selection, shared tunnels, Paramiko
transports, the child reaper or the close thread selection (upstream's).

## Regression design and clean control

`ssh_tunnel_close_test` replaces only the `ssh` executable with a real Python
child; `connect_to()` builds the real `Popen`, pipes and callback. Closing the
connection must leave `child.returncode == -SIGTERM` without the test calling
`wait()` (clean source: `None`); a child ignoring `SIGTERM` must still be
running after the close (left to the reaper, never killed); a shared tunnel
must stay running and an exited one must not be signalled. Closing twice runs
the callback once. Upstream's `unit.net.ssh_exec_client_test` checks the
reaper side and that nothing is killed. Blind spot: the filtered thread path
is upstream's and covered by `unit.net.protocol_test`
`test_close_from_filtered_thread`.

## Durable live or package boundary

The live SSH detach and transport-loss profiles close exec-SSH transports with
the complete stack on both endpoints; all nine profiles are required.

## Invariants not to simplify

- Only owned (`exit_ssh`) tunnels which are still running are signalled and
  waited for.
- Collect through the transport's own `Popen`, never `waitpid(-1)`.
- Never escalate to `SIGKILL`: a slow owned tunnel belongs to the reaper
  (upstream `55d197dd07`).

## Required validation

Run the manifest's focused modules (patched, and tests-only as the clean
control), the full legs and the complete nine-profile live suite.
