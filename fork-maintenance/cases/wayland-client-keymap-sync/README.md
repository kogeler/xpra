# Client-driven Wayland keymap synchronization

## Failure boundary

The native Wayland server starts its wlroots virtual keyboard with a one-group
`us` keymap. Its keyboard manager discards the client's structured keyboard
properties, the Wayland keyboard configuration does not parse or apply RMLVO
data, and the manager inherits the generic no-op keymap installer. Client key
events may carry a group, but a group that was never compiled cannot select the
client's layout. The device also flattens every group into one first-win
`keysym -> keycode` map, which is wrong for collisions such as `us,fr` A/Q.

This is not only a missing `set_layout()` call. Xpra receives keyboard state
through several packet generations, allows multiple input clients to share one
server seat, and tracks key presses, modifiers, repeat, focus, and readonly
policy in different subsystems. Replacing only the bootstrap layout would
leave releases translated through a different map, let non-owners overwrite a
shared device, or silently accept an approximate legacy representation.

## Surrounding code and ownership map

The case crosses client discovery, generic protocol code, the native Wayland
server, and the wlroots Cython boundary. These are the relevant responsibilities
in the current source:

| Layer | Relevant responsibility |
| --- | --- |
| `xpra/client/subsystem/keyboard.py` | Creates the GUI helper, puts keyboard properties in the hello packet, schedules delayed configuration after the handshake, and records the server's exact-RMLVO capability before that callback runs. |
| `xpra/client/gui/keyboard_helper.py` | Combines platform discovery with command-line overrides and builds the versioned exact RMLVO block. A server which advertises the same version receives one flat `keyboard-config`; an unnegotiated server retains the established `layout-changed` then nested `keymap-changed` compatibility flow. |
| `xpra/client/gtk3/keyboard_helper.py`, `xpra/platform/keyboard_base.py`, and `xpra/platform/posix/keyboard.py` | React to a real X11 keymap change and invalidate the cached modifier meanings before the refreshed packet is built. |
| `xpra/server/source/keyboard.py` | Stores one `keyboard_config` per connection and distinguishes a requested recording client from one which was actually authorized to record. |
| `xpra/server/subsystem/keyboard.py` | Owns generic packet decoding, key injection, UI-driver arbitration, keyboard-sync, and repeat interfaces. This case extends its internal key/repeat calls with source and wire-key identity without changing the non-Wayland behavior. |
| `xpra/wayland/server/keyboard_config.py` | Normalizes untrusted structured RMLVO data, records presence and last-known-good state, and pins press-time translations. It deliberately does not own the wlroots device. |
| `xpra/wayland/server/subsystem/keyboard.py` | Owns policy and the shared-seat state machine: per-source configurations, deterministic map ownership, validation versus installation, group translation, modifiers, held keys, repeat timers, promotion, rollback, and keyboard information. |
| `xpra/wayland/server/keyboard.pyx` and `wlroots.pxd` | Compile libxkbcommon candidates, build the group-aware lookup index, prepare and swap the native `wlr_keyboard`, verify symbols under an XKB state, and notify the seat. |
| `xpra/wayland/server/subsystem/pointer.py` and `window.py` | Attribute modifier and focus changes to the source which caused them. The pointer path mirrors readonly and UI-driver arbitration before forwarding modifiers; the window path keeps generic readonly handling and adds recording-source guards, while writable non-owner focus remains supported. |
| `xpra/server/subsystem/settings.py` and `xpra/server/core.py` | Publish readonly changes before notification side effects so the Wayland manager can settle input and reconcile ownership for client, control-command, and global transitions. |

There is one `wlr_seat` and one installed `wlr_keyboard`, not one native device
per Xpra connection. The per-client `KeyboardConfig` objects describe and
translate each source, while `WaylandKeyboardManager.effective_rmlvo` describes
the single map currently installed on the seat. Keeping those two concepts
separate is the central design constraint.

The normal current-client flow is:

```text
real X11 XKB state changes
  -> GTK keys-changed callback
  -> invalidate cached POSIX modifier meanings
  -> query XKB names and build versioned exact RMLVO properties
  -> hello keymap, then a post-handshake packet when delay is enabled
  -> ClientSession parses the source without touching the seat
  -> add_new_client runs after connection sharing/lock policy
       -> eligible vacant owner: compile and install
       -> delayed source: validate and reserve until a usable update arrives
       -> non-owner/readonly/recorder: validate source-local map only
  -> key events resolve source group + symbol against the installed owner map
  -> wlroots receives the resolved server keycode, group, and modifier state
```

Do not move native device mutation back into hello parsing. The generic client
session has not finished connection acceptance and sharing policy at that
point; `add_new_client()` is the first safe mutation boundary.

Reserving ownership for a delayed source does not block that otherwise
eligible source's input. Before its complete packet arrives it may type any
symbol that resolves against the still-active installed startup/default map.
If completion is rejected or the reservation is withdrawn, the relevant
recovery path must settle those startup-map presses and repeat state before
promotion or fallback. Packet rejection uses `_recover_unapplied_owner()`;
readonly, disconnect, and stale-owner transitions reconcile through
`_reconcile_owner()` and `_settle_input()`. Treating “delayed” as “cannot
inject”, or routing policy transitions through the packet helper, would leave
stuck input or conflate two state-machine boundaries.

