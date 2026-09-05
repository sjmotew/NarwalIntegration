"""Data models for Narwal vacuum state."""

from __future__ import annotations

import contextlib
import logging
import math
import struct
import time
import zlib
from dataclasses import dataclass, field
from typing import Any, ClassVar

from .const import (
    ACTIVE_CLEANING_STATUSES,
    CommandResult,
    WorkingStatus,
)

_LOGGER = logging.getLogger(__name__)

# Raw working_status values already reported. The robot rebroadcasts its status
# every ~1.5s, so warning on each broadcast floods the log with thousands of
# identical lines (#46). Warn once per distinct value instead.
_WARNED_WORKING_STATUS: set[Any] = set()

_ACTIVE_WORKING_STATUS_TTL = 15.0
_TERMINAL_WORKING_STATUS_TTL = 15.0
_DOCK_DRYING_STATUS_TTL = 180.0
_DOCK_TASK_ASSUME_TTL = 30.0
# clean/start_clean can be accepted long before working_status metrics arrive.
_ROBOT_START_ASSUME_TTL = 180.0
# Live hardware has taken about 50 seconds to leave a stale docked status after
# accepting a room clean. After this handoff window, repeated idle dock
# telemetry is stronger evidence than the accepted-command reservation.
_ROBOT_START_DOCKED_HANDOFF_GRACE = 60.0
_KNOWN_DOCK_ACTIVITY_VALUES = {0, 2, 3, 4, 6}

DOCK_TASK_EMPTY_DUSTBIN = "empty_dustbin"
DOCK_TASK_WASH_MOP = "wash_mop"
DOCK_TASK_DRY_MOP = "dry_mop"
DOCK_TASK_DRY_DUST_BIN = "dry_dust_bin"
DOCK_TASK_DRY_DOCK_BAG = "dry_dock_bag"

DOCK_TASK_KEYS = (
    DOCK_TASK_EMPTY_DUSTBIN,
    DOCK_TASK_WASH_MOP,
    DOCK_TASK_DRY_MOP,
    DOCK_TASK_DRY_DUST_BIN,
    DOCK_TASK_DRY_DOCK_BAG,
)
_DOCK_DRYING_TASK_ORDER = (
    DOCK_TASK_DRY_MOP,
    DOCK_TASK_DRY_DUST_BIN,
    DOCK_TASK_DRY_DOCK_BAG,
)
_DOCK_DRYING_TIMER_PAIRS: tuple[tuple[str, str, str], ...] = (
    (DOCK_TASK_DRY_MOP, "8", "9"),
    (DOCK_TASK_DRY_DUST_BIN, "10", "11"),
    (DOCK_TASK_DRY_DOCK_BAG, "12", "13"),
)
_UNMAPPED_DOCK_DRYING_TIMER_PAIRS: tuple[tuple[str, str], ...] = (
    ("14", "15"),
    ("16", "17"),
)


@dataclass
class DeviceInfo:
    """Device identity from get_device_info response."""

    product_key: str = ""
    device_id: str = ""
    firmware_version: str = ""


@dataclass(frozen=True)
class DockTaskTimer:
    """Timer details for one active dock task."""

    task: str
    elapsed: int
    target: int
    fields: tuple[str, str]
    observed_at: float = field(default_factory=time.monotonic)

    @property
    def current_elapsed(self) -> int:
        """Return the elapsed seconds reported by the latest timer snapshot."""
        return min(self.target, max(0, self.elapsed))

    @property
    def remaining(self) -> int:
        """Return remaining seconds, clamped at zero."""
        return max(0, self.target - self.current_elapsed)

    @property
    def progress_percent(self) -> int:
        """Return elapsed task progress as an integer percentage."""
        if self.target <= 0:
            return 0
        return min(100, round(self.current_elapsed / self.target * 100))


@dataclass
class RoomInfo:
    """A room on the map.

    Fields from get_map / get_editable_map field 2.12:
      field 1: room_id (matches pixel value >> 8 in map grid)
      field 2: room_sub_type — RoomType enum (MapBaseType.RoomType); see ROOM_TYPE_NAMES
      field 3: user-assigned name (UTF-8, empty if not named by user)
      field 4: category (1=room, 2=utility/small space)
      field 8: instance_index (1-based, for numbering duplicates: Bathroom 1, 2, 3...)
    """

    room_id: int = 0
    name: str = ""  # user-assigned name from field 3
    room_sub_type: int = 0  # ROOM_TYPE enum from field 2
    category: int = 0  # 1=room, 2=utility (field 4)
    instance_index: int = 0  # numbering for duplicates (field 8)

    # RoomType enum (MapBaseType.RoomType, 0-15) to the app's own en-US.json
    # room-name strings. One shared switch
    # (map_engine_i18n_configer.roomTypei18nKey) takes no model parameter, so
    # every model resolves these same names. See #22.
    ROOM_TYPE_NAMES: ClassVar[dict[int, str]] = {
        0: "Room",
        1: "Master bedroom",
        2: "Secondary bedroom",
        3: "Living room",
        4: "Kitchen",
        5: "Bathroom",
        6: "Toilet",
        7: "Balcony",
        8: "Dining room",
        9: "Closet",
        10: "Corridor",
        11: "Study",
        12: "Kids' room",
        13: "Entertainment room",
        14: "Storage room",
        15: "Others",
    }

    @property
    def display_name(self) -> str:
        """User name, or default RoomType name with suffix for duplicates."""
        if self.name:
            return self.name
        base = self.ROOM_TYPE_NAMES.get(self.room_sub_type, "Room")
        if self.instance_index > 1:
            return f"{base} {self.instance_index}"
        return base


@dataclass
class ObstacleInfo:
    """An obstacle/furniture annotation on the map.

    Parsed from get_map field 2.32 (MapFurnitureInfoList).
    The typeId maps to the furniture enum from APK map_furniture.json.

    bbp field mapping (confirmed from probe data + APK schema):
      bbp field 1 -> id (int32)
      bbp field 2 -> typeId (uint32, furniture enum)
      bbp field 3.1.1 -> centerX (float32)
      bbp field 3.1.2 -> centerY (float32)
      bbp field 3.2 -> width (float32)
      bbp field 3.3 -> height (float32)
      bbp field 4 -> angle (float32, degrees)
    """

    id: int = 0
    type_id: int = 0       # Furniture enum from APK map_furniture.json
    center_x: float = 0.0  # World X coordinate
    center_y: float = 0.0  # World Y coordinate
    width: float = 0.0     # Object width in grid units
    height: float = 0.0    # Object height in grid units
    angle: float = 0.0     # Rotation in degrees

    # Full furniture type enum from APK map_furniture.json
    TYPE_NAMES: ClassVar[dict[int, str]] = {
        0: "Placeholder",
        1: "Single Bed",
        2: "Double Bed",
        3: "Baby Bed",
        4: "Dining Table",
        5: "Round Table",
        6: "Tea Table",
        7: "Round Tea Table",
        8: "TV Stand",
        9: "Bedside Table",
        10: "Locker",
        11: "Wardrobe",
        12: "Shoe Cabinet",
        13: "Armchair",
        14: "Sofa",
        15: "L-Shaped Sofa",
        16: "Lazy Chair",
        17: "Chair",
        18: "Bar Chair",
        19: "Cat Toilet",
        20: "Pet Feeder",
        21: "Pet House",
        22: "Washing Machine",
        23: "Refrigerator",
        24: "Air Conditioner",
        25: "Fan",
        26: "Potted Plant",
        27: "Floor Mirror",
        28: "Toilet",
        29: "Piano",
        30: "U-Shaped Sofa",
        31: "Desk",
        32: "Grand Piano",
        33: "Washbasin",
        34: "Stove",
        75: "Cat House",
        76: "Dog House",
        77: "Round Placeholder",
        78: "Weighing Scale",
    }

    @property
    def display_name(self) -> str:
        """Return human-readable name for the obstacle type."""
        return self.TYPE_NAMES.get(self.type_id, f"Object {self.type_id}")

    def to_grid_coords(self, origin_x: int, origin_y: int) -> tuple[float, float]:
        """Convert world coordinates to grid pixel coordinates.

        Same transform as dock/robot: pixel = raw - origin.
        """
        return (self.center_x - origin_x, self.center_y - origin_y)


def _to_float32(val: Any) -> float | None:
    """Convert a protobuf value to float32.

    blackboxprotobuf may return fixed32 fields as either:
      - Python float (if it detects wire type 5 as float)
      - Python int (raw uint32 bit pattern)
    Handle both cases.
    """
    if isinstance(val, float):
        return val
    if isinstance(val, int):
        try:
            return struct.unpack("f", struct.pack("I", val & 0xFFFFFFFF))[0]
        except struct.error:
            return None
    return None


def overlay_to_grid(value: float, origin: int) -> float | None:
    """Convert a Narwal map/display_map coordinate to a grid coordinate."""
    if not math.isfinite(value):
        return None
    return value - origin


