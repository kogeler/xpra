# Client codec startup ordering

## Boundary

An explicit GUI-client encoding is validated against the client's available
codec inventory. The encoding subsystem delegates initialization to the decode
worker when that subsystem exists, then waits for its completion event. At the
embedded source, option validation runs immediately after `app.init()`, before
UI loading and before `app.run()` starts that worker.

For a fresh client with a decode subsystem and an unloaded inventory, this
orders a wait before the operation which can satisfy it. The default
`XPRA_CODEC_LOAD_TIMEOUT` is 30 seconds. On expiry the client warns:

```text
Warning: timed out waiting for the decode thread to load the codecs
```

It then invokes the existing caller-thread fallback. The eventual session may
work, but the startup delay and wrong-thread initialization have already
occurred. This particular failure is not evidence that a decoder, GPU or
remote server took 30 seconds to initialize. Empty or automatic encoding
selection bypasses that early validation path.

This case completes UI/load initialization before validating an explicit
encoding, starts the actual codec owner before waiting, and makes subsequent
decode `run()` calls reuse that worker. Codec discovery, security policy and
the timeout for a genuinely stalled initializer remain unchanged.

## Embedded-source context

The case resolves against source commit
`212038243d0067b6860ebe7d6953692179ef353f`, embedded in current `develop`.
Upstream commit `2638fb3762b` generalized the former draw thread into a shared
`decode` subsystem. Picture, icon and cursor consumers can now post work to
one worker, whose preload phase precedes the thread-local seccomp filter.

`Encodings.has_decode_thread()` tests whether the subsystem is present; it
does not inspect whether `Decode.run()` has executed or a worker is alive.
`Encodings.load()` already avoids waiting during early loading when such an
owner exists. The GUI option-validation call site must honor that same
lifecycle distinction.

The retained loader lock and completion event prevent repeated initialization;
they cannot start an absent worker. Similarly, a later successful handshake
cannot undo an earlier timeout fallback. The repair belongs to GUI bootstrap
and decode-worker ownership, not to decoder enumeration or an encoding-specific
loader override.

On an upstream refresh, inspect the bootstrap, subsystem run order, preload
hooks and security boundary together. An equivalent replacement must load on
the intended owner after consumers are initialized, avoid duplicate workers,
preserve early-option cleanup and leave real timeout diagnostics intact. A
shorter wait or a codec preload on the main thread is not equivalent.

## Surrounding code and ownership map

| Component | Responsibility |
| --- | --- |
| `xpra/scripts/main.py` | Creates the GUI client, orders init/UI/load, validates an explicit encoding and cleans up failed or informational startup exits. |
| `xpra/client/base/features.py` | Supplies the encoding feature gate consulted before option validation touches a subsystem. |
| `xpra/client/base/stub.py` | Defines `preload_decode()` and routes consumer work to the decode subsystem, or inline when none exists. |
| `xpra/client/subsystem/decode.py` | Owns the single work queue, worker identity, preload order, thread-local seccomp installation and exit marker. |
| `xpra/client/subsystem/encoding.py` | Owns allowed encodings, codec initialization lock/event, inventory queries and compatibility-mode capability delivery. |
| `xpra/codecs/loader.py` and the video helper | Own codec import, availability and initialization, including the existing decoder and CSC selection policies. |
| Client window, icon, cursor and notification subsystems | Initialize their own state, preload imports needed by queued work and retain their UI-delivery responsibilities. |
| `xpra/seccomp/` | Owns platform/enablement checks and the decode thread's syscall filter. |

The defective dependency and corrected startup are:

```text
embedded source:
  init -> validate encoding -> wait for codecs
                               worker has not been started
                               timeout -> caller-thread fallback
       -> init_ui -> load -> later app.run starts decode

patched bootstrap:
  init -> init_ui -> load -> validate explicit encoding
       -> Decode.run(): publish and start the one worker
       -> ensure_codecs_loaded(): wait for its inventory
       -> continue GUI bootstrap

worker:
  load_all_codecs -> publish codec event -> consumer preload hooks
       -> optional malloc prewarm -> Linux seccomp installation
       -> consume queued work

later app.run:
  Decode.run() sees the published worker and does not start another
```

The main thread's inventory wait can finish before consumer preloads and
seccomp installation finish. The codec event is not an all-worker-ready
barrier. Queued work remains ordered behind those steps by the worker itself.

## GUI bootstrap and option admission

