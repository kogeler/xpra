# This file is part of Xpra.
# Copyright (C) 2011 Serviware (Arthur Huillet, <ahuillet@serviware.com>)
# Copyright (C) 2010 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import os
from threading import RLock
from math import sqrt
from typing import Any
from time import sleep, monotonic
from collections.abc import Sequence
from contextlib import nullcontext

from xpra.os_util import gi_import
from xpra.net.common import FULL_INFO, BACKWARDS_COMPATIBLE
from xpra.net.constants import TCP_SOCKTYPES
from xpra.net.packet_type import ENCODING_SET
from xpra.server.common import may_update_bandwidth_limits, wants_windows
from xpra.server.source.stub import StubClientConnection
from xpra.server.window import batch_config
from xpra.server.core import ClientException
from xpra.codecs.video import getVideoHelper
from xpra.net.compression import use
from xpra.util.background_worker import add_work_item
from xpra.util.objects import typedict
from xpra.util.str_fn import csv
from xpra.util.env import envint
from xpra.log import Logger

GLib = gi_import("GLib")

log = Logger("encoding")
proxylog = Logger("proxy")
statslog = Logger("stats")

MIN_PIXEL_RECALCULATE = envint("XPRA_MIN_PIXEL_RECALCULATE", 2000)


def parse_batch_int(value, varname: str, default: int) -> int:
    if value is not None:
        try:
            return int(value)
        except (TypeError, ValueError):
            log.error("Error: invalid value %r for batch option %s", value, varname)
    return default


