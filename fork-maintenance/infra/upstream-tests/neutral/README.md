# Native Wayland pointer protocol regression

## Current ownership and upstream replacement

These are current runner-owned tests, not a production patch, quarantine or
historical verification archive. They retain the real native boundary after
upstream `9d0ec89ab8b53477f29c2dcbed38dcbb95dfc45e` incorporated the complete
wheel conversion at embedded source
`d95058b0916913fe6ae5296fb702f66d833898b0`.

The current native `WaylandPointer.wheel_motion()` uses the discrete wheel
table, takes direction from the mapped button, scales the absolute click
magnitude to surface/value-120 units, and rejects unsupported wheel buttons.
The previous downstream production delta is exactly reverse-applicable to
that source; manual review found no residual native conversion fix. The
separate production case is therefore retired, with runtime confirmation
still required through this maintained boundary.

Client GTK event admission remains owned by
[`gtk-client-scroll-deduplication`](../../../cases/gtk-client-scroll-deduplication/README.md).
One admitted packet's native conversion is independent of choosing one client
representation: a duplicate packet still means a duplicate native operation.

## Direction, units and lifecycle

The common client pointer subsystem maps direction buttons, including user
inversion, and serializes the signed distance in thousandths of a click.
The server parser divides by 1000 before calling the precise device. Native
Wayland must neither divide again nor undo mapped inversion with the sign of
the accompanying distance.

| Button | Native direction | Axis | Surface units per click | Value-120 |
| --- | --- | --- | --- | --- |
| 4 | Up | Vertical, 0 | -15 | -120 |
| 5 | Down | Vertical, 0 | +15 | +120 |
| 6 | Left | Horizontal, 1 | -15 | -120 |
| 7 | Right | Horizontal, 1 | +15 | +120 |

The production formula is the table's signed surface step multiplied by the
absolute click magnitude. A quarter click retains magnitude 3.75 surface units
and 30 value-120 units. Version-5 pointer consumers receive whole-step
`axis_discrete`; version-8 consumers receive `axis_value120`. The test oracle
uses independent literal expectations, not the production table.

Each admitted wheel operation emits one axis notification and one frame.
Wheel-button releases emit nothing; unsupported wheel buttons cannot become
ordinary vertical motion. Ordinary buttons retain Linux button-code mapping
and their press/release frames.

Focus, surface coordinates, readonly/source admission and compositor flushing
remain outside this conversion. The test establishes an actual mapped surface,
enters it with the real device, and flushes around each observation. It does
not change pointer lifetime, acceleration, inertia, axis-stop policy, relative
motion or constraints. Native units are not a promise of a fixed number of
application pixels or text lines.

## Real protocol fixture

`pointer_scroll_test.py` builds `pointer_scroll_client.c` with the installed
Wayland protocol descriptions, `wayland-scanner`, pkg-config and a C compiler
using `-Wall -Wextra -Werror`. Missing dependencies fail rather than skip.
Every pointer version gets a fresh Python interpreter, private runtime
directory, actual compiled compositor and Pixman renderer. The child inherits
the selected installed-module search path, not an unrelated host Xpra import.

The C consumer binds a version-5 or version-8 seat/pointer, creates an xdg
toplevel and attaches a 64x64 shared-memory buffer. Real `wl_pointer` callbacks
are serialized as JSON. Compositor flushing and client roundtrips delimit
events, with explicit ready/sync markers, checked process exit and bounded
waits. Teardown attempts the client, device and compositor independently even
if an earlier cleanup fails.

The assertions cover:

- every discrete wheel direction, an observed pointer enter, one signed axis
  group per press and silence on release;
- all four mapped buttons with positive/negative one- and two-click distances,
  plus positive/negative quarter clicks for version 8;
- one axis, one wheel-source record, one version-appropriate step record and
  a final frame, rejecting missing or duplicate protocol events;
- unsupported `wheel_motion(1, 1)` with no events, followed by a real ordinary
  button-1 press/release tail with native code 272.

This is not a mocked native method or an XI2 input generator. The original
negative oracle is now expected to pass on clean new upstream. A new failure
must be investigated, not hidden by restoring the retired production patch or
weakening the expected units.

## Frozen installation and inventory

The fixed inventory in `../neutral_tests.py` installs only these two files at
`tests/unittests/unit/wayland/` inside the runner's private cloned source.
It verifies the exact frozen HEAD and real checkout root, rejects symlinked
inputs/parents and any pre-existing target, validates both payloads before
writing either, and stages exactly the new files. An upstream test at the same
path requires explicit reassessment; even byte-identical files are not silently
overwritten or accepted as equivalent coverage.

The helper and both fixtures belong to the image-input hash, validated streamed
image context, Containerfile copy inventory and host runner digest. Each run
logs the installed paths and SHA-256 values. Staging includes them in the
focused applied-tree identity. Changing fixture/oracle bytes invalidates that
image/runner evidence, while this explanatory README is not an executable input.

The same installation runs after selected case handling for `clean`,
`tests-only` and `patched`. A clean run applies no case patch and leaves Xpra
production code unchanged, but still receives these independent test inputs.
This creates no new patch mode or partial live product. No neutral fixture is
copied into host Xpra source, the DEB package payload or a live endpoint.

Offline `test_neutral_tests.py` checks exact staging and input bytes on clean
and staged-production copies, frozen-source rejection, no-clobber behavior,
symlink/missing-input handling and image/runner inventory binding. Those are
fork-control tests, not execution of the native protocol regression.

## Required clean and resulting-stack proof

During an upstream refresh, first complete the whole-queue manual-review exit.
Then use the existing public native gate on the same source and immutable image:

```bash
make -C fork-maintenance test-start STACK=develop PATCH_MODE=clean \
  TARGET=wayland RUN=<cycle>-neutral-pointer-clean
make -C fork-maintenance test-start STACK=develop PATCH_MODE=patched \
  TARGET=wayland RUN=<cycle>-neutral-pointer-stack
```

Both runs must discover and execute `unit.wayland.pointer_scroll_test` with the
real protocol consumers. The stack's focused inventory also retains that module,
the server/client pointer modules and `unit.pointer_loopback_test`; run focused,
compiled and no-compat composition as required by the enclosing validation flow.
Native test discovery in full upstream legs sees the retained files too.

The shared hardware live profiles exercise Sway-to-Xwayland smooth input, while
GTK detach/transport-loss profiles exercise XTEST discrete input. Each stimulus,
including the first, must have exactly one wire operation, the corresponding
native axis, actual remote GTK displacement and visible feedback. Complete
post-input logs and fixture tails reject late duplicates. Those application
observations complement, rather than replace, native fractional/version checks.

Require all nine profiles through `live-all STACK=develop RUN=<fresh-prefix>`
and `live-suite-check`, with the complete queue on both endpoints. Final full
upstream, package and fork-control obligations remain unchanged. Keep exact
current result identities in the ignored cycle ledger, not here.
