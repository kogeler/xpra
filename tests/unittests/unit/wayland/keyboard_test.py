#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 kogeler
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import os
import subprocess
import sys
import unittest
from tempfile import TemporaryDirectory
from typing import ClassVar
from unittest.mock import Mock, patch

from xpra.keyboard.mask import DEFAULT_MODIFIER_MEANINGS
from xpra.net.common import BACKWARDS_COMPATIBLE, Packet
from xpra.server.source.keyboard import KeyboardConnection
from xpra.server.subsystem.client_session import ClientSessionServer
from xpra.util.signal_emitter import SignalEmitter
from xpra.util.objects import typedict
from xpra.wayland.server.keyboard_config import KeyboardConfig
from xpra.wayland.server.subsystem import keyboard as keyboard_module
from xpra.wayland.server.subsystem.keyboard import WaylandKeyboardManager
from xpra.wayland.server.subsystem.pointer import WaylandPointerManager

Q = ord("q")
A = ord("a")
UPPER_Q = ord("Q")
UPPER_A = ord("A")
AT = ord("@")
CYRILLIC_SHORT_I = 0x6CA
SHIFT_L = 0xFFE1
CAPS_LOCK = 0xFFE5
CONTROL_L = 0xFFE3
NUM_LOCK = 0xFF7F
SUPER_L = 0xFFEB
LEVEL5_SHIFT = 0xFE11


class FakeCandidate:
    def __init__(self, layouts):
        self.layouts = tuple(layouts)
        self.group_count = len(self.layouts)
        self.cleaned = False

    def cleanup(self):
        self.cleaned = True


class FakeDevice:
    SYMBOLS: ClassVar[dict[str, dict[int, int]]] = {
        "us": {Q: 24, UPPER_Q: 24, A: 38, UPPER_A: 38},
        "fr": {A: 24, UPPER_A: 24, Q: 38, UPPER_Q: 38},
        "ru": {CYRILLIC_SHORT_I: 24},
        "de": {Q: 24, UPPER_Q: 24, AT: 24},
    }
    KEYNAMES: ClassVar[dict[str, int]] = {
        "Shift_L": 50,
        "Shift_R": 62,
        "Caps_Lock": 66,
        "Control_L": 37,
        "Control_R": 105,
        "Num_Lock": 77,
        "Alt_L": 64,
        "ISO_Level3_Shift": 108,
        "Super_L": 133,
        "ISO_Level5_Shift": 94,
    }

    def __init__(self):
        self.layouts = ()
        self.group = 0
        self.modifiers = ()
        self.keys_down = set()
        self.compile_calls = []
        self.install_calls = []
        self.clear_calls = []
        self.clear_states = []
        self.repeat_calls = []
        self.resolve_calls = []
        self.candidates = []
        self.press_calls = []
        self.fail_install = False
        self.cleaned = False

    def compile_keymap(self, rules, model, layout, variant, options):
        self.compile_calls.append((rules, model, layout, variant, options))
        layouts = tuple(layout.split(","))
        if "missing" in layouts:
            raise ValueError("libxkbcommon rejected the RMLVO configuration")
        candidate = FakeCandidate(layouts)
        self.candidates.append(candidate)
        return candidate

    def set_layout(self, layout="us", model="pc105", variant="", options="") -> bool:
        # Current clean upstream installs through this public bool API.
        candidate = None
        try:
            candidate = self.compile_keymap("evdev", model, layout, variant, options)
            self.install_keymap(candidate)
            return True
        except (ValueError, RuntimeError):
            return False
        finally:
            if candidate is not None:
                candidate.cleanup()

    def install_keymap(self, candidate, settle_keycodes=(), modifiers=(), group=0):
        if self.fail_install:
            raise RuntimeError("simulated wlroots install failure")
        if settle_keycodes:
            self.clear_keys_pressed(settle_keycodes)
        self.layouts = candidate.layouts
        self.install_calls.append(self.layouts)
        self.modifiers = tuple(modifiers)
        self.group = group if 0 <= group < len(self.layouts) else 0
        candidate.cleanup()
        return len(self.layouts)

    def resolve_keycode(self, keyval, keyname, groups, modifiers, keystr="", fixed_modifiers=()):
        self.resolve_calls.append((keyval, keyname, tuple(groups), tuple(modifiers), keystr))
        groups = tuple(groups)
        if keyname in self.KEYNAMES:
            return self.KEYNAMES[keyname], next(
                (group for group in groups if 0 <= group < len(self.layouts)), 0,
            )
        symbols = []
        if len(keystr) == 1:
            symbols.append(ord(keystr))
        if type(keyval) is int and keyval > 0 and keyval not in symbols:
            symbols.append(keyval)
        original = list(dict.fromkeys(modifiers))
        fixed = frozenset(fixed_modifiers)
        trials = []
        for toggle in (
                (), ("mod5",), ("shift",), ("mod5", "shift"),
                ("mod2",), ("mod2", "mod5"), ("mod2", "shift"),
                ("mod2", "mod5", "shift")):
            trial = list(original)
            for modifier in toggle:
                if modifier in fixed:
                    continue
                if modifier in trial:
                    trial.remove(modifier)
                else:
                    trial.append(modifier)
            trial = tuple(trial)
            if trial not in trials:
                trials.append(trial)
        for symbol in symbols:
            for group in groups:
                if group < 0 or group >= len(self.layouts):
                    continue
                layout = self.layouts[group]
                keycode = self.SYMBOLS.get(layout, {}).get(symbol, 0)
                if not keycode:
                    continue
                for trial in trials:
                    if symbol in (UPPER_Q, UPPER_A) and "shift" not in trial and "lock" not in trial:
                        continue
                    if symbol in (Q, A) and "shift" in trial:
                        continue
                    if layout == "de" and symbol == AT and "mod5" not in trial:
                        continue
                    if isinstance(modifiers, list):
                        modifiers[:] = trial
                    return keycode, group
        return -1, 0

    def update_modifiers(self, modifiers, group):
        self.modifiers = tuple(modifiers)
        self.group = group if 0 <= group < len(self.layouts) else 0

    def get_modifiers(self):
        return self.modifiers

    def get_layout_group(self):
        return self.group

    def get_layout_group_count(self):
        return len(self.layouts)

    def set_layout_group(self, group):
        self.group = group if 0 <= group < len(self.layouts) else 0

    def reapply_modifiers(self):
        return None

    def press_key(self, keycode, pressed):
        self.press_calls.append((keycode, pressed))
        if pressed:
            self.keys_down.add(keycode)
        else:
            self.keys_down.discard(keycode)

    def clear_keys_pressed(self, keycodes):
        keycodes = tuple(keycodes)
        self.clear_calls.append(keycodes)
        self.clear_states.append((self.layouts, self.group, keycodes))
        self.keys_down.difference_update(keycodes)

    def get_keycodes_down(self):
        return tuple(sorted(self.keys_down))

    @staticmethod
    def get_keycode_capacity():
        return 32

    def get_keycode_for_keysym(self, keysym):
        # Current clean upstream returns keycode AND group, preferring its
        # first symbol match. Its group-one collision is an output failure.
        for group, layout in enumerate(self.layouts):
            if keycode := self.SYMBOLS.get(layout, {}).get(keysym):
                return keycode, group
        return -1, 0

    def get_keycode_for_keyname(self, name):
        return self.KEYNAMES.get(name, -1), 0

    def set_repeat_rate(self, delay, interval):
        self.repeat_calls.append((delay, interval))

    def cleanup(self):
        self.cleaned = True


class FakeCompositor:
    def __init__(self):
        self.flushes = 0
        self.keyboard_device = FakeDevice()

    def get_keyboard_device(self):
        return self.keyboard_device

    def flush(self):
        self.flushes += 1


class FakeServer(SignalEmitter):
    __signals__ = ("client-exited", "setting-changed")

    def __init__(self):
        super().__init__()
        self.readonly = False
        self.compositor = FakeCompositor()
        self.sources = []
        self.subsystems = {}
        self.protocol_sources = {}
        self.ui_driver_calls = []
        self.ui_driver = ""
        self.timers = {}
        self.removed_timers = []
        self.next_timer = 1

    def idle_add(self, callback, *args):
        return callback(*args)

    def timeout_add(self, delay, callback, *args):
        timer = self.next_timer
        self.next_timer += 1
        self.timers[timer] = (delay, callback, args)
        return timer

    def source_remove(self, source):
        self.removed_timers.append(source)
        self.timers.pop(source, None)

    def get_sources_by_type(self, _source_type=object, exclude=None):
        return tuple(source for source in self.sources if source is not exclude)

    def set_ui_driver(self, _source):
        self.ui_driver_calls.append(_source)

    def get_server_source(self, proto):
        return self.protocol_sources.get(proto)


class FakeSource(KeyboardConnection):
    def __init__(self, uuid, counter, config, *, readonly=False, record=False, record_requested=None):
        self.init_state()
        self.uuid = uuid
        self.counter = counter
        self.keyboard_config = config
        self.keyboard_record = record
        self.keyboard_record_requested = record if record_requested is None else record_requested
        self._readonly = readonly
        self.closed = False
        self.user_events = []
        self.send_async = Mock()

    def effective_readonly(self):
        return self._readonly

    def is_closed(self):
        return self.closed

    def make_keymask_match(self, _modifiers, _keycode=0, ignored_modifier_keynames=None):
        del ignored_modifier_keynames

    def is_modifier(self, keyname, _keycode):
        return keyname in DEFAULT_MODIFIER_MEANINGS

    def user_event(self, event):
        self.user_events.append(event)


def config(manager, layouts, variants=(), *, groups=True, options=""):
    if not variants:
        variants = ("",) * len(layouts)
    return KeyboardConfig({
        "rmlvo-version": 1,
        "rules": "evdev",
        "model": "pc105",
        "layouts": tuple(layouts),
        "variants": tuple(variants),
        "options": options,
        "layout_groups": groups,
    }, resolver=manager.resolve_keycode)


def manager_fixture():
    server = FakeServer()
    manager = WaylandKeyboardManager(server)
    manager.device = FakeDevice()
    bootstrap = config(manager, ("us",), groups=False)
    manager.bootstrap_config = bootstrap
    if not manager._apply_config(bootstrap, ""):
        raise AssertionError("failed to install fake bootstrap keymap")
    return manager, server


def accept(manager, server, source):
    if not any(source is item for item in server.sources):
        server.sources.append(source)
    manager.add_new_client(source, typedict())
    return source


class WaylandKeyboardWireBoundaryTest(unittest.TestCase):
    """Existing public APIs provide behavioral controls on the clean source."""

    def test_client_map_installs_after_acceptance_and_resolves_group_collision(self):
        server = FakeServer()
        server.compositor.keyboard_device.layouts = ("us",)
        manager = WaylandKeyboardManager(server)
        manager.setup()
        source = FakeSource("owner", 1, None)
        server.sources.append(source)
        caps = typedict({
            "keyboard": True,
            "keymap": {"layout": "us,fr", "variant": ",", "layout_groups": True},
        })

        manager.parse_hello_ui_keyboard(source, caps)
        self.assertEqual(manager.device.layouts, ("us",))
        manager.add_new_client(source, caps)
        self.assertEqual(manager.device.layouts, ("us", "fr"))
        self.assertEqual(manager.get_keycode(source, 24, "a", True, [], A, "a", 1), (24, 1))

    def test_readonly_keyboard_event_does_not_change_native_modifiers(self):
        server = FakeServer()
        server.compositor.keyboard_device.layouts = ("us",)
        manager = WaylandKeyboardManager(server)
        manager.setup()
        source = FakeSource("readonly", 1, None, readonly=True)
        server.sources.append(source)
        proto = object()
        server.protocol_sources[proto] = source
        caps = typedict({"keyboard": True, "keymap": {"layout": "us"}})
        manager.parse_hello_ui_keyboard(source, caps)
        manager.add_new_client(source, caps)
        before = manager.device.modifiers

        manager.do_process_keyboard_event(proto, 0, "Shift_L", True, {
            "modifiers": ("shift",), "keyval": SHIFT_L, "string": "",
            "keycode": 50, "group": 0,
        })

        self.assertEqual(manager.device.modifiers, before)
        self.assertFalse(manager.device.keys_down)

    def make_shared_signal_session(self):
        server = FakeServer()
        manager = WaylandKeyboardManager(server)
        manager.setup()  # subscribes to the actual server SignalEmitter
        sources = []
        for index in range(2):
            source = FakeSource(f"client-{index}", index + 1, None)
            proto = object()
            server.sources.append(source)
            server.protocol_sources[proto] = source
            caps = typedict({"keyboard": True, "keymap": {"layout": "us"}})
            manager.parse_hello_ui_keyboard(source, caps)
            manager.add_new_client(source, caps)
            sources.append((proto, source))
        return manager, server, sources

    @staticmethod
    def send_q(manager, proto, pressed):
        manager.do_process_keyboard_event(proto, 0, "q", pressed, {
            "modifiers": (), "keyval": Q, "string": "q", "keycode": 24, "group": 0,
        })

    def test_failed_repeat_admission_retains_releasable_source_holder(self):
        manager, server, sources = self.make_shared_signal_session()
        proto, source = sources[0]
        with patch.object(server, "timeout_add", side_effect=RuntimeError("timer admission failed")):
            # The scheduler method is copied from server by StubSubsystem.
            with patch.object(manager, "timeout_add", server.timeout_add):
                self.send_q(manager, proto, True)
        self.assertEqual(source.key_events, 1)
        self.assertEqual(manager.device.keys_down, {24})
        self.assertEqual(manager._key_holders[24], {(source, 24)})
        self.assertFalse(manager._key_repeat_timers)
        self.send_q(manager, proto, False)
        self.assertFalse(manager.device.keys_down)
        self.assertFalse(manager._key_holders)

    def test_nonowner_departure_signal_does_not_release_surviving_holder(self):
        manager, server, sources = self.make_shared_signal_session()
        (owner_proto, owner), (other_proto, other) = sources
        self.send_q(manager, owner_proto, True)
        self.send_q(manager, other_proto, True)
        server.sources.remove(other)
        server.protocol_sources.pop(other_proto)
        server.emit("client-exited", other)
        self.assertEqual(manager.device.keys_down, {24})
        self.assertEqual(manager._key_holders[24], {(owner, 24)})
        before = (list(manager.device.install_calls), list(manager.device.repeat_calls),
                  list(manager.device.press_calls), server.compositor.flushes)
        manager.cleanup_protocol(other_proto)
        self.assertEqual(before, (manager.device.install_calls, manager.device.repeat_calls,
                                  manager.device.press_calls, server.compositor.flushes))
        self.send_q(manager, owner_proto, False)
        self.assertFalse(manager.device.keys_down)

    def test_departure_from_reader_thread_defers_native_work_to_main_loop(self):
        manager, server, sources = self.make_shared_signal_session()
        (owner_proto, owner), (other_proto, other) = sources
        self.send_q(manager, owner_proto, True)
        self.send_q(manager, other_proto, True)
        # Connection loss reaches client-exited and cleanup_protocol on the
        # protocol reader thread; the seat must only change from the main loop.
        queued = []
        manager.idle_add = lambda callback, *args: queued.append((callback, args))
        server.sources.remove(other)
        server.protocol_sources.pop(other_proto)
        before = (list(manager.device.install_calls), list(manager.device.press_calls),
                  set(manager.device.keys_down), server.compositor.flushes)
        server.emit("client-exited", other)
        manager.cleanup_protocol(other_proto)
        self.assertEqual(len(queued), 2)
        self.assertIn(other, manager._keyboard_configs)
        self.assertEqual(manager._key_holders[24], {(owner, 24), (other, 24)})
        self.assertEqual(before, (manager.device.install_calls, manager.device.press_calls,
                                  manager.device.keys_down, server.compositor.flushes))
        for callback, args in queued:
            callback(*args)
        self.assertNotIn(other, manager._keyboard_configs)
        self.assertEqual(manager.device.keys_down, {24})
        self.assertEqual(manager._key_holders[24], {(owner, 24)})
        self.send_q(manager, owner_proto, False)
        self.assertFalse(manager.device.keys_down)

    def test_real_client_session_exit_precedes_close_and_fallback_cleanup(self):
        manager, server, sources = self.make_shared_signal_session()
        (owner_proto, owner), (other_proto, other) = sources
        self.send_q(manager, owner_proto, True)
        self.send_q(manager, other_proto, True)
        session = ClientSessionServer(server)
        session.sources = server.protocol_sources
        server.get_sources_by_type = session.get_sources_by_type
        server.cleanup_source = session.do_cleanup_source
        server.server_event = Mock()
        chronology = []
        server.connect("client-exited", lambda _server, source: chronology.append(
            ("signal", source not in session.sources.values(), source.closed),
        ))

        def close():
            self.assertNotIn(other, manager._keyboard_configs)
            self.assertEqual(manager.device.keys_down, {24})
            other.closed = True
            chronology.append(("close", True, other.closed))

        other.close = close
        self.assertIs(session.cleanup_client_protocol(other_proto), other)
        self.assertEqual(chronology, [("signal", True, False), ("close", True, True)])
        before = (list(manager.device.install_calls), list(manager.device.repeat_calls),
                  list(manager.device.press_calls), server.compositor.flushes)
        manager.cleanup_protocol(other_proto)
        self.assertEqual(before, (manager.device.install_calls, manager.device.repeat_calls,
                                  manager.device.press_calls, server.compositor.flushes))
        self.send_q(manager, owner_proto, False)
        self.assertFalse(manager.device.keys_down)

    def test_readonly_signal_settles_one_holder_and_promotes_in_both_directions(self):
        manager, server, sources = self.make_shared_signal_session()
        (owner_proto, owner), (other_proto, other) = sources
        self.send_q(manager, owner_proto, True)
        self.send_q(manager, other_proto, True)
        other._readonly = True
        server.emit("setting-changed", "readonly", True, other)
        self.assertEqual(manager.device.keys_down, {24})
        self.assertEqual(manager._key_holders[24], {(owner, 24)})
        other._readonly = False
        server.emit("setting-changed", "readonly", False, other)
        self.send_q(manager, other_proto, True)
        self.assertEqual(manager._key_holders[24], {(owner, 24), (other, 24)})
        owner._readonly = True
        server.emit("setting-changed", "readonly", True, owner)
        self.assertEqual(manager.keyboard_owner, other.uuid)
        self.assertFalse(manager.device.keys_down)


