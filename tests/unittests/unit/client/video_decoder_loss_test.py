#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 kogeler
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

"""Exercise decoder retirement, UI notification and existing refresh selection."""

from threading import Thread, get_ident
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from xpra.client.base.stub import StubClientSubsystem
from xpra.client.gui.widget_base import ClientWidgetBase
from xpra.client.gui.window import backing as backing_module
from xpra.client.gui.window.backing import WindowBackingBase, fire_paint_callbacks, get_backing_client_properties
from xpra.client.subsystem.window.manager import WindowManagerClient
from xpra.codecs.constants import EncodingNotSupported, TransientCodecException, VideoSpec
from xpra.codecs.video import VideoHelper
from xpra.common import noop
from xpra.net.common import Packet
from xpra.server.subsystem.window import WindowServer
from xpra.server.window.video_compress import WindowVideoSource
from xpra.util.objects import typedict


class RefusingDecoder:
    def init_context(self, *_args):
        raise EncodingNotSupported("fixture permanently lost this decoder")

    def clean(self):
        pass


class WorkingDecoder:
    def init_context(self, encoding, width, height, colorspace, _options):
        self.encoding = encoding
        self.width, self.height = width, height
        self.colorspace = colorspace

    def clean(self):
        pass

    def get_info(self):
        return {}

    def get_type(self):
        return "fixture"

    def decompress_image(self, _data, _options):
        return object()


class Backing(WindowBackingBase):
    RGB_MODES = ("RGBX", "RGBA")

    def __init__(self, wid, alpha, _depth=0):
        super().__init__(wid, alpha)

    def init(self, ww, wh, bw, bh):
        self.render_size = ww, wh
        self.size = bw, bh
        self._backing = True

    def do_video_paint(self, _coding, _image, _x, _y, _ew, _eh, _w, _h, _options, callbacks):
        fire_paint_callbacks(callbacks)


class Manager(WindowManagerClient):
    def __init__(self, client):
        StubClientSubsystem.__init__(self, client)
        WindowManagerClient.__init__(self)


def make_source(wid):
    source = WindowVideoSource.__new__(WindowVideoSource)
    source.wid = wid
    source.maximized = True
    source.client_render_size = (640, 480)
    source.client_bit_depth = source.image_depth = 24
    source.client_refresh_encodings = ("rgb24",)
    source.full_frames_only = source.is_tray = source.has_shape = False
    source.supports_transparency = False
    source.encodings = source.core_encodings = ("h264", "rgb24")
    source.common_encodings = source.encodings
    source.decoder_speed = typedict()
    source.rgb_formats = ("RGBX",)
    source.full_csc_modes = typedict({"h264": ("YUV420P",)})
    source._all_encoders = {"h264": [noop], "rgb24": [noop]}
    source._encoders = {"h264": noop, "rgb24": noop}
    source.encoding = "h264"
    source._mmap = None
    source._csc_encoder = source._video_encoder = None
    source._rgb_auto_threshold = 0
    source.refresh_quality = 100
    source.video_encodings = ("h264",)
    source.picture_encodings = ("rgb24",)
    source.scroll_min_percent = 50
    source.scroll_preference = 100
    source.video_subregion = SimpleNamespace(supported=False)
    source.scaling_control = None
    # Keep real CSC parsing, encoding exclusion and selection, substituting
    # only subsequent quality/score calculations unrelated to this boundary.
    source.update_quality = source.update_speed = noop
    source.update_encoding_options = source.update_refresh_attributes = noop
    return source


