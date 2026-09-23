"""Found in review: ``ExecutingLabwareThread.stop()`` must be idempotent on
``STOPPING`` and terminal states.

Production shutdown paths iterate threads unconditionally and call
``.stop()``:

- ``ThreadManager.stop_all_threads`` (``src/orca/system/thread_manager.py``)
- ``ExecutingWorkflow.cleanup_parked_threads``
  (``src/orca/workflow_models/workflows/executing_workflow.py``)

The strict-table ``_fire(STOP_REQUESTED)`` path would otherwise raise
``InvalidThreadTransition`` whenever the iteration reaches a thread
already stopping or finalized. ``stop()`` short-circuits on those
statuses; the transition table itself stays strict (the pure TSM
unit tests still pin that contract).
"""
import asyncio
from unittest.mock import MagicMock

import pytest

from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.runtime.run_modes import WorkflowRunMode
from orca.workflow_models.labware_threads.executing_labware_thread import (
    ExecutingLabwareThread,
)
from orca.workflow_models.labware_threads.labware_thread import (
    LabwareThreadInstance,
)
from orca.workflow_models.labware_threads.thread_state_machine import (
    ThreadEvent,
)
from orca.workflow_models.status_enums import LabwareThreadStatus


def _build_executing_thread() -> ExecutingLabwareThread:
    location = Location("pad", resource=PlatePad("pad"))
    labware = LabwareInstance("plate_96", "96_well")
    thread = LabwareThreadInstance(
        labware=labware,
        start_location=location,
        end_locations=[location],
        run_mode=WorkflowRunMode.PURE_SIM,
    )

    status_state = ["CREATED"]
    status_manager = MagicMock()
    status_manager.get_status = MagicMock(
        side_effect=lambda _id: status_state[-1],
    )

    def _set_status(_kind: str, _id: str, name: str, _ctx: object) -> None:
        status_state.append(name)

    status_manager.set_status = MagicMock(side_effect=_set_status)

    context = MagicMock()
    context.execution_id = "exec-stop-idem"
    context.workflow_name = "wf-stop-idem"

    loc_service = MagicMock()
    loc_service.get_history.return_value = MagicMock()

    et = ExecutingLabwareThread(
        thread=thread,
        event_bus=MagicMock(),
        move_handler=MagicMock(),
        status_manager=status_manager,
        actions_resolver=MagicMock(),
        context=context,
        labware_location_service=loc_service,
    )
    et.publish_initial_status()
    return et


def _drive_to(et: ExecutingLabwareThread, target: LabwareThreadStatus) -> None:
    """Walk the TSM to ``target`` via legal events only (no reaching
    into private state)."""
    if target is LabwareThreadStatus.STOPPING:
        et._fire(ThreadEvent.STOP_REQUESTED)
    elif target is LabwareThreadStatus.STOPPED:
        et._fire(ThreadEvent.STOP_REQUESTED)
        et._fire(ThreadEvent.STOP_COMPLETE)
    elif target is LabwareThreadStatus.COMPLETED:
        et._fire(ThreadEvent.THREAD_COMPLETED)
    elif target is LabwareThreadStatus.ABORTED:
        et._fire(ThreadEvent.ABORT_THREAD)
    else:
        raise AssertionError(f"unsupported drive target {target}")


@pytest.mark.parametrize("status", [
    LabwareThreadStatus.STOPPING,
    LabwareThreadStatus.STOPPED,
    LabwareThreadStatus.COMPLETED,
    LabwareThreadStatus.ABORTED,
])
def test_stop_is_noop_on_stopping_and_terminal_states(
    status: LabwareThreadStatus,
) -> None:
    et = _build_executing_thread()
    _drive_to(et, status)
    assert et.status is status
    # No exception; status unchanged.
    et.stop()
    assert et.status is status


def test_stop_is_idempotent_on_repeated_calls() -> None:
    """First call from a running status transitions to STOPPING; the
    second call (with no _handle_thread_stop in between) is a no-op
    and does not re-fire the event."""
    et = _build_executing_thread()
    # Walk to a legal STOP_REQUESTED source.
    et._fire(ThreadEvent.MOVE_RESERVATION_REQUESTED)  # CREATED -> AWAITING_MOVE_RESERVATION
    et.stop()
    assert et.status is LabwareThreadStatus.STOPPING
    assert et._stop_event.is_set()
    # Second call: short-circuits inside the guard.
    et.stop()
    assert et.status is LabwareThreadStatus.STOPPING
    assert et._stop_event.is_set()


def test_stop_then_complete_then_stop_again_is_noop() -> None:
    """Real shutdown ordering: stop() during operation, then
    _handle_thread_stop transitions to STOPPED. A late
    ``cleanup_parked_threads`` pass that calls stop() on the now-
    STOPPED thread must not raise."""
    et = _build_executing_thread()
    et._fire(ThreadEvent.MOVE_RESERVATION_REQUESTED)
    et.stop()
    et._fire(ThreadEvent.STOP_COMPLETE)
    assert et.status is LabwareThreadStatus.STOPPED
    et.stop()
    assert et.status is LabwareThreadStatus.STOPPED
