"""Tests for the optional Narwal dashboard generator."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("yaml", MagicMock())

from tools.gen_dashboard import collect, rooms_section  # noqa: E402


def test_collect_ignores_disabled_room_controls() -> None:
    """Generated dashboards only target entities that HA actually creates."""
    entries = [
        {
            "unique_id": "dev_map_200_room_4_mode",
            "entity_id": "select.narwal_kitchen_mode",
            "original_name": "Kitchen mode",
            "disabled_by": "integration",
        },
        {
            "unique_id": "dev_map_100_room_5_selected",
            "entity_id": "switch.narwal_hallway_selected",
            "original_name": "Hallway selected",
            "disabled_by": None,
        },
    ]

    rooms, _, _ = collect(entries, map_id=None)

    assert list(rooms) == [5]


def test_collect_rejects_disabled_selection_switch() -> None:
    """A possibly selected room cannot disappear from generated clear actions."""
    entries = [
        {
            "unique_id": "dev_map_100_room_4_selected",
            "entity_id": "switch.narwal_kitchen_selected",
            "original_name": "Kitchen selected",
            "disabled_by": "user",
        }
    ]

    with pytest.raises(SystemExit, match="re-enable it and clear"):
        collect(entries, map_id="100")


def test_requested_map_ignores_disabled_selection_on_other_map() -> None:
    """A stale disabled switch cannot block an explicitly requested map."""
    entries = [
        {
            "unique_id": "dev_map_200_room_4_selected",
            "entity_id": "switch.narwal_old_kitchen_selected",
            "disabled_by": "user",
        },
        {
            "unique_id": "dev_map_100_room_5_selected",
            "entity_id": "switch.narwal_hallway_selected",
            "original_name": "Hallway selected",
            "disabled_by": None,
        },
    ]

    rooms, _, _ = collect(entries, map_id="100")

    assert list(rooms) == [5]


def test_collects_room_order_without_corrupting_room_name() -> None:
    """The order entity belongs to the room and strips its longer suffix."""
    entries = [
        {
            "unique_id": "dev_map_100_room_4_clean_order",
            "entity_id": "number.narwal_kitchen_cleaning_order",
            "original_name": "Kitchen cleaning order",
        },
        {
            "unique_id": "dev_map_100_room_4_selected",
            "entity_id": "switch.narwal_kitchen_selected",
            "original_name": "Kitchen selected",
        },
    ]

    rooms, _, _ = collect(entries, map_id="100")

    assert rooms[4]["name"] == "Kitchen"
    assert rooms[4]["entities"]["clean_order"] == ("number.narwal_kitchen_cleaning_order")


def test_room_panel_includes_numeric_order_control() -> None:
    """Generated room panels expose the persisted order number."""
    rooms = {
        4: {
            "name": "Kitchen",
            "entities": {
                "selected": "switch.narwal_kitchen_selected",
                "clean_order": "number.narwal_kitchen_cleaning_order",
            },
        }
    }

    section = rooms_section(
        rooms,
        {},
        "vacuum.narwal",
        "input_select.narwal_room",
        "script.narwal_clean_room",
    )
    room_cards = section["cards"][-1]["states"]["Kitchen"]["cards"]

    assert room_cards[1]["entity"] == "number.narwal_kitchen_cleaning_order"
    assert room_cards[1]["features"] == [
        {"type": "numeric-input", "style": "buttons"}
    ]
