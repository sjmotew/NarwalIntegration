"""Clean-parameter select entities for Narwal vacuum.

These hold pending values applied at the next room clean. Water additionally writes
live via clean/set_mop_humidity while cleaning.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.util import slugify

from . import NarwalConfigEntry
from .const import (
    MOP_STRENGTH_MAP,
    UNVERSIONED_FAN_SPEED_MAP,
    WATER_MAP,
    WORK_MODE_MAP,
    fan_speed_label_map_for,
    fan_speed_list_for,
    fan_speed_map_for,
    normalize_fan_level_for_model,
)
from .coordinator import (
    NarwalCoordinator,
    can_edit_pending_clean_settings,
    clean_setting_applies_to_mode,
    is_live_clean_setting_available,
)
from .entity import NarwalEntity
from .narwal_client import (
    CleaningRoute,
    CommandResult,
    FanLevel,
    MopHumidity,
    MopStrengthLevel,
    WorkMode,
)
from .narwal_client.const import fan_level_for_live_command


def _raise_if_command_failed(response, action: str) -> None:
    """Raise a Home Assistant service error for rejected robot commands."""
    if response.accepted:
        return
    try:
        result_name = CommandResult(response.result_code).name
    except ValueError:
        result_name = f"UNKNOWN({response.result_code})"
    raise HomeAssistantError(f"Narwal {action} failed: {result_name}")


@dataclass(frozen=True, kw_only=True)
class NarwalSelectEntityDescription(SelectEntityDescription):
    """Describes a Narwal clean-param select."""

    attr: str  # CleanSettings field this select reads/writes
    mapping: dict[str, int]  # option label -> robot enum value
    live_setter: str | None = None  # NarwalClient coroutine applied live while cleaning


@dataclass(frozen=True)
class SettingRestoreData(ExtraStoredData):
    """Persist a clean setting by its stable robot value."""

    value: int
    version: int = 1

    def as_dict(self) -> dict[str, int]:
        """Return a JSON-serializable representation."""
        return {"version": self.version, "value": self.value}


@dataclass(frozen=True)
class RoomSettingRestoreData(ExtraStoredData):
    """Persist a room setting independently of entity availability."""

    value: int
    customized: bool
    version: int = 1

    def as_dict(self) -> dict[str, int | bool]:
        """Return a JSON-serializable representation."""
        return {
            "version": self.version,
            "value": self.value,
            "customized": self.customized,
        }


SELECT_DESCRIPTIONS: tuple[NarwalSelectEntityDescription, ...] = (
    NarwalSelectEntityDescription(
        key="work_mode",
        translation_key="work_mode",
        entity_category=EntityCategory.CONFIG,
        attr="work_mode",
        mapping=WORK_MODE_MAP,
        options=list(WORK_MODE_MAP),
    ),
    NarwalSelectEntityDescription(
        key="water",
        translation_key="water",
        entity_category=EntityCategory.CONFIG,
        attr="water",
        mapping=WATER_MAP,
        live_setter="set_mop_humidity",
        options=list(WATER_MAP),
    ),
    NarwalSelectEntityDescription(
        key="mop_strength",
        translation_key="mop_strength",
        entity_category=EntityCategory.CONFIG,
        attr="mop_strength",
        mapping=MOP_STRENGTH_MAP,
        options=list(MOP_STRENGTH_MAP),
    ),
)

LEGACY_MODE_OPTIONS = ("Vacuum", "Mop", "Vacuum then mop", "Vacuum and mop")
LEGACY_SUCTION_OPTIONS = ("AI", *fan_speed_list_for({}))
LEGACY_WATER_OPTIONS = ("Dry", "Normal", "Wet")
LEGACY_SCRUB_OPTIONS = ("Normal", "High")
LEGACY_ROUTE_OPTIONS = ("Standard", "Meticulous")
LEGACY_PASSES_OPTIONS = ("1", "2", "3")

LEGACY_MODE_MAP: dict[str, WorkMode] = {
    "Vacuum": WorkMode.VACUUM,
    "Mop": WorkMode.MOP,
    "Vacuum then mop": WorkMode.VACUUM_THEN_MOP,
    "Vacuum and mop": WorkMode.VACUUM_AND_MOP,
}
LEGACY_MODE_LABELS: dict[WorkMode, str] = {
    value: label for label, value in LEGACY_MODE_MAP.items()
}
def _legacy_suction_map_for(data: dict) -> dict[str, FanLevel]:
    """Return legacy suction options for this model, including hidden aliases."""
    return {"AI": FanLevel.UNSPECIFIED, **fan_speed_map_for(data)}


def _legacy_suction_labels_for(data: dict) -> dict[FanLevel, str]:
    """Return FanLevel labels for this model's visible legacy suction options."""
    return {FanLevel.UNSPECIFIED: "AI", **fan_speed_label_map_for(data)}
