"""Reconcile a missed terminal broadcast without ending a recharge hold."""

from copy import deepcopy
from importlib import import_module
from unittest.mock import AsyncMock, MagicMock

import pytest
import tests.ha_stubs

tests.ha_stubs.install()
from custom_components.narwal.coordinator import CleanPhase, is_clean_session_context
from custom_components.narwal.narwal_client import CommandResponse, NarwalClient
from tests.test_trajectory_job_identity import JOB, owner, timeline


@pytest.fixture
def held(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    c = owner()
    state = c.client.state
    c.client = NarwalClient("offline")
    c.client.state = state
    c.client._update_from_base_status_broadcast(
        {"3": {"1": 17, "17": 6, "11": 3}, "11": 3, "47": 1}
    )
    c._trajectory_job_id = str(JOB)
    c._trajectory_job_frame = c._trajectory_map_frame(state)
    c._clean_session_active = True
    c._task_context_was_active = True
    c._task_recovery_attempts = 3
    c._active_context_revision = 1
    c.client.get_clean_timeline = AsyncMock(
        return_value=timeline({"1": JOB, "2": 1}, {"1": JOB + 1, "2": 6})
    )
    c.client.send_command = AsyncMock(
        return_value=CommandResponse(result_code=1, data={"2": {"3": {"1": 1}, "11": 3, "47": 1}})
    )
    clock[0] += 20
    assert state.clean_task_phase == CleanPhase.UNKNOWN
    return c, clock


@pytest.mark.parametrize("prefix", ["narwal_client", "custom_components.narwal.narwal_client"])
@pytest.mark.parametrize(
    "bad", ["none", "active", "double_end", "after_end", "float", "bool", "unordered", "rejected"]
)
def test_terminal_timeline_is_strict(prefix, bad):
    parser = import_module(prefix + ".client").NarwalClient.ended_job_id_from_timeline
    r = timeline({"1": JOB, "2": 1}, {"1": JOB + 1, "2": 6})
    assert parser(r) == str(JOB)
    events = r.data["2"]["1"]
    if bad == "none":
        r.data = {}
    elif bad == "active":
        events[-1]["2"] = 4
    elif bad == "double_end":
        events.insert(1, {"1": JOB, "2": 6})
    elif bad == "after_end":
        events.append({"1": JOB + 2, "2": 4})
    elif bad == "float":
        events[-1]["1"] = float(JOB + 1)
    elif bad == "bool":
        events[-1]["2"] = True
    elif bad == "unordered":
        events[-1]["1"] = JOB - 1
    else:
        r.result_code = 4
    assert parser(r) is None


@pytest.mark.asyncio
async def test_same_ended_job_and_dock_release_hold_preserving_trail(held):
    c, _ = held
    state = c.client.state
    points = state.map_display_data.trajectory_points()
    await c._async_reconcile_ended_clean()
    assert not state.has_native_clean_task
    assert not is_clean_session_context(state)
    assert state.map_display_data.trajectory_points() == points
    c._schedule_map_display_cache_save.assert_called_with(state, immediate=True)
    c.client.send_command.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        "active",
        "other_job",
        "timeline_failure",
        "status_failure",
        "missing_dock",
        "off_dock",
        "recharge",
        "rejected",
    ],
)
async def test_ambiguous_evidence_never_releases_hold(held, bad):
    c, _ = held
    if bad == "active":
        c.client.get_clean_timeline.return_value = timeline()
    elif bad == "other_job":
        c.client.get_clean_timeline.return_value = timeline(
            {"1": JOB - 2, "2": 1}, {"1": JOB - 1, "2": 6}
        )
    elif bad == "timeline_failure":
        c.client.get_clean_timeline.side_effect = TimeoutError
    elif bad == "status_failure":
        c.client.send_command.side_effect = TimeoutError
    elif bad == "missing_dock":
        c.client.send_command.return_value.data["2"] = {"3": {"1": 1}}
    elif bad == "off_dock":
        c.client.send_command.return_value.data["2"]["11"] = 1
    elif bad == "recharge":
        c.client.send_command.return_value.data["2"]["3"] = {"1": 17, "17": 6, "11": 3}
    else:
        c.client.send_command.return_value.result_code = 4
    await c._async_reconcile_ended_clean()
    assert c.client.state.has_native_clean_task
    assert c.client.state.clean_task_phase == CleanPhase.UNKNOWN


