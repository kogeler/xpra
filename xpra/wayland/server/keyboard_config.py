# This file is part of Xpra.
# Copyright (C) 2025 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import hashlib
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from xpra.server.keyboard_config_base import KeyboardConfigBase
from xpra.util.objects import typedict


RMLVO_VERSION = 1
MAX_XKB_GROUPS = 4
MAX_NAME_SIZE = 128
MAX_OPTIONS_SIZE = 1024
# The current full legacy client payload has 19 top-level keymap entries and
# its XKB names query has five.  Keep compatibility headroom without allowing
# either nested mapping to grow with the enclosing hello packet.
MAX_KEYMAP_ENTRIES = 64
MAX_QUERY_ENTRIES = 16
MAX_REJECT_FINGERPRINT_BYTES = 4096
MAX_REJECT_FINGERPRINT_DEPTH = 4
MAX_REJECT_FINGERPRINT_NODES = 64
MAX_REJECT_CONTAINER_ITEMS = 16
MAX_REJECT_SCALAR_BYTES = 256
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
_OPTION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_:-]*")
_RMLVO_FIELDS = (
    "rmlvo-version", "rules", "model", "layout", "layouts", "variant", "variants",
    "options", "layout_groups", "query_struct",
)
_DIRECT_FIELDS = tuple(
    field for name in (*_RMLVO_FIELDS, "sync", "mod_meanings")
    for field in (name, f"xkbmap_{name}")
)
_QUERY_FIELDS = ("rules", "model", "layout", "layouts", "variant", "variants", "options")


class RMLVOError(ValueError):
    pass


def _dict_compatible(value: Any) -> bool:
    """Recognize a real dict subclass without consulting value.__class__."""
    # Membership would use equality and can therefore invoke a hostile
    # metaclass ``__eq__`` from the MRO.  Identity is both sufficient and
    # hook-free: a real dict subclass has the exact builtin dict in its MRO.
    return any(base is dict for base in type.__getattribute__(type(value), "__mro__"))


