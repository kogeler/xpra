# Frame-aware Wayland encoding

## Boundary

A Wayland window may support transparency while its current committed buffer
is opaque. The window-level `has-alpha` capability therefore cannot select the
codec for every frame: an opaque `RGBX` or `BGRX` frame may use H.264, while an
`RGBA` or `BGRA` frame must use an alpha-capable picture encoding.

The patch publishes the current buffer format as internal model state and
selects encoding from that frame state without changing the client's window
capability. Alpha-bearing frames fail closed when no negotiated alpha-capable
encoding is usable for the current damage geometry and options. Opaque frames
enter the normal upstream selector only after per-window CSC capabilities are
available, preserving its small-region, text/lossless, encoding-hint, and
feature-override decisions. The lossless mmap transport remains authoritative
for both opaque and alpha-bearing buffers. Popup damage is emitted only after
the image and its pixel format are published, so a buffer-format transition
cannot encode the new frame using the previous frame's alpha state.

## Patch ownership

`fix.patch` owns the Wayland model/subsystem paths, the video-source selection
path, and their focused tests listed in `case.toml`. It does not reintroduce
upstream's initial `0x0` window fix.

The patch must resolve against the source embedded in current `develop`. When
an operator-selected upstream refresh changes the same production boundary, refresh the
full staged candidate and let `workspace-update` derive both `fix.patch` and
its manifest metadata. The clean host-worktree fallback may use `patch-update`.

## Required validation

Run the focused Wayland and initial-damage modules, the native Wayland gate,
and all three upstream unit-test legs. The complete `develop` stack also
requires the fixed adaptive-alpha/default hardware-H.264 live profile. Its
exact title-bound `vkcube` window has an initial `BGRX`/`RGBX` snapshot and
dynamic opaque frame-state proof. Startup layout and picture packets remain
validated but are not acceptance evidence. With both title-bound windows at
stable tiled geometry, an exact interval tied to the active IDR group must show
dominant H.264 main regions and complete per-crop coverage by only exact
one-pixel lossless RGB codec edges through the VA-API and hardware-presentation
chain. Its separately title-bound native-Wayland GTK window uses a required
RGBA visual and deterministic transparent border; every saved source
screenshot must prove transparent and opaque pixels, and its packets may
contain only positive WebP or alpha-bearing RGB32, with no H.264 or RGB24.
Visible pixels, real input, ordered application exit, and owned cleanup remain
mandatory. A fallback diagnostic is not this gate.
