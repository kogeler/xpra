#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 Antoine Martin <antoine@xpra.org>
# Copyright (C) 2026 kogeler
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from xpra.codecs.image import ImageWrapper
from xpra.server.subsystem.window import WindowServer
from xpra.util.colourspace import Colourspace, Primaries, SRGB, TransferFunction
from xpra.wayland.server.models.subsurface_window import SubsurfaceWindow
from xpra.wayland.server.models import window as wayland_window_model

Window = wayland_window_model.Window


def absent_case_callable(name: str):
    def fail(*_args, **_kwargs):
        raise AssertionError(f"required Wayland case behavior {name} is absent")
    return fail


image_for_xdg_geometry = getattr(
    wayland_window_model, "image_for_xdg_geometry",
    absent_case_callable("image_for_xdg_geometry"),
)
wayland_sampling_affine = getattr(
    wayland_window_model, "wayland_sampling_affine",
    absent_case_callable("wayland_sampling_affine"),
)
xdg_root_damage = getattr(
    wayland_window_model, "xdg_root_damage",
    absent_case_callable("xdg_root_damage"),
)


def load_window_server_class():
    modules = {}
    for module_name, class_name in (
            ("xpra.wayland.server.popup", "Popup"),
            ("xpra.wayland.server.subsurface", "Subsurface"),
            ("xpra.wayland.server.surface", "Surface"),
    ):
        module = ModuleType(module_name)
        setattr(module, class_name, type(class_name, (), {}))
        modules[module_name] = module
    with patch.dict(sys.modules, modules):
        from xpra.wayland.server.subsystem import window as window_server
    return window_server.WaylandWindowServer, getattr(
        window_server, "replace_image_snapshot", absent_case_callable("replace_image_snapshot"),
    )


WaylandWindowServer, replace_image_snapshot = load_window_server_class()


class FakeSubsurface:

    def __init__(self, wid: int):
        self.wid = wid
        self.callbacks = {}
        self.connect_calls = []
        self.frame_done_calls = 0

    def connect(self, event: str, callback) -> None:
        self.connect_calls.append(event)
        self.callbacks[event] = callback

    def frame_done(self) -> None:
        self.frame_done_calls += 1


class WindowTopologyHarness(WaylandWindowServer):

    def __init__(self):
        # These tests exercise the real topology implementation without
        # constructing a compositor or replacing disputed methods with mocks.
        self.focused = 0
        self.pointer_focus = 0
        self.toplevel_wid = {}
        self.pending_popups = {}
        self.subsurface_info = {}
        self.subsurface_facades = {}
        self.subsurfaces = {}
        self.subsurface_parents = {}
        self.subsurface_stacking = {}
        self.subsurface_topology_errors = {}
        self.windows = {}
        self.sources = ()
        self.client_properties = {}
        self.server = SimpleNamespace(subsystems={}, compositor=Mock())

    def get_window(self, wid: int):
        return self.windows.get(wid)

    def window_sources(self, *args, **kwargs):
        return self.sources

    def add_root(self, wid: int, width: int = 100, height: int = 80,
                 colourspace: dict | None = None, *, snapshot: bool = True) -> Window:
        surface = Mock()
        surface.wid = wid
        surface.get_colourspace.return_value = colourspace or SRGB.to_dict()
        window = Window({
            "client-machine": "test", "display": None, "surface": surface,
            "colourspace": colourspace or SRGB.to_dict(), "title": "", "app-id": "",
            "parent": 0, "transient-for": 0, "relative-position": (),
            "override-redirect": False, "window-type": ("NORMAL",), "role": "",
            "iconic": False, "geometry": (0, 0, width, height), "image": None,
            "depth": 32, "has-alpha": True, "decorations": False,
        })
        window.setup()
        if snapshot:
            image = make_image(width, height, 1)
            window.set_image(image)
            image.free()
        self.windows[wid] = window
        return window


def make_image(width: int, height: int, value: int = 0,
               pixel_format: str = "BGRA") -> ImageWrapper:
    return ImageWrapper(
        0, 0, width, height, bytes([value]) * (width * height * 4),
        pixel_format, 32, width * 4,
    )


class ReconcileConnection:
    """Stateful exact-model connection double for reconciliation tests."""

    def __init__(self, uuid: str, supported: bool = True):
        self.uuid = uuid
        self.supported = supported
        self.refused = {}
        self.announced = {}
        self.child_sources = {}
        self.events = []
        self.fail_child = 0

    def bind_announced(self, wid: int, window) -> None:
        self.announced[wid] = window

    def supports_subsurface_composite(self) -> bool:
        return self.supported

    def is_window_refused(self, wid: int, window) -> bool:
        return self.refused.get(wid) is window

    def is_window_announced(self, wid: int, window) -> bool:
        return self.announced.get(wid) is window

    def can_consume_window_damage(self, wid: int, window) -> bool:
        return self.is_window_announced(wid, window)

    def refuse_window(self, wid: int, window, reason: str) -> bool:
        self.events.append(("refuse", wid, reason))
        self.refused[wid] = window
        if self.announced.get(wid) is window:
            self.announced.pop(wid)
        return True

    def allow_window(self, wid: int, window) -> bool:
        if self.refused.get(wid) is not window:
            return False
        self.events.append(("allow", wid))
        self.refused.pop(wid)
        return True

    def make_subsurface_source(
            self, wid: int, root_wid: int, offset_x: int, offset_y: int,
            facade, logical_w: int, logical_h: int,
            native_w: int, native_h: int, *, parent_window):
        self.events.append((
            "child", wid, root_wid, offset_x, offset_y,
            logical_w, logical_h, native_w, native_h,
            facade.has_image(), facade.get_colourspace_snapshot(),
        ))
        if wid == self.fail_child:
            return None
        source = self.child_sources.setdefault(wid, object())
        return source

    def update_subsurface_geometries(self, root_wid: int, geometries, order) -> None:
        self.events.append(("topology", root_wid, tuple(geometries), tuple(order)))

    def new_window(self, packet_type: str, wid: int, window,
                   x: int, y: int, width: int, height: int, properties) -> None:
        if self.is_window_refused(wid, window):
            return
        self.events.append(("new", packet_type, wid, x, y, width, height, dict(properties)))
        self.announced[wid] = window

    def damage(self, wid: int, window, x: int, y: int,
               width: int, height: int, options) -> None:
        if self.is_window_refused(wid, window):
            return
        self.events.append(("damage", wid, x, y, width, height, dict(options)))

    def cleanup_subsurface_source(self, wid: int) -> None:
        self.events.append(("cleanup", wid))
        self.child_sources.pop(wid, None)


def compositor_tree():
    # child 2 is below the root, child 3 is nested within it, and child 4 is
    # above the root. This is the order delivered by wlroots' renderer walk.
    return (
        (2, -2, 3, 20, 10, 20, 10),
        (7, 0, 0, 100, 80, 100, 80),
        (3, 4, 5, 8, 6, 8, 6),
        (4, 60, 12, 16, 14, 16, 14),
    )