`get_client_gui_app()` still constructs the same client and calls `init()`
first. It registers the existing handshake-progress callback, completes
`init_ui(opts)` and `load()`, then calls `handle_client_encoding_option()`.
Request-mode setup and listen-mode handling remain after that validation.
Starting the application or its network main loop is not needed to validate
the encoding.

The relocation matters beyond avoiding a wait. The decode worker calls
consumer `preload_decode()` hooks. Starting it immediately after `init()`
would expose consumers whose UI/load state is not ready. The new call site
finishes both phases before giving the worker permission to preload.

The option handler preserves its existing early returns:

| Input or client capability | Validation behavior |
| --- | --- |
| Empty encoding or `auto` | Return the automatic-selection value without starting decode from this path. |
| Encoding feature disabled | Return without a codec wait or early worker start. |
| No encoding subsystem | Return without starting decode merely for validation. |
| Explicit encoding and both subsystems present | Start/reuse decode, then wait for the real encoding inventory. |
| Explicit encoding without decode | Use the existing inline loader; there is no worker to wait for. |
| `help` or unsupported explicit encoding | Build the normal supported-encoding information, raise `InitInfo` and run the bootstrap exception cleanup. |

Validation still uses `get_encodings()` plus the existing `auto` and `stream`
entries. It does not add an encoding, bypass the allowlist, force a decoder
or infer availability from the requested string alone. A previously loaded
inventory retains its existing event-based fast path.

The early worker start occurs only after the encoding feature/subsystem checks.
No-decode clients and standalone subsystem use remain supported. Ordinary
automatic startup still creates its worker during the application's normal
run phase rather than eagerly starting it for every GUI client.

## Single-worker identity and terminal lifetime

`Decode.run()` now creates a worker only when `_thread is None`. It uses the
existing `make_thread()` helper, stores the returned object, then calls
`start()`. The queue remains the same `SimpleQueue`, the worker keeps its
`decode` name and ordinary non-daemon behavior, and `run()` returns the same
successful exit value.

Publication before `start()` makes the owner visible before it can execute
preload or finish. Later application startup reaches the same object instead
of allocating another consumer for the queue. The guard is for serialized
client lifecycle calls; it is not a new arbitrary-concurrent-call API and does
not introduce a cross-thread startup lock.

The loop no longer clears `_thread` on exit:

| Worker state | Meaning of a later `Decode.run()` |
| --- | --- |
| Never started; `_thread is None` | Publish and start the initial worker. |
| Published and preloading or consuming work | Reuse its identity; do not create another queue consumer. |
| Previously started worker has finished | Preserve its identity; do not turn a terminal subsystem into a restartable worker. |

Checking only `is_alive()` would be insufficient: it would admit a replacement
after the existing worker ended. Clearing the reference at loop exit would
likewise reopen the startup guard and lose the identity used for teardown.
The patch does not provide retry/recovery after thread-start failure or a
worker crash; that would require separate lifecycle policy.

`cleanup()` retains its existing queue exit marker and bounded join of a live
worker. It does not forcibly terminate a thread or guarantee completion of a
genuinely stuck codec load. GUI help and invalid-option paths invoke the same
cleanup even though the application run phase has not begun. The regression
checks that an ordinarily progressing early worker exits and is not restarted.

Consumers still enqueue `(callable, args)` work through the common subsystem
interface. FIFO dispatch, the per-item error boundary, decode counters and
the client's exit-code check remain unchanged. This case does not move GUI
updates onto the worker or split icons and frames across separate queues.

## Codec completion and security ordering

`Encodings.load_all_codecs()` remains the lock-protected initializer. It
checks `_codecs_loaded`, performs normal initialization once, then sets the
event. Consumers call `ensure_codecs_loaded()` rather than competing to load
codecs while the decode worker has responsibility for them.

The worker's unchanged preload sequence is:

1. Initialize encoding codecs on the decode thread before it is filtered.
2. Invoke every other subsystem's `preload_decode()` hook on that thread.
3. Perform the existing malloc-arena prewarm when seccomp is enabled.
4. On Linux, enter the existing seccomp installation path.
5. Begin consuming queued data.

Codec imports, dynamic-library loading and self-tests can require file access.
Consumer imports must likewise happen before filtered work. Moving a codec
load to the main thread or installing seccomp first would cross that security
ordering even if the startup warning disappeared.

