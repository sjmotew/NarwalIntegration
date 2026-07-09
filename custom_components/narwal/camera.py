"""Map camera entity for Narwal vacuum — MJPEG streaming for live updates."""

from __future__ import annotations

import io
import logging
import math
import time

from homeassistant.components.camera import Camera
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity
from .narwal_client.const import WorkingStatus

_LOGGER = logging.getLogger(__name__)

# Minimum seconds between re-renders (display_map arrives every ~1.5s
# but PIL rendering is CPU-bound — no need to render every broadcast).
_MIN_RENDER_INTERVAL = 2

# Trail recording (used in both debug and normal modes)
_TRAIL_MAX_POINTS = 50000  # full cleaning session worth
_TRAIL_RECORD_INTERVAL = 3  # seconds between trail point recordings

# Debug view: blank canvas with robot dot + trail.
# Set to False to use the real map renderer instead.
_DEBUG_VIEW = False
_DEBUG_CANVAS_SIZE = 600  # pixels
_DEBUG_TRAIL_MAX = _TRAIL_MAX_POINTS
_DEBUG_RECORD_INTERVAL = _TRAIL_RECORD_INTERVAL


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

    _attr_name = "Carpet map"

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

    _attr_name = "Map"
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
        # Cached base map (PIL Image) — only re-rendered when static map changes
        self._base_map_image = None  # PIL Image or None
        self._base_map_ts: int = 0  # created_at of the static map used for base
        # Trail state — accumulated grid-coordinate positions during cleaning
        self._trail: list[tuple[float, float]] = []
        self._last_trail_record: float = 0.0
        self._last_cleaning_status: WorkingStatus = WorkingStatus.UNKNOWN
        # Debug view state — full session trail with growing viewport
        self._dock_pos: tuple[float, float] | None = None
        self._vp_min_x: float = 0.0
        self._vp_max_x: float = 0.0
        self._vp_min_y: float = 0.0
        self._vp_max_y: float = 0.0
        self._vp_initialized: bool = False

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

    @property
    def extra_state_attributes(self) -> dict[str, str | int]:
        """Expose render count so HA detects state changes for MJPEG refresh."""
        return {"render_count": self._render_count}

    def _reset_debug_trail(self) -> None:
        """Clear trail and viewport for a new cleaning session."""
        self._trail.clear()
        self._dock_pos = None
        self._vp_initialized = False
        self._last_trail_record = 0.0

    def _record_debug_position(self, x: float, y: float) -> None:
        """Record a position and expand viewport bounds."""
        now = time.monotonic()

        if self._dock_pos is None:
            self._dock_pos = (x, y)
            self._trail.append((x, y))
            self._last_trail_record = now
        elif (
            now - self._last_trail_record >= _DEBUG_RECORD_INTERVAL
            and len(self._trail) < _DEBUG_TRAIL_MAX
        ):
            self._trail.append((x, y))
            self._last_trail_record = now

        if not self._vp_initialized:
            self._vp_min_x = x
            self._vp_max_x = x
            self._vp_min_y = y
            self._vp_max_y = y
            self._vp_initialized = True
        else:
            if x < self._vp_min_x:
                self._vp_min_x = x
            if x > self._vp_max_x:
                self._vp_max_x = x
            if y < self._vp_min_y:
                self._vp_min_y = y
            if y > self._vp_max_y:
                self._vp_max_y = y

    def _reset_trail(self) -> None:
        """Clear trail for a new cleaning session."""
        self._trail.clear()
        self._last_trail_record = 0.0

    def _record_trail_position(self, grid_x: float, grid_y: float) -> None:
        """Record a grid-coordinate position to the cleaning trail."""
        now = time.monotonic()
        if now - self._last_trail_record >= _TRAIL_RECORD_INTERVAL:
            if len(self._trail) < _TRAIL_MAX_POINTS:
                self._trail.append((grid_x, grid_y))
            self._last_trail_record = now

    @callback
    def _handle_coordinator_update(self) -> None:
        """Re-render the map when new data arrives from the coordinator."""
        state = self.coordinator.client.state
        display = state.map_display_data

        # Detect cleaning session transitions — clear trail on new session
        current_status = state.working_status
        was_cleaning = self._last_cleaning_status in (
            WorkingStatus.CLEANING, WorkingStatus.CLEANING_ALT,
        )
        is_cleaning = current_status in (
            WorkingStatus.CLEANING, WorkingStatus.CLEANING_ALT,
        )
        if is_cleaning and not was_cleaning:
            _LOGGER.info("New cleaning session — clearing trail and vision obstacles")
            self._reset_trail()
        if current_status != WorkingStatus.UNKNOWN:
            self._last_cleaning_status = current_status

        if _DEBUG_VIEW:
            if not display or (display.robot_x == 0.0 and display.robot_y == 0.0):
                self.async_write_ha_state()
                return
            self._record_debug_position(display.robot_x, display.robot_y)
            new_key = (display.robot_x, display.robot_y, display.robot_heading)
        else:
            static_map = state.map_data
            if not static_map or not static_map.compressed_map:
                self.async_write_ha_state()
                return
            if static_map.width <= 0 or static_map.height <= 0:
                self.async_write_ha_state()
                return

            # Record trail in grid coordinates
            if display and not (display.robot_x == 0.0 and display.robot_y == 0.0):
                grid_pos = display.to_grid_coords(
                    static_map.resolution, static_map.origin_x, static_map.origin_y,
                )
                if grid_pos is not None:
                    self._record_trail_position(grid_pos[0], grid_pos[1])

            static_ts = static_map.created_at or 0
            trail_len = len(self._trail)
            if display:
                new_key = (static_ts, display.robot_x, display.robot_y, display.robot_heading, trail_len)
            else:
                new_key = (static_ts,)

        now = time.monotonic()
        since_render = now - self._last_render_time if self._last_render_time else 999

        if new_key == self._cache_key and self._cached_image:
            self.async_write_ha_state()
            return

        if self._cached_image and since_render < _MIN_RENDER_INTERVAL:
            self.async_write_ha_state()
            return

        self.hass.async_create_task(self._async_render(display, new_key))

    async def _async_render(self, display, new_key) -> None:
        """Render the map image in an executor thread."""
        if _DEBUG_VIEW and display:
            trail = list(self._trail)
            dock = self._dock_pos
            viewport = None
            if self._vp_initialized:
                viewport = (
                    self._vp_min_x, self._vp_min_y,
                    self._vp_max_x, self._vp_max_y,
                )
            try:
                png_bytes = await self.hass.async_add_executor_job(
                    _render_debug_view,
                    display.robot_x,
                    display.robot_y,
                    display.robot_heading,
                    trail,
                    dock,
                    viewport,
                )
                if png_bytes:
                    self._cached_image = png_bytes
                    self._cache_key = new_key
                    self._last_render_time = time.monotonic()
                    self._render_count += 1
            except Exception:
                _LOGGER.exception("Failed to render debug view")
            self.async_write_ha_state()
            return

        # --- Normal map render path (with cached base + overlay) ---
        state = self.coordinator.client.state
        static_map = state.map_data
        if not static_map:
            self.async_write_ha_state()
            return

        from .narwal_client.map_renderer import render_base_map, render_overlay

        # Rebuild base map only when static map data changes
        static_ts = static_map.created_at or 0
        if self._base_map_image is None or static_ts != self._base_map_ts:
            room_names: dict[int, str] | None = None
            if static_map.rooms:
                room_names = {
                    r.room_id: r.display_name for r in static_map.rooms
                }
            base_img = await self.hass.async_add_executor_job(
                render_base_map,
                static_map.compressed_map,
                static_map.width,
                static_map.height,
                static_map.dock_x,
                static_map.dock_y,
                room_names,
                static_map.obstacles,
                static_map.origin_x,
                static_map.origin_y,
            )
            if base_img:
                self._base_map_image = base_img
                self._base_map_ts = static_ts
                _LOGGER.info("Base map rendered (ts=%d, %dx%d)", static_ts, static_map.width, static_map.height)
            else:
                self.async_write_ha_state()
                return

        # Compute robot grid position
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
                # Log transform details periodically for debugging position offset
                if self._render_count % 30 == 0:
                    try:
                        # Compare display_map dock ref (field 5) with static map dock
                        dock_ref_grid_x = dock_ref_grid_y = None
                        if display.dock_ref_x != 0.0 or display.dock_ref_y != 0.0:
                            dock_ref_grid_x = display.dock_ref_x - static_map.origin_x
                            dock_ref_grid_y = display.dock_ref_y - static_map.origin_y
                        # Room lookup at robot grid position
                        from .narwal_client.map_renderer import lookup_room_at_grid
                        robot_rid, robot_room = lookup_room_at_grid(
                            static_map.compressed_map, static_map.width, static_map.height,
                            int(robot_x), int(robot_y),
                        )
                        dock_rid, dock_room = (-1, "n/a")
                        if static_map.dock_x is not None and static_map.dock_y is not None:
                            dock_rid, dock_room = lookup_room_at_grid(
                                static_map.compressed_map, static_map.width, static_map.height,
                                int(static_map.dock_x), int(static_map.dock_y),
                            )
                        _LOGGER.debug(
                            "POSITION DIAG: robot_raw=(%.2f, %.2f) robot_grid=(%.1f, %.1f) robot_room=%s(id=%d) "
                            "| dock_ref_raw=(%.2f, %.2f) dock_ref_grid=(%.1f, %.1f) "
                            "| static_dock_grid=(%.1f, %.1f) dock_room=%s(id=%d) "
                            "| res=%d origin=(%d, %d) map=%dx%d",
                            display.robot_x, display.robot_y,
                            robot_x, robot_y, robot_room, robot_rid,
                            display.dock_ref_x, display.dock_ref_y,
                            dock_ref_grid_x or 0, dock_ref_grid_y or 0,
                            static_map.dock_x or 0, static_map.dock_y or 0,
                            dock_room, dock_rid,
                            static_map.resolution,
                            static_map.origin_x, static_map.origin_y,
                            static_map.width, static_map.height,
                        )
                    except Exception:
                        _LOGGER.debug("POSITION DIAG failed", exc_info=True)

        trail = list(self._trail) if self._trail else None

        try:
            png_bytes = await self.hass.async_add_executor_job(
                render_overlay,
                self._base_map_image,
                static_map.height,
                robot_x,
                robot_y,
                robot_heading,
                trail,
            )

            if png_bytes:
                self._cached_image = png_bytes
                self._cache_key = new_key
                self._last_render_time = time.monotonic()
                self._render_count += 1

        except Exception:
            _LOGGER.exception("Failed to render map overlay")

        self.async_write_ha_state()


