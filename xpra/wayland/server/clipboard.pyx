#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

# cython: language_level=3

from typing import Tuple

import os

from libc.stdint cimport uintptr_t, uint32_t
from libc.stdlib cimport calloc, free, malloc
from libc.string cimport memcpy
from cpython.ref cimport Py_INCREF, Py_DECREF

from xpra.clipboard.common import ClipboardCallback
from xpra.clipboard.core import MAX_CLIPBOARD_PACKET_SIZE
from xpra.clipboard.proxy import ClipboardProxyCore
from xpra.clipboard.timeout import ClipboardTimeoutHelper, REMOTE_TIMEOUT
from xpra.os_util import gi_import
from xpra.util.gobject import n_arg_signal, one_arg_signal
from xpra.util.str_fn import Ellipsizer, bytestostr
from xpra.log import Logger

from xpra.wayland.server.wlroots cimport (
    wl_array_add,
    wl_display, wl_display_next_serial, wl_display_flush_clients,
    wl_array,
    wlr_data_source, wlr_data_source_impl,
    wlr_data_source_init, wlr_data_source_destroy, wlr_data_source_send,
    wlr_seat_set_selection,
    wlr_seat, wlr_seat_set_primary_selection,
    wlr_primary_selection_source, wlr_primary_selection_source_impl,
    wlr_primary_selection_source_init, wlr_primary_selection_source_destroy,
    wlr_primary_selection_source_send,
)


GLib = gi_import("GLib")
GObject = gi_import("GObject")

log = Logger("wayland", "clipboard")

WAYLAND_CLIPBOARDS = ("CLIPBOARD", "PRIMARY")
ORIGIN_MIME_TYPE = "application/x-xpra-clipboard-origin"
MAX_ORIGIN_SIZE = 64


def remove_source(source_id) -> None:
    try:
        GLib.source_remove(source_id)
    except Exception:
        # Registries are retired before cancellation; escaped callbacks check
        # their request identity. One failed removal must not strand other FDs.
        log("failed to remove Wayland clipboard source", exc_info=True)


cdef wlr_data_source_impl DATA_SOURCE_IMPL
cdef wlr_primary_selection_source_impl PRIMARY_SOURCE_IMPL
DATA_SOURCE_OWNERS = {}


cdef bytes bstr(value):
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    return str(value).encode("utf8")


cdef void add_mime_type(wl_array *mime_types, bytes mime):
    cdef char **slot
    cdef char *value
    cdef size_t size
    if mime_types == NULL or not mime:
        return
    size = len(mime) + 1
    value = <char*> malloc(size)
    if value == NULL:
        raise MemoryError("failed to allocate selection mime type")
    memcpy(value, <const char*> mime, size - 1)
    value[size - 1] = 0
    slot = <char**> wl_array_add(mime_types, sizeof(char*))
    if slot == NULL:
        free(value)
        raise MemoryError("failed to append selection mime type")
    slot[0] = value


cdef tuple source_mime_types(wl_array *mime_types):
    cdef char **values
    cdef size_t count
    cdef size_t i
    if mime_types == NULL:
        return ()
    values = <char**> mime_types.data
    count = mime_types.size // sizeof(char*)
    return tuple(values[i].decode("utf8", "replace") for i in range(count) if values[i] != NULL)


cdef void data_source_send(wlr_data_source *source, const char *mime_type, int fd) noexcept:
    cdef bint transferred = False
    try:
        owner = DATA_SOURCE_OWNERS.get(<uintptr_t> source) if source != NULL else None
        if owner is None:
            return
        target = mime_type.decode("utf8", "replace") if mime_type != NULL else ""
        # The wrapper/proxy owns all completion and error paths after this
        # handoff.  Its cleanup may already close and reuse the descriptor.
        transferred = True
        owner.send(target, fd)
    except Exception:
        log.error("Error sending selection contents", exc_info=True)
    finally:
        if not transferred:
            try:
                os.close(fd)
            except OSError:
                pass


cdef void data_source_destroy(wlr_data_source *source) noexcept:
    try:
        owner = DATA_SOURCE_OWNERS.pop(<uintptr_t> source, None) if source != NULL else None
        if owner is not None:
            owner.destroyed()
    except Exception:
        log.error("Error destroying selection source", exc_info=True)
    free(source)


