"""Tests for MergeLane[T]: async generator + insertion deque merge with peek and skip.

Covers base merging behaviors (insert_next / append / peek / skip / close) and
the anchor-based insertion mechanism (insert_before / insert_after).
"""

import asyncio
from typing import AsyncGenerator

import pytest


from orca.workflow_models.merge_lane import MergeLane


# --- Helpers ---

async def _items_to_generator(*items: str) -> AsyncGenerator[str, None]:
    for item in items:
        yield item


async def _slow_generator(*items: str) -> AsyncGenerator[str, None]:
    for item in items:
        await asyncio.sleep(0.01)
        yield item


async def _crashing_generator(items: list[str], crash_after: int) -> AsyncGenerator[str, None]:
    for i, item in enumerate(items):
        if i >= crash_after:
            raise RuntimeError("generator crashed")
        yield item


def _identity_name(item: str) -> str | None:
    """Default name_getter for string-valued lanes: the item is its own name."""
    return item


def _make_lane(*items: str) -> MergeLane[str]:
    return MergeLane(_items_to_generator(*items), name_getter=_identity_name)


async def _collect_all(lane: MergeLane[str]) -> list[str]:
    """Drain all items from a lane."""
    result: list[str] = []
    while True:
        try:
            item = await lane.next()
            result.append(item)
        except StopAsyncIteration:
            break
    return result


# ---------------------------------------------------------------------------
# Generator basics
# ---------------------------------------------------------------------------

class TestGeneratorBasics:

    @pytest.mark.asyncio
    async def test_next_yields_items_in_order(self) -> None:
        lane = _make_lane("a", "b", "c")
        assert await lane.next() == "a"
        assert await lane.next() == "b"
        assert await lane.next() == "c"

    @pytest.mark.asyncio
    async def test_next_raises_stop_when_exhausted(self) -> None:
        lane = _make_lane("a")
        await lane.next()  # consume "a"
        with pytest.raises(StopAsyncIteration):
            await lane.next()

    @pytest.mark.asyncio
    async def test_empty_generator_raises_stop_immediately(self) -> None:
        lane = _make_lane()
        with pytest.raises(StopAsyncIteration):
            await lane.next()

    @pytest.mark.asyncio
    async def test_collect_all_drains_generator(self) -> None:
        lane = _make_lane("a", "b", "c")
        assert await _collect_all(lane) == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Peek
# ---------------------------------------------------------------------------

class TestPeek:

    @pytest.mark.asyncio
    async def test_peek_returns_next_without_consuming(self) -> None:
        lane = _make_lane("a", "b")
        peeked = await lane.peek()
        assert peeked == "a"
        # next() should still return "a" (not consumed by peek)
        assert await lane.next() == "a"
        assert await lane.next() == "b"

    @pytest.mark.asyncio
    async def test_peek_then_next_returns_same_item(self) -> None:
        lane = _make_lane("x")
        peeked = await lane.peek()
        consumed = await lane.next()
        assert peeked == consumed == "x"

    @pytest.mark.asyncio
    async def test_peek_when_exhausted_returns_none(self) -> None:
        lane = _make_lane()
        assert await lane.peek() is None

    @pytest.mark.asyncio
    async def test_multiple_peeks_return_same_item(self) -> None:
        lane = _make_lane("a", "b")
        assert await lane.peek() == "a"
        assert await lane.peek() == "a"
        assert await lane.peek() == "a"
        # still consumable
        assert await lane.next() == "a"

    @pytest.mark.asyncio
    async def test_peek_after_partial_consumption(self) -> None:
        lane = _make_lane("a", "b", "c")
        await lane.next()  # consume "a"
        assert await lane.peek() == "b"
        assert await lane.next() == "b"


# ---------------------------------------------------------------------------
# Insertions
# ---------------------------------------------------------------------------

