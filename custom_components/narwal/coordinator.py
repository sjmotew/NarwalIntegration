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

# After deep-sleep correction, ignore all broadcasts for this long.
# Prevents stale broadcasts (from brief wake-ups) from overriding corrected state.
STALE_GUARD_SECONDS = 120.0

# Battery threshold above which a sleeping robot is assumed to be on the dock.
# A robot stuck on the floor drains battery; only a docked robot reaches/stays >95%.
DOCK_BATTERY_THRESHOLD = 95


class NarwalCoordinator(DataUpdateCoordinator[NarwalState]):
    """Push-mode coordinator for Narwal vacuum.

    Primary data source is WebSocket broadcasts (every ~1.5s when awake).
    Fallback polling every 60s via get_status() in case broadcasts stop.

    Deep sleep handling:
      The robot enters deep sleep when idle (on dock or floor). In this mode,
      the WebSocket server still responds to commands but the firmware CPU
      is in low-power mode, so working_status and dock fields are stale
      (cached from the last active session). Battery is always fresh
      (hardware-sampled from the voltage rail).

      Key insight: high battery (>95%) + no broadcasts = on dock (a floor-stuck
      robot drains battery, only a docked robot maintains high charge).
      We detect this and override to DOCKED, with a time-based guard to prevent
      stale broadcasts (from brief wake-ups) from overwriting the corrected state.
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
        # Monotonic time until which ALL broadcasts are rejected
        # (set after deep-sleep detection confirms robot is docked)
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

        # Deep sleep detection: robot responds but isn't broadcasting, and
        # battery is high (>95%). High battery = on dock (a floor-stuck robot
        # drains battery). Low battery deep sleep = possibly stuck on floor,
        # don't assume docked.
        if (
            not self.client.robot_awake
            and state.battery_level >= DOCK_BATTERY_THRESHOLD
            and state.working_status not in (
                WorkingStatus.DOCKED, WorkingStatus.CHARGED, WorkingStatus.UNKNOWN,
            )
        ):
            await self._fix_deep_sleep_state(state, "startup")

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
        # Guard: after force_end verified stale state, reject ALL broadcasts
        # for a window. Brief wake-up broadcasts contain stale data (CLEANING,
        # STANDBY without dock signals, etc). Guard clears when user starts
        # a clean or after timeout. Battery still updates via get_status().
        if self._stale_guard_until > 0:
            if time.monotonic() < self._stale_guard_until:
                _LOGGER.debug(
                    "Dropping broadcast during stale guard (%.0fs left, status=%s)",
                    self._stale_guard_until - time.monotonic(),
                    state.working_status.name,
                )
                return
            # Guard expired
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

    async def _fix_deep_sleep_state(
        self, state: NarwalState, context: str
    ) -> None:
        """Correct stale state when robot is in deep sleep with high battery.

        High battery (>95%) + no broadcasts = on dock. The firmware CPU is in
        low-power mode, so working_status is stale (CLEANING from last session,
        STANDBY without dock signals, etc). Battery is always fresh
        (hardware-sampled from the voltage rail).

        Strategy:
          1. If CLEANING, verify with force_end (confirms no active task)
          2. Try yell to force full CPU wake → re-query
          3. If state refreshed to DOCKED/CHARGED, done
          4. Otherwise, override to DOCKED (deep sleep = on dock)

        Sets a time-based guard to prevent stale broadcasts from overriding.
        """
        _LOGGER.info(
            "%s: deep sleep detected (no broadcasts), stale status=%s — "
            "correcting to DOCKED",
            context, state.working_status.name,
        )

        # If reporting CLEANING, verify no active task with force_end
        if state.working_status in (
            WorkingStatus.CLEANING, WorkingStatus.CLEANING_ALT,
        ):
            try:
                resp = await self.client.stop()
                if resp.not_applicable:
                    _LOGGER.info(
                        "%s: force_end=NOT_APPLICABLE — not actually cleaning",
                        context,
                    )
                elif resp.success:
                    _LOGGER.info(
                        "%s: force_end=SUCCESS — ended lingering task", context
                    )
                else:
                    _LOGGER.info(
                        "%s: force_end code %d", context, resp.result_code
                    )
            except Exception:
                _LOGGER.debug("%s: force_end failed (timeout?)", context)

        # Try to force a full wake with yell (locate sound).
        _LOGGER.debug("%s: sending yell to force full firmware wake", context)
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

        # Check if state refreshed to a dock state
        if state.working_status in (
            WorkingStatus.DOCKED, WorkingStatus.CHARGED,
        ):
            _LOGGER.info(
                "%s: state refreshed to %s after wake attempt",
                context, state.working_status.name,
            )
            return

        # Still stale. Deep sleep = on dock. Override to DOCKED.
        _LOGGER.info(
            "%s: still %s after wake attempt — setting DOCKED "
            "(deep sleep only occurs on dock)",
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

        # Deep sleep detection: not broadcasting + high battery + non-dock state.
        # High battery = on dock. A truly active robot broadcasts every ~1.5s.
        if (
            not self.client.robot_awake
            and state.battery_level >= DOCK_BATTERY_THRESHOLD
            and state.working_status not in (
                WorkingStatus.DOCKED, WorkingStatus.CHARGED, WorkingStatus.UNKNOWN,
            )
        ):
            await self._fix_deep_sleep_state(state, "poll")

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
