"""Tests for the diagnostics dump.

The point of diagnostics is that a reporter can attach one file instead of
answering six questions, so the tests here are mostly about what the file is
guaranteed to contain — and about the one thing it must never contain.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import tests.ha_stubs

tests.ha_stubs.install()

from custom_components.narwal.const import (  # noqa: E402
    CONF_DEVICE_ID,
    CONF_MODEL,
    CONF_PRODUCT_KEY,
)
from custom_components.narwal.diagnostics import (  # noqa: E402
    TO_REDACT,
    _device_id_suffix,
    _jsonable,
    _map_summary,
    async_get_config_entry_diagnostics,
)
from custom_components.narwal.narwal_client.client import _parse_robot_info  # noqa: E402
from tests.test_robot_diagnostics import (  # noqa: E402
    _battery_response,
    _item,
    _section,
)


def _with_wifi_frame() -> bytes:
    """A get_robot_info response with the network block the CX7 really sends."""
    network = _item("Wifi", "ssid:HomeNet\npsk:hunter2-secret")
    return b"\x08\x01" + _section("网络信息", network) + _battery_response()[2:]


REDACTED = "**REDACTED**"

DEVICE_ID = "71c53f01c14f49088338863e147bb53c"


def _make_entry(
    *,
    product_key: str = "QoEsI5qYXO",
    model: str = "Narwal Flow",
    host: str = "10.0.0.112",
) -> MagicMock:
    entry = MagicMock()
    entry.version = 2
    entry.data = {
        "host": host,
        "port": 9002,
        CONF_DEVICE_ID: DEVICE_ID,
        CONF_PRODUCT_KEY: product_key,
        CONF_MODEL: model,
    }
    entry.options = {}
    return entry


def _make_state(**overrides):
    state = MagicMock()
    device_info = MagicMock()
    device_info.product_key = overrides.pop("product_key", "QoEsI5qYXO")
    device_info.device_id = overrides.pop("device_id", DEVICE_ID)
    state.device_info = device_info
    state.firmware_version = "v01.08.03.07"
    state.firmware_target = ""
    state.working_status = 1
    state.battery_level = 100
    state.binded_uuid = "account-uuid-that-must-not-leak"
    state.map_data = None
    state.raw_base_status = {}
    state.maintain_items = []
    state.replace_items = []
    state.raw_consumable_info = {}
    state.diagnostics = None
    state.error_codes = []
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


def _make_entry_with_runtime(entry: MagicMock, state, *, connected: bool = True) -> MagicMock:
    coordinator = MagicMock()
    client = MagicMock()
    client.connected = connected
    client.topic_prefix = "/QoEsI5qYXO"
    client.supports_broadcasts = True
    client.robot_awake = True
    client.state = state
    client.get_feature_list = AsyncMock(return_value={1: 1, 2: 0})
    coordinator.client = client
    coordinator.last_update_success = True
    coordinator.has_fresh_state = True
    entry.runtime_data = coordinator
    return entry


class TestRedaction:
    """The dump is posted to a public issue tracker."""

    async def test_household_identifiers_are_redacted(self) -> None:
        """Address, device id and bound account UUID must never survive."""
        entry = _make_entry()
        _make_entry_with_runtime(entry, _make_state())

        result = await async_get_config_entry_diagnostics(MagicMock(), entry)

        assert result["entry"]["data"]["host"] == REDACTED
        assert result["entry"]["data"][CONF_DEVICE_ID] == REDACTED
        assert DEVICE_ID not in repr(result)
        assert "account-uuid-that-must-not-leak" not in repr(result)

    async def test_product_key_is_not_redacted(self) -> None:
        """The whole point of the file. A key identifies a model, not a person.

        If this ever starts failing because product_key was added to TO_REDACT,
        the fix is to remove it again — a redacted dump cannot answer the
        question it exists to answer (#81).
        """
        entry = _make_entry()
        _make_entry_with_runtime(entry, _make_state())

        result = await async_get_config_entry_diagnostics(MagicMock(), entry)

        assert result["entry"]["data"][CONF_PRODUCT_KEY] == "QoEsI5qYXO"
        assert result["device"]["product_key"] == "QoEsI5qYXO"
        assert "product_key" not in TO_REDACT

    def test_device_id_suffix_keeps_only_the_mdns_tail(self) -> None:
        """Enough to match a reporter's logs, not enough to be the identifier."""
        assert _device_id_suffix(DEVICE_ID) == "7bb53c"
        assert _device_id_suffix(None) is None
        assert _device_id_suffix("abc") is None


class TestModelResolution:
    """The section that would have closed #81 in one comment."""

    async def test_unknown_key_is_reported_as_unknown(self) -> None:
        """A working robot the integration cannot name is the interesting case.

        @DeNo64's Flow 2 reported `mkbqaprvrb` and ran fine for three releases
        while being called "Narwal Flow", because nothing surfaced that the key
        was unrecognised.
        """
        entry = _make_entry(product_key="zzzzUNKNOWN", model="Narwal Flow")
        state = _make_state(product_key="zzzzUNKNOWN")
        _make_entry_with_runtime(entry, state)
        entry.runtime_data.client.topic_prefix = "/zzzzUNKNOWN"

        result = await async_get_config_entry_diagnostics(MagicMock(), entry)
        resolution = result["model_resolution"]

        assert resolution["resolved_product_key"] == "zzzzUNKNOWN"
        assert resolution["key_is_known"] is False
        assert resolution["label_for_resolved_key"] is None

    async def test_alias_key_is_known_but_not_selectable(self) -> None:
        """`mkbqaprvrb` names a model without appearing in the dropdown."""
        entry = _make_entry(product_key="mkbqaprvrb", model="Narwal Flow 2")
        state = _make_state(product_key="mkbqaprvrb")
        _make_entry_with_runtime(entry, state)
        entry.runtime_data.client.topic_prefix = "/mkbqaprvrb"

        result = await async_get_config_entry_diagnostics(MagicMock(), entry)
        resolution = result["model_resolution"]

        assert resolution["key_is_known"] is True
        assert resolution["key_is_selectable"] is False
        assert resolution["label_for_resolved_key"] == "Narwal Flow 2"

    async def test_disagreement_between_stored_and_resolved_key_is_flagged(self) -> None:
        """Stored key vs the key the robot actually answers on."""
        entry = _make_entry(product_key="QoEsI5qYXO")
        state = _make_state(product_key="QxMSPG6VSO")
        _make_entry_with_runtime(entry, state)
        entry.runtime_data.client.topic_prefix = "/QxMSPG6VSO"

        result = await async_get_config_entry_diagnostics(MagicMock(), entry)

        assert result["model_resolution"]["keys_disagree"] is True


class TestFeatureList:
    """A live round trip that must never hang the diagnostics download."""

    async def test_feature_list_included_when_the_robot_answers(self) -> None:
        entry = _make_entry()
        _make_entry_with_runtime(entry, _make_state())

        result = await async_get_config_entry_diagnostics(MagicMock(), entry)

        assert result["feature_list"]["available"] is True
        assert result["feature_list"]["features"] == {"1": 1, "2": 0}

    async def test_disconnected_robot_does_not_block_the_dump(self) -> None:
        entry = _make_entry()
        _make_entry_with_runtime(entry, _make_state(), connected=False)

        result = await async_get_config_entry_diagnostics(MagicMock(), entry)

        assert result["feature_list"] == {"available": False, "reason": "not connected"}
        assert result["device"]["product_key"] == "QoEsI5qYXO"

    async def test_failure_reason_is_reported_rather_than_swallowed(self) -> None:
        """"The robot refused get_feature_list" is itself a model finding."""
        entry = _make_entry()
        _make_entry_with_runtime(entry, _make_state())
        entry.runtime_data.client.get_feature_list = AsyncMock(
            side_effect=RuntimeError("NOT_APPLICABLE")
        )

        result = await async_get_config_entry_diagnostics(MagicMock(), entry)

        assert result["feature_list"]["available"] is False
        assert "RuntimeError" in result["feature_list"]["reason"]
        assert "NOT_APPLICABLE" in result["feature_list"]["reason"]

    async def test_a_hanging_robot_times_out_instead_of_hanging(self) -> None:
        """Diagnostics is user-initiated from the UI and must always return."""
        entry = _make_entry()
        _make_entry_with_runtime(entry, _make_state())

        async def _never_answers() -> dict[int, int]:
            await asyncio.sleep(3600)
            raise AssertionError("should have been cancelled")

        entry.runtime_data.client.get_feature_list = _never_answers

        import custom_components.narwal.diagnostics as diagnostics_module

        original = diagnostics_module._FEATURE_LIST_TIMEOUT
        diagnostics_module._FEATURE_LIST_TIMEOUT = 0.01
        try:
            result = await asyncio.wait_for(
                async_get_config_entry_diagnostics(MagicMock(), entry), timeout=5
            )
        finally:
            diagnostics_module._FEATURE_LIST_TIMEOUT = original

        assert result["feature_list"] == {"available": False, "reason": "timed out"}


class TestRobotInfo:
    """developer/get_robot_info reaches the dump — minus the Wi-Fi PSK.

    The robot returns its Wi-Fi credentials in that response, and this file is
    attached to public issues. The PSK is discarded when the client parses the
    frame, so the guarantee tested here is end to end: a frame carrying a
    `psk:` value goes in, and neither the key nor the label comes out.
    """

    async def test_psk_from_a_captured_frame_never_reaches_the_download(self) -> None:
        frame = _with_wifi_frame()
        diagnostics = _parse_robot_info(frame)
        assert diagnostics is not None
        # Belt: the parsed object itself carries no trace of it.
        for items in diagnostics.sections.values():
            for label, value in items.items():
                assert "psk" not in label.lower()
                assert "psk" not in value.lower()
                assert "hunter2-secret" not in value
        entry = _make_entry()
        _make_entry_with_runtime(entry, _make_state(diagnostics=diagnostics))

        result = await async_get_config_entry_diagnostics(MagicMock(), entry)

        # Braces: nor does the download, anywhere in it.
        blob = repr(result)
        assert "hunter2-secret" not in blob
        assert "psk" not in blob.lower()
        assert result["robot_info"]["battery_health"] == 89
        assert result["robot_info"]["sections"]["电池信息"]["使用次数"] == "519"

    async def test_models_without_the_topic_report_null(self) -> None:
        """A robot that never answered get_robot_info is not an error."""
        entry = _make_entry()
        _make_entry_with_runtime(entry, _make_state(diagnostics=None))

        result = await async_get_config_entry_diagnostics(MagicMock(), entry)

        assert result["robot_info"] is None


class TestRawPayloads:
    """New-model support is built from the fields we haven't decoded."""

    def test_bytes_survive_as_hex_rather_than_being_dropped(self) -> None:
        """An undecoded blob is frequently the field being asked about."""
        result = _jsonable({"38": b"\x08\x01", "nested": {"41": [b"\xff", 3]}})

        assert result["38"] == {"__bytes_hex__": "0801", "length": 2}
        assert result["nested"]["41"][0] == {"__bytes_hex__": "ff", "length": 1}
        assert result["nested"]["41"][1] == 3

    async def test_raw_base_status_is_included_whole(self) -> None:
        entry = _make_entry()
        _make_entry_with_runtime(
            entry, _make_state(raw_base_status={"99": 7, "100": b"\x02"})
        )

        result = await async_get_config_entry_diagnostics(MagicMock(), entry)

        assert result["raw_base_status"]["99"] == 7
        assert result["raw_base_status"]["100"]["__bytes_hex__"] == "02"

    async def test_raw_consumable_info_is_included(self) -> None:
        """The consumable response is not the same shape on every firmware.

        A Flow 2 (v01.09.09.05) sends an envelope with the firmware string in field 3
        and five scalar fields in field 1, which the parser reads as ids that do not
        exist. Keeping the raw decode makes that identifiable from a diagnostics
        download.
        """
        entry = _make_entry()
        _make_entry_with_runtime(
            entry,
            _make_state(
                raw_consumable_info={
                    "1": {"1": 1000, "2": 1000},
                    "2": 30000,
                    "3": "v01.09.09.05\n",
                },
                maintain_items=[1000],
            ),
        )

        result = await async_get_config_entry_diagnostics(MagicMock(), entry)

        assert result["consumables"]["raw_consumable_info"]["2"] == 30000
        assert result["consumables"]["raw_consumable_info"]["1"]["1"] == 1000
        assert result["consumables"]["maintain_items"] == [1000]

    def test_map_summary_omits_the_compressed_payload(self) -> None:
        """Room structure is useful; a megabyte of packed grid is not."""
        # MagicMock(name=...) sets the mock's own name, not a `.name` attribute.
        room = MagicMock(room_id=3, room_sub_type=5, category=1)
        room.name = "Laundry"
        map_data = MagicMock(
            map_id=1, width=800, height=600, resolution=50, area=42,
            origin_x=10, origin_y=20, dock_x=1.0, dock_y=2.0,
            compressed_map=b"\x00" * 4096, obstacles=[], rooms=[room],
        )

        summary = _map_summary(map_data)

        assert summary["compressed_map_bytes"] == 4096
        assert "compressed_map" not in summary
        assert summary["rooms"] == [
            {"room_id": 3, "name": "Laundry", "room_sub_type": 5, "category": 1}
        ]

    def test_map_summary_handles_a_robot_with_no_map(self) -> None:
        assert _map_summary(None) is None