class TestInsertions:

    @pytest.mark.asyncio
    async def test_insert_next_consumed_before_generator(self) -> None:
        lane = _make_lane("gen_1", "gen_2")
        lane.insert_next("inserted")
        assert await lane.next() == "inserted"
        assert await lane.next() == "gen_1"
        assert await lane.next() == "gen_2"

    @pytest.mark.asyncio
    async def test_append_consumed_after_generator(self) -> None:
        """append() adds to suffix -- consumed AFTER generator exhausts."""
        lane = _make_lane("gen_1")
        lane.insert_next("urgent")
        lane.append("later")
        assert await lane.next() == "urgent"  # insertion first
        assert await lane.next() == "gen_1"   # generator second
        assert await lane.next() == "later"   # suffix last

    @pytest.mark.asyncio
    async def test_multiple_appends_preserve_order_after_generator(self) -> None:
        lane = _make_lane("gen")
        lane.append("first_appended")
        lane.append("second_appended")
        assert await lane.next() == "gen"              # generator first
        assert await lane.next() == "first_appended"   # suffix
        assert await lane.next() == "second_appended"  # suffix

    @pytest.mark.asyncio
    async def test_multiple_insert_next_lifo_order(self) -> None:
        lane = _make_lane("gen")
        lane.insert_next("first_inserted")
        lane.insert_next("second_inserted")
        # insert_next pushes to front, so second_inserted comes first
        assert await lane.next() == "second_inserted"
        assert await lane.next() == "first_inserted"
        assert await lane.next() == "gen"

    @pytest.mark.asyncio
    async def test_insertion_after_generator_exhausted(self) -> None:
        lane = _make_lane("a")
        await lane.next()  # exhaust
        with pytest.raises(StopAsyncIteration):
            await lane.next()
        # Now insert -- should still be consumable
        lane.append("late_arrival")
        assert await lane.next() == "late_arrival"

    @pytest.mark.asyncio
    async def test_peek_shows_insertion_over_generator(self) -> None:
        lane = _make_lane("gen_1")
        lane.insert_next("inserted")
        assert await lane.peek() == "inserted"
        assert await lane.next() == "inserted"
        assert await lane.peek() == "gen_1"

    @pytest.mark.asyncio
    async def test_insertion_while_generator_has_peeked_item(self) -> None:
        lane = _make_lane("gen_1", "gen_2")
        await lane.peek()  # buffers "gen_1" in _peeked
        lane.insert_next("inserted")
        # Insertion takes priority over peeked item
        assert await lane.next() == "inserted"
        assert await lane.next() == "gen_1"  # was peeked, now consumed
        assert await lane.next() == "gen_2"


# ---------------------------------------------------------------------------
# Skip
# ---------------------------------------------------------------------------

class TestSkip:

    @pytest.mark.asyncio
    async def test_should_skip_returns_true_for_added_name(self) -> None:
        lane = _make_lane()
        lane.add_skip("method_a")
        assert lane.should_skip("method_a") is True

    @pytest.mark.asyncio
    async def test_should_skip_consumes_entry(self) -> None:
        lane = _make_lane()
        lane.add_skip("method_a")
        assert lane.should_skip("method_a") is True
        # Second check: already consumed
        assert lane.should_skip("method_a") is False

    @pytest.mark.asyncio
    async def test_should_skip_returns_false_for_unknown(self) -> None:
        lane = _make_lane()
        assert lane.should_skip("unknown") is False

    @pytest.mark.asyncio
    async def test_skip_multiple_names(self) -> None:
        lane = _make_lane()
        lane.add_skip("a")
        lane.add_skip("b")
        assert lane.should_skip("a") is True
        assert lane.should_skip("b") is True
        assert lane.should_skip("c") is False


# ---------------------------------------------------------------------------
# Close
# ---------------------------------------------------------------------------

class TestClose:

    @pytest.mark.asyncio
    async def test_close_stops_generator(self) -> None:
        lane = _make_lane("a", "b", "c")
        await lane.next()  # consume "a"
        await lane.close()
        with pytest.raises(StopAsyncIteration):
            await lane.next()

    @pytest.mark.asyncio
    async def test_close_clears_insertions(self) -> None:
        lane = _make_lane("gen")
        lane.append("inserted")
        await lane.close()
        with pytest.raises(StopAsyncIteration):
            await lane.next()

    @pytest.mark.asyncio
    async def test_close_clears_peeked(self) -> None:
        lane = _make_lane("a", "b")
        await lane.peek()  # buffers "a"
        await lane.close()
        assert await lane.peek() is None

    @pytest.mark.asyncio
    async def test_next_after_close_raises_stop(self) -> None:
        lane = _make_lane("a")
        await lane.close()
        with pytest.raises(StopAsyncIteration):
            await lane.next()

    @pytest.mark.asyncio
    async def test_insertion_after_close_still_works(self) -> None:
        """After close, generator is dead but insertions should still work.
        This supports the recovery pattern: generator crashes, thread pauses,
        operator inserts methods, thread resumes from insertions."""
        lane = _make_lane("a", "b")
        await lane.close()
        lane.append("recovery_method")
        assert await lane.next() == "recovery_method"

    @pytest.mark.asyncio
    async def test_close_clears_anchor_state(self) -> None:
        """Close() clears pending anchor inserts (no caller-facing API
        exposes them after close)."""
        lane = _make_lane("a")
        lane.insert_before("never_appears", "pending_before")
        lane.insert_after("also_never", "pending_after")

        assert lane._before_anchors  # internal: anchors present before close
        assert lane._after_anchors

        await lane.close()

        assert lane._before_anchors == {}
        assert lane._after_anchors == {}


