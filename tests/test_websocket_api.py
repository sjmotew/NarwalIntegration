"""Tests for the Narwal websocket commands (custom_components.narwal.websocket_api)."""

from __future__ import annotations

import base64
import math
import struct
from unittest.mock import MagicMock

import tests.ha_stubs

tests.ha_stubs.install()

from custom_components.narwal.camera import NarwalMapCamera  # noqa: E402
from custom_components.narwal.const import CONF_MAP_ROTATION, CONF_MAP_ZOOM, DOMAIN  # noqa: E402
from custom_components.narwal.coordinator import NATIVE_TRAJECTORY_MAX_POINTS  # noqa: E402
from custom_components.narwal.websocket_api import (  # noqa: E402
    ANSWER_VERSION,
    TRAJECTORY_COMMAND,
    trajectory_payload,
    ws_trajectory,
)
from narwal_client.models import MapData, MapDisplayData, NarwalState  # noqa: E402


def _float_stream(values: list[float]) -> bytes:
    """Encode a packed float32 stream as display_map field 2 uses it."""
    return struct.pack(f"<{len(values)}f", *values)


def _unpack(encoded: str) -> list[float]:
    raw = base64.b64decode(encoded)
    return list(struct.unpack(f"<{len(raw) // 4}f", raw))


def _display(
    xs: list[float], ys: list[float], breaks: tuple[int, ...] = ()
) -> MapDisplayData:
    return MapDisplayData(
        trajectory_x_values=_float_stream(xs),
        trajectory_y_values=_float_stream(ys),
        trajectory_signature=(len(xs), 7, 9),
        trajectory_breaks=breaks,
    )


def _map() -> MapData:
    return MapData(
        map_id=2,
        created_at=1_700_000_000,
        width=100,
        height=80,
        resolution=60,
        origin_x=2,
        origin_y=4,
    )


def _hass_with(*coordinators: MagicMock) -> MagicMock:
    hass = MagicMock()
    hass.data = {DOMAIN: {c.config_entry.entry_id: c for c in coordinators}}

    async def _executor(func, *args):
        return func(*args)

    hass.async_add_executor_job = _executor
    return hass


def _coordinator(entry_id: str, state: NarwalState, options: dict | None = None) -> MagicMock:
    coordinator = MagicMock()
    coordinator.config_entry.entry_id = entry_id
    coordinator.config_entry.options = options or {}
    coordinator.client.state = state
    return coordinator


def test_command_schema() -> None:
    """The command takes an optional entry_id and nothing else."""
    assert TRAJECTORY_COMMAND == "narwal/trajectory"
    assert set(ws_trajectory._ws_schema) == {"type", "entry_id"}


def test_payload_without_map() -> None:
    """No static map yet: version, view and an empty trail, no map block."""
    answer = trajectory_payload("e1", None, None, {})

    assert answer == {
        "version": ANSWER_VERSION,
        "entry_id": "e1",
        "map": None,
        "rotation": 0,
        "zoom": 1.0,
        "signature": None,
        "count": 0,
        "x": "",
        "y": "",
    }


def test_payload_with_map_but_no_trail() -> None:
    """A docked robot: the map is described, the trail is empty."""
    answer = trajectory_payload("e1", _map(), MapDisplayData(robot_x=4.0), {})

    assert answer["map"] == {
        "id": 2,
        "created_at": 1_700_000_000,
        "width": 100,
        "height": 80,
        "resolution_mm": 60,
    }
    assert answer["count"] == 0
    assert answer["signature"] is None


def test_payload_points_are_the_ones_the_camera_draws() -> None:
    """Grid conversion, bounds and segment breaks match the rendered trail."""
    static_map = _map()
    # Second point is a duplicate (dropped), the fourth lies outside the map
    # (break), the break index splits the route once more.
    display = _display(
        [3.0, 3.05, 4.0, 500.0, 6.0, 8.0, 9.0],
        [5.0, 5.05, 6.0, 500.0, 7.0, 9.0, 10.0],
        breaks=(6,),
    )

    answer = trajectory_payload("e1", static_map, display, {})

    expected = NarwalMapCamera._native_trajectory_grid_points(
        display.trajectory_render_points(), static_map
    )
    xs, ys = _unpack(answer["x"]), _unpack(answer["y"])
    assert answer["count"] == len(expected) == len(xs) == len(ys)
    assert answer["signature"] == [7, 7, 9]
    for (ex, ey), x, y in zip(expected, xs, ys, strict=True):
        if math.isnan(ex):
            assert math.isnan(x) and math.isnan(y)
        else:
            assert (x, y) == (ex, ey)
    assert (xs[0], ys[0]) == (1.0, 1.0)
    assert sum(math.isnan(x) for x in xs) == 2


def test_payload_is_not_decimated_like_the_path_attribute() -> None:
    """The full trail comes back, not the 400-point copy of the camera attribute."""
    count = 2_000
    static_map = MapData(width=count + 10, height=10)
    display = _display([float(i) for i in range(count)], [1.0] * count)

    answer = trajectory_payload("e1", static_map, display, {})

    assert answer["count"] == count
    assert _unpack(answer["x"])[-1] == float(count - 1)
    assert count < NATIVE_TRAJECTORY_MAX_POINTS


def test_payload_view_follows_the_camera_options() -> None:
    """Rotation and zoom are the camera's, with its fallbacks for bad values."""
    answer = trajectory_payload(
        "e1", None, None, {CONF_MAP_ROTATION: 450, CONF_MAP_ZOOM: "1.5"}
    )
    assert (answer["rotation"], answer["zoom"]) == (90, 1.5)

    answer = trajectory_payload("e1", None, None, {CONF_MAP_ROTATION: 45, CONF_MAP_ZOOM: 9})
    assert (answer["rotation"], answer["zoom"]) == (0, 2.0)


async def test_ws_trajectory_defaults_to_the_first_entry() -> None:
    state = NarwalState()
    state.map_data = _map()
    first = _coordinator("e1", state, {CONF_MAP_ZOOM: 1.25})
    second = _coordinator("e2", NarwalState())
    connection = MagicMock()

    await ws_trajectory(_hass_with(first, second), connection, {"id": 5})

    connection.send_error.assert_not_called()
    msg_id, payload = connection.send_result.call_args.args
    assert msg_id == 5
    assert payload["entry_id"] == "e1"
    assert payload["zoom"] == 1.25
    assert payload["map"]["id"] == 2


async def test_ws_trajectory_named_entry() -> None:
    first = _coordinator("e1", NarwalState())
    second = _coordinator("e2", NarwalState())
    connection = MagicMock()

    await ws_trajectory(_hass_with(first, second), connection, {"id": 6, "entry_id": "e2"})

    assert connection.send_result.call_args.args[1]["entry_id"] == "e2"


async def test_ws_trajectory_unknown_entry() -> None:
    connection = MagicMock()

    await ws_trajectory(
        _hass_with(_coordinator("e1", NarwalState())),
        connection,
        {"id": 7, "entry_id": "nope"},
    )

    connection.send_result.assert_not_called()
    assert connection.send_error.call_args.args[:2] == (7, "not_found")


async def test_ws_trajectory_without_entries() -> None:
    hass = MagicMock()
    hass.data = {}
    connection = MagicMock()

    await ws_trajectory(hass, connection, {"id": 8})

    assert connection.send_error.call_args.args[:2] == (8, "not_found")