cdef void primary_source_send(wlr_primary_selection_source *source, const char *mime_type, int fd) noexcept:
    cdef bint transferred = False
    try:
        owner = <object> source.data if source != NULL and source.data != NULL else None
        if owner is None:
            return
        target = mime_type.decode("utf8", "replace") if mime_type != NULL else ""
        transferred = True
        owner.send(target, fd)
    except Exception:
        log.error("Error sending primary selection contents", exc_info=True)
    finally:
        if not transferred:
            try:
                os.close(fd)
            except OSError:
                pass


cdef void primary_source_destroy(wlr_primary_selection_source *source) noexcept:
    try:
        owner = <object> source.data if source != NULL and source.data != NULL else None
        if owner is not None:
            source.data = NULL
            try:
                owner.destroyed()
            finally:
                Py_DECREF(owner)
    except Exception:
        log.error("Error destroying primary selection source", exc_info=True)
    free(source)


cdef class WaylandSelectionSource:
    cdef wlr_data_source *source
    cdef object proxy
    cdef object target_data

    def __cinit__(self):
        self.source = NULL
        self.proxy = None
        self.target_data = {}

    def __init__(self, proxy, targets, target_data=None):
        cdef bytes mime
        if DATA_SOURCE_IMPL.send == NULL:
            DATA_SOURCE_IMPL.send = data_source_send
            DATA_SOURCE_IMPL.destroy = data_source_destroy
        self.proxy = proxy
        self.target_data = target_data or {}
        self.source = <wlr_data_source*> calloc(1, sizeof(wlr_data_source))
        if self.source == NULL:
            raise MemoryError("failed to allocate selection source")
        wlr_data_source_init(self.source, &DATA_SOURCE_IMPL)
        try:
            DATA_SOURCE_OWNERS[self.ptr()] = self
            for target in targets or ():
                mime = bstr(target)
                add_mime_type(&self.source.mime_types, mime)
        except Exception:
            # This candidate has never reached the seat. Break its native
            # owner reference without notifying the proxy about a replacement.
            self.proxy = None
            self.destroy()
            raise

    def ptr(self) -> int:
        return <uintptr_t> self.source

    def destroy(self) -> None:
        cdef wlr_data_source *source = self.source
        if source != NULL:
            self.source = NULL
            wlr_data_source_destroy(source)

    def destroyed(self) -> None:
        proxy = self.proxy
        self.source = NULL
        self.proxy = None
        if proxy is not None:
            proxy.source_destroyed(self)

    def send(self, mime_type: str, fd: int) -> None:
        proxy = self.proxy
        if proxy is None:
            os.close(fd)
            return
        proxy.send_remote_contents(self, mime_type, fd, self.target_data)


cdef class WaylandPrimarySource:
    cdef wlr_primary_selection_source *source
    cdef object proxy
    cdef object target_data

    def __cinit__(self):
        self.source = NULL
        self.proxy = None
        self.target_data = {}

    def __init__(self, proxy, targets, target_data=None):
        cdef bytes mime
        if PRIMARY_SOURCE_IMPL.send == NULL:
            PRIMARY_SOURCE_IMPL.send = primary_source_send
            PRIMARY_SOURCE_IMPL.destroy = primary_source_destroy
        self.proxy = proxy
        self.target_data = target_data or {}
        self.source = <wlr_primary_selection_source*> calloc(1, sizeof(wlr_primary_selection_source))
        if self.source == NULL:
            raise MemoryError("failed to allocate primary selection source")
        wlr_primary_selection_source_init(self.source, &PRIMARY_SOURCE_IMPL)
        Py_INCREF(self)
        self.source.data = <void*> self
        try:
            for target in targets or ():
                mime = bstr(target)
                add_mime_type(&self.source.mime_types, mime)
        except Exception:
            self.proxy = None
            self.destroy()
            raise

    def ptr(self) -> int:
        return <uintptr_t> self.source

    def destroy(self) -> None:
        cdef wlr_primary_selection_source *source = self.source
        if source != NULL:
            self.source = NULL
            wlr_primary_selection_source_destroy(source)

    def destroyed(self) -> None:
        proxy = self.proxy
        self.source = NULL
        self.proxy = None
        if proxy is not None:
            proxy.source_destroyed(self)

    def send(self, mime_type: str, fd: int) -> None:
        proxy = self.proxy
        if proxy is None:
            os.close(fd)
            return
        proxy.send_remote_contents(self, mime_type, fd, self.target_data)


