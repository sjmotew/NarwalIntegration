"""Tests for NarwalCoordinator resilience -- failure buffering and push reset.

Verifies the coordinator returns stale data on transient failures, raises
UpdateFailed after the threshold, and resets counters on success/push.
"""

from __future__ import annotations

import asyncio
import math
import struct
import sys
import time
from collections.abc import Coroutine
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, PropertyMock, call, patch

import pytest

# Install HA stubs before any custom_components import
import tests.ha_stubs  # noqa: E402

tests.ha_stubs.install()

from custom_components.narwal.const import NO_BROADCAST_PRODUCT_KEYS  # noqa: E402
from custom_components.narwal.coordinator import (  # noqa: E402
    TOPIC_RESUBSCRIBE_AFTER,
    TOPIC_SUBSCRIPTION_TTL,
    CleanSettings,
    NarwalCoordinator,
    can_edit_pending_clean_settings,
    can_pause_cleaning,
    can_prepare_clean_start,
    can_start_cleaning,
    is_clean_session_context,
    is_confirmed_terminal_clean_state,
    is_live_clean_setting_available,
    is_narwal_task_busy,
)  # noqa: E402
from custom_components.narwal.narwal_client import (  # noqa: E402
    CommandResponse,
    CommandResult,
    FanLevel,
    MapData,
    MapDisplayData,
    MopHumidity,
    NarwalConnectionError,
    NarwalState,
    RoomCleanSettings,
    WorkingStatus,
    WorkMode,
)
from custom_components.narwal.narwal_client.models import (  # noqa: E402
    DOCK_TASK_DRY_DOCK_BAG,
    DOCK_TASK_DRY_DUST_BIN,
    DOCK_TASK_DRY_MOP,
    DOCK_TASK_EMPTY_DUSTBIN,
    DOCK_TASK_WASH_MOP,
)

UpdateFailed = sys.modules["homeassistant.helpers.update_coordinator"].UpdateFailed


def test_pause_available_with_stale_unconfirmed_return_flag() -> None:
    """A stale field 3.7 alone must not hide Pause during an active clean."""
    state = NarwalState()
    state.update_from_base_status({"3": {"1": 4, "7": 1}})
    state.last_active_working_status_time = 0.0

    assert can_pause_cleaning(state)


class _RoomSelectionStore:
    """Minimal storage double for room-selection persistence tests."""

    def __init__(self) -> None:
        self.data: object | None = None

    async def async_load(self) -> object | None:
        """Return stored data."""
        return self.data

    async def async_save(self, data: object) -> None:
        """Store data."""
        self.data = data


def _docked_state() -> NarwalState:
    """Return an idle on-dock state."""
    state = NarwalState(working_status=WorkingStatus.DOCKED)
    state.dock_presence = 6
    state.dock_field11 = 2
    return state


class _FakeStore:
    """Store test double that records saved payloads."""

    def __init__(self, data: object | None = None) -> None:
        self.data = data
        self.saved: list[object] = []

    async def async_load(self) -> object | None:
        return self.data

    async def async_save(self, data: object) -> None:
        self.saved.append(data)
        self.data = data


def _trajectory_state() -> NarwalState:
    """Return a state with static map and native display-map trajectory."""
    state = NarwalState()
    state.map_data = MapData(
        map_id=12,
        width=100,
        height=100,
        created_at=34,
        compressed_map=b"\x01",
    )
    state.map_display_data = MapDisplayData(
        robot_x=1.25,
        robot_y=2.5,
        robot_heading=90.0,
        timestamp=123456,
        dock_ref_x=3.0,
        dock_ref_y=4.0,
        trajectory_x_values=b"xxxx",
        trajectory_y_values=b"yyyy",
        trajectory_signature=(4, 4, 99),
    )
    return state


def _trajectory_display(
    *points: tuple[float, float], timestamp: int = 0
) -> MapDisplayData:
    """Return native packed trajectory data for coordinator cache tests."""
    return MapDisplayData.from_broadcast(
        {
            "1": {"1": {"1": points[-1][0], "2": points[-1][1]}},
            "2": {
                "1": b"".join(struct.pack("<f", point[0]) for point in points),
                "2": b"".join(struct.pack("<f", point[1]) for point in points),
            },
            "10": timestamp,
        }
    )


def _close_background_task(_hass: object, coro: object, _name: str) -> MagicMock:
    """Consume scheduled coroutine objects in coordinator unit tests."""
    close = getattr(coro, "close", None)
    if close is not None:
        close()
    task = MagicMock()
    task.done.return_value = True
    return task


def test_non_broadcast_product_key_configures_polling_client() -> None:
    """Coordinator propagates the product capability to the client."""
    product_key = next(iter(NO_BROADCAST_PRODUCT_KEYS))
    entry = MagicMock()
    entry.data = {
        "host": "10.0.0.70",
        "port": 9002,
        "device_id": "device-id",
        "product_key": product_key,
    }

    with patch("custom_components.narwal.coordinator.NarwalClient") as client_class:
        NarwalCoordinator(MagicMock(), entry)

    client_class.assert_called_once_with(
        host="10.0.0.70",
        port=9002,
        device_id="device-id",
        topic_prefix=f"/{product_key}",
        supports_broadcasts=False,
    )


def test_room_profiles_only_override_customized_fields() -> None:
    """Read-only room profile creation must not freeze global defaults."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.client = MagicMock()
    coordinator.client.state = NarwalState()
    coordinator.data = coordinator.client.state
    coordinator.clean_settings = CleanSettings()
    coordinator.room_clean_settings = {}
    coordinator.room_clean_settings_customized = {}

    coordinator.room_clean_settings_for(4)
    coordinator.clean_settings.fan = FanLevel.STRONG
    coordinator.clean_settings.water = MopHumidity.WET

    settings = coordinator.room_clean_settings_for_rooms([4])[4]

    assert settings.fan == FanLevel.STRONG
    assert settings.water == MopHumidity.WET

    coordinator.set_room_clean_setting(4, "water", MopHumidity.DRY)
    merged = coordinator.room_clean_settings_for_rooms([4])[4]

    assert merged.fan == FanLevel.STRONG
    assert merged.water == MopHumidity.DRY


def test_effective_room_profile_follows_global_defaults_until_customized() -> None:
    """Room entity reads should not materialize stale inherited defaults."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.client = MagicMock()
    coordinator.client.state = NarwalState()
    coordinator.data = coordinator.client.state
    coordinator.clean_settings = CleanSettings()
    coordinator.room_clean_settings = {}
    coordinator.room_clean_settings_customized = {}

    first = coordinator.effective_room_clean_settings_for(4)
    coordinator.clean_settings.route = first.route
    coordinator.clean_settings.fan = FanLevel.STRONG

    inherited = coordinator.effective_room_clean_settings_for(4)

    assert inherited.fan == FanLevel.STRONG
    assert coordinator.room_clean_settings == {}

    coordinator.set_room_clean_setting(4, "water", MopHumidity.DRY)
    coordinator.clean_settings.water = MopHumidity.WET
    customized = coordinator.effective_room_clean_settings_for(4)

    assert customized.fan == FanLevel.STRONG
    assert customized.water == MopHumidity.DRY


def test_room_profiles_can_be_bypassed_for_explicit_service_settings() -> None:
    """Callers can request exact settings without saved room-profile overrides."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.client = MagicMock()
    coordinator.client.state = NarwalState()
    coordinator.data = coordinator.client.state
    coordinator.clean_settings = CleanSettings()
    coordinator.room_clean_settings = {}
    coordinator.room_clean_settings_customized = {}
    coordinator.set_room_clean_setting(4, "fan", FanLevel.MUTE)
    requested = RoomCleanSettings(fan=FanLevel.STRONG)

    settings = coordinator.room_clean_settings_for_rooms(
        [4],
        default=requested,
        use_room_profiles=False,
    )[4]

    assert settings is requested
    assert settings.fan == FanLevel.STRONG


def test_selected_clean_rooms_fall_back_to_all_rooms() -> None:
    """No room selection means the native start command cleans every room."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.client = MagicMock()
    coordinator.client.state = NarwalState()
    coordinator.data = coordinator.client.state
    coordinator.selected_clean_rooms = {}

    assert coordinator.selected_clean_room_ids_for([4, 5]) == [4, 5]


def test_selected_clean_rooms_prune_stale_ids_and_are_map_scoped() -> None:
    """Known selected rooms remain scoped without mutating stale selections."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.client = MagicMock()
    coordinator.client.state = NarwalState()
    coordinator.data = coordinator.client.state
    coordinator.selected_clean_rooms = {}

    coordinator.set_room_selected_for_clean(5, True, map_id="upstairs")
    coordinator.set_room_selected_for_clean(99, True, map_id="upstairs")

    assert coordinator.selected_clean_room_ids_for([4, 5], map_id="upstairs") == [5]
    assert coordinator.selected_clean_room_ids_for([4, 5], map_id="downstairs") == [4, 5]

    coordinator.set_room_selected_for_clean(5, False, map_id="upstairs")
    assert coordinator.selected_clean_room_ids_for([4, 5], map_id="upstairs") == []


def test_selected_clean_rooms_fall_back_when_every_selected_room_vanished() -> None:
    """A vanished explicit selection continues to fail closed."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.client = MagicMock()
    coordinator.client.state = NarwalState()
    coordinator.data = coordinator.client.state
    coordinator.selected_clean_rooms = {"upstairs": {99}}

    assert coordinator.selected_clean_room_ids_for([4, 5], map_id="upstairs") == []
    assert coordinator.selected_clean_room_ids_for([4, 5], map_id="upstairs") == []
    assert coordinator.has_selected_clean_rooms(map_id="upstairs")


def test_unidentified_map_selection_remains_explicit_after_identification() -> None:
    """Learning a map id cannot broaden an unresolved explicit selection."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.selected_clean_rooms = {None: {4}}
    coordinator._room_selection_store_loaded = True

    assert coordinator.selected_clean_room_ids_for([4, 5], map_id="100") == [4]
    assert coordinator.has_selected_clean_rooms(map_id="100")
    assert coordinator.is_room_selected_for_clean(4, map_id="100")

    coordinator._schedule_room_selection_save = MagicMock()
    coordinator.set_room_selected_for_clean(4, False, map_id="100")
    coordinator.set_room_selected_for_clean(5, True, map_id="100")

    assert None not in coordinator.selected_clean_rooms
    assert coordinator.selected_clean_rooms == {"100": {5}}


async def test_room_selection_store_preserves_disappeared_selected_room() -> None:
    """Restart cannot broaden a stale explicit selection to every current room."""
    store = _RoomSelectionStore()
    before = NarwalCoordinator.__new__(NarwalCoordinator)
    before.selected_clean_rooms = {"upstairs": {4}}
    before._room_selection_store = store
    before._room_selection_save_lock = asyncio.Lock()
    before._room_selection_store_loaded = True

    await before._async_save_room_selections()

    after = NarwalCoordinator.__new__(NarwalCoordinator)
    after.selected_clean_rooms = {}
    after._room_selection_store = store
    after._room_selection_store_loaded = False
    await after._async_restore_room_selections()

    assert after.selected_clean_room_ids_for([5], map_id="upstairs") == []
    assert after.is_room_selected_for_clean(4, map_id="upstairs")


async def test_room_store_restores_customized_profile_without_entities() -> None:
    """Disabled room controls cannot lose their raw customized values."""
    store = _RoomSelectionStore()
    before = NarwalCoordinator.__new__(NarwalCoordinator)
    before.selected_clean_rooms = {}
    before.room_clean_settings = {
        ("upstairs", 4): RoomCleanSettings(
            work_mode=WorkMode.MOP,
            fan=FanLevel.STRONG,
            passes=3,
        )
    }
    before.room_clean_settings_customized = {
        ("upstairs", 4): {"work_mode", "fan", "passes"}
    }
    before._room_selection_store = store
    before._room_selection_save_lock = asyncio.Lock()
    before._room_selection_store_loaded = True

    await before._async_save_room_selections()

    after = NarwalCoordinator.__new__(NarwalCoordinator)
    after.selected_clean_rooms = {}
    after.room_clean_settings = {}
    after.room_clean_settings_customized = {}
    after._room_selection_store = store
    after._room_selection_store_loaded = False
    await after._async_restore_room_selections()

    restored = after.room_clean_settings[("upstairs", 4)]
    assert restored.work_mode == WorkMode.MOP
    assert restored.fan == FanLevel.STRONG
    assert restored.passes == 3
    assert after.room_clean_settings_customized == {
        ("upstairs", 4): {"work_mode", "fan", "passes"}
    }


async def test_room_selection_load_failure_cannot_overwrite_stored_state() -> None:
    """A failed restore must not replace unread selections during shutdown."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.selected_clean_rooms = {}
    coordinator._room_selection_store = MagicMock()
    coordinator._room_selection_store.async_load = AsyncMock(side_effect=OSError)
    coordinator._room_selection_store.async_save = AsyncMock()
    coordinator._room_selection_save_lock = asyncio.Lock()
    coordinator._room_selection_store_loaded = False
    coordinator._room_profile_store_loaded = False

    await coordinator._async_restore_room_selections()
    await coordinator._async_save_room_selections()

    assert coordinator.selected_clean_room_ids_for([4, 5], map_id="100") == []
    coordinator._room_selection_store.async_save.assert_not_awaited()


async def test_selection_change_cannot_authorize_unread_profile_overwrite() -> None:
    """A room toggle after failed restore cannot replace durable profiles."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.selected_clean_rooms = {}
    coordinator.room_clean_settings = {
        ("upstairs", 4): RoomCleanSettings(fan=FanLevel.MUTE)
    }
    coordinator.room_clean_settings_customized = {("upstairs", 4): {"fan"}}
    coordinator._room_selection_store = MagicMock()
    coordinator._room_selection_store.async_load = AsyncMock(
        side_effect=[OSError, {"maps": [], "profiles": [{"durable": "profile"}]}]
    )
    coordinator._room_selection_store.async_save = AsyncMock()
    coordinator._room_selection_save_lock = asyncio.Lock()
    coordinator._room_selection_store_loaded = False
    coordinator._room_profile_store_loaded = False
    coordinator._schedule_room_selection_save = MagicMock()

    await coordinator._async_restore_room_selections()
    coordinator.set_room_selected_for_clean(4, True, map_id="upstairs")
    await coordinator._async_save_room_selections()

    assert coordinator._room_selection_store_loaded
    assert not coordinator._room_profile_store_loaded
    coordinator._room_selection_store.async_save.assert_awaited_once_with(
        {
            "maps": [{"map_id": "upstairs", "room_ids": [4]}],
            "profiles": [{"durable": "profile"}],
        }
    )


async def test_selection_retry_preserves_other_stored_maps() -> None:
    """A local toggle after a failed read must merge unrelated stored maps."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.selected_clean_rooms = {}
    coordinator.room_clean_settings = {}
    coordinator.room_clean_settings_customized = {}
    coordinator.data = NarwalState()
    coordinator.client = MagicMock()
    coordinator.client.state = coordinator.data
    coordinator._room_selection_store = MagicMock()
    coordinator._room_selection_store.async_load = AsyncMock(
        side_effect=[
            OSError,
            {
                "maps": [
                    {"map_id": "100", "room_ids": [4]},
                    {"map_id": "200", "room_ids": [7]},
                ],
                "profiles": [],
            },
        ]
    )
    coordinator._room_selection_store.async_save = AsyncMock()
    coordinator._room_selection_save_lock = asyncio.Lock()
    coordinator._room_selection_store_loaded = False
    coordinator._room_profile_store_loaded = False
    coordinator._schedule_room_selection_save = MagicMock()

    await coordinator._async_restore_room_selections()
    coordinator.set_room_selected_for_clean(5, True, map_id="100")
    await coordinator._async_save_room_selections()

    saved = coordinator._room_selection_store.async_save.await_args.args[0]
    assert saved["maps"] == [
        {"map_id": "100", "room_ids": [5]},
        {"map_id": "200", "room_ids": [7]},
    ]


def test_map_identification_migrates_unresolved_profiles_with_selection() -> None:
    """Resolving a map keeps its customized room profiles attached."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    profile = RoomCleanSettings(work_mode=WorkMode.VACUUM)
    coordinator.selected_clean_rooms = {None: {4}}
    coordinator.room_clean_settings = {(None, 4): profile}
    coordinator.room_clean_settings_customized = {(None, 4): {"work_mode"}}
    coordinator._room_selection_store_loaded = True
    coordinator._schedule_room_selection_save = MagicMock()

    coordinator.set_room_selected_for_clean(5, True, map_id="100")

    assert coordinator.selected_clean_rooms == {"100": {4, 5}}
    assert coordinator.room_clean_settings == {("100", 4): profile}
    assert coordinator.room_clean_settings_customized == {
        ("100", 4): {"work_mode"}
    }


def test_profile_resolution_does_not_require_another_selection_toggle() -> None:
    """A fetched map immediately attaches unresolved profiles to native starts."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.clean_settings = CleanSettings(work_mode=WorkMode.VACUUM_AND_MOP)
    coordinator.selected_clean_rooms = {None: {4}}
    coordinator.room_clean_settings = {
        (None, 4): RoomCleanSettings(work_mode=WorkMode.VACUUM)
    }
    coordinator.room_clean_settings_customized = {(None, 4): {"work_mode"}}
    coordinator._room_selection_store_loaded = True
    coordinator._room_selection_dirty_maps = set()
    coordinator._schedule_room_selection_save = MagicMock()

    settings = coordinator.room_clean_settings_for_rooms([4], map_id="100")

    assert settings[4].work_mode == WorkMode.VACUUM
    assert coordinator.selected_clean_rooms == {"100": {4}}
    assert set(coordinator.room_clean_settings) == {("100", 4)}
    assert coordinator.room_clean_settings[("100", 4)].work_mode == WorkMode.VACUUM


