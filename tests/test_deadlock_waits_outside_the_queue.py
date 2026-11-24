"""The deadlock detector sees two waits that are not queued reservation requests.

- A placement wait: a plate that is not on the deck yet waits for one location to empty
  (a dispense waiting for a stacker output). It is a one-candidate OR wait.
- A co-labware wait: a thread holds its action's device until every other input arrives.
  It needs ALL of them, so it is stuck as soon as one of them is stuck.

A wait on a thread that is still working is a slow wait, never a deadlock.
"""
from typing import Dict, List

from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.system.reservation_manager.deadlock_manager import (
    DeadlockStarvationRegistry,
    ThreadDeadlockDetector,
)
from orca.system.reservation_manager.location_reservation import LocationReservation
from tests.test_deadlock_knot import _Collection, _site, _ThreadRegistry

_HANDLER = "handler"
_OUTPUT = "stacker_output"


class _Bench:
    """A handler and a stacker output, with the reservations and hand-overs a test plants or reads."""

    def __init__(self) -> None:
        self.held: Dict[str, LocationReservation] = {}
        self.handed_over: Dict[str, str] = {}
        self.registry = _ThreadRegistry([])
        self.starvation = DeadlockStarvationRegistry()
        self.output = Location(_OUTPUT)
        self.detector = ThreadDeadlockDetector(
            self.registry, self.starvation, reservation_at=self.held.get,
            hand_over=self.handed_over.__setitem__, promised_to=self.handed_over.get,
        )

    def thread(self, thread_id: str) -> LabwareInstance:
        plate = LabwareInstance(f"{thread_id}-plate", "plate")
        self.registry.add_holder(thread_id, plate)
        return plate

    def holds(self, thread_id: str, position_id: str) -> None:
        reservation = LocationReservation(Location(position_id))
        reservation.thread_id = thread_id
        self.held[position_id] = reservation

    def waits_for_the_handler(self, thread_id: str) -> _Collection:
        collection = _Collection(thread_id, [LocationReservation(Location(_HANDLER))])
        collection.rejected.set()
        return collection

    def waits_to_be_placed_on_the_output(self, thread_id: str) -> None:
        self.detector.note_location_wait(thread_id, LocationReservation(self.output))

    def waits_for_co_labware(self, thread_id: str, *missing: LabwareInstance) -> None:
        self.detector.note_co_labware_wait(thread_id, lambda: {plate.id for plate in missing})


def _the_shared_stacker_cycle(bench: _Bench, later_samples: List[str]) -> _Collection:
    """sample-a holds the handler and waits for reagent-a. reagent-a waits for the output.
    sample-b sits in the output and waits for the handler. Later samples wait for the output too."""
    bench.thread("sample-a")
    reagent = bench.thread("reagent-a")
    sample_b = bench.thread("sample-b")
    bench.output.resource.initialize_labware(sample_b)
    bench.holds("sample-a", _HANDLER)
    bench.waits_for_co_labware("sample-a", reagent)
    bench.waits_to_be_placed_on_the_output("reagent-a")
    for later in later_samples:
        bench.thread(later)
        bench.waits_to_be_placed_on_the_output(later)
    return bench.waits_for_the_handler("sample-b")


def test_a_cycle_through_a_co_labware_wait_and_a_placement_wait_is_a_deadlock() -> None:
    bench = _Bench()
    request = _the_shared_stacker_cycle(bench, [])

    assert bench.detector.find_yielding_thread([request]) == "sample-b"


def test_the_plate_that_moves_is_the_one_in_the_way_however_many_wait_behind_it() -> None:
    bench = _Bench()
    request = _the_shared_stacker_cycle(bench, ["sample-c", "sample-d"])

    bench.detector.process_tick_results([request])

    assert request.deadlocked.is_set()


def test_the_freed_output_goes_to_the_waiter_in_the_cycle_not_to_the_others_waiting_for_it() -> None:
    bench = _Bench()
    request = _the_shared_stacker_cycle(bench, ["aaa-sample-c", "sample-d"])

    bench.detector.process_tick_results([request])

    assert bench.handed_over == {_OUTPUT: "reagent-a"}


