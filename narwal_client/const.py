"""Protocol constants, enums, and field mappings for Narwal vacuum."""

from enum import IntEnum

# Connection defaults
DEFAULT_PORT = 9002

# Frame structure
FRAME_TYPE_BYTE = 0x01
PROTOBUF_FIELD_TAG = 0x22  # field 4, wire type 2 (broadcasts/requests)
TOPIC_LENGTH_OFFSET = 3
TOPIC_DATA_OFFSET = 4

# Default topic prefix — Narwal Flow (AX12) product key.
# Overridden at runtime by NarwalClient once get_device_info returns
# the actual product_key for the connected device.
DEFAULT_TOPIC_PREFIX = "/QoEsI5qYXO"

# Known product keys for multi-model discovery.
# During wake/discovery, the client cycles through these prefixes until
# the robot responds. Once it does, the correct prefix is locked in.
# Order: confirmed working models first, then unverified keys.
KNOWN_PRODUCT_KEYS = [
    # Confirmed working (local WebSocket)
    "QoEsI5qYXO",  # AX12 — Narwal Flow (primary, confirmed)
    "QxMSPG6VSO",  # Narwal Flow 2 (confirmed working via local WebSocket)
    "DrzDKQ0MU8",   # CX4  — Freo Z10 Ultra (confirmed by @irekkl-maker)
    # Confirmed cloud-only (port 9002 open but no local broadcasts)
    "BYWBPqSxeC",   # CX7  — Freo Z Ultra (cloud-only, confirmed by @gabrielozcomidi)
    # Confirmed cloud-only (ZeroMQ port 6789, no WebSocket)
    "LnugwMG9ss",   # AX18 — Freo X Ultra (cloud-only, confirmed by @ManivannanBA)
    "5OMbqk58Sc",   # AX19 — Freo X Ultra
    # From APK analysis (unverified — model compatibility unknown)
    "tPQJmoIbEC",   # AX6  (APK, contributed by @northwestsupra)
    "HgArZ7KuJL",   # AX7  (APK, contributed by @northwestsupra)
    "Uuug39n0fD",   # AX8  (APK, contributed by @northwestsupra)
    "CNbforyZWI",   # AX15 — Freo X10 Pro (confirmed by @jlowen07)
    "E9Q8aDzUbp",   # AX17
    "jI5rHi4mKa",   # AX24
    "UuTSLsMce4",   # AX25
    "qV6BujoYLz",   # AX26
    "88OLXLpkjT",   # BX4  (note: APK also has 3rIGshGNAj — may vary by FW revision)
    "3rIGshGNAj",   # BX4/Y1 alternate key (APK, contributed by @northwestsupra)
    "7sSZZ4XfTI",   # CX2
    "OlkUn3oUCu",   # CX3 / CX3Pure
    "mvlduyye85",   # X30
    "pcbfh2ldvx",   # X31
    "EHf6cRNRGT",   # J4 / J4Pure (APK, contributed by @northwestsupra)
    "6NjIDYxBXb",   # J4Lite (APK, contributed by @northwestsupra)
    "hEA7OEshlx",   # J5  (APK, contributed by @northwestsupra)
    "cUlfJN5JYP",   # Unknown model (APK, contributed by @northwestsupra)
]

# --- Status topics (robot → client, field 4 / 0x22 frames) ---
TOPIC_WORKING_STATUS = "status/working_status"
TOPIC_ROBOT_BASE_STATUS = "status/robot_base_status"
TOPIC_UPGRADE_STATUS = "upgrade/upgrade_status"
TOPIC_DOWNLOAD_STATUS = "status/download_status"
TOPIC_DISPLAY_MAP = "map/display_map"
TOPIC_TIMELINE_STATUS = "status/time_line_status"
TOPIC_PLANNING_DEBUG = "developer/planning_debug_info"

