"""Websocket commands that hand map data to dashboard clients.

``narwal/trajectory`` returns the trail of the current clean as data: grid
points converted the way the map camera converts them, from the coordinator's
retained trail, every retained point included. The
camera's ``path`` attribute carries a copy decimated to 400 points, because
attributes go to every frontend on every frame; a client that draws the route
itself asks for the full trail, and while polling sends the signature it holds
so an unchanged trail is not sent again.

``narwal/active_map`` asks the robot which map is active now and answers its
id, size and rooms. The integration otherwise fetches the map only at startup
and when a clean is started from Home Assistant, so a floor switched in the
Narwal app stays invisible until then (#43). The command makes the same
``get_map`` call a clean start makes, under the same lock, and tells the
entities afterwards, so the camera and the room list follow the robot's floor.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import logging
import sys
import time
from array import array
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary

import voluptuous as vol
from homeassistant.auth.permissions.const import POLICY_CONTROL, POLICY_READ
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from websockets.exceptions import WebSocketException

from .const import DOMAIN
from .coordinator import is_clean_session_context
from .map_geometry import (
    map_is_drawable,
    map_placement,
    map_rotation,
    map_zoom,
    native_trajectory_grid_points,
)
from .narwal_client import NarwalCommandError, NarwalConnectionError

_LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from homeassistant.auth.models import User

    from .coordinator import NarwalCoordinator
    from .narwal_client.models import MapData, MapDisplayData

TRAJECTORY_COMMAND = f"{DOMAIN}/trajectory"
ACTIVE_MAP_COMMAND = f"{DOMAIN}/active_map"
#: Version of the answers' shape; bumped on any incompatible change.
ANSWER_VERSION = 1
#: A successful answer younger than this (monotonic seconds) is reused, so
#: dashboards polling the command do not turn into a stream of get_map calls.
ACTIVE_MAP_TTL = 30.0
#: After a failed fetch the robot is not asked again for this long, so a card
#: polling a robot that does not answer cannot keep the dock lock busy.
ACTIVE_MAP_FAILURE_BACKOFF = 30.0
ERR_MAP_FAILED = "map_failed"
#: Error code when several entries are configured and the request names none.
ERR_TARGET_REQUIRED = "target_required"
#: Seconds to wait for a sleeping robot to start broadcasting, as the vacuum does.
WAKE_TIMEOUT = 10.0

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


class ActiveMapError(Exception):
    """The active map could not be fetched."""


#: What a robot that does not answer raises: the client's own errors, a bare
#: timeout, and the socket's errors that ``send`` lets through. Anything else
#: is a bug; it propagates, so Home Assistant logs it with its traceback.
FETCH_ERRORS: tuple[type[BaseException], ...] = (
    ActiveMapError,
    NarwalCommandError,
    NarwalConnectionError,
    TimeoutError,
    OSError,
    WebSocketException,
)


@dataclass
class _ActiveMapFetch:
    """Per-entry fetch state: the running request and the last outcome."""

    task: asyncio.Task[dict[str, Any]] | None = None
    answer: dict[str, Any] | None = None
    answered_at: float = 0.0
    #: The coordinator's ``map_data`` object the answer was built from.
    map_data: Any = None
    #: The last failure as text, never the exception: its traceback holds the
    #: coordinator, which would keep this entry's key alive after an unload.
    failure: str | None = None
    failed_at: float = 0.0
    #: Whether the robot was asleep when the fetch failed.
    failed_asleep: bool = False


# Keyed by coordinator, so an unloaded entry takes its state with it.
_active_map_fetches: WeakKeyDictionary[NarwalCoordinator, _ActiveMapFetch] = (
    WeakKeyDictionary()
)


@callback
def async_setup_websocket_api(hass: HomeAssistant) -> None:
    """Register the Narwal websocket commands."""
    websocket_api.async_register_command(hass, ws_trajectory)
    websocket_api.async_register_command(hass, ws_active_map)


@callback
def coordinator_for(hass: HomeAssistant, msg: dict[str, Any]) -> NarwalCoordinator:
    """Return the coordinator the request names, or the only configured one.

    A card holds an entity id, so ``entity_id`` (any entity of the robot) is
    resolved through the entity registry like the services do. Without a
    target the command answers for the one entry that is configured and
    enabled, ``target_required`` for several and ``not_found`` for none:
    counting loaded entries instead would answer for the other robot while
    one is still retrying its setup after a restart.
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
def authorized_coordinator(
    hass: HomeAssistant, user: User, msg: dict[str, Any], policy: str
) -> NarwalCoordinator:
    """Return the requested robot's coordinator once the user may use it.

    Permission comes first, as in the services: a named entity is checked for
    read permission before it is resolved, and a restricted user who names no
    entity gets ``unauthorized`` instead of ``not_found``, so an answer never
    tells such a user which ids belong to a loaded robot. A request without a
    target still gets ``target_required`` when several robots are configured,
    which names no id. ``policy`` is then checked on the robot's vacuum, since
    the named entity may be its camera.
    """
    entity_id = msg.get("entity_id")
    if (
        not user.is_admin
        and entity_id is not None
        and not user.permissions.check_entity(entity_id, POLICY_READ)
    ):
        raise Unauthorized(entity_id=entity_id, permission=POLICY_READ)
    try:
        coordinator = coordinator_for(hass, msg)
    except TargetError as err:
        if user.is_admin or entity_id is not None or err.code != websocket_api.ERR_NOT_FOUND:
            raise
        raise Unauthorized(permission=policy) from None
    check_robot_permission(hass, user, coordinator, policy)
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


