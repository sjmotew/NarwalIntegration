"""Narwal Flow Robot Vacuum integration for Home Assistant."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TypeAlias

import voluptuous as vol
from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.components.vacuum import DOMAIN as VACUUM_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_AREA_ID, ATTR_DEVICE_ID, ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import (
    ConfigEntryNotReady,
    HomeAssistantError,
    Unauthorized,
    UnknownUser,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import service

from .const import (
    CONF_DEVICE_ID,
    CONF_MODEL,
    CONF_PRODUCT_KEY,
    DOMAIN,
    PLATFORMS,
    SERVICE_CLEAN_ROOMS,
    SERVICE_SET_PENDING_PROFILES,
    configured_model_name,
    fan_speed_map_for,
)
from .coordinator import (
    CleanSettings,
    NarwalCoordinator,
    can_apply_pending_profiles,
    can_prepare_clean_start,
    clean_setting_applies_to_mode,
)
from .narwal_client import (
    CleaningRoute,
    CommandResult,
    FanLevel,
    MopHumidity,
    MopStrengthLevel,
    NarwalConnectionError,
    RoomCleanSettings,
    WorkMode,
)

_LOGGER = logging.getLogger(__name__)

# Configured from config entries only; async_setup just registers services.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

NarwalConfigEntry: TypeAlias = ConfigEntry[NarwalCoordinator]

_CONFIG_ENTRY_MINOR_VERSION = 2
_LEGACY_REPLACED_SENSOR_SUFFIXES = (
    "base_station_cleaning_filter_used_hours",
    "current_room",
    "map_metadata",
    "status",
    "task_progress",
    "task_status",
)

FIELD_ROOMS = "rooms"
FIELD_MODE = "mode"
FIELD_SUCTION = "suction"
FIELD_WATER = "water"
FIELD_MOP_STRENGTH = "mop_strength"
FIELD_PASSES = "passes"
FIELD_ROUTE = "route"

WORK_MODE_OPTIONS: dict[str, WorkMode] = {
    "vacuum": WorkMode.VACUUM,
    "mop": WorkMode.MOP,
    "vacuum_then_mop": WorkMode.VACUUM_THEN_MOP,
    "vacuum_and_mop": WorkMode.VACUUM_AND_MOP,
}
SUCTION_OPTIONS = (
    "ai",
    "quiet",
    "standard",
    "normal",
    "strong",
    "super_powerful",
    "ultra_powerful",
    "super",
    "ultra",
    "max",
)
WATER_OPTIONS: dict[str, MopHumidity] = {
    "dry": MopHumidity.DRY,
    "normal": MopHumidity.NORMAL,
    "wet": MopHumidity.WET,
}
MOP_STRENGTH_OPTIONS: dict[str, MopStrengthLevel] = {
    "normal": MopStrengthLevel.NORMAL,
    "high": MopStrengthLevel.HIGH,
}
ROUTE_OPTIONS: dict[str, CleaningRoute] = {
    "standard": CleaningRoute.STANDARD,
    "meticulous": CleaningRoute.METICULOUS,
}

CLEAN_ROOMS_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTITY_ID): cv.comp_entity_ids,
        vol.Optional(ATTR_DEVICE_ID): cv.ensure_list,
        vol.Optional(ATTR_AREA_ID): cv.ensure_list,
        vol.Required(FIELD_ROOMS): cv.ensure_list,
        vol.Optional(FIELD_MODE, default="vacuum_and_mop"): vol.In(WORK_MODE_OPTIONS),
        vol.Optional(FIELD_SUCTION, default="standard"): vol.In(SUCTION_OPTIONS),
        vol.Optional(FIELD_WATER, default="normal"): vol.In(WATER_OPTIONS),
        vol.Optional(FIELD_MOP_STRENGTH, default="normal"): vol.In(
            MOP_STRENGTH_OPTIONS
        ),
        vol.Optional(FIELD_PASSES, default=1): lambda value: _validate_pass_count(
            value
        ),
        vol.Optional(FIELD_ROUTE): vol.In(ROUTE_OPTIONS),
    }
)

_PENDING_PROFILE_FIELDS = {
    vol.Required("mode"): vol.In(
        ("Vacuum", "Mop", "Vacuum then mop", "Vacuum and mop")
    ),
    vol.Optional("suction"): str,
    vol.Optional("water"): vol.In(("Dry", "Normal", "Wet")),
    vol.Optional("scrub"): vol.In(("Normal", "High")),
    vol.Optional("passes"): vol.In(("1", "2", "3")),
    vol.Optional("route"): vol.In(("Standard", "Meticulous")),
}
SET_PENDING_PROFILES_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.comp_entity_ids,
        vol.Required("global_profile"): vol.Schema(_PENDING_PROFILE_FIELDS),
        vol.Required("room_profiles"): [
            vol.Schema(
                {
                    vol.Required("entity_id"): str,
                    vol.Required("profile"): vol.Schema(_PENDING_PROFILE_FIELDS),
                }
            )
        ],
        vol.Required("selected_room_entities"): [str],
    }
)

SUCTION_OPTION_LABELS: dict[str, str] = {
    "quiet": "Quiet",
    "standard": "Standard",
    "normal": "Standard",
    "strong": "Strong",
    "ultra": "Ultra Powerful",
    "ultra_powerful": "Ultra Powerful",
    "super": "Super Powerful",
    "super_powerful": "Super Powerful",
    "max": "Super Powerful",
}


def _suction_for_coordinator(
    coordinator: NarwalCoordinator,
    option: str,
) -> FanLevel:
    """Return the model-valid suction level for a service option."""
    if option == "ai":
        return FanLevel.UNSPECIFIED
    fan_map = fan_speed_map_for(coordinator.config_entry.data, include_aliases=False)
    if option == "max":
        return list(fan_map.values())[-1]
    compatibility_label = {
        "super": "Super",
        "ultra": "Ultra",
    }.get(option)
    if compatibility_label is not None:
        compatibility_level = fan_speed_map_for(
            coordinator.config_entry.data
        ).get(compatibility_label)
        if compatibility_level is None:
            raise HomeAssistantError(
                f"{compatibility_label} suction is not supported by this Narwal model"
            )
        return compatibility_level
    label = SUCTION_OPTION_LABELS[option]
    if label not in fan_map:
        raise HomeAssistantError(
            f"{label} suction is not supported by this Narwal model"
        )
    return fan_map[label]


def _domain_data(hass: HomeAssistant) -> dict[str, NarwalCoordinator]:
    """Return Narwal domain runtime data."""
    return hass.data.setdefault(DOMAIN, {})


def _validate_pass_count(value: object) -> int:
    """Return a pass count, rejecting fractional coercion."""
    if isinstance(value, bool):
        raise vol.Invalid("Narwal pass count must be an integer")
    if isinstance(value, int):
        passes = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise vol.Invalid("Narwal pass count must be an integer")
        passes = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text.isdecimal():
            raise vol.Invalid("Narwal pass count must be an integer")
        passes = int(text)
    else:
        raise vol.Invalid("Narwal pass count must be an integer")
    if passes < 1 or passes > 3:
        raise vol.Invalid("Narwal pass count must be between 1 and 3")
    return passes


def _room_tokens(raw_rooms: list) -> list[object]:
    """Return room request tokens split the same way for all/all+room checks."""
    tokens: list[object] = []
    for item in raw_rooms:
        if isinstance(item, str):
            cleaned = item.strip().strip("[]")
            tokens.extend(cleaned.replace(",", " ").split())
        else:
            tokens.append(item)
    return tokens


def _normalise_room_ids(raw_rooms: list) -> list[int]:
    """Return room IDs from HA service data."""
    room_ids: list[int] = []
    seen: set[int] = set()
    for value in _room_tokens(raw_rooms):
        if isinstance(value, str) and value.lower() == "all":
            continue
        if isinstance(value, bool):
            raise HomeAssistantError(f"Invalid Narwal room ID: {value}")
        try:
            if isinstance(value, float):
                if not value.is_integer():
                    raise ValueError
                room_id = int(value)
            elif isinstance(value, int):
                room_id = value
            elif isinstance(value, str) and value.isdecimal():
                room_id = int(value)
            else:
                raise ValueError
        except (TypeError, ValueError) as err:
            raise HomeAssistantError(f"Invalid Narwal room ID: {value}") from err
        if room_id <= 0:
            raise HomeAssistantError("Narwal room IDs must be positive")
        if room_id not in seen:
            seen.add(room_id)
            room_ids.append(room_id)
    return room_ids


def _rooms_requested_all(raw_rooms: list) -> bool:
    """Return True if a service call requests all current map rooms."""
    return any(
        isinstance(item, str) and item.lower() == "all"
        for item in _room_tokens(raw_rooms)
    )


async def _async_room_ids_for_coordinator(
    coordinator: NarwalCoordinator,
    raw_rooms: list,
) -> list[int]:
    """Resolve configured service rooms for a single Narwal coordinator."""
    requested_all = _rooms_requested_all(raw_rooms)
    tokens = _room_tokens(raw_rooms)
    if requested_all and len(tokens) > 1:
        raise HomeAssistantError('Use either "all" or explicit room IDs, not both')
    state = coordinator.client.state
    room_ids = [] if requested_all else _normalise_room_ids(raw_rooms)
    # Explicit IDs already on the cached map leave nothing to resolve, so a
    # map refresh that times out must not abort the clean (#108).
    if room_ids and state.map_data is not None:
        cached_ids = {room.room_id for room in state.map_data.rooms if room.room_id > 0}
        if all(room_id in cached_ids for room_id in room_ids):
            return room_ids
    try:
        await coordinator.client.get_map()
    except Exception as err:
        raise HomeAssistantError("Narwal map could not be refreshed") from err
    if state.map_data is None:
        raise HomeAssistantError("Narwal map is not available")
    map_room_ids = [room.room_id for room in state.map_data.rooms if room.room_id > 0]
    known_room_ids = set(map_room_ids)
    if requested_all:
        map_id = coordinator.room_settings_map_id(state.map_data)
        saved_order = coordinator.room_clean_orders.get(map_id or "")
        if saved_order:
            return coordinator.ordered_clean_room_ids_for(
                map_room_ids,
                map_id=map_id,
            )
        return sorted(known_room_ids)

    unknown_ids = [room_id for room_id in room_ids if room_id not in known_room_ids]
    if unknown_ids:
        raise HomeAssistantError(
            f"Unknown Narwal room ID: {', '.join(str(room_id) for room_id in unknown_ids)}"
        )
    return room_ids


async def _async_validate_target_permissions(
    hass: HomeAssistant,
    call: ServiceCall,
    entity_ids: list[str],
) -> None:
    """Validate control permission for service target entities."""
    user_id = call.context.user_id
    if not user_id:
        return
    user = await hass.auth.async_get_user(user_id)
    if user is None:
        raise UnknownUser(context=call.context)
    if user.is_admin:
        return
    for entity_id in entity_ids:
        if not user.permissions.check_entity(entity_id, POLICY_CONTROL):
            raise Unauthorized(
                context=call.context,
                entity_id=entity_id,
                permission=POLICY_CONTROL,
            )


async def _async_validate_clean_rooms_targets(
    hass: HomeAssistant,
    call: ServiceCall,
    entity_ids: list[str],
) -> list[str]:
    """Validate clean-room target domains and user permissions."""
    registry = er.async_get(hass)
    vacuum_entity_ids: list[str] = []
    for entity_id in entity_ids:
        registry_entry = registry.async_get(entity_id)
        if (
            entity_id.startswith(f"{VACUUM_DOMAIN}.")
            and registry_entry is not None
            and registry_entry.platform == DOMAIN
            and hass.states.get(entity_id) is not None
        ):
            vacuum_entity_ids.append(entity_id)

    direct_entity_ids = call.data.get(ATTR_ENTITY_ID, [])
    if isinstance(direct_entity_ids, str):
        direct_entity_ids = [direct_entity_ids]
    direct_entity_ids = [
        entity_id
        for entity_id in direct_entity_ids
        if entity_id != "all" and not entity_id.startswith("group.")
    ]
    if any(entity_id not in vacuum_entity_ids for entity_id in direct_entity_ids):
        raise HomeAssistantError("Target must be a Narwal vacuum entity")

    await _async_validate_target_permissions(hass, call, vacuum_entity_ids)
    return vacuum_entity_ids


async def _async_get_service_coordinators(
    hass: HomeAssistant,
    entity_ids: list[str] | None,
) -> list[NarwalCoordinator]:
    """Resolve service entity IDs to Narwal coordinators."""
    data = _domain_data(hass)
    if not entity_ids:
        raise HomeAssistantError("Target a Narwal vacuum")

    registry = er.async_get(hass)
    coordinators: list[NarwalCoordinator] = []
    for entity_id in entity_ids:
        registry_entry = registry.async_get(entity_id)
        if registry_entry is None or registry_entry.config_entry_id is None:
            raise HomeAssistantError(
                f"Narwal target is not loaded: {entity_id}"
            )
        coordinator = data.get(registry_entry.config_entry_id)
        if not isinstance(coordinator, NarwalCoordinator):
            raise HomeAssistantError(
                f"Narwal target is not loaded: {entity_id}"
            )
        if coordinator not in coordinators:
            coordinators.append(coordinator)
    if not coordinators:
        raise HomeAssistantError("Target does not contain a Narwal entity")
    return coordinators


def _async_register_services(hass: HomeAssistant) -> None:
    """Register Narwal domain services."""
    register_clean_rooms = not hass.services.has_service(DOMAIN, SERVICE_CLEAN_ROOMS)
    register_pending_profiles = not hass.services.has_service(
        DOMAIN, SERVICE_SET_PENDING_PROFILES
    )
    if not register_clean_rooms and not register_pending_profiles:
        return

    async def _async_extract_entity_ids(call: ServiceCall) -> list[str]:
        """Extract target entity IDs across supported HA helper signatures."""
        try:
            entity_ids = await service.async_extract_entity_ids(call)
        except TypeError:
            entity_ids = await service.async_extract_entity_ids(hass, call)
        return list(entity_ids)

    async def async_clean_rooms(call: ServiceCall) -> None:
        entity_ids = await _async_extract_entity_ids(call)
        if not entity_ids and any(
            key in call.data for key in (ATTR_ENTITY_ID, ATTR_DEVICE_ID, ATTR_AREA_ID)
        ):
            raise HomeAssistantError("Target does not contain a Narwal entity")
        entity_ids = await _async_validate_clean_rooms_targets(hass, call, entity_ids)
        coordinators = await _async_get_service_coordinators(hass, entity_ids)
        if len(coordinators) != 1:
            raise HomeAssistantError("Target exactly one Narwal vacuum")
        coordinator = coordinators[0]

        work_mode = WORK_MODE_OPTIONS[call.data[FIELD_MODE]]
        water = WATER_OPTIONS[call.data[FIELD_WATER]]
        mop_strength = MOP_STRENGTH_OPTIONS[call.data[FIELD_MOP_STRENGTH]]
        passes = call.data[FIELD_PASSES]
        route_label = call.data.get(FIELD_ROUTE)

        if not coordinator.client.robot_awake:
            await coordinator.client.wake(timeout=10.0)

        room_ids = await _async_room_ids_for_coordinator(
            coordinator,
            call.data[FIELD_ROOMS],
        )
        if not room_ids:
            raise HomeAssistantError("At least one room must be selected")

        fan = _suction_for_coordinator(coordinator, call.data[FIELD_SUCTION])
        route = ROUTE_OPTIONS.get(route_label, coordinator.clean_settings.route)
        requested_settings = RoomCleanSettings(
            work_mode=work_mode,
            fan=fan,
            water=water,
            mop_strength=mop_strength,
            passes=passes,
            route=route,
        )
        room_settings = coordinator.room_clean_settings_for_rooms(
            room_ids,
            default=requested_settings,
            use_room_profiles=False,
        )

        async with coordinator.dock_action_lock:
            if not await coordinator.async_refresh_dock_status():
                raise HomeAssistantError("Narwal status could not be refreshed")
            if not can_prepare_clean_start(coordinator.client.state):
                raise HomeAssistantError("Narwal room clean cannot be started right now")
            if not await coordinator.async_prepare_clean_start():
                raise HomeAssistantError("Narwal room clean cannot be started right now")

            client = coordinator.client
            resp = await client.start_rooms(
                room_ids,
                work_mode=requested_settings.work_mode,
                fan=requested_settings.fan,
                water=requested_settings.water,
                mop_strength=requested_settings.mop_strength,
                passes=requested_settings.passes,
                route=requested_settings.route,
                room_settings=room_settings,
            )
            if resp.result_code == 0:
                result_name = "ACCEPTED"
            else:
                try:
                    result_name = CommandResult(resp.result_code).name
                except ValueError:
                    result_name = f"UNKNOWN({resp.result_code})"
            _LOGGER.info(
                "Clean rooms response: %s (code=%s), rooms=%s",
                result_name,
                resp.result_code,
                room_ids,
            )
            if not resp.accepted:
                raise HomeAssistantError(
                    f"Narwal room clean failed: {result_name} ({resp.result_code})"
                )
            coordinator.clean_settings.work_mode = requested_settings.work_mode
            coordinator.clean_settings.fan = requested_settings.fan
            coordinator.clean_settings.water = requested_settings.water
            coordinator.clean_settings.mop_strength = requested_settings.mop_strength
            coordinator.clean_settings.passes = requested_settings.passes
            coordinator.clean_settings.route = requested_settings.route
            coordinator.record_accepted_clean_start(room_settings)
            client.state.assume_robot_clean()
            await coordinator.async_clear_map_display_cache()
            coordinator.async_set_updated_data(client.state)

    if register_clean_rooms:
        hass.services.async_register(
            DOMAIN,
            SERVICE_CLEAN_ROOMS,
            async_clean_rooms,
            schema=CLEAN_ROOMS_SCHEMA,
        )

    async def _async_apply_pending_profiles(call: ServiceCall) -> None:
        """Apply an idle-only pending profile and selection transaction."""
        entity_ids = await _async_extract_entity_ids(call)
        if len(entity_ids) != 1:
            raise HomeAssistantError("Target exactly one Narwal vacuum")
        entity_ids = await _async_validate_clean_rooms_targets(hass, call, entity_ids)
        coordinators = await _async_get_service_coordinators(hass, entity_ids)
        if len(coordinators) != 1:
            raise HomeAssistantError("Target exactly one Narwal vacuum")
        coordinator = coordinators[0]

        # Reuse the option values exposed by the existing legacy selects, so
        # model-specific fan labels and enum meanings remain in one place.
        from .select import (
            LEGACY_MODE_MAP,
            LEGACY_ROUTE_MAP,
            LEGACY_SCRUB_MAP,
            LEGACY_WATER_MAP,
            _legacy_suction_map_for,
        )
        state = coordinator.data
        map_data = state.map_data if state is not None else None
        current_map_id = coordinator.room_settings_map_id(map_data)
        rooms = map_data.rooms if map_data is not None else None
        if (
            current_map_id is None
            or not isinstance(rooms, (list, tuple))
            or not rooms
        ):
            raise HomeAssistantError("Narwal current map is not positively identified")
        if (
            not coordinator._room_selection_store_loaded
            or not coordinator._room_profile_store_loaded
            or not can_apply_pending_profiles(coordinator.data)
        ):
            raise HomeAssistantError("Narwal pending profiles require a loaded idle docked robot")

        room_ids = {room.room_id for room in rooms if room.room_id > 0}
        requested = call.data["room_profiles"]
        registry = er.async_get(hass)

        def room_id_for_entity(entity_id: str) -> int:
            """Resolve a loaded current-map Narwal room entity safely."""
            registry_entry = registry.async_get(entity_id)
            entity_state = hass.states.get(entity_id)
            attributes = entity_state.attributes if entity_state is not None else None
            if (
                registry_entry is None
                or registry_entry.platform != DOMAIN
                or registry_entry.config_entry_id != coordinator.config_entry.entry_id
                or entity_state is None
                or entity_state.state not in {"on", "off"}
                or not isinstance(attributes, Mapping)
                or attributes.get("map_id") != current_map_id
            ):
                raise HomeAssistantError("Narwal room identity does not match the current map")
            room_id = attributes.get("room_id")
            if not isinstance(room_id, int) or isinstance(room_id, bool) or room_id <= 0:
                raise HomeAssistantError("Narwal room identity is not positively loaded")
            return room_id

        requested_ids = [room_id_for_entity(item["entity_id"]) for item in requested]
        selected_ids = [
            room_id_for_entity(entity_id)
            for entity_id in call.data["selected_room_entities"]
        ]
        if (
            len(set(requested_ids)) != len(requested_ids)
            or len(set(selected_ids)) != len(selected_ids)
            or not set(selected_ids) <= room_ids
            or set(requested_ids) != room_ids
        ):
            raise HomeAssistantError("Narwal room profiles do not match the current map")

        fan_map = _legacy_suction_map_for(coordinator.config_entry.data)

        def parse_profile(
            raw: Mapping[str, object],
        ) -> tuple[RoomCleanSettings, set[str]]:
            mode = LEGACY_MODE_MAP[raw["mode"]]
            settings = RoomCleanSettings(work_mode=mode)
            attrs = {"work_mode"}
            if clean_setting_applies_to_mode("fan", mode):
                if "suction" not in raw:
                    raise HomeAssistantError("Vacuum profile requires suction")
                try:
                    settings.fan = fan_map[raw["suction"]]
                except KeyError as err:
                    raise HomeAssistantError(
                        "Suction is not supported by this Narwal model"
                    ) from err
                attrs.add("fan")
            elif "suction" in raw:
                raise HomeAssistantError(
                    "Suction is not applicable to a mop-only profile"
                )
            if clean_setting_applies_to_mode("water", mode):
                if "water" not in raw or "scrub" not in raw:
                    raise HomeAssistantError("Mop profile requires water and scrub")
                settings.water = LEGACY_WATER_MAP[raw["water"]]
                settings.mop_strength = LEGACY_SCRUB_MAP[raw["scrub"]]
                attrs.update({"water", "mop_strength"})
            elif "water" in raw or "scrub" in raw:
                raise HomeAssistantError(
                    "Water and scrub are not applicable to a vacuum-only profile"
                )
            if "passes" not in raw or "route" not in raw:
                raise HomeAssistantError("Profile requires passes and route")
            settings.passes = int(raw["passes"])
            settings.route = LEGACY_ROUTE_MAP[raw["route"]]
            attrs.update({"passes", "route"})
            return settings, attrs

        global_profile, global_attrs = parse_profile(call.data["global_profile"])
        parsed_profiles = {
            room_id: parse_profile(item["profile"])
            for room_id, item in zip(requested_ids, requested, strict=True)
        }
        clean_settings = CleanSettings(
            work_mode=global_profile.work_mode,
            fan=(
                global_profile.fan
                if "fan" in global_attrs
                else coordinator.clean_settings.fan
            ),
            water=(
                global_profile.water
                if "water" in global_attrs
                else coordinator.clean_settings.water
            ),
            mop_strength=(
                global_profile.mop_strength
                if "mop_strength" in global_attrs
                else coordinator.clean_settings.mop_strength
            ),
            passes=global_profile.passes,
            route=global_profile.route,
        )
        try:
            async with coordinator.dock_action_lock:
                coordinator.set_pending_profile_transaction(
                    map_id=current_map_id,
                    global_settings=clean_settings,
                    room_profiles=parsed_profiles,
                    selected_room_ids=set(selected_ids),
                )
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err

    async def async_set_pending_profiles(call: ServiceCall) -> None:
        """Log a visible warning and preserve a failed service result."""
        try:
            await _async_apply_pending_profiles(call)
        except HomeAssistantError as err:
            _LOGGER.warning(
                "Pending profiles were not applied for %s: %s",
                call.data.get(ATTR_ENTITY_ID),
                err,
            )
            raise

    if register_pending_profiles:
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_PENDING_PROFILES,
            async_set_pending_profiles,
            schema=SET_PENDING_PROFILES_SCHEMA,
        )


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up Narwal services."""
    _async_register_services(hass)
    return True


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old config entries and remove replaced status sensors."""
    update_kwargs: dict[str, object] = {}

    if config_entry.version < 2:
        _LOGGER.info(
            "Migrating Narwal config entry from version %d to 2",
            config_entry.version,
        )
        new_data = {**config_entry.data}
        if CONF_PRODUCT_KEY not in new_data:
            new_data[CONF_PRODUCT_KEY] = "QoEsI5qYXO"
        if CONF_MODEL not in new_data:
            new_data[CONF_MODEL] = "Narwal Flow"
        update_kwargs["data"] = new_data
        update_kwargs["version"] = 2
        _LOGGER.info("Migration complete: product_key=%s", new_data[CONF_PRODUCT_KEY])

    if getattr(config_entry, "minor_version", 1) < _CONFIG_ENTRY_MINOR_VERSION:
        _async_remove_legacy_replaced_sensors(hass, config_entry)
        update_kwargs["minor_version"] = _CONFIG_ENTRY_MINOR_VERSION

    if update_kwargs:
        hass.config_entries.async_update_entry(config_entry, **update_kwargs)
    return True


def _async_remove_legacy_replaced_sensors(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
) -> None:
    """Remove old sensors now represented by native entities or attributes."""
    device_id = config_entry.data.get(CONF_DEVICE_ID)
    if not device_id:
        return

    registry = er.async_get(hass)
    unique_ids = {
        f"{device_id}_{suffix}" for suffix in _LEGACY_REPLACED_SENSOR_SUFFIXES
    }
    entity_ids: set[str] = set()

    for unique_id in unique_ids:
        entity_id = registry.async_get_entity_id(
            "sensor",
            DOMAIN,
            unique_id,
        )
        if entity_id is not None:
            entity_ids.add(entity_id)

    for entry in er.async_entries_for_config_entry(
        registry,
        config_entry.entry_id,
    ):
        if (
            entry.domain == "sensor"
            and entry.platform == DOMAIN
            and entry.unique_id in unique_ids
        ):
            entity_ids.add(entry.entity_id)

    for entity_id in sorted(entity_ids):
        registry_entry = registry.async_get(entity_id)
        if registry_entry is None or registry_entry.platform != DOMAIN:
            continue
        _LOGGER.info("Removing legacy Narwal sensor %s", entity_id)
        registry.async_remove(entity_id)


async def async_setup_entry(hass: HomeAssistant, entry: NarwalConfigEntry) -> bool:
    """Set up Narwal from a config entry."""
    _async_remove_legacy_replaced_sensors(hass, entry)
    coordinator = NarwalCoordinator(hass, entry)
    try:
        await coordinator.async_setup()
    except NarwalConnectionError as err:
        raise ConfigEntryNotReady(
            f"Cannot connect to Narwal vacuum at {entry.data['host']}: {err}"
        ) from err
    except Exception as err:  # noqa: BLE001 - any startup failure must retry
        # An unexpected exception here leaves the entry in HA's setup-error
        # state, which is never retried (#101). Ask for a retry instead.
        _LOGGER.warning(
            "Narwal setup at %s failed unexpectedly, will retry: %s",
            entry.data.get("host"),
            err,
        )
        raise ConfigEntryNotReady(
            f"Narwal setup at {entry.data.get('host')} failed: {err}"
        ) from err

    entry.runtime_data = coordinator
    _domain_data(hass)[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    device_registry = dr.async_get(hass)
    device = device_registry.async_get_device(
        identifiers={(DOMAIN, entry.data["device_id"])}
    )
    if device is not None:
        device_registry.async_update_device(
            device.id,
            model=configured_model_name(entry.data),
            sw_version=coordinator.client.state.firmware_version or None,
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: NarwalConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        await entry.runtime_data.async_shutdown()
        _domain_data(hass).pop(entry.entry_id, None)

    return unload_ok
