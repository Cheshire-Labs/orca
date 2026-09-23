"""Split a channel-ordered sequence into consecutive same-labware runs.

One head operation can reach across two labware, and every layer that describes
it has to describe it the same way. They did not: the wire split correctly and
the ledger wrote every position under the FIRST labware's name, so one was
debited for what another held and the other read untouched. Tips and wells both
group here now.

Runs, not groups: `[A1@a, B1@a, A1@b, A2@a]` is three runs, not two labware. The
order channels engage is what the positions mean, and collapsing the two `a`
runs together would misreport which channel touched which well.
"""

from typing import Callable, Protocol, Sequence, TypeVar


class HasParentName(Protocol):
    @property
    def parent_name(self) -> str: ...

    @property
    def identifier(self) -> str: ...


T = TypeVar("T")


def consecutive_runs(
    items: Sequence[T], key: Callable[[T], str],
) -> list[tuple[str, list[T]]]:
    """Consecutive same-key runs, in the order given."""
    runs: list[tuple[str, list[T]]] = []
    current_key: str | None = None
    current: list[T] = []
    for item in items:
        if key(item) != current_key:
            if current_key is not None:
                runs.append((current_key, current))
            current_key = key(item)
            current = []
        current.append(item)
    if current_key is not None:
        runs.append((current_key, current))
    return runs


def consecutive_runs_by_parent(
    tip_spots: Sequence[HasParentName],
) -> list[tuple[str, list[str]]]:
    """Consecutive same-parent runs as (rack name, positions), in channel order."""
    return [
        (rack, [spot.identifier for spot in spots])
        for rack, spots in consecutive_runs(tip_spots, lambda s: s.parent_name)
    ]
