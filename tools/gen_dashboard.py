"""Generate a room-picker dashboard section for the Narwal integration.

v1.0.8 creates six profile selects and a selection switch for every room on
the map — 168 entities for a 24-room house. Putting them all on a dashboard is
unusable. This tool emits Lovelace YAML that shows ONE room at a time, chosen
from a dropdown, so a dashboard carries seven tiles instead of 168.

It reads Home Assistant's entity registry rather than the robot, because the
registry is the only place that knows which entity_id belongs to which room:
entity_ids are minted from the room name at first registration and never
change, while unique_ids carry the stable ``map_<id>_room_<id>`` key.

Usage (from the repo root, with a copy of the registry file)::

    py tools/gen_dashboard.py --registry core.entity_registry \
        --vacuum vacuum.narwal_flow_vacuum --out build/

Outputs, all plain YAML you paste into your own dashboard / config:

- ``narwal_rooms_section.yaml``  — a sections-view grid: picker + per-room panel
- ``narwal_dock_section.yaml``   — a grid with the five dock task switches
- ``narwal_input_select.yaml``   — the ``input_select`` helper the picker uses
- ``narwal_script.yaml``         — ``script.narwal_clean_room``: select one
  room and start, so a room's own profile is used (``narwal.clean_rooms``
  deliberately takes explicit settings and ignores profiles)

Requires the ``state-switch`` custom card (HACS: lovelace-state-switch).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

ROOM_KEY = re.compile(r"_map_(?P<map>\w+?)_room_(?P<room>\d+)_(?P<key>mode|suction|water|scrub|route|passes|selected)$")
PROFILE_KEYS = ("mode", "suction", "water", "scrub", "route", "passes")
GLOBAL_SUFFIXES = {
    "_mode": "mode",
    "_runtime_suction": "suction",
    "_runtime_water": "water",
    "_scrub": "scrub",
    "_route": "route",
    "_passes": "passes",
}
DOCK_SUFFIXES = {
    "_empty_dustbin": "Empty dustbin",
    "_wash_mop": "Wash mop",
    "_dry_mop": "Dry mop",
    "_dry_dust_bin": "Dry dust bin",
    "_dry_dock_bag": "Dry dock bag",
}
LABELS = {
    "mode": "Mode",
    "suction": "Suction",
    "water": "Water",
    "scrub": "Scrub",
    "route": "Route",
    "passes": "Passes",
}
WHOLE_HOUSE = "Whole house"


def load_registry(path: Path) -> list[dict]:
    """Accept the raw .storage file or a pre-filtered list of entries."""
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    entries = payload["data"]["entities"] if isinstance(payload, dict) else payload
    return [e for e in entries if e.get("platform") == "narwal" or "platform" not in e]


def room_name(entry: dict, key: str) -> str:
    """Prefer the user's override name, else strip the key suffix from the original."""
    name = entry.get("name") or entry.get("original_name") or entry["entity_id"]
    suffix = " " + key
    return name[: -len(suffix)] if name.lower().endswith(suffix) else name


def collect(entries: list[dict], map_id: str | None):
    rooms: dict[int, dict] = {}
    globals_: dict[str, str] = {}
    dock: dict[str, str] = {}
    maps: set[str] = set()
    for e in entries:
        uid, eid = e["unique_id"], e["entity_id"]
        if m := ROOM_KEY.search(uid):
            if map_id is not None and m["map"] != map_id:
                continue
            if e.get("disabled_by") is not None:
                if m["key"] == "selected":
                    sys.exit(
                        f"{eid} is disabled; re-enable it and clear any stale "
                        "selection before generating the dashboard"
                    )
                continue
            maps.add(m["map"])
            room = rooms.setdefault(int(m["room"]), {"entities": {}})
            room["entities"][m["key"]] = eid
            room.setdefault("name", room_name(e, m["key"]))
            continue
        for suffix, key in GLOBAL_SUFFIXES.items():
            if uid.endswith(suffix) and eid.startswith("select."):
                globals_[key] = eid
        for suffix, label in DOCK_SUFFIXES.items():
            if uid.endswith(suffix) and eid.startswith("switch."):
                dock[label] = eid
    if map_id is None and len(maps) > 1:
        sys.exit(f"registry holds rooms for several maps {sorted(maps)}; pass --map-id")
    complete = {rid: r for rid, r in rooms.items() if "selected" in r["entities"]}
    return dict(sorted(complete.items(), key=lambda kv: kv[1]["name"].lower())), globals_, dock


def tile(entity: str, name: str, feature: str | None = None, **extra) -> dict:
    card = {"type": "tile", "entity": entity, "name": name}
    if feature:
        card["features"] = [{"type": feature}]
    card.update(extra)
    return card


def button(name: str, icon: str, action: str, *, target: dict | None = None, data: dict | None = None) -> dict:
    tap = {"action": "perform-action", "perform_action": action}
    if target:
        tap["target"] = target
    if data:
        tap["data"] = data
    return {"type": "button", "name": name, "icon": icon, "show_state": False, "tap_action": tap}


