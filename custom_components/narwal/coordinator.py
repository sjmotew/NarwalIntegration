"""DataUpdateCoordinator for Narwal vacuum."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import time
import zlib
from bisect import bisect_left
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, NO_BROADCAST_PRODUCT_KEYS
from .dock_tasks import (
    ROBOT_START_STOP_REQUIRED_DOCK_TASKS,
    can_start_robot_clean,
    can_stop_dock_task,
    dock_task_blocks_robot_return,
)
from .narwal_client import (
    CleaningRoute,
    CommandResponse,
    FanLevel,
    MapDisplayData,
    MopHumidity,
    MopStrengthLevel,
    NarwalClient,
    NarwalConnectionError,
    NarwalState,
    RoomCleanSettings,
    WorkMode,
)
from .narwal_client.const import ACTIVE_CLEANING_STATUSES, WorkingStatus

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL = timedelta(seconds=60)

# Fast re-poll when state is incomplete (robot asleep at startup)
FAST_POLL_INTERVAL = timedelta(seconds=10)
FAST_POLL_MAX = 6  # up to 60s of fast polling before falling back to normal

# Consumable alerts change over weeks — poll every ~30 min (30 * POLL_INTERVAL).
CONSUMABLE_POLL_EVERY = 30
MAP_DISPLAY_CACHE_VERSION = 1
ROOM_SELECTION_STORE_VERSION = 1
# Retained routes can reach roughly 0.5 MiB at the point cap. Persist session
# boundaries immediately, but checkpoint point growth at a storage-safe cadence.
MAP_DISPLAY_CACHE_SAVE_INTERVAL = 300.0
NATIVE_TRAJECTORY_MAX_POINTS = 50_000
NATIVE_TRAJECTORY_RECENT_TAIL_POINTS = 200
NATIVE_TRAJECTORY_RESTORE_MIN_OVERLAP_POINTS = 3
NATIVE_TRAJECTORY_RESTORE_GRACE = 60.0

# The robot only broadcasts working_status and display_map while an
# active_robot_publish subscription is live, and that subscription lasts
# TOPIC_SUBSCRIPTION_TTL seconds. Renew well inside the window: once it lapses the
# robot goes quiet on both topics, the vacuum entity freezes on its last
# base_status-derived value, and the live map stops updating (#73).
TOPIC_SUBSCRIPTION_TTL = 600.0
TOPIC_RESUBSCRIBE_AFTER = 240.0
ROOM_CLEAN_SETTING_ATTRS = frozenset(field.name for field in fields(RoomCleanSettings))
ROOM_CLEAN_SETTING_VALUE_TYPES = {
    "work_mode": WorkMode,
    "fan": FanLevel,
    "water": MopHumidity,
    "mop_strength": MopStrengthLevel,
    "route": CleaningRoute,
}
MOP_WORK_MODES = frozenset(
    {WorkMode.MOP, WorkMode.VACUUM_THEN_MOP, WorkMode.VACUUM_AND_MOP}
)
VACUUM_WORK_MODES = frozenset(
    {WorkMode.VACUUM, WorkMode.VACUUM_THEN_MOP, WorkMode.VACUUM_AND_MOP}
)


@dataclass(frozen=True)
class _MapDisplayCacheSnapshot:
    """Lightweight display-map trajectory snapshot queued for persistence."""

    map_id: int
    map_created_at: int
    active_clean: bool
    display: MapDisplayData

    @property
    def trajectory_signature(self) -> tuple[int, int, int] | tuple[()]:
        """Return the native trajectory signature for this snapshot."""
        return self.display.trajectory_signature


def _status_payload(response: CommandResponse) -> dict[str, object] | None:
    """Return the decoded robot_base_status payload from a response."""
    if not response.accepted or not isinstance(response.data, dict):
        return None
    status_data = response.data.get("2")
    if isinstance(status_data, dict) and status_data:
        return status_data
    return None


def _has_dock_status_payload(response: CommandResponse) -> bool:
    """Return True when a response carries the dock status submessage."""
    status_data = _status_payload(response)
    if status_data is None:
        return False
    field3 = status_data.get("3")
    if isinstance(field3, list):
        field3 = field3[0] if field3 else None
    if not isinstance(field3, dict):
        return False
    return bool({"1", "2", "3", "7", "10", "12", "18"}.intersection(field3))


@dataclass
class CleanSettings(RoomCleanSettings):
    """User-selected clean parameters for the next clean or live controls.

    Select/number entities mutate this, and clean-start paths read it. Each
    entity persists its value via RestoreEntity, so settings survive restarts.
    Only fan and water have live setters; the other parameters take effect at
    the next start.
    """

    work_mode: WorkMode = WorkMode.VACUUM_AND_MOP
    fan: FanLevel = FanLevel.NORMAL
    water: MopHumidity = MopHumidity.NORMAL
    mop_strength: MopStrengthLevel = MopStrengthLevel.NORMAL
    passes: int = 1
    route: CleaningRoute = CleaningRoute.METICULOUS


def _state_attr_is_true(state: NarwalState, attr: str) -> bool:
    """Return True only for explicit boolean state properties."""
    return getattr(state, attr, False) is True


def has_blocking_error(state: NarwalState | None) -> bool:
    """Return True when the robot reports a command-blocking error."""
    return (
        state is None
        or state.working_status == WorkingStatus.ERROR
        or _state_attr_is_true(state, "has_error")
    )


def is_confirmed_terminal_clean_state(state: NarwalState) -> bool:
    """Return True when reconciled telemetry confirms the clean has ended."""
    if has_blocking_error(state) or state.working_status == WorkingStatus.TASK_COMPLETED:
        return True
    if _state_attr_is_true(state, "has_paused_clean_task_context"):
        return False
    return (
        not state.has_assumed_robot_clean
        and not state.has_explicit_off_dock_signal
        and (state.is_docked or state.has_recent_terminal_working_status)
    )


def is_active_clean_session(state: NarwalState | None) -> bool:
    """Return True while clean parameters are locked to the current task."""
    if state is None:
        return False
    return (
        state.working_status in ACTIVE_CLEANING_STATUSES
        or _state_attr_is_true(state, "has_assumed_robot_clean")
        or _state_attr_is_true(state, "has_recent_active_working_status")
        or _state_attr_is_true(state, "has_paused_clean_task_context")
    ) and not _state_attr_is_true(state, "is_returning")


def is_clean_session_context(state: NarwalState | None) -> bool:
    """Return True while robot-side clean task context is still current."""
    if state is None:
        return False
    return (
        state.working_status in ACTIVE_CLEANING_STATUSES
        or state.working_status == WorkingStatus.REMAPPING
        or (
            state.working_status == WorkingStatus.TASK_COMPLETED
            and not state.has_current_dock_presence_signal
        )
        or _state_attr_is_true(state, "has_assumed_robot_clean")
        or _state_attr_is_true(state, "has_recent_active_working_status")
        or _state_attr_is_true(state, "has_paused_clean_task_context")
        or _state_attr_is_true(state, "is_returning")
    )


def is_live_clean_setting_available(state: NarwalState | None) -> bool:
    """Return True when live clean settings can be changed during a task."""
    if has_blocking_error(state):
        return False
    if state.working_status in {WorkingStatus.REMAPPING, WorkingStatus.TASK_COMPLETED}:
        return False
    return (
        (_state_attr_is_true(state, "is_cleaning") or is_active_clean_session(state))
        and not dock_task_blocks_robot_return(state)
    )


def clean_setting_applies_to_mode(attr: str, work_mode: WorkMode | None) -> bool:
    """Return True when a clean setting is meaningful for the selected mode."""
    if work_mode is None:
        return attr not in {"fan", "water", "mop_strength"}
    if attr in {"water", "mop_strength"}:
        return work_mode in MOP_WORK_MODES
    if attr == "fan":
        return work_mode in VACUUM_WORK_MODES
    return True


def is_narwal_task_busy(state: NarwalState | None) -> bool:
    """Return True while the robot or dock is busy with a task phase."""
    if state is None:
        return False
    return (
        state.working_status in ACTIVE_CLEANING_STATUSES
        or state.working_status == WorkingStatus.REMAPPING
        or (
            state.working_status == WorkingStatus.TASK_COMPLETED
            and not state.has_current_dock_presence_signal
        )
        or _state_attr_is_true(state, "has_assumed_robot_clean")
        or _state_attr_is_true(state, "has_recent_active_working_status")
        or _state_attr_is_true(state, "has_paused_clean_task_context")
        or _state_attr_is_true(state, "is_returning")
        or _state_attr_is_true(state, "is_charging_to_resume")
        or (
            _state_attr_is_true(state, "is_station_active")
            and _state_attr_is_true(state, "blocks_robot_start_for_dock_task")
        )
    )


def can_edit_pending_clean_settings(state: NarwalState | None) -> bool:
    """Return True when pending next-clean settings can be edited locally."""
    if state is None:
        return True
    if has_blocking_error(state):
        return False
    return not is_narwal_task_busy(state)


def can_start_cleaning(state: NarwalState | None) -> bool:
    """Return True when a new robot clean command can be sent now."""
    if has_blocking_error(state) or state.working_status == WorkingStatus.UNKNOWN:
        return False
    return (
        state.is_docked
        and not is_clean_session_context(state)
        and can_start_robot_clean(state)
    )


def _can_start_cleaning_without_dock_stop(state: NarwalState | None) -> bool:
    """Return True when refreshed state permits sending a clean command now."""
    return (
        can_start_cleaning(state)
        and not state.has_unmapped_active_dock_task
        and state.assumed_active_dock_task is None
    )


def can_prepare_clean_start(
    state: NarwalState | None,
    *,
    allow_dock_stop: bool = True,
) -> bool:
    """Return True when a clean start can run now or after a safe dock stop."""
    if state is None:
        return False
    active_tasks = state.active_dock_task_keys
    if _can_start_cleaning_without_dock_stop(state):
        return True
    if not allow_dock_stop:
        return False
    if (
        has_blocking_error(state)
        or not state.is_docked
        or is_clean_session_context(state)
        or state.has_unmapped_active_dock_task
        or state.assumed_active_dock_task is not None
    ):
        return False

    return (
        len(active_tasks) == 1
        and active_tasks[0] in ROBOT_START_STOP_REQUIRED_DOCK_TASKS
        and can_stop_dock_task(state, active_tasks[0])
    )


def can_pause_cleaning(state: NarwalState | None) -> bool:
    """Return True when the active robot clean can be paused."""
    if has_blocking_error(state):
        return False
    return (
        (
            _state_attr_is_true(state, "is_cleaning")
            or state.working_status == WorkingStatus.REMAPPING
        )
        and not _state_attr_is_true(state, "is_paused")
        and not dock_task_blocks_robot_return(state)
    )


def can_resume_cleaning(state: NarwalState | None) -> bool:
    """Return True when a paused robot clean can be resumed."""
    if has_blocking_error(state):
        return False
    return (
        (
            state.working_status in (*ACTIVE_CLEANING_STATUSES, WorkingStatus.REMAPPING)
            or _state_attr_is_true(state, "has_paused_clean_task_context")
        )
        and _state_attr_is_true(state, "is_paused")
        and not dock_task_blocks_robot_return(state)
    )


def can_stop_cleaning(state: NarwalState | None) -> bool:
    """Return True when a robot-side clean task can be stopped."""
    if has_blocking_error(state):
        return False
    return is_clean_session_context(state)


def can_return_home(state: NarwalState | None) -> bool:
    """Return True when the robot can be recalled to the dock."""
    if has_blocking_error(state):
        return False
    return (
        not state.is_docked
        and state.working_status != WorkingStatus.TASK_COMPLETED
        and not _state_attr_is_true(state, "is_returning")
        and not dock_task_blocks_robot_return(state)
    )


def can_locate_robot(state: NarwalState | None) -> bool:
    """Return True when the locate command can be sent."""
    if has_blocking_error(state):
        return False
    return not dock_task_blocks_robot_return(state)


class NarwalCoordinator(DataUpdateCoordinator[NarwalState]):
    """Push-mode coordinator for Narwal vacuum.

    Primary data source is WebSocket broadcasts (every ~1.5s when awake).
    Fallback polling every 60s via get_status() in case broadcasts stop.

    Setup is kept fast: connect, try a few commands (which may time out if
    the robot is asleep), then start the listener. The listener's keepalive
    loop handles waking the robot — no blocking wake call during setup.
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
        product_key = entry.data.get("product_key")
        topic_prefix = f"/{product_key}" if product_key else None
        supports_broadcasts = product_key not in NO_BROADCAST_PRODUCT_KEYS
        self.client = NarwalClient(
            host=entry.data["host"],
            port=entry.data["port"],
            device_id=entry.data.get("device_id", ""),
            topic_prefix=topic_prefix,
            supports_broadcasts=supports_broadcasts,
        )
        self.clean_settings = CleanSettings()
        self.room_clean_settings: dict[tuple[str | None, int], RoomCleanSettings] = {}
        self.room_clean_settings_customized: dict[tuple[str | None, int], set[str]] = {}
        self.selected_clean_rooms: dict[str | None, set[int]] = {}
        self._room_selection_store = Store(
            hass,
            ROOM_SELECTION_STORE_VERSION,
            f"{DOMAIN}_room_selection_{entry.entry_id}",
        )
        self._room_selection_save_lock = asyncio.Lock()
        self._room_selection_store_loaded = False
        self._room_profile_store_loaded = False
        self._room_selection_dirty_maps: set[str | None] = set()
        self._room_profile_pending_resolution: set[int] = set()
        self.active_clean_work_mode: WorkMode | None = None
        self.active_room_clean_settings: dict[int, RoomCleanSettings] = {}
        self.active_clean_setting_overrides: dict[str, object] = {}
        self._listen_task: asyncio.Task[None] | None = None
        self._fast_poll_remaining = 0
        self._prev_working_status = WorkingStatus.UNKNOWN
        self._clean_session_active = False
        self._map_fetch_pending = False
        self._last_display_map_resub: float = 0.0
        self._last_topic_subscribe: float = 0.0
        self._consecutive_failures = 0
        self._max_failures = 5  # 5 * 60s = 5 minutes before entities go unavailable
        self._dock_status_refresh_failed = True
        self._consumable_poll_countdown = 0
        self._map_display_cache_store = Store(
            hass,
            MAP_DISPLAY_CACHE_VERSION,
            f"{DOMAIN}_map_display_{entry.entry_id}",
        )
        self._map_display_cache_signature: tuple[int, int, int] | tuple[()] = ()
        self._map_display_cache_active_clean: bool | None = None
        self._map_display_cache_last_save = 0.0
        self._pending_map_display_cache_snapshot: _MapDisplayCacheSnapshot | None = None
        self._pending_map_display_cache_restore: dict[str, object] | None = None
        self._map_display_cache_save_task: asyncio.Task[None] | None = None
        self._map_display_cache_clear_event = asyncio.Event()
        self._map_display_cache_clear_event.set()
        self._map_display_cache_clear_lock = asyncio.Lock()
        self._map_display_cache_write_lock = asyncio.Lock()
        self._map_display_cache_clear_count = 0
        self._map_display_cache_clear_pending = False
        self._retained_map_display: MapDisplayData | None = None
        self._retained_map_identity: tuple[int, int] | None = None
        self._map_display_cache_restored = False
        self._map_display_cache_restored_from_active = False
        self._map_display_cache_restored_at = 0.0
        self.dock_action_lock = asyncio.Lock()

    @property
    def has_fresh_state(self) -> bool:
        """Return true when the coordinator has not returned stale poll data."""
        return self.last_update_success and not self._dock_status_refresh_failed

    def _mark_dock_status_refresh_failed(self) -> None:
        """Record that dock-control state may be stale."""
        self._dock_status_refresh_failed = True

    def _mark_dock_status_refresh_succeeded(self) -> None:
        """Record that dock-control state came from a current base-status payload."""
        self._dock_status_refresh_failed = False

    def default_room_clean_settings(self) -> RoomCleanSettings:
        """Return a room-clean profile copied from the current global defaults."""
        return RoomCleanSettings(
            work_mode=self.clean_settings.work_mode,
            fan=self.clean_settings.fan,
            water=self.clean_settings.water,
            mop_strength=self.clean_settings.mop_strength,
            passes=self.clean_settings.passes,
            route=self.clean_settings.route,
        )

    @staticmethod
    def shared_room_clean_work_mode(
        room_settings: Mapping[int, RoomCleanSettings],
    ) -> WorkMode | None:
        """Return the shared work mode, or None for a mixed-room task."""
        modes = {settings.work_mode for settings in room_settings.values()}
        if len(modes) == 1:
            return next(iter(modes))
        return None

    def record_accepted_clean_start(
        self,
        room_settings: Mapping[int, RoomCleanSettings],
    ) -> None:
        """Record effective room profiles for the accepted robot task."""
        self.active_clean_setting_overrides = {}
        self._clean_session_active = True
        self.active_clean_work_mode = self.shared_room_clean_work_mode(
            room_settings
        )
        self.active_room_clean_settings = {
            room_id: replace(settings)
            for room_id, settings in room_settings.items()
        }

    def active_clean_setting(self, attr: str) -> object | None:
        """Return the effective live value for the current clean, if known."""
        state = self.data or self.client.state
        if not is_clean_session_context(state):
            return None
        overrides = getattr(self, "active_clean_setting_overrides", {})
        if attr in overrides:
            return overrides[attr]
        if (
            state.current_room_id is not None
            and state.current_room_id in self.active_room_clean_settings
        ):
            return getattr(
                self.active_room_clean_settings[state.current_room_id], attr
            )
        values = {
            getattr(settings, attr)
            for settings in self.active_room_clean_settings.values()
        }
        return next(iter(values)) if len(values) == 1 else None

    def set_active_clean_setting(self, attr: str, value: object) -> None:
        """Update the displayed live value after an accepted runtime command."""
        if not hasattr(self, "active_clean_setting_overrides"):
            self.active_clean_setting_overrides = {}
        self.active_clean_setting_overrides[attr] = value
        for settings in self.active_room_clean_settings.values():
            setattr(settings, attr, value)

    def clean_setting_applicability_mode(
        self, *, live: bool = False
    ) -> WorkMode | None:
        """Return the mode used to decide whether fan/water controls apply."""
        if live:
            state = self.data or self.client.state
            if is_clean_session_context(state):
                if (
                    state.current_room_id is not None
                    and state.current_room_id in self.active_room_clean_settings
                ):
                    return self.active_room_clean_settings[
                        state.current_room_id
                    ].work_mode
                return self.active_clean_work_mode
        return self.clean_settings.work_mode

    def _sync_active_clean_context(self, state: NarwalState) -> None:
        """Clear accepted-task metadata once the robot is no longer in a clean context."""
        if not is_clean_session_context(state):
            self.active_clean_work_mode = None
            self.active_room_clean_settings.clear()
            if hasattr(self, "active_clean_setting_overrides"):
                self.active_clean_setting_overrides.clear()

    @staticmethod
    def _normalise_room_settings_map_id(map_id: object) -> str | None:
        """Return a stable map id for room profiles."""
        if map_id in (None, "", 0, "0"):
            return None
        return str(map_id)

    def room_settings_map_id(self, map_data: object | None = None) -> str | None:
        """Return the active map id used to scope room profiles."""
        if map_data is None:
            state = self.data or self.client.state
            map_data = getattr(state, "map_data", None) if state is not None else None
        return self._normalise_room_settings_map_id(getattr(map_data, "map_id", None))

    def _room_clean_settings_key(
        self,
        room_id: int,
        map_id: str | None = None,
    ) -> tuple[str | None, int]:
        """Return the storage key for a room profile."""
        map_key = map_id if map_id is not None else self.room_settings_map_id()
        if map_key is not None:
            self._resolve_identified_room_state(map_key)
        return (map_key, room_id)

    def room_clean_settings_for(
        self,
        room_id: int,
        *,
        map_id: str | None = None,
    ) -> RoomCleanSettings:
        """Return the configured room-clean profile for a room."""
        key = self._room_clean_settings_key(room_id, map_id)
        if key not in self.room_clean_settings:
            self.room_clean_settings[key] = self.default_room_clean_settings()
        return self.room_clean_settings[key]

    def effective_room_clean_settings_for(
        self,
        room_id: int,
        *,
        map_id: str | None = None,
    ) -> RoomCleanSettings:
        """Return the room profile after applying current global fallbacks."""
        return self.room_clean_settings_for_rooms([room_id], map_id=map_id)[room_id]

    def room_clean_settings_for_rooms(
        self,
        room_ids: list[int],
        *,
        default: RoomCleanSettings | None = None,
        map_id: str | None = None,
        use_room_profiles: bool = True,
    ) -> dict[int, RoomCleanSettings]:
        """Return stored room-clean profiles for a set of rooms.

        Missing rooms use the supplied default without creating profile entries.
        """
        map_key = map_id if map_id is not None else self.room_settings_map_id()
        if map_key is not None:
            self._resolve_identified_room_state(map_key)
        fallback = default or self.default_room_clean_settings()
        if not use_room_profiles:
            return {room_id: fallback for room_id in room_ids}
        customized = getattr(self, "room_clean_settings_customized", {})
        settings: dict[int, RoomCleanSettings] = {}
        for room_id in room_ids:
            key = (map_key, room_id)
            profile = self.room_clean_settings.get(key)
            custom_fields = customized.get(key, set())
            if profile is None or not custom_fields:
                settings[room_id] = fallback
                continue
            merged = RoomCleanSettings(
                work_mode=fallback.work_mode,
                fan=fallback.fan,
                water=fallback.water,
                mop_strength=fallback.mop_strength,
                passes=fallback.passes,
                route=fallback.route,
            )
            for attr in custom_fields:
                if attr in ROOM_CLEAN_SETTING_ATTRS:
                    setattr(merged, attr, getattr(profile, attr))
            settings[room_id] = merged
        return settings

    def selected_clean_room_ids_for(
        self,
        room_ids: list[int],
        *,
        map_id: str | None = None,
    ) -> list[int]:
        """Return selected rooms without broadening a stale explicit selection."""
        if not getattr(self, "_room_selection_store_loaded", True):
            return []
        map_key = map_id if map_id is not None else self.room_settings_map_id()
        if map_key is not None:
            self._resolve_identified_room_state(map_key)
        selected = self.selected_clean_rooms.get(map_key, set())
        if not selected and map_key is not None:
            selected = self.selected_clean_rooms.get(None, set())
        if not selected:
            return list(room_ids)
        return [room_id for room_id in room_ids if room_id in selected]

    def has_selected_clean_rooms(self, *, map_id: str | None = None) -> bool:
        """Return whether the current map has an explicit next-clean selection."""
        map_key = map_id if map_id is not None else self.room_settings_map_id()
        if map_key is not None:
            self._resolve_identified_room_state(map_key)
        return bool(
            self.selected_clean_rooms.get(map_key)
            or (map_key is not None and self.selected_clean_rooms.get(None))
        )

    def is_room_selected_for_clean(
        self,
        room_id: int,
        *,
        map_id: str | None = None,
    ) -> bool:
        """Return True when a room is selected for the next vacuum start."""
        map_key = map_id if map_id is not None else self.room_settings_map_id()
        if map_key is not None:
            self._resolve_identified_room_state(map_key)
        selected = self.selected_clean_rooms.get(map_key, set())
        if not selected and map_key is not None:
            selected = self.selected_clean_rooms.get(None, set())
        return room_id in selected

    def set_room_selected_for_clean(
        self,
        room_id: int,
        selected: bool,
        *,
        map_id: str | None = None,
    ) -> None:
        """Set whether a room is included in the next vacuum start."""
        map_key = map_id if map_id is not None else self.room_settings_map_id()
        if map_key is not None:
            self._resolve_identified_room_state(map_key)
        selected_rooms = self.selected_clean_rooms.setdefault(map_key, set())
        if selected:
            selected_rooms.add(room_id)
        else:
            selected_rooms.discard(room_id)
            if not selected_rooms:
                self.selected_clean_rooms.pop(map_key, None)
        if not hasattr(self, "_room_selection_dirty_maps"):
            self._room_selection_dirty_maps = set()
        self._room_selection_dirty_maps.add(map_key)
        self._schedule_room_selection_save()

    def _resolve_identified_room_state(self, map_id: str) -> None:
        """Resolve unidentified state and persist the newly known map key."""
        if not self._migrate_unidentified_room_state(map_id):
            return
        if not hasattr(self, "_room_selection_dirty_maps"):
            self._room_selection_dirty_maps = set()
        if None in self._room_selection_dirty_maps:
            self._room_selection_dirty_maps.remove(None)
        self._room_selection_dirty_maps.add(map_id)
        self._schedule_room_selection_save()

    def _migrate_unidentified_room_state(self, map_id: str) -> bool:
        """Move unresolved selection and profile state to an identified map."""
        changed = False
        if None in self.selected_clean_rooms:
            unresolved = self.selected_clean_rooms.pop(None)
            dirty_maps = getattr(self, "_room_selection_dirty_maps", set())
            if map_id not in self.selected_clean_rooms or None in dirty_maps:
                self.selected_clean_rooms[map_id] = unresolved
            changed = True
        customized = getattr(self, "room_clean_settings_customized", {})
        settings = getattr(self, "room_clean_settings", {})
        pending_profiles = getattr(self, "_room_profile_pending_resolution", set())
        for source_key in [key for key in settings if key[0] is None]:
            target_key = (map_id, source_key[1])
            source_profile = settings.pop(source_key)
            source_fields = customized.get(source_key, set())
            if target_key not in settings:
                settings[target_key] = source_profile
                if source_fields:
                    customized[target_key] = set(source_fields)
            elif source_key[1] in pending_profiles:
                target_profile = settings[target_key]
                for attr in source_fields:
                    setattr(target_profile, attr, getattr(source_profile, attr))
                customized.setdefault(target_key, set()).update(source_fields)
            customized.pop(source_key, None)
            pending_profiles.discard(source_key[1])
            changed = True
        return changed

    def _room_selection_store_payload(
        self,
        *,
        preserved_profiles: list[object] | None = None,
    ) -> dict[str, object]:
        """Return durable room selections and customized profile fields."""
        profiles: list[dict[str, object]] = []
        customized = getattr(self, "room_clean_settings_customized", {})
        room_settings = getattr(self, "room_clean_settings", {})
        for (map_id, room_id), custom_fields in sorted(
            customized.items(),
            key=lambda item: (item[0][0] or "", item[0][1]),
        ):
            profile = room_settings.get((map_id, room_id))
            if profile is None or not custom_fields:
                continue
            profiles.append(
                {
                    "map_id": map_id,
                    "room_id": room_id,
                    "values": {
                        attr: int(getattr(profile, attr))
                        for attr in sorted(custom_fields)
                        if attr in ROOM_CLEAN_SETTING_ATTRS
                    },
                    **(
                        {"pending_map_resolution": True}
                        if map_id is None
                        and room_id
                        in getattr(self, "_room_profile_pending_resolution", set())
                        else {}
                    ),
                }
            )
        return {
            "maps": [
                {
                    "map_id": map_id,
                    "room_ids": sorted(room_ids),
                    **(
                        {"pending_map_resolution": True}
                        if map_id is None
                        and None
                        in getattr(self, "_room_selection_dirty_maps", set())
                        else {}
                    ),
                }
                for map_id, room_ids in sorted(
                    self.selected_clean_rooms.items(),
                    key=lambda item: item[0] or "",
                )
                if room_ids
            ],
            "profiles": profiles if preserved_profiles is None else preserved_profiles,
        }

    @staticmethod
    def _deserialize_room_selection_maps(
        maps: object,
    ) -> tuple[dict[str | None, set[int]], set[str | None]] | None:
        """Validate and deserialize the maps portion of stored room state."""
        if not isinstance(maps, list):
            return None
        restored: dict[str | None, set[int]] = {}
        pending_resolution: set[str | None] = set()
        for item in maps:
            if not isinstance(item, Mapping):
                return None
            map_id = item.get("map_id")
            room_ids = item.get("room_ids")
            pending = item.get("pending_map_resolution", False)
            if (map_id is not None and not isinstance(map_id, str)) or not isinstance(
                room_ids, list
            ):
                return None
            if not isinstance(pending, bool) or (pending and map_id is not None):
                return None
            if not room_ids or any(
                not isinstance(room_id, int)
                or isinstance(room_id, bool)
                or room_id <= 0
                for room_id in room_ids
            ):
                return None
            if map_id in restored:
                return None
            restored[map_id] = set(room_ids)
            if pending:
                pending_resolution.add(map_id)
        return restored, pending_resolution

    async def _async_restore_room_selections(self) -> None:
        """Restore explicit room selections independently of dynamic entities."""
        try:
            payload = await self._room_selection_store.async_load()
        except Exception:
            _LOGGER.debug("Could not restore room selections")
            return
        if payload is None:
            self._room_selection_store_loaded = True
            self._room_profile_store_loaded = True
            return
        if not isinstance(payload, Mapping):
            return
        parsed_maps = self._deserialize_room_selection_maps(payload.get("maps"))
        if parsed_maps is None:
            return
        restored, stored_dirty_maps = parsed_maps
        profiles = payload.get("profiles", [])
        if not isinstance(profiles, list):
            return
        restored_profiles: dict[tuple[str | None, int], RoomCleanSettings] = {}
        restored_customized: dict[tuple[str | None, int], set[str]] = {}
        restored_pending_profiles: set[int] = set()
        for item in profiles:
            if not isinstance(item, Mapping):
                return
            map_id = item.get("map_id")
            room_id = item.get("room_id")
            values = item.get("values")
            pending = item.get("pending_map_resolution", False)
            if (
                (map_id is not None and not isinstance(map_id, str))
                or not isinstance(room_id, int)
                or isinstance(room_id, bool)
                or room_id <= 0
                or not isinstance(values, Mapping)
                or not values
                or not isinstance(pending, bool)
                or (pending and map_id is not None)
            ):
                return
            key = (map_id, room_id)
            if key in restored_profiles:
                return
            profile = RoomCleanSettings()
            custom_fields: set[str] = set()
            for attr, raw_value in values.items():
                if (
                    attr not in ROOM_CLEAN_SETTING_ATTRS
                    or not isinstance(raw_value, int)
                    or isinstance(raw_value, bool)
                ):
                    return
                if attr == "passes":
                    if raw_value not in (1, 2, 3):
                        return
                    value = raw_value
                else:
                    value_type = ROOM_CLEAN_SETTING_VALUE_TYPES.get(attr)
                    if value_type is None:
                        return
                    try:
                        value = value_type(raw_value)
                    except ValueError:
                        return
                setattr(profile, attr, value)
                custom_fields.add(attr)
            restored_profiles[key] = profile
            restored_customized[key] = custom_fields
            if pending:
                restored_pending_profiles.add(room_id)
        dirty_maps = getattr(self, "_room_selection_dirty_maps", set())
        for map_id in dirty_maps:
            if selected := self.selected_clean_rooms.get(map_id):
                restored[map_id] = set(selected)
            else:
                restored.pop(map_id, None)
        self.selected_clean_rooms = restored
        self.room_clean_settings = restored_profiles
        self.room_clean_settings_customized = restored_customized
        self._room_profile_pending_resolution = restored_pending_profiles
        self._room_selection_dirty_maps = stored_dirty_maps | dirty_maps
        self._room_selection_store_loaded = True
        self._room_profile_store_loaded = True

    def _schedule_room_selection_save(self) -> None:
        """Persist explicit room selections after a switch changes."""
        if not hasattr(self, "_room_selection_store"):
            return
        self.config_entry.async_create_background_task(
            self.hass,
            self._async_save_room_selections(),
            f"{DOMAIN}_room_selection_save",
        )

    async def _async_save_room_selections(self) -> None:
        """Serialize room-selection writes so the newest state wins."""
        async with self._room_selection_save_lock:
            preserved_profiles: list[object] | None = None
            if not self._room_selection_store_loaded:
                local_dirty_maps = getattr(
                    self, "_room_selection_dirty_maps", set()
                )
                stored_dirty_maps: set[str | None] = set()
                try:
                    stored = await self._room_selection_store.async_load()
                except Exception:
                    _LOGGER.debug("Could not reconcile room selections before save")
                    return
                if stored is None:
                    restored: dict[str | None, set[int]] = {}
                    self._room_profile_store_loaded = True
                elif not isinstance(stored, Mapping):
                    return
                else:
                    parsed = self._deserialize_room_selection_maps(stored.get("maps"))
                    profiles = stored.get("profiles", [])
                    if parsed is None or not isinstance(profiles, list):
                        return
                    restored, stored_dirty_maps = parsed
                    if not getattr(self, "_room_profile_store_loaded", True):
                        preserved_profiles = list(profiles)
                for map_id in local_dirty_maps:
                    if selected := self.selected_clean_rooms.get(map_id):
                        restored[map_id] = set(selected)
                    else:
                        restored.pop(map_id, None)
                self._room_selection_dirty_maps = (
                    stored_dirty_maps | local_dirty_maps
                )
                self.selected_clean_rooms = restored
                self._room_selection_store_loaded = True
            if not getattr(self, "_room_profile_store_loaded", True):
                if preserved_profiles is None:
                    try:
                        stored = await self._room_selection_store.async_load()
                    except Exception:
                        _LOGGER.debug("Could not reconcile room profiles before save")
                        return
                    if stored is None:
                        self._room_profile_store_loaded = True
                    elif isinstance(stored, Mapping) and isinstance(
                        stored.get("profiles", []), list
                    ):
                        preserved_profiles = list(stored.get("profiles", []))
                    else:
                        return
            payload = self._room_selection_store_payload(
                preserved_profiles=preserved_profiles
            )
            save_task = asyncio.create_task(
                self._room_selection_store.async_save(payload)
            )
            cancelled = False
            while not save_task.done():
                try:
                    await asyncio.shield(save_task)
                except asyncio.CancelledError:
                    cancelled = True
                except Exception:
                    break
            saved = False
            try:
                await save_task
            except Exception:
                _LOGGER.debug("Could not save room selections")
            else:
                saved = True
            if saved:
                dirty_maps = getattr(self, "_room_selection_dirty_maps", set())
                self._room_selection_dirty_maps = (
                    {None}
                    if None in dirty_maps and None in self.selected_clean_rooms
                    else set()
                )
            if cancelled:
                raise asyncio.CancelledError

    def set_room_clean_setting(
        self,
        room_id: int,
        attr: str,
        value,
        *,
        map_id: str | None = None,
    ) -> None:
        """Store one room-clean profile value."""
        if attr not in ROOM_CLEAN_SETTING_ATTRS:
            raise AttributeError(f"Unsupported room clean setting: {attr}")
        key = self._room_clean_settings_key(room_id, map_id)
        setattr(self.room_clean_settings_for(room_id, map_id=map_id), attr, value)
        if not hasattr(self, "room_clean_settings_customized"):
            self.room_clean_settings_customized = {}
        self.room_clean_settings_customized.setdefault(key, set()).add(attr)
        if key[0] is None:
            if not hasattr(self, "_room_profile_pending_resolution"):
                self._room_profile_pending_resolution = set()
            self._room_profile_pending_resolution.add(room_id)
        self._schedule_room_selection_save()

    def _map_display_cache_payload(
        self,
        state: NarwalState,
    ) -> dict[str, object] | None:
        """Return a serializable display-map trajectory cache payload."""
        snapshot = self._map_display_cache_snapshot(state)
        return (
            self._map_display_cache_payload_from_snapshot(snapshot)
            if snapshot is not None
            else None
        )

    def _map_display_cache_snapshot(
        self,
        state: NarwalState,
    ) -> _MapDisplayCacheSnapshot | None:
        """Return a lightweight display-map trajectory cache snapshot."""
        if state.working_status == WorkingStatus.REMAPPING:
            return None
        display = state.map_display_data
        if display is None or not display.has_trajectory:
            return None
        static_map = state.map_data
        confirmed_terminal = (
            is_confirmed_terminal_clean_state(state)
            and not self._stale_startup_dock_may_await_trajectory(state)
        )
        active_clean = not confirmed_terminal and (
            is_clean_session_context(state)
            or getattr(self, "_map_display_cache_restored_from_active", False)
        )
        return _MapDisplayCacheSnapshot(
            map_id=getattr(static_map, "map_id", 0) if static_map else 0,
            map_created_at=getattr(static_map, "created_at", 0) if static_map else 0,
            active_clean=active_clean,
            display=display,
        )

    @staticmethod
    def _map_display_cache_snapshot_is_scoped(
        snapshot: _MapDisplayCacheSnapshot,
    ) -> bool:
        """Return whether a trajectory snapshot identifies its static map."""
        return snapshot.map_id != 0 or snapshot.map_created_at != 0

    @staticmethod
    def _static_map_identity(state: NarwalState) -> tuple[int, int] | None:
        """Return the identity of the active static map, when known."""
        static_map = state.map_data
        if static_map is None:
            return None
        return (static_map.map_id, static_map.created_at)

    @staticmethod
    def _map_display_cache_payload_from_snapshot(
        snapshot: _MapDisplayCacheSnapshot,
    ) -> dict[str, object]:
        """Return a serializable display-map trajectory cache payload."""
        display = snapshot.display
        return {
            "map_id": snapshot.map_id,
            "map_created_at": snapshot.map_created_at,
            "active_clean": snapshot.active_clean,
            "robot_x": display.robot_x,
            "robot_y": display.robot_y,
            "robot_heading": display.robot_heading,
            "timestamp": display.timestamp,
            "dock_ref_x": display.dock_ref_x,
            "dock_ref_y": display.dock_ref_y,
            "trajectory_x_values": base64.b64encode(
                display.trajectory_x_values
            ).decode("ascii"),
            "trajectory_y_values": base64.b64encode(
                display.trajectory_y_values
            ).decode("ascii"),
            "trajectory_signature": list(display.trajectory_signature),
            "trajectory_breaks": list(display.trajectory_breaks),
        }

    @staticmethod
    def _optional_cache_int(value: object) -> int | None:
        """Return an integer cache value, treating blank/zero as absent."""
        if value in (None, "", 0, "0"):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _map_display_from_cache(
        payload: Mapping[str, object] | None,
    ) -> MapDisplayData | None:
        """Return cached display-map data, if the stored payload is valid."""
        if not payload:
            return None
        try:
            trajectory_x_values = base64.b64decode(
                str(payload["trajectory_x_values"])
            )
            trajectory_y_values = base64.b64decode(
                str(payload["trajectory_y_values"])
            )
            signature_raw = payload["trajectory_signature"]
            if not isinstance(signature_raw, list):
                return None
            signature = tuple(int(value) for value in signature_raw)
            if len(signature) != 3:
                return None
            breaks_raw = payload.get("trajectory_breaks", [])
            if not isinstance(breaks_raw, list):
                return None
            trajectory_breaks = tuple(int(value) for value in breaks_raw)
            display = MapDisplayData(
                # Cached trajectories are persisted so completed routes survive
                # restart, but robot pose must come from a live display_map packet.
                robot_x=0.0,
                robot_y=0.0,
                robot_heading=0.0,
                timestamp=int(payload.get("timestamp", 0)),
                dock_ref_x=float(payload.get("dock_ref_x", 0.0)),
                dock_ref_y=float(payload.get("dock_ref_y", 0.0)),
                trajectory_x_values=trajectory_x_values,
                trajectory_y_values=trajectory_y_values,
                trajectory_signature=signature,
                trajectory_breaks=trajectory_breaks,
            )
        except (KeyError, TypeError, ValueError):
            return None
        return display if display.has_trajectory else None

    async def _async_restore_map_display_cache(self) -> None:
        """Restore the last display-map trajectory for the active static map."""
        payload = await self._map_display_cache_store.async_load()
        if not isinstance(payload, Mapping):
            return
        if self.client.state.map_data is None:
            self._pending_map_display_cache_restore = dict(payload)
            return
        self._restore_map_display_cache_with_live_trajectory(payload)

    def _restore_pending_map_display_cache(self) -> None:
        """Restore a delayed display-map cache once a static map is available."""
        payload = self._pending_map_display_cache_restore
        if payload is None:
            return
        self._pending_map_display_cache_restore = None
        self._restore_map_display_cache_with_live_trajectory(payload)

    def _restore_map_display_cache_with_live_trajectory(
        self,
        payload: Mapping[str, object],
    ) -> bool:
        """Restore an active prefix before merging an early live window."""
        state = self.client.state
        current = state.map_display_data
        if current is None:
            return self._restore_map_display_cache_payload(payload)
        if not current.has_trajectory:
            restored = self._restore_map_display_cache_payload(payload)
            if not restored or state.map_display_data is None:
                return restored
            restored_display = replace(
                state.map_display_data,
                robot_x=current.robot_x,
                robot_y=current.robot_y,
                robot_heading=current.robot_heading,
                timestamp=current.timestamp,
            )
            state.map_display_data = restored_display
            self._retained_map_display = restored_display
            snapshot = self._map_display_cache_snapshot(state)
            if snapshot is not None:
                self._pending_map_display_cache_snapshot = snapshot
            return True
        if payload.get("active_clean") is not True:
            return False

        state.map_display_data = None
        restored = self._restore_map_display_cache_payload(payload)
        state.map_display_data = current
        if restored:
            self._retain_native_trajectory(state)
            snapshot = self._map_display_cache_snapshot(state)
            if snapshot is not None:
                self._pending_map_display_cache_snapshot = snapshot
        return restored

    def _scope_pending_map_display_cache_snapshot(self) -> None:
        """Bind a pre-map trajectory snapshot to the map fetched later."""
        snapshot = self._pending_map_display_cache_snapshot
        map_identity = self._static_map_identity(self.client.state)
        if (
            snapshot is None
            or map_identity is None
            or snapshot.map_id != 0
            or snapshot.map_created_at != 0
        ):
            return
        self._pending_map_display_cache_snapshot = replace(
            snapshot,
            map_id=map_identity[0],
            map_created_at=map_identity[1],
        )

    def _restore_map_display_cache_payload(
        self,
        payload: Mapping[str, object],
    ) -> bool:
        """Restore a display-map cache payload if it matches the active map."""
        display = self._map_display_from_cache(payload)
        if display is None:
            return False

        static_map = self.client.state.map_data
        if static_map is None:
            self._pending_map_display_cache_restore = dict(payload)
            return False
        cached_map_id = self._optional_cache_int(payload.get("map_id"))
        cached_created_at = self._optional_cache_int(payload.get("map_created_at"))
        if cached_map_id is None and cached_created_at is None:
            return False
        if cached_map_id is not None and cached_map_id != static_map.map_id:
            return False
        if (
            cached_created_at is not None
            and cached_created_at != static_map.created_at
        ):
            return False
        if self._has_current_map_display_trajectory():
            return False
        cached_active_clean = payload.get("active_clean") is True
        if is_active_clean_session(self.client.state) and not cached_active_clean:
            return False

        self.client.state.map_display_data = display
        self._retained_map_display = display
        self._retained_map_identity = self._static_map_identity(self.client.state)
        self._map_display_cache_signature = display.trajectory_signature
        self._map_display_cache_active_clean = cached_active_clean
        self._map_display_cache_restored = True
        self._map_display_cache_restored_from_active = cached_active_clean
        self._map_display_cache_restored_at = (
            time.monotonic() if cached_active_clean else 0.0
        )
        _LOGGER.debug(
            "Restored Narwal display-map trajectory cache with %d bytes",
            len(display.trajectory_x_values) + len(display.trajectory_y_values),
        )
        return True

    def _reset_map_display_cache_state(self, *, clear_memory: bool) -> None:
        """Reset in-memory display-map trail cache state."""
        self._pending_map_display_cache_snapshot = None
        self._pending_map_display_cache_restore = None
        self._map_display_cache_signature = ()
        self._map_display_cache_active_clean = None
        self._map_display_cache_restored = False
        self._map_display_cache_restored_from_active = False
        self._map_display_cache_restored_at = 0.0
        if clear_memory:
            self.client.state.map_display_data = None
            self._retained_map_display = None
            self._retained_map_identity = None
        else:
            self._retained_map_display = self.client.state.map_display_data
            self._retained_map_identity = self._static_map_identity(self.client.state)

    @staticmethod
    def _native_trajectory_overlap(
        previous: MapDisplayData,
        current: MapDisplayData,
    ) -> int:
        """Return the exact point overlap between two Narwal trajectory windows."""
        previous_count = min(
            len(previous.trajectory_x_values),
            len(previous.trajectory_y_values),
        ) // 4
        current_count = min(
            len(current.trajectory_x_values),
            len(current.trajectory_y_values),
        ) // 4
        if previous_count <= 0 or current_count <= 0:
            return 0

        previous_x = memoryview(previous.trajectory_x_values)[
            : previous_count * 4
        ].cast("I")
        previous_y = memoryview(previous.trajectory_y_values)[
            : previous_count * 4
        ].cast("I")
        current_x = memoryview(current.trajectory_x_values)[
            : current_count * 4
        ].cast("I")
        current_y = memoryview(current.trajectory_y_values)[
            : current_count * 4
        ].cast("I")

        # KMP keeps overlap discovery linear at the 50,000-point retention cap.
        prefix = [0] * current_count
        matched = 0
        for index in range(1, current_count):
            while matched and (
                current_x[index] != current_x[matched]
                or current_y[index] != current_y[matched]
            ):
                matched = prefix[matched - 1]
            if (
                current_x[index] == current_x[matched]
                and current_y[index] == current_y[matched]
            ):
                matched += 1
            prefix[index] = matched

        matched = 0
        for index in range(previous_count):
            while matched and (
                previous_x[index] != current_x[matched]
                or previous_y[index] != current_y[matched]
            ):
                matched = prefix[matched - 1]
            if (
                previous_x[index] == current_x[matched]
                and previous_y[index] == current_y[matched]
            ):
                matched += 1
            if matched == current_count and index != previous_count - 1:
                matched = prefix[matched - 1]
        return matched

    @staticmethod
    def _native_trajectory_find(
        haystack_x: bytes,
        haystack_y: bytes,
        needle_x: bytes,
        needle_y: bytes,
    ) -> int | None:
        """Return the last exact packed-point sequence offset, if present."""
        haystack_count = min(len(haystack_x), len(haystack_y)) // 4
        needle_count = min(len(needle_x), len(needle_y)) // 4
        if needle_count <= 0 or haystack_count < needle_count:
            return None

        haystack_x_view = memoryview(haystack_x)[: haystack_count * 4].cast("I")
        haystack_y_view = memoryview(haystack_y)[: haystack_count * 4].cast("I")
        needle_x_view = memoryview(needle_x)[: needle_count * 4].cast("I")
        needle_y_view = memoryview(needle_y)[: needle_count * 4].cast("I")

        prefix = [0] * needle_count
        matched = 0
        for index in range(1, needle_count):
            while matched and (
                needle_x_view[index] != needle_x_view[matched]
                or needle_y_view[index] != needle_y_view[matched]
            ):
                matched = prefix[matched - 1]
            if (
                needle_x_view[index] == needle_x_view[matched]
                and needle_y_view[index] == needle_y_view[matched]
            ):
                matched += 1
            prefix[index] = matched

        matched = 0
        last_match: int | None = None
        for index in range(haystack_count):
            while matched and (
                haystack_x_view[index] != needle_x_view[matched]
                or haystack_y_view[index] != needle_y_view[matched]
            ):
                matched = prefix[matched - 1]
            if (
                haystack_x_view[index] == needle_x_view[matched]
                and haystack_y_view[index] == needle_y_view[matched]
            ):
                matched += 1
            if matched == needle_count:
                last_match = index - needle_count + 1
                matched = prefix[matched - 1]
        return last_match

    @classmethod
    def _native_trajectory_contains(
        cls,
        haystack_x: bytes,
        haystack_y: bytes,
        needle_x: bytes,
        needle_y: bytes,
    ) -> bool:
        """Return whether packed trajectory points contain an exact sequence."""
        return cls._native_trajectory_find(
            haystack_x, haystack_y, needle_x, needle_y
        ) is not None

    @classmethod
    def _native_trajectory_compacted_tail_end(
        cls,
        previous: MapDisplayData,
        current: MapDisplayData,
    ) -> int:
        """Return the end offset of a retained compacted tail in a rolling window."""
        previous_count = min(
            len(previous.trajectory_x_values),
            len(previous.trajectory_y_values),
        ) // 4
        if previous_count != NATIVE_TRAJECTORY_MAX_POINTS:
            return 0
        tail_count = min(NATIVE_TRAJECTORY_RECENT_TAIL_POINTS, previous_count)
        tail_size = tail_count * 4
        start = cls._native_trajectory_find(
            current.trajectory_x_values,
            current.trajectory_y_values,
            previous.trajectory_x_values[-tail_size:],
            previous.trajectory_y_values[-tail_size:],
        )
        return 0 if start is None else start + tail_count

    @classmethod
    def _compact_native_trajectory(
        cls,
        x_values: bytes,
        y_values: bytes,
        breaks: tuple[int, ...],
        *,
        max_points: int | None = None,
        recent_tail_points: int | None = None,
    ) -> tuple[bytes, bytes, tuple[int, ...]]:
        """Bound retained route work while preserving its shape and recent tail."""
        if max_points is None:
            max_points = NATIVE_TRAJECTORY_MAX_POINTS
        if recent_tail_points is None:
            recent_tail_points = NATIVE_TRAJECTORY_RECENT_TAIL_POINTS
        point_count = min(len(x_values), len(y_values)) // 4
        if point_count <= max_points:
            return x_values, y_values, breaks

        tail_size = min(recent_tail_points, max_points)
        tail_start = point_count - tail_size
        prefix_slots = max_points - tail_size
        span = max(tail_start - 1, 0)
        slots = max(prefix_slots - 1, 1)
        selected = {
            round(slot * span / slots) for slot in range(prefix_slots)
        }
        selected.update(range(tail_start, point_count))
        selected_indices = sorted(selected)
        x_view = memoryview(x_values)
        y_view = memoryview(y_values)
        compact_x = b"".join(
            x_view[index * 4 : index * 4 + 4] for index in selected_indices
        )
        compact_y = b"".join(
            y_view[index * 4 : index * 4 + 4] for index in selected_indices
        )
        compact_breaks = {
            bisect_left(selected_indices, index) for index in breaks
        }
        return (
            compact_x,
            compact_y,
            tuple(
                sorted(
                    index
                    for index in compact_breaks
                    if 0 < index < len(selected_indices)
                )
            ),
        )

    @classmethod
    def _native_trajectory_replaces_compacted(
        cls,
        previous: MapDisplayData,
        current: MapDisplayData,
    ) -> bool:
        """Return whether a full native route contains a compacted route tail."""
        previous_count = min(
            len(previous.trajectory_x_values),
            len(previous.trajectory_y_values),
        ) // 4
        current_count = min(
            len(current.trajectory_x_values),
            len(current.trajectory_y_values),
        ) // 4
        if (
            previous_count != NATIVE_TRAJECTORY_MAX_POINTS
            or current_count < previous_count
        ):
            return False
        previous_x = previous.trajectory_x_values[: previous_count * 4]
        previous_y = previous.trajectory_y_values[: previous_count * 4]
        current_x = current.trajectory_x_values[: current_count * 4]
        current_y = current.trajectory_y_values[: current_count * 4]
        if current_x[:4] != previous_x[:4] or current_y[:4] != previous_y[:4]:
            return False
        tail_count = min(NATIVE_TRAJECTORY_RECENT_TAIL_POINTS, previous_count)
        tail_size = tail_count * 4
        return cls._native_trajectory_contains(
            current_x,
            current_y,
            previous_x[-tail_size:],
            previous_y[-tail_size:],
        )

    @classmethod
    def _merge_native_trajectory_windows(
        cls,
        previous: MapDisplayData,
        current: MapDisplayData,
    ) -> MapDisplayData:
        """Append a Narwal trajectory window to the route retained by HA."""
        previous_count = min(
            len(previous.trajectory_x_values),
            len(previous.trajectory_y_values),
        ) // 4
        current_count = min(
            len(current.trajectory_x_values),
            len(current.trajectory_y_values),
        ) // 4
        if previous_count <= 0:
            return current
        if current_count <= 0:
            return replace(
                current,
                trajectory_x_values=previous.trajectory_x_values,
                trajectory_y_values=previous.trajectory_y_values,
                trajectory_signature=previous.trajectory_signature,
                trajectory_breaks=previous.trajectory_breaks,
            )
        if (
            current.timestamp
            and previous.timestamp
            and current.timestamp < previous.timestamp
        ):
            return previous

        previous_x = previous.trajectory_x_values[: previous_count * 4]
        previous_y = previous.trajectory_y_values[: previous_count * 4]
        current_x = current.trajectory_x_values[: current_count * 4]
        current_y = current.trajectory_y_values[: current_count * 4]
        current_has_previous_prefix = (
            current_count >= previous_count
            and current_x.startswith(previous_x)
            and current_y.startswith(previous_y)
        )
        # Once HA compacts an accumulated route, its full byte prefix no
        # longer matches. The untouched recent tail still identifies the
        # next full Narwal window without re-appending the whole route.
        current_replaces_compacted = (
            not current_has_previous_prefix
            and cls._native_trajectory_replaces_compacted(previous, current)
        )
        if current_has_previous_prefix or current_replaces_compacted:
            # Exact prefix growth retains HA's discontinuities. A complete
            # Narwal window replacing compacted data is authoritative and can
            # resolve a gap that HA previously had to mark locally.
            replacement_breaks = current.trajectory_breaks
            if current_has_previous_prefix:
                replacement_breaks = tuple(
                    sorted(
                        set(previous.trajectory_breaks)
                        | set(current.trajectory_breaks)
                    )
                )
            bounded_x, bounded_y, bounded_breaks = cls._compact_native_trajectory(
                current_x,
                current_y,
                replacement_breaks,
            )
            if (
                bounded_x == current.trajectory_x_values
                and bounded_y == current.trajectory_y_values
                and bounded_breaks == current.trajectory_breaks
            ):
                return current
            break_bytes = b"".join(
                index.to_bytes(4, "little", signed=False)
                for index in bounded_breaks
            )
            return replace(
                current,
                trajectory_x_values=bounded_x,
                trajectory_y_values=bounded_y,
                trajectory_signature=(
                    len(bounded_x) // 4,
                    zlib.crc32(bounded_x) & 0xFFFFFFFF,
                    zlib.crc32(break_bytes, zlib.crc32(bounded_y)) & 0xFFFFFFFF,
                ),
                trajectory_breaks=bounded_breaks,
            )
        overlap = cls._native_trajectory_overlap(previous, current)
        compacted_tail_end = cls._native_trajectory_compacted_tail_end(
            previous, current
        )

        append_point = max(overlap, compacted_tail_end)
        current_break_start = 0
        if compacted_tail_end > overlap:
            current_break_start = compacted_tail_end - min(
                NATIVE_TRAJECTORY_RECENT_TAIL_POINTS,
                previous_count,
            )
        append_offset = append_point * 4
        merged_x = previous_x + current_x[append_offset:]
        merged_y = previous_y + current_y[append_offset:]
        current_offset = previous_count - append_point
        merged_count = len(merged_x) // 4
        merged_breaks = set(previous.trajectory_breaks)
        merged_breaks.update(
            current_offset + index
            for index in current.trajectory_breaks
            if index >= current_break_start
        )
        if append_point == 0:
            merged_breaks.add(previous_count)
        valid_breaks = tuple(
            sorted(index for index in merged_breaks if 0 < index < merged_count)
        )
        merged_x, merged_y, valid_breaks = cls._compact_native_trajectory(
            merged_x,
            merged_y,
            valid_breaks,
        )
        break_bytes = b"".join(
            index.to_bytes(4, "little", signed=False) for index in valid_breaks
        )
        return replace(
            current,
            trajectory_x_values=merged_x,
            trajectory_y_values=merged_y,
            trajectory_signature=(
                len(merged_x) // 4,
                zlib.crc32(merged_x) & 0xFFFFFFFF,
                zlib.crc32(break_bytes, zlib.crc32(merged_y)) & 0xFFFFFFFF,
            ),
            trajectory_breaks=valid_breaks,
        )

    def _retain_native_trajectory(self, state: NarwalState) -> None:
        """Accumulate Narwal's overlapping trajectory windows for this clean."""
        map_identity = self._static_map_identity(state)
        retained_map_identity = getattr(self, "_retained_map_identity", None)
        if retained_map_identity is None and map_identity is not None:
            self._retained_map_identity = map_identity
            retained_map_identity = map_identity
        if (
            retained_map_identity is not None
            and map_identity is not None
            and map_identity != retained_map_identity
        ):
            # A display-map packet cannot be attributed safely while the active
            # static map is changing. Drop it with the old route; the next
            # packet on the new map starts a fresh retained trajectory.
            self._reset_map_display_cache_state(clear_memory=True)
            self._retained_map_identity = map_identity
            self._schedule_map_display_cache_clear(None)
            return
        if state.working_status == WorkingStatus.REMAPPING:
            # display_map field 2 is also populated while Narwal rebuilds its
            # map. Keep that geometry out of the retained cleaning route while
            # still publishing the live pose carried by the current packet.
            current = state.map_display_data
            previous = getattr(self, "_retained_map_display", None)
            if current is not None and previous is not None:
                retained = replace(
                    previous,
                    robot_x=current.robot_x,
                    robot_y=current.robot_y,
                    robot_heading=current.robot_heading,
                    timestamp=current.timestamp,
                    dock_ref_x=current.dock_ref_x,
                    dock_ref_y=current.dock_ref_y,
                )
                state.map_display_data = retained
                self._retained_map_display = retained
            elif current is not None:
                state.map_display_data = replace(
                    current,
                    trajectory_x_values=b"",
                    trajectory_y_values=b"",
                    trajectory_signature=(),
                    trajectory_breaks=(),
                )
            else:
                state.map_display_data = previous
            return
        current = state.map_display_data
        if current is None:
            return
        previous = getattr(self, "_retained_map_display", None)
        if previous is current:
            return
        if previous is None:
            self._retained_map_display = current
            self._retained_map_identity = map_identity
            return
        restored_active = getattr(
            self,
            "_map_display_cache_restored_from_active",
            False,
        )
        if restored_active and current.has_trajectory:
            if (
                current.timestamp
                and previous.timestamp
                and current.timestamp < previous.timestamp
            ):
                state.map_display_data = previous
                return
            previous_count = min(
                len(previous.trajectory_x_values),
                len(previous.trajectory_y_values),
            ) // 4
            current_count = min(
                len(current.trajectory_x_values),
                len(current.trajectory_y_values),
            ) // 4
            required_overlap = min(
                NATIVE_TRAJECTORY_RESTORE_MIN_OVERLAP_POINTS,
                previous_count,
            )
            if current_count < required_overlap:
                pending = replace(
                    current,
                    trajectory_x_values=previous.trajectory_x_values,
                    trajectory_y_values=previous.trajectory_y_values,
                    trajectory_signature=previous.trajectory_signature,
                    trajectory_breaks=previous.trajectory_breaks,
                )
                state.map_display_data = pending
                # A later non-map callback can revisit the published state.
                # Keep object identity aligned so the restored route cannot
                # validate itself before another native window arrives.
                self._retained_map_display = pending
                return
            if (
                max(
                    self._native_trajectory_overlap(previous, current),
                    self._native_trajectory_compacted_tail_end(previous, current),
                )
                < required_overlap
                and not self._native_trajectory_replaces_compacted(previous, current)
            ):
                # Narwal exposes no cleaning-session id. Refuse to join a cached
                # route unless a short tail sequence appears in the first live
                # rolling window; one point can coincide across different cleans.
                self._retained_map_display = current
                self._map_display_cache_signature = ()
                self._map_display_cache_active_clean = None
                self._map_display_cache_restored = False
                self._map_display_cache_restored_from_active = False
                self._map_display_cache_restored_at = 0.0
                return
        merged = self._merge_native_trajectory_windows(previous, current)
        state.map_display_data = merged
        self._retained_map_display = merged
        if (
            restored_active
            and current.has_trajectory
            and is_active_clean_session(state)
        ):
            self._map_display_cache_restored = False
            self._map_display_cache_restored_from_active = False
            self._map_display_cache_restored_at = 0.0

    def _has_current_map_display_trajectory(self) -> bool:
        """Return true when current state already has native trajectory data."""
        display = self.client.state.map_display_data
        return display is not None and display.has_trajectory

    @staticmethod
    def _map_display_signature_from_payload(
        payload: Mapping[str, object],
    ) -> tuple[int, int, int] | tuple[()]:
        """Return the trajectory signature stored in a cache payload."""
        signature_raw = payload.get("trajectory_signature")
        return (
            tuple(int(value) for value in signature_raw)
            if isinstance(signature_raw, list)
            else ()
        )

    def _schedule_map_display_cache_save(
        self, state: NarwalState, *, immediate: bool = False
    ) -> None:
        """Schedule a throttled save of the latest display-map trajectory."""
        snapshot = self._map_display_cache_snapshot(state)
        if snapshot is None:
            return
        if (
            snapshot.trajectory_signature == self._map_display_cache_signature
            and snapshot.active_clean
            == getattr(self, "_map_display_cache_active_clean", None)
        ):
            return
        active_clean_changed = snapshot.active_clean != getattr(
            self, "_map_display_cache_active_clean", None
        )
        if active_clean_changed:
            immediate = True
        self._pending_map_display_cache_snapshot = snapshot
        if not self._map_display_cache_snapshot_is_scoped(snapshot):
            return
        if (
            self._map_display_cache_save_task is not None
            and not self._map_display_cache_save_task.done()
        ):
            if not immediate:
                return
            self._map_display_cache_save_task.cancel()
            self._map_display_cache_save_task = None
        delay = (
            0.0
            if immediate
            else max(
                0.0,
                MAP_DISPLAY_CACHE_SAVE_INTERVAL
                - (time.monotonic() - self._map_display_cache_last_save),
            )
        )
        self._map_display_cache_save_task = self.config_entry.async_create_background_task(
            self.hass,
            self._async_save_pending_map_display_cache(delay),
            f"{DOMAIN}_map_display_cache_save",
        )

    def _cancel_map_display_cache_save_task(self) -> asyncio.Task[None] | None:
        """Cancel any queued trajectory-cache write and return the task."""
        task = self._map_display_cache_save_task
        if task is None or task.done():
            return None
        task.cancel()
        return task

    async def _async_cancel_map_display_cache_save(self) -> None:
        """Cancel and await any queued trajectory-cache write."""
        task = self._cancel_map_display_cache_save_task()
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._map_display_cache_save_task is task:
            self._map_display_cache_save_task = None

    async def _async_save_pending_map_display_cache(self, delay: float) -> None:
        """Persist the newest queued display-map trajectory cache payload."""
        clear_event = getattr(self, "_map_display_cache_clear_event", None)
        if clear_event is not None:
            await clear_event.wait()
        if delay > 0:
            await asyncio.sleep(delay)
        while self._pending_map_display_cache_snapshot is not None:
            snapshot = self._pending_map_display_cache_snapshot
            self._pending_map_display_cache_snapshot = None
            if not self._map_display_cache_snapshot_is_scoped(snapshot):
                self._pending_map_display_cache_snapshot = snapshot
                return
            payload = self._map_display_cache_payload_from_snapshot(snapshot)
            try:
                await self._async_write_map_display_cache(payload)
            except Exception:
                _LOGGER.debug("Could not save display-map trajectory cache")
                return
            self._map_display_cache_signature = snapshot.trajectory_signature
            self._map_display_cache_active_clean = snapshot.active_clean
            self._map_display_cache_last_save = time.monotonic()
            self._map_display_cache_clear_pending = False
            if self._pending_map_display_cache_snapshot is not None:
                await asyncio.sleep(MAP_DISPLAY_CACHE_SAVE_INTERVAL)

    async def _async_flush_map_display_cache(self) -> None:
        """Persist the current display-map trajectory before shutdown."""
        clear_event = getattr(self, "_map_display_cache_clear_event", None)
        if clear_event is not None:
            await clear_event.wait()
        await self._async_cancel_map_display_cache_save()
        self._scope_pending_map_display_cache_snapshot()

        snapshot = (
            self._pending_map_display_cache_snapshot
            or self._map_display_cache_snapshot(self.client.state)
        )
        if snapshot is not None and not self._map_display_cache_snapshot_is_scoped(
            snapshot
        ):
            if not getattr(self, "_map_display_cache_clear_pending", False):
                return
            try:
                await self._async_write_map_display_cache({})
            except Exception:
                _LOGGER.debug("Could not flush display-map trajectory cache clear")
                return
            self._map_display_cache_signature = ()
            self._map_display_cache_active_clean = False
            self._map_display_cache_last_save = time.monotonic()
            self._map_display_cache_clear_pending = False
            return
        self._pending_map_display_cache_snapshot = None
        payload = (
            self._map_display_cache_payload_from_snapshot(snapshot)
            if snapshot is not None
            else (
                {}
                if getattr(self, "_map_display_cache_clear_pending", False)
                else None
            )
        )
        if payload is None:
            return

        try:
            await self._async_write_map_display_cache(payload)
        except Exception:
            _LOGGER.debug("Could not flush display-map trajectory cache")
            return
        self._map_display_cache_signature = self._map_display_signature_from_payload(
            payload
        )
        self._map_display_cache_active_clean = payload.get("active_clean") is True
        self._map_display_cache_last_save = time.monotonic()
        self._map_display_cache_clear_pending = False

    def _map_display_cache_clear_gate(self) -> asyncio.Event:
        """Return the gate that serializes new route writes after cache clears."""
        event = getattr(self, "_map_display_cache_clear_event", None)
        if event is None:
            event = asyncio.Event()
            event.set()
            self._map_display_cache_clear_event = event
        return event

    def _map_display_cache_clear_mutex(self) -> asyncio.Lock:
        """Return the lock that orders overlapping trajectory-cache clears."""
        lock = getattr(self, "_map_display_cache_clear_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._map_display_cache_clear_lock = lock
        return lock

    def _map_display_cache_write_mutex(self) -> asyncio.Lock:
        """Return the lock that serializes every trajectory-cache write."""
        lock = getattr(self, "_map_display_cache_write_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._map_display_cache_write_lock = lock
        return lock

    async def _async_write_map_display_cache(self, payload: object) -> None:
        """Finish an in-flight storage write before honoring cancellation."""
        async with self._map_display_cache_write_mutex():
            write_task = asyncio.create_task(
                self._map_display_cache_store.async_save(payload)
            )
            try:
                await asyncio.shield(write_task)
            except asyncio.CancelledError:
                while not write_task.done():
                    try:
                        await asyncio.shield(write_task)
                    except asyncio.CancelledError:
                        continue
                with contextlib.suppress(Exception):
                    write_task.result()
                raise

    def _begin_map_display_cache_clear(self) -> None:
        """Keep route writes gated until every scheduled clear has finished."""
        self._map_display_cache_clear_count = (
            getattr(self, "_map_display_cache_clear_count", 0) + 1
        )
        self._map_display_cache_clear_gate().clear()

    def _finish_map_display_cache_clear(self) -> None:
        """Release route writes after the last overlapping clear."""
        remaining = max(
            0, getattr(self, "_map_display_cache_clear_count", 1) - 1
        )
        self._map_display_cache_clear_count = remaining
        if remaining:
            return
        self._map_display_cache_clear_gate().set()
        if self._pending_map_display_cache_snapshot is not None:
            self._schedule_map_display_cache_save(self.client.state, immediate=True)

    async def async_clear_map_display_cache(self) -> None:
        """Clear cached display-map trajectory after accepting a new clean."""
        self._begin_map_display_cache_clear()
        self._map_display_cache_clear_pending = True
        cancelled_save = self._cancel_map_display_cache_save_task()
        self._reset_map_display_cache_state(clear_memory=True)
        try:
            async with self._map_display_cache_clear_mutex():
                if cancelled_save is not None:
                    with contextlib.suppress(asyncio.CancelledError):
                        await cancelled_save
                    if self._map_display_cache_save_task is cancelled_save:
                        self._map_display_cache_save_task = None
                try:
                    await self._async_write_map_display_cache({})
                except Exception:
                    _LOGGER.debug("Could not clear display-map trajectory cache")
                else:
                    self._map_display_cache_clear_pending = False
        finally:
            self._finish_map_display_cache_clear()

    def _schedule_map_display_cache_clear(
        self,
        snapshot: _MapDisplayCacheSnapshot | None,
    ) -> None:
        """Clear or replace persisted trail cache from a synchronous callback."""
        self._begin_map_display_cache_clear()
        self._map_display_cache_clear_pending = True
        cancelled_save = self._cancel_map_display_cache_save_task()
        self._pending_map_display_cache_snapshot = None
        self.config_entry.async_create_background_task(
            self.hass,
            self._async_clear_or_replace_map_display_cache(snapshot, cancelled_save),
            f"{DOMAIN}_map_display_cache_clear",
        )

    async def _async_clear_or_replace_map_display_cache(
        self,
        snapshot: _MapDisplayCacheSnapshot | None,
        cancelled_save: asyncio.Task[None] | None,
    ) -> None:
        """Persist the new-clean cache decision after cancelling stale writes."""
        try:
            async with self._map_display_cache_clear_mutex():
                if cancelled_save is not None:
                    with contextlib.suppress(asyncio.CancelledError):
                        await cancelled_save
                    if self._map_display_cache_save_task is cancelled_save:
                        self._map_display_cache_save_task = None
                if snapshot is None:
                    try:
                        await self._async_write_map_display_cache({})
                    except Exception:
                        _LOGGER.debug("Could not clear display-map trajectory cache")
                    else:
                        self._map_display_cache_clear_pending = False
                else:
                    payload = self._map_display_cache_payload_from_snapshot(snapshot)
                    try:
                        await self._async_write_map_display_cache(payload)
                    except Exception:
                        _LOGGER.debug("Could not replace display-map trajectory cache")
                    else:
                        self._map_display_cache_signature = snapshot.trajectory_signature
                        self._map_display_cache_active_clean = snapshot.active_clean
                        self._map_display_cache_last_save = time.monotonic()
                        self._map_display_cache_clear_pending = False
        finally:
            self._finish_map_display_cache_clear()

    @staticmethod
    def _has_clean_session_signal(state: NarwalState) -> bool:
        """Return true when current telemetry describes an active clean session."""
        if (
            state.working_status == WorkingStatus.REMAPPING
            or is_confirmed_terminal_clean_state(state)
        ):
            return False
        return (
            state.working_status in ACTIVE_CLEANING_STATUSES
            or state.has_assumed_robot_clean
            or state.has_paused_clean_task_context
            or (
                state.has_recent_active_working_status
                and not _state_attr_is_true(state, "is_returning")
            )
        )

    def _reconcile_map_display_after_status_refresh(self) -> None:
        """Apply refreshed task state to trajectory retention and persistence."""
        state = self.client.state
        self._handle_working_status_transition(state)
        self._retain_native_trajectory(state)
        self._schedule_map_display_cache_save(state)

    def _is_new_clean_transition(self, state: NarwalState) -> bool:
        """Return true when state has entered a new robot cleaning session."""
        if not self._has_clean_session_signal(state):
            return False
        if getattr(self, "_clean_session_active", False):
            return False
        pending_restore = getattr(self, "_pending_map_display_cache_restore", None)
        if isinstance(pending_restore, Mapping) and pending_restore.get(
            "active_clean"
        ) is True:
            return False
        return not (
            self._map_display_cache_restored
            and self._map_display_cache_restored_from_active
        )

    def _active_map_display_cache_awaits_validation(self) -> bool:
        """Return true while a restored active route awaits its first live window."""
        restored_at = getattr(self, "_map_display_cache_restored_at", 0.0)
        return bool(
            getattr(self, "_map_display_cache_restored_from_active", False)
            and restored_at > 0
            and time.monotonic() - restored_at <= NATIVE_TRAJECTORY_RESTORE_GRACE
        )

    def _stale_startup_dock_may_await_trajectory(self, state: NarwalState) -> bool:
        """Return true only for ambiguous idle-dock state during restore grace."""
        return (
            self._active_map_display_cache_awaits_validation()
            and state.working_status
            in {
                WorkingStatus.STANDBY,
                WorkingStatus.DOCKED,
                WorkingStatus.CHARGED,
                WorkingStatus.DOCKED_V2,
            }
            and state.is_docked
            and not has_blocking_error(state)
        )

    def _clear_map_display_cache_for_new_clean(self) -> None:
        """Clear stale trail data when a clean starts outside HA."""
        self._reset_map_display_cache_state(clear_memory=True)
        self._schedule_map_display_cache_clear(None)
        _LOGGER.debug("Cleared Narwal display-map trajectory cache for new clean")

    def _handle_working_status_transition(self, state: NarwalState) -> None:
        """Apply transition side effects and record the latest working status."""
        if (
            is_confirmed_terminal_clean_state(state)
            and not self._stale_startup_dock_may_await_trajectory(state)
        ):
            self._map_display_cache_restored_from_active = False
            self._map_display_cache_restored_at = 0.0
            pending_restore = getattr(
                self, "_pending_map_display_cache_restore", None
            )
            if isinstance(pending_restore, Mapping) and pending_restore.get(
                "active_clean"
            ) is True:
                self._pending_map_display_cache_restore = {
                    **pending_restore,
                    "active_clean": False,
                }
        if self._is_new_clean_transition(state):
            self._clear_map_display_cache_for_new_clean()
        self._clean_session_active = self._has_clean_session_signal(state)
        self._prev_working_status = state.working_status

    async def async_setup(self) -> None:
        """Connect to the vacuum and start the WebSocket listener.

        Queries initial state BEFORE starting the listener to avoid
        concurrent recv issues (see 446be16). Each command is wrapped in
        try/except so setup never crashes if the robot is asleep.
        The listener's keepalive loop handles waking independently.
        """
        await self._async_restore_room_selections()
        await self.client.connect()

        # Fetch initial state BEFORE starting listener (no concurrent recv)
        try:
            await self.client.get_device_info()
        except Exception:
            _LOGGER.debug("Could not fetch device info at startup")

        try:
            response = await self.client.get_status(
                full_update=not self.client.state.has_recent_active_working_status
            )
            if not getattr(response, "accepted", True):
                raise NarwalConnectionError(
                    f"Status refresh failed with code {response.result_code}"
                )
            if not _has_dock_status_payload(response):
                raise NarwalConnectionError(
                    "Status refresh returned no dock-status payload"
                )
            self._mark_dock_status_refresh_succeeded()
        except Exception:
            self._mark_dock_status_refresh_failed()
            _LOGGER.debug("Could not fetch initial status")

        try:
            await self.client.get_map()
        except Exception:
            _LOGGER.debug("Could not fetch initial map")

        try:
            await self._async_restore_map_display_cache()
        except Exception:
            _LOGGER.debug("Could not restore display-map trajectory cache")
        if (
            self._has_clean_session_signal(self.client.state)
            and not getattr(self, "_map_display_cache_restored", False)
            and getattr(self, "_pending_map_display_cache_restore", None) is None
        ):
            # Initial map fetches can already have delivered this clean's first
            # native window. Treat it as the current session, not stale history.
            self._clean_session_active = True
        self._handle_working_status_transition(self.client.state)
        self._retain_native_trajectory(self.client.state)

        try:
            await self.client.get_consumable_info()
        except Exception:
            _LOGGER.debug("Could not fetch initial consumable info")

        # Subscribe to broadcast topics (display_map, working_status, etc.)
        # Must be sent before listener starts so display_map flows during cleaning.
        if self.client.supports_broadcasts:
            try:
                await self.client.subscribe_to_topics()
                self._last_topic_subscribe = time.monotonic()
            except Exception:
                _LOGGER.debug("Could not send topic subscription at startup")

        self._retain_native_trajectory(self.client.state)
        self._schedule_map_display_cache_save(self.client.state)
        self.async_set_updated_data(self.client.state)
        self._prev_working_status = self.client.state.working_status
        self._clean_session_active = self._has_clean_session_signal(self.client.state)

        # Set up push callback and start persistent listener
        self.client.on_state_update = self._on_state_update
        self._listen_task = self.config_entry.async_create_background_task(
            self.hass,
            self.client.start_listening(),
            f"{DOMAIN}_ws_listener",
        )

        state = self.client.state
        _LOGGER.info(
            "Narwal startup: status=%s, battery=%d, docked=%s, awake=%s",
            state.working_status.name, state.battery_level,
            state.is_docked, self.client.robot_awake,
        )

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
        # Push data arriving means robot is reachable — reset failure counter
        self._consecutive_failures = 0

        # Fetch static map if missing (get_map failed at startup)
        if state.map_data is None and not self._map_fetch_pending:
            self._map_fetch_pending = True
            self.config_entry.async_create_background_task(
                self.hass,
                self._fetch_missing_map(),
                f"{DOMAIN}_map_fetch",
            )
        elif state.map_data is not None:
            self._restore_pending_map_display_cache()

        # Detect return-to-dock transition: CLEANING/CLEANING_ALT → docked state.
        # Broadcast dock fields are stale after docking — immediate poll
        # refreshes them so UI shows DOCKED instead of IDLE.
        # On older FW the transition is → STANDBY; on v01.07.23+ it may
        # go directly to DOCKED_V2(2).
        if (
            state.working_status in (
                WorkingStatus.STANDBY, WorkingStatus.DOCKED_V2,
            )
            and self._prev_working_status
            in ACTIVE_CLEANING_STATUSES
        ):
            _LOGGER.info("Return-to-dock detected, refreshing dock status")
            self.hass.async_create_task(self._refresh_dock_status())
        self._handle_working_status_transition(state)
        self._retain_native_trajectory(state)
        self._schedule_map_display_cache_save(state)

        # display_map dropout recovery: if cleaning but no display_map for
        # 30s, re-send topic subscription. Only subscription — no wake burst
        # (wake bursts during cleaning cause pause bouncing).
        is_cleaning = (
            state.is_cleaning
            or state.has_recent_active_working_status
            or (
                not state.is_docked
                and state.working_status in ACTIVE_CLEANING_STATUSES
            )
        )
        if is_cleaning:
            display_age = self.client.last_display_map_age
            now = time.monotonic()
            if (
                display_age > 30.0
                and now - self._last_display_map_resub > 45.0
            ):
                _LOGGER.info(
                    "display_map dropout (%.0fs) — re-subscribing to topics",
                    display_age,
                )
                self._last_display_map_resub = now
                self.config_entry.async_create_background_task(
                    self.hass,
                    self._resub_topics(),
                    f"{DOMAIN}_resub",
                )

        self._sync_active_clean_context(state)
        self.async_set_updated_data(state)

        # Broadcast arrived — switch back to normal polling if in fast mode
        if self._fast_poll_remaining > 0:
            self._fast_poll_remaining = 0
            self.update_interval = POLL_INTERVAL
            _LOGGER.info(
                "Broadcast received (status=%s) — normal polling restored",
                state.working_status.name,
            )

    async def _fetch_missing_map(self) -> None:
        """Fetch static map when it's missing (get_map failed at startup)."""
        try:
            await self.client.get_map()
            _LOGGER.info("Static map loaded (was missing at startup)")
        except Exception:
            _LOGGER.debug("Map fetch failed — will retry on next broadcast")
            self._map_fetch_pending = False
            return
        self._scope_pending_map_display_cache_snapshot()
        self._restore_pending_map_display_cache()
        if self.client.supports_broadcasts:
            try:
                await self.client.subscribe_to_topics()
                self._last_topic_subscribe = time.monotonic()
            except Exception:
                _LOGGER.debug("Topic subscription failed after map load")
        self.async_set_updated_data(self.client.state)

    async def _resub_topics(self) -> None:
        """Re-send topic subscription to recover display_map during cleaning."""
        if not self.client.supports_broadcasts:
            return
        try:
            await self.client.subscribe_to_topics()
            self._last_topic_subscribe = time.monotonic()
        except Exception:
            _LOGGER.debug("Topic re-subscription failed")

    async def _refresh_dock_status(self) -> None:
        """Immediate get_status() after return-to-dock to refresh dock fields."""
        try:
            response = await self.client.get_status(
                full_update=not self.client.state.has_recent_active_working_status
            )
            if not getattr(response, "accepted", True):
                raise NarwalConnectionError(
                    f"Status refresh failed with code {response.result_code}"
                )
            if not _has_dock_status_payload(response):
                raise NarwalConnectionError(
                    "Status refresh returned no dock-status payload"
                )
            self._mark_dock_status_refresh_succeeded()
            self._reconcile_map_display_after_status_refresh()
            self._sync_active_clean_context(self.client.state)
            self.async_set_updated_data(self.client.state)
        except Exception:
            self._mark_dock_status_refresh_failed()
            _LOGGER.debug("Failed to refresh dock status after transition")
            self._sync_active_clean_context(self.client.state)
            self.async_set_updated_data(self.client.state)

    async def async_refresh_dock_status(self) -> bool:
        """Refresh full dock/base-station status for action gating."""
        full_update = not self.client.state.has_recent_active_working_status
        try:
            response = await self.client.get_status(full_update=full_update)
        except Exception:
            self._mark_dock_status_refresh_failed()
            _LOGGER.debug("Failed to refresh dock status")
            self._sync_active_clean_context(self.client.state)
            self.async_set_updated_data(self.client.state)
            return False
        if not response.accepted:
            self._mark_dock_status_refresh_failed()
            _LOGGER.debug(
                "Dock status refresh was rejected with code %s",
                response.result_code,
            )
            self._sync_active_clean_context(self.client.state)
            self.async_set_updated_data(self.client.state)
            return False
        if full_update and not _has_dock_status_payload(response):
            self._mark_dock_status_refresh_failed()
            _LOGGER.debug("Dock status refresh returned no dock-status payload")
            self._sync_active_clean_context(self.client.state)
            self.async_set_updated_data(self.client.state)
            return False
        if full_update:
            self._mark_dock_status_refresh_succeeded()
        self._reconcile_map_display_after_status_refresh()
        self._sync_active_clean_context(self.client.state)
        self.async_set_updated_data(self.client.state)
        return True

    async def async_prepare_clean_start(self, *, allow_dock_stop: bool = True) -> bool:
        """Stop safe dock-side blockers before starting a robot clean."""
        if not await self.async_refresh_dock_status():
            return False
        state = self.client.state
        active_tasks = state.active_dock_task_keys
        if _can_start_cleaning_without_dock_stop(state):
            return True
        if not allow_dock_stop:
            return False
        if (
            has_blocking_error(state)
            or not state.is_docked
            or is_clean_session_context(state)
            or state.has_unmapped_active_dock_task
            or state.assumed_active_dock_task is not None
            or len(active_tasks) != 1
        ):
            return False

        blocker = active_tasks[0]
        if blocker not in ROBOT_START_STOP_REQUIRED_DOCK_TASKS or not can_stop_dock_task(
            state, blocker
        ):
            return False
        response = await self.client.stop_dock_task(blocker)
        if not response.accepted:
            return False
        self._sync_active_clean_context(self.client.state)
        self.async_set_updated_data(self.client.state)

        if not await self.async_refresh_dock_status():
            return False
        return _can_start_cleaning_without_dock_stop(self.client.state)

    async def async_refresh_action_status(self) -> bool:
        """Refresh state for a robot action without clobbering live task telemetry."""
        full_update = not self.client.state.has_recent_active_working_status
        try:
            response = await self.client.get_status(full_update=full_update)
        except Exception:
            _LOGGER.debug("Failed to refresh Narwal action status")
            if full_update:
                self._mark_dock_status_refresh_failed()
            self._sync_active_clean_context(self.client.state)
            self.async_set_updated_data(self.client.state)
            return False
        if not response.accepted:
            _LOGGER.debug(
                "Narwal action status refresh was rejected with code %s",
                response.result_code,
            )
            if full_update:
                self._mark_dock_status_refresh_failed()
            self._sync_active_clean_context(self.client.state)
            self.async_set_updated_data(self.client.state)
            return False
        if full_update and not _has_dock_status_payload(response):
            _LOGGER.debug("Narwal action status refresh returned no dock-status payload")
            self._mark_dock_status_refresh_failed()
            self._sync_active_clean_context(self.client.state)
            self.async_set_updated_data(self.client.state)
            return False
        if full_update:
            self._mark_dock_status_refresh_succeeded()
        self._reconcile_map_display_after_status_refresh()
        self._sync_active_clean_context(self.client.state)
        self.async_set_updated_data(self.client.state)
        return True

    async def _async_update_data(self) -> NarwalState:
        """Polling fallback — fetch status if no push updates arrived.

        Reconnection is handled by the listener loop's exponential backoff.
        We do NOT call client.connect() here to avoid racing with the listener
        and violating the single-WS-connection-per-IP constraint.

        On poll failure, returns stale data for up to _max_failures consecutive
        failures (~5 minutes) before raising UpdateFailed.
        """
        if (
            not getattr(self, "_room_selection_store_loaded", True)
            or not getattr(self, "_room_profile_store_loaded", True)
        ):
            async with self._room_selection_save_lock:
                if (
                    not self._room_selection_store_loaded
                    or not self._room_profile_store_loaded
                ):
                    await self._async_restore_room_selections()

        try:
            if not self.client.connected:
                raise NarwalConnectionError("Not connected")
            full_update = not self.client.state.has_recent_active_working_status
            response = await self.client.get_status(full_update=full_update)
            if not getattr(response, "accepted", True):
                raise NarwalConnectionError(
                    f"Status refresh failed with code {response.result_code}"
                )
            if full_update and not _has_dock_status_payload(response):
                # An accepted partial response still proves the connection is
                # healthy. Keep dock actions stale without taking unrelated
                # entities unavailable after repeated battery-only polls.
                self._mark_dock_status_refresh_failed()
        except Exception as err:
            self._consecutive_failures += 1
            self._mark_dock_status_refresh_failed()
            if self._consecutive_failures >= self._max_failures:
                raise UpdateFailed(
                    f"Vacuum unreachable for {self._consecutive_failures} consecutive polls"
                ) from err
            _LOGGER.debug(
                "Poll %d/%d failed (robot may be asleep): %s",
                self._consecutive_failures, self._max_failures, err,
            )
            return self.client.state  # stale data keeps entities available
        else:
            self._consecutive_failures = 0
            if full_update and _has_dock_status_payload(response):
                self._mark_dock_status_refresh_succeeded()

        # Retry map fetch if it failed during setup
        if self.client.state.map_data is None:
            with contextlib.suppress(Exception):
                await self.client.get_map()
        if self.client.state.map_data is not None:
            self._scope_pending_map_display_cache_snapshot()
            self._restore_pending_map_display_cache()

        self._reconcile_map_display_after_status_refresh()

        # Renew the broadcast subscription before it lapses. This is deliberately
        # NOT conditional on believing we are cleaning: working_status is what tells
        # us we are cleaning, so gating renewal on that state deadlocks — the
        # subscription expires, the robot goes quiet, the entity stays "docked", and
        # nothing ever re-subscribes (#73).
        if (
            self.client.supports_broadcasts
            and time.monotonic() - self._last_topic_subscribe > TOPIC_RESUBSCRIBE_AFTER
        ):
            try:
                await self.client.subscribe_to_topics()
                self._last_topic_subscribe = time.monotonic()
                _LOGGER.debug("Renewed topic subscription")
            except Exception:
                _LOGGER.debug("Topic subscription renewal failed")

        # Refresh consumable alerts periodically (slow-changing; not broadcast)
        if self._consumable_poll_countdown <= 0:
            self._consumable_poll_countdown = CONSUMABLE_POLL_EVERY
            try:
                await self.client.get_consumable_info()
            except Exception:
                _LOGGER.debug("Consumable info poll failed")
        else:
            self._consumable_poll_countdown -= 1

        # Manage fast poll countdown
        if self._fast_poll_remaining > 0:
            if self.client.state.working_status != WorkingStatus.UNKNOWN:
                self._fast_poll_remaining = 0
                self.update_interval = POLL_INTERVAL
            else:
                self._fast_poll_remaining -= 1
                if self._fast_poll_remaining <= 0:
                    self.update_interval = POLL_INTERVAL

        self._sync_active_clean_context(self.client.state)
        return self.client.state

    async def async_shutdown(self) -> None:
        """Disconnect from the vacuum."""
        await self.client.disconnect()
        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._listen_task
        await self._async_flush_map_display_cache()
        await self._async_save_room_selections()
        await super().async_shutdown()
