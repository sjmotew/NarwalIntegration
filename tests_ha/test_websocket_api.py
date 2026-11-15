"""The websocket commands, through Home Assistant's real websocket API.

The stub suite calls the handlers directly; this checks that HA accepts the
registration, validates the schema and delivers the answer to a client.
"""

from __future__ import annotations

import base64
import struct

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.narwal.narwal_client.models import MapData, MapDisplayData


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_trajectory_over_the_websocket(
    hass: HomeAssistant,
    hass_ws_client,
    flow_entry: MockConfigEntry,
    silent_robot: dict,
) -> None:
    """A client gets the trail as grid points, in the documented shape."""
    await _setup(hass, flow_entry)
    state = flow_entry.runtime_data.client.state
    state.map_data = MapData(map_id=1, created_at=42, width=50, height=40, resolution=60)
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
    assert struct.unpack("<3f", base64.b64decode(result["x"])) == (1.0, 2.0, 3.0)
    assert struct.unpack("<3f", base64.b64decode(result["y"])) == (4.0, 5.0, 6.0)


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