# --- Command topics (client → robot, confirmed working) ---
# Common
TOPIC_CMD_YELL = "common/yell"
TOPIC_CMD_REBOOT = "common/reboot"
TOPIC_CMD_SHUTDOWN = "common/shutdown"
TOPIC_CMD_GET_DEVICE_INFO = "common/get_device_info"
TOPIC_CMD_GET_FEATURE_LIST = "common/get_feature_list"
TOPIC_CMD_GET_BASE_STATUS = "status/get_device_base_status"

# Task control
TOPIC_CMD_PAUSE = "task/pause"
TOPIC_CMD_RESUME = "task/resume"
TOPIC_CMD_FORCE_END = "task/force_end"
TOPIC_CMD_CANCEL = "task/cancel"

# Supply/dock
TOPIC_CMD_RECALL = "supply/recall"
TOPIC_CMD_WASH_MOP = "supply/wash_mop"
TOPIC_CMD_DRY_MOP = "supply/dry_mop"
TOPIC_CMD_DUST_GATHERING = "supply/dust_gathering"

# Cleaning (Pita protocol — correct for AX12)
TOPIC_CMD_START_CLEAN = "clean/plan/start"  # whole-house clean (empty payload)
TOPIC_CMD_START_CLEAN_LEGACY = "clean/start_clean"  # does NOT work from STANDBY
TOPIC_CMD_EASY_CLEAN = "clean/easy_clean/start"
TOPIC_CMD_SET_FAN_LEVEL = "clean/set_fan_level"
TOPIC_CMD_SET_MOP_HUMIDITY = "clean/set_mop_humidity"
TOPIC_CMD_GET_CURRENT_TASK = "clean/current_clean_task/get"

# Map
TOPIC_CMD_GET_MAP = "map/get_map"
TOPIC_CMD_GET_ALL_MAPS = "map/get_all_reduced_maps"

# Camera (developer commands)
TOPIC_CMD_TAKE_PICTURE = "developer/take_picture"
TOPIC_CMD_SET_LED = "developer/led_control"

# Wake / Keep-alive (from APK analysis — candidates for waking sleeping robot)
TOPIC_CMD_ACTIVE_ROBOT = "common/active_robot_publish"  # TopicDuration keepalive
TOPIC_CMD_APP_HEARTBEAT = "status/app_status_heartbeat"  # periodic app heartbeat
TOPIC_CMD_NOTIFY_APP_EVENT = "common/notify_app_event"  # "app opened" event
TOPIC_CMD_PING = "developer/ping"  # dev ping/pong

# Reconnection parameters
RECONNECT_INITIAL_DELAY = 1.0  # seconds
RECONNECT_MAX_DELAY = 300.0  # 5 minutes
RECONNECT_BACKOFF_FACTOR = 2.0
RECONNECT_COOLDOWN = 10.0  # wait after robot disconnects on invalid message

# Heartbeat
HEARTBEAT_INTERVAL = 30.0  # seconds

# Keep-alive interval — sends wake commands to prevent robot from sleeping
KEEPALIVE_INTERVAL = 15.0  # seconds

# How long without a broadcast before we consider the robot asleep again.
# Robot broadcasts every 1.5s when awake — 15s without one means it's asleep.
BROADCAST_STALE_TIMEOUT = 15.0  # seconds (~10x the 1.5s broadcast interval)

# Wake sequence timeout — how long to wait for robot to respond after wake burst
WAKE_TIMEOUT = 20.0  # seconds

# Command response timeout
COMMAND_RESPONSE_TIMEOUT = 5.0  # seconds

# display_map dropout detection — if robot is cleaning but no display_map
# arrives for this long, escalate to a full wake burst to recover the
# topic subscription (which can die during CLEANING_ALT / stuck episodes)
DISPLAY_MAP_DROPOUT_TIMEOUT = 30.0  # seconds
DISPLAY_MAP_RECOVERY_COOLDOWN = 45.0  # retry recovery every 45s if dropout persists

# Status broadcast interval
STATUS_BROADCAST_INTERVAL = 1.5  # seconds (when robot is awake)


class CommandResult(IntEnum):
    """Response code from command field 1."""

    SUCCESS = 1
    NOT_APPLICABLE = 2  # e.g., set_fan_level when not cleaning
    CONFLICT = 3  # e.g., recall when already recalling


