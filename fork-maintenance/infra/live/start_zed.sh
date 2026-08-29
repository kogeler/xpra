#!/bin/sh
set -eu

install -d -m 0700 /tmp/zed-home /tmp/zed-data /tmp/zed-cache /tmp/zed-config
printf '%s\n' "$$" > /artifacts/zed.pid

exec env \
    HOME=/tmp/zed-home \
    XDG_CACHE_HOME=/tmp/zed-cache \
    XDG_CONFIG_HOME=/tmp/zed-config \
    WAYLAND_DEBUG=1 \
    /home/lab/live-input/zed.app/libexec/zed-editor \
        --user-data-dir=/tmp/zed-data \
        > /artifacts/zed.stdout \
        2> /artifacts/zed.stderr
