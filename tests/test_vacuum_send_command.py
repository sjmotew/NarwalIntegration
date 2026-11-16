"""Raw robot topics for settings the integration has no entity for.

Writes go through ``vacuum.send_command``: field 1 of the reply is the result
code and a refusal raises. Reads go through ``narwal.get_topic_response``,
which returns the reply bytes untouched, because field 1 of a read reply can
be data rather than a result code.

Captured on a Freo 20 (`fjhpiem4ba`, fw v01.00.21.05): `config/set` with
`a00200` (SetConfig field 36 `speechControlEnabled` = false) turned the
"Hey Nawa" voice assistant off, and `region/set` with `{1: Region}` moved the
robot clock to Europe/Rome.
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
SupportsResponse = sys.modules["homeassistant.core"].SupportsResponse
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


def _vacuum_module(vac):
    return sys.modules[type(vac).__module__]


async def _write(vac, topic="config/set", **params):
    await vac.async_send_command(topic, params)


async def _read(vac, topic="region/get", **kwargs):
    return await vac.async_get_topic_response(topic, **kwargs)


class TestFeature:
    def test_advertised_when_available(self) -> None:
        vac = _vacuum()
        assert vac.supported_features & VacuumEntityFeature.SEND_COMMAND

    def test_advertised_before_any_state(self) -> None:
        """A robot that has reported nothing yet can still be sent a topic."""
        vac = _vacuum()
        vac.coordinator.data = None
        assert vac.supported_features & VacuumEntityFeature.SEND_COMMAND

    def test_not_advertised_when_unavailable(self) -> None:
        vac = _vacuum()
        vac.coordinator.last_update_success = False
        assert not vac.supported_features & VacuumEntityFeature.SEND_COMMAND


class TestRegistration:
    async def test_read_action_returns_a_response(self, monkeypatch) -> None:
        vac = _vacuum()
        module = _vacuum_module(vac)
        platform = MagicMock()
        monkeypatch.setattr(
            module.entity_platform,
            "async_get_current_platform",
            MagicMock(return_value=platform),
        )
        entry = MagicMock()
        entry.runtime_data = vac.coordinator

        await module.async_setup_entry(MagicMock(), entry, MagicMock())

        platform.async_register_entity_service.assert_called_once_with(
            "get_topic_response",
            module.GET_TOPIC_RESPONSE_SCHEMA,
            "async_get_topic_response",
            supports_response=SupportsResponse.ONLY,
        )


class TestWrite:
    async def test_sends_topic_and_payload(self) -> None:
        vac = _vacuum()
        await _write(vac, payload_hex="a00200")
        vac.coordinator.client.send_command.assert_awaited_once_with(
            "config/set", bytes.fromhex("a00200"), timeout=5.0
        )

    async def test_strips_slashes_and_defaults_to_empty_payload(self) -> None:
        vac = _vacuum()
        await vac.async_send_command("/region/set/")
        vac.coordinator.client.send_command.assert_awaited_once_with(
            "region/set", b"", timeout=5.0
        )

    @pytest.mark.parametrize("timeout", [1, 8, 10, 7.5])
    async def test_timeout_within_the_action_cap(self, timeout) -> None:
        vac = _vacuum()
        await _write(vac, timeout=timeout)
        assert vac.coordinator.client.send_command.call_args.kwargs["timeout"] == float(timeout)

    async def test_fires_no_event(self) -> None:
        """Success or a service error is the whole output of a write."""
        vac = _vacuum()
        await _write(vac, payload_hex="a00200")
        vac.hass.bus.async_fire.assert_not_called()

    async def test_refused_command_raises(self) -> None:
        vac = _vacuum(CommandResponse(result_code=CommandResult.NOT_APPLICABLE))
        with pytest.raises(_HomeAssistantError, match="NOT_APPLICABLE"):
            await _write(vac, payload_hex="a00200")

    async def test_get_named_topic_refusal_still_raises(self) -> None:
        """The topic name no longer decides how the reply is read."""
        vac = _vacuum(CommandResponse(result_code=CommandResult.NOT_APPLICABLE))
        with pytest.raises(_HomeAssistantError):
            await vac.async_send_command("map/get_map")

    @pytest.mark.parametrize(
        "params",
        [
            ["a00200"],
            {"payload": "a00200"},
            {1: "a00200"},
            {1: "x", "foo": "y"},
        ],
    )
    async def test_invalid_params(self, params) -> None:
        vac = _vacuum()
        with pytest.raises(_HomeAssistantError, match="params"):
            await vac.async_send_command("config/set", params)
        vac.coordinator.client.send_command.assert_not_awaited()

    async def test_unknown_param_keys_are_named(self) -> None:
        vac = _vacuum()
        with pytest.raises(_HomeAssistantError, match="unknown params 1, foo"):
            await vac.async_send_command("config/set", {1: "x", "foo": "y"})


class TestRead:
    async def test_returns_reply_without_judging_field_1(self) -> None:
        """A read whose field 1 is data (here 7) is returned, not refused."""
        vac = _vacuum(CommandResponse(result_code=7, raw_payload=bytes.fromhex("0807")))
        response = await _read(vac, "developer/take_picture")
        assert response == {"response_hex": "0807", "response_bytes": 2}

    async def test_refusal_is_returned_as_bytes(self) -> None:
        """A sleeping robot's NOT_APPLICABLE reaches the caller as 0802."""
        vac = _vacuum(
            CommandResponse(
                result_code=CommandResult.NOT_APPLICABLE, raw_payload=bytes.fromhex("0802")
            )
        )
        response = await _read(vac, "map/get_map")
        assert response == {"response_hex": "0802", "response_bytes": 2}

    async def test_large_reply_is_returned_whole(self) -> None:
        payload = bytes(range(256)) * 80  # 20,480 B, past the recorder's event limit
        vac = _vacuum(CommandResponse(result_code=CommandResult.SUCCESS, raw_payload=payload))
        response = await _read(vac, "map/get_map")
        assert response["response_bytes"] == len(payload)
        assert bytes.fromhex(response["response_hex"]) == payload

    async def test_empty_reply(self) -> None:
        vac = _vacuum(CommandResponse(result_code=CommandResult.SUCCESS))
        assert await _read(vac) == {"response_hex": "", "response_bytes": 0}

    async def test_sends_payload_and_timeout(self) -> None:
        vac = _vacuum()
        await _read(vac, "config/get", payload_hex="0a00", timeout=3)
        vac.coordinator.client.send_command.assert_awaited_once_with(
            "config/get", bytes.fromhex("0a00"), timeout=3.0
        )

    async def test_fires_no_event(self) -> None:
        vac = _vacuum()
        await _read(vac)
        vac.hass.bus.async_fire.assert_not_called()


