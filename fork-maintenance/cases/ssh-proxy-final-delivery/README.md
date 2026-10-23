# Deliver the server's last packets through the ssh stdio proxy

Code: the `Fork-Case: ssh-proxy-final-delivery` commit on `develop`
(`make -C fork-maintenance case-show CASE=ssh-proxy-final-delivery`).

## Boundary

Every `ssh://` attach, detach or info command runs `xpra _proxy` on the server
host. `run_proxy()` connects it to the server's local socket and
`XpraProxy` copies bytes between that socket and its stdin/stdout, which are
the ssh channel. When the server disconnects a client (a detach, a shutdown,
a replaced connection), it writes a `connection-close` packet and then closes
its socket. The client must receive that packet: it then exits with status 0
(`server requested disconnect: detach request`). If only the end of the stream
arrives, the client reports `Connection lost` and exits with status 1
(`CONNECTION_LOST`), and an `xpra detach` check which also requires the
attached client's status fails although the detach command itself succeeded.

The relay loses the packet when the attached client is still sending (pointer
motion, draw acknowledgements, window events) at the moment the server closes:

1. The server has written its last packet and closed its socket.
2. The `->server` thread forwards the client's next packet; the write fails
   with `EPIPE`, which also raises `SIGPIPE` in that thread.
3. Either path ends the whole proxy: the copy loop's `finally: quit(0)`, or the
   `SIGPIPE` handler's `quit(141)`. `quit()` closes both connections and
   `force_quit()` exits the process.
4. The `<-server` thread has not yet copied the server's last bytes, which are
   still readable on its socket. They are never relayed.

Before the write fails the relay is healthy; afterwards nothing it does can
reach the server, but everything the server sent before closing must still
reach the client. The defect is in the relay process, independent of the
server's own disconnect ordering (owned by `server-shutdown-disconnect-flush`)
and of the client's ssh child management (`ssh-tunnel-close-owner`).

A second, user-visible symptom on the same path: the closed-socket error is a
`ConnectionClosedException`, which the copy loop's `except OSError` does not
catch. When the `finally` path wins the race the process exits before the
thread exception is printed; otherwise the relay prints an `Exception in thread`
traceback on its stderr, which ssh carries back to the client's terminal.

Unaffected: direct TCP/unix/WebSocket/SSL connections (no relay), Xpra's own
`bind-ssh` server (it connects the channel internally), and the client-side
ssh transport. The deterministic regression below demonstrates the loss with
the actual relay; the detach incidents also had a server-side cause (see
`server-shutdown-disconnect-flush`), so a successful detach on one transport
does not prove either case.

## Embedded-source context