def _rejected_fingerprint(value: Any) -> str:
    """Hash a bounded structural prefix without invoking untrusted hooks."""
    digest = hashlib.sha256()
    remaining = MAX_REJECT_FINGERPRINT_BYTES
    nodes = 0
    active: set[int] = set()

    def emit(data: bytes) -> None:
        nonlocal remaining
        if remaining <= 0:
            return
        chunk = data[:remaining]
        digest.update(chunk)
        remaining -= len(chunk)

    def emit_size(value: int) -> None:
        emit(str(value).encode("ascii"))
        emit(b":")

    def enter_container(item: Any, tag: bytes, length: int, depth: int) -> bool:
        emit(tag)
        emit_size(length)
        if depth >= MAX_REJECT_FINGERPRINT_DEPTH:
            emit(b"!depth;")
            return False
        identity = id(item)
        if identity in active:
            emit(b"!cycle;")
            return False
        active.add(identity)
        emit(b"[")
        return True

    def leave_container(item: Any, length: int, visited: int) -> None:
        active.remove(id(item))
        if visited < length:
            emit(b"!more:")
            emit_size(length - visited)
        emit(b"]")

    def visit(item: Any, depth: int = 0) -> None:
        nonlocal nodes
        if nodes >= MAX_REJECT_FINGERPRINT_NODES:
            emit(b"!nodes;")
            return
        nodes += 1
        kind = type(item)
        if item is None:
            emit(b"n;")
            return
        if kind is bool:
            emit(b"b1;" if item else b"b0;")
            return
        if kind is int:
            bits = int.bit_length(item)
            negative = int.__lt__(item, 0) is True
            emit(b"i-" if negative else b"i+")
            emit_size(bits)
            if bits <= MAX_REJECT_SCALAR_BYTES * 8:
                magnitude = int.__abs__(item)
                width = max(1, (bits + 7) // 8)
                emit(int.to_bytes(magnitude, width, "big"))
            emit(b";")
            return
        if kind is float:
            emit(b"f:")
            emit(float.hex(item).encode("ascii"))
            emit(b";")
            return
        if kind is str:
            length = str.__len__(item)
            prefix = str.__getitem__(item, slice(0, MAX_REJECT_SCALAR_BYTES))
            emit(b"s")
            emit_size(length)
            emit(str.encode(prefix, "utf-8", "replace")[:MAX_REJECT_SCALAR_BYTES])
            emit(b";")
            return
        if kind is bytes:
            length = bytes.__len__(item)
            emit(b"y")
            emit_size(length)
            emit(bytes.__getitem__(item, slice(0, MAX_REJECT_SCALAR_BYTES)))
            emit(b";")
            return
        if kind is bytearray:
            length = bytearray.__len__(item)
            prefix = bytearray.__getitem__(item, slice(0, MAX_REJECT_SCALAR_BYTES))
            emit(b"a")
            emit_size(length)
            emit(bytes(prefix))
            emit(b";")
            return
        if kind is dict or kind is typedict:
            length = dict.__len__(item)
            if not enter_container(item, b"d", length, depth):
                return
            visited = 0
            iterator = iter(dict.items(item))
            while visited < min(length, MAX_REJECT_CONTAINER_ITEMS):
                if nodes >= MAX_REJECT_FINGERPRINT_NODES:
                    break
                key, child = next(iterator)
                visit(key, depth + 1)
                visit(child, depth + 1)
                visited += 1
            leave_container(item, length, visited)
            return
        if kind is list:
            length = list.__len__(item)
            if not enter_container(item, b"l", length, depth):
                return
            visited = 0
            while visited < min(length, MAX_REJECT_CONTAINER_ITEMS):
                if nodes >= MAX_REJECT_FINGERPRINT_NODES:
                    break
                visit(list.__getitem__(item, visited), depth + 1)
                visited += 1
            leave_container(item, length, visited)
            return
        if kind is tuple:
            length = tuple.__len__(item)
            if not enter_container(item, b"t", length, depth):
                return
            visited = 0
            while visited < min(length, MAX_REJECT_CONTAINER_ITEMS):
                if nodes >= MAX_REJECT_FINGERPRINT_NODES:
                    break
                visit(tuple.__getitem__(item, visited), depth + 1)
                visited += 1
            leave_container(item, length, visited)
            return
        if kind is set:
            length = set.__len__(item)
            # Set iteration order is hash-seed dependent.  Its bounded length
            # is stable and sufficient for a diagnostic of an invalid value.
            emit(b"e")
            emit_size(length)
            emit(b";")
            return
        if kind is frozenset:
            length = frozenset.__len__(item)
            emit(b"r")
            emit_size(length)
            emit(b";")
            return
        # Wire values should be builtins.  Keep an unsupported object opaque:
        # even its type metadata may be supplied through dynamic Python hooks.
        emit(b"?;")

    emit(b"xpra-rmlvo-rejection-v1;")
    visit(value)
    return digest.hexdigest()


@dataclass(frozen=True)
class RMLVOConfig:
    rules: str
    model: str
    layouts: tuple[str, ...]
    variants: tuple[str, ...]
    options: str
    layout_groups: bool
    present: frozenset[str] = field(default_factory=frozenset, repr=False)

    @property
    def layout(self) -> str:
        return ",".join(self.layouts)

    @property
    def variant(self) -> str:
        return ",".join(self.variants)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rules": self.rules,
            "model": self.model,
            "layouts": self.layouts,
            "variants": self.variants,
            "options": self.options,
            "layout-groups": self.layout_groups,
        }

    def get_hash(self) -> str:
        payload = {
            "layout_groups": self.layout_groups,
            "layouts": self.layouts,
            "model": self.model,
            "options": self.options,
            "rules": self.rules,
            "variants": self.variants,
        }
        data = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(data.encode("ascii")).hexdigest()


BOOTSTRAP_RMLVO = RMLVOConfig(
    rules="evdev",
    model="pc105",
    layouts=("us",),
    variants=("",),
    options="",
    layout_groups=False,
)


