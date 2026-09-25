# This file is part of Xpra.
# Copyright (C) 2026 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

from socket import gethostname
from typing import Final
from collections.abc import Sequence

from xpra.codecs.image import ImageWrapper
from xpra.common import noop
from xpra.constants import MoveResize, SOURCE_INDICATION_NORMAL
from xpra.net.common import Packet
from xpra.net.packet_type import WINDOW_CREATE
from xpra.server.common import get_sources_by_type
from xpra.server.source.window import WindowsConnection
from xpra.server.subsystem.window import WindowServer
from xpra.util.colourspace import Colourspace
from xpra.util.objects import typedict
from xpra.wayland.server.models.subsurface_window import SubsurfaceWindow
from xpra.wayland.server.models.window import Window
from xpra.wayland.server.popup import Popup
from xpra.wayland.server.subsurface import Subsurface
from xpra.wayland.server.surface import Surface
from xpra.log import Logger

log = Logger("server", "wayland")
focuslog = Logger("server", "wayland", "focus")

# Per-surface signals connected on each new Surface in `new_surface`.
PER_SURFACE_EVENTS: Final[Sequence[str]] = (
    "map", "unmap", "commit", "destroy",
    "title", "app-id",
    "minimize", "maximize", "fullscreen",
    "move", "resize",
    "surface-snapshot",
    "set-parent",
    "new-subsurface",
)
PER_SUBSURFACE_EVENTS: Final[Sequence[str]] = (
    "destroy", "new-subsurface", "subsurface-commit",
    "subsurface-role-destroy",
)


def replace_image_snapshot(window, image: ImageWrapper | None) -> None:
    """Copy one native-owned generation into retained model state.

    The callback argument remains borrowed: the native emitter owns and frees
    it exactly once after all synchronous observers return.  This boundary
    additionally invalidates the old generation if replacement fails and
    handles the optional WIS pixel-format property when clearing.
    """
    if image is None:
        window.clear_image()
        return
    try:
        replace = getattr(window, "replace_image_snapshot", window.set_image)
        replace(image)
    except BaseException:
        try:
            window.clear_image()
        except BaseException:
            log.error("Error invalidating failed Wayland snapshot", exc_info=True)
        raise


def canonical_colourspace(value) -> dict[str, str] | None:
    """Accept only an exact canonical Colourspace dictionary round-trip."""
    if not isinstance(value, dict):
        return None
    try:
        canonical = Colourspace.from_dict(value).to_dict()
    except BaseException:
        return None
    return canonical if value == canonical else None