## Client discovery and wire compatibility

The client helper must preserve two meanings which older properties overload.
On X11, an authoritative `query_struct` contains the actual comma-separated
XKB layout groups. On Win32, macOS, and some generic helpers, plural `layouts`
and `variants` are lists available for selection, not simultaneous XKB groups.
Consequently, version 1 gives plural fields exact positional semantics only
when `rmlvo-version=1` is present. An unversioned packet prefers the singular
current layout and XKB query values over legacy selection lists.

`get_keymap_spec()` first applies command-line overrides to the detected XKB
names and stores that result in `query_struct`. `get_rmlvo_properties()` then
chooses the effective client values as follows:

- the already override-adjusted authoritative XKB query layout is preferred;
- explicit model, layout, variant, and option overrides therefore replace the
  original detected values before exact serialization;
- a detected singular layout is used when there is no authoritative query;
- an uninitialized base helper omits the exact block instead of claiming an
  empty map; and
- an aggregate detected value such as `us,fr,ru` is not prepended again to an
  identical plural group list.

`XkbRF_GetNamesProp` may omit optional empty rules, model, variant, or options
entries. If the same query contains a layout plus rules or model, those
omissions are authoritative empty values, not permission to inherit a
non-empty server default. The helper therefore sends explicit empties in that
case. The command-line spelling `options=none` also means explicitly empty.

The helper keeps command-line option intent separate from detected runtime
state. `options_option` is the immutable override supplied at construction;
`options` is replaced on every platform query with the current effective XKB
value. Only the former is allowed to override later detection. Conflating the
two makes the first detected non-empty option sticky: a subsequent option
change or explicit clear is silently replaced with the old value, can leave
the stable hash unchanged, and can suppress the update entirely. An explicit
`options=none` is normalized to the empty value in both the versioned packet
and the legacy `layout-changed`/`keymap-changed` path, including an alternate
keyboard backend which emits only `layout-changed`.

The helper continues to carry legacy fields for older servers. It must not let
those compatibility fields overwrite versioned `layouts` or `variants` after
the exact block is constructed. The Wayland server advertises
`keyboard.rmlvo-version=1`; only an exact integer version supported by the
client enables the one-packet flat path. Boolean, string, absent, or unsupported
versions retain legacy behavior. This negotiation occurs while parsing the
server hello, before the queued post-handshake `send_config()` callback runs.
An uninitialized helper which cannot produce an exact block also falls back,
even if the server advertises the capability. The server accepts all three
established shapes:

- hello and legacy `keymap-changed` data nested below `keymap`;
- current flat `keyboard-config` properties; and
- the older `layout-changed` packet, which updates layout, variant, and options
  while preserving fields that packet cannot represent.

The post-handshake ordering needs an explicit barrier rather than relying on
the nominal keyboard-data delay. GTK may deliver `keys-changed`, and its
500 ms coalescing timer may expire, before the server hello has selected a wire
version. Every writable helper therefore starts with `server_rmlvo_pending`
set and registers one handshake-completion callback. While that flag is set,
`send_config()` records only `config_pending`; it must not serialize or queue a
legacy packet through the client's own `after_handshake()` send wrapper.
Parsing server capabilities records version 0 or 1 but deliberately leaves the
barrier in place until all handshake parsing has completed. The callback then
clears the barrier and sends the newest configuration at most once. With
`DELAY_KEYBOARD_DATA` it pre-marks the initial configuration as pending; with
delay disabled it sends only when an early change actually occurred. Thus
multiple pre-handshake changes coalesce, a delayed client always completes its
reservation, and a no-delay client retains its historical no-initial-packet
behavior without racing exact-RMLVO negotiation.

The normalizer also recognizes the established `xkbmap_*` aliases. Packet
envelopes remain strict: nested and flat payloads must be dictionaries, their
`force` value must be a real boolean, and hello and structured-keymap modifier
lists are read as raw exact builtin sequences and bounded before normalization
or proportional copies. Key-event modifier decoding still belongs to the
generic packet path and must not be mistaken for this structured-map bound.
Current and legacy event groups which are boolean, non-integer, negative, or
outside the usable map fall back to zero. Signed integer packet values are
decoded first and bounded by the manager before any wlroots call; flat event
attributes are read without coercing strings, floats, or booleans into an
integer. An absent legacy key-action group uses the protocol's group-zero
default, while the separate internal modifier-only sentinel preserves the
current valid group. The `keyboard` and nested `delay` capability booleans retain
`typedict.boolget()` compatibility, including the scalar wrapper
`{"": value}`; they must be unwrapped through fixed dictionary lookups rather
than by copying the enclosing mapping. Only an internal `None` passed to
`get_keyboard_config()` denotes server defaults. Any other non-dictionary
object is a malformed configuration and must retain its bounded rejection
rather than silently becoming bootstrap state.

With `DELAY_KEYBOARD_DATA`, hello still advertises the bounded structured
configuration and `delay=true`, but omits the large raw keycode maps. The
server may validate that map and reserve a vacant owner slot while the installed
startup/default device remains active. A negotiated patched client ends this
interval with one forced flat `keyboard-config`, so a rules/model plus
layout/variant/options change is one complete transaction. It must not also
send the legacy prefix, because that prefix cannot represent rules or model and
would expose a transient hybrid map.

