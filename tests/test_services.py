"""Tests for Narwal domain services."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import tests.ha_stubs  # noqa: E402

tests.ha_stubs.install()

from homeassistant.exceptions import HomeAssistantError  # noqa: E402

from custom_components.narwal import (  # noqa: E402
    FIELD_MODE,
    FIELD_MOP_STRENGTH,
    FIELD_PASSES,
    FIELD_ROOMS,
    FIELD_ROUTE,
    FIELD_SUCTION,
    FIELD_WATER,
    _async_register_services,
    _async_room_ids_for_coordinator,
    _async_validate_clean_rooms_targets,
    _normalise_room_ids,
    _suction_for_coordinator,
    _validate_pass_count,
)
from custom_components.narwal.const import (  # noqa: E402
    DOMAIN,
    SERVICE_CLEAN_ROOMS,
    SERVICE_SET_PENDING_PROFILES,
)
from custom_components.narwal.coordinator import (  # noqa: E402
    CleanSettings,
    NarwalCoordinator,
    can_start_cleaning,
)
from custom_components.narwal.narwal_client import (  # noqa: E402
    CommandResponse,
    CommandResult,
    FanLevel,
    MapData,
    MopHumidity,
    NarwalCommandError,
    NarwalState,
    RoomCleanSettings,
    RoomInfo,
    WorkingStatus,
    WorkMode,
)


def _coordinator(
    *,
    docked: bool = True,
    result_code: int = CommandResult.SUCCESS,
    product_key: str = "QoEsI5qYXO",
) -> NarwalCoordinator:
    entry = MagicMock()
    entry.entry_id = "service-test"
    entry.data = {
        "host": "127.0.0.1",
        "port": 9002,
        "device_id": "service-test",
        "product_key": product_key,
    }
    hass = MagicMock()
    coordinator = NarwalCoordinator(hass, entry)
    coordinator.hass = hass
    coordinator.config_entry = entry
    entry.async_create_background_task.side_effect = lambda _hass, coro, _name: (
        coro.close(),
        MagicMock(),
    )[1]
    state = NarwalState()
    if docked:
        state.update_from_base_status({"3": {"1": 10, "10": 1}})
    state.map_data = MapData(map_id=77, rooms=[RoomInfo(room_id=4), RoomInfo(room_id=7)])
    coordinator.client = MagicMock()
    coordinator.client.robot_awake = True
    coordinator.client.state = state
    coordinator.client.get_map = AsyncMock(return_value=state.map_data)
    coordinator.client.start_rooms = AsyncMock(
        return_value=CommandResponse(result_code=result_code)
    )
    coordinator.async_refresh_dock_status = AsyncMock(return_value=True)

    async def prepare_clean_start(*, allow_dock_stop: bool = True) -> bool:
        return coordinator.client.state.is_docked and (
            allow_dock_stop or not coordinator.client.state.active_dock_task_keys
        )

    coordinator.async_prepare_clean_start = AsyncMock(side_effect=prepare_clean_start)
    coordinator.dock_action_lock = asyncio.Lock()
    coordinator._room_selection_store_loaded = True
    coordinator._room_profile_store_loaded = True
    coordinator._room_order_store_loaded = True
    coordinator.clean_settings = CleanSettings()
    coordinator.room_clean_settings = {}
    coordinator.room_clean_settings_customized = {}
    coordinator.active_clean_work_mode = None
    coordinator.data = state
    coordinator.async_set_updated_data = MagicMock()
    coordinator.async_update_listeners = MagicMock()
    coordinator.async_clear_map_display_cache = AsyncMock()
    return coordinator


def _register_clean_rooms_handler(coordinator: NarwalCoordinator):
    """Register the test service handler for a coordinator."""
    hass = MagicMock()
    coordinator.config_entry.entry_id = "entry-1"
    hass.data = {DOMAIN: {"entry-1": coordinator}}
    hass.services.has_service.return_value = False
    handlers = {}

    def register_service(domain: str, service_name: str, handler, **kwargs) -> None:
        handlers[(domain, service_name)] = handler

    hass.services.async_register.side_effect = register_service
    registry = MagicMock()
    registry.async_get.return_value = SimpleNamespace(
        config_entry_id="entry-1",
        platform=DOMAIN,
    )
    _async_register_services(hass)
    return handlers[(DOMAIN, SERVICE_CLEAN_ROOMS)], registry


def _register_pending_profiles_handler(coordinator: NarwalCoordinator):
    """Register the pending mutation service against one real coordinator."""
    hass = MagicMock()
    coordinator.config_entry.entry_id = "entry-1"
    hass.data = {DOMAIN: {"entry-1": coordinator}}
    hass.services.has_service.return_value = False

    def get_state(entity_id: str):
        if entity_id.startswith("switch.room_"):
            room_id = int(entity_id.rsplit("_", 1)[1])
            return SimpleNamespace(
                state="off",
                attributes={"room_id": room_id, "map_id": "77"},
            )
        return object()

    hass.states.get.side_effect = get_state
    handlers = {}

    def register_service(domain: str, service_name: str, handler, **kwargs) -> None:
        handlers[(domain, service_name)] = handler

    hass.services.async_register.side_effect = register_service
    registry = MagicMock()
    registry.async_get.return_value = SimpleNamespace(
        config_entry_id="entry-1",
        platform=DOMAIN,
    )
    _async_register_services(hass)
    return handlers[(DOMAIN, SERVICE_SET_PENDING_PROFILES)], registry


def _pending_profiles_call() -> SimpleNamespace:
    """Build one complete two-room pending-only request."""
    mop = {
        "mode": "Vacuum and mop",
        "suction": "Ultra Powerful",
        "water": "Wet",
        "scrub": "High",
        "passes": "2",
        "route": "Meticulous",
    }
    return SimpleNamespace(
        context=SimpleNamespace(user_id=None),
        data={
            "entity_id": ["vacuum.downstairs_narwal_vacuum"],
            "global_profile": mop,
            "room_profiles": [
                {
                    "entity_id": "switch.room_4",
                    "profile": dict(mop, mode="Vacuum then mop"),
                },
                {
                    "entity_id": "switch.room_7",
                    "profile": dict(mop, mode="Vacuum then mop"),
                },
            ],
            "selected_room_entities": [],
        },
    )


def _clean_rooms_call() -> SimpleNamespace:
    """Return a service call with non-default settings."""
    return SimpleNamespace(
        context=SimpleNamespace(user_id=None),
        data={
            FIELD_ROOMS: [4, 7],
            FIELD_MODE: "vacuum_and_mop",
            FIELD_SUCTION: "strong",
            FIELD_WATER: "wet",
            FIELD_MOP_STRENGTH: "high",
            FIELD_PASSES: 2,
            FIELD_ROUTE: "standard",
        },
    )


@pytest.mark.parametrize("initial_mode", [WorkMode.VACUUM, WorkMode.MOP])
@pytest.mark.parametrize(
    "status",
    [WorkingStatus.STANDBY, WorkingStatus.DOCKED, WorkingStatus.DOCKED_V2, WorkingStatus.CHARGED],
)
async def test_pending_profile_service_commits_idle_profiles_without_client_commands(
    initial_mode: WorkMode,
    status: WorkingStatus,
) -> None:
    """The bulk service commits pending defaults from either starting mode."""
    coordinator = _coordinator()
    coordinator.data.working_status = status
    coordinator.clean_settings.work_mode = initial_mode
    coordinator.room_clean_settings[("other-map", 99)] = RoomCleanSettings(water=MopHumidity.DRY)
    coordinator.room_clean_settings_customized[("other-map", 99)] = {"water"}
    coordinator.room_clean_orders["other-map"] = [99, 98]
    coordinator.selected_clean_rooms["other-map"] = {99}
    coordinator.client.set_mop_humidity = AsyncMock()
    coordinator.client.set_fan_speed = AsyncMock()
    coordinator.client.start_rooms = AsyncMock()
    coordinator.client.sent_vendor_commands = []
    handler, registry = _register_pending_profiles_handler(coordinator)
    call = _pending_profiles_call()

    with (
        patch(
            "custom_components.narwal.service.async_extract_entity_ids",
            new_callable=AsyncMock,
            return_value=["vacuum.downstairs_narwal_vacuum"],
        ),
        patch("custom_components.narwal.er.async_get", return_value=registry),
    ):
        await handler(call)

    assert coordinator.clean_settings.work_mode == WorkMode.VACUUM_AND_MOP
    assert coordinator.clean_settings.fan == FanLevel.SUPER
    assert coordinator.clean_settings.passes == 2
    assert coordinator.room_clean_settings[("77", 4)].work_mode == WorkMode.VACUUM_THEN_MOP
    assert coordinator.room_clean_settings_customized[("77", 4)] == {
        "work_mode",
        "fan",
        "water",
        "mop_strength",
        "passes",
        "route",
    }
    assert coordinator.room_clean_settings[("other-map", 99)].water == MopHumidity.DRY
    assert coordinator.room_clean_settings_customized[("other-map", 99)] == {"water"}
    assert coordinator.room_clean_orders["other-map"] == [99, 98]
    assert coordinator.selected_clean_rooms["other-map"] == {99}
    coordinator.config_entry.async_create_background_task.assert_called_once()
    coordinator.async_update_listeners.assert_called_once_with()
    coordinator.async_set_updated_data.assert_not_called()
    assert coordinator.selected_clean_rooms.get("77") is None
    coordinator.client.set_mop_humidity.assert_not_awaited()
    coordinator.client.set_fan_speed.assert_not_awaited()
    coordinator.client.start_rooms.assert_not_awaited()
    assert coordinator.client.sent_vendor_commands == []


@pytest.mark.parametrize("state_kind", ["active", "paused", "unknown", "transition"])
async def test_pending_profile_service_rejects_nonidle_at_mutation_boundary(
    state_kind: str,
) -> None:
    """A late active, paused, unknown, or transition state cannot reach setters."""
    coordinator = _coordinator()
    state = coordinator.data
    if state_kind == "active":
        state.working_status = WorkingStatus.CLEANING
    elif state_kind == "paused":
        state.is_paused = True
    elif state_kind == "unknown":
        state.working_status = WorkingStatus.UNKNOWN
    else:
        state.working_status = WorkingStatus.REMAPPING
    coordinator.client.set_mop_humidity = AsyncMock()
    coordinator.client.set_fan_speed = AsyncMock()
    coordinator.client.sent_vendor_commands = []
    before = coordinator.clean_settings
    handler, registry = _register_pending_profiles_handler(coordinator)

    with (
        patch(
            "custom_components.narwal.service.async_extract_entity_ids",
            new_callable=AsyncMock,
            return_value=["vacuum.downstairs_narwal_vacuum"],
        ),
        patch("custom_components.narwal.er.async_get", return_value=registry),
        pytest.raises(HomeAssistantError, match="pending profiles|idle docked"),
    ):
        await handler(_pending_profiles_call())

    assert coordinator.clean_settings is before
    coordinator.client.set_mop_humidity.assert_not_awaited()
    coordinator.client.set_fan_speed.assert_not_awaited()
    assert coordinator.client.sent_vendor_commands == []


async def test_pending_profile_service_rechecks_map_after_dispatch_wait() -> None:
    """A map change during dispatch validation is rejected before local writes."""
    coordinator = _coordinator()
    before = coordinator.clean_settings
    handler, registry = _register_pending_profiles_handler(coordinator)

    async def change_map_then_extract(*_args):
        coordinator.data.map_data = MapData(
            map_id=88,
            rooms=[RoomInfo(room_id=4), RoomInfo(room_id=7)],
        )
        return ["vacuum.downstairs_narwal_vacuum"]

    with (
        patch(
            "custom_components.narwal.service.async_extract_entity_ids",
            new_callable=AsyncMock,
            side_effect=change_map_then_extract,
        ),
        patch("custom_components.narwal.er.async_get", return_value=registry),
        pytest.raises(HomeAssistantError, match="current map"),
    ):
        await handler(_pending_profiles_call())

    assert coordinator.clean_settings is before
    assert coordinator.room_clean_settings == {}
    assert coordinator.selected_clean_rooms == {}


async def test_pending_profile_service_validation_failure_is_atomic() -> None:
    """A bad later room profile cannot leave earlier pending values committed."""
    coordinator = _coordinator()
    coordinator.client.set_mop_humidity = AsyncMock()
    coordinator.client.set_fan_speed = AsyncMock()
    original_settings = coordinator.clean_settings
    original_profiles = dict(coordinator.room_clean_settings)
    original_customized = dict(coordinator.room_clean_settings_customized)
    original_selections = dict(coordinator.selected_clean_rooms)
    call = _pending_profiles_call()
    call.data["room_profiles"][1]["profile"]["suction"] = "unsupported-tier"
    handler, registry = _register_pending_profiles_handler(coordinator)

    with (
        patch(
            "custom_components.narwal.service.async_extract_entity_ids",
            new_callable=AsyncMock,
            return_value=["vacuum.downstairs_narwal_vacuum"],
        ),
        patch("custom_components.narwal.er.async_get", return_value=registry),
        pytest.raises(HomeAssistantError, match="Suction is not supported"),
    ):
        await handler(call)

    assert coordinator.clean_settings is original_settings
    assert coordinator.room_clean_settings == original_profiles
    assert coordinator.room_clean_settings_customized == original_customized
    assert coordinator.selected_clean_rooms == original_selections
    coordinator.client.set_mop_humidity.assert_not_awaited()
    coordinator.client.set_fan_speed.assert_not_awaited()


def test_clean_rooms_schema_uses_all_capable_entity_validator() -> None:
    """The service target validator must permit Home Assistant's `all` sentinel."""
    import voluptuous as vol
    from homeassistant.helpers import config_validation as cv

    service_schemas = [
        call.args[0]
        for call in vol.Schema.call_args_list
        if call.args and isinstance(call.args[0], dict) and FIELD_ROOMS in call.args[0]
    ]
    assert len(service_schemas) == 1
    assert service_schemas[0]["entity_id"] is cv.comp_entity_ids


