"""Vacuum behaviour on models whose reported state does not track reality.

A Freo Z Ultra (CX7, product key `hEA7OEshlx`) on firmware v01.13.11.02 never
broadcasts, and its polled state trails the robot. Captured live: it answered
`status/get_device_base_status` with a field-3 subtree of `{1: 19, 18: 1}`
unchanged across 40s while the robot physically drove back to its dock, never
reporting CLEANING or RETURNING.

Evaluated against that live state, every capability predicate returned False:
`can_start_cleaning`, `can_prepare_clean_start`, `can_return_home`,
`_can_accept_return_home`, `can_pause_cleaning`, `_can_stop_vacuum` and
`can_locate_robot`. The entity therefore advertised `STATE` and nothing else.

`is_docked` read True there and stays True while the robot is away, so
`async_return_to_base` also treats a recall as an idle-dock no-op.

The state does settle later (it subsequently read STANDBY, restoring several
predicates), so this is an intermittent loss of every control rather than a
permanent one.

Confirmed on hardware: the integration could neither start the robot nor send
it home, while the same commands sent directly to the same robot returned
SUCCESS and physically worked.

The robot's capabilities are not the limitation. `map/get_map` answers (map_id
1, rooms 1-3), and a `clean/start_clean` CleanTask naming one room in
vacuum-only mode was accepted and read back verbatim from
`clean/current_clean_task/get`. Only the local vetoes are relaxed here.
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock

import pytest

import tests.ha_stubs

tests.ha_stubs.install()

from narwal_client.client import NarwalClient, _robot_start_blocked  # noqa: E402
from narwal_client.const import CommandResult, WorkingStatus  # noqa: E402
from narwal_client.models import (  # noqa: E402
    CommandResponse,
    MapData,
    NarwalState,
    RoomInfo,
)
from tests.test_vacuum_segments import _make_vacuum  # noqa: E402

VacuumEntityFeature = sys.modules["homeassistant.components.vacuum"].VacuumEntityFeature
_HomeAssistantError = sys.modules["homeassistant.exceptions"].HomeAssistantError

ACTIONABLE = (
    VacuumEntityFeature.START
    | VacuumEntityFeature.STOP
    | VacuumEntityFeature.PAUSE
    | VacuumEntityFeature.RETURN_HOME
    | VacuumEntityFeature.LOCATE
)


def _cx7_state() -> NarwalState:
    """The only state a CX7 ever reports, whatever it is actually doing.

    Carries the map the real robot returns: `map/get_map` answers normally on
    these models, so room selection has everything it needs.
    """
    state = NarwalState(working_status=WorkingStatus.TASK_COMPLETED)
    state.map_data = MapData(
        map_id=1,
        rooms=[RoomInfo(room_id=1), RoomInfo(room_id=2), RoomInfo(room_id=3)],
    )
    return state


def _make_cx7(supports_broadcasts: bool = False):
    """Vacuum entity backed by a robot whose state never changes."""
    vac = _make_vacuum(state=_cx7_state())
    vac.coordinator.client.supports_broadcasts = supports_broadcasts
    ok = CommandResponse(result_code=CommandResult.SUCCESS)
    vac.coordinator.client.start_rooms = AsyncMock(return_value=ok)
    vac.coordinator.client.return_to_base = AsyncMock(return_value=ok)
    vac.coordinator.client.locate = AsyncMock(return_value=ok)
    vac.coordinator.client.pause = AsyncMock(return_value=ok)
    vac.coordinator.client.stop = AsyncMock(return_value=ok)
    vac.coordinator.client.resume = AsyncMock(return_value=ok)
    return vac


class TestNonBroadcastCapabilities:
    """A model with untrustworthy state still exposes its commands."""

    def test_actionable_features_are_advertised(self) -> None:
        """Every command the robot can arbitrate is offered."""
        features = _make_cx7().supported_features
        assert features & ACTIONABLE == ACTIONABLE

    def test_broadcasting_model_keeps_state_gating(self) -> None:
        """Regression guard: the Flow path must not be loosened.

        The same TASK_COMPLETED state on a broadcasting model still withholds
        START and RETURN_HOME, which is what #88 intended.
        """
        features = _make_cx7(supports_broadcasts=True).supported_features
        assert not features & VacuumEntityFeature.START
        assert not features & VacuumEntityFeature.RETURN_HOME

    def test_unavailable_still_reports_nothing(self) -> None:
        """An unreachable robot advertises no commands regardless of model."""
        vac = _make_cx7()
        vac.coordinator.last_update_success = False
        assert vac.supported_features == VacuumEntityFeature.STATE


class TestNonBroadcastCommands:
    """The commands dispatch instead of raising on a stale snapshot."""

    async def test_start_dispatches_a_room_clean(self) -> None:
        """The normal room path runs: the robot accepts a real CleanTask."""
        vac = _make_cx7()
        await vac.async_start()
        vac.coordinator.client.start_rooms.assert_awaited_once()
        room_ids = vac.coordinator.client.start_rooms.call_args.args[0]
        assert sorted(room_ids) == [1, 2, 3]

    async def test_return_to_base_dispatches(self) -> None:
        """TASK_COMPLETED must not veto the recall — the robot decides."""
        vac = _make_cx7()
        await vac.async_return_to_base()
        vac.coordinator.client.return_to_base.assert_awaited_once()

    async def test_locate_dispatches(self) -> None:
        """Locate is always safe to attempt."""
        vac = _make_cx7()
        await vac.async_locate()
        vac.coordinator.client.locate.assert_awaited_once()

    async def test_a_refused_command_still_surfaces(self) -> None:
        """Skipping local gating must not swallow the robot's own refusal."""
        vac = _make_cx7()
        vac.coordinator.client.start_rooms = AsyncMock(
            return_value=CommandResponse(result_code=CommandResult.NOT_READY)
        )
        with pytest.raises(_HomeAssistantError):
            await vac.async_start()


class TestStartGuard:
    """`_start_blocked` defers to the robot only where state is unusable."""

    @staticmethod
    def _client(supports_broadcasts: bool) -> NarwalClient:
        client = NarwalClient(
            host="10.0.0.1",
            device_id="d" * 32,
            topic_prefix="/hEA7OEshlx",
            supports_broadcasts=supports_broadcasts,
        )
        client.state.working_status = WorkingStatus.TASK_COMPLETED
        return client

    def test_state_that_blocks_a_flow_does_not_block_a_cx7(self) -> None:
        """The CX7's permanent clean-session context must not veto a start."""
        client = self._client(supports_broadcasts=False)
        assert _robot_start_blocked(client.state) is True
        assert client._start_blocked() is False

    def test_broadcasting_model_still_honours_the_guard(self) -> None:
        """Regression guard: the Flow keeps its protection against #25/#37."""
        client = self._client(supports_broadcasts=True)
        assert client._start_blocked() is True