@pytest.mark.asyncio
@pytest.mark.parametrize("during", ["timeline", "status"])
@pytest.mark.parametrize("change", ["epoch", "frame", "job", "task", "base", "working", "state"])
async def test_each_await_fences_new_telemetry_and_ownership(held, during, change):
    c, _ = held
    method = c.client.get_clean_timeline if during == "timeline" else c.client.send_command
    result = method.return_value

    async def changed(*args, **kwargs):
        if change == "epoch":
            c._active_context_revision += 1
        elif change == "frame":
            c.client.state.map_data.origin_x += 1
        elif change == "job":
            c._trajectory_job_id = str(JOB + 2)
        elif change == "task":
            c.client.state.native_clean_task = None
        elif change == "base":
            c.client._update_from_base_status_broadcast({"2": 1109393408})
        elif change == "working":
            c.client._update_from_working_status_broadcast({})
        else:
            c.client.state = deepcopy(c.client.state)
        return result

    method.side_effect = changed
    before = c.client.state
    await c._async_reconcile_ended_clean()
    if change != "task":
        assert before.has_native_clean_task
    if during == "timeline":
        c.client.send_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_budget_independent_of_profile_recovery_and_rate_limited(held):
    c, clock = held
    c.client.get_clean_timeline.return_value = timeline()
    assert c._needs_active_task_recovery(c.client.state)
    await c._async_reconcile_ended_clean()
    await c._async_reconcile_ended_clean()
    assert c.client.get_clean_timeline.await_count == 1
    for _ in range(2):
        clock[0] += 60
        await c._async_reconcile_ended_clean()
    assert c.client.get_clean_timeline.await_count == 3
    assert c._needs_terminal_task_recovery(c.client.state)
    clock[0] += 600
    await c._async_reconcile_ended_clean()
    assert c.client.get_clean_timeline.await_count == 3
    clock[0] += 300
    c.client.get_clean_timeline.return_value = timeline({"1": JOB, "2": 1}, {"1": JOB + 1, "2": 6})
    await c._async_reconcile_ended_clean()
    assert c.client.get_clean_timeline.await_count == 4
    assert not c.client.state.has_native_clean_task


@pytest.mark.asyncio
async def test_third_terminal_attempt_can_succeed(held):
    c, clock = held
    c.client.get_clean_timeline.return_value = timeline()
    await c._async_reconcile_ended_clean()
    clock[0] += 60
    await c._async_reconcile_ended_clean()
    c.client.get_clean_timeline.return_value = timeline({"1": JOB, "2": 1}, {"1": JOB + 1, "2": 6})
    clock[0] += 60
    await c._async_reconcile_ended_clean()
    assert not c.client.state.has_native_clean_task


@pytest.mark.asyncio
async def test_verified_job_recovery_does_not_require_map_data(held):
    c, _ = held
    c.client.state.map_data = None
    assert c._needs_terminal_task_recovery(c.client.state)
    await c._recover_active_task()
    assert not c.client.state.has_native_clean_task


@pytest.mark.parametrize(
    "status",
    [
        {"3": {"1": 1}, "11": 1, "47": 2},
        {"3": {"1": 1}},
        {"3": {"1": 99}, "11": 1, "47": 2},
        {"3": {"1": 14}, "11": 1, "47": 2},
        {"3": {"1": 2, "4": 1}},
    ],
)
def test_unconfirmed_idle_cannot_release_native_hold(held, status):
    c, _ = held
    c.client._update_from_base_status_broadcast(status)
    assert c.client.state.has_native_clean_task
    assert c.client.state.clean_task_phase == CleanPhase.UNKNOWN


@pytest.mark.parametrize("prefix", ["narwal_client", "custom_components.narwal.narwal_client"])
@pytest.mark.parametrize("fallback", [{"1": 4}, {"1": 4, "7": 1}])
def test_sequential_fallback_preserves_an_existing_job(prefix, fallback):
    client = import_module(prefix + ".client").NarwalClient("offline")
    client._update_from_base_status_broadcast({"3": {"1": 17, "17": 7}, "11": 1, "47": 2})
    client._update_from_base_status_broadcast({"3": fallback})
    assert client.state.has_native_clean_task
    assert client.state.clean_task_phase == CleanPhase.UNKNOWN
    assert client._start_blocked()


@pytest.mark.asyncio
async def test_recovery_releases_active_profiles_before_next_app_job(held):
    c, _ = held
    c.active_room_clean_settings = {1: object()}
    await c._async_reconcile_ended_clean()
    assert c.active_room_clean_settings == {}
    assert c._task_context_was_active is False
    assert c._trajectory_job_id is None


@pytest.mark.parametrize("prefix", ["narwal_client", "custom_components.narwal.narwal_client"])
@pytest.mark.parametrize(
    "partial",
    [
        {"3": {"1": 17}, "11": 1, "47": 2},
        {"3": {"17": 7}, "11": 1, "47": 2},
        {"3": {}, "11": 3, "47": 1},
    ],
)
def test_partial_native_contact_is_not_an_implicit_resume(prefix, partial):
    client = import_module(prefix + ".client").NarwalClient("offline")
    client._update_from_base_status_broadcast({"3": {"1": 17, "17": 7, "2": 1}, "11": 1, "47": 2})
    client._update_from_base_status_broadcast(partial)
    assert client.state.is_paused
    client._update_from_base_status_broadcast({"3": {"1": 17, "17": 7}, "11": 1, "47": 2})
    assert not client.state.is_paused


