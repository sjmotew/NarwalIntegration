"""Tests for the map geometry shared by the camera and the websocket API."""

from __future__ import annotations

import logging
import math

import pytest

import tests.ha_stubs

tests.ha_stubs.install()

from custom_components.narwal.const import (  # noqa: E402
    CONF_MAP_ROTATION,
    CONF_MAP_ZOOM,
    NATIVE_TRAJECTORY_MAX_POINTS,
)
from custom_components.narwal.map_geometry import (  # noqa: E402
    map_is_drawable,
    map_rotation,
    map_zoom,
    native_trajectory_grid_points,
)
from narwal_client.models import MapData  # noqa: E402


def test_native_trajectory_grid_points_convert_and_dedupe() -> None:
    """Narwal-native trajectory points are converted using the map origin."""
    static_map = MapData(width=100, height=100, origin_x=2, origin_y=4)

    points = native_trajectory_grid_points(
        [(3.0, 5.0), (3.1, 5.1), (4.0, 6.0), (200.0, 200.0)],
        static_map,
    )

    assert points == [(1.0, 1.0), (2.0, 2.0)]


def test_native_trajectory_grid_points_preserve_segment_breaks() -> None:
    """Discontinuous native windows remain separated for the renderer."""
    static_map = MapData(width=100, height=100, origin_x=2, origin_y=4)

    points = native_trajectory_grid_points(
        [(3.0, 5.0), (4.0, 6.0), (float("nan"), float("nan")), (5.0, 7.0)],
        static_map,
    )

    assert points[:2] == [(1.0, 1.0), (2.0, 2.0)]
    assert math.isnan(points[2][0])
    assert math.isnan(points[2][1])
    assert points[3] == (3.0, 3.0)


def test_native_trajectory_grid_points_break_at_out_of_bounds_sample() -> None:
    """Missing off-map samples cannot connect otherwise nearby valid points."""
    static_map = MapData(width=100, height=100)

    points = native_trajectory_grid_points(
        [(10.0, 10.0), (101.0, 10.0), (11.0, 10.0)],
        static_map,
    )

    assert points[0] == (10.0, 10.0)
    assert math.isnan(points[1][0])
    assert math.isnan(points[1][1])
    assert points[2] == (11.0, 10.0)


def test_native_trajectory_grid_points_log_out_of_bounds_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A trail off the map logs one line with a count, not one per point."""
    static_map = MapData(width=100, height=100, origin_x=2, origin_y=4)
    trail = [(500.0 + i, 500.0) for i in range(1000)] + [(3.0, 5.0)]

    with caplog.at_level(logging.DEBUG, logger="custom_components.narwal.map_geometry"):
        points = native_trajectory_grid_points(trail, static_map, max_points=len(trail))

    assert points == [(1.0, 1.0)]
    records = [r for r in caplog.records if "outside map bounds" in r.getMessage()]
    assert len(records) == 1
    assert "Skipped 1000 " in records[0].getMessage()
    assert "first at 498.0, 496.0" in records[0].getMessage()


def test_native_trajectory_grid_points_in_bounds_logs_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A trail on the map logs no out-of-bounds line."""
    static_map = MapData(width=100, height=100, origin_x=2, origin_y=4)

    with caplog.at_level(logging.DEBUG, logger="custom_components.narwal.map_geometry"):
        native_trajectory_grid_points([(3.0, 5.0), (4.0, 6.0)], static_map)

    assert not [r for r in caplog.records if "outside map bounds" in r.getMessage()]


def test_native_trajectory_grid_points_decimates_long_route_and_preserves_tail() -> None:
    """Long native trajectories retain the latest Narwal point when capped."""
    static_map = MapData(width=NATIVE_TRAJECTORY_MAX_POINTS + 20, height=10)
    points = [(float(index), 1.0) for index in range(NATIVE_TRAJECTORY_MAX_POINTS + 10)]

    converted = native_trajectory_grid_points(points, static_map)

    assert len(converted) == NATIVE_TRAJECTORY_MAX_POINTS
    assert converted[0] == (0.0, 1.0)
    assert converted[-1] == (float(NATIVE_TRAJECTORY_MAX_POINTS + 9), 1.0)
    assert converted[-200:] == [
        (float(index), 1.0)
        for index in range(NATIVE_TRAJECTORY_MAX_POINTS - 190, NATIVE_TRAJECTORY_MAX_POINTS + 10)
    ]


def test_map_is_drawable_needs_size_and_image() -> None:
    """The camera draws no frame for a map without size or image; no trail goes there."""
    assert map_is_drawable(MapData(map_id=2, width=10, height=10, compressed_map=b"\x01"))
    assert not map_is_drawable(None)
    assert not map_is_drawable(MapData(map_id=2, width=0, height=10, compressed_map=b"\x01"))
    assert not map_is_drawable(MapData(map_id=2, width=10, height=0, compressed_map=b"\x01"))
    assert not map_is_drawable(MapData(map_id=2, width=10, height=10))


def test_map_view_options_fall_back_and_clamp() -> None:
    assert (map_rotation({}), map_zoom({})) == (0, 1.0)
    assert map_rotation({CONF_MAP_ROTATION: 450}) == 90
    assert map_rotation({CONF_MAP_ROTATION: 45}) == 0
    assert map_rotation({CONF_MAP_ROTATION: "bogus"}) == 0
    assert (map_zoom({CONF_MAP_ZOOM: 5}), map_zoom({CONF_MAP_ZOOM: 0.5})) == (2.0, 1.0)
