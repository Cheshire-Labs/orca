"""Where to place a method/action inserted via the mutation coordinator.

Replaces the old `position: int` parameter, which was effectively binary
(0 = insert next, anything else = append at end) and misleading about
intermediate indices. A generator-backed queue has no meaningful index,
so placement is either relative to the consumption head (AtHead/AtTail)
or anchored to a named peer (Before/After).

Each variant encapsulates its own placement behavior via `apply()`, so the
mutation machinery does not branch on type. Callers pass any `InsertPosition`
and the target lane receives the correct call.

Anchors match by name:
- For methods: `MethodTemplate.name` (the decorated function name).
- For actions: `ActionTemplate.tag` only. Untagged actions cannot be anchored.

Semantics on the consumption side (see `MergeLane`):
- AtHead and AtTail are fired immediately on insert (placed in the
  insertions or suffix deque, respectively).
- Before(name) is held until the anchor name appears on the consumption
  stream. After(name) also accepts the item that was JUST consumed, so
  anchoring to the step a paused thread is standing on works. First match
  wins; subsequent occurrences of the same name are ignored.
- An insert still pending at lane close is surfaced as an `UNRESOLVED_ANCHOR_INSERT`
  incident at lane close.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Protocol, TypeVar

T_contra = TypeVar("T_contra", contravariant=True)


class InsertTarget(Protocol[T_contra]):
    """Minimal lane-like protocol that InsertPosition variants apply against.

    MergeLane satisfies this structurally; other future queue-like targets can
    too, without InsertPosition needing to know concrete types.
    """

    def insert_next(self, item: T_contra) -> None: ...
    def append(self, item: T_contra) -> None: ...
    def insert_before(self, anchor_name: str, item: T_contra) -> None: ...
    def insert_after(self, anchor_name: str, item: T_contra) -> None: ...


T = TypeVar("T")


class InsertPosition(ABC):
    """Polymorphic placement descriptor. Subclasses encapsulate one behavior."""

    @abstractmethod
    def apply(self, target: InsertTarget[T], item: T) -> None: ...


@dataclass(frozen=True)
class AtHead(InsertPosition):
    """Run immediately next, ahead of any pending generator item.

    Consecutive AtHead inserts consume LIFO (last-inserted runs first).
    """

    def apply(self, target: InsertTarget[T], item: T) -> None:
        target.insert_next(item)


@dataclass(frozen=True)
class AtTail(InsertPosition):
    """Run after all remaining generator items have run.

    Consecutive AtTail inserts consume FIFO (first-inserted runs first).
    """

    def apply(self, target: InsertTarget[T], item: T) -> None:
        target.append(item)


@dataclass(frozen=True)
class Before(InsertPosition):
    """Run immediately before the first consumption whose name matches `anchor_name`.

    First match wins; later occurrences of the same name are not re-targeted.
    Consecutive Before inserts for the same anchor consume FIFO (first-inserted
    fires first, before the anchor).
    """

    anchor_name: str

    def apply(self, target: InsertTarget[T], item: T) -> None:
        target.insert_before(self.anchor_name, item)


@dataclass(frozen=True)
class After(InsertPosition):
    """Run immediately after the first consumption whose name matches `anchor_name` completes.

    Same first-match-wins and FIFO semantics as Before.
    """

    anchor_name: str

    def apply(self, target: InsertTarget[T], item: T) -> None:
        target.insert_after(self.anchor_name, item)
