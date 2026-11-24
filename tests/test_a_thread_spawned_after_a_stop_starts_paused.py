"""A thread spawned mid-run while a stop or pause holds the run starts paused, so it moves nothing."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from orca.workflow_models.labware_threads.executing_labware_thread import ExecutingLabwareThread
from orca.workflow_models.workflows.executing_workflow import ExecutingWorkflow
from orca.workflow_models.workflows.workflow import WorkflowInstance
from orca.workflow_models.workflow_templates import WorkflowTemplate

pytestmark = pytest.mark.asyncio


def _workflow() -> ExecutingWorkflow:
    template = WorkflowTemplate(name="held_wf")
    return ExecutingWorkflow(
        workflow=WorkflowInstance(name="held_wf", template=template),
        thread_reservation_coordinator=MagicMock(),
        system_thread_manager=MagicMock(),
        event_bus=MagicMock(),
        move_handler=MagicMock(),
        status_manager=MagicMock(),
        system_map=MagicMock(),
    )


def _spawned() -> MagicMock:
    thread = MagicMock(spec=ExecutingLabwareThread)
    thread.id = "child-1"
    thread.start = AsyncMock()
    return thread


async def test_a_thread_spawned_while_the_run_is_held_starts_paused() -> None:
    workflow = _workflow()
    workflow.hold_new_threads(reason="manual")
    child = _spawned()

    workflow.start_thread(child)

    child.hold_at_start.assert_called_once_with(reason="manual")


async def test_a_thread_spawned_after_a_resume_starts_running() -> None:
    workflow = _workflow()
    workflow.hold_new_threads(reason="manual")
    workflow.release_new_threads()
    child = _spawned()

    workflow.start_thread(child)

    child.hold_at_start.assert_not_called()


async def test_an_operator_spawn_into_a_held_run_is_not_held() -> None:
    """An operator's spawn injects a thread into a paused run on purpose."""
    workflow = _workflow()
    workflow.hold_new_threads(reason="manual")
    child = _spawned()

    workflow.start_thread(child, honor_pause_hold=False)

    child.hold_at_start.assert_not_called()