async def test_newer_unresolved_selection_supersedes_same_stored_map() -> None:
    """A post-failure unresolved choice wins when its map becomes known."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.selected_clean_rooms = {}
    coordinator.room_clean_settings = {}
    coordinator.room_clean_settings_customized = {}
    coordinator.data = NarwalState()
    coordinator.client = MagicMock()
    coordinator.client.state = coordinator.data
    coordinator._room_selection_store = MagicMock()
    coordinator._room_selection_store.async_load = AsyncMock(
        side_effect=[
            OSError,
            {
                "maps": [{"map_id": "100", "room_ids": [4]}],
                "profiles": [],
            },
        ]
    )
    coordinator._room_selection_store.async_save = AsyncMock()
    coordinator._room_selection_save_lock = asyncio.Lock()
    coordinator._room_selection_store_loaded = False
    coordinator._room_profile_store_loaded = False
    coordinator._room_selection_dirty_maps = set()
    coordinator._schedule_room_selection_save = MagicMock()

    await coordinator._async_restore_room_selections()
    coordinator.set_room_selected_for_clean(5, True)
    await coordinator._async_save_room_selections()

    assert coordinator.selected_clean_room_ids_for([4, 5], map_id="100") == [5]
    assert coordinator.selected_clean_rooms == {"100": {5}}

    saved = coordinator._room_selection_store.async_save.await_args.args[0]
    assert saved["maps"] == [
        {
            "map_id": None,
            "room_ids": [5],
            "pending_map_resolution": True,
        },
        {"map_id": "100", "room_ids": [4]},
    ]
    restarted = NarwalCoordinator.__new__(NarwalCoordinator)
    restarted.selected_clean_rooms = {}
    restarted.room_clean_settings = {}
    restarted.room_clean_settings_customized = {}
    restarted._room_selection_store = _RoomSelectionStore()
    restarted._room_selection_store.data = saved
    restarted._room_selection_store_loaded = False
    restarted._room_profile_store_loaded = False
    restarted._room_selection_dirty_maps = set()
    restarted._schedule_room_selection_save = MagicMock()

    await restarted._async_restore_room_selections()

    assert restarted.selected_clean_room_ids_for([4, 5], map_id="100") == [5]
    assert restarted.selected_clean_rooms == {"100": {5}}


async def test_persisted_unresolved_precedence_survives_failed_read_retry() -> None:
    """A shutdown retry cannot treat persisted precedence as a local deletion."""
    stored = {
        "maps": [
            {
                "map_id": None,
                "room_ids": [5],
                "pending_map_resolution": True,
            },
            {"map_id": "100", "room_ids": [4]},
        ],
        "profiles": [],
    }
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.selected_clean_rooms = {}
    coordinator.room_clean_settings = {}
    coordinator.room_clean_settings_customized = {}
    coordinator._room_selection_store = MagicMock()
    coordinator._room_selection_store.async_load = AsyncMock(
        side_effect=[OSError, stored]
    )
    coordinator._room_selection_store.async_save = AsyncMock()
    coordinator._room_selection_save_lock = asyncio.Lock()
    coordinator._room_selection_store_loaded = False
    coordinator._room_profile_store_loaded = False
    coordinator._room_selection_dirty_maps = set()

    await coordinator._async_restore_room_selections()
    await coordinator._async_save_room_selections()

    saved = coordinator._room_selection_store.async_save.await_args.args[0]
    assert saved["maps"] == stored["maps"]

    restarted = NarwalCoordinator.__new__(NarwalCoordinator)
    restarted.selected_clean_rooms = {}
    restarted.room_clean_settings = {}
    restarted.room_clean_settings_customized = {}
    restarted._room_selection_store = _RoomSelectionStore()
    restarted._room_selection_store.data = saved
    restarted._room_selection_store_loaded = False
    restarted._room_profile_store_loaded = False
    restarted._room_selection_dirty_maps = set()
    restarted._schedule_room_selection_save = MagicMock()
    await restarted._async_restore_room_selections()

    assert restarted.selected_clean_room_ids_for([4, 5], map_id="100") == [5]


async def test_newer_unresolved_profile_overrides_scoped_profile_after_restart() -> None:
    """Profile resolution preserves a newer edit made before map identification."""
    store = _RoomSelectionStore()
    store.data = {
        "maps": [{"map_id": "100", "room_ids": [4]}],
        "profiles": [
            {
                "map_id": "100",
                "room_id": 4,
                "values": {"work_mode": int(WorkMode.VACUUM_AND_MOP)},
            },
            {
                "map_id": None,
                "room_id": 4,
                "values": {"work_mode": int(WorkMode.VACUUM)},
                "pending_map_resolution": True,
            },
        ],
    }
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.selected_clean_rooms = {}
    coordinator.room_clean_settings = {}
    coordinator.room_clean_settings_customized = {}
    coordinator.clean_settings = CleanSettings()
    coordinator._room_selection_store = store
    coordinator._room_selection_store_loaded = False
    coordinator._room_profile_store_loaded = False
    coordinator._room_selection_dirty_maps = set()
    coordinator._schedule_room_selection_save = MagicMock()

    await coordinator._async_restore_room_selections()
    settings = coordinator.room_clean_settings_for_rooms([4], map_id="100")

    assert settings[4].work_mode == WorkMode.VACUUM
    assert set(coordinator.room_clean_settings) == {("100", 4)}


async def test_unresolved_profile_edit_preserves_other_scoped_fields() -> None:
    """Resolution merges newer fields without replacing scoped customization."""
    store = _RoomSelectionStore()
    store.data = {
        "maps": [{"map_id": "100", "room_ids": [4]}],
        "profiles": [
            {
                "map_id": "100",
                "room_id": 4,
                "values": {"work_mode": int(WorkMode.VACUUM)},
            },
            {
                "map_id": None,
                "room_id": 4,
                "values": {"fan": int(FanLevel.STRONG)},
                "pending_map_resolution": True,
            },
        ],
    }
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.clean_settings = CleanSettings(work_mode=WorkMode.VACUUM_AND_MOP)
    coordinator.selected_clean_rooms = {}
    coordinator.room_clean_settings = {}
    coordinator.room_clean_settings_customized = {}
    coordinator._room_selection_store = store
    coordinator._room_selection_store_loaded = False
    coordinator._room_profile_store_loaded = False
    coordinator._room_selection_dirty_maps = set()
    coordinator._schedule_room_selection_save = MagicMock()

    await coordinator._async_restore_room_selections()
    settings = coordinator.room_clean_settings_for_rooms([4], map_id="100")

    assert settings[4].work_mode == WorkMode.VACUUM
    assert settings[4].fan == FanLevel.STRONG
    assert coordinator.room_clean_settings_customized == {
        ("100", 4): {"work_mode", "fan"}
    }


async def test_room_selection_write_failure_does_not_escape() -> None:
    """Store write failures are logged without aborting coordinator shutdown."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.selected_clean_rooms = {"100": {4}}
    coordinator.room_clean_settings = {}
    coordinator.room_clean_settings_customized = {}
    coordinator._room_selection_store = MagicMock()
    coordinator._room_selection_store.async_save = AsyncMock(
        side_effect=PermissionError
    )
    coordinator._room_selection_save_lock = asyncio.Lock()
    coordinator._room_selection_store_loaded = True
    coordinator._room_profile_store_loaded = True
    coordinator._room_selection_dirty_maps = {"100"}

    await coordinator._async_save_room_selections()

    assert coordinator._room_selection_dirty_maps == {"100"}


async def test_cancelled_room_save_cannot_overwrite_newer_selection() -> None:
    """Serialization remains held until a cancelled Store write completes."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.selected_clean_rooms = {"upstairs": {4}}
    coordinator.room_clean_settings = {}
    coordinator.room_clean_settings_customized = {}
    coordinator._room_selection_store_loaded = True
    coordinator._room_profile_store_loaded = True
    coordinator._room_selection_save_lock = asyncio.Lock()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    saved: list[object] = []

    async def save(data: object) -> None:
        if not saved:
            first_started.set()
            await release_first.wait()
        saved.append(data)

    coordinator._room_selection_store = MagicMock()
    coordinator._room_selection_store.async_save = AsyncMock(side_effect=save)

    older = asyncio.create_task(coordinator._async_save_room_selections())
    await first_started.wait()
    coordinator.selected_clean_rooms["upstairs"].add(5)
    newer = asyncio.create_task(coordinator._async_save_room_selections())
    older.cancel()
    await asyncio.sleep(0)
    older.cancel()
    await asyncio.sleep(0)
    release_first.set()
    await asyncio.gather(older, newer, return_exceptions=True)

    assert saved[-1]["maps"] == [
        {"map_id": "upstairs", "room_ids": [4, 5]}
    ]


async def test_malformed_room_selection_store_remains_non_authoritative() -> None:
    """Malformed nested data cannot enable starts or be overwritten."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.selected_clean_rooms = {}
    coordinator._room_selection_store = MagicMock()
    coordinator._room_selection_store.async_load = AsyncMock(
        return_value={"maps": [{"map_id": "100", "room_ids": "4"}]}
    )
    coordinator._room_selection_store.async_save = AsyncMock()
    coordinator._room_selection_save_lock = asyncio.Lock()
    coordinator._room_selection_store_loaded = False
    coordinator._room_profile_store_loaded = False

    await coordinator._async_restore_room_selections()
    await coordinator._async_save_room_selections()

    assert coordinator.selected_clean_room_ids_for([4, 5], map_id="100") == []
    coordinator._room_selection_store.async_save.assert_not_awaited()


def test_selected_clean_room_presence_is_map_scoped() -> None:
    """Whole-floor setup can distinguish explicit selections per map."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.client = MagicMock()
    coordinator.client.state = NarwalState()
    coordinator.data = coordinator.client.state
    coordinator.selected_clean_rooms = {"upstairs": {5}}

    assert coordinator.has_selected_clean_rooms(map_id="upstairs")
    assert not coordinator.has_selected_clean_rooms(map_id="downstairs")


def test_active_clean_settings_follow_current_room_and_runtime_updates() -> None:
    """Live controls report dispatched room profiles instead of pending globals."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    state = NarwalState(working_status=WorkingStatus.CLEANING)
    state.current_room_id = 5
    coordinator.client = MagicMock()
    coordinator.client.state = state
    coordinator.data = state
    coordinator.active_clean_work_mode = None
    coordinator.active_room_clean_settings = {}
    requested = {
        4: RoomCleanSettings(fan=FanLevel.NORMAL),
        5: RoomCleanSettings(fan=FanLevel.STRONG),
    }

    coordinator.record_accepted_clean_start(requested)

    assert coordinator.active_clean_setting("fan") == FanLevel.STRONG
    assert coordinator.active_room_clean_settings[5] is not requested[5]

    coordinator.set_active_clean_setting("fan", FanLevel.DEEP)

    assert coordinator.active_clean_setting("fan") == FanLevel.DEEP
    assert all(
        settings.fan == FanLevel.DEEP
        for settings in coordinator.active_room_clean_settings.values()
    )


def test_runtime_setting_is_retained_without_reconstructed_room_profiles() -> None:
    """An accepted live command remains visible without startup task profiles."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    state = NarwalState(working_status=WorkingStatus.CLEANING)
    coordinator.client = MagicMock()
    coordinator.client.state = state
    coordinator.data = state
    coordinator.active_clean_work_mode = None
    coordinator.active_room_clean_settings = {}
    coordinator.active_clean_setting_overrides = {}

    coordinator.set_active_clean_setting("fan", FanLevel.STRONG)

    assert coordinator.active_clean_setting("fan") == FanLevel.STRONG

    state.working_status = WorkingStatus.STANDBY
    coordinator._sync_active_clean_context(state)

    assert coordinator.active_clean_setting_overrides == {}


def test_mixed_active_clean_uses_current_room_mode_for_live_controls() -> None:
    """Runtime control applicability follows the room currently being cleaned."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    state = NarwalState(working_status=WorkingStatus.CLEANING)
    state.current_room_id = 5
    coordinator.client = MagicMock()
    coordinator.client.state = state
    coordinator.data = state
    coordinator.active_clean_work_mode = None
    coordinator.active_room_clean_settings = {}

    coordinator.record_accepted_clean_start(
        {
            4: RoomCleanSettings(work_mode=WorkMode.MOP),
            5: RoomCleanSettings(work_mode=WorkMode.VACUUM),
        }
    )

    assert coordinator.active_clean_work_mode is None
    assert coordinator.clean_setting_applicability_mode(live=True) == WorkMode.VACUUM

    state.current_room_id = 4
    assert coordinator.clean_setting_applicability_mode(live=True) == WorkMode.MOP


def test_paused_standby_task_context_blocks_new_actions() -> None:
    """Paused STANDBY overlays still represent the current clean task."""
    state = NarwalState()
    state.task_progress_percent = 72
    state.task_elapsed_time = 900
    state.current_room_id = 4

    state.update_from_base_status({"3": {"1": 1, "2": 1}, "11": 1, "47": 2})

    assert state.working_status == WorkingStatus.STANDBY
    assert state.is_paused
    assert state.has_paused_clean_task_context
    assert is_live_clean_setting_available(state)
    assert not can_edit_pending_clean_settings(state)
    assert not can_start_cleaning(state)


def test_task_completed_off_dock_remains_busy() -> None:
    """TASK_COMPLETED remains the return leg while the robot is off-dock."""
    state = NarwalState(working_status=WorkingStatus.TASK_COMPLETED)
    state.dock_presence = 2
    state.dock_field11 = 1
    state.dock_field47 = 2

    assert not state.is_docked
    assert is_clean_session_context(state)
    assert is_narwal_task_busy(state)
    assert not can_edit_pending_clean_settings(state)
    assert not can_start_cleaning(state)


def test_task_completed_docked_releases_robot_controls() -> None:
    """A seated robot is idle even if its dock retains TASK_COMPLETED."""
    state = NarwalState()
    state.update_from_base_status(
        {"3": {"1": int(WorkingStatus.TASK_COMPLETED), "3": 6}}
    )

    assert state.is_docked
    assert not is_clean_session_context(state)
    assert not is_narwal_task_busy(state)
    assert can_edit_pending_clean_settings(state)
    assert can_start_cleaning(state)


def test_task_completed_does_not_reuse_retained_dock_fields() -> None:
    """A status-only completion packet keeps the return leg busy."""
    state = NarwalState()
    state.update_from_base_status(
        {"3": {"1": int(WorkingStatus.DOCKED), "3": 6}, "11": 2}
    )
    state.update_from_base_status(
        {"3": {"1": int(WorkingStatus.TASK_COMPLETED)}}
    )

    assert state.is_docked
    assert not state.has_current_dock_presence_signal
    assert is_clean_session_context(state)
    assert is_narwal_task_busy(state)
    assert not can_start_cleaning(state)


def test_remapping_does_not_expose_live_clean_settings() -> None:
    """Map-building tasks do not accept live clean-setting commands."""
    state = NarwalState(working_status=WorkingStatus.REMAPPING)

    assert not is_live_clean_setting_available(state)


@pytest.mark.parametrize(
    "working_status", (WorkingStatus.TASK_COMPLETED, WorkingStatus.ERROR)
)
def test_terminal_status_does_not_expose_live_clean_settings(
    working_status: WorkingStatus,
) -> None:
    """Accepted-start context cannot expose controls after a terminal status."""
    state = NarwalState(working_status=WorkingStatus.CLEANING)
    state.assume_robot_clean()
    state.working_status = working_status

    assert not is_live_clean_setting_available(state)


