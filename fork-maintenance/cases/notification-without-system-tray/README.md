# Deliver notifications without tray forwarding

Code: the `Fork-Case: notification-without-system-tray` commit on `develop`
(`make -C fork-maintenance case-show CASE=notification-without-system-tray`).

## Boundary

Client notifications are independent of forwarding remote application tray
icons. `--system-tray=no` sets `features.systray=False`, which excludes the
`WindowTray` mixin from the actual `WindowClient` composite. A window subsystem
still exists, but has no `get_tray_window()` method. The notification UI callback
previously called it unconditionally whenever that subsystem existed, raising
`AttributeError` before invoking the notifier. GLib reports the callback error
and continues, so a successful rendered session and exit status can hide every
lost notification.

Notification delivery must work with tray forwarding disabled. When forwarding
is enabled, existing PID/title selection of a remote application's tray remains
intact. Otherwise the optional own Xpra tray is a suitable notification anchor;
without either tray, the notifier receives `None` as before.

## Embedded-source context

The embedded source is `646b712d1bff46b08b8f26ddc7a83229d65ba5bc`.
Antoine Martin's `5cd75ce8b5` moved notification tray lookup to the window
subsystem. The current base also conditionally composes `WindowTray` through
`get_window_client_base_classes()` using `features.systray`. Both the routing
change and composition were read. The unconditional call assumes a method
which the supported disabled configuration intentionally does not provide.

`TrayClient` is a separate subsystem controlled by `features.tray` (`--tray`);
it owns Xpra's own status icon. `WindowTray.get_tray_window()` already falls
back to that icon after checking forwarded trays. This case preserves those
owners rather than enabling forwarding or adding the optional API to every
window class. Retirement requires notification lookup to respect the configured
window composition and preserve both the enabled and disabled routes. No
upstream endorsement is claimed.

## Surrounding code and ownership map

| Component | Responsibility |
| --- | --- |
| `client/base/features.py` | Resolves `tray` and `systray` independently from options before client classes are imported. |
| `client/subsystem/window/__init__.py` | Builds the real window composite, including `WindowTray` only when forwarding is selected. |
| `client/subsystem/window/tray.py:WindowTray` | Selects forwarded tray widgets by PID/title and supplies the own-tray fallback. |
| `client/subsystem/tray.py:TrayClient` | Owns the optional Xpra status icon independently of forwarded applications. |
| `client/subsystem/notification.py:NotificationClient` | Decodes/sanitizes notification contents, then selects an optional tray anchor on the UI thread and invokes the notifier. |
| Platform notifier | Owns actual notification presentation and callbacks; may accept a tray anchor or `None`. |

The window subsystem's existence alone does not certify the optional tray
mixin. Conversely, disabling forwarded trays must not disable notifications or
the own status icon.

## Mechanism and lifecycle

The UI callback consults the same `features.systray` flag as the production
window factory. Only the enabled route obtains the window subsystem and calls
its required `get_tray_window()` method. Disabled forwarding or an absent
window subsystem selects the optional own tray directly; an absent tray
subsystem yields `None`.

This is a configuration boundary, not dynamic method probing. No `getattr`
fallback hides a missing method in an enabled real window composite. Class
construction and feature flags keep their existing startup order. No optional
forwarding module is imported or activated to show a notification.

Notifier invocation, sanitized arguments, active callbacks, rate limiting and
the existing caught notifier-failure response retain their behavior. Errors
from a required enabled implementation still propagate through their existing
boundary. Cleanup, notification ordering through the decode/UI queues and
platform notifier selection are not changed.

## Case-commit and integration ownership

The case owns only notification tray-anchor selection and its focused
regression module. The necessary features, real window factory and tray
subsystem exist in the embedded base, so no case dependency is needed.
`case-check` proves independent application and removal.

The server-side `notification-client-detach` case handles a reply after server
source removal; it is a different lifetime and does not supply a client tray
API. The fake-window callback repair concerns public OpenGL construction,
not optional application-tray composition. This case requires neither fix to
reproduce or repair the missing method. Window drawing, clipboard, pointers
and WSSO retain their independent ownership.

## Commit scope and non-goals

Preserve notification delivery across optional tray configurations. Do not
turn on tray forwarding, fabricate a successful notification, disable the
notification feature, change platform backends or broaden the window API.
Do not use an application-specific launch flag as a workaround. The own tray
and forwarded trays retain their separate options and ownership.

## Regression design and clean control

`unit.client.subsystem.notification_tray_test` starts a fresh Python process
for each startup configuration, sets the actual feature flags before importing
the real `WindowClient`, and constructs that composite and real notification
and own-tray subsystems. This avoids reconstructing the factory or reloading
modules with a contradictory cached class. The test covers disabled forwarding
with and without an own tray, enabled forwarded PID matching, absent windows
with an own tray, and no trays/windows.

The actual notification callback must call a notifier spy exactly once with
the expected anchor and unchanged notification fields. Forwarded tray metadata
is a small model stand-in; the real optional mixin performs selection. The
notifier is substituted because this test targets routing, not a desktop daemon.
The clean control reaches the real missing-method `AttributeError` under
`systray=False` and the absent-window fallback mismatch; it needs no new API.
Each configuration gets a separate interpreter because production composes
classes once after resolving options.

Existing notification initialization/sanitization and window subsystem modules
remain selected. This does not prove native popup display, every platform tray
backend or interprocess notification timing; complete-stack package-backed
live tests and full compatibility/Cython legs complement it.

## Durable live or package boundary

The live client uses supported disabled tray-forwarding options while remote
clients generate user-connection notifications. Full client stderr contained
unhandled `_show_notification()` tracebacks despite successful frames and
normal process exit. The positive peer-log oracle must reject this callback
error rather than accepting only pixels. It retains benign caught codec-debug
tracebacks and does not disable notification messages to obtain a pass.

Both endpoints must use the complete current stack and exact validated Ubuntu
server/Debian client packages. Continue from the earliest failed live gate,
finish the development walk, then obtain one complete nine-profile pass and
suite check without intermediate fixes. Runtime provenance remains in the
ignored session ledger.

## Invariants not to simplify

- Honor the same `systray` flag used by the real window factory.
- Preserve enabled PID/title matching through `WindowTray`.
- Keep Xpra's own tray independently optional.
- Deliver notifications with a `None` anchor when no tray exists.
- Test fresh actual compositions; never hide enabled-interface failures.

## Required validation

Run the meaningful tests-only control, repaired focused case and composed stack,
existing notification/window tests, all three full legs and offline controls.
Validate both real DEB builds and the complete-stack live loop, including full
peer callback-error checks and one final complete nine-profile pass.
