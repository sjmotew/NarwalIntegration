"""Websocket commands that hand map data to dashboard clients.

``narwal/trajectory`` returns the trail of the current clean as data: the same
grid points the map camera draws, from the coordinator's retained trail. The
camera's ``path`` attribute carries a copy decimated to 400 points, because
attributes go to every frontend on every frame; a client that draws the route
itself asks for the full trail, and while polling sends the signature it holds
so an unchanged trail is not sent again.
"""

from __future__ import annotations

import base64
import struct
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.auth.permissions.const import POLICY_READ
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .map_geometry import (
    map_is_drawable,
    map_rotation,
    map_zoom,
    native_trajectory_grid_points,
)

if TYPE_CHECKING:
    from homeassistant.auth.models import User

    from .coordinator import NarwalCoordinator
    from .narwal_client.models import MapData, MapDisplayData

TRAJECTORY_COMMAND = f"{DOMAIN}/trajectory"
#: Version of the answers' shape; bumped on any incompatible change.
ANSWER_VERSION = 1
#: Error code when several entries are configured and the request names none.
ERR_TARGET_REQUIRED = "target_required"

#: Request keys that pick the robot; at most one of them per request.
TARGET_SCHEMA = {
    vol.Exclusive("entity_id", "target"): cv.entity_id,
    vol.Exclusive("entry_id", "target"): str,
}


