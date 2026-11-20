"""Native clean-task phases, separate from legacy outer status values."""

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any


class CleanPhase(StrEnum):
    UNKNOWN = "unknown"
    PREPARING = "preparing"
    LEAVING_DOCK = "leaving_dock"
    CLEANING = "cleaning"
    RETURNING = "returning"
    RETURNING_TO_WASH = "returning_to_wash"
    RETURNING_TO_CHARGE = "returning_to_charge"
    WASHING_MOPS = "washing_mops"
    CHARGING_TO_RESUME = "charging_to_resume"
    DOCKING = "docking"
    EMPTYING = "emptying"


@dataclass(frozen=True)
class CleanTaskStatus:
    task_type: int
    phase_code: int
    phase: CleanPhase
    charging_stage: int | None = None


def dock_contact(decoded: dict[str, Any]) -> bool | None:
    """Use hardware fields, not the task-specific nested enums, for contact."""
    contact, charging = decoded.get("11"), decoded.get("47")
    contact = contact if type(contact) is int else None
    charging = charging if type(charging) is int else None
    if contact == 1 or charging == 2:
        return False
    if contact in (2, 3) or charging in (1, 3):
        return True
    return None


def clean_task_status(
    decoded: dict[str, Any], previous: CleanTaskStatus | None = None
) -> CleanTaskStatus | None:
    """Decode supported task layouts; omissions retain the same task's phase."""
    status = decoded.get("3")
    if isinstance(status, list):
        status = status[0] if status else None
    if not isinstance(status, dict):
        if previous is None or previous.task_type != 4 or dock_contact(decoded) is None:
            return previous
        status = {}
    task = status.get("1", previous.task_type if previous else None)
    if type(task) is not int:
        return previous
    same_task = previous is not None and task == previous.task_type
    key = {2: "4", 4: "7", 5: "6", 17: "17"}.get(task)
    if key is None:
        if previous is not None and not is_terminal_status(decoded):
            return replace(previous, phase=CleanPhase.UNKNOWN)
        return None if "1" in status else previous
    code = status.get(key, previous.phase_code if same_task else None)
    if type(code) is not int:
        if task == 4 and not same_task:
            return replace(previous, phase=CleanPhase.UNKNOWN) if previous is not None else None
        if previous is not None:
            return previous if same_task else CleanTaskStatus(task, -1, CleanPhase.UNKNOWN)
        return None
    if task == 4 and not same_task and code not in (6, 7):
        return replace(previous, phase=CleanPhase.UNKNOWN) if previous is not None else None
    # Layouts are family-specific: composite9 means mopping, not a terminal
    # status; its15 is departure, whereas normal sweep-and-mop uses11.
    phases = {
        2: {
            3: CleanPhase.CLEANING,
            6: CleanPhase.DOCKING,
            7: CleanPhase.LEAVING_DOCK,
            8: CleanPhase.PREPARING,
            12: CleanPhase.EMPTYING,
        },
        4: {
            7: CleanPhase.CLEANING,
        },
        5: {
            1: CleanPhase.RETURNING,
            2: CleanPhase.RETURNING_TO_WASH,
            4: CleanPhase.WASHING_MOPS,
            7: CleanPhase.CLEANING,
            10: CleanPhase.DOCKING,
            11: CleanPhase.LEAVING_DOCK,
            12: CleanPhase.PREPARING,
            16: CleanPhase.EMPTYING,
        },
        17: {
            1: CleanPhase.RETURNING,
            2: CleanPhase.RETURNING_TO_WASH,
            4: CleanPhase.WASHING_MOPS,
            7: CleanPhase.CLEANING,
            9: CleanPhase.CLEANING,
            15: CleanPhase.LEAVING_DOCK,
            16: CleanPhase.PREPARING,
            19: CleanPhase.EMPTYING,
        },
    }
    # Value2/phase1 is also an old dock-idle encoding, not a demonstrated task.
    if task == 2 and code == 1:
        if previous is not None and not is_terminal_status(decoded):
            return replace(previous, phase=CleanPhase.UNKNOWN)
        return None
    stage = None
    phase = phases[task].get(code, CleanPhase.UNKNOWN)
    if task == 4 and code == 7 and dock_contact(decoded) is not False:
        if same_task and previous.phase == CleanPhase.CHARGING_TO_RESUME:
            return CleanTaskStatus(task, code, previous.phase, previous.charging_stage)
        if same_task and dock_contact(decoded) is None:
            if previous.phase != CleanPhase.CLEANING:
                phase = CleanPhase.UNKNOWN
        else:
            phase = CleanPhase.UNKNOWN
    if task in (4, 17) and code == 6:
        stage = status.get("11")
        if stage is None and same_task and previous.phase_code == code:
            stage = previous.charging_stage
        stage = stage if type(stage) is int else None
        if stage in (1, 2):
            phase = CleanPhase.RETURNING_TO_CHARGE
        elif stage == 3 and (
            dock_contact(decoded) is True
            or (
                same_task
                and previous.phase == CleanPhase.CHARGING_TO_RESUME
                and dock_contact(decoded) is None
            )
        ):
            phase = CleanPhase.CHARGING_TO_RESUME
    return CleanTaskStatus(task, code, phase, stage)


def is_terminal_status(decoded: dict[str, Any]) -> bool:
    """A docked idle or drying broadcast supersedes the old job counters."""
    status = decoded.get("3")
    if isinstance(status, list):
        status = status[0] if status else None
    return (
        isinstance(status, dict)
        and (
            (status.get("1") == 1 and status.get("3", 1) in (1, 2, 6))
            or (status.get("1") == 2 and status.get("4") == 1)
            or status.get("1") in (10, 14)
        )
        and dock_contact(decoded) is True
    )
