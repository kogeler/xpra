#!/bin/sh
set -eu

install -d -m 0700 /tmp/zed-home /tmp/zed-data /tmp/zed-cache /tmp/zed-config
printf '%s\n' "$$" > /artifacts/zed.pid

# Zed redraws only while its startup work (fonts, theme, welcome page) lands,
# then idles. Without a client the server answers its frame callbacks only after
# XPRA_WAYLAND_FRAME_TIMEOUT (1 s), so that startup could finish unseen and the
# client would only receive the initial and resize refreshes, which the server
# always sends as pictures. Start Zed once the client is attached.
# (`--start-child-after-connect` is not used: this server records it twice and
# starts two instances.)
waited=0
until xpra info wayland-0 --socket-dir=/tmp/server-runtime/xpra-sockets 2>/dev/null \
        | grep -q '^client\.0\.connection\.active=True$'; do
    waited=$((waited + 1))
    if [ "$waited" -gt 240 ]; then
        echo "no Xpra client attached within 120 seconds" >&2
        exit 1
    fi
    sleep 0.5
done

exec env \
    HOME=/tmp/zed-home \
    XDG_CACHE_HOME=/tmp/zed-cache \
    XDG_CONFIG_HOME=/tmp/zed-config \
    WAYLAND_DEBUG=1 \
    /home/lab/live-input/zed.app/libexec/zed-editor \
        --user-data-dir=/tmp/zed-data \
        > /artifacts/zed.stdout \
        2> /artifacts/zed.stderr
