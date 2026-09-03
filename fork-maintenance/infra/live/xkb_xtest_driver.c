/* Copyright (C) 2026 kogeler */

#define _POSIX_C_SOURCE 200809L

#include <X11/XKBlib.h>
#include <X11/Xlib.h>
#include <X11/extensions/XTest.h>

#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

static unsigned long parse_number(const char *value, const char *name,
                                  unsigned long maximum) {
    char *end = NULL;
    errno = 0;
    const unsigned long result = strtoul(value, &end, 0);
    if (errno || !end || *end || result > maximum) {
        fprintf(stderr, "invalid %s: %s\n", name, value);
        exit(2);
    }
    return result;
}

int main(int argc, char **argv) {
    if (argc != 5) {
        fprintf(stderr, "usage: %s DISPLAY WINDOW GROUP KEYCODE\n", argv[0]);
        return 2;
    }
    const char *display_name = argv[1];
    const Window window = (Window)parse_number(argv[2], "window", ULONG_MAX);
    const unsigned int group =
        (unsigned int)parse_number(argv[3], "group", XkbNumKbdGroups - 1);
    const unsigned int keycode =
        (unsigned int)parse_number(argv[4], "keycode", 255);
    if (!window || keycode < 8) {
        fprintf(stderr, "window and keycode must be positive X11 values\n");
        return 2;
    }

    Display *display = XOpenDisplay(display_name);
    if (!display) {
        fprintf(stderr, "could not open X11 display %s\n", display_name);
        return 3;
    }
    int event_base = 0;
    int error_base = 0;
    int major = 0;
    int minor = 0;
    if (!XTestQueryExtension(display, &event_base, &error_base, &major, &minor)) {
        fprintf(stderr, "XTEST is unavailable\n");
        XCloseDisplay(display);
        return 4;
    }
    int xkb_opcode = 0;
    int xkb_event = 0;
    int xkb_error = 0;
    int xkb_major = XkbMajorVersion;
    int xkb_minor = XkbMinorVersion;
    if (!XkbQueryExtension(display, &xkb_opcode, &xkb_event, &xkb_error,
                           &xkb_major, &xkb_minor)) {
        fprintf(stderr, "XKB is unavailable\n");
        XCloseDisplay(display);
        return 5;
    }

    XRaiseWindow(display, window);
    XSetInputFocus(display, window, RevertToParent, CurrentTime);
    if (!XkbLockGroup(display, XkbUseCoreKbd, group)) {
        fprintf(stderr, "could not lock XKB group %u\n", group);
        XCloseDisplay(display);
        return 6;
    }
    XSync(display, False);

    Window focused = None;
    int revert_to = 0;
    XGetInputFocus(display, &focused, &revert_to);
    XkbStateRec before;
    memset(&before, 0, sizeof(before));
    if (XkbGetState(display, XkbUseCoreKbd, &before) != Success ||
        before.group != group || focused != window) {
        fprintf(stderr, "X11 focus or XKB group did not settle\n");
        XCloseDisplay(display);
        return 7;
    }

    const KeySym keysym = XkbKeycodeToKeysym(display, (KeyCode)keycode, group, 0);
    const char *keysym_name = XKeysymToString(keysym);
    if (keysym == NoSymbol || !keysym_name) {
        fprintf(stderr, "physical key has no symbol in XKB group %u\n", group);
        XCloseDisplay(display);
        return 8;
    }
    if (!XTestFakeKeyEvent(display, keycode, True, CurrentTime)) {
        fprintf(stderr, "XTEST key press failed\n");
        XCloseDisplay(display);
        return 9;
    }
    XSync(display, False);
    const struct timespec key_interval = {.tv_sec = 0, .tv_nsec = 50000000};
    if (nanosleep(&key_interval, NULL) != 0) {
        fprintf(stderr, "could not wait between XTEST key events\n");
        XCloseDisplay(display);
        return 10;
    }
    if (!XTestFakeKeyEvent(display, keycode, False, CurrentTime)) {
        fprintf(stderr, "XTEST key release failed\n");
        XCloseDisplay(display);
        return 11;
    }
    XSync(display, False);

    XkbStateRec after;
    memset(&after, 0, sizeof(after));
    if (XkbGetState(display, XkbUseCoreKbd, &after) != Success ||
        after.group != group) {
        fprintf(stderr, "XKB group changed during injection\n");
        XCloseDisplay(display);
        return 12;
    }
    printf(
        "{\"schema\":1,\"display\":\"%s\",\"window\":%lu,"
        "\"group_requested\":%u,\"group_before\":%u,\"group_after\":%u,"
        "\"physical_keycode\":%u,\"keysym\":%lu,\"keysym_name\":\"%s\","
        "\"focus_before\":%lu,\"press\":true,\"release\":true}\n",
        display_name, (unsigned long)window, group, before.group, after.group,
        keycode, (unsigned long)keysym, keysym_name, (unsigned long)focused);
    fflush(stdout);
    XCloseDisplay(display);
    return 0;
}