class WaylandSubsurfaceTopologyTest(unittest.TestCase):

    @staticmethod
    def supporting_connection():
        connection = Mock()
        connection.uuid = "test-client"
        connection.supports_subsurface_composite.return_value = True
        connection.is_window_refused.return_value = False
        connection.is_window_announced.return_value = True
        connection.can_consume_window_damage.return_value = True
        connection.make_subsurface_source.return_value = Mock()
        return connection

    @staticmethod
    def register_tree(server: WindowTopologyHarness) -> tuple[FakeSubsurface, ...]:
        if not server.get_window(7):
            server.add_root(7)
        below = FakeSubsurface(2)
        nested = FakeSubsurface(3)
        above = FakeSubsurface(4)
        for parent_wid, child, width, height, value in (
                (7, below, 20, 10, 2),
                (2, nested, 8, 6, 3),
                (7, above, 16, 14, 4),
        ):
            initial = make_image(width, height, value)
            server.new_subsurface(
                parent_wid, child, width, height, width, height,
                initial_image=initial, colourspace=SRGB.to_dict(),
            )
            # Mirrors the native emitter's single owner-finally.
            initial.free()
        return below, nested, above

    @staticmethod
    def commit_child(server: WindowTopologyHarness, wid: int, root_wid: int,
                     mapped: bool, has_buffer: bool, tree, image=None,
                     colourspace: dict | None = None, damage_rects=None) -> None:
        info = server.subsurface_info.get(wid)
        entry = next((item for item in tree if item[0] == wid), None)
        if entry:
            _wid, _x, _y, logical_w, logical_h, native_w, native_h = entry
        elif info:
            _root, _x, _y, logical_w, logical_h, native_w, native_h = info
        else:
            facade = server.subsurface_facades[wid]
            logical_w, logical_h = facade.get_dimensions()
            native_w, native_h = logical_w, logical_h
        try:
            server.subsurface_commit(
                wid, root_wid, mapped, has_buffer, tree,
                damage_rects if damage_rects is not None else ((0, 0, logical_w, logical_h),),
                image,
                logical_w, logical_h, native_w, native_h,
                colourspace or SRGB.to_dict(),
            )
        finally:
            # Mirrors the native emitter's single owner-finally.  The Python
            # handler borrows the wrapper and must never consume it.
            if image is not None:
                image.free()

    def test_nested_registration_and_exact_bottom_to_top_tree(self):
        server = WindowTopologyHarness()
        below, nested, above = self.register_tree(server)
        connection = self.supporting_connection()
        server.sources = (connection,)

        server._update_subsurface_topology(7, compositor_tree())

        self.assertEqual(server.subsurface_parents, {2: 7, 3: 2, 4: 7})
        self.assertEqual(server.subsurface_stacking[7], (2, 7, 3, 4))
        self.assertEqual(server.subsurface_info, {
            2: (7, -2, 3, 20, 10, 20, 10),
            3: (7, 4, 5, 8, 6, 8, 6),
            4: (7, 60, 12, 16, 14, 16, 14),
        })
        connection.update_subsurface_geometries.assert_called_once_with(
            7,
            (compositor_tree()[0], compositor_tree()[2], compositor_tree()[3]),
            (2, 7, 3, 4),
        )
        for wrapper in (below, nested, above):
            self.assertEqual(set(wrapper.callbacks), {
                "destroy", "new-subsurface", "subsurface-commit",
                "subsurface-role-destroy",
            })

    def test_root_commit_installs_topology_before_empty_damage_ack(self):
        server = WindowTopologyHarness()
        self.register_tree(server)
        window = server.windows[7]
        surface = Mock()
        server.get_surface = Mock(return_value=surface)
        server.track_toplevel = Mock()
        server.update_colourspace = Mock()
        server.update_size = Mock()
        server.refresh_window_area = Mock()
        connection = self.supporting_connection()
        events = []
        connection.update_subsurface_geometries.side_effect = lambda *_args: events.append("topology")
        window.acknowledge_changes = Mock(side_effect=lambda: events.append("ack"))
        server.sources = (connection,)

        server.commit(7, True, (100, 80), (), compositor_tree())

        self.assertEqual(events, ["topology", "ack"])
        server.refresh_window_area.assert_not_called()

    def test_root_only_surface_tree_delegates_empty_damage_pacing(self):
        server = WindowTopologyHarness()
        window = server.add_root(7)
        surface = Mock()
        server.get_surface = Mock(return_value=surface)
        server.track_toplevel = Mock()
        server.update_colourspace = Mock()
        server.update_size = Mock()
        window.schedule_empty_acknowledgement = Mock()
        window.acknowledge_changes = Mock()

        server.commit(
            7, True, (100, 80), (),
            ((7, 0, 0, 100, 80, 100, 80),),
        )

        window.schedule_empty_acknowledgement.assert_called_once_with()
        window.acknowledge_changes.assert_not_called()

    def test_refused_root_without_repair_delegates_empty_damage_pacing(self):
        server = WindowTopologyHarness()
        window = server.add_root(7)
        surface = Mock()
        server.get_surface = Mock(return_value=surface)
        server.track_toplevel = Mock()
        server.update_colourspace = Mock()
        server.update_size = Mock()
        window.schedule_empty_acknowledgement = Mock()
        window.acknowledge_changes = Mock()
        connection = self.supporting_connection()
        server.sources = (connection,)

        server.commit(7, True, (100, 80), (), ())

        connection.refuse_window.assert_called_once()
        connection.damage.assert_not_called()
        window.schedule_empty_acknowledgement.assert_called_once_with()
        window.acknowledge_changes.assert_not_called()

    def test_root_repair_remains_the_only_ordinary_ack_owner(self):
        server = WindowTopologyHarness()
        window = server.add_root(7)
        surface = Mock()
        server.get_surface = Mock(return_value=surface)
        server.track_toplevel = Mock()
        server.update_colourspace = Mock()
        server.update_size = Mock()
        window.schedule_empty_acknowledgement = Mock()
        pending = {"value": False}
        events = []
        window.cancel_empty_ack_timer = Mock(
            side_effect=lambda: events.append("cancel"),
        )
        window.mark_damage_frame_pending = Mock(
            side_effect=lambda: (events.append("mark"), pending.__setitem__("value", True)),
        )
        connection = self.supporting_connection()
        connection.is_window_announced.return_value = False

        def announce(*_args):
            events.append("announce")
            connection.is_window_announced.return_value = True

        def synchronous_damage(*_args):
            events.append("damage")
            self.assertTrue(pending["value"])
            pending["value"] = False

        connection.new_window.side_effect = announce
        connection.damage.side_effect = synchronous_damage
        server.sources = (connection,)

        server.commit(
            7, True, (100, 80), (),
            ((7, 0, 0, 100, 80, 100, 80),),
        )

        connection.damage.assert_called_once_with(
            7, window, 0, 0, 100, 80, {"damage": True},
        )
        self.assertEqual(events, ["announce", "cancel", "mark", "damage"])
        self.assertFalse(pending["value"])
        window.cancel_empty_ack_timer.assert_called_once_with()
        window.mark_damage_frame_pending.assert_called_once_with()
        window.schedule_empty_acknowledgement.assert_not_called()

    def test_child_driven_root_only_repair_guards_before_damage(self):
        server = WindowTopologyHarness()
        self.register_tree(server)
        window = server.windows[7]
        server._update_subsurface_topology(7, compositor_tree(), reconcile=False)
        connection = ReconcileConnection("root-only-repair")
        connection.bind_announced(7, window)
        server.sources = (connection,)
        pending = {"value": False}
        events = []
        window.cancel_empty_ack_timer = Mock(
            side_effect=lambda: events.append("cancel"),
        )
        window.mark_damage_frame_pending = Mock(
            side_effect=lambda: (events.append("mark"), pending.__setitem__("value", True)),
        )

        def synchronous_damage(*_args):
            events.append("damage")
            self.assertTrue(pending["value"])
            pending["value"] = False

        connection.damage = Mock(side_effect=synchronous_damage)

        self.commit_child(
            server, 2, 7, False, False,
            ((7, 0, 0, 100, 80, 100, 80),),
            damage_rects=(),
        )

        self.assertEqual(server.subsurface_stacking[7], (7,))
        self.assertEqual(events, ["cancel", "mark", "damage"])
        self.assertFalse(pending["value"])
        connection.damage.assert_called_once_with(
            7, window, 0, 0, 100, 80, {"damage": True},
        )

    def test_unannounced_source_does_not_suppress_empty_damage_pacing(self):
        server = WindowTopologyHarness()
        window = server.add_root(7)
        surface = Mock()
        server.get_surface = Mock(return_value=surface)
        server.track_toplevel = Mock()
        server.update_colourspace = Mock()
        server.update_size = Mock()
        window.schedule_empty_acknowledgement = Mock()
        unavailable = self.supporting_connection()
        unavailable.uuid = "unavailable"
        unavailable.is_window_announced.return_value = False
        unavailable.can_consume_window_damage.return_value = False
        eligible = self.supporting_connection()
        eligible.uuid = "eligible"
        server.sources = unavailable, eligible

        server.commit(
            7, True, (100, 80), (),
            ((7, 0, 0, 100, 80, 100, 80),),
        )

        unavailable.new_window.assert_called_once()
        unavailable.damage.assert_not_called()
        eligible.damage.assert_not_called()
        window.schedule_empty_acknowledgement.assert_called_once_with()

    def test_failed_repair_releases_only_its_new_guard_for_empty_pacing(self):
        server = WindowTopologyHarness()
        window = server.add_root(7)
        surface = Mock()
        server.get_surface = Mock(return_value=surface)
        server.track_toplevel = Mock()
        server.update_colourspace = Mock()
        server.update_size = Mock()
        events = []
        # The model's damage guard is its pending safety timer.
        window.mark_damage_frame_pending = Mock(
            side_effect=lambda: (events.append("mark"), setattr(window, "_damage_frame_timer", 1)),
        )
        window.cancel_damage_frame_timer = Mock(
            side_effect=lambda: (events.append("clear"), setattr(window, "_damage_frame_timer", 0)),
        )
        window.cancel_empty_ack_timer = Mock(
            side_effect=lambda: events.append("cancel"),
        )

        def schedule():
            events.append("schedule")
            self.assertFalse(window._damage_frame_timer)

        window.schedule_empty_acknowledgement = Mock(side_effect=schedule)
        failing = self.supporting_connection()
        failing.uuid = "failing"
        failing.is_window_announced.return_value = False

        def announce(*_args):
            events.append("announce")
            failing.is_window_announced.return_value = True

        failing.new_window.side_effect = announce
        failing.damage.side_effect = RuntimeError("repair failed")
        eligible = self.supporting_connection()
        eligible.uuid = "eligible"
        server.sources = failing, eligible

        server.commit(
            7, True, (100, 80), (),
            ((7, 0, 0, 100, 80, 100, 80),),
        )

        self.assertEqual(
            events,
            ["announce", "cancel", "mark", "clear", "schedule"],
        )
        self.assertFalse(window._damage_frame_timer)
        eligible.damage.assert_not_called()
        window.cancel_damage_frame_timer.assert_called_once_with()

    def test_failed_repair_preserves_an_existing_damage_guard(self):
        server = WindowTopologyHarness()
        window = server.add_root(7)
        server.subsurface_stacking[7] = (7,)
        # An older damage owner already holds the model's pending timer.
        window._damage_frame_timer = 1
        window.mark_damage_frame_pending = Mock()
        window.cancel_damage_frame_timer = Mock()
        connection = self.supporting_connection()
        connection.damage.side_effect = RuntimeError("repair failed")
        server.sources = (connection,)
        repaired = set()

        handled = server._reconcile_subsurface_root(
            7, full_damage=True, repaired_sources=repaired,
        )

        self.assertEqual(handled, {connection})
        self.assertEqual(repaired, set())
        self.assertEqual(window._damage_frame_timer, 1)
        window.cancel_damage_frame_timer.assert_not_called()
        window._damage_frame_timer = 0

    def test_root_only_surface_tree_guards_damage_before_fanout(self):
        server = WindowTopologyHarness()
        window = server.add_root(7)
        surface = Mock()
        server.get_surface = Mock(return_value=surface)
        server.track_toplevel = Mock()
        server.update_colourspace = Mock()
        server.update_size = Mock()
        events = []
        window.cancel_empty_ack_timer = Mock(
            side_effect=lambda: events.append("cancel"),
        )
        window.mark_damage_frame_pending = Mock(
            side_effect=lambda: events.append("mark"),
        )
        window.acknowledge_changes = Mock()
        connection = self.supporting_connection()
        connection.damage.side_effect = lambda *_args: events.append("damage")
        server.sources = (connection,)

        server.commit(
            7, True, (100, 80), ((0, 0, 5, 4),),
            ((7, 0, 0, 100, 80, 100, 80),),
        )

        self.assertEqual(events, ["cancel", "mark", "damage"])
        window.mark_damage_frame_pending.assert_called_once_with()
        window.acknowledge_changes.assert_not_called()

    def test_other_root_reconciliation_does_not_suppress_root_damage(self):
        server = WindowTopologyHarness()
        window = server.add_root(7)
        server.add_root(8)
        surface = Mock()
        server.get_surface = Mock(return_value=surface)
        server.track_toplevel = Mock()
        server.update_colourspace = Mock()
        server.update_size = Mock()
        server.subsurface_stacking.update({7: (7,), 8: (8,)})
        connection = self.supporting_connection()
        server.sources = (connection,)
        server._update_subsurface_topology = Mock(return_value={7, 8})
        server._reconcile_subsurface_root = Mock(
            side_effect=lambda root_wid, **_kwargs: {connection} if root_wid == 8 else set(),
        )

        server.commit(
            7, True, (100, 80), ((0, 0, 5, 4),),
            ((7, 0, 0, 100, 80, 100, 80),),
        )

        connection.damage.assert_called_once_with(
            7, window, 0, 0, 5, 4, {"damage": True, "more": False},
        )

    def test_other_root_reconciliation_does_not_suppress_child_damage(self):
        server = WindowTopologyHarness()
        self.register_tree(server)
        server.add_root(8)
        connection = self.supporting_connection()
        server.sources = (connection,)
        server.subsurface_info[2] = (8, -2, 3, 20, 10, 20, 10)
        server._prepare_subsurface_snapshot = Mock()
        server._update_subsurface_topology = Mock(return_value={7, 8})
        server._subsurface_topology_signature = Mock(return_value=("stable",))
        server._reconcile_subsurface_root = Mock(
            side_effect=lambda root_wid, **_kwargs: {connection} if root_wid == 8 else set(),
        )
        server._damage_subsurface_regions = Mock()

        server._apply_subsurface_commit(
            2, 7, True, True, compositor_tree(), ((1, 2, 3, 4),), None,
            20, 10, 20, 10, SRGB.to_dict(),
        )

        server._damage_subsurface_regions.assert_called_once_with(
            2, 7, ((1, 2, 3, 4),), (connection,), set(),
        )

    def test_composite_intercepted_root_damage_is_acknowledged_once(self):
        server = WindowTopologyHarness()
        self.register_tree(server)
        window = server.windows[7]
        surface = Mock()
        server.get_surface = Mock(return_value=surface)
        server.track_toplevel = Mock()
        server.update_colourspace = Mock()
        server.update_size = Mock()
        window.schedule_empty_acknowledgement = Mock()
        window.cancel_empty_ack_timer = Mock()
        window.acknowledge_changes = Mock()
        connections = self.supporting_connection(), self.supporting_connection()
        server.sources = connections

        server.commit(7, True, (100, 80), ((0, 0, 5, 4),), compositor_tree())

        window.acknowledge_changes.assert_called_once_with()
        window.schedule_empty_acknowledgement.assert_not_called()
        window.cancel_empty_ack_timer.assert_called_once_with()
        for connection in connections:
            connection.damage.assert_called_once_with(
                7, window, 0, 0, 5, 4, {"damage": True, "more": False},
            )

    def test_desynchronized_unmap_detaches_subtree_and_remap_restores_layout(self):
        server = WindowTopologyHarness()
        self.register_tree(server)
        connection = self.supporting_connection()
        server.sources = (connection,)
        server._update_subsurface_topology(7, compositor_tree())
        connection.reset_mock()

        self.commit_child(
            server,
            2, 7, False, True,
            (compositor_tree()[1], compositor_tree()[3]),
            make_image(20, 10, 12),
        )

        self.assertEqual(server.subsurface_stacking[7], (7, 4))
        self.assertEqual(set(server.subsurface_info), {4})
        self.assertEqual(set(server.subsurface_facades), {2, 3, 4})
        self.assertEqual(set(server.subsurfaces), {2, 3, 4})
        self.assertEqual(
            [call.args[0] for call in connection.cleanup_subsurface_source.call_args_list],
            [3, 2],
        )

        connection.reset_mock()
        self.commit_child(
            server, 2, 7, True, True, compositor_tree(),
            make_image(20, 10, 13),
        )

        self.assertEqual(server.subsurface_stacking[7], (2, 7, 3, 4))
        self.assertEqual(set(server.subsurface_info), {2, 3, 4})
        self.assertEqual(set(server.subsurface_facades), {2, 3, 4})
        self.assertEqual(
            [call.args[0] for call in connection.make_subsurface_source.call_args_list],
            [2, 3, 4],
        )
        connection.update_subsurface_geometries.assert_called_once_with(
            7,
            (compositor_tree()[0], compositor_tree()[2], compositor_tree()[3]),
            (2, 7, 3, 4),
        )

    def test_root_unmap_clears_exact_focused_child_before_tree_detach(self):
        server = WindowTopologyHarness()
        self.register_tree(server)
        server._update_subsurface_topology(7, compositor_tree())
        cleared = []

        def clear_focus(*surface_wids):
            cleared.append(surface_wids)
            if not surface_wids or server.pointer_focus in surface_wids:
                server.pointer_focus = 0

        server.server.subsystems["pointer"] = SimpleNamespace(clear_pointer_focus=clear_focus)
        server.pointer_focus = 3

        server.unmap(7)

        self.assertEqual(server.pointer_focus, 0)
        self.assertIn((3,), cleared)
        self.assertEqual(server.subsurface_stacking[7], (7,))
        self.assertEqual(set(server.subsurfaces), {2, 3, 4})

    def test_move_resize_and_restack_replace_authoritative_geometry(self):
        server = WindowTopologyHarness()
        self.register_tree(server)
        connection = self.supporting_connection()
        server.sources = (connection,)
        facade = server.subsurface_facades[2]
        server._update_subsurface_topology(7, compositor_tree())
        connection.reset_mock()
        changed_tree = (
            (4, 1, 2, 16, 14, 16, 14),
            (7, 0, 0, 100, 80, 100, 80),
            (2, 30, 31, 24, 12, 24, 12),
            (3, 34, 35, 8, 6, 8, 6),
        )

        self.commit_child(
            server, 2, 7, True, True, changed_tree,
            make_image(24, 12, 14),
        )

        self.assertEqual(server.subsurface_stacking[7], (4, 7, 2, 3))
        self.assertEqual(server.subsurface_info[2], (7, 30, 31, 24, 12, 24, 12))
        self.assertEqual(facade.get_dimensions(), (24, 12))
        connection.update_subsurface_geometries.assert_called_once_with(
            7, (changed_tree[0], changed_tree[2], changed_tree[3]), (4, 7, 2, 3),
        )

    def test_first_mapped_state_precedes_pixels_and_none_source_is_supported(self):
        server = WindowTopologyHarness()
        server.add_root(7)
        below = FakeSubsurface(2)
        server.new_subsurface(7, below, 20, 10, 20, 10, colourspace=SRGB.to_dict())
        connection = self.supporting_connection()
        connection.make_subsurface_source.return_value = None
        server.sources = (connection,)
        tree = (
            (7, 0, 0, 100, 80, 100, 80),
            (2, 9, 11, 20, 10, 20, 10),
        )

        image = ImageWrapper(0, 0, 20, 10, bytes(20 * 10 * 4), "BGRA", 32, 20 * 4)
        self.commit_child(server, 2, 7, True, True, tree, image)

        connection.make_subsurface_source.assert_called_once()
        connection.damage.assert_not_called()
        self.assertEqual(server.subsurface_info[2], (7, 9, 11, 20, 10, 20, 10))
        facade = server.subsurface_facades[2]
        borrowed = facade.get_image(0, 0, 20, 10)
        self.assertIsNot(borrowed, image)
        self.assertFalse(image.has_pixels())
        self.assertEqual((borrowed.get_target_x(), borrowed.get_target_y()), (0, 0))

    def test_first_role_captures_precommitted_buffer_before_parent_commit(self):
        server = WindowTopologyHarness()
        child = FakeSubsurface(2)
        server.add_root(7)
        connection = self.supporting_connection()
        connection.make_subsurface_source.return_value = Mock()
        server.sources = (connection,)
        initial = ImageWrapper(0, 0, 20, 10, bytes([13]) * (20 * 10 * 4), "BGRA", 32, 20 * 4)

        # The native first-role event carries a snapshot even when applying the
        # role still requires a parent commit and the child has no later commit.
        server.new_subsurface(
            7, child, 20, 10, 20, 10, 7,
            ((7, 0, 0, 100, 80, 100, 80),), initial,
            SRGB.to_dict(),
        )

        self.assertTrue(initial.has_pixels())
        initial.free()
        facade = server.subsurface_facades[2]
        self.assertTrue(facade.has_image())
        self.assertEqual(bytes(facade.get_image(0, 0, 20, 10).get_pixels()), bytes([13]) * (20 * 10 * 4))
        connection.make_subsurface_source.assert_not_called()

        server.get_surface = Mock(return_value=Mock())
        server.track_toplevel = Mock()
        server.update_colourspace = Mock()
        server.update_size = Mock()
        parent_tree = (
            (7, 0, 0, 100, 80, 100, 80),
            (2, 9, 11, 20, 10, 20, 10),
        )
        server.commit(7, True, (100, 80), (), parent_tree)

        connection.make_subsurface_source.assert_called_once()
        self.assertEqual(connection.make_subsurface_source.call_args.args[0], 2)
        self.assertIs(connection.make_subsurface_source.call_args.args[4], facade)

    def test_committed_image_is_shared_safely_by_every_client_source(self):
        server = WindowTopologyHarness()
        server.add_root(7)
        below = FakeSubsurface(2)
        server.new_subsurface(7, below, 5, 6, 5, 6, colourspace=SRGB.to_dict())
        server._update_subsurface_topology(7, (
            (7, 0, 0, 100, 80, 100, 80),
            (2, 3, 4, 5, 6, 5, 6),
        ))
        first = self.supporting_connection()
        second = self.supporting_connection()
        first.make_subsurface_source.return_value = Mock()
        second.make_subsurface_source.return_value = Mock()
        server.sources = first, second
        image = ImageWrapper(0, 0, 5, 6, bytes(5 * 6 * 4), "BGRA", 32, 5 * 4)

        self.commit_child(server, 2, 7, True, True, (
            (7, 0, 0, 100, 80, 100, 80),
            (2, 3, 4, 5, 6, 5, 6),
        ), image)

        first_facade = first.make_subsurface_source.call_args.args[4]
        second_facade = second.make_subsurface_source.call_args.args[4]
        self.assertIs(first_facade, second_facade)
        first_borrow = first_facade.get_image(0, 0, 5, 6)
        second_borrow = second_facade.get_image(0, 0, 5, 6)
        self.assertIsNot(first_borrow, second_borrow)
        self.assertFalse(image.has_pixels())
        first_borrow.set_pixel_format("BGRX")
        first_borrow.set_target_x(99)
        self.assertEqual(second_borrow.get_pixel_format(), "BGRA")
        self.assertEqual(second_borrow.get_target_x(), 0)
        first.damage.assert_called_once()
        second.damage.assert_called_once()

    def test_image_replacement_and_deactivation_preserve_borrowed_frames(self):
        server = WindowTopologyHarness()
        server.add_root(7)
        below = FakeSubsurface(2)
        server.new_subsurface(7, below, 5, 6, 5, 6, colourspace=SRGB.to_dict())
        server._update_subsurface_topology(7, (
            (7, 0, 0, 100, 80, 100, 80),
            (2, 3, 4, 5, 6, 5, 6),
        ))
        connection = self.supporting_connection()
        connection.make_subsurface_source.return_value = Mock()
        server.sources = (connection,)
        first = ImageWrapper(0, 0, 5, 6, bytes(5 * 6 * 4), "BGRA", 32, 5 * 4)
        second = ImageWrapper(0, 0, 5, 6, bytes(5 * 6 * 4), "RGBA", 32, 5 * 4)

        first_pixels = bytes(first.get_pixels())
        second_pixels = bytes(second.get_pixels())
        tree = (
            (7, 0, 0, 100, 80, 100, 80),
            (2, 3, 4, 5, 6, 5, 6),
        )
        self.commit_child(server, 2, 7, True, True, tree, first)
        facade = server.subsurface_facades[2]
        first_retained = facade._image
        borrowed_first = facade.get_image(0, 0, 5, 6)
        self.commit_child(server, 2, 7, True, True, tree, second)

        self.assertIs(server.subsurface_facades[2], facade)
        self.assertFalse(first.has_pixels())
        self.assertFalse(second.has_pixels())
        self.assertFalse(first_retained.has_pixels())
        self.assertEqual(bytes(borrowed_first.get_pixels()), first_pixels)
        borrowed_second = facade.get_image(0, 0, 5, 6)
        self.assertEqual(bytes(borrowed_second.get_pixels()), second_pixels)

        server.unmap(7)

        self.assertIs(server.subsurface_facades[2], facade)
        self.assertTrue(facade.has_image())
        self.assertEqual(bytes(facade.get_image(0, 0, 5, 6).get_pixels()), second_pixels)
        connection.cleanup_subsurface_source.assert_called_once_with(2)

        server.destroy(2)

        self.assertNotIn(2, server.subsurface_facades)
        self.assertFalse(facade.has_image())
        self.assertEqual(bytes(borrowed_second.get_pixels()), second_pixels)

    def test_terminal_destroy_cleans_only_own_identity_after_connection_error(self):
        server = WindowTopologyHarness()
        self.register_tree(server)
        first = self.supporting_connection()
        first.cleanup_subsurface_source.side_effect = RuntimeError("cleanup failed")
        second = self.supporting_connection()
        server.sources = first, second
        server._update_subsurface_topology(7, compositor_tree())

        server.destroy(2)

        self.assertNotIn(2, server.subsurfaces)
        self.assertIn(3, server.subsurfaces)
        self.assertNotIn(3, server.subsurface_parents)
        self.assertEqual(server.subsurface_stacking[7], (7, 4))
        self.assertEqual(
            [call.args[0] for call in first.cleanup_subsurface_source.call_args_list],
            [3, 2],
        )
        self.assertEqual(
            [call.args[0] for call in second.cleanup_subsurface_source.call_args_list],
            [3, 2],
        )

    def test_role_recreate_reparents_static_nested_branch_with_stable_identity(self):
        server = WindowTopologyHarness()
        below, nested, _above = self.register_tree(server)
        server.add_root(8, 120, 90)
        connection = self.supporting_connection()
        connection.make_subsurface_source.return_value = Mock()
        server.sources = (connection,)
        server._update_subsurface_topology(7, compositor_tree())
        below_image = ImageWrapper(0, 0, 20, 10, bytes([17]) * (20 * 10 * 4), "BGRA", 32, 20 * 4)
        nested_image = ImageWrapper(0, 0, 8, 6, bytes([29]) * (8 * 6 * 4), "RGBA", 32, 8 * 4)
        self.commit_child(server, 2, 7, True, True, compositor_tree(), below_image)
        self.commit_child(server, 3, 7, True, True, compositor_tree(), nested_image)
        below_facade = server.subsurface_facades[2]
        nested_facade = server.subsurface_facades[3]
        below_pixels = bytes(below_facade.get_image(0, 0, 20, 10).get_pixels())
        nested_pixels = bytes(nested_facade.get_image(0, 0, 8, 6).get_pixels())
        connection.reset_mock()
        cleared = []

        def clear_focus(*surface_wids):
            cleared.append(surface_wids)
            if not surface_wids or server.pointer_focus in surface_wids:
                server.pointer_focus = 0

        server.server.subsystems["pointer"] = SimpleNamespace(clear_pointer_focus=clear_focus)
        server.pointer_focus = 2

        below.callbacks["subsurface-role-destroy"](2)

        self.assertEqual(server.pointer_focus, 0)
        self.assertIn((2,), cleared)
        self.assertEqual(server.subsurface_stacking[7], (7, 4))
        self.assertNotIn(2, server.subsurface_parents)
        self.assertEqual(server.subsurface_parents[3], 2)
        self.assertIs(server.subsurfaces[2], below)
        self.assertIs(server.subsurfaces[3], nested)
        self.assertIs(server.subsurface_facades[2], below_facade)
        self.assertIs(server.subsurface_facades[3], nested_facade)
        self.assertEqual(bytes(below_facade.get_image(0, 0, 20, 10).get_pixels()), below_pixels)
        self.assertEqual(bytes(nested_facade.get_image(0, 0, 8, 6).get_pixels()), nested_pixels)
        self.assertEqual(
            [call.args[0] for call in connection.cleanup_subsurface_source.call_args_list],
            [3, 2],
        )

        connection.reset_mock()
        new_tree = (
            (8, 0, 0, 120, 90, 120, 90),
            (2, 30, 20, 20, 10, 20, 10),
            (3, 36, 25, 8, 6, 8, 6),
        )
        server.new_subsurface(
            8, below, 20, 10, 20, 10, 8,
            ((8, 0, 0, 120, 90, 120, 90),),
            colourspace=SRGB.to_dict(),
        )

        self.assertEqual(below.wid, 2)
        self.assertEqual(server.subsurface_parents, {2: 8, 3: 2, 4: 7})
        self.assertEqual(server.subsurface_stacking[8], (8,))
        self.assertEqual(below.connect_calls.count("destroy"), 1)
        connection.make_subsurface_source.assert_not_called()

        # Applying the recreated role is a parent-side commit.  No child or
        # nested descendant commit is needed to rematerialize retained pixels.
        server.get_surface = Mock(return_value=Mock())
        server.track_toplevel = Mock()
        server.update_colourspace = Mock()
        server.update_size = Mock()
        server.commit(8, True, (120, 90), (), new_tree)

        self.assertEqual(server.subsurface_stacking[8], (8, 2, 3))
        self.assertEqual(
            [call.args[0] for call in connection.make_subsurface_source.call_args_list],
            [2, 3],
        )
        self.assertEqual(bytes(below_facade.get_image(0, 0, 20, 10).get_pixels()), below_pixels)
        self.assertEqual(bytes(nested_facade.get_image(0, 0, 8, 6).get_pixels()), nested_pixels)

        connection.reset_mock()
        # A later native hit-test may reacquire the same persistent WID after
        # role recreation; terminal wl_surface destroy must clear it again.
        server.pointer_focus = 2
        below.callbacks["destroy"](2)
        server.destroy(2)

        self.assertEqual(server.pointer_focus, 0)
        self.assertGreaterEqual(cleared.count((2,)), 2)
        self.assertNotIn(2, server.subsurfaces)
        self.assertNotIn(2, server.subsurface_facades)
        self.assertFalse(below_facade.has_image())
        self.assertIs(server.subsurfaces[3], nested)
        self.assertIs(server.subsurface_facades[3], nested_facade)
        self.assertNotIn(3, server.subsurface_parents)
        self.assertEqual(bytes(nested_facade.get_image(0, 0, 8, 6).get_pixels()), nested_pixels)
        self.assertEqual(
            [call.args[0] for call in connection.cleanup_subsurface_source.call_args_list],
            [3, 2],
        )

    def test_hidden_buffer_commit_replaces_snapshot_on_root_only_remap(self):
        server = WindowTopologyHarness()
        below, nested, _above = self.register_tree(server)
        window = server.windows[7]
        root_surface = Mock()
        root_surface.get_surface_tree.return_value = compositor_tree()
        server.get_surface = Mock(return_value=root_surface)
        connection = self.supporting_connection()
        connection.make_subsurface_source.return_value = Mock()
        server.sources = (connection,)
        server._update_subsurface_topology(7, compositor_tree())
        image_a = ImageWrapper(0, 0, 20, 10, bytes([11]) * (20 * 10 * 4), "BGRA", 32, 20 * 4)
        nested_image = ImageWrapper(0, 0, 8, 6, bytes([31]) * (8 * 6 * 4), "RGBA", 32, 8 * 4)
        self.commit_child(server, 2, 7, True, True, compositor_tree(), image_a)
        self.commit_child(server, 3, 7, True, True, compositor_tree(), nested_image)
        facade = server.subsurface_facades[2]
        nested_facade = server.subsurface_facades[3]

        server.unmap(7)
        self.assertTrue(facade.has_image())
        self.assertTrue(nested_facade.has_image())
        image_b = ImageWrapper(0, 0, 20, 10, bytes([23]) * (20 * 10 * 4), "BGRA", 32, 20 * 4)
        self.commit_child(
            server, 2, 7, False, True,
            ((7, 0, 0, 100, 80, 100, 80),), image_b,
        )
        self.assertEqual(bytes(facade.get_image(0, 0, 20, 10).get_pixels()), bytes([23]) * (20 * 10 * 4))

        connection.reset_mock()
        server.map(7, "title", "app", (100, 80))

        self.assertFalse(window.get_property("iconic"))
        self.assertEqual(
            [call.args[0] for call in connection.make_subsurface_source.call_args_list],
            [2, 3, 4],
        )
        self.assertEqual(bytes(facade.get_image(0, 0, 20, 10).get_pixels()), bytes([23]) * (20 * 10 * 4))
        self.assertEqual(bytes(nested_facade.get_image(0, 0, 8, 6).get_pixels()), bytes([31]) * (8 * 6 * 4))
        self.assertIs(server.subsurfaces[2], below)
        self.assertIs(server.subsurfaces[3], nested)

    def test_null_buffer_and_failed_capture_never_resurrect_old_generation(self):
        server = WindowTopologyHarness()
        below, nested, _above = self.register_tree(server)
        root_surface = Mock()
        server.get_surface = Mock(return_value=root_surface)
        connection = self.supporting_connection()
        connection.make_subsurface_source.return_value = Mock()
        server.sources = (connection,)
        server._update_subsurface_topology(7, compositor_tree())
        own_image = ImageWrapper(0, 0, 20, 10, bytes([41]) * (20 * 10 * 4), "BGRA", 32, 20 * 4)
        nested_image = ImageWrapper(0, 0, 8, 6, bytes([43]) * (8 * 6 * 4), "RGBA", 32, 8 * 4)
        self.commit_child(server, 2, 7, True, True, compositor_tree(), own_image)
        self.commit_child(server, 3, 7, True, True, compositor_tree(), nested_image)
        own_facade = server.subsurface_facades[2]
        nested_facade = server.subsurface_facades[3]

        # A non-NULL commit whose native capture fails is still one atomic
        # publication, leaving the root refused with no stale child snapshot.
        self.commit_child(server, 2, 7, True, True, compositor_tree())
        self.assertFalse(own_facade.has_image())
        self.assertTrue(nested_facade.has_image())
        root_surface.get_surface_tree.return_value = compositor_tree()
        connection.reset_mock()
        server.unmap(7)
        server.map(7, "title", "app", (100, 80))
        self.assertNotIn(2, [call.args[0] for call in connection.make_subsurface_source.call_args_list])

        replacement = ImageWrapper(0, 0, 20, 10, bytes([47]) * (20 * 10 * 4), "BGRA", 32, 20 * 4)
        self.commit_child(server, 2, 7, True, True, compositor_tree(), replacement)
        self.assertTrue(own_facade.has_image())
        self.commit_child(
            server, 2, 7, False, False,
            ((7, 0, 0, 100, 80, 100, 80),),
        )

        self.assertFalse(own_facade.has_image())
        self.assertTrue(nested_facade.has_image())
        self.assertIs(server.subsurfaces[2], below)
        self.assertIs(server.subsurfaces[3], nested)
        root_surface.get_surface_tree.return_value = ((7, 0, 0, 100, 80, 100, 80),)
        connection.reset_mock()
        server.unmap(7)
        server.map(7, "title", "app", (100, 80))
        connection.make_subsurface_source.assert_not_called()

    def test_reparent_detaches_old_branch_before_new_root_topology(self):
        server = WindowTopologyHarness()
        below, _nested, _above = self.register_tree(server)
        server.add_root(8, 120, 90)
        connection = self.supporting_connection()
        server.sources = (connection,)
        server._update_subsurface_topology(7, compositor_tree())
        connection.reset_mock()

        server.new_subsurface(8, below, 20, 10, 20, 10,
                              colourspace=SRGB.to_dict())

        self.assertEqual(server.subsurface_parents[2], 8)
        self.assertEqual(server.subsurface_stacking[7], (7, 4))
        self.assertEqual(set(server.subsurfaces), {2, 3, 4})
        self.assertEqual(
            [call.args[0] for call in connection.cleanup_subsurface_source.call_args_list],
            [3, 2],
        )

        connection.reset_mock()
        new_tree = (
            (8, 0, 0, 120, 90, 120, 90),
            (2, 30, 20, 20, 10, 20, 10),
            (3, 36, 25, 8, 6, 8, 6),
        )
        self.commit_child(
            server, 2, 8, True, True, new_tree,
            make_image(20, 10, 19),
        )

        self.assertEqual(server.subsurface_stacking[8], (8, 2, 3))
        self.assertEqual(server.subsurface_info[2][0], 8)
        self.assertEqual(server.subsurface_info[3][0], 8)

    def test_late_client_materializes_static_tree_before_parent_damage(self):
        server = WindowTopologyHarness()
        self.register_tree(server)
        server._update_subsurface_topology(7, compositor_tree())
        connection = self.supporting_connection()
        connection.can_send_window.return_value = True
        connection.is_window_announced.return_value = False
        events = []
        connection.update_subsurface_geometries.side_effect = lambda *_args: events.append("topology")
        connection.make_subsurface_source.side_effect = lambda wid, *_args, **_kwargs: (
            events.append(f"child-{wid}") or Mock()
        )

        with patch.object(
                WindowServer, "send_initial_windows",
                autospec=True, side_effect=lambda *_args, **_kwargs: events.append("parent")):
            server.send_initial_windows(connection)

        self.assertEqual(events, ["child-2", "child-3", "child-4", "topology", "parent"])

    def test_atomic_child_commit_prepares_snapshot_before_topology_without_window_churn(self):
        server = WindowTopologyHarness()
        root = server.add_root(7)
        child = FakeSubsurface(2)
        server.new_subsurface(7, child, 20, 10, 20, 10,
                              colourspace=SRGB.to_dict())
        connection = ReconcileConnection("atomic")
        connection.bind_announced(7, root)
        server.sources = (connection,)
        tree = (
            (7, 0, 0, 100, 80, 100, 80),
            (2, 9, 11, 20, 10, 20, 10),
        )
        image = make_image(20, 10, 37)
        native_free = image.free
        free_calls = []

        def tracked_native_free():
            free_calls.append(True)
            native_free()

        image.free = tracked_native_free

        # Exercise the one native callback connected for ordinary child
        # commits, rather than invoking separate state and image handlers.
        try:
            child.callbacks["subsurface-commit"](
                2, 7, True, True, tree, ((0, 0, 20, 10),), image,
                20, 10, 20, 10, SRGB.to_dict(),
            )
            self.assertTrue(image.has_pixels())
        finally:
            # This is the exact native callback contract: the emitter, and no
            # Python observer, owns the one terminal free.
            image.free()

        self.assertEqual(len(free_calls), 1)
        self.assertFalse(image.has_pixels())
        event_names = [event[0] for event in connection.events]
        self.assertEqual(event_names, ["child", "topology", "damage"])
        child_event = connection.events[0]
        self.assertEqual(child_event[5:9], (20, 10, 20, 10))
        self.assertTrue(child_event[9])
        self.assertEqual(child_event[10], SRGB.to_dict())
        self.assertFalse({"refuse", "allow", "new"} & set(event_names))

    def test_effective_child_damage_translates_and_clips_in_root_coordinates(self):
        server = WindowTopologyHarness()
        self.register_tree(server)
        root = server.windows[7]
        connection = ReconcileConnection("effective-damage")
        connection.bind_announced(7, root)
        server.sources = (connection,)
        server._update_subsurface_topology(7, compositor_tree())
        connection.events.clear()

        self.commit_child(
            server, 2, 7, True, True, compositor_tree(),
            make_image(20, 10, 43),
            damage_rects=((0, 0, 4, 4), (18, 8, 5, 5), (25, 2, 3, 3)),
        )

        damage = [event for event in connection.events if event[0] == "damage"]
        self.assertEqual(
            damage,
            [
                ("damage", 7, 0, 3, 2, 4, {"damage": True, "more": True}),
                ("damage", 7, 16, 11, 2, 2, {"damage": True, "more": False}),
            ],
        )
        child_events = [event for event in connection.events if event[0] == "child"]
        self.assertTrue(child_events)
        self.assertTrue(all(event[5:9] == (20, 10, 20, 10) for event in child_events if event[1] == 2))

    def test_image_ownership_is_terminal_on_unknown_and_callback_failure_paths(self):
        server = WindowTopologyHarness()
        unknown_root = make_image(2, 1, 3)
        server.surface_snapshot(99, unknown_root)
        self.assertTrue(unknown_root.has_pixels())
        unknown_root.free()

        unknown_child = make_image(2, 1, 5)
        server.subsurface_commit(
            98, 7, True, True, (), ((0, 0, 2, 1),), unknown_child,
            2, 1, 2, 1, SRGB.to_dict(),
        )
        self.assertTrue(unknown_child.has_pixels())
        unknown_child.free()

        root = server.add_root(7)
        child = FakeSubsurface(2)
        initial = make_image(2, 1, 7)
        server.new_subsurface(
            7, child, 2, 1, 2, 1,
            initial_image=initial, colourspace=SRGB.to_dict(),
        )
        self.assertTrue(initial.has_pixels())
        initial.free()
        connection = ReconcileConnection("failure")
        connection.bind_announced(7, root)
        server.sources = (connection,)
        tree = ((7, 0, 0, 100, 80, 100, 80), (2, 1, 1, 2, 1, 2, 1))
        incoming = make_image(2, 1, 11)
        with patch.object(SubsurfaceWindow, "replace_colourspace_snapshot",
                          side_effect=RuntimeError("callback failed")):
            server.subsurface_commit(
                2, 7, True, True, tree, ((0, 0, 2, 1),), incoming,
                2, 1, 2, 1, SRGB.to_dict(),
            )

        self.assertTrue(incoming.has_pixels())
        incoming.free()
        self.assertFalse(server.subsurface_facades[2].has_image())
        self.assertTrue(connection.is_window_refused(7, root))

    def test_each_child_commit_is_acknowledged_once_after_reconciliation(self):
        server = WindowTopologyHarness()
        below, _nested, _above = self.register_tree(server)
        root = server.windows[7]
        connections = (
            ReconcileConnection("first"),
            ReconcileConnection("second"),
        )
        for connection in connections:
            connection.bind_announced(7, root)
        server.sources = connections
        server._update_subsurface_topology(7, compositor_tree())
        server.server.compositor.reset_mock()

        # A successful generation is acknowledged once for the native
        # wl_surface, not once for each connected Xpra peer.
        self.commit_child(
            server, 2, 7, True, True, compositor_tree(),
            make_image(20, 10, 47),
        )
        self.assertEqual(below.frame_done_calls, 1)
        server.server.compositor.flush.assert_called_once_with()

        # Capture failure invalidates the retained generation and refuses both
        # peers, but the originating client still receives one frame callback
        # so it can render and commit a replacement generation.
        self.commit_child(server, 2, 7, True, True, compositor_tree())
        self.assertEqual(below.frame_done_calls, 2)
        self.assertEqual(server.server.compositor.flush.call_count, 2)
        self.assertTrue(all(connection.is_window_refused(7, root)
                            for connection in connections))

        # The frame callback is already owned by the native surface when the
        # downstream display flush runs.  A flush failure must stay contained
        # inside the noexcept commit route and must not suppress that callback.
        server.server.compositor.flush.side_effect = RuntimeError("flush failed")
        self.commit_child(
            server, 2, 7, True, True, compositor_tree(),
            make_image(20, 10, 49),
        )
        self.assertEqual(below.frame_done_calls, 3)
        self.assertEqual(server.server.compositor.flush.call_count, 3)

    def test_nested_colourspace_eligibility_is_strict_and_recovers_per_client(self):
        server = WindowTopologyHarness()
        self.register_tree(server)
        root = server.windows[7]
        p3 = Colourspace(
            primaries=Primaries.DISPLAY_P3,
            transfer=TransferFunction.SRGB,
        ).to_dict()
        root._updateprop("colourspace", p3)
        for facade in server.subsurface_facades.values():
            facade.replace_colourspace_snapshot(p3)
        connection = ReconcileConnection("colour")
        connection.bind_announced(7, root)
        server.sources = (connection,)

        server._update_subsurface_topology(7, compositor_tree())
        self.assertFalse(connection.is_window_refused(7, root))
        self.assertEqual(
            [event[1] for event in connection.events if event[0] == "child"],
            [2, 3, 4],
        )

        connection.events.clear()
        server.subsurface_facades[3].replace_colourspace_snapshot(SRGB.to_dict())
        server._reconcile_subsurface_root(7, full_damage=True)
        self.assertTrue(connection.is_window_refused(7, root))
        self.assertIn("mixed", connection.events[-1][2])

        connection.events.clear()
        server.subsurface_facades[3].replace_colourspace_snapshot({"primaries": "bt709"})
        server._reconcile_subsurface_root(7, full_damage=True)
        self.assertTrue(connection.is_window_refused(7, root))
        self.assertIn("not canonical", connection.events[-1][2])

        connection.events.clear()
        for facade in server.subsurface_facades.values():
            facade.replace_colourspace_snapshot(p3)
        server._reconcile_subsurface_root(7, full_damage=True)
        self.assertFalse(connection.is_window_refused(7, root))
        self.assertEqual(
            [event[0] for event in connection.events],
            ["child", "child", "child", "topology", "allow", "new", "damage"],
        )

    def test_root_colourspace_only_commit_and_snapshot_failure_reconcile(self):
        server = WindowTopologyHarness()
        self.register_tree(server)
        root = server.windows[7]
        surface = root.get_property("surface")
        server._update_subsurface_topology(7, compositor_tree(), reconcile=False)
        connection = ReconcileConnection("root-generation")
        connection.bind_announced(7, root)
        server.sources = (connection,)

        p3 = Colourspace(
            primaries=Primaries.DISPLAY_P3,
            transfer=TransferFunction.SRGB,
        ).to_dict()
        surface.get_colourspace.return_value = p3
        server.commit(7, True, (100, 80), (), compositor_tree())
        self.assertTrue(connection.is_window_refused(7, root))
        self.assertIn("mixed", connection.events[-1][2])

        surface.get_colourspace.return_value = SRGB.to_dict()
        connection.events.clear()
        server.surface_snapshot(7, None)
        server.commit(7, True, (100, 80), (), compositor_tree())
        self.assertTrue(connection.is_window_refused(7, root))
        self.assertIn("root snapshot", connection.events[-1][2])

        connection.events.clear()
        recovery = make_image(100, 80, 53)
        server.surface_snapshot(7, recovery)
        self.assertTrue(recovery.has_pixels())
        recovery.free()
        server.commit(7, True, (100, 80), (), compositor_tree())
        self.assertFalse(connection.is_window_refused(7, root))
        self.assertEqual(
            [event[0] for event in connection.events],
            ["child", "child", "child", "topology", "allow", "new", "damage"],
        )

    def test_late_clients_are_prepared_or_refused_independently_before_base_send(self):
        server = WindowTopologyHarness()
        self.register_tree(server)
        root = server.windows[7]
        server._update_subsurface_topology(7, compositor_tree(), reconcile=False)
        supported = ReconcileConnection("supported", supported=True)
        unsupported = ReconcileConnection("unsupported", supported=False)
        base_events = []

        def base_send(_server, connection, _sharing=False):
            base_events.append((
                connection.uuid,
                connection.is_window_refused(7, root),
                tuple(event[0] for event in connection.events),
            ))

        # A real function descriptor binds self for both Python and compiled
        # parent methods; autospec cannot infer the compiled descriptor shape.
        with patch.object(WindowServer, "send_initial_windows", new=base_send):
            server.send_initial_windows(unsupported)
            server.send_initial_windows(supported)

        self.assertEqual(base_events[0], ("unsupported", True, ("refuse",)))
        self.assertEqual(
            base_events[1],
            ("supported", False, ("child", "child", "child", "topology")),
        )
        self.assertFalse(supported.events[-1][0] == "damage")

        supported.bind_announced(7, root)
        supported.events.clear()
        unsupported.events.clear()
        server.sources = supported, unsupported
        server._reconcile_subsurface_root(7, full_damage=True)
        self.assertIn("damage", [event[0] for event in supported.events])
        self.assertNotIn("damage", [event[0] for event in unsupported.events])
        self.assertTrue(unsupported.is_window_refused(7, root))

    def test_last_child_removal_restores_root_only_for_unsupported_client(self):
        server = WindowTopologyHarness()
        self.register_tree(server)
        root = server.windows[7]
        unsupported = ReconcileConnection("root-only", supported=False)
        unsupported.bind_announced(7, root)
        server.sources = (unsupported,)
        server._update_subsurface_topology(7, compositor_tree())
        self.assertTrue(unsupported.is_window_refused(7, root))

        # Root-only ordinary streaming does not depend on retained composite
        # snapshots or the extension capability.
        server.surface_snapshot(7, None)
        unsupported.events.clear()
        self.commit_child(
            server, 2, 7, False, False,
            ((7, 0, 0, 100, 80, 100, 80),),
        )

        self.assertEqual(server.subsurface_stacking[7], (7,))
        self.assertFalse(unsupported.is_window_refused(7, root))
        self.assertEqual(
            [event[0] for event in unsupported.events],
            ["cleanup", "cleanup", "cleanup", "allow", "new", "damage"],
        )

    def test_malformed_authoritative_tree_refuses_without_reusing_or_partially_installing(self):
        server = WindowTopologyHarness()
        self.register_tree(server)
        root = server.windows[7]
        connection = ReconcileConnection("malformed")
        connection.bind_announced(7, root)
        server.sources = (connection,)
        server._update_subsurface_topology(7, compositor_tree())
        old_order = server.subsurface_stacking[7]
        old_info = dict(server.subsurface_info)
        connection.events.clear()

        # A root commit missing its structural root marker must not make the
        # previous tree appear current or install the lone child entry.
        server.commit(7, True, (100, 80), (), (compositor_tree()[0],))
        self.assertEqual(server.subsurface_stacking[7], old_order)
        self.assertEqual(server.subsurface_info, old_info)
        self.assertIn(7, server.subsurface_topology_errors)
        self.assertTrue(connection.is_window_refused(7, root))
        self.assertEqual([event[0] for event in connection.events], ["refuse"])

        connection.events.clear()
        server.commit(7, True, (100, 80), (), compositor_tree())
        self.assertNotIn(7, server.subsurface_topology_errors)
        self.assertFalse(connection.is_window_refused(7, root))
        self.assertEqual(
            [event[0] for event in connection.events],
            ["child", "child", "child", "topology", "allow", "new", "damage"],
        )

        # The same fail-closed rule applies when the malformed generation came
        # with a child snapshot.  The image is retained, but it cannot be
        # replayed against stale topology.
        connection.events.clear()
        duplicate_child_tree = compositor_tree() + (compositor_tree()[0],)
        self.commit_child(
            server, 2, 7, True, True, duplicate_child_tree,
            make_image(20, 10, 61),
        )
        self.assertEqual(server.subsurface_stacking[7], old_order)
        self.assertEqual(server.subsurface_info, old_info)
        self.assertIn(7, server.subsurface_topology_errors)
        self.assertTrue(connection.is_window_refused(7, root))
        self.assertEqual([event[0] for event in connection.events], ["refuse"])

        # A native new-role callback carries root_wid even when native tree
        # collection failed closed.  Its empty authoritative tuple must not be
        # mistaken for the legacy omitted-tree default or reuse the old tree.
        connection.events.clear()
        extra = FakeSubsurface(5)
        initial = make_image(3, 2, 67)
        try:
            server.new_subsurface(
                7, extra, 3, 2, 3, 2,
                root_wid=7, surface_tree=(), initial_image=initial,
                colourspace=SRGB.to_dict(),
            )
        finally:
            initial.free()
        self.assertEqual(server.subsurface_stacking[7], old_order)
        self.assertEqual(server.subsurface_info, old_info)
        self.assertIn(7, server.subsurface_topology_errors)
        self.assertTrue(connection.is_window_refused(7, root))
        self.assertEqual([event[0] for event in connection.events], ["refuse"])


