# Case-owned regression boundaries

The regression sources are exported inside `../fix.patch`; this directory
does not carry a second copy. `../case.toml` is the authoritative module and
gate list. The main case README explains the surrounding production owners
and the full acceptance contract.

The focused selection covers these complementary boundaries:

- `unit.wayland.subsurface_discovery_test` compiles its adjacent tracked C
  protocol client in the frozen image and exercises real wlroots first-role
  publication for prebuilt roots, prebuilt children, and synchronized trees.
  Missing native support fails rather than skips. The process boundary keeps
  deterministic adapter mocks out of this native lifecycle test.
- `unit.wayland.subsurface_stream_test` and `unit.wayland.pointer_test`
  control native-adapter/model state, normalized sampling, root-indexed
  reconciliation, stable identity, role reparenting, and leaf input.
- `unit.server.window.subsurface_source_test` covers source ownership,
  immutable capture, per-root transactions, publication/ACK routing,
  scheduler lifetime, composition-drain counters, and exact source borrows
  during background recalculation and bandwidth work. Its calculator tests
  compose the real window and encoding subsystems so complete-stack producer
  fencing and standalone per-source ownership remain compatible. Real global
  statistics exercise the shared diagnostic namespace with historical queue
  samples alongside current ownership/queue counts. Decode-error recovery
  runs its real scheduled GLib callback, including the generic timer wrapper
  when that separate case is selected.
- Cairo and mapped GTK OpenGL tests compare real staging and committed
  pixels. The OpenGL depth controls distinguish persistent byte-composite
  storage from configured ordinary/output depth and cover partial updates,
  abort, ordinary/scroll transitions, resize, and close.
- Client window/draw and GTK/terminal capability tests bind backing epochs,
  rejection, refresh, callback completion, and exact renderer admission.

Use named jobs, with a fresh run name for every attempt:

```bash
make -C fork-maintenance test-start CASE=wayland-subsurface-stream-ownership \
  TARGET=focused PATCH_MODE=patched RUN=<unique-run>
make -C fork-maintenance test-status RUN=<unique-run>
make -C fork-maintenance test-collect RUN=<unique-run>
```

The separate `live-wayland-subsurface` profile selects exactly this case for
both endpoints. Its schema-5 fixture, canonical source oracle, retained raw
packet replay, real input, active-producer proof, final drain, and owned
cleanup are maintained in `fork-maintenance/infra/live`, not duplicated here.
Source callback counts and captured transaction counts have distinct owners:
normal pending-damage coalescing may produce fewer transactions than native
commits. Every captured transaction and ACK must still be complete, at least
two distinct transactions must finish while production is active, and the
terminal canonical state must arrive exactly.

Keep tests-only clean-source controls and current-candidate before/after
controls for every newly corrected boundary. A passing mocked topology test
cannot replace the real native discovery control; packet replay cannot
replace the independent canonical source oracle; and a clean-source or
fallback diagnostic cannot publish positive live acceptance.
