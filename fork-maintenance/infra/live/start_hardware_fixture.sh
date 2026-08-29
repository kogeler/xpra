#!/bin/bash
set -euo pipefail

vulkan_pid=
interaction_pid=
vulkan_status=
interaction_status=

wait_for_child() {
    local pid=$1
    local status_variable=$2
    local child_status
    set +e
    wait "$pid"
    child_status=$?
    set -e
    printf -v "$status_variable" '%s' "$child_status"
}

record_statuses() {
    if [[ -n "$interaction_status" ]]; then
        printf '%s\n' "$interaction_status" > /artifacts/interaction.exit
    fi
    if [[ -n "$vulkan_status" ]]; then
        printf '%s\n' "$vulkan_status" > /artifacts/vkcube.exit
    fi
}

cleanup() {
    trap - EXIT HUP INT TERM
    if [[ -n "$interaction_pid" && -z "$interaction_status" ]]; then
        kill "$interaction_pid" 2>/dev/null || true
        wait_for_child "$interaction_pid" interaction_status
    fi
    if [[ -n "$vulkan_pid" && -z "$vulkan_status" ]]; then
        kill "$vulkan_pid" 2>/dev/null || true
        wait_for_child "$vulkan_pid" vulkan_status
    fi
    record_statuses
}

trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
rm -f -- \
    /tmp/xpra-hardware-interaction-ready \
    /tmp/xpra-hardware-pointer-clicked \
    /tmp/xpra-hardware-keyboard-escape

vkcube --wsi wayland --width 640 --height 480 --suppress_popups \
    > /artifacts/vkcube.stdout 2> /artifacts/vkcube.stderr &
vulkan_pid=$!
printf '%s\n' "$vulkan_pid" > /artifacts/vkcube.pid

env -u DISPLAY GDK_BACKEND=wayland \
    python3 /opt/xpra-lab/interaction_fixture.py \
    > /artifacts/interaction.stdout 2> /artifacts/interaction.stderr &
interaction_pid=$!
printf '%s\n' "$interaction_pid" > /artifacts/interaction.pid

wait_for_child "$interaction_pid" interaction_status
wait_for_child "$vulkan_pid" vulkan_status
record_statuses
if [[ "$interaction_status" -ne 0 ]]; then
    exit "$interaction_status"
fi
exit "$vulkan_status"