An unnegotiated client retains the established wire contract. In the clean
X11/no-backend compatibility path a truthy layout sends `layout-changed` before
the nested `keymap-changed`; a non-empty alternate keyboard backend may send
only `layout-changed`. A client connected to the patched native Wayland server
uses the negotiated complete flat map even when the helper reports an alternate
backend; backend/name are legacy hints and the generic Wayland `set_backend()`
hook is intentionally a no-op. The legacy packet remains an immediate usable
update and may end the delayed reservation while preserving rules, model, sync,
and modifier meanings already held by the source configuration. The following
nested packet is a separate complete update, commonly hash-identical during
initial attachment but potentially a second valid transaction at runtime.
There is no protocol end marker, so delaying an unversioned layout packet with
an idle callback or timer would race and break legacy/backend clients. The live
gate intentionally uses the clean maintained client and requires the richer
nested update to install its distinct model; negotiated one-packet behavior is
bound by focused client/server regressions.

## Presence-aware RMLVO normalization

`RMLVOConfig` is an immutable effective configuration containing rules, model,
ordered layouts, aligned variants, options, the client's `layout_groups`
capability, and a set recording which effective fields came from the wire or
query rather than defaults. Normalization uses this precedence independently
for each field:

```text
explicit current/versioned field -> query_struct field -> configured default
```

Configured server defaults are themselves normalized field by field against
the hard `BOOTSTRAP_RMLVO` (`evdev/pc105/us` plus empty variant/options), so an
operator may specify only the fields being overridden. Parser-generated empty
command-line values are removed first and therefore remain absent rather than
overriding those fallback fields. If configured-default normalization fails,
or if the resulting structured map cannot compile during setup, the complete
hard bootstrap is installed instead. This fallback layering belongs only to
server startup. An invalid client map is rejected and rolled back to its own
last-known-good state; it is never repaired field by field with bootstrap
values.

Explicit empty rules, model, variant slots, and options therefore differ from
missing values. Variants are padded with empty strings to the layout count, but
they may not outnumber layouts. Neither parser nor hash reorders or
deduplicates groups. Group identity is the `(layout, variant)` pair plus its
duplicate occurrence, so a reorder of repeated layouts remains deterministic.
If a later map contains fewer duplicate occurrences, the old occurrence is
clamped to the last surviving match rather than selecting an unrelated group.

The stable SHA-256 covers every effective RMLVO field and `layout_groups`; it
does not change merely because the same effective value came from a different
wire-presence path. This lets nested and flat equivalents suppress an
unnecessary native replacement without erasing presence semantics during
normalization and diagnostics.

Structured input is untrusted. The parser bounds individual names, aggregate
strings, options, group count, modifier sequences, and modifier-meaning maps
before allocating proportional copies. A full hello may legitimately contain
many unrelated capabilities, so its outer dictionary is not copied or rejected
by total cardinality: the parser probes only a fixed owned-key projection.
Nested `keymap` and `query_struct` dictionaries are independently capped at 64
and 16 top-level entries before that projection. The current full legacy
`keymap` payload uses 19 entries; the base XKB names query uses five before the
helper may add a backend or plural override fields. Layouts are limited to
XKB's four simultaneous groups. Names and options accept the bounded XKB
identifier forms used by installed rules, while slash, filesystem path,
include syntax, control characters and malformed selected mappings are
rejected. Unknown legacy raw keymap fields are never consumed or applied;
rejecting every unknown envelope field would break compatible clients. The
server does not try to prove availability with a language, country, layout,
variant, option, or model allowlist: after syntax and size checks,
libxkbcommon and the installed XKB rules/data are authoritative.

Generic command-line parsing materializes absent keyboard options as empty
strings and lists. Wayland setup removes those empty legacy defaults before
normalization, retains `sync` as runtime policy, and now carries the configured
model alongside layout/variant/options. Do not reinterpret parser-generated
empties as explicit versioned wire values; that would reject an ordinary
default startup or override the hard bootstrap incorrectly.

Parsing the RMLVO, sync flag, and modifier meanings is all-or-nothing. A full
structured parse preserves an absent sync value but deliberately clears absent
modifier meanings; the full packet is authoritative for that metadata. Legacy
`layout-changed` cannot represent either field, so its narrow update preserves
both. A bad field records a bounded reason plus a hash of a bounded structural
prefix, then
restores the last usable snapshot. That fingerprint has independent byte,
depth, node, container-item, and scalar limits; it is cycle-safe, keeps unknown
objects opaque, and must not invoke untrusted `repr`, type metadata, or general
container hooks. The code maintains distinct validated and applied snapshots
because a non-owner may successfully compile a newer map without ever
installing it. If its later promotion fails at the native install step,
rollback must return to the last actually applied metadata rather than
presenting the merely validated map as active.

