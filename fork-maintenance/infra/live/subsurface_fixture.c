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
#include <time.h>
#include <unistd.h>
#include <wayland-client.h>

#include "xdg-shell-client-protocol.h"

#define STRINGIFY_DETAIL(value) #value
#define STRINGIFY(value) STRINGIFY_DETAIL(value)
#define SUBSURFACE_FIXTURE_SCHEMA 6
#define JSON_SCHEMA "\"schema\":" STRINGIFY(SUBSURFACE_FIXTURE_SCHEMA)

#define PRIMARY_WIDTH 420
#define PRIMARY_HEIGHT 300
#define SECONDARY_WIDTH 360
#define SECONDARY_HEIGHT 260

#define LOWER_WIDTH 220
#define LOWER_HEIGHT 140
#define LOWER_BUFFER_SCALE 2
#define LOWER_BUFFER_WIDTH (LOWER_WIDTH * LOWER_BUFFER_SCALE)
#define LOWER_BUFFER_HEIGHT (LOWER_HEIGHT * LOWER_BUFFER_SCALE)
#define LOWER_INITIAL_X 72
#define LOWER_INITIAL_Y 64
#define LOWER_MOVED_X 48
#define LOWER_MOVED_Y 110

#define UPPER_WIDTH 160
#define UPPER_HEIGHT 100
#define UPPER_X 150
#define UPPER_Y 150
#define UPPER_REPARENT_X 80
#define UPPER_REPARENT_Y 70
#define UPPER_BUFFER_TRANSFORM_NAME "180"

#define OVERLAP_X UPPER_X
#define OVERLAP_Y UPPER_Y
#define OVERLAP_WIDTH (LOWER_MOVED_X + LOWER_WIDTH - UPPER_X)
#define OVERLAP_HEIGHT (LOWER_MOVED_Y + LOWER_HEIGHT - UPPER_Y)

#define READY_MARKER "/tmp/xpra-subsurface-ready"
#define UPDATE_MARKER "/tmp/xpra-subsurface-update-two"
#define RESTORE_MARKER "/tmp/xpra-subsurface-restore-one"
#define MOVE_MARKER "/tmp/xpra-subsurface-move-lower"
#define STACK_MARKER "/tmp/xpra-subsurface-create-upper"
#define LOWER_UPDATE_MARKER "/tmp/xpra-subsurface-update-lower-under-upper"
#define FRAME_GENERATION_ONE_MARKER "/tmp/xpra-subsurface-frame-generation-one"
#define FRAME_GENERATION_TWO_MARKER "/tmp/xpra-subsurface-frame-generation-two"
#define CONTINUOUS_START_MARKER "/tmp/xpra-subsurface-continuous-start"
#define CONTINUOUS_STOP_MARKER "/tmp/xpra-subsurface-continuous-stop"
#define CLICK_MARKER "/tmp/xpra-subsurface-upper-clicked"
#define DESTROY_LOWER_MARKER "/tmp/xpra-subsurface-destroy-lower"
#define DETACH_UPPER_MARKER "/tmp/xpra-subsurface-detach-upper"
#define REPARENT_UPPER_MARKER "/tmp/xpra-subsurface-reparent-upper"
#define EXIT_MARKER "/tmp/xpra-subsurface-exit"

#define CONTINUOUS_MIN_GENERATIONS 2
#define CONTINUOUS_MAX_GENERATIONS 256
#define CONTINUOUS_MIN_INTERVAL_NS 50000000
#define CONTINUOUS_DAMAGE_X 112
#define CONTINUOUS_DAMAGE_Y 50
#define CONTINUOUS_DAMAGE_WIDTH 32
#define CONTINUOUS_DAMAGE_HEIGHT 32

#define PRIMARY_TITLE "Xpra Wayland Subsurface Fixture"
#define SECONDARY_TITLE "Xpra Wayland Subsurface Reparent Target"

enum pixel_pattern {
    PRIMARY_PARENT,
    SECONDARY_PARENT,
    LOWER_STATE_ONE,
    LOWER_STATE_TWO,
    LOWER_STATE_THREE,
    LOWER_STATE_FOUR,
    UPPER_STATE,
    LOWER_CONTINUOUS_ONE,
};

struct pixel_buffer {
    struct wl_buffer *buffer;
    void *pixels;
    size_t size;
};

struct fixture {
    struct wl_display *display;
    struct wl_registry *registry;
    struct wl_compositor *compositor;
    struct wl_subcompositor *subcompositor;
    struct wl_shm *shm;
    struct wl_seat *seat;
    struct wl_pointer *pointer;
    struct xdg_wm_base *wm_base;

    struct wl_surface *parent_surfaces[2];
    struct xdg_surface *parent_xdg_surfaces[2];
    struct xdg_toplevel *parent_toplevels[2];
    struct pixel_buffer parent_buffers[2];
    bool parent_mapped[2];

    struct wl_surface *lower_surface;
    struct wl_subsurface *lower_subsurface;
    struct pixel_buffer lower_buffers[7];
    struct wl_callback *lower_frame_callback;
    uint32_t lower_frame_callback_data;
    uint32_t lower_frame_callback_id;
    unsigned int lower_attach_count;
    unsigned int lower_commit_count;
    unsigned int lower_frame_done_count;
    unsigned int lower_generation_count;
    unsigned int lower_continuous_generation_count;
    uint64_t lower_continuous_commit_ns;
    unsigned int lower_update_count;
    int lower_state;
    bool lower_frame_ready;
    bool lower_continuous_active;
    bool lower_continuous_stopped;

    struct wl_surface *upper_surface;
    struct wl_subsurface *upper_subsurface;
    struct pixel_buffer upper_buffer;
    unsigned int upper_attach_count;
    unsigned int upper_commit_count;
    bool upper_precommitted_before_role;
    bool upper_reattach_without_child_commit;

    struct wl_surface *pointer_surface;
    wl_fixed_t pointer_x;
    wl_fixed_t pointer_y;
    uint64_t event_sequence;
    unsigned int click_count;
    bool ready_emitted;
    bool lower_moved;
    bool upper_created;
    bool lower_destroyed;
    bool upper_detached;
    bool upper_reparented;
    bool running;
};

static void fail(const char *message)
{
    fprintf(stderr, "subsurface fixture: %s: %s\n", message, strerror(errno));
    exit(EXIT_FAILURE);
}

static void fail_message(const char *message)
{
    fprintf(stderr, "subsurface fixture: %s\n", message);
    exit(EXIT_FAILURE);
}

static uint64_t monotonic_ns(void)
{
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) < 0) {
        fail("cannot read monotonic clock");
    }
    return (uint64_t) now.tv_sec * UINT64_C(1000000000) + (uint64_t) now.tv_nsec;
}

static uint32_t proxy_id(void *proxy)
{
    if (proxy == NULL) {
        fail_message("cannot read a null Wayland proxy identity");
    }
    return wl_proxy_get_id((struct wl_proxy *) proxy);
}

