The case commit owns `test_first_x11_scroll_step` in the upstream module
`unit.client.gtk3.client_window_test` and the `connect` stub which the bare
`PointerWindow()` of `unit.client.opengl_backing_test` needs. Complete-stack
Sway/Xwayland live input in the hardware profiles supplies the real X11
valuator stimulus; it is always run as part of all nine live profiles, never
in isolation.
