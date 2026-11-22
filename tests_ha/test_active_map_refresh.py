"""The coordinator's active-map refresh in real Home Assistant (#43).

The stub suite covers the sequencing; this checks what only the real thing
shows: the poll reaches the entities, and a reload ends a running refresh.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from unittest.mock import AsyncMock, PropertyMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.narwal.coordinator import (
    ACTIVE_MAP_POLL_INTERVAL,
    ActiveMapError,
    NarwalCoordinator,
)
from custom_components.narwal.narwal_client import CommandResponse
from custom_components.narwal.narwal_client.const import WorkingStatus
from custom_components.narwal.narwal_client.models import MapData, MapDisplayData, RoomInfo

CLIENT = "custom_components.narwal.coordinator.NarwalClient"

FLOOR_1 = MapData(
    map_id=1, created_at=7, width=50, height=40, rooms=[RoomInfo(room_id=1, name="Kitchen")]
)
FLOOR_2 = MapData(
    map_id=2,
    created_at=9,
    width=60,
    height=50,
    rooms=[RoomInfo(room_id=4, name="Study"), RoomInfo(room_id=5, name="Hall")],
)


async def _docked_on_floor_1(
    hass: HomeAssistant, entry: MockConfigEntry, silent_robot: dict
) -> NarwalCoordinator:
    """Set the entry up with a robot that answers its status poll from the dock."""
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator: NarwalCoordinator = entry.runtime_data
    coordinator.client.state.map_data = FLOOR_1
    coordinator.client.state.working_status = WorkingStatus.DOCKED
    silent_robot["get_status"].side_effect = None
    silent_robot["get_status"].return_value = CommandResponse(data={"2": {"3": {"1": 10}}})
    return coordinator


async def test_the_poll_switches_the_entities_to_the_active_floor(
    hass: HomeAssistant,
    flow_entry: MockConfigEntry,
    silent_robot: dict,
) -> None:
    """Docked on floor 1 with its trail, switched to floor 2 in the app, no client asking.

    The poll finds the switch: floor 1's trail and pose leave memory, the
    stored trail is left alone, the room entities of floor 2 are registered,
    and the robot is never woken for it.
    """
    coordinator = await _docked_on_floor_1(hass, flow_entry, silent_robot)
    state = coordinator.client.state
    state.map_display_data = MapDisplayData.from_broadcast(
        {
            "1": {"1": {"1": 2.0, "2": 2.0}},
            "2": {"1": struct.pack("<2f", 1.0, 2.0), "2": struct.pack("<2f", 1.0, 2.0)},
            "10": 100,
        }
    )
    # The path a display_map broadcast takes, which ties the trail to floor 1.
    coordinator._on_state_update(state)
    await hass.async_block_till_done()
    assert state.map_display_data is not None and state.map_display_data.has_trajectory
    registry = er.async_get(hass)

    def room_entities() -> set[str]:
        return {
            registry_entry.entity_id
            for registry_entry in er.async_entries_for_config_entry(registry, flow_entry.entry_id)
            if registry_entry.domain == "select" and registry_entry.entity_id.endswith("_mode")
        }

    on_floor_1 = room_entities()

    async def fetch_map() -> MapData:
        # The answer takes its time, as over the network: the poll has returned
        # and told the entities about floor 1 again before floor 2 is in.
        await asyncio.sleep(0)
        return FLOOR_2

    silent_robot["fetch_map"].side_effect = fetch_map
    silent_robot["get_map"].reset_mock()
    store_writes = AsyncMock()

    with (
        patch(f"{CLIENT}.wake", new=AsyncMock()) as wake,
        # The status poll gives up early on a client that never connected.
        patch(f"{CLIENT}.connected", new_callable=PropertyMock, return_value=True),
        patch.object(coordinator, "_async_write_map_display_cache", store_writes),
    ):
        # Not due yet: an ordinary poll sends no get_map.
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        silent_robot["fetch_map"].assert_not_awaited()

        coordinator._active_map_checked_at -= ACTIVE_MAP_POLL_INTERVAL
        await coordinator.async_refresh()
        # The poll did not wait for the map.
        assert state.map_data is FLOOR_1
        refresh = coordinator._active_map_task
        assert refresh is not None
        await refresh
        await hass.async_block_till_done()

    silent_robot["fetch_map"].assert_awaited_once()
    silent_robot["get_map"].assert_not_awaited()
    wake.assert_not_awaited()
    assert state.map_data is FLOOR_2
    assert state.map_display_data is None
    # Looking at floor 2 is no clean: the store is neither emptied nor given
    # anything but floor 1's own trail, whose save was still queued.
    assert [written.args[0].get("map_id") for written in store_writes.await_args_list] in (
        [],
        [1],
    )
    # The room entities of floor 2 are created when the refresh tells the entities.
    assert len(room_entities() - on_floor_1) == 2


async def test_a_polled_refresh_that_fails_at_once_is_over_with_the_poll(
    hass: HomeAssistant,
    flow_entry: MockConfigEntry,
    silent_robot: dict,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Home Assistant starts the refresh eagerly: a get_map that fails without
    waiting is done before the poll returns, and nothing is left hanging."""
    coordinator = await _docked_on_floor_1(hass, flow_entry, silent_robot)
    silent_robot["fetch_map"].side_effect = TimeoutError("robot asleep")
    coordinator._active_map_checked_at -= ACTIVE_MAP_POLL_INTERVAL

    with (
        patch(f"{CLIENT}.connected", new_callable=PropertyMock, return_value=True),
        caplog.at_level(logging.DEBUG, logger="custom_components.narwal.coordinator"),
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

        silent_robot["fetch_map"].assert_awaited_once()
        assert coordinator.client.state.map_data is FLOOR_1
        assert coordinator.last_update_success
        assert "Active map refresh failed: robot asleep" in caplog.text
        # Not asked again by the next poll, and no refresh is left behind.
        await coordinator.async_refresh()
        await hass.async_block_till_done()
    silent_robot["fetch_map"].assert_awaited_once()
    assert coordinator._running_active_map_refresh() is None

    # A caller is backed off by it: on the dock its get_map would be the same.
    with pytest.raises(ActiveMapError, match="robot asleep"):
        await asyncio.wait_for(coordinator.async_refresh_active_map(), timeout=5)
    silent_robot["fetch_map"].assert_awaited_once()


async def test_a_reload_ends_a_running_refresh(
    hass: HomeAssistant,
    flow_entry: MockConfigEntry,
    silent_robot: dict,
) -> None:
    """Reloaded while get_map is out: the old entry applies nothing, the new one works."""
    coordinator = await _docked_on_floor_1(hass, flow_entry, silent_robot)
    asked = asyncio.Event()
    answer = asyncio.Event()

    async def fetch_map() -> MapData:
        asked.set()
        await answer.wait()
        return FLOOR_2

    silent_robot["fetch_map"].side_effect = fetch_map
    waiting = hass.async_create_task(coordinator.async_refresh_active_map())
    await asked.wait()

    assert await hass.config_entries.async_reload(flow_entry.entry_id)
    await hass.async_block_till_done()

    with pytest.raises(ActiveMapError, match="unloaded"):
        await waiting
    # The answer arriving after the unload changes nothing on the old coordinator.
    answer.set()
    await hass.async_block_till_done()
    assert coordinator.client.state.map_data is FLOOR_1
    with pytest.raises(ActiveMapError, match="unloaded"):
        await coordinator.async_refresh_active_map()

    assert flow_entry.state is ConfigEntryState.LOADED
    reloaded: NarwalCoordinator = flow_entry.runtime_data
    assert reloaded is not coordinator
    reloaded.client.state.working_status = WorkingStatus.DOCKED
    refresh = await reloaded.async_refresh_active_map()
    assert (refresh.held, refresh.map_data) == (False, FLOOR_2)


async def test_home_assistant_stopping_ends_a_running_refresh(
    hass: HomeAssistant,
    flow_entry: MockConfigEntry,
    silent_robot: dict,
) -> None:
    """A stop unloads nothing; Home Assistant cancels its tasks.

    The refresh is one of them: it applies nothing and is not left running. The
    caller is cancelled by the stop as well, or told that the refresh is gone.
    """
    coordinator = await _docked_on_floor_1(hass, flow_entry, silent_robot)
    asked = asyncio.Event()

    async def fetch_map() -> MapData:
        asked.set()
        await asyncio.Event().wait()
        return FLOOR_2

    silent_robot["fetch_map"].side_effect = fetch_map
    waiting = hass.async_create_task(coordinator.async_refresh_active_map())
    await asked.wait()

    await hass.async_stop()

    await asyncio.wait([waiting])
    assert waiting.cancelled() or isinstance(waiting.exception(), ActiveMapError)
    assert coordinator.client.state.map_data is FLOOR_1
    assert coordinator._running_active_map_refresh() is None
