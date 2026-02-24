"""Frame parsing and building for the Narwal WebSocket protocol.

Frame structure:
    Byte 0:    0x01 (frame type)
    Byte 1:    secondary header byte
    Byte 2:    0x22 (protobuf field 4, wire type 2)
    Byte 3:    topic_length (uint8)
    Bytes 4+:  UTF-8 topic string
    Remaining: protobuf-encoded payload
"""

from __future__ import annotations

from dataclasses import dataclass

from .const import FRAME_TYPE_BYTE, PROTOBUF_FIELD_TAG, TOPIC_DATA_OFFSET, TOPIC_LENGTH_OFFSET

# Field 5, wire type 2 (length-delimited) — used by some message types (possibly responses)
PROTOBUF_FIELD5_TAG = 0x2A


class ProtocolError(Exception):
    """Raised when a frame cannot be parsed."""


@dataclass(frozen=True)
class NarwalMessage:
    """A parsed Narwal WebSocket message."""

    topic: str
    payload: bytes
    header_byte: int  # secondary header (byte 1) = topic_len + 2
    field_tag: int  # protobuf field tag byte (0x22=field4, 0x2a=field5)
    raw: bytes  # original full frame

    @property
    def short_topic(self) -> str:
        """Return the topic without the prefix and device ID.

        '/{product_key}/{device_id}/status/working_status' → 'status/working_status'
        """
        parts = self.topic.split("/")
        # Skip empty string, prefix, device_id → rejoin the rest
        if len(parts) >= 4:
            return "/".join(parts[3:])
        return self.topic


def parse_frame(data: bytes) -> NarwalMessage:
    """Parse a raw WebSocket binary frame into a NarwalMessage.

    Args:
        data: Raw binary frame received from the WebSocket.

    Returns:
        Parsed NarwalMessage with topic and protobuf payload.

    Raises:
        ProtocolError: If the frame structure is invalid.
    """
    if len(data) < 4:
        raise ProtocolError(f"Frame too short: {len(data)} bytes (minimum 4)")

    if data[0] != FRAME_TYPE_BYTE:
        raise ProtocolError(f"Invalid frame type byte: 0x{data[0]:02x} (expected 0x01)")

    if data[2] not in (PROTOBUF_FIELD_TAG, PROTOBUF_FIELD5_TAG):
        raise ProtocolError(f"Invalid protobuf field tag: 0x{data[2]:02x} (expected 0x22 or 0x2a)")

    header_byte = data[1]
    topic_len = data[TOPIC_LENGTH_OFFSET]

    topic_end = TOPIC_DATA_OFFSET + topic_len
    if len(data) < topic_end:
        raise ProtocolError(
            f"Frame truncated: expected {topic_end} bytes for topic, got {len(data)}"
        )

    try:
        topic = data[TOPIC_DATA_OFFSET:topic_end].decode("utf-8")
    except UnicodeDecodeError as e:
        raise ProtocolError(f"Invalid UTF-8 in topic: {e}") from e

    payload = data[topic_end:]

    return NarwalMessage(
        topic=topic,
        payload=payload,
        header_byte=header_byte,
        field_tag=data[2],
        raw=bytes(data),
    )


def build_frame(topic: str, payload: bytes, header_byte: int | None = None) -> bytes:
    """Build a binary frame to send to the Narwal vacuum.

    Args:
        topic: Full MQTT-style topic string.
        payload: Protobuf-encoded payload bytes.
        header_byte: Secondary header byte. If None (default), auto-calculated
            as topic_length + 2 (matching the observed broadcast format where
            byte 1 = length of the protobuf field 4 TLV encoding).

    Returns:
        Complete binary frame ready to send over WebSocket.

    Raises:
        ValueError: If the topic is too long (>255 bytes) or empty.
    """
    topic_bytes = topic.encode("utf-8")
    if len(topic_bytes) == 0:
        raise ValueError("Topic cannot be empty")
    if len(topic_bytes) > 255:
        raise ValueError(f"Topic too long: {len(topic_bytes)} bytes (max 255)")

    if header_byte is None:
        # Auto-calculate: 1 (tag 0x22) + 1 (length byte) + topic_len
        header_byte = len(topic_bytes) + 2

    frame = bytearray()
    frame.append(FRAME_TYPE_BYTE)
    frame.append(header_byte & 0xFF)
    frame.append(PROTOBUF_FIELD_TAG)
    frame.append(len(topic_bytes))
    frame.extend(topic_bytes)
    frame.extend(payload)

    return bytes(frame)
