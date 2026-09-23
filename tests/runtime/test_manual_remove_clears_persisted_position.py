"""A manual remove leaves NO surface still claiming the plate's old pad.

The gap this pins (2026-08-24 live finding): after an operator discharged a
plate parked at AWAITING_MANUAL_REMOVE, the persisted labware record still sat
at pad_1 -- the store kept the active position, so occupancy surfaces lied and
a reboot would re-seed the ghost back onto the pad. The end-to-end contract:
the discharge releases the park, the thread completes, the record SURVIVES
(history is not retracted), and its active position clears everywhere --
in-memory slot, persisted store, and the next boot's rehydrate.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_location_service import (
    InMemoryLabwareLocationService,
    PlacementState,
)
from orca.resource_models.labware_state import InMemoryLabwareRegistry
from orca.events.execution_context import WorkflowExecutionContext
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.registries import NullGatewayRegistry
from orca.runtime.run_modes import WorkflowRunMode
from orca.workflow_models.status_enums import LabwareThreadStatus
from orca.runtime.system_runtime import SystemRuntime
from orca.workflow_models.labware_threads.executing_labware_thread import (
    ExecutingLabwareThread,
)
from orca.workflow_models.labware_threads.labware_thread import (
    LabwareThreadInstance,
)
from tests.runtime.manual_place_fixtures import (
    _build_manual_place_system,
    _live_connection_source,
)
from tests.test_helpers import create_test_plate_template


async def _await_thread_status(
    runtime: SystemRuntime, execution_id: str, status: LabwareThreadStatus,
    *, timeout: float = 15.0,
) -> str:
    """Poll until a thread of the execution reaches `status`; return its
    labware_id."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            threads = runtime.list_threads(execution_id)
        except KeyError:
            threads = []
        for thread in threads:
            if thread.status == status.name and thread.labware_id is not None:
                return thread.labware_id
        await asyncio.sleep(0.05)
    states = [
        f"{t.name}[{t.status}]" for t in runtime.list_threads(execution_id)
    ]
    raise AssertionError(f"no thread reached {status.name}; threads: {states}")


async def test_manual_remove_clears_the_persisted_position() -> None:
    system = await _build_manual_place_system("wf_manual_remove_persist")
    store = InMemoryLabwareStore()
    runtime = SystemRuntime(
        system,
        labware_store=store,
        gateway_registry=NullGatewayRegistry(),
        connection_source=_live_connection_source(),
    )
    await runtime.start()
    try:
        submission = await runtime.submit_workflow(
            "wf_manual_remove_persist", mode=WorkflowRunMode.LIVE,
        )
        execution_id = submission.id

        # Release the start-side manual place: operator sets the plate down.
        await _await_thread_status(
            runtime, execution_id, LabwareThreadStatus.AWAITING_MANUAL_PLACE,
        )
        snap = await runtime.labware.register(
            "plate_wf_manual_remove_persist", location="pad1", confirm=True,
        )

        labware_id = await _await_thread_status(
            runtime, execution_id, LabwareThreadStatus.AWAITING_MANUAL_REMOVE,
        )
        await runtime.flush_labware_location_writes()
        active = dict(await store.list_active_locations())
        assert active.get(labware_id) == "pad1", (
            f"precondition: the parked plate's position must be persisted; "
            f"store shows {active}"
        )

        # The operator physically takes the plate off and confirms.
        await runtime.labware.discharge_labware(labware_id, force=False)

        execution = runtime._executions[execution_id]
        await asyncio.wait_for(asyncio.shield(execution.task), timeout=15.0)

        await runtime.flush_labware_location_writes()
        assert await store.list_active_locations() == [], (
            "the discharged plate's active position must clear, or the next "
            "boot re-seeds a ghost onto the pad"
        )
        assert await store.get_by_id(labware_id) is not None, (
            "discharge is a close-out, not a retraction: the record survives"
        )
        pad1 = system.system_map.get_location("pad1")
        assert pad1.labware is None

        del snap
    finally:
        await runtime.shutdown()


