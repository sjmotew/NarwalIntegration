"""Tests for the clean-settings select and number entities (#50).

Importing these modules at all is the regression guard for the bad
`RestoreSelect` import; the rest exercises the value round-trip, the live
mop-humidity setter, and the RestoreEntity/RestoreNumber restore paths.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import tests.ha_stubs  # noqa: E402

tests.ha_stubs.install()

from homeassistant.components.select import SelectEntity  # noqa: E402
from homeassistant.exceptions import HomeAssistantError  # noqa: E402
from homeassistant.helpers.restore_state import RestoreEntity  # noqa: E402

from custom_components.narwal.coordinator import (  # noqa: E402
    CleanSettings,
    can_edit_pending_clean_settings,
)
from custom_components.narwal.number import NarwalPassesNumber  # noqa: E402
from custom_components.narwal.select import (  # noqa: E402
    LEGACY_SELECT_DESCRIPTIONS,
    ROOM_SELECT_DESCRIPTIONS,
    SELECT_DESCRIPTIONS,
    LegacyNarwalSettingSelect,
    NarwalSelect,
    RoomNarwalSettingSelect,
    RoomSettingRestoreData,
    SettingRestoreData,
    async_setup_entry,
)
from narwal_client import NarwalState, RoomCleanSettings  # noqa: E402
from narwal_client.const import (  # noqa: E402
    CleaningRoute,
    CommandResult,
    FanLevel,
    MopHumidity,
    MopStrengthLevel,
    WorkingStatus,
    WorkMode,
)
from narwal_client.models import (  # noqa: E402
    DOCK_TASK_DRY_DOCK_BAG,
    CommandResponse,  # noqa: E402
    MapData,
    RoomInfo,
)

_DESCS = {d.key: d for d in SELECT_DESCRIPTIONS}
_LEGACY_DESCS = {d.setting_key: d for d in LEGACY_SELECT_DESCRIPTIONS}
_ROOM_DESCS = {d.setting_key: d for d in ROOM_SELECT_DESCRIPTIONS}


def _coordinator(
    *,
    settings: CleanSettings | None = None,
    state: object | None = None,
    product_key: str = "QoEsI5qYXO",
) -> MagicMock:
    coord = MagicMock()
    coord.config_entry = MagicMock()
    coord.config_entry.data = {"device_id": "dev1", "product_key": product_key}
    coord.config_entry.title = "Narwal Test"
    coord.client = MagicMock()
    coord.client.state = MagicMock()
    coord.client.state.firmware_version = "1.0.0"
    coord.last_update_success = True
    coord.clean_settings = settings or CleanSettings()
    coord.room_clean_settings = {}
    coord.room_clean_settings_customized = {}
    coord.active_clean_work_mode = None
    coord.has_selected_clean_rooms.return_value = False
    coord.active_clean_setting.return_value = None
    coord.data = state
    coord._legacy_mode_option = "Vacuum and mop"

    def room_settings_map_id(map_data=None) -> str | None:
        if map_data is None:
            map_data = getattr(coord.data, "map_data", None) if coord.data else None
        map_id = getattr(map_data, "map_id", None)
        if map_id in (None, "", 0, "0"):
            return None
        return str(map_id)

    def room_clean_settings_for(
        room_id: int,
        *,
        map_id: str | None = None,
    ) -> RoomCleanSettings:
        key = (map_id if map_id is not None else room_settings_map_id(), room_id)
        if key not in coord.room_clean_settings:
            coord.room_clean_settings[key] = RoomCleanSettings(
                work_mode=coord.clean_settings.work_mode,
                fan=coord.clean_settings.fan,
                water=coord.clean_settings.water,
                mop_strength=coord.clean_settings.mop_strength,
                passes=coord.clean_settings.passes,
                route=coord.clean_settings.route,
            )
        return coord.room_clean_settings[key]

    def effective_room_clean_settings_for(
        room_id: int,
        *,
        map_id: str | None = None,
    ) -> RoomCleanSettings:
        key = (map_id if map_id is not None else room_settings_map_id(), room_id)
        fallback = RoomCleanSettings(
            work_mode=coord.clean_settings.work_mode,
            fan=coord.clean_settings.fan,
            water=coord.clean_settings.water,
            mop_strength=coord.clean_settings.mop_strength,
            passes=coord.clean_settings.passes,
            route=coord.clean_settings.route,
        )
        profile = coord.room_clean_settings.get(key)
        for attr in coord.room_clean_settings_customized.get(key, set()):
            setattr(fallback, attr, getattr(profile, attr))
        return fallback

    def set_room_clean_setting(
        room_id: int,
        attr: str,
        value,
        *,
        map_id: str | None = None,
    ) -> None:
        key = (map_id if map_id is not None else room_settings_map_id(), room_id)
        setattr(room_clean_settings_for(room_id, map_id=map_id), attr, value)
        coord.room_clean_settings_customized.setdefault(key, set()).add(attr)

    coord.room_settings_map_id.side_effect = room_settings_map_id
    coord.room_clean_settings_for.side_effect = room_clean_settings_for
    coord.effective_room_clean_settings_for.side_effect = effective_room_clean_settings_for
    coord.set_room_clean_setting.side_effect = set_room_clean_setting
    def clean_setting_applicability_mode(*, live: bool = False) -> WorkMode | None:
        if not live:
            return coord.clean_settings.work_mode
        if (
            getattr(coord.data, "is_cleaning", False) is True
            or getattr(coord.data, "has_recent_active_working_status", False) is True
            or getattr(coord.data, "has_paused_clean_task_context", False) is True
        ):
            return coord.active_clean_work_mode
        return coord.clean_settings.work_mode

    coord.clean_setting_applicability_mode.side_effect = clean_setting_applicability_mode
    return coord


def _state(
    working_status: WorkingStatus = WorkingStatus.DOCKED,
    *,
    recent: bool = False,
    returning: bool = False,
) -> MagicMock:
    state = MagicMock()
    state.working_status = working_status
    state.has_recent_active_working_status = recent
    state.is_returning = returning
    state.is_cleaning = working_status == WorkingStatus.CLEANING and not returning
    state.is_station_active = False
    state.has_unmapped_active_dock_task = False
    state.assumed_active_dock_task = None
    state.map_data = None
    return state


def test_select_bases_use_restore_entity() -> None:
    """The select restores via RestoreEntity (HA has no RestoreSelect)."""
    assert issubclass(NarwalSelect, RestoreEntity)
    assert issubclass(NarwalSelect, SelectEntity)


class TestNarwalSelect:
    def test_current_option_reflects_settings(self) -> None:
        coord = _coordinator(settings=CleanSettings(work_mode=WorkMode.MOP))
        sel = NarwalSelect(coord, _DESCS["work_mode"])
        assert sel.current_option == "mop"

    async def test_select_option_stores_value(self) -> None:
        coord = _coordinator(state=_state())
        coord.async_update_listeners = MagicMock()
        sel = NarwalSelect(coord, _DESCS["mop_strength"])
        await sel.async_select_option("high")
        assert coord.clean_settings.mop_strength == MopStrengthLevel.HIGH
        assert sel.current_option == "high"
        coord.async_update_listeners.assert_called_once()

    async def test_select_option_stores_pending_value_when_entity_unavailable(self) -> None:
        coord = _coordinator(state=_state())
        coord.last_update_success = False
        sel = NarwalSelect(coord, _DESCS["mop_strength"])

        await sel.async_select_option("high")

        assert coord.clean_settings.mop_strength == MopStrengthLevel.HIGH

    async def test_extra_restore_data_survives_unavailable_entity_state(self) -> None:
        coord = _coordinator(state=_state(WorkingStatus.CLEANING))
        sel = NarwalSelect(coord, _DESCS["work_mode"])
        extra = SettingRestoreData(value=int(WorkMode.MOP))

        with (
            patch.object(
                sel, "async_get_last_extra_data", AsyncMock(return_value=extra)
            ),
            patch.object(
                sel,
                "async_get_last_state",
                AsyncMock(return_value=MagicMock(state="unavailable")),
            ) as get_last_state,
        ):
            await sel.async_added_to_hass()

        get_last_state.assert_not_awaited()
        assert coord.clean_settings.work_mode == WorkMode.MOP

    def test_extra_restore_data_records_stable_robot_value(self) -> None:
        coord = _coordinator(settings=CleanSettings(water=MopHumidity.WET))
        sel = NarwalSelect(coord, _DESCS["water"])

        assert sel.extra_restore_state_data.as_dict() == {
            "version": 1,
            "value": int(MopHumidity.WET),
        }

    async def test_water_applies_live_while_cleaning(self) -> None:
        coord = _coordinator(state=_state(WorkingStatus.CLEANING))
        coord.active_clean_work_mode = WorkMode.MOP
        coord.client.set_mop_humidity = AsyncMock(
            return_value=CommandResponse(result_code=CommandResult.SUCCESS)
        )
        sel = NarwalSelect(coord, _DESCS["water"])
        await sel.async_select_option("wet")
        assert coord.clean_settings.water == MopHumidity.WET
        coord.client.set_mop_humidity.assert_awaited_once_with(MopHumidity.WET)
        coord.set_active_clean_setting.assert_called_once_with(
            "water", MopHumidity.WET
        )

    def test_whole_floor_settings_hide_when_rooms_are_selected(self) -> None:
        """An explicit room selection leaves only room-level setup controls."""
        coord = _coordinator(state=_state())
        coord.has_selected_clean_rooms.return_value = True

        assert not NarwalSelect(coord, _DESCS["work_mode"]).available
        assert not LegacyNarwalSettingSelect(coord, _LEGACY_DESCS["mode"]).available
        assert not NarwalPassesNumber(coord).available

    def test_live_controls_report_active_room_values(self) -> None:
        """Runtime suction and water reflect the dispatched room profile."""
        coord = _coordinator(state=_state(WorkingStatus.CLEANING))
        coord.has_selected_clean_rooms.return_value = True
        coord.active_clean_work_mode = WorkMode.VACUUM_AND_MOP
        coord.active_clean_setting.side_effect = lambda attr: {
            "fan": FanLevel.STRONG,
            "water": MopHumidity.WET,
        }.get(attr)

        suction = LegacyNarwalSettingSelect(coord, _LEGACY_DESCS["suction"])
        water = LegacyNarwalSettingSelect(coord, _LEGACY_DESCS["water"])

        assert suction.available
        assert suction.current_option == "Strong"
        assert water.available
        assert water.current_option == "Wet"

    async def test_water_accepts_live_accepted_response(self) -> None:
        coord = _coordinator(state=_state(WorkingStatus.CLEANING))
        coord.active_clean_work_mode = WorkMode.MOP
        coord.client.set_mop_humidity = AsyncMock(
            return_value=CommandResponse(result_code=0)
        )
        sel = NarwalSelect(coord, _DESCS["water"])

        await sel.async_select_option("wet")

        assert coord.clean_settings.water == MopHumidity.WET
        coord.client.set_mop_humidity.assert_awaited_once_with(MopHumidity.WET)

    async def test_live_water_uses_active_room_mode(self) -> None:
        """Live water gating follows the accepted room mode, not pending globals."""
        coord = _coordinator(state=_state(WorkingStatus.CLEANING))
        coord.clean_settings.work_mode = WorkMode.VACUUM
        coord.active_clean_work_mode = WorkMode.MOP
        coord.client.set_mop_humidity = AsyncMock(
            return_value=CommandResponse(result_code=0)
        )
        sel = NarwalSelect(coord, _DESCS["water"])

        await sel.async_select_option("wet")

        assert coord.clean_settings.water == MopHumidity.WET
        coord.set_active_clean_setting.assert_called_once_with(
            "water", MopHumidity.WET
        )
        coord.client.set_mop_humidity.assert_awaited_once_with(MopHumidity.WET)

    async def test_live_water_rejects_active_vacuum_mode(self) -> None:
        """Pending mop mode must not expose water for an active vacuum-only task."""
        coord = _coordinator(state=_state(WorkingStatus.CLEANING))
        coord.clean_settings.work_mode = WorkMode.MOP
        coord.active_clean_work_mode = WorkMode.VACUUM
        coord.client.set_mop_humidity = AsyncMock()
        sel = NarwalSelect(coord, _DESCS["water"])

        with pytest.raises(HomeAssistantError, match="changed right now|selected mode"):
            await sel.async_select_option("wet")

        coord.client.set_mop_humidity.assert_not_awaited()

    async def test_live_water_rejects_unknown_active_mode(self) -> None:
        """Unknown active mode must not expose a cross-mode water command."""
        coord = _coordinator(state=_state(WorkingStatus.CLEANING))
        coord.clean_settings.work_mode = WorkMode.MOP
        coord.active_clean_work_mode = None
        coord.client.set_mop_humidity = AsyncMock()
        sel = NarwalSelect(coord, _DESCS["water"])

        assert not sel.available
        with pytest.raises(HomeAssistantError, match="changed right now|selected mode"):
            await sel.async_select_option("wet")

        coord.client.set_mop_humidity.assert_not_awaited()

    async def test_water_applies_live_while_paused(self) -> None:
        state = _state(WorkingStatus.CLEANING)
        state.is_paused = True
        state.is_cleaning = False
        coord = _coordinator(state=state)
        coord.client.set_mop_humidity = AsyncMock(
            return_value=CommandResponse(result_code=CommandResult.SUCCESS)
        )

        sel = NarwalSelect(coord, _DESCS["water"])
        await sel.async_select_option("wet")

        assert coord.clean_settings.water == MopHumidity.WET
        coord.set_active_clean_setting.assert_called_once_with(
            "water", MopHumidity.WET
        )
        coord.client.set_mop_humidity.assert_awaited_once_with(MopHumidity.WET)

    async def test_rejected_live_water_change_does_not_update_settings(self) -> None:
        coord = _coordinator(state=_state(WorkingStatus.CLEANING))
        coord.clean_settings.water = MopHumidity.NORMAL
        coord.client.set_mop_humidity = AsyncMock(
            return_value=CommandResponse(result_code=CommandResult.NOT_APPLICABLE)
        )
        sel = NarwalSelect(coord, _DESCS["water"])

        try:
            await sel.async_select_option("wet")
        except HomeAssistantError:
            pass
        else:
            raise AssertionError("Rejected live water change should raise")

        assert coord.clean_settings.water == MopHumidity.NORMAL

    async def test_no_live_setter_when_not_cleaning(self) -> None:
        coord = _coordinator(state=_state())
        coord.client.set_mop_humidity = AsyncMock()
        sel = NarwalSelect(coord, _DESCS["water"])
        await sel.async_select_option("dry")
        coord.client.set_mop_humidity.assert_not_awaited()

    async def test_restore_from_last_state(self) -> None:
        coord = _coordinator()
        coord.async_update_listeners = MagicMock()
        sel = NarwalSelect(coord, _DESCS["work_mode"])
        with patch.object(
            sel, "async_get_last_state", AsyncMock(return_value=MagicMock(state="mop"))
        ):
            await sel.async_added_to_hass()
        assert coord.clean_settings.work_mode == WorkMode.MOP
        coord.async_update_listeners.assert_called_once()

    async def test_restore_ignores_unknown_option(self) -> None:
        coord = _coordinator(settings=CleanSettings(work_mode=WorkMode.VACUUM))
        sel = NarwalSelect(coord, _DESCS["work_mode"])
        with patch.object(
            sel, "async_get_last_state", AsyncMock(return_value=MagicMock(state="bogus"))
        ):
            await sel.async_added_to_hass()
        assert coord.clean_settings.work_mode == WorkMode.VACUUM

    def test_start_only_selects_unavailable_during_active_clean(self) -> None:
        coord = _coordinator(state=_state(WorkingStatus.CLEANING))
        coord.active_clean_work_mode = WorkMode.VACUUM_AND_MOP
        assert not NarwalSelect(coord, _DESCS["work_mode"]).available
        assert not NarwalSelect(coord, _DESCS["mop_strength"]).available
        assert NarwalSelect(coord, _DESCS["water"]).available

    def test_pending_settings_available_during_compatible_dock_bag_drying(self) -> None:
        state = NarwalState(working_status=WorkingStatus.DOCKED)
        state.dock_presence = 1
        state.dock_field11 = 2
        state.dock_field47 = 3
        state.set_dock_drying_task(
            DOCK_TASK_DRY_DOCK_BAG,
            elapsed=45,
            target=180,
            fields=("12", "13"),
        )
        coord = _coordinator(state=state)

        assert state.is_station_active
        assert can_edit_pending_clean_settings(state)
        assert NarwalSelect(coord, _DESCS["work_mode"]).available

    def test_primary_settings_unavailable_when_not_applicable_to_mode(self) -> None:
        coord = _coordinator(state=_state())
        coord.clean_settings.work_mode = WorkMode.VACUUM

        assert not NarwalSelect(coord, _DESCS["water"]).available
        assert not NarwalSelect(coord, _DESCS["mop_strength"]).available

    async def test_primary_setting_rejects_option_when_not_applicable_to_mode(self) -> None:
        coord = _coordinator(state=_state())
        coord.clean_settings.work_mode = WorkMode.VACUUM
        coord.client.set_mop_humidity = AsyncMock()
        sel = NarwalSelect(coord, _DESCS["water"])

        with pytest.raises(HomeAssistantError, match="selected mode"):
            await sel.async_select_option("wet")

        coord.client.set_mop_humidity.assert_not_awaited()


class TestLegacyNarwalSettingSelect:
    def test_start_only_settings_unavailable_during_active_clean(self) -> None:
        coord = _coordinator(state=_state(WorkingStatus.CLEANING))
        for key in ("mode", "passes", "scrub"):
            assert not LegacyNarwalSettingSelect(coord, _LEGACY_DESCS[key]).available

    def test_live_settings_remain_available_during_active_clean(self) -> None:
        coord = _coordinator(state=_state(WorkingStatus.CLEANING))
        coord.active_clean_work_mode = WorkMode.VACUUM_AND_MOP
        assert LegacyNarwalSettingSelect(coord, _LEGACY_DESCS["suction"]).available
        assert LegacyNarwalSettingSelect(coord, _LEGACY_DESCS["water"]).available

    def test_suction_options_stay_stable_while_cleaning(self) -> None:
        coord = _coordinator(state=_state(WorkingStatus.CLEANING))
        sel = LegacyNarwalSettingSelect(coord, _LEGACY_DESCS["suction"])
        assert "AI" in sel.options
        assert "Standard" in sel.options

    def test_ax26_legacy_suction_omits_ultra(self) -> None:
        coord = _coordinator(product_key="qV6BujoYLz")
        sel = LegacyNarwalSettingSelect(coord, _LEGACY_DESCS["suction"])

        assert "Super Powerful" in sel.options
        assert sel._normalise_option("Super") == "Super"
        assert "Ultra" not in sel.options
        assert sel._normalise_option("Ultra powerful") is None

    async def test_ai_suction_rejected_while_cleaning(self) -> None:
        coord = _coordinator(state=_state(WorkingStatus.CLEANING))
        coord.client.set_fan_speed = AsyncMock()
        sel = LegacyNarwalSettingSelect(coord, _LEGACY_DESCS["suction"])
        try:
            await sel.async_select_option("AI")
        except HomeAssistantError:
            pass
        else:
            raise AssertionError("AI suction should not be selectable mid-clean")
        coord.client.set_fan_speed.assert_not_awaited()

    def test_route_is_available_when_idle(self) -> None:
        coord = _coordinator(state=_state())
        assert LegacyNarwalSettingSelect(coord, _LEGACY_DESCS["route"]).available

    def test_route_is_unavailable_during_active_clean(self) -> None:
        coord = _coordinator(state=_state(WorkingStatus.CLEANING))
        assert not LegacyNarwalSettingSelect(coord, _LEGACY_DESCS["route"]).available

    def test_mode_specific_settings_unavailable_when_not_applicable(self) -> None:
        coord = _coordinator(state=_state())
        coord.clean_settings.work_mode = WorkMode.VACUUM
        assert not LegacyNarwalSettingSelect(coord, _LEGACY_DESCS["water"]).available
        assert not LegacyNarwalSettingSelect(coord, _LEGACY_DESCS["scrub"]).available

        coord.clean_settings.work_mode = WorkMode.MOP
        assert not LegacyNarwalSettingSelect(coord, _LEGACY_DESCS["suction"]).available

    async def test_legacy_live_water_uses_active_room_mode(self) -> None:
        coord = _coordinator(state=_state(WorkingStatus.CLEANING))
        coord.clean_settings.work_mode = WorkMode.VACUUM
        coord.active_clean_work_mode = WorkMode.MOP
        coord.client.set_mop_humidity = AsyncMock(
            return_value=CommandResponse(result_code=0)
        )
        sel = LegacyNarwalSettingSelect(coord, _LEGACY_DESCS["water"])

        await sel.async_select_option("Wet")

        assert coord.clean_settings.water == MopHumidity.WET
        coord.set_active_clean_setting.assert_called_once_with(
            "water", MopHumidity.WET
        )
        coord.client.set_mop_humidity.assert_awaited_once_with(MopHumidity.WET)

    async def test_legacy_live_suction_rejects_active_mop_mode(self) -> None:
        coord = _coordinator(state=_state(WorkingStatus.CLEANING))
        coord.clean_settings.work_mode = WorkMode.VACUUM
        coord.active_clean_work_mode = WorkMode.MOP
        coord.client.set_fan_speed = AsyncMock()
        sel = LegacyNarwalSettingSelect(coord, _LEGACY_DESCS["suction"])

        with pytest.raises(HomeAssistantError, match="changed right now|mop-only"):
            await sel.async_select_option("Strong")

        coord.client.set_fan_speed.assert_not_awaited()

    async def test_legacy_live_suction_rejects_unknown_active_mode(self) -> None:
        coord = _coordinator(state=_state(WorkingStatus.CLEANING))
        coord.clean_settings.work_mode = WorkMode.VACUUM
        coord.active_clean_work_mode = None
        coord.client.set_fan_speed = AsyncMock()
        sel = LegacyNarwalSettingSelect(coord, _LEGACY_DESCS["suction"])

        assert not sel.available
        with pytest.raises(HomeAssistantError, match="changed right now|mop-only"):
            await sel.async_select_option("Strong")

        coord.client.set_fan_speed.assert_not_awaited()

    async def test_legacy_live_highest_suction_clamps_and_stays_pending(self) -> None:
        coord = _coordinator(state=_state(WorkingStatus.CLEANING))
        coord.active_clean_work_mode = WorkMode.VACUUM
        coord.client.set_fan_speed = AsyncMock(
            return_value=CommandResponse(result_code=0)
        )
        sel = LegacyNarwalSettingSelect(coord, _LEGACY_DESCS["suction"])

        await sel.async_select_option("Ultra Powerful")

        coord.client.set_fan_speed.assert_awaited_once_with(FanLevel.DEEP)
        coord.set_active_clean_setting.assert_called_once_with("fan", FanLevel.DEEP)
        assert coord.clean_settings.fan == FanLevel.SUPER

    def test_legacy_settings_are_config_entities(self) -> None:
        coord = _coordinator(state=_state())
        select = LegacyNarwalSettingSelect(coord, _LEGACY_DESCS["route"])

        assert select._attr_entity_category == "config"

    async def test_restore_does_not_overwrite_primary_settings(self) -> None:
        coord = _coordinator(
            settings=CleanSettings(
                work_mode=WorkMode.MOP,
                fan=FanLevel.STRONG,
                water=MopHumidity.WET,
                mop_strength=MopStrengthLevel.HIGH,
                passes=3,
            )
        )
        sel = LegacyNarwalSettingSelect(coord, _LEGACY_DESCS["mode"])
        with patch.object(
            sel,
            "async_get_last_state",
            AsyncMock(return_value=MagicMock(state="Vacuum")),
        ):
            await sel.async_added_to_hass()

        assert coord.clean_settings.work_mode == WorkMode.MOP
        assert sel.current_option == "Mop"

    async def test_route_restore_stores_legacy_only_setting(self) -> None:
        coord = _coordinator(settings=CleanSettings(route=CleaningRoute.METICULOUS))
        sel = LegacyNarwalSettingSelect(coord, _LEGACY_DESCS["route"])
        with patch.object(
            sel,
            "async_get_last_state",
            AsyncMock(return_value=MagicMock(state="Standard")),
        ):
            await sel.async_added_to_hass()

        assert coord.clean_settings.route == CleaningRoute.STANDARD
        assert sel.current_option == "Standard"

    async def test_ai_suction_restore_stores_legacy_only_setting(self) -> None:
        coord = _coordinator(settings=CleanSettings(fan=FanLevel.NORMAL))
        sel = LegacyNarwalSettingSelect(coord, _LEGACY_DESCS["suction"])
        with patch.object(
            sel,
            "async_get_last_state",
            AsyncMock(return_value=MagicMock(state="AI")),
        ):
            await sel.async_added_to_hass()

        assert coord.clean_settings.fan == FanLevel.UNSPECIFIED
        assert sel.current_option == "AI"

    async def test_suction_extra_restore_uses_stable_robot_value(self) -> None:
        coord = _coordinator(settings=CleanSettings(fan=FanLevel.NORMAL))
        sel = LegacyNarwalSettingSelect(coord, _LEGACY_DESCS["suction"])
        extra = SettingRestoreData(value=int(FanLevel.SUPER))

        with (
            patch.object(
                sel, "async_get_last_extra_data", AsyncMock(return_value=extra)
            ),
            patch.object(
                sel,
                "async_get_last_state",
                AsyncMock(return_value=MagicMock(state="unavailable")),
            ) as get_last_state,
        ):
            await sel.async_added_to_hass()

        get_last_state.assert_not_awaited()
        assert coord.clean_settings.fan == FanLevel.SUPER

    async def test_four_tier_suction_extra_restore_clamps_level_five(self) -> None:
        coord = _coordinator(
            settings=CleanSettings(fan=FanLevel.NORMAL),
            product_key="qV6BujoYLz",
        )
        sel = LegacyNarwalSettingSelect(coord, _LEGACY_DESCS["suction"])
        extra = SettingRestoreData(value=int(FanLevel.SUPER))

        with patch.object(
            sel, "async_get_last_extra_data", AsyncMock(return_value=extra)
        ):
            await sel.async_added_to_hass()

        assert coord.clean_settings.fan == FanLevel.DEEP
        assert sel.current_option == "Super Powerful"

    async def test_unversioned_suction_restore_keeps_v105_meaning(self) -> None:
        coord = _coordinator(settings=CleanSettings(fan=FanLevel.NORMAL))
        sel = LegacyNarwalSettingSelect(coord, _LEGACY_DESCS["suction"])

        with patch.object(
            sel,
            "async_get_last_state",
            AsyncMock(return_value=MagicMock(state="Ultra")),
        ):
            await sel.async_added_to_hass()

        assert coord.clean_settings.fan == FanLevel.SUPER

    async def test_global_suction_restore_clamps_for_four_tier_model(self) -> None:
        coord = _coordinator(
            product_key="qV6BujoYLz",
            settings=CleanSettings(fan=FanLevel.NORMAL),
        )
        sel = LegacyNarwalSettingSelect(coord, _LEGACY_DESCS["suction"])
        extra = SettingRestoreData(value=int(FanLevel.SUPER))

        with patch.object(
            sel, "async_get_last_extra_data", AsyncMock(return_value=extra)
        ):
            await sel.async_added_to_hass()

        assert coord.clean_settings.fan == FanLevel.DEEP

    async def test_four_tier_unversioned_ultra_clamps_level_five(self) -> None:
        coord = _coordinator(
            settings=CleanSettings(fan=FanLevel.NORMAL),
            product_key="qV6BujoYLz",
        )
        sel = LegacyNarwalSettingSelect(coord, _LEGACY_DESCS["suction"])

        with patch.object(
            sel,
            "async_get_last_state",
            AsyncMock(return_value=MagicMock(state="Ultra")),
        ):
            await sel.async_added_to_hass()

        assert coord.clean_settings.fan == FanLevel.DEEP
        assert sel.current_option == "Super Powerful"

    async def test_select_option_refreshes_related_setting_entities(self) -> None:
        coord = _coordinator(state=_state())
        coord.async_update_listeners = MagicMock()
        sel = LegacyNarwalSettingSelect(coord, _LEGACY_DESCS["mode"])
        await sel.async_select_option("Vacuum")
        coord.async_update_listeners.assert_called_once()

    async def test_route_select_stores_clean_setting(self) -> None:
        coord = _coordinator(state=_state())
        sel = LegacyNarwalSettingSelect(coord, _LEGACY_DESCS["route"])
        await sel.async_select_option("Standard")
        assert coord.clean_settings.route == CleaningRoute.STANDARD

    async def test_select_option_stores_pending_value_when_entity_unavailable(self) -> None:
        coord = _coordinator(state=_state())
        coord.last_update_success = False
        sel = LegacyNarwalSettingSelect(coord, _LEGACY_DESCS["route"])

        await sel.async_select_option("Standard")

        assert coord.clean_settings.route == CleaningRoute.STANDARD


class TestRoomNarwalSettingSelect:
    def test_disabled_by_default(self) -> None:
        """High-cardinality room profile entities are opt-in."""
        assert RoomNarwalSettingSelect._attr_entity_registry_enabled_default is False

    def test_current_option_reflects_room_settings(self) -> None:
        coord = _coordinator()
        coord.room_clean_settings[(None, 4)] = RoomCleanSettings(
            work_mode=WorkMode.MOP,
            route=CleaningRoute.METICULOUS,
        )
        coord.room_clean_settings_customized[(None, 4)] = {"work_mode"}
        assert (
            RoomNarwalSettingSelect(coord, 4, "Kitchen", _ROOM_DESCS["mode"]).current_option
            == "Mop"
        )

    async def test_select_option_stores_room_setting(self) -> None:
        coord = _coordinator(state=_state())
        sel = RoomNarwalSettingSelect(coord, 4, "Kitchen", _ROOM_DESCS["route"])
        await sel.async_select_option("Standard")
        assert coord.room_clean_settings[(None, 4)].route == CleaningRoute.STANDARD

    async def test_failed_store_restore_blocks_room_profile_edits(self) -> None:
        """An unread durable profile cannot be replaced by an entity edit."""
        coord = _coordinator(state=_state())
        coord._room_profile_store_loaded = False
        sel = RoomNarwalSettingSelect(coord, 4, "Kitchen", _ROOM_DESCS["route"])

        assert not sel.available
        with pytest.raises(HomeAssistantError, match="not restored"):
            await sel.async_select_option("Standard")

        assert coord.room_clean_settings == {}

    async def test_select_option_stores_pending_value_when_entity_unavailable(self) -> None:
        coord = _coordinator(state=_state())
        coord.last_update_success = False
        sel = RoomNarwalSettingSelect(coord, 4, "Kitchen", _ROOM_DESCS["route"])

        await sel.async_select_option("Standard")

        assert coord.room_clean_settings[(None, 4)].route == CleaningRoute.STANDARD

    async def test_select_passes_stores_room_setting(self) -> None:
        coord = _coordinator(state=_state())
        sel = RoomNarwalSettingSelect(coord, 4, "Kitchen", _ROOM_DESCS["passes"])
        await sel.async_select_option("3")
        assert coord.room_clean_settings[(None, 4)].passes == 3

    async def test_select_suction_stores_room_setting(self) -> None:
        coord = _coordinator(state=_state())
        sel = RoomNarwalSettingSelect(coord, 4, "Kitchen", _ROOM_DESCS["suction"])
        await sel.async_select_option("Strong")
        assert coord.room_clean_settings[(None, 4)].fan == FanLevel.STRONG

    async def test_added_without_restore_does_not_customize_room_defaults(self) -> None:
        coord = _coordinator(state=_state())
        sel = RoomNarwalSettingSelect(coord, 4, "Kitchen", _ROOM_DESCS["route"])

        with patch.object(sel, "async_get_last_state", AsyncMock(return_value=None)):
            await sel.async_added_to_hass()

        assert coord.room_clean_settings_customized == {}
        coord.clean_settings.route = CleaningRoute.STANDARD
        assert sel.current_option == "Standard"

    async def test_restored_inherited_room_state_does_not_customize_room_defaults(
        self,
    ) -> None:
        coord = _coordinator(state=_state())
        sel = RoomNarwalSettingSelect(coord, 4, "Kitchen", _ROOM_DESCS["route"])
        restored = MagicMock(state="Meticulous", attributes={"customized": False})

        with patch.object(sel, "async_get_last_state", AsyncMock(return_value=restored)):
            await sel.async_added_to_hass()

        assert coord.room_clean_settings_customized == {}
        coord.clean_settings.route = CleaningRoute.STANDARD
        assert sel.current_option == "Standard"

    def test_inherited_room_state_follows_global_defaults(self) -> None:
        coord = _coordinator(state=_state())
        sel = RoomNarwalSettingSelect(coord, 4, "Kitchen", _ROOM_DESCS["route"])

        assert sel.current_option == "Meticulous"
        coord.clean_settings.route = CleaningRoute.STANDARD

        assert sel.current_option == "Standard"
        assert coord.room_clean_settings_customized == {}

    def test_customized_room_state_overrides_global_defaults(self) -> None:
        coord = _coordinator(state=_state())
        sel = RoomNarwalSettingSelect(coord, 4, "Kitchen", _ROOM_DESCS["route"])

        coord.set_room_clean_setting(4, "route", CleaningRoute.STANDARD)
        coord.clean_settings.route = CleaningRoute.METICULOUS

        assert sel.current_option == "Standard"
        assert sel.extra_state_attributes["customized"] is True

    async def test_restored_customized_room_state_is_applied(self) -> None:
        coord = _coordinator(state=_state())
        sel = RoomNarwalSettingSelect(coord, 4, "Kitchen", _ROOM_DESCS["route"])
        restored = MagicMock(state="Standard", attributes={"customized": True})

        with patch.object(sel, "async_get_last_state", AsyncMock(return_value=restored)):
            await sel.async_added_to_hass()

        assert coord.room_clean_settings_customized == {(None, 4): {"route"}}
        assert sel.current_option == "Standard"
        assert sel.extra_state_attributes["customized"] is True
        coord.async_update_listeners.assert_called_once_with()

    async def test_coordinator_restore_wins_over_entity_restore_data(self) -> None:
        """A disabled entity's old HA state cannot replace the durable profile."""
        coord = _coordinator(state=_state())
        coord.set_room_clean_setting(4, "route", CleaningRoute.STANDARD)
        sel = RoomNarwalSettingSelect(coord, 4, "Kitchen", _ROOM_DESCS["route"])
        extra = RoomSettingRestoreData(
            value=int(CleaningRoute.METICULOUS), customized=True
        )

        with patch.object(
            sel, "async_get_last_extra_data", AsyncMock(return_value=extra)
        ) as get_extra:
            await sel.async_added_to_hass()

        get_extra.assert_not_awaited()
        assert sel.current_option == "Standard"

    async def test_extra_restore_data_survives_unavailable_entity_state(self) -> None:
        coord = _coordinator(state=_state())
        sel = RoomNarwalSettingSelect(coord, 4, "Kitchen", _ROOM_DESCS["route"])
        extra = RoomSettingRestoreData(
            value=int(CleaningRoute.STANDARD), customized=True
        )

        with (
            patch.object(
                sel, "async_get_last_extra_data", AsyncMock(return_value=extra)
            ),
            patch.object(
                sel,
                "async_get_last_state",
                AsyncMock(return_value=MagicMock(state="unavailable")),
            ) as get_last_state,
        ):
            await sel.async_added_to_hass()

        get_last_state.assert_not_awaited()
        assert coord.room_clean_settings_customized == {(None, 4): {"route"}}
        assert sel.current_option == "Standard"
        coord.async_update_listeners.assert_called_once_with()

    def test_extra_restore_data_records_customized_room_setting(self) -> None:
        coord = _coordinator(state=_state())
        sel = RoomNarwalSettingSelect(coord, 4, "Kitchen", _ROOM_DESCS["route"])
        coord.set_room_clean_setting(4, "route", CleaningRoute.STANDARD)

        assert sel.extra_restore_state_data.as_dict() == {
            "version": 1,
            "value": int(CleaningRoute.STANDARD),
            "customized": True,
        }

    async def test_unversioned_room_suction_keeps_v105_meaning(self) -> None:
        coord = _coordinator(state=_state())
        sel = RoomNarwalSettingSelect(coord, 4, "Kitchen", _ROOM_DESCS["suction"])
        extra = MagicMock()
        extra.as_dict.return_value = {"option": "Ultra", "customized": True}

        with patch.object(
            sel, "async_get_last_extra_data", AsyncMock(return_value=extra)
        ):
            await sel.async_added_to_hass()

        assert coord.room_clean_settings[(None, 4)].fan == FanLevel.SUPER

    async def test_unversioned_room_suction_clamps_for_four_tier_model(self) -> None:
        coord = _coordinator(product_key="qV6BujoYLz", state=_state())
        sel = RoomNarwalSettingSelect(coord, 4, "Kitchen", _ROOM_DESCS["suction"])
        extra = MagicMock()
        extra.as_dict.return_value = {"option": "Ultra", "customized": True}

        with patch.object(
            sel, "async_get_last_extra_data", AsyncMock(return_value=extra)
        ):
            await sel.async_added_to_hass()

        assert coord.room_clean_settings[(None, 4)].fan == FanLevel.DEEP

    def test_mode_specific_settings_unavailable_when_not_applicable(self) -> None:
        coord = _coordinator(state=_state())
        coord.room_clean_settings[(None, 4)] = RoomCleanSettings(work_mode=WorkMode.VACUUM)
        coord.room_clean_settings_customized[(None, 4)] = {"work_mode", "water"}
        assert not RoomNarwalSettingSelect(coord, 4, "Kitchen", _ROOM_DESCS["water"]).available
        assert not RoomNarwalSettingSelect(coord, 4, "Kitchen", _ROOM_DESCS["scrub"]).available

        coord.room_clean_settings[(None, 4)].work_mode = WorkMode.MOP
        coord.room_clean_settings_customized[(None, 4)].add("fan")
        assert not RoomNarwalSettingSelect(coord, 4, "Kitchen", _ROOM_DESCS["suction"]).available

    def test_room_profile_entities_are_config_entities(self) -> None:
        coord = _coordinator(state=_state())
        select = RoomNarwalSettingSelect(coord, 4, "Kitchen", _ROOM_DESCS["route"])

        assert select._attr_entity_category == "config"

    async def test_room_profiles_are_scoped_by_map_id(self) -> None:
        state = _state()
        state.map_data = MapData(map_id=100, rooms=[RoomInfo(room_id=4, name="Kitchen")])
        coord = _coordinator(state=state)
        kitchen = RoomNarwalSettingSelect(
            coord,
            4,
            "Kitchen",
            _ROOM_DESCS["route"],
            map_id="100",
        )

        await kitchen.async_select_option("Standard")

        state.map_data = MapData(map_id=200, rooms=[RoomInfo(room_id=4, name="Bedroom")])
        bedroom = RoomNarwalSettingSelect(
            coord,
            4,
            "Bedroom",
            _ROOM_DESCS["route"],
            map_id="200",
        )

        assert bedroom.current_option == "Meticulous"
        assert coord.room_clean_settings[("100", 4)].route == CleaningRoute.STANDARD
        assert ("200", 4) not in coord.room_clean_settings
        assert not kitchen.available
        with pytest.raises(HomeAssistantError, match="room is not available"):
            await kitchen.async_select_option("Meticulous")
        assert coord.room_clean_settings[("100", 4)].route == CleaningRoute.STANDARD

    def test_room_profiles_unavailable_during_active_clean(self) -> None:
        coord = _coordinator(state=_state(WorkingStatus.CLEANING))
        assert not RoomNarwalSettingSelect(coord, 4, "Kitchen", _ROOM_DESCS["mode"]).available

    async def test_room_setting_entities_update_name_after_map_rename(self) -> None:
        coord = _coordinator()
        coord.client.state.map_data = MapData(
            rooms=[RoomInfo(room_id=4, name="Kitchen")]
        )
        entry = MagicMock()
        entry.runtime_data = coord
        added_entities = []
        listeners = []

        def add_entities(entities) -> None:
            added_entities.extend(entities)

        coord.async_add_listener.side_effect = lambda listener: listeners.append(listener)

        await async_setup_entry(MagicMock(), entry, add_entities)

        room_mode = next(
            entity
            for entity in added_entities
            if isinstance(entity, RoomNarwalSettingSelect)
            and entity.entity_description.setting_key == "mode"
        )
        assert room_mode._attr_name == "Kitchen mode"
        assert room_mode.extra_state_attributes["room_name"] == "Kitchen"

        coord.client.state.map_data = MapData(
            rooms=[RoomInfo(room_id=4, name="Pantry")]
        )
        listeners[0]()

        assert len(added_entities) == len(SELECT_DESCRIPTIONS) + len(
            LEGACY_SELECT_DESCRIPTIONS
        ) + len(ROOM_SELECT_DESCRIPTIONS)
        assert room_mode._attr_name == "Pantry mode"
        assert room_mode.extra_state_attributes["room_name"] == "Pantry"