def test_map_display_cache_payload_round_trips_native_trajectory() -> None:
    """Native display-map trails can be serialized through HA storage."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    state = _trajectory_state()
    state.map_display_data.trajectory_breaks = (1,)

    payload = coordinator._map_display_cache_payload(state)
    restored = NarwalCoordinator._map_display_from_cache(payload)

    assert payload is not None
    assert restored is not None
    assert payload["map_id"] == 12
    assert payload["map_created_at"] == 34
    assert payload["active_clean"] is False
    assert restored.robot_x == 0.0
    assert restored.robot_y == 0.0
    assert restored.robot_heading == 0.0
    assert restored.timestamp == state.map_display_data.timestamp
    assert restored.dock_ref_x == state.map_display_data.dock_ref_x
    assert restored.dock_ref_y == state.map_display_data.dock_ref_y
    assert restored.trajectory_x_values == b"xxxx"
    assert restored.trajectory_y_values == b"yyyy"
    assert restored.trajectory_signature == (4, 4, 99)
    assert restored.trajectory_breaks == (1,)


def test_native_trajectory_windows_are_joined_by_exact_overlap() -> None:
    """Rolling robot windows extend the HA-retained native route once."""
    previous = _trajectory_display(
        (1.0, 1.0),
        (2.0, 2.0),
        (3.0, 3.0),
        timestamp=100,
    )
    current = _trajectory_display(
        (3.0, 3.0),
        (4.0, 4.0),
        (5.0, 5.0),
        timestamp=200,
    )

    merged = NarwalCoordinator._merge_native_trajectory_windows(previous, current)

    assert merged.trajectory_points() == [
        (1.0, 1.0),
        (2.0, 2.0),
        (3.0, 3.0),
        (4.0, 4.0),
        (5.0, 5.0),
    ]
    assert merged.trajectory_signature[0] == 5
    assert merged.robot_x == 5.0
    assert merged.timestamp == 200


def test_overlapping_window_preserves_break_inside_matched_points() -> None:
    """A native discontinuity in the overlap remains in retained geometry."""
    previous = _trajectory_display(
        (1.0, 1.0),
        (2.0, 2.0),
        (3.0, 3.0),
        timestamp=100,
    )
    current = _trajectory_display(
        (2.0, 2.0),
        (3.0, 3.0),
        (4.0, 4.0),
        timestamp=200,
    )
    current.trajectory_breaks = (1,)

    merged = NarwalCoordinator._merge_native_trajectory_windows(previous, current)

    assert merged.trajectory_points() == [
        (1.0, 1.0),
        (2.0, 2.0),
        (3.0, 3.0),
        (4.0, 4.0),
    ]
    assert merged.trajectory_breaks == (2,)


def test_native_trajectory_overlap_is_bounded_at_retention_limit() -> None:
    """Maximum-size unrelated windows do not require quadratic byte copying."""
    point_count = 50_000
    previous = MapDisplayData(
        trajectory_x_values=struct.pack("<I", 1) * point_count,
        trajectory_y_values=struct.pack("<I", 2) * point_count,
    )
    current = MapDisplayData(
        trajectory_x_values=struct.pack("<I", 3) * point_count,
        trajectory_y_values=struct.pack("<I", 4) * point_count,
    )

    assert NarwalCoordinator._native_trajectory_overlap(previous, current) == 0


def test_repeated_native_trajectory_window_does_not_duplicate_route() -> None:
    """Repeated display_map windows update pose without growing the route."""
    previous = _trajectory_display(
        (1.0, 1.0),
        (2.0, 2.0),
        (3.0, 3.0),
        timestamp=100,
    )
    current = _trajectory_display(
        (1.0, 1.0),
        (2.0, 2.0),
        (3.0, 3.0),
        timestamp=200,
    )

    merged = NarwalCoordinator._merge_native_trajectory_windows(previous, current)

    assert merged.trajectory_points() == previous.trajectory_points()
    assert merged.trajectory_signature[0] == 3
    assert merged.timestamp == 200


def test_extending_full_trajectory_reuses_robot_window() -> None:
    """An accumulated Narwal route does not rebuild its retained prefix."""
    previous = _trajectory_display(
        (1.0, 1.0), (2.0, 2.0), (3.0, 3.0), timestamp=100
    )
    current = _trajectory_display(
        (1.0, 1.0), (2.0, 2.0), (3.0, 3.0), (4.0, 4.0), timestamp=200
    )

    merged = NarwalCoordinator._merge_native_trajectory_windows(previous, current)

    assert merged is current


def test_extending_full_trajectory_preserves_retained_breaks() -> None:
    """Exact prefix growth must not reconnect a gap retained by HA."""
    previous = _trajectory_display(
        (1.0, 1.0), (2.0, 2.0), (3.0, 3.0), timestamp=100
    )
    previous.trajectory_breaks = (2,)
    current = _trajectory_display(
        (1.0, 1.0), (2.0, 2.0), (3.0, 3.0), (4.0, 4.0), timestamp=200
    )

    merged = NarwalCoordinator._merge_native_trajectory_windows(previous, current)

    assert merged.trajectory_points() == current.trajectory_points()
    assert merged.trajectory_breaks == (2,)


def test_retained_trajectory_is_compacted_with_recent_tail_and_breaks() -> None:
    """Long routes retain their full shape without unbounded callback work."""
    display = _trajectory_display(
        *((float(index), float(index)) for index in range(10)), timestamp=200
    )

    compact_x, compact_y, compact_breaks = (
        NarwalCoordinator._compact_native_trajectory(
            display.trajectory_x_values,
            display.trajectory_y_values,
            (5,),
            max_points=6,
            recent_tail_points=2,
        )
    )
    compact = replace(
        display,
        trajectory_x_values=compact_x,
        trajectory_y_values=compact_y,
        trajectory_breaks=compact_breaks,
    )

    assert compact.trajectory_points() == [
        (0.0, 0.0),
        (2.0, 2.0),
        (5.0, 5.0),
        (7.0, 7.0),
        (8.0, 8.0),
        (9.0, 9.0),
    ]
    assert compact_breaks == (2,)


def test_accumulated_robot_route_extends_after_retained_compaction() -> None:
    """Later full Narwal windows replace, rather than append to, compacted routes."""
    previous = _trajectory_display(
        *((float(index), float(index)) for index in range(6)), timestamp=100
    )
    first_extension = _trajectory_display(
        *((float(index), float(index)) for index in range(7)), timestamp=200
    )
    second_extension = _trajectory_display(
        *((float(index), float(index)) for index in range(8)), timestamp=300
    )

    with (
        patch(
            "custom_components.narwal.coordinator.NATIVE_TRAJECTORY_MAX_POINTS", 6
        ),
        patch(
            "custom_components.narwal.coordinator."
            "NATIVE_TRAJECTORY_RECENT_TAIL_POINTS",
            2,
        ),
    ):
        compacted = NarwalCoordinator._merge_native_trajectory_windows(
            previous, first_extension
        )
        merged = NarwalCoordinator._merge_native_trajectory_windows(
            compacted, second_extension
        )

    assert len(merged.trajectory_points()) == 6
    assert merged.trajectory_points()[0] == (0.0, 0.0)
    assert merged.trajectory_points()[-2:] == [(6.0, 6.0), (7.0, 7.0)]


def test_rolling_window_containing_compacted_tail_appends_only_new_points() -> None:
    """A window starting before the retained tail must not duplicate old geometry."""
    full_previous = _trajectory_display(
        *((float(index), float(index)) for index in range(10)), timestamp=100
    )
    current = _trajectory_display(
        *((float(index), float(index)) for index in range(6, 12)), timestamp=200
    )

    with (
        patch(
            "custom_components.narwal.coordinator.NATIVE_TRAJECTORY_MAX_POINTS", 6
        ),
        patch(
            "custom_components.narwal.coordinator."
            "NATIVE_TRAJECTORY_RECENT_TAIL_POINTS",
            2,
        ),
    ):
        compact_x, compact_y, compact_breaks = (
            NarwalCoordinator._compact_native_trajectory(
                full_previous.trajectory_x_values,
                full_previous.trajectory_y_values,
                (),
            )
        )
        compacted = replace(
            full_previous,
            trajectory_x_values=compact_x,
            trajectory_y_values=compact_y,
            trajectory_breaks=compact_breaks,
        )
        merged = NarwalCoordinator._merge_native_trajectory_windows(
            compacted, current
        )

    assert merged.trajectory_points() == [
        (0.0, 0.0),
        (5.0, 5.0),
        (7.0, 7.0),
        (9.0, 9.0),
        (10.0, 10.0),
        (11.0, 11.0),
    ]
    assert not merged.trajectory_breaks


def test_accumulated_robot_route_replaces_compacted_route_with_breaks() -> None:
    """A later complete Narwal window resolves gaps retained before compaction."""
    retained = _trajectory_display(
        *((float(index), float(index)) for index in range(8)), timestamp=200
    )
    retained.trajectory_breaks = (3,)
    second_extension = _trajectory_display(
        *((float(index), float(index)) for index in range(9)), timestamp=300
    )

    with (
        patch(
            "custom_components.narwal.coordinator.NATIVE_TRAJECTORY_MAX_POINTS", 6
        ),
        patch(
            "custom_components.narwal.coordinator."
            "NATIVE_TRAJECTORY_RECENT_TAIL_POINTS",
            2,
        ),
    ):
        compact_x, compact_y, compact_breaks = (
            NarwalCoordinator._compact_native_trajectory(
                retained.trajectory_x_values,
                retained.trajectory_y_values,
                retained.trajectory_breaks,
            )
        )
        compacted = replace(
            retained,
            trajectory_x_values=compact_x,
            trajectory_y_values=compact_y,
            trajectory_breaks=compact_breaks,
        )
        assert compacted.trajectory_breaks
        merged = NarwalCoordinator._merge_native_trajectory_windows(
            compacted, second_extension
        )

    assert len(merged.trajectory_points()) == 6
    assert merged.trajectory_points()[0] == (0.0, 0.0)
    assert merged.trajectory_points()[-2:] == [(7.0, 7.0), (8.0, 8.0)]
    assert not merged.trajectory_breaks


def test_equal_size_robot_route_replaces_compacted_route() -> None:
    """A full route at the retention cap must not append compacted data."""
    retained = _trajectory_display(
        *((float(index), float(index)) for index in range(9)), timestamp=200
    )
    current = _trajectory_display(
        (0.0, 0.0),
        (1.0, 1.0),
        (2.0, 2.0),
        (3.0, 3.0),
        (7.0, 7.0),
        (8.0, 8.0),
        timestamp=300,
    )

    with (
        patch(
            "custom_components.narwal.coordinator.NATIVE_TRAJECTORY_MAX_POINTS", 6
        ),
        patch(
            "custom_components.narwal.coordinator."
            "NATIVE_TRAJECTORY_RECENT_TAIL_POINTS",
            2,
        ),
    ):
        compact_x, compact_y, compact_breaks = (
            NarwalCoordinator._compact_native_trajectory(
                retained.trajectory_x_values,
                retained.trajectory_y_values,
                retained.trajectory_breaks,
            )
        )
        compacted = replace(
            retained,
            trajectory_x_values=compact_x,
            trajectory_y_values=compact_y,
            trajectory_breaks=compact_breaks,
        )
        merged = NarwalCoordinator._merge_native_trajectory_windows(
            compacted, current
        )

    assert merged.trajectory_points() == current.trajectory_points()


def test_compacted_route_tail_matches_repeated_x_coordinates() -> None:
    """Compacted replacement matches complete points, not the first X bytes."""
    compacted = _trajectory_display(
        (0.0, 0.0),
        (1.0, 1.0),
        (2.0, 2.0),
        (3.0, 3.0),
        (5.0, 10.0),
        (5.0, 11.0),
        timestamp=200,
    )
    compacted.trajectory_breaks = (3,)
    current = _trajectory_display(
        (0.0, 0.0),
        (5.0, 99.0),
        (5.0, 100.0),
        (4.0, 4.0),
        (5.0, 10.0),
        (5.0, 11.0),
        (6.0, 6.0),
        timestamp=300,
    )

    with (
        patch(
            "custom_components.narwal.coordinator.NATIVE_TRAJECTORY_MAX_POINTS", 6
        ),
        patch(
            "custom_components.narwal.coordinator."
            "NATIVE_TRAJECTORY_RECENT_TAIL_POINTS",
            2,
        ),
    ):
        merged = NarwalCoordinator._merge_native_trajectory_windows(
            compacted, current
        )

    assert (3.0, 3.0) not in merged.trajectory_points()
    assert merged.trajectory_points()[-2:] == [(5.0, 11.0), (6.0, 6.0)]
    assert not merged.trajectory_breaks


def test_active_cached_trajectory_requires_live_window_overlap() -> None:
    """A clean started during HA downtime must not inherit the previous route."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    previous = _trajectory_display((1.0, 1.0), (2.0, 2.0), timestamp=100)
    current = _trajectory_display((8.0, 8.0), (9.0, 9.0), timestamp=200)
    state = NarwalState(working_status=WorkingStatus.CLEANING)
    state.map_display_data = current
    coordinator._retained_map_display = previous
    coordinator._map_display_cache_signature = previous.trajectory_signature
    coordinator._map_display_cache_restored = True
    coordinator._map_display_cache_restored_from_active = True

    coordinator._retain_native_trajectory(state)

    assert state.map_display_data is current
    assert coordinator._retained_map_display is current
    assert not coordinator._map_display_cache_restored
    assert not coordinator._map_display_cache_restored_from_active


def test_active_cached_trajectory_merges_overlapping_live_window() -> None:
    """An overlapping first live window proves an active-cache reconnect."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    previous = _trajectory_display(
        (1.0, 1.0), (2.0, 2.0), (3.0, 3.0), (4.0, 4.0), timestamp=100
    )
    current = _trajectory_display(
        (2.0, 2.0), (3.0, 3.0), (4.0, 4.0), (5.0, 5.0), timestamp=200
    )
    state = NarwalState(working_status=WorkingStatus.CLEANING)
    state.map_display_data = current
    coordinator._retained_map_display = previous
    coordinator._map_display_cache_signature = previous.trajectory_signature
    coordinator._map_display_cache_restored = True
    coordinator._map_display_cache_restored_from_active = True

    coordinator._retain_native_trajectory(state)

    assert state.map_display_data.trajectory_points() == [
        (1.0, 1.0),
        (2.0, 2.0),
        (3.0, 3.0),
        (4.0, 4.0),
        (5.0, 5.0),
    ]
    assert not coordinator._map_display_cache_restored
    assert not coordinator._map_display_cache_restored_from_active


def test_active_compacted_cache_accepts_full_live_window_after_restart() -> None:
    """A retained recent tail validates a compacted active route after restart."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    full_previous = _trajectory_display(
        *((float(index), float(index)) for index in range(10)), timestamp=100
    )
    current = _trajectory_display(
        *((float(index), float(index)) for index in range(12)), timestamp=200
    )
    with (
        patch(
            "custom_components.narwal.coordinator.NATIVE_TRAJECTORY_MAX_POINTS", 6
        ),
        patch(
            "custom_components.narwal.coordinator."
            "NATIVE_TRAJECTORY_RECENT_TAIL_POINTS",
            2,
        ),
    ):
        compact_x, compact_y, compact_breaks = (
            NarwalCoordinator._compact_native_trajectory(
                full_previous.trajectory_x_values,
                full_previous.trajectory_y_values,
                (),
            )
        )
        previous = replace(
            full_previous,
            trajectory_x_values=compact_x,
            trajectory_y_values=compact_y,
            trajectory_breaks=compact_breaks,
        )
        state = NarwalState(working_status=WorkingStatus.CLEANING)
        state.map_display_data = current
        coordinator._retained_map_display = previous
        coordinator._map_display_cache_signature = previous.trajectory_signature
        coordinator._map_display_cache_restored = True
        coordinator._map_display_cache_restored_from_active = True

        coordinator._retain_native_trajectory(state)

    assert state.map_display_data.trajectory_points()[-1] == (11.0, 11.0)
    assert not coordinator._map_display_cache_restored
    assert not coordinator._map_display_cache_restored_from_active


def test_metric_only_overlap_completes_active_cache_validation() -> None:
    """An overlapping live window validates a route despite coarse standby."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    previous = _trajectory_display(
        (1.0, 1.0), (2.0, 2.0), (3.0, 3.0), (4.0, 4.0), timestamp=100
    )
    current = _trajectory_display(
        (2.0, 2.0), (3.0, 3.0), (4.0, 4.0), (5.0, 5.0), timestamp=200
    )
    state = NarwalState(working_status=WorkingStatus.STANDBY)
    state.update_from_working_status({"3": 120})
    state.map_display_data = current
    coordinator._retained_map_display = previous
    coordinator._map_display_cache_signature = previous.trajectory_signature
    coordinator._map_display_cache_restored = True
    coordinator._map_display_cache_restored_from_active = True

    coordinator._retain_native_trajectory(state)

    assert state.map_display_data.trajectory_points()[-1] == (5.0, 5.0)
    assert not coordinator._map_display_cache_restored
    assert not coordinator._map_display_cache_restored_from_active


def test_active_cached_trajectory_rejects_single_point_overlap() -> None:
    """One coincidental point cannot join routes from different clean sessions."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    previous = _trajectory_display(
        (1.0, 1.0), (2.0, 2.0), (3.0, 3.0), timestamp=100
    )
    current = _trajectory_display(
        (3.0, 3.0), (8.0, 8.0), (9.0, 9.0), timestamp=200
    )
    state = NarwalState(working_status=WorkingStatus.CLEANING)
    state.map_display_data = current
    coordinator._retained_map_display = previous
    coordinator._map_display_cache_signature = previous.trajectory_signature
    coordinator._map_display_cache_restored = True
    coordinator._map_display_cache_restored_from_active = True

    coordinator._retain_native_trajectory(state)

    assert state.map_display_data is current
    assert not coordinator._map_display_cache_restored_from_active


def test_active_cached_trajectory_waits_for_validation_window() -> None:
    """A tiny first live window defers the restored-route decision."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    previous = _trajectory_display(
        (1.0, 1.0), (2.0, 2.0), (3.0, 3.0), timestamp=100
    )
    current = _trajectory_display((3.0, 3.0), timestamp=200)
    current.robot_heading = 45.0
    state = NarwalState(working_status=WorkingStatus.CLEANING)
    state.map_display_data = current
    coordinator._retained_map_display = previous
    coordinator._map_display_cache_signature = previous.trajectory_signature
    coordinator._map_display_cache_restored = True
    coordinator._map_display_cache_restored_from_active = True

    coordinator._retain_native_trajectory(state)

    assert state.map_display_data.trajectory_points() == previous.trajectory_points()
    assert state.map_display_data.robot_x == 3.0
    assert state.map_display_data.robot_y == 3.0
    assert state.map_display_data.robot_heading == 45.0
    assert state.map_display_data.timestamp == 200
    assert coordinator._map_display_cache_restored_from_active

    # Reprocessing the state published above is not new trajectory evidence.
    coordinator._retain_native_trajectory(state)
    assert coordinator._map_display_cache_restored_from_active

    unrelated = _trajectory_display(
        (8.0, 8.0), (9.0, 9.0), (10.0, 10.0), timestamp=300
    )
    state.map_display_data = unrelated
    coordinator._retain_native_trajectory(state)

    assert state.map_display_data is unrelated
    assert not coordinator._map_display_cache_restored_from_active


def test_active_cached_trajectory_ignores_pose_only_window_for_validation() -> None:
    """A pose-only packet cannot validate a route restored after restart."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    previous = _trajectory_display((1.0, 1.0), (2.0, 2.0), timestamp=100)
    coordinator._retained_map_display = previous
    coordinator._map_display_cache_signature = previous.trajectory_signature
    coordinator._map_display_cache_restored = True
    coordinator._map_display_cache_restored_from_active = True
    state = NarwalState(working_status=WorkingStatus.CLEANING)
    state.map_display_data = MapDisplayData(robot_x=3.0, robot_y=3.0, timestamp=150)

    coordinator._retain_native_trajectory(state)

    assert coordinator._map_display_cache_restored_from_active
    assert state.map_display_data.trajectory_points() == [(1.0, 1.0), (2.0, 2.0)]

    state.map_display_data = _trajectory_display(
        (8.0, 8.0), (9.0, 9.0), timestamp=200
    )
    coordinator._retain_native_trajectory(state)

    assert state.map_display_data.trajectory_points() == [(8.0, 8.0), (9.0, 9.0)]
    assert not coordinator._map_display_cache_restored_from_active


def test_empty_native_trajectory_window_preserves_completed_route() -> None:
    """A terminal pose-only packet must not discard the completed trail."""
    previous = _trajectory_display((1.0, 1.0), (2.0, 2.0), timestamp=100)
    current = MapDisplayData(robot_x=9.0, robot_y=8.0, timestamp=200)

    merged = NarwalCoordinator._merge_native_trajectory_windows(previous, current)

    assert merged.trajectory_points() == previous.trajectory_points()
    assert merged.robot_x == 9.0
    assert merged.robot_y == 8.0
    assert merged.timestamp == 200