static void emit_ready(struct fixture *fixture)
{
    printf(
        "{\"event\":\"ready\",\"lower_attach_count\":%u,"
        "\"lower_buffer_dimensions\":[%d,%d],\"lower_buffer_id\":%u,"
        "\"lower_buffer_scale\":%d,\"lower_commit_count\":%u,"
        "\"lower_dimensions\":[%d,%d],\"lower_offset\":[%d,%d],"
        "\"lower_state_id\":1,\"lower_surface_id\":%u,"
        "\"monotonic_ns\":%llu,\"parent_dimensions\":[%d,%d],"
        "\"parents_alive\":2," JSON_SCHEMA ","
        "\"secondary_parent_dimensions\":[%d,%d],\"sequence\":%llu}\n",
        fixture->lower_attach_count,
        LOWER_BUFFER_WIDTH,
        LOWER_BUFFER_HEIGHT,
        proxy_id(fixture->lower_buffers[0].buffer),
        LOWER_BUFFER_SCALE,
        fixture->lower_commit_count,
        LOWER_WIDTH,
        LOWER_HEIGHT,
        LOWER_INITIAL_X,
        LOWER_INITIAL_Y,
        proxy_id(fixture->lower_surface),
        (unsigned long long) monotonic_ns(),
        PRIMARY_WIDTH,
        PRIMARY_HEIGHT,
        SECONDARY_WIDTH,
        SECONDARY_HEIGHT,
        (unsigned long long) fixture->event_sequence++
    );
    fflush(stdout);
}

static void emit_lower_state(
    struct fixture *fixture,
    const char *event,
    unsigned int buffer_index
)
{
    printf(
        "{\"event\":\"%s\",\"lower_attach_count\":%u,"
        "\"lower_buffer_id\":%u,"
        "\"lower_buffer_scale\":%d,\"lower_commit_count\":%u,"
        "\"lower_state_id\":%d,"
        "\"lower_surface_id\":%u,\"monotonic_ns\":%llu,"
        JSON_SCHEMA ",\"sequence\":%llu,\"update_index\":%u,"
        "\"upper_attach_count\":%u,\"upper_commit_count\":%u}\n",
        event,
        fixture->lower_attach_count,
        proxy_id(fixture->lower_buffers[buffer_index].buffer),
        LOWER_BUFFER_SCALE,
        fixture->lower_commit_count,
        fixture->lower_state,
        proxy_id(fixture->lower_surface),
        (unsigned long long) monotonic_ns(),
        (unsigned long long) fixture->event_sequence++,
        fixture->lower_update_count,
        fixture->upper_attach_count,
        fixture->upper_commit_count
    );
    fflush(stdout);
}

static void emit_lower_frame_generation(
    struct fixture *fixture,
    unsigned int buffer_index,
    uint32_t callback_id,
    uint32_t callback_data
)
{
    printf(
        "{\"event\":\"lower-frame-generation\","
        "\"frame_callback_data\":%u,\"frame_callback_id\":%u,"
        "\"frame_done_count\":%u,\"generation_id\":%u,"
        "\"lower_attach_count\":%u,\"lower_buffer_id\":%u,"
        "\"lower_buffer_scale\":%d,\"lower_commit_count\":%u,"
        "\"lower_state_id\":%d,\"lower_surface_id\":%u,"
        "\"monotonic_ns\":%llu," JSON_SCHEMA ",\"sequence\":%llu,"
        "\"update_index\":%u,\"upper_attach_count\":%u,"
        "\"upper_commit_count\":%u}\n",
        callback_data,
        callback_id,
        fixture->lower_frame_done_count,
        fixture->lower_generation_count,
        fixture->lower_attach_count,
        proxy_id(fixture->lower_buffers[buffer_index].buffer),
        LOWER_BUFFER_SCALE,
        fixture->lower_commit_count,
        fixture->lower_state,
        proxy_id(fixture->lower_surface),
        (unsigned long long) monotonic_ns(),
        (unsigned long long) fixture->event_sequence++,
        fixture->lower_update_count,
        fixture->upper_attach_count,
        fixture->upper_commit_count
    );
    fflush(stdout);
}

static void emit_continuous_start(struct fixture *fixture)
{
    printf(
        "{\"continuous_buffer_ids\":[%u,%u],"
        "\"continuous_generation_count\":0,\"event\":\"continuous-start\","
        "\"frame_callback_pending\":%s,\"frame_callback_ready\":%s,"
        "\"frame_done_count\":%u,\"lower_attach_count\":%u,"
        "\"lower_buffer_id\":%u,\"lower_commit_count\":%u,"
        "\"lower_state_id\":%d,\"lower_surface_id\":%u,"
        "\"lower_update_count\":%u,\"monotonic_ns\":%llu,"
        "\"producer_active\":true," JSON_SCHEMA ",\"sequence\":%llu,"
        "\"upper_attach_count\":%u,\"upper_commit_count\":%u}\n",
        proxy_id(fixture->lower_buffers[6].buffer),
        proxy_id(fixture->lower_buffers[5].buffer),
        fixture->lower_frame_callback != NULL ? "true" : "false",
        fixture->lower_frame_ready ? "true" : "false",
        fixture->lower_frame_done_count,
        fixture->lower_attach_count,
        proxy_id(fixture->lower_buffers[5].buffer),
        fixture->lower_commit_count,
        fixture->lower_state,
        proxy_id(fixture->lower_surface),
        fixture->lower_update_count,
        (unsigned long long) monotonic_ns(),
        (unsigned long long) fixture->event_sequence++,
        fixture->upper_attach_count,
        fixture->upper_commit_count
    );
    fflush(stdout);
}

static void emit_continuous_generation(
    struct fixture *fixture,
    unsigned int buffer_index,
    uint32_t callback_id,
    uint32_t callback_data,
    uint64_t committed_ns
)
{
    printf(
        "{\"continuous_generation_id\":%u,\"event\":\"continuous-generation\","
        "\"frame_callback_data\":%u,\"frame_callback_id\":%u,"
        "\"frame_done_count\":%u,\"lower_attach_count\":%u,"
        "\"lower_buffer_id\":%u,\"lower_buffer_scale\":%d,"
        "\"lower_commit_count\":%u,\"lower_state_id\":%d,"
        "\"lower_surface_id\":%u,\"lower_update_count\":%u,"
        "\"monotonic_ns\":%llu,\"producer_active\":true,"
        JSON_SCHEMA ",\"sequence\":%llu,\"upper_attach_count\":%u,"
        "\"upper_commit_count\":%u}\n",
        fixture->lower_continuous_generation_count,
        callback_data,
        callback_id,
        fixture->lower_frame_done_count,
        fixture->lower_attach_count,
        proxy_id(fixture->lower_buffers[buffer_index].buffer),
        LOWER_BUFFER_SCALE,
        fixture->lower_commit_count,
        fixture->lower_state,
        proxy_id(fixture->lower_surface),
        fixture->lower_update_count,
        (unsigned long long) committed_ns,
        (unsigned long long) fixture->event_sequence++,
        fixture->upper_attach_count,
        fixture->upper_commit_count
    );
    fflush(stdout);
}

