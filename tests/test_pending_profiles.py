"""Pending reset requires positive dock presence, not one status code."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import tests.ha_stubs

tests.ha_stubs.install()

from homeassistant.exceptions import HomeAssistantError

from custom_components.narwal.coordinator import can_apply_pending_profiles
from narwal_client.const import WorkingStatus
from narwal_client.models import NarwalState, RoomInfo
from tests.test_services import (
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

    def begin_clean(**kwargs):
        if change == "clean":
            coordinator.data.working_status = WorkingStatus.CLEANING
        else:
            coordinator.data.map_data.map_id = 999
        original(**kwargs)

    coordinator.set_pending_profile_transaction = begin_clean
    handler, registry = _register_pending_profiles_handler(coordinator)
    with (
        patch(
            "custom_components.narwal.service.async_extract_entity_ids",
            new_callable=AsyncMock,
            return_value=["vacuum.downstairs_narwal_vacuum"],
        ),
        patch("custom_components.narwal.er.async_get", return_value=registry),
        pytest.raises(HomeAssistantError, match="idle docked"),
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
        patch("custom_components.narwal.service.async_extract_entity_ids", new_callable=AsyncMock,
              return_value=["vacuum.downstairs_narwal_vacuum"]),
        patch("custom_components.narwal.er.async_get", return_value=registry),
    ):
        reset = asyncio.create_task(handler(_pending_profiles_call()))
        await asyncio.sleep(0)
        assert not reset.done()
        assert coordinator.clean_settings is before
        coordinator.data.working_status = WorkingStatus.CLEANING
        coordinator.dock_action_lock.release()
        with pytest.raises(HomeAssistantError, match="idle docked"):
            await reset
    assert coordinator.clean_settings is before
    coordinator.client.start_rooms.assert_not_awaited()


async def test_start_after_reset_reads_committed_profile():
    coordinator = _coordinator()
    handler, registry = _register_pending_profiles_handler(coordinator)
    with (
        patch("custom_components.narwal.service.async_extract_entity_ids", new_callable=AsyncMock,
              return_value=["vacuum.downstairs_narwal_vacuum"]),
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