def rooms_section(rooms, globals_, vacuum, picker, script) -> dict:
    selected_switches = [r["entities"]["selected"] for r in rooms.values()]
    states = {
        WHOLE_HOUSE: {
            "type": "vertical-stack",
            "cards": [
                {
                    "type": "markdown",
                    "content": "Defaults for every room that has no profile of its own. "
                    "Pick a room above to give it different settings.",
                },
                *[tile(globals_[k], LABELS[k], "select-options") for k in PROFILE_KEYS if k in globals_],
            ],
        }
    }
    for room in rooms.values():
        ents = room["entities"]
        cards = [tile(ents["selected"], "Include in next start", "toggle")]
        cards += [tile(ents[k], LABELS[k], "select-options") for k in PROFILE_KEYS if k in ents]
        cards.append(
            button(
                f"Clean {room['name']} only",
                "mdi:broom",
                script,
                data={"room_switch": ents["selected"]},
            )
        )
        states[room["name"]] = {"type": "vertical-stack", "cards": cards}

    return {
        "type": "grid",
        "column_span": 2,
        "cards": [
            {"type": "heading", "heading": "Rooms", "heading_style": "title", "icon": "mdi:floor-plan"},
            {
                "type": "markdown",
                "content": "**Whole house** holds the defaults. Pick a room to set its own mode, "
                "suction, water, scrub, route and passes — those are applied when the robot "
                "cleans that room, whether by **Start**, by the room's own button, or from the app-style "
                "**Clean areas** picker on the vacuum. Rooms you never touch follow Whole house.",
            },
            tile(picker, "Room", "select-options"),
            {
                "type": "horizontal-stack",
                "cards": [
                    button(
                        "Start (selected rooms, or all)",
                        "mdi:play",
                        "vacuum.start",
                        target={"entity_id": vacuum},
                    ),
                    button(
                        "Clear selection",
                        "mdi:select-off",
                        "switch.turn_off",
                        target={"entity_id": selected_switches},
                    ),
                ],
            },
            {
                "type": "custom:state-switch",
                "entity": picker,
                "default": WHOLE_HOUSE,
                "states": states,
            },
        ],
    }


def dock_section(dock) -> dict:
    return {
        "type": "grid",
        "cards": [
            {"type": "heading", "heading": "Dock tasks", "heading_style": "title", "icon": "mdi:home-import-outline"},
            {
                "type": "markdown",
                "content": "A switch is on while that task runs and shows time left. It is only "
                "available when the task can be started or safely stopped right now.",
            },
            *[
                tile(eid, label, "toggle", state_content=["state", "time_left"])
                for label, eid in dock.items()
            ],
        ],
    }


def input_select_config(rooms, name: str) -> dict:
    return {"name": name, "icon": "mdi:floor-plan", "options": [WHOLE_HOUSE, *[r["name"] for r in rooms.values()]]}


def script_config(rooms, vacuum: str) -> dict:
    selected_switches = [r["entities"]["selected"] for r in rooms.values()]
    return {
        "alias": "Narwal: clean one room",
        "description": "Select exactly one room and start, so the room's own profile is used.",
        "icon": "mdi:broom",
        "mode": "single",
        "fields": {
            "room_switch": {
                "name": "Room",
                "description": "The room's 'selected' switch",
                "required": True,
                "selector": {"entity": {"domain": "switch", "integration": "narwal"}},
            }
        },
        "sequence": [
            {"action": "switch.turn_off", "target": {"entity_id": selected_switches}},
            {"action": "switch.turn_on", "target": {"entity_id": "{{ room_switch }}"}},
            {"delay": {"seconds": 1}},
            {"action": "vacuum.start", "target": {"entity_id": vacuum}},
        ],
    }


def dump(obj) -> str:
    return yaml.safe_dump(obj, sort_keys=False, allow_unicode=True, width=100)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--registry", required=True, type=Path, help="core.entity_registry (or a JSON list of narwal entries)")
    parser.add_argument("--vacuum", required=True, help="vacuum entity_id, e.g. vacuum.narwal_flow_vacuum")
    parser.add_argument("--picker", default="input_select.narwal_room", help="input_select entity_id for the room dropdown")
    parser.add_argument("--picker-name", default="Narwal room")
    parser.add_argument("--script", default="script.narwal_clean_room", help="script entity_id for 'clean this room only'")
    parser.add_argument("--map-id", help="restrict to one map when the registry knows several")
    parser.add_argument("--out", type=Path, default=Path("build"))
    args = parser.parse_args()

    rooms, globals_, dock = collect(load_registry(args.registry), args.map_id)
    if not rooms:
        sys.exit(
            "no enabled room controls found; enable the Narwal room entities "
            "you want on the dashboard, then run this tool again"
        )
    missing = [k for k in PROFILE_KEYS if k not in globals_]
    if missing:
        print(f"warning: global selects missing for {missing}; Whole house panel will be partial", file=sys.stderr)

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "narwal_rooms_section.yaml").write_text(
        dump([rooms_section(rooms, globals_, args.vacuum, args.picker, args.script)]), encoding="utf-8"
    )
    (args.out / "narwal_dock_section.yaml").write_text(dump([dock_section(dock)]) if dock else "", encoding="utf-8")
    (args.out / "narwal_input_select.yaml").write_text(
        dump({args.picker.split(".", 1)[1]: input_select_config(rooms, args.picker_name)}), encoding="utf-8"
    )
    (args.out / "narwal_script.yaml").write_text(
        dump({args.script.split(".", 1)[1]: script_config(rooms, args.vacuum)}), encoding="utf-8"
    )
    print(f"{len(rooms)} rooms, {len(globals_)} global selects, {len(dock)} dock switches -> {args.out}/")
    for rid, room in rooms.items():
        print(f"  room {rid:>3}  {room['name']}")


if __name__ == "__main__":
    main()