class WaylandKeyboardTranslationTest(unittest.TestCase):

    def test_group_aware_a_q_collision_and_actual_group(self):
        manager, server = manager_fixture()
        source = FakeSource("owner", 1, config(manager, ("us", "fr")))
        accept(manager, server, source)

        for client_keycode, keyname, keyval, keystr, group, expected in (
                (24, "q", Q, "q", 0, (24, 0)),
                (24, "a", A, "a", 1, (24, 1)),
                (38, "a", A, "a", 0, (38, 0))):
            with self.subTest(keyname=keyname, group=group):
                self.assertEqual(
                    manager.get_keycode(source, client_keycode, keyname, True, [], keyval, keystr, group),
                    expected,
                )
                self.assertEqual(
                    manager.get_keycode(source, client_keycode, keyname, False, [], keyval, keystr, group),
                    expected,
                )
        self.assertEqual(manager.device.group, 0)

    def test_group_fallbacks_and_client_without_group_support(self):
        manager, server = manager_fixture()
        owner = FakeSource("owner", 1, config(manager, ("us", "fr")))
        accept(manager, server, owner)
        for unsafe in (-7, 2, 255, True, "1", 1.0, {"": 1}):
            with self.subTest(group=unsafe):
                owner.keyboard_config.pressed_translation.clear()
                self.assertEqual(manager.get_keycode(owner, 24, "q", True, [], Q, "q", unsafe), (24, 0))

        legacy = FakeSource("legacy", 2, config(manager, ("us", "fr"), groups=False))
        accept(manager, server, legacy)
        self.assertEqual(manager.get_keycode(legacy, 24, "q", True, [], Q, "q", 1), (24, 0))

    def test_modifier_group_requires_exact_int_and_preserves_absent_sentinel(self):
        manager, server = manager_fixture()
        source = FakeSource("owner", 1, config(manager, ("us", "fr")))
        accept(manager, server, source)

        manager.device.update_modifiers((), 1)
        manager.update_keyboard_modifiers((), source=source)
        self.assertEqual(manager.device.group, 1)
        self.assertEqual(source.keyboard_config.current_group, 1)

        for unsafe in (True, "1", 1.0, {"": 1}, -7):
            with self.subTest(group=unsafe):
                manager.device.update_modifiers((), 1)
                manager.update_keyboard_modifiers((), unsafe, source=source)
                self.assertEqual(manager.device.group, 0)
                self.assertEqual(source.keyboard_config.current_group, 0)

        manager.update_keyboard_modifiers((), 1, source=source)
        self.assertEqual(manager.device.group, 1)
        self.assertEqual(source.keyboard_config.current_group, 1)

    def test_owner_activation_rejects_a_coercible_saved_group(self):
        manager, server = manager_fixture()
        source = FakeSource("owner", 1, config(manager, ("us", "fr")))
        accept(manager, server, source)
        source.keyboard_config.current_group = "1"

        manager._activate_owner_state(source)

        self.assertEqual(manager.device.group, 0)

    def test_release_keeps_the_pressed_group(self):
        manager, server = manager_fixture()
        source = FakeSource("owner", 1, config(manager, ("us", "fr")))
        accept(manager, server, source)
        pressed = manager.get_keycode(source, 24, "a", True, ["lock"], A, "a", 1)
        released = manager.get_keycode(source, 24, "a", False, [], A, "a", 0)
        self.assertEqual(pressed, (24, 1))
        self.assertEqual(released, pressed)
        self.assertEqual(manager.device.modifiers, ())
        self.assertEqual(manager.device.group, 1)

    def test_altgr_locks_and_repeat_inputs_are_preserved(self):
        manager, server = manager_fixture()
        source = FakeSource("owner", 1, config(manager, ("de",)))
        accept(manager, server, source)
        self.assertEqual(manager.get_keycode(source, 24, "at", True, ["mod5"], AT, "@", 0), (24, 0))
        self.assertEqual(manager.device.resolve_calls[-1][-2], ("mod5",))
        manager.update_keyboard_modifiers(("lock", "mod2"), 0)
        self.assertEqual(manager.device.modifiers, ("lock", "mod2"))
        manager.set_keyboard_repeat(600, 40)
        self.assertEqual(manager.device.repeat_calls[-1], (600, 40))

    def test_inferred_altgr_is_retained_as_current_client_state(self):
        manager, server = manager_fixture()
        source = FakeSource("owner", 1, config(manager, ("de",)))
        accept(manager, server, source)
        modifiers = []
        self.assertEqual(manager.get_keycode(source, 24, "at", True, modifiers, AT, "@", 0), (24, 0))
        self.assertEqual(modifiers, ["mod5"])
        self.assertEqual(source.keyboard_config.current_modifiers, ("mod5",))
        self.assertEqual(manager.device.modifiers, ("mod5",))

    def test_unicode_string_priority_infers_shift_before_base_name(self):
        manager, server = manager_fixture()
        source = FakeSource("owner", 1, config(manager, ("fr", "us")))
        accept(manager, server, source)
        modifiers = []

        self.assertEqual(
            manager.get_keycode(source, 24, "q", True, modifiers, Q, "Q", 1),
            (24, 1),
        )
        self.assertEqual(modifiers, ["shift"])
        self.assertEqual(source.keyboard_config.current_modifiers, ("shift",))
        self.assertEqual(manager.device.modifiers, ("shift",))

    def test_unicode_string_priority_infers_altgr_before_base_name(self):
        manager, server = manager_fixture()
        source = FakeSource("owner", 1, config(manager, ("us", "de")))
        accept(manager, server, source)
        modifiers = []

        self.assertEqual(
            manager.get_keycode(source, 24, "q", True, modifiers, Q, "@", 1),
            (24, 1),
        )
        self.assertEqual(modifiers, ["mod5"])
        self.assertEqual(source.keyboard_config.current_modifiers, ("mod5",))
        self.assertEqual(manager.device.modifiers, ("mod5",))

    def test_repeat_retains_inferred_level_without_pinning_packet_modifiers(self):
        for layouts, keystr, inferred in (
                (("fr", "us"), "Q", "shift"),
                (("us", "de"), "@", "mod5")):
            with self.subTest(layouts=layouts, keystr=keystr):
                manager, server = manager_fixture()
                source = FakeSource("owner", 1, config(manager, layouts))
                accept(manager, server, source)
                proto = WaylandKeyboardPacketTest.bind(manager, server, source)
                event = {
                    "modifiers": ("control",), "keyval": Q, "string": keystr,
                    "keycode": 24, "group": 1,
                }
                manager.do_process_keyboard_event(proto, 0, "q", True, event)
                self.assertEqual(manager.device.keys_down, {24})
                self.assertEqual(manager.device.group, 1)
                self.assertEqual(set(manager.device.modifiers), {inferred, "control"})
                resolve_count = len(manager.device.resolve_calls)
                for modifiers in ((), ("mod1",), ()):
                    # Repeat keeps the physical key and inferred level, while
                    # newly released/pressed real modifiers still take effect.
                    event["modifiers"] = modifiers
                    manager.do_process_keyboard_event(proto, 0, "q", True, event)
                    self.assertEqual(set(manager.device.modifiers), {inferred, *modifiers})
                    self.assertEqual(set(source.keyboard_config.current_modifiers), {inferred, *modifiers})
                    self.assertEqual(source.keyboard_config.pressed_translation, {24: (24, 1)})
                self.assertEqual(len(manager.device.resolve_calls), resolve_count)
                self.assertEqual(manager.device.press_calls, [(24, True)])
                self.assertEqual(set(manager._key_repeat_timers), {(source, 24)})

                manager.do_process_keyboard_event(proto, 0, "q", False, event)
                self.assertEqual(manager.device.modifiers, ())
                self.assertFalse(manager.device.keys_down)
                self.assertFalse(manager._key_repeat_timers)
                self.assertFalse(source.keyboard_config.pressed_modifier_overrides)

    def test_rejected_inference_does_not_poison_reused_key_identity(self):
        manager, server = manager_fixture()
        source = FakeSource("owner", 1, config(manager, ("us",)))
        accept(manager, server, source)
        proto = WaylandKeyboardPacketTest.bind(manager, server, source)
        manager.device.get_keycode_capacity = lambda: 1
        a_event = {"modifiers": (), "keyval": A, "string": "a", "keycode": 38, "group": 0}
        q_event = {"modifiers": (), "keyval": Q, "string": "Q", "keycode": 24, "group": 0}
        manager.do_process_keyboard_event(proto, 0, "a", True, a_event)
        manager.do_process_keyboard_event(proto, 0, "q", True, q_event)
        self.assertEqual(manager.device.keys_down, {38})

        manager.do_process_keyboard_event(proto, 0, "a", False, a_event)
        q_event["string"] = "q"
        for _ in range(2):
            manager.do_process_keyboard_event(proto, 0, "q", True, q_event)
            self.assertEqual(manager.device.modifiers, ())
            self.assertEqual(manager.device.keys_down, {24})
        manager.do_process_keyboard_event(proto, 0, "q", False, q_event)


def native_keyboard_public_api_probe():
    # A fresh interpreter prevents other tests' mocked native imports from
    # replacing the actual compositor, seat and compiled keyboard.
    from xpra.wayland.server.compositor import WaylandCompositor

    check = unittest.TestCase()
    with TemporaryDirectory(prefix="xpra-native-keyboard-") as runtime:
        with patch.dict(os.environ, {
            "XDG_RUNTIME_DIR": runtime, "XPRA_WAYLAND_GPU": "no", "WLR_RENDERER": "pixman",
        }):
            compositor = WaylandCompositor()
            device = None
            try:
                compositor.add_socket()
                device = compositor.get_keyboard_device()
                check.assertIs(device.set_layout("us,fr", variant=","), True)
                check.assertEqual(device.get_keycode_for_keysym(Q), (24, 0))
                check.assertEqual(device.get_keycode_for_keysym(A), (38, 0))
                check.assertEqual(device.get_keycode_for_keysym(A, 1), (24, 1))
                check.assertEqual(device.get_keycode_for_keyname("a", 1), (24, 1))
                device.update_modifiers(("shift", "lock"), 1)
                device.press_key(24, True)
                check.assertIs(device.set_layout("xpra_missing_layout"), False)
                check.assertEqual(device.get_keycodes_down(), (24,))
                check.assertEqual(device.get_layout_group_count(), 2)
                check.assertEqual(device.get_layout_group(), 1)
                check.assertEqual(device.get_modifiers(), ("shift", "lock"))
                check.assertEqual(device.get_keycode_for_keyname("a", 1), (24, 1))

                closed = device.compile_keymap("evdev", "pc105", "de", "", "")
                closed.cleanup()
                with check.assertRaises(ValueError):
                    device.install_keymap(closed)
                check.assertEqual(device.get_keycodes_down(), (24,))
                check.assertEqual(device.get_layout_group(), 1)

                check.assertIs(device.set_layout("de"), True)
                check.assertFalse(device.get_keycodes_down())
                check.assertEqual(device.get_layout_group_count(), 1)
                check.assertEqual(device.get_layout_group(), 0)
                check.assertEqual(device.get_modifiers(), ())
                device.cleanup()
                check.assertIs(device.set_layout("us"), False)
                check.assertEqual(device.get_keycode_for_keysym(Q), (-1, 0))
                check.assertEqual(device.get_keycode_for_keyname("q"), (-1, 0))
            finally:
                if device is not None:
                    device.cleanup()
                compositor.cleanup()


class CompiledXKBKeymapTest(unittest.TestCase):

    @staticmethod
    def compile(layout, variant="", options="", model="pc105", rules="evdev"):
        from xpra.wayland.server.keyboard import WaylandKeyboard
        return WaylandKeyboard.compile_keymap(rules, model, layout, variant, options)

    def test_public_native_api_and_transactional_refusal(self):
        result = subprocess.run((
            sys.executable, "-c",
            "from unit.wayland.keyboard_test import native_keyboard_public_api_probe; "
            "native_keyboard_public_api_probe()",
        ), env=dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path)),
            capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_installed_one_through_maximum_groups(self):
        layouts = ("us", "fr", "ru", "de")
        for count in range(1, len(layouts) + 1):
            with self.subTest(count=count):
                candidate = self.compile(",".join(layouts[:count]), ",".join(("",) * count))
                self.addCleanup(candidate.cleanup)
                self.assertEqual(candidate.group_count, count)

    def test_arbitrary_variants_and_options_are_compiled_exactly(self):
        candidate = self.compile(
            "us,fr,ru,de", "intl,oss,,nodeadkeys", "caps:escape,compose:ralt",
        )
        self.addCleanup(candidate.cleanup)
        self.assertEqual(candidate.group_count, 4)
        self.assertEqual(candidate.rmlvo, {
            "rules": "evdev",
            "model": "pc105",
            "layout": "us,fr,ru,de",
            "variant": "intl,oss,,nodeadkeys",
            "options": "caps:escape,compose:ralt",
        })

    def test_us_fr_collision_is_group_specific(self):
        candidate = self.compile("us,fr", ",")
        self.addCleanup(candidate.cleanup)
        self.assertIn(24, candidate.get_keycodes_for_keyname("q", 0))
        self.assertIn(24, candidate.get_keycodes_for_keyname("a", 1))
        self.assertEqual(candidate.resolve_keycode("q", 0), 24)
        self.assertEqual(candidate.resolve_keycode("a", 1), 24)

    def test_modifiers_locks_altgr_and_dead_key(self):
        us = self.compile("us", "intl")
        de = self.compile("de")
        self.addCleanup(us.cleanup)
        self.addCleanup(de.cleanup)
        self.assertEqual(us.resolve_keycode("Q", 0, ("shift",)), 24)
        self.assertEqual(us.resolve_keycode("A", 0, ("lock",)), 38)
        self.assertGreater(us.resolve_keycode("KP_1", 0, ("mod2",)), 0)
        self.assertGreater(us.resolve_keycode("dead_acute", 0), 0)
        self.assertEqual(de.resolve_keycode("at", 0, ("mod5",)), 24)
        self.assertEqual(de.resolve_keycode("at", 0, ("control", "mod5")), 24)
        self.assertNotEqual(de.resolve_keycode("at", 0, ("shift", "mod5")), 24)

    def test_event_unicode_candidate_priority_precedes_level_trials(self):
        for layout, keystr, inferred_modifier in (
                ("fr,us", "Q", "shift"),
                ("us,de", "@", "mod5")):
            with self.subTest(layout=layout, keystr=keystr):
                candidate = self.compile(layout, ",")
                self.addCleanup(candidate.cleanup)
                modifiers = []

                self.assertEqual(
                    candidate.resolve_event_keycode(Q, "q", (1, 0), modifiers, keystr),
                    (24, 1),
                )
                self.assertEqual(modifiers, [inferred_modifier])

    def test_repeat_keeps_inferred_level_under_real_xkb_state(self):
        for layouts, keystr, keysym, sync in (
                (("fr", "us"), "Q", "Q", True),
                (("us", "de"), "@", "at", True),
                (("fr", "us"), "Q", "Q", False),
                (("us", "de"), "@", "at", False)):
            with self.subTest(layouts=layouts, keystr=keystr, sync=sync):
                candidate = self.compile(",".join(layouts), ",")
                self.addCleanup(candidate.cleanup)
                manager, server = manager_fixture()
                source = FakeSource("owner", 1, config(manager, layouts))
                source.keyboard_config.sync = sync
                accept(manager, server, source)
                manager.device.resolve_keycode = candidate.resolve_event_keycode
                proto = WaylandKeyboardPacketTest.bind(manager, server, source)
                event = {
                    "modifiers": (), "keyval": Q, "string": keystr,
                    "keycode": 24, "group": 1,
                }
                for _ in range(3):
                    manager.do_process_keyboard_event(proto, 0, "q", True, event)
                    self.assertEqual(candidate.resolve_keycode(
                        keysym, manager.device.group, manager.device.modifiers,
                    ), 24)
                manager.do_process_keyboard_event(proto, 0, "q", False, event)
                self.assertFalse(manager.device.keys_down)
                self.assertEqual(manager.device.modifiers, ())

    def test_event_keyname_precedes_printable_keypad_string(self):
        from xpra.wayland.server.keyboard import WaylandKeyboard
        keyboard = WaylandKeyboard.__new__(WaylandKeyboard)
        symbols = keyboard.keysyms(0, "KP_1", "1")
        self.assertGreaterEqual(len(symbols), 2)
        self.assertEqual(symbols[0], 0xFFB1)
        self.assertEqual(symbols[1], ord("1"))

        for name, keystr, expected in (
                ("q", "й", CYRILLIC_SHORT_I),
                ("q", "ض", 0x5D6)):
            with self.subTest(keystr=keystr):
                symbols = keyboard.keysyms(0, name, keystr)
                self.assertEqual(symbols[0], expected)
                self.assertEqual(symbols[1], Q)

    def test_common_keys_are_indexed_in_every_global_group(self):
        candidate = self.compile("us,fr,ru,de", ",,,")
        self.addCleanup(candidate.cleanup)
        for group in range(candidate.group_count):
            for name in ("Return", "space", "Shift_L", "Left"):
                with self.subTest(group=group, name=name):
                    self.assertGreater(candidate.resolve_keycode(name, group), 0)

    def test_unavailable_layout_is_rejected(self):
        with self.assertRaises(ValueError):
            self.compile("xpra_missing_layout")

    def test_models_are_delegated_to_installed_rules(self):
        # evdev deliberately has wildcard model rules.  Passing an unlisted
        # model through to libxkbcommon is therefore different from imposing
        # a server-side model allowlist: the installed rules remain the
        # authority for both an explicit model and the default empty model.
        for model in ("pc104", "", "xpra_missing_model"):
            with self.subTest(model=model):
                candidate = self.compile("us", model=model)
                self.addCleanup(candidate.cleanup)
                self.assertEqual(candidate.group_count, 1)
                self.assertEqual(candidate.rmlvo["model"], model)

    def test_unknown_option_is_not_silently_ignored(self):
        with self.assertRaises(ValueError):
            self.compile("us", options="xpra:missing_option")


