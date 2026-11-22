"""Tests for Narwal map camera trail handling."""

from __future__ import annotations

import asyncio
import math
import struct
import zlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import tests.ha_stubs

tests.ha_stubs.install()

from custom_components.narwal.camera import (  # noqa: E402
    _MAP_CARD_ATTRIBUTES,
    _PATH_ATTRIBUTE_MAX_POINTS,
    NarwalMapCamera,
)
from custom_components.narwal.const import CONF_SHOW_ROOM_LABELS  # noqa: E402
from narwal_client.const import WorkingStatus  # noqa: E402
from narwal_client.models import MapData, MapDisplayData, NarwalState, RoomInfo  # noqa: E402


def _float_stream(*values: float) -> bytes:
    """Encode a packed float32 stream as display_map field 2 uses it."""
    return b"".join(struct.pack("<f", value) for value in values)


def _encode_varint(value: int) -> bytes:
    """Encode a non-negative integer as a protobuf varint."""
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value & 0x7F)
    return bytes(out)


def _compress_room_grid(pixels: list[int]) -> bytes:
    """Encode a list of pixel values into the zlib-compressed protobuf grid format."""
    packed = b"".join(_encode_varint(pixel) for pixel in pixels)
    payload = b"\x0a" + _encode_varint(len(packed)) + packed
    return zlib.compress(payload)


def _camera(state: NarwalState) -> NarwalMapCamera:
    """Create a NarwalMapCamera with mocked coordinator and render hook."""
    camera = object.__new__(NarwalMapCamera)
    camera.coordinator = MagicMock()
    camera.coordinator.client.state = state
    camera.coordinator.config_entry = MagicMock()
    camera.coordinator.config_entry.options = {}
    camera.hass = MagicMock()
    camera.hass.async_create_task = MagicMock()
    camera.async_write_ha_state = MagicMock()
    camera.async_update_token = MagicMock()
    camera._cached_image = None
    camera._cache_key = ()
    camera._last_render_time = 0.0
    camera._render_count = 0
    camera._pending_render = None
    camera._render_task = None
    camera._async_render = MagicMock(return_value="render-task")
    camera._base_map_image = None
    camera._base_map_ts = 0
    camera._base_map_options_key = (True, True, True)
    camera._room_label_points = []
    camera._cached_rooms = {}
    camera._map_attributes = {}
    return camera


def test_trajectory_cache_key_changes_for_same_length_native_update() -> None:
    """Same-length native trajectory updates must repaint the map overlay."""
    first = MapDisplayData.from_broadcast(
        {"2": {"1": _float_stream(1.0, 2.0), "2": _float_stream(1.0, 2.0)}}
    )
    corrected = MapDisplayData.from_broadcast(
        {"2": {"1": _float_stream(1.0, 3.0), "2": _float_stream(1.0, 2.0)}}
    )

    assert NarwalMapCamera._trajectory_cache_key(first) != (
        NarwalMapCamera._trajectory_cache_key(corrected)
    )


def test_handle_update_passes_native_trail_to_renderer_outside_cleaning() -> None:
    """The HA-retained native trajectory is rendered whenever present."""
    state = NarwalState(working_status=WorkingStatus.CHARGED)
    state.map_data = MapData(
        width=100,
        height=100,
        resolution=50,
        origin_x=2,
        origin_y=4,
        compressed_map=b"\x01",
    )
    state.map_display_data = MapDisplayData.from_broadcast(
        {
            "1": {"1": {"1": 4.0, "2": 6.0}},
            "2": {
                "1": _float_stream(3.0, 4.0),
                "2": _float_stream(5.0, 6.0),
            },
        }
    )
    camera = _camera(state)
    camera._schedule_render = MagicMock()

    with patch(
        "custom_components.narwal.camera.native_trajectory_grid_points",
        side_effect=AssertionError("callback must not process full native route"),
    ):
        camera._handle_coordinator_update()

    camera._schedule_render.assert_called_once()
    render_display, render_key = camera._schedule_render.call_args.args
    assert render_display is state.map_display_data
    assert render_key[-1] == state.map_display_data.trajectory_signature
    camera.hass.async_create_task.assert_not_called()