async def _call(vac, action: str, topic: str, **params):
    if action == "write":
        await vac.async_send_command(topic, params)
    else:
        await vac.async_get_topic_response(topic, **params)


@pytest.mark.parametrize("action", ["write", "read"])
class TestShared:
    async def test_holds_the_action_lock_while_sending(self, action: str) -> None:
        vac = _vacuum()
        lock = vac.coordinator.dock_action_lock

        async def _send(*_args, **_kwargs):
            assert lock.locked()
            return CommandResponse(result_code=CommandResult.SUCCESS)

        vac.coordinator.client.send_command = AsyncMock(side_effect=_send)
        await _call(vac, action, "region/get")
        assert not lock.locked()

    async def test_wakes_a_sleeping_robot_first(self, action: str) -> None:
        vac = _vacuum()
        vac.coordinator.client.robot_awake = False
        vac.coordinator.client.wake = AsyncMock()
        await _call(vac, action, "region/get")
        vac.coordinator.client.wake.assert_awaited_once()

    @pytest.mark.parametrize(
        "topic",
        ["", "/", "Config/Set", "config//set", "a b", "../x", "config/set?x=1"],
    )
    async def test_invalid_topic(self, action: str, topic: str) -> None:
        vac = _vacuum()
        with pytest.raises(_HomeAssistantError, match="invalid topic"):
            await _call(vac, action, topic)
        vac.coordinator.client.send_command.assert_not_awaited()

    async def test_never_answered_topic_is_refused(self, action: str) -> None:
        """An unanswered request would hold its reply slot and starve the next ones."""
        vac = _vacuum()
        with pytest.raises(_HomeAssistantError, match="never answered"):
            await _call(vac, action, "status/app_status_heartbeat")
        vac.coordinator.client.send_command.assert_not_awaited()

    @pytest.mark.parametrize(
        ("params", "message"),
        [
            ({"payload_hex": "zz"}, "not valid hex"),
            ({"payload_hex": "a0020"}, "not valid hex"),
            ({"payload_hex": 100}, "quoted hex string"),
            ({"timeout": "soon"}, "must be a number"),
            ({"timeout": True}, "must be a number"),
            ({"timeout": False}, "must be a number"),
            ({"timeout": 0}, "1-10 s"),
            ({"timeout": 0.5}, "1-10 s"),
            ({"timeout": 10.5}, "1-10 s"),
            ({"timeout": 15}, "1-10 s"),
            ({"timeout": 45}, "1-10 s"),
        ],
    )
    async def test_invalid_values(self, action: str, params: dict, message: str) -> None:
        vac = _vacuum()
        with pytest.raises(_HomeAssistantError, match=message):
            await _call(vac, action, "config/set", **params)
        vac.coordinator.client.send_command.assert_not_awaited()

    @pytest.mark.parametrize("error_name", ["NarwalCommandError", "NarwalConnectionError"])
    async def test_client_errors_become_service_errors(
        self, action: str, error_name: str
    ) -> None:
        vac = _vacuum()
        # The vacuum catches the embedded client copy that HA loads.
        error = getattr(_vacuum_module(vac), error_name)("boom")
        vac.coordinator.client.send_command = AsyncMock(side_effect=error)
        with pytest.raises(_HomeAssistantError, match="boom"):
            await _call(vac, action, "region/get")

    async def test_wake_failure_becomes_service_error(self, action: str) -> None:
        vac = _vacuum()
        vac.coordinator.client.robot_awake = False
        error = _vacuum_module(vac).NarwalConnectionError
        vac.coordinator.client.wake = AsyncMock(side_effect=error("gone"))
        with pytest.raises(_HomeAssistantError):
            await _call(vac, action, "region/get")
        vac.coordinator.client.send_command.assert_not_awaited()