class WorkingStatus(IntEnum):
    """Robot working state from robot_base_status field 3 → sub-field 1.

    Values confirmed via live WebSocket monitoring (2026-02-27):
      1  = STANDBY (idle, transition state between cleaning and docked)
      4  = CLEANING (plan-based start; also stays 4 while returning to dock)
      5  = CLEANING_ALT (observed live: robot was physically stuck when reporting 5)
      10 = DOCKED (on dock, charging)
      14 = CHARGED (on dock, fully charged)

    Field 3 sub-fields (confirmed live):
      3.2  = 1 means PAUSED (overlay on CLEANING state)
      3.7  = 1 means RETURNING to dock (robot navigating home)
      3.10 = dock sub-state (1=docked, 2=docking in progress)
      3.12 = dock activity (values 2, 6 observed when docked)

    Not yet confirmed:
      error states (WorkingStatus.ERROR placeholder = 99)
    """

    UNKNOWN = 0
    STANDBY = 1       # idle / transition state
    CLEANING = 4      # active cleaning (stays 4 even while returning to dock)
    CLEANING_ALT = 5  # cleaning — observed when robot was physically stuck; may indicate error/stuck state
    DOCKED = 10       # on dock (does NOT reliably indicate charging vs charged)
    CHARGED = 14      # on dock (reported before 100% — use battery_level for charge state)
    # PLACEHOLDER: error state value not yet observed live.
    # Trigger a real error (e.g., pick up robot mid-clean) to discover the value.
    ERROR = 99


class FanLevel(IntEnum):
    """Suction fan speed levels (SweepMode from APK)."""

    QUIET = 0
    NORMAL = 1
    STRONG = 2
    MAX = 3


class MopHumidity(IntEnum):
    """Mop wetness levels."""

    DRY = 0
    NORMAL = 1
    WET = 2


# robot_base_status field numbers
class BaseStatusField(IntEnum):
    """Field numbers in the robot_base_status protobuf message.

    Battery notes (confirmed via 35-min monitor capture, 2026-02-27):
      Field 2  = real-time battery level as IEEE 754 float32
                 (e.g. 1118175232 → 83.0%, matching app display ~84%)
      Field 38 = static battery health (always 100; design capacity, not SOC)
    """

    BATTERY_LEVEL = 2  # real-time SOC as float32 — CONFIRMED
    MODE_STATE = 3
    SESSION_ID = 13
    SENSOR_DATA = 25
    TIMESTAMP = 36
    BATTERY_HEALTH = 38  # static, always 100 (design capacity)
    BATTERY_CAPACITY = 41


# upgrade_status field numbers
class UpgradeStatusField(IntEnum):
    """Field numbers in the upgrade_status protobuf message."""

    STATUS_CODE = 4
    CURRENT_FIRMWARE = 7
    TARGET_FIRMWARE = 8


# working_status field numbers
class WorkingStatusField(IntEnum):
    """Field numbers in the working_status protobuf message.

    Confirmed via live test (2026-02-27):
      3  = current session elapsed seconds (confirmed: 2136→2159 over 35-min clean)
      13 = cleaning area in cm² (confirmed: 18000 = 1.8m²)
      15 = 600 during cleaning (possibly cumulative or constant)
      6  = 1 during cleaning (observed in plan-based clean; may vary by mode)
      10 = time since docked in seconds (post-dock only, counts up)
      11 = 2700 post-dock (unknown, constant)

    Also broadcast during cleaning:
      status/time_line_status — timeline/history data
      developer/planning_debug_info — navigation debug (collision count, stall count)
    """

    ELAPSED_TIME = 3  # current session elapsed seconds — CONFIRMED
    AREA = 13  # cm² — CONFIRMED (18000 = 1.8m²)
    CUMULATIVE_TIME = 15  # 600 during cleaning (purpose uncertain)
    TIME_SINCE_DOCKED = 10  # seconds since docked (post-dock only)
