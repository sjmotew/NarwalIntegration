"""Number entity for the Narwal clean pass count.

Holds a pending value applied at the next room clean; the builder routes it to the right
CleanParam tag for the current mode (sweep->5, mop->6, sweep_then_mop->5+6, sync->7).
"""

from __future__ import annotations

import voluptuous as vol
from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry, _validate_pass_count
from .const import PASSES_MAX, PASSES_MIN
from .coordinator import NarwalCoordinator, can_edit_pending_clean_settings
from .entity import NarwalEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Narwal passes number entity."""
    async_add_entities([NarwalPassesNumber(entry.runtime_data)])


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
        """Return True when the pass count can be changed now.

        Selecting rooms does not hide this control: the whole-floor pass count
        is the fallback for every selected room without its own override.
        """
        return can_edit_pending_clean_settings(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, list[str]]:
        """Expose which next-clean rooms override the whole-floor pass count."""
        lookup = getattr(self.coordinator, "rooms_overriding_clean_setting", None)
        rooms = lookup("passes") if lookup is not None else []
        return {
            "overridden_by_rooms": list(rooms)
            if isinstance(rooms, (list, tuple))
            else [],
        }

    @property
    def native_value(self) -> float:
        """Return the stored pass count."""
        return self.coordinator.clean_settings.passes

    async def async_set_native_value(self, value: float) -> None:
        """Store the pass count."""
        if not can_edit_pending_clean_settings(self.coordinator.data):
            raise HomeAssistantError("Narwal pass count cannot be changed right now")
        try:
            passes = _validate_pass_count(value)
        except vol.Invalid as err:
            raise HomeAssistantError(str(err)) from err
        self.coordinator.clean_settings.passes = passes
        self.async_write_ha_state()
        self.coordinator.async_update_listeners()