async def test_clean_rooms_rejects_registry_only_target() -> None:
    """A disabled or stale registry entry cannot address a loaded coordinator."""
    hass = MagicMock()
    hass.states.get.return_value = None
    registry = MagicMock()
    registry.async_get.return_value = SimpleNamespace(
        config_entry_id="entry-1",
        platform=DOMAIN,
    )
    call = SimpleNamespace(
        context=SimpleNamespace(user_id=None),
        data={"entity_id": "vacuum.downstairs_narwal"},
    )

    with (
        patch("custom_components.narwal.er.async_get", return_value=registry),
        pytest.raises(HomeAssistantError, match="Target must be a Narwal vacuum"),
    ):
        await _async_validate_clean_rooms_targets(
            hass,
            call,
            ["vacuum.downstairs_narwal"],
        )


async def test_clean_rooms_service_starts_requested_rooms() -> None:
    """The service path starts the requested room plan."""
    coordinator = _coordinator()
    handler, registry = _register_clean_rooms_handler(coordinator)
    call = _clean_rooms_call()

    with (
        patch(
            "custom_components.narwal.service.async_extract_entity_ids",
            new_callable=AsyncMock,
            return_value=["vacuum.downstairs_narwal"],
        ) as extract_entity_ids,
        patch("custom_components.narwal.er.async_get", return_value=registry),
    ):
        await handler(call)

    extract_entity_ids.assert_awaited_once()
    assert extract_entity_ids.await_args.args[0] is call
    coordinator.client.start_rooms.assert_awaited_once()
    assert coordinator.client.start_rooms.await_args.args[0] == [4, 7]
    assert coordinator.client.state.has_assumed_robot_clean
    assert coordinator.active_clean_work_mode == WorkMode.VACUUM_AND_MOP
    assert not can_start_cleaning(coordinator.client.state)
    coordinator.async_set_updated_data.assert_called_once_with(coordinator.client.state)