def _remap_group(old: RMLVOConfig, old_group: int, new: RMLVOConfig) -> int:
    """Preserve a layout/variant identity, including its duplicate occurrence."""
    if (not old.layout_groups or not new.layout_groups
            or not old.layouts or not new.layouts
            or len(old.variants) != len(old.layouts)
            or len(new.variants) != len(new.layouts)
            or type(old_group) is not int
            or old_group < 0 or old_group >= len(old.layouts)):
        return 0
    identity = old.layouts[old_group], old.variants[old_group]
    occurrence = sum(
        1 for index in range(old_group)
        if (old.layouts[index], old.variants[index]) == identity
    )
    matches = tuple(
        index for index, value in enumerate(zip(new.layouts, new.variants))
        if value == identity
    )
    if not matches:
        return 0
    return matches[min(occurrence, len(matches) - 1)]


def _mapping(
        value: Any, name: str, fields: Sequence[str], max_entries: int = 0,
) -> dict[str, Any]:
    if value is None:
        raise RMLVOError(f"{name} must be a dictionary")
    if not _dict_compatible(value):
        raise RMLVOError(f"{name} must be a dictionary")
    if max_entries and dict.__len__(value) > max_entries:
        raise RMLVOError(f"{name} contains too many entries")
    # Hello and legacy keymap dictionaries also contain unrelated capabilities
    # and potentially large raw keycode tables.  Probe only the fixed structured
    # fields that this parser owns: dict(value), keys() or items() would traverse
    # and duplicate attacker-controlled input before any RMLVO bound can run.
    return {
        field: dict.__getitem__(value, field)
        for field in fields if dict.__contains__(value, field)
    }


def _direct_properties(props: typedict | dict[str, Any]) -> dict[str, Any]:
    if props is None:
        return {}
    if not _dict_compatible(props):
        raise RMLVOError("keyboard properties must be a dictionary")
    if not dict.__contains__(props, "keymap"):
        return _mapping(props, "keyboard properties", _DIRECT_FIELDS)
    return _mapping(
        dict.__getitem__(props, "keymap"), "keymap", _DIRECT_FIELDS, MAX_KEYMAP_ENTRIES,
    )


def _legacy_get(values: dict[str, Any], name: str) -> tuple[bool, Any]:
    if name in values:
        return True, values[name]
    legacy = f"xkbmap_{name}"
    if legacy in values:
        return True, values[legacy]
    return False, None


def _bounded_name(value: Any, field_name: str, *, empty: bool = False) -> str:
    if type(value) is not str:
        raise RMLVOError(f"{field_name} must be a string")
    if not value:
        if empty:
            return ""
        raise RMLVOError(f"{field_name} must not be empty")
    # Every Unicode code point occupies at least one UTF-8 byte.  Reject a
    # clearly oversized wire value before encoding it and allocating a second
    # copy proportional to attacker-controlled input.
    if len(value) > MAX_NAME_SIZE:
        raise RMLVOError(f"invalid {field_name}")
    if len(value.encode("utf-8")) > MAX_NAME_SIZE or not _NAME_RE.fullmatch(value):
        raise RMLVOError(f"invalid {field_name}")
    return value


def _group_values(value: Any, field_name: str, *, empty: bool) -> tuple[str, ...]:
    if type(value) is str:
        # Four bounded names plus their separators are the largest valid
        # aggregate.  Check this before count/split traverse and duplicate an
        # otherwise unbounded network string.
        if len(value) > MAX_XKB_GROUPS * MAX_NAME_SIZE + MAX_XKB_GROUPS - 1:
            raise RMLVOError(f"{field_name} are too long")
        if value.count(",") >= MAX_XKB_GROUPS:
            raise RMLVOError(f"{field_name} contain too many groups")
        entries = value.split(",")
    elif type(value) is tuple or type(value) is list:
        if len(value) > MAX_XKB_GROUPS:
            raise RMLVOError(f"{field_name} contain too many groups")
        entries = value
    else:
        raise RMLVOError(f"{field_name} must be a string or sequence")
    result = []
    for entry in entries:
        # Validate the exact type and bound length before any scan. The
        # bounded name grammar also rejects commas inside a sequence entry.
        result.append(_bounded_name(entry, field_name, empty=empty))
    return tuple(result)


