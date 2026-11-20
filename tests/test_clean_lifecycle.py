"""Native task phase and retained-session regressions, without device I/O."""

# ruff: noqa: E402
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock

import blackboxprotobuf
import pytest

import tests.ha_stubs

tests.ha_stubs.install()

from custom_components.narwal.coordinator import (
    can_prepare_clean_start,
    can_resume_cleaning,
    can_stop_cleaning,
    is_clean_session_context,
    is_confirmed_terminal_clean_state,
    is_live_clean_setting_available,
)
from custom_components.narwal.vacuum import _status_summary, _task_status
from narwal_client.client import NarwalClient
from narwal_client.const import CommandResult
from narwal_client.lifecycle import CleanPhase, clean_task_status
from narwal_client.models import CommandResponse
from narwal_client.protocol import build_frame
from tests.test_vacuum_segments import VacuumActivity, _make_vacuum


@pytest.fixture
def clock(monkeypatch):
    value = [1000.0]
    fake = SimpleNamespace(monotonic=lambda: value[0])
    monkeypatch.setattr("narwal_client.models.time", fake)
    monkeypatch.setattr("custom_components.narwal.narwal_client.models.time", fake)
    return value


def base(task, code, dock=False, **nested):
    return {
        "3": {"1": task, {2: "4", 5: "6", 17: "17"}[task]: code, **nested},
        "11": 3 if dock else 1,
        "47": 1 if dock else 2,
    }


def metrics(seconds=100, area=2.0):
    return {
        "1": 0.3,
        "2": struct.unpack("<I", struct.pack("<f", area))[0],
        "3": seconds,
        "4": 400,
        "6": 1,
    }


def cleaning(clock, task=17):
    client = NarwalClient("offline", device_id="test-device")
    client._update_from_base_status_broadcast(base(task, 3 if task == 2 else 7))
    client.state.update_from_working_status(metrics())
    assert client.state.is_cleaning
    return client


@pytest.mark.parametrize(
    "task,code,phase,dock",
    [
        (5, 2, CleanPhase.RETURNING_TO_WASH, False),
        (5, 4, CleanPhase.WASHING_MOPS, True),
        (5, 10, CleanPhase.DOCKING, False),
        (5, 11, CleanPhase.LEAVING_DOCK, False),
        (5, 12, CleanPhase.PREPARING, True),
        (5, 16, CleanPhase.EMPTYING, True),
        (17, 2, CleanPhase.RETURNING_TO_WASH, False),
        (17, 4, CleanPhase.WASHING_MOPS, True),
        (17, 15, CleanPhase.LEAVING_DOCK, False),
        (17, 16, CleanPhase.PREPARING, True),
        (17, 19, CleanPhase.EMPTYING, True),
        (2, 6, CleanPhase.DOCKING, False),
        (2, 7, CleanPhase.LEAVING_DOCK, False),
        (2, 8, CleanPhase.PREPARING, True),
        (2, 12, CleanPhase.EMPTYING, True),
    ],
)
def test_phase_overrides_recent_metrics(clock, task, code, phase, dock):
    c = cleaning(clock, task)
    before = (c.state.cleaning_time, c.state.cleaning_area)
    c._update_from_base_status_broadcast(base(task, code, dock))
    s = c.state
    assert s.clean_task_phase == phase
    assert s.is_docked is dock
    assert not s.is_cleaning
    assert is_clean_session_context(s)
    assert not is_confirmed_terminal_clean_state(s)
    assert not can_prepare_clean_start(s)
    assert not is_live_clean_setting_available(s)
    assert can_stop_cleaning(s)
    s.update_from_working_status(metrics(300, 10.0))
    assert (s.cleaning_time, s.cleaning_area) == before
    assert s.clean_task_phase == phase
    assert _task_status(s) == phase.value
    assert _status_summary(s) == phase.value.replace("_", " ").title()
    assert "current_room" not in _make_vacuum(s).extra_state_attributes


def test_recharge_resume_and_terminal_are_distinct(clock):
    c = cleaning(clock)
    s = c.state
    c._update_from_base_status_broadcast(base(17, 6, **{"11": 1}))
    assert s.is_returning and not s.is_charging_to_resume
    c._update_from_base_status_broadcast(base(17, 6, **{"11": 2}))
    assert s.is_returning and not s.is_docked
    c._update_from_base_status_broadcast(base(17, 6, True, **{"11": 3}))
    assert s.is_charging_to_resume and s.is_docked
    assert _task_status(s) == "charging_to_resume"
    assert _make_vacuum(s).activity == VacuumActivity.DOCKED
    assert _make_vacuum(s).extra_state_attributes["charging_to_resume"] is True
    assert not can_prepare_clean_start(s)
    s.is_paused = True
    assert not can_resume_cleaning(s)
    s.is_paused = False
    s.update_from_working_status(metrics(900, 15.0))
    assert s.cleaning_time == 100
    assert s.is_charging_to_resume
    clock[0] += 10
    c._update_from_base_status_broadcast(base(17, 15, True))
    assert not s.is_charging_to_resume and not s.is_cleaning and s.is_docked
    c._update_from_base_status_broadcast(base(17, 15))
    assert not s.is_docked
    c._update_from_base_status_broadcast(base(17, 9))
    s.update_from_working_status(metrics(101, 2.1))
    assert s.is_cleaning and s.cleaning_time == 101
    assert is_live_clean_setting_available(s)
    c._update_from_base_status_broadcast({"3": {"1": 1, "3": 1}, "11": 3, "47": 1})
    assert not s.has_native_clean_task
    assert not s.is_charging_to_resume and not s.is_cleaning
    assert is_confirmed_terminal_clean_state(s)