async def test_clean_rooms_service_supports_legacy_entity_extractor() -> None:
    """Target extraction remains compatible with older HA helper signatures."""
    coordinator = _coordinator()
    handler, registry = _register_clean_rooms_handler(coordinator)
    call = _clean_rooms_call()

    with (
        patch(
            "custom_components.narwal.service.async_extract_entity_ids",
            new_callable=AsyncMock,
            side_effect=[
                TypeError("missing required positional argument: service_call"),
                ["vacuum.downstairs_narwal"],
            ],
        ) as extract_entity_ids,
        patch("custom_components.narwal.er.async_get", return_value=registry),
    ):
        await handler(call)

    assert len(extract_entity_ids.await_args_list) == 2
    assert extract_entity_ids.await_args_list[0].args == (call,)
    assert len(extract_entity_ids.await_args_list[1].args) == 2
    assert extract_entity_ids.await_args_list[1].args[1] is call
    coordinator.client.start_rooms.assert_awaited_once()


async def test_clean_rooms_service_accepts_async_accepted_response() -> None:
    """The robot can accept a start with code 0 before reporting completion."""
    coordinator = _coordinator(result_code=0)
    handler, registry = _register_clean_rooms_handler(coordinator)
    call = _clean_rooms_call()

    with (
        patch(
            "custom_components.narwal.service.async_extract_entity_ids",
            new_callable=AsyncMock,
            return_value=["vacuum.downstairs_narwal"],
        ),
        patch("custom_components.narwal.er.async_get", return_value=registry),
    ):
        await handler(call)

    coordinator.client.start_rooms.assert_awaited_once()
    assert coordinator.client.state.has_assumed_robot_clean
    coordinator.async_set_updated_data.assert_called_once_with(coordinator.client.state)


