#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2018 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import unittest
from unittest.mock import Mock, patch

from xpra.common import noop
from xpra.net.common import BACKWARDS_COMPATIBLE
from xpra.util.objects import AdHocStruct, typedict
from xpra.client.gui.keyboard_helper import KeyboardHelper
from unit.process_test_util import DisplayContext


class KeyboardHelperTest(unittest.TestCase):

    def test_modifier(self):
        kh = KeyboardHelper(noop)

        def checkmask(mask, *modifiers):
            #print("checkmask(%s, %s)", mask, modifiers)
            mods = kh.mask_to_names(mask)
            assert set(mods) == set(modifiers), "expected %s got %s" % (modifiers, mods)

        from gi.repository import Gdk  # @UnresolvedImport
        checkmask(Gdk.ModifierType.SHIFT_MASK, "shift")
        checkmask(Gdk.ModifierType.LOCK_MASK, "lock")
        if getattr(kh.keyboard, "swap_keys", False):
            checkmask(Gdk.ModifierType.SHIFT_MASK | Gdk.ModifierType.META_MASK, "shift", "control")
            #turn swap off and run again:
            kh.keyboard.swap_keys = False
        checkmask(Gdk.ModifierType.SHIFT_MASK | Gdk.ModifierType.CONTROL_MASK, "shift", "control")
        kh.cleanup()

    def test_keymap_properties(self):
        kh = KeyboardHelper(noop)
        kh.query_xkbmap()
        p = kh.get_keymap_properties()
        assert p, "no keymap properties returned"
        self.assertEqual(p.get("rmlvo-version"), 1)
        self.assertTrue(p.get("layouts"))
        self.assertIn("layout_groups", p)
        kh.cleanup()

    def test_exact_rmlvo_properties(self):
        kh = KeyboardHelper(noop)
        try:
            kh.query_struct = {
                "rules": "evdev",
                "model": "pc104",
                "layout": "us,fr,ru",
                "variant": ",oss,",
                "options": "query:authoritative",
            }
            kh.model = "pc105"
            kh.layout = "us,fr,ru"
            kh.layouts = ["us", "fr", "ru"]
            kh.variant = "nodeadkeys"
            kh.variants = ["nodeadkeys"]
            kh.options = "detected:ignored"
            kh.layout_groups = True
            self.assertEqual(kh.get_rmlvo_properties(), {
                "rmlvo-version": 1,
                "rules": "evdev",
                "model": "pc104",
                "layouts": ("us", "fr", "ru"),
                "variants": ("", "oss", ""),
                "options": "query:authoritative",
                "layout_groups": True,
            })
        finally:
            kh.cleanup()

    def test_negotiated_exact_rmlvo_uses_one_current_packet(self):
        packets = []
        kh = KeyboardHelper(lambda *parts: packets.append(parts))
        try:
            kh.query_struct = {
                "rules": "evdev",
                "model": "pc104",
                "layout": "us,fr,ru",
                "variant": ",oss,",
                "options": "grp:alt_shift_toggle",
            }
            kh.layout = "us,fr,ru"
            kh.layout_groups = True

            from xpra.client.subsystem.keyboard import KeyboardClient
            client = KeyboardClient()
            client.helper = kh
            kh.server_rmlvo_pending = True
            kh.send_config()
            kh.send_config()
            self.assertEqual(packets, [])
            self.assertTrue(kh.config_pending)

            self.assertTrue(client.parse_server_capabilities(typedict({
                "keyboard.rmlvo-version": 1,
            })))
            self.assertEqual(kh.server_rmlvo_version, 1)
            self.assertTrue(kh.server_rmlvo_pending)

            client._send_pending_keyboard_config()

            self.assertEqual(len(packets), 1)
            self.assertEqual(packets[0][0], "keyboard-config")
            props = packets[0][1]
            self.assertIs(props["force"], True)
            self.assertEqual(props["rmlvo-version"], 1)
            self.assertEqual(props["rules"], "evdev")
            self.assertEqual(props["model"], "pc104")
            self.assertEqual(props["layouts"], ("us", "fr", "ru"))
            self.assertEqual(props["variants"], ("", "oss", ""))
            self.assertEqual(props["options"], "grp:alt_shift_toggle")

            for unsupported in (0, 2, True, "1", None):
                with self.subTest(unsupported=unsupported):
                    client.parse_server_capabilities(typedict({
                        "keyboard.rmlvo-version": unsupported,
                    }))
                    self.assertEqual(kh.server_rmlvo_version, 0)
        finally:
            kh.cleanup()

    def test_client_coalesces_config_through_handshake_with_or_without_delay(self):
        from xpra.client.subsystem.keyboard import KeyboardClient

        class FakeApp:
            readonly = False
            idle_add = staticmethod(noop)
            timeout_add = staticmethod(noop)
            source_remove = staticmethod(noop)

            def __init__(self):
                self.callbacks = []

            def after_handshake(self, callback, *args):
                self.callbacks.append((callback, args))

        class FakeHelper:
            keyboard = None

            def __init__(self, *_args, **_kwargs):
                self.server_rmlvo_version = 0
                self.server_rmlvo_pending = False
                self.config_pending = False
                self.send_count = 0

            def send_config(self):
                if self.server_rmlvo_pending:
                    self.config_pending = True
                    return
                self.config_pending = False
                self.send_count += 1

        opts = AdHocStruct()
        for name in (
            "keyboard_backend", "keyboard_model", "keyboard_layout",
            "keyboard_layouts", "keyboard_variant", "keyboard_variants",
            "keyboard_options", "shortcut_modifiers", "key_shortcut",
        ):
            setattr(opts, name, "")
        opts.keyboard_sync = True
        opts.keyboard_raw = False
        opts.swap_keys = False

        for delay, early_changes, expected_sends in (
            (True, 0, 1),
            (True, 2, 1),
            (False, 0, 0),
            (False, 2, 1),
        ):
            with self.subTest(delay=delay, early_changes=early_changes):
                app = FakeApp()
                client = KeyboardClient(app)
                client.helper_class = FakeHelper
                with patch("xpra.client.subsystem.keyboard.DELAY_KEYBOARD_DATA", delay):
                    client.init_ui(opts)
                helper = client.helper
                self.assertTrue(helper.server_rmlvo_pending)
                self.assertEqual(helper.config_pending, delay)
                self.assertEqual(len(app.callbacks), 1)
                for _ in range(early_changes):
                    helper.send_config()
                self.assertEqual(helper.send_count, 0)
                self.assertEqual(helper.config_pending, delay or bool(early_changes))

                client.parse_server_capabilities(typedict({
                    "keyboard.rmlvo-version": 1,
                }))
                self.assertTrue(helper.server_rmlvo_pending)
                callback, args = app.callbacks.pop()
                callback(*args)
                self.assertFalse(helper.server_rmlvo_pending)
                self.assertFalse(helper.config_pending)
                self.assertEqual(helper.send_count, expected_sends)

    @unittest.skipUnless(BACKWARDS_COMPATIBLE, "requires legacy keyboard packets")
    def test_unnegotiated_server_keeps_legacy_packet_contract(self):
        packets = []
        kh = KeyboardHelper(lambda *parts: packets.append(parts))
        try:
            kh.query_struct = {
                "rules": "evdev",
                "model": "pc105",
                "layout": "us,fr",
                "variant": ",oss",
                "options": "",
            }
            kh.layout = "us,fr"
            kh.variant = ",oss"
            kh.layout_groups = True

            kh.send_config()

            self.assertEqual([packet[0] for packet in packets], [
                "layout-changed", "keymap-changed",
            ])
            self.assertEqual(packets[0], (
                "layout-changed", "us,fr", ",oss", "", "", "",
            ))
            self.assertEqual(packets[1][1]["keymap"]["rmlvo-version"], 1)

            packets.clear()
            kh.backend = "ibus"
            kh.backend_name = "engine"
            kh.send_config()
            self.assertEqual(packets, [(
                "layout-changed", "us,fr", ",oss", "", "ibus", "engine",
            )])
        finally:
            kh.cleanup()

    def test_x11_aggregate_layout_is_not_duplicated(self):
        kh = KeyboardHelper(noop)
        try:
            spec = (
                "pc105",
                "us,fr,ru",
                ("us", "fr", "ru"),
                "",
                (),
                "",
            )
            with patch.object(kh.keyboard, "get_layout_spec", return_value=spec):
                model, layout, layouts, _variant, _variants, _options = kh.get_layout_spec()
            self.assertEqual(model, "pc105")
            self.assertEqual(layout, "us,fr,ru")
            self.assertEqual(layouts, ["us", "fr", "ru"])
        finally:
            kh.cleanup()

    def test_legacy_platform_layout_choices_are_not_groups(self):
        for backend in ("win32", "darwin"):
            with self.subTest(backend=backend):
                kh = KeyboardHelper(noop, backend=backend)
                try:
                    kh.layout = "de"
                    kh.layouts = ["us", "de", "fr"]
                    kh.layouts_option = ["fr", "us", "de"]
                    kh.variants_option = ["oss", "", "nodeadkeys"]
                    props = kh.get_rmlvo_properties()
                    self.assertEqual(props["layouts"], ("de",))
                    self.assertNotEqual(props["layouts"], tuple(kh.layouts))
                    self.assertNotIn("variants", props)
                    full = kh.get_keymap_properties()
                    self.assertEqual(full["layouts"], ("de",))
                    self.assertNotIn("variants", full)
                finally:
                    kh.cleanup()

    def test_legacy_query_selection_lists_do_not_override_current_layout(self):
        kh = KeyboardHelper(noop)
        try:
            kh.query_struct = {
                "layout": "de",
                "layouts": "fr,us,de",
                "variant": "",
                "variants": "oss,,nodeadkeys",
            }
            props = kh.get_rmlvo_properties()
            self.assertEqual(props["layouts"], ("de",))
            self.assertEqual(props["variants"], ("",))
        finally:
            kh.cleanup()

    def test_plural_only_query_falls_back_to_current_singular_values(self):
        kh = KeyboardHelper(noop)
        try:
            kh.query_struct = {
                "layouts": "fr,us,de",
                "variants": "oss,,nodeadkeys",
            }
            kh.layout = "de"
            kh.variant = "nodeadkeys"
            props = kh.get_rmlvo_properties()
            self.assertEqual(props["layouts"], ("de",))
            self.assertEqual(props["variants"], ("nodeadkeys",))
        finally:
            kh.cleanup()

    def test_fresh_base_helper_has_no_exact_layout(self):
        kh = KeyboardHelper(noop)
        try:
            self.assertEqual(kh.get_rmlvo_properties(), {})
            self.assertNotIn("rmlvo-version", kh.get_keymap_properties())
        finally:
            kh.cleanup()

    def test_rmlvo_options_presence(self):
        kh = KeyboardHelper(noop)
        try:
            kh.query_struct = {"layout": "us"}
            kh.options = ""
            self.assertNotIn("options", kh.get_rmlvo_properties())
            self.assertNotIn("variants", kh.get_rmlvo_properties())

            # An XKB rules-name property represents empty optional fields by
            # omitting them.  They are known empty, rather than unknown, and
            # must override non-empty defaults on the server.
            kh.query_struct = {
                "rules": "evdev",
                "model": "pc105",
                "layout": "us,fr",
            }
            props = kh.get_rmlvo_properties()
            self.assertEqual(props["variants"], ("", ""))
            self.assertIn("options", props)
            self.assertEqual(props["options"], "")

            for value in ("", "none", "NoNe"):
                with self.subTest(value=value):
                    kh.query_struct = {"layout": "us", "options": value}
                    props = kh.get_rmlvo_properties()
                    self.assertIn("options", props)
                    self.assertEqual(props["options"], "")

            kh.query_struct = {"layout": "us"}
            kh.options = "none"
            self.assertEqual(kh.get_rmlvo_properties()["options"], "")
        finally:
            kh.cleanup()

    def test_runtime_detected_options_do_not_become_an_override(self):
        packets = []
        kh = KeyboardHelper(lambda *parts: packets.append(parts))
        try:
            kh.server_rmlvo_version = 1
            layout_specs = (
                ("pc105", "us", ("us",), "", (), "grp:alt_shift_toggle"),
                ("pc105", "us", ("us",), "", (), "compose:ralt"),
                ("pc105", "us", ("us",), "", (), ""),
            )
            keymap_specs = (
                {
                    "rules": "evdev", "model": "pc105", "layout": "us",
                    "options": "grp:alt_shift_toggle",
                },
                {
                    "rules": "evdev", "model": "pc105", "layout": "us",
                    "options": "compose:ralt",
                },
                {"rules": "evdev", "model": "pc105", "layout": "us"},
            )
            with (
                patch.object(kh.keyboard, "get_layout_spec", side_effect=layout_specs),
                patch.object(kh.keyboard, "get_keymap_spec", side_effect=keymap_specs),
                patch.object(kh.keyboard, "get_x11_keymap", return_value={}),
                patch.object(
                    kh.keyboard, "get_keymap_modifiers", return_value=({}, [], []),
                ),
            ):
                hashes = []
                for expected in ("grp:alt_shift_toggle", "compose:ralt", ""):
                    kh.query_xkbmap()
                    self.assertEqual(kh.options, expected)
                    self.assertEqual(kh.get_rmlvo_properties()["options"], expected)
                    hashes.append(kh.hash)
                    kh.send_config()
                    self.assertEqual(packets[-1][0], "keyboard-config")
                    self.assertEqual(packets[-1][1]["options"], expected)
            self.assertEqual(len(set(hashes)), 3)
            self.assertEqual(len(packets), 3)
            self.assertEqual(kh.options_option, "")
        finally:
            kh.cleanup()

    @unittest.skipUnless(BACKWARDS_COMPATIBLE, "requires legacy keyboard packets")
    def test_explicit_empty_options_remain_empty_in_legacy_updates(self):
        packets = []
        kh = KeyboardHelper(lambda *parts: packets.append(parts), options="none")
        try:
            with (
                patch.object(kh.keyboard, "get_layout_spec", return_value=(
                    "pc105", "us", ("us",), "", (), "grp:alt_shift_toggle",
                )),
                patch.object(kh.keyboard, "get_keymap_spec", return_value={
                    "rules": "evdev", "model": "pc105", "layout": "us",
                    "options": "grp:alt_shift_toggle",
                }),
                patch.object(kh.keyboard, "get_x11_keymap", return_value={}),
                patch.object(
                    kh.keyboard, "get_keymap_modifiers", return_value=({}, [], []),
                ),
            ):
                kh.query_xkbmap()
            self.assertEqual(kh.options_option, "none")
            self.assertEqual(kh.options, "")
            self.assertEqual(kh.query_struct["options"], "")
            self.assertEqual(kh.get_rmlvo_properties()["options"], "")

            kh.send_config()
            self.assertEqual(packets[0][0], "layout-changed")
            self.assertEqual(packets[0][3], "")
            self.assertEqual(packets[1][1]["keymap"]["options"], "")

            packets.clear()
            kh.backend = "ibus"
            kh.backend_name = "engine"
            kh.send_config()
            self.assertEqual(packets, [("layout-changed", "us", "", "", "ibus", "engine")])
        finally:
            kh.cleanup()

    def test_exact_query_detection_and_command_line_precedence(self):
        for names, expected_rules, expected_model in (
                ({"rules": "evdev", "layout": "us"}, "evdev", ""),
                ({"model": "pc105", "layout": "us"}, "", "pc105"),
        ):
            with self.subTest(names=names):
                kh = KeyboardHelper(noop)
                try:
                    kh.query_struct = names
                    props = kh.get_rmlvo_properties()
                    self.assertEqual(props["rules"], expected_rules)
                    self.assertEqual(props["model"], expected_model)
                    self.assertEqual(props["variants"], ("",))
                    self.assertEqual(props["options"], "")
                finally:
                    kh.cleanup()

        kh = KeyboardHelper(
            noop, model="pc104", layout="de", variant="nodeadkeys", options="none",
        )
        try:
            detected = {
                "rules": "evdev",
                "model": "pc105",
                "layout": "us,fr",
                "variant": ",oss",
                "options": "grp:alt_shift_toggle",
            }
            with patch.object(kh.keyboard, "get_keymap_spec", return_value=detected):
                query = kh.get_keymap_spec()
            self.assertEqual(query, {
                "rules": "evdev",
                "model": "pc104",
                "layout": "de",
                "variant": "nodeadkeys",
                "options": "",
            })
            kh.query_struct = query
            kh.model = "pc104"
            kh.layout = "de"
            kh.variant = "nodeadkeys"
            props = kh.get_rmlvo_properties()
            self.assertEqual(props["model"], "pc104")
            self.assertEqual(props["layouts"], ("de",))
            self.assertEqual(props["variants"], ("nodeadkeys",))
            self.assertEqual(props["options"], "")
        finally:
            kh.cleanup()

    def test_gtk_runtime_update_invalidates_modifier_cache_before_query(self):
        from xpra.client.gtk3.keyboard_helper import GTKKeyboardHelper

        events = []

        class Backend:
            cached = True
            modifier_map = {}

            def invalidate_keymap_modifiers(self):
                self.cached = False
                events.append("invalidate")

            def update_modifier_map(self, meanings):
                events.append(("modifier-map", dict(meanings)))

        helper = GTKKeyboardHelper.__new__(GTKKeyboardHelper)
        helper.hash = "old"
        helper.keyboard = Backend()
        helper.mod_meanings = {"Alt_R": "mod1"}

        def query(instance):
            self.assertFalse(instance.keyboard.cached)
            events.append("query")
            instance.mod_meanings = {"ISO_Level3_Shift": "mod5"}
            instance.hash = "new"

        with (
                patch("xpra.client.gtk3.keyboard_helper.is_X11", return_value=True),
                patch.object(KeyboardHelper, "update", query),
        ):
            self.assertTrue(helper.update())
        self.assertEqual(events, [
            "invalidate",
            "query",
            ("modifier-map", {"ISO_Level3_Shift": "mod5"}),
        ])

    def test_gtk_cleanup_retires_pending_keymap_change(self):
        from xpra.client.gtk3.keyboard_helper import GTKKeyboardHelper

        keymap = Mock()
        keymap.connect.return_value = 23
        backend = Mock()
        backend.get_keyboard_repeat.return_value = None
        callbacks = []

        def schedule(delay, callback):
            self.assertEqual(delay, 500)
            callbacks.append(callback)
            return 17

        with (
                patch.object(KeyboardHelper, "make_keyboard", return_value=backend),
                patch.object(GTKKeyboardHelper, "update", return_value=True) as update,
                patch("xpra.client.gtk3.keyboard_helper.get_default_keymap", return_value=keymap),
                patch("xpra.client.gtk3.keyboard_helper.GLib.timeout_add", side_effect=schedule),
                patch("xpra.client.gtk3.keyboard_helper.GLib.source_remove") as remove,
        ):
            helper = GTKKeyboardHelper(noop)
            helper.keymap_changed()
            helper.keymap_changed()
            self.assertEqual(len(callbacks), 1)
            helper.cleanup()
            update.reset_mock()
            keymap.connect.reset_mock()

            # A callback already dispatched by GLib must also be harmless.
            callbacks[0]()
            keymap.connect.assert_not_called()
            update.assert_not_called()
            remove.assert_called_once_with(17)
            helper.cleanup()
            remove.assert_called_once_with(17)

    def test_parse_shortcuts(self):
        shortcuts = [
            'Control+Menu:toggle_keyboard_grab',
            'Shift+Menu:toggle_pointer_grab',
            'Shift+F11:toggle_fullscreen',
            '#+F1:show_menu',
            'Control+F1:show_window_menu',
            '#+F2:show_start_new_command',
            '#+F3:show_bug_report',
            '#+F4:quit',
            '#+F5:increase_quality',
            '#+F6:decrease_quality',
            '#+F7:increase_speed',
            '#+F8:decrease_speed',
            '#+F10:magic_key',
            '#+F11:show_session_info',
            '#+F12:toggle_debug',
            '#+plus:scaleup',
            '#+minus:scaledown',
            '#+underscore:scaledown',
            '#+KP_Add:scaleup',
            '#+KP_Subtract:scaledown',
            '#+KP_Multiply:scalereset',
            '#+bar:scalereset',
            '#+question:scalingoff',
        ]
        kh = KeyboardHelper(noop, key_shortcuts=shortcuts)
        parsed = kh.parse_shortcuts()
        assert kh.shortcut_modifiers, "no shortcut modifiers: %s" % (kh.shortcut_modifiers,)
        assert len(parsed) > 10, "not enough shortcuts parsed: %s" % (parsed,)
        window = AdHocStruct()
        window.quit = noop
        modifier_names = kh.get_modifier_names()
        modifiers_used = [modifier_names.get(x, x) for x in kh.shortcut_modifiers]
        assert kh.key_handled_as_shortcut(window, "F4", modifiers_used, True)
        assert not kh.key_handled_as_shortcut(window, "F1", [], True)
        kh.cleanup()


def main():
    with DisplayContext():
        unittest.main()


if __name__ == '__main__':
    main()
