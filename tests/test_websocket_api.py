"""Tests for the Narwal websocket commands (custom_components.narwal.websocket_api)."""

from __future__ import annotations

import base64
import math
import struct
from array import array
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest

import tests.ha_stubs

tests.ha_stubs.install()

from homeassistant.exceptions import Unauthorized  # noqa: E402

from custom_components.narwal.const import (  # noqa: E402
    CONF_MAP_ROTATION,
    CONF_MAP_ZOOM,
    DOMAIN,
    NATIVE_TRAJECTORY_MAX_POINTS,
)
from custom_components.narwal.websocket_api import (  # noqa: E402
    ANSWER_VERSION,
    ERR_TARGET_REQUIRED,
    TRAJECTORY_COMMAND,
    _pack,
    trajectory_payload,
    ws_trajectory,
)
from narwal_client.models import MapData, MapDisplayData, NarwalState  # noqa: E402

NAN = float("nan")
#: The map's part of every signature in these tests: id, created_at, origin, size.
MAP_KEY = [2, 1_700_000_000, 2, 4, 100, 80]


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


def _map(map_id: int = 2) -> MapData:
    return MapData(
        map_id=map_id,
        created_at=1_700_000_000,
        width=100,
        height=80,
        resolution=60,
        origin_x=2,
        origin_y=4,
        compressed_map=b"\x01",
    )


def _hass_with(*coordinators: MagicMock, configured: list[str] | None = None) -> MagicMock:
    """Home Assistant with these coordinators loaded and ``configured`` entry ids set up."""
    hass = MagicMock()
    hass.data = {DOMAIN: {c.config_entry.entry_id: c for c in coordinators}}
    entry_ids = (
        configured if configured is not None else [c.config_entry.entry_id for c in coordinators]
    )
    hass.config_entries.async_entries.return_value = [
        MagicMock(entry_id=entry_id) for entry_id in entry_ids
    ]

    async def _executor(func, *args):
        return func(*args)

    hass.async_add_executor_job = MagicMock(side_effect=_executor)
    return hass


def _coordinator(entry_id: str, state: NarwalState, options: dict | None = None) -> MagicMock:
    coordinator = MagicMock()
    coordinator.config_entry.entry_id = entry_id
    coordinator.config_entry.options = options or {}
    coordinator.client.state = state
    return coordinator


def _registry(**entries: str | None) -> MagicMock:
    """Entity registry mapping entity ids to config entry ids."""
    registry = MagicMock()

    def _get(entity_id: str):
        if entity_id not in entries:
            return None
        return MagicMock(config_entry_id=entries[entity_id])

    registry.async_get = _get
    return registry


def _state_with_trail() -> NarwalState:
    state = NarwalState()
    state.map_data = _map()
    state.map_display_data = _display([3.0, 4.0], [5.0, 6.0])
    return state


def test_command_schema() -> None:
    """The command takes one optional target and an optional signature."""
    assert TRAJECTORY_COMMAND == "narwal/trajectory"
    assert {str(key) for key in ws_trajectory._ws_schema} == {
        "type",
        "entity_id",
        "entry_id",
        "signature",
    }


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
        "unchanged": False,
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


def test_payload_reports_a_map_that_decoded_empty() -> None:
    """A reply that decoded to no map (#108) is told apart from "no map yet"."""
    display = _display([3.0, 4.0], [5.0, 6.0])

    answer = trajectory_payload("e1", MapData(map_id=2), display, {})

    assert answer["map"] is not None
    assert (answer["map"]["width"], answer["map"]["height"]) == (0, 0)
    assert answer["count"] == 0
    assert answer["signature"] is None


def test_payload_on_a_map_without_its_image_carries_no_trail() -> None:
    """Where the camera draws no frame, the command hands out no points either."""
    static_map = _map()
    static_map.compressed_map = b""

    answer = trajectory_payload("e1", static_map, _display([3.0, 4.0], [5.0, 6.0]), {})

    assert answer["map"]["id"] == 2
    assert (answer["count"], answer["signature"]) == (0, None)


