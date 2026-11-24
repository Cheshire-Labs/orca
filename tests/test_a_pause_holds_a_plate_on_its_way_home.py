"""A plate going back to its end location stops at the next leg when the execution is paused."""
import asyncio
from tests.thread_builder import build_executing_thread
from collections.abc import Callable
from unittest.mock import MagicMock

import pytest

from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.modes.run_modes import WorkflowRunMode
from orca.system.reservation_manager.errors import MoveAbandonedError
from orca.workflow_models.actions.location_action import LocationAction
from orca.workflow_models.labware_threads.labware_thread import LabwareThreadInstance
from orca.workflow_models.labware_threads.thread_state_machine import ThreadEvent
from orca.workflow_models.status_enums import LabwareThreadStatus
from tests.test_helpers import let_the_loop_run


def _location(name: str) -> Location:
    return Location(name, resource=PlatePad(name))


class _TwoLegsHome:
    """A thread at the deck whose way home is deck -> handoff -> shelf; a pause arrives during the first leg."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        deck, self.handoff, shelf = _location("deck"), _location("handoff"), _location("shelf")
        self.here = deck
        self.legs_started = 0
        thread = LabwareThreadInstance(
            labware=LabwareInstance("plate_96", "96_well"),
            start_location=deck,
            end_locations=[shelf],
            run_mode=WorkflowRunMode.CHECK_RUN,
        )
        location_service = MagicMock()
        location_service.get.side_effect = lambda _labware: self.here
        status_manager = MagicMock()
        status_manager.get_status.return_value = LabwareThreadStatus.PAUSED.name
        self.executing = build_executing_thread(
            thread=thread,
            event_bus=MagicMock(),
            move_handler=MagicMock(),
            status_manager=status_manager,
            actions_resolver=MagicMock(),
            context=MagicMock(),
            labware_location_service=location_service,
        )
        self.shelf = shelf
        monkeypatch.setattr(self.executing, "_fire_and_execute_move_to", self._run_leg)
        self.fired: list[ThreadEvent] = []
        monkeypatch.setattr(self.executing, "_fire", self.fired.append)

    async def _run_leg(self, _event: ThreadEvent, _targets: list[Location]) -> None:
        self.legs_started += 1
        if self.legs_started == 1:
            self.here = self.handoff
            self.executing.request_pause()
        else:
            self.here = self.shelf

    async def parked(self) -> None:
        for _ in range(50):
            if ThreadEvent.PAUSE_REQUESTED in self.fired:
                return
            await asyncio.sleep(0)
        raise AssertionError("the thread never paused between the legs")


@pytest.mark.asyncio
async def test_a_pause_during_a_leg_home_holds_the_plate_before_the_next_leg(monkeypatch: pytest.MonkeyPatch) -> None:
    home = _TwoLegsHome(monkeypatch)
    going_home = asyncio.ensure_future(home.executing._handle_thread_completion())

    await home.parked()
    await let_the_loop_run()
    assert home.legs_started == 1
    assert home.here is home.handoff

    home.executing._resume_event.set()
    await asyncio.wait_for(going_home, timeout=5.0)
    assert home.legs_started == 2
    assert home.here is home.shelf


@pytest.mark.asyncio
async def test_a_stop_during_the_hold_runs_no_further_leg(monkeypatch: pytest.MonkeyPatch) -> None:
    home = _TwoLegsHome(monkeypatch)
    going_home = asyncio.ensure_future(home.executing._handle_thread_completion())

    await home.parked()
    home.executing._stop_event.set()
    await asyncio.wait_for(going_home, timeout=5.0)

    assert home.legs_started == 1
    assert home.here is home.handoff


class _WaitingForAMove:
    """A thread whose move request is refused until something withdraws it, as when the target device is held."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        deck, self.shelf = _location("deck"), _location("shelf")
        thread = LabwareThreadInstance(
            labware=LabwareInstance("plate_96", "96_well"),
            start_location=deck,
            end_locations=[self.shelf],
            run_mode=WorkflowRunMode.CHECK_RUN,
        )
        location_service = MagicMock()
        location_service.get.return_value = deck
        status_manager = MagicMock()
        status_manager.get_status.return_value = LabwareThreadStatus.PAUSED.name
        self.requests = 0
        move_handler = MagicMock()
        move_handler.resolve_move_action = self._resolve
        self.executing = build_executing_thread(
            thread=thread,
            event_bus=MagicMock(),
            move_handler=move_handler,
            status_manager=status_manager,
            actions_resolver=MagicMock(),
            context=MagicMock(),
            labware_location_service=location_service,
        )
        self.fired: list[ThreadEvent] = []
        monkeypatch.setattr(self.executing, "_fire", self.fired.append)
        monkeypatch.setattr(self.executing, "_execute_move_action", self._moved)
        monkeypatch.setattr(self.executing, "_plan_is_stale", lambda: False)

    async def _resolve(
        self,
        thread_id: str,
        labware: LabwareInstance,
        current_location: Location,
        targets: list[Location],
        assigned_action: LocationAction | None,
        previous_location: Location | None,
        escape_on_arrival: bool,
        withdraw: Callable[[], bool],
    ) -> MagicMock:
        self.requests += 1
        if self.requests == 1:
            while not withdraw():
                await asyncio.sleep(0)
            raise MoveAbandonedError("withdrawn")
        return MagicMock()

    async def _moved(self) -> bool:
        return True


@pytest.mark.asyncio
async def test_a_thread_waiting_for_a_move_pauses_where_it_waits_and_asks_again_on_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waiting = _WaitingForAMove(monkeypatch)
    moving = asyncio.ensure_future(
        waiting.executing._fire_and_execute_move_to(ThreadEvent.MOVE_TO_END_REQUESTED, [waiting.shelf]),
    )
    await let_the_loop_run()
    waiting.executing.request_pause()
    await let_the_loop_run()
    assert ThreadEvent.PAUSE_REQUESTED in waiting.fired
    assert waiting.requests == 1

    waiting.executing._resume_event.set()
    await asyncio.wait_for(moving, timeout=5.0)
    assert waiting.requests == 2
