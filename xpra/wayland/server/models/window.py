# This file is part of Xpra.
# Copyright (C) 2025 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

from typing import Any
from math import isfinite

from xpra.constants import MAX_WINDOW_SIZE
from xpra.util.gobject import one_arg_signal
from xpra.codecs.image import ImageWrapper
from xpra.wayland.server.models.frame import FrameCallbackModel
from xpra.os_util import gi_import
from xpra.log import Logger

log = Logger("wayland", "window")

GObject = gi_import("GObject")


# wl_output_transform values are protocol ABI and intentionally mirrored here
# so the pure-Python geometry helpers can be tested without loading the native
# Wayland server extension.
_WAYLAND_TEXTURE_AFFINES = {
    0: (1, 0, 0, 0, 1, 0),       # normal
    1: (0, -1, 1, 1, 0, 0),      # 90
    2: (-1, 0, 1, 0, -1, 1),     # 180
    3: (0, 1, 0, -1, 0, 1),      # 270
    4: (-1, 0, 1, 0, 1, 0),      # flipped
    5: (0, 1, 0, 1, 0, 0),       # flipped 90
    6: (1, 0, 0, 0, -1, 1),      # flipped 180
    7: (0, -1, 1, -1, 0, 1),     # flipped 270
}


def wayland_sampling_affine(buffer_transform: int,
                            source_box: tuple[float, float, float, float],
                            logical_width: int, logical_height: int) -> tuple[float, ...]:
    """Return the logical-pixel-center to buffer-texel affine mapping.

    wlroots renders a surface with the inverse of its buffer transform.  The
    returned coordinates use texel centers as integers, which is what the
    ingest resampler needs for bilinear filtering.  ``source_box`` is the
    buffer-local box returned by ``wlr_surface_get_buffer_source_box``.
    """
    if not (0 < logical_width <= MAX_WINDOW_SIZE and 0 < logical_height <= MAX_WINDOW_SIZE):
        raise ValueError(f"invalid logical raster size {logical_width}x{logical_height}")
    try:
        transform = (3 if buffer_transform == 1 else
                     1 if buffer_transform == 3 else buffer_transform)
        a, b, c, d, e, f = _WAYLAND_TEXTURE_AFFINES[transform]
    except KeyError as e_invalid:
        raise ValueError(f"invalid wl_output_transform {buffer_transform}") from e_invalid
    source_x, source_y, source_width, source_height = source_box
    if (not all(isfinite(value) for value in source_box)
            or source_width <= 0 or source_height <= 0):
        raise ValueError(f"invalid Wayland buffer source box {source_box}")
    return (
        source_width * a / logical_width,
        source_width * b / logical_height,
        source_x + source_width * (a / (2 * logical_width) + b / (2 * logical_height) + c) - 0.5,
        source_height * d / logical_width,
        source_height * e / logical_height,
        source_y + source_height * (d / (2 * logical_width) + e / (2 * logical_height) + f) - 0.5,
    )


def xdg_root_damage(
        surface_damage, geometry: tuple[int, int, int, int],
        previous_geometry: tuple[int, int, int, int] | None = None,
        force_full: bool = False,
) -> tuple[tuple[int, int, int, int], ...]:
    """Translate effective surface damage into the XDG geometry canvas."""
    gx, gy, width, height = geometry
    if width <= 0 or height <= 0:
        return ()
    if force_full or previous_geometry != geometry:
        return ((0, 0, width, height),)
    clipped = []
    for x, y, w, h in surface_damage:
        x1 = max(0, x - gx)
        y1 = max(0, y - gy)
        x2 = min(width, x + w - gx)
        y2 = min(height, y + h - gy)
        if x2 > x1 and y2 > y1:
            clipped.append((x1, y1, x2 - x1, y2 - y1))
    return tuple(clipped)


