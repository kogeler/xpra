/* This file is part of Xpra.
 * Copyright (C) 2026 kogeler
 * Xpra is released under the terms of the GNU GPL v2, or, at your option, any
 * later version. See the file COPYING for details.
 */

#define _POSIX_C_SOURCE 200809L
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>
#include <wayland-client.h>
#include "xdg-shell-client-protocol.h"
#include "viewporter-client-protocol.h"

static struct wl_compositor *compositor;
static struct wl_subcompositor *subcompositor;
static struct xdg_wm_base *shell;
static struct wl_shm *shm;
static struct wp_viewporter *viewporter;

static void ping(void *data, struct xdg_wm_base *base, uint32_t serial) {
    (void)data;
    xdg_wm_base_pong(base, serial);
}

static const struct xdg_wm_base_listener shell_listener = {.ping = ping};

static void global(void *data, struct wl_registry *registry, uint32_t name,
                   const char *interface, uint32_t version) {
    (void)data;
    if (strcmp(interface, "wl_compositor") == 0) {
        compositor = wl_registry_bind(registry, name, &wl_compositor_interface, version < 4 ? version : 4);
    } else if (strcmp(interface, "wl_subcompositor") == 0) {
        subcompositor = wl_registry_bind(registry, name, &wl_subcompositor_interface, 1);
    } else if (strcmp(interface, "xdg_wm_base") == 0) {
        shell = wl_registry_bind(registry, name, &xdg_wm_base_interface, 1);
        xdg_wm_base_add_listener(shell, &shell_listener, NULL);
    } else if (strcmp(interface, "wl_shm") == 0) {
        shm = wl_registry_bind(registry, name, &wl_shm_interface, 1);
    } else if (strcmp(interface, "wp_viewporter") == 0) {
        viewporter = wl_registry_bind(registry, name, &wp_viewporter_interface, 1);
    }
}

static void global_remove(void *data, struct wl_registry *registry, uint32_t name) {
    (void)data;
    (void)registry;
    (void)name;
}

static const struct wl_registry_listener registry_listener = {
    .global = global, .global_remove = global_remove,
};

static void configure(void *data, struct xdg_surface *surface, uint32_t serial) {
    (void)data;
    xdg_surface_ack_configure(surface, serial);
}

static const struct xdg_surface_listener surface_listener = {.configure = configure};

static void toplevel_configure(void *data, struct xdg_toplevel *toplevel,
                               int32_t width, int32_t height, struct wl_array *states) {
    (void)data;
    (void)toplevel;
    (void)width;
    (void)height;
    (void)states;
}

static void toplevel_close(void *data, struct xdg_toplevel *toplevel) {
    (void)data;
    (void)toplevel;
    abort();
}

static const struct xdg_toplevel_listener toplevel_listener = {
    .configure = toplevel_configure, .close = toplevel_close,
};

static void roundtrip(struct wl_display *display) {
    if (wl_display_roundtrip(display) < 0) {
        fprintf(stderr, "Wayland discovery client roundtrip failed\n");
        exit(1);
    }
}

static void report_and_wait(const char *message) {
    puts(message);
    fflush(stdout);
    if (getchar() != '\n') {
        exit(1);
    }
}

