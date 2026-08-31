/* Copyright (C) 2026 kogeler */

#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <linux/input-event-codes.h>
#include <poll.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include <wayland-client.h>

#include "xdg-shell-client-protocol.h"

#define PARENT_WIDTH 360
#define PARENT_HEIGHT 240
#define CHILD_WIDTH 260
#define CHILD_HEIGHT 160
#define PRESSURE_FRAMES 60

#define READY_MARKER "/tmp/xpra-empty-damage-fixture-ready"
#define START_MARKER "/tmp/xpra-empty-damage-pressure-start"
#define PRESSURE_MARKER "/tmp/xpra-empty-damage-pressure-ready"
#define CLICK_MARKER "/tmp/xpra-empty-damage-child-clicked"

#define PARENT_TITLE "Xpra Empty Damage Parent"
#define CHILD_TITLE "Xpra Empty Damage Child"

struct fixture;

struct fixture_window {
    struct fixture *fixture;
    struct wl_surface *surface;
    struct xdg_surface *xdg_surface;
    struct xdg_toplevel *toplevel;
    struct wl_buffer *buffer;
    struct wl_callback *frame_callback;
    void *pixels;
    size_t pixels_size;
    const char *title;
    int width;
    int height;
    uint64_t frame_count;
    bool mapped;
};

struct fixture {
    struct wl_display *display;
    struct wl_registry *registry;
    struct wl_compositor *compositor;
    struct wl_shm *shm;
    struct wl_seat *seat;
    struct wl_pointer *pointer;
    struct xdg_wm_base *wm_base;
    struct wl_surface *pointer_surface;
    struct fixture_window parent;
    struct fixture_window child;
    wl_fixed_t pointer_x;
    wl_fixed_t pointer_y;
    bool running;
    bool ready_published;
    bool pressure_started;
    bool pressure_published;
};

static void fail(const char *message)
{
    fprintf(stderr, "empty-damage fixture: %s: %s\n", message, strerror(errno));
    exit(EXIT_FAILURE);
}

static void fail_message(const char *message)
{
    fprintf(stderr, "empty-damage fixture: %s\n", message);
    exit(EXIT_FAILURE);
}

static void write_all(int descriptor, const char *data, size_t size)
{
    while (size > 0) {
        ssize_t written = write(descriptor, data, size);
        if (written < 0) {
            if (errno == EINTR) {
                continue;
            }
            fail("cannot write marker");
        }
        if (written == 0) {
            fail_message("marker write made no progress");
        }
        data += written;
        size -= (size_t) written;
    }
}

static void publish_marker(const char *path, const char *message)
{
    int descriptor = open(path, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
    if (descriptor < 0) {
        fail("cannot create marker");
    }
    write_all(descriptor, message, strlen(message));
    if (close(descriptor) < 0) {
        fail("cannot close marker");
    }
}

static void clear_marker(const char *path)
{
    if (unlink(path) < 0 && errno != ENOENT) {
        fail("cannot clear marker");
    }
}

static double monotonic_seconds(void)
{
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) < 0) {
        fail("cannot read monotonic clock");
    }
    return (double) now.tv_sec + (double) now.tv_nsec / 1000000000.0;
}

static void emit_event(const char *event, const struct fixture *fixture)
{
    printf(
        "{\"child_frames\":%llu,\"event\":\"%s\","
        "\"monotonic_seconds\":%.6f,\"parent_frames\":%llu}\n",
        (unsigned long long) fixture->child.frame_count,
        event,
        monotonic_seconds(),
        (unsigned long long) fixture->parent.frame_count
    );
    fflush(stdout);
}

static int create_shm_file(size_t size)
{
    char template[] = "/tmp/xpra-empty-damage-XXXXXX";
    int descriptor = mkstemp(template);
    if (descriptor < 0) {
        fail("cannot create shared-memory file");
    }
    if (unlink(template) < 0) {
        fail("cannot unlink shared-memory file");
    }
    if (fcntl(descriptor, F_SETFD, FD_CLOEXEC) < 0) {
        fail("cannot protect shared-memory descriptor");
    }
    if (ftruncate(descriptor, (off_t) size) < 0) {
        fail("cannot size shared-memory file");
    }
    return descriptor;
}

