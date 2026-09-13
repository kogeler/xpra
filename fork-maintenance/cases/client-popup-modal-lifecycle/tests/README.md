# Regression Ownership

The case's executable regressions are carried by `../fix.patch` at
`tests/unittests/unit/client/subsystem/popup_modal_test.py`, alongside the
selected upstream `unit.client.subsystem.window_test` control. They run from
the applied source tree, not from this directory.

The [case README](../README.md#regression-design-and-clean-control) defines the
native GTK fixture, independent clean-source failure oracles, cleanup and
remaining live boundary. There are no additional case-local functional probes.
Use the manifest-selected named runs and complete-stack live suite described
there; do not create a second copied test implementation here.
