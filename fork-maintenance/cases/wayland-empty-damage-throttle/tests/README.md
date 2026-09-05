# Empty-damage regression ownership

The case extends `tests/unittests/unit/wayland/window_test.py` through
`../fix.patch`; there is no second copy of the regression in this directory.
`../case.toml` declares that focused module, the native `wayland` boundary,
the upstream unit-test legs, and the positive `live-rgb` gate.

The focused tests call the real model and subsystem methods to establish
coalescing, damage-guard ordering, eligible-consumer checks, exact window
identity, re-arming, configure flush order, and terminal cancellation. The
fixture explicitly describes an ordinary root without active subsurface
children. This keeps standalone and composed tests on the WEDT boundary;
WSSO's separate regressions own authoritative tree classification, composite
root/child completion, and the ordinary-root delegation into this API.

Retain a tests-only clean control which exposes synchronous empty-commit
feedback, then run the complete patched focused and native selections both
standalone and through `stacks/develop`. Inspect discovery after changing
neighboring low-context patches: the empty-damage test class must remain
top-level, not become part of another case's test class.

The durable native fixture and its event-stream validator live in
`fork-maintenance/infra/live`. Atomic `live-rgb` acceptance requires real
callback pressure, bounded child input, ordered fixture exit, and owned
cleanup. The complete stack additionally retains all seven positive profiles.
Packet counters or an ad hoc timeout probe do not replace those boundaries.
