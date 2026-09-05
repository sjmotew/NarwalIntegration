"""Tests for Narwal dock task entities and command gates."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import tests.ha_stubs

tests.ha_stubs.install()

from homeassistant.exceptions import HomeAssistantError  # noqa: E402

from custom_components.narwal.dock_tasks import (  # noqa: E402
    can_start_dock_task,
    can_start_robot_clean,
    can_stop_dock_task,
    dock_task_blocks_robot_return,
)
from custom_components.narwal.switch import (  # noqa: E402
    DOCK_TASK_SWITCHES,
    NarwalDockTaskSwitch,
)
from narwal_client.const import CommandResult, WorkingStatus  # noqa: E402
from narwal_client.models import (  # noqa: E402
    DOCK_TASK_DRY_DOCK_BAG,
    DOCK_TASK_DRY_DUST_BIN,
    DOCK_TASK_DRY_MOP,
    DOCK_TASK_EMPTY_DUSTBIN,
    DOCK_TASK_WASH_MOP,
    CommandResponse,
    NarwalState,
)


def _docked_state() -> NarwalState:
    """Return an idle on-dock state."""
    state = NarwalState(working_status=WorkingStatus.DOCKED)
    state.dock_presence = 6
    state.dock_field11 = 2
    state.dock_field47 = 3
    return state


def _coordinator(state: NarwalState | None = None) -> MagicMock:
    """Return a minimal coordinator stub for switch entity tests."""
    state = state or _docked_state()
    coordinator = MagicMock()
    coordinator.config_entry.data = {"device_id": "test_device", "model": "flow"}
    coordinator.config_entry.title = "Narwal Test"
    coordinator.config_entry.options = {}
    coordinator.client.state = state
    coordinator.client.robot_awake = True
    coordinator.data = state
    coordinator.last_update_success = True
    coordinator.has_fresh_state = True
    coordinator.async_refresh_dock_status = AsyncMock(return_value=True)
    coordinator.dock_action_lock = asyncio.Lock()
    return coordinator


def _switch(task_key: str, state: NarwalState | None = None) -> NarwalDockTaskSwitch:
    """Build one dock task switch by key."""
    descriptions = {description.key: description for description in DOCK_TASK_SWITCHES}
    return NarwalDockTaskSwitch(_coordinator(state), descriptions[task_key])


def test_five_dock_task_switches_are_exposed() -> None:
    """The dock exposes exactly the five app-visible task controls."""
    assert [description.key for description in DOCK_TASK_SWITCHES] == [
        DOCK_TASK_EMPTY_DUSTBIN,
        DOCK_TASK_WASH_MOP,
        DOCK_TASK_DRY_MOP,
        DOCK_TASK_DRY_DUST_BIN,
        DOCK_TASK_DRY_DOCK_BAG,
    ]


def test_dock_task_switch_belongs_to_dock_device() -> None:
    """Dock task controls are grouped under the dock device."""
    switch = _switch(DOCK_TASK_EMPTY_DUSTBIN)

    assert switch._attr_device_info["identifiers"] == {("narwal", "test_device_dock")}
    assert switch._attr_device_info["via_device"] == ("narwal", "test_device")


def test_idle_docked_state_can_start_any_single_task() -> None:
    """An idle dock exposes start controls for all known tasks."""
    state = _docked_state()

    assert can_start_dock_task(state)
    assert all(
        can_start_dock_task(state, description.key)
        for description in DOCK_TASK_SWITCHES
    )


def test_docked_v2_with_off_dock_fields_hides_dock_start_controls() -> None:
    """A coarse docked status is not enough when dock telemetry says off-dock."""
    state = NarwalState()
    state.update_from_base_status({"3": {"1": 2}, "11": 1, "47": 2})

    assert not state.is_docked
    assert not can_start_dock_task(state, DOCK_TASK_EMPTY_DUSTBIN)


def test_cleaning_state_hides_dock_start_controls() -> None:
    """Robot cleaning context blocks dock starts."""
    state = NarwalState(working_status=WorkingStatus.CLEANING)
    state.dock_presence = 2
    state.dock_field11 = 1
    state.dock_field47 = 2

    assert not can_start_dock_task(state, DOCK_TASK_EMPTY_DUSTBIN)


def test_retained_paused_context_hides_dock_start_controls() -> None:
    """A paused clean remains robot work after live metric freshness expires."""
    state = _docked_state()
    state.working_status = WorkingStatus.STANDBY
    state.is_paused = True
    state.cleaning_time = 120
    state.task_elapsed_time = 120

    assert state.has_paused_clean_task_context
    assert not can_start_dock_task(state, DOCK_TASK_EMPTY_DUSTBIN)


def test_task_completed_docked_state_exposes_dock_start_controls() -> None:
    """A seated robot releases dock controls despite retained task status."""
    state = NarwalState()
    state.update_from_base_status(
        {"3": {"1": int(WorkingStatus.TASK_COMPLETED), "3": 6}, "11": 2}
    )

    assert can_start_dock_task(state, DOCK_TASK_EMPTY_DUSTBIN)


def test_task_completed_off_dock_state_hides_dock_start_controls() -> None:
    """TASK_COMPLETED remains return-to-dock context until physically seated."""
    state = NarwalState(working_status=WorkingStatus.TASK_COMPLETED)
    state.dock_presence = 2
    state.dock_field11 = 1
    state.dock_field47 = 2

    assert not can_start_dock_task(state, DOCK_TASK_EMPTY_DUSTBIN)


def test_unknown_working_status_hides_dock_start_controls() -> None:
    """Unknown robot status is not treated as safe dock-idle state."""
    state = _docked_state()
    state.working_status = WorkingStatus.UNKNOWN

    assert not can_start_dock_task(state, DOCK_TASK_EMPTY_DUSTBIN)
    assert not can_start_robot_clean(state)
    assert not can_stop_dock_task(state, DOCK_TASK_EMPTY_DUSTBIN)


def test_assumed_robot_clean_hides_dock_start_controls() -> None:
    """An accepted robot start blocks dock starts until telemetry catches up."""
    state = _docked_state()
    state.assume_robot_clean()

    assert not can_start_dock_task(state, DOCK_TASK_EMPTY_DUSTBIN)
    assert not can_start_robot_clean(state)


def test_active_known_task_is_on_and_stoppable() -> None:
    """Coarse station activity maps to the relevant dock task switch."""
    state = _docked_state()
    state.station_activity = 1

    empty = _switch(DOCK_TASK_EMPTY_DUSTBIN, state)
    wash = _switch(DOCK_TASK_WASH_MOP, state)

    assert empty.is_on
    assert empty.available
    assert not wash.is_on
    assert not wash.available


def test_active_switch_is_unavailable_until_its_stop_is_safe() -> None:
    """An active switch cannot advertise a stop that its service rejects."""
    state = _docked_state()
    state.assume_dock_task(DOCK_TASK_DRY_DOCK_BAG)
    switch = _switch(DOCK_TASK_DRY_DOCK_BAG, state)

    assert switch.is_on
    assert not switch.available

    state.set_dock_drying_task(
        DOCK_TASK_DRY_DOCK_BAG,
        elapsed=45,
        target=180,
        fields=("12", "13"),
    )
    assert switch.available


def test_multi_task_switch_is_available_only_for_a_scoped_stop() -> None:
    """Parallel telemetry exposes only a task with an unambiguous stop payload."""
    state = _docked_state()
    state.set_dock_drying_task(
        DOCK_TASK_DRY_MOP,
        elapsed=45,
        target=180,
        fields=("8", "9"),
    )
    state.set_dock_drying_task(
        DOCK_TASK_DRY_DOCK_BAG,
        elapsed=45,
        target=180,
        fields=("12", "13"),
    )

    assert not _switch(DOCK_TASK_DRY_MOP, state).available
    assert _switch(DOCK_TASK_DRY_DOCK_BAG, state).available


def test_dock_task_switch_available_to_refresh_stale_connected_state() -> None:
    """A connected switch remains actionable so its service can wake and refresh."""
    state = _docked_state()
    state.working_status = WorkingStatus.CLEANING
    coordinator = _coordinator(state)
    coordinator.has_fresh_state = False
    switch = NarwalDockTaskSwitch(coordinator, DOCK_TASK_SWITCHES[0])

    assert switch.available


def test_dock_task_switch_unavailable_when_disconnected() -> None:
    coordinator = _coordinator()
    coordinator.client.connected = False
    switch = NarwalDockTaskSwitch(coordinator, DOCK_TASK_SWITCHES[0])

    assert not switch.available


async def test_stale_dock_task_start_forces_wake_before_refresh() -> None:
    coordinator = _coordinator()
    coordinator.has_fresh_state = False
    coordinator.client.wake = AsyncMock(return_value=True)
    coordinator.client.empty_dustbin = AsyncMock(
        return_value=CommandResponse(result_code=CommandResult.SUCCESS)
    )
    switch = NarwalDockTaskSwitch(coordinator, DOCK_TASK_SWITCHES[0])

    await switch.async_turn_on()

    coordinator.client.wake.assert_awaited_once_with(timeout=10.0, force=True)
    coordinator.client.empty_dustbin.assert_awaited_once()


async def test_cached_active_dock_task_is_rechecked_before_start() -> None:
    """A stale active bit must not discard a start after refresh clears it."""
    state = _docked_state()
    state.assume_dock_task(DOCK_TASK_EMPTY_DUSTBIN)
    coordinator = _coordinator(state)
    coordinator.client.empty_dustbin = AsyncMock(
        return_value=CommandResponse(result_code=CommandResult.SUCCESS)
    )

    async def refresh_dock_status() -> bool:
        state.clear_assumed_dock_task(DOCK_TASK_EMPTY_DUSTBIN)
        return True

    coordinator.async_refresh_dock_status = AsyncMock(side_effect=refresh_dock_status)
    switch = NarwalDockTaskSwitch(coordinator, DOCK_TASK_SWITCHES[0])

    await switch.async_turn_on()

    coordinator.async_refresh_dock_status.assert_awaited()
    coordinator.client.empty_dustbin.assert_awaited_once()


async def test_stale_dock_bag_stop_does_not_force_wake_during_cleaning() -> None:
    """A live clean must not receive a disruptive forced wake burst."""
    state = _docked_state()
    state.working_status = WorkingStatus.CLEANING
    state.set_dock_drying_task(
        DOCK_TASK_DRY_DOCK_BAG,
        elapsed=45,
        target=180,
        fields=("12", "13"),
    )
    coordinator = _coordinator(state)
    coordinator.has_fresh_state = False
    coordinator.client.wake = AsyncMock(return_value=True)
    coordinator.client.stop_dock_task = AsyncMock(
        return_value=CommandResponse(result_code=CommandResult.SUCCESS)
    )
    switch = NarwalDockTaskSwitch(coordinator, DOCK_TASK_SWITCHES[4])

    await switch.async_turn_off()

    coordinator.client.wake.assert_not_awaited()
    coordinator.client.stop_dock_task.assert_awaited_once_with(
        DOCK_TASK_DRY_DOCK_BAG
    )


def test_unmapped_dock_activity_blocks_start_and_stop() -> None:
    """Unknown station activity is not treated as safe idle state."""
    state = _docked_state()
    state.station_activity = 99

    assert not can_start_dock_task(state, DOCK_TASK_EMPTY_DUSTBIN)
    assert not can_stop_dock_task(state)


def test_dock_task_attributes_use_timer_progress() -> None:
    """Task switches expose coarse time-left and percent progress attributes."""
    state = _docked_state()
    state.set_dock_drying_task(
        DOCK_TASK_DRY_MOP,
        elapsed=61,
        target=180,
        fields=("8", "9"),
    )
    switch = _switch(DOCK_TASK_DRY_MOP, state)

    assert switch.extra_state_attributes == {
        "time_left": "2m",
        "progress": 34,
    }


def test_dry_dust_bin_is_active_and_stoppable() -> None:
    """Dry dust-bin remains visible and can be stopped with its scoped command."""
    state = _docked_state()
    state.set_dock_drying_task(
        DOCK_TASK_DRY_DUST_BIN,
        elapsed=61,
        target=180,
        fields=("10", "11"),
    )
    switch = _switch(DOCK_TASK_DRY_DUST_BIN, state)

    assert switch.is_on
    assert switch.available
    assert can_stop_dock_task(state)
    assert can_stop_dock_task(state, DOCK_TASK_DRY_DUST_BIN)
    assert switch.extra_state_attributes == {
        "time_left": "2m",
        "progress": 34,
    }


async def test_active_dry_dust_bin_switch_stops_with_scoped_command() -> None:
    """The dry dust-bin switch uses the validated scoped stop path."""
    state = _docked_state()
    state.set_dock_drying_task(
        DOCK_TASK_DRY_DUST_BIN,
        elapsed=61,
        target=180,
        fields=("10", "11"),
    )
    coordinator = _coordinator(state)
    coordinator.client.stop_dock_task = AsyncMock(
        return_value=CommandResponse(result_code=CommandResult.SUCCESS)
    )
    switch = NarwalDockTaskSwitch(coordinator, DOCK_TASK_SWITCHES[3])

    await switch.async_turn_off()

    coordinator.client.stop_dock_task.assert_awaited_once_with(DOCK_TASK_DRY_DUST_BIN)


async def test_switch_refreshes_before_stop_validation() -> None:
    """A stale local status cannot reject a typed stop before refresh."""
    state = _docked_state()
    state.working_status = WorkingStatus.UNKNOWN
    state.set_dock_drying_task(
        DOCK_TASK_DRY_DUST_BIN,
        elapsed=61,
        target=180,
        fields=("10", "11"),
    )
    coordinator = _coordinator(state)

    async def refresh_dock_status() -> bool:
        state.working_status = WorkingStatus.DOCKED
        return True

    coordinator.async_refresh_dock_status = AsyncMock(side_effect=refresh_dock_status)
    coordinator.client.stop_dock_task = AsyncMock(
        return_value=CommandResponse(result_code=CommandResult.SUCCESS)
    )
    switch = NarwalDockTaskSwitch(coordinator, DOCK_TASK_SWITCHES[3])

    await switch.async_turn_off()

    coordinator.async_refresh_dock_status.assert_awaited()
    coordinator.client.stop_dock_task.assert_awaited_once_with(DOCK_TASK_DRY_DUST_BIN)


def test_multiple_tasks_only_allow_scoped_stop() -> None:
    """Generic stop is unavailable for ambiguous multi-task dock activity."""
    state = _docked_state()
    state.set_dock_drying_task(
        DOCK_TASK_DRY_MOP,
        elapsed=30,
        target=180,
        fields=("8", "9"),
    )
    state.set_dock_drying_task(
        DOCK_TASK_DRY_DOCK_BAG,
        elapsed=45,
        target=180,
        fields=("12", "13"),
    )

    assert not can_stop_dock_task(state)
    assert not can_stop_dock_task(state, DOCK_TASK_DRY_MOP)
    assert can_stop_dock_task(state, DOCK_TASK_DRY_DOCK_BAG)

    state.set_dock_drying_task(
        DOCK_TASK_DRY_DUST_BIN,
        elapsed=45,
        target=180,
        fields=("10", "11"),
    )
    assert can_stop_dock_task(state, DOCK_TASK_DRY_DUST_BIN)


def test_clean_session_context_rejects_unscoped_dock_stop() -> None:
    """Generic force-end must not be exposed while robot work is current."""
    state = _docked_state()
    state.working_status = WorkingStatus.CLEANING
    state.station_activity = 1

    assert not can_stop_dock_task(state)
    assert not can_stop_dock_task(state, DOCK_TASK_EMPTY_DUSTBIN)


def test_task_completed_allows_typed_empty_dustbin_stop() -> None:
    """Dock emptying reports TASK_COMPLETED while its generic stop is valid."""
    state = _docked_state()
    state.working_status = WorkingStatus.TASK_COMPLETED
    state.station_activity = 1

    assert can_stop_dock_task(state)
    assert can_stop_dock_task(state, DOCK_TASK_EMPTY_DUSTBIN)


def test_task_completed_rejects_empty_dustbin_stop_without_dock_proof() -> None:
    """Generic dock stop is unsafe without an explicit dock presence signal."""
    state = NarwalState(working_status=WorkingStatus.TASK_COMPLETED)
    state.station_activity = 1

    assert not can_stop_dock_task(state)
    assert not can_stop_dock_task(state, DOCK_TASK_EMPTY_DUSTBIN)


def test_scoped_dry_stop_requires_telemetry_not_only_assumption() -> None:
    """Scoped force-end is hidden until the dry task is typed by telemetry."""
    state = _docked_state()
    state.assume_dock_task(DOCK_TASK_DRY_DOCK_BAG)

    assert not can_stop_dock_task(state, DOCK_TASK_DRY_DOCK_BAG)

    state.set_dock_drying_task(
        DOCK_TASK_DRY_DOCK_BAG,
        elapsed=45,
        target=180,
        fields=("12", "13"),
    )
    assert can_stop_dock_task(state, DOCK_TASK_DRY_DOCK_BAG)


def test_clean_session_context_allows_scoped_dock_bag_stop() -> None:
    """The scoped dock-bag payload remains safe during robot-side work."""
    state = _docked_state()
    state.working_status = WorkingStatus.TASK_COMPLETED
    state.set_dock_drying_task(
        DOCK_TASK_DRY_DOCK_BAG,
        elapsed=45,
        target=180,
        fields=("12", "13"),
    )

    assert can_stop_dock_task(state, DOCK_TASK_DRY_DOCK_BAG)


def test_unmapped_coarse_activity_allows_scoped_dock_bag_stop() -> None:
    """Typed dock-bag telemetry can still be force-ended with stale coarse fields."""
    state = _docked_state()
    state.station_activity = 99
    state.set_dock_drying_task(
        DOCK_TASK_DRY_DOCK_BAG,
        elapsed=45,
        target=180,
        fields=("12", "13"),
    )

    assert not can_stop_dock_task(state)
    assert can_stop_dock_task(state, DOCK_TASK_DRY_DOCK_BAG)
    assert not can_stop_dock_task(state, DOCK_TASK_DRY_MOP)


@pytest.mark.parametrize(
    ("task", "fields"),
    [
        (DOCK_TASK_DRY_MOP, ("8", "9")),
        (DOCK_TASK_DRY_DUST_BIN, ("10", "11")),
        (DOCK_TASK_DRY_DOCK_BAG, ("12", "13")),
    ],
)
def test_robot_clean_start_allows_typed_drying_tasks(
    task: str,
    fields: tuple[str, str],
) -> None:
    """Typed drying tasks can be handed off to the robot clean command."""
    state = _docked_state()
    state.assume_dock_task(task)
    assert not can_start_robot_clean(state)

    state = _docked_state()
    state.set_dock_drying_task(
        task,
        elapsed=45,
        target=180,
        fields=fields,
    )
    assert can_start_robot_clean(state)


def test_robot_clean_start_allows_multiple_typed_drying_tasks() -> None:
    """Multiple typed drying timers can be handed off on clean start."""
    state = _docked_state()
    state.set_dock_drying_task(
        DOCK_TASK_DRY_MOP,
        elapsed=45,
        target=180,
        fields=("8", "9"),
    )
    state.set_dock_drying_task(
        DOCK_TASK_DRY_DOCK_BAG,
        elapsed=45,
        target=180,
        fields=("12", "13"),
    )

    assert can_start_robot_clean(state)


def test_robot_clean_start_rejects_typed_drying_with_unmapped_activity() -> None:
    """Typed drying must not mask additional unmapped station work."""
    state = _docked_state()
    state.station_activity = 99
    state.set_dock_drying_task(
        DOCK_TASK_DRY_DOCK_BAG,
        elapsed=45,
        target=180,
        fields=("12", "13"),
    )

    assert not can_start_robot_clean(state)


def test_robot_return_allows_only_typed_dock_bag() -> None:
    """Robot return is blocked by dock work except typed dock-bag drying."""
    state = NarwalState(working_status=WorkingStatus.STANDBY)
    state.station_activity = 4
    assert dock_task_blocks_robot_return(state)

    state = NarwalState(working_status=WorkingStatus.STANDBY)
    state.set_dock_drying_task(
        DOCK_TASK_DRY_DOCK_BAG,
        elapsed=45,
        target=180,
        fields=("12", "13"),
    )
    assert not dock_task_blocks_robot_return(state)

    state = NarwalState(working_status=WorkingStatus.STANDBY)
    state.dock_presence = 6
    state.set_dock_drying_task(
        DOCK_TASK_DRY_MOP,
        elapsed=45,
        target=180,
        fields=("8", "9"),
    )
    assert dock_task_blocks_robot_return(state)


@pytest.mark.parametrize(
    "task",
    [
        DOCK_TASK_EMPTY_DUSTBIN,
        DOCK_TASK_WASH_MOP,
        DOCK_TASK_DRY_MOP,
        DOCK_TASK_DRY_DUST_BIN,
    ],
)
def test_accepted_dock_task_blocks_robot_return_during_handoff(task: str) -> None:
    """Accepted incompatible dock work blocks robot actions before telemetry."""
    state = NarwalState(working_status=WorkingStatus.STANDBY)
    state.assume_dock_task(task)

    assert dock_task_blocks_robot_return(state)


def test_accepted_dock_bag_dry_keeps_robot_return_compatible() -> None:
    """The proven dock-bag exception applies during its telemetry handoff too."""
    state = NarwalState(working_status=WorkingStatus.STANDBY)
    state.assume_dock_task(DOCK_TASK_DRY_DOCK_BAG)

    assert not dock_task_blocks_robot_return(state)


def test_unmapped_dock_timer_blocks_robot_return_without_coarse_activity() -> None:
    """Fresh unknown timer work blocks return despite an idle station field."""
    state = NarwalState(working_status=WorkingStatus.STANDBY)
    state.update_from_working_status({"14": 60, "15": 180})

    assert state.has_unmapped_active_dock_task
    assert not state.is_station_active
    assert dock_task_blocks_robot_return(state)


async def test_wash_mop_switch_uses_status_gated_command() -> None:
    """Wash mop uses the app-style status-gated dock command directly."""
    coordinator = _coordinator()
    coordinator.client.wash_mop = AsyncMock(
        return_value=CommandResponse(result_code=CommandResult.SUCCESS)
    )
    coordinator.client.wash_mop_by_robot_status = AsyncMock(
        return_value=CommandResponse(result_code=CommandResult.SUCCESS)
    )
    switch = NarwalDockTaskSwitch(coordinator, DOCK_TASK_SWITCHES[1])

    await switch.async_turn_on()

    coordinator.client.wash_mop.assert_not_awaited()
    coordinator.client.wash_mop_by_robot_status.assert_awaited_once()


async def test_successful_start_reserves_private_guard_when_post_refresh_fails() -> None:
    """Accepted starts block follow-up commands without publishing task state."""
    coordinator = _coordinator()
    coordinator.client.empty_dustbin = AsyncMock(
        side_effect=lambda: (
            coordinator.client.state.assume_dock_task(DOCK_TASK_EMPTY_DUSTBIN)
            or CommandResponse(result_code=CommandResult.SUCCESS)
        )
    )
    refresh_calls = 0

    async def refresh_dock_status() -> bool:
        nonlocal refresh_calls
        refresh_calls += 1
        if refresh_calls == 2:
            coordinator.has_fresh_state = False
            return False
        return True

    coordinator.async_refresh_dock_status = AsyncMock(side_effect=refresh_dock_status)
    switch = NarwalDockTaskSwitch(coordinator, DOCK_TASK_SWITCHES[0])

    await switch.async_turn_on()

    assert switch.is_on
    assert switch.available
    coordinator.client.empty_dustbin.assert_awaited_once()


async def test_concurrent_starts_are_serialized_by_local_reservation() -> None:
    """Two start requests cannot both dispatch against the same idle state."""
    coordinator = _coordinator()
    empty_switch = NarwalDockTaskSwitch(coordinator, DOCK_TASK_SWITCHES[0])
    wash_switch = NarwalDockTaskSwitch(coordinator, DOCK_TASK_SWITCHES[1])
    coordinator.client.empty_dustbin = AsyncMock(
        side_effect=lambda: (
            coordinator.client.state.assume_dock_task(DOCK_TASK_EMPTY_DUSTBIN)
            or CommandResponse(result_code=CommandResult.SUCCESS)
        )
    )
    coordinator.client.wash_mop = AsyncMock(
        return_value=CommandResponse(result_code=CommandResult.SUCCESS)
    )
    coordinator.async_refresh_dock_status = AsyncMock(return_value=True)

    results = await asyncio.gather(
        empty_switch.async_turn_on(),
        wash_switch.async_turn_on(),
        return_exceptions=True,
    )

    assert not isinstance(results[0], Exception)
    assert isinstance(results[1], HomeAssistantError)
    coordinator.client.empty_dustbin.assert_awaited_once()
    coordinator.client.wash_mop.assert_not_awaited()


async def test_switch_blocks_command_when_preflight_refresh_fails() -> None:
    """A failed pre-command state refresh prevents sending a start command."""
    coordinator = _coordinator()
    coordinator.async_refresh_dock_status = AsyncMock(return_value=False)
    coordinator.client.empty_dustbin = AsyncMock()
    switch = NarwalDockTaskSwitch(coordinator, DOCK_TASK_SWITCHES[0])

    with pytest.raises(HomeAssistantError):
        await switch.async_turn_on()

    coordinator.client.empty_dustbin.assert_not_awaited()
