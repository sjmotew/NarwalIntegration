"""Exercise pending profiles through genuine HA schemas, services and scripts."""

from copy import deepcopy
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.script import Script
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.narwal.const import DOMAIN, SERVICE_SET_PENDING_PROFILES
from custom_components.narwal.narwal_client import MapData, RoomCleanSettings, RoomInfo


@pytest.fixture
async def pending_robot(
    hass: HomeAssistant, flow_entry: MockConfigEntry, silent_robot: dict, request
):
    flow_entry.add_to_hass(hass)
    if hasattr(request, "param"):
        hass.config_entries.async_update_entry(
            flow_entry, data=dict(flow_entry.data, product_key=request.param)
        )
    assert await hass.config_entries.async_setup(flow_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = flow_entry.runtime_data
    assert coordinator.profiles_loaded
    coordinator.client.state.update_from_base_status({"3": {"1": 10, "10": 1}})
    coordinator.client.state.map_data = MapData(
        map_id=77,
        rooms=[RoomInfo(room_id=4, name="Living room"), RoomInfo(room_id=7, name="Kitchen")],
    )
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, flow_entry.entry_id)
    vacuum = next(entry.entity_id for entry in entries if entry.domain == "vacuum")
    assert not any("room_id" in state.attributes for state in hass.states.async_all())
    for mock in silent_robot.values():
        mock.reset_mock()
    with patch.object(coordinator.client, "send_command", new_callable=AsyncMock) as send:
        yield coordinator, vacuum, send
        send.assert_not_awaited()
        silent_robot["get_map"].assert_not_awaited()
        silent_robot["get_status"].assert_not_awaited()


def _data(vacuum):
    return {
        "entity_id": vacuum,
        "global_profile": {
            "mode": "vacuum_and_mop",
            "suction": "standard",
            "water": "normal",
            "mop_strength": "normal",
            "passes": 1,
            "route": "meticulous",
        },
        "room_profiles": {
            "Living room": {"mode": "vacuum", "suction": "ai", "passes": 2, "route": "standard"},
        },
    }


@pytest.mark.parametrize("selection", [None, [], ["7"]])
async def test_pending_profiles_script_without_room_entities(hass, pending_robot, selection):
    coordinator, vacuum, _send = pending_robot
    coordinator.selected_clean_rooms["77"] = {4}
    coordinator.room_clean_settings[("77", 7)] = RoomCleanSettings(passes=3)
    coordinator.room_clean_settings_customized[("77", 7)] = {"passes"}
    coordinator.last_update_success = False
    data = _data(vacuum)
    data.pop("entity_id")
    if selection is not None:
        data["selected_rooms"] = selection
    script = Script(
        hass,
        [
            {
                "action": f"{DOMAIN}.{SERVICE_SET_PENDING_PROFILES}",
                "target": {"entity_id": vacuum},
                "data": data,
            }
        ],
        "Pending profiles",
        DOMAIN,
    )
    await script.async_run()
    await hass.async_block_till_done()
    assert coordinator.clean_settings.passes == 1
    assert coordinator.room_clean_settings[("77", 4)].passes == 2
    assert coordinator.room_clean_settings[("77", 7)].passes == 3
    expected = {4} if selection is None else ({7} if selection else None)
    assert coordinator.selected_clean_rooms.get("77") == expected
    assert coordinator.last_update_success is False
    assert hass.states.get(vacuum).state == "unavailable"


@pytest.mark.parametrize(
    "field, value",
    [
        ("mode", "Vacuum and mop"),
        ("mode", "invalid"),
        ("suction", "Strong"),
        ("suction", "invalid"),
        ("water", "Wet"),
        ("water", "invalid"),
        ("mop_strength", "High"),
        ("mop_strength", "invalid"),
        ("route", "Meticulous"),
        ("route", "invalid"),
        ("passes", "2"),
        ("passes", 2.0),
        ("passes", 1.5),
        ("passes", True),
        ("passes", 0),
        ("passes", 4),
        ("scrub", "high"),
    ],
)
@pytest.mark.parametrize("scope", ["global_profile", "room_profiles"])
async def test_pending_service_schema_rejects_bad_values_atomically(
    hass, pending_robot, field, value, scope
):
    coordinator, vacuum, _send = pending_robot
    data = _data(vacuum)
    profile = deepcopy(data["global_profile"])
    profile[field] = value
    if scope == "global_profile":
        data[scope] = profile
    else:
        data[scope] = {4: data["global_profile"], 7: profile}
    before = coordinator.clean_settings
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(DOMAIN, SERVICE_SET_PENDING_PROFILES, data, blocking=True)
    assert coordinator.clean_settings is before
    assert coordinator.room_clean_settings == {}
    assert coordinator.selected_clean_rooms == {}


@pytest.mark.parametrize("field", ["mode", "passes", "route"])
async def test_pending_service_schema_requires_complete_profile(hass, pending_robot, field):
    coordinator, vacuum, _send = pending_robot
    data = _data(vacuum)
    data["room_profiles"]["Living room"].pop(field)
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(DOMAIN, SERVICE_SET_PENDING_PROFILES, data, blocking=True)
    assert coordinator.room_clean_settings == {}


@pytest.mark.parametrize("reference", [True, 4.0, 0, ""])
async def test_pending_service_schema_rejects_bad_room_reference(hass, pending_robot, reference):
    coordinator, vacuum, _send = pending_robot
    data = _data(vacuum)
    data["selected_rooms"] = [reference]
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(DOMAIN, SERVICE_SET_PENDING_PROFILES, data, blocking=True)
    assert coordinator.room_clean_settings == {}


@pytest.mark.parametrize("pending_robot", ["qV6BujoYLz"], indirect=True)
async def test_later_model_unsupported_profile_leaves_all_state_unchanged(hass, pending_robot):
    coordinator, vacuum, _send = pending_robot
    data = _data(vacuum)
    data["room_profiles"]["Kitchen"] = dict(data["global_profile"], suction="ultra_powerful")
    before = coordinator.clean_settings
    with pytest.raises(HomeAssistantError, match="not supported"):
        await hass.services.async_call(DOMAIN, SERVICE_SET_PENDING_PROFILES, data, blocking=True)
    assert coordinator.clean_settings is before
    assert coordinator.room_clean_settings == {}
