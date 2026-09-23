"""Round 5 S1-A: per-cycle status emission from the reservation retry loop.

When ``ResourcePoolResolver.resolve_action_location`` parks on a
contested reservation, the thread that owns the resolver call MUST get
a ``notify_awaiting_reservation`` callback so the snapshot ticks with
the candidate location list. Pre-fix the resolver looped silently with
no status change, leaving the dashboard stuck in
``RESOLVING_ACTION_LOCATION`` with ``waiting_for=null``.

Round 5 S1 typed timeout: the same loop, with a configured
``action_reservation_timeout``, raises ``ActionReservationTimeoutError``
once the budget elapses, carrying the candidate locations and the last
outcome (rejected/deadlocked) so a hosted envelope can point at the
specific contention.
"""

import asyncio
from typing import List
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from orca.config import ReservationConfig
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.resource_pool import ResourcePool
from orca.system.reservation_manager.errors import (
    AcquisitionYieldRequested,
    ActionReservationTimeoutError,
)
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.workflow_models.actions.util import ResourcePoolResolver


class _RecordingSink:
    """Captures the sequence of (notify, clear) calls for assertion."""

    def __init__(self) -> None:
        self.notify_calls: list[list[str]] = []
        self.clear_calls = 0

    def notify_awaiting_reservation(self, candidate_position_ids: List[str]) -> None:
        self.notify_calls.append(list(candidate_position_ids))

    def clear_awaiting_reservation(self) -> None:
        self.clear_calls += 1


def _build_resolver_world(reservation_config: ReservationConfig):
    """One-PlatePad pool with the reservation coordinator stubbed.

    The coordinator's ``submit_reservation_request`` flips outcomes to
    drive the loop without spinning up the actual ThreadReservationCoordinator
    (which involves the ticker loop, the deadlock detector, and the
    location registry -- overkill for testing the resolver's side of
    the contract).
    """
    pad = Location("pad_1", PlatePad("pad_1"))
    pool = ResourcePool(name="pool", resources=[])
    pool._resources = []  # bypass strict validation if any

    # Inject the pad as a direct potential location.
    resolver = ResourcePoolResolver(pool, reservation_config)
    # Replace the potential-location helper to return our pad. The
    # signature mirrors the production method's two args (resource_locator,
    # requesting_labware) so the resolver can pass either positional
    # or keyword.
    resolver._get_potential_action_locations = (
        lambda resource_locator, requesting_labware=None: [LocationReservation(pad, requesting_labware)]
    )

    system_map = MagicMock()
    coord = MagicMock()
    # The resolver's rejected branch snapshots release counts and awaits the
    # release wait; a bare MagicMock is not awaitable, so stub both here.
    coord.release_snapshot = Mock(return_value={})
    coord.wait_for_location_release = AsyncMock()
    return resolver, system_map, coord, pad


async def _drive_outcomes(coord, outcomes):
    """Make submit_reservation_request set the next outcome on the request.

    Outcomes are a finite sequence followed by a repeating final value;
    the resolver retry loop may iterate more than ``len(outcomes)`` times
    on Windows where ``event_loop().time()`` granularity (~15ms) lets
    many cycles complete inside a single 0.05s timeout window.

    The collection's ``resolve_final_reservation`` derives ``granted``
    from the inner ``LocationReservation`` events, so the ``granted``
    path must mark the underlying reservation as well -- otherwise
    ``resolve_action_location`` reaches ``request.reserved_action_location``
    on the granted-shortcut and raises "No action location reserved".
    """
    seq = list(outcomes)
    cursor = [0]

    async def submit(thread_id, request):
        outcome = seq[cursor[0]] if cursor[0] < len(seq) else seq[-1]
        cursor[0] += 1
        if outcome == "granted":
            inner = request.get_reservations()[0]
            inner.granted.set()
            request.resolve_final_reservation()
        elif outcome == "rejected":
            request.rejected.set()
            request.processed.set()
        elif outcome == "deadlocked":
            request.deadlocked.set()
            request.processed.set()

    coord.submit_reservation_request.side_effect = submit


