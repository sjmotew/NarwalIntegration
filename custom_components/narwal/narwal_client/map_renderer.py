"""Map renderer for Narwal vacuum — converts raw map data to PNG bytes.

Pure Python module with no Home Assistant dependencies.
Uses Pillow for image rendering.

Map data format (confirmed from live robot data):
  - Compressed with standard zlib (header 78 01)
  - Decompressed data is a protobuf message: field 1 = packed repeated varints
  - Skip 4-byte protobuf header, then decode varints
  - Each varint encodes: room_id = value >> 8, pixel_type = value & 0xFF
  - Value 0 = unknown/outside, 0x20 = unassigned floor, 0x28 = unassigned obstacle
  - pixel_type & 0x10 = wall/border edge (darken the room color)
"""

from __future__ import annotations

import io
import logging
import zlib

_LOGGER = logging.getLogger(__name__)

# Room color palette (RGB) — up to 22 rooms
ROOM_COLORS: list[tuple[int, int, int]] = [
    (100, 149, 237),  # 1 - cornflower blue
    (144, 238, 144),  # 2 - light green
    (255, 182, 193),  # 3 - light pink
    (255, 218, 185),  # 4 - peach
    (221, 160, 221),  # 5 - plum
    (176, 224, 230),  # 6 - powder blue
    (255, 255, 150),  # 7 - light yellow
    (188, 143, 143),  # 8 - rosy brown
    (152, 251, 152),  # 9 - pale green
    (135, 206, 250),  # 10 - light sky blue
    (240, 128, 128),  # 11 - light coral
    (216, 191, 216),  # 12 - thistle
    (250, 250, 210),  # 13 - light goldenrod
    (173, 216, 230),  # 14 - light blue
    (244, 164, 96),   # 15 - sandy brown
    (245, 222, 179),  # 16 - wheat
    (127, 255, 212),  # 17 - aquamarine
    (255, 160, 122),  # 18 - light salmon
    (186, 218, 160),  # 19 - light green 2
    (255, 228, 196),  # 20 - bisque
    (200, 162, 200),  # 21 - light purple
    (174, 198, 207),  # 22 - pastel blue
]

# Special pixel colors
COLOR_UNKNOWN = (40, 40, 40)         # outside map / unmapped
COLOR_UNASSIGNED_FLOOR = (200, 200, 200)  # floor not assigned to a room
COLOR_UNASSIGNED_OBSTACLE = (80, 80, 80)  # obstacle not in a room
COLOR_FALLBACK = (180, 180, 180)     # unknown room ID


def decompress_map(compressed: bytes) -> bytes:
    """Decompress map grid data using zlib.

    Args:
        compressed: Raw compressed bytes from the robot (zlib format, header 78 01).

    Returns:
        Decompressed bytes containing protobuf-wrapped pixel varints.
    """
    if not compressed:
        return b""

    # Try zlib auto-detect (wbits=47 handles zlib, gzip, and raw)
    try:
        return zlib.decompress(compressed, 47)
    except zlib.error:
        pass

    # Try zlib default
    try:
        return zlib.decompress(compressed)
    except zlib.error:
        pass

    # Try raw deflate
    try:
        return zlib.decompress(compressed, -15)
    except zlib.error:
        pass

    _LOGGER.warning(
        "Could not decompress map data (%d bytes), using raw", len(compressed)
    )
    return compressed


def _decode_packed_varints(data: bytes) -> list[int]:
    """Decode protobuf packed repeated varint field from decompressed map data.

    The decompressed data starts with a protobuf field header:
      byte 0: 0x0a (field 1, wire type 2 = length-delimited)
      bytes 1-3: varint length of the packed data

    After the header, the remaining bytes are packed varint pixel values.

    Args:
        data: Decompressed bytes from decompress_map().

    Returns:
        List of integer pixel values.
    """
    if len(data) < 4:
        return []

    # Skip protobuf header: field tag (1 byte) + length varint (variable)
    pos = 0
    if data[0] == 0x0A:  # field 1, wire type 2
        pos = 1
        # Skip the length varint
        while pos < len(data) and data[pos] & 0x80:
            pos += 1
        pos += 1  # skip the final byte of the length varint
    # else: try decoding from the start (no header)

    pixels: list[int] = []
    while pos < len(data):
        val = 0
        shift = 0
        while pos < len(data):
            b = data[pos]
            pos += 1
            val |= (b & 0x7F) << shift
            shift += 7
            if not (b & 0x80):
                break
        pixels.append(val)

    return pixels


def _darken(color: tuple[int, int, int], amount: int = 80) -> tuple[int, int, int]:
    """Darken an RGB color by subtracting from each channel."""
    return (
        max(0, color[0] - amount),
        max(0, color[1] - amount),
        max(0, color[2] - amount),
    )


