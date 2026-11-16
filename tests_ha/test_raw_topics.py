"""Raw topics through the real service layer: schema, targeting and responses.

The stub suite calls the entity methods directly. These tests go through
``hass.services.async_call`` so the parts only real Home Assistant provides are
covered: ``vacuum.send_command`` reaching the entity via SEND_COMMAND, the
``narwal.get_topic_response`` schema, and its response keyed by entity id.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.narwal.const import DOMAIN, SERVICE_GET_TOPIC_RESPONSE
from custom_components.narwal.narwal_client.const import CommandResult
from custom_components.narwal.narwal_client.models import CommandResponse


@pytest.fixture
async def robot(
    hass: HomeAssistant,
    flow_entry: MockConfigEntry,
    silent_robot: dict,
) -> AsyncGenerator[tuple[str, AsyncMock], None]:
    """A loaded entry whose raw commands are answered by a mock."""
    send_command = AsyncMock(
        return_value=CommandResponse(
            result_code=CommandResult.SUCCESS, raw_payload=bytes.fromhex("0801")
        )
    )
    with (
        patch(
            "custom_components.narwal.coordinator.NarwalClient.send_command",
            new=send_command,
        ),
        patch("custom_components.narwal.coordinator.NarwalClient.wake", new=AsyncMock()),
    ):
        flow_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(flow_entry.entry_id)
        await hass.async_block_till_done()
        entity_id = next(
            e.entity_id
            for e in er.async_entries_for_config_entry(er.async_get(hass), flow_entry.entry_id)
            if e.domain == "vacuum"
        )
        send_command.reset_mock()
        yield entity_id, send_command


async def test_read_action_is_registered_with_response(
    hass: HomeAssistant, robot: tuple[str, AsyncMock]
) -> None:
    assert hass.services.has_service(DOMAIN, SERVICE_GET_TOPIC_RESPONSE)
    assert (
        hass.services.supports_response(DOMAIN, SERVICE_GET_TOPIC_RESPONSE)
        is SupportsResponse.ONLY
    )


async def test_read_returns_the_reply_keyed_by_entity(
    hass: HomeAssistant, robot: tuple[str, AsyncMock]
) -> None:
    entity_id, send_command = robot
    send_command.return_value = CommandResponse(
        result_code=CommandResult.NOT_APPLICABLE, raw_payload=bytes.fromhex("0802")
    )
    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_GET_TOPIC_RESPONSE,
        {"entity_id": entity_id, "topic": "map/get_map", "payload_hex": "", "timeout": 3},
        blocking=True,
        return_response=True,
    )
    assert response == {entity_id: {"response_hex": "0802", "response_bytes": 2}}
    send_command.assert_awaited_once_with("map/get_map", b"", timeout=3.0)


@pytest.mark.parametrize(
    "data",
    [
        {"topic": "region/get", "timeout": True},
        {"topic": "region/get", "timeout": 15},
        {"topic": "region/get", "payload_hex": 100},
        {"topic": "Region/Get"},
        {"topic": "region/get", "params": {}},
        {},
    ],
)
async def test_read_rejects_bad_input(
    hass: HomeAssistant, robot: tuple[str, AsyncMock], data: dict
) -> None:
    """Wrong types and unknown keys stop at the schema; the rest at the entity."""
    entity_id, send_command = robot
    with pytest.raises((HomeAssistantError, vol.Invalid)):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_GET_TOPIC_RESPONSE,
            {"entity_id": entity_id, **data},
            blocking=True,
            return_response=True,
        )
    send_command.assert_not_awaited()


async def test_send_command_writes_and_raises_on_refusal(
    hass: HomeAssistant, robot: tuple[str, AsyncMock]
) -> None:
    entity_id, send_command = robot
    await hass.services.async_call(
        "vacuum",
        "send_command",
        {"entity_id": entity_id, "command": "config/set", "params": {"payload_hex": "a00200"}},
        blocking=True,
    )
    send_command.assert_awaited_once_with("config/set", bytes.fromhex("a00200"), timeout=5.0)

    send_command.return_value = CommandResponse(result_code=CommandResult.NOT_APPLICABLE)
    with pytest.raises(HomeAssistantError, match="NOT_APPLICABLE"):
        await hass.services.async_call(
            "vacuum",
            "send_command",
            {"entity_id": entity_id, "command": "map/get_map"},
            blocking=True,
        )
