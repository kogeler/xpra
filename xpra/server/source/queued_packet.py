# This file is part of Xpra.
# Copyright (C) 2026 kogeler
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

from collections.abc import Sequence

from xpra.net.common import Packet, PacketElement
from xpra.net.packet_type import WINDOW_DRAW


def queued_draw_packet(packet: Sequence[PacketElement]) -> Packet | None:
    """Narrow the mixed outgoing queue before calling draw-specific accessors.

    Clipboard, icons and EOS may be plain sequences. They are not damage and
    must retain their original representation, ownership and batching flags.
    Sequence-shaped draws must still undergo the caller's stale-frame checks.
    """
    if not packet or packet[0] != WINDOW_DRAW:
        return None
    if isinstance(packet, Packet):
        return packet
    return Packet(WINDOW_DRAW, *packet[1:])
