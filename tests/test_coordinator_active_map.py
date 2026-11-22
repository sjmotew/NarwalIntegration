"""Tests for NarwalCoordinator.async_refresh_active_map and the poll that calls it (#43)."""

from __future__ import annotations

import asyncio
import gc
import logging
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from websockets.exceptions import ConnectionClosedError, ProtocolError

import tests.ha_stubs

tests.ha_stubs.install()

from homeassistant.exceptions import HomeAssistantError  # noqa: E402

from custom_components.narwal import coordinator as coordinator_module  # noqa: E402
from custom_components.narwal.coordinator import (  # noqa: E402
    _TRAIL_STATE_ATTRS,
    ACTIVE_MAP_FAILURE_BACKOFF,
    ACTIVE_MAP_POLL_INTERVAL,
    ACTIVE_MAP_TTL,
    ACTIVE_MAP_WAKE_TIMEOUT,
    ActiveMapError,
    NarwalCoordinator,
)
from custom_components.narwal.narwal_client import (  # noqa: E402
    CommandResponse,
    NarwalCommandError,
    NarwalConnectionError,
)
from narwal_client.const import WorkingStatus  # noqa: E402
from narwal_client.models import MapData, MapDisplayData, NarwalState, RoomInfo  # noqa: E402
from tests.test_services import (  # noqa: E402
    _coordinator as _service_coordinator,
)
from tests.test_services import (  # noqa: E402
    _pending_profiles_call,
    _register_pending_profiles_handler,
)

HELD = (1, 100)
OTHER = (2, 200)


def _map(identity: tuple[int, int] = OTHER) -> MapData:
    """A static map with two assigned rooms and one unassigned."""
    return MapData(
        map_id=identity[0],
        created_at=identity[1],
        width=100,
        height=80,
        compressed_map=b"\x01",
        rooms=[
            RoomInfo(room_id=1, name="Kitchen"),
            RoomInfo(room_id=0, name="unassigned"),
            RoomInfo(room_id=3, name="Study"),
        ],
    )


def _trail(marker: bytes = b"aaaa") -> MapDisplayData:
    """A retained trail of one point; ``marker`` tells two trails apart."""
    return MapDisplayData(
        robot_x=1.25,
        robot_y=2.5,
        robot_heading=90.0,
        timestamp=123456,
        dock_ref_x=3.0,
        dock_ref_y=4.0,
        trajectory_x_values=marker,
        trajectory_y_values=marker,
        trajectory_signature=(4, 4, marker[0]),
    )


class _Store:
    """Trail store double that records what is read and written."""

    def __init__(self, data: object | None = None) -> None:
        self.data = data
        self.loads = 0
        self.saved: list[object] = []

    async def async_load(self) -> object | None:
        self.loads += 1
        return self.data

    async def async_save(self, data: object) -> None:
        self.saved.append(data)
        self.data = data


def _background_task(_hass: object, coro, name: str) -> asyncio.Task:
    return asyncio.get_running_loop().create_task(coro, name=name)


def _counting_fetch(result: MapData | None = None, *, error: Exception | None = None, gate=None):
    """A ``fetch_map`` double: counts its calls, can wait on ``gate``, can fail."""
    calls: list[int] = []

    async def fetch_map() -> MapData:
        calls.append(1)
        if gate is not None:
            await gate.wait()
        if error is not None:
            raise type(error)(*error.args)
        return result or _map()

    return fetch_map, calls


def _coordinator(
    fetch_map=None, *, awake: bool = True, docked: bool = True, trail: bool = False
) -> NarwalCoordinator:
    """The real coordinator of a robot that holds map ``HELD``; only its client is a double."""
    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.data = {"host": "127.0.0.1", "port": 9002, "device_id": "test-device"}
    entry.async_create_background_task.side_effect = _background_task
    coordinator = NarwalCoordinator(hass, entry)
    coordinator.hass = hass
    coordinator.config_entry = entry
    coordinator._room_selection_store_loaded = True
    coordinator._room_profile_store_loaded = True
    coordinator._room_order_store_loaded = True
    coordinator._consumable_poll_countdown = 99
    coordinator._last_topic_subscribe = coordinator_module.time.monotonic()
    state = NarwalState(working_status=WorkingStatus.DOCKED if docked else WorkingStatus.STANDBY)
    state.map_data = _map(HELD)
    coordinator.client = MagicMock()
    coordinator.client.state = state
    coordinator.client.connected = True
    coordinator.client.robot_awake = awake
    coordinator.client.wake = AsyncMock()
    coordinator.client.fetch_map = fetch_map or _counting_fetch()[0]
    coordinator.client.get_map = AsyncMock(side_effect=AssertionError("get_map stores at once"))
    coordinator.client.get_status = AsyncMock(
        return_value=CommandResponse(data={"2": {"3": {"1": 10}}})
    )
    coordinator.client.get_consumable_info = AsyncMock()
    coordinator.client.subscribe_to_topics = AsyncMock()
    coordinator.async_update_listeners = MagicMock()
    coordinator.async_set_updated_data = MagicMock()
    coordinator._map_display_cache_store = _Store()
    if trail:
        state.map_display_data = _trail()
        coordinator._retained_map_display = state.map_display_data
        coordinator._retained_map_identity = HELD
    return coordinator


def _stored(coordinator: NarwalCoordinator, identity: tuple[int, int], marker: bytes) -> dict:
    """The store payload of a trail saved on map ``identity``."""
    state = NarwalState(working_status=WorkingStatus.DOCKED)
    state.map_data = _map(identity)
    state.map_display_data = _trail(marker)
    return coordinator._map_display_cache_payload_from_snapshot(
        coordinator._map_display_cache_snapshot(state)
    )


def _queued(coordinator: NarwalCoordinator, identity: tuple[int, int] = HELD):
    """Queue the held trail for saving, as a clean that just ended leaves it."""
    snapshot = replace(
        coordinator._map_display_cache_snapshot(coordinator.client.state),
        map_id=identity[0],
        map_created_at=identity[1],
    )
    coordinator._pending_map_display_cache_snapshot = snapshot
    return snapshot


#: What a refresh that fails is allowed to change on the coordinator.
_REFRESH_BOOKKEEPING = {
    "_active_map_outcome",
    "_active_map_checked_at",
    "_active_map_task",
    "_active_map_task_wakes",
    "_align_trail_with_static_map",
}


def _everything(coordinator: NarwalCoordinator) -> dict[str, object]:
    """Every attribute of the coordinator, and the map and trail on the client.

    Unlike ``_trail_state`` this does not go by ``_TRAIL_STATE_ATTRS``, so it
    also sees a change to an attribute that list forgot.
    """
    state = coordinator.client.state
    return {
        "map_data": state.map_data,
        "map_display_data": state.map_display_data,
        **{k: v for k, v in vars(coordinator).items() if k not in _REFRESH_BOOKKEEPING},
    }


def _eager_background_task(_hass: object, coro, name: str) -> asyncio.Task:
    """Start the task at once, as Home Assistant's background tasks do."""
    return asyncio.Task(coro, loop=asyncio.get_running_loop(), name=name, eager_start=True)


