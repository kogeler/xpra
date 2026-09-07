# GTK client scroll event ownership

## Boundary

The GTK pointer handler accepts both discrete and smooth scroll events. When
an input action also has a discrete emulation, forwarding both creates two
independent remote scroll operations. A correct client boundary must preserve
smooth fractions without replaying their emulation, while keeping standalone
discrete devices usable. Device names and arrival-time heuristics are not an
authority for dropping input.

This case owns event admission in the GTK client. It does not reinterpret the
wheel protocol or correct native Wayland conversion; that belongs to the
separate Wayland pointer normalization case.

The GTK adapter normally drops GDK-marked discrete emulation when smooth
handling is enabled. X11 needs one exception: GDK reports zero on an axis's
first absolute valuator sample after initialization or reset, before delivering
its emulated buttons. Those buttons retain movement missing from that smooth
event. The adapter preserves them only on a zero axis with matching latest
X11 event time, window, devices, coordinates and modifiers. Every smooth event
replaces this bounded state, including when timestamps coincide. Multiple
whole steps from one initial sample remain usable.

Native Wayland sends the discrete copy first and has no absolute valuator
baseline; it never uses this X11 fallback. Genuine discrete events, coarse
policy, axis inversions and pointer admission remain intact. Upstream XI2
selection and handlers are unchanged. There are no timers, delays or device-name
heuristics. The common pointer subsystem still owns wire serialization.
An initial fractional X11 sample with no discrete counterpart cannot be
reconstructed at this GTK boundary.

## Validation

The native focused regression obtains an actually emulated GDK event from a
Wayland compositor and dispatches it through real GTK widget delivery and the
outgoing pointer subsystem. Publicly constructed unmarked events cover genuine
discrete input. Controls include both axes, fractions, repeated and reversed
event order, inversion, coarse policy and disabled/readonly admission. Real
Xvfb windows and devices additionally exercise the X11 fallback using the same
native emulation flag and public event metadata: first/reset samples, per-axis
recovery, multiple initial steps, coincident timestamps, metadata mismatches,
and admission/cleanup invalidation. These are GTK-adapter controls, not an XI2
input generator. The
native event remains owned by its callback: copying a boxed GDK event can lose
its private emulation flag and would invalidate this control. Tests-only clean
source must expose duplicate forwarding, not a missing new API.

The complete-stack hardware live profiles additionally exercise real
Xwayland/GTK input from Sway axis events. Each stimulus, including the first,
must produce exactly one wire operation (a wheel packet or a button pair),
matching native server axes, remote GTK displacement and visible feedback.
Forwarding both representations fails. The GTK detach and
transport-loss profiles cover real XTEST discrete scrolling without emulation.

Run focused, compiled, no-compat and composed stack checks plus the final three
full upstream legs. Live validation always uses the complete queue on both
endpoints and all nine profiles through `live-all STACK=develop`; require
`live-suite-check`, never an isolated case live run.