def image_for_xdg_geometry(image: ImageWrapper,
                           geometry: tuple[int, int, int, int]) -> ImageWrapper | None:
    """Crop/pad a surface-local raster to the exact XDG geometry canvas.

    Areas of the XDG geometry which lie outside the root wl_surface are
    transparent.  Packed premultiplied channel bytes are copied unchanged;
    opaque X formats are promoted to their alpha-bearing counterpart only
    when transparent padding is required.
    """
    gx, gy, width, height = geometry
    if width <= 0 or height <= 0:
        return None
    if width > MAX_WINDOW_SIZE or height > MAX_WINDOW_SIZE:
        raise ValueError(f"Wayland XDG canvas exceeds maximum size: {width}x{height}")
    image_width = image.get_width()
    image_height = image.get_height()
    if gx == 0 and gy == 0 and width == image_width and height == image_height:
        image.set_target_x(0)
        image.set_target_y(0)
        return image
    if image.get_planes() != ImageWrapper.PACKED or image.get_bytesperpixel() != 4:
        raise ValueError(f"cannot place non-packed Wayland image {image}")

    source_left = max(0, gx)
    source_top = max(0, gy)
    source_right = min(image_width, gx + width)
    source_bottom = min(image_height, gy + height)
    copy_width = max(0, source_right - source_left)
    copy_height = max(0, source_bottom - source_top)
    transparent_padding = copy_width != width or copy_height != height
    pixel_format = image.get_pixel_format()
    if transparent_padding:
        pixel_format = {"BGRX": "BGRA", "RGBX": "RGBA"}.get(pixel_format, pixel_format)
    output_stride = width * 4
    output = bytearray(output_stride * height)
    source = image.get_pixels()
    source_stride = image.get_rowstride()
    destination_x = source_left - gx
    destination_y = source_top - gy
    for row in range(copy_height):
        source_offset = (source_top + row) * source_stride + source_left * 4
        destination_offset = (destination_y + row) * output_stride + destination_x * 4
        source_row = bytearray(source[source_offset:source_offset + copy_width * 4])
        if transparent_padding and image.get_pixel_format().endswith("X"):
            source_row[3::4] = b"\xff" * copy_width
        output[destination_offset:destination_offset + copy_width * 4] = source_row
    result = ImageWrapper(
        0, 0, width, height, output, pixel_format, image.get_depth(), output_stride,
        planes=image.get_planes(), thread_safe=True,
        palette=image.get_palette(), full_range=image.get_full_range(),
    )
    result.set_timestamp(image.get_timestamp())
    return result


def borrow_image_wrapper(image: ImageWrapper) -> ImageWrapper:
    """Return an independently mutable wrapper over retained immutable pixels."""
    if image.get_planes() != ImageWrapper.PACKED:
        raise ValueError(f"cannot borrow planar Wayland image {image}")
    borrowed = ImageWrapper(
        image.get_x(), image.get_y(), image.get_width(), image.get_height(),
        image.get_pixels(), image.get_pixel_format(), image.get_depth(),
        image.get_rowstride(), image.get_bytesperpixel(), image.get_planes(),
        True, image.get_palette(), image.get_full_range(),
    )
    borrowed.set_target_x(image.get_target_x())
    borrowed.set_target_y(image.get_target_y())
    borrowed.set_timestamp(image.get_timestamp())
    return borrowed


def retain_image_snapshot(image: ImageWrapper) -> ImageWrapper:
    """Copy one borrowed committed frame into model-owned immutable storage."""
    if image.get_planes() != ImageWrapper.PACKED:
        raise ValueError(f"cannot retain planar Wayland image {image}")
    retained = ImageWrapper(
        image.get_x(), image.get_y(), image.get_width(), image.get_height(),
        bytes(image.get_pixels()), image.get_pixel_format(), image.get_depth(),
        image.get_rowstride(), image.get_bytesperpixel(), image.get_planes(),
        True, image.get_palette(), image.get_full_range(),
    )
    retained.set_target_x(image.get_target_x())
    retained.set_target_y(image.get_target_y())
    retained.set_timestamp(image.get_timestamp())
    return retained


