"""Number entities for Narwal pass count and persisted room cleaning order.

Holds a pending value applied at the next room clean; the builder routes it to the right
CleanParam tag for the current mode (sweep->5, mop->6, sweep_then_mop->5+6, sync->7).
"""

from __future__ import annotations

import voluptuous as vol
from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import slugify

from . import NarwalConfigEntry, _validate_pass_count
from .const import PASSES_MAX, PASSES_MIN
from .coordinator import NarwalCoordinator, can_edit_pending_clean_settings
from .entity import NarwalEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Narwal global passes and per-room cleaning-order entities."""
    coordinator = entry.runtime_data
    known_room_orders: dict[tuple[str | None, int], NarwalRoomOrderNumber] = {}

    @callback
    def async_add_room_order_entities() -> None:
        map_data = coordinator.client.state.map_data
        if map_data is None:
            return
        map_id = coordinator.room_settings_map_id(map_data)
        entities: list[NarwalRoomOrderNumber] = []
        for room in sorted(map_data.rooms, key=lambda item: item.display_name.lower()):
            if room.room_id <= 0:
                continue
            key = (map_id, room.room_id)
            if key in known_room_orders:
                known_room_orders[key].async_update_room_name(room.display_name)
                continue
            entity = NarwalRoomOrderNumber(
                coordinator,
                room.room_id,
                room.display_name,
                map_id=map_id,
            )
            known_room_orders[key] = entity
            entities.append(entity)
        if entities:
            async_add_entities(entities)

    async_add_entities([NarwalPassesNumber(coordinator)])
    async_add_room_order_entities()
    entry.async_on_unload(coordinator.async_add_listener(async_add_room_order_entities))


class NarwalPassesNumber(NarwalEntity, RestoreNumber):
    """Pending clean pass count, applied at the next room clean; restored across restarts."""

    _attr_translation_key = "passes"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = PASSES_MIN
    _attr_native_max_value = PASSES_MAX
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        """Initialize the number entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.data['device_id']}_passes"

    async def async_added_to_hass(self) -> None:
        """Restore the last pass count into clean_settings (persists across restarts)."""
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            self.coordinator.clean_settings.passes = int(last.native_value)

    @property
    def available(self) -> bool:
        """Return True when the pass count can be changed now."""
        return (
            can_edit_pending_clean_settings(self.coordinator.data)
            and not self.coordinator.has_selected_clean_rooms()
        )

    @property
    def native_value(self) -> float:
        """Return the stored pass count."""
        return self.coordinator.clean_settings.passes

    async def async_set_native_value(self, value: float) -> None:
        """Store the pass count."""
        if (
            not can_edit_pending_clean_settings(self.coordinator.data)
            or self.coordinator.has_selected_clean_rooms()
        ):
            raise HomeAssistantError("Narwal pass count cannot be changed right now")
        try:
            passes = _validate_pass_count(value)
        except vol.Invalid as err:
            raise HomeAssistantError(str(err)) from err
        self.coordinator.clean_settings.passes = passes
        self.async_write_ha_state()
        self.coordinator.async_update_listeners()


class NarwalRoomOrderNumber(NarwalEntity, RestoreNumber):
    """One-based room position for the next native vacuum start."""

    _attr_entity_registry_enabled_default = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:sort-numeric-ascending"
    _attr_native_min_value = 1
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        coordinator: NarwalCoordinator,
        room_id: int,
        room_name: str,
        *,
        map_id: str | None = None,
    ) -> None:
        """Initialize a room cleaning-order number."""
        super().__init__(coordinator)
        self._map_id = map_id
        self._room_id = room_id
        self._room_name = room_name
        device_id = coordinator.config_entry.data["device_id"]
        map_prefix = f"map_{slugify(map_id)}_" if map_id is not None else ""
        self._attr_unique_id = f"{device_id}_{map_prefix}room_{room_id}_clean_order"
        self._attr_name = f"{room_name} cleaning order"

    @callback
    def async_update_room_name(self, room_name: str) -> None:
        """Update display metadata when the map renames this room."""
        if room_name == self._room_name:
            return
        self._room_name = room_name
        self._attr_name = f"{room_name} cleaning order"
        if getattr(self, "hass", None) is not None:
            self.async_write_ha_state()

    def _current_room_ids(self) -> list[int]:
        """Return positive room IDs from this entity's active map."""
        map_data = getattr(self.coordinator.client.state, "map_data", None)
        if map_data is None:
            return []
        if self.coordinator.room_settings_map_id(map_data) != self._map_id:
            return []
        return [room.room_id for room in map_data.rooms if room.room_id > 0]

    @property
    def native_max_value(self) -> float:
        """Return the current room count as the highest valid position."""
        return max(1, len(self._current_room_ids()))

    @property
    def native_value(self) -> float | None:
        """Return this room's current one-based cleaning position."""
        return self.coordinator.room_clean_order_for(
            self._room_id,
            self._current_room_ids(),
            map_id=self._map_id,
        )

    @property
    def extra_state_attributes(self) -> dict[str, str | int]:
        """Return stable room metadata for dashboards and automations."""
        return {
            "room_id": self._room_id,
            "room_name": self._room_name,
            "map_id": self._map_id or "",
        }

    @property
    def available(self) -> bool:
        """Return True when the current room order can be edited."""
        return (
            getattr(self.coordinator, "_room_order_store_loaded", True)
            and self._map_id is not None
            and self._room_id in self._current_room_ids()
            and can_edit_pending_clean_settings(self.coordinator.data)
        )

    async def async_set_native_value(self, value: float) -> None:
        """Move this room and persist the resulting complete order."""
        if not self.available:
            raise HomeAssistantError("Narwal room cleaning order cannot be changed right now")
        if not float(value).is_integer():
            raise HomeAssistantError("Narwal room cleaning order must be a whole number")
        try:
            self.coordinator.set_room_clean_order(
                self._room_id,
                int(value),
                self._current_room_ids(),
                map_id=self._map_id,
            )
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        self.async_write_ha_state()
