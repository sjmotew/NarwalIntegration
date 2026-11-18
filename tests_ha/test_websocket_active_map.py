"""The narwal/active_map command, through Home Assistant's real websocket API.

The stub suite calls the handlers directly; this checks that HA accepts the
registration, validates the schema and delivers the answer to a client.
"""

from __future__ import annotations

import struct
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.narwal.const import CONF_DEVICE_ID
from custom_components.narwal.narwal_client.const import WorkingStatus
from custom_components.narwal.narwal_client.models import MapData, MapDisplayData, RoomInfo

_WAKE = "custom_components.narwal.coordinator.NarwalClient.wake"


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_active_map_over_the_websocket(
    hass: HomeAssistant,
    hass_ws_client,
    flow_entry: MockConfigEntry,
    silent_robot: dict,
) -> None:
    """A client gets the robot's active map in the documented shape."""
    await _setup(hass, flow_entry)
    silent_robot["get_map"].reset_mock()
    silent_robot["get_map"].return_value = MapData(
        map_id=2,
        created_at=42,
        width=100,
        height=80,
        rooms=[RoomInfo(room_id=1, name="Kitchen"), RoomInfo(room_id=0)],
    )

    client = await hass_ws_client(hass)
    with patch(_WAKE, new=AsyncMock()) as wake:
        await client.send_json_auto_id({"type": "narwal/active_map"})
        msg = await client.receive_json()

    assert msg["success"], msg
    assert msg["result"] == {
        "version": 1,
        "entry_id": flow_entry.entry_id,
        "held": False,
        "map": {"id": 2, "created_at": 42, "width": 100, "height": 80},
        "rooms": [{"id": 1, "name": "Kitchen"}],
    }
    silent_robot["get_map"].assert_awaited_once()
    # The robot never broadcast in this test, so it counts as asleep.
    wake.assert_awaited_once()


async def test_active_map_errors(
    hass: HomeAssistant,
    hass_ws_client,
    flow_entry: MockConfigEntry,
    silent_robot: dict,
) -> None:
    """A robot that does not answer is map_failed; schema and entry are checked."""
    await _setup(hass, flow_entry)
    silent_robot["get_map"].side_effect = TimeoutError("robot asleep")
    client = await hass_ws_client(hass)

    with patch(_WAKE, new=AsyncMock()):
        await client.send_json_auto_id({"type": "narwal/active_map"})
        msg = await client.receive_json()
    assert not msg["success"]
    assert msg["error"]["code"] == "map_failed"

    await client.send_json_auto_id({"type": "narwal/active_map", "bogus": 1})
    msg = await client.receive_json()
    assert msg["error"]["code"] == "invalid_format"

    await client.send_json_auto_id({"type": "narwal/active_map", "entry_id": "missing"})
    msg = await client.receive_json()
    assert msg["error"]["code"] == "not_found"


async def test_active_map_by_entity_and_held_during_a_clean(
    hass: HomeAssistant,
    hass_ws_client,
    flow_entry: MockConfigEntry,
    silent_robot: dict,
) -> None:
    """A card names the vacuum; mid-clean the robot is not asked."""
    await _setup(hass, flow_entry)
    state = flow_entry.runtime_data.client.state
    state.map_data = MapData(map_id=1, created_at=7, width=50, height=40)
    state.working_status = WorkingStatus.CLEANING
    silent_robot["get_map"].reset_mock()
    entity_id = next(
        entry.entity_id
        for entry in er.async_entries_for_config_entry(er.async_get(hass), flow_entry.entry_id)
        if entry.domain == "vacuum"
    )
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": "narwal/active_map", "entity_id": entity_id})
    msg = await client.receive_json()

    assert msg["success"], msg
    assert msg["result"]["held"] is True
    assert msg["result"]["map"] == {"id": 1, "created_at": 7, "width": 50, "height": 40}
    silent_robot["get_map"].assert_not_awaited()

    await client.send_json_auto_id(
        {"type": "narwal/active_map", "entity_id": entity_id, "entry_id": flow_entry.entry_id}
    )
    msg = await client.receive_json()
    assert msg["error"]["code"] == "invalid_format"


