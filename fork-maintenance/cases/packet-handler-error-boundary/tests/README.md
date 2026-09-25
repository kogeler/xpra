The case commit owns `unit.net.packet_handler_error_test`. It imports the real
GLib bindings and Xpra dispatch/client/server code; absence of a subject
dependency is a failure, not a skip. Run it through the manifest-driven focused
modes. There is no isolated-case live fixture: all live validation uses the
complete `develop` stack and mandatory nine-profile suite.