That hook-free boundary also applies before fingerprinting. Real dictionary
subclasses are recognized by identity against the builtin `dict` in their type
MRO, never by equality or an instance-provided `__class__`; owned keys are then
read with builtin `dict` operations. Sequence and scalar types are accepted by
exact type identity, because even apparently harmless tuple membership can
invoke a hostile metaclass `__eq__`. Debug logging must expose only normalized
state and the bounded rejection hash, never format the original wire object.
Ordinary packet decoding normally yields builtins, but direct embedding calls
and regressions exercise these fallbacks, and a malformed object must not gain
a side-effect hook merely because debug logging is enabled.

## Native XKB candidate and device lifecycle

`WaylandKeyboard.compile_keymap()` builds a detached `KeymapCandidate`. It
creates an XKB context and keymap, enumerates every keycode, global group,
level, and symbol, and stores candidates under `(group, keysym)`. Iterating all
global groups is intentional: libxkbcommon wraps keys with shorter per-key
layout arrays, so Return, Space, navigation, and modifiers must remain
resolvable outside group zero.

Libxkbcommon can return a keymap while logging a hard error for an ignored
component, notably an unknown option. A temporary XKB log callback records
that condition so the server rejects the partial result. The callback's user
data points to a C stack flag; it must be cleared before the keymap retains the
context beyond `compile_keymap()`. Every candidate, keymap, and context is
released on success and failure paths. Do not simplify this to checking only
for a non-NULL keymap.

The manager also requires the compiled global group count to equal the exact
requested layout count. A syntactically valid but unavailable map therefore
cannot become the active translation metadata. Unknown models are not rejected
by a Python allowlist because installed rules such as `evdev` deliberately
contain wildcard model handling; a logged libxkbcommon error remains the
failure authority.

Installation has two transactions:

1. Python parses, compiles, indexes, validates the group count, calculates the
   replacement group by layout/variant identity, and snapshots held state.
2. Cython allocates a detached replacement `wlr_keyboard`, applies the keymap,
   repeat settings, safe group, and retained non-held modifiers, and only then
   releases old held keys and swaps the seat's keyboard pointer.

Until the detached `wlr_keyboard_set_keymap()` succeeds, the old device,
keymap, repeat state, modifiers, group, translations, and logical holder state
remain untouched. After the one-way native swap succeeds, Python retires
repeat timers and old press translations, commits the new effective metadata,
and flushes the compositor. Scheduler cleanup errors are made harmless by
retiring timer ownership before calling the scheduler; they cannot split an
already committed native replacement.

The Cython device is constructed with a small hard `evdev/pc105/us` map so a
no-client or legacy session always has a keyboard. Startup then normalizes and
tries the configured server default transactionally. The successfully
installed startup/default snapshot is retained as
`WaylandKeyboardManager.bootstrap_config`; despite that historical attribute
name, it may be a configured map such as `de/pc104`, not the hard US map. Owner
departure first promotes the earliest remaining eligible client whose source
configuration was validated; only when no such promotion succeeds does it
restore this installed snapshot. If configured normalization or compilation
fails, both Python metadata and the native device use the complete hard
`BOOTSTRAP_RMLVO`, and that hard snapshot becomes `bootstrap_config`. Neither
form is a production language restriction.

Native cleanup is also ordered. It releases pressed keys, detaches the
keyboard only if it is still the seat's current device, calls
`wlr_keyboard_finish()`, and frees it. The Python keyboard subsystem must drop
that device while the compositor and seat still exist; reverse subsystem
cleanup destroys the Wayland manager later. Core subsystems already exist when
`xpra/scripts/server.py` inserts the Wayland manager, but it does so before
`app.init_subsystems()` registers backend subsystems such as the keyboard;
`xpra/server/core.py` then cleans that order in reverse. Those two source
boundaries are what currently guarantee device-before-seat teardown. Moving
device cleanup after seat destruction creates a dangling native pointer.

## Group-aware symbol translation

Client keycodes are not portable across X11, Wayland, Win32, macOS, terminal,
or RFB clients. On the native server they are press identities, not trusted
wlroots keycodes. Resolution derives ordered symbol candidates from:

1. a `KP_*` key name first, because the printable string alone would lose
   keypad and Num Lock semantics;
2. a single printable Unicode `keystr` converted with
   `xkb_utf32_to_keysym()`;
3. the XKB key name; and
4. a bounded numeric keyval only as a final fallback, because other backends
   may use that field for a scan code, toolkit enum, or Unicode value.

The source's requested group is first bounded. A client without
`layout_groups` support, a boolean, negative value, missing legacy group, or an
out-of-range group safely becomes source group zero and is never passed to
wlroots. For a capable client, the manager maps the source
`(layout, variant, duplicate occurrence)` to the installed owner's group. That
match is a preference, not a restriction: the resolver tries the corresponding
occurrence first and then every other installed group in deterministic order
for the actual symbol. When an identity is absent, as it may be for a
non-owner, the deterministic search simply starts at group zero. It never
injects the foreign raw keycode.

The group-aware index is only a candidate list. A temporary `xkb_state` then
verifies that a selected physical key produces the requested symbol under the
exact effective group and modifier level. The resolver may try a bounded set of
Shift, Mod2, and Mod5 toggles for clients which omit a level selector, and it
returns the verified modifier list to the manager. It never toggles away a
modifier backed by another currently held physical key. The returned pair is
the actual server keycode and actual server group.