async def test_clean_rooms_service_uses_requested_settings_over_room_profiles() -> None:
    """Explicit service settings should not be silently overridden by room profiles."""
    coordinator = _coordinator()
    coordinator.set_room_clean_setting(4, "fan", FanLevel.MUTE)
    handler, registry = _register_clean_rooms_handler(coordinator)
    call = _clean_rooms_call()

    with (
        patch(
            "custom_components.narwal.service.async_extract_entity_ids",
            new_callable=AsyncMock,
            return_value=["vacuum.downstairs_narwal"],
        ),
        patch("custom_components.narwal.er.async_get", return_value=registry),
    ):
        await handler(call)

    kwargs = coordinator.client.start_rooms.await_args.kwargs
    assert kwargs["room_settings"][4].fan == FanLevel.STRONG
    assert kwargs["room_settings"][7].fan == FanLevel.STRONG


@pytest.mark.parametrize(
    ("product_key", "expected"),
    [
        ("QoEsI5qYXO", FanLevel.SUPER),
        ("qV6BujoYLz", FanLevel.DEEP),
    ],
)
async def test_clean_rooms_service_resolves_max_for_each_model(
    product_key: str,
    expected: FanLevel,
) -> None:
    """The max alias resolves through each model's visible tiers."""
    coordinator = _coordinator(product_key=product_key)
    handler, registry = _register_clean_rooms_handler(coordinator)
    call = _clean_rooms_call()
    call.data[FIELD_SUCTION] = "max"

    with (
        patch(
            "custom_components.narwal.service.async_extract_entity_ids",
            new_callable=AsyncMock,
            return_value=["vacuum.downstairs_narwal"],
        ),
        patch("custom_components.narwal.er.async_get", return_value=registry),
    ):
        await handler(call)

    room_settings = coordinator.client.start_rooms.await_args.kwargs["room_settings"]
    assert all(settings.fan == expected for settings in room_settings.values())


