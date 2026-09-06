"""Tests for narwal_client.client — WebSocket client."""

from __future__ import annotations

import asyncio
import logging
import math
import struct
import time
from unittest.mock import AsyncMock, patch

import pytest

from narwal_client.client import NarwalClient, NarwalCommandError, NarwalConnectionError
from narwal_client.const import (
    TOPIC_CMD_CLEAN_TASK,
    TOPIC_CMD_DRY_DUST_BAG,
    TOPIC_CMD_DRY_MOP,
    TOPIC_CMD_DRY_STATION_BAG,
    TOPIC_CMD_DUST_GATHERING,
    TOPIC_CMD_FORCE_END,
    TOPIC_CMD_GET_BASE_STATUS,
    TOPIC_CMD_GET_MAP,
    TOPIC_CMD_RESUME,
    TOPIC_CMD_SET_FAN_LEVEL,
    TOPIC_CMD_WASH_MOP,
    AmbientLightCtrlType,
    CleaningRoute,
    CommandResult,
    FanLevel,
    WorkingStatus,
)
from narwal_client.models import (
    DOCK_TASK_DRY_DOCK_BAG,
    DOCK_TASK_DRY_DUST_BIN,
    DOCK_TASK_DRY_MOP,
    DOCK_TASK_EMPTY_DUSTBIN,
    DOCK_TASK_WASH_MOP,
    CommandResponse,
    MapData,
    RoomInfo,
)
from narwal_client.protocol import PROTOBUF_FIELD5_TAG, build_frame


def _float_stream(*values: float) -> bytes:
    """Encode a packed float32 stream as display_map field 2 uses it."""
    return b"".join(struct.pack("<f", value) for value in values)


class TestNarwalClientInit:
    """Tests for NarwalClient initialization."""

    def test_default_port(self) -> None:
        client = NarwalClient("192.168.1.100")
        assert client.host == "192.168.1.100"
        assert client.port == 9002
        assert client.url == "ws://192.168.1.100:9002"

    def test_custom_port(self) -> None:
        client = NarwalClient("10.0.0.1", port=8080)
        assert client.port == 8080
        assert client.url == "ws://10.0.0.1:8080"

    def test_initial_state(self) -> None:
        client = NarwalClient("10.0.0.1")
        assert not client.connected
        assert client.state.battery_level == 0

    def test_command_response_accepted_codes(self) -> None:
        """Narwal uses code 0 for accepted async commands and 1 for success."""
        accepted = CommandResponse(result_code=0)
        success = CommandResponse(result_code=CommandResult.SUCCESS)
        applied = CommandResponse(result_code=CommandResult.APPLIED)
        rejected = CommandResponse(result_code=CommandResult.NOT_APPLICABLE)

        assert accepted.accepted
        assert not accepted.success
        assert success.accepted
        assert success.success
        assert applied.accepted
        assert not rejected.accepted

    @pytest.mark.asyncio
    async def test_get_status_without_base_payload_returns_not_ready(self) -> None:
        """A get_status ack without robot_base_status field 2 is not a refresh."""
        client = NarwalClient("10.0.0.1")
        ack_without_status = CommandResponse(
            result_code=CommandResult.SUCCESS,
            data={"1": 1},
            raw_payload=b"raw",
        )

        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = ack_without_status
            result = await client.get_status()

        assert result.result_code == CommandResult.NOT_READY
        assert result.data == {"1": 1}
        assert result.raw_payload == b"raw"
        mock_send.assert_awaited_once_with(TOPIC_CMD_GET_BASE_STATUS)

    def test_topic_subscription_excludes_planned_route_trails(self) -> None:
        """Map trails come only from map/display_map's native trajectory window."""
        client = NarwalClient("10.0.0.1")
        payload = client._build_topic_subscription()

        assert b"map/display_map" in payload
        assert b"status/point_navi_plan_traj" not in payload
        assert b"developer/planning_debug_info" not in payload

    @pytest.mark.asyncio
    async def test_display_map_trajectory_updates_visual_data_only(self) -> None:
        """display_map field 2 must not infer robot task state."""
        client = NarwalClient("10.0.0.1", device_id="device")
        frame = build_frame(client._full_topic("map/display_map"), b"payload")
        client.state.working_status = WorkingStatus.CHARGED
        client.state.station_activity = 2
        client.state.dock_field11 = 3
        client.state.dock_field47 = 1
        client.state.task_progress_percent = 48

        with patch.object(
            client,
            "_decode_protobuf",
            return_value={
                "1": {"1": {"1": 1.25, "2": 1.0}},
                "2": {
                    "1": _float_stream(1.0, 1.25),
                    "2": _float_stream(2.0, 2.25),
                },
            },
        ):
            await client._handle_message(frame)

        assert client.state.map_display_data is not None
        assert client.state.map_display_data.trajectory_points() == [
            (1.0, 2.0),
            (1.25, 2.25),
        ]
        assert not client.state.is_cleaning
        assert client.state.is_docked
        assert client.state.is_station_active
        assert not hasattr(client.state, "last_map_robot_movement")

    @pytest.mark.asyncio
    async def test_display_map_non_finite_pair_preserves_trajectory_break(self) -> None:
        """Invalid native coordinates must not join the surrounding route."""
        client = NarwalClient("10.0.0.1", device_id="device")
        frame = build_frame(client._full_topic("map/display_map"), b"payload")

        with patch.object(
            client,
            "_decode_protobuf",
            return_value={
                "2": {
                    "1": _float_stream(1.0, float("nan"), 2.0),
                    "2": _float_stream(1.0, 1.5, 2.0),
                },
            },
        ):
            await client._handle_message(frame)

        display = client.state.map_display_data
        assert display is not None
        assert display.trajectory_points() == [(1.0, 1.0), (2.0, 2.0)]
        assert display.trajectory_breaks == (1,)
        render_points = display.trajectory_render_points()
        assert math.isnan(render_points[1][0])
        assert math.isnan(render_points[1][1])

    @pytest.mark.asyncio
    async def test_wait_for_response_marks_display_map_fresh(self) -> None:
        """display_map packets consumed while waiting for an ack are fresh."""
        client = NarwalClient("10.0.0.1", device_id="device")
        display_frame = build_frame(client._full_topic("map/display_map"), b"display")
        response_frame = bytearray(build_frame(client._full_topic("cmd/test"), b"ack"))
        response_frame[2] = PROTOBUF_FIELD5_TAG
        client._ws = AsyncMock()
        client._ws.recv = AsyncMock(side_effect=[display_frame, bytes(response_frame)])

        with patch.object(
            client,
            "_decode_protobuf",
            return_value={
                "1": {"1": {"1": 1.25, "2": 1.0}},
                "2": {
                    "1": _float_stream(1.0, 1.25),
                    "2": _float_stream(2.0, 2.25),
                },
            },
        ):
            msg = await client._wait_for_field5_response(1.0)

        assert msg.payload == b"ack"
        assert client.state.map_display_data is not None
        assert client.state.map_display_data.trajectory_points() == [
            (1.0, 2.0),
            (1.25, 2.25),
        ]
        assert client.last_display_map_age < 1.0

    def test_unconfirmed_idle_base_status_preserves_active_metrics(self) -> None:
        """Stale idle base_status must not hide a fresh working_status task."""
        client = NarwalClient("10.0.0.1")
        client._update_from_working_status_broadcast({"3": 120})

        client._update_from_base_status_broadcast(
            {"3": {"1": 1}, "11": 1, "47": 2, "2": 87.0}
        )

        assert client.state.working_status == WorkingStatus.UNKNOWN
        assert client.state.battery_level == 87
        assert client.state.is_cleaning

    def test_confirmed_dock_base_status_waits_for_active_metrics_to_expire(self) -> None:
        """Dock indicators win after fresh task-metric activity expires."""
        client = NarwalClient("10.0.0.1")
        with patch("narwal_client.models.time.monotonic", return_value=100.0):
            client._update_from_working_status_broadcast({"3": 120})

        docked = {"3": {"1": 10}, "11": 2, "47": 3}
        with patch("narwal_client.models.time.monotonic", return_value=101.0):
            client._update_from_base_status_broadcast(docked)
            assert client.state.working_status == WorkingStatus.UNKNOWN
            assert client.state.is_cleaning

        with patch("narwal_client.models.time.monotonic", return_value=116.0):
            client._update_from_base_status_broadcast(docked)

        assert client.state.working_status == WorkingStatus.DOCKED
        assert not client.state.has_recent_active_working_status
        assert client.state.is_docked

    @pytest.mark.asyncio
    async def test_commands_require_connection(self) -> None:
        client = NarwalClient("10.0.0.1")
        with pytest.raises(NarwalConnectionError):
            await client.start()

    @pytest.mark.asyncio
    async def test_accepted_resume_recovers_partial_metric_stream(self) -> None:
        """Accepted resume keeps partial progress active beyond the old TTL."""
        client = NarwalClient("10.0.0.1")
        client.state.working_status = WorkingStatus.STANDBY
        client.state.is_paused = True
        client.state.task_progress_percent = 25
        client.state.last_active_working_status_time = 100.0

        with (
            patch("narwal_client.models.time.monotonic", return_value=200.0),
            patch.object(
                client,
                "send_command",
                new_callable=AsyncMock,
                return_value=CommandResponse(result_code=CommandResult.SUCCESS),
            ) as mock_send,
        ):
            response = await client.resume()

        assert response.accepted
        mock_send.assert_awaited_once_with(TOPIC_CMD_RESUME, timeout=5.0)
        assert not client.state.is_paused

        with patch("narwal_client.models.time.monotonic", return_value=201.0):
            client.state.update_from_working_status({"1": 26})
        with patch("narwal_client.models.time.monotonic", return_value=215.0):
            client.state.update_from_working_status({"1": 27})
        with patch("narwal_client.models.time.monotonic", return_value=229.0):
            assert client.state.has_recent_active_working_status
            assert client.state.is_cleaning

    @pytest.mark.asyncio
    async def test_rejected_resume_does_not_change_paused_state(self) -> None:
        """Only an accepted robot response can establish resume activity."""
        client = NarwalClient("10.0.0.1")
        client.state.is_paused = True

        with patch.object(
            client,
            "send_command",
            new_callable=AsyncMock,
            return_value=CommandResponse(result_code=CommandResult.NOT_APPLICABLE),
        ):
            response = await client.resume()

        assert not response.accepted
        assert client.state.is_paused
        assert not client.state.has_recent_active_working_status

    @pytest.mark.asyncio
    async def test_accepted_resume_without_paused_clean_context_is_not_active(
        self,
    ) -> None:
        """An accepted idle no-op must not manufacture active-clean state."""
        client = NarwalClient("10.0.0.1")
        client.state.working_status = WorkingStatus.DOCKED

        with patch.object(
            client,
            "send_command",
            new_callable=AsyncMock,
            return_value=CommandResponse(result_code=CommandResult.SUCCESS),
        ):
            response = await client.resume()

        assert response.accepted
        assert client.state.is_docked
        assert not client.state.has_recent_active_working_status
        assert not client.state.is_cleaning

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status", (WorkingStatus.TASK_COMPLETED, WorkingStatus.ERROR)
    )
    async def test_resume_response_does_not_overwrite_intervening_terminal_status(
        self, status: WorkingStatus
    ) -> None:
        """A terminal packet received in flight outranks resume acceptance."""
        client = NarwalClient("10.0.0.1")
        client.state.working_status = WorkingStatus.STANDBY
        client.state.is_paused = True
        client.state.task_progress_percent = 25

        async def terminal_then_accept(*args, **kwargs) -> CommandResponse:
            client.state.update_from_base_status({"3": {"1": int(status)}})
            return CommandResponse(result_code=CommandResult.SUCCESS)

        with (
            patch("narwal_client.models.time.monotonic", return_value=200.0),
            patch.object(client, "send_command", side_effect=terminal_then_accept),
        ):
            response = await client.resume()

        assert response.accepted
        assert client.state.working_status == status
        assert not client.state.has_recent_active_working_status
        assert not client.state.is_cleaning

    @pytest.mark.asyncio
    async def test_resume_response_does_not_overwrite_new_pause_after_terminal(
        self,
    ) -> None:
        """A terminal event remains visible after later telemetry resets its timestamp."""
        client = NarwalClient("10.0.0.1")
        client.state.working_status = WorkingStatus.CLEANING
        client.state.is_paused = True
        client.state.task_progress_percent = 25

        async def terminal_then_paused(*args, **kwargs) -> CommandResponse:
            client.state.update_from_base_status(
                {"3": {"1": int(WorkingStatus.TASK_COMPLETED)}}
            )
            client.state.update_from_base_status(
                {
                    "3": {"1": int(WorkingStatus.CLEANING), "2": 1, "10": 2},
                    "11": 1,
                    "47": 2,
                }
            )
            return CommandResponse(result_code=CommandResult.SUCCESS)

        with patch.object(client, "send_command", side_effect=terminal_then_paused):
            response = await client.resume()

        assert response.accepted
        assert client.state.is_paused
        assert not client.state.has_recent_active_working_status

    @pytest.mark.asyncio
    async def test_resume_observes_suppressed_docked_broadcast(self) -> None:
        """A dock event blocks resume inference even when its stale state is ignored."""
        client = NarwalClient("10.0.0.1")
        client.state.working_status = WorkingStatus.CLEANING
        client.state.is_paused = True
        client.state.task_progress_percent = 25
        client.state.last_active_working_status_time = time.monotonic()

        async def docked_then_accept(*args, **kwargs) -> CommandResponse:
            client._update_from_base_status_broadcast(
                {
                    "3": {"1": int(WorkingStatus.DOCKED), "10": 1},
                    "11": 2,
                    "47": 3,
                }
            )
            return CommandResponse(result_code=CommandResult.SUCCESS)

        with patch.object(client, "send_command", side_effect=docked_then_accept):
            response = await client.resume()

        assert response.accepted
        assert client.state.working_status == WorkingStatus.CLEANING
        assert client.state.is_paused

    @pytest.mark.asyncio
    async def test_resume_ignores_suppressed_off_dock_standby_label(self) -> None:
        """An off-dock standby label is not terminal proof for resume ordering."""
        client = NarwalClient("10.0.0.1")
        client.state.working_status = WorkingStatus.CLEANING
        client.state.is_paused = True
        client.state.task_progress_percent = 25
        client.state.last_active_working_status_time = time.monotonic()
        client.state.dock_sub_state = 2
        client.state.dock_field11 = 1
        client.state.dock_field47 = 2

        async def standby_then_accept(*args, **kwargs) -> CommandResponse:
            client._update_from_base_status_broadcast(
                {
                    "3": {"1": int(WorkingStatus.STANDBY), "10": 2},
                    "11": 1,
                    "47": 2,
                }
            )
            return CommandResponse(result_code=CommandResult.SUCCESS)

        with patch.object(client, "send_command", side_effect=standby_then_accept):
            response = await client.resume()

        assert response.accepted
        assert not client.state.is_paused
        assert client.state.has_recent_active_working_status

    @pytest.mark.asyncio
    async def test_resume_prefers_current_off_dock_over_retained_dock_state(self) -> None:
        """Current off-dock fields outrank stale retained dock indicators."""
        client = NarwalClient("10.0.0.1")
        client.state.working_status = WorkingStatus.CLEANING
        client.state.is_paused = True
        client.state.task_progress_percent = 25
        client.state.last_active_working_status_time = time.monotonic()
        client.state.dock_sub_state = 1
        client.state.dock_field11 = 2
        client.state.dock_field47 = 3

        async def stale_label_then_accept(*args, **kwargs) -> CommandResponse:
            client._update_from_base_status_broadcast(
                {
                    "3": {"1": int(WorkingStatus.DOCKED), "10": 2},
                    "11": 1,
                    "47": 2,
                }
            )
            return CommandResponse(result_code=CommandResult.SUCCESS)

        with patch.object(client, "send_command", side_effect=stale_label_then_accept):
            response = await client.resume()

        assert response.accepted
        assert not client.state.is_paused
        assert client.state.has_recent_active_working_status

    @pytest.mark.asyncio
    async def test_send_raw_without_connection_raises(self) -> None:
        client = NarwalClient("10.0.0.1")
        with pytest.raises(NarwalConnectionError):
            await client.send_raw("test/topic", b"\x08\x01")

    @pytest.mark.asyncio
    async def test_set_ambient_light_mode_accepts_applied(self) -> None:
        """Dock light command accepts the observed applied result code."""
        client = NarwalClient("10.0.0.1")
        applied = CommandResponse(result_code=CommandResult.APPLIED)

        with patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = applied
            result = await client.set_ambient_light_mode(
                AmbientLightCtrlType.NIGHT_LIGHT
            )

        assert result is applied
        mock_send.assert_awaited_once_with(
            "supply/ambient_light_ctrl",
            payload=b"\x08\x01",
        )

    @pytest.mark.asyncio
    async def test_set_fan_speed_sends_supported_live_level(self) -> None:
        client = NarwalClient("10.0.0.1")
        accepted = CommandResponse(result_code=CommandResult.SUCCESS)

        with patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = accepted
            result = await client.set_fan_speed(FanLevel.DEEP)

        assert result is accepted
        mock_send.assert_awaited_once_with(
            TOPIC_CMD_SET_FAN_LEVEL,
            b"\x08\x04",
        )

    @pytest.mark.asyncio
    async def test_set_fan_speed_clamps_super_to_deep(self) -> None:
        client = NarwalClient("10.0.0.1")
        accepted = CommandResponse(result_code=CommandResult.SUCCESS)

        with patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = accepted
            result = await client.set_fan_speed(FanLevel.SUPER)

        assert result is accepted
        mock_send.assert_awaited_once_with(
            TOPIC_CMD_SET_FAN_LEVEL,
            b"\x08\x04",
        )

    @pytest.mark.asyncio
    async def test_device_info_updates_firmware_state(self) -> None:
        """Device identity queries populate the firmware sensor state."""
        client = NarwalClient("10.0.0.1")
        response = CommandResponse(
            data={
                "1": b"hEA7OEshlx",
                "2": b"0123456789abcdef0123456789abcdef",
                "3": b"v01.13.11.02",
            }
        )

        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = response
            info = await client.get_device_info()

        assert info.firmware_version == "v01.13.11.02"
        assert client.state.firmware_version == "v01.13.11.02"

    def test_addressed_response_marks_non_broadcast_model_reachable(self) -> None:
        """Non-broadcast models use command responses as reachability evidence."""
        client = NarwalClient("10.0.0.1", supports_broadcasts=False)
        assert not client.robot_awake

        client._mark_response_received()

        assert client.robot_awake
        assert client._last_response_time > 0

    @pytest.mark.asyncio
    async def test_non_broadcast_wake_returns_without_waiting(self) -> None:
        """A connected non-broadcast model does not wait for impossible broadcasts."""
        client = NarwalClient("10.0.0.1", supports_broadcasts=False)
        client._ws = AsyncMock()
        client._connected.set()

        with patch.object(client, "_send_wake_burst", new_callable=AsyncMock) as wake_burst:
            assert await client.wake(timeout=10.0)

        wake_burst.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_discover_device_id_preserves_field5_product_key(self) -> None:
        """Auto-discovery must keep the product key that answered the wake probe."""
        client = NarwalClient("10.0.0.1")
        client._ws = AsyncMock()
        client._connected.set()
        client._ws.recv.return_value = bytes(
            [0x01, 0x02, PROTOBUF_FIELD5_TAG, 0x00, 0x08, 0x01]
        )

        with patch.object(
            client,
            "_decode_protobuf",
            return_value={
                "1": b"DrzDKQ0MU8",
                "2": b"auto_device_456",
            },
        ):
            device_id = await client.discover_device_id(timeout=1.0)

        assert device_id == "auto_device_456"
        assert client.device_id == "auto_device_456"
        assert client.topic_prefix == "/DrzDKQ0MU8"


