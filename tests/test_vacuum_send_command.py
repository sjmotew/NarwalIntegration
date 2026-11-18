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

import asyncio
import importlib
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

    @pytest.mark.parametrize("timeout", [1, 8, 10, 7.5, 12.5, 15])
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

    async def test_reply_with_data_in_field_1_raises(self) -> None:
        """The client counts a non-int field 1 as SUCCESS; a write must not."""
        vac = _vacuum(
            CommandResponse(
                result_code=CommandResult.SUCCESS,
                data={"1": {"2": 3}},
                raw_payload=bytes.fromhex("0a021003"),
            )
        )
        with pytest.raises(
            _HomeAssistantError, match="answered region/get with data.*get_topic_response"
        ):
            await vac.async_send_command("region/get")
        vac.coordinator.client.send_command.assert_awaited_once()

    @pytest.mark.parametrize("data", [{"1": True}, {"1": b"x"}, {"1": "x"}])
    async def test_non_int_field_1_values_raise(self, data: dict) -> None:
        vac = _vacuum(CommandResponse(result_code=CommandResult.SUCCESS, data=data))
        with pytest.raises(_HomeAssistantError, match="with data"):
            await _write(vac)

    @pytest.mark.parametrize("code", ["2", "3", "4"])
    async def test_string_field_1_is_data_not_a_result_code(self, code: str) -> None:
        """0a0132 decodes to '2', which the client turns into result code 2.

        The write ran; reporting NOT_APPLICABLE would invite a resend.
        """
        raw = bytes.fromhex("0a01") + code.encode()
        vac = _vacuum(
            CommandResponse(result_code=int(code), data={"1": code}, raw_payload=raw)
        )
        with pytest.raises(_HomeAssistantError) as err:
            await _write(vac)
        message = str(err.value)
        assert "with data (3 B)" in message
        assert "should not be sent again" in message
        assert "failed" not in message

    @pytest.mark.parametrize(
        "response",
        [
            CommandResponse(result_code=0, data={"2": 5}, raw_payload=bytes.fromhex("1005")),
            CommandResponse(result_code=0),
            CommandResponse(result_code=CommandResult.SUCCESS, data={"1": 1}),
            CommandResponse(result_code=CommandResult.APPLIED, data={"1": 6}),
        ],
    )
    async def test_reply_without_data_in_field_1_is_accepted(self, response) -> None:
        vac = _vacuum(response)
        await _write(vac, payload_hex="a00200")

    async def test_current_task_read_is_refused(self) -> None:
        """Only the read action has the clean/ and task/ read allowlist."""
        vac = _vacuum()
        with pytest.raises(_HomeAssistantError, match="clean/current_clean_task/get is refused"):
            await vac.async_send_command("clean/current_clean_task/get")
        vac.coordinator.client.send_command.assert_not_awaited()


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

    async def test_data_in_field_1_is_returned(self) -> None:
        vac = _vacuum(
            CommandResponse(
                result_code=CommandResult.SUCCESS,
                data={"1": {"2": 3}},
                raw_payload=bytes.fromhex("0a021003"),
            )
        )
        assert await _read(vac) == {"response_hex": "0a021003", "response_bytes": 4}

    async def test_current_task_read_is_allowed(self) -> None:
        vac = _vacuum()
        await _read(vac, "clean/current_clean_task/get")
        vac.coordinator.client.send_command.assert_awaited_once_with(
            "clean/current_clean_task/get", b"", timeout=5.0
        )



_NATIVE_TOPICS = [
    ("clean/plan/start", "vacuum.start or narwal.clean_rooms"),
    ("clean/start_clean", "vacuum.start or narwal.clean_rooms"),
    ("clean/easy_clean/start", "vacuum.start or narwal.clean_rooms"),
    ("task/pause", "vacuum.pause"),
    ("task/resume", "vacuum.start"),
    ("task/force_end", "vacuum.stop"),
    ("task/cancel", "vacuum.stop"),
    ("supply/recall", "vacuum.return_to_base"),
    ("supply/wash_mop", "the Wash mop switch"),
    ("supply/wash_mop_by_robot_status", "the Wash mop switch"),
    ("supply/dry_mop", "the Dry mop switch"),
    ("supply/dry_dust_bag", "the Dry and disinfect dust bin switch"),
    ("supply/dry_station_bag", "the Dry and disinfect dock bag switch"),
    ("supply/dust_gathering", "the Empty dustbin switch"),
    ("clean/set_fan_level", "vacuum.set_fan_speed"),
    ("clean/set_mop_humidity", "the Mopping humidity select"),
    ("supply/ambient_light_ctrl", "the Dock light entity"),
]


async def _call(vac, action: str, topic: str, **params):
    if action == "write":
        await vac.async_send_command(topic, params)
    else:
        await vac.async_get_topic_response(topic, **params)