static void paint_window(struct fixture_window *window, uint32_t background, uint32_t accent)
{
    uint32_t *pixels = window->pixels;
    const int left = window->width / 5;
    const int right = window->width - left;
    const int top = window->height / 3;
    const int bottom = window->height - top;

    for (int y = 0; y < window->height; ++y) {
        for (int x = 0; x < window->width; ++x) {
            pixels[y * window->width + x] =
                x >= left && x < right && y >= top && y < bottom
                    ? accent
                    : background;
        }
    }
}

static void create_buffer(
    struct fixture_window *window,
    uint32_t background,
    uint32_t accent
)
{
    const size_t stride = (size_t) window->width * sizeof(uint32_t);
    if ((size_t) window->height > SIZE_MAX / stride) {
        fail_message("shared-memory buffer size overflow");
    }
    window->pixels_size = stride * (size_t) window->height;
    int descriptor = create_shm_file(window->pixels_size);
    window->pixels = mmap(
        NULL,
        window->pixels_size,
        PROT_READ | PROT_WRITE,
        MAP_SHARED,
        descriptor,
        0
    );
    if (window->pixels == MAP_FAILED) {
        fail("cannot map shared-memory buffer");
    }
    struct wl_shm_pool *pool = wl_shm_create_pool(
        window->fixture->shm,
        descriptor,
        (int) window->pixels_size
    );
    if (pool == NULL) {
        fail_message("cannot create Wayland shared-memory pool");
    }
    window->buffer = wl_shm_pool_create_buffer(
        pool,
        0,
        window->width,
        window->height,
        (int) stride,
        WL_SHM_FORMAT_XRGB8888
    );
    wl_shm_pool_destroy(pool);
    if (close(descriptor) < 0) {
        fail("cannot close shared-memory descriptor");
    }
    if (window->buffer == NULL) {
        fail_message("cannot create Wayland shared-memory buffer");
    }
    paint_window(window, background, accent);
}

static void maybe_publish_ready(struct fixture *fixture)
{
    if (!fixture->ready_published && fixture->parent.mapped && fixture->child.mapped) {
        publish_marker(READY_MARKER, "parent and child mapped\n");
        fixture->ready_published = true;
        emit_event("ready", fixture);
    }
}

static void maybe_publish_pressure(struct fixture *fixture)
{
    if (
        !fixture->pressure_published
        && fixture->parent.frame_count >= PRESSURE_FRAMES
        && fixture->child.frame_count >= PRESSURE_FRAMES
    ) {
        publish_marker(PRESSURE_MARKER, "both toplevels recycling empty commits\n");
        fixture->pressure_published = true;
        emit_event("pressure-ready", fixture);
    }
}

static void schedule_frame(struct fixture_window *window);

static void frame_done(
    void *data,
    struct wl_callback *callback,
    uint32_t callback_data
)
{
    (void) callback_data;
    struct fixture_window *window = data;
    if (window->frame_callback != callback) {
        fail_message("unexpected frame callback");
    }
    wl_callback_destroy(callback);
    window->frame_callback = NULL;
    ++window->frame_count;
    maybe_publish_pressure(window->fixture);
    if (!window->fixture->running) {
        return;
    }
    schedule_frame(window);
}

static void schedule_frame(struct fixture_window *window)
{
    window->frame_callback = wl_surface_frame(window->surface);
    if (window->frame_callback == NULL) {
        fail_message("cannot request Wayland frame callback");
    }
    static const struct wl_callback_listener listener = {frame_done};
    if (wl_callback_add_listener(window->frame_callback, &listener, window) < 0) {
        fail_message("cannot listen for Wayland frame callback");
    }
    /* This is intentionally an empty commit: no attach and no damage request. */
    wl_surface_commit(window->surface);
}

static void map_window(struct fixture_window *window)
{
    if (window->mapped) {
        return;
    }
    wl_surface_attach(window->surface, window->buffer, 0, 0);
    wl_surface_damage_buffer(window->surface, 0, 0, window->width, window->height);
    wl_surface_commit(window->surface);
    window->mapped = true;
    maybe_publish_ready(window->fixture);
}

static void xdg_surface_configure(
    void *data,
    struct xdg_surface *xdg_surface,
    uint32_t serial
)
{
    struct fixture_window *window = data;
    xdg_surface_ack_configure(xdg_surface, serial);
    map_window(window);
}

static const struct xdg_surface_listener xdg_surface_listener = {
    .configure = xdg_surface_configure,
};

static void toplevel_configure(
    void *data,
    struct xdg_toplevel *toplevel,
    int32_t width,
    int32_t height,
    struct wl_array *states
)
{
    (void) data;
    (void) toplevel;
    (void) width;
    (void) height;
    (void) states;
}

