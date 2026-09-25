# This file is part of Xpra.
# Copyright (C) 2026 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.


from xpra.wayland.server.wlroots cimport wl_listener, wlr_subsurface, wlr_surface
from xpra.wayland.server.wayland_surface cimport WaylandSurface


cdef tuple undiscovered_subsurfaces(wlr_surface *surface)


cdef class Subsurface(WaylandSurface):
    cdef wlr_subsurface *wlr_subsurface
    cdef WaylandSurface parent              # current role parent; None while the role is absent

    cdef void attach(self, WaylandSurface parent, wlr_subsurface *subsurface)
    cdef object capture_attached_pixels(self)
    cdef void dispatch(self, wl_listener *listener, void *data) noexcept
    cdef void commit(self) noexcept
    cdef void discover_subsurfaces(self) noexcept
    cdef void new_subsurface(self, wlr_subsurface *subsurface) noexcept
    cdef void role_destroy(self) noexcept
    cdef void destroy(self) noexcept