class WaylandKeyboardReplacementTest(unittest.TestCase):

    def test_runtime_reorder_settles_input_and_preserves_layout_identity(self):
        manager, server = manager_fixture()
        source = FakeSource("owner", 1, config(manager, ("us", "fr")))
        accept(manager, server, source)
        manager.set_keyboard_repeat(600, 40)
        self.assertEqual(manager.get_keycode(source, 24, "a", True, ["lock"], A, "a", 1), (24, 1))
        manager.fake_key(24, True)
        manager.keys_pressed = {24: "a"}

        compile_count = len(manager.device.compile_calls)
        source.keyboard_config.parse({
            "layouts": ("fr", "us"),
            "variants": ("", ""),
            "layout_groups": True,
        })
        manager.set_keymap(source, force=True)
        self.assertEqual(len(manager.device.compile_calls), compile_count + 1)
        self.assertEqual(manager.device.layouts, ("fr", "us"))
        self.assertEqual(manager.device.group, 0)
        self.assertEqual(source.keyboard_config.current_group, 0)
        self.assertEqual(manager.device.modifiers, ("lock",))
        self.assertEqual(manager.device.clear_calls[-1], (24,))
        self.assertEqual(manager.device.clear_states[-1], (("us", "fr"), 1, (24,)))
        self.assertFalse(manager.keys_pressed)
        self.assertEqual(manager.device.repeat_calls[-1], (600, 40))
        resolve_count = len(manager.device.resolve_calls)
        self.assertEqual(manager.get_keycode(source, 24, "a", False, [], A, "a", 0), (-1, 0))
        self.assertEqual(len(manager.device.resolve_calls), resolve_count)
        self.assertEqual(manager.get_keycode(source, 24, "q", True, [], Q, "q", 1), (24, 1))

        compile_count = len(manager.device.compile_calls)
        manager.set_keymap(source, force=True)
        self.assertEqual(len(manager.device.compile_calls), compile_count)

    def test_reordered_owner_group_survives_demotion_and_repromotion(self):
        manager, server = manager_fixture()
        owner = FakeSource("owner", 1, config(manager, ("us", "fr")))
        fallback = FakeSource("fallback", 2, config(manager, ("de",)))
        accept(manager, server, owner)
        accept(manager, server, fallback)
        manager.update_keyboard_modifiers(("lock",), 1, source=owner)

        owner.keyboard_config.parse({
            "rmlvo-version": 1,
            "layouts": ("fr", "us"),
            "variants": ("", ""),
            "layout_groups": True,
        })
        self.assertTrue(manager.set_keymap(owner, force=True))
        self.assertEqual(manager.device.group, 0)
        self.assertEqual(owner.keyboard_config.current_group, 0)

        owner._readonly = True
        manager.setting_changed_handler(None, "readonly", owner.effective_readonly(), owner)
        self.assertIs(manager._keyboard_owner_source, fallback)
        owner._readonly = False
        fallback._readonly = True
        manager.setting_changed_handler(None, "readonly", fallback.effective_readonly(), fallback)
        self.assertIs(manager._keyboard_owner_source, owner)
        self.assertEqual(manager.device.layouts, ("fr", "us"))
        self.assertEqual(manager.device.group, 0)

    def test_disabling_layout_groups_resets_the_active_group(self):
        manager, server = manager_fixture()
        owner = FakeSource("owner", 1, config(manager, ("us", "fr")))
        accept(manager, server, owner)
        manager.update_keyboard_modifiers(("lock",), 1, source=owner)
        self.assertEqual(manager.device.group, 1)

        owner.keyboard_config.parse({
            "rmlvo-version": 1,
            "layouts": ("us", "fr"),
            "variants": ("", ""),
            "layout_groups": False,
        })
        self.assertTrue(manager.set_keymap(owner, force=True))

        self.assertFalse(owner.keyboard_config.rmlvo.layout_groups)
        self.assertEqual(owner.keyboard_config.current_group, 0)
        self.assertEqual(manager.device.group, 0)
        self.assertEqual(manager.device.modifiers, ("lock",))

    def test_owner_replacement_keeps_foreign_modifier_state_attribution(self):
        manager, server = manager_fixture()
        owner = FakeSource("owner", 1, config(manager, ("us", "fr")))
        other = FakeSource("other", 2, config(manager, ("fr", "us")))
        accept(manager, server, owner)
        accept(manager, server, other)
        manager.update_keyboard_modifiers(("shift",), 0, source=other)
        self.assertIs(manager._keyboard_state_source, other)
        self.assertEqual(manager.device.group, 1)

        owner.keyboard_config.parse({
            "rmlvo-version": 1,
            "layouts": ("fr", "us"),
            "variants": ("", ""),
            "layout_groups": True,
        })
        self.assertTrue(manager.set_keymap(owner, force=True))
        self.assertIs(manager._keyboard_state_source, other)
        self.assertEqual(manager.device.modifiers, ("shift",))
        self.assertEqual(manager.device.group, 0)

        server.sources = [owner]
        manager.cleanup_protocol(object())
        self.assertIs(manager._keyboard_state_source, owner)
        self.assertEqual(manager.device.modifiers, ())
        # The owner's pre-reorder group zero was `us`, now group one.  The
        # departing source's temporary `fr` group must not overwrite that
        # source-local identity.
        self.assertEqual(manager.device.group, 1)

    def test_install_failure_restores_complete_last_good_state(self):
        manager, server = manager_fixture()
        source = FakeSource("owner", 1, config(manager, ("us", "fr")))
        accept(manager, server, source)
        manager.set_keyboard_repeat(650, 45)
        self.assertEqual(manager.get_keycode(source, 24, "a", True, ["lock"], A, "a", 1), (24, 1))
        manager.fake_key(24, True)
        manager.keys_pressed = {24: "a"}
        manager.key_repeat_timer = 77
        active_hash = manager.config_hash
        active_rmlvo = manager.effective_rmlvo
        repeats = tuple(manager.device.repeat_calls)
        presses = tuple(manager.device.press_calls)
        clears = tuple(manager.device.clear_calls)

        source.keyboard_config.parse({
            "rmlvo-version": 1,
            "layouts": ("fr", "us"),
            "variants": ("", ""),
            "layout_groups": True,
            "sync": False,
            "mod_meanings": {"Alt_R": "mod5"},
        })
        source.keyboard_config.current_modifiers = ("mod5",)
        manager.device.fail_install = True
        self.assertFalse(manager.set_keymap(source, force=True))

        self.assertTrue(manager.device.candidates[-1].cleaned)
        self.assertEqual(manager.config_hash, active_hash)
        self.assertIs(manager.effective_rmlvo, active_rmlvo)
        self.assertEqual(manager.device.layouts, ("us", "fr"))
        self.assertEqual(manager.device.group, 1)
        self.assertEqual(manager.device.modifiers, ("lock",))
        self.assertEqual(manager.device.keys_down, {24})
        self.assertEqual(manager.keys_pressed, {24: "a"})
        self.assertEqual(source.keyboard_config.pressed_translation, {24: (24, 1)})
        self.assertTrue(source.keyboard_config.sync)
        self.assertEqual(source.keyboard_config.modifier_meanings, {})
        self.assertEqual(source.keyboard_config.representation, "versioned")
        self.assertEqual(source.keyboard_config.current_modifiers, ("lock",))
        self.assertEqual(manager.key_repeat_timer, 77)
        self.assertEqual(tuple(manager.device.repeat_calls), repeats)
        self.assertEqual(tuple(manager.device.press_calls), presses)
        self.assertEqual(tuple(manager.device.clear_calls), clears)
        self.assertIn("rejected-configuration", manager.get_keyboard_info())

    def test_scheduler_failure_cannot_split_a_committed_replacement(self):
        manager, server = manager_fixture()
        source = FakeSource("owner", 1, config(manager, ("us", "fr")))
        accept(manager, server, source)
        proto = WaylandKeyboardPacketTest.bind(manager, server, source)
        event = {
            "modifiers": (), "keyval": Q, "string": "q", "keycode": 24, "group": 0,
        }
        manager.do_process_keyboard_event(proto, 0, "q", True, event)
        timer = manager._key_repeat_timers[(source, 24)][0]
        manager.source_remove = Mock(side_effect=RuntimeError("scheduler teardown"))

        source.keyboard_config.parse({
            "rmlvo-version": 1,
            "layouts": ("fr", "us"),
            "variants": ("", ""),
            "layout_groups": True,
        })
        self.assertTrue(manager.set_keymap(source, force=True))

        self.assertEqual(manager.device.layouts, ("fr", "us"))
        self.assertEqual(manager.effective_rmlvo.layouts, ("fr", "us"))
        self.assertEqual(manager.config_hash, source.keyboard_config.get_hash())
        self.assertFalse(manager.keys_pressed)
        self.assertFalse(manager._key_holders)
        self.assertFalse(manager._key_repeat_timers)
        self.assertIn(24, source.keyboard_config.settled_translation)

        # Simulate a scheduler which both raised and failed to remove the
        # callback.  Its retired generation must not release input in the new
        # keymap or reconstruct stale manager state.
        _delay, callback, args = server.timers[timer]
        presses = tuple(manager.device.press_calls)
        callback(*args)
        self.assertEqual(tuple(manager.device.press_calls), presses)
        self.assertFalse(manager._key_holders)

    def test_replacement_settles_depressed_modifier_without_resurrection(self):
        manager, server = manager_fixture()
        source = FakeSource("owner", 1, config(manager, ("us", "fr")))
        accept(manager, server, source)
        proto = WaylandKeyboardPacketTest.bind(manager, server, source)
        press = {"modifiers": (), "keyval": SHIFT_L, "string": "", "keycode": 50, "group": 1}
        manager.do_process_keyboard_event(proto, 0, "Shift_L", True, press)
        self.assertEqual(manager.device.modifiers, ("shift",))

        source.keyboard_config.parse({
            "rmlvo-version": 1,
            "layouts": ("fr", "us"),
            "variants": ("", ""),
            "layout_groups": True,
            "mod_meanings": {"Shift_L": "mod5"},
        })
        self.assertTrue(manager.set_keymap(source, force=True))
        self.assertEqual(manager.device.modifiers, ())
        self.assertEqual(source.keyboard_config.current_modifiers, ())
        self.assertFalse(manager._modifier_holders)
        self.assertEqual(manager._settled_modifier_meanings, {(source, 50): "shift"})

        release = dict(press, modifiers=("shift",))
        manager.do_process_keyboard_event(proto, 0, "Shift_L", False, release)
        self.assertEqual(manager.device.modifiers, ())
        self.assertEqual(source.keyboard_config.current_modifiers, ())
        self.assertFalse(manager.device.keys_down)
        self.assertFalse(manager._settled_modifier_meanings)

    def test_replacement_pins_lock_modifier_meaning_until_release(self):
        manager, server = manager_fixture()
        source = FakeSource("owner", 1, config(manager, ("us", "fr")))
        accept(manager, server, source)
        proto = WaylandKeyboardPacketTest.bind(manager, server, source)
        event = {"modifiers": (), "keyval": CAPS_LOCK, "string": "", "keycode": 66, "group": 0}
        manager.do_process_keyboard_event(proto, 0, "Caps_Lock", True, event)
        self.assertEqual(manager.device.modifiers, ("lock",))
        self.assertEqual(manager._settled_modifier_meanings, {(source, 66): "lock"})

        source.keyboard_config.parse({
            "rmlvo-version": 1,
            "layouts": ("fr", "us"),
            "variants": ("", ""),
            "layout_groups": True,
            "mod_meanings": {"Caps_Lock": "shift"},
        })
        self.assertTrue(manager.set_keymap(source, force=True))
        self.assertEqual(manager._settled_modifier_meanings, {(source, 66): "lock"})
        self.assertEqual(manager.device.modifiers, ("lock",))

        event["modifiers"] = ("lock",)
        manager.do_process_keyboard_event(proto, 0, "Caps_Lock", False, event)
        self.assertEqual(manager.device.modifiers, ("lock",))
        self.assertFalse(manager._settled_modifier_meanings)

    def test_invalid_compile_retains_every_active_state(self):
        manager, server = manager_fixture()
        manager.key_repeat_delay = 500
        manager.key_repeat_interval = 30
        source = FakeSource("owner", 1, config(manager, ("us", "fr")))
        accept(manager, server, source)
        manager.device.update_modifiers(("lock", "mod2"), 1)
        manager.device.keys_down.add(24)
        manager.keys_pressed = {24: "a"}
        active_hash = manager.config_hash
        installs = tuple(manager.device.install_calls)
        clears = tuple(manager.device.clear_calls)
        repeats = tuple(manager.device.repeat_calls)

        source.keyboard_config.parse({"layout": "missing", "layout_groups": True})
        manager.set_keymap(source, force=True)
        self.assertEqual(manager.config_hash, active_hash)
        self.assertEqual(tuple(manager.device.install_calls), installs)
        self.assertEqual(tuple(manager.device.clear_calls), clears)
        self.assertEqual(tuple(manager.device.repeat_calls), repeats)
        self.assertEqual(manager.device.layouts, ("us", "fr"))
        self.assertEqual(manager.device.group, 1)
        self.assertEqual(manager.device.modifiers, ("lock", "mod2"))
        self.assertEqual(manager.keys_pressed, {24: "a"})
        self.assertEqual(source.keyboard_config.rmlvo.layouts, ("us", "fr"))
        self.assertIn("rejected-configuration", manager.get_keyboard_info())


