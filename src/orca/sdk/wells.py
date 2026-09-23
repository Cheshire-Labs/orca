"""Well/position address helpers for SBS-format labware.

These produce **lists of explicit well strings** (e.g. ``["A1", "B1", ...]``)
suitable for passing to ``DeclaredTracking(tips_used=..., wells_used=...)``
on closed-protocol actions, where orca cannot introspect the real behavior.

For RUNTIME well selection (passed to ``@orca.action(well_selectors=...)``),
use the ``WellSelector`` factory functions in
``orca.resource_models.well_selector`` instead. Those resolve against the
actual labware geometry at dispatch time; the helpers here just compute the
address strings up front.

Addresses follow PyLabRobot's column-major enumeration convention:
``A1, B1, ..., H1, A2, B2, ..., H12`` for a 96-well layout.
"""

from typing import Tuple


_SBS_ROWS_96 = "ABCDEFGH"
_SBS_COLS_96 = 12

_SBS_ROWS_384 = "ABCDEFGHIJKLMNOP"
_SBS_COLS_384 = 24


def _build(rows: str, cols: range) -> list[str]:
    return [f"{r}{c}" for c in cols for r in rows]


def columns_96(start: int, end: int) -> list[str]:
    """All 96-format wells in columns ``[start, end]`` inclusive (A1..H<end>).

    Example: ``columns_96(1, 3)`` returns the 24 wells of the first three columns.
    """
    if not (1 <= start <= end <= _SBS_COLS_96):
        raise ValueError(
            f"columns_96 range {start}..{end} outside valid [1, {_SBS_COLS_96}]"
        )
    return _build(_SBS_ROWS_96, range(start, end + 1))


def columns_384(start: int, end: int) -> list[str]:
    """All 384-format wells in columns ``[start, end]`` inclusive (A1..P<end>)."""
    if not (1 <= start <= end <= _SBS_COLS_384):
        raise ValueError(
            f"columns_384 range {start}..{end} outside valid [1, {_SBS_COLS_384}]"
        )
    return _build(_SBS_ROWS_384, range(start, end + 1))


def column_stripes_96(n: int) -> Tuple[list[str], ...]:
    """Partition a 96-well plate into ``n`` equal column-stripes, left to right.

    Returns a tuple of ``n`` well-address lists. ``n`` must evenly divide 12.
    Example: ``q1, q2, q3, q4 = column_stripes_96(4)`` gives four 24-well stripes
    at columns 1..3, 4..6, 7..9, 10..12.
    """
    if _SBS_COLS_96 % n != 0:
        raise ValueError(
            f"column_stripes_96({n}) requires n to divide {_SBS_COLS_96} evenly"
        )
    width = _SBS_COLS_96 // n
    return tuple(
        columns_96(1 + i * width, (i + 1) * width) for i in range(n)
    )


def all_wells_96() -> list[str]:
    """Every well on a 96-format plate, column-major order."""
    return _build(_SBS_ROWS_96, range(1, _SBS_COLS_96 + 1))


def all_wells_384() -> list[str]:
    """Every well on a 384-format plate, column-major order."""
    return _build(_SBS_ROWS_384, range(1, _SBS_COLS_384 + 1))
