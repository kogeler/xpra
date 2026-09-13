# Client Popup Modal Lifecycle

## Boundary

With `--modal-windows=yes`, creating an override-redirect window after any
existing client window can raise a false duplicate-ID error. The minimal
sequence is an existing window 3 followed by a valid `window-create` packet
for a distinct window 4 carrying `override-redirect=True`. The client should
register window 4 independently and temporarily relax existing modal windows
so the popup can receive input. Instead, `_process_new_common` overwrites its
incoming ID while inspecting the window table and reports:

```text
ValueError: we already have a window 0x3
```

The existing window need not be modal. The loop assigns its key before testing
whether the object is ordinary, override-redirect or a tray. Consequently all
nonempty window tables expose the ID overwrite when this branch is enabled.
An initially empty table or disabled modal-window support bypasses the defect.

There is a second error in the same suspend/restore lifecycle: after the last
popup closes, the client sets modality on that closing popup rather than on
the ordinary windows whose metadata requests it. Those windows remain
nonmodal. This case corrects the two identity mistakes while retaining the
existing feature policy, duplicate check and window lifecycle.

A traceback alone does not retain the incoming ID or metadata. This mechanism
explains the reported signature and is independently reproducible, but does
not prove that every duplicate-ID report is false. Genuine duplicates remain
invalid and must still be rejected.

## Embedded-source context