def test_payload_points_are_the_ones_the_camera_draws() -> None:
    """Grid conversion, bounds, segment breaks and de-duplication.

    Origin (2, 4) on a 100x80 map. The second point moves less than 0.25 cells
    (dropped), the fourth lies outside the map (a break), and the break index
    before the last point splits the route once more.
    """
    display = _display(
        [3.0, 3.05, 4.0, 500.0, 6.0, 8.0, 9.0],
        [5.0, 5.05, 6.0, 500.0, 7.0, 9.0, 10.0],
        breaks=(6,),
    )

    answer = trajectory_payload("e1", _map(), display, {})

    xs, ys = _unpack(answer["x"]), _unpack(answer["y"])
    expected = [(1.0, 1.0), (2.0, 2.0), (NAN, NAN), (4.0, 3.0), (6.0, 5.0), (NAN, NAN), (7.0, 6.0)]
    assert answer["count"] == len(expected) == len(xs) == len(ys)
    assert answer["signature"] == [7, 7, 9, *MAP_KEY]
    for (ex, ey), x, y in zip(expected, xs, ys, strict=True):
        if math.isnan(ex):
            assert math.isnan(x) and math.isnan(y)
        else:
            assert (x, y) == (ex, ey)


def test_payload_is_not_decimated_like_the_path_attribute() -> None:
    """The full trail comes back, not the 400-point copy of the camera attribute."""
    count = 2_000
    static_map = MapData(map_id=2, width=count + 10, height=10, compressed_map=b"\x01")
    display = _display([float(i) for i in range(count)], [1.0] * count)

    answer = trajectory_payload("e1", static_map, display, {})

    assert answer["count"] == count
    assert _unpack(answer["x"])[-1] == float(count - 1)
    assert count < NATIVE_TRAJECTORY_MAX_POINTS


def test_payload_keeps_every_point_of_a_full_trail() -> None:
    """A trail at the retention cap keeps all its points although breaks add NaN pairs.

    The coordinator caps the retained trail; capping again on the trail plus
    its break markers would decimate the prefix and drop real points.
    """
    count = NATIVE_TRAJECTORY_MAX_POINTS
    static_map = MapData(map_id=2, width=count + 10, height=10, compressed_map=b"\x01")
    breaks = (10_000, 20_000, 30_000)
    display = _display([float(i) for i in range(count)], [1.0] * count, breaks=breaks)

    answer = trajectory_payload("e1", static_map, display, {})

    xs = _unpack(answer["x"])
    real = [x for x in xs if not math.isnan(x)]
    assert real == [float(i) for i in range(count)]
    assert answer["count"] == count + len(breaks) == len(xs)


def test_pack_is_little_endian_float32() -> None:
    assert base64.b64decode(_pack(array("f", [1.0, -2.5]))) == struct.pack("<2f", 1.0, -2.5)


def test_payload_view_follows_the_camera_options() -> None:
    """Rotation and zoom are the camera's, with its fallbacks for bad values."""
    answer = trajectory_payload(
        "e1", None, None, {CONF_MAP_ROTATION: 450, CONF_MAP_ZOOM: "1.5"}
    )
    assert (answer["rotation"], answer["zoom"]) == (90, 1.5)

    answer = trajectory_payload("e1", None, None, {CONF_MAP_ROTATION: 45, CONF_MAP_ZOOM: 9})
    assert (answer["rotation"], answer["zoom"]) == (0, 2.0)


async def test_ws_trajectory_answers_the_only_entry() -> None:
    state = NarwalState()
    state.map_data = _map()
    only = _coordinator("e1", state, {CONF_MAP_ZOOM: 1.25})
    connection = MagicMock()

    await ws_trajectory(_hass_with(only), connection, {"id": 5})

    connection.send_error.assert_not_called()
    msg_id, payload = connection.send_result.call_args.args
    assert msg_id == 5
    assert payload["entry_id"] == "e1"
    assert payload["zoom"] == 1.25
    assert payload["map"]["id"] == 2