async def test_a_foreign_plate_in_the_slot_does_not_bind_the_waiting_thread() -> None:
    """The same ghost, from the other side: a plate of the wrong template
    landing in the awaited slot must not become the thread's labware, and must
    leave nothing persisted at the pad.

    The thread keeps waiting rather than failing. The template refusal now
    lives at `labware.register`, where the operator can act on it, and the
    thread's own wait is on ITS labware arriving -- so nothing else turning up
    in the slot can end it.
    """
    pad = PlatePad("pad_wrong")
    location = Location("pad_wrong", resource=pad)
    expected = LabwareInstance("plate_expected", "96_well")
    thread = LabwareThreadInstance(
        labware=expected,
        start_location=location,
        end_locations=[location],
        run_mode=WorkflowRunMode.LIVE,
    )
    thread.set_labware_template(create_test_plate_template("plate_expected"))

    loc_service = InMemoryLabwareLocationService()
    et = _executing_thread_for(thread, loc_service, "exec-wrong", "wf-wrong")
    persisted: list[str] = []
    loc_service.add_update_listener(lambda lw, loc: persisted.append(lw.id))

    acquire = asyncio.create_task(et.initialize_labware())
    await _await_parked(et)

    # Operator sets down a plate of the WRONG template.
    await location.place_labware(LabwareInstance("plate_other", "96_well"))

    # The wait's exit condition is this thread's OWN labware arriving, and a
    # foreign plate does not move it, so the wait cannot have ended.
    assert loc_service.placement(expected) is PlacementState.EXPECTED
    assert not acquire.done()
    assert thread.labware is expected
    assert persisted == [], "nothing the thread expects has been placed yet"

    et.stop()
    await asyncio.wait_for(acquire, timeout=5.0)


async def test_stop_before_operator_place_leaves_no_trace_of_the_labware() -> None:
    """A LIVE thread names its labware and where it belongs before any plate
    exists. A stop that beats the operator must leave nothing claiming that
    pad -- not in the ledger, and above all not in the store, where a position
    IS a real plate and the next boot re-places it."""
    pad = PlatePad("pad_stop")
    location = Location("pad_stop", resource=pad)
    expected = LabwareInstance("plate_stop", "96_well")
    thread = LabwareThreadInstance(
        labware=expected,
        start_location=location,
        end_locations=[location],
        run_mode=WorkflowRunMode.LIVE,
    )
    thread.set_labware_template(create_test_plate_template("plate_stop"))

    loc_service = InMemoryLabwareLocationService()
    et = _executing_thread_for(thread, loc_service, "exec-stop", "wf-stop")
    persisted: list[str] = []
    dropped: list[LabwareInstance] = []
    loc_service.add_update_listener(lambda lw, loc: persisted.append(lw.id))
    loc_service.add_expectation_dropped_listener(dropped.append)

    acquire = asyncio.create_task(et.initialize_labware())
    await _await_parked(et)
    assert not acquire.done()
    assert location.labware is None

    et.stop()
    await asyncio.wait_for(acquire, timeout=5.0)

    assert persisted == [], "an unplaced labware must never reach the store"
    assert dropped == [expected]
    with pytest.raises(KeyError):
        loc_service.get(expected)


def _executing_thread_for(
    thread: LabwareThreadInstance,
    loc_service: InMemoryLabwareLocationService,
    execution_id: str,
    workflow_name: str,
) -> ExecutingLabwareThread:
    """A thread wired with a status stub that echoes real enum names, because
    `status` reads back through the manager as a name string."""
    context = MagicMock()
    context.execution_id = execution_id
    context.workflow_name = workflow_name
    status_state = ["CREATED"]
    status_manager = MagicMock()
    status_manager.get_status = MagicMock(side_effect=lambda _id: status_state[-1])

    def _set_status(
        _kind: str, _id: str, name: str, _ctx: WorkflowExecutionContext,
    ) -> None:
        status_state.append(name)

    status_manager.set_status = MagicMock(side_effect=_set_status)
    # A LIVE manual place claims the slot it waits at, so the stand-in for the
    # MoveHandler has to answer that call rather than hand back a MagicMock.
    move_handler = MagicMock()
    move_handler.acquire_placement_reservation = AsyncMock(return_value=MagicMock())
    move_handler.try_acquire_placement_reservation = AsyncMock(
        return_value=MagicMock(is_released=False, is_displaced=False),
    )
    et = ExecutingLabwareThread(
        thread=thread,
        event_bus=MagicMock(),
        move_handler=move_handler,
        status_manager=status_manager,
        actions_resolver=MagicMock(),
        context=context,
        labware_location_service=loc_service,
    )
    et.set_labware_registry(InMemoryLabwareRegistry())
    return et


async def _await_parked(et: ExecutingLabwareThread) -> None:
    deadline = asyncio.get_running_loop().time() + 5.0
    while et.status is not LabwareThreadStatus.AWAITING_MANUAL_PLACE:
        assert asyncio.get_running_loop().time() < deadline, et.status
        await asyncio.sleep(0.01)
