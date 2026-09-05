# Video and connection lifecycle regressions

Both focused modules are exported inside `../fix.patch`; this directory does
not contain duplicate test sources. `../case.toml` is the authoritative module
and gate list, and the main case README maps their production ownership.

- `unit.server.window.video_compress_test` exercises native-pair publication
  and retirement, delayed image ownership, video/subregion GLib sources,
  captured flush requests, saved streams, scroll release, exhaustive dynamic
  mixin cleanup, and the real encode FIFO. Controlled resources and threads
  make ordering deterministic; actual GLib dispatch proves retained-source
  destruction across early one-shot completion.
- `unit.server.source.encoding_lifecycle_test` exercises connection-wide
  calculation admission and completion, local CUDA-context publication and
  loser release, cancellation failure, and the shared encode tail. Its native
  GLib control proves the delayed calculation handoff; its real dynamic
  connection proves that active calculation finishes before window teardown.

Both modules share `make_encoding_connection()`: the real muxer initializes
base, mmap, window, and encoding subsystems and processes an RGB hello. The
calculation test creates its source through `make_window_source()` and uses
the normal encode/UI cleanup. Additional stack-owned state must be initialized
by those production owners, not manually copied into the fixture.

Retain non-vacuous tests-only clean controls for both modules, run their
complete standalone patched selection, and repeat both through
`stacks/develop`. The stack must also run the generic timer and WSSO focused
modules. VPC stops the connection's calculation producer; WSSO independently
borrows each exact pixel source during calculation and bandwidth callouts so
individual removal is safe while the connection stays live.

The case declares no atomic live gate. Real codec-lifetime acceptance belongs
to the complete stack's Vulkan and OpenGL hardware-H.264 profiles, together
with the remaining five stack profiles. Those gates require frame-state
evidence owned by WIS and cannot honestly run with only VPC selected. Resource
doubles prove Xpra ownership and ordering, not backend-private GPU completion.
