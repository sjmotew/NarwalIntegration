"""Data models for Narwal vacuum state."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any

from .const import CommandResult, FanLevel, MopHumidity, WorkingStatus


@dataclass
class DeviceInfo:
    """Device identity from get_device_info response."""

    product_key: str = ""
    device_id: str = ""
    firmware_version: str = ""


@dataclass
class RoomInfo:
    """A room on the map."""

    room_id: int = 0
    name: str = ""
    room_type: int = 0


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
                    room_type=int(room.get("2", 0)),
                ))

        compressed = payload.get("17", b"")
        if isinstance(compressed, str):
            compressed = compressed.encode("latin-1")

        resolution = int(payload.get("3", 0))

        # Extract origin offsets from field 6 (coordinate transform).
        # These convert world coordinates (cm) to grid pixel coordinates:
        #   pixel_x = x_cm / cm_per_pixel - origin_x
        #   pixel_y = y_cm / cm_per_pixel - origin_y
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

        # Parse dock position from field 48 (timestamped positions in cm).
        # Field 48 is a list of {1: id, 2: {1: x_cm, 2: y_cm}, 3: timestamp}.
        # Use the entry with the latest timestamp (most recent dock position).
        # Coordinates are in centimeters; convert to pixels via resolution (mm/pixel).
        # Pixel transform: px = (x_cm * 10 / resolution) - field6.3
        # Requires field 6 for origin offsets — without it, pixel coords are wrong.
        dock_x = None
        dock_y = None
        field48 = payload.get("48")
        # bbpb returns single repeated field as dict, not [dict]
        if isinstance(field48, dict):
            field48 = [field48]
        if isinstance(field48, list) and isinstance(field6, dict) and resolution > 0:
            best_ts = -1
            best_pos = None
            for entry in field48:
                if not isinstance(entry, dict):
                    continue
                ts = 0
                try:
                    ts = int(entry.get("3", 0))
                except (ValueError, TypeError):
                    pass
                pos = entry.get("2")
                if isinstance(pos, dict) and "1" in pos and "2" in pos and ts >= best_ts:
                    best_ts = ts
                    best_pos = pos
            if best_pos is not None:
                try:
                    x_cm = _to_float32(best_pos["1"])
                    y_cm = _to_float32(best_pos["2"])
                    if x_cm is not None and y_cm is not None:
                        cm_per_pixel = resolution / 10  # 60mm/px = 6cm/px
                        dock_x = x_cm / cm_per_pixel - origin_x
                        dock_y = y_cm / cm_per_pixel - origin_y
                except (struct.error, OverflowError, ValueError, TypeError):
                    pass

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

    robot_x: float = 0.0  # centimeters, world coordinates
    robot_y: float = 0.0  # centimeters, world coordinates
    robot_heading: float = 0.0  # degrees (converted from radians for renderer)
    timestamp: int = 0  # milliseconds since epoch (field 10)

    def to_grid_coords(
        self, resolution: int, origin_x: int, origin_y: int,
    ) -> tuple[float, float] | None:
        """Convert world-coordinate position (cm) to grid pixel coordinates.

        Uses the same pixel transform as dock position in MapData.from_response():
          pixel = x_cm / cm_per_pixel - origin_offset

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
        cm_per_pixel = resolution / 10  # 60mm/px = 6cm/px
        px = self.robot_x / cm_per_pixel - origin_x
        py = self.robot_y / cm_per_pixel - origin_y
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
    battery_health: int = 0  # static design capacity from field 38 (always 100)
    firmware_version: str = ""
    firmware_target: str = ""

    # Device identity
    device_info: DeviceInfo | None = None

    # Session
    session_id: str = ""
    timestamp: int = 0

    # Position (from map data)
    position: Position | None = None

    # Cleaning stats
    cleaning_area: int = 0  # cm²
    cleaning_time: int = 0  # seconds

    # Map
    map_data: MapData | None = None
    map_display_data: MapDisplayData | None = None

    # Download/upgrade status
    download_status: int = 0
    upgrade_status_code: int = 0

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
        """True when on dock: DOCKED(10), CHARGED(14), or STANDBY(1) with dock signals.

        STANDBY(1) is ambiguous — robot can be idle on or off the dock.
        Dock signals for STANDBY (checked in priority order):
          - dock_sub_state == 1 (field 3.10, confirmed live)
          - dock_activity > 0 (field 3.12, values 2/6 when docked)
          - dock_field11 == 2 (field 11: 2=docked, 1=undocked)
          - dock_field47 == 3 (field 47: 3=docked, 2=undocked)

        Fields 11 and 47 validated via dock_research.py guided test with
        5 captures across on-dock and off-dock states — perfect correlation.
        """
        if self.working_status in (WorkingStatus.DOCKED, WorkingStatus.CHARGED):
            return True
        if self.working_status == WorkingStatus.STANDBY:
            if self.dock_sub_state == 1:
                return True
            if self.dock_activity > 0:
                return True
            if self.dock_field11 == 2:
                return True
            if self.dock_field47 == 3:
                return True
        return False

    @property
    def is_returning(self) -> bool:
        """True when the robot is actively returning to the dock.

        Live-validated: during return-to-dock, field 3 shows:
          {1=4, 7=1, 10=2} — working_status stays CLEANING(4),
          field 7=1 (returning flag), field 10=2 (docking in progress).

        We use field 3.7 (is_returning_to_dock) as the primary indicator.
        Fallback: dock_sub_state == 2 when not in a cleaning/charged/docked state.
        """
        if self.is_returning_to_dock:
            return True
        if self.dock_sub_state == 2 and self.working_status not in (
            WorkingStatus.DOCKED, WorkingStatus.CHARGED,
        ):
            return True
        return False

    def update_from_working_status(self, decoded: dict[str, Any]) -> None:
        """Update state from a decoded working_status message.

        Confirmed via 35-min monitor capture (2026-02-27):
          Field 3  = current session elapsed time (seconds)
                     (confirmed: 2136→2159 over 35-min clean)
          Field 13 = cleaning area (cm²) — CONFIRMED (18000 = 1.8m²)
          Field 15 = 600 during cleaning (purpose uncertain)
        """
        self.raw_working_status = decoded
        if "3" in decoded:
            try:
                self.cleaning_time = int(decoded["3"])
            except (ValueError, TypeError):
                pass
        if "13" in decoded:
            self.cleaning_area = int(decoded["13"])
        if "15" in decoded:
            # Field 15 may be cumulative time; prefer field 3 for current session
            pass

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
        field3 = decoded.get("3")
        if isinstance(field3, dict) and "1" in field3:
            try:
                self.working_status = WorkingStatus(int(field3["1"]))
            except (ValueError, TypeError):
                self.working_status = WorkingStatus.UNKNOWN
            # Sub-field 2 = 1 means paused (overlay on cleaning state)
            self.is_paused = bool(field3.get("2"))
            # Sub-field 7 = 1 means returning to dock (confirmed via live test)
            self.is_returning_to_dock = bool(field3.get("7"))
            # Sub-field 10 = dock sub-state (1=docked, 2=docking in progress)
            try:
                self.dock_sub_state = int(field3.get("10", 0))
            except (ValueError, TypeError):
                self.dock_sub_state = 0
            # Sub-field 12 = dock activity (values 2, 6 observed when docked)
            try:
                self.dock_activity = int(field3.get("12", 0))
            except (ValueError, TypeError):
                self.dock_activity = 0
            # Sub-field 3 = dock presence (1/6=on dock, 2=off dock)
            try:
                self.dock_presence = int(field3.get("3", 0))
            except (ValueError, TypeError):
                self.dock_presence = 0
        if "2" in decoded:
            # Field 2 = real-time battery SOC as float32
            # (e.g. 1118175232 → 83.0%; bbp may return int or float)
            bat = _to_float32(decoded["2"])
            if bat is not None:
                self.battery_level = round(bat)
        if "38" in decoded:
            # Field 38 = static battery health (always 100, design capacity)
            self.battery_health = int(decoded["38"])
        if "36" in decoded:
            self.timestamp = int(decoded["36"])
        if "13" in decoded:
            raw = decoded["13"]
            if isinstance(raw, bytes):
                self.session_id = raw.decode("utf-8", errors="replace")
            else:
                self.session_id = str(raw)
                if self.session_id.startswith("b'"):
                    self.session_id = self.session_id[2:-1]

    def update_from_upgrade_status(self, decoded: dict[str, Any]) -> None:
        """Update state from a decoded upgrade_status message."""
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
        if "4" in decoded:
            self.upgrade_status_code = int(decoded["4"])

    def update_from_download_status(self, decoded: dict[str, Any]) -> None:
        """Update state from a decoded download_status message."""
        if "1" in decoded:
            self.download_status = int(decoded["1"])
