# This file is part of Xpra.
# Copyright (C) 2011 Serviware (Arthur Huillet, <ahuillet@serviware.com>)
# Copyright (C) 2010 Antoine Martin <antoine@xpra.org>
# Copyright (C) 2008 Nathaniel Smith <njs@pobox.com>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

from collections import deque
from time import monotonic
from typing import Any

from xpra.net.common import Packet, BACKWARDS_COMPATIBLE
from xpra.net.packet_type import WINDOW_ACK, WINDOW_DRAW_ACK
from xpra.common import SUBSURFACE_CLIENT_BACKING_STATE, SUBSURFACE_TRANSACTION_ID
from xpra.constants import WINDOW_DECODE_SKIPPED, WINDOW_DECODE_ERROR, WINDOW_NOT_FOUND
from xpra.util.objects import typedict
from xpra.util.str_fn import repr_ellipsized
from xpra.util.env import envint, envbool
from xpra.client.base.stub import StubClientSubsystem
from xpra.log import Logger

log = Logger("window", "draw")
paintlog = Logger("window", "paint")

PAINT_FAULT_RATE: int = envint("XPRA_PAINT_FAULT_INJECTION_RATE")
PAINT_FAULT_TELL: bool = envbool("XPRA_PAINT_FAULT_INJECTION_TELL", True)
PAINT_DELAY: int = envint("XPRA_PAINT_DELAY", -1)

DRAW_LOG_FMT = "process_draw: %7i %8s for window %3i, sequence %8i, %4ix%-4i at %4i,%-4i" \
               " using %6s encoding with options=%s"

DRAW_TYPES: dict[type, str] = {bytes: "bytes", str: "bytes", tuple: "arrays", list: "arrays"}


