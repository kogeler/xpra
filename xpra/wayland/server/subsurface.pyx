# This file is part of Xpra.
# Copyright (C) 2026 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

# cython: language_level=3

from xpra.log import Logger
from libc.stdint cimport uintptr_t
from xpra.wayland.server.wlroots cimport (
    wl_listener,
    wl_list,
    wlr_subsurface,
    wlr_surface,
    wlr_surface_get_effective_damage,
    wlr_surface_has_buffer,
    WLR_SURFACE_STATE_TRANSFORM, WLR_SURFACE_STATE_SCALE,
    WLR_SURFACE_STATE_VIEWPORT,
)
from xpra.wayland.server.pixman cimport (
    pixman_region32_t, pixman_region32_init, pixman_region32_fini,
)
from xpra.wayland.server.wayland_surface cimport WaylandSurface, next_wid, get_damage_areas
from xpra.wayland.server.wayland_surface import surfaces


log = Logger("wayland")


# Last successfully captured logical sampling generation per live wl_surface.
# Role loss retains it; terminal wl_surface destroy removes it.
subsurface_frames = {}
subsurface_capture_pending = set()


cdef extern from *:
    """
    #include <wlr/types/wlr_subcompositor.h>
    static struct wlr_subsurface *xpra_subsurface_from_current_link(struct wl_list *link) {
        struct wlr_subsurface *subsurface = NULL;
        return wl_container_of(link, subsurface, current.link);
    }
    """
    wlr_subsurface *xpra_subsurface_from_current_link(wl_list *link)


cdef tuple undiscovered_subsurfaces(wlr_surface *surface):
    """Snapshot committed child roles, including currently unmapped children.

    A synchronized ancestor commit may publish these roles before the parent
    wrapper installs its new_subsurface listener.  Only `added` roles belong
    to the current native tree; pending roles still arrive through that signal.
    """
    if surface == NULL:
        return ()
    result = []
    cdef wl_list *head
    cdef wl_list *link
    cdef wlr_subsurface *subsurface
    cdef int layer
    for layer in range(2):
        head = &surface.current.subsurfaces_below if layer == 0 else &surface.current.subsurfaces_above
        link = head.next
        while link != head:
            subsurface = xpra_subsurface_from_current_link(link)
            link = link.next
            if not subsurface.added:
                continue
            existing = surfaces.get(<uintptr_t> subsurface.surface)
            if isinstance(existing, Subsurface) and (<Subsurface> existing).wlr_subsurface == subsurface:
                continue
            result.append(<uintptr_t> subsurface)
    return tuple(result)


# Listener slots for Subsurface.  A subsurface may itself be the parent of a
# nested subsurface, so its wl_surface lifetime includes new-subsurface events.
cdef enum SubsurfaceListener:
    L_COMMIT
    L_NEW_SUBSURFACE
    L_DESTROY            # underlying wl_surface destroy
    L_SUB_DESTROY        # subsurface-role destroy (role unassigned but wl_surface may live on)
    N_LISTENERS


