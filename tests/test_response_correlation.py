"""Field5 responses are matched to requests by send order (#108).

Responses carry no topic, so the client keeps an ordered record of the
requests still owed an answer. These tests replay the failure from #108 and
the fire-and-forget sends that can put an unclaimed ack on the wire.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from narwal_client.client import NarwalClient, NarwalCommandError
from narwal_client.const import (
    LATE_RESPONSE_GRACE,
    TOPIC_CMD_APP_HEARTBEAT,
    TOPIC_CMD_GET_BASE_STATUS,
    TOPIC_CMD_GET_MAP,
)
from narwal_client.protocol import PROTOBUF_FIELD5_TAG


def _response(payload: bytes) -> bytes:
    """A field5 command response: empty topic, header byte 2 (PROTOCOL.md §2)."""
    return bytes([0x01, 0x02, PROTOBUF_FIELD5_TAG, 0x00]) + payload


# robot_base_status answer: field 2 is an int (1120403456), as captured in #108
STATUS_RESPONSE = _response(b"\x10" + NarwalClient._encode_varint(1120403456))
# get_map answer: field 2 is a message
MAP_RESPONSE = _response(bytes.fromhex("1200"))
ACK = _response(b"\x08\x01")


def _listening_client() -> NarwalClient:
    client = NarwalClient("10.0.0.1", device_id="device")
    client._ws = AsyncMock()
    client._connected.set()
    client._listener_active = True
    return client


async def _deliver_after(client: NarwalClient, frame: bytes, delay: float) -> None:
    await asyncio.sleep(delay)
    await client._handle_message(frame)


@pytest.mark.asyncio
async def test_late_response_is_not_handed_to_the_next_command() -> None:
    """The exact #108 sequence: A times out, A's answer lands while B waits."""
    client = _listening_client()

    with pytest.raises(NarwalCommandError):
        await client.send_command(TOPIC_CMD_GET_BASE_STATUS, timeout=0.05)

    async def robot() -> None:
        await asyncio.sleep(0.02)
        await client._handle_message(STATUS_RESPONSE)  # A's, late
        await asyncio.sleep(0.02)
        await client._handle_message(MAP_RESPONSE)  # B's own

    robot_task = asyncio.create_task(robot())
    response = await client.send_command(TOPIC_CMD_GET_MAP, timeout=1.0)
    await robot_task

    assert response.raw_payload == MAP_RESPONSE[4:]


@pytest.mark.asyncio
async def test_timed_out_command_stops_holding_its_place_after_grace() -> None:
    """A command whose answer never comes cannot swallow responses forever."""
    client = _listening_client()

    with pytest.raises(NarwalCommandError):
        await client.send_command(TOPIC_CMD_GET_BASE_STATUS, timeout=0.01)

    (orphan,) = client._expected_responses
    assert orphan.expires - time.monotonic() > LATE_RESPONSE_GRACE - 1
    orphan.expires = time.monotonic() - 1  # grace used up, answer never came

    delivery = asyncio.create_task(_deliver_after(client, MAP_RESPONSE, 0.01))
    response = await client.send_command(TOPIC_CMD_GET_MAP, timeout=1.0)
    await delivery

    assert response.raw_payload == MAP_RESPONSE[4:]


@pytest.mark.asyncio
async def test_wake_burst_acks_are_not_taken_for_a_command_response() -> None:
    """Burst acks arriving after a command is sent are discarded, in order."""
    client = _listening_client()
    with patch("narwal_client.client.asyncio.sleep", new=AsyncMock()):
        await client._send_wake_burst()

    # The heartbeat is never acknowledged, so it holds no place
    acked = [expected.topic for expected in client._expected_responses]
    assert TOPIC_CMD_APP_HEARTBEAT not in acked
    assert len(acked) == 4

    async def robot() -> None:
        await asyncio.sleep(0.01)
        for _ in acked:
            await client._handle_message(ACK)
        await client._handle_message(MAP_RESPONSE)

    robot_task = asyncio.create_task(robot())
    response = await client.send_command(TOPIC_CMD_GET_MAP, timeout=1.0)
    await robot_task

    assert response.raw_payload == MAP_RESPONSE[4:]


@pytest.mark.asyncio
async def test_heartbeat_does_not_swallow_the_next_response() -> None:
    """The robot never answers the app heartbeat, so it must not hold a place."""
    client = _listening_client()
    await client._send_unawaited(TOPIC_CMD_APP_HEARTBEAT, b"\x08\x01")

    delivery = asyncio.create_task(_deliver_after(client, MAP_RESPONSE, 0.01))
    response = await client.send_command(TOPIC_CMD_GET_MAP, timeout=1.0)
    await delivery

    assert response.raw_payload == MAP_RESPONSE[4:]


@pytest.mark.asyncio
async def test_failed_send_releases_its_place() -> None:
    """A frame that never left cannot be owed a response."""
    client = _listening_client()
    client._ws.send = AsyncMock(side_effect=ConnectionError("gone"))

    with pytest.raises(ConnectionError):
        await client.send_command(TOPIC_CMD_GET_MAP, timeout=0.1)

    assert not client._expected_responses


@pytest.mark.asyncio
async def test_unsolicited_response_is_dropped() -> None:
    client = _listening_client()
    await client._handle_message(ACK)
    assert not client._expected_responses


@pytest.mark.asyncio
async def test_direct_read_path_discards_late_response() -> None:
    """Without the listener, send_command reads the socket itself; same rule."""
    client = NarwalClient("10.0.0.1", device_id="device")
    client._ws = AsyncMock()
    client._connected.set()

    async def silent() -> bytes:
        await asyncio.sleep(10)
        return b""

    client._ws.recv = AsyncMock(side_effect=silent)

    with pytest.raises(NarwalCommandError):
        await client.send_command(TOPIC_CMD_GET_BASE_STATUS, timeout=0.05)

    client._ws.recv = AsyncMock(side_effect=[STATUS_RESPONSE, MAP_RESPONSE])
    response = await client.send_command(TOPIC_CMD_GET_MAP, timeout=1.0)

    assert response.raw_payload == MAP_RESPONSE[4:]


@pytest.mark.asyncio
async def test_reconnect_forgets_what_the_old_socket_was_owed() -> None:
    client = _listening_client()
    await client._send_unawaited("common/notify_app_event", b"\x08\x01")
    assert client._expected_responses

    with patch("narwal_client.client.websockets.connect", new=AsyncMock()):
        await client.connect()

    assert not client._expected_responses

