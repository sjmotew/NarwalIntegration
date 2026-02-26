"""Map image entity for Narwal vacuum."""

from __future__ import annotations

from datetime import datetime, timezone
import logging

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity

_LOGGER = logging.getLogger(__name__)


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
        self._last_map_timestamp: int = 0

    @property
    def image_last_updated(self) -> datetime | None:
        """Return when the image was last updated."""
        state = self.coordinator.client.state

        # Prefer real-time display_map timestamp
        if state.map_display_data and state.map_display_data.timestamp:
            return datetime.fromtimestamp(
                state.map_display_data.timestamp, tz=timezone.utc
            )

        # Fall back to static map created_at
        if state.map_data and state.map_data.created_at:
            return datetime.fromtimestamp(
                state.map_data.created_at, tz=timezone.utc
            )

        return None

    async def async_image(self) -> bytes | None:
        """Return the map as a PNG image."""
        state = self.coordinator.client.state

        # Determine which map source to use
        # Prefer real-time display_map during cleaning
        display = state.map_display_data
        static_map = state.map_data

        compressed = None
        width = 0
        height = 0
        robot_x = None
        robot_y = None
        robot_heading = None
        dock_x = None
        dock_y = None
        current_ts = 0

        if display and display.compressed_grid:
            compressed = display.compressed_grid
            width = display.width
            height = display.height
            robot_x = display.robot_x if display.robot_x else None
            robot_y = display.robot_y if display.robot_y else None
            robot_heading = display.robot_heading if display.robot_heading else None
            current_ts = display.timestamp or 0
        elif static_map and static_map.compressed_map:
            compressed = static_map.compressed_map
            width = static_map.width
            height = static_map.height
            current_ts = static_map.created_at or 0

        # Dock position and room names come from static map (always available)
        room_names: dict[int, str] | None = None
        if static_map:
            dock_x = static_map.dock_x
            dock_y = static_map.dock_y
            if static_map.rooms:
                room_names = {
                    r.room_id: r.name for r in static_map.rooms if r.name
                }

        if not compressed or width <= 0 or height <= 0:
            return self._cached_image

        # Only re-render if data has changed
        if current_ts == self._last_map_timestamp and self._cached_image:
            return self._cached_image

        # Render in executor (Pillow is CPU-bound)
        try:
            from .narwal_client.map_renderer import render_map_from_compressed

            png_bytes = await self.hass.async_add_executor_job(
                render_map_from_compressed,
                compressed,
                width,
                height,
                robot_x,
                robot_y,
                robot_heading,
                dock_x,
                dock_y,
                room_names,
            )

            if png_bytes:
                self._cached_image = png_bytes
                self._last_map_timestamp = current_ts

        except Exception:
            _LOGGER.exception("Failed to render map image")

        return self._cached_image