static void toplevel_close(void *data, struct xdg_toplevel *toplevel)
{
    (void) toplevel;
    struct fixture_window *window = data;
    window->fixture->running = false;
}

static void toplevel_configure_bounds(
    void *data,
    struct xdg_toplevel *toplevel,
    int32_t width,
    int32_t height
)
{
    (void) data;
    (void) toplevel;
    (void) width;
    (void) height;
}

static void toplevel_wm_capabilities(
    void *data,
    struct xdg_toplevel *toplevel,
    struct wl_array *capabilities
)
{
    (void) data;
    (void) toplevel;
    (void) capabilities;
}

static const struct xdg_toplevel_listener toplevel_listener = {
    .configure = toplevel_configure,
    .close = toplevel_close,
    .configure_bounds = toplevel_configure_bounds,
    .wm_capabilities = toplevel_wm_capabilities,
};

static void create_window(
    struct fixture *fixture,
    struct fixture_window *window,
    struct xdg_toplevel *parent,
    const char *title,
    int width,
    int height,
    uint32_t background,
    uint32_t accent
)
{
    *window = (struct fixture_window) {
        .fixture = fixture,
        .title = title,
        .width = width,
        .height = height,
    };
    create_buffer(window, background, accent);
    window->surface = wl_compositor_create_surface(fixture->compositor);
    if (window->surface == NULL) {
        fail_message("cannot create Wayland surface");
    }
    window->xdg_surface = xdg_wm_base_get_xdg_surface(
        fixture->wm_base,
        window->surface
    );
    if (window->xdg_surface == NULL) {
        fail_message("cannot create XDG surface");
    }
    if (xdg_surface_add_listener(window->xdg_surface, &xdg_surface_listener, window) < 0) {
        fail_message("cannot listen for XDG surface configure");
    }
    window->toplevel = xdg_surface_get_toplevel(window->xdg_surface);
    if (window->toplevel == NULL) {
        fail_message("cannot create XDG toplevel");
    }
    if (xdg_toplevel_add_listener(window->toplevel, &toplevel_listener, window) < 0) {
        fail_message("cannot listen for XDG toplevel events");
    }
    xdg_toplevel_set_title(window->toplevel, title);
    xdg_toplevel_set_app_id(window->toplevel, "org.xpra.EmptyDamageFixture");
    xdg_toplevel_set_min_size(window->toplevel, width, height);
    xdg_toplevel_set_max_size(window->toplevel, width, height);
    if (parent != NULL) {
        xdg_toplevel_set_parent(window->toplevel, parent);
    }
    wl_surface_commit(window->surface);
}

static void wm_base_ping(
    void *data,
    struct xdg_wm_base *wm_base,
    uint32_t serial
)
{
    (void) data;
    xdg_wm_base_pong(wm_base, serial);
}

static const struct xdg_wm_base_listener wm_base_listener = {
    .ping = wm_base_ping,
};

static void pointer_enter(
    void *data,
    struct wl_pointer *pointer,
    uint32_t serial,
    struct wl_surface *surface,
    wl_fixed_t surface_x,
    wl_fixed_t surface_y
)
{
    (void) pointer;
    (void) serial;
    struct fixture *fixture = data;
    fixture->pointer_surface = surface;
    fixture->pointer_x = surface_x;
    fixture->pointer_y = surface_y;
}

static void pointer_leave(
    void *data,
    struct wl_pointer *pointer,
    uint32_t serial,
    struct wl_surface *surface
)
{
    (void) pointer;
    (void) serial;
    struct fixture *fixture = data;
    if (fixture->pointer_surface == surface) {
        fixture->pointer_surface = NULL;
    }
}

static void pointer_motion(
    void *data,
    struct wl_pointer *pointer,
    uint32_t time,
    wl_fixed_t surface_x,
    wl_fixed_t surface_y
)
{
    (void) pointer;
    (void) time;
    struct fixture *fixture = data;
    fixture->pointer_x = surface_x;
    fixture->pointer_y = surface_y;
}