# ---------------------------------------------------------------------------
# Async behavior
# ---------------------------------------------------------------------------

class TestAsyncBehavior:

    @pytest.mark.asyncio
    async def test_next_awaits_slow_generator(self) -> None:
        lane = MergeLane(_slow_generator("a", "b"), name_getter=_identity_name)
        assert await lane.next() == "a"
        assert await lane.next() == "b"

    @pytest.mark.asyncio
    async def test_generator_exception_propagates(self) -> None:
        lane = MergeLane(
            _crashing_generator(["a", "b", "c"], crash_after=2),
            name_getter=_identity_name,
        )
        assert await lane.next() == "a"
        assert await lane.next() == "b"
        with pytest.raises(RuntimeError, match="generator crashed"):
            await lane.next()

    @pytest.mark.asyncio
    async def test_generator_exception_then_insertion_recovery(self) -> None:
        """After generator crashes, insertions should still work.
        This is the crash recovery pattern."""
        lane = MergeLane(
            _crashing_generator(["a", "b"], crash_after=1),
            name_getter=_identity_name,
        )
        assert await lane.next() == "a"
        with pytest.raises(RuntimeError, match="generator crashed"):
            await lane.next()
        # Generator is dead, but we can still insert
        lane.append("recovery")
        assert await lane.next() == "recovery"

    @pytest.mark.asyncio
    async def test_double_close_is_safe(self) -> None:
        lane = _make_lane("a")
        await lane.close()
        await lane.close()  # should not raise


# ---------------------------------------------------------------------------
# Exhausted property
# ---------------------------------------------------------------------------

class TestExhausted:

    @pytest.mark.asyncio
    async def test_not_exhausted_initially(self) -> None:
        lane = _make_lane("a")
        assert lane.exhausted is False

    @pytest.mark.asyncio
    async def test_exhausted_after_draining(self) -> None:
        lane = _make_lane("a")
        await lane.next()
        # Need to attempt one more to discover exhaustion
        try:
            await lane.next()
        except StopAsyncIteration:
            pass
        assert lane.exhausted is True

    @pytest.mark.asyncio
    async def test_exhausted_after_close(self) -> None:
        lane = _make_lane("a", "b")
        await lane.close()
        assert lane.exhausted is True

    @pytest.mark.asyncio
    async def test_not_exhausted_when_insertions_remain(self) -> None:
        """Even if generator is exhausted, lane is not 'done' if insertions exist."""
        lane = _make_lane()
        lane.append("inserted")
        # Generator is exhausted but there's work to do
        assert await lane.next() == "inserted"


# ---------------------------------------------------------------------------
# Anchor inserts: insert_before / insert_after
# ---------------------------------------------------------------------------


class TestAnchorBefore:

    @pytest.mark.asyncio
    async def test_insert_before_fires_just_before_anchor(self) -> None:
        """Peek sees the anchor item, then next returns the Before-insert first;
        the subsequent next returns the anchor item itself."""
        lane = _make_lane("alpha", "beta", "gamma")
        lane.insert_before("beta", "pre_beta")

        # First item: "alpha" (no anchor match).
        assert await lane.next() == "alpha"

        # Peek should return the anchor item itself (peek does NOT fire anchors).
        assert await lane.peek() == "beta"

        # Next call: the Before-insert fires first.
        assert await lane.next() == "pre_beta"

        # Then the anchor item.
        assert await lane.next() == "beta"
        assert await lane.next() == "gamma"

    @pytest.mark.asyncio
    async def test_multiple_insert_before_same_anchor_fifo(self) -> None:
        """FIFO within a single Before anchor: first-inserted fires first."""
        lane = _make_lane("target", "rest")
        lane.insert_before("target", "first")
        lane.insert_before("target", "second")

        assert await lane.next() == "first"
        assert await lane.next() == "second"
        assert await lane.next() == "target"
        assert await lane.next() == "rest"

    @pytest.mark.asyncio
    async def test_insert_before_first_match_wins(self) -> None:
        """If the anchor name appears twice, only the first occurrence fires
        the Before-insert. Later occurrences behave normally."""
        lane = _make_lane("target", "other", "target")
        lane.insert_before("target", "pre_target")

        # First "target" triggers the Before-insert.
        assert await lane.next() == "pre_target"
        assert await lane.next() == "target"
        # Second "target" comes through with no re-targeting.
        assert await lane.next() == "other"
        assert await lane.next() == "target"


