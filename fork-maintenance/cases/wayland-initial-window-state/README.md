# Frame-aware Wayland encoding

## Boundary

A native-Wayland window has two different alpha facts. Its public `has-alpha`
property says that the window may submit transparent buffers during its
lifetime. The format of the current committed buffer says whether the pixels
selected for the next draw are `RGBA` / `BGRA` or the opaque `RGBX` / `BGRX`
variants. Codec selection must use both facts without turning the private
per-frame value into changing window metadata.

This case publishes the current readback format as internal Wayland model
state before the corresponding damage can select a coding. Each
`WindowVideoSource` lazily acquires the applicable model notification on its
first damage, caches a tri-state alpha classification, and rebinds its encoding
selector before that damage can enter batching. Later format notifications
rebind immediately, while every damage samples the current class and rebinds
only when that class or its derived alpha policy changed. This folds a resize
or other generic policy change into the callable without rebuilding it for
unchanged steady-state frames. A
frame whose alpha must be preserved is restricted to a negotiated,
geometry-usable transparency encoding and fails closed. The actual extracted
wrapper is checked again before video masks or encode-queue handoff, including
video-subregion and refresh routes which choose their own coding. A known opaque frame
enters the complete upstream adaptive selector, so it may use H.264 when the
normal size, content, quality, CSC, and policy rules allow it.

The same source also owns a narrow first-damage barrier. Ordinary mapped
toplevels do not encode a video-only initial frame before their client backing
has supplied the relevant per-window CSC modes. The map-time refresh remains
the recovery edge; no timer, polling loop, or stored damage replay is added.

Frame policy is kept coherent when generic encoding configuration changes.
Opaque-region changes recompute `discard_alpha` before selector rebinding, and
the video dimension-update hook reapplies the cached frame class immediately
after the base source has changed opaque-region coverage. This includes a
resize discovered inside the generic damage method itself. Only the
UI-owned notification and damage routes reach into the GObject model.

This case owns frame state and the Python Wayland image/format publication
seam. The separate `wayland-subsurface-stream-ownership` case feeds that seam
with copied normalized root and child snapshots, owns native surface identity
and topology, and publishes an ordered raw RGB32 transaction into the parent
backing with atomic client-side composition. WIS decides what the current
model image means for ordinary encoding; WSSO decides how a borrowed native
tree generation becomes retained state and one atomic composite transaction.
The separate `video-pipeline-cleanup-race` case owns codec instances and video
resource teardown.

## Embedded-source context

The case resolves against source commit
`212038243d0067b6860ebe7d6953692179ef353f`, embedded in the current `develop`
history. That source already supplies the surrounding contracts which this
patch preserves:

- video pipeline candidates are built locally and published only after codec
  initialization;
- client map properties can update an existing per-window source;
- a Wayland model whose geometry is still `0x0` is withheld until it becomes
  ready;
- native image access follows the existing `ImageWrapper` locking and lifetime
  rules; and
- ordinary non-composite toplevel empty-damage acknowledgement and
  frame-callback pacing remain owned by `wayland-empty-damage-throttle`, while
  WSSO owns composite-root acknowledgement and child commit completion.

The case does not reimplement any of those behaviors. On an upstream refresh,
retirement is behavioral: clean upstream must publish the current format at
the same ordering boundary, preserve alpha-safe selection and CSC readiness,
and pass the same focused and hardware gates. Patch application alone is not
evidence of continued need or correctness.

## Surrounding code and ownership map

The behavior crosses native capture, the model bridge, client backing
negotiation, generic compression, and the video selector:

| Layer | Responsibility |
| --- | --- |
| `xpra/wayland/server/wayland_surface.pyx` | Captures wlroots texture or DMA-BUF content, downloads when required, and maps the native read format to an Xpra pixel format. |
| `xpra/wayland/server/surface.pyx` | Emits the standalone mapped-toplevel image before its commit/damage event; with WSSO selected, emits a borrowed normalized `surface-snapshot` whose synchronous consumer must copy it. |
| `xpra/wayland/server/popup.pyx` | Emits popup commit metadata before the separately captured image; the Python bridge must therefore defer popup damage until image publication. |
| `xpra/wayland/server/subsurface.pyx` | Emits a child image plus logical/native geometry through the standalone route; with WSSO selected, emits one borrowed atomic `subsurface-commit` carrying image, damage, colourspace, geometry, and authoritative tree state together. |
| `xpra/wayland/server/subsystem/window.py` | Creates Wayland models, publishes current format and image, orders popup damage, and performs the map-time refresh. |
| `xpra/wayland/server/models/window.py` | Owns toplevel/popup properties; `pixel-format` is internal encoding state and `has-alpha` remains public capability. |
| `xpra/wayland/server/models/subsurface_window.py` | Stores the current child image and its internal format before the child source is damaged; WSSO extends this model with retained normalized snapshots and a content generation. |
| `xpra/server/source/window.py` | Creates one source per client/window and carries map properties to the source. |
| `xpra/client/gui/window/backing.py` | Derives backing-specific `encoding.full_csc_modes`, possibly omitting values equal to connection defaults. |
| `xpra/server/window/compress.py` | Owns stable transparency policy, opaque-region state, mmap precedence, damage batching, image extraction, and the later encode-side `pixel_format`. |
| `xpra/server/window/video_compress.py` | Owns the frame-alpha cache, selector rebinding, client CSC readiness, adaptive video choice, CSC scoring, and encoder pipeline entry. |