class TestBuildCleanPayloadV2:
    """Tests for the v2 clean payload schema introduced for firmware
    v01.07.22+ (issue #36)."""

    def test_single_room_uses_nested_room_id(self) -> None:
        """Single room encodes as a nested {1:1, 2:room_id} message."""
        import blackboxprotobuf

        client = NarwalClient("127.0.0.1")
        payload = client._build_clean_payload_v2([7])
        decoded, _ = blackboxprotobuf.decode_message(payload)

        outer = decoded["1"]
        assert outer["1"] == 1
        assert outer["5"] == 6  # observed task source marker

        entry = outer["2"]
        assert entry["1"]["2"] == 7, "Room ID lives at 1.2.1.2 in v2 schema"
        assert entry["1"]["1"] == 1, "Inner field 1.2.1.1 was 1 in observed capture"
        assert entry["3"] == 1, "Sequence index starts at 1"

    def test_multiple_rooms_get_sequence_indices(self) -> None:
        """Multiple rooms preserve order via the 3:<seq> field, 1-indexed."""
        import blackboxprotobuf

        client = NarwalClient("127.0.0.1")
        payload = client._build_clean_payload_v2([5, 9, 3])
        decoded, _ = blackboxprotobuf.decode_message(payload)

        entries = decoded["1"]["2"]
        assert isinstance(entries, list)
        assert len(entries) == 3
        assert [e["1"]["2"] for e in entries] == [5, 9, 3]
        assert [e["3"] for e in entries] == [1, 2, 3]

    def test_default_clean_params(self) -> None:
        """Default suction=3 / mop=2 / passes=1 / cleanMode=3 — Flow 1 max."""
        import blackboxprotobuf

        client = NarwalClient("127.0.0.1")
        payload = client._build_clean_payload_v2([1])
        decoded, _ = blackboxprotobuf.decode_message(payload)

        params = decoded["1"]["2"]["2"]
        assert params["1"] == 3, "suction default 3 (Flow 1 max)"
        assert params["2"] == 3, "cleanMode default 3 (sweep+mop in v2)"
        assert params["3"] == 1, "passes default 1"
        assert params["7"] == 2, "mop_humidity default 2 (wet)"

    def test_custom_params_propagate(self) -> None:
        """Caller-provided params (suction=4 for Flow 2) reach the wire."""
        import blackboxprotobuf

        client = NarwalClient("127.0.0.1")
        payload = client._build_clean_payload_v2(
            [1], suction=4, mop_humidity=1, passes=2, clean_mode=3
        )
        decoded, _ = blackboxprotobuf.decode_message(payload)
        params = decoded["1"]["2"]["2"]
        assert params["1"] == 4
        assert params["7"] == 1
        assert params["3"] == 2

