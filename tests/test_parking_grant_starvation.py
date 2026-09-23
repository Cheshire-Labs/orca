"""Starvation-score reset semantics: reset means REAL progress, not a grant.

Two livelocks pinned here, both observed on the N=6 SMC batch:

1. R1: the blanket reset-on-grant zeroed a parked thread's score on its PARK
   grant, re-crowning the same victim forever (63/70 flags on one thread).
2. The boomerang: with parks excluded, the victim's RETURN hop (re-entering
   the spot it had just vacated -- granted trivially) still reset the score,
   pinning the one useless victim while every useful victim froze at >=1.

The durable rule: a granted MOVE collection never resets the score -- a hop
grant is not progress. Moves pay down their sacrifice debt at ARRIVAL
(``MoveHandler._mark_episode_escaped``, the same site that clears the pad
cooldown). ACTION acquisition grants still reset here: a granted device IS
the progress being waited on.
"""
from unittest.mock import Mock

import pytest

from orca.system.reservation_manager.deadlock_manager import (
    DeadlockStarvationRegistry,
    ThreadDeadlockDetector,
)
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.system.reservation_manager.move_handler import (
    MoveActionCollectionReservationRequest,
)
from orca.system.reservation_manager.reservation_manager import (
    ThreadReservationCoordinator,
)
from orca.system.system_map import SystemMap
from orca.workflow_models.actions.util import LocationCollectionReservationRequest
from tests.test_cross_tick_deadlock import (
    DeadlockScenario,
    _make_location,
    _make_move_action,
)


class TestGrantResetSemantics:

    def test_granted_move_collection_does_not_reset_starvation(self) -> None:
        """No move grant resets -- parking, boomerang return, or ordinary hop.

        Supersedes the R1 pin that only excluded ``is_parking`` grants: the
        boomerang return hop was an ordinary granted move and re-crowned the
        same victim every lap. Moves reset at arrival instead.
        """
        s = DeadlockScenario()
        starvation = DeadlockStarvationRegistry()
        detector = ThreadDeadlockDetector(
            s.thread_reg, starvation, reservation_at=lambda _position_id: None
        )
        starvation.increment_starvation_score("thread_a")
        starvation.increment_starvation_score("thread_a")

        move = _make_move_action(s.plate_a, s.src_a, s.loc_a)
        granted = MoveActionCollectionReservationRequest("thread_a", [move])
        granted.granted.set()
        detector.process_tick_results([granted])

        assert starvation.get_starvation_score("thread_a") == 2

    def test_granted_action_collection_resets_starvation(self) -> None:
        """A granted device acquisition IS the awaited progress: reset here."""
        s = DeadlockScenario()
        starvation = DeadlockStarvationRegistry()
        detector = ThreadDeadlockDetector(
            s.thread_reg, starvation, reservation_at=lambda _position_id: None
        )
        starvation.increment_starvation_score("thread_a")

        reference = _make_location("reference_pad")
        action_request = LocationCollectionReservationRequest(
            "thread_a",
            [LocationReservation(s.loc_a, s.plate_a)],
            Mock(spec=SystemMap),
            reference,
        )
        action_request.granted.set()
        detector.process_tick_results([action_request])

        assert starvation.get_starvation_score("thread_a") == 0


class TestVictimRotation:

    @pytest.mark.asyncio
    async def test_victim_rotates_after_park_grant(self) -> None:
        """After thread_a is flagged and its PARK is granted, the next
        detection of the same still-standing cycle selects thread_b.

        Pre-fix this failed: the park grant reset thread_a's score to 0,
        tying it with thread_b, and the lexicographic tie-break re-picked
        thread_a forever.
        """
        s = DeadlockScenario()
        coordinator = ThreadReservationCoordinator(s.location_reg, s.thread_reg)
        detector = coordinator._deadlock_detector

        # Ticks 1+2: a then b rejected on separate ticks -> cross-tick cycle
        # flags exactly one thread (both score 0, lex tie-break -> thread_a).
        col_a = s.collection_a()
        async with coordinator._lock:
            coordinator._queue.append(col_a)
        await coordinator._on_tick()
        col_b = s.collection_b()
        async with coordinator._lock:
            coordinator._queue.append(col_b)
        await coordinator._on_tick()
        assert detector._deadlocked_threads == {"thread_a"}

        # Tick 3: thread_a resubmits, consumes the flag (score 0 -> 1), parks;
        # its parking collection is granted on tick 4.
        col_a2 = s.collection_a()
        async with coordinator._lock:
            coordinator._queue.append(col_a2)
        await coordinator._on_tick()
        assert col_a2.deadlocked.is_set()

        parking = MoveActionCollectionReservationRequest(
            "thread_a",
            [_make_move_action(s.plate_a, s.src_a, s.src_b)],
        )
        parking.granted.set()
        detector.process_tick_results([parking])

        # Cycle still standing, thread_a rejected again: the next victim must
        # be thread_b (score 0), not thread_a (score 1, unreset by the park).
        col_a3 = s.collection_a()
        async with coordinator._lock:
            coordinator._queue.append(col_a3)
        await coordinator._on_tick()
        assert detector._deadlocked_threads == {"thread_b"}

        # thread_b's next submission consumes the flag and is deadlocked
        # (parks), completing the rotation a -> b.
        col_b2 = s.collection_b()
        async with coordinator._lock:
            coordinator._queue.append(col_b2)
        await coordinator._on_tick()
        assert col_b2.deadlocked.is_set()
        assert not col_a3.deadlocked.is_set()
