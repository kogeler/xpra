#!/usr/bin/env python3
# Copyright (C) 2025 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

from collections.abc import Sequence
from time import monotonic

from xpra.log import Logger

from libc.stdlib cimport free, calloc
from libc.string cimport memset
from libc.stdint cimport uintptr_t, uint32_t, int32_t
from xpra.wayland.server.wlroots cimport (
    wlr_seat, wlr_surface, wlr_xdg_surface, wlr_keyboard, wlr_keyboard_impl, wlr_keyboard_init, wlr_keyboard_finish,
    wlr_seat_set_keyboard, wlr_seat_get_keyboard, wlr_seat_keyboard_notify_key, wlr_seat_keyboard_notify_modifiers,
    wlr_seat_keyboard_notify_enter, wlr_seat_keyboard_notify_clear_focus,
    wlr_keyboard_set_repeat_info, wlr_keyboard_notify_key, wlr_keyboard_notify_modifiers,
    wlr_keyboard_key_event,
    wl_keyboard_key_state,
    WL_KEYBOARD_KEY_STATE_PRESSED, WL_KEYBOARD_KEY_STATE_RELEASED,
    xkb_context, xkb_context_new, xkb_context_unref,
    xkb_keymap, xkb_keymap_unref, wlr_keyboard_set_keymap, xkb_rule_names, xkb_keymap_new_from_names,
    xkb_state, xkb_state_new, xkb_state_unref, xkb_state_update_mask, xkb_state_key_get_syms,
    XKB_CONTEXT_NO_FLAGS, XKB_KEYMAP_COMPILE_NO_FLAGS, XKB_KEYSYM_NO_FLAGS, XKB_KEYSYM_CASE_INSENSITIVE,
    xkb_keycode_t, xkb_keysym_t, xkb_layout_index_t, xkb_level_index_t, xkb_mod_index_t,
    xkb_keymap_min_keycode, xkb_keymap_max_keycode, xkb_keymap_num_layouts,
    xkb_keymap_mod_get_index,
    xkb_keymap_num_levels_for_key, xkb_keymap_key_get_syms_by_level,
    xkb_keysym_from_name, xkb_utf32_to_keysym,
)

cdef extern from *:
    """
    #include <stdarg.h>
    #include <xkbcommon/xkbcommon.h>
    #include <wlr/types/wlr_keyboard.h>

    static unsigned int xpra_wlr_keyboard_keys_cap(void) {
        return WLR_KEYBOARD_KEYS_CAP;
    }

    static void xpra_xkb_log_error(struct xkb_context *context,
            enum xkb_log_level level, const char *format, va_list args) {
        (void) format;
        (void) args;
        int *error = xkb_context_get_user_data(context);
        if (error != NULL && level <= XKB_LOG_LEVEL_ERROR) {
            *error = 1;
        }
    }

    static void xpra_xkb_reject_logged_errors(struct xkb_context *context, int *error) {
        xkb_context_set_user_data(context, error);
        xkb_context_set_log_level(context, XKB_LOG_LEVEL_ERROR);
        xkb_context_set_log_fn(context, xpra_xkb_log_error);
    }

    static void xpra_xkb_stop_reject_logged_errors(struct xkb_context *context) {
        /* A keymap retains its context.  Never leave the address of the
         * compile_keymap stack flag reachable through that retained object. */
        xkb_context_set_user_data(context, NULL);
        xkb_context_set_log_fn(context, NULL);
    }
    """
    void xpra_xkb_reject_logged_errors(xkb_context *context, int *error) noexcept
    void xpra_xkb_stop_reject_logged_errors(xkb_context *context) noexcept
    unsigned int xpra_wlr_keyboard_keys_cap() noexcept

log = Logger("wayland", "keyboard")

base_time = monotonic()

MOD_INDEX = {
    "shift": 0,
    "lock": 1,
    "control": 2,
    "mod1": 3,
    "mod2": 4,
    "mod3": 5,
    "mod4": 6,
    "mod5": 7,
}
LOCKED_MODIFIERS = frozenset(("lock", "mod2"))
XKB_MOD_NAMES = {
    "shift": b"Shift",
    "lock": b"Lock",
    "control": b"Control",
    "mod1": b"Mod1",
    "mod2": b"Mod2",
    "mod3": b"Mod3",
    "mod4": b"Mod4",
    "mod5": b"Mod5",
}


