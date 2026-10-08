"""Parsing of developer/get_robot_info into RobotDiagnostics.

The robot answers with labelled sections rather than a typed message, and the
labels are Chinese whatever the configured voice language is:

    Section{1: title, 2: repeated Item{1: label, 2: value}}

Captured on a Freo Z Ultra (CX7, fw v01.13.11.02). None of this data appears in
any broadcast, and it settles an open question in docs/PROTOCOL.md: base_status
field 38 reads a constant 100 while true battery health reads 89%.

SECURITY: the same response carries the Wi-Fi SSID and pre-shared key in clear
over an unauthenticated socket, and this integration offers a diagnostics
download that users attach to public issues. The PSK must never survive
parsing -- test_wifi_psk_is_never_retained is the regression guard.
"""

from __future__ import annotations

import tests.ha_stubs

tests.ha_stubs.install()

from narwal_client.client import _parse_robot_info  # noqa: E402


def _varint(value: int) -> bytes:
    out = bytearray()
    while value > 0x7F:
        out.append(value & 0x7F | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _ld(field: int, payload: bytes) -> bytes:
    """Encode one length-delimited protobuf field."""
    return bytes([(field << 3) | 2]) + _varint(len(payload)) + payload


def _item(label: str, value: str) -> bytes:
    return _ld(2, _ld(1, label.encode()) + _ld(2, value.encode()))


def _section(title: str, items: bytes) -> bytes:
    return _ld(2, _ld(1, title.encode()) + items)


def _battery_response() -> bytes:
    """The battery section exactly as the reference CX7 returns it."""
    items = (
        _item("电量", "98%")
        + _item("真实电量", "94%")
        + _item("虚拟电量", "98%")
        + _item("虚拟服务开关", "关")
        + _item("健康值", "89%")
        + _item("使用次数", "519")
        + _item("充电剩余时间", "0")
        + _item("电流", "-0.714000")
        + _item("电压", "15.900000")
        + _item("温度", "35.900002")
    )
    return b"\x08\x01" + _section("电池信息", items)


class TestBatteryParsing:
    """Numbers are lifted out of the labelled strings."""

    def test_all_battery_fields(self) -> None:
        diagnostics = _parse_robot_info(_battery_response())
        assert diagnostics is not None
        assert diagnostics.battery_level == 98
        assert diagnostics.battery_real_level == 94
        assert diagnostics.battery_health == 89
        assert diagnostics.battery_cycles == 519
        assert diagnostics.charge_remaining_minutes == 0
        assert diagnostics.battery_voltage == 15.9
        assert diagnostics.battery_temperature == 35.900002

    def test_negative_current_survives(self) -> None:
        """Current is negative while charging; the sign must not be dropped."""
        diagnostics = _parse_robot_info(_battery_response())
        assert diagnostics.battery_current == -0.714

    def test_percent_suffix_is_stripped(self) -> None:
        """Values arrive as '98%', not 98."""
        assert _parse_robot_info(_battery_response()).battery_level == 98

    def test_unrecognised_labels_are_still_kept(self) -> None:
        """A label with no mapping stays available in `sections`."""
        diagnostics = _parse_robot_info(_battery_response())
        assert diagnostics.sections["电池信息"]["虚拟服务开关"] == "关"

    def test_empty_response_returns_none(self) -> None:
        """Models without the topic must not produce an empty diagnostics object."""
        assert _parse_robot_info(b"\x08\x01") is None

    def test_garbage_does_not_raise(self) -> None:
        """A truncated or foreign payload degrades to None rather than crashing."""
        assert _parse_robot_info(b"\xff\xff\xff\xff") is None


class TestSecretRedaction:
    """The Wi-Fi PSK must not survive parsing."""

    @staticmethod
    def _with_wifi() -> bytes:
        network = _item("Wifi", "ssid:HomeNet\npsk:hunter2-secret")
        return b"\x08\x01" + _section("网络信息", network) + _battery_response()[2:]

    def test_wifi_psk_is_never_retained(self) -> None:
        """Regression guard for the diagnostics download."""
        diagnostics = _parse_robot_info(self._with_wifi())
        blob = repr(diagnostics)
        assert "hunter2-secret" not in blob
        assert "psk" not in blob.lower()
        # `sections` is what the HA diagnostics download serialises, so check
        # it explicitly rather than only through the repr.
        for items in diagnostics.sections.values():
            for label, value in items.items():
                assert "psk" not in label.lower()
                assert "psk" not in value.lower()
                assert "hunter2-secret" not in value

    def test_battery_data_still_parses_alongside_wifi(self) -> None:
        """Redaction must not discard the rest of the response."""
        diagnostics = _parse_robot_info(self._with_wifi())
        assert diagnostics.battery_health == 89

    def test_a_label_containing_password_is_dropped(self) -> None:
        """Redaction keys on the label as well as the value."""
        payload = b"\x08\x01" + _section(
            "x", _item("password", "letmein") + _item("健康值", "89%")
        )
        diagnostics = _parse_robot_info(payload)
        assert "letmein" not in repr(diagnostics)
        assert diagnostics.battery_health == 89
