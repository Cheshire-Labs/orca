"""ThreadSnapshot exposes `waiting_for` for every kind of wait.

Diagnostic field naming the specific subject of a thread's wait: target
location for move waits, missing-labware names for co-thread waits, method
name for action-resolution.

A device or transporter lock wait is the one kind with no status of its own,
so it is asked before the status and covered at the bottom of this file.
"""

from unittest.mock import MagicMock

import pytest

from orca.runtime.status_builders import derive_waiting_for
from orca.workflow_models.labware_threads.executing_labware_thread import (
    ExecutingLabwareThread,
)
from orca.workflow_models.status_enums import LabwareThreadStatus


def _thread(status: LabwareThreadStatus) -> MagicMock:
    """Build a thread mock with only `status` pre-set.

    Top-level attributes are bound to ``ExecutingLabwareThread`` via ``spec=``
    so a typo at the helper's first attribute access (e.g.
    ``thread.assigned_actoin``) fails the test instead of auto-vivifying.
    Nested chains (``assigned_action.action.location``) still auto-vivify;
    individual tests must opt in by configuring them or setting them to
    ``None`` explicitly.
    """
    thread = MagicMock(spec=ExecutingLabwareThread)
    thread.status = status
    # Consulted before status, so a mock left to auto-vivify would answer
    # every case with a lock wait that is not there.
    thread.blocked_on_lock = None
    return thread


def _location(name: str) -> MagicMock:
    loc = MagicMock()
    loc.name = name
    return loc


def test_returns_none_for_non_awaiting_states() -> None:
    for status in (
        LabwareThreadStatus.CREATED,
        LabwareThreadStatus.EXECUTING_ACTION,
        LabwareThreadStatus.MOVING,
        LabwareThreadStatus.COMPLETED,
        LabwareThreadStatus.PAUSED,
        LabwareThreadStatus.ABORTED,
    ):
        assert derive_waiting_for(_thread(status)) is None


def test_awaiting_move_target_availability_returns_move_target_name() -> None:
    thread = _thread(LabwareThreadStatus.AWAITING_MOVE_TARGET_AVAILABILITY)
    thread.move_action.target = _location("shaker_pad_2")
    assert derive_waiting_for(thread) == "shaker_pad_2"


def test_awaiting_move_reservation_prefers_resolved_move_target() -> None:
    thread = _thread(LabwareThreadStatus.AWAITING_MOVE_RESERVATION)
    thread.move_action.target = _location("centrifuge_slot_1")
    thread.assigned_action.action.location = _location("ignored")
    assert derive_waiting_for(thread) == "centrifuge_slot_1"


def test_awaiting_move_reservation_falls_back_to_assigned_action_location() -> None:
    thread = _thread(LabwareThreadStatus.AWAITING_MOVE_RESERVATION)
    thread.move_action = None
    thread.assigned_action.action.location = _location("liquid_handler_1")
    assert derive_waiting_for(thread) == "liquid_handler_1"


def test_awaiting_move_reservation_falls_back_to_end_location_when_method_done() -> None:
    thread = _thread(LabwareThreadStatus.AWAITING_MOVE_RESERVATION)
    thread.move_action = None
    thread.assigned_method = None
    thread.assigned_action = None
    thread.end_locations = [_location("stacker_out")]
    assert derive_waiting_for(thread) == "stacker_out"


def test_awaiting_move_reservation_at_end_of_thread_ignores_stale_assigned_action() -> None:
    """If a future dispatch-loop refactor stops nulling ``_assigned_action`` at
    end-of-method, the helper must still surface ``end_location.name`` for
    the end-of-thread move. This pins the invariant the helper depends on
    (``assigned_method is None`` => end-of-thread) regardless of whether
    ``_assigned_action`` was cleared.
    """
    thread = _thread(LabwareThreadStatus.AWAITING_MOVE_RESERVATION)
    thread.move_action = None
    thread.assigned_method = None
    # Realistic worst-case if the dispatch loop ever stops clearing
    # ``_assigned_action`` between methods: a stale last-action reference.
    thread.assigned_action.action.location = _location("liquid_handler_1")
    thread.end_locations = [_location("stacker_out")]
    assert derive_waiting_for(thread) == "stacker_out"


