"""Tests for the websocket command narwal/active_map (custom_components.narwal.websocket_api).

The command resolves the target, checks permission, asks the coordinator and
shapes the answer; what the coordinator does is in test_coordinator_active_map.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import tests.ha_stubs

tests.ha_stubs.install()

from homeassistant.exceptions import Unauthorized  # noqa: E402

from custom_components.narwal import coordinator as coordinator_module  # noqa: E402
from custom_components.narwal import websocket_api as ws  # noqa: E402
from custom_components.narwal.const import DOMAIN  # noqa: E402
from custom_components.narwal.coordinator import (  # noqa: E402
    ActiveMapError,
    ActiveMapRefresh,
)
from custom_components.narwal.narwal_client import NarwalConnectionError  # noqa: E402
from narwal_client.models import MapData, RoomInfo  # noqa: E402
from tests.test_coordinator_active_map import (  # noqa: E402
    _coordinator as _real_coordinator,
)
from tests.test_coordinator_active_map import (  # noqa: E402
    _counting_fetch,
    _settle,
)


def _map(map_id: int = 2) -> MapData:
    return MapData(
        map_id=map_id,
        created_at=1_700_000_000,
        width=100,
        height=80,
        rooms=[
            RoomInfo(room_id=1, name="Kitchen"),
            RoomInfo(room_id=0, name="unassigned"),
            RoomInfo(room_id=3, name="Study"),
        ],
    )


def _coordinator(entry_id: str = "e1", *, map_id: int = 2, held: bool = False) -> MagicMock:
    """Only what the command may touch: any other attribute raises AttributeError."""
    coordinator = MagicMock(spec=["config_entry", "async_refresh_active_map"])
    coordinator.config_entry = MagicMock(entry_id=entry_id)
    coordinator.async_refresh_active_map = AsyncMock(
        return_value=ActiveMapRefresh(_map(map_id), held=held)
    )
    return coordinator


def _hass_with(*coordinators: object, configured: list[str] | None = None) -> MagicMock:
    """Home Assistant with these coordinators loaded and ``configured`` entry ids set up."""
    hass = MagicMock()
    hass.data = {DOMAIN: {c.config_entry.entry_id: c for c in coordinators}}
    entry_ids = (
        configured if configured is not None else [c.config_entry.entry_id for c in coordinators]
    )
    hass.config_entries.async_entries.return_value = [
        MagicMock(entry_id=entry_id) for entry_id in entry_ids
    ]
    return hass


def _registry(**entries: str | None) -> MagicMock:
    """Entity registry mapping entity ids to config entry ids."""
    registry = MagicMock()

    def _get(entity_id: str):
        if entity_id not in entries:
            return None
        return MagicMock(config_entry_id=entries[entity_id])

    registry.async_get = _get
    return registry


def _user(
    *, admin: bool = False, allowed: tuple[str, ...] = (), read_only: tuple[str, ...] = ()
) -> MagicMock:
    """A user with read and control on ``allowed`` and read alone on ``read_only``."""
    user = MagicMock(is_admin=admin)
    user.permissions.check_entity = lambda entity_id, policy: (
        (policy in ("read", "control") and entity_id in allowed)
        or (policy == "read" and entity_id in read_only)
    )
    return user


def test_command_schema() -> None:
    """The command takes one optional target and nothing else."""
    assert ws.ACTIVE_MAP_COMMAND == "narwal/active_map"
    assert {str(key) for key in ws.ws_active_map._ws_schema} == {"type", "entity_id", "entry_id"}


def test_payload_lists_assigned_rooms_only() -> None:
    answer = ws.active_map_payload("e1", _map(), held=False)

    assert answer == {
        "version": ws.ANSWER_VERSION,
        "entry_id": "e1",
        "held": False,
        "map": {"id": 2, "created_at": 1_700_000_000, "width": 100, "height": 80},
        "rooms": [
            {"id": 1, "name": RoomInfo(room_id=1, name="Kitchen").display_name},
            {"id": 3, "name": RoomInfo(room_id=3, name="Study").display_name},
        ],
    }


def test_payload_without_a_held_map() -> None:
    answer = ws.active_map_payload("e1", None, held=True)

    assert (answer["held"], answer["map"], answer["rooms"]) == (True, None, [])


async def test_the_answer_is_what_the_coordinator_refreshed() -> None:
    """One call to the coordinator, and nothing else of it is touched."""
    coordinator = _coordinator()
    connection = MagicMock()

    await ws.ws_active_map(_hass_with(coordinator), connection, {"id": 1})

    coordinator.async_refresh_active_map.assert_awaited_once_with()
    assert connection.send_result.call_args.args == (
        1,
        ws.active_map_payload("e1", _map(), held=False),
    )
    connection.send_error.assert_not_called()


async def test_a_held_map_is_answered_as_held() -> None:
    coordinator = _coordinator(map_id=1, held=True)
    connection = MagicMock()

    await ws.ws_active_map(_hass_with(coordinator), connection, {"id": 1})

    answer = connection.send_result.call_args.args[1]
    assert (answer["held"], answer["map"]["id"]) == (True, 1)


async def test_no_held_map_during_a_task_answers_null() -> None:
    coordinator = _coordinator()
    coordinator.async_refresh_active_map.return_value = ActiveMapRefresh(None, held=True)
    connection = MagicMock()

    await ws.ws_active_map(_hass_with(coordinator), connection, {"id": 1})

    answer = connection.send_result.call_args.args[1]
    assert (answer["held"], answer["map"], answer["rooms"]) == (True, None, [])


async def test_a_robot_that_does_not_answer_is_map_failed() -> None:
    coordinator = _coordinator()
    coordinator.async_refresh_active_map.side_effect = ActiveMapError("TimeoutError")
    connection = MagicMock()

    await ws.ws_active_map(_hass_with(coordinator), connection, {"id": 4})

    assert connection.send_error.call_args.args == (
        4,
        ws.ERR_MAP_FAILED,
        "Narwal map could not be fetched: TimeoutError",
    )
    connection.send_result.assert_not_called()


async def test_a_bug_is_raised_not_reported() -> None:
    """A programming error reaches HA's handler, which logs its traceback."""
    coordinator = _coordinator()
    coordinator.async_refresh_active_map.side_effect = AttributeError("no attribute")
    connection = MagicMock()

    with pytest.raises(AttributeError):
        await ws.ws_active_map(_hass_with(coordinator), connection, {"id": 1})

    connection.send_error.assert_not_called()
    connection.send_result.assert_not_called()


