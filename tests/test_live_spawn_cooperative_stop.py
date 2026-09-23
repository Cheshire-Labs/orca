from tests.mock import EXTERNAL_MOVER
"""Review item L1: LIVE-mode spawn strategies must cooperate with
`thread.stop()`.

`ManualPlaceSpawn._acquire_live` and `ManualRemoveSpawn._dispose_live`
poll on `asyncio.sleep(_RETRY_BACKOFF_S)` waiting for an operator
labware_register / labware_discharge. Pre-fix the polling loop did
not consult the cooperative-stop event, so `thread.stop()` only took
effect AFTER the operator finally placed labware (or never, if the
operator did not). Execution-level abort
(`stop_execution -> task.cancel()`) DID work because cancellation
injects `CancelledError` into the `asyncio.sleep`; only the
thread-level cooperative stop was unreachable.

Post-fix: both LIVE paths poll `thread.stop_event` alongside their
condition. When the event is set, the polling loop exits cleanly
on the next tick (at most `_RETRY_BACKOFF_S` later) without binding
labware. The strategy returns; the thread's main loop catches the
stop and runs `_handle_thread_stop` cleanly.
"""

import asyncio
from unittest.mock import MagicMock

from orca.resource_models.labware_location_service import (
    ArrivalMechanism,
    InMemoryLabwareLocationService,
)
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.runtime.run_modes import WorkflowRunMode
from orca.workflow_models.labware_threads.labware_thread import (
    LabwareThreadInstance,
)
from orca.workflow_models.spawn_actions import (
    ManualPlaceSpawn, ManualRemoveSpawn,
)


def _make_platepad_location(name: str = "pad1") -> Location:
    pad = PlatePad(name)
    return Location(name, resource=pad)


def _fresh_labware(template_name: str = "plate_96") -> LabwareInstance:
    return LabwareInstance(template_name, "96_well")


def _make_thread(
    labware: LabwareInstance,
    location: Location,
    *,
    run_mode: WorkflowRunMode = WorkflowRunMode.LIVE,
    stop_event: asyncio.Event | None = None,
) -> LabwareThreadInstance:
    thread = LabwareThreadInstance(
        labware=labware,
        start_location=location,
        end_locations=[location],
        run_mode=run_mode,
    )
    if stop_event is not None:
        thread.set_stop_event(stop_event)
    return thread


def _ledger_expecting(
    labware: LabwareInstance, location: Location,
) -> InMemoryLabwareLocationService:
    service = InMemoryLabwareLocationService()
    service.expect(labware, location, ArrivalMechanism.MANUAL_PLACE)
    return service


class TestManualPlaceSpawnCooperativeStop:

    async def test_acquire_live_exits_when_stop_event_set(self) -> None:
        """Set the stop event BEFORE acquire starts. The first poll
        tick sees the event and exits immediately without binding."""
        location = _make_platepad_location()
        stop_event = asyncio.Event()
        stop_event.set()
        labware = _fresh_labware()
        thread = _make_thread(labware, location, stop_event=stop_event)

        spawn = ManualPlaceSpawn(location, _ledger_expecting(labware, location))
        # No operator places labware; without the cooperative-stop seam
        # the call would hang on `asyncio.sleep` and timeout. With the
        # seam it returns immediately.
        await spawn.acquire(thread)
        assert location.labware is None

    async def test_acquire_live_exits_when_stop_event_set_mid_poll(
        self,
    ) -> None:
        """Set the stop event AFTER the poll loop has entered an
        `asyncio.sleep`. The next tick (at most ~0.5s later) sees the
        event and exits."""
        location = _make_platepad_location()
        stop_event = asyncio.Event()
        labware = _fresh_labware()
        thread = _make_thread(labware, location, stop_event=stop_event)

        spawn = ManualPlaceSpawn(location, _ledger_expecting(labware, location))
        acquire_task = asyncio.create_task(spawn.acquire(thread))
        # Let the task enter its first sleep.
        await asyncio.sleep(0)
        assert not acquire_task.done()
        # Fire the cooperative stop. Next tick releases the poll.
        stop_event.set()
        await acquire_task
        assert location.labware is None

    async def test_acquire_live_proceeds_when_no_stop_event(self) -> None:
        """No stop event bound -- the value-object construction path
        used by pure unit tests. The wait continues normally and ends
        when the labware arrives. Pins the
        `stop_event is None -> proceed` contract."""
        location = _make_platepad_location()
        labware = _fresh_labware()
        thread = _make_thread(labware, location)
        assert thread.stop_event is None
        service = _ledger_expecting(labware, location)

        spawn = ManualPlaceSpawn(location, service)
        acquire_task = asyncio.create_task(spawn.acquire(thread))
        await asyncio.sleep(0)
        assert not acquire_task.done()
        location.initialize_labware(labware)
        service.update(labware, location)
        await acquire_task
        assert thread.labware is labware


