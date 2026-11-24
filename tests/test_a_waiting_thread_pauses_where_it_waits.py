"""A thread waiting for something pauses where it waits, because it moves nothing there."""

import asyncio
from tests.thread_builder import build_executing_thread
from collections.abc import Callable
from unittest.mock import MagicMock

import pytest

from orca.config import ReservationConfig
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.modes.run_modes import WorkflowRunMode
from orca.system.reservation_manager.errors import AcquisitionYieldRequested, MoveAbandonedError
from orca.system.reservation_manager.interfaces import IThreadReservationCoordinator
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.workflow_models.actions.util import LocationCollectionReservationRequest, ResourcePoolResolver
from orca.workflow_models.labware_threads.executing_labware_thread import (
    _ThreadStopSignal,
)
from orca.workflow_models.labware_threads.labware_thread import LabwareThreadInstance
from orca.workflow_models.labware_threads.thread_state_machine import ThreadEvent
from orca.workflow_models.status_enums import LabwareThreadStatus
from tests.test_helpers import let_the_loop_run


class _Thread:
    """An executing thread on mocks, recording the events it fires."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, move_handler: MagicMock | None = None) -> None:
        deck = Location("deck", resource=PlatePad("deck"))
        thread = LabwareThreadInstance(
            labware=LabwareInstance("plate_96", "96_well"),
            start_location=deck,
            end_locations=[deck],
            run_mode=WorkflowRunMode.CHECK_RUN,
        )
        location_service = MagicMock()
        location_service.get.return_value = deck
        status_manager = MagicMock()
        status_manager.get_status.return_value = LabwareThreadStatus.PAUSED.name
        self.executing = build_executing_thread(
            thread=thread,
            event_bus=MagicMock(),
            move_handler=move_handler or MagicMock(),
            status_manager=status_manager,
            actions_resolver=MagicMock(),
            context=MagicMock(),
            labware_location_service=location_service,
        )
        self.fired: list[ThreadEvent] = []
        monkeypatch.setattr(self.executing, "_fire", self.fired.append)
        monkeypatch.setattr(self.executing, "_plan_is_stale", lambda: False)

    @property
    def paused(self) -> bool:
        return ThreadEvent.PAUSE_REQUESTED in self.fired


async def test_a_contributor_waiting_for_its_owner_pauses_and_waits_again_on_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    t = _Thread(monkeypatch)
    bound = asyncio.Event()
    action = MagicMock()

    async def wait_for_current_action() -> MagicMock:
        await bound.wait()
        return action

    method = MagicMock()
    method.wait_for_current_action = wait_for_current_action
    t.executing._assigned_method = method
    waiting = asyncio.ensure_future(t.executing._wait_for_the_owner_to_bind())
    await let_the_loop_run()

    t.executing.request_pause()
    await let_the_loop_run()
    assert t.paused

    t.executing._resume_event.set()
    bound.set()
    assert await asyncio.wait_for(waiting, timeout=5.0) is action


async def test_a_move_waiting_for_its_target_gives_way_to_a_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    t = _Thread(monkeypatch)
    move = MagicMock()
    move.target.wait_until_available = asyncio.Event().wait
    t.executing._move_action = move
    waiting = asyncio.ensure_future(t.executing._await_move_target())
    await let_the_loop_run()

    t.executing.request_pause()

    assert await asyncio.wait_for(waiting, timeout=5.0) is False


async def test_a_deadlocked_move_waiting_for_a_parking_spot_gives_way_to_a_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handle_deadlock(
        thread_id: str, move_action: MagicMock, previous_location: Location | None,
        abandon_when: Callable[[], bool],
    ) -> MagicMock:
        while not abandon_when():
            await asyncio.sleep(0)
        raise MoveAbandonedError("withdrawn")

    move_handler = MagicMock()
    move_handler.handle_deadlock = handle_deadlock
    t = _Thread(monkeypatch, move_handler)
    t.executing._move_action = MagicMock()
    waiting = asyncio.ensure_future(t.executing._handle_deadlock())
    await let_the_loop_run()

    t.executing.request_pause()

    assert await asyncio.wait_for(waiting, timeout=5.0) is False


async def test_a_stop_while_paused_where_it_waits_ends_the_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    t = _Thread(monkeypatch)
    t.executing.request_pause()
    holding = asyncio.ensure_future(t.executing.hold_for_pause())
    await let_the_loop_run()

    t.executing._stop_event.set()

    with pytest.raises(_ThreadStopSignal):
        await asyncio.wait_for(holding, timeout=5.0)


async def test_a_stop_raised_inside_the_method_lane_is_a_stop_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    t = _Thread(monkeypatch)

    async def stopped() -> MagicMock:
        raise _ThreadStopSignal()

    async def error_pause(*args: Exception | str) -> None:
        raise AssertionError("a stop was handled as an error")

    lane = MagicMock()
    lane.next = stopped
    t.executing._method_lane = lane
    monkeypatch.setattr(t.executing, "_pause_for_error", error_pause)

    with pytest.raises(_ThreadStopSignal):
        await t.executing._run_method_loop()


def _coordinator() -> MagicMock:
    """Refuses the first two requests and calls the third deadlocked."""
    requests: list[LocationCollectionReservationRequest] = []

    async def submit_reservation_request(thread_id: str, request: LocationCollectionReservationRequest) -> None:
        requests.append(request)
        (request.deadlocked if len(requests) == 3 else request.rejected).set()
        request.processed.set()

    async def wait_for_location_release(snapshot: dict[str, int], timeout: float) -> None:
        del snapshot, timeout

    coordinator = MagicMock(spec=IThreadReservationCoordinator)
    coordinator.release_snapshot.return_value = {}
    coordinator.submit_reservation_request = submit_reservation_request
    coordinator.wait_for_location_release = wait_for_location_release
    return coordinator


class _PausingSink:
    """Asks for a pause at the first wait; the pause lasts far longer than the reservation timeout."""

    def __init__(self, clock: list[float]) -> None:
        self._clock = clock
        self.pause_requested = asyncio.Event()
        self.pause_requested.set()
        self.holds = 0

    def notify_awaiting_reservation(self, candidate_position_ids: list[str]) -> None:
        del candidate_position_ids

    def clear_awaiting_reservation(self) -> None:
        return None

    async def hold_for_pause(self) -> None:
        self.holds += 1
        self._clock[0] += 100.0
        self.pause_requested.clear()


async def test_time_spent_paused_does_not_count_toward_the_reservation_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [0.0]
    monkeypatch.setattr(asyncio.get_running_loop(), "time", lambda: clock[0])
    resolver = ResourcePoolResolver(MagicMock(), ReservationConfig(action_reservation_timeout=10.0))
    no_candidates: list[LocationReservation] = []
    monkeypatch.setattr(resolver, "_get_potential_action_locations", lambda *_: no_candidates)
    sink = _PausingSink(clock)

    with pytest.raises(AcquisitionYieldRequested):
        await resolver.resolve_action_location(
            "t1", MagicMock(), _coordinator(), MagicMock(), status_sink=sink,
        )
    assert sink.holds == 1
