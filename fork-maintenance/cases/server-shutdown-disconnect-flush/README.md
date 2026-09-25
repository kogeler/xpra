# Flush the shutdown disconnect before closing client protocols

Code: the `Fork-Case: server-shutdown-disconnect-flush` commit on `develop`
(`make -C fork-maintenance case-show CASE=server-shutdown-disconnect-flush`).

## Boundary

When a server exits cleanly while a client is attached, the client must be
told why: `ServerCore.cleanup()` sends every protocol a `connection-close`
packet with `server shutdown` (or the exit/upgrade reason), and the client then
logs `server requested disconnect` and exits with status 0. Instead, the
client intermittently logs only `Connection lost` and exits with status 1,
although the server logged `Disconnecting client ... server shutdown`.

The smallest trigger is any clean exit with a connected client, for example
`--exit-with-children` when the last child ends, `xpra stop`, or a signal.
The failure is user visible (`xpra attach` reports an error for a normal
server shutdown) and breaks scripts and the live lifecycle oracle which bind a
successful client exit to the server's shutdown. The client correctly reports
what it received; the server loses the packet.

The queued disconnect can be lost in three independent ways, and this case
closes all three:

1. **A late client packet closes the connection.** A packet which the client
   sent before it saw the disconnect reaches the server after the client has
   been detached, and `ServerCore.handle_invalid_packet()` closes the protocol
   at once, under the disconnect write.
2. **`flush_then_close()` counts a dequeued packet as sent.** It closes as
   soon as the write queue is empty, while the write thread may still be
   writing the packet it has just taken off that queue.
3. **No flush interval before the server exits.** Nothing gives the write
   threads time to send the packet before the server process exits.

Evidence:

- The live keyboard profile showed the first way. The application exited,
  the client destroyed its window and sent a `focus` packet 3 ms before the
  server logged `Disconnecting client`. The server protocol logged its close
  statistics (115 received, 88 sent) *between* `Disconnecting client` and
  `client 1 disconnected`, so on another thread while the main thread was
  still inside `disconnect_protocol()`. The client had sent 115 packets and
  received 88: its last packet had arrived and the disconnect had not been
  sent. The run has no network debug log; the attribution is an inference: the
  only close on that route which needs no peer action and logs nothing is the
  parse thread's `handle_invalid_packet()`, and nothing in the stack closes
  the protocol there otherwise. The unit test below demonstrates the close.
- Two earlier clipboard runs lost the disconnect too (67 sent/66 received
  with the server's complete input read, then 20 sent/20 received with the
  close logged in the same millisecond as the disconnect). Their logs cannot
  tell which of the three ways was taken.
- The second way is a code-reading finding, demonstrated deterministically by
  `unit.net.protocol_flush_test`; the third is the upstream regression
  described below.

Whether the packet gets out depends on thread scheduling and on what the
client sends while the server shuts down, so most exits deliver it.
Disconnects outside server shutdown (a kicked or replaced client) can also
meet the first way; they are not part of this case, see the non-goals.

## Embedded-source context

The case resolves against embedded source
`0a80430b6506e403f6469416d8aaa8463e331296`.

`ServerCore.cleanup()` in `xpra/server/core.py` dispatches subsystem cleanup,
calls `cleanup_all_protocols()`, then `do_cleanup()`, then
`cleanup_sockets()`. For every protocol, `disconnect_protocol()` logs
`Disconnecting client`, calls `protocol.send_disconnect(reasons)` and then
`cleanup_protocol()`, which removes the protocol from `_potential_protocols`
and its source from the `client-session` subsystem: from then on the protocol
is *detached*.

Incoming packets are dispatched on the protocol's parse thread:
`ServerCore.process_packet()` passes `authenticated=bool(source)` to
`PacketDispatcher.dispatch_packet()` in `xpra/net/dispatch.py`, which calls
`handle_invalid_packet()` for a packet type without an unauthenticated
handler. `ServerCore.handle_invalid_packet()` ends with `if not ss:
proto.close()`. That rule came with upstream `d9ae6888ae` (#4885, client
sessions as a subsystem) and was kept, with a test asserting the close, by
`617ed23485` ("avoid errors for packets from detached clients"), which only
silenced the error log for detached protocols. Earlier `ServerBase` code had
the same close for protocols without a source.

`SocketProtocol.do_flush_then_close()` in
`xpra/net/protocol/socket_handler.py` is what `send_disconnect()` uses. It
queues the last packet on the protocol's one-slot write queue, then checks
`wait_for_packet_sent()` immediately and every 100 ms afterwards, and treats
`self._write_queue.empty()` as "sent". The write thread (`_write()`) takes an
item off the queue *before* writing it, and `write_buffers()` stops as soon as
the protocol is closed. The docstring promises "we wait again for the queue to
flush"; an empty queue was meant as "flushed".

