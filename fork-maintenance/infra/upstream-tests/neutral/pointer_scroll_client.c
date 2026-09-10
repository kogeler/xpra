/* This file is part of Xpra.
 * Copyright (C) 2026 kogeler
 * Xpra is released under the terms of the GNU GPL v2, or, at your option, any
 * later version. See the file COPYING for details.
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>
#include <wayland-client.h>
#include "xdg-shell-client-protocol.h"

static struct wl_compositor *compositor;
static struct wl_shm *shm;
static struct wl_seat *seat;
static struct wl_pointer *pointer;
static struct xdg_wm_base *shell;
static unsigned pointer_version;

static void enter(void *data, struct wl_pointer *p, uint32_t serial,
                  struct wl_surface *surface, wl_fixed_t x, wl_fixed_t y) {
    (void)data; (void)p; (void)serial; (void)surface; (void)x; (void)y;
    puts("[\"enter\"]");
}
static void leave(void *data, struct wl_pointer *p, uint32_t serial, struct wl_surface *surface) {
    (void)data; (void)p; (void)serial; (void)surface;
    puts("[\"leave\"]");
}
static void motion(void *data, struct wl_pointer *p, uint32_t time, wl_fixed_t x, wl_fixed_t y) {
    (void)data; (void)p; (void)time; (void)x; (void)y;
    puts("[\"motion\"]");
}
static void button(void *data, struct wl_pointer *p, uint32_t serial,
                   uint32_t time, uint32_t code, uint32_t state) {
    (void)data; (void)p; (void)serial; (void)time;
    printf("[\"button\",%u,%u]\n", code, state);
}
static void axis(void *data, struct wl_pointer *p, uint32_t time, uint32_t orientation, wl_fixed_t value) {
    (void)data; (void)p; (void)time;
    printf("[\"axis\",%u,%.8f]\n", orientation, wl_fixed_to_double(value));
}
static void frame(void *data, struct wl_pointer *p) {
    (void)data; (void)p;
    puts("[\"frame\"]");
}
static void axis_source(void *data, struct wl_pointer *p, uint32_t source) {
    (void)data; (void)p;
    printf("[\"source\",%u]\n", source);
}
static void axis_stop(void *data, struct wl_pointer *p, uint32_t time, uint32_t orientation) {
    (void)data; (void)p; (void)time;
    printf("[\"stop\",%u]\n", orientation);
}
static void axis_discrete(void *data, struct wl_pointer *p, uint32_t orientation, int32_t value) {
    (void)data; (void)p;
    printf("[\"discrete\",%u,%d]\n", orientation, value);
}
static void axis_value120(void *data, struct wl_pointer *p, uint32_t orientation, int32_t value) {
    (void)data; (void)p;
    printf("[\"value120\",%u,%d]\n", orientation, value);
}
static const struct wl_pointer_listener pointer_listener = {
    .enter = enter, .leave = leave, .motion = motion, .button = button,
    .axis = axis, .frame = frame, .axis_source = axis_source,
    .axis_stop = axis_stop, .axis_discrete = axis_discrete, .axis_value120 = axis_value120,
};
static void capabilities(void *data, struct wl_seat *s, uint32_t caps) {
    (void)data;
    if ((caps & WL_SEAT_CAPABILITY_POINTER) && !pointer) {
        pointer = wl_seat_get_pointer(s);
        wl_pointer_add_listener(pointer, &pointer_listener, NULL);
    }
}
static void seat_name(void *data, struct wl_seat *s, const char *name) {
    (void)data; (void)s; (void)name;
}
static const struct wl_seat_listener seat_listener = {.capabilities = capabilities, .name = seat_name};
static void ping(void *data, struct xdg_wm_base *base, uint32_t serial) {
    (void)data;
    xdg_wm_base_pong(base, serial);
}
static const struct xdg_wm_base_listener shell_listener = {.ping = ping};
static void global(void *data, struct wl_registry *registry, uint32_t name,
                   const char *interface, uint32_t version) {
    (void)data;
    if (strcmp(interface, "wl_compositor") == 0) {
        compositor = wl_registry_bind(registry, name, &wl_compositor_interface, 4);
    } else if (strcmp(interface, "wl_shm") == 0) {
        shm = wl_registry_bind(registry, name, &wl_shm_interface, 1);
    } else if (strcmp(interface, "wl_seat") == 0 && version >= pointer_version) {
        seat = wl_registry_bind(registry, name, &wl_seat_interface, pointer_version);
        wl_seat_add_listener(seat, &seat_listener, NULL);
    } else if (strcmp(interface, "xdg_wm_base") == 0) {
        shell = wl_registry_bind(registry, name, &xdg_wm_base_interface, 1);
        xdg_wm_base_add_listener(shell, &shell_listener, NULL);
    }
}
static void global_remove(void *data, struct wl_registry *registry, uint32_t name) {
    (void)data; (void)registry; (void)name;
}
static const struct wl_registry_listener registry_listener = {.global = global, .global_remove = global_remove};
static void configure(void *data, struct xdg_surface *surface, uint32_t serial) {
    (void)data;
    xdg_surface_ack_configure(surface, serial);
}
static const struct xdg_surface_listener surface_listener = {.configure = configure};
static void toplevel_configure(void *data, struct xdg_toplevel *top,
                               int32_t width, int32_t height, struct wl_array *states) {
    (void)data; (void)top; (void)width; (void)height; (void)states;
}
static void toplevel_close(void *data, struct xdg_toplevel *top) {
    (void)data; (void)top;
    abort();
}
static const struct xdg_toplevel_listener toplevel_listener = {
    .configure = toplevel_configure, .close = toplevel_close,
};
static void roundtrip(struct wl_display *display) {
    if (wl_display_roundtrip(display) < 0) {
        fputs("Wayland pointer roundtrip failed\n", stderr);
        exit(1);
    }
}
int main(int argc, char **argv) {
    if (argc != 2 || (strcmp(argv[1], "5") && strcmp(argv[1], "8"))) {
        return 2;
    }
    pointer_version = (unsigned)atoi(argv[1]);
    struct wl_display *display = wl_display_connect(NULL);
    if (!display) return 1;
    struct wl_registry *registry = wl_display_get_registry(display);
    wl_registry_add_listener(registry, &registry_listener, NULL);
    roundtrip(display);
    roundtrip(display);
    if (!compositor || !shm || !shell || !pointer) return 1;
    struct wl_surface *surface = wl_compositor_create_surface(compositor);
    struct xdg_surface *xdg = xdg_wm_base_get_xdg_surface(shell, surface);
    xdg_surface_add_listener(xdg, &surface_listener, NULL);
    struct xdg_toplevel *top = xdg_surface_get_toplevel(xdg);
    xdg_toplevel_add_listener(top, &toplevel_listener, NULL);
    xdg_toplevel_set_title(top, "Xpra native pointer unit fixture");
    wl_surface_commit(surface);
    roundtrip(display);
    const int size = 64 * 64 * 4;
    int fd = memfd_create("xpra-pointer-test", MFD_CLOEXEC);
    if (fd < 0 || ftruncate(fd, size) != 0) return 1;
    void *pixels = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (pixels == MAP_FAILED) return 1;
    memset(pixels, 0x7f, size);
    struct wl_shm_pool *pool = wl_shm_create_pool(shm, fd, size);
    struct wl_buffer *buffer = wl_shm_pool_create_buffer(pool, 0, 64, 64, 64 * 4, WL_SHM_FORMAT_XRGB8888);
    wl_surface_attach(surface, buffer, 0, 0);
    wl_surface_damage(surface, 0, 0, 64, 64);
    wl_surface_commit(surface);
    roundtrip(display);
    puts("ready");
    fflush(stdout);
    int command;
    while ((command = getchar()) == '\n') {
        roundtrip(display);
        puts("sync");
        fflush(stdout);
    }
    if (command != 'q') return 1;
    wl_pointer_release(pointer);
    xdg_toplevel_destroy(top);
    xdg_surface_destroy(xdg);
    wl_surface_destroy(surface);
    wl_buffer_destroy(buffer);
    wl_shm_pool_destroy(pool);
    munmap(pixels, size);
    close(fd);
    wl_seat_release(seat);
    wl_shm_destroy(shm);
    xdg_wm_base_destroy(shell);
    wl_compositor_destroy(compositor);
    wl_registry_destroy(registry);
    roundtrip(display);
    wl_display_disconnect(display);
    return 0;
}