class TestWholeHouseStart:
    """start() must route a whole-house clean through clean/start_clean (#69).

    The old implementation sent a minimal payload to clean/plan/start and
    returned on any result other than NOT_APPLICABLE. Newer firmware answers
    SUCCESS there and does nothing, so callers saw a start that never happened.
    """

    def _connected_client(self) -> NarwalClient:
        client = NarwalClient("127.0.0.1")
        client._ws = AsyncMock()
        client._connected.set()
        client.state.update_from_base_status(
            {"3": {"1": int(WorkingStatus.DOCKED), "10": 1}, "11": 2}
        )
        return client

    @pytest.mark.asyncio
    async def test_start_uses_start_clean_with_every_room(self) -> None:
        """Cached map rooms are all enumerated onto clean/start_clean."""
        client = self._connected_client()
        client.state.map_data = MapData(
            map_id=1, rooms=[RoomInfo(room_id=11), RoomInfo(room_id=14)]
        )
        success = CommandResponse(result_code=CommandResult.SUCCESS)

        with patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = success
            result = await client.start()

        assert result is success
        topic = mock_send.await_args_list[0].args[0]
        assert topic == TOPIC_CMD_CLEAN_TASK

    @pytest.mark.asyncio
    async def test_start_sends_only_start_clean(self) -> None:
        """Regression guard for #69: no path sends clean/plan/start."""
        client = self._connected_client()
        client.state.map_data = MapData(map_id=1, rooms=[RoomInfo(room_id=3)])

        with patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = CommandResponse(
                result_code=CommandResult.SUCCESS
            )
            await client.start()

        sent_topics = [call.args[0] for call in mock_send.await_args_list]
        assert sent_topics == [TOPIC_CMD_CLEAN_TASK]

    @pytest.mark.asyncio
    async def test_start_forwards_clean_settings(self) -> None:
        """Clean params reach the CleanTask payload rather than being dropped."""
        client = self._connected_client()
        client.state.map_data = MapData(map_id=1, rooms=[RoomInfo(room_id=6)])

        with patch.object(
            client, "start_rooms", new_callable=AsyncMock
        ) as mock_rooms:
            mock_rooms.return_value = CommandResponse(
                result_code=CommandResult.SUCCESS
            )
            await client.start(
                fan=FanLevel.STRONG,
                passes=2,
                route=CleaningRoute.METICULOUS,
            )

        assert mock_rooms.await_args.args[0] == [6]
        assert mock_rooms.await_args.kwargs["fan"] is FanLevel.STRONG
        assert mock_rooms.await_args.kwargs["passes"] == 2
        assert mock_rooms.await_args.kwargs["route"] is CleaningRoute.METICULOUS

    @pytest.mark.asyncio
    async def test_start_without_rooms_returns_not_ready(self) -> None:
        """No map rooms means no explicit whole-house payload can be built."""
        client = self._connected_client()
        assert client.state.map_data is None

        with patch.object(
            client, "get_map", new_callable=AsyncMock
        ) as mock_map, patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send:
            mock_map.side_effect = RuntimeError("no map")
            result = await client.start()

        assert result.result_code == CommandResult.NOT_READY
        mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_start_ignores_zero_room_ids(self) -> None:
        """A map whose rooms all have id 0 is treated as having no rooms."""
        client = self._connected_client()
        client.state.map_data = MapData(map_id=1, rooms=[RoomInfo(room_id=0)])

        with patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = CommandResponse(
                result_code=CommandResult.SUCCESS
            )
            result = await client.start()

        assert result.result_code == CommandResult.NOT_READY
        mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_start_rooms_with_empty_list_returns_not_ready(self) -> None:
        """start_rooms([]) does not dispatch an ambiguous saved plan."""
        client = self._connected_client()
        client.state.map_data = MapData(map_id=1, rooms=[RoomInfo(room_id=2)])

        with patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = CommandResponse(
                result_code=CommandResult.SUCCESS
            )
            result = await client.start_rooms([])

        mock_send.assert_not_awaited()
        assert result.result_code == CommandResult.NOT_READY

    @pytest.mark.asyncio
    async def test_start_rooms_rejects_active_dock_task(self) -> None:
        """Direct room starts are blocked while incompatible dock work is active."""
        client = self._connected_client()
        client.state.map_data = MapData(map_id=1, rooms=[RoomInfo(room_id=2)])
        client.state.station_activity = 1

        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            result = await client.start_rooms([2])

        assert result.result_code == CommandResult.NOT_APPLICABLE
        mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_start_rooms_allows_docked_task_completed(self) -> None:
        """Dock presence makes a retained TASK_COMPLETED label idle."""
        client = self._connected_client()
        client.state.map_data = MapData(map_id=1, rooms=[RoomInfo(room_id=2)])
        client.state.update_from_base_status(
            {"3": {"1": int(WorkingStatus.TASK_COMPLETED), "3": 6}, "11": 2}
        )
        success = CommandResponse(result_code=CommandResult.SUCCESS)

        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = success
            result = await client.start_rooms([2])

        assert result is success
        mock_send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_start_rooms_rejects_task_completed_without_current_dock_proof(
        self,
    ) -> None:
        """A status-only completion packet cannot reuse retained dock fields."""
        client = self._connected_client()
        client.state.map_data = MapData(map_id=1, rooms=[RoomInfo(room_id=2)])
        client.state.update_from_base_status(
            {"3": {"1": int(WorkingStatus.TASK_COMPLETED)}}
        )

        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            result = await client.start_rooms([2])

        assert result.result_code == CommandResult.NOT_APPLICABLE
        mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_start_rooms_reserves_private_guard_on_acceptance(self) -> None:
        """Accepted direct room starts block follow-up starts until telemetry arrives."""
        client = self._connected_client()
        client.state.map_data = MapData(map_id=1, rooms=[RoomInfo(room_id=2)])
        success = CommandResponse(result_code=CommandResult.SUCCESS)

        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = success
            first = await client.start_rooms([2])
            second = await client.start_rooms([2])

        assert first is success
        assert second.result_code == CommandResult.NOT_APPLICABLE
        assert client.state.has_assumed_robot_clean
        mock_send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_concurrent_direct_start_rooms_serialize_preflight(self) -> None:
        """Only one direct room start validates before the accepted-command guard."""
        client = self._connected_client()
        client.state.map_data = MapData(map_id=1, rooms=[RoomInfo(room_id=2)])
        success = CommandResponse(result_code=CommandResult.SUCCESS)
        command_started = asyncio.Event()
        release_command = asyncio.Event()

        async def slow_send(*args, **kwargs):
            command_started.set()
            await release_command.wait()
            return success

        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            mock_send.side_effect = slow_send
            first = asyncio.create_task(client.start_rooms([2]))
            await command_started.wait()
            second = asyncio.create_task(client.start_rooms([2]))
            await asyncio.sleep(0)
            release_command.set()
            first_result, second_result = await asyncio.gather(first, second)

        assert first_result is success
        assert second_result.result_code == CommandResult.NOT_APPLICABLE
        mock_send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_start_rejects_active_dock_task_before_map_lookup(self) -> None:
        """Whole-house start is blocked before map lookup when dock work is active."""
        client = self._connected_client()
        client.state.station_activity = 1

        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            result = await client.start()

        assert result.result_code == CommandResult.NOT_APPLICABLE
        mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_start_easy_clean_rejects_active_dock_task(self) -> None:
        """Quick clean uses the same dock-task guard as other robot starts."""
        client = self._connected_client()
        client.state.station_activity = 1

        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            result = await client.start_easy_clean()

        assert result.result_code == CommandResult.NOT_APPLICABLE
        mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_start_easy_clean_reserves_private_guard_on_acceptance(self) -> None:
        """Accepted quick-clean starts use the same duplicate-start guard."""
        client = self._connected_client()
        success = CommandResponse(result_code=CommandResult.SUCCESS)

        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = success
            first = await client.start_easy_clean()
            second = await client.start_easy_clean()

        assert first is success
        assert second.result_code == CommandResult.NOT_APPLICABLE
        assert client.state.has_assumed_robot_clean
        mock_send.assert_awaited_once()

    @pytest.mark.parametrize(
        ("task", "fields"),
        (
            (DOCK_TASK_DRY_MOP, ("8", "9")),
            (DOCK_TASK_DRY_DUST_BIN, ("10", "11")),
            (DOCK_TASK_DRY_DOCK_BAG, ("12", "13")),
        ),
    )
    @pytest.mark.asyncio
    async def test_start_rooms_allows_typed_drying(
        self,
        task: str,
        fields: tuple[str, str],
    ) -> None:
        """Each typed drying task is compatible with robot start."""
        client = self._connected_client()
        client.state.map_data = MapData(map_id=1, rooms=[RoomInfo(room_id=2)])
        client.state.set_dock_drying_task(
            task,
            elapsed=60,
            target=180,
            fields=fields,
        )
        success = CommandResponse(result_code=CommandResult.SUCCESS)

        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = success
            result = await client.start_rooms([2])

        assert result is success
        mock_send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_map_rejection_preserves_cache_but_raises(self) -> None:
        """A rejected refresh leaves display data intact but fails validation."""
        client = self._connected_client()
        cached = MapData(map_id=7, rooms=[RoomInfo(room_id=2)])
        client.state.map_data = cached

        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = CommandResponse(
                result_code=CommandResult.NOT_APPLICABLE,
                data={},
            )
            with pytest.raises(NarwalCommandError, match="get_map failed"):
                await client.get_map()

        assert client.state.map_data is cached
        mock_send.assert_awaited_once_with(TOPIC_CMD_GET_MAP, timeout=15.0)

    @pytest.mark.asyncio
    async def test_get_map_rejection_without_cache_raises(self) -> None:
        """A rejected map refresh with no cache is a command failure."""
        client = self._connected_client()

        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = CommandResponse(
                result_code=CommandResult.NOT_APPLICABLE,
                data={},
            )
            with pytest.raises(NarwalCommandError, match="get_map failed"):
                await client.get_map()

        mock_send.assert_awaited_once_with(TOPIC_CMD_GET_MAP, timeout=15.0)

    @pytest.mark.asyncio
    async def test_get_map_empty_payload_without_cache_raises(self) -> None:
        """A payloadless accepted map response is not a valid empty map."""
        client = self._connected_client()

        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = CommandResponse(data={})
            with pytest.raises(NarwalCommandError, match="active map"):
                await client.get_map()

        mock_send.assert_awaited_once_with(TOPIC_CMD_GET_MAP, timeout=15.0)

    @pytest.mark.asyncio
    async def test_get_map_empty_payload_preserves_cache_but_raises(self) -> None:
        """An empty accepted refresh cannot validate cached room topology."""
        client = self._connected_client()
        cached = MapData(map_id=7, rooms=[RoomInfo(room_id=2)])
        client.state.map_data = cached

        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = CommandResponse(data={})
            with pytest.raises(NarwalCommandError, match="active map"):
                await client.get_map()

        assert client.state.map_data is cached
        mock_send.assert_awaited_once_with(TOPIC_CMD_GET_MAP, timeout=15.0)


