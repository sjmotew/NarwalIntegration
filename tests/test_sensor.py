"""Tests for Narwal sensor entities."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import tests.ha_stubs

tests.ha_stubs.install()

from custom_components.narwal.const import NARWAL_MODELS  # noqa: E402
from custom_components.narwal.sensor import (  # noqa: E402
    SENSOR_DESCRIPTIONS,
    NarwalSensor,
    async_setup_entry,
)
from narwal_client.const import WorkingStatus  # noqa: E402
from narwal_client.models import NarwalState  # noqa: E402


def _sensor(key: str, state: NarwalState) -> NarwalSensor:
    """Create a NarwalSensor with mocked coordinator data."""
    coordinator = MagicMock()
    coordinator.data = state
    coordinator.last_update_success = True
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.data = {"device_id": "test_device"}
    coordinator.config_entry.title = "Narwal Test"
    coordinator.client = MagicMock()
    coordinator.client.state = state
    coordinator.client.state.firmware_version = "1.0.0"
    description = next(item for item in SENSOR_DESCRIPTIONS if item.key == key)
    return NarwalSensor(coordinator, description)


def test_current_room_is_not_a_standalone_sensor() -> None:
    """Live task context is carried by the vacuum entity, not stale sensors."""
    sensor_keys = {description.key for description in SENSOR_DESCRIPTIONS}
    assert "current_room" not in sensor_keys
    assert "map_metadata" not in sensor_keys
    assert "status" not in sensor_keys
    assert "task_progress" not in sensor_keys


def test_cleaning_metrics_are_unavailable_when_idle() -> None:
    """Cleaning metric sensors should not expose stale previous-clean values."""
    state = NarwalState(working_status=WorkingStatus.DOCKED)
    state.cleaning_area = 12.5
    state.cleaning_time = 900
    state.task_remaining_time = 300

    assert not _sensor("cleaning_area", state).available
    assert not _sensor("cleaning_time", state).available
    assert not _sensor("remaining_time", state).available


def test_cleaning_metrics_are_available_during_active_clean() -> None:
    """Cleaning metric sensors expose current robot task values while active."""
    state = NarwalState(working_status=WorkingStatus.CLEANING)
    state.cleaning_area = 12.5
    state.cleaning_time = 900
    state.task_remaining_time = 300

    assert _sensor("cleaning_area", state).available
    assert _sensor("cleaning_area", state).native_value == 12.5
    assert _sensor("cleaning_time", state).available
    assert _sensor("cleaning_time", state).native_value == 900
    assert _sensor("remaining_time", state).available
    assert _sensor("remaining_time", state).native_value == 300


@pytest.mark.parametrize(
    ("product_key", "enabled_by_default"),
    [
        (NARWAL_MODELS["Narwal Flow"], False),
        (NARWAL_MODELS["Narwal Flow 2"], False),
        ("iSuVlI1If2", False),  # Flow 2 alias key (#81)
        ("mkbqaprvrb", False),  # Flow 2 alias key (#81)
        (NARWAL_MODELS["Narwal Freo Z10 Ultra"], True),
    ],
)
async def test_detergent_sensor_disabled_by_default_without_detergent_tank(
    product_key: str, enabled_by_default: bool
) -> None:
    """Flow models have no detergent tank, so field 41 is created but disabled."""
    state = NarwalState()
    coordinator = _sensor("battery", state).coordinator
    entry = MagicMock()
    entry.runtime_data = coordinator
    entry.data = {"device_id": "test_device", "product_key": product_key}
    added: list = []

    await async_setup_entry(MagicMock(), entry, added.extend)

    detergent = next(
        entity for entity in added
        if getattr(entity, "entity_description", None) is not None
        and entity.entity_description.key == "detergent_remaining"
    )
    assert detergent.entity_description.entity_registry_enabled_default is enabled_by_default
    other = [
        entity.entity_description for entity in added
        if getattr(entity, "entity_description", None) is not None
        and entity.entity_description.key != "detergent_remaining"
    ]
    assert all(description.entity_registry_enabled_default for description in other)
