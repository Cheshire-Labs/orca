"""Pending-drain takeover at acquisition time.

Pins the drain-gated release mechanics that fixed the batch livelock:
a departed action's mutex hold carries a ``pending_drain_check``; the
manager evaluates it when someone asks and takes the hold over when it
passes. Also pins two safety properties: a released reservation's
callback is neutered (a stale caller cannot delete a successor's
entry), and the requester's own labware never blocks its own takeover
(staying-for-next-action handoff).
"""
import pytest

from orca.resource_models.labware import LabwareInstance
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.system.reservation_manager.reservation_manager import (
    LocationReservationManager,
)
from tests.test_cross_tick_deadlock import _make_location, _make_location_registry


def _reservation(location, labware: LabwareInstance | None = None) -> LocationReservation:
    return LocationReservation(location, labware)


class TestDrainTakeover:

    @pytest.mark.asyncio
    async def test_takeover_waits_for_predicate_then_grants(self) -> None:
        """A pending-drain hold rejects while occupants remain and is taken
        over at the next attempt after the predicate flips true -- the flip
        needs no event, the successor's own attempt re-evaluates it."""
        loc = _make_location("dev_a")
        manager = LocationReservationManager(_make_location_registry({"dev_a": loc}))
        holder = _reservation(loc)
        await manager.attempt_reservation("dev_a", holder, thread_id="t1")
        assert holder.granted.is_set()

        state = {"drained": False}
        holder.mark_pending_drain(lambda _exclude: state["drained"])

        blocked = _reservation(loc)
        await manager.attempt_reservation("dev_a", blocked, thread_id="t2")
        assert blocked.rejected.is_set()

        state["drained"] = True
        winner = _reservation(loc)
        await manager.attempt_reservation("dev_a", winner, thread_id="t2")
        assert winner.granted.is_set()
        assert manager.get_reservation_at("dev_a") is winner

    @pytest.mark.asyncio
    async def test_requester_labware_never_blocks_its_own_takeover(self) -> None:
        """A plate staying on the device to own the successor action must
        not deadlock the drain: the predicate receives the requester's labware
        id and the requester is granted while a foreign requester is not."""
        loc = _make_location("dev_a")
        manager = LocationReservationManager(_make_location_registry({"dev_a": loc}))
        stayer = LabwareInstance("stayer_plate", "plate")
        foreign = LabwareInstance("foreign_plate", "plate")

        holder = _reservation(loc)
        await manager.attempt_reservation("dev_a", holder, thread_id="t1")
        # Drained iff the only remaining occupant (the stayer) is the requester.
        holder.mark_pending_drain(lambda exclude: exclude == stayer.id)

        rejected = _reservation(loc, foreign)
        await manager.attempt_reservation("dev_a", rejected, thread_id="t2")
        assert rejected.rejected.is_set()

        granted = _reservation(loc, stayer)
        await manager.attempt_reservation("dev_a", granted, thread_id="t3")
        assert granted.granted.is_set()

    @pytest.mark.asyncio
    async def test_released_reservation_callback_is_neutered(self) -> None:
        """A stale release call on an already-released reservation must not
        delete the successor's entry (abort sweep / operator cancel safety)."""
        loc = _make_location("dev_a")
        manager = LocationReservationManager(_make_location_registry({"dev_a": loc}))
        holder = _reservation(loc)
        await manager.attempt_reservation("dev_a", holder, thread_id="t1")
        manager.release_reservation("dev_a")

        successor = _reservation(loc)
        await manager.attempt_reservation("dev_a", successor, thread_id="t2")
        assert successor.granted.is_set()

        holder.release_reservation()
        assert manager.get_reservation_at("dev_a") is successor

    @pytest.mark.asyncio
    async def test_displaced_reservation_release_is_inert(self) -> None:
        """Re-entrant re-grant displaces the old reservation and neuters its
        callback; a stale release on the displaced object must not free the
        live hold (pins the displacement contract that rule 7 rides on)."""
        loc = _make_location("dev_a")
        manager = LocationReservationManager(_make_location_registry({"dev_a": loc}))
        first = _reservation(loc)
        await manager.attempt_reservation("dev_a", first, thread_id="t1")
        second = _reservation(loc)
        await manager.attempt_reservation("dev_a", second, thread_id="t1")
        assert second.granted.is_set()
        assert first.is_displaced

        first.release_reservation()
        assert manager.get_reservation_at("dev_a") is second