async def _clean_started_in_the_app(coordinator: NarwalCoordinator) -> None:
    """Run a clean that began outside Home Assistant and left a trail, then dock again."""
    state = coordinator.client.state
    state.working_status = WorkingStatus.CLEANING
    coordinator._handle_working_status_transition(state)
    state.map_display_data = _trail()
    coordinator._retain_native_trajectory(state)
    state.working_status = WorkingStatus.DOCKED
    state.last_active_working_status_time = 0.0
    coordinator._handle_working_status_transition(state)
    await _settle()


def _trail_state(coordinator: NarwalCoordinator) -> dict[str, object]:
    """Everything a floor switch may touch."""
    state = coordinator.client.state
    return {
        "map_data": state.map_data,
        "map_display_data": state.map_display_data,
        **{name: getattr(coordinator, name) for name in _TRAIL_STATE_ATTRS},
    }


def _background_task_names(coordinator: NarwalCoordinator) -> list[str]:
    return [
        call.args[2]
        for call in coordinator.config_entry.async_create_background_task.call_args_list
    ]


async def _settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


def _at(seconds: float):
    """Freeze the coordinator's monotonic clock."""
    return patch.object(coordinator_module.time, "monotonic", return_value=seconds)


# --- the refresh itself ---------------------------------------------------------------


async def test_refresh_stores_the_map_fetched_under_the_dock_lock() -> None:
    locked: list[bool] = []
    coordinator = _coordinator()

    async def fetch_map() -> MapData:
        locked.append(coordinator.dock_action_lock.locked())
        return _map()

    coordinator.client.fetch_map = fetch_map

    refresh = await coordinator.async_refresh_active_map()

    assert locked == [True]
    assert not coordinator.dock_action_lock.locked()
    assert refresh.held is False
    assert refresh.map_data is coordinator.client.state.map_data
    assert refresh.map_data.map_id == OTHER[0]


async def test_entities_are_told_once_the_refresh_is_recorded() -> None:
    """After the swap, outside the lock, and without moving the 60 s poll."""
    seen: list[tuple] = []
    coordinator = _coordinator()
    coordinator.async_update_listeners.side_effect = lambda: seen.append(
        (
            coordinator.client.state.map_data.map_id,
            coordinator._active_map_outcome.map_data is coordinator.client.state.map_data,
            coordinator.dock_action_lock.locked(),
        )
    )

    await coordinator.async_refresh_active_map()

    assert seen == [(OTHER[0], True, False)]
    # async_set_updated_data would move the coordinator's 60 s poll on every refresh.
    coordinator.async_set_updated_data.assert_not_called()