cdef class WaylandSelection:
    cdef wlr_seat *seat
    cdef wl_display *display

    def __init__(self, uintptr_t display_ptr, uintptr_t seat_ptr):
        self.display = <wl_display*> display_ptr
        self.seat = <wlr_seat*> seat_ptr

    def set_source(self, WaylandSelectionSource source) -> None:
        cdef uint32_t serial
        if self.display == NULL or self.seat == NULL:
            raise RuntimeError("selection seat is not available")
        serial = wl_display_next_serial(self.display)
        wlr_seat_set_selection(self.seat, source.source, serial)
        self.flush()

    def clear(self) -> None:
        cdef uint32_t serial
        if self.display == NULL or self.seat == NULL:
            return
        serial = wl_display_next_serial(self.display)
        wlr_seat_set_selection(self.seat, NULL, serial)
        self.flush()

    def source_targets(self, uintptr_t source_ptr) -> tuple:
        cdef wlr_data_source *source = <wlr_data_source*> source_ptr
        if source == NULL:
            return ()
        return source_mime_types(&source.mime_types)

    def send_source(self, uintptr_t source_ptr, str target, int fd) -> None:
        cdef bytes mime = bstr(target)
        cdef wlr_data_source *source = <wlr_data_source*> source_ptr
        if source == NULL or self.display == NULL:
            return
        # wlroots transfers ownership to the source implementation, which
        # closes this duplicate.  The caller still owns its original FD.
        wlr_data_source_send(source, <const char*> mime, os.dup(fd))
        self.flush()


    cdef void flush(self) noexcept:
        # the wlroots calls above only queue events on the native clients' connections.
        # We get here from xpra packet handlers and GLib callbacks, so the compositor's
        # own dispatch is not about to run and flush them for us:
        # an unflushed `selection` event leaves the client unaware that the clipboard
        # changed, and an unflushed `send` event leaves it waiting on a pipe
        # that nobody told it to write to.
        if self.display != NULL:
            wl_display_flush_clients(self.display)


cdef class WaylandPrimarySelection:
    cdef wlr_seat *seat
    cdef wl_display *display

    def __init__(self, uintptr_t display_ptr, uintptr_t seat_ptr):
        self.display = <wl_display*> display_ptr
        self.seat = <wlr_seat*> seat_ptr

    def set_source(self, WaylandPrimarySource source) -> None:
        cdef uint32_t serial
        if self.display == NULL or self.seat == NULL:
            raise RuntimeError("primary selection seat is not available")
        serial = wl_display_next_serial(self.display)
        wlr_seat_set_primary_selection(self.seat, source.source, serial)
        self.flush()

    def clear(self) -> None:
        cdef uint32_t serial
        if self.display == NULL or self.seat == NULL:
            return
        serial = wl_display_next_serial(self.display)
        wlr_seat_set_primary_selection(self.seat, NULL, serial)
        self.flush()

    def source_targets(self, uintptr_t source_ptr) -> Tuple:
        cdef wlr_primary_selection_source *source = <wlr_primary_selection_source*> source_ptr
        if source == NULL:
            return ()
        return source_mime_types(&source.mime_types)

    def send_source(self, uintptr_t source_ptr, str target, int fd) -> None:
        cdef bytes mime = bstr(target)
        cdef wlr_primary_selection_source *source = <wlr_primary_selection_source*> source_ptr
        if source == NULL or self.display == NULL:
            return
        wlr_primary_selection_source_send(source, <const char*> mime, os.dup(fd))
        self.flush()


    cdef void flush(self) noexcept:
        # the wlroots calls above only queue events on the native clients' connections.
        # We get here from xpra packet handlers and GLib callbacks, so the compositor's
        # own dispatch is not about to run and flush them for us:
        # an unflushed `selection` event leaves the client unaware that the clipboard
        # changed, and an unflushed `send` event leaves it waiting on a pipe
        # that nobody told it to write to.
        if self.display != NULL:
            wl_display_flush_clients(self.display)


