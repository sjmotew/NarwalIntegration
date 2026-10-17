"""Tests for persisted Narwal room cleaning order."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import tests.ha_stubs  # noqa: E402

tests.ha_stubs.install()

from homeassistant.components.number import RestoreNumber  # noqa: E402
from homeassistant.exceptions import HomeAssistantError  # noqa: E402

from custom_components.narwal.coordinator import NarwalCoordinator  # noqa: E402
from custom_components.narwal.number import (  # noqa: E402
    NarwalPassesNumber,
    NarwalRoomOrderNumber,
    async_setup_entry,
)
from narwal_client import NarwalState  # noqa: E402
from narwal_client.const import WorkingStatus  # noqa: E402
from narwal_client.models import MapData, RoomInfo  # noqa: E402


class _Store:
    """Minimal HA Store test double."""

    def __init__(self, data: object | None = None) -> None:
        self.data = data

    async def async_load(self) -> object | None:
        """Return stored data."""
        return self.data

    async def async_save(self, data: object) -> None:
        """Save data."""
        self.data = data


def _coordinator() -> NarwalCoordinator:
    """Return a coordinator with a current three-room map."""
    state = NarwalState(working_status=WorkingStatus.DOCKED)
    state.map_data = MapData(
        map_id=100,
        rooms=[
            RoomInfo(room_id=4, name="Kitchen"),
            RoomInfo(room_id=5, name="Hallway"),
            RoomInfo(room_id=6, name="Lounge"),
        ],
    )
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.data = {"device_id": "dev1"}
    coordinator.client = MagicMock()
    coordinator.client.state = state
    coordinator.data = state
    coordinator.last_update_success = True
    coordinator.selected_clean_rooms = {}
    coordinator.room_clean_orders = {}
    coordinator._room_selection_store_loaded = True
    coordinator._room_order_store_loaded = True
    coordinator._schedule_room_order_save = MagicMock()
    coordinator.async_update_listeners = MagicMock()
    coordinator.async_add_listener = MagicMock()
    return coordinator


def test_moving_room_shifts_the_complete_order() -> None:
    """Setting one position cannot leave duplicate room positions."""
    coordinator = _coordinator()

    coordinator.set_room_clean_order(6, 1, [4, 5, 6], map_id="100")
    coordinator.set_room_clean_order(4, 2, [4, 5, 6], map_id="100")

    assert coordinator.room_clean_orders == {"100": [6, 4, 5]}
    assert coordinator.selected_clean_room_ids_for([4, 5, 6], map_id="100") == [6, 4, 5]
    assert coordinator._schedule_room_order_save.call_count == 2


def test_selected_rooms_follow_persisted_order() -> None:
    """Normal Start filters selections without losing configured order."""
    coordinator = _coordinator()
    coordinator.room_clean_orders = {"100": [6, 4, 5]}
    coordinator.selected_clean_rooms = {"100": {4, 5}}

    assert coordinator.selected_clean_room_ids_for([4, 5, 6], map_id="100") == [4, 5]


async def test_room_order_survives_restart() -> None:
    """The complete order is saved and restored independently of entities."""
    store = _Store()
    before = _coordinator()
    before.room_clean_orders = {"100": [6, 4, 5]}
    before._room_order_store = store
    before._room_order_save_lock = asyncio.Lock()

    await before._async_save_room_orders()

    after = _coordinator()
    after.room_clean_orders = {}
    after._room_order_store = store
    after._room_order_save_lock = asyncio.Lock()
    after._room_order_store_loaded = False
    await after._async_restore_room_orders()

    assert after.room_clean_orders == {"100": [6, 4, 5]}
    assert after.selected_clean_room_ids_for([4, 5, 6], map_id="100") == [6, 4, 5]


async def test_invalid_room_order_is_dropped_without_blocking_valid_maps() -> None:
    """Corrupt entries recover to defaults while valid map orders survive."""
    store = _Store(
        {
            "maps": [
                {"map_id": "100", "room_ids": [4, 4]},
                {"map_id": "200", "room_ids": [8, 7]},
            ]
        }
    )
    coordinator = _coordinator()
    coordinator.room_clean_orders = {}
    coordinator._room_order_store = store
    coordinator._room_order_save_lock = asyncio.Lock()
    coordinator._room_order_store_loaded = False

    await coordinator._async_restore_room_orders()
    await coordinator._async_save_room_orders()

    assert coordinator._room_order_store_loaded
    assert coordinator.room_clean_orders == {"200": [8, 7]}
    assert coordinator.selected_clean_room_ids_for([4, 5, 6], map_id="100") == [
        4,
        5,
        6,
    ]
    assert store.data == {"maps": [{"map_id": "200", "room_ids": [8, 7]}]}


async def test_room_order_read_failure_falls_back_without_overwrite() -> None:
    """A failed read keeps Start usable and preserves storage for retry."""
    coordinator = _coordinator()
    coordinator.room_clean_orders = {}
    coordinator._room_order_store = MagicMock()
    coordinator._room_order_store.async_load = AsyncMock(side_effect=OSError)
    coordinator._room_order_store.async_save = AsyncMock()
    coordinator._room_order_save_lock = asyncio.Lock()
    coordinator._room_order_store_loaded = False

    await coordinator._async_restore_room_orders()
    await coordinator._async_save_room_orders()

    assert not coordinator._room_order_store_loaded
    assert coordinator.selected_clean_room_ids_for([4, 5, 6], map_id="100") == [
        4,
        5,
        6,
    ]
    coordinator._room_order_store.async_save.assert_not_awaited()


async def test_room_order_requires_identified_map() -> None:
    """An order cannot be edited before it can be scoped to a stable map."""
    coordinator = _coordinator()
    coordinator.client.state.map_data.map_id = 0
    number = NarwalRoomOrderNumber(coordinator, 5, "Hallway", map_id=None)

    assert not number.available
    with pytest.raises(HomeAssistantError, match="cannot be changed right now"):
        await number.async_set_native_value(1.0)
    with pytest.raises(ValueError, match="map is not available"):
        coordinator.set_room_clean_order(5, 1, [4, 5, 6], map_id=None)
    assert coordinator.room_clean_orders == {}


async def test_room_order_number_moves_room_and_updates_name() -> None:
    """The opt-in number presents and edits one room's persisted position."""
    coordinator = _coordinator()
    number = NarwalRoomOrderNumber(coordinator, 5, "Hallway", map_id="100")

    assert issubclass(NarwalRoomOrderNumber, RestoreNumber)
    assert NarwalRoomOrderNumber._attr_entity_registry_enabled_default is False
    assert number.native_value == 2
    assert number.native_max_value == 3
    assert number.available
    assert number.extra_state_attributes == {
        "room_id": 5,
        "room_name": "Hallway",
        "map_id": "100",
    }

    await number.async_set_native_value(1.0)
    assert coordinator.room_clean_orders == {"100": [5, 4, 6]}

    number.async_update_room_name("Landing")
    assert number._attr_name == "Landing cleaning order"


