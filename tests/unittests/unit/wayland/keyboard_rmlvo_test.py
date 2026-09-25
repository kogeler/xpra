#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 kogeler
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import unittest
from dataclasses import replace

from xpra.util.objects import typedict
from xpra.wayland.server.keyboard_config import (
    BOOTSTRAP_RMLVO,
    MAX_NAME_SIZE,
    MAX_OPTIONS_SIZE,
    MAX_XKB_GROUPS,
    RMLVOConfig,
    RMLVOError,
    KeyboardConfig,
    normalize_rmlvo,
)


class RMLVOParsingTest(unittest.TestCase):

    class UnscannableDict(dict):
        """Expose exact dict lookups but fail any proportional mapping copy."""

        @staticmethod
        def fail(*_args, **_kwargs):
            raise AssertionError("untrusted mapping was scanned or copied")

        __iter__ = fail
        __len__ = fail
        __repr__ = fail
        copy = fail
        items = fail
        keys = fail
        values = fail

    class OversizedString(str):
        """Fail if validation performs expensive string work after len()."""

        def __len__(self):
            return 10**9

        def encode(self, *_args, **_kwargs):
            raise AssertionError("oversized input was encoded")

        def count(self, *_args, **_kwargs):
            raise AssertionError("oversized input was scanned")

        def split(self, *_args, **_kwargs):
            raise AssertionError("oversized input was split")

    def exact_values(self) -> dict:
        return {
            "rmlvo-version": 1,
            "rules": "evdev",
            "model": "pc105",
            "layouts": ("us", "fr", "ru"),
            "variants": ("", "oss", ""),
            "options": "grp:alt_shift_toggle,compose:ralt",
            "layout_groups": True,
        }

    def unscannable(self, **values) -> dict:
        data = {f"unrelated-{index}": index for index in range(4096)}
        data.update(values)
        return self.UnscannableDict(data)

    def test_nested_and_flat_are_equivalent(self):
        values = self.exact_values()
        nested = normalize_rmlvo(typedict({"keymap": values}))
        flat = normalize_rmlvo(typedict(values))
        self.assertEqual(nested, flat)
        self.assertEqual(nested.layouts, ("us", "fr", "ru"))
        self.assertEqual(nested.variants, ("", "oss", ""))

    def test_large_outer_mapping_is_projected_without_copy(self):
        values = self.unscannable(**self.exact_values())
        normalized = normalize_rmlvo(values)
        self.assertEqual(normalized.layouts, ("us", "fr", "ru"))
        config = KeyboardConfig(values)
        self.assertTrue(config.valid)
        self.assertEqual(config.rmlvo, normalized)

    def test_large_nested_mapping_is_rejected_before_copy(self):
        values = self.unscannable(**self.exact_values())
        with self.assertRaisesRegex(RMLVOError, "keymap contains too many entries"):
            normalize_rmlvo({"keymap": values})
        config = KeyboardConfig({"keymap": values})
        self.assertFalse(config.valid)
        self.assertEqual(config.rejected, "keymap contains too many entries")

    def test_large_query_mapping_is_rejected_before_copy(self):
        query = self.unscannable(
            rules="evdev", model="pc104", layout="us,fr", variant=",oss", options="",
        )
        with self.assertRaisesRegex(RMLVOError, "query_struct contains too many entries"):
            normalize_rmlvo({"query_struct": query, "layout_groups": True})
        config = KeyboardConfig({"query_struct": query, "layout_groups": True})
        self.assertFalse(config.valid)
        self.assertEqual(config.rejected, "query_struct contains too many entries")

    def test_rejected_fingerprint_is_bounded_for_a_huge_typedict_subclass(self):
        class ReprBombTypedict(typedict):
            def __repr__(self):
                raise AssertionError("untrusted typedict was represented")

        keymap = self.unscannable(**self.exact_values())
        payload = ReprBombTypedict({"keymap": keymap})
        payload["cycle"] = payload
        payload.update({f"outer-{index}": index for index in range(4096)})
        config = KeyboardConfig(payload)
        self.assertFalse(config.valid)
        self.assertRegex(config.rejected_hash, r"^[0-9a-f]{64}$")
        first_hash = config.rejected_hash
        self.assertEqual(config.parse(payload), 0)
        self.assertEqual(config.rejected_hash, first_hash)

    def test_rejected_fingerprint_bounds_exact_container_cycles_depth_and_width(self):
        cycle = {}
        cycle["self"] = cycle
        deep = []
        cursor = deep
        for _index in range(32):
            child = []
            cursor.append(child)
            cursor = child
        payload = {
            "cycle": cycle,
            "deep": deep,
            "wide": list(range(4096)),
            "layout": "x" * 100_000,
        }
        config = KeyboardConfig(payload)
        self.assertFalse(config.valid)
        self.assertRegex(config.rejected_hash, r"^[0-9a-f]{64}$")
        first_hash = config.rejected_hash
        self.assertEqual(config.parse(payload), 0)
        self.assertEqual(config.rejected_hash, first_hash)

    def test_rejected_fingerprint_keeps_unknown_objects_opaque(self):
        class EqualityBombMeta(type):
            def __eq__(cls, _other):
                raise AssertionError("untrusted metaclass equality hook was invoked")

        class EqualityBomb(metaclass=EqualityBombMeta):
            pass

        class ClassBomb:
            @property
            def __class__(self):
                raise AssertionError("untrusted __class__ hook was invoked")

            def __repr__(self):
                raise AssertionError("untrusted object was represented")

        for payload in (
            ClassBomb(),
            {"rmlvo-version": ClassBomb(), "layout": "us"},
            {"layout": ClassBomb()},
            {"layouts": EqualityBomb()},
        ):
            with self.subTest(payload_type=type(payload)):
                config = KeyboardConfig(payload)
                self.assertFalse(config.valid)
                self.assertTrue(config.rejected)
                self.assertRegex(config.rejected_hash, r"^[0-9a-f]{64}$")

    def test_bounded_mapping_subclasses_preserve_structured_fields(self):
        nested = self.UnscannableDict(self.exact_values())
        query = self.UnscannableDict({
            "rules": "evdev", "model": "pc104", "layout": "us,fr",
            "variant": ",oss", "options": "",
        })
        self.assertEqual(normalize_rmlvo({"keymap": nested}).layouts, ("us", "fr", "ru"))
        normalized = normalize_rmlvo({"query_struct": query, "layout_groups": True})
        self.assertEqual(normalized.rules, "evdev")
        self.assertEqual(normalized.model, "pc104")
        self.assertEqual(normalized.layouts, ("us", "fr"))
        self.assertEqual(normalized.variants, ("", "oss"))

    def test_clean_legacy_nested_aggregate_ignores_selection_list(self):
        values = {
            "layout": "us,fr,ru",
            "layouts": ("us,fr,ru", "us", "fr", "ru"),
            "variant": ",,",
            "variants": (",,", "", "", ""),
            "layout_groups": True,
            "query_struct": {
                "rules": "evdev",
                "model": "pc105",
                "layout": "us,fr,ru",
                "variant": ",,",
                "options": "",
            },
        }
        config = normalize_rmlvo(typedict({"keymap": values}))
        self.assertEqual(config.layouts, ("us", "fr", "ru"))
        self.assertEqual(config.variants, ("", "", ""))
        self.assertEqual(config.rules, "evdev")
        self.assertEqual(config.model, "pc105")

    def test_legacy_current_layout_precedes_platform_choices(self):
        config = normalize_rmlvo({
            "layout": "de",
            "layouts": ("us", "de", "fr"),
            "variant": "nodeadkeys",
            "variants": ("", "nodeadkeys", "oss"),
            "layout_groups": False,
        })
        self.assertEqual(config.layouts, ("de",))
        self.assertEqual(config.variants, ("nodeadkeys",))

    def test_explicit_values_precede_query_and_defaults(self):
        defaults = RMLVOConfig("base", "fallback", ("de",), ("nodeadkeys",), "caps:escape", False)
        config = normalize_rmlvo({
            "rmlvo-version": 1,
            "model": "pc104",
            "layouts": ("ca", "jp"),
            "variants": ("", "kana"),
            "options": "",
            "layout_groups": True,
            "query_struct": {
                "rules": "evdev",
                "model": "query-model",
                "layout": "us,fr",
                "variant": "intl,oss",
                "options": "compose:ralt",
            },
        }, defaults)
        self.assertEqual(config.rules, "evdev")
        self.assertEqual(config.model, "pc104")
        self.assertEqual(config.layouts, ("ca", "jp"))
        self.assertEqual(config.variants, ("", "kana"))
        self.assertEqual(config.options, "")
        self.assertTrue(config.layout_groups)
        self.assertEqual(config.present, {"rules", "model", "layouts", "variants", "options", "layout_groups"})

    def test_explicit_empty_rules_and_model_override_defaults(self):
        defaults = RMLVOConfig("base", "pc105", ("de",), ("nodeadkeys",), "caps:escape", False)
        for field in ("rules", "model"):
            with self.subTest(field=field):
                config = normalize_rmlvo({
                    "rmlvo-version": 1,
                    "layouts": ("us",),
                    "variants": ("",),
                    "options": "",
                    field: "",
                    "layout_groups": True,
                }, defaults)
                self.assertEqual(getattr(config, field), "")
                self.assertIn(field, config.present)

    def test_versioned_query_uses_singular_current_values(self):
        config = normalize_rmlvo({
            "rmlvo-version": 1,
            "query_struct": {
                "layout": "de,ca",
                "layouts": ("us", "fr", "de", "ca"),
                "variant": "nodeadkeys,multix",
                "variants": ("", "oss", "nodeadkeys", "multix"),
            },
        })
        self.assertEqual(config.layouts, ("de", "ca"))
        self.assertEqual(config.variants, ("nodeadkeys", "multix"))

    def test_versioned_direct_exact_plural_values_win(self):
        config = normalize_rmlvo({
            "rmlvo-version": 1,
            "layout": "de",
            "layouts": ("us", "fr", "ru"),
            "variant": "nodeadkeys",
            "variants": ("", "oss", ""),
            "query_struct": {
                "layout": "ca,jp",
                "variant": ",kana",
            },
        })
        self.assertEqual(config.layouts, ("us", "fr", "ru"))
        self.assertEqual(config.variants, ("", "oss", ""))

    def test_missing_and_explicitly_empty_options_differ(self):
        defaults = replace(BOOTSTRAP_RMLVO, options="caps:escape")
        missing = normalize_rmlvo({"layout": "us"}, defaults)
        empty = normalize_rmlvo({"layout": "us", "options": ""}, defaults)
        self.assertEqual(missing.options, "caps:escape")
        self.assertEqual(empty.options, "")
        self.assertNotIn("options", missing.present)
        self.assertIn("options", empty.present)

    def test_none_options_is_explicitly_empty(self):
        defaults = replace(BOOTSTRAP_RMLVO, options="caps:escape")
        config = normalize_rmlvo({"layout": "us", "options": "NoNe"}, defaults)
        self.assertEqual(config.options, "")
        self.assertIn("options", config.present)

    def test_legacy_names_and_positional_padding(self):
        config = normalize_rmlvo({
            "xkbmap_layout": "us,de,ca",
            "xkbmap_variant": ",nodeadkeys",
            "xkbmap_options": "caps:escape",
            "xkbmap_layout_groups": True,
        })
        self.assertEqual(config.layouts, ("us", "de", "ca"))
        self.assertEqual(config.variants, ("", "nodeadkeys", ""))
        self.assertEqual(config.options, "caps:escape")
        self.assertTrue(config.layout_groups)

    def test_one_through_maximum_groups(self):
        names = ("us", "fr", "ru", "de")
        for count in range(1, MAX_XKB_GROUPS + 1):
            with self.subTest(count=count):
                config = normalize_rmlvo({
                    "layouts": names[:count],
                    "variants": ("",) * count,
                    "layout_groups": True,
                })
                self.assertEqual(len(config.layouts), count)
                self.assertEqual(len(config.variants), count)

    def test_hash_covers_every_effective_field(self):
        base = normalize_rmlvo(self.exact_values())
        changes = (
            replace(base, rules="base"),
            replace(base, model="pc104"),
            replace(base, layouts=("fr", "us", "ru")),
            replace(base, variants=("intl", "oss", "")),
            replace(base, options=""),
            replace(base, layout_groups=False),
        )
        for changed in changes:
            with self.subTest(changed=changed):
                self.assertNotEqual(base.get_hash(), changed.get_hash())
        equivalent = normalize_rmlvo({
            "query_struct": {
                "rules": base.rules,
                "model": base.model,
                "layout": base.layout,
                "variant": base.variant,
                "options": base.options,
            },
            "layout_groups": True,
        })
        self.assertEqual(base.get_hash(), equivalent.get_hash())

    def test_hash_tracks_effective_keymap_not_wire_presence(self):
        missing = normalize_rmlvo({"layout": "us"})
        explicit = normalize_rmlvo({"layout": "us", "options": ""})
        self.assertNotEqual(missing.present, explicit.present)
        self.assertEqual(missing.get_hash(), explicit.get_hash())

    def test_unsafe_or_unbounded_values_fail(self):
        invalid = (
            {"layout": "../us"},
            {"layout": "us+inet(evdev)"},
            {"layout": "us\nfr"},
            {"layouts": ("us",) * (MAX_XKB_GROUPS + 1)},
            {"layouts": ("us",), "variants": ("", "oss")},
            {"layout": "us", "options": "grp:alt_shift_toggle,/tmp/map"},
            {"layout": "us", "options": "xkb_symbols:include(foo)"},
            {"rmlvo-version": 2, "layout": "us"},
            {"layout": "us", "layout_groups": 1},
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(RMLVOError):
                normalize_rmlvo(values)

    def test_oversized_strings_are_rejected_before_copy_or_split(self):
        oversized = self.OversizedString("x")
        limits = (
            ("rules", MAX_NAME_SIZE),
            ("layout", MAX_XKB_GROUPS * MAX_NAME_SIZE + MAX_XKB_GROUPS - 1),
            ("options", MAX_OPTIONS_SIZE),
        )
        self.assertTrue(all(limit < len(oversized) for _field, limit in limits))
        for field, _limit in limits:
            with self.subTest(field=field), self.assertRaises(RMLVOError):
                normalize_rmlvo({"layout": "us", field: oversized})

    def test_explicit_null_nested_mappings_fail(self):
        for values in ({"keymap": None}, {"query_struct": None}):
            with self.subTest(values=values), self.assertRaises(RMLVOError):
                normalize_rmlvo(values)

    def test_builtin_sequence_names_are_bounded_individually(self):
        for container in (tuple, list):
            for field in ("layouts", "variants"):
                for text in ("x" * (MAX_NAME_SIZE + 1), "x" * (1024 * 1024), "us,fr"):
                    values = self.exact_values()
                    values[field] = container((text,))
                    with self.subTest(container=container, field=field, length=len(text)), \
                            self.assertRaises(RMLVOError):
                        normalize_rmlvo(values)


class KeyboardConfigStateTest(unittest.TestCase):

    def test_invalid_update_rolls_back_then_valid_update_recovers(self):
        config = KeyboardConfig({"layout": "us,fr", "variant": ",oss", "layout_groups": True})
        original = config.rmlvo
        original_hash = config.get_hash()
        config.current_group = 1
        self.assertEqual(config.parse(typedict({"layout": "../bad"})), 0)
        self.assertTrue(config.valid)
        self.assertEqual(config.rmlvo, original)
        self.assertEqual(config.get_hash(), original_hash)
        self.assertEqual(config.current_group, 1)
        self.assertTrue(config.rejected)
        self.assertEqual(config.parse(typedict({"layout": "fr,us", "variant": "oss,"})), 1)
        self.assertTrue(config.valid)
        self.assertFalse(config.rejected)
        self.assertEqual(config.rmlvo.layouts, ("fr", "us"))
        self.assertEqual(config.current_group, 0)

    def test_successful_parse_remaps_current_group_by_identity_occurrence(self):
        for old_layouts, old_variants, old_group, new_layouts, new_variants, groups, expected in (
                (("us", "fr", "us"), ("", "", ""), 2,
                 ("us", "us", "fr"), ("", "", ""), True, 1),
                (("us", "us", "us"), ("", "", ""), 2,
                 ("us", "us"), ("", ""), True, 1),
                (("us", "fr"), ("", "oss"), 1,
                 ("fr", "us"), ("", ""), True, 0),
                (("us", "fr"), ("", ""), 1,
                 ("fr", "us"), ("", ""), False, 0),
                (("us", "fr"), ("", ""), -1,
                 ("fr", "us"), ("", ""), True, 0)):
            with self.subTest(
                    old_layouts=old_layouts, old_group=old_group,
                    new_layouts=new_layouts, groups=groups):
                config = KeyboardConfig({
                    "rmlvo-version": 1,
                    "layouts": old_layouts,
                    "variants": old_variants,
                    "layout_groups": True,
                })
                config.current_group = old_group
                self.assertTrue(config.parse({
                    "rmlvo-version": 1,
                    "layouts": new_layouts,
                    "variants": new_variants,
                    "layout_groups": groups,
                }))
                self.assertEqual(config.current_group, expected)

    def test_invalid_rmlvo_does_not_partially_change_sync(self):
        config = KeyboardConfig({"layout": "us", "sync": True})
        self.assertEqual(config.parse({"layout": "../bad", "sync": False}), 0)
        self.assertTrue(config.sync)
        self.assertTrue(config.valid)
        self.assertTrue(config.rejected)

    def test_malformed_nested_keymap_is_bounded_rejection(self):
        config = KeyboardConfig({"layout": "us", "sync": True})
        original = config.rmlvo
        self.assertEqual(config.parse(typedict({"keymap": ("not", "a", "mapping")})), 0)
        self.assertTrue(config.valid)
        self.assertEqual(config.rmlvo, original)
        self.assertIn("dictionary", config.rejected)

    def test_initial_invalid_configuration_has_no_usable_client_state(self):
        config = KeyboardConfig({"layout": "../bad"})
        self.assertFalse(config.valid)
        self.assertTrue(config.rejected)

    def test_invalid_modifier_meanings_update_is_all_or_nothing(self):
        original_meanings = {"Shift_L": "shift"}
        invalid_meanings = (
            {"Control_L": "control", "bad\nkey": "mod1"},
            {f"Key{index}": "shift" for index in range(257)},
        )
        for meanings in invalid_meanings:
            with self.subTest(size=len(meanings)):
                config = KeyboardConfig({
                    "layout": "us",
                    "sync": True,
                    "mod_meanings": original_meanings,
                })
                original = config.rmlvo
                self.assertEqual(config.modifier_meanings, original_meanings)
                self.assertEqual(config.parse({
                    "layout": "fr",
                    "sync": False,
                    "mod_meanings": meanings,
                }), 0)
                self.assertEqual(config.rmlvo, original)
                self.assertTrue(config.sync)
                self.assertEqual(config.modifier_meanings, original_meanings)
                self.assertTrue(config.valid)
                self.assertTrue(config.rejected)

    def test_full_parse_clears_absent_modifier_meanings_but_layout_change_preserves(self):
        meanings = {"Shift_L": "shift", "ISO_Level3_Shift": "mod5"}
        config = KeyboardConfig({"layout": "us", "mod_meanings": meanings})
        self.assertEqual(config.modifier_meanings, meanings)
        self.assertTrue(config.set_layout("fr", "oss", None))
        self.assertEqual(config.modifier_meanings, meanings)
        self.assertEqual(config.parse({"layout": "de"}), 1)
        self.assertEqual(config.modifier_meanings, {})

    def test_rejection_diagnostic_and_hash_are_bounded_and_stable(self):
        config = KeyboardConfig({"layout": "us"})
        invalid = {"layout": "x" * 100_000}
        self.assertEqual(config.parse(invalid), 0)
        first_hash = config.rejected_hash
        self.assertLessEqual(len(config.rejected), 160)
        self.assertEqual(len(first_hash), 64)
        self.assertEqual(config.parse(invalid), 0)
        self.assertEqual(config.rejected_hash, first_hash)

    def test_layout_changed_preserves_absent_options(self):
        config = KeyboardConfig({"layout": "us", "options": "caps:escape", "layout_groups": True})
        self.assertTrue(config.set_layout("fr", "oss", None))
        self.assertEqual(config.rmlvo.options, "caps:escape")
        self.assertTrue(config.set_layout("fr", "oss", ""))
        self.assertEqual(config.rmlvo.options, "")

    def test_release_uses_and_consumes_pressed_translation(self):
        calls = []

        def resolve(_config, _name, _modifiers, _keyval, _keystr, group):
            calls.append(group)
            return 24, group

        config = KeyboardConfig({"layout": "us,fr", "layout_groups": True}, resolver=resolve)
        self.assertEqual(config.get_keycode(24, "q", True, [], 113, "q", 1), (24, 1))
        self.assertEqual(config.get_keycode(24, "q", False, [], 113, "q", 0), (24, 1))
        self.assertNotIn(24, config.pressed_translation)
        self.assertEqual(calls, [1])

    def test_repeat_press_does_not_replace_original_translation(self):
        calls = []

        def resolve(_config, _name, _modifiers, _keyval, _keystr, group):
            calls.append(group)
            return 24 + group, group

        config = KeyboardConfig({"layout": "us,fr", "layout_groups": True}, resolver=resolve)
        self.assertEqual(config.get_keycode(24, "a", True, [], 97, "a", 1), (25, 1))
        self.assertEqual(config.get_keycode(24, "q", True, [], 113, "q", 0), (25, 1))
        self.assertEqual(config.get_keycode(24, "q", False, [], 113, "q", 0), (25, 1))
        self.assertEqual(calls, [1])

    def test_zero_keycodes_use_key_identity(self):
        def resolve(_config, name, _modifiers, _keyval, _keystr, group):
            return {"q": 24, "a": 38}[name], group

        config = KeyboardConfig({"layout": "us", "layout_groups": True}, resolver=resolve)
        self.assertEqual(config.get_keycode(0, "q", True, [], 113, "q", 0), (24, 0))
        self.assertEqual(config.get_keycode(0, "a", True, [], 97, "a", 0), (38, 0))
        self.assertEqual(config.get_keycode(0, "q", False, [], 113, "q", 0), (24, 0))
        self.assertEqual(config.get_keycode(0, "a", False, [], 97, "a", 0), (38, 0))

    def test_settled_press_ignores_repeats_until_release(self):
        calls = []

        def resolve(_config, _name, _modifiers, _keyval, _keystr, group):
            calls.append(group)
            return 24, group

        config = KeyboardConfig({"layout": "us,fr", "layout_groups": True}, resolver=resolve)
        self.assertEqual(config.get_keycode(24, "a", True, [], 97, "a", 1), (24, 1))
        config.settle_pressed_translations()
        self.assertEqual(config.get_keycode(24, "a", True, [], 97, "a", 0), (-1, 0))
        self.assertEqual(config.get_keycode(24, "a", False, [], 97, "a", 0), (-1, 0))
        self.assertEqual(config.get_keycode(24, "q", True, [], 113, "q", 0), (24, 0))
        self.assertEqual(calls, [1, 0])


def main():
    unittest.main()


if __name__ == "__main__":
    main()