class WaylandPrimaryClipboardProxy(ClipboardProxyCore, GObject.GObject):
    __gsignals__ = {
        "send-clipboard-token": one_arg_signal,
        "send-clipboard-request": n_arg_signal(3),
    }

    # the two selections differ only in which native objects they are made of:
    SELECTION_SIGNAL = "primary-selection"
    SELECTION_API = WaylandPrimarySelection
    SOURCE_CLASS = WaylandPrimarySource

    def __init__(self, selection, compositor):
        ClipboardProxyCore.__init__(self, selection)
        GObject.GObject.__init__(self)
        self.compositor = compositor
        self.selection_api = self.SELECTION_API(compositor.get_display_ptr(), compositor.get_seat_ptr())
        self.closing = False
        self.compositor_handler = None
        self.remote_generation = 0
        self.local_source_ptr = 0
        self.source_generation = 0
        self.remote_source = None
        self.remote_source_ptr = 0
        self.targets = ()
        self.target_data = {}
        self.pending_reads = {}
        self.pending_read_timers = {}
        self.pending_read_counter = 0
        self.read_limit = lambda: MAX_CLIPBOARD_PACKET_SIZE
        self.pending_writes = {}
        self.pending_write_sources = {}
        self.pending_write_counter = 0
        self.compositor_handler = compositor.connect(self.SELECTION_SIGNAL, self.selection_changed)

    def __repr__(self):
        return "WaylandPrimaryClipboardProxy(%s)" % self._selection

    def cancel_emit_token(self) -> None:
        try:
            super().cancel_emit_token()
        except Exception:
            # The base retires its timer identity before removal. A scheduler
            # failure cannot turn a committed native replacement into refusal.
            log("failed to cancel Wayland clipboard token", exc_info=True)

    def set_enabled(self, enabled: bool) -> None:
        revoked = self._enabled and not enabled
        super().set_enabled(enabled)
        if revoked:
            self.client_reset()

    def set_direction(self, can_send: bool, can_receive: bool) -> None:
        send_revoked = self._can_send and not can_send
        receive_revoked = self._can_receive and not can_receive
        super().set_direction(can_send, can_receive)
        # The other direction keeps its current owner and transfers. In
        # particular, becoming receive-only must not clear received contents.
        if send_revoked:
            self.source_generation += 1
            self.cancel_pending_reads()
        if receive_revoked:
            self.remote_generation += 1
            self.cancel_pending_writes()
            self.clear_remote_source()

    def cleanup(self) -> None:
        if self.closing:
            return
        self.closing = True
        self.source_generation += 1
        self.remote_generation += 1
        try:
            try:
                super().cleanup()
            finally:
                try:
                    self.cancel_pending_reads()
                finally:
                    try:
                        self.cancel_pending_writes()
                    finally:
                        self.clear_remote_source()
        finally:
            self.disconnect_compositor_signal()

    def disconnect_compositor_signal(self) -> None:
        compositor = self.compositor
        handler = self.compositor_handler
        self.compositor_handler = None
        self.compositor = None
        if compositor is not None and handler is not None:
            try:
                compositor.disconnect(self.SELECTION_SIGNAL, handler)
            except Exception:  # noqa: BLE001
                log("failed to disconnect the Wayland clipboard signal", exc_info=True)

    def client_reset(self) -> None:
        if self.closing:
            return
        self.source_generation += 1
        self.remote_generation += 1
        self.cancel_pending_reads()
        self.cancel_pending_writes()
        self.clear_remote_source()
        self._clipboard_origin = ""

    def selection_changed(self, source_ptr: int) -> None:
        log("%s selection_changed(%#x) remote=%#x", self._selection, source_ptr, self.remote_source_ptr)
        if self.closing:
            return
        # wlroots frees the outgoing source before a new one is allocated, and both are
        # fixed-size heap objects - so the next source can land at the address the last
        # one had. Only this counter can tell an asynchronous read that it is stale:
        self.source_generation += 1
        self.local_source_ptr = source_ptr
        self.cancel_pending_reads(source_ptr, notify=True)
        if source_ptr == 0:
            self.targets = ()
            self.target_data = {}
            self._clipboard_origin = ""
            return
        if source_ptr == self.remote_source_ptr:
            return
        self.local_source_changed(source_ptr)

    def local_source_changed(self, source_ptr: int) -> None:
        targets = self.selection_api.source_targets(source_ptr)
        generation = self.source_generation
        self.targets = tuple(x for x in targets if x != ORIGIN_MIME_TYPE)
        self.target_data = {}

        def got_origin(_dtype: str, dformat: int, data) -> None:
            if self.closing or generation != self.source_generation:
                return
            origin = bytestostr(data) if dformat == 8 and data else ""
            if len(origin) > MAX_ORIGIN_SIZE:
                log.warn("Warning: ignoring oversized Wayland clipboard origin")
                origin = ""
            self._clipboard_origin = origin
            self.do_owner_changed()

        if ORIGIN_MIME_TYPE in targets:
            self.get_contents(ORIGIN_MIME_TYPE, got_origin)
        else:
            self._clipboard_origin = ""
            self.do_owner_changed()

    def source_destroyed(self, source) -> None:
        self.cancel_pending_writes(source)
        if source is self.remote_source:
            self.remote_source = None
            self.remote_source_ptr = 0

    def clear_remote_source(self) -> None:
        source = self.remote_source
        if source is None:
            return
        source_ptr = self.remote_source_ptr
        self.remote_generation += 1
        # Do not clear a newer local owner which has already replaced our
        # source.  wlroots synchronously destroys the active source while
        # installing NULL; the adapter flushes that queued selection update
        # before this method returns.
        try:
            if source_ptr and self.local_source_ptr == source_ptr:
                self.selection_api.clear()
        finally:
            # The setter normally destroys synchronously. Also destroy when
            # its unavailable adapter cannot clear, without losing the owner.
            try:
                source.destroy()
            finally:
                if self.remote_source is source:
                    self.remote_source = None
                    self.remote_source_ptr = 0

    def cancel_pending_reads(self, source_ptr=None, notify=False) -> None:
        for read_key, pending in tuple(self.pending_reads.items()):
            generation, pending_source_ptr, rfd, source_id, got_contents = pending
            if (
                source_ptr is not None
                and generation == self.source_generation
                and pending_source_ptr == source_ptr
            ):
                continue
            self.pending_reads.pop(read_key, None)
            remove_source(source_id)
            if timer := self.pending_read_timers.pop(read_key, 0):
                remove_source(timer)
            try:
                os.close(rfd)
            except OSError:
                pass
            if notify:
                try:
                    got_contents("", 0, b"")
                except Exception:  # noqa: BLE001
                    log("failed to cancel stale Wayland clipboard read generation %i",
                        generation, exc_info=True)

    def cancel_pending_writes(self, source=None) -> None:
        for write_key, pending in tuple(self.pending_writes.items()):
            pending_source, _generation, _target, _fd = pending
            if source is not None and pending_source is not source:
                continue
            self.finish_pending_write(write_key)

    def finish_pending_write(self, write_key: int) -> None:
        pending = self.pending_writes.pop(write_key, None)
        for source_id in self.pending_write_sources.pop(write_key, ()):
            remove_source(source_id)
        if pending is not None:
            try:
                os.close(pending[3])
            except OSError:
                pass

    def do_owner_changed(self) -> None:
        if self.closing or not self._enabled or not self._can_send:
            return
        self.schedule_emit_token()

    def do_emit_token(self) -> bool:
        if self.closing or not self._enabled or not self._can_send:
            return False
        targets = self.targets if (self._want_targets or self._greedy_client) else ()
        if not self._greedy_client:
            self.emit("send-clipboard-token", {"targets": tuple(targets), "data": {}})
            return True
        eager_targets = self.get_eager_targets(targets)
        source_ptr = self.local_source_ptr
        generation = self.source_generation

        def generation_is_current() -> bool:
            return (
                not self.closing
                and self._enabled and self._can_send
                and generation == self.source_generation
                and source_ptr == self.local_source_ptr
            )

        def got_target_data(target_data) -> None:
            if not generation_is_current():
                return
            self.emit("send-clipboard-token", {
                "targets": tuple(targets),
                "data": target_data,
            })

        self.collect_contents(
            eager_targets, got_target_data, request_valid=generation_is_current,
        )
        return True

    def get_contents(self, target: str, got_contents: ClipboardCallback) -> None:
        log("get_contents(%s, %s) source=%#x", target, got_contents, self.local_source_ptr)
        if self.closing or not self._enabled or not self._can_send:
            got_contents("", 0, b"")
            return
        if target == "TARGETS":
            got_contents("ATOM", 32, self.targets)
            return
        if target_data := self.target_data.get(target):
            dtype, dformat, data = target_data
            got_contents(dtype, dformat, data)
            return
        source_ptr = self.local_source_ptr
        if not source_ptr or source_ptr == self.remote_source_ptr:
            got_contents(target, 0, b"")
            return
        generation = self.source_generation
        max_size = MAX_ORIGIN_SIZE if target == ORIGIN_MIME_TYPE else self.read_limit()
        rfd, wfd = os.pipe()
        data = bytearray()
        read_key = self.pending_read_counter
        self.pending_read_counter += 1
        read_registered = False

        def finish_read(valid: bool) -> None:
            pending = self.pending_reads.pop(read_key, None)
            if pending is None:
                return
            remove_source(pending[3])
            if timer := self.pending_read_timers.pop(read_key, 0):
                remove_source(timer)
            try:
                os.close(pending[2])
            except OSError:
                pass
            if (
                valid
                and not self.closing
                and generation == self.source_generation
                and source_ptr == self.local_source_ptr
            ):
                got_contents(target, 8, bytes(data))
            else:
                got_contents("", 0, b"")

        def io_callback(fd, condition):
            if read_key not in self.pending_reads:
                return False
            if condition & (GLib.IO_ERR | GLib.IO_NVAL):
                finish_read(False)
                return False
            if condition & GLib.IO_IN:
                try:
                    chunk = os.read(fd, 65536)
                except (BlockingIOError, InterruptedError):
                    return True
                except OSError:
                    finish_read(False)
                    return False
                if chunk:
                    if max_size >= 0 and len(data) + len(chunk) > max_size:
                        log.warn("Warning: native Wayland clipboard data exceeds its size limit")
                        finish_read(False)
                        return False
                    data.extend(chunk)
                    return True
            finish_read(True)
            return False

        def read_timeout():
            log.warn("Warning: native Wayland clipboard source timed out")
            finish_read(False)
            return False

        try:
            os.set_blocking(rfd, False)
            source_id = GLib.io_add_watch(
                rfd, GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR | GLib.IO_NVAL, io_callback,
            )
            self.pending_reads[read_key] = (generation, source_ptr, rfd, source_id, got_contents)
            read_registered = True
            self.pending_read_timers[read_key] = GLib.timeout_add(REMOTE_TIMEOUT, read_timeout)
            self.selection_api.send_source(source_ptr, target, wfd)
        except Exception:
            # Release the still-owned write side before a completion can
            # re-enter and allocate another descriptor with its old number.
            os.close(wfd)
            wfd = -1
            if read_registered:
                finish_read(False)
            else:
                os.close(rfd)
                got_contents("", 0, b"")
            raise
        finally:
            if wfd >= 0:
                os.close(wfd)

    def got_token(self, targets, target_data=None, claim=True, _synchronous_client=False) -> bool:
        if self.closing or not self._enabled:
            return False
        self._got_token_events += 1
        log("got_token(%s, %s, claim=%s)", targets, Ellipsizer(target_data), claim)
        # An informational or receive-denied token has not replaced the native
        # owner. Preserve its targets, origin read and pending local emission.
        if not claim or not self._can_receive:
            return False
        new_targets = tuple(
            x for x in (bytestostr(y) for y in (targets or ()))
            if x != ORIGIN_MIME_TYPE
        )
        new_data = dict(target_data or {})
        new_data.pop(ORIGIN_MIME_TYPE, None)
        has_contents = bool(new_targets or new_data)
        if self._clipboard_origin:
            new_data[ORIGIN_MIME_TYPE] = (ORIGIN_MIME_TYPE, 8, self._clipboard_origin.encode())
        if not has_contents:
            # No advertised targets cannot establish a new native owner.
            # Only retire our own still-active offer; an independent local
            # source and its pending announcement remain authoritative.
            owned = self.remote_source is not None and self.local_source_ptr == self.remote_source_ptr
            try:
                self.clear_remote_source()
            except Exception:
                # clear_remote_source destroys the wrapper in finally, so this
                # is a completed retirement, not a failed replacement to undo.
                log.error("Error clearing the Wayland clipboard source", exc_info=True)
            if not owned:
                return False
            self.targets = ()
            self.target_data = {}
            self._have_token = False
            self.cancel_emit_token()
            return True

        # Finish fallible MIME construction before changing the installed
        # source metadata. Native set_source can refuse only before its setter;
        # after the setter its flush is noexcept.
        source_targets = tuple(dict.fromkeys(new_targets + tuple(new_data)))
        source = self.SOURCE_CLASS(self, source_targets, new_data)
        previous = (
            self.remote_source, self.remote_source_ptr, self.remote_generation,
            self.targets, self.target_data, self._have_token,
        )
        self.remote_generation += 1
        self.remote_source = source
        self.remote_source_ptr = source.ptr()
        self.targets = new_targets
        self.target_data = new_data
        try:
            # Publish identity first: wlroots synchronously destroys the old
            # source and emits the selection signal while installing this one.
            self.selection_api.set_source(source)
        except Exception:
            (self.remote_source, self.remote_source_ptr, self.remote_generation,
             self.targets, self.target_data, self._have_token) = previous
            source.destroy()
            raise
        self._have_token = True
        self.cancel_emit_token()
        return True

    def send_remote_contents(self, source, target: str, fd: int, target_data=None) -> None:
        if self.closing or not self._enabled or not self._can_receive or source is not self.remote_source:
            try:
                os.close(fd)
            except OSError:
                pass
            return
        write_key = self.pending_write_counter
        self.pending_write_counter += 1
        generation = self.remote_generation
        self.pending_writes[write_key] = (source, generation, target, fd)
        if target_data and target in target_data:
            _dtype, _dformat, data = target_data[target]
            self.write_fd(write_key, data)
            return

        def got_remote_contents(_dtype="", _dformat=0, data=b"") -> None:
            pending = self.pending_writes.get(write_key)
            if pending is None:
                return
            pending_source, pending_generation, _pending_target, _pending_fd = pending
            if (
                self.closing
                or pending_source is not self.remote_source
                or pending_generation != self.remote_generation
            ):
                self.finish_pending_write(write_key)
                return
            self.write_fd(write_key, data)

        try:
            self.emit("send-clipboard-request", self._selection, target, got_remote_contents)
        except Exception:
            self.finish_pending_write(write_key)
            raise

    def write_fd(self, write_key: int, data) -> None:
        pending = self.pending_writes.get(write_key)
        if pending is None:
            return
        fd = pending[3]
        try:
            if data is None:
                data = b""
            elif isinstance(data, str):
                data = data.encode("utf8")
            elif not isinstance(data, bytes):
                # Byte offsets must not become element offsets for a typed
                # or strided view.  Own a stable snapshot while output waits.
                # Native buffers need not have a scalar truth value or length.
                data = bytes(memoryview(data))
            data = memoryview(data)
            os.set_blocking(fd, False)
        except Exception:
            self.finish_pending_write(write_key)
            raise
        offset = 0

        def write_ready(_fd, condition):
            nonlocal offset
            current = self.pending_writes.get(write_key)
            if current is None:
                return False
            source, generation, _target, current_fd = current
            if (
                self.closing
                or source is not self.remote_source
                or generation != self.remote_generation
                or condition & (GLib.IO_HUP | GLib.IO_ERR | GLib.IO_NVAL)
            ):
                self.finish_pending_write(write_key)
                return False
            try:
                # One bounded nonblocking write per dispatch keeps the main
                # loop responsive even when a native consumer stops reading.
                if offset < len(data):
                    written = os.write(current_fd, data[offset:offset + 65536])
                    if written <= 0:
                        self.finish_pending_write(write_key)
                        return False
                    offset += written
            except (BlockingIOError, InterruptedError):
                return True
            except OSError:
                self.finish_pending_write(write_key)
                return False
            if offset == len(data):
                self.finish_pending_write(write_key)
                return False
            return True

        def write_timeout():
            log.warn("Warning: native Wayland clipboard consumer timed out")
            self.finish_pending_write(write_key)
            return False

        if not write_ready(fd, GLib.IO_OUT):
            return
        try:
            watch = GLib.io_add_watch(
                fd, GLib.IO_OUT | GLib.IO_HUP | GLib.IO_ERR | GLib.IO_NVAL, write_ready,
            )
            self.pending_write_sources[write_key] = (watch,)
            timer = GLib.timeout_add(REMOTE_TIMEOUT, write_timeout)
            self.pending_write_sources[write_key] = (watch, timer)
        except Exception:
            self.finish_pending_write(write_key)
            raise


