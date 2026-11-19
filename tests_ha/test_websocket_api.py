"""The websocket commands, through Home Assistant's real websocket API.

The stub suite calls the handlers directly; this checks that HA accepts the
registration, validates the schema and delivers the answer to a client.
"""

from __future__ import annotations

import base64
import struct

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import CLIENT_ID, MockConfigEntry, MockUser

from custom_components.narwal.const import CONF_DEVICE_ID
from custom_components.narwal.narwal_client.models import MapData, MapDisplayData


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _map() -> MapData:
    return MapData(
        map_id=1, created_at=42, width=50, height=40, resolution=60, compressed_map=b"\x01"
    )


def _with_trail(entry: MockConfigEntry) -> None:
    state = entry.runtime_data.client.state
    state.map_data = _map()
    state.map_display_data = MapDisplayData(
        trajectory_x_values=struct.pack("<2f", 1.0, 2.0),
        trajectory_y_values=struct.pack("<2f", 4.0, 5.0),
        trajectory_signature=(2, 1, 2),
    )


def _entity_id(hass: HomeAssistant, entry: MockConfigEntry, domain: str) -> str:
    return next(
        registry_entry.entity_id
        for registry_entry in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if registry_entry.domain == domain
    )


async def test_trajectory_over_the_websocket(
    hass: HomeAssistant,
    hass_ws_client,
    flow_entry: MockConfigEntry,
    silent_robot: dict,
) -> None:
    """A client gets the trail as grid points, in the documented shape."""
    await _setup(hass, flow_entry)
    state = flow_entry.runtime_data.client.state
    state.map_data = _map()
    state.map_display_data = MapDisplayData(
        trajectory_x_values=struct.pack("<3f", 1.0, 2.0, 3.0),
        trajectory_y_values=struct.pack("<3f", 4.0, 5.0, 6.0),
        trajectory_signature=(3, 1, 2),
    )

    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "narwal/trajectory"})
    msg = await client.receive_json()

    assert msg["success"], msg
    result = msg["result"]
    assert result["version"] == 1
    assert result["entry_id"] == flow_entry.entry_id
    assert result["map"] == {
        "id": 1,
        "created_at": 42,
        "width": 50,
        "height": 40,
        "resolution_mm": 60,
    }
    assert result["count"] == 3
    # Trail (count, checksums), then map id, created_at, origin and size.
    assert result["signature"] == [3, 1, 2, 1, 42, 0, 0, 50, 40]
    assert struct.unpack("<3f", base64.b64decode(result["x"])) == (1.0, 2.0, 3.0)
    assert struct.unpack("<3f", base64.b64decode(result["y"])) == (4.0, 5.0, 6.0)


async def test_trajectory_takes_back_a_null_signature(
    hass: HomeAssistant,
    hass_ws_client,
    flow_entry: MockConfigEntry,
    silent_robot: dict,
) -> None:
    """A card that polls before any clean sends back the null it got."""
    await _setup(hass, flow_entry)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": "narwal/trajectory"})
    msg = await client.receive_json()
    assert msg["success"], msg
    assert msg["result"]["signature"] is None

    await client.send_json_auto_id({"type": "narwal/trajectory", "signature": None})
    msg = await client.receive_json()
    assert msg["success"], msg
    assert msg["result"]["unchanged"] is False
    assert msg["result"]["signature"] is None

    await client.send_json_auto_id({"type": "narwal/trajectory", "signature": "abc"})
    msg = await client.receive_json()
    assert msg["error"]["code"] == "invalid_format"


async def test_trajectory_rejects_unknown_fields_and_entries(
    hass: HomeAssistant,
    hass_ws_client,
    flow_entry: MockConfigEntry,
    silent_robot: dict,
) -> None:
    """The schema is enforced by HA; an unknown entry is not_found."""
    await _setup(hass, flow_entry)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": "narwal/trajectory", "bogus": 1})
    msg = await client.receive_json()
    assert not msg["success"]
    assert msg["error"]["code"] == "invalid_format"

    await client.send_json_auto_id({"type": "narwal/trajectory", "entry_id": "missing"})
    msg = await client.receive_json()
    assert not msg["success"]
    assert msg["error"]["code"] == "not_found"