def test_nonoverlapping_window_after_restart_keeps_both_native_sections() -> None:
    """A reboot gap retains native points before and after the missed window."""
    previous = _trajectory_display((1.0, 1.0), (2.0, 2.0), timestamp=100)
    current = _trajectory_display((8.0, 8.0), (9.0, 9.0), timestamp=200)

    merged = NarwalCoordinator._merge_native_trajectory_windows(previous, current)

    assert merged.trajectory_points() == [
        (1.0, 1.0),
        (2.0, 2.0),
        (8.0, 8.0),
        (9.0, 9.0),
    ]
    assert merged.trajectory_breaks == (2,)
    render_points = merged.trajectory_render_points()
    assert len(render_points) == 5
    assert math.isnan(render_points[2][0])
    assert math.isnan(render_points[2][1])


def test_retain_native_trajectory_updates_shared_state() -> None:
    """Coordinator state and persistence see the accumulated native route."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator._retained_map_display = _trajectory_display(
        (1.0, 1.0), (2.0, 2.0), timestamp=100
    )
    state = NarwalState()
    state.map_display_data = _trajectory_display(
        (2.0, 2.0), (3.0, 3.0), timestamp=200
    )

    coordinator._retain_native_trajectory(state)

    assert state.map_display_data is coordinator._retained_map_display
    assert state.map_display_data.trajectory_points() == [
        (1.0, 1.0),
        (2.0, 2.0),
        (3.0, 3.0),
    ]


def test_retain_native_trajectory_clears_when_active_map_changes() -> None:
    """A route from one floor must not be retained against another map."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    state = NarwalState()
    state.map_data = MapData(map_id=13, created_at=35)
    state.map_display_data = _trajectory_display(
        (8.0, 8.0), (9.0, 9.0), timestamp=200
    )
    coordinator.client = MagicMock()
    coordinator.client.state = state
    coordinator._retained_map_display = _trajectory_display(
        (1.0, 1.0), (2.0, 2.0), timestamp=100
    )
    coordinator._retained_map_identity = (12, 34)
    coordinator._pending_map_display_cache_snapshot = None
    coordinator._pending_map_display_cache_restore = None
    coordinator._map_display_cache_signature = (2, 1, 1)
    coordinator._map_display_cache_active_clean = False
    coordinator._map_display_cache_restored = True
    coordinator._map_display_cache_restored_from_active = False
    coordinator._schedule_map_display_cache_clear = MagicMock()

    coordinator._retain_native_trajectory(state)

    assert state.map_display_data is None
    assert coordinator._retained_map_display is None
    assert coordinator._retained_map_identity == (13, 35)
    assert coordinator._map_display_cache_signature == ()
    coordinator._schedule_map_display_cache_clear.assert_called_once_with(None)

    new_map_window = _trajectory_display((10.0, 10.0), timestamp=300)
    state.map_display_data = new_map_window
    coordinator._retain_native_trajectory(state)

    assert state.map_display_data is new_map_window
    assert coordinator._retained_map_display is new_map_window


def test_retained_trajectory_adopts_delayed_static_map_identity() -> None:
    """A map loaded after the first route still scopes later trajectory data."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    state = NarwalState()
    state.map_data = MapData(map_id=12, created_at=34)
    state.map_display_data = _trajectory_display(
        (2.0, 2.0), (3.0, 3.0), timestamp=200
    )
    coordinator.client = MagicMock()
    coordinator.client.state = state
    coordinator._retained_map_display = _trajectory_display(
        (1.0, 1.0), (2.0, 2.0), timestamp=100
    )
    coordinator._retained_map_identity = None
    coordinator._map_display_cache_restored_from_active = False
    coordinator._schedule_map_display_cache_clear = MagicMock()

    coordinator._retain_native_trajectory(state)

    assert coordinator._retained_map_identity == (12, 34)
    assert state.map_display_data.trajectory_points() == [
        (1.0, 1.0),
        (2.0, 2.0),
        (3.0, 3.0),
    ]

    state.map_data = MapData(map_id=13, created_at=35)
    state.map_display_data = _trajectory_display((8.0, 8.0), timestamp=300)
    coordinator._retain_native_trajectory(state)

    assert coordinator._retained_map_identity == (13, 35)
    assert coordinator._retained_map_display is None
    assert state.map_display_data is None
    coordinator._schedule_map_display_cache_clear.assert_called_once_with(None)


def test_remapping_trajectory_does_not_replace_retained_clean_route() -> None:
    """Map-building geometry must not contaminate the cleaning trail."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    retained = _trajectory_display((1.0, 1.0), (2.0, 2.0), timestamp=100)
    coordinator._retained_map_display = retained
    state = NarwalState(working_status=WorkingStatus.REMAPPING)
    remapping = _trajectory_display(
        (8.0, 8.0), (9.0, 9.0), timestamp=200
    )
    remapping.robot_heading = 45.0
    remapping.dock_ref_x = 4.0
    remapping.dock_ref_y = 5.0
    state.map_display_data = remapping

    coordinator._retain_native_trajectory(state)

    assert state.map_display_data.trajectory_points() == retained.trajectory_points()
    assert state.map_display_data.robot_x == 9.0
    assert state.map_display_data.robot_y == 9.0
    assert state.map_display_data.robot_heading == 45.0
    assert state.map_display_data.timestamp == 200
    assert state.map_display_data.dock_ref_x == 4.0
    assert state.map_display_data.dock_ref_y == 5.0
    assert coordinator._retained_map_display is state.map_display_data
    assert coordinator._map_display_cache_snapshot(state) is None


def test_remapping_without_retained_route_keeps_live_pose_only() -> None:
    """Map rebuilding keeps the marker without adopting mapping geometry."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator._retained_map_display = None
    state = NarwalState(working_status=WorkingStatus.REMAPPING)
    remapping = _trajectory_display((8.0, 8.0), (9.0, 9.0), timestamp=200)
    remapping.robot_heading = 45.0
    state.map_display_data = remapping

    coordinator._retain_native_trajectory(state)

    assert state.map_display_data is not None
    assert not state.map_display_data.has_trajectory
    assert state.map_display_data.robot_x == 9.0
    assert state.map_display_data.robot_y == 9.0
    assert state.map_display_data.robot_heading == 45.0
    assert state.map_display_data.timestamp == 200
    assert coordinator._retained_map_display is None


def test_new_clean_drops_pre_status_trajectory_window() -> None:
    """A display packet before clean status is dropped to avoid stale routes."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.hass = MagicMock()
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.async_create_background_task = MagicMock(
        side_effect=_close_background_task
    )
    coordinator.client = MagicMock()
    coordinator.client.last_display_map_age = 1.0
    coordinator.client.state = NarwalState(working_status=WorkingStatus.STANDBY)
    coordinator._retained_map_display = _trajectory_display(
        (1.0, 1.0), (2.0, 2.0), timestamp=100
    )
    coordinator._map_display_cache_store = _FakeStore({"old": "trail"})
    coordinator._pending_map_display_cache_snapshot = None
    coordinator._pending_map_display_cache_restore = None
    coordinator._map_display_cache_signature = (2, 1, 1)
    coordinator._map_display_cache_restored = False
    coordinator._map_display_cache_restored_from_active = False
    coordinator._map_display_cache_save_task = None
    coordinator._map_display_cache_last_save = time.monotonic()

    raw_window = _trajectory_display((8.0, 8.0), (9.0, 9.0), timestamp=200)
    coordinator.client.state.map_display_data = raw_window
    coordinator._retain_native_trajectory(coordinator.client.state)

    assert coordinator.client.state.map_display_data.trajectory_points() == [
        (1.0, 1.0),
        (2.0, 2.0),
        (8.0, 8.0),
        (9.0, 9.0),
    ]

    coordinator.client.state.working_status = WorkingStatus.CLEANING
    coordinator._clear_map_display_cache_for_new_clean()

    assert coordinator.client.state.map_display_data is None
    assert coordinator._retained_map_display is None
    assert coordinator._pending_map_display_cache_snapshot is None


async def test_restore_map_display_cache_restores_matching_static_map() -> None:
    """A saved trail is restored after restart when the static map matches."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    source = _trajectory_state()
    coordinator.client = MagicMock()
    coordinator.client.state = NarwalState()
    coordinator.client.state.map_data = source.map_data
    coordinator._map_display_cache_store = _FakeStore(
        coordinator._map_display_cache_payload(source)
    )
    coordinator._map_display_cache_signature = ()
    coordinator._pending_map_display_cache_restore = None
    coordinator._map_display_cache_restored = False

    await coordinator._async_restore_map_display_cache()

    restored = coordinator.client.state.map_display_data
    assert restored is not None
    assert restored.trajectory_signature == (4, 4, 99)
    assert coordinator._retained_map_identity == (12, 34)
    assert coordinator._map_display_cache_signature == (4, 4, 99)
    assert coordinator._map_display_cache_restored


async def test_restore_map_display_cache_rejects_inactive_route_during_clean() -> None:
    """A completed saved route cannot seed a clean already active at startup."""
    source = _trajectory_state()
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.client = MagicMock()
    coordinator.client.state = NarwalState(working_status=WorkingStatus.CLEANING)
    coordinator.client.state.map_data = source.map_data
    coordinator._map_display_cache_store = _FakeStore(
        coordinator._map_display_cache_payload(source)
    )
    coordinator._map_display_cache_signature = ()
    coordinator._pending_map_display_cache_restore = None
    coordinator._map_display_cache_restored = False
    coordinator._map_display_cache_restored_from_active = False

    await coordinator._async_restore_map_display_cache()

    assert coordinator.client.state.map_display_data is None
    assert coordinator._map_display_cache_signature == ()
    assert not coordinator._map_display_cache_restored


async def test_restore_map_display_cache_preserves_live_pose_only_packet() -> None:
    """Cached route bytes must not replace the robot's live startup pose."""
    cached = _trajectory_state()
    cached.working_status = WorkingStatus.CLEANING
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.client = MagicMock()
    coordinator.client.state = NarwalState(working_status=WorkingStatus.CLEANING)
    coordinator.client.state.map_data = cached.map_data
    coordinator.client.state.map_display_data = MapDisplayData(
        robot_x=9.0,
        robot_y=8.0,
        robot_heading=45.0,
        timestamp=200,
    )
    coordinator._map_display_cache_store = _FakeStore(
        coordinator._map_display_cache_payload(cached)
    )
    coordinator._map_display_cache_signature = ()
    coordinator._pending_map_display_cache_snapshot = None
    coordinator._pending_map_display_cache_restore = None
    coordinator._map_display_cache_restored = False
    coordinator._map_display_cache_restored_from_active = False

    await coordinator._async_restore_map_display_cache()

    restored = coordinator.client.state.map_display_data
    assert restored is not None
    assert restored.trajectory_points() == cached.map_display_data.trajectory_points()
    assert (restored.robot_x, restored.robot_y) == (9.0, 8.0)
    assert restored.robot_heading == 45.0
    assert restored.timestamp == 200


async def test_restore_map_display_cache_does_not_overwrite_live_trail() -> None:
    """A live display_map packet received during startup wins over stored routes."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    cached = _trajectory_state()
    cached.map_display_data = MapDisplayData(
        robot_x=5.0,
        robot_y=6.0,
        robot_heading=180.0,
        timestamp=123457,
        dock_ref_x=3.0,
        dock_ref_y=4.0,
        trajectory_x_values=b"aa",
        trajectory_y_values=b"bb",
        trajectory_signature=(2, 2, 77),
    )
    coordinator.client = MagicMock()
    coordinator.client.state = _trajectory_state()
    coordinator._map_display_cache_store = _FakeStore(
        coordinator._map_display_cache_payload(cached)
    )
    coordinator._map_display_cache_signature = ()
    coordinator._pending_map_display_cache_restore = None
    coordinator._map_display_cache_restored = False
    coordinator._map_display_cache_restored_from_active = False

    await coordinator._async_restore_map_display_cache()

    assert coordinator.client.state.map_display_data.trajectory_signature == (4, 4, 99)
    assert coordinator._map_display_cache_signature == ()
    assert not coordinator._map_display_cache_restored


async def test_restore_active_cache_merges_early_live_window() -> None:
    """An early startup window validates and extends an active saved prefix."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    cached = _trajectory_state()
    cached.working_status = WorkingStatus.CLEANING
    cached.map_display_data = _trajectory_display(
        *((float(index), float(index)) for index in range(1, 31)),
        timestamp=100,
    )
    payload = coordinator._map_display_cache_payload(cached)
    assert payload is not None
    assert payload["active_clean"] is True

    coordinator.client = MagicMock()
    coordinator.client.state = _trajectory_state()
    coordinator.client.state.working_status = WorkingStatus.CLEANING
    coordinator.client.state.map_display_data = _trajectory_display(
        *((float(index), float(index)) for index in range(27, 57)),
        timestamp=200,
    )
    coordinator._map_display_cache_store = _FakeStore(payload)
    coordinator._map_display_cache_signature = ()
    coordinator._pending_map_display_cache_restore = None
    coordinator._map_display_cache_restored = False
    coordinator._map_display_cache_restored_from_active = False
    coordinator._retained_map_display = None
    coordinator._retained_map_identity = None

    await coordinator._async_restore_map_display_cache()

    assert coordinator.client.state.map_display_data.trajectory_points() == [
        (float(index), float(index)) for index in range(1, 57)
    ]
    assert not coordinator._map_display_cache_restored_from_active


async def test_stale_startup_status_keeps_validated_active_cache_until_cleaning() -> None:
    """A late cleaning status cannot clear a prefix validated while STANDBY."""
    cached = _trajectory_state()
    cached.working_status = WorkingStatus.CLEANING
    cached.map_display_data = _trajectory_display(
        *((float(index), float(index)) for index in range(1, 31)),
        timestamp=100,
    )
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    payload = coordinator._map_display_cache_payload(cached)
    assert payload is not None

    coordinator.client = MagicMock()
    coordinator.client.state = _trajectory_state()
    coordinator.client.state.working_status = WorkingStatus.STANDBY
    coordinator.client.state.map_display_data = _trajectory_display(
        *((float(index), float(index)) for index in range(27, 57)),
        timestamp=200,
    )
    coordinator._map_display_cache_store = _FakeStore(payload)
    coordinator._map_display_cache_signature = ()
    coordinator._pending_map_display_cache_restore = None
    coordinator._map_display_cache_restored = False
    coordinator._map_display_cache_restored_from_active = False
    coordinator._retained_map_display = None
    coordinator._retained_map_identity = None
    coordinator._clean_session_active = False
    coordinator._clear_map_display_cache_for_new_clean = MagicMock()

    await coordinator._async_restore_map_display_cache()

    assert coordinator.client.state.map_display_data.trajectory_points() == [
        (float(index), float(index)) for index in range(1, 57)
    ]
    assert coordinator._pending_map_display_cache_snapshot is not None
    assert coordinator._pending_map_display_cache_snapshot.display.trajectory_points() == [
        (float(index), float(index)) for index in range(1, 57)
    ]
    assert coordinator._map_display_cache_restored_from_active
    persisted = coordinator._map_display_cache_payload(coordinator.client.state)
    assert persisted is not None
    assert persisted["active_clean"] is True

    restarted = NarwalCoordinator.__new__(NarwalCoordinator)
    restarted.client = MagicMock()
    restarted.client.state = NarwalState(working_status=WorkingStatus.CLEANING)
    restarted.client.state.map_data = cached.map_data
    restarted._map_display_cache_store = _FakeStore(persisted)
    restarted._map_display_cache_signature = ()
    restarted._pending_map_display_cache_restore = None
    restarted._map_display_cache_restored = False
    restarted._map_display_cache_restored_from_active = False

    await restarted._async_restore_map_display_cache()

    assert restarted.client.state.map_display_data is not None
    assert restarted._map_display_cache_restored_from_active

    coordinator.client.state.working_status = WorkingStatus.CLEANING
    coordinator._handle_working_status_transition(coordinator.client.state)

    coordinator._clear_map_display_cache_for_new_clean.assert_not_called()
    coordinator.client.state.map_display_data = _trajectory_display(
        *((float(index), float(index)) for index in range(53, 83)),
        timestamp=300,
    )
    coordinator._retain_native_trajectory(coordinator.client.state)

    assert coordinator.client.state.map_display_data.trajectory_points() == [
        (float(index), float(index)) for index in range(1, 83)
    ]
    assert not coordinator._map_display_cache_restored_from_active


@pytest.mark.parametrize(
    ("working_status", "dock_presence", "dock_field11", "expected_active"),
    (
        (WorkingStatus.STANDBY, 6, 2, False),
        (WorkingStatus.ERROR, 0, 0, False),
        (WorkingStatus.DOCKED_V2, 2, 0, True),
    ),
)
def test_restored_active_cache_uses_reconciled_terminal_state(
    working_status: WorkingStatus,
    dock_presence: int,
    dock_field11: int,
    expected_active: bool,
) -> None:
    """Dock evidence and explicit off-dock evidence override a bare status enum."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    state = _trajectory_state()
    state.working_status = working_status
    state.dock_presence = dock_presence
    state.dock_field11 = dock_field11
    state.last_active_working_status_time = time.monotonic()
    if working_status == WorkingStatus.STANDBY:
        state.last_terminal_working_status_time = time.monotonic()
    coordinator._map_display_cache_restored_from_active = True

    payload = coordinator._map_display_cache_payload(state)

    assert payload is not None
    assert payload["active_clean"] is expected_active


def test_off_dock_docked_v2_does_not_end_clean_session() -> None:
    """Explicit off-dock telemetry preserves continuity across a stale dock enum."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    state = _trajectory_state()
    state.working_status = WorkingStatus.DOCKED_V2
    state.dock_presence = 2
    state.last_active_working_status_time = time.monotonic()
    coordinator._clean_session_active = True
    coordinator._pending_map_display_cache_restore = None
    coordinator._map_display_cache_restored_from_active = False
    coordinator._clear_map_display_cache_for_new_clean = MagicMock()

    coordinator._handle_working_status_transition(state)
    state.working_status = WorkingStatus.CLEANING
    coordinator._handle_working_status_transition(state)

    assert coordinator._clean_session_active
    coordinator._clear_map_display_cache_for_new_clean.assert_not_called()


