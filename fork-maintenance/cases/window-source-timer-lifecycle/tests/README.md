# Generic timer regression ownership

The regression is exported in `../fix.patch` as
`tests/unittests/unit/server/window/compress_test.py`; this directory does not
hold a second copy. `../case.toml` declares `unit.server.window.compress_test`
and the three upstream unit-test legs. The main case README describes the
lease state machine and surrounding cleanup owners.

The focused module preserves the upstream selector tests and exercises the
real generic timer methods, dynamic connection construction, and controlled
thread interleavings. It covers pending source publication, cancellation and
replacement, active callbacks, nested callback-requested cleanup, retryable
packet unregistration, exception ordering, the inherited video expiry slot,
and icon-to-encode-queue handoff. Real GLib tests additionally dispatch actual
sources before and after publication: fake registries cannot prove safe
destruction of an already-completed native source.

Retain a non-vacuous tests-only clean-source control, run the complete patched
standalone module, then repeat it through the resolved `stacks/develop` queue.
Fixtures must initialize the actual connection owners reached by the composed
path; copying a partial set of locks or queue fields does not establish the
connection lifecycle.

There is no atomic live gate for this case. The seven complete-stack profiles
exercise real shutdown, detach, transport loss, rendering, and video, but do
not replace the focused publication and callback interleavings. Video-only
sources belong to VPC; connection-owned composite idles and watchdogs belong
to WSSO. Neither registry is a generic timer fixture here.