static void emit_continuous_stop(
    struct fixture *fixture,
    bool terminal_callback_completed,
    bool pending_callback_cancelled,
    uint32_t callback_id,
    uint32_t callback_data
)
{
    const unsigned int buffer_index =
        fixture->lower_continuous_generation_count % 2 == 0 ? 5 : 6;
    printf(
        "{\"continuous_buffer_ids\":[%u,%u],"
        "\"continuous_generation_count\":%u,\"event\":\"continuous-stop\","
        "\"frame_done_count\":%u,\"lower_attach_count\":%u,"
        "\"lower_buffer_id\":%u,\"lower_commit_count\":%u,"
        "\"lower_state_id\":%d,\"lower_surface_id\":%u,"
        "\"lower_update_count\":%u,\"monotonic_ns\":%llu,"
        "\"pending_callback_cancelled\":%s,\"producer_active\":false,"
        JSON_SCHEMA ",\"sequence\":%llu,\"terminal_callback_completed\":%s,"
        "\"terminal_callback_data\":%u,\"terminal_callback_id\":%u,"
        "\"upper_attach_count\":%u,\"upper_commit_count\":%u}\n",
        proxy_id(fixture->lower_buffers[6].buffer),
        proxy_id(fixture->lower_buffers[5].buffer),
        fixture->lower_continuous_generation_count,
        fixture->lower_frame_done_count,
        fixture->lower_attach_count,
        proxy_id(fixture->lower_buffers[buffer_index].buffer),
        fixture->lower_commit_count,
        fixture->lower_state,
        proxy_id(fixture->lower_surface),
        fixture->lower_update_count,
        (unsigned long long) monotonic_ns(),
        pending_callback_cancelled ? "true" : "false",
        (unsigned long long) fixture->event_sequence++,
        terminal_callback_completed ? "true" : "false",
        callback_data,
        callback_id,
        fixture->upper_attach_count,
        fixture->upper_commit_count
    );
    fflush(stdout);
}

static void emit_lower_moved(struct fixture *fixture)
{
    printf(
        "{\"event\":\"lower-moved\",\"from_offset\":[%d,%d],"
        "\"lower_attach_count\":%u,\"lower_buffer_scale\":%d,"
        "\"lower_commit_count\":%u,"
        "\"lower_surface_id\":%u,\"monotonic_ns\":%llu,"
        JSON_SCHEMA ",\"sequence\":%llu,\"to_offset\":[%d,%d]}\n",
        LOWER_INITIAL_X,
        LOWER_INITIAL_Y,
        fixture->lower_attach_count,
        LOWER_BUFFER_SCALE,
        fixture->lower_commit_count,
        proxy_id(fixture->lower_surface),
        (unsigned long long) monotonic_ns(),
        (unsigned long long) fixture->event_sequence++,
        LOWER_MOVED_X,
        LOWER_MOVED_Y
    );
    fflush(stdout);
}

static void emit_sibling_created(struct fixture *fixture)
{
    printf(
        "{\"event\":\"sibling-created\",\"lower_offset\":[%d,%d],"
        "\"monotonic_ns\":%llu,\"overlap\":[%d,%d,%d,%d],"
        JSON_SCHEMA ",\"sequence\":%llu,"
        "\"stacking\":[\"lower\",\"upper\"],"
        "\"upper_attach_count\":%u,\"upper_buffer_id\":%u,"
        "\"upper_buffer_transform\":\"" UPPER_BUFFER_TRANSFORM_NAME "\","
        "\"upper_commit_count\":%u,\"upper_dimensions\":[%d,%d],"
        "\"upper_offset\":[%d,%d],"
        "\"upper_precommitted_before_role\":%s,"
        "\"upper_surface_id\":%u}\n",
        LOWER_MOVED_X,
        LOWER_MOVED_Y,
        (unsigned long long) monotonic_ns(),
        OVERLAP_X,
        OVERLAP_Y,
        OVERLAP_WIDTH,
        OVERLAP_HEIGHT,
        (unsigned long long) fixture->event_sequence++,
        fixture->upper_attach_count,
        proxy_id(fixture->upper_buffer.buffer),
        fixture->upper_commit_count,
        UPPER_WIDTH,
        UPPER_HEIGHT,
        UPPER_X,
        UPPER_Y,
        fixture->upper_precommitted_before_role ? "true" : "false",
        proxy_id(fixture->upper_surface)
    );
    fflush(stdout);
}

static void emit_sibling_click(struct fixture *fixture)
{
    printf(
        "{\"event\":\"sibling-click\",\"monotonic_ns\":%llu,"
        "\"parent_coordinates\":[%d,%d]," JSON_SCHEMA ","
        "\"sequence\":%llu,\"surface_coordinates\":[%.3f,%.3f],"
        "\"target\":\"upper\"}\n",
        (unsigned long long) monotonic_ns(),
        UPPER_X + UPPER_WIDTH / 2,
        UPPER_Y + UPPER_HEIGHT / 2,
        (unsigned long long) fixture->event_sequence++,
        wl_fixed_to_double(fixture->pointer_x),
        wl_fixed_to_double(fixture->pointer_y)
    );
    fflush(stdout);
}

static void emit_lower_destroyed(struct fixture *fixture)
{
    printf(
        "{\"event\":\"lower-destroyed\",\"lower_update_count\":%u,"
        "\"monotonic_ns\":%llu,\"parents_alive\":2," JSON_SCHEMA ","
        "\"sequence\":%llu,\"upper_alive\":true}\n",
        fixture->lower_update_count,
        (unsigned long long) monotonic_ns(),
        (unsigned long long) fixture->event_sequence++
    );
    fflush(stdout);
}

static void emit_upper_detached(struct fixture *fixture)
{
    printf(
        "{\"event\":\"upper-detached\",\"lower_destroyed\":true,"
        "\"monotonic_ns\":%llu,\"old_parent\":\"primary\","
        "\"parents_alive\":2," JSON_SCHEMA ",\"sequence\":%llu,"
        "\"upper_attach_count\":%u,\"upper_buffer_id\":%u,"
        "\"upper_buffer_transform\":\"" UPPER_BUFFER_TRANSFORM_NAME "\","
        "\"upper_commit_count\":%u,"
        "\"upper_precommitted_before_role\":%s,"
        "\"upper_surface_id\":%u}\n",
        (unsigned long long) monotonic_ns(),
        (unsigned long long) fixture->event_sequence++,
        fixture->upper_attach_count,
        proxy_id(fixture->upper_buffer.buffer),
        fixture->upper_commit_count,
        fixture->upper_precommitted_before_role ? "true" : "false",
        proxy_id(fixture->upper_surface)
    );
    fflush(stdout);
}

