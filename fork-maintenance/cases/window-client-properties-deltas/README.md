# Preserve omitted state when applying client property deltas

Code: the `Fork-Case: window-client-properties-deltas` commit on `develop`
(`make -C fork-maintenance case-show CASE=window-client-properties-deltas`).

## Boundary

An existing window receives a partial client-property dictionary, for example
`{"encoding.full_csc_modes": {}}` with a buffer refresh. The server must replace
the supplied CSC modes without changing omitted properties. Current code instead
resets maximized state to false, render size to `(0, 0)`, and an explicitly
disabled video subregion to true. This loses unrelated negotiated state during
an ordinary capability update. Initial values and explicit replacements are
not the defect; treating a later delta as a new complete snapshot is.

## Embedded-source context

The embedded `WindowSource.do_set_client_properties` already retains the old
value for bit depth, refresh encodings, transparency, encoder lists and RGB
formats when a key is absent. `parse_csc_modes` likewise changes its state only
when a dictionary is supplied, accepting an explicitly empty dictionary as a
real replacement. The three exceptional defaults contradict that existing
delta contract. `typedict.boolget` and `intpair` already support an explicit
default; no new parsing mechanism is necessary.

This is a separate correction from decoder retirement and display lifetime.
The maintainer's [review requesting separation of unrelated CSC and window
changes][scope-review] informs that scope: changing native VA ownership must
not incidentally absorb a server property-parser repair. The current neighboring
setters provide the concrete implementation precedent. Prior feedback is not
claimed as upstream approval of this fix. Keep the case until upstream retains
all three omitted values while still applying explicit false/zero values.

## Surrounding code and ownership map

The UI-dispatched configure, map and refresh handlers in
`xpra.server.subsystem.window` pass supplied client properties through
`_set_client_properties`. `WindowsConnection.set_client_properties` applies
them to the owning window source and propagates backing properties to its
subsurface sources. It does not reconstruct a complete client snapshot.
`WindowSource.set_client_properties` drops workspace/screen-only input, then
dispatches non-empty deltas to `do_set_client_properties`.

The base source owns maximized state and render size. The video source owns
the video-subregion admission flag and delegates common parsing to the base.
Encoder selection is recalculated after property application. Render size
affects scaling/encoding thresholds; maximized state affects video-region
selection. Re-enabling a disabled subregion also changes that policy. These
fields are not harmless diagnostic defaults.

## Mechanism and lifecycle

The base parser now passes `self.maximized` and `self.client_render_size` as
the getters' defaults. The video parser uses the existing subregion flag as
its default when that feature is enabled. Each getter still validates and
converts a supplied value through the ordinary typed dictionary API. Explicit
false, `(0, 0)` or a different size, and explicit subregion enable/disable
remain valid updates. An explicitly empty CSC dictionary still clears CSC
modes and triggers the existing selection update.

No state or lifetime is added. Constructors still establish initial defaults;
subsequent deltas preserve them until explicitly replaced. Workspace-only
input still avoids expensive selection work. The existing video-subregion
feature gate remains in place, and the patch does not instantiate a disabled
feature. The production route is serialized UI property handling, not a new
worker or lock boundary; encoder teardown and queued work remain with their
existing owners.

## Case-commit and integration ownership

The product commit owns the three getter-default changes in `compress.py` and
`video_compress.py`, plus `unit.server.window.client_properties_test`. It has
no dependencies: the behavior exists on the embedded base and does not need
the libva or setup-switch cases. The timer and video-cleanup cases touch the
same source classes but own scheduling and native-resource cleanup, not delta
parsing. The Wayland cases own frame/stream and backing transactions; this
case preserves their property propagation instead of changing their protocol.

Runtime decoder-loss notification is a distinct consumer of the corrected
partial-update boundary. It must not compensate for server defaults by
inventing a complete client window-state snapshot in decoder code.

## Commit scope and non-goals

No packet shape, new property, feature default, encoding preference, codec,
timer, platform gate or package dependency changes. In particular an empty
CSC update is not ignored, and a missing key is not confused with a supplied
false value. This is not an implementation of decoder-loss notification,
generic capability renegotiation or recovery from malformed bitstreams.

## Regression design and clean control

`unit.server.window.client_properties_test` uses the actual base/video
`set_client_properties` and parsing implementations on initialized source
state. Only the subsequent encoding-selection calculations are substituted
with a mock: the tests isolate dictionary application, not video scoring.
There is no native window or GPU, and the video-subregion feature is explicitly
enabled for its subject assertions rather than silently skipped.

Three independent tests send only a CSC delta and check maximized state,
render size and disabled subregion respectively. On tests-only embedded
production each fails at its specific old-value assertion, not on an absent
API. The maximized test additionally verifies that empty CSC modes really
replace the old modes and selection is invoked. A fourth control supplies
explicit values and proves they still replace state. The adjacent upstream
`compress_test` and `video_compress_test` modules challenge the consumers.

These tests do not prove wire transport, native window management or live
encoder scheduling; mock selection success is not fallback evidence. They
also do not add a native Windows display test. The complete live composition
retains those endpoint and lifecycle obligations.

## Durable live or package boundary

All live endpoints contain the entire queue. Existing map/configure and
buffer-refresh traffic exercises the shared property route across RGB, video,
subsurface and window lifecycle profiles; all nine profiles remain required.
A decoder-loss scenario must separately verify its actual CSC delta and
server fallback, not infer either from these parsing assertions. Live success
without that stimulus does not establish notification behavior. This Python
state correction introduces no new native payload or package split.

## Invariants not to simplify

- Constructor defaults initialize state; they are not defaults for later deltas.
- Keep explicit false and zero-size updates distinct from omitted keys.
- Keep explicitly empty CSC dictionaries meaningful.
- Preserve the video-subregion feature gate and its current admission flag.
- Leave selection and native encoder cleanup with their existing owners.

## Required validation

Follow [canonical validation](../../docs/runbooks/validation.md): tests-only
negative and patched focused property/consumer tests, composed focused checks,
all three final upstream modes, full fork-control checks and the complete
nine-profile live loop. Compiled/no-compat execution challenges the same
shared source paths; no new compatibility protocol is introduced. Ruff uses
the embedded configuration. This patch does not expand the separate scoped
mypy module inventory. Transient pass/failure results belong in the session
ledger, not this design description.

[scope-review]: https://github.com/Xpra-org/xpra/pull/5023#discussion_r3861406986