async def test_restore_map_display_cache_waits_for_static_map() -> None:
    """A saved trail is not restored until the active static map is known."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    source = _trajectory_state()
    coordinator.client = MagicMock()
    coordinator.client.state = NarwalState()
    coordinator._map_display_cache_store = _FakeStore(
        coordinator._map_display_cache_payload(source)
    )
    coordinator._map_display_cache_signature = ()
    coordinator._pending_map_display_cache_restore = None
    coordinator._map_display_cache_restored = False

    await coordinator._async_restore_map_display_cache()

    assert coordinator.client.state.map_display_data is None
    assert coordinator._pending_map_display_cache_restore is not None
    assert coordinator._map_display_cache_signature == ()
    assert not coordinator._map_display_cache_restored

    coordinator.client.state.map_data = source.map_data
    coordinator._restore_pending_map_display_cache()

    restored = coordinator.client.state.map_display_data
    assert restored is not None
    assert restored.trajectory_signature == (4, 4, 99)
    assert coordinator._pending_map_display_cache_restore is None
    assert coordinator._map_display_cache_restored


async def test_restore_map_display_cache_ignores_different_static_map() -> None:
    """A saved trail from another map must not be overlaid."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    source = _trajectory_state()
    coordinator.client = MagicMock()
    coordinator.client.state = NarwalState()
    coordinator.client.state.map_data = MapData(
        map_id=13,
        width=100,
        height=100,
        created_at=34,
        compressed_map=b"\x01",
    )
    coordinator._map_display_cache_store = _FakeStore(
        coordinator._map_display_cache_payload(source)
    )
    coordinator._map_display_cache_signature = ()
    coordinator._pending_map_display_cache_restore = None
    coordinator._map_display_cache_restored = False

    await coordinator._async_restore_map_display_cache()

    assert coordinator.client.state.map_display_data is None
    assert coordinator._map_display_cache_signature == ()
    assert not coordinator._map_display_cache_restored


async def test_restore_map_display_cache_rejects_missing_map_identity() -> None:
    """An unscoped persisted route cannot be assigned to an arbitrary map."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    source = _trajectory_state()
    payload = coordinator._map_display_cache_payload(source)
    assert payload is not None
    payload["map_id"] = 0
    payload["map_created_at"] = 0
    coordinator.client = MagicMock()
    coordinator.client.state = NarwalState()
    coordinator.client.state.map_data = source.map_data
    coordinator._map_display_cache_store = _FakeStore(payload)
    coordinator._map_display_cache_signature = ()
    coordinator._pending_map_display_cache_restore = None
    coordinator._map_display_cache_restored = False

    await coordinator._async_restore_map_display_cache()

    assert coordinator.client.state.map_display_data is None
    assert not coordinator._map_display_cache_restored


async def test_setup_retains_trajectory_received_with_initial_map() -> None:
    """The first native window participates in later rolling-window merges."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.hass = MagicMock()
    coordinator.config_entry = MagicMock()
    coordinator.client = MagicMock()
    coordinator.client.state = _trajectory_state()
    coordinator.client.state.map_display_data = _trajectory_display(
        *((float(index), float(index)) for index in range(1, 31)),
        timestamp=100,
    )
    coordinator.client.state.working_status = WorkingStatus.CLEANING
    coordinator.client.connect = AsyncMock()
    coordinator.client.get_device_info = AsyncMock()
    coordinator.client.get_status = AsyncMock(
        return_value=CommandResponse(
            data={"2": {"3": {"1": int(WorkingStatus.CLEANING)}}}
        )
    )
    coordinator.client.get_map = AsyncMock()
    async def get_consumable_info() -> None:
        coordinator.client.state.map_display_data = _trajectory_display(
            *((float(index), float(index)) for index in range(27, 57)),
            timestamp=200,
        )

    coordinator.client.get_consumable_info = AsyncMock(side_effect=get_consumable_info)
    coordinator.client.supports_broadcasts = False
    coordinator.client.robot_awake = True
    coordinator.client.start_listening = AsyncMock()
    coordinator._async_restore_room_selections = AsyncMock()
    coordinator._async_restore_map_display_cache = AsyncMock()
    coordinator._mark_dock_status_refresh_succeeded = MagicMock()
    coordinator._mark_dock_status_refresh_failed = MagicMock()
    coordinator._schedule_map_display_cache_save = MagicMock()
    coordinator.async_set_updated_data = MagicMock()
    coordinator._retained_map_display = None
    coordinator._retained_map_identity = None
    coordinator._map_display_cache_restored_from_active = False
    coordinator._map_display_cache_restored = False
    coordinator._pending_map_display_cache_restore = None
    coordinator._clean_session_active = False
    coordinator._prev_working_status = WorkingStatus.UNKNOWN
    coordinator._listen_task = None
    coordinator._fast_poll_remaining = 0
    coordinator.config_entry.async_create_background_task.side_effect = (
        lambda _hass, coro, _name: (coro.close(), MagicMock(done=lambda: False))[1]
    )

    await coordinator.async_setup()

    assert coordinator._retained_map_display.trajectory_points() == [
        (float(index), float(index)) for index in range(1, 57)
    ]
    coordinator._schedule_map_display_cache_save.assert_called_once_with(
        coordinator.client.state
    )


async def test_clear_map_display_cache_clears_memory_and_store() -> None:
    """Accepted clean starts clear both memory and persisted trail state."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.client = MagicMock()
    coordinator.client.state = _trajectory_state()
    coordinator._map_display_cache_store = _FakeStore({"old": "trail"})
    coordinator._pending_map_display_cache_snapshot = (
        coordinator._map_display_cache_snapshot(coordinator.client.state)
    )
    coordinator._pending_map_display_cache_restore = {"pending": "trail"}
    coordinator._map_display_cache_signature = (4, 4, 99)
    coordinator._map_display_cache_restored = True
    coordinator._map_display_cache_restored_from_active = True
    coordinator._map_display_cache_save_task = asyncio.create_task(asyncio.sleep(60))
    coordinator._map_display_cache_clear_event = asyncio.Event()
    coordinator._map_display_cache_clear_event.set()

    await coordinator.async_clear_map_display_cache()

    assert coordinator.client.state.map_display_data is None
    assert coordinator._pending_map_display_cache_snapshot is None
    assert coordinator._pending_map_display_cache_restore is None
    assert coordinator._map_display_cache_signature == ()
    assert not coordinator._map_display_cache_restored
    assert coordinator._map_display_cache_save_task is None
    assert coordinator._map_display_cache_store.saved == [{}]


async def test_clear_waits_for_uncancellable_storage_write() -> None:
    """A new-clean clear must land after an executor-backed stale save."""
    write_started = asyncio.Event()
    release_write = asyncio.Event()

    class ExecutorBackedStore(_FakeStore):
        async def async_save(self, data: object) -> None:
            if data != {}:
                async def finish_write() -> None:
                    write_started.set()
                    await release_write.wait()
                    self.saved.append(data)
                    self.data = data

                await asyncio.shield(asyncio.create_task(finish_write()))
                return
            await super().async_save(data)

    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.hass = MagicMock()
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.async_create_background_task.side_effect = (
        lambda _hass, coro, _name: asyncio.create_task(coro)
    )
    coordinator.client = MagicMock()
    coordinator.client.state = _trajectory_state()
    coordinator._map_display_cache_store = ExecutorBackedStore()
    coordinator._map_display_cache_signature = ()
    coordinator._map_display_cache_active_clean = None
    coordinator._map_display_cache_last_save = 0.0
    coordinator._pending_map_display_cache_snapshot = None
    coordinator._pending_map_display_cache_restore = None
    coordinator._map_display_cache_restored = False
    coordinator._map_display_cache_restored_from_active = False
    coordinator._map_display_cache_save_task = None
    coordinator._map_display_cache_clear_event = asyncio.Event()
    coordinator._map_display_cache_clear_event.set()

    coordinator._schedule_map_display_cache_save(
        coordinator.client.state,
        immediate=True,
    )
    save_task = coordinator._map_display_cache_save_task
    assert save_task is not None
    await write_started.wait()

    clear_task = asyncio.create_task(coordinator.async_clear_map_display_cache())
    await asyncio.sleep(0)
    save_task.cancel()
    await asyncio.sleep(0)
    assert not clear_task.done()

    release_write.set()
    await clear_task

    assert coordinator._map_display_cache_store.saved[-1] == {}


async def test_new_clean_clear_preserves_later_trajectory_snapshot() -> None:
    """A delayed clear cannot discard the first route window of a new clean."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.hass = MagicMock()
    coordinator.config_entry = MagicMock()
    coordinator.client = MagicMock()
    coordinator.client.state = _trajectory_state()
    coordinator._map_display_cache_store = _FakeStore({"old": "trail"})
    coordinator._map_display_cache_signature = (4, 4, 99)
    coordinator._map_display_cache_last_save = time.monotonic()
    coordinator._pending_map_display_cache_snapshot = (
        coordinator._map_display_cache_snapshot(coordinator.client.state)
    )
    coordinator._map_display_cache_save_task = asyncio.create_task(asyncio.sleep(60))
    coordinator._map_display_cache_clear_event = asyncio.Event()
    coordinator._map_display_cache_clear_event.set()
    background_tasks: list[asyncio.Task[None]] = []

    def create_background_task(
        _hass: object, coro: Coroutine[object, object, None], _name: str
    ) -> asyncio.Task[None]:
        task = asyncio.create_task(coro)
        background_tasks.append(task)
        return task

    coordinator.config_entry.async_create_background_task.side_effect = (
        create_background_task
    )

    coordinator._schedule_map_display_cache_clear(None)
    coordinator.client.state.map_display_data = _trajectory_display(
        (8.0, 8.0), (9.0, 9.0), timestamp=200
    )
    new_signature = coordinator.client.state.map_display_data.trajectory_signature
    coordinator._schedule_map_display_cache_save(
        coordinator.client.state, immediate=True
    )

    await background_tasks[0]
    await background_tasks[-1]

    assert coordinator._map_display_cache_store.saved[0] == {}
    assert coordinator._map_display_cache_store.saved[-1]["trajectory_signature"] == list(
        new_signature
    )
    assert coordinator._pending_map_display_cache_snapshot is None


async def test_overlapping_new_clean_clears_keep_route_writes_gated() -> None:
    """Concurrent command/status clears finish before a new route is saved."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.hass = MagicMock()
    coordinator.config_entry = MagicMock()
    coordinator.client = MagicMock()
    coordinator.client.state = _trajectory_state()
    coordinator._map_display_cache_store = _FakeStore({"old": "trail"})
    coordinator._map_display_cache_signature = (4, 4, 99)
    coordinator._map_display_cache_last_save = time.monotonic()
    coordinator._pending_map_display_cache_snapshot = None
    coordinator._map_display_cache_save_task = None
    background_tasks: list[asyncio.Task[None]] = []

    def create_background_task(
        _hass: object, coro: Coroutine[object, object, None], _name: str
    ) -> asyncio.Task[None]:
        task = asyncio.create_task(coro)
        background_tasks.append(task)
        return task

    coordinator.config_entry.async_create_background_task.side_effect = (
        create_background_task
    )

    coordinator._schedule_map_display_cache_clear(None)
    coordinator._schedule_map_display_cache_clear(None)
    coordinator.client.state.map_display_data = _trajectory_display(
        (8.0, 8.0), (9.0, 9.0), timestamp=200
    )
    new_signature = coordinator.client.state.map_display_data.trajectory_signature
    coordinator._schedule_map_display_cache_save(
        coordinator.client.state, immediate=True
    )

    await asyncio.gather(*background_tasks[:2])
    await background_tasks[-1]

    assert coordinator._map_display_cache_store.saved[:2] == [{}, {}]
    assert coordinator._map_display_cache_store.saved[-1]["trajectory_signature"] == list(
        new_signature
    )
    assert coordinator._map_display_cache_clear_count == 0
    assert coordinator._map_display_cache_clear_gate().is_set()


def test_reconnect_into_running_clean_keeps_restored_trail() -> None:
    """First cleaning update after restoring a cache is not a new clean start."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.client = MagicMock()
    coordinator.client.state = _trajectory_state()
    coordinator.client.last_display_map_age = float("inf")
    coordinator._prev_working_status = WorkingStatus.UNKNOWN
    coordinator._map_display_cache_restored = True
    coordinator._map_display_cache_restored_from_active = True

    state = coordinator.client.state
    state.update_from_working_status({"3": 42})

    assert not coordinator._is_new_clean_transition(state)


def test_stale_docked_startup_keeps_active_trail_for_overlap_validation() -> None:
    """A stale idle enum cannot clear an active cache before a native window."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator._prev_working_status = WorkingStatus.STANDBY
    coordinator._clean_session_active = False
    coordinator._map_display_cache_restored = True
    coordinator._map_display_cache_restored_from_active = True
    coordinator._map_display_cache_restored_at = 100.0
    coordinator._clear_map_display_cache_for_new_clean = MagicMock()
    state = _trajectory_state()
    state.working_status = WorkingStatus.DOCKED
    state.dock_presence = 6
    state.dock_field11 = 2

    with patch(
        "custom_components.narwal.coordinator.time.monotonic", return_value=101.0
    ):
        coordinator._handle_working_status_transition(state)
        payload = coordinator._map_display_cache_payload(state)

    coordinator._clear_map_display_cache_for_new_clean.assert_not_called()
    assert coordinator._map_display_cache_restored_from_active
    assert payload is not None
    assert payload["active_clean"] is True

    state.working_status = WorkingStatus.CLEANING
    state.dock_presence = 2
    state.dock_field11 = 1
    coordinator._handle_working_status_transition(state)

    coordinator._clear_map_display_cache_for_new_clean.assert_not_called()
    assert coordinator._clean_session_active


def test_restored_active_trail_validation_grace_expires() -> None:
    """Repeated terminal state revokes a restored-active marker after startup."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator._prev_working_status = WorkingStatus.UNKNOWN
    coordinator._clean_session_active = False
    coordinator._map_display_cache_restored = True
    coordinator._map_display_cache_restored_from_active = True
    coordinator._map_display_cache_restored_at = 100.0
    coordinator._pending_map_display_cache_restore = None
    coordinator._clear_map_display_cache_for_new_clean = MagicMock()
    state = NarwalState(working_status=WorkingStatus.DOCKED)
    state.dock_presence = 6

    with patch(
        "custom_components.narwal.coordinator.time.monotonic", return_value=161.0
    ):
        coordinator._handle_working_status_transition(state)

    assert not coordinator._map_display_cache_restored_from_active


@pytest.mark.parametrize(
    "working_status", (WorkingStatus.TASK_COMPLETED, WorkingStatus.ERROR)
)
def test_explicit_terminal_status_bypasses_active_trail_restore_grace(
    working_status: WorkingStatus,
) -> None:
    """Completion and fault packets end a restored session immediately."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator._prev_working_status = WorkingStatus.UNKNOWN
    coordinator._clean_session_active = False
    coordinator._map_display_cache_restored = True
    coordinator._map_display_cache_restored_from_active = True
    coordinator._map_display_cache_restored_at = 100.0
    coordinator._pending_map_display_cache_restore = None
    coordinator._clear_map_display_cache_for_new_clean = MagicMock()
    state = _trajectory_state()
    state.working_status = working_status

    with patch(
        "custom_components.narwal.coordinator.time.monotonic", return_value=101.0
    ):
        coordinator._handle_working_status_transition(state)
        payload = coordinator._map_display_cache_payload(state)

    assert not coordinator._map_display_cache_restored_from_active
    assert payload is not None
    assert payload["active_clean"] is False

    coordinator._handle_working_status_transition(
        NarwalState(working_status=WorkingStatus.CLEANING)
    )
    coordinator._clear_map_display_cache_for_new_clean.assert_called_once_with()


def test_confirmed_terminal_revokes_restored_active_cache_exemption() -> None:
    """A later clean cannot inherit a route after confirmed completion."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator._prev_working_status = WorkingStatus.CLEANING
    coordinator._clean_session_active = True
    coordinator._map_display_cache_restored = True
    coordinator._map_display_cache_restored_from_active = True
    coordinator._map_display_cache_active_clean = True
    coordinator._pending_map_display_cache_restore = None
    coordinator._clear_map_display_cache_for_new_clean = MagicMock()

    terminal = NarwalState(working_status=WorkingStatus.DOCKED_V2)
    terminal.dock_presence = 6
    coordinator._handle_working_status_transition(terminal)
    coordinator._handle_working_status_transition(
        NarwalState(working_status=WorkingStatus.CLEANING)
    )

    assert not coordinator._map_display_cache_restored_from_active
    coordinator._clear_map_display_cache_for_new_clean.assert_called_once_with()


def test_confirmed_terminal_revokes_pending_active_cache_without_map() -> None:
    """An unscoped pending route cannot exempt a later clean after docking."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator._prev_working_status = WorkingStatus.UNKNOWN
    coordinator._clean_session_active = False
    coordinator._map_display_cache_restored = False
    coordinator._map_display_cache_restored_from_active = False
    coordinator._map_display_cache_active_clean = None
    coordinator._pending_map_display_cache_restore = {
        "active_clean": True,
        "trajectory_x": "old",
    }
    coordinator._clear_map_display_cache_for_new_clean = MagicMock()

    terminal = NarwalState(working_status=WorkingStatus.DOCKED_V2)
    terminal.dock_presence = 6
    coordinator._handle_working_status_transition(terminal)
    coordinator._handle_working_status_transition(
        NarwalState(working_status=WorkingStatus.CLEANING)
    )

    assert coordinator._pending_map_display_cache_restore["active_clean"] is False
    coordinator._clear_map_display_cache_for_new_clean.assert_called_once_with()


def test_unknown_to_cleaning_clears_inactive_restored_trail() -> None:
    """A completed cached trail must not be treated as an active reconnect."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.client = MagicMock()
    coordinator.client.state = _trajectory_state()
    coordinator.client.last_display_map_age = float("inf")
    coordinator._prev_working_status = WorkingStatus.UNKNOWN
    coordinator._map_display_cache_restored = True
    coordinator._map_display_cache_restored_from_active = False

    state = coordinator.client.state
    state.update_from_working_status({"3": 42})

    assert coordinator._is_new_clean_transition(state)


