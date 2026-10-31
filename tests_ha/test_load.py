"""Does the integration load in real Home Assistant?

The stub-based suite under ``tests/`` cannot answer that question (every HA
import path resolves there by construction). These tests can. They are the CI
stand-in for "deploy to the live instance and watch the entities come up".
"""

from __future__ import annotations

import importlib
import logging

import pytest
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.narwal.const import DOMAIN, PLATFORMS

from .conftest import DEVICE_ID

# Entities a Narwal Flow entry registers on a fresh install, disabled ones
# included. v1.0.2 shipped 28; per-room profiles, dock tasks and map toggles
# pushed it well past that. The floor is deliberately loose: the test is here
# to notice a platform that silently failed to set up, not to pin the count.
MIN_REGISTERED_ENTITIES = 40

MODULES = [*(str(p) for p in PLATFORMS), "config_flow", "diagnostics", "entity"]


@pytest.mark.parametrize("module", MODULES)
def test_module_imports_against_real_ha(module: str) -> None:
    """Every module imports with the genuine ``homeassistant`` package.

    This is the v1.0.2 bug: ``switch.py`` imported ``EntityCategory`` from a
    path real HA no longer provides, and the stubs provided it anyway.
    """
    importlib.import_module(f"custom_components.narwal.{module}")


def test_map_card_attributes_stay_out_of_the_recorder() -> None:
    """Real HA merges our unrecorded keys with the camera's own ones.

    The stubs cannot show this: the union is built by ``Entity.__init_subclass__``.
    """
    from custom_components.narwal.camera import _MAP_CARD_ATTRIBUTES, NarwalMapCamera

    combined = NarwalMapCamera._Entity__combined_unrecorded_attributes
    assert combined >= _MAP_CARD_ATTRIBUTES
    assert "access_token" in combined
    assert "render_count" not in combined


async def test_user_flow_shows_form(hass: HomeAssistant) -> None:
    """The config flow at least renders its first step."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_entry_sets_up_every_platform(
    hass: HomeAssistant,
    flow_entry: MockConfigEntry,
    silent_robot: dict,
) -> None:
    """A Flow entry loads, forwards to all platforms and registers its entities."""
    flow_entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(flow_entry.entry_id)
    await hass.async_block_till_done()

    assert flow_entry.state is ConfigEntryState.LOADED
    silent_robot["connect"].assert_awaited_once()

    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, flow_entry.entry_id)
    platforms_seen = {e.domain for e in entries}
    missing = {str(p) for p in PLATFORMS} - platforms_seen
    assert not missing, f"platforms that registered no entity: {sorted(missing)}"
    assert len(entries) >= MIN_REGISTERED_ENTITIES, len(entries)

    vacuum = next(e for e in entries if e.domain == "vacuum")
    state = hass.states.get(vacuum.entity_id)
    assert state is not None
    assert state.state != "unavailable"

    devices = dr.async_get(hass)
    robot = devices.async_get_device(identifiers={(DOMAIN, DEVICE_ID)})
    dock = devices.async_get_device(identifiers={(DOMAIN, f"{DEVICE_ID}_dock")})
    assert robot is not None and dock is not None
    assert dock.via_device_id == robot.id


async def test_entry_unloads_cleanly(
    hass: HomeAssistant,
    flow_entry: MockConfigEntry,
    silent_robot: dict,
) -> None:
    """Unload tears everything down without leaving the entry half-loaded."""
    flow_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(flow_entry.entry_id)
    await hass.async_block_till_done()

    assert await hass.config_entries.async_unload(flow_entry.entry_id)
    await hass.async_block_till_done()

    assert flow_entry.state is ConfigEntryState.NOT_LOADED
    silent_robot["disconnect"].assert_awaited()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Known HA 2027.8 deprecations: via_device in DeviceInfo (entity.py) and "
        "dr.async_get_device(identifiers=...) in __init__.py. Remove this marker "
        "when both are fixed; strict=True makes the fix itself flip the test."
    ),
)
async def test_no_deprecation_reports_from_narwal(
    hass: HomeAssistant,
    flow_entry: MockConfigEntry,
    silent_robot: dict,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Home Assistant reports no deprecated usage attributed to this integration.

    HA's ``frame.report_usage`` logs 'Detected that custom integration
    'narwal' ...' with the version the behaviour stops working in. The live
    instance currently shows two such lines; this test turns them into CI.
    """
    caplog.set_level(logging.WARNING)
    flow_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(flow_entry.entry_id)
    await hass.async_block_till_done()

    reports = [
        r.getMessage()
        for r in caplog.records
        if "custom integration 'narwal'" in r.getMessage()
        or "custom integration narwal" in r.getMessage()
    ]
    assert not reports, "\n".join(reports)