With this case selected by itself, the normal mapped-toplevel path is:

```text
wlroots surface commit
  -> native capture creates ImageWrapper
  -> native surface-image signal
  -> WaylandWindowServer.surface_image()
       -> model pixel-format
       -> model image
  -> native commit with damage rectangles
  -> WaylandWindowServer.commit()
  -> refresh_window_area()
  -> WindowVideoSource.damage()
       -> acquire one notify::pixel-format lease, if supported
       -> UI-thread format sample and selector rebind
  -> delayed / merged send_regions()
       -> choose coding from cached frame policy
  -> process_damage_region()
       -> extract current ImageWrapper
       -> record actual encode-side pixel format
  -> do_process_damage_image()
       -> validate actual wrapper alpha, geometry, and selected coding
       -> preserve picture dimensions; apply masks only to video
  -> CSC / picture or video encoder
```

After that first damage has acquired the lease, a later model format update
also runs the synchronous `notify::pixel-format` callback between format and
image publication. Thus construction order is covered by the mandatory first
damage sample, while later `A <-> X` transitions rebind before the replacement
image becomes damageable.

Damage can be delayed or coalesced, so this is not a promise that one Wayland
commit becomes one Xpra packet. The notification or mandatory damage sample
establishes the safe coding family for the newest published image; the later
extracted wrapper format remains the final authority for alpha admission, CSC,
and pipeline validation. A wrapper which was captured before a later model
notification is not reinterpreted using that newer model class.

When WSSO is composed in the stack, native toplevel ingest instead emits
`surface-snapshot`. The synchronous Python callback copies the borrowed,
already normalized logical raster into the same retained `Window` model and
publishes its matching internal format before the retained image. Native child
ingest emits one `subsurface-commit` carrying the borrowed normalized raster,
authoritative tree generation, geometry, damage, and colourspace together; the
child facade installs a coherent retained image/format generation before any
child damage is reconciled. In both routes the native emitter remains the sole
owner of the borrowed wrapper and frees it after all callbacks return.

WSSO then captures every intersecting root and child wrapper synchronously and
emits exact uncompressed RGB32 stages. Those stages deliberately bypass this
case's frame-alpha selector and CSC-readiness barrier, including the root
stage. The parent remains a `WindowVideoSource` for ordinary root-only damage
and the generic raw capture machinery, but a composite transaction cannot be
redirected into picture/video selection without losing its atomicity and
premultiplied layer semantics.

The shared model seam does not transfer transaction ownership to WIS. For a
root, WSSO extends `Window.set_image()` with private retained-byte ownership,
generation counting, clear semantics, and rollback of a partially notified
replacement; the WIS-owned `pixel-format` update remains part of the same
publication. For a child, WSSO's retained replacement calls the WIS-owned
`SubsurfaceWindow.set_image()` seam only after it has copied and normalized the
borrowed raster. WSSO then exposes topology and schedules transaction damage.
The combined native tuple, borrow/free lifetime, topology epoch, content
generation, raw packet set, client staging, and final swap are never WIS
state.

## Capability, frame, and client state

The implementation deliberately keeps these domains separate:

| State | Meaning | Consumer |
| --- | --- | --- |
| `has-alpha` | Stable capability of the producer window. | Generic window metadata and transparency policy. |
| model `pixel-format` | Format of the most recently published Wayland image. | UI-thread notification and damage-time sampling. |
| `_current_frame_has_alpha` | `True`, `False`, or `None` derived from the model format. | Cached video-source selector input. |
| `discard_alpha` | Generic policy derived from opaque region, dimensions, and other source state. | Determines whether alpha may intentionally be discarded. |
| `_want_alpha` | Current result of capability, client support, discard policy, and frame class. | Selects alpha-safe versus ordinary adaptive encoding. |
| `WindowSource.pixel_format` | Format of the image actually extracted for encoding. | CSC and video pipeline scoring/setup. |
| `_client_csc_modes_resolved` | Whether backing-specific map information reached a terminal state, including an explicit empty mapping. | Initial video-damage barrier. |
| `full_csc_modes` | Client-advertised output formats for each video codec. | Candidate readiness and pipeline construction. |

The frame policy is equivalent to:

```text
frame_requires_alpha = current_frame_has_alpha is not False
want_alpha = (
    is_tray or (has_alpha and supports_transparency)
) and not discard_alpha and frame_requires_alpha
```

