"""Every entity translation_key must have a matching name in strings.json / en.json.

PR #62 added three map-display switches whose `translation_key`s had no entry in
`strings.json`, so Home Assistant fell back to the device name and created three
entities all called "Narwal Flow" — `switch.<device>`, `_2` and `_3`. Nothing in
the test suite noticed, because the stubs never render an entity name.

Like `test_intra_package_imports`, this reads the source rather than importing it,
so it needs neither Home Assistant nor `ha_stubs`.
"""

from __future__ import annotations

import ast
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "custom_components" / "narwal"

# Platform modules whose entities carry translation keys, mapped to their HA domain.
PLATFORM_DOMAINS = {
    "vacuum": "vacuum",
    "sensor": "sensor",
    "binary_sensor": "binary_sensor",
    "select": "select",
    "number": "number",
    "switch": "switch",
    "light": "light",
    "camera": "camera",
}


def _string_constants() -> dict[str, str]:
    """Module-level `NAME = "literal"` pairs from const.py, for indirect keys."""
    tree = ast.parse((PACKAGE / "const.py").read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out[target.id] = node.value.value
    return out


def _translation_keys(module: pathlib.Path, consts: dict[str, str]) -> set[str]:
    """translation_key=... and _attr_translation_key = ... values in a module."""
    tree = ast.parse(module.read_text(encoding="utf-8"))
    keys: set[str] = set()

    def resolve(node: ast.expr) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return consts.get(node.id)
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "translation_key":
            if (key := resolve(node.value)) is not None:
                keys.add(key)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == "_attr_translation_key"
                    and (key := resolve(node.value)) is not None
                ):
                    keys.add(key)
    return keys


def test_every_translation_key_has_a_name() -> None:
    """No entity may fall back to the device name for lack of a translation."""
    consts = _string_constants()
    catalogues = {
        "strings.json": json.loads((PACKAGE / "strings.json").read_text(encoding="utf-8")),
        "translations/en.json": json.loads(
            (PACKAGE / "translations" / "en.json").read_text(encoding="utf-8")
        ),
    }

    missing: list[str] = []
    for stem, domain in PLATFORM_DOMAINS.items():
        module = PACKAGE / f"{stem}.py"
        if not module.exists():
            continue
        for key in sorted(_translation_keys(module, consts)):
            for label, data in catalogues.items():
                entry = data.get("entity", {}).get(domain, {}).get(key)
                if not entry or not entry.get("name"):
                    missing.append(f"{label}: entity.{domain}.{key}.name is missing")

    assert not missing, "untranslated entity names:\n  " + "\n  ".join(missing)
