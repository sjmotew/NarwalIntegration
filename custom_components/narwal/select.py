"""Select entities for Narwal vacuum."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .const import MOP_HUMIDITY_LIST, MOP_HUMIDITY_MAP
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Narwal select entities."""
    coordinator = entry.runtime_data
    async_add_entities([NarwalMopHumiditySelect(coordinator)])


class NarwalMopHumiditySelect(NarwalEntity, SelectEntity):
    """Select entity for mop humidity level (dry/normal/wet)."""

    _attr_translation_key = "mop_humidity"
    _attr_options = MOP_HUMIDITY_LIST

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        """Initialize the mop humidity select."""
        super().__init__(coordinator)
        device_id = coordinator.config_entry.data["device_id"]
        self._attr_unique_id = f"{device_id}_mop_humidity"
        # Like fan speed, the robot doesn't broadcast the current mop humidity
        # setting, so we track the last value set via the integration.
        self._attr_current_option: str | None = None

    async def async_select_option(self, option: str) -> None:
        """Set the mop humidity level."""
        level = MOP_HUMIDITY_MAP.get(option)
        if level is not None:
            await self.coordinator.client.set_mop_humidity(level)
            self._attr_current_option = option
            self.async_write_ha_state()
