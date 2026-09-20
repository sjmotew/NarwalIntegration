"""Diagnostics support for the Narwal vacuum integration.

Most questions this project receives are some form of "does my model work?",
and answering one has meant asking the reporter for a product key, a firmware
string, a port scan and a log line, one comment at a time. #81 took three
releases to resolve because nobody had the robot's product key in front of
them: it was `mkbqaprvrb`, a Flow 2 key the integration had never seen.

This dump exists so that whole exchange becomes a single attachment. It leans
deliberately towards including undecoded data — `raw_base_status` is the field
map we reverse-engineer new models from — and the `model_resolution` section
states plainly whether this build recognises the robot's key at all, which is
the question behind most model reports.
"""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import NarwalConfigEntry
from .const import (
    CONF_DEVICE_ID,
    CONF_MODEL,
    CONF_PRODUCT_KEY,
    NARWAL_MODELS,
    configured_model_name,
    model_label_for_product_key,
)
from .narwal_client.const import KNOWN_PRODUCT_KEYS

# `product_key` is deliberately NOT redacted — it is the single most useful
# value in this file and identifies a model, not a person. What is redacted
# identifies the household or the account: the robot's address on the network,
# its serial-like device id, and the UUID of the Narwal account it is bound to.
TO_REDACT = {
    "host",
    CONF_DEVICE_ID,
    "device_id",
    "binded_uuid",
}

# Feature-list query is a live round trip. A docked robot can be slow to answer
# and diagnostics must never hang the UI, so this is best-effort.
_FEATURE_LIST_TIMEOUT = 10.0


def _device_id_suffix(device_id: str | None) -> str | None:
    """Last six hex characters of the device id.

    The full id is redacted, but this suffix is what the robot advertises over
    mDNS (`NARWAL_7bb53c.local.`) and what appears in every topic, so keeping
    it lets a reporter's logs be matched to their diagnostics without exposing
    the identifier itself.
    """
    if not device_id or len(device_id) < 6:
        return None
    return device_id[-6:].lower()


def _model_resolution(entry: NarwalConfigEntry, resolved_key: str | None) -> dict[str, Any]:
    """How this build names the robot, and whether it recognises its key.

    `key_is_known` false is the interesting case and the reason this section
    exists: it means the robot works but the integration has no name for it,
    which is a one-line fix once someone can see it.
    """
    stored_key = entry.data.get(CONF_PRODUCT_KEY)
    return {
        "stored_product_key": stored_key,
        "resolved_product_key": resolved_key,
        "stored_model_label": entry.data.get(CONF_MODEL),
        "label_for_resolved_key": model_label_for_product_key(resolved_key),
        "device_registry_model": configured_model_name(dict(entry.data)),
        "key_is_known": resolved_key in KNOWN_PRODUCT_KEYS if resolved_key else None,
        "key_is_selectable": resolved_key in set(NARWAL_MODELS.values())
        if resolved_key
        else None,
        "keys_disagree": bool(stored_key and resolved_key and stored_key != resolved_key),
    }


def _map_summary(map_data: Any) -> dict[str, Any] | None:
    """Room and geometry summary, without the compressed map payload itself."""
    if map_data is None:
        return None
    return {
        "map_id": map_data.map_id,
        "width": map_data.width,
        "height": map_data.height,
        "resolution": map_data.resolution,
        "area": map_data.area,
        "origin_x": map_data.origin_x,
        "origin_y": map_data.origin_y,
        "has_dock_position": map_data.dock_x is not None and map_data.dock_y is not None,
        "compressed_map_bytes": len(map_data.compressed_map or b""),
        "obstacle_count": len(map_data.obstacles),
        "rooms": [
            {
                "room_id": room.room_id,
                "name": room.name,
                "room_sub_type": room.room_sub_type,
                "category": room.category,
            }
            for room in map_data.rooms
        ],
    }


def _robot_info(diagnostics: Any) -> dict[str, Any] | None:
    """Battery engineering data plus the labelled sections it was read from.

    `sections` keeps the robot's own labels (Chinese, whatever the voice
    language) so an unrecognised line can be mapped from a download without
    another capture. The PSK never reaches `RobotDiagnostics` in the first
    place, which is what makes including the sections whole safe.
    """
    if diagnostics is None:
        return None
    return {
        "battery_health": diagnostics.battery_health,
        "battery_cycles": diagnostics.battery_cycles,
        "battery_voltage": diagnostics.battery_voltage,
        "battery_current": diagnostics.battery_current,
        "battery_temperature": diagnostics.battery_temperature,
        "battery_real_level": diagnostics.battery_real_level,
        "charge_remaining_minutes": diagnostics.charge_remaining_minutes,
        "sections": {
            title: dict(items) for title, items in diagnostics.sections.items()
        },
    }