class WaylandWindowServer(WindowServer):
    __slots__ = (
        "focused", "pending_popups", "pointer_focus", "subsurface_facades", "subsurface_info",
        "subsurface_parents", "subsurface_stacking", "subsurface_topology_errors",
        "subsurfaces", "toplevel_wid",
    )

    def __init__(self, server=None):
        super().__init__(server)
        self.focused = 0
        self.pointer_focus = 0
        self.toplevel_wid: dict[int, int] = {}
        self.pending_popups: dict[int, tuple[int, Popup]] = {}
        # subsurface wid -> (parent_wid, offset_x, offset_y, logical_w, logical_h, native_w, native_h)
        self.subsurface_info: dict[int, tuple[int, int, int, int, int, int, int]] = {}
        # subsurface wid -> SubsurfaceWindow facade kept alive across the subsurface lifetime.
        self.subsurface_facades: dict[int, SubsurfaceWindow] = {}
        # WSSO stores the root wire parent in the first `subsurface_info` item;
        # native direct-parent identity remains separate below for nested trees.
        # Every wl_surface-owned wrapper remains registered through temporary
        # role loss and effective unmap; only wl_surface destroy removes it.
        self.subsurfaces: dict[int, Subsurface] = {}
        # subsurface wid -> direct Wayland parent wid (which may itself be a child).
        self.subsurface_parents: dict[int, int] = {}
        # root WID -> full mapped wlroots bottom-to-top paint order, including
        # the root marker at its exact compositor position.
        self.subsurface_stacking: dict[int, tuple[int, ...]] = {}
        # A malformed authoritative tree must fail closed without installing a
        # partial replacement or silently continuing the previous composite.
        self.subsurface_topology_errors: dict[int, str] = {}

    def connect_compositor(self, compositor) -> None:
        compositor.connect("new-surface", self.new_surface)
        compositor.connect("new-popup", self.new_popup)
        compositor.connect("ssd", self.ssd)
        compositor.connect("activate-request", self.activate_request)

    def get_surface(self, wid: int):
        window = self.get_window(wid)
        if not window:
            return None
        return window._gproperties.get("surface")

    def get_surface_wid(self, surface_ptr: int, root_wid: int = 0) -> int:
        """Map one live native surface pointer to its stable internal WID."""
        if not surface_ptr:
            return 0
        if root_wid:
            root = self.get_surface(root_wid)
            if root and getattr(root, "wl_surface_ptr", 0) == surface_ptr:
                return root_wid
        for wid, surface in tuple(self.subsurfaces.items()):
            if getattr(surface, "wl_surface_ptr", 0) == surface_ptr:
                return wid
        return 0

    def _clear_pointer_focus(self, *surface_wids: int) -> None:
        """Release a seat focus before any referenced native role disappears."""
        pointer = self.server.subsystems.get("pointer")
        clear_focus = getattr(pointer, "clear_pointer_focus", None)
        if clear_focus:
            clear_focus(*surface_wids)
        elif not surface_wids or self.pointer_focus in surface_wids:
            # Defensive fallback for partial/headless subsystem setups.
            self.pointer_focus = 0

    def _register_surface_events(self, surface, events: Sequence[str]) -> None:
        for event in events:
            handler = getattr(self, event.replace("-", "_"))
            surface.connect(event, handler)

    def new_surface(self, surface: Surface, title: str, app_id: str, size: tuple[int, int]) -> None:
        self._register_surface_events(surface, PER_SURFACE_EVENTS)
        geom = (0, 0, size[0], size[1])
        window = Window({
            "client-machine": gethostname(),
            "pid": surface.get_client_pid(),
            "display": self.server.compositor.get_display(),
            "surface": surface,
            "colourspace": surface.get_colourspace(),
            "content-types": surface.get_content_types(),
            "title": title,
            "app-id": app_id,
            "parent": 0,
            "transient-for": 0,
            "relative-position": (),
            "override-redirect": False,
            "window-type": ("NORMAL",),
            "role": "",
            "iconic": False,
            "geometry": geom,
            "image": None,
            "depth": 32,
            "has-alpha": True,
            "opaque-region": surface.get_opaque_region(),
            "decorations": False,
        })
        window.setup()
        self.track_toplevel(surface)
        self.do_add_new_window_common(surface.wid, window)
        if size != (0, 0):
            self._do_send_new_window_packet(WINDOW_CREATE, window, geom)

    def new_popup(self, parent_wid: int, popup: Popup,
                  position: tuple[int, int], size: tuple[int, int]) -> None:
        popup.connect("map", self.popup_map)
        popup.connect("unmap", self.unmap)
        popup.connect("commit", self.popup_commit)
        popup.connect("surface-image", self.surface_image)
        popup.connect("reposition", self.popup_reposition)
        popup.connect("destroy", self.popup_destroy)
        self.pending_popups[popup.wid] = (parent_wid, popup)
        self._ensure_popup_window(parent_wid, popup, position, size)

    def _ensure_popup_window(self, parent_wid: int, popup: Popup,
                             position: tuple[int, int], size: tuple[int, int]):
        window = self.get_window(popup.wid)
        if window:
            return window
        x, y = position
        w, h = size
        if w <= 0 or h <= 0:
            log("not creating popup window %i yet: invalid size %ix%i", popup.wid, w, h)
            return None
        parent_wid = self._popup_parent_window_wid(popup, parent_wid)
        geom = (x, y, w, h)
        window = Window({
            "client-machine": gethostname(),
            "pid": popup.get_client_pid(),
            "display": self.server.compositor.get_display(),
            "surface": popup,
            "colourspace": popup.get_colourspace(),
            "content-types": popup.get_content_types(),
            "title": "",
            "app-id": "",
            "parent": parent_wid,
            "transient-for": parent_wid,
            "relative-position": position,
            "override-redirect": True,
            "window-type": ("DROPDOWN_MENU", "POPUP_MENU"),
            "role": "popup",
            "iconic": False,
            "geometry": geom,
            "image": None,
            "depth": 32,
            "has-alpha": True,
            "decorations": False,
        })
        window.setup()
        self.do_add_new_window_common(popup.wid, window)
        self.pending_popups.pop(popup.wid, None)
        self._do_send_new_window_packet(WINDOW_CREATE, window, geom)
        return window

    def _popup_parent_window_wid(self, popup: Popup, fallback: int) -> int:
        parent = popup.get_parent()
        while parent:
            wid = getattr(parent, "wid", 0)
            if wid and self.get_window(wid):
                return wid
            parent = parent.get_parent() if hasattr(parent, "get_parent") else None
        return fallback if self.get_window(fallback) else 0

    def track_toplevel(self, surface) -> None:
        if not surface:
            return
        log("toplevel(%s)=%#x", surface, surface.toplevel_ptr)
        if toplevel_ptr := surface.toplevel_ptr:
            self.toplevel_wid[toplevel_ptr] = surface.wid

    def ssd(self, toplevel_ptr: int, ssd: bool) -> None:
        wid = self.toplevel_wid.get(toplevel_ptr, 0)
        log.info("ssd(%#x)=%s (wid=%i)", toplevel_ptr, ssd, wid)

    def activate_request(self, surface_ptr: int, token: str) -> None:
        focuslog("activate-request(%#x, %r)", surface_ptr, token)
        from xpra.wayland.server.wayland_surface import surfaces
        wsurface = surfaces.get(surface_ptr)
        if not wsurface:
            focuslog("activate-request: no surface for %#x", surface_ptr)
            return
        wid = getattr(wsurface, "wid", 0)
        window = self.get_window(wid)
        if not wid or not window:
            focuslog("activate-request: no window for wid=%s", wid)
            return
        self._focus(None, wid, None)
        for ss in get_sources_by_type(self.server, WindowsConnection):
            ss.raise_window(wid, window)

    def surface_image(self, wid: int, image: ImageWrapper) -> None:
        window = self.get_window(wid)
        if not window:
            if wid in self.pending_popups:
                return
            log.warn("Warning: cannot update window %i: not found!", wid)
            return
        log("new surface image for window %i: %s", wid, image)
        # Do not free the image while window compression threads may still reference it.
        image.free = noop
        # The buffer the client has committed tells us whether these pixels really have
        # an alpha channel: an `XRGB` / `XBGR` one has none, which lets the window source
        # use a video encoding for them. This is internal state and not `has-alpha`:
        # the client's backing must keep the alpha a subsurface may still paint into it.
        # It has to be published before the image, so that the encoding selection is
        # already up to date when the `commit` which follows this signal becomes damage:
        window._updateprop("frame-has-alpha", "A" in image.get_pixel_format())
        window._updateprop("image", image)

    def map(self, wid: int, title: str, app_id: str, size: tuple[int, int]) -> None:
        window = self.get_window(wid)
        if not window:
            log.warn("Warning: cannot map window %i: not found!", wid)
            return
        window._updateprop("iconic", False)
        window._updateprop("title", title)
        window._updateprop("app-id", app_id)
        self.update_size(window, size)
        surface = self.get_surface(wid)
        get_surface_tree = getattr(surface, "get_surface_tree", None)
        if get_surface_tree:
            surface_tree = get_surface_tree()
            if surface_tree:
                self._update_subsurface_topology(wid, surface_tree)

    def title(self, wid: int, title: str) -> None:
        window = self.get_window(wid)
        if window:
            window._updateprop("title", title)

    def app_id(self, wid: int, app_id: str) -> None:
        window = self.get_window(wid)
        if window:
            window._updateprop("app-id", app_id)

    def unmap(self, wid: int) -> None:
        window = self.get_window(wid)
        if window:
            window.set_property("iconic", True)
        # Root visibility is independent of each child's attached buffer.
        # Stop active composition while retaining every descendant snapshot so
        # a root-only remap can restore a completely static branch.
        for child_wid in self._direct_subsurface_children(wid):
            self._deactivate_subsurface_tree(child_wid)
        self._clear_pointer_focus(wid)

    def minimize(self, wid: int) -> None:
        self._toggle_state(wid, "iconic")

    def maximize(self, wid: int) -> None:
        window = self.get_window(wid)
        if not window:
            return
        window._updateprop("iconic", False)
        self._toggle_state(wid, "maximized")

    def fullscreen(self, wid: int) -> None:
        self._toggle_state(wid, "fullscreen")

    def _toggle_state(self, wid, name: str) -> None:
        window = self.get_window(wid)
        if not window:
            log.warn("Warning: cannot toggle %r state, window %i not found", name, wid)
            return
        window._updateprop(name, not window._gproperties.get(name, None))

    def commit(self, wid: int, mapped: bool,
               size: tuple[int, int],
               rects: Sequence[tuple[int, int, int, int]],
               subsurfaces: Sequence[tuple[int, int, int, int, int, int, int]]) -> None:
        log(f"commit wid {wid} {mapped=}, {size=}, {rects=}, {subsurfaces=}")
        window = self.get_window(wid)
        if not window:
            return
        surface = self.get_surface(wid)
        self.track_toplevel(surface)
        self.update_colourspace(window, surface)
        self.update_content_types(window, surface)
        self.update_size(window, size)
        self.update_opaque_region(window, surface)
        if not isinstance(subsurfaces, list):
            # Native WSSO supplies an authoritative tuple containing the root
            # marker.  Keep the legacy list path below textually and
            # semantically owned by the ordinary Wayland commit case.
            self._commit_surface_tree(wid, mapped, rects, subsurfaces, window)
            return
        for sub_wid, sx, sy, logical_w, logical_h, native_w, native_h in subsurfaces:
            self.subsurface_info[sub_wid] = (wid, sx, sy, logical_w, logical_h, native_w, native_h)
            facade = self.subsurface_facades.get(sub_wid)
            if facade:
                facade.update_dimensions(logical_w, logical_h)
            for ss in self.window_sources():
                sub_ws = ss.subsurface_sources.get(sub_wid)
                if sub_ws:
                    sub_ws.update_geometry(wid, sx, sy, logical_w, logical_h, native_w, native_h)
        if mapped and not rects:
            window.schedule_empty_acknowledgement()
            return
        if rects:
            # this damage has to reach a client before the frame callback can be answered.
            # marking it must happen before the first `refresh_window_area`, which can send
            # the delayed regions synchronously - and would then have nothing left to clear:
            window.mark_damage_frame_pending()
        options = {"damage": True}
        last = len(rects) - 1
        for i, (x, y, w, h) in enumerate(rects):
            options["more"] = i != last
            self.refresh_window_area(window, x, y, w, h, options=options)

    @staticmethod
    def _cancel_ordinary_empty_damage_ack(window) -> None:
        """Cancel the model's paced answer to an earlier undamaged root commit."""
        window.cancel_empty_ack_timer()

    @staticmethod
    def _mark_ordinary_damage_frame_pending(window) -> None:
        """Join the model's frame guard before ordinary damage fanout."""
        window.mark_damage_frame_pending()

    @staticmethod
    def _acknowledge_ordinary_empty_commit(window) -> None:
        """Pace an ordinary root-only undamaged commit through its model."""
        window.schedule_empty_acknowledgement()

    def _commit_surface_tree(
            self, wid: int, mapped: bool,
            rects: Sequence[tuple[int, int, int, int]],
            surface_tree: Sequence[tuple[int, int, int, int, int, int, int]],
            window,
    ) -> None:
        """Reconcile one authoritative WSSO root generation."""
        affected_roots = self._update_subsurface_topology(wid, surface_tree, reconcile=False)
        handled_sources_by_root = {}
        repaired_sources_by_root = {}
        for root_wid in affected_roots:
            repaired_sources = set()
            handled_sources_by_root[root_wid] = self._reconcile_subsurface_root(
                root_wid, repaired_sources=repaired_sources,
                mapped_root=mapped if root_wid == wid else None,
            )
            repaired_sources_by_root[root_wid] = repaired_sources
        handled_sources = handled_sources_by_root.get(wid, set())
        repaired_sources = repaired_sources_by_root.get(wid, set())
        composite_owned = mapped and any(
            surface_wid != wid for surface_wid in self.subsurface_stacking.get(wid, ())
        )
        if mapped and not rects and not repaired_sources:
            if composite_owned:
                self._cancel_ordinary_empty_damage_ack(window)
                window.acknowledge_changes()
            else:
                self._acknowledge_ordinary_empty_commit(window)
            return
        if not repaired_sources and (not mapped or composite_owned or rects):
            self._cancel_ordinary_empty_damage_ack(window)
        if mapped and not composite_owned and rects and not repaired_sources:
            self._mark_ordinary_damage_frame_pending(window)
        sources = self._subsurface_window_sources()
        options = {"damage": True}
        last = len(rects) - 1
        try:
            for i, (x, y, w, h) in enumerate(rects):
                options["more"] = i != last
                for ss in sources:
                    if ss not in handled_sources:
                        ss.damage(wid, window, x, y, w, h, options)
        finally:
            if composite_owned:
                # Each connection redirects this damage into its own atomic
                # transaction, so no WindowSource reaches send_delayed_regions
                # to acknowledge the compositor commit.  Frame pacing belongs
                # to the root wl_surface and is completed exactly once here,
                # independently of the number of connected peers.
                window.acknowledge_changes()

    def subsurface_image(self, wid: int, image: ImageWrapper,
                         logical_w: int, logical_h: int, native_w: int, native_h: int) -> None:
        """Handle ``subsurface-image`` signals dynamically connected in ``new_subsurface``.

        ``Subsurface.commit`` emits this signal after capturing damaged child pixels.
        """
        info = self.subsurface_info.get(wid)
        if not info:
            log("subsurface-image: no parent info for wid=%i, dropping", wid)
            return
        parent_wid, ox, oy, old_logical_w, old_logical_h, old_native_w, old_native_h = info
        if not self.get_window(parent_wid):
            log("subsurface-image: no parent window for wid=%i (parent=%i)", wid, parent_wid)
            return
        native_w = native_w or old_native_w or image.get_width()
        native_h = native_h or old_native_h or image.get_height()
        logical_w = logical_w or old_logical_w or native_w
        logical_h = logical_h or old_logical_h or native_h
        self.subsurface_info[wid] = (parent_wid, ox, oy, logical_w, logical_h, native_w, native_h)
        facade = self.subsurface_facades.get(wid)
        if facade is None:
            facade = SubsurfaceWindow(logical_w, logical_h, has_alpha=True, depth=32,
                                      surface=self.subsurfaces.get(wid),
                                      display=self.server.compositor.get_display())
            self.subsurface_facades[wid] = facade
        else:
            facade.update_dimensions(logical_w, logical_h)
        facade.set_image(image)
        # this damage has to reach a client before the child's frame callback can be
        # answered - marking it before the damage, which can be sent synchronously:
        facade.mark_damage_frame_pending()
        for ss in self.window_sources():
            sub_ws = ss.make_subsurface_source(wid, parent_wid, ox, oy, facade,
                                               logical_w, logical_h, native_w, native_h)
            sub_ws.damage(0, 0, logical_w, logical_h, {})

    def subsurface_empty_commit(self, wid: int) -> None:
        # the child asked for another frame callback without damaging anything:
        # answer it here, since no damage will come through to do it for us
        facade = self.subsurface_facades.get(wid)
        log("subsurface-empty-commit: wid=%i, facade=%s", wid, facade)
        if facade:
            facade.schedule_empty_acknowledgement()

    def surface_snapshot(self, wid: int, image: ImageWrapper | None) -> None:
        """Copy one borrowed WSSO toplevel snapshot generation.

        Popups deliberately retain the WIS-owned ``surface_image`` path.
        """
        window = self.get_window(wid)
        if not window:
            log("surface-snapshot: unknown toplevel wid=%#x, dropping", wid)
            return
        # The native capture owner catches failures and keeps capture pending.
        # Swallowing them here would claim success after the model invalidated
        # its snapshot, stranding a static root on later empty commits.
        replace_image_snapshot(window, image)

    def _prepare_subsurface_snapshot(
            self, wid: int, logical_w: int, logical_h: int,
            native_w: int, native_h: int, colourspace,
            image: ImageWrapper | None, *, replace: bool = True,
    ) -> SubsurfaceWindow:
        """Prepare model content before exposing its topology to any client."""
        try:
            facade = self.subsurface_facades.get(wid)
            if facade is None:
                facade = SubsurfaceWindow(logical_w, logical_h, has_alpha=True, depth=32,
                                          surface=self.subsurfaces.get(wid),
                                          display=self.server.compositor.get_display())
                self.subsurface_facades[wid] = facade
            else:
                facade.replace_dimensions(logical_w, logical_h)
            facade.replace_colourspace_snapshot(colourspace)
            if replace:
                replace_image_snapshot(facade, image)
            elif image is not None:
                # Defensive ownership boundary: role reattachment normally carries
                # no image because the wl_surface-owned snapshot is already held.
                replace_image_snapshot(facade, image)
            return facade
        except BaseException:
            # The callback wrapper remains borrowed on every path; invalidate
            # only retained model state and leave the native emitter to free
            # its owner after synchronous callbacks return.
            failed_facade = self.subsurface_facades.get(wid)
            if failed_facade is not None:
                try:
                    failed_facade.clear_image()
                except BaseException:
                    log.error("Error invalidating failed Wayland subsurface %#x snapshot",
                              wid, exc_info=True)
            raise

    def subsurface_commit(
            self, wid: int, root_wid: int, mapped: bool, has_buffer: bool,
            surface_tree: Sequence[tuple[int, int, int, int, int, int, int]],
            damage_rects: Sequence[tuple[int, int, int, int]],
            image: ImageWrapper | None,
            logical_w: int, logical_h: int, native_w: int, native_h: int,
            colourspace,
    ) -> None:
        """Atomically install one child generation and reconcile its root."""
        if wid not in self.subsurfaces:
            log("subsurface-commit: unknown surface wid=%#x, dropping", wid)
            return
        try:
            self._apply_subsurface_commit(
                wid, root_wid, mapped, has_buffer,
                surface_tree, damage_rects, image,
                logical_w, logical_h, native_w, native_h, colourspace,
            )
        finally:
            # Frame callbacks belong to the compositor-side surface commit,
            # not to any one Xpra connection.  Acknowledge after the complete
            # generation has either been installed or invalidated, exactly
            # once even when reconciliation fails part-way through.
            surface = self.subsurfaces.get(wid)
            if surface is not None:
                try:
                    surface.frame_done()
                except BaseException:
                    log.error("Error acknowledging Wayland subsurface %#x commit",
                              wid, exc_info=True)
                finally:
                    compositor = getattr(self.server, "compositor", None)
                    if compositor is not None:
                        try:
                            compositor.flush()
                        except BaseException:
                            # Flushing is downstream of the frame-callback
                            # ownership boundary.  A compositor transport
                            # failure must not escape the native ``noexcept``
                            # commit emitter or obscure reconciliation errors.
                            log.error(
                                "Error flushing acknowledged Wayland subsurface %#x commit",
                                wid, exc_info=True,
                            )

    def _apply_subsurface_commit(
            self, wid: int, root_wid: int, mapped: bool, has_buffer: bool,
            surface_tree: Sequence[tuple[int, int, int, int, int, int, int]],
            damage_rects: Sequence[tuple[int, int, int, int]],
            image: ImageWrapper | None,
            logical_w: int, logical_h: int, native_w: int, native_h: int,
            colourspace,
    ) -> None:
        if wid not in self.subsurfaces:
            log("subsurface-commit: unknown surface wid=%#x, dropping", wid)
            return
        try:
            self._prepare_subsurface_snapshot(
                wid, logical_w, logical_h, native_w, native_h,
                colourspace, image,
            )
        except BaseException:
            log.error("Error preparing Wayland subsurface %#x commit", wid, exc_info=True)
        affected_roots = set()
        old_info = self.subsurface_info.get(wid)
        if old_info:
            affected_roots.add(old_info[0])
        candidate_roots = set(affected_roots)
        if root_wid:
            candidate_roots.add(root_wid)
        old_signatures = {
            candidate_root: self._subsurface_topology_signature(candidate_root)
            for candidate_root in candidate_roots
        }
        old_errors = {
            candidate_root: self.subsurface_topology_errors.get(candidate_root)
            for candidate_root in candidate_roots
        }
        if root_wid and self.get_window(root_wid):
            affected_roots.update(self._update_subsurface_topology(root_wid, surface_tree, reconcile=False))
            if not mapped and wid in self.subsurface_info:
                affected_roots.update(self._deactivate_subsurface_tree(wid, reconcile=False))
        else:
            affected_roots.update(self._deactivate_subsurface_tree(wid, reconcile=False))
        sources = self._subsurface_window_sources()
        handled_sources_by_root = {}
        for affected_root in affected_roots:
            topology_changed = (
                old_signatures.get(affected_root) != self._subsurface_topology_signature(affected_root)
                or old_errors.get(affected_root) != self.subsurface_topology_errors.get(affected_root)
            )
            handled_sources_by_root[affected_root] = self._reconcile_subsurface_root(
                affected_root, full_damage=topology_changed, sources=sources,
            )
        if mapped and has_buffer and damage_rects and root_wid in affected_roots:
            self._damage_subsurface_regions(
                wid, root_wid, damage_rects, sources,
                handled_sources_by_root.get(root_wid, set()),
            )

    @staticmethod
    def update_colourspace(window, surface) -> None:
        # the `wp_color_management_surface_v1` tag is double buffered state,
        # so it only takes effect when the surface is committed:
        if surface:
            window._updateprop("colourspace", surface.get_colourspace())

    @staticmethod
    def update_content_types(window, surface) -> None:
        # wp_content_type_v1 state is double-buffered and changes on commit.
        if surface:
            window._updateprop("content-types", surface.get_content_types())

    @staticmethod
    def update_opaque_region(window, surface) -> None:
        if surface:
            window._updateprop("opaque-region", surface.get_opaque_region())

    def update_size(self, window, size: tuple[int, int]) -> None:
        old_geom = window.get_property("geometry")
        w, h = size
        if old_geom[2] == w and old_geom[3] == h:
            return
        geom = (old_geom[0], old_geom[1], w, h)
        window._updateprop("geometry", geom)
        if (old_geom[2] == old_geom[3] == 0) and size[0] and size[1]:
            self._do_send_new_window_packet(WINDOW_CREATE, window, geom)

    @staticmethod
    def update_geometry(window, position: tuple[int, int], size: tuple[int, int]) -> None:
        old_geom = window.get_property("geometry")
        x, y = position
        w, h = size
        if w <= 0 or h <= 0:
            return
        geom = (x, y, w, h)
        if old_geom == geom:
            return
        window._updateprop("geometry", geom)
        window._updateprop("relative-position", position)

    def popup_map(self, wid: int, position: tuple[int, int], size: tuple[int, int]) -> None:
        window = self.get_window(wid)
        if not window:
            popup_info = self.pending_popups.get(wid)
            if not popup_info:
                log.warn("Warning: cannot map popup window %i: not found!", wid)
                return
            parent_wid, popup = popup_info
            window = self._ensure_popup_window(parent_wid, popup, position, size)
            if not window:
                return
        window._updateprop("iconic", False)
        self.update_geometry(window, position, size)

    def popup_commit(self, wid: int, mapped: bool,
                     position: tuple[int, int], size: tuple[int, int],
                     has_image: bool) -> None:
        log(f"popup commit wid {wid} {mapped=}, {position=}, {size=}, {has_image=}")
        window = self.get_window(wid)
        if not window:
            popup_info = self.pending_popups.get(wid)
            if not popup_info:
                return
            parent_wid, popup = popup_info
            window = self._ensure_popup_window(parent_wid, popup, position, size)
            if not window:
                return
        surface = self.get_surface(wid)
        self.update_colourspace(window, surface)
        self.update_content_types(window, surface)
        self.update_geometry(window, position, size)
        if mapped and has_image:
            w, h = size
            if w > 0 and h > 0:
                self.refresh_window_area(window, 0, 0, w, h, options={"damage": True})

    def popup_reposition(self, wid: int, position: tuple[int, int]) -> None:
        window = self.get_window(wid)
        if window:
            self.update_geometry(window, position, window.get_property("geometry")[2:4])

    def popup_destroy(self, wid: int) -> None:
        self.pending_popups.pop(wid, None)
        self.destroy(wid)

    def destroy(self, wid: int) -> None:
        # The wlroots xdg surface is being freed. Drop every server-side
        # surface reference before delayed focus or encoder callbacks can use it.
        if self._destroy_surface_tree_state(wid):
            return
        if wid in self.subsurface_info:
            self.subsurface_info.pop(wid, None)
            if facade := self.subsurface_facades.pop(wid, None):
                # drop the frame timeout: it holds a reference to the facade
                facade.unmanage()
            for ss in self.window_sources():
                ss.cleanup_subsurface_source(wid)
        window = self.get_window(wid)
        if window is not None:
            surface = window.get_property("surface")
            if surface:
                self.toplevel_wid.pop(getattr(surface, "toplevel_ptr", 0), 0)
            window._internal_set_property("surface", None)
            window.unmanage()
            if not isinstance(surface, Subsurface):
                self._remove_wid(wid)
        if self.focused == wid:
            self.focused = 0
        if self.pointer_focus == wid:
            self.pointer_focus = 0

    def _destroy_surface_tree_state(self, wid: int) -> bool:
        """Finalize a WSSO-known surface without leaking partial state."""
        if wid in self.subsurfaces:
            self._forget_subsurface(wid)
            return True
        window = self.get_window(wid)
        has_tree_state = bool(
            wid in self.subsurface_info
            or wid in self.subsurface_facades
            or wid in self.subsurface_parents
            or wid in self.subsurface_stacking
            or wid in self.subsurface_topology_errors
            or self._direct_subsurface_children(wid)
        )
        if window is None and not has_tree_state:
            return False
        for child_wid in self._direct_subsurface_children(wid):
            self._deactivate_subsurface_tree(child_wid)
            self.subsurface_parents.pop(child_wid, None)
        self._clear_pointer_focus(wid)
        self.subsurface_stacking.pop(wid, None)
        self.subsurface_topology_errors.pop(wid, None)
        if window is not None:
            surface = None
            try:
                surface = window.get_property("surface")
                if surface:
                    self.toplevel_wid.pop(getattr(surface, "toplevel_ptr", 0), 0)
            except BaseException:
                log.error("Error reading destroyed Wayland window %#x", wid, exc_info=True)
            try:
                window._internal_set_property("surface", None)
            except BaseException:
                log.error("Error detaching destroyed Wayland window %#x", wid, exc_info=True)
            try:
                window.unmanage()
            except BaseException:
                log.error("Error unmanaging destroyed Wayland window %#x", wid, exc_info=True)
            if not isinstance(surface, Subsurface):
                try:
                    self._remove_wid(wid)
                except BaseException:
                    log.error("Error removing destroyed Wayland window %#x", wid, exc_info=True)
        if self.focused == wid:
            self.focused = 0
        if self.pointer_focus == wid:
            self.pointer_focus = 0
        return True

    def _direct_subsurface_children(self, wid: int) -> tuple[int, ...]:
        return tuple(child_wid for child_wid, parent_wid in self.subsurface_parents.items()
                     if parent_wid == wid)

    def _deactivate_subsurface(self, wid: int, *, discard_image: bool = False) -> None:
        """Drop active encoders while retaining remappable native state."""
        # Role loss and reparent retain the wrapper and WID, but the currently
        # focused leaf is no longer part of the active tree. Clear the wl_seat
        # focus while its wl_surface pointer is still valid.
        self._clear_pointer_focus(wid)
        info = self.subsurface_info.pop(wid, None)
        facade = self.subsurface_facades.get(wid)
        if info is None and not discard_image:
            return
        if info is not None:
            for ss in self._subsurface_window_sources():
                try:
                    ss.cleanup_subsurface_source(wid)
                except BaseException:
                    # This path is entered from a wlroots `noexcept` callback.
                    # Every connection must be attempted and no Python exception
                    # may prevent native listener/registry finalization.
                    log.error("Error cleaning Wayland subsurface %#x from %s", wid, ss, exc_info=True)
        if discard_image and facade:
            self.subsurface_facades.pop(wid, None)
            try:
                facade.clear_image()
            except BaseException:
                log.error("Error releasing Wayland subsurface %#x image", wid, exc_info=True)
            try:
                # Release the terminal child surface reference held by the
                # upstream frame-callback model; WSSO never arms its timers.
                facade.unmanage()
            except BaseException:
                log.error("Error unmanaging Wayland subsurface %#x facade", wid, exc_info=True)

    def _deactivate_subsurface_tree(self, wid: int, *, reconcile: bool = True) -> set[int]:
        """Detach a branch for unmap or reparent while retaining wrappers."""
        branch = {wid}
        pending = [wid]
        visited = set()
        while pending:
            parent_wid = pending.pop()
            if parent_wid in visited:
                continue
            visited.add(parent_wid)
            children = self._direct_subsurface_children(parent_wid)
            branch.update(children)
            pending.extend(children)
        changed_roots = []
        ordered = []
        for root_wid, order in tuple(self.subsurface_stacking.items()):
            ordered.extend(child_wid for child_wid in reversed(order) if child_wid in branch)
            new_order = tuple(child_wid for child_wid in order if child_wid not in branch)
            if new_order != order:
                self.subsurface_stacking[root_wid] = new_order
                changed_roots.append(root_wid)
        ordered.extend(branch - set(ordered))
        for child_wid in ordered:
            self._deactivate_subsurface(child_wid)
        changed = set(changed_roots)
        if reconcile:
            for root_wid in changed:
                self._reconcile_subsurface_root(root_wid)
        return changed

    def _clear_subsurface_image(self, wid: int) -> None:
        """Invalidate one committed snapshot without ending surface identity."""
        facade = self.subsurface_facades.get(wid)
        if facade:
            try:
                facade.clear_image()
            except BaseException:
                log.error("Error invalidating Wayland subsurface %#x image", wid, exc_info=True)

    def _forget_subsurface(self, wid: int) -> None:
        """Forget one terminal wl_surface while retaining live descendants."""
        direct_children = self._direct_subsurface_children(wid)
        self._deactivate_subsurface_tree(wid)
        # Descendant wl_surfaces have independent lifetimes.  Their current
        # roles are no longer attached through this terminal parent, but their
        # wrappers, nested relationships and retained snapshots may be reused.
        for child_wid in direct_children:
            if self.subsurface_parents.get(child_wid) == wid:
                self.subsurface_parents.pop(child_wid, None)
        self._deactivate_subsurface(wid, discard_image=True)
        self.subsurfaces.pop(wid, None)
        self.subsurface_parents.pop(wid, None)
        changed_roots = []
        for root_wid, order in tuple(self.subsurface_stacking.items()):
            if wid not in order:
                continue
            new_order = tuple(item for item in order if item != wid)
            if root_wid == wid:
                self.subsurface_stacking.pop(root_wid, None)
            else:
                self.subsurface_stacking[root_wid] = new_order
                changed_roots.append(root_wid)
        for root_wid in changed_roots:
            self._reconcile_subsurface_root(root_wid)

    def _remove_subsurface(self, wid: int) -> None:
        """Compatibility entry point for permanent child removal."""
        self._forget_subsurface(wid)

    def subsurface_role_destroy(self, wid: int) -> None:
        """Detach a wl_subsurface role while retaining its wl_surface state."""
        if wid not in self.subsurfaces:
            return
        self._deactivate_subsurface_tree(wid)
        self.subsurface_parents.pop(wid, None)

    @staticmethod
    def _supports_subsurface_composite(ss) -> bool:
        supported = getattr(ss, "supports_subsurface_composite", None)
        return bool(supported and supported())

    def _subsurface_window_sources(self) -> tuple:
        try:
            return tuple(self.window_sources())
        except BaseException:
            # Native commit/destroy callbacks must still finish their own
            # bookkeeping if connection enumeration is already tearing down.
            log.error("Error enumerating window sources for Wayland subsurface state", exc_info=True)
            return ()

    def _root_composite_state(self, root_wid: int):
        """Return one validated, connection-independent root snapshot."""
        order = self.subsurface_stacking.get(root_wid, ())
        parent_window = self.get_window(root_wid)
        if not parent_window:
            return None, order, (), "root model is unavailable"
        if topology_error := self.subsurface_topology_errors.get(root_wid):
            return parent_window, order, (), topology_error
        try:
            geometries = tuple(
                (child_wid, *self.subsurface_info[child_wid][1:])
                for child_wid in order
                if child_wid != root_wid
                and child_wid in self.subsurface_info
                and self.subsurface_info[child_wid][0] == root_wid
            )
            active_children = tuple(child_wid for child_wid in order if child_wid != root_wid)
            if not active_children:
                return parent_window, order, geometries, ""
            if len(geometries) != len(active_children):
                return parent_window, order, geometries, "active child geometry is incomplete"
            has_root_image = getattr(parent_window, "has_image", None)
            if not has_root_image or not has_root_image():
                return parent_window, order, geometries, "active root snapshot is unavailable"
            root_colourspace = canonical_colourspace(parent_window.get_property("colourspace"))
            if root_colourspace is None:
                return parent_window, order, geometries, "root colourspace is not canonical"
            for child_wid in active_children:
                facade = self.subsurface_facades.get(child_wid)
                if facade is None or not facade.has_image():
                    return parent_window, order, geometries, f"child {child_wid:#x} snapshot is unavailable"
                child_colourspace = canonical_colourspace(facade.get_colourspace_snapshot())
                if child_colourspace is None:
                    return parent_window, order, geometries, f"child {child_wid:#x} colourspace is not canonical"
                if child_colourspace != root_colourspace:
                    return parent_window, order, geometries, f"child {child_wid:#x} colourspace is mixed"
            return parent_window, order, geometries, ""
        except BaseException:
            log.error("Error validating Wayland composite root %#x", root_wid, exc_info=True)
            return parent_window, order, (), "root composite state is invalid"

    def _subsurface_topology_signature(self, root_wid: int) -> tuple:
        order = self.subsurface_stacking.get(root_wid, ())
        return order, tuple(
            (child_wid, self.subsurface_info.get(child_wid))
            for child_wid in order if child_wid != root_wid
        )

    def _damage_subsurface_regions(self, wid: int, root_wid: int, rects,
                                   sources, already_damaged) -> None:
        """Translate effective child damage into the root backing canvas."""
        info = self.subsurface_info.get(wid)
        parent_window = self.get_window(root_wid)
        if not info or info[0] != root_wid or not parent_window:
            return
        _root, offset_x, offset_y, logical_w, logical_h, _native_w, _native_h = info
        try:
            _x, _y, root_w, root_h = parent_window.get_property("geometry")
            clipped = []
            for x, y, width, height in rects:
                child_x1 = max(0, x)
                child_y1 = max(0, y)
                child_x2 = min(logical_w, x + width)
                child_y2 = min(logical_h, y + height)
                root_x1 = max(0, offset_x + child_x1)
                root_y1 = max(0, offset_y + child_y1)
                root_x2 = min(root_w, offset_x + child_x2)
                root_y2 = min(root_h, offset_y + child_y2)
                if root_x2 > root_x1 and root_y2 > root_y1:
                    clipped.append((root_x1, root_y1, root_x2 - root_x1, root_y2 - root_y1))
        except BaseException:
            log.error("Error translating Wayland subsurface %#x damage", wid, exc_info=True)
            for ss in sources:
                try:
                    self._refuse_source_root(ss, root_wid, parent_window, "invalid child damage")
                except BaseException:
                    log.error("Error refusing invalid Wayland child damage on %s", ss, exc_info=True)
            return
        for ss in sources:
            if ss in already_damaged:
                continue
            try:
                if self._source_refused(ss, root_wid, parent_window):
                    continue
                for index, (x, y, width, height) in enumerate(clipped):
                    ss.damage(
                        root_wid, parent_window, x, y, width, height,
                        {"damage": True, "more": index != len(clipped) - 1},
                    )
            except BaseException:
                log.error("Error publishing Wayland subsurface %#x damage on %s",
                          wid, ss, exc_info=True)
                try:
                    self._refuse_source_root(ss, root_wid, parent_window, "child damage failed")
                except BaseException:
                    log.error("Error refusing failed Wayland child damage on %s", ss, exc_info=True)

    @staticmethod
    def _source_refused(ss, root_wid: int, window) -> bool:
        return bool(ss.is_window_refused(root_wid, window))

    @staticmethod
    def _source_announced(ss, root_wid: int, window) -> bool:
        return bool(ss.is_window_announced(root_wid, window))

    @staticmethod
    def _refuse_source_root(ss, root_wid: int, window, reason: str) -> None:
        ss.refuse_window(root_wid, window, reason)

    @staticmethod
    def _allow_source_root(ss, root_wid: int, window) -> bool:
        return bool(ss.allow_window(root_wid, window))

    @staticmethod
    def _source_accepts_root_damage(ss, root_wid: int, window) -> bool:
        if not ss.is_window_announced(root_wid, window):
            return False
        accepts = getattr(ss, "can_consume_window_damage", None)
        return bool(accepts(root_wid, window)) if accepts else True

    def _prepare_reconciliation_damage(
            self, root_wid: int, window, composite_owned: bool,
            mapped_root: bool | None,
    ) -> bool:
        """Join the model's frame guard before the first repair request for one root."""
        self._cancel_ordinary_empty_damage_ack(window)
        if composite_owned:
            return False
        mapped = mapped_root
        if mapped is None:
            mapped = not bool(window.get_property("iconic"))
        if not mapped:
            return False
        # The upstream frame model has no public predicate: its damage guard is
        # the pending safety timer which `mark_damage_frame_pending` arms.
        already_pending = bool(window._damage_frame_timer)
        self._mark_ordinary_damage_frame_pending(window)
        return not already_pending

    @staticmethod
    def _rollback_reconciliation_damage(window, acquired_guard: bool) -> None:
        """Release only a guard acquired for a repair which accepted no work."""
        if acquired_guard:
            window.cancel_damage_frame_timer()

    def _announce_source_root(self, ss, root_wid: int, window) -> None:
        if self._source_announced(ss, root_wid, window):
            return
        x, y, width, height = window.get_property("geometry")
        if width <= 0 or height <= 0:
            return
        properties = self.client_properties.get(root_wid, {}).get(ss.uuid, {})
        ss.new_window(WINDOW_CREATE, root_wid, window, x, y, width, height, properties)

    def _materialize_source_root(self, ss, root_wid: int, parent_window,
                                 geometries, order) -> None:
        for child_wid, offset_x, offset_y, logical_w, logical_h, native_w, native_h in geometries:
            facade = self.subsurface_facades[child_wid]
            sub_ws = ss.make_subsurface_source(
                child_wid, root_wid, offset_x, offset_y, facade,
                logical_w, logical_h, native_w, native_h,
                parent_window=parent_window,
            )
            if sub_ws is None:
                raise RuntimeError(f"failed to materialize child {child_wid:#x}")
        ss.update_subsurface_geometries(root_wid, geometries, order)

    def _reconcile_subsurface_root(self, root_wid: int, *, full_damage: bool = False,
                                   sources=None, initial: bool = False,
                                   repaired_sources: set | None = None,
                                   mapped_root: bool | None = None) -> set:
        """Converge each client independently on one eligible root state."""
        parent_window, order, geometries, state_error = self._root_composite_state(root_wid)
        if not parent_window:
            return set()
        active_children = bool(tuple(child_wid for child_wid in order if child_wid != root_wid))
        source_list = tuple(sources) if sources is not None else self._subsurface_window_sources()
        # Sources returned here have already received a full repair or must not
        # receive ordinary damage because this generation was refused/failed.
        handled_sources = set()
        damage_prepared = False
        damage_accepted = False
        acquired_guard = False
        for ss in source_list:
            try:
                reason = state_error
                if active_children and not self._supports_subsurface_composite(ss):
                    reason = "client has no subsurface composite capability"
                if reason:
                    self._refuse_source_root(ss, root_wid, parent_window, reason)
                    handled_sources.add(ss)
                    continue
                was_refused = self._source_refused(ss, root_wid, parent_window)
                if active_children:
                    self._materialize_source_root(ss, root_wid, parent_window, geometries, order)
                if was_refused and not self._allow_source_root(ss, root_wid, parent_window):
                    raise RuntimeError(f"failed to allow recovered root {root_wid:#x}")
                if initial:
                    continue
                was_announced = self._source_announced(ss, root_wid, parent_window)
                self._announce_source_root(ss, root_wid, parent_window)
                if full_damage or was_refused or not was_announced:
                    _x, _y, width, height = parent_window.get_property("geometry")
                    if width > 0 and height > 0:
                        if not self._source_accepts_root_damage(ss, root_wid, parent_window):
                            handled_sources.add(ss)
                            continue
                        if not damage_prepared:
                            acquired_guard = self._prepare_reconciliation_damage(
                                root_wid, parent_window, active_children, mapped_root,
                            )
                            damage_prepared = True
                        accepted = ss.damage(
                            root_wid, parent_window, 0, 0, width, height, {"damage": True},
                        )
                        handled_sources.add(ss)
                        if accepted is not False:
                            damage_accepted = True
                        if accepted is not False and repaired_sources is not None:
                            repaired_sources.add(ss)
            except BaseException:
                handled_sources.add(ss)
                log.error("Error reconciling Wayland subsurface root %#x on %s",
                          root_wid, ss, exc_info=True)
                try:
                    self._refuse_source_root(ss, root_wid, parent_window, "reconciliation failed")
                except BaseException:
                    log.error("Error refusing failed Wayland root %#x on %s",
                              root_wid, ss, exc_info=True)
        if damage_prepared and not damage_accepted:
            try:
                self._rollback_reconciliation_damage(parent_window, acquired_guard)
            except BaseException:
                log.error("Error rolling back Wayland root %#x damage guard",
                          root_wid, exc_info=True)
        return handled_sources

    def _update_subsurface_topology(
            self, root_wid: int,
            surface_tree: Sequence[tuple[int, int, int, int, int, int, int]],
            *, reconcile: bool = True,
    ) -> set[int]:
        """Install one authoritative mapped wlroots tree.

        `surface_tree` is already in compositor paint order and contains the
        root marker.  The order is never reconstructed from creation order or
        dictionaries because doing so would lose below-parent layers.
        """
        tree = []
        seen = set()
        try:
            for entry in surface_tree:
                if len(entry) != 7:
                    return self._invalidate_subsurface_topology(
                        root_wid, f"invalid surface-tree entry {entry!r}", reconcile,
                    )
                child_wid, offset_x, offset_y, logical_w, logical_h, native_w, native_h = entry
                if child_wid in seen:
                    return self._invalidate_subsurface_topology(
                        root_wid, f"duplicate surface {child_wid:#x}", reconcile,
                    )
                if min(logical_w, logical_h, native_w, native_h) < 0:
                    return self._invalidate_subsurface_topology(
                        root_wid, f"invalid surface geometry {entry!r}", reconcile,
                    )
                seen.add(child_wid)
                tree.append((child_wid, offset_x, offset_y, logical_w, logical_h, native_w, native_h))
        except BaseException as e:
            return self._invalidate_subsurface_topology(
                root_wid, f"unreadable surface tree: {e}", reconcile,
            )
        order = tuple(entry[0] for entry in tree)
        if order.count(root_wid) != 1:
            return self._invalidate_subsurface_topology(
                root_wid, f"surface tree has no unique root marker: {order!r}", reconcile,
            )

        self.subsurface_topology_errors.pop(root_wid, None)

        new_children = set(order)
        new_children.discard(root_wid)
        previous_children = {
            child_wid for child_wid, info in self.subsurface_info.items()
            if info[0] == root_wid
        }
        # Upper layers are detached first. Their cleanup may enqueue repair,
        # but the final authoritative topology below determines replay order.
        previous_order = self.subsurface_stacking.get(root_wid, ())
        affected_roots = {root_wid}
        detach_order = tuple(reversed(previous_order)) + tuple(previous_children - set(previous_order))
        for child_wid in detach_order:
            if child_wid != root_wid and child_wid not in new_children:
                self._deactivate_subsurface(child_wid)

        for child_wid, offset_x, offset_y, logical_w, logical_h, native_w, native_h in tree:
            if child_wid == root_wid:
                continue
            old_info = self.subsurface_info.get(child_wid)
            if old_info and old_info[0] != root_wid:
                old_root = old_info[0]
                self._deactivate_subsurface(child_wid)
                old_order = self.subsurface_stacking.get(old_root, ())
                self.subsurface_stacking[old_root] = tuple(item for item in old_order if item != child_wid)
                affected_roots.add(old_root)
            self.subsurface_info[child_wid] = (
                root_wid, offset_x, offset_y,
                logical_w, logical_h, native_w, native_h,
            )
            if facade := self.subsurface_facades.get(child_wid):
                try:
                    facade.replace_dimensions(logical_w, logical_h)
                except BaseException:
                    log.error("Error updating Wayland subsurface facade %#x", child_wid, exc_info=True)

        self.subsurface_stacking[root_wid] = order
        if reconcile:
            for affected_root in affected_roots:
                self._reconcile_subsurface_root(affected_root)
        return affected_roots

    def _invalidate_subsurface_topology(self, root_wid: int, reason: str,
                                        reconcile: bool) -> set[int]:
        """Refuse a malformed authoritative generation without partial install."""
        message = f"invalid authoritative Wayland topology: {reason}"
        log.error("Error: %s for root %#x", message, root_wid)
        self.subsurface_topology_errors[root_wid] = message
        if reconcile:
            self._reconcile_subsurface_root(root_wid)
        return {root_wid}

    def send_initial_windows(self, ss, sharing=False) -> None:
        # Refusal and composite preparation happen before the base path can
        # announce any root to this late client.  The base method then owns the
        # single canonical WINDOW_CREATE plus its one initial full damage.
        for parent_wid in tuple(self.subsurface_stacking):
            self._reconcile_subsurface_root(
                parent_wid, sources=(ss,), initial=True,
            )
        super().send_initial_windows(ss, sharing)

    def set_parent(self, wid: int, parent_wid: int) -> None:
        log("set_parent: wid=%i, parent_wid=%i", wid, parent_wid)
        window = self.get_window(wid)
        if not window:
            return
        window._updateprop("parent", parent_wid)
        window._updateprop("transient-for", parent_wid)

    def new_subsurface(self, wid: int, subsurface: Subsurface,
                       width: int, height: int, native_width: int = 0, native_height: int = 0,
                       root_wid: int = 0,
                       surface_tree: Sequence[tuple[int, int, int, int, int, int, int]] = (),
                       initial_image: ImageWrapper | None = None,
                       colourspace=None) -> None:
        log.info("new subsurface of %i: %s %ix%i native=%ix%i",
                 wid, subsurface, width, height, native_width, native_height)
        child_wid = subsurface.wid
        existing = self.subsurfaces.get(child_wid)
        new_identity = existing is not subsurface
        affected_roots = set()
        if existing is subsurface:
            if self.subsurface_parents.get(child_wid) != wid:
                affected_roots.update(self._deactivate_subsurface_tree(child_wid, reconcile=False))
                self.subsurface_parents[child_wid] = wid
        else:
            if existing is not None:
                self._forget_subsurface(child_wid)
            self.subsurfaces[child_wid] = subsurface
            self.subsurface_parents[child_wid] = wid
            self._register_surface_events(subsurface, PER_SUBSURFACE_EVENTS)
        try:
            # Store the first roleless-buffer snapshot and colourspace before
            # making its mapped topology observable to any connection.
            self._prepare_subsurface_snapshot(
                child_wid, width, height,
                native_width or width, native_height or height,
                colourspace, initial_image,
                replace=new_identity,
            )
        except BaseException:
            log.error("Error preparing new Wayland subsurface %#x", child_wid, exc_info=True)
        # Role attachment alone restores a static buffer and nested branch;
        # neither the child nor its descendants need another commit.
        if root_wid and self.get_window(root_wid):
            # Native emitters always provide an authoritative tree when they
            # provide a root identity.  An empty tuple is therefore the
            # fail-closed result of an unregistered mapped wl_surface, not an
            # instruction to retain the previous (now incomplete) topology.
            affected_roots.update(self._update_subsurface_topology(
                root_wid, surface_tree, reconcile=False,
            ))
        for affected_root in affected_roots:
            self._reconcile_subsurface_root(affected_root, full_damage=True)

    def move(self, wid: int, serial: int) -> None:
        log(f"move wid {wid}, serial={serial:#x}")
        window = self.get_window(wid)
        if not window:
            log.warn("Warning: cannot move window %i: not found!", wid)
            return
        wsources = self.window_sources()
        if not wsources:
            return
        driversources = [ss for ss in wsources if self.server.ui_driver == ss.uuid]
        source = driversources[0] if driversources else wsources[0]
        x_root, y_root = self.server.subsystems["pointer"].pointer_device.get_position()
        source.initiate_moveresize(wid, window, x_root, y_root, int(MoveResize.MOVE), 1,
                                   SOURCE_INDICATION_NORMAL)

    def resize(self, wid: int, serial: int, moveresize: int) -> None:
        log(f"resize wid {wid:#x}, serial={serial:#x}, moveresize={moveresize}")
        window = self.get_window(wid)
        if not window:
            log.warn("Warning: cannot resize window %i: not found!", wid)
            return
        wsources = self.window_sources()
        if not wsources:
            return
        driversources = [ss for ss in wsources if self.server.ui_driver == ss.uuid]
        source = driversources[0] if driversources else wsources[0]
        x_root, y_root = self.server.subsystems["pointer"].pointer_device.get_position()
        source.initiate_moveresize(wid, window, x_root, y_root, int(moveresize), 1,
                                   SOURCE_INDICATION_NORMAL)

    def _process_map(self, proto, packet: Packet) -> None:
        wid = packet.get_wid()
        window = self.get_window(wid)
        surface = self.get_surface(wid)
        if not (window and surface):
            return
        w = packet.get_i16(4)
        h = packet.get_i16(5)
        cp = packet.get_dict(6) if len(packet) >= 7 else {}
        cp["event"] = "map"
        self._set_client_properties(proto, wid, window, cp)
        surface.resize(w, h)
        self.server.compositor.flush()
        self.refresh_window(window)

    def do_process_window_configure(self, proto, wid, config: typedict) -> None:
        window = self.get_window(wid)
        surface = self.get_surface(wid)
        if not (window and surface):
            return
        properties = config.dictget("properties")
        if properties:
            log("window client properties updates: %s", properties)
            self._set_client_properties(proto, wid, window, properties)
        geometry = config.inttupleget("geometry")
        if geometry:
            w, h = geometry[2:4]
            surface.resize(w, h)
            surface.frame_done()
            self.server.compositor.flush()

    def _focus(self, _server_source, wid: int, modifiers) -> None:
        server = self.server
        focuslog("_focus(%s, %s) current focus=%i", wid, modifiers, self.focused)
        keyboard = server.subsystems.get("keyboard")
        if modifiers is not None and keyboard:
            keyboard.update_keyboard_modifiers(modifiers)
        if self.focused == wid:
            return
        for window_id, state in {
            self.focused: False,
            wid: True,
        }.items():
            if not window_id:
                if state and keyboard and keyboard.device:
                    keyboard.device.focus(0)
                continue
            window = self.get_window(window_id)
            surface = self.get_surface(window_id)
            focuslog("focus: wid=%#x, state=%s, window=%s, surface=%s", window_id, state, window, surface)
            if window and surface:
                surface.focus(state)
                if state and (ptr := surface.xdg_surface_ptr):
                    if keyboard and keyboard.device:
                        keyboard.device.focus(ptr)
        self.focused = wid
        server.compositor.flush()

    def get_focus(self) -> int:
        return self.focused