static void pointer_button(
    void *data,
    struct wl_pointer *pointer,
    uint32_t serial,
    uint32_t time,
    uint32_t button,
    uint32_t state
)
{
    (void) pointer;
    (void) serial;
    (void) time;
    struct fixture *fixture = data;
    if (
        fixture->pointer_surface == fixture->child.surface
        && button == BTN_LEFT
        && state == WL_POINTER_BUTTON_STATE_RELEASED
    ) {
        publish_marker(CLICK_MARKER, "child pointer release received\n");
        printf(
            "{\"event\":\"child-click\",\"monotonic_seconds\":%.6f,"
            "\"x\":%.3f,\"y\":%.3f}\n",
            monotonic_seconds(),
            wl_fixed_to_double(fixture->pointer_x),
            wl_fixed_to_double(fixture->pointer_y)
        );
        fflush(stdout);
        fixture->running = false;
    }
}

static void pointer_axis(
    void *data,
    struct wl_pointer *pointer,
    uint32_t time,
    uint32_t axis,
    wl_fixed_t value
)
{
    (void) data;
    (void) pointer;
    (void) time;
    (void) axis;
    (void) value;
}

static void pointer_frame(void *data, struct wl_pointer *pointer)
{
    (void) data;
    (void) pointer;
}

static void pointer_axis_source(
    void *data,
    struct wl_pointer *pointer,
    uint32_t source
)
{
    (void) data;
    (void) pointer;
    (void) source;
}

static void pointer_axis_stop(
    void *data,
    struct wl_pointer *pointer,
    uint32_t time,
    uint32_t axis
)
{
    (void) data;
    (void) pointer;
    (void) time;
    (void) axis;
}

static void pointer_axis_discrete(
    void *data,
    struct wl_pointer *pointer,
    uint32_t axis,
    int32_t discrete
)
{
    (void) data;
    (void) pointer;
    (void) axis;
    (void) discrete;
}

static const struct wl_pointer_listener pointer_listener = {
    .enter = pointer_enter,
    .leave = pointer_leave,
    .motion = pointer_motion,
    .button = pointer_button,
    .axis = pointer_axis,
    .frame = pointer_frame,
    .axis_source = pointer_axis_source,
    .axis_stop = pointer_axis_stop,
    .axis_discrete = pointer_axis_discrete,
};

static void seat_capabilities(
    void *data,
    struct wl_seat *seat,
    uint32_t capabilities
)
{
    struct fixture *fixture = data;
    if ((capabilities & WL_SEAT_CAPABILITY_POINTER) && fixture->pointer == NULL) {
        fixture->pointer = wl_seat_get_pointer(seat);
        if (fixture->pointer == NULL) {
            fail_message("cannot acquire Wayland pointer");
        }
        if (wl_pointer_add_listener(fixture->pointer, &pointer_listener, fixture) < 0) {
            fail_message("cannot listen for Wayland pointer events");
        }
    }
}

static void seat_name(void *data, struct wl_seat *seat, const char *name)
{
    (void) data;
    (void) seat;
    (void) name;
}

static const struct wl_seat_listener seat_listener = {
    .capabilities = seat_capabilities,
    .name = seat_name,
};

static uint32_t bounded_version(uint32_t offered, uint32_t maximum)
{
    return offered < maximum ? offered : maximum;
}

static void registry_global(
    void *data,
    struct wl_registry *registry,
    uint32_t name,
    const char *interface,
    uint32_t version
)
{
    struct fixture *fixture = data;
    if (strcmp(interface, wl_compositor_interface.name) == 0) {
        fixture->compositor = wl_registry_bind(
            registry,
            name,
            &wl_compositor_interface,
            bounded_version(version, 4)
        );
    } else if (strcmp(interface, wl_shm_interface.name) == 0) {
        fixture->shm = wl_registry_bind(registry, name, &wl_shm_interface, 1);
    } else if (strcmp(interface, xdg_wm_base_interface.name) == 0) {
        fixture->wm_base = wl_registry_bind(registry, name, &xdg_wm_base_interface, 1);
        if (fixture->wm_base != NULL) {
            xdg_wm_base_add_listener(fixture->wm_base, &wm_base_listener, fixture);
        }
    } else if (strcmp(interface, wl_seat_interface.name) == 0) {
        fixture->seat = wl_registry_bind(
            registry,
            name,
            &wl_seat_interface,
            bounded_version(version, 5)
        );
        if (fixture->seat != NULL) {
            wl_seat_add_listener(fixture->seat, &seat_listener, fixture);
        }
    }
}

static void registry_global_remove(
    void *data,
    struct wl_registry *registry,
    uint32_t name
)
{
    (void) data;
    (void) registry;
    (void) name;
}

static const struct wl_registry_listener registry_listener = {
    .global = registry_global,
    .global_remove = registry_global_remove,
};

