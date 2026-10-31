"""Tests for fan_speed labels, back-compat aliases, and the per-model tier list."""

from __future__ import annotations

import tests.ha_stubs

tests.ha_stubs.install()

from custom_components.narwal.const import (  # noqa: E402
    CONF_PRODUCT_KEY,
    FAN_SPEED_LIST,
    FAN_SPEED_MAP,
    fan_speed_list_for,
)
from custom_components.narwal.narwal_client.const import FanLevel  # noqa: E402

AX26 = "qV6BujoYLz"  # Freo Z10 Pro / Turbo — app tops out at DEEP (#70)
FLOW_2 = "QxMSPG6VSO"


def test_canonical_labels_are_the_offered_list() -> None:
    """The offered labels match the app, in ascending suction order."""
    assert FAN_SPEED_LIST == [
        "Quiet",
        "Standard",
        "Strong",
        "Super Powerful",
        "Ultra Powerful",
    ]


def test_canonical_labels_map_to_the_proto_enum() -> None:
    """Each label carries the CleanParam tag 2 value the app sends for it."""
    assert FAN_SPEED_MAP["Quiet"] is FanLevel.MUTE
    assert FAN_SPEED_MAP["Standard"] is FanLevel.NORMAL
    assert FAN_SPEED_MAP["Strong"] is FanLevel.STRONG
    assert FAN_SPEED_MAP["Super Powerful"] is FanLevel.DEEP
    assert int(FAN_SPEED_MAP["Super Powerful"]) == 4
    assert FAN_SPEED_MAP["Ultra Powerful"] is FanLevel.SUPER


def test_pre_rename_labels_still_resolve() -> None:
    """Labels shipped through v1.0.3 keep working in existing automations."""
    assert FAN_SPEED_MAP["Super"] is FanLevel.DEEP
    assert FAN_SPEED_MAP["Ultra"] is FanLevel.SUPER
    assert FAN_SPEED_MAP["Ultra powerful"] is FanLevel.SUPER
    assert FAN_SPEED_MAP["Super powerful"] is FanLevel.DEEP


def test_lowercase_aliases_still_resolve() -> None:
    """The original lowercase fan_speed values keep working too."""
    assert FAN_SPEED_MAP["quiet"] is FanLevel.MUTE
    assert FAN_SPEED_MAP["normal"] is FanLevel.NORMAL
    assert FAN_SPEED_MAP["strong"] is FanLevel.STRONG
    assert FAN_SPEED_MAP["max"] is FanLevel.SUPER


def test_aliases_are_not_offered_as_options() -> None:
    """Input-only compatibility spellings stay out of the canonical list."""
    for alias in ("Super", "Super powerful", "Ultra powerful", "quiet", "max"):
        assert alias not in FAN_SPEED_LIST


def test_every_offered_label_is_resolvable() -> None:
    """Guards vacuum._FAN_LABELS, which indexes FAN_SPEED_MAP by every offered label."""
    for label in FAN_SPEED_LIST:
        assert label in FAN_SPEED_MAP


def test_ultra_powerful_withheld_where_the_app_cannot_reach_it() -> None:
    """AX26's app exposes four tiers, so level 5 is not offered there (#70)."""
    assert fan_speed_list_for({CONF_PRODUCT_KEY: AX26}) == [
        "Quiet",
        "Standard",
        "Strong",
        "Super Powerful",
    ]


def test_other_models_keep_all_five_tiers() -> None:
    """Five-tier models advertise only the canonical labels."""
    assert fan_speed_list_for({CONF_PRODUCT_KEY: FLOW_2}) == FAN_SPEED_LIST


def test_unknown_or_missing_product_key_keeps_all_five_tiers() -> None:
    """Entries created before product_key persistence must not lose a tier."""
    assert fan_speed_list_for({}) == FAN_SPEED_LIST
    assert fan_speed_list_for({CONF_PRODUCT_KEY: None}) == FAN_SPEED_LIST
