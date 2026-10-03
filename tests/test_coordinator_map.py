"""Tests for NarwalCoordinator map refresh logic.

Covers MAP-04 (post-cleaning map refresh) validation gaps:
  - _on_state_update triggers _fetch_missing_map when map_data is None
  - _was_cleaning / _prev_working_status tracks state transitions
  - Return-to-dock transition triggers dock status refresh
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

# Install HA stubs before any custom_components import
import tests.ha_stubs  # noqa: E402

tests.ha_stubs.install()

from custom_components.narwal.coordinator import NarwalCoordinator  # noqa: E402
from custom_components.narwal.narwal_client import MapData, NarwalState  # noqa: E402
from custom_components.narwal.narwal_client.const import WorkingStatus  # noqa: E402


class TestCoordinatorMapRefresh:
    """Tests for coordinator map fetch and state transition detection."""

    def _make_coordinator(self) -> NarwalCoordinator:
        """Create a NarwalCoordinator with mocked hass and entry."""
        mock_hass = MagicMock()
        mock_entry = MagicMock()
        mock_entry.data = {
            "host": "10.0.0.100",
            "port": 9002,
            "device_id": "test_device",
            "product_key": "QoEsI5qYXO",
        }

        coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
        coordinator.hass = mock_hass
        coordinator.config_entry = mock_entry
        coordinator.client = MagicMock()
        coordinator.client.state = NarwalState()
        coordinator._consecutive_failures = 0
        coordinator._max_failures = 5
        coordinator._fast_poll_remaining = 0
        coordinator._listen_task = None
        coordinator._map_fetch_pending = False
        coordinator._last_display_map_resub = None
        coordinator._last_status_resub = 0.0
        coordinator._prev_working_status = WorkingStatus.UNKNOWN
        coordinator.active_clean_work_mode = None
        coordinator.active_room_clean_settings = {}
        coordinator._map_display_cache_store = MagicMock()
        coordinator._map_display_cache_store.async_save = AsyncMock()
        coordinator._map_display_cache_signature = ()
        coordinator._pending_map_display_cache_snapshot = None
        coordinator._pending_map_display_cache_restore = None
        coordinator._map_display_cache_save_task = None
        coordinator._map_display_cache_last_save = 0.0
        coordinator._map_display_cache_restored = False
        coordinator.update_interval = None
        coordinator.async_set_updated_data = MagicMock()
        def _close_background_task(*args: object) -> None:
            for arg in args:
                if hasattr(arg, "close"):
                    arg.close()

        mock_entry.async_create_background_task = MagicMock(side_effect=_close_background_task)
        mock_hass.async_create_task = MagicMock(side_effect=_close_background_task)
        # Prevent TypeError on display_map dropout check when is_cleaning
        coordinator.client.last_display_map_age = 0.0
        coordinator.client.last_subscription_age = float("inf")
        return coordinator

    def test_missing_map_triggers_fetch(self) -> None:
        """When map_data is None and not already pending, schedule map fetch."""
        coordinator = self._make_coordinator()
        state = NarwalState()
        state.map_data = None  # no map
        state.working_status = WorkingStatus.STANDBY

        coordinator._on_state_update(state)

        assert coordinator._map_fetch_pending is True
        coordinator.config_entry.async_create_background_task.assert_called_once()
        # Verify the task name contains "map_fetch"
        call_args = coordinator.config_entry.async_create_background_task.call_args
        assert "map_fetch" in call_args[0][2]

    def test_map_present_no_fetch(self) -> None:
        """When map_data exists, no map fetch is triggered."""
        coordinator = self._make_coordinator()
        state = NarwalState()
        state.map_data = MagicMock()  # map exists
        state.working_status = WorkingStatus.STANDBY

        coordinator._on_state_update(state)

        # No background task should be created for map fetch
        # (there might be other tasks, so check none have "map_fetch" in name)
        for call in coordinator.config_entry.async_create_background_task.call_args_list:
            assert "map_fetch" not in call[0][2]

    def test_map_fetch_not_duplicated(self) -> None:
        """When map fetch is already pending, don't schedule another."""
        coordinator = self._make_coordinator()
        coordinator._map_fetch_pending = True
        state = NarwalState()
        state.map_data = None
        state.working_status = WorkingStatus.STANDBY

        coordinator._on_state_update(state)

        # No new background task created
        coordinator.config_entry.async_create_background_task.assert_not_called()

    def test_cleaning_to_standby_triggers_dock_refresh(self) -> None:
        """Transition from CLEANING to STANDBY triggers dock status refresh."""
        coordinator = self._make_coordinator()
        coordinator._prev_working_status = WorkingStatus.CLEANING
        state = NarwalState()
        state.map_data = MagicMock()  # avoid map fetch
        state.working_status = WorkingStatus.STANDBY

        coordinator._on_state_update(state)

        # hass.async_create_task should be called for dock refresh
        coordinator.hass.async_create_task.assert_called_once()
        assert coordinator._prev_working_status == WorkingStatus.STANDBY

    def test_cleaning_alt_to_standby_triggers_dock_refresh(self) -> None:
        """Transition from CLEANING_ALT to STANDBY also triggers dock refresh."""
        coordinator = self._make_coordinator()
        coordinator._prev_working_status = WorkingStatus.CLEANING_ALT
        state = NarwalState()
        state.map_data = MagicMock()
        state.working_status = WorkingStatus.STANDBY

        coordinator._on_state_update(state)

        coordinator.hass.async_create_task.assert_called_once()

    def test_standby_to_cleaning_no_dock_refresh(self) -> None:
        """Transition from STANDBY to CLEANING does NOT trigger dock refresh."""
        coordinator = self._make_coordinator()
        coordinator._prev_working_status = WorkingStatus.STANDBY
        state = NarwalState()
        state.map_data = MagicMock()
        state.working_status = WorkingStatus.CLEANING

        coordinator._on_state_update(state)

        coordinator.hass.async_create_task.assert_not_called()

    def test_prev_working_status_tracks_transitions(self) -> None:
        """_prev_working_status updates after each _on_state_update call."""
        coordinator = self._make_coordinator()
        state = NarwalState()
        state.map_data = MagicMock()

        # UNKNOWN -> CLEANING
        state.working_status = WorkingStatus.CLEANING
        coordinator._on_state_update(state)
        assert coordinator._prev_working_status == WorkingStatus.CLEANING

        # CLEANING -> STANDBY
        state.working_status = WorkingStatus.STANDBY
        coordinator._on_state_update(state)
        assert coordinator._prev_working_status == WorkingStatus.STANDBY

        # STANDBY -> DOCKED
        state.working_status = WorkingStatus.DOCKED
        coordinator._on_state_update(state)
        assert coordinator._prev_working_status == WorkingStatus.DOCKED

    def test_push_update_resets_fast_poll(self) -> None:
        """Push update during fast polling restores normal polling."""
        coordinator = self._make_coordinator()
        coordinator._fast_poll_remaining = 3

        from custom_components.narwal.coordinator import POLL_INTERVAL

        state = NarwalState()
        state.map_data = MagicMock()
        state.working_status = WorkingStatus.STANDBY

        coordinator._on_state_update(state)

        assert coordinator._fast_poll_remaining == 0
        assert coordinator.update_interval == POLL_INTERVAL