async def test_clean_rooms_service_rejects_unavailable_start() -> None:
    """The service enforces the same availability as the start entities."""
    coordinator = _coordinator(docked=False)
    handler, registry = _register_clean_rooms_handler(coordinator)

    with (
        patch(
            "custom_components.narwal.service.async_extract_entity_ids",
            new_callable=AsyncMock,
            return_value=["vacuum.downstairs_narwal"],
        ),
        patch("custom_components.narwal.er.async_get", return_value=registry),
        pytest.raises(Exception, match="cannot be started"),
    ):
        await handler(_clean_rooms_call())

    coordinator.client.start_rooms.assert_not_awaited()


async def test_clean_rooms_service_rejects_unknown_docked_status() -> None:
    """Dock fields cannot authorize a clean from an unrecognized robot state."""
    coordinator = _coordinator()
    coordinator.client.state.working_status = WorkingStatus.UNKNOWN
    handler, registry = _register_clean_rooms_handler(coordinator)

    with (
        patch(
            "custom_components.narwal.service.async_extract_entity_ids",
            new_callable=AsyncMock,
            return_value=["vacuum.downstairs_narwal"],
        ),
        patch("custom_components.narwal.er.async_get", return_value=registry),
        pytest.raises(HomeAssistantError, match="cannot be started"),
    ):
        await handler(_clean_rooms_call())

    coordinator.client.start_rooms.assert_not_awaited()