Symbol priority surrounds group and modifier search: the resolver exhausts all
ordered group preferences and bounded level trials for the printable Unicode
`keystr` before considering a lower-priority key name or numeric keyval. If the
loops are reversed, an unmodified base name such as `q` can win before the
actual `Q` or `@` string has a chance to infer Shift or Level3. Real compiled
XKB coverage, not only the fake policy device, must bind this ordering.

This distinction is what the `us,fr` regression proves. Physical AD01/X11
keycode 24 is `q` in group zero and `a` in group one, while those symbols also
exist on other physical keys. A flat “symbol exists somewhere” lookup can pass
a weak test and still type the wrong character; the assertion must bind symbol,
physical collision, and returned group.

The native keycode includes XKB's offset. `press_key()` sends `keycode - 8` to
wlroots, updates both the keyboard's held-key array and the seat notification,
and supplies modifier state separately with `update_state=false`. Focus enter
therefore sees the native held-key array while the complete client-derived mask
remains authoritative.

## Held keys, modifiers, locks, and repeat

A translation is pinned on press and consumed on release. Positive client
keycodes are the usual identity; clients which send zero for every key fall
back to `(client_keycode, keyname-or-keyval)`. Repeats reuse the original
server keycode/group instead of resolving through the current group or a newly
installed map. When policy or replacement forcibly settles a press, a bounded
tombstone ignores later repeats and consumes the eventual release so it cannot
release an unrelated key in the new state.

Two additional maps are required because one native seat is shared:

- `_key_holders[server_keycode]` records every source/wire identity holding
  that key. One client's release reaches wlroots only when the final holder is
  gone; and
- `_modifier_holders[source][modifier]` records depressed physical modifiers,
  while lock and Num Lock are persistent locked state rather than held masks.

Client modifier masks describe the state immediately before the event. The
manager computes the post-event state, aggregates depressed holders from all
eligible clients, and toggles locks only on an accepted press. A modifier's
meaning is pinned at press time so a keymap or `mod_meanings` change cannot
reclassify its release. Modifier-only negative-keycode events remain supported
for Win32 AltGr emulation without injecting a physical key.

Repeat timers are scoped by source and wire-key identity and protected by an
opaque generation token. Removing or replacing a timer retires its ownership
first, so an escaped callback becomes a no-op. Unsynchronized non-modifier
presses are released immediately by the generic path and never become shared
holders. Tracking is bounded per source, across all sources, and by wlroots'
fixed distinct-keycode capacity; alias identities for an already held server
keycode remain legal.

Hello repeat values are accepted only as an exact two-integer bounded pair;
either zero disables both values. Because server capabilities are sent before
`add_new_client()` can validate and install the source map, they report the
active device's repeat contract rather than speculative client values. The
owner's validated repeat settings are activated only with its usable map, and
the detached replacement keyboard inherits the active native values across a
runtime map swap.

Source attribution also applies to mask-only focus and pointer updates. If a
non-owner supplies the current group/modifiers, replacing the owner's map must
not relabel that snapshot as owner state. On departure or readonly transition,
the manager removes only that source's holders, restores surviving state where
possible, and prevents a stale depressed mask from reappearing on later
promotion. Global or owner transitions settle physical input before promotion;
when no eligible source remains, the retained installed startup/default
snapshot resets runtime state.

## Shared sessions, readonly policy, and focus

The first accepted writable, keyboard-enabled, non-recording source claims a
vacant keyboard in deterministic connection acceptance order. Ownership uses
the server-source object identity, not its UUID: reconnects may legitimately
reuse a UUID. Remaining configurations are compiled to validate them, then the
detached candidate is released; only normalized RMLVO, validation, group, and
runtime metadata is retained for translation and future promotion. Promotion
recompiles that configuration before a real replacement installation. If its
hash already equals the active native map, promotion instead transfers the
owner/applied snapshot without another compile, install, or held-state change.
Such a non-owner cannot replace the active seat. Owner departure or loss of
eligibility settles input,
promotes the earliest remaining validated source whose complete configuration
is available, or restores the retained installed startup/default snapshot.

The main state transitions are:

| Event | Required outcome |
| --- | --- |
| Server setup | Normalize configured fields against hard bootstrap fields and install the result if it compiles exactly; after normalization or compile failure, record the rejection and align metadata and device on the complete hard bootstrap map. Retain the successfully installed result as the startup/default snapshot. |
| Hello parse | Create source-local normalized state only; do not mutate the seat. |
| First eligible non-delayed source | Claim by source identity, compile, install, and activate its repeat/modifier/group state. |
| Eligible delayed source | Validate and reserve a vacant slot while the installed startup/default snapshot remains active. A negotiated exact client completes with one flat packet; an unversioned legacy layout packet is itself an immediate usable update and may complete a backend-only client. Compatible symbols may still be injected through the startup map during the reservation interval, so a failed or withdrawn completion must settle them before ownership recovery. |
| Non-owner, readonly, or recording source | Compile for validation and future policy changes, but never replace the device. |
| Identical effective owner map | Refresh the source's applied snapshot and ownership metadata without compiling or disturbing held state, even if the legacy packet says `force`. |
| Invalid parse, compile, or native preparation | Record a bounded rejection, retain the active map and any source-local last-known-good snapshot, and leave a first-time invalid source unusable. |
| Successful runtime replacement | Preserve a valid layout/variant group identity and non-held state, settle old presses/repeats exactly once, then publish the new map. |
| Non-owner departure | Remove only its holders, translations, repeat, and attributable depressed state; retain the owner's map. |
| Owner departure or ineligibility | Settle shared input, promote the earliest usable validated source, or restore the retained installed startup/default snapshot. |
| Server cleanup | Settle input and timers, clear policy state, and detach/free the native keyboard before compositor teardown. |

