The patch owns `unit.wayland.gtk_scroll_test`: native GDK emulation, real GTK
delivery, pointer serialization, and X11 zero-valuator fallback controls.
Complete-stack Sway/Xwayland live input supplies the independent native X11
stimulus; it is always run as part of all nine live profiles, never in isolation.