@pytest.mark.asyncio
@pytest.mark.parametrize("fresh", [False, True])
async def test_unknown_job_recovers_only_from_fresh_native_status(held, fresh):
    c, _ = held
    c._trajectory_job_id = None
    c.client.state.map_data = None

    async def subscribe():
        if fresh:
            c.client._update_from_base_status_broadcast({"3": {"1": 14}, "11": 3, "47": 1})
            c._sync_active_clean_context(c.client.state)

    c.client.subscribe_to_topics = AsyncMock(side_effect=subscribe)
    if fresh:
        await c._recover_active_task()
    else:
        await c._async_reconcile_ended_clean()
    # A completed timeline cannot establish ownership retrospectively.
    c.client.get_clean_timeline.assert_awaited_once()
    c.client.send_command.assert_not_awaited()
    c.client.subscribe_to_topics.assert_awaited_once()
    assert c.client.state.has_native_clean_task is not fresh


def test_native_pause_is_shown_even_without_legacy_active_status(held):
    from custom_components.narwal.vacuum import _task_status, NarwalVacuum
    from custom_components.narwal.narwal_client.const import WorkingStatus

    c, _ = held
    c.client.state.is_paused = True
    c.client.state.working_status = WorkingStatus.UNKNOWN
    assert _task_status(c.client.state) == "paused"
    c.data = c.client.state
    assert NarwalVacuum(c).activity == "paused"


@pytest.mark.asyncio
async def test_lifecycle_identity_recovers_without_a_map_or_trajectory_label(held):
    c, clock = held
    c._trajectory_job_id = None
    c.client.state.map_data = None
    c.client.get_clean_timeline.return_value = timeline()
    c.client.subscribe_to_topics = AsyncMock()
    await c._async_reconcile_ended_clean()
    assert c._native_clean_job_id == str(JOB)
    assert c._trajectory_job_id is None
    assert c.client.state.has_native_clean_task
    c.client.get_clean_timeline.return_value = timeline({"1": JOB, "2": 1}, {"1": JOB + 1, "2": 6})
    clock[0] += 60
    await c._async_reconcile_ended_clean()
    assert not c.client.state.has_native_clean_task
    assert c._native_clean_job_id is None


@pytest.mark.asyncio
async def test_failed_unknown_identity_read_still_renews_broadcasts(held):
    c, _ = held
    c._trajectory_job_id = None
    c.client.get_clean_timeline.side_effect = TimeoutError
    c.client.subscribe_to_topics = AsyncMock()
    await c._async_reconcile_ended_clean()
    c.client.subscribe_to_topics.assert_awaited_once()
    assert c.client.state.has_native_clean_task


@pytest.mark.parametrize("prefix", ["narwal_client", "custom_components.narwal.narwal_client"])
def test_legacy_docked_terminal_releases_native_hold(prefix):
    client = import_module(prefix + ".client").NarwalClient("offline")
    client._update_from_base_status_broadcast({"3": {"1": 17, "17": 7}, "11": 1, "47": 2})
    client._update_from_base_status_broadcast({"3": {"1": 10}, "11": 3, "47": 1})
    assert not client.state.has_native_clean_task
    assert client.state.is_docked


@pytest.mark.asyncio
async def test_confirmed_end_with_legacy_dock_poll_releases_hold(held):
    c, _ = held
    c.client.send_command.return_value.data["2"]["3"] = {"1": 10}
    await c._async_reconcile_ended_clean()
    assert not c.client.state.has_native_clean_task


@pytest.mark.asyncio
async def test_repeated_unknown_phases_do_not_reset_exhausted_backoff(held):
    c, clock = held
    c.client.get_clean_timeline.return_value = timeline()
    c.client.subscribe_to_topics = AsyncMock()
    for index in range(4):
        c.client._update_from_base_status_broadcast({"3": {"1": 17, "17": 999}, "11": 3, "47": 1})
        await c._async_reconcile_ended_clean()
        clock[0] += 60
    assert c.client.get_clean_timeline.await_count == 3
    assert c._terminal_recovery_attempts == 3
    assert c._terminal_recovery_next_at > clock[0] + 600


@pytest.mark.asyncio
async def test_terminal_cooldown_cannot_starve_room_profile_recovery(held):
    import asyncio
    from tests.test_current_task_recovery import task_response
    from custom_components.narwal.narwal_client.models import RoomInfo

    c, clock = held
    c._task_recovery_attempts = 0
    c._terminal_recovery_next_at = clock[0] + 900
    c.active_room_clean_settings = {}
    c.client.state.map_data.rooms = [RoomInfo(room_id=3)]
    response = task_response()
    response.data["2"]["1"] = c.client.state.map_data.map_id
    c.client.get_current_task = AsyncMock(return_value=response)
    c._wait_for_task_recovery_retry = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await c._recover_active_task()
    c.client.get_current_task.assert_awaited_once()
    assert 3 in c.active_room_clean_settings
    c.client.get_clean_timeline.assert_not_awaited()
