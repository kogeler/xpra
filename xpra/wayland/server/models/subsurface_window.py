# This file is part of Xpra.
# Copyright (C) 2026 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

from typing import Any

from xpra.codecs.image import ImageWrapper
from xpra.net.common import BACKWARDS_COMPATIBLE
from xpra.os_util import gi_import
from xpra.wayland.server.models.frame import FrameCallbackModel
from xpra.wayland.server.models.window import borrow_image_wrapper, retain_image_snapshot

GObject = gi_import("GObject")


class SubsurfaceWindow(FrameCallbackModel):
    """Retain one wl_subsurface's normalized logical raster for WSSO.

    This facade exists only so an internal WindowSource can capture a bounded
    region for the connection-owned raw RGB32 transaction.  It is not a client
    window and has no independent decoder, title, transient relationship or
    presentation lifecycle.  Content hints stay empty so compatibility calls
    cannot inherit the parent window's encoding policy.  It publishes the
    child surface and display like every upstream frame-callback model, but
    WSSO completes each native child commit directly and never arms its
    frame timers.
    """

    __gproperties__ = {
        # the child's own `wl_surface` and the display to flush: a subsurface has its own
        # frame callbacks, which only `frame_done` on this surface can answer
        "surface": (GObject.TYPE_PYOBJECT, "the wayland Subsurface object", "",
                    GObject.ParamFlags.READABLE),
        "display": (GObject.TYPE_PYOBJECT, "the wayland Display object", "",
                    GObject.ParamFlags.READABLE),
        "depth": (GObject.TYPE_INT, "bit depth", "", -1, 64, -1, GObject.ParamFlags.READABLE),
        "has-alpha": (GObject.TYPE_BOOLEAN, "alpha channel", "", False, GObject.ParamFlags.READABLE),
        "pixel-format": (GObject.TYPE_PYOBJECT, "current pixel format", "", GObject.ParamFlags.READABLE),
        "frame-has-alpha": (GObject.TYPE_BOOLEAN, "alpha channel of the current buffer", "",
                            True, GObject.ParamFlags.READABLE),
    }

    _property_names = ["depth", "has-alpha"]
    _dynamic_property_names: list[str] = []
    # a subsurface has its own buffer, so its transparency is its own:
    # the canonical use for one is an opaque video plane inside a parent
    # which is only translucent for its shadow and rounded corners
    _internal_property_names: list[str] = ["frame-has-alpha", "pixel-format"]
    _MODELTYPE = "WaylandSubsurface"

    def __init__(self, width: int, height: int, has_alpha: bool = True, depth: int = 32,
                 surface=None, display=None):
        super().__init__()
        self._width = width
        self._height = height
        self._image: ImageWrapper | None = None
        self._internal_set_property("surface", surface)
        self._internal_set_property("display", display)
        self._snapshot_generation = 0
        self._internal_set_property("depth", depth)
        self._internal_set_property("has-alpha", has_alpha)
        self._internal_set_property("pixel-format", "")
        self._setup_done = True
        self._managed = True

    def unmanage(self, exiting=False) -> None:
        # there is no `unmanaged` signal to emit: the subsystem drops the facade directly
        self._managed = False
        self.cancel_damage_frame_timer()
        self.cancel_empty_ack_timer()
        self._internal_set_property("surface", None)
        super().unmanage(exiting)

    def set_image(self, image: ImageWrapper) -> None:
        self._image = image
        self._updateprop("pixel-format", image.get_pixel_format())
        # publish the alpha the buffer really has, so that an opaque subsurface
        # can be encoded as video even if its parent window is translucent:
        self._updateprop("frame-has-alpha", "A" in image.get_pixel_format())

    def update_dimensions(self, width: int, height: int) -> None:
        self._width = width
        self._height = height

    def get_dimensions(self) -> tuple[int, int]:
        return self._width, self._height

    def get_geometry(self) -> tuple[int, int, int, int]:
        return 0, 0, self._width, self._height

    def get_image(self, x: int, y: int, width: int, height: int) -> ImageWrapper | None:
        image = self._image
        if image is None:
            return None
        logical_width = self._width
        logical_height = self._height
        if logical_width <= 0 or logical_height <= 0:
            return None
        left = max(0, x)
        top = max(0, y)
        right = min(logical_width, x + width)
        bottom = min(logical_height, y + height)
        if right <= left or bottom <= top:
            return None
        if left == 0 and top == 0 and right == logical_width and bottom == logical_height:
            return borrow_image_wrapper(image)
        cropped = image.get_sub_image(left, top, right - left, bottom - top)
        cropped.set_target_x(left)
        cropped.set_target_y(top)
        return cropped

    def get(self, name: str, default_value: Any = None) -> Any:
        if BACKWARDS_COMPATIBLE and name == "content-type":
            return ""
        if name == "content-types":
            return ()
        if name == "opaque-region":
            return ()
        return super().get(name, default_value)

    def is_OR(self) -> bool:
        return False

    def is_tray(self) -> bool:
        return False

    def is_shadow(self) -> bool:
        return False

    def replace_image_snapshot(self, image: ImageWrapper) -> None:
        """Copy one borrowed normalized raster into retained model state."""
        # Native ingest has already normalized transform, scale and viewport
        # state into this exact surface-local logical raster.
        if (image.get_width(), image.get_height()) != (self._width, self._height):
            raise ValueError(
                f"Wayland subsurface raster {image.get_width()}x{image.get_height()} "
                f"does not match logical size {self._width}x{self._height}"
            )
        retained = retain_image_snapshot(image)
        retained.set_target_x(0)
        retained.set_target_y(0)
        previous = self._image
        previous_frame_alpha = self._gproperties.get("frame-has-alpha", True)
        has_pixel_format = "pixel-format" in self.get_internal_property_names()
        previous_pixel_format = self._gproperties.get("pixel-format")
        try:
            # Keep this call as the composition point for the WIS-owned
            # pixel-format update added independently to `set_image`.
            self.set_image(retained)
        except BaseException:
            if self._image is retained:
                self._image = previous
            self._gproperties["frame-has-alpha"] = previous_frame_alpha
            if has_pixel_format:
                self._gproperties["pixel-format"] = previous_pixel_format
            retained.free()
            raise
        self._snapshot_generation += 1
        if previous:
            previous.free()

    def replace_dimensions(self, width: int, height: int) -> None:
        """Update logical dimensions and invalidate an incompatible snapshot."""
        if (width, height) != (self._width, self._height):
            self.clear_image()
        self.update_dimensions(width, height)

    def has_image(self) -> bool:
        return self._image is not None

    def get_snapshot_generation(self) -> int:
        return self._snapshot_generation

    def clear_image(self) -> None:
        image = self._image
        self._image = None
        self._snapshot_generation += 1
        try:
            if "pixel-format" in self.get_internal_property_names():
                self._updateprop("pixel-format", "")
        finally:
            try:
                self._updateprop("frame-has-alpha", True)
            finally:
                self._gproperties["frame-has-alpha"] = True
                if image:
                    image.free()

    def replace_colourspace_snapshot(self, colourspace) -> None:
        """Retain authoritative child colourspace outside WIS GObject state."""
        self._subsurface_colourspace = dict(colourspace) if isinstance(colourspace, dict) else colourspace

    def get_colourspace_snapshot(self):
        return getattr(self, "_subsurface_colourspace", None)
