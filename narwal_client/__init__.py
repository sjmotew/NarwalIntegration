"""Narwal robot vacuum client library — local WebSocket API."""

from .client import (
    NarwalClient,
    NarwalCommandError,
    NarwalConnectionError,
    RoomCleanSettings,
)
from .const import (
    AmbientLightCtrlType,
    CleaningRoute,
    CommandResult,
    FanLevel,
    MopHumidity,
    MopStrengthLevel,
    WorkingStatus,
    WorkMode,
)
from .models import (
    CommandResponse,
    DeviceInfo,
    DockStatusFreshness,
    MapData,
    MapDisplayData,
    NarwalState,
    RoomInfo,
)
from .protocol import build_frame, parse_frame

__all__ = [
    "NarwalClient",
    "NarwalCommandError",
    "NarwalConnectionError",
    "RoomCleanSettings",
    "NarwalState",
    "CommandResponse",
    "CommandResult",
    "CleaningRoute",
    "DeviceInfo",
    "DockStatusFreshness",
    "AmbientLightCtrlType",
    "FanLevel",
    "MapData",
    "MapDisplayData",
    "MopHumidity",
    "MopStrengthLevel",
    "RoomInfo",
    "WorkMode",
    "WorkingStatus",
    "build_frame",
    "parse_frame",
]