`None` is conservative. An empty or unfamiliar format restores the stable
capability policy; it never retains a selector derived from an older opaque
frame. An A-format is treated as alpha-bearing even if a particular pixel
sample happens to be opaque. Pixel inspection is not a substitute for the
buffer format and lifetime contract.

`has-alpha` is never changed when the frame class changes. Exporting the
private value as public metadata would cause capability churn, confuse client
backing lifetime, and make a per-buffer decision look like a window property.

## Native format provenance

The native capture path normalizes the supported wlroots read formats to four
explicit Xpra values:

| DRM read format | Xpra format | Cached class |
| --- | --- | --- |
| `DRM_FORMAT_ABGR8888` | `RGBA` | alpha-bearing |
| `DRM_FORMAT_XBGR8888` | `RGBX` | opaque |
| `DRM_FORMAT_ARGB8888` | `BGRA` | alpha-bearing |
| `DRM_FORMAT_XRGB8888` | `BGRX` | opaque |

An unsupported preferred read format falls back to ABGR8888 and is therefore
published as `RGBA`. DMA-BUF capture downloads before publication on this
path, so the wrapper exposes the normalized CPU image format rather than an
opaque DMA-BUF label.

`update_frame_alpha_state()` enumerates exactly the four normalized strings.
It does not infer alpha from spelling. Any future format becomes `None` until
its semantics are deliberately added and tested.

## Model publication and notification lifetime

Both Wayland model classes declare `pixel-format` as an internal property. It
is readable by the server-side source but is excluded from client window
metadata. Models initialize it to the empty string, then update it from the
new `ImageWrapper` whenever an image is published.

Frame-policy state has conservative class defaults and becomes per-instance on
first use. `damage()` performs a one-shot internal-property probe. If the model
exposes `pixel-format`, the source connects `notify::pixel-format` to
`update_frame_alpha_state()` and records the signal ID in the inherited
`window_signal_handlers` list. The one-shot flag is source-lifetime state, not
reusable encoder state in `init_vars()`. Normal `ui_cleanup()` disconnects the
recorded lease with every other model signal. Models which do not expose the
property pay only the first probe and retain capability-only behavior.

The first damage acquires the lease before sampling the already-published
format and before the CSC barrier or generic batching. This makes the initial
frame independent of constructor order. A later notification is synchronous
with the model update and rebinds the cached selector before the event loop can
service damage for the replacement image. Every damage samples the class and
compares the derived alpha decision with `_want_alpha`. Dimension updates
also reapply the cached class immediately after recomputing `discard_alpha`,
including resizes first discovered inside generic damage handling after that
initial sample. An unchanged steady-state sample does not rebuild the selector.
Encoder reinitialization never reads a GObject model from a protocol or codec
worker.

The bounded diagnostic emitted on a class transition contains the window ID,
format name, and `want-alpha` result. It contains no pixels or image content.

## Surface-specific ordering

### Toplevel

The native toplevel route already emits `surface-image` before `commit`.
`surface_image()` now publishes `pixel-format` before `image`; the later commit
fans out its damage rectangles. The case does not add a second full-window
damage and does not change wlroots frame acknowledgement.

### Popup

The native popup route reports commit state before it emits the captured image.
Damage in the generic commit handler would therefore be able to select coding
against the preceding frame. Popup full damage is issued instead from
`surface_image()` after both format and image have been published, and only
for a positive mapped geometry.

This preserves the native signal order rather than pretending that popups use
the toplevel order. Resize and position processing remain in their existing
owners.

### Subsurface

`SubsurfaceWindow.set_image()` stores the exact child image and its matching
internal format before the server can damage the dedicated child source. When
WSSO is composed, the standalone `subsurface_image()` compatibility entry
point remains the WIS publication route, but native WSSO ingest does not split
a generation across that event and a later commit. It emits one
`subsurface-commit`; the replacement helper first copies the borrowed
normalized raster, then calls the same `set_image()` seam to publish a coherent
image/format pair before topology exposure or child damage reconciliation.
Unlike the toplevel GObject route, the child facade does not expose two
separately observable property notifications whose relative order consumers
may depend on.

WSSO does not run the child through `WindowVideoSource` or this case's
frame-alpha selector: it uses a direct `WindowSource` only to capture exact
uncompressed RGB32 layers for a connection-owned parent-backing transaction.
Snapshot retention, content and topology generations, layer order,
sequence/ACK ownership, client staging, input routing, frame callbacks, and
child teardown all remain WSSO responsibilities.

## Client CSC readiness

An ordinary toplevel source can exist before the client has created the window
backing and sent its per-window `encoding.full_csc_modes`. A known opaque frame
may be video-eligible during that interval, but constructing a pipeline without
the backing's actual output formats is not valid.

`_client_csc_modes_resolved` becomes true when either of these terminal events
arrives:

