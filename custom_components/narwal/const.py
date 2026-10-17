"""Constants for the Narwal vacuum integration."""

from homeassistant.const import Platform

from .narwal_client import (
    AmbientLightCtrlType,
    FanLevel,
    MopHumidity,
    MopStrengthLevel,
    WorkMode,
)

DOMAIN = "narwal"
DEFAULT_PORT = 9002
MANUFACTURER = "Narwal"
MODEL = "Flow (AX12)"

# Model selector for config flow.
# Keys are user-facing labels; values are product key prefixes.
# "auto" cycles all known keys during discovery (slower, fallback).
NARWAL_MODELS: dict[str, str] = {
    "Narwal Flow": "QoEsI5qYXO",
    "Narwal Flow 2": "QxMSPG6VSO",
    "Narwal Freo Z10 Ultra": "DrzDKQ0MU8",
    # AX26 ships under two marketing names on identical firmware (v01.02.00.15):
    # "Z10 Turbo" (@romedtino, #40) and "Z10 Pro" (@shin906710, #70), same product_key.
    "Narwal Freo Z10 Pro / Turbo": "qV6BujoYLz",
    "Narwal Freo X10 Pro": "CNbforyZWI",
    "Narwal Freo Z Ultra (CX7)": "hEA7OEshlx",
    "Narwal JX": "CGjuB6dzq7",
    # Confirmed working by @kvkessler (#97) as "Unknown (fjhpiem4ba)" on
    # firmware v01.00.35.03 -- map streaming, current room, dock sensors all
    # live. A different key from the JX, so the two are separate platforms (#42).
    "Narwal Freo 20": "fjhpiem4ba",
    # Freo 20 Edge (SKU YJCC041), confirmed by @cwisor (#109) as
    # "Unknown (ulonq49mm1)" on firmware v01.06.10.48 -- broadcasts, map
    # streaming and a room clean all worked. A different key from the plain
    # Freo 20, so it is its own selector entry rather than an alias.
    "Narwal Freo 20 Edge": "ulonq49mm1",
    "Other / Auto-detect": "auto",
}

# Label pre-selected in the model selector. Discovery carries no model
# information, so any specific model shown there is a guess presented as a
# fact; auto-detect reads the real key off the robot and names the entry from
# that (#81).
MODEL_AUTO_LABEL = "Other / Auto-detect"

# Product keys that belong to a model already in the selector but are not the
# key the selector sends. One model can ship several keys across hardware
# revisions and regions, and a key missing here resolves to no label at all --
# which is how a Flow 2 ended up named "Narwal Flow" (#81).
PRODUCT_KEY_ALIASES: dict[str, str] = {
    # Flow 2 has been observed reporting three distinct keys. QxMSPG6VSO is the
    # one the selector sends; these two are equally real.
    "iSuVlI1If2": "Narwal Flow 2",
    # Reported by @DeNo64 (#81) on firmware v01.09.08.00 -- fully working, 28
    # entities, but unnamed because the key was unknown.
    "mkbqaprvrb": "Narwal Flow 2",
}

# Reverse of NARWAL_MODELS, for naming an entry added through Other / Auto-detect.
# Auto-detect resolves the real product key over the WebSocket, so an entry that
# would otherwise be titled "Narwal CGjuB6dzq7" can carry the model's own name
# (#81). First label wins if two ever share a key.
PRODUCT_KEY_TO_MODEL: dict[str, str] = {
    key: label
    for label, key in reversed(list(NARWAL_MODELS.items()))
    if key != "auto"
} | PRODUCT_KEY_ALIASES


def model_label_for_product_key(product_key: str | None) -> str | None:
    """Model selector label for a resolved product key, or None if unknown."""
    return PRODUCT_KEY_TO_MODEL.get(product_key or "")


CONF_DEVICE_ID = "device_id"
CONF_MODEL = "model"
CONF_PRODUCT_KEY = "product_key"