@pytest.mark.parametrize("task,code", [(5, 7), (17, 7), (17, 9)])
def test_clean_phase_does_not_invent_off_dock(clock, task, code):
    c = cleaning(clock, task)
    c._update_from_base_status_broadcast(base(task, code, True))
    assert c.state.is_docked and not c.state.is_cleaning


@pytest.mark.parametrize(
    "status",
    [
        {"3": {"1": 14}, "11": 3, "47": 1},
        {"3": {"1": 1}, "11": 3, "47": 1},
        base(17, 6, True),
        base(17, 6, False, **{"11": 3}),
    ],
)
def test_contact_alone_cannot_claim_recharge_hold(clock, status):
    c = NarwalClient("offline")
    c._update_from_base_status_broadcast(status)
    assert not c.state.is_charging_to_resume


def test_battery_partial_and_stale_phase_are_not_new_activity(clock):
    c = cleaning(clock)
    c._update_from_base_status_broadcast(base(17, 6, True, **{"11": 3}))
    clock[0] += 20
    c._update_from_base_status_broadcast({"2": 1109393408})
    assert c.state.has_native_clean_task
    assert c.state.clean_task_phase == CleanPhase.UNKNOWN
    assert not c.state.is_charging_to_resume
    assert not c.state.is_cleaning
    assert not can_prepare_clean_start(c.state)
    c.state.update_from_working_status(metrics(600, 9.0))
    assert c.state.clean_task_phase == CleanPhase.UNKNOWN
    assert c.state.cleaning_time == 100


@pytest.mark.asyncio
async def test_defaulted_rpc_cannot_end_broadcast_task(clock):
    c = cleaning(clock)
    c._update_from_base_status_broadcast(base(17, 6, True, **{"11": 3}))
    c.send_command = AsyncMock(
        return_value=CommandResponse(
            result_code=CommandResult.SUCCESS,
            data={"2": {"3": {"1": 1}, "11": 3, "47": 1}},
        )
    )
    await c.get_status(full_update=True)
    assert c.state.is_charging_to_resume
    clock[0] += 20
    await c.get_status(full_update=True)
    assert c.state.has_native_clean_task
    assert c.state.clean_task_phase == CleanPhase.UNKNOWN
    assert not can_prepare_clean_start(c.state)


@pytest.mark.asyncio
async def test_real_frame_pipeline_keeps_phase_and_pause(clock):
    c = cleaning(clock)
    c.state.is_paused = True
    data = base(17, 6, True, **{"11": 3, "2": 1})
    typedef = {
        "3": {
            "type": "message",
            "message_typedef": {str(k): {"type": "int"} for k in [1, 2, 11, 17]},
        },
        "11": {"type": "int"},
        "47": {"type": "int"},
    }
    payload = bytes(blackboxprotobuf.encode_message(data, typedef))
    await c._handle_message(build_frame(c._full_topic("status/robot_base_status"), payload))
    c.state.update_from_working_status(metrics(400, 10.0))
    assert c.state.is_paused
    assert not can_resume_cleaning(c.state)
    assert _task_status(c.state) == "paused"


@pytest.mark.parametrize("phase", [10, 12, 999])
def test_unknown_composite_phase_is_held_not_invented(clock, phase):
    c = cleaning(clock)
    c._update_from_base_status_broadcast(base(17, phase, True))
    assert c.state.clean_task_phase == CleanPhase.UNKNOWN
    assert not c.state.is_cleaning
    assert is_clean_session_context(c.state)
    assert not can_prepare_clean_start(c.state)
    assert not is_live_clean_setting_available(c.state)


@pytest.mark.parametrize("status", [None, [], {"1": []}, {"1": 17, "17": []}])
def test_invalid_status_is_not_a_recharge_signal(status):
    assert clean_task_status({"3": status}) is None


@pytest.mark.parametrize("code", [2, 999])
def test_unknown_sweep_phase_keeps_task(clock, code):
    c = cleaning(clock, 2)
    c._update_from_base_status_broadcast(base(2, code, True))
    assert c.state.has_native_clean_task
    assert c.state.clean_task_phase == CleanPhase.UNKNOWN
    assert not can_prepare_clean_start(c.state)
    assert not is_confirmed_terminal_clean_state(c.state)