def _pack(values: array[float]) -> str:
    """Return float32 values as little-endian base64."""
    if sys.byteorder == "big":
        values.byteswap()
    return base64.b64encode(values.tobytes()).decode("ascii")


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
    camera caches on), then every map field the conversion reads.
    """
    if not map_is_drawable(static_map) or display is None or not display.has_trajectory:
        return None
    return [*display.trajectory_signature, *map_placement(static_map)]


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
    render_points = display.trajectory_render_points()
    # The coordinator caps the retained trail already; capping again here would
    # count the inserted break pairs and drop real points from a full trail.
    points = native_trajectory_grid_points(
        render_points, static_map, max_points=len(render_points)
    )
    answer["count"] = len(points)
    answer["x"] = _pack(array("f", (x for x, _ in points)))
    answer["y"] = _pack(array("f", (y for _, y in points)))
    return answer


@websocket_api.websocket_command(
    {
        vol.Required("type"): TRAJECTORY_COMMAND,
        **TARGET_SCHEMA,
        # The answer's signature is null without a trail; sent back, it matches nothing.
        vol.Optional("signature"): vol.Any(None, [int]),
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
        # The trail shows where the robot went: whoever may read the robot may read it.
        coordinator = authorized_coordinator(hass, connection.user, msg, POLICY_READ)
    except TargetError as err:
        connection.send_error(msg["id"], err.code, err.message)
        return
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


def active_map_payload(entry_id: str, map_data: Any, *, held: bool) -> dict[str, Any]:
    """Build the ``narwal/active_map`` answer.

    ``held`` marks a map the integration already held instead of one the robot
    was asked for (during a task); ``map`` is null when none is held yet.
    """
    return {
        "version": ANSWER_VERSION,
        "entry_id": entry_id,
        "held": held,
        "map": None
        if map_data is None
        else {
            "id": map_data.map_id,
            "created_at": map_data.created_at,
            "width": map_data.width,
            "height": map_data.height,
        },
        "rooms": []
        if map_data is None
        else [
            {"id": room.room_id, "name": room.display_name}
            for room in map_data.rooms
            if room.room_id > 0
        ],
    }


def _held_payload(coordinator: NarwalCoordinator) -> dict[str, Any]:
    """Answer the map the integration holds, without asking the robot."""
    return active_map_payload(
        coordinator.config_entry.entry_id, coordinator.client.state.map_data, held=True
    )


async def _async_fetch_active_map(
    coordinator: NarwalCoordinator, fetch: _ActiveMapFetch
) -> dict[str, Any]:
    """Ask the robot for its active map; only a fetched answer is remembered."""
    client = coordinator.client
    try:
        async with coordinator.dock_action_lock:
            if is_clean_session_context(client.state):
                # A clean started while this request waited for the lock.
                return _held_payload(coordinator)
            if not client.robot_awake and not client.state.is_docked:
                # Off the dock a robot asleep answers nothing; vacuum.start would
                # wake it too. A docked robot answers get_map without a burst, as
                # it answers the 60 s get_status poll, and bursting each fetch at
                # a robot that stops broadcasting while docked would bring back
                # the bursts #90 removed.
                await client.wake(timeout=WAKE_TIMEOUT)
            map_data = await client.get_map()
            await coordinator.async_align_trail_with_static_map()
    except FETCH_ERRORS as err:
        # Only a robot that did not answer starts the back-off, never a bug.
        fetch.failure = _describe(err)
        fetch.failed_at = time.monotonic()
        fetch.failed_asleep = not client.robot_awake
        raise
    answer = active_map_payload(coordinator.config_entry.entry_id, map_data, held=False)
    fetch.answer = answer
    fetch.answered_at = time.monotonic()
    fetch.map_data = client.state.map_data
    fetch.failure = None
    # Told only once the fetch is recorded, and guarded: HA does not guard
    # listener callbacks, and the map and trail have already been swapped, so
    # an entity that raises must not turn this fetch into a failure that is
    # neither cached nor backed off. Without async_set_updated_data, which
    # would move the 60 s poll on every fetch and starve it under a polling card.
    try:
        coordinator.async_update_listeners()
    except Exception:
        _LOGGER.exception("Error telling the Narwal entities about the fetched map")
    return answer


def _describe(err: BaseException) -> str:
    """Return an error text that is never empty (a bare TimeoutError has none)."""
    return str(err) or type(err).__name__


def _fetch_done(fetch: _ActiveMapFetch, task: asyncio.Task[Any]) -> None:
    """Drop the finished task.

    Dropping it lets an unloaded coordinator go: a kept task keeps its
    exception, whose traceback holds the coordinator. Retrieving the
    exception keeps asyncio from logging it when nobody awaits the fetch any
    more; whoever does await it gets it raised.
    """
    if fetch.task is task:
        fetch.task = None
    if not task.cancelled():
        task.exception()


async def async_get_active_map(
    hass: HomeAssistant, coordinator: NarwalCoordinator
) -> dict[str, Any]:
    """Return the robot's active map, sharing one request among concurrent callers."""
    state = coordinator.client.state
    if is_clean_session_context(state):
        # The map is fixed for the duration of a task, and a get_map during a
        # clean is unmeasured: answer the held map instead of asking the robot.
        return _held_payload(coordinator)
    # No await before the task is stored, so concurrent callers cannot race here.
    fetch = _active_map_fetches.setdefault(coordinator, _ActiveMapFetch())
    now = time.monotonic()
    if (
        fetch.answer is not None
        and now - fetch.answered_at < ACTIVE_MAP_TTL
        # A clean start or setup that fetched the map since makes this answer stale.
        and fetch.map_data is state.map_data
    ):
        return copy.deepcopy(fetch.answer)
    task = fetch.task
    if task is None:
        if (
            fetch.failure is not None
            and now - fetch.failed_at < ACTIVE_MAP_FAILURE_BACKOFF
            # A robot that failed asleep and has woken since is worth asking again.
            and not (fetch.failed_asleep and coordinator.client.robot_awake)
        ):
            raise ActiveMapError(fetch.failure)
        # Tied to the entry: an unload cancels it, so nothing more is sent to
        # the robot or told to the entities of an unloaded entry, and whoever
        # still waits gets map_failed.
        task = fetch.task = coordinator.config_entry.async_create_background_task(
            hass,
            _async_fetch_active_map(coordinator, fetch),
            f"{DOMAIN}_active_map",
        )
        task.add_done_callback(partial(_fetch_done, fetch))
    try:
        # A client that disconnects must not cancel the request others wait for.
        answer = await asyncio.shield(task)
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if task.cancelled() and (current is None or not current.cancelling()):
            raise ActiveMapError("Narwal entry was unloaded") from None
        raise
    return copy.deepcopy(answer)


@websocket_api.websocket_command(
    {
        vol.Required("type"): ACTIVE_MAP_COMMAND,
        **TARGET_SCHEMA,
    }
)
@websocket_api.async_response
async def ws_active_map(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Answer the robot's active map (id, size, rooms) for one Narwal entry."""
    try:
        # Asking wakes the robot and replaces the map the entities show: control.
        coordinator = authorized_coordinator(hass, connection.user, msg, POLICY_CONTROL)
    except TargetError as err:
        connection.send_error(msg["id"], err.code, err.message)
        return
    try:
        answer = await async_get_active_map(hass, coordinator)
    except FETCH_ERRORS as err:
        connection.send_error(
            msg["id"], ERR_MAP_FAILED, f"Narwal map could not be fetched: {_describe(err)}"
        )
        return
    connection.send_result(msg["id"], answer)
