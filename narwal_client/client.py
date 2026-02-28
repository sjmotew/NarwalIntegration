"""WebSocket client for Narwal robot vacuum."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable
from typing import Any

import websockets
import websockets.exceptions

from .const import (
    BROADCAST_STALE_TIMEOUT,
    COMMAND_RESPONSE_TIMEOUT,
    DEFAULT_PORT,
    HEARTBEAT_INTERVAL,
    KEEPALIVE_INTERVAL,
    RECONNECT_BACKOFF_FACTOR,
    RECONNECT_INITIAL_DELAY,
    RECONNECT_MAX_DELAY,
    TOPIC_CMD_ACTIVE_ROBOT,
    TOPIC_CMD_APP_HEARTBEAT,
    TOPIC_CMD_CANCEL,
    TOPIC_CMD_DRY_MOP,
    TOPIC_CMD_DUST_GATHERING,
    TOPIC_CMD_EASY_CLEAN,
    TOPIC_CMD_FORCE_END,
    TOPIC_CMD_GET_ALL_MAPS,
    TOPIC_CMD_GET_BASE_STATUS,
    TOPIC_CMD_GET_CURRENT_TASK,
    TOPIC_CMD_GET_DEVICE_INFO,
    TOPIC_CMD_GET_FEATURE_LIST,
    TOPIC_CMD_GET_MAP,
    TOPIC_CMD_NOTIFY_APP_EVENT,
    TOPIC_CMD_PAUSE,
    TOPIC_CMD_PING,
    TOPIC_CMD_RECALL,
    TOPIC_CMD_RESUME,
    TOPIC_CMD_SET_FAN_LEVEL,
    TOPIC_CMD_SET_MOP_HUMIDITY,
    TOPIC_CMD_START_CLEAN,
    TOPIC_CMD_WASH_MOP,
    TOPIC_CMD_YELL,
    DEFAULT_TOPIC_PREFIX,
    WAKE_TIMEOUT,
    FanLevel,
    MopHumidity,
)
from .models import CommandResponse, DeviceInfo, MapData, MapDisplayData, NarwalState
from .protocol import (
    PROTOBUF_FIELD5_TAG,
    NarwalMessage,
    ProtocolError,
    build_frame,
    parse_frame,
)

_LOGGER = logging.getLogger(__name__)


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
    ) -> None:
        self.host = host
        self.port = port
        self.device_id = device_id
        self.url = f"ws://{host}:{port}"
        self.topic_prefix = DEFAULT_TOPIC_PREFIX  # updated by get_device_info()
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
        self._robot_awake = False  # True once we receive a broadcast
        self._last_broadcast_time: float = 0.0  # monotonic time of last broadcast
        # Queue for field5 command responses
        self._response_queue: asyncio.Queue[NarwalMessage] = asyncio.Queue()

    def _full_topic(self, short_topic: str) -> str:
        """Build the full topic path."""
        return f"{self.topic_prefix}/{self.device_id}/{short_topic}"

    @property
    def connected(self) -> bool:
        """Return True if the WebSocket is currently connected."""
        return self._ws is not None and self._connected.is_set()

    @property
    def robot_awake(self) -> bool:
        """Return True if the robot is actively broadcasting."""
        return self._robot_awake

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

        # Build wake frames with multiple topic prefixes to support all models.
        # The local WebSocket server may only accept commands with the correct
        # product key prefix. Since we don't know the model yet, try the default
        # prefix first, then a bare topic with no prefix.
        cmd = TOPIC_CMD_GET_DEVICE_INFO
        wake_frames = [
            build_frame(self._full_topic(cmd), b""),  # default prefix
            build_frame(f"//{cmd}", b""),  # no prefix, no device_id
        ]
        for frame in wake_frames:
            try:
                await self._ws.send(frame)
            except Exception as e:
                _LOGGER.warning("Failed to send wake command: %s", e)
        _LOGGER.debug("Sent discovery wake commands (device_id='%s')", self.device_id)

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
            except asyncio.TimeoutError:
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
                    raw_id = decoded.get("2", b"")
                    if isinstance(raw_id, bytes):
                        raw_id = raw_id.decode("utf-8", errors="replace").strip()
                    else:
                        raw_id = str(raw_id).strip()
                    if raw_id:
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
                    if parts[1]:
                        self.topic_prefix = f"/{parts[1]}"
                        _LOGGER.info("Topic prefix from broadcast: %s", self.topic_prefix)
                    self.device_id = parts[2]
                    _LOGGER.info("Discovered device_id from broadcast: %s", self.device_id)
                    return self.device_id

        raise NarwalCommandError(
            f"No response or broadcast within {timeout}s — check vacuum IP and power"
        )

    async def disconnect(self) -> None:
        """Disconnect from the vacuum and stop all tasks."""
        self._should_reconnect = False
        self._listener_active = False
        self._robot_awake = False
        self._connected.clear()

        for task in (self._heartbeat_task, self._keepalive_task, self._listen_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

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
            await self._response_queue.put(msg)
            return

        # Any broadcast means the robot is awake
        self._last_broadcast_time = time.monotonic()
        if not self._robot_awake:
            self._robot_awake = True
            _LOGGER.info("Robot is awake (received broadcast)")

        if self.on_message:
            self.on_message(msg)

        # Decode protobuf and update state based on topic
        short_topic = msg.short_topic
        try:
            decoded = self._decode_protobuf(msg.payload)
        except Exception:
            _LOGGER.debug("Failed to decode protobuf for topic %s", short_topic)
            return

        if short_topic == "status/working_status":
            self.state.update_from_working_status(decoded)
        elif short_topic == "status/robot_base_status":
            was_cleaning = self.state.is_cleaning
            self.state.update_from_base_status(decoded)
            # Clear stale display_map when robot stops cleaning
            if was_cleaning and not self.state.is_cleaning:
                self.state.map_display_data = None
        elif short_topic == "upgrade/upgrade_status":
            self.state.update_from_upgrade_status(decoded)
        elif short_topic == "status/download_status":
            self.state.update_from_download_status(decoded)
        elif short_topic == "map/display_map":
            self.state.map_display_data = MapDisplayData.from_broadcast(decoded)

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

    # All broadcast topics the robot can send — used for active_robot_publish
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

    def _build_wake_commands(self) -> list[tuple[str, bytes]]:
        """Build the sequence of wake commands to try.

        Returns list of (short_topic, payload) tuples. Mimics what the
        Narwal app sends when opened: notify_app_event → subscribe to all
        broadcast topics → heartbeat → status query.
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

        # 5. get_device_base_status — forces robot to process a command,
        #    response updates battery; may also trigger broadcast as side effect
        cmds.append((TOPIC_CMD_GET_BASE_STATUS, b""))

        # 6. get_device_info — lightweight command that always gets a response
        cmds.append((TOPIC_CMD_GET_DEVICE_INFO, b""))

        # 7. developer ping
        cmds.append((TOPIC_CMD_PING, b""))

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

    async def wake(self, timeout: float = WAKE_TIMEOUT) -> bool:
        """Attempt to wake the robot from sleep.

        Sends a burst of wake commands and waits for the robot to start
        broadcasting status messages. Returns True if robot woke up.

        Args:
            timeout: Maximum seconds to wait for the robot to respond.

        Returns:
            True if the robot is awake (received broadcasts), False otherwise.
        """
        if self._robot_awake:
            return True

        if not self.connected:
            raise NarwalConnectionError("Not connected to vacuum")

        _LOGGER.info("Attempting to wake robot...")

        deadline = asyncio.get_event_loop().time() + timeout
        attempt = 0

        while asyncio.get_event_loop().time() < deadline:
            attempt += 1
            _LOGGER.debug("Wake attempt %d", attempt)

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

    async def _keepalive_loop(self) -> None:
        """Periodically send wake/heartbeat commands to prevent robot from sleeping.

        Runs alongside the listener loop. Sends a lightweight heartbeat
        command every KEEPALIVE_INTERVAL seconds. If the robot stops
        broadcasting for BROADCAST_STALE_TIMEOUT seconds (goes back to
        sleep), resets _robot_awake and escalates to a full wake burst.
        """
        try:
            while self.connected:
                await asyncio.sleep(KEEPALIVE_INTERVAL)
                if not self.connected or not self._ws:
                    break

                # Check if broadcasts have gone stale (robot fell back asleep)
                if (
                    self._robot_awake
                    and self._last_broadcast_time > 0
                    and time.monotonic() - self._last_broadcast_time
                    > BROADCAST_STALE_TIMEOUT
                ):
                    _LOGGER.info(
                        "No broadcast for %.0fs — robot may have gone to sleep",
                        time.monotonic() - self._last_broadcast_time,
                    )
                    self._robot_awake = False

                if self._robot_awake:
                    # Robot is awake — send lightweight heartbeat
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
                else:
                    # Robot appears asleep — send full wake burst
                    _LOGGER.debug("Robot not awake, sending wake burst")
                    await self._send_wake_burst()

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

        Works both with and without start_listening() running. When the
        listener loop is active, responses arrive via the queue. Otherwise,
        this method directly reads from the WebSocket.

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

        # Drain any stale responses
        while not self._response_queue.empty():
            try:
                self._response_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

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
            except asyncio.TimeoutError:
                raise NarwalCommandError(
                    f"No response for command '{short_topic}' within {timeout}s"
                ) from None
        else:
            # No listener — read directly from websocket
            msg = await self._wait_for_field5_response(timeout)

        # Decode response
        try:
            decoded = self._decode_protobuf(msg.payload)
        except Exception:
            decoded = {}

        # Field 1 is a result code for action commands (int),
        # but data for some query commands (string/bytes).
        raw_field1 = decoded.get("1", 0)
        try:
            result_code = int(raw_field1)
        except (ValueError, TypeError):
            result_code = 0  # treat non-int field 1 as success (data response)

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
            except asyncio.TimeoutError:
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
                self.state.update_from_working_status(decoded)
            elif short_topic == "status/robot_base_status":
                self.state.update_from_base_status(decoded)
            elif short_topic == "upgrade/upgrade_status":
                self.state.update_from_upgrade_status(decoded)
            elif short_topic == "status/download_status":
                self.state.update_from_download_status(decoded)
            elif short_topic == "map/display_map":
                self.state.map_display_data = MapDisplayData.from_broadcast(decoded)

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

    async def start(self) -> CommandResponse:
        """Start cleaning."""
        return await self.send_command(TOPIC_CMD_START_CLEAN)

    async def start_easy_clean(self) -> CommandResponse:
        """Start quick/easy clean."""
        return await self.send_command(TOPIC_CMD_EASY_CLEAN)

    async def pause(self) -> CommandResponse:
        """Pause current task."""
        return await self.send_command(TOPIC_CMD_PAUSE)

    async def resume(self) -> CommandResponse:
        """Resume paused task."""
        return await self.send_command(TOPIC_CMD_RESUME)

    async def stop(self) -> CommandResponse:
        """Force-stop current task."""
        return await self.send_command(TOPIC_CMD_FORCE_END)

    async def cancel(self) -> CommandResponse:
        """Cancel current task."""
        return await self.send_command(TOPIC_CMD_CANCEL)

    async def return_to_base(self) -> CommandResponse:
        """Return to charging dock."""
        return await self.send_command(TOPIC_CMD_RECALL)

    async def set_fan_speed(self, level: FanLevel | int) -> CommandResponse:
        """Set suction fan speed.

        Args:
            level: FanLevel enum or int (0=quiet, 1=normal, 2=strong, 3=max).
        """
        payload = b"\x08" + bytes([int(level) & 0x7F])
        return await self.send_command(TOPIC_CMD_SET_FAN_LEVEL, payload)

    async def set_mop_humidity(self, level: MopHumidity | int) -> CommandResponse:
        """Set mop wetness level.

        Args:
            level: MopHumidity enum or int (0=dry, 1=normal, 2=wet).
        """
        payload = b"\x08" + bytes([int(level) & 0x7F])
        return await self.send_command(TOPIC_CMD_SET_MOP_HUMIDITY, payload)

    async def wash_mop(self) -> CommandResponse:
        """Wash the mop pads at the station."""
        return await self.send_command(TOPIC_CMD_WASH_MOP)

    async def dry_mop(self) -> CommandResponse:
        """Dry the mop pads at the station."""
        return await self.send_command(TOPIC_CMD_DRY_MOP)

    async def empty_dustbin(self) -> CommandResponse:
        """Empty the dustbin at the station."""
        return await self.send_command(TOPIC_CMD_DUST_GATHERING)

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
        status_data = resp.data.get("2", {})
        if status_data:
            _LOGGER.debug(
                "get_status response (full=%s): field3=%r, field2=%r",
                full_update,
                status_data.get("3") if isinstance(status_data, dict) else None,
                status_data.get("2") if isinstance(status_data, dict) else None,
            )
            if full_update:
                self.state.update_from_base_status(status_data)
            else:
                self.state.update_battery_from_base_status(status_data)
        else:
            _LOGGER.debug("get_status response has no field 2; keys: %s", list(resp.data.keys()))
        return resp

    async def get_current_task(self) -> CommandResponse:
        """Query the current clean task."""
        return await self.send_command(TOPIC_CMD_GET_CURRENT_TASK)

    async def get_map(self) -> MapData:
        """Download the full map data."""
        resp = await self.send_command(TOPIC_CMD_GET_MAP, timeout=15.0)
        map_data = MapData.from_response(resp.data)
        self.state.map_data = map_data
        return map_data

    async def get_all_maps(self) -> CommandResponse:
        """Download all saved/reduced maps."""
        return await self.send_command(TOPIC_CMD_GET_ALL_MAPS, timeout=15.0)