class WaylandClipboardProxy(WaylandPrimaryClipboardProxy):

    SELECTION_SIGNAL = "selection"
    SELECTION_API = WaylandSelection
    SOURCE_CLASS = WaylandSelectionSource

    def __repr__(self):
        return "WaylandClipboardProxy(%s)" % self._selection


GObject.type_register(WaylandPrimaryClipboardProxy)
GObject.type_register(WaylandClipboardProxy)


class WaylandClipboard(ClipboardTimeoutHelper):

    def __init__(self, *args, compositor=None, **kwargs):
        self.compositor = compositor
        kwargs["clipboards.local"] = WAYLAND_CLIPBOARDS
        kwargs["clipboards.remote"] = WAYLAND_CLIPBOARDS
        try:
            super().__init__(*args, **kwargs)
        except Exception:
            for proxy in tuple(getattr(self, "_clipboard_proxies", {}).values()):
                try:
                    proxy.cleanup()
                except Exception:
                    log("failed to clean up a partial Wayland clipboard proxy", exc_info=True)
            raise
        self.local_selections = WAYLAND_CLIPBOARDS
        self.remote_clipboards = WAYLAND_CLIPBOARDS
        self.local_want_targets = WAYLAND_CLIPBOARDS
        self.local_greedy = WAYLAND_CLIPBOARDS

    def __repr__(self):
        return "WaylandClipboard"

    def client_reset(self) -> None:
        super().client_reset()
        for proxy in self._clipboard_proxies.values():
            proxy.client_reset()

    def set_direction(self, can_send: bool, can_receive: bool,
                      max_send_size: int | None = None, max_receive_size: int | None = None) -> None:
        receive_revoked = self.can_receive and not can_receive
        super().set_direction(can_send, can_receive, max_send_size, max_receive_size)
        if receive_revoked:
            # Native proxies have already retired the revoked consumers.
            # Release their wire timers without resetting the allowed direction.
            self.cancel_outstanding_requests()

    def native_read_limit(self) -> int:
        # Explicit send-size policy truncates before the packet-size check and
        # records the total truncated count.  Preserve that established path.
        if self.max_clipboard_send_size > 0:
            return -1
        return self.max_clipboard_packet_size

    def make_proxy(self, selection):
        if selection == "CLIPBOARD" and self.compositor is not None:
            proxy = WaylandClipboardProxy(selection, self.compositor)
        elif selection == "PRIMARY" and self.compositor is not None:
            proxy = WaylandPrimaryClipboardProxy(selection, self.compositor)
        else:
            raise RuntimeError(f"unsupported Wayland clipboard selection: {selection!r}")
        # Keep constructor rollback ownership before any fallible setup.
        self._clipboard_proxies[selection] = proxy
        proxy.set_want_targets(self.proxy_want_targets(selection))
        proxy.read_limit = self.native_read_limit
        proxy.set_direction(self.can_send, self.can_receive)
        proxy.connect("send-clipboard-token", self._send_clipboard_token_handler)
        proxy.connect("send-clipboard-request", self._send_clipboard_request_handler)
        return proxy
