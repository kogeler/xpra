# Propagate runtime video decoder capability loss to the server

Code: the `Fork-Case: video-decoder-capability-loss` commit on `develop`
(`make -C fork-maintenance case-show CASE=video-decoder-capability-loss`).

## Boundary

The client advertises a video encoding, then its last decoder for one input
colorspace permanently fails initialization. `WindowBackingBase` already
retires that entry from the shared `VIDEO_DECODERS` registry, but the server
keeps the old CSC modes and continues sending the encoding. Later frames fail
as `invalid encoding`; error-triggered refreshes do not repair the stale
capabilities. A connection configured with H.264 plus RGB/WebP can therefore
loop instead of using its available picture fallback.

The required transition is a capability update at retirement, not permanent
disabling on every bad frame. A surviving alternative decoder or colorspace
must remain usable. Ordinary corrupted-stream recovery and transient setup
retries retain their existing semantics.

## Embedded-source context

The embedded source already ranks decoder specifications, raises their setup
cost after failures and removes a colorspace when all its candidates are
exhausted. `VideoHelper` regenerates specifications from installed modules,
so its CSC queries do not reflect that registry retirement. Backing capability
advertisements therefore remain stale even after the local dictionary changes.

The existing `window-refresh` packet carries per-window client properties.
`WindowServer._process_refresh` applies those properties before requesting
damage; `WindowVideoSource.update_encoding_selection` excludes video modes
whose CSC list is empty and the base selects an available common encoding.
This is the existing extension point: neither a new packet nor interpretation
of error-message strings is required.

The maintainer's [VA review][va-review] asks about stale decoders outside a
single edited path. Its implication here is to account for the shared registry,
all current backings and future windows, not only the reporting window. His
[picture-fallback review][fallback-review] rejects extensive machinery for
configurations that have no fallback. This case updates truthful capabilities
and uses already-negotiated picture encodings; it does not invent an encoder,
fallback policy, retry framework or support for a video-only configuration
after its only decoder is lost. Keep the case until upstream propagates the
same retirement through existing and newly created windows without disabling
working alternatives.

## Surrounding code and ownership map

| Boundary | Owner and role |
| --- | --- |
| Installed codec specifications | `VideoHelper` describes module capabilities; it does not own runtime backing retirement. |
| Runtime decoder modes | `backing.py:VIDEO_DECODERS` is the existing shared registry; the decode worker removes an exhausted colorspace. |
| Backing-to-client callback | Widget, tray and native Win32 backing creation bind `on_video_decoders_changed` to the owning window subsystem. Standalone backings retain a no-op callback. |
| Current windows and UI scheduling | `WindowManagerClient` schedules one UI update for a retirement and reads its current window registry/backings there. |
| Capability advertisement | Each backing intersects helper CSC modes with remaining decoder colorspaces, then adds its existing picture modes. |
| Server selection | The normal refresh property route reparses CSC modes and recalculates common encodings before refresh damage. |

The GUI widget path covers GTK/Cairo/OpenGL and terminal backings. Tray
creation is separate and must bind the callback too. Win32 GDI and WGL share
`init_backing_props`; both use `backing`, while shared GUI windows use
`_backing`. The manager accommodates those two existing accessors instead of
introducing a new cross-backend window interface. Qt, Tk and pyglet's separate
picture renderers do not consume `WindowBackingBase`'s video registry.

## Mechanism and lifecycle

After the existing candidate loop removes its last decoder for a colorspace,
the backing invokes its callback once at that removal. The callback only queues
work on the existing UI scheduler: it neither sends packets nor touches native
windows while the decode worker owns the decoder lock. No new lock, observer
registry, timer or process-global client reference is introduced.

On UI dispatch the manager iterates current windows and queries each current
backing. A closed/replaced reporting window is not retained as the target.
Windows without a backing, and picture-only/fake backings without CSC
properties, require no update. Each remaining target receives its actual CSC
dictionary through `control_refresh`, with only that property and a refresh
request. The existing server route applies it before choosing refresh data.
Already queued/in-flight video packets may still fail, but they do not generate
another retirement notification. Later selection no longer chooses the lost
mode when a negotiated picture fallback exists.

`_get_full_csc_modes` filters the helper result by the actual surviving
colorspace keys. Empty video lists remain explicit, so they replace stale
server modes. Existing WebP/JPEG/JPH/JPEGA picture additions occur afterward
and are not filtered through a video-only registry. New backings and normal
map properties use the same calculation and cannot re-advertise a lost mode
merely because the helper still knows the installed module.

A failed candidate followed by a working alternative does not remove the
colorspace or notify. A transient initialization failure which succeeds on
retry and ordinary decompression errors preserve existing retry/keyframe
recovery. The case does not change the inherited setup-cost retirement policy
or the lifetime of the pre-existing process-global decoder cache across an
embedded client's reconnect.

## Case-commit and integration ownership

The product commit owns the backing intersection/notification, three existing
backing-creation hooks, the manager's UI update and
`unit.client.video_decoder_loss_test`, plus the terminal-window fixture's new
callback surface. It depends on
`window-client-properties-deltas`: sending a CSC-only update must not reset
unrelated server render size, maximized state or subregion policy. That parser
repair has its own independent commit and regression.

