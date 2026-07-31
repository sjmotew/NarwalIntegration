"""Data models for Narwal vacuum state."""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field
from typing import Any, ClassVar

_LOGGER = logging.getLogger(__name__)

from .const import CommandResult, FanLevel, MopHumidity, WorkingStatus


@dataclass
class DeviceInfo:
    """Device identity from get_device_info response."""

    product_key: str = ""
    device_id: str = ""
    firmware_version: str = ""


@dataclass
class RoomInfo:
    """A room on the map.

    Fields from get_map / get_editable_map field 2.12:
      field 1: room_id (matches pixel value >> 8 in map grid)
      field 2: room_sub_type — ROOM_TYPE enum from APK (0=unspecified,
               1=main bedroom, 2=secondary room, 3=living room, 4=kitchen,
               5=study, 6=bathroom, 7=dining room, 8=corridor, 9=balcony,
               10=utility room, 11=cloak room, 12=nursery, 13=recreation,
               14=shower room, 15=other)
      field 3: user-assigned name (UTF-8, empty if not named by user)
      field 4: category (1=room, 2=utility/small space)
      field 8: instance_index (1-based, for numbering duplicates: Bathroom 1, 2, 3...)
    """

    room_id: int = 0
    name: str = ""  # user-assigned name from field 3
    room_sub_type: int = 0  # ROOM_TYPE enum from field 2
    category: int = 0  # 1=room, 2=utility (field 4)
    instance_index: int = 0  # numbering for duplicates (field 8)
    model_key: str = ""  # product_key — selects per-model name overrides

    # ROOM_TYPE enum → default display name (from APK libapp.so string analysis)
    ROOM_TYPE_NAMES: dict[int, str] = field(default=None, repr=False)

    # Per-model overrides where Narwal renamed sub-types between models.
    # Confirmed on Narwal Flow 2 (QxMSPG6VSO, firmware v01.07.16.01) — see #22.
    MODEL_ROOM_TYPE_OVERRIDES: ClassVar[dict[str, dict[int, str]]] = {
        "QxMSPG6VSO": {  # Flow 2
            1: "Master Bedroom",
            5: "Bathroom",
            10: "Corridor",
        },
    }

    def __post_init__(self):
        if self.ROOM_TYPE_NAMES is None:
            object.__setattr__(self, "ROOM_TYPE_NAMES", {
                0: "Room",
                1: "Primary Bedroom",
                2: "Secondary Bedroom",
                3: "Living Room",
                4: "Kitchen",
                5: "Study",
                6: "Bathroom",
                7: "Dining Room",
                8: "Corridor",
                9: "Balcony",
                10: "Utility Room",
                11: "Cloak Room",
                12: "Nursery",
                13: "Recreation Room",
                14: "Shower Room",
                15: "Other",
            })

    @property
    def display_name(self) -> str:
        """Return user name if set, otherwise generate default from ROOM_TYPE enum.

        Matches Narwal app behavior: unnamed rooms show their type name
        with an instance number suffix for duplicates (e.g. "Bathroom 2").
        Per-model overrides apply where Narwal renamed sub-types (e.g. Flow 2
        renames sub_type 1 → "Master Bedroom" vs Flow 1's "Primary Bedroom").
        """
        if self.name:
            return self.name
        overrides = self.MODEL_ROOM_TYPE_OVERRIDES.get(self.model_key, {})
        base = overrides.get(self.room_sub_type) or \
            self.ROOM_TYPE_NAMES.get(self.room_sub_type, "Room")
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
    def from_response(
        cls, decoded: dict[str, Any], product_key: str = ""
    ) -> MapData:
        """Parse map data from a get_map field5 response.

        Args:
            decoded: blackboxprotobuf-decoded get_map response.
            product_key: Device product key — propagated to RoomInfo so model-
                specific room-type name overrides apply (see #22 for Flow 2).
        """
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
                    model_key=product_key,
                ))

        compressed = payload.get("17", b"")
        if isinstance(compressed, str):
            compressed = compressed.encode("latin-1")

        resolution = int(payload.get("3", 0))

        # Extract origin offsets from field 6 (coordinate transform).
        # Field 6: {1: origin_y, 2: ?, 3: origin_x, 4: resolution}
        # Positions are in grid-offset units: pixel = raw - origin
        origin_x = 0
        origin_y = 0
        field6 = payload.get("6")
        if isinstance(field6, dict):
            try:
                origin_x = int(field6.get("3", 0))
            except (ValueError, TypeError):
                pass
            try:
                origin_y = int(field6.get("1", 0))
            except (ValueError, TypeError):
                pass

        # Parse dock position from field 8 (dock/charging station location).
        # Field 8 structure: {1: {1: x_dm, 2: y_dm}, 2: heading_rad}
        # Coordinates are in decimeters (same as display_map field 5).
        # Matches display_map field 5 (confirmed via live capture cross-reference).
        # Pixel transform: px = (x_dm * 10) / cm_per_pixel - origin
        dock_x = None
        dock_y = None
        field8 = payload.get("8")
        if isinstance(field8, dict) and resolution > 0:
            pos = field8.get("1")
            if isinstance(pos, dict) and "1" in pos and "2" in pos:
                try:
                    x_dm = _to_float32(pos["1"])
                    y_dm = _to_float32(pos["2"])
                    if x_dm is not None and y_dm is not None:
                        dock_x = x_dm - origin_x
                        dock_y = y_dm - origin_y
                except (struct.error, OverflowError, ValueError, TypeError):
                    pass

        # Parse obstacle/furniture annotations from field 32 (MapFurnitureInfoList)
        obstacles: list[ObstacleInfo] = []
        field32 = payload.get("32")
        if isinstance(field32, dict):
            obstacles = _parse_obstacles(field32)

        return cls(
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

    Sent every ~1.5s during active cleaning. Contains robot position in cm,
    heading in radians, and a small cleaned-area grid overlay (NOT the full
    house map — that comes from get_map).

    Validated field layout (live capture 2026-02-28, 13 broadcasts):
      field 1.1: {1: x_cm, 2: y_cm} — robot position as float32 centimeters
      field 1.2: heading as float32 radians
      field 5: dock/reference position (constant, same format)
      field 7: cleaned-area grid {1: width, 2: height, 3: compressed_bytes}
      field 10: timestamp in milliseconds since epoch
      field 12: active room list
    """

    robot_x: float = 0.0  # decimeters, world coordinates
    robot_y: float = 0.0  # decimeters, world coordinates
    robot_heading: float = 0.0  # degrees (converted from radians for renderer)
    timestamp: int = 0  # milliseconds since epoch (field 10)
    # Dock/reference position from field 5 (same coordinate system as robot)
    dock_ref_x: float = 0.0
    dock_ref_y: float = 0.0

    def to_grid_coords(
        self, resolution: int, origin_x: int, origin_y: int,
    ) -> tuple[float, float] | None:
        """Convert world-coordinate position (dm) to grid pixel coordinates.

        display_map positions are in decimeters (validated via live capture).
        Same coordinate system as get_map field 8 (dock position).
          pixel = (x_dm * 10) / cm_per_pixel - origin_offset

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
        # Positions are in grid-offset units: pixel = raw - origin
        px = self.robot_x - origin_x
        py = self.robot_y - origin_y
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
            try:
                result.timestamp = int(decoded["10"])
            except (ValueError, TypeError):
                pass

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

    # Consumables / station / fault (base_status; present on dock and during cleaning)
    dust_bag_health: float = 0.0  # field 35 stationBagHealthScore (%)
    detergent_remaining: int = 0  # field 41 heavyDetergentRemainPercent (%)
    curing_agent_consumption_percent: int = 0  # field 38
    has_error: bool = False  # field 1 errorCode has an active code
    error_codes: list[int] = field(default_factory=list)  # field 1 ErrorCode.identityCode(s)
    error_level: int = 0  # ErrorCode.level (field 1 sub-2)
    error_detail: str = ""  # ErrorCode.debugDetail (field 1 sub-3)

    # Station tank/bag enum states (base_status; None = not reported by this model).
    # 0=unspecified, 1=ok/installed, ≥2=attention (empty/abnormal/replace) — see BaseStatusField.
    clean_water_tank_state: int | None = None  # field 23 (CleanWaterTankState)
    sewage_tank_state: int | None = None  # field 24 (SewageTankState)
    dust_box_state: int | None = None  # field 20 (DustBoxState)
    dust_bag_state: int | None = None  # field 21 (DustBagState)
    station_bag_state: int | None = None  # field 39 (StationBagStatus)

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

    # Raw data for fields we haven't fully decoded yet
    raw_base_status: dict[str, Any] = field(default_factory=dict)
    raw_working_status: dict[str, Any] = field(default_factory=dict)

    @property
    def is_cleaning(self) -> bool:
        """True when actively cleaning (not paused, not returning to dock)."""
        return (
            self.working_status in (WorkingStatus.CLEANING, WorkingStatus.CLEANING_ALT)
            and not self.is_paused
            and not self.is_returning_to_dock
        )

    @property
    def is_docked(self) -> bool:
        """True when on dock: DOCKED(10), CHARGED(14), DOCKED_V2(2), or dock field signals.

        Dock signals (checked for STANDBY, UNKNOWN, and any unmapped status):
          - dock_sub_state == 1 (field 3.10, old FW only)
          - dock_activity > 0 (field 3.12, old FW only)
          - dock_field11 >= 2 (field 11: old FW 2=docked/1=undocked,
                               v01.07.23 3=docked)
          - dock_field47 in (1, 3) (field 47: old FW 3=docked/2=undocked,
                                    v01.07.23 1=docked)

        Dock fields are checked for STANDBY/UNKNOWN and any status where
        cleaning is not active, since the robot can report unmapped states
        (e.g. self-test) while physically docked.
        """
        if self.working_status in (
            WorkingStatus.DOCKED, WorkingStatus.CHARGED, WorkingStatus.DOCKED_V2,
        ):
            return True
        if self.working_status in (WorkingStatus.CLEANING, WorkingStatus.CLEANING_ALT):
            return False
        # For STANDBY, UNKNOWN, or any other status: check dock field signals.
        # Values differ across firmware versions:
        #   Old FW: dock_sub_state=1, dock_field11=2, dock_field47=3
        #   v01.07.23.00: dock_sub_state absent, dock_field11=3, dock_field47=1
        if self.dock_sub_state == 1:
            return True
        if self.dock_activity > 0:
            return True
        if self.dock_field11 >= 2:
            return True
        if self.dock_field47 in (1, 3):
            return True
        return False

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
        if self.working_status not in (
            WorkingStatus.CLEANING, WorkingStatus.CLEANING_ALT,
        ):
            return False
        return self.is_returning_to_dock and self.dock_sub_state == 2

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
        if "3" in decoded:
            try:
                self.cleaning_time = int(decoded["3"])
            except (ValueError, TypeError):
                pass
        if "2" in decoded:
            area = _to_float32(decoded["2"])
            if area is not None and area >= 0:
                self.cleaning_area = area

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
        # Field 3 is a nested message: {1: state_int, ...}
        # Sub-field layout differs across firmware versions:
        #   Old FW: {1: ws, 2: paused, 3: dock_presence, 7: returning, 10: dock_sub, 12: dock_activity}
        #   v01.07.23+: {1: ws, 4: ?, 11: ?} — sub-fields 2/3/7/10/12 absent
        # bbp may also return a list for repeated messages.
        field3 = decoded.get("3")
        if isinstance(field3, list):
            field3 = field3[0] if field3 else None
        if isinstance(field3, dict):
            if "1" in field3:
                try:
                    self.working_status = WorkingStatus(int(field3["1"]))
                except (ValueError, TypeError):
                    raw_val = field3["1"]
                    _LOGGER.warning(
                        "Unknown working_status value: %s — treating as UNKNOWN. "
                        "Please report this value at the GitHub repo.",
                        raw_val,
                    )
                    self.working_status = WorkingStatus.UNKNOWN
            # Sub-field 2: paused overlay (0 or absent = not paused, 1 = paused)
            self.is_paused = bool(field3.get("2"))
            # Sub-field 7: returning to dock on old FW (value 1 = returning).
            # On newer FW, field 7 is repurposed (e.g. value 7 during cleaning).
            # Only treat value 1 as returning — other values are not the flag.
            self.is_returning_to_dock = field3.get("7") == 1
            if "10" in field3:
                try:
                    self.dock_sub_state = int(field3["10"])
                except (ValueError, TypeError):
                    pass
            if "12" in field3:
                try:
                    self.dock_activity = int(field3["12"])
                except (ValueError, TypeError):
                    pass
            if "3" in field3:
                try:
                    self.dock_presence = int(field3["3"])
                except (ValueError, TypeError):
                    pass
            # Log unrecognized sub-fields for future firmware mapping
            _known_f3 = {"1", "2", "3", "7", "10", "12"}
            _unknown_f3 = set(field3.keys()) - _known_f3
            if _unknown_f3:
                _LOGGER.debug(
                    "field3 unrecognized sub-fields: %s",
                    {k: field3[k] for k in sorted(_unknown_f3)},
                )
        elif field3 is not None:
            _LOGGER.warning(
                "field3 is %s (expected dict): %r — state may not update. "
                "Please report this at the GitHub repo.",
                type(field3).__name__, field3,
            )
        if "2" in decoded:
            # Field 2 = real-time battery SOC as float32 (batteryPercentage)
            # (e.g. 1118175232 → 83.0%; bbp may return int or float)
            bat = _to_float32(decoded["2"])
            if bat is not None:
                self.battery_level = round(bat)
        self._update_consumables(decoded)
        if "13" in decoded:
            raw = decoded["13"]
            if isinstance(raw, bytes):
                self.binded_uuid = raw.decode("utf-8", errors="replace")
            else:
                self.binded_uuid = str(raw)
                if self.binded_uuid.startswith("b'"):
                    self.binded_uuid = self.binded_uuid[2:-1]

    def _update_consumables(self, decoded: dict[str, Any]) -> None:
        """Parse base_status consumable/station/fault fields — hardware-sampled, so trustworthy even in deep-sleep battery-only updates."""
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
        # Always reparse (even when field 1 is absent) — protobuf omits an empty repeated field, so a recovered robot drops it; without this the prior fault would stick forever.
        self._parse_error_codes(decoded.get("1"))
        for attr, key in (
            ("clean_water_tank_state", "23"), ("sewage_tank_state", "24"),
            ("dust_box_state", "20"), ("dust_bag_state", "21"),
            ("station_bag_state", "39"),
        ):
            if key in decoded:
                try:
                    setattr(self, attr, int(decoded[key]))
                except (ValueError, TypeError):
                    pass

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
            try:
                level = max(level, int(entry.get("2", 0)))
            except (ValueError, TypeError):
                pass
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
        """Update only the trustworthy hardware-sampled fields (battery + consumable/station/fault/tank state, via _update_consumables) from a base_status response.

        Used when the robot is not broadcasting (deep sleep on dock): get_status() returns a current battery counter but a stale working_status (firmware cache from the last active session), so working_status is deliberately skipped.
        """
        self.raw_base_status = decoded
        if "2" in decoded:
            bat = _to_float32(decoded["2"])
            if bat is not None:
                self.battery_level = round(bat)
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