async def test_clean_rooms_service_preserves_settings_on_rejected_start() -> None:
    """Rejected starts must not alter the pending settings for the next clean."""
    coordinator = _coordinator(result_code=CommandResult.CONFLICT)
    handler, registry = _register_clean_rooms_handler(coordinator)
    original_settings = CleanSettings(**coordinator.clean_settings.__dict__)

    with (
        patch(
            "custom_components.narwal.service.async_extract_entity_ids",
            new_callable=AsyncMock,
            return_value=["vacuum.downstairs_narwal"],
        ),
        patch("custom_components.narwal.er.async_get", return_value=registry),
        pytest.raises(Exception, match="failed"),
    ):
        await handler(_clean_rooms_call())

    assert coordinator.clean_settings == original_settings
    assert not coordinator.client.state.has_assumed_robot_clean


async def test_clean_rooms_service_rejects_unsupported_ultra_suction() -> None:
    """AX26 must not silently map unsupported Ultra suction to Strong."""
    coordinator = _coordinator(product_key="qV6BujoYLz")
    handler, registry = _register_clean_rooms_handler(coordinator)
    call = _clean_rooms_call()
    call.data[FIELD_SUCTION] = "ultra"

    with (
        patch(
            "custom_components.narwal.service.async_extract_entity_ids",
            new_callable=AsyncMock,
            return_value=["vacuum.downstairs_narwal"],
        ),
        patch("custom_components.narwal.er.async_get", return_value=registry),
        pytest.raises(Exception, match="Ultra suction is not supported"),
    ):
        await handler(call)

    coordinator.client.start_rooms.assert_not_awaited()


def test_clean_rooms_ultra_alias_keeps_historical_level() -> None:
    """The hidden service alias keeps its prior SUPER enum meaning."""
    assert _suction_for_coordinator(_coordinator(), "ultra") is FanLevel.SUPER

    with pytest.raises(Exception, match="Ultra suction is not supported"):
        _suction_for_coordinator(_coordinator(product_key="qV6BujoYLz"), "ultra")


def test_clean_rooms_ultra_powerful_uses_level_five_when_supported() -> None:
    """The canonical top tier uses SUPER and remains model-gated."""
    assert _suction_for_coordinator(_coordinator(), "ultra_powerful") is FanLevel.SUPER

    with pytest.raises(Exception, match="Ultra Powerful suction is not supported"):
        _suction_for_coordinator(
            _coordinator(product_key="qV6BujoYLz"),
            "ultra_powerful",
        )


def test_clean_rooms_super_alias_keeps_historical_level() -> None:
    """The hidden service alias keeps its pre-rename DEEP enum meaning."""
    assert _suction_for_coordinator(_coordinator(), "super") is FanLevel.DEEP
    assert (
        _suction_for_coordinator(
            _coordinator(product_key="qV6BujoYLz"),
            "super",
        )
        is FanLevel.DEEP
    )