- a client property update carries `event=map`; or
- `encoding.full_csc_modes` is present as a dictionary, including `{}`.

The map marker is used only to identify the handshake edge. Existing server
logic does not persist it as normal window state.

The candidate set is selector-aware:

- `auto`, `stream`, and `grayscale` may proceed when any common video codec has
  a usable client CSC mode;
- a concrete video encoding waits for that codec;
- a hardcoded, strict, or encoding-hint selector which resolves to a concrete
  video codec waits for that resolved codec, not an unrelated one; and
- a concrete or hinted picture encoding has no video candidate and does not
  wait.

The barrier applies only when all of the following are true:

```text
client CSC state is unresolved
and source is an ordinary mapped toplevel
and at least one selected video candidate exists
and current frame is known opaque
    or current format is unknown and there is no picture fallback
and no selected candidate has a client CSC mode
```

Override-redirect, tray, shadow, popup, and parented subsurface lifecycles do
not receive the ordinary toplevel map handshake and are not stalled by it.
WSSO negotiates its independent exact raw-composite capability against the
parent client backing; it does not borrow this video CSC barrier for children.

When the conjunction holds, `damage()` returns before batching or image
capture. Once map properties are applied, the existing sequence
`set_client_properties -> resize -> compositor flush -> refresh` performs a
fresh full-window damage with current state. No stale rectangle is retained or
replayed.

## Frame selector and rebinding

`apply_frame_alpha_state()` computes `_want_alpha` from cached state and then
calls the existing `assign_encoding_getter()`. Rebinding the callable is as
important as updating the boolean: delayed damage consults the cached selector
installed by generic compression code.

The method runs at four state edges:

- a recognized or unknown `pixel-format` notification;
- a UI-thread damage sample whose class or derived alpha policy changed;
- a dimension update after generic code recomputes `discard_alpha`; and
- generic encoding-option reconfiguration.

The format cache and bounded diagnostic change only when the normalized class
changes. Selector application also runs when the class is unchanged but its
derived policy differs. A resize updates `_want_alpha` and the cached callable
in the same dimension-update hook, before its enclosing damage can be batched
or encoded. Otherwise the steady-state damage sample leaves the cached callable
intact.

## Opaque-region and resize coherence

Generic `WindowSource` owns `_opaque_region` and derives `discard_alpha` by
comparing it with current window dimensions. Its opaque-region notification
uses the virtual `update_encoding_options()` path, so the video override must
establish the current discard result before either the generic or frame-aware
selector is assigned.

`WindowVideoSource.update_encoding_options()` therefore:

1. recomputes `discard_alpha` from the already-published region and dimensions;
2. runs the complete generic option update;
3. reapplies the cached frame class; and
4. updates video-subregion policy and performs any requested codec reload.

The repeated discard calculation is intentional and idempotent. It closes the
virtual-dispatch ordering without moving generic opaque-region ownership into
the video source.

The base `update_window_dimensions()` publishes the new dimensions and
refreshes generic discard state. The video override immediately reapplies the
cached frame class before returning to its caller. This hook is required even
with damage-time sampling: `WindowVideoSource.damage()` enters before generic
`WindowSource.damage()` discovers changed dimensions through
`may_update_window_dimensions()`. The same damage must see the new selector at
its `do_damage()` batching boundary, without waiting for another commit or
notification. Growing or shrinking across a fixed opaque region therefore
cannot use the preceding alpha policy while the image remains `BGRA` or `RGBA`.
Dimension mutation and video teardown stay in their established owners.

`update_encoding_options()` and the dimension-update hook sample no model.
They use the cached frame class; model reads remain confined to UI-owned
notification and damage paths.

## Encoding precedence and fail-closed behavior

For an ordinary known opaque frame, or an initially unknown frame whose stable
policy does not require alpha, the complete upstream selector remains
authoritative. This preserves:

- mmap and grayscale/palette special cases;
- strict requests, hardcoded choices, and window encoding hints;
- lossless window and content types;
- text and small-region picture choices;
- scrolling and video-subregion heuristics;
- size, quality, speed, congestion, and recent-update decisions; and
- lossless edge packets required by video encoder masks.

An X format permits video; it does not force H.264.

When `_want_alpha` is true, alpha safety outranks an opaque strict request,
hint, hardcoded value, or adaptive video result. The existing lossless
non-grayscale mmap transport remains the earlier authority because it preserves
the source data without choosing an opaque codec.

`get_frame_transparent_encoding()` delegates candidate preference to the
existing `get_transparent_encoding()` helper, then validates the result against
the intersection of `common_encodings` and `TRANSPARENCY_ENCODINGS`. It also
retains explicit geometry limits:

- WebP requires both dimensions in `2..16383`;
- JPEG-A requires both dimensions to be at least two; and
- the remaining negotiated transparency encodings retain their established
  encoder checks.