async def test_ws_trajectory_needs_a_target_with_several_entries() -> None:
    """Without a target, two robots are an error, not "the first one"."""
    first = _coordinator("e1", NarwalState())
    second = _coordinator("e2", NarwalState())
    connection = MagicMock()
    hass = _hass_with(first, second)

    await ws_trajectory(hass, connection, {"id": 6})

    connection.send_result.assert_not_called()
    assert connection.send_error.call_args.args[:2] == (6, ERR_TARGET_REQUIRED)
    hass.config_entries.async_entries.assert_called_once_with(
        DOMAIN, include_ignore=False, include_disabled=False
    )


async def test_ws_trajectory_a_robot_still_setting_up_counts_as_configured() -> None:
    """Robot e2 retries its setup after a restart: no silent answer with e1's trail."""
    hass = _hass_with(_coordinator("e1", _state_with_trail()), configured=["e1", "e2"])
    connection = MagicMock()

    await ws_trajectory(hass, connection, {"id": 6})

    connection.send_result.assert_not_called()
    assert connection.send_error.call_args.args[:2] == (6, ERR_TARGET_REQUIRED)


async def test_ws_trajectory_the_only_robot_not_loaded_yet() -> None:
    connection = MagicMock()

    await ws_trajectory(_hass_with(configured=["e1"]), connection, {"id": 6})

    assert connection.send_error.call_args.args[:2] == (6, "not_found")


async def test_ws_trajectory_resolves_an_entity_id() -> None:
    """A card's camera entity picks its robot through the entity registry."""
    first = _coordinator("e1", NarwalState())
    second = _coordinator("e2", NarwalState())
    connection = MagicMock()
    registry = _registry(**{"camera.upstairs_map": "e2"})

    with patch("custom_components.narwal.websocket_api.er.async_get", return_value=registry):
        await ws_trajectory(
            _hass_with(first, second),
            connection,
            {"id": 7, "entity_id": "camera.upstairs_map"},
        )

    assert connection.send_result.call_args.args[1]["entry_id"] == "e2"


async def test_ws_trajectory_unknown_entity_id() -> None:
    """An entity of another integration, or none at all, is not found."""
    connection = MagicMock()
    registry = _registry(**{"camera.other": "foreign"})
    hass = _hass_with(_coordinator("e1", NarwalState()))

    with patch("custom_components.narwal.websocket_api.er.async_get", return_value=registry):
        await ws_trajectory(hass, connection, {"id": 8, "entity_id": "camera.other"})
        await ws_trajectory(hass, connection, {"id": 9, "entity_id": "camera.gone"})

    connection.send_result.assert_not_called()
    assert [c.args[:2] for c in connection.send_error.call_args_list] == [
        (8, "not_found"),
        (9, "not_found"),
    ]


async def test_ws_trajectory_named_entry() -> None:
    first = _coordinator("e1", NarwalState())
    second = _coordinator("e2", NarwalState())
    connection = MagicMock()

    await ws_trajectory(_hass_with(first, second), connection, {"id": 10, "entry_id": "e2"})

    assert connection.send_result.call_args.args[1]["entry_id"] == "e2"


async def test_ws_trajectory_unknown_entry() -> None:
    connection = MagicMock()

    await ws_trajectory(
        _hass_with(_coordinator("e1", NarwalState())),
        connection,
        {"id": 11, "entry_id": "nope"},
    )

    connection.send_result.assert_not_called()
    assert connection.send_error.call_args.args[:2] == (11, "not_found")


async def test_ws_trajectory_without_entries() -> None:
    connection = MagicMock()

    await ws_trajectory(_hass_with(), connection, {"id": 12})

    assert connection.send_error.call_args.args[:2] == (12, "not_found")


