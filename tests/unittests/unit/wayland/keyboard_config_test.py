#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 Antoine Martin <antoine@xpra.org>
# Copyright (C) 2026 kogeler
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import unittest

from xpra.util.objects import typedict
from xpra.wayland.server.keyboard_config import KeyboardConfig
from xpra.wayland.server.subsystem.keyboard import WaylandKeyboardManager
from unit.wayland.keyboard_test import FakeDevice as BoundaryDevice, FakeServer, FakeSource as BoundarySource


class FakeDevice(BoundaryDevice):
    """Record the exact candidate installed, not the last non-owner compile."""

    def __init__(self, refused=()):
        super().__init__()
        self.installed: list[tuple[str, str, str, str]] = []
        self.refused = tuple(refused)

    def compile_keymap(self, rules, model, layout, variant, options):
        if layout in self.refused:
            raise ValueError("simulated unavailable XKB layout")
        candidate = super().compile_keymap(rules, model, layout, variant, options)
        candidate.install_tuple = layout, model, variant, options
        return candidate

    def install_keymap(self, candidate, settle_keycodes=(), modifiers=(), group=0):
        groups = super().install_keymap(candidate, settle_keycodes, modifiers, group)
        self.installed.append(candidate.install_tuple)
        return groups


class Opts:
    """Only the keyboard_* options are ever read from here."""

    def __init__(self, **kwargs):
        self.keyboard_sync = True
        self.keyboard_layout = ""
        self.keyboard_layouts = []
        self.keyboard_variant = ""
        self.keyboard_variants = []
        self.keyboard_options = ""
        for k, v in kwargs.items():
            setattr(self, f"keyboard_{k}", v)


class FakeSource(BoundarySource):
    """Retain the real KeyboardConnection state and public source API."""

    def __init__(self, uuid: str, *, readonly=False):
        super().__init__(uuid, 0, None, readonly=readonly)


class KeyboardConfigTest(unittest.TestCase):

    def test_parse_hello(self):
        # The hello packet nests everything in a keymap dictionary.
        kc = KeyboardConfig()
        mods = kc.parse(typedict({
            "keymap": {
                "layout": "de",
                "variant": "nodeadkeys",
                "options": "compose:ralt",
                "query_struct": {"model": "pc104"},
            },
        }))
        self.assertEqual((kc.rmlvo.layout, kc.rmlvo.model, kc.rmlvo.variant, kc.rmlvo.options),
                         ("de", "pc104", "nodeadkeys", "compose:ralt"))
        # RMLVO is one atomic change, not four independently mutated strings.
        self.assertGreater(mods, 0)

    def test_parse_config_packet(self):
        # keyboard-config sends the same attributes at the top level.
        kc = KeyboardConfig()
        kc.parse(typedict({"layout": "fr", "query_struct": {"model": "pc105"}}))
        self.assertEqual((kc.rmlvo.layout, kc.rmlvo.model, kc.rmlvo.variant, kc.rmlvo.options),
                         ("fr", "pc105", "", ""))
        self.assertEqual(kc.parse(typedict({"layout": "fr", "query_struct": {"model": "pc105"}})), 0)

    def test_set_layout(self):
        # The legacy layout-changed packet does not carry the model.
        kc = KeyboardConfig()
        kc.parse(typedict({"layout": "fr", "query_struct": {"model": "pc104"}}))
        self.assertTrue(kc.set_layout("gb", "", ""))
        self.assertEqual((kc.rmlvo.layout, kc.rmlvo.model, kc.rmlvo.variant), ("gb", "pc104", ""))
        self.assertFalse(kc.set_layout("gb", "", ""))

    def test_layout_groups_keep_exact_order(self):
        kc = KeyboardConfig()
        kc.parse(typedict({
            "rmlvo-version": 1, "layout": "fr", "variant": "oss",
            "layouts": ["us", "fr", "de"], "variants": ["", "oss", "nodeadkeys"],
            "layout_groups": True,
        }))
        self.assertEqual(tuple(zip(kc.rmlvo.layouts, kc.rmlvo.variants)),
                         (("us", ""), ("fr", "oss"), ("de", "nodeadkeys")))

    def test_layout_groups_without_variants(self):
        kc = KeyboardConfig()
        kc.parse(typedict({"rmlvo-version": 1, "layouts": ["us", "fr"]}))
        self.assertEqual(tuple(zip(kc.rmlvo.layouts, kc.rmlvo.variants)), (("us", ""), ("fr", "")))

    def test_legacy_selection_list_does_not_reorder_current_layout(self):
        kc = KeyboardConfig()
        kc.parse(typedict({
            "layout": "fr", "variant": "oss",
            "layouts": ["us", "fr", "de"], "variants": ["", "oss", "nodeadkeys"],
        }))
        self.assertEqual(tuple(zip(kc.rmlvo.layouts, kc.rmlvo.variants)), (("fr", "oss"),))

    def test_no_layout_has_known_unapplied_bootstrap(self):
        kc = KeyboardConfig()
        self.assertEqual(tuple(zip(kc.rmlvo.layouts, kc.rmlvo.variants)), (("us", ""),))
        self.assertFalse(kc.applied_hash)
        self.assertFalse(kc.validated_hash)
        self.assertEqual(kc.compiled_groups, 0)

    def test_hash(self):
        kc = KeyboardConfig()
        # A normalized identity exists before it has been installed.
        bootstrap_hash = kc.get_hash()
        self.assertTrue(bootstrap_hash)
        self.assertFalse(kc.applied_hash)
        kc.parse(typedict({"layout": "fr"}))
        self.assertNotEqual(bootstrap_hash, kc.get_hash())
        self.assertFalse(kc.applied_hash)