`ServerCore.do_cleanup()` is only `sleep(0.1)` with the comment "allow just a
bit of time for the protocol packet flush". `ServerBase.do_cleanup()` in
`xpra/server/base.py` overrides it without calling `super()`. Before upstream
`8b532b2967` (#4885, composition instead of inheritance) that override ran
every mixin's `cleanup()` itself, after the disconnect packets had been
queued, which implicitly gave them time to leave. The refactor moved subsystem
cleanup ahead of `cleanup_all_protocols()` and left the override emitting only
the `exit` server event.

All three defects are in the previous embedded source too; the new upstream
does not change these paths. An upstream replacement must establish, for every
server class derived from `ServerCore`/`ServerBase`:

- a packet received during shutdown from a protocol without a source does not
  close that protocol;
- `flush_then_close()` considers the last packet sent only once the write
  thread has finished writing it;
- there is a flush opportunity between queuing the disconnect packets and the
  server's exit, and the `exit` server event is kept.

## Surrounding code and ownership map

| Owner | Responsibility |
| --- | --- |
| `ServerCore.clean_quit()` | Arms a 5 s `force_quit`, marks the server closing (`_closing = True`), runs `cleanup()`, then `quit_worker()`. |
| `ServerCore.cleanup()` | Subsystem cleanup, `cleanup_all_protocols()`, `do_cleanup()`, `cleanup_sockets()`, asyncio loop stop. |
| `ServerCore.disconnect_protocol()` / `cleanup_protocol()` | Log `Disconnecting client`, `protocol.send_disconnect(reasons)`, then detach the protocol: remove it from `_potential_protocols` and its source from `client-session`. |
| `ServerCore.process_packet()` / `PacketDispatcher.dispatch_packet()` | Parse thread: route each packet by type and by whether the protocol still has a source; unknown ones go to `handle_invalid_packet()`. |
| `ServerCore.handle_invalid_packet()` | Logs unexpected packets and closes protocols without a source; with this case not while the server is closing. |
| `SocketProtocol.do_flush_then_close()` | Best effort: takes the write lock, waits for the write queue, queues the last packet and closes once it is sent (checked at once, then by GLib timers, with a 5 s fallback). With this case "sent" means that the write thread finished that queue item. |
| Protocol write thread (`_write()`, `write_items()`, `write_buffers()`) | Daemon thread: takes one item off the write queue, writes it and stops writing once the protocol is closed. With this case it marks each item done (`task_done()`) after writing it. |
| `ServerBase.do_cleanup()` | Emits the `exit` server event; with this case it also keeps `ServerCore`'s flush interval. |
| `quit_worker()` -> `ServerCore.quit()` -> `late_cleanup()` -> `do_quit()` | Force-closes the protocols still registered (`get_all_protocols()`: potential protocols and sources); detached protocols are not among them. Then the main loop quits and the process exits, ending the daemon write threads. |

Order on a clean exit:

```text
clean_quit                              _closing = True
  cleanup
    _dispatch_fire("cleanup")           subsystems
    cleanup_all_protocols()
      disconnect_protocol(p)
        p.send_disconnect()             queue connection-close
        cleanup_protocol(p)             detach p
                                        <- late client packet: parse thread
    do_cleanup()                        ServerBase: exit event, flush interval
    cleanup_sockets()                   listening sockets
  quit_worker(quit)
    quit -> late_cleanup                registered protocols only
         -> do_quit                     main loop quits, process exits
```

## Mechanism and lifecycle

### Late client packets during shutdown

A client keeps sending while the server shuts down: pointer, focus and window
packets, for example the `focus` and `window-unmap` packets which a client
sends when the application's window disappears just before
`--exit-with-children` ends the server. Once `cleanup_protocol()` has detached
the protocol, such a packet is no longer authenticated, has no unauthenticated
handler and reaches `handle_invalid_packet()` on the parse thread, which closed
the protocol immediately. `close()` sets `_closed`, closes the socket and
replaces the write queue; the disconnect packet is dropped or cut off, and the
client sees the connection end without it.

With this case `handle_invalid_packet()` does not close a protocol without a
source while `self._closing` is set; logging is unchanged (detached protocols
are only debug-logged, and errors were already suppressed while closing).
During shutdown every protocol which the server has registered is
disconnected by `cleanup_all_protocols()` in the same `cleanup()`, and every
detached protocol has its disconnect in flight: `flush_then_close()` closes it
once the packet is sent (if the main loop still runs), and otherwise the
process exit does. Outside shutdown, packets from protocols without a source
still close them as upstream intends.

### Sent means written

`send_disconnect()` only queues the packet. `flush_then_close()`'s first
`wait_for_packet_sent()` runs right after queuing; if the write thread has
just taken the packet off the queue, the queue is empty and the protocol
closes while the packet is being written.

The write thread now keeps a reference to the queue it took an item from and
calls `task_done()` on it once `write_items()` returns (also when the write
raised), so the standard `queue.Queue` count of unfinished items drops only
after the write. `flush_then_close()` binds the queue it put the last packet
on, and `wait_for_packet_sent()` reports the packet sent only when that queue
has no unfinished item, or the protocol is already closed. Every other rule of
`flush_then_close()` stays: the write lock is held from before queuing until
`close_and_release()`, so the format thread cannot queue anything after the
last packet; the 100 ms re-check and the 5 s fallback close are unchanged.

After `close()`, `terminate_queue_threads()` replaces the write queue with a
`SimpleQueue` of exit markers. The write thread returns on those markers
before `task_done()`, and a `flush_then_close()` that raced a close sees
`closed` first, so neither ever needs the counter of that queue.

### The flush interval

The write threads are daemon threads, and a detached protocol is not closed by
`late_cleanup()`, so without a pause the main thread goes from queuing the
disconnects through `cleanup_sockets()` and `quit()` to the process exit while
the packets may still be unwritten. `ServerBase.do_cleanup()` now calls
`super().do_cleanup()` after emitting the `exit` event, restoring
`ServerCore`'s 0.1 s sleep between queuing the disconnect packets and closing
the sockets. The main thread sleeps; the write threads deliver the small
`connection-close` packets. Subsystem cleanup has already run and the `exit`
event still precedes the interval.

### Limits and cost

This remains the existing best-effort contract, not a delivery guarantee: a
peer which stops reading, or a write queue still holding a large backlog, can
exceed the interval, after which the server exits as before. There is no new
lock, thread, retry or timer. `task_done()` adds one uncontended lock per
written packet, next to the `put()`/`get()` the queue already does for it.

Outside server exit, a disconnect now closes when the packet has been written
rather than when it was dequeued: when the first check is too early, that
close moves to the next 100 ms re-check, the delay the same code already had
whenever it found the packet still queued.

A protocol between `accept_connection()` and the registration of its source
is in neither `_potential_protocols` nor the sources, so shutdown does not
disconnect it; a packet from it during shutdown no longer closes it either,
and the process exit closes it instead.

The interval runs once per server exit, with or without connected clients
(with none it only delays exit by 0.1 s, as `ServerCore` intended). Proxy and
other servers that do not derive from `ServerBase` keep their own
`do_cleanup()`, but get the other two fixes.

## Case-commit and integration ownership

The case commit changes `xpra/net/protocol/socket_handler.py`,
`xpra/server/core.py` and `xpra/server/base.py`, and adds
`tests/unittests/unit/net/protocol_flush_test.py` and
`tests/unittests/unit/server/shutdown_flush_test.py`. No other case commit
touches `SocketProtocol`, `ServerCore.handle_invalid_packet()`,
`ServerBase.do_cleanup()` or the protocol close path, and it has no
dependencies: it applies alone on the upstream base and is removable from
`HEAD` (`case-check`). The client and every other protocol user share
`SocketProtocol`; the RFB protocol has its own write queue and is not changed.

`packet-handler-error-boundary` owns exceptions escaping packet handlers in
`xpra/net/dispatch.py`; this case does not change dispatch and relies only on
`handle_invalid_packet()` being the route for unauthenticated packets.
`video-pipeline-cleanup-race` and `wayland-subsurface-stream-ownership` own
per-connection encode and packet lifetimes inside `ClientConnection`; they run
during `cleanup_protocol()` and are unaffected by when the socket closes.

## Commit scope and non-goals

The case keeps a disconnecting protocol open until its last packet has been
written during shutdown, makes `flush_then_close()` wait for the write it
already meant to wait for, and restores the flush interval. It does not change
the disconnect reasons, the order of queuing, the write lock, the 100 ms
re-check, the 5 s fallback, `force_quit`'s 5 s bound, packet routing or client
packet handling.

Not changed on purpose:

- Packets from protocols without a source still close them outside shutdown.
  The same race can drop the reason of a single-client disconnect (a kicked or
  replaced client); protecting every pending disconnect needs protocol state
  that the server can query, a separate change which no required gate
  exercises.
- Delivery is not made synchronous (for example by joining write threads or
  blocking the main thread on the queue), which would change shutdown latency
  and ownership for every transport; a longer interval would only slow every
  exit.
- Counting `output_packetcount` instead of unfinished items is not exact: when
  the last packet is queued, an earlier item may still be being written.
- Stopping the read thread during the flush would leave unread data in the
  socket, and closing such a socket resets the connection.
- Treating `Connection lost` at server exit as success in the live oracle
  would hide the user-visible error instead of fixing it.

## Regression design and clean control

Each way of losing the packet has its own test, so one failure cannot hide
another.

`unit.server.shutdown_flush_test`:

- `test_late_client_packet_does_not_close_the_disconnecting_protocol` calls
  the real `ServerCore.handle_invalid_packet()` with a closing server
  namespace, no source, no potential protocols and a mock protocol receiving a
  `focus` packet, in the style of upstream's
  `test_handle_invalid_packet_from_detached_protocol`. It requires no error
  log and no `close()`. On clean source `close()` is called.
- `test_disconnect_packets_get_the_flush_interval_before_sockets_close`
  constructs a real `ServerBase` object without running its initializer and
  calls the real `ServerCore.cleanup()`. It substitutes only the side effects:
  subsystem dispatch, protocol disconnection, the `exit` server event, the
  flush `sleep` in `xpra.server.core`, socket close and the asyncio loop stop,
  each recording its position. It requires the order subsystems, disconnect,
  `exit` event, a 0.1 s flush, sockets. On clean source the flush entry is
  missing.

`unit.net.protocol_flush_test` drives a real `SocketProtocol` through
`send_disconnect()` with a connection whose write blocks until the test
releases it and a scheduler whose timers only run when the test calls them.
Its `raw_write()` waits until the write thread has taken the packet off the
queue and entered the write, which fixes the losing interleaving. The test
requires that the protocol is still open, nothing was closed or written, and
exactly one 100 ms re-check was scheduled; after the release it polls that
re-check until it closes and requires the connection to see the
`connection-close` write before its close, and the done callback to run. On
clean source the protocol closes during `send_disconnect()`, so the test fails
on that assertion.

All three fail on the defect itself, not on an import or fixture. Upstream's
`unit.server.core_test` (which requires the close for a detached protocol
while the server is not closing) and `unit.net.protocol_test` cover the
surrounding behaviour.

Blind spot: the unit tests prove each part separately, with mocks, a fake
connection and no main loop. That a real server delivers a real packet on a
real socket while its client keeps sending is the live boundary below.

## Durable live or package boundary

Every live profile with the application-exit lifecycle ends with the
application exiting, the server exiting through `--exit-with-children`, and the
client required to exit successfully. The lifecycle report binds
`client_exit_status` 0 after the server's exit; the clipboard profile
additionally binds it to its terminal authority check. The keyboard and
clipboard profiles are where the lost disconnect was observed: the
application's window disappears right before the server exits, so the client
is still sending. These run with the complete stack on both endpoints; all
nine profiles are required.

## Invariants not to simplify

- Keep the `self._closing` condition on the close in
  `handle_invalid_packet()`, and keep the close outside shutdown (upstream's
  test requires it).
- Keep `task_done()` in `_write()` after `write_items()` returns, in a
  `finally`, and on the queue the item came from, not on `self._write_queue`
  read again: after a close that is a `SimpleQueue` without `task_done()`.
- Keep `wait_for_packet_sent()` bound to the queue the last packet was put on
  and checking `closed` first. An empty queue is not a sent packet.
- Keep `super().do_cleanup()` in `ServerBase.do_cleanup()` after the `exit`
  event: the interval must fall between `cleanup_all_protocols()` and the
  server's exit.
- Do not move subsystem cleanup back between the disconnect and the exit to
  recreate the delay implicitly; its order is upstream's.
- Do not replace the interval with a GLib timer: `quit()` may run
  synchronously after `cleanup()`, so a timer need not fire before the exit.

## Required validation

Follow [development and final acceptance](../../docs/runbooks/validation.md).
Run the tests-only focused control (all three new tests must fail: the late
packet closes the protocol, the protocol closes during `send_disconnect()`,
and the flush entry is missing), the patched focused modules
`unit.net.protocol_flush_test`, `unit.server.shutdown_flush_test`,
`unit.net.protocol_test` and `unit.server.core_test`, the stack
focused/compiled/no-compat runs, all three full legs and all nine
complete-stack live profiles. There is no packaging change and no native code.