class WaylandLogicalRasterTest(unittest.TestCase):

    @staticmethod
    def make_image(width: int, height: int) -> ImageWrapper:
        pixels = bytes(range(width * height * 4))
        return ImageWrapper(3, 5, width, height, pixels, "BGRA", 32, width * 4)

    def test_all_buffer_transforms_map_to_surface_logical_orientation(self):
        source = (("a", "b"), ("c", "d"), ("e", "f"))
        expected = {
            0: ("ab", "cd", "ef"),
            1: ("eca", "fdb"),
            2: ("fe", "dc", "ba"),
            3: ("bdf", "ace"),
            4: ("ba", "dc", "fe"),
            5: ("ace", "bdf"),
            6: ("ef", "cd", "ab"),
            7: ("fdb", "eca"),
        }
        for transform, expected_rows in expected.items():
            logical_width, logical_height = ((3, 2) if transform % 2 else (2, 3))
            affine = wayland_sampling_affine(
                transform, (0.0, 0.0, 2.0, 3.0), logical_width, logical_height,
            )
            actual_rows = []
            for y in range(logical_height):
                row = []
                for x in range(logical_width):
                    sx = affine[0] * x + affine[1] * y + affine[2]
                    sy = affine[3] * x + affine[4] * y + affine[5]
                    row.append(source[round(sy)][round(sx)])
                actual_rows.append("".join(row))
            self.assertEqual(tuple(actual_rows), expected_rows, transform)

    def test_scale_and_fractional_viewport_use_pixel_center_sampling(self):
        self.assertEqual(
            wayland_sampling_affine(0, (0.0, 0.0, 4.0, 2.0), 2, 1),
            (2.0, 0.0, 0.5, 0.0, 2.0, 0.5),
        )
        affine = wayland_sampling_affine(0, (0.25, 0.5, 2.5, 1.5), 2, 3)
        self.assertEqual(affine, (1.25, 0.0, 0.375, 0.0, 0.5, 0.25))

    def test_invalid_sampling_sizes_and_coordinates_fail_before_allocation(self):
        from xpra.constants import MAX_WINDOW_SIZE

        for size in ((0, 1), (1, 0), (MAX_WINDOW_SIZE + 1, 1), (1, 65536)):
            with self.subTest(size=size), self.assertRaises(ValueError):
                wayland_sampling_affine(0, (0.0, 0.0, 4.0, 4.0), *size)
        for invalid in (float("inf"), float("-inf"), float("nan")):
            for index in range(4):
                box = [0.0, 0.0, 4.0, 4.0]
                box[index] = invalid
                with self.subTest(box=box), self.assertRaises(ValueError):
                    wayland_sampling_affine(0, tuple(box), 4, 4)
        source = self.make_image(2, 2)
        try:
            for size in ((MAX_WINDOW_SIZE + 1, 1), (1, MAX_WINDOW_SIZE + 1), (65536, 65536)):
                with self.subTest(size=size), self.assertRaises(ValueError):
                    image_for_xdg_geometry(source, (0, 0, *size))
                self.assertTrue(source.has_pixels())
        finally:
            source.free()

    def test_effective_damage_translation_clipping_and_geometry_change(self):
        geometry = (2, -1, 4, 3)
        damage = ((1, 0, 4, 2), (5, -2, 3, 2), (20, 20, 1, 1))
        self.assertEqual(
            xdg_root_damage(damage, geometry, geometry),
            ((0, 1, 3, 2), (3, 0, 1, 1)),
        )
        self.assertEqual(
            xdg_root_damage((), geometry, (2, -1, 3, 3)),
            ((0, 0, 4, 3),),
        )
        self.assertEqual(
            xdg_root_damage((), geometry, geometry, force_full=True),
            ((0, 0, 4, 3),),
        )

    def test_xdg_geometry_crop_and_transparent_negative_padding(self):
        source = ImageWrapper(
            0, 0, 3, 2,
            bytes(range(3 * 2 * 4)), "BGRA", 32, 3 * 4,
        )
        cropped = image_for_xdg_geometry(source, (1, 0, 2, 2))
        self.assertTrue(source.has_pixels())
        self.assertEqual(cropped.get_pixels(), bytes(range(4, 12)) + bytes(range(16, 24)))
        source.free()
        cropped.free()

        opaque = ImageWrapper(
            0, 0, 2, 2,
            bytes((1, 2, 3, 0, 5, 6, 7, 0,
                   9, 10, 11, 0, 13, 14, 15, 0)),
            "BGRX", 32, 8,
        )
        padded = image_for_xdg_geometry(opaque, (-1, -1, 4, 4))
        self.assertTrue(opaque.has_pixels())
        opaque.free()
        self.assertEqual(padded.get_pixel_format(), "BGRA")
        pixels = bytes(padded.get_pixels())
        self.assertEqual(pixels[:4 * 4], bytes(4 * 4))
        self.assertEqual(pixels[(1 * 4 + 1) * 4:(1 * 4 + 3) * 4],
                         bytes((1, 2, 3, 255, 5, 6, 7, 255)))
        self.assertEqual(pixels[(2 * 4 + 1) * 4:(2 * 4 + 3) * 4],
                         bytes((9, 10, 11, 255, 13, 14, 15, 255)))
        self.assertEqual(pixels[3 * 4 * 4:], bytes(4 * 4))

        root = Window({"geometry": (0, 0, 4, 4), "image": None})
        root.set_image(padded)
        self.assertTrue(padded.has_pixels())
        padded.free()
        full = bytes(root.get_image(0, 0, 4, 4).get_pixels())
        tiled = bytearray(4 * 4 * 4)
        for tile_y in (0, 2):
            for tile_x in (0, 2):
                tile = root.get_image(tile_x, tile_y, 2, 2)
                tile_pixels = tile.get_pixels()
                for row in range(2):
                    destination = (tile_y + row) * 16 + tile_x * 4
                    tiled[destination:destination + 8] = tile_pixels[row * 8:(row + 1) * 8]
        self.assertEqual(bytes(tiled), full)

    def test_xdg_geometry_helper_never_frees_caller_owned_images(self):
        def tracked(image):
            calls = []
            image_free = image.free

            def free():
                calls.append(True)
                image_free()

            image.free = free
            return calls

        exact = self.make_image(2, 2)
        exact_calls = tracked(exact)
        exact_output = image_for_xdg_geometry(exact, (0, 0, 2, 2))
        self.assertIs(exact_output, exact)
        self.assertEqual(exact_calls, [])
        # Simulate Surface.capture_surface_pixels(): one object has one owner.
        exact_output.free()
        self.assertEqual(len(exact_calls), 1)

        cropped_input = self.make_image(3, 2)
        cropped_input_calls = tracked(cropped_input)
        cropped_output = image_for_xdg_geometry(cropped_input, (1, 0, 2, 2))
        cropped_output_calls = tracked(cropped_output)
        self.assertEqual(cropped_input_calls, [])
        self.assertEqual(cropped_output_calls, [])
        # The capture caller owns and releases the distinct normalized input
        # and XDG-canvas output exactly once each.
        cropped_input.free()
        cropped_output.free()
        self.assertEqual(len(cropped_input_calls), 1)
        self.assertEqual(len(cropped_output_calls), 1)

        invalid = self.make_image(2, 2)
        invalid.set_planes(ImageWrapper.PLANAR_3)
        invalid_calls = tracked(invalid)
        with self.assertRaises(ValueError):
            image_for_xdg_geometry(invalid, (0, 0, 1, 1))
        self.assertEqual(invalid_calls, [])
        # The helper's error path also leaves its borrowed input to the caller.
        invalid.free()
        self.assertEqual(len(invalid_calls), 1)

    def test_normalized_full_and_tiled_subsurface_reads_are_identical(self):
        window = SubsurfaceWindow(4, 4)
        self.assertEqual(window.get_snapshot_generation(), 0)
        image = self.make_image(4, 4)
        pixels = bytes(image.get_pixels())
        window.replace_image_snapshot(image)
        self.assertEqual(window.get_snapshot_generation(), 1)
        self.assertTrue(image.has_pixels())
        image.free()
        full = bytes(window.get_image(0, 0, 4, 4).get_pixels())
        tiled = bytearray(4 * 4 * 4)
        for tile_y in (0, 2):
            for tile_x in (0, 2):
                tile = window.get_image(tile_x, tile_y, 2, 2)
                self.assertEqual((tile.get_target_x(), tile.get_target_y()), (tile_x, tile_y))
                tile_pixels = tile.get_pixels()
                for row in range(2):
                    destination = (tile_y + row) * 16 + tile_x * 4
                    tiled[destination:destination + 8] = tile_pixels[row * 8:(row + 1) * 8]
        self.assertEqual(full, pixels)
        self.assertEqual(bytes(tiled), full)
        self.assertIsNone(window.get_image(-2, -2, 1, 1))

    def test_mismatched_logical_raster_is_rejected_without_consuming_borrow(self):
        window = SubsurfaceWindow(4, 4)
        image = self.make_image(2, 2)
        with self.assertRaises(ValueError):
            window.replace_image_snapshot(image)
        self.assertTrue(image.has_pixels())
        image.free()


