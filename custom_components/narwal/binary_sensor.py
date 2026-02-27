"""Binary sensor entities for Narwal vacuum."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity
from .narwal_client.const import WorkingStatus


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Narwal binary sensor entities."""
    coordinator = entry.runtime_data
    async_add_entities([
        NarwalDockedSensor(coordinator),
        NarwalChargingSensor(coordinator),
    ])


class NarwalDockedSensor(NarwalEntity, BinarySensorEntity):
    """Binary sensor that reports whether the vacuum is on the dock."""

    _attr_translation_key = "docked"

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        """Initialize the docked sensor."""
        super().__init__(coordinator)
        device_id = coordinator.config_entry.data["device_id"]
        self._attr_unique_id = f"{device_id}_docked"

    @property
    def is_on(self) -> bool | None:
        """Return True if the vacuum is on the dock."""
        state = self.coordinator.data
        if state is None:
            return None
        return state.is_docked


class NarwalChargingSensor(NarwalEntity, BinarySensorEntity):
    """Binary sensor: Charging / Not charging, only when docked."""

    _attr_translation_key = "charging"
    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        """Initialize the charging sensor."""
        super().__init__(coordinator)
        device_id = coordinator.config_entry.data["device_id"]
        self._attr_unique_id = f"{device_id}_charging"

    @property
    def is_on(self) -> bool | None:
        """Return True if charging (DOCKED=10), False if charged (CHARGED=14).

        Returns None (unavailable) when not docked.
        """
        state = self.coordinator.data
        if state is None:
            return None
        if not state.is_docked:
            return None
        return state.working_status == WorkingStatus.DOCKED
