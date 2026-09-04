#!/bin/bash
# Copyright (C) 2026 kogeler

set -uo pipefail

unset DISPLAY
export GDK_BACKEND=wayland

python3 /opt/xpra-fork-maintenance/wayland_clipboard_fixture.py \
    --command-file=/tmp/xpra-wayland-clipboard-command \
    >/artifacts/clipboard-fixture.stdout \
    2>/artifacts/clipboard-fixture.stderr &
child=$!
printf '%s\n' "$child" >/artifacts/clipboard-fixture.pid

status=0
wait "$child" || status=$?
printf '%s\n' "$status" >/artifacts/clipboard-fixture.exit
exit "$status"
