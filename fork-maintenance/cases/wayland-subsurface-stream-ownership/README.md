# Atomic native Wayland surface-tree composition

## Boundary

A native Wayland surface tree and the Xpra window protocol have different
identity and presentation models. An XDG toplevel owns one client-visible Xpra
window and one client backing, while every `wl_subsurface` is an independent
native pixel producer with its own commit, buffer, transform, scale, viewport,
damage, input region, frame callbacks, role lifetime, and stable `wl_surface`
identity.

This case flattens that native tree into the existing toplevel backing without
inventing client windows or child decoder histories. It establishes one exact
versioned contract:

- the root and every active child retain their latest normalized logical raster
  as independently owned model generations;
- each connection creates an internal `WindowSource` raw-capture owner for
  every active child but never announces the child as a wire window;
- damage to any participating layer schedules one connection-owned transaction
  for the affected rectangle of the root backing;
- the transaction serializes the intersecting layers in authoritative wlroots
  bottom-to-top paint order;
- every stage is uncompressed `rgb32` in one of four fixed packed formats and
  carries the same transaction, topology, and backing identities;
- the client applies every stage to a private Cairo or OpenGL canvas and makes
  the result visible only after the final stage succeeds; and
- packet publication, acknowledgements, callbacks, teardown, and recovery stay
  bound to the exact internal source which owns them even though the wire WID
  is the parent.

The composite mode is `premultiplied-source-over-v1`. Its alpha semantics are
part of the protocol, not an encoder preference. `BGRA` and `RGBA` carry
Wayland-premultiplied alpha. `BGRX` and `RGBX` are strictly opaque. A root with
active children is either reproduced through this complete mode or refused for
that one client; it is never approximated with independently decoded picture
or video packets.

A root with no active child remains on the ordinary Xpra path. Removing the
last child restores that path after the old child footprint has been repaired.
The case does not turn subsurfaces into toplevels, give them separate client
backings, poll their pixels, or create a new stateful child codec stream.

## Frozen source and refresh boundary

The case resolves against embedded source commit
`212038243d0067b6860ebe7d6953692179ef353f`. Its isolated resolution must
implement the complete behavior described here.

On an operator-selected upstream refresh, review the complete ownership chain
rather than judging the case by patch applicability. A replacement is complete
only if it preserves:

- stable native `wl_surface` identity across subsurface-role loss and reattach;
- authoritative topology and paint order, including children below the root;
- normalized transform, scale, viewport, XDG-geometry, alpha, and colourspace
  semantics;
- per-connection capability refusal and exact-model recovery;
- atomic private client staging with no intermediate visible state;
- backing, topology, geometry, source, and content-generation fences;
- global packet sequence allocation and exact source ACK routing;
- mmap terminal-drain behavior;
- pointer hit testing against the native leaf;
- root and child frame-callback completion;
- bounded retry, watchdog, and cleanup ownership; and
- the case-only live oracle described below.

Patch metadata, path lists, and digests must be regenerated from the complete
staged isolated workspace. They are not documentation fields to edit by hand.

## Architecture and ownership map

The implementation deliberately crosses native Wayland ingest, server window
sources, the wire packet boundary, and two client renderers. Ownership is split
as follows.

| Layer | Owned responsibility |
| --- | --- |
| `xpra/common.py` | Defines the exact mode name, four packed formats, five mandatory transaction fields, and the private client backing-state key shared by both endpoints. |
| `xpra/wayland/server/wlroots.pxd` | Exposes the wlroots buffer-source geometry and texture readback ABI needed by normalized ingest. |
| `xpra/wayland/server/wayland_surface.pyx` | Owns the process-wide `wl_surface *` registry, stable WIDs, colourspace lookup, raw texture capture, transform/scale/viewport normalization, and common surface lifetime. |
| `xpra/wayland/server/compositor.pyx`, `surface.pyx`, and `surface.pxd` | Own XDG-root wrapper publication and prebuilt-tree bootstrap, commits, effective damage, XDG geometry, root snapshots, root markers, and authoritative traversal through `wlr_surface_for_each_surface`. |
| `xpra/wayland/server/subsurface.pyx` and `subsurface.pxd` | Own one persistent child `wl_surface` wrapper, role attach/detach, nested-child discovery, atomic child commit emission, and terminal native destruction. |
| `xpra/wayland/server/models/window.py` | Owns pure-Python affine geometry, XDG canvas construction, retained root generations, and borrowed-wrapper isolation. |
| `xpra/wayland/server/models/subsurface_window.py` | Owns the internal child facade, retained normalized child generations, bounded crop access, and authoritative child colourspace. |
| `xpra/wayland/server/subsystem/window.py` | Owns native topology validation, stacking, parent relationships, snapshot-before-topology ordering, root eligibility state, root-indexed per-connection reconciliation, repair attribution, and the ordinary/composite frame-completion handoff. |
| `xpra/server/window/subsurface_source.py` | Owns one internal child capture/statistics lifecycle, exact raw-composite policy, narrow ordinary compatibility encodings, immutable geometry snapshots, parent retargeting, and suppression of child auto-refresh. |
| `xpra/server/window/compress.py` | Owns generic capture and raw packet construction, the final damage-deferral seam, content capture completion, and source-level publication calls. |
| `xpra/server/source/window.py` | Owns source sets, capability parsing, refusal/allow state, backing and topology epochs, per-root transaction state, atomic layer-snapshot capture, GLib callbacks, global packet sequences, publication leases, ACK routing, dequeue validation, mmap terminal drain, and cleanup. |
| `xpra/server/source/client_connection.py` | Owns the outgoing control/pixel queue boundary used for final packet validation and ordering. |
| `xpra/server/source/encoding.py`, `source/bandwidth.py`, `source/avsync.py`, `server/subsystem/display.py`, and `server/subsystem/window.py` | Fan generic runtime state to every pixel source while keeping toplevel-only accounting and video ownership separate; worker-side calculation borrows exact current sources through the shared lifetime owner. |
| `xpra/client/gui/ui_client_base.py` | Provides a toolkit-neutral empty backing-capability hook. |
| `xpra/client/gtk3/client_base.py` | Computes the GTK client's globally safe composite capability from every backing that can actually be selected. |
| `xpra/client/subsystem/window/manager.py` | Publishes the concrete backing capability below the `window` hello namespace. |
| `xpra/client/subsystem/window/draw.py` | Captures exact window/backing identity and epochs before handing a draw from the network thread to the decode and UI/GL boundaries. |
| `xpra/client/gui/window_base.py` | Owns window-level backing generations, transaction-bound refresh accumulation, backing replacement invalidation, and final redraw. |
| `xpra/client/gui/window/backing.py` | Validates the wire transaction state machine, keeps the transaction floor, rejects unsupported ingress, and defines atomic staging hooks. |
| `xpra/cairo/backing.py` and `cairo/backing_base.py` | Implement a private Cairo image surface, packed premultiplied source-over, and final backing swap. |
| `xpra/opengl/backing.py` and `opengl/shaders.py` | Implement a private FBO/texture generation, exact premultiplied blending, GL state preservation, and final identity swap. |
| `xpra/client/gtk3/opengl/drawing_area.py` and `glarea_backing.py` | Bind deferred GL-context work to the exact GTK backing, hand realization callbacks to the shared owner, and complete obsolete work without a context. |
| `xpra/wayland/server/pointer.pyx` and `wayland/server/subsystem/pointer.py` | Resolve parent-local wire coordinates to the current native input leaf and own leaf-local constraints and focus cleanup. |
| Focused unit modules | Bind native identity, geometry, topology, refusal, transaction, packet, ACK, renderer, input, and failure semantics. |
| `fork-maintenance/infra/live/subsurface_fixture.c` and the live runner | Provide the durable schema-6 native fixture and the independent packet/pixel/liveness/input/lifecycle oracle. |

The steady-state data path is:

```text
wl_surface commit
  -> native full-raster capture and logical normalization
  -> retained root or child model generation
  -> authoritative wlroots surface-tree traversal
  -> per-connection root reconciliation
  -> root-local repair region
  -> serial root/child raw RGB32 capture
  -> parent-WID transaction stages
  -> client private Cairo surface or OpenGL FBO
  -> final-stage atomic swap
  -> parent-WID draw ACK routed to the exact source
```

The native WID, internal source WID, parent wire WID, transaction ID, packet
sequence, backing epoch, topology epoch, geometry generation, and content
generation are separate identities. Collapsing any two of them removes a
lifetime or ordering fence.

## Endpoint negotiation

The capability is global to one client connection because any announced window
may choose a different backing later.

`UIClientBase.get_window_backing_caps()` returns no capability. A toolkit which
does not provide a concrete renderer therefore cannot accidentally claim the
mode. The GTK implementation advertises:

```text
window.subsurface-composite = ("premultiplied-source-over-v1",)
```

only when every backing selectable after hello implements that exact mode.

Cairo is always part of the decision because it is both a normal per-window
choice and the fallback when OpenGL is unavailable. If the OpenGL subsystem has
a selectable GL window class, GTK intersects Cairo's declaration with
`GLWindowBackingBase.SUBSURFACE_COMPOSITE_MODES`. A future backend-only mode is
not advertised until `xpra/common.py` defines it for the shared endpoint
contract.

Capability discovery fails closed. Fake backing mode, absence of a client
window class, an empty declaration, a malformed non-tuple declaration,
non-string values, import failure, or renderer introspection failure produces
no capability. Discovery errors are logged, but they do not abort the client
connection.

The terminal client has no composite renderer and advertises no mode. Its
backing rejects an unsolicited composite packet. This makes capability
negotiation a real renderer guarantee rather than a user-interface label.

`WindowsConnection.parse_client_caps()` reads the tuple from the `window`
namespace. Negotiation has no effect while the native root has no active child.
As soon as a child participates, the entire root must satisfy the negotiated
contract.

## Whole-root eligibility and fail-closed refusal

Eligibility is evaluated per connection and for the entire active root. It
requires:

- the exact `premultiplied-source-over-v1` client mode;
- an available parent pixel source;
- the exact unmodified `NoFilter` image-filter class for the parent and every
  child source;
- an installed `rgb32` encoder for every participating source; and
- `rgb32` in the client's core encodings.

Per-window transparency flags and the normal top-level RGB-format list are not
eligibility tests. They describe an ordinary backing, while the separately
negotiated composite mode explicitly permits alpha-bearing internal layers in
an opaque top-level backing.

Eligibility also depends on connection-independent native state:

- exactly one valid root marker and a complete authoritative tree;
- a retained root snapshot and one retained snapshot for every active child;
- complete child geometry;
- canonical root and child colourspace dictionaries; and
- exact colourspace equality across all participating layers.

A configured image filter cannot preserve the byte-defined composite canvas,
so it refuses the whole root before capture. An unavailable snapshot,
malformed topology, unknown mapped native surface, malformed colourspace, or
mixed colourspace also refuses the whole root. The server never publishes a
partial tree, reuses a previous tree merely because it looks plausible, drops
one inconvenient layer, converts colourspaces implicitly, or falls back to an
opaque codec.

Refusal is connection-local. A capable peer may keep displaying the same
native root while an incapable peer receives no root. If an already announced
root becomes ineligible, the connection atomically detaches its parent and
child sources, advances the backing and topology epochs, and sends
`WINDOW_DESTROY` for that root. If the root was not announced, no destroy is
invented.

The refusal, announcement, model, and in-progress-detach maps retain the exact
model object, not only the numeric WID. This prevents a reused WID from
inheriting another window's refusal or racing its teardown. `allow_window()`
waits only for teardown of that exact model, removes its refusal, and advances
the backing epoch before recreation. Reconciliation then performs the canonical
window announcement and a full repaint.

Late clients are reconciled before the base initial-window loop. An ineligible
root is therefore never briefly announced, and an eligible root is announced
exactly once through the normal `WINDOW_CREATE` path.

## Stable native surface identity

`xpra.wayland.server.wayland_surface.surfaces` is one strong-reference registry
for every XDG root and subsurface wrapper. Its key is the exact underlying
`wl_surface *`, the only native identity common to both roles. `next_wid()`
assigns a WID once when a wrapper is created.

The strong reference is a correctness requirement. wlroots event lists contain
listener pointers owned by the Python/Cython wrapper. The registry keeps that
wrapper alive until the matching native destroy callback has detached its
listeners and removed the entry.

A `Subsurface` separates two lifetimes:

1. the `wl_subsurface` role may be destroyed and recreated; and
2. the underlying `wl_surface` may remain alive with its committed buffer,
   transform, viewport, colourspace, nested children, and stable WID.

Destroying only the role removes the role-destroy listener, nulls the borrowed
role pointer, clears the current parent, and deactivates the child from the
composite topology. It deliberately retains the `wl_surface` commit,
new-subsurface, and destroy listeners, registry entry, retained facade, captured
generation, and WID.