async def test_clean_rooms_service_rejects_invalid_room_ids() -> None:
    """Room IDs must be valid positive integers."""
    coordinator = _coordinator()
    handler, registry = _register_clean_rooms_handler(coordinator)
    call = _clean_rooms_call()
    call.data[FIELD_ROOMS] = [4, 0]

    with (
        patch(
            "custom_components.narwal.service.async_extract_entity_ids",
            new_callable=AsyncMock,
            return_value=["vacuum.downstairs_narwal"],
        ),
        patch("custom_components.narwal.er.async_get", return_value=registry),
        pytest.raises(Exception, match="positive"),
    ):
        await handler(call)

    coordinator.client.start_rooms.assert_not_awaited()


def test_room_id_validation_rejects_fractional_values() -> None:
    """Room IDs must not be silently truncated by int coercion."""
    with pytest.raises(Exception, match="Invalid Narwal room ID"):
        _normalise_room_ids([4.5])


def test_pass_count_validation_rejects_fractional_values() -> None:
    """Pass counts must not be silently truncated by int coercion."""
    with pytest.raises(Exception, match="integer"):
        _validate_pass_count(2.5)


async def test_clean_rooms_service_rejects_unknown_room_ids() -> None:
    """Room IDs must exist on the active map."""
    coordinator = _coordinator()
    handler, registry = _register_clean_rooms_handler(coordinator)
    call = _clean_rooms_call()
    call.data[FIELD_ROOMS] = [4, 99]

    with (
        patch(
            "custom_components.narwal.service.async_extract_entity_ids",
            new_callable=AsyncMock,
            return_value=["vacuum.downstairs_narwal"],
        ),
        patch("custom_components.narwal.er.async_get", return_value=registry),
        pytest.raises(Exception, match="Unknown Narwal room ID"),
    ):
        await handler(call)

    coordinator.client.start_rooms.assert_not_awaited()


async def test_clean_rooms_service_refreshes_room_topology() -> None:
    """Service validation uses the current map after room edits."""
    coordinator = _coordinator()
    coordinator.client.state.map_data = MapData(
        map_id=1,
        rooms=[RoomInfo(room_id=4)],
    )

    async def refresh_map() -> MapData:
        coordinator.client.state.map_data = MapData(
            map_id=1,
            rooms=[RoomInfo(room_id=7)],
        )
        return coordinator.client.state.map_data

    coordinator.client.get_map = AsyncMock(side_effect=refresh_map)

    room_ids = await _async_room_ids_for_coordinator(coordinator, [7])

    assert room_ids == [7]
    coordinator.client.get_map.assert_awaited_once()


async def test_clean_rooms_service_rejects_unloaded_target_before_starting() -> None:
    """Every explicit Narwal vacuum target must resolve to a loaded coordinator."""
    coordinator = _coordinator()
    handler, registry = _register_clean_rooms_handler(coordinator)
    registry.async_get.return_value = SimpleNamespace(
        config_entry_id="entry-missing",
        platform=DOMAIN,
    )

    with (
        patch(
            "custom_components.narwal.service.async_extract_entity_ids",
            new_callable=AsyncMock,
            return_value=["vacuum.downstairs_narwal"],
        ),
        patch("custom_components.narwal.er.async_get", return_value=registry),
        pytest.raises(Exception, match="not loaded"),
    ):
        await handler(_clean_rooms_call())

    coordinator.client.start_rooms.assert_not_awaited()


async def test_clean_rooms_service_rejects_all_mixed_with_explicit_room_ids() -> None:
    """The `all` shortcut must not be embedded with explicit room IDs."""
    coordinator = _coordinator()

    with pytest.raises(Exception, match='either "all" or explicit room IDs'):
        await _async_room_ids_for_coordinator(coordinator, ["all, 4"])


