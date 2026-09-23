"""Unit tests for ``CoLabwareCoordinator`` deterministic priority.

Priority is asymmetric (see ``CoLabwareWaitOutcome`` docstring):
- Fast path (pre-set events): STOP > EXIT > CO_LABWARE. PAUSE deferred.
- Race (events fire mid-wait): STOP > PAUSE > EXIT > CO_LABWARE > TIMEOUT.

Priority is deterministic. It used to be probabilistic: ``asyncio.wait``
done-set order picked the outcome when two events fired together.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from orca.workflow_models.labware_threads.co_labware_coordinator import (
    CoLabwareCoordinator,
    CoLabwareWaitOutcome,
)


pytestmark = pytest.mark.asyncio


def _events() -> tuple[asyncio.Event, asyncio.Event, asyncio.Event, asyncio.Event]:
    return (asyncio.Event(), asyncio.Event(), asyncio.Event(), asyncio.Event())


class TestFastPathPriority:
    """Each of the 16 pre-set combinations resolves deterministically.

    Fast-path order: STOP > EXIT > CO_LABWARE. PAUSE is intentionally
    deferred when the wait would complete naturally on entry, so a
    paused thread does not hold its device reservation across the pause.
    """

    @pytest.mark.parametrize("stop,exit_,pause,co,expected", [
        # Single events.
        (False, False, False, False, CoLabwareWaitOutcome.TIMEOUT),
        (False, False, False, True,  CoLabwareWaitOutcome.CO_LABWARE_PRESENT),
        (False, False, True,  False, CoLabwareWaitOutcome.PAUSE_REQUESTED),
        (False, True,  False, False, CoLabwareWaitOutcome.METHOD_EXIT),
        (True,  False, False, False, CoLabwareWaitOutcome.STOP_REQUESTED),
        # PAUSE + CO: CO wins on fast path (pause deferred to post-action boundary).
        (False, False, True,  True,  CoLabwareWaitOutcome.CO_LABWARE_PRESENT),
        # EXIT + CO: EXIT wins.
        (False, True,  False, True,  CoLabwareWaitOutcome.METHOD_EXIT),
        # EXIT + PAUSE: EXIT pre-checked; pause-only goes into race.
        (False, True,  True,  False, CoLabwareWaitOutcome.METHOD_EXIT),
        # STOP combinations: STOP always wins on fast path.
        (True,  False, False, True,  CoLabwareWaitOutcome.STOP_REQUESTED),
        (True,  False, True,  False, CoLabwareWaitOutcome.STOP_REQUESTED),
        (True,  True,  False, False, CoLabwareWaitOutcome.STOP_REQUESTED),
        # Triples.
        (False, True,  True,  True,  CoLabwareWaitOutcome.METHOD_EXIT),
        (True,  False, True,  True,  CoLabwareWaitOutcome.STOP_REQUESTED),
        (True,  True,  False, True,  CoLabwareWaitOutcome.STOP_REQUESTED),
        (True,  True,  True,  False, CoLabwareWaitOutcome.STOP_REQUESTED),
        # All four.
        (True,  True,  True,  True,  CoLabwareWaitOutcome.STOP_REQUESTED),
    ])
    async def test_fast_path_combo(
        self,
        stop: bool, exit_: bool, pause: bool, co: bool,
        expected: CoLabwareWaitOutcome,
    ) -> None:
        stop_event, exit_event, pause_event, co_event = _events()
        if stop: stop_event.set()
        if exit_: exit_event.set()
        if pause: pause_event.set()
        if co: co_event.set()

        outcome = await CoLabwareCoordinator.wait(
            stop_event=stop_event,
            exit_event=exit_event,
            pause_event=pause_event,
            co_labware_event=co_event,
            timeout=0.05,
        )
        assert outcome is expected


class TestPostRacePriority:
    """When the wait blocks then multiple events fire mid-race, the
    done-set is resolved with priority STOP > PAUSE > EXIT > CO_LABWARE.
    PAUSE beats EXIT in the race (operator pause during a blocking wait
    is durable); on the fast path EXIT pre-empts because no race ran."""

    @pytest.mark.parametrize("setters,expected", [
        # Single setters.
        (("stop",), CoLabwareWaitOutcome.STOP_REQUESTED),
        (("exit",), CoLabwareWaitOutcome.METHOD_EXIT),
        (("pause",), CoLabwareWaitOutcome.PAUSE_REQUESTED),
        (("co",), CoLabwareWaitOutcome.CO_LABWARE_PRESENT),
        # Pairs.
        (("stop", "exit"), CoLabwareWaitOutcome.STOP_REQUESTED),
        (("stop", "pause"), CoLabwareWaitOutcome.STOP_REQUESTED),
        (("stop", "co"), CoLabwareWaitOutcome.STOP_REQUESTED),
        (("exit", "pause"), CoLabwareWaitOutcome.PAUSE_REQUESTED),
        (("exit", "co"), CoLabwareWaitOutcome.METHOD_EXIT),
        (("pause", "co"), CoLabwareWaitOutcome.PAUSE_REQUESTED),
        # Triples.
        (("stop", "exit", "pause"), CoLabwareWaitOutcome.STOP_REQUESTED),
        (("stop", "exit", "co"), CoLabwareWaitOutcome.STOP_REQUESTED),
        (("stop", "pause", "co"), CoLabwareWaitOutcome.STOP_REQUESTED),
        (("exit", "pause", "co"), CoLabwareWaitOutcome.PAUSE_REQUESTED),
        # All four.
        (("stop", "exit", "pause", "co"), CoLabwareWaitOutcome.STOP_REQUESTED),
    ])
    async def test_simultaneous_fire(
        self,
        setters: tuple[str, ...],
        expected: CoLabwareWaitOutcome,
    ) -> None:
        stop_event, exit_event, pause_event, co_event = _events()
        by_name = {
            "stop": stop_event, "exit": exit_event,
            "pause": pause_event, "co": co_event,
        }

        async def fire_after_delay() -> None:
            # One yield: this task is created before wait() is awaited, and
            # wait()'s pre-checks are synchronous, so it parks before this fires.
            await asyncio.sleep(0)
            for name in setters:
                by_name[name].set()

        setter = asyncio.create_task(fire_after_delay())
        try:
            outcome = await CoLabwareCoordinator.wait(
                stop_event=stop_event,
                exit_event=exit_event,
                pause_event=pause_event,
                co_labware_event=co_event,
                timeout=1.0,
            )
            assert outcome is expected
        finally:
            await setter


class TestTimeout:

    async def test_timeout_when_nothing_fires(self) -> None:
        stop_event, exit_event, pause_event, co_event = _events()
        outcome = await CoLabwareCoordinator.wait(
            stop_event=stop_event,
            exit_event=exit_event,
            pause_event=pause_event,
            co_labware_event=co_event,
            timeout=0.05,
        )
        assert outcome is CoLabwareWaitOutcome.TIMEOUT

    async def test_none_timeout_waits_unbounded_for_co_labware(self) -> None:
        """timeout=None (the production default) makes the wait unbounded: a
        co-labware arrival resolves as CO_LABWARE_PRESENT with no timeout in
        play, never TIMEOUT. Guards the fix for the wall-clock cap that
        spuriously failed healthy slow convergence under load."""
        stop_event, exit_event, pause_event, co_event = _events()

        async def fire_co() -> None:
            # One yield: task created before wait() is awaited, wait()'s
            # pre-checks are synchronous, so it parks before this fires.
            await asyncio.sleep(0)
            co_event.set()

        setter = asyncio.create_task(fire_co())
        try:
            outcome = await CoLabwareCoordinator.wait(
                stop_event=stop_event,
                exit_event=exit_event,
                pause_event=pause_event,
                co_labware_event=co_event,
                timeout=None,
            )
            assert outcome is CoLabwareWaitOutcome.CO_LABWARE_PRESENT
        finally:
            await setter

    async def test_none_timeout_wait_broken_by_stop(self) -> None:
        """Recovery contract for an unbounded (timeout=None) co-labware wait:
        an operator stop breaks it. With no co-labware arrival the wait would
        otherwise never return, so STOP_REQUESTED is the documented escape."""
        stop_event, exit_event, pause_event, co_event = _events()

        async def fire_stop() -> None:
            # One yield so wait() parks before stop is set.
            await asyncio.sleep(0)
            stop_event.set()

        setter = asyncio.create_task(fire_stop())
        try:
            outcome = await CoLabwareCoordinator.wait(
                stop_event=stop_event,
                exit_event=exit_event,
                pause_event=pause_event,
                co_labware_event=co_event,
                timeout=None,
            )
            assert outcome is CoLabwareWaitOutcome.STOP_REQUESTED
        finally:
            await setter

    async def test_default_config_co_labware_timeout_is_unbounded(self) -> None:
        """The co-labware wait defaults to unbounded, matching every sibling
        reservation/coordination timeout. Genuine cycles are caught by the
        reservation-layer deadlock detector, not this wall-clock cap."""
        from orca.config import CoordinationConfig

        assert CoordinationConfig().co_labware_timeout is None


class TestNoOrphanTasks:
    """Cancelled racing tasks are awaited so they don't survive on the loop."""

    async def test_tasks_cancelled_after_winning_outcome(self) -> None:
        stop_event, exit_event, pause_event, co_event = _events()

        async def fire_co_after_delay() -> None:
            await asyncio.sleep(0)
            co_event.set()

        setter = asyncio.create_task(fire_co_after_delay())
        try:
            outcome = await CoLabwareCoordinator.wait(
                stop_event=stop_event,
                exit_event=exit_event,
                pause_event=pause_event,
                co_labware_event=co_event,
                timeout=1.0,
            )
            assert outcome is CoLabwareWaitOutcome.CO_LABWARE_PRESENT
        finally:
            await setter

        await asyncio.sleep(0)
        leaked = [t for t in asyncio.all_tasks() if not t.done() and t is not asyncio.current_task()]
        assert leaked == [], f"Leaked tasks: {leaked}"