static void emit_upper_reparented(struct fixture *fixture)
{
    printf(
        "{\"event\":\"upper-reparented\",\"monotonic_ns\":%llu,"
        "\"new_offset\":[%d,%d],\"new_parent\":\"secondary\","
        "\"parents_alive\":2," JSON_SCHEMA ",\"sequence\":%llu,"
        "\"upper_attach_count\":%u,\"upper_buffer_id\":%u,"
        "\"upper_buffer_transform\":\"" UPPER_BUFFER_TRANSFORM_NAME "\","
        "\"upper_commit_count\":%u,"
        "\"upper_precommitted_before_role\":%s,"
        "\"upper_reattach_parent_committed\":true,"
        "\"upper_reattach_without_child_commit\":%s,"
        "\"upper_surface_id\":%u}\n",
        (unsigned long long) monotonic_ns(),
        UPPER_REPARENT_X,
        UPPER_REPARENT_Y,
        (unsigned long long) fixture->event_sequence++,
        fixture->upper_attach_count,
        proxy_id(fixture->upper_buffer.buffer),
        fixture->upper_commit_count,
        fixture->upper_precommitted_before_role ? "true" : "false",
        fixture->upper_reattach_without_child_commit ? "true" : "false",
        proxy_id(fixture->upper_surface)
    );
    fflush(stdout);
}

