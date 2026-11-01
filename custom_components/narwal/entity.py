"""Base entity for Narwal vacuum integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, configured_model_name
from .coordinator import NarwalCoordinator


def narwal_robot_device_info(coordinator: NarwalCoordinator) -> DeviceInfo:
    """Return device info for the robot vacuum."""
    device_id = coordinator.config_entry.data["device_id"]
    return DeviceInfo(
        identifiers={(DOMAIN, device_id)},
        manufacturer=MANUFACTURER,
        model=configured_model_name(coordinator.config_entry.data),
        sw_version=coordinator.client.state.firmware_version or None,
        name=coordinator.config_entry.title,
    )


def narwal_dock_device_info(coordinator: NarwalCoordinator) -> DeviceInfo:
    """Return device info for the robot dock/base station."""
    device_id = coordinator.config_entry.data["device_id"]
    return DeviceInfo(
        identifiers={(DOMAIN, f"{device_id}_dock")},
        manufacturer=MANUFACTURER,
        model=f"{configured_model_name(coordinator.config_entry.data)} Dock",
        sw_version=coordinator.client.state.firmware_version or None,
        translation_key="dock",
        translation_placeholders={"name": coordinator.config_entry.title},
        via_device=(DOMAIN, device_id),
    )


class NarwalEntity(CoordinatorEntity[NarwalCoordinator]):
    """Base class for Narwal entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._attr_device_info = narwal_robot_device_info(coordinator)

    @property
    def available(self) -> bool:
        """Return True if the entity is available."""
        return self.coordinator.last_update_success


class NarwalDockEntity(NarwalEntity):
    """Base class for Narwal dock/base-station entities."""

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        """Initialize the dock entity."""
        super().__init__(coordinator)
        self._attr_device_info = narwal_dock_device_info(coordinator)
