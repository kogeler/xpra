/* This file is part of Xpra.
 * Copyright (C) 2026 kogeler
 * Xpra is released under the terms of the GNU GPL v2, or, at your option, any
 * later version. See the file COPYING for details.
 * Deterministic VA driver boundary for the real va_decode.c lifecycle tests.
 * Device open/close remain real syscalls, including under the real filter. */
#include <assert.h>
#include <fcntl.h>
#include <stdint.h>
#include <va/va.h>
#include <va/va_drm.h>

struct Display {
    int fd;
    int alive;
    int resources;
};
static struct Display displays[32];
static unsigned int opened, terminated, next_id = 1;
static int failure;

static struct Display *live(VADisplay display) {
    struct Display *d = display;
    assert(d >= displays && d < displays + opened);
    assert(d->alive);
    assert(fcntl(d->fd, F_GETFD) >= 0);
    return d;
}

unsigned int test_opened(void) { return opened; }
unsigned int test_terminated(void) { return terminated; }
void test_failure(int stage) { failure = stage; }

VADisplay vaGetDisplayDRM(int fd) {
    assert(opened < 32);
    struct Display *d = &displays[opened++];
    d->fd = fd;
    d->alive = 1;
    return d;
}

VAStatus vaInitialize(VADisplay display, int *major, int *minor) {
    live(display);
    *major = 1;
    *minor = 23;
    return VA_STATUS_SUCCESS;
}

const char *vaQueryVendorString(VADisplay display) {
    live(display);
    return "Xpra lifecycle test driver (not hardware decode evidence)";
}

VAStatus vaTerminate(VADisplay display) {
    struct Display *d = live(display);
    assert(d->resources == 0);
    d->alive = 0;
    terminated++;
    return VA_STATUS_SUCCESS;
}

VAStatus vaQueryConfigProfiles(VADisplay display, VAProfile *profiles, int *count) {
    live(display);
    profiles[0] = VAProfileH264High;
    profiles[1] = VAProfileVP9Profile0;
    *count = failure == 4 ? 0 : 2;
    return VA_STATUS_SUCCESS;
}

VAStatus vaQueryConfigEntrypoints(VADisplay display, VAProfile profile,
                                 VAEntrypoint *entrypoints, int *count) {
    (void)profile;
    live(display);
    entrypoints[0] = VAEntrypointVLD;
    *count = 1;
    return VA_STATUS_SUCCESS;
}

VAStatus vaGetConfigAttributes(VADisplay display, VAProfile profile,
                              VAEntrypoint entrypoint, VAConfigAttrib *attrs, int count) {
    (void)profile;
    assert(entrypoint == VAEntrypointVLD && count == 1);
    live(display);
    attrs[0].value = VA_RT_FORMAT_YUV420;
    return VA_STATUS_SUCCESS;
}

VAStatus vaCreateConfig(VADisplay display, VAProfile profile, VAEntrypoint entrypoint,
                       VAConfigAttrib *attrs, int count, VAConfigID *config) {
    (void)profile;
    (void)attrs;
    assert(entrypoint == VAEntrypointVLD && count == 1);
    struct Display *d = live(display);
    if (failure == 1)
        return VA_STATUS_ERROR_OPERATION_FAILED;
    d->resources++;
    *config = next_id++;
    return VA_STATUS_SUCCESS;
}

VAStatus vaDestroyConfig(VADisplay display, VAConfigID config) {
    assert(config != VA_INVALID_ID);
    live(display)->resources--;
    return VA_STATUS_SUCCESS;
}

VAStatus vaQuerySurfaceAttributes(VADisplay display, VAConfigID config,
                                 VASurfaceAttrib *attrs, unsigned int *count) {
    (void)config;
    (void)attrs;
    live(display);
    *count = 0;
    return VA_STATUS_SUCCESS;
}

VAStatus vaCreateSurfaces(VADisplay display, unsigned int format,
                        unsigned int width, unsigned int height,
                        VASurfaceID *surfaces, unsigned int count,
                        VASurfaceAttrib *attrs, unsigned int nattrs) {
    (void)attrs;
    assert(format == VA_RT_FORMAT_YUV420 && width && height && nattrs == 2);
    struct Display *d = live(display);
    if (failure == 2)
        return VA_STATUS_ERROR_OPERATION_FAILED;
    d->resources += count;
    for (unsigned int i = 0; i < count; i++)
        surfaces[i] = next_id++;
    return VA_STATUS_SUCCESS;
}

VAStatus vaDestroySurfaces(VADisplay display, VASurfaceID *surfaces, int count) {
    (void)surfaces;
    live(display)->resources -= count;
    return VA_STATUS_SUCCESS;
}

VAStatus vaCreateContext(VADisplay display, VAConfigID config, int width, int height,
                        int flags, VASurfaceID *surfaces, int count, VAContextID *context) {
    (void)config;
    (void)surfaces;
    assert(width && height && flags == VA_PROGRESSIVE && count == 4);
    struct Display *d = live(display);
    if (failure == 3)
        return VA_STATUS_ERROR_OPERATION_FAILED;
    d->resources++;
    *context = next_id++;
    return VA_STATUS_SUCCESS;
}

VAStatus vaDestroyContext(VADisplay display, VAContextID context) {
    assert(context != VA_INVALID_ID);
    live(display)->resources--;
    return VA_STATUS_SUCCESS;
}
