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
    KNOWN_PRODUCT_KEYS,
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
    TOPIC_CMD_TAKE_PICTURE,
    TOPIC_CMD_GET_DEBUG_IMAGE,
    TOPIC_CMD_SET_LED,
    TOPIC_CMD_WASH_MOP,
    TOPIC_CMD_YELL,
    DEFAULT_TOPIC_PREFIX,
    WAKE_TIMEOUT,
    CommandResult,
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
        topic_prefix: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.device_id = device_id
        self.url = f"ws://{host}:{port}"
        self.topic_prefix = topic_prefix or DEFAULT_TOPIC_PREFIX
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
        self._last_display_map_time: float = 0.0  # monotonic time of last display_map
        # Queue for field5 command responses
        self._response_queue: asyncio.Queue[NarwalMessage] = asyncio.Queue()
        # Lock to prevent concurrent send_command calls from racing on the queue
        self._command_lock = asyncio.Lock()

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
                data = await asyncio.wait_for(self._ws.recv(), timeout=0.05)
                drained += 1
            except asyncio.TimeoutError:
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
                    # Immediate wake burst on (re)connect — the fresh TCP
                    # connection may trigger the robot's deep-sleep wake
                    # interrupt, but only if we send commands before it
                    # expires.  Don't wait for the keepalive loop's first
                    # tick (15s delay would be too late).
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
            _LOGGER.debug("Field5 response routed to queue: %s", msg.short_topic)
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
        _LOGGER.debug("Broadcast topic: %s (tag=0x%02x)", short_topic, msg.field_tag)
        try:
            decoded = self._decode_protobuf(msg.payload)
        except Exception:
            _LOGGER.debug("Failed to decode protobuf for topic %s", short_topic)
            return

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
            self._last_display_map_time = time.monotonic()
            _LOGGER.debug(
                "display_map received: robot=(%.2f, %.2f) ts=%d",
                self.state.map_display_data.robot_x,
                self.state.map_display_data.robot_y,
                self.state.map_display_data.timestamp,
            )
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
        "status/point_navi_plan_traj",
        "developer/planning_debug_info",
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
            True if the robot is awake (received broadcasts), False otherwise.
        """
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
        # Start at 0 so the first keepalive tick sends the subscription
        # immediately. This handles the case where the robot is already
        # broadcasting (e.g. mid-cleaning) and wake() skips the burst.
        last_resub_time = 0.0
        consecutive_wake_failures = 0
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
                    consecutive_wake_failures = 0

                if self._robot_awake:
                    consecutive_wake_failures = 0
                    # Re-subscribe to topics before the subscription expires
                    if time.monotonic() - last_resub_time > self._TOPIC_RESUB_INTERVAL:
                        try:
                            payload = self._build_topic_subscription(
                                self._TOPIC_SUB_DURATION
                            )
                            frame = build_frame(
                                self._full_topic(TOPIC_CMD_ACTIVE_ROBOT), payload
                            )
                            await self._ws.send(frame)
                            last_resub_time = time.monotonic()
                            _LOGGER.debug("Topic subscription renewed")
                        except Exception:
                            _LOGGER.debug("Topic re-subscribe failed")

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
                else:
                    # Robot appears asleep — send full wake burst
                    # (wake burst includes topic subscription)
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

    # Legacy clean task payload — works on Flow firmware < v01.07.22.
    # Structure: {1: {2: {}, 5: {1: {1: 3, 2: 2, 3: 1}, 5: {}}}}
    #   field 5.1.1 = suction level (3=max)
    #   field 5.1.2 = mop humidity (2=wet)
    #   field 5.1.3 = passes (1=single)
    # On firmware v01.07.22+ (Flow), this returns NOT_APPLICABLE and
    # start() falls back to the v2 room-list schema. See issue #36.
    _DEFAULT_CLEAN_PAYLOAD = bytes.fromhex("0a0e12002a0a0a060803100218012a00")

    async def start(self, **kwargs) -> CommandResponse:
        """Start whole-house cleaning.

        Tries the legacy minimal payload first (works on Flow firmware
        < v01.07.22). If the robot returns NOT_APPLICABLE — observed on
        firmware v01.07.22.00 in issue #36 — falls back to the v2 schema
        that includes an explicit room list, mirroring what the Narwal
        app sends on newer firmware.

        The v2 fallback requires the map to be loaded (get_map called)
        so we know which rooms to include.
        """
        resp = await self.send_command(
            TOPIC_CMD_START_CLEAN,
            payload=self._DEFAULT_CLEAN_PAYLOAD,
            timeout=10.0,
        )
        if resp.result_code != CommandResult.NOT_APPLICABLE:
            return resp

        # Legacy payload rejected — likely newer firmware. Need the room
        # list from the cached map to build a v2 payload.
        if not (self.state.map_data and self.state.map_data.rooms):
            _LOGGER.warning(
                "start() got NOT_APPLICABLE and no map rooms cached; "
                "cannot build v2 payload. Call get_map() first."
            )
            return resp

        room_ids = [r.room_id for r in self.state.map_data.rooms if r.room_id]
        if not room_ids:
            _LOGGER.warning(
                "start() got NOT_APPLICABLE but cached map has 0 rooms with IDs"
            )
            return resp

        _LOGGER.info(
            "start(): legacy payload rejected, retrying with v2 schema (%d rooms)",
            len(room_ids),
        )
        payload = self._build_clean_payload_v2(room_ids)
        return await self.send_command(
            TOPIC_CMD_START_CLEAN, payload=payload, timeout=10.0,
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
        Each room entry uses a nested room_id (different from the flat
        schema in _build_room_clean_payload used for room-targeted cleans
        on older firmware):

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
            Encoded protobuf bytes for clean/plan/start.
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

    def _build_room_clean_payload(self, room_ids: list[int]) -> bytes:
        """Build CleanTask protobuf with per-room clean params in field 1.2.

        Each room entry in field 1.2 requires full MapCleanParamInfo fields
        (from APK proto analysis):
          field 1: roomId (uint32)
          field 2: cleanMode (int32) — 0=sweep, 1=mop, 2=sweep+mop
          field 3: cleanTimes (int32) — number of passes
          field 6: sweepMode (int32) — suction level (3=max)
          field 7: mopMode (int32) — mop humidity (2=wet)

        A bare roomId without clean params is silently ignored by the robot.

        Args:
            room_ids: List of room IDs from RoomInfo.room_id.

        Returns:
            Encoded protobuf bytes for clean/plan/start.
        """
        if not room_ids:
            return self._DEFAULT_CLEAN_PAYLOAD

        import blackboxprotobuf

        # Build per-room entries with default clean settings
        room_entries = []
        for rid in room_ids:
            room_entries.append({
                "1": rid,       # roomId
                "2": 2,         # cleanMode = sweep+mop
                "3": 1,         # cleanTimes = 1 pass
                "6": 3,         # sweepMode = max suction
                "7": 2,         # mopMode = wet
            })

        room_typedef = {
            "type": "message",
            "seen_repeated": True,
            "message_typedef": {
                "1": {"type": "uint"},
                "2": {"type": "int"},
                "3": {"type": "int"},
                "6": {"type": "int"},
                "7": {"type": "int"},
            }
        }

        # Single room: field 1.2 is a message; multiple: repeated message
        field_2_value = room_entries[0] if len(room_entries) == 1 else room_entries

        msg = {
            "1": {
                "2": field_2_value,
                "5": {
                    "1": {"1": 3, "2": 2, "3": 1},
                    "5": {}
                }
            }
        }
        typedef = {
            "1": {
                "type": "message",
                "message_typedef": {
                    "2": room_typedef,
                    "5": {
                        "type": "message",
                        "message_typedef": {
                            "1": {
                                "type": "message",
                                "message_typedef": {
                                    "1": {"type": "int"},
                                    "2": {"type": "int"},
                                    "3": {"type": "int"}
                                }
                            },
                            "5": {"type": "message", "message_typedef": {}}
                        }
                    }
                }
            }
        }
        return blackboxprotobuf.encode_message(msg, typedef)

    async def start_rooms(
        self, room_ids: list[int],
    ) -> CommandResponse:
        """Start room-specific cleaning.

        Sends clean/plan/start with the user-selected rooms. Tries the v2
        nested-room schema first (required by firmware v01.07.22+, and fixes
        the ack-but-ignore behavior in #37 where legacy schema returns
        SUCCESS but the robot runs the app shortcut instead of HA-selected
        rooms). Falls back to legacy flat-room schema on NOT_APPLICABLE
        for older firmware.

        Args:
            room_ids: List of room IDs from RoomInfo.room_id.

        Returns:
            CommandResponse with result code from whichever schema landed.
        """
        if not room_ids:
            return await self.start()

        payload_v2 = self._build_clean_payload_v2(room_ids)
        resp = await self.send_command(
            TOPIC_CMD_START_CLEAN, payload=payload_v2, timeout=10.0,
        )
        if resp.result_code != CommandResult.NOT_APPLICABLE:
            return resp

        # v2 rejected — try legacy flat-room schema (older firmware)
        _LOGGER.info(
            "start_rooms(): v2 payload rejected, retrying with legacy schema (%d rooms)",
            len(room_ids),
        )
        payload_legacy = self._build_room_clean_payload(room_ids)
        return await self.send_command(
            TOPIC_CMD_START_CLEAN, payload=payload_legacy, timeout=10.0,
        )

    async def start_easy_clean(self) -> CommandResponse:
        """Start quick/easy clean."""
        return await self.send_command(TOPIC_CMD_EASY_CLEAN)

    async def pause(self) -> CommandResponse:
        """Pause current task."""
        return await self.send_command(TOPIC_CMD_PAUSE)

    async def resume(self, timeout: float = COMMAND_RESPONSE_TIMEOUT) -> CommandResponse:
        """Resume paused task."""
        return await self.send_command(TOPIC_CMD_RESUME, timeout=timeout)

    async def stop(self, timeout: float = 15.0) -> CommandResponse:
        """Force-stop current task.

        Note: force_end is slow — robot physically stops before responding.
        Previous testing shows 10-15s response times from CLEANING state.
        """
        return await self.send_command(TOPIC_CMD_FORCE_END, timeout=timeout)

    async def cancel(self) -> CommandResponse:
        """Cancel current task."""
        return await self.send_command(TOPIC_CMD_CANCEL)

    async def return_to_base(self, timeout: float = COMMAND_RESPONSE_TIMEOUT) -> CommandResponse:
        """Return to charging dock."""
        return await self.send_command(TOPIC_CMD_RECALL, timeout=timeout)

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
        product_key = ""
        if self.state.device_info and self.state.device_info.product_key:
            product_key = self.state.device_info.product_key
        map_data = MapData.from_response(resp.data, product_key=product_key)
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
