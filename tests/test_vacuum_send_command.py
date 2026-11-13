"""vacuum.send_command: raw topics for settings the integration has no entity for.

Captured on a Freo 20 (`fjhpiem4ba`, fw v01.00.21.05): `config/set` with
`a00200` (SetConfig field 36 `speechControlEnabled` = false) turned the
"Hey Nawa" voice assistant off, and `region/set` with `{1: Region}` moved the
robot clock to Europe/Rome. Neither has an entity, so `send_command` is the
documented way to reach them.
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

import tests.ha_stubs

tests.ha_stubs.install()

from narwal_client.const import CommandResult  # noqa: E402
from narwal_client.models import CommandResponse, NarwalState  # noqa: E402
from tests.test_vacuum_segments import _make_vacuum  # noqa: E402

VacuumEntityFeature = sys.modules["homeassistant.components.vacuum"].VacuumEntityFeature
_HomeAssistantError = sys.modules["homeassistant.exceptions"].HomeAssistantError

_DOCKED = NarwalState()


def _vacuum(response: CommandResponse | None = None):
    vac = _make_vacuum(_DOCKED)
    vac.hass = MagicMock()
    vac.entity_id = "vacuum.narwal"
    vac.coordinator.client.robot_awake = True
    vac.coordinator.client.send_command = AsyncMock(
        return_value=response
        or CommandResponse(result_code=CommandResult.SUCCESS, raw_payload=bytes.fromhex("0801"))
    )
    return vac


class TestFeature:
    def test_advertised_when_available(self) -> None:
        vac = _vacuum()
        assert vac.supported_features & VacuumEntityFeature.SEND_COMMAND

    def test_not_advertised_without_state(self) -> None:
        vac = _vacuum()
        vac.coordinator.data = None
        assert not vac.supported_features & VacuumEntityFeature.SEND_COMMAND


class TestDispatch:
    async def test_sends_topic_and_payload(self) -> None:
        vac = _vacuum()
        await vac.async_send_command("config/set", {"payload_hex": "a00200"})
        vac.coordinator.client.send_command.assert_awaited_once_with(
            "config/set", bytes.fromhex("a00200"), timeout=5.0
        )

    async def test_strips_slashes_and_defaults_to_empty_payload(self) -> None:
        vac = _vacuum()
        await vac.async_send_command("/region/get/")
        vac.coordinator.client.send_command.assert_awaited_once_with(
            "region/get", b"", timeout=5.0
        )

    async def test_custom_timeout(self) -> None:
        vac = _vacuum()
        await vac.async_send_command("region/get", {"timeout": 20})
        assert vac.coordinator.client.send_command.call_args.kwargs["timeout"] == 20.0

    async def test_fires_response_event(self) -> None:
        vac = _vacuum()
        await vac.async_send_command("config/set", {"payload_hex": "a00200"})
        vac.hass.bus.async_fire.assert_called_once_with(
            "narwal_command_response",
            {
                "entity_id": "vacuum.narwal",
                "command": "config/set",
                "result_code": 1,
                "accepted": True,
                "response_hex": "0801",
            },
        )

    async def test_holds_the_action_lock_while_sending(self) -> None:
        vac = _vacuum()
        lock = vac.coordinator.dock_action_lock

        async def _send(*_args, **_kwargs):
            assert lock.locked()
            return CommandResponse(result_code=CommandResult.SUCCESS)

        vac.coordinator.client.send_command = AsyncMock(side_effect=_send)
        await vac.async_send_command("region/get")
        assert not lock.locked()

    async def test_get_topic_reply_is_not_a_refusal(self) -> None:
        """Field 1 of a read reply can be data that looks like a result code."""
        vac = _vacuum(CommandResponse(result_code=7, raw_payload=b"\x08\x07"))
        await vac.async_send_command("common/get_feature_list")
        assert vac.hass.bus.async_fire.call_args.args[1]["response_hex"] == "0807"

    async def test_wakes_a_sleeping_robot_first(self) -> None:
        vac = _vacuum()
        vac.coordinator.client.robot_awake = False
        vac.coordinator.client.wake = AsyncMock()
        await vac.async_send_command("region/get")
        vac.coordinator.client.wake.assert_awaited_once()


class TestRejections:
    @pytest.mark.parametrize(
        "topic",
        ["", "/", "Config/Set", "config//set", "a b", "../x", "config/set?x=1"],
    )
    async def test_invalid_topic(self, topic: str) -> None:
        vac = _vacuum()
        with pytest.raises(_HomeAssistantError):
            await vac.async_send_command(topic)
        vac.coordinator.client.send_command.assert_not_awaited()

    async def test_never_answered_topic_is_refused(self) -> None:
        """An unanswered request would hold its reply slot and starve the next ones."""
        vac = _vacuum()
        with pytest.raises(_HomeAssistantError):
            await vac.async_send_command("status/app_status_heartbeat")
        vac.coordinator.client.send_command.assert_not_awaited()

    @pytest.mark.parametrize(
        "params",
        [
            ["a00200"],
            {"payload": "a00200"},
            {"payload_hex": "zz"},
            {"payload_hex": "a0020"},
            {"timeout": "soon"},
            {"timeout": 0},
            {"timeout": 45},
            {"payload_hex": 100},
        ],
    )
    async def test_invalid_params(self, params) -> None:
        vac = _vacuum()
        with pytest.raises(_HomeAssistantError):
            await vac.async_send_command("config/set", params)
        vac.coordinator.client.send_command.assert_not_awaited()

    async def test_refused_command_raises_after_event(self) -> None:
        vac = _vacuum(CommandResponse(result_code=CommandResult.NOT_APPLICABLE))
        with pytest.raises(_HomeAssistantError):
            await vac.async_send_command("config/set", {"payload_hex": "a00200"})
        assert vac.hass.bus.async_fire.call_args.args[1]["accepted"] is False

    @pytest.mark.parametrize("error_name", ["NarwalCommandError", "NarwalConnectionError"])
    async def test_client_errors_become_service_errors(self, error_name: str) -> None:
        vac = _vacuum()
        # The vacuum catches the embedded client copy that HA loads.
        error = getattr(sys.modules[type(vac).__module__], error_name)("boom")
        vac.coordinator.client.send_command = AsyncMock(side_effect=error)
        with pytest.raises(_HomeAssistantError):
            await vac.async_send_command("region/get")
        vac.hass.bus.async_fire.assert_not_called()

    async def test_wake_failure_becomes_service_error(self) -> None:
        vac = _vacuum()
        vac.coordinator.client.robot_awake = False
        error = sys.modules[type(vac).__module__].NarwalConnectionError
        vac.coordinator.client.wake = AsyncMock(side_effect=error("gone"))
        with pytest.raises(_HomeAssistantError):
            await vac.async_send_command("region/get")
        vac.coordinator.client.send_command.assert_not_awaited()
