# Publish the opaque region of Wayland popups

Code: the `Fork-Case: wayland-popup-opaque-region` commit on `develop`
(`make -C fork-maintenance case-show CASE=wayland-popup-opaque-region`).

## Boundary

A native-Wayland seamless server creates an Xpra window model for every
`xdg_popup` (menus, context menus, tooltips) in
`WaylandWindowServer._ensure_popup_window()`. Toplevel models are created with
an `opaque-region` and update it on every commit; popup models were created
without one, so their `opaque-region` property stayed `None` for their whole
life.

Every attached client gets a `WindowSource` for the new popup. Its constructor
reads `self.window.get("opaque-region", ())`, which returns the property value
`None` (the property exists, so the default is not used), and
`update_encoding_options()` then calls `is_covered_by_opaque_region(None, ...)`
whenever the client supports transparency. Iterating `None` raises
`TypeError`. The exception escapes `popup_commit()` from the native
`Popup.commit` callback, where Cython reports it as
`Exception ignored in: 'xpra.wayland.server.popup.Popup.commit'`; the window
source is left half initialised (its constructor stopped in
`init_encoders()`), and the rest of that commit (colourspace, content types,
geometry) is skipped for the popup.

The live `live-x11-clipboard` profile reproduces it (Debian 13 X11 client
with transparency, Ubuntu 26.04 native-Wayland server, RGB): opening the GTK
context menu of the clipboard fixture prints the unraisable exception in the
server log every time, and the live runtime-log audit fails a profile on any
unraisable exception. The profile's clipboard oracles do not inspect the
popup's window source, so only the log audit exposes it.
X11 servers are unaffected: their models always publish a tuple. Subsurface
facades answer `()` for the property.

## Embedded-source context

