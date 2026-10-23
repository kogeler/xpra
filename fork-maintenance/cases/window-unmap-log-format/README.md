# Correct the base window-unmap log format

Code: the `Fork-Case: window-unmap-log-format` commit on `develop`
(`make -C fork-maintenance case-show CASE=window-unmap-log-format`).

## Boundary

An accepted `window-unmap` packet reaches the base `WindowServer` handler on
a non-readonly connection. It logs two arguments using a format with one
placeholder, causing a logging `TypeError` instead of the intended diagnostic.
The Wayland subsystem inherits this handler; X11 seamless overrides it and does
not use this particular diagnostic. Readonly requests return before logging.

## Embedded-source context

The maintainer's packet-prefix migration `f1cd65e488e` introduced the malformed
format while renaming the previous correctly formatted unmap handler. The
later subsystem-prefix change `8cd0f9cf3d` renamed the method again without
repairing that string. Current neighboring map/close stubs use
`log.info("_process_<name>(%s, %s)", proto, packet)`.

Restore precisely that current convention rather than combining a typo fix
with a logging-level policy change or new Wayland window semantics. The
upstream [review's separation requirement][scope-review] is also respected:
this independently observable diagnostic defect is not folded into a decoder
or lifecycle patch. Keep the case until upstream's real logger can format this
handler's record with both its protocol and packet arguments.

## Surrounding code and ownership map

The server registers the window subsystem's UI packet handlers and dispatches
`window-unmap` to the backend's override or inherited method. The base handler
checks `is_readonly(proto)` and otherwise emits one INFO diagnostic. Xpra's
`Logger` delegates to Python logging, whose record formatter performs the
placeholder substitution. Merely mocking the logger call does not exercise
the failing boundary. The Wayland subsystem overrides map/configure but not
unmap; the X11 seamless implementation owns its separate unmap behavior.

## Mechanism and lifecycle

One string now names the current `_process_unmap` method and contains two
placeholders for the two existing arguments. INFO level, argument objects,
readonly admission and handler dispatch remain unchanged. No window model,
map state, timer, thread or native resource is added or mutated. The handler
continues to be a base diagnostic, not an implementation of minimize/close.
Repeated packets each produce a valid diagnostic, not repeated formatting
tracebacks. Log escaping remains with the existing record/formatter layer.

## Case-commit and integration ownership

The product commit owns one line in `xpra.server.subsystem.window` plus the new
`unit.server.subsystem.window_logging_test`. It applies independently on the
embedded base and has no dependencies. The Wayland stream and initial-state
cases own their backend protocol/state behavior, not this base log string.
Decoder-loss notification uses the separate refresh handler and likewise does
not own this fix. No shared test harness change is required.

## Commit scope and non-goals

No logging verbosity, handler registration, readonly policy, packet schema,
native X11/Wayland operation, window shutdown or decoder change. Treating every
INFO base stub as leftover debugging would exceed the observed defect and
change the surrounding upstream convention. Correct formatting alone does
not claim to implement an inherited Wayland unmap/close operation.

## Regression design and clean control

`unit.server.subsystem.window_logging_test` calls the real base handler with a
real `Packet` and Xpra logger. A temporary stdlib handler executes
`record.getMessage()` so a mismatch raises at actual formatting instead of
being hidden in a mocked call or captured stderr traceback. Logger level,
propagation and handler membership are restored after each test. The only
server fixture substitution is the readonly predicate; no display is needed.

The unmap assertion fails on tests-only embedded production with the original
formatting `TypeError`, before producing the expected message, and succeeds
with the one-line repair. Independent readonly and adjacent map/close controls
preserve admission and the surrounding diagnostic convention. `unit.log_test`
retains log escaping behavior, and the existing server-window module covers
dispatch consumers. These checks do not execute native backend unmapping.

## Durable live or package boundary

The complete-stack Wayland lifecycle profiles carry normal client window-map,
configure and unmap traffic and retain complete peer logs. A formatter traceback
must not be accepted as successful lifecycle behavior. All nine profiles remain
mandatory; the deterministic unit is the direct formatting oracle even if an
individual live scenario does not issue this base packet. This is a Python
diagnostic repair with no new payload or native package boundary.

## Invariants not to simplify

- Keep one placeholder for each protocol/packet argument.
- Preserve readonly short-circuiting before the diagnostic.
- Leave backend behavior and log-level policy unchanged.
- Exercise formatting, not only invocation of a mocked logger method.

## Required validation

Use [canonical validation](../../docs/runbooks/validation.md): tests-only
negative, patched focused formatting/log/window tests, composed focused checks,
all three final upstream legs, full fork-control checks and the full nine-profile
live loop. Native unmap behavior remains outside the formatting unit's claim;
its backend composition is covered by the required live suite. Ruff uses the
embedded configuration; this case adds no scoped mypy module. Store transient
results in the session ledger, not this maintained rationale.

[scope-review]: https://github.com/Xpra-org/xpra/pull/5023#discussion_r3861406986
