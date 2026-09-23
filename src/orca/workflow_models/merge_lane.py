"""MergeLane[T]: async generator + anchor-based insertion merge with peek and skip.

The core data structure for V5.1's generator-based execution model. Merges a
primary async generator source with insertion/anchor mechanisms and a skip set.

Consumption priority at `next()`:
  1. After-anchor items pending for a consumed name. An inserted item's own
     name is pushed on top of the group it came from, so a chain off it runs
     before the rest of that group, and the group resumes once the chain ends.
  2. `_insertions` (AtHead deque, LIFO via appendleft + popleft).
  3. Before-anchor items: on peek of an item whose name matches a registered
     Before anchor, hold the anchor item and return the Before-insert first.
  4. Peeked generator item.
  5. Generator's next item.
  6. `_suffix` (AtTail deque, FIFO).

Skip set is name-based and one-shot: when an item's name matches, it is
filtered out and the skip entry is consumed.

Used at both method level (UnresolvedLocationAction, keyed by tag) and thread
level (ExecutingMethod, keyed by name).
"""

from collections import deque
from dataclasses import dataclass
from typing import AsyncGenerator, Callable, Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class DroppedAnchorInsert:
    """A Before/After insert still pending when the lane closed.

    Surfaced when the lane drains so the caller can record an
    ``UNRESOLVED_ANCHOR_INSERT`` incident. ``item_name`` is the inserted
    item's own name (``None`` when the inserted item is unnamed, e.g. an
    untagged action).

    ``anchor_reached`` says which of the two drops this is. False: the anchor
    name never came past on the stream, so the operator's target was never
    reached. True: it was, and the insert did not run anyway. Telling an
    operator their tag never appeared when the step it names went by sends
    them looking for a typo that is not there.
    """
    anchor_name: str
    direction: str          # "before" | "after"
    item_name: str | None
    anchor_reached: bool


