"""Map camera entity for Narwal vacuum — MJPEG streaming for live updates."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time

from homeassistant.components.camera import Camera
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .const import (
    CONF_MAP_ROTATION,
    CONF_MAP_ZOOM,
    CONF_SHOW_FURNITURE,
    CONF_SHOW_FURNITURE_LABELS,
    CONF_SHOW_ROOM_LABELS,
    MAP_OPTION_DEFAULTS,
    MAP_ROTATION_DEFAULT,
    MAP_ZOOM_DEFAULT,
)
from .coordinator import (
    NATIVE_TRAJECTORY_MAX_POINTS,
    NATIVE_TRAJECTORY_RECENT_TAIL_POINTS,
    NarwalCoordinator,
)
from .entity import NarwalEntity
from .narwal_client.const import WorkingStatus
from .narwal_client.models import overlay_to_grid

_LOGGER = logging.getLogger(__name__)

# Minimum seconds between re-renders (display_map arrives every ~1.5s
# but PIL rendering is CPU-bound — no need to render every broadcast).
_MIN_RENDER_INTERVAL = 2

# Native trajectory points can repeat across display_map frames because field 2 is
# the accumulated Narwal trajectory. Deduplicate tiny adjacent moves only.
_NATIVE_TRAIL_MIN_GRID_DELTA = 0.25
_NATIVE_TRAIL_MAX_POINTS = NATIVE_TRAJECTORY_MAX_POINTS
_NATIVE_TRAIL_RECENT_TAIL_POINTS = NATIVE_TRAJECTORY_RECENT_TAIL_POINTS

async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Narwal camera entities."""
    coordinator = entry.runtime_data
    async_add_entities([NarwalMapCamera(coordinator), NarwalCarpetCamera(coordinator)])


class NarwalCarpetCamera(NarwalEntity, Camera):
    """On-demand camera showing the robot's carpet-detection debug image.

    Backed by developer/get_robot_debug_image, which returns cleartext PNGs (no
    crypto). Images are only produced while the robot is mapping/cleaning, so this
    only polls in an active state and otherwise serves the last image.
    """

    _attr_translation_key = "carpet_map"

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        """Initialize the carpet debug camera entity."""
        NarwalEntity.__init__(self, coordinator)
        Camera.__init__(self)
        device_id = coordinator.config_entry.data["device_id"]
        self._attr_unique_id = f"{device_id}_carpet_map"
        self._cached: bytes | None = None
        self._cached_at: float = 0.0

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None,
    ) -> bytes | None:
        """Return the latest carpet-detection PNG; poll only while active."""
        state = self.coordinator.data
        active = state is not None and (
            state.is_cleaning or state.working_status == WorkingStatus.REMAPPING
        )
        if not active:
            return self._cached  # keep last image; don't hit the WS while idle
        now = time.monotonic()
        if self._cached is not None and now - self._cached_at < 8.0:
            return self._cached
        img = await self.coordinator.client.get_robot_debug_image()
        if img is not None:
            self._cached = img
            self._cached_at = now
        return self._cached


