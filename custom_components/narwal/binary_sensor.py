"""Binary sensor entities for Narwal vacuum."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .narwal_client import NarwalState

from . import NarwalConfigEntry
from .const import ERROR_HELP_URL_TEMPLATE
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity


@dataclass(frozen=True, kw_only=True)
class NarwalBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describes a Narwal binary sensor; value_fn returns None when unavailable."""

    value_fn: Callable[[NarwalState], bool | None]
    attrs_fn: Callable[[NarwalState], dict[str, Any] | None] | None = None


def _tank_problem(attr: str, bad: frozenset[int]) -> Callable[[NarwalState], bool | None]:
    """A station tank/bag state is a problem when its enum value is one of `bad`.

    The state attr is None when this model doesn't report that field, which
    keeps the entity unavailable rather than asserting "OK".
    """
    def fn(state: NarwalState) -> bool | None:
        value = getattr(state, attr)
        return None if value is None else value in bad
    return fn


# Station tank/bag problem sensors. Bad-value sets come from the decoded enums
# (RobotBaseStatus.pbenum): every named value ≥ 2 is an attention state
# (empty / abnormal / not-installed / suggest-replace); 0=unspecified, 1=ok.
BINARY_SENSOR_DESCRIPTIONS: tuple[NarwalBinarySensorEntityDescription, ...] = (
    NarwalBinarySensorEntityDescription(
        key="error",
        translation_key="error",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        # base_status field 1 errorCode: empty when healthy, populated on a fault.
        value_fn=lambda s: s.has_error if s.raw_base_status else None,
        # Expose the fault detail (numeric code(s), severity, debug string, help link) when present.
        attrs_fn=lambda s: {
            "codes": s.error_codes,
            "level": s.error_level,
            "detail": s.error_detail,
            **(
                {"help_url": ERROR_HELP_URL_TEMPLATE.format(code=s.error_codes[0])}
                if s.error_codes else {}
            ),
        } if s.raw_base_status else None,
    ),
    NarwalBinarySensorEntityDescription(
        key="clean_water_tank",
        translation_key="clean_water_tank",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_tank_problem("clean_water_tank_state", frozenset({2, 3, 4})),
    ),
    NarwalBinarySensorEntityDescription(
        key="sewage_tank",
        translation_key="sewage_tank",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_tank_problem("sewage_tank_state", frozenset({2, 3})),
    ),
    NarwalBinarySensorEntityDescription(
        key="dust_box",
        translation_key="dust_box",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_tank_problem("dust_box_state", frozenset({2, 3, 4})),
    ),
    NarwalBinarySensorEntityDescription(
        key="dust_bag",
        translation_key="dust_bag",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_tank_problem("dust_bag_state", frozenset({2, 3, 4})),
    ),
    NarwalBinarySensorEntityDescription(
        key="station_bag",
        translation_key="station_bag",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_tank_problem("station_bag_state", frozenset({2, 3, 4})),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Narwal binary sensor entities."""
    coordinator = entry.runtime_data
    entities: list[BinarySensorEntity] = [NarwalDockedSensor(coordinator)]
    entities += [
        NarwalBinarySensor(coordinator, description)
        for description in BINARY_SENSOR_DESCRIPTIONS
    ]
    async_add_entities(entities)


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


class NarwalBinarySensor(NarwalEntity, BinarySensorEntity):
    """A description-driven Narwal binary sensor (fault / station consumables)."""

    entity_description: NarwalBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: NarwalCoordinator,
        description: NarwalBinarySensorEntityDescription,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        device_id = coordinator.config_entry.data["device_id"]
        self._attr_unique_id = f"{device_id}_{description.key}"

    @property
    def is_on(self) -> bool | None:
        """Return the sensor value (None = unavailable)."""
        state = self.coordinator.data
        if state is None:
            return None
        return self.entity_description.value_fn(state)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Optional per-sensor attributes (e.g. fault code detail)."""
        state = self.coordinator.data
        if state is None or self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(state)