static void emit_exit(struct fixture *fixture)
{
    printf(
        "{\"click_count\":%u,\"event\":\"exit\","
        "\"lower_destroyed\":true,\"lower_update_count\":%u,"
        "\"monotonic_ns\":%llu,\"parents_alive\":2," JSON_SCHEMA ","
        "\"sequence\":%llu,\"upper_reparented\":true}\n",
        fixture->click_count,
        fixture->lower_update_count,
        (unsigned long long) monotonic_ns(),
        (unsigned long long) fixture->event_sequence++
    );
    fflush(stdout);
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

static bool consume_marker(const char *path)
{
    if (unlink(path) == 0) {
        return true;
    }
    if (errno == ENOENT) {
        return false;
    }
    fail("cannot consume marker");
    return false;
}

static int create_shm_file(size_t size)
{
    char template[] = "/tmp/xpra-subsurface-XXXXXX";
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

static uint32_t premultiplied_argb(
    uint32_t alpha,
    uint32_t red,
    uint32_t green,
    uint32_t blue
)
{
    red = (red * alpha + 127) / 255;
    green = (green * alpha + 127) / 255;
    blue = (blue * alpha + 127) / 255;
    return alpha << 24 | red << 16 | green << 8 | blue;
}

static uint32_t pattern_pixel(enum pixel_pattern pattern, int x, int y)
{
    if (pattern == LOWER_CONTINUOUS_ONE) {
        /* Buffer replacement preserves every pixel outside the damage region. */
        pattern = x >= CONTINUOUS_DAMAGE_X && y >= CONTINUOUS_DAMAGE_Y
            && x < CONTINUOUS_DAMAGE_X + CONTINUOUS_DAMAGE_WIDTH
            && y < CONTINUOUS_DAMAGE_Y + CONTINUOUS_DAMAGE_HEIGHT
            ? LOWER_STATE_THREE : LOWER_STATE_FOUR;
    }
    uint32_t alpha = 255;
    uint32_t red;
    uint32_t green;
    uint32_t blue;
    switch (pattern) {
    case PRIMARY_PARENT:
        red = (uint32_t) (24 + (x * 5 + y * 3) % 72);
        green = (uint32_t) (42 + (x * 2 + y * 7) % 88);
        blue = (uint32_t) (76 + (x * 3 + y * 5) % 112);
        break;
    case SECONDARY_PARENT:
        red = (uint32_t) (78 + (x * 2 + y * 5) % 96);
        green = (uint32_t) (22 + (x * 7 + y * 3) % 80);
        blue = (uint32_t) (118 + (x * 5 + y * 2) % 110);
        break;
    case LOWER_STATE_ONE:
        alpha = 144;
        red = (uint32_t) (132 + (x * 3 + y * 5) % 112);
        green = (uint32_t) (20 + (x * 7 + y * 2) % 92);
        blue = (uint32_t) (64 + (x * 2 + y * 3) % 128);
        break;
    case LOWER_STATE_TWO:
        alpha = 144;
        red = (uint32_t) (18 + (x * 5 + y * 2) % 84);
        green = (uint32_t) (126 + (x * 3 + y * 7) % 118);
        blue = (uint32_t) (32 + (x * 7 + y * 5) % 96);
        break;
    case LOWER_STATE_THREE:
        alpha = 144;
        red = (uint32_t) (42 + (x * 7 + y * 3) % 108);
        green = (uint32_t) (54 + (x * 2 + y * 5) % 112);
        blue = (uint32_t) (136 + (x * 5 + y * 7) % 108);
        break;
    case LOWER_STATE_FOUR:
        alpha = 144;
        red = (uint32_t) (118 + (x * 2 + y * 7) % 126);
        green = (uint32_t) (36 + (x * 5 + y * 3) % 102);
        blue = (uint32_t) (26 + (x * 7 + y * 2) % 96);
        break;
    case UPPER_STATE:
        alpha = 176;
        red = (uint32_t) (220 + (x * 2 + y * 3) % 35);
        green = (uint32_t) (138 + (x * 5 + y * 2) % 80);
        blue = (uint32_t) (8 + (x * 3 + y * 7) % 64);
        break;
    default:
        fail_message("invalid pixel pattern");
    }
    return premultiplied_argb(alpha, red, green, blue);
}

static void create_buffer(
    struct fixture *fixture,
    struct pixel_buffer *result,
    int width,
    int height,
    enum pixel_pattern pattern,
    int scale,
    enum wl_output_transform transform
)
{
    if (
        scale <= 0
        || (transform != WL_OUTPUT_TRANSFORM_NORMAL
            && transform != WL_OUTPUT_TRANSFORM_180)
        || width > INT32_MAX / scale
        || height > INT32_MAX / scale
    ) {
        fail_message("invalid shared-memory buffer layout");
    }
    const int buffer_width = width * scale;
    const int buffer_height = height * scale;
    const size_t stride = (size_t) buffer_width * sizeof(uint32_t);
    if ((size_t) buffer_height > SIZE_MAX / stride) {
        fail_message("shared-memory buffer size overflow");
    }
    result->size = stride * (size_t) buffer_height;
    int descriptor = create_shm_file(result->size);
    result->pixels = mmap(
        NULL,
        result->size,
        PROT_READ | PROT_WRITE,
        MAP_SHARED,
        descriptor,
        0
    );
    if (result->pixels == MAP_FAILED) {
        fail("cannot map shared-memory buffer");
    }
    struct wl_shm_pool *pool = wl_shm_create_pool(
        fixture->shm,
        descriptor,
        (int) result->size
    );
    if (pool == NULL) {
        fail_message("cannot create Wayland shared-memory pool");
    }
    uint32_t format = pattern <= SECONDARY_PARENT
        ? WL_SHM_FORMAT_XRGB8888
        : WL_SHM_FORMAT_ARGB8888;
    result->buffer = wl_shm_pool_create_buffer(
        pool,
        0,
        buffer_width,
        buffer_height,
        (int) stride,
        format
    );
    wl_shm_pool_destroy(pool);
    if (close(descriptor) < 0) {
        fail("cannot close shared-memory descriptor");
    }
    if (result->buffer == NULL) {
        fail_message("cannot create Wayland shared-memory buffer");
    }
    uint32_t *pixels = result->pixels;
    for (int y = 0; y < buffer_height; ++y) {
        for (int x = 0; x < buffer_width; ++x) {
            int logical_x = x / scale;
            int logical_y = y / scale;
            if (transform == WL_OUTPUT_TRANSFORM_180) {
                logical_x = width - logical_x - 1;
                logical_y = height - logical_y - 1;
            }
            pixels[y * buffer_width + x] = pattern_pixel(
                pattern,
                logical_x,
                logical_y
            );
        }
    }
}

static void commit_lower_state(struct fixture *fixture, int buffer_index, int state)
{
    if (fixture->lower_destroyed || fixture->lower_surface == NULL) {
        fail_message("cannot update a destroyed lower surface");
    }
    wl_surface_attach(
        fixture->lower_surface,
        fixture->lower_buffers[buffer_index].buffer,
        0,
        0
    );
    ++fixture->lower_attach_count;
    wl_surface_damage_buffer(
        fixture->lower_surface,
        0,
        0,
        LOWER_BUFFER_WIDTH,
        LOWER_BUFFER_HEIGHT
    );
    wl_surface_commit(fixture->lower_surface);
    ++fixture->lower_commit_count;
    fixture->lower_state = state;
}

static void lower_frame_done(
    void *data,
    struct wl_callback *callback,
    uint32_t callback_data
);

static const struct wl_callback_listener lower_frame_listener = {
    .done = lower_frame_done,
};

static void arm_lower_frame_callback(struct fixture *fixture)
{
    if (fixture->lower_frame_callback != NULL) {
        fail_message("lower frame callback is already armed");
    }
    fixture->lower_frame_callback = wl_surface_frame(fixture->lower_surface);
    if (fixture->lower_frame_callback == NULL) {
        fail_message("cannot create lower frame callback");
    }
    if (
        wl_callback_add_listener(
            fixture->lower_frame_callback,
            &lower_frame_listener,
            fixture
        ) < 0
    ) {
        fail_message("cannot listen for lower frame callback");
    }
}

static void lower_frame_done(
    void *data,
    struct wl_callback *callback,
    uint32_t callback_data
)
{
    struct fixture *fixture = data;
    if (callback != fixture->lower_frame_callback) {
        fail_message("unexpected lower frame callback identity");
    }
    if (fixture->lower_frame_ready) {
        fail_message("lower frame callback was not consumed before the next callback");
    }
    fixture->lower_frame_callback_id = proxy_id(callback);
    fixture->lower_frame_callback_data = callback_data;
    wl_callback_destroy(callback);
    fixture->lower_frame_callback = NULL;
    ++fixture->lower_frame_done_count;
    if (
        fixture->lower_frame_done_count
        > 2 + CONTINUOUS_MAX_GENERATIONS + 1
    ) {
        fail_message("too many lower frame callbacks");
    }
    fixture->lower_frame_ready = true;
}

static void commit_lower_frame_generation(struct fixture *fixture)
{
    const unsigned int generation = fixture->lower_generation_count + 1;
    if (
        !fixture->lower_frame_ready
        || fixture->lower_frame_done_count != generation
        || generation > 2
    ) {
        fail_message("lower frame generation is not callback-ready");
    }
    const unsigned int buffer_index = 3 + generation;
    const int state = 2 + (int) generation;
    arm_lower_frame_callback(fixture);
    commit_lower_state(fixture, (int) buffer_index, state);
    ++fixture->lower_update_count;
    fixture->lower_generation_count = generation;
    emit_lower_frame_generation(
        fixture,
        buffer_index,
        fixture->lower_frame_callback_id,
        fixture->lower_frame_callback_data
    );
    fixture->lower_frame_callback_id = 0;
    fixture->lower_frame_callback_data = 0;
    fixture->lower_frame_ready = false;
}

static void commit_lower_continuous_generation(struct fixture *fixture)
{
    const unsigned int generation = fixture->lower_continuous_generation_count + 1;
    if (
        !fixture->lower_continuous_active
        || fixture->lower_continuous_stopped
        || !fixture->lower_frame_ready
        || fixture->lower_frame_done_count != 2 + generation
        || generation > CONTINUOUS_MAX_GENERATIONS
    ) {
        fail_message("continuous lower generation is not callback-ready");
    }
    // A frame callback authorizes another commit, not a network transaction.
    // Keep this independent producer observable without blocking Wayland input
    // or stop markers. Late callbacks restart the interval: never catch up.
    const uint64_t committed_ns = monotonic_ns();
    if (
        fixture->lower_continuous_commit_ns != 0
        && committed_ns - fixture->lower_continuous_commit_ns
            < CONTINUOUS_MIN_INTERVAL_NS
    ) {
        return;
    }
    const unsigned int buffer_index = generation % 2 == 0 ? 5 : 6;
    const int state = generation % 2 == 0 ? 4 : 3;
    const uint32_t callback_id = fixture->lower_frame_callback_id;
    const uint32_t callback_data = fixture->lower_frame_callback_data;
    arm_lower_frame_callback(fixture);
    wl_surface_attach(
        fixture->lower_surface,
        fixture->lower_buffers[buffer_index].buffer,
        0,
        0
    );
    ++fixture->lower_attach_count;
    wl_surface_damage_buffer(
        fixture->lower_surface,
        CONTINUOUS_DAMAGE_X * LOWER_BUFFER_SCALE,
        CONTINUOUS_DAMAGE_Y * LOWER_BUFFER_SCALE,
        CONTINUOUS_DAMAGE_WIDTH * LOWER_BUFFER_SCALE,
        CONTINUOUS_DAMAGE_HEIGHT * LOWER_BUFFER_SCALE
    );
    wl_surface_commit(fixture->lower_surface);
    ++fixture->lower_commit_count;
    ++fixture->lower_update_count;
    fixture->lower_state = state;
    fixture->lower_continuous_generation_count = generation;
    fixture->lower_continuous_commit_ns = committed_ns;
    emit_continuous_generation(
        fixture,
        buffer_index,
        callback_id,
        callback_data,
        committed_ns
    );
    fixture->lower_frame_callback_id = 0;
    fixture->lower_frame_callback_data = 0;
    fixture->lower_frame_ready = false;
}

static void stop_lower_continuous_generations(struct fixture *fixture)
{
    if (!fixture->lower_continuous_active || fixture->lower_continuous_stopped) {
        fail_message("continuous lower generations are not active");
    }
    if (
        fixture->lower_continuous_generation_count
        < CONTINUOUS_MIN_GENERATIONS
    ) {
        fail_message("too few continuous lower generations");
    }
    bool terminal_callback_completed = fixture->lower_frame_ready;
    bool pending_callback_cancelled = fixture->lower_frame_callback != NULL;
    uint32_t callback_id = terminal_callback_completed
        ? fixture->lower_frame_callback_id
        : proxy_id(fixture->lower_frame_callback);
    uint32_t callback_data = terminal_callback_completed
        ? fixture->lower_frame_callback_data
        : 0;
    if (terminal_callback_completed == pending_callback_cancelled) {
        fail_message("continuous lower callback terminal state is ambiguous");
    }
    if (pending_callback_cancelled) {
        wl_callback_destroy(fixture->lower_frame_callback);
        fixture->lower_frame_callback = NULL;
    }
    fixture->lower_frame_callback_id = 0;
    fixture->lower_frame_callback_data = 0;
    fixture->lower_frame_ready = false;
    fixture->lower_continuous_active = false;
    fixture->lower_continuous_stopped = true;
    emit_continuous_stop(
        fixture,
        terminal_callback_completed,
        pending_callback_cancelled,
        callback_id,
        callback_data
    );
}

static void commit_parent(struct fixture *fixture, int index)
{
    int width = index == 0 ? PRIMARY_WIDTH : SECONDARY_WIDTH;
    int height = index == 0 ? PRIMARY_HEIGHT : SECONDARY_HEIGHT;
    wl_surface_attach(
        fixture->parent_surfaces[index],
        fixture->parent_buffers[index].buffer,
        0,
        0
    );
    wl_surface_damage_buffer(fixture->parent_surfaces[index], 0, 0, width, height);
    wl_surface_commit(fixture->parent_surfaces[index]);
    fixture->parent_mapped[index] = true;
}

static void maybe_emit_ready(struct fixture *fixture)
{
    if (
        fixture->ready_emitted
        || !fixture->parent_mapped[0]
        || !fixture->parent_mapped[1]
        || fixture->lower_commit_count != 1
    ) {
        return;
    }
    fixture->ready_emitted = true;
    publish_marker(READY_MARKER, "two parents and lower subsurface mapped\n");
    emit_ready(fixture);
}

static void xdg_surface_configure(
    void *data,
    struct xdg_surface *xdg_surface,
    uint32_t serial
)
{
    struct fixture *fixture = data;
    xdg_surface_ack_configure(xdg_surface, serial);
    for (int index = 0; index < 2; ++index) {
        if (fixture->parent_xdg_surfaces[index] != xdg_surface) {
            continue;
        }
        if (!fixture->parent_mapped[index]) {
            commit_parent(fixture, index);
            if (index == 0 && fixture->lower_commit_count == 0) {
                commit_lower_state(fixture, 0, 1);
            }
        }
        maybe_emit_ready(fixture);
        return;
    }
    fail_message("configure arrived for an unknown parent surface");
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
    struct fixture *fixture = data;
    fixture->running = false;
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
        fixture->pointer_surface == fixture->upper_surface
        && button == BTN_LEFT
        && state == WL_POINTER_BUTTON_STATE_RELEASED
        && fixture->click_count == 0
    ) {
        ++fixture->click_count;
        publish_marker(CLICK_MARKER, "upper sibling pointer release received\n");
        emit_sibling_click(fixture);
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
    } else if (strcmp(interface, wl_subcompositor_interface.name) == 0) {
        fixture->subcompositor = wl_registry_bind(
            registry,
            name,
            &wl_subcompositor_interface,
            1
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

static void destroy_pixel_buffer(struct pixel_buffer *buffer)
{
    if (buffer->buffer != NULL) {
        wl_buffer_destroy(buffer->buffer);
        buffer->buffer = NULL;
    }
    if (buffer->pixels != NULL && buffer->pixels != MAP_FAILED) {
        munmap(buffer->pixels, buffer->size);
        buffer->pixels = NULL;
    }
}

static void move_lower(struct fixture *fixture)
{
    wl_subsurface_set_position(
        fixture->lower_subsurface,
        LOWER_MOVED_X,
        LOWER_MOVED_Y
    );
    wl_surface_commit(fixture->parent_surfaces[0]);
    fixture->lower_moved = true;
    emit_lower_moved(fixture);
}

static void create_upper(struct fixture *fixture)
{
    fixture->upper_surface = wl_compositor_create_surface(fixture->compositor);
    if (fixture->upper_surface == NULL) {
        fail_message("cannot create upper sibling surface");
    }
    wl_surface_set_buffer_transform(
        fixture->upper_surface,
        WL_OUTPUT_TRANSFORM_180
    );
    wl_surface_attach(fixture->upper_surface, fixture->upper_buffer.buffer, 0, 0);
    ++fixture->upper_attach_count;
    wl_surface_damage_buffer(fixture->upper_surface, 0, 0, UPPER_WIDTH, UPPER_HEIGHT);
    wl_surface_commit(fixture->upper_surface);
    ++fixture->upper_commit_count;
    if (wl_display_roundtrip(fixture->display) < 0) {
        fail_message("Wayland roundtrip failed after role-less upper commit");
    }
    fixture->upper_precommitted_before_role = true;

    fixture->upper_subsurface = wl_subcompositor_get_subsurface(
        fixture->subcompositor,
        fixture->upper_surface,
        fixture->parent_surfaces[0]
    );
    if (fixture->upper_subsurface == NULL) {
        fail_message("cannot create upper sibling role");
    }
    wl_subsurface_set_position(fixture->upper_subsurface, UPPER_X, UPPER_Y);
    wl_subsurface_set_desync(fixture->upper_subsurface);
    wl_subsurface_place_above(fixture->upper_subsurface, fixture->lower_surface);
    wl_surface_commit(fixture->parent_surfaces[0]);
    fixture->upper_created = true;
    emit_sibling_created(fixture);
}

static void destroy_lower(struct fixture *fixture)
{
    if (fixture->lower_destroyed) {
        fail_message("lower surface was destroyed twice");
    }
    wl_subsurface_destroy(fixture->lower_subsurface);
    fixture->lower_subsurface = NULL;
    wl_surface_destroy(fixture->lower_surface);
    fixture->lower_surface = NULL;
    fixture->lower_destroyed = true;
    emit_lower_destroyed(fixture);
}

static void detach_upper(struct fixture *fixture)
{
    if (fixture->upper_subsurface == NULL || fixture->upper_surface == NULL) {
        fail_message("cannot detach an unavailable upper sibling");
    }
    wl_subsurface_destroy(fixture->upper_subsurface);
    fixture->upper_subsurface = NULL;
    if (wl_display_roundtrip(fixture->display) < 0) {
        fail_message("Wayland roundtrip failed after upper detach");
    }
    fixture->upper_detached = true;
    emit_upper_detached(fixture);
}

static void reparent_upper(struct fixture *fixture)
{
    if (!fixture->upper_detached || fixture->upper_subsurface != NULL) {
        fail_message("upper sibling is not ready for reparenting");
    }
    const unsigned int attach_count = fixture->upper_attach_count;
    const unsigned int commit_count = fixture->upper_commit_count;
    fixture->upper_subsurface = wl_subcompositor_get_subsurface(
        fixture->subcompositor,
        fixture->upper_surface,
        fixture->parent_surfaces[1]
    );
    if (fixture->upper_subsurface == NULL) {
        fail_message("cannot recreate upper sibling role under secondary parent");
    }
    wl_subsurface_set_position(
        fixture->upper_subsurface,
        UPPER_REPARENT_X,
        UPPER_REPARENT_Y
    );
    wl_subsurface_set_desync(fixture->upper_subsurface);
    wl_surface_commit(fixture->parent_surfaces[1]);
    if (wl_display_roundtrip(fixture->display) < 0) {
        fail_message("Wayland roundtrip failed after parent-only upper reattach");
    }
    if (
        fixture->upper_attach_count != attach_count
        || fixture->upper_commit_count != commit_count
    ) {
        fail_message("upper sibling changed its buffer state during reattach");
    }
    fixture->upper_reattach_without_child_commit = true;
    fixture->upper_reparented = true;
    emit_upper_reparented(fixture);
}

static void create_toplevel(
    struct fixture *fixture,
    int index,
    const char *title,
    const char *app_id,
    int width,
    int height
)
{
    fixture->parent_surfaces[index] = wl_compositor_create_surface(fixture->compositor);
    if (fixture->parent_surfaces[index] == NULL) {
        fail_message("cannot create parent Wayland surface");
    }
    fixture->parent_xdg_surfaces[index] = xdg_wm_base_get_xdg_surface(
        fixture->wm_base,
        fixture->parent_surfaces[index]
    );
    if (fixture->parent_xdg_surfaces[index] == NULL) {
        fail_message("cannot create parent XDG surface");
    }
    if (
        xdg_surface_add_listener(
            fixture->parent_xdg_surfaces[index],
            &xdg_surface_listener,
            fixture
        ) < 0
    ) {
        fail_message("cannot listen for parent XDG configure");
    }
    fixture->parent_toplevels[index] = xdg_surface_get_toplevel(
        fixture->parent_xdg_surfaces[index]
    );
    if (fixture->parent_toplevels[index] == NULL) {
        fail_message("cannot create parent XDG toplevel");
    }
    if (
        xdg_toplevel_add_listener(
            fixture->parent_toplevels[index],
            &toplevel_listener,
            fixture
        ) < 0
    ) {
        fail_message("cannot listen for parent toplevel events");
    }
    xdg_toplevel_set_title(fixture->parent_toplevels[index], title);
    xdg_toplevel_set_app_id(fixture->parent_toplevels[index], app_id);
    xdg_toplevel_set_min_size(fixture->parent_toplevels[index], width, height);
    xdg_toplevel_set_max_size(fixture->parent_toplevels[index], width, height);
}

static void create_surfaces(struct fixture *fixture)
{
    create_buffer(fixture, &fixture->parent_buffers[0], PRIMARY_WIDTH,
                  PRIMARY_HEIGHT, PRIMARY_PARENT, 1, WL_OUTPUT_TRANSFORM_NORMAL);
    create_buffer(fixture, &fixture->parent_buffers[1], SECONDARY_WIDTH,
                  SECONDARY_HEIGHT, SECONDARY_PARENT, 1, WL_OUTPUT_TRANSFORM_NORMAL);
    create_buffer(fixture, &fixture->lower_buffers[0], LOWER_WIDTH,
                  LOWER_HEIGHT, LOWER_STATE_ONE, LOWER_BUFFER_SCALE,
                  WL_OUTPUT_TRANSFORM_NORMAL);
    create_buffer(fixture, &fixture->lower_buffers[1], LOWER_WIDTH,
                  LOWER_HEIGHT, LOWER_STATE_TWO, LOWER_BUFFER_SCALE,
                  WL_OUTPUT_TRANSFORM_NORMAL);
    create_buffer(fixture, &fixture->lower_buffers[2], LOWER_WIDTH,
                  LOWER_HEIGHT, LOWER_STATE_ONE, LOWER_BUFFER_SCALE,
                  WL_OUTPUT_TRANSFORM_NORMAL);
    create_buffer(fixture, &fixture->lower_buffers[3], LOWER_WIDTH,
                  LOWER_HEIGHT, LOWER_STATE_TWO, LOWER_BUFFER_SCALE,
                  WL_OUTPUT_TRANSFORM_NORMAL);
    create_buffer(fixture, &fixture->lower_buffers[4], LOWER_WIDTH,
                  LOWER_HEIGHT, LOWER_STATE_THREE, LOWER_BUFFER_SCALE,
                  WL_OUTPUT_TRANSFORM_NORMAL);
    create_buffer(fixture, &fixture->lower_buffers[5], LOWER_WIDTH,
                  LOWER_HEIGHT, LOWER_STATE_FOUR, LOWER_BUFFER_SCALE,
                  WL_OUTPUT_TRANSFORM_NORMAL);
    create_buffer(fixture, &fixture->lower_buffers[6], LOWER_WIDTH,
                  LOWER_HEIGHT, LOWER_CONTINUOUS_ONE, LOWER_BUFFER_SCALE,
                  WL_OUTPUT_TRANSFORM_NORMAL);
    create_buffer(fixture, &fixture->upper_buffer, UPPER_WIDTH,
                  UPPER_HEIGHT, UPPER_STATE, 1, WL_OUTPUT_TRANSFORM_180);

    create_toplevel(fixture, 0, PRIMARY_TITLE,
                    "org.xpra.SubsurfaceStreamFixture",
                    PRIMARY_WIDTH, PRIMARY_HEIGHT);
    create_toplevel(fixture, 1, SECONDARY_TITLE,
                    "org.xpra.SubsurfaceReparentTarget",
                    SECONDARY_WIDTH, SECONDARY_HEIGHT);

    fixture->lower_surface = wl_compositor_create_surface(fixture->compositor);
    if (fixture->lower_surface == NULL) {
        fail_message("cannot create lower sibling surface");
    }
    wl_surface_set_buffer_scale(fixture->lower_surface, LOWER_BUFFER_SCALE);
    fixture->lower_subsurface = wl_subcompositor_get_subsurface(
        fixture->subcompositor,
        fixture->lower_surface,
        fixture->parent_surfaces[0]
    );
    if (fixture->lower_subsurface == NULL) {
        fail_message("cannot create lower sibling role");
    }
    wl_subsurface_set_position(
        fixture->lower_subsurface,
        LOWER_INITIAL_X,
        LOWER_INITIAL_Y
    );
    wl_subsurface_set_desync(fixture->lower_subsurface);
    wl_surface_commit(fixture->parent_surfaces[0]);
    wl_surface_commit(fixture->parent_surfaces[1]);
}

static void destroy_fixture(struct fixture *fixture)
{
    if (fixture->lower_frame_callback != NULL) {
        wl_callback_destroy(fixture->lower_frame_callback);
        fixture->lower_frame_callback = NULL;
    }
    if (fixture->lower_subsurface != NULL) {
        wl_subsurface_destroy(fixture->lower_subsurface);
    }
    if (fixture->lower_surface != NULL) {
        wl_surface_destroy(fixture->lower_surface);
    }
    if (fixture->upper_subsurface != NULL) {
        wl_subsurface_destroy(fixture->upper_subsurface);
    }
    if (fixture->upper_surface != NULL) {
        wl_surface_destroy(fixture->upper_surface);
    }
    for (int index = 0; index < 2; ++index) {
        if (fixture->parent_toplevels[index] != NULL) {
            xdg_toplevel_destroy(fixture->parent_toplevels[index]);
        }
        if (fixture->parent_xdg_surfaces[index] != NULL) {
            xdg_surface_destroy(fixture->parent_xdg_surfaces[index]);
        }
        if (fixture->parent_surfaces[index] != NULL) {
            wl_surface_destroy(fixture->parent_surfaces[index]);
        }
        destroy_pixel_buffer(&fixture->parent_buffers[index]);
    }
    for (size_t index = 0;
         index < sizeof(fixture->lower_buffers) / sizeof(fixture->lower_buffers[0]);
         ++index) {
        destroy_pixel_buffer(&fixture->lower_buffers[index]);
    }
    destroy_pixel_buffer(&fixture->upper_buffer);
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
    if (fixture->subcompositor != NULL) {
        wl_subcompositor_destroy(fixture->subcompositor);
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
    const char *markers[] = {
        READY_MARKER,
        UPDATE_MARKER,
        RESTORE_MARKER,
        MOVE_MARKER,
        STACK_MARKER,
        LOWER_UPDATE_MARKER,
        FRAME_GENERATION_ONE_MARKER,
        FRAME_GENERATION_TWO_MARKER,
        CONTINUOUS_START_MARKER,
        CONTINUOUS_STOP_MARKER,
        CLICK_MARKER,
        DESTROY_LOWER_MARKER,
        DETACH_UPPER_MARKER,
        REPARENT_UPPER_MARKER,
        EXIT_MARKER,
    };
    for (size_t index = 0; index < sizeof(markers) / sizeof(markers[0]); ++index) {
        clear_marker(markers[index]);
    }

    struct fixture fixture = {
        .lower_state = 1,
        .running = true,
    };
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
    if (
        wl_display_roundtrip(fixture.display) < 0
        || wl_display_roundtrip(fixture.display) < 0
    ) {
        fail_message("cannot enumerate Wayland globals");
    }
    if (
        fixture.compositor == NULL
        || fixture.subcompositor == NULL
        || fixture.shm == NULL
        || fixture.wm_base == NULL
        || fixture.seat == NULL
        || fixture.pointer == NULL
    ) {
        fail_message("required Wayland globals are unavailable");
    }
    create_surfaces(&fixture);

    const int display_descriptor = wl_display_get_fd(fixture.display);
    while (fixture.running) {
        if (
            fixture.ready_emitted
            && fixture.lower_update_count == 0
            && consume_marker(UPDATE_MARKER)
        ) {
            commit_lower_state(&fixture, 1, 2);
            fixture.lower_update_count = 1;
            emit_lower_state(&fixture, "lower-state", 1);
        }
        if (
            fixture.lower_update_count == 1
            && consume_marker(RESTORE_MARKER)
        ) {
            commit_lower_state(&fixture, 2, 1);
            fixture.lower_update_count = 2;
            emit_lower_state(&fixture, "lower-state", 2);
        }
        if (
            fixture.lower_update_count == 2
            && !fixture.lower_moved
            && consume_marker(MOVE_MARKER)
        ) {
            move_lower(&fixture);
        }
        if (
            fixture.lower_moved
            && !fixture.upper_created
            && consume_marker(STACK_MARKER)
        ) {
            create_upper(&fixture);
        }
        if (
            fixture.upper_created
            && fixture.lower_update_count == 2
            && consume_marker(LOWER_UPDATE_MARKER)
        ) {
            arm_lower_frame_callback(&fixture);
            commit_lower_state(&fixture, 3, 2);
            fixture.lower_update_count = 3;
            emit_lower_state(&fixture, "lower-updated-under-upper", 3);
        }
        if (
            fixture.lower_update_count == 3
            && fixture.lower_frame_ready
            && consume_marker(FRAME_GENERATION_ONE_MARKER)
        ) {
            commit_lower_frame_generation(&fixture);
        }
        if (
            fixture.lower_update_count == 4
            && fixture.lower_frame_ready
            && consume_marker(FRAME_GENERATION_TWO_MARKER)
        ) {
            commit_lower_frame_generation(&fixture);
        }
        if (
            fixture.lower_update_count == 5
            && !fixture.lower_continuous_active
            && !fixture.lower_continuous_stopped
            && consume_marker(CONTINUOUS_START_MARKER)
        ) {
            fixture.lower_continuous_active = true;
            emit_continuous_start(&fixture);
        }
        if (
            fixture.lower_continuous_active
            && consume_marker(CONTINUOUS_STOP_MARKER)
        ) {
            stop_lower_continuous_generations(&fixture);
        } else if (
            fixture.lower_continuous_active
            && fixture.lower_frame_ready
            && fixture.lower_continuous_generation_count
                < CONTINUOUS_MAX_GENERATIONS
        ) {
            commit_lower_continuous_generation(&fixture);
        }
        if (
            fixture.lower_continuous_stopped
            && fixture.click_count == 1
            && !fixture.lower_destroyed
            && consume_marker(DESTROY_LOWER_MARKER)
        ) {
            destroy_lower(&fixture);
        }
        if (
            fixture.lower_destroyed
            && !fixture.upper_detached
            && consume_marker(DETACH_UPPER_MARKER)
        ) {
            detach_upper(&fixture);
        }
        if (
            fixture.upper_detached
            && !fixture.upper_reparented
            && consume_marker(REPARENT_UPPER_MARKER)
        ) {
            reparent_upper(&fixture);
        }
        if (fixture.upper_reparented && consume_marker(EXIT_MARKER)) {
            fixture.running = false;
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
        int ready = poll(&descriptor, 1, 25);
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
    emit_exit(&fixture);
    destroy_fixture(&fixture);
    return EXIT_SUCCESS;
}