class TargetError(Exception):
    """The request does not resolve to exactly one loaded Narwal entry."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@callback
def async_setup_websocket_api(hass: HomeAssistant) -> None:
    """Register the Narwal websocket commands."""
    websocket_api.async_register_command(hass, ws_trajectory)


@callback
def coordinator_for(hass: HomeAssistant, msg: dict[str, Any]) -> NarwalCoordinator:
    """Return the coordinator the request names, or the only configured one.

    A card holds an entity id, so ``entity_id`` (any entity of the robot) is
    resolved through the entity registry like the services do. Without a
    target the command answers only when exactly one entry is configured and
    enabled: counting loaded entries instead would answer for the other robot
    while one is still retrying its setup after a restart.
    """
    coordinators: dict[str, NarwalCoordinator] = hass.data.get(DOMAIN, {})
    if (entity_id := msg.get("entity_id")) is not None:
        registry_entry = er.async_get(hass).async_get(entity_id)
        coordinator = (
            coordinators.get(registry_entry.config_entry_id)
            if registry_entry is not None and registry_entry.config_entry_id
            else None
        )
        if coordinator is None:
            raise TargetError(
                websocket_api.ERR_NOT_FOUND, f"Narwal target is not loaded: {entity_id}"
            )
        return coordinator
    if (entry_id := msg.get("entry_id")) is None:
        entries = hass.config_entries.async_entries(
            DOMAIN, include_ignore=False, include_disabled=False
        )
        if len(entries) > 1:
            raise TargetError(
                ERR_TARGET_REQUIRED,
                "Several Narwal entries are configured; name one with entity_id",
            )
        if not entries:
            raise TargetError(websocket_api.ERR_NOT_FOUND, "No Narwal entry configured")
        entry_id = entries[0].entry_id
    coordinator = coordinators.get(entry_id)
    if coordinator is None:
        raise TargetError(
            websocket_api.ERR_NOT_FOUND, f"Narwal entry is not loaded: {entry_id}"
        )
    return coordinator


@callback
def check_robot_permission(
    hass: HomeAssistant, user: User, coordinator: NarwalCoordinator, policy: str
) -> None:
    """Raise Unauthorized unless the user holds ``policy`` on the robot's vacuum.

    The services check control permission on their vacuum targets; this does
    the same for the robot an entry belongs to, whichever way it was named.
    """
    if user.is_admin:
        return
    registry = er.async_get(hass)
    vacuums = [
        entry.entity_id
        for entry in er.async_entries_for_config_entry(
            registry, coordinator.config_entry.entry_id
        )
        if entry.domain == "vacuum"
    ]
    if not vacuums:
        raise Unauthorized(permission=policy)
    for entity_id in vacuums:
        if not user.permissions.check_entity(entity_id, policy):
            raise Unauthorized(entity_id=entity_id, permission=policy)


def _pack(values: list[float]) -> str:
    """Return little-endian float32 values as base64."""
    return base64.b64encode(struct.pack(f"<{len(values)}f", *values)).decode("ascii")


def _map_block(static_map: MapData | None) -> dict[str, Any] | None:
    """Describe the static map the trail belongs to."""
    if static_map is None:
        return None
    return {
        "id": static_map.map_id,
        "created_at": static_map.created_at,
        "width": static_map.width,
        "height": static_map.height,
        "resolution_mm": static_map.resolution,
    }


def trail_signature(
    static_map: MapData | None, display: MapDisplayData | None
) -> list[int] | None:
    """Return the signature of the answer's points, or None without a trail.

    The grid points depend on the trail and on the map they are placed on, so
    both are in it: the trail's own signature (count and checksums, as the
    camera caches on), then the map's ``id`` and ``created_at``.
    """
    if not map_is_drawable(static_map) or display is None or not display.has_trajectory:
        return None
    return [*display.trajectory_signature, static_map.map_id, static_map.created_at]


def _answer(
    entry_id: str,
    static_map: MapData | None,
    options: dict[str, Any],
    signature: list[int] | None,
    *,
    unchanged: bool,
) -> dict[str, Any]:
    """Return the fields every answer carries."""
    return {
        "version": ANSWER_VERSION,
        "entry_id": entry_id,
        "map": _map_block(static_map),
        "rotation": map_rotation(options),
        "zoom": map_zoom(options),
        "signature": signature,
        "unchanged": unchanged,
    }


def unchanged_payload(
    entry_id: str,
    static_map: MapData,
    signature: list[int],
    options: dict[str, Any],
) -> dict[str, Any]:
    """Build the answer for a client that already holds exactly these points."""
    return _answer(entry_id, static_map, options, signature, unchanged=True)


def trajectory_payload(
    entry_id: str,
    static_map: MapData | None,
    display: MapDisplayData | None,
    options: dict[str, Any],
) -> dict[str, Any]:
    """Build the ``narwal/trajectory`` answer (executor thread).

    Decoding and converting the retained trail does not belong on the event
    loop. The coordinator replaces ``map_data`` and ``map_display_data`` rather
    than mutating them, so the references passed in stay consistent here.
    """
    signature = trail_signature(static_map, display)
    answer = _answer(entry_id, static_map, options, signature, unchanged=False)
    answer.update(count=0, x="", y="")
    if signature is None or static_map is None or display is None:
        return answer
    points = native_trajectory_grid_points(display.trajectory_render_points(), static_map)
    answer["count"] = len(points)
    answer["x"] = _pack([x for x, _ in points])
    answer["y"] = _pack([y for _, y in points])
    return answer


@websocket_api.websocket_command(
    {
        vol.Required("type"): TRAJECTORY_COMMAND,
        **TARGET_SCHEMA,
        vol.Optional("signature"): [int],
    }
)
@websocket_api.async_response
async def ws_trajectory(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Answer the trail of the current clean for one Narwal entry."""
    try:
        coordinator = coordinator_for(hass, msg)
    except TargetError as err:
        connection.send_error(msg["id"], err.code, err.message)
        return
    # The trail shows where the robot went: whoever may read the robot may read it.
    check_robot_permission(hass, connection.user, coordinator, POLICY_READ)
    entry_id = coordinator.config_entry.entry_id
    options = dict(coordinator.config_entry.options)
    state = coordinator.client.state
    static_map = state.map_data
    signature = trail_signature(static_map, state.map_display_data)
    if signature is not None and static_map is not None and msg.get("signature") == signature:
        # The client holds these points already: skip the decode and the ~270 KB.
        connection.send_result(
            msg["id"], unchanged_payload(entry_id, static_map, signature, options)
        )
        return
    payload = await hass.async_add_executor_job(
        trajectory_payload, entry_id, static_map, state.map_display_data, options
    )
    connection.send_result(msg["id"], payload)
