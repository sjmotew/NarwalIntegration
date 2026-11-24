"""Current-task readback, independent from next-clean settings."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import tests.ha_stubs

tests.ha_stubs.install()

from custom_components.narwal.coordinator import CleanSettings, NarwalCoordinator
from custom_components.narwal.narwal_client import (
    CommandResponse,
    FanLevel,
    NarwalClient,
    NarwalState,
    WorkMode,
)
from custom_components.narwal.narwal_client.models import MapData, RoomInfo


def task_response():
    return CommandResponse(
        result_code=1,
        data={
            "1": 1,
            "2": {
                "1": 1,
                "2": {
                    "1": {"1": 1, "2": 3},
                    "2": {
                        "1": 5,
                        "2": 5,
                        "3": 2,
                        "4": 3,
                        "5": 2,
                        "6": 2,
                        "8": 2,
                    },
                    "3": 1,
                },
                "3": {},
                "5": 3,
            },
        },
    )


def coordinator():
    c = NarwalCoordinator.__new__(NarwalCoordinator)
    state = NarwalState()
    state.update_from_base_status({"3": {"1": 4, "7": 7}, "11": 1, "47": 2})
    state.map_data = MapData(map_id=1, rooms=[RoomInfo(room_id=3)])
    c.client = SimpleNamespace(
        state=state, get_current_task=AsyncMock(return_value=task_response())
    )
    c.active_clean_work_mode = None
    c.active_room_clean_settings = {}
    c.active_clean_setting_overrides = {}
    c.notifications = []
    c.async_set_updated_data = c.notifications.append
    c.clean_settings = CleanSettings(work_mode=WorkMode.MOP)
    return c


def test_captured_legacy_room_task_decodes_wire_modes():
    map_id, rooms = NarwalClient.room_settings_from_current_task(task_response())
    assert map_id == 1
    assert rooms[3].work_mode == WorkMode.VACUUM_THEN_MOP
    assert rooms[3].fan == FanLevel.SUPER
    assert rooms[3].passes == 2
    assert int(rooms[3].route) == 2


@pytest.mark.parametrize("change", ["mode", "passes", "zone", "duplicate", "rejected"])
def test_unknown_or_unrepresentable_task_stays_unknown(change):
    response = task_response()
    item = response.data["2"]["2"]
    if change == "mode":
        item["2"]["1"] = 99
    elif change == "passes":
        item["2"]["6"] = 1
    elif change == "zone":
        item["1"]["1"] = 2
    elif change == "duplicate":
        response.data["2"]["2"] = [item, deepcopy(item)]
    else:
        response.result_code = 7
    assert NarwalClient.room_settings_from_current_task(response) is None


@pytest.mark.asyncio
async def test_reconnect_uses_actual_task_and_leaves_pending_profile_alone():
    c = coordinator()
    await c._async_recover_active_clean_context()
    assert c.active_clean_work_mode == WorkMode.VACUUM_THEN_MOP
    assert c.active_room_clean_settings[3].fan == FanLevel.SUPER
    assert c.clean_settings.work_mode == WorkMode.MOP
    assert c.notifications == [c.client.state]
    await c._async_recover_active_clean_context()
    c.client.get_current_task.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["map", "unknown_room", "ended", "superseded"])
async def test_changed_task_cannot_adopt_delayed_readback(change):
    c = coordinator()

    async def reply():
        if change == "map":
            c.client.state.map_data.map_id = 2
        elif change == "unknown_room":
            c.client.state.map_data.rooms = [RoomInfo(room_id=9)]
        else:
            c.client.state.update_from_base_status({"3": {"1": 1}, "11": 3, "47": 1})
            c._sync_active_clean_context(c.client.state)
            if change == "superseded":
                c.client.state.update_from_base_status({"3": {"1": 4, "7": 7}, "11": 1, "47": 2})
                c._sync_active_clean_context(c.client.state)
        return task_response()

    c.client.get_current_task.side_effect = reply
    await c._async_recover_active_clean_context()
    assert not c.active_room_clean_settings


@pytest.mark.asyncio
async def test_failed_query_has_a_bounded_retry_budget(monkeypatch):
    c = coordinator()
    c.client.get_current_task.side_effect = TimeoutError
    clock = [100.0]
    monkeypatch.setattr("custom_components.narwal.coordinator.time.monotonic", lambda: clock[0])
    for _ in range(8):
        await c._async_recover_active_clean_context()
        await c._async_recover_active_clean_context()
        clock[0] += 61
    assert c.client.get_current_task.await_count == 3
    assert not c.active_room_clean_settings


@pytest.mark.parametrize("missing", ["2", "3", "4"])
def test_missing_applicable_settings_are_not_reported_as_actual(missing):
    response = task_response()
    del response.data["2"]["2"]["2"][missing]
    assert NarwalClient.room_settings_from_current_task(response) is None


@pytest.mark.asyncio
async def test_successful_live_change_invalidates_the_in_flight_readback():
    c = coordinator()

    async def reply():
        c.set_active_clean_setting("fan", FanLevel.NORMAL)
        return task_response()

    c.client.get_current_task.side_effect = reply
    await c._async_recover_active_clean_context()
    assert not c.active_room_clean_settings
    assert c.active_clean_setting_overrides["fan"] == FanLevel.NORMAL


@pytest.mark.asyncio
async def test_push_schedules_one_recovery_worker_without_polling():
    import asyncio

    c = coordinator()
    c.hass = object()
    scheduled = []

    def spawn(hass, work, name):
        task = asyncio.create_task(work)
        scheduled.append(task)
        return task

    c.config_entry = SimpleNamespace(async_create_background_task=spawn)
    c._schedule_active_task_recovery(c.client.state)
    c._schedule_active_task_recovery(c.client.state)
    await c._task_recovery_task
    assert len(scheduled) == 1
    assert c.active_clean_work_mode == WorkMode.VACUUM_THEN_MOP


@pytest.mark.asyncio
async def test_worker_retries_when_polling_is_suppressed(monkeypatch):
    import asyncio

    c = coordinator()
    c.client.get_current_task.side_effect = TimeoutError
    clock = [100.0]
    sleeps = []

    async def advance(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr("custom_components.narwal.coordinator.time.monotonic", lambda: clock[0])
    c._wait_for_task_recovery_retry = advance
    await c._recover_active_task()
    assert c.client.get_current_task.await_count == 3
    assert sleeps == [60, 60]


@pytest.mark.asyncio
async def test_startup_tracks_transitions_before_task_readback():
    import asyncio
    from unittest.mock import MagicMock

    hass, entry = MagicMock(), MagicMock()
    entry.data = {
        "host": "10.0.0.1",
        "port": 9002,
        "device_id": "test",
        "product_key": "QoEsI5qYXO",
    }
    c = NarwalCoordinator(hass, entry)
    c.config_entry = entry
    c.hass = hass
    c.client.state = coordinator().client.state
    for method in (
        "connect",
        "get_device_info",
        "get_map",
        "get_consumable_info",
        "subscribe_to_topics",
        "disconnect",
    ):
        setattr(c.client, method, AsyncMock())
    c.client.get_status = AsyncMock(
        return_value=CommandResponse(result_code=1, data={"3": {"1": 4}})
    )
    for method in (
        "_async_restore_room_selections",
        "_async_restore_room_orders",
        "_async_restore_map_display_cache",
    ):
        setattr(c, method, AsyncMock())
    c.async_set_updated_data = MagicMock()
    listening, queried = asyncio.Event(), asyncio.Event()
    owned = []

    async def listen():
        listening.set()
        await asyncio.Event().wait()

    c.client.start_listening = listen

    def spawn(hass, work, name):
        task = asyncio.create_task(work)
        owned.append(task)
        return task

    entry.async_create_background_task = spawn
    hass.async_create_task = lambda work: spawn(hass, work, "test")

    async def read_task():
        if queried.is_set():
            await asyncio.Event().wait()
        assert listening.is_set()
        assert c.client.on_state_update == c._on_state_update
        c.client.state.update_from_base_status({"3": {"1": 1}, "11": 3, "47": 1})
        c.client.on_state_update(c.client.state)
        c.client.state.update_from_base_status({"3": {"1": 4, "7": 7}, "11": 1, "47": 2})
        c.client.on_state_update(c.client.state)
        queried.set()
        return task_response()

    c.client.get_current_task = read_task
    try:
        await c.async_setup()
        await asyncio.wait_for(queried.wait(), 1)
        await asyncio.sleep(0)
        assert not c.active_room_clean_settings
    finally:
        for task in owned:
            task.cancel()
        await asyncio.gather(*owned, return_exceptions=True)


@pytest.mark.asyncio
async def test_new_clean_wakes_a_sleeping_recovery_worker():
    import asyncio

    c = coordinator()
    c._task_context_was_active = False
    c._task_recovery_attempts = 3
    c._task_recovery_next_at = 999999999.0
    waiting = asyncio.create_task(c._wait_for_task_recovery_retry(60))
    await asyncio.sleep(0)
    c._sync_active_clean_context(c.client.state)
    await asyncio.wait_for(waiting, 0.2)
    assert c._task_recovery_attempts == 0
    assert c._task_recovery_next_at == 0
