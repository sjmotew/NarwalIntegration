"""Tests for the station fault/consumable binary sensors (value_fn logic)."""

from __future__ import annotations

import tests.ha_stubs

tests.ha_stubs.install()

from narwal_client.models import NarwalState  # noqa: E402
from custom_components.narwal.binary_sensor import (  # noqa: E402
    BINARY_SENSOR_DESCRIPTIONS,
)

_DESCS = {d.key: d for d in BINARY_SENSOR_DESCRIPTIONS}


def test_error_gated_on_base_status_seen() -> None:
    """Error is unavailable until a base_status arrives, then reflects has_error."""
    fn = _DESCS["error"].value_fn
    assert fn(NarwalState()) is None  # no base_status yet
    s = NarwalState()
    s.update_from_base_status({"1": {}})  # healthy
    assert fn(s) is False
    s.update_from_base_status({"1": {"1": 7}})  # fault
    assert fn(s) is True


def test_error_clears_when_field_absent() -> None:
    """A recovered robot omits field 1 entirely (empty repeated) — the fault must clear, not stick."""
    fn = _DESCS["error"].value_fn
    s = NarwalState()
    s.update_from_base_status({"1": {"1": 7}})  # fault
    assert fn(s) is True
    s.update_from_base_status({"2": 0})  # next status drops field 1
    assert fn(s) is False
    assert s.error_codes == []


def test_error_attributes_expose_code_detail() -> None:
    """The error sensor surfaces code/level/detail + a help link when faulted."""
    desc = _DESCS["error"]
    s = NarwalState()
    s.update_from_base_status({"1": {"1": 2105, "2": 3, "3": b"wheel stuck"}})
    attrs = desc.attrs_fn(s)
    assert attrs["codes"] == [2105]
    assert attrs["level"] == 3
    assert attrs["detail"] == "wheel stuck"
    assert "code=2105" in attrs["help_url"]


def test_no_help_url_when_healthy() -> None:
    """No help_url attribute when there's no active error code."""
    desc = _DESCS["error"]
    s = NarwalState()
    s.update_from_base_status({"1": {}})
    assert "help_url" not in desc.attrs_fn(s)


def test_tank_problem_states() -> None:
    """Tank/bag sensors: None until reported, ok at value 1, problem at ≥2."""
    cw = _DESCS["clean_water_tank"].value_fn
    sb = _DESCS["station_bag"].value_fn
    assert cw(NarwalState()) is None  # not reported -> unavailable
    s = NarwalState()
    s.update_from_base_status({"23": 1, "39": 1})  # both ok
    assert cw(s) is False
    assert sb(s) is False
    s.update_from_base_status({"23": 2, "39": 3})  # EMPTY / SUGGEST_REPLACE
    assert cw(s) is True
    assert sb(s) is True


def test_unspecified_is_not_a_problem() -> None:
    """Value 0 (UNSPECIFIED) must not read as a problem."""
    fn = _DESCS["sewage_tank"].value_fn
    s = NarwalState()
    s.update_from_base_status({"24": 0})
    assert fn(s) is False