static void pixel_commits(struct wl_display *display, struct wl_surface *root,
                          struct wl_surface *child) {
    if (!shm || !viewporter) {
        exit(1);
    }
    char name[] = "/tmp/xpra-subsurface-pixels-XXXXXX";
    int fd = mkstemp(name);
    if (fd < 0) {
        exit(1);
    }
    unlink(name);
    if (ftruncate(fd, 128) != 0) {
        exit(1);
    }
    uint32_t *pixels = mmap(NULL, 128, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (pixels == MAP_FAILED) {
        exit(1);
    }
    for (int i = 0; i < 16; ++i) {
        pixels[i] = 0x00112233;
        pixels[16 + i] = 0xff112233;
    }
    struct wl_shm_pool *pool = wl_shm_create_pool(shm, fd, 128);
    struct wl_buffer *opaque = wl_shm_pool_create_buffer(pool, 0, 4, 4, 16, WL_SHM_FORMAT_XRGB8888);
    struct wl_buffer *alpha = wl_shm_pool_create_buffer(pool, 64, 4, 4, 16, WL_SHM_FORMAT_ARGB8888);
    struct wp_viewport *viewport = wp_viewporter_get_viewport(viewporter, root);
    wl_surface_attach(child, alpha, 0, 0);
    wl_surface_damage(child, 0, 0, 4, 4);
    wl_surface_commit(child);
    wl_surface_attach(root, opaque, 0, 0);
    wl_surface_damage(root, 0, 0, 4, 4);
    wl_surface_commit(root);
    roundtrip(display);
    report_and_wait("pixels");

    /* Change only raw formats: no damage or logical geometry changes. */
    wl_surface_attach(child, opaque, 0, 0);
    wl_surface_commit(child);
    wl_surface_attach(root, alpha, 0, 0);
    wl_surface_commit(root);
    roundtrip(display);
    report_and_wait("format");

    /* This tiny attached buffer must never allocate the oversized raster. */
    wp_viewport_set_destination(viewport, 65536, 65536);
    wl_surface_commit(root);
    roundtrip(display);
    report_and_wait("oversized");
    wp_viewport_set_destination(viewport, 4, 4);
    wl_surface_commit(root);
    roundtrip(display);
    report_and_wait("recovered");

    /* The reader injects one model retention failure, then sends no damage. */
    wl_surface_damage(root, 0, 0, 4, 4);
    wl_surface_commit(root);
    roundtrip(display);
    report_and_wait("failed-retain");
    wl_surface_commit(root);
    roundtrip(display);
    report_and_wait("empty-recovered");

    wl_surface_attach(child, NULL, 0, 0);
    wl_surface_commit(child);
    wl_surface_attach(root, NULL, 0, 0);
    wl_surface_commit(root);
    roundtrip(display);
    report_and_wait("unmapped");
    wp_viewport_destroy(viewport);
    wl_buffer_destroy(opaque);
    wl_buffer_destroy(alpha);
    wl_shm_pool_destroy(pool);
    munmap(pixels, 128);
    close(fd);
}

int main(int argc, char **argv) {
    if (argc != 2) {
        return 2;
    }
    const int prebuilt_root = strcmp(argv[1], "prebuilt-root") == 0;
    const int prebuilt_child = strcmp(argv[1], "prebuilt-child") == 0;
    const int test_pixels = strcmp(argv[1], "pixels") == 0;
    if (!prebuilt_root && !prebuilt_child && !test_pixels && strcmp(argv[1], "synchronized") != 0) {
        return 2;
    }
    struct wl_display *display = wl_display_connect(NULL);
    if (!display) {
        return 1;
    }
    struct wl_registry *registry = wl_display_get_registry(display);
    wl_registry_add_listener(registry, &registry_listener, NULL);
    roundtrip(display);
    if (!compositor || !subcompositor || !shell) {
        return 1;
    }
    struct wl_surface *root = wl_compositor_create_surface(compositor);
    struct wl_surface *child = wl_compositor_create_surface(compositor);
    struct wl_surface *leaf = wl_compositor_create_surface(compositor);
    struct wl_subsurface *child_role = NULL;
    struct wl_subsurface *leaf_role = NULL;
    struct xdg_surface *xdg_surface = NULL;
    struct xdg_toplevel *toplevel = NULL;

    if (prebuilt_root) {
        child_role = wl_subcompositor_get_subsurface(subcompositor, child, root);
        leaf_role = wl_subcompositor_get_subsurface(subcompositor, leaf, child);
        wl_surface_commit(child);
        wl_surface_commit(root);
        roundtrip(display);
    }
    xdg_surface = xdg_wm_base_get_xdg_surface(shell, root);
    xdg_surface_add_listener(xdg_surface, &surface_listener, NULL);
    toplevel = xdg_surface_get_toplevel(xdg_surface);
    xdg_toplevel_add_listener(toplevel, &toplevel_listener, NULL);
    wl_surface_commit(root);
    roundtrip(display);

    if (!prebuilt_root) {
        if (prebuilt_child) {
            leaf_role = wl_subcompositor_get_subsurface(subcompositor, leaf, child);
            wl_surface_commit(child);
            roundtrip(display);
        }
        child_role = wl_subcompositor_get_subsurface(subcompositor, child, root);
        if (!leaf_role) {
            leaf_role = wl_subcompositor_get_subsurface(subcompositor, leaf, child);
        }
        wl_surface_commit(child);
        wl_surface_commit(root);
        roundtrip(display);
    }
    /* No buffer is required for discovery: unmapped roles own listeners too. */
    report_and_wait("ready");
    if (test_pixels) {
        pixel_commits(display, root, child);
    }
    wl_surface_commit(leaf);
    wl_surface_commit(child);
    wl_surface_commit(root);
    roundtrip(display);
    report_and_wait("recommitted");

    wl_subsurface_destroy(leaf_role);
    wl_subsurface_destroy(child_role);
    wl_surface_destroy(leaf);
    wl_surface_destroy(child);
    xdg_toplevel_destroy(toplevel);
    xdg_surface_destroy(xdg_surface);
    wl_surface_destroy(root);
    roundtrip(display);
    puts("destroyed");
    fflush(stdout);
    wl_subcompositor_destroy(subcompositor);
    wl_compositor_destroy(compositor);
    xdg_wm_base_destroy(shell);
    if (shm) {
        wl_shm_destroy(shm);
    }
    if (viewporter) {
        wp_viewporter_destroy(viewporter);
    }
    wl_registry_destroy(registry);
    wl_display_disconnect(display);
    return 0;
}
