# Capture debug snapshots from available window buffers

Code: the `Fork-Case: window-debug-snapshot-lifetime` commit on `develop`
(`make -C fork-maintenance case-show CASE=window-debug-snapshot-lifetime`).

## Boundary

Screen-update recording schedules `WindowSource.save_screenshot()` through
`GLib.idle_add()` after recording an encoded update. The callback runs later
on the UI thread. Its source window can have been cleared by terminal cleanup,
or its current model can temporarily have no image. Resize can also make the
source's cached dimensions differ from the current model or retained raster.

The old callback dereferenced the image before checking it, used cached source
geometry, and allowed an unavailable capture's `ValueError` to escape. Real
Wayland SSH churn produced repeated bare GLib callback tracebacks while frames
continued rendering and processes exited normally. These are asynchronous
callback failures, not harmless caught codec-capability probes.

Debug recording must remain enabled and write real PNGs when pixels are
available. An opportunistic snapshot with no current window, positive geometry
or capturable image has nothing to write; it should return without rescheduling.
A later ordinary capture may succeed when a matching buffer arrives.

## Embedded-source context

The source remains `646b712d1bff46b08b8f26ddc7a83229d65ba5bc`.
Antoine Martin's `15605ed2bc` extracted the callback and changed recording to
queue it with GLib. The old image-format lookup precedes its null check in the
current upstream base. The actual commit and current recording/capture owners
were read. `ImageWrapper.get_sub_image()` explicitly raises `ValueError` for
requests extending past its buffer; the Wayland model can reach that helper
while model geometry and a retained raster are temporarily different.

The callback also still indexes `self.window` after it may become `None`.
Both failures predate this fork. The existing recording option is useful live
evidence and must not be disabled to silence its callbacks. Retirement requires
upstream to handle deferred capture availability and preserve successful pixel
recording. No upstream endorsement is claimed.

## Surrounding code and ownership map

| Owner | Responsibility |
| --- | --- |
| `server/window/compress.py` screen-update recorder | Writes update metadata and schedules one opportunistic UI screenshot at the existing flush boundary. |
| `WindowSource.save_screenshot()` | Resolves the current source window, captures its current geometry, encodes supported pixels and writes the PNG. |
| `WindowSource.ui_cleanup()` | Clears the window reference during terminal cleanup; a queued diagnostic callback may still run. |
| Window model | Owns current geometry and capture availability; Wayland may return no image or reject a crop during a buffer transition. |
| `codecs/image.py:ImageWrapper` | Owns pixel/crop bounds and the borrowed/owned wrapper contract. |
| Pillow encoding helper | Performs the real PNG conversion; I/O or conversion failures retain their diagnostic behavior. |

The callback is UI-owned, so current model dimensions may be read there. This
case does not move capture or GObject access onto the encode worker.

## Mechanism and lifecycle

The callback snapshots the current window reference and returns if it has been
cleared. It asks that model for current dimensions and returns for empty
geometry. It then requests the image, treating `ValueError` from that capture
as a temporarily unavailable snapshot and logging the reason at debug level.
It returns for a missing image before reading its format.

For a supported real image, the existing Pillow PNG conversion and file write
remain unchanged. The capture exception boundary does not include conversion
or file output: unexpected encoding, import or I/O failures are not hidden.
Unsupported image formats retain the old no-output behavior. A failed optional
sample creates no timer, retry, wait, synthetic pixels or completed capture.

The callback does not free a model-owned image explicitly. A capture provider
may return a borrowed wrapper or its own retained image, depending on backend
and crop. Existing wrapper ownership/refcount behavior is preserved; adding an
unconditional `free()` would corrupt a provider that returned its own raster.

## Case-commit and integration ownership

This independent case owns only deferred debug-capture availability and its
focused module. It introduces no lifecycle field or API supplied by another
case, so its standalone application/removal requires no dependency. The model
dimension method is already required by ordinary source construction.

`window-source-timer-lifecycle` owns production damage/timer admission and
terminal barriers; this case neither replaces that protocol nor claims to
cancel every queued diagnostic callback. `video-pipeline-cleanup-race` owns
native encode resources, and WSSO owns retained surface snapshots and wrapper
leases. This callback consumes the existing capture contract without changing
those owners or destroying their borrowed rasters.

## Commit scope and non-goals

Make existing optional samples safe across unavailable model/buffer states.
Do not disable recording, accept missing normal render frames, relax screenshot
pixel checks, change image geometry globally or suppress arbitrary callback
exceptions. No preload, application-specific option, polling or new native
readback method is added. Normal frame capture and transport remain independent
of this diagnostic sample.

## Regression design and clean control

`unit.server.window.debug_snapshot_test` invokes the production callback with
its existing source fields. Its focused fixture deliberately bypasses unrelated
worker/timer construction because the callback consumes only the window,
cached dimensions and output directory; it adds no private lifecycle field.
Window providers represent no-image and terminal states, while real
`ImageWrapper.get_sub_image()` supplies actual crop validation and pixels.

The control covers a cleared source window, an absent buffer and empty current
geometry. A stale cached source size must not prevent a valid current 2x2 model
from writing a real PNG with exact pixel values. A geometry/buffer mismatch
must produce no screenshot, then a later matching buffer must produce a real
3x3 PNG. The retained model image must remain unfreed. Pillow decoding of the
written file verifies the result; conversion/output are not mocked.

The tests-only control reaches the original null dereferences and real crop
`ValueError`, rather than requiring a new method or field. Existing compression
and video-compression modules remain selected. These synthetic model providers
do not prove native wlroots/X11 readback, dispatch timing or every platform;
package-backed live tests supply the actual deferred callback environment.

## Durable live or package boundary

The complete-stack detach/churn fixture keeps screen-update recording enabled.
Its server stderr exposed `save_screenshot()` callback failures between
successful frames. Positive acceptance must reject those callback tracebacks,
including interleaved logging, rather than trusting process status or a later
successful screenshot. Real valid snapshots and source-appropriate pixels
remain required; debug evidence must not be turned off to pass the suite.

Use both exact validated Ubuntu server and Debian client DEBs with the complete
current stack. Continue the live loop from the earliest failed gate, finish
its remaining profiles and obtain one complete nine-profile pass without a
fix, followed by suite check. Runtime identities/results live in the ignored
session ledger, not this README.

## Invariants not to simplify

- Resolve the current window and geometry when the deferred callback executes.
- Missing or temporarily uncapturable buffers create no screenshot or retry.
- Preserve real PNG output and recovery on a later normal sample.
- Keep capture availability separate from conversion and I/O failures.
- Respect borrowed image ownership and reject actual callback errors in live logs.

## Required validation

Run tests-only and repaired focused controls, existing compression tests and
the composed stack, native Wayland and all three full legs. Complete offline
oracle controls, both real DEB builds and the package-backed live loop, keeping
recording enabled and ending in one full nine-profile pass with peer-log checks.