def _draw_dock(
    draw: "ImageDraw.ImageDraw",
    dock_x: int,
    dock_y: int,
    size: int = 6,
) -> None:
    """Draw a dock/charging station icon at the given grid coordinates.

    Renders as a small white filled circle (matching the Narwal app style).
    """
    radius = size // 2
    draw.ellipse(
        [dock_x - radius, dock_y - radius, dock_x + radius, dock_y + radius],
        fill=(255, 255, 255),
        outline=(180, 180, 180),
    )


def render_map_png(
    decompressed: bytes,
    width: int,
    height: int,
    robot_x: float | None = None,
    robot_y: float | None = None,
    robot_heading: float | None = None,
    dock_x: float | None = None,
    dock_y: float | None = None,
) -> bytes:
    """Render decompressed map data as a PNG image.

    Decodes the protobuf-packed varint pixel data and renders each pixel:
      - Value 0: unknown/outside (dark gray)
      - Value 0x20: unassigned floor (light gray)
      - Value 0x28: unassigned obstacle (dark gray)
      - Otherwise: room_id = value >> 8, pixel_type = value & 0xFF
        - pixel_type & 0x10: wall/border (darker shade of room color)
        - else: floor (room color)

    Args:
        decompressed: Decompressed map bytes (from decompress_map).
        width: Map width in pixels.
        height: Map height in pixels.
        robot_x: Robot X position in grid coordinates (optional).
        robot_y: Robot Y position in grid coordinates (optional).
        robot_heading: Robot heading in degrees (optional).

    Returns:
        PNG image as bytes, or empty bytes on failure.
    """
    if not decompressed or width <= 0 or height <= 0:
        return b""

    try:
        from PIL import Image, ImageDraw
    except ImportError:
        _LOGGER.error("Pillow is required for map rendering")
        return b""

    pixels = _decode_packed_varints(decompressed)
    expected = width * height

    if len(pixels) < expected:
        _LOGGER.warning(
            "Map has %d pixels, expected %d (%dx%d) — padding",
            len(pixels), expected, width, height,
        )
        pixels.extend([0] * (expected - len(pixels)))
    elif len(pixels) > expected:
        pixels = pixels[:expected]

    img = Image.new("RGB", (width, height), COLOR_UNKNOWN)
    px = img.load()

    for i, val in enumerate(pixels):
        x = i % width
        y = i // width

        if val == 0:
            continue  # already set to COLOR_UNKNOWN
        elif val == 0x20:
            px[x, y] = COLOR_UNASSIGNED_FLOOR
        elif val == 0x28:
            px[x, y] = COLOR_UNASSIGNED_OBSTACLE
        else:
            room_id = val >> 8
            ptype = val & 0xFF

            if 1 <= room_id <= len(ROOM_COLORS):
                base = ROOM_COLORS[room_id - 1]
            else:
                base = COLOR_FALLBACK

            if ptype & 0x10:  # wall/border edge
                px[x, y] = _darken(base)
            else:
                px[x, y] = base

    # Draw dock position (before robot so robot draws on top)
    if dock_x is not None and dock_y is not None:
        draw = ImageDraw.Draw(img)
        dock_size = max(4, min(width, height) // 60)
        _draw_dock(draw, int(dock_x), int(dock_y), dock_size)

    # Draw robot position
    if robot_x is not None and robot_y is not None:
        draw = ImageDraw.Draw(img)
        rx, ry = int(robot_x), int(robot_y)
        radius = max(3, min(width, height) // 80)
        draw.ellipse(
            [rx - radius, ry - radius, rx + radius, ry + radius],
            fill=(0, 120, 255),
            outline=(255, 255, 255),
        )

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def render_map_from_compressed(
    compressed: bytes,
    width: int,
    height: int,
    robot_x: float | None = None,
    robot_y: float | None = None,
    robot_heading: float | None = None,
    dock_x: float | None = None,
    dock_y: float | None = None,
) -> bytes:
    """Decompress and render map data in one step.

    Args:
        compressed: Compressed map bytes from the robot.
        width: Map width in pixels.
        height: Map height in pixels.
        robot_x: Robot X position (optional).
        robot_y: Robot Y position (optional).
        robot_heading: Robot heading in degrees (optional).
        dock_x: Dock X position (optional).
        dock_y: Dock Y position (optional).

    Returns:
        PNG image as bytes, or empty bytes on failure.
    """
    decompressed = decompress_map(compressed)
    return render_map_png(
        decompressed, width, height, robot_x, robot_y, robot_heading, dock_x, dock_y
    )
