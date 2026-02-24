"""Tests for narwal_client.models — state data models."""

from __future__ import annotations

from narwal_client.const import WorkingStatus
from narwal_client.models import NarwalState


class TestNarwalState:
    """Tests for NarwalState data model."""

    def test_default_state(self) -> None:
        state = NarwalState()
        assert state.working_status == WorkingStatus.UNKNOWN
        assert state.battery_level == 0
        assert state.firmware_version == ""
        assert not state.is_cleaning
        assert not state.is_docked
        assert not state.is_returning

    def test_update_from_working_status(self) -> None:
        state = NarwalState()
        state.update_from_working_status({"3": 3, "13": 18000, "15": 600})
        assert state.working_status == WorkingStatus.CLEANING
        assert state.is_cleaning
        assert state.cleaning_area == 18000
        assert state.cleaning_time == 600

    def test_update_from_working_status_docked(self) -> None:
        state = NarwalState()
        state.update_from_working_status({"3": 1})
        assert state.working_status == WorkingStatus.IDLE_DOCKED
        assert state.is_docked

    def test_update_from_working_status_returning(self) -> None:
        state = NarwalState()
        state.update_from_working_status({"3": 5})
        assert state.working_status == WorkingStatus.RETURNING
        assert state.is_returning

    def test_update_from_working_status_unknown_value(self) -> None:
        state = NarwalState()
        state.update_from_working_status({"3": 99})
        assert state.working_status == WorkingStatus.UNKNOWN

    def test_update_from_base_status(self) -> None:
        state = NarwalState()
        state.update_from_base_status({
            "38": 85,
            "36": 1757252225,
            "13": "d4bec8c82c484a3ba0428bb0dd4359e2",
        })
        assert state.battery_level == 85
        assert state.timestamp == 1757252225
        assert state.session_id == "d4bec8c82c484a3ba0428bb0dd4359e2"

    def test_update_from_upgrade_status(self) -> None:
        state = NarwalState()
        state.update_from_upgrade_status({
            "7": "v01.02.19.02",
            "8": "v01.02.19.02",
            "4": 10,
        })
        assert state.firmware_version == "v01.02.19.02"
        assert state.firmware_target == "v01.02.19.02"
        assert state.upgrade_status_code == 10

    def test_update_from_download_status(self) -> None:
        state = NarwalState()
        state.update_from_download_status({"1": 2})
        assert state.download_status == 2

    def test_incremental_updates(self) -> None:
        """State should accumulate across multiple topic updates."""
        state = NarwalState()
        state.update_from_base_status({"38": 100})
        state.update_from_working_status({"3": 3})
        state.update_from_upgrade_status({"7": "v01.02.19.02"})

        assert state.battery_level == 100
        assert state.is_cleaning
        assert state.firmware_version == "v01.02.19.02"

    def test_raw_data_preserved(self) -> None:
        state = NarwalState()
        raw = {"38": 100, "47": 2, "unknown_field": "value"}
        state.update_from_base_status(raw)
        assert state.raw_base_status == raw