The relay's teardown was changed by the main maintainer in `d4b64ee15b`
("better error handling in fdproxy"): an error in either copy loop now tears
down both directions through `finally: self.quit(0)`, so a failing direction
cannot leave the other one hanging (a zombie `_proxy`). The `SIGPIPE` handler
comes from `84f178c1b4` ("also listen for SIGHUP and SIGPIPE on Posix to try
harder to prevent zombies"). Both goals stay valid: a relay whose client has
gone must end. Neither change considered the server closing first while the
client is still sending, where ending at once discards the data the server
sent last. The public upstream history of `xpra/scripts/fdproxy.py` has no
later change (checked read-only on the canonical repository).

The same principle has been applied by the maintainer to the protocol itself:
for the historical detach issue
[#581](https://github.com/Xpra-org/xpra/issues/581) ("xpra detach may cause a
connection lost rather than a clean disconnection"), `6d9054a849` waits for the
disconnect packet "to be actually sent ... before closing the socket", and
`26dc1a3920` stopped other code from closing the protocol prematurely. This case
applies that principle to the relay: the last packet already sent by the
server is delivered before the relay ends. The protocol layer already treats
`ConnectionClosedException` as an expected closure
(`SocketProtocol._io_thread_loop()`, `rfb/protocol.py`), which is the precedent
for catching it here instead of letting it escape as a thread traceback.

Retire this case when an upstream relay delivers everything the server wrote
before closing even when writing towards the server has already failed, and
still ends promptly when the client side goes away.

## Surrounding code and ownership map

| Owner | Responsibility |
| --- | --- |
| `xpra/scripts/main.py:run_proxy()` | connects `server_conn` (the session socket), wraps stdin/stdout in a `TwoFileConnection` and runs `XpraProxy` |
| `XpraProxy.__init__()` | installs `SIGINT`/`SIGTERM`/`SIGHUP`/`SIGPIPE` handlers which call `quit()` |
| `XpraProxy.run()` | starts both copy threads, joins them, then `quit(0)` |
| `_to_client_loop()` | `<-server`: server socket → stdout (the ssh channel) |
| `_to_server_loop()` | `->server`: stdin → server socket |
| `XpraProxy.quit()` | records the first exit code, closes both connections, `force_quit()` |
| `Connection` (`xpra/net/bytestreams.py`) | `can_retry()` turns `BrokenPipeError` and abort errnos into `ConnectionClosedException` |

The relay owns no protocol state: it copies opaque bytes, so it cannot know
which packet is "last". The only safe rule is stream based: whatever the server
has written before closing is delivered, and the relay ends when the server
stream ends or the client side fails.

## Mechanism and lifecycle

### Stream ends, not write failures, end the relay

`_to_server_loop()` now passes `drain=True`. In `_copy_loop()` a
`ConnectionClosedException` raised while *writing* in that direction means
the server has closed its end; the loop sets `self.draining`, logs at debug
level and returns **without** calling `quit()`. The `<-server` direction keeps
reading the server socket; on a unix socket the bytes queued before the close
are returned before end-of-stream or `ECONNRESET`, and once it reaches either it
calls `quit(0)` exactly as before. Reading from the client (end of stdin) and
every other error in either direction still end the relay immediately: the
client side is gone, so there is nobody to deliver to.

`writing` is set only around the inner write loop, so a read failure in the
`->server` direction (client stdin) is never mistaken for a server close. Only a
`ConnectionClosedException` while writing drains; any other `OSError` towards
the server keeps the previous immediate teardown.

### SIGPIPE stays a client-pipe signal

A failed write raises `SIGPIPE` in the writing thread. The `->server` thread
blocks `SIGPIPE` for itself (`signal.pthread_sigmask`, POSIX only) before it
starts copying, so a write towards a closed server returns the error to the
loop instead of ending the process from the handler. The `<-server` thread does
not block it: a broken client pipe still raises `SIGPIPE` and ends the relay
through the handler (or through its own write error), preserving the
zombie-prevention behaviour of `84f178c1b4`. A blocked, thread-directed
`SIGPIPE` stays pending on that thread and is discarded when it exits.

### Logging

Both loops now also catch `ConnectionClosedException`. While draining, the
`<-server` loop's final `ECONNRESET` (the server closed with unread client
data) is expected and is not reported as a warning; other failures while the
relay is active keep the single warning line introduced by `d4b64ee15b`.

### Limits

Draining relies on the server closing its socket, which it does after its last
write or when it exits; `flush_then_close()` also closes after its own fallback
timeout. The relay does not add timers. Windows named pipes keep the same
copy-loop logic without the signal mask (no `SIGPIPE` there).

## Case-commit and integration ownership

The commit changes only `xpra/scripts/fdproxy.py` and adds
`tests/unittests/unit/scripts/fdproxy_test.py`. It has no dependency. Related
cases own different steps of the same disconnect:

- `server-shutdown-disconnect-flush`: the server writes the disconnect packet
  before its protocol is closed (including late client packets after a
  detach). Without it the relay has nothing to deliver; without this case the
  written packet can still be lost in the relay.
- `ssh-tunnel-close-owner`: the *client* side terminates and collects its own
  ssh child after the transport closes; unrelated to the remote relay.
- `protocol-parse-start-lifecycle`: client/server protocol worker startup;
  no shared code.

`case-check` proves textual independence; the composed live detach scenarios
exercise the server, the relay and the client together.

## Commit scope and non-goals

Unchanged: the relay's buffer size, the exit codes it reports, `run_proxy()`'s
connection setup, client-side ssh handling, and the protocol encoding. No
timer, polling, extra thread or protocol awareness is added. Not a fix for a
server which never writes its disconnect packet, for the client-side ordering
of a write failure against reading already received data, or for TCP resets
on direct connections.

Rejected alternatives: ignoring `SIGPIPE` process-wide (would change the
client-pipe teardown and its exit status), removing the `finally` teardown
(re-introduces relays hanging after one direction fails), or a fixed grace
delay before quitting (delays every relay exit and still races).

## Regression design and clean control

`unit.scripts.fdproxy_test` runs the real `XpraProxy` with a real
`TwoFileConnection` over two `os.pipe()`s and a real `SocketConnection` over an
`AF_UNIX` `socketpair()`. Only `force_quit` is replaced so that the test process
survives; signal handlers are restored afterwards.

- `test_last_server_packet_reaches_client_sending_after_close`: the server
  writes more than the relay's buffer plus the client pipe can hold, then its
  last packet, and closes; the client then writes. The client must receive every
  byte including the last packet, and the relay must end with 0. On the clean
  source the client receives only what was relayed before the write failure
  and the last packet is missing.
- `test_server_close_ends_proxy`, `test_client_end_of_stream_ends_proxy`,
  `test_broken_client_pipe_still_ends_proxy`: controls that both directions
  still end the relay (the broken client pipe through the loop or the
  `SIGPIPE` handler); they pass on clean and patched source.

`unit.net.bytestreams_test` is the adjacent upstream control for the
`Connection`/`SocketConnection` read and write paths the relay copies through
(peeked reads, `recv_into`, socket timeouts); the case does not change
`bytestreams.py`, and the module must keep passing unchanged.

The clean failure is the missing data, not a missing API or fixture error.
Blind spots: real ssh flow control and a real attached client are covered by
the live detach scenarios, not by this unit test.

## Durable live or package boundary

The live `live-xpra-detach` profile attaches the real packaged client through
the owned OpenSSH mux, so every SSH scenario runs the packaged `xpra _proxy`
on the server. The churn detach scenarios keep the client sending (pointer
motion, multi-window/notification churn on a non-composited desktop) while
`xpra detach` runs, and require detach command status 0, attached client
status 0 with the `detach request` reason, server survival and owned reaping.
The relay race itself was not separately observed live once the server-side
close was fixed; the deterministic unit test is its proof, the live scenarios
prove the composed path does not regress.

## Invariants not to simplify

- A failed write towards the server must not end the relay while the server
  stream still has data; the end of the server stream ends it.
- `SIGPIPE` is blocked only in the `->server` thread; the client pipe must keep
  ending the relay.
- `writing` must bracket only the write loop, so a client-side read failure
  still ends the relay at once.
- Catch `ConnectionClosedException`: the connections raise it, not `OSError`.

## Required validation

Follow [canonical validation](../../docs/runbooks/validation.md): clean
tests-only control failing `test_last_server_packet_reaches_client_sending_after_close`
with the controls passing; patched focused run of the manifest modules; the
three full upstream legs; the exact matching complete-stack DEB artifacts; and
the complete nine-profile live suite including the SSH and churn detach
scenarios.