def test_repeated_metric_only_updates_clear_trail_once() -> None:
    """A stale idle enum cannot reannounce one metric-only clean session."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator._prev_working_status = WorkingStatus.STANDBY
    coordinator._clean_session_active = False
    coordinator._map_display_cache_restored = False
    coordinator._map_display_cache_restored_from_active = False
    coordinator._clear_map_display_cache_for_new_clean = MagicMock()
    state = NarwalState(working_status=WorkingStatus.STANDBY)
    state.update_from_working_status({"3": 42})

    coordinator._handle_working_status_transition(state)
    coordinator._handle_working_status_transition(state)

    coordinator._clear_map_display_cache_for_new_clean.assert_called_once_with()
    assert coordinator._clean_session_active


def test_paused_standby_resume_keeps_current_trajectory() -> None:
    """An expired active-metric TTL while paused cannot split one clean."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator._prev_working_status = WorkingStatus.CLEANING
    coordinator._clean_session_active = True
    coordinator._map_display_cache_restored = False
    coordinator._map_display_cache_restored_from_active = False
    coordinator._clear_map_display_cache_for_new_clean = MagicMock()

    paused = NarwalState(working_status=WorkingStatus.STANDBY)
    paused.is_paused = True
    paused.task_progress_percent = 42
    paused.dock_presence = 6
    paused.dock_field11 = 2
    paused.dock_field47 = 3
    assert not paused.has_recent_active_working_status
    assert paused.has_paused_clean_task_context
    assert not is_confirmed_terminal_clean_state(paused)

    coordinator._handle_working_status_transition(paused)
    coordinator._handle_working_status_transition(
        NarwalState(working_status=WorkingStatus.CLEANING)
    )

    assert coordinator._clean_session_active
    coordinator._clear_map_display_cache_for_new_clean.assert_not_called()


def test_accepted_clean_latches_session_before_metric_handoff() -> None:
    """Early task metrics cannot clear a route already reset by an HA start."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator._prev_working_status = WorkingStatus.STANDBY
    coordinator._clean_session_active = False
    coordinator._map_display_cache_restored = False
    coordinator._map_display_cache_restored_from_active = False
    coordinator._clear_map_display_cache_for_new_clean = MagicMock()
    coordinator.active_clean_work_mode = None
    coordinator.active_room_clean_settings = {}

    coordinator.record_accepted_clean_start({4: RoomCleanSettings()})
    state = NarwalState(working_status=WorkingStatus.STANDBY)
    state.assume_robot_clean()
    state.update_from_working_status({"3": 1})
    coordinator._handle_working_status_transition(state)

    coordinator._clear_map_display_cache_for_new_clean.assert_not_called()
    assert coordinator._clean_session_active


def test_accepted_clean_latch_survives_pre_status_display_packet() -> None:
    """A cached dock status cannot end the accepted-start handoff."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator._prev_working_status = WorkingStatus.DOCKED_V2
    coordinator._clean_session_active = True
    coordinator._map_display_cache_restored = False
    coordinator._map_display_cache_restored_from_active = False
    coordinator._clear_map_display_cache_for_new_clean = MagicMock()
    state = NarwalState(working_status=WorkingStatus.DOCKED_V2)
    state.dock_presence = 6
    state.assume_robot_clean()

    coordinator._handle_working_status_transition(state)

    assert coordinator._clean_session_active
    coordinator._clear_map_display_cache_for_new_clean.assert_not_called()


def test_terminal_late_metrics_do_not_clear_completed_trail() -> None:
    """Late counters from a completed task cannot announce a new clean."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.client = MagicMock()
    coordinator._prev_working_status = WorkingStatus.TASK_COMPLETED
    coordinator._map_display_cache_restored = False
    coordinator._map_display_cache_restored_from_active = False
    state = NarwalState(working_status=WorkingStatus.TASK_COMPLETED)

    state.update_from_working_status({"3": 42})

    assert not state.has_recent_active_working_status
    assert not coordinator._is_new_clean_transition(state)


def test_assumed_clean_docked_handoff_is_not_terminal() -> None:
    """Cached docking cannot override an accepted-start reservation."""
    state = NarwalState(working_status=WorkingStatus.DOCKED_V2)
    state.dock_presence = 6
    state.assume_robot_clean()

    assert not is_confirmed_terminal_clean_state(state)


def test_returning_route_snapshot_remains_active() -> None:
    """Returning to dock remains part of the persisted cleaning session."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator._map_display_cache_restored_from_active = False
    state = _trajectory_state()
    state.working_status = WorkingStatus.CLEANING
    state.is_returning_to_dock = True
    state.dock_sub_state = 2

    snapshot = coordinator._map_display_cache_snapshot(state)

    assert snapshot is not None
    assert snapshot.active_clean


def test_remapping_transition_does_not_clear_completed_trail() -> None:
    """Map rebuilding is not a new cleaning session."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator._prev_working_status = WorkingStatus.STANDBY
    coordinator._clear_map_display_cache_for_new_clean = MagicMock()
    state = NarwalState(working_status=WorkingStatus.REMAPPING)

    coordinator._handle_working_status_transition(state)

    coordinator._clear_map_display_cache_for_new_clean.assert_not_called()
    assert coordinator._prev_working_status == WorkingStatus.REMAPPING


def test_remapping_to_cleaning_starts_a_new_retained_route() -> None:
    """A clean after map rebuilding must clear the previous cleaning trail."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator._prev_working_status = WorkingStatus.REMAPPING
    coordinator._map_display_cache_restored = False
    coordinator._map_display_cache_restored_from_active = False
    coordinator._clear_map_display_cache_for_new_clean = MagicMock()
    state = NarwalState(working_status=WorkingStatus.CLEANING)

    coordinator._handle_working_status_transition(state)

    coordinator._clear_map_display_cache_for_new_clean.assert_called_once_with()
    assert coordinator._prev_working_status == WorkingStatus.CLEANING


def test_idle_to_cleaning_transition_clears_stale_restored_trail() -> None:
    """A clean started while HA is running must drop the previous clean's trail."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.hass = MagicMock()
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.async_create_background_task = MagicMock()
    coordinator.client = MagicMock()
    coordinator.client.state = _trajectory_state()
    coordinator.client.last_display_map_age = float("inf")
    coordinator._map_display_cache_store = _FakeStore({"old": "trail"})
    coordinator._pending_map_display_cache_snapshot = None
    coordinator._pending_map_display_cache_restore = None
    coordinator._map_display_cache_signature = (4, 4, 99)
    coordinator._map_display_cache_restored = True
    coordinator._map_display_cache_restored_from_active = False
    coordinator._map_display_cache_save_task = None
    coordinator._prev_working_status = WorkingStatus.STANDBY
    coordinator.config_entry.async_create_background_task.side_effect = (
        _close_background_task
    )

    state = coordinator.client.state
    state.update_from_working_status({"3": 42})

    assert coordinator._is_new_clean_transition(state)
    coordinator._clear_map_display_cache_for_new_clean()

    assert state.map_display_data is None
    assert coordinator._map_display_cache_signature == ()
    coordinator.config_entry.async_create_background_task.assert_called_once()


def test_status_first_new_clean_drops_recent_previous_trajectory() -> None:
    """A recent old packet is not a new-clean packet when status arrives first."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.hass = MagicMock()
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.async_create_background_task = MagicMock(
        side_effect=_close_background_task
    )
    coordinator.client = MagicMock()
    coordinator.client.state = _trajectory_state()
    coordinator.client.last_display_map_age = 1.0
    coordinator._retained_map_display = coordinator.client.state.map_display_data
    coordinator._map_display_cache_store = _FakeStore({"old": "trail"})
    coordinator._pending_map_display_cache_snapshot = None
    coordinator._pending_map_display_cache_restore = None
    coordinator._map_display_cache_signature = (4, 4, 99)
    coordinator._map_display_cache_restored = True
    coordinator._map_display_cache_restored_from_active = False
    coordinator._map_display_cache_save_task = None
    coordinator._prev_working_status = WorkingStatus.STANDBY

    state = coordinator.client.state
    state.update_from_working_status({"3": 42})
    coordinator._clear_map_display_cache_for_new_clean()

    assert state.map_display_data is None
    assert coordinator._retained_map_display is None
    assert coordinator._pending_map_display_cache_snapshot is None


def test_new_clean_with_recent_inactive_trail_still_clears_cache() -> None:
    """Recency alone cannot distinguish a late old packet from a new route."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.hass = MagicMock()
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.async_create_background_task = MagicMock()
    coordinator.client = MagicMock()
    coordinator.client.state = _trajectory_state()
    coordinator.client.last_display_map_age = 1.0
    coordinator._map_display_cache_store = _FakeStore({"old": "trail"})
    coordinator._pending_map_display_cache_snapshot = None
    coordinator._pending_map_display_cache_restore = None
    coordinator._map_display_cache_signature = (1, 1, 1)
    coordinator._map_display_cache_restored = True
    coordinator._map_display_cache_restored_from_active = False
    coordinator._map_display_cache_save_task = None
    coordinator._map_display_cache_last_save = time.monotonic()
    coordinator.config_entry.async_create_background_task.side_effect = (
        _close_background_task
    )

    state = coordinator.client.state
    state.update_from_working_status({"3": 42})

    coordinator._clear_map_display_cache_for_new_clean()

    assert state.map_display_data is None
    assert coordinator._pending_map_display_cache_snapshot is None
    assert coordinator._map_display_cache_store.saved == []
    coordinator.config_entry.async_create_background_task.assert_called_once()


def test_schedule_map_display_cache_save_defers_serialization() -> None:
    """Scheduling cache persistence must not encode full routes in callbacks."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.hass = MagicMock()
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.async_create_background_task = MagicMock(
        side_effect=_close_background_task
    )
    coordinator._map_display_cache_signature = ()
    coordinator._pending_map_display_cache_snapshot = None
    coordinator._map_display_cache_save_task = None
    coordinator._map_display_cache_last_save = time.monotonic()

    with patch.object(
        NarwalCoordinator,
        "_map_display_cache_payload_from_snapshot",
        side_effect=AssertionError("serialization should be throttled"),
    ):
        coordinator._schedule_map_display_cache_save(_trajectory_state())

    assert coordinator._pending_map_display_cache_snapshot is not None
    coordinator.config_entry.async_create_background_task.assert_called_once()


async def test_schedule_map_display_cache_save_persists_terminal_metadata() -> None:
    """An unchanged completed route must replace its active cache marker."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.hass = MagicMock()
    coordinator.config_entry = MagicMock()
    coordinator._map_display_cache_store = _FakeStore()
    coordinator._map_display_cache_signature = (4, 4, 99)
    coordinator._map_display_cache_active_clean = True
    coordinator._pending_map_display_cache_snapshot = None
    coordinator._map_display_cache_save_task = None
    coordinator._map_display_cache_last_save = 0.0
    background_tasks: list[asyncio.Task[None]] = []

    def create_background_task(
        _hass: object, coro: Coroutine[object, object, None], _name: str
    ) -> asyncio.Task[None]:
        task = asyncio.create_task(coro)
        background_tasks.append(task)
        return task

    coordinator.config_entry.async_create_background_task.side_effect = (
        create_background_task
    )

    coordinator._schedule_map_display_cache_save(
        _trajectory_state(), immediate=True
    )
    await background_tasks[0]

    assert coordinator._map_display_cache_store.saved[-1]["active_clean"] is False
    assert coordinator._map_display_cache_active_clean is False


async def test_shutdown_flushes_current_display_map_cache() -> None:
    """HA shutdown should persist the newest trail even when no save is queued."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.client = MagicMock()
    coordinator.client.state = _trajectory_state()
    coordinator._map_display_cache_store = _FakeStore()
    coordinator._pending_map_display_cache_snapshot = None
    coordinator._pending_map_display_cache_restore = None
    coordinator._map_display_cache_save_task = None
    coordinator._map_display_cache_signature = ()
    coordinator._map_display_cache_last_save = 0.0

    await coordinator._async_flush_map_display_cache()

    assert coordinator._map_display_cache_store.saved
    assert coordinator._map_display_cache_store.saved[-1]["trajectory_signature"] == [
        4,
        4,
        99,
    ]
    assert coordinator._map_display_cache_signature == (4, 4, 99)


async def test_shutdown_does_not_replace_scoped_cache_with_pre_map_route() -> None:
    """A failed map fetch cannot make an existing route unrestorable."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    scoped_state = _trajectory_state()
    existing = coordinator._map_display_cache_payload(scoped_state)
    assert existing is not None
    coordinator.client = MagicMock()
    coordinator.client.state = _trajectory_state()
    coordinator.client.state.map_data = None
    coordinator._map_display_cache_store = _FakeStore(existing)
    coordinator._pending_map_display_cache_snapshot = None
    coordinator._pending_map_display_cache_restore = None
    coordinator._map_display_cache_save_task = None
    coordinator._map_display_cache_signature = (4, 4, 99)
    coordinator._map_display_cache_last_save = 0.0

    await coordinator._async_flush_map_display_cache()

    assert coordinator._map_display_cache_store.data == existing
    assert coordinator._map_display_cache_store.saved == []


async def test_shutdown_retries_failed_trajectory_cache_clear() -> None:
    """A failed clear remains pending when no replacement route arrives."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.client = MagicMock()
    coordinator.client.state = NarwalState()
    coordinator._map_display_cache_store = MagicMock()
    coordinator._map_display_cache_store.async_save = AsyncMock(
        side_effect=[OSError, None]
    )
    coordinator._pending_map_display_cache_snapshot = None
    coordinator._pending_map_display_cache_restore = None
    coordinator._map_display_cache_save_task = None
    coordinator._map_display_cache_signature = (4, 4, 99)
    coordinator._map_display_cache_last_save = 0.0
    coordinator._map_display_cache_clear_event = asyncio.Event()
    coordinator._map_display_cache_clear_event.set()
    coordinator._map_display_cache_clear_lock = asyncio.Lock()
    coordinator._map_display_cache_write_lock = asyncio.Lock()
    coordinator._map_display_cache_clear_count = 0

    await coordinator.async_clear_map_display_cache()

    assert coordinator._map_display_cache_clear_pending
    coordinator.client.state.map_display_data = _trajectory_display(
        (1.0, 1.0), (2.0, 2.0), timestamp=200
    )
    await coordinator._async_flush_map_display_cache()

    assert coordinator._map_display_cache_store.async_save.await_args_list == [
        call({}),
        call({}),
    ]
    assert not coordinator._map_display_cache_clear_pending


async def test_shutdown_flush_waits_for_pending_cache_clear() -> None:
    """A delayed clear cannot overwrite the route persisted during shutdown."""
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.hass = MagicMock()
    coordinator.config_entry = MagicMock()
    coordinator.client = MagicMock()
    coordinator.client.state = _trajectory_state()
    coordinator._map_display_cache_store = _FakeStore({"old": "trail"})
    coordinator._pending_map_display_cache_snapshot = None
    coordinator._pending_map_display_cache_restore = None
    coordinator._map_display_cache_save_task = None
    coordinator._map_display_cache_signature = ()
    coordinator._map_display_cache_last_save = 0.0
    clear_started = asyncio.Event()
    release_clear = asyncio.Event()
    original_save = coordinator._map_display_cache_store.async_save

    async def delayed_save(data: object) -> None:
        if data == {}:
            clear_started.set()
            await release_clear.wait()
        await original_save(data)

    coordinator._map_display_cache_store.async_save = delayed_save
    background_tasks: list[asyncio.Task[None]] = []

    def create_background_task(
        _hass: object, coro: Coroutine[object, object, None], _name: str
    ) -> asyncio.Task[None]:
        task = asyncio.create_task(coro)
        background_tasks.append(task)
        return task

    coordinator.config_entry.async_create_background_task.side_effect = (
        create_background_task
    )

    coordinator._schedule_map_display_cache_clear(None)
    await clear_started.wait()
    flush_task = asyncio.create_task(coordinator._async_flush_map_display_cache())
    await asyncio.sleep(0)

    assert not flush_task.done()

    release_clear.set()
    await asyncio.gather(background_tasks[0], flush_task)

    assert coordinator._map_display_cache_store.saved[0] == {}
    assert coordinator._map_display_cache_store.saved[-1]["trajectory_signature"] == [
        4,
        4,
        99,
    ]


