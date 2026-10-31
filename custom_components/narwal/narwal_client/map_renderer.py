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
import math
import zlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL import Image, ImageDraw

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

# Obstacle/furniture annotation colors by catalog from APK map_furniture.json
OBSTACLE_COLORS: dict[int, tuple[int, int, int]] = {
    # Beds (1-3)
    1: (180, 140, 100),    # single bed - tan
    2: (180, 140, 100),    # double bed - tan
    3: (180, 140, 100),    # baby bed - tan
    # Tables (4-7, 31)
    4: (160, 130, 90),     # dining table - brown
    5: (160, 130, 90),     # round table - brown
    6: (160, 130, 90),     # tea table - brown
    7: (160, 130, 90),     # round tea table - brown
    31: (160, 130, 90),    # desk - brown
    # Cupboards/storage (8-12)
    8: (140, 120, 100),    # TV stand - dark tan
    9: (140, 120, 100),    # bedside table - dark tan
    10: (140, 120, 100),   # locker - dark tan
    11: (140, 120, 100),   # wardrobe - dark tan
    12: (140, 120, 100),   # shoe cabinet - dark tan
    # Sofas/chairs (13-18, 30)
    13: (100, 160, 130),   # armchair - sage
    14: (100, 160, 130),   # sofa - sage
    15: (100, 160, 130),   # L-shaped sofa - sage
    16: (100, 160, 130),   # lazy chair - sage
    17: (100, 160, 130),   # chair - sage
    18: (100, 160, 130),   # bar chair - sage
    30: (100, 160, 130),   # U-shaped sofa - sage
    # Pets (19-21, 75-76)
    19: (200, 160, 120),   # cat toilet - peach
    20: (200, 160, 120),   # pet feeder - peach
    21: (200, 160, 120),   # pet house - peach
    75: (200, 160, 120),   # cat house - peach
    76: (200, 160, 120),   # dog house - peach
    # Appliances (22-25, 34)
    22: (150, 180, 200),   # washing machine - steel blue
    23: (150, 180, 200),   # refrigerator - steel blue
    24: (150, 180, 200),   # air conditioner - steel blue
    25: (150, 180, 200),   # fan - steel blue
    34: (150, 180, 200),   # stove - steel blue
    # Bathroom (28, 33)
    28: (120, 180, 220),   # toilet - light blue
    33: (120, 180, 220),   # washbasin - light blue
    # Misc (26-27, 29, 32, 77-78)
    26: (100, 180, 100),   # potted plant - green
    27: (200, 200, 220),   # floor mirror - silver
    29: (80, 80, 80),      # piano - dark gray
    32: (80, 80, 80),      # grand piano - dark gray
    77: (200, 200, 200),   # round placeholder - gray
    78: (200, 200, 200),   # weighing scale - gray
}
OBSTACLE_COLOR_DEFAULT = (200, 200, 200)

# Special pixel colors
COLOR_UNKNOWN = (0, 0, 0, 0)         # outside map / unmapped, transparent
COLOR_UNASSIGNED_FLOOR = (200, 200, 200)  # floor not assigned to a room
COLOR_UNASSIGNED_OBSTACLE = (80, 80, 80)  # obstacle not in a room
COLOR_FALLBACK = (180, 180, 180)     # unknown room ID
MAP_RENDER_SCALE = 3
ROOM_LABEL_FONT_SCALE = 10
OBSTACLE_LABEL_FONT_SCALE = 6
FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-SemiBold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "arial.ttf",
)


def _opaque(color: tuple[int, int, int]) -> tuple[int, int, int, int]:
    """Return an RGB color with a fully opaque alpha channel."""
    return (*color, 255)


def _load_font(image_font: object, size: int):
    """Load a crisp TrueType font, falling back to Pillow's default font."""
    for path in FONT_PATHS:
        try:
            return image_font.truetype(path, size)
        except OSError:
            continue
    return image_font.load_default()


def _grid_to_pixel(value: float, scale: float) -> float:
    """Return the unclamped image coordinate of a grid cell centre."""
    return value * scale + (scale / 2)