async def test_trajectory_by_entity_and_signature(
    hass: HomeAssistant,
    hass_ws_client,
    flow_entry: MockConfigEntry,
    silent_robot: dict,
) -> None:
    """A card names its camera; a current signature skips the points."""
    await _setup(hass, flow_entry)
    state = flow_entry.runtime_data.client.state
    state.map_data = _map()
    state.map_display_data = MapDisplayData(
        trajectory_x_values=struct.pack("<2f", 1.0, 2.0),
        trajectory_y_values=struct.pack("<2f", 4.0, 5.0),
        trajectory_signature=(2, 1, 2),
    )
    entity_id = next(
        entry.entity_id
        for entry in er.async_entries_for_config_entry(er.async_get(hass), flow_entry.entry_id)
        if entry.domain == "camera"
    )
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": "narwal/trajectory", "entity_id": entity_id})
    msg = await client.receive_json()
    assert msg["success"], msg
    assert msg["result"]["entry_id"] == flow_entry.entry_id
    assert msg["result"]["unchanged"] is False
    assert msg["result"]["count"] == 2

    signature = msg["result"]["signature"]

    await client.send_json_auto_id(
        {"type": "narwal/trajectory", "entity_id": entity_id, "signature": signature}
    )
    msg = await client.receive_json()
    assert msg["success"], msg
    assert msg["result"]["unchanged"] is True
    assert "x" not in msg["result"]

    await client.send_json_auto_id(
        {"type": "narwal/trajectory", "entity_id": entity_id, "entry_id": flow_entry.entry_id}
    )
    msg = await client.receive_json()
    assert not msg["success"]
    assert msg["error"]["code"] == "invalid_format"

    await client.send_json_auto_id({"type": "narwal/trajectory", "entity_id": "camera.nothing"})
    msg = await client.receive_json()
    assert not msg["success"]
    assert msg["error"]["code"] == "not_found"


async def test_trajectory_with_two_robots_needs_a_target(
    hass: HomeAssistant,
    hass_ws_client,
    flow_entry: MockConfigEntry,
    silent_robot: dict,
) -> None:
    """Two configured robots: no answer for "the first one", the camera picks one."""
    await _setup(hass, flow_entry)
    second = MockConfigEntry(
        domain=flow_entry.domain,
        title="Narwal Flow 2",
        unique_id="fedcba9876543210fedcba9876543210",
        data={
            **flow_entry.data,
            "host": "192.0.2.11",
            CONF_DEVICE_ID: "fedcba9876543210fedcba9876543210",
        },
        options=dict(flow_entry.options),
        version=2,
    )
    await _setup(hass, second)
    _with_trail(second)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": "narwal/trajectory"})
    msg = await client.receive_json()
    assert msg["error"]["code"] == "target_required"

    await client.send_json_auto_id(
        {"type": "narwal/trajectory", "entity_id": _entity_id(hass, second, "camera")}
    )
    msg = await client.receive_json()
    assert msg["success"], msg
    assert (msg["result"]["entry_id"], msg["result"]["count"]) == (second.entry_id, 2)


async def test_trajectory_needs_read_permission_on_the_vacuum(
    hass: HomeAssistant,
    hass_ws_client,
    hass_read_only_access_token: str,
    flow_entry: MockConfigEntry,
    silent_robot: dict,
) -> None:
    """A read-only user gets the trail; a user who may read only the camera does not."""
    await _setup(hass, flow_entry)
    _with_trail(flow_entry)

    reader = await hass_ws_client(hass, hass_read_only_access_token)
    await reader.send_json_auto_id({"type": "narwal/trajectory"})
    msg = await reader.receive_json()
    assert msg["success"], msg
    assert msg["result"]["count"] == 2

    user = MockUser().add_to_hass(hass)
    user.mock_policy(
        {"entities": {"entity_ids": {_entity_id(hass, flow_entry, "camera"): True}}}
    )
    refresh_token = await hass.auth.async_create_refresh_token(user, CLIENT_ID)
    camera_only = await hass_ws_client(hass, hass.auth.async_create_access_token(refresh_token))
    await camera_only.send_json_auto_id({"type": "narwal/trajectory"})
    msg = await camera_only.receive_json()
    assert msg["error"]["code"] == "unauthorized"

    # Permission before the target: an unknown id answers like a robot the user may not read.
    for target in ({"entity_id": "camera.gone"}, {"entry_id": "nope"}):
        await camera_only.send_json_auto_id({"type": "narwal/trajectory", **target})
        msg = await camera_only.receive_json()
        assert msg["error"]["code"] == "unauthorized", target
