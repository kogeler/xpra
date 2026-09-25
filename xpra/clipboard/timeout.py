# This file is part of Xpra.
# Copyright (C) 2019 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

from collections.abc import Callable

from xpra.clipboard.common import ClipboardCallback, env_timeout
from xpra.clipboard.core import ClipboardProtocolHelperCore
from xpra.clipboard.proxy import ClipboardProxyCore
from xpra.common import noop
from xpra.log import Logger
from xpra.os_util import gi_import
from xpra.util.str_fn import Ellipsizer

GLib = gi_import("GLib")

log = Logger("clipboard")

REMOTE_TIMEOUT = env_timeout("REMOTE", 2500)


class ClipboardTimeoutHelper(ClipboardProtocolHelperCore):
    # a clipboard superclass that handles timeouts

    def __init__(self, send_packet_cb: Callable, progress_cb=noop, **kwargs):
        self._clipboard_draining_requests = False
        self._clipboard_closed = False
        super().__init__(send_packet_cb, progress_cb, **kwargs)
        self._clipboard_outstanding_requests: dict[
            int, tuple[int, str, str, ClipboardCallback | None]
        ] = {}

    def cleanup(self) -> None:
        self._clipboard_closed = True
        self.send = noop
        try:
            self.cancel_outstanding_requests()
        finally:
            super().cleanup()

    ############################################################################
    # network methods for communicating with the remote clipboard:
    ############################################################################

    def _send_clipboard_request_handler(self, proxy: ClipboardProxyCore, selection: str, target: str,
                                        got_contents: ClipboardCallback | None = None) -> None:
        log("send_clipboard_request_handler%s", (proxy, selection, target))
        if self._clipboard_draining_requests or self._clipboard_closed:
            self._deliver_clipboard_contents(proxy, target, got_contents)
            return
        request_id = self._clipboard_request_counter
        self._clipboard_request_counter += 1
        remote = self.local_to_remote(selection)
        log("send_clipboard_request %s to %s, id=%s", selection, remote, request_id)
        try:
            timer = GLib.timeout_add(REMOTE_TIMEOUT, self.timeout_request, request_id)
        except Exception:
            self._deliver_clipboard_contents(proxy, target, got_contents)
            raise
        self._clipboard_outstanding_requests[request_id] = (timer, selection, target, got_contents)
        try:
            self.progress()
            self.send("clipboard-request", request_id, remote, target)
        except Exception:
            if request_id in self._clipboard_outstanding_requests:
                self._clipboard_got_contents(request_id)
            raise

    def progress(self) -> None:
        try:
            super().progress()
        except Exception:
            # Advisory progress must not interrupt result/timer ownership.
            log.error("Error reporting clipboard progress", exc_info=True)

    def timeout_request(self, request_id: int) -> None:
        try:
            _timer, selection, target, got_contents = self._clipboard_outstanding_requests.pop(request_id)
        except KeyError:
            log.warn("Warning: clipboard request id %i not found", request_id)
            return
        finally:
            self.progress()
        log.warn("Warning: remote clipboard request timed out")
        log.warn(" request id %i, selection=%s, target=%s", request_id, selection, target)
        self._deliver_clipboard_contents(self._get_proxy(selection), target, got_contents)

    def _clipboard_got_contents(self, request_id: int, dtype: str = "", dformat: int = 0, data=None) -> None:
        try:
            timer, selection, target, got_contents = self._clipboard_outstanding_requests.pop(request_id)
        except KeyError:
            log.warn("Warning: request id %i not found", request_id)
            log.warn(" already timed out or duplicate reply")
            return
        finally:
            self.progress()
        try:
            GLib.source_remove(timer)
        except Exception:
            # The request was already retired; an escaped callback cannot
            # complete it again. Continue delivering its terminal result.
            log("failed to remove clipboard request timer", exc_info=True)
        proxy = self._get_proxy(selection)
        log("clipboard got contents%s: proxy=%s for selection=%s",
            (request_id, dtype, dformat, Ellipsizer(data)), proxy, selection)
        if isinstance(data, memoryview):
            data = bytes(data)
        self._deliver_clipboard_contents(proxy, target, got_contents, dtype, dformat, data)

    @staticmethod
    def _deliver_clipboard_contents(proxy: ClipboardProxyCore | None, target: str,
                                    got_contents: ClipboardCallback | None,
                                    dtype: str = "", dformat: int = 0, data=None) -> None:
        try:
            if got_contents is not None:
                got_contents(dtype, dformat, data)
            elif proxy:
                proxy.got_contents(target, dtype, dformat, data)
        except Exception:
            log.error("Error delivering clipboard request result", exc_info=True)

    def cancel_outstanding_requests(self) -> None:
        if self._clipboard_draining_requests:
            return
        self._clipboard_draining_requests = True
        try:
            for request_id in tuple(self._clipboard_outstanding_requests):
                if request_id in self._clipboard_outstanding_requests:
                    self._clipboard_got_contents(request_id)
        finally:
            self._clipboard_draining_requests = False

    def client_reset(self) -> None:
        if cor := self._clipboard_outstanding_requests:
            log.info("cancelling %i clipboard requests", len(cor))
            self.cancel_outstanding_requests()
        super().client_reset()