def test_handle_update_without_native_trajectory_renders_no_trail() -> None:
    """A live robot position alone must not draw a trail."""
    state = NarwalState(working_status=WorkingStatus.CLEANING)
    state.map_data = MapData(
        width=100,
        height=100,
        resolution=50,
        origin_x=2,
        origin_y=4,
        compressed_map=b"\x01",
    )
    state.map_display_data = MapDisplayData(robot_x=4.0, robot_y=6.0)
    camera = _camera(state)
    camera._schedule_render = MagicMock()

    camera._handle_coordinator_update()

    camera._schedule_render.assert_called_once()
    render_display, render_key = camera._schedule_render.call_args.args
    assert render_display is state.map_display_data
    assert render_key[-1] == ()


def test_schedule_render_coalesces_while_render_is_running() -> None:
    """Only the latest native trajectory render is kept while a render is active."""
    camera = _camera(NarwalState())
    running_task = MagicMock()
    running_task.done.return_value = False
    camera._render_task = running_task
    first = MapDisplayData(robot_x=1.0)
    latest = MapDisplayData(robot_x=2.0)

    camera._schedule_render(first, ("first",))
    camera._schedule_render(latest, ("latest",))

    assert camera._pending_render == (latest, ("latest",))
    camera.hass.async_create_task.assert_not_called()


def test_handle_update_replaces_pending_render_when_state_returns_to_cache() -> None:
    """A rollback to the cached key cancels any queued intermediate render."""
    state = NarwalState()
    state.map_data = MapData(
        width=100,
        height=100,
        resolution=50,
        created_at=123,
        compressed_map=b"\x01",
    )
    state.map_display_data = MapDisplayData(robot_x=4.0, robot_y=6.0)
    camera = _camera(state)
    cached_key = (
        123,
        (True, False, False),
        (0, 1.0),
        4.0,
        6.0,
        0.0,
        (),
    )
    camera._cached_image = b"old"
    camera._cache_key = cached_key
    running_task = MagicMock()
    running_task.done.return_value = False
    camera._render_task = running_task
    camera._pending_render = (MapDisplayData(robot_x=9.0), ("intermediate",))

    camera._handle_coordinator_update()

    assert camera._pending_render == (state.map_display_data, cached_key)
    camera.hass.async_create_task.assert_not_called()


async def test_render_pending_delays_throttled_native_update() -> None:
    """Throttled rendering coalesces again after waiting."""
    camera = _camera(NarwalState())
    camera._cached_image = b"old"
    camera._cache_key = (1, (True, True, True), (0, 1.0), 1.0)
    camera._last_render_time = 100.0
    first = MapDisplayData(robot_x=2.0)
    first_key = (1, (True, True, True), (0, 1.0), 2.0)
    latest = MapDisplayData(robot_x=3.0)
    latest_key = (1, (True, True, True), (0, 1.0), 3.0)
    camera._pending_render = (
        first,
        first_key,
    )
    camera._async_render = AsyncMock()

    async def queue_latest(_delay: float) -> None:
        camera._pending_render = (latest, latest_key)

    with (
        patch("custom_components.narwal.camera.time.monotonic", return_value=101.0),
        patch(
            "custom_components.narwal.camera.asyncio.sleep",
            new_callable=AsyncMock,
            side_effect=queue_latest,
        ) as sleep,
    ):
        await camera._async_render_pending()

    sleep.assert_awaited_once_with(1.0)
    camera._async_render.assert_awaited_once_with(latest, latest_key)
    assert camera._render_task is None


async def test_remove_camera_cancels_pending_render_task() -> None:
    """Entity removal cannot leave a render that later writes stale state."""
    camera = _camera(NarwalState())
    render_started = asyncio.Event()
    keep_rendering = asyncio.Event()

    async def blocked_render(_display, _key) -> None:
        render_started.set()
        await keep_rendering.wait()

    camera._async_render = blocked_render
    camera._pending_render = (MapDisplayData(robot_x=1.0), ("pending",))
    render_task = asyncio.create_task(camera._async_render_pending())
    camera._render_task = render_task
    await render_started.wait()

    await camera.async_will_remove_from_hass()

    assert render_task.cancelled()
    assert camera._render_task is None
    assert camera._pending_render is None