async def test_ws_trajectory_matching_signature_skips_the_trail() -> None:
    """A client that holds the current trail gets map and view, no points."""
    hass = _hass_with(_coordinator("e1", _state_with_trail(), {CONF_MAP_ROTATION: 90}))
    connection = MagicMock()

    await ws_trajectory(hass, connection, {"id": 13, "signature": [2, 7, 9, *MAP_KEY]})

    hass.async_add_executor_job.assert_not_called()
    payload = connection.send_result.call_args.args[1]
    assert payload == {
        "version": ANSWER_VERSION,
        "entry_id": "e1",
        "map": {
            "id": 2,
            "created_at": 1_700_000_000,
            "width": 100,
            "height": 80,
            "resolution_mm": 60,
        },
        "rotation": 90,
        "zoom": 1.0,
        "signature": [2, 7, 9, *MAP_KEY],
        "unchanged": True,
    }


async def test_ws_trajectory_stale_signature_gets_the_trail() -> None:
    """Another signature, or none, answers the full trail."""
    hass = _hass_with(_coordinator("e1", _state_with_trail()))
    connection = MagicMock()

    await ws_trajectory(hass, connection, {"id": 14, "signature": [1, 7, 9, *MAP_KEY]})
    await ws_trajectory(hass, connection, {"id": 15})

    for call in connection.send_result.call_args_list:
        payload = call.args[1]
        assert payload["unchanged"] is False
        assert payload["signature"] == [2, 7, 9, *MAP_KEY]
        assert payload["count"] == 2


async def test_ws_trajectory_same_trail_on_another_map_is_not_unchanged() -> None:
    """The points depend on the map: a new map before the next display_map sends them anew."""
    state = _state_with_trail()
    state.map_data = _map(3)
    hass = _hass_with(_coordinator("e1", state))
    connection = MagicMock()

    await ws_trajectory(hass, connection, {"id": 18, "signature": [2, 7, 9, *MAP_KEY]})

    payload = connection.send_result.call_args.args[1]
    assert payload["unchanged"] is False
    assert payload["signature"] == [2, 7, 9, 3, *MAP_KEY[1:]]
    assert payload["count"] == 2


@pytest.mark.parametrize(
    "change",
    [
        {"created_at": 0, "origin_x": 3},
        {"created_at": 0, "origin_y": 5},
        {"created_at": 0, "width": 120},
        {"created_at": 0, "height": 90},
    ],
)
async def test_ws_trajectory_same_map_id_with_other_geometry_is_not_unchanged(
    change: dict,
) -> None:
    """A map that grew keeps its id; without a creation time only its geometry tells."""
    state = _state_with_trail()
    state.map_data = replace(_map(), created_at=0)
    hass = _hass_with(_coordinator("e1", state))
    connection = MagicMock()
    await ws_trajectory(hass, connection, {"id": 1})
    held = connection.send_result.call_args.args[1]["signature"]

    state.map_data = replace(_map(), **change)
    await ws_trajectory(hass, connection, {"id": 2, "signature": held})

    payload = connection.send_result.call_args.args[1]
    assert payload["unchanged"] is False
    assert payload["count"] == 2


async def test_ws_trajectory_signature_without_a_trail() -> None:
    """A trail that ended (docked, new map) is never reported as unchanged."""
    state = NarwalState()
    state.map_data = _map()
    hass = _hass_with(_coordinator("e1", state))
    connection = MagicMock()

    await ws_trajectory(hass, connection, {"id": 16, "signature": [2, 7, 9, *MAP_KEY]})

    payload = connection.send_result.call_args.args[1]
    assert payload["unchanged"] is False
    assert payload["signature"] is None
    assert payload["count"] == 0


async def test_ws_trajectory_null_signature_sent_back_without_a_trail() -> None:
    """The null a robot without a trail answers, sent back, gets a full answer."""
    state = NarwalState()
    state.map_data = _map()
    hass = _hass_with(_coordinator("e1", state))
    connection = MagicMock()

    await ws_trajectory(hass, connection, {"id": 19, "signature": None})

    payload = connection.send_result.call_args.args[1]
    assert (payload["unchanged"], payload["signature"], payload["count"]) == (False, None, 0)