async def test_clean_rooms_service_rejects_multiple_vacuums_before_side_effects() -> None:
    """A single service call cannot partially start multiple vacuums."""
    first = _coordinator()
    first.client.robot_awake = False
    first.client.wake = AsyncMock()
    second = _coordinator()

    hass = MagicMock()
    hass.data = {DOMAIN: {"entry-1": first, "entry-2": second}}
    hass.services.has_service.return_value = False
    handlers = {}

    def register_service(domain: str, service_name: str, handler, **kwargs) -> None:
        handlers[(domain, service_name)] = handler

    hass.services.async_register.side_effect = register_service
    registry = MagicMock()
    registry.async_get.side_effect = lambda entity_id: {
        "vacuum.first_narwal": SimpleNamespace(
            config_entry_id="entry-1",
            platform=DOMAIN,
        ),
        "vacuum.second_narwal": SimpleNamespace(
            config_entry_id="entry-2",
            platform=DOMAIN,
        ),
    }[entity_id]
    _async_register_services(hass)
    handler = handlers[(DOMAIN, SERVICE_CLEAN_ROOMS)]

    with (
        patch(
            "custom_components.narwal.service.async_extract_entity_ids",
            new_callable=AsyncMock,
            return_value=["vacuum.first_narwal", "vacuum.second_narwal"],
        ),
        patch("custom_components.narwal.er.async_get", return_value=registry),
        pytest.raises(Exception, match="exactly one Narwal vacuum"),
    ):
        await handler(_clean_rooms_call())

    first.client.wake.assert_not_awaited()
    first.client.start_rooms.assert_not_awaited()
    second.client.start_rooms.assert_not_awaited()


async def test_clean_rooms_service_wakes_targets_before_start_preflight() -> None:
    """Sleeping targets are woken before state-dependent start checks run."""
    coordinator = _coordinator(docked=False)
    coordinator.client.robot_awake = False
    coordinator.client.wake = AsyncMock(
        side_effect=lambda **_: coordinator.client.state.update_from_base_status(
            {"3": {"1": 10, "10": 1}, "11": 2}
        )
    )
    handler, registry = _register_clean_rooms_handler(coordinator)

    with (
        patch(
            "custom_components.narwal.service.async_extract_entity_ids",
            new_callable=AsyncMock,
            return_value=["vacuum.downstairs_narwal"],
        ),
        patch("custom_components.narwal.er.async_get", return_value=registry),
    ):
        await handler(_clean_rooms_call())

    coordinator.client.wake.assert_awaited_once_with(timeout=10.0)
    coordinator.client.start_rooms.assert_awaited_once()


async def test_clean_rooms_service_skips_map_refresh_for_known_room_ids() -> None:
    """Explicit IDs already on the cached map need no refresh (#108)."""
    coordinator = _coordinator()
    coordinator.client.state.map_data = MapData(
        map_id=1,
        rooms=[RoomInfo(room_id=4), RoomInfo(room_id=9)],
    )
    coordinator.client.get_map = AsyncMock(
        side_effect=NarwalCommandError("No response for command 'map/get_map'")
    )

    room_ids = await _async_room_ids_for_coordinator(coordinator, [9])

    assert room_ids == [9]
    coordinator.client.get_map.assert_not_awaited()


async def test_clean_rooms_service_all_still_refreshes_map() -> None:
    """`all` resolves against a fresh map, not whatever was cached."""
    coordinator = _coordinator()
    coordinator.client.state.map_data = MapData(
        map_id=1,
        rooms=[RoomInfo(room_id=11), RoomInfo(room_id=4), RoomInfo(room_id=9)],
    )
    coordinator.client.get_map = AsyncMock()

    room_ids = await _async_room_ids_for_coordinator(coordinator, ["all"])

    assert room_ids == [4, 9, 11]
    coordinator.client.get_map.assert_awaited_once()


async def test_clean_rooms_all_appends_new_rooms_in_current_map_order() -> None:
    """A saved order is completed from the current map before new rooms append."""
    coordinator = _coordinator()
    coordinator.client.state.map_data = MapData(
        map_id=100,
        rooms=[
            RoomInfo(room_id=6),
            RoomInfo(room_id=9),
            RoomInfo(room_id=4),
            RoomInfo(room_id=7),
        ],
    )
    coordinator.room_clean_orders = {"100": [6, 4]}
    coordinator.client.get_map = AsyncMock()

    room_ids = await _async_room_ids_for_coordinator(coordinator, ["all"])

    assert room_ids == [6, 4, 9, 7]
