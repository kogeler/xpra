# Copyright (C) 2026 kogeler
"""Recovery-only oracle; never substitutes picture frames for H.264 proof."""

import ast
import json
import re

from decoder_loss_fixture import MARKER


def viewport_size(server_info: str, window_id: int) -> list[int]:
    sizes = re.findall(rf"(?m)^windows\.{window_id}\.size=(.+)$", server_info)
    if len(sizes) != 1:
        raise ValueError("recovery capture-phase viewport is missing or ambiguous")
    value = ast.literal_eval(sizes[0])
    if not isinstance(value, (tuple, list)) or len(value) != 2 or any(type(n) is not int or n <= 0 for n in value):
        raise ValueError("invalid recovery capture-phase viewport")
    return list(value)


def evaluate(*, client_log: str, server_log: str, server_info: str,
             packets: list[dict], window_id: int, client_pid: int) -> dict:
    faults = []
    installed = client_log.find("seccomp installed=True")
    fault_positions = []
    position = 0
    for line in client_log.splitlines(keepends=True):
        if line.startswith(MARKER):
            event = json.loads(line[len(MARKER):])
            if (not isinstance(event, dict) or event.get("schema") != 1
                    or event.get("event") != "injected-init-failure" or event.get("encoding") != "h264"
                    or event.get("thread") != "decode" or type(event.get("pid")) is not int
                    or event["pid"] != client_pid or event.get("colorspace") != "YUV420P"
                    or any(type(event.get(key)) is not int or event[key] <= 0
                           for key in ("width", "height", "monotonic_ns"))):
                raise ValueError("invalid live decoder fault record")
            faults.append(event)
            fault_positions.append(position)
        position += len(line)

    properties = []
    for match in re.finditer(r"do_set_client_properties\(typedict\((\{[^\r\n]*\})\)\)", server_log):
        value = ast.literal_eval(match[1])
        modes = value.get("encoding.full_csc_modes") if isinstance(value, dict) else None
        if isinstance(modes, dict) and "h264" in modes:
            properties.append(value)
    initially_available = any(value["encoding.full_csc_modes"]["h264"] for value in properties)
    deltas = [value for value in properties if value["encoding.full_csc_modes"]["h264"] in ([], ())]

    fields = {}
    for line in server_info.splitlines():
        key, separator, value = line.partition("=")
        if separator and re.fullmatch(rf"client\.[0-9]+\.window\.windows\.{window_id}\.encodings\.(?:video|csc_modes\.h264)", key):
            suffix = key.split(".encodings.", 1)[1]
            if suffix in fields:
                raise ValueError("ambiguous recovered server window")
            fields[suffix] = ast.literal_eval(value)

    video = [packet for packet in packets if packet.get("encoding") == "h264"]
    picture = [packet for packet in packets if packet.get("encoding") in {"rgb24", "rgb32", "webp"}]
    first_video = min((packet["sequence"] for packet in video), default=None)
    full_pictures = []
    for packet in picture:
        size = packet.get("options", {}).get("window-size")
        if (isinstance(size, (tuple, list)) and len(size) == 2
                and all(type(value) is int and value > 0 for value in size)
                and packet.get("x") == packet.get("y") == 0
                and packet.get("w") == size[0] and packet.get("h") == size[1]
                and first_video is not None and packet["sequence"] > first_video):
            full_pictures.append(packet)
    cutoff = min((packet["sequence"] for packet in full_pictures), default=None)
    fault_start = min(fault_positions) if fault_positions else len(client_log)
    ack = re.search(rf"record_decode_time\((?:True|1),[^\n]*\) wid=0x{window_id:x}, (?:rgb24|rgb32|webp):",
                    client_log[fault_start:])
    checks = {
        "real_default_filter_before_fault": installed >= 0 and bool(fault_positions) and installed < min(fault_positions),
        "one_permanent_live_failure": len(faults) == 1,
        "h264_was_advertised_and_received": initially_available and bool(video),
        "minimal_capability_delta_received": bool(deltas) and all(set(value) == {"encoding.full_csc_modes"} for value in deltas),
        "server_retired_h264": fields.get("csc_modes.h264") in ([], ())
                               and isinstance(fields.get("video"), (tuple, list)) and "h264" not in fields["video"],
        "subsequent_full_picture_frames": len(full_pictures) >= 3,
        "no_h264_after_picture_recovery": cutoff is not None and not any(packet["sequence"] > cutoff for packet in video),
        "picture_decode_acknowledged": bool(ack),
        # An old in-flight video packet may fail before the new peer state
        # takes effect. Once picture decode succeeds, no further error loop
        # is permitted. Keep the complete logs; do not globally ignore it.
        "no_invalid_encoding_after_picture_ack": bool(ack) and "invalid encoding:" not in client_log[fault_start + ack.end():],
    }
    # Literal log values contain tuples; persisted JSON contains arrays. The
    # worker and independent collector must compare the same wire-neutral form.
    return {"faults": faults, "capability_deltas": json.loads(json.dumps(deltas)), "viewport_size": viewport_size(server_info, window_id),
            "first_full_picture_sequence": cutoff, "full_picture_count": len(full_pictures), "checks": checks}
