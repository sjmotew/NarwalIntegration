"""Data models for Narwal vacuum state."""

from __future__ import annotations

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

        return cls(
            width=int(payload.get("4", 0)),
            height=int(payload.get("5", 0)),
            resolution=int(payload.get("3", 0)),
            rooms=rooms,
            compressed_map=compressed if isinstance(compressed, bytes) else b"",
            area=int(payload.get("33", 0)),
            created_at=int(payload.get("34", 0)),
            raw=payload,
        )


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
        """True when on dock: either state DOCKED(10) or dock sub-state=1 (fully docked)."""
        return self.working_status == WorkingStatus.DOCKED or self.dock_sub_state == 1

    @property
    def is_returning(self) -> bool:
        return self.working_status == WorkingStatus.RETURNING

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