LEGACY_WATER_MAP: dict[str, MopHumidity] = {
    "Dry": MopHumidity.DRY,
    "Normal": MopHumidity.NORMAL,
    "Wet": MopHumidity.WET,
}
LEGACY_SCRUB_MAP: dict[str, MopStrengthLevel] = {
    "Normal": MopStrengthLevel.NORMAL,
    "High": MopStrengthLevel.HIGH,
}
LEGACY_ROUTE_MAP: dict[str, CleaningRoute] = {
    "Standard": CleaningRoute.STANDARD,
    "Meticulous": CleaningRoute.METICULOUS,
}
LEGACY_MOP_MODES = {"Mop", "Vacuum then mop", "Vacuum and mop"}
LEGACY_VACUUM_MODES = {"Vacuum", "Vacuum then mop", "Vacuum and mop"}
LEGACY_START_ONLY_SETTINGS = {"mode", "passes", "route", "scrub"}
START_ONLY_CLEAN_SETTING_ATTRS = {"work_mode", "mop_strength"}


@dataclass(frozen=True, kw_only=True)
class LegacyNarwalSelectEntityDescription(SelectEntityDescription):
    """Describes a backwards-compatible Narwal setting select."""

    setting_key: str
    setting_options: tuple[str, ...]
    default_option: str
    icon: str


LEGACY_SELECT_DESCRIPTIONS: tuple[LegacyNarwalSelectEntityDescription, ...] = (
    LegacyNarwalSelectEntityDescription(
        key="mode",
        setting_key="mode",
        name="Mode",
        setting_options=LEGACY_MODE_OPTIONS,
        default_option="Vacuum and mop",
        icon="mdi:robot-vacuum",
    ),
    LegacyNarwalSelectEntityDescription(
        key="runtime_suction",
        setting_key="suction",
        name="Suction",
        setting_options=LEGACY_SUCTION_OPTIONS,
        default_option="Super Powerful",
        icon="mdi:fan",
    ),
    LegacyNarwalSelectEntityDescription(
        key="runtime_water",
        setting_key="water",
        name="Water",
        setting_options=LEGACY_WATER_OPTIONS,
        default_option="Wet",
        icon="mdi:water",
    ),
    LegacyNarwalSelectEntityDescription(
        key="scrub",
        setting_key="scrub",
        name="Scrub",
        setting_options=LEGACY_SCRUB_OPTIONS,
        default_option="High",
        icon="mdi:brush",
    ),
    LegacyNarwalSelectEntityDescription(
        key="route",
        setting_key="route",
        name="Route",
        setting_options=LEGACY_ROUTE_OPTIONS,
        default_option="Meticulous",
        icon="mdi:routes",
    ),
    LegacyNarwalSelectEntityDescription(
        key="passes",
        setting_key="passes",
        name="Passes",
        setting_options=LEGACY_PASSES_OPTIONS,
        default_option="2",
        icon="mdi:counter",
    ),
)


@dataclass(frozen=True, kw_only=True)
class RoomNarwalSelectEntityDescription(SelectEntityDescription):
    """Describes a per-room Narwal clean profile select."""

    setting_key: str
    attr: str
    default_option: str
    icon: str


