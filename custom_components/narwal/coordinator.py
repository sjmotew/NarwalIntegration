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

# Fast re-poll when state is incomplete (robot asleep at startup)
FAST_POLL_INTERVAL = timedelta(seconds=10)
FAST_POLL_MAX = 6  # up to 60s of fast polling before falling back to normal


class NarwalCoordinator(DataUpdateCoordinator[NarwalState]):
    """Push-mode coordinator for Narwal vacuum.

    Primary data source is WebSocket broadcasts (every ~1.5s when awake).
    Fallback polling every 60s via get_status() in case broadcasts stop.

    State trust model:
      - Broadcasts are AUTHORITATIVE — working_status, dock flags, etc.
      - get_status() while robot is broadcasting: AUTHORITATIVE (full update)
      - get_status() while robot is NOT broadcasting: only battery/health
        are trustworthy (hardware-sampled). Working_status may be stale
        firmware cache from a previous session.
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
            await self.client.get_status(full_update=True)
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

        # Verify CLEANING at startup — robot on dock often reports stale
        # CLEANING from its last session. Ask the robot directly.
        if state.working_status in (
            WorkingStatus.CLEANING, WorkingStatus.CLEANING_ALT,
        ) and not self.client.robot_awake:
            try:
                resp = await self.client.stop()
                if resp.not_applicable:
                    _LOGGER.info(
                        "Startup: force_end=NOT_APPLICABLE — "
                        "CLEANING is stale, setting DOCKED"
                    )
                    state.working_status = WorkingStatus.DOCKED
                    state.is_paused = False
                    state.is_returning_to_dock = False
            except Exception:
                _LOGGER.debug("Startup verification failed")
        _LOGGER.debug(
            "Narwal startup: status=%s, battery=%d, docked=%s, "
            "f11=%d, f47=%d, dock_sub=%d, dock_act=%d, field3=%r, awake=%s",
            state.working_status.name, state.battery_level, state.is_docked,
            state.dock_field11, state.dock_field47,
            state.dock_sub_state, state.dock_activity,
            state.raw_base_status.get("3"),
            self.client.robot_awake,
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
        """Polling fallback — fetch status if no push updates arrived.

        State verification:
          When get_status() reports CLEANING but the robot is not actively
          broadcasting, the state may be a stale firmware cache from a
          previous session. We verify by sending task/force_end:
            - NOT_APPLICABLE (2) → robot confirms it's NOT cleaning → stale
            - SUCCESS (1) → robot WAS cleaning and we ended it → real
          This is a deterministic check, not inference.
        """
        if not self.client.connected:
            try:
                await self.client.connect()
            except NarwalConnectionError as err:
                raise UpdateFailed(f"Cannot connect to vacuum: {err}") from err

        # Try to wake the robot if not broadcasting
        if not self.client.robot_awake:
            await self.client.wake(timeout=20.0)

        # Query full status
        try:
            await self.client.get_status(full_update=True)
        except Exception as err:
            raise UpdateFailed(f"Failed to get status: {err}") from err

        state = self.client.state

        # Verify suspicious CLEANING state when robot is not broadcasting.
        # A truly cleaning robot broadcasts every ~1.5s. If it's not
        # broadcasting but reports CLEANING, ask the robot directly.
        if (
            state.working_status
            in (WorkingStatus.CLEANING, WorkingStatus.CLEANING_ALT)
            and not self.client.robot_awake
        ):
            _LOGGER.info(
                "State says CLEANING but no broadcasts — verifying with force_end"
            )
            try:
                resp = await self.client.stop()
                if resp.not_applicable:
                    # Robot confirms: "nothing to stop" → CLEANING is stale
                    _LOGGER.info(
                        "force_end returned NOT_APPLICABLE — "
                        "CLEANING is stale, re-querying status"
                    )
                    # Re-query: robot just processed a command, state may
                    # have refreshed
                    try:
                        await self.client.get_status(full_update=True)
                    except Exception:
                        pass
                    state = self.client.state
                    # If STILL stale after re-query, force to DOCKED —
                    # we verified the robot is not cleaning
                    if state.working_status in (
                        WorkingStatus.CLEANING,
                        WorkingStatus.CLEANING_ALT,
                    ):
                        _LOGGER.info(
                            "Still CLEANING after re-query — "
                            "robot confirmed idle, setting DOCKED"
                        )
                        state.working_status = WorkingStatus.DOCKED
                        state.is_paused = False
                        state.is_returning_to_dock = False
                elif resp.success:
                    _LOGGER.info(
                        "force_end returned SUCCESS — "
                        "robot was in stale active state, now stopped"
                    )
                    # Re-query for fresh state after stop
                    try:
                        await self.client.get_status(full_update=True)
                        state = self.client.state
                    except Exception:
                        pass
            except Exception:
                _LOGGER.debug("force_end verification failed, keeping state as-is")

        _LOGGER.debug(
            "Poll update: status=%s, docked=%s, battery=%d, awake=%s, "
            "f11=%d, f47=%d, field3=%r",
            state.working_status.name, state.is_docked,
            state.battery_level, self.client.robot_awake,
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
