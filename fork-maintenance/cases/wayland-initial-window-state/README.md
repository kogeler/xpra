# Frame-aware Wayland encoding

## Boundary

A Wayland window may support transparency while its current committed buffer
is opaque. The window-level `has-alpha` capability therefore cannot select the
codec for every frame: an opaque `RGBX` or `BGRX` frame may use H.264, while an
`RGBA` or `BGRA` frame must use an alpha-capable picture encoding.

The patch publishes the current buffer format as internal model state and
selects encoding from that frame state without changing the client's window
capability. Alpha-bearing frames fail closed when no alpha-capable encoding is
negotiated. Opaque frames use the selected H.264 path only after per-window CSC
capabilities are available.

## Patch ownership

`fix.patch` owns the Wayland model/subsystem paths, the video-source selection
path, and their focused tests listed in `case.toml`. It does not reintroduce
upstream's initial `0x0` window fix.

The patch must resolve against the currently synchronized upstream `master`.
When upstream changes the same production boundary, refresh the full staged
candidate and let `patch-update` derive both `fix.patch` and its manifest
metadata.

## Required validation

Run the focused Wayland and initial-damage modules, the native Wayland gate,
and all three upstream unit-test legs. The complete `develop` stack also
requires the hardware-H.264 live profile so opaque H.264 regions,
alpha-capable fallbacks, presentation, input, lifecycle, and cleanup are all
observed.