async def test_render_keeps_camera_access_token_stable() -> None:
    """Rendered frames rely on state updates without invalidating image URLs."""
    state = NarwalState()
    state.map_data = MapData(
        width=100,
        height=100,
        resolution=50,
        origin_x=2,
        origin_y=4,
        created_at=123,
        compressed_map=b"\x01",
    )
    camera = _camera(state)
    camera._async_render = NarwalMapCamera._async_render.__get__(
        camera, NarwalMapCamera
    )

    async def executor_job(fn, *args):
        if getattr(fn, "__name__", "") == "render_base_map":
            return _BaseImage(300, 300)
        if getattr(fn, "__name__", "") == "room_label_points":
            return []
        if getattr(fn, "__name__", "") == "_build_rooms_attribute":
            return {}
        if getattr(fn, "__name__", "") == "_render_frame":
            return fn(*args)
        return b"png"

    camera.hass.async_add_executor_job = AsyncMock(side_effect=executor_job)

    with patch.object(NarwalMapCamera, "_render_overlay_image", return_value=b"png"):
        await camera._async_render(MapDisplayData(robot_x=4.0, robot_y=6.0), ("key",))

    assert camera._cached_image == b"png"
    attributes = camera.extra_state_attributes
    assert attributes["render_count"] == 1
    assert attributes["vacuum_position"] == {"x": 2.0, "y": 2.0, "a": 0.0}
    assert len(attributes["calibration_points"]) == 3
    camera.async_update_token.assert_not_called()
    camera.async_write_ha_state.assert_called_once()


class _BaseImage:
    """Size-only stand-in for the cached PIL base map."""

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height


def _map_with_dock() -> MapData:
    return MapData(
        width=100,
        height=80,
        resolution=50,
        origin_x=2,
        origin_y=4,
        created_at=123,
        compressed_map=b"\x01",
        dock_x=10.0,
        dock_y=20.0,
    )


def test_map_card_attributes_describe_the_rendered_frame() -> None:
    """Robot, heading, dock and trail come out in grid coordinates."""
    display = MapDisplayData.from_broadcast(
        {
            "1": {"1": {"1": 4.0, "2": 6.0}, "2": math.pi / 2},
            "2": {
                "1": _float_stream(3.0, 4.0),
                "2": _float_stream(5.0, 6.0),
            },
        }
    )

    attributes = NarwalMapCamera._map_card_attributes(
        _map_with_dock(), display, 0, 1.0, 300, 240,
    )

    assert attributes["vacuum_position"] == {"x": 2.0, "y": 2.0, "a": 90.0}
    assert attributes["charger"] == {"x": 10.0, "y": 20.0}
    assert attributes["path"] == [[[1.0, 1.0], [2.0, 2.0]]]
    assert [point["vacuum"] for point in attributes["calibration_points"]] == [
        {"x": 0.0, "y": 0.0},
        {"x": 100.0, "y": 0.0},
        {"x": 0.0, "y": 80.0},
    ]


def test_map_card_attributes_without_live_data() -> None:
    """Before the first display_map only the static map is described."""
    attributes = NarwalMapCamera._map_card_attributes(
        _map_with_dock(), None, 0, 1.0, 300, 240,
    )

    assert set(attributes) == {"calibration_points", "charger"}


def test_map_card_attributes_skip_lost_position() -> None:
    """A (0, 0) display_map means lost map context, not a robot at the origin."""
    attributes = NarwalMapCamera._map_card_attributes(
        _map_with_dock(), MapDisplayData(robot_x=0.0, robot_y=0.0), 0, 1.0, 300, 240,
    )

    assert "vacuum_position" not in attributes


def test_path_attribute_splits_at_breaks_and_drops_single_points() -> None:
    """Segment breaks start a new segment; a lone point is not a segment."""
    display = MagicMock()
    display.to_grid_coords.return_value = None
    display.has_trajectory = True
    nan = float("nan")
    display.trajectory_render_points.return_value = [
        (3.0, 5.0), (4.0, 6.0), (nan, nan), (12.0, 14.0), (nan, nan), (20.0, 20.0), (21.0, 21.0),
    ]

    attributes = NarwalMapCamera._map_card_attributes(
        _map_with_dock(), display, 0, 1.0, 300, 240,
    )

    assert attributes["path"] == [
        [[1.0, 1.0], [2.0, 2.0]],
        [[18.0, 16.0], [19.0, 17.0]],
    ]


def test_path_attribute_is_capped_and_keeps_the_newest_points() -> None:
    """A long route is decimated for the attribute; the latest point survives."""
    display = MagicMock()
    display.to_grid_coords.return_value = None
    display.has_trajectory = True
    display.trajectory_render_points.return_value = [
        (2.0 + index * 0.05, 5.0 + (index % 2)) for index in range(1500)
    ]

    attributes = NarwalMapCamera._map_card_attributes(
        _map_with_dock(), display, 0, 1.0, 300, 240,
    )

    points = [point for segment in attributes["path"] for point in segment]
    assert len(points) <= _PATH_ATTRIBUTE_MAX_POINTS
    assert points[-1] == [round(1499 * 0.05, 1), 2.0]