def _background_task_names(coordinator: NarwalCoordinator) -> list[str]:
    create = coordinator.config_entry.async_create_background_task
    return [call.args[2] for call in create.call_args_list]


class TestDisplayMapDropout:
    """display_map dropout recovery must not fire on silence it caused itself."""

    # A frozen clock, so these tests don't depend on the host's uptime.
    FAKE_NOW = 1_000_000.0

    def _update(
        self,
        coordinator: NarwalCoordinator,
        state: NarwalState,
        now: float = FAKE_NOW,
    ) -> None:
        with patch(
            "custom_components.narwal.coordinator.time.monotonic",
            return_value=now,
        ):
            coordinator._on_state_update(state)

    def _cleaning_coordinator(self) -> NarwalCoordinator:
        coordinator = TestCoordinatorMapRefresh()._make_coordinator()
        coordinator.client.state.map_data = MapData()
        return coordinator

    def _cleaning_state(self) -> NarwalState:
        state = NarwalState()
        state.map_data = MapData()
        state.working_status = WorkingStatus.CLEANING
        return state

    def test_fresh_connection_is_not_a_dropout(self) -> None:
        """No display_map yet (age 999) right after subscribing is not a dropout."""
        coordinator = self._cleaning_coordinator()
        coordinator.client.last_display_map_age = 999.0
        coordinator.client.last_subscription_age = 0.2

        self._update(coordinator, self._cleaning_state())

        assert "narwal_resub" not in _background_task_names(coordinator)

    def test_wake_burst_resubscribe_suppresses_duplicate(self) -> None:
        """A keepalive burst that just re-subscribed already did the recovery."""
        coordinator = self._cleaning_coordinator()
        coordinator.client.last_display_map_age = 48.0
        coordinator.client.last_subscription_age = 0.1

        self._update(coordinator, self._cleaning_state())

        assert "narwal_resub" not in _background_task_names(coordinator)

    def test_real_dropout_still_resubscribes(self) -> None:
        coordinator = self._cleaning_coordinator()
        coordinator.client.last_display_map_age = 48.0
        coordinator.client.last_subscription_age = 300.0

        self._update(coordinator, self._cleaning_state())

        assert "narwal_resub" in _background_task_names(coordinator)

    def test_first_dropout_resubscribes_on_a_freshly_booted_host(self) -> None:
        """The cooldown must not depend on time.monotonic() being large.

        time.monotonic() is system uptime on Linux. The cooldown timestamp used
        to start at 0.0, so on a host up for less than 45s the first genuine
        dropout was held back until uptime passed 45s.
        """
        coordinator = self._cleaning_coordinator()
        coordinator.client.last_display_map_age = 48.0
        coordinator.client.last_subscription_age = 300.0

        self._update(coordinator, self._cleaning_state(), now=20.0)

        assert "narwal_resub" in _background_task_names(coordinator)

    def test_cooldown_still_holds_back_a_repeat(self) -> None:
        coordinator = self._cleaning_coordinator()
        coordinator.client.last_display_map_age = 48.0
        coordinator.client.last_subscription_age = 300.0

        self._update(coordinator, self._cleaning_state(), now=20.0)
        self._update(coordinator, self._cleaning_state(), now=40.0)

        assert _background_task_names(coordinator).count("narwal_resub") == 1
