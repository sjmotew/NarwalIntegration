"""Pending reset requires positive dock presence, not one status code."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import tests.ha_stubs

tests.ha_stubs.install()

from homeassistant.exceptions import HomeAssistantError  # noqa: E402

from custom_components.narwal.coordinator import can_apply_pending_profiles  # noqa: E402
from narwal_client.const import WorkingStatus  # noqa: E402
from narwal_client.models import NarwalState, RoomInfo  # noqa: E402
from tests.test_services import (  # noqa: E402
    _coordinator,
    _pending_profiles_call,
    _register_pending_profiles_handler,
)


def _standby_docked():
    state = NarwalState()
    state.update_from_base_status({"3": {"1": 1, "3": 6}, "11": 2, "47": 3})
    return state


def test_standby_with_observed_dock_fields_is_eligible():
    state = _standby_docked()
    assert state.working_status == WorkingStatus.STANDBY
    assert state.is_docked
    assert can_apply_pending_profiles(state)


@pytest.mark.parametrize("kind", ["off_dock", "paused", "returning", "cleaning", "unknown"])
def test_non_idle_or_unconfirmed_dock_is_rejected(kind):
    state = _standby_docked()
    if kind == "off_dock":
        state = NarwalState(working_status=WorkingStatus.STANDBY)
    elif kind == "paused":
        state.is_paused = True
    elif kind == "returning":
        state.is_returning_to_dock = True
    elif kind == "cleaning":
        state.working_status = WorkingStatus.CLEANING
    else:
        state.working_status = WorkingStatus.UNKNOWN
    assert not can_apply_pending_profiles(state)


async def test_exact_standby_reset_stays_local_and_updates_selection():
    coordinator = _coordinator()
    state = _standby_docked()
    state.map_data = coordinator.data.map_data
    coordinator.data = coordinator.client.state = state
    coordinator.selected_clean_rooms["77"] = {4}
    handler, registry = _register_pending_profiles_handler(coordinator)
    with (
        patch(
            "custom_components.narwal.service.async_extract_entity_ids",
            new_callable=AsyncMock,
            return_value=["vacuum.downstairs_narwal_vacuum"],
        ),
        patch("custom_components.narwal.er.async_get", return_value=registry),
    ):
        await handler(_pending_profiles_call())
    assert coordinator.selected_clean_rooms.get("77") is None
    coordinator.client.start_rooms.assert_not_awaited()
    coordinator.client.get_map.assert_not_awaited()


@pytest.mark.parametrize("change", ["clean", "map"])
async def test_state_change_between_validation_and_commit_changes_nothing(change):
    coordinator = _coordinator()
    original = coordinator.set_pending_profile_transaction
    before = coordinator.clean_settings
    before_selection = dict(coordinator.selected_clean_rooms)

    def begin_clean(transaction):
        if change == "clean":
            coordinator.data.working_status = WorkingStatus.CLEANING
        else:
            coordinator.data.map_data.map_id = 999
        original(transaction)

    coordinator.set_pending_profile_transaction = begin_clean
    handler, registry = _register_pending_profiles_handler(coordinator)
    with (
        patch(
            "custom_components.narwal.service.async_extract_entity_ids",
            new_callable=AsyncMock,
            return_value=["vacuum.downstairs_narwal_vacuum"],
        ),
        patch("custom_components.narwal.er.async_get", return_value=registry),
        pytest.raises(HomeAssistantError, match="idle docked|current map"),
    ):
        await handler(_pending_profiles_call())
    assert coordinator.clean_settings is before
    assert coordinator.selected_clean_rooms == before_selection
    coordinator.client.start_rooms.assert_not_awaited()


async def test_reset_waits_for_in_flight_start_then_rejects_active_state():
    coordinator = _coordinator()
    before = coordinator.clean_settings
    handler, registry = _register_pending_profiles_handler(coordinator)
    await coordinator.dock_action_lock.acquire()
    with (
        patch(
            "custom_components.narwal.service.async_extract_entity_ids",
            new_callable=AsyncMock,
            return_value=["vacuum.downstairs_narwal_vacuum"],
        ),
        patch("custom_components.narwal.er.async_get", return_value=registry),
    ):
        reset = asyncio.create_task(handler(_pending_profiles_call()))
        await asyncio.sleep(0)
        assert not reset.done()
        assert coordinator.clean_settings is before
        coordinator.data.working_status = WorkingStatus.CLEANING
        coordinator.dock_action_lock.release()
        with pytest.raises(HomeAssistantError, match="idle docked|current map"):
            await reset
    assert coordinator.clean_settings is before
    coordinator.client.start_rooms.assert_not_awaited()


async def test_start_after_reset_reads_committed_profile():
    coordinator = _coordinator()
    handler, registry = _register_pending_profiles_handler(coordinator)
    with (
        patch(
            "custom_components.narwal.service.async_extract_entity_ids",
            new_callable=AsyncMock,
            return_value=["vacuum.downstairs_narwal_vacuum"],
        ),
        patch("custom_components.narwal.er.async_get", return_value=registry),
    ):
        await handler(_pending_profiles_call())
    async with coordinator.dock_action_lock:
        assert coordinator.clean_settings.passes == 2
        assert coordinator.selected_clean_rooms.get("77") is None
    coordinator.client.start_rooms.assert_not_awaited()


async def test_reset_rejects_room_added_while_waiting_for_commit():
    coordinator = _coordinator()
    coordinator.selected_clean_rooms["77"] = {4}
    before = coordinator.clean_settings
    profiles = coordinator.room_clean_settings
    selections = coordinator.selected_clean_rooms
    coordinator.async_update_listeners = MagicMock()
    handler, registry = _register_pending_profiles_handler(coordinator)
    await coordinator.dock_action_lock.acquire()
    with (
        patch(
            "custom_components.narwal.service.async_extract_entity_ids",
            new_callable=AsyncMock,
            return_value=["vacuum.downstairs_narwal_vacuum"],
        ),
        patch("custom_components.narwal.er.async_get", return_value=registry),
    ):
        reset = asyncio.create_task(handler(_pending_profiles_call()))
        await asyncio.sleep(0)
        assert not reset.done()
        coordinator.data.map_data.rooms.append(RoomInfo(room_id=9))
        coordinator.dock_action_lock.release()
        with pytest.raises(HomeAssistantError, match="current map"):
            await reset

    assert coordinator.clean_settings is before
    assert coordinator.room_clean_settings is profiles
    assert coordinator.selected_clean_rooms is selections
    assert coordinator.selected_clean_rooms["77"] == {4}
    coordinator.config_entry.async_create_background_task.assert_not_called()
    coordinator.async_update_listeners.assert_not_called()
    coordinator.async_set_updated_data.assert_not_called()
    coordinator.client.start_rooms.assert_not_awaited()


@pytest.mark.parametrize("last_update_success", [False, True])
async def test_pending_reset_notifies_without_changing_update_success(last_update_success):
    coordinator = _coordinator()
    coordinator.last_update_success = last_update_success
    coordinator.async_set_updated_data.side_effect = lambda _state: setattr(
        coordinator, "last_update_success", True
    )
    observed = []
    coordinator.async_update_listeners = MagicMock(
        side_effect=lambda: observed.append(
            (coordinator.last_update_success, coordinator.clean_settings.passes)
        )
    )
    handler, registry = _register_pending_profiles_handler(coordinator)
    with (
        patch(
            "custom_components.narwal.service.async_extract_entity_ids",
            new_callable=AsyncMock,
            return_value=["vacuum.downstairs_narwal_vacuum"],
        ),
        patch("custom_components.narwal.er.async_get", return_value=registry),
    ):
        await handler(_pending_profiles_call())

    assert coordinator.last_update_success is last_update_success
    assert observed == [(last_update_success, 2)]
    coordinator.async_update_listeners.assert_called_once_with()
    coordinator.async_set_updated_data.assert_not_called()


def _parse(coordinator, **overrides):
    data = _pending_profiles_call().data
    data.update(overrides)
    return coordinator.parse_pending_profile_transaction(
        global_profile=data["global_profile"],
        room_profiles=data["room_profiles"],
        selected_rooms=data.get("selected_rooms"),
    )


@pytest.mark.parametrize("selected_rooms, expected", [(None, {7}), ([], None), (["7"], {7})])
async def test_partial_profiles_preserve_omitted_rooms_and_explicit_selection_contract(
    selected_rooms, expected
):
    coordinator = _coordinator()
    coordinator.set_room_clean_setting(7, "passes", 3, map_id="77")
    omitted = coordinator.room_clean_settings[("77", 7)]
    coordinator.selected_clean_rooms["77"] = {7}
    coordinator.room_clean_orders["77"] = [7, 4]
    coordinator.config_entry.async_create_background_task.reset_mock()
    data = _pending_profiles_call().data
    transaction = _parse(
        coordinator, room_profiles={4: data["room_profiles"][4]}, selected_rooms=selected_rooms
    )
    async with coordinator.dock_action_lock:
        coordinator.set_pending_profile_transaction(transaction)
    assert coordinator.room_clean_settings[("77", 7)] is omitted
    assert coordinator.room_clean_settings_customized[("77", 7)] == {"passes"}
    assert omitted.passes == 3
    assert coordinator.selected_clean_rooms.get("77") == expected
    assert coordinator.room_clean_orders["77"] == [7, 4]
    assert coordinator.room_clean_settings[("77", 4)].passes == 2
    coordinator.config_entry.async_create_background_task.assert_called_once()
    coordinator.client.assert_not_called()
    assert coordinator.client.method_calls == []


async def test_empty_profiles_preserve_rooms_and_selection_as_of_commit():
    coordinator = _coordinator()
    coordinator.selected_clean_rooms["77"] = {4}
    transaction = _parse(coordinator, room_profiles={}, selected_rooms=None)
    coordinator.selected_clean_rooms["77"] = {7}
    async with coordinator.dock_action_lock:
        coordinator.set_pending_profile_transaction(transaction)
    assert coordinator.room_clean_settings == {}
    assert coordinator.selected_clean_rooms["77"] == {7}


@pytest.mark.parametrize("reference", [4, "4", "Living room"])
def test_room_ids_and_names_resolve_without_entities(reference):
    coordinator = _coordinator()
    coordinator.data.map_data.rooms[0].name = "Living room"
    raw = _pending_profiles_call().data["room_profiles"][4]
    transaction = _parse(coordinator, room_profiles={reference: raw}, selected_rooms=[reference])
    assert set(transaction.room_profiles) == {4}
    assert transaction.selected_room_ids == {4}
    coordinator.client.get_map.assert_not_awaited()


@pytest.mark.parametrize(
    "profiles, selected, error",
    [
        ([4, "4"], [], "Duplicate"),
        ([4, "Living room"], [], "Duplicate"),
        ([99], [], "Unknown"),
        (["living room"], [], "Unknown"),
        (["Room"], [], "Ambiguous"),
        ([4], [4, "4"], "Duplicate"),
        ([4], [99], "Unknown"),
        ([True], [], "invalid"),
        ([4.0], [], "invalid"),
    ],
)
def test_bad_room_references_are_atomic(profiles, selected, error):
    coordinator = _coordinator()
    if profiles != ["Room"]:
        coordinator.data.map_data.rooms[0].name = "Living room"
    before = coordinator.clean_settings
    raw = _pending_profiles_call().data["global_profile"]
    with pytest.raises(ValueError, match=error):
        _parse(coordinator, room_profiles=dict.fromkeys(profiles, raw), selected_rooms=selected)
    assert coordinator.clean_settings is before
    assert coordinator.room_clean_settings == {}
    assert coordinator.selected_clean_rooms == {}
    coordinator.config_entry.async_create_background_task.assert_not_called()
    coordinator.async_update_listeners.assert_not_called()


@pytest.mark.parametrize("store", ["_room_selection_store_loaded", "_room_profile_store_loaded"])
@pytest.mark.parametrize("late", [False, True])
async def test_profiles_loaded_gate_rechecked_at_commit(store, late):
    coordinator = _coordinator()
    assert coordinator.profiles_loaded
    transaction = _parse(coordinator) if late else None
    setattr(coordinator, store, False)
    assert not coordinator.profiles_loaded
    async with coordinator.dock_action_lock:
        with pytest.raises(ValueError, match="loaded idle docked"):
            if late:
                coordinator.set_pending_profile_transaction(transaction)
            else:
                _parse(coordinator)
    assert coordinator.room_clean_settings == {}
    coordinator.async_update_listeners.assert_not_called()


@pytest.mark.parametrize("change", ["add", "remove_omitted", "rename_omitted", "map"])
async def test_partial_request_rechecks_whole_topology_after_lock_wait(change):
    coordinator = _coordinator()
    before = coordinator.clean_settings
    profiles = coordinator.room_clean_settings
    selections = coordinator.selected_clean_rooms
    raw = _pending_profiles_call().data["room_profiles"][4]
    transaction = _parse(coordinator, room_profiles={4: raw}, selected_rooms=None)
    async with coordinator.dock_action_lock:
        if change == "add":
            coordinator.data.map_data.rooms.append(RoomInfo(room_id=9))
        elif change == "remove_omitted":
            coordinator.data.map_data.rooms.pop()
        elif change == "rename_omitted":
            coordinator.data.map_data.rooms[-1].name = "New name"
        else:
            coordinator.data.map_data.map_id = 88
        with pytest.raises(ValueError, match="current map changed"):
            coordinator.set_pending_profile_transaction(transaction)
    assert coordinator.clean_settings is before
    assert coordinator.room_clean_settings is profiles
    assert coordinator.selected_clean_rooms is selections
    coordinator.config_entry.async_create_background_task.assert_not_called()
    coordinator.async_update_listeners.assert_not_called()


@pytest.mark.parametrize(
    "profile, error",
    [
        ({"mode": "vacuum", "passes": 1, "route": "standard"}, "requires suction"),
        ({"mode": "mop", "passes": 1, "route": "standard"}, "requires water"),
        ({"mode": "mop", "suction": "ai", "passes": 1, "route": "standard"}, "mop-only"),
        (
            {"mode": "vacuum", "suction": "ai", "water": "wet", "passes": 1, "route": "standard"},
            "vacuum-only",
        ),
    ],
)
def test_mode_inapplicable_or_incomplete_profiles_reject_everything(profile, error):
    coordinator = _coordinator()
    with pytest.raises(ValueError, match=error):
        _parse(coordinator, global_profile=profile)
    assert coordinator.room_clean_settings == {}
    coordinator.config_entry.async_create_background_task.assert_not_called()


@pytest.mark.parametrize(
    "suction",
    [
        "ai",
        "standard",
        "normal",
        "super",
        "ultra",
        "max",
        "quiet",
        "strong",
        "super_powerful",
        "ultra_powerful",
    ],
)
@pytest.mark.parametrize("product_key", ["QoEsI5qYXO", "qV6BujoYLz"])
def test_pending_suction_has_same_model_mapping_as_clean_rooms(suction, product_key):
    from custom_components.narwal import _suction_for_coordinator

    coordinator = _coordinator(product_key=product_key)
    raw = dict(_pending_profiles_call().data["global_profile"], suction=suction)
    try:
        expected = _suction_for_coordinator(coordinator, suction)
    except HomeAssistantError:
        with pytest.raises(ValueError, match="not supported"):
            _parse(coordinator, global_profile=raw, room_profiles={})
    else:
        transaction = _parse(coordinator, global_profile=raw, room_profiles={})
        assert transaction.global_profile[0].fan == expected


@pytest.mark.parametrize("selection", [None, [], [4]])
async def test_unresolved_startup_defaults_cannot_overwrite_committed_profiles(selection):
    from custom_components.narwal.narwal_client import RoomCleanSettings

    coordinator = _coordinator()
    coordinator.selected_clean_rooms[None] = {7}
    coordinator._room_selection_dirty_maps.add(None)
    coordinator.room_clean_settings[(None, 4)] = RoomCleanSettings(passes=3)
    coordinator.room_clean_settings_customized[(None, 4)] = {"passes"}
    coordinator._room_profile_pending_resolution.add(4)
    coordinator.room_clean_settings[(None, 7)] = RoomCleanSettings(passes=3)
    coordinator.room_clean_settings_customized[(None, 7)] = {"passes"}
    coordinator._room_profile_pending_resolution.add(7)
    raw = _pending_profiles_call().data["room_profiles"][4]
    transaction = _parse(coordinator, room_profiles={4: raw}, selected_rooms=selection)
    # Parsing has no side effects, including migration of startup defaults.
    assert coordinator.selected_clean_rooms[None] == {7}
    async with coordinator.dock_action_lock:
        coordinator.set_pending_profile_transaction(transaction)
    assert coordinator.effective_room_clean_settings_for(4).passes == 2
    assert coordinator.effective_room_clean_settings_for(7).passes == 3
    expected = {7} if selection is None else (set(selection) if selection else None)
    assert coordinator.selected_clean_rooms.get("77") == expected
    assert None not in coordinator.selected_clean_rooms
    assert None not in coordinator._room_selection_dirty_maps
    assert not coordinator._room_profile_pending_resolution


async def test_inapplicable_global_fields_preserve_latest_values_at_commit():
    from custom_components.narwal.narwal_client import MopHumidity

    coordinator = _coordinator()
    profile = {"mode": "vacuum", "suction": "ai", "passes": 1, "route": "standard"}
    transaction = _parse(coordinator, global_profile=profile, room_profiles={})
    coordinator.clean_settings.water = MopHumidity.WET
    async with coordinator.dock_action_lock:
        coordinator.set_pending_profile_transaction(transaction)
    assert coordinator.clean_settings.water == MopHumidity.WET
