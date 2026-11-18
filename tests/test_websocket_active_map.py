"""Tests for the websocket command narwal/active_map (custom_components.narwal.websocket_api)."""

from __future__ import annotations

import asyncio
import gc
import weakref
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from websockets.exceptions import ConnectionClosedError

import tests.ha_stubs

tests.ha_stubs.install()

from homeassistant.exceptions import Unauthorized  # noqa: E402

from custom_components.narwal import websocket_api as ws  # noqa: E402
from custom_components.narwal.const import DOMAIN  # noqa: E402
from custom_components.narwal.narwal_client import (  # noqa: E402
    NarwalCommandError,
    NarwalConnectionError,
)
from narwal_client.const import WorkingStatus  # noqa: E402
from narwal_client.models import MapData, NarwalState, RoomInfo  # noqa: E402


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


def _background_task(_hass, coro, name):
    return asyncio.get_running_loop().create_task(coro, name=name)


class _Coordinator:
    """The parts of NarwalCoordinator the command touches (weak-referenceable)."""

    def __init__(self, entry_id: str, get_map, *, awake: bool = True) -> None:
        self.config_entry = MagicMock()
        self.config_entry.entry_id = entry_id
        self.config_entry.async_create_background_task = _background_task
        self.client = MagicMock()
        self.client.state = NarwalState()
        self.client.robot_awake = awake
        self.client.wake = AsyncMock()
        self.dock_action_lock = asyncio.Lock()
        self.async_set_updated_data = MagicMock()
        self.async_update_listeners = MagicMock()
        self.async_align_trail_with_static_map = AsyncMock()
        self.set_get_map(get_map)

    def set_get_map(self, get_map) -> None:
        """Store the fetched map in the client state, as NarwalClient.get_map does."""
        if get_map is None:
            self.client.get_map = None
            return

        async def _get_map():
            map_data = await get_map()
            self.client.state.map_data = map_data
            return map_data

        self.client.get_map = _get_map


def _hass_with(*coordinators: _Coordinator, configured: list[str] | None = None) -> MagicMock:
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


def _counting_get_map(result=None, *, error: Exception | None = None, gate=None):
    calls: list[int] = []

    async def get_map():
        calls.append(1)
        if gate is not None:
            await gate.wait()
        if error is not None:
            # A fresh exception per call, as a real failure: a shared one's traceback
            # would tie this test's frames to every coordinator that raised it.
            raise type(error)(*error.args)
        return result or _map()

    return get_map, calls


def _registry(**entries: str | None) -> MagicMock:
    """Entity registry mapping entity ids to config entry ids."""
    registry = MagicMock()

    def _get(entity_id: str):
        if entity_id not in entries:
            return None
        return MagicMock(config_entry_id=entries[entity_id])

    registry.async_get = _get
    return registry