class WaylandKeymapInstallTest(unittest.TestCase):

    def make_manager(self, refused=(), **opts) -> tuple[WaylandKeyboardManager, FakeDevice]:
        device = FakeDevice(refused)
        server = FakeServer()
        server.compositor.keyboard_device = device
        self.sources = server.sources
        manager = WaylandKeyboardManager(server=server)
        manager.init_state()
        manager.init(Opts(**opts))
        manager.setup()
        return manager, device

    def connect(self, manager, uuid: str, keymap: dict, *, readonly=False, keyboard=True) -> FakeSource:
        ss = FakeSource(uuid, readonly=readonly)
        caps = typedict({"keyboard": keyboard, "keymap": keymap})
        before = tuple(manager.server.compositor.keyboard_device.installed)
        manager.parse_hello_ui_keyboard(ss, caps)
        self.assertEqual(tuple(manager.server.compositor.keyboard_device.installed), before,
                         "hello parsing must not mutate the shared seat before acceptance")
        self.sources.append(ss)
        manager.add_new_client(ss, caps)
        return ss

    def test_server_layout_option(self):
        manager, device = self.make_manager(layout="fr")
        self.assertEqual(device.installed, [("fr", "pc105", "", "")])
        self.assertEqual(manager.config_hash, manager.config.get_hash())
        self.assertEqual(manager.config.applied_hash, manager.config_hash)

    def test_no_layout_installs_bootstrap_once(self):
        # The manager binds explicit known-good metadata to the native map.
        # An identical default client must not trigger another installation.
        manager, device = self.make_manager(layout="")
        self.assertEqual(device.installed, [("us", "pc105", "", "")])
        ss = self.connect(manager, "client-1", {})
        self.assertEqual(device.installed, [("us", "pc105", "", "")])
        self.assertIs(manager._keyboard_owner_source, ss)
        self.assertEqual(ss.keyboard_config.applied_hash, manager.config_hash)

    def test_client_layout(self):
        manager, device = self.make_manager(layout="")
        ss = self.connect(manager, "client-1", {
            "layout": "de", "variant": "nodeadkeys", "query_struct": {"model": "pc105"},
        })
        self.assertEqual(device.installed, [("us", "pc105", "", ""), ("de", "pc105", "nodeadkeys", "")])
        before = tuple(device.installed)
        ss.keyboard_config.parse(typedict({"layout": "de", "variant": "nodeadkeys",
                                           "query_struct": {"model": "pc105"}}))
        manager.set_keymap(ss, True)
        self.assertEqual(tuple(device.installed), before, "an unchanged keymap must not be re-installed")
        ss.keyboard_config.parse(typedict({"layout": "fr", "query_struct": {"model": "pc105"}}))
        manager.set_keymap(ss, True)
        self.assertEqual(device.installed[-1], ("fr", "pc105", "", ""))

    def test_client_layout_groups(self):
        manager, device = self.make_manager(layout="")
        self.connect(manager, "client-1", {
            "rmlvo-version": 1, "layout": "fr", "layouts": ["us", "fr"], "variants": ["", "oss"],
            "layout_groups": True,
        })
        self.assertEqual(device.installed[-1], ("us,fr", "pc105", ",oss", ""))

    def test_other_clients_do_not_replace_the_owner_map(self):
        # Replace upstream's union assertion: it renumbers the owner's groups.
        manager, device = self.make_manager(layout="")
        owner = self.connect(manager, "client-1", {"layout": "de"})
        before = tuple(device.installed)
        other = self.connect(manager, "client-2", {"layout": "es"})
        self.assertEqual(tuple(device.installed), before)
        self.assertEqual(device.layouts, ("de",))
        self.assertIs(manager._keyboard_owner_source, owner)
        self.assertTrue(other.keyboard_config.validated_hash)
        self.assertFalse(other.keyboard_config.applied_hash)

    def test_owner_variants_stay_positional_after_foreign_validation(self):
        manager, device = self.make_manager(layout="")
        self.connect(manager, "client-1", {
            "rmlvo-version": 1, "layouts": ["us", "de"], "variants": ["", "nodeadkeys"],
        })
        before = tuple(device.installed)
        self.connect(manager, "client-2", {"layout": "fr"})
        self.assertEqual(tuple(device.installed), before)
        layouts, _, variants, _ = device.installed[-1]
        self.assertEqual((layouts, variants), ("us,de", ",nodeadkeys"))
        self.assertEqual(len(layouts.split(",")), len(variants.split(",")))

    def test_too_many_groups_are_rejected_without_truncation(self):
        manager, device = self.make_manager(layout="")
        owner = self.connect(manager, "owner", {"layout": "de"})
        before = tuple(device.installed)
        rejected = self.connect(manager, "too-many", {
            "rmlvo-version": 1, "layouts": ["de", "es", "it", "pt", "se"],
        })
        self.assertFalse(rejected.keyboard_config.valid)
        self.assertTrue(rejected.keyboard_config.rejected)
        self.assertEqual(tuple(device.installed), before)
        self.assertIs(manager._keyboard_owner_source, owner)

    def test_duplicate_groups_keep_their_positions(self):
        manager, device = self.make_manager(layout="")
        self.connect(manager, "client-1", {
            "rmlvo-version": 1, "layouts": ["de", "de", "us"],
            "variants": ["nodeadkeys", "", ""], "layout_groups": True,
        })
        self.assertEqual(device.installed[-1], ("de,de,us", "pc105", "nodeadkeys,,", ""))

    def test_identical_other_client_does_not_reinstall(self):
        manager, device = self.make_manager(layout="")
        self.connect(manager, "client-1", {"layout": "de"})
        before = tuple(device.installed)
        self.connect(manager, "client-2", {"layout": "de"})
        self.assertEqual(tuple(device.installed), before)

    def test_readonly_client_cannot_change_the_keymap(self):
        manager, device = self.make_manager(layout="fr")
        ss = self.connect(manager, "readonly-client", {"layout": "de"}, readonly=True)
        manager.set_keymap(ss)
        self.assertEqual([x[0] for x in device.installed], ["fr"])

    def test_disabled_keyboard(self):
        manager, device = self.make_manager(layout="")
        ss = self.connect(manager, "client-1", {"layout": "de"}, keyboard=False)
        self.assertFalse(ss.keyboard_config.enabled)
        manager.set_keymap(ss)
        self.assertEqual(device.installed, [("us", "pc105", "", "")])

    def test_unusable_foreign_layout_preserves_the_installed_owner(self):
        manager, device = self.make_manager(refused=("fr",))
        owner = self.connect(manager, "client-1", {"layout": "de"})
        before = tuple(device.installed)
        rejected = self.connect(manager, "client-2", {"layout": "fr"})
        self.assertEqual(tuple(device.installed), before)
        self.assertIs(manager._keyboard_owner_source, owner)
        self.assertEqual(manager.config_hash, owner.keyboard_config.applied_hash)
        self.assertFalse(rejected.keyboard_config.validated_hash)
        self.assertTrue(rejected.keyboard_config.rejected)

    def test_no_device(self):
        manager, device = self.make_manager(layout="")
        before = tuple(device.installed)
        installed_hash = manager.config_hash
        manager.device = None
        rejected = self.connect(manager, "client-1", {"layout": "de"})
        self.assertEqual(tuple(device.installed), before)
        self.assertEqual(manager.config_hash, installed_hash)
        self.assertFalse(rejected.keyboard_config.applied_hash)
        self.assertTrue(rejected.keyboard_config.rejected)


def main():
    unittest.main()


if __name__ == "__main__":
    main()
