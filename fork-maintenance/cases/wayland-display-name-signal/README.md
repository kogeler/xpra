# Native Wayland display-name signal declaration

## Boundary

Native Wayland startup uses the manager's `display-name` signal to publish the
socket name selected by the compositor. The session-files subsystem subscribes
before socket allocation, then uses the notification to reconcile the session
directory and daemon log with that actual display name.

At the embedded source, `WaylandManager` connects and emits this signal without
declaring it. The inherited `SignalEmitter` verifies both operations and warns:

```text
Warning: 'display-name' is not a declared signal of WaylandManager
```

The verifier is diagnostic: it neither rejects the connection nor suppresses
the callback. A working session can therefore contain the warning at listener
registration and again at emission. This is not proof that socket allocation,
session-directory migration or window mapping failed.

This case declares the existing signal on its actual producer. It does not
remove verification, lower the log level or change when display names become
available. Unknown signals must remain visible as programming errors.

## Embedded-source context

The case resolves against source commit
`d95058b0916913fe6ae5296fb702f66d833898b0`, embedded in current `develop`.
Upstream commit `1f5f73ee619` separated the native Wayland manager from the
server implementation. The manager already contains the socket-binding and
`display-name` emission path; the missing part is its declaration.

Signal declaration checking was introduced in `28ab373efe0`. Its current
`_verify_signal()` explicitly consults the concrete emitter's `__signals__`.
The neighboring `XvfbManager` already declares `["display-name"]` for the same
session-files consumer. Neither the checker nor the consumer needs a Wayland
exception.

Current-code reassessment retains the patch unchanged. The manager, generic
emitter and session-files startup wiring still have the same omission and
contract; no upstream replacement declares the signal. Callback scheduling,
socket and backend lifetimes remain owned by their existing implementations.
The narrow declaration does not attempt rollback of a failed consumer's
filesystem operations or make a cleaned manager reusable.

On an upstream refresh, inspect the current manager, emitter and startup
wiring together. An equivalent replacement must declare the producer's real
signal while preserving callback delivery and diagnostics for unknown names.
Removing the call, weakening generic verification or renaming only one side
is not an equivalent repair, even if the original warning disappears.

## Surrounding code and ownership map

| Component | Responsibility |
| --- | --- |
| `xpra/scripts/server.py` | Orders subsystem construction, listener registration, privilege transition, socket binding and subsequent server initialization. |
| `xpra/wayland/server/subsystem/manager.py` | Owns the compositor, selected socket name, display notification and one-time backend start. |
| `xpra/server/subsystem/stub.py` | Supplies the subsystem's `SignalEmitter` state and access to its server's main loop. |
| `xpra/util/signal_emitter.py` | Verifies declared names, stores callbacks and chooses direct or GLib-idle delivery. |
| `xpra/server/subsystem/sessionfiles.py` | Receives the actual name, updates session-directory state and notifies the daemon subsystem. |
| `xpra/server/subsystem/daemon.py` | Owns daemon log-path changes and associated environment values. |
| `xpra/wayland/server/seamless.py` | Starts the backend after display/window listeners exist and owns the GLib source dispatching compositor events. |
| `xpra/wayland/server/compositor.pyx` | Allocates the actual native socket and owns native compositor resources. |

The relevant startup sequence remains:

```text
do_run_server()
  -> initialize session files and daemon log state
  -> add WaylandManager; init() constructs compositor
  -> connect("display-name", session_files.display_name_changed)
  -> process.setup(): reach the target user's privilege state
  -> manager.setup_display()
       -> bind_display(): allocate socket, publish WAYLAND_DISPLAY
       -> emit("display-name", socket_name)
            -> session-files callback
                 -> reconcile session directory
                 -> notify daemon about directory/display name
       -> return VFBStartResult
  -> init_subsystems(): attach display/window listeners
  -> WaylandSeamlessServer.setup(): start_display()
```

The signal is a Python subsystem interface. It is not a Wayland wire event,
a wlroots listener name, a clipboard offer or window metadata sent to the
remote client. Those interfaces have separate owners and validation.

## Signal declaration and callback semantics

`WaylandManager` inherits `SignalEmitter` through `StubSubsystem`. Its signal
vocabulary therefore belongs in `__signals__`, not a GObject `__gsignals__`
dictionary on the server or compositor. The production patch adds only:

```python
__signals__ = ["display-name"]
```

The declaration changes the verification result for that exact name. It does
not add callback state, instance slots, a new signal implementation or a new
scheduler. Existing `connect()` still registers the listener; existing
`emit()` still invokes it.

With no extra arguments supplied at registration, the session-files callback
receives `(manager, socket_name)`. The producer is passed by the emitter; it
is not a second display name or a native compositor pointer. The consumer's
historical `_xvfb` parameter name does not restrict it to an X11 manager.