Keyboard ownership controls installation, not the right of every eligible
shared client to type. A writable non-owner still resolves its own symbol and
group against the owner's installed map, may focus a window, and participates
in shared holder accounting. Readonly, disabled, stale, actually recording,
and recording-requesting sources may neither inject nor own. Keeping
`keyboard_record_requested` separate is deliberate: a denied recorder remains
an ambiguous input source and is excluded even though authorization did not
set `keyboard_record`.

Readonly can change globally, from a client setting, or through a control
command. `SettingsServer` emits `readonly-changed` before broadcasting the new
setting, and `ServerCore` routes control changes through that same boundary.
The Wayland manager can therefore settle held input and reconcile ownership
before later packets observe the new policy. Bypassing the settings subsystem
would leave a stale owner or depressed state on the shared device.

Recording-only input is rejected at both relevant Wayland window boundaries.
The inner `_focus()` guard protects the wlroots device and modifier state from
direct calls. The packet-level `_process_focus()` guard must run before the
generic window handler: that parent otherwise records a user event and invokes
`sync_focus()` even when the inner focus operation returned early. A synced
peer can raise and present the requested window, then send a new focus packet
as an eligible client. Relying only on the device-level guard therefore leaves
an indirect route back to shared focus as well as observable idle and window
side effects. Writable non-owner clients retain ordinary focus handling; the
guard is specific to actual or requested keyboard-recording sources.

Pointer modifier updates have a similar ordering constraint. The Wayland
override mirrors the generic readonly and UI-driver guards before touching the
keyboard, lets the generic pointer path run, and then applies a source-tagged
modifier update. The keyboard manager performs the final eligibility check,
including recording policy.

The client-session registry removes a source before subsystem
`cleanup_protocol()` callbacks run. The keyboard manager therefore identifies
departed clients by object identity against the current registry rather than
expecting the protocol callback to carry the old source. Changing this generic
cleanup order requires revisiting promotion and per-source settlement.

## Information and diagnostics

Per-source keyboard information includes the normalized RMLVO fields,
normalized schema version, wire representation kind, presence set, compiled
group count, sync state, current installation owner metadata, and bounded rejection
status. Only the configuration actually installed on the shared seat carries
an owner value; ownership transfer clears it from former owners and validated
non-owner snapshots. The manager-level `keyboard_owner` may meanwhile name a
delayed source which has only reserved the vacant seat, so it is not by itself
proof that the source's map is installed. Server keyboard information also
exposes that shared owner/reservation, exact effective RMLVO, active compiled
group count, current group, and last rejected client/hash/reason. It
deliberately does not expose raw keymap text. `rmlvo-version` is therefore
always the normalized schema version 1, including for legacy input;
`rmlvo-representation` is the separate provenance field which says `legacy` or
`versioned`.

The manager logs a receipt marker after a structured packet reaches an enabled,
accepted configuration. Malformed outer envelopes and disabled or unregistered
sources do not produce that marker. A later explicit acceptance record is
limited to successful owner installation or identical-map application and
carries the normalized hash, group count, owner, representation, and result;
source-local non-owner validation is not an installation acceptance. These
records diagnose packet ordering and are used by the live gate, but they do not
supersede the actual application text. An invalid update must be visible as a
bounded rejection while the last-known-good native and per-source runtime state
remains usable.

## Patch-queue and test integration traps

This case has no semantic dependency on another production case and declares
the native `wayland` gate itself. It must apply and test standalone. The
maintained stack nevertheless orders `wayland-initial-window-state`, this case,
and then `wayland-empty-damage-throttle`; all three touch
`xpra/wayland/server/subsystem/window.py` and
`tests/unittests/unit/wayland/window_test.py` near one another.

`WaylandWindowServerFocusTest` must remain a top-level class alongside the
initial-state and empty-damage test classes after the whole stack is applied.
Its `Packet` import is intentionally local to `focus_packet()`: moving it into
the module import block creates avoidable clean-base patch overlap with the
earlier case. Textual apply/reverse success cannot prove class ownership or
test discovery when neighboring patches use short context. Inspect the applied
class order and run the focused window test through both the standalone case
and `stacks/develop` after any adjacent edit.

That focus test runs under the native gate, but its autospec signature trap
belongs to the `full-cython` leg: `CYTHONIZE_MORE` compiles the inherited
generic window subsystem. A call arriving through that compiled parent and a
pure-Python call do not expose identical autospec arguments; the compiled call
may omit an explicit positional `self`. Keep the robust assertion as one call
plus exact keyword arguments; restoring a Python-only
`assert_called_once_with(self, ...)` reproduces the full-Cython-only failure.