def _optional_int(value: Any) -> int | None:
    """Coerce a protobuf scalar to int when possible."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _task_metrics_progressed(
    previous: dict[str, Any], current: dict[str, Any]
) -> bool:
    """Return true when current reports directional clean-task progress."""
    increasing = ("progress", "elapsed", "area")
    if any(
        key in previous and key in current and current[key] < previous[key]
        for key in increasing
    ):
        return False
    if (
        "remaining" in previous
        and "remaining" in current
        and current["remaining"] > previous["remaining"]
    ):
        return False
    if any(
        key in previous and key in current and current[key] > previous[key]
        for key in increasing
    ):
        return True
    return (
        "remaining" in previous
        and "remaining" in current
        and current["remaining"] < previous["remaining"]
    )


def _packed_float32_values(value: Any) -> list[float]:
    """Decode a protobuf packed fixed32/float stream."""
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
        return [
            struct.unpack_from("<f", raw, offset)[0]
            for offset in range(0, len(raw) - len(raw) % 4, 4)
        ]
    if isinstance(value, str):
        raw = value.encode("latin-1", "ignore")
        return [
            struct.unpack_from("<f", raw, offset)[0]
            for offset in range(0, len(raw) - len(raw) % 4, 4)
        ]
    if isinstance(value, list):
        values: list[float] = []
        for item in value:
            parsed = _to_float32(item)
            if parsed is not None:
                values.append(parsed)
        return values
    return []


def _packed_float32_bytes(value: Any) -> bytes:
    """Return raw packed float32 bytes from a protobuf field."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("latin-1", "ignore")
    if isinstance(value, list):
        raw = bytearray()
        for item in value:
            parsed = _to_float32(item)
            raw.extend(struct.pack("<f", parsed if parsed is not None else float("nan")))
        return bytes(raw)
    return b""


def _decode_trajectory(
    x_values: bytes,
    y_values: bytes,
) -> list[tuple[float, float]]:
    """Decode map/display_map field 2 into Narwal-native trajectory points."""
    import math

    xs = _packed_float32_values(x_values)
    ys = _packed_float32_values(y_values)
    # X/Y are parallel streams. Filter after zipping so one invalid value
    # drops that coordinate pair instead of shifting the axes.
    return [
        (x, y)
        for x, y in zip(xs, ys, strict=False)
        if math.isfinite(x) and math.isfinite(y)
    ]