@pytest.mark.parametrize("action", ["write", "read"])
class TestShared:
    @pytest.mark.parametrize(("topic", "instead"), _NATIVE_TOPICS)
    async def test_native_topic_is_refused(self, action: str, topic: str, instead: str) -> None:
        """A raw copy would skip the coordinator bookkeeping of the native action."""
        vac = _vacuum()
        with pytest.raises(_HomeAssistantError) as err:
            await _call(vac, action, topic)
        assert topic in str(err.value)
        assert f"use {instead}" in str(err.value)
        vac.coordinator.client.send_command.assert_not_awaited()

    def test_native_topics_cover_the_mapping(self, action: str) -> None:
        vac = _vacuum()
        assert set(_vacuum_module(vac)._NATIVE_TOPICS) == {t for t, _ in _NATIVE_TOPICS}

    @pytest.mark.parametrize(
        "topic", ["clean/foo", "clean/plan/get_all", "task/foo", "task/get_status"]
    )
    async def test_other_clean_and_task_topics_are_refused(
        self, action: str, topic: str
    ) -> None:
        vac = _vacuum()
        with pytest.raises(_HomeAssistantError, match=f"{topic} is refused"):
            await _call(vac, action, topic)
        vac.coordinator.client.send_command.assert_not_awaited()

    @pytest.mark.parametrize(
        "topic",
        ["config/set", "region/set", "common/reboot", "config/gett", "config/get", "region/get"],
    )
    async def test_other_topics_are_sent(self, action: str, topic: str) -> None:
        """Topics outside the refused groups are sent as given."""
        vac = _vacuum()
        await _call(vac, action, topic)
        vac.coordinator.client.send_command.assert_awaited_once_with(
            topic, b"", timeout=5.0
        )

    @pytest.mark.parametrize(
        "topic", ["common/active_robot_publish", "common/notify_app_event"]
    )
    async def test_session_topic_is_refused(self, action: str, topic: str) -> None:
        """A raw copy can end the integration's subscription without it knowing (#73)."""
        vac = _vacuum()
        with pytest.raises(_HomeAssistantError, match="awake and subscribed") as err:
            await _call(vac, action, topic)
        assert topic in str(err.value)
        vac.coordinator.client.send_command.assert_not_awaited()

    @pytest.mark.parametrize(("payload_hex", "body"), [(1234, b"\x12\x34"), (12, b"\x12")])
    async def test_int_payload_is_read_as_hex_text(
        self, action: str, payload_hex: int, body: bytes
    ) -> None:
        """An all-digit body from a template arrives as an int."""
        vac = _vacuum()
        await _call(vac, action, "config/set", payload_hex=payload_hex)
        assert vac.coordinator.client.send_command.call_args.args[1] == body

    async def test_wakes_a_sleeping_robot_first(self, action: str) -> None:
        vac = _vacuum()
        vac.coordinator.client.robot_awake = False
        vac.coordinator.client.wake = AsyncMock()
        await _call(vac, action, "region/get")
        vac.coordinator.client.wake.assert_awaited_once()

    @pytest.mark.parametrize(
        "topic",
        [
            "map/get_map",
            "region/get",
            "config/get",
            "config/set",
            "region/set",
            "common/reboot",
            "common/yell",
        ],
    )
    async def test_holds_the_action_lock_while_sending(self, action: str, topic: str) -> None:
        """The action cannot tell a read from a write, so every topic locks."""
        vac = _vacuum()
        lock = vac.coordinator.dock_action_lock

        async def _send(*_args, **_kwargs):
            assert lock.locked()
            return CommandResponse(result_code=CommandResult.SUCCESS)

        vac.coordinator.client.send_command = AsyncMock(side_effect=_send)
        await asyncio.wait_for(_call(vac, action, topic), 1)
        vac.coordinator.client.send_command.assert_awaited_once()
        assert not lock.locked()

    async def test_wakes_without_the_action_lock(self, action: str) -> None:
        vac = _vacuum()
        lock = vac.coordinator.dock_action_lock
        vac.coordinator.client.robot_awake = False

        async def _wake(*_args, **_kwargs):
            assert not lock.locked()

        vac.coordinator.client.wake = AsyncMock(side_effect=_wake)
        await asyncio.wait_for(_call(vac, action, "config/set"), 1)
        vac.coordinator.client.wake.assert_awaited_once()
        vac.coordinator.client.send_command.assert_awaited_once()

    async def test_wakes_before_waiting_for_the_action_lock(self, action: str) -> None:
        """Stop or return holding the lock does not delay the wake, and vice versa."""
        vac = _vacuum()
        lock = vac.coordinator.dock_action_lock
        client = vac.coordinator.client
        client.robot_awake = False
        client.wake = AsyncMock()
        await lock.acquire()
        task = asyncio.ensure_future(_call(vac, action, "config/set"))
        try:
            for _ in range(5):
                await asyncio.sleep(0)
            client.wake.assert_awaited_once()
            client.send_command.assert_not_awaited()
            assert not task.done()
        finally:
            lock.release()
        await asyncio.wait_for(task, 1)
        client.send_command.assert_awaited_once()
        assert not lock.locked()

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
            ({"payload_hex": 100}, "not valid hex"),
            ({"payload_hex": True}, "quoted hex string"),
            ({"payload_hex": False}, "quoted hex string"),
            ({"payload_hex": 12.34}, "quoted hex string"),
            ({"payload_hex": ["a0"]}, "quoted hex string"),
            ({"payload_hex": None}, "quoted hex string"),
            ({"timeout": "soon"}, "must be a number"),
            ({"timeout": True}, "must be a number"),
            ({"timeout": False}, "must be a number"),
            ({"timeout": 0}, "1-15 s"),
            ({"timeout": 0.5}, "1-15 s"),
            ({"timeout": 15.5}, "1-15 s"),
            ({"timeout": 20}, "1-15 s"),
            ({"timeout": 45}, "1-15 s"),
        ],
    )
    async def test_invalid_values(self, action: str, params: dict, message: str) -> None:
        vac = _vacuum()
        with pytest.raises(_HomeAssistantError, match=message):
            await _call(vac, action, "config/set", **params)
        vac.coordinator.client.send_command.assert_not_awaited()

    async def test_client_gets_only_topic_payload_and_timeout(self, action: str) -> None:
        """No other keyword reaches the client, so a stray one cannot come back unseen."""
        vac = _vacuum()
        await _call(vac, action, "map/get_map", payload_hex="0a00", timeout=15)
        call = vac.coordinator.client.send_command.await_args
        assert call.args == ("map/get_map", bytes.fromhex("0a00"))
        assert call.kwargs == {"timeout": 15.0}

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
        with pytest.raises(_HomeAssistantError, match="gone"):
            await _call(vac, action, "region/get")
        vac.coordinator.client.send_command.assert_not_awaited()
        assert not vac.coordinator.dock_action_lock.locked()


