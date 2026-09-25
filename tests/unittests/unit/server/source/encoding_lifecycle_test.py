#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 kogeler
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import unittest
from contextlib import contextmanager
from threading import Event, Thread
from time import monotonic
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from xpra.server.source import encoding, factory
from xpra.server.source.client_connection import ClientConnection
from xpra.server.source.encoding import EncodingsConnection
from xpra.server.source.mmap import MMAP_Connection
from xpra.server.source.window import WindowsConnection
from xpra.util.objects import typedict


def make_encoding_connection():
    """Use the real muxer lifecycle for every selected connection subsystem."""
    bases = (ClientConnection, MMAP_Connection, WindowsConnection, EncodingsConnection)
    with patch.object(factory, "get_needed_based_classes", return_value=bases):
        connection_class = factory.get_client_connection_class(typedict())
    protocol = SimpleNamespace(
        is_closed=lambda: False,
        _conn=SimpleNamespace(output_bytecount=0),
        source_has_more=Mock(),
        set_packet_source=Mock(),
    )
    server = SimpleNamespace(readonly=False, subsystems={
        "window": SimpleNamespace(
            get_focus=lambda: 0,
            get_window_geometry=lambda window: (0, 0, *window.get_dimensions()),
            window_filters=[],
        ),
        "encoding": SimpleNamespace(
            core_encodings=("rgb24", "rgb32"), encodings=("rgb",),
            default_encoding="", scaling_control=None,
            default_quality=0, default_min_quality=0,
            default_speed=0, default_min_speed=0,
        ),
        "mmap": SimpleNamespace(supported=False, dirs=(), files=(), min_size=0),
    })
    source = connection_class(protocol, Mock(), server, Mock())
    source.parse_hello(typedict({
        "window": {"enabled": True},
        "encoding": {"options": ("rgb",), "core": ("rgb24", "rgb32"),
                     "rgb_formats": ("RGB", "RGBX", "RGBA")},
    }))
    return source


def finish_encoding_connection(source) -> None:
    """Teardown after assertions, including a failed production close."""
    try:
        if not source.is_closed():
            source.close()
    finally:
        if worker := source.encode_thread:
            worker.join(2)
            if worker.is_alive():
                # Do not let a failed control strand a non-daemon worker in
                # Queue.get(). Recovery is after the observation and remains
                # a failure even if the explicit terminal request succeeds.
                source.stop_encode_thread()
                worker.join(2)
                raise AssertionError(f"connection cleanup leaked encode worker: alive={worker.is_alive()}")