class TestEmitTipEvents:
    """``emit_tip_events`` reads the ledger and fires TIP_RACK.LOW / EMPTY."""

    async def test_no_tip_racks_no_emit(self) -> None:
        from orca.workflow_models.actions.executable_location_action import (
            ExecutableLocationAction,
        )
        action = MagicMock(spec=ExecutableLocationAction)
        action.action = MagicMock()
        action.action.expected_inputs = []

        bus = MagicMock()
        ctx = MagicMock()

        await CoLabwareCoordinator.emit_tip_events(action, ctx, bus)
        bus.emit.assert_not_called()

    @pytest.mark.parametrize("num_tips,present_count,expected_event", [
        (0, 0, None),       # num_tips == 0 -> early continue, no event
        (96, 96, None),     # full rack -> used_fraction == 0, no event
        (96, 24, None),     # used_fraction == 0.75 exactly -> NOT > 0.75, no event
        (96, 23, "TIP_RACK.LOW"),    # used_fraction > 0.75 -> LOW
        (96, 0, "TIP_RACK.EMPTY"),   # present_count == 0 -> EMPTY (precedence over LOW)
    ])
    async def test_emits_for_tip_rack_state(
        self, num_tips: int, present_count: int, expected_event: str | None,
    ) -> None:
        from unittest.mock import patch
        from orca.resource_models.labware import TipRackInstance
        from orca.workflow_models.actions.executable_location_action import (
            ExecutableLocationAction,
        )

        tip_rack = MagicMock(spec=TipRackInstance)
        tip_rack.tip_rack = MagicMock()
        tip_rack.tip_rack.num_tips = num_tips
        tip_rack.name = "tips_1"
        tip_rack.ops = AsyncMock(return_value=[])

        action = MagicMock(spec=ExecutableLocationAction)
        action.action = MagicMock()
        action.action.expected_inputs = [tip_rack]

        bus = MagicMock()
        ctx = MagicMock()

        with patch(
            "orca.state.projections.has_tip_baseline",
            return_value=True,
        ), patch(
            "orca.state.projections.tips_present",
            return_value=[object()] * present_count,
        ):
            await CoLabwareCoordinator.emit_tip_events(action, ctx, bus)

        if expected_event is None:
            bus.emit.assert_not_called()
        else:
            bus.emit.assert_called_once_with(expected_event, ctx)

    async def test_rack_with_no_seed_in_reach_emits_nothing(self) -> None:
        """A rack restored from a store folds to zero tips because its seed sits
        in an execution bucket nothing has bound. Announcing EMPTY there would
        retire a rack nobody has touched."""
        from orca.resource_models.labware import TipRackInstance
        from orca.workflow_models.actions.executable_location_action import (
            ExecutableLocationAction,
        )

        tip_rack = MagicMock(spec=TipRackInstance)
        tip_rack.tip_rack = MagicMock()
        tip_rack.tip_rack.num_tips = 96
        tip_rack.name = "tips_1"
        tip_rack.ops = AsyncMock(return_value=[])

        action = MagicMock(spec=ExecutableLocationAction)
        action.action = MagicMock()
        action.action.expected_inputs = [tip_rack]

        bus = MagicMock()

        await CoLabwareCoordinator.emit_tip_events(action, MagicMock(), bus)

        bus.emit.assert_not_called()

    async def test_non_tiprack_input_filtered(self) -> None:
        from orca.workflow_models.actions.executable_location_action import (
            ExecutableLocationAction,
        )

        plate = MagicMock()  # NOT spec'd as TipRackInstance
        action = MagicMock(spec=ExecutableLocationAction)
        action.action = MagicMock()
        action.action.expected_inputs = [plate]

        bus = MagicMock()
        ctx = MagicMock()

        await CoLabwareCoordinator.emit_tip_events(action, ctx, bus)
        bus.emit.assert_not_called()