def test_awaiting_move_reservation_returns_none_when_no_subject_reachable() -> None:
    thread = _thread(LabwareThreadStatus.AWAITING_MOVE_RESERVATION)
    thread.move_action = None
    thread.assigned_action = None
    # assigned_method left as MagicMock (not None) -> not end-of-thread path
    assert derive_waiting_for(thread) is None


def test_awaiting_co_threads_joins_missing_labware_names() -> None:
    thread = _thread(LabwareThreadStatus.AWAITING_CO_THREADS)
    thread.assigned_action.missing_input_report.return_value = [
        "sample_plate_2",
        "reagent_plate_3",
    ]
    assert derive_waiting_for(thread) == "sample_plate_2, reagent_plate_3"


def test_awaiting_co_threads_names_a_slot_no_thread_ever_filled() -> None:
    """The wait that used to report nothing.

    A slot no thread assigned has no labware to name, so the old
    peek-what-is-missing read came back empty and the diagnostic printed a bare
    '?'. That is exactly the shape a mutation leaves behind when it wires only
    one thread of a shared action.
    """
    thread = _thread(LabwareThreadStatus.AWAITING_CO_THREADS)
    thread.assigned_action.missing_input_report.return_value = ["r5_tips"]
    assert derive_waiting_for(thread) == "r5_tips"


def test_awaiting_co_threads_returns_none_when_no_assigned_action() -> None:
    thread = _thread(LabwareThreadStatus.AWAITING_CO_THREADS)
    thread.assigned_action = None
    assert derive_waiting_for(thread) is None


def test_awaiting_co_threads_returns_none_when_nothing_missing() -> None:
    thread = _thread(LabwareThreadStatus.AWAITING_CO_THREADS)
    thread.assigned_action.missing_input_report.return_value = []
    assert derive_waiting_for(thread) is None


def test_awaiting_co_threads_does_not_call_mutating_accessor() -> None:
    thread = _thread(LabwareThreadStatus.AWAITING_CO_THREADS)
    thread.assigned_action.missing_input_report.return_value = []
    derive_waiting_for(thread)
    thread.assigned_action.refresh_labware_presence.assert_not_called()


def test_resolving_action_location_returns_assigned_method_name() -> None:
    thread = _thread(LabwareThreadStatus.RESOLVING_ACTION_LOCATION)
    thread.assigned_method.name = "transfer_to_pcr_plate"
    assert derive_waiting_for(thread) == "transfer_to_pcr_plate"


def test_resolving_action_location_returns_none_when_method_unset() -> None:
    thread = _thread(LabwareThreadStatus.RESOLVING_ACTION_LOCATION)
    thread.assigned_method = None
    assert derive_waiting_for(thread) is None


@pytest.mark.parametrize(
    "status",
    [
        LabwareThreadStatus.AWAITING_MOVE_TARGET_AVAILABILITY,
        LabwareThreadStatus.AWAITING_MOVE_RESERVATION,
    ],
)
def test_move_waits_return_none_when_no_move_or_action_set(
    status: LabwareThreadStatus,
) -> None:
    thread = _thread(status)
    thread.move_action = None
    thread.assigned_action = None
    # assigned_method left as MagicMock (not None) -> not end-of-thread path
    assert derive_waiting_for(thread) is None

def test_awaiting_move_reservation_joins_end_candidates_like_its_siblings() -> None:
    """Candidate ends surface comma-joined, the same shape every other
    multi-subject ``waiting_for`` uses (missing co-labware, contended action
    candidates). An operator reading the field during an hours-long LIVE wait
    should not have to learn a second list syntax.
    """
    thread = _thread(LabwareThreadStatus.AWAITING_MOVE_RESERVATION)
    thread.move_action = None
    thread.assigned_method = None
    thread.end_locations = [
        _location("hotel_pad_1"),
        _location("hotel_pad_2"),
        _location("hotel_pad_3"),
    ]
    assert derive_waiting_for(thread) == "hotel_pad_1, hotel_pad_2, hotel_pad_3"


def test_a_lock_wait_is_reported_even_though_the_status_says_in_flight() -> None:
    """The status of a thread queued for a lock is whatever it was already
    doing, so without this branch a stuck arm reports nothing at all."""
    for status in (LabwareThreadStatus.MOVING, LabwareThreadStatus.EXECUTING_ACTION):
        thread = _thread(status)
        thread.blocked_on_lock = "flex_1 device lock held by aspirate"
        assert derive_waiting_for(thread) == "flex_1 device lock held by aspirate"
