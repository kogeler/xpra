# This file is part of Xpra.
# Copyright (C) 2025 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

# cython: language_level=3

from typing import Dict

from xpra.log import Logger
from xpra.constants import MoveResize
from xpra.wayland.server.models.window import image_for_xdg_geometry, xdg_root_damage

from libc.stdint cimport uintptr_t, uint32_t, int32_t

from xpra.wayland.server.wayland_surface cimport WaylandSurface, next_wid, get_damage_areas
from xpra.wayland.server.subsurface cimport Subsurface, undiscovered_subsurfaces
# `surfaces` is the shared registry (Python dict) defined in wayland_surface.pyx
from xpra.wayland.server.wayland_surface import surfaces


# Import definitions from .pxd file
from xpra.wayland.server.wlroots cimport (
    wl_listener,
    wlr_subsurface, wlr_surface_for_each_surface,
    wlr_surface, wlr_box,
    wlr_surface_get_effective_damage, wlr_surface_has_buffer,
    WLR_SURFACE_STATE_TRANSFORM, WLR_SURFACE_STATE_SCALE,
    WLR_SURFACE_STATE_VIEWPORT,
    wlr_xdg_toplevel, wlr_xdg_surface,
    wlr_xdg_toplevel_decoration_v1,
    wlr_xdg_toplevel_move_event, wlr_xdg_toplevel_resize_event, wlr_xdg_toplevel_show_window_menu_event,
    wlr_xdg_toplevel_set_size, wlr_xdg_toplevel_set_activated,
    wlr_xdg_toplevel_decoration_v1_set_mode, WLR_XDG_TOPLEVEL_DECORATION_V1_MODE_SERVER_SIDE,
    wlr_xdg_surface_schedule_configure,
    WLR_XDG_SURFACE_ROLE_TOPLEVEL,
    WLR_EDGE_TOP, WLR_EDGE_BOTTOM, WLR_EDGE_LEFT, WLR_EDGE_RIGHT,
)
from xpra.wayland.server.pixman cimport (
    pixman_region32_t, pixman_box32_t, pixman_region32_rectangles,
    pixman_region32_init, pixman_region32_fini,
)


EDGES: Dict[int, str] = {
    WLR_EDGE_TOP: "TOP",
    WLR_EDGE_BOTTOM: "BOTTOM",
    WLR_EDGE_LEFT: "LEFT",
    WLR_EDGE_RIGHT: "RIGHT",
}

EDGES_MAP: Dict[int, MoveResize] = {
    WLR_EDGE_TOP: MoveResize.SIZE_TOP,
    WLR_EDGE_TOP | WLR_EDGE_LEFT: MoveResize.SIZE_TOPLEFT,
    WLR_EDGE_TOP | WLR_EDGE_RIGHT: MoveResize.SIZE_TOPRIGHT,
    WLR_EDGE_BOTTOM: MoveResize.SIZE_BOTTOM,
    WLR_EDGE_BOTTOM | WLR_EDGE_LEFT: MoveResize.SIZE_BOTTOMLEFT,
    WLR_EDGE_BOTTOM | WLR_EDGE_RIGHT: MoveResize.SIZE_BOTTOMRIGHT,
    WLR_EDGE_LEFT: MoveResize.SIZE_LEFT,
    WLR_EDGE_RIGHT: MoveResize.SIZE_RIGHT,
}


# Listener slot indices for Surface; N_LISTENERS sizes the listeners array.
# Toplevel slots are kept contiguous so unregister_toplevel_handlers can use
# a simple range loop — when adding a new toplevel-only slot, place it in the
# block bounded by L_TOPLEVEL_DESTROY..L_REQUEST_SHOW_WINDOW_MENU (inclusive).
cdef enum SurfaceListener:
    L_MAP
    L_UNMAP
    L_DESTROY
    L_COMMIT
    L_NEW_SUBSURFACE
    L_TOPLEVEL_DESTROY
    L_REQUEST_MOVE
    L_REQUEST_RESIZE
    L_REQUEST_MAXIMIZE
    L_REQUEST_FULLSCREEN
    L_REQUEST_MINIMIZE
    L_SET_TITLE
    L_SET_APP_ID
    L_SET_PARENT
    L_REQUEST_SHOW_WINDOW_MENU
    N_LISTENERS


