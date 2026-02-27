"""DataUpdateCoordinator for Narwal vacuum."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .narwal_client import NarwalClient, NarwalConnectionError, NarwalState
from .narwal_client.const import WorkingStatus

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL = timedelta(seconds=60)


class NarwalCoordinator(DataUpdateCoordinator[NarwalState]):
    """Push-mode coordinator for Narwal vacuum.

    Primary data source is WebSocket broadcasts (every ~1.5s when awake).
    Fallback polling every 60s via get_status() in case broadcasts stop.
    """

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=POLL_INTERVAL,
        )
        self.client = NarwalClient(
            host=entry.data["host"],
            port=entry.data["port"],
            device_id=entry.data.get("device_id", ""),
        )
        self._listen_task: asyncio.Task[None] | None = None

    async def async_setup(self) -> None:
        """Connect to the vacuum and start the WebSocket listener."""
        await self.client.connect()

        # Set up push callback before starting listener
        self.client.on_state_update = self._on_state_update

        # Start persistent WebSocket listener as a background task.
        # This also starts the keepalive loop which sends wake commands.
        self._listen_task = self.config_entry.async_create_background_task(
            self.hass,
            self.client.start_listening(),
            f"{DOMAIN}_ws_listener",
        )

        # Attempt to wake the robot (sends burst of wake commands)
        await self.client.wake(timeout=20.0)

        # Fetch initial state (best-effort — may fail if robot didn't wake)
        try:
            await self.client.get_device_info()
        except Exception:
            _LOGGER.debug("Could not fetch device info (robot may be asleep)")

        try:
            await self.client.get_status()
        except Exception:
            _LOGGER.debug("Could not fetch status (robot may be asleep)")

        # Fetch initial map (best-effort)
        try:
            await self.client.get_map()
        except Exception:
            _LOGGER.debug("Could not fetch initial map")

        _LOGGER.debug(
            "After get_status: working_status=%s, battery=%d, is_docked=%s",
            self.client.state.working_status,
            self.client.state.battery_level,
            self.client.state.is_docked,
        )

        # If working_status is still unknown, wait for broadcasts to arrive.
        # The listener is running and will update state via push callbacks.
        # Broadcasts arrive every ~1.5s when awake, so 5s is plenty.
        if self.client.state.working_status == WorkingStatus.UNKNOWN:
            _LOGGER.debug("Waiting for first broadcast to determine robot state")
            for _ in range(10):
                await asyncio.sleep(0.5)
                if self.client.state.working_status != WorkingStatus.UNKNOWN:
                    break

        _LOGGER.info(
            "Startup state: working_status=%s, battery=%d, is_docked=%s, is_returning=%s",
            self.client.state.working_status,
            self.client.state.battery_level,
            self.client.state.is_docked,
            self.client.state.is_returning,
        )
        self.async_set_updated_data(self.client.state)

    def _on_state_update(self, state: NarwalState) -> None:
        """Handle a push state update from the WebSocket listener."""
        self.async_set_updated_data(state)

    async def _async_update_data(self) -> NarwalState:
        """Polling fallback — fetch status if no push updates arrived.

        Also attempts to wake the robot if it appears to be sleeping.
        """
        if not self.client.connected:
            try:
                await self.client.connect()
            except NarwalConnectionError as err:
                raise UpdateFailed(f"Cannot connect to vacuum: {err}") from err

        # If robot isn't broadcasting, try to wake it
        if not self.client.robot_awake:
            await self.client.wake(timeout=10.0)

        try:
            await self.client.get_status()
        except Exception as err:
            raise UpdateFailed(f"Failed to get status: {err}") from err

        return self.client.state

    async def async_shutdown(self) -> None:
        """Disconnect from the vacuum."""
        await self.client.disconnect()
        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
        await super().async_shutdown()
