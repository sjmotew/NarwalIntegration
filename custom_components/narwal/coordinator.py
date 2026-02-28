"""DataUpdateCoordinator for Narwal vacuum."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .narwal_client import NarwalClient, NarwalConnectionError, NarwalState
from .narwal_client.const import WorkingStatus

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL = timedelta(seconds=60)

# Fast re-poll when state is incomplete (robot asleep at startup)
FAST_POLL_INTERVAL = timedelta(seconds=10)
FAST_POLL_MAX = 6  # up to 60s of fast polling before falling back to normal


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
        self._fast_poll_remaining = 0

    async def async_setup(self) -> None:
        """Connect to the vacuum and start the WebSocket listener.

        Keeps setup fast (<15s) so HA doesn't time out. If the robot is
        asleep, entities are created with defaults and a fast re-poll
        (every 10s) populates them once the robot wakes.
        """
        await self.client.connect()

        # Set up push callback before starting listener
        self.client.on_state_update = self._on_state_update

        # Start persistent WebSocket listener as a background task.
        self._listen_task = self.config_entry.async_create_background_task(
            self.hass,
            self.client.start_listening(),
            f"{DOMAIN}_ws_listener",
        )

        # Quick wake attempt (5s, not 20s — keep setup fast)
        await self.client.wake(timeout=5.0)

        # Single attempt at fetching initial state
        try:
            await self.client.get_device_info()
        except Exception:
            _LOGGER.debug("Could not fetch device info")

        try:
            await self.client.get_status()
        except Exception:
            _LOGGER.debug("Could not fetch initial status")

        try:
            await self.client.get_map()
        except Exception:
            _LOGGER.debug("Could not fetch initial map")

        # Brief wait for broadcasts if status is still unknown
        if self.client.state.working_status == WorkingStatus.UNKNOWN:
            for _ in range(6):  # up to 3 seconds
                await asyncio.sleep(0.5)
                if self.client.state.working_status != WorkingStatus.UNKNOWN:
                    break

        state = self.client.state
        _LOGGER.debug(
            "Narwal startup: status=%s, battery=%d, docked=%s, "
            "f11=%d, f47=%d, dock_sub=%d, dock_act=%d, field3=%r",
            state.working_status.name, state.battery_level, state.is_docked,
            state.dock_field11, state.dock_field47,
            state.dock_sub_state, state.dock_activity,
            state.raw_base_status.get("3"),
        )

        self.async_set_updated_data(state)

        # If robot didn't respond, use fast polling to catch it when it wakes
        if state.working_status == WorkingStatus.UNKNOWN:
            self._fast_poll_remaining = FAST_POLL_MAX
            self.update_interval = FAST_POLL_INTERVAL
            _LOGGER.info(
                "Robot asleep — fast polling every %ds until it responds",
                int(FAST_POLL_INTERVAL.total_seconds()),
            )

    def _on_state_update(self, state: NarwalState) -> None:
        """Handle a push state update from the WebSocket listener."""
        _LOGGER.debug(
            "Broadcast update: status=%s, docked=%s, f11=%d, f47=%d, "
            "dock_sub=%d, dock_act=%d, field3=%r",
            state.working_status.name, state.is_docked,
            state.dock_field11, state.dock_field47,
            state.dock_sub_state, state.dock_activity,
            state.raw_base_status.get("3"),
        )
        self.async_set_updated_data(state)

        # Broadcast arrived — switch back to normal polling if in fast mode
        if self._fast_poll_remaining > 0:
            self._fast_poll_remaining = 0
            self.update_interval = POLL_INTERVAL
            _LOGGER.info(
                "Narwal broadcast received: status=%s — normal polling restored",
                state.working_status.name,
            )

    async def _async_update_data(self) -> NarwalState:
        """Polling fallback — fetch status if no push updates arrived."""
        if not self.client.connected:
            try:
                await self.client.connect()
            except NarwalConnectionError as err:
                raise UpdateFailed(f"Cannot connect to vacuum: {err}") from err

        # Always try to wake the robot — even on the dock in deep sleep,
        # the WebSocket server may still process wake commands.
        if not self.client.robot_awake:
            await self.client.wake(timeout=10.0)

        try:
            await self.client.get_status()
        except Exception as err:
            # If robot is unresponsive and last state was active (cleaning/returning),
            # it has likely completed its task and is now idle on the dock.
            # Clear stale active flags so entities don't stay stuck.
            state = self.client.state
            if state.working_status in (
                WorkingStatus.CLEANING, WorkingStatus(5),  # CLEANING_ALT
            ) and not state.is_paused:
                _LOGGER.info(
                    "Robot unresponsive after cleaning — inferring idle/docked"
                )
                state.working_status = WorkingStatus.DOCKED
                state.is_paused = False
                state.is_returning_to_dock = False
                return state
            raise UpdateFailed(f"Failed to get status: {err}") from err

        state = self.client.state

        # Detect stale CLEANING state: an actively cleaning robot broadcasts
        # every ~1.5s. If state says CLEANING but no broadcasts for 30+ seconds,
        # the robot completed its task and the state data is stale.
        if state.working_status in (
            WorkingStatus.CLEANING, WorkingStatus(5),
        ) and not state.is_paused:
            last_bc = self.client._last_broadcast_time
            if last_bc > 0:
                silence = time.monotonic() - last_bc
                if silence > 30:
                    _LOGGER.info(
                        "Robot reports CLEANING but silent for %.0fs — "
                        "inferring idle/docked",
                        silence,
                    )
                    state.working_status = WorkingStatus.DOCKED
                    state.is_paused = False
                    state.is_returning_to_dock = False

        _LOGGER.debug(
            "Poll update: status=%s, docked=%s, f11=%d, f47=%d, field3=%r",
            state.working_status.name, state.is_docked,
            state.dock_field11, state.dock_field47,
            state.raw_base_status.get("3"),
        )

        # Manage fast poll countdown
        if self._fast_poll_remaining > 0:
            if self.client.state.working_status != WorkingStatus.UNKNOWN:
                self._fast_poll_remaining = 0
                self.update_interval = POLL_INTERVAL
                _LOGGER.info(
                    "Narwal poll got status=%s — normal polling restored",
                    self.client.state.working_status.name,
                )
            else:
                self._fast_poll_remaining -= 1
                if self._fast_poll_remaining <= 0:
                    self.update_interval = POLL_INTERVAL
                    _LOGGER.info("Fast poll exhausted — normal polling restored")

        return self.client.state

    async def async_shutdown(self) -> None:
        """Disconnect from the vacuum."""
        await self.client.disconnect()
        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
        await super().async_shutdown()