If bias toward the current coding produces an unusable choice, the helper is
retried once without that bias. If no usable transparency coding exists, the
method raises `ValueError`; it never silently hands alpha pixels to H.264 or
another opaque encoding.

## Captured-image admission and geometry

The cached selector is the ordinary damage planner, not the only caller which
chooses a coding. `send_regions()` can send an identified video subregion
directly; `novideo` and lossless refresh paths can select a picture through
their own helper. These branches retain their batching and region policies,
but all converge on `do_process_damage_image()` before the captured wrapper
can enter the encode queue.

At that boundary, a frame-aware source checks the actual wrapper's `RGBA` or
`BGRA` format and the existing client/window transparency policy. It preserves
non-grayscale mmap first; otherwise it reuses the negotiated transparency
selector with the wrapper's actual width and height. An unavailable or
geometry-unusable coding releases that wrapper and raises before any worker
handoff. No model read, pixel inspection, new queue, or video-pipeline cleanup
is involved.

The damage planner's options interface is an ordinary `dict`; the generic
image-processing layer has already prepared a `typedict` for codec access,
including window size, scaling, and A/V delay. Final alpha admission takes a
plain-dictionary snapshot only for the read-only planner call. It leaves the
original typed options object and every value intact for the encode handoff.
Both the frame selector and the inherited transparency selector retain their
normal dictionary contract, including its exact runtime type in Cython builds.

Generic `process_damage_image()` runs first and may intentionally replace an
alpha format with its X variant for a fully opaque region. The final check
respects that already-applied decision. It does not use a later
`discard_alpha` value or a newer model format to strip alpha from a retained
wrapper. Conversely, a captured BGRX/RGBX image remains video-eligible even if
the model has since published an alpha-bearing replacement.

Video dimension masks belong only to a video coding. Picture and mmap
handoffs retain the wrapper's exact dimensions, including odd sizes and 1x1
repairs after a pipeline with even-width/even-height constraints. Only the
video branch creates the established codec-edge regions; the frame case does
not change that branch's edge policy or codec lifetime.

## Thread and resource lifecycle

This case adds state ordering but no independent worker or resource owner:

- wlroots capture and model publication run on the compositor/UI path;
- model reads occur only in the notification and UI-thread `damage()` path;
- cached booleans and selector assignment may be reapplied by generic
  configuration without touching the model;
- delayed damage and image extraction retain the existing encode-queue
  contract, with alpha admission completed before ownership is handed off;
- the extracted wrapper format remains authoritative for alpha safety and CSC; and
- model destruction, signal disconnection, image lifetime, timers, codec
  instances, and encode-worker teardown retain their established owners.

The standalone WIS `surface_image()` and popup routes continue to replace
`image.free` with `noop` because encode workers may retain those wrappers. WSSO
does not extend that convention to its root or child callbacks: the native
emitter owns each borrowed wrapper, and the Python callback makes a synchronous
retained copy before returning. Neither case weakens the `ImageWrapper`
locking rules or introduces a zero-copy lifetime.

Generic timer closure belongs to `window-source-timer-lifecycle`. Codec-pair,
queued-image, B-frame, and video-subregion cleanup belongs to
`video-pipeline-cleanup-race`. The frame selector must remain valid when those
cases are composed with it.

## Patch-queue and integration ownership

`fix.patch` owns exactly the paths derived in `case.toml`:

- `xpra/wayland/server/models/window.py`;
- `xpra/wayland/server/models/subsurface_window.py`;
- `xpra/wayland/server/subsystem/window.py`;
- `xpra/server/window/video_compress.py`;
- `tests/unittests/unit/wayland/window_test.py`; and
- new downstream test
  `tests/unittests/unit/server/window/initial_damage_test.py`.

The new test file carries the required `Copyright (C) 2026 kogeler` notice.
The patch has no downstream dependency and must remain selectable against the
clean embedded source. In the complete stack, WSSO overlaps both Wayland model
paths and the Wayland window subsystem while VPC overlaps
`video_compress.py`; WEDT overlaps the Wayland subsystem and
`window_test.py`. Complete-stack resolution must
preserve this case's format/image/damage order, WSSO's retained generation and
composition state, WEDT's ordinary-root acknowledgement path, and VPC's video
resource lifecycle.

Responsibility is split as follows:

| Case | Owned boundary |
| --- | --- |
| `wayland-initial-window-state` | Current format publication, frame-alpha policy, CSC startup barrier, popup damage order, opaque-region/resize rebinding. |
| `wayland-subsurface-stream-ownership` | Normalized retained snapshots, stable surface identity, authoritative topology and colourspace, ordered raw RGB32 parent-backing transactions, exact packet ownership and client draw-ACK routing, atomic Cairo/OpenGL staging, native input, composite-root acknowledgement, child frame completion, and live subsurface proof. |
| `wayland-empty-damage-throttle` | Ordinary non-composite toplevel frame-callback acknowledgement, empty-damage guard, and damage/no-damage pacing. |
| `window-source-timer-lifecycle` | Generic window-source GLib timer leases and terminal close. |
| `video-pipeline-cleanup-race` | Codec, video queue, flush/watchdog, and video-subregion resources. |

