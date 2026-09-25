# This file is part of Xpra.
# Copyright (C) 2010 Antoine Martin <antoine@xpra.org>
# Copyright (C) 2008 Nathaniel Smith <njs@pobox.com>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import os
from threading import Condition, RLock
from time import monotonic
from typing import Any
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager

from xpra.net.packet_type import (
    WINDOW_RESTACK, WINDOW_RAISE, WINDOW_INITIATE_MOVERESIZE, WINDOW_DESTROY, WINDOW_RESIZED,
    WINDOW_MOVE_RESIZE, WINDOW_METADATA, WINDOW_BELL, WINDOW_GRAB, WINDOW_UNGRAB, WINDOW_STACKING,
)
from xpra.os_util import gi_import
from xpra.server.common import may_update_bandwidth_limits, wants_windows
from xpra.server.source.stub import StubClientConnection, is_recording_allowed, is_sync_allowed
from xpra.server.source.queued_packet import queued_draw_packet
from xpra.server.window.compress import free_image_wrapper
from xpra.server.window.metadata import make_window_metadata
from xpra.server.window.filters import get_window_filter
from xpra.util.objects import typedict
from xpra.util.env import envint
from xpra.common import (
    SUBSURFACE_COMPOSITE_FORMATS, SUBSURFACE_COMPOSITE_MODE,
    may_notify_client, force_size_constraint,
)
from xpra.net.common import BACKWARDS_COMPATIBLE, Packet
from xpra.constants import NotificationID, DEFAULT_METADATA_SUPPORTED
from xpra.log import Logger

GLib = gi_import("GLib")

log = Logger("server")
damagelog = Logger("window", "damage")
focuslog = Logger("focus")
metalog = Logger("metadata")
bandwidthlog = Logger("bandwidth")
eventslog = Logger("events")
filterslog = Logger("filter")
sharinglog = Logger("sharing")

# the metadata we override for windows that are outside a client's display area
# when using `sharing=combine`: the client keeps the window, but does not show it
HIDDEN_METADATA: Sequence[str] = ("iconic", "skip-taskbar", "skip-pager")
# A subsurface is painted into its parent's existing client backing.  Only
# propagate the decoder capabilities of that backing: parent geometry and
# presentation state do not describe the child coordinate space.
SUBSURFACE_BACKING_PROPERTIES: Sequence[str] = (
    "bit-depth",
    "decoder-speed",
    "encoding.full_csc_modes",
    "encoding.full_frames_only",
    "encoding.transparency",
    "encodings",
    "encodings.core",
    "encodings.rgb_formats",
)

CONGESTION_WARNING_EVENT_COUNT = envint("XPRA_CONGESTION_WARNING_EVENT_COUNT", 10)
CONGESTION_REPEAT_DELAY = envint("XPRA_CONGESTION_REPEAT_DELAY", 60)
MIN_BANDWIDTH = envint("XPRA_MIN_BANDWIDTH", 5 * 1024 * 1024)
SUBSURFACE_COMPOSITE_STAGE_TIMEOUT = max(
    1000, envint("XPRA_SUBSURFACE_COMPOSITE_STAGE_TIMEOUT", 15000),
)

PROPERTIES_DEBUG = [x.strip() for x in os.environ.get("XPRA_WINDOW_PROPERTIES_DEBUG", "").split(",")]


def noid(_window) -> int:
    return 0


