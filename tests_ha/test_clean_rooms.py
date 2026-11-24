"""Exercise narwal.clean_rooms room names, areas and the all toggle in real HA."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector
from homeassistant.util.yaml import load_yaml_dict

from custom_components.narwal.const import DOMAIN, SERVICE_CLEAN_ROOMS
from custom_components.narwal.narwal_client import (
    CommandResponse,
    MapData,
    RoomInfo,
)


@pytest.fixture
async def rooms_robot(hass: HomeAssistant, flow_entry, silent_robot):
    flow_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(flow_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = flow_entry.runtime_data
    coordinator.client.state.update_from_base_status({"3": {"1": 10, "10": 1}})
    coordinator.client.state.map_data = MapData(
        map_id=77,
        rooms=[RoomInfo(room_id=4, name="Living room"), RoomInfo(room_id=7, name="Kitchen")],
    )
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, flow_entry.entry_id)
    vacuum = next(entry.entity_id for entry in entries if entry.domain == "vacuum")
    registry.async_update_entity_options(
        vacuum,
        "vacuum",
        {"area_mapping": {"lounge": ["4"], "cooking": ["7"], "both": ["7", "4"]}},
    )
    with (
        patch.object(coordinator, "async_refresh_dock_status", AsyncMock(return_value=True)),
        patch.object(coordinator, "async_prepare_clean_start", AsyncMock(return_value=True)),
        patch.object(coordinator.client, "wake", AsyncMock()),
        patch.object(
            coordinator.client,
            "start_rooms",
            AsyncMock(return_value=CommandResponse(result_code=0)),
        ) as start_rooms,
    ):
        yield coordinator, vacuum, start_rooms


async def _clean(hass: HomeAssistant, vacuum: str, **data) -> None:
    await hass.services.async_call(
        DOMAIN,
        SERVICE_CLEAN_ROOMS,
        data,
        target={"entity_id": vacuum},
        blocking=True,
    )


@pytest.mark.parametrize(
    "rooms, expected",
    [
        (["Kitchen"], [7]),
        (["Kitchen", "Living room"], [7, 4]),
        (["Living room", 7], [4, 7]),
        (["4", 7], [4, 7]),
        (["all"], [4, 7]),
    ],
)
async def test_rooms_accepts_names_ids_and_all(hass, rooms_robot, rooms, expected):
    _coordinator, vacuum, start_rooms = rooms_robot
    await _clean(hass, vacuum, rooms=rooms)
    assert start_rooms.await_args.args[0] == expected


@pytest.mark.parametrize(
    "areas, expected",
    [
        (["cooking"], [7]),
        (["cooking", "lounge"], [7, 4]),
        (["lounge", "cooking"], [4, 7]),
        (["both"], [7, 4]),
        ("lounge", [4]),
    ],
)
async def test_areas_pick_mapped_rooms_in_selected_order(hass, rooms_robot, areas, expected):
    _coordinator, vacuum, start_rooms = rooms_robot
    await _clean(hass, vacuum, areas=areas)
    assert start_rooms.await_args.args[0] == expected


@pytest.mark.parametrize(
    "data",
    [
        {"rooms": ["Kitchen"], "areas": ["lounge"]},
        {"rooms": ["Kitchen"], "all_rooms": True},
        {"areas": ["lounge"], "all_rooms": True},
        {"rooms": ["Kitchen"], "areas": ["lounge"], "all_rooms": True},
    ],
)
async def test_only_one_room_source_is_accepted(hass, rooms_robot, data):
    _coordinator, vacuum, start_rooms = rooms_robot
    with pytest.raises(vol.Invalid, match="only one of Rooms, Areas or All rooms"):
        await _clean(hass, vacuum, **data)
    start_rooms.assert_not_awaited()


@pytest.mark.parametrize("empty", [{"rooms": []}, {"areas": []}, {"all_rooms": False}])
async def test_empty_sources_do_not_count_as_a_second_source(hass, rooms_robot, empty):
    _coordinator, vacuum, start_rooms = rooms_robot
    await _clean(hass, vacuum, areas=["lounge"], **{k: v for k, v in empty.items() if k != "areas"})
    assert start_rooms.await_args.args[0] == [4]


async def test_all_rooms_toggle(hass, rooms_robot):
    _coordinator, vacuum, start_rooms = rooms_robot
    await _clean(hass, vacuum, all_rooms=True)
    assert start_rooms.await_args.args[0] == [4, 7]


async def test_all_rooms_off_needs_another_room_source(hass, rooms_robot):
    _coordinator, vacuum, start_rooms = rooms_robot
    with pytest.raises(HomeAssistantError, match="At least one room"):
        await _clean(hass, vacuum, all_rooms=False, rooms=[])
    start_rooms.assert_not_awaited()


async def test_all_rooms_needs_no_area_mapping(hass, rooms_robot):
    _coordinator, vacuum, start_rooms = rooms_robot
    er.async_get(hass).async_update_entity_options(vacuum, "vacuum", None)
    await _clean(hass, vacuum, all_rooms=True)
    assert start_rooms.await_args.args[0] == [4, 7]


async def test_a_room_source_is_required(hass, rooms_robot):
    _coordinator, vacuum, start_rooms = rooms_robot
    with pytest.raises(vol.Invalid):
        await _clean(hass, vacuum)
    start_rooms.assert_not_awaited()


@pytest.mark.parametrize("rooms", [["Garage"], [99]])
async def test_unknown_rooms_are_rejected(hass, rooms_robot, rooms):
    _coordinator, vacuum, start_rooms = rooms_robot
    with pytest.raises(HomeAssistantError, match="Unknown Narwal room"):
        await _clean(hass, vacuum, rooms=rooms)
    start_rooms.assert_not_awaited()


async def test_unmapped_area_is_skipped_with_a_warning(hass, rooms_robot, caplog):
    _coordinator, vacuum, start_rooms = rooms_robot
    ar.async_get(hass).async_create("Garage")
    await _clean(hass, vacuum, areas=["garage", "lounge"])
    assert start_rooms.await_args.args[0] == [4]
    assert "Area Garage is not mapped to a Narwal room" in caplog.text


async def test_only_unmapped_areas_start_nothing(hass, rooms_robot, caplog):
    _coordinator, vacuum, start_rooms = rooms_robot
    with pytest.raises(HomeAssistantError, match="At least one room"):
        await _clean(hass, vacuum, areas=["garage"])
    start_rooms.assert_not_awaited()
    assert "Area garage is not mapped" in caplog.text


async def test_areas_without_a_mapping_ask_to_configure_it(hass, rooms_robot):
    _coordinator, vacuum, start_rooms = rooms_robot
    er.async_get(hass).async_update_entity_options(vacuum, "vacuum", None)
    with pytest.raises(HomeAssistantError, match="area mapping.*vacuum entity's settings"):
        await _clean(hass, vacuum, areas=["lounge"])
    # Rooms and the all toggle never need the mapping.
    await _clean(hass, vacuum, rooms=["Kitchen"])
    assert start_rooms.await_args.args[0] == [7]


def test_services_yaml_has_valid_area_and_toggle_selectors():
    path = Path(__file__).parents[1] / "custom_components" / DOMAIN / "services.yaml"
    fields = load_yaml_dict(str(path))[SERVICE_CLEAN_ROOMS]["fields"]
    assert "required" not in fields["rooms"]
    assert "default" not in fields["all_rooms"]
    assert fields["all_rooms"]["selector"] == {"constant": {"value": True, "label": "All rooms"}}
    assert selector.selector(fields["all_rooms"]["selector"])(True) is True
    config = fields["areas"]["selector"]
    assert config["area"]["multiple"] is True
    assert config["area"]["reorder"] is True
    try:
        selector.selector(config)
    except vol.Invalid:
        # `reorder` on area selectors needs a newer HA than the pinned test harness.
        selector.selector({"area": {"multiple": True}})
