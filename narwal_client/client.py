"""WebSocket client for Narwal robot vacuum."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import websockets
import websockets.exceptions

from .const import (
    ACTIVE_CLEANING_STATUSES,
    BROADCAST_STALE_TIMEOUT,
    COMMAND_RESPONSE_TIMEOUT,
    DEFAULT_PORT,
    DEFAULT_TOPIC_PREFIX,
    HEARTBEAT_INTERVAL,
    KEEPALIVE_INTERVAL,
    KNOWN_PRODUCT_KEYS,
    RECONNECT_BACKOFF_FACTOR,
    RECONNECT_INITIAL_DELAY,
    RECONNECT_MAX_DELAY,
    TOPIC_CMD_ACTIVE_ROBOT,
    TOPIC_CMD_AMBIENT_LIGHT_CTRL,
    TOPIC_CMD_APP_HEARTBEAT,
    TOPIC_CMD_CANCEL,
    TOPIC_CMD_CLEAN_TASK,
    TOPIC_CMD_DRY_DUST_BAG,
    TOPIC_CMD_DRY_MOP,
    TOPIC_CMD_DRY_STATION_BAG,
    TOPIC_CMD_DUST_GATHERING,
    TOPIC_CMD_EASY_CLEAN,
    TOPIC_CMD_FORCE_END,
    TOPIC_CMD_GET_ALL_MAPS,
    TOPIC_CMD_GET_BASE_STATUS,
    TOPIC_CMD_GET_CONSUMABLE_INFO,
    TOPIC_CMD_GET_CURRENT_TASK,
    TOPIC_CMD_GET_DEBUG_IMAGE,
    TOPIC_CMD_GET_DEVICE_INFO,
    TOPIC_CMD_GET_FEATURE_LIST,
    TOPIC_CMD_GET_MAP,
    TOPIC_CMD_NOTIFY_APP_EVENT,
    TOPIC_CMD_PAUSE,
    TOPIC_CMD_RECALL,
    TOPIC_CMD_RESUME,
    TOPIC_CMD_SET_FAN_LEVEL,
    TOPIC_CMD_SET_LED,
    TOPIC_CMD_SET_MOP_HUMIDITY,
    TOPIC_CMD_TAKE_PICTURE,
    TOPIC_CMD_WASH_MOP,
    TOPIC_CMD_WASH_MOP_BY_ROBOT_STATUS,
    TOPIC_CMD_YELL,
    WAKE_TIMEOUT,
    AmbientLightCtrlType,
    CleaningRoute,
    CommandResult,
    FanLevel,
    MopHumidity,
    MopStrengthLevel,
    WorkingStatus,
    WorkMode,
    fan_level_for_live_command,
)
from .models import (
    DOCK_TASK_DRY_DOCK_BAG,
    DOCK_TASK_DRY_DUST_BIN,
    DOCK_TASK_DRY_MOP,
    DOCK_TASK_EMPTY_DUSTBIN,
    DOCK_TASK_WASH_MOP,
    CommandResponse,
    DeviceInfo,
    MapData,
    MapDisplayData,
    NarwalState,
)
from .protocol import (
    PROTOBUF_FIELD5_TAG,
    NarwalMessage,
    ProtocolError,
    build_frame,
    parse_frame,
)

_LOGGER = logging.getLogger(__name__)

_STALE_DOCK_BASE_STATUSES = {
    WorkingStatus.UNKNOWN,
    WorkingStatus.STANDBY,
    WorkingStatus.DOCKED,
    WorkingStatus.CHARGED,
    WorkingStatus.DOCKED_V2,
}

_DOCK_TASK_REFRESH_DELAY = 6.0
_DOCK_TASK_IDENTIFY_ATTEMPTS = 4
_DOCK_TASK_IDENTIFY_DELAY = 1.5
_DOCK_TASK_FORCE_END_PAYLOADS = {
    # Live-validated on Flow 2: the app's ForceEndTask.Request uses field 1
    # with ParallelTaskType.DRY_STATION_BAG to stop dock-bag drying.
    DOCK_TASK_DRY_DOCK_BAG: b"\x08\x01",
    # Live-validated on Flow 2: the app's ForceEndTask.Request uses field 1
    # with ParallelTaskType.DRY_BAG to stop robot dust-bin drying.
    DOCK_TASK_DRY_DUST_BIN: b"\x08\x05",
}


def _accepted_response(response: CommandResponse) -> bool:
    """Return true for response codes that mean the robot accepted a command."""
    return response.accepted


def _clean_session_context(state: NarwalState) -> bool:
    """Return true while robot-side work or its accepted-command guard is active."""
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


def _robot_work_blocks_generic_dock_stop(state: NarwalState) -> bool:
    """Return true when generic force-end could target robot work, not dock work."""
    return (
        state.is_cleaning
        or state.has_assumed_robot_clean
        or state.working_status in ACTIVE_CLEANING_STATUSES
        or state.has_recent_active_working_status
        or state.has_paused_clean_task_context
        or state.is_returning
    )


def _robot_start_blocked(state: NarwalState) -> bool:
    """Return true unless fresh state permits dispatching a robot start."""
    return (
        state.has_error
        or state.working_status in (WorkingStatus.UNKNOWN, WorkingStatus.ERROR)
        or not state.is_docked
        or _clean_session_context(state)
        or state.blocks_robot_start_for_dock_task
    )


def _can_force_end_scoped_dock_task(state: NarwalState, task: str | None) -> bool:
    """Return true when a typed force-end can safely target a known task."""
    return task in _DOCK_TASK_FORCE_END_PAYLOADS and task in state.telemetry_dock_task_keys


def _has_generic_dock_stop_proof(state: NarwalState) -> bool:
    """Return true when generic force-end can only target dock-side work."""
    return state.is_docked and state.has_dock_presence_signal


@dataclass
class RoomCleanSettings:
    """Clean parameters for a single room in a custom clean task."""

    work_mode: WorkMode = WorkMode.VACUUM_AND_MOP
    fan: FanLevel = FanLevel.NORMAL
    water: MopHumidity = MopHumidity.NORMAL
    mop_strength: MopStrengthLevel = MopStrengthLevel.NORMAL
    passes: int = 1
    route: CleaningRoute | None = None


def _base_status_working_status(decoded: dict[str, Any] | object) -> WorkingStatus | None:
    """Extract robot_base_status field 3.1."""
    if not isinstance(decoded, dict):
        return None
    field3 = decoded.get("3")
    if isinstance(field3, list):
        field3 = field3[0] if field3 else None
    if not isinstance(field3, dict) or "1" not in field3:
        return None
    try:
        return WorkingStatus(int(field3["1"]))
    except (TypeError, ValueError):
        return None


def _base_status_dock_evidence(decoded: dict[str, Any] | object) -> bool | None:
    """Return explicit current dock evidence, or None when the payload is silent."""
    if not isinstance(decoded, dict):
        return None
    field3 = decoded.get("3")
    if isinstance(field3, list):
        field3 = field3[0] if field3 else None
    if not isinstance(field3, dict):
        field3 = {}
    def optional_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    presence = optional_int(field3.get("3"))
    sub_state = optional_int(field3.get("10"))
    field11 = optional_int(decoded.get("11"))
    field47 = optional_int(decoded.get("47"))
    reports_docked = (
        presence in (1, 6)
        or sub_state == 1
        or (field11 is not None and field11 >= 2)
        or field47 in (1, 3)
    )
    reports_off_dock = (
        presence == 2
        or sub_state == 2
        or (field11 == 1 and field47 == 2)
    )
    if reports_off_dock:
        return False
    if reports_docked:
        return True
    return None


def _base_status_payload(response: CommandResponse) -> dict[str, Any] | None:
    """Return the decoded robot_base_status payload from a command response."""
    if not isinstance(response.data, dict):
        return None
    status_data = response.data.get("2")
    if isinstance(status_data, dict) and status_data:
        return status_data
    return None


def _has_dock_status_payload(response: CommandResponse) -> bool:
    """Return true when a response carries the dock/base status submessage."""
    if not response.accepted:
        return False
    status_data = _base_status_payload(response)
    if status_data is None:
        return False
    field3 = status_data.get("3")
    if isinstance(field3, list):
        field3 = field3[0] if field3 else None
    if not isinstance(field3, dict):
        return False
    return bool({"1", "2", "3", "7", "10", "12", "18"}.intersection(field3))


def _dock_status_confirms_idle(state: NarwalState) -> bool:
    """Return true when fresh dock status reports no active station task."""
    return (
        state.station_activity <= 0
        and state.dock_activity in (0, 2, 6)
        and state.has_dock_presence_signal
        and state.working_status
        in (
            WorkingStatus.UNKNOWN,
            WorkingStatus.STANDBY,
            WorkingStatus.DOCKED,
            WorkingStatus.CHARGED,
            WorkingStatus.DOCKED_V2,
            WorkingStatus.TASK_COMPLETED,
        )
    )


class NarwalConnectionError(Exception):
    """Raised when connection to the vacuum fails."""


class NarwalCommandError(Exception):
    """Raised when a command fails or times out."""


class NarwalClient:
    """Async WebSocket client for communicating with a Narwal vacuum.

    Usage:
        client = NarwalClient(host="192.168.1.100", device_id="your_device_id")
        await client.connect()
        client.on_state_update = my_callback
        await client.start_listening()
        # ...later...
        await client.disconnect()
    """

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        device_id: str = "",
        topic_prefix: str | None = None,
        supports_broadcasts: bool = True,
    ) -> None:
        self.host = host
        self.port = port
        self.device_id = device_id
        self.url = f"ws://{host}:{port}"
        self.topic_prefix = topic_prefix or DEFAULT_TOPIC_PREFIX
        self.supports_broadcasts = supports_broadcasts
        self.state = NarwalState()
        self.on_state_update: Callable[[NarwalState], None] | None = None
        self.on_message: Callable[[NarwalMessage], None] | None = None

        self._ws: Any = None
        self._listen_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._connected = asyncio.Event()
        self._should_reconnect = True
        self._listener_active = False  # True when start_listening() is running recv loop
        self._robot_awake = False  # True after a broadcast or addressed response
        self._last_broadcast_time: float = 0.0  # monotonic time of last broadcast
        self._last_response_time: float = 0.0  # monotonic time of last addressed response
        self._last_display_map_time: float = 0.0  # monotonic time of last display_map
        # Queue for field5 command responses
        self._response_queue: asyncio.Queue[NarwalMessage] = asyncio.Queue()
        # Lock to prevent concurrent send_command calls from racing on the queue
        self._command_lock = asyncio.Lock()
        # Lock high-level action preflight through accepted-command reservation.
        # The lower command lock only serializes wire traffic; this prevents
        # direct robot and dock commands from validating the same idle snapshot.
        self._action_lock = asyncio.Lock()
        self._dock_task_lock = self._action_lock
        self._robot_start_lock = self._action_lock

    def _full_topic(self, short_topic: str) -> str:
        """Build the full topic path."""
        return f"{self.topic_prefix}/{self.device_id}/{short_topic}"

    def _update_topic_prefix_from_topic(self, topic: str, source: str) -> None:
        """Preserve the product-key prefix from a robot response topic."""
        parts = topic.split("/")
        if len(parts) >= 2 and parts[0] == "" and parts[1]:
            self.topic_prefix = f"/{parts[1]}"
            _LOGGER.info("Topic prefix from %s: %s", source, self.topic_prefix)

    @property
    def connected(self) -> bool:
        """Return True if the WebSocket is currently connected."""
        return self._ws is not None and self._connected.is_set()

    @property
    def robot_awake(self) -> bool:
        """Return True if the robot has confirmed local reachability."""
        return self._robot_awake

    def _mark_response_received(self) -> None:
        """Record an addressed response as reachability for non-broadcast models."""
        self._last_response_time = time.monotonic()
        if not self.supports_broadcasts:
            self._robot_awake = True

    @property
    def last_broadcast_age(self) -> float:
        """Seconds since last broadcast (0.0 if none received yet)."""
        if self._last_broadcast_time <= 0:
            return 0.0
        return time.monotonic() - self._last_broadcast_time

    @property
    def last_display_map_age(self) -> float:
        """Seconds since last display_map broadcast (999.0 if none received)."""
        if self._last_display_map_time <= 0:
            return 999.0
        return time.monotonic() - self._last_display_map_time

    def _update_from_working_status_broadcast(self, decoded: dict[str, Any]) -> None:
        """Apply live task metrics from the working_status broadcast."""
        self.state.update_from_working_status(decoded)

    def _update_from_base_status_broadcast(self, decoded: dict[str, Any]) -> None:
        """Apply a base-status broadcast without clobbering fresh task metrics."""
        status = _base_status_working_status(decoded)
        if (
            self.state.has_recent_active_working_status
            and status in _STALE_DOCK_BASE_STATUSES
        ):
            terminal_dock_status = status in {
                WorkingStatus.DOCKED,
                WorkingStatus.CHARGED,
                WorkingStatus.DOCKED_V2,
            }
            dock_evidence = _base_status_dock_evidence(decoded)
            if dock_evidence is True or (
                dock_evidence is None
                and terminal_dock_status
                and not self.state.has_explicit_off_dock_signal
            ):
                self.state.terminal_working_status_generation += 1
            self.state.update_battery_from_base_status(decoded)
            _LOGGER.debug(
                "Ignoring stale base_status=%s while working_status task metrics are fresh",
                status.name if status else "unknown",
            )
            return

        self.state.update_from_base_status(decoded)

    def _update_from_display_map_broadcast(self, decoded: dict[str, Any]) -> None:
        """Apply a display-map broadcast and mark the trajectory as fresh."""
        self.state.map_display_data = MapDisplayData.from_broadcast(decoded)
        self._last_display_map_time = time.monotonic()
        _LOGGER.debug(
            "display_map received: robot=(%.2f, %.2f) ts=%d",
            self.state.map_display_data.robot_x,
            self.state.map_display_data.robot_y,
            self.state.map_display_data.timestamp,
        )

    async def connect(self) -> None:
        """Establish WebSocket connection to the vacuum.

        Raises:
            NarwalConnectionError: If connection cannot be established.
        """
        try:
            self._ws = await websockets.connect(
                self.url, ping_interval=30, ping_timeout=10
            )
            self._connected.set()
            _LOGGER.info("Connected to Narwal vacuum at %s", self.url)
        except (OSError, websockets.exceptions.WebSocketException) as e:
            raise NarwalConnectionError(
                f"Failed to connect to {self.url}: {e}"
            ) from e

    async def discover_device_id(self, timeout: float = 15.0) -> str:
        """Discover the device_id by waking the robot and reading its response.

        The robot sleeps when idle and won't broadcast until woken. This method
        sends a get_device_info command (with empty device_id) as a wake signal.
        The robot's local WebSocket server processes commands regardless of the
        device_id in the topic. The response contains the real device_id.

        Falls back to extracting device_id from broadcast topics if the
        command response doesn't contain it.

        Args:
            timeout: Seconds to wait for discovery.

        Returns:
            The device_id string.

        Raises:
            NarwalConnectionError: If not connected.
            NarwalCommandError: If discovery fails within timeout.
        """
        if not self.connected:
            raise NarwalConnectionError("Not connected to vacuum")

        # Build wake frames using all known product key prefixes.
        # The robot only responds to commands with its correct product key
        # in the topic. Since we don't know the model yet, try all known
        # keys until one provokes a response.
        cmd = TOPIC_CMD_GET_DEVICE_INFO
        wake_frames = [
            build_frame(self._full_topic(cmd), b""),  # current prefix (default or user-set)
            build_frame(f"//{cmd}", b""),  # bare topic, no prefix
        ]
        # Add frames for all known product keys (skip default, already included)
        for key in KNOWN_PRODUCT_KEYS:
            if key != self.topic_prefix.lstrip("/"):
                wake_frames.append(
                    build_frame(f"/{key}/{self.device_id}/{cmd}", b"")
                )
        # Send first batch (default + bare + first few known keys)
        batch_size = min(5, len(wake_frames))
        for frame in wake_frames[:batch_size]:
            try:
                await self._ws.send(frame)
            except Exception as e:
                _LOGGER.warning("Failed to send wake command: %s", e)
        _LOGGER.debug(
            "Sent discovery wake commands (%d prefixes, device_id='%s')",
            batch_size, self.device_id,
        )

        wake_index = 0  # cycle through wake frames on retry
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                data = await asyncio.wait_for(
                    self._ws.recv(), timeout=min(remaining, 2.0)
                )
            except TimeoutError:
                # Re-send wake commands, cycling through prefixes
                try:
                    await self._ws.send(wake_frames[wake_index % len(wake_frames)])
                    wake_index += 1
                    _LOGGER.debug("Re-sent wake-up command (variant %d)", wake_index)
                except Exception:
                    pass
                continue

            if not isinstance(data, bytes) or len(data) < 4:
                continue

            try:
                msg = parse_frame(data)
            except ProtocolError:
                continue

            # Check field5 response — get_device_info returns device_id in field 2
            if msg.field_tag == PROTOBUF_FIELD5_TAG and msg.payload:
                try:
                    decoded = self._decode_protobuf(msg.payload)
                    raw_product_key = decoded.get("1", b"")
                    if isinstance(raw_product_key, bytes):
                        product_key = raw_product_key.decode(
                            "utf-8", errors="replace"
                        ).strip()
                    elif isinstance(raw_product_key, str):
                        product_key = raw_product_key.strip()
                    else:
                        product_key = ""
                    raw_id = decoded.get("2", b"")
                    if isinstance(raw_id, bytes):
                        raw_id = raw_id.decode("utf-8", errors="replace").strip()
                    else:
                        raw_id = str(raw_id).strip()
                    if raw_id:
                        if product_key:
                            self.topic_prefix = f"/{product_key}"
                            _LOGGER.info(
                                "Topic prefix from response payload: %s",
                                self.topic_prefix,
                            )
                        else:
                            self._update_topic_prefix_from_topic(msg.topic, "response")
                        self.device_id = raw_id
                        _LOGGER.info("Discovered device_id from response: %s", self.device_id)
                        return self.device_id
                except Exception:
                    _LOGGER.debug("Failed to decode response payload")

            # Fallback: broadcast messages (field4/0x22) have device_id in topic
            if msg.field_tag != PROTOBUF_FIELD5_TAG and msg.topic:
                parts = msg.topic.split("/")
                # Topic format: /{product_key}/{device_id}/{category}/{type}
                if len(parts) >= 4 and parts[2]:
                    # Extract product_key from topic to set correct prefix
                    self._update_topic_prefix_from_topic(msg.topic, "broadcast")
                    self.device_id = parts[2]
                    _LOGGER.info("Discovered device_id from broadcast: %s", self.device_id)
                    return self.device_id

        raise NarwalCommandError(
            f"No response or broadcast within {timeout}s — check vacuum IP and power"
        )

    async def drain_ws_buffer(self) -> None:
        """Drain any pending messages from the WebSocket receive buffer.

        Called between discover_device_id() and send_command() to clear
        stale field5 responses left by wake probe commands. Without this,
        _wait_for_field5_response may consume a stale response instead of
        the real one, which can have unexpected data or error codes.
        """
        if not self.connected:
            return
        drained = 0
        while True:
            try:
                await asyncio.wait_for(self._ws.recv(), timeout=0.05)
                drained += 1
            except TimeoutError:
                break
            except Exception:
                break
        if drained:
            _LOGGER.debug("Drained %d stale messages from WebSocket buffer", drained)

    async def disconnect(self) -> None:
        """Disconnect from the vacuum and stop all tasks."""
        self._should_reconnect = False
        self._listener_active = False
        self._robot_awake = False
        self._connected.clear()

        for task in (self._heartbeat_task, self._keepalive_task, self._listen_task):
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        if self._ws:
            await self._ws.close()
            self._ws = None

        _LOGGER.info("Disconnected from Narwal vacuum")

    async def start_listening(self) -> None:
        """Start the persistent message listener with auto-reconnect.

        This method runs indefinitely until disconnect() is called.
        """
        self._should_reconnect = True
        retry_delay = RECONNECT_INITIAL_DELAY

        while self._should_reconnect:
            try:
                if not self.connected:
                    await self.connect()
                    # Immediate wake burst on (re)connect — the fresh TCP
                    # connection may trigger the robot's deep-sleep wake
                    # interrupt, but only if we send commands before it
                    # expires.  Don't wait for the keepalive loop's first
                    # tick (15s delay would be too late).
                    if self.supports_broadcasts:
                        await self._send_wake_burst()

                retry_delay = RECONNECT_INITIAL_DELAY  # reset on success
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
                self._keepalive_task = asyncio.create_task(self._keepalive_loop())
                self._listener_active = True

                async for raw_message in self._ws:
                    if isinstance(raw_message, bytes):
                        await self._handle_message(raw_message)

            except NarwalConnectionError as e:
                _LOGGER.warning("Connection failed: %s", e)
            except websockets.exceptions.ConnectionClosed as e:
                _LOGGER.warning("Connection closed: %s", e)
            except asyncio.CancelledError:
                _LOGGER.debug("Listener cancelled")
                return
            except Exception:
                _LOGGER.exception("Unexpected error in listener")
            finally:
                self._listener_active = False
                self._robot_awake = False
                self._connected.clear()
                for task in (self._heartbeat_task, self._keepalive_task):
                    if task and not task.done():
                        task.cancel()

            if not self._should_reconnect:
                break

            # Exponential backoff with jitter
            jitter = random.uniform(0, 1)
            wait = retry_delay + jitter
            _LOGGER.info("Reconnecting in %.1fs...", wait)
            await asyncio.sleep(wait)
            retry_delay = min(
                retry_delay * RECONNECT_BACKOFF_FACTOR, RECONNECT_MAX_DELAY
            )

    async def _handle_message(self, data: bytes) -> None:
        """Parse a raw frame and update state or route response."""
        if len(data) < 4:
            return

        try:
            msg = parse_frame(data)
        except ProtocolError as e:
            _LOGGER.debug("Failed to parse frame: %s", e)
            return

        # Field5 (0x2a) messages are command responses
        if msg.field_tag == PROTOBUF_FIELD5_TAG:
            self._mark_response_received()
            _LOGGER.debug("Field5 response routed to queue: %s", msg.short_topic)
            await self._response_queue.put(msg)
            return

        # Any broadcast means the robot is awake
        self._last_broadcast_time = time.monotonic()
        if not self._robot_awake:
            self._robot_awake = True
            _LOGGER.debug("Robot is awake (received broadcast)")

        if self.on_message:
            self.on_message(msg)

        # Decode protobuf and update state based on topic
        short_topic = msg.short_topic
        _LOGGER.debug("Broadcast topic: %s (tag=0x%02x)", short_topic, msg.field_tag)
        try:
            decoded = self._decode_protobuf(msg.payload)
        except Exception:
            _LOGGER.debug("Failed to decode protobuf for topic %s", short_topic)
            return

        # Full decoded payload, DEBUG only. tools/narwal_capture.py and
        # tools/coverage_probe.py parse exactly this "DUMP <topic>: <payload>"
        # shape out of `ha core logs`, so the prefix is a contract — see
        # tools/CAPTURE_GUIDE.md. %r is formatted lazily, so a non-debug
        # install pays nothing for it.
        _LOGGER.debug("DUMP %s: %r", short_topic, decoded)

        if short_topic == "status/working_status":
            self._update_from_working_status_broadcast(decoded)
        elif short_topic == "status/robot_base_status":
            self._update_from_base_status_broadcast(decoded)
        elif short_topic == "upgrade/upgrade_status":
            self.state.update_from_upgrade_status(decoded)
        elif short_topic == "status/download_status":
            self.state.update_from_download_status(decoded)
        elif short_topic == "map/display_map":
            self._update_from_display_map_broadcast(decoded)
        if self.on_state_update:
            self.on_state_update(self.state)

    def _decode_protobuf(self, payload: bytes) -> dict[str, Any]:
        """Decode a protobuf payload without a schema using blackboxprotobuf."""
        import blackboxprotobuf  # lazy import — heavy dependency

        decoded, _ = blackboxprotobuf.decode_message(payload)
        return decoded

    async def _heartbeat_loop(self) -> None:
        """Send periodic WebSocket pings to keep the connection alive."""
        try:
            while self.connected:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                if self._ws:
                    await self._ws.ping()
                    _LOGGER.debug("Heartbeat ping sent")
        except asyncio.CancelledError:
            return
        except Exception:
            _LOGGER.debug("Heartbeat failed, connection may be lost")

    # --- Wake / Keep-alive ---

    @staticmethod
    def _encode_varint(value: int) -> bytes:
        """Encode an integer as a protobuf varint."""
        result = []
        while value > 0x7F:
            result.append((value & 0x7F) | 0x80)
            value >>= 7
        result.append(value & 0x7F)
        return bytes(result)

    @classmethod
    def _encode_varint_field(cls, field_num: int, value: int) -> bytes:
        """Encode a protobuf varint field (tag + value)."""
        tag = (field_num << 3) | 0  # wire type 0 = varint
        return cls._encode_varint(tag) + cls._encode_varint(value)

    @classmethod
    def _encode_bytes_field(cls, field_num: int, data: bytes) -> bytes:
        """Encode a protobuf length-delimited field."""
        tag = (field_num << 3) | 2  # wire type 2 = length-delimited
        return cls._encode_varint(tag) + cls._encode_varint(len(data)) + data

    @classmethod
    def _encode_string_field(cls, field_num: int, text: str) -> bytes:
        """Encode a protobuf string field."""
        return cls._encode_bytes_field(field_num, text.encode("utf-8"))

    # Broadcast topics needed for state, maps and diagnostics.
    _ALL_BROADCAST_TOPICS = [
        "status/robot_base_status",
        "status/working_status",
        "upgrade/upgrade_status",
        "status/download_status",
        "map/display_map",
        "status/time_line_status",
    ]

    def _build_topic_subscription(self, duration: int = 600) -> bytes:
        """Build active_robot_publish payload subscribing to ALL broadcast topics.

        The Narwal app sends this on open to tell the robot which topics to
        broadcast and for how long. Format: repeated field 1 = TopicDuration
        sub-messages with {1: topic_string, 2: duration_seconds}.
        """
        payload = b""
        for topic in self._ALL_BROADCAST_TOPICS:
            inner = (
                self._encode_string_field(1, topic)
                + self._encode_varint_field(2, duration)
            )
            payload += self._encode_bytes_field(1, inner)
        return payload

    async def subscribe_to_topics(self, duration: int = 600) -> None:
        """Send topic subscription to the robot.

        This tells the robot to broadcast display_map, working_status, etc.
        Must be called after connecting, especially if the robot is already
        awake (wake() skips the burst when robot_awake is True).
        """
        if not self.connected or not self._ws:
            return
        payload = self._build_topic_subscription(duration)
        frame = build_frame(
            self._full_topic(TOPIC_CMD_ACTIVE_ROBOT), payload
        )
        await self._ws.send(frame)
        _LOGGER.info("Topic subscription sent (duration=%ds)", duration)

    def _build_wake_commands(self) -> list[tuple[str, bytes]]:
        """Build the sequence of wake commands to try.

        Returns list of (short_topic, payload) tuples.  The first four
        commands are passive (subscription / heartbeat).  The final
        command is a query (get_device_base_status) that forces the
        robot's main processor to fully wake and enter command-ready
        mode.  Its field5 response ends up in _response_queue and is
        harmlessly drained by send_command() before real commands.
        """
        cmds: list[tuple[str, bytes]] = []

        # 1. notify_app_event — signal "app opened" (triggers robot wake)
        cmds.append((TOPIC_CMD_NOTIFY_APP_EVENT, self._encode_varint_field(1, 1)))

        # 2. active_robot_publish — subscribe to ALL topics for 10 minutes
        cmds.append((TOPIC_CMD_ACTIVE_ROBOT, self._build_topic_subscription(600)))

        # 3. active_robot_publish — simple duration (field 1 = 600)
        cmds.append((TOPIC_CMD_ACTIVE_ROBOT, self._encode_varint_field(1, 600)))

        # 4. app heartbeat — field 1 = 1
        cmds.append((TOPIC_CMD_APP_HEARTBEAT, self._encode_varint_field(1, 1)))

        # 5. get_device_base_status — forces robot CPU into command-ready
        #    state; passive commands alone only wake the WS server, not the
        #    application processor.  The field5 response is drained by
        #    send_command() before it processes real user commands.
        cmds.append((TOPIC_CMD_GET_BASE_STATUS, b""))

        return cmds

    async def _send_wake_burst(self) -> None:
        """Send all wake candidate commands in quick succession.

        Fire-and-forget: sends each command with a short delay between them.
        Does not wait for responses (the listener loop handles those).
        """
        if not self.connected or not self._ws:
            return

        commands = self._build_wake_commands()
        for short_topic, payload in commands:
            try:
                full_topic = self._full_topic(short_topic)
                frame = build_frame(full_topic, payload)
                await self._ws.send(frame)
                _LOGGER.debug("Wake burst: sent %s (%d bytes)", short_topic, len(payload))
            except Exception:
                _LOGGER.debug("Wake burst: failed to send %s", short_topic)
                return  # connection probably lost
            await asyncio.sleep(0.2)

    async def wake(self, timeout: float = WAKE_TIMEOUT, force: bool = False) -> bool:
        """Attempt to wake the robot from sleep.

        Sends repeated bursts of wake commands and waits for the robot to
        start broadcasting status messages.  Does NOT reconnect the
        WebSocket — the keepalive loop handles reconnect escalation
        independently (avoids race conditions with the listener loop).

        Args:
            timeout: Maximum seconds to wait for the robot to respond.
            force: If True, send wake burst even if robot_awake is True.
                Use when broadcasts have gone stale but the flag hasn't
                been reset yet.

        Returns:
            True if the robot has confirmed local reachability, False otherwise.
        """
        if not self.supports_broadcasts:
            return self.connected
        if self._robot_awake and not force:
            return True

        if not self.connected:
            raise NarwalConnectionError("Not connected to vacuum")

        _LOGGER.info("Attempting to wake robot (timeout=%.0fs)...", timeout)

        deadline = asyncio.get_event_loop().time() + timeout
        attempt = 0

        while asyncio.get_event_loop().time() < deadline:
            attempt += 1

            if not self.connected:
                _LOGGER.debug("Connection lost during wake — aborting")
                break

            await self._send_wake_burst()

            # Wait up to 5 seconds for a broadcast to arrive
            wait_end = min(
                asyncio.get_event_loop().time() + 5.0,
                deadline,
            )
            while asyncio.get_event_loop().time() < wait_end:
                if self._robot_awake:
                    _LOGGER.info("Robot woke up after %d attempt(s)", attempt)
                    return True
                await asyncio.sleep(0.3)

        _LOGGER.warning("Robot did not wake up within %.0fs (%d attempts)", timeout, attempt)
        return False

    # Topic subscription duration (seconds) and renewal interval
    _TOPIC_SUB_DURATION = 600  # 10 minutes — matches what Narwal app sends
    _TOPIC_RESUB_INTERVAL = 480  # re-subscribe every 8 min (before 10min expiry)

    # After this many consecutive wake bursts without response (~60s),
    # force a WebSocket reconnect to try triggering the robot's deep sleep
    # wake handler via a fresh TCP connection.
    _WAKE_RECONNECT_THRESHOLD = 2

    async def _renew_topic_subscription(self) -> bool:
        """Re-send active_robot_publish. True if it went out.

        Kept separate from the wake burst because the two are different acts:
        this only says "keep sending me broadcasts", while a burst tries to
        rouse a sleeping robot. Letting the subscription lapse is what freezes
        the entity at `docked` (#73), so it has to happen on its own schedule
        even when we are deliberately not waking the robot (#90).
        """
        if not self._ws:
            return False
        try:
            payload = self._build_topic_subscription(self._TOPIC_SUB_DURATION)
            frame = build_frame(self._full_topic(TOPIC_CMD_ACTIVE_ROBOT), payload)
            await self._ws.send(frame)
            _LOGGER.debug("Topic subscription renewed")
            return True
        except Exception:
            _LOGGER.debug("Topic re-subscribe failed")
            return False

    async def _keepalive_loop(self) -> None:
        """Periodically send wake/heartbeat commands to prevent robot from sleeping.

        Runs alongside the listener loop. Sends a lightweight heartbeat
        command every KEEPALIVE_INTERVAL seconds. If the robot stops
        broadcasting for BROADCAST_STALE_TIMEOUT seconds (goes back to
        sleep), resets _robot_awake and escalates to a full wake burst.

        Also re-subscribes to broadcast topics before the subscription
        expires (every _TOPIC_RESUB_INTERVAL seconds) so that display_map,
        robot_base_status, etc. keep flowing during long cleaning sessions.

        If wake bursts fail repeatedly, forces a WebSocket reconnect by
        closing the connection (the listener loop handles reconnection).
        """
        # None means "never sent", so the first tick always subscribes. This
        # used to be 0.0 and rely on time.monotonic() being larger than
        # _TOPIC_RESUB_INTERVAL, which is not guaranteed: monotonic() is only
        # promised to be monotonic, and on Linux it is system uptime. On a host
        # that booted less than 8 minutes ago the first subscription was
        # silently deferred. That was masked while the wake burst also carried
        # a subscription; now that a docked robot gets no burst (#90), a slow
        # first subscribe would mean no broadcasts at all until it fired (#73).
        last_resub_time: float | None = None
        consecutive_wake_failures = 0
        try:
            while self.connected:
                await asyncio.sleep(KEEPALIVE_INTERVAL)
                if not self.connected or not self._ws:
                    break

                if not self.supports_broadcasts:
                    continue

                # Check if broadcasts have gone stale (robot fell back asleep)
                if (
                    self._robot_awake
                    and self._last_broadcast_time > 0
                    and time.monotonic() - self._last_broadcast_time
                    > BROADCAST_STALE_TIMEOUT
                ):
                    _LOGGER.debug(
                        "No broadcast for %.0fs — robot may have gone to sleep",
                        time.monotonic() - self._last_broadcast_time,
                    )
                    self._robot_awake = False
                    consecutive_wake_failures = 0

                # Renew the broadcast subscription on its own schedule,
                # whether or not the robot is currently broadcasting. This used
                # to ride inside the awake branch and on every wake burst; now
                # that a docked robot is left alone, it has to stand on its own
                # or the subscription lapses and the entity freezes (#73).
                if (
                    last_resub_time is None
                    or time.monotonic() - last_resub_time
                    > self._TOPIC_RESUB_INTERVAL
                ):
                    if await self._renew_topic_subscription():
                        last_resub_time = time.monotonic()

                if self._robot_awake:
                    consecutive_wake_failures = 0
                    # Send lightweight heartbeat to keep robot awake.
                    # The Narwal app sends this continuously regardless of
                    # robot state — it's safe during cleaning.
                    try:
                        payload = self._encode_varint_field(1, 1)
                        frame = build_frame(
                            self._full_topic(TOPIC_CMD_APP_HEARTBEAT), payload
                        )
                        await self._ws.send(frame)
                        _LOGGER.debug("Keepalive heartbeat sent")
                    except Exception:
                        _LOGGER.debug("Keepalive send failed")
                        break
                elif self.state.is_docked:
                    # Silence from a docked robot is normal, not a fault.
                    #
                    # Measured on a Flow (AX12, v01.08.03.07) over 775s with
                    # every wake burst suppressed: the robot broadcasts for
                    # 30.0s or 45.5s, goes quiet for 60-124s, and comes back
                    # on its own. Six windows, five unprompted restarts, no
                    # bursts sent. BROADCAST_STALE_TIMEOUT is 15s, so treating
                    # that silence as sleep fired a full wake burst roughly
                    # every 46s -- about 1,900 a day at an idle docked robot,
                    # measured independently by @hyeok-yoo (#82, #90).
                    #
                    # Docked state stays fresh through the 60s poll, and
                    # commands still rouse the robot via wake() from
                    # _ensure_awake, so nothing here needs to nag it.
                    consecutive_wake_failures = 0
                    _LOGGER.debug(
                        "Docked and quiet for %.0fs — leaving the robot alone",
                        time.monotonic() - self._last_broadcast_time,
                    )

                else:
                    # Not docked and not broadcasting — a genuine dropout
                    # (mid-clean stall, deep sleep off the dock, dead socket).
                    consecutive_wake_failures += 1
                    _LOGGER.debug(
                        "Robot not awake, sending wake burst "
                        "(attempt %d/%d before reconnect)",
                        consecutive_wake_failures,
                        self._WAKE_RECONNECT_THRESHOLD,
                    )
                    await self._send_wake_burst()
                    last_resub_time = time.monotonic()

                    # Escalation: after repeated failures, force a fresh
                    # WebSocket connection. Close the socket — the listener
                    # loop's reconnect logic will establish a new connection.
                    if consecutive_wake_failures >= self._WAKE_RECONNECT_THRESHOLD:
                        _LOGGER.warning(
                            "Wake burst failed %d times — forcing WebSocket "
                            "reconnect to trigger deep sleep wake",
                            consecutive_wake_failures,
                        )
                        consecutive_wake_failures = 0
                        if self._ws:
                            await self._ws.close()
                        break  # exit keepalive; listener reconnects

        except asyncio.CancelledError:
            return
        except Exception:
            _LOGGER.debug("Keepalive loop error, will restart with listener")

    # --- Command infrastructure ---

    async def send_command(
        self,
        short_topic: str,
        payload: bytes = b"",
        timeout: float = COMMAND_RESPONSE_TIMEOUT,
    ) -> CommandResponse:
        """Send a command and wait for the field5 response.

        Uses a lock to prevent concurrent commands from racing on the
        response queue. Works both with and without start_listening().

        Args:
            short_topic: Command topic without prefix/device_id.
            payload: Protobuf-encoded payload (empty for most commands).
            timeout: Seconds to wait for response.

        Returns:
            CommandResponse with result code and decoded data.

        Raises:
            NarwalConnectionError: If not connected.
            NarwalCommandError: If response times out.
        """
        if not self.connected:
            raise NarwalConnectionError("Not connected to vacuum")

        async with self._command_lock:
            # Drain any stale responses (e.g. from fire-and-forget wake burst)
            drained = 0
            while not self._response_queue.empty():
                try:
                    self._response_queue.get_nowait()
                    drained += 1
                except asyncio.QueueEmpty:
                    break
            if drained:
                _LOGGER.debug("Drained %d stale field5 responses", drained)

            full_topic = self._full_topic(short_topic)
            frame = build_frame(full_topic, payload)
            await self._ws.send(frame)
            _LOGGER.debug("Sent command: %s (%d bytes)", short_topic, len(frame))

            # If listener is running, wait on the queue (avoid concurrent recv)
            if self._listener_active:
                try:
                    msg = await asyncio.wait_for(
                        self._response_queue.get(), timeout=timeout
                    )
                except TimeoutError:
                    raise NarwalCommandError(
                        f"No response for command '{short_topic}' within {timeout}s"
                    ) from None
            else:
                # No listener — read directly from websocket
                msg = await self._wait_for_field5_response(timeout)

            self._mark_response_received()

        # Decode response
        try:
            decoded = self._decode_protobuf(msg.payload)
        except Exception:
            decoded = {}

        # Field 1 is a result code for action commands (int),
        # but data for some query commands (string/bytes/dict).
        # Room-clean returns field 1 as a dict (config echo), not an int.
        raw_field1 = decoded.get("1", 0)
        try:
            result_code = int(raw_field1)
        except (ValueError, TypeError):
            result_code = CommandResult.SUCCESS  # non-int field 1 = data response = success

        return CommandResponse(
            result_code=result_code,
            data=decoded,
            raw_payload=msg.payload,
        )

    async def _wait_for_field5_response(
        self, timeout: float
    ) -> NarwalMessage:
        """Read from WebSocket until a field5 response arrives."""
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                data = await asyncio.wait_for(
                    self._ws.recv(), timeout=min(remaining, 1.0)
                )
            except TimeoutError:
                continue

            if not isinstance(data, bytes) or len(data) < 4:
                continue

            try:
                msg = parse_frame(data)
            except ProtocolError:
                continue

            if msg.field_tag == PROTOBUF_FIELD5_TAG:
                return msg

            # Process broadcast messages while waiting
            short_topic = msg.short_topic
            try:
                decoded = self._decode_protobuf(msg.payload)
            except Exception:
                continue

            if short_topic == "status/working_status":
                self._update_from_working_status_broadcast(decoded)
            elif short_topic == "status/robot_base_status":
                self._update_from_base_status_broadcast(decoded)
            elif short_topic == "upgrade/upgrade_status":
                self.state.update_from_upgrade_status(decoded)
            elif short_topic == "status/download_status":
                self.state.update_from_download_status(decoded)
            elif short_topic == "map/display_map":
                self._update_from_display_map_broadcast(decoded)

        raise NarwalCommandError(
            f"No field5 response within {timeout}s"
        )

    async def send_raw(
        self, topic: str, payload: bytes, header_byte: int | None = None
    ) -> None:
        """Send a raw command frame to the vacuum.

        Args:
            topic: Full topic string.
            payload: Protobuf-encoded payload.
            header_byte: Header byte (auto-calculated if None).

        Raises:
            NarwalConnectionError: If not connected.
        """
        if not self.connected:
            raise NarwalConnectionError("Not connected to vacuum")

        frame = build_frame(topic, payload, header_byte)
        await self._ws.send(frame)
        _LOGGER.debug("Sent raw to topic: %s (%d bytes)", topic, len(frame))

    # --- High-level commands ---

    async def locate(self) -> CommandResponse:
        """Trigger locate sound — robot says 'Robot is here'."""
        return await self.send_command(TOPIC_CMD_YELL)

    async def _all_room_ids(self) -> list[int]:
        """Every cleanable room id on the active map, fetching the map if needed."""
        map_data = self.state.map_data
        if not (map_data and map_data.rooms):
            try:
                map_data = await self.get_map()
            except Exception:  # noqa: BLE001 — best-effort prefetch
                _LOGGER.debug("start(): get_map failed; no room list available")
                map_data = self.state.map_data
        if not (map_data and map_data.rooms):
            return []
        return [r.room_id for r in map_data.rooms if r.room_id > 0]

    async def start(
        self,
        *,
        work_mode: WorkMode = WorkMode.VACUUM_AND_MOP,
        fan: FanLevel = FanLevel.NORMAL,
        water: MopHumidity = MopHumidity.NORMAL,
        mop_strength: MopStrengthLevel = MopStrengthLevel.NORMAL,
        passes: int = 1,
        route: CleaningRoute | None = None,
    ) -> CommandResponse:
        """Start a whole-house clean.

        Enumerates every room on the active map and issues clean/start_clean,
        matching the app's allRoomIds() path.

        The old implementation sent a minimal payload to clean/plan/start.
        Newer firmware answers SUCCESS there and does nothing, so this path
        fails clearly when no room list is available instead (#69).
        """
        if not self.connected:
            raise NarwalConnectionError("Not connected to vacuum")
        if _robot_start_blocked(self.state):
            _LOGGER.warning(
                "start: robot or dock task active (%s); not starting whole-house clean",
                self.state.active_dock_task_keys or "unmapped",
            )
            return CommandResponse(result_code=CommandResult.NOT_APPLICABLE)
        room_ids = await self._all_room_ids()
        if not room_ids:
            _LOGGER.warning("start(): no map rooms available")
            return CommandResponse(result_code=CommandResult.NOT_READY)
        _LOGGER.info("start(): whole-house clean over %d rooms", len(room_ids))
        return await self.start_rooms(
            room_ids,
            work_mode=work_mode,
            fan=fan,
            water=water,
            mop_strength=mop_strength,
            passes=passes,
            route=route,
        )

    def _build_clean_payload_v2(
        self,
        room_ids: list[int],
        suction: int = 3,
        mop_humidity: int = 2,
        passes: int = 1,
        clean_mode: int = 3,
    ) -> bytes:
        """Build clean task payload using the v2 schema (firmware v01.07.22+).

        Observed in issue #36 from a Flow on firmware v01.07.22.00.
        Each room entry uses a nested room_id:

            {
              1: {1: 1, 2: <room_id>},                # nested room ref
              2: {1: <suction>, 2: <clean_mode>,      # per-room params
                  3: <passes>, 7: <mop_humidity>},
              3: <sequence>,                          # 1-indexed
            }

        Outer envelope:
            {1: {1: 1, 2: [<rooms>], 3: {}, 5: 6}}

        Defaults match a normal whole-house clean: max Flow 1 suction (3),
        sweep+mop (3 in v2 schema), single pass, wet mop.

        Args:
            room_ids: List of room IDs from RoomInfo.room_id.
            suction: 1-3 (Flow 1) / 1-4 (Flow 2). Default 3 = max for Flow 1.
            mop_humidity: 1=dry, 2=wet, 3=very wet. Default 2.
            passes: Number of passes. Default 1.
            clean_mode: v2 schema cleanMode (3 observed for sweep+mop).

        Returns:
            Encoded protobuf bytes for clean/start_clean.
        """
        import blackboxprotobuf

        room_entries = [
            {
                "1": {"1": 1, "2": rid},
                "2": {
                    "1": suction,
                    "2": clean_mode,
                    "3": passes,
                    "7": mop_humidity,
                },
                "3": idx + 1,
            }
            for idx, rid in enumerate(room_ids)
        ]

        room_entry_typedef = {
            "type": "message",
            "seen_repeated": True,
            "message_typedef": {
                "1": {
                    "type": "message",
                    "message_typedef": {
                        "1": {"type": "int"},
                        "2": {"type": "uint"},
                    },
                },
                "2": {
                    "type": "message",
                    "message_typedef": {
                        "1": {"type": "int"},
                        "2": {"type": "int"},
                        "3": {"type": "int"},
                        "7": {"type": "int"},
                    },
                },
                "3": {"type": "int"},
            },
        }

        msg = {
            "1": {
                "1": 1,
                "2": room_entries if len(room_entries) > 1 else room_entries[0],
                "3": {},
                "5": 6,
            }
        }
        typedef = {
            "1": {
                "type": "message",
                "message_typedef": {
                    "1": {"type": "int"},
                    "2": room_entry_typedef,
                    "3": {"type": "message", "message_typedef": {}},
                    "5": {"type": "int"},
                },
            }
        }
        return blackboxprotobuf.encode_message(msg, typedef)

    # WorkMode -> (CleanParam.mode tag 1, pass-count tags to set from `passes`). The robot's
    # execution mode is CleanTask.taskType (= the WorkMode value); CleanParam.mode and the
    # pass tag are derived here so the two can't drift. Live-validated on a Flow 2; see
    # project_history.md "CleanParam — fully decoded".
    _WORK_MODE_PARAM: dict[WorkMode, tuple[int, tuple[str, ...]]] = {
        WorkMode.VACUUM: (2, ("5",)),               # sweepTime
        WorkMode.MOP: (3, ("6",)),                 # mopTime
        WorkMode.VACUUM_THEN_MOP: (5, ("5", "6")),  # sweep + mop pass counts
        WorkMode.VACUUM_AND_MOP: (4, ("7",)),      # sweepMopSyncTime
    }
    # The app uses taskType 6 for a custom task whose CleanItems carry the
    # effective mode independently.  Captured in upstream issue #36 and
    # runtime-validated on Flow firmware v01.07.23.00.
    _CUSTOM_CLEAN_TASK_TYPE = 6

    def _clean_param_for_settings(self, settings: RoomCleanSettings) -> dict[str, int]:
        """Return a CleanParam protobuf dict for one room."""
        param_mode, pass_tags = self._WORK_MODE_PARAM[settings.work_mode]
        param: dict[str, int] = {
            "1": int(param_mode),
            "2": int(settings.fan),
            "3": int(settings.mop_strength),
            "4": int(settings.water),
        }
        if settings.route is not None:
            param["8"] = int(settings.route)
        for tag in pass_tags:
            param[tag] = int(settings.passes)
        return param

    @classmethod
    def _task_type_for_settings(
        cls,
        room_settings: list[RoomCleanSettings],
    ) -> int:
        """Return the outer CleanTask type for a room-clean payload."""
        modes = {settings.work_mode for settings in room_settings}
        if len(modes) == 1:
            return int(next(iter(modes)))
        return cls._CUSTOM_CLEAN_TASK_TYPE

    def _build_start_clean_payload(
        self,
        room_ids: list[int],
        map_id: int,
        *,
        work_mode: WorkMode = WorkMode.VACUUM_AND_MOP,
        fan: FanLevel = FanLevel.NORMAL,
        water: MopHumidity = MopHumidity.NORMAL,
        mop_strength: MopStrengthLevel = MopStrengthLevel.NORMAL,
        passes: int = 1,
        route: CleaningRoute | None = None,
        room_settings: Mapping[int, RoomCleanSettings] | None = None,
    ) -> bytes:
        """Build a clean/start_clean request for the given rooms.

        StartClean_Request{1: CleanTask{1: map_id, 2: [CleanItem...], 3: {} (TaskOption),
        5: taskType}}; CleanItem{1: ZoneOption{1: 1 (room zone), 2: room_id}, 2: CleanParam,
        3: order}. Uniform tasks use the WorkMode as taskType. Mixed-mode tasks use
        the app's custom taskType (6), with each CleanParam carrying its own mode.
        overlapLevel is CleanParam tag 8 when supplied.

        Args:
            room_ids: Robot room IDs (RoomInfo.room_id).
            map_id: Active map id (MapData.map_id, get_map field 2.1).
            work_mode: Vacuum / mop / vacuum-then-mop / vacuum-and-mop.
            fan: Suction level (CleanParam tag 2).
            water: Mop water volume (tag 4).
            mop_strength: Mop scrub intensity (tag 3).
            passes: Clean count, routed to the pass tag(s) for the mode.
            route: Optional route overlap level (tag 8).
            room_settings: Optional per-room settings keyed by robot room ID.
        """
        import blackboxprotobuf

        default_settings = RoomCleanSettings(
            work_mode=work_mode,
            fan=fan,
            water=water,
            mop_strength=mop_strength,
            passes=passes,
            route=route,
        )
        settings_by_room = (
            {rid: room_settings.get(rid, default_settings) for rid in room_ids}
            if room_settings
            else {rid: default_settings for rid in room_ids}
        )
        task_type = self._task_type_for_settings(
            list(settings_by_room.values()),
        )
        param_keys: set[str] = set()
        params_by_room: dict[int, dict[str, int]] = {}
        for rid, settings in settings_by_room.items():
            params_by_room[rid] = self._clean_param_for_settings(settings)
            param_keys.update(params_by_room[rid])

        items = [
            {"1": {"1": 1, "2": rid}, "2": params_by_room[rid], "3": idx + 1}
            for idx, rid in enumerate(room_ids)
        ]
        task = {
            "1": map_id,
            "2": items if len(items) > 1 else items[0],
            "3": {},
            "5": int(task_type),  # CleanTask.taskType
        }
        item_typedef = {
            "type": "message",
            "seen_repeated": True,
            "message_typedef": {
                "1": {"type": "message", "message_typedef": {
                    "1": {"type": "int"}, "2": {"type": "int"},
                }},
                # Derive the CleanParam typedef from the emitted dict — bbpb silently
                # drops any tag absent from the typedef.
                "2": {"type": "message", "message_typedef": {
                    k: {"type": "int"} for k in sorted(param_keys, key=int)
                }},
                "3": {"type": "int"},
            },
        }
        typedef = {"1": {"type": "message", "message_typedef": {
            "1": {"type": "int"},
            "2": item_typedef,
            "3": {"type": "message", "message_typedef": {}},
            "5": {"type": "int"},
        }}}
        return blackboxprotobuf.encode_message({"1": task}, typedef)

    async def start_rooms(
        self,
        room_ids: list[int],
        *,
        work_mode: WorkMode = WorkMode.VACUUM_AND_MOP,
        fan: FanLevel = FanLevel.NORMAL,
        water: MopHumidity = MopHumidity.NORMAL,
        mop_strength: MopStrengthLevel = MopStrengthLevel.NORMAL,
        passes: int = 1,
        route: CleaningRoute | None = None,
        room_settings: Mapping[int, RoomCleanSettings] | None = None,
    ) -> CommandResponse:
        """Start cleaning the given rooms via clean/start_clean.

        Room cleaning must use clean/start_clean (StartClean → CleanTask), not
        clean/plan/start: on Flow firmware the latter is StartWithPlan{planId,
        mapId} and ignores any room payload — the root cause of #25/#37, where
        the robot undocks and wanders instead of cleaning the selected rooms.
        The CleanTask carries the active map id (get_map field 2.1).

        clean/start_clean only works while docked; from STANDBY the robot
        returns NOT_READY (4). Callers should start from the dock; this retries
        briefly to cover the dock settling transition.

        Args:
            room_ids: Robot room IDs (RoomInfo.room_id), mapped from HA areas.
            work_mode, fan, water, mop_strength, passes, route: CleanParam settings —
                see _build_start_clean_payload.
            room_settings: Optional per-room settings keyed by robot room ID.
        """
        if not room_ids:
            return CommandResponse(result_code=CommandResult.NOT_READY)
        async with self._robot_start_lock:
            if _robot_start_blocked(self.state):
                _LOGGER.warning(
                    "start_rooms: robot or dock guard active (%s); not starting room clean",
                    self.state.active_dock_task_keys or "private",
                )
                return CommandResponse(result_code=CommandResult.NOT_APPLICABLE)

            map_data = self.state.map_data
            if not map_data or not map_data.map_id:
                try:
                    map_data = await self.get_map()
                except NarwalCommandError as err:
                    _LOGGER.warning("start_rooms: map fetch failed: %s", err)
                    map_data = self.state.map_data
            map_id = map_data.map_id if map_data else 0
            if not map_id:
                _LOGGER.warning(
                    "start_rooms: no active map id available; cannot start room clean"
                )
                return CommandResponse(result_code=CommandResult.NOT_APPLICABLE)

            try:
                payload = self._build_start_clean_payload(
                    room_ids,
                    map_id,
                    work_mode=work_mode,
                    fan=fan,
                    water=water,
                    mop_strength=mop_strength,
                    passes=passes,
                    route=route,
                    room_settings=room_settings,
                )
            except ValueError as err:
                _LOGGER.warning("start_rooms: %s", err)
                return CommandResponse(result_code=CommandResult.NOT_APPLICABLE)
            if _robot_start_blocked(self.state):
                _LOGGER.warning(
                    "start_rooms: state changed before dispatch; not starting room clean"
                )
                return CommandResponse(result_code=CommandResult.NOT_APPLICABLE)
            resp = await self.send_command(
                TOPIC_CMD_CLEAN_TASK, payload=payload, timeout=10.0,
            )
            for _ in range(3):
                if resp.result_code != CommandResult.NOT_READY:
                    break
                if not self.state.is_docked:
                    _LOGGER.warning(
                        "start_rooms: robot not docked (status=%s); clean/start_clean "
                        "requires the robot on the dock",
                        self.state.working_status.name,
                    )
                    break
                _LOGGER.info("start_rooms: robot docking/settling, retrying clean/start_clean")
                await asyncio.sleep(3.0)
                if _robot_start_blocked(self.state):
                    _LOGGER.warning(
                        "start_rooms: state changed before retry; not starting room clean"
                    )
                    return CommandResponse(result_code=CommandResult.NOT_APPLICABLE)
                resp = await self.send_command(
                    TOPIC_CMD_CLEAN_TASK, payload=payload, timeout=10.0,
                )
            if _accepted_response(resp):
                self.state.assume_robot_clean()
            return resp

    async def start_easy_clean(self) -> CommandResponse:
        """Start quick/easy clean."""
        async with self._robot_start_lock:
            if _robot_start_blocked(self.state):
                _LOGGER.warning(
                    "start_easy_clean: robot or dock guard active (%s); not starting quick clean",
                    self.state.active_dock_task_keys or "private",
                )
                return CommandResponse(result_code=CommandResult.NOT_APPLICABLE)
            response = await self.send_command(TOPIC_CMD_EASY_CLEAN)
            if _accepted_response(response):
                self.state.assume_robot_clean()
            return response

    async def pause(self) -> CommandResponse:
        """Pause current task."""
        return await self.send_command(TOPIC_CMD_PAUSE)

    async def resume(self, timeout: float = COMMAND_RESPONSE_TIMEOUT) -> CommandResponse:
        """Resume paused task."""
        had_paused_clean_context = self.state.is_paused and (
            self.state.working_status in ACTIVE_CLEANING_STATUSES
            or self.state.has_paused_clean_task_context
        )
        terminal_generation = self.state.terminal_working_status_generation
        response = await self.send_command(TOPIC_CMD_RESUME, timeout=timeout)
        terminal_during_request = (
            self.state.terminal_working_status_generation != terminal_generation
            or self.state.working_status
            in {WorkingStatus.TASK_COMPLETED, WorkingStatus.ERROR}
            or self.state.has_error
        )
        if (
            _accepted_response(response)
            and had_paused_clean_context
            and not terminal_during_request
        ):
            self.state.mark_robot_resumed()
        return response

    async def stop(self, timeout: float = 15.0) -> CommandResponse:
        """Force-stop current task.

        Note: force_end is slow — robot physically stops before responding.
        Previous testing shows 10-15s response times from CLEANING state.
        """
        return await self.send_command(TOPIC_CMD_FORCE_END, timeout=timeout)

    async def cancel(self) -> CommandResponse:
        """Cancel current task."""
        return await self.send_command(TOPIC_CMD_CANCEL)

    async def _refresh_after_dock_stop(self) -> bool:
        """Refresh robot state after a dock stop command."""
        try:
            response = await self.get_status(
                full_update=not self.state.has_recent_active_working_status
            )
        except (NarwalCommandError, NarwalConnectionError) as err:
            _LOGGER.debug("Status refresh after dock stop failed: %s", err)
            return False
        return _has_dock_status_payload(response)

    def _should_wait_for_scoped_dock_task(self, task: str | None) -> bool:
        """Return true when a typed scoped-stop target may still be settling."""
        if task not in _DOCK_TASK_FORCE_END_PAYLOADS:
            return False
        if task in self.state.telemetry_dock_task_keys:
            return False
        if task == self.state.assumed_active_dock_task:
            return True
        if self.state.has_unmapped_active_dock_task:
            return True
        if task == DOCK_TASK_DRY_DUST_BIN and self.state.dock_activity == 6:
            return True
        return self.state.station_activity == 4 and not self.state.active_dock_drying_tasks

    async def _refresh_before_dock_stop(
        self,
        task: str | None,
    ) -> CommandResponse:
        """Refresh dock state, allowing typed scoped-stop telemetry to settle."""
        response = await self.get_status(
            full_update=not self.state.has_recent_active_working_status
        )
        if not response.accepted or not _has_dock_status_payload(response):
            return response
        if not self._should_wait_for_scoped_dock_task(task):
            return response

        for _ in range(_DOCK_TASK_IDENTIFY_ATTEMPTS):
            await asyncio.sleep(_DOCK_TASK_IDENTIFY_DELAY)
            response = await self.get_status(
                full_update=not self.state.has_recent_active_working_status
            )
            if not response.accepted or not _has_dock_status_payload(response):
                return response
            if not self._should_wait_for_scoped_dock_task(task):
                return response
        return response

    async def stop_dock_task(self, task: str | None = None) -> CommandResponse:
        """Stop the active dock task without targeting a different task."""
        async with self._dock_task_lock:
            if (
                self.state.has_unmapped_active_dock_task
                and task not in _DOCK_TASK_FORCE_END_PAYLOADS
                and not _can_force_end_scoped_dock_task(self.state, task)
            ):
                return CommandResponse(result_code=CommandResult.NOT_APPLICABLE)
            refresh = await self._refresh_before_dock_stop(task)
            if not refresh.accepted:
                return refresh
            if not _has_dock_status_payload(refresh):
                return CommandResponse(
                    result_code=CommandResult.NOT_READY,
                    data=refresh.data,
                    raw_payload=refresh.raw_payload,
                )
            if self.state.has_unmapped_active_dock_task and not (
                _can_force_end_scoped_dock_task(self.state, task)
            ):
                return CommandResponse(result_code=CommandResult.NOT_APPLICABLE)
            active_tasks = self.state.active_dock_task_keys
            if task is not None and task not in active_tasks:
                return CommandResponse(result_code=CommandResult.NOT_APPLICABLE)
            if task is None and len(active_tasks) != 1:
                return CommandResponse(result_code=CommandResult.NOT_APPLICABLE)
            active_task = task or (active_tasks[0] if active_tasks else None)
            if active_task is None:
                return CommandResponse(result_code=CommandResult.NOT_APPLICABLE)
            if (
                _robot_work_blocks_generic_dock_stop(self.state)
                and active_task not in _DOCK_TASK_FORCE_END_PAYLOADS
            ):
                return CommandResponse(result_code=CommandResult.NOT_APPLICABLE)

            payload = _DOCK_TASK_FORCE_END_PAYLOADS.get(active_task)
            if payload is None:
                if active_task == DOCK_TASK_DRY_DUST_BIN:
                    return CommandResponse(result_code=CommandResult.NOT_APPLICABLE)
                if not _has_generic_dock_stop_proof(self.state):
                    return CommandResponse(result_code=CommandResult.NOT_APPLICABLE)
                if len(active_tasks) > 1:
                    return CommandResponse(result_code=CommandResult.NOT_APPLICABLE)
                response = await self.stop(timeout=15.0)
            else:
                if active_task not in self.state.telemetry_dock_task_keys:
                    return CommandResponse(result_code=CommandResult.NOT_APPLICABLE)
                response = await self.send_command(
                    TOPIC_CMD_FORCE_END,
                    payload=payload,
                    timeout=15.0,
                )

            await asyncio.sleep(_DOCK_TASK_REFRESH_DELAY)
            refreshed = await self._refresh_after_dock_stop()
            if _accepted_response(response):
                self.state.clear_assumed_dock_task(active_task)
                # Scoped force-end acknowledges the exact drying task. Clear its
                # cached timer after a fresh dock response instead of waiting for
                # the old timer snapshot to expire.
                if refreshed and (
                    payload is not None or _dock_status_confirms_idle(self.state)
                ):
                    self.state.clear_dock_drying_task(active_task)
            return response

    async def return_to_base(self, timeout: float = COMMAND_RESPONSE_TIMEOUT) -> CommandResponse:
        """Return to charging dock."""
        return await self.send_command(TOPIC_CMD_RECALL, timeout=timeout)

    async def set_fan_speed(self, level: FanLevel | int) -> CommandResponse:
        """Set suction fan speed live (clean/set_fan_level, field 1 = SweepFanLevel).

        The live command's enum is SweepFanLevel, which has no SUPER. A SUPER request
        is clamped to its highest supported tier, DEEP.
        """
        live = int(fan_level_for_live_command(level))
        payload = b"\x08" + bytes([live & 0x7F])
        return await self.send_command(TOPIC_CMD_SET_FAN_LEVEL, payload)

    async def set_mop_humidity(self, level: MopHumidity | int) -> CommandResponse:
        """Set mop water volume live (clean/set_mop_humidity, field 1 = MopHumidity).

        Args:
            level: MopHumidity enum or int (1=dry, 2=normal, 3=wet).
        """
        payload = b"\x08" + bytes([int(level) & 0x7F])
        return await self.send_command(TOPIC_CMD_SET_MOP_HUMIDITY, payload)

    async def wash_mop(self) -> CommandResponse:
        """Wash the mop pads at the station."""
        return await self._start_dock_task(DOCK_TASK_WASH_MOP, TOPIC_CMD_WASH_MOP)

    async def wash_mop_by_robot_status(self) -> CommandResponse:
        """Wash mop pads using the app's status-gated station command."""
        return await self._start_dock_task(
            DOCK_TASK_WASH_MOP,
            TOPIC_CMD_WASH_MOP_BY_ROBOT_STATUS,
        )

    async def dry_mop(self) -> CommandResponse:
        """Dry the mop pads at the station."""
        return await self._start_dock_task(DOCK_TASK_DRY_MOP, TOPIC_CMD_DRY_MOP)

    async def dry_dust_bag(self) -> CommandResponse:
        """Dry or disinfect the robot dust bin at the station."""
        return await self._start_dock_task(
            DOCK_TASK_DRY_DUST_BIN,
            TOPIC_CMD_DRY_DUST_BAG,
        )

    async def dry_station_bag(self) -> CommandResponse:
        """Dry or disinfect the dock dust bag at the station."""
        return await self._start_dock_task(
            DOCK_TASK_DRY_DOCK_BAG,
            TOPIC_CMD_DRY_STATION_BAG,
        )

    async def empty_dustbin(self) -> CommandResponse:
        """Empty the dustbin at the station."""
        return await self._start_dock_task(
            DOCK_TASK_EMPTY_DUSTBIN,
            TOPIC_CMD_DUST_GATHERING,
        )

    async def _start_dock_task(self, task: str, topic: str) -> CommandResponse:
        """Start one dock task after atomically validating reported state."""
        async with self._dock_task_lock:
            if not self._can_start_dock_task(task):
                return CommandResponse(result_code=CommandResult.NOT_APPLICABLE)
            refresh = await self.get_status(
                full_update=not self.state.has_recent_active_working_status
            )
            if not refresh.accepted:
                return refresh
            if not _has_dock_status_payload(refresh):
                return CommandResponse(
                    result_code=CommandResult.NOT_READY,
                    data=refresh.data,
                    raw_payload=refresh.raw_payload,
                )
            if not self._can_start_dock_task(task):
                return CommandResponse(result_code=CommandResult.NOT_APPLICABLE)
            response = await self.send_command(topic)
            if _accepted_response(response):
                self.state.assume_dock_task(task)
            return response

    def _can_start_dock_task(self, task: str) -> bool:
        """Return true when reported state permits a new dock-side command."""
        state = self.state
        if state.has_error or state.working_status in (
            WorkingStatus.ERROR,
            WorkingStatus.UNKNOWN,
        ):
            return False
        if not state.is_docked or _clean_session_context(state):
            return False
        if state.has_unmapped_active_dock_task:
            return False
        if state.assumed_active_dock_task is not None:
            return False
        active_tasks = set(state.active_dock_task_keys)
        if task in active_tasks:
            return False
        return not active_tasks

    # --- Query commands ---

    async def get_device_info(self) -> DeviceInfo:
        """Query device identity (product key, device ID, firmware)."""
        resp = await self.send_command(TOPIC_CMD_GET_DEVICE_INFO)
        data = resp.data

        def _clean_bytes(val: Any) -> str:
            if isinstance(val, bytes):
                return val.decode("utf-8", errors="replace").rstrip("\n")
            s = str(val)
            if s.startswith("b'") and s.endswith("'"):
                s = s[2:-1]
            return s.rstrip("\n")

        info = DeviceInfo(
            product_key=_clean_bytes(data.get("1", "")),
            device_id=_clean_bytes(data.get("2", "")),
            firmware_version=_clean_bytes(data.get("3", "")),
        )
        self.state.device_info = info
        self.state.firmware_version = info.firmware_version

        # Update topic prefix to match this device's product key
        if info.product_key:
            self.topic_prefix = f"/{info.product_key}"
            _LOGGER.info("Topic prefix set to %s", self.topic_prefix)

        return info

    async def get_feature_list(self) -> dict[int, int]:
        """Query supported features. Returns {feature_id: value}."""
        resp = await self.send_command(TOPIC_CMD_GET_FEATURE_LIST)
        return {int(k): int(v) for k, v in resp.data.items()}

    async def get_status(self, full_update: bool = True) -> CommandResponse:
        """Query current device base status.

        Args:
            full_update: If True, update all state fields (working_status,
                battery, etc). If False, only update hardware-sampled fields
                (battery, health) — used when robot is not broadcasting and
                working_status in the response may be stale.
        """
        resp = await self.send_command(TOPIC_CMD_GET_BASE_STATUS)
        status_data = _base_status_payload(resp)
        if status_data is not None:
            # Log the whole field map, not a chosen few: field-level bug reports
            # (wrong tank state, a value we don't map) are unanswerable from a log
            # that omits the field in question — see #77.
            _LOGGER.debug(
                "get_status response (full=%s): field3=%r, field2=%r, all_fields=%r",
                full_update,
                status_data.get("3") if isinstance(status_data, dict) else None,
                status_data.get("2") if isinstance(status_data, dict) else None,
                status_data,
            )
            if full_update:
                self.state.update_from_base_status(status_data)
            else:
                self.state.update_battery_from_base_status(status_data)
        else:
            _LOGGER.debug("get_status response has no field 2; keys: %s", list(resp.data.keys()))
            if resp.accepted:
                return CommandResponse(
                    result_code=CommandResult.NOT_READY,
                    data=resp.data,
                    raw_payload=resp.raw_payload,
                )
        return resp

    async def get_current_task(self) -> CommandResponse:
        """Query the current clean task."""
        return await self.send_command(TOPIC_CMD_GET_CURRENT_TASK)

    async def get_consumable_info(self) -> CommandResponse:
        """Query consumable maintain/replace alert lists (not broadcast)."""
        resp = await self.send_command(TOPIC_CMD_GET_CONSUMABLE_INFO, timeout=15.0)
        self.state.update_from_consumable_info(resp.data)
        return resp

    async def get_map(self) -> MapData:
        """Download the full map data."""
        resp = await self.send_command(TOPIC_CMD_GET_MAP, timeout=15.0)
        if not resp.accepted:
            raise NarwalCommandError(
                f"get_map failed with code {resp.result_code}"
            )
        map_data = MapData.from_response(resp.data)
        if not map_data.map_id:
            raise NarwalCommandError("get_map response did not contain an active map")
        self.state.map_data = map_data
        return map_data

    async def get_all_maps(self) -> CommandResponse:
        """Download all saved/reduced maps."""
        return await self.send_command(TOPIC_CMD_GET_ALL_MAPS, timeout=15.0)

    async def take_picture(self) -> bytes | None:
        """Capture a photo from the robot's camera.

        Returns raw image bytes from field 2 of the response, or None on failure.
        Note: the image is AES-encrypted; decoding requires the APK-derived key
        which is not yet known. Callers receive raw bytes as-is.
        """
        try:
            resp = await self.send_command(TOPIC_CMD_TAKE_PICTURE, timeout=15.0)
        except Exception:
            _LOGGER.warning("take_picture command failed")
            return None
        if resp.result_code == CommandResult.SUCCESS:
            return resp.data.get("2")
        _LOGGER.warning("take_picture returned result_code=%d", resp.result_code)
        return None

    async def get_robot_debug_image(self) -> bytes | None:
        """Fetch the robot's latest debug/planning image (cleartext PNG).

        developer/get_robot_debug_image returns a batch of carpet-detection and
        planning overlays as UNENCRYPTED PNGs (222x232 RGB), keyed by filename
        (InitCarpet/GlobalCarpet/UpdateRoomN_PlanningCarpet). Only produced while
        the robot is mapping/cleaning; returns None when idle or unavailable.

        Response shape: field 1 = repeated {1: filename, 2: png_bytes}. Returns the
        most recent whole-map ("GlobalCarpet") PNG if present, else the last image
        in the batch, else None.
        """
        try:
            resp = await self.send_command(TOPIC_CMD_GET_DEBUG_IMAGE, timeout=15.0)
        except Exception:
            _LOGGER.warning("get_robot_debug_image command failed")
            return None
        entries = resp.data.get("1")
        if isinstance(entries, dict):
            entries = [entries]
        if not isinstance(entries, list):
            return None
        best: bytes | None = None
        last: bytes | None = None
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            img = entry.get("2")
            if not isinstance(img, (bytes, bytearray)):
                continue
            last = bytes(img)
            name = entry.get("1")
            if isinstance(name, str) and "GlobalCarpet" in name:
                best = bytes(img)  # prefer the most recent whole-map carpet overlay
        return best or last

    async def set_led(self, on: bool) -> None:
        """Turn the camera LED fill light on or off.

        Payload: 0x08 0x01 = on, 0x08 0x00 = off (protobuf field 1, varint).
        """
        payload = b"\x08\x01" if on else b"\x08\x00"
        try:
            resp = await self.send_command(TOPIC_CMD_SET_LED, payload=payload)
        except Exception:
            _LOGGER.warning("set_led(%s) command failed", on)
            return
        if resp.result_code not in (CommandResult.SUCCESS, CommandResult.NOT_APPLICABLE):
            _LOGGER.warning(
                "set_led(%s) unexpected result_code=%d", on, resp.result_code
            )

    async def set_ambient_light_mode(
        self, mode: AmbientLightCtrlType | int
    ) -> CommandResponse | None:
        """Set the base station ambient light mode."""
        mode = AmbientLightCtrlType(mode)
        payload = self._encode_varint_field(1, int(mode))
        try:
            resp = await self.send_command(
                TOPIC_CMD_AMBIENT_LIGHT_CTRL,
                payload=payload,
            )
        except Exception:
            _LOGGER.warning("set_ambient_light_mode(%s) command failed", mode)
            return None
        if resp.result_code not in (
            0,
            CommandResult.SUCCESS,
            CommandResult.NOT_APPLICABLE,
            CommandResult.APPLIED,
        ):
            _LOGGER.warning(
                "set_ambient_light_mode(%s) unexpected result_code=%d",
                mode,
                resp.result_code,
            )
        return resp
