# Frame-aware Wayland encoding

## Boundary

A native-Wayland window has a stable public `has-alpha` capability and a
private `frame-has-alpha` value describing its latest buffer. Upstream now owns
the latter property, its publication before the image, and the single
constructor-owned notification in generic `WindowSource`. This case preserves
that owner rather than subscribing to a second property or classifying format
strings independently.

The residual patch keeps the actual readback `pixel-format` internal for
server information and hardware provenance, serializes both private properties,
and moves popup damage behind matching format/alpha/image publication. Its
ordinary-toplevel CSC barrier waits for the relevant backing negotiation and
recovers through the existing map refresh, without polling or damage replay.

A frame requiring alpha must use a negotiated, geometry-usable transparency
encoding, even when a strict request, hint or hardcoded choice requests an
opaque codec. Captured images are checked again before video masks or worker
handoff, including video-subregion and refresh routes. That check uses cached
feature/window/browser capability, not `has_alpha` narrowed by a newer model
frame: a later XRGB buffer cannot authorize losing an earlier captured ARGB
buffer's alpha. Opaque images retain the upstream adaptive selector.

Generic opaque-region notification computes discard state before virtual
reconfiguration. The video resize hook refreshes UI-owned state and rebinds the
selector before the same damage can enter batching. Encoder reconfiguration
uses cached values and never reads a GObject model on a worker.

WSSO separately owns normalized retained snapshots, surface identity/topology,
raw RGB32 transactions and atomic client backing composition. Those raw stages
bypass this case's ordinary codec/CSC policy. VPC owns video resources and the
exact edge-image fanout/handoff; WIS owns whether masks apply to the selected
coding, not a second fanout implementation.

## Embedded-source context

The current source is `d95058b0916913fe6ae5296fb702f66d833898b0`.
The earlier case was based on `212038243d0067b6860ebe7d6953692179ef353f`.
Manual reassessment on the refreshed source removes the absorbed lazy
`notify::pixel-format` lease and four-string classification. Upstream now
publishes `frame-has-alpha` before the image, connects its notification once
in `WindowSource.__init__()`, and narrows/reconfigures source alpha policy.

The source also already retains native source FourCC across readback, constructs
video candidates locally, applies client map properties to existing sources,
withholds 0x0 Wayland windows, and provides image locks and mandatory
`call_in_encode_thread(callback, *args)` ownership. These are preserved,
not reimplemented here.

The remaining necessity follows concrete current paths: the popup commit
precedes image publication, internal metadata names are unhandled, initial CSC
readiness has no barrier, fixed selectors can bypass alpha, actual captured
alpha can differ from the latest source state, picture sizes are still masked,
and opaque-region/resize selection can use the previous discard decision.
VPC's linked repair binds derived edge geometry to its existing fanout owner.

Retirement requires a code-supported replacement of every residual boundary
and preservation of its regressions. Applicability or passing tests alone
cannot establish either continued necessity or correctness.

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
| `xpra/server/window/metadata.py` | Serializes the internal current format for server information requests without adding a client window property. |
| `xpra/server/source/window.py` | Creates one source per client/window and carries map properties to the source. |
| `xpra/client/gui/window/backing.py` | Derives backing-specific `encoding.full_csc_modes`, possibly omitting values equal to connection defaults. |
| `xpra/server/window/compress.py` | Owns the existing frame notification/cache, separately cached stable alpha capability, opaque-region state, damage batching, image extraction, and encode-side `pixel_format`. |
| `xpra/server/window/video_compress.py` | Consumes cached frame policy for safe selection/CSC readiness and captured-wrapper admission; VPC owns exact video edge fanout and worker handoff. |

With this case selected by itself, the normal mapped-toplevel path is:

