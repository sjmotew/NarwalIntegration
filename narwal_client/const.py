"""Protocol constants, enums, and field mappings for Narwal vacuum."""

from enum import IntEnum

# Connection defaults
DEFAULT_PORT = 9002

# Frame structure
FRAME_TYPE_BYTE = 0x01
PROTOBUF_FIELD_TAG = 0x22  # field 4, wire type 2 (broadcasts/requests)
TOPIC_LENGTH_OFFSET = 3
TOPIC_DATA_OFFSET = 4

# Topic addressing — prefix is the Narwal Flow (AX12) product key
TOPIC_PREFIX = "/QoEsI5qYXO"

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

# Reconnection parameters
RECONNECT_INITIAL_DELAY = 1.0  # seconds
RECONNECT_MAX_DELAY = 300.0  # 5 minutes
RECONNECT_BACKOFF_FACTOR = 2.0
RECONNECT_COOLDOWN = 10.0  # wait after robot disconnects on invalid message

# Heartbeat
HEARTBEAT_INTERVAL = 30.0  # seconds

# Command response timeout
COMMAND_RESPONSE_TIMEOUT = 5.0  # seconds

# Status broadcast interval
STATUS_BROADCAST_INTERVAL = 1.5  # seconds (when robot is awake)


class CommandResult(IntEnum):
    """Response code from command field 1."""

    SUCCESS = 1
    NOT_APPLICABLE = 2  # e.g., set_fan_level when not cleaning
    CONFLICT = 3  # e.g., recall when already recalling


class WorkingStatus(IntEnum):
    """Robot working state from robot_base_status field 3 → sub-field 1.

    Values confirmed via live WebSocket monitoring:
      1  = STANDBY (idle, off dock)
      5  = CLEANING (active cleaning session)
      10 = DOCKED (on dock, charged/charging)
    Values not yet confirmed (need more captures):
      returning, paused, error states
    """

    UNKNOWN = 0
    STANDBY = 1       # idle, off dock (also on dock at 100% battery)
    CLEANING = 4      # active cleaning (plan-based start)
    CLEANING_ALT = 5  # active cleaning (seen in some modes)
    DOCKED = 10       # on dock, actively charging or returning
    # Field 3 sub-field 2 = 1 means PAUSED (overlay on CLEANING state)
    # Field 3 sub-field 10 = dock sub-state (1=docked, 2=docking)


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
    """Field numbers in the robot_base_status protobuf message."""

    MODE_STATE = 3
    SESSION_ID = 13
    SENSOR_DATA = 25
    TIMESTAMP = 36
    BATTERY_PERCENT = 38
    BATTERY_CAPACITY = 41


# upgrade_status field numbers
class UpgradeStatusField(IntEnum):
    """Field numbers in the upgrade_status protobuf message."""

    STATUS_CODE = 4
    CURRENT_FIRMWARE = 7
    TARGET_FIRMWARE = 8


# working_status field numbers
class WorkingStatusField(IntEnum):
    """Field numbers in the working_status protobuf message."""

    ELAPSED_TIME = 3  # current session elapsed seconds (NOT state!)
    AREA = 13  # cm² (may be cumulative)
    CUMULATIVE_TIME = 15  # seconds (may be cumulative)