class EncodingsConnection(StubClientConnection):
    """
    Store information about the client's support for encodings.
    Runs the encode thread.
    """
    PREFIX = "encoding"

    @classmethod
    def is_needed(cls, caps: typedict) -> bool:
        if BACKWARDS_COMPATIBLE and "encoding" in caps:
            return True
        return bool(caps.dictget("encoding") or caps.strtupleget("encodings")) or wants_windows(caps)

    def init_state(self) -> None:
        self._encoding_state_lock = RLock()
        self._calculate_execution_lock = RLock()
        self._encoding_closed = False
        # contains default values, some of which may be supplied by the client:
        self.default_batch_config = batch_config.DamageBatchConfig()
        self.global_batch_config = self.default_batch_config.clone()  # global batch config

        self.encoding = ""  # the default encoding for all windows
        self.encodings: Sequence[str] = ()  # all the encodings supported by the client
        self.core_encodings: Sequence[str] = ()
        self.full_csc_modes = dict()
        self.window_icon_encodings: Sequence[str] = ()
        self.rgb_formats: Sequence[str] = ("RGB",)
        self.encoding_options = typedict()
        self.icons_encoding_options = typedict()
        self.default_encoding_options = typedict()
        self.auto_refresh_delay: int = 0

        self.lz4 = use("lz4")

        # for managing the recalculate_delays work:
        self.calculate_window_pixels: dict[int, int] = {}
        self.calculate_window_ids: set[int] = set()
        self.calculate_timer: GLib.Source | int = 0
        self._calculate_request: object | None = None
        self.calculate_last_time: float = 0

        self.video_helper = getVideoHelper()
        self.cuda_device_context = None

    def init_from(self, _protocol, server) -> None:
        # `EncodingServer` is the standalone subsystem instance:
        enc = server.subsystems["encoding"]
        self.server_core_encodings = enc.core_encodings
        self.server_encodings = enc.encodings
        self.default_encoding = enc.default_encoding
        self.scaling_control = enc.scaling_control
        self.default_quality = enc.default_quality
        self.default_min_quality = enc.default_min_quality
        self.default_speed = enc.default_speed
        self.default_min_speed = enc.default_min_speed

    def reinit_encodings(self, server) -> None:
        with self._encoding_state_lock:
            if self._encoding_closed or self.is_closed():
                return
            self.server_core_encodings = server.core_encodings
            self.server_encodings = server.encodings
        # If this client connected before nvenc finished loading, CUDA context allocation
        # was skipped in parse_encoding_caps. Allocate it now if still missing.
        if not self.cuda_device_context and self.wants_cuda_device():
            self.allocate_cuda_device_context()
        # Propagate cuda context to any window sources created before it was available.
        with self._encoding_state_lock:
            if self._encoding_closed or self.is_closed():
                return
            context = self.cuda_device_context
            if context:
                get_sources = getattr(self, "all_pixel_sources", self.all_window_sources)
                for ws in get_sources():
                    if not ws.cuda_device_context:
                        ws.cuda_device_context = context

    def cleanup(self) -> None:
        with self._encoding_state_lock:
            if self._encoding_closed:
                return
            self._encoding_closed = True
        errors: list[tuple[BaseException, Any]] = []
        try:
            self.cancel_recalculate_timer()
        except BaseException as error:
            errors.append((error, error.__traceback__))
        # Background calculation is a separate producer, not drained by the
        # encode FIFO. Wait without holding its short publication lock before
        # the reverse muxer traversal releases window state.
        with self._calculate_execution_lock:
            with self._encoding_state_lock:
                self.calculate_window_ids.clear()
                self.calculate_window_pixels.clear()
        # the video encoders are using the cuda context, and they are only cleaned up
        # when the window subsystem is - which happens after this one,
        # so this can only be freed at the very end.
        # Preserve the unconditional upstream tail registration. A constructor
        # losing to the publication fence frees its unpublished candidate;
        # the worker tail frees any context which was already published.
        try:
            self.call_in_encode_thread_at_end(self.free_cuda_device_context)
        except BaseException as error:
            errors.append((error, error.__traceback__))
        if errors:
            for error, traceback in errors[1:]:
                log.error("Additional error during encoding cleanup: %s", error,
                          exc_info=(type(error), error, traceback))
            error, traceback = errors[0]
            raise error.with_traceback(traceback)

    def free_cuda_device_context(self) -> None:
        with self._encoding_state_lock:
            cdd = self.cuda_device_context
            self.cuda_device_context = None
        if cdd:
            cdd.free()

    def all_window_sources(self) -> tuple:
        # we can't assume that the window mixin is loaded:
        window_sources = getattr(self, "window_sources", {})
        return tuple(window_sources.values())

    def all_pixel_sources(self) -> tuple:
        # Keep `all_window_sources()` as the toplevel reporting/bandwidth set;
        # this wider view is only for encoder configuration and lifecycle.
        return self.all_window_sources() + tuple(getattr(self, "subsurface_sources", {}).values())

    def get_window_pixel_sources(self, wid: int) -> tuple:
        window_sources = getattr(self, "window_sources", {})
        subsurface_sources = getattr(self, "subsurface_sources", {})
        sources = []
        if ws := window_sources.get(wid):
            sources.append(ws)
        sources += [ws for sub_wid, ws in subsurface_sources.items()
                    if sub_wid == wid or ws.parent_wid == wid]
        return tuple(sources)

    def get_caps(self) -> dict[str, str | int]:
        return {
            "auto_refresh_delay": self.auto_refresh_delay,
            "encoding": self.encoding,
        }

    def threaded_init_complete(self, encoding) -> None:
        if not self.hello_sent:
            # hello has not been sent yet; the source will be picked up by add_new_client once it is
            return
        # by now, all the codecs have been initialized
        d = encoding.get_encoding_info()
        if FULL_INFO > 1:
            from xpra.codecs.loader import codec_versions
            # codec_versions: dict[str, tuple[Any, ...]] = {}
            for codec, version in codec_versions.items():
                d[codec] = {"version": version}
        video = {}
        for encoding in self.video_helper.get_encodings():
            especs = self.video_helper.get_encoder_specs(encoding)
            ecaps = {}
            for csc, specs in especs.items():
                ecaps[csc] = tuple(spec.to_dict("codec_class") for spec in specs)
            if ecaps:
                video[encoding] = ecaps
        log(f"video specs={video}")
        self.send_async(ENCODING_SET, {"encodings": d, "video": video})
        # only print encoding info when not using mmap:
        mmap_write_area = getattr(self, "mmap_write_area", None)
        if not mmap_write_area or not mmap_write_area.enabled:
            self.print_encoding_info()

    def recalculate_delays(self, request: object | None = None) -> None:
        """ calls update_averages() on `ServerSource.statistics` (`GlobalStatistics`)
            and `WindowSource.statistics` (`WindowPerformanceStatistics`) for each window id in calculate_window_ids,
            this runs in the worker thread.
        """
        with self._calculate_execution_lock:
            with self._encoding_state_lock:
                if self._encoding_closed or self.is_closed():
                    return
                if request is not None:
                    if self._calculate_request is not request:
                        return
                    self._calculate_request = None
                # Claim this batch before admitting a successor. Failed
                # successor publication must not erase the active batch.
                wids = tuple(self.calculate_window_ids)
                self.calculate_window_ids.clear()
                for wid in wids:
                    self.calculate_window_pixels.pop(wid, None)
            self._recalculate_delays(wids)

    def _recalculate_delays(self, wids: Sequence[int]) -> None:
        if self.is_closed():
            return
        now = monotonic()
        self.calculate_last_time = now
        p = self.protocol
        if not p or p.is_closed():
            return
        conn = p._conn
        if not conn:
            return
        # we can't assume that 'self' is a full ClientConnection object:
        stats = getattr(self, "statistics", None)
        if stats:
            stats.bytes_sent.append((now, conn.output_bytecount))
            if getattr(conn, "socktype_wrapped", "") in TCP_SOCKTYPES:
                try:
                    from xpra.platform.netdev_query import get_socket_tcp_info
                except ImportError:
                    pass
                else:
                    if get_raw_socket := getattr(conn, "get_raw_socket", None):
                        try:
                            # noinspection calling-non-callable
                            if sock := get_raw_socket():
                                stats.record_tcp_info(now, get_socket_tcp_info(sock))
                        except (OSError, ValueError):
                            log("failed to query TCP_INFO for %s", conn, exc_info=True)
            stats.update_averages()
        may_update_bandwidth_limits(self)
        focus = self.get_focus()
        get_window_sources = getattr(self, "window_source_items", lambda: tuple(self.window_sources.items()))
        source_operation = getattr(self, "pixel_source_operation", nullcontext)
        sources = get_window_sources()
        maximized_wids = tuple(wid for wid, source in sources if source is not None and source.maximized)
        fullscreen_wids = tuple(wid for wid, source in sources if source is not None and source.fullscreen)
        log("recalculate_delays() wids=%s, focus=%s, maximized=%s, fullscreen=%s",
            wids, focus, maximized_wids, fullscreen_wids)
        for wid in wids:
            get_pixel_source = getattr(self, "get_pixel_source", self.window_sources.get)
            with source_operation(get_pixel_source(wid)) as ws:
                if ws is None:
                    continue
                with log.trap_error("Error calculating delays for window %s", wid):
                    ws.statistics.update_averages()
                    ws.calculate_batch_delay(wid == focus,
                                             len(fullscreen_wids) > 0 and wid not in fullscreen_wids,
                                             len(maximized_wids) > 0 and wid not in maximized_wids)
                    ws.reconfigure()
            if self.is_closed():
                return
            # allow other threads to run
            # (ideally this would be a low priority thread)
            sleep(0)
        # calculate weighted average as new global default delay:
        wdimsum, wdelay, tsize, tcount = 0, 0, 0, 0
        for _wid, source in get_window_sources():
            with source_operation(source) as ws:
                if ws is None or ws.batch_config.last_updated <= 0:
                    continue
                w, h = ws.window_dimensions
                tsize += w * h
                tcount += 1
                time_w = 2.0 + (now - ws.batch_config.last_updated)  # add 2 seconds to even things out
                weight = int(w * h * time_w)
                wdelay += ws.batch_config.delay * weight
                wdimsum += weight
        if wdimsum > 0 and tcount > 0:
            # weighted delay:
            delay = wdelay // wdimsum
            self.global_batch_config.last_delays.append((now, delay))
            self.global_batch_config.delay = delay
            # store the delay as a normalized value per megapixel,
            # so we can adapt it to different window sizes:
            avg_size = tsize // tcount
            ratio = sqrt(1000000.0 / avg_size)
            normalized_delay = int(delay * ratio)
            self.global_batch_config.delay_per_megapixel = normalized_delay
            log("delay_per_megapixel=%i, delay=%i, for wdelay=%i, avg_size=%i, ratio=%.2f",
                normalized_delay, delay, wdelay, avg_size, ratio)

    def may_recalculate(self, wid: int, pixel_count: int) -> None:
        with self._encoding_state_lock:
            if self._encoding_closed or self.is_closed():
                return
            if wid in self.calculate_window_ids:
                return
            v = self.calculate_window_pixels.get(wid, 0) + pixel_count
            self.calculate_window_pixels[wid] = v
            if v < MIN_PIXEL_RECALCULATE:
                return
            statslog("may_recalculate(%#x, %i) total %i pixels, scheduling recalculate work item", wid, pixel_count, v)
            self.calculate_window_ids.add(wid)
            if self._calculate_request is not None:
                return
            request = object()
            self._calculate_request = request

            def calculate() -> None:
                self.recalculate_delays(request)

            def rollback() -> None:
                if self._calculate_request is request:
                    self._calculate_request = None
                    # Keep the pixel totals so the next real update can retry.
                    # No active calculation owns these pending IDs: it takes a
                    # separate batch before releasing its admission state.
                    self.calculate_window_ids.clear()

            delta = monotonic() - self.calculate_last_time
            recalculate_delay = 1.0
            timer = None
            try:
                if delta > recalculate_delay:
                    add_work_item(calculate)
                    return
                delay = int(1000 * (recalculate_delay - delta))
                timer = GLib.timeout_source_new(delay)

                def enqueue_recalculate(*_args) -> bool:
                    with self._encoding_state_lock:
                        if self.calculate_timer is not timer or self._calculate_request is not request:
                            return False
                        self.calculate_timer = 0
                        try:
                            add_work_item(calculate)
                        except BaseException:
                            rollback()
                            raise
                    return False

                timer.set_callback(enqueue_recalculate)
                self.calculate_timer = timer
                timer.attach()
            except BaseException:
                rollback()
                if self.calculate_timer is timer:
                    self.calculate_timer = 0
                if timer is not None:
                    timer.destroy()
                raise

    def cancel_recalculate_timer(self) -> None:
        with self._encoding_state_lock:
            timer = self.calculate_timer
            self.calculate_timer = 0
            self._calculate_request = None
            self.calculate_window_ids.clear()
        if timer:
            timer.destroy()

    def parse_client_caps(self, c: typedict) -> None:
        # batch options:
        # since v6.3, we can have a "batch" dict in the "encoding" caps
        # rather than having it at the top level:
        batch_caps = c.get("batch", {})
        if BACKWARDS_COMPATIBLE and not isinstance(c.get("encoding"), dict):
            enc_caps = {}
        else:
            enc_caps = c.dictget("encoding")
        batch_caps = enc_caps.get("batch", batch_caps)

        def batch_value(prop: str, default: int, minv=-1, maxv=-1) -> int:
            assert default is not None
            raw_value = os.environ.get(f"XPRA_BATCH_{prop.upper()}") or batch_caps.get(prop) or c.get(f"batch.{prop}")
            v = parse_batch_int(raw_value, prop, default)
            assert v is not None
            if minv >= 0:
                v = max(minv, v)
            if maxv >= 0:
                v = min(maxv, v)
            return v

        # general features:
        self.lz4 = c.boolget("lz4", False) and use("lz4")
        self.brotli = c.boolget("brotli", False) and use("brotli")
        log("compressors: lz4=%s, brotli=%s", self.lz4, self.brotli)

        delay = batch_config.START_DELAY
        dbc = self.default_batch_config
        dbc.always = bool(batch_value("always", int(dbc.always)))
        dbc.min_delay = batch_value("min_delay", dbc.min_delay, 0, 1000)
        dbc.max_delay = batch_value("max_delay", dbc.max_delay, 1, 15000)
        dbc.max_events = batch_value("max_events", dbc.max_events)
        dbc.max_pixels = batch_value("max_pixels", dbc.max_pixels)
        dbc.time_unit = batch_value("time_unit", dbc.time_unit, 1)
        dbc.delay = batch_value("delay", delay, dbc.min_delay)
        log("default batch config: %s", dbc)
        self.vrefresh = c.intget("vrefresh", -1)

        evalue = c.get("encoding")
        if isinstance(evalue, dict):
            eopts = typedict(evalue)
        else:
            eopts = typedict()
        self.parse_encoding_caps(c, eopts)

    def parse_encoding_caps(self, c: typedict, eopts: typedict) -> None:
        window_requested = wants_windows(c)
        if not BACKWARDS_COMPATIBLE:
            # should not be used, so blank it:
            c = typedict()
        self.encoding_options.update(eopts)
        self.encodings = eopts.strtupleget("options") or c.strtupleget("encodings")
        self.core_encodings = eopts.strtupleget("core") or c.strtupleget("encodings.core", self.encodings)
        if not self.core_encodings and window_requested:
            raise ClientException("client failed to specify any supported encodings")
        self.full_csc_modes = eopts.dictget("full_csc_modes")
        log("encodings=%s, core_encodings=%s", self.encodings, self.core_encodings)

        self.window_icon_encodings = eopts.strtupleget("window-icon") or c.strtupleget("encodings.window-icon")
        log("window_icon_encodings=%s", self.window_icon_encodings)
        self.rgb_formats = eopts.strtupleget("rgb_formats") or c.strtupleget("encodings.rgb_formats")

        self.set_encoding(eopts.strget("setting") or eopts.strget(""), None)
        # encoding options (filter):
        # 1: these properties are special cased here because we
        # defined their name before the "encoding." prefix convention,
        # or because we want to pass default values (ie: lz4):
        for k, ek in {
            "initial_quality": "initial_quality",
            "quality": "quality",
        }.items():
            if k in c:
                self.encoding_options[ek] = c.intget(k)
        for k, ek in {
            "lz4": "rgb_lz4",
            "zstd": "rgb_zstd",
        }.items():
            if k in c:
                self.encoding_options[ek] = c.boolget(k)
        # 2: standardized encoding options:
        self.icons_encoding_options.update(self.encoding_options.pop("icons", None) or {})
        for k in c.keys():
            if k.startswith("theme.") or k.startswith("encoding.icons."):
                self.icons_encoding_options[k.replace("encoding.icons.", "").replace("theme.", "")] = c.get(k)
            elif k.startswith("encoding."):
                stripped_k = k.removeprefix("encoding.")
                if stripped_k in ("transparency", "rgb_lz4", "rgb_zstd"):
                    v = c.boolget(k)
                elif stripped_k in (
                    "initial_quality", "initial_speed",
                    "min-quality", "quality",
                    "min-speed", "speed",
                ):
                    v = c.intget(k)
                else:
                    v = c.get(k)
                self.encoding_options[stripped_k] = v
        log("encoding options: %s", self.encoding_options)
        log("icons encoding options: %s", self.icons_encoding_options)

        sc = self.encoding_options.get("scaling.control", self.scaling_control)
        if sc is not None:
            self.default_encoding_options["scaling.control"] = sc
        q = self.encoding_options.intget("quality", self.default_quality)  # 0.7 onwards:
        if q > 0:
            self.default_encoding_options["quality"] = q
        mq = self.encoding_options.intget("min-quality", self.default_min_quality)
        if mq > 0 and (q <= 0 or q > mq):
            self.default_encoding_options["min-quality"] = mq
        s = self.encoding_options.intget("speed", self.default_speed)
        if s > 0:
            self.default_encoding_options["speed"] = s
        ms = self.encoding_options.intget("min-speed", self.default_min_speed)
        if ms > 0 and (s <= 0 or s > ms):
            self.default_encoding_options["min-speed"] = ms
        log("default encoding options: %s", self.default_encoding_options)
        self.set_min_speed(ms)
        self.set_min_quality(mq)
        self.auto_refresh_delay = c.intget("auto_refresh_delay", 0)
        # are we going to need a cuda context?
        if not self.cuda_device_context and self.wants_cuda_device():
            self.allocate_cuda_device_context()

    def wants_cuda_device(self) -> bool:
        if getattr(self, "mmap_write_area", None):
            return False
        from xpra.codecs.loader import has_codec
        common_encodings = set(x for x in self.encodings if x in self.server_encodings)
        return any((
            has_codec("nvenc") and {"h264", "h265", "av1"} & common_encodings,
            has_codec("enc_nvjpeg") and "jpeg" in common_encodings,
        ))

    def allocate_cuda_device_context(self):
        cudalog = Logger("cuda")
        with self._encoding_state_lock:
            if self._encoding_closed or self.is_closed():
                return None
            if self.cuda_device_context:
                return self.cuda_device_context
        try:
            # pylint: disable=import-outside-toplevel
            from xpra.codecs.nvidia.cuda.context import get_device_context
        except ImportError as e:
            cudalog(f"unable to import cuda context: {e}")
            return None
        try:
            candidate = get_device_context(self.encoding_options)
        except Exception as e:
            cudalog("failed to get a cuda device context using encoding options %s",
                    self.encoding_options, exc_info=True)
            cudalog.error("Error: failed to allocate a CUDA context:")
            cudalog.estr(e)
            cudalog.error(" NVJPEG and NVENC will not be available")
            return None
        if not candidate:
            return None
        with self._encoding_state_lock:
            if self._encoding_closed or self.is_closed():
                current = None
            elif current := self.cuda_device_context:
                pass
            else:
                self.cuda_device_context = current = candidate
        if current is not candidate:
            candidate.free()
        cudalog("cuda_device_context=%s", current)
        return current

    def print_encoding_info(self) -> None:
        log("print_encoding_info() core-encodings=%s, server-core-encodings=%s",
            self.core_encodings, self.server_core_encodings)
        others = tuple(x for x in self.core_encodings
                       if x in self.server_core_encodings and x != self.encoding)
        if self.encoding == "auto":
            s = "automatic picture encoding enabled"
        elif self.encoding == "stream":
            s = "streaming mode enabled"
        else:
            s = f"using {self.encoding!r} as primary encoding"
        if others:
            log.info(f" {s}, also available:")
            log.info("  " + csv(others))
        else:
            log.warn(f" {s}")
            log.warn("  no other encodings are available!")

    ######################################################################
    # Functions used by the server to request something
    # (window events, stats, user requests, etc)
    #
    def set_auto_refresh_delay(self, delay: int, window_ids) -> None:
        if window_ids is not None:
            wss = tuple(ws for wid in window_ids for ws in self.get_window_pixel_sources(wid))
        else:
            wss = self.all_pixel_sources()
        for ws in wss:
            if ws is not None:
                ws.set_auto_refresh_delay(delay)

    def set_encoding(self, encoding: str, window_ids, strict=False) -> None:
        """ Changes the encoder for the given 'window_ids',
            or for all windows if 'window_ids' is None.
        """
        log("set_encoding(%s, %s, %s)", encoding, window_ids, strict)
        if encoding and encoding not in ("auto", "stream"):
            # old clients (v0.9.x and earlier) only supported 'rgb24' as 'rgb' mode:
            if encoding == "rgb24":
                encoding = "rgb"
            if encoding not in self.encodings:
                log.warn(f"Warning: client specified {encoding!r} encoding,")
                log.warn(" but it only supports: " + csv(self.encodings))
            if encoding not in self.server_encodings:
                log.error(f"Error: encoding {encoding!r} is not supported by this server")
                log.error(" server encodings: " + csv(self.server_encodings))
                encoding = ""
        if not encoding:
            encoding = "auto"
        if window_ids is not None:
            wss = tuple(ws for wid in window_ids for ws in self.get_window_pixel_sources(wid))
        else:
            wss = self.all_pixel_sources()
        # if we're updating all the windows, reset global stats too:
        if set(wss).issuperset(self.all_window_sources()):
            log("resetting global stats")
            # we can't assume that 'self' is a full ClientConnection object:
            stats = getattr(self, "statistics", None)
            if stats:
                stats.reset()
            self.global_batch_config = self.default_batch_config.clone()
        for ws in wss:
            if ws is not None:
                ws.set_new_encoding(encoding, strict)
        if not window_ids:
            self.encoding = encoding

    def get_info(self) -> dict[str, Any]:
        einfo = {
            "default": self.default_encoding or "",
            "defaults": dict(self.default_encoding_options),
            "client-defaults": dict(self.encoding_options),
        }
        ieo = dict(self.icons_encoding_options)
        ieo.pop("default.icons", None)
        info: dict[str, Any] = {
            "auto_refresh": self.auto_refresh_delay,
            "lz4": self.lz4,
            "encodings": {
                "": self.encodings,
                "core": self.core_encodings,
                "window-icon": self.window_icon_encodings,
            },
            "icons": ieo,
            "encoding": einfo,
        }
        return info

    def set_min_quality(self, min_quality: int) -> None:
        for ws in self.all_pixel_sources():
            ws.set_min_quality(min_quality)

    def set_max_quality(self, max_quality: int) -> None:
        for ws in self.all_pixel_sources():
            ws.set_max_quality(max_quality)

    def set_quality(self, quality: int) -> None:
        for ws in self.all_pixel_sources():
            ws.set_quality(quality)

    def set_min_speed(self, min_speed: int) -> None:
        for ws in self.all_pixel_sources():
            ws.set_min_speed(min_speed)

    def set_max_speed(self, max_speed: int) -> None:
        for ws in self.all_pixel_sources():
            ws.set_max_speed(max_speed)

    def set_speed(self, speed: int) -> None:
        for ws in self.all_pixel_sources():
            ws.set_speed(speed)

    def make_batch_config(self, wid: int, window):
        config = self.default_batch_config.clone()
        config.wid = wid
        # scale initial delay based on window size
        # (the global value is normalized to 1MPixel)
        # but use sqrt to smooth things and prevent excesses
        # (ie: a 4MPixel window, will start at 2 times the global delay)
        # (ie: a 0.5MPixel window will start at 0.7 times the global delay)
        dpm = self.global_batch_config.delay_per_megapixel
        w, h = window.get_dimensions()
        if dpm >= 0:
            ratio = sqrt(1000000.0 / (w * h))
            config.delay = max(config.min_delay, min(config.max_delay, int(dpm * sqrt(ratio))))
        log("make_batch_config(%i, %s) global delay per megapixel=%i, new window delay for %ix%i=%s",
            wid, window, dpm, w, h, config.delay)
        return config