class TestDockTaskCommands:
    """Dock task commands and scoped dock-task stop behavior."""

    def _docked_client(self) -> NarwalClient:
        """Return a client with explicit idle-docked robot state."""
        client = NarwalClient("127.0.0.1")
        client.state.working_status = WorkingStatus.DOCKED
        client.state.dock_presence = 6
        client.state.dock_field11 = 2
        return client

    def _docked_status_response(self) -> CommandResponse:
        """Return a valid docked base-status refresh response."""
        return CommandResponse(
            data={"2": {"3": {"1": int(WorkingStatus.DOCKED)}, "11": 2}},
        )

    @pytest.mark.asyncio
    async def test_empty_dustbin_command_assumes_task_on_success(self) -> None:
        client = self._docked_client()
        success = CommandResponse(result_code=CommandResult.SUCCESS)

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send:
            mock_status.return_value = self._docked_status_response()
            mock_send.return_value = success
            result = await client.empty_dustbin()

        assert result is success
        mock_status.assert_awaited_once_with(full_update=True)
        mock_send.assert_awaited_once_with(TOPIC_CMD_DUST_GATHERING)
        assert client.state.assumed_active_dock_task == DOCK_TASK_EMPTY_DUSTBIN

    @pytest.mark.asyncio
    async def test_empty_dustbin_allows_docked_task_completed(self) -> None:
        """Dock presence makes a retained TASK_COMPLETED label idle."""
        client = self._docked_client()
        client.state.update_from_base_status(
            {"3": {"1": int(WorkingStatus.TASK_COMPLETED), "3": 6}, "11": 2}
        )
        success = CommandResponse(result_code=CommandResult.SUCCESS)

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send:
            mock_status.return_value = self._docked_status_response()
            mock_send.return_value = success
            result = await client.empty_dustbin()

        assert result is success
        mock_send.assert_awaited_once_with(TOPIC_CMD_DUST_GATHERING)

    @pytest.mark.asyncio
    async def test_wash_mop_command_assumes_task_on_success(self) -> None:
        client = self._docked_client()
        success = CommandResponse(result_code=CommandResult.SUCCESS)

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send:
            mock_status.return_value = self._docked_status_response()
            mock_send.return_value = success
            result = await client.wash_mop()

        assert result is success
        mock_status.assert_awaited_once_with(full_update=True)
        mock_send.assert_awaited_once_with(TOPIC_CMD_WASH_MOP)
        assert client.state.assumed_active_dock_task == DOCK_TASK_WASH_MOP

    @pytest.mark.asyncio
    async def test_dry_mop_command_assumes_task_on_success(self) -> None:
        client = self._docked_client()
        success = CommandResponse(result_code=CommandResult.SUCCESS)

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send:
            mock_status.return_value = self._docked_status_response()
            mock_send.return_value = success
            result = await client.dry_mop()

        assert result is success
        mock_status.assert_awaited_once_with(full_update=True)
        mock_send.assert_awaited_once_with(TOPIC_CMD_DRY_MOP)
        assert client.state.assumed_active_dock_task == DOCK_TASK_DRY_MOP

    @pytest.mark.asyncio
    async def test_dry_dust_bag_command_assumes_task_on_success(self) -> None:
        client = self._docked_client()
        success = CommandResponse(result_code=CommandResult.SUCCESS)

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send:
            mock_status.return_value = self._docked_status_response()
            mock_send.return_value = success
            result = await client.dry_dust_bag()

        assert result is success
        mock_status.assert_awaited_once_with(full_update=True)
        mock_send.assert_awaited_once_with(TOPIC_CMD_DRY_DUST_BAG)
        assert client.state.assumed_active_dock_drying_task == DOCK_TASK_DRY_DUST_BIN

    @pytest.mark.asyncio
    async def test_dry_station_bag_command_assumes_task_on_success(self) -> None:
        client = self._docked_client()
        success = CommandResponse(result_code=CommandResult.SUCCESS)

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send:
            mock_status.return_value = self._docked_status_response()
            mock_send.return_value = success
            result = await client.dry_station_bag()

        assert result is success
        mock_status.assert_awaited_once_with(full_update=True)
        mock_send.assert_awaited_once_with(TOPIC_CMD_DRY_STATION_BAG)
        assert client.state.assumed_active_dock_drying_task == DOCK_TASK_DRY_DOCK_BAG

    @pytest.mark.asyncio
    async def test_direct_dock_command_rejects_cleaning_state(self) -> None:
        """Direct client dock commands cannot bypass robot-work gating."""
        client = NarwalClient("127.0.0.1")
        client.state.working_status = WorkingStatus.CLEANING

        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            result = await client.empty_dustbin()

        assert result.result_code == CommandResult.NOT_APPLICABLE
        mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_direct_dock_command_rejects_existing_dock_task(self) -> None:
        """Only one unscoped dock start may be dispatched at a time."""
        client = self._docked_client()
        client.state.station_activity = 1

        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            result = await client.wash_mop_by_robot_status()

        assert result.result_code == CommandResult.NOT_APPLICABLE
        mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_direct_dock_command_rejects_private_command_guard(self) -> None:
        """Accepted-command guards serialize direct dock starts without publishing state."""
        client = self._docked_client()
        client.state.assume_dock_task(DOCK_TASK_EMPTY_DUSTBIN)

        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            result = await client.wash_mop_by_robot_status()

        assert result.result_code == CommandResult.NOT_APPLICABLE
        mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_concurrent_direct_dock_commands_serialize_preflight(self) -> None:
        """Only one direct dock start validates and reserves an idle snapshot."""
        client = self._docked_client()
        success = CommandResponse(result_code=CommandResult.SUCCESS)
        command_started = asyncio.Event()
        release_command = asyncio.Event()

        async def slow_send(*args, **kwargs):
            command_started.set()
            await release_command.wait()
            return success

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send:
            mock_status.return_value = CommandResponse(
                data={"2": {"3": {"1": int(WorkingStatus.DOCKED)}, "11": 2}},
            )
            mock_send.side_effect = slow_send
            first = asyncio.create_task(client.dry_dust_bag())
            await command_started.wait()
            second = asyncio.create_task(client.dry_station_bag())
            await asyncio.sleep(0)
            release_command.set()
            first_result, second_result = await asyncio.gather(first, second)

        assert first_result is success
        assert second_result.result_code == CommandResult.NOT_APPLICABLE
        assert mock_status.await_count == 1
        mock_send.assert_awaited_once_with(TOPIC_CMD_DRY_DUST_BAG)
        assert client.state.assumed_active_dock_task == DOCK_TASK_DRY_DUST_BIN

    @pytest.mark.asyncio
    async def test_direct_robot_and_dock_starts_share_action_preflight(self) -> None:
        """A robot clean and dock task cannot both validate the same idle snapshot."""
        client = self._docked_client()
        client.state.map_data = MapData(map_id=1, rooms=[RoomInfo(room_id=2)])
        success = CommandResponse(result_code=CommandResult.SUCCESS)
        command_started = asyncio.Event()
        release_command = asyncio.Event()

        async def slow_send(*args, **kwargs):
            command_started.set()
            await release_command.wait()
            return success

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send:
            mock_status.return_value = self._docked_status_response()
            mock_send.side_effect = slow_send
            robot = asyncio.create_task(client.start_rooms([2]))
            await command_started.wait()
            dock = asyncio.create_task(client.empty_dustbin())
            await asyncio.sleep(0)
            release_command.set()
            robot_result, dock_result = await asyncio.gather(robot, dock)

        assert robot_result is success
        assert dock_result.result_code == CommandResult.NOT_APPLICABLE
        mock_status.assert_not_awaited()
        mock_send.assert_awaited_once()
        assert client.state.has_assumed_robot_clean

    @pytest.mark.asyncio
    async def test_direct_dock_command_refreshes_before_start(self) -> None:
        """Direct dock starts revalidate the device state inside the action lock."""
        client = self._docked_client()

        async def stale_refresh(*args, **kwargs):
            client.state.working_status = WorkingStatus.CLEANING
            return CommandResponse(
                data={"2": {"3": {"1": int(WorkingStatus.CLEANING)}}},
            )

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send:
            mock_status.side_effect = stale_refresh
            result = await client.dry_dust_bag()

        assert result.result_code == CommandResult.NOT_APPLICABLE
        mock_status.assert_awaited_once_with(full_update=True)
        mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_direct_dock_command_rejects_retained_paused_clean(self) -> None:
        """Expired metric freshness does not make a paused clean dock-idle."""
        client = self._docked_client()
        client.state.working_status = WorkingStatus.STANDBY
        client.state.is_paused = True
        client.state.cleaning_time = 120
        client.state.task_elapsed_time = 120

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send:
            result = await client.empty_dustbin()

        assert result.result_code == CommandResult.NOT_APPLICABLE
        mock_status.assert_not_awaited()
        mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_direct_dock_command_rejects_battery_only_refresh(self) -> None:
        """Direct dock starts require live dock status, not only any status field."""
        client = self._docked_client()

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send:
            mock_status.return_value = CommandResponse(data={"2": {"2": 85.0}})
            result = await client.empty_dustbin()

        assert result.result_code == CommandResult.NOT_READY
        mock_status.assert_awaited_once_with(full_update=True)
        mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stop_dock_task_rejects_unscoped_clean_context(self) -> None:
        """Direct dock stops cannot use generic force-end during robot work."""
        client = self._docked_client()
        client.state.working_status = WorkingStatus.CLEANING
        client.state.station_activity = 1

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(client, "stop", new_callable=AsyncMock) as mock_stop:
            mock_status.return_value = self._docked_status_response()
            result = await client.stop_dock_task(DOCK_TASK_EMPTY_DUSTBIN)

        assert result.result_code == CommandResult.NOT_APPLICABLE
        mock_status.assert_awaited_once_with(full_update=True)
        mock_stop.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stop_empty_dustbin_allows_task_completed_dock_context(self) -> None:
        """Dock emptying reports TASK_COMPLETED, but its generic stop is dock-scoped."""
        client = self._docked_client()
        client.state.working_status = WorkingStatus.TASK_COMPLETED
        client.state.station_activity = 1
        success = CommandResponse(result_code=CommandResult.SUCCESS)

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(
            client, "stop", new_callable=AsyncMock
        ) as mock_stop, patch.object(
            client, "_refresh_after_dock_stop", new_callable=AsyncMock
        ) as mock_refresh, patch(
            "narwal_client.client.asyncio.sleep", new_callable=AsyncMock
        ):
            mock_status.return_value = self._docked_status_response()
            mock_stop.return_value = success
            mock_refresh.return_value = True
            result = await client.stop_dock_task(DOCK_TASK_EMPTY_DUSTBIN)

        assert result is success
        mock_status.assert_awaited_once_with(full_update=True)
        mock_stop.assert_awaited_once_with(timeout=15.0)

    @pytest.mark.asyncio
    async def test_stop_empty_dustbin_rejects_task_completed_without_dock_proof(
        self,
    ) -> None:
        """Generic dock stop needs a dock signal, not just TASK_COMPLETED."""
        client = NarwalClient("127.0.0.1")
        client.state.working_status = WorkingStatus.TASK_COMPLETED
        client.state.station_activity = 1

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(client, "stop", new_callable=AsyncMock) as mock_stop:
            mock_status.return_value = self._docked_status_response()
            result = await client.stop_dock_task(DOCK_TASK_EMPTY_DUSTBIN)

        assert result.result_code == CommandResult.NOT_APPLICABLE
        mock_status.assert_awaited_once_with(full_update=True)
        mock_stop.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stop_dry_station_bag_uses_scoped_force_end_payload(self) -> None:
        client = NarwalClient("127.0.0.1")
        client.state.set_dock_drying_task(
            DOCK_TASK_DRY_DOCK_BAG,
            elapsed=60,
            target=180,
            fields=("12", "13"),
        )
        success = CommandResponse(result_code=CommandResult.SUCCESS)

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send, patch.object(
            client, "_refresh_after_dock_stop", new_callable=AsyncMock
        ) as mock_refresh, patch(
            "narwal_client.client.asyncio.sleep", new_callable=AsyncMock
        ):
            mock_status.return_value = self._docked_status_response()
            mock_send.return_value = success
            mock_refresh.return_value = True
            result = await client.stop_dock_task(DOCK_TASK_DRY_DOCK_BAG)

        assert result is success
        mock_send.assert_awaited_once_with(
            TOPIC_CMD_FORCE_END,
            payload=b"\x08\x01",
            timeout=15.0,
        )
        mock_status.assert_awaited_once_with(full_update=True)
        assert DOCK_TASK_DRY_DOCK_BAG not in client.state.dock_drying_tasks

    @pytest.mark.asyncio
    async def test_stop_dry_station_bag_allows_unmapped_coarse_activity(self) -> None:
        """Typed dock-bag force-end stays safe when coarse station flags are stale."""
        client = NarwalClient("127.0.0.1")
        client.state.station_activity = 99
        client.state.set_dock_drying_task(
            DOCK_TASK_DRY_DOCK_BAG,
            elapsed=60,
            target=180,
            fields=("12", "13"),
        )
        success = CommandResponse(result_code=CommandResult.SUCCESS)

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send, patch.object(
            client, "_refresh_after_dock_stop", new_callable=AsyncMock
        ) as mock_refresh, patch(
            "narwal_client.client.asyncio.sleep", new_callable=AsyncMock
        ):
            mock_status.return_value = self._docked_status_response()
            mock_send.return_value = success
            mock_refresh.return_value = True
            result = await client.stop_dock_task(DOCK_TASK_DRY_DOCK_BAG)

        assert result is success
        mock_send.assert_awaited_once_with(
            TOPIC_CMD_FORCE_END,
            payload=b"\x08\x01",
            timeout=15.0,
        )
        mock_status.assert_awaited_once_with(full_update=True)

    @pytest.mark.asyncio
    async def test_stop_dry_station_bag_waits_for_typed_task_after_unmapped_snapshot(
        self,
    ) -> None:
        """Scoped dry stop waits for typed telemetry after a coarse active snapshot."""
        client = NarwalClient("127.0.0.1")
        client.state.working_status = WorkingStatus.TASK_COMPLETED
        client.state.station_activity = 4
        client.state.dock_presence = 6
        client.state.dock_field11 = 2
        success = CommandResponse(result_code=CommandResult.SUCCESS)
        refresh_count = 0

        async def refresh_status(*args, **kwargs):
            nonlocal refresh_count
            refresh_count += 1
            client.state.station_activity = 4
            if refresh_count == 2:
                client.state.set_dock_drying_task(
                    DOCK_TASK_DRY_DOCK_BAG,
                    elapsed=60,
                    target=180,
                    fields=("12", "13"),
                )
            return self._docked_status_response()

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send, patch.object(
            client, "_refresh_after_dock_stop", new_callable=AsyncMock
        ) as mock_refresh, patch(
            "narwal_client.client.asyncio.sleep", new_callable=AsyncMock
        ) as mock_sleep:
            mock_status.side_effect = refresh_status
            mock_send.return_value = success
            mock_refresh.return_value = True
            result = await client.stop_dock_task(DOCK_TASK_DRY_DOCK_BAG)

        assert result is success
        mock_send.assert_awaited_once_with(
            TOPIC_CMD_FORCE_END,
            payload=b"\x08\x01",
            timeout=15.0,
        )
        assert mock_status.await_count == 2
        mock_sleep.assert_any_await(1.5)

    @pytest.mark.asyncio
    async def test_stop_dry_station_bag_waits_past_local_assumption(
        self,
    ) -> None:
        """Scoped stop waits for telemetry rather than a local task reservation."""
        client = NarwalClient("127.0.0.1")
        client.state.working_status = WorkingStatus.TASK_COMPLETED
        client.state.station_activity = 4
        client.state.dock_presence = 6
        client.state.dock_field11 = 2
        client.state.assume_dock_task(DOCK_TASK_DRY_DOCK_BAG)
        client.state.update_from_working_status({"12": 0, "13": 18000})
        success = CommandResponse(result_code=CommandResult.SUCCESS)
        refresh_count = 0

        async def refresh_status(*args, **kwargs):
            nonlocal refresh_count
            refresh_count += 1
            client.state.station_activity = 4
            if refresh_count == 2:
                client.state.set_dock_drying_task(
                    DOCK_TASK_DRY_DOCK_BAG,
                    elapsed=2,
                    target=18000,
                    fields=("12", "13"),
                )
            return self._docked_status_response()

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send, patch.object(
            client, "_refresh_after_dock_stop", new_callable=AsyncMock
        ) as mock_refresh, patch(
            "narwal_client.client.asyncio.sleep", new_callable=AsyncMock
        ) as mock_sleep:
            mock_status.side_effect = refresh_status
            mock_send.return_value = success
            mock_refresh.return_value = True
            result = await client.stop_dock_task(DOCK_TASK_DRY_DOCK_BAG)

        assert result is success
        assert mock_status.await_count == 2
        mock_sleep.assert_any_await(1.5)
        mock_send.assert_awaited_once_with(
            TOPIC_CMD_FORCE_END,
            payload=b"\x08\x01",
            timeout=15.0,
        )

    @pytest.mark.asyncio
    async def test_stop_dry_dust_bag_waits_past_local_assumption(
        self,
    ) -> None:
        """Scoped dry dust-bin stop also waits for typed telemetry."""
        client = NarwalClient("127.0.0.1")
        client.state.working_status = WorkingStatus.CHARGED
        client.state.dock_presence = 6
        client.state.dock_field11 = 2
        client.state.assume_dock_task(DOCK_TASK_DRY_DUST_BIN)
        client.state.update_from_working_status({"10": 0, "11": 180})
        success = CommandResponse(result_code=CommandResult.SUCCESS)
        refresh_count = 0

        async def refresh_status(*args, **kwargs):
            nonlocal refresh_count
            refresh_count += 1
            if refresh_count == 2:
                client.state.set_dock_drying_task(
                    DOCK_TASK_DRY_DUST_BIN,
                    elapsed=2,
                    target=180,
                    fields=("10", "11"),
                )
            return self._docked_status_response()

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send, patch.object(
            client, "_refresh_after_dock_stop", new_callable=AsyncMock
        ) as mock_refresh, patch(
            "narwal_client.client.asyncio.sleep", new_callable=AsyncMock
        ) as mock_sleep:
            mock_status.side_effect = refresh_status
            mock_send.return_value = success
            mock_refresh.return_value = True
            result = await client.stop_dock_task(DOCK_TASK_DRY_DUST_BIN)

        assert result is success
        assert mock_status.await_count == 2
        mock_sleep.assert_any_await(1.5)
        mock_send.assert_awaited_once_with(
            TOPIC_CMD_FORCE_END,
            payload=b"\x08\x05",
            timeout=15.0,
        )

    @pytest.mark.asyncio
    async def test_stop_dry_dust_bag_waits_for_dock_activity_snapshot(
        self,
    ) -> None:
        """Dry dust-bin can report dock_activity=6 before timer telemetry."""
        client = NarwalClient("127.0.0.1")
        client.state.working_status = WorkingStatus.CHARGED
        client.state.dock_presence = 6
        client.state.dock_field11 = 2
        client.state.dock_activity = 6
        success = CommandResponse(result_code=CommandResult.SUCCESS)
        refresh_count = 0

        async def refresh_status(*args, **kwargs):
            nonlocal refresh_count
            refresh_count += 1
            client.state.dock_activity = 6
            if refresh_count == 2:
                client.state.set_dock_drying_task(
                    DOCK_TASK_DRY_DUST_BIN,
                    elapsed=2,
                    target=180,
                    fields=("10", "11"),
                )
            return self._docked_status_response()

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send, patch.object(
            client, "_refresh_after_dock_stop", new_callable=AsyncMock
        ) as mock_refresh, patch(
            "narwal_client.client.asyncio.sleep", new_callable=AsyncMock
        ) as mock_sleep:
            mock_status.side_effect = refresh_status
            mock_send.return_value = success
            mock_refresh.return_value = True
            result = await client.stop_dock_task(DOCK_TASK_DRY_DUST_BIN)

        assert result is success
        assert mock_status.await_count == 2
        mock_sleep.assert_any_await(1.5)
        mock_send.assert_awaited_once_with(
            TOPIC_CMD_FORCE_END,
            payload=b"\x08\x05",
            timeout=15.0,
        )

    @pytest.mark.asyncio
    async def test_stop_dry_station_bag_waits_after_idle_drying_snapshot(
        self,
    ) -> None:
        """Station drying can report an idle timer snapshot before typed telemetry."""
        client = NarwalClient("127.0.0.1")
        client.state.working_status = WorkingStatus.TASK_COMPLETED
        client.state.station_activity = 4
        client.state.dock_presence = 6
        client.state.dock_field11 = 2
        client.state.update_from_working_status({"12": 0, "13": 18000})
        success = CommandResponse(result_code=CommandResult.SUCCESS)
        refresh_count = 0

        async def refresh_status(*args, **kwargs):
            nonlocal refresh_count
            refresh_count += 1
            client.state.station_activity = 4
            if refresh_count == 2:
                client.state.set_dock_drying_task(
                    DOCK_TASK_DRY_DOCK_BAG,
                    elapsed=2,
                    target=18000,
                    fields=("12", "13"),
                )
            return self._docked_status_response()

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send, patch.object(
            client, "_refresh_after_dock_stop", new_callable=AsyncMock
        ) as mock_refresh, patch(
            "narwal_client.client.asyncio.sleep", new_callable=AsyncMock
        ) as mock_sleep:
            mock_status.side_effect = refresh_status
            mock_send.return_value = success
            mock_refresh.return_value = True
            result = await client.stop_dock_task(DOCK_TASK_DRY_DOCK_BAG)

        assert result is success
        mock_send.assert_awaited_once_with(
            TOPIC_CMD_FORCE_END,
            payload=b"\x08\x01",
            timeout=15.0,
        )
        assert mock_status.await_count == 2
        mock_sleep.assert_any_await(1.5)

    @pytest.mark.asyncio
    async def test_stop_dry_dust_bag_uses_scoped_force_end_payload(self) -> None:
        """Dry dust-bin drying uses the live-validated scoped force-end payload."""
        client = NarwalClient("127.0.0.1")
        client.state.set_dock_drying_task(
            DOCK_TASK_DRY_DUST_BIN,
            elapsed=60,
            target=180,
            fields=("10", "11"),
        )
        client.state.dock_presence = 1
        success = CommandResponse(result_code=CommandResult.SUCCESS)

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send, patch.object(
            client, "_refresh_after_dock_stop", new_callable=AsyncMock
        ) as mock_refresh, patch(
            "narwal_client.client.asyncio.sleep", new_callable=AsyncMock
        ):
            mock_status.return_value = self._docked_status_response()
            mock_send.return_value = success
            mock_refresh.return_value = True
            result = await client.stop_dock_task(DOCK_TASK_DRY_DUST_BIN)

        assert result is success
        mock_send.assert_awaited_once_with(
            TOPIC_CMD_FORCE_END,
            payload=b"\x08\x05",
            timeout=15.0,
        )
        mock_status.assert_awaited_once_with(full_update=True)
        assert DOCK_TASK_DRY_DUST_BIN not in client.state.dock_drying_tasks

    @pytest.mark.asyncio
    async def test_unscoped_stop_dry_dust_bag_uses_scoped_force_end(self) -> None:
        """Unscoped stop can use the dry dust-bin payload when it is the only task."""
        client = NarwalClient("127.0.0.1")
        client.state.set_dock_drying_task(
            DOCK_TASK_DRY_DUST_BIN,
            elapsed=60,
            target=180,
            fields=("10", "11"),
        )
        client.state.dock_presence = 1
        success = CommandResponse(result_code=CommandResult.SUCCESS)

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send, patch.object(
            client, "_refresh_after_dock_stop", new_callable=AsyncMock
        ) as mock_refresh, patch(
            "narwal_client.client.asyncio.sleep", new_callable=AsyncMock
        ):
            mock_status.return_value = self._docked_status_response()
            mock_send.return_value = success
            mock_refresh.return_value = True
            result = await client.stop_dock_task()

        assert result is success
        mock_send.assert_awaited_once_with(
            TOPIC_CMD_FORCE_END,
            payload=b"\x08\x05",
            timeout=15.0,
        )
        mock_status.assert_awaited_once_with(full_update=True)
        assert client.state.dock_task_timer(DOCK_TASK_DRY_DUST_BIN) is None

    @pytest.mark.asyncio
    async def test_unscoped_stop_rejects_multiple_scoped_tasks(self) -> None:
        """An unscoped request cannot choose arbitrarily between typed tasks."""
        client = NarwalClient("127.0.0.1")
        client.state.set_dock_drying_task(
            DOCK_TASK_DRY_DUST_BIN,
            elapsed=60,
            target=180,
            fields=("10", "11"),
        )
        client.state.set_dock_drying_task(
            DOCK_TASK_DRY_DOCK_BAG,
            elapsed=60,
            target=180,
            fields=("12", "13"),
        )
        client.state.dock_presence = 1

        with patch.object(
            client,
            "get_status",
            new_callable=AsyncMock,
            return_value=self._docked_status_response(),
        ), patch.object(client, "send_command", new_callable=AsyncMock) as command:
            result = await client.stop_dock_task()

        assert result.result_code == CommandResult.NOT_APPLICABLE
        command.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stop_dock_task_keeps_timer_when_refresh_fails(self) -> None:
        client = NarwalClient("127.0.0.1")
        client.state.set_dock_drying_task(
            DOCK_TASK_DRY_DOCK_BAG,
            elapsed=60,
            target=180,
            fields=("12", "13"),
        )
        success = CommandResponse(result_code=CommandResult.SUCCESS)

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send, patch.object(
            client, "_refresh_after_dock_stop", new_callable=AsyncMock
        ) as mock_refresh, patch(
            "narwal_client.client.asyncio.sleep", new_callable=AsyncMock
        ):
            mock_status.return_value = self._docked_status_response()
            mock_send.return_value = success
            mock_refresh.return_value = False
            result = await client.stop_dock_task(DOCK_TASK_DRY_DOCK_BAG)

        assert result is success
        mock_status.assert_awaited_once_with(full_update=True)
        assert client.state.dock_task_timer(DOCK_TASK_DRY_DOCK_BAG) is not None

    @pytest.mark.asyncio
    async def test_stop_dock_task_clears_timer_after_confirmed_idle_refresh(self) -> None:
        """Accepted dock stop clears the stopped timer once fresh dock status is idle."""
        client = NarwalClient("127.0.0.1")
        client.state.set_dock_drying_task(
            DOCK_TASK_DRY_DOCK_BAG,
            elapsed=60,
            target=180,
            fields=("12", "13"),
        )
        success = CommandResponse(result_code=CommandResult.SUCCESS)

        async def refresh_idle() -> bool:
            client.state.update_from_base_status(
                {"3": {"1": int(WorkingStatus.DOCKED), "3": 6}, "11": 2}
            )
            return True

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send, patch.object(
            client, "_refresh_after_dock_stop", new_callable=AsyncMock
        ) as mock_refresh, patch(
            "narwal_client.client.asyncio.sleep", new_callable=AsyncMock
        ):
            mock_status.return_value = self._docked_status_response()
            mock_send.return_value = success
            mock_refresh.side_effect = refresh_idle
            result = await client.stop_dock_task(DOCK_TASK_DRY_DOCK_BAG)

        assert result is success
        mock_status.assert_awaited_once_with(full_update=True)
        assert client.state.dock_task_timer(DOCK_TASK_DRY_DOCK_BAG) is None

    @pytest.mark.asyncio
    async def test_concurrent_direct_dock_stops_serialize_preflight(self) -> None:
        """Only one direct dock stop validates an active task snapshot."""
        client = NarwalClient("127.0.0.1")
        client.state.set_dock_drying_task(
            DOCK_TASK_DRY_DOCK_BAG,
            elapsed=60,
            target=180,
            fields=("12", "13"),
        )
        success = CommandResponse(result_code=CommandResult.SUCCESS)
        command_started = asyncio.Event()
        release_command = asyncio.Event()

        async def slow_send(*args, **kwargs):
            command_started.set()
            await release_command.wait()
            return success

        async def refresh_state():
            client.state.clear_dock_drying_task(DOCK_TASK_DRY_DOCK_BAG)
            return True

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(
            client, "send_command", new_callable=AsyncMock
        ) as mock_send, patch.object(
            client, "_refresh_after_dock_stop", new_callable=AsyncMock
        ) as mock_refresh, patch(
            "narwal_client.client.asyncio.sleep", new_callable=AsyncMock
        ):
            mock_status.return_value = self._docked_status_response()
            mock_send.side_effect = slow_send
            mock_refresh.side_effect = refresh_state
            first = asyncio.create_task(client.stop_dock_task(DOCK_TASK_DRY_DOCK_BAG))
            await command_started.wait()
            second = asyncio.create_task(client.stop_dock_task(DOCK_TASK_DRY_DOCK_BAG))
            await asyncio.sleep(0)
            release_command.set()
            first_result, second_result = await asyncio.gather(first, second)

        assert first_result is success
        assert second_result.result_code == CommandResult.NOT_APPLICABLE
        mock_send.assert_awaited_once_with(
            TOPIC_CMD_FORCE_END,
            payload=b"\x08\x01",
            timeout=15.0,
        )
        assert mock_status.await_count == 2

    @pytest.mark.asyncio
    async def test_refresh_after_dock_stop_rejects_missing_base_status_payload(self) -> None:
        client = NarwalClient("127.0.0.1")

        with patch.object(client, "get_status", new_callable=AsyncMock) as mock_status:
            mock_status.return_value = CommandResponse(
                result_code=CommandResult.NOT_READY,
                data={"1": 1},
            )
            assert not await client._refresh_after_dock_stop()

        mock_status.assert_awaited_once_with(full_update=True)

    @pytest.mark.asyncio
    async def test_refresh_after_dock_stop_rejects_empty_dock_status(self) -> None:
        """An empty dock-status submessage must not clear active dock tasks."""
        client = NarwalClient("127.0.0.1")

        with patch.object(client, "get_status", new_callable=AsyncMock) as mock_status:
            mock_status.return_value = CommandResponse(data={"2": {"3": {}}})
            assert not await client._refresh_after_dock_stop()

        mock_status.assert_awaited_once_with(full_update=True)

    @pytest.mark.asyncio
    async def test_dock_stop_refresh_preserves_recent_cleaning_state(self) -> None:
        """Dock refreshes cannot replace authoritative live cleaning telemetry."""
        client = NarwalClient("127.0.0.1")
        client.state.working_status = WorkingStatus.CLEANING
        client.state.last_active_working_status_time = time.monotonic()

        with patch.object(client, "get_status", new_callable=AsyncMock) as mock_status:
            mock_status.return_value = self._docked_status_response()

            assert await client._refresh_after_dock_stop()

        mock_status.assert_awaited_once_with(full_update=False)

    @pytest.mark.asyncio
    async def test_pre_stop_refresh_preserves_recent_cleaning_state(self) -> None:
        """Scoped stop identification also keeps the live robot task authoritative."""
        client = NarwalClient("127.0.0.1")
        client.state.working_status = WorkingStatus.CLEANING
        client.state.last_active_working_status_time = time.monotonic()

        with patch.object(client, "get_status", new_callable=AsyncMock) as mock_status:
            mock_status.return_value = self._docked_status_response()

            await client._refresh_before_dock_stop(None)

        mock_status.assert_awaited_once_with(full_update=False)

    def test_statusless_base_broadcast_preserves_fresh_clean_metrics(self) -> None:
        """Battery-only telemetry cannot apply a cached terminal status."""
        client = NarwalClient("127.0.0.1")
        client.state.working_status = WorkingStatus.DOCKED
        client.state.last_terminal_working_status_time = 0.0
        client.state.update_from_working_status({"3": 120, "4": 600})

        client._update_from_base_status_broadcast({"2": 75.0})

        assert client.state.battery_level == 75
        assert client.state.is_cleaning
        assert client.state.task_elapsed_time == 120
        assert client.state.task_remaining_time == 600

    def test_external_start_survives_delayed_docked_base_broadcast(self) -> None:
        """Fresh native task metrics outrank delayed dock confirmation."""
        client = NarwalClient("127.0.0.1")
        with patch("narwal_client.models.time.monotonic", return_value=100.0):
            client.state.update_from_base_status(
                {"3": {"1": int(WorkingStatus.DOCKED)}, "11": 2, "47": 3}
            )
        with patch("narwal_client.models.time.monotonic", return_value=101.0):
            client.state.update_from_working_status(
                {"1": 25, "2": 12.5, "3": 120, "4": 600, "6": 4}
            )
            assert not client.state.is_cleaning
        with patch("narwal_client.models.time.monotonic", return_value=102.0):
            client._update_from_base_status_broadcast(
                {
                    "3": {"1": int(WorkingStatus.DOCKED), "10": 1},
                    "11": 2,
                    "47": 3,
                }
            )
            assert client.state.working_status == WorkingStatus.DOCKED
            assert not client.state.is_cleaning
        with patch("narwal_client.models.time.monotonic", return_value=102.5):
            client.state.update_from_working_status({"3": 120})
            assert not client.state.is_cleaning
        with patch("narwal_client.models.time.monotonic", return_value=103.0):
            client.state.update_from_working_status({"3": 121})
            assert client.state.is_cleaning
        with patch("narwal_client.models.time.monotonic", return_value=104.0):
            client._update_from_base_status_broadcast(
                {
                    "3": {"1": int(WorkingStatus.DOCKED), "10": 1},
                    "11": 2,
                    "47": 3,
                }
            )
            assert client.state.is_cleaning
        assert client.state.task_progress_percent == 25
        assert client.state.cleaning_area == 12.5
        assert client.state.task_elapsed_time == 121
        assert client.state.task_remaining_time == 600
        assert client.state.current_room_id == 4

    def test_expired_external_start_candidate_requires_new_progression(self) -> None:
        """An old candidate cannot confirm a later metric packet by itself."""
        client = NarwalClient("127.0.0.1")
        docked = {
            "3": {"1": int(WorkingStatus.DOCKED), "10": 1},
            "11": 2,
            "47": 3,
        }
        with patch("narwal_client.models.time.monotonic", return_value=100.0):
            client.state.update_from_base_status(docked)
        with patch("narwal_client.models.time.monotonic", return_value=101.0):
            client.state.update_from_working_status({"1": 25, "3": 120})
        with patch("narwal_client.models.time.monotonic", return_value=500.0):
            client._update_from_base_status_broadcast(docked)
        with patch("narwal_client.models.time.monotonic", return_value=501.0):
            client.state.update_from_working_status({"1": 26, "3": 121})
            assert not client.state.is_cleaning
        with patch("narwal_client.models.time.monotonic", return_value=502.0):
            client.state.update_from_working_status({"1": 27, "3": 122})
            assert client.state.is_cleaning

    def test_fresh_candidate_outlives_older_terminal_evidence(self) -> None:
        """A fresh candidate can confirm after the older dock evidence expires."""
        client = NarwalClient("127.0.0.1")
        docked = {
            "3": {"1": int(WorkingStatus.DOCKED), "10": 1},
            "11": 2,
            "47": 3,
        }
        with patch("narwal_client.models.time.monotonic", return_value=100.0):
            client.state.update_from_base_status(docked)
        with patch("narwal_client.models.time.monotonic", return_value=114.0):
            client.state.update_from_working_status({"1": 25})
            assert not client.state.is_cleaning
        with patch("narwal_client.models.time.monotonic", return_value=116.0):
            client.state.update_from_working_status({"1": 26})
            assert client.state.is_cleaning

    def test_omitted_nested_room_name_does_not_confirm_external_start(self) -> None:
        """A partial nested room packet must contain real metric progression."""
        client = NarwalClient("127.0.0.1")
        docked = {
            "3": {"1": int(WorkingStatus.DOCKED), "10": 1},
            "11": 2,
            "47": 3,
        }
        with patch("narwal_client.models.time.monotonic", return_value=100.0):
            client.state.update_from_base_status(docked)
        with patch("narwal_client.models.time.monotonic", return_value=101.0):
            client.state.update_from_working_status(
                {"3": 120, "6": {"1": 4, "3": "Kitchen"}}
            )
        with patch("narwal_client.models.time.monotonic", return_value=102.0):
            client._update_from_base_status_broadcast(docked)
        with patch("narwal_client.models.time.monotonic", return_value=103.0):
            client.state.update_from_working_status({"3": 120, "6": {"1": 4}})
            assert not client.state.is_cleaning
        with patch("narwal_client.models.time.monotonic", return_value=104.0):
            client.state.update_from_working_status({"3": 121, "6": {"1": 4}})
            assert client.state.is_cleaning

    def test_regressed_metrics_do_not_confirm_external_start(self) -> None:
        """Out-of-order counters cannot revive a terminal clean session."""
        client = NarwalClient("127.0.0.1")
        with patch("narwal_client.models.time.monotonic", return_value=100.0):
            client.state.update_from_base_status(
                {"3": {"1": int(WorkingStatus.DOCKED)}, "11": 2, "47": 3}
            )
        with patch("narwal_client.models.time.monotonic", return_value=101.0):
            client.state.update_from_working_status({"1": 75, "3": 900})
        with patch("narwal_client.models.time.monotonic", return_value=102.0):
            client.state.update_from_working_status({"1": 74, "3": 899})
            assert not client.state.is_cleaning
        with patch("narwal_client.models.time.monotonic", return_value=103.0):
            client.state.update_from_working_status({"1": 75, "3": 900})
            assert not client.state.is_cleaning
        with patch("narwal_client.models.time.monotonic", return_value=104.0):
            client.state.update_from_working_status({"1": 76, "3": 901})
            assert client.state.is_cleaning

    def test_mixed_direction_metrics_do_not_confirm_external_start(self) -> None:
        """One advancing counter cannot outweigh another counter regressing."""
        client = NarwalClient("127.0.0.1")
        with patch("narwal_client.models.time.monotonic", return_value=100.0):
            client.state.update_from_base_status(
                {"3": {"1": int(WorkingStatus.DOCKED)}, "11": 2, "47": 3}
            )
        with patch("narwal_client.models.time.monotonic", return_value=101.0):
            client.state.update_from_working_status({"1": 75, "3": 900})
        with patch("narwal_client.models.time.monotonic", return_value=102.0):
            client.state.update_from_working_status({"1": 74, "3": 901})

            assert not client.state.is_cleaning

    def test_candidate_tracks_room_transition_before_confirmation(self) -> None:
        """A later room report replaces stale candidate room metadata."""
        client = NarwalClient("127.0.0.1")
        with patch("narwal_client.models.time.monotonic", return_value=100.0):
            client.state.update_from_base_status(
                {"3": {"1": int(WorkingStatus.DOCKED)}, "11": 2, "47": 3}
            )
        with patch("narwal_client.models.time.monotonic", return_value=101.0):
            client.state.update_from_working_status(
                {"3": 120, "6": {"1": 4, "3": "Kitchen"}}
            )
        with patch("narwal_client.models.time.monotonic", return_value=102.0):
            client.state.update_from_working_status({"3": 120, "6": {"1": 5}})
        with patch("narwal_client.models.time.monotonic", return_value=103.0):
            client.state.update_from_working_status({"3": 121})

            assert client.state.is_cleaning
            assert client.state.current_room_id == 5
            assert client.state.current_room_aux_name == ""

    def test_room_only_packet_preserves_external_start_candidate(self) -> None:
        """Metadata-only telemetry cannot erase a fresh metric baseline."""
        client = NarwalClient("127.0.0.1")
        with patch("narwal_client.models.time.monotonic", return_value=100.0):
            client.state.update_from_base_status(
                {"3": {"1": int(WorkingStatus.DOCKED)}, "11": 2, "47": 3}
            )
        with patch("narwal_client.models.time.monotonic", return_value=101.0):
            client.state.update_from_working_status({"3": 120})
        with patch("narwal_client.models.time.monotonic", return_value=102.0):
            client.state.update_from_working_status({"6": {"1": 5, "3": "Lounge"}})
            assert client.state.pending_active_working_status is not None
        with patch("narwal_client.models.time.monotonic", return_value=103.0):
            client.state.update_from_working_status({"3": 121})

            assert client.state.is_cleaning
            assert client.state.current_room_id == 5
            assert client.state.current_room_aux_name == "Lounge"

    def test_confirming_new_room_does_not_restore_prior_room_name(self) -> None:
        """A partial room transition cannot inherit the candidate room name."""
        client = NarwalClient("127.0.0.1")
        with patch("narwal_client.models.time.monotonic", return_value=100.0):
            client.state.update_from_base_status(
                {"3": {"1": int(WorkingStatus.DOCKED)}, "11": 2, "47": 3}
            )
        with patch("narwal_client.models.time.monotonic", return_value=101.0):
            client.state.update_from_working_status(
                {"3": 120, "6": {"1": 4, "3": "Kitchen"}}
            )
        with patch("narwal_client.models.time.monotonic", return_value=102.0):
            client.state.update_from_working_status({"3": 121, "6": {"1": 5}})

            assert client.state.is_cleaning
            assert client.state.current_room_id == 5
            assert client.state.current_room_aux_name == ""

    def test_explicit_terminal_status_discards_external_start_candidate(self) -> None:
        """A candidate from before completion cannot confirm later telemetry."""
        client = NarwalClient("127.0.0.1")
        with patch("narwal_client.models.time.monotonic", return_value=100.0):
            client.state.update_from_base_status(
                {"3": {"1": int(WorkingStatus.DOCKED)}, "11": 2, "47": 3}
            )
        with patch("narwal_client.models.time.monotonic", return_value=101.0):
            client.state.update_from_working_status({"1": 25, "3": 120})
        with patch("narwal_client.models.time.monotonic", return_value=102.0):
            client.state.update_from_base_status(
                {"3": {"1": int(WorkingStatus.TASK_COMPLETED)}}
            )
            assert client.state.pending_active_working_status is None
        with patch("narwal_client.models.time.monotonic", return_value=103.0):
            client.state.update_from_base_status(
                {"3": {"1": int(WorkingStatus.DOCKED)}, "11": 2, "47": 3}
            )
        with patch("narwal_client.models.time.monotonic", return_value=104.0):
            client.state.update_from_working_status({"1": 26, "3": 121})
            assert not client.state.is_cleaning

    def test_repeated_final_metrics_cannot_mask_confirmed_docking(self) -> None:
        """Unchanged final counters expire so repeated dock evidence can win."""
        client = NarwalClient("127.0.0.1")
        with patch("narwal_client.models.time.monotonic", return_value=100.0):
            client.state.update_from_base_status(
                {"3": {"1": int(WorkingStatus.CLEANING)}, "11": 1, "47": 2}
            )
            client.state.update_from_working_status({"1": 75, "3": 900, "6": 4})

        docked = {
            "3": {"1": int(WorkingStatus.DOCKED), "10": 1},
            "11": 2,
            "47": 3,
        }
        with patch("narwal_client.models.time.monotonic", return_value=101.0):
            client._update_from_base_status_broadcast(docked)
        assert client.state.is_cleaning

        with patch("narwal_client.models.time.monotonic", return_value=105.0):
            client.state.update_from_working_status({"1": 75, "3": 900, "6": 4})
        with patch("narwal_client.models.time.monotonic", return_value=116.0):
            client.state.update_from_working_status({"1": 75, "3": 900, "6": 4})
            client._update_from_base_status_broadcast(docked)

        assert client.state.working_status == WorkingStatus.DOCKED
        assert client.state.is_docked
        assert not client.state.is_cleaning

    def test_statusless_pause_broadcast_applies_during_fresh_clean(self) -> None:
        """A pause overlay remains authoritative without a coarse status enum."""
        client = NarwalClient("127.0.0.1")
        client.state.working_status = WorkingStatus.DOCKED
        client.state.last_terminal_working_status_time = 0.0
        client.state.update_from_working_status({"3": 120, "4": 600})

        client._update_from_base_status_broadcast({"3": {"2": 1}})

        assert client.state.is_paused
        assert client.state.task_elapsed_time == 120
        assert client.state.task_remaining_time == 600

    def test_statusless_packet_does_not_reuse_standby_dock_evidence(self) -> None:
        """Cached dock fields cannot make a status-less packet terminal."""
        client = NarwalClient("127.0.0.1")
        client.state.update_from_base_status(
            {"3": {"1": int(WorkingStatus.STANDBY), "3": 6}}
        )
        client.state.last_terminal_working_status_time = 0.0
        client.state.update_from_working_status({"3": 120, "4": 600})

        client._update_from_base_status_broadcast({"3": {"7": 0}, "2": 80.0})

        assert client.state.is_cleaning
        assert client.state.task_elapsed_time == 120
        assert client.state.task_remaining_time == 600
        assert not client.state.has_recent_terminal_working_status

    @pytest.mark.asyncio
    async def test_stop_dock_task_rejects_ambiguous_generic_stop(self) -> None:
        client = NarwalClient("127.0.0.1")
        client.state.set_dock_drying_task(
            DOCK_TASK_DRY_MOP,
            elapsed=60,
            target=180,
            fields=("8", "9"),
        )
        client.state.set_dock_drying_task(
            DOCK_TASK_DRY_DOCK_BAG,
            elapsed=60,
            target=180,
            fields=("12", "13"),
        )

        with patch.object(
            client, "get_status", new_callable=AsyncMock
        ) as mock_status, patch.object(client, "stop", new_callable=AsyncMock) as mock_stop:
            mock_status.return_value = self._docked_status_response()
            result = await client.stop_dock_task(DOCK_TASK_DRY_MOP)

        assert result.result_code == CommandResult.NOT_APPLICABLE
        mock_status.assert_awaited_once_with(full_update=True)
        mock_stop.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stop_dock_task_rejects_unmapped_activity(self) -> None:
        """Unknown active dock work is not safe to stop with the generic command."""
        client = NarwalClient("127.0.0.1")
        client.state.station_activity = 99

        with patch.object(client, "stop", new_callable=AsyncMock) as mock_stop:
            result = await client.stop_dock_task()

        assert result.result_code == CommandResult.NOT_APPLICABLE
        mock_stop.assert_not_awaited()