Reattaching a role for that same `wl_surface` finds the existing wrapper and
reuses its WID. It may bind a different parent and restore a static committed
buffer and nested branch without requiring another child commit. Attachment
rejects a different `wl_surface`, an incompatible wrapper type, or replacement
of a still-live role.

Terminal `wl_surface` destruction is different. It removes capture-pending
state, detaches all listeners, notifies model teardown, removes the registry
entry, nulls every borrowed native pointer, clears pointer focus first where
needed, and removes active encoder sources. Descendant `wl_surface` objects
retain their own independent native lifetime and wrapper identity, but their
branch is inactive until a valid role tree reconnects it to a root.

This separation is what lets a source be torn down during role loss while the
native WID, buffer generation, and later ACK-safe replacement remain coherent.

### Discovery before visibility

Native role discovery is independent of mapping. `new_subsurface` is a
first-publication signal, not a replayable inventory: wlroots may apply a
synchronized child's cached commit before publishing that child's role to its
parent. A grandchild's role can therefore already be current when Xpra first
installs the direct child's listener. A roleless `wl_surface` can also own a
committed branch before that surface acquires either its XDG or subsurface
role.

`undiscovered_subsurfaces()` snapshots both authoritative
`current.subsurfaces_below` and `current.subsurfaces_above` lists. It includes
unmapped children, requires the native role's `added` flag, and excludes an
exact role already bound to its registered wrapper. Pending, not-yet-added
roles remain the ordinary native signal's responsibility. The native list
link is recovered through the wlroots header's `current.link` layout rather
than guessed pointer offsets. Mapped-only surface-tree traversal cannot serve
as this lifetime inventory.

`WaylandCompositor.new_toplevel()` publishes the root to Python observers
before bootstrapping that inventory. `Surface.new_subsurface()` and
`Subsurface.new_subsurface()` likewise publish and attach consumers to each
new child before discovering its pre-existing descendants. The same ordinary
notification path therefore establishes facade ownership and callbacks for
both future and prebuilt branches. An already bound exact role is a no-op:
neither a second wrapper nor duplicate native listener or Python notification
is introduced. This is a one-time bootstrap at attachment, not a per-frame
tree poll.

## Normalized Wayland pixel ingest

`WaylandSurface.capture_logical_pixels()` reads the whole current texture into
one supported four-byte packed format before any damage crop is taken.

The preferred DRM read format maps as follows.

| DRM format | Xpra packed format | Alpha meaning |
| --- | --- | --- |
| `DRM_FORMAT_ABGR8888` | `RGBA` | Premultiplied alpha retained |
| `DRM_FORMAT_XBGR8888` | `RGBX` | Strictly opaque |
| `DRM_FORMAT_ARGB8888` | `BGRA` | Premultiplied alpha retained |
| `DRM_FORMAT_XRGB8888` | `BGRX` | Strictly opaque |

An unsupported preferred format is warned once and captured through the
ABGR/RGBA fallback. The result is packed, four bytes per pixel, tightly
normalized to the surface-local logical size, marked thread-safe, and retains
the native capture timestamp.

The native ingest route does not construct a `DMABufImageWrapper` merely to
download it. The ordinary upstream route already calls that wrapper's
`may_download()` immediately, closes its duplicated native FDs, and publishes
the downloaded image. WSSO directly reads the current texture and publishes
the normalized `surface-snapshot`; neither route promises zero-copy transfer.
Native DMA-BUF FourCC/modifier planes describe the producer allocation, not
necessarily the preferred texture-readback byte order.

The shared Zed live capture boundary identifies its actual primary WID and
saved packet canvas, then requires one native read, non-None image
publication, and mapped nonempty commit in that order for that window. It
distinguishes the legacy DMA-BUF route from normalized texture readback rather
than inventing absent DMA-BUF metadata. At least one such capture must be
opaque: actual `RGBX`/`BGRX` publication for normalized readback, or actual
`XRGB8888`/`XBGR8888` producer format for the legacy route. Read bytes must equal four times the
physical read area; the published zero-origin depth-32 canvas and mapped
logical size must match the owned window. Physical and logical dimensions
remain separate because normalization may scale, transform, or pad the image.
An unrelated allocation probe, a read-attempt line alone, or a publication
after source invalidation/unmap/destruction cannot supply this proof. Explicit
native capture failure, model-copy failure, and unknown-model rejection reset
that window's pending chain. A later fresh capture may establish a new chain.

This is positive native capture/liveness evidence, not a packet-correlated
frame identity. Independent source/client pixel checks remain mandatory, as
does the exact packet/frame-state binding for H.264. A Zed H.264 session may
start with an alpha-bearing frame and later supply an opaque native capture;
this observer does not impose the hardware profiles' initial-opacity rule on
that adaptive session or replace the existing per-frame alpha policy.
The trusted host runner parses this capture evidence and classifies it before
writing the scenario report. Generic Zed collection binds the immutable report
and raw artifact hashes; it does not independently reconstruct this capture
classifier as the case-specific subsurface collector does for its own evidence.

wlroots supplies:

- the current logical surface width and height;
- the current buffer transform;
- the `wlr_surface_get_buffer_source_box()` source rectangle, which already
  incorporates buffer scale and viewport source/destination state; and
- the raw texture dimensions and rowstride.

The pure-Python `wayland_sampling_affine()` maps each logical pixel centre to a
buffer texel centre. If `u=(x+0.5)/logical_width` and
`v=(y+0.5)/logical_height`, the normalized transform component is:

| Wayland buffer transform | Source-box coordinate before scale/offset |
| --- | --- |
| normal | `(u, v)` |
| 90 | `(v, 1-u)` |
| 180 | `(1-u, 1-v)` |
| 270 | `(1-v, u)` |
| flipped | `(1-u, v)` |
| flipped 90 | `(v, u)` |
| flipped 180 | `(u, 1-v)` |
| flipped 270 | `(1-v, 1-u)` |

The 90/270 swap is intentional: wlroots renders using the inverse of the
declared buffer transform. The affine then scales into the source box and
subtracts half a texel so integer coordinates identify texel centres.

Non-identity sampling uses bilinear interpolation with edge clamping. The
common identity case returns the original captured wrapper without a second
copy. Invalid logical dimensions, transform values, source boxes, plane
layouts, or bytes-per-pixel fail instead of silently guessing.

Normalization always occurs on the full image before a partial crop. Therefore
the bytes used by a full transaction and the corresponding region used by a
tiled transaction are identical. Downstream logical and transport dimensions
are both the normalized logical dimensions; the physical buffer size is
diagnostic input and is never used to scale the pixels a second time.

## XDG root canvas and retained root generations

The Xpra toplevel backing is the XDG window geometry, not necessarily the raw
root `wl_surface` extent. On each mapped root commit, `surface.pyx` records the
XDG geometry, current surface logical size, effective damage, transform, scale,
viewport state, and the previous sampled geometry.

`xdg_root_damage()` translates surface-local effective damage into the
XDG-geometry canvas and clips it there. A geometry change, logical-size change,
transform change, scale change, viewport change, or pending recovery capture
forces full-canvas damage.

Before a required readback, the native commit emits
`surface-snapshot(wid, None)`. This advances and clears the retained model
generation before capture. A failed capture can therefore leave the client
showing its already presented frame, but it cannot label old bytes as the new
native commit.

`image_for_xdg_geometry()` then places the normalized root raster into the exact
`(gx, gy, width, height)` XDG canvas:

- overlapping pixels are cropped or shifted to the canvas origin;
- pixels outside the root `wl_surface` are transparent;
- premultiplied channel bytes are copied unchanged;
- transparent padding promotes `BGRX` to `BGRA` or `RGBX` to `RGBA`; and
- copied X-format pixels receive alpha 255, while untouched padding remains
  alpha zero.

The exact identity geometry keeps the original wrapper and resets its target
origin to zero.

The root `Window` retains a private byte copy. Replacing an image publishes its
pixel format before the retained image, treats both as one model generation,
increments a monotonic snapshot generation, and frees the previous retained
wrapper only after successful publication. Because GObject property
notification may raise after storing a value, replacement restores both
previous fields before freeing a failed candidate. Clearing the image is also
an authoritative generation and frees the old image even if pixel-format
notification fails.

`get_image()` returns a new metadata wrapper over retained immutable bytes.
Cropping or freeing that borrowed wrapper during asynchronous encode cannot
free or mutate the model-owned generation.

## Child snapshots and commit atomicity

A child commit publishes one combined native record:

- stable child WID and current root WID;
- effective mapping state and whether a buffer is attached;
- the complete authoritative root tree;
- effective child damage;
- normalized logical raster;
- logical and transport dimensions; and
- committed colourspace.

Effective `mapped` may be false because an ancestor is unmapped while the child
still has an attached authoritative buffer. The buffer is captured and retained
independently of effective visibility. A null-buffer commit clears the retained
generation.

Transform, scale, viewport, logical-size, or capture-recovery changes replace
the damage with full logical damage. If capture fails, capture-pending state
remains armed so the next successful generation is full.

`_prepare_subsurface_snapshot()` creates or updates the internal
`SubsurfaceWindow`, installs logical dimensions and colourspace, and retains the
image before topology is exposed to any connection. A dimension change clears
an incompatible image first. A failed replacement invalidates the facade
rather than retaining a half-updated generation.

Like the root model, the facade copies bytes into model-owned immutable
storage, returns independent wrappers for capture, increments a monotonic
snapshot generation on every replace or clear, and frees the prior generation
only after successful replacement. It is an internal capture facade only: it
has no client metadata, decoder, title, transient relation, presentation
lifecycle, or wire window.

The WIS-owned `set_image()` seam remains the composition point for a coherent
current image/pixel-format pair before child damage. The facade's private image
slot and internal format property do not expose a separate ordering contract.
WSSO adds retained-generation ownership around the pair; it does not redefine
the WIS frame-format policy.

## Native commit and compatibility API boundary

The authoritative WSSO root and child paths have deliberately different event
shapes from the ordinary compatibility path.

For a root, `Surface.get_surface_tree()` produces an immutable tuple containing
the exact compositor traversal and its root marker. `Surface.commit()` supplies
that tuple to `WaylandWindowServer.commit()`, whose tuple branch enters
`_commit_surface_tree()`. The tuple is therefore a protocol between the native
emitter and the topology reconciler: it is not converted to a child-only list,
and its order is not reconstructed after delivery.

For a child, the native `subsurface-commit` signal carries mapping and buffer
state, the same authoritative tree, effective damage, the normalized image,
logical and transport dimensions, and colourspace as one synchronous record.
`WaylandWindowServer.subsurface_commit()` installs or invalidates that complete
generation before topology reconciliation and completes the child frame
callback in its terminal `finally` boundary. Native WSSO emitters use this
atomic entry point; they do not split one generation across an image callback
and a later topology callback.

`commit(..., subsurfaces=list)` and `subsurface_image(...)` remain a separate
ordinary compatibility API. The list branch updates its child geometry and
uses the ordinary root refresh path; `subsurface_image()` updates one facade
and its ordinary child source independently. Those values do not contain an
authoritative root marker, committed colourspace, or a generation-wide
snapshot/topology fence, so they never create or amend a WSSO transaction.
Keeping this API intact preserves adjacent callers and tests without allowing
its weaker shape to become native-tree authority.

The concrete container type is consequently meaningful at the root commit
boundary: tuple selects the authoritative WSSO tree, while list selects the
ordinary compatibility path. New native-tree work must preserve that
distinction, and compatibility callers must not acquire composite semantics by
inference.

## Authoritative topology and stacking

`Surface.get_surface_tree()` calls `wlr_surface_for_each_surface()` and keeps the
exact mapped bottom-to-top compositor traversal order. The root marker is
preserved at the exact point where wlroots visits it, so children below the
toplevel content remain below it during replay.

Every tuple has exactly seven fields:

```text
(wid, x, y, logical_width, logical_height, transport_width, transport_height)
```

The root tuple is:

```text
(root_wid, 0, 0, xdg_width, xdg_height, xdg_width, xdg_height)
```

Child offsets are translated from root-surface coordinates into the XDG canvas
by the XDG geometry origin. Native ingest has already normalized each child, so
its logical and transport dimensions are equal at this boundary.

A mapped native `wl_surface` absent from the shared registry invalidates the
whole traversal. The consumer also requires readable seven-field tuples,
non-negative dimensions, no duplicate WIDs, and exactly one root marker. A bad
authoritative generation records a root topology error and refuses the root; it
does not partially install entries or keep a previous tree.

The server stores the exact order. It never reconstructs it from facade
creation order, dictionaries, parent links, or a child-only list. Transaction
stage creation walks this retained order, including the root at its true paint
position.