class TestAnchorAfter:

    @pytest.mark.asyncio
    async def test_insert_after_fires_just_after_anchor(self) -> None:
        """After-anchor fires on the NEXT .next() call after the matching
        item was consumed."""
        lane = _make_lane("alpha", "beta", "gamma")
        lane.insert_after("beta", "post_beta")

        assert await lane.next() == "alpha"
        # Consuming "beta" records it as 'last consumed' with pending after.
        assert await lane.next() == "beta"
        # On the NEXT call, the after-insert fires first.
        assert await lane.next() == "post_beta"
        assert await lane.next() == "gamma"

    @pytest.mark.asyncio
    async def test_multiple_insert_after_same_anchor_fifo(self) -> None:
        """FIFO within a single After anchor."""
        lane = _make_lane("target", "tail")
        lane.insert_after("target", "first")
        lane.insert_after("target", "second")

        assert await lane.next() == "target"
        assert await lane.next() == "first"
        assert await lane.next() == "second"
        assert await lane.next() == "tail"

    @pytest.mark.asyncio
    async def test_insert_after_first_match_wins(self) -> None:
        """Only the first occurrence of the anchor name fires the After-insert."""
        lane = _make_lane("target", "other", "target")
        lane.insert_after("target", "post_target")

        assert await lane.next() == "target"
        assert await lane.next() == "post_target"
        assert await lane.next() == "other"
        assert await lane.next() == "target"  # second target: no after fires
        with pytest.raises(StopAsyncIteration):
            await lane.next()