@pytest.mark.asyncio
async def test_sink_notified_with_candidates_on_first_rejection() -> None:
    """Sink sees the candidate list after the first reject before retry."""
    cfg = ReservationConfig(retry_interval=0.01)
    resolver, system_map, coord, pad = _build_resolver_world(cfg)
    sink = _RecordingSink()
    await _drive_outcomes(coord, ["rejected", "granted"])

    await resolver.resolve_action_location(
        thread_id="t1",
        reference_point=pad,
        thread_reservation_manager=coord,
        system_map=system_map,
        status_sink=sink,
    )

    assert sink.notify_calls == [["pad_1"]]
    assert sink.clear_calls == 1


@pytest.mark.asyncio
async def test_sink_cleared_on_grant_without_prior_wait() -> None:
    """When the first cycle grants, only the clear-on-exit fires.

    The retry loop never notifies because no rejection happened, but
    the try/finally still calls ``clear_awaiting_reservation`` so the
    snapshot field cannot leak from a previous reservation cycle.
    """
    cfg = ReservationConfig(retry_interval=0.01)
    resolver, system_map, coord, pad = _build_resolver_world(cfg)
    sink = _RecordingSink()
    await _drive_outcomes(coord, ["granted"])

    await resolver.resolve_action_location(
        thread_id="t1",
        reference_point=pad,
        thread_reservation_manager=coord,
        system_map=system_map,
        status_sink=sink,
    )

    assert sink.notify_calls == []
    assert sink.clear_calls == 1


@pytest.mark.asyncio
async def test_typed_timeout_fires_on_rejected_path() -> None:
    """Configured timeout raises ActionReservationTimeoutError, not RuntimeError."""
    cfg = ReservationConfig(retry_interval=0.01, action_reservation_timeout=0.05)
    resolver, system_map, coord, pad = _build_resolver_world(cfg)
    sink = _RecordingSink()
    await _drive_outcomes(coord, ["rejected"] * 100)

    with pytest.raises(ActionReservationTimeoutError) as exc:
        await resolver.resolve_action_location(
            thread_id="t1",
            reference_point=pad,
            thread_reservation_manager=coord,
            system_map=system_map,
            status_sink=sink,
        )

    assert exc.value.thread_id == "t1"
    assert exc.value.timeout_seconds == 0.05
    assert exc.value.candidate_locations == ["pad_1"]
    assert exc.value.last_outcome == "rejected"
    # Sink is still cleared on raise via the try/finally guard.
    assert sink.clear_calls == 1


@pytest.mark.asyncio
async def test_deadlocked_path_signals_acquisition_yield() -> None:
    """A deadlock verdict exits the resolver immediately as a yield signal.

    Supersedes the deadlocked-retry timeout pin: the resolver no longer
    retries a flagged acquisition at all (retrying cannot unwind a rule-7
    acquisition swap -- the flagged thread must physically park). The
    unbounded-wait concern the old pin guarded is gone with the loop.
    """
    cfg = ReservationConfig(retry_interval=0.01, action_reservation_timeout=0.05)
    resolver, system_map, coord, pad = _build_resolver_world(cfg)
    sink = _RecordingSink()
    await _drive_outcomes(coord, ["deadlocked"])

    with pytest.raises(AcquisitionYieldRequested):
        await resolver.resolve_action_location(
            thread_id="t1",
            reference_point=pad,
            thread_reservation_manager=coord,
            system_map=system_map,
            status_sink=sink,
        )


@pytest.mark.asyncio
async def test_default_timeout_none_does_not_raise() -> None:
    """Without ``action_reservation_timeout`` the loop waits indefinitely.

    Reservation contention can legitimately last hours on real hardware, so an
    indefinite wait is correct. The typed-timeout error class exists for opt-in
    test/debug use; the production default is ``None`` and the loop keeps
    cycling.
    """
    cfg = ReservationConfig(retry_interval=0.01)
    assert cfg.action_reservation_timeout is None  # baseline assumption
    resolver, system_map, coord, pad = _build_resolver_world(cfg)
    sink = _RecordingSink()
    # Three rejections then grant -- no timeout would have fired anyway,
    # the assertion is purely on the absence of ActionReservationTimeoutError.
    await _drive_outcomes(coord, ["rejected", "rejected", "rejected", "granted"])

    await resolver.resolve_action_location(
        thread_id="t1",
        reference_point=pad,
        thread_reservation_manager=coord,
        system_map=system_map,
        status_sink=sink,
    )

    assert len(sink.notify_calls) == 3  # one notify per rejected cycle
    assert sink.clear_calls == 1