class MergeLane(Generic[T]):

    def __init__(
        self,
        generator: AsyncGenerator[T, None],
        name_getter: Callable[[T], str | None],
    ) -> None:
        self._generator = generator
        self._name_getter = name_getter
        self._peeked: T | None = None
        self._has_peeked: bool = False
        self._insertions: deque[T] = deque()
        self._suffix: deque[T] = deque()
        self._skip_set: set[str] = set()
        self._before_anchors: dict[str, deque[T]] = {}
        self._after_anchors: dict[str, deque[T]] = {}
        # Every name that has come past on the consumption stream, so a
        # drop can say whether the anchor was ever reached.
        self._consumed_names: set[str] = set()
        # Names an After anchor may fire against, innermost last. An
        # inserted item is pushed on top of the group it came from, so a
        # chain off it runs before the rest of that group.
        self._pending_after_names: list[str] = []
        self._exhausted: bool = False

    @property
    def exhausted(self) -> bool:
        """Whether the generator has finished (does not account for insertions)."""
        return self._exhausted

    async def peek(self) -> T | None:
        """Look at the next item without consuming. Answers "is the lane
        empty", which is what both consumers ask it.

        An After-anchor item waiting on a consumed name IS reported: it
        is already committed to the stream, so saying the lane was empty told a
        consumer to stop with the insert still queued. A Before anchor is NOT,
        so peek returns the raw item where `next()` would return the insert
        that precedes it -- a Before only fires once `next()` commits to that
        raw item, and peeking is not that commitment.

        Never consumes and never mutates the lane.
        """
        for name in reversed(self._pending_after_names):
            after_queue = self._after_anchors.get(name)
            if after_queue:
                return after_queue[0]
        if self._insertions:
            return self._insertions[0]
        if self._has_peeked:
            return self._peeked
        if not self._exhausted:
            try:
                self._peeked = await self._generator.__anext__()
                self._has_peeked = True
                return self._peeked
            except StopAsyncIteration:
                self._exhausted = True
        if self._suffix:
            return self._suffix[0]
        return None

    async def next(self) -> T:
        """Consume next item.

        Priority:
          1. After-anchor items for a consumed name, innermost first.
          2. Insertions deque (AtHead) -- LIFO.
          3. Before-anchor items matching the next raw item's name.
          4. Peeked item.
          5. Generator.
          6. Suffix deque (AtTail) -- FIFO.
        """
        # 1. After-anchor fires first, innermost pending name first.
        while self._pending_after_names:
            name = self._pending_after_names[-1]
            after_queue = self._after_anchors.get(name)
            if not after_queue:
                self._after_anchors.pop(name, None)
                self._pending_after_names.pop()
                continue
            item = after_queue.popleft()
            self._note_consumed(item)
            if not after_queue:
                # Spent, so retire the name with the queue. Leaving it on the
                # stack would let a later insert anchor to a step the lane went
                # past several consumptions ago.
                del self._after_anchors[name]
                self._pending_after_names.pop()
            item_name = self._name_getter(item)
            if item_name is not None:
                self._pending_after_names.append(item_name)
            return item

        # 2. AtHead insertions.
        if self._insertions:
            item = self._insertions.popleft()
            self._record_consumed_name(item)
            return item

        # 3. Peek the next raw item (peeked / generator / suffix). If its name
        # has a Before anchor, hold it in the peeked slot and return the
        # Before-anchor insert instead.
        raw = await self._peek_raw()
        if raw is not None:
            name = self._name_getter(raw)
            if name is not None and name in self._before_anchors:
                before_queue = self._before_anchors[name]
                if before_queue:
                    # The lane reached the anchor: that is what a later drop
                    # report needs, and consuming it is a call away.
                    self._note_consumed(raw)
                    # Keep `raw` in the peeked slot (it stays until next call).
                    self._hold_in_peeked(raw)
                    item = before_queue.popleft()
                    if not before_queue:
                        del self._before_anchors[name]
                    self._record_consumed_name(item)
                    return item

        # 4-6. Consume the raw item normally.
        if self._has_peeked:
            item = self._peeked
            self._peeked = None
            self._has_peeked = False
            assert item is not None
            self._record_consumed_name(item)
            return item
        if not self._exhausted:
            try:
                item = await self._generator.__anext__()
                self._record_consumed_name(item)
                return item
            except StopAsyncIteration:
                self._exhausted = True
        if self._suffix:
            item = self._suffix.popleft()
            self._record_consumed_name(item)
            return item
        raise StopAsyncIteration

    async def _peek_raw(self) -> T | None:
        """Peek the next item that would come from peeked/generator/suffix.

        Unlike `peek()`, this does NOT consider insertions (those were already
        handled by the caller). Returns None if all raw sources are exhausted.
        """
        if self._has_peeked:
            return self._peeked
        if not self._exhausted:
            try:
                self._peeked = await self._generator.__anext__()
                self._has_peeked = True
                return self._peeked
            except StopAsyncIteration:
                self._exhausted = True
        if self._suffix:
            return self._suffix[0]
        return None

    def _hold_in_peeked(self, item: T) -> None:
        """No-op invariant: ``next()``'s Before-anchor branch passes only
        items from ``_peek_raw()``, which are already at peeked or suffix head."""
        if self._has_peeked and self._peeked is item:
            return
        if self._suffix and self._suffix[0] is item:
            return
        suffix_head = self._suffix[0] if self._suffix else None
        raise RuntimeError(
            f"_hold_in_peeked invariant violated: item {item!r} is neither "
            f"peeked (={self._peeked!r}, has_peeked={self._has_peeked}) "
            f"nor suffix head (={suffix_head!r})"
        )

    def _record_consumed_name(self, item: T) -> None:
        """Mark this item's name as the 'last consumed' for after-anchor firing.

        Recorded whether or not an After anchor is registered for it yet. An
        operator can only insert while the thread is paused, and the commonest
        pause is an error on the action they are anchoring to -- which has
        already been consumed. Requiring the anchor up front meant that insert
        could never fire.

        Only reached once every pending After name is spent, so this replaces
        the stack rather than adding to it.
        """
        self._note_consumed(item)
        name = self._name_getter(item)
        self._pending_after_names = [] if name is None else [name]

    def _note_consumed(self, item: T) -> None:
        """Remember that this name came past, for the drop report at close."""
        name = self._name_getter(item)
        if name is not None:
            self._consumed_names.add(name)

    # -- Insertion API -------------------------------------------------------

    def insert_next(self, item: T) -> None:
        """Insert item to be consumed next (AtHead, before generator's next).

        Consecutive calls LIFO: `insert_next(A); insert_next(B)` consumes B then A.
        """
        self._insertions.appendleft(item)

    def append(self, item: T) -> None:
        """Append item AFTER the generator is exhausted (AtTail, true end-of-queue).

        Consecutive calls FIFO: `append(A); append(B)` consumes A then B.
        """
        self._suffix.append(item)

    def insert_before(self, anchor_name: str, item: T) -> None:
        """Insert `item` to fire just before the first consumption of
        an item whose name matches `anchor_name`.

        Consecutive calls FIFO within the same anchor. An item still pending
        at ``close()`` is dropped and reported, whether the anchor was never
        reached or was reached before the insert was registered.
        """
        self._before_anchors.setdefault(anchor_name, deque()).append(item)

    def insert_after(self, anchor_name: str, item: T) -> None:
        """Insert `item` to fire just after an item named `anchor_name`.

        The LAST CONSUMED item counts, so anchoring to the step that just ran
        works: that is what an operator paused on a step is asking for. Beyond
        that the anchor waits for the next item of that name, so a name that
        appears twice fires the insert after the first of the two still to come.

        Consecutive calls FIFO within the same anchor. An insert anchored to an
        inserted item runs right after it, ahead of that anchor's later inserts.
        """
        self._after_anchors.setdefault(anchor_name, deque()).append(item)

    # -- Skip API ------------------------------------------------------------

    def add_skip(self, name: str) -> None:
        """Add a name to the skip set. One-shot: consumed on first match."""
        self._skip_set.add(name)

    def should_skip(self, name: str) -> bool:
        """Check if name should be skipped. Consumes the entry (one-shot)."""
        if name in self._skip_set:
            self._skip_set.discard(name)
            return True
        return False

    def unresolved_anchor_inserts(self) -> list[DroppedAnchorInsert]:
        """Snapshot every still-pending Before/After insert (non-destructive).

        Each carries ``anchor_reached``: whether its anchor name ever came past
        on the consumption stream. Order: before-anchors then after-anchors,
        each FIFO within its anchor.
        """
        dropped: list[DroppedAnchorInsert] = []
        for anchor_name, queue in self._before_anchors.items():
            ran = anchor_name in self._consumed_names
            for item in queue:
                dropped.append(DroppedAnchorInsert(
                    anchor_name, "before", self._name_getter(item), ran))
        for anchor_name, queue in self._after_anchors.items():
            ran = anchor_name in self._consumed_names
            for item in queue:
                dropped.append(DroppedAnchorInsert(
                    anchor_name, "after", self._name_getter(item), ran))
        return dropped

    async def close(self) -> list[DroppedAnchorInsert]:
        """Abandon generator and clear all state.

        Insertions/suffix added AFTER close still work (recovery pattern).
        Anchor queues and skip set are cleared. Returns the anchored inserts
        that were still pending, so the caller can surface them as
        ``UNRESOLVED_ANCHOR_INSERT`` incidents.
        """
        dropped = self.unresolved_anchor_inserts()
        if not self._exhausted:
            await self._generator.aclose()
            self._exhausted = True
        self._insertions.clear()
        self._suffix.clear()
        self._peeked = None
        self._has_peeked = False
        self._before_anchors.clear()
        self._after_anchors.clear()
        self._pending_after_names.clear()
        # _consumed_names is kept, alone among the state here: inserts added
        # after close are supported, and their drop report still has to say
        # whether the lane ever reached the anchor.
        self._skip_set.clear()
        return dropped
