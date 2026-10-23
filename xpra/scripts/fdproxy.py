# This file is part of Xpra.
# Copyright (C) 2012 Antoine Martin <antoine@xpra.org>
# Copyright (C) 2010 Nathaniel Smith <njs@pobox.com>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import signal
import threading

from xpra.exit_codes import ExitValue
from xpra.net.bytestreams import untilConcludes
from xpra.net.common import ConnectionClosedException
from xpra.util.str_fn import repr_ellipsized, hexstr
from xpra.util.env import envint, envbool
from xpra.os_util import force_quit, POSIX
from xpra.log import Logger

log = Logger("proxy")

SHOW_DATA = envbool("XPRA_PROXY_SHOW_DATA")
PROXY_BUFFER_SIZE = envint("XPRA_PROXY_BUFFER_SIZE", 65536)


def noretry(_e) -> bool:
    return False


class XpraProxy:
    """
        This is the proxy command that runs
        when one uses the hidden subcommand
        "xpra _proxy".
        It simply forwards stdin/stdout to
        the server socket.
    """

    def __repr__(self):
        return f"XpraProxy({self._name}: {self._client_conn} - {self._server_conn})"

    def __init__(self, name, client_conn, server_conn):
        self._name = name
        self._client_conn = client_conn
        self._server_conn = server_conn
        self._to_client = threading.Thread(target=self._to_client_loop, daemon=True)
        self._to_server = threading.Thread(target=self._to_server_loop, daemon=True)
        self.exit_code: ExitValue | None = None
        # set when the server has closed its end while we were still forwarding to it:
        self.draining = False
        signal.signal(signal.SIGINT, self.signal_quit)
        signal.signal(signal.SIGTERM, self.signal_quit)
        if POSIX:
            signal.signal(signal.SIGHUP, self.signal_quit)
            signal.signal(signal.SIGPIPE, self.signal_quit)

    def start_threads(self) -> None:
        self._to_client.start()
        self._to_server.start()

    def run(self) -> ExitValue:
        log("XpraProxy.run() %s", self._name)
        self.start_threads()
        self._to_client.join()
        self._to_server.join()
        log("XpraProxy.run() %s: all the threads have ended, calling quit() to close the connections", self._name)
        self.quit(0)
        return self.exit_code

    def _to_client_loop(self) -> None:
        self._copy_loop(f"<-server {self._name}", self._server_conn, self._client_conn)

    def _to_server_loop(self) -> None:
        # the server closes its socket as soon as its last packet has been sent (ie: a disconnection),
        # so forwarding what the client sends after that fails,
        # but the other direction must still deliver that last packet before it ends the proxy:
        if POSIX:
            # writing to the closed server socket must not end the proxy from the SIGPIPE handler,
            # this thread gets the error instead (the other direction still gets SIGPIPE from the client pipe):
            signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGPIPE})
        self._copy_loop(f"->server {self._name}", self._client_conn, self._server_conn, drain=True)

    def _copy_loop(self, log_name: str, from_conn, to_conn, drain=False) -> None:
        # log("XpraProxy._copy_loop(%s, %s, %s)", log_name, from_conn, to_conn)
        writing = False
        try:
            while self.exit_code is None:
                log("%s: waiting for data", log_name)
                buf = untilConcludes(self.is_active, noretry, from_conn.read, PROXY_BUFFER_SIZE)
                if not buf:
                    log("%s: connection lost", log_name)
                    return
                if SHOW_DATA:
                    log("%s: %s bytes: %s", log_name, len(buf), repr_ellipsized(buf))
                    log("%s:           %s", log_name, repr_ellipsized(hexstr(buf)))
                writing = True
                while buf and self.exit_code is None:
                    log("%s: writing %s bytes", log_name, len(buf))
                    written = untilConcludes(self.is_active, noretry, to_conn.write, buf)
                    buf = buf[written:]
                    log("%s: written %s bytes", log_name, written)
                writing = False
            log("%s copy loop ended", log_name)
        except (OSError, ConnectionClosedException) as e:
            if drain and writing and isinstance(e, ConnectionClosedException):
                # the peer has closed its end, the other direction will see the end of its stream:
                self.draining = True
                log("%s: %s", log_name, e)
            # an error during shutdown is expected (the pipes get closed), so only make noise
            # if we were still active - and log a single line rather than flooding with tracebacks
            # (this goes to stderr, which is a separate pipe from the protocol stream on stdout):
            elif self.exit_code is None and not self.draining:
                log.warn("Warning: %s connection failed: %s", log_name, e)
            log("%s copy loop error", log_name, exc_info=True)
        finally:
            # make sure the other direction is torn down too, whichever way this loop ended,
            # unless it still has to deliver what the server sent before closing:
            if not (drain and self.draining):
                self.quit(0)

    def is_active(self) -> bool:
        return self.exit_code is None

    def signal_quit(self, signum, _frame=None) -> None:
        self.quit(128 + signum)

    def quit(self, exit_code: ExitValue) -> None:
        log("XpraProxy.quit(%s) %s: closing connections", exit_code, self._name)
        if self.exit_code is None:
            self.exit_code = exit_code
        try:
            self._client_conn.close()
        except OSError:
            pass
        try:
            self._server_conn.close()
        except OSError:
            pass
        log("quit exit-code=%s", self.exit_code)
        force_quit(self.exit_code)
