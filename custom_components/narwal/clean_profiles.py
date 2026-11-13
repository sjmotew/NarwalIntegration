"""Stable service keys shared by room cleaning and pending profiles."""

from collections.abc import Mapping
from typing import NotRequired, TypedDict

from .const import fan_speed_map_for
from .narwal_client import CleaningRoute, FanLevel, MopHumidity, MopStrengthLevel, WorkMode


class PendingProfileInput(TypedDict):
    """Schema-validated service profile, before model and mode validation."""

    mode: str
    passes: int
    route: str
    suction: NotRequired[str]
    water: NotRequired[str]
    mop_strength: NotRequired[str]


WORK_MODE_OPTIONS: dict[str, WorkMode] = {
    "vacuum": WorkMode.VACUUM,
    "mop": WorkMode.MOP,
    "vacuum_then_mop": WorkMode.VACUUM_THEN_MOP,
    "vacuum_and_mop": WorkMode.VACUUM_AND_MOP,
}
SUCTION_OPTIONS = (
    "ai",
    "quiet",
    "standard",
    "normal",
    "strong",
    "super_powerful",
    "ultra_powerful",
    "super",
    "ultra",
    "max",
)
WATER_OPTIONS: dict[str, MopHumidity] = {
    "dry": MopHumidity.DRY,
    "normal": MopHumidity.NORMAL,
    "wet": MopHumidity.WET,
}
MOP_STRENGTH_OPTIONS: dict[str, MopStrengthLevel] = {
    "normal": MopStrengthLevel.NORMAL,
    "high": MopStrengthLevel.HIGH,
}
ROUTE_OPTIONS: dict[str, CleaningRoute] = {
    "standard": CleaningRoute.STANDARD,
    "meticulous": CleaningRoute.METICULOUS,
}

SUCTION_OPTION_LABELS: dict[str, str] = {
    "quiet": "Quiet",
    "standard": "Standard",
    "normal": "Standard",
    "strong": "Strong",
    "ultra": "Ultra Powerful",
    "ultra_powerful": "Ultra Powerful",
    "super": "Super Powerful",
    "super_powerful": "Super Powerful",
    "max": "Super Powerful",
}


def suction_for_model(
    model_data: Mapping[str, object],
    option: str,
) -> FanLevel:
    """Return the model-valid suction level for a service option."""
    if option == "ai":
        return FanLevel.UNSPECIFIED
    data = dict(model_data)
    fan_map = fan_speed_map_for(data, include_aliases=False)
    if option == "max":
        return list(fan_map.values())[-1]
    compatibility_label = {
        "super": "Super",
        "ultra": "Ultra",
    }.get(option)
    if compatibility_label is not None:
        compatibility_level = fan_speed_map_for(data).get(compatibility_label)
        if compatibility_level is None:
            raise ValueError(f"{compatibility_label} suction is not supported by this Narwal model")
        return compatibility_level
    label = SUCTION_OPTION_LABELS[option]
    if label not in fan_map:
        raise ValueError(f"{label} suction is not supported by this Narwal model")
    return fan_map[label]
