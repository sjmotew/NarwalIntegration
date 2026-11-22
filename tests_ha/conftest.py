"""Fixtures for the real-Home-Assistant load tests.

These tests run against the genuine ``homeassistant`` package supplied by
``pytest-homeassistant-custom-component`` (pinned in ``requirements-ha-test.txt``).
They deliberately live outside ``tests/`` so that ``tests/ha_stubs.py`` is never
imported: the whole point is to catch import paths and API changes the stubs
cannot see (see ``docs/TROUBLESHOOTING.md`` and the v1.0.2 post-mortem).

Only the robot I/O is faked. The coordinator, every entity platform, the config
flow and the device/entity registries are the real thing.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.narwal.const import (
    CONF_DEVICE_ID,
    CONF_DOCK_LIGHT_SUPPORTED,
    CONF_MODEL,
    CONF_PRODUCT_KEY,
    DOMAIN,
)

FLOW_PRODUCT_KEY = "QoEsI5qYXO"
DEVICE_ID = "0123456789abcdef0123456789abcdef"

# Every client coroutine the coordinator awaits during async_setup(), plus
# the listener it hands to a background task and the shutdown path.
_CLIENT_IO = (
    "connect",
    "disconnect",
    "get_device_info",
    "get_status",
    "get_map",
    "fetch_map",
    "get_consumable_info",
    "subscribe_to_topics",
    "start_listening",
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let Home Assistant load ``custom_components/narwal`` from the repo root."""


@pytest.fixture
async def silent_robot() -> AsyncGenerator[dict[str, AsyncMock], None]:
    """Patch the client's network calls so setup runs without a robot.

    ``get_status`` raises, which is what a sleeping robot looks like from the
    coordinator's side; every other call returns ``None`` and is ignored.
    """
    mocks: dict[str, AsyncMock] = {}
    patchers = []
    for name in _CLIENT_IO:
        mock = AsyncMock(name=name)
        if name == "get_status":
            mock.side_effect = TimeoutError("robot asleep")
        mocks[name] = mock
        patchers.append(
            patch(
                f"custom_components.narwal.coordinator.NarwalClient.{name}",
                new=mock,
            )
        )
    for p in patchers:
        p.start()
    try:
        yield mocks
    finally:
        for p in patchers:
            p.stop()


@pytest.fixture
def flow_entry() -> MockConfigEntry:
    """A config entry shaped like one the config flow writes for a Narwal Flow."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Narwal Flow",
        unique_id=DEVICE_ID,
        data={
            "host": "192.0.2.10",
            "port": 9002,
            CONF_DEVICE_ID: DEVICE_ID,
            CONF_MODEL: "Narwal Flow",
            CONF_PRODUCT_KEY: FLOW_PRODUCT_KEY,
        },
        # A Flow has no dock light by product key; the option override is the
        # documented way to force it, and it makes the light platform register
        # an entity so every platform is exercised.
        options={CONF_DOCK_LIGHT_SUPPORTED: True},
        version=2,
    )