class TestResidencyCheck:
    """The drain-predicate residency classifier over live thread state."""

    @staticmethod
    def _build(template_flags: dict[str, bool] | None, status: str | None):
        from unittest.mock import Mock

        from orca.workflow_models.labware_threads.residency import (
            build_residency_check,
        )
        from orca.workflow_models.status_manager import StatusManager

        thread = Mock()
        thread.id = "thread-1"
        if template_flags is None:
            thread.thread_template = None
        else:
            template = Mock()
            # Every flag the classifier reads, set explicitly: an unset attribute
            # on a Mock is truthy, which would make each case resident by default.
            template.start_reuse_existing = template_flags.get("reuse", False)
            template.immovable = template_flags.get("immovable", False)
            template.end_leave_in_place = template_flags.get("leave_in_place", False)
            thread.thread_template = template

        registry = Mock()
        registry.get_thread_by_labware = Mock(return_value=thread)

        status_manager = StatusManager(Mock())
        if status is not None:
            status_manager.set_status("THREAD", "thread-1", status, Mock())
        return build_residency_check(registry, status_manager)

    def test_reuse_existing_template_is_resident(self) -> None:
        check = self._build({"reuse": True}, status="EXECUTING_ACTION")
        assert check("lw-1") is True

    def test_immovable_template_is_resident(self) -> None:
        check = self._build({"immovable": True}, status="EXECUTING_ACTION")
        assert check("lw-1") is True

    def test_leave_in_place_end_is_resident(self) -> None:
        """The thread declared its labware is never coming off that site, so a
        deferred device release must not wait for it to depart."""
        check = self._build({"leave_in_place": True}, status="EXECUTING_ACTION")
        assert check("lw-1") is True

    def test_join_waiting_thread_is_resident(self) -> None:
        check = self._build({}, status="AWAITING_CO_THREADS")
        assert check("lw-1") is True

    def test_running_thread_is_not_resident(self) -> None:
        check = self._build({}, status="MOVING")
        assert check("lw-1") is False

    def test_unknown_labware_is_not_resident(self) -> None:
        from unittest.mock import Mock

        from orca.workflow_models.labware_threads.residency import (
            build_residency_check,
        )
        from orca.workflow_models.status_manager import StatusManager

        registry = Mock()
        registry.get_thread_by_labware = Mock(side_effect=KeyError("no thread"))
        check = build_residency_check(registry, StatusManager(Mock()))
        assert check("lw-unknown") is False

    def test_unknown_status_is_not_resident(self) -> None:
        check = self._build({}, status=None)
        assert check("lw-1") is False


class TestOwnedSiteOccupancyEdges:
    """Cross-acquisition swaps over device-owned SITES must cast blocker edges.

    A mutex position's own labware is always None in the flat model (plates
    sit on child sites), so without owned-site occupancy the detector prunes
    both waiters as escapable and the swap stalls silently: X's plate on
    dev_a's site while X requests dev_b, Y's plate on dev_b's site while Y
    requests dev_a, both previous holds pending-drain.
    """

    @pytest.mark.asyncio
    async def test_owned_site_swap_is_detected_and_flagged(self) -> None:
        from unittest.mock import Mock

        from orca.resource_models.deck_site_location import DeckSiteLocation
        from orca.system.reservation_manager.reservation_manager import (
            ThreadReservationCoordinator,
        )
        from tests.test_cross_tick_deadlock import (
            _make_collection,
            _make_labware,
            _make_mock_thread,
            _make_thread_registry,
            _place_labware,
        )

        plate_x = _make_labware("plate_x")
        plate_y = _make_labware("plate_y")

        dev_a = _make_location("dev_a")
        dev_b = _make_location("dev_b")
        owner_a = Mock()
        owner_a.name = "dev_a"
        owner_b = Mock()
        owner_b.name = "dev_b"
        site_a = DeckSiteLocation("dev_a/site", owner_a, mutex_position_id="dev_a")
        site_b = DeckSiteLocation("dev_b/site", owner_b, mutex_position_id="dev_b")
        _place_labware(site_a, plate_x)
        _place_labware(site_b, plate_y)
        src = _make_location("src")

        locations = {
            "dev_a": dev_a, "dev_b": dev_b,
            "dev_a/site": site_a, "dev_b/site": site_b, "src": src,
        }
        reg = Mock()
        reg.get_location = Mock(side_effect=lambda name: locations[name])
        reg.locations = list(locations.values())
        reg.sites_of = Mock(side_effect=lambda mutex_key: [
            location for location in locations.values()
            if location.owner_mutex_id == mutex_key
        ])
        thread_reg = _make_thread_registry({
            "thread_x": _make_mock_thread(plate_x),
            "thread_y": _make_mock_thread(plate_y),
        })
        coordinator = ThreadReservationCoordinator(reg, thread_reg)

        # Both mutexes held by departed threads whose drain never completes
        # (the other waiter's plate blocks it) -- the N=5 stall shape.
        holder_a = LocationReservation(dev_a)
        await coordinator._reservation_manager.attempt_reservation(
            "dev_a", holder_a, thread_id="departed_1"
        )
        holder_a.mark_pending_drain(lambda _exclude: False)
        holder_b = LocationReservation(dev_b)
        await coordinator._reservation_manager.attempt_reservation(
            "dev_b", holder_b, thread_id="departed_2"
        )
        holder_b.mark_pending_drain(lambda _exclude: False)

        col_x = _make_collection("thread_x", plate_x, site_a, dev_b)
        async with coordinator._lock:
            coordinator._queue.append(col_x)
        await coordinator._on_tick()
        assert col_x.rejected.is_set()

        col_y = _make_collection("thread_y", plate_y, site_b, dev_a)
        async with coordinator._lock:
            coordinator._queue.append(col_y)
        await coordinator._on_tick()
        assert col_y.rejected.is_set()

        flagged = coordinator._deadlock_detector._deadlocked_threads
        assert len(flagged) == 1 and flagged <= {"thread_x", "thread_y"}, (
            f"owned-site swap must flag one waiter; flagged={flagged}"
        )