class WaylandKeyboardOwnershipTest(unittest.TestCase):

    @staticmethod
    def press_q(manager, proto, client_keycode: int) -> None:
        manager.do_process_keyboard_event(proto, 0, "q", True, {
            "modifiers": (), "keyval": Q, "string": "q",
            "keycode": client_keycode, "group": 0,
        })

    @staticmethod
    def release_q(manager, proto, client_keycode: int) -> None:
        manager.do_process_keyboard_event(proto, 0, "q", False, {
            "modifiers": (), "keyval": Q, "string": "q",
            "keycode": client_keycode, "group": 0,
        })

    def test_wire_key_tracking_is_bounded_and_capacity_is_reusable(self):
        MAX_TRACKED_KEYS_PER_SOURCE = keyboard_module.MAX_TRACKED_KEYS_PER_SOURCE
        manager, server = manager_fixture()
        source = FakeSource("owner", 1, config(manager, ("us",)))
        accept(manager, server, source)
        proto = WaylandKeyboardPacketTest.bind(manager, server, source)

        for client_keycode in range(1, MAX_TRACKED_KEYS_PER_SOURCE + 1):
            self.press_q(manager, proto, client_keycode)
        self.assertEqual(len(manager._tracked_key_tokens()), MAX_TRACKED_KEYS_PER_SOURCE)
        self.assertEqual(len(manager._key_repeat_timers), MAX_TRACKED_KEYS_PER_SOURCE)
        press_calls = tuple(manager.device.press_calls)
        self.press_q(manager, proto, 1)
        self.assertEqual(len(manager._tracked_key_tokens()), MAX_TRACKED_KEYS_PER_SOURCE)
        self.assertEqual(len(manager._key_repeat_timers), MAX_TRACKED_KEYS_PER_SOURCE)
        self.assertEqual(tuple(manager.device.press_calls), press_calls)
        self.press_q(manager, proto, MAX_TRACKED_KEYS_PER_SOURCE + 1)
        self.assertEqual(len(manager._tracked_key_tokens()), MAX_TRACKED_KEYS_PER_SOURCE)
        self.assertNotIn(MAX_TRACKED_KEYS_PER_SOURCE + 1, source.keyboard_config.pressed_translation)

        self.release_q(manager, proto, 1)
        self.assertEqual(len(manager._tracked_key_tokens()), MAX_TRACKED_KEYS_PER_SOURCE - 1)
        self.press_q(manager, proto, MAX_TRACKED_KEYS_PER_SOURCE + 1)
        self.assertEqual(len(manager._tracked_key_tokens()), MAX_TRACKED_KEYS_PER_SOURCE)
        self.assertIn(MAX_TRACKED_KEYS_PER_SOURCE + 1, source.keyboard_config.pressed_translation)

        server.sources = []
        manager.cleanup_protocol(object())
        self.assertFalse(manager._tracked_key_tokens())

    def test_wire_key_tracking_has_a_shared_multi_source_bound(self):
        MAX_TRACKED_KEYS = keyboard_module.MAX_TRACKED_KEYS
        MAX_TRACKED_KEYS_PER_SOURCE = keyboard_module.MAX_TRACKED_KEYS_PER_SOURCE
        manager, server = manager_fixture()
        source_count = MAX_TRACKED_KEYS // MAX_TRACKED_KEYS_PER_SOURCE
        sources = [
            FakeSource(f"source-{index}", index + 1, config(manager, ("us",)))
            for index in range(source_count + 1)
        ]
        protocols = []
        for source in sources:
            accept(manager, server, source)
            protocols.append(WaylandKeyboardPacketTest.bind(manager, server, source))
        for proto in protocols[:source_count]:
            for client_keycode in range(1, MAX_TRACKED_KEYS_PER_SOURCE + 1):
                self.press_q(manager, proto, client_keycode)
        self.assertEqual(len(manager._tracked_key_tokens()), MAX_TRACKED_KEYS)

        extra = sources[-1]
        self.press_q(manager, protocols[-1], 1)
        self.assertNotIn(1, extra.keyboard_config.pressed_translation)
        self.assertEqual(len(manager._tracked_key_tokens()), MAX_TRACKED_KEYS)

        departed = sources[source_count - 1]
        server.sources = [source for source in sources if source is not departed]
        manager.cleanup_protocol(object())
        self.assertEqual(
            len(manager._tracked_key_tokens()),
            MAX_TRACKED_KEYS - MAX_TRACKED_KEYS_PER_SOURCE,
        )
        self.press_q(manager, protocols[-1], 1)
        self.assertIn(1, extra.keyboard_config.pressed_translation)

    def test_distinct_server_keycodes_respect_native_capacity(self):
        manager, server = manager_fixture()
        source = FakeSource("owner", 1, config(manager, ("us",)))
        accept(manager, server, source)
        proto = WaylandKeyboardPacketTest.bind(manager, server, source)
        capacity = manager.device.get_keycode_capacity()
        manager.device.resolve_keycode = Mock(
            side_effect=lambda keyval, _name, _groups, _modifiers,
            _keystr="", _fixed_modifiers=(): (keyval, 0),
        )

        def send(client_keycode: int, server_keycode: int, pressed: bool) -> None:
            manager.do_process_keyboard_event(proto, 0, f"key-{client_keycode}", pressed, {
                "modifiers": (), "keyval": server_keycode, "string": "",
                "keycode": client_keycode, "group": 0,
            })

        for index in range(capacity):
            send(index + 1, index + 8, True)
        self.assertEqual(len(manager.device.keys_down), capacity)
        self.assertEqual(len(manager._key_holders), capacity)

        # A distinct wire identity aliasing an already-held server keycode is
        # safe and must not consume another native held-keycode slot.
        send(capacity + 1, 8, True)
        self.assertEqual(len(manager._key_holders[8]), 2)
        self.assertEqual(len(manager.device.keys_down), capacity)

        rejected_identity = capacity + 2
        rejected_keycode = capacity + 8
        send(rejected_identity, rejected_keycode, True)
        self.assertNotIn(rejected_keycode, manager.device.keys_down)
        self.assertNotIn(rejected_keycode, manager._key_holders)
        self.assertNotIn(rejected_identity, source.keyboard_config.pressed_translation)
        self.assertNotIn((source, rejected_identity), manager._key_repeat_timers)

        # Releasing the only holder of one distinct key makes that exact native
        # slot reusable; the previously rejected identity has no stale mapping.
        send(2, 9, False)
        send(rejected_identity, rejected_keycode, True)
        self.assertIn(rejected_keycode, manager.device.keys_down)
        self.assertIn(rejected_identity, source.keyboard_config.pressed_translation)
        self.assertEqual(len(manager.device.keys_down), capacity)

    def test_unavailable_configured_bootstrap_falls_back_to_us(self):
        server = FakeServer()
        manager = WaylandKeyboardManager(server)
        manager.keymap_options = {"layout": "missing"}
        manager.setup()

        self.assertEqual(manager.device.layouts, ("us",))
        self.assertEqual(manager.bootstrap_config.rmlvo.layouts, ("us",))
        self.assertEqual(manager.effective_rmlvo.layouts, ("us",))
        self.assertEqual(manager.config_hash, manager.bootstrap_config.get_hash())
        self.assertIn("libxkbcommon rejected", manager.last_rejected["reason"])

        owner = FakeSource("owner", 1, config(manager, ("fr",)))
        accept(manager, server, owner)
        self.assertEqual(manager.device.layouts, ("fr",))
        server.sources = []
        manager.cleanup_protocol(object())
        self.assertEqual(manager.device.layouts, ("us",))
        self.assertEqual(manager.device.group, 0)
        self.assertEqual(manager.device.modifiers, ())

    def test_empty_parser_defaults_are_absent_bootstrap_fields(self):
        server = FakeServer()
        manager = WaylandKeyboardManager(server)
        manager.keymap_options = {
            "sync": False,
            "model": "",
            "layout": "",
            "layouts": [],
            "variant": "",
            "variants": [],
            "options": "",
        }
        manager.setup()

        self.assertEqual(manager.keymap_options, {"sync": False})
        self.assertEqual(manager.device.layouts, ("us",))
        self.assertEqual(manager.effective_rmlvo.layouts, ("us",))
        self.assertEqual(manager.effective_rmlvo.model, "pc105")
        self.assertFalse(manager.config.sync)
        self.assertNotIn("rejected-configuration", manager.get_keyboard_info())

    def test_legacy_bootstrap_current_layout_precedes_selection_lists(self):
        server = FakeServer()
        manager = WaylandKeyboardManager(server)
        manager.keymap_options = {
            "model": "pc104",
            "layout": "de",
            "layouts": ["us", "fr"],
            "variant": "nodeadkeys",
            "variants": ["", "oss"],
            "options": "caps:escape",
        }
        manager.setup()

        self.assertEqual(manager.device.layouts, ("de",))
        self.assertEqual(manager.effective_rmlvo.variants, ("nodeadkeys",))
        self.assertEqual(manager.effective_rmlvo.model, "pc104")
        self.assertEqual(manager.effective_rmlvo.options, "caps:escape")
        self.assertEqual(manager.config.representation, "legacy")

        bootstrap = manager.bootstrap_config
        owner = FakeSource("owner", 1, config(manager, ("fr",)))
        accept(manager, server, owner)
        self.assertEqual(manager.device.layouts, ("fr",))
        self.assertEqual(owner.keyboard_config.owner, "owner")
        self.assertIsNone(bootstrap.owner)

        server.sources = []
        manager.cleanup_protocol(object())
        self.assertIs(manager.config, bootstrap)
        self.assertIs(manager.bootstrap_config, bootstrap)
        self.assertEqual(manager.keyboard_owner, "")
        self.assertEqual(bootstrap.owner, "")
        self.assertIsNone(owner.keyboard_config.owner)
        self.assertEqual(manager.device.layouts, ("de",))
        self.assertEqual(manager.effective_rmlvo.rules, "evdev")
        self.assertEqual(manager.effective_rmlvo.model, "pc104")
        self.assertEqual(manager.effective_rmlvo.layouts, ("de",))
        self.assertEqual(manager.effective_rmlvo.variants, ("nodeadkeys",))
        self.assertEqual(manager.effective_rmlvo.options, "caps:escape")
        self.assertEqual(
            manager.device.compile_calls[-1],
            ("evdev", "pc104", "de", "nodeadkeys", "caps:escape"),
        )

    def test_non_owner_translation_and_owner_promotion(self):
        manager, server = manager_fixture()
        owner = FakeSource("owner", 1, config(manager, ("us", "fr")))
        other = FakeSource("other", 2, config(manager, ("fr", "us")))
        readonly = FakeSource("readonly", 3, config(manager, ("ru",)), readonly=True)
        recorder = FakeSource("recorder", 4, config(manager, ("ru",)), record=True)
        server.sources = [owner, other, readonly, recorder]
        manager.add_new_client(owner, typedict())
        owner.keyboard_config.current_modifiers = ("lock",)
        owner.keyboard_config.current_group = 1
        manager.device.update_modifiers(("lock",), 1)
        manager.set_keyboard_repeat(650, 45)
        compile_count = len(manager.device.compile_calls)
        manager.add_new_client(other, typedict())
        manager.add_new_client(readonly, typedict())
        manager.add_new_client(recorder, typedict())
        self.assertEqual(manager.keyboard_owner, "owner")
        self.assertEqual(owner.keyboard_config.owner, "owner")
        self.assertIsNone(other.keyboard_config.owner)
        self.assertIsNone(readonly.keyboard_config.owner)
        self.assertIsNone(recorder.keyboard_config.owner)
        self.assertIsNone(manager.bootstrap_config.owner)
        self.assertEqual(len(manager.device.compile_calls), compile_count + 3)
        self.assertEqual(manager.device.install_calls[-1], ("us", "fr"))
        self.assertEqual(manager.device.group, 1)
        self.assertEqual(manager.device.modifiers, ("lock",))
        self.assertEqual(manager.device.repeat_calls[-1], (650, 45))
        self.assertEqual(manager.get_keycode(other, 24, "a", True, [], A, "a", 0), (24, 1))
        self.assertEqual(manager.get_keycode(readonly, 24, "Cyrillic_shorti", True, [], CYRILLIC_SHORT_I, "", 0),
                         (-1, 0))
        self.assertEqual(manager.get_keycode(recorder, 24, "Cyrillic_shorti", True, [], CYRILLIC_SHORT_I, "", 0),
                         (-1, 0))

        server.sources = [other, readonly, recorder]
        manager.cleanup_protocol(object())
        self.assertEqual(manager.keyboard_owner, "other")
        self.assertIsNone(owner.keyboard_config.owner)
        self.assertEqual(other.keyboard_config.owner, "other")
        self.assertEqual(manager.device.layouts, ("fr", "us"))

        server.sources = [readonly, recorder]
        manager.cleanup_protocol(object())
        self.assertEqual(manager.keyboard_owner, "")
        self.assertIsNone(other.keyboard_config.owner)
        self.assertEqual(manager.bootstrap_config.owner, "")
        self.assertEqual(manager.device.layouts, ("us",))

    def test_nonowner_reorder_preserves_group_identity_for_later_promotion(self):
        manager, server = manager_fixture()
        owner = FakeSource("owner", 1, config(manager, ("us", "fr")))
        other = FakeSource("other", 2, config(manager, ("us", "fr")))
        accept(manager, server, owner)
        accept(manager, server, other)
        manager.update_keyboard_modifiers((), 1, source=other)
        self.assertEqual(other.keyboard_config.current_group, 1)
        proto = WaylandKeyboardPacketTest.bind(manager, server, other)

        manager._process_config(proto, Packet("keyboard-config", {
            "rmlvo-version": 1,
            "layouts": ("fr", "us"),
            "variants": ("", ""),
            "layout_groups": True,
        }))

        self.assertIs(manager._keyboard_owner_source, owner)
        self.assertEqual(manager.device.layouts, ("us", "fr"))
        self.assertEqual(manager.device.group, 1)
        self.assertEqual(other.keyboard_config.rmlvo.layouts, ("fr", "us"))
        self.assertEqual(other.keyboard_config.current_group, 0)
        server.sources = [other]
        manager.cleanup_protocol(object())
        self.assertIs(manager._keyboard_owner_source, other)
        self.assertEqual(manager.device.layouts, ("fr", "us"))
        self.assertEqual(manager.device.group, 0)

    def test_stale_owner_packet_promotes_earliest_validated_client(self):
        manager, server = manager_fixture()
        stale = FakeSource("stale", 1, config(manager, ("us",)))
        fallback = FakeSource("fallback", 2, config(manager, ("fr",)))
        later = FakeSource("later", 3, config(manager, ("ru",)))
        accept(manager, server, stale)
        accept(manager, server, fallback)
        accept(manager, server, later)
        proto = WaylandKeyboardPacketTest.bind(manager, server, later)
        stale.closed = True

        manager._process_config(proto, Packet("keyboard-config", {
            "rmlvo-version": 1,
            "layouts": ("de",),
            "variants": ("",),
            "layout_groups": True,
        }))

        self.assertIs(manager._keyboard_owner_source, fallback)
        self.assertEqual(manager.device.layouts, ("fr",))
        self.assertEqual(later.keyboard_config.rmlvo.layouts, ("de",))
        self.assertEqual(later.keyboard_config.validated_hash, later.keyboard_config.get_hash())
        server.sources = [later]
        manager.cleanup_protocol(object())
        self.assertIs(manager._keyboard_owner_source, later)
        self.assertEqual(manager.device.layouts, ("de",))

    def test_unrelated_departure_settles_a_stale_owner_before_identical_promotion(self):
        manager, server = manager_fixture()
        owner = FakeSource("owner", 1, config(manager, ("us",)))
        fallback = FakeSource("fallback", 2, config(manager, ("us",)))
        departing = FakeSource("departing", 3, config(manager, ("us",)))
        for source in (owner, fallback, departing):
            accept(manager, server, source)
        proto = WaylandKeyboardPacketTest.bind(manager, server, owner)
        self.press_q(manager, proto, 24)
        self.assertEqual(manager.device.keys_down, {24})

        owner.closed = True
        server.sources = [owner, fallback]
        manager.cleanup_protocol(object())

        self.assertIs(manager._keyboard_owner_source, fallback)
        self.assertIsNone(owner.keyboard_config.owner)
        self.assertEqual(fallback.keyboard_config.owner, "fallback")
        self.assertIsNone(manager.bootstrap_config.owner)
        self.assertEqual(manager.device.layouts, ("us",))
        self.assertFalse(manager.device.keys_down)
        self.assertFalse(manager.keys_pressed)
        self.assertFalse(manager._key_holders)
        self.assertFalse(manager._key_repeat_timers)
        self.assertFalse(owner.keyboard_config.pressed_translation)
        self.assertIn(24, owner.keyboard_config.settled_translation)

    def test_disjoint_non_owner_never_uses_raw_keycode(self):
        manager, server = manager_fixture()
        owner = FakeSource("owner", 1, config(manager, ("us", "fr")))
        foreign = FakeSource("foreign", 2, config(manager, ("ru",)))
        server.sources = [owner, foreign]
        manager.add_new_client(owner, typedict())
        manager.add_new_client(foreign, typedict())
        self.assertEqual(
            manager.get_keycode(foreign, 99, "Cyrillic_shorti", True, [], CYRILLIC_SHORT_I, "", 0),
            (-1, 0),
        )

    def test_disabled_and_denied_recording_clients_never_own_or_inject(self):
        manager, server = manager_fixture()
        disabled_config = config(manager, ("ru",))
        disabled_config.enabled = False
        disabled = FakeSource("disabled", 1, disabled_config)
        denied_recorder = FakeSource(
            "denied-recorder", 2, config(manager, ("ru",)), record=False, record_requested=True,
        )
        accept(manager, server, disabled)
        accept(manager, server, denied_recorder)
        self.assertEqual(manager.keyboard_owner, "")
        self.assertEqual(manager.device.layouts, ("us",))
        for source in (disabled, denied_recorder):
            self.assertEqual(
                manager.get_keycode(source, 24, "Cyrillic_shorti", True, [], CYRILLIC_SHORT_I, "й", 0),
                (-1, 0),
            )

    def test_same_uuid_reconnect_is_tracked_by_source_identity(self):
        manager, server = manager_fixture()
        old = FakeSource("same", 1, config(manager, ("us", "fr")))
        new = FakeSource("same", 2, config(manager, ("fr", "us")))
        accept(manager, server, old)
        accept(manager, server, new)
        self.assertIs(manager._keyboard_owner_source, old)

        self.assertEqual(manager.get_keycode(old, 24, "a", True, [], A, "a", 1), (24, 1))
        manager.fake_key(24, True)
        manager.keys_pressed = {24: "a"}
        server.sources = [new]
        manager.cleanup_protocol(object())

        self.assertIs(manager._keyboard_owner_source, new)
        self.assertEqual(manager.keyboard_owner, "same")
        self.assertEqual(manager.device.layouts, ("fr", "us"))
        self.assertFalse(manager.keys_pressed)
        self.assertFalse(old.keyboard_config.pressed_translation)

    def test_non_owner_departure_settles_its_input_without_changing_owner_map(self):
        manager, server = manager_fixture()
        owner = FakeSource("owner", 1, config(manager, ("us", "fr")))
        other = FakeSource("other", 2, config(manager, ("fr", "us")))
        accept(manager, server, owner)
        accept(manager, server, other)
        self.assertEqual(manager.get_keycode(other, 24, "a", True, [], A, "a", 0), (24, 1))
        manager._handle_key(0, True, "a", A, 24, [], False, True, other)
        # A stale metadata value from an older server must not outlive removal
        # merely because the real owner remains active and cleanup returns
        # without a promotion transition.
        other.keyboard_config.owner = "stale-other"
        installs = tuple(manager.device.install_calls)

        server.sources = [owner]
        manager.cleanup_protocol(object())
        self.assertIs(manager._keyboard_owner_source, owner)
        self.assertEqual(manager.device.layouts, ("us", "fr"))
        self.assertEqual(tuple(manager.device.install_calls), installs)
        self.assertFalse(manager.keys_pressed)
        self.assertFalse(other.keyboard_config.pressed_translation)
        self.assertIsNone(other.keyboard_config.owner)

    def test_clients_sharing_a_server_keycode_do_not_release_each_other(self):
        manager, server = manager_fixture()
        owner = FakeSource("owner", 1, config(manager, ("us", "fr")))
        other = FakeSource("other", 2, config(manager, ("fr", "us")))
        accept(manager, server, owner)
        accept(manager, server, other)
        protocols = [WaylandKeyboardPacketTest.bind(manager, server, source) for source in (owner, other)]

        for proto, source, group in ((protocols[0], owner, 0), (protocols[1], other, 1)):
            manager.do_process_keyboard_event(proto, 0, "q", True, {
                "modifiers": (), "keyval": Q, "string": "q", "keycode": 24, "group": group,
            })
            self.assertTrue(any(token[0] is source for token in manager._key_holders[24]))
        self.assertEqual(manager.device.keys_down, {24})

        manager.do_process_keyboard_event(protocols[0], 0, "q", False, {
            "modifiers": (), "keyval": Q, "string": "q", "keycode": 24, "group": 0,
        })
        self.assertEqual(manager.device.keys_down, {24})
        self.assertEqual({token[0] for token in manager._key_holders[24]}, {other})
        manager.do_process_keyboard_event(protocols[1], 0, "q", False, {
            "modifiers": (), "keyval": Q, "string": "q", "keycode": 24, "group": 1,
        })
        self.assertFalse(manager.device.keys_down)
        self.assertNotIn(24, manager._key_holders)

    def test_one_source_holding_two_client_keys_for_one_server_keycode(self):
        manager, server = manager_fixture()
        source = FakeSource("owner", 1, config(manager, ("us",)))
        accept(manager, server, source)
        proto = WaylandKeyboardPacketTest.bind(manager, server, source)

        for client_keycode in (9, 66):
            manager.do_process_keyboard_event(proto, 0, "q", True, {
                "modifiers": (), "keyval": Q, "string": "q",
                "keycode": client_keycode, "group": 0,
            })
        self.assertEqual(len(manager._key_holders[24]), 2)
        self.assertEqual(set(manager._key_repeat_timers), {(source, 9), (source, 66)})
        self.assertEqual(manager.device.keys_down, {24})

        manager.do_process_keyboard_event(proto, 0, "q", False, {
            "modifiers": (), "keyval": Q, "string": "q", "keycode": 9, "group": 0,
        })
        self.assertEqual(len(manager._key_holders[24]), 1)
        self.assertEqual(set(manager._key_repeat_timers), {(source, 66)})
        self.assertEqual(manager.device.keys_down, {24})
        manager.do_process_keyboard_event(proto, 0, "q", False, {
            "modifiers": (), "keyval": Q, "string": "q", "keycode": 66, "group": 0,
        })
        self.assertNotIn(24, manager._key_holders)
        self.assertFalse(manager.device.keys_down)

    def test_last_client_clear_removes_shared_holder_state(self):
        manager, server = manager_fixture()
        source = FakeSource("owner", 1, config(manager, ("us",)))
        accept(manager, server, source)
        proto = WaylandKeyboardPacketTest.bind(manager, server, source)
        manager.do_process_keyboard_event(proto, 0, "q", True, {
            "modifiers": (), "keyval": Q, "string": "q", "keycode": 24, "group": 0,
        })
        manager.clear_keys_pressed()
        self.assertFalse(manager.keys_pressed)
        self.assertFalse(manager.device.keys_down)
        self.assertFalse(manager._key_holders)
        self.assertFalse(manager._key_repeat_timers)
        self.assertFalse(source.keyboard_config.pressed_translation)

    def test_non_owner_departure_restores_owner_modifiers(self):
        manager, server = manager_fixture()
        owner = FakeSource("owner", 1, config(manager, ("us", "fr")))
        other = FakeSource("other", 2, config(manager, ("fr", "us")))
        accept(manager, server, owner)
        accept(manager, server, other)
        owner.keyboard_config.current_modifiers = ("lock",)
        owner.keyboard_config.current_group = 0
        self.assertEqual(manager.get_keycode(other, 24, "q", True, ["mod5"], Q, "q", 1), (24, 0))
        manager._handle_key(0, True, "q", Q, 24, ["mod5"], False, True, other)
        self.assertEqual(manager.device.modifiers, ("mod5",))

        server.sources = [owner]
        manager.cleanup_protocol(object())
        self.assertEqual(manager.device.modifiers, ("lock",))
        self.assertEqual(manager.device.group, 0)
        self.assertFalse(other.keyboard_config.pressed_translation)

    def test_group_less_nonowner_modifiers_preserve_active_group_after_departure(self):
        for owner_layouts, active_group, other_layouts, expected_other_group in (
                (("us", "fr"), 0, ("fr", "us"), 1),
                (("us", "us", "fr"), 1, ("us", "fr", "us"), 2),
                (("us", "us", "us"), 2, ("us",), 0)):
            with self.subTest(
                    owner_layouts=owner_layouts,
                    active_group=active_group,
                    other_layouts=other_layouts):
                manager, server = manager_fixture()
                owner = FakeSource("owner", 1, config(manager, owner_layouts))
                other = FakeSource("other", 2, config(manager, other_layouts))
                holder = FakeSource("holder", 3, config(manager, owner_layouts))
                accept(manager, server, owner)
                accept(manager, server, other)
                accept(manager, server, holder)
                manager.update_keyboard_modifiers((), active_group, source=owner)

                proto = WaylandKeyboardPacketTest.bind(manager, server, holder)
                manager.do_process_keyboard_event(proto, 0, "Shift_L", True, {
                    "modifiers": (), "keyval": SHIFT_L, "string": "",
                    "keycode": 50, "group": active_group,
                })
                manager.update_keyboard_modifiers(("lock",), source=other)
                self.assertEqual(other.keyboard_config.current_group, expected_other_group)
                self.assertEqual(manager.device.group, active_group)
                self.assertEqual(manager.device.modifiers, ("shift", "lock"))

                server.sources = [owner, other]
                manager.cleanup_protocol(object())
                self.assertIs(manager._keyboard_state_source, other)
                self.assertEqual(manager.device.group, active_group)
                self.assertEqual(manager.device.modifiers, ("lock",))

    def test_foreign_symbol_fallback_group_survives_holder_departure(self):
        manager, server = manager_fixture()
        owner = FakeSource("owner", 1, config(manager, ("fr", "de")))
        other = FakeSource("other", 2, config(manager, ("ru",)))
        holder = FakeSource("holder", 3, config(manager, ("fr", "de")))
        accept(manager, server, owner)
        accept(manager, server, other)
        accept(manager, server, holder)

        holder_proto = WaylandKeyboardPacketTest.bind(manager, server, holder)
        # Control does not change AD01's XKB level.  Shift+Level3 would
        # produce Greek_OMEGA on the real German map, making '@' impossible
        # while the resolver correctly preserves the held Shift key.
        manager.do_process_keyboard_event(holder_proto, 0, "Control_L", True, {
            "modifiers": (), "keyval": CONTROL_L, "string": "",
            "keycode": 37, "group": 0,
        })
        other_proto = WaylandKeyboardPacketTest.bind(manager, server, other)
        manager.do_process_keyboard_event(other_proto, 0, "at", True, {
            "modifiers": (), "keyval": AT, "string": "@",
            "keycode": 24, "group": 0,
        })
        self.assertEqual(manager.device.group, 1)
        self.assertEqual(manager.device.modifiers, ("control", "mod5"))
        self.assertIs(manager._keyboard_state_source, other)

        server.sources = [owner, other]
        manager.cleanup_protocol(object())
        self.assertEqual(manager.device.group, 1)
        self.assertEqual(manager.device.modifiers, ("mod5",))
        self.assertEqual(manager.device.keys_down, {24})
        self.assertIs(manager._keyboard_state_source, other)

    def test_readonly_transition_settles_and_repromotes(self):
        manager, server = manager_fixture()
        owner = FakeSource("owner", 1, config(manager, ("us", "fr")))
        accept(manager, server, owner)
        self.assertEqual(manager.get_keycode(owner, 24, "q", True, [], Q, "q", 0), (24, 0))
        manager.fake_key(24, True)
        manager.keys_pressed = {24: "q"}

        owner._readonly = True
        manager.setting_changed_handler(None, "readonly", owner.effective_readonly(), owner)
        self.assertEqual(manager.keyboard_owner, "")
        self.assertIsNone(owner.keyboard_config.owner)
        self.assertEqual(manager.bootstrap_config.owner, "")
        self.assertEqual(manager.device.layouts, ("us",))
        self.assertFalse(manager.keys_pressed)

        owner._readonly = False
        manager.setting_changed_handler(None, "readonly", owner.effective_readonly(), owner)
        self.assertIs(manager._keyboard_owner_source, owner)
        self.assertEqual(owner.keyboard_config.owner, "owner")
        self.assertIsNone(manager.bootstrap_config.owner)
        self.assertEqual(manager.device.layouts, ("us", "fr"))

    def test_promotion_retries_a_previous_applied_map_after_bad_non_owner_update(self):
        manager, server = manager_fixture()
        fallback = FakeSource("fallback", 2, config(manager, ("fr", "us")))
        accept(manager, server, fallback)
        applied_hash = fallback.keyboard_config.applied_hash

        fallback._readonly = True
        manager.setting_changed_handler(None, "readonly", fallback.effective_readonly(), fallback)
        owner = FakeSource("owner", 1, config(manager, ("us", "fr")))
        accept(manager, server, owner)
        fallback._readonly = False
        manager.setting_changed_handler(None, "readonly", fallback.effective_readonly(), fallback)
        proto = WaylandKeyboardPacketTest.bind(manager, server, fallback)
        manager._process_config(proto, Packet("keyboard-config", {
            "rmlvo-version": 1,
            "layouts": ("missing",),
            "variants": ("",),
            "layout_groups": True,
        }))
        self.assertEqual(fallback.keyboard_config.get_hash(), applied_hash)
        self.assertTrue(fallback.keyboard_config.rejected)

        server.sources = [fallback]
        manager.cleanup_protocol(object())
        self.assertIs(manager._keyboard_owner_source, fallback)
        self.assertEqual(manager.keyboard_owner, "fallback")
        self.assertEqual(manager.device.layouts, ("fr", "us"))
        self.assertEqual(fallback.keyboard_config.applied_hash, applied_hash)
        self.assertFalse(fallback.keyboard_config.rejected)
        self.assertIn("rejected-configuration", manager.get_keyboard_info())

    def test_promotion_rolls_back_validated_map_after_install_failure(self):
        manager, server = manager_fixture()
        fallback = FakeSource("fallback", 2, config(manager, ("us", "fr")))
        accept(manager, server, fallback)
        manager.update_keyboard_modifiers(("lock",), 1, source=fallback)
        applied_hash = fallback.keyboard_config.applied_hash

        fallback._readonly = True
        manager.setting_changed_handler(None, "readonly", fallback.effective_readonly(), fallback)
        owner = FakeSource("owner", 1, config(manager, ("de",)))
        accept(manager, server, owner)
        fallback._readonly = False
        manager.setting_changed_handler(None, "readonly", fallback.effective_readonly(), fallback)
        proto = WaylandKeyboardPacketTest.bind(manager, server, fallback)
        manager._process_config(proto, Packet("keyboard-config", {
            "rmlvo-version": 1,
            "layouts": ("fr", "us"),
            "variants": ("", ""),
            "layout_groups": True,
            "modifiers": ("mod5",),
        }))
        manager.update_keyboard_modifiers(("mod5",), 1, source=fallback)
        self.assertEqual(fallback.keyboard_config.rmlvo.layouts, ("fr", "us"))
        self.assertEqual(fallback.keyboard_config.current_group, 1)
        self.assertEqual(fallback.keyboard_config.current_modifiers, ("mod5",))
        self.assertNotEqual(fallback.keyboard_config.validated_hash, applied_hash)

        install_keymap = manager.device.install_keymap
        failed = False

        def fail_new_map_once(candidate, *args, **kwargs):
            nonlocal failed
            if candidate.layouts == ("fr", "us") and not failed:
                failed = True
                raise RuntimeError("transient install failure")
            return install_keymap(candidate, *args, **kwargs)

        manager.device.install_keymap = fail_new_map_once
        server.sources = [fallback]
        manager.cleanup_protocol(object())

        self.assertTrue(failed)
        self.assertIs(manager._keyboard_owner_source, fallback)
        self.assertEqual(manager.device.layouts, ("us", "fr"))
        self.assertEqual(fallback.keyboard_config.rmlvo.layouts, ("us", "fr"))
        self.assertEqual(fallback.keyboard_config.get_hash(), applied_hash)
        self.assertEqual(fallback.keyboard_config.validated_hash, applied_hash)
        self.assertEqual(fallback.keyboard_config.applied_hash, applied_hash)
        self.assertEqual(fallback.keyboard_config.current_group, 0)
        self.assertEqual(fallback.keyboard_config.current_modifiers, ("mod5",))
        self.assertEqual(manager.device.group, 0)
        self.assertEqual(manager.device.modifiers, ("mod5",))
        self.assertFalse(fallback.keyboard_config.rejected)
        self.assertEqual(
            manager.get_keyboard_info()["rejected-configuration"]["client"],
            "fallback",
        )

    def test_invalid_initial_configuration_cannot_inject(self):
        manager, server = manager_fixture()
        source = FakeSource("invalid", 1, None)
        manager.parse_hello_ui_keyboard(source, typedict({
            "keyboard": True, "keymap": {"layout": "../bad", "delay": True},
        }))
        accept(manager, server, source)
        proto = WaylandKeyboardPacketTest.bind(manager, server, source)

        manager.do_process_keyboard_event(proto, 0, "q", True, {
            "modifiers": (), "keyval": Q, "string": "q", "keycode": 24, "group": 0,
        })
        self.assertFalse(manager._eligible(source))
        self.assertEqual(manager.keyboard_owner, "")
        self.assertFalse(manager.device.keys_down)
        self.assertEqual(manager.get_keyboard_info()["rejected-configuration"]["client"], "invalid")

    def test_unavailable_initial_nonowner_map_is_validated_without_clobbering_owner(self):
        manager, server = manager_fixture()
        owner = FakeSource("owner", 1, config(manager, ("us",)))
        unavailable = FakeSource("unavailable", 2, config(manager, ("missing",)))
        accept(manager, server, owner)
        installs = tuple(manager.device.install_calls)
        accept(manager, server, unavailable)

        self.assertIs(manager._keyboard_owner_source, owner)
        self.assertEqual(tuple(manager.device.install_calls), installs)
        self.assertFalse(unavailable.keyboard_config.valid)
        self.assertFalse(manager._eligible(unavailable))
        self.assertEqual(
            manager.get_keyboard_info()["rejected-configuration"]["client"], "unavailable",
        )

    def test_readonly_release_does_not_poison_the_next_press(self):
        manager, server = manager_fixture()
        source = FakeSource("owner", 1, config(manager, ("us",)))
        accept(manager, server, source)
        proto = WaylandKeyboardPacketTest.bind(manager, server, source)
        event = {"modifiers": (), "keyval": Q, "string": "q", "keycode": 24, "group": 0}
        manager.do_process_keyboard_event(proto, 0, "q", True, event)

        source._readonly = True
        manager.setting_changed_handler(None, "readonly", source.effective_readonly(), source)
        self.assertIn(24, source.keyboard_config.settled_translation)
        manager.do_process_keyboard_event(proto, 0, "q", False, event)
        self.assertNotIn(24, source.keyboard_config.settled_translation)

        source._readonly = False
        manager.setting_changed_handler(None, "readonly", source.effective_readonly(), source)
        manager.do_process_keyboard_event(proto, 0, "q", True, event)
        self.assertEqual(manager.device.keys_down, {24})
        manager.do_process_keyboard_event(proto, 0, "q", False, event)

    def test_late_modifier_release_after_readonly_reenable(self):
        manager, server = manager_fixture()
        owner = FakeSource("owner", 1, config(manager, ("us",)))
        source = FakeSource("source", 2, config(manager, ("us",)))
        accept(manager, server, owner)
        accept(manager, server, source)
        owner_proto = WaylandKeyboardPacketTest.bind(manager, server, owner)
        source_proto = WaylandKeyboardPacketTest.bind(manager, server, source)
        event = {"modifiers": (), "keyval": SHIFT_L, "string": "", "keycode": 50, "group": 0}
        manager.do_process_keyboard_event(owner_proto, 0, "Shift_L", True, event)
        manager.do_process_keyboard_event(source_proto, 0, "Shift_L", True, event)

        source._readonly = True
        manager.setting_changed_handler(None, "readonly", source.effective_readonly(), source)
        source._readonly = False
        manager.setting_changed_handler(None, "readonly", source.effective_readonly(), source)

        release = dict(event, modifiers=("shift",))
        manager.do_process_keyboard_event(source_proto, 0, "Shift_L", False, release)
        self.assertEqual(source.keyboard_config.current_modifiers, ())
        self.assertEqual(manager.device.modifiers, ("shift",))
        self.assertEqual(manager.device.keys_down, {50})
        self.assertEqual(manager._modifier_holders, {owner: {"shift": {50}}})

        manager.do_process_keyboard_event(owner_proto, 0, "Shift_L", False, release)
        self.assertEqual(manager.device.modifiers, ())
        self.assertFalse(manager.device.keys_down)

    def test_late_lock_release_after_readonly_reenable_does_not_toggle(self):
        manager, server = manager_fixture()
        source = FakeSource("owner", 1, config(manager, ("us",)))
        accept(manager, server, source)
        proto = WaylandKeyboardPacketTest.bind(manager, server, source)
        event = {"modifiers": (), "keyval": CAPS_LOCK, "string": "", "keycode": 66, "group": 0}
        manager.do_process_keyboard_event(proto, 0, "Caps_Lock", True, event)
        self.assertEqual(manager.device.modifiers, ("lock",))

        server.readonly = True
        manager.setting_changed_handler(None, "readonly", manager.server.readonly)
        server.readonly = False
        manager.setting_changed_handler(None, "readonly", manager.server.readonly)
        self.assertEqual(manager.device.modifiers, ("lock",))

        manager.do_process_keyboard_event(
            proto, 0, "Caps_Lock", False, dict(event, modifiers=("lock",)),
        )
        self.assertEqual(source.keyboard_config.current_modifiers, ("lock",))
        self.assertEqual(manager.device.modifiers, ("lock",))
        self.assertFalse(manager.device.keys_down)

    def test_initially_readonly_release_cannot_resurrect_depressed_modifier(self):
        manager, server = manager_fixture()
        source = FakeSource("readonly", 1, config(manager, ("us",)), readonly=True)
        source.keyboard_config.current_modifiers = ("shift",)
        accept(manager, server, source)
        proto = WaylandKeyboardPacketTest.bind(manager, server, source)

        manager.do_process_keyboard_event(proto, 0, "Shift_L", False, {
            "modifiers": ("shift",), "keyval": SHIFT_L, "string": "", "keycode": 50, "group": 0,
        })
        self.assertEqual(source.keyboard_config.current_modifiers, ())
        self.assertEqual(manager.device.modifiers, ())

        source._readonly = False
        manager.setting_changed_handler(None, "readonly", source.effective_readonly(), source)
        self.assertIs(manager._keyboard_owner_source, source)
        self.assertEqual(manager.device.modifiers, ())

    def test_initially_readonly_lock_release_preserves_lock_state(self):
        manager, server = manager_fixture()
        source = FakeSource("readonly", 1, config(manager, ("us",)), readonly=True)
        source.keyboard_config.current_modifiers = ("lock",)
        accept(manager, server, source)
        proto = WaylandKeyboardPacketTest.bind(manager, server, source)

        manager.do_process_keyboard_event(proto, 0, "Caps_Lock", False, {
            "modifiers": ("lock",), "keyval": CAPS_LOCK, "string": "", "keycode": 66, "group": 0,
        })
        self.assertEqual(source.keyboard_config.current_modifiers, ("lock",))
        self.assertEqual(manager.device.modifiers, ())

        source._readonly = False
        manager.setting_changed_handler(None, "readonly", source.effective_readonly(), source)
        self.assertIs(manager._keyboard_owner_source, source)
        self.assertEqual(manager.device.modifiers, ("lock",))

    def test_sync_toggle_release_keeps_another_holder_and_timer(self):
        manager, server = manager_fixture()
        first = FakeSource("first", 1, config(manager, ("us",)))
        second = FakeSource("second", 2, config(manager, ("us",)))
        accept(manager, server, first)
        accept(manager, server, second)
        first_proto = WaylandKeyboardPacketTest.bind(manager, server, first)
        second_proto = WaylandKeyboardPacketTest.bind(manager, server, second)
        event = {"modifiers": (), "keyval": Q, "string": "q", "keycode": 24, "group": 0}
        manager.do_process_keyboard_event(first_proto, 0, "q", True, event)
        manager.do_process_keyboard_event(second_proto, 0, "q", True, event)
        self.assertEqual(len(manager._key_repeat_timers), 2)

        manager._process_sync(first_proto, Packet("keyboard-sync-enabled", False))
        manager.do_process_keyboard_event(first_proto, 0, "q", False, event)
        self.assertEqual(manager.device.keys_down, {24})
        self.assertEqual(set(manager._key_repeat_timers), {(second, 24)})
        manager.do_process_keyboard_event(second_proto, 0, "q", False, event)
        self.assertFalse(manager.device.keys_down)

    def test_repeat_timeout_and_source_departure_are_token_scoped(self):
        manager, server = manager_fixture()
        owner = FakeSource("owner", 0, config(manager, ("us",)))
        first = FakeSource("first", 1, config(manager, ("us",)))
        second = FakeSource("second", 2, config(manager, ("us",)))
        accept(manager, server, owner)
        accept(manager, server, first)
        accept(manager, server, second)
        protocols = [WaylandKeyboardPacketTest.bind(manager, server, source) for source in (first, second)]
        event = {"modifiers": (), "keyval": Q, "string": "q", "keycode": 24, "group": 0}
        for proto in protocols:
            manager.do_process_keyboard_event(proto, 0, "q", True, event)

        server.sources = [owner, second]
        manager.cleanup_protocol(object())
        self.assertEqual(set(manager._key_repeat_timers), {(second, 24)})
        self.assertEqual(manager.device.keys_down, {24})
        timer, _generation = manager._key_repeat_timers[(second, 24)]
        _delay, callback, args = server.timers.pop(timer)
        callback(*args)
        self.assertFalse(manager.device.keys_down)
        self.assertFalse(manager._key_repeat_timers)

    def test_modifier_state_is_post_event_and_aggregate(self):
        manager, server = manager_fixture()
        owner = FakeSource("owner", 1, config(manager, ("us",)))
        modifier_source = FakeSource("modifier", 2, config(manager, ("us",)))
        typing_source = FakeSource("typing", 3, config(manager, ("us",)))
        for source in (owner, modifier_source, typing_source):
            accept(manager, server, source)
        modifier_proto = WaylandKeyboardPacketTest.bind(manager, server, modifier_source)
        typing_proto = WaylandKeyboardPacketTest.bind(manager, server, typing_source)
        shift = {"modifiers": (), "keyval": SHIFT_L, "string": "", "keycode": 50, "group": 0}
        manager.do_process_keyboard_event(modifier_proto, 0, "Shift_L", True, shift)
        self.assertEqual(modifier_source.keyboard_config.current_modifiers, ("shift",))
        self.assertEqual(manager.device.modifiers, ("shift",))
        self.assertNotIn((modifier_source, 50), manager._key_repeat_timers)

        qevent = {"modifiers": (), "keyval": Q, "string": "q", "keycode": 24, "group": 0}
        manager.do_process_keyboard_event(typing_proto, 0, "q", True, qevent)
        self.assertEqual(typing_source.keyboard_config.current_modifiers, ())
        self.assertEqual(manager.device.modifiers, ("shift",))
        self.assertNotIn(24, manager.device.keys_down)
        self.assertEqual(manager.device.resolve_calls[-1][-2], ("shift",))

        server.sources = [owner, typing_source]
        manager.cleanup_protocol(object())
        self.assertEqual(manager.device.modifiers, ())
        self.assertFalse(manager.device.keys_down)
        manager.do_process_keyboard_event(typing_proto, 0, "q", False, qevent)

    def test_modifier_release_uses_press_time_meaning(self):
        manager, server = manager_fixture()
        source = FakeSource("owner", 1, config(manager, ("us",)))
        accept(manager, server, source)
        proto = WaylandKeyboardPacketTest.bind(manager, server, source)
        shift = {"modifiers": (), "keyval": SHIFT_L, "string": "", "keycode": 50, "group": 0}
        manager.do_process_keyboard_event(proto, 0, "Shift_L", True, shift)
        self.assertEqual(manager.device.modifiers, ("shift",))

        source.keyboard_config.modifier_meanings = {"Shift_L": "mod5"}
        shift["modifiers"] = ("shift",)
        manager.do_process_keyboard_event(proto, 0, "Shift_L", False, shift)
        self.assertEqual(manager.device.modifiers, ())
        self.assertFalse(manager._modifier_holders)
        self.assertEqual(source.keyboard_config.current_modifiers, ())

    def test_settled_nonowner_modifier_cannot_resurrect_on_promotion(self):
        manager, server = manager_fixture()
        owner = FakeSource("owner", 1, config(manager, ("us",)))
        fallback = FakeSource("fallback", 2, config(manager, ("us",)))
        accept(manager, server, owner)
        accept(manager, server, fallback)
        proto = WaylandKeyboardPacketTest.bind(manager, server, fallback)
        manager.do_process_keyboard_event(proto, 0, "Shift_L", True, {
            "modifiers": (), "keyval": SHIFT_L, "string": "", "keycode": 50, "group": 0,
        })
        self.assertEqual(fallback.keyboard_config.current_modifiers, ("shift",))

        fallback._readonly = True
        manager.setting_changed_handler(None, "readonly", fallback.effective_readonly(), fallback)
        self.assertEqual(fallback.keyboard_config.current_modifiers, ())
        fallback._readonly = False
        manager.setting_changed_handler(None, "readonly", fallback.effective_readonly(), fallback)

        server.sources = [fallback]
        manager.cleanup_protocol(object())
        self.assertIs(manager._keyboard_owner_source, fallback)
        self.assertEqual(manager.device.modifiers, ())

    def test_settled_nonowner_mask_cannot_resurrect_on_promotion(self):
        manager, server = manager_fixture()
        owner = FakeSource("owner", 1, config(manager, ("us",)))
        fallback = FakeSource("fallback", 2, config(manager, ("us",)))
        accept(manager, server, owner)
        accept(manager, server, fallback)
        fallback.keyboard_config.current_modifiers = ("Shift_L", "lock")
        manager.device.update_modifiers(("shift", "lock"), 0)
        self.assertFalse(manager._modifier_holders)

        fallback._readonly = True
        manager.setting_changed_handler(None, "readonly", fallback.effective_readonly(), fallback)
        self.assertEqual(fallback.keyboard_config.current_modifiers, ("lock",))
        fallback._readonly = False
        manager.setting_changed_handler(None, "readonly", fallback.effective_readonly(), fallback)

        server.sources = [fallback]
        manager.cleanup_protocol(object())
        self.assertIs(manager._keyboard_owner_source, fallback)
        self.assertEqual(manager.device.modifiers, ("lock",))

    def test_global_readonly_discards_depressed_mask_but_preserves_lock(self):
        manager, server = manager_fixture()
        owner = FakeSource("owner", 1, config(manager, ("us",)))
        accept(manager, server, owner)
        manager.update_keyboard_modifiers(("shift", "lock"), source=owner)

        server.readonly = True
        manager.setting_changed_handler(None, "readonly", manager.server.readonly)
        self.assertEqual(owner.keyboard_config.current_modifiers, ("lock",))
        self.assertEqual(manager.keyboard_owner, "")
        server.readonly = False
        manager.setting_changed_handler(None, "readonly", manager.server.readonly)

        self.assertIs(manager._keyboard_owner_source, owner)
        self.assertEqual(manager.device.modifiers, ("lock",))

    def test_owner_readonly_discards_depressed_mask_before_later_repromotion(self):
        manager, server = manager_fixture()
        owner = FakeSource("owner", 1, config(manager, ("us",)))
        fallback = FakeSource("fallback", 2, config(manager, ("fr",)))
        accept(manager, server, owner)
        accept(manager, server, fallback)
        manager.update_keyboard_modifiers(("shift", "lock"), source=owner)

        owner._readonly = True
        manager.setting_changed_handler(None, "readonly", owner.effective_readonly(), owner)
        self.assertIs(manager._keyboard_owner_source, fallback)
        self.assertEqual(owner.keyboard_config.current_modifiers, ("lock",))
        owner._readonly = False
        manager.setting_changed_handler(None, "readonly", owner.effective_readonly(), owner)
        server.sources = [owner]
        manager.cleanup_protocol(object())

        self.assertIs(manager._keyboard_owner_source, owner)
        self.assertEqual(manager.device.modifiers, ("lock",))

    def test_legacy_super_mapping_is_consistent_and_never_repeats(self):
        manager, server = manager_fixture()
        source = FakeSource("owner", 1, config(manager, ("us",)))
        accept(manager, server, source)
        proto = WaylandKeyboardPacketTest.bind(manager, server, source)
        event = {"modifiers": (), "keyval": SUPER_L, "string": "", "keycode": 133, "group": 0}

        manager.do_process_keyboard_event(proto, 0, "Super_L", True, event)
        self.assertEqual(source.keyboard_config.current_modifiers, ("mod3",))
        self.assertEqual(manager.device.modifiers, ("mod3",))
        self.assertNotIn((source, 133), manager._key_repeat_timers)
        event["modifiers"] = ("mod3",)
        manager.do_process_keyboard_event(proto, 0, "Super_L", False, event)
        self.assertEqual(manager.device.modifiers, ())

    def test_client_defined_modifier_is_classified_before_repeat_logic(self):
        manager, server = manager_fixture()
        source = FakeSource("owner", 1, config(manager, ("us",)))
        source.keyboard_config.modifier_meanings = {"ISO_Level5_Shift": "mod3"}
        accept(manager, server, source)
        proto = WaylandKeyboardPacketTest.bind(manager, server, source)
        event = {
            "modifiers": (), "keyval": LEVEL5_SHIFT, "string": "", "keycode": 94, "group": 0,
        }

        manager.do_process_keyboard_event(proto, 0, "ISO_Level5_Shift", True, event)
        self.assertEqual(manager.device.modifiers, ("mod3",))
        self.assertNotIn((source, 94), manager._key_repeat_timers)
        event["modifiers"] = ("mod3",)
        manager.do_process_keyboard_event(proto, 0, "ISO_Level5_Shift", False, event)
        self.assertEqual(manager.device.modifiers, ())
        self.assertFalse(manager._modifier_holders)

    def test_nonowner_same_map_metadata_refreshes_validated_rollback_snapshot(self):
        manager, server = manager_fixture()
        owner = FakeSource("owner", 1, config(manager, ("us",)))
        other = FakeSource("other", 2, config(manager, ("fr",)))
        accept(manager, server, owner)
        accept(manager, server, other)
        proto = WaylandKeyboardPacketTest.bind(manager, server, other)
        good = {
            "rmlvo-version": 1,
            "layouts": ("fr",),
            "variants": ("",),
            "layout_groups": True,
            "mod_meanings": {"ISO_Level5_Shift": "mod3"},
        }
        manager._process_config(proto, Packet("keyboard-config", good))
        self.assertEqual(other.keyboard_config.modifier_meanings, good["mod_meanings"])

        manager._process_config(proto, Packet("keyboard-config", {
            "rmlvo-version": 1,
            "layouts": ("missing",),
            "variants": ("",),
            "layout_groups": True,
        }))
        self.assertEqual(other.keyboard_config.rmlvo.layouts, ("fr",))
        self.assertEqual(other.keyboard_config.modifier_meanings, good["mod_meanings"])
        self.assertIn("rejected-configuration", manager.get_keyboard_info())

        manager._process_config(proto, Packet("keyboard-config", good))
        self.assertNotIn("rejected-configuration", manager.get_keyboard_info())

    def test_sync_change_before_release_cancels_source_repeat(self):
        manager, server = manager_fixture()
        source = FakeSource("owner", 1, config(manager, ("us",)))
        accept(manager, server, source)
        proto = WaylandKeyboardPacketTest.bind(manager, server, source)
        event = {"modifiers": (), "keyval": Q, "string": "q", "keycode": 24, "group": 0}
        manager.do_process_keyboard_event(proto, 0, "q", True, event)
        self.assertIn((source, 24), manager._key_repeat_timers)

        source.keyboard_config.sync = False
        manager.do_process_keyboard_event(proto, 0, "q", False, event)
        self.assertFalse(manager.device.keys_down)
        self.assertNotIn((source, 24), manager._key_repeat_timers)

    def test_shift_and_lock_release_reconcile_post_event_state(self):
        manager, server = manager_fixture()
        source = FakeSource("owner", 1, config(manager, ("us",)))
        accept(manager, server, source)
        proto = WaylandKeyboardPacketTest.bind(manager, server, source)
        shift = {"modifiers": (), "keyval": SHIFT_L, "string": "", "keycode": 50, "group": 0}
        manager.do_process_keyboard_event(proto, 0, "Shift_L", True, shift)
        shift["modifiers"] = ("shift",)
        manager.do_process_keyboard_event(proto, 0, "Shift_L", False, shift)
        self.assertEqual(source.keyboard_config.current_modifiers, ())
        self.assertEqual(manager.device.modifiers, ())

        caps = {"modifiers": (), "keyval": CAPS_LOCK, "string": "", "keycode": 66, "group": 0}
        manager.do_process_keyboard_event(proto, 0, "Caps_Lock", True, caps)
        self.assertEqual(manager.device.modifiers, ("lock",))
        caps["modifiers"] = ("lock",)
        manager.do_process_keyboard_event(proto, 0, "Caps_Lock", False, caps)
        self.assertEqual(manager.device.modifiers, ("lock",))
        manager.do_process_keyboard_event(proto, 0, "Caps_Lock", True, caps)
        self.assertEqual(manager.device.modifiers, ())
        caps["modifiers"] = ()
        manager.do_process_keyboard_event(proto, 0, "Caps_Lock", False, caps)
        self.assertEqual(source.keyboard_config.current_modifiers, ())

    def test_duplicate_lock_press_does_not_toggle_twice(self):
        manager, server = manager_fixture()
        source = FakeSource("owner", 1, config(manager, ("us",)))
        accept(manager, server, source)
        proto = WaylandKeyboardPacketTest.bind(manager, server, source)
        caps = {"modifiers": (), "keyval": CAPS_LOCK, "string": "", "keycode": 66, "group": 0}
        manager.do_process_keyboard_event(proto, 0, "Caps_Lock", True, caps)
        self.assertEqual(manager.device.modifiers, ("lock",))
        manager.do_process_keyboard_event(proto, 0, "Caps_Lock", True, caps)
        self.assertEqual(manager.device.modifiers, ("lock",))
        caps["modifiers"] = ("lock",)
        manager.do_process_keyboard_event(proto, 0, "Caps_Lock", False, caps)
        self.assertEqual(manager.device.modifiers, ("lock",))

    def test_invalid_window_modifier_press_has_no_side_effects(self):
        manager, server = manager_fixture()
        source = FakeSource("owner", 1, config(manager, ("us",)))
        accept(manager, server, source)
        proto = WaylandKeyboardPacketTest.bind(manager, server, source)
        window = type("Window", (), {"get_window": staticmethod(lambda _wid: None)})()
        server.subsystems["window"] = window
        manager.do_process_keyboard_event(proto, 99, "Shift_L", True, {
            "modifiers": (), "keyval": SHIFT_L, "string": "", "keycode": 50, "group": 0,
        })
        self.assertEqual(manager.device.modifiers, ())
        self.assertFalse(manager._modifier_holders)
        self.assertFalse(source.keyboard_config.pressed_translation)

    def test_owner_departure_resets_bootstrap_runtime_state(self):
        manager, server = manager_fixture()
        owner = FakeSource("owner", 1, config(manager, ("us", "fr")))
        accept(manager, server, owner)
        owner.keyboard_config.current_modifiers = ("lock", "mod2")
        owner.keyboard_config.current_group = 1
        manager.device.update_modifiers(("lock", "mod2"), 1)
        server.sources = []
        manager.cleanup_protocol(object())
        self.assertEqual(manager.device.layouts, ("us",))
        self.assertEqual(manager.device.group, 0)
        self.assertEqual(manager.device.modifiers, ())
        self.assertEqual(manager.device.repeat_calls[-1], (500, 30))

    def test_cleanup_releases_device(self):
        manager, server = manager_fixture()
        source = FakeSource("owner", 1, config(manager, ("us",)))
        accept(manager, server, source)
        proto = WaylandKeyboardPacketTest.bind(manager, server, source)
        manager.do_process_keyboard_event(proto, 0, "Shift_L", True, {
            "modifiers": (), "keyval": SHIFT_L, "string": "", "keycode": 50, "group": 0,
        })
        device = manager.device
        saved_owner_config = source.keyboard_config
        self.assertEqual(saved_owner_config.owner, "owner")
        manager.cleanup()
        self.assertTrue(device.cleaned)
        self.assertIsNone(manager.device)
        self.assertFalse(manager._settled_modifier_meanings)
        self.assertIsNone(saved_owner_config.owner)

    def test_cleanup_releases_device_when_settle_fails(self):
        class FailingSettleManager(WaylandKeyboardManager):
            def _settle_input(self, *, release_depressed: bool = False) -> None:
                del release_depressed
                raise RuntimeError("simulated settle failure")

        server = FakeServer()
        manager = FailingSettleManager(server)
        device = FakeDevice()
        manager.device = device
        timer = server.timeout_add(1000, lambda: None)
        manager._key_repeat_timers[(object(), 1)] = timer, object()

        with self.assertRaisesRegex(RuntimeError, "simulated settle failure"):
            manager.cleanup()

        self.assertIn(timer, server.removed_timers)
        self.assertNotIn(timer, server.timers)
        self.assertFalse(manager._key_repeat_timers)
        self.assertTrue(device.cleaned)
        self.assertIsNone(manager.device)
        self.assertIsNone(manager.bootstrap_config)
        self.assertIsNone(manager.effective_rmlvo)


