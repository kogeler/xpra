# This file is part of Xpra.
# Copyright (C) 2026 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

from collections.abc import Sequence
from time import monotonic
from typing import Any

from xpra.keyboard.mask import DEFAULT_MODIFIER_MEANINGS
from xpra.net.common import BACKWARDS_COMPATIBLE, Packet
from xpra.server.subsystem.keyboard import KeyboardManager
from xpra.util.objects import typedict
from xpra.wayland.server.keyboard_config import MAX_NAME_SIZE, RMLVO_VERSION, _dict_compatible
from xpra.log import Logger

log = Logger("server", "wayland", "keyboard")

MODIFIER_ORDER = ("shift", "lock", "control", "mod1", "mod2", "mod3", "mod4", "mod5")
CANONICAL_MODIFIERS = frozenset(MODIFIER_ORDER)
LOCKED_MODIFIERS = frozenset(("lock", "mod2"))
MODIFIER_ALIASES = {
    "Shift_L": "shift", "Shift_R": "shift",
    "Caps_Lock": "lock",
    "Control_L": "control", "Control_R": "control", "ctrl": "control",
    "Alt_L": "mod1", "Alt_R": "mod1", "alt": "mod1",
    "Num_Lock": "mod2", "numlock": "mod2",
    "Super_L": "mod3", "Super_R": "mod3", "super": "mod3",
    "Meta_L": "mod1", "Meta_R": "mod1", "meta": "mod1",
    "Hyper_L": "mod4", "Hyper_R": "mod4", "hyper": "mod4",
    "ISO_Level3_Shift": "mod5", "Mode_switch": "mod5", "AltGr": "mod5",
}
MAX_REPEAT_MSEC = 60_000
MAX_XKB_KEYSYM = 0xFFFFFFFF
# wlroots itself retains only a small bounded pressed-key array.  Synthetic
# clients need some headroom for shared-keycode aliases, while still preventing
# one connection (or many connections together) from allocating unbounded
# translation tombstones and one GLib repeat timer per arbitrary wire keycode.
MAX_TRACKED_KEYS_PER_SOURCE = 64
MAX_TRACKED_KEYS = 256


def _mapping_bool(mapping, key: str, default: bool = False) -> bool:
    """Match typedict.boolget without copying an untrusted enclosing mapping."""
    if not _dict_compatible(mapping) or not dict.__contains__(mapping, key):
        return default
    value = dict.__getitem__(mapping, key)
    if _dict_compatible(value) and dict.__contains__(value, ""):
        value = dict.__getitem__(value, "")
    kind = type(value)
    if kind is bool:
        return value
    if value is None:
        return False
    if any(kind is allowed for allowed in (
            int, float, complex, str, bytes, bytearray, tuple, list, set, frozenset,
    )):
        return bool(value)
    if _dict_compatible(value):
        return bool(dict.__len__(value))
    return default