cdef inline uint32_t get_time_msec() noexcept:
    return round((monotonic() - base_time) * 1000)


cdef inline bytes b(s: str):
    if not s:
        return b""
    return s.encode("ascii")


cdef tuple event_keysyms(object keysym, str name, str keystr):
    cdef xkb_keysym_t sym = 0
    cdef xkb_keysym_t name_sym = 0
    cdef list values = []
    cdef uint32_t codepoint
    if name:
        try:
            bname = name.encode("ascii")
        except UnicodeEncodeError:
            bname = b""
        if bname:
            name_sym = xkb_keysym_from_name(bname, XKB_KEYSYM_NO_FLAGS)
            if name_sym == 0:
                name_sym = xkb_keysym_from_name(bname, XKB_KEYSYM_CASE_INSENSITIVE)
    # Keypad names carry physical-key semantics which their printable string
    # does not: KP_1 with text "1" must retain NumLock behaviour.  Other
    # backends may send a base/physical name with the real Unicode character
    # only in keystr, so they keep the portable string priority.
    if name.startswith("KP_") and name_sym:
        values.append(<unsigned int> name_sym)
    if len(keystr) == 1:
        codepoint = ord(keystr)
        if codepoint >= 0x20 and codepoint != 0x7f:
            sym = xkb_utf32_to_keysym(codepoint)
            if sym and <unsigned int> sym not in values:
                values.append(<unsigned int> sym)
    if name_sym and <unsigned int> name_sym not in values:
        values.append(<unsigned int> name_sym)
    if (type(keysym) is int and 0 < keysym <= 0xffffffff
            and <unsigned int> keysym not in values):
        # Numeric keyval is an XKB keysym for GTK/X11, but a scan code, Qt
        # enum or Unicode value for other clients.  It is only a final fallback
        # after the ordered name/string candidates.
        values.append(<unsigned int> keysym)
    return tuple(values)


cdef tuple modifier_trials(modifiers, fixed_modifiers):
    cdef list original_modifiers = list(dict.fromkeys(modifiers or ()))
    cdef frozenset fixed = frozenset(fixed_modifiers or ())
    cdef list trial_modifiers
    cdef list trials = []
    cdef tuple toggle
    cdef tuple trial
    # X11 clients report canonical effective modifiers.  Win32 and some
    # lightweight clients may omit the level selector and rely on the actual
    # string.  Try only the bounded XKB level-affecting toggles.
    for toggle in (
            (), ("mod5",), ("shift",), ("mod5", "shift"),
            ("mod2",), ("mod2", "mod5"), ("mod2", "shift"),
            ("mod2", "mod5", "shift")):
        trial_modifiers = list(original_modifiers)
        for modifier in toggle:
            # A modifier backed by a currently held physical key belongs to
            # the shared seat, not just to this packet.  Never infer a symbol
            # by toggling it away while another key remains down.
            if modifier in fixed:
                continue
            if modifier in trial_modifiers:
                trial_modifiers.remove(modifier)
            else:
                trial_modifiers.append(modifier)
        trial = tuple(trial_modifiers)
        if trial not in trials:
            trials.append(trial)
    return tuple(trials)


cdef tuple keymap_modifier_masks(xkb_keymap *keymap, modifiers):
    cdef xkb_mod_index_t index
    cdef uint32_t depressed = 0
    cdef uint32_t locked = 0
    cdef uint32_t bit
    cdef str modifier
    for modifier in modifiers or ():
        mod_name = XKB_MOD_NAMES.get(modifier)
        if not mod_name:
            continue
        index = xkb_keymap_mod_get_index(keymap, mod_name)
        if index == <uint32_t> -1 or index >= 32:
            continue
        bit = <uint32_t> 1 << index
        if modifier in LOCKED_MODIFIERS:
            locked |= bit
        else:
            depressed |= bit
    return depressed, locked