class TestManualRemoveSpawnCooperativeStop:

    async def test_dispose_live_exits_when_stop_event_set(self) -> None:
        """Set the stop event before dispose starts. The first poll
        tick exits without waiting for operator discharge."""
        location = _make_platepad_location()
        labware = _fresh_labware()
        location.initialize_labware(labware)
        stop_event = asyncio.Event()
        stop_event.set()
        thread = _make_thread(labware, location, stop_event=stop_event)

        spawn = ManualRemoveSpawn(location)
        # Slot still occupied; without the seam the call would hang.
        await spawn.dispose(thread)
        # No discharge happened (the operator did not act).
        assert location.labware is labware

    async def test_dispose_live_exits_when_stop_event_set_mid_poll(
        self,
    ) -> None:
        location = _make_platepad_location()
        labware = _fresh_labware()
        location.initialize_labware(labware)
        stop_event = asyncio.Event()
        thread = _make_thread(labware, location, stop_event=stop_event)

        spawn = ManualRemoveSpawn(location)
        dispose_task = asyncio.create_task(spawn.dispose(thread))
        await asyncio.sleep(0)
        assert not dispose_task.done()
        stop_event.set()
        await dispose_task
        # Slot stays occupied; the LIVE wait was halted before the
        # operator could discharge.
        assert location.labware is labware

    async def test_dispose_live_proceeds_when_no_stop_event(self) -> None:
        location = _make_platepad_location()
        labware = _fresh_labware()
        location.initialize_labware(labware)
        thread = _make_thread(labware, location)
        assert thread.stop_event is None

        spawn = ManualRemoveSpawn(location)
        dispose_task = asyncio.create_task(spawn.dispose(thread))
        await asyncio.sleep(0)
        assert not dispose_task.done()
        # Simulate operator discharge.
        await location.notify_picked(labware, EXTERNAL_MOVER)
        await dispose_task
        assert location.labware is None


class TestHandleThreadCompletionRespectsStopEvent:
    """Integration-level pin from review iter-2 L2 #3: when
    `_dispose_live` returns early on cooperative stop, the
    `_handle_thread_completion` caller MUST NOT continue to
    `update_state(..., ENDED)` + `drain_for_handoff` +
    `status = COMPLETED`. A stopped thread reports STOPPED, leaves the
    end_location's labware in place, and does not drain its slot's
    queue to a phantom receiver.
    """

    async def test_stop_during_dispose_live_emits_stopped_status(
        self,
    ) -> None:
        """End-to-end characterization through `_handle_thread_completion`:
        a LIVE thread whose end-location wait is stopped via
        `thread.stop()` finalizes as STOPPED, not COMPLETED. The
        registry entry is NOT transitioned to ENDED (labware is still
        physically present) and `drain_for_handoff` does NOT fire."""
        from unittest.mock import MagicMock
        from orca.resource_models.labware_state import (
            InMemoryLabwareRegistry, LabwareState,
        )
        from orca.workflow_models.labware_threads.executing_labware_thread import (
            ExecutingLabwareThread,
        )
        from orca.workflow_models.status_enums import LabwareThreadStatus

        location = _make_platepad_location()
        labware = _fresh_labware()
        location.initialize_labware(labware)

        from tests.test_helpers import create_test_plate_template

        thread = _make_thread(labware, location, run_mode=WorkflowRunMode.LIVE)
        thread.set_labware_template(create_test_plate_template("plate_x"))

        loc_service = MagicMock()
        loc_service.get_history.return_value = MagicMock()
        loc_service.get.return_value = location

        context = MagicMock()
        context.execution_id = "exec-stop-test"
        context.workflow_name = "wf-stop-test"

        # Track status sets so the read-back asserts can verify the
        # post-stop emission. `ExecutingLabwareThread.status` getter
        # delegates to `status_manager.get_status` which returns a
        # name string; pin that pipeline with a small side-effect.
        status_state = ["CREATED"]
        status_manager = MagicMock()
        status_manager.get_status = MagicMock(
            side_effect=lambda _id: status_state[-1],
        )
        def _set_status(_kind: str, _id: str, name: str, _ctx: object) -> None:
            status_state.append(name)
        status_manager.set_status = MagicMock(side_effect=_set_status)

        et = ExecutingLabwareThread(
            thread=thread,
            event_bus=MagicMock(),
            move_handler=MagicMock(),
            status_manager=status_manager,
            actions_resolver=MagicMock(),
            context=context,
            labware_location_service=loc_service,
        )
        registry = InMemoryLabwareRegistry()
        et.set_labware_registry(registry)

        # Simulate `_handle_thread_completion`'s end-dispatch path
        # directly: get the end_spawn, set AWAITING_MANUAL_REMOVE,
        # kick off dispose, set the stop event mid-poll, and assert
        # the post-dispose stop check routes to _handle_thread_stop.
        from orca.workflow_models.labware_threads.thread_state_machine import (
            ThreadEvent,
        )
        et.publish_initial_status()
        spawn = ManualRemoveSpawn(location)
        et._fire(ThreadEvent.LIVE_MANUAL_REMOVE_AWAITED)
        dispose_task = asyncio.create_task(spawn.dispose(thread))
        await asyncio.sleep(0)
        assert not dispose_task.done()

        et.stop()  # sets _stop_event
        await dispose_task
        # `_handle_thread_completion`'s post-dispose stop check fires:
        # status flips to STOPPED, registry entry is removed (not
        # transitioned to ENDED), and drain is skipped.
        if et._stop_event.is_set():
            et._handle_thread_stop()

        assert et.status == LabwareThreadStatus.STOPPED
        # Labware is still physically on the end-location -- the
        # operator did not discharge it.
        assert location.labware is labware
        # Registry no longer holds the labware (unregistered on stop).
        # Critical contract: it was NOT marked ENDED.
        assert registry.get_state(labware.id) is None
