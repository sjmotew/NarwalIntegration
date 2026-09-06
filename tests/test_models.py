"""Tests for narwal_client.models — state data models."""

from __future__ import annotations

import logging
import struct
from dataclasses import replace
from unittest.mock import patch

import pytest

from narwal_client.const import WorkingStatus
from narwal_client.models import (
    DOCK_TASK_DRY_DOCK_BAG,
    DOCK_TASK_DRY_DUST_BIN,
    DOCK_TASK_DRY_MOP,
    DOCK_TASK_EMPTY_DUSTBIN,
    DOCK_TASK_WASH_MOP,
    MapData,
    MapDisplayData,
    NarwalState,
    ObstacleInfo,
    RoomInfo,
    _parse_obstacles,
)


class TestNarwalState:
    """Tests for NarwalState data model."""

    def test_default_state(self) -> None:
        state = NarwalState()
        assert state.working_status == WorkingStatus.UNKNOWN
        assert state.battery_level == 0
        assert state.firmware_version == ""
        assert not state.is_cleaning
        assert not state.is_docked
        assert not state.is_returning

    def test_update_from_working_status(self) -> None:
        """working_status topic sets cleaning metrics without rewriting status."""
        state = NarwalState()
        # Field 2 = coveredArea (float32, m²); field 13 = totalDryStationBagTime, ignored.
        state.update_from_working_status(
            {"2": _float_to_uint32(12.5), "3": 120, "13": 18000}
        )
        assert state.cleaning_time == 120
        assert state.cleaning_area == 12.5
        assert state.working_status == WorkingStatus.UNKNOWN
        assert state.has_recent_active_working_status

    def test_working_status_station_timers_do_not_mark_cleaning(self) -> None:
        """Station-only working_status timers are not active clean telemetry."""
        state = NarwalState()
        state.update_from_working_status({"13": 18000})
        assert state.working_status == WorkingStatus.UNKNOWN
        assert state.last_active_working_status_time == 0.0
        assert not state.has_recent_active_working_status

    def test_robot_side_drying_ignores_stale_clean_counters(self) -> None:
        """Mop and robot-bin drying timers override prior clean metrics."""
        for payload in (
            {"3": 120, "8": 60, "9": 180},
            {"3": 120, "10": 60, "11": 180},
        ):
            state = NarwalState()
            state.update_from_base_status(
                {"3": {"1": 10, "3": 6}, "11": 2, "47": 3}
            )

            state.update_from_working_status(payload)

            assert not state.has_recent_active_working_status

    def test_dock_bag_drying_does_not_hide_live_clean_counters(self) -> None:
        """Dock-bag drying may continue while the robot cleans off-dock."""
        state = NarwalState()

        state.update_from_working_status(
            {"3": 120, "12": 60, "13": 180}
        )

        assert state.has_recent_active_working_status

    def test_working_status_maps_dock_task_timers(self) -> None:
        """Typed dock timer pairs expose task progress without marking cleaning."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 10, "3": 6}, "11": 2, "47": 3})

        state.update_from_working_status({"8": 90, "9": 300, "12": 60, "13": 180})

        assert state.active_dock_task_keys == (
            DOCK_TASK_DRY_MOP,
            DOCK_TASK_DRY_DOCK_BAG,
        )
        assert not state.is_cleaning
        timer = state.dock_task_timer(DOCK_TASK_DRY_DOCK_BAG)
        assert timer is not None
        assert timer.remaining == 120
        assert timer.progress_percent == 33

    def test_dry_dock_bag_timer_survives_robot_departure(self) -> None:
        """Dock-bag drying can continue after the robot leaves the dock."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 10, "3": 6}, "11": 2, "47": 3})
        state.update_from_working_status({"12": 60, "13": 180})

        state.update_from_base_status({"3": {"1": 4}, "11": 1, "47": 2})

        assert state.active_dock_task_keys == (DOCK_TASK_DRY_DOCK_BAG,)
        assert state.dock_task_timer(DOCK_TASK_DRY_DOCK_BAG) is not None

    def test_other_dock_timers_require_dock_presence(self) -> None:
        """Robot-owned dock timers are hidden once the robot is explicitly away."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 10, "3": 6}, "11": 2, "47": 3})
        state.update_from_working_status({"10": 60, "11": 180})

        state.update_from_base_status({"3": {"1": 4}, "11": 1, "47": 2})

        assert state.active_dock_task_keys == ()
        assert state.dock_task_timer(DOCK_TASK_DRY_DUST_BIN) is None

    def test_docked_v2_with_off_dock_fields_is_not_docked(self) -> None:
        """Explicit dock fields override a coarse DOCKED_V2 working status."""
        state = NarwalState()

        state.update_from_base_status({"3": {"1": 2}, "11": 1, "47": 2})

        assert state.working_status == WorkingStatus.DOCKED_V2
        assert state.has_explicit_off_dock_signal
        assert not state.is_docked

    def test_off_dock_fields_prevent_false_terminal_metric_suppression(self) -> None:
        """A coarse dock enum cannot suppress later live off-dock metrics."""
        state = NarwalState()

        state.update_from_base_status(
            {"3": {"1": 10, "3": 6, "10": 1}, "11": 2, "47": 3}
        )
        assert state.last_terminal_working_status_time > 0

        state.update_from_base_status({"3": {"1": 2}, "11": 1, "47": 2})
        state.update_from_working_status({"3": 120})

        assert state.has_explicit_off_dock_signal
        assert state.has_recent_active_working_status
        assert state.is_cleaning

    def test_off_dock_standby_clears_terminal_metric_suppression(self) -> None:
        """Explicit departure clears a prior terminal timestamp before metrics."""
        state = NarwalState()
        state.update_from_base_status(
            {"3": {"1": 10, "3": 6, "10": 1}, "11": 2, "47": 3}
        )
        assert state.last_terminal_working_status_time > 0

        state.update_from_base_status(
            {"3": {"1": 1, "3": 2, "10": 2}, "11": 1, "47": 2}
        )
        state.update_from_working_status({"3": 120})

        assert state.working_status == WorkingStatus.STANDBY
        assert state.has_explicit_off_dock_signal
        assert state.last_terminal_working_status_time == 0.0
        assert state.has_recent_active_working_status
        assert state.is_cleaning

    def test_nested_off_dock_fields_apply_before_terminal_classification(self) -> None:
        """Current nested presence overrides retained dock indicators."""
        state = NarwalState()
        state.update_from_base_status(
            {"3": {"1": 10, "3": 6, "10": 1}, "11": 2, "47": 3}
        )

        state.update_from_base_status({"3": {"1": 2, "3": 2, "10": 2}})
        state.update_from_working_status({"3": 120})

        assert state.has_explicit_off_dock_signal
        assert state.has_recent_active_working_status
        assert state.is_cleaning

    def test_sparse_docked_status_preserves_current_dock_evidence(self) -> None:
        """Retained off-dock fields cannot erase a newer confirmed docking."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 4, "3": 2, "10": 2}})
        state.update_from_base_status({"3": {"1": 10, "3": 6}})

        state.update_from_base_status({"3": {"1": 2}})

        assert not state.has_explicit_off_dock_signal
        assert state.is_docked

    def test_presence_only_departure_clears_retained_dock_evidence(self) -> None:
        """Current nested departure wins over omitted prior dock fields."""
        state = NarwalState()
        state.update_from_base_status(
            {"3": {"1": 10, "3": 6, "10": 1}, "11": 2, "47": 3}
        )

        state.update_from_base_status({"3": {"1": 2, "3": 2}})
        state.update_from_working_status({"3": 120})

        assert state.has_explicit_off_dock_signal
        assert state.has_recent_active_working_status
        assert state.is_cleaning

    def test_explicit_off_dock_fields_override_retained_dock_activity(self) -> None:
        """Coarse station activity cannot override current off-dock fields."""
        state = NarwalState()

        state.update_from_base_status(
            {"3": {"1": 1, "3": 2, "12": 6}, "11": 1, "47": 2}
        )

        assert state.has_explicit_off_dock_signal
        assert not state.is_docked

    def test_dock_task_timer_uses_latest_reported_snapshot(self) -> None:
        """Timer-backed dock tasks do not locally count down to completion."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 10, "3": 6}, "11": 2, "47": 3})
        state.set_dock_drying_task(
            DOCK_TASK_DRY_MOP,
            elapsed=10,
            target=20,
            fields=("8", "9"),
        )
        timer = state.dock_drying_tasks[DOCK_TASK_DRY_MOP]
        state.dock_drying_tasks[DOCK_TASK_DRY_MOP] = replace(
            timer,
            observed_at=timer.observed_at - 11,
        )
        timer = state.dock_drying_tasks[DOCK_TASK_DRY_MOP]

        assert timer.remaining == 10
        assert timer.progress_percent == 50
        assert state.active_dock_task_keys == (DOCK_TASK_DRY_MOP,)

    def test_zeroed_timers_do_not_create_dock_tasks(self) -> None:
        """Configured timer totals at zero elapsed are idle, even with field 19."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 10, "3": 6, "12": 4}, "11": 2})

        state.update_from_working_status(
            {"8": 0, "9": 300, "12": 0, "13": 180, "19": 1}
        )

        assert state.active_dock_task_keys == ()
        assert not state.has_unmapped_active_dock_task

    def test_idle_timer_snapshot_suppresses_stale_station_drying(self) -> None:
        """Fresh typed timer evidence beats stale coarse station drying state."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 10, "3": 6, "18": 4}, "11": 2})

        state.update_from_working_status({"8": 0, "9": 300})

        assert state.active_dock_task_keys == ()
        assert not state.has_unmapped_active_dock_task
        assert not state.is_station_active

    def test_nested_room_field_does_not_clear_dock_drying_timer(self) -> None:
        """A firmware-specific room payload in field 8 is not timer telemetry."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 10, "10": 1}, "11": 2})
        state.set_dock_drying_task(
            DOCK_TASK_DRY_MOP,
            elapsed=60,
            target=300,
            fields=("8", "9"),
        )

        state.update_from_working_status({"8": {"1": 4, "3": b"Kitchen"}})

        assert state.active_dock_task_keys == (DOCK_TASK_DRY_MOP,)
        assert not state.has_fresh_idle_dock_drying_snapshot

    def test_recent_clean_metrics_preserve_fresh_emptying_activity(self) -> None:
        """An intermediate empty phase remains active despite recent counters."""
        state = NarwalState(working_status=WorkingStatus.CLEANING)
        state.update_from_working_status({"3": 60})
        state.station_activity = 1

        assert state.has_recent_active_working_status
        assert state.is_station_active

    def test_unmapped_dock_timer_expires_by_freshness_window(self) -> None:
        """Unmapped timer fields stop blocking when their packet is stale."""
        state = NarwalState()
        state.update_from_working_status({"14": 60, "15": 180})
        state.dock_drying_status_time -= 181

        assert not state.has_unmapped_active_dock_task

    def test_zeroed_unmapped_timer_does_not_block_commands(self) -> None:
        """A zeroed unknown timer pair is a configured total, not active work."""
        state = NarwalState()
        state.update_from_working_status({"14": 0, "15": 180})

        assert not state.has_unmapped_active_dock_task

    def test_unmapped_dock_timer_blocks_commands(self) -> None:
        """Unmapped active timer pairs are kept as a blocking safety signal."""
        state = NarwalState()
        state.update_from_working_status({"14": 60, "15": 180})

        assert state.active_dock_task_keys == ()
        assert state.has_unmapped_active_dock_task

    def test_zeroed_task_metrics_do_not_mark_cleaning(self) -> None:
        """A zeroed session counter is not evidence of an active clean.

        Field presence alone must not flip the entity to cleaning — a docked
        robot reporting timeConsuming=0 would otherwise be shown as running.
        """
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 10, "10": 1}})
        assert state.is_docked

        state.update_from_working_status({"2": _float_to_uint32(0.0), "3": 0})

        assert not state.has_recent_active_working_status
        assert not state.is_cleaning
        assert state.is_docked
        assert state.working_status == WorkingStatus.DOCKED

    def test_working_status_decodes_progress_and_remaining_time(self) -> None:
        """working_status reports progress and remaining time for vacuum attrs."""
        state = NarwalState()

        state.update_from_working_status(
            {"1": _float_to_uint32(0.64), "3": 120, "4": 600}
        )

        assert state.task_progress_percent == 64
        assert state.cleaning_time == 120
        assert state.task_elapsed_time == 120
        assert state.task_remaining_time == 600

    def test_non_cleaning_base_status_clears_stale_task_details(self) -> None:
        """Progress/current-room fields from the prior clean should not leak."""
        state = NarwalState()
        state.task_progress_percent = 72
        state.task_elapsed_time = 900
        state.task_remaining_time = 300
        state.current_room_id = 4
        state.current_room_aux_name = "Kitchen"

        state.update_from_base_status({"3": {"1": 10, "10": 1}})

        assert state.task_progress_percent is None
        assert state.task_elapsed_time == 0
        assert state.task_remaining_time == 0
        assert state.current_room_id is None
        assert state.current_room_aux_name == ""

    def test_terminal_docked_status_clears_stale_paused_context(self) -> None:
        """A stale paused bit cannot keep a confirmed docked robot active."""
        state = NarwalState()
        state.last_active_working_status_time = 1.0
        state.task_progress_percent = 72
        state.task_elapsed_time = 900
        state.current_room_id = 4

        state.update_from_base_status(
            {"3": {"1": 10, "2": 1, "10": 1}, "11": 2, "47": 3}
        )

        assert state.last_active_working_status_time == 0.0
        assert not state.has_recent_active_working_status
        assert state.task_progress_percent is None
        assert state.task_elapsed_time == 0
        assert state.current_room_id is None
        assert state.is_docked

    def test_off_dock_task_completed_clears_stale_paused_context(self) -> None:
        """Completion cannot retain a resumable paused overlay off the dock."""
        state = NarwalState(working_status=WorkingStatus.CLEANING)
        state.is_paused = True
        state.cleaning_area = 25.5
        state.cleaning_time = 900
        state.task_elapsed_time = 900
        state.dock_presence = 2

        state.update_from_base_status(
            {"3": {"1": int(WorkingStatus.TASK_COMPLETED), "2": 1, "3": 2}}
        )

        assert not state.is_paused
        assert state.cleaning_area == 0.0
        assert state.cleaning_time == 0
        assert state.task_elapsed_time == 0
        assert not state.has_paused_clean_task_context

    def test_working_status_clears_stale_dock_fields(self) -> None:
        """Fresh task metrics override stale dock indicators."""
        state = NarwalState(working_status=WorkingStatus.DOCKED)
        state.dock_sub_state = 1
        state.dock_activity = 2
        state.dock_field11 = 2
        state.dock_field47 = 3

        state.update_from_working_status({"3": 120})

        assert state.is_cleaning
        assert not state.is_docked
        assert state.working_status == WorkingStatus.DOCKED

    def test_recent_working_status_survives_stale_docked_base_packet(self) -> None:
        """A delayed dock packet cannot erase a confirmed clean handoff."""
        state = NarwalState(working_status=WorkingStatus.DOCKED)
        state.assume_robot_clean()
        state.update_from_working_status({"1": 25, "3": 120, "6": 4})

        state.update_from_base_status(
            {"3": {"1": int(WorkingStatus.DOCKED), "10": 1}, "11": 2, "47": 3}
        )

        assert state.has_recent_active_working_status
        assert state.is_cleaning
        assert not state.is_docked
        assert state.task_progress_percent == 25
        assert state.task_elapsed_time == 120
        assert state.current_room_id == 4

    def test_update_from_base_status_cleaning(self) -> None:
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 4}, "2": _float_to_uint32(85.0)})
        assert state.working_status == WorkingStatus.CLEANING
        assert state.is_cleaning
        assert state.battery_level == 85

    def test_update_from_base_status_docked(self) -> None:
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 10, "10": 1}})
        assert state.working_status == WorkingStatus.DOCKED
        assert state.is_docked

    def test_base_status_maps_station_activity_to_dock_tasks(self) -> None:
        """Coarse station activity still names emptying and washing tasks."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 10, "3": 6, "18": 1}, "11": 2})
        assert state.active_dock_task_keys == (DOCK_TASK_EMPTY_DUSTBIN,)

        state.update_from_base_status({"3": {"1": 10, "3": 6, "18": 2}, "11": 2})
        assert state.active_dock_task_keys == (DOCK_TASK_WASH_MOP,)

    def test_unknown_station_activity_blocks_commands(self) -> None:
        """Unknown station activity must not be exposed as an idle dock."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 10, "3": 6, "18": 99}, "11": 2})

        assert state.active_dock_task_keys == ()
        assert state.has_unmapped_active_dock_task

    def test_unknown_dock_activity_blocks_commands(self) -> None:
        """Unknown nonzero dock activity is not treated as a safe idle dock."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 10, "3": 6, "12": 99}, "11": 2})

        assert state.active_dock_task_keys == ()
        assert state.has_unmapped_active_dock_task

    def test_idle_base_status_clears_stale_coarse_dock_activity(self) -> None:
        """A later authoritative idle packet clears stale dock_activity."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 10, "3": 6, "12": 4}, "11": 2})
        assert state.active_dock_task_keys == (DOCK_TASK_DRY_MOP,)

        state.update_from_base_status({"3": {"1": 10, "3": 6}, "11": 2})

        assert state.dock_activity == 0
        assert state.active_dock_task_keys == ()

    def test_stale_idle_base_status_preserves_private_dock_task_guard(self) -> None:
        """Accepted command reservations block starts without publishing task state."""
        state = NarwalState()
        state.assume_dock_task(DOCK_TASK_EMPTY_DUSTBIN)

        state.update_from_base_status({"3": {"1": 10, "3": 6}, "11": 2})

        assert state.active_dock_task_keys == (DOCK_TASK_EMPTY_DUSTBIN,)
        assert state.blocks_robot_start_for_dock_task

    def test_confirmed_dock_task_clears_reservation_then_idle_clears_task(self) -> None:
        """Hardware activity owns state once a reservation has been confirmed."""
        state = NarwalState()
        state.assume_dock_task(DOCK_TASK_EMPTY_DUSTBIN)

        state.update_from_base_status({"3": {"1": 10, "3": 6, "18": 1}, "11": 2})

        assert state.assumed_active_dock_task is None
        assert state.active_dock_task_keys == (DOCK_TASK_EMPTY_DUSTBIN,)

        state.update_from_base_status({"3": {"1": 10, "3": 6}, "11": 2})

        assert state.active_dock_task_keys == ()

    def test_stale_working_metrics_do_not_override_active_station_task(self) -> None:
        """Historic clean counters cannot hide fresh emptying or washing."""
        for station_activity, dock_activity, task in (
            (1, 0, DOCK_TASK_EMPTY_DUSTBIN),
            (2, 0, DOCK_TASK_WASH_MOP),
            (0, 3, DOCK_TASK_WASH_MOP),
        ):
            state = NarwalState(working_status=WorkingStatus.DOCKED)
            state.dock_presence = 6
            state.station_activity = station_activity
            state.dock_activity = dock_activity

            state.update_from_working_status({"3": 120})

            assert state.station_activity == station_activity
            assert state.dock_activity == dock_activity
            assert not state.has_recent_active_working_status
            assert state.active_dock_task_keys == (task,)

    def test_active_base_status_clears_stale_station_activity(self) -> None:
        """Active robot base-status packets cannot keep stale dock switches on."""
        state = NarwalState()
        state.assume_dock_task(DOCK_TASK_EMPTY_DUSTBIN)

        state.update_from_base_status({"3": {"1": 4, "12": 4, "18": 1}, "11": 2})

        assert state.dock_activity == 0
        assert state.station_activity == 0
        assert state.active_dock_task_keys == ()

    def test_dock_activity_is_timer_presence_signal(self) -> None:
        """Dock activity alone is enough to keep a robot-owned timer visible."""
        state = NarwalState()
        state.dock_activity = 4
        state.set_dock_drying_task(
            DOCK_TASK_DRY_MOP,
            elapsed=60,
            target=180,
            fields=("8", "9"),
        )

        assert state.dock_task_timer(DOCK_TASK_DRY_MOP) is not None

    def test_explicit_docked_status_is_timer_presence_signal(self) -> None:
        """Docked working status keeps typed dock timers active without field 11/47."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 2}})
        state.set_dock_drying_task(
            DOCK_TASK_DRY_MOP,
            elapsed=60,
            target=180,
            fields=("8", "9"),
        )

        assert state.working_status == WorkingStatus.DOCKED_V2
        assert state.is_docked
        assert not state.has_dock_presence_signal
        assert state.dock_task_timer(DOCK_TASK_DRY_MOP) is not None
        assert state.active_dock_task_keys == (DOCK_TASK_DRY_MOP,)
        assert not state.blocks_robot_start_for_dock_task

    def test_stale_dock_timer_does_not_remain_active(self) -> None:
        """Typed dock timers stop driving visible state after telemetry expires."""
        state = NarwalState()
        state.set_dock_drying_task(
            DOCK_TASK_DRY_MOP,
            elapsed=60,
            target=180,
            fields=("8", "9"),
        )

        state.dock_drying_status_time -= 181

        assert state.dock_task_timer(DOCK_TASK_DRY_MOP) is None
        assert state.active_dock_task_keys == ()
        assert not state.has_unmapped_active_dock_task
        assert not state.blocks_robot_start_for_dock_task

    def test_stale_dock_timer_with_station_activity_fails_closed(self) -> None:
        """Stale typed identity cannot classify fresh-looking station work."""
        state = NarwalState()
        state.station_activity = 4
        state.set_dock_drying_task(
            DOCK_TASK_DRY_MOP,
            elapsed=60,
            target=180,
            fields=("8", "9"),
        )

        state.dock_drying_status_time -= 181

        assert state.dock_task_timer(DOCK_TASK_DRY_MOP) is None
        assert state.active_dock_task_keys == ()
        assert state.has_unmapped_active_dock_task
        assert state.blocks_robot_start_for_dock_task

    def test_robot_start_allows_only_typed_drying_work(self) -> None:
        """Typed drying is compatible with a new robot clean."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 10, "3": 6, "18": 1}, "11": 2})
        assert state.blocks_robot_start_for_dock_task

        for task, fields in (
            (DOCK_TASK_DRY_MOP, ("8", "9")),
            (DOCK_TASK_DRY_DUST_BIN, ("10", "11")),
            (DOCK_TASK_DRY_DOCK_BAG, ("12", "13")),
        ):
            state = NarwalState()
            state.update_from_base_status({"3": {"1": 10, "3": 6}, "11": 2})
            state.set_dock_drying_task(
                task,
                elapsed=60,
                target=180,
                fields=fields,
            )
            assert not state.blocks_robot_start_for_dock_task

        state.dock_drying_status_time -= 181

        assert state.active_dock_task_keys == ()
        assert not state.blocks_robot_start_for_dock_task

    def test_assumed_dock_task_temporarily_blocks_robot_start(self) -> None:
        """Accepted-command reservations block new robot starts until telemetry arrives."""
        state = NarwalState()
        state.assume_dock_task(DOCK_TASK_DRY_DOCK_BAG)

        assert state.active_dock_task_keys == (DOCK_TASK_DRY_DOCK_BAG,)
        assert state.blocks_robot_start_for_dock_task

    def test_assumed_robot_clean_waits_for_active_base_status(self) -> None:
        """Working metrics do not expose a start to delayed dock packets."""
        state = NarwalState()
        state.assume_robot_clean()

        state.update_from_working_status({"3": 120})

        assert state.has_assumed_robot_clean
        assert state.is_cleaning

        state.update_from_base_status(
            {"3": {"1": int(WorkingStatus.CLEANING)}, "11": 1, "47": 2}
        )

        assert not state.has_assumed_robot_clean

    def test_assumed_robot_clean_covers_slow_status_handoff(self) -> None:
        """Accepted room starts may take over 30s to publish task telemetry."""
        state = NarwalState()

        with patch("narwal_client.models.time.monotonic", return_value=1000.0):
            state.assume_robot_clean()
        with patch("narwal_client.models.time.monotonic", return_value=1179.0):
            assert state.has_assumed_robot_clean
        with patch("narwal_client.models.time.monotonic", return_value=1181.0):
            assert not state.has_assumed_robot_clean

    def test_assumed_robot_clean_ignores_unknown_off_dock_handoff(self) -> None:
        """UNKNOWN/off-dock base status can appear before task telemetry arrives."""
        state = NarwalState()
        state.assume_robot_clean()

        state.update_from_base_status({"3": {"1": 0}, "11": 1, "47": 2})

        assert state.has_assumed_robot_clean

    def test_assumed_robot_clean_ignores_previous_docked_base_status(self) -> None:
        """A stale dock status cannot erase an accepted start during handoff."""
        state = NarwalState()
        with patch("narwal_client.models.time.monotonic", return_value=1000.0):
            state.assume_robot_clean()

        with patch("narwal_client.models.time.monotonic", return_value=1059.0):
            state.update_from_base_status({"3": {"1": 10, "10": 1}, "11": 2})
            assert state.has_assumed_robot_clean

    def test_assumed_robot_clean_clears_after_docked_handoff_grace(self) -> None:
        """Current idle dock telemetry wins if an accepted start never begins."""
        state = NarwalState()
        with patch("narwal_client.models.time.monotonic", return_value=1000.0):
            state.assume_robot_clean()

        with patch("narwal_client.models.time.monotonic", return_value=1061.0):
            state.update_from_base_status({"3": {"1": 10, "10": 1}, "11": 2})

        assert not state.has_assumed_robot_clean

    def test_assumed_robot_clean_clears_previous_display_map(self) -> None:
        """A newly accepted clean should not render the previous task's route."""
        state = NarwalState()
        state.map_display_data = MapDisplayData(robot_x=1.0, robot_y=2.0)

        state.assume_robot_clean()

        assert state.map_display_data is None

    def test_paused_context_handles_missing_task_metrics(self) -> None:
        """Paused context is safe before richer task metric fields exist."""
        state = NarwalState()
        state.is_paused = True

        assert not state.has_paused_clean_task_context

        state.cleaning_time = 120

        assert state.has_paused_clean_task_context

    @pytest.mark.parametrize(
        "status",
        (
            WorkingStatus.DOCKED,
            WorkingStatus.CHARGED,
            WorkingStatus.DOCKED_V2,
            WorkingStatus.TASK_COMPLETED,
            WorkingStatus.ERROR,
        ),
    )
    def test_terminal_status_ignores_late_active_metrics(
        self, status: WorkingStatus
    ) -> None:
        """Late counters cannot reactivate a terminal robot state."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": int(status)}})

        state.update_from_working_status({"2": 12.5, "3": 900})

        assert not state.has_recent_active_working_status
        assert not state.is_cleaning

    def test_terminal_suppressed_metrics_cannot_revive_paused_context(self) -> None:
        """A later pause bit cannot revive metrics rejected after docking."""
        state = NarwalState()
        state.update_from_base_status(
            {"3": {"1": int(WorkingStatus.DOCKED), "10": 1}, "11": 2}
        )

        state.update_from_working_status({"1": 25, "2": 12.5, "3": 900, "6": 4})
        state.update_from_base_status({"3": {"2": 1}})

        assert state.task_progress_percent is None
        assert state.task_elapsed_time == 0
        assert state.current_room_id is None
        assert not state.has_paused_clean_task_context

    @pytest.mark.parametrize(
        "status", (WorkingStatus.TASK_COMPLETED, WorkingStatus.ERROR)
    )
    def test_explicit_terminal_status_rejects_progressing_metrics(
        self, status: WorkingStatus
    ) -> None:
        """Metric progression cannot override an explicit terminal robot state."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": int(status)}})

        state.update_from_working_status({"1": 25, "3": 120})
        state.update_from_working_status({"1": 26, "3": 121})

        assert not state.has_recent_active_working_status
        assert not state.is_cleaning

    def test_blocking_station_task_rejects_external_clean_candidate(self) -> None:
        """Empty/wash telemetry cannot retain or confirm stale clean metrics."""
        state = NarwalState()
        state.update_from_base_status(
            {"3": {"1": int(WorkingStatus.DOCKED), "10": 1}, "11": 2}
        )
        state.station_activity = 1

        state.update_from_working_status({"1": 25, "3": 120})
        state.update_from_working_status({"1": 26, "3": 121})

        assert state.pending_active_working_status is None
        assert state.task_progress_percent is None
        assert state.task_elapsed_time == 0
        assert not state.has_recent_active_working_status
        assert not state.is_cleaning

    def test_unmapped_station_task_rejects_external_clean_candidate(self) -> None:
        """Unknown dock work cannot be replaced by inferred robot cleaning."""
        state = NarwalState()
        state.update_from_base_status(
            {"3": {"1": int(WorkingStatus.DOCKED), "10": 1}, "11": 2}
        )
        state.dock_activity = 99

        state.update_from_working_status({"3": 120})
        state.update_from_working_status({"3": 121})

        assert state.pending_active_working_status is None
        assert state.task_elapsed_time == 0
        assert not state.has_recent_active_working_status
        assert not state.is_cleaning

    def test_device_error_rejects_external_clean_candidate(self) -> None:
        """Delayed clean metrics cannot override an active device fault."""
        state = NarwalState()
        state.update_from_base_status(
            {
                "1": {"1": 2105, "2": 3, "3": b"wheel stuck"},
                "3": {"1": int(WorkingStatus.DOCKED), "10": 1},
                "11": 2,
            }
        )

        state.update_from_working_status({"1": 25, "3": 120})
        state.update_from_working_status({"1": 26, "3": 121})

        assert state.has_error
        assert state.pending_active_working_status is None
        assert state.task_progress_percent is None
        assert state.task_elapsed_time == 0
        assert not state.has_recent_active_working_status
        assert not state.is_cleaning

        state.update_from_base_status(
            {"1": {}, "3": {"1": int(WorkingStatus.DOCKED), "10": 1}, "11": 2}
        )

        assert not state.has_error
        assert state.working_status == WorkingStatus.DOCKED
        assert not state.is_cleaning

    def test_device_error_discards_preexisting_external_clean_candidate(self) -> None:
        """A candidate from before a fault cannot confirm after recovery."""
        state = NarwalState()
        state.update_from_base_status(
            {"3": {"1": int(WorkingStatus.DOCKED), "10": 1}, "11": 2}
        )
        state.update_from_working_status({"3": 120})
        assert state.pending_active_working_status is not None

        state.update_from_base_status({"1": {"1": 2105}})
        assert state.pending_active_working_status is None

        state.update_from_base_status(
            {"1": {}, "3": {"1": int(WorkingStatus.DOCKED), "10": 1}, "11": 2}
        )
        state.update_from_working_status({"3": 122})

        assert not state.has_error
        assert state.pending_active_working_status is not None
        assert not state.has_recent_active_working_status
        assert not state.is_cleaning

    def test_device_error_prevents_clean_metrics_refresh(self) -> None:
        """Task counters cannot keep active-clean evidence alive through a fault."""
        state = NarwalState()
        with patch("narwal_client.models.time.monotonic", return_value=100.0):
            state.update_from_base_status(
                {"3": {"1": int(WorkingStatus.CLEANING)}, "11": 1, "47": 2}
            )
            state.update_from_working_status({"1": 25, "3": 120})
        with patch("narwal_client.models.time.monotonic", return_value=101.0):
            state.update_from_base_status(
                {"1": {"1": 2105, "2": 3, "3": b"wheel stuck"}}
            )
        with patch("narwal_client.models.time.monotonic", return_value=120.0):
            state.update_from_working_status({"1": 26, "3": 121})

            assert state.has_error
            assert not state.has_recent_active_working_status
            assert not state.is_cleaning

    @pytest.mark.parametrize(
        ("first", "second", "third"),
        (
            ({"1": 25}, {"1": 26}, {"1": 27}),
            ({"4": 600}, {"4": 599}, {"4": 598}),
        ),
    )
    def test_directional_partial_metrics_confirm_external_clean(
        self,
        first: dict[str, int],
        second: dict[str, int],
        third: dict[str, int],
    ) -> None:
        """Progress-only and remaining-only streams can confirm robot work."""
        state = NarwalState()
        with patch("narwal_client.models.time.monotonic", return_value=100.0):
            state.update_from_base_status(
                {"3": {"1": int(WorkingStatus.DOCKED), "10": 1}, "11": 2}
            )

        with patch("narwal_client.models.time.monotonic", return_value=101.0):
            state.update_from_working_status(first)
            assert not state.is_cleaning
        with patch("narwal_client.models.time.monotonic", return_value=102.0):
            state.update_from_working_status(second)
            assert state.is_cleaning
        with patch("narwal_client.models.time.monotonic", return_value=116.0):
            state.update_from_working_status(third)
            assert state.is_cleaning

        with patch("narwal_client.models.time.monotonic", return_value=130.0):
            assert state.has_recent_active_working_status
            assert state.is_cleaning

    @pytest.mark.parametrize(
        "dock_fields",
        ({"11": 2}, {"11": 3}, {"47": 1}, {"47": 3}),
    )
    def test_docked_standby_ignores_late_active_metrics(
        self, dock_fields: dict[str, int]
    ) -> None:
        """Legacy and newer docked STANDBY packets are terminal telemetry."""
        state = NarwalState(working_status=WorkingStatus.CLEANING)
        state.update_from_working_status({"2": 12.5, "3": 900})
        assert state.has_recent_active_working_status

        state.update_from_base_status({"3": {"1": 1}, **dock_fields})
        state.update_from_working_status({"2": 12.5, "3": 900})

        assert state.working_status == WorkingStatus.STANDBY
        assert state.is_docked
        assert not state.has_recent_active_working_status
        assert not state.is_cleaning

    def test_task_completed_clears_metrics_after_accepted_start(self) -> None:
        """Completion ends fresh metrics even during the accepted-start handoff."""
        state = NarwalState(working_status=WorkingStatus.STANDBY)
        state.assume_robot_clean()
        state.update_from_working_status({"2": 12.5, "3": 900})
        assert state.has_assumed_robot_clean
        assert state.is_cleaning

        state.update_from_base_status(
            {"3": {"1": int(WorkingStatus.TASK_COMPLETED)}}
        )

        assert not state.has_assumed_robot_clean
        assert not state.has_recent_active_working_status
        assert not state.is_cleaning

    def test_paused_context_ignores_stale_metrics_after_docking(self) -> None:
        """Docked API state ends stale paused overlays from the previous task."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 10, "2": 1, "10": 1}, "11": 2})
        state.cleaning_time = 120
        state.current_room_id = 4

        assert not state.has_paused_clean_task_context

    def test_paused_context_outranks_cached_dock_fields(self) -> None:
        """Only a current terminal update may end retained paused context."""
        state = NarwalState(working_status=WorkingStatus.STANDBY)
        state.is_paused = True
        state.cleaning_time = 120
        state.dock_presence = 6
        state.dock_field11 = 2
        state.dock_field47 = 3

        assert state.has_paused_clean_task_context

    def test_dock_task_assumption_is_only_a_short_command_guard(self) -> None:
        """Dock task assumptions must not fabricate long-running device state."""
        state = NarwalState()

        with patch("narwal_client.models.time.monotonic", return_value=1000.0):
            state.assume_dock_task(DOCK_TASK_DRY_DOCK_BAG)
        with patch("narwal_client.models.time.monotonic", return_value=1029.0):
            assert state.assumed_active_dock_task == DOCK_TASK_DRY_DOCK_BAG
            assert state.active_dock_task_keys == (DOCK_TASK_DRY_DOCK_BAG,)
        with patch("narwal_client.models.time.monotonic", return_value=1031.0):
            assert state.assumed_active_dock_task is None
            assert state.active_dock_task_keys == ()

    def test_update_from_base_status_charged(self) -> None:
        """Status 14 = fully charged on dock."""
        state = NarwalState()
        state.update_from_base_status({
            "3": {"1": 14, "10": 1},
            "2": _float_to_uint32(100.0),
            "38": 100,
        })
        assert state.working_status == WorkingStatus.CHARGED
        assert state.is_docked
        assert state.battery_level == 100
        assert state.curing_agent_consumption_percent == 100

    def test_update_from_base_status_standby_on_dock(self) -> None:
        """STANDBY(1) with dock sub-state=1 means docked."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 1, "10": 1}})
        assert state.working_status == WorkingStatus.STANDBY
        assert state.is_docked

    def test_update_from_base_status_standby_off_dock_field11(self) -> None:
        """STANDBY(1) with field 11=1 means off dock (validated via dock_research)."""
        state = NarwalState()
        state.update_from_base_status({
            "3": {"1": 1, "3": 2}, "11": 1, "47": 2,
            "2": _float_to_uint32(100.0),
        })
        assert state.working_status == WorkingStatus.STANDBY
        assert state.dock_field11 == 1
        assert state.dock_field47 == 2
        assert not state.is_docked

    def test_update_from_base_status_standby_on_dock_field11(self) -> None:
        """STANDBY(1) with field 11=2 means on dock (validated via dock_research).

        5 captures: field 11=2 in all 3 on-dock, field 11=1 in both off-dock.
        """
        state = NarwalState()
        state.update_from_base_status({
            "3": {"1": 1, "3": 6}, "11": 2, "47": 3,
        })
        assert state.working_status == WorkingStatus.STANDBY
        assert state.dock_field11 == 2
        assert state.dock_field47 == 3
        assert state.is_docked

    def test_update_from_base_status_standby_on_dock_presence_only(self) -> None:
        """STANDBY with field 3.3 dock presence means on dock."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 1, "3": 6}})
        assert state.working_status == WorkingStatus.STANDBY
        assert state.is_docked

    def test_update_from_base_status_standby_on_dock_field47_only(self) -> None:
        """STANDBY(1) with field 47=3 means on dock (secondary signal)."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 1}, "47": 3})
        assert state.working_status == WorkingStatus.STANDBY
        assert state.is_docked

    def test_update_from_base_status_standby_no_signals(self) -> None:
        """STANDBY(1) with no dock signals at all — NOT docked (safe default)."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 1}})
        assert state.working_status == WorkingStatus.STANDBY
        assert not state.is_docked

    def test_update_from_base_status_standby_dock_activity(self) -> None:
        """STANDBY(1) with dock_activity > 0 means docked."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 1, "12": 2}})
        assert state.working_status == WorkingStatus.STANDBY
        assert state.is_docked

    def test_update_from_base_status_paused(self) -> None:
        """Paused overlay: field 3 sub-field 2 = 1."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 4, "2": 1}})
        assert state.working_status == WorkingStatus.CLEANING
        assert state.is_paused
        assert not state.is_cleaning  # is_cleaning is False when paused

    # --- v01.07.23+ firmware tests ---

    def test_docked_v2_working_status(self) -> None:
        """DOCKED_V2(2) on v01.07.23+ firmware maps to docked."""
        state = NarwalState()
        state.update_from_base_status({
            "3": {"1": 2, "4": 1, "11": 3},  # new FW sub-fields
            "11": 3, "47": 1,
        })
        assert state.working_status == WorkingStatus.DOCKED_V2
        assert state.is_docked

    def test_cleaning_v2_working_status(self) -> None:
        """CLEANING_V2(3) on newer Flow 2 firmware maps to active cleaning."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 3}, "11": 1, "47": 2})
        assert state.working_status == WorkingStatus.CLEANING_V2
        assert state.is_cleaning
        assert not state.is_docked

    def test_new_fw_field3_unknown_subfields_logged(self) -> None:
        """New firmware sub-fields (4, 11) are parsed without error."""
        state = NarwalState()
        # Should not raise — unknown sub-fields logged at debug level
        state.update_from_base_status({"3": {"1": 2, "4": 99, "11": 3}})
        assert state.working_status == WorkingStatus.DOCKED_V2

    def test_new_fw_dock_field11_gte2(self) -> None:
        """v01.07.23 dock_field11=3 detected as docked via >= 2 check."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 1}, "11": 3})
        assert state.dock_field11 == 3
        assert state.is_docked

    def test_new_fw_dock_field47_eq1(self) -> None:
        """v01.07.23 dock_field47=1 detected as docked."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 1}, "47": 1})
        assert state.dock_field47 == 1
        assert state.is_docked

    def test_field3_as_list_parsed(self) -> None:
        """bbp can return field3 as a list — first element should be used."""
        state = NarwalState()
        state.update_from_base_status({"3": [{"1": 4, "2": 1}]})
        assert state.working_status == WorkingStatus.CLEANING
        assert state.is_paused

    def test_field3_empty_list_no_crash(self) -> None:
        """Empty list for field3 should not crash."""
        state = NarwalState()
        state.update_from_base_status({"3": []})
        assert state.working_status == WorkingStatus.UNKNOWN  # unchanged default

    def test_field3_not_dict_no_crash(self) -> None:
        """Non-dict field3 (e.g., bytes from bbp) should not crash."""
        state = NarwalState()
        state.update_from_base_status({"3": b"\x08\x02"})
        assert state.working_status == WorkingStatus.UNKNOWN  # unchanged default

    def test_absent_paused_subfield_resets_to_false(self) -> None:
        """When field3.2 is absent (protobuf default=0), is_paused resets."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 4, "2": 1}})
        assert state.is_paused
        # Next broadcast without "2" key → paused resets to False
        state.update_from_base_status({"3": {"1": 4}})
        assert not state.is_paused

    def test_unknown_working_status_value(self) -> None:
        """Unmapped working_status value falls back to UNKNOWN."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 255}})
        assert state.working_status == WorkingStatus.UNKNOWN

    def test_unknown_working_status_warns_once(self, caplog) -> None:
        """Repeated unknown values warn once, not once per broadcast (#46).

        The robot rebroadcasts status every ~1.5s; warning each time floods
        the log with thousands of identical lines.
        """
        from narwal_client import models as models_mod

        models_mod._WARNED_WORKING_STATUS.discard(255)
        state = NarwalState()
        with caplog.at_level(logging.WARNING, logger=models_mod.__name__):
            for _ in range(50):
                state.update_from_base_status({"3": {"1": 255}})

        warnings = [r for r in caplog.records if "Unknown working_status" in r.message]
        assert len(warnings) == 1
        assert state.working_status == WorkingStatus.UNKNOWN

    def test_custom_cleaning_status_preserves_live_room_metrics(self) -> None:
        """Status 17 is active custom cleaning, not an unknown idle state."""
        state = NarwalState()
        state.update_from_base_status(
            {"3": {"1": 17, "14": 1, "17": 7}, "11": 1, "47": 2}
        )
        state.update_from_working_status({"3": 120, "6": 4})

        assert state.working_status == WorkingStatus.CUSTOM_CLEANING
        assert state.is_cleaning
        assert not state.is_docked
        assert state.current_room_id == 4
        assert state.cleaning_time == 120

    def test_update_from_base_status(self) -> None:
        state = NarwalState()
        state.update_from_base_status({
            "2": _float_to_uint32(85.0),
            "38": 100,
            "36": 1757252225,
            "13": "d4bec8c82c484a3ba0428bb0dd4359e2",
        })
        assert state.battery_level == 85
        assert state.curing_agent_consumption_percent == 100
        assert state.station_bag_health_reset_time == 1757252225
        assert state.binded_uuid == "d4bec8c82c484a3ba0428bb0dd4359e2"

    def test_base_status_consumables_and_error(self) -> None:
        """Field 35 dust-bag health (float32 %), 41 detergent %, 1 errorCode presence."""
        state = NarwalState()
        state.update_from_base_status({
            "1": {},  # empty errorCode = no fault
            "35": _float_to_uint32(68.5),
            "41": 100,
        })
        assert round(state.dust_bag_health, 1) == 68.5
        assert state.detergent_remaining == 100
        assert state.has_error is False
        assert state.error_codes == []
        # A populated ErrorCode flips has_error on and exposes code/level/detail.
        state.update_from_base_status({"1": {"1": 2105, "2": 3, "3": b"wheel stuck"}})
        assert state.has_error is True
        assert state.error_codes == [2105]
        assert state.error_level == 3
        assert state.error_detail == "wheel stuck"
        # Clears when the next base_status reports an empty errorCode.
        state.update_from_base_status({"1": {}})
        assert state.has_error is False
        assert state.error_codes == []

    def test_multiple_error_codes(self) -> None:
        """Repeated ErrorCode (bbp list) collects all identityCodes, max level."""
        state = NarwalState()
        state.update_from_base_status({"1": [{"1": 10, "2": 1}, {"1": 20, "2": 4}]})
        assert state.error_codes == [10, 20]
        assert state.error_level == 4
        assert state.has_error is True

    def test_base_status_tank_states(self) -> None:
        """Tank/bag enum states parse into Optional ints; unreported stays None."""
        state = NarwalState()
        # Live healthy snapshot: clean-water/sewage ok (1), dust box ok (1),
        # station bag installed (1). No dust-bag field on this model.
        state.update_from_base_status({"23": 1, "24": 1, "20": 1, "39": 1})
        assert state.clean_water_tank_state == 1
        assert state.sewage_tank_state == 1
        assert state.dust_box_state == 1
        assert state.station_bag_state == 1
        assert state.dust_bag_state is None  # not reported by this model
        # Attention states.
        state.update_from_base_status({"23": 2, "39": 3})
        assert state.clean_water_tank_state == 2  # EMPTY
        assert state.station_bag_state == 3  # SUGGEST_REPLACE

    def test_terminate_reason(self) -> None:
        """Field 15 = terminateReason (TaskResult)."""
        state = NarwalState()
        state.update_from_base_status({"15": 4})
        assert state.terminate_reason == 4  # LOW_BATTERY_FORCE_END

    def test_consumable_info_parse(self) -> None:
        """consumable/get_consumable_info → maintain/replace alert lists; empty clears."""
        state = NarwalState()
        state.update_from_consumable_info({"1": {"1": [1, 9], "2": 8}})
        assert state.maintain_items == [1, 9]  # dust_box, water_tank_sponge
        assert state.replace_items == [8]  # dust_bag
        state.update_from_consumable_info({"1": {}})  # healthy
        assert state.maintain_items == []
        assert state.replace_items == []

    def test_consumable_info_packed_varints(self) -> None:
        """The wire shape: protobuf packs repeated scalars, bbp yields str (#79).

        Verbatim capture from a Flow (AX12, v01.08.03.07). The list-and-int shapes
        above are what a hand-written payload looks like, not what a robot sends —
        which is how this went unnoticed while every alert was dropped.
        """
        state = NarwalState()
        state.update_from_consumable_info({"1": {"1": "\x04\x06\x08\n", "2": "\x03\x14"}})
        # wash ribs, universal wheel, side distance sensor, anti-winding brush
        assert state.maintain_items == [4, 6, 8, 10]
        assert state.replace_items == [3, 20]  # side brush, station bag

    def test_consumable_info_single_packed_item(self) -> None:
        """A one-item packed list is where an off-by-one decoder still looks right."""
        state = NarwalState()
        state.update_from_consumable_info({"1": {"1": "\x02", "2": "\x08"}})
        assert state.maintain_items == [2]  # dust filter
        assert state.replace_items == [8]  # dust bag

    def test_consumable_info_accepts_bytes(self) -> None:
        """Same blob as bytes rather than str — decoder version shouldn't matter."""
        state = NarwalState()
        state.update_from_consumable_info({"1": {"1": b"\x04\x06", "2": b"\x14"}})
        assert state.maintain_items == [4, 6]
        assert state.replace_items == [20]

    def test_consumable_info_multibyte_varint(self) -> None:
        """Values above 127 span bytes; a byte-per-value shortcut would misread them."""
        state = NarwalState()
        state.update_from_consumable_info({"1": {"1": "\xac\x02", "2": "\x01"}})
        assert state.maintain_items == [300]
        assert state.replace_items == [1]

    def test_consumable_info_empty_blob_is_healthy(self) -> None:
        """An empty packed field still means nothing needs attention."""
        state = NarwalState()
        state.update_from_consumable_info({"1": {"1": [4], "2": [20]}})
        state.update_from_consumable_info({"1": {"1": "", "2": ""}})
        assert state.maintain_items == []
        assert state.replace_items == []

    def test_base_status_dock_light_mode(self) -> None:
        """Field 50 exposes the base station ambient light mode."""
        state = NarwalState()
        state.update_from_base_status({"50": 2})
        assert state.dock_light_mode == 2

    def test_base_status_missing_dock_light_means_off(self) -> None:
        """When the dock omits field 50, the ambient light is off."""
        state = NarwalState()
        state.update_from_base_status({"50": 2})
        state.update_from_base_status({"2": _float_to_uint32(100.0)})
        assert state.dock_light_mode == 0

    def test_update_from_upgrade_status(self) -> None:
        state = NarwalState()
        state.update_from_upgrade_status({
            "7": "v01.02.19.02",
            "8": "v01.02.19.02",
            "2": 3,
            "4": 10,
        })
        assert state.firmware_version == "v01.02.19.02"
        assert state.firmware_target == "v01.02.19.02"
        assert state.upgrade_status == 3
        assert state.upgrade_stage == 10

    def test_update_from_download_status(self) -> None:
        # Field 3 = state (field 1 is download type, ignored).
        state = NarwalState()
        state.update_from_download_status({"1": 5, "3": 2})
        assert state.download_status == 2

    def test_incremental_updates(self) -> None:
        """State should accumulate across multiple topic updates."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 4}, "2": _float_to_uint32(95.0)})
        state.update_from_working_status({"3": 120, "2": _float_to_uint32(12.5)})
        state.update_from_upgrade_status({"7": "v01.02.19.02"})

        assert state.battery_level == 95
        assert state.is_cleaning
        assert state.cleaning_time == 120
        assert state.cleaning_area == 12.5
        assert state.firmware_version == "v01.02.19.02"

    def test_raw_data_preserved(self) -> None:
        state = NarwalState()
        raw = {"2": _float_to_uint32(100.0), "38": 100, "47": 2, "unknown_field": "value"}
        state.update_from_base_status(raw)
        assert state.raw_base_status == raw

    def test_battery_field2_float32_83(self) -> None:
        """Field 2 = 1118175232 → 83.0% battery (confirmed from monitor capture)."""
        state = NarwalState()
        state.update_from_base_status({"2": 1118175232})
        assert state.battery_level == 83

    def test_battery_field2_float32_85(self) -> None:
        """Field 2 = 1118437376 → 85.0% battery."""
        state = NarwalState()
        state.update_from_base_status({"2": 1118437376})
        assert state.battery_level == 85

    def test_battery_field2_as_python_float(self) -> None:
        """bbp may return field 2 as a Python float directly."""
        state = NarwalState()
        state.update_from_base_status({"2": 83.0})
        assert state.battery_level == 83

    def test_field38_curing_agent_not_battery(self) -> None:
        """Field 38 is curingAgentConsumptionPercent, not battery SOC/health."""
        state = NarwalState()
        state.update_from_base_status({"38": 100})
        assert state.curing_agent_consumption_percent == 100
        # battery_level unchanged (no field 2)
        assert state.battery_level == 0

    def test_battery_only_update_ignores_working_status(self) -> None:
        """update_battery_from_base_status updates battery but NOT working_status.

        When robot is in deep sleep, get_status() returns current battery
        but stale working_status. The battery-only method must not overwrite
        the last authoritative working_status.
        """
        state = NarwalState()
        # Simulate last authoritative state from a broadcast: DOCKED
        state.update_from_base_status({
            "3": {"1": 10, "10": 1},
            "2": _float_to_uint32(80.0),
        })
        assert state.working_status == WorkingStatus.DOCKED
        assert state.battery_level == 80

        # Now simulate a deep-sleep get_status() response with stale CLEANING
        # but fresh battery. Use battery-only update.
        stale_response = {
            "3": {"1": 4, "7": 1},  # stale CLEANING+returning
            "2": _float_to_uint32(85.0),
            "38": 100,
        }
        state.update_battery_from_base_status(stale_response)

        # Battery updated, working_status preserved from last authoritative source
        assert state.battery_level == 85
        assert state.curing_agent_consumption_percent == 100
        assert state.working_status == WorkingStatus.DOCKED  # NOT overwritten
        assert state.is_docked  # still correct

    def test_returning_to_dock_field7(self) -> None:
        """Field 3.7=1 indicates returning to dock (confirmed live)."""
        state = NarwalState()
        # Live data: {1=4, 7=1, 10=2} — CLEANING + returning + docking
        state.update_from_base_status({"3": {"1": 4, "7": 1, "10": 2}})
        assert state.working_status == WorkingStatus.CLEANING
        assert state.is_returning_to_dock
        assert state.dock_sub_state == 2
        assert state.is_returning  # should be True via field 3.7
        assert not state.is_cleaning  # returning takes priority

    def test_stale_return_flag_does_not_hide_active_clean(self) -> None:
        """Field 3.7 alone is not an authoritative returning state."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 4, "7": 1}})
        state.last_active_working_status_time = 0.0

        assert state.is_returning_to_dock
        assert not state.is_returning
        assert state.is_cleaning

    def test_returning_clears_when_docked(self) -> None:
        """Returning flag clears when robot docks."""
        state = NarwalState()
        # During return
        state.update_from_base_status({"3": {"1": 4, "7": 1, "10": 2}})
        assert state.is_returning
        # After docking: {1=14, 12=2}
        state.update_from_base_status({"3": {"1": 14, "12": 2}})
        assert not state.is_returning
        assert state.is_docked
        assert state.dock_activity == 2

    def test_returning_via_dock_sub_state_only(self) -> None:
        """dock_sub_state=2 alone is NOT enough — both field 3.7 AND 3.10 required."""
        state = NarwalState()
        # Only dock_sub_state=2 without field 3.7 — should NOT be returning
        # (single stale field causes false positives during normal cleaning)
        state.update_from_base_status({"3": {"1": 4, "10": 2}})
        assert not state.is_returning

    def test_not_returning_when_standby_with_dock_sub_state(self) -> None:
        """STANDBY with dock_sub_state=2 means docked, not returning."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 1, "10": 2}})
        assert not state.is_returning

    def test_not_returning_when_cleaning_without_field7(self) -> None:
        """Cleaning without field 3.7 is NOT returning (just cleaning)."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 4}})
        assert state.is_cleaning
        assert not state.is_returning

    def test_unknown_working_status_high_value(self) -> None:
        """Unknown status values should fall back to UNKNOWN."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 255}})
        assert state.working_status == WorkingStatus.UNKNOWN


def _float_to_uint32(f: float) -> int:
    """Encode a float as the uint32 bit pattern (for protobuf simulation)."""
    return struct.unpack("I", struct.pack("f", f))[0]


class TestMapData:
    """Tests for MapData.from_response()."""

    def test_basic_map_parsing(self) -> None:
        decoded = {"2": {
            "3": 60,
            "4": 341,
            "5": 494,
            "12": [{"1": 3, "2": 0, "3": b"Kitchen"}],
            "17": b"\x78\x01" + b"\x00" * 20,
            "33": 944,
            "34": 1740000000,
        }}
        m = MapData.from_response(decoded)
        assert m.width == 341
        assert m.height == 494
        assert m.resolution == 60
        assert len(m.rooms) == 1
        assert m.rooms[0].name == "Kitchen"
        assert m.area == 944

    def test_dock_position_from_field8_uint32(self) -> None:
        """Dock parsed from field 8 (dm coords as uint32, same as display_map field 5)."""
        decoded = {"2": {
            "3": 60,
            "4": 341,
            "5": 494,
            "6": {"1": -341, "2": 152, "3": -280, "4": 60},
            "8": {
                "1": {
                    "1": _float_to_uint32(-8.0188),
                    "2": _float_to_uint32(0.221),
                },
                "2": _float_to_uint32(0.036),
            },
            "17": b"",
        }}
        m = MapData.from_response(decoded)
        assert m.dock_x is not None
        assert m.dock_y is not None
        assert abs(m.dock_x - 272.0) < 1.0
        assert abs(m.dock_y - 341.2) < 1.0

    def test_dock_position_from_field8_float(self) -> None:
        """bbp may return fixed32 fields as Python floats directly."""
        decoded = {"2": {
            "3": 60,
            "4": 341,
            "5": 494,
            "6": {"1": -341, "3": -280},
            "8": {"1": {"1": -8.0188, "2": 0.221}, "2": 0.036},
            "17": b"",
        }}
        m = MapData.from_response(decoded)
        assert m.dock_x is not None
        assert m.dock_y is not None
        assert abs(m.dock_x - 272.0) < 1.0
        assert abs(m.dock_y - 341.2) < 1.0

    def test_dock_position_missing_field8(self) -> None:
        """No dock position when field 8 is missing."""
        decoded = {"2": {
            "3": 60,
            "4": 341,
            "5": 494,
            "6": {"1": -341, "3": -280},
            "17": b"",
        }}
        m = MapData.from_response(decoded)
        assert m.dock_x is None
        assert m.dock_y is None

    def test_dock_position_zero_resolution(self) -> None:
        """No dock position when resolution is zero."""
        decoded = {"2": {
            "3": 0,
            "4": 341,
            "5": 494,
            "8": {"1": {"1": -8.0, "2": 0.2}, "2": 0.0},
            "17": b"",
        }}
        m = MapData.from_response(decoded)
        assert m.dock_x is None
        assert m.dock_y is None

    def test_empty_response(self) -> None:
        m = MapData.from_response({})
        assert m.width == 0
        assert m.dock_x is None

    def test_obstacles_from_field32(self) -> None:
        """MapData.from_response includes obstacles parsed from field 32."""
        decoded = {"2": {
            "3": 60,
            "4": 341,
            "5": 494,
            "6": {"1": -341, "3": -280},
            "17": b"",
            "32": {
                "1": [
                    {
                        "1": 1,
                        "2": 14,
                        "3": {
                            "1": {
                                "1": _float_to_uint32(-110.5),
                                "2": _float_to_uint32(-129.5),
                            },
                            "2": _float_to_uint32(11.0),
                            "3": _float_to_uint32(41.0),
                        },
                        "4": _float_to_uint32(180.0),
                    },
                ],
            },
        }}
        m = MapData.from_response(decoded)
        assert len(m.obstacles) == 1
        obs = m.obstacles[0]
        assert obs.id == 1
        assert obs.type_id == 14
        assert obs.display_name == "Sofa"
        assert abs(obs.center_x - (-110.5)) < 0.5
        assert abs(obs.center_y - (-129.5)) < 0.5
        assert abs(obs.width - 11.0) < 0.5
        assert abs(obs.height - 41.0) < 0.5

    def test_obstacles_empty_when_no_field32(self) -> None:
        """MapData.from_response returns empty obstacles when field 32 is missing."""
        decoded = {"2": {"3": 60, "4": 10, "5": 10, "17": b""}}
        m = MapData.from_response(decoded)
        assert m.obstacles == []


class TestObstacleInfo:
    """Tests for ObstacleInfo dataclass."""

    def test_display_name_known_type(self) -> None:
        """ObstacleInfo with type_id=14 has display_name 'Sofa'."""
        obs = ObstacleInfo(id=1, type_id=14)
        assert obs.display_name == "Sofa"

    def test_display_name_unknown_type(self) -> None:
        """ObstacleInfo with unknown type_id=99 has display_name 'Object 99'."""
        obs = ObstacleInfo(id=1, type_id=99)
        assert obs.display_name == "Object 99"

    def test_display_name_all_known_types(self) -> None:
        """All known type IDs have correct display names."""
        expected = {2: "Double Bed", 4: "Dining Table", 6: "Tea Table", 14: "Sofa", 28: "Toilet"}
        for type_id, name in expected.items():
            obs = ObstacleInfo(id=1, type_id=type_id)
            assert obs.display_name == name

    def test_to_grid_coords(self) -> None:
        """to_grid_coords subtracts origin correctly."""
        obs = ObstacleInfo(id=1, type_id=14, center_x=-110.5, center_y=-129.5)
        gx, gy = obs.to_grid_coords(origin_x=-280, origin_y=-341)
        assert abs(gx - 169.5) < 0.01
        assert abs(gy - 211.5) < 0.01


class TestParseObstacles:
    """Tests for _parse_obstacles function."""

    def test_parse_obstacles_list(self) -> None:
        """_parse_obstacles with bbp-decoded field 32 data returns correct list."""
        field32 = {
            "1": [
                {
                    "1": 1,
                    "2": 14,
                    "3": {
                        "1": {
                            "1": _float_to_uint32(-110.5),
                            "2": _float_to_uint32(-129.5),
                        },
                        "2": _float_to_uint32(11.0),
                        "3": _float_to_uint32(41.0),
                    },
                    "4": _float_to_uint32(180.0),
                },
                {
                    "1": 4,
                    "2": 2,
                    "3": {
                        "1": {
                            "1": _float_to_uint32(10.0),
                            "2": _float_to_uint32(95.5),
                        },
                        "2": _float_to_uint32(36.0),
                        "3": _float_to_uint32(29.0),
                    },
                    "4": _float_to_uint32(180.0),
                },
            ],
        }
        obstacles = _parse_obstacles(field32)
        assert len(obstacles) == 2
        assert obstacles[0].id == 1
        assert obstacles[0].type_id == 14
        assert obstacles[0].display_name == "Sofa"
        assert abs(obstacles[0].center_x - (-110.5)) < 0.5
        assert obstacles[1].id == 4
        assert obstacles[1].type_id == 2
        assert obstacles[1].display_name == "Double Bed"

    def test_parse_obstacles_empty_field32(self) -> None:
        """_parse_obstacles handles missing/empty field 32 gracefully."""
        assert _parse_obstacles({}) == []
        assert _parse_obstacles({"1": []}) == []

    def test_parse_obstacles_single_item_dict(self) -> None:
        """_parse_obstacles handles single item (dict not list) in field 32.1."""
        field32 = {
            "1": {
                "1": 13,
                "2": 4,
                "3": {
                    "1": {
                        "1": _float_to_uint32(-154.0),
                        "2": _float_to_uint32(-55.5),
                    },
                    "2": _float_to_uint32(13.0),
                    "3": _float_to_uint32(20.0),
                },
                "4": _float_to_uint32(90.0),
            },
        }
        obstacles = _parse_obstacles(field32)
        assert len(obstacles) == 1
        assert obstacles[0].id == 13
        assert obstacles[0].type_id == 4
        assert obstacles[0].display_name == "Dining Table"

    def test_parse_obstacles_float32_conversion(self) -> None:
        """float32 conversion works for coordinate values (uint32 bit patterns)."""
        # Use known value: -110.5 as uint32 = struct.unpack('I', struct.pack('f', -110.5))[0]
        field32 = {
            "1": {
                "1": 1,
                "2": 14,
                "3": {
                    "1": {
                        "1": _float_to_uint32(-110.5),
                        "2": _float_to_uint32(-129.5),
                    },
                    "2": _float_to_uint32(11.0),
                    "3": _float_to_uint32(41.0),
                },
                "4": _float_to_uint32(180.0),
            },
        }
        obstacles = _parse_obstacles(field32)
        assert len(obstacles) == 1
        assert abs(obstacles[0].center_x - (-110.5)) < 0.1
        assert abs(obstacles[0].center_y - (-129.5)) < 0.1
        assert abs(obstacles[0].width - 11.0) < 0.1
        assert abs(obstacles[0].height - 41.0) < 0.1
        assert abs(obstacles[0].angle - 180.0) < 0.1

    def test_parse_obstacles_skips_bad_items(self) -> None:
        """_parse_obstacles skips non-dict items without crashing."""
        field32 = {
            "1": [
                "not a dict",
                42,
                {"1": 1, "2": 28, "3": {"1": {"1": 0.0, "2": 0.0}}},
            ],
        }
        obstacles = _parse_obstacles(field32)
        assert len(obstacles) == 1
        assert obstacles[0].type_id == 28

class TestCurrentRoomTracking:
    """Tests for current_room_id parsing and current_room_name lookup.

    working_status field 6 confirmed 2026-04-24 from live Flow 2 capture:
    value changed 4 (Corridor) → 1 (Living Room) as robot moved between rooms.
    """

    def test_current_room_id_from_working_status_field6(self) -> None:
        """Field 6 in working_status sets current_room_id."""
        state = NarwalState()
        state.update_from_working_status({"6": 4})
        assert state.current_room_id == 4

    def test_current_room_id_updates_as_robot_moves(self) -> None:
        """current_room_id updates each time working_status arrives with field 6."""
        state = NarwalState()
        state.update_from_working_status({"6": 4})
        assert state.current_room_id == 4
        state.update_from_working_status({"6": 1})
        assert state.current_room_id == 1

    def test_current_room_id_zero_becomes_none(self) -> None:
        """Field 6 = 0 is treated as absent (no room)."""
        state = NarwalState()
        state.update_from_working_status({"6": 0})
        assert state.current_room_id is None

    def test_nested_current_room_details_supply_id_and_name(self) -> None:
        """Nested firmware room details populate the fallback room name."""
        state = NarwalState()

        state.update_from_working_status({"6": {"1": 4, "3": b"Kitchen"}})

        assert state.current_room_id == 4
        assert state.current_room_aux_name == "Kitchen"
        assert state.current_room_name == "Kitchen"

    def test_current_room_id_not_cleared_when_field6_absent(self) -> None:
        """If field 6 is not in the message, current_room_id is not cleared.

        working_status messages without field 6 are routine (e.g. the idle
        heartbeat only sends a few fields). We must not reset current_room_id
        on every message — only update it when field 6 is explicitly present.
        """
        state = NarwalState()
        state.update_from_working_status({"6": 4})
        assert state.current_room_id == 4
        # Message without field 6
        state.update_from_working_status({"3": 120, "13": 18000})
        assert state.current_room_id == 4  # unchanged

    def test_current_room_id_default_is_none(self) -> None:
        """Default state has no current room."""
        state = NarwalState()
        assert state.current_room_id is None

    def test_current_room_name_returns_none_when_no_current_room(self) -> None:
        """current_room_name is None when current_room_id is None."""
        state = NarwalState()
        assert state.current_room_name is None

    def test_current_room_name_returns_none_when_no_map(self) -> None:
        """current_room_name is None when map_data has not loaded yet."""
        state = NarwalState()
        state.update_from_working_status({"6": 4})
        assert state.map_data is None
        assert state.current_room_name is None

    def test_current_room_name_with_user_named_room(self) -> None:
        """current_room_name returns user-assigned name for named rooms."""
        state = NarwalState()
        state.update_from_working_status({"6": 3})
        state.map_data = MapData(
            rooms=[
                RoomInfo(room_id=1, room_sub_type=3),     # Living Room
                RoomInfo(room_id=3, name="Phoebe's room"),  # user-named
            ],
        )
        assert state.current_room_name == "Phoebe's room"

    def test_current_room_name_with_type_named_room(self) -> None:
        """current_room_name falls back to room type name for unnamed rooms."""
        state = NarwalState()
        state.update_from_working_status({"6": 1})
        state.map_data = MapData(
            rooms=[
                RoomInfo(room_id=1, room_sub_type=3),  # type 3 = Living room
                RoomInfo(room_id=3, name="Phoebe's room"),
            ],
        )
        assert state.current_room_name == "Living room"

    def test_current_room_name_with_numbered_room(self) -> None:
        """current_room_name appends instance_index for duplicate room types."""
        state = NarwalState()
        state.update_from_working_status({"6": 10})
        state.map_data = MapData(
            rooms=[
                RoomInfo(room_id=7, room_sub_type=6, instance_index=1),   # Toilet
                RoomInfo(room_id=10, room_sub_type=6, instance_index=2),  # Toilet 2
                RoomInfo(room_id=11, room_sub_type=6, instance_index=3),  # Toilet 3
            ],
        )
        assert state.current_room_name == "Toilet 2"

    def test_current_room_name_unknown_room_id_returns_none(self) -> None:
        """current_room_name returns None if room_id not found in map."""
        state = NarwalState()
        state.update_from_working_status({"6": 99})
        state.map_data = MapData(
            rooms=[RoomInfo(room_id=1, room_sub_type=3)],
        )
        assert state.current_room_name is None

    def test_current_room_name_matches_live_capture(self) -> None:
        """Simulate the 2026-04-24 live capture: room 4 = Corridor, room 1 = Living room.

        Capture confirmed: field 6 changed from 4 to 1 as robot moved rooms.
        Names follow the shared RoomType table corrected in #48.
        """
        state = NarwalState()
        # Build room map from live get_map data
        state.map_data = MapData(
            rooms=[
                RoomInfo(room_id=1, name="", room_sub_type=3),   # Living room (type 3)
                RoomInfo(room_id=4, name="", room_sub_type=10),  # Corridor (type 10)
            ],
        )
        # First capture: field 6 = 4 (Corridor)
        state.update_from_working_status({"6": 4})
        assert state.current_room_id == 4
        assert state.current_room_name == "Corridor"

        # Second capture 22 minutes later: field 6 = 1 (Living room)
        state.update_from_working_status({"6": 1})
        assert state.current_room_id == 1
        assert state.current_room_name == "Living room"


class TestRoomInfoNames:
    """Tests for the shared RoomType-to-name table (issue #22).

    The app names rooms through one switch keyed only on the RoomType enum, so
    every model resolves the same names from the app's en-US.json.
    """

    def test_shared_table_names(self) -> None:
        """Every RoomType resolves to its verbatim en-US.json name."""
        expected = {
            0: "Room", 1: "Master bedroom", 2: "Secondary bedroom",
            3: "Living room", 4: "Kitchen", 5: "Bathroom", 6: "Toilet",
            7: "Balcony", 8: "Dining room", 9: "Closet", 10: "Corridor",
            11: "Study", 12: "Kids' room", 13: "Entertainment room",
            14: "Storage room", 15: "Others",
        }
        for sub_type, name in expected.items():
            assert RoomInfo(room_sub_type=sub_type).display_name == name

    def test_user_assigned_name_wins(self) -> None:
        """A user-assigned name always wins over the table."""
        room = RoomInfo(room_sub_type=5, name="Powder Room")
        assert room.display_name == "Powder Room"

    def test_instance_index_appends(self) -> None:
        """Duplicate rooms get an instance-number suffix (Bathroom 2)."""
        room = RoomInfo(room_sub_type=5, instance_index=2)
        assert room.display_name == "Bathroom 2"

    def test_unknown_sub_type_falls_back_to_room(self) -> None:
        """An out-of-range sub-type falls back to the default name."""
        assert RoomInfo(room_sub_type=99).display_name == "Room"

    def test_map_data_from_response_resolves_names(self) -> None:
        """get_map parse resolves room names via the shared table."""
        decoded = {
            "2": {
                "12": [
                    {"1": 1, "2": 1, "3": b"", "4": 1, "8": 1},
                    {"1": 5, "2": 5, "3": b"", "4": 1, "8": 2},
                ],
            }
        }
        map_data = MapData.from_response(decoded)
        names = [r.display_name for r in map_data.rooms]
        assert names == ["Master bedroom", "Bathroom 2"]