Topology replacement follows these rules:

- removed upper layers are deactivated first;
- moved or resized layers repair the union of old and new footprints;
- reorder repairs every affected footprint;
- reparenting removes the child from the old root, repairs the old root, then
  installs and repairs the new root;
- the first root-only to composite transition invalidates ordinary in-flight
  parent packets and repaints the entire backing; and
- role loss removes the active encoder source but retains the native facade and
  snapshot for a later role reattach.

A topology epoch advances on each connection whenever its root transaction
view changes. The epoch is carried by every stage and prevents a partial old
tree from becoming visible after a move, restack, detach, or reparent.

## Colourspace authority

Wayland colour-management surface state is double buffered and becomes
authoritative on commit. `WaylandSurface.get_colourspace()` returns the
committed `wp_color_management_surface_v1` description or canonical sRGB when
the surface has no explicit description or the manager is unavailable.

Before a composite is admitted, every root and child dictionary is parsed
through Xpra's `Colourspace` representation and serialized back to its canonical
form. A non-canonical dictionary is invalid. Every child must exactly equal the
root's canonical dictionary.

WSSO does not perform colour conversion between layers. A mixed-colourspace
tree is refused as one unit because source-over in different transfer functions
would not implement the advertised byte contract. A later set of matching
committed descriptions is ordinary recovery input and causes per-connection
reconciliation.

## Connection source views

`WindowsConnection` exposes deliberately different source sets.

| View | Members | Consumers |
| --- | --- | --- |
| `window_sources` / `all_window_sources()` | Client-visible toplevel sources only | Window reporting, bandwidth allocation, global delay averages, focus/fullscreen weighting, and video cleanup |
| `subsurface_sources` | Internal child sources keyed by native child WID | Child lookup, topology, exact ACK ownership, nested diagnostics, and removal |
| `all_pixel_sources()` | Snapshot of toplevel plus child sources | Generic suspend/resume, idle transitions, encoder registry refresh, quality/speed, A/V, display refresh, cancellation, and terminal cleanup |

This separation keeps a child out of wire metadata and global toplevel
statistics without leaving it on stale runtime configuration.

A parent-targeted control expands through `get_window_pixel_sources()` to the
parent and its children. A child WID selects only that child. Wildcard control
expansion de-duplicates exact source objects.

A child created after the parent has received client properties gets only this
cached backing allowlist:

- `bit-depth`;
- `decoder-speed`;
- `encoding.full_csc_modes`;
- `encoding.full_frames_only`;
- `encoding.transparency`;
- `encodings`;
- `encodings.core`; and
- `encodings.rgb_formats`.

Parent geometry, render size, workspace, fullscreen/maximized state,
presentation state, and any other parent-local field are not replayed into the
child coordinate system.

Only toplevel `WindowVideoSource` objects participate in video-context cleanup.
The child is a direct `WindowSource` and has no dummy video cleanup surface.

## Internal child source contract

`SubsurfaceWindowSource` directly subclasses `WindowSource`. It owns:

- the internal child WID;
- the retained child facade used for capture;
- child-local damage and statistics;
- current parent WID and parent-local offset;
- normalized logical and transport dimensions;
- a monotonic geometry generation; and
- generic source cleanup inherited from `WindowSource`.

It never constructs `WindowVideoSource`, `VideoSubregion`, a video codec
context, a child stream history, scrolling state, video-only timers, or an EOS
identity. Its encoder initialization calls the base `WindowSource` registry
directly.

Compatibility calls outside an active composite can use only installed common
picture encodings and can never select `scroll` or a video name. An
alpha-bearing compatibility call remains raw `rgb32` so premultiplied bytes are
not converted through a straight-alpha picture codec. Active WSSO bypasses the
normal selector entirely and requires raw `rgb32` at the final capture
boundary.

The source snapshots its seven-field geometry and geometry generation into
each encode request. Parent retargeting uses that snapshot rather than mutable
live fields. A non-mmap packet can publish only while the generation still
matches. A partial local rectangle beginning at `(0, 0)` retains its partial
destination size; it is not stretched to the child's full logical dimensions.

Per-child auto-refresh is suppressed. The root transaction owns refresh and
must replay every intersecting layer in paint order. A child-local refresh timer
could otherwise publish one layer outside that transaction.

## Damage and repair ownership

All composite repair rectangles are expressed in the parent XDG backing
coordinate system.

Child effective damage is first clipped to child logical bounds, translated by
the child offset, and clipped to the root canvas. Root damage is already
translated from the root `wl_surface` into that canvas. Multiple pending
regions merge to their clipped bounding rectangle.

Once a root has any active child, ordinary parent and child
`WindowSource.damage()` calls are intercepted by the connection's
`defer_damage` callback before normal batching. The final
`process_damage_region()` boundary performs the same deferral for paths that do
not re-enter `damage()`, including delayed damage, auto-refresh, explicit
control refresh, and decode-error recovery. Only a request already carrying
the exact composite mode may capture directly.

This ensures that all pixels affecting one repair region are rebuilt from the
same authoritative tree. The interception does not replace generic damage
bookkeeping; it consumes or cancels stale per-source work before the composite
capture begins.

Repair inputs include:

- root or child effective damage;
- child move, resize, restack, map, unmap, role loss, destruction, or reparent;
- first activation of composition;
- backing resize, map, remap, or full-refresh request;
- decode-error recovery; and
- return from refusal after state becomes eligible.

Repair scope belongs to the event owner, not merely to the rectangle of the
last changed child. Connection-local damage and geometry maintenance preserve
bounded footprint repairs; native snapshot/role reconciliation can request a
full root rebuild before that input reaches the connection scheduler.

| Entry boundary | Repair region and ownership |
| --- | --- |
| Root or child content damage with a stable tree | The clipped bounding union of pending damage, replaying every intersecting layer in current paint order. |
| A parent-side geometry/stacking update for existing roles | `update_subsurface_geometries()` merges old and new footprints, including the participating layer footprints on order changes. An explicit simultaneous full refresh still dominates that union. |
| First active child on an ordinary root | A full root rebuild fences earlier ordinary packets, so their rejection cannot leave unrelated old pixels in the new composite backing. |
| Native role attachment or reattachment through `new_subsurface()` | Publish the retained snapshot and colourspace, install the authoritative tree, then reconcile every affected root with `full_damage=True`. All newly admitted sources exist before the full-root request; a static pre-role buffer needs no extra child commit. |
| A child commit which changes the authoritative topology or its eligibility error | `_apply_subsurface_commit()` requests full reconciliation for the changed root. Its handled-source result excludes a second local-damage request for that same client/root. A stable-tree content commit does not take this branch. |
| Role loss or terminal child removal | `cleanup_subsurface_source()` removes the source/order entry and records its old footprint under the connection lock. Remaining intersecting layers repair that region after source cleanup; removal of the final child uses a root-only repair transaction. |
| Recovered/refused or newly announced root, or explicit full refresh | Full-root repair, with per-client admission and root-indexed handled/repaired results. |

Reparenting therefore clears the old footprint on the former root but may
rebuild the entire destination root: role attachment requests full
reconciliation, and a previously ordinary destination independently requires
the first-child activation reset. A full-root request is not interchangeable
with a child-local dirty rectangle. Conversely, role removal from a healthy
already-announced root does not invent a full reconciliation request merely
because its connection-owned topology epoch advances.

### Root-indexed reconciliation results

One topology generation may affect more than one root. Reparenting, role loss,
and branch removal can require an old-root repair while the same native event
installs or damages a different current root. `_reconcile_subsurface_root()`
therefore reports handled sources and can separately record successful repair
sources. The root commit retains both results per affected root; the child
commit retains the handled result it needs for damage exclusion. Neither path
unions results across the event:

- a **handled** source either received that root's full repair or must not
  receive ordinary damage for that root because reconciliation refused or
  failed it; and
- a **repaired** source is the strict subset for which a positive full-root
  damage request was successfully scheduled.

An eligible already-announced source which needs no full repair is in neither
set. A refused source or a source whose reconciliation raises is handled but is
not repaired. This distinction prevents an unavailable client path from being
treated as a positive repair; for a root-only commit, only a positive repair
can transfer completion to the ordinary `WindowSource` path.

The root commit path consults only `handled_sources_by_root[current_root]` when
deciding which sources may receive that root's explicit damage rectangles. It
consults only `repaired_sources_by_root[current_root]` when deciding whether a
successful full repair already supplies a root-only ordinary damage owner. In
active composite state that same repair is transaction input and WSSO retains
the single direct root ACK. A repair or refusal on another affected root cannot
suppress current-root damage or claim its ACK. Likewise, child damage excludes
only sources handled for the child's current root; an old root receives its own
topology repair but never consumes the new root's child damage.

This root indexing is part of the reparent and recovery contract. The old and
new canvases may converge in one synchronous event, but their damage,
eligibility, transaction, and frame-completion ownership remain independent.

## Server transaction state machine

Each wire root has one connection-owned state containing:

- `pending_region`;
- `active_region`;
- a running flag and exact token;
- the current transaction ID;
- the participating content-generation snapshot;
- one slot for each captured immutable layer image plus a readiness flag; and
- a bounded consecutive-failure count.

A damage request merges into `pending_region`. If no transaction is running,
the connection reserves a new token and schedules one owned GLib idle. The idle
moves pending to active, revalidates eligibility and dimensions, allocates a
new connection-global transaction ID, snapshots backing and topology epochs,
and builds the stage list.

Only layers intersecting `active_region` become stages. Each entry binds the
exact source object, source-local crop, destination geometry, child geometry
generation, and target parent window size. The order is the retained wlroots
paint order and may place the root between lower and upper children.

Before capture, every participating source cancels older damage. An mmap encode
which already wrote shared-ring bytes retains its terminal publication lease;
other stale work loses publication authority.

The same UI callback then borrows every intersecting image from its retained
model generation before any asynchronous stage starts. It verifies exact crop
dimensions and thread safety and revalidates backing, topology, source,
geometry, and all recorded content generations after the last borrow. Only a
complete coherent set is published into the transaction state. A commit which
arrives after that point may add damage to `pending_region`, but it cannot
replace or revoke any captured wrapper. The current transaction therefore has
bounded forward progress even while a child continuously commits.

Stages consume that fixed set serially. A stage atomically replaces its image
slot with `None` before transferring the wrapper to
`WindowSource.process_damage_region(captured_image=...)`; that path never reads
the newer live model in its place. Stage `n+1` is not started until stage `n`
reports whether its packet was actually published. Unconsumed slots are freed
on every finish, park, refusal, detach, and connection-cleanup path, while a
transferred wrapper is released only by its encode completion. This provides a
single ordered pixel-queue transaction and exactly-once image ownership without
requiring a new packet type.

The transaction finishes in one of four ways:

1. all stages publish, the state is cleared, and later damage may start a new
   transaction;
2. an identity becomes stale, or a content generation changes during the
   synchronous capture pass, so the active region is requeued without
   consuming the failure budget and a fresh transaction starts with a new
   reset;
3. capture, scheduling, or publication fails, the active region is requeued and
   the bounded failure counter advances; or
4. teardown removes the state and its callbacks, so no retry can publish.

The first failed attempt and two automatic retries are allowed. After that, a
still-pending region is parked without a callback. Fresh native or requested
damage resets the failure count and re-arms progress. This bounds a persistent
failure without making recovery require reconnecting. Content committed after
a successful capture is ordinary successor damage: it does not reset the
current attempt or its failure accounting.

## Exact wire transaction contract

Every composite stage is a normal `draw` packet targeting the parent wire WID
and parent-local coordinates. The wire coding is exactly `rgb32`. The packed
pixel format is exactly one of:

```text
BGRA, RGBA, BGRX, RGBX
```

No picture codec, video codec, scroll packet, planar format, mmap payload, or
24-bit RGB payload is valid in the mode. No compressor may be enabled. The
server forces raw RGB options, preserves premultiplied alpha, sets quality and
speed to 100, supplies only a content hint of `picture`, and prevents LZ4/Zstd
compression.
The client rejects every enabled compressor it knows.

Every stage carries all five mandatory integer fields:

| Option | Meaning |
| --- | --- |
| `subsurface-transaction-id` | Positive connection-global attempt identity |
| `subsurface-stage-index` | Zero-based index in this transaction |
| `subsurface-stage-count` | Positive fixed number of stages |
| `subsurface-topology-epoch` | Non-negative identity of the authoritative layer tree |
| `subsurface-backing-epoch` | Non-negative identity of the parent wire canvas |

Boolean values are not accepted as integers.

Stage zero alone carries:

```text
subsurface-reset = (x, y, width, height)
```

The reset is a positive rectangle wholly inside the client backing after
gravity adjustment. It clears the complete active repair region to transparent
before any layer is replayed. Later stages must omit the option.