class EncodingConnectionLifecycleTest(unittest.TestCase):

    def make_connection(self):
        source = make_encoding_connection()
        self.addCleanup(finish_encoding_connection, source)
        return source

    def close_connection(self, source) -> None:
        source.close()
        if worker := source.encode_thread:
            worker.join(2)
            self.assertFalse(worker.is_alive())
        self.assertTrue(source.encode_work_queue.empty())

    @staticmethod
    def cuda_module(factory_fn):
        module = ModuleType("xpra.codecs.nvidia.cuda.context")
        module.get_device_context = factory_fn
        return patch.dict("sys.modules", {module.__name__: module})

    def test_closed_reinitialization_cannot_publish_policy_or_context(self) -> None:
        source = self.make_connection()
        core, encodings = source.server_core_encodings, source.server_encodings
        self.close_connection(source)
        source.wants_cuda_device = Mock(return_value=True)
        source.allocate_cuda_device_context = Mock()
        source.all_window_sources = Mock(return_value=())
        source.reinit_encodings(SimpleNamespace(core_encodings=("h264",), encodings=("h264",)))
        self.assertEqual(source.server_core_encodings, core)
        self.assertEqual(source.server_encodings, encodings)
        source.wants_cuda_device.assert_not_called()
        source.allocate_cuda_device_context.assert_not_called()
        source.all_window_sources.assert_not_called()

    def test_cuda_propagation_honours_optional_pixel_source_view(self) -> None:
        source = self.make_connection()
        context = SimpleNamespace(free=Mock())
        existing = object()
        # The composed window owner also retires this pixel-source view on close.
        root = SimpleNamespace(cuda_device_context=None, cleanup=Mock())
        child = SimpleNamespace(cuda_device_context=None, cleanup=Mock())
        other = SimpleNamespace(cuda_device_context=existing, cleanup=Mock())
        source.cuda_device_context = context
        source.all_pixel_sources = Mock(return_value=(root, child, other))
        source.reinit_encodings(SimpleNamespace(core_encodings=("rgb32",), encodings=("rgb",)))
        self.assertIs(root.cuda_device_context, context)
        self.assertIs(child.cuda_device_context, context)
        self.assertIs(other.cuda_device_context, existing)
        source.all_pixel_sources.assert_called_once_with()
        self.close_connection(source)
        context.free.assert_called_once_with()

    def test_calculator_uses_exact_optional_source_borrows(self) -> None:
        for rejected in (False, True):
            with self.subTest(rejected=rejected):
                source = self.make_connection()
                held = set()
                used = []
                borrowed = []

                def use(wid, operation):
                    self.assertIn(wid, held, "source use escaped its borrow")
                    used.append((wid, operation))

                def window(wid):
                    return SimpleNamespace(
                        wid=wid, maximized=False, fullscreen=False,
                        statistics=SimpleNamespace(update_averages=lambda: use(wid, "statistics")),
                        calculate_batch_delay=lambda *_args: use(wid, "batch"),
                        reconfigure=lambda: use(wid, "reconfigure"),
                        batch_config=SimpleNamespace(last_updated=0),
                    )

                root, child = window(1), window(2)

                @contextmanager
                def operation(candidate):
                    borrowed.append(candidate.wid)
                    if rejected and candidate is child:
                        yield None
                    else:
                        held.add(candidate.wid)
                        try:
                            yield candidate
                        finally:
                            held.remove(candidate.wid)

                source.window_sources = {1: root}
                source.window_source_items = Mock(return_value=((1, root),))
                source.get_pixel_source = {1: root, 2: child}.get
                source.pixel_source_operation = operation
                source.calculate_window_ids.update((1, 2))
                try:
                    source.recalculate_delays()
                    expected = [(1, name) for name in ("statistics", "batch", "reconfigure")]
                    if not rejected:
                        expected += [(2, name) for name in ("statistics", "batch", "reconfigure")]
                    self.assertCountEqual(used, expected)
                    self.assertEqual(borrowed.count(1), 2)  # per-source and final root-only weight
                    self.assertEqual(borrowed.count(2), 1)
                    self.assertEqual(held, set())
                    self.assertEqual(source.calculate_window_ids, set())
                finally:
                    source.window_sources = {}
                    self.close_connection(source)

    def test_cuda_constructor_losing_to_close_frees_unpublished_context(self) -> None:
        source = self.make_connection()
        entered, release = Event(), Event()
        context = SimpleNamespace(free=Mock())
        results, errors = [], []

        def construct(_options):
            entered.set()
            if not release.wait(2):
                raise AssertionError("CUDA construction was not released")
            return context

        def allocate():
            try:
                results.append(source.allocate_cuda_device_context())
            except BaseException as error:
                errors.append(error)

        with self.cuda_module(construct):
            worker = Thread(target=allocate, daemon=True)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.close_connection(source)
            finally:
                release.set()
                worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(results, [None])
            self.assertIsNone(source.cuda_device_context)
            context.free.assert_called_once_with()

    def test_concurrent_cuda_constructors_publish_one_owner(self) -> None:
        source = self.make_connection()
        entered = (Event(), Event())
        release = (Event(), Event())
        contexts = (SimpleNamespace(free=Mock()), SimpleNamespace(free=Mock()))
        results, errors = {}, []

        def construct(_options):
            index = 1 if entered[0].is_set() else 0
            entered[index].set()
            if not release[index].wait(2):
                raise AssertionError("CUDA construction was not released")
            return contexts[index]

        def allocate(index):
            try:
                results[index] = source.allocate_cuda_device_context()
            except BaseException as error:
                errors.append(error)

        with self.cuda_module(construct):
            workers = [Thread(target=allocate, args=(index,), daemon=True) for index in (0, 1)]
            try:
                workers[0].start()
                self.assertTrue(entered[0].wait(2))
                workers[1].start()
                self.assertTrue(entered[1].wait(2))
                release[0].set()
                workers[0].join(2)
                self.assertFalse(workers[0].is_alive())
                release[1].set()
                workers[1].join(2)
                self.assertFalse(workers[1].is_alive())
                self.assertEqual(errors, [])
                self.assertIs(results[0], contexts[0])
                self.assertIs(results[1], contexts[0])
                self.assertIs(source.cuda_device_context, contexts[0])
                contexts[0].free.assert_not_called()
                contexts[1].free.assert_called_once_with()
            finally:
                for event in release:
                    event.set()
                for worker in workers:
                    if worker.ident is not None:
                        worker.join(2)
                self.close_connection(source)
            contexts[0].free.assert_called_once_with()
            contexts[1].free.assert_called_once_with()

    def test_calculation_source_removal_error_does_not_skip_cuda_tail(self) -> None:
        source = self.make_connection()
        context = SimpleNamespace(free=Mock())
        source.cuda_device_context = context
        source.cancel_recalculate_timer = Mock(side_effect=RuntimeError("calculate source removal failed"))
        with self.assertRaisesRegex(RuntimeError, "calculate source removal failed"):
            source.close()
        self.assertIsNotNone(source.encode_thread, "cleanup error bypassed the mandatory encode tail")
        source.encode_thread.join(2)
        self.assertFalse(source.encode_thread.is_alive())
        self.assertTrue(source.encode_work_queue.empty())
        self.assertIsNone(source.cuda_device_context)
        context.free.assert_called_once_with()

    def test_closed_connection_does_not_schedule_calculation(self) -> None:
        source = self.make_connection()
        self.close_connection(source)
        with patch.object(encoding, "add_work_item") as add_work_item:
            source.may_recalculate(1, encoding.MIN_PIXEL_RECALCULATE)
        add_work_item.assert_not_called()
        self.assertEqual(source.calculate_window_ids, set())
        self.assertEqual(source.calculate_window_pixels, {})

    def test_calculation_timeout_releases_source_before_background_handoff(self) -> None:
        source = self.make_connection()
        context = encoding.GLib.MainContext.default()
        source.calculate_last_time = monotonic()
        with patch.object(encoding, "add_work_item", return_value=None) as add_work_item:
            source.may_recalculate(1, encoding.MIN_PIXEL_RECALCULATE)
            timer = source.calculate_timer
            if isinstance(timer, int):
                timer = context.find_source_by_id(timer)
            self.assertIsNotNone(timer)
            timer.set_ready_time(0)
            try:
                deadline = monotonic() + 1
                while not add_work_item.called and monotonic() < deadline:
                    context.iteration(False)
                add_work_item.assert_called_once()
                self.assertTrue(timer.is_destroyed())
                self.assertFalse(source.calculate_timer)
                self.close_connection(source)
                with patch.object(source.statistics, "update_averages") as update_averages:
                    add_work_item.call_args.args[0]()
                update_averages.assert_not_called()
            finally:
                timer.destroy()
                self.close_connection(source)

    def test_failed_background_admission_releases_ids_and_invalidates_late_work(self) -> None:
        for queued_before_error in (False, True):
            with self.subTest(queued_before_error=queued_before_error):
                source = self.make_connection()
                work = []

                def refuse(callback):
                    if queued_before_error:
                        work.append(callback)
                    raise RuntimeError("background admission failed")

                try:
                    with patch.object(encoding, "monotonic", return_value=100), \
                            patch.object(encoding, "add_work_item", side_effect=refuse):
                        with self.assertRaisesRegex(RuntimeError, "background admission failed"):
                            source.may_recalculate(1, encoding.MIN_PIXEL_RECALCULATE)
                    self.assertEqual(source.calculate_window_ids, set())
                    self.assertEqual(source.calculate_window_pixels[1], encoding.MIN_PIXEL_RECALCULATE)

                    with patch.object(encoding, "monotonic", return_value=100), \
                            patch.object(encoding, "add_work_item", side_effect=work.append) as submit:
                        source.may_recalculate(1, 1)
                    submit.assert_called_once()
                    with patch.object(source.statistics, "update_averages") as average, \
                            patch.object(encoding, "may_update_bandwidth_limits"):
                        for stale in work[:-1]:
                            stale()
                        average.assert_not_called()
                        self.assertEqual(source.calculate_window_ids, {1})
                        work[-1]()
                        average.assert_called_once()
                    self.assertEqual(source.calculate_window_ids, set())
                finally:
                    self.close_connection(source)

    def test_failed_calculation_source_setup_releases_reservation(self) -> None:
        for phase in ("construct", "callback", "attach"):
            with self.subTest(phase=phase):
                source = self.make_connection()
                source.calculate_last_time = 100
                timer = SimpleNamespace(set_callback=Mock(), attach=Mock(), destroy=Mock())
                create = Mock(return_value=timer)
                failure = RuntimeError("calculate source setup failed")
                if phase == "construct":
                    create.side_effect = failure
                elif phase == "callback":
                    timer.set_callback.side_effect = failure
                else:
                    timer.attach.side_effect = failure
                try:
                    with patch.object(encoding, "monotonic", return_value=100), \
                            patch.object(encoding.GLib, "timeout_source_new", create):
                        with self.assertRaisesRegex(RuntimeError, "calculate source setup failed"):
                            source.may_recalculate(1, encoding.MIN_PIXEL_RECALCULATE)
                    self.assertFalse(source.calculate_timer)
                    self.assertEqual(source.calculate_window_ids, set())
                    self.assertEqual(source.calculate_window_pixels[1], encoding.MIN_PIXEL_RECALCULATE)
                    if phase != "construct":
                        timer.destroy.assert_called_once_with()
                    with patch.object(encoding, "monotonic", return_value=102), \
                            patch.object(encoding, "add_work_item") as submit:
                        source.may_recalculate(1, 1)
                    submit.assert_called_once()
                finally:
                    self.close_connection(source)

    def test_failed_timeout_handoff_releases_all_coalesced_ids(self) -> None:
        source = self.make_connection()
        source.calculate_last_time = 100
        timer = SimpleNamespace(set_callback=Mock(), attach=Mock(), destroy=Mock())
        work = []

        def refuse(callback):
            work.append(callback)
            raise RuntimeError("timed background admission failed")

        try:
            with patch.object(encoding, "monotonic", return_value=100), \
                    patch.object(encoding.GLib, "timeout_source_new", return_value=timer):
                source.may_recalculate(1, encoding.MIN_PIXEL_RECALCULATE)
                source.may_recalculate(2, encoding.MIN_PIXEL_RECALCULATE)
            timer.attach.assert_called_once()
            fire = timer.set_callback.call_args.args[0]
            with patch.object(encoding, "add_work_item", side_effect=refuse):
                with self.assertRaisesRegex(RuntimeError, "timed background admission failed"):
                    fire()
            self.assertFalse(source.calculate_timer)
            self.assertEqual(source.calculate_window_ids, set())
            self.assertEqual(set(source.calculate_window_pixels), {1, 2})
            with patch.object(encoding, "monotonic", return_value=102), \
                    patch.object(encoding, "add_work_item", side_effect=work.append) as submit:
                source.may_recalculate(2, 1)
                self.assertFalse(fire(), "stale timeout must not revoke a replacement")
            submit.assert_called_once()
            with patch.object(source.statistics, "update_averages") as average, \
                    patch.object(encoding, "may_update_bandwidth_limits"):
                work[0]()
                average.assert_not_called()
                self.assertEqual(source.calculate_window_ids, {2})
                work[1]()
                average.assert_called_once()
        finally:
            self.close_connection(source)

    def test_failed_successor_admission_does_not_erase_active_calculation_batch(self) -> None:
        source = self.make_connection()
        # This implementation-specific control complements the public clean
        # failure above: the executing batch is no longer in the pending set.
        batches, work = [], []

        def calculate(wids):
            with patch.object(encoding, "add_work_item", side_effect=RuntimeError("successor failed")):
                with self.assertRaisesRegex(RuntimeError, "successor failed"):
                    source.may_recalculate(2, encoding.MIN_PIXEL_RECALCULATE)
            batches.append(tuple(wids))

        try:
            with patch.object(encoding, "monotonic", return_value=100), \
                    patch.object(encoding, "add_work_item", side_effect=work.append):
                source.may_recalculate(1, encoding.MIN_PIXEL_RECALCULATE)
                with patch.object(source, "_recalculate_delays", side_effect=calculate):
                    work[0]()
            self.assertEqual(batches, [(1,)])
            self.assertEqual(source.calculate_window_ids, set())
            self.assertEqual(source.calculate_window_pixels, {2: encoding.MIN_PIXEL_RECALCULATE})
        finally:
            self.close_connection(source)

    def test_connection_close_waits_for_active_background_calculation(self) -> None:
        source = self.make_connection()
        entered, release, closing, closed = Event(), Event(), Event(), Event()
        retired, errors, used_after_retirement = Event(), [], []
        from xpra.codecs.loader import load_codec
        self.assertIsNotNone(load_codec("enc_rgb"))
        properties = {"depth": 24, "content-types": (), "fullscreen": False}
        window = SimpleNamespace(
            get_dimensions=lambda: (64, 64),
            get_dynamic_property_names=lambda: (),
            get_internal_property_names=lambda: (),
            get=properties.get, get_property=properties.__getitem__,
            is_OR=lambda: False, is_tray=lambda: False,
            is_shadow=lambda: False, has_alpha=lambda: False,
            is_managed=lambda: True, disconnect=Mock(),
        )
        # The connection creates, initializes and publishes the real source;
        # downstream source-lifetime owners participate through this same API.
        ws = source.make_window_source(1, window)

        def update_averages():
            entered.set()
            if not release.wait(2):
                raise AssertionError("background calculation was not released")

        def record_use(*_args):
            if retired.is_set():
                used_after_retirement.append(True)

        ws.statistics.update_averages = update_averages
        ws.calculate_batch_delay = record_use
        ws.reconfigure = record_use

        ui_cleanup = ws.ui_cleanup

        def retire():
            ui_cleanup()
            retired.set()

        ws.ui_cleanup = retire
        source.calculate_window_ids.add(ws.wid)

        def calculate():
            try:
                source.recalculate_delays()
            except BaseException as error:
                errors.append(error)

        def close():
            closing.set()
            try:
                self.close_connection(source)
            except BaseException as error:
                errors.append(error)
            finally:
                closed.set()

        calculate_thread = Thread(target=calculate, daemon=True)
        close_thread = Thread(target=close, daemon=True)
        calculate_thread.start()
        try:
            self.assertTrue(entered.wait(2))
            close_thread.start()
            self.assertTrue(closing.wait(2))
            self.assertFalse(closed.wait(0.1), "connection retired while its calculator still owned a window")
            self.assertFalse(retired.is_set())
        finally:
            release.set()
            calculate_thread.join(2)
            if close_thread.ident is not None:
                close_thread.join(2)
        self.assertFalse(calculate_thread.is_alive())
        self.assertFalse(close_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(used_after_retirement, [])
        context = encoding.GLib.MainContext.default()
        deadline = monotonic() + 1
        while not retired.is_set() and monotonic() < deadline:
            context.iteration(False)
        self.assertTrue(retired.is_set())


def load_tests(_loader, tests, _pattern):
    # Clean source must expose policy publication after actual connection
    # close, before implementation-specific calculator/borrow controls.
    control = EncodingConnectionLifecycleTest("test_closed_reinitialization_cannot_publish_policy_or_context")
    ordered = unittest.TestSuite([control])
    for group in tests:
        ordered.addTests(test for test in group if test.id() != control.id())
    return ordered


if __name__ == "__main__":
    unittest.main(failfast=True)