class WindowsConnection(StubClientConnection):
    """
    Handle window forwarding:
    - damage
    - geometry
    - events
    etc
    """
    PREFIX = "window"
    __signals__ = ["new-window-source", "remove-window-source", "suspend", "resume"]

    @classmethod
    def is_needed(cls, caps: typedict) -> bool:
        return wants_windows(caps)

    def __init__(self):
        super().__init__()
        self.get_focus: Callable | None = None
        self.get_server_geometry: Callable | None = None
        self.window_filters = []
        self.readonly = False
        # duplicated from encodings:
        self.global_batch_config = None
        # duplicated from clientconnection:
        self.statistics = None
        self.connect("suspend", self.suspend_window_sources)
        self.connect("resume", self.resume_window_sources)

    def init_from(self, _protocol, server) -> None:
        # `WindowServer` is the standalone subsystem instance:
        window = server.subsystems["window"]
        self.get_focus = window.get_focus
        self.get_server_geometry = window.get_window_geometry
        self.window_filters = window.window_filters
        self.readonly = server.readonly

    def init_state(self) -> None:
        # WindowSource for each Window ID
        self.window_sources: dict[int, Any] = {}
        # Per-subsurface WindowSource, kept separate so they're not treated
        # as toplevels by bandwidth/cancel/info logic.
        self.subsurface_sources: dict[int, Any] = {}
        self.window_backing_properties: dict[int, dict] = {}
        self.subsurface_stacking: dict[int, tuple[int, ...]] = {}
        self._subsurface_composites: dict[int, dict[str, Any]] = {}
        self._subsurface_topology_epochs: dict[int, int] = {}
        self._subsurface_transaction_id = 0
        self._subsurface_transaction_ids: dict[int, int] = {}
        self._subsurface_glib_sources: dict[object, dict[str, Any]] = {}
        self.subsurface_composite_modes: tuple[str, ...] = ()
        self.window_frame_sizes: dict = {}
        self.window_bell = False
        self.window_enabled = True
        self.window_min_size: tuple[int, int] = (0, 0)
        self.window_max_size: tuple[int, int] = (0, 0)
        self.window_restack = False
        # `sharing=sync`: move and resize the windows
        # when another client modifies their geometry
        self.window_sync_position = False
        # `sharing=sync`: raise the windows focused by another client
        self.window_sync_focus = False
        # receive the complete stacking order reported by another client
        self.window_sync_stacking = False
        self.window_metadata_supported: Sequence[str] = ()
        self.window_grabs = False
        self.system_tray = False
        # for handling resize synchronization between client and server (this is not xsync!):
        self.window_configure_time = 0.0
        self.window_record = False
        # `sharing=combine`: the windows that fall outside this client's display area,
        # which are sent to it but not shown (see `HIDDEN_METADATA`)
        self.hidden_windows: set = set()
        # Draw packets from ordinary windows and parent-retargeted subsurfaces
        # share one wire sequence space.  The registry binds each ACK to the
        # exact source which produced it.
        self._damage_packet_sequence = 1
        self._damage_packet_owners: dict[tuple[int, int], Any] = {}
        self._damage_packet_sources: dict[int, Any] = {}
        self._damage_packet_publication_leases: dict[object, Any] = {}
        self._damage_packet_active_ops: dict[int, int] = {}
        self._backing_epochs: dict[int, int] = {}
        # Last exact wire canvas size sent to this client for each toplevel.
        # It is independent of renderer-only ``render-size`` scaling.
        self._client_backing_sizes: dict[int, tuple[int, int]] = {}
        # These registries bind client-visible lifecycle state to the exact
        # model object, not just its reusable wire ID.  A refusal is local to
        # this connection and may be lifted without changing other clients.
        self._window_models: dict[int, Any] = {}
        self._announced_windows: dict[int, Any] = {}
        self._refused_windows: dict[int, tuple[Any, str]] = {}
        self._window_detaching: dict[int, Any] = {}
        self._damage_packet_closing = False
        self._damage_packet_lock = RLock()
        self._damage_packet_condition = Condition(self._damage_packet_lock)

    def cleanup(self) -> None:
        with self._damage_packet_condition:
            window_sources = self.all_pixel_sources()
            snapshot_images = tuple(
                image
                for state in self._subsurface_composites.values()
                for image in self._take_subsurface_snapshot_images_locked(state)
            )
            self._damage_packet_closing = True
            self.window_sources = {}
            self.subsurface_sources = {}
            self.window_backing_properties = {}
            self.subsurface_stacking = {}
            self._subsurface_composites = {}
            self._subsurface_topology_epochs = {}
            self._subsurface_transaction_ids = {}
            self._damage_packet_sources.clear()
            while self._damage_packet_active_ops:
                self._damage_packet_condition.wait()
            self._damage_packet_owners.clear()
            self._damage_packet_publication_leases.clear()
            self._backing_epochs.clear()
            self._client_backing_sizes.clear()
            self._window_models.clear()
            self._announced_windows.clear()
            self._refused_windows.clear()
            self._window_detaching.clear()
            self._damage_packet_condition.notify_all()
        self._free_subsurface_snapshot_images(snapshot_images)
        self._cancel_subsurface_glib_sources()
        cleanup_errors: list[tuple[BaseException, Any]] = []
        for window_source in window_sources:
            try:
                window_source.cleanup()
            except BaseException as e:
                cleanup_errors.append((e, e.__traceback__))
        self.hidden_windows = set()
        self._raise_pixel_cleanup_errors("connection window-source cleanup", cleanup_errors)

    def all_window_sources(self) -> tuple:
        # This is the client-visible toplevel set used by reporting and
        # bandwidth allocation.  Internal subsurface encoders are deliberately
        # excluded.
        return tuple(self.window_sources.values())

    def all_pixel_sources(self) -> tuple:
        # Configuration and lifecycle operations must reach every source which
        # can publish pixels into a client backing.
        with self._damage_packet_lock:
            return tuple(self.window_sources.values()) + tuple(self.subsurface_sources.values())

    def get_pixel_source(self, wid: int):
        with self._damage_packet_lock:
            source = self.window_sources.get(wid)
            if source is None:
                source = self.subsurface_sources.get(wid)
            return source

    def window_source_items(self) -> tuple:
        with self._damage_packet_lock:
            return tuple(self.window_sources.items())

    @contextmanager
    def pixel_source_operation(self, source):
        """Borrow one exact current source across a worker-thread callout.

        This shares publication/ACK lifetime ownership. A borrowed callout
        must not synchronously remove its own source: unregister waits for
        these operations before source statistics or encoders may be cleaned.
        """
        owner = None
        with self._damage_packet_condition:
            if source is not None and not self._damage_packet_closing:
                current = self.window_sources.get(source.wid)
                if current is None:
                    current = self.subsurface_sources.get(source.wid)
                if current is source and self._damage_packet_sources.get(id(source)) is source:
                    owner = source
                    self._begin_damage_packet_operation_locked(owner)
        try:
            yield owner
        finally:
            if owner is not None:
                with self._damage_packet_condition:
                    self._finish_damage_packet_operation_locked(owner)

    def get_window_pixel_sources(self, wid: int) -> tuple:
        with self._damage_packet_lock:
            sources = []
            if ws := self.window_sources.get(wid):
                sources.append(ws)
            sources += [ws for sub_wid, ws in self.subsurface_sources.items()
                        if sub_wid == wid or ws.parent_wid == wid]
            return tuple(sources)

    def get_subsurface_sources(self, parent_wid: int) -> tuple:
        with self._damage_packet_lock:
            return tuple(
                ws for wid in self.subsurface_stacking.get(parent_wid, ())
                if wid != parent_wid
                and (ws := self.subsurface_sources.get(wid)) is not None
                and ws.parent_wid == parent_wid
            )

    def subsurface_composite_eligibility(self, parent_wid: int) -> str:
        """Return an exact per-connection reason why this root cannot composite."""
        if not self.supports_subsurface_composite():
            return f"the client did not negotiate {SUBSURFACE_COMPOSITE_MODE}"
        with self._damage_packet_lock:
            parent = self.window_sources.get(parent_wid)
            children = tuple(
                source for source in self.subsurface_sources.values()
                if source.parent_wid == parent_wid
            )
        if parent is None:
            return "the parent pixel source is unavailable"
        for source in (parent, *children):
            try:
                _formats, error = source.subsurface_composite_eligibility()
            except BaseException:
                log.error(
                    "Error validating composite eligibility for source %#x",
                    source.wid, exc_info=True,
                )
                return f"source {source.wid:#x} composite eligibility failed"
            if error:
                return f"source {source.wid:#x}: {error}"
        return ""

    def _refuse_ineligible_subsurface_composite(self, parent_wid: int, reason: str) -> bool:
        """Withdraw an announced root whose composite wire contract is no longer valid."""
        with self._damage_packet_lock:
            parent = self.window_sources.get(parent_wid)
        if parent is None:
            return False
        log.warn(
            "Warning: refusing composite root %#x for this client: %s",
            parent_wid, reason,
        )
        return self.refuse_window(parent_wid, parent.window, reason)

    def allocate_damage_packet_sequence(self) -> int:
        with self._damage_packet_lock:
            sequence = self._damage_packet_sequence
            self._damage_packet_sequence += 1
        return sequence

    def claim_damage_packet_publication(self, source) -> object | None:
        with self._damage_packet_condition:
            if self._damage_packet_closing or self._damage_packet_sources.get(id(source)) is not source:
                return None
            lease = object()
            self._damage_packet_publication_leases[lease] = source
            self._begin_damage_packet_operation_locked(source)
            return lease

    def release_damage_packet_publication(self, source, lease: object) -> None:
        with self._damage_packet_condition:
            if self._damage_packet_publication_leases.get(lease) is source:
                self._damage_packet_publication_leases.pop(lease, None)
                self._finish_damage_packet_operation_locked(source)

    def _begin_damage_packet_operation_locked(self, source) -> None:
        source_id = id(source)
        self._damage_packet_active_ops[source_id] = self._damage_packet_active_ops.get(source_id, 0) + 1

    def _finish_damage_packet_operation_locked(self, source) -> None:
        source_id = id(source)
        count = self._damage_packet_active_ops.get(source_id, 0)
        if count <= 1:
            self._damage_packet_active_ops.pop(source_id, None)
        else:
            self._damage_packet_active_ops[source_id] = count - 1
        self._damage_packet_condition.notify_all()

    def publish_damage_packet(self, wire_wid: int, sequence: int, source,
                              publisher: Callable[[], None],
                              terminal_publisher: Callable[[], None] | None = None,
                              publication_lease: object | None = None,
                              validator: Callable[[], bool] | None = None) -> bool:
        callback = None
        owns_operation = False
        owner_key = None
        with self._damage_packet_condition:
            lease_owner = None
            if publication_lease is not None:
                lease_owner = self._damage_packet_publication_leases.get(publication_lease)
                owns_operation = lease_owner is source
                if owns_operation:
                    self._damage_packet_publication_leases.pop(publication_lease, None)
            if self._damage_packet_closing:
                if owns_operation:
                    self._finish_damage_packet_operation_locked(source)
                return False
            try:
                valid = not validator or validator()
            except BaseException:
                if owns_operation:
                    # The caller still owns an already-written mmap marker.
                    # Restore the exact lease so its exception path can drain
                    # the chunks while unregister remains fenced behind the
                    # same active operation.
                    self._damage_packet_publication_leases[publication_lease] = source
                    owns_operation = False
                raise
            active = self._damage_packet_sources.get(id(source)) is source
            if not valid or not active:
                # A completed mmap encode owns space in the connection's
                # shared ring.  Even after its window source is detached, its
                # descriptor must reach the live client so the read pointer
                # advances.  This terminal path records no source ACK state
                # and cannot be used by ordinary late packets.
                if terminal_publisher and owns_operation:
                    callback = terminal_publisher
                else:
                    if owns_operation:
                        self._finish_damage_packet_operation_locked(source)
                    return False
            else:
                if publication_lease is not None and not owns_operation:
                    return False
                if not owns_operation:
                    self._begin_damage_packet_operation_locked(source)
                    owns_operation = True
                owner_key = wire_wid, sequence
                owner = self._damage_packet_owners.get(owner_key)
                if owner is not None:
                    log.error("Error: duplicate damage packet owner for window %#x sequence %s", wire_wid, sequence)
                    if terminal_publisher and publication_lease is not None:
                        # A global sequence collision must never strand mmap
                        # chunks.  Drain without painting or replacing the
                        # existing ACK owner.
                        owner_key = None
                        callback = terminal_publisher
                    else:
                        self._finish_damage_packet_operation_locked(source)
                        return False
                else:
                    self._damage_packet_owners[owner_key] = source
                    callback = publisher
        try:
            callback()
        except BaseException:
            with self._damage_packet_condition:
                if owner_key:
                    if self._damage_packet_owners.get(owner_key) is source:
                        self._damage_packet_owners.pop(owner_key, None)
                # A publication callback can fail before the outbound deque
                # accepts the packet.  Preserve an mmap lease so its caller
                # can still publish the already-written chunks through the
                # terminal no-paint path while detach remains fenced behind
                # this active operation.
                if publication_lease is not None and owns_operation and not self._damage_packet_closing:
                    self._damage_packet_publication_leases[publication_lease] = source
                    owns_operation = False
            raise
        finally:
            if owns_operation:
                with self._damage_packet_condition:
                    self._finish_damage_packet_operation_locked(source)
        # A terminal mmap drain is deliberately not a publication into the
        # target backing and therefore cannot complete a composition stage.
        return owner_key is not None

    def unregister_damage_packets(self, source) -> None:
        with self._damage_packet_condition:
            self._damage_packet_sources.pop(id(source), None)
            while self._damage_packet_active_ops.get(id(source), 0):
                self._damage_packet_condition.wait()
            self._damage_packet_owners = {
                key: owner for key, owner in self._damage_packet_owners.items() if owner is not source
            }
            self._damage_packet_publication_leases = {
                lease: owner for lease, owner in self._damage_packet_publication_leases.items()
                if owner is not source
            }
            # No acknowledgement can be routed to a detached source after the
            # owner registry above has been cleared.  Remove its corresponding
            # congestion/accounting state before cleanup can tear statistics
            # down.  This is deliberately best-effort and non-throwing because
            # the registry transition is authoritative.
            try:
                pending = getattr(getattr(source, "statistics", None), "damage_ack_pending", None)
                if pending is not None:
                    pending.clear()
            except BaseException:
                log.error("Error clearing detached damage acknowledgement state", exc_info=True)

    def filter_queued_damage_packet(self, queued_packet):
        """Drop a draw which no longer belongs to its target backing.

        Ordinary packets have already been given priority by ``next_packet``.
        Revalidation here closes the remaining destroy/re-create race: encoded
        pixels can spend an arbitrary amount of time in ``packet_queue`` after
        their source or backing epoch has been replaced.  An mmap descriptor
        cannot simply be discarded because the peer must advance its shared
        ring read pointer, so stale mmap draws are retargeted to the client's
        unknown-window drain path instead.
        """
        packet, source_wid, pixels, wait_for_more = queued_packet
        try:
            draw_packet = queued_draw_packet(packet)
            if draw_packet is None:
                return queued_packet
            if draw_packet is not packet:
                packet = draw_packet
                queued_packet = packet, source_wid, pixels, wait_for_more
            wire_wid = packet.get_wid()
            coding = packet.get_str(6)
            sequence = packet.get_u64(8)
        except BaseException:
            log.error("Error validating queued draw packet", exc_info=True)
            return None
        if wire_wid == -1 and coding == "mmap":
            return queued_packet
        with self._damage_packet_condition:
            source = self._damage_packet_owners.get((wire_wid, sequence))
            try:
                # Publication already proved that this exact owner and packet
                # belonged to the then-current composite transaction.  A
                # later transaction on the same unchanged backing may start
                # before the network thread dequeues this packet; that must
                # not invalidate an already committed transaction.  The
                # exact source registry, refusal state and backing epoch still
                # reject packets whose client backing no longer exists;
                # topology repairs remain ordered behind committed stages.
                valid = source is not None and self.validate_damage_packet(
                    source, packet, committed=True,
                )
            except BaseException:
                log.error("Error revalidating queued draw packet", exc_info=True)
                valid = False
            if valid:
                return queued_packet
            if source is not None and self._damage_packet_owners.get((wire_wid, sequence)) is source:
                self._damage_packet_owners.pop((wire_wid, sequence), None)
                try:
                    pending = getattr(getattr(source, "statistics", None), "damage_ack_pending", None)
                    if pending is not None:
                        pending.pop(sequence, None)
                except BaseException:
                    log.error("Error clearing stale damage acknowledgement state", exc_info=True)
        if coding != "mmap":
            return None
        drain_packet = Packet(packet.get_type(), -1, *packet[2:])
        # A terminal no-paint packet cannot promise the cancelled batch's
        # successor. ``next_packet`` will set ``more`` again when another
        # packet is actually queued.
        return drain_packet, 0, 0, False

    def configure_pixel_source(self, source) -> None:
        with self._damage_packet_lock:
            self._damage_packet_sources[id(source)] = source
            source.allocate_damage_packet_sequence = self.allocate_damage_packet_sequence
            source.defer_damage = self.defer_pixel_damage
            source.snapshot_damage_options = self.snapshot_damage_options
            source.validate_damage_packet = self.validate_damage_packet
            source.claim_damage_packet_publication = self.claim_damage_packet_publication
            source.release_damage_packet_publication = self.release_damage_packet_publication
            source.publish_damage_packet = self.publish_damage_packet
            source.unregister_damage_packets = self.unregister_damage_packets

    def snapshot_damage_options(self, source, options: Mapping[str, Any]) -> dict:
        result = dict(options)
        with self._damage_packet_lock:
            if self.subsurface_sources.get(source.wid) is source:
                wire_wid = source.parent_wid
            else:
                wire_wid = source.wid
            epoch = self._backing_epochs.get(wire_wid, 0)
            if result.get("subsurface-composite") == SUBSURFACE_COMPOSITE_MODE:
                result.setdefault("subsurface-backing-epoch", epoch)
            else:
                result.setdefault("backing-epoch", epoch)
        return result

    def validate_damage_packet(self, _source, packet, *, committed: bool = False) -> bool:
        source = _source
        wire_wid = packet.get_wid()
        client_options = packet.get_dict(10)
        mode = client_options.get("subsurface-composite")
        epoch_key = "subsurface-backing-epoch" if mode == SUBSURFACE_COMPOSITE_MODE else "backing-epoch"
        packet_epoch = client_options.get(epoch_key, -1 if mode == SUBSURFACE_COMPOSITE_MODE else 0)
        with self._damage_packet_lock:
            if self._damage_packet_sources.get(id(source)) is not source:
                return False
            if wire_wid in self._refused_windows:
                return False
            if self.window_sources.get(source.wid) is source:
                if source.wid != wire_wid:
                    return False
            elif self.subsurface_sources.get(source.wid) is source:
                # A committed child packet retains its snapshotted wire
                # parent even if the live source has since been reparented.
                # It precedes the replacement topology transaction in the
                # ordered pixel queue.  Strict publication never gets this
                # exception.
                if not committed and getattr(source, "parent_wid", wire_wid) != wire_wid:
                    return False
            else:
                return False
            if packet_epoch != self._backing_epochs.get(wire_wid, 0):
                return False
            active_composite = wire_wid in self._subsurface_composites or bool(
                self.get_subsurface_sources(wire_wid)
            )
            if active_composite and mode != SUBSURFACE_COMPOSITE_MODE:
                return False
            if mode == SUBSURFACE_COMPOSITE_MODE:
                if self.subsurface_composite_eligibility(wire_wid):
                    return False
                _rgb_formats, source_error = source.subsurface_composite_eligibility()
                pixel_format = client_options.get("rgb_format")
                if (
                    source_error
                    or packet.get_str(6) != "rgb32"
                    or pixel_format not in SUBSURFACE_COMPOSITE_FORMATS
                    or client_options.get("lz4", 0)
                    or client_options.get("zstd", 0)
                ):
                    return False
                topology_epoch = client_options.get("subsurface-topology-epoch", -1)
                transaction_id = client_options.get("subsurface-transaction-id", 0)
                stage_index = client_options.get("subsurface-stage-index", -1)
                stage_count = client_options.get("subsurface-stage-count", 0)
                state = self._subsurface_composites.get(wire_wid)
                current_transaction = committed or bool(
                    state
                    and state.get("running")
                    and state.get("transaction_id") == transaction_id
                    and state.get("snapshots_ready")
                )
                return (
                    type(packet_epoch) is int and packet_epoch >= 0
                    and type(topology_epoch) is int and topology_epoch >= 0
                    and type(transaction_id) is int and transaction_id > 0
                    and type(stage_index) is int and stage_index >= 0
                    and type(stage_count) is int and stage_count > stage_index
                    and (
                        committed
                        or topology_epoch == self._subsurface_topology_epochs.get(wire_wid, 0)
                    )
                    and (
                        committed
                        or transaction_id == self._subsurface_transaction_ids.get(wire_wid, 0)
                    )
                    and current_transaction
                )
            return True

    def defer_pixel_damage(self, source, x: int = 0, y: int = 0,
                           width: int = 0, height: int = 0) -> bool:
        with self._damage_packet_lock:
            if self._damage_packet_closing or self._damage_packet_sources.get(id(source)) is not source:
                return True
            if self.window_sources.get(source.wid) is source:
                parent_wid = source.wid
                parent_region = (x, y, width, height)
                internal_subsurface = False
            else:
                parent_wid = source.parent_wid
                parent_region = (
                    source.offset_x + x, source.offset_y + y,
                    width, height,
                )
                internal_subsurface = self.subsurface_sources.get(source.wid) is source
            composite_owned = (
                internal_subsurface
                or parent_wid in self._subsurface_composites
                or bool(self.get_subsurface_sources(parent_wid))
            )
        if not composite_owned:
            return False
        self.request_subsurface_composite(parent_wid, parent_region=parent_region)
        return True

    @staticmethod
    def _raise_pixel_cleanup_errors(scope: str, errors: list[tuple[BaseException, Any]]) -> None:
        if not errors:
            return
        for later_error, later_traceback in errors[1:]:
            log.error("Additional error during %s: %s", scope, later_error,
                      exc_info=(type(later_error), later_error, later_traceback))
        error, traceback = errors[0]
        raise error.with_traceback(traceback)

    def cleanup_video_encoders(self) -> None:
        # Only toplevel WindowVideoSource instances own video pipelines.
        # Internal subsurface sources deliberately inherit WindowSource and
        # participate in generic lifecycle fanout without exposing a dummy
        # video cleanup API.
        for ws in self.all_window_sources():
            ws.video_context_clean()

    def suspend_window_sources(self) -> None:
        for ws in self.all_pixel_sources():
            ws.suspend()

    def resume_window_sources(self) -> None:
        for ws in self.all_pixel_sources():
            ws.resume()

    def go_idle(self) -> None:
        # usually fires from the server's idle_grace_timeout_cb
        if self.idle:
            return
        self.idle = True
        for window_source in self.all_pixel_sources():
            window_source.go_idle()

    def no_idle(self) -> None:
        # on user event, we stop being idle
        if not self.idle:
            return
        self.idle = False
        for window_source in self.all_pixel_sources():
            window_source.no_idle()

    def requires_sharing(self) -> bool:
        return not self.window_record

    def parse_client_caps(self, c: typedict) -> None:
        ui_client = c.boolget("ui_client", True) or not BACKWARDS_COMPATIBLE
        wcaps = typedict(c.dictget("window") or {})
        if BACKWARDS_COMPATIBLE and not wcaps:
            legacy = c.get("windows")
            if isinstance(legacy, dict):
                # the offscreen recorder used to send its options in the plural namespace:
                wcaps = typedict(legacy)
        self.window_enabled = ui_client and wants_windows(c)
        self.window_grabs = c.boolget("window.grabs")
        if BACKWARDS_COMPATIBLE and not self.window_grabs:
            # older clients advertise grabs in the `pointer` namespace:
            self.window_grabs = c.boolget("pointer.grabs")
        self.window_bell = c.boolget("bell")
        self.system_tray = c.boolget("system_tray")
        default_metadata_supported = DEFAULT_METADATA_SUPPORTED
        if BACKWARDS_COMPATIBLE:
            default_metadata_supported += ("content-type", )
        self.window_metadata_supported = c.strtupleget("metadata.supported", default_metadata_supported)
        log("metadata supported=%s", self.window_metadata_supported)
        self.window_frame_sizes = wcaps.dictget("frame_sizes")
        self.window_min_size = wcaps.inttupleget("min-size", (0, 0))
        self.window_max_size = wcaps.inttupleget("max-size", (0, 0))
        self.window_restack = wcaps.boolget("restack", False)
        # window filters:
        try:
            for object_name, property_name, operator, value in c.tupleget("window-filters"):
                self.add_window_filter(object_name, property_name, operator, value)
        except Exception as e:
            filterslog.error("Error parsing window-filters: %s", e)
        window_record_requested = wcaps.boolget("record")
        self.window_record = window_record_requested and is_recording_allowed(self, "windows")
        self.window_sync_position = wcaps.boolget("sync-position", self.window_record) and is_sync_allowed(self, "position")
        self.window_sync_focus = wcaps.boolget("sync-focus", self.window_record) and is_sync_allowed(self, "focus")
        # Stacking updates are harmless window-manager state. Unlike the other
        # recording and synchronization streams, they do not need a socket option.
        self.window_sync_stacking = window_record_requested or wcaps.boolget("sync-stacking")
        self.subsurface_composite_modes = wcaps.strtupleget("subsurface-composite")

    def supports_subsurface_composite(self) -> bool:
        return SUBSURFACE_COMPOSITE_MODE in self.subsurface_composite_modes

    def get_caps(self) -> dict[str, Any]:
        return {}

    ######################################################################
    # info:
    def get_info(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "enabled": self.window_enabled,
            "bell": self.window_bell,
            "system-tray": self.system_tray,
            "restack": self.window_restack,
            "grabs": self.window_grabs,
            "sync-position": self.window_sync_position,
            "sync-focus": self.window_sync_focus,
            "sync-stacking": self.window_sync_stacking,
        }
        if self.hidden_windows:
            # `sharing=combine`: the windows outside this client's area of the display
            info["hidden"] = len(self.hidden_windows)
        wsize: dict[str, Any] = {
            "min": self.window_min_size,
            "max": self.window_max_size,
        }
        if self.window_frame_sizes:
            wsize["frame-sizes"] = self.window_frame_sizes
        info["size"] = wsize
        info.update(self.get_window_info())
        return {"window": info}

    def get_window_info(self) -> dict[str, Any]:
        """
        Adds encoding and window specific information
        """
        # pylint: disable=import-outside-toplevel
        from xpra.util.stats import get_list_stats
        pqpixels = [x[2] for x in tuple(self.packet_queue)]
        pqpi = get_list_stats(pqpixels)
        if pqpixels:
            pqpi["current"] = pqpixels[-1]
        with self._damage_packet_lock:
            next_packet_sequence = self._damage_packet_sequence
            ack_owners = len(self._damage_packet_owners)
            active_pixel_sources = len(self._damage_packet_sources)
            # A parked successor and a captured transaction can exist without
            # an encode item or ACK owner. Expose those distinct drain owners.
            subsurface_pending = sum(bool(state.get("pending_region"))
                                     for state in self._subsurface_composites.values())
            subsurface_inflight = sum(bool(state.get("running"))
                                      for state in self._subsurface_composites.values())
            window_sources = tuple(self.window_sources.items())
            subsurface_sources = tuple(self.subsurface_sources.items())
        # Historical statistics and current ownership share the damage
        # namespace. Populate the statistics first, then add the exact live
        # state without replacing historical nested queue samples.
        info: dict[str, Any] = s.get_info() if (s := self.statistics) else {}
        dinfo = info.setdefault("damage", {})
        dinfo.update({
            "packet_queue_pixels": pqpi,
            "next-packet-sequence": next_packet_sequence,
            "ack-owners": ack_owners,
            "active-pixel-sources": active_pixel_sources,
            "subsurface-pending": subsurface_pending,
            "subsurface-inflight": subsurface_inflight,
        })
        dinfo.setdefault("compression_queue", {}).setdefault("size", {})["current"] = self.encode_queue_size()
        dinfo.setdefault("packet_queue", {}).setdefault("size", {})["current"] = len(self.packet_queue)
        if gbc := self.global_batch_config:
            info["batch"] = gbc.get_info()
        if window_sources:
            total_pixels = 0
            total_time = 0.0
            in_latencies, out_latencies = [], []
            winfo: dict[int, Any] = {}
            # group subsurface sources by parent wid for the parent's "subsurfaces" entry
            subs_by_parent: dict[int, dict[int, dict[str, Any]]] = {}
            for sub_wid, sub_ws in subsurface_sources:
                entry: dict[str, Any] = {
                    "offset": (sub_ws.offset_x, sub_ws.offset_y),
                    "info": sub_ws.get_info(),
                }
                subs_by_parent.setdefault(sub_ws.parent_wid, {})[sub_wid] = entry
            for wid, ws in window_sources:
                # per-window source stats:
                winfo[wid] = ws.get_info()
                if subs := subs_by_parent.get(wid):
                    winfo[wid]["subsurfaces"] = subs
                # collect stats for global averages:
                for _, _, pixels, _, _, encoding_time in tuple(ws.statistics.encoding_stats):
                    total_pixels += pixels
                    total_time += encoding_time
                in_latencies += [x * 1000 for _, _, _, x in tuple(ws.statistics.damage_in_latency)]
                out_latencies += [x * 1000 for _, _, _, x in tuple(ws.statistics.damage_out_latency)]
            info["windows"] = winfo
            v = 0
            if total_time > 0:
                v = int(total_pixels / total_time)
            info.setdefault("encoding", {})["pixels_encoded_per_second"] = v
            dinfo = info.setdefault("damage", {})
            dinfo["in_latency"] = get_list_stats(in_latencies, show_percentile=(9,))
            dinfo["out_latency"] = get_list_stats(out_latencies, show_percentile=(9,))
        return info

    ######################################################################
    # grabs:
    def window_grab(self, wid) -> None:
        if self.window_grabs and self.hello_sent:
            self.send(WINDOW_GRAB, wid)

    def window_ungrab(self, wid) -> None:
        if self.window_grabs and self.hello_sent:
            self.send(WINDOW_UNGRAB, wid)

    def send_window_stacking(self, stacking: Sequence[int]) -> None:
        focuslog("send_window_stacking(%s)", stacking)
        self.send(WINDOW_STACKING, list(stacking))

    def bell(self, wid: int, device: int, percent: int, pitch: int, duration: int,
             bell_class, bell_id: int, bell_name: str) -> None:
        if not self.window_bell or self.suspended or not self.hello_sent:
            return
        self.send_async(WINDOW_BELL, wid, device, percent, pitch, duration, bell_class, bell_id, bell_name)

    ######################################################################
    # window filters:
    def reset_window_filters(self) -> None:
        self.window_filters = [(uuid, f) for uuid, f in self.window_filters if uuid != self.uuid]

    def get_all_window_filters(self) -> list:
        return [f for uuid, f in self.window_filters if uuid == self.uuid]

    def add_window_filter(self, object_name: str, property_name: str, operator: str, value: Any) -> None:
        window_filter = get_window_filter(object_name, property_name, operator, value)
        assert window_filter
        self.do_add_window_filter(window_filter)

    def do_add_window_filter(self, window_filter) -> None:
        # (reminder: filters are shared between all sources)
        self.window_filters.append((self.uuid, window_filter))

    def _window_model_matches(self, wid: int, window) -> bool:
        with self._damage_packet_lock:
            current = self._window_models.get(wid)
            source = self.window_sources.get(wid)
            source_window = getattr(source, "window", None)
            detaching = self._window_detaching.get(wid)
            return (
                (current is None or current is window)
                and (source_window is None or source_window is window)
                and detaching is None
            )

    def is_window_refused(self, wid: int, window) -> bool:
        with self._damage_packet_lock:
            refused = self._refused_windows.get(wid)
            return bool(refused and refused[0] is window)

    def is_window_announced(self, wid: int, window) -> bool:
        with self._damage_packet_lock:
            return self._announced_windows.get(wid) is window

    def _announce_window(self, wid: int, window, callback: Callable[[], None]) -> bool:
        """Queue one client backing creation and bind it to this exact model."""
        with self._damage_packet_lock:
            if self._damage_packet_closing:
                return False
            current = self._window_models.get(wid)
            refused = self._refused_windows.get(wid)
            if current is not None and current is not window:
                return False
            source = self.window_sources.get(wid)
            source_window = getattr(source, "window", None)
            if source_window is not None and source_window is not window:
                return False
            if refused and refused[0] is window:
                return False
            if wid in self._window_detaching:
                return False
            if self._announced_windows.get(wid) is window:
                return False
            callback()
            self._window_models[wid] = window
            self._announced_windows[wid] = window
            return True

    def allow_window(self, wid: int, window) -> bool:
        """Lift this connection's refusal for the exact live model.

        Re-creation remains the caller's responsibility.  Advancing the
        backing epoch here separates any restoration setup from packets which
        belonged to the refused generation.
        """
        with self._damage_packet_condition:
            while self._window_detaching.get(wid) is window and not self._damage_packet_closing:
                self._damage_packet_condition.wait()
            if self._damage_packet_closing:
                return False
            if wid in self._window_detaching:
                return False
            current = self._window_models.get(wid)
            refused = self._refused_windows.get(wid)
            if current is not window or not refused or refused[0] is not window:
                return False
            self._refused_windows.pop(wid, None)
            self._backing_epochs[wid] = self._backing_epochs.get(wid, 0) + 1
            return True

    def refuse_window(self, wid: int, window, reason: str) -> bool:
        """Withdraw one exact model from this client and detach its backing."""
        with self._damage_packet_condition:
            while self._window_detaching.get(wid) is window and not self._damage_packet_closing:
                self._damage_packet_condition.wait()
            if self._damage_packet_closing:
                return False
            if wid in self._window_detaching:
                return False
            current = self._window_models.get(wid)
            if current is not None and current is not window:
                return False
            source = self.window_sources.get(wid)
            source_window = getattr(source, "window", None)
            if source_window is not None and source_window is not window:
                return False
            refused = self._refused_windows.get(wid)
            changed = not refused or refused[0] is not window
            announced = self._announced_windows.get(wid) is window
            self._window_models[wid] = window
            self._refused_windows[wid] = window, str(reason)
            if announced:
                self._announced_windows.pop(wid, None)
            self.hidden_windows.discard(window)
            self._window_detaching[wid] = window
        try:
            try:
                # A repeated refusal normally has nothing left to detach.  If
                # a source was accidentally materialized while refused, this
                # call still restores the invariant and advances its epoch.
                self._detach_window_sources(wid, window, force_epoch=changed)
            finally:
                if announced:
                    self.send(WINDOW_DESTROY, wid)
        finally:
            with self._damage_packet_condition:
                if self._window_detaching.get(wid) is window:
                    self._window_detaching.pop(wid, None)
                self._damage_packet_condition.notify_all()
        return changed

    def can_send_window(self, window) -> bool:
        with self._damage_packet_lock:
            if any(model is window for model, _reason in self._refused_windows.values()):
                return False
        if not self.hello_sent or not (self.window_enabled or self.system_tray):
            return False
        # we could also allow filtering for system tray windows?
        if self.window_filters and self.window_enabled and not window.is_tray():
            for uuid, window_filter in self.window_filters:
                filterslog("can_send_window(%s) checking %s for uuid=%s (client uuid=%s)",
                           window, window_filter, uuid, self.uuid)
                if window_filter.matches(window):
                    v = uuid in ("*", self.uuid)
                    filterslog("can_send_window(%s)=%s", window, v)
                    return v
        if self.window_enabled and self.system_tray:
            # common case shortcut
            v = True
        elif window.is_tray():
            v = self.system_tray
        else:
            v = self.window_enabled
        filterslog("can_send_window(%s)=%s", window, v)
        return v

    def can_send_window_model(self, wid: int, window) -> bool:
        return self._window_model_matches(wid, window) and self.can_send_window(window)

    def can_consume_window_damage(self, wid: int, window) -> bool:
        """Return whether ordinary damage can acquire ACK ownership for this model."""
        if (
            self.is_closed()
            or self.suspended
            or not self.is_window_announced(wid, window)
            or not self.can_send_window_model(wid, window)
            or self.is_window_hidden(window)
        ):
            return False
        source = self.get_window_source(wid)
        return source is None or not source.suspended

    ######################################################################
    # display area: with `sharing=combine`, this client only occupies a part
    # of the server's virtual display, so the coordinates we send it are
    # relative to that area, and the windows outside of it are hidden.
    # (`display_area` is provided by the `DisplayConnection` subsystem,
    #  it is `None` for every other sharing mode, which makes all of this a no-op)
    def get_window_origin(self) -> tuple[int, int]:
        area = getattr(self, "display_area", None)
        if not area:
            return 0, 0
        return area.x, area.y

    def to_client_position(self, x: int, y: int) -> tuple[int, int]:
        """ translate a position on the server's virtual display to this client's coordinate space """
        ox, oy = self.get_window_origin()
        return x - ox, y - oy

    def is_window_visible(self, window, geometry: Sequence[int] = ()) -> bool:
        """ does this window intersect this client's area of the virtual display? """
        area = getattr(self, "display_area", None)
        if not area:
            return True
        if window.is_tray():
            # trays are not placed on the desktop
            return True
        if not geometry or len(geometry) != 4:
            if not self.get_server_geometry:
                return True
            geometry = self.get_server_geometry(window)
        x, y, w, h = geometry
        if w <= 0 or h <= 0:
            return True
        visible = bool(area.intersects(x, y, w, h))
        sharinglog("is_window_visible(%s, %s)=%s (area=%s)", window, geometry, visible, area)
        return visible

    def is_window_hidden(self, window) -> bool:
        """ is this window outside this client's area of the virtual display? """
        return window in self.hidden_windows

    def get_hidden_metadata(self, window, hidden: bool) -> dict[str, Any]:
        """ the `HIDDEN_METADATA` values to send for a window entering or leaving this client's area """
        metadata: dict[str, Any] = {}
        for prop in HIDDEN_METADATA:
            if prop not in self.window_metadata_supported:
                continue
            if hidden:
                metadata[prop] = True
            else:
                metadata.update(make_window_metadata(window, prop))
        return metadata

    def update_window_visibility(self, wid: int, window, geometry: Sequence[int] = (),
                                 force=False, notify=True) -> bool:
        """
        Hide or show this window for this client, depending on whether it
        intersects this client's area of the virtual display.
        `notify` can be disabled when the caller is about to send the metadata itself.
        Returns `True` if the window is hidden from this client.
        """
        if not self._window_model_matches(wid, window):
            return False
        hidden = not self.is_window_visible(window, geometry)
        was_hidden = self.is_window_hidden(window)
        if hidden == was_hidden and not force:
            return hidden
        if hidden == was_hidden:
            sharinglog("re-asserting %s state of window %#x for %s",
                       "hidden" if hidden else "visible", wid, self)
        else:
            sharinglog("window %#x is now %s for %s",
                       wid, "hidden" if hidden else "visible", self)
        if hidden:
            self.hidden_windows.add(window)
        else:
            self.hidden_windows.discard(window)
        if not self.can_send_window(window):
            return hidden
        if notify and (metadata := self.get_hidden_metadata(window, hidden)):
            sharinglog("sending %s for window %#x", metadata, wid)
            self.send(WINDOW_METADATA, wid, metadata)
        # a hidden window is unmapped as far as this client is concerned,
        # so we can stop sending it any pixels:
        pixel_sources = self.get_window_pixel_sources(wid)
        if pixel_sources:
            if hidden:
                for ws in pixel_sources:
                    ws.unmap()
            else:
                if self.get_server_geometry:
                    # `mapped_at` is in server coordinates, as recorded by `_window_mapped_at`
                    # (the client will correct it when it maps the window again):
                    mapped_at = self.get_server_geometry(window)[:2]
                    for ws in pixel_sources:
                        ws.map(mapped_at)
                # it may have changed a lot since we stopped sending it:
                self.refresh(wid, window, {})
        return hidden

    ######################################################################
    # windows:
    def initiate_moveresize(self, wid: int, window, x_root: int, y_root: int,
                            direction: int, button: int, source_indication: int) -> None:
        if not self.can_send_window_model(wid, window):
            return
        log("initiate_moveresize sending to %s", self)
        x_root, y_root = self.to_client_position(x_root, y_root)
        self.send(WINDOW_INITIATE_MOVERESIZE, wid, x_root, y_root, direction, button, source_indication)

    def or_window_geometry(self, wid: int, window, x: int, y: int, w: int, h: int) -> None:
        if not self.can_send_window_model(wid, window):
            return
        self.update_window_visibility(wid, window, (x, y, w, h))
        packet_type = "configure-override-redirect" if BACKWARDS_COMPATIBLE else WINDOW_MOVE_RESIZE
        x, y = self.to_client_position(x, y)
        resized = self.client_backing_resized(wid, window, w, h)
        self.send(packet_type, wid, x, y, w, h)
        if resized and self.get_subsurface_sources(wid):
            self.request_subsurface_composite(wid, parent_region=(0, 0, w, h))

    def window_metadata(self, wid: int, window, prop: str) -> None:
        if not self.can_send_window_model(wid, window):
            return
        if prop == "icons":
            self.send_window_icon(wid, window)
        else:
            metadata = self._make_metadata(window, prop)
            if prop in PROPERTIES_DEBUG:
                metalog.info("make_metadata(%#x, %s, %r)=%s", wid, window, prop, metadata)
            else:
                metalog("make_metadata(%#x, %s, %r)=%s", wid, window, prop, metadata)
            if metadata:
                self.send(WINDOW_METADATA, wid, metadata)

    # Takes the name of a WindowModel property, and returns a dictionary of
    # xpra window metadata values that depend on that property
    def _make_metadata(self, window, propname: str, skip_defaults=False) -> dict[str, Any]:
        if propname not in self.window_metadata_supported:
            metalog("make_metadata: client does not support %r", propname)
            return {}
        if propname in HIDDEN_METADATA and self.is_window_hidden(window):
            # this window is outside this client's area of the virtual display,
            # tell it to keep the window out of the way:
            # (unconditionally, since `skip_defaults` would drop the `False` default)
            sharinglog("forcing %s=True for hidden window %s", propname, window)
            return {propname: True}
        metadata = make_window_metadata(window, propname, skip_defaults=skip_defaults)
        if getattr(self, "effective_readonly", lambda: self.readonly)():
            metalog("overriding size-constraints for readonly mode")
            size = window.get_dimensions()
            metadata["size-constraints"] = force_size_constraint(*size)
        return metadata

    def new_tray(self, wid: int, window, w: int, h: int) -> None:
        assert window.is_tray()
        if not self.can_send_window_model(wid, window):
            return
        metadata = {}
        for propname in list(window.get_property_names()):
            metadata.update(self._make_metadata(window, propname, skip_defaults=True))
        self._announce_window(
            wid, window, lambda: self.send_async("new-tray", wid, w, h, metadata),
        )

    def new_window(self, packet_type: str, wid: int, window, x: int, y: int, w: int, h: int,
                   client_properties: dict) -> None:
        if not self.can_send_window_model(wid, window):
            return
        # decide if this window belongs on this client's area before making the metadata,
        # so that `_make_metadata` can override it - no need to notify separately:
        # (the geometry we are given may have been adjusted for this client,
        #  the visibility must be decided from the position on the server's display)
        hidden = self.update_window_visibility(wid, window, notify=False)
        x, y = self.to_client_position(x, y)
        send_props = list(window.get_property_names())
        metalog("new window properties: %r", send_props)
        send_raw_icon = "icons" in send_props
        if send_raw_icon:
            send_props.remove("icons")
        metadata = {}
        for prop in send_props:
            v = self._make_metadata(window, prop, skip_defaults=True)
            if prop in PROPERTIES_DEBUG:
                metalog.info("make_metadata(%#x, %s, %r)=%s", wid, window, prop, v)
            else:
                metalog("make_metadata(%#x, %s, %r)=%s", wid, window, prop, v)
            metadata.update(v)
        log("new_window(%s, %#x, %s, %i, %i, %i, %i, %s) metadata(%s)=%s",
            packet_type, wid, window, x, y, w, h, client_properties, send_props, metadata)

        def announce() -> None:
            self.send_async(packet_type, wid, x, y, w, h, metadata, client_properties or {})
            # `_announce_window` invokes this callback under the packet-state
            # lock, so the initial client canvas and model identity appear as
            # one publication boundary without advancing the initial epoch.
            self._client_backing_sizes[wid] = (w, h)

        if not self._announce_window(wid, window, announce):
            return
        if hidden and (hidden_metadata := self.get_hidden_metadata(window, True)):
            # showing a new window usually de-iconifies it,
            # so we have to ask for it to be hidden again once it exists:
            self.send(WINDOW_METADATA, wid, hidden_metadata)
        if send_raw_icon:
            self.send_window_icon(wid, window)

    def send_window_icon(self, wid: int, window) -> None:
        if not self.can_send_window_model(wid, window):
            return
        # we may need to make a new source at this point:
        ws = self.make_window_source(wid, window)
        ws.send_window_icon()

    def lost_window(self, wid: int, window) -> None:
        with self._damage_packet_lock:
            current = self._window_models.get(wid)
            if current is not window or self._announced_windows.get(wid) is not window:
                return
            self._announced_windows.pop(wid, None)
        self.send(WINDOW_DESTROY, wid)

    def move_resize_window(self, wid: int, window, x: int, y: int, ww: int, wh: int, resize_counter: int = 0) -> None:
        """
        The server detected that the application window has been moved and/or resized,
        we forward it if the client supports this type of event.
        """
        if not self.can_send_window_model(wid, window):
            return
        self.update_window_visibility(wid, window, (x, y, ww, wh))
        x, y = self.to_client_position(x, y)
        resized = self.client_backing_resized(wid, window, ww, wh)
        self.send(WINDOW_MOVE_RESIZE, wid, x, y, ww, wh, resize_counter)
        if resized and self.get_subsurface_sources(wid):
            self.request_subsurface_composite(wid, parent_region=(0, 0, ww, wh))

    def resize_window(self, wid: int, window, ww: int, wh: int, resize_counter: int = 0) -> None:
        if not self.can_send_window_model(wid, window):
            return
        self.update_window_visibility(wid, window)
        resized = self.client_backing_resized(wid, window, ww, wh)
        self.send(WINDOW_RESIZED, wid, ww, wh, resize_counter)
        if resized and self.get_subsurface_sources(wid):
            self.request_subsurface_composite(wid, parent_region=(0, 0, ww, wh))

    def client_backing_resized(self, wid: int, window, ww: int, wh: int) -> bool:
        """Fence packets captured for an older client-side wire canvas.

        The epoch advances before the resize control packet is queued.  A
        network-dequeue recheck then drops already queued draw packets from
        the old canvas, while the replacement composite carries this epoch.
        Renderer-only ``render-size`` changes do not alter this wire canvas.
        """
        if not self._window_model_matches(wid, window) or self.is_window_refused(wid, window):
            return False
        size = int(ww), int(wh)
        with self._damage_packet_lock:
            if self._client_backing_sizes.get(wid) == size:
                return False
            self._client_backing_sizes[wid] = size
            self._backing_epochs[wid] = self._backing_epochs.get(wid, 0) + 1
        return True

    def cancel_damage(self, wid: int) -> None:
        """
        Use this method to cancel all currently pending and ongoing
        damage requests for a window.
        """
        for ws in self.get_window_pixel_sources(wid):
            ws.cancel_damage()

    def record_scroll_event(self, wid: int) -> None:
        if ws := self.window_sources.get(wid):
            ws.record_scroll_event()

    def reinit_encoders(self) -> None:
        for ws in self.all_pixel_sources():
            ws.init_encoders()

    def map_window(self, wid: int, window, coords=None) -> None:
        if not self._window_model_matches(wid, window) or self.is_window_refused(wid, window):
            return
        self.make_window_source(wid, window)
        with self._damage_packet_lock:
            self._backing_epochs[wid] = self._backing_epochs.get(wid, 0) + 1
        for source in self.get_window_pixel_sources(wid):
            source.map(coords)
        if self.get_subsurface_sources(wid):
            source = self.get_window_source(wid)
            if source:
                self.request_subsurface_composite(wid, parent_region=(0, 0, *source.window_dimensions))

    def unmap_window(self, wid: int, window) -> None:
        if not self._window_model_matches(wid, window) or self.is_window_refused(wid, window):
            return
        with self._damage_packet_lock:
            self._backing_epochs[wid] = self._backing_epochs.get(wid, 0) + 1
        for ws in self.get_window_pixel_sources(wid):
            ws.unmap()

    def restack_window(self, wid: int, window, detail: int, sibling: int) -> None:
        focuslog("restack_window(%#x, %s, %i, %i)", wid, window, detail, sibling)
        if not self.can_send_window_model(wid, window):
            return
        if not self.window_restack:
            # older clients can only handle "raise-window"
            if detail != 0:
                return
            self.send_async(WINDOW_RAISE, wid)
            return
        self.send_async(WINDOW_RESTACK, wid, detail, sibling)

    def raise_window(self, wid: int, window) -> None:
        if not self.can_send_window_model(wid, window):
            return
        self.send_async(WINDOW_RAISE, wid)

    def _detach_window_sources(self, wid: int, window=None, *, force_epoch: bool) -> bool:
        """Atomically detach a wire backing and all of its pixel producers."""
        with self._damage_packet_lock:
            ws = self.window_sources.get(wid)
            source_window = getattr(ws, "window", None)
            if window is not None and source_window is not None and source_window is not window:
                return False
            subsurfaces = tuple(
                (sub_wid, sub_ws) for sub_wid, sub_ws in self.subsurface_sources.items()
                if sub_ws.parent_wid == wid
            )
            had_state = bool(
                ws or subsurfaces
                or wid in self.window_backing_properties
                or wid in self.subsurface_stacking
                or wid in self._subsurface_composites
            )
            if ws:
                self.window_sources.pop(wid, None)
            for sub_wid, _sub_ws in subsurfaces:
                self.subsurface_sources.pop(sub_wid, None)
            self.window_backing_properties.pop(wid, None)
            self._client_backing_sizes.pop(wid, None)
            self.subsurface_stacking.pop(wid, None)
            composite_state = self._subsurface_composites.pop(wid, None)
            snapshot_images = self._take_subsurface_snapshot_images_locked(composite_state)
            self._subsurface_transaction_ids.pop(wid, None)
            if force_epoch or had_state:
                self._backing_epochs[wid] = self._backing_epochs.get(wid, 0) + 1
                self._subsurface_topology_epochs[wid] = (
                    self._subsurface_topology_epochs.get(wid, 0) + 1
                )
        self._free_subsurface_snapshot_images(snapshot_images)
        self._cancel_subsurface_glib_sources(wid)
        cleanup_errors: list[tuple[BaseException, Any]] = []

        def run_cleanup_step(callback: Callable, *args) -> None:
            try:
                callback(*args)
            except BaseException as e:
                cleanup_errors.append((e, e.__traceback__))

        sources = ((ws,) if ws else ()) + tuple(sub_ws for _sub_wid, sub_ws in subsurfaces)
        for source in sources:
            run_cleanup_step(self.unregister_damage_packets, source)
        if ws:
            run_cleanup_step(self.emit, "remove-window-source", ws)
            run_cleanup_step(ws.cleanup)
        for sub_wid, sub_ws in subsurfaces:
            run_cleanup_step(sub_ws.cleanup)
            self.calculate_window_pixels.pop(sub_wid, None)
        self.calculate_window_pixels.pop(wid, None)
        self._raise_pixel_cleanup_errors(f"window {wid:#x} removal", cleanup_errors)
        return had_state

    def remove_window(self, wid: int, window) -> None:
        """ The given window is gone, ensure we free all the related resources """
        with self._damage_packet_condition:
            while self._window_detaching.get(wid) is window and not self._damage_packet_closing:
                self._damage_packet_condition.wait()
            if self._damage_packet_closing or wid in self._window_detaching:
                return
            current = self._window_models.get(wid)
            if current is not None and current is not window:
                return
            source = self.window_sources.get(wid)
            source_window = getattr(source, "window", None)
            if source_window is not None and source_window is not window:
                return
            self._window_detaching[wid] = window
        self.hidden_windows.discard(window)
        try:
            self._detach_window_sources(wid, window, force_epoch=True)
        finally:
            with self._damage_packet_condition:
                if self._window_models.get(wid) is window:
                    self._window_models.pop(wid, None)
                if self._announced_windows.get(wid) is window:
                    self._announced_windows.pop(wid, None)
                refused = self._refused_windows.get(wid)
                if refused and refused[0] is window:
                    self._refused_windows.pop(wid, None)
                if self._window_detaching.get(wid) is window:
                    self._window_detaching.pop(wid, None)
                self._damage_packet_condition.notify_all()

    def refresh(self, wid: int, window, opts) -> None:
        if not self.can_send_window_model(wid, window):
            return
        if self.get_subsurface_sources(wid):
            width, height = window.get_dimensions()
            self.request_subsurface_composite(wid, parent_region=(0, 0, width, height))
            return
        self.cancel_damage(wid)
        w, h = window.get_dimensions()
        self.damage(wid, window, 0, 0, w, h, opts)

    def update_batch(self, wid: int, window, batch_props: typedict) -> None:
        if not self._window_model_matches(wid, window):
            return
        for ws in self.get_window_pixel_sources(wid):
            if "reset" in batch_props:
                ws.batch_config = self.make_batch_config(ws.wid, ws.window)
            for x in ("always", "locked"):
                if x in batch_props:
                    setattr(ws.batch_config, x, batch_props.boolget(x))
            for x in ("min_delay", "max_delay", "timeout_delay", "delay"):
                if x in batch_props:
                    setattr(ws.batch_config, x, batch_props.intget(x))
            log("batch config updated for window %#x: %s", wid, ws.batch_config)

    def set_client_properties(self, wid: int, window, new_client_properties: typedict) -> None:
        assert self.window_enabled
        if not self._window_model_matches(wid, window) or self.is_window_refused(wid, window):
            return
        ws = self.make_window_source(wid, window)
        ws.set_client_properties(typedict(dict(new_client_properties)))
        backing_properties = {
            name: new_client_properties[name]
            for name in SUBSURFACE_BACKING_PROPERTIES if name in new_client_properties
        }
        if not backing_properties:
            return
        with self._damage_packet_lock:
            self.window_backing_properties.setdefault(wid, {}).update(backing_properties)
            subsurfaces = tuple(ws for ws in self.subsurface_sources.values() if ws.parent_wid == wid)
        for sub_ws in subsurfaces:
            sub_ws.set_client_properties(typedict(backing_properties.copy()))

    def apply_subsurface_client_properties(self, ws, parent_wid: int) -> None:
        with self._damage_packet_lock:
            properties = self.window_backing_properties.get(parent_wid, {}).copy()
        if properties:
            ws.set_client_properties(typedict(properties))

    def _activate_pixel_source(self, sources: dict[int, Any], wid: int, source):
        with self._damage_packet_lock:
            if self._damage_packet_closing:
                raise RuntimeError("connection is closing")
            if sources is self.window_sources:
                window = getattr(source, "window", None)
                current = self._window_models.get(wid)
                if wid in self._window_detaching:
                    raise RuntimeError(f"window {wid:#x} source is being detached")
                if current is not None and current is not window:
                    raise RuntimeError(f"window {wid:#x} belongs to another model")
            if existing := sources.get(wid):
                return existing
            sources[wid] = source
            self.configure_pixel_source(source)
        return source

    def _discard_pixel_source(self, sources: dict[int, Any], wid: int, source, scope: str) -> None:
        with self._damage_packet_lock:
            if sources.get(wid) is source:
                sources.pop(wid, None)
        self.unregister_damage_packets(source)
        try:
            source.cleanup()
        except BaseException:
            log.error("Additional error cleaning %s", scope, exc_info=True)

    def update_subsurface_geometries(
            self, parent_wid: int,
            geometries: Sequence[tuple[int, int, int, int, int, int, int]],
            stacking: Sequence[int] = (),
    ) -> None:
        geometry_by_wid = {geometry[0]: geometry for geometry in geometries}
        allowed = {parent_wid, *geometry_by_wid}
        order = tuple(dict.fromkeys(wid for wid in stacking if wid in allowed))
        if parent_wid not in order:
            order = (parent_wid, *order)
        order += tuple(wid for wid in geometry_by_wid if wid not in order)
        repair_regions: dict[int, tuple[int, int, int, int] | None] = {}
        property_updates = []
        schedules: dict[int, object] = {}
        with self._damage_packet_lock:
            previous_order = self.subsurface_stacking.get(parent_wid, ())
            self.subsurface_stacking[parent_wid] = order
            previous_children = tuple(wid for wid in previous_order if wid != parent_wid)
            current_children = tuple(wid for wid in order if wid != parent_wid)
            if previous_order != order:
                if not previous_children and current_children:
                    # Activating composition invalidates every ordinary parent
                    # packet which may already be encoding. Rebuild the whole
                    # backing once so a rejected pre-topology packet cannot
                    # leave an unrelated parent region unpainted.
                    if parent := self.window_sources.get(parent_wid):
                        repair_regions[parent_wid] = (0, 0, *parent.window_dimensions)
                for child_wid in set(previous_order) | set(order):
                    if geometry := geometry_by_wid.get(child_wid):
                        region = geometry[1], geometry[2], geometry[3], geometry[4]
                    elif ws := self.subsurface_sources.get(child_wid):
                        region = ws.offset_x, ws.offset_y, ws.logical_width, ws.logical_height
                    else:
                        continue
                    repair_regions[parent_wid] = self._merge_damage_regions(
                        repair_regions.get(parent_wid), region,
                    )
            for geometry in geometries:
                wid, offset_x, offset_y, logical_width, logical_height, native_width, native_height = geometry
                ws = self.subsurface_sources.get(wid)
                if ws is None:
                    continue
                old_geometry = ws.geometry()
                new_geometry = (
                    parent_wid, offset_x, offset_y,
                    logical_width if logical_width > 0 else ws.logical_width,
                    logical_height if logical_height > 0 else ws.logical_height,
                    native_width if native_width > 0 else ws.native_width,
                    native_height if native_height > 0 else ws.native_height,
                )
                if old_geometry == new_geometry:
                    continue
                old_parent, old_x, old_y, old_width, old_height, _old_native_w, _old_native_h = old_geometry
                ws.update_geometry(*new_geometry)
                if old_parent != parent_wid:
                    old_order = self.subsurface_stacking.get(old_parent, ())
                    self.subsurface_stacking[old_parent] = tuple(value for value in old_order if value != wid)
                    property_updates.append(ws)
                if old_width > 0 and old_height > 0:
                    repair_regions[old_parent] = self._merge_damage_regions(
                        repair_regions.get(old_parent), (old_x, old_y, old_width, old_height),
                    )
                if new_geometry[3] > 0 and new_geometry[4] > 0:
                    repair_regions[parent_wid] = self._merge_damage_regions(
                        repair_regions.get(parent_wid),
                        (new_geometry[1], new_geometry[2], new_geometry[3], new_geometry[4]),
                    )
            if (previous_order != order and (previous_children or current_children)
                    and parent_wid not in repair_regions):
                if parent := self.window_sources.get(parent_wid):
                    repair_regions[parent_wid] = (0, 0, *parent.window_dimensions)
            for repair_parent, region in repair_regions.items():
                if repair_parent:
                    token = self._record_subsurface_composite_locked(
                        repair_parent, region, topology_change=True,
                    )
                    if token:
                        schedules[repair_parent] = token
        errors: list[tuple[BaseException, Any]] = []
        for ws in property_updates:
            try:
                self.apply_subsurface_client_properties(ws, parent_wid)
            except BaseException as e:
                errors.append((e, e.__traceback__))
        for repair_parent, token in schedules.items():
            self._schedule_subsurface_callback(
                repair_parent, token, self._run_subsurface_composite, errors=errors,
            )
        self._raise_pixel_cleanup_errors("subsurface geometry update", errors)

    def update_subsurface_geometry(
            self, wid: int, parent_wid: int, offset_x: int, offset_y: int,
            logical_width: int, logical_height: int,
            native_width: int, native_height: int,
    ) -> None:
        repair_regions: dict[int, tuple[int, int, int, int] | None] = {}
        schedules: dict[int, object] = {}
        with self._damage_packet_lock:
            ws = self.subsurface_sources.get(wid)
            if ws is None:
                return
            old_geometry = ws.geometry()
            new_geometry = (
                parent_wid, offset_x, offset_y,
                logical_width if logical_width > 0 else ws.logical_width,
                logical_height if logical_height > 0 else ws.logical_height,
                native_width if native_width > 0 else ws.native_width,
                native_height if native_height > 0 else ws.native_height,
            )
            if old_geometry == new_geometry:
                return
            old_parent, old_x, old_y, old_width, old_height, _old_native_w, _old_native_h = old_geometry
            ws.update_geometry(*new_geometry)
            if old_parent != parent_wid:
                old_order = self.subsurface_stacking.get(old_parent, ())
                self.subsurface_stacking[old_parent] = tuple(value for value in old_order if value != wid)
                new_order = self.subsurface_stacking.get(parent_wid, ())
                if wid not in new_order:
                    self.subsurface_stacking[parent_wid] = (*new_order, wid)
            if old_width > 0 and old_height > 0:
                repair_regions[old_parent] = self._merge_damage_regions(
                    repair_regions.get(old_parent), (old_x, old_y, old_width, old_height),
                )
            if new_geometry[3] > 0 and new_geometry[4] > 0:
                repair_regions[parent_wid] = self._merge_damage_regions(
                    repair_regions.get(parent_wid),
                    (new_geometry[1], new_geometry[2], new_geometry[3], new_geometry[4]),
                )
            for repair_parent, region in repair_regions.items():
                token = self._record_subsurface_composite_locked(
                    repair_parent, region, topology_change=True,
                )
                if token:
                    schedules[repair_parent] = token
        errors: list[tuple[BaseException, Any]] = []
        if old_parent != parent_wid:
            try:
                self.apply_subsurface_client_properties(ws, parent_wid)
            except BaseException as e:
                errors.append((e, e.__traceback__))
        for repair_parent, token in schedules.items():
            self._schedule_subsurface_callback(
                repair_parent, token, self._run_subsurface_composite, errors=errors,
            )
        self._raise_pixel_cleanup_errors("subsurface geometry update", errors)

    def damage_subsurface(self, wid: int) -> None:
        with self._damage_packet_lock:
            ws = self.subsurface_sources.get(wid)
            parent_wid = ws.parent_wid if ws else 0
        if not ws:
            return
        if s := self.statistics:
            width = max(0, ws.logical_width)
            height = max(0, ws.logical_height)
            s.damage_last_events.append((wid, monotonic(), width * height))
        self.request_subsurface_composite(
            parent_wid,
            parent_region=(ws.offset_x, ws.offset_y, ws.logical_width, ws.logical_height),
        )

    def request_subsurface_composite(
            self, parent_wid: int, *, changed_wid: int = 0,
            parent_region: tuple[int, int, int, int] | None = None,
            topology_change: bool = False,
    ) -> bool:
        if not parent_wid:
            return False
        with self._damage_packet_lock:
            if self._damage_packet_closing:
                return False
            if changed_wid:
                ws = self.subsurface_sources.get(changed_wid)
                if ws and ws.parent_wid == parent_wid:
                    parent_region = (
                        ws.offset_x, ws.offset_y,
                        ws.logical_width, ws.logical_height,
                    )
            token = self._record_subsurface_composite_locked(
                parent_wid, parent_region, topology_change=topology_change,
            )
            accepted = parent_wid in self._subsurface_composites
        if token:
            return self._schedule_subsurface_callback(
                parent_wid, token, self._run_subsurface_composite,
            )
        return accepted

    @staticmethod
    def _merge_damage_regions(
            first: tuple[int, int, int, int] | None,
            second: tuple[int, int, int, int] | None,
    ) -> tuple[int, int, int, int] | None:
        if not second or second[2] <= 0 or second[3] <= 0:
            return first
        if not first or first[2] <= 0 or first[3] <= 0:
            return second
        left = min(first[0], second[0])
        top = min(first[1], second[1])
        right = max(first[0] + first[2], second[0] + second[2])
        bottom = max(first[1] + first[3], second[1] + second[3])
        return left, top, right - left, bottom - top

    @staticmethod
    def _intersect_damage_regions(
            first: tuple[int, int, int, int],
            second: tuple[int, int, int, int],
    ) -> tuple[int, int, int, int] | None:
        left = max(first[0], second[0])
        top = max(first[1], second[1])
        right = min(first[0] + first[2], second[0] + second[2])
        bottom = min(first[1] + first[3], second[1] + second[3])
        if right <= left or bottom <= top:
            return None
        return left, top, right - left, bottom - top

    @staticmethod
    def _subsurface_content_generation(source) -> int:
        """Return the retained native-raster generation for one stage source."""
        window = getattr(source, "window", None)
        getter = getattr(type(window), "get_snapshot_generation", None)
        if getter is None:
            # Non-Wayland test/compatibility models predate retained WSSO
            # snapshots. Their immutable test raster is generation zero.
            return 0
        try:
            generation = getter(window)
        except BaseException:
            log.error("Error reading source %#x snapshot generation", source.wid, exc_info=True)
            return -1
        return generation if type(generation) is int and generation >= 0 else -1

    def _subsurface_content_generations_current(self, state: dict[str, Any]) -> bool:
        generations = state.get("content_generations", ())
        return bool(generations) and all(
            generation >= 0 and self._subsurface_content_generation(source) == generation
            for source, generation in generations
        )

    @staticmethod
    def _take_subsurface_snapshot_images_locked(state: dict[str, Any] | None) -> tuple:
        """Detach every unconsumed image from one composite transaction."""
        if not state:
            return ()
        images = tuple(image for image in state.get("snapshot_images", ()) if image is not None)
        state["snapshot_images"] = []
        state["snapshots_ready"] = False
        return images

    @staticmethod
    def _free_subsurface_snapshot_images(images: Sequence) -> None:
        for image in images:
            try:
                free_image_wrapper(image)
            except BaseException:
                log.error("Error releasing a captured subsurface composite image", exc_info=True)

    def _record_subsurface_composite_locked(
            self, parent_wid: int,
            region: tuple[int, int, int, int] | None,
            *, topology_change: bool,
    ) -> object | None:
        if not parent_wid or self._damage_packet_closing:
            return None
        new_region = self._merge_damage_regions(None, region)
        state = self._subsurface_composites.get(parent_wid)
        if topology_change:
            self._subsurface_topology_epochs[parent_wid] = (
                self._subsurface_topology_epochs.get(parent_wid, 0) + 1
            )
        if state is None:
            # A zero-sized child has no pixels to erase. Advancing the topology
            # epoch is still useful for invalidating in-flight packets, but an
            # empty state would reject ordinary parent packets forever with no
            # repair transaction capable of clearing it.
            if not new_region:
                return None
            state = {
                "running": False,
                "token": None,
                "pending_region": None,
                "active_region": None,
                "transaction_id": 0,
                "content_generations": (),
                "snapshot_images": [],
                "snapshots_ready": False,
                "failures": 0,
            }
            self._subsurface_composites[parent_wid] = state
        if topology_change:
            state["pending_region"] = self._merge_damage_regions(
                state["pending_region"], state["active_region"],
            )
        state["pending_region"] = self._merge_damage_regions(state["pending_region"], new_region)
        if new_region and not state["running"] and state["token"] is None:
            state["failures"] = 0
        if state["running"] or not state["pending_region"]:
            return None
        state["running"] = True
        state["token"] = token = object()
        return token

    def _park_subsurface_composite(self, parent_wid: int, token: object) -> None:
        """Leave a failed-to-schedule repair dormant but fully recoverable."""
        with self._damage_packet_lock:
            state = self._subsurface_composites.get(parent_wid)
            if not state or state["token"] is not token:
                return
            snapshot_images = self._take_subsurface_snapshot_images_locked(state)
            state["pending_region"] = self._merge_damage_regions(
                state["pending_region"], state["active_region"],
            )
            state["active_region"] = None
            state["running"] = False
            state["token"] = None
            state["transaction_id"] = 0
            state["content_generations"] = ()
            state["failures"] += 1
            if not state["pending_region"]:
                self._subsurface_composites.pop(parent_wid, None)
        self._free_subsurface_snapshot_images(snapshot_images)

    @staticmethod
    def _destroy_subsurface_source(source: GLib.Source, description: str) -> None:
        try:
            source.destroy()
        except BaseException:
            log.error("Error destroying %s", description, exc_info=True)

    def _register_subsurface_glib_source(
            self, parent_wid: int, token: object, kind: str,
            create_source: Callable[[], GLib.Source], callback: Callable, *args,
    ) -> GLib.Source | None:
        """Own an exact Source before attachment can dispatch or race removal."""
        reservation = object()
        record: dict[str, Any] = {
            "parent-wid": parent_wid,
            "token": token,
            "kind": kind,
            "source": None,
            "cancelled": False,
        }
        with self._damage_packet_lock:
            state = self._subsurface_composites.get(parent_wid)
            if self._damage_packet_closing or not state or state["token"] is not token:
                return None
            self._subsurface_glib_sources[reservation] = record

        def dispatch(*_unused) -> bool:
            with self._damage_packet_lock:
                if self._subsurface_glib_sources.get(reservation) is not record:
                    return False
                self._subsurface_glib_sources.pop(reservation, None)
            callback(*args)
            return False

        source = None
        description = f"subsurface {kind} callback for parent {parent_wid:#x}"
        try:
            source = create_source()
            with self._damage_packet_lock:
                record["source"] = source
                cancelled = record["cancelled"]
            if cancelled:
                self._destroy_subsurface_source(source, description)
                return None
            source.set_callback(dispatch)
            with self._damage_packet_lock:
                cancelled = record["cancelled"]
            if cancelled:
                self._destroy_subsurface_source(source, description)
                return None
            # No connection lock across attachment. Teardown can destroy this
            # exact Source even while attach is blocked; a returned numeric ID
            # is never retained or used for cancellation.
            source_id = source.attach(None)
            with self._damage_packet_lock:
                cancelled = record["cancelled"]
            if cancelled:
                self._destroy_subsurface_source(source, description)
                return None
            if not source_id:
                raise RuntimeError(f"failed to attach {description}")
            # Dispatch may already have removed the reservation and destroyed
            # the one-shot source. That is successful scheduling, not rollback.
            return source
        except BaseException:
            with self._damage_packet_lock:
                if self._subsurface_glib_sources.get(reservation) is record:
                    self._subsurface_glib_sources.pop(reservation, None)
                record["cancelled"] = True
            if source is not None:
                self._destroy_subsurface_source(source, description)
            raise

    def _remove_subsurface_glib_source(
            self, source: GLib.Source | None, parent_wid: int, token: object, kind: str,
    ) -> bool:
        if source is None:
            return False
        owned = False
        with self._damage_packet_lock:
            for reservation, record in tuple(self._subsurface_glib_sources.items()):
                if (
                    record["source"] is source
                    and record["parent-wid"] == parent_wid
                    and record["token"] is token
                    and record["kind"] == kind
                ):
                    self._subsurface_glib_sources.pop(reservation, None)
                    record["cancelled"] = True
                    owned = True
                    break
        if owned:
            self._destroy_subsurface_source(
                source, f"subsurface {kind} callback for parent {parent_wid:#x}",
            )
        return owned

    def _cancel_subsurface_glib_sources(self, parent_wid: int | None = None) -> None:
        sources: list[tuple[GLib.Source, int, str]] = []
        with self._damage_packet_lock:
            for reservation, record in tuple(self._subsurface_glib_sources.items()):
                if parent_wid is not None and record["parent-wid"] != parent_wid:
                    continue
                self._subsurface_glib_sources.pop(reservation, None)
                record["cancelled"] = True
                if record["source"] is not None:
                    sources.append((record["source"], record["parent-wid"], record["kind"]))
        for source, owner_wid, kind in sources:
            self._destroy_subsurface_source(
                source, f"subsurface {kind} callback for parent {owner_wid:#x}",
            )

    def _schedule_subsurface_callback(
            self, parent_wid: int, token: object, callback: Callable, *args,
            errors: list[tuple[BaseException, Any]] | None = None,
    ) -> bool:
        try:
            source = self._register_subsurface_glib_source(
                parent_wid, token, "idle", GLib.idle_source_new,
                callback, parent_wid, token, *args,
            )
            return source is not None
        except BaseException as e:
            self._park_subsurface_composite(parent_wid, token)
            if errors is not None:
                errors.append((e, e.__traceback__))
            else:
                log.error(
                    "Error scheduling subsurface composition for parent %#x",
                    parent_wid, exc_info=True,
                )
            return False

    def _run_subsurface_composite(self, parent_wid: int, token: object) -> None:
        with self._damage_packet_lock:
            state = self._subsurface_composites.get(parent_wid)
            if not state or state["token"] is not token or self._damage_packet_closing:
                return
            parent = self.window_sources.get(parent_wid)
            active_region = state["pending_region"]
            state["pending_region"] = None
            state["active_region"] = active_region
        if not parent or not active_region:
            self._finish_subsurface_composite(parent_wid, token, requeue_active=True, failed=True)
            return
        if eligibility_error := self.subsurface_composite_eligibility(parent_wid):
            if not self._refuse_ineligible_subsurface_composite(parent_wid, eligibility_error):
                self._finish_subsurface_composite(parent_wid, token, requeue_active=True, failed=True)
            return
        try:
            parent.may_update_window_dimensions()
        except AttributeError:
            pass
        except BaseException:
            log.error("Error updating parent dimensions for subsurface composition %#x", parent_wid,
                      exc_info=True)
            self._finish_subsurface_composite(parent_wid, token, requeue_active=True, failed=True)
            return
        if eligibility_error := self.subsurface_composite_eligibility(parent_wid):
            if not self._refuse_ineligible_subsurface_composite(parent_wid, eligibility_error):
                self._finish_subsurface_composite(parent_wid, token, requeue_active=True, failed=True)
            return
        with self._damage_packet_lock:
            state = self._subsurface_composites.get(parent_wid)
            if not state or state["token"] is not token or self._damage_packet_closing:
                return
            backing_epoch = self._backing_epochs.get(parent_wid, 0)
            topology_epoch = self._subsurface_topology_epochs.get(parent_wid, 0)
            # Transaction identities are connection-global so a child moving
            # between wire parents cannot reuse an ID from the repair it just
            # invalidated.  The per-parent map below remains the validator for
            # independently queued compositions.
            self._subsurface_transaction_id += 1
            transaction_id = self._subsurface_transaction_id
            self._subsurface_transaction_ids[parent_wid] = transaction_id
            window_width, window_height = parent.window_dimensions
            active_region = self._intersect_damage_regions(
                active_region, (0, 0, window_width, window_height),
            )
            state["active_region"] = active_region
            order = self.subsurface_stacking.get(parent_wid, ())
            if parent_wid not in order:
                order = (parent_wid, *order)
            layers = []
            for layer_wid in tuple(dict.fromkeys(order)):
                if layer_wid == parent_wid:
                    source = parent
                    layer_region = (0, 0, window_width, window_height)
                    geometry_generation = -1
                else:
                    source = self.subsurface_sources.get(layer_wid)
                    if source is None or source.parent_wid != parent_wid:
                        continue
                    layer_region = (
                        source.offset_x, source.offset_y,
                        source.logical_width, source.logical_height,
                    )
                    geometry_generation = source.geometry_generation
                intersection = self._intersect_damage_regions(active_region, layer_region) if active_region else None
                if not intersection:
                    continue
                ix, iy, width, height = intersection
                layers.append((
                    source, ix - layer_region[0], iy - layer_region[1], width, height,
                    geometry_generation, (window_width, window_height),
                ))
            # Bind the capture pass to one retained-raster generation per
            # participating source.  Every image wrapper is acquired below in
            # this same UI iteration before asynchronous encoding starts.
            state["transaction_id"] = transaction_id
            state["content_generations"] = tuple(
                (source, self._subsurface_content_generation(source))
                for source in dict.fromkeys(layer[0] for layer in layers)
            )
            state["snapshots_ready"] = False

        # Cancel every producer for this backing before the repair stages are
        # captured. Already executing mmap writes retain their terminal lease;
        # every other stale generation loses publication authority.
        for source in tuple(dict.fromkeys(layer[0] for layer in layers)):
            try:
                source.cancel_damage()
            except BaseException:
                log.error("Error cancelling source %#x for subsurface composition", source.wid, exc_info=True)

        if not layers:
            self._finish_subsurface_composite(parent_wid, token, requeue_active=True, failed=True)
            return

        # Retained Wayland rasters contain immutable bytes. Borrow every layer
        # synchronously so this transaction represents one compositor state,
        # then let the serial encoder stages consume those wrappers. A later
        # native commit can queue a successor without revoking these pixels.
        captured_images = []
        try:
            for source, x, y, width, height, _geometry_generation, _target_size in layers:
                image = source.capture_damage_image(x, y, width, height)
                if image is None:
                    raise ValueError(f"source {source.wid:#x} has no composite pixels")
                captured_images.append(image)
                if (image.get_width(), image.get_height()) != (width, height):
                    raise ValueError(
                        f"source {source.wid:#x} returned composite pixels "
                        f"{image.get_width()}x{image.get_height()}, expected {width}x{height}"
                    )
                if not image.is_thread_safe():
                    raise ValueError(f"source {source.wid:#x} composite pixels are not thread-safe")
        except BaseException:
            log.error("Error capturing subsurface composition for parent %#x", parent_wid,
                      exc_info=True)
            self._free_subsurface_snapshot_images(captured_images)
            self._finish_subsurface_composite(
                parent_wid, token, requeue_active=True, failed=True,
            )
            return

        with self._damage_packet_lock:
            state = self._subsurface_composites.get(parent_wid)
            stale = (
                not state
                or state["token"] is not token
                or self._damage_packet_closing
                or self._backing_epochs.get(parent_wid, 0) != backing_epoch
                or self._subsurface_topology_epochs.get(parent_wid, 0) != topology_epoch
                or state.get("transaction_id") != transaction_id
                or not self._subsurface_content_generations_current(state)
            )
            if not stale:
                for source, _x, _y, _width, _height, geometry_generation, _target_size in layers:
                    if geometry_generation < 0:
                        active = source is self.window_sources.get(parent_wid)
                    else:
                        active = (
                            source is self.subsurface_sources.get(source.wid)
                            and source.parent_wid == parent_wid
                            and source.geometry_generation == geometry_generation
                        )
                    if not active or self._damage_packet_sources.get(id(source)) is not source:
                        stale = True
                        break
            if not stale:
                state["snapshot_images"] = captured_images
                state["snapshots_ready"] = True
        if stale:
            self._free_subsurface_snapshot_images(captured_images)
            self._finish_subsurface_composite(parent_wid, token, requeue_active=True)
            return
        self._queue_subsurface_composite_stage(
            parent_wid, token, backing_epoch, topology_epoch, transaction_id,
            tuple(layers), 0, True,
        )

    def _queue_subsurface_composite_stage(
            self, parent_wid: int, token: object,
            backing_epoch: int, topology_epoch: int, transaction_id: int,
            stages: tuple[tuple[Any, int, int, int, int, int, tuple[int, int]], ...], index: int,
            reset_pending: bool,
    ) -> bool:
        with self._damage_packet_lock:
            state = self._subsurface_composites.get(parent_wid)
            if not state or state["token"] is not token or self._damage_packet_closing:
                return False
            stale = (
                self._backing_epochs.get(parent_wid, 0) != backing_epoch
                or self._subsurface_topology_epochs.get(parent_wid, 0) != topology_epoch
                or state.get("transaction_id") != transaction_id
                or not state.get("snapshots_ready")
            )
        if stale:
            self._finish_subsurface_composite(parent_wid, token, requeue_active=True)
            return False
        if index >= len(stages):
            self._finish_subsurface_composite(parent_wid, token)
            return False
        source, x, y, width, height, geometry_generation, target_window_size = stages[index]
        with self._damage_packet_lock:
            active = self._damage_packet_sources.get(id(source)) is source
            if geometry_generation < 0:
                active = active and source is self.window_sources.get(parent_wid)
            else:
                active = (
                    active
                    and source is self.subsurface_sources.get(source.wid)
                    and source.parent_wid == parent_wid
                    and source.geometry_generation == geometry_generation
                )
            snapshot_images = state.get("snapshot_images", ())
            captured_image = snapshot_images[index] if index < len(snapshot_images) else None
            active = active and captured_image is not None
            reset_region = state["active_region"]

        completed = False
        completion_lock = RLock()
        timeout_source = None
        stage_sequence = 0

        def complete_stage(published: bool, timed_out: bool = False) -> None:
            nonlocal completed
            with completion_lock:
                if completed:
                    return
                completed = True
                remove_timeout = timeout_source if not timed_out else None
                cancel_sequence = stage_sequence if timed_out else 0
            if remove_timeout:
                self._remove_subsurface_glib_source(
                    remove_timeout, parent_wid, token, "watchdog",
                )
            if cancel_sequence:
                try:
                    source.cancel_damage(cancel_sequence)
                except BaseException:
                    log.error("Error cancelling timed-out subsurface composition stage", exc_info=True)
            self._schedule_subsurface_callback(
                parent_wid, token, self._complete_subsurface_composite_stage,
                backing_epoch, topology_epoch, transaction_id,
                stages, index, reset_pending, published,
            )

        def stage_complete(published: bool) -> None:
            complete_stage(published)

        def stage_timeout() -> bool:
            log.error(
                "Error: subsurface composition stage %s for parent %#x timed out",
                index, parent_wid,
            )
            complete_stage(False, timed_out=True)
            return False

        if not active:
            stage_complete(False)
            return False
        options = {
            "auto_refresh": True,
            "content-types": ("picture",),
            "subsurface-backing-epoch": backing_epoch,
            "subsurface-topology-epoch": topology_epoch,
            "subsurface-transaction-id": transaction_id,
            "subsurface-stage-index": index,
            "subsurface-stage-count": len(stages),
            "subsurface-composite": SUBSURFACE_COMPOSITE_MODE,
            "preserve-premultiplied-alpha": True,
            "window-size": target_window_size,
            "quality": 100,
            "speed": 100,
        }
        if reset_pending:
            options["subsurface-reset"] = reset_region
        try:
            if eligibility_error := self.subsurface_composite_eligibility(parent_wid):
                if not self._refuse_ineligible_subsurface_composite(parent_wid, eligibility_error):
                    stage_complete(False)
                return False
            rgb_formats, eligibility_error = source.subsurface_composite_eligibility()
            if eligibility_error:
                raise ValueError(
                    f"composite source {source.wid:#x} is ineligible: {eligibility_error}"
                )
            options.update({
                "alpha": True,
                "lz4": False,
                "rgb_formats": rgb_formats,
                "zstd": False,
            })
            with self._damage_packet_lock:
                state = self._subsurface_composites.get(parent_wid)
                images = state.get("snapshot_images", ()) if state else ()
                claimable = bool(
                    state
                    and state["token"] is token
                    and not self._damage_packet_closing
                    and self._backing_epochs.get(parent_wid, 0) == backing_epoch
                    and self._subsurface_topology_epochs.get(parent_wid, 0) == topology_epoch
                    and state.get("transaction_id") == transaction_id
                    and state.get("snapshots_ready")
                    and index < len(images)
                    and images[index] is captured_image
                )
                if claimable:
                    images[index] = None
            if not claimable:
                self._finish_subsurface_composite(parent_wid, token, requeue_active=True)
                return False
            coding = "rgb32"
            queued = source.process_damage_region(
                monotonic(), x, y, width, height, coding, options,
                flush=len(stages) - index - 1,
                packet_complete=stage_complete,
                force_basic_picture=True,
                captured_image=captured_image,
            )
        except BaseException:
            log.error("Error queueing subsurface composition stage for source %#x", source.wid, exc_info=True)
            eligibility_error = self.subsurface_composite_eligibility(parent_wid)
            if not eligibility_error or not self._refuse_ineligible_subsurface_composite(
                    parent_wid, eligibility_error,
            ):
                stage_complete(False)
        else:
            if not queued:
                stage_complete(False)
            else:
                with completion_lock:
                    if not completed:
                        stage_sequence = getattr(source, "_sequence", 0)
                        try:
                            timeout_source = self._register_subsurface_glib_source(
                                parent_wid, token, "watchdog",
                                lambda: GLib.timeout_source_new(
                                    SUBSURFACE_COMPOSITE_STAGE_TIMEOUT,
                                ),
                                stage_timeout,
                            )
                            if timeout_source is None:
                                return False
                        except BaseException:
                            log.error(
                                "Error scheduling subsurface composition stage watchdog",
                                exc_info=True,
                            )
                            complete_stage(False)
        return False

    def _complete_subsurface_composite_stage(
            self, parent_wid: int, token: object,
            backing_epoch: int, topology_epoch: int, transaction_id: int,
            stages: tuple[tuple[Any, int, int, int, int, int, tuple[int, int]], ...], index: int,
            reset_pending: bool, published: bool,
    ) -> bool:
        with self._damage_packet_lock:
            state = self._subsurface_composites.get(parent_wid)
            if not state or state["token"] is not token or self._damage_packet_closing:
                return False
            stale = (
                self._backing_epochs.get(parent_wid, 0) != backing_epoch
                or self._subsurface_topology_epochs.get(parent_wid, 0) != topology_epoch
                or state.get("transaction_id") != transaction_id
                or not state.get("snapshots_ready")
            )
        if stale:
            self._finish_subsurface_composite(parent_wid, token, requeue_active=True)
        elif not published:
            self._finish_subsurface_composite(parent_wid, token, requeue_active=True, failed=True)
        else:
            self._queue_subsurface_composite_stage(
                parent_wid, token, backing_epoch, topology_epoch, transaction_id,
                stages, index + 1, False,
            )
        return False

    def _finish_subsurface_composite(
            self, parent_wid: int, token: object,
            *, requeue_active: bool = False, failed: bool = False,
    ) -> None:
        with self._damage_packet_lock:
            state = self._subsurface_composites.get(parent_wid)
            if not state or state["token"] is not token:
                return
            snapshot_images = self._take_subsurface_snapshot_images_locked(state)
            if requeue_active:
                state["pending_region"] = self._merge_damage_regions(
                    state["pending_region"], state["active_region"],
                )
            state["active_region"] = None
            state["running"] = False
            state["transaction_id"] = 0
            state["content_generations"] = ()
            state["failures"] = state["failures"] + 1 if failed else 0
            rerun = bool(state["pending_region"]) and state["failures"] <= 2
            if rerun:
                state["running"] = True
                state["token"] = next_token = object()
            elif not state["pending_region"]:
                self._subsurface_composites.pop(parent_wid, None)
                next_token = None
            else:
                state["token"] = None
                next_token = None
        self._free_subsurface_snapshot_images(snapshot_images)
        if rerun:
            self._schedule_subsurface_callback(
                parent_wid, next_token, self._run_subsurface_composite,
            )

    def get_window_source(self, wid: int):
        return self.window_sources.get(wid)

    def make_window_source(self, wid: int, window):
        if ws := self.window_sources.get(wid):
            return ws
        batch_config = self.make_batch_config(wid, window)
        ww, wh = window.get_dimensions()
        mmap_write_area = getattr(self, "mmap_write_area", None)
        if mmap_write_area and mmap_write_area.enabled:
            bandwidth_limit = 0
        else:
            mmap_write_area = None
            bandwidth_limit = getattr(self, "bandwidth_limit", 0)
        av_sync = getattr(self, "av_sync", False)
        av_sync_delay = getattr(self, "av_sync_delay", 0)
        conn = getattr(self.protocol, "_conn", None)
        socktype = getattr(conn, "socktype_wrapped", "")
        jitter = getattr(conn, "jitter", 0)
        datagram = 1350 if socktype == "quic" else 0
        log(f"datagram({socktype=})={datagram}")
        # pylint: disable=import-outside-toplevel
        from xpra.server.window.video_compress import WindowVideoSource
        ws = WindowVideoSource(
            ww, wh,
            self.record_congestion_event, self.encode_queue_size,
            self.call_in_encode_thread, self.queue_packet,
            self.statistics,
            wid, window, batch_config, self.auto_refresh_delay,
            av_sync, av_sync_delay,
            self.video_helper,
            self.cuda_device_context,
            self.server_core_encodings, self.server_encodings,
            self.encoding, self.encodings, self.core_encodings,
            self.window_icon_encodings, self.encoding_options, self.icons_encoding_options,
            self.rgb_formats,
            self.default_encoding_options,
            mmap_write_area, bandwidth_limit, jitter, datagram)
        try:
            ws.init_encoders()
            active = self._activate_pixel_source(self.window_sources, wid, ws)
            if active is not ws:
                self._discard_pixel_source(self.window_sources, wid, ws, f"duplicate window source {wid:#x}")
                return active
            if len(self.window_sources) > 1:
                # re-distribute bandwidth:
                may_update_bandwidth_limits(self)
            self.emit("new-window-source", ws)
        except BaseException:
            self._discard_pixel_source(self.window_sources, wid, ws, f"window source {wid:#x}")
            if self.window_sources:
                with log.trap_error("Error restoring bandwidth after source construction failure"):
                    may_update_bandwidth_limits(self)
            raise
        return ws

    def make_subsurface_source(self, wid: int, parent_wid: int, offset_x: int, offset_y: int, window,
                               logical_width: int, logical_height: int, native_width: int, native_height: int,
                               parent_window=None):
        if not self.supports_subsurface_composite():
            return None
        if ws := self.subsurface_sources.get(wid):
            self.update_subsurface_geometry(
                wid, parent_wid, offset_x, offset_y,
                logical_width, logical_height, native_width, native_height,
            )
            if eligibility_error := self.subsurface_composite_eligibility(parent_wid):
                raise ValueError(
                    f"composite root {parent_wid:#x} is ineligible: {eligibility_error}"
                )
            return ws
        if parent_window is not None:
            self.make_window_source(parent_wid, parent_window)
        batch_config = self.make_batch_config(wid, window)
        ww, wh = window.get_dimensions()
        mmap_write_area = getattr(self, "mmap_write_area", None)
        if mmap_write_area and mmap_write_area.enabled:
            bandwidth_limit = 0
        else:
            mmap_write_area = None
            bandwidth_limit = getattr(self, "bandwidth_limit", 0)
        av_sync = getattr(self, "av_sync", False)
        av_sync_delay = getattr(self, "av_sync_delay", 0)
        conn = getattr(self.protocol, "_conn", None)
        socktype = getattr(conn, "socktype_wrapped", "")
        jitter = getattr(conn, "jitter", 0)
        datagram = 1350 if socktype == "quic" else 0
        # pylint: disable=import-outside-toplevel
        from xpra.server.window.subsurface_source import SubsurfaceWindowSource
        ws = SubsurfaceWindowSource(
            ww, wh,
            self.record_congestion_event, self.encode_queue_size,
            self.call_in_encode_thread, self.queue_packet,
            self.statistics,
            wid, window, batch_config, self.auto_refresh_delay,
            av_sync, av_sync_delay,
            self.video_helper,
            self.cuda_device_context,
            self.server_core_encodings, self.server_encodings,
            self.encoding, self.encodings, self.core_encodings,
            self.window_icon_encodings, self.encoding_options, self.icons_encoding_options,
            self.rgb_formats,
            self.default_encoding_options,
            mmap_write_area, bandwidth_limit, jitter, datagram,
            parent_wid=parent_wid, offset_x=offset_x, offset_y=offset_y,
            logical_width=logical_width, logical_height=logical_height,
            native_width=native_width, native_height=native_height,
        )
        try:
            ws.init_encoders()
            self.apply_subsurface_client_properties(ws, parent_wid)
            active = self._activate_pixel_source(self.subsurface_sources, wid, ws)
            if active is not ws:
                if eligibility_error := self.subsurface_composite_eligibility(parent_wid):
                    raise ValueError(
                        f"composite root {parent_wid:#x} is ineligible: {eligibility_error}"
                    )
                self._discard_pixel_source(
                    self.subsurface_sources, wid, ws, f"duplicate subsurface source {wid:#x}",
                )
                return active
            if eligibility_error := self.subsurface_composite_eligibility(parent_wid):
                raise ValueError(
                    f"composite root {parent_wid:#x} is ineligible: {eligibility_error}"
                )
        except BaseException:
            self._discard_pixel_source(self.subsurface_sources, wid, ws, f"subsurface source {wid:#x}")
            raise
        return ws

    def cleanup_subsurface_source(self, wid: int) -> None:
        with self._damage_packet_lock:
            ws = self.subsurface_sources.pop(wid, None)
            if ws:
                repaint = (
                    ws.parent_wid, ws.offset_x, ws.offset_y,
                    ws.logical_width, ws.logical_height,
                )
                order = self.subsurface_stacking.get(ws.parent_wid, ())
                self.subsurface_stacking[ws.parent_wid] = tuple(value for value in order if value != wid)
                repair_token = self._record_subsurface_composite_locked(
                    ws.parent_wid, repaint[1:], topology_change=True,
                )
            else:
                repaint = ()
                repair_token = None
        cleanup_errors: list[tuple[BaseException, Any]] = []
        if ws:
            try:
                self.unregister_damage_packets(ws)
            except BaseException as e:
                cleanup_errors.append((e, e.__traceback__))
            try:
                ws.cleanup()
            except BaseException as e:
                cleanup_errors.append((e, e.__traceback__))
        self.calculate_window_pixels.pop(wid, None)
        # The topology epoch was invalidated atomically with detachment.  Run
        # the old-footprint repair after terminal source cleanup has drained.
        if repair_token:
            self._schedule_subsurface_callback(
                repaint[0], repair_token, self._run_subsurface_composite,
                errors=cleanup_errors,
            )
        self._raise_pixel_cleanup_errors(f"subsurface {wid:#x} removal", cleanup_errors)

    def damage(self, wid: int, window, x: int, y: int, w: int, h: int, options=None) -> bool:
        """
            Main entry point from the window manager,
            we dispatch to the WindowSource for this window id
            (creating a new one if needed)
        """
        if not self.can_send_window_model(wid, window):
            return False
        if self.is_window_hidden(window):
            # `sharing=combine`: this window is not on this client's area of the display,
            # it is minimized there, so there is no point in sending it any pixels
            sharinglog("damage(%s) ignored: hidden from %s", window, self)
            return False
        assert window is not None
        if options:
            damage_options = options.copy()
        else:
            damage_options = {}
        if s := self.statistics:
            s.damage_last_events.append((wid, monotonic(), w * h))
        ws = self.make_window_source(wid, window)
        if self.get_subsurface_sources(wid):
            return self.request_subsurface_composite(wid, parent_region=(x, y, w, h))
        return ws.damage(x, y, w, h, damage_options)

    def client_ack_damage(self, damage_packet_sequence: int, wid: int,
                          width: int, height: int, decode_time: int, message: str) -> None:
        """
            The client is acknowledging a damage packet,
            we record the 'client decode time' (which is provided by the client)
            and WindowSource will calculate and record the "client latency".
            (since it knows when the "draw" packet was sent)
        """
        if not self.window_enabled:
            log.error("Error: draw ack received for window %#x, but it is not enabled!")
            return
        with self._damage_packet_condition:
            ws = self._damage_packet_owners.pop((wid, damage_packet_sequence), None)
            if ws:
                self._begin_damage_packet_operation_locked(ws)
        if not ws:
            return
        try:
            if decode_time > 0:
                try:
                    self.statistics.client_decode_time.append((wid, monotonic(), width * height, decode_time))
                except BaseException:
                    log.error("Error recording connection draw acknowledgement statistics", exc_info=True)
            if ws.wid != wid:
                try:
                    damagelog(
                        "draw acknowledgement sequence %s for wire window %#x routed to subsurface window %#x",
                        damage_packet_sequence, wid, ws.wid,
                    )
                except BaseException:
                    log.error("Error recording routed draw acknowledgement", exc_info=True)
            try:
                ws.damage_packet_acked(damage_packet_sequence, width, height, decode_time, message)
            finally:
                try:
                    self.may_recalculate(ws.wid, width * height)
                except BaseException:
                    log.error("Error recalculating window statistics after draw acknowledgement", exc_info=True)
        finally:
            with self._damage_packet_condition:
                self._finish_damage_packet_operation_locked(ws)

    #
    # Methods used by WindowSource:
    #
    def record_congestion_event(self, source, late_pct: int = 0, send_speed: int = 0) -> None:
        if not getattr(self, "bandwidth_detection", False):
            return
        gs = self.statistics
        if not gs:
            # window cleaned up?
            return
        now = monotonic()
        elapsed = now - self.bandwidth_warning_time
        bandwidthlog("record_congestion_event(%s, %i, %i) bandwidth_warnings=%s, elapsed time=%i",
                     source, late_pct, send_speed, self.bandwidth_warnings, elapsed)
        gs.last_congestion_time = now
        gs.congestion_send_speed.append((now, late_pct, send_speed))
        if self.bandwidth_warnings and elapsed > CONGESTION_REPEAT_DELAY:
            # enough congestion events?
            T = 10
            min_time = now - T
            count = sum(int(x[0] > min_time) for x in gs.congestion_send_speed)
            bandwidthlog("record_congestion_event: %i events in the last %i seconds (warnings after %i)",
                         count, T, CONGESTION_WARNING_EVENT_COUNT)
            if count > CONGESTION_WARNING_EVENT_COUNT:
                self.bandwidth_warning_time = now
                nid = NotificationID.BANDWIDTH
                summary = "Network Performance Issue"
                body = "\n".join((
                    "Your network connection is struggling to keep up,",
                    "consider lowering the bandwidth limit,",
                    "or turning off automatic network congestion management.",
                    "Choosing 'ignore' will silence all further warnings.",
                ))
                actions = []
                if self.bandwidth_limit == 0 or self.bandwidth_limit > MIN_BANDWIDTH:
                    actions += ["lower-bandwidth", "Lower bandwidth limit"]
                actions += ["bandwidth-off", "Turn off"]
                # if self.default_min_quality>10:
                #    actions += ["lower-quality", "Lower quality"]
                actions += ["ignore", "Ignore"]
                hints = {}
                may_notify_client(self, nid, summary, body, actions, hints,
                                  icon_name="connect", user_callback=self.congestion_notification_callback)

    def congestion_notification_callback(self, nid: int, action_id: str) -> None:
        bandwidthlog("congestion_notification_callback(%i, %s)", nid, action_id)
        if action_id == "lower-bandwidth":
            bandwidth_limit = 50 * 1024 * 1024
            if self.bandwidth_limit > 256 * 1024:
                bandwidth_limit = self.bandwidth_limit // 2
            css = 50 * 1024 * 1024
            if self.statistics.avg_congestion_send_speed > 256 * 1024:
                # round up:
                css = int(self.statistics.avg_congestion_send_speed // 16 / 1024) * 16 * 1024
            self.bandwidth_limit = max(MIN_BANDWIDTH, min(bandwidth_limit, css))
            self.setting_changed("bandwidth-limit", self.bandwidth_limit)
        # elif action_id=="lower-quality":
        #    self.default_min_quality = max(1, self.default_min_quality-15)
        #    self.set_min_quality(self.default_min_quality)
        #    self.setting_changed("min-quality", self.default_min_quality)
        elif action_id == "bandwidth-off":
            self.bandwidth_detection = False
        elif action_id == "ignore":
            self.bandwidth_warnings = False