def test_partial_clean_phase_preserves_dock_confirmation(clock):
    c = cleaning(clock)
    c._update_from_base_status_broadcast(base(17, 7, True))
    c._update_from_base_status_broadcast({"3": {"1": 17, "17": 7}})
    assert c.state.is_docked
    assert not c.state.is_cleaning
    assert not is_live_clean_setting_available(c.state)
    c._update_from_base_status_broadcast(base(17, 7))
    assert c.state.is_cleaning
    assert is_live_clean_setting_available(c.state)


def test_terminal_drying_supersedes_counters(clock):
    c = cleaning(clock)
    c._update_from_base_status_broadcast(base(17, 6, True, **{"11": 3}))
    c._update_from_base_status_broadcast({"3": {"1": 14}, "11": 3, "47": 1})
    assert not c.state.has_native_clean_task
    assert is_confirmed_terminal_clean_state(c.state)
    c.state.update_from_working_status(metrics(300, 8.0))
    assert not c.state.has_native_clean_task
    assert not c.state.is_cleaning


def test_sweep_dock_visit_preserves_session(clock):
    from tests.test_coordinator import _new_coordinator

    coordinator = _new_coordinator()
    c = cleaning(clock, 2)
    for code in (6, 7, 3):
        c._update_from_base_status_broadcast(base(2, code))
        assert coordinator._has_clean_session_signal(c.state)


def test_emptying_held_sweep_keeps_robot_stop(clock):
    from custom_components.narwal.vacuum import _can_stop_vacuum

    c = cleaning(clock, 2)
    c._update_from_base_status_broadcast(base(2, 12, True))
    assert _can_stop_vacuum(c.state)


def test_metrics_cannot_clear_native_pause(clock):
    c = cleaning(clock)
    c._update_from_base_status_broadcast(base(17, 7, **{"2": 1}))
    assert c.state.is_paused
    c.state.update_from_working_status(metrics(300, 8.0))
    assert c.state.is_paused
    c._update_from_base_status_broadcast({"3": {"1": 17, "17": 7}})
    assert c.state.is_paused
    c._update_from_base_status_broadcast(base(17, 7))
    assert not c.state.is_paused


@pytest.mark.parametrize(
    "task,code,nested", [(5, 2, {}), (17, 2, {}), (17, 6, {"11": 1}), (2, 6, {})]
)
def test_paused_off_dock_return_can_resume(clock, task, code, nested):
    c = cleaning(clock, task)
    c._update_from_base_status_broadcast(base(task, code, **nested, **{"2": 1}))
    assert can_resume_cleaning(c.state)
    assert not is_live_clean_setting_available(c.state)
    c._update_from_base_status_broadcast(base(task, code, True, **nested, **{"2": 1}))
    assert not can_resume_cleaning(c.state)


@pytest.mark.parametrize(
    "status", [{"3": {"1": 1}, "11": 3, "47": 1}, {"3": {"1": 2, "4": 1}, "11": 3, "47": 1}]
)
def test_confirmed_idle_releases_native_task(clock, status):
    c = cleaning(clock)
    c._update_from_base_status_broadcast(status)
    assert not c.state.has_native_clean_task
    assert is_confirmed_terminal_clean_state(c.state)


def test_malformed_phase_cannot_release_held_task(clock):
    c = cleaning(clock, 2)
    c._update_from_base_status_broadcast(base(2, 7))
    c._update_from_base_status_broadcast({"3": {"1": 2, "4": []}, "11": 3, "47": 1})
    assert c.state.has_native_clean_task
    assert not can_prepare_clean_start(c.state)


def test_partial_recharge_stage_updates_phase(clock):
    c = cleaning(clock)
    c._update_from_base_status_broadcast(base(17, 6, **{"11": 1}))
    clock[0] += 20
    c._update_from_base_status_broadcast({"3": {"1": 17, "11": 3}, "11": 3, "47": 1})
    assert c.state.is_charging_to_resume


@pytest.mark.asyncio
async def test_native_task_blocks_sdk_quick_start(clock):
    c = cleaning(clock, 2)
    c._update_from_base_status_broadcast(base(2, 8, True))
    c.send_command = AsyncMock()
    result = await c.start_easy_clean()
    assert result.result_code == CommandResult.NOT_APPLICABLE
    c.send_command.assert_not_awaited()


def test_native_emptying_cannot_be_stopped_as_standalone_dock_work(clock):
    from custom_components.narwal.dock_tasks import can_stop_dock_task
    from narwal_client.client import _robot_work_blocks_generic_dock_stop
    from narwal_client.models import DOCK_TASK_EMPTY_DUSTBIN

    c = cleaning(clock, 2)
    c._update_from_base_status_broadcast(base(2, 12, True))
    assert _robot_work_blocks_generic_dock_stop(c.state)
    assert not can_stop_dock_task(c.state, DOCK_TASK_EMPTY_DUSTBIN)
    assert can_stop_cleaning(c.state)


def test_initial_paused_sweep_return_can_resume(clock):
    c = NarwalClient("offline")
    c._update_from_base_status_broadcast(base(2, 6, **{"2": 1}))
    assert can_resume_cleaning(c.state)