This patch does not add imports to bypass the filter or change the filter's
syscalls, action, platform gates or enablement. It leaves the existing hardware
decoder exclusions and JPEG restrictions under seccomp in the encoding
subsystem. Codec allowlists and CSC-module selection retain their distinct
roles; enabling one is not a workaround for discovery of the other.

`CODEC_LOAD_TIMEOUT`, its warning and its last-resort fallback remain intact
for a worker which actually fails to publish the inventory. The case removes
the deterministic wait-before-start cause; it does not promise that every
future occurrence of the warning has the same cause or that a slow native
initializer can never hit the deadline.

Compatibility mode also retains its existing owner. With compatibility enabled,
hello capability construction waits for codec availability. Without it,
`Encodings.run()` schedules the existing encoding-configuration work and
post-handshake delivery. The early option-validation fix does not rewrite
either packet format or use one mode as a substitute for testing the other.

## Patch-queue and integration ownership

`fix.patch` changes `xpra/scripts/main.py` and
`xpra/client/subsystem/decode.py`, and adds
`tests/unittests/unit/client/subsystem/codec_startup_test.py`. No codec,
seccomp or encoding-loader implementation is changed. The manifest has no
case dependencies and the clean-source control does not need another patch
to reach this startup defect.

[`video-pipeline-cleanup-race`](../video-pipeline-cleanup-race/README.md)
owns server-side video context lifetime, not this client's bootstrap worker.
[`wayland-display-name-signal`](../wayland-display-name-signal/README.md)
owns a server subsystem declaration. The clipboard cases own selection
events and requests. A shared session or warning oracle does not turn these
into one production behavior.

The complete queue still needs composed validation: real clients start this
worker while using the queue's window, clipboard and rendering behavior.
The shared live harness owns full-log rejection of startup warnings and its
VA-API trace collection. Trace-file retention is test-environment evidence
handling, not an additional codec-loading change in this patch.

## Patch ownership and non-goals

The case does not:

- reduce or extend codec wait timeouts, suppress warnings or remove fallback;
- preload codecs on the main thread when a decode owner exists;
- start the application main loop or connect to a server to validate an option;
- alter encoding, decoder or CSC discovery, hardware selection or self-tests;
- change seccomp policy, install the decode filter on the main thread or add
  imports solely to evade filtered file access;
- make the decode subsystem restartable or add concurrent-start synchronization;
- change decode FIFO ordering, UI callback ownership or server video cleanup;
- guarantee shutdown of a native initializer which is genuinely stalled.

## Focused regression design

`unit.client.subsystem.codec_startup_test` enters through the real
`get_client_gui_app()` and option handler. `BootstrapClient` is a small UI
shell recording lifecycle phases; it does not connect to a server or create
a real toolkit display. Its encoding/decode instances inherit the actual
subsystems, use their real loader lock/event and queue, and run real threads.

For the ordinary controls, an observed codec body records its caller and the
test encoding adapter supplies a deterministic inventory. Native codec loading
and seccomp are not needed to isolate the bootstrap-order assertions. The
observer records both thread identity and UI/load readiness, so merely
returning quickly cannot satisfy the control.

The seven tests cover:

| Test group | Required behavior |
| --- | --- |
| Explicit encoding | Exactly one codec load on the decode worker after UI/load, followed by consumer preload and the filter entry; no startup warning. |
| Later run and terminal reuse | Normal run does not start a second worker; repeated run keeps the same object; after cleanup it remains stopped and is not recreated. |
| Help and invalid option | Both produce the existing `InitInfo` content and leave no live early worker. |
| Automatic encoding | Both empty and `auto` avoid early start, then load correctly when normal run begins. |
| No decode subsystem | Load once inline without a worker or timeout warning. |
| Disabled or absent encoding | The option handler does not start decode. |
| Native child | Actual compiled WebP inventory and a real thread-local seccomp filter satisfy the same ordering and isolation assertions. |

Queue barriers are real work items backed by threading events. They are needed
because returning from the inventory wait alone does not prove completion of
the later consumer-preload and filter stages. The recorded order must be
codec load, consumer preload, filter entry, all on the same observed worker.

The native child uses `XPRA_SECCOMP=1` and
`XPRA_SECCOMP_DECODE_ACTION=errno`. It requires the actual `.so` WebP decoder
to load and advertise `webp`; it does not substitute a fake codec. Queued work
attempts to open the test file and must receive `EPERM`, while the child main
thread must still open that file successfully. The child boundary prevents
its filter from affecting the outer test process. Missing seccomp or the
native decoder is a failure, not a skip.

