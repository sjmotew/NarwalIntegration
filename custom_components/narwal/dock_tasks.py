"""Narwal dock task definitions and command gates."""

from __future__ import annotations

from dataclasses import dataclass

from .narwal_client.const import ACTIVE_CLEANING_STATUSES, WorkingStatus
from .narwal_client.models import (
    DOCK_TASK_DRY_DOCK_BAG,
    DOCK_TASK_DRY_DUST_BIN,
    DOCK_TASK_DRY_MOP,
    DOCK_TASK_EMPTY_DUSTBIN,
    DOCK_TASK_WASH_MOP,
    NarwalState,
)


@dataclass(frozen=True)
class DockTaskDefinition:
    """Description of one supported dock task."""

    key: str
    translation_key: str
    action: str
    icon: str


DOCK_TASKS: tuple[DockTaskDefinition, ...] = (
    DockTaskDefinition(
        key=DOCK_TASK_EMPTY_DUSTBIN,
        translation_key=DOCK_TASK_EMPTY_DUSTBIN,
        action="empty_dustbin",
        icon="mdi:delete-empty",
    ),
    DockTaskDefinition(
        key=DOCK_TASK_WASH_MOP,
        translation_key=DOCK_TASK_WASH_MOP,
        action="wash_mop_by_robot_status",
        icon="mdi:waves-arrow-up",
    ),
    DockTaskDefinition(
        key=DOCK_TASK_DRY_MOP,
        translation_key=DOCK_TASK_DRY_MOP,
        action="dry_mop",
        icon="mdi:fan",
    ),
    DockTaskDefinition(
        key=DOCK_TASK_DRY_DUST_BIN,
        translation_key=DOCK_TASK_DRY_DUST_BIN,
        action="dry_dust_bag",
        icon="mdi:air-filter",
    ),
    DockTaskDefinition(
        key=DOCK_TASK_DRY_DOCK_BAG,
        translation_key=DOCK_TASK_DRY_DOCK_BAG,
        action="dry_station_bag",
        icon="mdi:shield-sun-outline",
    ),
)

GENERIC_STOP_DOCK_TASKS = frozenset(
    {
        DOCK_TASK_EMPTY_DUSTBIN,
        DOCK_TASK_WASH_MOP,
        DOCK_TASK_DRY_MOP,
    }
)
SCOPED_STOP_DOCK_TASKS = frozenset({DOCK_TASK_DRY_DOCK_BAG, DOCK_TASK_DRY_DUST_BIN})
STOPPABLE_DOCK_TASKS = GENERIC_STOP_DOCK_TASKS | SCOPED_STOP_DOCK_TASKS
ROBOT_RETURN_COMPATIBLE_DOCK_TASKS = frozenset({DOCK_TASK_DRY_DOCK_BAG})
ROBOT_START_COMPATIBLE_DOCK_TASKS = frozenset(
    {DOCK_TASK_DRY_MOP, DOCK_TASK_DRY_DUST_BIN, DOCK_TASK_DRY_DOCK_BAG}
)
ROBOT_START_STOP_REQUIRED_DOCK_TASKS = frozenset({DOCK_TASK_EMPTY_DUSTBIN, DOCK_TASK_WASH_MOP})


def has_blocking_error(state: NarwalState | None) -> bool:
    """Return True when the robot reports a fault that should block commands."""
    return state is None or state.working_status == WorkingStatus.ERROR or state.has_error


def is_clean_session_context(state: NarwalState | None) -> bool:
    """Return True while robot-side clean task context is still current."""
    if state is None:
        return False
    return (
        state.is_cleaning
        or state.has_assumed_robot_clean
        or state.working_status in ACTIVE_CLEANING_STATUSES
        or (
            state.working_status == WorkingStatus.TASK_COMPLETED
            and not state.has_current_dock_presence_signal
        )
        or state.has_recent_active_working_status
        or state.has_paused_clean_task_context
        or state.is_returning
    )