ROOM_SELECT_DESCRIPTIONS: tuple[RoomNarwalSelectEntityDescription, ...] = (
    RoomNarwalSelectEntityDescription(
        key="room_mode",
        setting_key="mode",
        attr="work_mode",
        name="mode",
        default_option="Vacuum and mop",
        icon="mdi:robot-vacuum",
    ),
    RoomNarwalSelectEntityDescription(
        key="room_suction",
        setting_key="suction",
        attr="fan",
        name="suction",
        default_option="Super Powerful",
        icon="mdi:fan",
    ),
    RoomNarwalSelectEntityDescription(
        key="room_water",
        setting_key="water",
        attr="water",
        name="water",
        default_option="Wet",
        icon="mdi:water",
    ),
    RoomNarwalSelectEntityDescription(
        key="room_scrub",
        setting_key="scrub",
        attr="mop_strength",
        name="scrub",
        default_option="High",
        icon="mdi:brush",
    ),
    RoomNarwalSelectEntityDescription(
        key="room_route",
        setting_key="route",
        attr="route",
        name="route",
        default_option="Meticulous",
        icon="mdi:routes",
    ),
    RoomNarwalSelectEntityDescription(
        key="room_passes",
        setting_key="passes",
        attr="passes",
        name="passes",
        default_option="2",
        icon="mdi:counter",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Narwal clean-param select entities."""
    coordinator = entry.runtime_data
    known_room_settings: dict[tuple[str | None, int, str], RoomNarwalSettingSelect] = {}

    @callback
    def async_add_room_setting_entities() -> None:
        map_data = coordinator.client.state.map_data
        if map_data is None:
            return
        map_id = coordinator.room_settings_map_id(map_data)
        entities: list[RoomNarwalSettingSelect] = []
        for room in sorted(map_data.rooms, key=lambda item: item.display_name.lower()):
            if room.room_id <= 0:
                continue
            for description in ROOM_SELECT_DESCRIPTIONS:
                key = (map_id, room.room_id, description.key)
                if key in known_room_settings:
                    known_room_settings[key].async_update_room_name(room.display_name)
                    continue
                entity = RoomNarwalSettingSelect(
                    coordinator,
                    room.room_id,
                    room.display_name,
                    description,
                    map_id=map_id,
                )
                known_room_settings[key] = entity
                entities.append(entity)
        if entities:
            async_add_entities(entities)

    async_add_entities(
        [
            *(
                NarwalSelect(coordinator, description)
                for description in SELECT_DESCRIPTIONS
            ),
            *(
                LegacyNarwalSettingSelect(coordinator, description)
                for description in LEGACY_SELECT_DESCRIPTIONS
            ),
        ]
    )
    async_add_room_setting_entities()
    entry.async_on_unload(coordinator.async_add_listener(async_add_room_setting_entities))


class NarwalSelect(NarwalEntity, RestoreEntity, SelectEntity):
    """Clean-parameter select backed by coordinator.clean_settings."""

    entity_description: NarwalSelectEntityDescription

    def __init__(
        self,
        coordinator: NarwalCoordinator,
        description: NarwalSelectEntityDescription,
    ) -> None:
        """Initialize the select."""
        super().__init__(coordinator)
        self.entity_description = description
        device_id = coordinator.config_entry.data["device_id"]
        self._attr_unique_id = f"{device_id}_{description.key}"
        self._labels = {int(v): k for k, v in description.mapping.items()}

    async def async_added_to_hass(self) -> None:
        """Restore the last selection into clean_settings (persists across restarts)."""
        await super().async_added_to_hass()
        extra = await self.async_get_last_extra_data()
        if extra is not None:
            data = extra.as_dict()
            raw_value = data.get("value")
            values = {
                int(value): value for value in self.entity_description.mapping.values()
            }
            if (
                data.get("version") == 1
                and isinstance(raw_value, int)
                and not isinstance(raw_value, bool)
                and raw_value in values
            ):
                setattr(
                    self.coordinator.clean_settings,
                    self.entity_description.attr,
                    values[raw_value],
                )
                self.coordinator.async_update_listeners()
                return
        last = await self.async_get_last_state()
        if last is not None and last.state in self.entity_description.mapping:
            setattr(
                self.coordinator.clean_settings,
                self.entity_description.attr,
                self.entity_description.mapping[last.state],
            )
            self.coordinator.async_update_listeners()

    @property
    def extra_restore_state_data(self) -> ExtraStoredData:
        """Return the pending value even when this select is unavailable."""
        value = getattr(
            self.coordinator.clean_settings,
            self.entity_description.attr,
        )
        return SettingRestoreData(value=int(value))

    @property
    def available(self) -> bool:
        """Return True when this clean parameter can be changed now."""
        state = self.coordinator.data
        setup_available = (
            can_edit_pending_clean_settings(state)
            and not self.coordinator.has_selected_clean_rooms()
        )
        setup_applies = clean_setting_applies_to_mode(
            self.entity_description.attr,
            self.coordinator.clean_settings.work_mode,
        )
        if self.entity_description.attr in START_ONLY_CLEAN_SETTING_ATTRS:
            return setup_available and setup_applies
        live_available = super().available and is_live_clean_setting_available(state)
        live_mode = self.coordinator.clean_setting_applicability_mode(live=True)
        live_applies = clean_setting_applies_to_mode(
            self.entity_description.attr,
            live_mode,
        )
        return (
            (setup_available and setup_applies)
            or (live_available and live_applies)
        )

    @property
    def current_option(self) -> str | None:
        """Return the stored option label."""
        value = self.coordinator.active_clean_setting(self.entity_description.attr)
        if value is None:
            value = getattr(
                self.coordinator.clean_settings,
                self.entity_description.attr,
            )
        return self._labels.get(int(value))

    async def async_select_option(self, option: str) -> None:
        """Store an editable pending selection or apply it to a live clean."""
        if option not in self.entity_description.mapping:
            raise HomeAssistantError(f"Unsupported Narwal option: {option}")
        state = self.coordinator.data
        has_selected_rooms = self.coordinator.has_selected_clean_rooms()
        setup_available = (
            can_edit_pending_clean_settings(state)
            and not has_selected_rooms
        )
        live_available = super().available and is_live_clean_setting_available(state)
        setup_applies = clean_setting_applies_to_mode(
            self.entity_description.attr,
            self.coordinator.clean_settings.work_mode,
        )
        live_mode = self.coordinator.clean_setting_applicability_mode(live=True)
        live_applies = clean_setting_applies_to_mode(
            self.entity_description.attr,
            live_mode,
        )
        if not (
            (setup_available and setup_applies)
            or (
                self.entity_description.attr not in START_ONLY_CLEAN_SETTING_ATTRS
                and live_available
                and live_applies
            )
        ):
            if not setup_applies and not live_applies:
                raise HomeAssistantError(
                    "This Narwal setting is not available for the selected mode"
                )
            raise HomeAssistantError(
                "This Narwal setting cannot be changed right now"
            )
        if (
            self.entity_description.attr in START_ONLY_CLEAN_SETTING_ATTRS
            and not setup_available
        ):
            raise HomeAssistantError("This Narwal setting cannot be changed right now")
        if (
            self.entity_description.attr not in START_ONLY_CLEAN_SETTING_ATTRS
            and not setup_available
            and not live_available
        ):
            raise HomeAssistantError("This Narwal setting cannot be changed right now")

        value = self.entity_description.mapping[option]
        if (
            self.entity_description.live_setter
            and state is not None
            and live_available
            and live_applies
        ):
            response = await getattr(
                self.coordinator.client, self.entity_description.live_setter
            )(
                value
            )
            _raise_if_command_failed(response, f"set {self.entity_description.name}")
            self.coordinator.set_active_clean_setting(
                self.entity_description.attr,
                value,
            )
        if not has_selected_rooms:
            setattr(self.coordinator.clean_settings, self.entity_description.attr, value)
        self.async_write_ha_state()
        self.coordinator.async_update_listeners()


class RoomNarwalSettingSelect(NarwalEntity, RestoreEntity, SelectEntity):
    """Per-room clean profile select backed by coordinator room settings."""

    _attr_entity_registry_enabled_default = False

    entity_description: RoomNarwalSelectEntityDescription

    def __init__(
        self,
        coordinator: NarwalCoordinator,
        room_id: int,
        room_name: str,
        description: RoomNarwalSelectEntityDescription,
        *,
        map_id: str | None = None,
    ) -> None:
        """Initialize the per-room select."""
        super().__init__(coordinator)
        self.entity_description = description
        self._map_id = map_id
        self._room_id = room_id
        self._room_name = room_name
        device_id = coordinator.config_entry.data["device_id"]
        map_prefix = f"map_{slugify(map_id)}_" if map_id is not None else ""
        self._attr_unique_id = (
            f"{device_id}_{map_prefix}room_{room_id}_{description.setting_key}"
        )
        self._attr_name = f"{room_name} {description.name}"
        self._attr_icon = description.icon
        self._attr_options = self._options_for_description(description)
        self._attr_entity_category = EntityCategory.CONFIG

    @callback
    def async_update_room_name(self, room_name: str) -> None:
        """Update display metadata when the map renames this room."""
        if room_name == self._room_name:
            return
        self._room_name = room_name
        self._attr_name = f"{room_name} {self.entity_description.name}"
        if getattr(self, "hass", None) is not None:
            self.async_write_ha_state()

    def _options_for_description(
        self,
        description: RoomNarwalSelectEntityDescription,
    ) -> list[str]:
        """Return selectable options for this room setting."""
        if description.setting_key == "suction":
            return ["AI", *fan_speed_list_for(self.coordinator.config_entry.data)]
        if description.setting_key == "mode":
            return list(LEGACY_MODE_OPTIONS)
        if description.setting_key == "water":
            return list(LEGACY_WATER_OPTIONS)
        if description.setting_key == "scrub":
            return list(LEGACY_SCRUB_OPTIONS)
        if description.setting_key == "route":
            return list(LEGACY_ROUTE_OPTIONS)
        if description.setting_key == "passes":
            return list(LEGACY_PASSES_OPTIONS)
        return []

    async def async_added_to_hass(self) -> None:
        """Restore the room profile option."""
        await super().async_added_to_hass()
        if getattr(self.coordinator, "_room_profile_store_loaded", True) is False:
            return
        if self._is_customized:
            return
        extra = await self.async_get_last_extra_data()
        if extra is not None:
            data = extra.as_dict()
            if data.get("customized") is False:
                return
            if data.get("customized") is True:
                raw_value = data.get("value")
                if (
                    data.get("version") == 1
                    and isinstance(raw_value, int)
                    and not isinstance(raw_value, bool)
                    and self._apply_raw_value(raw_value)
                ):
                    return
                option = data.get("option")
                if isinstance(option, str) and self._apply_unversioned_option(option):
                    return
        last = await self.async_get_last_state()
        if last is not None and self._restore_state_is_customized(last):
            self._apply_unversioned_option(last.state)

    @property
    def extra_restore_state_data(self) -> ExtraStoredData:
        """Return the room profile even when its entity is unavailable."""
        return RoomSettingRestoreData(
            value=self._raw_value,
            customized=self._is_customized,
        )

    @property
    def available(self) -> bool:
        """Return True when this room profile can be changed now."""
        if (
            getattr(self.coordinator, "_room_profile_store_loaded", True) is False
            or not can_edit_pending_clean_settings(self.coordinator.data)
            or not self._room_exists
        ):
            return False
        key = self.entity_description.setting_key
        mode = self._selected_mode
        if key == "water" and mode not in LEGACY_MOP_MODES:
            return False
        if key == "scrub" and mode not in LEGACY_MOP_MODES:
            return False
        return key != "suction" or mode in LEGACY_VACUUM_MODES

    @property
    def options(self) -> list[str]:
        """Return the selectable room profile options."""
        return list(self._attr_options or [])

    @property
    def current_option(self) -> str | None:
        """Return the currently selected profile option."""
        settings = self.coordinator.effective_room_clean_settings_for(
            self._room_id,
            map_id=self._map_id,
        )
        return self._label_for_value(getattr(settings, self.entity_description.attr))

    @property
    def extra_state_attributes(self) -> dict[str, str | int | bool]:
        """Return room metadata for dashboards and automations."""
        return {
            "room_id": self._room_id,
            "room_name": self._room_name,
            "setting": self.entity_description.setting_key,
            "map_id": self._map_id or "",
            "customized": self._is_customized,
        }

    @property
    def _is_customized(self) -> bool:
        """Return True when this room field was explicitly customized."""
        customized = getattr(self.coordinator, "room_clean_settings_customized", {})
        key = (self._map_id, self._room_id)
        return self.entity_description.attr in customized.get(key, set())

    @property
    def _room_exists(self) -> bool:
        """Return True when the room still exists in the current map."""
        state = self.coordinator.data
        map_data = getattr(state, "map_data", None) if state is not None else None
        if self.coordinator.room_settings_map_id(map_data) != self._map_id:
            return False
        rooms = getattr(map_data, "rooms", None)
        if not isinstance(rooms, (list, tuple)):
            return True
        return any(room.room_id == self._room_id for room in rooms)

    @property
    def _selected_mode(self) -> str:
        """Return the selected room clean mode."""
        settings = self.coordinator.effective_room_clean_settings_for(
            self._room_id,
            map_id=self._map_id,
        )
        return LEGACY_MODE_LABELS.get(settings.work_mode) or "Vacuum and mop"

    def _label_for_value(self, value) -> str | None:
        """Return the UI option label for a profile value."""
        key = self.entity_description.setting_key
        if key == "mode":
            return LEGACY_MODE_LABELS.get(value)
        if key == "suction":
            return self._suction_labels.get(value)
        if key == "water":
            labels = {value: label for label, value in LEGACY_WATER_MAP.items()}
            return labels.get(value)
        if key == "scrub":
            labels = {value: label for label, value in LEGACY_SCRUB_MAP.items()}
            return labels.get(value)
        if key == "route":
            labels = {value: label for label, value in LEGACY_ROUTE_MAP.items()}
            return labels.get(value)
        if key == "passes":
            return str(value)
        return None

    def _normalise_option(self, option: str) -> str | None:
        """Return a current option, accepting old hidden suction aliases."""
        if option in self.options:
            return option
        if self.entity_description.setting_key != "suction":
            return None
        label = self._suction_labels.get(self._suction_map.get(option))
        return label if label in self.options else None

    @staticmethod
    def _restore_state_is_customized(state: State) -> bool:
        """Return True when restored state represents an explicit room override."""
        attributes = getattr(state, "attributes", None)
        return isinstance(attributes, dict) and attributes.get("customized") is True

    @property
    def _raw_value(self) -> int:
        """Return the stable robot value for this room setting."""
        settings = self.coordinator.effective_room_clean_settings_for(
            self._room_id,
            map_id=self._map_id,
        )
        return int(getattr(settings, self.entity_description.attr))

    def _value_for_option(self, option: str, *, unversioned: bool = False):
        """Return the robot value represented by an option."""
        key = self.entity_description.setting_key
        if key == "mode":
            return LEGACY_MODE_MAP.get(option)
        elif key == "suction":
            mapping = (
                UNVERSIONED_FAN_SPEED_MAP
                if unversioned
                else self._suction_map
            )
            return mapping.get(option)
        elif key == "water":
            return LEGACY_WATER_MAP.get(option)
        elif key == "scrub":
            return LEGACY_SCRUB_MAP.get(option)
        elif key == "route":
            return LEGACY_ROUTE_MAP.get(option)
        elif key == "passes":
            return int(option) if option in LEGACY_PASSES_OPTIONS else None
        return None

    def _allowed_values(self) -> dict[int, object]:
        """Return valid robot values for this room field."""
        options = self.options
        values = [self._value_for_option(option) for option in options]
        return {int(value): value for value in values if value is not None}

    def _apply_raw_value(self, raw_value: int) -> bool:
        """Store a validated stable robot value."""
        value = self._allowed_values().get(raw_value)
        if value is None:
            return False
        self.coordinator.set_room_clean_setting(
            self._room_id,
            self.entity_description.attr,
            value,
            map_id=self._map_id,
        )
        self.coordinator.async_update_listeners()
        return True

    def _apply_unversioned_option(self, option: str) -> bool:
        """Restore a pre-schema label using its historical meaning."""
        value = self._value_for_option(option, unversioned=True)
        if value is None:
            return False
        if self.entity_description.setting_key == "suction":
            value = normalize_fan_level_for_model(
                self.coordinator.config_entry.data,
                value,
            )
        self.coordinator.set_room_clean_setting(
            self._room_id,
            self.entity_description.attr,
            value,
            map_id=self._map_id,
        )
        self.coordinator.async_update_listeners()
        return True

    def _apply_option(self, option: str) -> None:
        """Store a room profile option."""
        value = self._value_for_option(option)
        if value is None:
            raise HomeAssistantError(f"Unsupported Narwal room option: {option}")
        self.coordinator.set_room_clean_setting(
            self._room_id,
            self.entity_description.attr,
            value,
            map_id=self._map_id,
        )

    @property
    def _suction_map(self) -> dict[str, FanLevel]:
        """Return the model-specific room suction map."""
        return _legacy_suction_map_for(self.coordinator.config_entry.data)

    @property
    def _suction_labels(self) -> dict[FanLevel, str]:
        """Return the model-specific room suction labels."""
        return _legacy_suction_labels_for(self.coordinator.config_entry.data)

    async def async_select_option(self, option: str) -> None:
        """Apply a room profile option."""
        if getattr(self.coordinator, "_room_profile_store_loaded", True) is False:
            raise HomeAssistantError("Narwal room profiles are not restored yet")
        requested_option = option
        option = self._normalise_option(option) or ""
        if not option:
            raise HomeAssistantError(f"Unsupported Narwal room option: {requested_option}")
        if not self._room_exists:
            raise HomeAssistantError("Narwal room is not available")
        if not can_edit_pending_clean_settings(self.coordinator.data):
            raise HomeAssistantError("Narwal room profiles cannot be changed right now")

        key = self.entity_description.setting_key
        mode = self._selected_mode
        if key == "water" and mode not in LEGACY_MOP_MODES:
            raise HomeAssistantError("Water level is not available in vacuum-only mode")
        if key == "scrub" and mode not in LEGACY_MOP_MODES:
            raise HomeAssistantError("Scrub level is not available in vacuum-only mode")
        if key == "suction" and mode not in LEGACY_VACUUM_MODES:
            raise HomeAssistantError("Suction is not available in mop-only mode")

        self._apply_option(option)
        self.async_write_ha_state()
        self.coordinator.async_update_listeners()


class LegacyNarwalSettingSelect(NarwalEntity, RestoreEntity, SelectEntity):
    """Backwards-compatible selects for existing dashboards and scripts."""

    entity_description: LegacyNarwalSelectEntityDescription

    def __init__(
        self,
        coordinator: NarwalCoordinator,
        description: LegacyNarwalSelectEntityDescription,
    ) -> None:
        """Initialize the legacy select."""
        super().__init__(coordinator)
        self.entity_description = description
        device_id = coordinator.config_entry.data["device_id"]
        self._attr_unique_id = f"{device_id}_{description.key}"
        self._attr_icon = description.icon
        self._attr_entity_category = EntityCategory.CONFIG
        if description.setting_key == "suction":
            self._attr_options = [
                "AI",
                *fan_speed_list_for(coordinator.config_entry.data),
            ]
        else:
            self._attr_options = list(description.setting_options)
        self._option = description.default_option

    async def async_added_to_hass(self) -> None:
        """Restore legacy-only settings without competing with primary entities."""
        await super().async_added_to_hass()
        key = self.entity_description.setting_key
        if key in {"route", "suction"}:
            extra = await self.async_get_last_extra_data()
            if extra is not None:
                data = extra.as_dict()
                raw_value = data.get("value")
                if (
                    data.get("version") == 1
                    and isinstance(raw_value, int)
                    and not isinstance(raw_value, bool)
                    and self._apply_restored_raw_value(raw_value)
                ):
                    return
            last = await self.async_get_last_state()
            if last is not None and self._apply_unversioned_restore(last.state):
                return
        self._option = self._option_from_settings()

    @property
    def extra_restore_state_data(self) -> ExtraStoredData:
        """Return the pending value even when this select is unavailable."""
        return SettingRestoreData(value=self._raw_setting_value)

    @property
    def available(self) -> bool:
        """Return True when this legacy setting can be changed now."""
        return self._setting_available()

    @property
    def options(self) -> list[str]:
        """Return the static option list for Home Assistant capabilities."""
        return list(self._attr_options or [])

    @property
    def current_option(self) -> str | None:
        """Return the current selected option."""
        return self._option_from_settings()

    @property
    def _selected_mode(self) -> str:
        """Return the selected legacy clean mode."""
        return (
            LEGACY_MODE_LABELS.get(self.coordinator.clean_settings.work_mode)
            or "Vacuum and mop"
        )

    def _mode_for_applicability(self, *, live: bool = False) -> str | None:
        """Return the mode label used for pending or live setting applicability."""
        if not live:
            return self._selected_mode
        mode = self.coordinator.clean_setting_applicability_mode(live=True)
        if mode is None:
            return None
        return LEGACY_MODE_LABELS.get(mode) or self._selected_mode

    def _setting_applies_to_mode(self, key: str, *, live: bool = False) -> bool:
        """Return whether the legacy setting applies to the selected mode."""
        mode = self._mode_for_applicability(live=live)
        if mode is None:
            return key not in {"suction", "water", "scrub"}
        if key == "water" and mode not in LEGACY_MOP_MODES:
            return False
        if key == "scrub" and mode not in LEGACY_MOP_MODES:
            return False
        return not (key == "suction" and mode not in LEGACY_VACUUM_MODES)

    @property
    def _is_cleaning_or_paused(self) -> bool:
        """Return True while the robot is in an active clean session."""
        return is_live_clean_setting_available(self.coordinator.data)

    def _setting_available(self) -> bool:
        """Return whether this setting is currently meaningful and actionable."""
        key = self.entity_description.setting_key
        state = self.coordinator.data
        setup_available = (
            can_edit_pending_clean_settings(state)
            and not self.coordinator.has_selected_clean_rooms()
        )
        live_available = super().available and is_live_clean_setting_available(state)
        setup_applies = self._setting_applies_to_mode(key)
        if key in LEGACY_START_ONLY_SETTINGS:
            return setup_available and setup_applies
        live_applies = self._setting_applies_to_mode(key, live=True)
        return (
            (setup_available and setup_applies)
            or (live_available and live_applies)
        )

    def _apply_option(self, option: str) -> None:
        """Store a legacy option and mirror it into clean settings."""
        self._option = option
        key = self.entity_description.setting_key
        settings = self.coordinator.clean_settings
        if key == "mode":
            settings.work_mode = LEGACY_MODE_MAP[option]
            self.coordinator._legacy_mode_option = option
        elif key == "suction":
            settings.fan = self._suction_map[option]
        elif key == "water":
            settings.water = LEGACY_WATER_MAP[option]
        elif key == "scrub":
            settings.mop_strength = LEGACY_SCRUB_MAP[option]
        elif key == "route":
            settings.route = LEGACY_ROUTE_MAP[option]
        elif key == "passes":
            settings.passes = int(option)

    @property
    def _raw_setting_value(self) -> int:
        """Return the stable robot value for this setting."""
        key = self.entity_description.setting_key
        settings = self.coordinator.clean_settings
        if key == "mode":
            return int(settings.work_mode)
        if key == "suction":
            return int(settings.fan)
        if key == "water":
            return int(settings.water)
        if key == "scrub":
            return int(settings.mop_strength)
        if key == "route":
            return int(settings.route)
        return settings.passes

    def _apply_restored_raw_value(self, raw_value: int) -> bool:
        """Restore the legacy-owned global settings from a stable value."""
        key = self.entity_description.setting_key
        if key == "suction":
            values = {int(value): value for value in FanLevel}
            if (value := values.get(raw_value)) is None:
                return False
            self.coordinator.clean_settings.fan = normalize_fan_level_for_model(
                self.coordinator.config_entry.data,
                value,
            )
        elif key == "route":
            values = {int(value): value for value in CleaningRoute}
            if (value := values.get(raw_value)) is None:
                return False
            self.coordinator.clean_settings.route = value
        else:
            return False
        self._option = self._option_from_settings()
        self.coordinator.async_update_listeners()
        return True

    def _apply_unversioned_restore(self, option: str) -> bool:
        """Restore labels using their v1.0.5 meanings."""
        key = self.entity_description.setting_key
        if key == "suction":
            value = UNVERSIONED_FAN_SPEED_MAP.get(option)
            if value is None:
                return False
            self.coordinator.clean_settings.fan = normalize_fan_level_for_model(
                self.coordinator.config_entry.data,
                value,
            )
        elif key == "route":
            value = LEGACY_ROUTE_MAP.get(option)
            if value is None:
                return False
            self.coordinator.clean_settings.route = value
        else:
            return False
        self._option = self._option_from_settings()
        self.coordinator.async_update_listeners()
        return True

    def _option_from_settings(self) -> str:
        """Return this legacy option from the shared clean settings."""
        key = self.entity_description.setting_key
        settings = self.coordinator.clean_settings
        if key == "mode":
            option = LEGACY_MODE_LABELS.get(settings.work_mode)
        elif key == "suction":
            value = self.coordinator.active_clean_setting("fan")
            option = self._suction_labels.get(
                value if value is not None else settings.fan
            )
        elif key == "water":
            value = self.coordinator.active_clean_setting("water")
            option = {value: label for label, value in LEGACY_WATER_MAP.items()}.get(
                value if value is not None else settings.water
            )
        elif key == "scrub":
            option = {value: label for label, value in LEGACY_SCRUB_MAP.items()}.get(
                settings.mop_strength
            )
        elif key == "route":
            option = {value: label for label, value in LEGACY_ROUTE_MAP.items()}.get(
                settings.route
            )
        elif key == "passes":
            option = str(settings.passes)
        else:
            option = None
        return self._normalise_option(option or "") or self.entity_description.default_option

    def _normalise_option(self, option: str) -> str | None:
        """Return a current option, accepting old hidden suction aliases."""
        if option in self.options:
            return option
        if self.entity_description.setting_key != "suction":
            return None
        label = self._suction_labels.get(self._suction_map.get(option))
        return label if label in self.options else None

    async def async_select_option(self, option: str) -> None:
        """Apply a legacy setting option."""
        requested_option = option
        option = self._normalise_option(option) or ""
        if not option:
            raise HomeAssistantError(f"Unsupported Narwal option: {requested_option}")

        key = self.entity_description.setting_key
        state = self.coordinator.data
        has_selected_rooms = self.coordinator.has_selected_clean_rooms()
        setup_available = (
            can_edit_pending_clean_settings(state)
            and not has_selected_rooms
        )
        live_available = super().available and is_live_clean_setting_available(state)
        setup_applies = self._setting_applies_to_mode(key)
        live_applies = self._setting_applies_to_mode(key, live=True)
        if not (
            (setup_available and setup_applies)
            or (
                key not in LEGACY_START_ONLY_SETTINGS
                and live_available
                and live_applies
            )
        ):
            if key in {"water", "scrub"} and not setup_applies and not live_applies:
                raise HomeAssistantError(
                    f"{self.entity_description.name} is not available in vacuum-only mode"
                )
            if key == "suction" and not setup_applies and not live_applies:
                raise HomeAssistantError("Suction is not available in mop-only mode")
            raise HomeAssistantError("This Narwal setting cannot be changed right now")
        if key in LEGACY_START_ONLY_SETTINGS and not setup_available:
            raise HomeAssistantError("This Narwal setting cannot be changed right now")
        if key not in LEGACY_START_ONLY_SETTINGS and not setup_available and not live_available:
            raise HomeAssistantError("This Narwal setting cannot be changed right now")
        if key == "suction" and option == "AI" and live_available and not setup_available:
            raise HomeAssistantError("AI suction cannot be selected mid-clean")
        response = None
        live_value = None
        if live_available and live_applies and not setup_available:
            if key == "suction":
                live_value = fan_level_for_live_command(self._suction_map[option])
                response = await self.coordinator.client.set_fan_speed(live_value)
            elif key == "water":
                live_value = LEGACY_WATER_MAP[option]
                response = await self.coordinator.client.set_mop_humidity(
                    live_value
                )

        if response is not None and not response.accepted:
            try:
                result_name = CommandResult(response.result_code).name
            except ValueError:
                result_name = f"UNKNOWN({response.result_code})"
            raise HomeAssistantError(
                f"Narwal setting command failed: {result_name}"
            )

        if response is not None:
            attr = "fan" if key == "suction" else "water"
            self.coordinator.set_active_clean_setting(attr, live_value)

        if not has_selected_rooms:
            self._apply_option(option)
        self.async_write_ha_state()
        self.coordinator.async_update_listeners()

    @property
    def _suction_map(self) -> dict[str, FanLevel]:
        """Return the model-specific legacy suction map."""
        return _legacy_suction_map_for(self.coordinator.config_entry.data)

    @property
    def _suction_labels(self) -> dict[FanLevel, str]:
        """Return the model-specific legacy suction labels."""
        return _legacy_suction_labels_for(self.coordinator.config_entry.data)