class TestPeekReportsPendingAfterAnchors:
    """A peek that says "nothing left" must mean it.

    Consumers peek to decide whether the lane is finished. An After anchor on
    the LAST item is already committed to the stream, so reporting the lane
    empty ends consumption with the insert still queued -- and it is then
    reported as an anchor whose name never appeared, which is the opposite of
    what happened.
    """

    @pytest.mark.asyncio
    async def test_peek_sees_an_after_anchor_on_the_last_item(self) -> None:
        lane = _make_lane("a")
        lane.insert_after("a", "after_a")

        assert await lane.next() == "a"
        assert await lane.peek() == "after_a"
        assert await lane.next() == "after_a"
        assert await lane.peek() is None

    @pytest.mark.asyncio
    async def test_an_after_anchor_on_the_last_item_is_not_reported_as_dropped(
        self,
    ) -> None:
        lane = _make_lane("a")
        lane.insert_after("a", "after_a")

        consumed = [await lane.next()]
        while await lane.peek() is not None:
            consumed.append(await lane.next())

        assert consumed == ["a", "after_a"]
        assert lane.unresolved_anchor_inserts() == []

    @pytest.mark.asyncio
    async def test_peek_does_not_consume_the_after_anchor(self) -> None:
        lane = _make_lane("a", "b")
        lane.insert_after("a", "after_a")

        assert await lane.next() == "a"
        assert await lane.peek() == "after_a"
        assert await lane.peek() == "after_a"
        assert await lane.next() == "after_a"
        assert await lane.next() == "b"

    @pytest.mark.asyncio
    async def test_a_before_anchor_is_still_not_reported_by_peek(self) -> None:
        """Before fires only once next() commits to the item it precedes, so
        peek reports the raw item, as it always has."""
        lane = _make_lane("a", "b")
        lane.insert_before("b", "before_b")

        assert await lane.next() == "a"
        assert await lane.peek() == "b"
        assert await lane.next() == "before_b"

    @pytest.mark.asyncio
    async def test_an_after_anchor_added_once_its_anchor_has_run_still_fires(self) -> None:
        """The operator flow: paused on the action that just failed, inserting
        after it. Requiring the anchor to be registered before its item was
        consumed made that insert unreachable."""
        lane = _make_lane("a")

        assert await lane.next() == "a"
        lane.insert_after("a", "after_a")

        assert await lane.peek() == "after_a"
        assert await lane.next() == "after_a"
        assert lane.unresolved_anchor_inserts() == []

    @pytest.mark.asyncio
    async def test_an_after_anchor_for_an_older_item_does_not_fire(self) -> None:
        """Only the last consumed item is anchorable. Two items back is past."""
        lane = _make_lane("a", "b")

        assert await lane.next() == "a"
        assert await lane.next() == "b"
        lane.insert_after("a", "after_a")

        assert await lane.peek() is None
        assert [d.anchor_name for d in lane.unresolved_anchor_inserts()] == ["a"]

    @pytest.mark.asyncio
    async def test_an_after_anchor_outranks_a_queued_head_insertion(self) -> None:
        lane = _make_lane("a", "b")
        lane.insert_after("a", "after_a")

        assert await lane.next() == "a"
        lane.insert_next("at_head")

        assert await lane.peek() == "after_a"
        assert await lane.next() == "after_a"
        assert await lane.next() == "at_head"

    @pytest.mark.asyncio
    async def test_several_after_anchors_on_one_name_keep_their_order(self) -> None:
        lane = _make_lane("a")
        lane.insert_after("a", "first")
        lane.insert_after("a", "second")

        assert await lane.next() == "a"
        assert await lane.peek() == "first"
        assert await lane.next() == "first"
        assert await lane.peek() == "second"
        assert await lane.next() == "second"
        assert await lane.peek() is None

    @pytest.mark.asyncio
    async def test_a_second_insert_can_anchor_to_the_first(self) -> None:
        """The operator inserts, that runs, it fails, they insert after IT. The
        flow this whole class exists for, one step further along."""
        lane = _make_lane("a")

        assert await lane.next() == "a"
        lane.insert_after("a", "after_a")
        assert await lane.next() == "after_a"

        lane.insert_after("after_a", "after_that")
        assert await lane.peek() == "after_that"
        assert await lane.next() == "after_that"
        assert lane.unresolved_anchor_inserts() == []

    @pytest.mark.asyncio
    async def test_a_chain_of_after_anchors_runs_in_order(self) -> None:
        lane = _make_lane("a")
        lane.insert_after("a", "x")
        lane.insert_after("x", "y")

        assert await lane.next() == "a"
        assert await lane.next() == "x"
        assert await lane.next() == "y"
        assert lane.unresolved_anchor_inserts() == []

    @pytest.mark.asyncio
    async def test_a_chain_off_the_first_of_a_group_runs_before_its_sibling(
        self,
    ) -> None:
        """An operator inserts two steps after one anchor, then a third after
        the first of those. All three run, and the third runs where they asked
        for it: right after the step it names, not after the whole group.
        """
        lane = _make_lane("a")
        lane.insert_after("a", "first")
        lane.insert_after("a", "second")
        lane.insert_after("first", "chained_off_first")

        consumed = []
        for _ in range(4):
            consumed.append(await lane.next())

        assert consumed == ["a", "first", "chained_off_first", "second"]
        assert lane.unresolved_anchor_inserts() == []

    @pytest.mark.asyncio
    async def test_a_chain_two_deep_off_the_first_of_a_group_still_resumes_it(
        self,
    ) -> None:
        """The group underneath is not forgotten however deep the chain goes."""
        lane = _make_lane("a")
        lane.insert_after("a", "first")
        lane.insert_after("a", "second")
        lane.insert_after("first", "x")
        lane.insert_after("x", "y")

        consumed = []
        for _ in range(5):
            consumed.append(await lane.next())

        assert consumed == ["a", "first", "x", "y", "second"]
        assert lane.unresolved_anchor_inserts() == []

    @pytest.mark.asyncio
    async def test_peek_sees_past_a_spent_chain_to_the_group_beneath(self) -> None:
        """peek() must walk every pending name, not just the innermost, or a
        consumer stops on an empty lane with the group still queued. Here the
        chain is finished and what remains is two names down."""
        lane = _make_lane("a")
        lane.insert_after("a", "first")
        lane.insert_after("a", "second")
        lane.insert_after("first", "chained_off_first")

        assert await lane.next() == "a"
        assert await lane.next() == "first"
        assert await lane.next() == "chained_off_first"

        assert await lane.peek() == "second"

    @pytest.mark.asyncio
    async def test_a_spent_anchor_stops_being_anchorable(self) -> None:
        """Only the step the lane is on is anchorable, and running an insert
        moves it on. An anchor whose inserts have all run is past, so a later
        insert naming it waits for the name to come round again rather than
        firing here."""
        lane = _make_lane("a")
        lane.insert_after("a", "x")

        assert await lane.next() == "a"
        assert await lane.next() == "x"
        lane.insert_after("a", "too_late")

        assert await lane.peek() is None
