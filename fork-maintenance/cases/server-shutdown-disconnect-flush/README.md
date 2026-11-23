# Don't close detached protocols on late client packets

Code: the `Fork-Case: server-shutdown-disconnect-flush` commit on `develop`
(`make -C fork-maintenance case-show CASE=server-shutdown-disconnect-flush`).

## Boundary

When the server disconnects a client which is still sending (pointer motion,
draw acknowledgements, a window closing), the client sometimes never receives
the `connection-close` packet: it logs `Connection lost` and exits with
`CONNECTION_LOST` (1) instead of `server requested disconnect: ...` and 0.
`CONNECTION_LOST` is one of the `RETRY_EXIT_CODES` (`xpra/exit_codes.py`), so
with the default `--reconnect=yes` `run_client()` (`xpra/scripts/clients.py`)
re-executes `xpra attach`: a client which has just been detached attaches
again, and after a server shutdown it tries to reconnect to a server which is
gone. Our live profiles run with `--reconnect=no` (`live-cli.yml`), where the
visible symptom is exit status 1. Any transport is affected, and any
disconnect reason: `xpra detach`, server exit (`--exit-with-children`, `xpra
stop`, a signal), a kicked or replaced client.

Sequence: `NetworkServer.disconnect_protocol()` queues the disconnect packet
(`send_disconnect()` → `flush_then_close()`), then `cleanup_protocol()`
removes the client's source, so the protocol is *detached*. A packet which the
client had already sent arrives on the parse thread, `dispatch_packet()` finds
no source and calls `ServerCore.handle_invalid_packet()`, which ended with
`if not ss: proto.close()` and closed the connection under the pending write.

Provenance: our live profiles saw it as a client `Connection lost`/status 1
at server exit in the keyboard profile (the client sent its last packet 3 ms
before the server logged `Disconnecting client`; the server protocol counted
115 received / 88 sent while the client had sent 115 and received 88) and in
detach churn (`xpra detach` while the attached client keeps sending pointer
motion). A server traced with `-d events` showed `connection-close` queued but
not written yet when `Protocol.close()` was called from the parse thread
through `handle_invalid_packet()`. It also reproduces with a `SocketProtocol`
over a socketpair and a client sending a `pointer` packet every 0.5 ms while it
is disconnected: the disconnect was lost in 3 of 2000 runs on upstream
`master`, none with this change.

## Embedded-source context

Source: `d2940c7ddc6edd65aadb00f55ac559045d0c94ed`. The case commit is,
byte for byte, the proposed upstream fix on branch
`upstream/detached-protocol-disconnect` (one commit on this same `master`),
with its message plus the case trailer. When upstream merges it,
`develop-rebase` drops this commit and the case is retired.

The detached classification and the test asserting the close come from
upstream `617ed23485` ("avoid errors for packets from detached clients",
2026-08-27), which only silenced the error log; this change flips that one
assertion. In favour of the change: `6d9054a849`/`26dc1a3920` (2014, #581,
"xpra detach may cause a connection lost rather than a clean disconnection")
removed premature closes for the same symptom.

The previous version of this case also changed `flush_then_close()` (an empty
write queue was taken as "sent" while the write thread still held the packet)
and restored the `sleep(0.1)` flush interval lost by `ServerBase.do_cleanup()`
not calling `super()` (`8b532b2967`). Both were dropped with this
replacement: probes on real unix/TCP sockets always delivered the packet on
the first path (a `send()` blocked in the kernel survives `close()` from
another thread) and showed no loss for realistic exit durations on the second,
so neither failure has been observed outside a fake connection. If a live run
shows a lost disconnect again, those paths are the first candidates.

## Surrounding code and ownership map

| Path / symbol | Role |
| --- | --- |
| `xpra/server/subsystem/network.py` `NetworkServer.disconnect_protocol()` | Queues the disconnect packet and detaches the protocol; owns closing it through `flush_then_close()` (closes after the write, or after its 5 s timeout). |
| `force_disconnect()`, `_process_connection_lost`, ssl/rfb upgrades (`steal_connection`) | The other close owners; unchanged. |
| `xpra/server/core.py` `ServerCore.handle_invalid_packet()` | Logs and, for a protocol without a source, closed it; now skips the close for a `detached` protocol (the existing local variable). |
| `xpra/net/dispatch.py` `PacketDispatcher.dispatch_packet()` | Calls `handle_invalid_packet()` for packets without an unauthenticated handler. |

## Mechanism

`handle_invalid_packet()` already computes `detached` (the protocol was
accepted and is no longer in `_potential_protocols`, so it has been
disconnected). The close becomes `if not ss and not detached:`. A detached
protocol now lives until its disconnect packet is written or the 5 s fallback
of `flush_then_close()` fires; its late packets are dropped with the existing
debug log. Protocols which were never accepted (requests such as `info`,
`detach`, `version` before their hello completes) are still closed. No lock,
timer or state is added.

Known limit: `ProxyServer` has no `client-session` subsystem, so a client
accepted by the proxy is considered detached until `steal_connection()`; an
unexpected packet in that window is now ignored instead of closing the
connection. The proxy has its own close owners (`send_disconnect` on errors,
`steal_connection` on success). The client sends nothing before the server's
hello.

## Case-commit and integration ownership

The commit changes `xpra/server/core.py` (+2/-1) and the one assertion in the
upstream test `unit/server/core_test.py`
`test_handle_invalid_packet_from_detached_protocol`. It has no dependency.
`ssh-proxy-final-delivery` separately keeps the last server packets flowing
through the ssh stdio proxy; together they cover the detach failures seen in
the live suite.

## Commit scope and non-goals

No change to `flush_then_close()`, the write thread, the server cleanup
order, the proxy server or the client. See the Embedded-source context for
the two dropped paths.

## Regression design and clean control

`unit.server.core_test` `test_handle_invalid_packet_from_detached_protocol`
calls the real `handle_invalid_packet()` for a detached protocol and asserts
`proto.close.assert_not_called()`. On clean source it fails with "Expected
'close' to not have been called. Called 1 times."; the rest of the module is
upstream's. The race itself (a write in progress) is not reproduced by the
unit test; the live profiles below exercise it.

## Durable live or package boundary

Every application-exit live profile ends with the server exiting through
`--exit-with-children` while the application's window has just closed (the
client is still sending), and requires the client to exit with status 0. The
detach profile requires both the detach command and the attached client to
exit with status 0 for every TCP and SSH detach, while churn scenarios keep the
client sending. All nine profiles run with the complete stack on both
endpoints.

## Invariants not to simplify

- Only detached protocols are exempt; a protocol which was never accepted is
  still closed.
- The disconnect owner (`flush_then_close()`) remains the only closer of a
  detached protocol.

## Required validation

Run `unit.server.core_test` patched and as the tests-only clean control, the
full legs, and the complete nine-profile live suite.