def _options(value: Any) -> str:
    if type(value) is not str:
        raise RMLVOError("options must be a string")
    if len(value) > MAX_OPTIONS_SIZE:
        raise RMLVOError("options are too long")
    if len(value.encode("utf-8")) > MAX_OPTIONS_SIZE:
        raise RMLVOError("options are too long")
    if not value or value.lower() == "none":
        return ""
    entries = value.split(",")
    if any(not entry or not _OPTION_RE.fullmatch(entry) for entry in entries):
        raise RMLVOError("invalid options")
    return value


def _pick(
        direct: dict[str, Any], query: dict[str, Any], direct_names: Sequence[str],
        query_names: Sequence[str], default: Any,
) -> tuple[Any, str]:
    for name in direct_names:
        found, value = _legacy_get(direct, name)
        if found:
            return value, "direct"
    for name in query_names:
        if name in query:
            return query[name], "query"
    return default, "default"


def normalize_rmlvo(
        props: typedict | dict[str, Any], defaults: RMLVOConfig = BOOTSTRAP_RMLVO,
) -> RMLVOConfig:
    direct = _direct_properties(props)
    version_present, version = _legacy_get(direct, "rmlvo-version")
    if version_present and (type(version) is not int or version != RMLVO_VERSION):
        raise RMLVOError("unsupported RMLVO representation version")
    query_present, query_value = _legacy_get(direct, "query_struct")
    query = _mapping(
        query_value, "query_struct", _QUERY_FIELDS, MAX_QUERY_ENTRIES,
    ) if query_present else {}
    present: set[str] = set()

    rules_value, rules_source = _pick(direct, query, ("rules",), ("rules",), defaults.rules)
    model_value, model_source = _pick(direct, query, ("model",), ("model",), defaults.model)
    if version_present:
        # Only the versioned representation gives the old plural fields exact,
        # positional RMLVO semantics.  In old packets those fields mean the
        # layouts / variants available for selection on the client.
        layouts_value, layouts_source = _pick(
            direct, query, ("layouts", "layout"), ("layout",), defaults.layouts,
        )
        variants_value, variants_source = _pick(
            direct, query, ("variants", "variant"), ("variant",), defaults.variants,
        )
    else:
        # A clean pre-version client can prepend its aggregate singular layout
        # (for example "us,fr,ru") to the legacy `layouts` selection list.
        # Prefer the singular current value and the XKB query over that list.
        layouts_value, layouts_source = _pick(
            direct, query, ("layout",), ("layout", "layouts"), None,
        )
        if layouts_source == "default":
            layouts_value, layouts_source = _pick(direct, {}, ("layouts",), (), defaults.layouts)
        variants_value, variants_source = _pick(
            direct, query, ("variant",), ("variant", "variants"), None,
        )
        if variants_source == "default":
            variants_value, variants_source = _pick(direct, {}, ("variants",), (), defaults.variants)
    options_value, options_source = _pick(direct, query, ("options",), ("options",), defaults.options)
    groups_value, groups_source = _pick(
        direct, {}, ("layout_groups",), (), defaults.layout_groups,
    )

    for name, source in (
            ("rules", rules_source),
            ("model", model_source),
            ("layouts", layouts_source),
            ("variants", variants_source),
            ("options", options_source),
            ("layout_groups", groups_source),
    ):
        if source != "default":
            present.add(name)

    rules = _bounded_name(rules_value, "rules", empty=True)
    model = _bounded_name(model_value, "model", empty=True)
    layouts = _group_values(layouts_value, "layouts", empty=False)
    if not layouts or len(layouts) > MAX_XKB_GROUPS:
        raise RMLVOError(f"layouts must contain between 1 and {MAX_XKB_GROUPS} groups")
    variants = _group_values(variants_value, "variants", empty=True)
    if len(variants) > len(layouts):
        raise RMLVOError("variants must not outnumber layouts")
    variants += ("",) * (len(layouts) - len(variants))
    options = _options(options_value)
    if type(groups_value) is not bool:
        raise RMLVOError("layout_groups must be a boolean")
    return RMLVOConfig(
        rules=rules,
        model=model,
        layouts=layouts,
        variants=variants,
        options=options,
        layout_groups=groups_value,
        present=frozenset(present),
    )