# --- through the real coordinator: the documented answers -----------------------------


async def test_concurrent_requests_share_one_get_map_and_reuse_it() -> None:
    gate = asyncio.Event()
    fetch_map, calls = _counting_fetch(_map(), gate=gate)
    hass = _hass_with(_real_coordinator(fetch_map))
    connections = [MagicMock() for _ in range(3)]

    requests = [
        asyncio.ensure_future(ws.ws_active_map(hass, connection, {"id": index}))
        for index, connection in enumerate(connections)
    ]
    await _settle()
    gate.set()
    await asyncio.gather(*requests)
    later = MagicMock()
    await ws.ws_active_map(hass, later, {"id": 9})

    assert calls == [1]
    answers = [c.send_result.call_args.args[1] for c in (*connections, later)]
    assert answers[0] == ws.active_map_payload("e1", _map(), held=False)
    assert all(answer == answers[0] for answer in answers)
    # Each client gets its own answer object.
    assert answers[0] is not answers[1]


async def test_failures_are_map_failed_with_the_documented_texts() -> None:
    """A timeout, then the back-off, with the robot asked once."""
    fetch_map, calls = _counting_fetch(error=TimeoutError())
    hass = _hass_with(_real_coordinator(fetch_map))
    connection = MagicMock()

    await ws.ws_active_map(hass, connection, {"id": 4})
    await ws.ws_active_map(hass, connection, {"id": 5})

    assert [c.args for c in connection.send_error.call_args_list] == [
        (msg_id, ws.ERR_MAP_FAILED, "Narwal map could not be fetched: TimeoutError")
        for msg_id in (4, 5)
    ]
    assert calls == [1]
    connection.send_result.assert_not_called()


async def test_a_wake_that_fails_is_map_failed() -> None:
    fetch_map, calls = _counting_fetch()
    coordinator = _real_coordinator(fetch_map, awake=False, docked=False)
    coordinator.client.wake = AsyncMock(
        side_effect=NarwalConnectionError("Not connected to vacuum")
    )
    connection = MagicMock()

    await ws.ws_active_map(_hass_with(coordinator), connection, {"id": 2})

    assert connection.send_error.call_args.args == (
        2,
        ws.ERR_MAP_FAILED,
        "Narwal map could not be fetched: Not connected to vacuum",
    )
    assert calls == []


