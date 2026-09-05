"""Switch entities for Narwal dock tasks, room selection, and map display options."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import slugify

from . import NarwalConfigEntry
from .const import (
    CONF_SHOW_FURNITURE,
    CONF_SHOW_FURNITURE_LABELS,
    CONF_SHOW_ROOM_LABELS,
    MAP_OPTION_DEFAULTS,
)
from .coordinator import NarwalCoordinator, can_edit_pending_clean_settings
from .dock_tasks import (
    DOCK_TASKS,
    can_start_dock_task,
    can_stop_dock_task,
    is_robot_work_context,
)
from .entity import NarwalDockEntity, NarwalEntity
from .narwal_client import CommandResponse, CommandResult


@dataclass(frozen=True, kw_only=True)
class NarwalDockTaskSwitchEntityDescription(SwitchEntityDescription):
    """Description for a Narwal dock task switch."""

    action: str
    icon: str


DOCK_TASK_SWITCHES: tuple[NarwalDockTaskSwitchEntityDescription, ...] = tuple(
    NarwalDockTaskSwitchEntityDescription(
        key=task.key,
        translation_key=task.translation_key,
        action=task.action,
        icon=task.icon,
    )
    for task in DOCK_TASKS
)


@dataclass(frozen=True, kw_only=True)
class NarwalMapSwitchEntityDescription(SwitchEntityDescription):
    """Description for a Narwal map display switch."""

    default: bool


MAP_SWITCHES: tuple[NarwalMapSwitchEntityDescription, ...] = (
    NarwalMapSwitchEntityDescription(
        key=CONF_SHOW_ROOM_LABELS,
        translation_key=CONF_SHOW_ROOM_LABELS,
        default=MAP_OPTION_DEFAULTS[CONF_SHOW_ROOM_LABELS],
    ),
    NarwalMapSwitchEntityDescription(
        key=CONF_SHOW_FURNITURE,
        translation_key=CONF_SHOW_FURNITURE,
        default=MAP_OPTION_DEFAULTS[CONF_SHOW_FURNITURE],
    ),
    NarwalMapSwitchEntityDescription(
        key=CONF_SHOW_FURNITURE_LABELS,
        translation_key=CONF_SHOW_FURNITURE_LABELS,
        default=MAP_OPTION_DEFAULTS[CONF_SHOW_FURNITURE_LABELS],
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Narwal switch entities."""
    coordinator = entry.runtime_data
    known_room_selections: dict[tuple[str | None, int], NarwalRoomSelectionSwitch] = {}

    @callback
    def async_add_room_selection_entities() -> None:
        map_data = coordinator.client.state.map_data
        if map_data is None:
            return
        map_id = coordinator.room_settings_map_id(map_data)
        entities: list[NarwalRoomSelectionSwitch] = []
        current_room_ids: set[int] = set()
        for room in sorted(map_data.rooms, key=lambda item: item.display_name.lower()):
            if room.room_id <= 0:
                continue
            current_room_ids.add(room.room_id)
            key = (map_id, room.room_id)
            if key in known_room_selections:
                known_room_selections[key].async_update_room_name(room.display_name)
                continue
            entity = NarwalRoomSelectionSwitch(
                coordinator,
                room.room_id,
                room.display_name,
                map_id=map_id,
            )
            known_room_selections[key] = entity
            entities.append(entity)
        for room_id in sorted(
            coordinator.selected_clean_rooms.get(map_id, set()) - current_room_ids
        ):
            key = (map_id, room_id)
            if key in known_room_selections:
                continue
            entity = NarwalRoomSelectionSwitch(
                coordinator,
                room_id,
                f"Room {room_id}",
                map_id=map_id,
            )
            known_room_selections[key] = entity
            entities.append(entity)
        if entities:
            async_add_entities(entities)

    async_add_entities(
        NarwalDockTaskSwitch(coordinator, description)
        for description in DOCK_TASK_SWITCHES
    )
    async_add_entities(
        NarwalMapOptionSwitch(coordinator, description)
        for description in MAP_SWITCHES
    )
    async_add_room_selection_entities()
    entry.async_on_unload(coordinator.async_add_listener(async_add_room_selection_entities))


def _format_duration(seconds: int) -> str:
    """Return a short human-readable duration."""
    minutes = _duration_minutes(seconds)
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def _duration_minutes(seconds: int) -> int:
    """Return remaining duration rounded up to whole minutes."""
    if seconds <= 0:
        return 0
    return (seconds + 59) // 60


def _accepted_response(response: CommandResponse) -> bool:
    """Return true for response codes that mean the robot accepted a command."""
    return response.accepted


