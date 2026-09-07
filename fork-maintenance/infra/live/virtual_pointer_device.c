/* Copyright (C) 2026 kogeler
 * Test-only persistent virtual wheel for the headless Sway seat.
 * Accept six bounded local commands, never Xpra packets or application values.
 */
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <poll.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>
#include <wayland-client.h>
#include "wlr-virtual-pointer-client-protocol.h"

static struct wl_seat *seat;
static struct zwlr_virtual_pointer_manager_v1 *manager;
static bool has_pointer;
static unsigned seat_count;

static bool inject(struct wl_display *display, struct zwlr_virtual_pointer_v1 *pointer,
                   int control, unsigned *sequence) {
    char command[4];
    ssize_t count = recv(control, command, sizeof(command), 0);
    if (count < 0 && (errno == EAGAIN || errno == EINTR)) {
        return true;
    }
    if (count != 3 || *sequence >= 6 || command[0] != (char)('1' + *sequence)
            || command[1] != ' ' || command[2] < '4' || command[2] > '7') {
        fputs("invalid or out-of-order wheel command\n", stderr);
        return false;
    }
    unsigned button = (unsigned)(command[2] - '0');
    uint32_t axis = button <= 5 ? WL_POINTER_AXIS_VERTICAL_SCROLL : WL_POINTER_AXIS_HORIZONTAL_SCROLL;
    int direction = button == 4 || button == 6 ? -1 : 1;
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) {
        return false;
    }
    uint32_t time = (uint32_t)((uint64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000);
    zwlr_virtual_pointer_v1_axis_source(pointer, WL_POINTER_AXIS_SOURCE_WHEEL);
    zwlr_virtual_pointer_v1_axis_discrete(pointer, time, axis, wl_fixed_from_int(direction * 15), direction);
    zwlr_virtual_pointer_v1_frame(pointer);
    if (wl_display_roundtrip(display) < 0) {
        return false;
    }
    (*sequence)++;
    printf("wheel %u %u\n", *sequence, button);
    fflush(stdout);
    return true;
}

static void capabilities(void *data, struct wl_seat *object, uint32_t value) {
    (void)data;
    (void)object;
    has_pointer = (value & WL_SEAT_CAPABILITY_POINTER) != 0;
}

static const struct wl_seat_listener seat_listener = {
    .capabilities = capabilities,
};

static void global(void *data, struct wl_registry *registry, uint32_t name,
                   const char *interface, uint32_t version) {
    (void)data;
    (void)version;
    if (strcmp(interface, wl_seat_interface.name) == 0) {
        seat_count++;
        if (!seat) {
            seat = wl_registry_bind(registry, name, &wl_seat_interface, 1);
            wl_seat_add_listener(seat, &seat_listener, NULL);
        }
    } else if (strcmp(interface, zwlr_virtual_pointer_manager_v1_interface.name) == 0) {
        manager = wl_registry_bind(registry, name, &zwlr_virtual_pointer_manager_v1_interface, 1);
    }
}

static void global_remove(void *data, struct wl_registry *registry, uint32_t name) {
    (void)data;
    (void)registry;
    (void)name;
}

static const struct wl_registry_listener registry_listener = {
    .global = global,
    .global_remove = global_remove,
};

int main(void) {
    struct wl_display *display = wl_display_connect(NULL);
    if (!display) {
        fputs("cannot connect to the fixture compositor\n", stderr);
        return 1;
    }
    struct wl_registry *registry = wl_display_get_registry(display);
    wl_registry_add_listener(registry, &registry_listener, NULL);
    if (wl_display_roundtrip(display) < 0 || seat_count != 1 || !seat || !manager) {
        fputs("one seat and the virtual-pointer protocol are required\n", stderr);
        wl_display_disconnect(display);
        return 1;
    }
    struct zwlr_virtual_pointer_v1 *pointer =
        zwlr_virtual_pointer_manager_v1_create_virtual_pointer(manager, seat);
    if (wl_display_roundtrip(display) < 0 || !has_pointer) {
        fputs("compositor did not advertise the pointer capability\n", stderr);
        wl_display_disconnect(display);
        return 1;
    }
    int control = socket(AF_UNIX, SOCK_DGRAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    strcpy(address.sun_path, "/tmp/xpra-virtual-pointer-control");
    umask(077);
    if (control < 0 || bind(control, (struct sockaddr *)&address, sizeof(address)) != 0) {
        fputs("cannot bind the private wheel control socket\n", stderr);
        if (control >= 0) {
            close(control);
        }
        wl_display_disconnect(display);
        return 1;
    }
    puts("pointer-capability-ready");
    fflush(stdout);
    /* Keep the device plugged in until the owned compositor/container exits.
     * Creating one device per stimulus would race capability loss and regain.
     */
    unsigned sequence = 0;
    while (has_pointer) {
        while (wl_display_prepare_read(display) != 0) {
            if (wl_display_dispatch_pending(display) < 0) {
                goto finished;
            }
        }
        if (wl_display_flush(display) < 0) {
            wl_display_cancel_read(display);
            break;
        }
        struct pollfd fds[] = {{wl_display_get_fd(display), POLLIN, 0}, {control, POLLIN, 0}};
        int ready = poll(fds, 2, -1);
        if (ready < 0) {
            wl_display_cancel_read(display);
            if (errno == EINTR) {
                continue;
            }
            break;
        }
        if (fds[0].revents & POLLIN) {
            if (wl_display_read_events(display) < 0) {
                break;
            }
        } else {
            wl_display_cancel_read(display);
        }
        if (wl_display_dispatch_pending(display) < 0
                || ((fds[0].revents | fds[1].revents) & (POLLERR | POLLHUP | POLLNVAL))) {
            break;
        }
        if ((fds[1].revents & POLLIN) && !inject(display, pointer, control, &sequence)) {
            break;
        }
    }
finished:
    close(control);
    unlink(address.sun_path);
    zwlr_virtual_pointer_v1_destroy(pointer);
    zwlr_virtual_pointer_manager_v1_destroy(manager);
    wl_seat_destroy(seat);
    wl_registry_destroy(registry);
    wl_display_disconnect(display);
    return 1;
}