class TestBroadcastDumpLogging:
    """The DUMP debug line is a parsing contract for tools/, not decoration.

    tools/narwal_capture.py and tools/coverage_probe.py recover the decoded
    payload by scraping "DUMP <topic>: <payload>" out of `ha core logs`. The
    line was dropped once already, which silently reduced both tools to
    producing nothing. These tests fail loudly if that happens again.
    """

    def _client_with_broadcast(self) -> tuple[NarwalClient, bytes]:
        client = NarwalClient("10.0.0.1")
        client.device_id = "abc123"
        frame = build_frame("/QoEsI5qYXO/abc123/status/working_status", b"\x08\x04")
        return client, frame

    @pytest.mark.asyncio
    async def test_broadcast_logs_dump_line_with_decoded_payload(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A decoded broadcast emits DUMP <short_topic>: <payload> at DEBUG."""
        client, frame = self._client_with_broadcast()

        with caplog.at_level(logging.DEBUG, logger="narwal_client.client"):
            with patch.object(
                client, "_decode_protobuf", return_value={"1": 4}
            ):
                await client._handle_message(frame)

        dumps = [r.getMessage() for r in caplog.records if "DUMP " in r.getMessage()]
        assert dumps, "no DUMP line emitted - tools/ capture scripts rely on it"
        assert dumps[0].startswith("DUMP status/working_status: ")
        assert "{'1': 4}" in dumps[0]

    @pytest.mark.asyncio
    async def test_dump_line_is_debug_only(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """DUMP must not reach INFO - it fires on every broadcast."""
        client, frame = self._client_with_broadcast()

        with caplog.at_level(logging.INFO, logger="narwal_client.client"):
            with patch.object(client, "_decode_protobuf", return_value={"1": 4}):
                await client._handle_message(frame)

        assert not [r for r in caplog.records if "DUMP " in r.getMessage()]


class TestDockedRobotIsLeftAlone:
    """A docked robot's silence is its duty cycle, not sleep (#90).

    Measured on a Flow (AX12, v01.08.03.07) with wake bursts suppressed: the
    robot broadcasts for 30.0s or 45.5s, goes quiet for 60-124s, and resumes
    unprompted. BROADCAST_STALE_TIMEOUT is 15s, so before this the keepalive
    read every one of those gaps as sleep and fired a full wake burst -- about
    1,900 a day at an idle docked robot.
    """

    # A frozen clock. time.monotonic() is system uptime on Linux, so anything
    # derived from the real one is a coin flip on a freshly booted CI runner --
    # which is exactly how the first version of these tests failed there.
    FAKE_NOW = 1_000_000.0

    def _quiet_client(self, *, docked: bool) -> NarwalClient:
        """A connected client whose broadcasts went stale long ago."""
        client = NarwalClient("10.0.0.1")
        client.device_id = "abc123"
        client._ws = AsyncMock()
        client._connected.set()
        client._robot_awake = True
        # Well past BROADCAST_STALE_TIMEOUT, so the stale check trips.
        client._last_broadcast_time = self.FAKE_NOW - 600
        client.state.working_status = (
            WorkingStatus.DOCKED if docked else WorkingStatus.CLEANING
        )
        if not docked:
            client.state.last_active_working_status_time = 0.0
        return client

    async def _run_ticks(self, client: NarwalClient, ticks: int = 2) -> int:
        """Drive _keepalive_loop through exactly `ticks` iterations.

        The loop paces itself with asyncio.sleep(KEEPALIVE_INTERVAL); replacing
        that with a counter makes the test independent of wall-clock timing.
        An earlier version slept for a fixed 0.15s and hoped enough ticks fit,
        which flaked on CI -- and worse, made the "no wake burst" assertion
        pass for free whenever the loop had not run at all.
        """
        seen = {"n": 0}
        real_sleep = asyncio.sleep

        async def fake_sleep(delay: float, *args, **kwargs):
            seen["n"] += 1
            if seen["n"] > ticks:
                client._connected.clear()  # loop breaks after this sleep
            await real_sleep(0)

        with patch("narwal_client.client.time.monotonic", return_value=self.FAKE_NOW):
            with patch("narwal_client.client.asyncio.sleep", fake_sleep):
                await client._keepalive_loop()
        return seen["n"]

    @pytest.mark.asyncio
    async def test_docked_and_quiet_sends_no_wake_burst(self) -> None:
        """The bug: this fired a burst every ~46s, forever."""
        client = self._quiet_client(docked=True)
        assert client.state.is_docked

        with patch.object(client, "_send_wake_burst", AsyncMock()) as burst:
            with patch.object(
                client, "_renew_topic_subscription", AsyncMock(return_value=True)
            ) as renew:
                ticks = await self._run_ticks(client)

        assert ticks >= 2, "loop did not run — the assertion below would be vacuous"
        renew.assert_awaited()  # positive proof the loop reached the branch
        burst.assert_not_awaited()
        assert not client._robot_awake

    @pytest.mark.asyncio
    async def test_undocked_and_quiet_still_wakes(self) -> None:
        """Off the dock, silence is still a real dropout — don't over-fix."""
        client = self._quiet_client(docked=False)
        assert not client.state.is_docked

        with patch.object(client, "_send_wake_burst", AsyncMock()) as burst:
            with patch.object(
                client, "_renew_topic_subscription", AsyncMock(return_value=True)
            ):
                await self._run_ticks(client)

        assert burst.await_count >= 1

    @pytest.mark.asyncio
    async def test_subscription_is_renewed_while_docked_and_quiet(self) -> None:
        """#73 guard: renewal used to ride on the wake burst.

        With bursts no longer fired at a docked robot, renewal has to stand on
        its own — otherwise active_robot_publish expires after 600s, broadcasts
        stop for real, and the entity freezes at `docked`.
        """
        client = self._quiet_client(docked=True)

        with patch.object(client, "_send_wake_burst", AsyncMock()) as burst:
            with patch.object(
                client, "_renew_topic_subscription", AsyncMock(return_value=True)
            ) as renew:
                await self._run_ticks(client)

        burst.assert_not_awaited()
        renew.assert_awaited()

    @pytest.mark.asyncio
    async def test_renewal_does_not_depend_on_being_awake(self) -> None:
        """Renewal is scheduled, not conditional on broadcasts flowing."""
        client = self._quiet_client(docked=True)
        client._robot_awake = False

        with patch.object(
            client, "_renew_topic_subscription", AsyncMock(return_value=True)
        ) as renew:
            with patch.object(client, "_send_wake_burst", AsyncMock()):
                await self._run_ticks(client)

        renew.assert_awaited()

    @pytest.mark.asyncio
    async def test_first_subscription_fires_on_a_freshly_booted_host(self) -> None:
        """Renewal must not depend on time.monotonic() being a large number.

        monotonic() is only guaranteed monotonic; on Linux it is system uptime.
        `last_resub_time` used to start at 0.0 and compare
        `monotonic() - 0.0 > _TOPIC_RESUB_INTERVAL`, so on a host up for less
        than 8 minutes the first subscription was silently deferred. Caught by
        CI, where the runner's uptime is genuinely that low.
        """
        client = self._quiet_client(docked=True)
        client._last_broadcast_time = 20.0  # stale against the clock below

        seen = {"n": 0}
        real_sleep = asyncio.sleep

        async def fake_sleep(delay: float, *args, **kwargs):
            seen["n"] += 1
            if seen["n"] > 2:
                client._connected.clear()
            await real_sleep(0)

        with patch.object(
            client, "_renew_topic_subscription", AsyncMock(return_value=True)
        ) as renew:
            with patch.object(client, "_send_wake_burst", AsyncMock()):
                # 90s of uptime — far below _TOPIC_RESUB_INTERVAL (480).
                with patch("narwal_client.client.time.monotonic", return_value=90.0):
                    with patch("narwal_client.client.asyncio.sleep", fake_sleep):
                        await client._keepalive_loop()

        renew.assert_awaited()