def _user(*, admin: bool = False, allowed: tuple[str, ...] = ()) -> MagicMock:
    """A user holding read and control permission on the ``allowed`` entities only."""
    user = MagicMock(is_admin=admin)
    user.permissions.check_entity = lambda entity_id, policy: (
        policy in ("read", "control") and entity_id in allowed
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


async def test_answer_comes_from_get_map_under_the_dock_lock() -> None:
    held: list[bool] = []
    coordinator = _Coordinator("e1", None)

    async def get_map():
        held.append(coordinator.dock_action_lock.locked())
        return _map()

    coordinator.set_get_map(get_map)
    connection = MagicMock()

    await ws.ws_active_map(_hass_with(coordinator), connection, {"id": 1})

    assert held == [True]
    msg_id, answer = connection.send_result.call_args.args
    assert msg_id == 1
    assert answer["map"]["id"] == 2
    assert answer["entry_id"] == "e1"
    assert answer["held"] is False


async def test_trail_is_aligned_and_entities_told_inside_the_lock() -> None:
    """After get_map: the old floor's trail goes, then camera, selects and segments follow."""
    order: list[str] = []
    get_map, _ = _counting_get_map()
    coordinator = _Coordinator("e1", get_map)
    coordinator.async_align_trail_with_static_map.side_effect = lambda: order.append(
        f"align {coordinator.dock_action_lock.locked()} {coordinator.client.state.map_data.map_id}"
    )
    coordinator.async_update_listeners.side_effect = lambda: order.append(
        f"notify {coordinator.dock_action_lock.locked()}"
    )

    await ws.async_get_active_map(_hass_with(coordinator), coordinator)

    assert order == ["align True 2", "notify True"]
    coordinator.async_update_listeners.assert_called_once_with()
    # async_set_updated_data would move the coordinator's 60 s poll on every fetch.
    coordinator.async_set_updated_data.assert_not_called()


async def test_a_sleeping_robot_is_woken_first() -> None:
    order: list[str] = []
    coordinator = _Coordinator("e1", None, awake=False)

    async def wake(timeout: float) -> None:
        order.append(f"wake {coordinator.dock_action_lock.locked()}")

    async def get_map():
        order.append("get_map")
        return _map()

    coordinator.client.wake = AsyncMock(side_effect=wake)
    coordinator.set_get_map(get_map)

    await ws.async_get_active_map(_hass_with(coordinator), coordinator)

    assert order == ["wake True", "get_map"]
    coordinator.client.wake.assert_awaited_once_with(timeout=ws.WAKE_TIMEOUT)


async def test_a_docked_robot_is_not_woken() -> None:
    """Docked and quiet is normal: get_map goes out without a wake burst (#90)."""
    get_map, calls = _counting_get_map()
    coordinator = _Coordinator("e1", get_map, awake=False)
    coordinator.client.state.working_status = WorkingStatus.CHARGED
    assert coordinator.client.state.is_docked

    await ws.async_get_active_map(_hass_with(coordinator), coordinator)

    coordinator.client.wake.assert_not_awaited()
    assert calls == [1]


async def test_an_awake_robot_is_not_woken() -> None:
    get_map, _ = _counting_get_map()
    coordinator = _Coordinator("e1", get_map, awake=True)

    await ws.async_get_active_map(_hass_with(coordinator), coordinator)

    coordinator.client.wake.assert_not_awaited()


async def test_a_wake_that_fails_is_reported() -> None:
    """Not connected: wake raises, the answer is map_failed and get_map is not sent."""
    get_map, calls = _counting_get_map()
    coordinator = _Coordinator("e1", get_map, awake=False)
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


@pytest.mark.parametrize(
    "status", [WorkingStatus.CLEANING, WorkingStatus.CLEANING_V2, WorkingStatus.REMAPPING]
)
async def test_during_a_clean_the_held_map_answers(status: WorkingStatus) -> None:
    """The robot is not asked mid-task; the answer says the map is the held one."""
    get_map, calls = _counting_get_map()
    coordinator = _Coordinator("e1", get_map, awake=False)
    coordinator.client.state.map_data = _map(1)
    coordinator.client.state.working_status = status

    async with coordinator.dock_action_lock:
        # Answered at once: a Stop holding the lock is not delayed by it, nor it by a Stop.
        answer = await asyncio.wait_for(
            ws.async_get_active_map(_hass_with(coordinator), coordinator), timeout=1
        )

    assert calls == []
    coordinator.client.wake.assert_not_awaited()
    assert answer["held"] is True
    assert answer["map"]["id"] == 1


async def test_a_clean_started_while_waiting_answers_the_held_map() -> None:
    """vacuum.start held the lock and the clean began: no wake, no get_map mid-clean."""
    get_map, calls = _counting_get_map()
    coordinator = _Coordinator("e1", get_map, awake=False)
    coordinator.client.state.map_data = _map(1)
    hass = _hass_with(coordinator)

    async with coordinator.dock_action_lock:
        pending = asyncio.ensure_future(ws.async_get_active_map(hass, coordinator))
        for _ in range(5):
            await asyncio.sleep(0)
        coordinator.client.state.working_status = WorkingStatus.CLEANING
    answer = await asyncio.wait_for(pending, timeout=1)

    assert (answer["held"], answer["map"]["id"]) == (True, 1)
    assert calls == []
    coordinator.client.wake.assert_not_awaited()
    coordinator.async_update_listeners.assert_not_called()

    # The held answer is not reused once the robot is docked again.
    coordinator.client.state.working_status = WorkingStatus.DOCKED
    assert (await ws.async_get_active_map(hass, coordinator))["held"] is False
    assert calls == [1]


async def test_waits_for_a_dock_action_that_starts_no_clean() -> None:
    """A dock action holding the lock finishes before get_map is sent."""
    get_map, calls = _counting_get_map()
    coordinator = _Coordinator("e1", get_map)
    hass = _hass_with(coordinator)

    async with coordinator.dock_action_lock:
        pending = asyncio.ensure_future(ws.async_get_active_map(hass, coordinator))
        for _ in range(5):
            await asyncio.sleep(0)
        assert calls == []
    answer = await pending
    assert (answer["held"], answer["map"]["id"]) == (False, 2)
    assert calls == [1]


async def test_recent_answer_is_reused_as_a_copy() -> None:
    get_map, calls = _counting_get_map()
    coordinator = _Coordinator("e1", get_map)
    hass = _hass_with(coordinator)

    with patch.object(ws.time, "monotonic", return_value=100.0):
        first = await ws.async_get_active_map(hass, coordinator)
    first["rooms"].clear()
    with patch.object(ws.time, "monotonic", return_value=100.0 + ws.ACTIVE_MAP_TTL - 1):
        second = await ws.async_get_active_map(hass, coordinator)
    assert len(second["rooms"]) == 2
    second["rooms"].clear()
    with patch.object(ws.time, "monotonic", return_value=100.0 + ws.ACTIVE_MAP_TTL - 1):
        third = await ws.async_get_active_map(hass, coordinator)
    assert len(third["rooms"]) == 2
    assert calls == [1]

    with patch.object(ws.time, "monotonic", return_value=100.0 + ws.ACTIVE_MAP_TTL + 1):
        await ws.async_get_active_map(hass, coordinator)
    assert calls == [1, 1]


async def test_a_map_fetched_elsewhere_ends_the_reuse() -> None:
    """After a clean start refreshed the map, the cached answer is not handed out."""
    get_map, calls = _counting_get_map()
    coordinator = _Coordinator("e1", get_map)
    hass = _hass_with(coordinator)

    with patch.object(ws.time, "monotonic", return_value=100.0):
        await ws.async_get_active_map(hass, coordinator)
        coordinator.client.state.map_data = _map(5)
        await ws.async_get_active_map(hass, coordinator)

    assert calls == [1, 1]


async def test_concurrent_requests_share_one_get_map() -> None:
    gate = asyncio.Event()
    get_map, calls = _counting_get_map(gate=gate)
    coordinator = _Coordinator("e1", get_map)
    hass = _hass_with(coordinator)

    waiters = [asyncio.ensure_future(ws.async_get_active_map(hass, coordinator)) for _ in range(3)]
    await asyncio.sleep(0)
    gate.set()
    answers = await asyncio.gather(*waiters)

    assert calls == [1]
    assert answers[0] == answers[1] == answers[2]
    assert answers[0] is not answers[1]


async def test_a_disconnecting_client_does_not_cancel_the_fetch() -> None:
    gate = asyncio.Event()
    get_map, calls = _counting_get_map(gate=gate)
    coordinator = _Coordinator("e1", get_map)
    hass = _hass_with(coordinator)

    leaving = asyncio.ensure_future(ws.async_get_active_map(hass, coordinator))
    staying = asyncio.ensure_future(ws.async_get_active_map(hass, coordinator))
    await asyncio.sleep(0)
    leaving.cancel()
    gate.set()

    assert (await staying)["map"]["id"] == 2
    with pytest.raises(asyncio.CancelledError):
        await leaving
    assert calls == [1]


async def test_an_unload_mid_fetch_is_reported() -> None:
    """The entry's background task is cancelled on unload; waiting clients get map_failed."""
    gate = asyncio.Event()
    get_map, _ = _counting_get_map(gate=gate)
    coordinator = _Coordinator("e1", get_map)
    connection = MagicMock()

    pending = asyncio.ensure_future(
        ws.ws_active_map(_hass_with(coordinator), connection, {"id": 9})
    )
    await asyncio.sleep(0)
    ws._active_map_fetches[coordinator].task.cancel()
    await asyncio.wait_for(pending, timeout=1)

    assert connection.send_error.call_args.args == (
        9,
        ws.ERR_MAP_FAILED,
        "Narwal map could not be fetched: Narwal entry was unloaded",
    )


async def test_a_failure_nobody_awaits_is_not_logged_as_unretrieved() -> None:
    """The only client left; the fetch then fails. asyncio must not complain at GC."""
    loop = asyncio.get_running_loop()
    reported: list[dict] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    try:
        gate = asyncio.Event()
        get_map, _ = _counting_get_map(error=TimeoutError("robot asleep"), gate=gate)
        coordinator = _Coordinator("e1", get_map)

        leaving = asyncio.ensure_future(
            ws.async_get_active_map(_hass_with(coordinator), coordinator)
        )
        await asyncio.sleep(0)
        leaving.cancel()
        with pytest.raises(asyncio.CancelledError):
            await leaving
        gate.set()
        for _ in range(5):
            await asyncio.sleep(0)
        assert ws._active_map_fetches[coordinator].task is None
        del leaving
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert reported == []


async def test_a_failed_fetch_does_not_keep_an_unloaded_coordinator() -> None:
    """The failure's traceback holds the coordinator; the cache must not keep it."""
    get_map, _ = _counting_get_map(error=TimeoutError("robot asleep"))
    coordinator = _Coordinator("e1", get_map)
    connection = MagicMock()

    await ws.ws_active_map(_hass_with(coordinator), connection, {"id": 1})
    for _ in range(3):
        await asyncio.sleep(0)
    assert connection.send_error.call_args.args[1] == ws.ERR_MAP_FAILED
    gone = weakref.ref(coordinator)
    del coordinator, connection
    gc.collect()

    assert gone() is None
    assert len(ws._active_map_fetches) == 0


async def test_failure_is_reported_and_retried_after_a_pause() -> None:
    """A robot that does not answer is not asked again for a while; the lock stays free."""
    get_map, calls = _counting_get_map(error=TimeoutError())
    coordinator = _Coordinator("e1", get_map)
    hass = _hass_with(coordinator)
    connection = MagicMock()

    with patch.object(ws.time, "monotonic", return_value=100.0):
        await ws.ws_active_map(hass, connection, {"id": 4})
        for _ in range(3):
            await asyncio.sleep(0)
    with patch.object(ws.time, "monotonic", return_value=100.0 + ws.ACTIVE_MAP_FAILURE_BACKOFF - 1):
        await ws.ws_active_map(hass, connection, {"id": 5})
    assert calls == [1]
    with patch.object(ws.time, "monotonic", return_value=100.0 + ws.ACTIVE_MAP_FAILURE_BACKOFF + 1):
        await ws.ws_active_map(hass, connection, {"id": 6})

    connection.send_result.assert_not_called()
    assert [c.args for c in connection.send_error.call_args_list] == [
        (msg_id, ws.ERR_MAP_FAILED, "Narwal map could not be fetched: TimeoutError")
        for msg_id in (4, 5, 6)
    ]
    assert calls == [1, 1]
    coordinator.async_update_listeners.assert_not_called()


@pytest.mark.parametrize(
    "error",
    [
        NarwalCommandError("get_map failed with code 3"),
        NarwalConnectionError("Not connected to vacuum"),
        TimeoutError(),
        OSError("network unreachable"),
        ConnectionClosedError(None, None),
    ],
    ids=["command", "connection", "timeout", "os", "socket closed"],
)
async def test_a_robot_that_does_not_answer_is_map_failed(error: Exception) -> None:
    get_map, _ = _counting_get_map(error=error)
    coordinator = _Coordinator("e1", get_map)
    connection = MagicMock()

    await ws.ws_active_map(_hass_with(coordinator), connection, {"id": 1})

    assert connection.send_error.call_args.args[1] == ws.ERR_MAP_FAILED
    assert ws._active_map_fetches[coordinator].failure is not None


@pytest.mark.parametrize("where", ["get_map", "align"])
async def test_a_bug_is_raised_not_reported_and_starts_no_back_off(where: str) -> None:
    """A programming error reaches HA's handler, which logs its traceback."""
    get_map, calls = _counting_get_map(
        error=AttributeError("no attribute") if where == "get_map" else None
    )
    coordinator = _Coordinator("e1", get_map)
    if where == "align":
        coordinator.async_align_trail_with_static_map.side_effect = AttributeError("align")
    hass = _hass_with(coordinator)
    connection = MagicMock()

    for msg_id in (1, 2):
        with pytest.raises(AttributeError):
            await ws.ws_active_map(hass, connection, {"id": msg_id})
        for _ in range(3):
            await asyncio.sleep(0)

    connection.send_error.assert_not_called()
    assert calls == [1, 1]
    assert ws._active_map_fetches[coordinator].failure is None


async def test_a_robot_that_woke_after_failing_asleep_is_asked_again() -> None:
    """The wake timed out, then a late broadcast shows the robot awake: no back-off."""
    get_map, calls = _counting_get_map()
    coordinator = _Coordinator("e1", get_map, awake=False)
    coordinator.client.wake = AsyncMock(side_effect=TimeoutError())
    hass = _hass_with(coordinator)
    connection = MagicMock()

    await ws.ws_active_map(hass, connection, {"id": 1})
    for _ in range(3):
        await asyncio.sleep(0)
    await ws.ws_active_map(hass, connection, {"id": 2})
    assert calls == []
    assert [c.args[0] for c in connection.send_error.call_args_list] == [1, 2]

    coordinator.client.robot_awake = True
    await ws.ws_active_map(hass, connection, {"id": 3})

    assert calls == [1]
    assert connection.send_result.call_args.args[0] == 3


async def test_entries_are_cached_separately() -> None:
    get_first, first_calls = _counting_get_map(_map(1))
    get_second, second_calls = _counting_get_map(_map(2))
    first = _Coordinator("e1", get_first)
    second = _Coordinator("e2", get_second)
    hass = _hass_with(first, second)
    connection = MagicMock()

    await ws.ws_active_map(hass, connection, {"id": 1, "entry_id": "e2"})
    await ws.ws_active_map(hass, connection, {"id": 2, "entry_id": "e1"})

    answers = [c.args[1] for c in connection.send_result.call_args_list]
    assert [(a["entry_id"], a["map"]["id"]) for a in answers] == [("e2", 2), ("e1", 1)]
    assert (first_calls, second_calls) == ([1], [1])


async def test_several_entries_need_a_target() -> None:
    """Without a target, two robots are an error, not "the first one"."""
    get_map, calls = _counting_get_map()
    hass = _hass_with(_Coordinator("e1", get_map), _Coordinator("e2", get_map))
    connection = MagicMock()

    await ws.ws_active_map(hass, connection, {"id": 3})

    assert connection.send_error.call_args.args[:2] == (3, ws.ERR_TARGET_REQUIRED)
    assert calls == []
    hass.config_entries.async_entries.assert_called_once_with(
        DOMAIN, include_ignore=False, include_disabled=False
    )


async def test_a_robot_still_setting_up_counts_as_configured() -> None:
    """Robot e2 retries its setup after a restart: no silent answer for e1."""
    get_map, calls = _counting_get_map()
    hass = _hass_with(_Coordinator("e1", get_map), configured=["e1", "e2"])
    connection = MagicMock()

    await ws.ws_active_map(hass, connection, {"id": 3})

    assert connection.send_error.call_args.args[:2] == (3, ws.ERR_TARGET_REQUIRED)
    assert calls == []


async def test_the_only_robot_not_loaded_yet() -> None:
    hass = _hass_with(configured=["e1"])
    connection = MagicMock()

    await ws.ws_active_map(hass, connection, {"id": 3})

    assert connection.send_error.call_args.args[:2] == (3, "not_found")


async def test_an_entity_id_picks_the_robot() -> None:
    get_first, first_calls = _counting_get_map(_map(1))
    get_second, second_calls = _counting_get_map(_map(2))
    hass = _hass_with(_Coordinator("e1", get_first), _Coordinator("e2", get_second))
    connection = MagicMock()
    registry = _registry(**{"vacuum.upstairs": "e2", "light.other": "foreign"})

    with patch("custom_components.narwal.websocket_api.er.async_get", return_value=registry):
        await ws.ws_active_map(hass, connection, {"id": 4, "entity_id": "vacuum.upstairs"})
        await ws.ws_active_map(hass, connection, {"id": 5, "entity_id": "light.other"})
        await ws.ws_active_map(hass, connection, {"id": 6, "entity_id": "vacuum.gone"})

    assert connection.send_result.call_args.args[1]["map"]["id"] == 2
    assert [c.args[:2] for c in connection.send_error.call_args_list] == [
        (5, "not_found"),
        (6, "not_found"),
    ]
    assert (first_calls, second_calls) == ([], [1])


async def test_unknown_entry() -> None:
    get_map, calls = _counting_get_map()
    connection = MagicMock()

    await ws.ws_active_map(
        _hass_with(_Coordinator("e1", get_map)), connection, {"id": 7, "entry_id": "nope"}
    )

    assert connection.send_error.call_args.args[:2] == (7, "not_found")
    assert calls == []


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
    get_map, calls = _counting_get_map()
    hass = _hass_with(_Coordinator("e1", get_map))
    entries = _vacuum_registry_entries("vacuum.upstairs", "camera.upstairs_map")

    with patch(
        "custom_components.narwal.websocket_api.er.async_entries_for_config_entry",
        return_value=entries,
    ):
        for user in (_user(), _user(allowed=("camera.upstairs_map",))):
            connection = MagicMock(user=user)
            with pytest.raises(Unauthorized):
                await ws.ws_active_map(hass, connection, {"id": 1})
        assert calls == []

        connection = MagicMock(user=_user(allowed=("vacuum.upstairs",)))
        await ws.ws_active_map(hass, connection, {"id": 2})

    assert connection.send_result.call_args.args[1]["map"]["id"] == 2
    assert calls == [1]


async def test_permission_is_checked_before_the_target() -> None:
    """A restricted user cannot tell an unknown id from a robot it may not control."""
    get_map, calls = _counting_get_map()
    hass = _hass_with(_Coordinator("e1", get_map))
    registry = _registry(**{"camera.upstairs_map": "e1"})
    restricted = _user(allowed=("camera.upstairs_map",))

    with patch("custom_components.narwal.websocket_api.er.async_get", return_value=registry):
        for msg in ({"id": 1, "entity_id": "camera.gone"}, {"id": 2, "entry_id": "nope"}):
            with pytest.raises(Unauthorized):
                await ws.ws_active_map(hass, MagicMock(user=restricted), msg)
            connection = MagicMock(user=_user(admin=True))
            await ws.ws_active_map(hass, connection, msg)
            assert connection.send_error.call_args.args[1] == "not_found"
    assert calls == []


async def test_a_robot_without_a_vacuum_entity_is_admin_only() -> None:
    get_map, calls = _counting_get_map()
    hass = _hass_with(_Coordinator("e1", get_map))

    with patch(
        "custom_components.narwal.websocket_api.er.async_entries_for_config_entry",
        return_value=[],
    ):
        with pytest.raises(Unauthorized):
            await ws.ws_active_map(hass, MagicMock(user=_user()), {"id": 1})
        connection = MagicMock(user=_user(admin=True))
        await ws.ws_active_map(hass, connection, {"id": 2})

    assert connection.send_result.call_args.args[1]["map"]["id"] == 2
    assert calls == [1]
