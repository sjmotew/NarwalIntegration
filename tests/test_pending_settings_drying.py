"""Next-clean mode edits during dock drying must stay entirely local."""

from __future__ import annotations

from types import MethodType
from unittest.mock import AsyncMock, patch

import pytest

from tests.test_clean_settings_entities import _coordinator, _DESCS, _LEGACY_DESCS
from tests.test_client_rooms import _items, _task
from tests.test_vacuum_segments import _make_vacuum
from custom_components.narwal.coordinator import (
    NarwalCoordinator,
    can_edit_pending_clean_settings,
    can_start_cleaning,
    is_narwal_task_busy,
)
from custom_components.narwal.select import (
    LegacyNarwalSettingSelect,
    NarwalSelect,
    SettingRestoreData,
)
from homeassistant.exceptions import HomeAssistantError
from narwal_client.client import NarwalClient, RoomCleanSettings
from narwal_client.const import WorkingStatus, WorkMode
from narwal_client.models import (
    CommandResponse,
    DOCK_TASK_DRY_DOCK_BAG,
    DOCK_TASK_DRY_DUST_BIN,
    DOCK_TASK_DRY_MOP,
    MapData,
    NarwalState,
    RoomInfo,
)


def _drying_state():
    # Older/non-broadcast telemetry identifies mop drying without typed timers.
    return NarwalState(
        working_status=WorkingStatus.CHARGED,
        dock_presence=1,
        dock_field11=2,
        dock_activity=4,
    )


def _selector(coordinator, kind):
    if kind == "primary":
        return NarwalSelect(coordinator, _DESCS["work_mode"]), "vacuum"
    return LegacyNarwalSettingSelect(coordinator, _LEGACY_DESCS["mode"]), "Vacuum"


@pytest.mark.parametrize("kind", ["primary", "legacy"])
async def test_vacuum_mode_during_drying_is_local_then_used_by_native_room_start(kind):
    state = _drying_state()
    state.map_data = MapData(map_id=1, rooms=[RoomInfo(room_id=2)])
    vacuum = _make_vacuum(state)
    coordinator = vacuum.coordinator
    client = NarwalClient(
        "unused.invalid",
        device_id="test",
        topic_prefix="/hEA7OEshlx",
        supports_broadcasts=False,
    )
    client.state = state
    client._connected.set()
    client._ws = AsyncMock()
    client._robot_awake = True
    client.send_command = AsyncMock(return_value=CommandResponse(result_code=1))
    client.get_map = AsyncMock(return_value=state.map_data)
    coordinator.client = client
    coordinator.room_clean_settings = {}
    coordinator.room_clean_settings_customized = {}
    coordinator.room_settings_map_id.return_value = "1"
    for method in ("default_room_clean_settings", "room_clean_settings_for_rooms"):
        setattr(
            coordinator,
            method,
            MethodType(getattr(NarwalCoordinator, method), coordinator),
        )

    primary = NarwalSelect(coordinator, _DESCS["work_mode"])
    with patch.object(
        primary,
        "async_get_last_extra_data",
        AsyncMock(return_value=SettingRestoreData(value=4)),
    ):
        await primary.async_added_to_hass()
    assert coordinator.clean_settings.work_mode == WorkMode.VACUUM_AND_MOP
    accepted_profile = RoomCleanSettings(work_mode=WorkMode.VACUUM_AND_MOP)
    coordinator.active_clean_work_mode = WorkMode.VACUUM_AND_MOP
    coordinator.active_room_clean_settings = {2: accepted_profile}

    selector, option = _selector(coordinator, kind)
    assert is_narwal_task_busy(state)
    assert state.blocks_robot_start_for_dock_task
    start_allowed_before = can_start_cleaning(state)
    assert selector.available
    await selector.async_select_option(option)

    assert coordinator.clean_settings.work_mode == WorkMode.VACUUM
    assert selector.extra_restore_state_data.as_dict()["value"] == int(WorkMode.VACUUM)
    assert coordinator.active_clean_work_mode == WorkMode.VACUUM_AND_MOP
    assert accepted_profile.work_mode == WorkMode.VACUUM_AND_MOP
    coordinator.set_active_clean_setting.assert_not_called()
    client.send_command.assert_not_awaited()
    client._ws.send.assert_not_awaited()
    assert state.dock_activity == 4
    assert state.blocks_robot_start_for_dock_task
    assert can_start_cleaning(state) == start_allowed_before

    await vacuum.async_clean_segments(["2"])
    client.send_command.assert_awaited_once()
    call = client.send_command.await_args
    assert call.args[0] == "clean/start_clean"
    task = _task(call.kwargs["payload"])
    assert task["5"] == int(WorkMode.VACUUM)
    assert _items(task)[0]["1"]["2"] == 2
    assert _items(task)[0]["2"]["1"] == 2  # CleanParam.VACUUM
    assert "5" in _items(task)[0]["2"]
    assert "6" not in _items(task)[0]["2"]
    assert "7" not in _items(task)[0]["2"]