class TestCoordinatorResilience:
    """Tests for NarwalCoordinator failure buffering and availability."""

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
        # Initialize the attributes that __init__ sets, bypassing
        # DataUpdateCoordinator.__init__ which needs a real hass.
        coordinator.hass = mock_hass
        coordinator.config_entry = mock_entry
        coordinator.client = MagicMock()
        coordinator.client.state = NarwalState()
        coordinator.last_update_success = True
        coordinator._consecutive_failures = 0
        coordinator._dock_status_refresh_failed = False
        coordinator._max_failures = 5
        coordinator._consumable_poll_countdown = 99  # don't fire consumable poll in unit tests
        coordinator._fast_poll_remaining = 0
        coordinator._listen_task = None
        coordinator._map_fetch_pending = False
        coordinator._last_display_map_resub = 0.0
        # Fresh subscription so renewal does not fire in unrelated tests.
        coordinator._last_topic_subscribe = time.monotonic()
        coordinator._prev_working_status = MagicMock()
        coordinator.active_clean_work_mode = None
        coordinator.active_room_clean_settings = {}
        coordinator._map_display_cache_store = _FakeStore()
        coordinator._map_display_cache_signature = ()
        coordinator._pending_map_display_cache_snapshot = None
        coordinator._pending_map_display_cache_restore = None
        coordinator._map_display_cache_save_task = None
        coordinator._map_display_cache_last_save = 0.0
        coordinator._map_display_cache_restored = False
        coordinator.update_interval = None
        def _close_background_task(*args: object) -> None:
            for arg in args:
                if hasattr(arg, "close"):
                    arg.close()
        mock_entry.async_create_background_task = MagicMock(
            side_effect=_close_background_task
        )
        return coordinator

    async def test_stale_data_on_first_failure(self) -> None:
        """_async_update_data returns stale state on first poll failure."""
        coordinator = self._make_coordinator()
        type(coordinator.client).connected = PropertyMock(return_value=False)

        result = await coordinator._async_update_data()

        assert result is coordinator.client.state
        assert coordinator._consecutive_failures == 1

    async def test_poll_retries_failed_room_store_restore(self) -> None:
        """Polling retries a transient Store read failure without user action."""
        coordinator = self._make_coordinator()
        coordinator._room_selection_store = MagicMock()
        coordinator._room_selection_store.async_load = AsyncMock(
            side_effect=[OSError, None]
        )
        coordinator._room_selection_save_lock = asyncio.Lock()
        coordinator._room_selection_store_loaded = False
        coordinator._room_profile_store_loaded = False
        type(coordinator.client).connected = PropertyMock(return_value=False)

        await coordinator._async_restore_room_selections()
        assert not coordinator._room_selection_store_loaded
        assert not coordinator._room_profile_store_loaded

        result = await coordinator._async_update_data()

        assert result is coordinator.client.state
        assert coordinator._room_selection_store_loaded
        assert coordinator._room_profile_store_loaded
        assert coordinator._room_selection_store.async_load.await_count == 2

    async def test_poll_restore_is_serialized_with_room_store_save(self) -> None:
        """A retry cannot apply an old read after a concurrent save."""
        coordinator = self._make_coordinator()
        coordinator.selected_clean_rooms = {}
        coordinator.room_clean_settings = {}
        coordinator.room_clean_settings_customized = {}
        coordinator._room_selection_dirty_maps = set()
        coordinator._room_profile_pending_resolution = set()
        coordinator._room_selection_save_lock = asyncio.Lock()
        coordinator._room_selection_store_loaded = False
        coordinator._room_profile_store_loaded = False
        coordinator._schedule_room_selection_save = MagicMock()
        type(coordinator.client).connected = PropertyMock(return_value=False)
        read_started = asyncio.Event()
        release_read = asyncio.Event()

        async def delayed_load() -> object:
            read_started.set()
            await release_read.wait()
            return {
                "maps": [{"map_id": "upstairs", "room_ids": [4]}],
                "profiles": [],
            }

        coordinator._room_selection_store = MagicMock()
        coordinator._room_selection_store.async_load = AsyncMock(
            side_effect=delayed_load
        )
        coordinator._room_selection_store.async_save = AsyncMock()

        poll_task = asyncio.create_task(coordinator._async_update_data())
        await read_started.wait()
        coordinator.set_room_selected_for_clean(5, True, map_id="upstairs")
        save_task = asyncio.create_task(coordinator._async_save_room_selections())
        release_read.set()
        await poll_task
        await save_task

        assert coordinator.selected_clean_rooms == {"upstairs": {5}}
        coordinator._room_selection_store.async_save.assert_awaited_once()
        saved = coordinator._room_selection_store.async_save.await_args.args[0]
        assert saved["maps"] == [{"map_id": "upstairs", "room_ids": [5]}]

    async def test_stale_data_on_consecutive_failures_below_threshold(self) -> None:
        """_async_update_data returns stale state for failures 1-4."""
        coordinator = self._make_coordinator()
        type(coordinator.client).connected = PropertyMock(return_value=False)

        for i in range(4):
            result = await coordinator._async_update_data()
            assert result is coordinator.client.state
            assert coordinator._consecutive_failures == i + 1

    async def test_update_failed_after_max_failures(self) -> None:
        """_async_update_data raises UpdateFailed after 5 consecutive failures."""
        coordinator = self._make_coordinator()
        type(coordinator.client).connected = PropertyMock(return_value=False)

        # Burn through 4 failures (stale data returned)
        for _ in range(4):
            await coordinator._async_update_data()

        # 5th failure raises UpdateFailed
        with pytest.raises(UpdateFailed, match="5 consecutive polls"):
            await coordinator._async_update_data()

        assert coordinator._consecutive_failures == 5

    async def test_success_resets_failure_counter(self) -> None:
        """_async_update_data resets _consecutive_failures to 0 on success."""
        coordinator = self._make_coordinator()

        # Simulate 3 failures first
        type(coordinator.client).connected = PropertyMock(return_value=False)
        for _ in range(3):
            await coordinator._async_update_data()
        assert coordinator._consecutive_failures == 3

        # Now succeed
        type(coordinator.client).connected = PropertyMock(return_value=True)
        coordinator.client.get_status = AsyncMock(
            return_value=CommandResponse(data={"2": {"3": {"1": 10}}})
        )

        result = await coordinator._async_update_data()

        assert coordinator._consecutive_failures == 0
        assert result is coordinator.client.state

    async def test_terminal_poll_refreshes_queued_cache_metadata(self) -> None:
        """A graceful shutdown cannot flush stale active metadata after completion."""
        coordinator = self._make_coordinator()
        state = _trajectory_state()
        state.working_status = WorkingStatus.CLEANING
        coordinator.client.state = state
        coordinator._map_display_cache_restored_from_active = False
        coordinator._retained_map_display = state.map_display_data
        coordinator._retained_map_identity = (12, 34)
        coordinator._map_display_cache_active_clean = True
        coordinator._pending_map_display_cache_snapshot = (
            coordinator._map_display_cache_snapshot(state)
        )
        assert coordinator._pending_map_display_cache_snapshot is not None
        assert coordinator._pending_map_display_cache_snapshot.active_clean
        type(coordinator.client).connected = PropertyMock(return_value=True)

        async def get_status(*, full_update: bool) -> CommandResponse:
            assert full_update
            state.working_status = WorkingStatus.TASK_COMPLETED
            return CommandResponse(
                data={
                    "2": {
                        "3": {"1": int(WorkingStatus.TASK_COMPLETED), "3": 6},
                        "11": 2,
                    }
                }
            )

        coordinator.client.get_status = AsyncMock(side_effect=get_status)

        await coordinator._async_update_data()

        assert coordinator._pending_map_display_cache_snapshot is not None
        assert not coordinator._pending_map_display_cache_snapshot.active_clean
        assert coordinator._map_display_cache_active_clean is True

        await coordinator._async_save_pending_map_display_cache(0)

        assert coordinator._map_display_cache_store.saved[-1]["active_clean"] is False
        assert coordinator._map_display_cache_active_clean is False

    async def test_poll_map_fetch_restores_pending_display_cache(self) -> None:
        """A fallback map fetch restores a route that was waiting for its map."""
        coordinator = self._make_coordinator()
        source = _trajectory_state()
        payload = coordinator._map_display_cache_payload(source)
        assert payload is not None

        type(coordinator.client).connected = PropertyMock(return_value=True)
        coordinator.client.supports_broadcasts = False
        coordinator.client.get_status = AsyncMock(
            return_value=CommandResponse(data={"2": {"3": {"1": 10}}})
        )
        coordinator.client.state.map_data = None
        coordinator._pending_map_display_cache_restore = payload

        async def get_map() -> None:
            coordinator.client.state.map_data = source.map_data

        coordinator.client.get_map = AsyncMock(side_effect=get_map)

        await coordinator._async_update_data()

        restored = coordinator.client.state.map_display_data
        assert restored is not None
        assert restored.trajectory_signature == (4, 4, 99)
        assert coordinator._pending_map_display_cache_restore is None
        assert coordinator._map_display_cache_restored

    async def test_poll_restores_pending_active_route_before_clean_transition(self) -> None:
        """Status recovery cannot erase an active route waiting for its map."""
        coordinator = self._make_coordinator()
        source = _trajectory_state()
        source.working_status = WorkingStatus.CLEANING
        payload = coordinator._map_display_cache_payload(source)
        assert payload is not None
        assert payload["active_clean"] is True

        type(coordinator.client).connected = PropertyMock(return_value=True)
        coordinator.client.supports_broadcasts = False
        coordinator.client.state.map_data = None
        coordinator._pending_map_display_cache_restore = payload
        coordinator._clean_session_active = False

        async def get_status(*, full_update: bool) -> CommandResponse:
            coordinator.client.state.working_status = WorkingStatus.CLEANING
            return CommandResponse(
                data={"2": {"3": {"1": int(WorkingStatus.CLEANING)}}}
            )

        async def get_map() -> None:
            coordinator.client.state.map_data = source.map_data

        coordinator.client.get_status = AsyncMock(side_effect=get_status)
        coordinator.client.get_map = AsyncMock(side_effect=get_map)

        await coordinator._async_update_data()

        assert coordinator.client.state.map_display_data is not None
        assert coordinator._map_display_cache_restored_from_active
        assert coordinator._pending_map_display_cache_restore is None

    async def test_active_pending_route_survives_push_and_failed_map_retry(self) -> None:
        """Push recovery cannot erase an active cache before its map is known."""
        coordinator = self._make_coordinator()
        source = _trajectory_state()
        source.working_status = WorkingStatus.CLEANING
        payload = coordinator._map_display_cache_payload(source)
        assert payload is not None
        assert payload["active_clean"] is True

        coordinator.client.state.map_data = None
        coordinator.client.state.working_status = WorkingStatus.CLEANING
        coordinator.client.last_display_map_age = 0.0
        coordinator._pending_map_display_cache_restore = payload
        coordinator._clean_session_active = False
        coordinator._map_fetch_pending = True
        coordinator._clear_map_display_cache_for_new_clean = MagicMock()
        coordinator.async_set_updated_data = MagicMock()

        coordinator._on_state_update(coordinator.client.state)

        coordinator._clear_map_display_cache_for_new_clean.assert_not_called()
        assert coordinator._pending_map_display_cache_restore == payload

        coordinator.client.get_map = AsyncMock(side_effect=OSError)
        await coordinator._fetch_missing_map()

        coordinator._clear_map_display_cache_for_new_clean.assert_not_called()
        assert coordinator._pending_map_display_cache_restore == payload

    async def test_poll_map_fetch_scopes_pending_live_snapshot(self) -> None:
        """A delayed static map scopes a live snapshot queued before the fetch."""
        coordinator = self._make_coordinator()
        source = _trajectory_state()
        coordinator.client.state.map_display_data = source.map_display_data
        snapshot = coordinator._map_display_cache_snapshot(coordinator.client.state)
        assert snapshot is not None
        assert snapshot.map_id == 0
        coordinator._pending_map_display_cache_snapshot = snapshot

        type(coordinator.client).connected = PropertyMock(return_value=True)
        coordinator.client.supports_broadcasts = False
        coordinator.client.get_status = AsyncMock(
            return_value=CommandResponse(data={"2": {"3": {"1": 10}}})
        )

        async def get_map() -> None:
            coordinator.client.state.map_data = source.map_data

        coordinator.client.get_map = AsyncMock(side_effect=get_map)

        await coordinator._async_update_data()

        scoped = coordinator._pending_map_display_cache_snapshot
        assert scoped is not None
        assert (scoped.map_id, scoped.map_created_at) == (12, 34)

    async def test_background_map_fetch_scopes_pending_live_snapshot(self) -> None:
        """The push-triggered map fetch also scopes a pre-map live snapshot."""
        coordinator = self._make_coordinator()
        source = _trajectory_state()
        coordinator.client.state.map_display_data = source.map_display_data
        snapshot = coordinator._map_display_cache_snapshot(coordinator.client.state)
        assert snapshot is not None
        assert snapshot.map_id == 0
        coordinator._pending_map_display_cache_snapshot = snapshot
        coordinator.client.supports_broadcasts = False
        coordinator.async_set_updated_data = MagicMock()

        async def get_map() -> None:
            coordinator.client.state.map_data = source.map_data

        coordinator.client.get_map = AsyncMock(side_effect=get_map)

        await coordinator._fetch_missing_map()

        scoped = coordinator._pending_map_display_cache_snapshot
        assert scoped is not None
        assert (scoped.map_id, scoped.map_created_at) == (12, 34)

    async def test_poll_preserves_recent_active_working_status(self) -> None:
        """Poll only refreshes hardware fields while task metrics are fresh."""
        coordinator = self._make_coordinator()
        type(coordinator.client).connected = PropertyMock(return_value=True)
        coordinator.client.state.update_from_working_status({"3": 120})
        coordinator.client.get_status = AsyncMock()

        result = await coordinator._async_update_data()

        coordinator.client.get_status.assert_awaited_once_with(full_update=False)
        assert result is coordinator.client.state

    async def test_poll_clean_transition_clears_stale_restored_trail(self) -> None:
        """Polling must clear a previous clean's trail when push updates are absent."""
        coordinator = self._make_coordinator()
        type(coordinator.client).connected = PropertyMock(return_value=True)
        coordinator.client.state = _trajectory_state()
        coordinator.client.state.working_status = WorkingStatus.STANDBY
        coordinator.client.last_display_map_age = float("inf")
        coordinator._prev_working_status = WorkingStatus.STANDBY
        coordinator._map_display_cache_signature = (4, 4, 99)
        coordinator._map_display_cache_restored = True
        coordinator._map_display_cache_restored_from_active = False

        async def get_status(*, full_update: bool) -> CommandResponse:
            coordinator.client.state.update_from_base_status(
                {"3": {"1": int(WorkingStatus.CLEANING)}}
            )
            return CommandResponse(
                data={"2": {"3": {"1": int(WorkingStatus.CLEANING)}}}
            )

        coordinator.client.get_status = AsyncMock(side_effect=get_status)

        result = await coordinator._async_update_data()

        coordinator.client.get_status.assert_awaited_once_with(full_update=True)
        assert result is coordinator.client.state
        assert result.map_display_data is None
        assert coordinator._map_display_cache_signature == ()
        assert coordinator._prev_working_status == WorkingStatus.CLEANING
        coordinator.config_entry.async_create_background_task.assert_called_once()

    async def test_push_update_resets_failure_counter(self) -> None:
        """_on_state_update resets _consecutive_failures to 0."""
        coordinator = self._make_coordinator()
        coordinator._consecutive_failures = 3
        coordinator._dock_status_refresh_failed = True

        # Mock methods called by _on_state_update
        coordinator.async_set_updated_data = MagicMock()
        coordinator._prev_working_status = MagicMock()

        state = NarwalState()
        coordinator._on_state_update(state)

        assert coordinator._consecutive_failures == 0
        assert not coordinator.has_fresh_state

    async def test_idle_push_clears_active_clean_work_mode(self) -> None:
        """Accepted-task mode metadata is only kept for active clean contexts."""
        coordinator = self._make_coordinator()
        coordinator.active_clean_work_mode = WorkMode.MOP
        coordinator.active_room_clean_settings = {4: RoomCleanSettings()}
        coordinator.async_set_updated_data = MagicMock()
        coordinator._prev_working_status = WorkingStatus.CLEANING
        state = NarwalState()
        state.update_from_base_status({"3": {"1": int(WorkingStatus.DOCKED), "3": 6}})

        coordinator._on_state_update(state)

        assert coordinator.active_clean_work_mode is None
        assert coordinator.active_room_clean_settings == {}

    async def test_poll_does_not_call_connect(self) -> None:
        """_async_update_data does NOT call client.connect() when disconnected."""
        coordinator = self._make_coordinator()
        type(coordinator.client).connected = PropertyMock(return_value=False)
        coordinator.client.connect = AsyncMock()

        # Run a few poll failures
        for _ in range(3):
            await coordinator._async_update_data()

        coordinator.client.connect.assert_not_awaited()

    async def test_connected_but_get_status_fails(self) -> None:
        """_async_update_data buffers failure when connected but get_status raises."""
        coordinator = self._make_coordinator()
        type(coordinator.client).connected = PropertyMock(return_value=True)
        coordinator.client.get_status = AsyncMock(
            side_effect=NarwalConnectionError("recv timeout")
        )

        result = await coordinator._async_update_data()

        assert result is coordinator.client.state
        assert coordinator._consecutive_failures == 1
        assert not coordinator.has_fresh_state

    async def test_payloadless_status_poll_counts_as_failed_refresh(self) -> None:
        """A status ack without base-status data must not mark stale data fresh."""
        coordinator = self._make_coordinator()
        type(coordinator.client).connected = PropertyMock(return_value=True)
        coordinator.client.get_status = AsyncMock(
            return_value=CommandResponse(
                result_code=CommandResult.NOT_READY,
                data={"1": 1},
            )
        )

        result = await coordinator._async_update_data()

        assert result is coordinator.client.state
        assert coordinator._consecutive_failures == 1

    async def test_partial_status_poll_only_marks_dock_state_stale(self) -> None:
        """A battery-only response proves connectivity, but not dock freshness."""
        coordinator = self._make_coordinator()
        type(coordinator.client).connected = PropertyMock(return_value=True)
        coordinator.client.get_status = AsyncMock(
            return_value=CommandResponse(data={"2": {"2": 85.0}})
        )

        result = await coordinator._async_update_data()

        assert result is coordinator.client.state
        assert coordinator._consecutive_failures == 0
        assert not coordinator.has_fresh_state

    async def test_refresh_dock_status_rejects_missing_base_status_payload(self) -> None:
        """Dock command gates require fresh base-status data, not only an ack."""
        coordinator = self._make_coordinator()
        coordinator.client.get_status = AsyncMock(return_value=CommandResponse(data={}))
        coordinator.async_set_updated_data = MagicMock()

        assert not await coordinator.async_refresh_dock_status()

        coordinator.client.get_status.assert_awaited_once_with(full_update=True)
        coordinator.async_set_updated_data.assert_called_once_with(
            coordinator.client.state
        )

    async def test_refresh_dock_status_rejects_rejected_status_response(self) -> None:
        """Rejected status responses must not unlock dock controls."""
        coordinator = self._make_coordinator()
        coordinator.client.get_status = AsyncMock(
            return_value=CommandResponse(
                result_code=CommandResult.NOT_APPLICABLE,
                data={"2": {"3": {"1": 10}}},
            )
        )
        coordinator.async_set_updated_data = MagicMock()

        assert not await coordinator.async_refresh_dock_status()

    async def test_refresh_dock_status_rejects_partial_base_status_payload(self) -> None:
        """Dock command gates require field 3, not just any base-status field."""
        coordinator = self._make_coordinator()
        coordinator.client.get_status = AsyncMock(
            return_value=CommandResponse(data={"2": {"2": 85.0}})
        )
        coordinator.async_set_updated_data = MagicMock()

        assert not await coordinator.async_refresh_dock_status()

    async def test_refresh_dock_status_rejects_empty_dock_status_payload(self) -> None:
        """Dock command gates require real status subfields inside field 3."""
        coordinator = self._make_coordinator()
        coordinator.client.get_status = AsyncMock(
            return_value=CommandResponse(data={"2": {"3": {}}})
        )
        coordinator.async_set_updated_data = MagicMock()

        assert not await coordinator.async_refresh_dock_status()

    async def test_refresh_dock_status_marks_fresh_before_notifying(self) -> None:
        """Listeners see fresh dock availability on the successful refresh update."""
        coordinator = self._make_coordinator()
        coordinator._dock_status_refresh_failed = True
        coordinator.client.get_status = AsyncMock(
            return_value=CommandResponse(
                data={"2": {"3": {"1": int(WorkingStatus.DOCKED)}, "11": 2}}
            )
        )
        seen: list[bool] = []

        def capture_update(_state):
            seen.append(coordinator.has_fresh_state)

        coordinator.async_set_updated_data = MagicMock(side_effect=capture_update)

        assert await coordinator.async_refresh_dock_status()
        assert seen == [True]

    async def test_refresh_dock_status_refreshes_queued_cache_metadata(self) -> None:
        """A direct terminal refresh replaces queued active route metadata."""
        coordinator = self._make_coordinator()
        state = _trajectory_state()
        state.working_status = WorkingStatus.CLEANING
        coordinator.client.state = state
        coordinator._map_display_cache_restored_from_active = False
        coordinator._retained_map_display = state.map_display_data
        coordinator._retained_map_identity = (12, 34)
        coordinator._map_display_cache_active_clean = True
        coordinator._pending_map_display_cache_snapshot = (
            coordinator._map_display_cache_snapshot(state)
        )

        async def get_status(*, full_update: bool) -> CommandResponse:
            assert full_update
            state.update_from_base_status(
                {
                    "3": {
                        "1": int(WorkingStatus.DOCKED),
                        "3": 6,
                        "10": 1,
                    },
                    "11": 2,
                }
            )
            return CommandResponse(
                data={
                    "2": {
                        "3": {
                            "1": int(WorkingStatus.DOCKED),
                            "3": 6,
                            "10": 1,
                        },
                        "11": 2,
                    }
                }
            )

        coordinator.client.get_status = AsyncMock(side_effect=get_status)
        coordinator.async_set_updated_data = MagicMock()

        assert await coordinator.async_refresh_dock_status()
        assert coordinator._pending_map_display_cache_snapshot is not None
        assert not coordinator._pending_map_display_cache_snapshot.active_clean

    async def test_refresh_dock_status_preserves_live_working_status(self) -> None:
        """Action preflight must not clobber fresh working_status task telemetry."""
        coordinator = self._make_coordinator()
        coordinator.client.state.update_from_working_status({"3": 42})
        coordinator.client.get_status = AsyncMock(
            return_value=CommandResponse(data={"2": {"2": 85.0}})
        )
        coordinator.async_set_updated_data = MagicMock()

        assert await coordinator.async_refresh_dock_status()

        coordinator.client.get_status.assert_awaited_once_with(full_update=False)
        assert not coordinator._dock_status_refresh_failed

    async def test_refresh_dock_status_marks_stale_before_notifying(self) -> None:
        """Listeners see stale dock availability on the failed refresh update."""
        coordinator = self._make_coordinator()
        coordinator._dock_status_refresh_failed = False
        coordinator.client.get_status = AsyncMock(
            return_value=CommandResponse(data={"2": {"2": 85.0}})
        )
        seen: list[bool] = []

        def capture_update(_state):
            seen.append(coordinator.has_fresh_state)

        coordinator.async_set_updated_data = MagicMock(side_effect=capture_update)

        assert not await coordinator.async_refresh_dock_status()
        assert seen == [False]

    async def test_prepare_clean_start_stops_single_safe_dock_blocker(self) -> None:
        """A clean-start intent clears one known safe dock blocker first."""
        coordinator = self._make_coordinator()
        state = _docked_state()
        state.station_activity = 1
        coordinator.client.state = state
        coordinator.data = state
        coordinator.async_refresh_dock_status = AsyncMock(return_value=True)
        coordinator.async_set_updated_data = MagicMock()

        async def stop_task(task: str | None = None) -> CommandResponse:
            assert task == DOCK_TASK_EMPTY_DUSTBIN
            state.station_activity = 0
            return CommandResponse(result_code=CommandResult.SUCCESS)

        coordinator.client.stop_dock_task = AsyncMock(side_effect=stop_task)

        assert can_prepare_clean_start(state)
        assert await coordinator.async_prepare_clean_start()

        coordinator.client.stop_dock_task.assert_awaited_once_with(
            DOCK_TASK_EMPTY_DUSTBIN
        )
        assert coordinator.async_refresh_dock_status.await_count == 2
        assert can_start_cleaning(state)

    async def test_prepare_clean_start_rejects_failed_initial_refresh(self) -> None:
        """Preparation does not act when its initial state refresh fails."""
        coordinator = self._make_coordinator()
        coordinator.async_refresh_dock_status = AsyncMock(return_value=False)
        coordinator.client.stop_dock_task = AsyncMock()

        assert not await coordinator.async_prepare_clean_start()

        coordinator.async_refresh_dock_status.assert_awaited_once()
        coordinator.client.stop_dock_task.assert_not_awaited()

    async def test_prepare_clean_start_rejects_failed_dock_stop(self) -> None:
        """Preparation does not start after the dock rejects its required stop."""
        coordinator = self._make_coordinator()
        state = _docked_state()
        state.station_activity = 1
        coordinator.client.state = state
        coordinator.data = state
        coordinator.async_refresh_dock_status = AsyncMock(return_value=True)
        coordinator.client.stop_dock_task = AsyncMock(
            return_value=CommandResponse(result_code=CommandResult.CONFLICT)
        )

        assert not await coordinator.async_prepare_clean_start()

        coordinator.async_refresh_dock_status.assert_awaited_once()
        coordinator.client.stop_dock_task.assert_awaited_once_with(
            DOCK_TASK_EMPTY_DUSTBIN
        )

    async def test_prepare_clean_start_rejects_failed_post_stop_refresh(self) -> None:
        """An accepted stop still requires authoritative refreshed state."""
        coordinator = self._make_coordinator()
        state = _docked_state()
        state.station_activity = 1
        coordinator.client.state = state
        coordinator.data = state
        coordinator.async_refresh_dock_status = AsyncMock(
            side_effect=(True, False)
        )
        coordinator.async_set_updated_data = MagicMock()
        coordinator.client.stop_dock_task = AsyncMock(
            return_value=CommandResponse(result_code=CommandResult.SUCCESS)
        )

        assert not await coordinator.async_prepare_clean_start()

        assert coordinator.async_refresh_dock_status.await_count == 2
        coordinator.client.stop_dock_task.assert_awaited_once_with(
            DOCK_TASK_EMPTY_DUSTBIN
        )

    @pytest.mark.parametrize(
        ("task", "fields"),
        [
            (DOCK_TASK_DRY_MOP, ("8", "9")),
            (DOCK_TASK_DRY_DUST_BIN, ("10", "11")),
            (DOCK_TASK_DRY_DOCK_BAG, ("12", "13")),
        ],
    )
    async def test_prepare_clean_start_keeps_typed_drying_task(
        self,
        task: str,
        fields: tuple[str, str],
    ) -> None:
        """A new clean lets firmware hand off typed drying work."""
        coordinator = self._make_coordinator()
        state = _docked_state()
        state.set_dock_drying_task(
            task,
            elapsed=60,
            target=180,
            fields=fields,
        )
        coordinator.client.state = state
        coordinator.data = state
        coordinator.async_refresh_dock_status = AsyncMock(return_value=True)
        coordinator.async_set_updated_data = MagicMock()
        coordinator.client.stop_dock_task = AsyncMock()

        assert can_prepare_clean_start(state)
        assert await coordinator.async_prepare_clean_start()
        assert can_prepare_clean_start(state, allow_dock_stop=False)

        coordinator.client.stop_dock_task.assert_not_awaited()
        assert coordinator.async_refresh_dock_status.await_count == 1
        assert can_start_cleaning(state)

    async def test_prepare_clean_start_rejects_lingering_dock_task_after_stop(self) -> None:
        """Accepted stop is not enough if refreshed telemetry still shows a task."""
        coordinator = self._make_coordinator()
        state = _docked_state()
        state.station_activity = 1
        coordinator.client.state = state
        coordinator.data = state
        coordinator.async_refresh_dock_status = AsyncMock(return_value=True)
        coordinator.async_set_updated_data = MagicMock()
        coordinator.client.stop_dock_task = AsyncMock(
            return_value=CommandResponse(result_code=CommandResult.SUCCESS)
        )

        assert can_prepare_clean_start(state)
        assert not await coordinator.async_prepare_clean_start()

        coordinator.client.stop_dock_task.assert_awaited_once_with(
            DOCK_TASK_EMPTY_DUSTBIN
        )
        assert coordinator.async_refresh_dock_status.await_count == 2

    async def test_prepare_clean_start_rejects_dock_stop_when_disabled(self) -> None:
        """No-stop preparation mode should never cancel dock maintenance."""
        coordinator = self._make_coordinator()
        state = _docked_state()
        state.station_activity = 2
        coordinator.client.state = state
        coordinator.data = state
        coordinator.async_refresh_dock_status = AsyncMock(return_value=True)
        coordinator.client.stop_dock_task = AsyncMock()

        assert not can_prepare_clean_start(state, allow_dock_stop=False)
        assert not await coordinator.async_prepare_clean_start(allow_dock_stop=False)

        coordinator.client.stop_dock_task.assert_not_awaited()

    async def test_prepare_clean_start_accepts_wash_follow_on_drying(self) -> None:
        """Stopping a wash may hand off mop drying to the clean command."""
        coordinator = self._make_coordinator()
        state = _docked_state()
        state.station_activity = 2
        coordinator.client.state = state
        coordinator.data = state
        coordinator.async_refresh_dock_status = AsyncMock(return_value=True)
        coordinator.async_set_updated_data = MagicMock()

        async def stop_task(task: str | None = None) -> CommandResponse:
            assert task == DOCK_TASK_WASH_MOP
            state.station_activity = 0
            state.set_dock_drying_task(
                DOCK_TASK_DRY_MOP,
                elapsed=0,
                target=18000,
                fields=("8", "9"),
            )
            return CommandResponse(result_code=CommandResult.SUCCESS)

        coordinator.client.stop_dock_task = AsyncMock(side_effect=stop_task)

        assert can_prepare_clean_start(state)
        assert await coordinator.async_prepare_clean_start()

        coordinator.client.stop_dock_task.assert_awaited_once_with(
            DOCK_TASK_WASH_MOP
        )
        assert coordinator.async_refresh_dock_status.await_count == 2
        assert state.active_dock_task_keys == (DOCK_TASK_DRY_MOP,)

    async def test_prepare_clean_start_allows_multiple_typed_dryers(self) -> None:
        """Multiple typed drying tasks can be handed off without pre-stops."""
        coordinator = self._make_coordinator()
        state = _docked_state()
        state.set_dock_drying_task(
            DOCK_TASK_DRY_MOP,
            elapsed=60,
            target=180,
            fields=("8", "9"),
        )
        state.set_dock_drying_task(
            DOCK_TASK_DRY_DUST_BIN,
            elapsed=60,
            target=180,
            fields=("10", "11"),
        )
        coordinator.client.state = state
        coordinator.data = state
        coordinator.async_refresh_dock_status = AsyncMock(return_value=True)
        coordinator.client.stop_dock_task = AsyncMock()

        assert can_prepare_clean_start(state)
        assert await coordinator.async_prepare_clean_start()

        coordinator.client.stop_dock_task.assert_not_awaited()

    async def test_prepare_clean_start_rejects_mixed_stop_and_dry_tasks(self) -> None:
        """Mixed generic-stop and drying tasks remain ambiguous."""
        coordinator = self._make_coordinator()
        state = _docked_state()
        state.station_activity = 1
        state.set_dock_drying_task(
            DOCK_TASK_DRY_MOP,
            elapsed=60,
            target=180,
            fields=("8", "9"),
        )
        coordinator.client.state = state
        coordinator.data = state
        coordinator.async_refresh_dock_status = AsyncMock(return_value=True)
        coordinator.client.stop_dock_task = AsyncMock()

        assert not can_prepare_clean_start(state)
        assert not await coordinator.async_prepare_clean_start()

        coordinator.client.stop_dock_task.assert_not_awaited()

    async def test_action_refresh_preserves_recent_active_working_status(self) -> None:
        """Robot action gates avoid full base-status refresh while task data is fresh."""
        coordinator = self._make_coordinator()
        coordinator.client.state.update_from_working_status({"3": 120})
        coordinator.client.get_status = AsyncMock(return_value=CommandResponse(data={}))
        coordinator.async_set_updated_data = MagicMock()

        assert await coordinator.async_refresh_action_status()

        coordinator.client.get_status.assert_awaited_once_with(full_update=False)
        coordinator.async_set_updated_data.assert_called_once_with(
            coordinator.client.state
        )
        assert not coordinator._dock_status_refresh_failed

    async def test_action_refresh_requires_dock_payload_when_full_update_needed(self) -> None:
        """Without active task telemetry, action refresh needs real dock/base status."""
        coordinator = self._make_coordinator()
        coordinator.client.get_status = AsyncMock(return_value=CommandResponse(data={}))
        coordinator.async_set_updated_data = MagicMock()

        assert not await coordinator.async_refresh_action_status()

        coordinator.client.get_status.assert_awaited_once_with(full_update=True)
        assert coordinator._dock_status_refresh_failed