```text
wlroots surface commit
  -> native capture creates ImageWrapper
  -> native surface-image signal
  -> WaylandWindowServer.surface_image()
       -> model pixel-format
       -> model frame-has-alpha (existing notification updates the selector)
       -> model image
  -> native commit with damage rectangles
  -> WaylandWindowServer.commit()
  -> refresh_window_area()
  -> WindowVideoSource.damage()
       -> sample through generic update_has_alpha() on the UI thread
       -> rebind only if cached class or alpha policy changed
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

The source constructor already owns one `notify::frame-has-alpha` lease, kept
in `window_signal_handlers` and disconnected by generic UI cleanup.
Notifications rebind before replacement damage. Damage also samples through
that same generic owner; `pixel-format` is used for bounded provenance logging,
never as an independently subscribed policy input.

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

| State | Meaning | Consumer |
| --- | --- | --- |
| model `has-alpha` | Stable producer capability. | Public metadata and backing lifetime. |
| model `frame-has-alpha` | Upstream private current-buffer alpha fact. | Existing generic notification and UI samples. |
| model `pixel-format` | Exact most recently published readback format. | Information replies and bounded frame diagnostics. |
| `_alpha_capable` | Cached feature/window/browser policy before newest-frame narrowing. | Final captured-image alpha admission. |
| `_current_frame_has_alpha` | Generic cached frame property, or `None` when unavailable. | Ordinary selector and initial CSC readiness. |
| source `has_alpha` | Capability narrowed by the latest frame. | Generic encoding policy. |
| `discard_alpha` | Current opaque-region coverage of source dimensions. | Intentional alpha stripping and selector coherence. |
| `_want_alpha` | Tray/capability/client/frame policy after discard. | Cached selector. |
| `WindowSource.pixel_format` | Actual extracted image format. | CSC and pipeline setup. |
| `_client_csc_modes_resolved` | Terminal map or explicit CSC dictionary, including empty. | Startup barrier. |
| `full_csc_modes` | Advertised output formats per video codec. | Readiness and pipeline construction. |

Generic `update_has_alpha()` is the only owner of the frame cache. It computes
feature/window/browser capability first, saves that independently, then narrows
`has_alpha` only if the current frame property is false. An absent value stays
conservative. Tray policy and client transparency remain explicit consumers;
neither a frame transition nor this case mutates public `has-alpha`.

The cached ordinary-frame decision is equivalent to:

```text
alpha_capable = feature && window capability && existing browser policy
has_alpha = alpha_capable && current_frame_has_alpha is not False
want_alpha = (is_tray || (has_alpha && client transparency)) && !discard_alpha
```

The final A-wrapper check deliberately substitutes `_alpha_capable` for the
latest-frame-narrowed `has_alpha`. It does not infer opacity from pixel samples,
nor reinterpret the captured wrapper using later model/discard state.

## Native format provenance

Native `get_capture_pixel_format()` uses both read format and known source
FourCC. Readback byte order maps as follows:

| Read format | Byte order |
| --- | --- |
| `DRM_FORMAT_ABGR8888` | `RGBA` |
| `DRM_FORMAT_XBGR8888` | `RGBX` |
| `DRM_FORMAT_ARGB8888` | `BGRA` |
| `DRM_FORMAT_XRGB8888` | `BGRX` |

A known opaque XRGB/XBGR source narrows an alpha-bearing readback to its X
variant. Unsupported preferred read formats use the existing ABGR fallback;
WSSO also preserves the known source-format narrowing on that fallback and its
direct normalized capture route. DMA-BUF readback still publishes a CPU image,
not an opaque DMA-BUF token. WIS changes neither native format mapping nor
capture allocation.

The upstream model publishes the buffer's alpha fact; WIS no longer maintains
another four-string table in the video source. `pixel-format` serialization
accepts future names as provenance without assigning them new codec semantics.

## Model publication and notification lifetime

Both models declare `pixel-format` alongside `frame-has-alpha` in their
internal names, not public or dynamic client metadata. The current format starts
empty and follows the image. Generic `WindowServer.get_window_info()` enumerates
internal properties, so the serializer explicitly supports both: empty string
is the format default, true is the conservative frame-alpha default, and
ordinary `skip_defaults`, unset values and `XPRA_SKIP_METADATA` semantics
remain intact. Unknown property names still take the normal error path.

The inherited constructor connects the sole frame signal before its first
sample, retaining its exact handler ID until UI cleanup. WIS adds no signal,
timer, lease acquisition flag or new cleanup owner. A second source for the
same model owns its own handler; cleaning one must not disconnect the other.
A real GObject model and actual source constructors exercise that boundary.

On UI damage, `update_frame_alpha_state()` calls generic `update_has_alpha()`
and rebinds only when the class or desired alpha changes. Resize repeats that
sample after dimensions/discard have changed. Worker-side encoder
reconfiguration uses the already narrowed generic `has_alpha`, without a
model read or duplicate video override of `update_encoding_options()`.

The separately cached diagnostic tuple `(pixel-format, want-alpha)` bounds
the existing per-window frame log. It is diagnostic only, not a second alpha
classifier. Format or policy transitions produce one new record; unchanged
damage does not rebuild the selector or repeat it. Logs contain identifiers
and policy, never pixels or application content.

## Surface-specific ordering

### Toplevel

The native toplevel route already emits `surface-image` before `commit`.
`surface_image()` publishes `pixel-format`, then upstream `frame-has-alpha`,
then `image`; the later commit
fans out its damage rectangles. The case does not add a second full-window
damage and does not change wlroots frame acknowledgement.

### Popup

The native popup route reports commit state before it emits the captured image.
Damage in the generic commit handler would therefore be able to select coding
against the preceding frame. Popup full damage is issued instead from
`surface_image()` after format, frame alpha and image have been published, and only
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
image/format/frame-alpha generation before topology exposure or child damage
reconciliation. Both models can emit GObject notifications; consumers must not
mistake an individual notification for acceptance of a complete WSSO snapshot.
WSSO restores all three cached values and rejects the capture if publication fails.

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
and (current frame is known opaque
     or frame state is unavailable and there is no picture fallback)
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

The upstream `frame_has_alpha_changed()` callback recomputes the generic
cache and runs the normal encoding-option update. That path recomputes
`_want_alpha` and assigns the selector. WIS does not add a competing callback.

`apply_frame_alpha_state()` is the narrow cached rebinding used by UI damage
sampling and resize recovery. It updates both `_want_alpha` and
`get_best_encoding`: updating only the boolean would leave delayed damage
using the previous callable. Sampling unchanged state does not reassign it.

## Opaque-region and resize coherence

The generic opaque-region callback now publishes the region, recomputes
`discard_alpha`, and only then invokes virtual `update_encoding_options()`.
The existing generic/video option updates run once in their existing owners;
the old duplicate video discard/reapply wrapper is removed.

The base dimension hook updates dimensions, queue-size policy and discard
coverage. The video hook then samples generic alpha policy on the UI thread
(including the existing size-dependent browser policy) and rebinds before
returning to damage batching. This matters because generic `damage()` may
discover a resize after the outer video damage wrapper already sampled the
old dimensions. Growing or shrinking across a fixed opaque region must take
effect in that same `do_damage()` call, without another native commit.

The real resize callback installed by image filtering only replaces its filter;
it does not send damage before this hook returns. Encoder reconfiguration
continues to run with cached policy only and never calls these model-reading
UI hooks from a worker.

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
non-grayscale mmap transport is the earlier authority, including under a fixed
opaque override, because it preserves
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
`BGRA` format and cached feature/window/browser/client transparency policy.
The latest-frame-narrowed `has_alpha` cannot decide that older image's fate.
It preserves
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
video branch creates codec-edge regions. VPC owns their disjoint geometry,
exact wrapper dimensions and exception-safe handoff. WIS does not duplicate
that loop or change codec lifetime.

## Thread and resource lifecycle

This case adds state ordering but no independent worker or resource owner:

- wlroots capture and model publication run on the compositor/UI path;
- model reads occur only in the existing constructor/notification and UI-owned
  damage/resize paths;
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
- `xpra/server/window/metadata.py`;
- `xpra/server/window/compress.py`;
- `xpra/server/window/video_compress.py`;
- `tests/unittests/unit/server/window/make_metadata_test.py`;
- `tests/unittests/unit/wayland/window_test.py`;
- new downstream test `tests/unittests/unit/server/window/initial_damage_test.py`; and
- new downstream test `tests/unittests/unit/wayland/window_metadata_test.py`.

Both new test files carry the required `Copyright (C) 2026 kogeler` notice.
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
| `video-pipeline-cleanup-race` | Codec, video queue, exact disjoint edge fanout, flush/watchdog, and video-subregion resources. |

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
uses a constructor-free source to exercise actual map/CSC wrappers without
constructing codecs or timers. It explicitly initializes generic cached
capability/frame state, window/client policy, dimensions, content/window types
and the selected video candidates. Its model advertises no frame property,
so the initial class is conservative and a video-only pre-map damage waits.
The map marker releases that wait before the recovery refresh.

This fixture does not prove signal ownership. The dedicated initial-damage
module uses two actual source constructors and a real `SubsurfaceWindow`
GObject model, invokes its real `set_image()` publication, and observes
upstream-owned frame notifications, selector transitions, no additional
subscription on damage and independent disconnection. Codec scoring is
controlled at its terminal boundary, not by replacing the frame callback or
generic encoding-option implementation.

## Patch ownership and non-goals

The production patch owns:

- internal current-format properties on Wayland toplevel/popup and subsurface
  models;
- current-format serialization in server information replies;
- format-before-image publication and popup image-before-damage ordering;
- separately cached stable capability for captured-image admission, while
  preserving the upstream frame cache and constructor-owned signal;
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

`unit.server.window.make_metadata_test` exercises the shared serializer with
all four current formats, an unknown future format, empty/unset state,
default omission and explicit metadata suppression. Its format-value
assertions fail on clean source because the serializer drops this property.

`unit.wayland.window_metadata_test` uses real GObject toplevel and subsurface
models and the real `WindowServer.get_window_info()` method. It changes their
current format, requires the exact value and unchanged size/alpha capability
in each information reply, and rejects the metadata-error logging path. It
also verifies that the format remains internal and absent from the public and
dynamic client property lists. This module imports no native-extension stubs;
the existing publication tests retain their separate controlled native seam.

`unit.server.window.initial_damage_test` builds a controlled
`WindowVideoSource` method surface without constructing real codecs. The tests
cover:

- actual constructor-owned GObject frame notification, no additional damage-time
  lease, per-source independence and inherited disconnection;
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
- conservative recovery when frame state is unavailable;
- generic reconfiguration with an already cached opaque frame;
- full opaque-region add/remove in both directions; and
- grow/shrink transitions across a fixed opaque region, including a changed
  model size discovered by the real generic damage method before batching;
- identified video-subregion and `novideo` paths through real generic image
  processing and a real `ImageWrapper` to the worker handoff;
- exact odd and 1x1 picture dimensions after video masks were installed;
- captured-image alpha independent of a newer model frame in both directions,
  with actual narrowing of `has_alpha`, plus feature/window/browser/client gates;
- an ordinary-dictionary planner snapshot with unchanged option values and
  the identical original `typedict` at the encode handoff;
- mmap without picture candidates, grayscale policy, intentional generic opaque-region discard,
  and wrapper release when no usable transparency coding exists.

`unit.wayland.window_test` exercises the real Python subsystem behind controlled
native-extension stubs. Its case-owned boundaries include:

- map properties are applied before resize, compositor flush, and first
  refresh;
- `surface_image()` publishes current format and upstream frame alpha before image;
- repeated popup format transitions damage only after the matching image and
  format are visible; and
- a subsurface facade receives its exact image and format before its source is
  damaged.

The map-order case uses the constructor-free fixture documented above and
patches generic base operations only at the terminal boundary. The initial
damage module separately proves the real signal lifecycle. Existing
`unit.server.window.compress_test` is also required: the new generic cache
must preserve upstream capability narrowing, no-frame-property behavior and
adaptive encoding policy. None of these tests replaces WSSO's native capture,
snapshot rollback or atomic backing-transaction controls.

The clean tests-only selection must fail non-vacuously at these behavior
assertions on the frozen source. The patched standalone selection must pass
all four case-owned focused modules plus the existing generic compression module;
those modules must pass through the complete stack after
the adjacent subsurface, timer, empty-damage, and video-cleanup cases compose.

Resize controls use the actual `WindowPerformanceStatistics` owner, including
its numeric packet counter and resize history, through generic encoding-option
recalculation. Publication checks retain both the upstream `frame-has-alpha`
notification and the case's preceding `pixel-format`, before image publication.
The captured-frame control must expose the actual encode-boundary decision
(`h264` instead of alpha-preserving `rgb32` on clean source); a missing private
cache field is not its behavioral proof.

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
diagnostic is not acceptance. Both profiles run only with the complete stack
on both endpoints as members of the mandatory nine-profile live suite. The separate
`live-wayland-subsurface` profile belongs to the subsurface stream case and is
not a substitute for either frame-aware hardware profile.

## Invariants not to simplify

- Keep stable `has-alpha` capability separate from current buffer format.
- Keep model `pixel-format` private and separate from the later extracted
  `WindowSource.pixel_format`.
- Consume upstream `frame-has-alpha` through its single generic cache; do not
  recreate an independent pixel-format classifier or signal owner.
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
- Preserve the constructor-owned frame signal and its inherited disconnection;
  damage and resize resample through the same generic owner.
- Never read the model from `init_encoders()` or another protocol/codec worker.
- Keep stable capability separate from latest-frame-narrowed source alpha at
  actual-wrapper admission.
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
During an upstream refresh, finish and checkpoint the initial per-case manual
review/adaptation and the composed manual-review exit before starting the
runtime sequence below. Tests challenge that review; they cannot replace
reasoning about uncovered signal order, geometry and ownership interleavings.
During development run the nearest selector/publication regression immediately
after an atomic edit, include affected upstream and composed case modules, and
check real native/compiled behavior where relevant. Exercise the appropriate
hardware live profile early after focused/native prerequisites; full suites
are not its prerequisite. The table is final coverage, not a per-edit schedule.
After candidate freeze, fill only missing or invalidated requirements:

| Validation | Required proof |
| --- | --- |
| Clean tests-only focused run | The new selector/publication tests reach the frozen source and fail for the absent behavior, not import or fixture errors. |
| Patched standalone focused run | The four case-owned modules and existing generic compression module pass, including metadata serialization and real-model information requests. |
| Patched standalone `wayland` run | The current native Wayland extensions compile/import and the complete subsystem boundary passes. |
| Complete-stack focused and `wayland` runs | Adjacent cases preserve frame, map, publication, selector, and resize behavior after composition. |
| Patch, stack, whitespace, lint, and fork-control checks | Patch digest/path authority and repository integration are exact. |
| Clean quarantine reassessment | Every assigned upstream failure is reproduced independently before patched full results are interpreted. |
| `full`, `full-cython`, `full-no-compat` | The complete queue passes all maintained upstream unit-test legs. |
| Complete-stack `live-wayland-h264-hardware` | Real Vulkan opaque-frame H.264 and alpha auxiliary behavior both satisfy the fixed profile. |
| Complete-stack `live-wayland-opengl-h264-hardware` | The independent native OpenGL/render-node/viewport path satisfies the same frame policy. |
| All nine complete-stack positive live profiles | `live-all STACK=develop` and `live-suite-check` prove rendering, detach, transport loss, input, clipboard, subsurface composition, hardware video, lifecycle, and owned cleanup on the current queue. |

Retain the exact clean failure and every named patched result below
`.artifacts/fork-maintenance/`. Stop at the first unexplained failure. Any
change to format mapping, signal order, client-property timing, selector
precedence, opaque-region geometry, frame-state logging, live viewport, or CSC
role invalidates older evidence for the affected boundary.