async def test_ws_trajectory_signature_on_a_map_that_decoded_empty() -> None:
    """A trail on a zero-size map has no points to keep, so it is not unchanged."""
    state = _state_with_trail()
    state.map_data = MapData(map_id=2)
    hass = _hass_with(_coordinator("e1", state))
    connection = MagicMock()

    await ws_trajectory(hass, connection, {"id": 17, "signature": [2, 7, 9, *MAP_KEY]})

    payload = connection.send_result.call_args.args[1]
    assert payload["unchanged"] is False
    assert payload["signature"] is None
    assert payload["count"] == 0


def _user(*, admin: bool = False, readable: tuple[str, ...] = ()) -> MagicMock:
    """A user holding read permission on the ``readable`` entities only."""
    user = MagicMock(is_admin=admin)
    user.permissions.check_entity = lambda entity_id, policy: (
        policy == "read" and entity_id in readable
    )
    return user


async def test_ws_trajectory_needs_read_permission_on_the_robot() -> None:
    """Where the robot went is the robot's data: read on its vacuum entity is required."""
    hass = _hass_with(_coordinator("e1", _state_with_trail()))
    entries = [
        MagicMock(entity_id="vacuum.upstairs", domain="vacuum"),
        MagicMock(entity_id="camera.upstairs_map", domain="camera"),
    ]

    with patch(
        "custom_components.narwal.websocket_api.er.async_entries_for_config_entry",
        return_value=entries,
    ):
        for user in (_user(), _user(readable=("camera.upstairs_map",))):
            with pytest.raises(Unauthorized):
                await ws_trajectory(hass, MagicMock(user=user), {"id": 1})
        hass.async_add_executor_job.assert_not_called()

        connection = MagicMock(user=_user(readable=("vacuum.upstairs",)))
        await ws_trajectory(hass, connection, {"id": 2})

    assert connection.send_result.call_args.args[1]["count"] == 2


async def test_ws_trajectory_checks_permission_before_resolving_the_target() -> None:
    """A restricted user cannot tell an unknown id from a robot it may not read."""
    camera_only = _user(readable=("camera.upstairs_map",))
    hass = _hass_with(_coordinator("e1", _state_with_trail()))
    registry = _registry(**{"camera.upstairs_map": "e1", "camera.other": "foreign"})

    with patch("custom_components.narwal.websocket_api.er.async_get", return_value=registry):
        for msg in (
            {"id": 1, "entity_id": "camera.gone"},
            {"id": 2, "entity_id": "camera.other"},
            {"id": 3, "entry_id": "nope"},
        ):
            with pytest.raises(Unauthorized):
                await ws_trajectory(hass, MagicMock(user=camera_only), msg)
            connection = MagicMock(user=_user(admin=True))
            await ws_trajectory(hass, connection, msg)
            assert connection.send_error.call_args.args[1] == "not_found"

        # An entity the user may read but that is no Narwal robot is not found, as in the services.
        connection = MagicMock(user=_user(readable=("camera.other",)))
        await ws_trajectory(hass, connection, {"id": 4, "entity_id": "camera.other"})
    assert connection.send_error.call_args.args[:2] == (4, "not_found")


async def test_ws_trajectory_restricted_user_without_a_target() -> None:
    """Without a target a restricted user still learns to name one, but not a missing entry."""
    connection = MagicMock(user=_user())
    await ws_trajectory(
        _hass_with(_coordinator("e1", NarwalState()), _coordinator("e2", NarwalState())),
        connection,
        {"id": 1},
    )
    assert connection.send_error.call_args.args[:2] == (1, ERR_TARGET_REQUIRED)

    with pytest.raises(Unauthorized):
        await ws_trajectory(_hass_with(), MagicMock(user=_user()), {"id": 2})


async def test_ws_trajectory_robot_without_a_vacuum_entity_is_admin_only() -> None:
    hass = _hass_with(_coordinator("e1", _state_with_trail()))

    with patch(
        "custom_components.narwal.websocket_api.er.async_entries_for_config_entry",
        return_value=[],
    ):
        with pytest.raises(Unauthorized):
            await ws_trajectory(hass, MagicMock(user=_user()), {"id": 1})
        connection = MagicMock(user=_user(admin=True))
        await ws_trajectory(hass, connection, {"id": 2})

    assert connection.send_result.call_args.args[1]["count"] == 2