async def test_hitting_the_limit_is_map_failed_with_a_text_that_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coordinator_module, "ACTIVE_MAP_FETCH_TIMEOUT", 0.01)
    fetch_map, _ = _counting_fetch(gate=asyncio.Event())
    connection = MagicMock()

    await asyncio.wait_for(
        ws.ws_active_map(_hass_with(_real_coordinator(fetch_map)), connection, {"id": 3}),
        timeout=2,
    )

    assert connection.send_error.call_args.args == (
        3,
        ws.ERR_MAP_FAILED,
        "Narwal map could not be fetched: No answer from the robot within 0 s",
    )


async def test_a_model_that_does_not_broadcast_is_asked_by_the_command() -> None:
    """The poll leaves such a model alone; the command, as documented, does not."""
    fetch_map, calls = _counting_fetch(_map())
    coordinator = _real_coordinator(fetch_map)
    coordinator.client.supports_broadcasts = False
    connection = MagicMock()

    await ws.ws_active_map(_hass_with(coordinator), connection, {"id": 1})

    assert calls == [1]
    assert connection.send_result.call_args.args[1]["held"] is False


async def test_an_unload_while_a_request_waits_is_map_failed() -> None:
    gate = asyncio.Event()
    fetch_map, _ = _counting_fetch(gate=gate)
    coordinator = _real_coordinator(fetch_map)
    connection = MagicMock()

    pending = asyncio.ensure_future(
        ws.ws_active_map(_hass_with(coordinator), connection, {"id": 9})
    )
    await _settle()
    await coordinator._async_close_active_map_refresh()
    await asyncio.wait_for(pending, timeout=1)

    assert connection.send_error.call_args.args == (
        9,
        ws.ERR_MAP_FAILED,
        "Narwal map could not be fetched: Narwal entry was unloaded",
    )


# --- target and permission ------------------------------------------------------------


async def test_several_entries_need_a_target() -> None:
    """Without a target, two robots are an error, not "the first one"."""
    first, second = _coordinator("e1"), _coordinator("e2")
    hass = _hass_with(first, second)
    connection = MagicMock()

    await ws.ws_active_map(hass, connection, {"id": 3})

    assert connection.send_error.call_args.args[:2] == (3, ws.ERR_TARGET_REQUIRED)
    first.async_refresh_active_map.assert_not_awaited()
    second.async_refresh_active_map.assert_not_awaited()
    hass.config_entries.async_entries.assert_called_once_with(
        DOMAIN, include_ignore=False, include_disabled=False
    )


async def test_a_robot_still_setting_up_counts_as_configured() -> None:
    """Robot e2 retries its setup after a restart: no silent answer for e1."""
    coordinator = _coordinator("e1")
    hass = _hass_with(coordinator, configured=["e1", "e2"])
    connection = MagicMock()

    await ws.ws_active_map(hass, connection, {"id": 3})

    assert connection.send_error.call_args.args[:2] == (3, ws.ERR_TARGET_REQUIRED)
    coordinator.async_refresh_active_map.assert_not_awaited()


async def test_the_only_robot_not_loaded_yet() -> None:
    hass = _hass_with(configured=["e1"])
    connection = MagicMock()

    await ws.ws_active_map(hass, connection, {"id": 3})

    assert connection.send_error.call_args.args[:2] == (3, "not_found")


async def test_an_entity_id_picks_the_robot() -> None:
    first, second = _coordinator("e1", map_id=1), _coordinator("e2", map_id=2)
    hass = _hass_with(first, second)
    connection = MagicMock()
    registry = _registry(**{"vacuum.upstairs": "e2", "light.other": "foreign"})

    with patch("custom_components.narwal.websocket_api.er.async_get", return_value=registry):
        await ws.ws_active_map(hass, connection, {"id": 4, "entity_id": "vacuum.upstairs"})
        await ws.ws_active_map(hass, connection, {"id": 5, "entity_id": "light.other"})
        await ws.ws_active_map(hass, connection, {"id": 6, "entity_id": "vacuum.gone"})

    answer = connection.send_result.call_args.args[1]
    assert (answer["entry_id"], answer["map"]["id"]) == ("e2", 2)
    assert [c.args[:2] for c in connection.send_error.call_args_list] == [
        (5, "not_found"),
        (6, "not_found"),
    ]
    first.async_refresh_active_map.assert_not_awaited()
    second.async_refresh_active_map.assert_awaited_once_with()