log = Logger("wayland")
cdef bint debug = log.is_debug_enabled()


# Last ingest frame per live Surface: XDG geometry followed by wl_surface
# logical size.  Surface is a pxd-defined extension type, so keeping this small
# state table here avoids widening its public ABI.  Terminal destroy removes
# the entry before the WID can become stale.
surface_geometries: Dict[int, tuple[int, int, int, int, int, int]] = {}
surface_capture_pending: set[int] = set()


# `surfaces` registry and `next_wid` are imported from wayland_surface — the
# base class shares them across every WaylandSurface subclass (xdg, subsurface…).


cdef class Surface(WaylandSurface):

    def __cinit__(self):
        # base class __cinit__ initialises self._callbacks
        self.title = ""
        self.app_id = ""

    def __init__(self):
        super().__init__(N_LISTENERS)
        self.wid = next_wid()

    def __repr__(self):
        return "Surface(%i : %s)" % (self.wid, self.title)

    @property
    def xdg_surface_ptr(self) -> int:
        """Raw wlr_xdg_surface pointer; for callers (e.g. WaylandPointer/Keyboard)
        that still take a plain integer. Returns 0 once the surface has been
        destroyed by wlroots — callers must treat 0 as 'gone'.
        (`wl_surface_ptr` is inherited from WaylandSurface for the underlying
        wl_surface pointer.)"""
        return <uintptr_t> self.wlr_xdg_surface

    @property
    def toplevel_ptr(self) -> int:
        cdef wlr_xdg_surface *surface = <wlr_xdg_surface*> self.wlr_xdg_surface
        if surface == NULL:
            return 0
        if surface.role != WLR_XDG_SURFACE_ROLE_TOPLEVEL:
            return 0
        return <uintptr_t> surface.toplevel

    # frame_done, capture_pixels, connect, disconnect, _emit are inherited
    # from WaylandSurface — they all key off self.wlr_surface (the wl_surface).

    cdef void add_main_listeners(self):
        cdef wlr_surface *s = self.wlr_xdg_surface.surface
        # Pull the wl_surface up onto the base so frame_done / capture_pixels
        # work uniformly across every WaylandSurface subclass.
        self.wlr_surface = s
        self.add_listener(L_COMMIT, &s.events.commit)
        self.add_listener(L_MAP, &s.events.map)
        self.add_listener(L_UNMAP, &s.events.unmap)
        self.add_listener(L_NEW_SUBSURFACE, &s.events.new_subsurface)
        self.add_listener(L_DESTROY, &self.wlr_xdg_surface.events.destroy)
        # Keep the Surface alive while wlroots holds listener pointers into it.
        # The shared registry is keyed by wl_surface so any role can share it.
        self.register()

    # Single C shim for every Surface-level listener. The slot is recovered by
    # pointer arithmetic on the listeners[] array, then dispatched to the matching
    # Surface method. This replaces 12 individual one-line callback wrappers.
    cdef void dispatch(self, wl_listener *listener, void *data) noexcept:
        cdef int slot = self.slot_of(listener)
        cdef wlr_xdg_toplevel_move_event *move_event
        cdef wlr_xdg_toplevel_resize_event *resize_event
        cdef wlr_xdg_toplevel_show_window_menu_event *show_menu_event
        if slot == L_MAP:
            self.map()
        elif slot == L_UNMAP:
            self.unmap()
        elif slot == L_DESTROY:
            self.destroy()
        elif slot == L_COMMIT:
            self.commit()
        elif slot == L_NEW_SUBSURFACE:
            self.new_subsurface(<wlr_subsurface*>data)
        elif slot == L_TOPLEVEL_DESTROY:
            self.toplevel_destroy()
        elif slot == L_REQUEST_MOVE:
            move_event = <wlr_xdg_toplevel_move_event*>data
            self.request_move(move_event.serial)
        elif slot == L_REQUEST_RESIZE:
            resize_event = <wlr_xdg_toplevel_resize_event*>data
            self.request_resize(resize_event.edges, resize_event.serial)
        elif slot == L_REQUEST_MAXIMIZE:
            self.request_maximize()
        elif slot == L_REQUEST_FULLSCREEN:
            self.request_fullscreen()
        elif slot == L_REQUEST_MINIMIZE:
            self.request_minimize()
        elif slot == L_SET_TITLE:
            self.set_title()
        elif slot == L_SET_APP_ID:
            self.set_app_id()
        elif slot == L_SET_PARENT:
            self.set_parent()
        elif slot == L_REQUEST_SHOW_WINDOW_MENU:
            show_menu_event = <wlr_xdg_toplevel_show_window_menu_event*>data
            self.request_show_window_menu(show_menu_event.serial,
                                          show_menu_event.x, show_menu_event.y)
        else:
            log.error("Error: unknown surface listener slot %i", slot)

    cdef void register_toplevel_handlers(self) noexcept:
        cdef wlr_xdg_toplevel *t = self.wlr_xdg_surface.toplevel
        log("register_toplevel_handlers() toplevel=%#x", <uintptr_t> t)
        if t == NULL:
            # no toplevel yet
            return
        if self.listeners[int(L_REQUEST_MOVE)].listener.link.next != NULL:
            # already done
            return

        log("Surface has toplevel, attaching toplevel handlers")
        self.add_listener(L_TOPLEVEL_DESTROY, &t.events.destroy)
        self.add_listener(L_REQUEST_MAXIMIZE, &t.events.request_maximize)
        self.add_listener(L_REQUEST_FULLSCREEN, &t.events.request_fullscreen)
        self.add_listener(L_REQUEST_MINIMIZE, &t.events.request_minimize)
        self.add_listener(L_REQUEST_MOVE, &t.events.request_move)
        self.add_listener(L_REQUEST_RESIZE, &t.events.request_resize)
        self.add_listener(L_SET_TITLE, &t.events.set_title)
        self.add_listener(L_SET_APP_ID, &t.events.set_app_id)
        self.add_listener(L_SET_PARENT, &t.events.set_parent)
        self.add_listener(L_REQUEST_SHOW_WINDOW_MENU, &t.events.request_show_window_menu)

    cdef void set_decoration(self, wlr_xdg_toplevel_decoration_v1 *decoration) noexcept:
        self.decoration = decoration
        self.apply_decoration_mode()

    cdef void apply_decoration_mode(self) noexcept:
        cdef wlr_xdg_toplevel_decoration_v1 *decoration = self.decoration
        if decoration == NULL or self.wlr_xdg_surface == NULL:
            return
        if not self.wlr_xdg_surface.initialized:
            log("deferring toplevel decoration mode until surface is initialized")
            return
        log("setting server-side decoration mode for surface %i", self.wid)
        wlr_xdg_toplevel_decoration_v1_set_mode(decoration, WLR_XDG_TOPLEVEL_DECORATION_V1_MODE_SERVER_SIDE)
        self.decoration = NULL

    cdef void map(self) noexcept:
        toplevel = self.wlr_xdg_surface.toplevel
        geometry = &self.wlr_xdg_surface.geometry
        self.register_toplevel_handlers()
        title = toplevel.title.decode("utf8") if (toplevel and toplevel.title) else ""
        app_id = toplevel.app_id.decode("utf8") if (toplevel and toplevel.app_id) else ""
        size = (geometry.width, geometry.height)
        if debug:
            log("XDG surface MAPPED: %r, size=%s", title, size)
        self._emit("map", self.wid, title, app_id, size)

    cdef void unmap(self) noexcept:
        self.unregister_toplevel_handlers()
        self.update_source_format(NULL)
        log("XDG surface UNMAPPED")
        self._emit("unmap", self.wid)

    cdef void toplevel_destroy(self) noexcept:
        log("XDG toplevel DESTROYED")
        self.unregister_toplevel_handlers()

    cdef void destroy(self) noexcept:
        if self.wlr_xdg_surface == NULL:
            # idempotent: destroy already ran (e.g. registry pop triggered it).
            return
        log("XDG surface DESTROYED, toplevel=%s", bool(self.wlr_xdg_surface.toplevel != NULL))
        surface_geometries.pop(self.wid, None)
        surface_capture_pending.discard(self.wid)
        # Detach all listeners while wlroots' event lists are still valid.
        # We MUST do this here rather than rely on __dealloc__: the dispatch
        # shim holds a strong reference for the duration of the call, so the
        # registry-drop below cannot bring the refcount to zero until after
        # this event handler returns — by then wlroots' event lists are gone.
        self._detach_all()
        try:
            self._emit("destroy", self.wid)
        except BaseException:
            log.error("Error dispatching destroy for %s", self, exc_info=True)
        finally:
            try:
                if debug:
                    log("xdg surface dropped")
                self.unregister()
            finally:
                # wlroots will free the wlr_xdg_surface (and wl_surface) as
                # soon as this callback returns. Null every borrowed pointer
                # even when registry or observer cleanup fails.
                self.update_source_format(NULL)
                self.wlr_xdg_surface = NULL
                self.wlr_surface = NULL

    cdef void request_move(self, uint32_t serial) noexcept:
        log("Surface REQUEST MOVE")
        self._emit("move", self.wid, serial)

    cdef void request_resize(self, uint32_t edges, uint32_t serial) noexcept:
        if debug:
            edge_names = tuple(edge_name for edge_val, edge_name in EDGES.items() if edges & edge_val)
            log("Surface REQUEST RESIZE edges: %d - %r", edges, edge_names)
        enumval = EDGES_MAP.get(edges, MoveResize.CANCEL)
        self._emit("resize", self.wid, serial, enumval)

    cdef void request_maximize(self) noexcept:
        if debug:
            log("Surface REQUEST MAXIMIZE")
        self._emit("maximize", self.wid)

    cdef void request_fullscreen(self) noexcept:
        if debug:
            log("Surface REQUEST FULLSCREEN")
        self._emit("fullscreen", self.wid)

    cdef void request_minimize(self) noexcept:
        if debug:
            log("Surface REQUEST MINIMIZE")
        self._emit("minimize", self.wid)

    cdef void set_title(self) noexcept:
        title = self.wlr_xdg_surface.toplevel.title
        self.title = title.decode("utf8") if title else ""
        log("Surface %i SET TITLE: %r", self.wid, self.title)
        self._emit("title", self.wid, self.title)

    cdef void set_app_id(self) noexcept:
        app_id = self.wlr_xdg_surface.toplevel.app_id
        self.app_id = app_id.decode("utf8") if app_id else ""
        log("Surface %i SET APP_ID: %s", self.wid, self.app_id)
        self._emit("app-id", self.wid, self.app_id)

    def get_opaque_region(self) -> tuple:
        """Return the committed opaque region in Xpra window coordinates.

        wl_surface stores this state relative to the full surface, whereas
        Xpra captures only xdg_surface.geometry (which excludes CSD margins).
        """
        cdef wlr_xdg_surface *xdg_surface = self.wlr_xdg_surface
        if xdg_surface == NULL or self.wlr_surface == NULL:
            return ()
        cdef wlr_box *geometry = &xdg_surface.geometry
        return get_clipped_opaque_region(&self.wlr_surface.opaque_region,
                                         geometry.x, geometry.y,
                                         geometry.width, geometry.height)

    cdef void request_show_window_menu(self, uint32_t serial, int32_t x, int32_t y) noexcept:
        # Client asked the compositor to pop up a window-management context
        # menu (move/resize/close). xpra is a remote display: we don't render
        # the menu ourselves — relay to consumers so the server can decide.
        # No consumer today; left as an emit-only no-op.
        if debug:
            log("Surface %i REQUEST SHOW WINDOW MENU at (%i, %i) serial=%#x",
                self.wid, x, y, serial)
        self._emit("show-window-menu", self.wid, serial, x, y)

    cdef void set_parent(self) noexcept:
        # Fired after wlroots has updated wlr_xdg_toplevel.parent. NULL means
        # "no parent" (cleared). The parent itself is a wlr_xdg_toplevel*; we
        # map it to our Surface via its .base wlr_xdg_surface, which is what
        # we keyed the `surfaces` registry with.
        if self.wlr_xdg_surface == NULL or self.wlr_xdg_surface.toplevel == NULL:
            return
        cdef wlr_xdg_toplevel *parent = self.wlr_xdg_surface.toplevel.parent
        cdef unsigned long parent_wid = 0
        cdef Surface parent_surface
        if parent != NULL and parent.base != NULL:
            parent_surface = surfaces.get(<uintptr_t> parent.base)
            if parent_surface is not None:
                parent_wid = parent_surface.wid
        log("Surface %i SET PARENT: parent_wid=%i", self.wid, parent_wid)
        self._emit("set-parent", self.wid, parent_wid)

    cdef void commit(self) noexcept:
        if debug:
            log("xdg_surface_commit")
        xdg_surface = self.wlr_xdg_surface

        if xdg_surface.role == WLR_XDG_SURFACE_ROLE_TOPLEVEL and xdg_surface.toplevel != NULL:
            self.register_toplevel_handlers()
            # Fallback: If configure wasn't sent yet (toplevel wasn't ready), send it now
            if xdg_surface.initialized and not xdg_surface.configured:
                if self.decoration != NULL:
                    self.apply_decoration_mode()
                else:
                    log("Surface initialized, sending first configure")
                    wlr_xdg_toplevel_set_size(xdg_surface.toplevel, 0, 0)
                    wlr_xdg_surface_schedule_configure(xdg_surface)
            else:
                self.apply_decoration_mode()

        geometry = (
            xdg_surface.geometry.x, xdg_surface.geometry.y,
            xdg_surface.geometry.width, xdg_surface.geometry.height,
        )
        size = geometry[2:4]
        wlr_surf = xdg_surface.surface
        surface_size = (wlr_surf.current.width, wlr_surf.current.height)
        previous_frame = surface_geometries.get(self.wid)
        previous_geometry = previous_frame[:4] if previous_frame is not None else None
        previous_surface_size = previous_frame[4:] if previous_frame is not None else None
        surface_damage = ()
        cdef pixman_region32_t effective_damage
        pixman_region32_init(&effective_damage)
        try:
            wlr_surface_get_effective_damage(wlr_surf, &effective_damage)
            surface_damage = tuple(get_damage_areas(&effective_damage))
        finally:
            pixman_region32_fini(&effective_damage)
        if wlr_surf.mapped:
            previous_format = self.source_format
            self.update_source_format(wlr_surf.buffer.source if wlr_surf.buffer != NULL else NULL)
            # A sampling or logical-extent change alters the whole normalized
            # raster.  A buffer attach alone does not: clients attach on most
            # frames and its declared effective damage remains authoritative.
            sampling_changed = (
                self.wid in surface_capture_pending or
                previous_surface_size != surface_size or
                previous_format != self.source_format or
                bool(wlr_surf.current.committed & (
                    WLR_SURFACE_STATE_TRANSFORM |
                    WLR_SURFACE_STATE_SCALE |
                    WLR_SURFACE_STATE_VIEWPORT
                ))
            )
            rects = xdg_root_damage(
                surface_damage, geometry, previous_geometry,
                force_full=sampling_changed,
            )
            if rects:
                # Invalidate the model generation before readback.  If the
                # replacement capture fails, old pixels must not be labelled
                # as this commit; the client may keep its already-presented
                # frame until a later successful generation arrives.
                surface_capture_pending.add(self.wid)
                try:
                    self._emit("surface-snapshot", self.wid, None)
                except BaseException:
                    log.error("Error invalidating root snapshot for %s", self, exc_info=True)
                self.capture_surface_pixels()
        else:
            self.update_source_format(NULL)
            rects = ()
            # A later map must publish a complete root raster even when the
            # client retained and reattached the same buffer without damage.
            surface_geometries.pop(self.wid, None)
            surface_capture_pending.discard(self.wid)
            if not wlr_surface_has_buffer(wlr_surf):
                # A NULL-buffer commit permanently clears the root generation.
                try:
                    self._emit("surface-snapshot", self.wid, None)
                except BaseException:
                    log.error("Error invalidating root snapshot for %s", self, exc_info=True)

        surface_tree = self.get_surface_tree()
        self._emit("commit", self.wid, bool(wlr_surf.mapped), size, rects, surface_tree)

    def get_surface_tree(self) -> tuple:
        """Return the current mapped paint tree in wlroots rendering order.

        The root marker is retained at the exact point where wlroots visits it,
        so callers can distinguish subsurfaces below the toplevel content from
        those above it.  All offsets are relative to the XDG window geometry,
        which is also the origin of the client backing and captured root image.
        """
        cdef wlr_xdg_surface *xdg_surface = self.wlr_xdg_surface
        if xdg_surface == NULL or xdg_surface.surface == NULL:
            return ()
        return tuple(collect_surfaces(
            xdg_surface.surface, self.wid,
            xdg_surface.geometry.x, xdg_surface.geometry.y,
            xdg_surface.geometry.width, xdg_surface.geometry.height,
        ))

    cdef void capture_surface_pixels(self) noexcept:
        # Normalize the whole root wl_surface first, then place it at
        # (-geometry.x, -geometry.y) in the exact XDG canvas.  This preserves
        # transparent padding when the requested geometry extends outside the
        # surface and makes full and tiled reads sample-identical.
        if self.wlr_xdg_surface == NULL:
            return
        logical_image = None
        image = None
        try:
            geometry = (
                self.wlr_xdg_surface.geometry.x, self.wlr_xdg_surface.geometry.y,
                self.wlr_xdg_surface.geometry.width, self.wlr_xdg_surface.geometry.height,
            )
            surface_size = (
                self.wlr_surface.current.width,
                self.wlr_surface.current.height,
            )
            logical_image = self.capture_logical_pixels()
            if logical_image is None:
                return
            image = image_for_xdg_geometry(logical_image, geometry)
            if image is not None:
                self._emit("surface-snapshot", self.wid, image)
                surface_geometries[self.wid] = geometry + surface_size
                surface_capture_pending.discard(self.wid)
        except BaseException:
            log.error("Error capturing logical root pixels for %s", self, exc_info=True)
        finally:
            if logical_image is not None and logical_image is not image:
                try:
                    logical_image.free()
                except BaseException:
                    log.error("Error releasing surface-local root pixels for %s", self, exc_info=True)
            if image is not None:
                try:
                    image.free()
                except BaseException:
                    log.error("Error releasing logical root pixels for %s", self, exc_info=True)

    cdef void discover_subsurfaces(self) noexcept:
        for pointer in undiscovered_subsurfaces(self.wlr_surface):
            self.new_subsurface(<wlr_subsurface*> <uintptr_t> pointer)

    cdef void new_subsurface(self, wlr_subsurface *subsurface) noexcept:
        if subsurface == NULL or subsurface.surface == NULL:
            return
        cdef Subsurface sub
        cdef int width
        cdef int height
        cdef bint created = False
        initial_image = None
        try:
            log("New SUBSURFACE created, parent wid=%i", self.wid)
            log(" subsurface wlr_surface=%#x, parent wlr_surface=%#x",
                <uintptr_t> subsurface.surface, <uintptr_t> subsurface.parent)
            existing = surfaces.get(<uintptr_t> subsurface.surface)
            if existing is None:
                sub = Subsurface()
                created = True
            elif isinstance(existing, Subsurface):
                sub = <Subsurface> existing
                if sub.wlr_subsurface == subsurface:
                    return
            else:
                log.error("Error: wl_surface %#x already has incompatible wrapper %s",
                          <uintptr_t> subsurface.surface, existing)
                return
            sub.attach(self, subsurface)
            width = subsurface.surface.current.width
            height = subsurface.surface.current.height
            initial_image = sub.capture_attached_pixels() if created else None
            # `capture_attached_pixels` has already sampled any physical
            # buffer scale/transform/viewport into this logical raster.  Keep
            # the transport size logical too so no later encoder scales it a
            # second time.
            self._emit("new-subsurface", self.wid, sub, width, height,
                       width, height, self.wid, self.get_surface_tree(), initial_image,
                       sub.get_colourspace())
            sub.discover_subsurfaces()
        except BaseException:
            log.error("Error dispatching subsurface for %s", self, exc_info=True)
        finally:
            if initial_image is not None:
                try:
                    initial_image.free()
                except BaseException:
                    log.error("Error releasing initial pixels for %s", self, exc_info=True)

    cdef void unregister_toplevel_handlers(self) noexcept nogil:
        # Toplevel slots are contiguous: L_REQUEST_MOVE..L_REQUEST_SHOW_WINDOW_MENU.
        # L_NEW_SUBSURFACE is a wl_surface lifetime listener, not a toplevel
        # map-state listener: it must survive unmap/remap so newly created
        # descendants remain observable.
        cdef int i
        for i in range(L_TOPLEVEL_DESTROY, L_REQUEST_SHOW_WINDOW_MENU + 1):
            self._detach_slot(i)

    def resize(self, width: int, height: int) -> None:
        cdef wlr_xdg_surface *surface = <wlr_xdg_surface*> self.wlr_xdg_surface
        if surface == NULL:
            log("%s.resize(%i, %i): surface destroyed; skipping", self, width, height)
            return
        # `surface.toplevel` is a union slot shared with `popup` — only safe to
        # treat as wlr_xdg_toplevel* when role is TOPLEVEL. Otherwise we'd be
        # passing a popup pointer to set_size and crash inside wlroots.
        if surface.role != WLR_XDG_SURFACE_ROLE_TOPLEVEL:
            log.warn("Warning: %s.resize(%i, %i) role=%d, not a toplevel; skipping", self, width, height, surface.role)
            return
        cdef wlr_xdg_toplevel *toplevel = surface.toplevel
        if toplevel == NULL:
            log("%s.resize(%i, %i): no toplevel yet, skipping", self, width, height)
            return
        log("wlr_xdg_toplevel_set_size(%#x, %i, %i)", <uintptr_t> toplevel, width, height)
        wlr_xdg_toplevel_set_size(toplevel, width, height)

    def focus(self, focused: bool) -> None:
        cdef wlr_xdg_surface *surface = <wlr_xdg_surface*> self.wlr_xdg_surface
        if surface == NULL:
            log("%s.focus(%s): surface destroyed; skipping", self, focused)
            return
        if surface.role != WLR_XDG_SURFACE_ROLE_TOPLEVEL:
            log.warn("Warning: %s.focus(%s): role=%d, not a toplevel; skipping", self, focused, surface.role)
            return
        cdef wlr_xdg_toplevel *toplevel = surface.toplevel
        if toplevel == NULL:
            log("%s.focus(%s): no toplevel yet, skipping", self, focused)
            return
        log("wlr_xdg_toplevel_set_activated(%#x, %s)", <uintptr_t> toplevel, focused)
        wlr_xdg_toplevel_set_activated(toplevel, focused)

    # __dealloc__ inherited from ListenerObject: detach + free the listeners array.