def test_a_thread_refused_a_promised_location_waits_for_the_thread_it_is_promised_to() -> None:
    """sample-c is refused the empty output because it is promised to reagent-a, which is stuck."""
    bench = _Bench()
    bench.thread("sample-c")
    bench.thread("reagent-a")
    bench.handed_over[_OUTPUT] = "reagent-a"
    bench.holds("sample-c", "pad")
    waiting_on_the_pad = _Collection("reagent-a", [LocationReservation(Location("pad"))])
    request = _Collection("sample-c", [LocationReservation(bench.output)])

    assert bench.detector.find_yielding_thread([waiting_on_the_pad, request]) in {"sample-c", "reagent-a"}


def test_a_co_labware_wait_on_a_plate_still_on_its_way_is_not_a_deadlock() -> None:
    """The reagent is moving, so it is not a waiter: the sample in the output has a slow wait."""
    bench = _Bench()
    bench.thread("sample-a")
    reagent = bench.thread("reagent-a")
    bench.output.resource.initialize_labware(bench.thread("sample-b"))
    bench.holds("sample-a", _HANDLER)
    bench.waits_for_co_labware("sample-a", reagent)

    assert bench.detector.find_yielding_thread([bench.waits_for_the_handler("sample-b")]) is None


def test_a_placement_wait_behind_a_plate_whose_thread_is_working_is_not_a_deadlock() -> None:
    """sample-b sits in the output but is not waiting for anything, so reagent-a's wait is slow, not stuck."""
    bench = _Bench()
    bench.thread("sample-a")
    reagent = bench.thread("reagent-a")
    bench.output.resource.initialize_labware(bench.thread("sample-b"))
    bench.holds("sample-a", _HANDLER)
    bench.waits_for_co_labware("sample-a", reagent)
    bench.waits_to_be_placed_on_the_output("reagent-a")
    bench.holds("busy", "elsewhere")
    waits_on_a_working_thread = _Collection("sample-x", [LocationReservation(Location("elsewhere"))])
    bench.thread("sample-x")

    assert bench.detector.find_yielding_thread([waits_on_a_working_thread]) is None


def test_a_co_labware_wait_needs_every_plate_so_one_stuck_plate_makes_it_stuck() -> None:
    """sample-a waits for two reagents. One is on its way, the other is in the cycle: still a deadlock."""
    bench = _Bench()
    bench.thread("sample-a")
    stuck_reagent = bench.thread("reagent-a")
    moving_reagent = bench.thread("reagent-moving")
    bench.output.resource.initialize_labware(bench.thread("sample-b"))
    bench.holds("sample-a", _HANDLER)
    bench.waits_for_co_labware("sample-a", moving_reagent, stuck_reagent)
    bench.waits_to_be_placed_on_the_output("reagent-a")

    assert bench.detector.find_yielding_thread([bench.waits_for_the_handler("sample-b")]) == "sample-b"


def test_a_reservation_wait_needs_one_candidate_so_one_free_candidate_keeps_it_out_of_the_deadlock() -> None:
    """sample-b can also use a second handler nobody holds, so nothing is stuck."""
    bench = _Bench()
    request = _the_shared_stacker_cycle(bench, [])
    either_handler = _Collection("sample-b", [
        *request.get_reservations(), LocationReservation(Location("handler_2")),
    ])

    assert bench.detector.find_yielding_thread([either_handler]) is None


def test_a_wait_that_ended_is_no_longer_part_of_a_deadlock() -> None:
    bench = _Bench()
    request = _the_shared_stacker_cycle(bench, [])
    bench.detector.end_location_wait("reagent-a")

    assert bench.detector.find_yielding_thread([request]) is None

    bench.waits_to_be_placed_on_the_output("reagent-a")
    bench.detector.end_co_labware_wait("sample-a")

    assert bench.detector.find_yielding_thread([request]) is None


def test_a_stuck_thread_nobody_waits_for_is_never_the_one_asked_to_move() -> None:
    """sample-x queues for a pad sample-b holds. Moving sample-x frees nothing, so sample-b moves."""
    bench = _Bench()
    request = _the_shared_stacker_cycle(bench, [])
    bench.thread("aaa-sample-x")
    bench.holds("sample-b", "pad")
    onlooker = _Collection("aaa-sample-x", [LocationReservation(Location("pad"))])

    assert bench.detector.find_yielding_thread([onlooker, request]) == "sample-b"