class Window(FrameCallbackModel):
    __gproperties__ = {
        "display": (
            GObject.TYPE_PYOBJECT,
            "The wayland Display object", "",
            GObject.ParamFlags.READABLE,
        ),
        "surface": (
            GObject.TYPE_PYOBJECT,
            "The wayland Surface object", "",
            GObject.ParamFlags.READABLE,
        ),
        "geometry": (
            GObject.TYPE_PYOBJECT,
            "current coordinates (x, y, w, h) for the window", "",
            GObject.ParamFlags.READABLE,
        ),
        "decorations": (
            GObject.TYPE_BOOLEAN,
            "Should the window be decorated?", "",
            False,
            GObject.ParamFlags.READABLE,
        ),
        "depth": (
            GObject.TYPE_INT,
            "window bit depth", "",
            -1, 64, -1,
            GObject.ParamFlags.READABLE,
        ),
        "colourspace": (
            GObject.TYPE_PYOBJECT,
            "the colourspace the surface is tagged with (wp_color_management_surface_v1)", "",
            GObject.ParamFlags.READABLE,
        ),
        "content-types": (
            GObject.TYPE_PYOBJECT,
            "Content hints from wp_content_type_v1", "",
            GObject.ParamFlags.READABLE,
        ),
        "has-alpha": (
            GObject.TYPE_BOOLEAN,
            "Does the window use transparency", "",
            False,
            GObject.ParamFlags.READABLE,
        ),
        "frame-has-alpha": (
            GObject.TYPE_BOOLEAN,
            "Does the buffer the client has committed have an alpha channel", "",
            True,
            GObject.ParamFlags.READABLE,
        ),
        "pixel-format": (
            GObject.TYPE_PYOBJECT,
            "pixel format of the current surface buffer", "",
            GObject.ParamFlags.READABLE,
        ),
        "opaque-region": (
            GObject.TYPE_PYOBJECT,
            "Compositor can assume that there is no transparency for this region", "",
            GObject.ParamFlags.READABLE,
        ),
        "client-machine": (
            GObject.TYPE_PYOBJECT,
            "Host where client process is running", "",
            GObject.ParamFlags.READABLE,
        ),
        "pid": (
            GObject.TYPE_INT,
            "PID of owning process", "",
            -1, 2147483647, -1,
            GObject.ParamFlags.READABLE,
        ),
        "title": (
            GObject.TYPE_PYOBJECT,
            "Window title", "",
            GObject.ParamFlags.READABLE,
        ),
        "app-id": (
            GObject.TYPE_PYOBJECT,
            "Window app id", "",
            GObject.ParamFlags.READABLE,
        ),
        "parent": (
            GObject.TYPE_PYOBJECT,
            "wid of the parent toplevel (xdg_toplevel.set_parent), 0 if none", "",
            GObject.ParamFlags.READABLE,
        ),
        "transient-for": (
            GObject.TYPE_PYOBJECT,
            "wid of the transient parent window, 0 if none", "",
            GObject.ParamFlags.READABLE,
        ),
        "relative-position": (
            GObject.TYPE_PYOBJECT,
            "current coordinates relative to the parent window", "",
            GObject.ParamFlags.READABLE,
        ),
        "override-redirect": (
            GObject.TYPE_BOOLEAN,
            "Is this an unmanaged override-redirect style window", "",
            False,
            GObject.ParamFlags.READABLE,
        ),
        "window-type": (
            GObject.TYPE_PYOBJECT,
            "Window type hints", "",
            GObject.ParamFlags.READABLE,
        ),
        "role": (
            GObject.TYPE_PYOBJECT,
            "The window's role (ICCCM session management)", "",
            GObject.ParamFlags.READABLE,
        ),
        "command": (
            GObject.TYPE_PYOBJECT,
            "Command used to start or restart the client", "",
            GObject.ParamFlags.READABLE,
        ),
        "iconic": (
            GObject.TYPE_BOOLEAN,
            "ICCCM 'iconic' state -- any sort of 'not on desktop'.", "",
            True,
            GObject.ParamFlags.READWRITE,
        ),
        "maximized": (
            GObject.TYPE_BOOLEAN,
            "Is the window maximized", "",
            False,
            GObject.ParamFlags.READWRITE,
        ),
        "fullscreen": (
            GObject.TYPE_BOOLEAN,
            "Is the window maximized", "",
            False,
            GObject.ParamFlags.READWRITE,
        ),
        "image": (
            GObject.TYPE_PYOBJECT,
            "ImageWrapper of the surface pixels", "",
            GObject.ParamFlags.READABLE,
        ),
    }

    __gsignals__ = {
        # signals we emit:
        "unmanaged": one_arg_signal,
        "initiate-moveresize": one_arg_signal,
        "grab": one_arg_signal,
        "ungrab": one_arg_signal,
        "bell": one_arg_signal,
        "client-contents-changed": one_arg_signal,
        "motion": one_arg_signal,
    }

    # things that we expose:
    _property_names = [
        "depth", "has-alpha", "opaque-region", "decorations", "colourspace", "content-types",
        "client-machine", "pid",
        "title", "role", "app-id",
        "command",
        "parent", "transient-for", "relative-position",
        "override-redirect", "window-type",
        "iconic", "maximized", "fullscreen",
    ]
    # exposed and changing (should be watched for notify signals):
    _dynamic_property_names = [
        "title", "app-id", "command", "colourspace", "opaque-region",
        "content-types",
        "parent", "transient-for", "relative-position",
        "iconic", "maximized", "fullscreen",
    ]
    # should not be exported to the clients:
    # `has-alpha` is the capability the client creates its visual and backing from,
    # so it must not follow the buffers: a surface can commit an opaque one and still
    # gain a translucent subsurface later, which the client paints into that backing
    _internal_property_names = ["frame-has-alpha", "pixel-format"]
    _MODELTYPE = "Wayland"

    def __init__(self, props: dict[str, Any]):
        super().__init__()
        self._internal_set_property("pixel-format", "")
        for key, prop in props.items():
            self._internal_set_property(key, prop)
        # Monotonic identity for the retained WSSO root raster.  A composite
        # transaction snapshots this value for every participating layer so a
        # commit between asynchronous stages cannot mix native generations.
        self._snapshot_generation = 0

    def __repr__(self) -> str:  # pylint: disable=arguments-differ
        surface = self._gproperties.get("surface", None)
        wid = getattr(surface, "wid", 0)
        return "WaylandWindow(%#x)" % wid

    def setup(self) -> None:
        self._managed = True
        self._setup_done = True

    def unmanage(self, exiting=False) -> None:
        if self._managed:
            self.emit("unmanaged", exiting)

    def do_unmanaged(self, _wm_exiting: bool) -> None:
        if not self._managed:
            return
        self._managed = False
        self.cancel_damage_frame_timer()
        self.cancel_empty_ack_timer()
        try:
            self.clear_image()
        finally:
            self.managed_disconnect()

    def get_image(self, x: int, y: int, width: int, height: int) -> ImageWrapper | None:
        image = self._gproperties["image"]
        if image is None:
            return None
        w, h = self._gproperties["geometry"][2:4]
        if x >= w or y >= h:
            raise ValueError("invalid position %ix%i for window of size %ix%i" % (x, y, w, h))
        if x == 0 and y == 0 and width == w and height == h:
            return borrow_image_wrapper(image)
        iw = min(width, w - x)
        ih = min(height, h - y)
        return image.get_sub_image(x, y, iw, ih)

    def set_image(self, image: ImageWrapper | None) -> None:
        if image is None:
            self.clear_image()
            return
        pixel_format = image.get_pixel_format()
        retained = retain_image_snapshot(image)
        previous = self._gproperties.get("image")
        has_pixel_format = "pixel-format" in self.get_internal_property_names()
        previous_pixel_format = self._gproperties.get("pixel-format")
        previous_frame_alpha = self._gproperties.get("frame-has-alpha", True)
        try:
            # Preserve upstream's per-buffer policy before image notification;
            # the visual/backing capability "has-alpha" remains unchanged.
            self._updateprop("frame-has-alpha", "A" in pixel_format)
            if has_pixel_format:
                self._updateprop("pixel-format", pixel_format)
            self._updateprop("image", retained)
        except BaseException:
            # `_updateprop` stores before notifying, so either call may raise
            # after partially installing the new generation.  Restore the
            # complete previous state before releasing our private copy.
            self._gproperties["image"] = previous
            self._gproperties["frame-has-alpha"] = previous_frame_alpha
            if has_pixel_format:
                self._gproperties["pixel-format"] = previous_pixel_format
            retained.free()
            raise
        self._snapshot_generation += 1
        if previous:
            previous.free()

    def clear_image(self) -> None:
        image = self._gproperties.get("image")
        self._gproperties["image"] = None
        # Clearing is itself a new authoritative generation, including the
        # pre-capture invalidation emitted for a damaged Wayland commit.
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

    def has_image(self) -> bool:
        return self._gproperties.get("image") is not None

    def get_snapshot_generation(self) -> int:
        return self._snapshot_generation

    def get_dimensions(self) -> tuple[int, int]:
        # just extracts the size from the geometry:
        return self._gproperties["geometry"][2:4]

    def get_geometry(self) -> tuple[int, int, int, int]:
        return self._gproperties["geometry"]

    ################################
    # Actions
    ################################

    def hide(self):
        """ this is not exposed to the windows """

    def show(self):
        """ this is not exposed to the windows """

    def raise_window(self) -> None:
        """ no-op: windows are unaware of their stacking status """

    def set_active(self) -> None:
        """ this is not available under Wayland? """

    @staticmethod
    def request_close() -> None:
        log.warn("Warning: close-request not implemented yet for Wayland")
