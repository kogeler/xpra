# wayland-empty-damage-throttle

## Boundary

A mapped Wayland client may request another frame callback while committing no
new buffer damage. Xpra must eventually acknowledge that callback, but it must
not do so synchronously from the same compositor event dispatch. A client that
commits again from every callback can otherwise continuously re-arm both sides
of that dispatch and delay unrelated input packets.

The retained Zed reproducer opens a second parented toplevel. While both Zed
surfaces are present, the unmodified server processes about 1,861 empty-damage
commits per second and delays pointer or keyboard packets for multiple seconds.
Once an input packet is scheduled, surface routing, application activation,
and all window-destruction boundaries complete normally.

## Patch ownership

The patch owns the Wayland window subsystem's empty-damage acknowledgement
scheduling and its focused unit regression. It must coalesce repeated empty
commits per window, retain bounded frame-callback liveness, and cancel pending
work when damaged content supersedes it or when the window leaves service.

It does not special-case Zed, a title, a coordinate, floating windows, or
dialogs. Modal protocol support is outside this case.

## Required validation

Run `unit.wayland.window_test` first in tests-only mode to prove the synchronous
clean-source failure, then with the completed case. Run the native Wayland gate
through `stacks/develop`, which contains its owning
`wayland-initial-window-state` case, and run all three full upstream unit-test
legs. The permanent RGB live regression must create a generic parented second
toplevel, sustain empty-damage frame callbacks, prove bounded real pointer
response and child teardown, and finish positive through the existing live
harness. Before publication, run all six fixed positive live profiles required
by the fork contract.