`flush` is exact:

```text
flush = stage_count - stage_index - 1
```

All stages retain the same transaction ID, stage count, topology epoch, and
backing epoch. Indices are contiguous from zero. The final stage is the only
visibility commit.

Source-over is defined over premultiplied channels. For an 8-bit source channel
`Cs`, source alpha `As`, and premultiplied destination channel `Cd`, the live
oracle uses:

```text
Cout = min(255, Cs + round(Cd * (255 - As) / 255))
Aout = min(255, As + round(Ad * (255 - As) / 255))
```

with integer half-up rounding. A-format input must satisfy every colour channel
`<= alpha`. X-format input is treated as alpha 255 regardless of its padding
byte.

## Backing, topology, geometry, source, and content fences

A transaction may enter asynchronous publication only after all of these
identities are current:

- exact parent `WindowSource` object;
- exact child `SubsurfaceWindowSource` objects;
- parent wire WID and exact model object;
- connection backing epoch;
- root topology epoch;
- connection-global transaction ID and per-root current ID;
- child geometry generation;
- root and child retained snapshot generations during the synchronous capture
  pass;
- non-refused state; and
- open publication lifecycle.

The server records every participating retained generation, synchronously
borrows the complete immutable layer set, and revalidates that generation set
before publishing it to the stage runner. From then on the captured wrappers,
not the mutable live model generations, are content authority. A native commit
between asynchronous stages merges its repair into the coalesced successor and
the already captured transaction continues to its final stage. This avoids both
mixed-generation frames and starvation under continuous commits.

Backing, topology, source, geometry, refusal, and lifecycle identities remain
strict throughout publication because they determine where or whether the
captured pixels may be delivered. If one of those changes, packets from earlier
stages may already be in the outgoing queue, but the client never exposes them
without a valid final stage; the replacement transaction starts with another
reset. A post-capture content commit alone is not such a structural invalidation.

`client_backing_resized()` advances the backing epoch before the resize control
packet enters the queue. Map, unmap, allow-after-refusal, and backing
detach/recreation also advance it. Renderer-only render-size changes do not
change the wire-canvas epoch.

At publication, all current transaction fences are strict. At dequeue, a stage
from a transaction already committed to the ordered pixel queue may remain
valid after a newer same-backing transaction or topology repair has started.
This is intentional: outbound order makes the committed transaction precede
its repair. Exact source identity, refusal state, and backing epoch still fence
it, so a packet can never target a destroyed or recreated canvas.

Packet sequence gaps caused by stale work are valid. IDs and sequences are
never reused to make a trace look contiguous.

## GLib scheduler and watchdog ownership

Composite idles and stage watchdogs are connection-owned, not generic
`WindowSource` timers. Each registry record contains:

- exact parent WID;
- exact transaction token;
- callback kind (`idle` or `watchdog`);
- returned GLib source ID; and
- cancellation state.

The registry publishes a reservation before calling `GLib.idle_add()` or
`GLib.timeout_add()`. This handles both real asynchronous registration and test
schedulers which invoke a callback synchronously before returning an ID. If
teardown cancels the reservation while registration is in progress, a positive
late source ID is immediately removed rather than leaked.

A stage watchdog defaults to 15000 ms and has a hard minimum of 1000 ms through
`XPRA_SUBSURFACE_COMPOSITE_STAGE_TIMEOUT`. Timeout cancels the exact source
capture sequence and reports one failed stage. A completion lock makes encode
completion, timeout, synchronous callback, scheduling error, and late callback
converge on one terminal completion.

Scheduling failure parks the pending transaction in recoverable state. Cleanup
removes only callbacks owned by that connection and parent/token record.

## Packet sequence and publication ownership

### Background source borrows

`window_source_items()` snapshots ordinary-window key/object pairs under the
source-map lock. `pixel_source_operation(source)` then revalidates the exact
object against both the current window/child maps and the registered live
pixel-source set before incrementing the existing active-operation count.
Deactivation, source replacement under the same WID, and connection closing
reject new borrows. The callout runs without the connection lock, and its
`finally` releases the same source identity and wakes unregister waiters.

The delay calculator uses this lease around the complete per-source
statistics/batch/reconfigure callout and separately around each final weighted
ordinary-window batch sample. Bandwidth calculation uses the same lease for
each ordinary-window weight read and each assigned limit. Its distribution
formula and toplevel-only population remain unchanged. The source object
retained in a snapshot cannot lend its old weight or work to a replacement
which happens to reuse its WID. Standalone subsystem compositions without
WindowsConnection retain their ordinary null-context path.

Source removal already removes map membership before unregister, waits for
that exact source's active operations, then cleans its statistics and encoder
state. This also protects internal children, whose removal does not emit the
toplevel removal signal. No new worker registry, signal-order dependency,
recalculation schedule, or connection-wide producer fence belongs here. The
separate VPC case owns closing that producer at full-connection shutdown.
Borrowed statistics/reconfigure callouts must not synchronously remove their
own source: removal waits for the borrow, as it does for existing ACK and
publication callouts. The supported worker-versus-UI removal path is the
concurrency boundary exercised by the regression.

### Packet publication leases

Every parent and child source on one connection uses one monotonic damage packet
sequence allocator. This is necessary because child packets are retargeted to a
parent WID, so source-local counters alone could produce the same
`(wire_wid, sequence)` pair for different producers.

The connection keeps an exact active-source registry:

```text
id(source) -> source
```

and an ACK-owner map:

```text
(wire_wid, sequence) -> exact source
```

Publication is a transaction under the damage-packet condition:

1. claim an active-operation lease for the exact source;
2. validate source, parent, refusal, backing, transaction, topology, geometry,
   content, encoding, and format state;
3. insert the ACK owner;
4. publish the source's pending-ACK statistics;
5. append the packet to the outgoing pixel queue; and
6. release the active-operation lease.

If owner insertion, source accounting, or append fails, owner and pending-ACK
state roll back together. Once queue append succeeds, diagnostics,
`save_update`, aggregate statistics, or protocol wakeup failure cannot
retroactively unpublish the packet or lose its ACK owner.

A child publication log binds both identities:

```text
subsurface draw packet sequence N from source window 0xC
published as wire window 0xP using rgb32
```

The client ACK still names the parent WID. The connection removes the exact
owner and invokes `damage_packet_acked()` on that source outside the connection
lock while retaining an active-operation lease. Connection statistics use the
wire WID; source statistics remain on the child WID. The routed log is:

```text
draw acknowledgement sequence N for wire window 0xP
routed to subsurface window 0xC
```

Unknown, duplicate, or late ACKs are ignored. Unregistering a source prevents
new claims, waits for active publication/ACK operations, and removes only that
source's owner entries, pending ACK statistics, and terminal leases before
source cleanup.

### Ordinary-window H.264 evidence and sequence namespaces

The allocator belongs to `WindowsConnection`, not to the composite encoding.
It therefore also assigns connection-global packet IDs when every source is
an ordinary toplevel and no subsurface tree is active. In a two-window hardware
session, an auxiliary picture packet can occupy an ID between two primary
H.264 packets, or between two packets of one primary damage group. The
primary's projection is strictly ordered but need not be numerically dense.
Neither source nor observer may renumber the real packet IDs to make it dense.

The shared H.264 live observer distinguishes that namespace from the legacy
per-window allocator. Its authority is the frozen selected source together
with an actual single owned connection: the selected cases establish the
expected allocator, while the owned run, server UUID, client UUID, session ID,
connection time, and endpoint bind the one active connection. A client UUID
may recur across runs; neither it nor an info-section index alone identifies
this session. The initial `damage.next-packet-sequence` /
`damage.ack-owners` values corroborate the namespace, not a later packet
frontier or final drain.
Missing or contradictory runtime identity is not permission to select the
legacy interpretation. Conversely, an approved source without this case keeps
the legacy dense per-window contract; numerical gaps alone never enable the
global interpretation.

Connection-global validation retains one exact ledger for every declared,
title-bound ordinary window on that connection. Each row binds the original
sequence and source WID to its saved metadata path and metadata/payload
digests and length. Every gap in a primary projection must be explained by
the exact intervening packets of another declared window in that ledger.
Duplicate IDs, undeclared producers, missing primary packets, and unexplained
connection-wide holes fail rather than becoming tolerated gaps. The
production ability to consume an ID for cancelled stale work is not a licence
to omit an unaccounted ID from this controlled workload's acceptance evidence.

A sealed startup prefix supplies a bounded observation boundary while the
producer remains active. Final acceptance must bind that unchanged prefix to
the complete retained history and satisfy all applicable client draw, ACK,
and terminal checks; a passing prefix is not a substitute for final
accounting. The startup counters cannot establish a final next-ID equality
or zero outstanding ACKs. Such a claim would require its own fresh quiescent
observation, not extrapolation from namespace discovery. Existing per-window
codec, frame-state, VA-API, client-packet, crop, and pixel checks continue to use
the actual packet IDs. Storage bucket indexes and the descending `flush`
countdown remain exact even when another window interleaves wire IDs.

