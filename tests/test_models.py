"""Tests for narwal_client.models — state data models."""

from __future__ import annotations

import struct

from narwal_client.const import WorkingStatus
from narwal_client.models import MapData, NarwalState


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
        """working_status topic sets cleaning metrics, not robot state."""
        state = NarwalState()
        state.update_from_working_status({"3": 120, "13": 18000, "15": 600})
        assert state.cleaning_time == 120
        assert state.cleaning_area == 18000
        # working_status is NOT set by this method (comes from base_status)
        assert state.working_status == WorkingStatus.UNKNOWN

    def test_update_from_base_status_cleaning(self) -> None:
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 4}, "38": 85})
        assert state.working_status == WorkingStatus.CLEANING
        assert state.is_cleaning
        assert state.battery_level == 85

    def test_update_from_base_status_docked(self) -> None:
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 10, "10": 1}})
        assert state.working_status == WorkingStatus.DOCKED
        assert state.is_docked

    def test_update_from_base_status_charged(self) -> None:
        """Status 14 = fully charged on dock."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 14, "10": 1}, "38": 100})
        assert state.working_status == WorkingStatus.CHARGED
        assert state.is_docked
        assert state.battery_level == 100

    def test_update_from_base_status_standby_on_dock(self) -> None:
        """STANDBY(1) with dock sub-state=1 means docked."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 1, "10": 1}})
        assert state.working_status == WorkingStatus.STANDBY
        assert state.is_docked

    def test_update_from_base_status_standby_off_dock(self) -> None:
        """STANDBY(1) without dock sub-state=1 means idle, not docked."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 1}})
        assert state.working_status == WorkingStatus.STANDBY
        assert not state.is_docked

    def test_update_from_base_status_paused(self) -> None:
        """Paused overlay: field 3 sub-field 2 = 1."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 4, "2": 1}})
        assert state.working_status == WorkingStatus.CLEANING
        assert state.is_paused
        assert not state.is_cleaning  # is_cleaning is False when paused

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
        state.update_from_base_status({"3": {"1": 4}, "38": 95})
        state.update_from_working_status({"3": 120, "13": 18000})
        state.update_from_upgrade_status({"7": "v01.02.19.02"})

        assert state.battery_level == 95
        assert state.is_cleaning
        assert state.cleaning_time == 120
        assert state.cleaning_area == 18000
        assert state.firmware_version == "v01.02.19.02"

    def test_raw_data_preserved(self) -> None:
        state = NarwalState()
        raw = {"38": 100, "47": 2, "unknown_field": "value"}
        state.update_from_base_status(raw)
        assert state.raw_base_status == raw

    def test_unknown_working_status_value(self) -> None:
        """Unknown status values should fall back to UNKNOWN."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 99}})
        assert state.working_status == WorkingStatus.UNKNOWN


def _float_to_uint32(f: float) -> int:
    """Encode a float as the uint32 bit pattern (for protobuf simulation)."""
    return struct.unpack("I", struct.pack("f", f))[0]


class TestMapData:
    """Tests for MapData.from_response()."""

    def test_basic_map_parsing(self) -> None:
        decoded = {"2": {
            "3": 60,
            "4": 341,
            "5": 494,
            "12": [{"1": 3, "2": 0, "3": b"Kitchen"}],
            "17": b"\x78\x01" + b"\x00" * 20,
            "33": 944,
            "34": 1740000000,
        }}
        m = MapData.from_response(decoded)
        assert m.width == 341
        assert m.height == 494
        assert m.resolution == 60
        assert len(m.rooms) == 1
        assert m.rooms[0].name == "Kitchen"
        assert m.area == 944

    def test_dock_position_from_field48_uint32(self) -> None:
        """Dock parsed from field 48 (latest timestamp, uint32 grid coords)."""
        decoded = {"2": {
            "3": 60,
            "4": 341,
            "5": 494,
            "6": {"1": -341, "2": 152, "3": -280, "4": 60},
            "48": [
                {"1": 123, "2": {"1": _float_to_uint32(-49.04), "2": _float_to_uint32(35.74)}, "3": 1000},
                {"1": 123, "2": {"1": _float_to_uint32(19.88), "2": _float_to_uint32(36.07)}, "3": 2000},
            ],
            "17": b"",
        }}
        m = MapData.from_response(decoded)
        # Latest entry (ts=2000): grid (19.88, 36.07) → px = 19.88+280, py = 36.07+341
        assert m.dock_x is not None
        assert m.dock_y is not None
        assert abs(m.dock_x - 299.88) < 1.0
        assert abs(m.dock_y - 377.07) < 1.0

    def test_dock_position_from_field48_float(self) -> None:
        """bbp may return fixed32 fields as Python floats directly."""
        decoded = {"2": {
            "3": 60,
            "4": 341,
            "5": 494,
            "6": {"1": -341, "3": -280},
            "48": [
                {"1": 1, "2": {"1": 19.88, "2": 36.07}, "3": 100},
            ],
            "17": b"",
        }}
        m = MapData.from_response(decoded)
        assert m.dock_x is not None
        assert m.dock_y is not None
        assert abs(m.dock_x - 299.88) < 1.0
        assert abs(m.dock_y - 377.07) < 1.0

    def test_dock_position_missing_field48(self) -> None:
        """No dock position when field 48 is missing."""
        decoded = {"2": {
            "3": 60,
            "4": 341,
            "5": 494,
            "6": {"1": -341, "3": -280},
            "17": b"",
        }}
        m = MapData.from_response(decoded)
        assert m.dock_x is None
        assert m.dock_y is None

    def test_dock_position_missing_field6(self) -> None:
        """No dock position when field 6 (transform) is missing."""
        decoded = {"2": {
            "3": 60,
            "4": 341,
            "5": 494,
            "48": [{"1": 1, "2": {"1": 10.0, "2": 20.0}, "3": 100}],
            "17": b"",
        }}
        m = MapData.from_response(decoded)
        assert m.dock_x is None
        assert m.dock_y is None

    def test_empty_response(self) -> None:
        m = MapData.from_response({})
        assert m.width == 0
        assert m.dock_x is None