Refresh the patch only through an isolated workspace and
`workspace-stage` / `workspace-update`; never edit its digest or path list by
hand.

Preserve the one-line diff context during export. The dimension-update hook
ends immediately before `cancel_damage()`, whose opening statements belong to
VPC; default three-line context would couple this otherwise independent
addition to the clean-base version of VPC's cleanup body. The same narrow
context also keeps adjacent Wayland additions independently applicable:

```bash
GIT_CONFIG_COUNT=1 \
GIT_CONFIG_KEY_0=diff.context \
GIT_CONFIG_VALUE_0=1 \
make -C fork-maintenance workspace-update \
  CASE=wayland-initial-window-state WORKSPACE=<owned-workspace>
```

Always resolve the complete stack after export and inspect the resulting
method and test-class ownership. Low-context application proves neither
correct placement nor behavioral composition by itself.

`WaylandWindowServerFrameStateTest.test_map_applies_properties_before_first_refresh`
uses `object.__new__(WindowVideoSource)` so it can exercise the real WIS
`damage()` and `set_client_properties()` wrappers without constructing codecs,
timers, queues, or a client connection. Bypassing normal construction makes
the fixture responsible for the complete lazy frame-policy slice reached by
that test: `window_signal_handlers`, `_client_csc_modes_resolved`,
`_current_frame_has_alpha`, `_frame_alpha_signal_initialized`, `has_alpha`,
`supports_transparency`, `discard_alpha`, and `_want_alpha`, plus the encoding
candidate fields used by the CSC barrier.

The fixture returns `()` from `window.get_internal_property_names()` on
purpose. The map test therefore proves the CSC/map recovery order without
also acquiring a `notify::pixel-format` lease, while `window.get()` returns the
empty current format and exercises the conservative frame class.
`initial_damage_test` separately owns the signal-lifetime boundary. The empty
property set does not bypass WIS logic: `_ensure_frame_alpha_signal()` still
runs exactly once, the initial pre-map damage is still withheld, and the
map-triggered refresh still reaches the patched base `WindowSource.damage()`.
Keep these explicit instance values when adjacent cases cause the real wrapper
to execute through the complete stack; an unconstrained `Mock` or reliance on
incidental class defaults would make the ordering assertion depend on
construction artifacts rather than the documented lazy-state contract.

## Patch ownership and non-goals

The production patch owns:

- internal current-format properties on Wayland toplevel/popup and subsurface
  models;
- format-before-image publication and popup image-before-damage ordering;
- one lazily acquired model-format signal owned by each applicable video
  source;
- explicit four-format tri-state classification;
- cached frame-aware selector rebinding;
- alpha-safe negotiated selection with geometry validation and fail-closed
  behavior;
- selector-aware initial CSC readiness and map-refresh recovery; and
- coherent opaque-region and dimension transitions;
- final captured-image alpha admission across direct video/refresh routes; and
- video-only dimension masks with exact picture/mmap geometry.

It does not:

- change the public `has-alpha` capability or add a wire property;
- inspect pixels to guess opacity;
- guarantee H.264 for every opaque frame;
- bypass ordinary adaptive selection for opaque or unknown frames;
- change mmap payload or lifetime semantics;
- change native DRM format mapping or add zero-copy capture;
- queue, poll, or replay startup damage;
- create synthetic map negotiation for non-toplevel surfaces;
- own subsurface snapshot, topology, transaction, packet/ACK, client-backing,
  input, frame-callback, or teardown behavior;
- own codec construction, cleanup, delayed B-frame, or video timer behavior;
- own the upstream `0x0` model readiness rule or empty-damage pacing; or
- special-case vkcube, glmark2, GTK, a title, or any application in production
  source.

## Regression design

`unit.server.window.initial_damage_test` builds a controlled
`WindowVideoSource` method surface without constructing real codecs. The tests
cover:

- first-damage signal acquisition, one-shot reuse, per-source independence,
  and inherited disconnection;
- absence of model reads during encoder reinitialization;
- video-only pre-map wait, map release, and explicit empty CSC resolution;
- concrete/fixed codec readiness versus adaptive multi-codec readiness;
- immediate picture, OR, tray, shadow, no-video, and parented-source paths;
- opaque video eligibility after CSC resolution;
- alpha-safe selection despite strict, hinted, or hardcoded opaque coding;
- missing and geometry-unusable transparency candidates;
- unbiased retry to a usable alpha coding;
- preservation of adaptive picture, lossless, explicit encoding, and mmap
  precedence;