def _scaled_coord(value: float, scale: float, size: int) -> int:
    """Return a scaled grid coordinate centred in the rendered map cell."""
    coordinate = int(round(_grid_to_pixel(value, scale)))
    return max(0, min(coordinate, size - 1))


def _draw_label(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: object,
    *,
    fill: tuple[int, int, int] = (255, 255, 255),
    stroke_fill: tuple[int, int, int] = (20, 20, 20),
    padding: int = 4,
) -> None:
    """Draw a readable centred map label."""
    cx, cy = xy
    bbox = font.getbbox(text)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    tx = cx - tw // 2
    ty = cy - th // 2
    bg = [
        tx - padding,
        ty - padding,
        tx + tw + padding,
        ty + th + padding,
    ]
    draw.rounded_rectangle(bg, radius=padding * 2, fill=(0, 0, 0, 120))
    draw.text(
        (tx, ty),
        text,
        fill=fill,
        font=font,
        stroke_width=max(1, padding // 2),
        stroke_fill=stroke_fill,
    )


def _normalize_rotation(rotation_degrees: int) -> int:
    """Return a rotation supported by both bitmap and point transforms."""
    rotation = int(rotation_degrees or 0) % 360
    return rotation if rotation in (0, 90, 180, 270) else 0


def _transform_point(
    x: float,
    y: float,
    width: int,
    height: int,
    rotation_degrees: int,
    zoom: float,
) -> tuple[float, float] | None:
    """Transform an unrotated image-space point into final view coordinates."""
    rotation = _normalize_rotation(rotation_degrees)
    if rotation == 0:
        rx, ry = x, y
        rotated_width, rotated_height = width, height
    elif rotation == 90:
        rx, ry = height - 1 - y, x
        rotated_width, rotated_height = height, width
    elif rotation == 180:
        rx, ry = width - 1 - x, height - 1 - y
        rotated_width, rotated_height = width, height
    elif rotation == 270:
        rx, ry = y, width - 1 - x
        rotated_width, rotated_height = height, width
    zoom = max(1.0, min(2.0, float(zoom or 1.0)))
    crop_width = max(1, int(rotated_width / zoom))
    crop_height = max(1, int(rotated_height / zoom))
    left = max(0, (rotated_width - crop_width) // 2)
    top = max(0, (rotated_height - crop_height) // 2)
    return rx - left, ry - top


def calibration_points(
    height: int,
    image_width: int,
    image_height: int,
    rotation_degrees: int = 0,
    zoom: float = 1.0,
) -> list[dict[str, dict[str, float]]]:
    """Return three grid-to-image calibration points for a rendered map.

    Vacuum map cards derive an affine transform from these points, so they can
    place the robot, the dock or a path on the camera image. The ``vacuum``
    side is in map grid coordinates (x right, y up, the units of the robot
    position); the ``map`` side is in pixels of the final image, after the same
    rotation and zoom ``render_overlay`` applies.

    Args:
        height: Map height in grid cells (for the Y-flip).
        image_width: Width of the unrotated rendered base map in pixels.
        image_height: Height of the unrotated rendered base map in pixels.
        rotation_degrees: Clockwise map rotation in degrees.
        zoom: Centre zoom factor.
    """
    scale = image_height / height if height > 0 else 1.0
    width = image_width / scale if scale > 0 else image_width
    points: list[dict[str, dict[str, float]]] = []
    for grid_x, grid_y in ((0.0, 0.0), (float(width), 0.0), (0.0, float(height))):
        point = _transform_point(
            _grid_to_pixel(grid_x, scale),
            _grid_to_pixel(height - 1 - grid_y, scale),
            image_width,
            image_height,
            rotation_degrees,
            zoom,
        )
        if point is None:
            return []
        map_x, map_y = point
        points.append(
            {
                "vacuum": {"x": round(grid_x, 2), "y": round(grid_y, 2)},
                "map": {"x": round(map_x, 2), "y": round(map_y, 2)},
            }
        )
    return points


def _rotated_heading(heading: float | None, rotation_degrees: int) -> float | None:
    """Rotate robot heading to match the final clockwise map rotation."""
    if heading is None:
        return None
    return (heading - _normalize_rotation(rotation_degrees)) % 360


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


def lookup_room_at_grid(
    compressed: bytes,
    width: int,
    height: int,
    grid_x: float,
    grid_y: float,
) -> tuple[int, str]:
    """Look up the room_id at a grid pixel coordinate.

    Returns (room_id, description) where description is one of:
      "room_N" for a valid room, "(empty)" for val=0,
      "(unassigned)" for 0x20/0x28, "(out_of_bounds)" if off grid.
    """
    px = int(grid_x)
    py = int(grid_y)
    if px < 0 or px >= width or py < 0 or py >= height:
        return (-1, f"(out_of_bounds: {px},{py} vs {width}x{height})")

    decompressed = decompress_map(compressed)
    if not decompressed:
        return (-1, "(no_data)")
    pixels = _decode_packed_varints(decompressed)

    idx = py * width + px
    if idx >= len(pixels):
        return (-1, f"(idx_overflow: {idx} >= {len(pixels)})")

    val = pixels[idx]
    if val == 0:
        return (0, "(empty)")
    if val in (0x20, 0x28):
        return (0, "(unassigned)")
    room_id = val >> 8
    ptype = val & 0xFF
    wall = " wall" if ptype & 0x10 else ""
    return (room_id, f"room_{room_id}{wall}")


def room_label_points(
    compressed: bytes,
    width: int,
    height: int,
    room_names: dict[int, str] | None,
) -> list[tuple[str, float, float]]:
    """Return room label centre points in unflipped grid coordinates."""
    if not compressed or width <= 0 or height <= 0 or not room_names:
        return []

    decompressed = decompress_map(compressed)
    if not decompressed:
        return []

    pixels = _decode_packed_varints(decompressed)
    expected = width * height
    if len(pixels) < expected:
        pixels.extend([0] * (expected - len(pixels)))
    elif len(pixels) > expected:
        pixels = pixels[:expected]

    room_sum_x: dict[int, int] = {}
    room_sum_y: dict[int, int] = {}
    room_count: dict[int, int] = {}
    for i, val in enumerate(pixels):
        if val == 0 or val in (0x20, 0x28):
            continue
        room_id = val >> 8
        ptype = val & 0xFF
        if room_id not in room_names or ptype & 0x10:
            continue
        x = i % width
        y = i // width
        room_sum_x[room_id] = room_sum_x.get(room_id, 0) + x
        room_sum_y[room_id] = room_sum_y.get(room_id, 0) + y
        room_count[room_id] = room_count.get(room_id, 0) + 1

    points: list[tuple[str, float, float]] = []
    for room_id, name in room_names.items():
        if not name or room_id not in room_count:
            continue
        points.append((
            name,
            room_sum_x[room_id] / room_count[room_id],
            room_sum_y[room_id] / room_count[room_id],
        ))
    return points


def _darken(color: tuple[int, int, int], amount: int = 80) -> tuple[int, int, int]:
    """Darken an RGB color by subtracting from each channel."""
    return (
        max(0, color[0] - amount),
        max(0, color[1] - amount),
        max(0, color[2] - amount),
    )


def _draw_dock(
    draw: ImageDraw.ImageDraw,
    dock_x: int,
    dock_y: int,
    size: int = 6,
) -> None:
    """Draw a small Flow-style dock marker at the given image coordinates."""
    half_w = max(5, size // 2)
    half_h = max(4, int(size * 0.42))
    outline_width = max(1, size // 12)
    draw.rounded_rectangle(
        [dock_x - half_w, dock_y - half_h, dock_x + half_w, dock_y + half_h],
        radius=max(3, size // 5),
        fill=(248, 249, 250, 245),
        outline=(90, 96, 104, 220),
        width=outline_width,
    )
    slot_h = max(2, size // 7)
    draw.rounded_rectangle(
        [
            dock_x - int(half_w * 0.55),
            dock_y - slot_h,
            dock_x + int(half_w * 0.55),
            dock_y + slot_h,
        ],
        radius=max(1, slot_h),
        fill=(38, 42, 48, 235),
    )
    led = max(1, size // 10)
    draw.ellipse(
        [
            dock_x + int(half_w * 0.58) - led,
            dock_y + int(half_h * 0.35) - led,
            dock_x + int(half_w * 0.58) + led,
            dock_y + int(half_h * 0.35) + led,
        ],
        fill=(80, 190, 120, 255),
    )


def _draw_robot(
    draw: ImageDraw.ImageDraw,
    rx: int,
    ry: int,
    heading: float | None,
    radius: int,
) -> None:
    """Draw a small Narwal Flow-style robot marker.

    Args:
        draw: PIL ImageDraw instance.
        rx: Robot X in image coordinates (already Y-flipped).
        ry: Robot Y in image coordinates (already Y-flipped).
        heading: Heading in degrees (0=right, 90=up in world coords).
            None to draw circle only without heading arrow.
        radius: Circle radius in pixels.
    """
    import math

    outline_width = max(1, radius // 8)

    if heading is None:
        heading = 90
    rad = math.radians(heading)
    front_dx = math.cos(rad)
    front_dy = -math.sin(rad)
    side_dx = -front_dy
    side_dy = front_dx

    def point(forward: float, side: float) -> tuple[float, float]:
        return (
            rx + (front_dx * forward) + (side_dx * side),
            ry + (front_dy * forward) + (side_dy * side),
        )

    # Soft shadow/halo for contrast on bright room colours.
    draw.ellipse(
        [
            rx - radius - 2,
            ry - radius - 2,
            rx + radius + 2,
            ry + radius + 2,
        ],
        fill=(0, 0, 0, 55),
    )

    # Main circular body.
    draw.ellipse(
        [rx - radius, ry - radius, rx + radius, ry + radius],
        fill=(246, 247, 248, 245),
        outline=(96, 102, 110, 210),
        width=outline_width,
    )

    # Flow-style front sensor bar, rotated with heading.
    bar_half_width = radius * 0.38
    bar_half_height = max(2, radius * 0.13)
    bar = [
        point(radius * 0.58 - bar_half_height, -bar_half_width),
        point(radius * 0.58 + bar_half_height, -bar_half_width),
        point(radius * 0.58 + bar_half_height, bar_half_width),
        point(radius * 0.58 - bar_half_height, bar_half_width),
    ]
    draw.polygon(bar, fill=(30, 33, 38, 245))
    lens_radius = max(1, radius // 8)
    for side in (-bar_half_width * 0.55, bar_half_width * 0.55):
        lx, ly = point(radius * 0.58, side)
        draw.ellipse(
            [lx - lens_radius, ly - lens_radius, lx + lens_radius, ly + lens_radius],
            fill=(85, 160, 225, 255),
        )

    # Top dial and subtle heading notch.
    dial_radius = max(2, radius // 3)
    draw.ellipse(
        [rx - dial_radius, ry - dial_radius, rx + dial_radius, ry + dial_radius],
        fill=(232, 234, 236, 255),
        outline=(150, 155, 160, 230),
        width=max(1, outline_width // 2),
    )
    notch = [
        point(radius * 0.95, 0),
        point(radius * 0.55, -radius * 0.16),
        point(radius * 0.55, radius * 0.16),
    ]
    draw.polygon(notch, fill=(255, 255, 255, 235))


def render_map_png(
    decompressed: bytes,
    width: int,
    height: int,
    robot_x: float | None = None,
    robot_y: float | None = None,
    robot_heading: float | None = None,
    dock_x: float | None = None,
    dock_y: float | None = None,
    room_names: dict[int, str] | None = None,
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
        dock_x: Dock X position in grid coordinates (optional).
        dock_y: Dock Y position in grid coordinates (optional).
        room_names: Mapping of room_id to display name (optional).

    Returns:
        PNG image as bytes, or empty bytes on failure.
    """
    if not decompressed or width <= 0 or height <= 0:
        return b""

    try:
        from PIL import Image, ImageDraw, ImageFont
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

    img = Image.new("RGBA", (width, height), COLOR_UNKNOWN)
    px = img.load()

    # Track room pixel sums for centroid computation
    room_sum_x: dict[int, int] = {}
    room_sum_y: dict[int, int] = {}
    room_count: dict[int, int] = {}

    for i, val in enumerate(pixels):
        x = i % width
        y = i // width

        if val == 0:
            continue  # already set to COLOR_UNKNOWN
        elif val == 0x20:
            px[x, y] = _opaque(COLOR_UNASSIGNED_FLOOR)
        elif val == 0x28:
            px[x, y] = _opaque(COLOR_UNASSIGNED_OBSTACLE)
        else:
            room_id = val >> 8
            ptype = val & 0xFF

            base = (
                ROOM_COLORS[room_id - 1]
                if 1 <= room_id <= len(ROOM_COLORS)
                else COLOR_FALLBACK
            )

            if ptype & 0x10:  # wall/border edge
                px[x, y] = _opaque(_darken(base))
            else:
                px[x, y] = _opaque(base)

            # Accumulate for centroid (floor pixels only, not walls)
            if room_names and room_id in room_names and not (ptype & 0x10):
                room_sum_x[room_id] = room_sum_x.get(room_id, 0) + x
                room_sum_y[room_id] = room_sum_y.get(room_id, 0) + y
                room_count[room_id] = room_count.get(room_id, 0) + 1

    # Flip vertically BEFORE drawing overlays — pixel data is stored with
    # Y increasing upward (math coordinates) but images render Y downward.
    # Overlays (labels, dock, robot) use flipped coordinates so text is right-side up.
    img = img.transpose(Image.FLIP_TOP_BOTTOM)
    scale = MAP_RENDER_SCALE
    if scale > 1:
        img = img.resize(
            (width * scale, height * scale),
            getattr(Image, "Resampling", Image).NEAREST,
        )

    draw = ImageDraw.Draw(img)
    scaled_height = height * scale

    # Draw room labels at flipped centroids
    if room_names:
        font = _load_font(ImageFont, ROOM_LABEL_FONT_SCALE * scale)
        for rid, name in room_names.items():
            if not name or rid not in room_count:
                continue
            cx = _scaled_coord(
                room_sum_x[rid] // room_count[rid], scale, img.width
            )
            cy = _scaled_coord(
                height - 1 - (room_sum_y[rid] // room_count[rid]),
                scale,
                img.height,
            )
            _draw_label(
                draw,
                (cx, cy),
                name,
                font=font,
                padding=max(3, scale),
            )

    # Draw dock position (before robot so robot draws on top)
    # Flip dock Y to match the flipped image
    marker_radius = max(3 * scale, min(width, height) * scale // 80)

    if (
        dock_x is not None
        and dock_y is not None
        and math.isfinite(dock_x)
        and math.isfinite(dock_y)
        and 0 <= dock_x < width
        and 0 <= dock_y < height
    ):
        dock_size = marker_radius * 2
        _draw_dock(
            draw,
            _scaled_coord(dock_x, scale, img.width),
            _scaled_coord(height - 1 - dock_y, scale, img.height),
            dock_size,
        )

    # Draw robot position (flip Y) — skip if out of bounds
    if (
        robot_x is not None
        and robot_y is not None
        and math.isfinite(robot_x)
        and math.isfinite(robot_y)
        and 0 <= robot_x < width
        and 0 <= robot_y < height
    ):
        rx = _scaled_coord(robot_x, scale, img.width)
        ry = _scaled_coord(height - 1 - robot_y, scale, scaled_height)
        _draw_robot(draw, rx, ry, robot_heading, marker_radius)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def render_base_map(
    compressed: bytes,
    width: int,
    height: int,
    dock_x: float | None = None,
    dock_y: float | None = None,
    room_names: dict[int, str] | None = None,
    obstacles: list | None = None,
    origin_x: int = 0,
    origin_y: int = 0,
    show_obstacle_labels: bool = True,
    show_room_labels: bool = True,
    show_dock: bool = True,
) -> Image.Image | None:
    """Render the static floor plan as a PIL Image (no robot overlay).

    Returns a PIL Image that can be cached and reused across frames.
    Only needs to be re-rendered when the static map data changes.

    Args:
        obstacles: List of ObstacleInfo objects to render (optional).
        origin_x: Map origin X offset for obstacle coordinate transform.
        origin_y: Map origin Y offset for obstacle coordinate transform.
        show_obstacle_labels: Whether to draw furniture/obstacle labels.
        show_room_labels: Whether to draw room labels into the base image.
        show_dock: Whether to draw the dock into the base image.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        _LOGGER.error("Pillow is required for map rendering")
        return None

    decompressed = decompress_map(compressed)
    if not decompressed or width <= 0 or height <= 0:
        return None

    pixels = _decode_packed_varints(decompressed)
    expected = width * height

    if len(pixels) < expected:
        pixels.extend([0] * (expected - len(pixels)))
    elif len(pixels) > expected:
        pixels = pixels[:expected]

    img = Image.new("RGBA", (width, height), COLOR_UNKNOWN)
    px = img.load()

    room_sum_x: dict[int, int] = {}
    room_sum_y: dict[int, int] = {}
    room_count: dict[int, int] = {}

    for i, val in enumerate(pixels):
        x = i % width
        y = i // width

        if val == 0:
            continue
        elif val == 0x20:
            px[x, y] = _opaque(COLOR_UNASSIGNED_FLOOR)
        elif val == 0x28:
            px[x, y] = _opaque(COLOR_UNASSIGNED_OBSTACLE)
        else:
            room_id = val >> 8
            ptype = val & 0xFF

            base = (
                ROOM_COLORS[room_id - 1]
                if 1 <= room_id <= len(ROOM_COLORS)
                else COLOR_FALLBACK
            )

            if ptype & 0x10:
                px[x, y] = _opaque(_darken(base))
            else:
                px[x, y] = _opaque(base)

            if room_names and room_id in room_names and not (ptype & 0x10):
                room_sum_x[room_id] = room_sum_x.get(room_id, 0) + x
                room_sum_y[room_id] = room_sum_y.get(room_id, 0) + y
                room_count[room_id] = room_count.get(room_id, 0) + 1

    img = img.transpose(Image.FLIP_TOP_BOTTOM)
    scale = MAP_RENDER_SCALE
    if scale > 1:
        img = img.resize(
            (width * scale, height * scale),
            getattr(Image, "Resampling", Image).NEAREST,
        )
    draw = ImageDraw.Draw(img)

    if room_names and show_room_labels:
        font = _load_font(ImageFont, ROOM_LABEL_FONT_SCALE * scale)
        for rid, name in room_names.items():
            if not name or rid not in room_count:
                continue
            cx = _scaled_coord(
                room_sum_x[rid] // room_count[rid], scale, img.width
            )
            cy = _scaled_coord(
                height - 1 - (room_sum_y[rid] // room_count[rid]),
                scale,
                img.height,
            )
            _draw_label(
                draw,
                (cx, cy),
                name,
                font=font,
                padding=max(3, scale),
            )

    # Draw obstacle/furniture annotations (static data from get_map field 2.32)
    if obstacles:
        obs_font = _load_font(ImageFont, OBSTACLE_LABEL_FONT_SCALE * scale)
        for obs in obstacles:
            gx, gy = obs.to_grid_coords(origin_x, origin_y)
            # Skip out-of-bounds obstacles
            if gx < 0 or gx >= width or gy < 0 or gy >= height:
                continue
            img_x = _scaled_coord(gx, scale, img.width)
            img_y = _scaled_coord(height - 1 - gy, scale, img.height)
            half_w = max(scale, int(obs.width * scale / 2))
            half_h = max(scale, int(obs.height * scale / 2))
            color = OBSTACLE_COLORS.get(obs.type_id, OBSTACLE_COLOR_DEFAULT)
            draw.rectangle(
                [img_x - half_w, img_y - half_h, img_x + half_w, img_y + half_h],
                outline=color, width=max(1, scale // 2),
            )
            if show_obstacle_labels:
                label = obs.display_name
                bbox = obs_font.getbbox(label)
                th = bbox[3] - bbox[1]
                _draw_label(
                    draw,
                    (img_x, img_y - half_h - th - (3 * scale)),
                    label,
                    fill=color,
                    font=obs_font,
                    padding=max(2, scale),
                )

    if (
        show_dock
        and dock_x is not None
        and dock_y is not None
        and math.isfinite(dock_x)
        and math.isfinite(dock_y)
        and 0 <= dock_x < width
        and 0 <= dock_y < height
    ):
        marker_radius = max(3 * scale, min(width, height) * scale // 80)
        dock_size = marker_radius * 2
        _draw_dock(
            draw,
            _scaled_coord(dock_x, scale, img.width),
            _scaled_coord(height - 1 - dock_y, scale, img.height),
            dock_size,
        )

    return img


def render_overlay(
    base_img: Image.Image,
    height: int,
    robot_x: float | None = None,
    robot_y: float | None = None,
    robot_heading: float | None = None,
    trail: list[tuple[float, float]] | None = None,
    rotation_degrees: int = 0,
    zoom: float = 1.0,
    room_labels: list[tuple[str, float, float]] | None = None,
    dock_x: float | None = None,
    dock_y: float | None = None,
) -> bytes:
    """Draw robot position and trail on a copy of the cached base map.

    Args:
        base_img: Cached PIL Image from render_base_map (not modified).
        height: Map height in pixels (for Y-flip).
        robot_x: Robot X in grid coordinates.
        robot_y: Robot Y in grid coordinates.
        robot_heading: Heading in degrees.
        trail: Narwal-native display_map trajectory points in grid coordinates.
        rotation_degrees: Clockwise map rotation in degrees.
        zoom: Centre zoom factor.
        room_labels: Room label centre points in grid coordinates.
        dock_x: Dock X position in grid coordinates.
        dock_y: Dock Y position in grid coordinates.

    Returns:
        PNG bytes of the composited image.
    """
    from PIL import ImageDraw, ImageFont

    img = base_img.copy()
    original_width = img.width
    original_height = img.height
    scale = img.height / height if height > 0 else 1.0
    map_width = original_width / scale if scale > 0 else original_width
    max_grid_segment = max(24.0, min(map_width, height) * 0.18)

    img = _apply_view_transform(img, rotation_degrees, zoom)
    draw = ImageDraw.Draw(img, "RGBA")

    def is_valid_grid_point(grid_x: float, grid_y: float) -> bool:
        return (
            math.isfinite(grid_x)
            and math.isfinite(grid_y)
            and 0 <= grid_x < map_width
            and 0 <= grid_y < height
        )

    def final_point(grid_x: float, grid_y: float) -> tuple[int, int] | None:
        if not is_valid_grid_point(grid_x, grid_y):
            return None
        x = _scaled_coord(grid_x, scale, original_width)
        y = _scaled_coord(height - 1 - grid_y, scale, original_height)
        transformed = _transform_point(
            x, y, original_width, original_height, rotation_degrees, zoom
        )
        if transformed is None:
            return None
        tx, ty = transformed
        return int(round(tx)), int(round(ty))

    # Draw the Narwal-native trajectory, skipping invalid or discontinuous points.
    if trail and len(trail) >= 2:
        trail_width = max(1, int(round(scale)))
        previous_grid: tuple[float, float] | None = None
        previous_point: tuple[int, int] | None = None
        for i in range(len(trail) - 1):
            grid_x, grid_y = trail[i]
            if not is_valid_grid_point(grid_x, grid_y):
                previous_grid = None
                previous_point = None
                continue

            point = final_point(grid_x, grid_y)
            if point is None:
                previous_grid = None
                previous_point = None
                continue

            if previous_grid is not None and previous_point is not None:
                distance = math.hypot(
                    grid_x - previous_grid[0],
                    grid_y - previous_grid[1],
                )
                if distance <= max_grid_segment:
                    draw.line(
                        [previous_point, point],
                        fill=(255, 255, 255, 220),
                        width=trail_width,
                    )

            previous_grid = (grid_x, grid_y)
            previous_point = point

        last_grid_x, last_grid_y = trail[-1]
        if previous_grid is not None and is_valid_grid_point(last_grid_x, last_grid_y):
            last_point = final_point(last_grid_x, last_grid_y)
            if last_point is not None:
                distance = math.hypot(
                    last_grid_x - previous_grid[0],
                    last_grid_y - previous_grid[1],
                )
                if distance <= max_grid_segment:
                    draw.line(
                        [previous_point, last_point],
                        fill=(255, 255, 255, 220),
                        width=trail_width,
                    )

    if room_labels:
        font = _load_font(ImageFont, ROOM_LABEL_FONT_SCALE * int(round(scale)))
        for label, grid_x, grid_y in room_labels:
            point = final_point(grid_x, grid_y)
            if point is None:
                continue
            x, y = point
            if -80 <= x <= img.width + 80 and -80 <= y <= img.height + 80:
                _draw_label(draw, (x, y), label, font, padding=max(4, int(scale)))

    if dock_x is not None and dock_y is not None:
        point = final_point(dock_x, dock_y)
        if point is not None:
            dx, dy = point
            if 0 <= dx < img.width and 0 <= dy < img.height:
                robot_radius = max(5 * int(round(scale)), min(img.width, img.height) // 64)
                dock_size = robot_radius * 2
                _draw_dock(draw, dx, dy, dock_size)

    # Draw robot
    if robot_x is not None and robot_y is not None:
        point = final_point(robot_x, robot_y)
        if point is not None:
            rx, ry = point
            if 0 <= rx < img.width and 0 <= ry < img.height:
                radius = max(5 * int(round(scale)), min(img.width, img.height) // 64)
                _draw_robot(
                    draw,
                    rx,
                    ry,
                    _rotated_heading(robot_heading, rotation_degrees),
                    radius,
                )

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _apply_view_transform(
    img: Image.Image,
    rotation_degrees: int = 0,
    zoom: float = 1.0,
) -> Image.Image:
    """Apply final map rotation and centre zoom."""
    rotation = _normalize_rotation(rotation_degrees)
    if rotation:
        img = img.rotate(-rotation, expand=True)

    zoom = max(1.0, min(2.0, float(zoom or 1.0)))
    if zoom <= 1.0:
        return img

    crop_width = max(1, int(img.width / zoom))
    crop_height = max(1, int(img.height / zoom))
    left = max(0, (img.width - crop_width) // 2)
    top = max(0, (img.height - crop_height) // 2)
    return img.crop((left, top, left + crop_width, top + crop_height))


def render_map_from_compressed(
    compressed: bytes,
    width: int,
    height: int,
    robot_x: float | None = None,
    robot_y: float | None = None,
    robot_heading: float | None = None,
    dock_x: float | None = None,
    dock_y: float | None = None,
    room_names: dict[int, str] | None = None,
) -> bytes:
    """Decompress and render map data in one step (legacy interface).

    Args:
        compressed: Compressed map bytes from the robot.
        width: Map width in pixels.
        height: Map height in pixels.
        robot_x: Robot X position (optional).
        robot_y: Robot Y position (optional).
        robot_heading: Robot heading in degrees (optional).
        dock_x: Dock X position (optional).
        dock_y: Dock Y position (optional).
        room_names: Mapping of room_id to display name (optional).

    Returns:
        PNG image as bytes, or empty bytes on failure.
    """
    decompressed = decompress_map(compressed)
    return render_map_png(
        decompressed, width, height, robot_x, robot_y, robot_heading,
        dock_x, dock_y, room_names,
    )