def test_one_waiting_request_is_enough_to_find_the_deadlock_on_a_later_tick() -> None:
    """The co-labware wait starts after sample-b was last refused: the carried refusal still finds it."""
    bench = _Bench()
    bench.thread("sample-a")
    reagent = bench.thread("reagent-a")
    bench.output.resource.initialize_labware(bench.thread("sample-b"))
    bench.holds("sample-a", _HANDLER)
    bench.waits_to_be_placed_on_the_output("reagent-a")
    request = bench.waits_for_the_handler("sample-b")
    bench.detector.process_tick_results([request])
    assert not request.deadlocked.is_set()

    bench.waits_for_co_labware("sample-a", reagent)
    bench.detector.process_tick_results([])

    assert bench.detector.is_flagged_for_deadlock("sample-b")
    assert bench.handed_over == {_OUTPUT: "reagent-a"}


def test_a_plate_the_device_holder_waits_for_is_not_blocked_by_that_holder() -> None:
    """sample-a holds the handler for an action reagent-a is part of, so the gate lets reagent-a onto
    the handler's sites. reagent-a refused a busy site waits for that site's plate, not for sample-a."""
    bench = _Bench()
    bench.thread("sample-a")
    reagent = bench.thread("reagent-a")
    leaving = bench.thread("reagent-leaving")
    bench.holds("sample-a", _HANDLER)
    bench.held[_HANDLER].set_membership(lambda labware_id: labware_id == reagent.id)
    bench.waits_for_co_labware("sample-a", reagent)
    site = _site(f"{_HANDLER}/reagent_site", _HANDLER)
    site.resource.initialize_labware(leaving)
    request = _Collection("reagent-a", [LocationReservation(site, reagent)])

    assert bench.detector.find_yielding_thread([request]) is None


def test_a_plate_a_co_labware_wait_is_waiting_for_is_not_asked_to_move() -> None:
    """Two owners each hold a device and wait for a plate that queues for the other's device.
    Moving either plate aside brings it no closer, so nobody is flagged and the stall detector pauses the run."""
    bench = _Bench()
    bench.thread("owner-1")
    bench.thread("owner-2")
    plate_1 = bench.thread("plate-1")
    plate_2 = bench.thread("plate-2")
    bench.holds("owner-1", "device_1")
    bench.holds("owner-2", "device_2")
    bench.waits_for_co_labware("owner-1", plate_1)
    bench.waits_for_co_labware("owner-2", plate_2)
    queue = [
        _Collection("plate-1", [LocationReservation(Location("device_2"))]),
        _Collection("plate-2", [LocationReservation(Location("device_1"))]),
    ]

    assert bench.detector.find_yielding_thread(queue) is None


def test_a_thread_that_cannot_move_aside_is_not_asked_again_until_it_is_granted_something() -> None:
    """With no pad to go to, asking the same thread every tick never settles. It is left to wait,
    so the stall detector sees a steady state and pauses the run."""
    bench = _Bench()
    request = _the_shared_stacker_cycle(bench, [])
    bench.detector.note_cannot_yield("sample-b")

    assert bench.detector.find_yielding_thread([request]) is None

    granted = _Collection("sample-b", [])
    granted.granted.set()
    bench.detector.process_tick_results([granted])

    assert bench.detector.find_yielding_thread([request]) == "sample-b"


def test_a_stuck_plate_that_only_queues_behind_the_cycle_is_not_the_one_asked_to_move() -> None:
    """aaa-hotel sits on a hotel slot and queues for the handler, and sample-x queues for that slot.
    aaa-hotel is in sample-x's way, but nothing in the cycle waits for it, so sample-b moves."""
    bench = _Bench()
    request = _the_shared_stacker_cycle(bench, [])
    bench.thread("aaa-hotel")
    bench.thread("sample-x")
    bench.holds("aaa-hotel", "hotel_slot")
    queue = [
        bench.waits_for_the_handler("aaa-hotel"),
        _Collection("sample-x", [LocationReservation(Location("hotel_slot"))]),
        request,
    ]

    assert bench.detector.find_yielding_thread(queue) == "sample-b"