async def test_a_raising_entity_does_not_undo_the_refresh(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """HA does not guard listeners; the fetched map stands, is reused and logged."""
    fetch_map, calls = _counting_fetch()
    coordinator = _coordinator(fetch_map)
    coordinator.async_update_listeners.side_effect = RuntimeError("entity broke")

    with caplog.at_level(logging.ERROR):
        first = await coordinator.async_refresh_active_map()
        second = await coordinator.async_refresh_active_map()

    assert (first.map_data.map_id, second.map_data.map_id) == (OTHER[0], OTHER[0])
    assert calls == [1]
    assert "Error telling the Narwal entities" in caplog.text
    assert "RuntimeError: entity broke" in caplog.text


async def test_a_sleeping_robot_off_the_dock_is_woken_first() -> None:
    order: list[str] = []
    coordinator = _coordinator(awake=False, docked=False)

    async def wake(timeout: float) -> None:
        order.append(f"wake {coordinator.dock_action_lock.locked()}")

    async def fetch_map() -> MapData:
        order.append("fetch_map")
        return _map()

    coordinator.client.wake = AsyncMock(side_effect=wake)
    coordinator.client.fetch_map = fetch_map

    await coordinator.async_refresh_active_map()

    assert order == ["wake True", "fetch_map"]
    coordinator.client.wake.assert_awaited_once_with(timeout=ACTIVE_MAP_WAKE_TIMEOUT)


async def test_a_docked_robot_is_not_woken() -> None:
    """Docked and quiet is normal: get_map goes out without a wake burst (#90)."""
    fetch_map, calls = _counting_fetch()
    coordinator = _coordinator(fetch_map, awake=False, docked=True)
    assert coordinator.client.state.is_docked

    await coordinator.async_refresh_active_map()

    coordinator.client.wake.assert_not_awaited()
    assert calls == [1]


async def test_an_awake_robot_is_not_woken() -> None:
    coordinator = _coordinator(awake=True, docked=False)

    await coordinator.async_refresh_active_map()

    coordinator.client.wake.assert_not_awaited()


async def test_a_robot_that_stays_quiet_is_asked_all_the_same() -> None:
    """wake() returns False when no broadcast came; get_map may still be answered."""
    fetch_map, calls = _counting_fetch()
    coordinator = _coordinator(fetch_map, awake=False, docked=False)
    coordinator.client.wake = AsyncMock(return_value=False)

    refresh = await coordinator.async_refresh_active_map()

    assert calls == [1]
    assert refresh.map_data.map_id == OTHER[0]


async def test_a_clean_that_began_during_the_wake_is_not_asked() -> None:
    """Started in the app while the robot was woken: no get_map into the clean."""
    fetch_map, calls = _counting_fetch()
    coordinator = _coordinator(fetch_map, awake=False, docked=False)
    held_map = coordinator.client.state.map_data

    async def wake(timeout: float) -> bool:
        coordinator.client.state.working_status = WorkingStatus.CLEANING
        return True

    coordinator.client.wake = wake

    refresh = await coordinator.async_refresh_active_map()

    assert (refresh.held, refresh.map_data) == (True, held_map)
    assert calls == []
    assert coordinator._active_map_outcome is None


async def test_a_clean_that_began_while_the_robot_was_asked_keeps_the_held_map() -> None:
    """The map is not swapped under a task, also not under one that just began."""
    coordinator = _coordinator(trail=True)
    before = _trail_state(coordinator)

    async def fetch_map() -> MapData:
        coordinator.client.state.working_status = WorkingStatus.CLEANING
        return _map()

    coordinator.client.fetch_map = fetch_map

    refresh = await coordinator.async_refresh_active_map()

    assert refresh.held is True
    assert refresh.map_data is before["map_data"]
    assert _trail_state(coordinator) == before
    assert coordinator._active_map_outcome is None
    coordinator.async_update_listeners.assert_not_called()


async def test_a_wake_that_fails_is_reported_without_asking_for_the_map() -> None:
    fetch_map, calls = _counting_fetch()
    coordinator = _coordinator(fetch_map, awake=False, docked=False)
    coordinator.client.wake = AsyncMock(
        side_effect=NarwalConnectionError("Not connected to vacuum")
    )

    with pytest.raises(ActiveMapError, match="Not connected to vacuum"):
        await coordinator.async_refresh_active_map()

    assert calls == []


@pytest.mark.parametrize(
    "status", [WorkingStatus.CLEANING, WorkingStatus.CLEANING_V2, WorkingStatus.REMAPPING]
)
async def test_during_a_task_the_held_map_answers(status: WorkingStatus) -> None:
    """The robot is not asked mid-task and no lock is taken."""
    fetch_map, calls = _counting_fetch()
    coordinator = _coordinator(fetch_map, awake=False, docked=False)
    held_map = coordinator.client.state.map_data
    coordinator.client.state.working_status = status

    async with coordinator.dock_action_lock:
        # Answered at once: a Stop holding the lock is not delayed by it, nor it by a Stop.
        refresh = await asyncio.wait_for(coordinator.async_refresh_active_map(), timeout=1)

    assert calls == []
    coordinator.client.wake.assert_not_awaited()
    assert refresh.held is True
    assert refresh.map_data is held_map


async def test_a_clean_started_while_waiting_answers_the_held_map() -> None:
    """vacuum.start held the lock and the clean began: no wake, no get_map mid-clean."""
    fetch_map, calls = _counting_fetch()
    coordinator = _coordinator(fetch_map, awake=False, docked=False)
    held_map = coordinator.client.state.map_data

    async with coordinator.dock_action_lock:
        pending = asyncio.ensure_future(coordinator.async_refresh_active_map())
        await _settle()
        coordinator.client.state.working_status = WorkingStatus.CLEANING
    refresh = await asyncio.wait_for(pending, timeout=1)

    assert (refresh.held, refresh.map_data) == (True, held_map)
    assert calls == []
    coordinator.client.wake.assert_not_awaited()
    coordinator.async_update_listeners.assert_not_called()

    # The held answer is not reused once the robot is docked again.
    coordinator.client.state.working_status = WorkingStatus.DOCKED
    assert (await coordinator.async_refresh_active_map()).held is False
    assert calls == [1]


async def test_waits_for_a_dock_action_that_starts_no_clean() -> None:
    """A dock action holding the lock finishes before get_map is sent."""
    fetch_map, calls = _counting_fetch()
    coordinator = _coordinator(fetch_map)

    async with coordinator.dock_action_lock:
        pending = asyncio.ensure_future(coordinator.async_refresh_active_map())
        await _settle()
        assert calls == []
    refresh = await pending

    assert (refresh.held, refresh.map_data.map_id) == (False, OTHER[0])
    assert calls == [1]


async def test_a_recent_refresh_is_answered_again_without_asking() -> None:
    fetch_map, calls = _counting_fetch()
    coordinator = _coordinator(fetch_map)

    with _at(100.0):
        first = await coordinator.async_refresh_active_map()
    with _at(100.0 + ACTIVE_MAP_TTL - 1):
        second = await coordinator.async_refresh_active_map()
    assert calls == [1]
    assert second.map_data is first.map_data
    assert second.held is False

    with _at(100.0 + ACTIVE_MAP_TTL + 1):
        await coordinator.async_refresh_active_map()
    assert calls == [1, 1]


async def test_a_map_fetched_elsewhere_ends_the_reuse() -> None:
    """After a clean start fetched the map itself, the robot is asked again."""
    fetch_map, calls = _counting_fetch()
    coordinator = _coordinator(fetch_map)

    with _at(100.0):
        await coordinator.async_refresh_active_map()
        coordinator.client.state.map_data = _map((5, 500))
        await coordinator.async_refresh_active_map()

    assert calls == [1, 1]


async def test_concurrent_callers_share_one_request() -> None:
    gate = asyncio.Event()
    fetch_map, calls = _counting_fetch(gate=gate)
    coordinator = _coordinator(fetch_map)

    waiters = [asyncio.ensure_future(coordinator.async_refresh_active_map()) for _ in range(3)]
    await _settle()
    gate.set()
    refreshes = await asyncio.gather(*waiters)

    assert calls == [1]
    assert {id(refresh.map_data) for refresh in refreshes} == {
        id(coordinator.client.state.map_data)
    }
    assert coordinator._active_map_task is None


async def test_a_caller_that_goes_away_does_not_cancel_the_request() -> None:
    gate = asyncio.Event()
    fetch_map, calls = _counting_fetch(gate=gate)
    coordinator = _coordinator(fetch_map)

    leaving = asyncio.ensure_future(coordinator.async_refresh_active_map())
    staying = asyncio.ensure_future(coordinator.async_refresh_active_map())
    await _settle()
    leaving.cancel()
    gate.set()

    assert (await staying).map_data.map_id == OTHER[0]
    with pytest.raises(asyncio.CancelledError):
        await leaving
    assert calls == [1]


async def test_a_failure_nobody_awaits_is_not_logged_as_unretrieved() -> None:
    """The only caller left; the refresh then fails. asyncio must not complain at GC."""
    loop = asyncio.get_running_loop()
    reported: list[dict] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    try:
        gate = asyncio.Event()
        fetch_map, _ = _counting_fetch(error=TimeoutError("robot asleep"), gate=gate)
        coordinator = _coordinator(fetch_map)

        leaving = asyncio.ensure_future(coordinator.async_refresh_active_map())
        await _settle()
        leaving.cancel()
        with pytest.raises(asyncio.CancelledError):
            await leaving
        gate.set()
        await _settle()
        assert coordinator._active_map_task is None
        del leaving
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert reported == []


async def test_the_lock_is_not_held_longer_than_the_robot_may_take(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_map can queue behind a command that is on its way: one limit covers that wait."""
    monkeypatch.setattr(coordinator_module, "ACTIVE_MAP_FETCH_TIMEOUT", 0.01)
    fetch_map, calls = _counting_fetch(gate=asyncio.Event())
    coordinator = _coordinator(fetch_map, trail=True)
    before = _trail_state(coordinator)

    with pytest.raises(ActiveMapError, match="^No answer from the robot within 0 s$"):
        await asyncio.wait_for(coordinator.async_refresh_active_map(), timeout=2)

    assert calls == [1]
    assert not coordinator.dock_action_lock.locked()
    assert _trail_state(coordinator) == before


async def test_the_wake_counts_towards_the_same_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(coordinator_module, "ACTIVE_MAP_FETCH_TIMEOUT", 0.01)
    monkeypatch.setattr(coordinator_module, "ACTIVE_MAP_WAKE_TIMEOUT", 0.01)
    fetch_map, calls = _counting_fetch()
    coordinator = _coordinator(fetch_map, awake=False, docked=False)

    async def wake(timeout: float) -> None:
        await asyncio.Event().wait()

    coordinator.client.wake = wake

    with pytest.raises(ActiveMapError, match="^No answer from the robot within"):
        await asyncio.wait_for(coordinator.async_refresh_active_map(), timeout=2)

    assert calls == []
    assert not coordinator.dock_action_lock.locked()


async def test_a_wake_has_its_own_share_of_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Off the dock the limit is the wake's time plus get_map's, not get_map's alone."""
    monkeypatch.setattr(coordinator_module, "ACTIVE_MAP_FETCH_TIMEOUT", 0.05)
    monkeypatch.setattr(coordinator_module, "ACTIVE_MAP_WAKE_TIMEOUT", 1.0)
    fetch_map, calls = _counting_fetch()
    coordinator = _coordinator(fetch_map, awake=False, docked=False)

    async def wake(timeout: float) -> None:
        await asyncio.sleep(0.2)

    coordinator.client.wake = wake

    refresh = await coordinator.async_refresh_active_map()

    assert refresh.map_data.map_id == OTHER[0]
    assert calls == [1]


# --- one failure policy, one back-off -------------------------------------------------


async def test_a_failure_is_reported_and_retried_after_a_pause() -> None:
    """A robot that does not answer is not asked again for a while; the lock stays free."""
    fetch_map, calls = _counting_fetch(error=TimeoutError())
    coordinator = _coordinator(fetch_map)

    with _at(100.0), pytest.raises(ActiveMapError, match="^TimeoutError$"):
        await coordinator.async_refresh_active_map()
    with (
        _at(100.0 + ACTIVE_MAP_FAILURE_BACKOFF - 1),
        pytest.raises(ActiveMapError, match="^TimeoutError$"),
    ):
        await coordinator.async_refresh_active_map()
    assert calls == [1]
    with _at(100.0 + ACTIVE_MAP_FAILURE_BACKOFF + 1), pytest.raises(ActiveMapError):
        await coordinator.async_refresh_active_map()

    assert calls == [1, 1]
    coordinator.async_update_listeners.assert_not_called()
    assert not coordinator.dock_action_lock.locked()


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
async def test_a_robot_that_does_not_answer_raises_active_map_error(error: Exception) -> None:
    fetch_map, _ = _counting_fetch(error=error)
    coordinator = _coordinator(fetch_map)
    held_map = coordinator.client.state.map_data

    with pytest.raises(ActiveMapError):
        await coordinator.async_refresh_active_map()

    assert coordinator._active_map_outcome.failure is not None
    assert coordinator.client.state.map_data is held_map


@pytest.mark.parametrize("where", ["fetch_map", "align"])
async def test_a_bug_is_raised_as_it_is_and_backs_off_too(where: str) -> None:
    """A programming error reaches the caller for its traceback, but not on every request."""
    fetch_map, calls = _counting_fetch(
        error=AttributeError("no attribute") if where == "fetch_map" else None
    )
    coordinator = _coordinator(fetch_map, trail=True)
    held_map = coordinator.client.state.map_data
    if where == "align":
        coordinator._align_trail_with_static_map = MagicMock(
            side_effect=AttributeError("no attribute")
        )

    with _at(100.0), pytest.raises(AttributeError):
        await coordinator.async_refresh_active_map()
    with _at(100.0 + 1), pytest.raises(ActiveMapError, match="no attribute"):
        await coordinator.async_refresh_active_map()
    assert calls == [1]

    with _at(100.0 + ACTIVE_MAP_FAILURE_BACKOFF + 1), pytest.raises(AttributeError):
        await coordinator.async_refresh_active_map()
    assert calls == [1, 1]
    coordinator.async_update_listeners.assert_not_called()
    assert coordinator.client.state.map_data is held_map


async def test_a_socket_error_that_is_no_lost_connection_is_a_bug() -> None:
    """Only a connection that dropped means the robot did not answer."""
    fetch_map, _ = _counting_fetch(error=ProtocolError("unexpected frame"))
    coordinator = _coordinator(fetch_map)

    with pytest.raises(ProtocolError):
        await coordinator.async_refresh_active_map()


async def test_a_robot_that_woke_after_failing_asleep_is_asked_again() -> None:
    """The robot stayed quiet and get_map timed out; then a late broadcast shows it
    awake: no back-off."""
    calls: list[int] = []

    async def fetch_map() -> MapData:
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError
        return _map()

    coordinator = _coordinator(fetch_map, awake=False, docked=False)
    coordinator.client.wake = AsyncMock(return_value=False)

    for _ in range(2):
        with pytest.raises(ActiveMapError):
            await coordinator.async_refresh_active_map()
    assert calls == [1]
    coordinator.client.wake.assert_awaited_once()

    coordinator.client.robot_awake = True
    refresh = await coordinator.async_refresh_active_map()

    assert calls == [1, 1]
    assert refresh.map_data.map_id == OTHER[0]


async def test_a_failure_after_a_success_is_not_masked_by_it() -> None:
    """The newest outcome decides: a failed refresh is reported although the last
    success is younger than the reuse time and its map is still the held one."""
    fetch_map, calls = _counting_fetch()
    coordinator = _coordinator(fetch_map)

    with _at(100.0):
        await coordinator.async_refresh_active_map()
    failing, failing_calls = _counting_fetch(error=TimeoutError())
    coordinator.client.fetch_map = failing
    # The poll's own refresh, which does not look at the reuse time.
    coordinator._active_map_checked_at = 100.0 - ACTIVE_MAP_POLL_INTERVAL
    with _at(110.0):
        coordinator._async_poll_active_map()
        await _settle()
    assert failing_calls == [1]

    with _at(112.0), pytest.raises(ActiveMapError, match="TimeoutError"):
        await coordinator.async_refresh_active_map()
    assert failing_calls == [1]

    # And a success after a failure ends the back-off.
    coordinator.client.fetch_map = fetch_map
    with _at(110.0 + ACTIVE_MAP_FAILURE_BACKOFF + 1):
        await coordinator.async_refresh_active_map()
    with _at(110.0 + ACTIVE_MAP_FAILURE_BACKOFF + 2):
        assert (await coordinator.async_refresh_active_map()).held is False
    assert calls == [1, 1]


# --- a floor switch applies whole or not at all ---------------------------------------


async def test_a_failure_while_aligning_changes_nothing() -> None:
    """The map is swapped only together with the trail: a raise in between is undone."""
    coordinator = _coordinator(trail=True)
    queued = _queued(coordinator)
    coordinator._map_display_cache_signature = (4, 4, 7)
    coordinator._map_display_cache_active_clean = False
    before = _trail_state(coordinator)
    real_reset = coordinator._reset_map_display_cache_state

    def align() -> None:
        # As far as a real alignment gets before something in it raises.
        assert coordinator.client.state.map_data.map_id == OTHER[0]
        real_reset(clear_memory=True)
        coordinator._retained_map_identity = OTHER
        raise AttributeError("no attribute")

    coordinator._align_trail_with_static_map = align

    with pytest.raises(AttributeError):
        await coordinator.async_refresh_active_map()

    assert _trail_state(coordinator) == before
    assert coordinator.client.state.map_data.map_id == HELD[0]
    assert coordinator._pending_map_display_cache_snapshot is queued
    assert coordinator._active_map_outcome.failure == "no attribute"
    coordinator.async_update_listeners.assert_not_called()
    assert not coordinator.dock_action_lock.locked()


@pytest.mark.parametrize("case", ["plain", "queued", "waiting"])
async def test_whatever_the_alignment_did_is_undone_when_it_fails(case: str) -> None:
    """The real alignment runs to its end on each of its paths, then raises.

    Everything on the coordinator is compared, not only the attributes the
    rollback knows about, so one it forgot shows up here.
    """
    coordinator = _coordinator(
        _counting_fetch(_map(HELD if case == "waiting" else OTHER))[0], trail=case != "waiting"
    )
    store = coordinator._map_display_cache_store
    if case == "queued":
        _queued(coordinator)
    elif case == "waiting":
        coordinator._pending_map_display_cache_restore = _stored(coordinator, HELD, b"aaaa")
    coordinator._map_display_cache_signature = (4, 4, 7)
    coordinator._map_display_cache_active_clean = False
    real_align = coordinator._align_trail_with_static_map
    changed: list[bool] = []

    def align() -> None:
        real_align()
        changed.append(_everything(coordinator) != before)
        raise AttributeError("no attribute")

    before = _everything(coordinator)
    coordinator._align_trail_with_static_map = align

    with pytest.raises(AttributeError):
        await coordinator.async_refresh_active_map()
    await _settle()

    # The alignment did change something, and none of it is left.
    assert changed == [True]
    assert _everything(coordinator) == before
    assert (store.loads, store.saved) == (0, [])


async def test_a_failed_fetch_leaves_the_held_floor_and_its_trail() -> None:
    fetch_map, _ = _counting_fetch(error=TimeoutError())
    coordinator = _coordinator(fetch_map, trail=True)
    before = _trail_state(coordinator)

    with pytest.raises(ActiveMapError):
        await coordinator.async_refresh_active_map()

    assert _trail_state(coordinator) == before
    assert coordinator._map_display_cache_store.loads == 0


async def test_a_cancelled_refresh_changes_nothing_and_is_no_failure() -> None:
    """Cancelled while the robot is asked: the floor is not half switched."""
    asked = asyncio.Event()

    async def fetch_map() -> MapData:
        asked.set()
        await asyncio.Event().wait()
        return _map()

    coordinator = _coordinator(fetch_map, trail=True)
    before = _trail_state(coordinator)

    caller = asyncio.ensure_future(coordinator.async_refresh_active_map())
    await asked.wait()
    coordinator._active_map_task.cancel()
    with pytest.raises(ActiveMapError, match="unloaded"):
        await caller

    assert _trail_state(coordinator) == before
    assert coordinator._active_map_outcome is None
    assert not coordinator.dock_action_lock.locked()


# --- the trail on a floor switch ------------------------------------------------------


async def test_another_floor_drops_the_trail_and_leaves_the_store_alone() -> None:
    """A floor switched in the app: the old floor's trail and pose leave memory.

    Looking at a floor is no clean, so the stored trail of the last clean is
    neither written nor read.
    """
    coordinator = _coordinator(trail=True)
    store = coordinator._map_display_cache_store
    store.data = _stored(coordinator, HELD, b"aaaa")

    await coordinator.async_refresh_active_map()
    await _settle()

    assert coordinator.client.state.map_display_data is None
    assert coordinator._retained_map_display is None
    assert coordinator._retained_map_identity == OTHER
    assert (store.loads, store.saved) == (0, [])
    assert store.data == _stored(coordinator, HELD, b"aaaa")
    assert coordinator._map_display_cache_clear_pending is False


async def test_a_trail_is_not_brought_back_by_a_floor_switch() -> None:
    """A trail carries the map that was held when its clean began. For a clean
    started in the app right after a switch that is the wrong map, so going by
    the label would draw the trail on a floor it was not recorded on: the
    stored trail is restored at startup as before, never by a floor switch."""
    coordinator = _coordinator(trail=True)
    store = coordinator._map_display_cache_store

    # To the other floor, while the store holds a trail labelled with it ...
    store.data = _stored(coordinator, OTHER, b"bbbb")
    await coordinator.async_refresh_active_map()
    assert coordinator.client.state.map_data.map_id == OTHER[0]
    assert coordinator.client.state.map_display_data is None

    # ... and back, while it holds one labelled with the floor returned to.
    store.data = _stored(coordinator, HELD, b"aaaa")
    coordinator.client.fetch_map = _counting_fetch(_map(HELD))[0]
    with _at(coordinator_module.time.monotonic() + ACTIVE_MAP_TTL + 1):
        await coordinator.async_refresh_active_map()
    await _settle()

    assert coordinator.client.state.map_data.map_id == HELD[0]
    assert coordinator.client.state.map_display_data is None
    assert (store.loads, store.saved) == (0, [])


async def test_a_queued_save_of_the_old_floor_still_reaches_the_store() -> None:
    """The last clean's trail was not saved yet when the floor was switched."""
    coordinator = _coordinator(trail=True)
    queued = _queued(coordinator)
    store = coordinator._map_display_cache_store

    await coordinator.async_refresh_active_map()

    assert coordinator.client.state.map_display_data is None
    assert coordinator._pending_map_display_cache_snapshot is queued

    await coordinator._async_save_pending_map_display_cache(0.0)
    assert store.saved == [_stored(coordinator, HELD, b"aaaa")]
    # A poll now finds nothing to write: memory and store do not disagree.
    coordinator._schedule_map_display_cache_save(coordinator.client.state)
    assert coordinator._pending_map_display_cache_snapshot is None


async def test_a_queued_save_without_a_map_is_dropped() -> None:
    """A snapshot that never knew its map would be bound to the new floor: it goes."""
    coordinator = _coordinator(trail=True)
    _queued(coordinator, (0, 0))

    await coordinator.async_refresh_active_map()

    assert coordinator._pending_map_display_cache_snapshot is None


async def test_the_same_floor_keeps_its_trail() -> None:
    coordinator = _coordinator(_counting_fetch(_map(HELD))[0], trail=True)
    trail = coordinator.client.state.map_display_data

    await coordinator.async_refresh_active_map()

    assert coordinator.client.state.map_display_data is trail
    assert coordinator._map_display_cache_store.loads == 0
    assert coordinator._map_display_cache_store.saved == []


async def test_a_trail_retained_before_any_map_is_bound_to_the_first_one() -> None:
    """get_map failed at startup mid-clean: the trail is bound to the first map fetched,
    so the next floor switch is recognised and drops it."""
    coordinator = _coordinator(_counting_fetch(_map(HELD))[0], trail=True)
    coordinator._retained_map_identity = None

    await coordinator.async_refresh_active_map()
    assert coordinator._retained_map_identity == HELD
    assert coordinator.client.state.map_display_data is not None

    coordinator.client.fetch_map = _counting_fetch(_map(OTHER))[0]
    with _at(coordinator_module.time.monotonic() + ACTIVE_MAP_TTL + 1):
        await coordinator.async_refresh_active_map()

    assert coordinator.client.state.map_display_data is None
    assert coordinator._retained_map_identity == OTHER


async def test_a_trail_cache_that_waited_for_a_map_is_restored() -> None:
    coordinator = _coordinator(_counting_fetch(_map(HELD))[0])
    coordinator._pending_map_display_cache_restore = _stored(coordinator, HELD, b"aaaa")

    await coordinator.async_refresh_active_map()

    assert coordinator._pending_map_display_cache_restore is None
    assert coordinator.client.state.map_display_data.trajectory_x_values == b"aaaa"
    assert coordinator._retained_map_identity == HELD
    assert coordinator._map_display_cache_store.saved == []


async def test_the_trail_of_a_clean_started_in_the_app_never_changes_floor() -> None:
    """Floor switched in the app, clean started there before the poll saw it.

    The trail was recorded on the map held then. The first map fetched after
    the clean is another one: the trail leaves the camera, and it is not shown
    on the floor it was bound to when the robot is back there.
    """
    coordinator = _coordinator()
    await _clean_started_in_the_app(coordinator)
    assert coordinator._retained_map_identity == HELD
    assert coordinator.client.state.map_display_data is not None

    await coordinator.async_refresh_active_map()
    assert coordinator.client.state.map_data.map_id == OTHER[0]
    assert coordinator.client.state.map_display_data is None

    coordinator.client.fetch_map = _counting_fetch(_map(HELD))[0]
    with _at(coordinator_module.time.monotonic() + ACTIVE_MAP_TTL + 1):
        await coordinator.async_refresh_active_map()
    await _settle()
    assert coordinator.client.state.map_data.map_id == HELD[0]
    assert coordinator.client.state.map_display_data is None


# --- unload ---------------------------------------------------------------------------


async def test_shutdown_ends_a_running_refresh_before_the_trail_is_flushed() -> None:
    """A reload mid-refresh: nothing is applied once the flush has begun."""
    gate = asyncio.Event()
    fetch_map, calls = _counting_fetch(gate=gate)
    coordinator = _coordinator(fetch_map, trail=True)
    before = _trail_state(coordinator)
    order: list[str] = []

    async def disconnect() -> None:
        order.append(f"disconnect, refresh running: {coordinator._active_map_task is not None}")

    async def flush() -> None:
        order.append(f"flush, refresh running: {coordinator._active_map_task is not None}")
        # Let a refresh that was not ended run on: it must not get here.
        gate.set()
        await _settle()

    coordinator.client.disconnect = disconnect
    coordinator._async_flush_map_display_cache = flush
    coordinator._async_save_room_selections = AsyncMock()
    coordinator._async_save_room_orders = AsyncMock()
    caller = asyncio.ensure_future(coordinator.async_refresh_active_map())
    await _settle()
    assert calls == [1]

    base = NarwalCoordinator.__mro__[1]
    with patch.object(base, "async_shutdown", AsyncMock(), create=True):
        await coordinator.async_shutdown()

    assert order == ["disconnect, refresh running: False", "flush, refresh running: False"]
    assert _trail_state(coordinator) == before
    with pytest.raises(ActiveMapError, match="unloaded"):
        await caller
    coordinator.async_update_listeners.assert_not_called()


async def test_nothing_is_refreshed_after_shutdown() -> None:
    """A request or a poll during the flush starts no new refresh."""
    fetch_map, calls = _counting_fetch()
    coordinator = _coordinator(fetch_map)

    await coordinator._async_close_active_map_refresh()

    with pytest.raises(ActiveMapError, match="unloaded"):
        await coordinator.async_refresh_active_map()
    coordinator._active_map_checked_at -= ACTIVE_MAP_POLL_INTERVAL
    coordinator._async_poll_active_map()
    await _settle()
    assert calls == []
    assert coordinator._active_map_task is None


# --- the lock and a pending-profiles transaction --------------------------------------


def _profiles_coordinator(fetched: MapData, gate: asyncio.Event) -> NarwalCoordinator:
    """The services' coordinator (map 77, rooms 4 and 7), with a real refresh."""
    coordinator = _service_coordinator()
    coordinator.config_entry.async_create_background_task.side_effect = _background_task
    coordinator.client.fetch_map, _ = _counting_fetch(fetched, gate=gate)
    return coordinator


async def _apply_profiles_during_a_refresh(coordinator: NarwalCoordinator, gate: asyncio.Event):
    handler, registry = _register_pending_profiles_handler(coordinator)
    refresh = asyncio.ensure_future(coordinator.async_refresh_active_map())
    await _settle()
    assert coordinator.dock_action_lock.locked()
    with (
        patch(
            "custom_components.narwal.service.async_extract_entity_ids",
            new_callable=AsyncMock,
            return_value=["vacuum.downstairs_narwal_vacuum"],
        ),
        patch("custom_components.narwal.er.async_get", return_value=registry),
    ):
        profiles = asyncio.ensure_future(handler(_pending_profiles_call()))
        await _settle()
        # The transaction waits for the refresh instead of failing or cutting in.
        assert not profiles.done()
        gate.set()
        await refresh
        return await asyncio.gather(profiles, return_exceptions=True)


async def test_pending_profiles_behind_a_refresh_apply_on_the_same_floor() -> None:
    gate = asyncio.Event()
    same_floor = MapData(map_id=77, rooms=[RoomInfo(room_id=4), RoomInfo(room_id=7)])
    coordinator = _profiles_coordinator(same_floor, gate)
    before = coordinator.clean_settings

    (result,) = await _apply_profiles_during_a_refresh(coordinator, gate)

    assert result is None
    assert coordinator.client.state.map_data is same_floor
    assert coordinator.clean_settings is not before
    assert set(coordinator.room_clean_settings) == {("77", 4), ("77", 7)}


async def test_pending_profiles_behind_a_floor_switch_are_rejected_whole() -> None:
    """Profiles resolved against the other floor's rooms: its own check refuses them."""
    gate = asyncio.Event()
    other_floor = MapData(map_id=78, rooms=[RoomInfo(room_id=4), RoomInfo(room_id=9)])
    coordinator = _profiles_coordinator(other_floor, gate)
    before = coordinator.clean_settings

    (result,) = await _apply_profiles_during_a_refresh(coordinator, gate)

    assert isinstance(result, HomeAssistantError)
    assert "current map changed" in str(result)
    assert coordinator.client.state.map_data is other_floor
    assert coordinator.clean_settings is before
    assert coordinator.room_clean_settings == {}


# --- the poll fallback ----------------------------------------------------------------


def _due(coordinator: NarwalCoordinator) -> None:
    coordinator._active_map_checked_at -= ACTIVE_MAP_POLL_INTERVAL


async def test_the_poll_follows_a_floor_switch_nobody_asked_about() -> None:
    """No dashboard open: the poll itself finds the floor switched in the app (#43)."""
    fetch_map, calls = _counting_fetch()
    coordinator = _coordinator(fetch_map, awake=False, trail=True)
    _due(coordinator)

    state = await coordinator._async_update_data()
    await _settle()

    assert calls == [1]
    assert state.map_data.map_id == OTHER[0]
    assert [room.room_id for room in state.map_data.rooms if room.room_id > 0] == [1, 3]
    assert state.map_display_data is None
    coordinator.async_update_listeners.assert_called_once_with()
    coordinator.client.wake.assert_not_awaited()
    coordinator.client.get_map.assert_not_awaited()


async def test_the_poll_asks_once_per_interval() -> None:
    fetch_map, calls = _counting_fetch()
    coordinator = _coordinator(fetch_map)

    with _at(1000.0):
        coordinator._active_map_checked_at = 1000.0
        await coordinator._async_update_data()
        await _settle()
    assert calls == []

    with _at(1000.0 + ACTIVE_MAP_POLL_INTERVAL - 1):
        await coordinator._async_update_data()
        await _settle()
    assert calls == []

    with _at(1000.0 + ACTIVE_MAP_POLL_INTERVAL):
        await coordinator._async_update_data()
        await _settle()
        await coordinator._async_update_data()
        await _settle()
    assert calls == [1]

    with _at(1000.0 + 2 * ACTIVE_MAP_POLL_INTERVAL - 1):
        await coordinator._async_update_data()
        await _settle()
    assert calls == [1]

    with _at(1000.0 + 2 * ACTIVE_MAP_POLL_INTERVAL):
        await coordinator._async_update_data()
        await _settle()
    assert calls == [1, 1]


async def test_a_refresh_somebody_asked_for_counts_for_the_poll() -> None:
    fetch_map, calls = _counting_fetch()
    coordinator = _coordinator(fetch_map)

    with _at(1000.0):
        coordinator._active_map_checked_at = 1000.0 - ACTIVE_MAP_POLL_INTERVAL + 1
        await coordinator.async_refresh_active_map()
    with _at(1000.0 + ACTIVE_MAP_POLL_INTERVAL - 1):
        await coordinator._async_update_data()
        await _settle()

    assert calls == [1]


async def test_the_poll_never_wakes_the_robot() -> None:
    """Off the dock and quiet: the robot is asked as it is, never with a wake burst."""
    fetch_map, calls = _counting_fetch()
    coordinator = _coordinator(fetch_map, awake=False, docked=False)
    _due(coordinator)

    await coordinator._async_update_data()
    await _settle()

    assert calls == [1]
    coordinator.client.wake.assert_not_awaited()


@pytest.mark.parametrize(
    "status", [WorkingStatus.CLEANING, WorkingStatus.CLEANING_V2, WorkingStatus.REMAPPING]
)
async def test_the_poll_does_not_ask_during_a_task(status: WorkingStatus) -> None:
    fetch_map, calls = _counting_fetch()
    coordinator = _coordinator(fetch_map)
    coordinator.client.state.working_status = status
    _due(coordinator)

    await coordinator._async_update_data()
    await _settle()

    assert calls == []
    # Not even started: nothing queues for the lock a Stop or a Pause needs.
    assert "narwal_active_map" not in _background_task_names(coordinator)

    # The task did not use up the interval: back on the dock, the next poll asks.
    coordinator.client.state.working_status = WorkingStatus.DOCKED
    coordinator.client.state.last_active_working_status_time = 0.0
    await coordinator._async_update_data()
    await _settle()
    assert calls == [1]


async def test_a_held_answer_does_not_use_up_the_polls_interval() -> None:
    """A request during a task asked the robot nothing, so the poll still owes the check."""
    fetch_map, calls = _counting_fetch()
    coordinator = _coordinator(fetch_map)
    _due(coordinator)

    # Both ways to a held answer: a clean that began while the request waited
    # for the lock, and a request during the clean.
    async with coordinator.dock_action_lock:
        pending = asyncio.ensure_future(coordinator.async_refresh_active_map())
        await _settle()
        coordinator.client.state.working_status = WorkingStatus.CLEANING
    assert (await pending).held is True
    assert (await coordinator.async_refresh_active_map()).held is True
    assert calls == []

    coordinator.client.state.working_status = WorkingStatus.DOCKED
    coordinator.client.state.last_active_working_status_time = 0.0
    await coordinator._async_update_data()
    await _settle()
    assert calls == [1]


async def test_a_failed_status_poll_asks_for_no_map() -> None:
    """A robot that does not answer get_status is not sent a get_map on top."""
    fetch_map, calls = _counting_fetch()
    coordinator = _coordinator(fetch_map)
    coordinator.client.get_status = AsyncMock(side_effect=TimeoutError())
    _due(coordinator)

    await coordinator._async_update_data()
    await _settle()

    assert calls == []


async def test_the_poll_does_not_wait_for_the_map() -> None:
    gate = asyncio.Event()
    fetch_map, calls = _counting_fetch(gate=gate)
    coordinator = _coordinator(fetch_map)
    _due(coordinator)

    state = await asyncio.wait_for(coordinator._async_update_data(), timeout=1)
    await _settle()

    assert calls == [1]
    assert state.map_data.map_id == HELD[0]
    # A second poll while the first refresh is still out starts no second one.
    _due(coordinator)
    await coordinator._async_update_data()
    await _settle()
    assert calls == [1]

    gate.set()
    await _settle()
    assert state.map_data.map_id == OTHER[0]
    assert calls == [1]
    assert _background_task_names(coordinator).count("narwal_active_map") == 1


async def test_a_polled_refresh_that_fails_waits_for_the_next_interval(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A robot that does not answer get_map is asked once per interval, not per poll."""
    fetch_map, calls = _counting_fetch(error=TimeoutError())
    coordinator = _coordinator(fetch_map)

    with _at(1000.0), caplog.at_level(logging.DEBUG):
        coordinator._active_map_checked_at = 1000.0 - ACTIVE_MAP_POLL_INTERVAL
        await coordinator._async_update_data()
        await _settle()
    with _at(1000.0 + ACTIVE_MAP_FAILURE_BACKOFF + 1):
        await coordinator._async_update_data()
        await _settle()

    assert calls == [1]
    assert "Active map refresh failed: TimeoutError" in caplog.text
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


async def test_a_bug_in_a_polled_refresh_is_logged_with_its_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Nobody awaits the poll's refresh, so the coordinator logs what Home Assistant would."""
    fetch_map, _ = _counting_fetch(error=AttributeError("no attribute"))
    coordinator = _coordinator(fetch_map)
    _due(coordinator)

    with caplog.at_level(logging.ERROR):
        await coordinator._async_update_data()
        await _settle()

    assert "Unexpected error refreshing the active Narwal map" in caplog.text
    assert "AttributeError: no attribute" in caplog.text


async def test_a_request_after_the_polls_failed_refresh_gets_its_own_attempt() -> None:
    """The poll wakes no robot, so its failure does not stand in for a request's."""
    failing, failed_calls = _counting_fetch(error=TimeoutError())
    coordinator = _coordinator(failing, awake=False, docked=False)
    _due(coordinator)
    coordinator._async_poll_active_map()
    await _settle()
    assert failed_calls == [1]
    coordinator.client.wake.assert_not_awaited()

    # The request wakes the robot and asks again, inside the poll's back-off.
    with pytest.raises(ActiveMapError):
        await coordinator.async_refresh_active_map()
    assert failed_calls == [1, 1]
    coordinator.client.wake.assert_awaited_once()

    # Its own failure backs off as any other.
    with pytest.raises(ActiveMapError):
        await coordinator.async_refresh_active_map()
    assert failed_calls == [1, 1]
    coordinator.client.wake.assert_awaited_once()


async def test_a_docked_robot_that_failed_the_poll_is_not_asked_again_at_once() -> None:
    """A request would send the same unwoken get_map, so the poll's failure stands."""
    failing, failed_calls = _counting_fetch(error=TimeoutError())
    coordinator = _coordinator(failing, awake=False, docked=True)
    _due(coordinator)
    coordinator._async_poll_active_map()
    await _settle()

    with pytest.raises(ActiveMapError, match="TimeoutError"):
        await coordinator.async_refresh_active_map()

    assert failed_calls == [1]
    coordinator.client.wake.assert_not_awaited()


async def test_a_bug_in_the_polls_refresh_pauses_requests_too() -> None:
    """The exception for a robot nobody woke is not for a bug: it would be hit again."""
    failing, failed_calls = _counting_fetch(error=AttributeError("no attribute"))
    coordinator = _coordinator(failing, awake=False, docked=False)
    _due(coordinator)
    coordinator._async_poll_active_map()
    await _settle()

    with pytest.raises(ActiveMapError, match="no attribute"):
        await coordinator.async_refresh_active_map()

    assert failed_calls == [1]
    coordinator.client.wake.assert_not_awaited()


async def test_a_robot_that_woke_during_the_polls_failed_refresh_is_asked_by_a_request() -> None:
    """Quiet off the dock when the poll asked, roused by the keepalive meanwhile,
    get_map still unanswered: the request has not had its attempt yet."""
    calls: list[int] = []
    coordinator = _coordinator(awake=False, docked=False)

    async def fetch_map() -> MapData:
        calls.append(1)
        if len(calls) == 1:
            coordinator.client.robot_awake = True
            raise TimeoutError
        return _map()

    coordinator.client.fetch_map = fetch_map
    _due(coordinator)
    coordinator._async_poll_active_map()
    await _settle()
    assert calls == [1]

    refresh = await coordinator.async_refresh_active_map()

    assert calls == [1, 1]
    assert refresh.map_data.map_id == OTHER[0]


async def test_a_clean_that_began_while_the_robot_did_not_answer_is_a_held_answer() -> None:
    """Busy starting a clean, the robot leaves get_map unanswered: no failure,
    no back-off, the held map as for any request during a task."""
    coordinator = _coordinator(trail=True)
    before = _trail_state(coordinator)

    async def fetch_map() -> MapData:
        coordinator.client.state.working_status = WorkingStatus.CLEANING
        raise TimeoutError

    coordinator.client.fetch_map = fetch_map

    refresh = await coordinator.async_refresh_active_map()

    assert refresh.held is True
    assert refresh.map_data is before["map_data"]
    assert _trail_state(coordinator) == before
    assert coordinator._active_map_outcome is None


async def test_an_awake_robot_that_failed_the_poll_is_not_asked_again_at_once() -> None:
    """Waking would change nothing, so the poll's failure backs a request off."""
    failing, failed_calls = _counting_fetch(error=TimeoutError())
    coordinator = _coordinator(failing, awake=True, docked=False)
    _due(coordinator)
    coordinator._async_poll_active_map()
    await _settle()

    with pytest.raises(ActiveMapError, match="TimeoutError"):
        await coordinator.async_refresh_active_map()

    assert failed_calls == [1]


async def test_a_request_that_joined_the_polls_failed_refresh_gets_its_own_attempt() -> None:
    """The request shares the poll's get_map; when that fails unwoken, it wakes and asks."""
    gate = asyncio.Event()
    calls: list[int] = []

    async def fetch_map() -> MapData:
        calls.append(1)
        if len(calls) == 1:
            await gate.wait()
            raise TimeoutError
        return _map()

    coordinator = _coordinator(fetch_map, awake=False, docked=False)
    _due(coordinator)
    coordinator._async_poll_active_map()
    await _settle()
    request = asyncio.ensure_future(coordinator.async_refresh_active_map())
    await _settle()
    assert calls == [1]
    coordinator.client.wake.assert_not_awaited()

    gate.set()
    refresh = await asyncio.wait_for(request, timeout=2)

    assert calls == [1, 1]
    coordinator.client.wake.assert_awaited_once()
    assert refresh.map_data.map_id == OTHER[0]


async def test_a_request_shares_the_polls_refresh_that_succeeds() -> None:
    gate = asyncio.Event()
    fetch_map, calls = _counting_fetch(gate=gate)
    coordinator = _coordinator(fetch_map, awake=False, docked=False)
    _due(coordinator)
    coordinator._async_poll_active_map()
    await _settle()

    request = asyncio.ensure_future(coordinator.async_refresh_active_map())
    await _settle()
    gate.set()
    refresh = await request

    assert calls == [1]
    coordinator.client.wake.assert_not_awaited()
    assert (refresh.held, refresh.map_data.map_id) == (False, OTHER[0])


async def test_a_finished_refresh_that_is_still_referenced_is_not_joined() -> None:
    """Home Assistant starts tasks eagerly: a refresh can be over before its done
    callback has run. A caller arriving then must not wait on it for ever."""

    async def failed() -> None:
        raise ActiveMapError("TimeoutError")

    fetch_map, calls = _counting_fetch()
    coordinator = _coordinator(fetch_map)
    finished = asyncio.ensure_future(failed())
    await asyncio.wait([finished])
    coordinator._active_map_task = finished
    coordinator._active_map_task_wakes = False

    refresh = await asyncio.wait_for(coordinator.async_refresh_active_map(), timeout=2)

    assert calls == [1]
    assert refresh.map_data.map_id == OTHER[0]
    # Nor does the poll take it for a refresh under way.
    coordinator._active_map_task = finished
    _due(coordinator)
    coordinator._async_poll_active_map()
    await _settle()
    assert calls == [1, 1]


async def test_a_bug_whose_caller_went_away_is_logged_with_its_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Nobody is left to raise it to, and the next callers only see the back-off."""
    gate = asyncio.Event()
    fetch_map, _ = _counting_fetch(error=AttributeError("no attribute"), gate=gate)
    coordinator = _coordinator(fetch_map)

    leaving = asyncio.ensure_future(coordinator.async_refresh_active_map())
    await _settle()
    leaving.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leaving
    with caplog.at_level(logging.ERROR):
        gate.set()
        await _settle()

    assert "Unexpected error refreshing the active Narwal map" in caplog.text
    assert "AttributeError: no attribute" in caplog.text


async def test_a_bug_is_logged_by_the_coordinator_also_when_a_caller_gets_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Whether somebody waits cannot be told reliably when a refresh ends, so the
    traceback is logged in every case rather than lost in one."""
    fetch_map, _ = _counting_fetch(error=AttributeError("no attribute"))
    coordinator = _coordinator(fetch_map)

    with caplog.at_level(logging.ERROR), pytest.raises(AttributeError):
        await coordinator.async_refresh_active_map()
    await _settle()

    assert caplog.text.count("Unexpected error refreshing the active Narwal map") == 1
    assert "AttributeError: no attribute" in caplog.text


async def test_a_request_asks_a_model_that_does_not_broadcast() -> None:
    """Only the unattended poll leaves such a model alone; a caller's request is sent."""
    fetch_map, calls = _counting_fetch()
    coordinator = _coordinator(fetch_map)
    coordinator.client.supports_broadcasts = False

    refresh = await coordinator.async_refresh_active_map()

    assert calls == [1]
    assert (refresh.held, refresh.map_data.map_id) == (False, OTHER[0])


async def test_the_poll_leaves_a_model_alone_that_does_not_broadcast() -> None:
    """Its status does not say whether a clean runs, so a get_map could land in one."""
    fetch_map, calls = _counting_fetch()
    coordinator = _coordinator(fetch_map)
    coordinator.client.supports_broadcasts = False
    _due(coordinator)

    await coordinator._async_update_data()
    await _settle()

    assert calls == []
    assert "narwal_active_map" not in _background_task_names(coordinator)


async def test_the_polls_get_map_goes_out_after_the_polls_own_commands() -> None:
    """Home Assistant starts the refresh at once: it must not get in among the
    status poll's commands, whose answers carry no name."""
    order: list[str] = []
    coordinator = _coordinator()
    coordinator.config_entry.async_create_background_task.side_effect = _eager_background_task

    async def fetch_map() -> MapData:
        order.append("get_map")
        return _map()

    coordinator.client.fetch_map = fetch_map
    for name in ("get_status", "subscribe_to_topics", "get_consumable_info"):
        mock = getattr(coordinator.client, name)
        mock.side_effect = lambda *args, _name=name, _mock=mock, **kwargs: (
            order.append(_name),
            _mock.return_value,
        )[1]
    coordinator._last_topic_subscribe = coordinator_module.time.monotonic() - 100_000
    coordinator._consumable_poll_countdown = 0
    _due(coordinator)

    await coordinator._async_update_data()
    await _settle()

    assert order == ["get_status", "subscribe_to_topics", "get_consumable_info", "get_map"]


async def test_a_missing_map_is_fetched_by_the_poll_as_before() -> None:
    """Without a map the poll's own retry runs; the active-map refresh waits its turn."""
    fetch_map, calls = _counting_fetch()
    coordinator = _coordinator(fetch_map)
    coordinator.client.state.map_data = None
    coordinator.client.get_map = AsyncMock()
    _due(coordinator)

    await coordinator._async_update_data()
    await _settle()

    coordinator.client.get_map.assert_awaited_once()
    assert calls == []