cdef tuple resolve_keycode_candidates(
        xkb_keymap *keymap, dict keysym_to_keycodes, unsigned int group_count,
        tuple symbols, tuple groups, tuple masked_trials, modifiers):
    cdef xkb_state *state = NULL
    cdef const xkb_keysym_t *effective_syms
    cdef xkb_keycode_t keycode
    cdef xkb_keysym_t sym
    cdef uint32_t depressed, locked
    cdef int n_syms, i, group
    cdef tuple trial
    if keymap == NULL or not symbols:
        return -1, 0
    state = xkb_state_new(keymap)
    if state == NULL:
        return -1, 0
    try:
        # Candidate priority is stronger than group/modifier preference.  The
        # Unicode string must exhaust every ordered group and bounded modifier
        # trial before a lower-priority physical name or numeric fallback can
        # win without the required Shift / Level3 selector.
        for symbol_value in symbols:
            sym = <xkb_keysym_t> symbol_value
            for group_value in groups:
                group = int(group_value)
                if group < 0 or group >= group_count:
                    continue
                for trial, depressed, locked in masked_trials:
                    xkb_state_update_mask(state, depressed, 0, locked, 0, 0, group)
                    for keycode_value in keysym_to_keycodes.get((group, <unsigned int> sym), ()):
                        keycode = <xkb_keycode_t> keycode_value
                        n_syms = xkb_state_key_get_syms(state, keycode, &effective_syms)
                        for i in range(n_syms):
                            if effective_syms[i] == sym:
                                if isinstance(modifiers, list):
                                    modifiers[:] = trial
                                return <int> keycode, group
        return -1, 0
    finally:
        xkb_state_unref(state)


cdef void virtual_keyboard_led_update(wlr_keyboard *wlr_kb, uint32_t leds) noexcept:
    """Called when LED state changes (Caps Lock, Num Lock, etc.)"""
    log.info("led-update: %#x", leds)


cdef class KeymapCandidate:
    cdef xkb_keymap *keymap
    cdef dict keysym_to_keycodes
    cdef public unsigned int group_count
    cdef public object rmlvo

    def __cinit__(self):
        self.keymap = NULL
        self.keysym_to_keycodes = {}
        self.group_count = 0
        self.rmlvo = None

    cdef void close(self) noexcept:
        if self.keymap != NULL:
            xkb_keymap_unref(self.keymap)
            self.keymap = NULL

    def cleanup(self) -> None:
        self.close()

    def get_keycodes_for_keyname(self, name: str, group: int) -> tuple[int, ...]:
        cdef xkb_keysym_t sym
        if not name or group < 0 or group >= self.group_count:
            return ()
        try:
            bname = name.encode("ascii")
        except UnicodeEncodeError:
            return ()
        sym = xkb_keysym_from_name(bname, XKB_KEYSYM_NO_FLAGS)
        if sym == 0:
            sym = xkb_keysym_from_name(bname, XKB_KEYSYM_CASE_INSENSITIVE)
        return self.keysym_to_keycodes.get((group, <unsigned int> sym), ())

    def resolve_keycode(self, name: str, group: int, modifiers=()) -> int:
        cdef xkb_keysym_t sym
        cdef xkb_state *state = NULL
        cdef xkb_keycode_t keycode
        cdef const xkb_keysym_t *effective_syms
        cdef uint32_t depressed = 0
        cdef uint32_t locked = 0
        cdef int n_syms, i
        if self.keymap == NULL or not name or group < 0 or group >= self.group_count:
            return -1
        try:
            bname = name.encode("ascii")
        except UnicodeEncodeError:
            return -1
        sym = xkb_keysym_from_name(bname, XKB_KEYSYM_NO_FLAGS)
        if sym == 0:
            sym = xkb_keysym_from_name(bname, XKB_KEYSYM_CASE_INSENSITIVE)
        if sym == 0:
            return -1
        depressed, locked = keymap_modifier_masks(self.keymap, modifiers)
        state = xkb_state_new(self.keymap)
        if state == NULL:
            return -1
        try:
            xkb_state_update_mask(state, depressed, 0, locked, 0, 0, group)
            for keycode_value in self.keysym_to_keycodes.get((group, <unsigned int> sym), ()):
                keycode = <xkb_keycode_t> keycode_value
                n_syms = xkb_state_key_get_syms(state, keycode, &effective_syms)
                for i in range(n_syms):
                    if effective_syms[i] == sym:
                        return <int> keycode
            return -1
        finally:
            xkb_state_unref(state)

    def resolve_event_keycode(self, keysym: int, name: str, groups, modifiers=(), keystr: str = "",
                              fixed_modifiers=()) -> tuple[int, int]:
        """Resolve an event against this compiled candidate without installing it."""
        cdef tuple symbols = event_keysyms(keysym, name, keystr)
        cdef tuple trials = modifier_trials(modifiers, fixed_modifiers)
        cdef tuple trial
        cdef uint32_t depressed, locked
        cdef list masked_trials = []
        if self.keymap == NULL or not symbols:
            return -1, 0
        for trial in trials:
            depressed, locked = keymap_modifier_masks(self.keymap, trial)
            masked_trials.append((trial, depressed, locked))
        return resolve_keycode_candidates(
            self.keymap, self.keysym_to_keycodes, self.group_count,
            symbols, tuple(groups), tuple(masked_trials), modifiers,
        )

    def __dealloc__(self):
        self.close()


