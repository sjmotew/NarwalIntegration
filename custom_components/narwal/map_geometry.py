"""Map view options and trail geometry shared by the camera and the websocket API.

Imports only ``const`` and the client models, no entity or coordinator module,
so both ``camera.py`` and ``websocket_api.py`` can import it at module level.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterator, Mapping
from typing import Any, TypeGuard

from .const import (
    CONF_MAP_ROTATION,
    CONF_MAP_ZOOM,
    MAP_ROTATION_DEFAULT,
    MAP_ZOOM_DEFAULT,
    NATIVE_TRAJECTORY_MAX_POINTS,
    NATIVE_TRAJECTORY_RECENT_TAIL_POINTS,
)
from .narwal_client.models import MapData, overlay_to_grid

_LOGGER = logging.getLogger(__name__)

# Native trajectory points can repeat across display_map frames because field 2 is
# the accumulated Narwal trajectory. Deduplicate tiny adjacent moves only.
NATIVE_TRAIL_MIN_GRID_DELTA = 0.25


def map_rotation(options: Mapping[str, Any]) -> int:
    """Return the clockwise map rotation in degrees from an entry's options."""
    try:
        rotation = int(options.get(CONF_MAP_ROTATION, MAP_ROTATION_DEFAULT))
    except (TypeError, ValueError):
        return MAP_ROTATION_DEFAULT
    rotation %= 360
    return rotation if rotation in (0, 90, 180, 270) else MAP_ROTATION_DEFAULT


def map_zoom(options: Mapping[str, Any]) -> float:
    """Return the map zoom factor from an entry's options."""
    try:
        zoom = float(options.get(CONF_MAP_ZOOM, MAP_ZOOM_DEFAULT))
    except (TypeError, ValueError):
        return MAP_ZOOM_DEFAULT
    return max(1.0, min(2.0, zoom))


def map_is_drawable(static_map: MapData | None) -> TypeGuard[MapData]:
    """Return whether the camera draws a frame for this static map.

    A map of zero size is a reply that decoded to no map (#108); a map without
    its image has nothing to draw a trail on either.
    """
    return (
        static_map is not None
        and bool(static_map.compressed_map)
        and static_map.width > 0
        and static_map.height > 0
    )


def native_trajectory_grid_points(
    points: list[tuple[float, float]],
    static_map: MapData,
    max_points: int = NATIVE_TRAJECTORY_MAX_POINTS,
    tail_points: int = NATIVE_TRAJECTORY_RECENT_TAIL_POINTS,
) -> list[tuple[float, float]]:
    """Return in-bounds grid points from Narwal's native trajectory."""
    converted: list[tuple[float, float]] = []
    pending_break = False

    for raw_x, raw_y in native_trajectory_render_points(
        points, max_points, tail_points,
    ):
        if not math.isfinite(raw_x) or not math.isfinite(raw_y):
            pending_break = bool(converted)
            continue
        grid_x = overlay_to_grid(raw_x, static_map.origin_x)
        grid_y = overlay_to_grid(raw_y, static_map.origin_y)
        if grid_x is None or grid_y is None:
            continue
        if (
            grid_x < 0
            or grid_y < 0
            or grid_x >= static_map.width
            or grid_y >= static_map.height
        ):
            _LOGGER.debug(
                "Skipping native Narwal trail point outside map bounds: "
                "%.1f, %.1f (%dx%d)",
                grid_x,
                grid_y,
                static_map.width,
                static_map.height,
            )
            pending_break = bool(converted)
            continue
        point = (grid_x, grid_y)
        if pending_break:
            converted.append((float("nan"), float("nan")))
            pending_break = False
        if converted and math.hypot(
            point[0] - converted[-1][0],
            point[1] - converted[-1][1],
        ) < NATIVE_TRAIL_MIN_GRID_DELTA:
            continue
        converted.append(point)
    return converted


def native_trajectory_render_points(
    points: list[tuple[float, float]],
    max_points: int = NATIVE_TRAJECTORY_MAX_POINTS,
    tail_points: int = NATIVE_TRAJECTORY_RECENT_TAIL_POINTS,
) -> Iterator[tuple[float, float]]:
    """Yield render-capped trajectory points while preserving the latest tail."""
    if len(points) <= max_points:
        yield from points
        return

    tail_size = min(tail_points, max_points)
    tail_start = len(points) - tail_size
    prefix_slots = max_points - tail_size
    if prefix_slots <= 0:
        yield from points[-max_points:]
        return

    span = max(tail_start - 1, 0)
    slots = max(prefix_slots - 1, 1)
    selected_indices: set[int] = set()
    for slot in range(prefix_slots):
        selected_indices.add(round(slot * span / slots))
    selected_indices.update(range(tail_start, len(points)))
    selected_indices.update(
        index
        for index, point in enumerate(points)
        if not math.isfinite(point[0]) or not math.isfinite(point[1])
    )
    for index in sorted(selected_indices):
        yield points[index]