KeycodeResolver = Callable[["KeyboardConfig", str, list[str], int, str, int], tuple[int, int]]


class KeyboardConfig(KeyboardConfigBase):

    def __init__(self, props=None, defaults: RMLVOConfig = BOOTSTRAP_RMLVO,
                 resolver: KeycodeResolver | None = None):
        super().__init__()
        self.defaults = defaults
        self.rmlvo = defaults
        self.last_good_rmlvo = defaults
        self.resolver = resolver
        self.client_id = ""
        self.compiled_groups = 0
        self.validated_hash = ""
        self.applied_hash = ""
        self.rejected = ""
        self.rejected_hash = ""
        self.valid = props is None
        self.modifier_meanings: dict[str, str] = {}
        self.pressed_modifier_overrides: dict[Any, tuple[tuple[str, ...], tuple[str, ...]]] = {}
        self.settled_translation: set[Any] = set()
        self.representation = "legacy"
        self.repeat_delay = 500
        self.repeat_interval = 30
        self.current_modifiers: tuple[str, ...] = ()
        self.current_group = 0
        self.delay = False
        self._applied_state = None
        self._validated_state = None
        if props is not None:
            self.parse(props)

    def __repr__(self):
        return f"KeyboardConfig({self.rmlvo.layout} / {self.rmlvo.variant} / {self.rmlvo.options})"

    def get_info(self) -> dict[str, Any]:
        info = super().get_info()
        info.update(self.rmlvo.as_dict())
        info["rmlvo-version"] = RMLVO_VERSION
        info["rmlvo-representation"] = self.representation
        info["rmlvo-present"] = tuple(sorted(self.rmlvo.present))
        info["compiled-groups"] = self.compiled_groups
        if self.rejected:
            info["rejected"] = {
                "hash": self.rejected_hash,
                "reason": self.rejected,
            }
        return info

    def _mark_rejected(self, reason: str, props: Any = None) -> None:
        self.rejected = reason[:160]
        self.rejected_hash = _rejected_fingerprint(props)

    def mark_rejected(self, reason: str, props: Any = None) -> None:
        self._mark_rejected(reason, props)
        self._restore_last_applied()

    def _snapshot_state(self) -> tuple:
        return (
            self.rmlvo, self.sync, dict(self.modifier_meanings), self.representation,
            self.current_modifiers, self.current_group, self.valid, self.compiled_groups,
        )

    def _restore_state(self, state: tuple) -> None:
        (self.rmlvo, self.sync, self.modifier_meanings, self.representation,
         self.current_modifiers, self.current_group, self.valid, self.compiled_groups) = state

    def remember_applied_runtime(self) -> None:
        """Keep the runtime part of the last compiled client state current."""
        if self._validated_state and self.validated_hash and self.get_hash() == self.validated_hash:
            self._validated_state = self._snapshot_state()
        if self._applied_state and self.applied_hash and self.get_hash() == self.applied_hash:
            self._applied_state = self._snapshot_state()

    def _restore_last_applied(self) -> None:
        if self._validated_state:
            self._restore_state(self._validated_state)
        elif self._applied_state:
            self._restore_state(self._applied_state)
        elif not self.valid:
            self.rmlvo = self.last_good_rmlvo
            self.compiled_groups = 0
            self.valid = False

    def mark_compile_rejected(self, reason: str, *, prefer_applied: bool = False) -> None:
        attempted = self.rmlvo
        validated_runtime = None
        if (prefer_applied and self._validated_state
                and self.validated_hash == attempted.get_hash()):
            validated_runtime = self._validated_state
        self._mark_rejected(reason, attempted.as_dict())
        if prefer_applied and self._applied_state:
            # A source can validate a newer map while it is not the seat owner.
            # If the later native installation fails, that validated snapshot
            # was never active.  Roll every validation marker back to the last
            # actually installed state so promotion can retry one coherent
            # known-good configuration.
            self._restore_state(self._applied_state)
            if validated_runtime:
                (runtime_rmlvo, runtime_sync, _runtime_meanings, _runtime_representation,
                 runtime_modifiers, runtime_group, _runtime_valid, _runtime_groups) = validated_runtime
                # The newer map may have been used for real non-owner input
                # after validation.  Keep that live source state while mapping
                # its group identity back into the installed RMLVO; keymap
                # metadata such as modifier meanings still comes from the
                # applied snapshot.
                self.sync = runtime_sync
                self.current_modifiers = runtime_modifiers
                self.current_group = _remap_group(runtime_rmlvo, runtime_group, self.rmlvo)
            self.last_good_rmlvo = self.rmlvo
            self.validated_hash = self.applied_hash
            self._applied_state = self._snapshot_state()
            self._validated_state = self._applied_state
        elif self._validated_state:
            self._restore_state(self._validated_state)
        elif self._applied_state:
            self._restore_state(self._applied_state)
        else:
            self.rmlvo = self.defaults
            self.compiled_groups = 0
            self.validated_hash = ""
            self.applied_hash = ""
            self.valid = False

    def mark_validated(self, groups: int) -> None:
        self.valid = True
        self.last_good_rmlvo = self.rmlvo
        self.compiled_groups = groups
        self.validated_hash = self.get_hash()
        self.rejected = ""
        self.rejected_hash = ""
        self._validated_state = self._snapshot_state()

    def mark_applied(self, groups: int) -> None:
        self.mark_validated(groups)
        self.applied_hash = self.get_hash()
        self._applied_state = self._snapshot_state()

    def _parse(self, props, defaults: RMLVOConfig, *, preserve_modifier_meanings: bool = False) -> int:
        old = self.rmlvo
        old_group = self.current_group
        oldsync = self.sync
        try:
            direct = _direct_properties(props)
            sync_present, sync_value = _legacy_get(direct, "sync")
            if sync_present and type(sync_value) is not bool:
                raise RMLVOError("sync must be a boolean")
            candidate = normalize_rmlvo(props, defaults)
            meanings_present = "mod_meanings" in direct or "xkbmap_mod_meanings" in direct
            meanings_value = direct.get("mod_meanings", direct.get("xkbmap_mod_meanings"))
            candidate_meanings = None
            if meanings_present:
                if (not _dict_compatible(meanings_value)
                        or dict.__len__(meanings_value) > 256):
                    raise RMLVOError("mod_meanings must be a bounded dictionary")
                candidate_meanings = {}
                for key, value in dict.items(meanings_value):
                    if (type(key) is not str or type(value) is not str
                            or len(key) > MAX_NAME_SIZE or len(value) > MAX_NAME_SIZE
                            or len(key.encode("utf-8")) > MAX_NAME_SIZE
                            or len(value.encode("utf-8")) > MAX_NAME_SIZE
                            or any(ord(char) < 0x20 or ord(char) == 0x7f for char in key + value)):
                        raise RMLVOError("invalid mod_meanings entry")
                    candidate_meanings[key] = value
        except (RMLVOError, TypeError, ValueError) as exc:
            self.mark_rejected(str(exc), props)
            return 0
        candidate_group = _remap_group(old, old_group, candidate)
        self.rmlvo = candidate
        self.current_group = candidate_group
        version_present, _version = _legacy_get(direct, "rmlvo-version")
        self.representation = "versioned" if version_present else "legacy"
        if sync_present:
            self.sync = sync_value
        if candidate_meanings is not None:
            self.modifier_meanings = candidate_meanings
        elif not preserve_modifier_meanings:
            self.modifier_meanings = {}
        self.valid = True
        self.rejected = ""
        self.rejected_hash = ""
        return int(candidate != old) + int(self.sync != oldsync)

    def parse(self, props: typedict) -> int:
        return self._parse(props, self.defaults)

    def parse_options(self, props: typedict) -> int:
        return self.parse(props)

    def get_hash(self) -> str:
        return self.rmlvo.get_hash()

    def set_layout(self, layout: str, variant: str, options: str | None) -> bool:
        values: dict[str, Any] = {
            "layout": layout,
            "variant": variant,
            "layout_groups": self.rmlvo.layout_groups,
        }
        if options is not None:
            values["options"] = options
        return bool(self._parse(typedict(values), self.rmlvo, preserve_modifier_meanings=True))

    def get_keycode(self, client_keycode: int, keyname: str, pressed: bool,
                    modifiers: list[str], keyval: int, keystr: str, group: int) -> tuple[int, int]:
        if not keyname and client_keycode < 0:
            return -1, group
        identity = self.key_identity(client_keycode, keyname, keyval)
        if identity in self.settled_translation:
            if not pressed:
                self.settled_translation.discard(identity)
            return -1, 0
        if identity in self.pressed_translation:
            translation = self.pressed_translation[identity]
            if pressed:
                # The pinned physical key still needs the level inferred on
                # press.  Repeats from lightweight clients can omit Shift or
                # Level3 just like the original event.  Preserve only the
                # inference delta, allowing real packet modifiers to change.
                added, removed = self.pressed_modifier_overrides.get(identity, ((), ()))
                modifiers[:] = [modifier for modifier in modifiers if modifier not in removed]
                modifiers.extend(modifier for modifier in added if modifier not in modifiers)
            else:
                self.discard_pressed_translation(identity)
            return translation
        original_modifiers = tuple(modifiers)
        keycode, server_group = self.do_get_keycode(
            client_keycode, keyname, pressed, modifiers, keyval, keystr, group,
        )
        if keycode < 0 and keyname and not keyname.islower():
            keycode, server_group = self.do_get_keycode(
                client_keycode, keyname.lower(), pressed, modifiers, keyval, keystr, group,
            )
        if pressed and keycode > 0:
            self.pressed_translation[identity] = keycode, server_group
            added = tuple(modifier for modifier in modifiers if modifier not in original_modifiers)
            removed = tuple(modifier for modifier in original_modifiers if modifier not in modifiers)
            if added or removed:
                self.pressed_modifier_overrides[identity] = added, removed
        return keycode, server_group

    @staticmethod
    def key_identity(client_keycode: int, keyname: str, keyval: int) -> Any:
        if client_keycode > 0:
            return client_keycode
        # Terminal, RFB, Pyglet and some old clients use zero for every key.
        # Their stable key name is the only wire identity available.
        return client_keycode, keyname or keyval

    def discard_key_release(self, client_keycode: int, keyname: str, keyval: int) -> None:
        """Consume a release suppressed by readonly/recording policy."""
        identity = self.key_identity(client_keycode, keyname, keyval)
        self.discard_pressed_translation(identity)
        self.settled_translation.discard(identity)

    def discard_pressed_translation(self, identity) -> None:
        self.pressed_translation.pop(identity, None)
        self.pressed_modifier_overrides.pop(identity, None)

    def settle_pressed_translations(self) -> None:
        """Ignore repeats and the eventual releases for keys settled by a map change."""
        self.settled_translation.update(self.pressed_translation)
        self.pressed_translation.clear()
        self.pressed_modifier_overrides.clear()

    def do_get_keycode(self, _client_keycode: int, keyname: str, _pressed: bool,
                       modifiers: list[str], keyval: int, keystr: str, group: int) -> tuple[int, int]:
        if not self.resolver:
            return -1, 0
        return self.resolver(self, keyname, modifiers, keyval, keystr, group)

    def make_keymask_match(self, _modifier_list, _ignored_modifier_keycode=0,
                           _ignored_modifier_keynames=None) -> None:
        # Modifier masks are applied transactionally by the Wayland manager.
        return None
