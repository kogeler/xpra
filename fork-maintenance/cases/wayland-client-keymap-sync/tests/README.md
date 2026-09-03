# Keyboard synchronization live scenario

Case-owned files here contain only versioned scenario data and native probe
sources. Durable image wiring, execution, evidence validation, and lifecycle
logic belong in `fork-maintenance/infra/live`.

The scenario first uses representative `us,fr,ru,ara` data at XKB's four-group
maximum, then replaces it with `ge,am,us,fr` while retaining one unchanged X11
physical keycode and connection. This covers Latin, Cyrillic, Arabic, Georgian,
and Armenian Unicode output. Those values are fixtures, never production constants or runner
branches. The server-side
native-Wayland editable widget records only its real ordered UTF-8 buffer
changes and is not given expected strings. The client-side driver configures
the real X11 display, locks actual XKB groups, and injects complete XTEST key
pairs; it must not construct Xpra packets, paste, or inject Unicode directly.