def _render_debug_view(
    robot_x: float,
    robot_y: float,
    robot_heading: float,
    trail: list[tuple[float, float]],
    dock_pos: tuple[float, float] | None = None,
    viewport: tuple[float, float, float, float] | None = None,
) -> bytes:
    """Render a blank canvas with full cleaning trail, dock marker, and robot dot."""
    from PIL import Image, ImageDraw, ImageFont

    size = _DEBUG_CANVAS_SIZE
    img = Image.new("RGB", (size, size), (20, 20, 30))
    draw = ImageDraw.Draw(img)

    if viewport:
        min_x, min_y, max_x, max_y = viewport
    elif trail:
        all_x = [p[0] for p in trail]
        all_y = [p[1] for p in trail]
        min_x, max_x = min(all_x), max(all_x)
        min_y, max_y = min(all_y), max(all_y)
    else:
        min_x = robot_x - 250
        max_x = robot_x + 250
        min_y = robot_y - 250
        max_y = robot_y + 250

    padding = 100
    range_x = max(max_x - min_x, 200) + padding * 2
    range_y = max(max_y - min_y, 200) + padding * 2
    center_x = (min_x + max_x) / 2
    center_y = (min_y + max_y) / 2

    margin = 50
    usable = size - margin * 2
    scale = usable / max(range_x, range_y)

    def to_px(cx: float, cy: float) -> tuple[int, int]:
        px = int((cx - center_x) * scale + size / 2)
        py = int(-(cy - center_y) * scale + size / 2)
        return px, py

    grid_interval = 100
    grid_start_x = int(center_x - range_x / 2)
    grid_start_x = grid_start_x - (grid_start_x % grid_interval)
    grid_start_y = int(center_y - range_y / 2)
    grid_start_y = grid_start_y - (grid_start_y % grid_interval)

    grid_color = (35, 35, 45)
    for gx in range(grid_start_x, int(center_x + range_x / 2) + grid_interval, grid_interval):
        px, _ = to_px(gx, 0)
        if 0 <= px < size:
            draw.line([(px, 0), (px, size)], fill=grid_color)
    for gy in range(grid_start_y, int(center_y + range_y / 2) + grid_interval, grid_interval):
        _, py = to_px(0, gy)
        if 0 <= py < size:
            draw.line([(0, py), (size, py)], fill=grid_color)

    if trail:
        n = len(trail)
        if n > 3000:
            step = max(n // 2000, 2)
            bulk = trail[: n - 200 : step]
            recent = trail[n - 200 :]
            render_trail = bulk + recent
        else:
            render_trail = trail

        recent_start = max(len(render_trail) - 200, 0)
        for i in range(len(render_trail) - 1):
            if i >= recent_start:
                color = (30, 120, 255)
            else:
                color = (15, 60, 130)
            x1, y1 = to_px(render_trail[i][0], render_trail[i][1])
            x2, y2 = to_px(render_trail[i + 1][0], render_trail[i + 1][1])
            draw.line([(x1, y1), (x2, y2)], fill=color, width=2)

    try:
        font = ImageFont.load_default()
    except Exception:
        font = None

    if dock_pos:
        dx, dy = to_px(dock_pos[0], dock_pos[1])
        r = 8
        draw.ellipse(
            [dx - r, dy - r, dx + r, dy + r],
            fill=(255, 140, 0),
            outline=(255, 200, 100),
        )
        draw.text((dx + 12, dy - 6), "DOCK", fill=(255, 140, 0), font=font)

    rx, ry = to_px(robot_x, robot_y)
    dot_r = 7
    draw.ellipse(
        [rx - dot_r, ry - dot_r, rx + dot_r, ry + dot_r],
        fill=(0, 255, 80),
        outline=(150, 255, 180),
    )

    heading_rad = math.radians(robot_heading)
    hx = rx + int(18 * math.cos(heading_rad))
    hy = ry - int(18 * math.sin(heading_rad))
    draw.line([(rx, ry), (hx, hy)], fill=(0, 255, 80), width=2)

    text_color = (180, 180, 190)
    dim_color = (100, 100, 120)
    y_text = 5
    draw.text((5, y_text), f"pos: ({robot_x:.1f}, {robot_y:.1f}) cm", fill=text_color, font=font)
    y_text += 15
    draw.text((5, y_text), f"trail: {len(trail)} pts", fill=dim_color, font=font)
    y_text += 15
    draw.text((5, y_text), f"heading: {robot_heading:.0f}", fill=dim_color, font=font)
    y_text += 15
    view_w = range_x / 100
    view_h = range_y / 100
    draw.text((5, y_text), f"view: {view_w:.1f}x{view_h:.1f}m", fill=dim_color, font=font)

    ox, oy = to_px(0, 0)
    if 0 <= ox < size and 0 <= oy < size:
        draw.line([(ox - 8, oy), (ox + 8, oy)], fill=(80, 80, 80))
        draw.line([(ox, oy - 8), (ox, oy + 8)], fill=(80, 80, 80))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