- `RGBX -> RGBA -> RGBX` selector transitions;
- conservative recovery for an unknown future format;
- generic reconfiguration with an already cached opaque frame;
- full opaque-region add/remove in both directions; and
- grow/shrink transitions across a fixed opaque region, including a changed
  model size discovered by the real generic damage method before batching;
- identified video-subregion and `novideo` paths through real generic image
  processing and a real `ImageWrapper` to the worker handoff;
- exact odd and 1x1 picture dimensions after video masks were installed;
- captured-image alpha independent of a newer model format in both directions;
- an ordinary-dictionary planner snapshot with unchanged option values and
  the identical original `typedict` at the encode handoff;
- mmap without picture candidates, intentional generic opaque-region discard,
  and wrapper release when no usable transparency coding exists.

`unit.wayland.window_test` exercises the real Python subsystem behind controlled
native-extension stubs. Its 11 tests include four case-owned boundaries:

- map properties are applied before resize, compositor flush, and first
  refresh;
- `surface_image()` publishes current format before image;
- repeated popup format transitions damage only after the matching image and
  format are visible; and
- a subsurface facade receives its exact image and format before its source is
  damaged.

The map-order case uses the constructor-free fixture documented above and
patches only the generic base operations at their terminal boundary. The
dedicated initial-damage module uses a model which advertises `pixel-format`
and therefore proves the complementary signal acquisition and cleanup path.
Together they distinguish lazy frame-policy initialization from map ordering;
neither test substitutes for WSSO's atomic `subsurface-commit` and backing
transaction regressions.

The clean tests-only selection must fail non-vacuously at these behavior
assertions on the frozen source. The patched standalone selection must pass
both modules, and the same modules must pass through the complete stack after
the adjacent subsurface, timer, empty-damage, and video-cleanup cases compose.

The native `wayland` target compiles/imports the adjacent Cython boundary and
runs the complete Wayland module set. It proves that the Python ordering tests
are connected to a viable native server build; the hardware gates provide the
real compositor, codec, presentation, and pixel evidence.
The `focused-cython` mode additionally compiles the generic and video
window-source modules and runs these same captured-image regressions through
their native argument checks during development. The full Cython leg retains
final integration coverage; the Wayland-only build does not substitute for
either compiled-runtime check.

## Durable live boundary

The case declares both fixed hardware profiles:

- `live-wayland-h264-hardware`, whose title-bound opaque primary is native
  Wayland `vkcube` and whose renderer proof requires RADV; and
- `live-wayland-opengl-h264-hardware`, whose title-bound opaque primary is the
  native `glmark2-wayland` `jellyfish` benchmark and whose proof requires the
  selected render node, a live non-software AMD Mesa context, exact viewport
  placement, and changing frames.

Both profiles use `H264_CLIENT_POLICY=adaptive-alpha`, the fixed default alpha
scenario, and asymmetric endpoint CSC. Server-side libyuv converts opaque
Wayland buffers to the NV12 accepted by libva encode. Client software CSC is
disabled so libva-decoded NV12 reaches the forced native OpenGL shader. A
diagnostic with client software CSC cannot accept this case because it would
hide the client presentation boundary.

For each opaque primary, acceptance requires:

- an initial `BGRX` or `RGBX` model snapshot;
- exact per-window frame-state records with `want-alpha=False`;
- a stable interval bound to the active IDR group and saved source geometry;
- predominant H.264 main regions for the required duration and frame count;
- complete crop coverage by only the exact one-pixel lossless RGB edge regions
  required by encoder masks;
- VA-API encode and decode, packet-chain, native hardware presentation, source
  to client pixel, motion, input, ordered application exit, and cleanup proof.

The separately title-bound native-Wayland GTK auxiliary requests an RGBA
visual and draws a deterministic transparent border around an opaque
interactive region. Every saved source sample for that exact window must
contain both transparent and opaque pixels. Its packets may be only positive
WebP or alpha-bearing RGB32; H.264, RGB24, and non-alpha RGB32 are failures.
Those source screenshots are asynchronous window samples and do not replace
packet-to-frame-state correlation.