class TestNarwalPassesNumber:
    def test_native_value_reflects_settings(self) -> None:
        coord = _coordinator(settings=CleanSettings(passes=2))
        assert NarwalPassesNumber(coord).native_value == 2

    def test_unavailable_during_active_clean(self) -> None:
        coord = _coordinator(state=_state(WorkingStatus.CLEANING))
        assert not NarwalPassesNumber(coord).available

    async def test_set_native_value_stores_int(self) -> None:
        coord = _coordinator()
        num = NarwalPassesNumber(coord)
        await num.async_set_native_value(3.0)
        assert coord.clean_settings.passes == 3

    async def test_set_native_value_rejects_fractional_values(self) -> None:
        coord = _coordinator(settings=CleanSettings(passes=2))
        num = NarwalPassesNumber(coord)

        with pytest.raises(HomeAssistantError, match="integer"):
            await num.async_set_native_value(2.5)

        assert coord.clean_settings.passes == 2

    async def test_set_native_value_stores_pending_value_when_entity_unavailable(self) -> None:
        coord = _coordinator(state=_state())
        coord.last_update_success = False
        num = NarwalPassesNumber(coord)

        await num.async_set_native_value(3.0)

        assert coord.clean_settings.passes == 3

    async def test_set_native_value_refreshes_related_setting_entities(self) -> None:
        coord = _coordinator(state=_state())
        coord.async_update_listeners = MagicMock()
        number = NarwalPassesNumber(coord)

        await number.async_set_native_value(3)

        assert coord.clean_settings.passes == 3
        coord.async_update_listeners.assert_called_once()

    async def test_restore_from_last_number_data(self) -> None:
        coord = _coordinator()
        num = NarwalPassesNumber(coord)
        data = MagicMock(native_value=2)
        with patch.object(num, "async_get_last_number_data", AsyncMock(return_value=data)):
            await num.async_added_to_hass()
        assert coord.clean_settings.passes == 2