The case targets source `d95058b0916913fe6ae5296fb702f66d833898b0`, embedded
in current `develop`. Upstream
[`f1cd65e488e5`](https://github.com/Xpra-org/xpra/commit/f1cd65e488e5b817671e4c696ac4b8338f36f5c2)
moved override-redirect handling into the common create path as packet names
became prefixed. That change introduced the loop before the existing
uniqueness check. Subsequent file/subsystem moves preserved the faulty local
variable use; replacing the assertion with `ValueError` did not create it.

The entire window-manager source is unchanged between the previous embedded
base `212038243d0067b6860ebe7d6953692179ef353f` and this base. No other active
patch changes the suspension loop or restoration method. The current-source
decision is to add this independent production fix, not repair an adaptation
of another case or weaken its error reporting.

Elsewindow
[`d6d15a33d9d9`](https://github.com/kogeler/elsewindow/commit/d6d15a33d9d9dc3a1f19ec02e8fd2b1fabf858f6)
enabled `--modal-windows=yes` in its normal client GUI options. Xpra's default
in `xpra/scripts/config.py` is false. Enabling a supported option exposes the
old defect; it is not an invalid application request. Disabling modality in
the caller would conceal the branch and change intended GUI behavior.

Future retirement must establish both independent invariants in current
upstream source: popup creation retains the packet's identity through table
inspection, and final popup destruction restores the eligible ordinary
windows, not the closing object. A new name for the packet, a moved method,
or a quieter traceback is not equivalent replacement.

## Surrounding code and ownership map

| Component | Responsibility |
| --- | --- |
| `xpra/wayland/server/subsystem/window.py` | Builds popup metadata with parent, relative position and override-redirect state; registers the compositor's popup ID and emits `window-create`. |
| `xpra/server/subsystem/window.py` and `xpra/server/source/window.py` | Own server-side registration and per-client window announcement, preserving the selected window ID and metadata. |
| `xpra/net/common.py::Packet` | Provides typed field access; this case does not change parsing or wire layout. |
| `xpra/client/base/client.py` and `xpra/net/dispatch.py` | Resolve and schedule client packet handlers; exception containment does not repair window state. |
| `xpra/client/subsystem/window/manager.py` | Owns both client window-ID maps, popup admission, temporary modal suspension and final-popup restoration. |
| `xpra/client/gui/window_base.py` | Retains server metadata and applies metadata updates to the window implementation. |
| `xpra/client/gtk3/window/base.py::set_modal` | Applies native GTK modality subject to the owning window subsystem's `modal_windows` policy. |
| Display subsystem and window factory | Own geometry scaling, native client-window/backing creation and registration after admission. |

The affected route is:

```text
server popup -> window-create(id, geometry, metadata)
  -> client UI handler -> _process_new_common
     -> inspect existing windows, temporarily clear native modality
     -> check incoming ID -> calculate geometry -> make_new_window

window-destroy(id) -> client UI handler -> _process_destroy
  -> if closing an OR window with modal policy enabled:
       check whether another OR window remains
       restore metadata-modal ordinary windows after the final popup
  -> remove both ID-map entries -> destroy the native window
```

These operations run through the client's existing UI lifecycle. The failure
is deterministic variable/object confusion, not a required inter-thread race.
The patch adds no lock, timer, worker, deferred callback or polling loop.

## Incoming identity and modal suspension

`wid` is read from the packet before metadata is cooked. Tray metadata takes
its existing early path; a normal create packet then combines its
override-redirect metadata with the legacy handler's explicit OR argument.

On clean source, `for wid, window in self._id_to_window.items()` rebinds that
same local ID for every existing entry. After any nonempty iteration it is
necessarily already a key in the table, so the duplicate check cannot admit
the otherwise valid popup. Skipping a tray or OR object does not undo Python's
loop-variable assignment. Looking only for modal parents would miss why a
nonmodal existing window also triggers the failure.

The patch uses `existing_wid` for the inspection and its diagnostic message.
The incoming `wid` then remains available to the duplicate check, geometry
diagnostics and `make_new_window`. It neither allocates a replacement ID nor
silently reuses/destroys an existing client window.

The suspension loop retains its original eligibility policy:

| Existing entry | Suspension behavior |
| --- | --- |
| Ordinary window with native modality set | Clear native modality temporarily. |
| Ordinary window already nonmodal | Leave it unchanged. |
| Override-redirect window or tray | Skip modal mutation. |
| No existing entries | Preserve the incoming ID and continue creation. |

Suspension changes native state through `set_modal(False)`, not the retained
`_metadata` request. This separation is necessary for later restoration. The
policy is global to the client table, not newly limited to one popup parent.
The patch does not reinterpret GTK application modality as a different
per-parent modality model.

## Final-popup restoration and destruction

`_process_destroy` looks up the closing object before removing either map
entry. For an OR window with modal support enabled, it first calls
`may_reenable_modal_windows(window)`. That ordering means the closing popup
is still in the table and must be excluded by object identity from the
remaining-OR test.

If any other OR window remains, restoration returns without changing native
modality. Nested popup menus therefore keep ordinary modal windows suspended
until the final OR object closes. There is no separate popup counter that
could drift from the actual table.

After the final popup, the method iterates ordinary, non-tray windows. A
window qualifies when its current `_metadata.boolget("modal")` is true and
its native `get_modal()` is false. Clean source correctly finds that object
as `w`, but then calls `window.set_modal(True)` on the closing popup. The
patch directs that call to `w`. Every eligible ordinary window is restored;
unrelated nonmodal windows and trays remain unchanged.

Both map entries are then deleted by the unchanged destroy handler, followed
by the client's native destruction and stacking notification. The helper does
not remove objects itself, retain a closing popup, or set modality on it.
An unknown/already-removed ID takes the existing no-window path. Ordinary
window destruction does not invoke popup restoration.

## Admission, geometry and failure limits

Modern metadata-based creation and the compatibility-enabled
`new-override-redirect` handler converge on the same fixed method. The legacy
handler remains unavailable with backwards compatibility disabled; the modern
path must work independently in that mode.

With modal support disabled, neither create-time suspension nor destroy-time
restoration is entered. GTK's real window setter additionally gates its native
flag on that policy. The case does not change command-line defaults or the
runtime policy toggle.

Relative popup placement still resolves metadata `parent` separately from the
new window ID, combines `relative-position` with the parent's position, then
uses the display subsystem's scaling helpers. Backing dimensions and optional
client properties continue unchanged to the factory. The fix is not a new
positioning or scaling algorithm.

Genuine duplicate IDs still raise before window creation. Dimension parsing,
the existing invalid-size fallback, tray admission and factory failures keep
their current behavior. In particular, modal suspension still precedes the
duplicate check and factory: this change does not promise transactional
rollback of earlier native mutations after malformed input or a later creation
failure. Exception containment does not provide such rollback either. The
normal valid-popup creation/destruction lifecycle is the repaired boundary.

## Patch-queue and integration ownership

`fix.patch` changes only `xpra/client/subsystem/window/manager.py` and adds
`tests/unittests/unit/client/subsystem/popup_modal_test.py`. Its manifest has
no dependencies: both defects are reachable on clean embedded production
source, and neither corrected method needs an API supplied by another case.

[`wayland-subsurface-stream-ownership`](../wayland-subsurface-stream-ownership/README.md)
also touches the manager and adjacent window tests, but owns surface-tree
composition and its client representation. Its changes do not implement
modal suspension/restoration. The complete stack must preserve both sets of
manager changes and execute the shared upstream window control.

[`packet-handler-error-boundary`](../packet-handler-error-boundary/README.md)
contains and reports exceptions from this route. Suppressed repeat reports
are not successfully created windows. This patch fixes the cause while
preserving that separate reporting boundary and genuine duplicate errors.

The clipboard cases own selection ownership, requests and paste behavior;
[`gtk-client-scroll-deduplication`](../gtk-client-scroll-deduplication/README.md)
owns scroll admission. Their common use of GTK input does not merge those
behaviors with popup identity. The shared live harness owns modal-enabled
CLI configuration and context-menu input; neither belongs in installed
production code or an Elsewindow-specific branch.

## Patch ownership and non-goals

The case owns preserving incoming popup identity and restoring the intended
ordinary windows at the existing final-popup boundary. It does not:

- accept or ignore a genuinely repeated create ID, renumber protocol objects,
  replace an existing native window or change server-side ID allocation;
- disable modal support, alter GTK feature policy or force a popup to be modal;
- change packet names, field validation, tray forwarding, scaling, parent or
  transient-for metadata, backing creation or server window ownership;
- change transport ordering, create retry/rollback behavior, or suppress the
  error boundary's diagnostics; or
- add an application workaround, installed dependency, native extension,
  codec, ABI or packaging change.

## Regression design and clean control

The durable module is `unit.client.subsystem.popup_modal_test`. It feeds real
`Packet` instances into the production create/destroy handlers. Upstream
`DisplayContext` supplies the display, including Xvfb on Linux. A small
`WindowManagerClient` subclass substitutes only the window factory and owning
client peers: it registers realized native `Gtk.Window` objects in both maps,
uses identity display scaling and delegates destruction to GTK.

Native `get_modal`, `set_modal` and `notify::modal` behavior are real, not mock
call counts. The window factory is not the full Xpra GTK/OpenGL backing
constructor, and packets are invoked directly rather than received from a
network connection. Parent-position data and tray classification are fixture
state; no real system-tray embedding is claimed. The disabled-policy test
seeds a modal native flag as a sentinel to prove the handler leaves it alone,
not to model GTKClientWindow's separate feature-gated setter.

The independent controls are:

| Stimulus | Observable invariant and clean-control expectation |
| --- | --- |
| Existing ordinary, modal, OR or tray entry 3; new popup 4 | Both maps retain distinct identities, native popup exists, eligible modality clears, backing size and client properties survive. Clean source falsely rejects ID 3 in each variant. |
| First popup in an empty table; modal policy disabled with an existing window | Creation/destruction preserve the existing bypass behavior; these controls need not fail on clean source. |
| Popup 7 with parent 3 and relative offset | Parent identity survives and the new popup receives the expected relative position. Clean source rejects creation before reaching this assertion. |
| A genuinely duplicated ID 3 | `ValueError` still occurs and no map entry is replaced. This must pass on both clean and patched source. |
| Legacy OR packet, when compatibility is enabled | The same admission fix retains ID 4 and suspends the modal parent. Clean source rejects the distinct popup. |
| Independently registered final popup plus suspended metadata-modal parent | Destroy restores the parent, leaves nonmodal/tray entries unchanged, emits no modal change on the closing popup and removes both mappings. Clean source restores the wrong native object. |
| Two popups and two metadata-modal ordinary windows | First close restores neither parent; final close restores both and leaves only ordinary mappings. Clean source already fails while creating the popups. |

The final-popup test seeds state without the defective create handler. Thus
the first bug cannot mask the second: the tests-only control must expose
both false duplicate admission and wrong-object restoration, not merely an
import failure or an unavailable API. GTK/display or subject-module absence
is a failure. Only the deliberately disabled legacy protocol path has a
compatibility-conditioned skip.

Cleanup destroys every still-registered native window even after an expected
clean failure. Class cleanup destroys remaining toplevels, drains pending GTK
work and collects reference cycles while the display is still valid, then
drains again. This prevents delayed widget finalization from aborting the
negative control after its behavioral assertions. The display context closes its native connection before its
Xvfb process exits, preserving the upstream teardown order. No background
test scheduler or timer approximates modal state. Adjacent
`unit.client.subsystem.window_test` remains selected to protect ordinary
window subsystem behavior and its complete-stack extensions.

## Durable live boundary

The existing `wayland_clipboard_fixture.py` and
`run.py::request_wayland_clipboard_paste` provide the real application route.
The runner opens the remote GTK context menu with Shift+F10 and activates its
Paste item. Native-Wayland popup discovery, server announcement, transport,
client popup creation and real input must all cooperate for that interaction.

The common live client base in `live-cli.yml` now requests
`--modal-windows=yes`, matching the affected caller configuration. Previously
the minimal live client left that option false, so a successful menu paste
could not challenge this branch. Merely repeating the earlier CLI would
leave the central reachability gap open.

The clipboard fixture is not a modal dialog. Its end-to-end paste/input and
lifecycle assertions cover popup creation and use, not native parent-modal
restoration after the final close. The independent GTK regression supplies
that oracle. Conversely, its substituted factory does not prove Xpra backing
creation, transported input, clipboard ownership or visible live behavior.

All nine profiles run the complete current queue on both endpoints, including
the unchanged rendering, hardware, input, detach/transport-loss and cleanup
oracles. The clipboard profile is this case's direct live boundary, not a
waiver of the other eight or permission for a case-only live product.

## Invariants not to simplify

- Keep the packet's `wid` distinct from IDs used to inspect existing entries;
  even a skipped tray/OR iteration assigns its loop key.
- Retain the real duplicate-ID error instead of masking failed popup creation.
- Suspend native modality without clearing the retained metadata request.
- Preserve the global existing-window policy; do not silently reduce it to
  the new popup's immediate parent or change feature defaults.
- Exclude the closing OR object while it remains registered, and defer
  restoration while any other OR window remains.
- Restore each eligible ordinary `w`, never the closing `window`, and keep
  tray/OR exclusions and native-state checks.
- Leave removal of both maps and native destruction with the destroy handler.
- Exercise wrong-object restoration independently of popup creation, and
  retain modal-enabled real live input in addition to native GTK unit state.

## Required validation

Follow [development and final acceptance](../../docs/runbooks/validation.md).
Run the case's tests-only clean control and patched focused modules on the
same frozen source/image, checking the specific native failures above.
Exercise interpreted, Cythonized and no-compat focused modes; the modern
packet regression is required in all three. The native boundary is GTK in
the focused module, not a claim that the separate `unit.wayland` discovery
gate automatically includes a test stored under `unit.client`.

Review and run complete-stack focused composition, including the adjacent
window module, without exporting the stack into this atomic patch. Start
`live-all STACK=develop RUN=<fresh-prefix>` after the nearest focused/native
prerequisites. Final acceptance fills missing current fork-control and three
full upstream legs, and requires all nine profiles through `live-suite-check`.
Reuse exact valid evidence under the canonical flow; these are coverage
obligations, not a demand to repeat full suites after every small edit.

No production packaging or ABI boundary changes here. Package obligations
follow the enclosing validation scope. The scoped mypy gate currently owns
`xpra/server/source/queued_packet.py`, not this window manager; its result
cannot be claimed as type-checking popup or GTK behavior.

Keep source, patch/selection/resolution, image and named-result identities,
actual outcomes and pending gates in the ignored cycle ledger. This README
describes current necessity and durable proof, not an acceptance report for
one incident or run.