async def _feature_list(coordinator: Any) -> dict[str, Any]:
    """Query the robot's feature list, degrading to the reason it failed.

    Reported rather than omitted on failure: "the robot would not answer
    get_feature_list" is itself a finding on an unsupported model.
    """
    client = coordinator.client
    if not client.connected:
        return {"available": False, "reason": "not connected"}
    try:
        async with asyncio.timeout(_FEATURE_LIST_TIMEOUT):
            features = await client.get_feature_list()
    except TimeoutError:
        return {"available": False, "reason": "timed out"}
    except Exception as err:  # noqa: BLE001 - any failure is worth reporting verbatim
        return {"available": False, "reason": f"{type(err).__name__}: {err}"}
    return {"available": True, "features": {str(k): v for k, v in features.items()}}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: NarwalConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a Narwal config entry."""
    coordinator = entry.runtime_data
    client = coordinator.client
    state = client.state
    device_info = state.device_info
    resolved_key = (client.topic_prefix or "").lstrip("/") or None

    diagnostics: dict[str, Any] = {
        "entry": {
            "version": entry.version,
            "data": dict(entry.data),
            "options": dict(entry.options),
        },
        "model_resolution": _model_resolution(entry, resolved_key),
        "device": {
            "product_key": device_info.product_key if device_info else None,
            "firmware_version": state.firmware_version or None,
            "firmware_target": state.firmware_target or None,
            "device_id_suffix": _device_id_suffix(
                device_info.device_id if device_info else None
            ),
        },
        "connection": {
            "connected": client.connected,
            "topic_prefix": client.topic_prefix,
            "supports_broadcasts": client.supports_broadcasts,
            "robot_awake": client.robot_awake,
            "last_update_success": coordinator.last_update_success,
            "has_fresh_state": coordinator.has_fresh_state,
        },
        "feature_list": await _feature_list(coordinator),
        "state": {
            "working_status": int(state.working_status),
            "battery_level": state.battery_level,
            "is_paused": state.is_paused,
            "is_returning_to_dock": state.is_returning_to_dock,
            "dock_presence": state.dock_presence,
            "dock_sub_state": state.dock_sub_state,
            "dock_activity": state.dock_activity,
            "station_activity": state.station_activity,
            "dock_field11": state.dock_field11,
            "dock_field47": state.dock_field47,
            "dock_light_mode": state.dock_light_mode,
            "current_room_id": state.current_room_id,
            "cleaning_area": state.cleaning_area,
            "cleaning_time": state.cleaning_time,
            "terminate_reason": state.terminate_reason,
        },
        "consumables": {
            "dust_bag_health": state.dust_bag_health,
            "detergent_remaining": state.detergent_remaining,
            "curing_agent_consumption_percent": state.curing_agent_consumption_percent,
            "clean_water_tank_state": state.clean_water_tank_state,
            "sewage_tank_state": state.sewage_tank_state,
            "dust_box_state": state.dust_box_state,
            "dust_bag_state": state.dust_bag_state,
            "station_bag_state": state.station_bag_state,
            "maintain_items": list(state.maintain_items),
            "replace_items": list(state.replace_items),
            "raw_consumable_info": _jsonable(state.raw_consumable_info),
        },
        "errors": {
            "has_error": state.has_error,
            "error_codes": list(state.error_codes),
            "error_level": state.error_level,
            "error_detail": state.error_detail,
        },
        "map": _map_summary(state.map_data),
        # developer/get_robot_info. The robot returns the Wi-Fi PSK in this
        # response; the client discards it at parse time, so nothing here can
        # carry it. See narwal_client.client._parse_robot_info.
        "robot_info": _robot_info(getattr(state, "diagnostics", None)),
        # The undecoded field map. Keys are protobuf field numbers as strings.
        # This is what new-model support is actually built from, so it is
        # included whole rather than filtered to fields this build understands.
        "raw_base_status": _jsonable(state.raw_base_status),
    }

    return async_redact_data(diagnostics, TO_REDACT)


def _jsonable(value: Any) -> Any:
    """Coerce decoded protobuf values into something the JSON dump survives.

    `raw_base_status` holds whatever the decoder produced — nested dicts, ints,
    and `bytes` for anything it could not type. Bytes are hex-encoded rather
    than dropped: an undecoded blob is frequently the field being asked about.
    """
    if isinstance(value, bytes):
        return {"__bytes_hex__": value.hex(), "length": len(value)}
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value