def test_map_card_attributes_are_not_recorded() -> None:
    """Live-only map-card data stays out of the recorder."""
    assert "rooms" in _MAP_CARD_ATTRIBUTES
    assert NarwalMapCamera._unrecorded_attributes >= _MAP_CARD_ATTRIBUTES
    assert "render_count" not in NarwalMapCamera._unrecorded_attributes


async def test_attribute_failure_keeps_the_rendered_frame() -> None:
    """A broken attribute build logs and leaves the image and counter intact."""
    state = NarwalState()
    state.map_data = _map_with_dock()
    camera = _camera(state)
    camera._async_render = NarwalMapCamera._async_render.__get__(
        camera, NarwalMapCamera
    )
    camera._map_attributes = {"vacuum_position": {"x": 1.0, "y": 1.0, "a": 0.0}}

    async def executor_job(fn, *args):
        if getattr(fn, "__name__", "") == "render_base_map":
            return _BaseImage(300, 240)
        if getattr(fn, "__name__", "") == "room_label_points":
            return []
        if getattr(fn, "__name__", "") == "_build_rooms_attribute":
            return {}
        if getattr(fn, "__name__", "") == "_render_frame":
            return fn(*args)
        return b"png"

    camera.hass.async_add_executor_job = AsyncMock(side_effect=executor_job)

    with (
        patch.object(NarwalMapCamera, "_render_overlay_image", return_value=b"png"),
        patch.object(
            NarwalMapCamera, "_map_card_attributes", side_effect=ValueError("boom"),
        ),
    ):
        await camera._async_render(MapDisplayData(robot_x=4.0, robot_y=6.0), ("key",))

    assert camera._cached_image == b"png"
    assert camera.extra_state_attributes == {"render_count": 1}


def test_path_attribute_cap_holds_with_many_breaks() -> None:
    """Kept segment breaks must not push the path past its cap; newest wins."""
    display = MagicMock()
    display.to_grid_coords.return_value = None
    display.has_trajectory = True
    nan = float("nan")
    points: list[tuple[float, float]] = []
    for index in range(600):
        points += [(3.0 + index * 0.1, 5.0), (3.0 + index * 0.1, 6.0), (nan, nan)]
    display.trajectory_render_points.return_value = points

    attributes = NarwalMapCamera._map_card_attributes(
        _map_with_dock(), display, 0, 1.0, 300, 240,
    )

    kept = [point for segment in attributes["path"] for point in segment]
    assert len(kept) <= _PATH_ATTRIBUTE_MAX_POINTS
    last_x = round(1.0 + 599 * 0.1, 1)
    assert attributes["path"][-1] == [[last_x, 1.0], [last_x, 2.0]]


def test_robot_outside_the_map_has_no_position() -> None:
    """Where render_overlay draws no robot, no position is published."""
    attributes = NarwalMapCamera._map_card_attributes(
        _map_with_dock(), MapDisplayData(robot_x=500.0, robot_y=6.0), 0, 1.0, 300, 240,
    )

    assert "vacuum_position" not in attributes


def test_build_rooms_attribute_computes_true_extents_and_handles_duplicate_names() -> None:
    """Room outlines and centroids match the mask by room_id even with duplicate names."""
    width, height = 6, 5
    grid = [0] * (width * height)
    # Room 1 ("Bedroom"): floor at (1, 1) and (2, 1), wall at (0, 1) and (3, 2)
    grid[1 * width + 0] = (1 << 8) | 0x10
    grid[1 * width + 1] = (1 << 8) | 0x00
    grid[1 * width + 2] = (1 << 8) | 0x00
    grid[2 * width + 3] = (1 << 8) | 0x10
    # Room 2 ("Bedroom" duplicate name): floor at (4, 3) and (4, 4)
    grid[3 * width + 4] = (2 << 8) | 0x00
    grid[4 * width + 4] = (2 << 8) | 0x00

    static_map = MapData(
        width=width,
        height=height,
        compressed_map=_compress_room_grid(grid),
        rooms=[
            RoomInfo(room_id=1, name="Bedroom"),
            RoomInfo(room_id=2, name="Bedroom"),
            RoomInfo(room_id=99, name="Unpainted Room"),
        ],
    )

    rooms = NarwalMapCamera._build_rooms_attribute(static_map)

    assert set(rooms) == {1, 2}
    assert rooms[1] == {
        "x0": 0.0,
        "y0": 1.0,
        "x1": 3.0,
        "y1": 2.0,
        "pos_x": 1.5,
        "pos_y": 1.0,
        "name": "Bedroom",
    }
    assert rooms[2] == {
        "x0": 4.0,
        "y0": 3.0,
        "x1": 4.0,
        "y1": 4.0,
        "pos_x": 4.0,
        "pos_y": 3.5,
        "name": "Bedroom",
    }