This is the shared ordinary-root H.264 observer's responsibility, documented
in the [live runbook](../../docs/runbooks/live-tests.md#h264-packet-sequence-authority).
It does not replace the case-only RGB composition ledger: that ledger still
binds internal source IDs to parent wire IDs, complete transaction stages,
epochs, premultiplied pixels, and source-routed ACKs. WIS owns frame-alpha
selection, not either sequence allocator or ledger.

## Outgoing dequeue and mmap terminal drain

Control packets retain priority over pixel packets. When a pixel packet is
dequeued, the connection revalidates it. A stale non-mmap packet is dropped,
then the queue checks again for newly available control work. The returned
`more` flag is computed from the actual remaining queue rather than copied from
a cancelled batch.

Active composite transactions never choose mmap, and the client rejects mmap
inside the composite mode. Generic `WindowSource` work may nevertheless have
written a shared-ring region just before a topology change, refusal, or source
detach invalidates its packet.

Those bytes cannot simply be dropped: the client's mmap read pointer must
advance or the shared ring remains occupied. A publication lease retains the
source until the descriptor reaches a terminal packet. The stale mmap packet is
retargeted to WID `-1`, carries no paint or ACK owner, and is delivered only so
the client consumes and releases the chunks. Packet-construction failure after
a successful mmap write uses the same terminal-drain route. A stale mmap found
at dequeue is likewise converted to the unknown-window drain form and cannot
promise a cancelled successor.

This terminal exception does not admit mmap into WSSO. It closes an already
performed generic transport side effect.

Packet vocabulary and mmap descriptor layout remain upstream protocol owners.
Publication, dequeue filtering, and their tests use `WINDOW_DRAW` from
`xpra.net.packet_type`: it resolves to the legacy `draw` name only when
backwards compatibility is enabled, and otherwise to `window-draw`. A
no-compatibility build must not recognize a fabricated legacy packet merely to
exercise the filter. The mmap encoder always publishes the exact descriptor
in `options["chunks"]`; only compatibility builds also duplicate those chunks
in the packet's data slot. Without compatibility that slot is empty bytes.
Terminal drains preserve this active-mode layout while changing only the wire
target and ownership, and the regression checks both the exact chunks and the
mode-specific payload in the ordinary and no-compatibility test legs.

## Client ingress and backing identity

`WindowDraw._process_draw()` runs on the network/main packet boundary. Before
queueing decode work it captures:

- the exact client window object;
- the exact backing object;
- the window's monotonic backing generation; and
- the backing's local reconfiguration epoch.

`_do_draw()` revalidates them before decode or paint. Stale identity produces a
failed draw ACK and no paint. For a composite stage, it overwrites the private
`_client-subsurface-backing-state` option with the captured
`(backing, generation, local_epoch)` tuple. A server-supplied value at that key
has no authority.

The backing revalidates the tuple again in the UI thread or active GL context,
where pixels can actually change. This closes the gap between decode queueing
and renderer execution.

`ClientWindowBase.new_backing()` increments the window backing generation even
when the backing factory reuses the same Python object and dimensions. It
invalidates private staging and assigns the new generation to the backing.
Backing init, backing-size change, render-size change, and close increment the
local epoch and invalidate staging. Window destruction does the same before the
backing disappears.

Composite packets bypass only the generic unsupported-encoding rejection so
the backing can invalidate an existing private transaction itself. They do not
bypass transaction validation. Malformed mmap ingress consumes its descriptor
before returning a failed ACK. Unknown mode, wrong coding, compression,
unsupported packed format, planar data, void data, or a synchronous draw error
invalidates the relevant private transaction.

A reported paint fault follows this same failure path and ACKs failure. The
intentional silent-paint fault sends no ACK; the server stage watchdog bounds
that condition.

## Client transaction state machine

`WindowBackingBase` validates every stage before renderer mutation.

A new transaction is accepted only when:

- the exact mode is present;
- wire `rgb32` has reached the packed-RGB painter as internal encoding `rgb`;
- the format is one of the four fixed packed formats;
- no compressor is enabled;
- all five exact integer options are present;
- transaction ID and stage count are positive;
- stage index, topology epoch, and backing epoch are non-negative;
- stage zero is the first observed stage;
- stage zero alone has a valid reset rectangle;
- `flush` is the exact remaining-stage count;
- epochs are not older than the backing's accepted epochs; and
- transaction ID is above the completed/rejected transaction floor.

The first stage copies the visible backing into private staging, then clears
the reset rectangle there. Subsequent stages require the same transaction ID,
stage count, topology epoch, backing epoch, and local backing epoch, with the
exact next contiguous index.

A higher transaction invalidates and replaces incomplete older staging. A
duplicate, skipped, out-of-order, stale, malformed, or post-failure stage is
rejected. The floor prevents an old transaction from being resurrected after
its staging was discarded.

The visible backing remains unchanged throughout non-final stages. The client
accumulates the union of the first-stage reset and every painted stage as the
transaction presentation region. Completion of the final stage performs the
renderer's irreversible swap, advances the transaction floor, and only then
allows redraw of that complete region.

Ordinary RGB, picture, video, scroll, or void paint invalidates private
composite staging before changing the visible backing. Backing reconfiguration,
decode failure, renderer failure, reported paint fault, close, and destroy also
discard it. A malformed final stage therefore cannot expose a prefix of the
tree.

## Cairo staging and commit

`CairoBackingBase` advertises the mode and owns
`_subsurface_staging_surface`.

On stage zero it allocates a full-size Cairo `ImageSurface` with the visible
backing's format and copies the visible backing into it with `Operator.SOURCE`.
It clears the reset rectangle and applies every layer with `Operator.OVER`.

A-format bytes are supplied as Cairo ARGB32 premultiplied input. X-format bytes
use RGB24 semantics, so the undefined X byte cannot become alpha. Scaling uses
a Cairo surface pattern with edge padding; it does not route through
`GdkPixbuf` straight-alpha conversion.

The final stage flushes the staging surface, detaches it from private state,
and swaps it into `_backing`. Finishing the previous visible surface happens
after that irreversible publish point. A failure there is logged but does not
turn a successfully visible transaction into a failed ACK which would provoke
a conflicting replay.

Discard and close detach the staging surface first and attempt to finish it
even when cleanup raises. Invalid bounds, rowstride, payload length, surface
creation, scaling, or Cairo paint fails the stage without modifying the visible
surface.

## OpenGL staging and commit

`GLWindowBackingBase` advertises the same mode and owns a private temporary FBO
and texture generation.

Stage zero copies the visible offscreen framebuffer to the temporary target.
The reset clears the exact top-origin backing rectangle to transparent using
GL scissoring. Clear colour, scissor enable, and scissor box are saved and
restored.

Each A-format layer uploads to `GL_RGBA8` even when the visible destination is
opaque, preserving child alpha independently of the root window capability.
Each X-format layer uploads to `GL_RGB8`, which supplies alpha 1.0.

The byte-defined canvas format is separate from the ordinary backing's
configured `internal_format`, `bit_depth`, and native context/visual. Stage
zero ensures its scratch texture uses `GL_RGBA8` for an alpha-capable backing
or `GL_RGB8` for an opaque backing. Both texture/FBO identities carry their
actual storage formats through the final swap. A persistent composite canvas
therefore never accumulates layers through `RGB10_A2`'s two-bit alpha, `RGBA4`,
or another reduced-precision intermediate representation. Final presentation
still uses the selected native output context; this does not force an
ordinary 30-bit window or the display itself to become 24-bit.

A first partial reset retains the *actual previous backing pixels* outside its
region. It cannot reconstruct byte values already quantized by an earlier
ordinary backing. Replayed reset regions become canonical byte composites;
subsequent partial transactions preserve that byte canvas outside their reset.
The server's first topology activation independently requests a full-root
rebuild. Resize preserves the active canvas storage format while copying its
pixels and recreating the scratch texture. Aborting a stage restores the
previous visible FBO and its exact format; scratch reallocation cannot publish
a new canvas by itself.

An ordinary packed, planar, NVJPEG, or scroll paint ends this mode-specific
storage ownership. Before its depth-owned update, the existing scratch FBO is
allocated in the configured format, the visible canvas is converted there,
and the normal identity swap installs that ordinary backing. Both FBO formats
are then restored to the ordinary policy. No third framebuffer, new visual,
capability withdrawal, or alternate wire representation is needed. Close
clears both resource and format identities. The real mapped regression binds
16/30-bit entry, partial replay, aborted entry and successor, ordinary packed
and scroll transitions, reentry, resize, and close, without treating output
quantization as an intermediate compositing operation.

The premultiplied shader clamps sampling coordinates to source pixel centres.
Blending uses:

```text
source factor      = ONE
destination factor = ONE_MINUS_SRC_ALPHA
```

for colour and alpha. Blend enable, blend equations, blend functions, scissor
state, framebuffer/texture bindings, clear colour, and transient upload objects
are restored or released on both success and error.

The final commit swaps the temporary and visible FBO/texture identities. That
identity swap is the irreversible publication point. Later presentation or
accounting errors are logged but do not report the committed draw as failed.
Discard restores the visible offscreen target and releases private state.

For direct presentation, including an unscaled single-buffered context, the
final stage queues the complete transaction presentation union rather than its
own layer rectangle. Partial framebuffer blits interpret `(x, y, width,
height)` as dimensions and convert both source and destination to endpoint
coordinates. Reset pixels and earlier layers therefore become visible in the
same presentation as the final layer.

Planar data is never accepted in the mode. A-format source alpha is not
discarded merely because the destination texture is opaque, and X padding is
not interpreted as alpha.

`GLWindowBackingBase` also owns every callback waiting for a GTK GL context.
The GLib idle thunk captures the exact backing widget which scheduled it; a
replacement cannot transfer that work to another widget. Work queued before
realization is stored once in the shared base rather than in either GTK
implementation. Realization drains that list through the current context.
Backing replacement or close instead completes every detached callback exactly
once with `context=None`, after the backing has become unusable, and a later
call against a closed backing receives the same immediate terminal result.

The `None` result follows the ordinary renderer failure callback: matching
private composite staging is discarded and the draw receives a failed ACK. It
is not silently migrated into a new GL object or a Cairo backing. A subsequent
backing selected through normal client policy begins with its own epoch and
clean staging. Both GTK OpenGL backends delegate queueing and drain ownership
to this common base so their close and pre-realize semantics cannot diverge.

## Redraw and client error semantics

`ClientWindowBase` keeps composite redraw accumulation separate from ordinary
pending damage. The accumulator is bound to the exact backing object, backing
generation, local epoch, and transaction identity.

Successful non-final stages add their regions but schedule no visible repaint.
The successful final commit drains the complete accumulated set through the
normal window repaint boundary. A failure clears only the matching composite
accumulator. A delayed callback from an older backing or transaction cannot
clear or publish a newer one.

An ordinary successful draw invalidates private staging and follows the
ordinary refresh accumulator. This ordering prevents a later final composite
callback from overwriting pixels committed through another packet family.

After the Cairo/GL identity swap, errors from disposal, presentation,
instrumentation, or repaint scheduling are post-commit diagnostics. They cannot
roll visible state back, so they also cannot produce a failure ACK for the
already committed transaction.

## Pointer hit testing and native focus

Wire pointer packets continue to name the root WID and carry XDG-backing-local
coordinates. They are not rewritten to an internal child WID.

Every adjusted pointer packet calls `enter_surface()` for the root XDG surface.
The native path adds the XDG geometry origin and calls
`wlr_surface_surface_at()`, which applies the current mapped topology and input
regions and returns the exact leaf `wl_surface` plus fractional leaf-local
coordinates.

The shared registry maps that native pointer back to its stable WID. An
unregistered result clears focus; it is never guessed from geometry.
`pointer_focus` stores the internal native leaf WID only for server-side
lifecycle. The normal wire and synchronized peer event remain rooted at the
toplevel.

The pointer device recomputes its signed offset on every packet:

```text
offset = leaf-local coordinate - parent-local coordinate
```

A desynchronized move or role reparent therefore does not need a new wire
window or stale cached geometry. On target change, the seat receives leave/
enter and the new leaf's pointer constraint becomes active. Relative deltas
remain derived from the stable parent stream. A locked constraint suppresses
absolute motion; a confined constraint clamps in leaf coordinates and converts
back to parent coordinates.

Motion, buttons, and wheel events are delivered to the focused leaf and flush
the compositor. Focus is cleared before role detach, topology removal, native
destroy, or pointer-device cleanup. A leave or compositor-flush error is logged
without preventing identity invalidation.

Only an actual target change emits the high-level diagnostic:

```text
Wayland pointer target root=0xP surface=0xC local=X,Y
```

## Root and child frame callbacks

Frame callbacks belong to native surface commits, not to an arbitrary Xpra
connection.

A child `subsurface_commit` synchronously installs or invalidates the complete
generation, topology, and per-connection reconciliation first. Its `finally`
path then calls `frame_done()` once on the same still-live child surface and
flushes the compositor. The count is independent of the number of Xpra peers
and remains one even when snapshot or reconciliation work raises. The native
emitter frees its borrowed image only after all synchronous observers return.

For a root with active composition, each connection redirects non-empty damage
into its own transaction, so generic `WindowSource.send_delayed_regions()`
cannot own the native root callback. The Wayland window subsystem acknowledges
the root commit once after all connection damage has been redirected. It does
not acknowledge once per peer.

Mapped root completion follows one exact ownership matrix:

| Root state | Empty/damage state | Completion owner |
| --- | --- | --- |
| Root-only | No explicit damage and no successful current-root repair | The optional WEDT `schedule_empty_damage_ack()` delegate; without WEDT, the ordinary immediate fallback |
| Root-only | Explicit damage delivered to an eligible source or a successful current-root full repair | The ordinary `WindowSource` damage path; explicit fanout follows cancel/mark, while a reconciliation repair remains the sole source request and ACK owner |
| Active composite | Empty and no successful current-root repair | One direct root acknowledgement after reconciliation, independent of peer count |
| Active composite | Explicit damage or a successful current-root full repair | One direct root acknowledgement in the commit adapter's `finally` boundary after every peer has received its transaction input |

The first row includes a root-only generation for which a client was refused or
reconciliation failed without scheduling a repair: handled-without-repair does
not invent a `WindowSource` completion owner. Conversely, a successfully
scheduled repair prevents both empty pacing and a second ordinary damage
request for that source.

WSSO calls the optional empty-damage delegates only at these integration seams.
It does not own their timer, delay, coalescing consumer, or cleanup policy. The
server-level cancel and model-level pending mark happen once before explicit
ordinary root damage fanout, not once per connection. Reconciliation prepares
that same ownership before the first full-repair request, so even a
synchronous source completion observes the guard in the correct order. If no
peer accepts any repair, rollback clears only a guard acquired by this attempt;
it cannot clear an older outstanding damage owner. The successful
repaired-source identity suppresses a duplicate explicit request.
Composite commits cancel any ordinary empty timer, do not mark the ordinary
model guard, and complete the native root exactly once even when several Xpra
peers receive independent transactions.

## Cleanup and terminal ordering

Source and connection cleanup use the same identity locks as publication.

Deactivating a child first removes it from active topology and source maps,
advances topology state, and captures its old footprint. It unregisters packet
publication, waits for active publish/ACK operations, removes owner and lease
state, and runs source cleanup. Only then is the old-footprint repair scheduled.
This prevents a terminal child packet from racing the repair which erases it.

Parent detach or refusal:

- marks the exact model as detaching;
- removes the parent and all of its child sources from active maps;
- advances backing and topology epochs;
- cancels only the parent's owned composite idles and watchdogs;
- unregisters every source before cleaning any of them;
- attempts all source cleanups even if one raises;
- sends `WINDOW_DESTROY` only when that exact model was announced; and
- wakes exact-model waiters after the detach boundary is complete.

Connection cleanup closes publication first, snapshots all pixel sources,
clears source, owner, transaction, refusal, announcement, epoch, topology, and
callback registries, waits for active operations, cancels remaining owned GLib
sources, and cleans every source. The first error is re-raised after later
errors have been logged, so one faulty child cannot leak all later children.

Native callbacks are `noexcept` boundaries. They catch observer failures and
still detach listeners, remove registry entries, clear pointer focus, null
borrowed pointers, release captured wrappers, and flush or finalize as far as
their ownership permits.

Cleanup never sends child EOS. There is no child stream identity at the client
which an EOS packet could terminate.

## Diagnostics and privacy

Connection info exposes:

- next global damage packet sequence;
- number of exact ACK owners;
- number of active pixel sources;
- number of roots with queued successor regions (`subsurface-pending`) and
  roots with a running captured transaction (`subsurface-inflight`);
- packet and encode queue state; and
- each child nested beneath its parent with child WID, parent WID, offset,
  dimensions, encoding state, pending ACKs, and packet statistics.

The two composition counts are sampled under the existing publication lock.
A parked successor remains pending even with no GLib idle, encode work, or ACK
owner; a captured transaction remains in flight before its first packet.
Consequently empty packet queues alone do not establish compositor drain.

`WindowsConnection.get_window_info()` combines two producers of the same
`damage` namespace. `GlobalPerformanceStatistics` supplies historical counts,
queue distributions, latency, and encoding/connection statistics; the window
connection supplies current queues and exact source/transaction ownership.
Assembly starts with the statistics result, then augments its nested queue
maps and adds the locked ownership snapshot. Neither producer may replace the
other's entire `damage` dictionary. In particular, a historical queue sample
(`cur`, `max`, or an average) does not replace the instantaneous `current`
queue length, and neither is a substitute for the pending/in-flight composite
counts. The focused regression uses the real statistics implementation with
nonzero history and verifies both namespaces remain visible before and after
child removal.

Publication and acknowledgement logs explicitly bind internal child identity
to parent wire identity. Pointer logs bind the root to the native leaf only
when the target changes. Refusal logs carry the root and structural reason.
Default logs do not include raw pixel bytes.

The durable live gate retains deterministic fixture-owned data below the
private artifacts boundary: schema-checked fixture JSON, server/client logs,
`xpra info` snapshots, `save_update` metadata, bounded raw fixture payloads,
the continuous active/drain record, SHA-256 bindings, XWD/PNG client captures,
and the final report. These are generated from the fixed synthetic fixture,
never from an arbitrary operator window. Raw payloads are authority only inside
that private bounded result tree.

## Adjacent-case responsibility map

WSSO shares files with other active production cases, but their semantic
ownership must remain separate.

### `wayland-initial-window-state`

WIS owns publication of the current Wayland image and pixel format before
damage, frame-aware alpha state, CSC readiness, and ordinary frame encoding
selection. WSSO consumes that model seam, adds retained snapshot generations
and a raw composite transaction, and intentionally bypasses ordinary
frame/video selection while a child tree is active.

Do not move WIS's opacity, CSC, or general encoding policy into the composite
transaction. Do not make WIS own topology, parent retargeting, transaction
epochs, client staging, or child ACK routing.

### `window-source-timer-lifecycle`

The timer case owns generic named `WindowSource` and icon/damage/refresh/
decode-error/A-V timer leases, early dispatch, terminal closure, and cleanup
ordering. Internal child sources inherit those generic boundaries where the
base API remains reachable.

WSSO suppresses child auto-refresh and separately owns only
connection-scoped composite idles and per-stage watchdogs. Its callback registry
must not be folded into generic source timer fields, and generic timer cleanup
must not cancel another connection's composite transaction.

### `video-pipeline-cleanup-race`

VPC owns codec-pair publication, video encoder/CSC cleanup, `VideoSubregion`,
delayed video images, B-frame flush, inactivity timers, video EOS, and
toplevel `WindowVideoSource` teardown. It also owns the connection-wide
`EncodingsConnection` calculator admission and execution fence, CUDA context
lifetime, and encode-queue shutdown. Closing first stops new calculator work
and drains an executing calculation before `WindowsConnection` releases its
sources. The encode queue remains open for mandatory cleanup tails and is
sealed only after those tails have been accepted; shared resources outlive
their final queued consumer.

WSSO owns the narrower lifetime of each current pixel source. Packet
publication, ACK handling, and background calculation or bandwidth callouts
borrow that exact source through the existing window-source lock and active
operation counter. Removal deactivates its identity before waiting for those
borrows, including removal of a child while the connection remains open. The
connection-wide VPC fence cannot substitute for this per-source boundary;
conversely, WSSO's leases do not stop a connection's calculator producer or
define when its shared encode queue and CUDA context may close. The calculator
body uses WSSO's optional borrow hook, while VPC owns the surrounding scheduling
and execution lifecycle, so the two cases remain independently selectable.

A WSSO child is directly a `WindowSource` and never owns any of those objects.
An active composite forces the root and children through the base raw capture
and packet-construction path for that transaction. Root-only toplevel video
behavior remains VPC authority.

### `wayland-empty-damage-throttle`

The empty-damage case owns the generic mapped-empty commit guard, bounded
pacing/coalescing timer, consumer replacement, and terminal timer cleanup.
WSSO owns the commit-classification adapter around that policy. A root-only
empty generation with no successful repair delegates to WEDT's scheduler. A
root-only explicit damage cancels the WEDT entry and marks the model frame
pending before ordinary source fanout. A successful reconciliation repair is
recorded separately, cancels and marks after it is scheduled, and remains the
only ordinary request and ACK owner. An active composite cancels the ordinary
entry and acknowledges the root exactly once after transaction input has been
distributed. WSSO also owns the exactly-once child commit callback.

The delegate lookup is optional so the WSSO case remains independently
selectable: without WEDT, only the root-only empty case uses the ordinary
immediate acknowledgement fallback. With the complete stack, WEDT remains the
sole owner of timer allocation, replacement, firing, and terminal cancellation;
WSSO never stores a duplicate timer or consumer.

Normalized native damage is an immutable tuple; the standalone upstream/WEDT
path may supply a list. Both use sequence emptiness, not the container type,
to select this handoff. The shared live log observer consequently recognizes
the exact empty `rects=()` and `rects=[]` fields, retaining mapped-state and
exact-window guards. Positive damage requires a valid nonempty rectangle
sequence; malformed text is not evidence of either state. This representation
choice changes neither WEDT's pacing policy nor the number of native callbacks.

Do not implement an empty-damage timer inside WSSO, use a composite watchdog as
a frame-pacing timer, or make child transaction retries acknowledge unrelated
native commits.

### `wayland-client-keymap-sync`

The keymap case owns the client-supplied RMLVO contract, native keymap/device
lifecycle, group-aware translation, modifier and repeat state, shared-session
focus-source selection, and readonly keyboard policy. WSSO shares the Wayland
window and pointer subsystems, plus the native declarations in `wlroots.pxd`,
only to preserve one rooted wire window while resolving each pointer event to
the current native leaf.

Do not infer keyboard ownership from WSSO's internal pointer focus, retarget a
keyboard packet to a child stream, or make surface-tree reconciliation replace
the keymap case's focus-source and readonly hooks. Conversely, keymap refresh
must not rebuild WSSO topology, alter stable native surface identities, or own
the root-to-leaf pointer coordinates.

## Patch responsibility

The final case patch owns one atomic native-tree-to-client-backing behavior.
Its production responsibility is grouped as follows.

Native Wayland ownership:

- `xpra/wayland/server/wayland_surface.pyx`;
- `xpra/wayland/server/wlroots.pxd`;
- `xpra/wayland/server/compositor.pyx`;
- `xpra/wayland/server/surface.pyx`;
- `xpra/wayland/server/surface.pxd`;
- `xpra/wayland/server/subsurface.pyx`;
- `xpra/wayland/server/subsurface.pxd`;
- `xpra/wayland/server/models/window.py`;
- `xpra/wayland/server/models/subsurface_window.py`;
- `xpra/wayland/server/subsystem/window.py`;
- `xpra/wayland/server/pointer.pyx`; and
- `xpra/wayland/server/subsystem/pointer.py`.

Server stream ownership:

- `xpra/server/window/subsurface_source.py`;
- `xpra/server/window/compress.py`;
- `xpra/server/source/window.py`;
- `xpra/server/source/client_connection.py`;
- `xpra/server/source/encoding.py`;
- `xpra/server/source/bandwidth.py`;
- `xpra/server/source/avsync.py`;
- `xpra/server/subsystem/display.py`; and
- `xpra/server/subsystem/window.py`.

Client protocol and renderer ownership:

- `xpra/common.py`;
- `xpra/client/gui/ui_client_base.py`;
- `xpra/client/gtk3/client_base.py`;
- `xpra/client/subsystem/window/manager.py`;
- `xpra/client/subsystem/window/draw.py`;
- `xpra/client/gui/window_base.py`;
- `xpra/client/gui/window/backing.py`;
- `xpra/cairo/backing.py`;
- `xpra/cairo/backing_base.py`;
- `xpra/client/gtk3/opengl/drawing_area.py`;
- `xpra/client/gtk3/opengl/glarea_backing.py`;
- `xpra/opengl/backing.py`; and
- `xpra/opengl/shaders.py`.

The patch also owns the focused regressions listed below. The durable live
fixture and runner are maintained infrastructure and are required acceptance
authority; their changes remain in the appropriate tracked infra boundary
rather than being copied into production source.

Overlapping files must resolve in active stack dependency order. Preserve the
semantic ownership above when refreshing or splitting hunks. Never copy an
adjacent case's production change into WSSO merely to make a standalone patch
apply.

## Non-goals

This case does not:

- create a wire window or client backing for a subsurface;
- add a new packet type;
- use a child video codec, `VideoSubregion`, scroll history, EOS, or polling;
- encode composite stages with WebP, PNG, JPEG, AVIF, H.264, mmap, RGB24, or a
  planar format;
- accept compressed raw stages;
- infer paint order from object creation or dictionaries;
- flatten colourspaces through an implicit conversion;
- stretch a partial child crop to full child dimensions;
- acknowledge a native frame once per Xpra peer;
- expose a partial transaction before its final stage;
- reuse a stale backing, topology, geometry, source, content, transaction, or
  ACK identity;
- hide unsupported state by dropping one child or keeping stale pixels;
- transfer parent presentation geometry into an internal child source;
- log arbitrary pixel contents;
- absorb WIS frame-selection, generic timer, VPC codec-lifetime, or
  empty-damage pacing policy; or
- weaken root-only ordinary Xpra behavior.

## Focused regression design

The native and renderer boundaries are exercised directly. Native
tests use real or faithful Wayland/wlroots-facing objects where identity and
ordering matter; packet/concurrency tests use deterministic source and
scheduler controls around the exact production hooks. Renderer tests inspect
real private staging and visible commit behavior rather than only checking
option parsing.

| Module | Case-owned boundary |
| --- | --- |
| `unit.server.window.subsurface_source_test` | Source policy, eligibility/refusal, damage deferral, atomic snapshot ownership, continuous-generation progress, transaction order, scheduler/watchdog, publication, ACK, genuine composition drain, mmap, teardown, and fanout |
| `unit.wayland.subsurface_discovery_test` and its C protocol client | Real wlroots role publication: prebuilt root, prebuilt child, and synchronized first-commit trees; unmapped descendant identity, one listener per wrapper, and terminal registry cleanup |
| `unit.wayland.subsurface_stream_test` | Deterministic native-adapter/model controls: stable identity, role loss/reparent, authoritative tuple dispatch, root-indexed reconciliation, WEDT handoff, topology, transforms, scale/viewport, XDG canvas, snapshots, colourspace, and child callbacks |
| `unit.wayland.pointer_test` | Native pointer adapter hit testing, move/reparent coordinates, no-target clearing, lifecycle, and failure paths |
| `unit.client.cairo_backing_test` | Exact validation, premultiplied staging, atomic swap, invalidation, scaling, and failures |
| `unit.client.opengl_backing_test` | Real mapped FBO staging, reset, blend/state restoration, direct-present rectangles, format handling, atomic commit, deferred-context ownership, backing replacement, close, and failures |
| `unit.client.subsystem.window_test` | Ingress identity, backing epochs, ACK outcomes, paint faults, and transaction-bound refresh |
| `unit.client.gtk3.subsystem.display_test` | Cairo/GL capability intersection and fail-closed discovery |
| `unit.client.terminal.terminal_client_test` | No terminal composite advertisement |
| `unit.client.terminal.terminal_window_test` | Unsolicited composite rejection |
| `unit.wayland.window_test` | Ordinary list-shaped commit/frame integration at the model/subsystem seam |

The discovery regression compiles its tracked protocol client in the frozen
test image and starts a real headless pixman compositor in a fresh process for
each ordering. Its unmapped children deliberately require lifetime discovery
before any buffer or rendering. Later commits must reach each wrapper exactly
once without changing its native pointer or WID; destroying the client removes
every exact registry entry. Native imports, compiler support, or compositor
startup failures cannot turn this subject-of-case test into a skip. The fake
native objects used by the deterministic adapter module cannot substitute for
this first-publication control. Conversely, this deliberately bufferless
discovery test does not observe a mapped root's damage plan. The exact mapped
new-role and reparent full-repair geometry is proved by the durable live gate,
through native role publication, reconciliation, and the retained wire packets.
Its infrastructure controls pin independent full-repair packet plans and reject
a substituted partial plan; they do not derive that expectation from whichever
geometry the producer happened to emit.

The server module covers at least these state families:

- raw-only selector and filter eligibility;
- root-only compatibility and late-client capability;
- exact-model refusal, detach wait, allow, WID reuse, and recovery;
- topology activation, move, reorder, destruction, reparent, and full repair;
- coherent all-layer capture, exactly-once wrapper transfer/free, post-capture
  successor damage, and continuous-generation progress;
- capture-pass content changes plus geometry-, topology-, backing-,
  transaction-, and source-staleness;
- serial stage completion, bounded retry, idle registration races, watchdog,
  synchronous scheduler callbacks, and cleanup cancellation;
- global packet sequence allocation, exact ACK ownership, post-append
  diagnostics, publication rollback, active-operation leases, and concurrent
  unregister;
- background source callouts retained across parent/child removal, final
  averaging and bandwidth read/write ownership, exact source replacement,
  exception-safe borrow release, and closing/deactivation admission;
- stale queue revalidation and mmap terminal drain;
- direct `process_damage_region` deferral and decode-error refresh through its
  real scheduled GLib callback, including consumed timer and refresh state;
- runtime property fanout and toplevel-only video cleanup; and
- cleanup completeness under injected errors.

The native module covers every Wayland transform, bilinear edge behavior,
scale-2 and viewport sampling, full-versus-cropped byte identity, XDG
crop/padding, premultiplied padding, snapshot rollback, capture failure,
unknown mapped surfaces, root-marker placement, layers below the root, nested
children, static pre-role buffers, role detach/reattach, cross-root reparent,
canonical colourspace, and one frame completion per child commit. It also binds
the authoritative tuple and atomic child-commit routes, the ordinary empty
delegate versus successful-repair ownership, cancel/mark ordering before
root-only damage fanout, single composite acknowledgement across multiple
peers, and isolation of old-root handled/repair results from current root and
child damage.

The late-client preparation test observes the real base-delegation seam with
an ordinary function descriptor installed on `WindowServer`. That descriptor
binds the server exactly once in both pure-Python and compiled-module runs.
Its observations assert that each client is prepared or refused before the
inherited initial-window sender runs, without depending on mock autospec
metadata for a compiled parent method.

The client modules cover every mandatory field, boolean rejection, exact flush,
reset placement, transaction floors, skipped/duplicate/stale stages, backing
reuse, decode-thread/UI-thread races, ordinary draw invalidation, malformed
mmap consumption, reported and silent paint faults, Cairo and GL
pre-commit/post-commit errors, format alpha semantics, final-only redraw,
idle-before-close, pre-realize close for both GTK GL backends, backing identity
replacement, and exception-complete GL teardown.

A mock-only test of a helper method cannot replace the native topology,
renderer, or live boundary.

## Durable live gate

`live-wayland-subsurface` is a case-only positive profile:

```bash
make -C fork-maintenance live-wayland-subsurface \
  CASE=wayland-subsurface-stream-ownership \
  RUN=<unique-name>
```

The wrapper rejects `STACK` and every other case. It fixes:

```text
APPLICATION=subsurface
LIFECYCLE=application-exit
ENCODING=rgb
H264_CLIENT_POLICY=strict
ALPHA_SCENARIOS=default
```

Unlike ordinary complete-stack profiles, it applies the exact selected WSSO
source and resolution to both endpoints. The native-Wayland server owns the
surface graph and transaction publisher; the GTK X11 client owns capability
advertisement and transaction rendering. The fixed live arguments select the
Cairo backing. The case's mapped real-Xvfb focused test independently owns the
GTK OpenGL replacement/close route; a Cairo live pass cannot substitute for
that GL boundary. A clean endpoint on either side cannot establish acceptance.

The case has no patch dependency on WEDT, so this case-only live selection runs
with WSSO's standalone immediate fallback available for an ordinary empty root;
it cannot establish WEDT's timer implementation. The resolved complete-stack
focused pair in the validation ladder is the authority for their shared
schedule/cancel/mark seam. This keeps the live pixel oracle tied to one atomic
production case without weakening the timer case's separate acceptance.

### Fixture schema and geometry

The C fixture emits schema 6 and creates two XDG toplevels:

| Role | Title | Logical canvas |
| --- | --- | ---: |
| Primary | `Xpra Wayland Subsurface Fixture` | 420x300 |
| Reparent target | `Xpra Wayland Subsurface Reparent Target` | 360x260 |

The lower child is 220x140 logical pixels backed by a 440x280 scale-2 buffer.
It starts at `(72, 64)` and moves to `(48, 110)` without another attach or
child commit.

The upper child is 160x100 at `(150, 150)` with buffer transform 180. Its
buffer is committed before the subsurface role exists. Later its role is
destroyed and recreated under the secondary root at `(80, 70)` without another
child attach or commit. The same live `wl_surface` keeps one wrapper and
internal WID across that role transition.

For `2 <= N <= 256` continuous generations, the exact `15 + N` event stream
is:

```text
ready
lower-state
lower-state
lower-moved
sibling-created
lower-updated-under-upper
lower-frame-generation
lower-frame-generation
continuous-start
continuous-generation x N
continuous-stop
sibling-click
lower-destroyed
upper-detached
upper-reparented
exit
```

Every event has an exact field set, schema value, zero-based sequence, and
strictly increasing monotonic timestamp. Duplicate JSON keys, missing or extra
events, reordered events, proxy-ID aliasing, counter mismatches, or unexpected
geometry fail the gate.

The two callback-driven lower frame generations attach new buffers and each
require one new callback ID/data pair and an exact cumulative done count. The
upper buffer, surface ID, attach count, and commit count remain unchanged
through detach and reparent.

After `continuous-start`, the same lower surface alternates two fixed buffers
and may commit only after the real `wl_surface_frame` callback for its preceding
commit and a 50 ms minimum interval from its previous continuous commit. These
are independent conditions: elapsed time never substitutes for a callback, and
an immediate callback never bypasses the cadence. The fixture samples one
monotonic timestamp before marshaling each accepted commit, uses it for the
next interval, and emits that same value as its event timestamp. A delayed
callback or late event-loop wake starts a new interval; there is no catch-up
burst. The due check does not sleep or consume a ready callback early, so normal
Wayland dispatch, input, and the stop marker remain responsive. No per-generation
operator command or network-ACK handshake drives this producer.

Their only differing pixels lie within the declared 32x32 logical
damage region; the canonical state outside that region is unchanged. The active
proof must finish within five seconds of the fixture's `continuous-start`,
with the deadline checked again after packet collection. The unchanged
256-generation safety cap cannot be reached sooner than 12.75 seconds after
the first continuous commit; it is a separate fail-closed resource bound, not
the normal end of the observation. Acceptance still requires
the fixture process live, producer active, stop marker absent, and at least two
complete transactions containing both lower payload digests. The stop boundary
then accepts a terminal count through 256, records whether the last callback
completed or was explicitly cancelled, and requires final state and captured
transactions to drain. Native frame callbacks acknowledge the retained commit,
not transport completion. Pending damage may coalesce while an immutable
transaction is in flight, so `N` source commits need not produce `N` network
transactions. A transaction observed only after stop is not active-liveness
evidence.

The schema-3 active/drain record retains the first observed generation count and its host
monotonic observation time, then requires a later source commit before its
final sample. A live process and an `active` flag without that actual progress
are insufficient. It rereads the source commit/callback timeline after
collecting packet artifacts and rejects an already stopped, capped, or late
producer. Captured transaction counts and source-state order bind this fresh
post-transfer prefix: commits may legitimately occur during the transfer, so
the earlier count is only the progress baseline, not a limit on newly captured
transactions. A stale source snapshot taken before an expensive transfer
cannot certify active completion. The retained artifact validator independently
rechecks the same timing, progress, cadence, and exact packet authorities.

An active snapshot has one explicit packet frontier. The observer first pulls
the primary source inventory and freezes its greatest new packet sequence plus
one. Only then does it pull the secondary and both children. Those later reads
may contain newer transactions; the active proof includes exactly the packets
below the already fixed frontier, without moving that frontier to fit the
result. Its last included packet is the newest primary stage zero, so it retains
every earlier complete transaction plus one root-only in-flight tail. Missing
or corrupt packets inside this prefix still fail. At final drain, the validator
independently derives the exact below-frontier packet set from the complete
raw-bound transaction ledger and requires equality with the saved active
snapshot. Every packet beyond the frontier remains part of the terminal
transaction, pixel, draw, ACK, and global sequence accounting; selecting an
observation prefix never discards final evidence.

The retained runner log bounds observation diagnostics to 64 attempts. Each
record identifies the last stage/reason, start/end monotonic times, per-role
collection durations and packet counts/frontiers, and available generation and
transaction counts. It contains no pixel payloads. A failed attempt therefore
distinguishes source readiness, transfer cost, a malformed bounded prefix, and
the final activity/deadline boundary without weakening any of them.

The infrastructure regression compiles the actual C commit scheduler with only
the monotonic clock and Wayland marshaling replaced. Immediate callbacks,
deadline boundaries, a delayed wake, and a missing callback prove pacing
without catch-up or timer-driven commits. Separate observer controls reject
stalled production and a deadline crossed either before or during collection.
These controls validate the fixture/observer contract; the durable live run
supplies the real Wayland callback and end-to-end packet proof.

### Phase and packet authority

Initial transport activity has two independent owners. The inherited server
`WindowManager.send_initial_windows()` announces each already mapped fixture
root and requests one full damage. Later the Wayland `_process_map()` handles
that root's client map, resizes/flushes the native surface, and requests a full
refresh through `refresh_window()`. For the primary, both requests enter
WSSO's composite scheduler; for the childless secondary, they enter ordinary
window batching. Pending requests may coalesce, so the fixed startup produces
one or two primary root/lower transactions and independently one or two
ordinary secondary packets. This bound belongs to those two triggers, not to
a tolerance for arbitrary duplicate delivery. The fixture acknowledges later
XDG configurations without attaching or committing another parent buffer, and
the configure handler itself does not request another full refresh.

The startup ledger retains every one of those packets. Each primary
transaction must contain the exact root stage followed by its lower stage,
with matching transaction identity and epochs, stage count two, the exact
flush countdown, and a first-stage-only full reset. Each secondary packet must
be an ordinary full-canvas packet. Every raw source crop must match the
independent initial fixture pixels, and every published packet must reach its
correct client wire window and complete its ACK ownership. Extra captures,
partial transactions, inconsistent epochs, unexpected geometry, or unaccounted
packets fail even when the final visible image happens to be correct.

Before accepting that snapshot, the runner also establishes that both client
map handlers have reached the server. GTK sends `WINDOW_MAP` before its focus
update on the same Xpra connection; the server dispatches both on its UI queue.
The runner therefore activates the two exact owned parent XIDs and binds fresh
server `_focus` entries for both WIDs. It begins with the opposite of the
latest observed server focus and waits for that handler before activating the
other parent, so GTK's deferred focus recheck cannot coalesce an unobserved
priming activation. One bounded log interval starts before sampling that
anchor and retains both handlers, including a valid already queued update.
The handler entry is the barrier; a repeated-focus early return need not emit
a second native focus-change line. Collection revalidates the exact retained
interval. This proves ordering of the startup requests, not a generic promise
that ordinary window batching is synchronous.

The childless secondary has a separate ordinary batching boundary. Its two
full-window `do_damage` requests may create separate delayed batches or merge
into one. The runner binds those requests and every resulting full-window
`process_damage_region` enqueue from the existing server log, and requires
the final capture to follow the final request. A quiet packet list while the
second request is still delayed is not readiness. Both parent sources also
have directly checked encoding/ACK queues and exact cumulative packet counts;
the global ownership counters and child queue state do not substitute for
those ordinary-source observations.

Startup must then reach a stable, drained packet/source snapshot before the
first controlled change. If `B` complete primary transactions and `S` secondary
packets were retained, both counts lie in `{1, 2}`. The final entries provide
the named initial image bindings, but earlier entries remain mandatory pixel,
transaction, route, ACK, and source-inventory evidence. Replay processes the
whole startup ledger; selecting the last initial packet does not discard its
predecessors. The lower source's initial cumulative packet count is exactly
`B`; every later fixed-phase count is derived from that bound count plus its
unchanged exact phase delta. Continuous capture accounting still uses its own
`M`-transaction interval and is not conflated with startup or native commits.

The pixel phases are:

```text
initial
changed
restored
moved
stacked
lower-updated
lower-frame-one
lower-frame-two
lower-destroyed
upper-detached
reparented
```

These eleven named phases remain deterministic snapshots. The continuous
producer has a separate `continuous-final` capture and drained `xpra info`
boundary; it is not disguised as a twelfth marker-gated phase.

The topology phases follow the production repair owners exactly:

| Phase | Root repair | Child replay |
| --- | --- | --- |
| `stacked` | Full primary canvas `(0, 0, 420, 300)`, because a new native role is reconciled. | Complete lower and upper layers at their current offsets. |
| `lower-destroyed` | Removed lower footprint `(48, 110, 220, 140)`. | Only the surviving upper intersection `(150, 150, 118, 100)`. |
| `upper-detached` | Removed upper footprint `(150, 150, 160, 100)`. | No child: one root-only transaction clears the last role. |
| `reparented` | Full secondary canvas `(0, 0, 360, 260)`, from role reconciliation and first-child activation. | Complete retained upper layer at the new parent-relative offset. |

Content-only updates after stacking retain the clipped dirty-region contract:
they do not gain permission for a full-window repair from the earlier role
attachment. Every phase has one exact plan, not a choice between an arbitrary
larger region and a child crop. The same plan determines the first-stage reset,
each layer's source crop, and the independent canonical pixel comparison.

The runner records `xpra info` and synchronizes `save_update` metadata and raw
payloads for exactly two root WIDs and two persistent child WIDs. It requires
private directories, safe relative paths, regular files, bounded sizes, unique
packet sequences, exact source-to-wire identities, and SHA-256 bindings between
the report, each `.info` file, and its payload.

The raw decoder independently rejects compressed metadata, compressor options,
bad rowstride, size mismatch, unknown format, digest mismatch, unsafe path, and
any composite packet not encoded as raw `rgb32`. It treats A formats as
premultiplied and X formats as opaque.

Transactions are regrouped from retained packet evidence, not trusted from the
runtime summary. The oracle requires exact transaction IDs, stage indices,
counts, epochs, flush countdown, first-stage-only reset, authoritative stage
order, phase-specific crops, and current parent WID. The continuous interval is
bounded by packet sequences captured before start and after drain. Its `M`
captured transactions satisfy `2 <= M <= N`, with each transaction's lower
state independently identified from the canonical fixture crop. Their states
must form a source-event subsequence whose final state is the terminal source
generation. At most the last transaction may be partial in the active
observation; none may remain partial after drain. The source commit/callback
counts and captured packet/stage/ACK counts are each exact within their own
ownership boundary, not falsely equated with each other.

Starting with the retained parent baseline, the oracle clears each reset and
replays source-over with integer premultiplied arithmetic. It compares the
resulting visible RGB pixels to the bound client capture with zero mean absolute
error for both roots in every applicable phase. Each raw layer crop is also
checked against the fixture's independent canonical source state, including
scale/transform normalization and pixels outside the latest dirty region. A
server packet and client backing agreeing on the same wrong pixels therefore
cannot establish acceptance. Async source screenshots are
neither collected for the successful WSSO profile nor accepted as
packet-correlated authority.

The phase comparisons prove:

- initial, changed, restored, and moved alpha composites exactly;
- restored pixels equal the original state;
- moving changes only placement and preserves source pixels;
- the upper child is above the lower child in their exact overlap;
- a lower update and both frame generations preserve unchanged upper pixels;
- continuous callback-gated lower updates complete while the producer remains
  active, capture both expected lower states in source-event order, preserve the upper layer,
  and produce the exact final composite after drain;
- lower destruction restores the parent plus upper child;
- upper role detach restores the primary parent;
- reparent preserves the upper surface and buffer and composites it into the
  secondary parent; and
- both root canvases remain stable where no phase owns a change.

### Input, ACK, lifecycle, and report authority

Client mapped-window identity and server wire identity are separate checks.
The runner retains each initial client XID together with its exact observed
window-manager title; the primary XID must match initial client discovery.
Before requesting fixture exit it requires those same tuples again. A client
title may include Xpra's configured host information, so it is not compared
with the bare source title or normalized by stripping a suffix. Missing
windows, replacement XIDs, and changed titles fail independently. Initial/final
tuples are retained in bounded structured diagnostics; truncation bounds only
the log, never the comparison. In parallel, every phase's server inventory
must retain exactly the two original wire WIDs and the fixture's bare source
titles. A stable client XID cannot substitute for that server-side identity
contract, or vice versa.

The runner has one combined three-second real-input deadline. `xdotool` moves
to and clicks the upper child's exact centre through the Xpra client. Acceptance
requires:

- the client-side pointer action;
- the server root-to-leaf pointer log with expected local coordinates;
- the fixture's `sibling-click` event naming the upper leaf; and
- exactly one bounded click response.

Publication logs and ACK logs are parsed into an exact ordered route. Every
child packet must target its current parent, every sequence must be globally
unique, every parent-WID ACK must route to its exact child source, and each
phase plus the continuous-final info snapshot must drain ACK owners, pending
ACKs, encoding work, both composition ownership counters, and any incomplete
transaction.

The gate requires the lower source to disappear after destruction, the upper
WID to remain stable across role rebind, no child EOS, both parent windows to
remain alive until application exit, exact fixture exit status zero, empty
fixture stderr, normal visible rendering, input, lifecycle, and owned cleanup.

The final classifier requires all 36 named checks:

```text
fixture_event_stream_exact
two_parent_wire_windows
internal_child_sources_identified
same_lower_updated_repeatedly
lower_moved_without_buffer_attach
overlapping_sibling_stack_exact
child_transactions_raw_rgb32_only
child_packets_target_current_parent
global_damage_sequences_unique
child_ack_owner_exact
child_ack_drained
child_sources_have_transparency
premultiplied_source_over_wire_contract
atomic_transaction_contract_exact
initial_alpha_composite_exact
changed_alpha_composite_exact
restored_alpha_composite_exact
moved_alpha_composite_exact
lower_update_preserves_upper
child_frame_generations_exact
continuous_child_active_liveness
continuous_transactions_complete
continuous_callback_accounting_exact
continuous_final_composite_exact
sibling_destroy_restores_parent_and_upper
upper_detach_restores_primary
reparent_preserves_surface_and_buffer
reparent_composite_exact
client_pointer_path
server_pointer_path
fixture_pointer_path
lower_source_removed
upper_wid_stable_and_role_rebound
no_child_eos
parents_live_until_exit
fixture_clean_exit
```

Collection reparses the retained artifacts and recomputes the report. A claimed
boolean without its underlying exact evidence cannot pass.

This gate is additional and case-only; it is not an eighth complete-stack
profile and does not replace any of the seven normal positive live profiles.

## Invariants not to simplify

Future work must preserve all of these invariants.

- One native `wl_surface` gets one stable wrapper and WID until native destroy.
- Subsurface-role destroy is not `wl_surface` destroy.
- A role can reattach a static buffer and nested branch without a child commit.
- Registry strong references live exactly as long as native listener pointers.
- Root and child pixels are normalized once, before cropping.
- Transform, scale, viewport, and XDG geometry are part of content identity.
- Retained model bytes outlive and are isolated from borrowed encode wrappers.
- Snapshot clear is an authoritative content generation.
- A tuple with one root marker is the authoritative WSSO root-commit shape;
  the child-only list and `subsurface_image()` remain ordinary compatibility
  APIs and never define a composite generation.
- Native child state reaches the server through one atomic
  `subsurface_commit`; topology and pixels are not split across callbacks.
- Topology is the exact wlroots paint order and contains one root marker.
- Children below the root remain below it during replay.
- Unknown or malformed native topology refuses the whole root.
- Root and every active child have canonical identical colourspace.
- Refusal and announcement bind the exact model object, not only a WID.
- Root-only operation requires no composite capability.
- An active tree never falls back to partial forwarding.
- Children remain internal sources and never become wire windows.
- Active composite stages are uncompressed raw `rgb32` only.
- A-format bytes remain premultiplied; X-format bytes remain opaque.
- Every transaction starts with one reset and ends with one atomic commit.
- No non-final stage changes visible client pixels or schedules visible redraw.
- Direct presentation covers the complete transaction union and uses exact
  framebuffer rectangle endpoints.
- Deferred GL-context work belongs to one exact backing and completes once;
  replacement or close supplies `context=None` rather than waiting for a
  realization which can no longer occur.
- Transaction IDs and packet sequences are connection-global and never reused.
- Backing, topology, geometry, source, and content generations are independent.
- Handled and successfully repaired sources are tracked per affected root;
  another root's reconciliation cannot suppress current-root or current-child
  damage.
- Refusal and reconciliation failure are handled-without-repair outcomes and
  never claim the successful repair's ordinary ACK ownership.
- Every transaction captures one coherent immutable layer set before async
  encoding; a newer native commit queues one coalesced successor and cannot
  starve or replace the captured transaction.
- Parent-targeted ACKs return to the exact producing source.
- Publication and ACK callbacks run outside the connection lock under leases.
- Source unregister waits for its active operations and removes only its state.
- A performed mmap write always receives a terminal drain.
- Outgoing control priority and actual-queue `more` semantics remain intact.
- Child auto-refresh, video, scroll, and EOS stay disabled.
- Pointer hit testing uses the current native input leaf on every packet.
- Pointer focus clears before native role or surface invalidation.
- One native commit produces one surface callback, not one callback per peer.
- Root-only empty pacing remains WEDT authority; ordinary root damage uses its
  cancel/mark guards, while composite root commits acknowledge directly once.
- Scheduler retries and watchdogs are bounded and owned by one connection.
- Cleanup attempts every owned source and reports the first error afterward.
- Diagnostics bind identities without logging raw pixel data.
- The schema-6 live oracle remains independent of implementation summaries.
- WIS, generic timer, VPC, and empty-damage ownership remain separate.

## Required validation

Follow [development and final acceptance](../../docs/runbooks/validation.md).
Run the nearest real regression immediately after each atomic edit, include
affected existing upstream and dependent/composed case modules, and stop
escalation at the first unexplained failure. The development boundaries are:

1. Retain a tests-only control which fails non-vacuously on the clean embedded
   source for the production behavior under test.
2. Run the dedicated server module:
   `unit.server.window.subsurface_source_test`.
3. Run the dedicated native modules:
   `unit.wayland.subsurface_discovery_test`,
   `unit.wayland.subsurface_stream_test`, and `unit.wayland.pointer_test`.
4. Run the affected client modules:
   `unit.client.cairo_backing_test`,
   `unit.client.opengl_backing_test`,
   `unit.client.subsystem.window_test`,
   `unit.client.gtk3.subsystem.display_test`,
   `unit.client.terminal.terminal_client_test`, and
   `unit.client.terminal.terminal_window_test`. The OpenGL module must exercise
   its mapped Xvfb close/replacement cases in the canonical container; a
   post-summary host display teardown abort is not a successful module result.
5. Run `unit.wayland.window_test` and the repository's `wayland` native leg.
   Resolve the complete stack and repeat both
   `unit.wayland.subsurface_stream_test` and `unit.wayland.window_test` there;
   the pair binds WSSO's commit adapter to WEDT's real scheduler, model guard,
   and cleanup owner while preserving the ordinary compatibility path.
6. Run case resolution, whitespace, manifest/path/digest, fork-control, and
   isolated-workspace checks required by the current repository contract.
7. Exercise the real compiled implementation and compatibility-disabled packet
   route when those boundaries change; Python-only tests do not substitute.
8. Run the case-owned positive
   `live-wayland-subsurface CASE=wayland-subsurface-stream-ownership` gate with a
   fresh unique run identity. It must prove at least two complete transactions
   while the callback-gated producer is active, then exact completion for every
   captured transaction, independent generated commit/callback accounting,
   genuine queue drain, and the final committed source pixels after stop.
   This relevant live proof runs after focused/native prerequisites, not after
   the full upstream matrix.

Changes to connection-wide packet ownership, sequence allocation, or saved
packet metadata also require an early affected complete-stack H.264 hardware
profile with both title-bound windows. The case-only RGB transaction gate
cannot exercise the shared ordinary-root H.264 observers. Use the existing
fixed wrapper with `STACK=develop` after the relevant focused/native checks;
do not invent an atomic H.264 gate for this case or require full upstream
suites or DEB builds before this integration feedback. This additional
development check does not replace the case-owned RGB transaction proof.

After reviewing and freezing source, fixtures and the packet/pixel oracle,
fill missing or invalidated final requirements: current clean quarantine,
the three full legs, every required atomic gate, and all seven positive
complete-stack profiles. Reuse valid development-stage named results only with
the input proof required by the validation runbook; do not repeat the whole
set after each subsurface edit.

Clean and patched comparisons must use the same frozen source, test image,
Wayland compositor, GTK client, fixture, static CLI/profile inputs, and harness
digest. A fallback picture, screenshot-only comparison, root-only window,
mock-only transaction, or report boolean without retained packet authority is
not acceptance evidence.
