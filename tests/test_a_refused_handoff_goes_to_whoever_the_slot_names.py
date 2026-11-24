"""A handoff that is refused raises in the ending thread, unless the slot was given someone to take it."""
from unittest.mock import MagicMock

import pytest

from orca.modes.run_modes import WorkflowRunMode
from orca.resource_models.capacity import CapacityExceededError
from orca.workflow_models.labware_state import LabwareSlot
from orca.workflow_models.method import ExecutingMethod
from orca.workflow_models.workflows.executing_workflow import ExecutingWorkflow


def _a_slot_with_one_method_waiting() -> tuple[LabwareSlot, ExecutingMethod]:
    slot = LabwareSlot(slot_key="tips", labware_template_name="tips")
    method = MagicMock(spec=ExecutingMethod)
    method.completed = MagicMock()
    method.completed.is_set.return_value = False
    slot.queue.put_nowait(method)
    return slot, method


async def _refuse(labware_name: str, method: ExecutingMethod, run_mode: WorkflowRunMode) -> None:
    raise CapacityExceededError("used up")


async def test_a_slot_nobody_takes_refusals_for_raises_in_the_ending_thread() -> None:
    slot, _ = _a_slot_with_one_method_waiting()

    with pytest.raises(CapacityExceededError, match="used up"):
        await slot.drain_for_handoff(_refuse, WorkflowRunMode.CHECK_RUN)


async def test_a_slot_with_someone_to_take_a_refusal_hands_it_the_method_and_the_reason() -> None:
    slot, method = _a_slot_with_one_method_waiting()
    taken: list[tuple[str, ExecutingMethod, str]] = []
    slot.on_refused_handoff = lambda name, refused, mode, reason: taken.append((name, refused, str(reason)))

    await slot.drain_for_handoff(_refuse, WorkflowRunMode.CHECK_RUN)

    assert taken == [("tips", method, "used up")]


async def test_the_workflow_takes_a_refusal_only_for_a_method_that_named_its_partner() -> None:
    slot, method = _a_slot_with_one_method_waiting()
    method.partner_barcodes = {}
    workflow = ExecutingWorkflow.__new__(ExecutingWorkflow)
    workflow._unserved = []
    slot.on_refused_handoff = workflow._leave_a_named_method_unserved

    with pytest.raises(CapacityExceededError, match="used up"):
        await slot.drain_for_handoff(_refuse, WorkflowRunMode.CHECK_RUN)
    assert workflow._unserved == []