class NarwalDockTaskSwitch(NarwalDockEntity, SwitchEntity):
    """Stateful start/stop control for one Narwal dock task."""

    entity_description: NarwalDockTaskSwitchEntityDescription

    def __init__(
        self,
        coordinator: NarwalCoordinator,
        description: NarwalDockTaskSwitchEntityDescription,
    ) -> None:
        """Initialize the dock task switch."""
        super().__init__(coordinator)
        self.entity_description = description
        device_id = coordinator.config_entry.data["device_id"]
        self._attr_unique_id = f"{device_id}_{description.key}"
        self._attr_icon = description.icon

    @property
    def is_on(self) -> bool | None:
        """Return whether this dock task is active."""
        state = self.coordinator.data
        if state is None:
            return None
        return self.entity_description.key in state.active_dock_task_keys

    @property
    def available(self) -> bool:
        """Return True when this dock task can be started or stopped."""
        if not self.coordinator.client.connected:
            return False
        state = self.coordinator.data
        if state is None:
            return False
        if not self.coordinator.has_fresh_state:
            # Keep the service reachable so it can wake the robot and replace a
            # stale cached decision with an authoritative dock refresh.
            return True
        if self.is_on:
            return can_stop_dock_task(state, self.entity_description.key)
        return can_start_dock_task(state, self.entity_description.key)

    @property
    def extra_state_attributes(self) -> dict[str, str | int] | None:
        """Return task progress attributes from typed dock telemetry."""
        state = self.coordinator.data
        if state is None or not self.is_on:
            return None
        timer = state.dock_task_timer(self.entity_description.key)
        if timer is None:
            return None
        return {
            "time_left": _format_duration(timer.remaining),
            "progress": timer.progress_percent,
        }

    async def async_turn_on(self, **kwargs) -> None:
        """Start this dock task."""
        async with self.coordinator.dock_action_lock:
            client = self.coordinator.client
            stale_state = not self.coordinator.has_fresh_state
            force_wake = stale_state and not is_robot_work_context(client.state)
            if not client.robot_awake or force_wake:
                await client.wake(timeout=10.0, force=force_wake)
            if not await self.coordinator.async_refresh_dock_status():
                raise HomeAssistantError("Narwal dock status could not be refreshed")
            if self.is_on:
                return
            if not can_start_dock_task(client.state, self.entity_description.key):
                raise HomeAssistantError("Narwal dock task cannot be started right now")

            command: Callable[[], Awaitable[CommandResponse]] = getattr(
                client,
                self.entity_description.action,
            )
            response = await command()
            self._raise_if_command_failed(response, "start")
            self.coordinator.async_set_updated_data(client.state)
            await self.coordinator.async_refresh_dock_status()

    async def async_turn_off(self, **kwargs) -> None:
        """Stop this dock task."""
        async with self.coordinator.dock_action_lock:
            client = self.coordinator.client
            stale_state = not self.coordinator.has_fresh_state
            force_wake = stale_state and not is_robot_work_context(client.state)
            if not client.robot_awake or force_wake:
                await client.wake(timeout=10.0, force=force_wake)
            if not await self.coordinator.async_refresh_dock_status():
                raise HomeAssistantError("Narwal dock status could not be refreshed")
            if self.entity_description.key not in client.state.active_dock_task_keys:
                return
            if not can_stop_dock_task(client.state, self.entity_description.key):
                raise HomeAssistantError("Narwal dock task cannot be stopped right now")

            response = await client.stop_dock_task(self.entity_description.key)
            self._raise_if_command_failed(response, "stop")
            self.coordinator.async_set_updated_data(client.state)
            await self.coordinator.async_refresh_dock_status()

    def _raise_if_command_failed(self, response: CommandResponse, action: str) -> None:
        """Raise a Home Assistant service error for rejected dock commands."""
        if _accepted_response(response):
            return
        try:
            result_name = CommandResult(response.result_code).name
        except (TypeError, ValueError):
            result_name = f"UNKNOWN({response.result_code})"
        raise HomeAssistantError(
            f"Narwal {action} {self.entity_description.key} failed: {result_name}"
        )