class WaylandKeyboardManager(KeyboardManager):
    __slots__ = (
        "bootstrap_config", "effective_rmlvo", "keyboard_defaults", "keyboard_owner",
        "_keyboard_configs", "_keyboard_owner_source", "_keyboard_state_source", "_key_holders",
        "_modifier_holders", "_settled_modifier_meanings", "_key_repeat_timers",
        "last_apply_result", "last_rejected",
    )
    BACKEND = "wayland"

    def __init__(self, server=None):
        super().__init__(server)
        self.bootstrap_config = None
        self.effective_rmlvo = None
        self.keyboard_defaults = None
        self.keyboard_owner = ""
        self._keyboard_configs: dict[Any, Any] = {}
        self._keyboard_owner_source = None
        self._keyboard_state_source = None
        self._key_holders: dict[int, set[tuple[Any, Any]]] = {}
        self._modifier_holders: dict[Any, dict[str, set[Any]]] = {}
        self._settled_modifier_meanings: dict[tuple[Any, Any], str] = {}
        self._key_repeat_timers: dict[tuple[Any, Any], tuple[int, object]] = {}
        self.last_apply_result = ""
        self.last_rejected: dict[str, Any] = {}

    def make_keyboard_device(self):
        return self.server.compositor.get_keyboard_device()

    def _get_defaults(self):
        if self.keyboard_defaults is not None:
            return self.keyboard_defaults
        from xpra.wayland.server.keyboard_config import BOOTSTRAP_RMLVO, RMLVOError, normalize_rmlvo
        values = {
            name: value for name, value in self.keymap_options.items()
            if value not in (None, "", (), [])
        }
        try:
            # Server options use the legacy singular/plural contract.  In
            # particular, an explicit current layout must take precedence
            # over the plural selection list; only client wire data may opt
            # into the exact versioned representation.
            defaults = normalize_rmlvo({"keymap": values}, BOOTSTRAP_RMLVO)
        except RMLVOError as exc:
            log.warn("Warning: invalid configured Wayland keyboard defaults: %s", exc)
            defaults = BOOTSTRAP_RMLVO
        self.keyboard_defaults = defaults
        return defaults

    def setup(self) -> None:
        # The generic command-line parser materializes absent RMLVO options as
        # empty strings and lists.  They are defaults, not an exact versioned
        # wire representation, and an empty legacy ``layout`` would otherwise
        # turn an ordinary no-option startup into a rejected configuration.
        # Keep ``sync`` because it is runtime policy rather than RMLVO data.
        self.keymap_options = {
            name: value for name, value in self.keymap_options.items()
            if name == "sync" or value not in (None, "", (), [])
        }
        super().setup()
        config = self.config
        if not config or not config.valid:
            from xpra.wayland.server.keyboard_config import KeyboardConfig
            if config and config.rejected:
                self._record_rejection(config, "")
            config = KeyboardConfig(defaults=self._get_defaults(), resolver=self.resolve_keycode)
        if not self._apply_config(config, ""):
            # Syntactically valid configured defaults can still name XKB data
            # which is not installed on this server.  The native keyboard is
            # initially constructed with a small US map, but treating the
            # rejected configuration as our bootstrap would make the Python
            # metadata disagree with the active wlroots device and would make
            # owner departure unable to restore a known-good map.
            from xpra.wayland.server.keyboard_config import BOOTSTRAP_RMLVO, KeyboardConfig
            self.keyboard_defaults = BOOTSTRAP_RMLVO
            fallback = KeyboardConfig(defaults=BOOTSTRAP_RMLVO, resolver=self.resolve_keycode)
            if not self._apply_config(fallback, ""):
                raise RuntimeError("failed to install the Wayland bootstrap keyboard configuration")
            config = fallback
        self.bootstrap_config = config
        self.set_keyboard_repeat(500, 30)

    def get_keyboard_config(self, props=None):
        # None means the ordinary server-default configuration.  Preserve any
        # other object so KeyboardConfig can reject a malformed wire envelope
        # instead of silently turning it into a valid bootstrap map.
        p = {} if props is None else props
        from xpra.wayland.server.keyboard_config import KeyboardConfig
        keyboard_config = KeyboardConfig(p, self._get_defaults(), self.resolve_keycode)
        keyboard_config.enabled = _mapping_bool(p, "keyboard", True)
        # Never format the raw wire object, even at debug level.  Invalid
        # payloads may be large or expose Python representation hooks in unit
        # and embedding callers; the bounded rejection hash is the diagnostic.
        log(
            "get_keyboard_config(valid=%s, representation=%s, hash=%s, rejected-hash=%s)",
            keyboard_config.valid, keyboard_config.representation, keyboard_config.get_hash(),
            keyboard_config.rejected_hash,
        )
        return keyboard_config

    def get_caps(self, source) -> dict[str, Any]:
        # Capabilities are sent before add_new_client can validate and install
        # this source's RMLVO.  Advertise only the current device contract;
        # speculative client repeat values could survive a failed compilation.
        caps = super().get_caps(source)
        caps["keyboard.rmlvo-version"] = RMLVO_VERSION
        return caps

    def _current_sources(self) -> tuple:
        return tuple(self.get_sources_by_type())

    def _keyboard_sources(self) -> tuple:
        current = self._current_sources()
        return tuple(source for source in self._keyboard_configs if any(source is item for item in current))

    def _source_present(self, server_source) -> bool:
        return bool(server_source) and any(source is server_source for source in self._current_sources())

    def _accepted(self, server_source) -> bool:
        return server_source in self._keyboard_configs

    @staticmethod
    def is_recording_source(server_source) -> bool:
        return bool(
            getattr(server_source, "keyboard_record", False)
            or getattr(server_source, "keyboard_record_requested", False)
        )

    def _eligible(self, server_source, *, require_valid: bool = False) -> bool:
        if not server_source or not self._source_present(server_source) or not self._accepted(server_source):
            return False
        if self.server.readonly:
            return False
        if self.is_recording_source(server_source):
            return False
        config = self._keyboard_configs.get(server_source)
        if not config or not config.enabled or not config.valid:
            return False
        if require_valid and (
                not config.validated_hash
                or config.validated_hash != config.get_hash()
                or config.compiled_groups <= 0):
            return False
        is_closed = getattr(server_source, "is_closed", None)
        if callable(is_closed) and is_closed():
            return False
        readonly = getattr(server_source, "effective_readonly", lambda: False)
        return not readonly()

    def _claim_owner(self, server_source) -> bool:
        if self._keyboard_owner_source is server_source:
            return True
        # A newly accepted writable client is compiled by _apply_config after
        # claiming the vacant seat.  Promotion, in contrast, is restricted to
        # configurations already checked by _validate_config.
        if self._keyboard_owner_source is not None or not self._eligible(server_source):
            return False
        client_id = self._source_id(server_source)
        self._clear_config_owners()
        self._keyboard_owner_source = server_source
        self.keyboard_owner = client_id
        log("Wayland keyboard owner is now %s", client_id or "unnamed client")
        return True

    @staticmethod
    def _source_id(server_source) -> str:
        client_id = str(getattr(server_source, "uuid", ""))
        if client_id:
            return client_id
        return f"client-{int(getattr(server_source, 'counter', 0))}"

    def parse_hello_ui_keyboard(self, ss, c: typedict) -> None:
        if not hasattr(ss, "keyboard_config"):
            return
        # Parsing is deliberately side-effect free.  Sharing/lock policy is
        # checked by later subsystems; device mutation starts in add_new_client.
        kc = self.get_keyboard_config(c)
        ss.keyboard_config = kc
        kc.client_id = self._source_id(ss)
        compatible_hello = _dict_compatible(c)
        raw_repeat = dict.get(c, "key_repeat", (500, 30)) if compatible_hello else (500, 30)
        raw_repeat_type = type(raw_repeat)
        if ((raw_repeat_type is not tuple and raw_repeat_type is not list) or len(raw_repeat) != 2
                or any(type(value) is not int for value in raw_repeat)
                or any(value < 0 or value > MAX_REPEAT_MSEC for value in raw_repeat)):
            log.warn("Warning: ignoring invalid Wayland keyboard repeat values")
            repeat = (500, 30)
        elif not all(raw_repeat):
            repeat = (0, 0)
        else:
            repeat = tuple(raw_repeat)
        kc.repeat_delay, kc.repeat_interval = repeat
        raw_modifiers = (
            dict.__getitem__(c, "modifiers")
            if compatible_hello and dict.__contains__(c, "modifiers") else ()
        )
        try:
            hello_modifiers = self._validate_modifiers(raw_modifiers)
        except (TypeError, ValueError, UnicodeError) as exc:
            kc.mark_rejected(f"malformed hello modifiers: {exc}", raw_modifiers)
            # This configuration has no applied snapshot to restore.  Keep it
            # ineligible until a later bounded structured update succeeds;
            # merely recording a rejection while leaving ``valid`` true would
            # still allow its key events through the shared bootstrap map.
            kc.valid = False
            kc.current_modifiers = ()
        else:
            kc.current_modifiers = tuple(self._normalize_modifiers(kc, hello_modifiers))
        kc.current_group = 0
        keymap = dict.get(c, "keymap") if compatible_hello else None
        kc.delay = _mapping_bool(keymap, "delay")

    def add_new_client(self, ss, _c: typedict) -> None:
        config = getattr(ss, "keyboard_config", None)
        if not config or not self._source_present(ss):
            return
        self._keyboard_configs[ss] = config
        client_id = self._source_id(ss)
        if config.rejected or not config.valid:
            if config.rejected:
                self._record_rejection(config, client_id)
            return
        if config.delay:
            if self._validate_config(config, client_id):
                self._claim_owner(ss)
            return
        if self._keyboard_owner_source is not None and self._keyboard_owner_source is not ss:
            self._validate_config(config, client_id)
            return
        if not self._eligible(ss):
            # Readonly and recording sources cannot own or mutate the seat, but
            # validating their bounded RMLVO now makes any later policy change
            # deterministic and exposes unavailable configurations promptly.
            self._validate_config(config, client_id)
            return
        if not self.set_keymap(ss):
            # A normal non-owner, recorder or readonly source is expected to
            # lose the claim without disturbing the existing owner.  Only an
            # apply failure after this source actually claimed the vacant seat
            # needs promotion/bootstrap recovery.
            if self._keyboard_owner_source is ss:
                self._drop_owner()
                self._promote_owner(exclude=ss)

    def _record_rejection(self, config, client_id: str) -> None:
        self.last_rejected = {
            "client": client_id,
            "hash": config.rejected_hash,
            "reason": config.rejected,
        }
        log.warn("Warning: rejected Wayland keyboard configuration for client %s: %s",
                 client_id or "bootstrap", config.rejected)

    def _translation_configs(self) -> tuple:
        configs = [self.config, self.bootstrap_config]
        configs.extend(self._keyboard_configs.values())
        seen = set()
        unique = []
        for config in configs:
            if not config or id(config) in seen:
                continue
            seen.add(id(config))
            unique.append(config)
        return tuple(unique)

    def _clear_config_owners(self) -> None:
        for config in self._translation_configs():
            config.owner = None

    def _set_config_owner(self, config, client_id: str) -> None:
        # The native seat has exactly one effective keymap.  Clear metadata on
        # every retained source snapshot before publishing the one which was
        # actually installed (or the ownerless bootstrap snapshot).
        self._clear_config_owners()
        config.owner = client_id

    def _drop_owner(self) -> None:
        self._clear_config_owners()
        self._keyboard_owner_source = None
        self.keyboard_owner = ""

    def _settle_pressed_translations(self) -> None:
        for config in self._translation_configs():
            config.settle_pressed_translations()

    @staticmethod
    def _event_token(source, key_identity, name: str = "", keyval: int = 0) -> tuple[Any, Any]:
        return source, key_identity if key_identity is not None else (name, keyval)

    def _tracked_key_tokens(self) -> set[tuple[Any, Any]]:
        tracked = set(self._key_repeat_timers)
        tracked.update(self._settled_modifier_meanings)
        for holders in self._key_holders.values():
            tracked.update(holders)
        for source, source_modifiers in self._modifier_holders.items():
            for holders in source_modifiers.values():
                tracked.update((source, identity) for identity in holders)
        for source, config in self._keyboard_configs.items():
            tracked.update((source, identity) for identity in config.pressed_translation)
            tracked.update((source, identity) for identity in config.settled_translation)
        return tracked

    def _can_track_key(self, source, identity) -> bool:
        token = self._event_token(source, identity)
        tracked = self._tracked_key_tokens()
        if token in tracked:
            return True
        source_count = sum(1 for tracked_source, _identity in tracked if tracked_source is source)
        if source_count >= MAX_TRACKED_KEYS_PER_SOURCE:
            log("ignoring key press beyond the per-client Wayland tracking limit")
            return False
        if len(tracked) >= MAX_TRACKED_KEYS:
            log("ignoring key press beyond the shared Wayland tracking limit")
            return False
        return True

    def _cancel_source_repeat(self, source, key_identity) -> None:
        token = self._event_token(source, key_identity)
        timer = self._key_repeat_timers.pop(token, None)
        if timer:
            self._remove_repeat_timer(timer[0])

    def _cancel_source_repeats(self, source) -> None:
        for token, timer in tuple(self._key_repeat_timers.items()):
            if token[0] is source:
                self._key_repeat_timers.pop(token, None)
                self._remove_repeat_timer(timer[0])

    def _remove_repeat_timer(self, timer) -> None:
        try:
            self.source_remove(timer)
        except Exception:
            # Timer ownership is retired before calling the scheduler.  A
            # source-specific callback that escaped removal checks that
            # ownership and becomes a no-op.  More importantly, an unexpected
            # scheduler failure cannot interrupt the one-way native keymap
            # commit after install_keymap has swapped the seat keyboard.
            log("failed to remove Wayland keyboard repeat timer", exc_info=True)

    def cancel_key_repeat_timer(self) -> None:
        if timer := self.key_repeat_timer:
            self.key_repeat_timer = 0
            self._remove_repeat_timer(timer)
        timers = tuple(self._key_repeat_timers.values())
        self._key_repeat_timers.clear()
        for timer, _generation in timers:
            self._remove_repeat_timer(timer)

    def _held_modifier_names(self) -> frozenset[str]:
        return frozenset(
            modifier
            for source_modifiers in self._modifier_holders.values()
            for modifier, holders in source_modifiers.items()
            if holders
        )

    def _commit_settled_input(self, released_modifiers=()) -> None:
        for source, source_modifiers in self._modifier_holders.items():
            for modifier, identities in source_modifiers.items():
                for identity in identities:
                    self._settled_modifier_meanings[(source, identity)] = modifier
        self.cancel_key_repeat_timer()
        self.keys_pressed = {}
        self.keys_timedout = {}
        released = frozenset(released_modifiers)
        if released:
            for config in self._translation_configs():
                current_modifiers = self._normalize_modifiers(config, config.current_modifiers)
                config.current_modifiers = tuple(
                    modifier for modifier in current_modifiers if modifier not in released
                )
                config.remember_applied_runtime()
        self._key_holders.clear()
        self._modifier_holders.clear()
        self._keyboard_state_source = None
        self._settle_pressed_translations()

    def _settle_input(self, *, release_depressed: bool = False) -> None:
        keycodes = set(self.keys_pressed)
        held_modifiers = self._held_modifier_names()
        released_modifiers = set(held_modifiers)
        if release_depressed:
            released_modifiers.update(CANONICAL_MODIFIERS - LOCKED_MODIFIERS)
        modifiers = tuple(
            modifier for modifier in (self.device.get_modifiers() if self.device else ())
            if modifier not in released_modifiers
        )
        group = self.device.get_layout_group() if self.device else 0
        if self.device:
            keycodes.update(self.device.get_keycodes_down())
            self.device.clear_keys_pressed(tuple(sorted(keycodes)))
        self._commit_settled_input(released_modifiers)
        if self.device:
            self.device.update_modifiers(modifiers, group)
            self.server.compositor.flush()

    def _settle_source_input(self, source) -> None:
        """Release only keys whose final live holder is `source`."""
        self._clear_settled_modifiers(source)
        config = self._keyboard_configs.get(source)
        source_modifier_holders = self._modifier_holders.get(source, {})
        released_modifiers = frozenset(
            modifier for modifier, holders in source_modifier_holders.items() if holders
        )
        if config:
            # A complete mask may arrive with focus/pointer input without a
            # corresponding key press.  Once this source becomes ineligible,
            # none of its depressed state may be promoted later.  Locks are
            # persistent state rather than held keys and remain source-local.
            current_modifiers = self._normalize_modifiers(config, config.current_modifiers)
            released_modifiers |= frozenset(
                modifier for modifier in current_modifiers
                if modifier in CANONICAL_MODIFIERS and modifier not in LOCKED_MODIFIERS
            )
        releases = []
        for keycode, holders in tuple(self._key_holders.items()):
            source_tokens = tuple(token for token in holders if token[0] is source)
            holders.difference_update(source_tokens)
            if holders:
                continue
            self._key_holders.pop(keycode, None)
            releases.append(keycode)
        if releases and self.device:
            self.device.clear_keys_pressed(tuple(sorted(releases)))
        for keycode in releases:
            self.keys_pressed.pop(keycode, None)
            self.keys_timedout.pop(keycode, None)
        if config:
            config.settle_pressed_translations()
            if released_modifiers:
                config.current_modifiers = tuple(
                    modifier for modifier in current_modifiers
                    if modifier not in released_modifiers
                )
                config.remember_applied_runtime()
        self._modifier_holders.pop(source, None)
        self._cancel_source_repeats(source)
        if releases and self.device:
            self.server.compositor.flush()
        if self._keyboard_state_source is source:
            self._keyboard_state_source = None

    def _clear_settled_modifiers(self, source) -> None:
        for token in tuple(self._settled_modifier_meanings):
            if token[0] is source:
                self._settled_modifier_meanings.pop(token, None)

    @staticmethod
    def _identity_group(old, old_group: int, new) -> int:
        if (not old or not new or not old.layout_groups or not new.layout_groups
                or not old.layouts or not new.layouts):
            return 0
        if old_group < 0 or old_group >= len(old.layouts):
            return 0
        identity = old.layouts[old_group], old.variants[old_group]
        occurrence = sum(
            1 for index in range(old_group + 1)
            if (old.layouts[index], old.variants[index]) == identity
        ) - 1
        matches = [
            index for index, value in enumerate(zip(new.layouts, new.variants))
            if value == identity
        ]
        if matches:
            return matches[min(occurrence, len(matches) - 1)]
        return 0

    def _compile_candidate(self, config):
        rmlvo = config.rmlvo
        candidate = self.device.compile_keymap(
            rmlvo.rules, rmlvo.model, rmlvo.layout, rmlvo.variant, rmlvo.options,
        )
        if candidate.group_count != len(rmlvo.layouts):
            candidate.cleanup()
            raise ValueError("compiled group count does not match the requested layouts")
        return candidate

    def _validate_config(self, config, client_id: str) -> bool:
        if not config.enabled or not config.valid:
            if config.rejected:
                self._record_rejection(config, client_id)
            return False
        keymap_hash = config.get_hash()
        if config.validated_hash == keymap_hash and config.compiled_groups:
            groups = config.compiled_groups
            config.mark_validated(groups)
            if self.last_rejected.get("client") == client_id:
                self.last_rejected = {}
            return True
        candidate = None
        try:
            candidate = self._compile_candidate(config)
            config.mark_validated(candidate.group_count)
        except Exception as exc:
            config.mark_compile_rejected(str(exc))
            self._record_rejection(config, client_id)
            return False
        finally:
            if candidate is not None:
                try:
                    candidate.cleanup()
                except Exception:
                    log("failed to release validated Wayland keymap candidate", exc_info=True)
        if self.last_rejected.get("client") == client_id:
            self.last_rejected = {}
        log("validated non-owner Wayland keyboard configuration hash=%s groups=%i client=%s",
            keymap_hash, config.compiled_groups, client_id or "unnamed client")
        return True

    def _apply_config(self, config, client_id: str) -> bool:
        self.last_apply_result = ""
        if not config.enabled or not config.valid:
            if config.rejected:
                self._record_rejection(config, client_id)
            return False
        recovering_rejection = bool(config.rejected)
        keymap_hash = config.get_hash()
        active_groups = self.device.get_layout_group_count() if self.device else 0
        if self.config_hash == keymap_hash and active_groups and getattr(self.config, "compiled_groups", 0):
            self._set_config_owner(config, client_id)
            config.mark_applied(active_groups)
            self.set_current_config(config)
            self.effective_rmlvo = config.rmlvo
            if (client_id and not recovering_rejection
                    and self.last_rejected.get("client") == client_id):
                self.last_rejected = {}
            self.last_apply_result = "identical"
            log("Wayland keyboard mapping already configured (skipped)")
            return True

        candidate = None
        device_down: tuple[int, ...] = ()
        previous_state_source = self._keyboard_state_source
        try:
            candidate = self._compile_candidate(config)

            old_rmlvo = self.effective_rmlvo
            old_group = self.device.get_layout_group() if self.device else 0
            old_modifiers = self.device.get_modifiers() if self.device else ()
            held_modifiers = self._held_modifier_names()
            replacement_modifiers = tuple(
                modifier for modifier in old_modifiers if modifier not in held_modifiers
            )
            replacement_group = self._identity_group(old_rmlvo, old_group, config.rmlvo) if old_rmlvo else 0
            device_down = tuple(self.device.get_keycodes_down()) if self.device else ()
            keycodes = set(self.keys_pressed)
            keycodes.update(device_down)
            # `wlr_keyboard_set_keymap` is transactional but may still reject
            # an allocation.  Do not emit observable release/re-press pairs
            # until that final native commit has succeeded.
            groups = self.device.install_keymap(
                candidate, tuple(sorted(keycodes)), replacement_modifiers, replacement_group,
            )
        except Exception as exc:
            config.mark_compile_rejected(str(exc), prefer_applied=True)
            self._record_rejection(config, client_id)
            return False
        finally:
            if candidate is not None:
                try:
                    candidate.cleanup()
                except Exception:
                    log("failed to release Wayland keymap candidate", exc_info=True)
        self._commit_settled_input(held_modifiers)
        if previous_state_source is not None and self._eligible(previous_state_source):
            # install_keymap preserves the bounded modifier/group state after
            # settling physical holds.  Preserve who supplied that remaining
            # state as well; _activate_owner_state decides whether it belongs
            # to the owner being applied or to another input client.
            self._keyboard_state_source = previous_state_source
        replacement_group = replacement_group if replacement_group < groups else 0
        self._set_config_owner(config, client_id)
        config.mark_applied(groups)
        self.set_current_config(config)
        self.effective_rmlvo = config.rmlvo
        if (client_id and not recovering_rejection
                and self.last_rejected.get("client") == client_id):
            self.last_rejected = {}
        self.last_apply_result = "installed"
        log.info("applied Wayland keyboard configuration hash=%s groups=%i owner=%s",
                 keymap_hash, groups, client_id or "bootstrap")
        self.server.compositor.flush()
        return True

    def _normalize_modifiers(self, config, modifiers: Sequence[str]) -> list[str]:
        meanings = getattr(config, "modifier_meanings", {}) if config else {}
        normalized = []
        for modifier in modifiers or ():
            canonical = meanings.get(modifier, modifier)
            canonical = MODIFIER_ALIASES.get(canonical, canonical)
            if canonical in CANONICAL_MODIFIERS and canonical not in normalized:
                normalized.append(canonical)
        return normalized

    @staticmethod
    def _modifier_for_key(config, keyname: str) -> str:
        meanings = getattr(config, "modifier_meanings", {}) if config else {}
        canonical = meanings.get(keyname, DEFAULT_MODIFIER_MEANINGS.get(keyname, keyname))
        canonical = MODIFIER_ALIASES.get(canonical, canonical)
        return canonical if canonical in CANONICAL_MODIFIERS else ""

    def _effective_modifiers(self, config, modifiers: Sequence[str]) -> list[str]:
        state = self._normalize_modifiers(config, modifiers)
        for holder_modifiers in self._modifier_holders.values():
            for modifier, holders in holder_modifiers.items():
                if holders and modifier not in state:
                    state.append(modifier)
        return [modifier for modifier in MODIFIER_ORDER if modifier in state]

    def _post_modifier_event(self, source, config, keyname: str, pressed: bool,
                             client_keycode: int, keyval: int, modifiers: Sequence[str],
                             canonical: str = "") -> None:
        canonical = canonical or self._modifier_for_key(config, keyname)
        identity = config.key_identity(client_keycode, keyname, keyval)
        source_holders = self._modifier_holders.get(source, {})
        if not pressed:
            # Modifier meanings can change while a key is held.  A release must
            # retire the classification pinned by its press, not a new mapping.
            canonical = next(
                (modifier for modifier, holders in source_holders.items() if identity in holders),
                canonical,
            )
        if not canonical or not self.device:
            return
        state = self._normalize_modifiers(config, modifiers)
        if canonical in LOCKED_MODIFIERS:
            # Client event masks describe the state immediately before the key
            # event.  Locks toggle on press and remain unchanged on release.
            if pressed:
                if canonical in state:
                    state.remove(canonical)
                else:
                    state.append(canonical)
        else:
            if pressed:
                source_holders = self._modifier_holders.setdefault(source, {})
                holders = source_holders.setdefault(canonical, set())
                holders.add(identity)
            else:
                holders = source_holders.get(canonical, set())
                holders.discard(identity)
            if holders:
                if canonical not in state:
                    state.append(canonical)
            else:
                if canonical in state:
                    state.remove(canonical)
                source_holders.pop(canonical, None)
            if not source_holders:
                self._modifier_holders.pop(source, None)

        config.current_modifiers = tuple(self._normalize_modifiers(config, state))
        config.remember_applied_runtime()
        effective = self._effective_modifiers(config, state)
        self.device.update_modifiers(effective, self.device.get_layout_group())
        self._keyboard_state_source = source
        self.server.compositor.flush()

    def _activate_owner_state(self, source, *, preserve_group: bool = False) -> None:
        config = self._keyboard_configs.get(source)
        if not config:
            return
        state_source = source
        if not preserve_group:
            delay = int(getattr(config, "repeat_delay", 500))
            interval = int(getattr(config, "repeat_interval", 30))
            self.set_keyboard_repeat(delay, interval)
        if preserve_group and self.device:
            modifiers = list(self.device.get_modifiers())
            # A non-owner is allowed to type through its own group identity.
            # Replacing the owner's map must not relabel that live modifier
            # snapshot as owner state, otherwise departure of the real source
            # cannot retire its depressed modifiers.
            previous_state_source = self._keyboard_state_source
            if (previous_state_source is not None
                    and self._eligible(previous_state_source)):
                state_source = previous_state_source
        else:
            modifiers = self._normalize_modifiers(config, getattr(config, "current_modifiers", ()))
            config.current_modifiers = tuple(modifiers)
        if preserve_group and self.device:
            server_group = self.device.get_layout_group()
        else:
            client_group = self._safe_client_group(config, getattr(config, "current_group", 0))
            preferences = self._group_preferences(config, client_group)
            server_group = preferences[0] if preferences else 0
        if preserve_group and state_source is source:
            # A runtime reorder remaps the same layout/variant identity to a
            # different numeric group.  Persist the actual device state only
            # when it still belongs to this owner; foreign source-local state
            # must remain attached to that source instead.
            config.current_modifiers = tuple(self._normalize_modifiers(config, modifiers))
            config.current_group = server_group
        if self.device:
            effective = self._effective_modifiers(config, modifiers)
            self.device.update_modifiers(effective, server_group)
            self._keyboard_state_source = state_source
            config.remember_applied_runtime()
            self.server.compositor.flush()

    def _reapply_source_state(self, source) -> bool:
        config = self._keyboard_configs.get(source)
        if not config or not self._eligible(source) or not self.device:
            return False
        modifiers = self._normalize_modifiers(config, getattr(config, "current_modifiers", ()))
        config.current_modifiers = tuple(modifiers)
        # This helper only reapplies the surviving current state after another
        # source's modifier holders have been removed.  Keep the actual active
        # group: a foreign source identity may be absent from the owner's map,
        # or duplicate occurrences may not be cardinality-aligned, so its
        # client-local numeric group cannot losslessly reconstruct this value.
        server_group = self.device.get_layout_group()
        group_count = self.device.get_layout_group_count()
        if (type(server_group) is not int
                or server_group < 0 or server_group >= group_count):
            server_group = 0
        source_group = self._identity_group(self.effective_rmlvo, server_group, config.rmlvo)
        config.current_group = self._safe_client_group(config, source_group)
        self.device.update_modifiers(self._effective_modifiers(config, modifiers), server_group)
        self._keyboard_state_source = source
        config.remember_applied_runtime()
        self.server.compositor.flush()
        return True

    def _promote_owner(self, exclude=None) -> bool:
        # Dict insertion order is the acceptance order.  Source-local counters
        # are not a stable ordering primitive across client implementations.
        eligible = (
            source for source in self._keyboard_sources()
            if source is not exclude and self._eligible(source, require_valid=True)
        )
        for source in eligible:
            config = self._keyboard_configs.get(source)
            if not config or getattr(config, "delay", False):
                continue
            if not self._claim_owner(source):
                continue
            applied = self._apply_config(config, self.keyboard_owner)
            # A non-owner may advertise an unavailable update after previously
            # owning a successfully compiled map.  mark_compile_rejected rolls
            # it back to that applied state; retry that exact hash once rather
            # than abandoning the only usable remaining input client.
            if (not applied and config.valid and config.applied_hash
                    and config.get_hash() == config.applied_hash):
                log("retrying last known-good Wayland keyboard map for promoted client %s",
                    self.keyboard_owner)
                applied = self._apply_config(config, self.keyboard_owner)
            if applied:
                self._activate_owner_state(source)
                return True
            self._drop_owner()
        if self.bootstrap_config:
            if self._apply_config(self.bootstrap_config, ""):
                self.set_keyboard_repeat(500, 30)
                if self.device:
                    self.device.update_modifiers((), 0)
                    self._keyboard_state_source = None
                    self.server.compositor.flush()
            else:
                log.error("Error: failed to restore the known-good Wayland bootstrap keymap")
        return False

    def _reconcile_owner(self, *, promote: bool = True) -> bool:
        owner = self._keyboard_owner_source
        if owner is not None and self._eligible(owner):
            return True
        if owner is not None:
            self._settle_input()
        self._drop_owner()
        return self._promote_owner() if promote else False

    def set_keymap(self, server_source, force=False) -> bool:
        del force  # identical normalized maps are suppressed even for forced legacy updates
        owner = self._keyboard_owner_source
        # If an owner became stale before its normal protocol cleanup, preserve
        # the same acceptance-order promotion used by that cleanup.  With no
        # previous owner, keep the slot available for the first unvalidated
        # client whose map is about to be compiled below.
        promote = owner is not None and not self._eligible(owner)
        self._reconcile_owner(promote=promote)
        if not self._claim_owner(server_source):
            log("not replacing the shared Wayland keymap for non-owner %s", server_source)
            return False
        config = self._keyboard_configs.get(server_source)
        if not config:
            return False
        client_id = self._source_id(server_source)
        preserve_group = (
            self.config is config and config.owner == client_id and bool(config.applied_hash)
        )
        config.client_id = client_id
        if not self._apply_config(config, client_id):
            return False
        self._activate_owner_state(server_source, preserve_group=preserve_group)
        return True

    @staticmethod
    def _safe_client_group(config, group: int) -> int:
        if not config.rmlvo.layout_groups:
            return 0
        if type(group) is not int:
            return 0
        if group < 0 or group >= len(config.rmlvo.layouts):
            return 0
        return group

    def _group_preferences(self, config, group: int) -> tuple[int, ...]:
        active = self.effective_rmlvo
        if not active:
            return (0,)
        source = config.rmlvo
        source_group = self._safe_client_group(config, group)
        identity = source.layouts[source_group], source.variants[source_group]
        occurrence = sum(
            1 for index in range(source_group + 1)
            if (source.layouts[index], source.variants[index]) == identity
        ) - 1
        matches = [
            index for index, value in enumerate(zip(active.layouts, active.variants))
            if value == identity
        ]
        if matches:
            preferred = matches[min(occurrence, len(matches) - 1)]
            return (preferred,) + tuple(index for index in range(len(active.layouts)) if index != preferred)
        # A foreign layout identity cannot select a raw client keycode. Search
        # the installed groups by actual keysym in deterministic order instead.
        return tuple(range(len(active.layouts)))

    def resolve_keycode(self, config, keyname: str, modifiers: list[str],
                        keyval: int, keystr: str, group: int) -> tuple[int, int]:
        if not self.device:
            return -1, 0
        if type(keyval) is not int or keyval < 0 or keyval > MAX_XKB_KEYSYM:
            # Preserve the portable key string/name fallbacks without passing
            # an unbounded Python integer through the Cython xkb_keysym_t edge.
            keyval = 0
        preferences = self._group_preferences(config, group)
        return self.device.resolve_keycode(
            keyval, keyname, preferences, modifiers, keystr, self._held_modifier_names(),
        )

    def get_keycode(self, ss, client_keycode: int, keyname: str,
                    pressed: bool, modifiers: list, keyval: int, keystr: str, group: int):
        config = self._keyboard_configs.get(ss)
        if not config or not self._eligible(ss):
            return -1, 0
        source_group = self._safe_client_group(config, group)
        normalized = self._normalize_modifiers(config, modifiers)
        initial_effective = self._effective_modifiers(config, normalized)
        resolve_modifiers = list(initial_effective)
        modifier_only = client_keycode < 0 and not keyname
        if not modifier_only:
            config.current_group = source_group
        keycode, server_group = config.get_keycode(
            client_keycode, keyname, pressed, resolve_modifiers, keyval, keystr, source_group,
        )
        # The native resolver may infer a missing level selector.  Project
        # only its additions/removals back onto this client's state: modifiers
        # contributed by another held key must remain global without becoming
        # part of this client's snapshot.
        added = tuple(modifier for modifier in resolve_modifiers if modifier not in initial_effective)
        removed = frozenset(modifier for modifier in initial_effective if modifier not in resolve_modifiers)
        source_modifiers = [modifier for modifier in normalized if modifier not in removed]
        source_modifiers.extend(modifier for modifier in added if modifier not in source_modifiers)
        source_modifiers = [modifier for modifier in MODIFIER_ORDER if modifier in source_modifiers]
        modifiers[:] = source_modifiers
        config.current_modifiers = tuple(source_modifiers)
        config.remember_applied_runtime()
        if keycode > 0 and self.device:
            effective = self._effective_modifiers(config, source_modifiers)
            self.device.update_modifiers(effective, server_group)
            self._keyboard_state_source = ss
        log("get_keycode: pressed=%s keyname=%r keyval=%i client-keycode=%i client-group=%i -> %i/%i",
            pressed, keyname, keyval, client_keycode, source_group, keycode, server_group)
        return keycode, server_group

    def _handle_key(self, wid: int, pressed: bool, name: str, keyval: int, keycode: int,
                    modifiers: list, is_mod: bool = False, sync: bool = True,
                    source=None, key_identity=None):
        # One wlroots keyboard is shared by all input clients.  The generic
        # manager tracks only a physical server keycode, so without this holder
        # set one client's release could unpress a key another client still
        # holds.  Unsynchronised non-modifier presses are immediately released
        # by the generic path and therefore never become holders.
        config = self._keyboard_configs.get(source)
        is_mod = is_mod or bool(self._modifier_for_key(config, name))
        if source is None or (pressed and not sync and not is_mod):
            return super()._handle_key(
                wid, pressed, name, keyval, keycode, modifiers,
                is_mod, sync, source, key_identity,
            )
        token = self._event_token(source, key_identity, name, keyval)
        if pressed and keycode not in self._key_holders:
            device_keycodes = set(self.device.get_keycodes_down()) if self.device else set()
            device_keycodes.update(self._key_holders)
            capacity_fn = getattr(self.device, "get_keycode_capacity", None)
            capacity = int(capacity_fn()) if callable(capacity_fn) else 0
            if capacity > 0 and keycode not in device_keycodes and len(device_keycodes) >= capacity:
                # wlr_keyboard_notify_key cannot retain another distinct key in
                # its fixed keycodes array.  Do not deliver a press which would
                # disappear from a subsequent focus-enter state.  A second wire
                # identity for an already-held server keycode remains valid.
                if config:
                    config.discard_pressed_translation(key_identity)
                log("ignoring key press beyond the wlroots held-keycode capacity")
                return None
        holders = self._key_holders.setdefault(keycode, set())
        if pressed:
            if wid:
                window = self.get_subsystem("window")
                if window is not None and not window.get_window(wid):
                    if not holders:
                        self._key_holders.pop(keycode, None)
                    return super()._handle_key(
                        wid, pressed, name, keyval, keycode, modifiers,
                        is_mod, sync, source, key_identity,
                    )
            try:
                return super()._handle_key(
                    wid, pressed, name, keyval, keycode, modifiers,
                    is_mod, sync, source, key_identity,
                )
            finally:
                # Generic handling records/presses before it schedules repeat.
                # Failed timer admission (or a later notification) must not
                # leave that press without a source holder able to release it.
                if keycode in self.keys_pressed:
                    holders.add(token)
                elif not holders:
                    self._key_holders.pop(keycode, None)
        if token not in holders:
            if not holders:
                self._key_holders.pop(keycode, None)
            self._cancel_source_repeat(source, key_identity)
            log("ignoring unmatched key release from %s for shared keycode %i", source, keycode)
            return None
        holders.discard(token)
        if holders:
            self._cancel_source_repeat(source, key_identity)
            return None
        self._key_holders.pop(keycode, None)
        # Sync can change between a press and its release.  The generic path
        # schedules cancellation only when the release is still synchronized,
        # so retire this source/key timer unconditionally here.
        self._cancel_source_repeat(source, key_identity)
        return super()._handle_key(
            wid, pressed, name, keyval, keycode, modifiers,
            is_mod, sync, source, key_identity,
        )

    def _key_repeat(self, wid: int, pressed: bool, keyname: str, keyval: int, keycode: int,
                    modifiers: list, is_mod: bool, delay_ms: int = 0,
                    source=None, key_identity=None) -> None:
        if source is None:
            super()._key_repeat(
                wid, pressed, keyname, keyval, keycode, modifiers,
                is_mod, delay_ms, source, key_identity,
            )
            return
        token = self._event_token(source, key_identity, keyname, keyval)
        self._cancel_source_repeat(source, token[1])
        if not pressed:
            return
        delay_ms = min(2000, max(1000, delay_ms))
        generation = object()
        timer = self.timeout_add(
            delay_ms, self._source_key_repeat_timeout,
            generation, monotonic(), delay_ms, wid, keyname, keyval, keycode,
            list(modifiers), is_mod, source, token[1],
        )
        self._key_repeat_timers[token] = timer, generation

    def _source_key_repeat_timeout(self, generation, when, delay_ms: int, wid: int,
                                   keyname: str, keyval: int, keycode: int,
                                   modifiers: list, is_mod: bool, source, key_identity) -> None:
        token = self._event_token(source, key_identity, keyname, keyval)
        current = self._key_repeat_timers.get(token)
        if not current or current[1] is not generation:
            return
        self._key_repeat_timers.pop(token, None)
        now = monotonic()
        log("source key repeat timeout for %s / %s after %sms", keyname, keycode, delay_ms)
        self._handle_key(
            wid, False, keyname, keyval, keycode, modifiers,
            is_mod, True, source, key_identity,
        )
        if keycode not in self._key_holders:
            self.keys_timedout[keycode] = now

    def clear_keys_pressed(self, *_args) -> None:
        # Also clear per-source holders and translation tombstones.  The base
        # method knows only the global keycode map and would leave stale holders
        # after last-client-exited.
        self._settle_input()

    def fake_key(self, keycode: int, press: bool) -> None:
        log("fake_key(%i, %s)", keycode, press)
        if self.device:
            self.device.reapply_modifiers()
        super().fake_key(keycode, press)
        self.server.compositor.flush()

    def update_keyboard_modifiers(self, modifiers: Sequence[str], group: int = -1, source=None) -> None:
        if not self.device:
            return
        if source is not None and not self._eligible(source):
            return
        config = self._keyboard_configs.get(source, self.config)
        modifiers = self._normalize_modifiers(config, modifiers)
        if source is not None and config:
            config.current_modifiers = tuple(modifiers)
        # Only the exact integer -1 is the established "group absent"
        # sentinel.  bool, numeric strings, floats and other coercible wire
        # values select the safe group zero instead of the current group.
        group_absent = type(group) is int and group == -1
        if type(group) is not int:
            group = 0
        if group_absent:
            group = self.device.get_layout_group()
            if source is not None and config:
                # Focus and pointer packets often carry only a modifier mask.
                # Store the unchanged device group in this source's ordering,
                # otherwise a later source-state restore can reinterpret its
                # stale numeric group after reordered or duplicate layouts.
                source_group = self._identity_group(self.effective_rmlvo, group, config.rmlvo)
                config.current_group = self._safe_client_group(config, source_group)
        elif source is not None and config:
            if group < 0:
                group = 0
            config.current_group = self._safe_client_group(config, group)
            preferences = self._group_preferences(config, group)
            group = preferences[0] if preferences else 0
        elif group < 0:
            group = 0
        effective = self._effective_modifiers(config, modifiers)
        self.device.update_modifiers(effective, group)
        if source is not None and config:
            config.remember_applied_runtime()
            self._keyboard_state_source = source
        self.server.compositor.flush()

    def do_process_keyboard_event(self, proto, wid: int, keyname: str, pressed: bool, kattrs: dict) -> None:
        ss = self.get_server_source(proto)
        attrs = typedict(kattrs)
        if (dict.__contains__(attrs, "group")
                and type(dict.__getitem__(attrs, "group")) is not int):
            # typedict.intget accepts bool (an int subclass) and numeric
            # strings.  A group is an exact wire index, not a coercible value;
            # normalize invalid current keyboard-event values before the
            # generic manager reads this mapping again.
            attrs["group"] = 0
        if not ss or not self._eligible(ss):
            # A policy transition may have settled a previously injected key.
            # Consume its eventual wire release while input is disabled so it
            # cannot leave a tombstone that eats the next genuine press.
            config = self._keyboard_configs.get(ss)
            if config and not pressed:
                identity = config.key_identity(
                    attrs.intget("keycode", 0), keyname, attrs.intget("keyval", 0),
                )
                token = self._event_token(ss, identity)
                canonical = self._settled_modifier_meanings.pop(token, "") or self._modifier_for_key(
                    config, keyname,
                )
                config.discard_key_release(
                    attrs.intget("keycode", 0), keyname, attrs.intget("keyval", 0),
                )
                if canonical and canonical not in LOCKED_MODIFIERS:
                    # Keep an ineligible source's private runtime snapshot
                    # current without touching the shared wlroots device.  If
                    # this release is ignored completely, an initially
                    # readonly client's hello-time depressed mask can be
                    # resurrected when that source later becomes writable.
                    config.current_modifiers = tuple(
                        modifier for modifier in self._normalize_modifiers(
                            config, config.current_modifiers,
                        ) if modifier != canonical
                    )
                    config.remember_applied_runtime()
            log("ignoring keyboard event from disabled, recording, readonly or stale source %s", ss)
            return
        if pressed and wid:
            window = self.get_subsystem("window")
            if window is not None and not window.get_window(wid):
                log("window %s is gone, ignoring key press before translation", wid)
                return
        config = self._keyboard_configs[ss]
        client_keycode = attrs.intget("keycode", 0)
        keyval = attrs.intget("keyval", 0)
        identity = config.key_identity(client_keycode, keyname, keyval)
        token = self._event_token(ss, identity)
        modifier_only = client_keycode < 0 and not keyname
        if pressed and not modifier_only and not self._can_track_key(ss, identity):
            return
        settled_modifier = self._settled_modifier_meanings.get(token, "")
        had_key_holder = any(token in holders for holders in self._key_holders.values())
        canonical = settled_modifier or self._modifier_for_key(config, keyname)
        if pressed and had_key_holder and canonical in LOCKED_MODIFIERS:
            log("ignoring duplicate held lock key press from %s for %s", ss, keyname)
            return
        # Win32 AltGr emulation deliberately sends a modifier-only event with
        # keycode/keyval/group=-1.  It must update state without resolving or
        # injecting a physical key.
        if modifier_only and "modifiers" in attrs:
            self.update_keyboard_modifiers(
                attrs.strtupleget("modifiers", ()), attrs.intget("group", -1), source=ss,
            )
        # ``typedict`` subclasses ``dict``.  Pure Python accepts it at the
        # generic manager boundary, but the full-cython leg enforces the base
        # method's ``kattrs: dict`` annotation as an exact builtin.  Pass an
        # exact copy containing the sanitized group while retaining ``attrs``
        # for the source-local post-event bookkeeping below.
        super().do_process_keyboard_event(proto, wid, keyname, pressed, dict(attrs))
        accepted_press = (
            pressed and not had_key_holder
            and any(token in holders for holders in self._key_holders.values())
        )
        # A source can become writable again before the release for a key that
        # was settled by readonly mode arrives.  Its translation tombstone is
        # deliberately cleared on re-enable so a lost release cannot suppress
        # the next genuine press.  Reconcile every canonical modifier release:
        # the packet mask is pre-event state, and aggregate holders below keep
        # the same modifier depressed when another client still holds it.
        accepted_release = not pressed and bool(canonical)
        if canonical and accepted_press:
            # Pin every modifier classification at press time.  Depressed
            # modifiers also live in _modifier_holders, but lock keys do not;
            # this token therefore has to survive a keymap/mod_meanings change
            # so their eventual release cannot be reclassified by the new map.
            self._settled_modifier_meanings[token] = canonical
        if canonical and (accepted_press or accepted_release):
            self._post_modifier_event(
                ss, config, keyname, pressed, client_keycode, keyval,
                attrs.strtupleget("modifiers", ()), canonical,
            )
        if not pressed:
            self._settled_modifier_meanings.pop(token, None)

    def set_keyboard_layout_group(self, grp: int) -> None:
        if self.device:
            self.device.set_layout_group(grp)

    def _process_layout_changed(self, proto, packet) -> None:
        assert BACKWARDS_COMPATIBLE
        ss = self.get_server_source(proto)
        if not ss:
            return
        try:
            layout = packet.get_str(1)
            variant = packet.get_str(2)
            options = packet.get_str(3) if len(packet) >= 4 else None
            backend = packet.get_str(4) if len(packet) >= 6 else ""
            name = packet.get_str(5) if len(packet) >= 6 else ""
        except (IndexError, TypeError, ValueError, UnicodeError) as exc:
            self._reject_structured_packet(proto, "layout-changed", str(exc))
            return
        self.set_backend(ss, backend, name)
        config = getattr(ss, "keyboard_config", None)
        if config:
            config.set_layout(layout, variant, options)
        if not config:
            return
        config.delay = False
        client_id = self._source_id(ss)
        if config.rejected or not config.valid:
            self._record_rejection(config, client_id)
            self._recover_unapplied_owner(ss, config)
            return
        owner = self._keyboard_owner_source
        if owner is not None and not self._eligible(owner):
            self._reconcile_owner()
            owner = self._keyboard_owner_source
        # A readonly client can later become writable without reconnecting or
        # resending its keymap.  Keep its bounded, compiled source-local state
        # current, but never let an ineligible source mutate the shared seat.
        can_apply = self._eligible(ss) and (owner is ss or owner is None)
        if can_apply:
            if not self.set_keymap(ss, force=True):
                self._recover_unapplied_owner(ss, config)
            return
        self._validate_config(config, client_id)

    def _recover_unapplied_owner(self, source, config) -> None:
        if self._keyboard_owner_source is source and not getattr(config, "applied_hash", ""):
            # A delayed owner can send input while the bootstrap map is still
            # active.  Settle it before relinquishing ownership; an identical
            # bootstrap re-apply would otherwise leave native held keys behind.
            self._settle_input()
            config.valid = False
            self._drop_owner()
            self._promote_owner(exclude=source)

    def _reject_structured_packet(self, proto, packet_type: str, reason: str, value=None) -> None:
        ss = self.get_server_source(proto)
        config = self._keyboard_configs.get(ss)
        if not config:
            return
        config.delay = False
        config.mark_rejected(f"malformed {packet_type}: {reason}", value)
        self._record_rejection(config, self._source_id(ss))
        self._recover_unapplied_owner(ss, config)

    @staticmethod
    def _validate_modifiers(value) -> tuple[str, ...]:
        # The value is untrusted wire data.  Exact builtins let the length
        # guard run before iteration without invoking subclass hooks, and the
        # 32-item limit bounds every later normalization/copy.
        value_type = type(value)
        if (value_type is not tuple and value_type is not list) or len(value) > 32:
            raise ValueError("modifiers must be a bounded sequence")
        result = []
        for modifier in value:
            if (type(modifier) is not str or len(modifier) > MAX_NAME_SIZE
                    or len(modifier.encode("utf-8")) > MAX_NAME_SIZE
                    or any(ord(char) < 0x20 or ord(char) == 0x7f for char in modifier)):
                raise ValueError("invalid modifier name")
            result.append(modifier)
        return tuple(result)

    def _process_structured_keymap(self, proto, props: dict, force: bool, packet_type: str) -> None:
        ss = self.get_server_source(proto)
        config = self._keyboard_configs.get(ss)
        if not config or not config.enabled:
            return
        log.info("received Wayland structured keymap packet=%s", packet_type)
        raw_direct = dict.get(props, "keymap")
        direct = raw_direct if _dict_compatible(raw_direct) else props
        modifier_props = props if dict.__contains__(props, "modifiers") else direct
        modifiers_present = dict.__contains__(modifier_props, "modifiers")
        try:
            raw_modifiers = dict.__getitem__(modifier_props, "modifiers") if modifiers_present else ()
            packet_modifiers = self._validate_modifiers(raw_modifiers) if modifiers_present else ()
        except (TypeError, ValueError, UnicodeError) as exc:
            self._reject_structured_packet(proto, packet_type, str(exc), raw_modifiers)
            return
        if packet_type == "keymap-changed":
            config.parse_options(props)
        else:
            config.parse(props)
        if config.rejected or not config.valid:
            self._record_rejection(config, self._source_id(ss))
            config.delay = False
            self._recover_unapplied_owner(ss, config)
            return
        config.delay = False
        client_id = self._source_id(ss)
        normalized_packet_modifiers = tuple(
            self._normalize_modifiers(config, packet_modifiers)
        ) if modifiers_present else ()
        owner = self._keyboard_owner_source
        if owner is not None and not self._eligible(owner):
            self._reconcile_owner()
            owner = self._keyboard_owner_source
        # Readonly is a dynamic per-client setting.  Validate and retain the
        # latest source-local RMLVO for deterministic later promotion, without
        # installing it until this source is eligible to own the seat.
        can_apply = self._eligible(ss) and (owner is ss or owner is None)
        if not can_apply:
            if not self._validate_config(config, client_id):
                return
            if modifiers_present:
                config.current_modifiers = normalized_packet_modifiers
                config.remember_applied_runtime()
            return
        if modifiers_present:
            config.current_modifiers = normalized_packet_modifiers
        if not self.set_keymap(ss, force):
            self._recover_unapplied_owner(ss, config)
            return
        if modifiers_present and self.device:
            self.update_keyboard_modifiers(normalized_packet_modifiers, source=ss)
        groups = self.device.get_layout_group_count() if self.device else 0
        log.info(
            "accepted Wayland structured keymap packet=%s representation=%s hash=%s groups=%i owner=%s result=%s",
            packet_type, getattr(config, "representation", "legacy"), config.get_hash(), groups,
            self.keyboard_owner or "unnamed client", self.last_apply_result or "identical",
        )

    def _process_keymap_changed(self, proto, packet: Packet) -> None:
        assert BACKWARDS_COMPATIBLE
        try:
            raw_props = packet[1]
            if not _dict_compatible(raw_props):
                raise TypeError("payload must be a dictionary")
            props = raw_props
            raw_force = packet[2] if len(packet) > 2 else True
            if type(raw_force) is not bool:
                raise TypeError("force must be a boolean")
            force = raw_force
        except (IndexError, TypeError, ValueError) as exc:
            self._reject_structured_packet(proto, "keymap-changed", str(exc))
            return
        self._process_structured_keymap(proto, props, force, "keymap-changed")

    def _process_config(self, proto, packet: Packet) -> None:
        try:
            raw_props = packet[1]
            if not _dict_compatible(raw_props):
                raise TypeError("payload must be a dictionary")
            props = raw_props
            raw_force = dict.get(raw_props, "force", False)
            if type(raw_force) is not bool:
                raise TypeError("force must be a boolean")
        except (IndexError, TypeError, ValueError) as exc:
            self._reject_structured_packet(proto, "keyboard-config", str(exc))
            return
        self._process_structured_keymap(proto, props, raw_force, "keyboard-config")

    def _process_key_action(self, proto, packet: Packet) -> None:
        """Legacy groups are untrusted signed integers; manager fallback bounds them."""
        assert BACKWARDS_COMPATIBLE
        wid = packet.get_wid()
        keyname = packet.get_str(2)
        pressed = packet.get_bool(3)
        modifiers = list(packet.get_strs(4))
        keyval = packet.get_i32(5)
        keystr = packet.get_str(6)
        keycode = packet.get_i32(7)
        try:
            raw_group = packet[8]
            if type(raw_group) is not int:
                raise TypeError("group must be an integer")
            group = packet.get_i32(8)
        except (IndexError, TypeError, ValueError, OverflowError):
            group = 0
        self.do_process_keyboard_event(proto, wid, keyname, pressed, {
            "modifiers": modifiers,
            "keyval": keyval,
            "string": keystr,
            "keycode": keycode,
            "group": group,
        })

    def get_keyboard_info(self) -> dict[str, Any]:
        info = super().get_keyboard_info()
        info["owner"] = self.keyboard_owner
        if self.effective_rmlvo:
            info["effective-rmlvo"] = self.effective_rmlvo.as_dict()
            info["compiled-groups"] = self.device.get_layout_group_count() if self.device else 0
        if self.device:
            info["current-group"] = self.device.get_layout_group()
        if self.last_rejected:
            info["rejected-configuration"] = dict(self.last_rejected)
        return info

    def client_exited(self, _server, source) -> None:
        # Client cleanup runs from the protocol reader thread (see the base
        # `KeyboardManager.client_exited`), but the seat and the native
        # `wlr_keyboard` belong to the main thread: release and reinstall there.
        self.idle_add(self._retire_exited_source, source)

    def _retire_exited_source(self, source) -> None:
        # ClientSession removed this exact source before emitting the signal
        # and queues this call ahead of cleanup_protocol's. Never use the
        # generic all-clients key release for one departed shared-key holder.
        if source in self._keyboard_configs:
            self._retire_keyboard_sources((source,))

    def cleanup_protocol(self, _protocol) -> None:
        # Dispatched from the same reader thread as client-exited.
        self.idle_add(self._retire_departed_sources)

    def _retire_departed_sources(self) -> None:
        current = self._current_sources()
        departed = tuple(
            source for source in self._keyboard_configs
            if not any(source is item for item in current)
        )
        if departed:
            self._retire_keyboard_sources(departed)
        elif self._keyboard_owner_source is not None and not self._eligible(self._keyboard_owner_source):
            # A different protocol can finish while the recorded owner is
            # closed but still present. Preserve deterministic promotion.
            self._reconcile_owner()
        # No departure or stale owner: the earlier client-exited signal has
        # already settled/promoted. Do not reinstall bootstrap or reset state.

    def _retire_keyboard_sources(self, departed: tuple) -> None:
        owner_departed = any(self._keyboard_owner_source is source for source in departed)
        state_source = self._keyboard_state_source
        state_source_departed = any(state_source is source for source in departed)
        modifier_source_departed = any(source in self._modifier_holders for source in departed)
        if owner_departed:
            self._settle_input()
        else:
            for source in departed:
                self._settle_source_input(source)
        for source in departed:
            self._clear_settled_modifiers(source)
            config = self._keyboard_configs.get(source)
            if config:
                config.owner = None
            self._keyboard_configs.pop(source, None)
        if self._keyboard_owner_source is not None and not owner_departed and self._eligible(
                self._keyboard_owner_source):
            if modifier_source_departed and not state_source_departed and self._reapply_source_state(state_source):
                return
            if state_source_departed or modifier_source_departed:
                self._activate_owner_state(self._keyboard_owner_source)
            return
        if self._keyboard_owner_source is not None and not owner_departed:
            # Another protocol can finish while the recorded owner is already
            # closed (but has not yet been removed from the source registry).
            # An identical-map promotion below would not pass through the
            # native replacement path, so settle the stale owner's input here.
            self._settle_input()
        self._drop_owner()
        self._promote_owner()

    def setting_changed_handler(self, _server, setting: str, _value, source=None) -> None:
        if setting != "readonly":
            return
        # Both transitions matter; read effective policy from its actual owner,
        # including pointer/focus-only modifier sources with no key_events.
        owner = self._keyboard_owner_source
        restore_owner = False
        surviving_state_source = self._keyboard_state_source
        if self.server.readonly:
            # Mask-only depressed modifiers may have arrived through focus or
            # pointer packets and have no holder to identify them.  Global
            # readonly must retire those snapshots as well as physical holds.
            self._settle_input(release_depressed=True)
        elif source is not None and not self._eligible(source):
            if source is owner:
                config = self._keyboard_configs.get(source)
                if config:
                    config.current_modifiers = tuple(
                        modifier for modifier in self._normalize_modifiers(
                            config, config.current_modifiers,
                        ) if modifier in LOCKED_MODIFIERS
                    )
                    config.remember_applied_runtime()
            else:
                restore_owner = self._keyboard_state_source is source or source in self._modifier_holders
                self._settle_source_input(source)
                surviving_state_source = self._keyboard_state_source
        else:
            candidates = self._keyboard_sources() if source is None else (source,)
            for candidate in candidates:
                if self._eligible(candidate):
                    config = self._keyboard_configs.get(candidate)
                    if config:
                        # A release can be lost while the connection is
                        # readonly.  Once all input has been settled, no old
                        # identity is allowed to suppress a future press.
                        config.settled_translation.clear()
                        self._clear_settled_modifiers(candidate)
        self._reconcile_owner()
        if restore_owner and self._keyboard_owner_source is owner and self._eligible(owner):
            if self._reapply_source_state(surviving_state_source):
                return
            self._activate_owner_state(owner)

    def cleanup(self) -> None:
        try:
            self._settle_input()
        finally:
            try:
                # The first settle can fail before its normal timer cleanup.
                # Retire every source while its opaque GLib id is still known.
                self.cancel_key_repeat_timer()
            except Exception:
                log("failed to cancel Wayland keyboard timers during cleanup", exc_info=True)
            self._drop_owner()
            self._keyboard_configs.clear()
            self._key_holders.clear()
            self._modifier_holders.clear()
            self._settled_modifier_meanings.clear()
            self._keyboard_state_source = None
            try:
                super().cleanup()
            finally:
                # The compositor (and its seat) is destroyed by the earlier
                # WaylandManager subsystem after this cleanup returns.  Drop
                # the manager reference first and guarantee that the native
                # keyboard detaches while its seat pointer is still valid.
                device = self.device
                self.device = None
                try:
                    if device:
                        cleanup = getattr(device, "cleanup", None)
                        if cleanup:
                            cleanup()
                finally:
                    self.bootstrap_config = None
                    self.effective_rmlvo = None