NO_BROADCAST_PRODUCT_KEYS = {"hEA7OEshlx"}


def configured_model_name(data: dict) -> str:
    """Return device-registry model metadata for a config entry."""
    model = data.get(CONF_MODEL)
    if not model or model == "Narwal Flow":
        return MODEL
    if model == MODEL_AUTO_LABEL:
        product_key = data.get(CONF_PRODUCT_KEY)
        return f"Unknown ({product_key})" if product_key else "Unknown"
    return model.removeprefix("Narwal ")

PLATFORMS: list[Platform] = [
    Platform.VACUUM,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.CAMERA,
    Platform.SELECT,
    Platform.NUMBER,
    Platform.SWITCH,
    Platform.LIGHT,
]

CONF_SHOW_ROOM_LABELS = "show_room_labels"
CONF_SHOW_FURNITURE = "show_furniture"
CONF_SHOW_FURNITURE_LABELS = "show_furniture_labels"

MAP_OPTION_DEFAULTS: dict[str, bool] = {
    CONF_SHOW_ROOM_LABELS: True,
    CONF_SHOW_FURNITURE: False,
    CONF_SHOW_FURNITURE_LABELS: False,
}

# Map view options read from the config entry's options by camera.py and applied by
# map_renderer.render_* . There is no options flow to set them yet, so they stay at
# their defaults — an unrotated, unzoomed map, i.e. the pre-#62 rendering.
CONF_MAP_ROTATION = "map_rotation"
CONF_MAP_ZOOM = "map_zoom"

MAP_ROTATION_DEFAULT = 0  # degrees clockwise; renderer accepts 0/90/180/270
MAP_ZOOM_DEFAULT = 1.0  # renderer clamps to 1.0–2.0

CONF_DOCK_LIGHT_SUPPORTED = "dock_light_supported"
SERVICE_CLEAN_ROOMS = "clean_rooms"

def product_keys_for_model(label: str) -> set[str]:
    """Every product key known to belong to one selector model."""
    return {key for key, name in PRODUCT_KEY_TO_MODEL.items() if name == label}


# Derived rather than listed: the dock light is a property of the Flow 2, not
# of one of its keys. Listing them meant a Flow 2 reporting a key nobody had
# added lost a working feature, silently (#81).
DOCK_LIGHT_PRODUCT_KEYS = product_keys_for_model("Narwal Flow 2")


def is_dock_light_supported(data: dict, options: dict | None = None) -> bool:
    """Return whether this configured model exposes dock ambient lighting."""
    if options and CONF_DOCK_LIGHT_SUPPORTED in options:
        return bool(options[CONF_DOCK_LIGHT_SUPPORTED])
    return data.get(CONF_PRODUCT_KEY) in DOCK_LIGHT_PRODUCT_KEYS


DOCK_LIGHT_MODES: dict[str, AmbientLightCtrlType] = {
    "Off": AmbientLightCtrlType.OFF,
    "Fireplace": AmbientLightCtrlType.WINTER_WARMTH,
    "Nightlight": AmbientLightCtrlType.NIGHT_LIGHT,
    "Purple": AmbientLightCtrlType.PURPLE_LIGHT,
}
DOCK_LIGHT_MODE_NAMES: dict[int, str] = {
    int(value): key for key, value in DOCK_LIGHT_MODES.items()
}

# HA fan_speed labels -> FanLevel, from the app's user-visible suction names
# (sentence case, as HA shows fan_speed values directly). The app's internal
# DEEP and SUPER identifiers surface as "Super Powerful" and "Ultra Powerful".
_FAN_SPEED_CANONICAL: dict[str, FanLevel] = {
    "Quiet": FanLevel.MUTE,
    "Standard": FanLevel.NORMAL,
    "Strong": FanLevel.STRONG,
    "Super Powerful": FanLevel.DEEP,
    "Ultra Powerful": FanLevel.SUPER,
}

FAN_SPEED_LIST: list[str] = list(_FAN_SPEED_CANONICAL)