The same leg makes builtin annotations an executable boundary. The Wayland
event override keeps a `typedict` for convenient source-local reads, but the
compiled generic `KeyboardManager.do_process_keyboard_event(..., kattrs:
dict)` accepts only an exact builtin `dict`, not that subclass. Its `super()`
call must therefore pass an exact copy containing the already sanitized group.
Pure Python and the native-extension-only `wayland` gate both accept the
subclass and cannot detect this regression; the complete `full-cython` leg is
the owning proof.

The large Wayland keyboard tests deliberately combine a fake device/manager
layer with real compiled-XKB checks. Fake lookup tables prove policy and state
transitions but cannot prove installed layout data, xkbcommon level selection,
ABI declarations, or native cleanup. Conversely, compiling a keymap only proves
symbol availability, not shared-client behavior. Preserve both layers.
`unit.wayland.linkage_test` may skip when native modules were not built; the
declared `wayland` gate must build all Wayland extensions and make its isolated
keyboard import non-skipping.

The case also changes narrow generic interfaces whose other backends inherit:
source/key identity on `_handle_key()` and repeat callbacks, the
`readonly-changed` settings signal, and control-command routing. After an
upstream refresh, inspect every caller and override rather than mechanically
retaining old signatures. Generic X11 keyboard behavior must remain unchanged,
while the Wayland subclass consumes the extra identity.

The case-owned live scenario is tracked outside `fix.patch`; production patches
must never contain `fork-maintenance/` paths. New case-owned upstream test files
must keep their `kogeler` copyright notice. Do not hand-edit `fix.patch`, its
digest, or manifest paths when resolving any of these overlaps; use the
isolated workspace transaction required by the fork contract.

## Patch ownership and non-goals

This case owns a presence-aware, bounded RMLVO representation for every
keyboard client; compatible nested, flat, and legacy packet parsing; stable
hashing; transactional libxkbcommon compilation and wlroots installation; and
group-aware keysym translation. It preserves ordered layouts and positionally
aligned empty variants, distinguishes missing from explicitly empty options,
and accepts any bounded combination available in the installed XKB data.

One writable, non-recording client deterministically owns the shared wlroots
keyboard. Readonly, recording-only, and non-owner clients cannot replace it.
Non-owner events translate by normalized layout/variant identity and actual
keysym rather than foreign raw keycodes. Replacement compiles all state before
settling held input, preserves the last known-good state after rejection,
suppresses identical maps, and safely promotes another eligible client or the
retained installed startup/default map when the owner leaves.

It does not:

- accept raw client keymap text, filesystem paths, include expressions, or
  unbounded arbitrary strings;
- impose a language, country, layout, variant, option, or model allowlist;
- require a generated locale when the installed XKB data is sufficient;
- create one wlroots seat or keyboard per client;
- trust a client raw keycode as a native Wayland physical key;
- turn ownership into an exclusive-input policy for otherwise eligible shared
  clients;
- implement an IME, clipboard, paste, synthetic Unicode, or text-input
  protocol; or
- special-case the live fixture, Elsewindow, a launcher profile, `us,ru`, or
  any application identity.

## Regression design

The focused parser and helper tests bind nested/flat equivalence, negotiated
single-packet exact delivery, unnegotiated legacy/backend packet behavior,
pre-handshake change coalescing with keyboard-data delay enabled and disabled,
replaceable detected options versus immutable command-line overrides, legacy
and versioned precedence, explicit empties, every hash field, positional
variants, one through four groups, bounded rejection, modifier-cache
invalidation, and last-good snapshots. Manager tests cover runtime replacement and reorder,
owner promotion, duplicate layout identity, readonly and recording policy,
foreign non-owner translation, shared holders, source-scoped repeat, lock and
depressed modifier state, zero-keycode clients, invalid windows, cleanup, and
native-install rollback.

Real compiled-XKB tests cover arbitrary installed layouts, variants and
options, all four groups, common keys in every global group, the `us,fr` A/Q
collision, Caps/Num Lock, AltGr, dead keys, keypad symbol priority, Unicode
`keystr` priority, unavailable data, logged option rejection, and rules-driven
model behavior. The native gate then compiles and independently imports the
Cython extension so a Python mock cannot hide a declaration or linkage error.

The dedicated positive live scenario is
`tests/live-wayland-keyboard.json`. Before attachment, the runner seeds the
clean maintained client's real X11 display with the scenario's *replacement*
map, `evdev/pc105/ge,am,us,fr`, and waits for its baseline structured
acceptance. That baseline carries no application input and is not a third
scenario phase. It is deliberate: the observed phase loop can then change to
the initial map and later back to the replacement map, proving that both
recorded updates are non-identical runtime installations. Seeding the initial
map would let the first observed phase collapse into a silent identical-map
path and weaken the gate.

The first observed phase applies `evdev/pc104/us,fr,ru,ara` and injects
AD01/X11 keycode 24 through groups zero through three, producing `q`, `a`,
`й`, and `ض`. Without reconnecting, the second phase restores
`evdev/pc105/ge,am,us,fr`; the same physical key produces `ქ`, `ճ`, `q`, and
`a`. These values belong to the versioned live-scenario schema; they are not
runner branches, production constants, or a claim that this compatibility
probe uses the exact-version wire representation. The clean client deliberately
advertises each map through the compatible nested `keymap-changed` form, which
the server records as `representation=legacy`. Together the two observed maps
exercise XKB's four-group maximum twice and Latin, Cyrillic, Arabic, Georgian,
and Armenian Unicode data.

