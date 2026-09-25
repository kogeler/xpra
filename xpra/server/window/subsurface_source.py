# This file is part of Xpra.
# Copyright (C) 2026 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

from collections.abc import Callable, Sequence

from xpra.common import SUBSURFACE_COMPOSITE_MODE
from xpra.codecs.constants import preforder
from xpra.codecs.image import ImageWrapper
from xpra.server.window.compress import WindowSource, free_image_wrapper


class SubsurfaceWindowSource(WindowSource):
    """Produce one internal layer of an atomic Wayland subsurface composite.

    The source keeps its native WID only for server-side snapshot and encoder
    ownership.  It is never announced as a client window: active WSSO stages
    are exact uncompressed RGB32 regions retargeted into the parent WID and
    parent-local coordinates by the connection-owned transaction.
    """

    def __init__(self, *args, parent_wid: int, offset_x: int, offset_y: int,
                 logical_width: int = 0, logical_height: int = 0,
                 native_width: int = 0, native_height: int = 0):
        super().__init__(*args)
        self.parent_wid = parent_wid
        self.offset_x = offset_x
        self.offset_y = offset_y
        self.logical_width = logical_width
        self.logical_height = logical_height
        self.native_width = native_width
        self.native_height = native_height
        self.geometry_generation = 0

    def update_geometry(self, parent_wid: int, offset_x: int, offset_y: int,
                        logical_width: int = 0, logical_height: int = 0,
                        native_width: int = 0, native_height: int = 0) -> bool:
        new_geometry = (
            parent_wid, offset_x, offset_y,
            logical_width or self.logical_width,
            logical_height or self.logical_height,
            native_width or self.native_width,
            native_height or self.native_height,
        )
        if self.geometry() == new_geometry:
            return False
        self.parent_wid = parent_wid
        self.offset_x = offset_x
        self.offset_y = offset_y
        if logical_width > 0 and logical_height > 0:
            self.logical_width = logical_width
            self.logical_height = logical_height
        if native_width > 0 and native_height > 0:
            self.native_width = native_width
            self.native_height = native_height
        self.geometry_generation += 1
        return True

    def geometry(self) -> tuple[int, int, int, int, int, int, int]:
        return (
            self.parent_wid, self.offset_x, self.offset_y,
            self.logical_width, self.logical_height,
            self.native_width, self.native_height,
        )

    def _draw_packet_target(self, x: int, y: int, options=None) -> tuple[int, int, int]:
        geometry = (options or {}).get("subsurface-geometry")
        if geometry:
            parent_wid, offset_x, offset_y = geometry[:3]
        else:
            parent_wid, offset_x, offset_y = self.parent_wid, self.offset_x, self.offset_y
        return parent_wid, x + offset_x, y + offset_y

    def do_init_encoders(self) -> None:
        # Install the base picture encoders needed by the raw RGB32 transaction
        # contract.  An internal child never constructs WindowVideoSource,
        # VideoSubregion, codec context, or video timers.
        super().do_init_encoders()
        picture = set(self.picture_encodings) & set(self.core_encodings) & set(self._encoders)
        self.non_video_encodings = preforder(picture)

    def get_best_encoding_impl(self) -> Callable[..., str]:
        # Compatibility callers outside an active composite still use the base
        # encoding selector.  WSSO itself bypasses this selector and requires
        # the exact raw RGB32 mode in process_damage_region().
        if self._mmap and self.encoding != "grayscale":
            return self.encoding_is_mmap
        return self.get_subsurface_encoding

    def get_subsurface_encoding(self, w: int, h: int, options: dict, current_encoding: str) -> str:
        # Compatibility-only direct calls still target the parent's decoder;
        # never admit stateful video or scroll encodings for the internal WID.
        encodings: Sequence[str] = tuple(
            encoding for encoding in self.non_video_encodings
            if encoding in self.common_encodings and encoding != "scroll"
        )
        if self.has_alpha:
            # Wayland readback is premultiplied, so a compatibility call with
            # alpha must retain raw RGB32 bytes as the active transaction does.
            encodings = tuple(encoding for encoding in encodings if encoding == "rgb32")
        if not encodings:
            detail = "alpha-capable " if self._want_alpha else ""
            raise ValueError(f"no {detail}picture encoding for subsurface {self.wid:#x}")
        encoding = WindowSource.do_get_auto_encoding(self, w, h, options, current_encoding, encodings)
        if encoding not in encodings:
            raise ValueError(f"invalid subsurface picture encoding {encoding!r}, expected one of {encodings}")
        return encoding

    def process_damage_region(self, damage_time: float, x: int, y: int, w: int, h: int,
                              coding: str, options: dict, flush=0,
                              packet_complete: Callable[[bool], None] | None = None,
                              force_basic_picture: bool = True, *,
                              captured_image: ImageWrapper | None = None) -> bool:
        # Enforce the raw transaction contract at the final image-capture
        # boundary.  The remaining branches are only the generic WindowSource
        # compatibility API; connection-owned active WSSO never selects them.
        owned_image = captured_image
        try:
            composite = options.get("subsurface-composite") == SUBSURFACE_COMPOSITE_MODE
            if composite:
                _rgb_formats, eligibility_error = self.subsurface_composite_eligibility()
                if eligibility_error:
                    raise ValueError(
                        f"composite subsurface {self.wid:#x} is ineligible: {eligibility_error}"
                    )
                picture = "rgb32"
            elif self._mmap and self.encoding != "grayscale":
                picture = self.encoding_is_mmap()
            else:
                picture = self.get_subsurface_encoding(w, h, options, coding)
            packet_options = dict(options)
            packet_options["subsurface-geometry"] = self.geometry()
            packet_options["subsurface-geometry-generation"] = self.geometry_generation
            owned_image = None
            return super().process_damage_region(
                damage_time, x, y, w, h, picture, packet_options, flush,
                packet_complete, force_basic_picture, captured_image=captured_image,
            )
        finally:
            if owned_image is not None:
                free_image_wrapper(owned_image)

    def make_draw_packet(self, x: int, y: int, outw: int, outh: int,
                         coding: str, data, outstride: int, client_options, options):
        geometry = options.get("subsurface-geometry")
        if geometry:
            client_options["subsurface-geometry-generation"] = options[
                "subsurface-geometry-generation"
            ]
        # Native ingest has already normalized the retained snapshot to the
        # logical surface raster.  In particular, a partial stage whose local
        # origin is (0, 0) must retain its partial destination size rather than
        # being stretched to the whole child canvas.
        return super().make_draw_packet(x, y, outw, outh, coding, data, outstride, client_options, options)

    def can_publish_damage_packet(self, packet) -> bool:
        if packet.get_str(6) == "mmap":
            # Its descriptor must advance the client read pointer. Geometry
            # transitions enqueue their parent repaint behind this work.
            return True
        client_options = packet.get_dict(10)
        return client_options.get(
            "subsurface-geometry-generation", self.geometry_generation,
        ) == self.geometry_generation

    def schedule_auto_refresh(self, packet, options) -> None:
        # Atomic refresh ownership is at the parent transaction.  The base
        # per-source timer would feed a parent-local outbound rectangle back
        # through this child-local model and could publish outside the ordered
        # stage set, so an internal source never owns an auto-refresh timer.
        return
