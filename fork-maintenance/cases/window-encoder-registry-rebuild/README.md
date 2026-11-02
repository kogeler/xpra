# Serialize window encoder registry rebuilds with worker lookups

Code: the `Fork-Case: window-encoder-registry-rebuild` commit on `develop`
(`make -C fork-maintenance case-show CASE=window-encoder-registry-rebuild`).

## Boundary

A live connection may receive an encoding configuration after frames have
already entered its compression queue. `EncodingServer._process_config()`
reinitializes existing sources on the protocol parse thread; codec-discovery
completion uses the UI scheduler. Either can overlap a captured RGB frame on
the connection's encode worker. Rebuilding the
registry first publishes an empty `_encoders` dictionary; lookup in that gap
raises `BUG: no encoder found for 'rgb32'` and drops the frame. A subsequent
frame can render successfully, so visible output alone does not exclude this
failure. The same transition is used when slow codec discovery finishes.

This case serializes registry rebuilding and worker function lookup. It does
not hold that lock while encoding pixels, create a second compression worker,
or make unsupported encodings available. The demonstrated live trigger was
SSH detach churn on a native-Wayland server; the owning Python registry and
worker route are shared with other display and transport backends.

## Embedded-source context

The embedded source is `646b712d1bff46b08b8f26ddc7a83229d65ba5bc`.
Antoine Martin's `984ebb8a03` ([issue #3382](https://github.com/Xpra-org/xpra/issues/3382))
introduced late encoder reinitialization after slower threaded setup. Its
actual diff reinitializes existing window sources and updates selection after
rebuilding encoders. Current `EncodingServer` still performs
that transition both after threaded discovery and on a client configuration.
The dictionary reset and unsynchronized worker lookup both predate the fork;
this is an additional upstream race exposed by the complete-stack live gate,
not another instance of the missing FakeClient interface.

The decision is to retain reinitialization and protect the shared registry at
its owner. The local maintainer-authored implementation and current callers
were read; the related public discussion could not be reread because anonymous
GitHub access was rate limited. No upstream approval is claimed. Retirement
requires upstream to make registry lookup safe throughout both base and
subclass rebuilding, including terminal clearing, without serializing actual
codec execution under the configuration lock.

## Surrounding code and ownership map

| Owner or caller | Responsibility |
| --- | --- |
| `server/subsystem/encoding.py` | Handles protocol-thread configuration and UI codec-discovery completion; asks each window connection to reinitialize its sources. |
| `server/source/window.py` | Creates and initializes window sources before admitting damage; `reinit_encoders()` revisits active ordinary and internal sources. |
| `server/window/compress.py:WindowSource` | Owns encoder registries, registry lock, packet construction and the worker's terminal `encode_ended()` barrier. |
| `server/window/video_compress.py:WindowVideoSource` | Extends the registry with video, auto, stream and scroll functions; preserves existing video-context cleanup before rebuilding. |
| `server/window/subsurface_source.py:SubsurfaceWindowSource` | Extends base initialization for the raw picture-only child policy, inheriting the same registry lock. |
| `server/source/client_connection.py` | Owns FIFO encode work and its terminal sentinel; this case does not change queue admission or resource cleanup ownership. |

The separate timer lifecycle and video-pipeline cases retain their terminal
admission, callback and native-resource guarantees. Registry serialization is
not a substitute for those boundaries.

## Mechanism and lifecycle

`WindowSource.init_vars()` allocates one `_encoder_lock` before the source is
published. Because cleanup also calls `init_vars()`, its existence guard keeps
the same lock rather than replacing a lock retained by another thread.

`init_encoders()` holds that lock only across the dynamic
`self.do_init_encoders()` call. This includes the complete subclass extension,
not just the base RGB population. The configuration caller releases it before CSC parsing
and selection updates. Existing encoder priority and discovery remain intact.

The encode worker holds the same lock only while obtaining the callable in
`make_data_packet()` or the picture fallback in `WindowVideoSource.do_scroll()`.
It releases the lock before invoking the callable. A queued frame therefore
waits for a registry rebuild to finish instead of observing a partially empty
map. Slow native encoding cannot itself hold up another registry rebuild.
Terminal `encode_ended()` clears the map under the same lock and posts UI
cleanup after releasing it; ordinary FIFO and cancellation checks still own
whether a terminal frame is allowed to run.