async def test_room_order_number_rejects_invalid_position() -> None:
    """The entity rejects fractional and out-of-range positions."""
    number = NarwalRoomOrderNumber(_coordinator(), 5, "Hallway", map_id="100")

    with pytest.raises(HomeAssistantError, match="whole number"):
        await number.async_set_native_value(1.5)
    with pytest.raises(HomeAssistantError, match="must be 1-3"):
        await number.async_set_native_value(4.0)


async def test_room_order_number_rejects_write_during_clean() -> None:
    """A direct number service call cannot bypass entity availability."""
    coordinator = _coordinator()
    coordinator.data.working_status = WorkingStatus.CLEANING
    number = NarwalRoomOrderNumber(coordinator, 5, "Hallway", map_id="100")

    assert not number.available
    with pytest.raises(HomeAssistantError, match="cannot be changed right now"):
        await number.async_set_native_value(1.0)
    assert coordinator.room_clean_orders == {}


async def test_number_setup_adds_one_order_entity_per_room() -> None:
    """Number setup adds global passes plus one opt-in order per map room."""
    coordinator = _coordinator()
    entry = MagicMock()
    entry.runtime_data = coordinator
    added: list[object] = []

    await async_setup_entry(MagicMock(), entry, lambda entities: added.extend(entities))

    assert sum(isinstance(entity, NarwalPassesNumber) for entity in added) == 1
    assert sum(isinstance(entity, NarwalRoomOrderNumber) for entity in added) == 3