class NarwalRoomSelectionSwitch(NarwalEntity, SwitchEntity):
    """Room inclusion switch for the next native vacuum start command."""

    _attr_entity_registry_visible_default = False
    _attr_icon = "mdi:floor-plan"

    def __init__(
        self,
        coordinator: NarwalCoordinator,
        room_id: int,
        room_name: str,
        *,
        map_id: str | None = None,
    ) -> None:
        """Initialize a room selection switch."""
        super().__init__(coordinator)
        self._map_id = map_id
        self._room_id = room_id
        self._room_name = room_name
        device_id = coordinator.config_entry.data["device_id"]
        map_prefix = f"map_{slugify(map_id)}_" if map_id is not None else ""
        self._attr_unique_id = f"{device_id}_{map_prefix}room_{room_id}_selected"
        self._attr_suggested_object_id = (
            f"{slugify(coordinator.config_entry.title)}_room_{slugify(room_name)}"
        )
        self._attr_name = f"{room_name} selected"

    @callback
    def async_update_room_name(self, room_name: str) -> None:
        """Update display metadata when the map renames this room."""
        if room_name == self._room_name:
            return
        self._room_name = room_name
        self._attr_name = f"{room_name} selected"
        self._attr_suggested_object_id = (
            f"{slugify(self.coordinator.config_entry.title)}_room_{slugify(room_name)}"
        )
        if getattr(self, "hass", None) is not None:
            self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        """Return whether this room is selected for the next vacuum start."""
        return self.coordinator.is_room_selected_for_clean(
            self._room_id,
            map_id=self._map_id,
        )

    @property
    def available(self) -> bool:
        """Return True when next-clean room selection can be changed."""
        return (
            can_edit_pending_clean_settings(self.coordinator.data)
            and (self._room_exists or (self._is_current_map and self.is_on))
        )

    @property
    def extra_state_attributes(self) -> dict[str, str | int]:
        """Return room metadata for dashboards and automations."""
        return {
            "room_id": self._room_id,
            "room_name": self._room_name,
            "map_id": self._map_id or "",
        }

    @property
    def _room_exists(self) -> bool:
        """Return True when the room still exists in the current map."""
        state = self.coordinator.data
        map_data = getattr(state, "map_data", None) if state is not None else None
        if not self._is_current_map:
            return False
        rooms = getattr(map_data, "rooms", None)
        if not isinstance(rooms, (list, tuple)):
            return True
        return any(room.room_id == self._room_id for room in rooms)

    @property
    def _is_current_map(self) -> bool:
        """Return True when this switch belongs to the currently loaded map."""
        state = self.coordinator.data
        map_data = getattr(state, "map_data", None) if state is not None else None
        return (
            map_data is not None
            and self.coordinator.room_settings_map_id(map_data) == self._map_id
        )

    async def async_turn_on(self, **kwargs) -> None:
        """Select this room for the next native vacuum start."""
        if not self.available or not self._room_exists:
            raise HomeAssistantError("Narwal room selection cannot be changed right now")
        self.coordinator.set_room_selected_for_clean(
            self._room_id,
            True,
            map_id=self._map_id,
        )
        self.async_write_ha_state()
        self.coordinator.async_update_listeners()

    async def async_turn_off(self, **kwargs) -> None:
        """Remove this room from the next native vacuum start."""
        if not self.available:
            raise HomeAssistantError("Narwal room selection cannot be changed right now")
        self.coordinator.set_room_selected_for_clean(
            self._room_id,
            False,
            map_id=self._map_id,
        )
        self.async_write_ha_state()
        self.coordinator.async_update_listeners()


class NarwalMapOptionSwitch(NarwalEntity, SwitchEntity):
    """Persistent map display switch backed by config entry options."""

    entity_description: NarwalMapSwitchEntityDescription
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: NarwalCoordinator,
        description: NarwalMapSwitchEntityDescription,
    ) -> None:
        """Initialize a map option switch."""
        super().__init__(coordinator)
        self.entity_description = description
        device_id = coordinator.config_entry.data["device_id"]
        self._attr_unique_id = f"{device_id}_map_{description.key}"

    @property
    def is_on(self) -> bool:
        """Return the current map option value."""
        return bool(
            self.coordinator.config_entry.options.get(
                self.entity_description.key,
                self.entity_description.default,
            )
        )

    async def async_turn_on(self, **kwargs) -> None:
        """Turn the map option on."""
        await self._async_set_option(True)

    async def async_turn_off(self, **kwargs) -> None:
        """Turn the map option off."""
        await self._async_set_option(False)

    async def _async_set_option(self, value: bool) -> None:
        """Persist the map option and notify camera listeners."""
        entry = self.coordinator.config_entry
        options = dict(entry.options)
        options[self.entity_description.key] = value
        self.hass.config_entries.async_update_entry(entry, options=options)
        self.async_write_ha_state()
        if self.coordinator.data is not None:
            self.coordinator.async_update_listeners()
