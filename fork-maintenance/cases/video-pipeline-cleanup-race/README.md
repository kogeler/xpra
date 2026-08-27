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

It must apply to the currently synchronized upstream `master` either after the
earlier active cases in `stacks/develop.toml` or as its standalone case. If
current upstream changes either boundary, refresh this patch from a staged
candidate; never edit its digest or path list by hand.

## Required validation

Run the focused cleanup module first, then all three upstream unit-test legs.
Because the change affects live hardware codec lifetime, the complete
`develop` stack also requires the Wayland hardware-H.264 profile.
