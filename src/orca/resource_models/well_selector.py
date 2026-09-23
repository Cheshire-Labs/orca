from dataclasses import dataclass


@dataclass(frozen=True)
class WellSelector:
    mode: str
    spec: str | list[str] | None


def all_wells() -> WellSelector:
    return WellSelector(mode="all", spec=None)


def quadrant(position: str) -> WellSelector:
    valid = {"tl", "tr", "bl", "br"}
    if position not in valid:
        raise ValueError(f"Invalid quadrant '{position}'. Must be one of {valid}")
    return WellSelector(mode="quadrant", spec=position)


def well_range(start: str, end: str) -> WellSelector:
    return WellSelector(mode="range", spec=f"{start}:{end}")


def well_list(wells: list[str]) -> WellSelector:
    return WellSelector(mode="list", spec=wells)
