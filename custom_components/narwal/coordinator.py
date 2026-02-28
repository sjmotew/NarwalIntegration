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

# After verifying stale state, ignore CLEANING broadcasts for this long.
# Prevents stale broadcasts (from brief wake-up) from overriding verified state.
STALE_GUARD_SECONDS = 120.0


class NarwalCoordinator(DataUpdateCoordinator[NarwalState]):
    """Push-mode coordinator for Narwal vacuum.

    Primary data source is WebSocket broadcasts (every ~1.5s when awake).
    Fallback polling every 60s via get_status() in case broadcasts stop.

    Stale state handling:
      The robot's firmware caches working_status from its last active session.
      When asleep on the dock, both command responses AND broadcasts may
      contain stale CLEANING data. We verify with task/force_end (the robot
      confirms whether a task is actually running) and guard against stale
      broadcasts overwriting the verified state.
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
        # Monotonic time until which CLEANING broadcasts are rejected
        # (set after force_end verification confirms stale state)
        self._stale_guard_until: float = 0.0

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
        ):
            await self._verify_and_fix_stale_cleaning(state, "startup")

        _LOGGER.info(
            "Narwal startup: status=%s, battery=%d, docked=%s, awake=%s",
            state.working_status.name, state.battery_level, state.is_docked,
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
        # Guard: after force_end verified stale CLEANING, reject CLEANING
        # broadcasts for a window. A genuine clean (user-initiated) will
        # produce sustained broadcasts that outlast the guard.
        if (
            self._stale_guard_until > 0
            and time.monotonic() < self._stale_guard_until
            and state.working_status
            in (WorkingStatus.CLEANING, WorkingStatus.CLEANING_ALT)
        ):
            _LOGGER.debug(
                "Dropping stale CLEANING broadcast (guard active for %.0fs)",
                self._stale_guard_until - time.monotonic(),
            )
            return

        # Guard expired or state is not CLEANING — clear it
        if self._stale_guard_until > 0 and time.monotonic() >= self._stale_guard_until:
            self._stale_guard_until = 0.0

        _LOGGER.debug(
            "Broadcast update: status=%s, docked=%s, f11=%d, f47=%d",
            state.working_status.name, state.is_docked,
            state.dock_field11, state.dock_field47,
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

    async def _verify_and_fix_stale_cleaning(
        self, state: NarwalState, context: str
    ) -> None:
        """Verify whether CLEANING state is real or stale firmware cache.

        Sends task/force_end and checks the response:
          - NOT_APPLICABLE → robot confirms it's NOT cleaning → stale
          - SUCCESS → robot WAS in a lingering active state → now stopped

        If stale, tries common/yell to force a full firmware wake (the robot
        must fully boot its CPU to play audio), then re-queries status.
        If still stale after all attempts, forces DOCKED.

        Sets a time-based guard to prevent stale broadcasts from overriding
        the verified state.
        """
        _LOGGER.info(
            "%s: state says CLEANING — verifying with force_end", context
        )
        try:
            resp = await self.client.stop()
        except Exception:
            _LOGGER.debug("%s: force_end failed (timeout?)", context)
            return

        if resp.not_applicable:
            _LOGGER.info(
                "%s: force_end=NOT_APPLICABLE — robot confirms NOT cleaning",
                context,
            )
        elif resp.success:
            _LOGGER.info(
                "%s: force_end=SUCCESS — ended lingering task", context
            )
        else:
            _LOGGER.info(
                "%s: force_end returned code %d", context, resp.result_code
            )

        # Try to force a full wake with yell (locate sound).
        # The robot must fully boot its CPU to play audio through the
        # speaker, which should refresh the firmware's state cache.
        _LOGGER.info("%s: sending yell to force full firmware wake", context)
        try:
            await self.client.locate()
        except Exception:
            _LOGGER.debug("%s: yell failed", context)

        # Give robot 3s to process and update state
        await asyncio.sleep(3.0)

        # Re-query status — may be fresh now after full wake
        try:
            await self.client.get_status(full_update=True)
        except Exception:
            _LOGGER.debug("%s: re-query after yell failed", context)

        # Check if state is now correct
        if state.working_status not in (
            WorkingStatus.CLEANING, WorkingStatus.CLEANING_ALT,
        ):
            _LOGGER.info(
                "%s: state refreshed to %s after yell",
                context, state.working_status.name,
            )
            return

        # Still stale after yell + re-query. We verified with force_end
        # that the robot is NOT cleaning. Override to DOCKED.
        _LOGGER.info(
            "%s: still %s after verification — setting DOCKED (verified idle)",
            context, state.working_status.name,
        )
        state.working_status = WorkingStatus.DOCKED
        state.is_paused = False
        state.is_returning_to_dock = False

        # Activate guard to prevent stale broadcasts from overriding
        self._stale_guard_until = time.monotonic() + STALE_GUARD_SECONDS
        _LOGGER.info(
            "%s: stale broadcast guard active for %ds",
            context, int(STALE_GUARD_SECONDS),
        )

    async def _async_update_data(self) -> NarwalState:
        """Polling fallback — fetch status if no push updates arrived."""
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
        # A truly cleaning robot broadcasts every ~1.5s.
        if (
            state.working_status
            in (WorkingStatus.CLEANING, WorkingStatus.CLEANING_ALT)
            and not self.client.robot_awake
        ):
            await self._verify_and_fix_stale_cleaning(state, "poll")

        _LOGGER.debug(
            "Poll update: status=%s, docked=%s, battery=%d, awake=%s",
            state.working_status.name, state.is_docked,
            state.battery_level, self.client.robot_awake,
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
