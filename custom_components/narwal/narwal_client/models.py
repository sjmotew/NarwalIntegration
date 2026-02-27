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
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_response(cls, decoded: dict[str, Any]) -> MapData:
        """Parse map data from a get_map field5 response."""
        payload = decoded.get("2", {})
        if not payload:
            return cls()

        rooms = []
        for room in payload.get("12", []):
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

        # Parse dock position from field 48 (timestamped positions in cm).
        # Field 48 is a list of {1: id, 2: {1: x_cm, 2: y_cm}, 3: timestamp}.
        # Use the entry with the latest timestamp (most recent dock position).
        # Coordinates are in centimeters; convert to pixels via resolution (mm/pixel).
        # Pixel transform: px = (x_cm * 10 / resolution) - field6.3
        dock_x = None
        dock_y = None
        field48 = payload.get("48")
        field6 = payload.get("6")
        if isinstance(field48, list) and isinstance(field6, dict):
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
                    if x_cm is not None and y_cm is not None and resolution > 0:
                        cm_per_pixel = resolution / 10  # 60mm/px = 6cm/px
                        origin_x = int(field6.get("3", 0))  # x pixel offset
                        origin_y = int(field6.get("1", 0))  # y pixel offset
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
            raw=payload,
        )


@dataclass
class MapDisplayData:
    """Real-time map display data from map/display_map broadcasts.

    Sent during cleaning sessions with updated map grid and robot position.
    """

    width: int = 0
    height: int = 0
    compressed_grid: bytes = b""
    robot_x: float = 0.0
    robot_y: float = 0.0
    robot_heading: float = 0.0
    timestamp: int = 0

    @classmethod
    def from_broadcast(cls, decoded: dict[str, Any]) -> MapDisplayData:
        """Parse display_map broadcast payload.

        NEEDS LIVE VALIDATION: field layout inferred from protocol analysis.
        Must capture display_map during an active cleaning session to confirm.

        Expected structure (protobuf fields — needs confirmation):
          field 7: map data submessage
            7.1: width
            7.2: height
            7.3: compressed grid bytes
          field 1: robot position submessage
            1.1: x coordinate
            1.2: y coordinate
            1.3: heading
        """
        result = cls()

        # Map grid — try field 7 (nested) or fall back to searching bytes fields
        field7 = decoded.get("7", {})
        if isinstance(field7, dict):
            result.width = int(field7.get("1", 0))
            result.height = int(field7.get("2", 0))
            compressed = field7.get("3", b"")
            if isinstance(compressed, str):
                compressed = compressed.encode("latin-1")
            result.compressed_grid = compressed if isinstance(compressed, bytes) else b""
        elif isinstance(field7, bytes) and len(field7) > 100:
            # Field 7 might be raw compressed bytes
            result.compressed_grid = field7

        # Robot position — try field 1
        field1 = decoded.get("1", {})
        if isinstance(field1, dict):
            try:
                result.robot_x = float(field1.get("1", 0))
                result.robot_y = float(field1.get("2", 0))
                result.robot_heading = float(field1.get("3", 0))
            except (ValueError, TypeError):
                pass

        # Fallback: if width/height are 0, try other known structures
        if result.width == 0 and result.height == 0:
            # Some firmwares put width/height at the top level
            if "4" in decoded:
                try:
                    result.width = int(decoded["4"])
                except (ValueError, TypeError):
                    pass
            if "5" in decoded:
                try:
                    result.height = int(decoded["5"])
                except (ValueError, TypeError):
                    pass

        # Look for large bytes fields as potential compressed grid
        if not result.compressed_grid:
            for key, val in decoded.items():
                if isinstance(val, bytes) and len(val) > 100:
                    result.compressed_grid = val
                    break

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
    battery_level: int = 0
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

    # Raw data for fields we haven't fully decoded yet
    raw_base_status: dict[str, Any] = field(default_factory=dict)
    raw_working_status: dict[str, Any] = field(default_factory=dict)

    @property
    def is_cleaning(self) -> bool:
        return self.working_status in (WorkingStatus.CLEANING, WorkingStatus.CLEANING_ALT) and not self.is_paused

    @property
    def is_docked(self) -> bool:
        """True when on dock: DOCKED(10), CHARGED(14), or STANDBY(1) with dock sub-state=1.

        dock_sub_state can linger at 1 after the robot leaves the dock,
        so only use it when working_status is STANDBY (the ambiguous case
        where the robot may be idle on or off the dock).
        """
        if self.working_status in (WorkingStatus.DOCKED, WorkingStatus.CHARGED):
            return True
        if self.working_status == WorkingStatus.STANDBY and self.dock_sub_state == 1:
            return True
        return False

    @property
    def is_returning(self) -> bool:
        """True when the robot is actively returning to the dock.

        dock_sub_state == 2 means 'docking in progress'. We only consider
        this as RETURNING when the robot is not actively cleaning.

        NEEDS LIVE VALIDATION: inferred from protocol analysis, not yet
        confirmed during a real recall-to-dock sequence.
        """
        return self.dock_sub_state == 2 and not self.is_cleaning

    def update_from_working_status(self, decoded: dict[str, Any]) -> None:
        """Update state from a decoded working_status message.

        Field 3 = current session elapsed time (seconds), NOT state enum.
        Field 13 = cleaning area (cm²) — may be cumulative.
        Field 15 = cleaning time (seconds) — may be cumulative.
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
        """Update state from a decoded robot_base_status message."""
        self.raw_base_status = decoded
        # Field 3 is a nested message: {1: state_int, ...}
        field3 = decoded.get("3")
        if isinstance(field3, dict) and "1" in field3:
            try:
                self.working_status = WorkingStatus(int(field3["1"]))
            except (ValueError, TypeError):
                self.working_status = WorkingStatus.UNKNOWN
            # Sub-field 2 = 1 means paused (overlay on cleaning state)
            self.is_paused = bool(field3.get("2"))
            # Sub-field 10 = dock sub-state (1=docked, 2=docking in progress)
            try:
                self.dock_sub_state = int(field3.get("10", 0))
            except (ValueError, TypeError):
                self.dock_sub_state = 0
        if "38" in decoded:
            # Field 38 is the battery percentage — confirmed correct field.
            # Updates in real-time during cleaning; triggers auto-return when low.
            # Not yet observed below 100 (robot always fully charged during dev).
            self.battery_level = int(decoded["38"])
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
