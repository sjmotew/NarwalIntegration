"""Entity and device names resolve through the translation files in real HA.

The camera, dock-device and legacy/per-room select names used to be hardcoded
English. They now come from ``strings.json`` like every other name, so these
tests pin two things the stub suite cannot see: English installs get exactly
the names they had before, and a non-English install gets translated names
while keeping the same (English-derived) entity ids.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.narwal.const import DOMAIN
from custom_components.narwal.narwal_client.models import MapData, RoomInfo

from .conftest import DEVICE_ID

# unique_id suffix -> (entity_id, English name, Korean name)
NAMED_ENTITIES = {
    "map": ("camera.narwal_flow_map", "Map", "지도"),
    "carpet_map": ("camera.narwal_flow_carpet_map", "Carpet map", "카펫 지도"),
    "runtime_suction": ("select.narwal_flow_suction", "Suction", "흡입력"),
    "route": ("select.narwal_flow_route", "Route", "청소 경로"),
}


@pytest.mark.parametrize(
    ("language", "dock_name", "column"),
    [("en", "Narwal Flow Dock", 1), ("ko", "Narwal Flow 도크", 2)],
)
async def test_names_follow_the_configured_language(
    hass: HomeAssistant,
    flow_entry: MockConfigEntry,
    silent_robot: dict,
    language: str,
    dock_name: str,
    column: int,
) -> None:
    """Names are translated; entity ids are not."""
    hass.config.language = language
    flow_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(flow_entry.entry_id)
    await hass.async_block_till_done()

    dock = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, f"{DEVICE_ID}_dock")}
    )
    assert dock is not None
    assert dock.name == dock_name

    registry = er.async_get(hass)
    for suffix, expected in NAMED_ENTITIES.items():
        entity_id = registry.async_get_entity_id(
            expected[0].split(".")[0], DOMAIN, f"{DEVICE_ID}_{suffix}"
        )
        assert entity_id == expected[0]
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.attributes["friendly_name"] == f"Narwal Flow {expected[column]}"


async def test_room_rename_updates_friendly_name(
    hass: HomeAssistant,
    flow_entry: MockConfigEntry,
    silent_robot: dict,
) -> None:
    """Renaming a room in the app renames its per-room entities without a reload.

    ``Entity.name`` is cached and new placeholders alone don't invalidate it, so
    this guards the cache clear in both ``async_update_room_name()`` methods.
    """
    flow_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    # Per-room selects are disabled by default; register one enabled so it has a state.
    select_uid = f"{DEVICE_ID}_room_3_mode"
    registry.async_get_or_create("select", DOMAIN, select_uid, config_entry=flow_entry)
    assert await hass.config_entries.async_setup(flow_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = flow_entry.runtime_data

    async def show_room(name: str) -> None:
        coordinator.client.state.map_data = MapData(rooms=[RoomInfo(room_id=3, name=name)])
        coordinator.async_update_listeners()
        await hass.async_block_till_done()

    def friendly_name(domain: str, unique_id: str) -> str:
        entity_id = registry.async_get_entity_id(domain, DOMAIN, unique_id)
        assert entity_id is not None
        state = hass.states.get(entity_id)
        assert state is not None
        return state.attributes["friendly_name"]

    switch_uid = f"{DEVICE_ID}_room_3_selected"
    await show_room("Kitchen")
    assert friendly_name("switch", switch_uid) == "Narwal Flow Kitchen selected"
    assert friendly_name("select", select_uid) == "Narwal Flow Kitchen mode"

    await show_room("Pantry")
    assert friendly_name("switch", switch_uid) == "Narwal Flow Pantry selected"
    assert friendly_name("select", select_uid) == "Narwal Flow Pantry mode"
