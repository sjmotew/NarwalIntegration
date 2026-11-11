"""Native job ownership when saved and live trail windows no longer overlap."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import importlib

import pytest
import tests.ha_stubs

tests.ha_stubs.install()
from custom_components.narwal.narwal_client import CommandResponse
from tests.test_coordinator import _new_coordinator, _trajectory_state, _trajectory_display

JOB = 1700000000000000001


def timeline(*events):
    return CommandResponse(
        result_code=1, data={"1": 1, "2": {"1": list(events) or [{"1": JOB, "2": 1}]}}
    )


@pytest.mark.parametrize("prefix", ["narwal_client", "custom_components.narwal.narwal_client"])
def test_exact_native_id_not_a_float(prefix):
    client = importlib.import_module(prefix + ".client").NarwalClient
    assert client.active_job_id_from_timeline(timeline()) == str(JOB)
    assert client.active_job_id_from_timeline(
        timeline({"1": JOB, "2": 1}, {"1": JOB + 1, "2": 4})
    ) == str(JOB)


@pytest.mark.parametrize("prefix", ["narwal_client", "custom_components.narwal.narwal_client"])
@pytest.mark.parametrize(
    "case",
    [
        "completed",
        "missing_start",
        "second_start",
        "unordered",
        "float",
        "bool",
        "negative",
        "rejected",
        "malformed",
    ],
)
def test_ambiguous_or_completed_timeline_has_no_active_id(prefix, case):
    response = timeline()
    if case == "completed":
        response = timeline({"1": JOB, "2": 1}, {"1": JOB + 1, "2": 6})
    elif case == "missing_start":
        response = timeline({"1": JOB, "2": 4})
    elif case == "second_start":
        response = timeline({"1": JOB, "2": 1}, {"1": JOB + 1, "2": 1})
    elif case == "unordered":
        response = timeline({"1": JOB, "2": 1}, {"1": JOB - 1, "2": 4})
    elif case == "float":
        response = timeline({"1": float(JOB), "2": 1})
    elif case == "bool":
        response = timeline({"1": True, "2": 1})
    elif case == "negative":
        response = timeline({"1": -1, "2": 1})
    elif case == "rejected":
        response.result_code = 4
    else:
        response.data = {"2": {"1": [None]}}
    client = importlib.import_module(prefix + ".client").NarwalClient
    assert client.active_job_id_from_timeline(response) is None


def owner():
    c = _new_coordinator()
    state = _trajectory_state()
    state.map_data.resolution = 60
    state.update_from_base_status({"3": {"1": 17, "17": 7}, "11": 1, "47": 2})
    state.map_display_data = _trajectory_display((0, 1), (1, 1), (2, 1), timestamp=100)
    c.client = SimpleNamespace(state=state, get_clean_timeline=AsyncMock(return_value=timeline()))
    c._retained_map_display = state.map_display_data
    c._retained_map_identity = c._static_map_identity(state)
    c._retained_trajectory_frame = c._trajectory_map_frame(state)
    c._retained_map_geometry_identity = c._static_map_geometry_identity(state)
    c.notifications = []
    c.async_set_updated_data = c.notifications.append
    c._schedule_map_display_cache_save = MagicMock()
    c._clean_session_active = True
    return c


@pytest.mark.asyncio
async def test_job_id_and_geometry_saved_together():
    c = owner()
    await c._async_recover_trajectory_job_id()
    payload = c._map_display_cache_payload(c.client.state)
    assert payload["job_id"] == str(JOB)
    assert payload["map_frame_v1"] == list(c._trajectory_map_frame(c.client.state))


@pytest.mark.asyncio
async def test_query_cannot_retro_label_restored_geometry():
    c = owner()
    payload = c._map_display_cache_payload(c.client.state)
    c.client.state.map_display_data = None
    assert c._restore_map_display_cache_payload(payload)
    await c._async_recover_trajectory_job_id()
    assert c._trajectory_job_id == str(JOB)
    assert c._map_display_cache_payload(c.client.state)["job_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["epoch", "map", "terminal"])
async def test_superseded_identity_query_is_discarded(change):
    c = owner()

    async def changed():
        if change == "epoch":
            c._active_context_revision += 1
        elif change == "map":
            c.client.state.map_data = replace(c.client.state.map_data, width=101)
        else:
            c.client.state.update_from_base_status({"3": {"1": 1, "3": 1}, "11": 3, "47": 1})
        return timeline()

    c.client.get_clean_timeline.side_effect = changed
    await c._async_recover_trajectory_job_id()
    assert getattr(c, "_trajectory_job_id", None) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("ownership", ["same", "different", "missing", "geometry"])
async def test_restart_gap_only_retains_verified_same_job(ownership):
    c = owner()
    await c._async_recover_trajectory_job_id()
    payload = deepcopy(c._map_display_cache_payload(c.client.state))
    if ownership == "different":
        payload["job_id"] = str(JOB - 100)
    elif ownership == "missing":
        payload.pop("job_id")
    elif ownership == "geometry":
        payload["map_frame_v1"][1] += 1
    c._retained_map_display = None
    c.client.state.map_display_data = None
    restored = c._restore_map_display_cache_payload(payload)
    if ownership in ("different", "geometry"):
        assert not restored
    else:
        assert restored
    c.client.state.map_display_data = _trajectory_display((20, 1), (21, 1), (22, 1), timestamp=200)
    c._retain_native_trajectory(c.client.state)
    display = c.client.state.map_display_data
    if ownership == "same":
        assert len(display.trajectory_x_values) == 6 * 4
        assert display.trajectory_breaks == (3,)
    else:
        assert len(display.trajectory_x_values) == 3 * 4
        assert display.trajectory_breaks == ()


@pytest.mark.asyncio
async def test_new_accepted_start_clears_old_identity():
    c = owner()
    await c._async_recover_trajectory_job_id()
    c._task_recovery_attempts = 3
    c.record_accepted_clean_start({})
    assert c._trajectory_job_id is None
    assert c._retained_trajectory_job_id is None
    assert c._task_recovery_attempts == 0


@pytest.mark.asyncio
async def test_late_identity_recovers_labelled_pending_prefix():
    c = owner()
    await c._async_recover_trajectory_job_id()
    payload = c._map_display_cache_payload(c.client.state)
    c._trajectory_job_id = None
    c._retained_trajectory_job_id = None
    c.client.state.map_display_data = None
    assert c._restore_map_display_cache_payload(payload)
    c.client.state.map_display_data = _trajectory_display((20, 1), (21, 1), (22, 1), timestamp=200)
    c._retain_native_trajectory(c.client.state)
    assert len(c.client.state.map_display_data.trajectory_x_values) == 12
    await c._async_recover_trajectory_job_id()
    assert len(c.client.state.map_display_data.trajectory_x_values) == 24
    assert c.client.state.map_display_data.trajectory_breaks == (3,)


@pytest.mark.asyncio
async def test_matching_identity_does_not_append_stale_window():
    c = owner()
    await c._async_recover_trajectory_job_id()
    payload = c._map_display_cache_payload(c.client.state)
    c.client.state.map_display_data = None
    assert c._restore_map_display_cache_payload(payload)
    c.client.state.map_display_data = _trajectory_display((20, 1), (21, 1), (22, 1), timestamp=50)
    c._retain_native_trajectory(c.client.state)
    assert c.client.state.map_display_data.timestamp == 100
    assert c.client.state.map_display_data.trajectory_breaks == ()


@pytest.mark.asyncio
async def test_late_mismatched_id_drops_unverified_route_even_if_it_overlapped():
    c = owner()
    await c._async_recover_trajectory_job_id()
    payload = c._map_display_cache_payload(c.client.state)
    payload["job_id"] = str(JOB - 100)
    c._trajectory_job_id = None
    c.client.state.map_display_data = None
    assert c._restore_map_display_cache_payload(payload)
    await c._async_recover_trajectory_job_id()
    assert not c.client.state.map_display_data.has_trajectory
    assert c._retained_trajectory_job_id == str(JOB)


@pytest.mark.asyncio
async def test_late_mismatch_keeps_current_native_windows():
    c = owner()
    await c._async_recover_trajectory_job_id()
    payload = c._map_display_cache_payload(c.client.state)
    payload["job_id"] = str(JOB - 10000000000)
    c._trajectory_job_id = None
    c.client.state.map_display_data = None
    assert c._restore_map_display_cache_payload(payload)
    c.client.state.map_display_data = _trajectory_display(
        (20, 1), (21, 1), (22, 1), timestamp=JOB // 1000000 + 1
    )
    c._retain_native_trajectory(c.client.state)
    c.client.state.map_display_data = _trajectory_display(
        (21, 1), (22, 1), (23, 1), timestamp=JOB // 1000000 + 2
    )
    c._retain_native_trajectory(c.client.state)
    await c._async_recover_trajectory_job_id()
    assert len(c.client.state.map_display_data.trajectory_x_values) == 4 * 4
    assert c.client.state.map_display_data.trajectory_breaks == ()


@pytest.mark.asyncio
async def test_identity_recovery_does_not_require_rooms_and_notifies():
    c = owner()
    c.client.state.map_data.rooms = []
    c.client.get_current_task = AsyncMock()
    await c._async_recover_active_clean_context()
    assert c._trajectory_job_id == str(JOB)
    c.client.get_current_task.assert_not_called()
    assert c.notifications == [c.client.state]
    assert not c._needs_active_task_recovery(c.client.state)


@pytest.mark.asyncio
async def test_unlabelled_restore_is_not_relabelled_after_overlap():
    c = owner()
    payload = c._map_display_cache_payload(c.client.state)
    c.client.state.map_display_data = None
    assert c._restore_map_display_cache_payload(payload)
    c.client.state.map_display_data = _trajectory_display(
        (0, 1), (1, 1), (2, 1), (3, 1), timestamp=200
    )
    c._retain_native_trajectory(c.client.state)
    assert not c._map_display_cache_restored_from_active
    await c._async_recover_trajectory_job_id()
    assert c._map_display_cache_payload(c.client.state)["job_id"] is None


@pytest.mark.asyncio
async def test_pending_ownership_blocks_every_cache_writer():
    c = owner()
    c._pending_job_trajectory_cache = {"job_id": str(JOB)}
    c._map_display_cache_store = SimpleNamespace(async_save=AsyncMock())
    assert c._persistable_map_display_cache_snapshot(c.client.state) is None
    with pytest.raises(RuntimeError, match="ownership"):
        await c._async_write_map_display_cache({"trajectory_x_values": "unverified"})
    c._map_display_cache_store.async_save.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_oversized_native_window_is_bounded():
    c = owner()
    c._pending_job_trajectory_cache = {"job_id": str(JOB)}
    from collections import deque

    c._unverified_live_windows = deque(maxlen=2048)
    c.client.state.map_display_data = _trajectory_display(
        *[(float(i), 1) for i in range(50001)], timestamp=200
    )
    c._retain_native_trajectory(c.client.state)
    assert (
        sum(
            len(x.trajectory_x_values) + len(x.trajectory_y_values)
            for x in c._unverified_live_windows
        )
        <= 50000 * 8
    )


@pytest.mark.asyncio
async def test_exhausted_identity_budget_releases_durable_cache_guard():
    c = owner()
    c._pending_job_trajectory_cache = {"job_id": str(JOB)}
    c.client.state.map_data.rooms = []
    c.client.get_clean_timeline.side_effect = TimeoutError
    for attempt in range(3):
        c._task_recovery_next_at = 0
        await c._async_recover_active_clean_context()
        if attempt < 2:
            assert c._pending_job_trajectory_cache is not None
    assert c._pending_job_trajectory_cache is None
    assert c._persistable_map_display_cache_snapshot(c.client.state) is not None
    assert c.client.get_clean_timeline.await_count == 3


@pytest.mark.asyncio
async def test_terminal_state_releases_pending_ownership():
    c = owner()
    c._pending_job_trajectory_cache = {"job_id": str(JOB)}
    c.client.state.update_from_base_status({"3": {"1": 1, "3": 1}, "11": 3, "47": 1})
    c._sync_active_clean_context(c.client.state)
    assert c._pending_job_trajectory_cache is None


@pytest.mark.asyncio
async def test_timestamp_free_live_window_is_preserved_without_claiming_ownership():
    c = owner()
    await c._async_recover_trajectory_job_id()
    payload = c._map_display_cache_payload(c.client.state)
    payload["job_id"] = str(JOB - 10000000000)
    c._trajectory_job_id = None
    c.client.state.map_display_data = None
    assert c._restore_map_display_cache_payload(payload)
    c.client.state.map_display_data = _trajectory_display((20, 1), (21, 1), (22, 1))
    c._retain_native_trajectory(c.client.state)
    await c._async_recover_trajectory_job_id()
    assert len(c.client.state.map_display_data.trajectory_x_values) == 12
    assert c._map_display_cache_payload(c.client.state)["job_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("during_query", [False, True])
async def test_unknown_is_not_a_terminal_ownership_release(during_query):
    c = owner()
    c._pending_job_trajectory_cache = {"job_id": str(JOB)}
    from custom_components.narwal.narwal_client import NarwalState

    old = c.client.state
    unknown = NarwalState(map_data=old.map_data, map_display_data=old.map_display_data)
    if during_query:
        c.client.state.map_data.rooms = []

        async def unknown_reply():
            c.client.state = unknown
            return timeline()

        c.client.get_clean_timeline.side_effect = unknown_reply
        await c._async_recover_active_clean_context()
    else:
        c.client.state = unknown
        c._sync_active_clean_context(unknown)
    assert c._pending_job_trajectory_cache is not None
    assert c._persistable_map_display_cache_snapshot(unknown) is None


@pytest.mark.parametrize("same_route", [True, False])
def test_completed_cache_restores_only_overlapping_live_tail(same_route):
    c = owner()
    c.client.state.map_display_data = _trajectory_display(
        (0, 1), (1, 1), (2, 1), (3, 1), timestamp=100
    )
    payload = c._map_display_cache_payload(c.client.state)
    payload["active_clean"] = False
    c._retained_map_display = None
    c.client.state.update_from_base_status({"3": {"1": 1, "3": 1}, "11": 3, "47": 1})
    c.client.state.map_display_data = _trajectory_display(
        *((1, 1), (2, 1), (3, 1)) if same_route else ((20, 1), (21, 1), (22, 1)), timestamp=200
    )
    assert c._restore_map_display_cache_with_live_trajectory(payload) is same_route
    assert len(c.client.state.map_display_data.trajectory_x_values) == (4 if same_route else 3) * 4


@pytest.mark.asyncio
async def test_changed_bitmap_keeps_legacy_labelled_route_in_same_frame():
    c = owner()
    await c._async_recover_trajectory_job_id()
    payload = c._map_display_cache_payload(c.client.state)
    payload.pop("map_frame_v1")
    payload["map_geometry"] = list(c._static_map_geometry_identity(c.client.state))
    c._trajectory_job_id = None
    c.client.state.map_data = replace(c.client.state.map_data, compressed_map=b"new pixels")
    c.client.state.map_display_data = None
    assert c._restore_map_display_cache_payload(payload)
    c.client.state.map_display_data = _trajectory_display((20, 1), (21, 1), (22, 1), timestamp=200)
    c._retain_native_trajectory(c.client.state)
    await c._async_recover_trajectory_job_id()
    assert len(c.client.state.map_display_data.trajectory_x_values) == 6 * 4
    assert c.client.state.map_display_data.trajectory_breaks == (3,)
    assert c._map_display_cache_payload(c.client.state)["job_id"] == str(JOB)


@pytest.mark.asyncio
async def test_bitmap_refresh_during_query_does_not_change_coordinate_frame():
    c = owner()

    async def refreshed():
        c.client.state.map_data = replace(c.client.state.map_data, compressed_map=b"new pixels")
        return timeline()

    c.client.get_clean_timeline.side_effect = refreshed
    await c._async_recover_trajectory_job_id()
    assert c._trajectory_job_id == str(JOB)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field", ["map_id", "created_at", "width", "height", "resolution", "origin_x", "origin_y"]
)
async def test_changed_coordinate_frame_rejects_old_route(field):
    c = owner()
    await c._async_recover_trajectory_job_id()
    payload = c._map_display_cache_payload(c.client.state)
    c.client.state.map_display_data = None
    setattr(c.client.state.map_data, field, getattr(c.client.state.map_data, field) + 1)
    assert not c._restore_map_display_cache_payload(payload)


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True])
def test_invalid_coordinate_frame_is_not_trusted(value):
    c = owner()
    c.client.state.map_data.resolution = value
    assert c._trajectory_map_frame(c.client.state) is None