def _client_const(module):
    """The embedded client's const module, the one the vacuum imports."""
    return importlib.import_module(f"{module.__package__}.narwal_client.const")


def _client_topic_constants(const) -> dict[str, str]:
    return {
        name: value for name, value in vars(const).items() if name.startswith("TOPIC_CMD_")
    }


class TestTopicClassification:
    def test_every_client_topic_is_in_exactly_one_group(self) -> None:
        """A new client topic must be consciously refused or allowed raw.

        Every TOPIC_CMD_* constant is refused as native, refused as a session
        topic, refused as never answered, or named in the sendable list here.
        """
        module = _vacuum_module(_vacuum())
        const = _client_const(module)
        constants = _client_topic_constants(const)
        assert len(constants) > 30
        sendable = {
            "TOPIC_CMD_YELL",
            "TOPIC_CMD_REBOOT",
            "TOPIC_CMD_SHUTDOWN",
            "TOPIC_CMD_GET_DEVICE_INFO",
            "TOPIC_CMD_GET_FEATURE_LIST",
            "TOPIC_CMD_GET_BASE_STATUS",
            "TOPIC_CMD_GET_CONSUMABLE_INFO",
            "TOPIC_CMD_GET_CURRENT_TASK",
            "TOPIC_CMD_GET_MAP",
            "TOPIC_CMD_GET_ALL_MAPS",
            "TOPIC_CMD_TAKE_PICTURE",
            "TOPIC_CMD_SET_LED",
            "TOPIC_CMD_GET_DEBUG_IMAGE",
            "TOPIC_CMD_PING",
        }
        assert sendable <= set(constants)
        groups = {
            "native": set(module._NATIVE_TOPICS),
            "session": set(module._SESSION_TOPICS),
            "unacknowledged": set(module.UNACKNOWLEDGED_TOPICS),
            "sendable": {constants[name] for name in sendable},
        }
        for name, topic in constants.items():
            homes = [group for group, topics in groups.items() if topic in topics]
            assert len(homes) == 1, f"{name} ({topic}) is in {homes or 'no group'}"

    def test_read_allowed_topics_are_clean_or_task_reads(self) -> None:
        module = _vacuum_module(_vacuum())
        allowed = module._RAW_READ_ALLOWED_TOPICS
        assert allowed == {"clean/current_clean_task/get"}
        assert all(topic.startswith(module._NATIVE_TOPIC_PREFIXES) for topic in allowed)
        assert not allowed & set(module._NATIVE_TOPICS)
        assert not allowed & module._SESSION_TOPICS
        assert not allowed & module.UNACKNOWLEDGED_TOPICS
