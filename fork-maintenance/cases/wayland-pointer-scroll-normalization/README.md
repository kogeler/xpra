# Native Wayland pointer scroll normalization

## Boundary

Xpra wheel packets carry a direction button and a fractional distance in wheel
clicks. Native Wayland must translate these to its axis orientation, surface
distance and value-120 units consistently with discrete buttons 4 through 7.
Passing the signed Xpra distance directly to wlroots reverses vertical smooth
scrolling relative to discrete scrolling and uses a different surface-distance
scale for the same wheel click. Client inversion is represented by the mapped
button and must not be lost at this boundary.

This case owns the native pointer conversion, not GTK event deduplication,
keyboard policy, rendering or packet-handler error reporting. GTK event
handling belongs to the separate GTK scroll case; upstream XI2 selection
remains unchanged. Each incoming wheel
operation is translated independently; two client representations of one
physical step are not deduplicated by this server fix.

## Validation

The regression starts a real compositor and a mapped native Wayland C client,
which records actual protocol events. Version-5 and version-8 pointer clients
exercise discrete and value-120 delivery, both axes and directions, signed
distance, fractions, mapped inversion, wheel releases and an ordinary button
tail. The tests-only clean control must fail on incorrect emitted values.

Existing complete-stack hardware profiles exercise smooth Xwayland client
input and the GTK lifecycle profiles exercise XTEST discrete input. Both bind
outgoing packets to the native server axis and actual remote GTK displacement;
packet logs without application behavior cannot establish acceptance.

Run focused checks, native Wayland, compiled and no-compat checks, composed
stack checks and the final three full upstream legs. Every live run uses the
complete queue on both endpoints; run all nine profiles through
`live-all STACK=develop RUN=<fresh-prefix>` and require `live-suite-check`.
