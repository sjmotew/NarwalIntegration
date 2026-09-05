"""Tests for the optional Narwal dashboard generator."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("yaml", MagicMock())

from tools.gen_dashboard import collect  # noqa: E402


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
