#!/bin/sh
# Copyright (C) 2026 kogeler

set -eu

umask 077
rm -f \
    /tmp/xpra-subsurface-ready \
    /tmp/xpra-subsurface-update-two \
    /tmp/xpra-subsurface-restore-one \
    /tmp/xpra-subsurface-move-lower \
    /tmp/xpra-subsurface-create-upper \
    /tmp/xpra-subsurface-update-lower-under-upper \
    /tmp/xpra-subsurface-frame-generation-one \
    /tmp/xpra-subsurface-frame-generation-two \
    /tmp/xpra-subsurface-continuous-start \
    /tmp/xpra-subsurface-continuous-stop \
    /tmp/xpra-subsurface-upper-clicked \
    /tmp/xpra-subsurface-destroy-lower \
    /tmp/xpra-subsurface-detach-upper \
    /tmp/xpra-subsurface-reparent-upper \
    /tmp/xpra-subsurface-exit

/usr/local/bin/xpra-subsurface-fixture \
    > /artifacts/subsurface-fixture.stdout \
    2> /artifacts/subsurface-fixture.stderr &
child=$!
printf '%s\n' "$child" > /artifacts/subsurface-fixture.pid
status=0
wait "$child" || status=$?
printf '%s\n' "$status" > /artifacts/subsurface-fixture.exit
exit "$status"