def _vacuum_entity_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    return next(
        registry_entry.entity_id
        for registry_entry in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if registry_entry.domain == "vacuum"
    )


async def test_active_map_floor_switch_through_the_coordinator(
    hass: HomeAssistant,
    hass_ws_client,
    flow_entry: MockConfigEntry,
    silent_robot: dict,
) -> None:
    """Docked on floor 1 with its trail, switched to floor 2 in the app.

    The real coordinator drops floor 1's trail and pose, and the room entities
    of floor 2 are registered at once, without waiting for a poll.
    """
    await _setup(hass, flow_entry)
    coordinator = flow_entry.runtime_data
    state = coordinator.client.state
    state.map_data = MapData(
        map_id=1, created_at=7, width=50, height=40, rooms=[RoomInfo(room_id=1, name="Kitchen")]
    )
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

    async def get_map() -> MapData:
        state.map_data = MapData(
            map_id=2,
            created_at=9,
            width=60,
            height=50,
            rooms=[RoomInfo(room_id=4, name="Study"), RoomInfo(room_id=5, name="Hall")],
        )
        return state.map_data

    silent_robot["get_map"].side_effect = get_map
    client = await hass_ws_client(hass)
    with patch(_WAKE, new=AsyncMock()):
        await client.send_json_auto_id({"type": "narwal/active_map"})
        msg = await client.receive_json()
    await hass.async_block_till_done()

    assert msg["success"], msg
    assert msg["result"]["map"]["id"] == 2
    assert state.map_display_data is None
    # The room entities of floor 2 are created by the notify, not by a later poll.
    assert len(room_entities() - on_floor_1) == 2


async def test_active_map_with_two_robots_needs_a_target(
    hass: HomeAssistant,
    hass_ws_client,
    flow_entry: MockConfigEntry,
    silent_robot: dict,
) -> None:
    """Two configured robots: no answer for "the first one", the vacuum picks one."""
    await _setup(hass, flow_entry)
    second = MockConfigEntry(
        domain=flow_entry.domain,
        title="Narwal Flow 2",
        unique_id="fedcba9876543210fedcba9876543210",
        data={
            **flow_entry.data,
            "host": "192.0.2.11",
            CONF_DEVICE_ID: "fedcba9876543210fedcba9876543210",
        },
        options=dict(flow_entry.options),
        version=2,
    )
    await _setup(hass, second)
    silent_robot["get_map"].reset_mock()
    silent_robot["get_map"].return_value = MapData(map_id=3, created_at=1, width=10, height=10)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": "narwal/active_map"})
    msg = await client.receive_json()
    assert msg["error"]["code"] == "target_required"
    silent_robot["get_map"].assert_not_awaited()

    with patch(_WAKE, new=AsyncMock()):
        await client.send_json_auto_id(
            {"type": "narwal/active_map", "entity_id": _vacuum_entity_id(hass, second)}
        )
        msg = await client.receive_json()
    assert msg["success"], msg
    assert msg["result"]["entry_id"] == second.entry_id


async def test_active_map_needs_control_permission(
    hass: HomeAssistant,
    hass_ws_client,
    hass_read_only_access_token: str,
    flow_entry: MockConfigEntry,
    silent_robot: dict,
) -> None:
    """A read-only user cannot wake the robot or replace its map."""
    await _setup(hass, flow_entry)
    silent_robot["get_map"].reset_mock()
    client = await hass_ws_client(hass, hass_read_only_access_token)

    await client.send_json_auto_id({"type": "narwal/active_map"})
    msg = await client.receive_json()

    assert msg["error"]["code"] == "unauthorized"
    silent_robot["get_map"].assert_not_awaited()
