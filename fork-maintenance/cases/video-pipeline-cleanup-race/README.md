# Video pipeline cleanup ordering

## Boundary

Encoder reinitialization can overwrite a live codec pair, and an exception
while closing the CSC can prevent both encoder cleanup and the final pipeline
sweep. The patch requests cleanup before a real reinitialization and uses
nested `finally` blocks so every captured codec and the late sweep are handled.

The upstream timer, flush, direct encode-thread policy, and inherited
cleanup/suspend paths remain unchanged.

## Patch ownership

`fix.patch` owns only:

- `xpra/server/window/video_compress.py`;
- `tests/unittests/unit/server/window/video_compress_test.py`.

It must apply to the current frozen fork-master source either after the earlier
active cases in `stacks/develop.toml` or as its standalone case. If a later
fork-master refresh changes either boundary, refresh this patch from a staged
isolated candidate; never edit its digest or path list by hand.

## Required validation

Run the focused cleanup module first, then all three upstream unit-test legs.
Because the change affects live hardware codec lifetime, the complete
`develop` stack also requires the fixed adaptive-alpha/default Wayland
hardware-H.264 profile. Its exact title-bound primary has an initial
`BGRX`/`RGBX` snapshot and dynamic opaque frame-state proof. Startup layout and
picture packets remain validated but are not production. An exact stable-tile
interval bound before auxiliary exit must show predominant H.264 main regions
and complete per-crop coverage by only exact one-pixel lossless RGB codec edges.
It must complete the matched VA-API encode/decode, packet-chain, presentation,
lifecycle, and cleanup boundaries. Every saved source screenshot of the
separate deterministic transparent GTK auxiliary must prove transparent and
opaque pixels; it may use only positive WebP or alpha-bearing RGB32 packets and
may never enter H.264.