This native control proves initialization and filter placement, not decoded
pixel correctness or hardware H.264 presentation. Those remain distinct
upstream/live assertions. The ordinary controls use a 0.2-second codec wait;
the native child uses five seconds and a bounded process timeout. These are
test-local values, never changes to the production default.

The tests-only clean control must expose the pre-start wait/caller-thread load
or repeated worker start through existing APIs. Missing a new helper or
failing before bootstrap is not an acceptable negative result. Test teardown
retains every observed worker, supplies sufficient exit markers even when
clean source created duplicates, and joins them; a failing assertion must not
leave a non-daemon test thread running.

The manifest retains these adjacent modules:

- `unit.client.subsystem.decode_test`: FIFO work, error survival, cleanup and
  shared consumer routing, including standalone inline use;
- `unit.client.subsystem.encodings_test`: the existing encoding subsystem;
- `unit.client.subsystem.seccomp_encoding_test` and `unit.seccomp_test`:
  security configuration and codec-policy controls;
- `unit.scripts.main_test` and `unit.scripts.args_test`: existing CLI behavior.

They supplement the actual bootstrap regression rather than replacing it with
isolated method calls. Compiled and no-compat runs must exercise the same
ownership and cleanup boundary.

## Durable live boundary

All live profiles carry the complete current queue on both endpoints. The
manifest names `live-rgb`, `live-h264`, `live-wayland-h264-hardware` and
`live-wayland-opengl-h264-hardware` as topical startup/rendering owners. Those
entries do not waive the other five profiles or authorize case-only products.

The real RGB and H.264 sessions exercise GUI creation with the tracked client
options and require actual remote rendering, input and application lifecycle.
The two hardware profiles additionally require their existing VA-API
encode/decode, complete packet/context, GPU presentation and pixel proofs.
Successful decoder import or a warning-free log cannot replace those claims.

The suite checks complete report-bound stdout/stderr of both peers in every
scenario after each collected member and again in `live-suite-check`. The
codec startup warning is rejected even if the application subsequently works
through fallback. Missing, changed or truncated evidence is not a clean log.
No timeout, rendering, input or cleanup assertion is relaxed for this case.

The focused child proves the seccomp boundary directly; a working live GPU
session is not by itself evidence that file access is restricted on precisely
the decode worker. Conversely, the child does not reproduce a full GUI or
network lifecycle. Both proof levels are retained.

## Invariants not to simplify

- Complete `init_ui()` and `load()` before starting an early decode owner.
- Preserve automatic/disabled/missing-subsystem early returns.
- Start the owner before waiting for its codec inventory.
- Publish the thread object before starting it and keep its terminal identity.
- Never add a second consumer merely because normal application run follows
  option validation or a previous worker has finished.
- Keep codecs and consumer preloads before the thread-local filter and queued
  untrusted work; do not reinterpret the codec event as an all-preload barrier.
- Preserve early help/error cleanup and existing bounded shutdown behavior.
- Keep real codec timeout/fallback, allowlists, hardware restrictions and both
  compatibility paths unchanged.
- Require real worker identity and native filter controls, plus full live
  application and log evidence; elapsed startup time alone is insufficient.

## Required validation

Follow [development and final acceptance](../../docs/runbooks/validation.md).
After a behavior change, run the same-image tests-only clean control and all
manifest focused modules. Exercise `focused-cython` and `focused-no-compat`,
including the native WebP/seccomp child, and the affected composed stack
inventory. Import-only checks and mocked worker calls cannot prove the owner.

Start the complete live suite after focused prerequisites:

```bash
make -C fork-maintenance live-all STACK=develop RUN=<fresh-cycle-prefix>
make -C fork-maintenance live-suite-check STACK=develop RUN=<fresh-cycle-prefix>
```

At candidate freeze, fill missing or invalidated controls, composed/native,
quarantine, fork-control and `full`, `full-cython`, `full-no-compat` gates.
Require all nine current positive live profiles. Follow the canonical
scheduling and equivalence rules instead of repeating unchanged expensive jobs
after each intermediate edit.

Package/build validation follows the enclosing contract; this case changes no
package layout or native ABI. The
[scoped mypy gate](../../docs/runbooks/typecheck.md) does not currently include
these client/bootstrap modules or their regression. Do not claim it as their
type coverage. Keep named results, toolchain/input identities and transient
diagnosis in the ignored cycle ledger, not this architectural README.