# Models whose app exposes only four suction tiers: FanLevel.SUPER (5) is
# unreachable there, and the live clean/set_fan_level enum has no SUPER at all.
# AX26 confirmed by app captures in #70: its top tier sends CleanParam tag 2 =
# 4 (DEEP), exposed as "Super Powerful" for those models.
NO_LEVEL_5_FAN_PRODUCT_KEYS: frozenset[str] = frozenset({"qV6BujoYLz"})

_FAN_SPEED_NO_LEVEL_5: dict[str, FanLevel] = {
    "Quiet": FanLevel.MUTE,
    "Standard": FanLevel.NORMAL,
    "Strong": FanLevel.STRONG,
    "Super Powerful": FanLevel.DEEP,
}

_FAN_SPEED_ALIASES: dict[str, FanLevel] = {
    "Super": FanLevel.DEEP,
    "Super powerful": FanLevel.DEEP,
    "Ultra": FanLevel.SUPER,
    "Ultra powerful": FanLevel.SUPER,
    "quiet": FanLevel.MUTE,
    "normal": FanLevel.NORMAL,
    "strong": FanLevel.STRONG,
    "max": FanLevel.SUPER,
}

# Meanings used by v1.0.5 and earlier persisted states. Keep this separate from
# current UI labels so a rename cannot silently change a restored robot enum.
UNVERSIONED_FAN_SPEED_MAP: dict[str, FanLevel] = {
    "AI": FanLevel.UNSPECIFIED,
    "Quiet": FanLevel.MUTE,
    "Standard": FanLevel.NORMAL,
    "Strong": FanLevel.STRONG,
    "Super Powerful": FanLevel.DEEP,
    "Ultra": FanLevel.SUPER,
    "Super": FanLevel.DEEP,
    "Super powerful": FanLevel.DEEP,
    "Ultra powerful": FanLevel.SUPER,
    "quiet": FanLevel.MUTE,
    "normal": FanLevel.NORMAL,
    "strong": FanLevel.STRONG,
    "max": FanLevel.SUPER,
}

_FAN_SPEED_NO_LEVEL_5_ALIASES: dict[str, FanLevel] = {
    "Super": FanLevel.DEEP,
    "Super powerful": FanLevel.DEEP,
    "quiet": FanLevel.MUTE,
    "normal": FanLevel.NORMAL,
    "strong": FanLevel.STRONG,
    "max": FanLevel.DEEP,
}


def fan_speed_list_for(data: dict) -> list[str]:
    """Return visible fan_speed options for this configured model."""
    return list(fan_speed_map_for(data, include_aliases=False))


def live_fan_speed_list_for(
    data: dict,
    active_fan: FanLevel | None,
) -> list[str]:
    """Return live suction choices, retaining an active level-five state."""
    options = fan_speed_list_for(data)
    if active_fan != FanLevel.SUPER and "Ultra Powerful" in options:
        options.remove("Ultra Powerful")
    return options


def fan_speed_map_for(
    data: dict,
    *,
    include_aliases: bool = True,
) -> dict[str, FanLevel]:
    """Return visible and compatibility fan_speed labels for this model."""
    if data.get(CONF_PRODUCT_KEY) in NO_LEVEL_5_FAN_PRODUCT_KEYS:
        return _FAN_SPEED_NO_LEVEL_5 | (
            _FAN_SPEED_NO_LEVEL_5_ALIASES if include_aliases else {}
        )
    return _FAN_SPEED_CANONICAL | (_FAN_SPEED_ALIASES if include_aliases else {})


def fan_speed_label_map_for(data: dict) -> dict[FanLevel, str]:
    """Return FanLevel -> visible fan_speed label for this model."""
    return {
        level: label
        for label, level in fan_speed_map_for(data, include_aliases=False).items()
    }