static void destroy_window(struct fixture_window *window)
{
    if (window->frame_callback != NULL) {
        wl_callback_destroy(window->frame_callback);
    }
    if (window->toplevel != NULL) {
        xdg_toplevel_destroy(window->toplevel);
    }
    if (window->xdg_surface != NULL) {
        xdg_surface_destroy(window->xdg_surface);
    }
    if (window->surface != NULL) {
        wl_surface_destroy(window->surface);
    }
    if (window->buffer != NULL) {
        wl_buffer_destroy(window->buffer);
    }
    if (window->pixels != NULL && window->pixels != MAP_FAILED) {
        munmap(window->pixels, window->pixels_size);
    }
}

static void destroy_fixture(struct fixture *fixture)
{
    destroy_window(&fixture->child);
    destroy_window(&fixture->parent);
    if (fixture->pointer != NULL) {
        wl_pointer_destroy(fixture->pointer);
    }
    if (fixture->seat != NULL) {
        wl_seat_destroy(fixture->seat);
    }
    if (fixture->wm_base != NULL) {
        xdg_wm_base_destroy(fixture->wm_base);
    }
    if (fixture->shm != NULL) {
        wl_shm_destroy(fixture->shm);
    }
    if (fixture->compositor != NULL) {
        wl_compositor_destroy(fixture->compositor);
    }
    if (fixture->registry != NULL) {
        wl_registry_destroy(fixture->registry);
    }
    if (fixture->display != NULL) {
        wl_display_flush(fixture->display);
        wl_display_disconnect(fixture->display);
    }
}

int main(void)
{
    clear_marker(READY_MARKER);
    clear_marker(START_MARKER);
    clear_marker(PRESSURE_MARKER);
    clear_marker(CLICK_MARKER);

    struct fixture fixture = {.running = true};
    fixture.display = wl_display_connect(NULL);
    if (fixture.display == NULL) {
        fail_message("cannot connect to Wayland display");
    }
    fixture.registry = wl_display_get_registry(fixture.display);
    if (fixture.registry == NULL) {
        fail_message("cannot acquire Wayland registry");
    }
    if (wl_registry_add_listener(fixture.registry, &registry_listener, &fixture) < 0) {
        fail_message("cannot listen for Wayland globals");
    }
    if (wl_display_roundtrip(fixture.display) < 0 || wl_display_roundtrip(fixture.display) < 0) {
        fail_message("cannot enumerate Wayland globals");
    }
    if (
        fixture.compositor == NULL
        || fixture.shm == NULL
        || fixture.wm_base == NULL
        || fixture.seat == NULL
        || fixture.pointer == NULL
    ) {
        fail_message("required Wayland globals are unavailable");
    }

    create_window(
        &fixture,
        &fixture.parent,
        NULL,
        PARENT_TITLE,
        PARENT_WIDTH,
        PARENT_HEIGHT,
        0x00202b38,
        0x00405c78
    );
    create_window(
        &fixture,
        &fixture.child,
        fixture.parent.toplevel,
        CHILD_TITLE,
        CHILD_WIDTH,
        CHILD_HEIGHT,
        0x00334a5e,
        0x0031d07b
    );

    const int display_descriptor = wl_display_get_fd(fixture.display);
    while (fixture.running) {
        if (
            fixture.ready_published
            && !fixture.pressure_started
            && access(START_MARKER, F_OK) == 0
        ) {
            fixture.pressure_started = true;
            schedule_frame(&fixture.parent);
            schedule_frame(&fixture.child);
        }
        if (wl_display_dispatch_pending(fixture.display) < 0) {
            fail_message("Wayland pending-event dispatch failed");
        }
        if (wl_display_flush(fixture.display) < 0 && errno != EAGAIN) {
            fail("cannot flush Wayland display");
        }
        struct pollfd descriptor = {
            .fd = display_descriptor,
            .events = POLLIN,
        };
        int ready = poll(&descriptor, 1, 50);
        if (ready < 0) {
            if (errno == EINTR) {
                continue;
            }
            fail("cannot poll Wayland display");
        }
        if (ready > 0 && (descriptor.revents & (POLLERR | POLLHUP | POLLNVAL))) {
            fail_message("Wayland display poll failed");
        }
        if (ready > 0 && (descriptor.revents & POLLIN)) {
            if (wl_display_dispatch(fixture.display) < 0) {
                fail_message("Wayland display dispatch failed");
            }
        }
    }
    emit_event("exit", &fixture);
    destroy_fixture(&fixture);
    return EXIT_SUCCESS;
}