class NarwalMapCamera(NarwalEntity, Camera):
    """Camera entity that streams the vacuum's map as MJPEG."""

    _attr_translation_key = "map"
    _attr_frame_interval = _MIN_RENDER_INTERVAL
    _attr_is_streaming = True

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        """Initialize the map camera entity."""
        NarwalEntity.__init__(self, coordinator)
        Camera.__init__(self)
        device_id = coordinator.config_entry.data["device_id"]
        self._attr_unique_id = f"{device_id}_map"
        self._cached_image: bytes | None = None
        self._cache_key: tuple = ()
        self._last_render_time: float = 0.0
        self._render_count: int = 0
        self._pending_render: tuple[object, tuple] | None = None
        self._render_task: asyncio.Task[None] | None = None
        # Cached base map (PIL Image) — only re-rendered when static map changes
        self._base_map_image = None  # PIL Image or None
        self._base_map_ts: int = 0  # created_at of the static map used for base
        self._base_map_options_key: tuple[bool, bool, bool] = (
            MAP_OPTION_DEFAULTS[CONF_SHOW_ROOM_LABELS],
            MAP_OPTION_DEFAULTS[CONF_SHOW_FURNITURE],
            MAP_OPTION_DEFAULTS[CONF_SHOW_FURNITURE_LABELS],
        )
        self._room_label_points: list[tuple[str, float, float]] = []

    @property
    def extra_state_attributes(self) -> dict[str, int]:
        """Expose a changing value so HA publishes each rendered frame."""
        return {"render_count": self._render_count}

    def _map_option(self, key: str) -> bool:
        """Return a persisted map display option."""
        return bool(
            self.coordinator.config_entry.options.get(
                key,
                MAP_OPTION_DEFAULTS[key],
            )
        )

    def _map_rotation(self) -> int:
        """Return persisted clockwise map rotation in degrees."""
        try:
            rotation = int(
                self.coordinator.config_entry.options.get(
                    CONF_MAP_ROTATION,
                    MAP_ROTATION_DEFAULT,
                )
            )
        except (TypeError, ValueError):
            return MAP_ROTATION_DEFAULT
        rotation %= 360
        return rotation if rotation in (0, 90, 180, 270) else MAP_ROTATION_DEFAULT

    def _map_zoom(self) -> float:
        """Return persisted map zoom factor."""
        try:
            zoom = float(
                self.coordinator.config_entry.options.get(
                    CONF_MAP_ZOOM,
                    MAP_ZOOM_DEFAULT,
                )
            )
        except (TypeError, ValueError):
            return MAP_ZOOM_DEFAULT
        return max(1.0, min(2.0, zoom))

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None,
    ) -> bytes | None:
        """Return the latest map image as PNG (for snapshot/polling clients)."""
        return self._cached_image

    async def handle_async_mjpeg_stream(self, request):
        """Stream map as MJPEG using HA's built-in still-image streamer."""
        from homeassistant.components.camera import async_get_still_stream

        return await async_get_still_stream(
            request, self.async_camera_image, "image/png", _MIN_RENDER_INTERVAL,
        )

    async def async_will_remove_from_hass(self) -> None:
        """Cancel queued map rendering before removing the camera entity."""
        self._pending_render = None
        render_task = self._render_task
        if render_task is not None and not render_task.done():
            render_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await render_task
        self._render_task = None
        await super().async_will_remove_from_hass()

    @staticmethod
    def _native_trajectory_grid_points(
        points: list[tuple[float, float]],
        static_map,
    ) -> list[tuple[float, float]]:
        """Return in-bounds grid points from Narwal's native trajectory."""
        converted: list[tuple[float, float]] = []
        pending_break = False

        for raw_x, raw_y in NarwalMapCamera._native_trajectory_render_points(points):
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
            ) < _NATIVE_TRAIL_MIN_GRID_DELTA:
                continue
            converted.append(point)
        return converted

    @staticmethod
    def _native_trajectory_render_points(
        points: list[tuple[float, float]],
    ):
        """Yield render-capped trajectory points while preserving the latest tail."""
        if len(points) <= _NATIVE_TRAIL_MAX_POINTS:
            yield from points
            return

        tail_size = min(_NATIVE_TRAIL_RECENT_TAIL_POINTS, _NATIVE_TRAIL_MAX_POINTS)
        tail_start = len(points) - tail_size
        prefix_slots = _NATIVE_TRAIL_MAX_POINTS - tail_size
        if prefix_slots <= 0:
            yield from points[-_NATIVE_TRAIL_MAX_POINTS:]
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

    @staticmethod
    def _trajectory_cache_key(
        display,
    ) -> tuple[int, int, int] | tuple[()]:
        """Return a compact cache key for native trajectory changes."""
        if display is None:
            return ()
        return display.trajectory_signature

    @classmethod
    def _render_overlay_image(
        cls,
        base_map_image,
        static_map,
        display,
        map_rotation: int,
        map_zoom: float,
        room_label_points: list[tuple[str, float, float]],
    ) -> bytes | None:
        """Render dynamic map overlays in an executor thread."""
        from .narwal_client.map_renderer import render_overlay

        native_trail = None
        if display and display.has_trajectory:
            native_trail = cls._native_trajectory_grid_points(
                display.trajectory_render_points(),
                static_map,
            )

        robot_x = None
        robot_y = None
        robot_heading = None
        if display:
            grid_pos = display.to_grid_coords(
                static_map.resolution, static_map.origin_x, static_map.origin_y,
            )
            if grid_pos is not None:
                robot_x, robot_y = grid_pos
                robot_heading = display.robot_heading

        return render_overlay(
            base_map_image,
            static_map.height,
            robot_x,
            robot_y,
            robot_heading,
            native_trail,
            map_rotation,
            map_zoom,
            room_label_points,
            static_map.dock_x,
            static_map.dock_y,
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Re-render the map when new data arrives from the coordinator."""
        state = self.coordinator.client.state
        display = state.map_display_data

        static_map = state.map_data
        if not static_map or not static_map.compressed_map:
            self.async_write_ha_state()
            return
        if static_map.width <= 0 or static_map.height <= 0:
            self.async_write_ha_state()
            return

        static_ts = static_map.created_at or 0
        map_options_key = (
            self._map_option(CONF_SHOW_ROOM_LABELS),
            self._map_option(CONF_SHOW_FURNITURE),
            self._map_option(CONF_SHOW_FURNITURE_LABELS),
        )
        view_options_key = (self._map_rotation(), self._map_zoom())
        if display:
            new_key = (
                static_ts,
                map_options_key,
                view_options_key,
                display.robot_x,
                display.robot_y,
                display.robot_heading,
                self._trajectory_cache_key(display),
            )
        else:
            new_key = (static_ts, map_options_key, view_options_key)

        if new_key == self._cache_key and self._cached_image:
            if self._render_task is not None and not self._render_task.done():
                self._pending_render = (display, new_key)
            self.async_write_ha_state()
            return

        self._schedule_render(display, new_key)
        self.async_write_ha_state()

    def _render_options_changed(self, new_key: tuple) -> bool:
        """Return True when map display options changed and should bypass throttle."""
        return (
            bool(self._cache_key)
            and len(self._cache_key) > 2
            and len(new_key) > 2
            and (
                self._cache_key[1] != new_key[1]
                or self._cache_key[2] != new_key[2]
            )
        )

    def _schedule_render(self, display, new_key: tuple) -> None:
        """Queue the latest render request, coalescing while a render is active."""
        self._pending_render = (display, new_key)
        if self._render_task is not None and not self._render_task.done():
            return
        self._render_task = self.hass.async_create_task(self._async_render_pending())

    async def _async_render_pending(self) -> None:
        """Render queued map updates sequentially, preserving the newest request."""
        try:
            while self._pending_render is not None:
                display, new_key = self._pending_render
                self._pending_render = None
                since_render = (
                    time.monotonic() - self._last_render_time
                    if self._last_render_time
                    else 999
                )
                if (
                    self._cached_image
                    and since_render < _MIN_RENDER_INTERVAL
                    and not self._render_options_changed(new_key)
                ):
                    await asyncio.sleep(_MIN_RENDER_INTERVAL - since_render)
                    if self._pending_render is not None:
                        display, new_key = self._pending_render
                        self._pending_render = None
                if new_key == self._cache_key and self._cached_image:
                    continue
                await self._async_render(display, new_key)
        finally:
            self._render_task = None

    async def _async_render(self, display, new_key) -> None:
        """Render the map image in an executor thread."""
        state = self.coordinator.client.state
        static_map = state.map_data
        if not static_map:
            self.async_write_ha_state()
            return

        from .narwal_client.map_renderer import (
            render_base_map,
            room_label_points,
        )

        # Rebuild base map only when static map data changes
        static_ts = static_map.created_at or 0
        map_options_key = (
            self._map_option(CONF_SHOW_ROOM_LABELS),
            self._map_option(CONF_SHOW_FURNITURE),
            self._map_option(CONF_SHOW_FURNITURE_LABELS),
        )
        if (
            self._base_map_image is None
            or static_ts != self._base_map_ts
            or map_options_key != self._base_map_options_key
        ):
            room_names: dict[int, str] | None = None
            if map_options_key[0] and static_map.rooms:
                room_names = {
                    r.room_id: r.display_name for r in static_map.rooms
                }
            obstacles = static_map.obstacles if map_options_key[1] else None
            base_img = await self.hass.async_add_executor_job(
                render_base_map,
                static_map.compressed_map,
                static_map.width,
                static_map.height,
                None,
                None,
                room_names,
                obstacles,
                static_map.origin_x,
                static_map.origin_y,
                map_options_key[2],
                False,
                False,
            )
            if base_img:
                self._base_map_image = base_img
                self._base_map_ts = static_ts
                self._base_map_options_key = map_options_key
                self._room_label_points = await self.hass.async_add_executor_job(
                    room_label_points,
                    static_map.compressed_map,
                    static_map.width,
                    static_map.height,
                    room_names,
                )
                _LOGGER.info(
                    "Base map rendered (ts=%d, %dx%d)",
                    static_ts,
                    static_map.width,
                    static_map.height,
                )
            else:
                self.async_write_ha_state()
                return

        try:
            png_bytes = await self.hass.async_add_executor_job(
                self._render_overlay_image,
                self._base_map_image,
                static_map,
                display,
                self._map_rotation(),
                self._map_zoom(),
                self._room_label_points,
            )

            if png_bytes:
                self._cached_image = png_bytes
                self._cache_key = new_key
                self._last_render_time = time.monotonic()
                self._render_count += 1

        except Exception:
            _LOGGER.exception("Failed to render map overlay")

        self.async_write_ha_state()