def test_map_card_attributes_include_cached_rooms_when_present() -> None:
    """Cached rooms are included in map-card attributes; empty dicts are omitted."""
    rooms_payload = {
        1: {
            "x0": 10.0,
            "y0": 20.0,
            "x1": 50.0,
            "y1": 60.0,
            "pos_x": 30.0,
            "pos_y": 40.0,
            "name": "Kitchen",
        }
    }

    with_rooms = NarwalMapCamera._map_card_attributes(
        _map_with_dock(), None, 0, 1.0, 300, 240, rooms=rooms_payload,
    )
    without_rooms = NarwalMapCamera._map_card_attributes(
        _map_with_dock(), None, 0, 1.0, 300, 240, rooms={},
    )

    assert with_rooms["rooms"] == rooms_payload
    assert "rooms" not in without_rooms


async def test_render_populates_rooms_even_when_room_labels_hidden() -> None:
    """Room outlines are exposed in attributes regardless of show_room_labels option."""
    width, height = 4, 4
    grid = [0] * (width * height)
    grid[1 * width + 1] = (3 << 8) | 0x00
    grid[2 * width + 2] = (3 << 8) | 0x00

    state = NarwalState()
    state.map_data = MapData(
        width=width,
        height=height,
        resolution=50,
        created_at=123,
        compressed_map=_compress_room_grid(grid),
        rooms=[RoomInfo(room_id=3, name="Living Room")],
    )
    camera = _camera(state)
    camera.coordinator.config_entry.options = {CONF_SHOW_ROOM_LABELS: False}
    camera._async_render = NarwalMapCamera._async_render.__get__(
        camera, NarwalMapCamera
    )

    async def executor_job(fn, *args):
        if getattr(fn, "__name__", "") == "render_base_map":
            return _BaseImage(12, 12)
        return fn(*args)

    camera.hass.async_add_executor_job = AsyncMock(side_effect=executor_job)

    with patch.object(NarwalMapCamera, "_render_overlay_image", return_value=b"png"):
        await camera._async_render(MapDisplayData(robot_x=1.0, robot_y=1.0), ("key",))

    assert camera._room_label_points == []
    assert camera.extra_state_attributes["rooms"] == {
        3: {
            "x0": 1.0,
            "y0": 1.0,
            "x1": 2.0,
            "y1": 2.0,
            "pos_x": 1.5,
            "pos_y": 1.5,
            "name": "Living Room",
        }
    }


@pytest.mark.parametrize(
    "static_map",
    [
        None,
        MapData(map_id=2, width=0, height=10, compressed_map=b"\x01"),
        MapData(map_id=2, width=10, height=10),
    ],
    ids=["no map", "zero width", "no image"],
)
def test_lost_map_clears_the_map_card_attributes(static_map: MapData | None) -> None:
    """Without a drawable map there is no frame to describe, and nothing is rendered."""
    state = NarwalState()
    state.map_data = static_map
    camera = _camera(state)
    camera._map_attributes = {"vacuum_position": {"x": 1.0, "y": 1.0, "a": 0.0}}

    camera._handle_coordinator_update()

    assert camera.extra_state_attributes == {"render_count": 0}
    camera._async_render.assert_not_called()


@pytest.mark.parametrize(
    "static_map",
    [
        None,
        MapData(map_id=2, width=0, height=10, compressed_map=b"\x01"),
        MapData(map_id=2, width=10, height=10),
    ],
    ids=["no map", "zero width", "no image"],
)
async def test_render_skips_a_map_that_became_undrawable(static_map: MapData | None) -> None:
    """A map replaced between scheduling and rendering is checked again, the same way."""
    state = NarwalState()
    state.map_data = static_map
    camera = _camera(state)
    camera._async_render = NarwalMapCamera._async_render.__get__(camera, NarwalMapCamera)
    camera.hass.async_add_executor_job = AsyncMock()

    await camera._async_render(MapDisplayData(robot_x=4.0, robot_y=6.0), ("key",))

    camera.hass.async_add_executor_job.assert_not_called()
    camera.async_write_ha_state.assert_called_once()
    assert camera._cached_image is None