def is_robot_work_context(state: NarwalState | None) -> bool:
    """Return True when generic force-end could affect robot-side work."""
    if state is None:
        return False
    return (
        state.is_cleaning
        or state.has_assumed_robot_clean
        or state.working_status in ACTIVE_CLEANING_STATUSES
        or state.has_recent_active_working_status
        or state.has_paused_clean_task_context
        or state.is_returning
    )


def has_active_dock_work(state: NarwalState | None) -> bool:
    """Return True when station work makes robot commands ambiguous."""
    if state is None:
        return False
    return (
        state.is_station_active
        or state.has_unmapped_active_dock_task
        or bool(state.active_dock_task_keys)
    )


def can_start_dock_task(state: NarwalState | None, task_key: str | None = None) -> bool:
    """Return True when a dock task can be started safely."""
    if has_blocking_error(state):
        return False
    if state.working_status == WorkingStatus.UNKNOWN:
        return False
    if not state.is_docked or is_clean_session_context(state):
        return False
    if state.has_unmapped_active_dock_task:
        return False
    if state.assumed_active_dock_task is not None:
        return False
    if task_key is None:
        return not state.active_dock_task_keys
    active_keys = set(state.active_dock_task_keys)
    if task_key in active_keys:
        return False
    # Default conservative policy: do not expose parallel starts until hardware
    # testing verifies an exact task combination.
    return not active_keys


def can_start_robot_clean(state: NarwalState | None) -> bool:
    """Return True when reported state permits sending a new robot clean."""
    if has_blocking_error(state):
        return False
    if state.working_status == WorkingStatus.UNKNOWN:
        return False
    if not state.is_docked or is_clean_session_context(state):
        return False
    if state.has_unmapped_active_dock_task or state.assumed_active_dock_task is not None:
        return False
    active_tasks = set(state.active_dock_task_keys)
    if active_tasks:
        return active_tasks <= ROBOT_START_COMPATIBLE_DOCK_TASKS and active_tasks <= set(
            state.telemetry_dock_task_keys
        )
    return not state.blocks_robot_start_for_dock_task


def dock_task_blocks_robot_return(state: NarwalState | None) -> bool:
    """Return True when dock work should block recalling an off-dock robot."""
    if state is None:
        return False
    if state.has_unmapped_active_dock_task:
        return True
    if state.assumed_active_dock_task is not None:
        return state.assumed_active_dock_task not in ROBOT_RETURN_COMPATIBLE_DOCK_TASKS
    if not state.is_station_active:
        return False
    active_tasks = set(state.active_dock_task_keys)
    if not active_tasks:
        return True
    return not active_tasks <= ROBOT_RETURN_COMPATIBLE_DOCK_TASKS


def can_stop_dock_task(state: NarwalState | None, task_key: str | None = None) -> bool:
    """Return True when a dock task can be stopped without ambiguity."""
    if has_blocking_error(state):
        return False
    if state.working_status == WorkingStatus.UNKNOWN:
        return False
    active_keys = state.active_dock_task_keys
    if not active_keys:
        return False
    if state.has_unmapped_active_dock_task:
        return task_key in SCOPED_STOP_DOCK_TASKS and task_key in active_keys
    if is_robot_work_context(state):
        return task_key in SCOPED_STOP_DOCK_TASKS and task_key in active_keys
    active_key_set = set(active_keys)
    telemetry_key_set = set(state.telemetry_dock_task_keys)
    if task_key is None:
        if len(active_key_set) != 1:
            return False
        active_key = next(iter(active_key_set))
        if active_key not in STOPPABLE_DOCK_TASKS:
            return False
        if active_key in SCOPED_STOP_DOCK_TASKS:
            return active_key in telemetry_key_set
        return state.is_docked and state.has_dock_presence_signal
    if task_key not in active_key_set or task_key not in STOPPABLE_DOCK_TASKS:
        return False
    if task_key in SCOPED_STOP_DOCK_TASKS:
        return task_key in telemetry_key_set
    if not (state.is_docked and state.has_dock_presence_signal):
        return False
    return active_key_set == {task_key}
