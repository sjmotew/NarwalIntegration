"""Map image entity for Narwal vacuum."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import time

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity

_LOGGER = logging.getLogger(__name__)

# Minimum seconds between re-renders (display_map arrives every ~1.5s
# but re-rendering every time is wasteful for the frontend).
_MIN_RENDER_INTERVAL = 5


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Narwal map image entity."""
    coordinator = entry.runtime_data
    async_add_entities([NarwalMapImage(hass, coordinator)])


class NarwalMapImage(NarwalEntity, ImageEntity):
    """Image entity that displays the vacuum's map as a PNG."""

    _attr_content_type = "image/png"
    _attr_name = "Map"

    def __init__(self, hass: HomeAssistant, coordinator: NarwalCoordinator) -> None:
        """Initialize the map image entity."""
        super().__init__(coordinator)
        ImageEntity.__init__(self, hass)
        device_id = coordinator.config_entry.data["device_id"]
        self._attr_unique_id = f"{device_id}_map"
        self._cached_image: bytes | None = None
        # Cache key: (static_map_ts, display_map_ts) — re-render when either changes
        self._cache_key: tuple[int, int] = (0, 0)
        self._last_render_time: float = 0.0

    @property
    def image_last_updated(self) -> datetime | None:
        """Return when the image was last updated."""
        state = self.coordinator.client.state

        # Prefer real-time display_map timestamp (ms since epoch)
        if state.map_display_data and state.map_display_data.timestamp:
            return datetime.fromtimestamp(
                state.map_display_data.timestamp / 1000, tz=timezone.utc
            )

        # Fall back to static map created_at
        if state.map_data and state.map_data.created_at:
            return datetime.fromtimestamp(
                state.map_data.created_at, tz=timezone.utc
            )

        return None

    async def async_image(self) -> bytes | None:
        """Return the map as a PNG image.

        Always uses the static map grid as background, with robot position
        overlaid from display_map when the robot is actively cleaning.
        """
        state = self.coordinator.client.state
        static_map = state.map_data
        display = state.map_display_data

        # Must have a static map to render anything
        if not static_map or not static_map.compressed_map:
            return self._cached_image
        if static_map.width <= 0 or static_map.height <= 0:
            return self._cached_image

        # Build cache key from both data sources
        static_ts = static_map.created_at or 0
        display_ts = display.timestamp if display else 0
        new_key = (static_ts, display_ts)

        # Skip re-render if nothing changed
        if new_key == self._cache_key and self._cached_image:
            return self._cached_image

        # Throttle renders during cleaning (display_map arrives every ~1.5s)
        now = time.monotonic()
        if (
            display_ts > 0
            and self._cached_image
            and now - self._last_render_time < _MIN_RENDER_INTERVAL
        ):
            return self._cached_image

        # Robot position from display_map (convert cm → grid pixels)
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

        # Dock position and room names from static map
        dock_x = static_map.dock_x
        dock_y = static_map.dock_y
        room_names: dict[int, str] | None = None
        if static_map.rooms:
            room_names = {
                r.room_id: r.name for r in static_map.rooms if r.name
            }

        # Render in executor (Pillow is CPU-bound)
        try:
            from .narwal_client.map_renderer import render_map_from_compressed

            png_bytes = await self.hass.async_add_executor_job(
                render_map_from_compressed,
                static_map.compressed_map,
                static_map.width,
                static_map.height,
                robot_x,
                robot_y,
                robot_heading,
                dock_x,
                dock_y,
                room_names,
            )

            if png_bytes:
                self._cached_image = png_bytes
                self._cache_key = new_key
                self._last_render_time = now

        except Exception:
            _LOGGER.exception("Failed to render map image")

        return self._cached_image
