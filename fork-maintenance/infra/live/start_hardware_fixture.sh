#!/bin/bash
set -euo pipefail

primary_mode=${1:-vulkan}
case "$primary_mode" in
    vulkan) primary_name=vkcube ;;
    opengl) primary_name=opengl ;;
    *)
        printf 'unsupported hardware fixture mode: %s\n' "$primary_mode" >&2
        exit 2
        ;;
esac

primary_pid=
interaction_pid=
primary_status=
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
    if [[ -n "$primary_status" ]]; then
        printf '%s\n' "$primary_status" > "/artifacts/$primary_name.exit"
    fi
}

cleanup() {
    trap - EXIT HUP INT TERM
    if [[ -n "$interaction_pid" && -z "$interaction_status" ]]; then
        kill "$interaction_pid" 2>/dev/null || true
        wait_for_child "$interaction_pid" interaction_status
    fi
    if [[ -n "$primary_pid" && -z "$primary_status" ]]; then
        kill "$primary_pid" 2>/dev/null || true
        wait_for_child "$primary_pid" primary_status
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

if [[ "$primary_mode" == vulkan ]]; then
    vkcube --wsi wayland --width 640 --height 480 --suppress_popups \
        > /artifacts/vkcube.stdout 2> /artifacts/vkcube.stderr &
else
    env -u DISPLAY \
        glmark2-wayland --run-forever --size 640x480 --swap-mode fifo \
        --visual-config red=8:green=8:blue=8:alpha=0:buffer=24 \
        --benchmark jellyfish \
        > /artifacts/opengl.stdout 2> /artifacts/opengl.stderr &
fi
primary_pid=$!
printf '%s\n' "$primary_pid" > "/artifacts/$primary_name.pid"

env -u DISPLAY GDK_BACKEND=wayland \
    python3 /opt/xpra-fork-maintenance/interaction_fixture.py \
    > /artifacts/interaction.stdout 2> /artifacts/interaction.stderr &
interaction_pid=$!
printf '%s\n' "$interaction_pid" > /artifacts/interaction.pid

wait_for_child "$interaction_pid" interaction_status
wait_for_child "$primary_pid" primary_status
record_statuses
if [[ "$interaction_status" -ne 0 ]]; then
    exit "$interaction_status"
fi
exit "$primary_status"