- `7ba6959c35` (2021, "#3317 don't encode alpha channel if the window is
  opaque") introduced the opaque-region alpha check in `WindowSource`. The
  constructor read has used `get("opaque-region", ())` since then, while the
  change handler uses `get_property(...) or ()`.
- `1d16c20e36` and `8989a9b6d6` (2026-03) moved the coverage loop into
  `xpra.common.is_covered_by_opaque_region()`. Like the loop it replaced, it
  requires a sequence; it did not introduce the failure.
- `f50030fce6` (2026-09, "#387 expose Wayland opaque regions") made the
  Wayland window model publish `opaque-region`: it added the property to the
  model, `Surface.get_opaque_region()` for toplevels, the initial value for
  toplevels and `update_opaque_region()` in the toplevel commit. Popups were
  not included: `Popup` has no `get_opaque_region()`, `_ensure_popup_window()`
  passes no value and `popup_commit()` does not update it.

The upstream issue [#387](https://github.com/Xpra-org/xpra/issues/387)
("wayland server backend") tracks the Wayland backend as a whole; its timeline
lists the commits but no discussion of opaque regions, popups' alpha or this
failure, and no public upstream issue for the traceback was found. The
public upstream `master` sources of these files, read without fetching, have
the same gap: `Popup` has no `get_opaque_region()`, and neither
`_ensure_popup_window()` nor `popup_commit()` sets the property, while
`WindowSource` reads it as described above. No maintainer review of popup
opaque regions exists to follow or answer.

The maintainer's own design for the property is a model that always carries a
tuple (X11 `_handle_opaque_region_change()` publishes `()` without a hint;
Wayland toplevels publish `surface.get_opaque_region()`, which is `()` without
an xdg surface). This case therefore completes `f50030fce6` for popups at the
producer, reusing its helper and `update_opaque_region()`, instead of making
consumers accept an unset value. Retire it when upstream popup models publish
their opaque region from creation and on commit.

## Surrounding code and ownership map

| Owner | Responsibility |
| --- | --- |
| `Surface.get_opaque_region()` (`surface.pyx`) | toplevel: clip `wlr_surface.opaque_region` to `xdg_surface.geometry` |
| `get_clipped_opaque_region()` (`surface.pyx`, now declared in `surface.pxd`) | the shared clipping helper, now also cimported by `popup.pyx` |
| `Popup.get_opaque_region()` (`popup.pyx`, new) | popup: the same clipping with the popup's own `xdg_surface` geometry |
| `WaylandWindowServer._ensure_popup_window()` | creates the popup model, now with the popup's committed region |
| `WaylandWindowServer.popup_commit()` | per-commit popup state, now including `update_opaque_region()` |
| `WindowSource.__init__()` / `window_opaque_region_changed()` | generic consumers, unchanged |

## Mechanism and lifecycle

`Popup.get_opaque_region()` mirrors the toplevel method: it returns `()` when
the popup has no `xdg_surface` or `wl_surface` (not yet attached or already
destroyed), otherwise the committed opaque region clipped to the popup's
`xdg_surface.geometry` and translated into window coordinates, matching what
the popup capture sends (`Popup.commit()` captures that same geometry).

The model is created with that value, so the first `WindowSource` reads a
tuple. Each `popup_commit()` refreshes it through the existing
`update_opaque_region()`, whose change notification drives the existing
`window_opaque_region_changed()` (discard-alpha and encoding options). The
update runs before the colourspace, content-type and geometry updates of the
same commit; the generic window source only re-evaluates coverage against its
own dimensions, which it refreshes on damage, so this order does not change
which dimensions it uses.

## Case-commit and integration ownership

The commit changes `xpra/wayland/server/popup.pyx`,
`xpra/wayland/server/surface.pxd` (declares the existing helper for cimport),
`xpra/wayland/server/subsystem/window.py` and
`tests/unittests/unit/wayland/window_test.py`. It has no dependency: the
popup creation and commit code it touches is the embedded source's, and the
insertion avoids the lines other cases change. Related cases:

- `wayland-initial-window-state` owns popup frame publication order
  (`pixel-format`/`frame-has-alpha`/image before damage) and the generic
  opaque-region notification ordering before virtual reconfiguration; this
  case only supplies the popup's region to that existing path.
- `wayland-subsurface-stream-ownership` owns subsurface facades, which answer
  `()` for the property themselves.

## Commit scope and non-goals

Unchanged: the toplevel path, the X11 models, `WindowSource` and
`is_covered_by_opaque_region()`. A consumer-side `or ()` in the
`WindowSource` constructor was rejected as the fix: it would hide the unset
model property, and popups would still never publish a real opaque region,
so an opaque menu would keep encoding an alpha channel it does not need.

## Regression design and clean control

`unit.wayland.window_test.WaylandPopupOpaqueRegionTest` uses the real Wayland
`Window` model created by the real `_ensure_popup_window()`:

- `test_new_popup_publishes_its_opaque_region`: the popup's region is the
  model's initial `opaque-region` (clean: `None`).
- `test_window_source_can_check_a_new_native_popup`: the compiled `Popup`
  extension (unattached, so its committed region is empty) creates a model
  whose region `is_covered_by_opaque_region()` accepts; it skips only when the
  Wayland server modules are not built, and the `wayland` gate builds them.
  Clean: `None`, the `TypeError` the live server raised.
- `test_popup_commit_updates_its_opaque_region`: a popup commit notifies the
  model's new region (clean: no notification).

The clean tests-only control must fail these three and only these. Blind spot:
the clipping of a real client's region is shared with toplevels and not
re-tested here; the live clipboard profile opens a real GTK context menu.

## Durable live or package boundary

`live-x11-clipboard` opens the clipboard fixture's GTK context menu on the
native-Wayland server with a transparency-capable client; its server log must
not contain an unraisable exception (enforced by the live runtime-log audit).

## Invariants not to simplify

- A Wayland window model never exposes `opaque-region` as `None`: popups are
  created with the popup's region and update it on commit, like toplevels.
- `Popup.get_opaque_region()` must use the popup's own `xdg_surface`
  geometry and return `()` once the popup is detached.

## Required validation

Follow [canonical validation](../../docs/runbooks/validation.md): clean
tests-only `focused` and `wayland` controls failing exactly the three tests
above; patched `focused` and `wayland` runs; the three full upstream legs; the
exact matching complete-stack DEB artifacts; the complete nine-profile live
suite, including
`live-x11-clipboard` with a clean server-log audit.
