"""Sensor entities for Narwal vacuum."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfArea,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .const import TASK_RESULT_OPTIONS
from .coordinator import NarwalCoordinator
from .entity import NarwalDockEntity, NarwalEntity
from .narwal_client import NarwalState


@dataclass(frozen=True, kw_only=True)
class NarwalSensorEntityDescription(SensorEntityDescription):
    """Describes a Narwal sensor entity."""

    value_fn: Callable[[NarwalState], float | int | str | None]
    available_fn: Callable[[NarwalState], bool] | None = None
    dock_device: bool = False


def _diag(state: NarwalState, attr: str) -> float | int | None:
    """Read one field from the polled developer/get_robot_info diagnostics."""
    diagnostics = getattr(state, "diagnostics", None)
    return getattr(diagnostics, attr, None) if diagnostics else None


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
    # --- developer/get_robot_info -------------------------------------------
    # Polled engineering data, absent from every broadcast. Confirmed on a
    # Freo Z Ultra (CX7, fw v01.13.11.02). These also settle an open question
    # in docs/PROTOCOL.md: base_status field 38 reads a constant 100 while true
    # battery health here reads 89%, so field 38 is not battery health.
    NarwalSensorEntityDescription(
        key="battery_health",
        translation_key="battery_health",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state: _diag(state, "battery_health"),
        available_fn=lambda state: _diag(state, "battery_health") is not None,
    ),
    NarwalSensorEntityDescription(
        key="battery_cycles",
        translation_key="battery_cycles",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state: _diag(state, "battery_cycles"),
        available_fn=lambda state: _diag(state, "battery_cycles") is not None,
    ),
    NarwalSensorEntityDescription(
        key="battery_voltage",
        translation_key="battery_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=2,
        value_fn=lambda state: _diag(state, "battery_voltage"),
        available_fn=lambda state: _diag(state, "battery_voltage") is not None,
    ),
    NarwalSensorEntityDescription(
        key="battery_current",
        translation_key="battery_current",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=2,
        # Signed, but the convention is not established: the reference CX7 was
        # observed at -0.714 A and at +0.776 A on separate reads, both while
        # docked and charging. Reported as-is rather than normalised.
        value_fn=lambda state: _diag(state, "battery_current"),
        available_fn=lambda state: _diag(state, "battery_current") is not None,
    ),
    NarwalSensorEntityDescription(
        key="battery_temperature",
        translation_key="battery_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=1,
        value_fn=lambda state: _diag(state, "battery_temperature"),
        available_fn=lambda state: _diag(state, "battery_temperature") is not None,
    ),
    NarwalSensorEntityDescription(
        key="battery_real_level",
        translation_key="battery_real_level",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        # The robot reports a "virtual" level (what the app shows, and what
        # base_status field 2 carries) alongside the real cell charge. They
        # differ: 98% virtual against 94% real on the reference CX7.
        value_fn=lambda state: _diag(state, "battery_real_level"),
        available_fn=lambda state: _diag(state, "battery_real_level") is not None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Narwal sensor entities."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        (
            NarwalDockSensor(coordinator, description)
            if description.dock_device
            else NarwalSensor(coordinator, description)
        )
        for description in SENSOR_DESCRIPTIONS
    ]
    entities.append(NarwalChargingStateSensor(coordinator))
    async_add_entities(entities)


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
