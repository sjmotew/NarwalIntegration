"""Websocket commands that hand map data to dashboard clients.

``narwal/trajectory`` returns the trail of the current clean as data: the same
grid points the map camera draws, up to the coordinator's 50,000-point cap.
The camera's ``path`` attribute carries a copy decimated to 400 points,
because attributes go to every frontend on every frame; a client that draws
the route itself asks for the full trail once, when it needs it.
"""

from __future__ import annotations

import base64
import struct
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN
from .coordinator import NarwalCoordinator

TRAJECTORY_COMMAND = f"{DOMAIN}/trajectory"
#: Version of the answers' shape; bumped on any incompatible change.
ANSWER_VERSION = 1


@callback
def async_setup_websocket_api(hass: HomeAssistant) -> None:
    """Register the Narwal websocket commands."""
    websocket_api.async_register_command(hass, ws_trajectory)


def _coordinator_for(hass: HomeAssistant, msg: dict[str, Any]) -> NarwalCoordinator | None:
    """Return the coordinator named by ``entry_id``, or the first loaded one."""
    coordinators: dict[str, NarwalCoordinator] = hass.data.get(DOMAIN, {})
    entry_id = msg.get("entry_id")
    if entry_id is not None:
        return coordinators.get(entry_id)
    return next(iter(coordinators.values()), None)


def _pack(values: list[float]) -> str:
    """Return little-endian float32 values as base64."""
    return base64.b64encode(struct.pack(f"<{len(values)}f", *values)).decode("ascii")


def trajectory_payload(
    entry_id: str,
    static_map: Any,
    display: Any,
    options: dict[str, Any],
) -> dict[str, Any]:
    """Build the ``narwal/trajectory`` answer (executor thread).

    Decoding and converting up to 50,000 points does not belong on the event
    loop. The coordinator replaces ``map_data`` and ``map_display_data`` rather
    than mutating them, so the references passed in stay consistent here.
    """
    # Imported here: camera.py imports this package's __init__, which imports
    # this module. By the time a command runs the camera platform is loaded.
    from .camera import NarwalMapCamera, map_rotation, map_zoom

    answer: dict[str, Any] = {
        "version": ANSWER_VERSION,
        "entry_id": entry_id,
        "map": None,
        "rotation": map_rotation(options),
        "zoom": map_zoom(options),
        "signature": None,
        "count": 0,
        "x": "",
        "y": "",
    }
    if static_map is None or static_map.width <= 0 or static_map.height <= 0:
        return answer
    answer["map"] = {
        "id": static_map.map_id,
        "created_at": static_map.created_at,
        "width": static_map.width,
        "height": static_map.height,
        "resolution_mm": static_map.resolution,
    }
    if display is None or not display.has_trajectory:
        return answer
    points = NarwalMapCamera._native_trajectory_grid_points(
        display.trajectory_render_points(), static_map
    )
    answer["signature"] = list(display.trajectory_signature)
    answer["count"] = len(points)
    answer["x"] = _pack([x for x, _ in points])
    answer["y"] = _pack([y for _, y in points])
    return answer


@websocket_api.websocket_command(
    {
        vol.Required("type"): TRAJECTORY_COMMAND,
        vol.Optional("entry_id"): str,
    }
)
@websocket_api.async_response
async def ws_trajectory(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Answer the trail of the current clean for one Narwal entry."""
    coordinator = _coordinator_for(hass, msg)
    if coordinator is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "No Narwal entry loaded")
        return
    state = coordinator.client.state
    payload = await hass.async_add_executor_job(
        trajectory_payload,
        coordinator.config_entry.entry_id,
        state.map_data,
        state.map_display_data,
        dict(coordinator.config_entry.options),
    )
    connection.send_result(msg["id"], payload)