Packet completeness follows the source's allocator, not the selected codec.
The standalone WIS source retains legacy dense per-window packet IDs. With
WSSO in the complete stack, the same ordinary primary and auxiliary instead
share one connection-global allocator, even without an active subsurface.
The shared [H.264 observer](../../docs/runbooks/live-tests.md#h264-packet-sequence-authority)
binds that namespace to the frozen selection and exact live connection,
accounts for every other-window ID through the complete declared-window
ledger, and requires the sealed startup prefix to match the final history.
It never renumbers packets or accepts an unexplained gap. The primary's
frame-state, crop, VA-API, and pixel proof and the auxiliary's alpha-safe
picture proof still use their original IDs. WIS owns neither sequence
allocation nor the separate WSSO raw RGB composition ledger.

Startup layout, an isolated H.264 packet, a format log, or a fallback picture
diagnostic is not acceptance. Both profiles run first with the atomic case
selection and later with the complete stack. The separate
`live-wayland-subsurface` profile belongs to the subsurface stream case and is
not a substitute for either frame-aware hardware profile.

## Invariants not to simplify

- Keep stable `has-alpha` capability separate from current buffer format.
- Keep model `pixel-format` private and separate from the later extracted
  `WindowSource.pixel_format`.
- Classify only `RGBA`, `BGRA`, `RGBX`, and `BGRX`; unknown means conservative,
  not opaque.
- Publish format before image and before any damage which may select coding for
  that image.
- Preserve the native toplevel order and the distinct popup order; do not add
  duplicate damage to make them look alike.
- Publish one coherent subsurface image/format generation before child damage,
  while leaving normalized snapshot retention and raw transaction ownership to
  the subsurface case; do not invent a separately observable child property
  order.
- Keep the standalone WIS `subsurface_image()` publication seam distinct from
  WSSO's combined native `subsurface-commit`; sharing `set_image()` does not
  share snapshot, topology, transaction, or callback ownership.
- Acquire the format signal once, before first-damage sampling, own it through
  `window_signal_handlers`, and retain sampling on every UI-thread damage.
- Never read the model from `init_encoders()` or another protocol/codec worker.
- Treat A-formats conservatively unless existing `discard_alpha` policy is
  authoritative; never infer opacity from pixel samples.
- Recompute discard state before virtual encoding reconfiguration and reapply
  cached frame state inside the dimension-update hook before it returns.
- Rebind the cached selector together with `_want_alpha`; updating only the
  boolean is incomplete.
- Keep mmap ahead of the frame-alpha override.
- Let required alpha preservation outrank strict, hinted, hardcoded, and
  adaptive opaque coding.
- Delegate the complete upstream selector for ordinary opaque and initially
  unknown frames; opaque eligibility is not a forced codec.
- Validate transparency membership and geometry, retry without invalid current
  bias once, then fail closed.
- Revalidate coding from the actual captured image before all worker handoffs;
  video-region and refresh routing do not waive alpha admission.
- Respect alpha stripping already applied by generic opaque-region policy,
  without reinterpreting captured pixels from newer model or discard state.
- Apply video masks only to video; picture/mmap handoffs retain odd and 1x1 sizes.
- Make the CSC barrier depend on the selected video candidate set, not any
  unrelated codec's mode.
- Treat `{}` and the map completion marker as terminal information.
- Apply map properties before resize/flush/recovery refresh; do not poll or
  retain stale initial damage.
- Do not stall surface classes which never receive the toplevel map handshake.
- Preserve `ImageWrapper` and encode-queue lifetime ownership.
- Keep diagnostics to identifiers, format, and policy state; never log pixels.
- Do not absorb timer, subsurface stream, empty-damage, or codec-cleanup
  ownership into this patch.

## Required validation

Follow [development and final acceptance](../../docs/runbooks/validation.md).
During development run the nearest selector/publication regression immediately
after an atomic edit, include affected upstream and composed case modules, and
check real native/compiled behavior where relevant. Exercise the appropriate
hardware live profile early after focused/native prerequisites; full suites
are not its prerequisite. The table is final coverage, not a per-edit schedule.
After candidate freeze, fill only missing or invalidated requirements:

| Validation | Required proof |
| --- | --- |
| Clean tests-only focused run | The new selector/publication tests reach the frozen source and fail for the absent behavior, not import or fixture errors. |
| Patched standalone focused run | The complete `initial_damage_test` and `wayland.window_test` modules pass on the atomic case. |
| Patched standalone `wayland` run | The current native Wayland extensions compile/import and the complete subsystem boundary passes. |
| Complete-stack focused and `wayland` runs | Adjacent cases preserve frame, map, publication, selector, and resize behavior after composition. |
| Patch, stack, whitespace, lint, and fork-control checks | Patch digest/path authority and repository integration are exact. |
| Clean quarantine reassessment | Every assigned upstream failure is reproduced independently before patched full results are interpreted. |
| `full`, `full-cython`, `full-no-compat` | The complete queue passes all maintained upstream unit-test legs. |
| Atomic and stack `live-wayland-h264-hardware` | Real Vulkan opaque-frame H.264 and alpha auxiliary behavior both satisfy the fixed profile. |
| Atomic and stack `live-wayland-opengl-h264-hardware` | The independent native OpenGL/render-node/viewport path satisfies the same frame policy. |
| Seven complete-stack positive live profiles | Rendering, detach, transport loss, input, hardware video, lifecycle, and owned cleanup remain intact before publication. |

Retain the exact clean failure and every named patched result below
`.artifacts/fork-maintenance/`. Stop at the first unexplained failure. Any
change to format mapping, signal order, client-property timing, selector
precedence, opaque-region geometry, frame-state logging, live viewport, or CSC
role invalidates older evidence for the affected boundary.
