# This file is part of Xpra.
# Copyright (C) 2017 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

from collections.abc import Callable

from xpra.os_util import gi_import, WIN32, OSX
from xpra.util.str_fn import Ellipsizer
from xpra.opengl.backing import GLWindowBackingBase
from xpra.platform.gl_context import GLContext
from xpra.log import Logger

log = Logger("opengl", "paint")

Gtk = gi_import("Gtk")
Gdk = gi_import("Gdk")


class GLDrawingArea(GLWindowBackingBase):

    def __init__(self, wid: int, window_alpha: bool, pixel_depth: int = 0):
        self.window_context = None
        self.context: GLContext | None = None
        super().__init__(wid, window_alpha, pixel_depth)

    def __repr__(self):
        return "GLDrawingArea(%s, %s)" % (self.wid, self.size)

    def init_gl_config(self) -> None:
        self.context = GLContext(self._alpha_enabled)  # pylint: disable=not-callable

    def is_double_buffered(self) -> bool:
        return self.context.is_double_buffered()

    def init_backing(self) -> None:
        da = Gtk.DrawingArea()
        da.set_app_paintable(True)
        da.connect_after("realize", self.on_realize)
        # da.connect('configure_event', self.on_configure_event)
        # da.connect('draw', self.on_draw)
        # double-buffering is enabled by default anyway, so this is redundant:
        # da.set_double_buffered(True)
        da.set_size_request(*self.size)
        da.set_events(da.get_events() | Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.POINTER_MOTION_HINT_MASK)
        da.show()
        self._backing = da

    def get_backing_handle(self) -> int:
        # the native handle of the drawing area's window:
        # the win32 `HWND`, the X11 `Window` xid, or the macOS `NSView` pointer.
        da = self._backing
        gdk_window = da.get_window() if da else None
        if not gdk_window:
            return 0
        if WIN32:
            from xpra.platform.win32.gtk import get_window_handle
            return get_window_handle(gdk_window)
        if OSX:
            from xpra.platform.darwin.gdk3_bindings import get_nsview_ptr
            return get_nsview_ptr(gdk_window)
        return gdk_window.get_xid()

    def on_realize(self, *args) -> None:
        from xpra.platform.gui import setup_gl_drawing_area
        setup_gl_drawing_area(self.get_backing_handle())
        log("GLDrawingArea.on_realize%s callbacks=%i", args, len(self._pending_gl_context_callbacks))
        gl_context = self.gl_context()
        with gl_context:
            self.run_gl_context_callbacks(gl_context)

    def with_gl_context(self, cb: Callable, *args) -> None:
        da = self._backing
        if da and da.get_mapped():
            if gl_context := self.gl_context():
                with gl_context:
                    cb(gl_context, *args)
            else:
                cb(None, *args)
        elif not da:
            cb(None, *args)
        else:
            log("GLDrawingArea.with_gl_context delayed: %s%s", cb, Ellipsizer(args))
            self.defer_gl_context_callback(cb, *args)

    def get_bit_depth(self, pixel_depth=0) -> int:
        return pixel_depth or self.context.get_bit_depth() or 24

    def gl_context(self):
        b = self._backing
        if not b:
            return None
        handle = self.get_backing_handle()
        if not handle:
            raise RuntimeError(f"backing {b} does not have a native window handle!")
        self.window_context = self.context.get_paint_context(handle)
        if not self.window_context:
            raise RuntimeError(f"failed to get an OpenGL window context for {b} from {self.context}")
        return self.window_context

    def do_gl_show(self, rect_count: int) -> None:
        if self.is_double_buffered():
            # Show the backbuffer on screen
            log("%s.do_gl_show(%s) swapping buffers now", rect_count, self)
            self.window_context.swap_buffers()
        else:
            # glFlush was enough
            pass

    def close_gl(self, context) -> None:
        from xpra.platform.gui import cleanup_gl_drawing_area
        cleanup_gl_drawing_area(self.get_backing_handle())
        super().close_gl(context)

    def close_gl_config(self) -> None:
        if c := self.context:
            self.context = None
            c.destroy()

    def draw_fbo(self, _context) -> bool:
        w, h = self.size
        with self.gl_context() as ctx:
            log("drawing_area.draw_fbo(%s) ctx=%s, size=%s", _context, ctx, (w, h))
            self.gl_init(ctx)
            self.present_fbo(ctx, 0, 0, w, h)
        return True
