#!/bin/bash
set -euo pipefail

vulkan_pid=
interaction_pid=

cleanup() {
    trap - EXIT HUP INT TERM
    for pid in "$interaction_pid" "$vulkan_pid"; do
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
            wait "$pid" 2>/dev/null || true
        fi
    done
}

trap cleanup EXIT
trap 'exit 0' HUP INT TERM
rm -f -- \
    /tmp/xpra-hardware-pointer-clicked \
    /tmp/xpra-hardware-keyboard-escape

vkcube --wsi wayland --width 640 --height 480 --suppress_popups \
    > /artifacts/vkcube.stdout 2> /artifacts/vkcube.stderr &
vulkan_pid=$!
printf '%s\n' "$vulkan_pid" > /artifacts/vkcube.pid

python3 /opt/xpra-lab/interaction_fixture.py \
    > /artifacts/interaction.stdout 2> /artifacts/interaction.stderr &
interaction_pid=$!
printf '%s\n' "$interaction_pid" > /artifacts/interaction.pid

wait "$interaction_pid"
interaction_status=$?
wait "$vulkan_pid"
vulkan_status=$?
test "$interaction_status" -eq 0
test "$vulkan_status" -eq 0
