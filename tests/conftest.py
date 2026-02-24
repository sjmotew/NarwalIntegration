"""Shared test fixtures for Narwal integration tests."""

from __future__ import annotations

import pytest

from narwal_client.protocol import build_frame


# --- Sample frames for testing ---

# A minimal valid frame with topic "status/working_status" and a small protobuf payload
SAMPLE_TOPIC = "/QoEsI5qYXO/test_device_id_000000000000000/status/working_status"
SAMPLE_PAYLOAD = b"\x18\x01"  # field 3, varint 1 (IDLE_DOCKED)


@pytest.fixture
def sample_frame() -> bytes:
    """A valid Narwal frame with working_status topic."""
    return build_frame(SAMPLE_TOPIC, SAMPLE_PAYLOAD)


@pytest.fixture
def sample_topic() -> str:
    return SAMPLE_TOPIC


@pytest.fixture
def sample_payload() -> bytes:
    return SAMPLE_PAYLOAD