class WaylandRetainedImageTest(unittest.TestCase):

    @staticmethod
    def make_image(pixel_format="BGRA") -> ImageWrapper:
        return ImageWrapper(0, 0, 2, 1, bytes((1, 2, 3, 4, 5, 6, 7, 8)), pixel_format, 32, 8)

    def test_retained_frame_alpha_tracks_bytes_not_the_backing_capability(self) -> None:
        for model in (
                Window({"geometry": (0, 0, 2, 1), "image": None, "has-alpha": True}),
                SubsurfaceWindow(2, 1),
        ):
            with self.subTest(model=type(model).__name__):
                for pixel_format in ("BGRX", "BGRA", "RGBX", "RGBA"):
                    image = self.make_image(pixel_format)
                    replace_image_snapshot(model, image)
                    image.free()
                    borrowed = model.get_image(0, 0, 2, 1)
                    self.assertEqual(borrowed.get_pixel_format(), pixel_format)
                    self.assertEqual(model.get_property("frame-has-alpha"), "A" in pixel_format)
                    self.assertTrue(model.get_property("has-alpha"))
                    model.clear_image()
                    self.assertTrue(model.get_property("frame-has-alpha"))
                    self.assertEqual(borrowed.get_pixel_format(), pixel_format)
                    self.assertTrue(borrowed.has_pixels())
                    borrowed.free()

    def test_unmanage_disconnects_even_if_snapshot_release_raises(self) -> None:
        window = Window({"geometry": (0, 0, 2, 1), "image": None})
        window._managed = True
        with (
            patch.object(window, "clear_image", side_effect=RuntimeError("snapshot release failed")),
            patch.object(window, "managed_disconnect") as disconnect,
            self.assertRaisesRegex(RuntimeError, "snapshot release failed"),
        ):
            window.do_unmanaged(False)
        self.assertFalse(window._managed)
        disconnect.assert_called_once_with()

    def test_root_retention_failure_reaches_native_capture_owner(self) -> None:
        window = Window({"geometry": (0, 0, 2, 1), "image": None})
        server = WindowTopologyHarness()
        server.windows[7] = window
        image = self.make_image("BGRX")
        server.surface_snapshot(7, image)
        with patch.object(window, "set_image", side_effect=MemoryError("retention failed")):
            with self.assertRaisesRegex(MemoryError, "retention failed"):
                server.surface_snapshot(7, image)
        self.assertTrue(image.has_pixels())
        self.assertFalse(window.has_image())
        self.assertTrue(window.get_property("frame-has-alpha"))
        image.free()

    def test_toplevel_replacement_and_clear_preserve_only_outstanding_borrows(self) -> None:
        window = Window({"geometry": (0, 0, 2, 1), "image": None})
        self.assertEqual(window.get_snapshot_generation(), 0)
        first = self.make_image()
        window.set_image(first)
        self.assertEqual(window.get_snapshot_generation(), 1)
        self.assertTrue(first.has_pixels())
        first_retained = window._gproperties["image"]
        borrow = window.get_image(0, 0, 2, 1)
        expected = bytes(borrow.get_pixels())
        borrow.set_pixel_format("BGRX")

        second = self.make_image("RGBA")
        window.set_image(second)
        self.assertEqual(window.get_snapshot_generation(), 2)

        self.assertTrue(first.has_pixels())
        self.assertTrue(second.has_pixels())
        self.assertFalse(first_retained.has_pixels())
        self.assertEqual(bytes(borrow.get_pixels()), expected)
        self.assertEqual(window.get_image(0, 0, 2, 1).get_pixel_format(), "RGBA")

        second_retained = window._gproperties["image"]
        window.clear_image()
        self.assertEqual(window.get_snapshot_generation(), 3)
        self.assertFalse(second_retained.has_pixels())
        self.assertIsNone(window.get_image(0, 0, 2, 1))
        self.assertEqual(bytes(borrow.get_pixels()), expected)
        first.free()
        second.free()

    def test_toplevel_replacement_rolls_back_partial_property_updates(self) -> None:
        for failure_property in ("image", "pixel-format", "frame-has-alpha"):
            with self.subTest(failure_property=failure_property):
                window = Window({"geometry": (0, 0, 2, 1), "image": None})
                previous = self.make_image("BGRA")
                incoming = self.make_image("RGBA")
                retained = self.make_image("RGBA")
                calls = {"previous": 0, "incoming": 0, "retained": 0}

                def track(image, name):
                    image_free = image.free

                    def free(counters=calls, counter_name=name):
                        counters[counter_name] += 1
                        image_free()

                    image.free = free

                track(previous, "previous")
                track(incoming, "incoming")
                track(retained, "retained")
                window._gproperties["image"] = previous
                window._gproperties["pixel-format"] = "BGRA"

                def failing_update(name, value):
                    # Match the real `_updateprop` ordering: store, then notify.
                    window._gproperties[name] = value
                    if name == failure_property:
                        raise RuntimeError(f"injected {name} notification failure")
                    return True

                with (
                    patch.object(window, "get_internal_property_names",
                                 return_value=("pixel-format",)),
                    patch.object(window, "_updateprop", side_effect=failing_update),
                    patch.object(wayland_window_model, "retain_image_snapshot",
                                 return_value=retained),
                    self.assertRaisesRegex(RuntimeError, failure_property),
                ):
                    window.set_image(incoming)

                self.assertIs(window._gproperties["image"], previous)
                self.assertEqual(window._gproperties["pixel-format"], "BGRA")
                self.assertTrue(previous.has_pixels())
                self.assertTrue(incoming.has_pixels())
                self.assertFalse(retained.has_pixels())
                self.assertEqual(calls, {"previous": 0, "incoming": 0, "retained": 1})
                previous.free()
                incoming.free()
                self.assertEqual(calls, {"previous": 1, "incoming": 1, "retained": 1})

    def test_toplevel_replacement_publishes_format_before_image(self) -> None:
        window = Window({"geometry": (0, 0, 2, 1), "image": None})
        incoming = self.make_image("RGBA")
        updates = []

        def update(name, value):
            window._gproperties[name] = value
            updates.append((name, value))
            if name == "image":
                self.assertIs(window._gproperties["frame-has-alpha"], True)
                self.assertEqual(window._gproperties["pixel-format"], "RGBA")
            return True

        with (
            patch.object(window, "get_internal_property_names",
                         return_value=("pixel-format",)),
            patch.object(window, "_updateprop", side_effect=update),
        ):
            window.set_image(incoming)

        self.assertEqual([name for name, _value in updates], ["frame-has-alpha", "pixel-format", "image"])
        self.assertIs(updates[0][1], True)
        self.assertEqual(updates[1][1], "RGBA")
        self.assertIsNot(updates[2][1], incoming)
        self.assertEqual(updates[2][1].get_pixel_format(), "RGBA")
        window.clear_image()
        incoming.free()

    def test_clear_releases_retained_snapshot_when_pixel_format_notify_fails(self) -> None:
        for model in (
                Window({"geometry": (0, 0, 2, 1), "image": None}),
                SubsurfaceWindow(2, 1),
        ):
            with self.subTest(model=type(model).__name__):
                retained = self.make_image()
                free_calls = []
                image_free = retained.free

                def tracked_free(calls=free_calls, free_image=image_free):
                    calls.append(True)
                    free_image()

                retained.free = tracked_free
                if isinstance(model, Window):
                    model._gproperties["image"] = retained
                else:
                    model._image = retained
                with (
                    patch.object(model, "get_internal_property_names",
                                 return_value=("pixel-format",)),
                    patch.object(model, "_updateprop",
                                 side_effect=RuntimeError("injected clear notification failure")),
                    self.assertRaisesRegex(RuntimeError, "injected clear"),
                ):
                    model.clear_image()
                self.assertEqual(len(free_calls), 1)
                self.assertFalse(retained.has_pixels())
                self.assertFalse(model.has_image())

    def test_snapshot_clear_publishes_pixel_format_once(self) -> None:
        for model in (
                Window({"geometry": (0, 0, 2, 1), "image": None}),
                SubsurfaceWindow(2, 1),
        ):
            with self.subTest(model=type(model).__name__):
                updates = []

                def update(name, value):
                    updates.append((name, value))
                    return True

                with (
                    patch.object(model, "get_internal_property_names",
                                 return_value=("pixel-format",)),
                    patch.object(model, "_updateprop", side_effect=update),
                ):
                    replace_image_snapshot(model, None)

                self.assertEqual(updates, [("pixel-format", ""), ("frame-has-alpha", True)])

    def test_failed_root_replacement_invalidates_model_until_recovery(self) -> None:
        window = Window({"geometry": (0, 0, 2, 1), "image": None})
        server = WindowTopologyHarness()
        server.windows[7] = window
        first = self.make_image()
        server.surface_snapshot(7, first)
        self.assertTrue(first.has_pixels())
        first.free()
        already_presented = window.get_image(0, 0, 2, 1)
        first_pixels = bytes(already_presented.get_pixels())

        # Surface.commit publishes this invalidation before attempting the B
        # readback.  No B image follows when capture fails or the buffer is
        # NULL, so model reads cannot relabel A as the new generation.
        server.surface_snapshot(7, None)
        self.assertIsNone(window.get_image(0, 0, 2, 1))
        self.assertEqual(bytes(already_presented.get_pixels()), first_pixels)

        recovery = ImageWrapper(
            0, 0, 2, 1, bytes((9, 10, 11, 12, 13, 14, 15, 16)),
            "BGRA", 32, 8,
        )
        server.surface_snapshot(7, recovery)
        self.assertTrue(recovery.has_pixels())
        recovery.free()
        self.assertEqual(
            bytes(window.get_image(0, 0, 2, 1).get_pixels()),
            bytes((9, 10, 11, 12, 13, 14, 15, 16)),
        )


def main():
    unittest.main()


if __name__ == "__main__":
    main()
