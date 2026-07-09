"""Tests for narwal_client.client — WebSocket client."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from narwal_client.client import NarwalClient, NarwalConnectionError
from narwal_client.const import CommandResult
from narwal_client.models import CommandResponse, MapData, RoomInfo


class TestNarwalClientInit:
    """Tests for NarwalClient initialization."""

    def test_default_port(self) -> None:
        client = NarwalClient("192.168.1.100")
        assert client.host == "192.168.1.100"
        assert client.port == 9002
        assert client.url == "ws://192.168.1.100:9002"

    def test_custom_port(self) -> None:
        client = NarwalClient("10.0.0.1", port=8080)
        assert client.port == 8080
        assert client.url == "ws://10.0.0.1:8080"

    def test_initial_state(self) -> None:
        client = NarwalClient("10.0.0.1")
        assert not client.connected
        assert client.state.battery_level == 0

    @pytest.mark.asyncio
    async def test_commands_require_connection(self) -> None:
        client = NarwalClient("10.0.0.1")
        with pytest.raises(NarwalConnectionError):
            await client.start()

    @pytest.mark.asyncio
    async def test_send_raw_without_connection_raises(self) -> None:
        client = NarwalClient("10.0.0.1")
        with pytest.raises(NarwalConnectionError):
            await client.send_raw("test/topic", b"\x08\x01")


class TestBuildCleanPayloadV2:
    """Tests for the v2 clean payload schema introduced for firmware
    v01.07.22+ (issue #36)."""

    def test_single_room_uses_nested_room_id(self) -> None:
        """Single room encodes as a nested {1:1, 2:room_id} message."""
        import blackboxprotobuf

        client = NarwalClient("127.0.0.1")
        payload = client._build_clean_payload_v2([7])
        decoded, _ = blackboxprotobuf.decode_message(payload)

        outer = decoded["1"]
        assert outer["1"] == 1
        assert outer["5"] == 6  # observed task source marker

        entry = outer["2"]
        assert entry["1"]["2"] == 7, "Room ID lives at 1.2.1.2 in v2 schema"
        assert entry["1"]["1"] == 1, "Inner field 1.2.1.1 was 1 in observed capture"
        assert entry["3"] == 1, "Sequence index starts at 1"

    def test_multiple_rooms_get_sequence_indices(self) -> None:
        """Multiple rooms preserve order via the 3:<seq> field, 1-indexed."""
        import blackboxprotobuf

        client = NarwalClient("127.0.0.1")
        payload = client._build_clean_payload_v2([5, 9, 3])
        decoded, _ = blackboxprotobuf.decode_message(payload)

        entries = decoded["1"]["2"]
        assert isinstance(entries, list)
        assert len(entries) == 3
        assert [e["1"]["2"] for e in entries] == [5, 9, 3]
        assert [e["3"] for e in entries] == [1, 2, 3]

    def test_default_clean_params(self) -> None:
        """Default suction=3 / mop=2 / passes=1 / cleanMode=3 — Flow 1 max."""
        import blackboxprotobuf

        client = NarwalClient("127.0.0.1")
        payload = client._build_clean_payload_v2([1])
        decoded, _ = blackboxprotobuf.decode_message(payload)

        params = decoded["1"]["2"]["2"]
        assert params["1"] == 3, "suction default 3 (Flow 1 max)"
        assert params["2"] == 3, "cleanMode default 3 (sweep+mop in v2)"
        assert params["3"] == 1, "passes default 1"
        assert params["7"] == 2, "mop_humidity default 2 (wet)"

    def test_custom_params_propagate(self) -> None:
        """Caller-provided params (suction=4 for Flow 2) reach the wire."""
        import blackboxprotobuf

        client = NarwalClient("127.0.0.1")
        payload = client._build_clean_payload_v2(
            [1], suction=4, mop_humidity=1, passes=2, clean_mode=3
        )
        decoded, _ = blackboxprotobuf.decode_message(payload)
        params = decoded["1"]["2"]["2"]
        assert params["1"] == 4
        assert params["7"] == 1
        assert params["3"] == 2

    def test_v2_payload_differs_from_legacy_default(self) -> None:
        """v2 schema must not collide with the legacy hardcoded default."""
        client = NarwalClient("127.0.0.1")
        v2 = client._build_clean_payload_v2([1])
        assert v2 != client._DEFAULT_CLEAN_PAYLOAD


class TestStartLegacyAndV2Fallback:
    """Tests for the start() legacy → v2 fallback (issue #36)."""

    def _connected_client(self) -> NarwalClient:
        client = NarwalClient("127.0.0.1")
        client._ws = AsyncMock()
        client._connected = True
        return client

    @pytest.mark.asyncio
    async def test_start_returns_legacy_response_on_success(self) -> None:
        """If legacy payload succeeds (code=1), no v2 retry happens."""
        client = self._connected_client()
        success = CommandResponse(result_code=CommandResult.SUCCESS)

        with patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = success
            result = await client.start()

        assert result is success
        mock_send.assert_awaited_once()  # no fallback fired

    @pytest.mark.asyncio
    async def test_start_falls_back_to_v2_on_not_applicable(self) -> None:
        """NOT_APPLICABLE from legacy triggers v2 retry with cached rooms."""
        client = self._connected_client()
        client.state.map_data = MapData(rooms=[
            RoomInfo(room_id=11),
            RoomInfo(room_id=14),
        ])

        not_applicable = CommandResponse(result_code=CommandResult.NOT_APPLICABLE)
        success = CommandResponse(result_code=CommandResult.SUCCESS)

        with patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send:
            mock_send.side_effect = [not_applicable, success]
            result = await client.start()

        assert mock_send.await_count == 2
        # Second call's payload must be v2 (different bytes from legacy)
        second_payload = mock_send.await_args_list[1].kwargs.get("payload")
        assert second_payload is not None
        assert second_payload != client._DEFAULT_CLEAN_PAYLOAD
        assert result is success

    @pytest.mark.asyncio
    async def test_start_returns_not_applicable_when_no_map_cached(self) -> None:
        """NOT_APPLICABLE without a cached map surfaces the error
        instead of crashing — user just needs to load the map first."""
        client = self._connected_client()
        assert client.state.map_data is None

        not_applicable = CommandResponse(result_code=CommandResult.NOT_APPLICABLE)

        with patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = not_applicable
            result = await client.start()

        mock_send.assert_awaited_once()  # no v2 retry without rooms
        assert result.result_code == CommandResult.NOT_APPLICABLE

    @pytest.mark.asyncio
    async def test_start_skips_v2_when_map_has_no_room_ids(self) -> None:
        """Map present but every room_id is 0 — don't send junk."""
        client = self._connected_client()
        client.state.map_data = MapData(rooms=[RoomInfo(room_id=0)])

        not_applicable = CommandResponse(result_code=CommandResult.NOT_APPLICABLE)

        with patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = not_applicable
            result = await client.start()

        mock_send.assert_awaited_once()
        assert result.result_code == CommandResult.NOT_APPLICABLE
