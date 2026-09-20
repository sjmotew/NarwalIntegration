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
    "iSuVlI1If2",  # Narwal Flow 2 alternate key (confirmed working locally)
    # Third Flow 2 key, reported by @DeNo64 (#81) on firmware v01.09.08.00.
    # Fully working over the local WebSocket -- 28 entities -- but the entry was
    # left unnamed until the key was added here.
    "mkbqaprvrb",  # Narwal Flow 2 (confirmed working via local WebSocket)
    "DrzDKQ0MU8",   # CX4  — Freo Z10 Ultra (confirmed by @irekkl-maker)
    # AX26 — sold as both "Freo Z10 Turbo" (@romedtino, #40) and "Freo Z10 Pro"
    # (@shin906710, #70); same key, same FW v01.02.00.15, so one platform.
    "qV6BujoYLz",   # AX26 — Freo Z10 Pro / Turbo (confirmed local WebSocket)
    # CX7 answers addressed local queries but emits no broadcasts.
    "hEA7OEshlx",   # CX7/J5 — Freo Z Ultra (confirmed local WebSocket)
    # Freo 20 -- confirmed by @kvkessler (#97) via auto-detect, fw v01.00.35.03.
    "fjhpiem4ba",   # Freo 20 (confirmed local WebSocket, broadcasts)
    "BYWBPqSxeC",   # Previously attributed to CX7; retained for discovery coverage
    # JX key contributed by @ciaoly (#42); local WebSocket confirmed by
    # @Smiorld 2026-08-30 — port 9002 open, auto-detect connects, map loads.
    "CGjuB6dzq7",   # JX — Narwal JX (confirmed local WebSocket)
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
    "88OLXLpkjT",   # BX4  (note: APK also has 3rIGshGNAj — may vary by FW revision)
    "3rIGshGNAj",   # BX4/Y1 alternate key (APK, contributed by @northwestsupra)
    "7sSZZ4XfTI",   # CX2
    "OlkUn3oUCu",   # CX3 / CX3Pure
    "mvlduyye85",   # X30
    "pcbfh2ldvx",   # X31
    "EHf6cRNRGT",   # J4 / J4Pure (APK, contributed by @northwestsupra)
    "6NjIDYxBXb",   # J4Lite (APK, contributed by @northwestsupra)
    "cUlfJN5JYP",   # Unknown model (APK, contributed by @northwestsupra)
]

# --- Status topics (robot → client, field 4 / 0x22 frames) ---
TOPIC_WORKING_STATUS = "status/working_status"
TOPIC_ROBOT_BASE_STATUS = "status/robot_base_status"
TOPIC_UPGRADE_STATUS = "upgrade/upgrade_status"
TOPIC_DOWNLOAD_STATUS = "status/download_status"
TOPIC_DISPLAY_MAP = "map/display_map"
TOPIC_TIMELINE_STATUS = "status/time_line_status"

# --- Command topics (client → robot, confirmed working) ---
# Common
TOPIC_CMD_YELL = "common/yell"
TOPIC_CMD_REBOOT = "common/reboot"
TOPIC_CMD_SHUTDOWN = "common/shutdown"
TOPIC_CMD_GET_DEVICE_INFO = "common/get_device_info"
TOPIC_CMD_GET_ROBOT_INFO = "developer/get_robot_info"
TOPIC_CMD_GET_FEATURE_LIST = "common/get_feature_list"
TOPIC_CMD_GET_BASE_STATUS = "status/get_device_base_status"
TOPIC_CMD_GET_CONSUMABLE_INFO = "consumable/get_consumable_info"

# Task control
TOPIC_CMD_PAUSE = "task/pause"
TOPIC_CMD_RESUME = "task/resume"
TOPIC_CMD_FORCE_END = "task/force_end"
TOPIC_CMD_CANCEL = "task/cancel"

# Supply/dock
TOPIC_CMD_AMBIENT_LIGHT_CTRL = "supply/ambient_light_ctrl"
TOPIC_CMD_RECALL = "supply/recall"
TOPIC_CMD_WASH_MOP = "supply/wash_mop"
TOPIC_CMD_WASH_MOP_BY_ROBOT_STATUS = "supply/wash_mop_by_robot_status"
TOPIC_CMD_DRY_MOP = "supply/dry_mop"
TOPIC_CMD_DRY_DUST_BAG = "supply/dry_dust_bag"
TOPIC_CMD_DRY_STATION_BAG = "supply/dry_station_bag"
TOPIC_CMD_DUST_GATHERING = "supply/dust_gathering"