cdef class WaylandKeyboard:
    cdef wlr_seat *seat
    cdef wlr_keyboard *keyboard
    cdef wlr_keyboard_impl keyboard_impl
    cdef object modifiers
    cdef object rmlvo
    cdef int group
    cdef unsigned int group_count
    cdef dict keysym_to_keycodes
    cdef set keys_down
    cdef bint keyboard_initialized
    cdef bint keyboard_attached

    def __cinit__(self):
        self.keyboard = NULL
        self.keyboard_initialized = False
        self.keyboard_attached = False

    def __init__(self, uintptr_t seat_ptr):
        self.seat = <wlr_seat*>seat_ptr
        self.modifiers = ()
        self.rmlvo = None
        self.group = 0
        self.group_count = 0
        self.keysym_to_keycodes = {}
        self.keys_down = set()
        if not seat_ptr:
            raise ValueError("seat pointer is NULL")
        self.keyboard = <wlr_keyboard*> calloc(1, sizeof(wlr_keyboard))
        if not self.keyboard:
            raise MemoryError("failed to allocate keyboard")
        log("wlr_keyboard=%#x", <uintptr_t> self.keyboard)
        self.keyboard_impl.name = b"xpra-virtual-keyboard"
        self.keyboard_impl.led_update = virtual_keyboard_led_update
        wlr_keyboard_init(self.keyboard, &self.keyboard_impl, b"virtual-keyboard")
        self.keyboard_initialized = True
        try:
            if not self.set_layout():
                raise RuntimeError("failed to compile the default keymap")
            # set a default repeat rate:
            wlr_keyboard_set_repeat_info(self.keyboard, 25, 600)
            wlr_seat_set_keyboard(self.seat, self.keyboard)
            self.keyboard_attached = True
        except Exception:
            self.close()
            raise

    def __repr__(self):
        return "WaylandKeyboard(%#x)" % (<uintptr_t> self.seat)

    cdef void close(self) noexcept:
        if self.keyboard:
            if self.keyboard_attached and self.seat != NULL and wlr_seat_get_keyboard(self.seat) == self.keyboard:
                wlr_seat_set_keyboard(self.seat, NULL)
            self.keyboard_attached = False
            if self.keyboard_initialized:
                wlr_keyboard_finish(self.keyboard)
                self.keyboard_initialized = False
            free(self.keyboard)
            self.keyboard = NULL

    def cleanup(self) -> None:
        if self.keyboard:
            try:
                self.clear_keys_pressed(tuple(self.keys_down))
            finally:
                self.close()
        self.keys_down.clear()

    def __dealloc__(self):
        self.close()

    @staticmethod
    def compile_keymap(rules: str, model: str, layout: str,
                       variant: str, options: str):
        cdef xkb_context *context = NULL
        cdef xkb_keymap *keymap = NULL
        cdef xkb_rule_names names
        cdef xkb_keycode_t min_kc, max_kc, kc
        cdef xkb_layout_index_t group, n_groups
        cdef xkb_level_index_t level, n_levels
        cdef const xkb_keysym_t *syms
        cdef int n_syms, i, xkb_error = 0
        cdef dict mapping = {}
        cdef list keycodes
        cdef KeymapCandidate candidate
        brules = b(rules)
        bmodel = b(model)
        blayout = b(layout)
        bvariant = b(variant)
        boptions = b(options)
        try:
            context = xkb_context_new(XKB_CONTEXT_NO_FLAGS)
            if context == NULL:
                raise RuntimeError("failed to create an XKB context")
            # libxkbcommon may return a keymap after logging a hard error for
            # an ignored RMLVO component (notably an unknown option).  Such a
            # partial result is not the exact client configuration and must
            # not replace the active map.  Model matching remains entirely
            # rules-driven: a ruleset wildcard may intentionally accept an
            # otherwise unlisted model without logging an error.
            xpra_xkb_reject_logged_errors(context, &xkb_error)
            memset(&names, 0, sizeof(xkb_rule_names))
            if brules:
                names.rules = brules
            if bmodel:
                names.model = bmodel
            names.layout = blayout
            names.variant = bvariant
            names.options = boptions
            keymap = xkb_keymap_new_from_names(context, &names, XKB_KEYMAP_COMPILE_NO_FLAGS)
            xpra_xkb_stop_reject_logged_errors(context)
            if keymap == NULL or xkb_error:
                raise ValueError("libxkbcommon rejected the RMLVO configuration")
            min_kc = xkb_keymap_min_keycode(keymap)
            max_kc = xkb_keymap_max_keycode(keymap)
            n_groups = xkb_keymap_num_layouts(keymap)
            for kc in range(min_kc, max_kc + 1):
                # xkbcommon wraps a key's shorter per-key layout list across
                # all global groups.  Index every effective global group so
                # Return, Space, modifiers and navigation keys remain usable
                # outside group zero.
                for group in range(n_groups):
                    n_levels = xkb_keymap_num_levels_for_key(keymap, kc, group)
                    for level in range(n_levels):
                        n_syms = xkb_keymap_key_get_syms_by_level(keymap, kc, group, level, &syms)
                        for i in range(n_syms):
                            keycodes = mapping.setdefault((<unsigned int> group, <unsigned int> syms[i]), [])
                            if <unsigned int> kc not in keycodes:
                                keycodes.append(<unsigned int> kc)
            candidate = KeymapCandidate()
            candidate.keymap = keymap
            keymap = NULL
            candidate.keysym_to_keycodes = {
                key: tuple(value) for key, value in mapping.items()
            }
            candidate.group_count = <unsigned int> n_groups
            candidate.rmlvo = {
                "rules": rules,
                "model": model,
                "layout": layout,
                "variant": variant,
                "options": options,
            }
            log("compiled XKB keymap with %i groups and %i group-aware symbols (keycodes %i..%i)",
                n_groups, len(mapping), min_kc, max_kc)
            return candidate
        finally:
            if keymap != NULL:
                xkb_keymap_unref(keymap)
            if context != NULL:
                xpra_xkb_stop_reject_logged_errors(context)
                xkb_context_unref(context)

    def install_keymap(self, KeymapCandidate candidate, settle_keycodes=(), modifiers=(), group: int = 0) -> int:
        cdef wlr_keyboard *replacement = NULL
        cdef wlr_keyboard *old_keyboard = NULL
        cdef bint replacement_initialized = False
        cdef tuple keycodes
        cdef tuple normalized_modifiers
        cdef uint32_t depressed = 0
        cdef uint32_t locked = 0
        if self.keyboard == NULL:
            raise RuntimeError("keyboard is not available")
        if candidate is None or candidate.keymap == NULL or not candidate.group_count:
            raise ValueError("invalid XKB keymap candidate")
        # The native device owns every held key, including a direct public
        # set_layout caller with no manager-provided settlement list.
        keycodes = tuple(dict.fromkeys((*(settle_keycodes or ()), *self.keys_down)))
        normalized_modifiers = tuple(x for x in (modifiers or ()) if x)
        if group < 0 or group >= candidate.group_count:
            group = 0

        if self.keyboard_attached:
            # Prepare a complete detached wlroots keyboard first.  Only after
            # set_keymap has succeeded can we safely release held keys from the
            # active device and swap the seat pointer without a rollback pair.
            replacement = <wlr_keyboard*> calloc(1, sizeof(wlr_keyboard))
            if replacement == NULL:
                raise MemoryError("failed to allocate replacement keyboard")
            try:
                wlr_keyboard_init(replacement, &self.keyboard_impl, b"virtual-keyboard")
                replacement_initialized = True
                if not wlr_keyboard_set_keymap(replacement, candidate.keymap):
                    raise RuntimeError("wlroots failed to prepare the XKB keymap")
                wlr_keyboard_set_repeat_info(
                    replacement, self.keyboard.repeat_info.rate, self.keyboard.repeat_info.delay,
                )
                depressed, locked = self.keyboard_modifier_masks(replacement, normalized_modifiers)
                wlr_keyboard_notify_modifiers(replacement, depressed, 0, locked, group)

                # Everything which can reject the replacement has completed.
                # Settle the active keyboard before the one-way seat swap.
                self.clear_keys_pressed(keycodes)
                old_keyboard = self.keyboard
                wlr_seat_set_keyboard(self.seat, replacement)
                self.keyboard = replacement
                replacement = NULL
                self.keyboard_attached = True
                wlr_keyboard_finish(old_keyboard)
                free(old_keyboard)
            finally:
                if replacement != NULL:
                    if replacement_initialized:
                        wlr_keyboard_finish(replacement)
                    free(replacement)
        else:
            if not wlr_keyboard_set_keymap(self.keyboard, candidate.keymap):
                raise RuntimeError("wlroots failed to install the XKB keymap")
            depressed, locked = self.keyboard_modifier_masks(self.keyboard, normalized_modifiers)
            wlr_keyboard_notify_modifiers(self.keyboard, depressed, 0, locked, group)
        self.keysym_to_keycodes = candidate.keysym_to_keycodes
        self.group_count = candidate.group_count
        self.rmlvo = candidate.rmlvo
        self.modifiers = normalized_modifiers
        self.group = group
        self.keys_down.clear()
        candidate.close()
        return self.group_count

    def set_layout(self, layout="us", model="pc105", variant="", options="", rules="evdev") -> bool:
        # Keep the upstream public refusal result; transactional callers use
        # compile_keymap/install_keymap directly and receive their exceptions.
        if self.keyboard == NULL:
            return False
        candidate = None
        try:
            candidate = self.compile_keymap(rules, model, layout, variant, options)
            self.install_keymap(candidate)
            return True
        except Exception as exc:
            log.warn("Warning: failed to install Wayland keyboard layout: %s", exc)
            return False
        finally:
            if candidate is not None:
                candidate.cleanup()

    cpdef tuple keysyms(self, object keysym, str name, str keystr):
        return event_keysyms(keysym, name, keystr)

    cdef tuple modifier_masks(self, modifiers):
        return self.keyboard_modifier_masks(self.keyboard, modifiers)

    cdef tuple keyboard_modifier_masks(self, wlr_keyboard *keyboard, modifiers):
        cdef uint32_t depressed = 0
        cdef uint32_t locked = 0
        cdef uint32_t bit = 0
        cdef str modifier
        for modifier in modifiers or ():
            bit = self.keyboard_modifier_bit(keyboard, modifier)
            if not bit:
                continue
            if modifier in LOCKED_MODIFIERS:
                locked |= bit
            else:
                depressed |= bit
        return depressed, locked

    def resolve_keycode(self, keysym: int, name: str, groups, modifiers=(), keystr: str = "",
                        fixed_modifiers=()) -> tuple[int, int]:
        cdef uint32_t depressed, locked
        cdef tuple symbols = self.keysyms(keysym, name, keystr)
        cdef tuple trials = modifier_trials(modifiers, fixed_modifiers)
        cdef tuple trial
        cdef list masked_trials = []
        if not symbols or self.keyboard == NULL or self.keyboard.keymap == NULL:
            return -1, 0
        for trial in trials:
            depressed, locked = self.modifier_masks(trial)
            masked_trials.append((trial, depressed, locked))
        return resolve_keycode_candidates(
            self.keyboard.keymap, self.keysym_to_keycodes, self.group_count,
            symbols, tuple(groups), tuple(masked_trials), modifiers,
        )

    def get_keycode_for_keysym(self, keysym: int, group: int = -1, modifiers=()) -> tuple[int, int]:
        groups = tuple(range(self.group_count)) if group < 0 else (group,)
        return self.resolve_keycode(keysym, "", groups, modifiers)

    def get_keycode_for_keyname(self, name: str, group: int = -1, modifiers=()) -> tuple[int, int]:
        groups = tuple(range(self.group_count)) if group < 0 else (group,)
        return self.resolve_keycode(0, name, groups, modifiers)

    def press_key(self, keycode: int, press: bool) -> None:
        cdef uint32_t time_msec = get_time_msec()
        cdef wl_keyboard_key_state state = (
            WL_KEYBOARD_KEY_STATE_PRESSED if press else WL_KEYBOARD_KEY_STATE_RELEASED
        )
        cdef wlr_keyboard_key_event event
        if self.keyboard != NULL and keycode >= 8:
            log("wlr_seat_keyboard_notify_key(%#x, %i, %i, %i)", <uintptr_t> self.seat, time_msec, keycode, state)
            memset(&event, 0, sizeof(wlr_keyboard_key_event))
            event.time_msec = time_msec
            event.keycode = keycode - 8
            # Modifier state comes from the client's complete modifier list;
            # still update wlroots' held-key array for focus enter and cleanup.
            event.update_state = False
            event.state = state
            wlr_keyboard_notify_key(self.keyboard, &event)
            wlr_seat_keyboard_notify_key(self.seat, time_msec, keycode - 8, state)
            if press:
                self.keys_down.add(keycode)
            else:
                self.keys_down.discard(keycode)

    def clear_keys_pressed(self, keycodes) -> None:
        for keycode in tuple(dict.fromkeys(keycodes or ())):
            if keycode in self.keys_down:
                self.press_key(keycode, False)

    def set_repeat_rate(self, delay, interval) -> None:
        cdef int32_t irate
        cdef int32_t idelay
        if self.keyboard == NULL:
            return
        try:
            delay = int(delay)
            interval = int(interval)
        except (TypeError, ValueError, OverflowError):
            delay = interval = 0
        if delay <= 0 or interval <= 0:
            wlr_keyboard_set_repeat_info(self.keyboard, 0, 0)
            log("disabled keyboard repeat")
            return
        irate = <int32_t> min(2147483647, max(1, round(1000 / interval)))
        idelay = <int32_t> min(2147483647, delay)
        wlr_keyboard_set_repeat_info(self.keyboard, irate, idelay)
        log("wlr_keyboard_set_repeat_info(%#x, %i, %i)", <uintptr_t> self.keyboard, irate, idelay)

    def get_keycodes_down(self) -> Sequence[int]:
        return tuple(sorted(self.keys_down))

    def get_keycode_capacity(self) -> int:
        return xpra_wlr_keyboard_keys_cap()

    def get_layout_group(self) -> int:
        return self.group

    def get_layout_group_count(self) -> int:
        return self.group_count

    def get_modifiers(self) -> tuple[str, ...]:
        return tuple(self.modifiers)

    def safe_group(self, group: int) -> int:
        if group < 0 or group >= self.group_count:
            return 0
        return group

    def set_layout_group(self, group: int) -> None:
        self.update_modifiers(self.modifiers, self.safe_group(group))

    def reapply_modifiers(self) -> None:
        self.update_modifiers(self.modifiers, self.group)

    def update_modifiers(self, modifiers=(), group: int = 0) -> None:
        cdef uint32_t depressed = 0
        cdef uint32_t locked = 0
        self.modifiers = tuple(x for x in (modifiers or ()) if x)
        self.group = self.safe_group(group)
        if self.keyboard == NULL:
            return
        depressed, locked = self.modifier_masks(self.modifiers)
        log("update_modifiers(%s, group=%i) depressed=%#x locked=%#x",
            self.modifiers, self.group, depressed, locked)
        wlr_keyboard_notify_modifiers(self.keyboard, depressed, 0, locked, self.group)
        wlr_seat_keyboard_notify_modifiers(self.seat, &self.keyboard.modifiers)

    cdef uint32_t modifier_bit(self, str modifier):
        return self.keyboard_modifier_bit(self.keyboard, modifier)

    cdef uint32_t keyboard_modifier_bit(self, wlr_keyboard *keyboard, str modifier):
        cdef int index = MOD_INDEX.get(modifier, -1)
        if index < 0 or index >= 8 or keyboard == NULL:
            return 0
        cdef uint32_t mod_index = keyboard.mod_indexes[index]
        if mod_index == <uint32_t> -1 or mod_index >= 32:
            return 0
        return <uint32_t> 1 << mod_index

    def focus(self, uintptr_t xdg_surface_ptr) -> None:
        if not xdg_surface_ptr:
            wlr_seat_keyboard_notify_clear_focus(self.seat)
            log("focus(%#x) cleared focus", xdg_surface_ptr)
            return
        cdef wlr_xdg_surface *xdg_surface = <wlr_xdg_surface*> xdg_surface_ptr
        cdef wlr_surface *surface = xdg_surface.surface
        if not surface:
            log("surface is NULL, cleared focus")
            wlr_seat_keyboard_notify_clear_focus(self.seat)
            return

        wlr_seat_keyboard_notify_enter(self.seat, surface,
                                       self.keyboard.keycodes, self.keyboard.num_keycodes, &self.keyboard.modifiers)
        log("keyboard.focus(%#x) done", xdg_surface_ptr)
