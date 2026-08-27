#!/usr/bin/env python3
"""Decode TrueColor XWD captures without discarding depth-32 alpha evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
from pathlib import Path

from PIL import Image


def component(pixel: int, mask: int) -> int:
    if mask == 0:
        return 0
    shift = (mask & -mask).bit_length() - 1
    maximum = mask >> shift
    return ((pixel & mask) >> shift) * 255 // maximum


def decode_xwd(path: Path) -> tuple[Image.Image, dict[str, int | float | str]]:
    """Return an RGBA image and deterministic XWD visual evidence."""
    data = path.read_bytes()
    if len(data) < 100:
        raise ValueError("XWD capture is shorter than its fixed header")
    header = struct.unpack(">25I", data[:100])
    (
        header_size,
        file_version,
        _pixmap_format,
        depth,
        width,
        height,
        xoffset,
        byte_order,
        _bitmap_unit,
        _bitmap_bit_order,
        _bitmap_pad,
        bits_per_pixel,
        bytes_per_line,
        visual_class,
        red_mask,
        green_mask,
        blue_mask,
        _bits_per_rgb,
        _colormap_entries,
        ncolors,
        _window_width,
        _window_height,
        _window_x,
        _window_y,
        _border_width,
    ) = header
    bytes_per_pixel = bits_per_pixel // 8
    pixel_offset = header_size + ncolors * 12
    required = pixel_offset + bytes_per_line * height
    if (
        file_version != 7
        or width < 1
        or height < 1
        or bytes_per_pixel not in (2, 3, 4)
        or byte_order not in (0, 1)
        or required > len(data)
    ):
        raise ValueError("XWD capture uses an unsupported layout")

    storage_mask = (1 << bits_per_pixel) - 1
    rgb_mask = red_mask | green_mask | blue_mask
    unused_mask = storage_mask & ~rgb_mask
    alpha_mask = unused_mask if depth == 32 else 0
    alpha_kind = "alpha" if alpha_mask else ("padding" if unused_mask else "none")
    endian = "little" if byte_order == 0 else "big"
    rgba = bytearray(width * height * 4)
    rgb = bytearray(width * height * 3)
    rgb_colors: set[tuple[int, int, int]] = set()
    alpha_values: list[int] = []
    rgba_offset = 0
    rgb_offset = 0
    for y in range(height):
        row = pixel_offset + y * bytes_per_line
        for x in range(width):
            start = row + (x + xoffset) * bytes_per_pixel
            pixel = int.from_bytes(data[start : start + bytes_per_pixel], endian)
            color = (
                component(pixel, red_mask),
                component(pixel, green_mask),
                component(pixel, blue_mask),
            )
            alpha = component(pixel, alpha_mask) if alpha_mask else 255
            rgba[rgba_offset : rgba_offset + 4] = bytes((*color, alpha))
            rgb[rgb_offset : rgb_offset + 3] = bytes(color)
            rgb_colors.add(color)
            alpha_values.append(alpha)
            rgba_offset += 4
            rgb_offset += 3

    opaque_pixels = sum(value == 255 for value in alpha_values)
    transparent_pixels = sum(value == 0 for value in alpha_values)
    pixel_count = width * height
    evidence: dict[str, int | float | str] = {
        "alpha_kind": alpha_kind,
        "alpha_mask": f"0x{alpha_mask:x}",
        "alpha_max": max(alpha_values),
        "alpha_min": min(alpha_values),
        "bits_per_pixel": bits_per_pixel,
        "blue_mask": f"0x{blue_mask:x}",
        "depth": depth,
        "green_mask": f"0x{green_mask:x}",
        "height": height,
        "opaque_ratio": opaque_pixels / pixel_count,
        "red_mask": f"0x{red_mask:x}",
        "rgb_sha256": hashlib.sha256(rgb).hexdigest(),
        "rgba_sha256": hashlib.sha256(rgba).hexdigest(),
        "transparent_ratio": transparent_pixels / pixel_count,
        "unique_rgb_colors": len(rgb_colors),
        "unused_mask": f"0x{unused_mask:x}",
        "visual_class": visual_class,
        "width": width,
    }
    return Image.frombytes("RGBA", (width, height), bytes(rgba)), evidence


def save_alpha_visualization(image: Image.Image, destination: Path) -> None:
    """Save alpha as a greyscale image so invisible content is reviewable."""
    image.getchannel("A").save(destination, format="PNG")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--alpha-output",
        type=Path,
        help="optional greyscale PNG containing the depth-32 alpha channel",
    )
    args = parser.parse_args()
    image, evidence = decode_xwd(args.input)
    image.save(args.output, format="PNG")
    if args.alpha_output:
        save_alpha_visualization(image, args.alpha_output)
    print(json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
