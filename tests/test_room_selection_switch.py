"""Tests for Narwal room-selection switches."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import tests.ha_stubs  # noqa: E402

tests.ha_stubs.install()

from homeassistant.components.switch import SwitchEntity  # noqa: E402
from homeassistant.exceptions import HomeAssistantError  # noqa: E402

from custom_components.narwal.coordinator import NarwalCoordinator  # noqa: E402
from custom_components.narwal.switch import (  # noqa: E402
    NarwalRoomSelectionSwitch,
    async_setup_entry,
)
from narwal_client import NarwalState  # noqa: E402
from narwal_client.const import WorkingStatus  # noqa: E402
from narwal_client.models import MapData, RoomInfo  # noqa: E402


def _state(
    working_status: WorkingStatus = WorkingStatus.DOCKED,
) -> NarwalState:
    """Return a Narwal state with one room map."""
    state = NarwalState(working_status=working_status)
    state.map_data = MapData(
        map_id=100,
        rooms=[RoomInfo(room_id=4, name="Kitchen")],
    )
    return state


def _coordinator(state: NarwalState | None = None) -> NarwalCoordinator:
    """Create a coordinator stub with real room-selection methods."""
    state = state or _state()
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.data = {"device_id": "dev1"}
    coordinator.config_entry.title = "Narwal Test"
    coordinator.client = MagicMock()
    coordinator.client.state = state
    coordinator.client.state.firmware_version = "1.0.0"
    coordinator.data = state
    coordinator.last_update_success = True
    coordinator.selected_clean_rooms = {}
    coordinator.async_update_listeners = MagicMock()
    coordinator.async_add_listener = MagicMock()
    return coordinator


def test_room_selection_switch_bases() -> None:
    """Room selection switches expose coordinator-owned state."""
    assert issubclass(NarwalRoomSelectionSwitch, SwitchEntity)
    assert NarwalRoomSelectionSwitch._attr_entity_registry_visible_default is False


async def test_room_selection_switch_updates_selected_rooms() -> None:
    """Turning the switch on/off mutates the coordinator selection state."""
    coordinator = _coordinator()
    switch = NarwalRoomSelectionSwitch(coordinator, 4, "Kitchen", map_id="100")

    assert not switch.is_on

    await switch.async_turn_on()

    assert switch.is_on
    coordinator.async_update_listeners.assert_called_once()
    assert coordinator.selected_clean_room_ids_for([4, 5], map_id="100") == [4]

    await switch.async_turn_off()

    assert not switch.is_on
    assert coordinator.selected_clean_room_ids_for([4, 5], map_id="100") == [4, 5]
    assert coordinator.async_update_listeners.call_count == 2


def test_room_selection_switch_unavailable_during_active_clean() -> None:
    """Room selection is locked while clean setup cannot be edited."""
    coordinator = _coordinator(_state(WorkingStatus.CLEANING))
    switch = NarwalRoomSelectionSwitch(coordinator, 4, "Kitchen", map_id="100")

    assert not switch.available


@pytest.mark.parametrize("turn_on", [True, False])
async def test_room_selection_service_rejects_unavailable_switch(turn_on: bool) -> None:
    """Direct service calls cannot bypass the room-selection availability guard."""
    coordinator = _coordinator(_state(WorkingStatus.CLEANING))
    coordinator.selected_clean_rooms = {"100": {4}}
    switch = NarwalRoomSelectionSwitch(coordinator, 4, "Kitchen", map_id="100")

    with pytest.raises(HomeAssistantError, match="cannot be changed"):
        if turn_on:
            await switch.async_turn_on()
        else:
            await switch.async_turn_off()

    assert coordinator.selected_clean_rooms == {"100": {4}}
    coordinator.async_update_listeners.assert_not_called()


async def test_room_selection_service_rejects_stale_map() -> None:
    """A switch from a prior map cannot persist selection into that stale map."""
    coordinator = _coordinator()
    switch = NarwalRoomSelectionSwitch(coordinator, 4, "Kitchen", map_id="old")

    with pytest.raises(HomeAssistantError, match="cannot be changed"):
        await switch.async_turn_on()

    assert coordinator.selected_clean_rooms == {}


async def test_removed_selected_room_can_be_deselected() -> None:
    """A disappeared room cannot leave native starts permanently blocked."""
    state = _state()
    state.map_data.rooms = [RoomInfo(room_id=5, name="Hall")]
    coordinator = _coordinator(state)
    coordinator.selected_clean_rooms = {"100": {4}}
    switch = NarwalRoomSelectionSwitch(coordinator, 4, "Kitchen", map_id="100")

    assert switch.available
    assert switch.is_on

    await switch.async_turn_off()

    assert coordinator.selected_clean_rooms == {}


async def test_selected_switch_from_another_map_cannot_be_changed() -> None:
    """Orphan recovery cannot mutate a selection belonging to a stale map."""
    coordinator = _coordinator()
    coordinator.selected_clean_rooms = {"old": {4}}
    switch = NarwalRoomSelectionSwitch(coordinator, 4, "Kitchen", map_id="old")

    assert not switch.available
    with pytest.raises(HomeAssistantError, match="cannot be changed"):
        await switch.async_turn_off()

    assert coordinator.selected_clean_rooms == {"old": {4}}


def test_room_selection_switch_uses_cached_map_while_coordinator_unavailable() -> None:
    """A connectivity outage cannot lock a persisted next-clean selection."""
    coordinator = _coordinator()
    coordinator.last_update_success = False
    switch = NarwalRoomSelectionSwitch(coordinator, 4, "Kitchen", map_id="100")

    assert switch.available


async def test_room_selection_entities_update_name_after_map_rename() -> None:
    """Dynamic room selection switches follow map room renames."""
    coordinator = _coordinator()
    entry = MagicMock()
    entry.runtime_data = coordinator
    added_entities = []
    listeners = []

    def add_entities(entities) -> None:
        added_entities.extend(list(entities))

    coordinator.async_add_listener.side_effect = lambda listener: listeners.append(
        listener
    )

    await async_setup_entry(MagicMock(), entry, add_entities)

    room_switch = next(
        entity
        for entity in added_entities
        if isinstance(entity, NarwalRoomSelectionSwitch)
    )
    assert room_switch._attr_name == "Kitchen selected"
    assert room_switch.extra_state_attributes["room_name"] == "Kitchen"

    coordinator.client.state.map_data = MapData(
        map_id=100,
        rooms=[RoomInfo(room_id=4, name="Pantry")],
    )
    listeners[0]()

    assert room_switch._attr_name == "Pantry selected"
    assert room_switch.extra_state_attributes["room_name"] == "Pantry"


async def test_setup_exposes_same_map_orphan_for_deselection() -> None:
    """A removed selected room remains recoverable after integration restart."""
    state = _state()
    state.map_data.rooms = [RoomInfo(room_id=5, name="Hall")]
    coordinator = _coordinator(state)
    coordinator.selected_clean_rooms = {"100": {4}}
    entry = MagicMock()
    entry.runtime_data = coordinator
    added_entities = []

    await async_setup_entry(
        MagicMock(),
        entry,
        lambda entities: added_entities.extend(list(entities)),
    )

    orphan = next(
        entity
        for entity in added_entities
        if isinstance(entity, NarwalRoomSelectionSwitch) and entity._room_id == 4
    )
    assert orphan.available
    assert orphan.is_on

    await orphan.async_turn_off()

    assert coordinator.selected_clean_rooms == {}