The emitter calls directly when there is no running main loop or the caller
owns that loop's context. Otherwise it uses the existing GLib idle dispatch.
Startup normally reaches this notification before the server loop runs. This
case neither forces synchronous delivery in other contexts nor adds another
idle hop. Callback exception reporting remains in the generic emitter.

Both `connect()` and `emit()` verify names independently. Declaring the signal
only at a call site, adding a warning filter around startup or changing the
generic emitter's default vocabulary would leave ownership incorrect. The
concrete producer must describe the interface it actually provides.

## Socket, notification and backend lifetime

The existing manager state has distinct meanings:

| State | Owner and lifetime |
| --- | --- |
| `compositor` | Constructed by `init()` if absent; released and cleared by manager cleanup. |
| `socket_name` | Cached name from the first successful `add_socket()`; reused by later binding calls on that manager. |
| `displayfd` | Configured startup value, converted back to an integer in `VFBStartResult`; no new descriptor is opened by this patch. |
| `started` | Records successful backend start, not socket existence or signal delivery. |

Socket creation must remain after the startup privilege transition because it
uses the target user's runtime directory. Merely constructing the compositor
in `init()` must not bind the socket early. The fix does not move either step.

`bind_display()` caches the allocated name and sets `WAYLAND_DISPLAY` before
`setup_display()` emits it. A listener can therefore observe the actual bound
socket and the matching environment value while `started` is still false.
The returned `VFBStartResult` describes this socket-binding phase; it is not
evidence that the backend has started or that an application has mapped.

Binding idempotence is not notification deduplication. A repeated
`setup_display()` reuses the same socket and result, but still emits
`display-name` again. The regression requires the second callback as well as
the unchanged socket inode. Replacing this with an emit-once policy would be
a new behavior outside the declaration repair.

The later `start_display()` remains independently idempotent. It starts the
backend only after the display/window subsystems have attached their listeners,
so the initial output notification is not lost. This case does not start the
backend from a signal callback or move that ordering into socket allocation.

Manager cleanup clears its compositor reference before invoking native
cleanup. The seamless server separately removes its compositor event source
before subsystem teardown. The patch adds no timer, FD, receiver or new cleanup
duty. It also does not turn a cleaned manager into a reusable startup object:
cached socket/backend fields retain their existing terminal-lifecycle rules.

## Allocation failure and diagnostic limits

If `add_socket()` raises before returning a name, setup must propagate that
failure without emitting a successful `display-name`. The cached name remains
empty, `WAYLAND_DISPLAY` remains as it was, and the backend remains unstarted.
The failure control injects precisely that allocator exception; it is not a
general test of every possible partial native-construction failure.

Conversely, a declared signal does not guarantee that downstream filesystem
operations succeed. `SessionFilesServer.display_name_changed()` still owns
directory migration and its error reporting, then passes display/log changes
to the daemon subsystem. This patch does not bypass permissions, choose a new
session-directory policy or hide errors from those consumers.

The warning also says nothing about `pixel-format` or another window-model
property. A signal declaration and a metadata property lookup are different
interfaces even when both failures mention a Wayland object. Successful socket
binding, signal verification, application mapping and remote rendering must
not be used as substitutes for one another's evidence.

## Patch-queue and integration ownership

`fix.patch` changes only `xpra/wayland/server/subsystem/manager.py` in
production and adds
`tests/unittests/unit/wayland/manager_signal_test.py`. The manifest has no case
dependencies; the omission and its regression are meaningful on the embedded
source independently of the rest of the queue.

[`wayland-initial-window-state`](../wayland-initial-window-state/README.md)
owns window creation and initial metadata, not manager signal declarations.
[`x11-client-clipboard-events`](../x11-client-clipboard-events/README.md)
owns its clipboard event/transfer and native-display integration boundaries.
Neither case needs to absorb this manager interface fix merely because its
live sessions also start a compositor.

The sibling
[`client-codec-startup-order`](../client-codec-startup-order/README.md) and
[`x11-selection-refusal`](../x11-selection-refusal/README.md) cases own other
warnings at different client-side boundaries. The shared live-suite log check
observes all of them; that common oracle does not merge their production
ownership or create manifest dependencies.

## Patch ownership and non-goals

The case does not:

- disable generic unknown-signal verification or change its severity;
- rename a signal, add a wire capability or change callback arguments;
- convert the manager into a GObject or modify server signal registration;
- change socket-name selection, privilege dropping or runtime-directory policy;
- deduplicate repeated setup notifications or restart a cleaned manager;
- alter backend startup, output listener ordering or compositor event dispatch;
- fix session-file permissions, daemon log migration, window metadata,
  clipboard traffic or decoder initialization.