class WindowDraw(StubClientSubsystem):
    __slots__ = ()
    # these mixins are all composed into a single `WindowClient` instance,
    # which is the subsystem registered as `window`: declaring the prefix here
    # too keeps each mixin usable (and testable) on its own
    PREFIX = "window"
    SLOT_NAMES = ("_draw_counter", "pixel_counter")

    def __init__(self):
        self._draw_counter: int = 0
        self.pixel_counter: deque = deque(maxlen=1000)

    def get_info(self) -> dict[str, Any]:
        return {
            "draw-counter": self._draw_counter,
        }

    ######################################################################
    # painting windows:
    # these handlers only queue the work, but they must stay on the UI thread
    # (`main_thread=True`): that hop is what orders them after the `new-window`
    # that creates the window they refer to - do not "optimize" it away.
    def _process_draw(self, packet: Packet) -> None:
        # Capture the exact UI target before the packet leaves ordered protocol
        # dispatch for the asynchronous decode queue. A resize or window
        # replacement can otherwise overtake an already dequeued draw before
        # its eventual UI / GL paint callback.
        delivery_state = None
        try:
            wid = packet.get_wid()
        except (IndexError, TypeError, ValueError):
            # Preserve the existing decode-thread packet validation boundary.
            # A malformed packet will be rejected when `_do_draw` parses it;
            # the UI ingress hook must not make that failure synchronous.
            pass
        else:
            window = self.get_window(wid)
            backing = getattr(window, "_backing", None)
            delivery_state = (
                window,
                backing,
                getattr(window, "_subsurface_backing_generation", 0),
                getattr(backing, "_subsurface_local_backing_epoch", 0),
            )
        if PAINT_DELAY >= 0:
            self.timeout_add(PAINT_DELAY, self.add_decode_work, self._do_draw, packet, delivery_state)
        else:
            self.add_decode_work(self._do_draw, packet, delivery_state)

    def _process_eos(self, packet: Packet) -> None:
        self.add_decode_work(self._do_draw, packet)

    def send_draw_ack(self, wid: int, width: int, height: int, packet_sequence: int,
                      decode_time: int, message="") -> None:
        if WINDOW_ACK in self.get_server_packet_types() or not BACKWARDS_COMPATIBLE:
            args = (WINDOW_ACK, wid, width, height, packet_sequence, decode_time, message)
        else:
            # the legacy ack packet starts with the `packet_sequence`:
            args = (WINDOW_DRAW_ACK, packet_sequence, wid, width, height, decode_time, message)
        log("sending ack: %s", args)
        self.send_now(*args)

    def _do_draw(self, packet: Packet, delivery_state=None) -> None:
        """ this runs from the decode thread (see `xpra/client/subsystem/decode.py`) """
        wid = packet.get_wid()
        window = self.get_window(wid)
        # legacy `eos` packets are rewritten to `window-eos` by the packet alias:
        if packet.get_type() == "window-eos":
            if window:
                window.eos()
            return
        x = packet.get_i16(2)
        y = packet.get_i16(3)
        width = packet.get_u16(4)
        height = packet.get_u16(5)
        coding = packet.get_str(6)
        if BACKWARDS_COMPATIBLE:
            # mmap can send a tuple, otherwise it's a buffer, see #4496:
            data = packet[7]
        else:
            data = packet.get_buffer(7)
        packet_sequence = packet.get_u64(8)
        rowstride = packet.get_u32(9)
        # rename old encoding aliases early:
        options = typedict()
        if len(packet) > 10:
            options.update(packet.get_dict(10))
        dtype = DRAW_TYPES.get(type(data), type(data))
        log(DRAW_LOG_FMT, len(data), dtype, wid, packet_sequence, width, height, x, y, coding, options)
        start = monotonic()

        def record_draw_error(code=WINDOW_NOT_FOUND, message="window not found") -> None:
            self.send_draw_ack(wid, width, height, packet_sequence, code, message)

        def draw_abort(code=WINDOW_NOT_FOUND, message="window not found") -> None:
            if coding == "mmap":
                # we need to ack the data to free the space!
                # (the mmap read area is owned by the `mmap` subsystem)
                if mmap := self.get_subsystem("mmap"):
                    mmap.free_packet_chunks(data, options)
            record_draw_error(code, message)

        def record_decode_time(success: bool | int, message="") -> None:
            if success > 0:
                end = monotonic()
                decode_time = round(end * 1000 * 1000 - start * 1000 * 1000)
                self.pixel_counter.append((start, end, width * height))
                dms = "%sms" % (int(decode_time / 100) / 10.0)
                paintlog("record_decode_time(%s, %s) wid=%#x, %s: %sx%s, %s",
                         success, message, wid, coding, width, height, dms)
            elif success == 0:
                decode_time = WINDOW_DECODE_ERROR
                paintlog("record_decode_time(%s, %s) decoding error on wid=%#x, %s: %sx%s",
                         success, message, wid, coding, width, height)
            else:
                assert success < 0
                decode_time = WINDOW_DECODE_SKIPPED
                paintlog("record_decode_time(%s, %s) decoding or painting skipped on wid=%#x, %s: %sx%s",
                         success, message, wid, coding, width, height)
            self.send_draw_ack(wid, width, height, packet_sequence, decode_time, repr_ellipsized(message, 512))

        if not window:
            # window is gone
            self.idle_add(draw_abort, WINDOW_NOT_FOUND, "window not found")
            return

        composite_present = "subsurface-composite" in options
        backing = None
        if composite_present:
            backing = getattr(window, "_backing", None)
            generation = getattr(window, "_subsurface_backing_generation", 0)
            local_epoch = getattr(backing, "_subsurface_local_backing_epoch", 0)
            current_delivery = delivery_state is None
            if isinstance(delivery_state, tuple) and len(delivery_state) == 4:
                delivered_window, delivered_backing, delivered_generation, delivered_epoch = delivery_state
                current_delivery = (
                    delivered_window is window
                    and delivered_backing is backing
                    and delivered_generation == generation
                    and delivered_epoch == local_epoch
                )
            if not current_delivery:
                self.idle_add(
                    draw_abort, WINDOW_DECODE_ERROR,
                    "stale subsurface draw target after backing reconfiguration",
                )
                return
            # This private value overwrites any untrusted packet member. The
            # backing checks object identity and both generations again inside
            # its UI / GL context, immediately before staging can begin.
            options[SUBSURFACE_CLIENT_BACKING_STATE] = (
                backing, generation, local_epoch,
            )

        def reject_owned_composite(callbacks, message: str) -> None:
            """Fail an accepted composite packet at its UI / GL owner."""
            try:
                backing.reject_subsurface_composite(
                    options.get(SUBSURFACE_TRANSACTION_ID), callbacks, message,
                )
            except Exception:
                log.error("Error invalidating a failed subsurface draw", exc_info=True)
                self.idle_add(record_draw_error, WINDOW_DECODE_ERROR, message)

        def inject_composite_failure(message: str) -> None:
            # Route the synthetic failure through ClientWindowBase so its
            # private refresh accumulator and the backing transaction are
            # completed together. A malformed mmap claim still has to consume
            # its shared-memory descriptor before the failure acknowledgement.
            failure_coding = "mmap" if coding == "mmap" else "void"
            failure_data = data if failure_coding == "mmap" else b""
            failure_stride = rowstride if failure_coding == "mmap" else 0
            callbacks = [record_decode_time]
            try:
                window.draw_region(
                    x, y, width, height, failure_coding, failure_data,
                    failure_stride, options, callbacks,
                )
            except Exception:
                log.error("Error routing a failed subsurface draw", exc_info=True)
                reject_owned_composite(callbacks, message)

        # the list of allowed encodings is owned by the `encoding` subsystem:
        encoding_subsystem = self.get_subsystem("encoding")
        allowed_encodings = encoding_subsystem.allowed_encodings if encoding_subsystem else ("rgb32", "rgb24", "mmap",)
        # A packet which claims the negotiated composite mode must reach the
        # backing even when its coding is unsupported. The backing owns the
        # private transaction and must invalidate its staging before ACKing the
        # malformed stage; this early generic reject cannot perform that work.
        if (not composite_present
                and coding not in allowed_encodings and coding != "mmap"):
            log.warn("Warning: server sent unsupported encoding %r", coding)
            self.idle_add(draw_abort, WINDOW_DECODE_ERROR, f"unsupported encoding {coding!r}")
            return

        self._draw_counter += 1
        if PAINT_FAULT_RATE > 0 and (self._draw_counter % PAINT_FAULT_RATE) == 0:
            log.warn("injecting paint fault for %s draw packet %i, sequence number=%i",
                     coding, self._draw_counter, packet_sequence)
            if PAINT_FAULT_TELL:
                msg = f"fault injection for {coding} draw packet {self._draw_counter}, sequence no={packet_sequence}"
                if composite_present:
                    inject_composite_failure(msg)
                else:
                    self.idle_add(draw_abort, WINDOW_DECODE_ERROR, msg)
            return
        # we could expose this to the csc step? (not sure how this could be used)
        # if self.xscale!=1 or self.yscale!=1:
        #    options["client-scaling"] = self.xscale, self.yscale
        callbacks = [record_decode_time]
        try:
            window.draw_region(x, y, width, height, coding, data, rowstride, options, callbacks)
        except Exception as e:
            log.error("Error drawing on window %#x", wid)
            log.error(f" using encoding {coding} with {options=}", exc_info=True)
            if composite_present:
                reject_owned_composite(callbacks, str(e))
            else:
                self.idle_add(record_draw_error, WINDOW_DECODE_ERROR, str(e))

    ######################################################################
    # packets:
    def init_authenticated_packet_handlers(self) -> None:
        if BACKWARDS_COMPATIBLE:
            self.add_legacy_alias("draw", "window-draw")
            self.add_legacy_alias("eos", "window-eos")
        # `main_thread=True` is required for ordering, not for the (trivial) handlers:
        # see `_process_draw` above
        self.add_packets("window-draw", "window-eos", main_thread=True)