There is no dependency on libva display lifetime. Any exhausted decoder can
reach this path, and a native VA fix does not make GPU/stream failure impossible.
`wayland-subsurface-stream-ownership` owns composition transactions and the
existing propagation of backing properties to child sources, which this case
reuses rather than changing. The timer and video-cleanup cases retain their
scheduling/resource ownership. The change must apply after its declared parser
dependency and remain removable without undoing an unrelated case.

## Commit scope and non-goals

No wire format, encoding preference, server scoring, syscall filter, module
loading, GPU resource owner or bitstream parser changes. The callback carries
no native handle or old backing snapshot. A full `encoding-config` resend
would reconstruct broader connection settings and still need to reconcile
per-window CSC restrictions; it is unnecessary for this window-level boundary.
Disabling an encoding on every decode error would incorrectly turn a corrupt
frame into permanent capability loss. Parsing a human error string would also
put decoder-state ownership in the wrong layer.

## Regression design and clean control

`unit.client.video_decoder_loss_test` calls the real backing factory/selection
path from a joined worker thread with deterministic decoder classes, not real
GPU codecs. A queued UI scheduler captures the handoff; packet sends assert
they occur on the test's UI owner. The real helper continues advertising its
installed specifications, making stale-mode filtering observable rather than
mocking that result away.

The principal test retires H.264, drains the manager's actual UI callback and
feeds its real refresh packets to `WindowServer._process_refresh`. A lightweight
connection registry delegates to real `WindowVideoSource` CSC parsing,
exclusion/selection and `get_current_or_rgb`; unrelated quality/score
calculations are substituted. It asserts RGB is selected for refresh after
property application, with maximized/render/subregion state unchanged. There
is no socket transport or native presentation in this fixture.

Separate assertions cover another surviving colorspace, unchanged picture
modes, a successful alternative decoder, transient setup plus a bad frame,
replacement/closed/current backings (including the Win32 accessor shape), new
window map properties and bounded handling of an in-flight old frame. The
tests-only control reaches the existing retirement but fails behavioral checks
because no update is sent and CSC modes remain stale; it does not call a new
API directly or fail on its absence. Cleanup drains decoder objects and restores
the module registry; missing subject imports fail rather than skip.

The adjacent window-draw, client-window and server-window modules preserve
dispatch, lifecycle and property consumers. The unit does not execute native
Win32 GDI/WGL or real network latency. Those platform limitations are not
disguised by the accessor-shape assertion.

The terminal window's existing minimal window-subsystem double also supplies
the callback that real `WindowManagerClient` now owns. Its display/geometry
tests still construct actual terminal windows and backings; this fixture-only
hook does not claim to exercise decoder loss. The retained terminal-window
module checks that adding the callback does not break this shared factory's
non-GTK consumers. No optional `getattr` fallback is added to production to
conceal an incomplete test double.

## Durable live or package boundary

The complete-stack physical H.264 scenarios in `infra/live/run.py` exercise
actual negotiated CSC modes, libva decode, native presentation, refreshes and
window lifecycle under the default client seccomp mode. Their H.264 proof
must remain positive; falling back everywhere cannot count as hardware
acceptance. RGB and the other full-stack profiles exercise picture and backing
composition paths. All nine profiles remain required on both complete-stack
endpoints.

The hardware profile additionally owns a separately named `decoder-loss`
scenario, described in the canonical
[live recovery boundary](../../docs/runbooks/live-tests.md#permanent-decoder-loss-recovery).
Its test launcher preserves real libva discovery/self-tests and changes only
the live backing factory, so the client first advertises H.264 and fails its
first real context under the installed default filter. Actual peers must
propagate the CSC-only retirement, exclude H.264 in server state, present
subsequent full-size picture frames and retain working input/lifecycle. The
collector reparses raw authorities independently of report assertions.
This does not substitute picture frames for the sibling's positive hardware
proof, simulate a physical mid-stream GPU reset, or provide native Windows
coverage. There is no new native payload or package dependency in this case;
the injected launcher belongs only to the fork's test images.

## Invariants not to simplify

- Notify at actual colorspace retirement, not every bad frame or candidate failure.
- Filter current backing capabilities, not the helper's static module inventory.
- Preserve surviving colorspaces and picture modes.
- Queue through the existing UI owner and query current windows/backings there.
- Apply the CSC delta before refresh; preserve other negotiated server state.
- Reuse existing picture fallback and decoder cleanup instead of adding parallel policy.

## Required validation

Follow [canonical validation](../../docs/runbooks/validation.md): non-vacuous
tests-only negative, patched focused regression and affected dependency/window
modules, composed focused checks, compiled and no-compat coverage, full
fork-control checks and all three full upstream legs after candidate freeze.
Complete the nine-profile live loop and its final uninterrupted pass. Static
checking uses the embedded Ruff configuration; no new module joins the scoped
mypy inventory. Runtime results and unavailable hardware/platform coverage
belong in the session ledger, not assertions of acceptance in this README.

[va-review]: https://github.com/Xpra-org/xpra/pull/4964#discussion_r3612047076
[fallback-review]: https://github.com/Xpra-org/xpra/pull/5023#discussion_r3861431765
