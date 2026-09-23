"""Regression tests for release-driven reservation waiters.

A rejected waiter used to sleep a blind ``retry_interval`` between attempts.
It now snapshots per-location release counts before attempting and parks in
``wait_for_location_release``, waking the instant one of ITS OWN contended
locations frees. The per-location count is the truth a waiter compares
against; the shared event is only a nudge, and ``timeout`` (retry_interval)
is the load-bearing safety cap that keeps the cross-tick deadlock detector
fed when a genuine deadlock produces no releases.

The resolver's deadlock branch deliberately KEEPS the fixed sleep: waking on
a release re-attempts before the blocking thread has moved and re-forms the
same cycle (sim livelock).
"""
import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from orca.config import ReservationConfig
from orca.resource_models.location import Location
from orca.system.reservation_manager.errors import AcquisitionYieldRequested
from orca.resource_models.plate_pad import PlatePad
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.system.reservation_manager.reservation_manager import (
    ThreadReservationCoordinator,
)
from orca.system.system_map import ILocationRegistry
from orca.system.thread_registry_interface import IThreadRegistry
from tests.test_action_reservation_status_surface import (
    _build_resolver_world,
    _drive_outcomes,
)


def _make_location(name: str) -> Location:
    return Location(name, PlatePad(name))


def _make_coordinator(locations: dict[str, Location]) -> ThreadReservationCoordinator:
    loc_reg = Mock(spec=ILocationRegistry)
    loc_reg.get_location = Mock(side_effect=lambda name: locations[name])
    loc_reg.locations = list(locations.values())
    thread_reg = Mock(spec=IThreadRegistry)
    thread_reg.get_thread = Mock(side_effect=lambda tid: None)
    thread_reg.threads = []
    return ThreadReservationCoordinator(loc_reg, thread_reg)


class TestWaitForLocationRelease:
    @pytest.mark.asyncio
    async def test_wakes_on_own_location_release(self) -> None:
        locs = {"pad1": _make_location("pad1")}
        coord = _make_coordinator(locs)
        coord._reservation_manager._reserve(
            "pad1", LocationReservation(locs["pad1"]), thread_id="holder",
        )

        snapshot = coord.release_snapshot(["pad1"])
        waiter = asyncio.create_task(
            coord.wait_for_location_release(snapshot, timeout=30.0)
        )
        await asyncio.sleep(0)
        assert not waiter.done(), "waiter must park while pad1 is held"

        coord._reservation_manager.release_reservation("pad1")

        await asyncio.wait_for(waiter, timeout=1.0)

    @pytest.mark.asyncio
    async def test_times_out_to_a_normal_return_without_release(self) -> None:
        """No release ever fires, so completing AT ALL is the timeout path;
        the pre-check pins that nothing wakes the waiter spuriously."""
        coord = _make_coordinator({})
        snapshot = coord.release_snapshot(["pad1"])
        parked = asyncio.create_task(
            coord.wait_for_location_release(snapshot, timeout=30.0)
        )
        for _ in range(10):
            await asyncio.sleep(0)
        assert not parked.done(), "no release: the waiter must stay parked"
        parked.cancel()
        try:
            await parked
        except asyncio.CancelledError:
            pass

        await coord.wait_for_location_release(snapshot, timeout=0.05)

    @pytest.mark.asyncio
    async def test_release_between_snapshot_and_wait_returns_immediately(self) -> None:
        """The generous inner timeout is never consumed: the pre-wait release
        is caught by the snapshot compare, so the outer failsafe proves the
        wait did not sleep through it."""
        locs = {"pad1": _make_location("pad1")}
        coord = _make_coordinator(locs)
        coord._reservation_manager._reserve(
            "pad1", LocationReservation(locs["pad1"]), thread_id="holder",
        )

        snapshot = coord.release_snapshot(["pad1"])
        coord._reservation_manager.release_reservation("pad1")

        await asyncio.wait_for(
            coord.wait_for_location_release(snapshot, timeout=30.0), timeout=2.0,
        )

    @pytest.mark.asyncio
    async def test_unrelated_release_does_not_end_the_wait(self) -> None:
        """The pad2 release wakes-and-reparks the waiter within the yielded
        event-loop turns; only pad1's own release ends the wait."""
        locs = {"pad1": _make_location("pad1"), "pad2": _make_location("pad2")}
        coord = _make_coordinator(locs)
        for pid in ("pad1", "pad2"):
            coord._reservation_manager._reserve(
                pid, LocationReservation(locs[pid]), thread_id="holder",
            )

        snapshot = coord.release_snapshot(["pad1"])
        waiter = asyncio.create_task(
            coord.wait_for_location_release(snapshot, timeout=30.0)
        )
        await asyncio.sleep(0)
        assert not waiter.done()

        coord._reservation_manager.release_reservation("pad2")
        for _ in range(10):
            await asyncio.sleep(0)
        assert not waiter.done(), (
            "an unrelated release must nudge the waiter back to sleep, not wake it"
        )

        coord._reservation_manager.release_reservation("pad1")
        await asyncio.wait_for(waiter, timeout=2.0)


class TestResolverRetryBranches:
    @pytest.mark.asyncio
    async def test_rejected_outcome_waits_on_release(self) -> None:
        cfg = ReservationConfig(retry_interval=0.01)
        resolver, system_map, coord, pad = _build_resolver_world(cfg)
        await _drive_outcomes(coord, ["rejected", "granted"])
        coord.release_snapshot = Mock(return_value={"pad_1": 0})
        coord.wait_for_location_release = AsyncMock()

        await resolver.resolve_action_location(
            thread_id="t1",
            reference_point=pad,
            thread_reservation_manager=coord,
            system_map=system_map,
            status_sink=None,
        )

        assert coord.wait_for_location_release.await_count == 1
        coord.release_snapshot.assert_called()

    @pytest.mark.asyncio
    async def test_deadlocked_outcome_exits_without_release_wait(self) -> None:
        """A deadlock verdict never parks on the release-waiter: the resolver
        raises the acquisition-yield signal instead of retrying (a wake-based
        re-attempt would re-form the same cycle; the thread must park)."""
        cfg = ReservationConfig(retry_interval=0.01)
        resolver, system_map, coord, pad = _build_resolver_world(cfg)
        await _drive_outcomes(coord, ["deadlocked", "granted"])
        coord.release_snapshot = Mock(return_value={"pad_1": 0})
        coord.wait_for_location_release = AsyncMock()

        with pytest.raises(AcquisitionYieldRequested):
            await resolver.resolve_action_location(
                thread_id="t1",
                reference_point=pad,
                thread_reservation_manager=coord,
                system_map=system_map,
                status_sink=None,
            )

        assert coord.wait_for_location_release.await_count == 0


class TestRetryIntervalConstraint:
    def test_zero_retry_interval_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ReservationConfig(retry_interval=0.0)
