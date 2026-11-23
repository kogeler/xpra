# Notify destruction of Wayland software output buffers

Code: the `Fork-Case: wayland-software-buffer-lifetime` commit on `develop`
(`make -C fork-maintenance case-show CASE=wayland-software-buffer-lifetime`).

## Boundary

The Wayland server (`xpra seamless --backend=wayland`) renders its headless
output either with the GLES2 renderer on a DRM render node or, when there is
no usable node, with wlroots' pixman software renderer. On the software path,
once the output has been resized (an attaching client's
`set_screen_size()` → `Output.resize()`), shutting the server down corrupts
the glibc heap: our E2E live detach profile caught it when the server exited
with `--exit-with-children` and aborted with `corrupted double-linked list` →
`Fatal Python error: Aborted` while the interpreter was garbage-collecting in
`Py_Exit`. Other runs of the same sequence end with `free(): invalid pointer`
or a segmentation fault in `_Py_Dealloc`; most runs without a memory checker
exit cleanly, because the damage only shows when an overwritten chunk is
later checked.

Necessary state: the pixman renderer, at least one output mode change, then
`WaylandCompositor.cleanup()`. Without a resize, with the GPU renderer, or
when the native teardown never runs, there is no corruption. The trigger does
not depend on windows, clients' surfaces, the keymap, the cursor, the scene
or Xpra's own Python state: a ctypes program calling only wlroots (headless
backend, pixman renderer, one `wlr_output_commit_state()` with a custom mode,
then `wlr_renderer_destroy()`) reproduces it. The expected behavior is a
clean shutdown; the actual one is a write into freed memory which the
interpreter has reused.

The user-visible effect is an aborting or crashing server on exit (exit status
134/139 instead of 0, `xpra stop` / exit-with-children / SIGTERM alike) for
every software-rendered session whose screen was ever resized: containers and
VMs without GPU access, `XPRA_WAYLAND_GPU=no`, or a service manager which does
not grant the render node (the live failure ran under `systemd --user`).

## Embedded-source context

Source: `d2940c7ddc6edd65aadb00f55ac559045d0c94ed`. The defect is in the
system wlroots 0.19.2 (`libwlroots-0.19` 0.19.2-1 in the Ubuntu 26.04 server
image; Debian 13 ships 0.18, which this backend does not support), not in
Xpra, but Xpra is what reaches it, and the fork cannot ship a wlroots fix:

- `types/buffer/buffer.c`: `buffer_consider_destroy()` calls
  `buffer->impl->destroy()` directly; since 0.19 every implementation must call
  `wlr_buffer_finish()`, which emits `events.destroy` and finishes the addons.
- `render/allocator/gbm.c`, `drm_dumb.c`, `types/buffer/dmabuf.c`,
  `types/wlr_shm.c` (client `wl_shm` buffers) and the others do;
  `render/allocator/shm.c` `buffer_destroy()` only does `munmap()`, `close()`
  and `free()`.
- `render/pixman/renderer.c` caches one `wlr_pixman_buffer` per rendered
  `wlr_buffer` in `renderer->buffers` and frees it from a listener on that
  buffer's destroy signal (`handle_destroy_buffer()`); `pixman_destroy()`
  calls `destroy_buffer()` for every remaining entry, which unlinks
  `buffer_destroy.link` from the (supposedly live) buffer's signal list.

Upstream Xpra's compositor (`xpra/wayland/server/compositor.pyx`, from
`8d0ce8e842` "#387 build the toy compositor"; GPU support added by
`1e8c331919` "#4877 add GPU support for applications") lets
`wlr_allocator_autocreate()` choose the allocator. For a renderer without a
DRM fd that is the shm allocator. Nothing in the current upstream source
avoids the path. Without this case every resized software session corrupts
the heap at shutdown.