class WaylandPointerModifierTest(unittest.TestCase):

    def test_non_ui_driver_pointer_cannot_change_shared_keyboard_modifiers(self):
        server = FakeServer()
        keyboard = Mock()
        server.subsystems = {"keyboard": keyboard}
        source = FakeSource("other", 1, None)
        proto = object()
        server.protocol_sources[proto] = source
        server.ui_driver = "owner"
        pointer = WaylandPointerManager(server)

        pointer._update_modifiers(proto, 0, ("shift",))
        keyboard.update_keyboard_modifiers.assert_not_called()

        server.ui_driver = "other"
        pointer._update_modifiers(proto, 0, ("shift",))
        keyboard.update_keyboard_modifiers.assert_called_once_with(("shift",), source=source)


class WaylandKeyboardPacketTest(unittest.TestCase):

    @staticmethod
    def bind(manager, server, source):
        proto = object()
        server.protocol_sources[proto] = source
        return proto

    def test_exact_rmlvo_capability_and_single_packet_replacement(self):
        manager, server = manager_fixture()
        source = FakeSource("owner", 1, config(manager, ("us",)))
        accept(manager, server, source)
        proto = self.bind(manager, server, source)
        self.assertEqual(manager.get_caps(source)["keyboard.rmlvo-version"], 1)

        event = {
            "modifiers": (), "keyval": Q, "string": "q",
            "keycode": 24, "group": 0,
        }
        manager.do_process_keyboard_event(proto, 0, "q", True, event)
        self.assertEqual(manager.device.keys_down, {24})
        installs = len(manager.device.install_calls)
        clears = len(manager.device.clear_calls)
        repeats = tuple(manager.device.repeat_calls)

        manager._process_config(proto, Packet("keyboard-config", {
            "rmlvo-version": 1,
            "rules": "evdev",
            "model": "pc104",
            "layouts": ("fr", "us"),
            "variants": ("oss", ""),
            "options": "grp:alt_shift_toggle",
            "layout_groups": True,
            "force": True,
        }))

        self.assertEqual(len(manager.device.install_calls), installs + 1)
        self.assertEqual(len(manager.device.clear_calls), clears + 1)
        self.assertEqual(tuple(manager.device.repeat_calls), repeats)
        self.assertEqual(manager.device.compile_calls[-1], (
            "evdev", "pc104", "fr,us", "oss,", "grp:alt_shift_toggle",
        ))
        self.assertEqual(source.keyboard_config.rmlvo.model, "pc104")
        self.assertEqual(source.keyboard_config.rmlvo.layouts, ("fr", "us"))
        self.assertEqual(source.keyboard_config.representation, "versioned")
        self.assertEqual(manager.last_apply_result, "installed")
        self.assertFalse(manager.device.keys_down)
        self.assertIn(24, source.keyboard_config.settled_translation)

        presses = tuple(manager.device.press_calls)
        manager.do_process_keyboard_event(proto, 0, "q", False, event)
        self.assertEqual(tuple(manager.device.press_calls), presses)
        self.assertNotIn(24, source.keyboard_config.settled_translation)

    @unittest.skipUnless(BACKWARDS_COMPATIBLE, "requires legacy keyboard packets")
    def test_delayed_backend_client_can_still_activate_with_layout_only(self):
        manager, server = manager_fixture()
        source = FakeSource("legacy-backend", 1, config(manager, ("us",)))
        source.keyboard_config.delay = True
        source.keyboard_config.repeat_delay = 750
        source.keyboard_config.repeat_interval = 40
        accept(manager, server, source)
        proto = self.bind(manager, server, source)
        self.assertEqual(manager.device.layouts, ("us",))
        self.assertIsNone(source.keyboard_config.owner)

        manager._process_layout_changed(
            proto, Packet("layout-changed", "fr", "oss", "", "ibus", "engine"),
        )

        self.assertFalse(source.keyboard_config.delay)
        self.assertEqual(source.keyboard_config.rmlvo.rules, "evdev")
        self.assertEqual(source.keyboard_config.rmlvo.model, "pc105")
        self.assertEqual(source.keyboard_config.rmlvo.layouts, ("fr",))
        self.assertEqual(source.keyboard_config.rmlvo.variants, ("oss",))
        self.assertEqual(manager.device.layouts, ("fr",))
        self.assertEqual(source.keyboard_config.owner, "legacy-backend")
        self.assertEqual(manager.device.repeat_calls[-1], (750, 40))

    def test_large_hello_mapping_is_projected_without_copy(self):
        class UnscannableTypedict(typedict):
            @staticmethod
            def fail(*_args, **_kwargs):
                raise AssertionError("hello or keymap mapping was scanned or copied")

            __iter__ = fail
            __len__ = fail
            copy = fail
            items = fail
            keys = fail
            values = fail

        manager, server = manager_fixture()
        source = FakeSource("large-hello", 1, None)
        server.sources = [source]
        keymap = UnscannableTypedict({
            "layout": "us,fr", "variant": ",oss", "layout_groups": True,
            "delay": {"": True},
        })
        hello_values = {f"unrelated-{index}": index for index in range(4096)}
        hello_values.update({"keyboard": {"": True}, "keymap": keymap})
        manager.parse_hello_ui_keyboard(source, UnscannableTypedict(hello_values))

        self.assertTrue(source.keyboard_config.enabled)
        self.assertTrue(source.keyboard_config.valid)
        self.assertTrue(source.keyboard_config.delay)
        self.assertEqual(source.keyboard_config.rmlvo.layouts, ("us", "fr"))
        self.assertEqual(source.keyboard_config.rmlvo.variants, ("", "oss"))

    def test_hello_modifiers_are_bounded_before_normalization(self):
        class IterationBomb(list):
            def __iter__(self):
                raise AssertionError("modifier sequence subclass was iterated")

        malformed = (
            "shift",
            ("shift", 1),
            ["shift"] * 100_000,
            IterationBomb(("shift",)),
        )
        for index, raw_modifiers in enumerate(malformed, 1):
            with self.subTest(index=index):
                manager, server = manager_fixture()
                source = FakeSource(f"bad-modifiers-{index}", index, None)
                server.sources = [source]
                manager.parse_hello_ui_keyboard(source, typedict({
                    "keyboard": True,
                    "keymap": {"layout": "us", "delay": True},
                    "modifiers": raw_modifiers,
                }))

                self.assertFalse(source.keyboard_config.valid)
                self.assertIn("malformed hello modifiers", source.keyboard_config.rejected)
                self.assertEqual(source.keyboard_config.current_modifiers, ())
                manager.add_new_client(source, typedict())
                self.assertFalse(manager._eligible(source))
                self.assertEqual(manager.keyboard_owner, "")
                self.assertEqual(manager.device.layouts, ("us",))
                rejected = manager.get_keyboard_info()["rejected-configuration"]
                self.assertEqual(rejected["client"], f"bad-modifiers-{index}")
                self.assertRegex(rejected["hash"], r"^[0-9a-f]{64}$")

    def test_malformed_outer_keyboard_properties_are_not_bootstrap_defaults(self):
        manager, _server = manager_fixture()

        config = manager.get_keyboard_config(("not", "a", "dictionary"))

        self.assertFalse(config.valid)
        self.assertIn("must be a dictionary", config.rejected)

    @unittest.skipUnless(BACKWARDS_COMPATIBLE, "requires legacy keyboard packets")
    def test_clean_legacy_nested_packet_is_accepted_after_delay(self):
        manager, server = manager_fixture()
        source = FakeSource("clean-client", 1, None)
        server.sources = [source]
        legacy = {
            "layout": "us,fr,ru",
            "layouts": ("us,fr,ru", "us", "fr", "ru"),
            "variant": ",,",
            "variants": (",,", "", "", ""),
            "options": "",
            "layout_groups": True,
            "query_struct": {
                "rules": "evdev", "model": "pc105", "layout": "us,fr,ru",
                "variant": ",,", "options": "",
            },
            "delay": True,
        }
        manager.parse_hello_ui_keyboard(source, typedict({
            "keyboard": True,
            "keymap": legacy,
            "key_repeat": (700, 50),
        }))
        manager.add_new_client(source, typedict())
        self.assertEqual(manager.device.layouts, ("us",))
        self.assertEqual(manager.keyboard_owner, "clean-client")
        self.assertIsNone(source.keyboard_config.owner)
        self.assertIsNone(manager.bootstrap_config.owner)
        proto = self.bind(manager, server, source)

        manager._process_keymap_changed(proto, Packet("keymap-changed", {"keymap": legacy}, True))
        self.assertEqual(manager.device.layouts, ("us", "fr", "ru"))
        self.assertEqual(manager.keyboard_owner, "clean-client")
        self.assertEqual(source.keyboard_config.owner, "clean-client")
        self.assertTrue(source.keyboard_config.valid)
        self.assertFalse(source.keyboard_config.rejected)
        self.assertNotIn("rejected-configuration", manager.get_keyboard_info())
        self.assertEqual(manager.last_apply_result, "installed")
        self.assertEqual(manager.device.repeat_calls[-1], (700, 50))

    def test_identical_recovery_clears_same_client_rejection(self):
        manager, server = manager_fixture()
        source = FakeSource("owner", 1, config(manager, ("us", "fr")))
        accept(manager, server, source)
        proto = self.bind(manager, server, source)
        manager._process_config(proto, Packet("keyboard-config", {
            "rmlvo-version": 1,
            "layouts": ("missing",),
            "variants": ("",),
            "layout_groups": True,
        }))
        self.assertIn("rejected-configuration", manager.get_keyboard_info())

        manager._process_config(proto, Packet("keyboard-config", {
            "rmlvo-version": 1,
            "layouts": ("us", "fr"),
            "variants": ("", ""),
            "layout_groups": True,
        }))
        self.assertEqual(manager.last_apply_result, "identical")
        self.assertNotIn("rejected-configuration", manager.get_keyboard_info())

    def test_anonymous_client_recovery_clears_its_rejection(self):
        manager, server = manager_fixture()
        source = FakeSource("", 7, config(manager, ("us", "fr")))
        accept(manager, server, source)
        proto = self.bind(manager, server, source)
        manager._process_config(proto, Packet("keyboard-config", {
            "rmlvo-version": 1,
            "layouts": ("../invalid",),
            "variants": ("",),
            "layout_groups": True,
        }))
        self.assertEqual(
            manager.get_keyboard_info()["rejected-configuration"]["client"],
            "client-7",
        )

        manager._process_config(proto, Packet("keyboard-config", {
            "rmlvo-version": 1,
            "layouts": ("us", "fr"),
            "variants": ("", ""),
            "layout_groups": True,
        }))
        self.assertEqual(manager.last_apply_result, "identical")
        self.assertNotIn("rejected-configuration", manager.get_keyboard_info())

    def test_readonly_structured_update_is_validated_for_later_promotion(self):
        manager, server = manager_fixture()
        owner = FakeSource("owner", 1, config(manager, ("us", "fr")))
        readonly = FakeSource("readonly", 2, config(manager, ("fr",)), readonly=True)
        accept(manager, server, owner)
        accept(manager, server, readonly)
        manager.update_keyboard_modifiers(("lock",), 1, owner)
        proto = self.bind(manager, server, readonly)
        active_state = (
            manager.keyboard_owner, manager.device.layouts, manager.device.group,
            manager.device.modifiers, tuple(manager.device.install_calls),
        )

        manager._process_config(proto, Packet("keyboard-config", {
            "rmlvo-version": 1,
            "layouts": ("ru",),
            "variants": ("",),
            "layout_groups": True,
            "modifiers": ("mod5",),
        }))

        self.assertEqual(
            (
                manager.keyboard_owner, manager.device.layouts, manager.device.group,
                manager.device.modifiers, tuple(manager.device.install_calls),
            ),
            active_state,
        )
        self.assertEqual(readonly.keyboard_config.rmlvo.layouts, ("ru",))
        self.assertEqual(readonly.keyboard_config.validated_hash, readonly.keyboard_config.get_hash())
        self.assertEqual(readonly.keyboard_config.compiled_groups, 1)
        self.assertFalse(readonly.keyboard_config.applied_hash)
        self.assertEqual(readonly.keyboard_config.current_modifiers, ("mod5",))

        readonly._readonly = False
        manager.setting_changed_handler(None, "readonly", readonly.effective_readonly(), readonly)
        self.assertIs(manager._keyboard_owner_source, owner)
        server.sources = [readonly]
        manager.cleanup_protocol(object())
        self.assertIs(manager._keyboard_owner_source, readonly)
        self.assertEqual(manager.keyboard_owner, "readonly")
        self.assertEqual(manager.device.layouts, ("ru",))
        self.assertEqual(manager.device.modifiers, ("mod5",))

    @unittest.skipUnless(BACKWARDS_COMPATIBLE, "requires legacy keyboard packets")
    def test_readonly_nested_keymap_update_is_validated_for_later_promotion(self):
        manager, server = manager_fixture()
        owner = FakeSource("owner", 1, config(manager, ("us",)))
        readonly = FakeSource("readonly", 2, config(manager, ("fr",)), readonly=True)
        accept(manager, server, owner)
        accept(manager, server, readonly)
        proto = self.bind(manager, server, readonly)
        installs = tuple(manager.device.install_calls)
        legacy = {
            "layout": "ru",
            "layouts": ("ru", "us"),
            "variant": "",
            "variants": ("", ""),
            "options": "",
            "layout_groups": True,
            "query_struct": {
                "rules": "evdev", "model": "pc105", "layout": "ru",
                "variant": "", "options": "",
            },
        }

        manager._process_keymap_changed(
            proto, Packet("keymap-changed", {"keymap": legacy}, True),
        )

        self.assertIs(manager._keyboard_owner_source, owner)
        self.assertEqual(manager.device.layouts, ("us",))
        self.assertEqual(tuple(manager.device.install_calls), installs)
        self.assertEqual(readonly.keyboard_config.rmlvo.layouts, ("ru",))
        self.assertEqual(readonly.keyboard_config.validated_hash, readonly.keyboard_config.get_hash())

        readonly._readonly = False
        manager.setting_changed_handler(None, "readonly", readonly.effective_readonly(), readonly)
        server.sources = [readonly]
        manager.cleanup_protocol(object())
        self.assertIs(manager._keyboard_owner_source, readonly)
        self.assertEqual(manager.device.layouts, ("ru",))

    def test_malformed_readonly_structured_update_preserves_last_good(self):
        manager, server = manager_fixture()
        owner = FakeSource("owner", 1, config(manager, ("us",)))
        readonly = FakeSource("readonly", 2, config(manager, ("fr",)), readonly=True)
        accept(manager, server, owner)
        accept(manager, server, readonly)
        proto = self.bind(manager, server, readonly)
        last_good = (
            readonly.keyboard_config.get_hash(), readonly.keyboard_config.validated_hash,
            readonly.keyboard_config.rmlvo, readonly.keyboard_config.compiled_groups,
        )
        active_state = (
            manager.keyboard_owner, manager.device.layouts,
            tuple(manager.device.install_calls), tuple(manager.device.repeat_calls),
        )

        manager._process_config(proto, Packet("keyboard-config", {"keymap": None}))

        self.assertEqual(
            (
                readonly.keyboard_config.get_hash(), readonly.keyboard_config.validated_hash,
                readonly.keyboard_config.rmlvo, readonly.keyboard_config.compiled_groups,
            ),
            last_good,
        )
        self.assertTrue(readonly.keyboard_config.valid)
        self.assertTrue(readonly.keyboard_config.rejected)
        self.assertEqual(
            (
                manager.keyboard_owner, manager.device.layouts,
                tuple(manager.device.install_calls), tuple(manager.device.repeat_calls),
            ),
            active_state,
        )
        self.assertEqual(
            manager.get_keyboard_info()["rejected-configuration"]["client"],
            "readonly",
        )

        readonly._readonly = False
        manager.setting_changed_handler(None, "readonly", readonly.effective_readonly(), readonly)
        server.sources = [readonly]
        manager.cleanup_protocol(object())
        self.assertIs(manager._keyboard_owner_source, readonly)
        self.assertEqual(manager.device.layouts, ("fr",))
        self.assertIn("rejected-configuration", manager.get_keyboard_info())

    @unittest.skipUnless(BACKWARDS_COMPATIBLE, "requires legacy keyboard packets")
    def test_readonly_legacy_layout_update_is_validated_for_later_promotion(self):
        manager, server = manager_fixture()
        owner = FakeSource("owner", 1, config(manager, ("us",)))
        readonly = FakeSource("readonly", 2, config(manager, ("fr",)), readonly=True)
        accept(manager, server, owner)
        accept(manager, server, readonly)
        proto = self.bind(manager, server, readonly)
        installs = tuple(manager.device.install_calls)

        manager._process_layout_changed(
            proto, Packet("layout-changed", "ru", "", ""),
        )

        self.assertIs(manager._keyboard_owner_source, owner)
        self.assertEqual(manager.device.layouts, ("us",))
        self.assertEqual(tuple(manager.device.install_calls), installs)
        self.assertEqual(readonly.keyboard_config.rmlvo.layouts, ("ru",))
        self.assertEqual(readonly.keyboard_config.validated_hash, readonly.keyboard_config.get_hash())
        self.assertEqual(readonly.keyboard_config.compiled_groups, 1)

        last_good = readonly.keyboard_config.get_hash()
        manager._process_layout_changed(proto, Packet("layout-changed"))
        self.assertEqual(readonly.keyboard_config.get_hash(), last_good)
        self.assertTrue(readonly.keyboard_config.rejected)
        self.assertEqual(manager.device.layouts, ("us",))
        self.assertEqual(tuple(manager.device.install_calls), installs)

        manager._process_layout_changed(
            proto, Packet("layout-changed", "../invalid", "", ""),
        )
        self.assertEqual(readonly.keyboard_config.get_hash(), last_good)
        self.assertTrue(readonly.keyboard_config.rejected)
        self.assertEqual(manager.device.layouts, ("us",))
        self.assertEqual(tuple(manager.device.install_calls), installs)

        readonly._readonly = False
        manager.setting_changed_handler(None, "readonly", readonly.effective_readonly(), readonly)
        server.sources = [readonly]
        manager.cleanup_protocol(object())
        self.assertIs(manager._keyboard_owner_source, readonly)
        self.assertEqual(manager.device.layouts, ("ru",))

    @unittest.skipUnless(BACKWARDS_COMPATIBLE, "requires legacy keyboard packets")
    def test_negative_legacy_group_is_bounded_before_event_processing(self):
        manager, server = manager_fixture()
        source = FakeSource("owner", 1, config(manager, ("us", "fr")))
        accept(manager, server, source)
        proto = self.bind(manager, server, source)
        manager._process_key_action(
            proto, Packet("key-action", 0, "q", True, (), Q, "q", 24, -7),
        )
        self.assertEqual(manager.device.group, 0)
        self.assertEqual(manager.device.keys_down, {24})
        manager._process_key_action(
            proto, Packet("key-action", 0, "q", False, (), Q, "q", 24, -7),
        )
        self.assertFalse(manager.device.keys_down)

    @unittest.skipUnless(BACKWARDS_COMPATIBLE, "requires legacy keyboard packets")
    def test_old_legacy_key_action_without_group_uses_group_zero(self):
        manager, server = manager_fixture()
        source = FakeSource("owner", 1, config(manager, ("us", "fr")))
        accept(manager, server, source)
        proto = self.bind(manager, server, source)
        manager._process_key_action(
            proto, Packet("key-action", 0, "q", True, (), Q, "q", 24),
        )
        self.assertEqual(manager.device.group, 0)
        self.assertEqual(manager.device.keys_down, {24})
        manager._process_key_action(
            proto, Packet("key-action", 0, "q", False, (), Q, "q", 24),
        )
        self.assertFalse(manager.device.keys_down)

    @unittest.skipUnless(BACKWARDS_COMPATIBLE, "requires legacy keyboard packets")
    def test_non_integer_legacy_groups_use_group_zero(self):
        for unsafe in (True, "1", 2**40):
            with self.subTest(group=unsafe):
                manager, server = manager_fixture()
                source = FakeSource("owner", 1, config(manager, ("us", "fr")))
                accept(manager, server, source)
                proto = self.bind(manager, server, source)
                manager._process_key_action(
                    proto, Packet("key-action", 0, "q", True, (), Q, "q", 24, unsafe),
                )
                self.assertEqual(manager.device.group, 0)
                self.assertEqual(manager.device.keys_down, {24})

    def test_current_event_group_requires_an_exact_integer(self):
        for raw_group, expected_group, expected_keycode, expected_preferences in (
                (True, 0, 24, (0, 1)),
                ("1", 0, 24, (0, 1)),
                (1.0, 0, 24, (0, 1)),
                ({"": 1}, 0, 24, (0, 1)),
                (1, 1, 38, (1, 0))):
            with self.subTest(group=raw_group):
                manager, server = manager_fixture()
                source = FakeSource("owner", 1, config(manager, ("us", "fr")))
                accept(manager, server, source)
                proto = self.bind(manager, server, source)
                event = {
                    "modifiers": (), "keyval": Q, "string": "q",
                    "keycode": 24, "group": raw_group,
                }

                manager._process_event(proto, Packet("keyboard-event", 0, "q", True, event))
                self.assertEqual(manager.device.group, expected_group)
                self.assertEqual(manager.device.keys_down, {expected_keycode})
                self.assertEqual(manager.device.resolve_calls[-1][2], expected_preferences)
                manager._process_event(proto, Packet("keyboard-event", 0, "q", False, event))
                self.assertFalse(manager.device.keys_down)

    @unittest.skipUnless(BACKWARDS_COMPATIBLE, "requires legacy keyboard packets")
    def test_signed_modifier_only_legacy_event_updates_altgr_without_a_key(self):
        manager, server = manager_fixture()
        source = FakeSource("owner", 1, config(manager, ("us", "de")))
        accept(manager, server, source)
        source.keyboard_config.current_group = 1
        manager.device.update_modifiers((), 1)
        proto = self.bind(manager, server, source)
        manager._process_key_action(
            proto, Packet("key-action", 0, "", True, ("mod5",), -1, "", -1, -1),
        )
        self.assertEqual(manager.device.modifiers, ("mod5",))
        self.assertEqual(manager.device.group, 1)
        self.assertEqual(source.keyboard_config.current_group, 1)
        self.assertFalse(manager.device.keys_down)

    def test_structured_explicit_modifiers_apply_without_resetting_group(self):
        manager, server = manager_fixture()
        source = FakeSource("owner", 1, config(manager, ("us", "fr")))
        accept(manager, server, source)
        manager.device.update_modifiers(("lock",), 1)
        proto = self.bind(manager, server, source)
        manager._process_config(proto, Packet("keyboard-config", {
            "rmlvo-version": 1,
            "layouts": ("us", "fr"),
            "variants": ("", ""),
            "layout_groups": True,
            "modifiers": ("mod5",),
        }))
        self.assertEqual(manager.device.group, 1)
        self.assertEqual(manager.device.modifiers, ("mod5",))
        self.assertEqual(source.keyboard_config.current_modifiers, ("mod5",))

    def test_delayed_identical_first_packet_is_still_installed(self):
        manager, server = manager_fixture()
        source = FakeSource("delayed", 1, config(manager, ("fr",)))
        source.keyboard_config.delay = True
        accept(manager, server, source)
        self.assertEqual(manager.device.layouts, ("us",))
        self.assertEqual(manager.keyboard_owner, "delayed")
        self.assertIsNone(source.keyboard_config.owner)
        self.assertIsNone(manager.bootstrap_config.owner)
        proto = self.bind(manager, server, source)
        manager._process_config(proto, Packet("keyboard-config", {
            "rmlvo-version": 1,
            "layouts": ("fr",),
            "variants": ("",),
            "layout_groups": True,
            "force": False,
        }))
        self.assertEqual(manager.device.layouts, ("fr",))
        self.assertEqual(source.keyboard_config.owner, "delayed")
        self.assertEqual(source.keyboard_config.applied_hash, source.keyboard_config.get_hash())

    def test_rejected_delayed_owner_settles_held_modifier_before_promotion(self):
        manager, server = manager_fixture()
        delayed = FakeSource("delayed", 1, config(manager, ("fr",)))
        delayed.keyboard_config.delay = True
        fallback = FakeSource("fallback", 2, config(manager, ("de",)))
        accept(manager, server, delayed)
        accept(manager, server, fallback)
        proto = self.bind(manager, server, delayed)
        event = {"modifiers": (), "keyval": SHIFT_L, "string": "", "keycode": 50, "group": 0}
        manager.do_process_keyboard_event(proto, 0, "Shift_L", True, event)
        self.assertEqual(manager.device.keys_down, {50})
        self.assertEqual(manager.device.modifiers, ("shift",))

        manager._process_config(proto, Packet("keyboard-config", {"keymap": None}))
        self.assertIs(manager._keyboard_owner_source, fallback)
        self.assertFalse(manager.device.keys_down)
        self.assertEqual(manager.device.modifiers, ())
        self.assertFalse(manager._key_holders)
        self.assertFalse(manager._modifier_holders)
        event["modifiers"] = ("shift",)
        manager.do_process_keyboard_event(proto, 0, "Shift_L", False, event)
        self.assertFalse(manager._settled_modifier_meanings)

    def test_rejected_delayed_owner_promotes_valid_fallback_once(self):
        manager, server = manager_fixture()
        delayed = FakeSource("delayed", 1, config(manager, ("fr",)))
        delayed.keyboard_config.delay = True
        fallback = FakeSource("fallback", 2, config(manager, ("de",)))
        accept(manager, server, delayed)
        accept(manager, server, fallback)
        proto = self.bind(manager, server, delayed)

        manager._process_config(proto, Packet("keyboard-config", {"keymap": None}))
        self.assertIs(manager._keyboard_owner_source, fallback)
        self.assertIsNone(delayed.keyboard_config.owner)
        self.assertEqual(fallback.keyboard_config.owner, "fallback")
        self.assertIsNone(manager.bootstrap_config.owner)
        self.assertEqual(manager.device.layouts, ("de",))
        self.assertFalse(delayed.keyboard_config.valid)
        self.assertIn("rejected-configuration", manager.get_keyboard_info())

        server.sources = [delayed]
        manager.cleanup_protocol(object())
        self.assertEqual(manager.keyboard_owner, "")
        self.assertIsNone(fallback.keyboard_config.owner)
        self.assertEqual(manager.bootstrap_config.owner, "")
        self.assertEqual(manager.device.layouts, ("us",))

    def test_malformed_structured_packets_are_bounded_and_non_mutating(self):
        cases = [
            ("keyboard-config", lambda manager, proto, packet: manager._process_config(proto, packet)),
        ]
        if BACKWARDS_COMPATIBLE:
            cases.append(
                ("keymap-changed", lambda manager, proto, packet: manager._process_keymap_changed(proto, packet)),
            )
        malformed = (
            lambda name: Packet(name),
            lambda name: Packet(name, "not-a-dictionary"),
        )
        for packet_type, handler in cases:
            for make_packet in malformed:
                with self.subTest(packet_type=packet_type, packet=make_packet):
                    manager, server = manager_fixture()
                    source = FakeSource("owner", 1, config(manager, ("us", "fr")))
                    accept(manager, server, source)
                    proto = self.bind(manager, server, source)
                    manager.update_keyboard_modifiers(("lock",), 1, source)
                    before = (
                        manager.config_hash, manager.device.layouts, manager.device.group,
                        manager.device.modifiers, tuple(manager.device.install_calls),
                        tuple(manager.device.repeat_calls),
                    )
                    handler(manager, proto, make_packet(packet_type))
                    after = (
                        manager.config_hash, manager.device.layouts, manager.device.group,
                        manager.device.modifiers, tuple(manager.device.install_calls),
                        tuple(manager.device.repeat_calls),
                    )
                    self.assertEqual(after, before)
                    rejected = manager.get_keyboard_info()["rejected-configuration"]
                    self.assertLessEqual(len(rejected["reason"]), 160)

    def test_malformed_python_objects_cannot_invoke_type_hooks(self):
        class EqualityBombMeta(type):
            def __eq__(cls, _other):
                raise AssertionError("untrusted metaclass equality hook was invoked")

        class EqualityBomb(metaclass=EqualityBombMeta):
            def __repr__(self):
                raise AssertionError("untrusted representation hook was invoked")

        bomb = EqualityBomb()
        manager, server = manager_fixture()
        source = FakeSource("owner", 1, config(manager, ("us", "fr")))
        accept(manager, server, source)
        proto = self.bind(manager, server, source)
        installs = tuple(manager.device.install_calls)

        # Call the boundary directly: Packet's constructor rejects arbitrary
        # Python objects before the server handler can exercise its fallback.
        manager._process_config(proto, ("keyboard-config", bomb))
        self.assertEqual(tuple(manager.device.install_calls), installs)
        self.assertIn("rejected-configuration", manager.get_keyboard_info())
        with self.assertRaisesRegex(ValueError, "bounded sequence"):
            manager._validate_modifiers(bomb)

        # Simulate an enabled debug logger which eagerly formats all arguments.
        # The raw object must never be one of them.
        with patch(
                "xpra.wayland.server.subsystem.keyboard.log",
                side_effect=lambda message, *args: message % args,
        ):
            rejected = manager.get_keyboard_config({"layout": bomb})
        self.assertFalse(rejected.valid)
        self.assertRegex(rejected.rejected_hash, r"^[0-9a-f]{64}$")

        peer = FakeSource("peer", 2, None)
        manager.parse_hello_ui_keyboard(peer, typedict({
            "keyboard": bomb,
            "keymap": {"layout": "us", "delay": bomb},
            "key_repeat": bomb,
        }))
        self.assertTrue(peer.keyboard_config.enabled)
        self.assertFalse(peer.keyboard_config.delay)
        self.assertEqual(
            (peer.keyboard_config.repeat_delay, peer.keyboard_config.repeat_interval),
            (500, 30),
        )

    def test_malformed_structured_modifiers_preserve_active_state(self):
        manager, server = manager_fixture()
        source = FakeSource("owner", 1, config(manager, ("us", "fr")))
        accept(manager, server, source)
        proto = self.bind(manager, server, source)
        manager.update_keyboard_modifiers(("lock",), 1, source)
        installs = tuple(manager.device.install_calls)
        for modifiers in ("shift", ("shift", 1), tuple("x" * 129 for _ in range(1))):
            with self.subTest(modifiers=modifiers):
                manager._process_config(proto, Packet("keyboard-config", {
                    "rmlvo-version": 1,
                    "layouts": ("us", "fr"),
                    "variants": ("", ""),
                    "layout_groups": True,
                    "modifiers": modifiers,
                }))
                self.assertEqual(manager.device.modifiers, ("lock",))
                self.assertEqual(manager.device.group, 1)
                self.assertEqual(tuple(manager.device.install_calls), installs)
                self.assertIn("rejected-configuration", manager.get_keyboard_info())

    def test_invalid_and_disabled_repeat_capabilities_are_bounded(self):
        manager, _server = manager_fixture()
        for index, raw_repeat in enumerate(((2 ** 40, 1), (True, 30), ("500", 30)), 1):
            with self.subTest(raw_repeat=raw_repeat):
                invalid = FakeSource(f"invalid-repeat-{index}", index, None)
                manager.parse_hello_ui_keyboard(invalid, typedict({
                    "keyboard": True, "keymap": {"layout": "us", "delay": True},
                    "key_repeat": raw_repeat,
                }))
                self.assertEqual(
                    (invalid.keyboard_config.repeat_delay, invalid.keyboard_config.repeat_interval),
                    (500, 30),
                )
        disabled = FakeSource("disabled-repeat", 2, None)
        manager.parse_hello_ui_keyboard(disabled, typedict({
            "keyboard": True, "keymap": {"layout": "us", "delay": True},
            "key_repeat": (0, 30),
        }))
        self.assertEqual((disabled.keyboard_config.repeat_delay, disabled.keyboard_config.repeat_interval), (0, 0))

    def test_unbounded_numeric_keysym_falls_back_to_portable_string(self):
        manager, server = manager_fixture()
        source = FakeSource("owner", 1, config(manager, ("us",)))
        accept(manager, server, source)
        proto = self.bind(manager, server, source)
        for keyval in (2 ** 100, -2 ** 100):
            with self.subTest(keyval=keyval):
                event = {
                    "modifiers": (), "keyval": keyval, "string": "q", "keycode": 24, "group": 0,
                }
                manager.do_process_keyboard_event(proto, 0, "q", True, event)
                self.assertEqual(manager.device.keys_down, {24})
                self.assertEqual(manager.device.resolve_calls[-1][0], 0)
                manager.do_process_keyboard_event(proto, 0, "q", False, event)
                self.assertFalse(manager.device.keys_down)

    def test_recording_readonly_and_stale_events_have_no_side_effects(self):
        for name, source_kwargs, remove in (
                ("recording", {"record": True}, False),
                ("readonly", {"readonly": True}, False),
                ("stale", {}, True)):
            with self.subTest(name=name):
                manager, server = manager_fixture()
                source = FakeSource(name, 1, config(manager, ("us",)), **source_kwargs)
                accept(manager, server, source)
                proto = self.bind(manager, server, source)
                if remove:
                    server.sources = []
                manager.do_process_keyboard_event(proto, 0, "q", True, {
                    "modifiers": (), "keyval": Q, "string": "q", "keycode": 24, "group": 0,
                })
                self.assertEqual(server.ui_driver_calls, [])
                self.assertEqual(source.user_events, [])
                self.assertFalse(manager.keys_pressed)
                self.assertFalse(manager.device.keys_down)

    def test_invalid_caps_do_not_advertise_unapplied_repeat(self):
        manager, server = manager_fixture()
        source = FakeSource("invalid", 1, config(manager, ("us",)))
        source.keyboard_config.repeat_delay = 900
        source.keyboard_config.repeat_interval = 90
        source.keyboard_config.parse({"layout": "../bad"})
        server.sources = [source]
        self.assertEqual(manager.get_caps(source)["key_repeat"], (-1, -1))

    def test_sync_toggle_survives_rejected_keymap_update(self):
        manager, server = manager_fixture()
        source = FakeSource("owner", 1, config(manager, ("us",)))
        accept(manager, server, source)
        proto = self.bind(manager, server, source)

        manager._process_sync(proto, Packet("keyboard-sync", False))
        self.assertFalse(source.keyboard_config.sync)
        manager._process_config(proto, Packet("keyboard-config", {
            "rmlvo-version": 1,
            "layouts": ("../invalid",),
            "variants": ("",),
            "layout_groups": True,
        }))

        self.assertFalse(source.keyboard_config.sync)
        self.assertIn("rejected-configuration", manager.get_keyboard_info())

    def test_uncompiled_caps_advertise_only_the_active_repeat_contract(self):
        manager, server = manager_fixture()
        manager.key_repeat_delay, manager.key_repeat_interval = 500, 30
        source = FakeSource("uncompiled", 1, config(manager, ("missing",)))
        source.keyboard_config.repeat_delay = 900
        source.keyboard_config.repeat_interval = 90
        server.sources = [source]
        self.assertEqual(manager.get_caps(source)["key_repeat"], (500, 30))


def main():
    unittest.main()


if __name__ == "__main__":
    main()
