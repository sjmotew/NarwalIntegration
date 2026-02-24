"""Tests for narwal_client.protocol — frame parsing and building."""

from __future__ import annotations

import pytest

from narwal_client.protocol import NarwalMessage, ProtocolError, build_frame, parse_frame


class TestParseFrame:
    """Tests for parse_frame()."""

    def test_parse_valid_frame(self, sample_frame: bytes, sample_topic: str) -> None:
        msg = parse_frame(sample_frame)
        assert msg.topic == sample_topic
        assert msg.payload == b"\x18\x01"
        # Auto-calculated: topic_len(66) + 2 = 68 = 0x44
        assert msg.header_byte == len(sample_topic.encode("utf-8")) + 2
        assert msg.field_tag == 0x22
        assert isinstance(msg, NarwalMessage)

    def test_parse_short_topic(self) -> None:
        msg = parse_frame(build_frame("a/b", b"\x08\x01"))
        assert msg.topic == "a/b"
        assert msg.short_topic == "a/b"

    def test_short_topic_extraction(self, sample_frame: bytes) -> None:
        msg = parse_frame(sample_frame)
        assert msg.short_topic == "status/working_status"

    def test_parse_preserves_raw(self, sample_frame: bytes) -> None:
        msg = parse_frame(sample_frame)
        assert msg.raw == sample_frame

    def test_parse_empty_payload(self) -> None:
        frame = build_frame("test/topic", b"")
        msg = parse_frame(frame)
        assert msg.payload == b""
        assert msg.topic == "test/topic"

    def test_parse_custom_header_byte(self) -> None:
        frame = build_frame("test/topic", b"\x08\x01", header_byte=0xAB)
        msg = parse_frame(frame)
        assert msg.header_byte == 0xAB

    def test_parse_too_short_raises(self) -> None:
        with pytest.raises(ProtocolError, match="too short"):
            parse_frame(b"\x01\x00\x22")

    def test_parse_wrong_frame_type_raises(self) -> None:
        with pytest.raises(ProtocolError, match="frame type"):
            parse_frame(b"\x02\x00\x22\x01X")

    def test_parse_wrong_protobuf_tag_raises(self) -> None:
        with pytest.raises(ProtocolError, match="protobuf field tag"):
            parse_frame(b"\x01\x00\x33\x01X")

    def test_parse_truncated_topic_raises(self) -> None:
        # Says topic is 10 bytes but only 1 byte follows
        with pytest.raises(ProtocolError, match="truncated"):
            parse_frame(b"\x01\x00\x22\x0aX")


class TestBuildFrame:
    """Tests for build_frame()."""

    def test_build_roundtrip(self, sample_topic: str, sample_payload: bytes) -> None:
        frame = build_frame(sample_topic, sample_payload)
        msg = parse_frame(frame)
        assert msg.topic == sample_topic
        assert msg.payload == sample_payload

    def test_build_frame_structure(self) -> None:
        frame = build_frame("abc", b"\x08\x01")
        assert frame[0] == 0x01  # frame type
        assert frame[1] == 5  # auto: topic_len(3) + 2
        assert frame[2] == 0x22  # protobuf tag
        assert frame[3] == 3  # topic length
        assert frame[4:7] == b"abc"
        assert frame[7:] == b"\x08\x01"

    def test_build_empty_topic_raises(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            build_frame("", b"\x08\x01")

    def test_build_long_topic_raises(self) -> None:
        with pytest.raises(ValueError, match="too long"):
            build_frame("x" * 256, b"\x08\x01")

    def test_build_custom_header(self) -> None:
        frame = build_frame("t", b"", header_byte=0xFF)
        assert frame[1] == 0xFF