Precedent for the design is the maintainer's own containment of an external
library defect: `xpra/util/glib_idle.py` (#5044) routes `GLib.idle_add`
through one persistent `GLib.Source` instead of changing PyGObject, keeping
the workaround inside Xpra's own call site. Existing native helpers in
`xpra/wayland/server/wlroots.pxd` (`cdef extern from *` blocks of small C
functions) show where such C belongs. Reviewed: local upstream history of
`compositor.pyx`, `wlroots.pxd` and `glib_idle.py`, the wlroots 0.19.2
sources named above, and the wlroots history of `render/allocator/shm.c`.
The #5044 and #4877 discussions on GitHub were not reviewed.

wlroots has fixed its allocator: `16cb509a` ("render/allocator: add missing
wlr_buffer_finish() in destroy impls", master, 2025-12-20) was backported to
the 0.19 branch as `4279ee50` and released in 0.19.3 (2026-03-19); every 0.20
release contains it. Only the 0.19 series before 0.19.3 is affected, but that
is what Ubuntu 26.04 LTS ships (0.19.2-1) and what upstream Xpra's own CI
builds against. The case retires when the server image's wlroots contains
that fix (0.19.3 or later); its replacement must establish that shm output
buffers emit their destroy signal before being freed.

## Surrounding code and ownership map

| Owner / symbol | Role |
| --- | --- |
| `WaylandCompositor.add_socket()` | Creates the display, headless backend (one 1920x1080 output), renderer (`create_renderer()`: GLES2 on a render node, otherwise `wlr_renderer_autocreate()`), and the allocator. With this case, `xpra_shm_allocator_create()` for a pixman renderer, otherwise `wlr_allocator_autocreate()`. |
| `WaylandCompositor.new_output()` | `wlr_output_init_render(output, allocator, renderer)`: the allocator is used only for the output's primary and cursor swapchains. |
| `Output.resize()` (`output.pyx`) | `wlr_output_state_set_custom_mode()` + `wlr_output_commit_state()`. |
| wlroots `output_ensure_buffer()` / `wlr_output_configure_primary_swapchain()` / `output_apply_state()` | Creates a swapchain at the new size, renders an empty modeset buffer into it with the renderer, and destroys the old swapchain, whose unlocked buffers are freed through `impl->destroy()`. |
| `Output.output_frame()` → `wlr_scene_output_commit()` | Acquires swapchain buffers and renders them (pixman: `get_buffer()` / `create_buffer()` per `wlr_buffer`). |
| `WaylandCompositor.cleanup()` | `wl_display_destroy_clients()`, scene, cursor, layout, seat, then `wlr_allocator_destroy()`, `wlr_renderer_destroy()`, `wlr_backend_destroy()` (outputs and their current swapchains), `wl_display_destroy()`. Called from `WaylandManager.cleanup()` and from `__dealloc__`. |

Client `wl_shm` buffers are wrapped by `types/wlr_shm.c`, which calls
`wlr_buffer_finish()`; they never come from this allocator and are unchanged.

## Mechanism and lifecycle

Old sequence (pixman renderer, stock shm allocator):

1. At start-up the output's first swapchain buffers are rendered; pixman
   creates a `wlr_pixman_buffer` for each one and listens on its destroy.
2. `Output.resize()` commits a new mode. wlroots creates a swapchain at the new
   size and destroys the old one. Each old buffer is freed without a destroy
   signal, so its pixman entry stays in `renderer->buffers` with a dangling
   `buffer` pointer and a listener linked into the freed buffer.
3. The interpreter reuses that memory for ordinary objects (in the live failure
   the disconnected client's connection cycle).
4. `cleanup()` destroys the renderer: `destroy_buffer()` on each stale entry
   rewrites the `prev`/`next` pointers inside memory now owned by Python
   objects (or by glibc's free lists).
5. Releasing those objects later (`gc.collect()` at exit) trips over the
   damaged chunk headers: abort or segmentation fault.

New sequence: for a pixman renderer the compositor installs
`xpra_shm_allocator`, which produces the same kind of buffer (a
memfd mapped `MAP_SHARED`, single plane, stride `width * bytes-per-pixel`,
offset 0, `WLR_BUFFER_CAP_DATA_PTR | WLR_BUFFER_CAP_SHM`, the same `get_shm`
and data-pointer access), but its `destroy` calls `wlr_buffer_finish()` first.
In step 2 every freed output buffer emits its destroy signal, pixman drops its
entry, and `renderer->buffers` only ever holds live buffers. The teardown
order is unchanged and now safe.

Hunks:

- The `cdef extern from *` C block: the buffer type, its
  `wlr_buffer_impl`, a bytes-per-pixel table covering every format the pixman
  renderer can render (`render/pixman/pixel_format.c`: the 8888, 2101010 and
  565 families) plus the 64-bit ABGR/XBGR 16161616 formats, size-overflow
  checks, `memfd_create(MFD_CLOEXEC)` + `ftruncate` (retried on `EINTR`), and
  the `wlr_allocator_interface`. An unknown format returns `NULL`, as wlroots
  does for an unsupported pixel format, and the swapchain creation fails
  visibly instead of producing a wrongly sized buffer.
- `add_socket()`: the allocator choice. `wlr_renderer_is_pixman()` is the
  exact condition under which `wlr_allocator_autocreate()` would have chosen
  the shm allocator in this backend (pixman has no DRM fd and only the
  data-pointer render capability); GLES2/Vulkan renderers keep autocreate and
  its gbm/dumb/udmabuf allocators. The existing "Failed to create allocator"
  check covers a failed allocation.

Failure paths: allocation errors before `wlr_buffer_init()` free only the
struct; after it nothing has been registered on the buffer yet, so plain
`free()` matches the stock allocator. `memfd_create` needs no file system
access, so a restricted runtime directory or Landlock policy cannot break the
software path (`shm_open` would need `/dev/shm`). The allocator is destroyed
by `wlr_allocator_destroy()`, which emits its destroy signal (swapchains drop
their pointer) and calls `xpra_shm_allocator_destroy()`.
Threading: all of this runs on the main thread inside wlroots calls made by
the compositor; there is no new state, lock or callback into Python.

## Case-commit and integration ownership

The commit changes `xpra/wayland/server/compositor.pyx` (C block and
allocator choice) and adds `tests/unittests/unit/wayland/software_allocator_test.py`.
No dependency: it applies to the upstream base alone. The subsurface
case (`wayland-subsurface-stream-ownership`) also edits `compositor.pyx`
(`connect()`/`disconnect()`, `discover_subsurfaces()`, `emit()` snapshotting)
in unrelated hunks. The keymap case (`wayland-client-keymap-sync`) owns the
keyboard devices which are torn down before the compositor; neither changes
renderer, allocator or output ownership. Composed risk: every case which
captures or encodes surface pixels uses client buffers and texture readback,
not this allocator, so their behavior on the software path is unchanged.

## Commit scope and non-goals

Unchanged: renderer selection (`XPRA_WAYLAND_GPU`, render node discovery,
GLES2 fallback), the GPU allocators, output sizes and formats, the teardown
order, client buffers, screencopy/image-capture paths, packaging (the
allocator is part of the existing extension module; no new dependency).

Tempting but incorrect repairs, all tried in the standalone reproduction:
skipping `wlr_renderer_destroy()` at shutdown still crashes after repeated
resizes (6/6 with 12 resizes), because stale entries stay reachable through
other teardown paths and through address reuse by later buffers. Leaking the
whole compositor at exit avoids the crash but leaves the Wayland socket and
lock behind and does not fix the stale cache while the server runs.
Reordering the teardown (backend before renderer) crashes too (3/4). Calling
`wlr_buffer_finish()` on swapchain buffers owned by wlroots from Xpra would
finish buffers which wlroots still uses. Granting the live fixture GPU access
would hide the defect from the only profile which exercises software rendering.

## Regression design and clean control

`unit.wayland.software_allocator_test` runs the real compositor in a fresh
interpreter (as `unit.wayland.subsurface_discovery_test` does) with
`XPRA_WAYLAND_GPU=no` and `WLR_RENDERER=pixman`, so the real
`create_renderer()` → pixman → allocator route is used. It starts the backend,
takes the real `Output` from the `new-output` signal, resizes it twelve times
through `Output.resize()` (each must return the requested size), holds
interpreter objects allocated after each resize, runs `cleanup()`, then
releases the objects and collects. glibc's `libc_malloc_debug.so.0` with
`MALLOC_CHECK_=3` and `PYTHONMALLOC=malloc` makes the overwritten chunks fail
on release; the probe must exit 0 and print its completion marker in each of
three fresh processes.

Clean control (tests-only): the stock allocator leaves stale pixman entries;
the probe aborts or segfaults on release, a behavioral failure of the real
route, not a missing import. The standalone wlroots reproduction failed 6/6
with the stock allocator and 0/6 with this allocator. Substitutions: no
client, no windows, no frames (the modeset buffers alone are enough); the
memory checker is the observer. The test fails rather than skips when the
checker library or the native module is missing. Blind spot: an allocator
which emits destroy but produces wrong pixels would pass; the live profiles
render and compare real software-rendered frames.

## Durable live or package boundary

`live-xpra-detach`, scenario `ssh-detach-persistent-01`: the server runs as a
`systemd --user` service which cannot open the render node, so it uses the
pixman renderer. The client's attach resizes the output; the detach,
application exit and `--exit-with-children` shutdown follow; the runtime log
audit rejects `fatal-python-error` / `native-memory-error` and the exit receipt
must be 0. The other detach scenarios use the GPU renderer and do not reach
this path. The live run proves the shipped extension module; it does not
prove every pixel format.

## Invariants not to simplify

- Every buffer handed to the pixman renderer must emit its destroy signal
  before its memory is freed: `wlr_buffer_finish()` comes first in
  `xpra_shm_buffer_destroy()`, before `munmap()`/`free()`.
- Use the replacement exactly when the renderer is pixman. A GPU renderer
  needs the DMA-BUF-capable allocators; an shm-only allocator would fail its
  `wlr_output_init_render()` capability check.
- Keep the buffer equivalent to wlroots' (single plane, `MAP_SHARED`, packed
  stride, both capabilities); the headless backend and pixman rely on
  `get_shm` and data-pointer access respectively.
- Keep the teardown order and the reliance on wlroots' destroy signals rather
  than skipping native destruction.

## Required validation

Follow [canonical validation](../../docs/runbooks/validation.md):
`case-check`, the case's `focused` and native `wayland` targets patched and
tests-only (the clean control must fail in `software_allocator_test`), the
stack `focused` and `wayland` runs, new DEBs for both distributions, then the
live walk from `live-xpra-detach` and one complete nine-profile pass with
`live-suite-check`, followed by the `full`, `full-cython` and
`full-no-compat` legs. The C block is compiled by the normal Cython build;
there is no separate static check beyond the compiler warnings.