The registry lock is distinct from timer, image, codec and video lifecycle
locks. `WindowVideoSource.video_context_clean()` runs before the base registry
critical section; registry construction does not execute pixel encoders or
wait for the encode worker. Neither lookup site executes codec callbacks
while holding the lock. This avoids introducing a registry/video lock inversion
or a configuration wait on an encoder that is itself waiting for registry access.

Initialization exceptions still propagate and release the lock. This patch
does not promise rollback to a previous registry after a discovery exception,
retain removed encodings, replay rejected frames or alter codec failure policy.
It protects complete successful rebuilds, and leaves existing cancellation,
missing-encoder diagnostics and picture/video fallback behavior meaningful.

## Case-commit and integration ownership

The case changes the two real lookup sites, the base rebuild and terminal
clear, and the lock's persistent initialization. The new focused module uses
real source constructors and the native RGB encoder. The existing
`VideoPipelineLifecycleTest` synthetic resource factory gains the corresponding
private lock field because it deliberately bypasses the owning constructor.
That fixture adaptation belongs to its owning `video-pipeline-cleanup-race`
commit; this case does not edit the dependency's tests. It is an explicit
fixture contract, not an optional production lookup, and preserves independent
removal of each case's diff.

The dependency on `window-source-timer-lifecycle` binds the persistent source
lifecycle and its terminal barrier, including the shared `Lock` import.
The dependency on `video-pipeline-cleanup-race` supplies that initialized
fixture and its pipeline reinitialization regressions. The production
registry mechanism does not replace the dependency's native-resource fences.
WSSO remains separately owned: its raw sources inherit base initialization;
the complete-stack focused and native gates retain its composition coverage.
No application, driver or transport-specific exception is added.

## Commit scope and non-goals

Registry configuration and callable acquisition are the atomic behavior here.
Negotiation, CSC capability retirement, codec choice, queue order, wire data,
security filters and package ownership keep their current contracts. This does
not claim a general snapshot of every mutable encoding policy attribute.

Suppressing the missing-encoder error or retrying/polling would hide a dropped
frame. Keeping stale dictionary keys would preserve encodings that were meant
to be removed. A base-only dictionary swap would not cover subclass auto/stream
registration. Locking around the whole encode operation would couple
configuration to native pixel execution. The short owner lock handles the
demonstrated race without changing those policies or extension interfaces.

## Regression design and clean control

`unit.server.window.encoder_registry_test` pauses real codec discovery inside
reinitialization after the registry reset, while a second thread constructs an
actual RGB32 draw packet. It covers both `WindowSource` and the video subclass.
Both threads must finish and produce a nonempty real RGB packet without an
exception; the oracle also permits a consistent registry snapshot. The
tests-only clean control reaches the old lookup gap
and fails behaviorally, rather than depending on a new API or private field.
A second regression holds actual RGB encoder execution and proves that another
rebuild can finish, rejecting an overly broad lock around codec execution.

The source model supplies ordinary property values; the source constructors,
registry construction, image wrapper, packet path and native RGB encoder are
real. Synchronization controls the interleaving, not the subject's outcome.
The test does not exhaust every configuration change, codec backend, or native
window lifecycle. Existing compression, video cleanup and full-stack native
regressions cover adjacent paths; physical profiles remain mandatory.

## Durable live or package boundary

`live-xpra-detach` includes repeated SSH detach, concurrent pointer/window
churn and retained complete server logs. Its RGB encoder boundary rejects
`failed to create data packet` even when later frames render successfully.
This observed negative demonstrates why a successful screenshot cannot waive
the encoder assertion. No warning ignore or oracle relaxation is introduced.
All nine positive live profiles must ultimately pass on both endpoints with
the complete current stack and the exact newly validated Ubuntu server and
Debian client DEBs. Existing package imports and pixel checks complement that
runtime proof; no installed-source build hook is added.

## Invariants not to simplify

- Preserve one registry lock across reusable and terminal state reset.
- Protect the entire dynamic rebuild, including subclass registrations.
- Release the lock before selection updates and pixel encoder invocation.
- Keep both ordinary packet and scroll-fallback lookups protected.
- Keep codec discovery, unavailable-mode diagnostics and terminal admission real.

## Required validation

Run the behavioral tests-only clean control, composed focused tests including
compression and video-pipeline lifecycle coverage, the native Wayland gate,
all three full upstream legs and the complete offline controls. Validate both
real DEB builds and finish the live fix-and-continue loop from the failed
detach gate, then obtain one complete nine-profile pass without a fix. Runtime
results and current package identities belong in the ignored session ledger.
