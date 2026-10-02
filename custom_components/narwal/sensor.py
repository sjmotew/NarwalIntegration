"""Sensor entities for Narwal vacuum."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfArea, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .const import CONF_PRODUCT_KEY, TASK_RESULT_OPTIONS, product_keys_for_model
from .coordinator import NarwalCoordinator
from .entity import NarwalDockEntity, NarwalEntity
from .narwal_client import NarwalState

# Product keys for models without an auto-detergent tank.
_NO_DETERGENT_KEYS = product_keys_for_model("Narwal Flow") | product_keys_for_model("Narwal Flow 2")


@dataclass(frozen=True, kw_only=True)
class NarwalSensorEntityDescription(SensorEntityDescription):
    """Describes a Narwal sensor entity."""

    value_fn: Callable[[NarwalState], float | int | str | None]
    available_fn: Callable[[NarwalState], bool] | None = None
    dock_device: bool = False


def _has_active_cleaning_metrics(state: NarwalState) -> bool:
    """Return true while live clean-progress metrics describe the current task."""
    return (
        state.is_cleaning
        or state.has_recent_active_working_status
        or state.has_paused_clean_task_context
    )


SENSOR_DESCRIPTIONS: tuple[NarwalSensorEntityDescription, ...] = (
    NarwalSensorEntityDescription(
        key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        # battery_level comes from field 2 (real-time SOC as float32)
        value_fn=lambda state: state.battery_level if state.battery_level > 0 else None,
    ),
    NarwalSensorEntityDescription(
        key="cleaning_area",
        translation_key="cleaning_area",
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        state_class=SensorStateClass.MEASUREMENT,
        # working_status field 2 (coveredArea) is already m²; populated only during active cleaning.
        value_fn=lambda state: round(state.cleaning_area, 2)
        if state.cleaning_area > 0
        else None,
        available_fn=_has_active_cleaning_metrics,
    ),
    NarwalSensorEntityDescription(
        key="cleaning_time",
        translation_key="cleaning_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        # working_status field 3 is session elapsed seconds.
        # NEEDS LIVE VALIDATION: only populated during active cleaning.
        value_fn=lambda state: state.cleaning_time
        if state.cleaning_time > 0
        else None,
        available_fn=_has_active_cleaning_metrics,
    ),
    NarwalSensorEntityDescription(
        key="remaining_time",
        translation_key="remaining_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        # base_status task ETA, populated while the robot keeps an active clean task.
        value_fn=lambda state: state.task_remaining_time
        if state.task_remaining_time > 0
        else None,
        available_fn=_has_active_cleaning_metrics,
    ),
    NarwalSensorEntityDescription(
        key="dust_bag_health",
        translation_key="dust_bag_health",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        dock_device=True,
        # base_status field 35 stationBagHealthScore (float32 %); present only with a station.
        value_fn=lambda state: round(state.dust_bag_health, 1)
        if "35" in state.raw_base_status
        else None,
    ),
    NarwalSensorEntityDescription(
        key="detergent_remaining",
        translation_key="detergent_remaining",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        dock_device=True,
        # base_status field 41 heavyDetergentRemainPercent.
        value_fn=lambda state: state.detergent_remaining
        if "41" in state.raw_base_status
        else None,
    ),
    NarwalSensorEntityDescription(
        key="firmware_version",
        translation_key="firmware_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state: state.firmware_version or None,
    ),
    NarwalSensorEntityDescription(
        key="last_clean_result",
        translation_key="last_clean_result",
        device_class=SensorDeviceClass.ENUM,
        entity_category=EntityCategory.DIAGNOSTIC,
        options=list(TASK_RESULT_OPTIONS.values()),
        # base_status field 15 terminateReason (TaskResult) — why the last task ended.
        value_fn=lambda state: TASK_RESULT_OPTIONS.get(state.terminate_reason),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Narwal sensor entities."""
    coordinator = entry.runtime_data
    product_key = entry.data.get(CONF_PRODUCT_KEY, "")
    entities: list[SensorEntity] = [
        (
            NarwalDockSensor(coordinator, description)
            if description.dock_device
            else NarwalSensor(coordinator, description)
        )
        for description in (
            _description_for_model(description, product_key)
            for description in SENSOR_DESCRIPTIONS
        )
    ]
    entities.append(NarwalChargingStateSensor(coordinator))
    async_add_entities(entities)


def _description_for_model(
    description: NarwalSensorEntityDescription, product_key: str
) -> NarwalSensorEntityDescription:
    """Disable sensors by default on models that lack the underlying hardware."""
    if description.key == "detergent_remaining" and product_key in _NO_DETERGENT_KEYS:
        return replace(description, entity_registry_enabled_default=False)
    return description


class NarwalSensor(NarwalEntity, SensorEntity):
    """A Narwal sensor entity."""

    entity_description: NarwalSensorEntityDescription

    def __init__(
        self,
        coordinator: NarwalCoordinator,
        description: NarwalSensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        device_id = coordinator.config_entry.data["device_id"]
        self._attr_unique_id = f"{device_id}_{description.key}"

    @property
    def native_value(self) -> float | int | str | None:
        """Return the sensor value."""
        state = self.coordinator.data
        if state is None:
            return None
        return self.entity_description.value_fn(state)

    @property
    def available(self) -> bool:
        """Return True when this sensor has meaningful current data."""
        if not super().available:
            return False
        if self.entity_description.available_fn is None:
            return True
        state = self.coordinator.data
        return state is not None and self.entity_description.available_fn(state)


class NarwalDockSensor(NarwalDockEntity, SensorEntity):
    """A description-driven Narwal sensor assigned to the dock device."""

    entity_description: NarwalSensorEntityDescription

    def __init__(
        self,
        coordinator: NarwalCoordinator,
        description: NarwalSensorEntityDescription,
    ) -> None:
        """Initialize the dock sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        device_id = coordinator.config_entry.data["device_id"]
        self._attr_unique_id = f"{device_id}_{description.key}"

    @property
    def native_value(self) -> float | int | str | None:
        """Return the sensor value."""
        state = self.coordinator.data
        if state is None:
            return None
        return self.entity_description.value_fn(state)

    @property
    def available(self) -> bool:
        """Return True when this sensor has meaningful current data."""
        if not super().available:
            return False
        if self.entity_description.available_fn is None:
            return True
        state = self.coordinator.data
        return state is not None and self.entity_description.available_fn(state)


class NarwalChargingStateSensor(NarwalEntity, SensorEntity):
    """Sensor showing charging state: Charging, Fully Charged, or unavailable."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_translation_key = "charging_state"
    _attr_options = ["charging", "fully_charged", "not_charging"]

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        """Initialize the charging state sensor."""
        super().__init__(coordinator)
        device_id = coordinator.config_entry.data["device_id"]
        self._attr_unique_id = f"{device_id}_charging_state"

    @property
    def native_value(self) -> str | None:
        """Return charging state.

        Returns None (unavailable) when not docked.
        """
        state = self.coordinator.data
        if state is None:
            return None
        if not state.is_docked:
            return "not_charging"
        if state.battery_level >= 100:
            return "fully_charged"
        return "charging"

    @property
    def icon(self) -> str:
        """Return icon based on charging state."""
        if self.native_value == "fully_charged":
            return "mdi:battery"
        if self.native_value == "charging":
            return "mdi:battery-charging"
        if self.native_value == "not_charging":
            return "mdi:battery-off-outline"
        return "mdi:battery-unknown"
