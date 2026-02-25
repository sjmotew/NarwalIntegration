"""Narwal robot vacuum client library — local WebSocket API."""

from .client import NarwalClient, NarwalCommandError, NarwalConnectionError
from .const import CommandResult, FanLevel, MopHumidity, WorkingStatus
from .models import CommandResponse, DeviceInfo, MapData, MapDisplayData, NarwalState, RoomInfo
from .protocol import build_frame, parse_frame

__all__ = [
    "NarwalClient",
    "NarwalCommandError",
    "NarwalConnectionError",
    "NarwalState",
    "CommandResponse",
    "CommandResult",
    "DeviceInfo",
    "FanLevel",
    "MapData",
    "MapDisplayData",
    "MopHumidity",
    "RoomInfo",
    "WorkingStatus",
    "build_frame",
    "parse_frame",
]
