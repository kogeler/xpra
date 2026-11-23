# GTK client: first X11 wheel step after entering a window

Code: the `Fork-Case: gtk-client-scroll-deduplication` commit on `develop`
(`make -C fork-maintenance case-show CASE=gtk-client-scroll-deduplication`).

## Boundary

On an X11 GTK3 client (Xorg with libinput, or `GDK_BACKEND=x11` under
Xwayland) with the default smooth wheel mode (`--mousewheel=on`), the first
wheel step after the pointer enters an Xpra window is lost, and so is the first
step on the other axis. The remote application does not scroll for that
click; the following clicks work. It happens after every enter, both for
OpenGL windows and for cairo windows (`--opengl=no`).

The mechanism, verified in the GDK 3.24 and X server sources and reproduced
with client and server built from upstream `master`:

1. GDK resets its XI2 scroll valuators on `XI_Enter` (and `XI_DeviceChanged`),
   so the first smooth scroll event after an enter has a zero delta and is a
   stop event (`is_stop`, `raw delta: 0.0,0.0`).
2. For a device with scroll valuators the X server emits that motion and then
   emulated wheel buttons 4-7 with the same time; GDK marks the latter
   `pointer_emulated`. Only the emulated event carries the step.
3. Upstream `f74c91e732` skips emulated events while smooth scrolling is used
   (to stop every step being sent twice), so the step is dropped.
4. For a cairo window the emulated event never reaches the drawing area at
   all: `proxy_button_event()` in `gdkwindow.c` keeps an emulated event on the
   native toplevel when the child window selected `SMOOTH_SCROLL_MASK`.

Not affected: native Wayland clients (the discrete copy of a frame arrives
first, with its own time), XTEST clicks (`xdotool`; that device has no scroll
valuators, so its buttons are not emulated), the XI2 input path
(`xpra/platform/posix/xi2.py` replaces `_do_scroll_event()`), macOS and MS
Windows (GDK does not set `is_stop` there, so `scroll_stop_time` stays `-1`).
For a touchpad the first sample of a gesture after an enter is also zero, but
no emulated button carries it, and this case does not change that.

## Embedded-source context

Source: `d2940c7ddc6edd65aadb00f55ac559045d0c94ed`. The case commit is,
byte for byte, the proposed upstream fix on branch
`upstream/x11-first-wheel-step` (one commit on this same `master`); its
message is that commit's message plus the case trailer. When upstream merges
it, `develop-rebase` drops this commit and the case is retired.

The previous version of this case (a nine-field event key, an axis mask, a
fake-client `wheel_map` and a 436-line native test) was replaced by this
minimal form. Its admission parts are upstream now (`is_readonly()` and the
checks in `PointerClient.wheel_event()` from `10a9c3ca12`/`b2ee40bcdb`, and
our #5152 `22a787b2db`, which does not send unmapped wheel buttons), so the
`FakePointerSubsystem.wheel_map` change is no longer needed either. The
maintainer had asked about the old patch in meta-issue #5037, item 12
(<https://github.com/Xpra-org/xpra/issues/5037#issuecomment-5652872912>):
"how reproducible is this? is it really worth the complexity? it's not clear
to me why f74c91e732 is not sufficient". The answers are in the Boundary
section: every time after an enter on X11; `f74c91e732` drops the only event
which carries the first step; and the fix is a few lines.

## Surrounding code and ownership map

| Path / symbol | Role |
| --- | --- |
| `xpra/client/gtk3/window/pointer.py` `PointerWindow._do_scroll_event()` | Converts GTK scroll events into `wheel_event()` (smooth) or button press/release pairs (discrete), and skips emulated duplicates since `f74c91e732` (`XPRA_SKIP_DUPLICATE_SCROLL_EVENTS`). |
| `PointerWindow.init_window()` | Window-level pointer state; the case adds `scroll_stop_time` and connects `toplevel_scroll_event` to the toplevel, like `KeyboardWindow` connects its key handlers there. |
| `xpra/client/gtk3/window/base.py` widget event wiring | The drawing area (cairo) or GL widget receives `scroll-event` and calls `_do_scroll_event()`. |
| `xpra/client/subsystem/pointer.py` `PointerClient.wheel_event()` / button path | Owns admission, `wheel_map` and serialization; unchanged. |
| `xpra/platform/posix/xi2.py` | The XI2 path replaces `_do_scroll_event` with `suppress_event`; the toplevel handler goes through the same method, so it is silent there too. |

## Mechanism

`scroll_stop_time` is the time of the last smooth event if it was a stop event
(on X11 exactly the zero-delta sample after a valuator reset), else `-1`. An
emulated event is a duplicate unless its time equals `scroll_stop_time`; only
duplicates are skipped. `toplevel_scroll_event()` handles, on the toplevel,
only the emulated event with that time, and otherwise lets GTK propagate the
event as before: scrolling over the header bar still does not reach the
server, and with `XPRA_SKIP_DUPLICATE_SCROLL_EVENTS=0` a cairo window does not
start receiving both copies. The state is one integer per window, written only
on the GTK main thread; there is nothing to clean up.

## Case-commit and integration ownership

The commit changes `pointer.py`, adds `test_first_x11_scroll_step` to the
existing `unit/client/gtk3/client_window_test.py`, and gives the bare
`PointerWindow()` in `unit/client/opengl_backing_test.py` a `connect` stub
(`init_window()` now connects a signal). It has no dependency. No other case
touches the GTK scroll path; pointer coordinate and wheel serialization belong
to upstream.

## Commit scope and non-goals

No change to smooth-event normalization, `wheel_map`, button mapping, the
suppression toggle or any platform other than GDK X11 behavior. The touchpad
first-sample loss described above is not addressed. The old case's
discrete-first Wayland handling and nine-field event key are intentionally not
kept.

## Regression design and clean control

`test_first_x11_scroll_step` creates a real `ClientWindow` with the upstream
`FakeWindowHost`/`FakePointerSubsystem`, enables `wheel_smooth`, and emits real
`Gdk.Event` scroll events on the drawing area and on the toplevel. Only
`Gdk.EventScroll.get_pointer_emulated` is patched: the flag lives in the
private part of the event and `gdk_event_set_pointer_emulated()` is not
exported. A zero-delta stop event at time 1 followed by an emulated `DOWN` on
the toplevel at time 1 must produce `["wheel", "button", "button"]`; on clean
source it produces only `["wheel"]`. The second half (time 2) checks that
ordinary emulated duplicates are still dropped both on the toplevel and on the
widget (the OpenGL path). Blind spots: the real XI2 valuator reset and the
real GDK proxying are not reproduced by the unit test.

## Durable live boundary

The Vulkan and OpenGL hardware profiles scroll with the fork's Sway virtual
pointer through Xwayland into the GTK client (an OpenGL window) and require
exactly one remote operation per wheel step, including the first one; on the
unpatched handler the first down and first right steps are lost. The GTK
detach and transport-loss profiles use XTEST discrete input, which must keep
working unchanged. No live profile currently scrolls a cairo window with
valuator input; that path is covered only by the unit test. Every live run
applies the complete stack to both endpoints.

## Invariants not to simplify

- Keep `f74c91e732`'s suppression for every emulated event except the one with
  the stop event's time.
- Handle only that event on the toplevel; let every other toplevel scroll
  event propagate unchanged.
- Keep the condition on the event time, not on ordering: on Wayland the
  discrete copy of a frame comes first with its own time.

## Required validation

Run the manifest's focused modules (patched, and tests-only as the clean
control, whose first failure must be `test_first_x11_scroll_step`), the full
legs, and the complete nine-profile live suite with the whole stack.