cdef tuple get_clipped_opaque_region(pixman_region32_t *region,
                                     int x, int y, int width, int height):
    """Translate a surface-local pixman region into a clipped window region."""
    if width <= 0 or height <= 0:
        return ()
    cdef int n_rects = 0
    cdef pixman_box32_t *rects = pixman_region32_rectangles(region, &n_rects)
    cdef int x2 = x + width
    cdef int y2 = y + height
    cdef int rx1, ry1, rx2, ry2
    rectangles = []
    cdef int i
    for i in range(n_rects):
        rx1 = rects[i].x1
        ry1 = rects[i].y1
        rx2 = rects[i].x2
        ry2 = rects[i].y2
        if rx1 < x:
            rx1 = x
        if ry1 < y:
            ry1 = y
        if rx2 > x2:
            rx2 = x2
        if ry2 > y2:
            ry2 = y2
        if rx2 > rx1 and ry2 > ry1:
            rectangles.append((rx1 - x, ry1 - y, rx2 - rx1, ry2 - ry1))
    return tuple(rectangles)


cdef struct collect_ctx:
    wlr_surface *root
    unsigned long root_wid
    int origin_x
    int origin_y
    int root_logical_width
    int root_logical_height
    bint invalid
    void *result_list


cdef void collect_surface_callback(wlr_surface *surface, int sx, int sy, void *user_data) noexcept:
    """Append one mapped surface without changing wlroots' paint order."""
    cdef collect_ctx *ctx = <collect_ctx*> user_data
    result = <object> ctx.result_list
    if surface == ctx.root:
        # The root is a structural marker as well as a paint layer.  Its
        # backing-local origin is always (0, 0), even when XDG geometry crops
        # shadows or client-side decorations from the wl_surface.
        result.append((ctx.root_wid, 0, 0,
                       ctx.root_logical_width, ctx.root_logical_height,
                       ctx.root_logical_width, ctx.root_logical_height))
        return
    sub = surfaces.get(<uintptr_t> surface)
    if sub is None:
        # A mapped native layer without a registered stable identity cannot be
        # omitted: doing so would present a valid-looking but incomplete
        # authoritative composition.  Mark the whole generation invalid and
        # let the Python topology boundary refuse it.
        ctx.invalid = True
        return
    result.append((sub.wid, sx - ctx.origin_x, sy - ctx.origin_y,
                   surface.current.width, surface.current.height,
                   surface.current.width, surface.current.height))


cdef list collect_surfaces(wlr_surface *surface, unsigned long root_wid,
                           int origin_x, int origin_y,
                           int root_logical_width, int root_logical_height):
    """
    Collect the full mapped tree exactly in wlroots bottom-to-top order.

    Returns a list of `(wid, sx, sy, logical_w, logical_h, native_w, native_h)`
    tuples.  In this post-ingest contract `native_w/h` are deliberately the
    normalized raster dimensions, not `buffer_width/height`: transform, scale
    and viewport sampling have already happened exactly once, so downstream
    packet encoding must not apply a second scale.  The root is retained as
    `root_wid`; child offsets are translated from wl_surface coordinates to
    the XDG client-backing origin.
    """
    result = []
    cdef collect_ctx ctx
    ctx.root = surface
    ctx.root_wid = root_wid
    ctx.origin_x = origin_x
    ctx.origin_y = origin_y
    ctx.root_logical_width = root_logical_width
    ctx.root_logical_height = root_logical_height
    ctx.invalid = False
    ctx.result_list = <void*> result
    wlr_surface_for_each_surface(surface, collect_surface_callback, <void*> &ctx)
    if ctx.invalid:
        result.clear()
    return result
