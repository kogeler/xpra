# Upstream test quarantine

This is the single duty case for upstream unit-test modules that are known to
be non-green on the fork's frozen Ubuntu 26.04 matrix. It owns test disabling
only; production behavior must never be changed here.

At the last recorded embedded-source reassessment,
`dbc575b063abbc6df314a63ee530fdb218776327`,
`unit.client.x11_client_paint_test` was non-green in all three local matrix
legs. Baseline and no-backwards-compatibility failed `test_colors_png` and
`test_xterm_position_and_color`. The Cython-heavy leg errored in all four test
methods because the spawned server reported no valid encodings. Upstream commit
`a0e6b3935fe6` independently added this whole module to the canonical
`--skip-fail` list. This historical rationale is not current acceptance
evidence.

The patch marks the whole class skipped because the failure boundary changes
between matrix legs. Before applying this case after every operator-selected
upstream rebase, run all three clean `quarantine*` gates. Each gate succeeds only when every
listed module is still an exact ignored failure. If any module passes, remove
or narrow its quarantine entry and refresh this one case before accepting the
stack.