# Cleaning (Pita protocol — correct for AX12)
TOPIC_CMD_PLAN_START = "clean/plan/start"  # whole-house clean (empty payload)
TOPIC_CMD_CLEAN_TASK = "clean/start_clean"  # room/zone CleanTask; only works docked
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
TOPIC_CMD_GET_DEBUG_IMAGE = "developer/get_robot_debug_image"  # cleartext carpet/planning PNGs

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
    NOT_READY = 4  # clean/start_clean while not docked (robot in STANDBY)
    APPLIED = 6  # observed on ambient light commands after the dock light changes


class AmbientLightCtrlType(IntEnum):
    """Base station ambient light mode."""

    OFF = 0
    NIGHT_LIGHT = 1
    WINTER_WARMTH = 2
    PURPLE_LIGHT = 3


class WorkingStatus(IntEnum):
    """Robot working state from robot_base_status field 3 → sub-field 1.

    These values are EMPIRICAL and intentionally do NOT match the app's compiled
    RobotTaskStatus.TaskType enum (sub-field 1's declared type): on this firmware
    the robot reports e.g. 14=charged where TaskType 14=WASH_AND_DRY_MOP. Trust
    the live-observed mapping below, not re/ENUMS.md, for this field (and f47).

    Values confirmed via live WebSocket monitoring:
      1  = STANDBY (idle, transition state between cleaning and docked)
      2  = DOCKED_V2 (on dock; confirmed v01.07.23.00 while charging at 10-36%)
      3  = CLEANING_V2 (active cleaning; observed on newer Flow 2 firmware)
      4  = CLEANING (plan-based start; also stays 4 while returning to dock on older FW)
      5  = CLEANING_ALT (observed live: robot was physically stuck when reporting 5)
      7  = REMAPPING (live 2026-07-09: robot exploring/rebuilding the map; camera
           active — developer/take_picture is accepted in this state)
      10 = DOCKED (on dock, charging)
      14 = CHARGED (on dock, fully charged)
      17 = CUSTOM_CLEANING (custom per-room clean; live-validated on Flow 2)
      19 = TASK_COMPLETED (transitional: scheduled task finished, returning to base)

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
    DOCKED_V2 = 2     # on dock (v01.07.23.00+ — replaces DOCKED=10/CHARGED=14 from older FW)
    # UNCONFIRMED: reported as active cleaning on newer Flow 2 firmware in PR #63,
    # but no capture has been supplied and PROTOCOL.md has no entry for 3. Treated
    # as active because the #73 failure mode is showing "docked" during a clean;
    # revisit if a capture shows 3 means something else (e.g. a self-test state).
    CLEANING_V2 = 3   # active cleaning on newer Flow 2 firmware
    CLEANING = 4      # active cleaning (stays 4 even while returning to dock)
    CLEANING_ALT = 5  # cleaning - seen when stuck; may indicate error/stuck state
    REMAPPING = 7     # mapping/exploration (live 2026-07-09); camera active, take_picture accepted
    DOCKED = 10       # on dock (does NOT reliably indicate charging vs charged)
    CHARGED = 14      # on dock (reported before 100% — use battery_level for charge state)
    CUSTOM_CLEANING = 17  # active custom per-room clean on Flow 2
    TASK_COMPLETED = 19  # transitional: task finished, robot returning to base (#41)
    # PLACEHOLDER: error state value not yet observed live.
    # Trigger a real error (e.g., pick up robot mid-clean) to discover the value.
    ERROR = 99


ACTIVE_CLEANING_STATUSES = frozenset(
    {
        WorkingStatus.CLEANING_V2,
        WorkingStatus.CLEANING,
        WorkingStatus.CLEANING_ALT,
        WorkingStatus.CUSTOM_CLEANING,
        WorkingStatus.REMAPPING,
    }
)


class FanLevel(IntEnum):
    """CleanParam suction level.

    Live clean/set_fan_level carries a SweepFanLevel instead: identical ints
    0-4, but it has no SUPER, so the app maps SUPER->STRONG on that path.
    """

    UNSPECIFIED = 0
    MUTE = 1
    NORMAL = 2
    STRONG = 3
    DEEP = 4
    SUPER = 5


def fan_level_for_live_command(level: FanLevel | int) -> FanLevel:
    """Return the closest level supported by clean/set_fan_level."""
    requested = FanLevel(int(level))
    return FanLevel.DEEP if requested is FanLevel.SUPER else requested


class MopHumidity(IntEnum):
    """Water volume shared by CleanParam tag 4 and live set_mop_humidity."""

    UNSPECIFIED = 0
    DRY = 1
    NORMAL = 2
    WET = 3


class MopStrengthLevel(IntEnum):
    """Mop scrub intensity (CleanParam tag 3)."""

    UNSPECIFIED = 0
    NORMAL = 1
    HIGH = 2


class CleaningRoute(IntEnum):
    """Cleaning route overlap level (CleanParam tag 8)."""

    STANDARD = 1
    METICULOUS = 2


class WorkMode(IntEnum):
    """Clean work mode selected by the app's robot_work_mode_* setting.

    Uniform jobs also use this value as CleanTask.taskType. Custom mixed-mode
    jobs use taskType 6 while each item's CleanParam.mode carries its work mode.
    """

    VACUUM = 1
    MOP = 2
    VACUUM_THEN_MOP = 3
    VACUUM_AND_MOP = 4


# robot_base_status field numbers
class BaseStatusField(IntEnum):
    """Field numbers in the robot_base_status (RobotBaseStatus) message.

    Names from the decompiled BuilderInfo, several live-validated on dock.
    Field 2 is float32 (PbFieldType 0x100), e.g. 1120403456 → 100.0.
    Most state fields (5,11,12,14,15,20-24,26,28,29,31,33,39,40,42,47,49,50)
    are enums whose value→label tables are not yet decoded.
    """

    ERROR_CODE = 1  # repeated ErrorCode; empty when no fault
    BATTERY_LEVEL = 2  # batteryPercentage, float32
    ROBOT_TASK_STATUS = 3  # nested task-status message
    BINDED_UUID = 13  # bound account/device UUID (string)
    CLEAN_WATER_TANK_STATE = 23  # enum
    SEWAGE_TANK_STATE = 24  # enum
    DEVICE_STATUS_CODE_LIST = 25
    FAN_LEVEL = 26  # active suction (FanLevel enum)
    MOP_HUMIDITY = 29  # active water level (MopHumidity enum)
    STATION_BAG_HEALTH_SCORE = 35  # float32, %
    STATION_BAG_HEALTH_RESET_TIME = 36  # epoch
    CURING_AGENT_CONSUMPTION_PERCENT = 38
    HEAVY_DETERGENT_REMAIN_PERCENT = 41
    CHARGING_STATUS = 47  # canonical charging state (enum)


# upgrade_status field numbers (OTAUpgradeStatus)
class UpgradeStatusField(IntEnum):
    """Field numbers in the upgrade_status (OTAUpgradeStatus) message.

    Names from the decompiled BuilderInfo: 1 type, 2 status, 3 progress,
    4 stage, 5 errorCode, 6 detailErrorCode, 7 currentVersion, 8 targetVersion.
    """

    STATUS = 2
    PROGRESS = 3
    STAGE = 4
    ERROR_CODE = 5
    CURRENT_FIRMWARE = 7
    TARGET_FIRMWARE = 8


# working_status field numbers
class WorkingStatusField(IntEnum):
    """Field numbers in the working_status protobuf message.

    Names from the decompiled WorkingStatus proto BuilderInfo:
      1  = workingProgress (float32, PbFieldType 0x100)
      2  = coveredArea (float32, PbFieldType 0x100) — area cleaned this session, m²
      3  = timeConsuming (seconds) — session elapsed time
      4  = remainedTime (seconds)
      6  = cleaningZoneId
      8..17 = station drying/sterilization/dust-bag timers (seconds); the
              cumulative "total*" counters (9/11/13/15/17) stay constant while
              idle. Field 13 = totalDryStationBagTime (18000 = 5h) — earlier
              misread as cleaning area because 18000/10000 looked like 1.8 m².
    """

    PROGRESS = 1  # workingProgress (float32, 0..1)
    AREA = 2  # coveredArea (float32) — m²
    ELAPSED_TIME = 3  # timeConsuming — session elapsed seconds
    REMAINING_TIME = 4  # remainedTime — seconds