class TestTopicSubscriptionRenewal:
    """The broadcast subscription must be renewed before it lapses (#73).

    The robot only broadcasts status/working_status and display_map while an
    active_robot_publish subscription is live, and that lasts 600 s. Observed on
    hardware 2026-08-08 during a real room clean: with the subscription expired,
    a 4000-line window carried 423 base_status broadcasts but exactly 1
    working_status and 1 display_map. The vacuum entity sat at "docked" while the
    robot was audibly cleaning, and the live map never moved. Re-subscribing
    turned it straight back on — 211 / 30 / 30 over the next window.

    The renewal must not be conditional on believing we are cleaning: working_status
    is the signal that tells us we are cleaning, so gating renewal on it deadlocks.
    """

    def _coordinator(self, last_subscribe: float) -> NarwalCoordinator:
        c = NarwalCoordinator.__new__(NarwalCoordinator)
        c.hass = MagicMock()
        c.config_entry = MagicMock()
        c.client = MagicMock()
        c.client.state = NarwalState()
        c.client.connected = True
        c.client.get_status = AsyncMock(
            return_value=CommandResponse(data={"2": {"3": {"1": 10}}})
        )
        c.client.get_map = AsyncMock()
        c.client.get_consumable_info = AsyncMock()
        c.client.subscribe_to_topics = AsyncMock()
        c.client.supports_broadcasts = True
        c.client.state.map_data = MagicMock()  # skip the map-retry branch
        c._consecutive_failures = 0
        c._max_failures = 5
        c._consumable_poll_countdown = 99
        c._fast_poll_remaining = 0
        c._listen_task = None
        # This fixture exercises subscription renewal, not deferred map fetches.
        # Keep that background path suppressed so its mocked task creator does
        # not leave an unconsumed coroutine behind.
        c._map_fetch_pending = True
        c._pending_map_display_cache_snapshot = None
        c._pending_map_display_cache_restore = None
        c._last_display_map_resub = 0.0
        c._last_topic_subscribe = last_subscribe
        c._prev_working_status = MagicMock()
        c.active_clean_work_mode = None
        c.active_room_clean_settings = {}
        c.update_interval = None
        return c

    @pytest.mark.asyncio
    async def test_renews_when_subscription_is_stale(self) -> None:
        """A poll past the renewal window re-sends the subscription."""
        c = self._coordinator(time.monotonic() - (TOPIC_RESUBSCRIBE_AFTER + 30))
        await c._async_update_data()
        c.client.subscribe_to_topics.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_does_not_renew_while_subscription_is_fresh(self) -> None:
        """A fresh subscription is not re-sent on every poll."""
        c = self._coordinator(time.monotonic())
        await c._async_update_data()
        c.client.subscribe_to_topics.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_broadcast_model_does_not_subscribe(self) -> None:
        """Polling-only models never renew an unsupported broadcast subscription."""
        c = self._coordinator(time.monotonic() - (TOPIC_RESUBSCRIBE_AFTER + 30))
        c.client.supports_broadcasts = False

        await c._async_update_data()

        c.client.subscribe_to_topics.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_renewal_is_not_gated_on_cleaning_state(self) -> None:
        """Renewal happens even when the entity believes the robot is docked.

        This is the deadlock that caused #73: no subscription means no
        working_status, which means the entity never leaves "docked", which — if
        renewal were gated on cleaning — would mean the subscription is never
        renewed.
        """
        c = self._coordinator(time.monotonic() - (TOPIC_RESUBSCRIBE_AFTER + 30))
        c.client.state.update_from_base_status({"3": {"1": 10, "10": 1}})
        assert c.client.state.is_docked
        assert not c.client.state.is_cleaning

        await c._async_update_data()

        c.client.subscribe_to_topics.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_renewal_window_is_inside_the_ttl(self) -> None:
        """Renew with margin — renewing at or after expiry would still drop frames."""
        assert TOPIC_RESUBSCRIBE_AFTER < TOPIC_SUBSCRIPTION_TTL
        assert TOPIC_RESUBSCRIBE_AFTER <= TOPIC_SUBSCRIPTION_TTL / 2

    @pytest.mark.asyncio
    async def test_renewal_failure_does_not_break_the_poll(self) -> None:
        """A failed renewal must not take the whole update down."""
        c = self._coordinator(time.monotonic() - (TOPIC_RESUBSCRIBE_AFTER + 30))
        c.client.subscribe_to_topics = AsyncMock(side_effect=RuntimeError("ws closed"))
        state = await c._async_update_data()
        assert state is c.client.state