class VideoDecoderLossTest(unittest.TestCase):
    def setUp(self):
        self.ui = []
        self.packets = []
        self.acks = []
        self.owner = get_ident()
        self.specs = [self.spec(RefusingDecoder)]
        self.helper = VideoHelper()
        self.helper.decoder_modules = {"fixture": SimpleNamespace(get_specs=lambda: self.specs)}
        for item in (
            patch.object(backing_module, "_loaded_video", True),
            patch.object(backing_module, "getVideoHelper", return_value=self.helper),
            patch.dict(backing_module.VIDEO_DECODERS, self.helper.get_video_decoding_options(), clear=True),
        ):
            item.start()
            self.addCleanup(item.stop)

        def send(*args):
            self.assertEqual(get_ident(), self.owner, "capabilities sent from the decode worker")
            self.packets.append(args)

        self.client = SimpleNamespace(
            subsystems={}, send=send, idle_add=lambda fn, *args: self.ui.append((fn, args)),
            timeout_add=noop, source_remove=noop,
        )
        self.client.get_subsystem = self.client.subsystems.get
        self.manager = Manager(self.client)
        self.client.subsystems["window"] = self.manager
        self.windows = [self.window(wid) for wid in (1, 2)]

    @staticmethod
    def spec(codec, colorspace="YUV420P", cost=50):
        return VideoSpec(codec_class=codec, codec_type=codec.__name__, encoding="h264",
                         input_colorspace=colorspace, output_colorspaces=("RGBX",), setup_cost=cost)

    def window(self, wid):
        window = ClientWidgetBase(self.client, wid, False)
        window._backing = window.make_new_backing(Backing, 128, 128, 128, 128)
        self.manager._id_to_window[wid] = window
        self.addCleanup(window._backing.close_decoder, False)
        return window

    def decode(self, window=None):
        backing = (window or self.windows[0])._backing
        errors = []

        def worker():
            try:
                backing.paint_with_video_decoder("h264", b"fixture", 0, 0, 128, 128,
                                                 typedict({"csc": "YUV420P"}),
                                                 [lambda *args: self.acks.append(args)])
            except BaseException as exc:
                errors.append(exc)

        thread = Thread(target=worker, name="fixture-decode", daemon=True)
        thread.start()
        thread.join(2)
        self.assertFalse(thread.is_alive(), "decode worker did not finish")
        if errors:
            raise errors[0]

    def drain_ui(self):
        while self.ui:
            fn, args = self.ui.pop(0)
            fn(*args)

    def test_retirement_updates_all_windows_before_server_refresh(self):
        self.decode()
        self.assertNotIn("h264", backing_module.VIDEO_DECODERS)
        self.assertEqual(self.packets, [])
        self.drain_ui()
        self.assertEqual(len(self.packets), 2, "retirement never reached the server")
        sources = {wid: make_source(wid) for wid in (1, 2)}
        selected = []
        connection = SimpleNamespace(
            set_client_properties=lambda wid, _window, props: sources[wid].set_client_properties(props),
            update_batch=noop,
            refresh=lambda wid, _window, _opts: selected.append(sources[wid].get_current_or_rgb(128 * 128)),
        )

        class Server(WindowServer):
            pass

        server = Server.__new__(Server)
        server._id_to_window = {wid: SimpleNamespace(is_managed=lambda: True) for wid in sources}
        server.get_server_source = lambda _proto: connection
        server.is_shown = lambda _window: True
        for args in self.packets:
            self.assertEqual(args[0], "window-refresh")
            self.assertEqual(set(args[5]), {"encoding.full_csc_modes"})
            server._process_refresh(None, Packet(*args))
        self.assertEqual(selected, ["rgb24", "rgb24"])
        for source in sources.values():
            self.assertNotIn("h264", source.common_encodings)
            self.assertEqual(source.common_video_encodings, ())
            self.assertTrue(source.maximized)
            self.assertEqual(source.client_render_size, (640, 480))
            self.assertFalse(source.video_subregion.supported)

    def test_other_colorspaces_and_picture_modes_remain_available(self):
        other = self.spec(WorkingDecoder, "YUV444P")
        self.specs.append(other)
        backing_module.VIDEO_DECODERS["h264"]["YUV444P"] = [other]
        self.decode()
        modes = self.windows[0]._backing.get_encoding_properties()["encoding.full_csc_modes"]
        self.assertEqual(modes["h264"], ("YUV444P",))
        self.assertEqual(modes["webp"], ("RGBX",))
        self.assertEqual(modes["jpeg"], ("RGBX",))

    def test_alternative_decoder_does_not_retire_the_mode(self):
        alternative = self.spec(WorkingDecoder, cost=60)
        self.specs.append(alternative)
        backing_module.VIDEO_DECODERS["h264"]["YUV420P"].append(alternative)
        self.decode()
        self.assertTrue(self.acks[-1][0])
        self.assertIn("YUV420P", backing_module.VIDEO_DECODERS["h264"])
        self.assertEqual(self.ui, [])

    def test_transient_setup_and_bad_frame_do_not_notify_capability_loss(self):
        class TransientOnce(WorkingDecoder):
            attempts = 0

            def init_context(self, *args):
                type(self).attempts += 1
                if self.attempts == 1:
                    raise TransientCodecException("fixture temporarily busy")
                super().init_context(*args)

            def decompress_image(self, *_args):
                raise RuntimeError("fixture corrupted frame")

        spec = self.spec(TransientOnce)
        self.specs[:] = [spec]
        backing_module.VIDEO_DECODERS["h264"]["YUV420P"] = [spec]
        self.decode()
        self.assertEqual(TransientOnce.attempts, 2)
        self.assertFalse(self.acks[-1][0])
        self.assertIn("h264", backing_module.VIDEO_DECODERS)
        self.assertEqual(self.ui, [])

    def test_notification_uses_current_windows_and_backings(self):
        self.decode()
        self.assertTrue(self.ui, "retirement did not queue an update")
        self.manager._id_to_window.clear()
        replacement = self.window(3)
        # Native Win32 owns `backing` rather than `_backing`; no native GUI is
        # substituted into this unit test, just its existing accessor shape.
        self.manager._id_to_window[4] = SimpleNamespace(backing=replacement._backing)
        self.manager._id_to_window[5] = SimpleNamespace(_backing=None)
        self.drain_ui()
        self.assertEqual([args[1] for args in self.packets], [3, 4])

    def test_cleanup_before_queued_notification_sends_nothing(self):
        self.decode()
        self.assertTrue(self.ui, "retirement did not queue an update")
        self.manager._id_to_window.clear()
        self.drain_ui()
        self.assertEqual(self.packets, [])

    def test_new_window_properties_do_not_re_advertise_retired_modes(self):
        self.decode()
        window = self.window(3)
        props = get_backing_client_properties(window._backing, {
            "full_csc_modes": {"h264": ("YUV420P",)},
        })
        self.assertFalse(props["encoding.full_csc_modes"].get("h264"))

    def test_inflight_bad_frame_does_not_repeat_notification(self):
        self.decode()
        queued = len(self.ui)
        self.windows[0]._backing.draw_region(0, 0, 128, 128, "h264", b"old-frame", 0, typedict(), [])
        self.assertEqual(len(self.ui), queued)
        self.drain_ui()
        self.assertEqual(len(self.packets), 2, "retirement did not produce one update per current window")


if __name__ == "__main__":
    unittest.main()