## Focused regression design

`unit.wayland.manager_signal_test` uses the real manager and inherited signal
emitter. The success path constructs the actual compiled `WaylandCompositor`
with a private `XDG_RUNTIME_DIR`, `XPRA_WAYLAND_GPU=no` and `WLR_RENDERER=pixman`.
No stand-in compositor or emitter supplies the successful notification.
Missing subject extensions fail rather than skip the test.

The three tests cover distinct claims:

| Control | Required observation |
| --- | --- |
| Successful native socket binding | The returned path is a real socket; callback producer/name/environment agree; progress identifies binding; `displayfd` remains zero and the backend is not started. |
| Repeated setup and cleanup, within the success test | Same result and socket inode, exactly one additional callback, no declaration warning, double cleanup leaves no compositor or socket. |
| Failed allocation | Injected `OSError` propagates, the allocator is called once, no display callback occurs and name/environment/backend state remain unchanged. |
| Unknown signal | Emitting `display-name-typo` still produces the exact generic declaration warning. |

The allocation-failure path alone substitutes `add_socket()` so it can fail
deterministically before native allocation. The logger is observed with a
wrapping mock: the test asserts warning behavior without replacing the signal
mechanism. The typo control prevents a blanket warning suppression from making
the positive assertions pass.

The focused inventory also retains `unit.util.signal_emitter_test`, which
covers main-loop ownership decisions and stub delegation, and
`unit.server.subsystem.stub_mixin_test`, which checks the server subsystem
interface. They complement the manager test; they do not provide a mapped
native application or exercise a real session-directory rename.

The `PATCH_MODE=tests-only` clean control must reach native socket allocation
and successful callback delivery, then fail because the real signal still
warns. The injected-failure test likewise sees the undeclared connection. The
unknown-signal control remains valid on clean source. An import error, missing
socket or unrelated construction failure is not the intended negative result.

## Durable live boundary

Every live profile runs the entire current `stacks/develop` queue on both
endpoints. The manager participates in actual native-Wayland server startup,
and `live-rgb` names this case's ordinary startup ownership in `case.toml`.
That entry is not permission to run or accept a case-only endpoint or one
profile instead of the mandatory nine-profile suite.

The shared suite checks the complete report-bound stdout and stderr of both
peers in every scenario. It rejects the undeclared `display-name` warning
after each collected member and again in `live-suite-check`. Missing, changed
or incomplete evidence cannot be interpreted as a warning-free startup. The
other codec/clipboard warning checks remain independent.

Actual connection, application rendering, input, lifecycle and owned cleanup
must also pass their existing profile assertions. A socket-only focused test
does not establish remote application behavior, while successful pixels do
not excuse a broken manager declaration. The focused test's software renderer
does not claim hardware renderer or codec coverage; the complete suite retains
its separate physical hardware profiles.

## Invariants not to simplify

- Declare `display-name` on `WaylandManager` itself using `__signals__`.
- Keep verification on both registration and emission, including unknown names.
- Preserve the producer/name callback contract and existing dispatch policy.
- Bind the socket after the privilege transition and publish its name before
  notifying consumers.
- Distinguish socket-binding idempotence from repeated signal delivery.
- Start the backend only after display/window listeners have been installed.
- Do not emit a successful name when allocation fails.
- Keep compositor cleanup separate from session-files and event-source duties.
- Require both the real native clean control and full-log live checks; absence
  of a warning alone does not establish a functioning session.

## Required validation

Follow [development and final acceptance](../../docs/runbooks/validation.md).
During an upstream refresh, finish and record the incremental manual review
and resulting-stack exit gate before starting runtime validation. Then run
the tests-only clean control and the manifest's
patched focused inventory against the same frozen source/image. Include the
native `wayland` gate, compiled/no-compat focused modes and the composed
complete-stack inventory. A Python-only declaration inspection cannot replace
the real compositor/socket regression.

Start the complete live suite once focused prerequisites are satisfied:

```bash
make -C fork-maintenance live-all STACK=develop RUN=<fresh-cycle-prefix>
make -C fork-maintenance live-suite-check STACK=develop RUN=<fresh-cycle-prefix>
```

At candidate freeze, fill missing or invalidated controls, native, quarantine,
fork-control and all three full upstream legs. Require all nine positive live
profiles with matching current source, queue and harness. Use the canonical
scheduling and reuse rules rather than repeating unchanged expensive checks
after every edit.

Package/build obligations follow the enclosing contract; this declaration
introduces no packaging rule or native ABI. The
[scoped mypy gate](../../docs/runbooks/typecheck.md) does not currently own
this manager or its regression, so it cannot be reported as their type
coverage. Keep named runs, exact input/result identities and transient
investigation notes in the ignored cycle ledger, not this architectural README.