For each phase the runner verifies the queried client RMLVO and requires the
clean client's nested structured update to be received, install the expected
hash, and be explicitly accepted in that order. The distinct valid model
fields force the richer update to establish its own state after the preceding
legacy layout packet. An identical-only result, silent packet, rejection, or
startup-only configuration cannot prove runtime synchronization.

The driver locks each actual XKB group and sends one complete XTEST
press/release pair for the unchanged physical key. It does not call
`xdotool type`, paste, send Unicode, or construct Xpra packets. Every input is
bound to the clean Xpra client's exact logged `key-action` press/release pair,
the client XKB group and symbol, the server's resolved group/keycode and device
events, and the forwarded fixture window ID.

The server-side fixture is an ordinary focused native-Wayland `Gtk.Entry`. It
receives no scenario or expected string and publishes only its actual ordered
UTF-8 key events and cumulative buffer. That buffer is acceptance authority.
Missing, extra, duplicate, reordered, stale, malformed, packet-only, wrong-key,
or wrong-group evidence fails even when logs and `xpra info` look plausible.
The exact final cumulative sequence for the current scenario is `qaйضქճqa`.
The report also binds unchanged client/server process identities and the same
established TCP connection, runtime replacement, exact server information,
zero fixture exit, application-exit lifecycle, and owned cleanup. Mutation
tests independently invalidate every acceptance-bearing field, including a
plausible packet-only report with no application observation.

The live runner and its job provenance currently require exactly one selected
case to own this scenario. Therefore the case cannot be retired merely because
upstream absorbs the production diff. The scenario must first move to durable
neutral ownership, or to an equivalent generic manifest-declared mechanism,
with the runner, provenance, inventories, schema checks, mutation tests,
contract, and live runbook updated together. The migrated
`live-wayland-keyboard STACK=develop` gate must pass before deleting this case.

## Invariants not to simplify

- Do not treat legacy plural layout choices as simultaneous groups without the
  versioned exact representation.
- Do not collapse missing and explicitly empty rules, model, variants, or
  options, and do not reorder or deduplicate layout groups.
- Do not include wire-presence provenance in the stable effective-map hash.
- Do not implement rejection diagnostics with `repr`, JSON serialization, or
  general traversal of untrusted objects; preserve the bounded, cycle-safe,
  hook-free structural fingerprint.
- Do not add a language/model allowlist or accept raw keymap text, paths, or
  include syntax; bounded libxkbcommon compilation is availability authority.
- Do not mutate the seat while parsing hello or while merely validating a
  non-owner, readonly, delayed, or recording source.
- Do not release held input before detached native keymap preparation has
  completely succeeded.
- Do not retain the temporary XKB error callback or its stack user data in a
  candidate context.
- Do not flatten `(group, keysym)` lookup to `keysym`, and do not accept a
  candidate keycode without verifying its effective XKB state.
- Do not inject a foreign raw client keycode when source and owner layouts
  differ.
- Do not pass a negative, boolean, group-less, or out-of-range event group to
  wlroots; use the documented group-zero fallback. Preserve the internal
  absent-group sentinel for modifier-only focus/pointer updates: it means
  "keep the current valid device group", not "select group zero", and must be
  resolved before the wlroots call.
- Do not re-resolve a release or repeat through the current map; keep its
  press-time server keycode and group until release or settlement.
- Do not let one source's release unpress a shared server keycode still held by
  another identity.
- Do not lose source attribution for modifiers, group-only focus/pointer state,
  or repeat timers across owner replacement and departure.
- Do not identify an owner solely by UUID or let an actual/requested recorder
  own, inject, or redirect focus.
- Do not remove the outer recording focus guard merely because `_focus()` also
  rejects the source; generic parent side effects occur outside `_focus()`.
- Do not bypass `readonly-changed` when changing client or global policy.
- Do not destroy the native keyboard after its seat, and do not trust an
  optional linkage-test skip in place of the native `wayland` gate.
- Do not trust patch applicability alone when adjacent queue cases touch the
  same window subsystem and test module.

## Required validation

Follow the current isolated-workspace, upstream-test, and live-test runbooks;
do not apply the production source to the host checkout or use ad hoc output as
acceptance. The retained tests-only regression must fail non-vacuously against
the embedded clean source. Run all focused modules declared by `case.toml`
with the completed standalone case, then run the native `wayland` boundary.
Repeat the affected focused and native boundaries through `stacks/develop` so
adjacent Wayland patches and generic interface changes are exercised together.

Reassess the clean quarantine modules, run all three full Ubuntu 26.04 legs,
and run the dedicated positive `live-wayland-keyboard` gate. The live result
must retain the exact versioned scenario digest, both four-group maps, every
press/release observation, authoritative eight-character application sequence,
runtime replacement, connection/process identities, information snapshot,
fixture exit, lifecycle, and owned cleanup. Before publication, run all seven
fixed positive live profiles required by the fork contract so this keyboard
case is also tested with the complete rendering, detach, transport-loss,
empty-damage, Vulkan, and OpenGL stack boundaries.
