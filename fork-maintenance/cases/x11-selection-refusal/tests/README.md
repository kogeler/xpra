The retained regression is `unit.x11.selection_refusal_test`, introduced by
`fix.patch`. It uses a private Xvfb display, actual compiled X11/GDK bindings,
GLib delivery and a separate raw Xlib selection owner. No external probe or
operator clipboard is required. See the case README for the negative control
and complete-stack acceptance boundary.
