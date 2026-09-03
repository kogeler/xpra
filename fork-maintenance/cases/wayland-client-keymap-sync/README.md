# Client-driven Wayland keymap synchronization

## Failure boundary

The native Wayland server starts its wlroots virtual keyboard with a one-group
`us` keymap. Its keyboard manager discards the client's structured keyboard
properties, the Wayland keyboard configuration does not parse or apply RMLVO
data, and the manager inherits the generic no-op keymap installer. Client key
events may carry a group, but a group that was never compiled cannot select the
client's layout. The device also flattens every group into one first-win
`keysym -> keycode` map, which is wrong for collisions such as `us,fr` A/Q.

## Patch boundary

This case owns a presence-aware, bounded RMLVO representation for every
keyboard client; compatible nested, flat, and legacy packet parsing; stable
hashing; transactional libxkbcommon compilation and wlroots installation; and
group-aware keysym translation. It preserves ordered layouts and positionally
aligned empty variants, distinguishes missing from explicitly empty options,
and accepts any bounded combination available in the installed XKB data. It
does not accept raw keymap text, paths, include syntax, language allowlists, or
application-specific maps.

One writable, non-recording client deterministically owns the shared wlroots
keyboard. Readonly, recording-only, and non-owner clients cannot replace it.
Non-owner events translate by normalized layout/variant identity and actual
keysym rather than foreign raw keycodes. Replacement compiles all state before
settling held input, preserves the last known-good state after rejection,
suppresses identical maps, and safely promotes another eligible client or the
bootstrap keymap when the owner leaves.

## Required validation

The retained tests-only regression must fail against the embedded clean source
and the patched focused modules must cover nested and flat precedence, every
RMLVO hash field, one through the XKB maximum number of groups, positional
variants, arbitrary installed layouts and options, A/Q collisions, runtime
replacement and rollback, group bounds, modifiers, repeat, locks, AltGr/dead
keys, press/release stability, legacy and multi-client ownership. The native
`wayland` gate must compile and independently import the keyboard extension.

Run the focused case, native Wayland boundary, clean quarantine reassessment,
all three full Ubuntu legs, and the dedicated positive
`live-wayland-keyboard` gate. The live gate must keep one real connection while
an X11 client display applies four ordered groups, locks each group,
and injects complete XTEST press/release pairs for one numeric physical key.
The native-Wayland editable widget's actual UTF-8 buffer is authoritative both
before and after an in-session RMLVO replacement. Distinct valid model fields
force each nested structured update to install its expected hash after receipt
and before acceptance; a preceding legacy layout update, packet-only evidence,
or info diagnostics cannot substitute for application observations. Each input
also freezes the clean Xpra client's actual logged `key-action` press/release
pair and cross-binds it to the native XTEST, server, and widget observations.