def normalize_fan_level_for_model(data: dict, fan: FanLevel) -> FanLevel:
    """Return a persisted fan level supported by the configured model."""
    if (
        fan == FanLevel.SUPER
        and data.get(CONF_PRODUCT_KEY) in NO_LEVEL_5_FAN_PRODUCT_KEYS
    ):
        return FanLevel.DEEP
    return fan


# FAN_SPEED_MAP also accepts the short "Super" and "Ultra" labels shipped through
# this stack, v1.0.3's "… powerful" labels, and the original lowercase values.
# Keep those aliases out of advertised options while accepting them through the
# vacuum command and restore paths.
FAN_SPEED_MAP: dict[str, FanLevel] = _FAN_SPEED_CANONICAL | _FAN_SPEED_ALIASES

# Backwards-compatible name for existing imports/tests.
NO_ULTRA_FAN_PRODUCT_KEYS = NO_LEVEL_5_FAN_PRODUCT_KEYS
# base_status field 15 (terminateReason) value → HA option key. From the decoded
# TaskResult enum (re/ENUMS.md). Live-confirmed: 1 = NORMAL_END.
TASK_RESULT_OPTIONS: dict[int, str] = {
    1: "normal_end",
    2: "user_force_end",
    3: "shutdown_force_end",
    4: "low_battery_force_end",
    5: "overflow_force_end",
    6: "pause_too_long_force_end",
    7: "system_error_force_end",
    8: "user_force_end_on_station",
    9: "user_force_end_on_app",
    10: "recall_end",
    11: "normal_end_replenish_map_fail",
    12: "map_unmatched_force_end",
    13: "relocation_fail_with_station_force_end",
    14: "relocation_fail_without_station_force_end",
    15: "pretask_error_force_end",
    16: "schedule_task_force_end",
    17: "unexecuted",
    18: "not_find_pet",
}

# Consumable alert enum value → name (ConsumableMaintainItem / ConsumableReplaceItem).
CONSUMABLE_MAINTAIN_ITEMS: dict[int, str] = {
    1: "dust box", 2: "dust filter", 4: "wash ribs", 6: "universal wheel",
    7: "cliff sensor", 8: "side distance sensor", 9: "water tank sponge",
    10: "anti-winding brush", 11: "smart module sponge", 20: "dust container",
}
CONSUMABLE_REPLACE_ITEMS: dict[int, str] = {
    1: "dust filter", 2: "mop", 3: "side brush", 4: "clear water filter",
    5: "roller brush", 6: "detergent", 7: "smart module filter", 8: "dust bag",
    20: "station bag", 21: "silver ions", 22: "curing agent", 23: "heavy detergent",
    24: "inner dust box",
}

# Best-effort help-center deep link for a robot error code. The app's goHelpCenterByCode
# builds <localized help base>?code=<n>&deviceId=…&lang=…; the exact base is a runtime
# i18n value we can't read, so this is inferred from the Flow's help-center family and
# should be corrected if a real error opens a different path. The raw code is the fallback.
ERROR_HELP_URL_TEMPLATE = (
    "https://help.narwal.com/helpcenter/vall/#/p2/question/all?eType=1&code={code}&lang=en-US"
)

# Select option id -> robot enum. Option ids are rendered to the app's
# user-visible labels via translations.
WORK_MODE_MAP: dict[str, WorkMode] = {
    "vacuum": WorkMode.VACUUM,
    "mop": WorkMode.MOP,
    "vacuum_then_mop": WorkMode.VACUUM_THEN_MOP,
    "vacuum_and_mop": WorkMode.VACUUM_AND_MOP,
}
WATER_MAP: dict[str, MopHumidity] = {
    "dry": MopHumidity.DRY,
    "normal": MopHumidity.NORMAL,
    "wet": MopHumidity.WET,
}
MOP_STRENGTH_MAP: dict[str, MopStrengthLevel] = {
    "normal": MopStrengthLevel.NORMAL,
    "high": MopStrengthLevel.HIGH,
}

PASSES_MIN = 1
PASSES_MAX = 3