async def test_an_entry_id_picks_the_robot() -> None:
    first, second = _coordinator("e1", map_id=1), _coordinator("e2", map_id=2)
    hass = _hass_with(first, second)
    connection = MagicMock()

    await ws.ws_active_map(hass, connection, {"id": 1, "entry_id": "e2"})
    await ws.ws_active_map(hass, connection, {"id": 2, "entry_id": "e1"})

    answers = [c.args[1] for c in connection.send_result.call_args_list]
    assert [(a["entry_id"], a["map"]["id"]) for a in answers] == [("e2", 2), ("e1", 1)]


async def test_unknown_entry() -> None:
    coordinator = _coordinator()
    connection = MagicMock()

    await ws.ws_active_map(_hass_with(coordinator), connection, {"id": 7, "entry_id": "nope"})

    assert connection.send_error.call_args.args[:2] == (7, "not_found")
    coordinator.async_refresh_active_map.assert_not_awaited()


async def test_without_entries() -> None:
    connection = MagicMock()

    await ws.ws_active_map(_hass_with(), connection, {"id": 8})

    assert connection.send_error.call_args.args[:2] == (8, "not_found")


def _vacuum_registry_entries(*entity_ids: str) -> list[MagicMock]:
    return [
        MagicMock(entity_id=entity_id, domain=entity_id.split(".")[0]) for entity_id in entity_ids
    ]


async def test_a_user_needs_control_of_the_robot() -> None:
    """Asking wakes the robot and replaces the map: the services' control check applies."""
    coordinator = _coordinator()
    hass = _hass_with(coordinator)
    entries = _vacuum_registry_entries("vacuum.upstairs", "camera.upstairs_map")

    with patch(
        "custom_components.narwal.websocket_api.er.async_entries_for_config_entry",
        return_value=entries,
    ):
        for user in (
            _user(),
            _user(allowed=("camera.upstairs_map",)),
            # Reading the vacuum is not enough.
            _user(read_only=("vacuum.upstairs", "camera.upstairs_map")),
        ):
            connection = MagicMock(user=user)
            with pytest.raises(Unauthorized):
                await ws.ws_active_map(hass, connection, {"id": 1})
        coordinator.async_refresh_active_map.assert_not_awaited()

        connection = MagicMock(user=_user(allowed=("vacuum.upstairs",)))
        await ws.ws_active_map(hass, connection, {"id": 2})

    assert connection.send_result.call_args.args[1]["map"]["id"] == 2
    coordinator.async_refresh_active_map.assert_awaited_once_with()


async def test_permission_is_checked_before_the_target() -> None:
    """A restricted user cannot tell an unknown id from a robot it may not control."""
    coordinator = _coordinator()
    hass = _hass_with(coordinator)
    registry = _registry(**{"camera.upstairs_map": "e1"})
    restricted = _user(allowed=("camera.upstairs_map",))

    with patch("custom_components.narwal.websocket_api.er.async_get", return_value=registry):
        for msg in ({"id": 1, "entity_id": "camera.gone"}, {"id": 2, "entry_id": "nope"}):
            with pytest.raises(Unauthorized):
                await ws.ws_active_map(hass, MagicMock(user=restricted), msg)
            connection = MagicMock(user=_user(admin=True))
            await ws.ws_active_map(hass, connection, msg)
            assert connection.send_error.call_args.args[1] == "not_found"
    coordinator.async_refresh_active_map.assert_not_awaited()


async def test_a_robot_without_a_vacuum_entity_is_admin_only() -> None:
    coordinator = _coordinator()
    hass = _hass_with(coordinator)

    with patch(
        "custom_components.narwal.websocket_api.er.async_entries_for_config_entry",
        return_value=[],
    ):
        with pytest.raises(Unauthorized):
            await ws.ws_active_map(hass, MagicMock(user=_user()), {"id": 1})
        connection = MagicMock(user=_user(admin=True))
        await ws.ws_active_map(hass, connection, {"id": 2})

    assert connection.send_result.call_args.args[1]["map"]["id"] == 2
    coordinator.async_refresh_active_map.assert_awaited_once_with()
