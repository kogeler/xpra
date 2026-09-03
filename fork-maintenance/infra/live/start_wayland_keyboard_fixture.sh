#!/bin/bash
# Copyright (C) 2026 kogeler

set -uo pipefail

unset DISPLAY
export GDK_BACKEND=wayland

python3 /opt/xpra-fork-maintenance/wayland_keyboard_fixture.py \
    >/artifacts/keyboard-fixture.stdout \
    2>/artifacts/keyboard-fixture.stderr &
child=$!
printf '%s\n' "$child" >/artifacts/keyboard-fixture.pid

status=0
wait "$child" || status=$?
printf '%s\n' "$status" >/artifacts/keyboard-fixture.exit
exit "$status"