cdef class Subsurface(WaylandSurface):

    def __init__(self):
        super().__init__(N_LISTENERS)
        self.wid = next_wid()

    def get_parent(self):
        return self.parent

    def get_root(self):
        """Return the non-subsurface ancestor which owns the client backing."""
        cdef WaylandSurface root = self.parent
        while root is not None and isinstance(root, Subsurface):
            root = (<Subsurface> root).parent
        return root

    cdef void attach(self, WaylandSurface parent, wlr_subsurface *subsurface):
        """Attach one wl_subsurface role to this wl_surface-owned wrapper.

        The wl_surface listeners and registry entry live until the underlying
        surface is destroyed.  Only the role listener is replaced when a
        client destroys and recreates wl_subsurface for the same surface.
        """
        if subsurface == NULL or subsurface.surface == NULL:
            return
        if self.wlr_surface != NULL:
            if self.wlr_surface != subsurface.surface:
                raise RuntimeError("cannot attach a subsurface wrapper to a different wl_surface")
            if self.wlr_subsurface == subsurface:
                self.parent = parent
                return
            if self.wlr_subsurface != NULL:
                raise RuntimeError("cannot replace a live wl_subsurface role")
            self.parent = parent
            self.wlr_subsurface = subsurface
            self.add_listener(L_SUB_DESTROY, &subsurface.events.destroy)
            return

        self.parent = parent
        self.wlr_subsurface = subsurface
        self.wlr_surface = subsurface.surface
        # The shared `surfaces` registry is keyed by wl_surface, so any code
        # path with a wl_surface pointer (e.g. nested subsurface lookups,
        # cursor tracking) can find this Subsurface uniformly.
        self.register()
        # Listen for the underlying wl_surface's commit and destroy, and the
        # subsurface-role destroy.
        self.add_listener(L_COMMIT, &subsurface.surface.events.commit)
        self.add_listener(L_NEW_SUBSURFACE, &subsurface.surface.events.new_subsurface)
        self.add_listener(L_DESTROY, &subsurface.surface.events.destroy)
        self.add_listener(L_SUB_DESTROY, &subsurface.events.destroy)

    cdef object capture_attached_pixels(self):
        """Capture the current attached generation, independent of visibility."""
        if self.wlr_surface == NULL or not wlr_surface_has_buffer(self.wlr_surface):
            return None
        try:
            return self.capture_logical_pixels()
        except BaseException:
            log.error("Error capturing initially attached pixels for %s", self, exc_info=True)
            return None

    cdef void dispatch(self, wl_listener *listener, void *data) noexcept:
        cdef int slot = self.slot_of(listener)
        if slot == L_COMMIT:
            self.commit()
        elif slot == L_NEW_SUBSURFACE:
            self.new_subsurface(<wlr_subsurface*> data)
        elif slot == L_DESTROY:
            self.destroy()
        elif slot == L_SUB_DESTROY:
            self.role_destroy()
        else:
            log.error("Error: unknown subsurface listener slot %i", slot)

    cdef void commit(self) noexcept:
        # Desynchronized subsurfaces can map, unmap, move and restack without
        # a root commit.  Publish topology, content generation and colourspace
        # as one callback so no observer can see an intermediate child state.
        cdef pixman_region32_t effective_damage
        if self.wlr_surface == NULL:
            return
        image = None
        try:
            mapped = bool(self.wlr_surface.mapped)
            has_buffer = bool(wlr_surface_has_buffer(self.wlr_surface))
            size = self.get_surface_size()
            previous_size = subsurface_frames.get(self.wid)
            sampling_changed = (
                self.wid in subsurface_capture_pending or
                previous_size != size or
                bool(self.wlr_surface.current.committed & (
                    WLR_SURFACE_STATE_TRANSFORM |
                    WLR_SURFACE_STATE_SCALE |
                    WLR_SURFACE_STATE_VIEWPORT
                ))
            )
            pixman_region32_init(&effective_damage)
            try:
                wlr_surface_get_effective_damage(self.wlr_surface, &effective_damage)
                damage = tuple(get_damage_areas(&effective_damage))
            finally:
                pixman_region32_fini(&effective_damage)
            if has_buffer and sampling_changed and size[0] > 0 and size[1] > 0:
                damage = ((0, 0, size[0], size[1]),)
            # `mapped` is effective visibility and becomes false when an
            # ancestor unmaps.  Its attached buffer remains authoritative.
            if has_buffer:
                try:
                    image = self.capture_logical_pixels()
                except BaseException:
                    # The combined callback still invalidates this generation
                    # and reconciles the root into its refused state.
                    log.error("Error capturing committed pixels for %s", self, exc_info=True)
                if image is None:
                    subsurface_capture_pending.add(self.wid)
                else:
                    subsurface_frames[self.wid] = size
                    subsurface_capture_pending.discard(self.wid)
            else:
                self.update_source_format(NULL)
                subsurface_frames.pop(self.wid, None)
                subsurface_capture_pending.discard(self.wid)
            # A synchronized child's commit can precede its parent's map.
            # Keep the attached buffer's format with its retained raster:
            # visibility is not a buffer detach, and remapping the parent
            # need not produce another child commit to restore this identity.
            root = self.get_root()
            root_wid = getattr(root, "wid", 0)
            surface_tree = root.get_surface_tree() if root is not None and hasattr(root, "get_surface_tree") else ()
            # The retained image is already normalized to `size`.  The second
            # pair is the transport raster size, not the physical wl_buffer,
            # and therefore stays logical to prevent downstream rescaling.
            self._emit(
                "subsurface-commit", self.wid, root_wid, mapped, has_buffer,
                surface_tree, damage, image,
                size[0], size[1], size[0], size[1], self.get_colourspace(),
            )
        except BaseException:
            log.error("Error dispatching atomic commit for %s", self, exc_info=True)
        finally:
            if image is not None:
                try:
                    image.free()
                except BaseException:
                    log.error("Error releasing committed pixels for %s", self, exc_info=True)

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
            log("New nested SUBSURFACE created, direct parent wid=%i", self.wid)
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
            root = sub.get_root()
            root_wid = getattr(root, "wid", 0)
            surface_tree = root.get_surface_tree() if root is not None and hasattr(root, "get_surface_tree") else ()
            initial_image = sub.capture_attached_pixels() if created else None
            # Initial snapshots use the same normalized transport contract as
            # ordinary commits; physical buffer dimensions are ingest-only.
            self._emit("new-subsurface", self.wid, sub, width, height,
                       width, height, root_wid, surface_tree, initial_image,
                       sub.get_colourspace())
            # Consumers now own this wrapper and its callbacks, so descendant
            # discovery can publish through the same normal notification path.
            sub.discover_subsurfaces()
        except BaseException:
            log.error("Error dispatching nested subsurface for %s", self, exc_info=True)
        finally:
            if initial_image is not None:
                try:
                    initial_image.free()
                except BaseException:
                    log.error("Error releasing initial pixels for %s", self, exc_info=True)

    cdef void role_destroy(self) noexcept:
        """Deactivate one wl_subsurface role without forgetting wl_surface."""
        if self.wlr_subsurface == NULL:
            return
        log("%s SUBSURFACE ROLE DESTROYED", self)
        self._detach_slot(L_SUB_DESTROY)
        try:
            self._emit("subsurface-role-destroy", self.wid)
        except BaseException:
            log.error("Error dispatching role destroy for %s", self, exc_info=True)
        finally:
            # The role object is invalid after this callback, but commit,
            # nested-role and surface-destroy listeners remain attached to the
            # persistent wl_surface and the registry continues to own `self`.
            self.wlr_subsurface = NULL
            self.parent = None

    cdef void destroy(self) noexcept:
        if self.wlr_surface == NULL:
            # Idempotent terminal wl_surface destruction.
            return
        log("%s WL_SURFACE DESTROYED", self)
        subsurface_frames.pop(self.wid, None)
        subsurface_capture_pending.discard(self.wid)
        self._detach_all()
        try:
            self._emit("destroy", self.wid)
        except BaseException:
            log.error("Error dispatching destroy for %s", self, exc_info=True)
        finally:
            try:
                self.unregister()
            finally:
                # The wl_surface is terminal and any still-attached role
                # pointer is no longer usable. Null every borrowed pointer even
                # when registry or observer cleanup fails.
                self.update_source_format(NULL)
                self.wlr_surface = NULL
                self.wlr_subsurface = NULL
                self.parent = None