@pytest.mark.parametrize("kind", ["primary", "legacy"])
@pytest.mark.parametrize(
    "blocked",
    [
        "washing",
        "emptying",
        "error_flag",
        "error_status",
        "cleaning",
        "paused",
        "returning",
        "assumed_clean",
        "charging_to_resume",
        "unmapped_dock_task",
        "assumed_dock_task",
        "unknown_status",
    ],
)
async def test_drying_exception_preserves_task_and_error_locks(kind, blocked):
    state = _drying_state()
    if blocked == "washing":
        state.station_activity = 2
    elif blocked == "emptying":
        state.station_activity = 1
    elif blocked == "error_flag":
        state.has_error = True
    elif blocked == "error_status":
        state.working_status = WorkingStatus.ERROR
    elif blocked == "cleaning":
        state.working_status = WorkingStatus.CLEANING
    elif blocked == "paused":
        state.is_paused = True
        state.current_room_id = 2
    elif blocked == "returning":
        state.working_status = WorkingStatus.TASK_COMPLETED
    elif blocked == "assumed_clean":
        state.assume_robot_clean()
    elif blocked == "charging_to_resume":
        state.update_from_base_status({"3": {"1": 17, "17": 6, "11": 3}, "11": 3, "47": 1})
    elif blocked == "unmapped_dock_task":
        state.station_activity = 4
    elif blocked == "assumed_dock_task":
        state.assume_dock_task(DOCK_TASK_DRY_MOP)
    elif blocked == "unknown_status":
        state.working_status = WorkingStatus.UNKNOWN
    coordinator = _coordinator(state=state)
    selector, option = _selector(coordinator, kind)
    assert not can_edit_pending_clean_settings(state)
    assert not selector.available
    with pytest.raises(HomeAssistantError, match="cannot be changed right now"):
        await selector.async_select_option(option)
    assert coordinator.clean_settings.work_mode == WorkMode.VACUUM_AND_MOP
    assert not coordinator.client.mock_calls


@pytest.mark.parametrize("kind", ["primary", "legacy"])
async def test_drying_does_not_override_selected_room_profile_policy(kind):
    coordinator = _coordinator(state=_drying_state())
    coordinator.has_selected_clean_rooms.return_value = True
    selector, option = _selector(coordinator, kind)
    assert not selector.available
    with pytest.raises(HomeAssistantError, match="cannot be changed right now"):
        await selector.async_select_option(option)


@pytest.mark.parametrize(
    "task, fields",
    [
        (DOCK_TASK_DRY_MOP, ("8", "9")),
        (DOCK_TASK_DRY_DUST_BIN, ("10", "11")),
        (DOCK_TASK_DRY_DOCK_BAG, ("12", "13")),
    ],
)
def test_existing_fresh_typed_drying_remains_editable(task, fields):
    state = _drying_state()
    state.dock_activity = 0
    state.set_dock_drying_task(task, elapsed=45, target=180, fields=fields)
    assert can_edit_pending_clean_settings(state)
    coordinator = _coordinator(state=state)
    assert _selector(coordinator, "primary")[0].available
    assert _selector(coordinator, "legacy")[0].available