def _trajectory_window_streams(
    decoded: dict[str, Any],
) -> tuple[
    bytes,
    bytes,
    tuple[int, int, int] | tuple[()],
    tuple[int, ...],
]:
    """Return one native trajectory window and its deterministic signature."""
    raw = decoded.get("2")
    if not isinstance(raw, dict):
        return b"", b"", (), ()
    x_values = _packed_float32_bytes(raw.get("1"))
    y_values = _packed_float32_bytes(raw.get("2"))
    pair_bytes = min(len(x_values), len(y_values))
    pair_bytes -= pair_bytes % 4
    if pair_bytes <= 0:
        return b"", b"", (), ()
    finite_x = bytearray()
    finite_y = bytearray()
    breaks: list[int] = []
    pending_break = False
    for offset in range(0, pair_bytes, 4):
        x_chunk = x_values[offset : offset + 4]
        y_chunk = y_values[offset : offset + 4]
        x = struct.unpack("<f", x_chunk)[0]
        y = struct.unpack("<f", y_chunk)[0]
        if not (math.isfinite(x) and math.isfinite(y)):
            pending_break = bool(finite_x)
            continue
        if pending_break:
            breaks.append(len(finite_x) // 4)
            pending_break = False
        finite_x.extend(x_chunk)
        finite_y.extend(y_chunk)
    if not finite_x:
        return b"", b"", (), ()
    x_values = bytes(finite_x)
    y_values = bytes(finite_y)
    trajectory_breaks = tuple(breaks)
    break_bytes = b"".join(
        index.to_bytes(4, "little", signed=False) for index in trajectory_breaks
    )
    return (
        x_values,
        y_values,
        (
            len(x_values) // 4,
            zlib.crc32(x_values) & 0xFFFFFFFF,
            zlib.crc32(break_bytes, zlib.crc32(y_values)) & 0xFFFFFFFF,
        ),
        trajectory_breaks,
    )



def _dock_drying_timers(decoded: dict[str, Any]) -> dict[str, DockTaskTimer]:
    """Return active dock drying timers from a working-status packet."""
    timers: dict[str, DockTaskTimer] = {}
    for task, elapsed_field, target_field in _DOCK_DRYING_TIMER_PAIRS:
        elapsed = _optional_int(decoded.get(elapsed_field))
        target = _optional_int(decoded.get(target_field))
        if elapsed is None or target is None:
            continue
        if target <= 0 or elapsed < 0 or elapsed > target:
            continue
        if elapsed > 0:
            timers[task] = DockTaskTimer(
                task,
                elapsed,
                target,
                (elapsed_field, target_field),
            )
    return timers


def _has_dock_drying_timer_fields(decoded: dict[str, Any]) -> bool:
    """Return true when a packet contains any dock timer field."""
    return any(
        _optional_int(decoded.get(elapsed_field)) is not None
        or _optional_int(decoded.get(target_field)) is not None
        for _, elapsed_field, target_field in _DOCK_DRYING_TIMER_PAIRS
    ) or any(
        _optional_int(decoded.get(elapsed_field)) is not None
        or _optional_int(decoded.get(target_field)) is not None
        for elapsed_field, target_field in _UNMAPPED_DOCK_DRYING_TIMER_PAIRS
    )


def _has_unmapped_dock_drying_timer(decoded: dict[str, Any]) -> bool:
    """Return true when an unmapped dock timer pair reports active work."""
    for elapsed_field, target_field in _UNMAPPED_DOCK_DRYING_TIMER_PAIRS:
        elapsed = _optional_int(decoded.get(elapsed_field))
        target = _optional_int(decoded.get(target_field))
        if elapsed is None or target is None:
            continue
        if 0 < elapsed < target:
            return True
    return False


def _packed_varints(raw: bytes) -> list[int]:
    """Decode a protobuf packed repeated varint field."""
    out: list[int] = []
    acc = shift = 0
    for byte in raw:
        acc |= (byte & 0x7F) << shift
        if byte & 0x80:
            shift += 7
        else:
            out.append(acc)
            acc = shift = 0
    return out  # a trailing unterminated varint is incomplete data; drop it


def _enum_int_list(val: Any) -> list[int]:
    """Coerce a bbp repeated field to a list of non-zero ints.

    protobuf packs repeated scalars, and blackboxprotobuf surfaces a packed field as
    str/bytes rather than a list — the code points are the encoded bytes. Feeding that
    to int() raises, and an alert list that fails to parse is indistinguishable from a
    robot reporting nothing wrong, so this silently reported healthy consumables on a
    robot asking for six parts (#79).
    """
    if isinstance(val, (bytes, bytearray)):
        items: Any = _packed_varints(bytes(val))
    elif isinstance(val, str):
        # bbp decodes the blob as latin-1, so each code point is one byte.
        items = _packed_varints(val.encode("latin-1", "ignore"))
    elif isinstance(val, list):
        items = val
    else:
        items = [val] if val is not None else []

    out: list[int] = []
    for item in items:
        try:
            n = int(item)
        except (ValueError, TypeError):
            continue
        if n:
            out.append(n)
    return out


def _parse_obstacles(field32: dict) -> list[ObstacleInfo]:
    """Parse obstacle/furniture annotations from bbp-decoded field 2.32.

    Args:
        field32: The decoded dict from map payload field "32".

    Returns:
        List of ObstacleInfo objects. Skips items that fail to parse.
    """
    items = field32.get("1", [])
    if isinstance(items, dict):
        items = [items]

    obstacles: list[ObstacleInfo] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            pos = item.get("3", {})
            center = pos.get("1", {}) if isinstance(pos, dict) else {}

            cx = _to_float32(center.get("1")) if isinstance(center, dict) else None
            cy = _to_float32(center.get("2")) if isinstance(center, dict) else None
            w = _to_float32(pos.get("2")) if isinstance(pos, dict) else None
            h = _to_float32(pos.get("3")) if isinstance(pos, dict) else None
            angle = _to_float32(item.get("4"))

            obstacles.append(ObstacleInfo(
                id=int(item.get("1", 0)),
                type_id=int(item.get("2", 0)),
                center_x=cx or 0.0,
                center_y=cy or 0.0,
                width=w or 0.0,
                height=h or 0.0,
                angle=angle or 0.0,
            ))
        except (ValueError, TypeError, AttributeError):
            continue
    return obstacles


@dataclass
class MapData:
    """Map data from get_map response."""

    map_id: int = 0  # active map id (field 2.1) — required by clean/start_clean
    width: int = 0
    height: int = 0
    resolution: int = 0
    rooms: list[RoomInfo] = field(default_factory=list)
    compressed_map: bytes = b""
    area: int = 0
    created_at: int = 0
    dock_x: float | None = None  # dock position in grid coordinates
    dock_y: float | None = None
    origin_x: int = 0  # x pixel offset from field 2.6.3
    origin_y: int = 0  # y pixel offset from field 2.6.1
    obstacles: list[ObstacleInfo] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_response(cls, decoded: dict[str, Any]) -> MapData:
        """Parse map data from a get_map field5 response."""
        payload = decoded.get("2", {})
        if not payload:
            return cls()

        rooms = []
        room_list = payload.get("12", [])
        if isinstance(room_list, dict):
            room_list = [room_list]
        for room in room_list:
            if isinstance(room, dict):
                name_raw = room.get("3", b"")
                if isinstance(name_raw, bytes):
                    name = name_raw.decode("utf-8", errors="replace")
                elif isinstance(name_raw, str):
                    # blackboxprotobuf sometimes returns "b'...'" strings
                    name = name_raw
                    if name.startswith("b'") and name.endswith("'"):
                        name = name[2:-1]
                else:
                    name = str(name_raw)
                rooms.append(RoomInfo(
                    room_id=int(room.get("1", 0)),
                    name=name,
                    room_sub_type=int(room.get("2", 0)),
                    category=int(room.get("4", 0)),
                    instance_index=int(room.get("8", 0)),
                ))

        compressed = payload.get("17", b"")
        if isinstance(compressed, str):
            compressed = compressed.encode("latin-1")

        resolution = int(payload.get("3", 0))

        # Extract origin offsets from field 6 (coordinate transform).
        # Field 6: {1: origin_y, 2: ?, 3: origin_x, 4: resolution}
        # field 6 provides grid origin offsets used by live map overlays:
        # pixel = value - origin
        origin_x = 0
        origin_y = 0
        field6 = payload.get("6")
        if isinstance(field6, dict):
            with contextlib.suppress(ValueError, TypeError):
                origin_x = int(field6.get("3", 0))
            with contextlib.suppress(ValueError, TypeError):
                origin_y = int(field6.get("1", 0))

        # Parse dock position from field 8 (dock/charging station location).
        # Field 8 structure: {1: {1: x, 2: y}, 2: heading_rad}
        # Coordinates use the same live map units as display_map field 5.
        # Matches display_map field 5 (confirmed via live capture cross-reference).
        # Pixel transform: px = value - origin
        dock_x = None
        dock_y = None
        field8 = payload.get("8")
        if isinstance(field8, dict) and resolution > 0:
            pos = field8.get("1")
            if isinstance(pos, dict) and "1" in pos and "2" in pos:
                try:
                    x_pos = _to_float32(pos["1"])
                    y_pos = _to_float32(pos["2"])
                    if x_pos is not None and y_pos is not None:
                        dock_x = overlay_to_grid(x_pos, origin_x)
                        dock_y = overlay_to_grid(y_pos, origin_y)
                except (struct.error, OverflowError, ValueError, TypeError):
                    pass

        # Parse obstacle/furniture annotations from field 32 (MapFurnitureInfoList)
        obstacles: list[ObstacleInfo] = []
        field32 = payload.get("32")
        if isinstance(field32, dict):
            obstacles = _parse_obstacles(field32)

        return cls(
            map_id=int(payload.get("1", 0)),
            width=int(payload.get("4", 0)),
            height=int(payload.get("5", 0)),
            resolution=resolution,
            rooms=rooms,
            compressed_map=compressed if isinstance(compressed, bytes) else b"",
            area=int(payload.get("33", 0)),
            created_at=int(payload.get("34", 0)),
            dock_x=dock_x,
            dock_y=dock_y,
            origin_x=origin_x,
            origin_y=origin_y,
            obstacles=obstacles,
            raw=payload,
        )


@dataclass
class MapDisplayData:
    """Real-time robot position from map/display_map broadcasts.

    Sent every ~1.5s during active cleaning. Contains robot position,
    heading in radians, and a small cleaned-area grid overlay (NOT the full
    house map — that comes from get_map).

    Validated field layout (live capture 2026-02-28, 13 broadcasts):
      field 1.1: {1: x, 2: y} — robot position as float32 map coordinates
      field 1.2: heading as float32 radians
      field 2: rolling trajectory window {1: x_bytes, 2: y_bytes}
      field 5: dock/reference position (constant, same format)
      field 7: cleaned-area grid {1: width, 2: height, 3: compressed_bytes}
      field 10: timestamp in milliseconds since epoch
      field 12: active room list
    """

    robot_x: float = 0.0  # live map X coordinate
    robot_y: float = 0.0  # live map Y coordinate
    robot_heading: float = 0.0  # degrees (converted from radians for renderer)
    timestamp: int = 0  # milliseconds since epoch (field 10)
    # Dock/reference position from field 5 (same coordinate system as robot)
    dock_ref_x: float = 0.0
    dock_ref_y: float = 0.0
    trajectory_x_values: bytes = b""
    trajectory_y_values: bytes = b""
    trajectory_signature: tuple[int, int, int] | tuple[()] = ()
    trajectory_breaks: tuple[int, ...] = ()

    @property
    def has_trajectory(self) -> bool:
        """Return true when display_map carried a native trajectory."""
        return bool(self.trajectory_signature)

    def trajectory_points(self) -> list[tuple[float, float]]:
        """Decode Narwal-native trajectory points from display_map field 2."""
        return _decode_trajectory(
            self.trajectory_x_values,
            self.trajectory_y_values,
        )

    def trajectory_render_points(self) -> list[tuple[float, float]]:
        """Return trajectory points with invalid sentinels at segment breaks."""
        points = self.trajectory_points()
        for index in reversed(self.trajectory_breaks):
            if 0 < index < len(points):
                points.insert(index, (float("nan"), float("nan")))
        return points

    def to_grid_coords(
        self, resolution: int, origin_x: int, origin_y: int,
    ) -> tuple[float, float] | None:
        """Convert live map position to grid pixel coordinates.

        display_map positions already use the static map coordinate scale.
        Same coordinate system as get_map field 8 (dock position).
          pixel = value - origin_offset

        Args:
            resolution: Map resolution in mm/pixel (e.g. 60).
            origin_x: X pixel offset (MapData.origin_x, from field 2.6.3).
            origin_y: Y pixel offset (MapData.origin_y, from field 2.6.1).

        Returns:
            (pixel_x, pixel_y) tuple, or None if no valid position.
        """
        if self.robot_x == 0.0 and self.robot_y == 0.0:
            return None
        if resolution <= 0:
            return None
        px = overlay_to_grid(self.robot_x, origin_x)
        py = overlay_to_grid(self.robot_y, origin_y)
        if px is None or py is None:
            return None
        return (px, py)

    @classmethod
    def from_broadcast(cls, decoded: dict[str, Any]) -> MapDisplayData:
        """Parse display_map broadcast payload."""
        import math

        result = cls()

        # Robot position — field 1.1 = {1: x_cm, 2: y_cm}, field 1.2 = heading_rad
        field1 = decoded.get("1", {})
        if isinstance(field1, dict):
            pos = field1.get("1", {})
            if isinstance(pos, dict):
                x_f = _to_float32(pos.get("1"))
                if x_f is not None and math.isfinite(x_f):
                    result.robot_x = x_f
                y_f = _to_float32(pos.get("2"))
                if y_f is not None and math.isfinite(y_f):
                    result.robot_y = y_f

            heading_raw = field1.get("2")
            if heading_raw is not None:
                h_f = _to_float32(heading_raw)
                if h_f is not None and math.isfinite(h_f):
                    result.robot_heading = math.degrees(h_f)

        # Rolling cleaning trajectory window from Narwal itself. Keep raw
        # streams here so HA can join exact overlapping windows without
        # sampling robot positions or decoding the route on the event loop.
        (
            result.trajectory_x_values,
            result.trajectory_y_values,
            result.trajectory_signature,
            result.trajectory_breaks,
        ) = _trajectory_window_streams(decoded)

        # Dock/reference position — field 5 (same format as field 1)
        field5 = decoded.get("5", {})
        if isinstance(field5, dict):
            pos5 = field5.get("1", {})
            if isinstance(pos5, dict):
                dx = _to_float32(pos5.get("1"))
                if dx is not None and math.isfinite(dx):
                    result.dock_ref_x = dx
                dy = _to_float32(pos5.get("2"))
                if dy is not None and math.isfinite(dy):
                    result.dock_ref_y = dy

        # Timestamp — field 10 (milliseconds since epoch)
        if "10" in decoded:
            with contextlib.suppress(ValueError, TypeError):
                result.timestamp = int(decoded["10"])

        return result


@dataclass
class Position:
    """Robot position from map/display_map."""

    x: float = 0.0
    y: float = 0.0
    heading: float = 0.0


@dataclass
class CommandResponse:
    """Response from a command sent to the robot."""

    result_code: int = 0
    data: dict[str, Any] = field(default_factory=dict)
    raw_payload: bytes = b""

    @property
    def success(self) -> bool:
        return self.result_code == CommandResult.SUCCESS

    @property
    def accepted(self) -> bool:
        """Return true when Narwal accepted or applied the command."""
        return self.result_code in (0, CommandResult.SUCCESS, CommandResult.APPLIED)

    @property
    def not_applicable(self) -> bool:
        return self.result_code == CommandResult.NOT_APPLICABLE


@dataclass
class NarwalState:
    """Complete state of a Narwal vacuum.

    Updated incrementally as different topic messages arrive.
    """

    # Core status
    working_status: WorkingStatus = WorkingStatus.UNKNOWN
    battery_level: int = 0  # real-time SOC from field 2 (float32)
    firmware_version: str = ""
    firmware_target: str = ""

    # Device identity
    device_info: DeviceInfo | None = None

    # Identity / station maintenance (base_status)
    binded_uuid: str = ""  # field 13 — bound account/device UUID
    station_bag_health_reset_time: int = 0  # field 36 — epoch of last bag-health reset

    # Position (from map data)
    position: Position | None = None

    # Cleaning stats
    cleaning_area: float = 0.0  # m² (coveredArea)
    cleaning_time: int = 0  # seconds
    last_active_working_status_time: float = 0.0
    last_terminal_working_status_time: float = 0.0
    terminal_working_status_generation: int = 0
    pending_active_working_status: dict[str, Any] | None = field(
        default=None, repr=False
    )
    pending_active_working_status_time: float = 0.0
    task_progress_percent: int | None = None
    task_elapsed_time: int = 0
    task_remaining_time: int = 0
    current_room_aux_name: str = ""

    # Consumables / station / fault (base_status; present on dock and during cleaning)
    dust_bag_health: float = 0.0  # field 35 stationBagHealthScore (%)
    detergent_remaining: int = 0  # field 41 heavyDetergentRemainPercent (%)
    curing_agent_consumption_percent: int = 0  # field 38
    has_error: bool = False  # field 1 errorCode has an active code
    error_codes: list[int] = field(default_factory=list)  # field 1 ErrorCode.identityCode(s)
    error_level: int = 0  # ErrorCode.level (field 1 sub-2)
    error_detail: str = ""  # ErrorCode.debugDetail (field 1 sub-3)
    terminate_reason: int = 0  # field 15 — TaskResult of the last task (why it ended)

    # Station tank/bag enum states (base_status; None = not reported by this model).
    # 0=unspecified, 1=ok/installed, ≥2=attention (empty/abnormal/replace) — see BaseStatusField.
    clean_water_tank_state: int | None = None  # field 23 (CleanWaterTankState)
    sewage_tank_state: int | None = None  # field 24 (SewageTankState)
    dust_box_state: int | None = None  # field 20 (DustBoxState)
    dust_bag_state: int | None = None  # field 21 (DustBagState)
    station_bag_state: int | None = None  # field 39 (StationBagStatus)

    # Consumable alerts from consumable/get_consumable_info (queried, not broadcast)
    maintain_items: list[int] = field(default_factory=list)  # ConsumableMaintainItem values
    replace_items: list[int] = field(default_factory=list)  # ConsumableReplaceItem values

    # Map
    map_data: MapData | None = None
    map_display_data: MapDisplayData | None = None

    # Download / upgrade status
    download_status: int = 0  # download_status field 3 (state)
    upgrade_status: int = 0  # upgrade_status field 2 (status)
    upgrade_stage: int = 0  # upgrade_status field 4 (stage)

    # Pause overlay (field 3 sub-field 2 = 1 means paused)
    is_paused: bool = False

    # Dock sub-state (field 3 sub-field 10: 1=docked, 2=docking in progress)
    dock_sub_state: int = 0

    # Returning flag (field 3 sub-field 7: 1=returning to dock)
    # Confirmed via live test: appears when robot is navigating back to dock
    is_returning_to_dock: bool = False

    # Dock activity (field 3 sub-field 12: 2/6 observed when docked)
    dock_activity: int = 0

    # Station activity (field 3 sub-field 18).
    # Observed: 1 during dust gathering, 4 during dock dry/disinfection work.
    station_activity: int = 0

    # Dock task timers from working_status fields 8..13.
    dock_drying_tasks: dict[str, DockTaskTimer] = field(default_factory=dict)
    dock_drying_status_time: float = 0.0
    has_dock_drying_timer_snapshot: bool = False
    has_unmapped_dock_drying_timer: bool = False
    assumed_dock_task: str = ""
    assumed_dock_task_until: float = 0.0
    assumed_robot_clean_until: float = 0.0

    # Dock presence (field 3 sub-field 3)
    # Values observed: 1=on dock, 2=off dock, 6=on dock (charged idle)
    dock_presence: int = 0

    # Dock indicator from field 11 (top-level base_status field)
    # Validated via dock_research.py guided test (5 captures):
    #   2 = on dock (all 3 on-dock captures)
    #   1 = off dock (both off-dock captures)
    # Perfect dock correlation — primary STANDBY dock signal.
    dock_field11: int = 0

    # Dock indicator from field 47 (top-level base_status field)
    # Validated via dock_research.py guided test (5 captures):
    #   3 = on dock (all 3 on-dock captures)
    #   2 = off dock (both off-dock captures)
    # Secondary confirmation signal.
    dock_field47: int = 0
    has_current_dock_presence_signal: bool = False

    # Base station ambient light mode from top-level base_status field 50.
    # Values validated against the app: 1=Nightlight,
    # 2=Fireplace / Winter warmth, 3=Purple. When the light is off the robot
    # omits field 50 from base_status, so missing field 50 is treated as 0.
    dock_light_mode: int | None = None

    # Current room being cleaned (working_status field 6, confirmed 2026-04-24).
    # room_id of the room the robot is actively cleaning right now.
    # None when robot is idle/docked or field 6 is absent/zero.
    current_room_id: int | None = None

    # Raw data for fields we haven't fully decoded yet
    raw_base_status: dict[str, Any] = field(default_factory=dict)
    raw_working_status: dict[str, Any] = field(default_factory=dict)

    @property
    def has_recent_active_working_status(self) -> bool:
        """True while live working_status task metrics are still fresh."""
        if self.last_active_working_status_time <= 0:
            return False
        return (
            time.monotonic() - self.last_active_working_status_time
            <= _ACTIVE_WORKING_STATUS_TTL
        )

    @property
    def has_recent_terminal_working_status(self) -> bool:
        """True just after authoritative terminal base-status telemetry."""
        if self.last_terminal_working_status_time <= 0:
            return False
        return (
            time.monotonic() - self.last_terminal_working_status_time
            <= _TERMINAL_WORKING_STATUS_TTL
        )

    @property
    def has_paused_clean_task_context(self) -> bool:
        """True when a paused overlay still has retained robot clean details."""
        if not self.is_paused:
            return False
        return (
            getattr(self, "task_progress_percent", None) is not None
            or getattr(self, "task_elapsed_time", 0) > 0
            or self.cleaning_time > 0
            or getattr(self, "task_remaining_time", 0) > 0
            or self.current_room_id is not None
            or bool(getattr(self, "current_room_aux_name", ""))
        )

    @property
    def is_cleaning(self) -> bool:
        """True when actively cleaning (not paused, not returning to dock)."""
        if self.has_error:
            return False
        if self.has_recent_active_working_status:
            return not self.is_paused and not self.is_returning
        if self.is_docked:
            return False
        return (
            self.working_status in ACTIVE_CLEANING_STATUSES
            and not self.is_paused
            and not self.is_returning
        )

    @property
    def has_explicit_off_dock_signal(self) -> bool:
        """True when dock telemetry explicitly says the robot is not seated."""
        if (
            self.dock_sub_state == 1
            or self.dock_field11 >= 2
            or self.dock_field47 in (1, 3)
        ):
            return False
        return (
            self.dock_presence == 2
            or self.dock_sub_state == 2
            or (self.dock_field11 == 1 and self.dock_field47 == 2)
        )

    @property
    def is_docked(self) -> bool:
        """True when on dock: DOCKED(10), CHARGED(14), DOCKED_V2(2), or dock field signals.

        Dock signals (checked for STANDBY, UNKNOWN, and any unmapped status):
          - dock_sub_state == 1 (field 3.10, old FW only)
          - dock_presence in (1, 6) (field 3.3, dock-present variants)
          - dock_activity > 0 (field 3.12, old FW only)
          - dock_field11 >= 2 (field 11: old FW 2=docked/1=undocked,
                               v01.07.23 3=docked)
          - dock_field47 in (1, 3) (field 47: old FW 3=docked/2=undocked,
                                    v01.07.23 1=docked)

        Dock fields are checked for STANDBY/UNKNOWN and any status where
        cleaning is not active, since the robot can report unmapped states
        (e.g. self-test) while physically docked.
        """
        if self.has_recent_active_working_status:
            return False
        if self.has_explicit_off_dock_signal:
            return False
        if self.working_status in (
            WorkingStatus.DOCKED, WorkingStatus.CHARGED, WorkingStatus.DOCKED_V2,
        ):
            return True
        if self.working_status in ACTIVE_CLEANING_STATUSES:
            return False
        # For STANDBY, UNKNOWN, or any other status: check dock field signals.
        # Values differ across firmware versions:
        #   Old FW: dock_sub_state=1, dock_field11=2, dock_field47=3
        #   v01.07.23.00: dock_sub_state absent, dock_field11=3, dock_field47=1
        if self.dock_sub_state == 1:
            return True
        if self.dock_presence in (1, 6):
            return True
        if self.dock_activity > 0:
            return True
        if self.dock_field11 >= 2:
            return True
        return self.dock_field47 in (1, 3)

    @property
    def has_dock_presence_signal(self) -> bool:
        """True when any field reports the robot is on the dock."""
        if self.has_explicit_off_dock_signal:
            return False
        return (
            self.dock_presence in (1, 6)
            or self.dock_sub_state == 1
            or self.dock_activity > 0
            or self.dock_field11 >= 2
            or self.dock_field47 in (1, 3)
        )

    @property
    def is_returning(self) -> bool:
        """True when the robot is actively returning to the dock.

        Live-validated: during return-to-dock, field 3 shows:
          {1=4, 7=1, 10=2} — working_status stays CLEANING(4),
          field 7=1 (returning flag), field 10=2 (docking in progress).

        Requires BOTH field 3.7=1 AND field 3.10=2 to avoid false
        positives — either field alone can be stale during normal
        cleaning (confirmed 2026-03-08: robot cleaning in Pantry
        showed returning=True from a single stale field).

        Only valid while working_status is CLEANING — once the robot
        transitions to STANDBY/DOCKED/CHARGED, it has already docked
        even if field 3.7 is momentarily still set.
        """
        if self.working_status not in ACTIVE_CLEANING_STATUSES:
            return False
        return self.is_returning_to_dock and self.dock_sub_state == 2

    @property
    def is_station_active(self) -> bool:
        """True when the dock/base station is running a dock-side task."""
        if self.has_recent_active_working_status:
            return (
                self.station_activity in (1, 2, 3)
                or self.is_washing_mop
                or bool(self.active_dock_drying_tasks)
            )
        return (
            self.is_washing_mop
            or self.is_drying_mop
            or bool(self.active_dock_drying_tasks)
            or (
                self.station_activity > 0
                and not (
                    self.station_activity == 4
                    and self.has_fresh_idle_dock_drying_snapshot
                )
            )
        )

    @property
    def blocks_robot_start_for_dock_task(self) -> bool:
        """True when dock-side activity should block a new robot clean."""
        if self.has_unmapped_active_dock_task:
            return True
        if self.assumed_active_dock_task is not None:
            return True
        telemetry_tasks = set(self.telemetry_dock_task_keys)
        if not telemetry_tasks:
            return False
        return not (
            telemetry_tasks.issubset(_DOCK_DRYING_TASK_ORDER)
            and self.has_recent_dock_drying_status
            and all(self.dock_task_timer(task) is not None for task in telemetry_tasks)
        )

    @property
    def is_washing_mop(self) -> bool:
        """True when the dock is washing the mop pads."""
        return self.station_activity in (2, 3) or self.dock_activity == 3

    @property
    def is_drying_mop(self) -> bool:
        """True when the dock is drying the mop pads."""
        if self.dock_task_timer(DOCK_TASK_DRY_MOP) is not None:
            return True
        if self.has_recent_dock_drying_status and self.has_dock_drying_timer_snapshot:
            return False
        return self.dock_activity == 4

    @property
    def active_dock_task_keys(self) -> tuple[str, ...]:
        """Return active known dock task keys from telemetry and accepted guards."""
        tasks = set(self.telemetry_dock_task_keys)
        if assumed := self.assumed_active_dock_task:
            tasks.add(assumed)
        return tuple(task for task in DOCK_TASK_KEYS if task in tasks)

    @property
    def telemetry_dock_task_keys(self) -> tuple[str, ...]:
        """Return active dock task keys from robot telemetry only."""
        tasks: list[str] = []
        if not self.has_recent_active_working_status:
            if self.station_activity == 1:
                tasks.append(DOCK_TASK_EMPTY_DUSTBIN)
            if self.is_washing_mop:
                tasks.append(DOCK_TASK_WASH_MOP)
        tasks.extend(self.telemetry_dock_drying_tasks)
        if (
            not self.has_recent_active_working_status
            and self.is_drying_mop
            and DOCK_TASK_DRY_MOP not in tasks
        ):
            tasks.append(DOCK_TASK_DRY_MOP)
        active = set(tasks)
        return tuple(task for task in DOCK_TASK_KEYS if task in active)

    @property
    def has_unmapped_active_dock_task(self) -> bool:
        """True when station work is active but not mapped to one of five tasks."""
        if self.has_unmapped_dock_drying_timer and self.has_recent_dock_drying_status:
            return True
        if self.dock_activity not in _KNOWN_DOCK_ACTIVITY_VALUES:
            return True
        if self.station_activity <= 0:
            return False
        if self.station_activity in (1, 2, 3):
            return False
        if self.station_activity == 4 and self.has_fresh_idle_dock_drying_snapshot:
            return False
        return not (self.station_activity == 4 and self.active_dock_drying_tasks)

    @property
    def active_dock_drying_tasks(self) -> tuple[str, ...]:
        """Return active dock drying/disinfection tasks from telemetry only."""
        return self.telemetry_dock_drying_tasks

    @property
    def telemetry_dock_drying_tasks(self) -> tuple[str, ...]:
        """Return active dock drying/disinfection tasks from telemetry only."""
        tasks = [
            task
            for task in _DOCK_DRYING_TASK_ORDER
            if self.dock_task_timer(task) is not None
        ]
        return tuple(tasks)

    @property
    def assumed_active_dock_task(self) -> str | None:
        """Return a short accepted-command dock task reservation, if valid."""
        if not self.assumed_dock_task:
            return None
        if time.monotonic() > self.assumed_dock_task_until:
            return None
        return self.assumed_dock_task

    @property
    def assumed_active_dock_drying_task(self) -> str | None:
        """Return an assumed dock drying task, if the reservation is drying."""
        assumed = self.assumed_active_dock_task
        if assumed in _DOCK_DRYING_TASK_ORDER:
            return assumed
        return None

    @property
    def has_recent_dock_drying_status(self) -> bool:
        """True while live dock drying timer fields are still fresh."""
        return (
            self.dock_drying_status_time > 0
            and time.monotonic() - self.dock_drying_status_time
            <= _DOCK_DRYING_STATUS_TTL
        )

    @property
    def has_fresh_idle_dock_drying_snapshot(self) -> bool:
        """True when fresh typed timer telemetry says no drying task is active."""
        return (
            self.has_recent_dock_drying_status
            and self.has_dock_drying_timer_snapshot
            and not self.telemetry_dock_drying_tasks
        )

    @property
    def has_assumed_robot_clean(self) -> bool:
        """Return a short accepted-command robot-clean reservation."""
        return (
            self.assumed_robot_clean_until > 0
            and time.monotonic() <= self.assumed_robot_clean_until
        )

    def assume_robot_clean(self) -> None:
        """Temporarily reserve robot-clean command context after an accepted start."""
        self.assumed_robot_clean_until = time.monotonic() + _ROBOT_START_ASSUME_TTL
        self.map_display_data = None

    def mark_robot_resumed(self) -> None:
        """Record an accepted resume as explicit active-clean evidence."""
        self.is_paused = False
        self.last_active_working_status_time = time.monotonic()
        self.last_terminal_working_status_time = 0.0
        self.pending_active_working_status = None
        self.pending_active_working_status_time = 0.0

    def clear_assumed_robot_clean(self) -> None:
        """Clear the local robot-clean command reservation."""
        self.assumed_robot_clean_until = 0.0

    def _clear_assumed_robot_clean_on_terminal_base_status(
        self,
        *,
        is_paused: bool,
    ) -> None:
        """Clear accepted-start context once base status proves it is terminal."""
        if not self.has_assumed_robot_clean:
            return
        if self.has_error or self.working_status == WorkingStatus.ERROR:
            self.clear_assumed_robot_clean()
            return
        if self.working_status == WorkingStatus.TASK_COMPLETED:
            self.clear_assumed_robot_clean()
            return
        handoff_ends = (
            self.assumed_robot_clean_until
            - _ROBOT_START_ASSUME_TTL
            + _ROBOT_START_DOCKED_HANDOFF_GRACE
        )
        if (
            not is_paused
            and time.monotonic() >= handoff_ends
            and self.working_status
            in (
                WorkingStatus.STANDBY,
                WorkingStatus.DOCKED,
                WorkingStatus.CHARGED,
                WorkingStatus.DOCKED_V2,
            )
            and self.is_docked
        ):
            self.clear_assumed_robot_clean()

    def dock_task_timer(self, task: str) -> DockTaskTimer | None:
        """Return timer details for one active dock task."""
        timer = self.dock_drying_tasks.get(task)
        if timer is None or timer.remaining <= 0:
            return None
        if not self.has_recent_dock_drying_status:
            return None
        if task == DOCK_TASK_DRY_DOCK_BAG:
            return timer
        if not self.has_dock_presence_signal and not self.is_docked:
            return None
        return timer

    def set_dock_drying_task(
        self,
        task: str,
        elapsed: int,
        target: int,
        fields: tuple[str, str],
    ) -> None:
        """Set one dock drying timer from typed telemetry or a test fixture."""
        self.dock_drying_tasks[task] = DockTaskTimer(task, elapsed, target, fields)
        self.dock_drying_status_time = time.monotonic()

    def clear_dock_drying_task(self, task: str | None = None) -> None:
        """Clear one or all dock drying timers."""
        if task is None:
            self.dock_drying_tasks.clear()
        else:
            self.dock_drying_tasks.pop(task, None)
        if not self.dock_drying_tasks:
            self.dock_drying_status_time = 0.0
            self.has_dock_drying_timer_snapshot = False

    def assume_dock_task(self, task: str, *, ttl: float = _DOCK_TASK_ASSUME_TTL) -> None:
        """Briefly reserve a dock task after an accepted command."""
        self.assumed_dock_task = task
        self.assumed_dock_task_until = time.monotonic() + ttl

    def clear_assumed_dock_task(self, task: str | None = None) -> None:
        """Clear a local dock task reservation."""
        if task is not None and task != self.assumed_dock_task:
            return
        self.assumed_dock_task = ""
        self.assumed_dock_task_until = 0.0

    @property
    def current_room_name(self) -> str | None:
        """Return the display name of the room currently being cleaned.

        Looks up current_room_id in the cached room table from get_map.
        Returns None if the robot is idle, the map has not loaded yet,
        or the room_id is not found in the map (e.g. during a partial map).
        """
        if self.current_room_id is None:
            return None
        if self.map_data is None:
            return self.current_room_aux_name or None
        for room in self.map_data.rooms:
            if room.room_id == self.current_room_id:
                return room.display_name
        return self.current_room_aux_name or None

    def clear_task_details(self) -> None:
        """Clear active robot-task detail fields."""
        self.is_paused = False
        self.cleaning_area = 0.0
        self.cleaning_time = 0
        self.task_progress_percent = None
        self.task_elapsed_time = 0
        self.task_remaining_time = 0
        self.current_room_id = None
        self.current_room_aux_name = ""

    @staticmethod
    def _task_progress_percent(value: Any) -> int | None:
        """Return a percent for task progress encoded as percent or 0..1 float."""
        if isinstance(value, float):
            if 0.0 <= value <= 1.0:
                return round(value * 100)
            if 0.0 <= value <= 100.0:
                return round(value)
        progress = _optional_int(value)
        if progress is not None and 0 <= progress <= 100:
            return progress
        progress_float = _to_float32(value)
        if progress_float is None:
            return None
        if 0.0 <= progress_float <= 1.0:
            return round(progress_float * 100)
        if 0.0 <= progress_float <= 100.0:
            return round(progress_float)
        return None

    def _update_current_room(self, payload: dict[str, Any]) -> None:
        """Parse scalar and nested current-room working-status fields."""
        room = payload.get("6")
        if not isinstance(room, dict):
            room = payload.get("8")
        if isinstance(room, dict):
            room_id = _optional_int(room.get("1"))
            if room_id is not None:
                if room_id != self.current_room_id and "3" not in room:
                    self.current_room_aux_name = ""
                self.current_room_id = room_id or None
            if "3" in room:
                name = room["3"]
                if isinstance(name, (bytes, bytearray)):
                    self.current_room_aux_name = bytes(name).decode(
                        "utf-8", errors="replace"
                    )
                else:
                    self.current_room_aux_name = str(name) if name else ""
                    if (
                        self.current_room_aux_name.startswith("b'")
                        and self.current_room_aux_name.endswith("'")
                    ):
                        self.current_room_aux_name = self.current_room_aux_name[2:-1]
        else:
            room_id = _optional_int(payload.get("6"))
            if room_id is not None:
                if room_id != self.current_room_id:
                    self.current_room_aux_name = ""
                self.current_room_id = room_id or None

    def _restore_candidate_task_details(
        self, candidate: dict[str, Any], reported: dict[str, Any]
    ) -> None:
        """Restore candidate values omitted by a confirming partial packet."""
        if "progress" not in reported and "progress" in candidate:
            self.task_progress_percent = candidate["progress"]
        if "remaining" not in reported and "remaining" in candidate:
            self.task_remaining_time = candidate["remaining"]
        if "elapsed" not in reported and "elapsed" in candidate:
            self.cleaning_time = candidate["elapsed"]
            self.task_elapsed_time = candidate["elapsed"]
        if "area" not in reported and "area" in candidate:
            self.cleaning_area = candidate["area"]
        if "room_id" not in reported and "room_id" in candidate:
            self.current_room_id = candidate["room_id"]
        can_restore_room_name = (
            "room_id" not in reported
            or reported["room_id"] == candidate.get("room_id")
        )
        if (
            "room_name" not in reported
            and "room_name" in candidate
            and can_restore_room_name
        ):
            self.current_room_aux_name = candidate["room_name"]

    def update_from_working_status(self, decoded: dict[str, Any]) -> None:
        """Update state from a decoded working_status message.

        WorkingStatus proto fields (decompiled BuilderInfo):
          Field 2 = coveredArea (float32, PbFieldType 0x100) — area cleaned this session, m²
          Field 3 = timeConsuming (seconds) — session elapsed time
                    (confirmed: 2136→2159 over a 35-min clean)

        Field 13 is totalDryStationBagTime (cumulative station timer, 18000 = 5h),
        not area — reading it as area is why the sensor was stuck at 1.8 m².
        """
        self.raw_working_status = decoded
        reported_task_details: dict[str, Any] = {}
        previous_task_metrics: dict[str, Any] = {
            "area": self.cleaning_area,
            "elapsed": self.cleaning_time,
            "remaining": self.task_remaining_time,
        }
        if self.task_progress_percent is not None:
            previous_task_metrics["progress"] = self.task_progress_percent
        previous_task_details = (
            self.task_progress_percent,
            self.cleaning_area,
            self.cleaning_time,
            self.task_remaining_time,
            self.current_room_id,
            self.current_room_aux_name,
        )
        progress = self._task_progress_percent(decoded.get("1"))
        if progress is not None:
            self.task_progress_percent = max(0, min(100, progress))
            reported_task_details["progress"] = self.task_progress_percent
        remaining = _optional_int(decoded.get("4"))
        if remaining is not None:
            self.task_remaining_time = max(0, remaining)
            reported_task_details["remaining"] = self.task_remaining_time
        has_robot_side_drying = False
        if _has_dock_drying_timer_fields(decoded):
            timers = _dock_drying_timers(decoded)
            has_robot_side_drying = bool(
                timers.keys() & {DOCK_TASK_DRY_MOP, DOCK_TASK_DRY_DUST_BIN}
            )
            self.dock_drying_tasks = timers
            self.has_dock_drying_timer_snapshot = True
            self.has_unmapped_dock_drying_timer = _has_unmapped_dock_drying_timer(
                decoded
            )
            self.dock_drying_status_time = time.monotonic()
            if timers:
                self.clear_assumed_dock_task()
        # Only a positive session counter is evidence of an active clean. Field
        # presence alone is not: a robot reporting timeConsuming=0 would
        # otherwise be flipped to CLEANING and shown as running while parked.
        active_payload = False
        if "3" in decoded:
            try:
                self.cleaning_time = int(decoded["3"])
                self.task_elapsed_time = self.cleaning_time
                reported_task_details["elapsed"] = self.cleaning_time
                active_payload = active_payload or self.cleaning_time > 0
            except (ValueError, TypeError):
                pass
        if "2" in decoded:
            area = _to_float32(decoded["2"])
            if area is not None and area >= 0:
                self.cleaning_area = area
                reported_task_details["area"] = self.cleaning_area
                active_payload = active_payload or area > 0
        if "6" in decoded or isinstance(decoded.get("8"), dict):
            # Field 6 is a scalar room id on Flow 2 and a nested room detail
            # message on other firmware; field 8 is the alternate nested shape.
            self._update_current_room(decoded)
            room_payload = decoded.get("6")
            if not isinstance(room_payload, dict):
                room_payload = decoded.get("8")
            if isinstance(room_payload, dict):
                if "1" in room_payload:
                    reported_task_details["room_id"] = self.current_room_id
                if "3" in room_payload:
                    reported_task_details["room_name"] = self.current_room_aux_name
            else:
                reported_task_details["room_id"] = self.current_room_id
        current_task_details = (
            self.task_progress_percent,
            self.cleaning_area,
            self.cleaning_time,
            self.task_remaining_time,
            self.current_room_id,
            self.current_room_aux_name,
        )
        task_details_changed = previous_task_details != current_task_details
        has_candidate_payload = active_payload or (
            reported_task_details.get("progress", 0) > 0
            or reported_task_details.get("remaining", 0) > 0
        )
        # Clean counters can arrive late from the previous session. Fresh
        # empty/wash telemetry is authoritative because raw clean commands
        # cannot start robot work during either task.
        has_blocking_station_task = (
            self.station_activity in (1, 2, 3)
            or self.dock_activity == 3
            or self.has_unmapped_active_dock_task
            or has_robot_side_drying
            or self.assumed_active_dock_task
            in (DOCK_TASK_EMPTY_DUSTBIN, DOCK_TASK_WASH_MOP)
        )
        explicit_terminal_status = self.working_status in {
            WorkingStatus.TASK_COMPLETED,
            WorkingStatus.ERROR,
        }
        now = time.monotonic()
        pending_candidate = self.pending_active_working_status
        pending_candidate_fresh = (
            pending_candidate is not None
            and now - self.pending_active_working_status_time
            <= _TERMINAL_WORKING_STATUS_TTL
        )
        confirmed_external_clean = (
            has_candidate_payload
            and not explicit_terminal_status
            and not self.has_error
            and not has_blocking_station_task
            and pending_candidate_fresh
            and pending_candidate is not None
            and _task_metrics_progressed(pending_candidate, reported_task_details)
        )
        continued_partial_clean = (
            self.has_recent_active_working_status
            and _task_metrics_progressed(
                previous_task_metrics, reported_task_details
            )
        )
        has_terminal_robot_status = not self.has_assumed_robot_clean and (
            explicit_terminal_status
            or (
                self.has_recent_terminal_working_status
                and not confirmed_external_clean
            )
        )
        if confirmed_external_clean:
            assert pending_candidate is not None
            self._restore_candidate_task_details(
                pending_candidate, reported_task_details
            )
        if (
            (active_payload or confirmed_external_clean or continued_partial_clean)
            and not self.has_error
            and not has_blocking_station_task
            and not has_terminal_robot_status
        ):
            if task_details_changed:
                self.last_active_working_status_time = now
                self.is_paused = False
            self.last_terminal_working_status_time = 0.0
            self.pending_active_working_status = None
            self.pending_active_working_status_time = 0.0
            self.dock_activity = 0
            self.station_activity = 0
            self.clear_assumed_dock_task()
        elif has_terminal_robot_status:
            if (
                (has_candidate_payload or pending_candidate_fresh)
                and not explicit_terminal_status
                and not self.has_error
                and not has_blocking_station_task
            ):
                if not pending_candidate_fresh:
                    self.pending_active_working_status = dict(reported_task_details)
                    self.pending_active_working_status_time = now
                else:
                    assert pending_candidate is not None
                    if "room_id" in reported_task_details:
                        if (
                            reported_task_details["room_id"]
                            != pending_candidate.get("room_id")
                            and "room_name" not in reported_task_details
                        ):
                            pending_candidate.pop("room_name", None)
                        pending_candidate["room_id"] = reported_task_details["room_id"]
                    if "room_name" in reported_task_details:
                        pending_candidate["room_name"] = reported_task_details[
                            "room_name"
                        ]
                    for key, value in reported_task_details.items():
                        if key in {"room_id", "room_name"}:
                            continue
                        pending_candidate.setdefault(key, value)
            else:
                self.pending_active_working_status = None
                self.pending_active_working_status_time = 0.0
            self.clear_task_details()

    def _update_battery_level(self, raw_value: Any) -> None:
        """Update battery level from base-status telemetry."""
        bat = _to_float32(raw_value)
        if bat is None:
            return
        self.battery_level = round(bat)

    def update_from_base_status(self, decoded: dict[str, Any]) -> None:
        """Update state from a decoded robot_base_status message.

        Battery (confirmed via 35-min monitor capture):
          Field 2  = real-time battery as IEEE 754 float32
                     (1118175232 → 83.0%, matching app ~84%)
          Field 38 = static battery health (always 100; design capacity)

        Field 3 sub-fields (confirmed via live test):
          3.1  = WorkingStatus enum
          3.2  = 1 means PAUSED
          3.7  = 1 means RETURNING to dock (live-validated)
          3.10 = dock sub-state (1=docked, 2=docking in progress)
          3.12 = dock activity (values 2, 6 observed)

        Dock indicators (validated via dock_research.py, 5 captures):
          Field 11 = 2 when docked, 1 when undocked
          Field 47 = 3 when docked, 2 when undocked

        Note: field 32 mirrors field 3 exactly (redundant).
        """
        self.raw_base_status = decoded
        self.has_current_dock_presence_signal = False
        if "2" in decoded:
            self._update_battery_level(decoded["2"])
        # Field 11 = dock indicator (2=docked, 1=undocked)
        if "11" in decoded:
            try:
                self.dock_field11 = int(decoded["11"])
            except (ValueError, TypeError):
                self.dock_field11 = 0
        # Field 47 = dock indicator (3=docked, 2=undocked)
        if "47" in decoded:
            try:
                self.dock_field47 = int(decoded["47"])
            except (ValueError, TypeError):
                self.dock_field47 = 0
        if "50" in decoded:
            try:
                self.dock_light_mode = int(decoded["50"])
            except (ValueError, TypeError):
                self.dock_light_mode = None
        else:
            self.dock_light_mode = 0
        # Field 3 is a nested message: {1: state_int, ...}
        # Sub-field layout differs across firmware versions:
        #   Old FW: {1: ws, 2: paused, 3: dock_presence, 7: returning,
        #            10: dock_sub, 12: dock_activity}
        #   v01.07.23+: {1: ws, 4: ?, 11: ?}; sub-fields 2/3/7/10/12 absent
        # bbp may also return a list for repeated messages.
        field3 = decoded.get("3")
        if isinstance(field3, list):
            field3 = field3[0] if field3 else None
        current_reports_docked = False
        if isinstance(field3, dict):
            is_paused = bool(field3.get("2"))
            current_presence = _optional_int(field3.get("3"))
            current_sub_state = _optional_int(field3.get("10"))
            current_field11 = _optional_int(decoded.get("11"))
            current_field47 = _optional_int(decoded.get("47"))
            if current_presence is not None:
                self.dock_presence = current_presence
            if current_sub_state is not None:
                self.dock_sub_state = current_sub_state
            current_reports_docked = (
                current_presence in (1, 6)
                or current_sub_state == 1
                or (current_field11 is not None and current_field11 >= 2)
                or current_field47 in (1, 3)
            )
            self.has_current_dock_presence_signal = current_reports_docked
            current_reports_off_dock = (
                current_presence == 2
                or current_sub_state == 2
                or (current_field11 == 1 and current_field47 == 2)
            )
            if current_reports_docked or current_reports_off_dock:
                if current_presence is None:
                    self.dock_presence = 0
                if current_sub_state is None:
                    self.dock_sub_state = 0
                if current_field11 is None:
                    self.dock_field11 = 0
                if current_field47 is None:
                    self.dock_field47 = 0
            if "1" in field3:
                try:
                    next_working_status = WorkingStatus(int(field3["1"]))
                except (ValueError, TypeError):
                    raw_val = field3["1"]
                    if raw_val not in _WARNED_WORKING_STATUS:
                        _WARNED_WORKING_STATUS.add(raw_val)
                        _LOGGER.warning(
                            "Unknown working_status value: %s — treating as UNKNOWN. "
                            "Please report this value at the GitHub repo. "
                            "(further occurrences of this value are suppressed)",
                            raw_val,
                    )
                    next_working_status = WorkingStatus.UNKNOWN
                self.working_status = next_working_status
                terminal_robot = self.working_status in {
                    WorkingStatus.TASK_COMPLETED,
                    WorkingStatus.ERROR,
                }
                terminal_dock = self.working_status in {
                    WorkingStatus.DOCKED,
                    WorkingStatus.CHARGED,
                    WorkingStatus.DOCKED_V2,
                }
                if terminal_robot or (
                    terminal_dock and not self.has_explicit_off_dock_signal
                ):
                    self.last_terminal_working_status_time = time.monotonic()
                    self.terminal_working_status_generation += 1
                elif (
                    terminal_dock
                    or current_reports_off_dock
                    or self.working_status in ACTIVE_CLEANING_STATUSES
                ):
                    self.last_terminal_working_status_time = 0.0
                if (
                    self.working_status not in ACTIVE_CLEANING_STATUSES
                    and not is_paused
                    and not self.has_assumed_robot_clean
                ):
                    self.last_active_working_status_time = 0.0
                    self.clear_task_details()
                if self.working_status in {
                    WorkingStatus.TASK_COMPLETED,
                    WorkingStatus.ERROR,
                }:
                    self.clear_assumed_robot_clean()
                    self.pending_active_working_status = None
                    self.pending_active_working_status_time = 0.0
            # Sub-field 2: paused overlay (0 or absent = not paused, 1 = paused)
            self.is_paused = is_paused
            # Sub-field 7: returning to dock on old FW (value 1 = returning).
            # On newer FW, field 7 is repurposed (e.g. value 7 during cleaning).
            # Only treat value 1 as returning — other values are not the flag.
            self.is_returning_to_dock = field3.get("7") == 1
            if "12" in field3:
                with contextlib.suppress(ValueError, TypeError):
                    self.dock_activity = int(field3["12"])
            elif self.working_status not in ACTIVE_CLEANING_STATUSES:
                self.dock_activity = 0
            self.station_activity = 0
            if "18" in field3:
                with contextlib.suppress(ValueError, TypeError):
                    self.station_activity = int(field3["18"])
            if self.working_status in ACTIVE_CLEANING_STATUSES:
                self.clear_assumed_robot_clean()
                if "10" not in field3:
                    self.dock_sub_state = 0
                self.dock_activity = 0
                self.station_activity = 0
                self.clear_assumed_dock_task()
                if "3" not in field3:
                    self.dock_presence = 2
                if "11" not in decoded:
                    self.dock_field11 = 1
                if "47" not in decoded:
                    self.dock_field47 = 2
            elif self.dock_activity > 0:
                # Cleaning packets synthesize off-dock defaults for omitted
                # fields. Do not let those stale defaults override a later
                # dock-activity packet, but retain any off-dock value that the
                # current packet reports explicitly.
                if "3" not in field3:
                    self.dock_presence = 0
                if "10" not in field3:
                    self.dock_sub_state = 0
                if "11" not in decoded:
                    self.dock_field11 = 0
                if "47" not in decoded:
                    self.dock_field47 = 0
            # Log unrecognized sub-fields for future firmware mapping
            _known_f3 = {"1", "2", "3", "7", "10", "12", "18"}
            _unknown_f3 = set(field3.keys()) - _known_f3
            if _unknown_f3:
                _LOGGER.debug(
                    "field3 unrecognized sub-fields: %s",
                    {k: field3[k] for k in sorted(_unknown_f3)},
                )
            if (
                self.station_activity <= 0
                and self.dock_activity in (0, 2, 6)
                and self.has_dock_presence_signal
                and self.assumed_active_dock_task is None
                and self.working_status
                in (
                    WorkingStatus.UNKNOWN,
                    WorkingStatus.STANDBY,
                    WorkingStatus.DOCKED,
                    WorkingStatus.CHARGED,
                    WorkingStatus.DOCKED_V2,
                    WorkingStatus.TASK_COMPLETED,
                )
                and not self.has_recent_dock_drying_status
            ):
                self.clear_dock_drying_task()
                self.has_dock_drying_timer_snapshot = False
                self.has_unmapped_dock_drying_timer = False
                self.clear_assumed_dock_task()
            telemetry_tasks = self.telemetry_dock_task_keys
            if (
                telemetry_tasks
                and self.assumed_active_dock_task is not None
            ):
                self.clear_assumed_dock_task()
        elif field3 is not None:
            _LOGGER.warning(
                "field3 is %s (expected dict): %r — state may not update. "
                "Please report this at the GitHub repo.",
                type(field3).__name__, field3,
            )
        if isinstance(field3, dict) and "1" in field3:
            self._clear_assumed_robot_clean_on_terminal_base_status(
                is_paused=bool(field3.get("2"))
            )
        standby_docked = (
            isinstance(field3, dict)
            and self.working_status == WorkingStatus.STANDBY
            and not self.is_paused
            and not self.has_assumed_robot_clean
            and current_reports_docked
        )
        has_current_working_status = isinstance(field3, dict) and "1" in field3
        terminal_docked = standby_docked or (
            has_current_working_status
            and self.working_status
            in (
                WorkingStatus.DOCKED,
                WorkingStatus.CHARGED,
                WorkingStatus.DOCKED_V2,
            )
        )
        terminal_robot = has_current_working_status and self.working_status in (
            WorkingStatus.TASK_COMPLETED,
            WorkingStatus.ERROR,
        )
        if standby_docked:
            self.last_terminal_working_status_time = time.monotonic()
            self.terminal_working_status_generation += 1
        if (
            (terminal_robot or (terminal_docked and not self.has_explicit_off_dock_signal))
            and not self.has_assumed_robot_clean
        ):
            # The paused overlay can remain set after docking. A terminal
            # base-status packet is authoritative over retained task context.
            self.last_active_working_status_time = 0.0
            self.clear_task_details()
        self._update_consumables(decoded)
        if "13" in decoded:
            raw = decoded["13"]
            if isinstance(raw, bytes):
                self.binded_uuid = raw.decode("utf-8", errors="replace")
            else:
                self.binded_uuid = str(raw)
                if self.binded_uuid.startswith("b'"):
                    self.binded_uuid = self.binded_uuid[2:-1]
        if "15" in decoded:
            with contextlib.suppress(ValueError, TypeError):
                self.terminate_reason = int(decoded["15"])

    def _update_consumables(self, decoded: dict[str, Any]) -> None:
        """Parse trustworthy hardware-sampled base_status fields."""
        if "35" in decoded:
            score = _to_float32(decoded["35"])
            if score is not None:
                self.dust_bag_health = score
        if "41" in decoded:
            self.detergent_remaining = int(decoded["41"])
        if "38" in decoded:
            self.curing_agent_consumption_percent = int(decoded["38"])
        if "36" in decoded:
            self.station_bag_health_reset_time = int(decoded["36"])
        # Always reparse, even when field 1 is absent. Protobuf omits an empty
        # repeated field, so a recovered robot drops it; otherwise the prior
        # fault would stick forever.
        self._parse_error_codes(decoded.get("1"))
        if self.has_error:
            self.pending_active_working_status = None
            self.pending_active_working_status_time = 0.0
        for attr, key in (
            ("clean_water_tank_state", "23"), ("sewage_tank_state", "24"),
            ("dust_box_state", "20"), ("dust_bag_state", "21"),
            ("station_bag_state", "39"),
        ):
            if key in decoded:
                with contextlib.suppress(ValueError, TypeError):
                    setattr(self, attr, int(decoded[key]))

    def _parse_error_codes(self, raw: Any) -> None:
        """Decode base_status field 1 (repeated ErrorCode{1:identityCode, 2:level, 3:debugDetail}).

        Empty/zero codes mean no active fault. bbp gives a dict for one entry, a list for many.
        """
        entries = raw if isinstance(raw, list) else [raw] if isinstance(raw, dict) else []
        codes: list[int] = []
        level = 0
        detail = ""
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("1"):
                continue
            try:
                codes.append(int(entry["1"]))
            except (ValueError, TypeError):
                continue
            with contextlib.suppress(ValueError, TypeError):
                level = max(level, int(entry.get("2", 0)))
            raw_detail = entry.get("3")
            if isinstance(raw_detail, bytes):
                detail = detail or raw_detail.decode("utf-8", errors="replace")
            elif isinstance(raw_detail, str):
                detail = detail or raw_detail
        self.error_codes = codes
        self.error_level = level
        self.error_detail = detail
        self.has_error = bool(codes)

    def update_battery_from_base_status(self, decoded: dict[str, Any]) -> None:
        """Update only trustworthy hardware-sampled base_status fields.

        Used when the robot is not broadcasting, such as deep sleep on dock.
        get_status() returns a current battery counter but a stale
        working_status firmware cache from the last active session, so
        working_status is deliberately skipped.
        """
        self.raw_base_status = decoded
        if "2" in decoded:
            self._update_battery_level(decoded["2"])
        self._update_consumables(decoded)

    def update_from_upgrade_status(self, decoded: dict[str, Any]) -> None:
        """Update state from a decoded upgrade_status message.

        Fields: 2 status, 4 stage, 7 currentVersion, 8 targetVersion.
        """
        if "7" in decoded:
            raw = decoded["7"]
            if isinstance(raw, bytes):
                self.firmware_version = raw.decode("utf-8", errors="replace")
            else:
                self.firmware_version = str(raw)
                if self.firmware_version.startswith("b'"):
                    self.firmware_version = self.firmware_version[2:-1]
        if "8" in decoded:
            raw = decoded["8"]
            if isinstance(raw, bytes):
                self.firmware_target = raw.decode("utf-8", errors="replace")
            else:
                self.firmware_target = str(raw)
                if self.firmware_target.startswith("b'"):
                    self.firmware_target = self.firmware_target[2:-1]
        if "2" in decoded:
            self.upgrade_status = int(decoded["2"])
        if "4" in decoded:
            self.upgrade_stage = int(decoded["4"])

    def update_from_download_status(self, decoded: dict[str, Any]) -> None:
        """Update state from a decoded download_status message (voice/timbre pack).

        Field 3 = state; field 1 is `type` (download category), not status.
        """
        if "3" in decoded:
            self.download_status = int(decoded["3"])

    def update_from_consumable_info(self, decoded: dict[str, Any]) -> None:
        """Parse a consumable/get_consumable_info response into maintain/replace alert lists.

        {1: ConsumableInfoPayload{1: maintainItems[], 2: replaceItems[]}}; an empty
        payload means nothing needs attention. Per-consumable life % is cloud-only.
        """
        payload = decoded.get("1")
        if not isinstance(payload, dict):
            payload = {}
        self.maintain_items = _enum_int_list(payload.get("1"))
        self.replace_items = _enum_int_list(payload.get("2"))
