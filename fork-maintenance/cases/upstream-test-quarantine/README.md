# Upstream test quarantine

This is the single duty case for upstream unit-test modules that are known to
be non-green on the fork's frozen Ubuntu 26.04 matrix. It owns test disabling
only; production behavior must never be changed here.

At the historical embedded-source boundary
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
upstream rebase, run all three clean `quarantine*` gates.

At current embedded source `212038243d0067b6860ebe7d6953692179ef353f`,
upstream commit
[`1ca1155cd48f`](https://github.com/Xpra-org/xpra/commit/1ca1155cd48f0dd336e0166166ad4983da135324)
added recursive blob extraction to `xpra/client/base/record.py`.
`WindowModel.extract_blobs`
annotates its argument as builtin `dict`, but window creation passes nested
`typedict` metadata back into that method. In the Cython-heavy build, Cython's
annotation typing enforces an exact builtin dictionary and the recursive call
raises `TypeError`; 19 of the module's 24 tests error at the common window-create
boundary. When `record.py` remains Python, the subclass is accepted and the
same module stays green. No production case in the queue changes this recorder
path, so it belongs to the upstream-test duty quarantine rather than to a
Wayland production patch.

The recorder quarantine imports `importlib.machinery.EXTENSION_SUFFIXES` and
skips `RecordClientTest` only when `xpra.client.base.record.__file__` ends in a
real extension-module suffix. It therefore disables the current failure in
`quarantine-cython` and `full-cython`, while baseline and no-compat continue to
execute the upstream tests. The existing unconditional X11 client-paint class
skip is unchanged because that module remains assigned to all three legs.
An unskipped boundary test in the same module proves that a `.py` implementation
does not trigger the quarantine, that every interpreter-declared extension
suffix does, and that the live class skip flag matches the loaded module form.
Keep that test outside `RecordClientTest`: placing it inside the quarantined
class would make an accidentally over-broad decorator self-validating through
skip rather than failing the standard or Cython-heavy leg.

`[quarantine].modules` is the ordered union of both changed modules;
`[quarantine.gates]` records the exact per-leg expected-failure subsets. Every
clean gate runs the whole union, ignores only its own subset, and requires all
remaining modules to pass with no skipped or unignored failure. A green
assigned module makes only that assignment stale; remove the patch path and
union entry only when no leg still needs it. A newly failing complement is a
separate admission decision. Refresh this one case through the documented
atomic `ALLOW_PATH_CHANGE=1` workspace transition before accepting the stack.
