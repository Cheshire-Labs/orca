"""What `ctx.set_partner_constraint` names binds the method, and a step whose input carries another barcode does not run."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from orca.events.event_bus import EventBus
from orca.events.execution_context import MethodExecutionContext, WorkflowExecutionContext
from orca.events.event_channel import EventChannelRegistry
from orca.plugins.declared_tracking_observer import DeclaredTrackingObserver
from orca.resource_models.error_policy_overrides import WrongPartnerPlateError
from orca.resource_models.tracking_context import TrackingContext
from orca.state.ops_history import OpsHistory
from orca.variables.variable_store import NullVariableResolver
from orca.workflow_models.actions.executable_location_action import ExecutableLocationAction
from orca.workflow_models.method import ExecutingMethod, MethodInstance
from orca.workflow_models.status_manager import StatusManager
from orca.workflow_models.thread_context import ThreadContext
from tests.memory_activity import memory_ops_store


def _method() -> ExecutingMethod:
    bus = EventBus()
    context = WorkflowExecutionContext(execution_id="e1", workflow_name="wf")
    return ExecutingMethod(MethodInstance("index_step"), bus, StatusManager(bus), context)


def _input(template_name: str, barcode: str | None) -> MagicMock:
    labware = MagicMock()
    labware.name = f"{template_name}_1"
    labware.template_name = template_name
    labware.barcode = barcode
    return labware


def _step(*inputs: MagicMock) -> tuple[ExecutableLocationAction, AsyncMock]:
    action = MagicMock()
    action.id = "a1"
    action.command = "transfer"
    action.location.name = "lh"
    action.expected_inputs = list(inputs)
    action.peek_missing_input_labware.return_value = []
    body = AsyncMock()
    action.execute = body
    action.declares = None
    action.build_template_to_instance_map.return_value = {}
    present = asyncio.Event()
    present.set()
    action.all_labware_is_present = present
    context = MethodExecutionContext(
        execution_id="e1", workflow_name="wf", method_id="m1", method_name="index_step",
        thread_id="t1", thread_name="sample_1", participating_thread_ids=("t1",),
    )
    history = OpsHistory(store=memory_ops_store(), execution_id="e1")
    tracking = TrackingContext(observer=DeclaredTrackingObserver(), ops_history=history)
    return ExecutableLocationAction(MagicMock(), action, context, tracking_context=tracking), body


def test_a_thread_context_with_no_thread_to_bind_refuses_a_constraint() -> None:
    ctx = ThreadContext(EventChannelRegistry(), NullVariableResolver(), "e1")

    with pytest.raises(RuntimeError, match="not available"):
        ctx.set_partner_constraint("index", {"barcode": "IDX-2"})


def test_threads_that_name_the_same_partner_bind_the_method_to_it() -> None:
    method = _method()

    method.set_partner_constraints("index", {"barcode": "IDX-2"})
    method.set_partner_constraints("index", {"barcode": "IDX-2"})

    assert method.partner_barcodes == {"index": "IDX-2"}


def test_a_second_thread_naming_another_partner_is_refused_and_the_first_name_stands() -> None:
    method = _method()
    method.set_partner_constraints("index", {"barcode": "IDX-2"})

    with pytest.raises(WrongPartnerPlateError, match="IDX-2.*IDX-9"):
        method.set_partner_constraints("index", {"barcode": "IDX-9"})

    assert method.partner_barcodes == {"index": "IDX-2"}


@pytest.mark.asyncio
async def test_a_step_whose_input_carries_another_barcode_is_refused_and_its_body_never_runs() -> None:
    step, body = _step(_input("sample", "S-2"), _input("index", "IDX-1"))
    step.require_partner_barcodes(lambda: {"index": "IDX-2"})

    with pytest.raises(WrongPartnerPlateError, match="'IDX-2'.*'IDX-1'"):
        await step.execute()

    body.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_step_whose_inputs_carry_the_named_barcodes_runs_its_body() -> None:
    step, body = _step(_input("sample", "S-2"), _input("index", "IDX-2"))
    step.require_partner_barcodes(lambda: {"index": "IDX-2"})

    await step.execute()

    body.assert_awaited_once()
